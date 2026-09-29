"""WinRT-based audio capture for STT services.

This module provides audio capture using Windows Runtime (WinRT) AudioGraph,
replacing the PortAudio/sounddevice dependency with native Windows APIs.

GLOSSARY
--------
- **AudioGraph** - WinRT audio processing graph connecting inputs to outputs
- **AudioDeviceInputNode** - Microphone capture node in the graph
- **AudioFrameOutputNode** - Node providing raw PCM frame access
- **MediaCategory.COMMUNICATIONS** - Capture category that asks Windows for
  its acoustic echo canceller, so sound the machine plays does not reach the
  recogniser. See _setup_graph for the reason and the caveat
  (wh-screen-reader-audio-suppression-conflict).

OVERVIEW
--------
WinRT AudioGraph provides native Windows audio capture with:

1. **Lower latency** - Direct Windows audio stack integration
2. **Better async** - Native async operations compatible with asyncio
3. **No external deps** - No PortAudio/compiled library needed
4. **Auto-format** - Handles sample rate conversion in hardware

KEY INSIGHTS
------------
1. **Graph lifecycle** - Graph must be created, started, and closed properly.
   Use as context manager or call close() explicitly.

2. **Encoding properties** - AudioEncodingProperties set on AudioGraphSettings
   affect DEVICE format, not internal processing. The AudioGraph always uses
   float32 internally regardless of encoding_properties settings.

3. **Frame format** - AudioFrameOutputNode.get_frame() returns FLOAT32 audio
   samples in range [-1.0, 1.0], NOT the int16 format you might expect.
   CRITICAL: You must convert float32 to int16 before sending to STT services:
       int16_sample = max(-32768, min(32767, int(float32_sample * 32767)))

4. **Buffer extraction** - IMemoryBufferReference requires buffer protocol
   access via memoryview(ref), NOT bytes(ref). The winsdk package implements
   __buffer__ which calls IMemoryBufferByteAccess::GetBuffer internally.

5. **Thread safety** - Graph runs in WinRT thread, frame data safe to read
   from Python thread. Queue provides producer-consumer decoupling.

6. **Setup is asynchronous** - start() only spawns the capture thread; the
   AudioGraph, the microphone node and the imports all happen inside that
   thread, and _capture_loop catches every failure there. start() therefore
   cannot report a denied microphone. Callers must follow start() with
   wait_ready(), which returns True only once the graph is actually running
   and exposes the failure text through setup_error
   (streaming-provider review .1.22).
"""

import logging
import queue
import threading
import time
from typing import Optional, Callable

import numpy as np

from .base import AudioConfig, AudioStats
from ..overflow_monitor import (
    OverflowMonitor,
    OverflowConfig,
    CAPTURE_QUEUE_SOURCE,
)
from ..thread_priority import (
    elevate_current_thread,
    register_current_thread_mmcss,
    revert_current_thread_mmcss,
)

logger = logging.getLogger(__name__)


def _is_winrt_available() -> bool:
    """Check if WinRT audio APIs are available."""
    try:
        from winsdk.windows.media.audio import AudioGraph  # noqa: F401
        return True
    except ImportError:
        return False


WINRT_AUDIO_AVAILABLE = _is_winrt_available()

# Consecutive frame polls that must raise before the graph counts as dead.
# More than one because a single exception is a hiccup and tearing capture
# down for it would report unavailable windows for a healthy provider. The
# error path sleeps 0.1s per failure, so three is about 0.3s -- short against
# the load reporter's 10s windows, and long enough that one bad frame does
# not mark a window (wh-stt-load-metrics.2.1.1).
POLL_FAILURES_BEFORE_DEAD = 3

# How long a running graph may go without delivering one audio sample before
# the microphone counts as lost and the graph is rebuilt
# (wh-mic-loss-capture-recovery). A quiet microphone still delivers samples:
# on 2026-09-24 the frame counter moved in every load-diag window for the 19
# minutes between the last utterance and the loss, with max_frame_gap_ms=62.
# 3.0 s is above the worst capture gap ever measured under load, 2.4 s on
# 2026-09-03 on the retired sounddevice path. Boss ruling 20:56 2026-09-24.
CAPTURE_STALL_SECONDS = 3.0

# The wait between two failed rebuild attempts while the microphone is gone.
# Fixed, with no attempt limit: a microphone can stay unplugged for hours and
# the provider must pick it up whenever it comes back. A failed attempt costs
# one AudioGraph.create_async call. Boss ruling 20:56 2026-09-24.
REBUILD_RETRY_SECONDS = 2.0

# The shortest time between two "still unavailable" reminder lines during one
# outage, so an absent microphone cannot fill the log. Boss ruling 20:56
# 2026-09-24.
STILL_UNAVAILABLE_LOG_SECONDS = 60.0

# The stall clock. Bound once at import rather than read as time.monotonic
# at each call, because tests that drive _poll_frames inline replace this
# module's `time` with a stub that has only sleep(); a lookup through it
# would raise inside the poll and be counted as a failed poll.
_monotonic = time.monotonic

# AudioGraphUnrecoverableError, written out for the same reason as
# AUDIO_GRAPH_DEVICE_NOT_AVAILABLE below: this module must load without
# winsdk (winsdk/windows/media/audio/__init__.pyi lists these four).
_UNRECOVERABLE_ERROR_NAMES = {
    0: 'NONE',
    1: 'AUDIO_DEVICE_LOST',
    2: 'AUDIO_SESSION_DISCONNECTED',
    3: 'UNKNOWN_FAILURE',
}


class _GraphLoss:
    """What one graph's UnrecoverableErrorOccurred handler tells the poll loop.

    WinRT calls the handler on its own thread, so the handler only records
    the reason and sets an event; the capture thread does the rebuild.
    Microsoft documents the event for the graph's device, and whether it
    fires when only the CAPTURE device goes is not proven. The sample stall
    in _poll_frames is the signal the 2026-09-24 log proves; this one makes
    the rebuild immediate when it does fire (wh-mic-loss-capture-recovery).
    """

    def __init__(self):
        self.event = threading.Event()
        self.reason: Optional[str] = None
        self.token = None

    def handler(self, sender, args) -> None:
        # Never raise into WinRT: an exception here would be reported on a
        # thread nobody watches, and the event must still be set.
        try:
            code = int(args.error)
            name = _UNRECOVERABLE_ERROR_NAMES.get(code, f'error {code}')
        except Exception:
            name = 'an unknown error'
        self.reason = f'AudioGraph reported {name}'
        self.event.set()


