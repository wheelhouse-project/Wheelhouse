"""Parakeet TDT STT Provider via Sherpa-ONNX.

Uses SherpaOfflineEngine with NeMo Parakeet TDT model.
Stage B benchmark: WER 0.0057 (v3), 406ms avg latency, CPU.
"""
from __future__ import annotations

# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()


import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from shared_stt.ws_forwarder import (
    WSForwarder,
    WebSocketLogHandler,
    RateLimitedWebSocketLogHandler,
)
from shared_stt.audio_processor import (
    AudioProcessor,
    LOAD_METRICS_LOGGER_NAME,
)
from shared_stt.redact import redact_transcript
from shared_stt.startup_refusal import (
    REFUSAL_EXIT_CODE,
    send_startup_failed_notice,
    wait_for_notice_connection,
)
from shared_audio import CAPTURE_LOGGER_NAME
from shared_audio.agc import AGCConfig
from shared_audio.capture import (
    get_audio_provider,
    AudioConfig,
    CAPTURE_BACKEND_NAME,
)
from shared_audio.diagnostics import CaptureLoadReporter

from sherpa_engine import SherpaOfflineEngine, read_token_pieces

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

    The pieces come from the same reader the engine's vocabulary check
    uses, which reads tokens.txt the way sherpa does. Reading it with
    str.splitlines and str.rsplit left a character the loader keeps
    inside a token -- U+2028, U+0085, a trailing U+00A0 -- out of this
    set, so a hint holding one was dropped as outside the model
    (wh-parakeet-hotword-vocab.2.6)."""
    try:
        chars: set[str] = set()
        for piece in read_token_pieces(tokens_path):
            chars.update(piece)
        return chars or None
    except Exception as e:
        logger.warning(f"Could not read model vocab for hint validation: {e}")
        return None


def prepare_hotwords_file(tokens_path: Path | None = None) -> str | None:
    """Write runtime/parakeet-hotwords.txt from the shared hints.txt.

    Plain phrases, one per line -- sherpa-onnx does the BPE segmentation
    internally when the engine passes modeling_unit='bpe' plus a
    sentencepiece bpe_vocab (wh-q3nrw spike, case A; the vocabulary is
    bpe.vocab, not tokens.txt -- wh-parakeet-hotword-vocab). Returns the file path,
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


def applies_hints_value(status, hotwords_enabled: bool) -> bool:
    """Whether this run applies a saved hint, for the capabilities frame.

    wh-boost-engine-qualification, ruling 2 of 2026-09-21: when boosting
    was requested (a hotwords file was built), the answer is the engine's
    own status -- active, or refused for a reason such as an unusable
    bpe.vocab. When it was not requested, the answer is the [hotwords]
    enabled flag: with boosting on and no hint saved yet the engine
    records "not requested", and reading only "active" would refuse the
    first "boost", so a user could never add the first hint.

    ``status`` is engine.hotwords_status, the object _hotwords_notice
    reads; nothing here decides a second time whether boosting works.
    Every read is ``is True`` for the same reason as there: a MagicMock
    status must not be able to claim "requested".
    """
    if getattr(status, "requested", False) is True:
        return getattr(status, "active", False) is True
    return bool(hotwords_enabled)


