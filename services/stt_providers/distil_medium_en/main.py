"""Distil-Whisper Medium.en STT Provider (GPU).

Uses WhisperStreamingEngine with distil-medium.en model on CUDA.
Stage B benchmark: WER 0.0093, 192ms avg latency.
"""
import argparse
import logging
import signal
import sys
import threading
import time

from shared_stt.ws_forwarder import WSForwarder, WebSocketLogHandler
from shared_stt.audio_processor import AudioProcessor
from shared_stt.redact import redact_transcript
from shared_stt.whisper_engine import WhisperStreamingEngine
from shared_audio.agc import AGCConfig
from shared_audio.capture import get_audio_provider, AudioConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("DistilMedium")

# Whisper conditions on a 224-token prompt window; faster-whisper caps
# the hotwords string at 223 tokens and keeps the HEAD when it
# truncates (get_prompt, venv-verified). Hint-style content measures
# ~3 chars/token (not the naive ~4), so 800 chars can exceed the cap --
# which is safe ONLY because the string is emitted newest-first: any
# token-level truncation eats the oldest hints (wh-apmg.1.1).
_HOTWORDS_CHAR_BUDGET = 800


def _import_hints_updater():
    """Import the shared loose-module hints_updater (same sys.path dance
    the add-hint handler uses; the shared dir is not a package)."""
    from pathlib import Path
    shared_path = Path(__file__).parent.parent / "shared"
    if str(shared_path) not in sys.path:
        sys.path.insert(0, str(shared_path))
    import hints_updater
    return hints_updater


def build_hotwords_string() -> str | None:
    """Join hints.txt into faster-whisper's hotwords bias string.

    hints.txt appends, so the newest hints sit at the end of the file.
    The string is emitted NEWEST FIRST: faster-whisper truncates
    hotwords at 223 tokens keeping the head, so newest-first makes the
    newest-hints-survive guarantee hold under any tokenization density
    (wh-apmg.1.1). When the joined string would exceed the char budget
    the OLDEST hints are dropped and a warning names the count. Returns
    None (feature off) when there are no hints or the file cannot be
    read -- startup must never fail on hints (wh-apmg).
    """
    try:
        hints = _import_hints_updater().get_hints()
    except Exception as e:
        logger.warning(f"Could not load hints for hotwords: {e}")
        return None
    if not hints:
        return None
    selected: list[str] = []
    used = 0
    for hint in reversed(hints):  # newest first
        cost = len(hint) + (2 if selected else 0)  # ", " separator
        if used + cost > _HOTWORDS_CHAR_BUDGET:
            break
        selected.append(hint)
        used += cost
    dropped = len(hints) - len(selected)
    if dropped:
        logger.warning(
            f"hotwords budget: dropped {dropped} oldest hint(s); "
            f"keeping the newest {len(selected)} within "
            f"{_HOTWORDS_CHAR_BUDGET} chars"
        )
    # No re-reversal: newest stays first (see docstring, wh-apmg.1.1).
    return ", ".join(selected)


