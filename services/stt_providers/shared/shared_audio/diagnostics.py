"""Microphone diagnostic tools for testing audio capture.

This module provides utilities for testing and validating microphone functionality
before starting the main speech recognition process. It includes tools for
recording test audio, checking audio levels, and optionally saving diagnostic
recordings to disk for troubleshooting purposes.

Key Functions:
  - run_mic_check: Tests microphone capture for a specified duration.

Key Classes:
  - LoopStallTracker: Measures the gap between iterations of a per-frame
    consumer loop and reports the gaps longer than a threshold. A long gap
    means the loop did not run; this class cannot tell whole-machine CPU
    starvation from a blocking call inside the loop, so it names neither
    (wh-stt-audio-consumer-behind-realtime, wh-stt-load-metrics.3).
  - CaptureLoadReporter: Periodic one-line report of capture queue depth,
    lost input frames, and consumer-loop stalls, for telling capture loss
    apart from inference lag under CPU load (wh-stt-load-metrics).

Typical Usage:
  from shared_audio.diagnostics import run_mic_check
  from shared_audio.capture import get_audio_provider, AudioConfig

  config = AudioConfig(rate=16000, chunk_ms=20)
  provider = get_audio_provider(config)

  # Test microphone for 5 seconds
  result = run_mic_check(provider, duration_seconds=5.0, rate=16000, chunk_ms=20)
  if result != 0:
      print("Microphone test failed")
"""
import array
import logging
import math
import time
import wave
import contextlib
from typing import Callable, Optional, Protocol, TYPE_CHECKING

logger = logging.getLogger(__name__)


class AudioProviderProtocol(Protocol):
    """Protocol for audio provider interface."""
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self, timeout: float = 1.0) -> Optional[bytes]: ...


# How every stall message begins. A constant rather than a literal in two
# files because the Google loop reads it to decide where its
# [stall-where] breakdown belongs: the breakdown follows the stall line
# and nothing else the reporter returns (wh-stt-load-metrics.4 G1a).
STALL_PREFIX = '[stall] '


class LoopStallTracker:
    """Detects when a per-frame consumer loop stops making progress.

    The STT main loop normally iterates every <=80ms (one 30ms frame plus the
    mic-read timeout). When the machine is CPU-saturated the loop can go
    unscheduled for seconds; the capture queue then fills and frames drop with
    no direct log signature -- only the resulting overflow counts. Call
    record() once per loop iteration: after a gap longer than the threshold it
    returns a log-ready message (rate-limited), and window counters accumulate
    for periodic diagnostics.

    State ownership: reset() owns only the gap-measurement state (_last_time);
    the window counters (stall_count, max_gap_ms) are owned by
    snapshot_and_reset_window(), which defines the reporting window. A mic
    restart therefore does NOT clear the window counters -- stalls recorded
    before the restart still belong to the current reporting window, matching
    the other [overflow-diag] accumulators (VAD/AGC timing lists).

    There are TWO windows over the one detector, and they are independent.
    The reporting window above belongs to whatever prints the periodic
    summary. The utterance window (utterance_stalls, utterance_max_gap_ms,
    owned by snapshot_and_reset_utterance) belongs to a caller that needs
    exact numbers for one utterance -- the Google provider's per-utterance
    [load-diag] line. record() advances both from the same gap, so the two
    always describe the same stalls; only their start and end differ. Two
    windows, not two detectors: there is still one threshold, one clock and
    one gap measurement, so the two readers cannot disagree about what a
    stall is. The alternative -- reading stall_count at utterance start and
    differencing at the end -- is not exact, because the periodic block's
    snapshot_and_reset_window() zeroes those counters whenever its interval
    lands inside an utterance (wh-stt-load-metrics.4).

    Threading: no internal locking; record() and reset() must never run
    concurrently. The supported usage is record() from a single loop or
    callback thread and reset() from the lifecycle-management thread, with
    the caller guaranteeing the two cannot overlap. MicrophoneStream got this
    guarantee from stream sequencing: start() called reset() before the
    stream (and its callback thread) existed, and stop() blocked until
    PortAudio had quiesced the callback. That class is deleted
    (wh-portaudio-capture-removal), and WinRTAudioCapture owes the same
    guarantee. Do not add a record()/reset() call from a new context
    without providing it.
    """

    def __init__(self, stall_threshold_s: float = 1.0,
                 min_log_interval_s: float = 5.0, clock=time.monotonic,
                 label: str = "consumer loop"):
        self._threshold = stall_threshold_s
        self._min_log_interval = min_log_interval_s
        self._clock = clock
        # Names the loop in the [stall] message ("consumer loop" for the
        # provider main loops, "capture callback" for the PortAudio callback).
        self._label = label
        self._last_time: Optional[float] = None
        self._last_log_time: Optional[float] = None
        self.stall_count = 0
        self.max_gap_ms = 0.0
        # The second window. Same stalls, different start and end; see the
        # class docstring for why the caller cannot difference the pair above.
        self.utterance_stalls = 0
        self.utterance_max_gap_ms = 0.0

    def record(
        self, queue_depth: Optional[int | Callable[[], int]] = None,
        busy_s: float = 0.0,
    ) -> Optional[str]:
        """Record one loop iteration; return a log message if a stall ended.

        Args:
            queue_depth: Current capture queue depth, included in the message
                so the log shows whether the stall was long enough to drop.
                May be a zero-argument callable; it is invoked only when a
                rate-limited stall message actually forms, so a caller on a
                real-time thread (the PortAudio callback) does not pay the
                queue-mutex acquisition on every frame
                (wh-sounddevice-starvation-parity.3.2).
            busy_s: Seconds of this gap the caller spent running its own
                work. The gap between two record() calls is a whole loop
                iteration, so on a loop that does synchronous work it holds
                that work as well as any time the thread was not scheduled.
                A caller that can measure its work passes it here and gets a
                stall figure that means what the message says; a caller that
                leaves it at zero keeps the plain wall-clock gap
                (wh-stt-load-metrics.1.12).

        Returns:
            A message describing the stall, or None (no stall, or rate-limited).
        """
        now = self._clock()
        last, self._last_time = self._last_time, now
        if last is None:
            return None

        # max(0.0, ...) because the work is timed on a different clock from
        # the gap; rounding between the two can put the work marginally past
        # the gap, and a negative gap would read as an enormous one.
        gap = max(0.0, (now - last) - busy_s)
        gap_ms = gap * 1000.0
        if gap_ms > self.max_gap_ms:
            self.max_gap_ms = gap_ms
        if gap_ms > self.utterance_max_gap_ms:
            self.utterance_max_gap_ms = gap_ms
        if gap < self._threshold:
            return None

        self.stall_count += 1
        self.utterance_stalls += 1
        if (self._last_log_time is not None
                and (now - self._last_log_time) < self._min_log_interval):
            return None
        self._last_log_time = now
        if callable(queue_depth):
            # The probe runs on the caller's (possibly real-time) thread; a
            # diagnostic read must never break the loop it observes
            # (wh-sounddevice-starvation-parity.3.3). On failure the message
            # still forms, just without the depth suffix.
            try:
                queue_depth = queue_depth()
            except Exception:
                queue_depth = None
        depth = "" if queue_depth is None else f"; capture queue depth now {queue_depth}"
        # The gap is the whole of the measurement, so the gap is the whole of
        # the message (wh-stt-load-metrics.3, criterion 4). It used to end
        # "(likely whole-machine CPU starvation)", which this class cannot
        # measure and which was wrong every time it was checked: whole-machine
        # CPU was 20-37% during the 09:13 stalls on 2026-09-03, and an
        # independent capture from the same microphone lost nothing over the
        # same minutes. A long gap has other explanations this class cannot
        # tell apart -- a blocking call inside the loop, a held GIL, a driver
        # -- so naming one of them sent the investigation the wrong way.
        return (f"{STALL_PREFIX}{self._label} made no progress for "
                f"{gap:.1f}s{depth}")

    def reset(self) -> None:
        """Forget the last iteration time after an intentional pause
        (for example a mic restart), so the pause is not counted as a stall.

        Deliberately leaves stall_count and max_gap_ms alone: those belong to
        the reporting window (see snapshot_and_reset_window), and stalls that
        happened before the pause are real evidence that must still appear in
        the next [overflow-diag] summary."""
        self._last_time = None

    def snapshot_and_reset_window(self) -> dict:
        """Return {'stalls', 'max_gap_ms'} for the window and start a new one.

        Deliberately leaves the utterance window alone. The periodic
        reporter's interval lands wherever it lands, including inside an
        utterance, and an utterance whose numbers were emptied mid-way
        would under-report the stall it exists to show.
        """
        snap = {"stalls": self.stall_count, "max_gap_ms": self.max_gap_ms}
        self.stall_count = 0
        self.max_gap_ms = 0.0
        return snap

    def snapshot_and_reset_utterance(self) -> dict:
        """Return {'stalls', 'max_gap_ms'} for the utterance and start a new one.

        Called at both ends of an utterance: at the start to open a window
        whose numbers describe only this utterance, and at the finish to
        read them. The reporting window above is untouched, so the periodic
        [overflow-diag] summary still counts every stall in its own interval
        (wh-stt-load-metrics.4).
        """
        snap = {"stalls": self.utterance_stalls,
                "max_gap_ms": self.utterance_max_gap_ms}
        self.utterance_stalls = 0
        self.utterance_max_gap_ms = 0.0
        return snap


