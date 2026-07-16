"""Tests for shared_audio.diagnostics.LoopStallTracker.

The tracker makes consumer-loop starvation visible: when the per-frame loop
stops being scheduled for more than the threshold, the next iteration gets a
log-ready message. Overflow bursts previously had no direct signature in the
logs -- only the queue-full drops they caused.
"""
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
