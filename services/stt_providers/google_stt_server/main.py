"""Google Speech-to-Text Server with Overlay Mode Processing

ARCHITECTURE:
  Audio → VAD → Google STT → Stability Filter → Overlay Messages → WheelHouse

CORE COMPONENTS:
  • StabilityProcessor: Sends full stable text and final transcripts
  • UtteranceManager: Handles speech boundaries and finalization triggers  
  • Main Loop: Orchestrates audio flow and response processing

PROCESSING FLOW:
  1. VAD detects speech → start new utterance
  2. Stream audio to Google STT API
  3. Filter responses by stability threshold (0.9)
  4. Send "stable" messages with full stable text via WebSocket
  5. Send "final" message when utterance completes
  6. Reset state for next utterance

KEY BEHAVIORS:
  • Sends full stable text (not incremental deltas)
  • WheelHouse extracts word deltas from stable messages
  • Hybrid finalization: Google final, EOS fallback, or silence timeout
  • Treats Google finals as maximally stable

CONFIGURATION:
  • stability_commit_threshold: 0.9 (confidence required)

RESULT: Sub-400ms latency with high accuracy, simplified message protocol.

Typical Usage:
  python main.py --config config.toml --device-index 1
"""

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

import logging
import os
import signal
import sys
import time
import threading
import queue
from collections import deque
from enum import Enum, auto

# Allow running this file directly by ensuring the repository root is on sys.path
try:
    from pathlib import Path
    _this_file = Path(__file__).resolve()
    for parent in [_this_file.parent, *_this_file.parents]:
        if (parent / 'pyproject.toml').exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            break
except Exception:
    pass

from google.cloud.speech_v1.types import StreamingRecognizeResponse

from config_loader import load_config
from direct_streamer import GoogleDirectStreamer
from shared_stt.redact import redact_transcript

# Add stt_providers/ to sys.path for cross-provider imports
sys.path.append(str(Path(__file__).parent.parent))
# Add services/ to sys.path for version_info
sys.path.append(str(Path(__file__).parent.parent.parent))
from version_info import get_startup_banner
import collections

# Import from shared libraries
from shared_audio.diagnostics import (
    run_mic_check, LoopStallTracker, UtteranceLoadMetrics,
    IterationSegments, CaptureLoadReporter, STALL_PREFIX)
# get_available_providers is deliberately absent from this list. It was
# on the dev side of this merge, and wh-capture-winrt-required A2 deleted
# it: the factory has one path, so there is nothing to enumerate, and
# shared/tests/test_audio_capture_factory.py asserts the name is gone.
from shared_audio.capture import (
    get_audio_provider,
    AudioConfig,
    CAPTURE_BACKEND_NAME,
)
from shared_audio.silero_vad import SileroVAD
from shared_audio.agc import SmartAGC, AGCConfig
from shared_audio.thread_priority import elevate_current_thread
from shared_stt.ws_forwarder import WSForwarder, WebSocketLogHandler
from shared_stt.startup_refusal import (
    REFUSAL_EXIT_CODE,
    send_startup_failed_notice,
    wait_for_notice_connection,
)

from usage_metrics import UsageMetrics

# Match the faster_whisper_cpu reference pattern: module-level basicConfig for
# stdout + a named logger the WebSocketLogHandler attaches to post-connect.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("GoogleSTT")


def build_streamer(cfg, transcription_enabled_event, client=None):
    """Build the Google streaming client from the loaded config.

    cfg.credentials_file carries the service-account key file configured in
    WheelHouse (wh-google-creds-file-picker); empty means Application
    Default Credentials, exactly the pre-feature behavior.

    client is the SpeechClient built once at startup; passing it stops
    every utterance from constructing a fresh client, which re-reads the
    key file from disk on the latency-critical speech-start path
    (wh-google-creds-file-picker.1.7). None keeps the pre-feature
    build-from-config behavior for abnormal recovery paths.
    """
    return GoogleDirectStreamer(
        language=cfg.language, model=cfg.model, sample_rate=cfg.rate,
        enable_auto_punct=cfg.auto_punct, debug_cfg=cfg.debug,
        single_utterance=cfg.single_utterance, phrase_hints=cfg.phrase_hints,
        phrase_hints_boost=cfg.hints_boost, class_tokens=cfg.class_tokens,
        credentials_file=cfg.credentials_file,
        client=client,
        transcription_enabled_event=transcription_enabled_event,
    )


def build_speech_client(cfg):
    """Build the SpeechClient the streamers will share. Raises when the
    credentials are bad; the caller decides how to report that. An empty
    cfg.credentials_file means Application Default Credentials, so a
    missing GOOGLE_APPLICATION_CREDENTIALS environment variable also
    fails here."""
    import importlib
    _mod = importlib.import_module('google.cloud.speech_v1')
    _SpeechClient = getattr(_mod, 'SpeechClient')
    if cfg.credentials_file:
        return _SpeechClient.from_service_account_file(cfg.credentials_file)
    return _SpeechClient()


# Bound on the client build: from_service_account_file reads the key
# file synchronously, and a file on a disconnected network drive can
# block on I/O for minutes. 30s is far above a healthy local read and
# matches the picker's validation bound in WheelHouse
# (wh-google-creds-file-picker.1.17).
_CREDENTIALS_PREFLIGHT_TIMEOUT_S = 30.0


def credentials_preflight(cfg, timeout_s=_CREDENTIALS_PREFLIGHT_TIMEOUT_S):
    """Build the Google client once at startup so a bad, missing, or
    stale key file fails before the startup notification instead of
    silently at the first utterance (wh-google-creds-file-picker.1.4).

    Returns (client, None) when the client builds, or (None, error)
    with a one-line error description when it does not.

    The build runs on a daemon worker thread with a join bound
    (wh-google-creds-file-picker.1.17): a hung key-file read (network
    drive gone) must produce a startup-failed notification, not block
    the provider's main loop forever. On timeout the worker thread is
    abandoned -- daemon threads cannot be cancelled -- which mirrors
    the picker's accepted validation-thread residual in WheelHouse.
    """
    result = {}

    def _build():
        try:
            result["client"] = build_speech_client(cfg)
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(
        target=_build, daemon=True, name="credentials-preflight"
    )
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None, (
            f"TimeoutError: reading the credentials file did not finish "
            f"within {timeout_s:.0f}s (is the key file on a disconnected "
            "network drive?)"
        )
    if "error" in result:
        return None, result["error"]
    return result.get("client"), None


def rebuild_speech_client(cfg, timeout_s=_CREDENTIALS_PREFLIGHT_TIMEOUT_S):
    """Bounded client rebuild for the recovery path
    (wh-google-creds-file-picker.1.18): after a failure dropped the
    cached client, the audio loop used to call build_speech_client
    directly, bypassing the preflight bound -- a key path that became an
    unresponsive network share then hung the loop forever, with no
    error notice and no way to process a queued soft restart. This
    wrapper reuses the bounded preflight and RAISES on failure or
    timeout so the existing streamer-failure handling applies: the
    kind="error" notice, the retry cooldown, and the VAD-gate close.
    """
    client, error = credentials_preflight(cfg, timeout_s=timeout_s)
    if client is None:
        raise RuntimeError(error)
    return client


def startup_notification(error):
    """The (title, message, kind) triple for the post-connect startup
    notification, from the preflight's error: "ready" only when the
    credentials preflight passed, otherwise what is wrong and that
    transcription will not work. kind="startup_failed" tells WheelHouse
    to treat the provider start as failed rather than suppress the
    notice as startup noise (wh-google-creds-file-picker.1.5)."""
    if error is None:
        return ("Google STT", "Transcription service ready", "ready")
    return (
        "Google STT",
        "Google credentials problem - transcription will not work: "
        + error,
        "startup_failed",
    )


def restart_completion_notification(error):
    """The (title, message, kind) triple for the soft-restart completion
    notification. The restart reloads the config, which may name a
    different (or now-missing) credentials file; saying "ready" and then
    dropping the first utterance would hide that failure, so "ready" is
    sent only when the reloaded credentials actually built a client
    (wh-google-creds-file-picker.1.12)."""
    if error is None:
        return ("STT Service", "Service restart completed. Ready.", "ready")
    return startup_notification(error)


def capture_failure_notification(detail):
    """The (title, message, kind) triple for a microphone that never
    became ready. Same title and same kind as the credentials failure,
    because WheelHouse routes on those and the two are the same event to
    the user: the provider started and will not transcribe
    (wh-provider-ready-handshake criterion 2)."""
    return (
        "Google STT",
        "Failed to start - transcription will not work: " + detail,
        "startup_failed",
    )


def capture_handshake(startup_error, mic):
    """Ask capture whether the microphone actually opened.

    Split out of startup_notification_after_capture for
    wh-provider-ready-handshake.1.2, which serializes the shutdown read
    with the send under a lock. This wait must stay OUTSIDE that lock.
    main()'s finally records the shutdown before it calls mic.stop(),
    and mic.stop() is what releases this wait, so a lock held across the
    wait would block the finally on its first statement while the call
    that would release the wait is one the finally has not reached: a
    deadlock that hangs shutdown.

    The credentials failure keeps precedence and capture is not even
    asked in that case, which is why the answer is None rather than
    False: a provider with no client will not transcribe whatever the
    microphone does, and waiting out the handshake would only delay a
    notice that will not mention the microphone.

    wait_ready() is the capture provider's own answer, defined at
    shared_audio/capture/base.py. It is called with no argument so the
    15 second default stays in one place: a number here could disagree
    with the provider's own (criterion 5).
    """
    if startup_error is not None:
        return None
    ready = mic.wait_ready()
    return ready


def startup_notification_after_capture(startup_error, ready, mic,
                                       shutting_down):
    """The startup notification triple, given capture's own answer.

    wh-provider-ready-handshake criterion 1. The preflight result alone
    says nothing about the microphone, so a device that never opened
    used to produce "Transcription service ready" while every spoken
    word was discarded.

    ready is the answer capture_handshake already collected, and is None
    when the credentials branch meant capture was never asked. It is a
    required positional argument: a default would let a future call site
    make the decision without the handshake in silence.

    The credentials failure keeps precedence: a missing keyfile is a
    real failure whether or not the server is stopping, and only the
    capture-side answer is suppressed.

    Returns None, and the caller sends nothing, when shutting_down is
    set by the time the handshake has answered. mic.stop() clears
    _capture_alive and joins the capture thread
    (shared_audio/capture/winrt_capture.py:260-298), that thread sets
    _setup_done on every exit path (:620), and wait_ready() then returns
    False with setup_error still None (:356) -- the same answer a dead
    microphone gives. main()'s finally block stops the microphone before
    it stops the forwarder, so the notice would still reach the user.

    The event is read AFTER the wait, not before it, because the wait is
    the window the stop lands in: a check taken before it answers a
    question about a moment that has already passed when the answer
    arrives. This function holds no lock of its own; the caller runs it
    and the send under one, so nothing can set the event between this
    read and that send (wh-provider-ready-handshake.1.2).
    """
    if startup_error is not None:
        return startup_notification(startup_error)
    if shutting_down.is_set():
        return None
    if ready:
        return startup_notification(None)
    detail = (
        getattr(mic, "setup_error", None)
        or "audio capture did not become ready in time"
    )
    # Google writes no "Audio capture started" success line, so there is
    # nothing to move for criterion 3; this is the half of it that
    # applies, and it is what an operator reads in wheelhouse.log to
    # tell a dead microphone from a quiet one.
    logger.error(f"[startup] Audio capture is not ready: {detail}")
    return capture_failure_notification(detail)