# The capture counters the per-utterance line reports as a difference
# between the start and the end of an utterance. The zero baseline in
# AudioProcessor.seed_capture_baseline_before_capture_starts covers exactly
# these, so a counter added to the line without being added here would
# silently lose its baseline (wh-stt-load-metrics.1.10).
CAPTURE_DELTA_KEYS = ('overflow_count', 'status_flags', 'drops')

# What a capture counter reads when nothing measured it. The load-test
# parser reads this spelling back as "not measured" rather than as a number
# (tools/stt_load_test/logparse.py NOT_REPORTED).
NOT_REPORTED = 'n/a'


def capture_deltas(at_open: Optional[dict],
                   at_end: Optional[dict]) -> dict:
    """How far each capture counter moved across one utterance.

    Returns one entry per CAPTURE_DELTA_KEYS, each an int or the string
    "n/a". It lives here rather than inside AudioProcessor because two
    providers now report the same three counters and two copies of this
    rule would drift (wh-stt-load-metrics.4); AudioProcessor is where it
    was written and still its first caller.

    "n/a" rather than 0 wherever there is no answer. This whole measurement
    exists to decide whether capture loss happened, and "overflow=0" is the
    reading that rules it out -- a provider that never counted must not be
    able to produce that reading. The cases: the WinRT capture path has no
    PortAudio status flags and no overflow count, both providers return a
    four-key dict when no stream is open, and a reading may be missing at
    either end.
    """
    if at_open is None or at_end is None:
        return dict.fromkeys(CAPTURE_DELTA_KEYS, NOT_REPORTED)
    deltas = {}
    for key in CAPTURE_DELTA_KEYS:
        if key not in at_open or key not in at_end:
            deltas[key] = NOT_REPORTED
            continue
        before, after = at_open[key], at_end[key]
        if not isinstance(before, int) or not isinstance(after, int):
            deltas[key] = NOT_REPORTED
            continue
        # A provider restart mid-utterance is the only way this goes
        # negative; report 0 rather than a number that reads as an
        # impossible measurement.
        deltas[key] = max(0, after - before)
    return deltas


