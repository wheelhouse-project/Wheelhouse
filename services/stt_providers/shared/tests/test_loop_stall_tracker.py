"""Tests for shared_audio.diagnostics.LoopStallTracker.

The tracker makes consumer-loop starvation visible: when the per-frame loop
stops being scheduled for more than the threshold, the next iteration gets a
log-ready message. Overflow bursts previously had no direct signature in the
logs -- only the queue-full drops they caused.

TestLoopStallTracker and TestUtteranceWindow are the tracker's own unit
tests and predate the PortAudio removal. They import nothing but
LoopStallTracker, so the removal never reached them.

TestLoopStallTrackerLazyDepth and TestLoopStallTrackerLabel came from
tests/test_microphone_starvation_defense.py (wh-sounddevice-starvation-parity.3.2
and .3.3), whose other eighteen tests drove MicrophoneStream._callback. That
class was deleted with the PortAudio capture path
(wh-portaudio-capture-removal), and these six never touched it. They cover a
tracker that sits in a loop that must not pay for a diagnostic it does not
print: reading the capture queue depth costs a mutex acquisition, so the depth
callable is invoked only when a rate-limited stall message actually forms, and
a callable that raises is swallowed rather than allowed to break the loop it
observes. Both surviving callers -- google_stt_server's consumer loop and the
WinRT poll loop -- use the tracker unchanged.
"""
from unittest.mock import Mock