def begin_shutdown(shutting_down, notification_lock):
    """Record the shutdown, under the lock the announcement thread holds
    across its own read of that record and its send.

    wh-provider-ready-handshake.1.2. The read and the send used to be
    two steps, and a stop landing between them still reached the user:
    WSForwarder.stop() gives already-queued frames a bounded 2.0 second
    chance to deliver before it stops its loop
    (shared_stt/ws_forwarder.py:912-953), so the notice the read exists
    to suppress was delivered, not discarded.

    A function rather than two lines inline in main()'s finally, so a
    test can request the stop through the same path the shutdown takes.
    A bare shutting_down.set() takes no lock and would pin nothing.

    This waits only for as long as the announcement thread holds the
    lock, and that thread does no waiting under it: the handshake wait
    happens before the lock is taken, and send_notification hands the
    frame to the forwarder's loop with run_coroutine_threadsafe without
    waiting on the future (ws_forwarder.py:848-874).
    """
    with notification_lock:
        shutting_down.set()


def send_startup_notification(forwarder, startup_error, mic, shutting_down,
                              notification_lock, capture_failed):
    """Tell WheelHouse how the start went, once capture has answered.

    A module-level function rather than the closure this used to be, so
    the behaviour can be pinned by a test without running main().

    Nothing is sent when startup_notification_after_capture returns
    None, which is its answer for a shutdown that landed inside the
    handshake; that function's docstring carries the mechanism.

    capture_failed is the only route from this thread back to main()'s
    loop (wh-capture-winrt-required A9). It is set when the microphone
    never opened, and main() reads it in its loop condition and in its
    return statement. It is required and positional for the same reason
    as ready and notification_lock: a default would let a future call
    site drop the whole mechanism in silence, and the provider would go
    back to running deaf. parakeet and distil need no event -- their
    announcement is a method and writes self.running directly -- but
    main() keeps its run state in a LOCAL that no other thread can
    assign to, so the answer has to travel as an object.

    A credentials failure does NOT set it. That failure has its own
    recovery: the restart handler reloads the config, rebuilds the
    client, and sends a fresh completion notice
    (wh-google-creds-file-picker.1.12). Capture is not even asked in
    that case, which is why capture_handshake answers None rather than
    False, and `not ready` alone would read that None as a failure.

    notification_lock is held across the decision and the send, and
    begin_shutdown holds the same lock around the event's set, so no
    stop can land between the read of that event and the moment the
    notice is decided and handed to the forwarder's loop
    (wh-provider-ready-handshake.1.2). That hand-off is the boundary,
    not queue acceptance: send_notification schedules the queue put
    with asyncio.run_coroutine_threadsafe and never waits on the future
    (ws_forwarder.py:848-874), so the lock does not order that put at
    all: the forwarder's loop can run it before or after this lock is
    released (wh-provider-ready-handshake.1.3, .1.4). It is a required
    positional argument for the same reason as ready: a default would
    let a future call site skip the serialization in silence. The handshake wait
    happens before the lock is taken -- capture_handshake's docstring
    says why a lock held across it deadlocks the shutdown.

    The 3.0 second sleep that stood where the handshake now stands is
    gone and nothing replaces it. It was described as waiting for the
    WebSocket to connect. WSForwarder.start() creates its loop and queue
    synchronously on the calling thread (shared_stt/ws_forwarder.py:163-
    164) before starting its background thread, and send_notification
    returns early only when those are None (:859-860). The queue is
    unbounded, and the disconnect clear runs only after a connection
    that had already succeeded is lost -- both call sites (:474, :496)
    sit inside "if was_connected". A notice queued before the first
    connection is delivered when that connection opens.
    """
    ready = capture_handshake(startup_error, mic)
    with notification_lock:
        triple = startup_notification_after_capture(
            startup_error, ready, mic, shutting_down)
        if triple is None:
            logger.info(
                "[startup] Server is stopping - no startup notification "
                "will be sent")
            return
        title, message, kind = triple
        logger.info(
            f"[startup] Sending startup notification to Wheelhouse: "
            f"{message}")
        # capture_backend names the path this run actually took, and
        # only on the ready notice (wh-capture-winrt-required A4). The
        # other two outcomes this function sends -- a credentials
        # failure, which never asked capture at all, and a capture
        # failure, which is a provider with no microphone -- have
        # nothing true to say about which path was taken.
        forwarder.send_notification(
            title, message, kind=kind,
            capture_backend=CAPTURE_BACKEND_NAME if kind == "ready" else "")
        # AFTER the send, never before (wh-capture-winrt-required A9).
        # The notice is the only thing that tells the user why, and an
        # event set first would be read by main()'s loop condition while
        # this thread was still deciding, so the shutdown could reach
        # forwarder.stop() before the notice was handed over.
        #
        # startup_error is None is what keeps this to a CAPTURE failure:
        # ready is None on the credentials branch, and `not ready` alone
        # would read that None as a microphone that never opened.
        if startup_error is None and not ready:
            logger.error(
                "[startup] Audio capture never became ready - stopping "
                "the service")
            # The send above only QUEUED that notice
            # (wh-capture-winrt-required.1.4). WSForwarder.stop() drains
            # the queue only when a connection is already live; with no
            # connection it sets its stop event at once and the sender
            # loop never reads the queue again. Setting the event here
            # takes main()'s loop into its finally, which stops the
            # forwarder, so a capture failure that landed before the
            # first handshake finished discarded the one message the
            # user could act on -- even when WheelHouse accepted the
            # connection a moment later.
            #
            # The same bounded wait the constructor refusal uses, not a
            # second copy of it. Under notification_lock, which
            # begin_shutdown() takes in that finally before
            # forwarder.stop() runs, so the teardown cannot begin while
            # the notice is still waiting. It waits on the forwarder's
            # own thread, which nothing in the shutdown has to reach
            # first, so holding the lock across it cannot deadlock.
            wait_for_notice_connection(forwarder, forwarder.uri)
            capture_failed.set()
    logger.info("[startup] Startup notification sent")


def report_streamer_start_failure(forwarder, exc, already_notified):
    """Log a streamer construction failure and tell the user, once per
    run. An info-level log line used to be the only trace while every
    utterance was dropped (wh-google-creds-file-picker.1.4). Returns
    whether a notification has now been sent. kind="error" exempts the
    notice from WheelHouse's is_starting suppression
    (wh-google-creds-file-picker.1.5)."""
    logger.error(f"Failed to start streamer: {type(exc).__name__}: {exc!r}")
    if forwarder is None or already_notified:
        return already_notified
    forwarder.send_notification(
        "Google STT",
        "Speech engine error - transcription is not working: "
        f"{type(exc).__name__}: {exc}",
        kind="error",
    )
    return True


# How long the main loop waits after a streamer construction failure
# before trying again. Without this it would rebuild and fail the
# streamer on every audio frame, tens of times per second
# (wh-google-creds-file-picker.1.6).
_STREAMER_RETRY_COOLDOWN_S = 5.0


def streamer_retry_allowed(now, last_failure_time):
    """Whether enough time has passed since the last streamer
    construction failure to try again."""
    return (now - last_failure_time) >= _STREAMER_RETRY_COOLDOWN_S


def vad_gate_may_open(streaming, now, last_failure_time):
    """Whether detected speech may open the VAD gate right now.

    During the streamer retry cooldown there is nothing to send audio
    to. If the gate opened anyway, the lead-in buffer would be flushed
    into chunks that section 3 then throws away, and the eventual retry
    would start mid-utterance -- losing the first seconds of the
    command. Keeping the gate closed keeps the rolling lead-in buffer
    alive instead (wh-google-creds-file-picker.1.11).
    """
    return streaming is not None or streamer_retry_allowed(
        now, last_failure_time
    )


# Floor on the consecutive-silence requirement that ends a contaminated
# speech run. The requirement normally equals the lead-in buffer's
# capacity, but a configuration with vad_lead_in_ms <= chunk_ms gives
# the buffer a capacity of 0 or 1, and a 1-frame requirement would turn
# a single 30ms VAD miss back into an utterance boundary
# (wh-google-creds-file-picker.1.25). Ten frames is 300ms at the
# shipped 30ms chunk size -- the same window the shipped lead-in
# configuration produces.
_CONTAMINATED_SILENCE_MIN_FRAMES = 10


def gate_frame(raw_is_speech, agc_audio, audio_frame, streaming, now,
               last_failure_time, vad_gate_open, lead_in_buffer,
               contaminated_silence_left):
    """Decide what one audio frame contributes to the stream.

    Returns (valid_chunks, vad_gate_open, contaminated_silence_left).

    The lead-in buffer holds only vad_lead_in_ms of audio, but frames
    keep rolling through it for the whole streamer retry cooldown. If
    the gate opened at cooldown expiry purely because the timer ran
    out, the utterance would start from the last few hundred
    milliseconds of a command begun seconds earlier, and the surviving
    tail can parse as a DIFFERENT command. Speech observed while the
    gate cannot open therefore marks the current speech run as
    contaminated (wh-google-creds-file-picker.1.23). A command spoken
    entirely while the engine was down is dropped whole -- the user
    already saw the engine-failure notice -- rather than sent as a
    fragment.

    contaminated_silence_left counts the consecutive silence frames
    still required before the contaminated run counts as ended. It is
    set to the lead-in buffer's capacity on every contaminated speech
    frame, because that is exactly how many silence appends evict
    every contaminated frame from the buffer. A single false VAD frame
    mid-command (a breath, a plosive gap, one Silero miss) therefore
    cannot reopen the gate while contaminated audio is still buffered
    (wh-google-creds-file-picker.1.24). Zero means clean.
    """
    if vad_gate_open:
        # Gate open: pass audio directly through.
        return [agc_audio], True, contaminated_silence_left

    if not raw_is_speech:
        # Silence rolls through the lead-in buffer; a contaminated run
        # only ends once enough consecutive silence frames have evicted
        # every contaminated frame.
        lead_in_buffer.append(audio_frame)
        return [], False, max(0, contaminated_silence_left - 1)

    if not vad_gate_may_open(streaming, now, last_failure_time):
        # Speech during the cooldown with nothing to send to: keep the
        # gate closed and mark the run
        # (wh-google-creds-file-picker.1.11).
        lead_in_buffer.append(audio_frame)
        return [], False, max(
            lead_in_buffer.maxlen or 0, _CONTAMINATED_SILENCE_MIN_FRAMES
        )

    if contaminated_silence_left:
        # The cooldown expired mid-command; opening now would send
        # only the tail of it. This speech frame rejoins the
        # contaminated run, so the silence requirement resets.
        lead_in_buffer.append(audio_frame)
        return [], False, max(
            lead_in_buffer.maxlen or 0, _CONTAMINATED_SILENCE_MIN_FRAMES
        )

    # Fresh speech onset: open the gate, send lead-in + current frame.
    valid_chunks = list(lead_in_buffer)
    valid_chunks.append(agc_audio)
    lead_in_buffer.clear()
    return valid_chunks, True, 0


def audio_discontinuity(lead_in_buffer):
    """Reset the VAD gate across a deliberate audio gap.

    The soft-restart block and the transcription-disabled branch both
    discard audio while the microphone keeps running. A speech run can
    span that gap, and the frames after it are then only the TAIL of
    the user's command; treating the first of them as a fresh onset
    would send a suffix to Google that can parse as a DIFFERENT
    command (wh-google-creds-file-picker.1.26). Close the gate, drop
    any buffered lead-in from before the gap, and require the full
    contamination silence boundary -- the same requirement gate_frame
    uses, including the fixed floor -- before speech may reopen.

    Returns (vad_gate_open, contaminated_silence_left).
    """
    lead_in_buffer.clear()
    return False, max(
        lead_in_buffer.maxlen or 0, _CONTAMINATED_SILENCE_MIN_FRAMES
    )