class IterationSegments:
    """Where one consumer-loop iteration spent its time.

    wh-stt-load-metrics.4, criterion G3. LoopStallTracker says a stall
    happened and how long it lasted; it cannot say where the time went,
    and the two candidate answers need opposite fixes. A loop blocked
    inside one call -- the send to the recognizer back-pressuring, a
    microphone read waiting on a device -- is a queue or a transport
    problem. A loop whose every call returned at once and which still
    lost nine seconds was not running: something else had the CPU.

    So this measures the parts and compares their sum against the whole.
    What is left over is reported as "unaccounted", and it competes with
    the named segments to be the worst -- it is the descheduling answer,
    and it is the one a reader most needs to see win.

    The loop reports the iteration that just ENDED: at the top of an
    iteration the accumulators still hold the previous one's work, which
    is the one the stall tracker just measured a gap across. start()
    closes that reporting and opens the next.

    Nothing here can stop the loop it watches. An unknown segment name is
    dropped rather than raising, a report before the first start() still
    answers, and a body that raises inside timing() keeps its measurement
    and re-raises.
    """

    #: Every part of one Google consumer-loop iteration that can block.
    #: Fixed rather than discovered, so a typo in a caller cannot add a
    #: field to the line and a segment that did nothing still prints 0.0.
    NAMES = ('responses', 'mic_read', 'vad', 'agc', 'send')

    #: The one segment that is a wait rather than work. A loop starved of
    #: CPU sits in the capture read, so those seconds ARE the stall signal
    #: and charging them to the loop's own work would erase it -- the same
    #: exclusion the Parakeet loop makes by starting its counter after the
    #: read returns (wh-stt-load-metrics.1.14).
    WAIT_NAME = 'mic_read'

    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._ms = dict.fromkeys(self.NAMES, 0.0)
        self._started: Optional[float] = None
        self._work_s = 0.0

    def start(self) -> None:
        """Begin an iteration, forgetting the one just reported."""
        for name in self.NAMES:
            self._ms[name] = 0.0
        self._started = self._clock()

    def add(self, name: str, ms: float) -> None:
        """Add one measurement to this iteration's segment."""
        if name not in self._ms:
            return
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        # perf_counter is monotonic, so a negative figure means the
        # caller passed the wrong thing; a NaN would print and would
        # poison the comparison that names the worst segment.
        if not math.isfinite(value) or value < 0.0:
            return
        self._ms[name] += value
        # wh-stt-load-metrics.4 G1a. Kept here rather than in the caller
        # because timing() records through this method too, so one site
        # covers both ways in and the checks above guard the total for
        # free: a rejected measurement is time the loop cannot prove it
        # spent, and subtracting it from a gap would hide a real stall.
        if name != self.WAIT_NAME:
            self._work_s += value / 1000.0

    @contextlib.contextmanager
    def timing(self, name: str):
        """Time a block, keeping the measurement even when it raises.

        The send to the recognizer raises when the stream dies, and that
        is exactly the iteration whose timing matters most.
        """
        started = self._clock()
        try:
            yield
        finally:
            self.add(name, (self._clock() - started) * 1000.0)

    @property
    def work_seconds(self) -> float:
        """Seconds this loop has spent inside its own timed calls.

        wh-stt-load-metrics.4 G1a. CaptureLoadReporter passes this to
        LoopStallTracker.record as busy_s, where the gap becomes
        (now - last) - busy_s, so this figure decides what counts as a
        stall. It is the SUM OF THE MEASURED SPANS, not the iteration's
        wall time minus the capture wait: wall time includes the
        unaccounted figure this class reports, unaccounted IS the
        descheduling, and subtracting it would drive every stall to zero
        -- the [stall] line would go quiet exactly when the machine was
        worst.

        Cumulative for the life of the loop, because the reporter
        differences it across iterations (_read_busy). A figure reset
        each iteration would charge its whole history to every gap.
        """
        return self._work_s

    def report(self) -> str:
        """The line for the iteration that just ended."""
        total = sum(self._ms.values())
        if self._started is None:
            iter_ms = total
        else:
            iter_ms = max(0.0, (self._clock() - self._started) * 1000.0)
        unaccounted = max(0.0, iter_ms - total)
        worst = max(
            list(self._ms.items()) + [('unaccounted', unaccounted)],
            key=lambda pair: pair[1])[0]
        parts = ' '.join(f'{name}={self._ms[name]:.1f}' for name in self.NAMES)
        return (f'[stall-where] iter={iter_ms:.1f} {parts} '
                f'unaccounted={unaccounted:.1f} worst={worst}')