class ParakeetServer:
    """STT server using Sherpa-ONNX with NeMo Parakeet TDT."""

    DISCONNECT_TIMEOUT_S = 5.0
    DISPLAY_NAME = "Parakeet v3 ({mode})"

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
        hotwords_file: str | None = None,
        hotwords_score: float = 2.0,
        hotwords_enabled: bool = False,
        log_load_diagnostics: bool = False,
    ):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        # add_hint consults this to decide whether a hard restart would
        # actually apply anything (wh-q33mj.1.1).
        self.hotwords_enabled = hotwords_enabled

        # Resolve display name with CPU/GPU mode
        use_gpu = model_config.get("use_gpu", False)
        self.display_name = resolve_display_name(model_config)

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
            # it: self.forwarder is built a few lines below and does not
            # exist yet, and moving that construction above the capture
            # would reorder this whole __init__.
            #
            # str(exc) rather than a second copy of the wording: the
            # message is user-visible and belongs to the factory that
            # owns the rule.
            send_startup_failed_notice(
                self.display_name,
                str(exc),
                ws_host,
                ws_port,
                provider_name="parakeet_tdt",
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

        self.transcription_enabled = threading.Event()
        self.transcription_enabled.set()

        self._disconnect_timer: threading.Timer | None = None
        self._ws_log_handler = None

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
            wake_word_activate_callback=self._handle_wake_word_activate,
            debug=True,
            provider_name="parakeet_tdt",
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
            # wh-stt-load-metrics: without this the per-utterance [load-diag]
            # line still prints, with "n/a" for every capture number.
            capture_stats=self.audio_capture.get_stats,
            # wh-audit13-preengine-load-review.1: the ten-second silence
            # line follows the same [debug] flag as the window line below.
            log_load_diagnostics=log_load_diagnostics,
        )

        # The periodic window line, which shows a struggling machine between
        # utterances and reports consumer-loop stalls. Gated, following the
        # google provider's [debug] log_overflow_diagnostics flag: one line
        # every ten seconds runs forever, unlike the per-utterance line.
        # busy_seconds is what keeps the stall field honest. This loop
        # calls the reporter at the top and then works to the end of the
        # iteration, so the gap the reporter measures holds that work as well
        # as any time the thread was not scheduled, and without this reader an
        # idle machine reported "likely whole-machine CPU starvation" for time
        # the loop spent working (wh-stt-load-metrics.1.12).
        #
        # The loop times its two branches itself rather than asking the
        # recognizer for its seconds. The recognizer is not the only thing
        # here that can pass the one-second threshold: the Silero VAD and the
        # AGC run on every chunk, a keep_warm decode runs during silence, and
        # the idle branch runs the wake-word model without touching
        # AudioProcessor at all. Timing the branches end to end is what makes
        # this complete rather than a list of sites -- work added inside
        # process_chunk later cannot escape it (wh-stt-load-metrics.1.14).
        self._load_work_s = 0.0
        self._load_reporter = (
            CaptureLoadReporter(
                capture_stats=self.audio_capture.get_stats,
                busy_seconds=lambda: self._load_work_s,
                # timeout=0 so the loop never waits on this. Both providers
                # answer immediately once setup has finished either way, and
                # a WinRT setup still in flight answers False, which is the
                # right answer for a window whose capture is not yet running
                # (wh-stt-load-metrics.1.13).
                capture_ready=lambda: self.audio_capture.wait_ready(
                    timeout=0.0),
            )
            if log_load_diagnostics else None
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
        # Whether this run applies a saved hint
        # (wh-boost-engine-qualification). WheelHouse types the word
        # "boost" as dictation when this is False.
        self.forwarder.applies_hints = applies_hints_value(
            getattr(self.engine, "hotwords_status", None),
            hotwords_enabled,
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
                if self._load_reporter is not None:
                    # Before the read, not after a chunk arrives: a starved
                    # loop is exactly the case being measured, and read()
                    # returning None is what that looks like from here.
                    for line in self._load_reporter.record_iteration():
                        logger.info(line)
                chunk = self.audio_capture.read(timeout=0.02)
                if chunk is None:
                    continue
                if not self.running:
                    break
                # Everything from here to the end of the iteration is work
                # this loop does, and the reporter subtracts it from the gap
                # it measures. The read above is deliberately outside: a
                # starved loop sits in that wait, so those seconds are the
                # stall signal itself and counting them would erase it
                # (wh-stt-load-metrics.1.14).
                _load_t0 = time.perf_counter()
                if not self.transcription_enabled.is_set():
                    if self._wake_word_detector and self._wake_word_listening:
                        result = self._wake_word_detector.process(chunk)
                        if result:
                            self.forwarder.send_wake_word_detected(result)
                            self._wake_word_listening = False
                            self.transcription_enabled.set()
                            logger.info(f"Wake word '{result}' detected - resuming transcription")
                    self._load_work_s += time.perf_counter() - _load_t0
                    continue
                self.audio_processor.process_chunk(chunk)
                self._load_work_s += time.perf_counter() - _load_t0
        except Exception as e:
            logger.error(f"Audio processing error: {e}", exc_info=True)

    def _announce_capture_outcome(self):
        """Tell Wheelhouse whether the microphone actually opened.

        wh-provider-ready-handshake, wh-parakeet-ready-notice-preflight.
        This used to sleep 3.0 seconds and then announce a working
        service with nothing in between that could know whether the
        microphone opened. A denied or missing device logged an error
        and the user was still told transcription works, while every
        spoken word was discarded. David measured the window on
        2026-09-03: Wheelhouse received the ready notice at 19:32:51.038
        while capture stayed unavailable until 19:32:58.

        wait_ready() is the capture provider's own answer, defined at
        shared_audio/capture/base.py. It is called with no argument so
        the 15 second default stays in one place: a number here could
        disagree with the provider's own.

        The deleted Kroko provider is the reference for this handshake
        (git show 15262e7d:services/stt_providers/
        sherpa_streaming_kroko_stt_server/main.py line 678). It logs
        "Audio capture started" BEFORE the handshake; that line moved
        after it here, because the line is what an operator reads in
        wheelhouse.log to decide whether the microphone worked.

        The 3.0 second sleep is gone and nothing replaces it. It was
        described as waiting for the WebSocket to connect, and it is not
        needed for that: WSForwarder.start() creates its loop and queue
        synchronously before starting its thread
        (shared_stt/ws_forwarder.py:163-164), the queue is unbounded,
        and the disconnect clear runs only after a connection that had
        already succeeded is lost (:474 and :496, both inside
        "if was_connected"). A notice queued before the first connection
        is delivered when that connection opens.

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
                    self.display_name,
                    "Transcription service ready" + self._hotwords_notice(),
                    kind="ready",
                    capture_backend=CAPTURE_BACKEND_NAME)
                return

            detail = (
                getattr(self.audio_capture, "setup_error", None)
                or "audio capture did not become ready in time"
            )
            logger.error(f"Audio capture is not ready: {detail}")
            self.forwarder.send_notification(
                self.display_name,
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

    def _hotwords_notice(self) -> str:
        """The part of the ready notice that reports hint boosting.

        Empty when boosting was never requested, so a user who set no
        hints reads the same notice as before. When it WAS requested,
        the notice says whether it started, and on failure it carries
        the engine's reason -- a rejected vocabulary otherwise leaves
        only a warning in a log file the user never opens, and the
        ready notice looks identical to a run where boosting works
        (wh-parakeet-hotword-vocab).

        Every read is `is True`, not truthiness: a test that stubs the
        engine with a MagicMock hands back a truthy mock for any
        attribute, and a mock must not be able to make this claim
        either way.
        """
        status = getattr(self.engine, "hotwords_status", None)
        if getattr(status, "requested", False) is not True:
            return ""
        if getattr(status, "active", False) is True:
            return " Word boosting is on."
        detail = getattr(status, "detail", "") or "the reason was not recorded"
        return f" Word boosting is off: {detail}."

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

        ws_log_handler = WebSocketLogHandler(self.forwarder, source=self.display_name)
        ws_log_handler.setLevel(logging.INFO)
        logger.addHandler(ws_log_handler)
        logger.propagate = False
        self._ws_log_handler = ws_log_handler
        logging.getLogger("shared_stt").propagate = False

        # That propagate = False discards every shared_stt record, including
        # the per-utterance [load-diag] line. Attach the same handler to the
        # one logger that line uses, so it reaches wheelhouse.log while every
        # other shared_stt record keeps the visibility it has today
        # (wh-stt-load-metrics).
        logging.getLogger(LOAD_METRICS_LOGGER_NAME).addHandler(ws_log_handler)

        # The capture path logs to the shared_audio tree, which nothing
        # here forwards, so the overflow warning, the device-open error,
        # and OverflowMonitor's summary reach this console and nothing
        # else -- a load investigation reading wheelhouse.log cannot
        # tell a capture drop from an inference stall
        # (wh-stt-load-metrics.2). Rate-limited, because the failure
        # that makes those records worth reading also makes them
        # frequent: a microphone dropping frames writes several a
        # second, and they share the WebSocket queue with transcripts.
        #
        # A SEPARATE handler instance from the one above, for the reason
        # the google provider records at google_stt_server/main.py:1188
        # -- _handle_set_log_level() raises the level of
        # self._ws_log_handler, so one shared object would let a
        # set_log_level("WARNING") command silently stop forwarding
        # OverflowMonitor's INFO summary. On the capture tree's own root
        # and not a broader parent, so WSForwarder's records of its own
        # sends stay out: forwarding one would produce another to
        # forward.
        capture_log_handler = RateLimitedWebSocketLogHandler(
            self.forwarder, source=self.display_name)
        capture_log_handler.setLevel(logging.INFO)
        logging.getLogger(CAPTURE_LOGGER_NAME).addHandler(capture_log_handler)
        self._capture_log_handler = capture_log_handler

        # The processor was built in __init__, when this capture provider
        # had no stream and could not report the two callback counters,
        # so its own baseline withheld them (wh-stt-load-metrics.1.9).
        # Take the baseline here rather than after the line below: the
        # callback starts filling the queue the moment the stream opens,
        # nothing discards that queue, and under CPU starvation this
        # thread can be preempted for an unbounded time before it runs
        # again -- so a baseline taken after start() can already contain
        # the loss from audio the first utterance is about to be given
        # (wh-stt-load-metrics.1.10). Here the counters are provably
        # zero: start() builds the capture stream itself.
        self.audio_processor.seed_capture_baseline_before_capture_starts()

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


# The directory name the installer builds the model in, used as the
# last-resort model location under %LOCALAPPDATA%\WheelHouse\models. The
# primary channel is the installer-written override file; this default only
# has to match the installer's pinned model. It lost its -int8 suffix on
# 2026-09-07 when the shipped model became Parakeet TDT 0.6b v3 at full
# precision; scripts/release/tests/test_installer.py holds this name and the
# installer's $ModelDirName to the same value.
DEFAULT_MODEL_DIRNAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"

# Per-machine, untracked override file written by the installer (release
# plan section 5 design notes, wh-797.3.6). Sections are keyed by
# [provider].name so other providers can adopt the same file later.
OVERRIDE_FILENAME = "stt_model_overrides.toml"


def _load_diagnostics_enabled(config: dict) -> bool:
    """Read [debug] log_load_diagnostics from the loaded config.

    A named helper rather than an inline config.get chain so the key can be
    tested against the tracked config.toml (wh-stt-load-metrics). Nothing else
    would notice a mismatch: a misspelling on either side leaves the flag
    permanently false, the periodic [load-diag] line never appears, and the
    load test silently measures nothing.

    Defaults to false. The line is one every ten seconds for the life of the
    process, so it is turned on for a load test and off again afterwards.
    """
    return bool(config.get("debug", {}).get("log_load_diagnostics", False))


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


def resolve_display_name(model_config: dict) -> str:
    """The user-visible provider name, with CPU or GPU filled in.

    ParakeetServer.__init__ is the only caller. It was extracted so
    --list-devices could name the provider in the notice it sent, and it
    stayed after that path stopped sending one: the name still belongs
    in one place, and __init__ needs it before the server exists.
    """
    use_gpu = model_config.get("use_gpu", False)
    return ParakeetServer.DISPLAY_NAME.replace(
        "{mode}", "GPU" if use_gpu else "CPU"
    )


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
    launch of parakeet_tdt happens to be live and would end a session
    the person never touched. A person who typed the flag is reading the
    console, so the console is where the refusal belongs.

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

    # Hotwords: opt-in because enabling forces modified_beam_search, which
    # measured +25% mean inference latency vs greedy (wh-q33mj benchmark).
    log_load_diagnostics = _load_diagnostics_enabled(config)

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
        log_load_diagnostics=log_load_diagnostics,
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.running = False
        logger.info("Exiting")