# How many consecutive mic.read timeouts count as a real capture gap.
# One None read usually means the capture thread delivered a frame
# late -- the audio still arrives complete on the next read, and
# closing the gate for it would clip ordinary commands. Two or more
# (>= 100ms with nothing delivered from a 30ms-cadence capture thread)
# mean capture stopped, and the audio from that interval never entered
# the queue (wh-google-creds-file-picker.1.27).
_CAPTURE_GAP_NONE_READS = 2


def capture_lost_audio(consecutive_none_reads, drops_now, drops_last_seen):
    """Decide whether audio was lost since the last processed frame.

    Two loss signals exist on the ordinary capture path
    (wh-google-creds-file-picker.1.27): a run of consecutive None
    reads (capture stopped; see _CAPTURE_GAP_NONE_READS), and an
    advance of the backend's queue-overflow drop counter (frames were
    discarded on queue.Full -- the WinRT backend counts these in the
    'drops' field of the AudioProvider adapter's get_stats(), and the
    sounddevice backend did too until it was deleted
    (wh-portaudio-capture-removal); see read_drop_count). Either
    means the frame in hand may be the tail of a command begun during
    the gap, so the caller must apply audio_discontinuity before
    processing it.
    """
    return (
        consecutive_none_reads >= _CAPTURE_GAP_NONE_READS
        or drops_now != drops_last_seen
    )


def sample_consumer_iteration(mic, stall_tracker, utterance_load,
                              segments=None, reporter=None):
    """Close one loop iteration: sample it, and say if it stalled.

    wh-stt-load-metrics.4. Two things report the queue: the stall
    tracker's periodic [stall] line and the per-utterance [load-diag]
    line. Reading mic.get_queue_size() twice would give them two
    different depths for one iteration -- and the two would disagree
    most under exactly the load these lines exist to measure, since
    that is when the queue moves fastest between the reads.

    Returns the lines to log, in order: the stall message, and after
    it the segment breakdown of the iteration that stall was measured
    across (wh-stt-load-metrics.4 G3, which asks for the CALL the loop
    waits on, not only the fact that it waited). Empty when nothing
    stalled, which is almost every iteration.

    The order is what makes the breakdown true: this runs at the TOP
    of an iteration, so the segment accumulators still hold the
    PREVIOUS iteration's work -- the one the tracker just measured a
    gap across. Reading them before segments.start() is the whole
    point; starting first would report an iteration that has not
    happened yet.

    Both collaborators are optional so the stall tracker, which has
    watched this loop since wh-stt-audio-consumer-behind-realtime,
    depends on neither.

    reporter is the shared CaptureLoadReporter, present only while
    [debug] log_load_diagnostics is on. It reads the queue itself and
    records the same tracker, so when it is here the depth comes from
    it rather than from a second read of the queue -- the one-reading
    rule above, kept across the two ways in. It returns a list that may
    hold the stall message beside an outage notice and the periodic
    window summary, so the breakdown is placed by matching the stall
    prefix rather than by position.
    """
    if reporter is not None:
        raw_lines = reporter.record_iteration()
        depth = reporter.last_queue_depth
    else:
        depth = mic.get_queue_size()
        stall_msg = stall_tracker.record(depth)
        raw_lines = [stall_msg] if stall_msg else []
    if utterance_load is not None:
        utterance_load.sample(depth)
    lines = []
    for line in raw_lines:
        lines.append(line)
        if segments is not None and line.startswith(STALL_PREFIX):
            lines.append(segments.report())
    if segments is not None:
        segments.start()
    return lines


def read_drop_count(mic):
    """Read the backend's queue-overflow drop counter.

    mic is the AudioProvider adapter from get_audio_provider(), whose
    public statistics method is get_stats() -- get_stats_snapshot()
    existed only on the private stream inside the sounddevice adapter,
    which is deleted (wh-portaudio-capture-removal), and calling it on
    the adapter crashed the provider at startup on every backend
    (wh-google-creds-file-picker.1.28). A provider whose
    stats lack the 'drops' field degrades to no-drop-detection rather
    than killing the audio loop.
    """
    return mic.get_stats().get('drops', 0)


def recover_from_streamer_failure(utterance_mgr, vad, lead_in_buffer):
    """Reset the utterance state machine and VAD after a streamer
    construction failure, and return the new vad_gate_open value.

    start_new_utterance moves the state machine to ACTIVE before the
    streamer is built; when the build fails there is no stream, so none
    of the normal exits fire: silence finalization needs a Google
    response timestamp that never comes, the no-text timeout is gated on
    a live stream, and close_stream returns early with no stream. The
    state machine would sit in ACTIVE forever and no later utterance
    could start (wh-google-creds-file-picker.1.6).
    """
    utterance_mgr.state = UtteranceState.IDLE
    lead_in_buffer.clear()
    vad.reset()
    return False


class UtteranceState(Enum):
    """Defines the states of the utterance lifecycle."""
    IDLE = auto()      # Waiting for the first sign of a new utterance.
    ACTIVE = auto()    # Actively receiving partials for the current utterance.
    FINALIZED = auto() # A final transcript has been sent; waiting for a new utterance to start.


class StabilityProcessor:
    """Handles stable segment extraction and overlay mode transmission."""

    def __init__(self, config, forwarder=None):
        self.stability_threshold = config.latency.stability_commit_threshold
        self.forwarder = forwarder
        self.config = config
        self.last_google_response_time = None  # For silence timeout calculation

        # Track last sent stable text to avoid duplicate sends
        self._last_stable_sent = ""

        # Whether to send interim (stable) results or only final results
        self.send_interim_results = True
        
    def reset_for_new_utterance(self):
        """Reset state for a new utterance."""
        self._last_stable_sent = ""
        self.last_google_response_time = None
        
    def process_response(self, response, utterance_id, is_final=False):
        """Process Google STT response using overlay mode."""
        # Update last Google response timestamp for silence timeout
        self.last_google_response_time = time.time()
        
        if is_final:
            # Send final transcript - this IS the utterance end
            final_text = self._extract_full_text(response)
            if final_text.strip():
                # final_reason="GOOGLE_FINAL" is hard-coded here because reaching
                # the is_final=True branch is the Google-final signal itself.
                # _finalize_utterance("GOOGLE_FINAL") runs in UtteranceManager
                # AFTER this send, so the reason cannot be passed through that
                # path. WheelHouse's three-mode retraction policy (wh-x4fwo)
                # uses this reason to detect ambiguous cases.
                self.forwarder.send_final(
                    final_text.strip(),
                    utterance_id,
                    trace_id=self.forwarder._current_trace_id,
                    final_reason="GOOGLE_FINAL",
                )
            return final_text

        # Extract stable portion
        stable_text = self._extract_stable_segments(response)

        # Send if we have stable text, it's different from last send, and interim results are enabled
        if stable_text.strip() and stable_text != self._last_stable_sent:
            self._last_stable_sent = stable_text
            if self.send_interim_results:
                self.forwarder.send_stable(stable_text.strip(), utterance_id, trace_id=self.forwarder._current_trace_id)

        return stable_text
        
    def _extract_stable_segments(self, response):
        """Extract text from segments above stability threshold."""
        stable_text = ""
        for result in response.results:
            if result.alternatives and hasattr(result, 'stability'):
                transcript = result.alternatives[0].transcript
                stability = result.stability
                
                if stability >= self.stability_threshold:
                    stable_text += transcript
                else:
                    break
        return stable_text
        
    def _extract_full_text(self, response):
        """Extract complete text from response (for finals)."""
        full_parts = []
        for result in response.results:
            if result.alternatives:
                full_parts.append(result.alternatives[0].transcript)
        return "".join(full_parts)