class DistilMediumServer:
    """STT server using distil-whisper medium.en on GPU."""

    DISCONNECT_TIMEOUT_S = 5.0
    DISPLAY_NAME = "Distil Medium (GPU)"

    def __init__(
        self,
        model_config: dict,
        engine_config: dict,
        ws_host: str = "localhost",
        ws_port: int = 0,
        vad_threshold: float = 0.5,
        vad_lead_in_ms: int = 300,
        agc_config: AGCConfig | None = None,
        sample_rate: int = 16000,
        chunk_ms: int = 30,
        wake_word_enabled: bool = False,
        wake_word_keyword: str = "computer",
        wake_word_sensitivity: float = 0.5,
        wake_word_mode: str = "idle_recovery",
        wake_word_model_dir: str = "data/wake_words",
        hotwords: str | None = None,
        hotwords_enabled: bool = True,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        # Construction-time [hotwords] gate value; the add-hint handler
        # re-reads config.toml and falls back to this cache only when
        # the re-read fails (parity with parakeet, wh-q33mj.4.1).
        self.hotwords_enabled = hotwords_enabled

        audio_config = AudioConfig(rate=sample_rate, channels=1, chunk_ms=chunk_ms)
        self.audio_capture = get_audio_provider(config=audio_config)

        self.engine = WhisperStreamingEngine(
            sample_rate=sample_rate,
            hotwords=hotwords,
            **model_config,
            **engine_config,
        )

        self.transcription_enabled = threading.Event()
        self.transcription_enabled.set()

        self._disconnect_timer: threading.Timer | None = None
        self._ws_log_handler = None

        self.forwarder = WSForwarder(
            host=ws_host,
            port=ws_port,
            transcription_enabled_event=self.transcription_enabled,
            restart_callback=self._handle_restart_service,
            hard_restart_callback=self._handle_hard_restart_service,
            on_disconnect_callback=self._handle_wheelhouse_disconnect,
            on_reconnect_callback=self._handle_wheelhouse_reconnect,
            shutdown_callback=self._handle_shutdown,
            set_interim_results_callback=self._handle_set_interim_results,
            set_log_level_callback=self._handle_set_log_level,
            add_hint_callback=self._handle_add_hint,
            wake_word_activate_callback=self._handle_wake_word_activate,
            debug=True,
            provider_name="distil_medium_en",
            emits_eos=False,
        )

        self.audio_processor = AudioProcessor(
            engine=self.engine,
            forwarder=self.forwarder,
            sample_rate=sample_rate,
            vad_threshold=vad_threshold,
            vad_lead_in_ms=vad_lead_in_ms,
            agc_config=agc_config,
            force_endpoint_silence_ms=engine_config.get("endpoint_silence_ms", 500),
        )

        self.running = False

        # Wake word detection
        self._wake_word_detector = None
        self._wake_word_listening = False
        self._wake_word_mode = wake_word_mode
        if wake_word_enabled:
            from shared_stt.wake_word_detector import WakeWordDetector
            self._wake_word_detector = WakeWordDetector(
                keyword=wake_word_keyword,
                model_dir=wake_word_model_dir,
                sensitivity=wake_word_sensitivity,
            )
            logger.info(
                f"Wake word detector initialized: keyword='{wake_word_keyword}', "
                f"mode='{wake_word_mode}', loaded={self._wake_word_detector.is_loaded}"
            )

    # -- Command handlers (standard boilerplate) --

    def _handle_shutdown(self):
        logger.info("Shutdown command received - exiting cleanly")
        self.stop()

    def _handle_restart_service(self):
        # Same defect as wh-parakeet-soft-restart-noop: load_config()
        # proves only that config.toml parses; nothing is applied to the
        # running service. Say so instead of claiming a reload.
        logger.info("Restart service command received - validating config")
        self.forwarder.send_notification(self.DISPLAY_NAME, "Validating configuration...")
        try:
            load_config()
            self.forwarder.send_notification(
                self.DISPLAY_NAME,
                "Configuration valid - restart to apply changes",
            )
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            self.forwarder.send_notification(self.DISPLAY_NAME, f"Failed to reload: {e}")

    def _write_restart_flag(self) -> bool:
        """Write the launcher restart flag; True when it is durably on
        disk. Callers must stop ONLY after a True return. Exiting with
        code 0 and no flag reads as a clean shutdown to the launcher
        (should_restart), which would leave STT permanently dead from a
        voice command (wh-q33mj.1.3 parity, wh-distil-hint-handler-parity)."""
        try:
            from shared_stt.launcher import get_restart_flag_path
            flag_path = get_restart_flag_path("distil_medium_en")
            with open(flag_path, "w") as f:
                f.write("restart")
            return True
        except Exception as e:
            logger.error(f"Failed to create restart flag, staying up: {e}")
            return False

    def _hotwords_enabled_now(self) -> bool:
        """Read the [hotwords] enabled gate fresh from config.toml so a
        user edit after startup takes effect; fall back to the
        construction-time cache when the file is unreadable mid-edit."""
        try:
            config = load_config()
            enabled = bool(config.get("hotwords", {}).get("enabled", True))
            self.hotwords_enabled = enabled
            return enabled
        except Exception as e:
            logger.warning(
                f"Could not re-read config for hotwords gate, using cached "
                f"value {self.hotwords_enabled}: {e}"
            )
            return self.hotwords_enabled

    def _handle_hard_restart_service(self):
        logger.info("Hard restart requested - creating flag file and exiting")
        # A failed flag write converts to a deferred apply: keep
        # running, the change lands at the next successful restart.
        if not self._write_restart_flag():
            self.forwarder.send_notification(
                self.DISPLAY_NAME,
                "Restart failed - change saved, will apply at next restart",
            )
            return
        self.forwarder.send_notification(self.DISPLAY_NAME, "Full restart in progress...")
        self.stop()

    def _handle_set_interim_results(self, enabled: bool):
        self.audio_processor.send_interim_results = enabled
        logger.info(f"Interim results {'enabled' if enabled else 'disabled'}")

    def _handle_set_log_level(self, level: str):
        numeric = logging.getLevelName(level.upper())
        if self._ws_log_handler:
            self._ws_log_handler.setLevel(numeric)
        logger.info("Log forwarding level set to %s", level)

    def _handle_add_hint(self, hint: str):
        """Add a hint to the shared hints.txt and hard-restart so the
        rebuilt hotwords string takes effect.

        add_hint returns False for BOTH a duplicate and an I/O error (it
        catches Exception internally), so the duplicate report is only
        trusted after confirming the hint is actually present
        (wh-q33mj.1.2 parity). The restart is skipped when hotwords are
        disabled in config -- it would reload the model and apply
        nothing."""
        try:
            hints_updater = _import_hints_updater()
            logger.info(f"Processing add_hint request: '{redact_transcript(hint)}'")
            success = hints_updater.add_hint(hint)
            if success:
                if not self._hotwords_enabled_now():
                    logger.info(
                        "Hint added; hotwords disabled in config, no restart"
                    )
                    self.forwarder.send_notification(
                        "STT Hint Saved",
                        f"Saved '{hint}' - enable [hotwords] in config.toml "
                        "to use it",
                    )
                    return
                logger.info("Hint added successfully, triggering hard restart")
                # Announce the restart only AFTER the flag is durably
                # written (wh-q33mj.3.1 parity): announcing first and
                # then failing the flag write sent two contradictory
                # voice notifications back to back.
                if not self._write_restart_flag():
                    self.forwarder.send_notification(
                        "STT Hint Saved",
                        f"Saved '{hint}' - restart failed, will apply at "
                        "next restart",
                    )
                    return
                self.forwarder.send_notification(
                    "STT Hint Added", f"Added '{hint}' - restarting to apply"
                )
                self.stop()
                return
            stored = {h.lower() for h in hints_updater.get_hints()}
            # Mirror add_hint's normalization (strip, truncate to 100,
            # strip, case-fold) or a long duplicate is misreported as a
            # write failure (wh-q33mj.2.1 parity).
            normalized = hint.strip()[:100].strip().lower()
            if normalized in stored:
                logger.info(f"Hint already exists: '{redact_transcript(hint)}'")
                self.forwarder.send_notification(
                    "STT Hint", f"Hint '{hint}' already exists"
                )
            else:
                logger.error(
                    f"Hint '{redact_transcript(hint)}' was not saved "
                    f"(hints file write failed)"
                )
                self.forwarder.send_notification(
                    "STT Error", f"Could not save hint '{hint}'"
                )
        except Exception as e:
            logger.error(f"Error adding hint: {e}")
            self.forwarder.send_notification("STT Error", f"Failed to add hint: {e}")

    def _handle_wake_word_activate(self, reason):
        if reason is None:
            self._wake_word_listening = False
            return
        if not self._wake_word_detector or not self._wake_word_detector.is_loaded:
            return
        should_activate = False
        if self._wake_word_mode == "idle_recovery":
            should_activate = (reason == "idle")
        elif self._wake_word_mode == "push_to_talk":
            should_activate = (reason in ("idle", "audio", "sonos"))
        if should_activate:
            self._wake_word_detector.reset()
            self._wake_word_listening = True
            logger.info(f"Wake word listening activated (reason={reason})")
        else:
            self._wake_word_listening = False

    def _handle_disconnect_timeout(self):
        logger.info(f"WheelHouse disconnected for {self.DISCONNECT_TIMEOUT_S}s - exiting cleanly")
        self.stop()

    def _handle_wheelhouse_disconnect(self):
        logger.info("WheelHouse disconnected - pausing, will exit in 5s if no reconnect")
        self.transcription_enabled.clear()
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._disconnect_timer = threading.Timer(self.DISCONNECT_TIMEOUT_S, self._handle_disconnect_timeout)
        self._disconnect_timer.daemon = True
        self._disconnect_timer.start()

    def _handle_wheelhouse_reconnect(self):
        logger.info("WheelHouse reconnected - resuming transcription")
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
        self.transcription_enabled.set()

    # -- Audio loop and lifecycle --

    def process_audio_loop(self):
        logger.info("Starting audio processing loop...")
        try:
            while self.running:
                chunk = self.audio_capture.read(timeout=0.02)
                if chunk is None:
                    continue
                if not self.running:
                    break
                if not self.transcription_enabled.is_set():
                    if self._wake_word_detector and self._wake_word_listening:
                        result = self._wake_word_detector.process(chunk)
                        if result:
                            self.forwarder.send_wake_word_detected(result)
                            self._wake_word_listening = False
                            self.transcription_enabled.set()
                            logger.info(f"Wake word '{result}' detected - resuming transcription")
                    continue
                self.audio_processor.process_chunk(chunk)
        except Exception as e:
            logger.error(f"Audio processing error: {e}", exc_info=True)

    def _send_startup_notification(self):
        def send_ready():
            time.sleep(3.0)
            logger.info("Sending 'ready' notification to WheelHouse...")
            self.forwarder.send_notification(self.DISPLAY_NAME, "Transcription service ready")
        threading.Thread(target=send_ready, daemon=True).start()

    def start(self):
        self.running = True
        self.forwarder.start()
        logger.info("WebSocket forwarder started")

        ws_log_handler = WebSocketLogHandler(self.forwarder, source=self.DISPLAY_NAME)
        ws_log_handler.setLevel(logging.INFO)
        logger.addHandler(ws_log_handler)
        logger.propagate = False
        self._ws_log_handler = ws_log_handler
        # shared_stt has propagation off (to suppress noisy defaults); attach
        # the handler to specific submodules (NOT parent shared_stt, which
        # would catch shared_stt.ws_forwarder's own "sent log" logs and
        # create an infinite feedback loop). wh-7ou.2 instrumentation.
        for submodule in ("shared_stt.whisper_engine", "shared_stt.audio_processor"):
            logging.getLogger(submodule).addHandler(ws_log_handler)
        logging.getLogger("shared_stt").propagate = False

        self._send_startup_notification()

        self.audio_capture.start()
        logger.info("Audio capture started")

        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, stopping...")
            self.stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            self.process_audio_loop()
        finally:
            self.cleanup()

    def stop(self):
        logger.info("Stopping server...")
        self.running = False

    def cleanup(self):
        logger.info("Cleaning up...")
        self.audio_capture.stop()
        self.engine.cleanup()
        self.forwarder.stop()
        logger.info("Server stopped cleanly")


def load_config() -> dict:
    """Load configuration from config.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    from pathlib import Path

    config_path = Path(__file__).parent / "config.toml"
    config = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        logger.info(f"Loaded config from {config_path}")
    else:
        logger.warning("No config.toml found, using defaults")
    return config


if __name__ == "__main__":
    config = load_config()
    model_config = config.get("model", {})
    engine_config = config.get("engine", {})
    client_config = config.get("client", {})
    agc_config_data = config.get("agc", {})

    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-host", default="localhost")
    parser.add_argument("--ws-port", type=int, required=True)
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--wake-word-enabled", action="store_true", default=False)
    parser.add_argument("--wake-word-keyword", default=None)
    parser.add_argument("--wake-word-sensitivity", type=float, default=None)
    parser.add_argument("--wake-word-mode", default=None)
    parser.add_argument("--wake-word-model-dir", default=None)
    args = parser.parse_args()

    if args.list_devices:
        audio_config = AudioConfig(rate=16000, channels=1, chunk_ms=30)
        mic = get_audio_provider(config=audio_config)
        print("Available audio input devices:")
        for dev in mic.list_audio_devices():
            print(f"  [{dev['index']}] {dev['name']}")
        sys.exit(0)

    agc_config = AGCConfig(
        enabled=agc_config_data.get("enabled", True),
        target_speech_rms=agc_config_data.get("target_speech_rms", 0.1),
        vad_threshold_rms=agc_config_data.get("vad_threshold_rms", 0.08),
        noise_floor_alpha=agc_config_data.get("noise_floor_alpha", 0.02),
        min_gain=agc_config_data.get("min_gain", 0.1),
        max_gain=agc_config_data.get("max_gain", 10.0),
        initial_noise_floor=agc_config_data.get("initial_noise_floor", 0.01),
    )

    sample_rate = client_config.get("rate", 16000)
    chunk_ms = client_config.get("chunk_ms", 30)
    vad_threshold = client_config.get("silero_threshold", 0.5)
    vad_lead_in_ms = client_config.get("vad_lead_in_ms", 300)

    # wh-apmg.1.2: the escape hatch. Emptying shared/hints.txt would
    # also destroy Google STT phrase adaptation and parakeet hotwords,
    # so a misbehaving distil bias needs its own off switch.
    hotwords_enabled = bool(config.get("hotwords", {}).get("enabled", True))
    hotwords = build_hotwords_string() if hotwords_enabled else None
    if hotwords:
        logger.info(f"Hotwords loaded from hints.txt ({len(hotwords)} chars)")
    elif not hotwords_enabled:
        logger.info("Hotwords disabled via [hotwords] enabled=false")
    else:
        logger.info("No hints.txt hotwords (file empty or missing)")

    logger.info(f"Model: {model_config.get('model_size_or_path')}, device={model_config.get('device')}")
    logger.info(f"Audio: rate={sample_rate}, chunk_ms={chunk_ms}")
    logger.info(f"Engine: re_inference={engine_config.get('re_inference_interval_ms')}ms")
    logger.info(f"Forwarding to ws://{args.ws_host}:{args.ws_port}")

    server = DistilMediumServer(
        model_config=model_config,
        engine_config=engine_config,
        hotwords=hotwords,
        hotwords_enabled=hotwords_enabled,
        ws_host=args.ws_host,
        ws_port=args.ws_port,
        vad_threshold=vad_threshold,
        vad_lead_in_ms=vad_lead_in_ms,
        agc_config=agc_config,
        sample_rate=sample_rate,
        chunk_ms=chunk_ms,
        wake_word_enabled=args.wake_word_enabled,
        wake_word_keyword=args.wake_word_keyword or "computer",
        wake_word_sensitivity=args.wake_word_sensitivity or 0.5,
        wake_word_mode=args.wake_word_mode or "idle_recovery",
        wake_word_model_dir=args.wake_word_model_dir or "data/wake_words",
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.running = False
        logger.info("Exiting")