class UtteranceLoadMetrics:
    """The per-utterance load line for a provider with no local recognizer.

    wh-stt-load-metrics.4. AudioProcessor writes this line for the local
    models, built from its own engine timings. The Google provider has no
    AudioProcessor and no local engine: Google runs the recognizer on its
    own machines, so the five engine fields read "n/a" here. Printing 0
    would make a Google run read as "the recognizer kept up", which nothing
    measured, and it is the reading that rules inference lag out.

    The field names and their order are AudioProcessor's, so one parser
    reads both providers, with stalls= and stall_max_ms= inserted after
    q_n. Those two are the ones a Google run actually needs: its consumer
    loop is where the 2026-09-05 stalls appeared, and until now the only
    stall numbers in the log belonged to a 30-second reporting window that
    no utterance lines up with.

    Everything it reports is already being measured. The capture counters
    are the provider's own, differenced across the utterance; the queue
    depths are the samples the loop already takes to feed the stall
    tracker, handed here rather than read a second time; the stall numbers
    come from that same tracker's utterance window.

    This class only observes, and it must never be able to stop the loop it
    watches: every reader failure costs its own fields and nothing else
    (the rule CaptureLoadReporter and the stall tracker's depth probe
    already follow).
    """

    def __init__(self, capture_stats: Optional[Callable[[], dict]] = None,
                 stall_tracker: Optional[LoopStallTracker] = None):
        self._capture_stats = capture_stats
        self._stall_tracker = stall_tracker
        self._at_open: Optional[dict] = None
        self._q_max = 0
        self._q_sum = 0
        self._q_samples = 0

    def _read(self) -> Optional[dict]:
        if self._capture_stats is None:
            return None
        try:
            stats = self._capture_stats()
        except Exception:
            return None
        return stats if isinstance(stats, dict) else None

    def start(self) -> None:
        """Open the window: read the baseline and forget the last utterance."""
        self._at_open = self._read()
        self._q_max = 0
        self._q_sum = 0
        self._q_samples = 0
        if self._stall_tracker is not None:
            # Discards whatever the tracker's utterance window held. What
            # it held is the stalls of the silence before this utterance,
            # which belong to no utterance and are already counted in the
            # periodic window.
            self._stall_tracker.snapshot_and_reset_utterance()

    def sample(self, queue_depth) -> None:
        """Record one capture-queue depth the loop has already read."""
        if not isinstance(queue_depth, int) or isinstance(queue_depth, bool):
            return
        self._q_samples += 1
        self._q_sum += queue_depth
        if queue_depth > self._q_max:
            self._q_max = queue_depth

    def finish(self, utt: int, kind: str) -> str:
        """The line for the utterance that just ended, and start the next.

        Answers even when start() was never called -- a stale finalization,
        or the first utterance after a restart. The capture fields then read
        n/a, which is the truth, and the loop gets a line instead of a
        traceback out of a diagnostic.
        """
        deltas = capture_deltas(self._at_open, self._read())
        q_mean = self._q_sum / self._q_samples if self._q_samples else 0.0
        if self._stall_tracker is None:
            stalls = NOT_REPORTED
            stall_max = NOT_REPORTED
        else:
            snap = self._stall_tracker.snapshot_and_reset_utterance()
            stalls = snap["stalls"]
            stall_max = f'{snap["max_gap_ms"]:.1f}'
        line = (
            f'[load-diag] utt={utt} kind={kind} '
            f'overflow={deltas["overflow_count"]} '
            f'status_flags={deltas["status_flags"]} '
            f'drops={deltas["drops"]} '
            f'q_max={self._q_max} q_mean={q_mean:.1f} q_n={self._q_samples} '
            f'stalls={stalls} stall_max_ms={stall_max} '
            f'engine_calls={NOT_REPORTED} '
            f'engine_ms_total={NOT_REPORTED} '
            f'engine_ms_max={NOT_REPORTED} '
            f'audio_ms={NOT_REPORTED} '
            f'engine_ratio={NOT_REPORTED}')
        self._at_open = None
        self._q_max = 0
        self._q_sum = 0
        self._q_samples = 0
        return line


# The share of a window that may pass with no frame before its capture
# numbers stop describing the window they are printed for
# (wh-stt-load-metrics.1.16).
_OUTAGE_WINDOW_FRACTION = 0.5


