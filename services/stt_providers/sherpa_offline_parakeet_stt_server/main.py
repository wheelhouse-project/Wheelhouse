"""Parakeet TDT STT Provider via Sherpa-ONNX.

Uses SherpaOfflineEngine with NeMo Parakeet TDT model.
Stage B benchmark: WER 0.0057 (v3), 406ms avg latency, CPU.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from shared_stt.ws_forwarder import WSForwarder, WebSocketLogHandler
from shared_stt.audio_processor import AudioProcessor
from shared_stt.redact import redact_transcript
from shared_audio.agc import AGCConfig
from shared_audio.capture import get_audio_provider, AudioConfig

from sherpa_engine import SherpaOfflineEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ParakeetTDT")

# Regenerated at every service start from the shared hints.txt; never
# committed (wh-5w04r).
# resolve(): the engine chdirs into the model dir before from_transducer
# (external weights), so this path must never be relative. __file__ is
# already absolute on Python >= 3.9 even when main.py is run directly;
# resolve() documents and enforces the requirement (wh-q33mj.4.2).
_HOTWORDS_RUNTIME_PATH = (
    Path(__file__).parent / "runtime" / "parakeet-hotwords.txt"
).resolve()


def _import_hints_updater():
    """Import the shared hints_updater module (a loose module in the shared
    directory, not part of the wheelhouse-shared package)."""
    shared_path = Path(__file__).parent.parent / "shared"
    if str(shared_path) not in sys.path:
        sys.path.insert(0, str(shared_path))
    import hints_updater
    return hints_updater


def _vocab_charset(tokens_path: Path) -> set[str] | None:
    """Character set of the model vocab, or None when unavailable.

    tokens.txt lines are '<piece> <id>'; the piece may itself contain
    the BPE space marker but never a plain space, so rsplit on the last
    whitespace isolates it."""
    try:
        chars: set[str] = set()
        for line in tokens_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            piece = line.rsplit(None, 1)[0]
            chars.update(piece)
        return chars or None
    except Exception as e:
        logger.warning(f"Could not read model vocab for hint validation: {e}")
        return None


def prepare_hotwords_file(tokens_path: Path | None = None) -> str | None:
    """Write runtime/parakeet-hotwords.txt from the shared hints.txt.

    Plain phrases, one per line -- sherpa-onnx does the BPE segmentation
    internally when the engine passes modeling_unit='bpe' plus
    bpe_vocab=tokens.txt (wh-q3nrw spike, case A). Returns the file path,
    or None when there are no usable hints (removing any stale file so
    the engine cannot pick up hints that were deleted).

    When tokens_path is given and readable, hints containing characters
    outside the model vocab are dropped with a warning -- sherpa ignores
    OOV hotwords silently while the user still pays the beam-search
    latency (wh-q3nrw spike caveat, wh-q33mj.1.6).

    Never raises: any I/O error degrades to no-hotwords (None) instead
    of crashing startup into the launcher's fast-crash restart loop
    (wh-q33mj.1.4)."""
    try:
        hints = _import_hints_updater().get_hints()

        charset = _vocab_charset(tokens_path) if tokens_path else None
        if charset is not None:
            covered = []
            for hint in hints:
                missing = {c for c in hint if c != " " and c not in charset}
                if missing:
                    # The hint is redacted; the missing-character set stays
                    # verbatim as diagnostic metadata (it names which
                    # characters the model vocab lacks, not the hint).
                    logger.warning(
                        f"Hint '{redact_transcript(hint)}' contains "
                        f"characters outside the model vocab "
                        f"({''.join(sorted(missing))}); sherpa would "
                        "ignore it silently -- dropping from hotwords"
                    )
                else:
                    covered.append(hint)
            hints = covered

        if not hints:
            _HOTWORDS_RUNTIME_PATH.unlink(missing_ok=True)
            logger.info("No usable hints; hotwords disabled for this run")
            return None

        _HOTWORDS_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HOTWORDS_RUNTIME_PATH.write_text(
            "\n".join(hints) + "\n", encoding="utf-8"
        )
        logger.info(f"Wrote {len(hints)} hotwords to {_HOTWORDS_RUNTIME_PATH}")
        return str(_HOTWORDS_RUNTIME_PATH)
    except Exception as e:
        logger.error(
            f"Could not prepare hotwords file, continuing without hotwords: {e}"
        )
        return None


class ParakeetServer:
    """STT server using Sherpa-ONNX with NeMo Parakeet TDT."""

    DISCONNECT_TIMEOUT_S = 5.0
    DISPLAY_NAME = "Parakeet v3 ({mode})"

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
        hotwords_file: str | None = None,
        hotwords_score: float = 2.0,
        hotwords_enabled: bool = False,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        # add_hint consults this to decide whether a hard restart would
        # actually apply anything (wh-q33mj.1.1).
        self.hotwords_enabled = hotwords_enabled

        # Resolve display name with CPU/GPU mode
        use_gpu = model_config.get("use_gpu", False)
        self.display_name = self.DISPLAY_NAME.replace(
            "{mode}", "GPU" if use_gpu else "CPU"
        )

        audio_config = AudioConfig(rate=sample_rate, channels=1, chunk_ms=chunk_ms)
        self.audio_capture = get_audio_provider(config=audio_config)

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
            provider_name="sherpa_offline_parakeet",
            emits_eos=False,
        )

        self.engine = SherpaOfflineEngine(
            model_path=model_config["model_path"],
            use_gpu=use_gpu,
            gpu_device_id=model_config.get("gpu_device_id", 0),
            re_inference_interval_ms=engine_config.get("re_inference_interval_ms", 600),
            endpoint_silence_ms=engine_config.get("endpoint_silence_ms", 800),
            silence_rms_threshold=engine_config.get("silence_rms_threshold", 0.01),
            sample_rate=sample_rate,
            num_threads=model_config.get("num_threads", 4),
            hotwords_file=hotwords_file,
            hotwords_score=hotwords_score,
        )

        self.audio_processor = AudioProcessor(
            engine=self.engine,
            forwarder=self.forwarder,
            sample_rate=sample_rate,
            vad_threshold=vad_threshold,
            vad_lead_in_ms=vad_lead_in_ms,
            agc_config=agc_config,
            force_endpoint_silence_ms=engine_config.get("endpoint_silence_ms", 800),
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

    # -- Command handlers --

    def _handle_shutdown(self):
        logger.info("Shutdown command received - exiting cleanly")
        self.stop()

    def _handle_restart_service(self):
        # wh-parakeet-soft-restart-noop: load_config() proves only that
        # config.toml parses; nothing is applied to the running service
        # (engine, AGC, endpoint rules all keep construction-time
        # values). Say so instead of claiming a successful reload.
        # Hot-applying safe values is a possible future upgrade.
        logger.info("Restart service command received - validating config")
        self.forwarder.send_notification(self.display_name, "Validating configuration...")
        try:
            load_config()
            self.forwarder.send_notification(
                self.display_name,
                "Configuration valid - restart to apply changes",
            )
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
            self.forwarder.send_notification(self.display_name, f"Failed to reload: {e}")

    def _hotwords_enabled_now(self) -> bool:
        """Re-read [hotwords].enabled from config.toml so the add-hint
        gate tracks the file, not the construction-time snapshot
        (wh-q33mj.4.1: the soft-restart handler reloads config without
        applying it, so the cached flag can go stale). Falls back to the
        cached value when the config cannot be read (e.g. mid-edit)."""
        try:
            config = load_config()
            enabled = bool(config.get("hotwords", {}).get("enabled", False))
            self.hotwords_enabled = enabled
            return enabled
        except Exception as e:
            logger.warning(
                f"Config re-read failed, using cached hotwords flag: {e}"
            )
            return self.hotwords_enabled

    def _write_restart_flag(self) -> bool:
        """Write the launcher restart flag; True when it is durably on
        disk. Callers must stop ONLY after a True return. Exiting with
        code 0 and no flag reads as a clean shutdown to the launcher
        (should_restart), which would leave STT permanently dead from a
        voice command (wh-q33mj.1.3)."""
        try:
            from shared_stt.launcher import get_restart_flag_path
            flag_path = get_restart_flag_path("parakeet_tdt")
            with open(flag_path, "w") as f:
                f.write("restart")
            return True
        except Exception as e:
            logger.error(f"Failed to create restart flag, staying up: {e}")
            return False

    def _handle_hard_restart_service(self):
        logger.info("Hard restart requested - creating flag file and exiting")
        # A failed flag write converts to a deferred apply: keep
        # running, the change lands at the next successful restart.
        if not self._write_restart_flag():
            self.forwarder.send_notification(
                self.display_name,
                "Restart failed - change saved, will apply at next restart",
            )
            return
        self.forwarder.send_notification(self.display_name, "Full restart in progress...")
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
        regenerated hotwords file takes effect (wh-kcu8f; mirrors the
        distil-medium add-then-restart pattern).

        add_hint returns False for BOTH a duplicate and an I/O error (it
        catches Exception internally), so the duplicate report is only
        trusted after confirming the hint is actually present
        (wh-q33mj.1.2). The restart is skipped when hotwords are
        disabled in config -- it would reload the model and apply
        nothing (wh-q33mj.1.1)."""
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
                # written (wh-q33mj.3.1): announcing first and then
                # failing the flag write sent two contradictory voice
                # notifications back to back.
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
            # write failure (wh-q33mj.2.1).
            normalized = hint.strip()[:100].strip().lower()
            if normalized in stored:
                logger.info(f"Hint already exists: '{redact_transcript(hint)}'")
                self.forwarder.send_notification(
                    "STT Hint", f"Hint '{hint}' already exists"
                )
            else:
                logger.error(
                    f"Hint '{redact_transcript(hint)}' was not saved "
                    "(hints file write failed)"
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
            self.forwarder.send_notification(self.display_name, "Transcription service ready")
        threading.Thread(target=send_ready, daemon=True).start()

    def start(self):
        self.running = True
        self.forwarder.start()
        logger.info("WebSocket forwarder started")

        ws_log_handler = WebSocketLogHandler(self.forwarder, source=self.display_name)
        ws_log_handler.setLevel(logging.INFO)
        logger.addHandler(ws_log_handler)
        logger.propagate = False
        self._ws_log_handler = ws_log_handler
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


# The directory name the v1 installer's model archive extracts to, used as
# the last-resort model location under %LOCALAPPDATA%\WheelHouse\models.
# The primary channel is the installer-written override file; this default
# only has to match the v1 pinned archive.
DEFAULT_MODEL_DIRNAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

# Per-machine, untracked override file written by the installer (release
# plan section 5 design notes, wh-797.3.6). Sections are keyed by
# [provider].name so other providers can adopt the same file later.
OVERRIDE_FILENAME = "stt_model_overrides.toml"


def _resolve_model_path(config: dict) -> dict:
    """Resolve [model].model_path through the per-machine override file.

    Precedence (wh-797.6.8): override file > tracked config > coded default
    under %LOCALAPPDATA%\\WheelHouse\\models. The public repo ships this
    provider's config with an empty model_path; the installer writes the
    override file, and the coded default covers an install whose override
    file was lost. Dev machines keep their tracked-config value.

    Never raises: a malformed or unreadable override file logs a warning
    and the tracked value stands, so a half-edited file cannot take the
    provider down. Non-string values from either source are treated as
    absent (with a warning), and on return [model].model_path is ALWAYS
    a str -- startup code indexes it unconditionally.
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    provider_name = config.get("provider", {}).get("name", "parakeet_tdt")
    model_cfg = config.setdefault("model", {})

    raw_configured = model_cfg.get("model_path")
    if raw_configured is not None and not isinstance(raw_configured, str):
        logger.warning(
            f"Ignoring non-string model_path in provider config: "
            f"{raw_configured!r}"
        )
        raw_configured = None
    configured = (raw_configured or "").strip()

    local_app_data = os.environ.get("LOCALAPPDATA", "")

    override_value = ""
    if local_app_data:
        override_path = Path(local_app_data) / "WheelHouse" / OVERRIDE_FILENAME
        if override_path.exists():
            overrides = {}
            try:
                # utf-8-sig tolerates the UTF-8 BOM that PowerShell 5.1
                # prepends by default when it writes files.
                overrides = tomllib.loads(
                    override_path.read_bytes().decode("utf-8-sig")
                )
            except Exception as e:
                logger.warning(
                    f"Ignoring unreadable model-path override file "
                    f"{override_path}: {e}"
                )
            section = overrides.get(provider_name)
            if isinstance(section, dict):
                raw_override = section.get("model_path")
                if raw_override is not None and not isinstance(
                    raw_override, str
                ):
                    logger.warning(
                        f"Ignoring non-string model_path in override file "
                        f"section [{provider_name}]: {raw_override!r}"
                    )
                    raw_override = None
                override_value = (raw_override or "").strip()
            elif section is not None:
                logger.warning(
                    f"Ignoring override file entry '{provider_name}': "
                    f"expected a [{provider_name}] table, got "
                    f"{type(section).__name__}"
                )

    if override_value:
        model_cfg["model_path"] = override_value
        logger.info(f"Model path from override file: {override_value}")
    elif configured:
        model_cfg["model_path"] = configured
    elif local_app_data:
        default_path = str(
            Path(local_app_data) / "WheelHouse" / "models" / DEFAULT_MODEL_DIRNAME
        )
        model_cfg["model_path"] = default_path
        logger.info(f"Model path defaulted to {default_path}")
    else:
        # No override, nothing configured, no LOCALAPPDATA: still leave a
        # string in place so startup fails with a clear model-missing
        # error rather than a KeyError.
        model_cfg["model_path"] = ""
    return config


def load_config(config_path: Path | None = None) -> dict:
    """Load configuration from config.toml and resolve the model path."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    if config_path is None:
        config_path = Path(__file__).parent / "config.toml"
    config = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        logger.info(f"Loaded config from {config_path}")
    else:
        logger.warning("No config.toml found, using defaults")
    return _resolve_model_path(config)


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

    # Hotwords: opt-in because enabling forces modified_beam_search, which
    # measured +25% mean inference latency vs greedy (wh-q33mj benchmark).
    hotwords_config = config.get("hotwords", {})
    hotwords_score = float(hotwords_config.get("score", 2.0))
    hotwords_enabled = bool(hotwords_config.get("enabled", False))
    hotwords_file = None
    if hotwords_enabled:
        hotwords_file = prepare_hotwords_file(
            tokens_path=Path(model_config["model_path"]) / "tokens.txt"
        )

    logger.info(f"Model: {model_config.get('model_path')}")
    logger.info(f"GPU: {model_config.get('use_gpu', False)}")
    logger.info(f"Audio: rate={sample_rate}, chunk_ms={chunk_ms}")
    logger.info(f"Engine: re_inference={engine_config.get('re_inference_interval_ms')}ms")
    logger.info(f"Forwarding to ws://{args.ws_host}:{args.ws_port}")

    server = ParakeetServer(
        model_config=model_config,
        engine_config=engine_config,
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
        hotwords_file=hotwords_file,
        hotwords_score=hotwords_score,
        hotwords_enabled=hotwords_enabled,
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.running = False
        logger.info("Exiting")