from shared_audio.diagnostics import LoopStallTracker


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestLoopStallTracker:
    def test_first_record_returns_none(self):
        clock = FakeClock()
        tracker = LoopStallTracker(clock=clock)
        assert tracker.record() is None

    def test_normal_cadence_returns_none(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        for _ in range(100):
            clock.advance(0.03)
            assert tracker.record() is None
        assert tracker.stall_count == 0

    def test_stall_returns_message_with_gap_and_depth(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.5)
        msg = tracker.record(queue_depth=83)
        assert msg is not None
        assert "2.5s" in msg
        assert "83" in msg
        assert tracker.stall_count == 1

    def test_stall_message_rate_limited_but_still_counted(self):
        clock = FakeClock()
        tracker = LoopStallTracker(
            stall_threshold_s=1.0, min_log_interval_s=5.0, clock=clock
        )
        tracker.record()
        clock.advance(2.0)
        assert tracker.record() is not None  # first stall logs
        clock.advance(2.0)
        assert tracker.record() is None  # second stall inside 5s window: no log
        assert tracker.stall_count == 2  # ...but still counted
        clock.advance(6.0)
        assert tracker.record() is not None  # rate-limit window passed
        assert tracker.stall_count == 3

    def test_reset_forgets_last_time(self):
        """After an intentional pause (mic restart), reset() prevents a false stall."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        tracker.reset()
        clock.advance(10.0)
        assert tracker.record() is None  # gap after reset is not a stall
        assert tracker.stall_count == 0

    def test_window_snapshot_and_reset(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record()
        clock.advance(0.03)
        tracker.record()

        snap = tracker.snapshot_and_reset_window()
        assert snap["stalls"] == 1
        assert snap["max_gap_ms"] == 3000.0

        snap2 = tracker.snapshot_and_reset_window()
        assert snap2["stalls"] == 0
        assert snap2["max_gap_ms"] == 0.0

    def test_max_gap_tracks_subthreshold_gaps(self):
        """max_gap_ms reflects the worst gap even when below the stall threshold."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(0.4)
        tracker.record()
        snap = tracker.snapshot_and_reset_window()
        assert snap["stalls"] == 0
        assert abs(snap["max_gap_ms"] - 400.0) < 0.1


class TestUtteranceWindow:
    """The second window on the one detector (wh-stt-load-metrics.4).

    The Google provider needs exact per-utterance stall numbers for its
    [load-diag] utt= line, and the reporting window is already consumed by
    the periodic [overflow-diag] block. A second window on the same
    detector gives the per-utterance reader its own start and end without
    a second timer or a second threshold.
    """

    def test_one_stall_is_counted_in_both_windows(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record()

        assert tracker.stall_count == 1
        assert tracker.utterance_stalls == 1
        assert tracker.max_gap_ms == 3000.0
        assert tracker.utterance_max_gap_ms == 3000.0

    def test_utterance_snapshot_returns_the_window_and_starts_a_new_one(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.5)
        tracker.record()

        snap = tracker.snapshot_and_reset_utterance()
        assert snap["stalls"] == 1
        assert snap["max_gap_ms"] == 2500.0

        snap2 = tracker.snapshot_and_reset_utterance()
        assert snap2["stalls"] == 0
        assert snap2["max_gap_ms"] == 0.0

    def test_reporting_window_reset_leaves_the_utterance_window_alone(self):
        """[overflow-diag] lands mid-utterance; the utterance keeps its numbers.

        This is the whole reason for the second window. The periodic block
        calls snapshot_and_reset_window() on its own interval, which can
        fall anywhere inside an utterance.
        """
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record()

        tracker.snapshot_and_reset_window()

        assert tracker.stall_count == 0
        assert tracker.max_gap_ms == 0.0
        assert tracker.utterance_stalls == 1
        assert tracker.utterance_max_gap_ms == 3000.0

    def test_utterance_reset_leaves_the_reporting_window_alone(self):
        """The mirror: a finished utterance must not empty the periodic block."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record()

        tracker.snapshot_and_reset_utterance()

        assert tracker.utterance_stalls == 0
        assert tracker.utterance_max_gap_ms == 0.0
        assert tracker.stall_count == 1
        assert tracker.max_gap_ms == 3000.0

    def test_a_stall_after_the_utterance_opens_is_counted_from_there(self):
        """Opening the window at utterance start is what makes the value exact."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(4.0)
        tracker.record()          # a stall before this utterance began

        tracker.snapshot_and_reset_utterance()   # utterance start
        clock.advance(0.03)
        tracker.record()
        clock.advance(1.5)
        tracker.record()          # the only stall inside the utterance

        snap = tracker.snapshot_and_reset_utterance()
        assert snap["stalls"] == 1
        assert snap["max_gap_ms"] == 1500.0

    def test_reset_leaves_both_windows_alone(self):
        """A mic restart is not a stall, and it is not an utterance boundary."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record()

        tracker.reset()
        clock.advance(10.0)
        assert tracker.record() is None

        assert tracker.stall_count == 1
        assert tracker.utterance_stalls == 1
        assert tracker.max_gap_ms == 3000.0
        assert tracker.utterance_max_gap_ms == 3000.0

    def test_busy_time_is_subtracted_in_the_utterance_window_too(self):
        """The utterance window reads the same gap the reporting window does."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(3.0)
        tracker.record(busy_s=2.5)

        assert tracker.utterance_stalls == 0
        assert abs(tracker.utterance_max_gap_ms - 500.0) < 0.1


class TestLoopStallTrackerLazyDepth:
    def test_callable_depth_not_evaluated_on_normal_iterations(self):
        """Queue depth costs a mutex acquisition; the tracker must only pay it
        when a rate-limited stall message actually forms."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(return_value=3)
        for _ in range(10):
            tracker.record(depth)
            clock.advance(0.03)
        depth.assert_not_called()

    def test_callable_depth_evaluated_when_message_forms(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(return_value=3)
        tracker.record(depth)
        clock.advance(2.0)
        msg = tracker.record(depth)
        assert depth.call_count == 1
        assert msg is not None
        assert "capture queue depth now 3" in msg

    def test_callable_depth_not_evaluated_when_rate_limited(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0,
                                   min_log_interval_s=5.0, clock=clock)
        depth = Mock(return_value=3)
        tracker.record(depth)
        clock.advance(2.0)
        tracker.record(depth)          # forms a message -> one evaluation
        clock.advance(2.0)
        assert tracker.record(depth) is None   # rate-limited -> no evaluation
        assert depth.call_count == 1

    def test_raising_callable_omits_depth_but_returns_message(self):
        """A diagnostic probe must never break the loop it observes
        (wh-sounddevice-starvation-parity.3.3): a raising depth callable is
        swallowed and the stall message forms without the depth suffix."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        depth = Mock(side_effect=RuntimeError("queue gone"))
        tracker.record(depth)
        clock.advance(2.0)
        msg = tracker.record(depth)
        assert msg == "[stall] consumer loop made no progress for 2.0s"


class TestLoopStallTrackerLabel:
    def test_custom_label_appears_in_message(self):
        clock = FakeClock()
        tracker = LoopStallTracker(
            stall_threshold_s=1.0, clock=clock, label="capture callback"
        )
        tracker.record()
        clock.advance(2.0)
        msg = tracker.record()
        assert msg is not None
        assert "capture callback" in msg
        assert "consumer loop" not in msg

    def test_default_label_is_consumer_loop(self):
        """The google provider's [stall] line must stay byte-identical."""
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        tracker.record()
        clock.advance(2.0)
        msg = tracker.record(queue_depth=7)
        assert msg == ("[stall] consumer loop made no progress for 2.0s"
                      "; capture queue depth now 7")