class CaptureLoadReporter:
    """Periodic capture-load summary for a provider's audio consumer loop.

    wh-stt-load-metrics. Transcription degrades when the CPU is very busy for
    two different reasons that need two different fixes: the capture callback
    loses input frames (the words come out wrong), or the recognizer falls
    behind (the words come out late but correct). AudioProcessor reports the
    per-utterance half of that; this reports the half that is not tied to an
    utterance, so a machine that is struggling shows it even while nobody is
    speaking.

    Call record_iteration() once per consumer-loop iteration and log whatever
    it returns. It returns a list because one iteration can produce more
    than one line -- a stall message, a periodic summary, and the record of a
    capture outage that just ended -- and an empty list on the ordinary
    iteration, which is almost all of them.

    Every number in the summary belongs to the window it reports: the queue
    high-water mark restarts each window, and the capture counters are
    differences, because the provider's own counters run for the life of the
    process. A window that reprinted lifetime totals would make one early
    spike look permanent.

    The default interval matches OverflowMonitor's own summary interval so the
    two read together in the log.

    This class only observes; it must never be able to stop the loop it
    watches. A capture-stats reader that raises costs the window its capture
    numbers and nothing else (wh-sounddevice-starvation-parity.3.3 applies the
    same rule to the stall tracker's depth probe).
    """

    def __init__(self, capture_stats: Optional[Callable[[], dict]] = None,
                 interval_s: float = 10.0, clock=time.monotonic,
                 stall_threshold_s: float = 1.0,
                 label: str = "consumer loop",
                 busy_seconds: Optional[Callable[[], float]] = None,
                 capture_ready: Optional[Callable[[], bool]] = None):
        self._capture_stats = capture_stats
        self._interval = interval_s
        self._clock = clock
        # Cumulative seconds the observed loop has spent in its own
        # synchronous work, read once per iteration and differenced. It must
        # be cumulative for the life of the loop: a counter that restarts
        # reads as negative work, and clamping that at zero charges the whole
        # next gap to starvation (wh-stt-load-metrics.1.12).
        self._busy_seconds = busy_seconds
        self._busy_at_last: Optional[float] = None
        # Whether the capture source is running. A provider whose device
        # failed to open still answers get_stats() with real zeros, and zeros
        # are indistinguishable from a clean window unless the provider is
        # asked (wh-stt-load-metrics.1.13). True until a reading says
        # otherwise; it tracks the whole window, because the capture counters
        # are differences across it and a baseline read from a dead provider
        # poisons the window even if the device opens later in it.
        self._capture_ready = capture_ready
        self._ready_all_window = True
        self.stall_tracker = LoopStallTracker(
            stall_threshold_s=stall_threshold_s, clock=clock, label=label)
        self._window_start: Optional[float] = None
        self._at_window_start: Optional[dict] = None
        self._q_max = 0
        self._q_now = 0
        # The provider's frame counter, watched on every iteration rather
        # than only at the two ends of a window (wh-stt-load-metrics.1.16).
        # _frames_progress_at is the last clock reading at which it moved,
        # and it deliberately survives a window boundary: a span that
        # straddles one is still frozen after it, and restarting the reading
        # there would read the boundary itself as the arrival of a frame.
        # What that span MEASURES is clipped to the reporting window
        # (wh-stt-load-metrics.1.18) -- see _track_frame_progress.
        self._frames_at_last: Optional[int] = None
        self._frames_progress_at: Optional[float] = None
        self._max_frame_gap_s = 0.0
        self._frames_measured = False
        # A run of consecutive windows that all measured no capture
        # (wh-whole-outage-metric). These deliberately survive the window
        # boundary reset in record_iteration, which is the whole point of
        # the record: _outage_start is the start of the first marked window
        # in the run and _outage_end the close of the last, so the event
        # describes only seconds a window really measured.
        self._outage_start: Optional[float] = None
        self._outage_end: Optional[float] = None
        self._outage_windows = 0
        # Counted here, reported when the window closes. The rate bound
        # is that boundary and nothing new was added for it: the
        # Parakeet consumer loop calls record_iteration every iteration
        # and polls with a 20 ms timeout (main.py, process_audio_loop),
        # so _read is asked 50 or more times a second, and a line per
        # failure would bury the band it exists to explain
        # (wh-capture-load-gaps).
        self._unreadable_counts: dict = {}
        self._unreadable_detail: Optional[str] = None
        # Set once the "no reader" case has been reported. Same reason as
        # in shared_stt.audio_processor: self._capture_stats is bound in
        # __init__ and never rebound, so it is said once per reporter and
        # never counted again (wh-capture-load-gaps.1.1).
        self._no_reader_reported = False

    def _read(self) -> Optional[dict]:
        """The provider's stats, or None when they cannot be read.

        This is the reader behind the window lines and the capture-outage
        event, so a window that says capture=unavailable says so because
        of one of the three None returns below. Each names itself now: the
        missing reader and the non-dict answer were silent, and the
        exception logged at debug on this module's logger, which is below
        the level the provider forwards -- so the 2026-08-30 loaded run
        lost capture readings in 13 of 83 windows with no cause anywhere
        in wheelhouse.log (wh-capture-load-gaps).
        """
        if self._capture_stats is None:
            self._note_unreadable("no-reader")
            return None
        try:
            stats = self._capture_stats()
        except Exception as e:
            self._note_unreadable("reader-raised",
                                  f"{type(e).__name__}: {e}")
            return None
        if not isinstance(stats, dict):
            self._note_unreadable("non-dict", type(stats).__name__)
            return None
        return stats

    def _note_unreadable(self, reason: str,
                         detail: Optional[str] = None) -> None:
        """Count one unreadable reading; the window boundary reports it.

        "no-reader" is counted once per reporter and then ignored. It is
        a fact about how the reporter was built rather than a reading
        that failed -- self._capture_stats is bound in __init__ and never
        rebound -- so counting it per reading would report the polling
        rate, about 50 a second, as a count of failures, and would put a
        line in every window for the life of the process. The other two
        reasons are real failures that can start and stop, so they are
        counted every time (wh-capture-load-gaps.1.1).

        Args:
            reason: which None case fired -- "no-reader", "reader-raised"
                or "non-dict".
            detail: the exception text, or the type that was not a dict.
                The newest one wins; only one is carried, because the
                line points at where to look next rather than recording
                every failure.
        """
        if reason == "no-reader":
            if self._no_reader_reported:
                return
            self._no_reader_reported = True
        self._unreadable_counts[reason] = (
            self._unreadable_counts.get(reason, 0) + 1)
        if detail is not None:
            self._unreadable_detail = detail

    def _unreadable_line(self) -> Optional[str]:
        """Why this window's capture readings were missing, or None.

        Returned rather than logged, like every other line this class
        produces: the caller logs them on the provider's own logger,
        which is the route the window lines already take to
        wheelhouse.log. A reason logged here instead would have to pick a
        logger, and picking this module's is exactly the mistake being
        fixed.

        The counts are cleared as the line is built, so each window
        reports what it saw rather than a lifetime total -- the same
        distinction the capture deltas make against their baseline.

        The prefix is not the per-utterance "[load-diag] " one: the three
        regexes in tools/stt_load_test/logparse.py match that literal
        including its trailing space, and the test helpers assert a run
        carries exactly one window line.
        """
        if not self._unreadable_counts:
            return None
        counts = " ".join(f"{name}={count}" for name, count
                          in sorted(self._unreadable_counts.items()))
        suffix = (f" (last: {self._unreadable_detail})"
                  if self._unreadable_detail else "")
        self._unreadable_counts = {}
        self._unreadable_detail = None
        return f"[load-diag-capture] unavailable: {counts}{suffix}"

    def _read_busy(self) -> float:
        """Seconds of work since the previous iteration, as a difference.

        Returns 0.0 when no reader was given, which is the behaviour every
        caller had before this existed.

        A reader that raises leaves the gap unmeasurable: the loop's work and
        a scheduler stall are the same wall-clock seconds, and telling them
        apart is the entire point of the field. Rather than name one without
        knowing, the tracker's timeline is restarted so this gap is skipped
        (wh-stt-load-metrics.1.12). Like the capture-stats reader, this can
        cost the reading and nothing else -- it must never stop the loop it
        observes.
        """
        if self._busy_seconds is None:
            return 0.0
        try:
            busy_now = float(self._busy_seconds())
        except Exception as e:
            logger.debug("[load-diag] work time unavailable: %s", e)
            self.stall_tracker.reset()
            self._busy_at_last = None
            return 0.0
        # The first reading opens the difference; the reader is cumulative for
        # the loop's life, so charging its whole history to the first gap
        # would silence a real stall at startup.
        busy_s = (0.0 if self._busy_at_last is None
                  else max(0.0, busy_now - self._busy_at_last))
        self._busy_at_last = busy_now
        return busy_s

    def _read_ready(self) -> bool:
        """Whether capture is running, as far as this reporter can tell.

        Returns True when no reader was given, which is the behaviour every
        caller had before this existed.

        A reader that raises answers False, not True. Not knowing whether the
        microphone works is not the same as knowing it does, and reporting
        drops=0 for a source that never captured anything is the defect this
        guards (wh-stt-load-metrics.1.13). Like the other two readers, it can
        cost the reading and nothing else.
        """
        if self._capture_ready is None:
            return True
        try:
            return bool(self._capture_ready())
        except Exception as e:
            logger.debug("[load-diag] capture readiness unavailable: %s", e)
            return False

    @staticmethod
    def _frames_arrived(before: dict, after: dict) -> bool:
        """Whether the provider's own frame counter moved across the window.

        The readiness reader now does take its answer back when the
        capture path dies (wh-stt-load-metrics.2), so it is no longer
        the only thing this counter compensates for. What it still
        cannot see is a provider that is alive by every measure it has
        and delivering nothing: PortAudio keeps calling a stream active
        and the WinRT thread keeps polling while a muted or stalled
        device returns silence. This counter is measured rather than
        asserted, so it can say what the handshake cannot
        (wh-stt-load-metrics.1.15).

        True when the provider does not report the counter at all: not knowing
        is not evidence of death, and marking every window of such a provider
        would blank numbers it really measured. The one production capture
        reports it: WinRTAudioCapture counts every polled frame, silence
        included. MicrophoneStream counted every PortAudio callback frame
        and is deleted (wh-portaudio-capture-removal).

        A counter that went BACKWARDS is a provider restart inside the window,
        which leaves no honest reading either: the frames the old counter held
        and the ones the new counter holds cannot be differenced.
        """
        a, b = before.get('captured'), after.get('captured')
        if not isinstance(a, int) or not isinstance(b, int):
            return True
        return b > a

    def _track_frame_progress(self, now: float,
                              stats: Optional[dict]) -> None:
        """Watch the provider's frame counter on every iteration.

        Comparing the counter only at the two ends of a window proves that
        SOME frame arrived, not that frames kept arriving. One frame just
        after a window opens, followed by a microphone that dies, leaves the
        endpoint difference positive while nearly the whole window holds no
        audio, and the window went back to printing drops=0 overflow=0 for it
        (wh-stt-load-metrics.1.16). Read every iteration, the counter gives
        the length of the longest span in which it did not move, which is the
        size of the outage rather than a guess about it.

        The span is measured on EVERY reading, the reading that sees the
        count move included (wh-stt-load-metrics.1.20). A reading is the only
        moment this loop learns anything about capture: between two of them
        it knows nothing. One long recognizer call is enough -- the loop
        reads the count, spends ten seconds inside process_chunk, and reads
        it again on the other side. Those seconds are charged to the loop's
        own work, so they are not a stall, and a count that moved across them
        proves one frame somewhere in there and nothing about the rest. A
        reading that ends such a span must charge it, or the window reports
        ten unwatched seconds as measured capture.

        A reading the provider cannot give restarts the timeline instead of
        charging its seconds to the microphone. Not knowing is not evidence
        of an outage -- the same rule _frames_arrived applies at the ends.

        Any CHANGE counts as the provider still working, backwards included.
        A counter that went backwards is a restart inside the window, which
        _frames_arrived marks on its own; here it means only that this
        reading is not silence.

        The span is measured from the later of the last arrival and the start
        of this window (wh-stt-load-metrics.1.18). The reading survives the
        boundary so the span stays frozen across it, but the seconds before
        the boundary belong to the window that already reported them: charging
        them again here would compare another window's silence against this
        window's bound and blank capture numbers that describe most of it.
        """
        captured = None if stats is None else stats.get('captured')
        if not isinstance(captured, int):
            self._frames_at_last = None
            self._frames_progress_at = None
            return
        self._frames_measured = True
        if self._frames_at_last is None:
            self._frames_at_last = captured
            self._frames_progress_at = now
            return
        gap_start = self._frames_progress_at
        if (self._window_start is not None
                and gap_start < self._window_start):
            gap_start = self._window_start
        gap = now - gap_start
        if gap > self._max_frame_gap_s:
            self._max_frame_gap_s = gap
        if captured != self._frames_at_last:
            self._frames_at_last = captured
            self._frames_progress_at = now

    def _capture_interrupted(self) -> bool:
        """Whether this window held a no-frame span too long to measure past.

        The bound is a fraction of the window rather than a fixed number of
        seconds, so it scales with the interval the caller chose, and it is
        deliberately generous. Marking a window blanks its capture numbers,
        and under the very load this procedure creates a short span with no
        frame is expected -- blanking there would delete the overflow reading
        that says what actually happened. A span longer than half the window
        is the other case: the numbers then describe less than half the time
        they claim to cover.

        The span this reads is the window's own, not one carried in from the
        window before it (wh-stt-load-metrics.1.18), so the fraction compares
        two lengths measured over the same seconds.
        """
        if not self._frames_measured:
            return False
        return self._max_frame_gap_s > self._interval * _OUTAGE_WINDOW_FRACTION

    def _capture_unavailable(self, before: dict, after: dict) -> bool:
        """Whether this window measured no microphone at all.

        Three ways to know, and the window's own line and the cross-window
        outage record read the same one, so the two cannot drift apart
        (wh-whole-outage-metric): the provider said it was not running at
        some point inside the window, its frame counter did not move across
        the window at all, or the counter stopped moving for longer than this
        window can be measured past.

        The first catches a backend that reports its own death. Since
        wh-stt-load-metrics.2 the capture takes the readiness answer back
        when capture ends: WinRTAudioCapture answers False once its setup
        wait expires, once the capture thread has ended, or once
        POLL_FAILURES_BEFORE_DEAD consecutive frame polls have raised. So
        a microphone that opened and then died already marks its own
        window through this condition. The earlier text here called that
        answer sticky, which the readiness work made false
        (wh-stt-load-metrics.2.1.10), then named the wrong class, and then
        listed only two of the three ways readiness says no. It also
        described a second backend: SounddeviceAudioCapture.wait_ready
        read MicrophoneStream.is_active on every probe, and that property
        asked PortAudio rather than returning a flag recorded at start-up.
        Both classes are deleted (wh-portaudio-capture-removal), so WinRT
        is the whole of this paragraph now.

        The second catches what the first still cannot: a capture source
        that is alive by every measure its provider has, and delivers no
        frame at all (wh-stt-load-metrics.1.15). The third catches what the
        second cannot: one frame at either end makes the endpoint difference
        positive however long the silence between them is
        (wh-stt-load-metrics.1.16).
        """
        return (not self._ready_all_window
                or not self._frames_arrived(before, after)
                or self._capture_interrupted())

    @property
    def last_queue_depth(self) -> int:
        """The capture-queue depth this reporter last read.

        wh-stt-load-metrics.4 G1a. The Google loop reports the queue in
        two places -- this reporter's window line and its own
        per-utterance load line -- and a second read would give them two
        different depths for one iteration, which they would disagree
        about most under exactly the load the lines exist to measure.
        Publishing the reading keeps one number for both.

        Zero before the first successful reading, and unchanged when a
        reading fails, so the utterance line gets whatever the stall
        tracker was given for the same iteration.
        """
        return self._q_now

    def record_iteration(self) -> list:
        """Record one loop iteration; return log-ready lines (usually none)."""
        now = self._clock()
        lines = []

        stats = self._read()
        ready_now = self._read_ready()
        self._ready_all_window = self._ready_all_window and ready_now
        self._track_frame_progress(now, stats)
        if stats is not None:
            depth = stats.get('qsize')
            if isinstance(depth, int):
                self._q_now = depth
                if depth > self._q_max:
                    self._q_max = depth

        stall_msg = self.stall_tracker.record(self._q_now,
                                              busy_s=self._read_busy())
        if stall_msg:
            lines.append(stall_msg)

        if self._window_start is None:
            # First iteration: open the window, say nothing about a window
            # that has not happened yet.
            self._window_start = now
            self._at_window_start = stats
            return lines

        if now - self._window_start < self._interval:
            return lines

        # The stall tracker's window closes at every elapsed boundary, not
        # only at the boundaries that can print a line. A window whose
        # capture reading was missing still happened, and its stalls belong
        # to it: banking them for the next window that CAN print makes that
        # later window report starvation it did not have
        # (wh-stt-load-metrics.1.7). The queue high-water mark below is reset
        # unconditionally for the same reason.
        stall_snap = self.stall_tracker.snapshot_and_reset_window()
        # The outage record is read before the window's own line and appended
        # before it: the outage ended before the window that reports the
        # recovery, and the log reads in the order the two things happened.
        outage = self._track_outage(now, stats)
        if outage:
            lines.append(outage)
        # Before the window's own line, so a reader meets the cause
        # first. Usually there IS no window line to come: _summary
        # needs a reading at each end of the window, so a failure at
        # either end silences it and this is the only record of that
        # window. A failure between the two ends leaves the window
        # line in place and this line above it.
        reason = self._unreadable_line()
        if reason:
            lines.append(reason)
        summary = self._summary(now, stats, stall_snap)
        if summary:
            lines.append(summary)

        # Each reading belongs to exactly one window: the reading that closed
        # this window was counted in its high-water mark, so the next window
        # starts from nothing rather than inheriting it. A window that
        # inherited the previous mark would keep reprinting one early spike.
        self._window_start = now
        self._at_window_start = stats
        self._q_max = 0
        self._ready_all_window = ready_now
        self._max_frame_gap_s = 0.0
        self._frames_measured = False
        return lines

    def _track_outage(self, now: float,
                      stats: Optional[dict]) -> Optional[str]:
        """Extend or end the run of windows that measured no capture.

        wh-whole-outage-metric. One outage spanning several windows printed
        one marked line per window and nothing that said where it began,
        where it ended, or how long it ran, so an operator had to count the
        lines and multiply -- which a run of unequal windows does not allow.

        The event is emitted when the outage ENDS, which is what makes it one
        event rather than one per window. A window whose capture reading is
        missing ends the run rather than spanning it: not knowing is not
        evidence of an outage and not evidence of recovery, and an event that
        spanned such a window would claim seconds nothing measured. That is
        the rule _track_frame_progress already applies to a reading the
        provider cannot give.

        Called before the window boundary resets, so self._window_start is
        still the start of the window being reported.

        crewcut: an outage still running when the process exits is therefore
        never reported as an event, and its per-window lines are the whole of
        what the operator gets for it. To remove the limit, give the consumer
        loop a shutdown hook that calls _close_outage once and logs what it
        returns.
        """
        before, after = self._at_window_start, stats
        # crewcut: _capture_unavailable answers True for a frame counter that
        # went BACKWARDS, which _frames_arrived's own docstring calls a
        # reading nobody can trust rather than a measured outage. This run
        # therefore counts such a window, and an event could in principle
        # claim seconds in which a restarted counter really did deliver
        # frames. The limit is kept on purpose: the event must agree with the
        # per-window capture=unavailable marker wh-stt-load-metrics.1.15 puts
        # on that same window, and asking a second question here is what
        # would let the two drift apart. No shipped path reaches it
        # (wh-whole-outage-metric.1.1, codex round 2, ruled not a defect):
        # both providers set _frames_captured only in __init__,
        # SoundDeviceAudioCapture never clears _stream so get_stats cannot
        # fall back to its zero dict, and self.audio_capture is assigned once
        # and never rebound. To remove the limit -- if a capture object ever
        # gains a second lifecycle -- reconcile the per-window line and this
        # event in ONE change, never either alone.
        if (before is not None and after is not None
                and self._capture_unavailable(before, after)):
            if self._outage_start is None:
                self._outage_start = self._window_start
                self._outage_windows = 0
            self._outage_end = now
            self._outage_windows += 1
            return None
        return self._close_outage(now)

    def _close_outage(self, now: float) -> Optional[str]:
        """The event line for a finished outage, or None when none is open.

        The two times are counted back from this moment because the clock is
        monotonic: its origin means nothing to a reader, while the log line
        this returns carries a wall-clock timestamp the reader can subtract
        them from. Injecting a second clock to print an absolute time would
        add a dependency for a number the log already carries.
        """
        if self._outage_start is None or self._outage_end is None:
            return None
        line = (
            f"[load-diag] capture-outage "
            f"start_s_ago={now - self._outage_start:.0f} "
            f"end_s_ago={now - self._outage_end:.0f} "
            f"duration_s={self._outage_end - self._outage_start:.0f} "
            f"windows={self._outage_windows}"
        )
        self._outage_start = None
        self._outage_end = None
        self._outage_windows = 0
        return line

    def _summary(self, now: float, stats: Optional[dict],
                 stall_snap: dict) -> Optional[str]:
        """The window's line, or None when the capture numbers are unknown.

        The caller closes the stall tracker's window and passes the result
        in, so returning None here cannot leave those counters running into
        the next window (wh-stt-load-metrics.1.7).
        """
        before, after = self._at_window_start, stats
        if before is None or after is None:
            return None

        gap_ms = (f"{self._max_frame_gap_s * 1000:.0f}"
                  if self._frames_measured else 'n/a')

        if self._capture_unavailable(before, after):
            # The three conditions live in _capture_unavailable, which the
            # cross-window outage record reads too, so a window marked here
            # and a window counted into an outage are the same window.
            #
            # Every capture field here would be a difference between two
            # readings of a provider that captured nothing, so all of them say
            # so. The loop numbers stay: the loop really did run, and its
            # stalls are the whole reason the operator turned this line on.
            return (
                f"[load-diag] window={now - self._window_start:.0f}s "
                f"capture=unavailable q_now=n/a q_max=n/a "
                f"drops=n/a overflow=n/a status_flags=n/a "
                f"stalls={stall_snap['stalls']} "
                f"max_gap_ms={stall_snap['max_gap_ms']:.0f} "
                f"max_frame_gap_ms={gap_ms}"
            )

        def delta(key):
            if key not in before or key not in after:
                # Not measured by this provider at all -- see the same guard
                # in AudioProcessor._log_load_metrics. Zero would read as
                # "measured, none happened", which is a different fact.
                return 'n/a'
            a, b = before[key], after[key]
            if not isinstance(a, int) or not isinstance(b, int):
                return 'n/a'
            # A provider restart inside the window is the only way this goes
            # negative; report 0 rather than an impossible measurement.
            return max(0, b - a)

        return (
            f"[load-diag] window={now - self._window_start:.0f}s "
            f"q_now={self._q_now} q_max={self._q_max} "
            f"drops={delta('drops')} overflow={delta('overflow_count')} "
            f"status_flags={delta('status_flags')} "
            f"stalls={stall_snap['stalls']} "
            f"max_gap_ms={stall_snap['max_gap_ms']:.0f} "
            f"max_frame_gap_ms={gap_ms}"
        )