class _Outage:
    """One stretch of time in which the microphone delivers no samples.

    It begins with the loss line and ends with the recovery line, and every
    rebuild in between -- one that failed, or one that built a graph that
    then delivered nothing -- is one more attempt in the same outage. Boss
    ruling C1, 20:56 2026-09-24: recovered means samples arrived, not that a
    graph was built, so a device that opens but never delivers cannot write
    a loss and a recovery line every few seconds.
    """

    def __init__(self, reason: str):
        self.started = _monotonic()
        self.last_logged = self.started
        self.attempts = 0
        self.last_error = reason
        self.device: Optional[str] = None
        self.over = False
        logger.warning(
            f'Microphone lost ({reason}); rebuilding the audio capture')

    def remind(self) -> None:
        """Log that the microphone is still missing, at most once a period."""
        now = _monotonic()
        if now - self.last_logged < STILL_UNAVAILABLE_LOG_SECONDS:
            return
        self.last_logged = now
        logger.warning(
            f'Microphone still unavailable after {self.attempts} '
            f'{"attempt" if self.attempts == 1 else "attempts"} '
            f'({self.last_error}); retrying every '
            f'{REBUILD_RETRY_SECONDS:g} s')

    def recovered(self) -> None:
        """Log the recovery once, when the first samples arrive."""
        self.over = True
        elapsed = _monotonic() - self.started
        # Ruling C4: the rebuild opens Windows's CURRENT default microphone,
        # which may not be the one that was lost, so the line names it.
        # Device names only, never audio.
        device = f' on {self.device!r}' if self.device else ''
        logger.info(
            f'Microphone recovered after {elapsed:.1f} s and '
            f'{self.attempts} '
            f'{"attempt" if self.attempts == 1 else "attempts"}; '
            f'audio capture rebuilt{device}')


def _device_name(mic_node) -> Optional[str]:
    """The opened microphone's name, when the input node can say cheaply."""
    try:
        name = mic_node.device.name
    except Exception:
        return None
    return str(name) if name else None

# AudioGraphCreationStatus.DEVICE_NOT_AVAILABLE. The winsdk enum is
# SUCCESS 0, DEVICE_NOT_AVAILABLE 1, FORMAT_NOT_SUPPORTED 2,
# UNKNOWN_FAILURE 3 (winsdk/windows/media/audio/__init__.pyi). The number
# is written here rather than imported, because this module must load in a
# venv with no winsdk at all -- WINRT_AUDIO_AVAILABLE below is how the
# factory finds that out, and an import at module scope would raise before
# it could.
AUDIO_GRAPH_DEVICE_NOT_AVAILABLE = 1

# What a user reads when Windows has no audio output device available.
# David approved this exact wording on 2026-09-06, so treat a change to it
# as a user-visible change: the provider carries setup_error into its
# startup-failed notice, and that notice is the tray message
# (wh-capture-winrt-required A10).
#
# WHY AN OUTPUT DEVICE STOPS A RECORDING. AudioGraph is built from an
# AudioRenderCategory and needs a render device even when the only node
# that will ever be connected is a microphone. David met this on
# 2026-09-06: his only output device was a TV that was switched off, and
# graph creation returned status=1 2.8 seconds into the run. Switching the
# TV on made the same run reach capture.
#
# WHY IT IS SEPARATE FROM WINRT_REQUIRED_MESSAGE. That one is for a venv
# with no winsdk, and its fix is to re-run the installer. Reaching this
# line proves winsdk imported, so sending someone to re-install a package
# they already have is a wrong instruction, not merely a vague one.
#
# WHY THE DEVELOPER SENTENCE IS NOT IN HERE. It is logged instead, at
# ERROR, immediately before the raise. A notice carrying it as well would
# be 319 characters, and the Windows notification structure plyer builds
# holds at most 256: its szInfo field is WCHAR * 256
# (plyer/platforms/win/libs/win_api_defs.py:62). ctypes refuses the longer
# string with "ValueError: string too long (319, maximum length 256)", and
# plyer raises it on its own thread
# (plyer/platforms/win/notification.py:17), so the user would have seen NO
# notice at all -- the exact state this bead exists to remove. The
# provider's notice adds a 47-character prefix, so the text here may not
# exceed 209 characters. Boss e7 ruled on 2026-09-06: David's three user
# sentences stay word for word and the developer sentence moves to the
# log.
AUDIO_DEVICE_MISSING_MESSAGE = (
    "The speech service cannot start. Windows has no audio output device "
    "available, and Windows needs one even to record. Connect or turn on "
    "your speakers, headphones, or TV, then start WheelHouse again."
)

# The developer half of the same refusal, logged rather than shown. It
# names the enum member so a log reader does not have to look up what 1
# means (wh-capture-winrt-required A10).
AUDIO_DEVICE_MISSING_LOG_LINE = (
    "AudioGraph creation returned DEVICE_NOT_AVAILABLE (status=1)"
)

# _cleanup_graph's default for `graph`, distinct from None. A capture thread
# whose setup raised owns nothing and passes None for all three; without a
# separate sentinel that call is indistinguishable from "tear down whatever
# the provider holds", which by then can be a LATER cycle's live graph
# (wh-stt-load-metrics.2.1.4).
_PROVIDER_RESOURCES = object()


