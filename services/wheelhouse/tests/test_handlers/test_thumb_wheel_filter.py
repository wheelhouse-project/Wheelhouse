"""Tests for ThumbWheelFilter (wh-mouse-wheel-sensitivity).

The filter sits between the 50 ms batches the HID listener queues and the
brightness and volume zones. Every test drives it with a fake clock so the
gesture gap is exact.
"""

import pytest

from handlers.thumb_wheel_filter import (
    DEFAULT_DEAD_ZONE_TICKS,
    DEFAULT_GESTURE_GAP_MS,
    DEFAULT_MAX_TICKS_PER_BATCH,
    ThumbWheelFilter,
)


class FakeClock:
    """A clock the test advances by hand, in seconds."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now += ms / 1000.0


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def wheel(clock):
    """Dead zone 3 ticks, gesture gap 500 ms, at most 3 ticks per batch."""
    return ThumbWheelFilter(
        dead_zone_ticks=3,
        gesture_gap_ms=500,
        max_ticks_per_batch=3,
        clock=clock,
    )


# ===========================================================================
# Dead zone
# ===========================================================================

class TestDeadZone:

    def test_single_tick_is_held(self, wheel):
        """One tick at the start of a gesture changes nothing."""
        assert wheel.filter_batch(1) == 0

    def test_ticks_below_dead_zone_are_held(self, wheel):
        assert wheel.filter_batch(1) == 0
        assert wheel.filter_batch(1) == 0

    def test_crossing_dead_zone_passes_held_ticks(self, wheel):
        """The batch that reaches the dead zone releases everything held."""
        wheel.filter_batch(1)
        wheel.filter_batch(1)
        assert wheel.filter_batch(1) == 3

    def test_one_batch_can_cross_dead_zone_alone(self, wheel):
        assert wheel.filter_batch(3) == 3

    def test_open_gesture_passes_single_ticks(self, wheel):
        """Once the gesture is open, each further tick acts at once."""
        wheel.filter_batch(3)
        assert wheel.filter_batch(1) == 1
        assert wheel.filter_batch(1) == 1

    def test_reversal_below_dead_zone_stays_closed(self, wheel):
        """Held ticks are signed: a wobble that nets to zero opens nothing."""
        assert wheel.filter_batch(2) == 0
        assert wheel.filter_batch(-2) == 0
        assert wheel.filter_batch(1) == 0

    def test_negative_direction_is_symmetric(self, wheel):
        assert wheel.filter_batch(-1) == 0
        assert wheel.filter_batch(-1) == 0
        assert wheel.filter_batch(-1) == -3
        assert wheel.filter_batch(-1) == -1

    def test_zero_batch_changes_nothing(self, wheel):
        wheel.filter_batch(2)
        assert wheel.filter_batch(0) == 0
        assert wheel.filter_batch(1) == 3

    def test_dead_zone_of_one_passes_every_tick(self, clock):
        wheel = ThumbWheelFilter(1, 500, 3, clock=clock)
        assert wheel.filter_batch(1) == 1
        assert wheel.filter_batch(-1) == -1


# ===========================================================================
# Gesture gap
# ===========================================================================

class TestGestureGap:

    def test_gap_forgets_held_ticks(self, wheel, clock):
        """Two ticks, a pause, one tick: the pause discards the two."""
        wheel.filter_batch(2)
        clock.advance_ms(600)
        assert wheel.filter_batch(1) == 0

    def test_gap_closes_an_open_gesture(self, wheel, clock):
        """After a pause the dead zone applies again."""
        wheel.filter_batch(3)
        clock.advance_ms(600)
        assert wheel.filter_batch(1) == 0

    def test_batch_inside_gap_keeps_gesture_open(self, wheel, clock):
        wheel.filter_batch(3)
        clock.advance_ms(400)
        assert wheel.filter_batch(1) == 1

    def test_batch_at_exactly_the_gap_keeps_gesture_open(self, wheel, clock):
        wheel.filter_batch(3)
        clock.advance_ms(500)
        assert wheel.filter_batch(1) == 1

    def test_gap_is_measured_from_the_previous_batch(self, wheel, clock):
        """Held ticks keep the gesture alive as long as batches keep coming."""
        wheel.filter_batch(1)
        clock.advance_ms(400)
        wheel.filter_batch(1)
        clock.advance_ms(400)
        assert wheel.filter_batch(1) == 3

    def test_gap_of_zero_closes_after_every_batch(self, clock):
        wheel = ThumbWheelFilter(3, 0, 3, clock=clock)
        wheel.filter_batch(3)
        clock.advance_ms(1)
        assert wheel.filter_batch(1) == 0

    def test_arrival_time_governs_the_gap_not_the_clock(self, wheel, clock):
        """A batch that ARRIVED inside the gap keeps the gesture open even
        when the consumer only gets to it two seconds later (a slow volume
        action, wh-mouse-wheel-sensitivity.1.1)."""
        wheel.filter_batch(3, at=1000.0)
        clock.advance_ms(2000)
        assert wheel.filter_batch(1, at=1000.3) == 1

    def test_arrival_time_can_close_the_gesture_before_the_clock_does(self, wheel, clock):
        """Two batches filtered in the same drain still belong to separate
        gestures when their arrival times are a gap apart."""
        wheel.filter_batch(3, at=1000.0)
        assert wheel.filter_batch(1, at=1000.6) == 0

    def test_without_arrival_time_the_clock_is_used(self, wheel, clock):
        wheel.filter_batch(3, at=1000.0)
        clock.advance_ms(2000)
        assert wheel.filter_batch(1) == 0


# ===========================================================================
# Per-batch cap
# ===========================================================================

class TestCap:

    def test_cap_limits_one_batch(self, wheel):
        assert wheel.filter_batch(12) == 3

    def test_cap_limits_negative_batch(self, wheel):
        assert wheel.filter_batch(-12) == -3

    def test_excess_is_discarded_not_carried(self, wheel):
        """A flick does not keep changing the volume after it stops."""
        wheel.filter_batch(12)
        assert wheel.filter_batch(1) == 1

    def test_cap_applies_to_the_dead_zone_crossing(self, wheel):
        wheel.filter_batch(2)
        assert wheel.filter_batch(12) == 3

    def test_batch_under_cap_passes_whole(self, wheel):
        wheel.filter_batch(3)
        assert wheel.filter_batch(2) == 2


# ===========================================================================
# Settings
# ===========================================================================

class TestSettings:

    def test_defaults(self):
        assert DEFAULT_DEAD_ZONE_TICKS == 3
        assert DEFAULT_GESTURE_GAP_MS == 500
        assert DEFAULT_MAX_TICKS_PER_BATCH == 3

    def test_settings_are_kept(self, clock):
        wheel = ThumbWheelFilter(4, 250, 2, clock=clock)
        assert wheel.dead_zone_ticks == 4
        assert wheel.gesture_gap_s == pytest.approx(0.25)
        assert wheel.max_ticks_per_batch == 2

    def test_out_of_range_settings_are_clamped(self, clock):
        """Zero or negative values cannot switch the wheel off or crash it."""
        wheel = ThumbWheelFilter(0, -5, 0, clock=clock)
        assert wheel.dead_zone_ticks == 1
        assert wheel.gesture_gap_s == 0.0
        assert wheel.max_ticks_per_batch == 1
        assert wheel.filter_batch(5) == 1

    def test_float_settings_are_truncated_to_whole_ticks(self, clock):
        wheel = ThumbWheelFilter(2.9, 500.0, 2.9, clock=clock)
        assert wheel.dead_zone_ticks == 2
        assert wheel.max_ticks_per_batch == 2

    def test_default_clock_is_monotonic(self):
        wheel = ThumbWheelFilter(3, 500, 3)
        assert wheel.filter_batch(3) == 3
        assert wheel.filter_batch(1) == 1