def run_mic_check(
    mic: AudioProviderProtocol,
    duration_seconds: float = 5.0,
    rate: int = 16000,
    chunk_ms: int = 20,
    write_wav_path: Optional[str] = None,
    device_index: Optional[int] = None
) -> int:
    """Runs a diagnostic test on the audio provider, printing stats and optionally saving a WAV file.

    Args:
        mic: Audio provider instance (must implement start/stop/read)
        duration_seconds: How long to run the test
        rate: Sample rate in Hz
        chunk_ms: Chunk duration in milliseconds
        write_wav_path: Optional path to write WAV file for debugging
        device_index: Device index for logging (informational only)

    Returns:
        0 on success, 2 if no frames captured
    """
    mic.start()
    expected_samples = int(rate * chunk_ms / 1000)
    expected_bytes = expected_samples * 2  # int16 mono (2 bytes per sample)
    frames = []
    zero_frames = 0
    total_frames = 0
    start = time.time()
    last_log = start
    rms = 0.0

    logger.info(
        f"[mic-check] Starting diagnostics for {duration_seconds:.2f}s | rate={rate} "
        f"chunk_ms={chunk_ms} expected_frame_bytes={expected_bytes} device_index={device_index}"
    )

    while (time.time() - start) < duration_seconds:
        frame = mic.read(timeout=0.5)
        now = time.time()
        if frame is None:
            continue
        total_frames += 1
        if len(frame) != expected_bytes:
            logger.warning(f"[mic-check] Unexpected frame size {len(frame)} (expected {expected_bytes})")
        arr = array.array('h')
        arr.frombytes(frame)
        if not arr:
            zero_frames += 1
            rms = 0.0
        else:
            s = 0
            z = True
            for sample in arr:
                if sample != 0:
                    z = False
                s += sample * sample
            if z:
                zero_frames += 1
            rms = math.sqrt(s / len(arr)) if arr else 0.0
        if (now - last_log) >= 1.0:
            silence_ratio = (zero_frames / total_frames) if total_frames else 0.0
            logger.info(
                f"[mic-check] frames={total_frames} silence_frames={zero_frames} "
                f"silence_ratio={silence_ratio:.2%} last_rms={rms:.1f}"
            )
            last_log = now
        if write_wav_path:
            frames.append(frame)

    duration = time.time() - start
    silence_ratio = (zero_frames / total_frames) if total_frames else 0.0
    logger.info(
        f"[mic-check][DONE] duration={duration:.2f}s frames={total_frames} frame_ms={chunk_ms} "
        f"silence_ratio={silence_ratio:.2%}"
    )

    if write_wav_path and frames:
        try:
            with wave.open(write_wav_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(b''.join(frames))
            logger.info(f"[mic-check] Wrote WAV: {write_wav_path}")
        except Exception as e:
            logger.warning(f"[mic-check] Failed to write WAV: {e}")

    mic.stop()

    if total_frames == 0:
        logger.error("[mic-check][RESULT] FAIL: No frames captured. Check device index / permissions.")
        return 2
    if silence_ratio > 0.95:
        logger.warning("[mic-check][RESULT] WARN: >95% frames are digital silence (mic muted / wrong source?)")
    else:
        logger.info("[mic-check][RESULT] PASS: Audio frames captured.")
    return 0