class WinRTAudioCapture:
    """Audio capture using WinRT AudioGraph.

    Satisfies the AudioProvider Protocol in base.py, and is the only
    class that does. It was written as a drop-in replacement for
    MicrophoneStream, the PortAudio capture deleted by
    wh-portaudio-capture-removal.
    Uses Windows native audio APIs for microphone capture.

    Args:
        config: Audio configuration (rate, channels, chunk_ms).
        overflow_callback: Optional callback when queue overflows.
        outage_callback: Optional callback for a lost and a recovered
            microphone; see __init__.

    Example:
        ```python
        config = AudioConfig(rate=16000, chunk_ms=30)
        capture = WinRTAudioCapture(config)
        capture.start()
        if not capture.wait_ready():
            raise RuntimeError(capture.setup_error or "capture never started")

        while running:
            audio = capture.read(timeout=1.0)
            if audio:
                process(audio)

        capture.stop()
        ```
    """

    #: Where this backend loses frames, in words. _poll_frames drops a chunk
    #: when the queue it hands to the forwarder is full, and there is no
    #: PortAudio anywhere in this path. A provider reads this to write its
    #: own overflow line, and the monitor below is given the same phrase, so
    #: the two lines cannot drift apart.
    OVERFLOW_SOURCE = CAPTURE_QUEUE_SOURCE

    def __init__(
        self,
        config: Optional[AudioConfig] = None,
        overflow_callback: Optional[Callable] = None,
        outage_callback: Optional[
            Callable[[str, Optional[str]], None]] = None
    ):
        """Initialize WinRT audio capture.

        Args:
            config: Audio configuration. Defaults to 16kHz mono 30ms.
            overflow_callback: Called when audio queue overflows.
            outage_callback: Called on the capture thread as
                outage_callback("lost", None) once when an outage begins,
                and outage_callback("recovered", device) once when the
                first samples arrive after a rebuild; device is the opened
                microphone's name, or None. The reminders call nothing. The
                provider turns the two calls into on-screen notices
                (wh-mic-loss-notice).
        """
        self.config = config or AudioConfig()
        self.overflow_callback = overflow_callback
        self.outage_callback = outage_callback

        # WinRT objects (created on start)
        self._graph = None
        self._mic_node = None
        self._frame_output = None

        # Threading
        self._capture_thread: Optional[threading.Thread] = None
        self._running = False
        # ~10s of audio at 30ms chunks. Deep on purpose: when the whole
        # machine is CPU-saturated the consumer loop can stall for seconds
        # while capture keeps producing; once rescheduled it drains at many
        # times real time, so depth turns dropped frames into briefly
        # delayed frames (wh-stt-audio-consumer-behind-realtime).
        self._q: queue.Queue = queue.Queue(maxsize=333)

        # Setup handshake. _capture_loop performs graph setup and swallows
        # its failures, so this event is the only way a caller learns which
        # way setup went; it is set on BOTH paths, and _setup_ok is what
        # distinguishes them (streaming-provider review .1.22).
        self._setup_done = threading.Event()
        self._setup_ok = False
        self._setup_error: Optional[str] = None
        # Liveness, which _setup_ok cannot carry: _setup_ok records how
        # setup went and must keep saying so, while this says whether
        # the capture thread is still polling frames right now
        # (wh-stt-load-metrics.2). A plain bool because the only writes
        # are single assignments, the same as _setup_ok.
        self._capture_alive = False
        # Which start() cycle the capture thread belongs to. stop() joins for
        # 2.0s while setup budgets two 5.0s WinRT calls, so a thread can
        # outlive its own stop(); without this it would then rewrite the
        # flags stop() cleared, and after a restart answer the NEW cycle's
        # handshake and tear down its graph (wh-stt-load-metrics.2.1.2).
        # An int rather than an event because the question is which cycle a
        # write belongs to, not whether one has happened.
        self._cycle = 0
        # Held across every read of _cycle that decides a write, so the
        # decision and the write cannot be split by the scheduler. A plain
        # comparison followed by the writes was still wrong on a saturated
        # machine: the thread passes a test that was true, loses the CPU,
        # and writes into a cycle that has since been replaced
        # (wh-stt-load-metrics.2.1.3). Never held across a join, a WinRT
        # call, or any other blocking operation. start() does hold it across
        # Thread.start(), which waits for the child to reach _bootstrap and
        # nothing further; the child's first use of this lock is
        # _publish_cycle, well after that point (wh-stt-load-metrics.2.1.13).
        self._lifecycle_lock = threading.Lock()
        # Set by stop() so a capture thread waiting between two rebuild
        # attempts wakes at once instead of finishing its wait (boss ruling
        # C2, 20:56 2026-09-24). One per start() cycle, handed to that
        # cycle's thread, so a stop()/start() pair cannot clear the event a
        # stale thread is still waiting on.
        self._stop_event = threading.Event()
        # What _capture_until_stopped hands _poll_frames besides its two
        # arguments; see _poll_frames. Local to each capture thread.
        self._poll_context = threading.local()

        # Statistics
        self._frames_captured = 0
        self._drops = 0
        self._max_queue_depth = 0
        self._start_time: Optional[float] = None

        # Initialize overflow monitoring. The field names came from
        # MicrophoneStream, which a provider could be handed instead of
        # this class until wh-portaudio-capture-removal deleted it.
        overflow_config = OverflowConfig(
            overflow_threshold=5,
            window_seconds=30.0,
            restart_cooldown_seconds=60.0,
            max_restart_attempts=3,
            stable_reset_seconds=300.0,
            overflow_source=self.OVERFLOW_SOURCE
        )
        self.overflow_monitor = OverflowMonitor(overflow_config, self.overflow_callback)

    def start(self) -> None:
        """Start audio capture.

        Creates WinRT AudioGraph and begins capturing to internal queue.

        Returns as soon as the capture thread is spawned: the graph is
        built inside that thread, so a successful return does NOT mean
        the microphone opened. Follow with wait_ready() before treating
        the capture as live (streaming-provider review .1.22).
        """
        if not WINRT_AUDIO_AVAILABLE:
            raise RuntimeError("WinRT audio APIs not available")

        # One transition, so a thread from the previous cycle cannot observe
        # this cycle half-built: the moment _cycle moves, that thread is
        # stale, and every write it might attempt is refused from here on.
        with self._lifecycle_lock:
            if self._running:
                return

            self._cycle += 1

            # Clear the previous cycle's handshake so a stop()/start() pair
            # cannot answer wait_ready() with the old result.
            self._setup_done.clear()
            self._setup_ok = False
            self._setup_error = None
            self._capture_alive = False
            self._graph = None
            self._mic_node = None
            self._frame_output = None

            self._running = True
            cycle = self._cycle
            self._stop_event = threading.Event()
            stop_event = self._stop_event

            self._start_time = time.time()

            # Start capture in background thread, under the same lock stop()
            # holds while it takes that thread away. Releasing the lock here
            # first left a window in which _running was already True and no
            # thread existed yet: a stop() arriving in it cleared _running,
            # found the previous cycle's reference or None, joined nothing,
            # and returned as though the capture were down -- after which
            # this thread went on to build a WinRT graph
            # (wh-stt-load-metrics.2.1.13).
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                args=(cycle, stop_event),
                daemon=True,
                name="WinRTAudioCapture"
            )
            self._capture_thread.start()

        logger.debug(
            f"WinRT audio started: {self.config.rate}Hz, "
            f"{self.config.channels}ch, {self.config.chunk_ms}ms chunks"
        )

    def stop(self) -> None:
        """Stop audio capture and release resources."""
        with self._lifecycle_lock:
            if not self._running:
                return

            self._running = False
            self._stop_event.set()
            # Also cleared by the capture thread's own finally, but the join
            # below is bounded and a thread that outlives it must not leave
            # a stopped provider answering ready.
            self._capture_alive = False

            # Taken and cleared under the same lock start() holds while it
            # installs one, for the mirror image of the same race: reading
            # it after the lock let this stop join, and then null, a thread
            # a start() had already installed for the NEXT cycle, leaving
            # that cycle with nothing for its own stop() to join
            # (wh-stt-load-metrics.2.1.13).
            capture_thread = self._capture_thread
            self._capture_thread = None

            # Inside the same transition, so no cycle can begin between the
            # clear and the release. Clearing after the join left the queue
            # unguarded for the length of a bounded 2.0s wait: a start()
            # took the released lock, advanced the cycle, and installed a
            # thread whose chunks passed both write guards, after which
            # this clear erased audio the running cycle had captured. The
            # loss was counted nowhere, because _drops moves only on
            # queue.Full (wh-stt-load-metrics.2.1.15).
            with self._q.mutex:
                self._q.queue.clear()

        # The join is deliberately outside the lock: the thread it waits on
        # takes the same lock on its way out, so joining under it would
        # deadlock until the timeout expired every single time.
        if capture_thread:
            capture_thread.join(timeout=2.0)

        logger.debug("WinRT audio stopped")

    @property
    def setup_error(self) -> Optional[str]:
        """Why the current start() cycle's setup failed, None otherwise.

        None also while setup is still in progress and after a wait_ready()
        timeout: nothing has failed yet in either case.
        """
        return self._setup_error

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Whether the AudioGraph is running, waiting out setup first.

        Two conditions, because setup succeeding is not the same as
        capture still running. Before this, _setup_ok stayed True
        through everything that came after it, so the provider went on
        answering ready while capturing nothing and the load reporter
        printed drops=0 for a dead microphone
        (wh-stt-load-metrics.2).

        Exactly three things take the answer back, and it is worth being
        precise about them, because an earlier version of this text
        named a case the code did not actually cover
        (wh-stt-load-metrics.2.1.1):

        1. The capture thread ending, whichever way it leaves.
        2. POLL_FAILURES_BEFORE_DEAD consecutive frame polls raising.
           The poll loop catches each one and keeps running, so the
           thread stays alive and its own lifetime cannot report this.
        3. A lost microphone (wh-mic-loss-capture-recovery): the graph's
           UnrecoverableErrorOccurred event, or CAPTURE_STALL_SECONDS
           with no samples. _mark_lost takes the answer back, and it
           stays False while the thread rebuilds the graph, until a
           rebuilt graph delivers samples. A rebuilt graph that opens
           but delivers nothing never answers ready.

        A single poll that succeeds and returns no frame is not one of
        them: a quiet microphone still delivers samples, so only a run
        of empty polls as long as CAPTURE_STALL_SECONDS counts as a
        loss.

        On the first start the answer is True as soon as the graph is
        built, before any sample arrives, exactly as before the rebuild
        existed; the provider's start-up handshake is unchanged.

        The wait is over setup only. A dead graph has already set the
        handshake, so this answers immediately rather than spending the
        timeout on an outcome the provider already knows.

        Args:
            timeout: Maximum seconds to wait for setup to finish.

        Returns:
            True only while the AudioGraph is running. False when setup
            raised (setup_error holds the message), when the wait
            expired with setup still unfinished (setup_error is None),
            when the capture thread has since ended (setup_error is
            None as well -- nothing about the setup failed), and during
            a microphone outage until a rebuilt graph delivers samples.
        """
        if not self._setup_done.wait(timeout):
            return False
        # The wait is outside the lock and the paired read is inside it:
        # blocking under the lock would stall every start() and stop() for
        # the whole timeout, and reading the two flags separately could
        # answer from a state that never existed.
        with self._lifecycle_lock:
            return self._setup_ok and self._capture_alive

    def read(self, timeout: float = 1.0) -> Optional[bytes]:
        """Read audio chunk from capture queue.

        Args:
            timeout: Maximum seconds to wait for audio.

        Returns:
            Audio bytes (int16 PCM) or None if timeout.
        """
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_stats(self) -> AudioStats:
        """Get capture statistics."""
        return {
            'captured': self._frames_captured,
            'drops': self._drops,
            'qsize': self._q.qsize(),
            'max_q': self._max_queue_depth,
        }

    def get_queue_size(self) -> int:
        """Get current queue depth."""
        return self._q.qsize()

    def reset_overflow_monitor(self) -> None:
        """Reset overflow monitoring state after restart."""
        self.overflow_monitor.reset_for_restart()

    def get_overflow_status(self) -> dict:
        """Get current overflow monitoring status for debugging."""
        return self.overflow_monitor.get_status()

    def list_audio_devices(self) -> list:
        """List available audio input devices.

        Uses WinRT device enumeration to find audio capture devices.

        Returns:
            List of dicts with device info: index, name, rate, channels
        """
        if not WINRT_AUDIO_AVAILABLE:
            return []

        try:
            from winsdk.windows.devices.enumeration import DeviceInformation, DeviceClass

            # Try to import winrt_helpers from wheelhouse
            # This is optional - if not available, we'll skip device enumeration
            try:
                import sys
                import os
                # Look for wheelhouse in common locations
                possible_paths = [
                    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'wheelhouse')),
                    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'wheelhouse')),
                ]
                for path in possible_paths:
                    if os.path.exists(path) and path not in sys.path:
                        sys.path.insert(0, path)

                from utils.winrt_helpers import run_winrt_sync

                # Find audio capture devices
                devices = run_winrt_sync(
                    DeviceInformation.find_all_async(DeviceClass.AUDIO_CAPTURE),
                    timeout=5.0
                )

                result = []
                for i, device in enumerate(devices):
                    result.append({
                        'index': i,
                        'name': device.name,
                        'rate': 16000,  # Default, WinRT handles conversion
                        'channels': 1,
                    })
                return result

            except ImportError:
                logger.warning("winrt_helpers not available, cannot enumerate devices")
                return []

        except Exception as e:
            logger.warning(f"Failed to enumerate audio devices: {e}")
            return []

    def _capture_loop(self, cycle: int,
                      stop_event: Optional[threading.Event] = None) -> None:
        """Background thread for audio capture.

        Creates WinRT graph and polls for frames, converting to bytes
        and queuing for consumption by read(). When the microphone is lost
        the same thread closes the graph and builds a new one, in the same
        start() cycle, until samples arrive again or the cycle ends
        (wh-mic-loss-capture-recovery).

        The graph, the microphone node and the frame output stay local to
        this thread until _publish_cycle installs them, and this thread is
        the only one that ever closes them. Setup can outlast the 2.0s join
        in stop(), so a thread that finishes late would otherwise hand its
        own resources to a cycle that has moved on, and take that cycle's
        resources with it (wh-stt-load-metrics.2.1.2, .2.1.3).

        Args:
            cycle: The start() cycle this thread belongs to. Every write it
                makes to the provider's shared state goes through
                _publish_cycle or _retire_cycle, both of which refuse a
                cycle that is no longer current.
            stop_event: Set by this cycle's stop(). Only the wait between
                two rebuild attempts reads it. None, for a caller that runs
                the loop directly, means an event nothing sets, so that wait
                then lasts its full length.
        """
        if stop_event is None:
            stop_event = threading.Event()
        # Every graph this thread currently owns, whatever it is. The rebuild
        # replaces the contents, and the finally below closes what is there
        # when the thread leaves, however it leaves.
        owned = [None, None, None]
        mmcss = None
        try:
            # Audio capture must stay scheduled when the machine is saturated
            # by bulk compute. Ask MMCSS first (wh-stt-load-metrics.3): a
            # priority raised with SetThreadPriority still competes inside
            # the ordinary scheduler, which is what left the sounddevice
            # path's capture thread unscheduled for up to 2.4 s on Ikon on
            # 2026-09-03 while an independent capture from the same
            # microphone lost nothing. MMCSS is the separate path the
            # Windows audio engine itself uses, and it gives a registered
            # thread a guaranteed share of every scheduling period.
            #
            # The registration is made here rather than in start() because
            # MMCSS records it against the CALLING thread, and only that
            # thread may release it. That is also why the revert is in this
            # function's finally rather than in stop().
            try:
                mmcss = register_current_thread_mmcss()
            except Exception as e:
                # register_current_thread_mmcss already promises never to
                # raise. This catch keeps that promise true for the capture
                # path even if a future edit breaks it: a failed scheduling
                # request must never stop the AudioGraph being built.
                mmcss = None
                logger.info(f"MMCSS registration raised: {e}; "
                            f"falling back to thread priority")
            if mmcss is not None and mmcss.handle is not None:
                # INFO, not DEBUG, and the same for the three lines below.
                # The provider calls logging.basicConfig(level=logging.INFO)
                # at sherpa_offline_parakeet_stt_server/main.py line 34, so a
                # DEBUG line is discarded before it is written and an
                # operator cannot tell from a log whether this thread got its
                # registration. The level was set while the sounddevice
                # path still existed and logged the same fact at INFO
                # (microphone.py drained _pending_log_msgs with
                # logger.info), because the two paths must not disagree
                # about the level of one fact (boss ruling 2026-09-03
                # 15:52). That path is gone (wh-portaudio-capture-removal)
                # and the level stays: the operator's need to read the line
                # is what the ruling rested on, and it did not go with it.
                logger.info(
                    f"Capture thread registered with MMCSS task "
                    f"{mmcss.task!r} (handle {mmcss.handle:#x})")
            else:
                # A successful registration does NOT also elevate. Windows
                # hands an MMCSS thread's scheduling to MMCSS, whose own
                # control is AvSetMmThreadPriority, so SetThreadPriority is
                # the fallback rather than an addition. The thread sleeps
                # most of every poll interval, so time-critical priority
                # costs the rest of the system nothing.
                if mmcss is not None:
                    logger.info(
                        f"MMCSS registration failed for task "
                        f"{mmcss.task!r}: {mmcss.error}; falling back to "
                        f"thread priority")
                elevated = elevate_current_thread('time_critical')
                logger.info(f"Capture thread priority elevated: {elevated}")
            owned[:] = self._setup_graph()
            if self._publish_cycle(cycle, *owned):
                self._capture_until_stopped(cycle, stop_event, owned)
        except Exception as e:
            logger.error(f"WinRT capture error: {e}")
            # Only a pre-handshake failure is a SETUP failure. _poll_frames
            # handles its own errors, but were one ever to escape it, the
            # handshake has already answered True and must not be rewritten
            # under a caller that has moved on. The cycle test is what stops
            # a stale thread reporting its own setup failure as the CURRENT
            # cycle's; _running is deliberately not tested here, because a
            # setup that raised during an ordinary stop() still owns its
            # cycle's handshake and a caller may be blocked on it.
            with self._lifecycle_lock:
                if self._cycle == cycle and not self._setup_done.is_set():
                    self._setup_error = str(e)
        finally:
            # Before anything else in this finally: only this thread may
            # release its own registration, and a thread that exits still
            # holding one leaves the service holding a handle nothing can
            # release.
            if mmcss is not None and mmcss.handle is not None:
                revert_current_thread_mmcss(mmcss.handle)
            self._retire_cycle(cycle)
            # Whatever this thread built, whether or not it was ever
            # published, and outside the lock because closing a WinRT graph
            # is a blocking call. Every other thread is closing a different
            # object, so no two of these can collide.
            self._cleanup_graph(*owned)

    def _publish_cycle(self, cycle: int, graph, mic_node,
                       frame_output) -> bool:
        """Install a thread's resources and open the readiness handshake.

        One transition under the lock, because the test and the writes
        together are what has to be indivisible: a thread descheduled
        between them would install a graph for a cycle that had already
        been stopped or replaced (wh-stt-load-metrics.2.1.3).

        Args:
            cycle: The start() cycle the calling thread belongs to.
            graph: The AudioGraph that thread created.
            mic_node: Its device input node.
            frame_output: Its frame output node.

        Returns:
            True when the resources were installed and the caller should go
            on to poll. False when the cycle has been stopped or replaced,
            in which case nothing was written and the caller owns the
            teardown of what it built.
        """
        with self._lifecycle_lock:
            # _running covers a plain stop(); the cycle number covers a
            # restart, where _running is True again and would otherwise let
            # a stale thread through.
            if not self._running or self._cycle != cycle:
                return False

            self._graph = graph
            self._mic_node = mic_node
            self._frame_output = frame_output
            self._setup_ok = True
            # Before the handshake is released, or a caller could wake
            # between the two and read a live graph as dead.
            self._capture_alive = True
            self._setup_done.set()
            return True

    def _retire_cycle(self, cycle: int) -> bool:
        """Close the readiness handshake for a thread that is leaving.

        _running is NOT tested here, unlike in _publish_cycle: an ordinary
        stop() has already set it False by the time this runs, so testing it
        would skip the teardown on the normal shutdown path and leave a
        stopped provider still answering ready.

        Args:
            cycle: The start() cycle the calling thread belongs to.

        Returns:
            True when this thread's cycle was still the current one and the
            provider's state was cleared, False when a later cycle owns that
            state and nothing was written.
        """
        with self._lifecycle_lock:
            if self._cycle != cycle:
                return False

            # Whichever way the thread leaves, the graph is about to be
            # torn down and no more frames are coming. _setup_ok is left
            # alone: it answers how setup went, and rewriting it here
            # would turn a graph that ran for an hour into a graph that
            # never came up (wh-stt-load-metrics.2).
            self._capture_alive = False
            # Set on every exit path: a caller blocked in wait_ready()
            # must learn about a failed setup immediately, not after its
            # timeout.
            self._setup_done.set()
            # The objects themselves are closed by the leaving thread, which
            # holds its own references to them.
            self._graph = None
            self._mic_node = None
            self._frame_output = None
            return True

    def _capture_until_stopped(self, cycle: int, stop_event: threading.Event,
                               owned: list) -> None:
        """Poll the published graph, and rebuild it whenever it is lost.

        Returns when the cycle ends. Nothing else ends it: a microphone that
        never comes back is retried for as long as the provider runs, every
        REBUILD_RETRY_SECONDS, because the provider cannot know how long a
        user will leave a device unplugged (boss ruling 20:56 2026-09-24).

        Args:
            cycle: The start() cycle the calling thread belongs to.
            stop_event: Set by this cycle's stop().
            owned: The calling thread's [graph, mic_node, frame_output],
                already published. Replaced in place on each rebuild, so the
                thread's finally always closes the graph it holds last.
        """
        outage: Optional[_Outage] = None
        while True:
            loss = self._watch_for_loss(owned[0])
            self._poll_context.loss = loss
            self._poll_context.recovering = outage
            # Its own node, not the attribute: a thread that publishes
            # after this one would otherwise redirect this loop onto a
            # graph nobody is reading.
            reason = self._poll_frames(cycle, owned[2])
            # Only a string names a loss. Anything else -- None from a
            # stopped cycle, or a test's stand-in for _poll_frames -- ends
            # the thread the way the poll loop ending always has.
            if not isinstance(reason, str):
                return
            if outage is not None and outage.over:
                outage = None

            self._mark_lost(cycle)
            self._forget_loss_handler(owned[0], loss)
            self._cleanup_graph(*owned)
            owned[:] = [None, None, None]

            if outage is None:
                outage = _Outage(reason)
                self._report_outage('lost', None)
            else:
                # A graph that built but never delivered: one more failed
                # attempt in the same outage (ruling C1).
                outage.last_error = reason
                outage.remind()

            rebuilt = self._rebuild(cycle, stop_event, outage)
            if rebuilt is None:
                return
            owned[:] = rebuilt

    def _report_outage(self, event: str, device: Optional[str]) -> None:
        """Tell the provider an outage began or ended (wh-mic-loss-notice).

        Ruling K1: the callback runs inside this try, so a notice fault is
        one WARNING line and never stops the recovery, and both call sites
        hold no lock -- the loss call comes after _mark_lost released
        _lifecycle_lock, and the recovery call runs after the frame's buffer
        is closed and before the chunk loop takes _lifecycle_lock. The
        callback is expected to return at once: the providers' callback only
        schedules a send onto the forwarder's own loop.
        """
        callback = self.outage_callback
        if callback is None:
            return
        try:
            callback(event, device)
        except Exception as e:
            # Worded so it can never be read as a second loss or recovery
            # line: the log reader and the tests count those by their
            # opening words.
            logger.warning(
                f'The {event} notice for the microphone outage failed: '
                f'{type(e).__name__}: {e}')

    def _watch_for_loss(self, graph) -> _GraphLoss:
        """Subscribe to one graph's UnrecoverableErrorOccurred event."""
        loss = _GraphLoss()
        try:
            loss.token = graph.add_unrecoverable_error_occurred(loss.handler)
        except Exception as e:
            # The sample stall still detects a lost microphone, so a graph
            # that refuses the subscription is still watched.
            logger.debug(f'UnrecoverableErrorOccurred subscription failed: {e}')
        return loss

    @staticmethod
    def _forget_loss_handler(graph, loss: _GraphLoss) -> None:
        if loss.token is None:
            return
        try:
            graph.remove_unrecoverable_error_occurred(loss.token)
        except Exception as e:
            logger.debug(f'UnrecoverableErrorOccurred removal failed: {e}')

    def _mark_lost(self, cycle: int) -> None:
        """Take readiness back and drop the published graph after a loss.

        The graph itself is closed by the capture thread, which holds its own
        references. _setup_ok and _setup_error are left alone: they answer
        how the FIRST setup went, and a caller has already read them.
        """
        with self._lifecycle_lock:
            # _running as well as the cycle, for the reason given at the
            # POLL_FAILURES_BEFORE_DEAD write in _poll_frames.
            if not self._running or self._cycle != cycle:
                return
            self._capture_alive = False
            self._graph = None
            self._mic_node = None
            self._frame_output = None

    def _rebuild(self, cycle: int, stop_event: threading.Event,
                 outage: _Outage) -> Optional[tuple]:
        """Build a new graph, retrying until one builds or the cycle ends.

        The first attempt runs at once. After a failed attempt the thread
        waits REBUILD_RETRY_SECONDS on stop_event, never on time.sleep, so a
        stop() in the wait returns at once (ruling C2).

        Returns:
            The new (graph, mic_node, frame_output), installed on the
            provider but NOT yet marked alive: the recovery counts only when
            samples arrive (ruling C1). None when the cycle ended first.
        """
        while self._running and self._cycle == cycle:
            outage.attempts += 1
            try:
                triple = self._setup_graph(log_failure=False)
            except Exception as e:
                outage.last_error = str(e)
                logger.debug(f'Microphone rebuild attempt {outage.attempts} '
                             f'failed: {e}')
                outage.remind()
                if stop_event.wait(REBUILD_RETRY_SECONDS):
                    return None
                continue

            outage.device = _device_name(triple[1])
            with self._lifecycle_lock:
                if self._running and self._cycle == cycle:
                    self._graph, self._mic_node, self._frame_output = triple
                    return tuple(triple)
            # The cycle ended while the graph was being built. Nobody else
            # can reach it, so this thread closes it.
            self._cleanup_graph(*triple)
            return None
        return None

    def _setup_graph(self, log_failure: bool = True):
        """Create and configure WinRT AudioGraph.

        Returns what it built rather than installing it on the provider, so
        the thread that created a graph is the only thread that can hand it
        over and the only thread that closes it (wh-stt-load-metrics.2.1.3).

        Args:
            log_failure: Whether a missing output device writes its ERROR
                line. The rebuild after a lost microphone passes False: it
                retries every REBUILD_RETRY_SECONDS, and its own reminder
                line carries the failure at most once a minute
                (wh-mic-loss-capture-recovery).

        Returns:
            A (graph, mic_node, frame_output) tuple. The graph is already
            started.
        """
        # Import here to avoid import errors when WinRT not available
        from winsdk.windows.media.audio import AudioGraph, AudioGraphSettings
        from winsdk.windows.media.render import AudioRenderCategory
        from winsdk.windows.media.mediaproperties import AudioEncodingProperties
        from winsdk.windows.media.capture import MediaCategory

        # Try to import winrt_helpers
        import sys
        import os
        possible_paths = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'wheelhouse')),
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'wheelhouse')),
        ]
        for path in possible_paths:
            if os.path.exists(path) and path not in sys.path:
                sys.path.insert(0, path)

        from utils.winrt_helpers import run_winrt_sync

        # Create encoding properties for STT
        props = AudioEncodingProperties.create_pcm(
            self.config.rate,
            self.config.channels,
            16  # bits per sample
        )

        # Create graph settings.
        #
        # WHY COMMUNICATIONS AND NOT SPEECH
        # (wh-screen-reader-audio-suppression-conflict A1/A4). Windows
        # runs its acoustic echo canceller for Communications streams,
        # referenced against what the machine is playing. That is the
        # only thing that can keep a screen reader's speech out of the
        # recogniser while the microphone stays open. The other defence,
        # pausing the microphone while sound plays, applies only where
        # Windows reports no echo canceller for the microphone, or where
        # ENABLE_AUDIO_SUPPRESSION forces it (wh-audio-suppression-auto),
        # and that left listening available for 0.330 s of a 45.008 s
        # measurement, 0.73%, while NVDA read a page aloud.
        #
        # CAVEAT 1, QUALITY. Communications also turns on noise
        # suppression and automatic gain control, so recognition quality
        # may change for every provider, not only for screen reader
        # users.
        #
        # CAVEAT 2, WHAT IS NOT YET MEASURED. On this machine on
        # 2026-08-27, AudioEffectsManager reported the same four effects
        # -- echo cancellation, noise suppression, automatic gain
        # control and deep noise suppression -- for MediaCategory.SPEECH
        # as for MediaCategory.COMMUNICATIONS. That query names the
        # capture category alone and does not cover the graph's render
        # category, and nobody has yet recorded this path with a screen
        # reader talking. So the gain over SPEECH is unproven here.
        # David measures it with NVDA after this merges, and his rule is
        # recorded on the bead: if the canceller works, the next bead
        # narrows the pause; if it does not, stop and price the
        # fallback.
        #
        # One category for every provider: no configuration switch and
        # no fallback (David's capture-path rule, 2026-09-05).
        settings = AudioGraphSettings(AudioRenderCategory.COMMUNICATIONS)
        settings.encoding_properties = props

        # Create graph
        result = run_winrt_sync(AudioGraph.create_async(settings), timeout=5.0)
        if result.status != 0:
            # One status has an answer a user can act on, so it gets
            # words written for a user. The rest name nothing anyone
            # could do, and inventing an instruction for them would
            # send someone to check speakers that already work
            # (wh-capture-winrt-required A10).
            if result.status == AUDIO_GRAPH_DEVICE_NOT_AVAILABLE:
                # The enum name goes in the log, never in the
                # notice: the notice has 209 characters to spend and
                # the user sentences use all of them. See
                # AUDIO_DEVICE_MISSING_MESSAGE above for the
                # measurement.
                if log_failure:
                    logger.error(AUDIO_DEVICE_MISSING_LOG_LINE)
                raise RuntimeError(AUDIO_DEVICE_MISSING_MESSAGE)
            raise RuntimeError(f"AudioGraph creation failed: status={result.status}")

        graph = result.graph

        # From here the graph exists and this thread is the only thing that
        # can reach it. Any of the four calls below can fail, and a graph
        # left open holds the microphone for the life of the process
        # (wh-stt-load-metrics.2.1.4).
        try:
            # Create microphone input. The category is Communications
            # for the reason written at the AudioGraphSettings call
            # above: it is what asks Windows for the echo canceller.
            # Both sites must name it -- the graph sets the render
            # category, this one sets the capture category, and the
            # canceller needs the pair.
            input_result = run_winrt_sync(
                graph.create_device_input_node_async(MediaCategory.COMMUNICATIONS),
                timeout=5.0
            )
            if input_result.status != 0:
                raise RuntimeError(f"Microphone node failed: status={input_result.status}")

            mic_node = input_result.device_input_node

            # Create frame output
            frame_output = graph.create_frame_output_node(props)

            # Connect mic to output
            mic_node.add_outgoing_connection(frame_output)

            # Start the graph
            graph.start()
        except Exception:
            self._cleanup_graph(graph, None, None)
            raise

        logger.debug("WinRT AudioGraph started")
        return graph, mic_node, frame_output

    def _poll_frames(self, cycle: int, frame_output) -> Optional[str]:
        """Poll for audio frames and queue them.

        KEY INSIGHT: AudioGraph ALWAYS outputs float32 audio internally, regardless of
        the encoding properties set on AudioGraphSettings. The encoding_properties setting
        affects device/render format, not internal processing. We must convert to int16.

        Every per-frame exception is caught here and the loop keeps running,
        so this loop -- not the thread's lifetime -- is the only thing that
        can notice a graph whose polls have started failing. It counts
        CONSECUTIVE failures and clears _capture_alive at
        POLL_FAILURES_BEFORE_DEAD, then sets it back on the first poll that
        works: a device that comes back needs no external restart, and the
        load reporter marks exactly the windows in which the graph was
        failing (wh-stt-load-metrics.2.1.1).

        This covers a poll that RAISES. A poll that succeeds and yields no
        samples is NOT a quiet microphone: a quiet microphone still delivers
        samples, and on 2026-09-24 the lost Scarlett Solo showed exactly this
        pattern until Wheelhouse was restarted. So the loop also returns when
        no sample has arrived for CAPTURE_STALL_SECONDS, whether the polls
        in that time raised or answered empty, and when the graph's own
        UnrecoverableErrorOccurred event has fired. The caller then rebuilds
        the graph (wh-mic-loss-capture-recovery). The stall test compares
        the time each poll STARTED with the time of the last samples, so a
        loss needs a poll that started CAPTURE_STALL_SECONDS after the last
        samples and then found none or raised. A thread that was not
        scheduled for longer than the threshold -- one that waited for the
        GIL after its sleep, for example -- polls once more first, collects
        its backlog, and is not counted as a stall
        (wh-mic-false-loss-stall-check).

        Args:
            cycle: The start() cycle this loop belongs to. The loop itself
                ends when the cycle changes -- testing _running alone was
                not enough, because a restart sets it True again and a
                thread blocked in get_frame past stop()'s bounded join would
                resume and keep polling for the life of the process
                (wh-stt-load-metrics.2.1.3). The queue and counter writes
                are guarded by it too, for the iteration that was already
                running when the restart landed.
            frame_output: The node this thread's own _setup_graph created.
                Read from the argument rather than from the provider, so a
                later thread publishing its own node cannot redirect this
                loop onto a graph nobody is reading.

        Two more inputs come from _capture_until_stopped through
        self._poll_context, which is local to the calling thread: `loss`,
        the graph's UnrecoverableErrorOccurred record, and `recovering`, the
        outage a rebuilt graph is meant to end. While `recovering` is set,
        the loop marks the capture alive only once samples arrive, and the
        first samples write the recovery line (ruling C1). They are not
        parameters because many tests stand in for this method with a
        two-argument function, and a thread-local store cannot be read by
        a stale thread of an earlier cycle.

        Returns:
            Why the microphone counts as lost, or None when the loop ended
            because the cycle was stopped or replaced.
        """
        from winsdk.windows.media import AudioBufferAccessMode

        # Calculate poll interval based on chunk size
        # Poll at 2x the chunk rate to avoid missing data
        poll_interval = self.config.chunk_ms / 1000.0 / 2
        target_bytes = self.config.bytes_per_chunk

        buffer_accumulator = bytearray()
        consecutive_failures = 0
        # The stall clock starts with the loop, so a graph that is built and
        # never delivers is given the same CAPTURE_STALL_SECONDS as one that
        # stops delivering later.
        last_samples_at = _monotonic()
        delivered = False
        # Set at the first samples of a recovery; read once the frame's
        # buffer is closed (wh-mic-loss-notice).
        announce_recovery = False
        loss = getattr(self._poll_context, 'loss', None)
        recovering = getattr(self._poll_context, 'recovering', None)

        # Deliberately read without the lock: this decides only whether to
        # make another WinRT call, never a write, and holding the lock across
        # get_frame would block stop() for the length of a device call. Every
        # write below re-tests the same pair under the lock, so the worst a
        # stale read here can buy is one extra poll whose writes are refused
        # (wh-stt-load-metrics.2.1.5).
        while self._running and self._cycle == cycle:
            if loss is not None and loss.event.is_set():
                return loss.reason
            # When this poll started, for the stall test below. GetFrame
            # returns everything captured since the previous call, so only a
            # poll that STARTED past the threshold and found nothing proves a
            # stall; the time after the sleep does not (wh-mic-false-loss-
            # stall-check).
            poll_started = _monotonic()
            try:
                frame = frame_output.get_frame()
                if frame:
                    # Extract bytes from frame using buffer protocol
                    audio_buffer = frame.lock_buffer(AudioBufferAccessMode.READ)
                    try:
                        ref = audio_buffer.create_reference()
                        # IMemoryBufferReference supports Python buffer protocol
                        # Use memoryview to access the underlying byte data
                        float32_data = bytes(memoryview(ref))

                        # AudioGraph outputs float32 (-1.0 to 1.0), convert to int16
                        # Each float32 is 4 bytes, each int16 is 2 bytes
                        if len(float32_data) >= 4:
                            num_samples = len(float32_data) // 4
                            # struct decoded float32 into Python doubles. Keep
                            # that precision at integer conversion boundaries.
                            float_samples = np.frombuffer(
                                float32_data, dtype='<f4', count=num_samples,
                            ).astype(np.float64)
                            finite = np.isfinite(float_samples)
                            if not finite.all():
                                # Keep the existing frame-error path, rather
                                # than silently converting NaN/inf to PCM.
                                int(float_samples[np.flatnonzero(~finite)[0]])
                            float_samples *= 32767
                            np.clip(float_samples, -32768, 32767, out=float_samples)
                            int16_data = float_samples.astype('<i2').tobytes()
                            buffer_accumulator.extend(int16_data)
                            last_samples_at = _monotonic()
                            if not delivered:
                                delivered = True
                                if recovering is not None:
                                    recovering.recovered()
                                    announce_recovery = True
                    finally:
                        audio_buffer.close()

                    # After the buffer is released and before the chunk
                    # loop takes _lifecycle_lock, so the provider's callback
                    # runs under no lock at all (ruling K1).
                    if announce_recovery and recovering is not None:
                        announce_recovery = False
                        self._report_outage('recovered', recovering.device)

                    # Yield chunks of target size (in int16 bytes)
                    while len(buffer_accumulator) >= target_bytes:
                        chunk = bytes(buffer_accumulator[:target_bytes])
                        buffer_accumulator = buffer_accumulator[target_bytes:]

                        overflowed = False
                        with self._lifecycle_lock:
                            if not self._running or self._cycle != cycle:
                                # A stop() or a restart landed while this
                                # iteration was inside get_frame. The audio
                                # belongs to a graph the provider is no
                                # longer reading, and the queue and the
                                # counters it would go into are the load
                                # reporter's evidence
                                # (wh-stt-load-metrics.2.1.3). The test and
                                # the writes are ONE transition, so a thread
                                # descheduled between them cannot write into
                                # the cycle that replaced it. stop() clears
                                # the queue inside its own transition and
                                # never moves the cycle, so that clear can
                                # neither be undone by a stale write nor
                                # reach past the lock to erase what a later
                                # cycle wrote (wh-stt-load-metrics.2.1.5,
                                # .2.1.15).
                                continue

                            self._frames_captured += 1

                            try:
                                self._q.put_nowait(chunk)
                                qsize = self._q.qsize()
                                if qsize > self._max_queue_depth:
                                    self._max_queue_depth = qsize
                            except queue.Full:
                                self._drops += 1
                                overflowed = True

                        # Outside the lock: the monitor may call back into a
                        # restart, which takes the same lock.
                        if overflowed:
                            self.overflow_monitor.report_overflow()

                time.sleep(poll_interval)

                # Only after a whole iteration has come through without
                # raising, so a failure anywhere above still counts.
                consecutive_failures = 0
                # A rebuilt graph is alive once it has delivered samples, not
                # once it has built (ruling C1).
                if recovering is None or delivered:
                    with self._lifecycle_lock:
                        if self._running and self._cycle == cycle:
                            self._capture_alive = True

            except Exception as e:
                if self._running:
                    logger.warning(f"Frame poll error: {e}")
                    consecutive_failures += 1
                    if consecutive_failures >= POLL_FAILURES_BEFORE_DEAD:
                        with self._lifecycle_lock:
                            # _running as well as the cycle: declaring the
                            # provider dead is the direction that blanks the
                            # load reporter's fields, so a thread that is no
                            # longer the live one may not do it
                            # (wh-stt-load-metrics.2.1.5).
                            if self._running and self._cycle == cycle:
                                self._capture_alive = False
                    time.sleep(0.1)

            # After the poll on both paths, so polls that raise and polls
            # that answer empty both run the clock out. The poll's start
            # time, not the time now: on 2026-09-25 the capture thread waited
            # seconds for the GIL after its sleep, and a test of the time now
            # declared 39 false losses before the next poll could collect the
            # audio. A poll that delivered samples set last_samples_at after
            # poll_started, so it never counts.
            if poll_started - last_samples_at >= CAPTURE_STALL_SECONDS:
                return f'no audio samples for {CAPTURE_STALL_SECONDS:.1f} s'
        return None

    def _cleanup_graph(self, graph=_PROVIDER_RESOURCES, mic_node=None,
                       frame_output=None) -> None:
        """Clean up WinRT resources.

        Args:
            graph: The AudioGraph to tear down. Omit it to tear down
                whatever the provider currently holds, which is what a
                caller outside the capture thread wants. A capture thread
                passes its OWN objects instead, because by the time it
                leaves, the provider may hold a later cycle's graph and
                closing that would stop a graph another thread is polling
                (wh-stt-load-metrics.2.1.3). Passing an explicit None means
                "this thread built nothing", which is why the default is a
                sentinel and not None: a thread whose setup raised owns
                nothing, and reading the provider's fields for it is the
                same defect from the other side
                (wh-stt-load-metrics.2.1.4).
            mic_node: Unused beyond making the thread's ownership explicit;
                closing the graph releases its nodes.
            frame_output: Likewise.
        """
        if graph is _PROVIDER_RESOURCES:
            graph = self._graph
            self._graph = None
            self._mic_node = None
            self._frame_output = None

        if not graph:
            return

        # Two obligations, two try blocks: a graph whose stop() raises still
        # holds the device until close() runs, and one block around both
        # skips the call that actually releases it
        # (wh-stt-load-metrics.2.1.4).
        try:
            graph.stop()
        except Exception as e:
            logger.warning(f"Graph stop error: {e}")

        try:
            graph.close()
        except Exception as e:
            logger.warning(f"Graph cleanup error: {e}")