class UtteranceManager:
    """Manages utterance state transitions and finalization triggers."""
    
    def __init__(self, config, stability_processor, forwarder=None,
                 usage_metrics=None, load_metrics=None):
        self.state = UtteranceState.IDLE
        self.current_utterance_id = 0
        self.last_speech_ts = None
        self.silence_threshold = config.silence_finalize_ms / 1000
        self.stability_processor = stability_processor
        self.forwarder = forwarder
        self.config = config
        self.closed_utterances = set()  # Track finalized utterance IDs to ignore late Google responses
        
        # Usage metrics tracking
        self.usage_metrics = usage_metrics

        # wh-stt-load-metrics.4: the per-utterance load line, or None
        # on a server built before it. Optional and last so every
        # existing construction of this class reads unchanged.
        self.load_metrics = load_metrics
        # wh-stt-load-metrics.4.1.1: which thread may touch the load
        # metrics. Four of the five finalization triggers run on the
        # consumer loop; the EOS fallback runs on a threading.Timer.
        # Neither UtteranceLoadMetrics nor LoopStallTracker's utterance
        # window locks, so a finish() from the timer thread can read
        # and zero counters the loop is writing that instant, and the
        # stall it drops appears on no line at all. This class is built
        # by the loop before the loop starts, so the identity recorded
        # here is the loop's.
        self._load_metrics_thread = threading.get_ident()
        self._pending_load_lines = deque()
        self.last_billed_seconds = 0  # From Google's total_billed_time
        self.last_final_text = ""  # Track text for metrics
        self.utterance_start_time = None  # Track when utterance started for duration fallback
        self._last_result_type = None  # For AGC feedback
        self._last_word_count = 0  # For AGC feedback
        
        # Final-driven utterance management:
        # Google sends multiple responses per utterance: interim results + final result(s)
        # Problem: Google sometimes sends duplicate final results for same utterance
        # Solution: Process only the FIRST final result per utterance, ignore all subsequent finals
        # This flag tracks whether we've already processed a final for current utterance
        self.utterance_has_final = False
        
        # EOS fallback timer tracking
        self.eos_fallback_timer = None
        
        self.stream_should_close = False  # Flag to signal stream should be closed after utterance finalization
        
        # Track if we got any useful transcription (used by check_no_text_timeout)
        self.has_received_stable_text = False
        
        # Hard timeout - abort if no text received within N seconds of utterance start
        self.max_no_text_threshold = config.max_no_text_seconds
        
    def clear_tracking_sets(self):
        """Clear tracking sets when new stream starts (prevents memory leaks)."""
        self.closed_utterances.clear()
        if self.config.debug.log_lifecycle:
            logger.info("[fsm] Cleared tracking sets for new stream")
    
    def update_speech_timestamp(self, timestamp):
        """Update the last speech timestamp."""
        self.last_speech_ts = timestamp
        
    def start_new_utterance(self):
        """Called when VAD commits to new speech."""
        self.current_utterance_id += 1
        self.state = UtteranceState.ACTIVE
        self.stability_processor.reset_for_new_utterance()

        # Reset final-driven management flag for new utterance
        self.utterance_has_final = False
        self.utterance_start_time = time.time()  # Track start for billing fallback

        # wh-stt-load-metrics.4.1.1: a line the timer thread queued has
        # still to read the window start() is about to zero, and the
        # next utterance can begin in the same iteration the EOS
        # fallback finalized in. Draining here, not only at the top of
        # the loop, is what makes the order certain.
        self.emit_pending_load_lines()

        # wh-stt-load-metrics.4: open this utterance's load window.
        # Guarded because this is a diagnostic on the consumer loop's
        # own thread: it may cost its own numbers, never the utterance.
        if self.load_metrics:
            try:
                self.load_metrics.start()
            except Exception as e:
                logger.warning(f'[load-diag] window not opened for '
                               f'UTT-{self.current_utterance_id}: '
                               f'{type(e).__name__}: {e}')

        # Cancel any pending EOS fallback timer
        if self.eos_fallback_timer:
            self.eos_fallback_timer.cancel()
            self.eos_fallback_timer = None

        # Clear tracking sets for new stream to prevent memory leaks
        self.clear_tracking_sets()

        # Generate trace_id at utterance birth
        from shared_stt.ws_forwarder import generate_trace_id
        self._current_trace_id = generate_trace_id()

        if self.config.debug.log_lifecycle:
            logger.info(f"[fsm] VAD commit. New UTT-{self.current_utterance_id}. State -> ACTIVE.")

        # Reset text tracking for new utterance
        self.has_received_stable_text = False

        # Notify WheelHouse that speech started (for instant visual feedback)
        # This triggers the GUI pulse ~150ms after speech start
        if self.forwarder:
            self.forwarder.send_vad_start(self.current_utterance_id, trace_id=self._current_trace_id)
        
    def process_google_response(self, response):
        """
        Handle response from Google STT using final-driven utterance management.
        
        ARCHITECTURE DECISION - Hybrid Final/EOS Approach:
        ========================================================
        Problem: Google STT sends EOS (End-of-Single-Utterance) events that sometimes arrive 
        WITHOUT a corresponding final transcription result. The original final-driven approach
        ignored all EOS events and waited only for final results, but this caused freezes when
        Google sent EOS without final results.
        
        Root Cause: Google's behavior is inconsistent - sometimes EOS + final, sometimes just EOS,
        sometimes just final. Relying exclusively on either signal causes issues.
        
        Solution: HYBRID approach that prefers final results but uses EOS as a fallback:
        1. Process interim results normally (for real-time feedback)
        2. On EOS event: start 500ms fallback timer, but continue waiting for final result
        3. On final result: process it immediately + finalize (cancels any pending EOS timer)
        4. If EOS timer expires without final result: finalize using EOS fallback
        
        This prevents both premature finalization (EOS before final) and freezing (EOS without final).
        """
        if self.state != UtteranceState.ACTIVE:
            return None

        # wh-2w8y: classify and log every Google response so we can see
        # which response types kept the silence_finalize timer alive during
        # apparent silence on the wire. Runs before the EOS / final / interim
        # branches below, so this fires for every response we get.
        self._log_response_kind(response)

        # HYBRID EOS HANDLING - Use EOS as fallback finalization trigger
        if response.speech_event_type == StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] EOS event received for UTT-{self.current_utterance_id} - starting EOS fallback timer")

            # Forward the EOS lifecycle event to WheelHouse BEFORE the fallback
            # timer is armed. WheelHouse's three-mode retraction policy
            # (wh-x4fwo) needs the eos signal to arrive ahead of the eventual
            # final on the wire so it can apply Mode 2 (trust stable, drop
            # disagreeing final).
            if self.forwarder:
                tid = getattr(self, '_current_trace_id', '')
                self.forwarder.send_eos(self.current_utterance_id, trace_id=tid)

            # Cancel any existing EOS timer
            if self.eos_fallback_timer:
                self.eos_fallback_timer.cancel()

            # Start a fallback timer to finalize if no final result arrives within 500ms
            def eos_fallback_finalization():
                time.sleep(0.5)  # Wait 500ms for final result
                if (self.state == UtteranceState.ACTIVE and
                    self.current_utterance_id and
                    not self.utterance_has_final):
                    if self.config.debug.log_lifecycle:
                        logger.info(f"[fsm] EOS fallback triggered for UTT-{self.current_utterance_id} - no final result received")
                    self._finalize_utterance("EOS_FALLBACK")

            # Start fallback timer in background and store reference
            self.eos_fallback_timer = threading.Timer(0.5, eos_fallback_finalization)
            self.eos_fallback_timer.start()
            return None
            
        if not response.results:
            return None
            
        # Check if this is a final result
        is_final = any(res.is_final for res in response.results)
        
        if is_final:
            # FIRST FINAL WINS - Process only the first final result per utterance
            if self.utterance_has_final:
                if self.config.debug.log_lifecycle:
                    logger.info(f"[fsm] Ignoring duplicate final result for UTT-{self.current_utterance_id} (already processed)")
                return None
                
            # Mark that we've processed a final for this utterance
            self.utterance_has_final = True
            
            # Cancel any pending EOS fallback timer since we got the final result
            if self.eos_fallback_timer:
                self.eos_fallback_timer.cancel()
                self.eos_fallback_timer = None
            
            # Extract billing info from Google's response
            if hasattr(response, 'total_billed_time') and response.total_billed_time:
                self.last_billed_seconds = response.total_billed_time.seconds
                
            # Process the final result through stability filter
            delta = self.stability_processor.process_response(response, self.current_utterance_id, is_final=True)
            self.last_final_text = delta.strip() if delta else ""
            
            # Finalize the utterance
            self._finalize_utterance("GOOGLE_FINAL")
            
            # Return any text delta from the final result
            return delta
        else:
            # Process interim (non-final) results normally for real-time feedback
            result = self.stability_processor.process_response(response, self.current_utterance_id, is_final=False)
            # Track if we've received any useful transcription (prevents VAD silence abort)
            if result and result.strip():
                self.has_received_stable_text = True
            return result

    def _log_response_kind(self, response):
        """wh-2w8y: classify and log every Google response.

        Reports kind, time since the previous response that updated the
        silence-finalize timer, and the top transcript fragment. Lets us
        verify whether low-stability or empty-results responses kept the
        silence_finalize timer alive during apparent gaps in the stable
        stream, which is the suspected cause of two-sentence merges.
        """
        now = time.time()
        last_t = self.stability_processor.last_google_response_time
        elapsed_ms = (now - last_t) * 1000 if last_t else 0.0

        if response.speech_event_type == StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE:
            logger.info(
                "[google-trace] UTT-%d: EOS (elapsed_since_last=%.0fms)",
                self.current_utterance_id, elapsed_ms,
            )
            return

        if not response.results:
            logger.info(
                "[google-trace] UTT-%d: empty_results (elapsed_since_last=%.0fms)",
                self.current_utterance_id, elapsed_ms,
            )
            return

        is_final = any(r.is_final for r in response.results)
        top = response.results[0]
        transcript = top.alternatives[0].transcript if top.alternatives else ""
        stability = getattr(top, "stability", 0.0)
        threshold = self.stability_processor.stability_threshold

        if is_final:
            logger.info(
                "[google-trace] UTT-%d: final (elapsed_since_last=%.0fms text=%r)",
                self.current_utterance_id, elapsed_ms, redact_transcript(transcript),
            )
        elif stability >= threshold:
            changed = transcript.strip() != self.stability_processor._last_stable_sent.strip()
            kind = "stable_changed" if changed else "stable_unchanged"
            logger.info(
                "[google-trace] UTT-%d: %s (elapsed_since_last=%.0fms stability=%.3f text=%r)",
                self.current_utterance_id, kind, elapsed_ms, stability,
                redact_transcript(transcript),
            )
        else:
            logger.info(
                "[google-trace] UTT-%d: interim_below_threshold (elapsed_since_last=%.0fms stability=%.3f text=%r)",
                self.current_utterance_id, elapsed_ms, stability,
                redact_transcript(transcript),
            )

    def check_silence_finalization(self, current_time):
        """Check if silence threshold exceeded timeout from last Google response."""

        if (self.state == UtteranceState.ACTIVE and 
            self.stability_processor.last_google_response_time and
            (current_time - self.stability_processor.last_google_response_time) > (self.silence_threshold)):
            
            # Finalize to clean up state (overlay mode sends final via websocket)
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] Silence timeout - finalizing UTT-{self.current_utterance_id}")
            return self._finalize_utterance("GOOGLE_SILENCE_2S")
        return None
    

    def check_no_text_timeout(self, current_time: float) -> bool:
        """Check if hard timeout exceeded - no stable text within max_no_text_seconds.
        
        This catches persistent noise where VAD keeps detecting intermittent "speech"
        but Google never returns useful transcription. Unlike VAD silence abort,
        this fires regardless of VAD state.
        
        Returns True if stream should be aborted due to timeout.
        """
        if self.state != UtteranceState.ACTIVE:
            return False
        
        # Disable if threshold is 0 or if we already got useful text
        if self.max_no_text_threshold <= 0 or self.has_received_stable_text:
            return False
        
        # Check if utterance has been active too long without any text
        if self.utterance_start_time is None:
            return False
            
        elapsed = current_time - self.utterance_start_time
        if elapsed >= self.max_no_text_threshold:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] No-text timeout - {elapsed:.1f}s with no transcription for UTT-{self.current_utterance_id}")
            return True
        
        return False
        
    def _finalize_utterance(self, reason):
        """End current utterance and reset state."""
        if self.state != UtteranceState.ACTIVE:
            if self.config.debug.log_lifecycle:
                logger.info(f"[fsm] Stale finalization trigger ignored for utt_id={self.current_utterance_id} (state={self.state.name})")
            return None
            
        # Calculate latency if we have speech timing
        latency_ms = (time.time() - self.last_speech_ts) * 1000 if self.last_speech_ts else -1
        
        if self.config.debug.log_lifecycle:
            logger.info(f"[FINAL:{reason}] UTT-{self.current_utterance_id} (eos_latency={latency_ms:.0f}ms)")
        
        # CRITICAL FIX: For non-Google-final triggers (GOOGLE_SILENCE_2S, EOS_FALLBACK,
        # NO_TEXT_TIMEOUT), we must send a final WebSocket message so WheelHouse queues
        # end_utterance. Without this, the clipboard manager times out after 60s.
        # The reason string is forwarded as final_reason so WheelHouse's three-mode
        # retraction policy (wh-x4fwo) can identify Mode 1 (treat-as-fresh) cases.
        if reason != "GOOGLE_FINAL" and self.forwarder:
            # Use the last stable text we sent as the final text
            last_text = self.stability_processor._last_stable_sent.strip()
            tid = getattr(self, '_current_trace_id', '')
            if last_text:
                self.forwarder.send_final(
                    last_text, self.current_utterance_id, trace_id=tid, final_reason=reason
                )
                logger.info(f"[ws] UTT-{self.current_utterance_id}: sent fallback final with {len(last_text)} chars ({reason})")
            else:
                # No stable text was ever sent - send empty final to trigger end_utterance anyway
                # This prevents 60s clipboard timeout even for utterances with no recognized speech
                self.forwarder.send_final(
                    "", self.current_utterance_id, trace_id=tid, final_reason=reason
                )
                logger.info(f"[ws] UTT-{self.current_utterance_id}: sent empty fallback final ({reason})")
        
        # wh-stt-load-metrics.4: what this utterance cost. Queued only
        # for an utterance that really just ended: the stale
        # finalization branch at the top returns before here, and
        # forwarder.send_final above can raise out of the WebSocket,
        # which leaves the utterance ACTIVE and reaches no line.
        # wh-stt-load-metrics.4.1.1: queued rather than written,
        # because the EOS fallback timer runs this method on its own
        # thread and the metrics have one owner thread.
        # wh-stt-load-metrics.4.1.3: queued immediately before the
        # transition below, because that transition and the
        # stream_should_close flag beside it are what let the consumer
        # loop close this utterance and open the next one. After them
        # the timer thread can be descheduled at any point, or wait
        # inside the synchronous usage CSV write; the reopen then
        # increments current_utterance_id, drains this queue and zeroes
        # the load window, so a request queued later names the wrong
        # utterance and reads the wrong window. The id goes into a
        # local for the same reason: a reopen must not be able to
        # relabel a request already queued.
        if self.load_metrics:
            finalized_utterance_id = self.current_utterance_id
            self._pending_load_lines.append(
                (finalized_utterance_id, reason))

        # Transition state and mark utterance as closed
        self.state = UtteranceState.FINALIZED
        self.closed_utterances.add(self.current_utterance_id)
        self.stream_should_close = True  # Signal that stream should be closed to prevent contamination
        if self.config.debug.log_lifecycle:
            logger.info(f"[fsm] UTT-{self.current_utterance_id}: State transition to FINALIZED on trigger: {reason}")
        
        # Log usage metrics
        if self.usage_metrics:
            # Get the final text (prefer last_final_text, fallback to last stable sent)
            final_text = self.last_final_text or self.stability_processor._last_stable_sent.strip()
            # Count words in the final text
            word_count = len(final_text.split()) if final_text else 0
            # Use Google's billed time if available, otherwise estimate from stream duration
            billed = self.last_billed_seconds
            if billed == 0 and self.utterance_start_time:
                # Round up to nearest second (Google bills in 1s increments)
                billed = int(time.time() - self.utterance_start_time) + 1
            self.usage_metrics.log_utterance(
                utterance_id=self.current_utterance_id,
                result_type=reason,
                billed_seconds=billed,
                word_count=word_count,
                text=final_text,
            )
            
            # Return the result type and word count for AGC feedback
            self._last_result_type = reason
            self._last_word_count = word_count
            
            # Reset for next utterance
            self.last_billed_seconds = 0
            self.last_final_text = ""
            self.utterance_start_time = None

        # wh-stt-load-metrics.4.1.1: on the loop's own thread the line
        # is written here and now, which keeps the other four
        # finalization triggers writing it exactly where they did. The
        # EOS fallback timer's thread leaves it for the loop's next
        # drain, so that thread never touches the metrics.
        if threading.get_ident() == self._load_metrics_thread:
            self.emit_pending_load_lines()

        return None

    def emit_pending_load_lines(self):
        """Write the load lines finalizations have queued.

        wh-stt-load-metrics.4.1.1. Called only from the consumer loop's
        thread -- the top of each iteration, and start_new_utterance
        before it zeroes the window -- so UtteranceLoadMetrics and the
        stall tracker's utterance window keep a single owner thread and
        need no lock. A lock would have to be held across finish() by
        whichever thread got there first, and under the CPU load this
        feature exists to measure that is the audio loop waiting on a
        descheduled timer thread.

        Reading the counters one iteration after an EOS finalization
        adds that iteration's samples to the utterance's tail, which is
        where they belong: the speech ended, and the loop kept
        measuring the same queue.

        crewcut: a line still queued when the loop exits is lost. The
        loop breaks on a finalization made on its own thread, which
        this method has already written by then, so only an EOS
        fallback landing in the same instant can be dropped. Draining
        once more after the loop would close it, at the cost of a call
        site inside the restart and shutdown chain that this bead's G4
        holds still.
        """
        # deque.append and deque.popleft are each atomic, and this is the
        # only method that pops, so the emptiness test cannot lose a race
        # with the timer thread's append: the worst it can do is leave a
        # line that has just arrived for the next drain. Entries exist
        # only when load_metrics does -- _finalize_utterance appends
        # inside that same guard.
        while self._pending_load_lines and self.load_metrics:
            utterance_id, reason = self._pending_load_lines.popleft()
            try:
                logger.info(self.load_metrics.finish(utterance_id, reason))
            except Exception as e:
                logger.warning(f'[load-diag] no line for '
                               f'UTT-{utterance_id}: '
                               f'{type(e).__name__}: {e}')


