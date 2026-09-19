"""Distil-Whisper Medium.en STT Provider (GPU).

Uses WhisperStreamingEngine with distil-medium.en model on CUDA.
Stage B benchmark: WER 0.0093, 192ms avg latency.
"""
import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from shared_stt.ws_forwarder import WSForwarder, WebSocketLogHandler
from shared_stt.audio_processor import AudioProcessor
from shared_stt.engine_settings import (
    validate_engine_settings,
    write_engine_settings,
)
from shared_stt.redact import redact_transcript
from shared_stt.startup_refusal import (
    REFUSAL_EXIT_CODE,
    send_startup_failed_notice,
    wait_for_notice_connection,
)
from shared_stt.whisper_engine import WhisperStreamingEngine
from shared_audio.agc import AGCConfig
from shared_audio.capture import (
    get_audio_provider,
    AudioConfig,
    CAPTURE_BACKEND_NAME,
)

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
    DISPLAY_NAME = "Distil-Whisper Medium (GPU)"

    # Whether the capture handshake reported a microphone that never
    # opened, which start() turns into a nonzero exit
    # (wh-capture-winrt-required A9). A class attribute rather than an
    # __init__ assignment so a test that builds the server with __new__
    # -- the shell this service's own test files use -- reads the
    # shipped default instead of AttributeError.
    _capture_failed = False

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
        hotwords_enabled: bool = False,
        log_load_diagnostics: bool = False,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        # Construction-time [hotwords] gate value; the add-hint handler
        # re-reads config.toml and falls back to this cache only when
        # the re-read fails (parity with parakeet, wh-q33mj.4.1).
        self.hotwords_enabled = hotwords_enabled

        audio_config = AudioConfig(rate=sample_rate, channels=1, chunk_ms=chunk_ms)
        try:
            self.audio_capture = get_audio_provider(config=audio_config)
        except RuntimeError as exc:
            # The factory refuses when winsdk is missing and there is no
            # second capture path to fall back to
            # (wh-capture-winrt-required). Starting anyway would leave a
            # provider that looks healthy and transcribes silence.
            #
            # The notice goes out over a forwarder that lives only for
            # it: self.forwarder is built below and does not exist yet,
            # and it takes a callback on self.engine, which is built
            # below this line -- so the construction cannot simply move
            # above the capture.
            #
            # str(exc) rather than a second copy of the wording: the
            # message is user-visible and belongs to the factory that
            # owns the rule.
            send_startup_failed_notice(
                self.DISPLAY_NAME,
                str(exc),
                ws_host,
                ws_port,
                provider_name="distil_medium_en",
            )
            # SystemExit, not a re-raise: the __main__ block constructs
            # this server outside its try, so a RuntimeError here would
            # reach the user as a traceback on top of the notice that
            # already said the same thing in words they can act on.
            #
            # REFUSAL_EXIT_CODE, not 1: a normal WheelHouse launch runs
            # launcher.py, whose supervisor restarts any nonzero exit
            # reached inside its fifteen-second crash window, and this
            # refusal is reached in well under a second
            # (wh-capture-winrt-required.1.5).
            sys.exit(REFUSAL_EXIT_CODE)

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

        # The apply_engine_settings handler must write the same file
        # load_config reads (wh-7ou.7.1.3).
        self._config_path = Path(__file__).parent / "config.toml"

        self.forwarder = WSForwarder(
            host=ws_host,
            port=ws_port,
            transcription_enabled_event=self.transcription_enabled,
            restart_callback=self._handle_restart_service,
            on_disconnect_callback=self._handle_wheelhouse_disconnect,
            on_reconnect_callback=self._handle_wheelhouse_reconnect,
            shutdown_callback=self._handle_shutdown,
            set_interim_results_callback=self._handle_set_interim_results,
            set_log_level_callback=self._handle_set_log_level,
            add_hint_callback=self._handle_add_hint,
            set_calibration_mode_callback=self.engine.set_calibration_mode,
            apply_engine_settings_callback=self._handle_apply_engine_settings,
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
            # wh-audit13-preengine-load-review.1: the ten-second silence
            # line is off unless [debug] log_load_diagnostics turns it on.
            log_load_diagnostics=log_load_diagnostics,
        )

        self.running = False
        # Held by the announcement thread across its read of
        # self.running and the send that follows, and by stop() and
        # cleanup() around the write, so no stop can land between that
        # read and the moment the notice is decided and handed to the
        # forwarder's loop (wh-provider-ready-handshake.1.2). The
        # hand-off is the boundary, not queue acceptance:
        # send_notification schedules the queue put with
        # asyncio.run_coroutine_threadsafe and never waits on the
        # future (shared_stt/ws_forwarder.py:848-874), so the lock does
        # not order that put at all: the forwarder's loop can run it
        # before or after this lock is released
        # (wh-provider-ready-handshake.1.3, .1.4).
        # A notice handed over in that window is delivered rather than
        # discarded: WSForwarder.stop() gives already-queued frames a
        # bounded 2.0 second chance to deliver before it stops its loop
        # (shared_stt/ws_forwarder.py:912-953).
        #
        # The wait_ready() call stays OUTSIDE this lock. cleanup() sets
        # the flag before it stops capture, and stopping capture is what
        # releases that wait, so a lock held across the wait would block
        # cleanup() on its first statement while the call that releases
        # the wait is one cleanup() has not reached.
        self._notification_lock = threading.Lock()

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
                log_load_diagnostics=log_load_diagnostics,
            )
            logger.info(
                f"Wake word detector initialized: keyword='{wake_word_keyword}', "
                f"mode='{wake_word_mode}', loaded={self._wake_word_detector.is_loaded}"
            )

        # Declared in the capabilities frame on every connect
        # (wh-audio-suppression-control C3). Set here rather than passed to
        # the constructor because the forwarder is built above, before the
        # detector exists, and a model that failed to load must not leave
        # WheelHouse telling the user to say a wake word.
        self.forwarder.wake_word_available = bool(
            self._wake_word_detector and self._wake_word_detector.is_loaded
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
        construction-time cache when the file is unreadable mid-edit.
        A missing section means OFF: the bias collapses distil-medium.en
        decoding (wh-distil-hotwords-decode-collapse), so it needs an
        explicit opt-in."""
        try:
            config = load_config()
            enabled = bool(config.get("hotwords", {}).get("enabled", False))
            self.hotwords_enabled = enabled
            return enabled
        except Exception as e:
            logger.warning(
                f"Could not re-read config for hotwords gate, using cached "
                f"value {self.hotwords_enabled}: {e}"
            )
            return self.hotwords_enabled

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
                    # Do NOT suggest turning [hotwords] on -- the bias
                    # collapses distil-medium.en decoding
                    # (wh-distil-hotwords-decode-collapse). The hint
                    # still reaches the engines that read hints.txt.
                    self.forwarder.send_notification(
                        "STT Hint Saved",
                        f"Saved '{hint}' - it will apply to STT engines "
                        "that support hints",
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

    def _handle_apply_engine_settings(self, settings: dict, apply_id=None):
        """Apply calibrated single-word rescue thresholds (wh-7ou.7.1.3).

        Contract A.3/A.4, spec Sections 5.3 and 8: validate against the
        fixed two-key allowed list, write the values into this provider's
        config.toml [engine] section preserving comments, reply
        engine_settings_result BEFORE restarting, then take the same
        flag-file-plus-exit hard-restart path the add-hint feature uses.
        Any validation or write failure replies ok=false with the exact
        error text and does NOT restart, so the calibration window can
        offer Apply again immediately. Every reply echoes the command's
        apply_id so WheelHouse can correlate it to the exact apply it
        answers (wh-7ou.7.6.9). Log lines print keys and numbers only,
        never transcript text.
        """
        clean, error = validate_engine_settings(settings)
        if error is not None:
            logger.error(f"apply_engine_settings rejected: {error}")
            self.forwarder.send_engine_settings_result(
                False, error, apply_id=apply_id,
            )
            return
        try:
            write_engine_settings(self._config_path, clean)
        except Exception as e:
            logger.error(f"apply_engine_settings write failed: {e}")
            self.forwarder.send_engine_settings_result(
                False, str(e), apply_id=apply_id,
            )
            return
        logger.info(
            "apply_engine_settings wrote %s - restarting to apply",
            {key: clean[key] for key in sorted(clean)},
        )
        # Reply only once the restart outcome is known (wh-7ou.7.6.7):
        # ok=true is a promise that a restart is coming, so it goes out
        # only after the flag is durably on disk. A failed flag write
        # means the values are saved but nothing will restart -- reply
        # ok=false with honest text so the calibration window shows the
        # failure and offers Apply again, and keep running (exiting
        # without the flag reads as a clean shutdown and leaves STT
        # permanently dead, see _write_restart_flag).
        if not self._write_restart_flag():
            self.forwarder.send_engine_settings_result(
                False,
                "Could not restart the speech engine - settings saved; "
                "they load at the next restart",
                apply_id=apply_id,
            )
            return
        self.forwarder.send_engine_settings_result(
            True, None, apply_id=apply_id,
        )
        self.stop()

    def _handle_wake_word_activate(self, reason):
        if reason is None:
            self._wake_word_listening = False
            return
        if not self._wake_word_detector or not self._wake_word_detector.is_loaded:
            return
        # Imported here, the way the detector itself is (__init__ above):
        # the module pulls in openwakeword, and a run with the wake word
        # switched off must not pay for it. Past the is_loaded check the
        # module is already imported, so this costs a sys.modules lookup.
        from shared_stt.wake_word_detector import should_listen_for_wake_word
        should_activate = should_listen_for_wake_word(
            self._wake_word_mode, reason
        )
        if should_activate:
            self._wake_word_detector.reset()
            self._wake_word_listening = True
            logger.info(f"Wake word listening activated (reason={reason})")
        else:
            self._wake_word_listening = False

    def _handle_disconnect_timeout(self):
        logger.info(f"Wheelhouse disconnected for {self.DISCONNECT_TIMEOUT_S}s - exiting cleanly")
        self.stop()

    def _handle_wheelhouse_disconnect(self):
        logger.info("Wheelhouse disconnected - pausing, will exit in 5s if no reconnect")
        self.transcription_enabled.clear()
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._disconnect_timer = threading.Timer(self.DISCONNECT_TIMEOUT_S, self._handle_disconnect_timeout)
        self._disconnect_timer.daemon = True
        self._disconnect_timer.start()

    def _handle_wheelhouse_reconnect(self):
        logger.info("Wheelhouse reconnected - resuming transcription")
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

    def _announce_capture_outcome(self):
        """Tell Wheelhouse whether the microphone actually opened.

        wh-provider-ready-handshake. This used to sleep 3.0 seconds and
        then announce a working service, with nothing in between that
        could know whether the microphone opened. A denied or missing
        device logged an error and the user was still told transcription
        works, while every spoken word was discarded.

        wait_ready() is the capture provider's own answer, defined at
        shared_audio/capture/base.py. It is called with no argument so
        the 15 second default stays in one place: a number here could
        disagree with the provider's own.

        The same change on the parakeet provider carries the fuller
        reasoning (sherpa_offline_parakeet_stt_server/main.py
        _announce_capture_outcome), including why nothing replaces the
        3.0 second sleep: WSForwarder.start() creates its loop and queue
        synchronously before starting its thread
        (shared_stt/ws_forwarder.py:163-164), the queue is unbounded,
        and the disconnect clear runs only after a connection that had
        already succeeded is lost, so a notice queued before the first
        connection is delivered when that connection opens.

        A stop that lands while the wait is still running sends no
        notice at all. audio_capture.stop() clears _capture_alive and
        joins the capture thread (shared_audio/capture/winrt_capture.py:
        260-298), that thread sets _setup_done on every exit path
        (:620), and wait_ready() then returns False with setup_error
        still None (:356) -- the same answer a dead microphone gives.
        cleanup() stops capture before it stops the forwarder, so the
        failure notice would still reach the user.

        self.running is re-read AFTER the wait, not before it, because
        the wait is the window the stop lands in: a check taken before
        it answers a question about a moment that has already passed
        when the answer arrives. Both branches are suppressed, not only
        the failure branch -- "Transcription service ready" for a
        service that is stopping is as wrong as the failure notice.

        The read and the send happen under _notification_lock, which
        stop() and cleanup() also take around their write
        (wh-provider-ready-handshake.1.2). A one-time read followed by
        an unlocked send left a window: a stop landing in it still
        reached the user, because WSForwarder.stop() gives
        already-queued frames a bounded 2.0 second chance to deliver
        before it stops its loop (ws_forwarder.py:912-953). The window
        is real work rather than a theoretical instant -- the two
        logger.info calls below go through WebSocketLogHandler to the
        forwarder.

        The wait is taken before the lock, never under it: cleanup()
        sets the flag before it stops capture, and stopping capture is
        what releases the wait, so a lock held across the wait would
        deadlock the shutdown.
        """
        ready = self.audio_capture.wait_ready()
        with self._notification_lock:
            if not self.running:
                logger.info(
                    "Server is stopping - no startup notification "
                    "will be sent")
                return

            if ready:
                logger.info("Audio capture started")
                logger.info("Sending 'ready' notification to Wheelhouse...")
                # kind="ready" is what WheelHouse routes on. It used to
                # accept any kind-less notice whose text contained
                # "ready", which also matched this provider's own
                # "Hint '<word>' already exists" notice and swallowed it
                # (wh-ready-connection-stamp.2.2.1).
                # capture_backend names the path this run actually took
                # (wh-capture-winrt-required A4). It goes on the ready
                # notice only: the failure notice below reports a
                # provider with no microphone, and naming a capture
                # backend there would state that WinRT is in use when
                # nothing is.
                self.forwarder.send_notification(
                    self.DISPLAY_NAME, "Transcription service ready",
                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
                return

            detail = (
                getattr(self.audio_capture, "setup_error", None)
                or "audio capture did not become ready in time"
            )
            logger.error(f"Audio capture is not ready: {detail}")
            self.forwarder.send_notification(
                self.DISPLAY_NAME,
                f"Failed to start - transcription will not work: {detail}",
                kind="startup_failed",
            )
            # The send only QUEUED that notice
            # (wh-capture-winrt-required.1.4). WSForwarder.stop() drains
            # the queue only when a connection is already live; with no
            # connection it sets its stop event at once and the sender
            # loop never reads the queue again. Ending the run here
            # takes cleanup() to forwarder.stop() with nothing in
            # between, so a capture failure that landed before the first
            # handshake finished discarded the one message the user
            # could act on -- even when WheelHouse accepted the
            # connection a moment later.
            #
            # The same bounded wait the constructor refusal uses, not a
            # second copy of it. Under _notification_lock, which
            # cleanup() takes before it stops the forwarder, so the
            # teardown cannot begin while the notice is still waiting.
            # That is safe here and would not be around wait_ready()
            # above: this waits on the forwarder's own thread, which
            # nothing in the shutdown has to reach first.
            wait_for_notice_connection(self.forwarder, self.forwarder.uri)
            # AFTER the send, never before (wh-capture-winrt-required
            # A9). The notice is the only thing that tells the user why,
            # and a flag written first would put the send in the state
            # the shutdown guard above suppresses, so the user would
            # read nothing at all.
            #
            # Until this, the failure branch returned with self.running
            # still True and process_audio_loop kept turning
            # `while self.running:` on a capture that will never produce
            # a chunk. The provider then ran deaf for as long as the
            # machine stayed up: a live process, a service that looks
            # healthy to WheelHouse, and every spoken word discarded.
            # A3 covers the capture that is never BUILT; this is the one
            # that builds and then fails to start -- an AudioGraph that
            # will not open, a device another process holds, a
            # permission denied after the model is already in memory.
            logger.error(
                "Audio capture never became ready - stopping the service")
            self._capture_failed = True
            self.running = False

    def _send_startup_notification(self):
        """Run the announcement on its own thread.

        The wait belongs off the command loop so a slow microphone open
        delays only the notice: process_audio_loop starts at once and
        the forwarder keeps answering Wheelhouse.
        """
        threading.Thread(
            target=self._announce_capture_outcome, daemon=True).start()

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

        self.audio_capture.start()

        # After start(), never before: wait_ready() is an answer about
        # a capture provider that has been asked to open. The
        # "Audio capture started" log line moved inside the
        # announcement, so it is written only once the handshake proves
        # the microphone works (wh-provider-ready-handshake criterion 3).
        self._send_startup_notification()

        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, stopping...")
            self.stop()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            self.process_audio_loop()
        finally:
            self.cleanup()

        # wh-capture-winrt-required A9. On the main thread, after
        # cleanup, and never from _announce_capture_outcome: that runs
        # on a daemon thread, where SystemExit kills only that thread
        # and leaves the process exactly as deaf as before.
        #
        # sys.exit rather than a returned code, for the same reason A3
        # uses it in __init__ above: the module's `if __name__ ==
        # "__main__"` block cannot be imported, so a code returned to it
        # would be handled by the one part of this file no test can
        # reach.
        #
        # REFUSAL_EXIT_CODE, not 1: the supervisor in
        # shared_stt/launcher.py restarts any nonzero exit reached
        # inside its fifteen-second crash window, and a machine whose
        # model is already in the file cache reaches this refusal well
        # inside it (wh-capture-winrt-required.1.5).
        if self._capture_failed:
            sys.exit(REFUSAL_EXIT_CODE)

    def stop(self):
        logger.info("Stopping server...")
        # Under the lock the announcement thread holds across its read
        # of this flag and its send, so the write cannot land between
        # the two (wh-provider-ready-handshake.1.2).
        with self._notification_lock:
            self.running = False

    def cleanup(self):
        # First statement, and not only in stop(): run() is a try/finally
        # around process_audio_loop, so a loop that raises reaches
        # cleanup() with stop() never called and the flag still reading
        # True. The announcement thread reads that flag.
        #
        # Under _notification_lock, and before audio_capture.stop():
        # this is what stops the write from landing between that
        # thread's read and its send, and it also means the teardown
        # below cannot begin while a notice is being decided and
        # handed to the forwarder's loop
        # (wh-provider-ready-handshake.1.2). The wait it releases is
        # taken outside the lock, so this cannot deadlock against it.
        with self._notification_lock:
            self.running = False
        logger.info("Cleaning up...")
        self.audio_capture.stop()
        self.engine.cleanup()
        self.forwarder.stop()
        logger.info("Server stopped cleanly")


def _load_diagnostics_enabled(config: dict) -> bool:
    """Read [debug] log_load_diagnostics from the loaded config.

    Same helper as the Parakeet provider's, for the same reason: a
    misspelling on either side leaves the flag permanently false and
    nothing else would notice. Defaults to false; the lines it turns on
    are one every ten seconds for the life of the process
    (wh-audit13-preengine-load-review.1).
    """
    return bool(config.get("debug", {}).get("log_load_diagnostics", False))


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


def run_list_devices() -> int:
    """Print the audio input devices, or refuse the way a start refuses.

    A module-level function rather than inline __main__ code because
    __main__ cannot be tested: this path builds the same capture the
    server builds, so it meets the same refusal when winsdk is missing
    (wh-capture-winrt-required).

    The refusal goes to stderr and sends NO startup_failed notice, which
    is the one way this path differs from a start. WheelHouse never
    launched a --list-devices run, so a notice from one is addressed to
    nobody -- and WheelHouse cannot simply ignore it either, because the
    notice names the provider, so it would be read against whatever
    launch of distil_medium_en happens to be live and would end a
    session the person never touched. A person who typed the flag is
    reading the console, so the console is where the refusal belongs.

    Returns:
        The process exit code: 0 after listing, 1 after refusing.
    """
    audio_config = AudioConfig(rate=16000, channels=1, chunk_ms=30)
    try:
        mic = get_audio_provider(config=audio_config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Available audio input devices:")
    for dev in mic.list_audio_devices():
        print(f"  [{dev['index']}] {dev['name']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command-line arguments this provider accepts.

    A module-level function rather than inline __main__ code for the same
    reason run_list_devices is one: __main__ cannot be tested, and the
    --ws-port rule below is a rule, not a declaration
    (wh-capture-winrt-required.1.2).
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-host", default="localhost")
    parser.add_argument("--ws-port", type=int, default=None)
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--wake-word-enabled", action="store_true", default=False)
    parser.add_argument("--wake-word-keyword", default=None)
    parser.add_argument("--wake-word-sensitivity", type=float, default=None)
    parser.add_argument("--wake-word-mode", default=None)
    parser.add_argument("--wake-word-model-dir", default=None)
    return parser


def parse_provider_args(argv=None):
    """Read the arguments, demanding --ws-port only for a real start.

    --list-devices lists devices and exits; it opens no forwarder, and
    the refusal it can meet goes to stderr, so the port is unused on that
    path. Requiring it there made `python main.py --list-devices` exit 2
    before printing anything, which is the console invocation the
    --list-devices help text invites and run_list_devices's own docstring
    assumes.

    The port stays required for a real start rather than defaulting to
    None: None would travel into WSForwarder (below, where the server is
    built) and fail later and less clearly than argparse's own message.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.list_devices and args.ws_port is None:
        parser.error("the following arguments are required: --ws-port")
    return args


if __name__ == "__main__":
    from shared_audio.thread_priority import elevate_current_process

    # High process class keeps STT inference scheduled under a saturated
    # CPU; the Below Normal class the Task Scheduler launch chain hands
    # down starves it (wh-process-priority-durable).
    elevate_current_process()
    config = load_config()
    model_config = config.get("model", {})
    engine_config = config.get("engine", {})
    client_config = config.get("client", {})
    agc_config_data = config.get("agc", {})

    args = parse_provider_args()

    if args.list_devices:
        sys.exit(run_list_devices())

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
    # wh-distil-hotwords-decode-collapse: default OFF -- the bias
    # collapses distil-medium.en decoding at any useful hint-list size,
    # so a missing [hotwords] section must mean disabled.
    hotwords_enabled = bool(config.get("hotwords", {}).get("enabled", False))
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
        log_load_diagnostics=_load_diagnostics_enabled(config),
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.running = False
        logger.info("Exiting")