def handle_set_log_level(level: str):
    """Handle set_log_level command from WheelHouse via WebSocket.

    Adjusts both the logger's own level and every attached handler's level so
    DEBUG records actually propagate through the WebSocketLogHandler when
    WheelHouse asks to see them. Handlers default to INFO at install time
    (see forwarder-setup block), so bumping only the logger would leave DEBUG
    records stranded.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper())
    if numeric_level is not None:
        logger.setLevel(numeric_level)
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        logger.info(f"[ws] Log forwarding level set to {level}")


def main(argv=None):
    """
    The main entry point for the Google STT server with hybrid final/EOS processing.
    
    :flow: STT Transcription
    :step: 2
    :description: Orchestrates the complete speech capture pipeline. Uses *Voice Activity Detection*
    to trigger utterance start, manages *GoogleDirectStreamer* lifecycle, and forwards Google
    responses to the *StabilityProcessor* for overlay mode transmission. Implements **hybrid
    final/EOS utterance management** that prefers Google final results but arms a 500 ms EOS
    fallback to guarantee clean shutdown when finals never arrive. Integrates silence finalization
    and logs audio input overflow reported by the capture backend, naming the place that backend
    loses frames. No restart follows an overflow and no notification is sent for one: the callback
    below writes the line and returns (wh-stt-overflow-config-and-wording, criterion 4).
    :data_in: Raw audio frames from `WinRTAudioCapture`, Google streaming responses (partials, finals,
    EOS events, stability scores), overflow telemetry.
    :data_out: Stable/final messages via `WSForwarder`, and optional restart/health notifications.
    """
    args, cfg = load_config()
    
    # Restart control - use threading.Event for thread-safe signaling across threads
    restart_requested_event = threading.Event()
    restart_count = 0
    max_restarts = 3
    
    # Control variables (declared early so handlers can use nonlocal)
    stop = False
    streaming = None
    
    # WebSocket forwarder (initialized later)
    forwarder = None
    
    def on_overflow_detected():
        """Log overflow events without triggering restart.

        The phrase comes from the backend rather than from this line. It
        used to say PortAudio whichever backend was running, and WinRT --
        the shipped default -- has no PortAudio in its capture path at
        all; it drops a chunk when the queue it hands to the forwarder is
        full. `mic` is assigned further down in this same function, before
        capture starts, so it is bound by the time this callback can run.
        """
        logger.info(
            f"[overflow] audio input overflow at {mic.OVERFLOW_SOURCE}; "
            f"frames may be dropped"
        )

    def handle_add_hint(hint: str):
        """Handle add_hint command from WheelHouse via WebSocket.

        This callback is invoked when the user speaks "x-ray boost" with text selected.
        It adds the hint to the shared hints.txt file, sends a notification, and
        triggers a service restart so the new hint takes effect immediately.

        Args:
            hint: The phrase to add to the STT hints list
        """
        try:
            import sys
            # Add shared module to path for import
            shared_path = Path(__file__).parent.parent / "shared"
            if str(shared_path) not in sys.path:
                sys.path.insert(0, str(shared_path))
            from hints_updater import add_hint

            logger.info(f"[config] Processing add_hint request: '{redact_transcript(hint)}'")
            success = add_hint(hint)
            
            if success:
                # Trigger restart to load new hint immediately (thread-safe)
                restart_requested_event.set()
                logger.info(f"[config] Hint added successfully, triggering service restart")
                
                if forwarder:
                    forwarder.send_notification(
                        "STT Hint Added",
                        f"Added '{hint}' - restarting to apply changes"
                    )
            elif not success and forwarder:
                logger.info(f"[config] Hint already exists, sending notification")
                forwarder.send_notification(
                    "STT Hint",
                    f"Hint '{hint}' already exists"
                )
            elif not forwarder:
                logger.info("[config] Warning: forwarder is None, cannot send notification")
        except Exception as e:
            logger.info(f"[config] Error adding hint: {e}")
            if forwarder:
                forwarder.send_notification(
                    "STT Error",
                    f"Failed to add hint: {e}"
                )

    def handle_restart_service():
        """Handle restart_service command from WheelHouse via WebSocket.
        
        This callback is invoked when WheelHouse sends the restart_service command.
        The "Restart Transcription Service" menu item that once sent a full restart
        was removed on 2026-09-04 (wh-remove-restart-credentials-items). It triggers a
        graceful restart of the STT process.
        """
        restart_requested_event.set()
        logger.info("[restart] Restart requested via WebSocket command")
        if forwarder:
            forwarder.send_notification(
                "STT Service",
                "Restarting transcription service..."
            )

    def handle_shutdown():
        """Handle shutdown command from WheelHouse via WebSocket.

        This triggers a clean shutdown (exit code 0) that the launcher will NOT
        restart. Used when WheelHouse wants to stop the STT provider permanently
        (e.g., when switching to a different provider or shutting down).
        """
        nonlocal stop
        logger.info("[shutdown] Shutdown command received - exiting cleanly")
        # Signal main loop to exit (will exit with code 0)
        stop = True

    def handle_set_interim_results(enabled: bool):
        """Handle set_interim_results command from WheelHouse via WebSocket.

        Toggles whether to send interim (stable) results or only final results.

        Args:
            enabled: If True, send stable (partial) results during speech.
                    If False, only send final results at end of utterance.
        """
        nonlocal stability_processor
        if stability_processor:
            stability_processor.send_interim_results = enabled
            logger.info(f"[config] Interim results {'enabled' if enabled else 'disabled'}")

    # Wake word detection
    wake_word_detector = None
    wake_word_listening = False
    wake_word_mode = getattr(cfg, 'wake_word_mode', "idle_recovery")

    if getattr(cfg, 'wake_word_enabled', False):
        from shared_stt.wake_word_detector import WakeWordDetector
        wake_word_detector = WakeWordDetector(
            keyword=getattr(cfg, 'wake_word_keyword', 'computer'),
            model_dir=getattr(cfg, 'wake_word_model_dir', 'data/wake_words'),
            sensitivity=getattr(cfg, 'wake_word_sensitivity', 0.5),
            diagnostic_logger=logger,
            log_load_diagnostics=cfg.debug.log_load_diagnostics,
        )
        logger.info(f"[wake_word] Detector initialized: keyword='{cfg.wake_word_keyword}', "
             f"mode='{wake_word_mode}', loaded={wake_word_detector.is_loaded}")

    def handle_wake_word_activate(reason):
        """Activate/deactivate wake word listening based on transcription status changes.

        Called by WSForwarder when WheelHouse sends set_transcription_status:
        - reason=None: transcription re-enabled -> stop listening for wake word
        - reason="idle": transcription disabled due to idle timeout -> start listening
        - reason="audio"/"sonos": transcription disabled for other reasons

        The wake_word_mode decides which reasons arm the detector, and the
        rule itself lives in one shared function so all three providers
        apply the same one (shared_stt.wake_word_detector.
        WAKE_WORD_ARMED_REASONS).
        """
        nonlocal wake_word_listening
        if reason is None:
            wake_word_listening = False
            return
        if not wake_word_detector or not wake_word_detector.is_loaded:
            return
        # Imported here, the way the detector itself is above: the module
        # pulls in openwakeword, and a run with the wake word switched off
        # must not pay for it. Past the is_loaded check the module is
        # already imported, so this costs a sys.modules lookup.
        from shared_stt.wake_word_detector import should_listen_for_wake_word
        should_activate = should_listen_for_wake_word(wake_word_mode, reason)
        if should_activate:
            wake_word_detector.reset()
            wake_word_listening = True
            logger.info(f"[wake_word] Listening activated (reason={reason})")
        else:
            wake_word_listening = False

    # Initialize audio capture. The factory returns the WinRT capture or
    # raises; there is no other backend to select (wh-capture-winrt-required).
    audio_config = AudioConfig(
        rate=cfg.rate,
        channels=1,
        chunk_ms=cfg.chunk_ms,
        device_index=cfg.device_index
    )
    
    # Overflow detection: log warnings but do NOT restart the mic.
    # Restarting the mic causes a 2s+ blackout that often makes things worse.
    try:
        mic = get_audio_provider(config=audio_config, overflow_callback=on_overflow_detected)
    except RuntimeError as exc:
        # The factory refuses when winsdk is missing and there is no second
        # capture path to fall back to (wh-capture-winrt-required). Starting
        # anyway would leave a provider that looks healthy and transcribes
        # silence, so the only answer is to say why and quit.
        #
        # The notice goes out over a forwarder that lives only for it: the
        # forwarder this function builds does not exist yet at this point
        # (it is constructed about seventy lines below, after the dozen
        # callbacks it takes), and moving that construction up to serve an
        # exit path would reorder the whole startup.
        #
        # str(exc) rather than a second copy of the wording: the message is
        # user-visible and belongs to the factory that owns the rule.
        #
        # A --list-devices run gets the refusal on stderr and NO notice.
        # WheelHouse never launched that run, so a notice from it is
        # addressed to nobody -- and it cannot be ignored either, because
        # the notice names the provider, so WheelHouse would read it
        # against whatever launch of google_stt happens to be live and
        # would end a session the person never touched. The flag is
        # handled about twenty lines below, past the capture both paths
        # need, so the branch has to be made here.
        #
        # cfg.forward_ws is the second run that gets stderr and no notice.
        # Every notice this provider sent before this change rode the
        # forwarder built under "if cfg.forward_ws:" below, so all of them
        # inherited that gate without saying so. This one does not ride it
        # -- send_startup_failed_notice opens a forwarder of its own -- so
        # the gate has to be read here or a run with forwarding turned off
        # still reaches out to WheelHouse and can end a live session
        # (wh-capture-winrt-required.1.1).
        if args.list_devices or not cfg.forward_ws:
            print(str(exc), file=sys.stderr)
            return 1
        send_startup_failed_notice(
            "Google STT",
            str(exc),
            cfg.ws_host,
            cfg.ws_port,
            provider_name="google_stt",
            emits_eos=True,
        )
        # REFUSAL_EXIT_CODE, not 1: a normal WheelHouse launch runs
        # launcher.py, whose supervisor restarts any nonzero exit
        # reached inside its fifteen-second crash window, and this
        # refusal is reached in well under a second
        # (wh-capture-winrt-required.1.5). The --list-devices and
        # forwarding-off branch above keeps 1: no supervisor watches a
        # run a person started by hand, and 1 is what a console user
        # expects from a command that printed an error.
        return REFUSAL_EXIT_CODE
    logger.info("[overflow] Overflow logging enabled (auto-restart disabled)")
    
    if args.list_devices:
        for d in mic.list_audio_devices():
            print(f"  [{d['index']}] {d['name']} - rate={d['rate']} channels={d['channels']}")
        return 0

    if cfg.mic_check_seconds > 0:
        return run_mic_check(
            mic,
            duration_seconds=cfg.mic_check_seconds,
            rate=cfg.rate,
            chunk_ms=cfg.chunk_ms,
            write_wav_path=cfg.mic_check_write if cfg.mic_check_write else None,
            device_index=cfg.device_index
        )

    try:
        from direct_streamer import GoogleDirectStreamer
    except ImportError:
        logger.info("[ERROR] Failed to import Google STT components.")
        return 1

    # Initialize core components
    # Initialize Silero VAD (only supported backend)
    logger.info(f"[vad] Using Silero VAD (threshold={cfg.silero_threshold})")
    vad = SileroVAD(threshold=cfg.silero_threshold, sample_rate=cfg.rate)
    
    transcription_enabled_event = threading.Event()
    transcription_enabled_event.set()

    # Disconnect timeout: exit cleanly after 5s without WheelHouse connection
    DISCONNECT_TIMEOUT_S = 5.0
    disconnect_timer: threading.Timer | None = None

    def handle_disconnect_timeout():
        """Called when disconnect timeout expires - exit cleanly."""
        nonlocal stop
        logger.info(f"[ws] Wheelhouse disconnected for {DISCONNECT_TIMEOUT_S}s - exiting cleanly")
        stop = True

    def handle_wheelhouse_disconnect():
        """Handle WheelHouse disconnect - pause transcription and start exit timer."""
        nonlocal disconnect_timer
        logger.info("[ws] Wheelhouse disconnected - pausing transcription, will exit in 5s if no reconnect")
        transcription_enabled_event.clear()

        # Start disconnect timeout timer
        if disconnect_timer:
            disconnect_timer.cancel()
        disconnect_timer = threading.Timer(DISCONNECT_TIMEOUT_S, handle_disconnect_timeout)
        disconnect_timer.daemon = True
        disconnect_timer.start()

    def handle_wheelhouse_reconnect():
        """Handle WheelHouse reconnect - cancel exit timer and resume transcription."""
        nonlocal disconnect_timer
        logger.info("[ws] Wheelhouse reconnected - resuming transcription")

        # Cancel disconnect timeout timer
        if disconnect_timer:
            disconnect_timer.cancel()
            disconnect_timer = None

        # Re-enable transcription (the server will send set_transcription_status if needed)
        transcription_enabled_event.set()

    # Setup WebSocket forwarder
    if cfg.forward_ws:
        try:
            forwarder = WSForwarder(
                host=cfg.ws_host,
                port=cfg.ws_port,
                transcription_enabled_event=transcription_enabled_event,
                add_hint_callback=handle_add_hint,
                restart_callback=handle_restart_service,
                on_disconnect_callback=handle_wheelhouse_disconnect,
                on_reconnect_callback=handle_wheelhouse_reconnect,
                shutdown_callback=handle_shutdown,
                set_interim_results_callback=handle_set_interim_results,
                set_log_level_callback=handle_set_log_level,
                wake_word_activate_callback=handle_wake_word_activate,
                debug=cfg.debug.log_lifecycle,
                provider_name="google_stt",
                emits_eos=True,
            )
            # Declared in the capabilities frame on every connect
            # (wh-audio-suppression-control C3). Set before start(), which
            # is what spawns the thread that sends the frame. An attribute
            # rather than a constructor argument because the parakeet and
            # distil servers build their forwarder before their detector
            # exists; this provider builds the detector first and assigns
            # the same way for one shape across the three.
            forwarder.wake_word_available = bool(
                wake_word_detector and wake_word_detector.is_loaded
            )
            # Google applies a saved hint always: phrase adaptation has no
            # [hotwords] gate (config_loader.py). WheelHouse accepts the
            # "boost" command only when the engine says it applies hints
            # (wh-boost-engine-qualification).
            forwarder.applies_hints = True
            forwarder.start()
            # Enable log forwarding to WheelHouse via the standard Python logging
            # pipeline (wh-6wp). A record written while WheelHouse is unreachable
            # is DROPPED, not buffered: WebSocketLogHandler counts it per logger
            # and reports the total on the first record sent after the connection
            # returns (wh-forwarded-log-time-order criterion 4). The
            # "[ws] forwarding to ..." line below MAY be one of those counted
            # records, and which way it goes is not fixed: forwarder.start()
            # only spawns the sender thread, so whether that thread has
            # connected by the time the line is written depends on the
            # scheduler. Before the gate moved down, the plain handler had no
            # reachability test at all and the line was queued either way.
            ws_log_handler = WebSocketLogHandler(forwarder, source="Google STT")
            ws_log_handler.setLevel(logging.INFO)
            logger.addHandler(ws_log_handler)
            logger.propagate = False
            # Forward the audio overflow monitor's rate-limited INFO summary to
            # WheelHouse so an ongoing audio overflow is visible in
            # wheelhouse.log, not only on the provider console. Use a SEPARATE
            # handler instance fixed at INFO, not the GoogleSTT handler above:
            # handle_set_log_level() raises/lowers the level of every handler on
            # the GoogleSTT logger, so sharing one object would let a
            # set_log_level("WARNING") command silently stop forwarding the
            # overflow summary. Attach to the specific overflow logger, not a
            # broad parent, to avoid catching shared_stt.ws_forwarder's own
            # "sent log" records and creating a feedback loop (see
            # distil_medium_en for the same pattern).
            overflow_log_handler = WebSocketLogHandler(forwarder, source="Google STT")
            overflow_log_handler.setLevel(logging.INFO)
            logging.getLogger("shared_audio.overflow_monitor").addHandler(overflow_log_handler)
            logger.info(f"[ws] forwarding to ws://{cfg.ws_host}:{cfg.ws_port}")
        except Exception as e:
            logger.warning(f"[ws] Could not start WebSocket forwarder: {e}")

    # Build the Google client once, up front: the preflight catches a
    # bad key before the startup notification
    # (wh-google-creds-file-picker.1.4), and sharing one client keeps
    # the key-file read off the per-utterance speech-start path
    # (wh-google-creds-file-picker.1.7).
    speech_client, startup_error = credentials_preflight(cfg)
    if startup_error is not None:
        logger.error(f"[startup] Credentials preflight failed: {startup_error}")

    # wh-stt-load-metrics.4 G3: which call the loop was waiting on when
    # it stalled. Always on -- a stall that only happens under load is
    # not one anybody can reproduce with a debug flag turned on
    # afterwards, and the cost is five clock reads an iteration. Built
    # above the stall tracker because the tracker's busy figure is this
    # timer's running work total.
    segments = IterationSegments()

    # Always-on consumer stall detection: when this loop goes unscheduled
    # for seconds (whole-machine CPU saturation) the capture queue fills
    # and frames drop with no other direct log signature
    # (wh-stt-audio-consumer-behind-realtime). Built here, above the
    # utterance manager, because the per-utterance load line reads this
    # tracker's utterance window (wh-stt-load-metrics.4).
    #
    # wh-stt-load-metrics.4 G1a: with [debug] log_load_diagnostics on,
    # the same CaptureLoadReporter the Parakeet loop runs. Its periodic
    # "[load-diag] window=" line is what the load-test tool waits for
    # before it starts a run, so without it a Google run could not be
    # measured by that tool at all. It brings its own stall tracker,
    # and the loop uses that one rather than building a second: two
    # trackers would apply one threshold twice and report the same
    # stall in two places with different numbers.
    #
    # busy_seconds is the loop's own measured work, so the [stall]
    # figure is the gap MINUS that work rather than plain wall time.
    # It must be the sum of the timed spans, not the iteration's wall
    # time less the capture wait: wall time carries the unaccounted
    # figure, unaccounted IS the descheduling, and subtracting it would
    # quiet the [stall] line exactly when the machine is worst.
    #
    # timeout=0.0 on capture_ready so the loop never waits on it; a
    # capture still opening answers False, which is the right answer
    # for a window with no frames yet (wh-stt-load-metrics.1.13).
    load_reporter = (
        CaptureLoadReporter(
            capture_stats=mic.get_stats,
            busy_seconds=lambda: segments.work_seconds,
            capture_ready=lambda: mic.wait_ready(timeout=0.0),
        )
        if cfg.debug.log_load_diagnostics else None
    )
    stall_tracker = (
        load_reporter.stall_tracker if load_reporter else LoopStallTracker())

    # wh-stt-load-metrics.4: what one utterance cost. mic.get_stats is
    # the AudioProvider adapter's public statistics method -- the same
    # one read_drop_count uses, and not get_stats_snapshot, which
    # existed only on the private stream inside the sounddevice adapter
    # before that adapter was deleted (wh-portaudio-capture-removal).
    utterance_load = UtteranceLoadMetrics(
        capture_stats=mic.get_stats, stall_tracker=stall_tracker)

    # Initialize stability-based processing and usage metrics
    usage_metrics = UsageMetrics()
    stability_processor = StabilityProcessor(cfg, forwarder)
    utterance_mgr = UtteranceManager(cfg, stability_processor, forwarder,
                                     usage_metrics,
                                     load_metrics=utterance_load)

    # Initialize Smart AGC (uses config from config_loader)
    agc_config = AGCConfig(
        enabled=cfg.agc.enabled,
        target_speech_rms=cfg.agc.target_speech_rms,
        vad_threshold_rms=cfg.agc.vad_threshold_rms,
        noise_floor_alpha=cfg.agc.noise_floor_alpha,
        min_gain=cfg.agc.min_gain,
        max_gain=cfg.agc.max_gain,
        initial_noise_floor=cfg.agc.initial_noise_floor,
    )
    agc = SmartAGC(agc_config)
    agc_log_interval = 30.0  # Log AGC diagnostics every 30 seconds
    last_agc_log = time.time()

    # Overflow diagnostics
    overflow_diag_interval = 30.0
    last_overflow_diag_log = time.time()
    vad_times_ms = []
    agc_times_ms = []

    if cfg.agc.enabled:
        logger.info(f"[agc] Smart AGC enabled: target_rms={cfg.agc.target_speech_rms}, max_gain={cfg.agc.max_gain}")
    else:
        logger.info("[agc] Smart AGC disabled")

    def _stop(*_):
        nonlocal stop
        stop = True

    # Setup signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: 
            signal.signal(sig, _stop)
        except Exception: 
            pass
    
    # Helper function to close stream cleanly
    def close_stream(reason: str):
        nonlocal streaming, vad_gate_open
        if not streaming: 
            return
        logger.info(f"[metrics] Total stream active time: {streaming.elapsed:.2f}s ({reason})")
        streaming.finish()
        streaming = None
        # Reset VAD gate and buffer state
        vad_gate_open = False
        lead_in_buffer.clear()
        vad.reset()
        utterance_mgr.state = UtteranceState.IDLE
        if cfg.debug.log_lifecycle:
            logger.info("[fsm] Stream closed. Resetting VAD and FSM state to IDLE.")
        return None

    # Initialize audio processing
    mic.start()

    # Keep the per-frame consumer loop scheduled under machine-wide CPU load;
    # it needs only a few percent of one core but must get it on time
    # (wh-stt-audio-consumer-behind-realtime).
    consumer_elevated = elevate_current_thread('highest')
    logger.info(f"[priority] Consumer thread priority elevated: {consumer_elevated}")


    # --- Lead-in Buffer ---
    # Keeps rolling audio so first syllables aren't cut off when speech starts
    lead_in_frames = int(cfg.vad_lead_in_ms / cfg.chunk_ms)
    lead_in_buffer = collections.deque(maxlen=lead_in_frames)
    vad_gate_open = False  # True when streaming audio to Google
    last_is_speech = False  # Track last VAD result for silence timeouts
    mic_none_count = 0  # Track consecutive mic.read() -> None for stall detection
    # Last-seen queue-overflow drop count from the capture backend; an
    # advance means frames were discarded, so the next frame is not
    # contiguous with the previous one
    # (wh-google-creds-file-picker.1.27).
    mic_drops_seen = read_drop_count(mic)
    # Nonzero while the current speech run began during the streamer
    # retry cooldown: the count of consecutive silence frames still
    # required before the run counts as ended. Keeps the gate closed
    # past expiry so a fragment of the command is never sent
    # (wh-google-creds-file-picker.1.23, .1.24).
    contaminated_silence_left = 0
    # True once a streamer-construction failure has been reported to the
    # user this run; the notice fires once, not per utterance.
    streamer_failure_notified = False
    # When the last streamer construction failure happened; gates the
    # retry cooldown (wh-google-creds-file-picker.1.6). 0.0 means no
    # failure yet, so the first attempt is always allowed.
    last_streamer_failure_time = 0.0
    # --------------------------

    # Display startup banner with version info
    logger.info(f"[startup] {get_startup_banner('Google STT Server')} - Ready")

    # Send the startup notification to WheelHouse.
    # Note: WheelHouse sends "Loading..." notification when starting provider,
    # so we only need to send the outcome here.
    # "ready" only when the startup credentials preflight passed
    # (wh-google-creds-file-picker.1.4) AND capture answered the
    # readiness handshake (wh-provider-ready-handshake criterion 1); the
    # kind lets WheelHouse route a failure instead of suppressing it
    # (wh-google-creds-file-picker.1.5).
    # The wait keeps its own thread, so a slow microphone open delays
    # only the notice and the frame loop below starts at once
    # (criterion 5). mic.start() ran above, which is what makes
    # wait_ready() a question capture can answer.
    # An intentional shutdown that lands inside that wait releases
    # wait_ready() as False, which is the same answer a dead microphone
    # gives, and mic.stop() runs before forwarder.stop() in the finally
    # below -- so without this event the user reads a startup failure
    # for a stop they asked for.
    shutting_down = threading.Event()
    # Serializes that event's set with the announcement thread's read of
    # it and the send that follows, so a stop cannot land between the
    # two (wh-provider-ready-handshake.1.2). Created whether or not
    # there is a forwarder, because the finally records the shutdown
    # through it either way.
    notification_lock = threading.Lock()
    # The announcement thread's only route back to the loop below
    # (wh-capture-winrt-required A9). `stop` is a local of this
    # function, so a daemon thread cannot assign to it; parakeet and
    # distil write self.running instead, which is the same answer
    # through an object they both already have.
    #
    # Without this the loop kept turning on a capture that will never
    # produce a chunk: the notice went out, the process stayed up, and
    # the provider ran deaf for as long as the machine did.
    capture_failed = threading.Event()
    if forwarder:
        threading.Thread(
            target=send_startup_notification,
            args=(forwarder, startup_error, mic, shutting_down,
                  notification_lock, capture_failed),
            daemon=True,
        ).start()

    if cfg.debug.log_lifecycle:
        logger.info(f"[vad-debug] lead_in_ms={cfg.vad_lead_in_ms}")

    # Main processing loop
    try:
        # capture_failed ends the loop the way `stop` does
        # (wh-capture-winrt-required A9): the microphone never opened,
        # so every further iteration is work done on audio that will
        # never arrive. The read below has a 0.05 second timeout, so
        # this condition is reached within one of those.
        while not stop and not capture_failed.is_set():
            # wh-stt-load-metrics.4.1.4: the sample first, the drain
            # second. sample_consumer_iteration measures the iteration
            # that has just ended, and through that iteration the
            # utterance a queued line reports was still the active one,
            # so its samples belong to that utterance's tail -- which is
            # what emit_pending_load_lines' own docstring says. Draining
            # first would read and clear the utterance stall window
            # before this iteration's stall had been recorded into it;
            # the next start() would then discard that stall, and the
            # stall that coincides with the end of an utterance would
            # reach no per-utterance line at all.
            for stall_line in sample_consumer_iteration(
                    mic, stall_tracker, utterance_load, segments,
                    reporter=load_reporter):
                logger.info(stall_line)

            # wh-stt-load-metrics.4.1.1: the EOS fallback timer runs on
            # its own thread and only queues its line. It is written
            # here, on the thread that samples the same counters, so the
            # two cannot overlap.
            utterance_mgr.emit_pending_load_lines()

            # Check if restart was requested (thread-safe check)
            if restart_requested_event.is_set():
                restart_count += 1
                if restart_count > max_restarts:
                    logger.info(f"[restart] Maximum restart attempts ({max_restarts}) exceeded - stopping service")
                    # Send notification about failure
                    if forwarder and cfg.forward_ws:
                        forwarder.send_notification(
                            "Wheelhouse: STT Service Error",
                            f"Audio overflow persists after {max_restarts} restart attempts. Service stopped."
                        )
                    stop = True
                    break
                    
                logger.info(f"[restart] Performing restart #{restart_count}")
                
                # Send notification about restart
                if forwarder and cfg.forward_ws:
                    forwarder.send_notification(
                        "Wheelhouse: STT Restarting",
                        f"Restarting service (attempt {restart_count}/{max_restarts})..."
                    )
                
                # Close current stream cleanly
                if streaming:
                    logger.info("[restart] Closing current stream")
                    streaming.finish()
                    streaming = None
                
                # Stop and restart microphone
                logger.info("[restart] Restarting microphone stream")
                mic.stop()
                time.sleep(2.0)  # Wait for resources to clear
                mic.start()
                mic.reset_overflow_monitor()
                
                # Reload config from disk (soft restart hot-reload)
                _, new_cfg = load_config()
                old_hints_count = len(cfg.phrase_hints)
                cfg = new_cfg  # Update the config reference

                # The reloaded config may name a different credentials
                # file; rebuild the client from it now so the completion
                # notice below can report a credentials problem instead
                # of announcing ready and dropping the first utterance
                # (wh-google-creds-file-picker.1.12).
                speech_client, restart_cred_error = credentials_preflight(cfg)
                
                # Update StabilityProcessor with new config
                stability_processor.stability_threshold = cfg.latency.stability_commit_threshold
                stability_processor.config = cfg
                
                # Update UtteranceManager thresholds
                utterance_mgr.silence_threshold = cfg.silence_finalize_ms / 1000
                utterance_mgr.max_no_text_threshold = cfg.max_no_text_seconds
                utterance_mgr.config = cfg
                
                # Reset lead-in buffer with new settings
                lead_in_frames = int(cfg.vad_lead_in_ms / cfg.chunk_ms)
                lead_in_buffer.clear()
                # Recreate buffer with new maxlen if lead_in_ms changed
                lead_in_buffer = collections.deque(maxlen=lead_in_frames)
                # The restart discarded seconds of audio; a command
                # still in progress must not resume as a fresh onset
                # (wh-google-creds-file-picker.1.26).
                vad_gate_open, contaminated_silence_left = (
                    audio_discontinuity(lead_in_buffer)
                )
                
                logger.info(f"[restart] Config reloaded: hints={len(cfg.phrase_hints)} (was {old_hints_count})")
                
                # Reset state
                utterance_mgr.state = UtteranceState.IDLE
                restart_requested_event.clear()
                
                logger.info("[restart] Restart completed - resuming normal operation")

                # Reset restart counter after successful restart
                # This allows future hint additions to work without hitting the max limit
                restart_count = 0

                # Send completion notification -- "ready" only if the
                # reloaded credentials actually built a client
                # (wh-google-creds-file-picker.1.12).
                if forwarder and cfg.forward_ws:
                    title, message, kind = restart_completion_notification(
                        restart_cred_error
                    )
                    # The same rule as the startup notice above: this is
                    # the OTHER notification that can carry kind="ready",
                    # and a ready that named no capture path would leave
                    # WheelHouse's log line missing after every
                    # credentials reload (wh-capture-winrt-required A4).
                    # The capture is untouched by that reload, so the
                    # name is as true here as at startup.
                    forwarder.send_notification(
                        title, message, kind=kind,
                        capture_backend=(
                            CAPTURE_BACKEND_NAME if kind == "ready" else ""))

                # The restart block pauses this loop for seconds on purpose;
                # don't count that pause as a scheduling stall.
                stall_tracker.reset()
                continue
            
            # Skip all audio processing if transcription is disabled (audio suppression active)
            # This prevents VAD triggering, utterance starts, and API billing during suppression
            if not transcription_enabled_event.is_set():
                # Every suppressed frame is a discarded piece of audio;
                # speech spanning the suppression window must not
                # resume as a fresh onset when transcription re-enables
                # (wh-google-creds-file-picker.1.26).
                vad_gate_open, contaminated_silence_left = (
                    audio_discontinuity(lead_in_buffer)
                )
                audio_frame = mic.read(timeout=0.05)
                if audio_frame and wake_word_detector and wake_word_listening:
                    result = wake_word_detector.process(audio_frame)
                    if result:
                        forwarder.send_wake_word_detected(result)
                        wake_word_listening = False
                        transcription_enabled_event.set()
                        vad.reset()
                        logger.info(f"[wake_word] '{result}' detected - resuming transcription")
                else:
                    time.sleep(0.02)  # Small delay to prevent busy-wait
                continue
            
            current_time = time.time()
            
            # 1. Process Google STT responses
            # Timed as one segment (wh-stt-load-metrics.4 G3) because
            # the time can land in any of its parts: the response poll,
            # the forwarding done inside process_google_response, or the
            # 0.2s sleep that follows a None response. That sleep runs
            # only after '[fsm] Streamer signaled an error or session
            # end.', so that line sits inside the stall when the sleep
            # was the cause (boss note, 2026-09-05). Bracketed rather
            # than wrapped in a with-block: every exit from the loop
            # below is a break, so control always reaches the add.
            responses_started = time.perf_counter()
            if streaming:
                while True:
                    try:
                        response = streaming.get_response_non_blocking()
                        if response is None:
                            logger.info("[fsm] Streamer signaled an error or session end.")
                            time.sleep(0.2)
                            break
                        
                        # Process response through utterance manager
                        result = utterance_mgr.process_google_response(response)
                        
                        # Feed AGC outcome for successful GOOGLE_FINAL
                        if hasattr(utterance_mgr, '_last_result_type') and utterance_mgr._last_result_type == "GOOGLE_FINAL":
                            agc.on_stt_outcome("GOOGLE_FINAL", utterance_mgr._last_word_count)
                            utterance_mgr._last_result_type = None  # Clear to avoid duplicate calls
                        
                        if result == "STOP":
                            stop = True
                            break

                            
                    except queue.Empty:
                        break

            segments.add('responses',
                         (time.perf_counter() - responses_started) * 1000)

            # 2. Read audio and process via AGC + Deflector
            with segments.timing('mic_read'):
                audio_frame = mic.read(timeout=0.05)
            if audio_frame is None:
                mic_none_count += 1
                if mic_none_count == 40:
                    logger.info("[mic] WARNING: no audio frames for ~2s -- microphone may be stalled")
                elif mic_none_count > 0 and mic_none_count % 600 == 0:
                    logger.info(f"[mic] WARNING: still no audio frames ({mic_none_count * 0.05:.0f}s stalled)")
                continue
            mic_drops_now = read_drop_count(mic)
            if capture_lost_audio(mic_none_count, mic_drops_now,
                                  mic_drops_seen):
                # Audio was lost since the last processed frame; the
                # frame in hand may be only the tail of a command begun
                # during the gap (wh-google-creds-file-picker.1.27).
                vad_gate_open, contaminated_silence_left = (
                    audio_discontinuity(lead_in_buffer)
                )
            mic_drops_seen = mic_drops_now
            if mic_none_count >= 40:
                logger.info(f"[mic] Audio resumed after {mic_none_count * 0.05:.1f}s stall ({mic_none_count} None reads)")
            mic_none_count = 0
            
            # Get raw VAD result BEFORE AGC (for AGC's is_speech parameter)
            # Unconditional since wh-stt-load-metrics.4: the segment
            # breakdown needs these two on every iteration, and three
            # perf_counter reads cost less than the branch they
            # replace. The [overflow-diag] averages below stay gated.
            t0 = time.perf_counter()
            raw_is_speech = vad.is_speech(audio_frame)
            t1 = time.perf_counter()

            # wh-2w8y: log every VAD transition while the gate is open. This
            # captures whether Silero saw silence inside a single utterance
            # whose stable transcript Google later revised end-to-end.
            # INFO level so it forwards to wheelhouse.log over the existing
            # WebSocketLogHandler.
            if vad_gate_open and raw_is_speech != last_is_speech:
                logger.info(
                    "[vad-trace] UTT-%d: %s (silero_conf=%.3f)",
                    utterance_mgr.current_utterance_id,
                    "speech_start" if raw_is_speech else "silence_start",
                    vad.get_confidence(),
                )

            last_is_speech = raw_is_speech  # Track for silence timeouts

            # Apply Smart AGC - normalizes audio and adapts to noise floor
            agc_audio = agc.process(audio_frame, raw_is_speech)
            t2 = time.perf_counter()
            segments.add('vad', (t1 - t0) * 1000)
            segments.add('agc', (t2 - t1) * 1000)
            if cfg.debug.log_overflow_diagnostics:
                vad_times_ms.append((t1 - t0) * 1000)
                agc_times_ms.append((t2 - t1) * 1000)
            
            # --- Lead-in Buffer / VAD gate logic (gate_frame) ---
            # During the streamer retry cooldown the gate stays closed
            # even on speech (wh-google-creds-file-picker.1.11), and a
            # speech run that began during the cooldown keeps it closed
            # past expiry so a mid-command reopen cannot send a
            # fragment (wh-google-creds-file-picker.1.23, .1.24).
            valid_chunks, vad_gate_open, contaminated_silence_left = gate_frame(
                raw_is_speech,
                agc_audio,
                audio_frame,
                streaming,
                current_time,
                last_streamer_failure_time,
                vad_gate_open,
                lead_in_buffer,
                contaminated_silence_left,
            )
            # --------------------------
            
            # Periodic AGC diagnostics logging
            if cfg.agc.enabled and (current_time - last_agc_log) > agc_log_interval:
                diag = agc.diagnostics
                logger.debug(f"[agc] gain={diag['effective_gain']:.2f}x, noise_floor={diag['noise_floor']:.4f}, failures={diag['consecutive_failures']}")
                last_agc_log = current_time

                # VAD stall check (piggyback on AGC interval)
                vad_warning = vad.check_stall()
                if vad_warning:
                    logger.info(vad_warning)

            # Periodic overflow diagnostics logging
            if cfg.debug.log_overflow_diagnostics and (current_time - last_overflow_diag_log) > overflow_diag_interval:
                mic_q = mic.get_queue_size()
                streamer_diag = streaming.diagnostics if streaming else {}

                vad_avg = sum(vad_times_ms) / len(vad_times_ms) if vad_times_ms else 0
                vad_max = max(vad_times_ms) if vad_times_ms else 0
                agc_avg = sum(agc_times_ms) / len(agc_times_ms) if agc_times_ms else 0
                agc_max = max(agc_times_ms) if agc_times_ms else 0

                # wh-stt-load-metrics.4 G1a: the stall summary that used
                # to sit here is now the load reporter's periodic window
                # line, under [debug] log_load_diagnostics. Two summaries
                # of one tracker cannot coexist: each reads the window by
                # emptying it, so whichever ran second would report a
                # window the first had already taken.
                logger.info(f"[overflow-diag] queues: mic={mic_q}, google_audio={streamer_diag.get('audio_q_size', 0)}, google_resp={streamer_diag.get('response_q_size', 0)}")
                logger.info(f"[overflow-diag] timing: vad={vad_avg:.1f}ms(max={vad_max:.1f}), agc={agc_avg:.2f}ms(max={agc_max:.2f})")

                vad_times_ms.clear()
                agc_times_ms.clear()
                last_overflow_diag_log = current_time

            # Update timestamps based on VAD (for silence timeouts)
            if last_is_speech:
                utterance_mgr.update_speech_timestamp(current_time)

            # 3. Handle Valid Speech (Start/Continue Stream)
            if valid_chunks:
                # If valid_chunks is not empty, it means we have Confirmed Speech
                
                # Start new stream if needed. The cooldown check runs
                # before start_new_utterance so a persistent failure
                # does not spin the state machine (and vad_start
                # notifications) on every audio frame
                # (wh-google-creds-file-picker.1.6).
                if streaming is None and streamer_retry_allowed(
                    current_time, last_streamer_failure_time
                ):
                    if utterance_mgr.state in (UtteranceState.IDLE, UtteranceState.FINALIZED):
                        utterance_mgr.start_new_utterance()

                        try:
                            # Rebuild the shared client only after a
                            # failure dropped it; the normal path reuses
                            # the one built at startup
                            # (wh-google-creds-file-picker.1.7). The
                            # rebuild is bounded so a hung key-file read
                            # raises into this handler instead of
                            # blocking the audio loop forever
                            # (wh-google-creds-file-picker.1.18).
                            if speech_client is None:
                                speech_client = rebuild_speech_client(cfg)
                            streaming = build_streamer(
                                cfg, transcription_enabled_event,
                                client=speech_client,
                            )
                            streaming.start()
                        except Exception as e:
                            streamer_failure_notified = report_streamer_start_failure(
                                forwarder, e, streamer_failure_notified
                            )
                            streaming = None
                            # The client may be the stale part (key file
                            # deleted or replaced); drop it so the next
                            # attempt rebuilds from config.
                            speech_client = None
                            last_streamer_failure_time = current_time
                            vad_gate_open = recover_from_streamer_failure(
                                utterance_mgr, vad, lead_in_buffer
                            )

                # Send audio to Google
                if streaming and utterance_mgr.state == UtteranceState.ACTIVE:
                    with segments.timing('send'):
                        for chunk in valid_chunks:
                            streaming.send_audio(chunk)


            # 4.5. Check for hard no-text timeout (catches persistent intermittent noise)
            if streaming and utterance_mgr.check_no_text_timeout(current_time):
                utterance_mgr._finalize_utterance("NO_TEXT_TIMEOUT")
                # Feed outcome to AGC (false positive - sent to Google but no text)
                agc.on_stt_outcome("NO_TEXT_TIMEOUT", utterance_mgr._last_word_count)

            # 5. Check silence-based finalization
            result = utterance_mgr.check_silence_finalization(current_time)
            if result == "STOP":
                stop = True
                break


            # 5.5. Check if stream should be closed after utterance finalization
            if utterance_mgr.stream_should_close and streaming:
                close_stream("prevent_contamination")
                utterance_mgr.stream_should_close = False
                continue

            # 6. Stream lifecycle management
            if streaming and streaming.elapsed >= cfg.max_stream_seconds:
                result = close_stream("hit max duration")
                if result == "STOP":
                    stop = True
                    break

    finally:
        # Before mic.stop(): stopping capture is what releases the
        # announcement thread's wait_ready(), so the event has to be set
        # first or that thread reads it too late. begin_shutdown takes
        # the notification lock, so the set cannot land between that
        # thread's read of the event and its send
        # (wh-provider-ready-handshake.1.2).
        begin_shutdown(shutting_down, notification_lock)
        if streaming:
            logger.info(f"[metrics] Total stream active time: {streaming.elapsed:.2f}s (program exit)")
            streaming.finish()
        mic.stop()
        if forwarder:
            forwarder.stop()

    # wh-capture-winrt-required A9. `sys.exit(main())` is the last line
    # of this module, so this IS the process exit code: a provider that
    # could not open its microphone must not look like a clean stop, or
    # WheelHouse sees a healthy exit for a service that never worked.
    # After the finally, so the teardown above runs either way.
    #
    # REFUSAL_EXIT_CODE, not 1: the supervisor in
    # shared_stt/launcher.py restarts any nonzero exit reached inside
    # its fifteen-second crash window, and a machine whose client builds
    # quickly reaches this refusal well inside it
    # (wh-capture-winrt-required.1.5).
    if capture_failed.is_set():
        return REFUSAL_EXIT_CODE
    return 0


if __name__ == "__main__":
    from shared_audio.thread_priority import elevate_current_process

    # High process class keeps STT streaming scheduled under a saturated
    # CPU; the Below Normal class the Task Scheduler launch chain hands
    # down starves it (wh-process-priority-durable).
    elevate_current_process()
    sys.exit(main())