"""Decide how many thumb wheel ticks each batch may act on (wh-mouse-wheel-sensitivity).

WHERE IT SITS. handlers/hid_listener.py turns the Logitech thumb wheel's raw
HID reports into one batch per 50 ms window and queues it.
handlers/mouse_handler.py drains that queue, runs EACH queued batch through
this filter in arrival order, and hands the sum of what passed to the
brightness zone or the volume zone by pointer position. Both zones get the
same dead zone and the same speed limit. The filter runs per batch, not on
the drained sum, because the zones await their plugins (the Sonos volume
plugin makes three network calls per change) and later batches queue up
behind that wait; summing them first would cap several 50 ms windows of
real movement as if they were one flick (wh-mouse-wheel-sensitivity.1.1).

WHY. David reported on 2026-09-13 that the wheel "responds to the slightest
touch" and "needs to be smoother". The log showed why (wheelhouse.log,
grep "Queued batched thumb_wheel"): a report carries a delta of +1 or -1
almost every time, 45 percent of all batches are a single tick, the first
batch of every roll is a single tick because the listener sends at once
when 50 ms have passed since its last send, and the volume zone acted on
every batch with no floor. One tick moved the Windows volume 0.75 dB at
once. Nothing bounded the change one batch could apply, so a flick whose
batch summed to 12 ticks (the largest seen) jumped 9 dB in one step.

THE THREE RULES.

1. DEAD ZONE. A gesture starts closed. Ticks are held, with their sign,
   until the held total reaches ``dead_zone_ticks``; that batch releases
   the held ticks and opens the gesture. While the gesture is open every
   batch passes at once, so a deliberate roll keeps its fine control after
   the first few ticks. A wobble that nets to zero opens nothing.

2. GESTURE GAP. A batch that arrives more than ``gesture_gap_ms`` after the
   previous batch starts a new gesture: the dead zone applies again and any
   held ticks are forgotten. Without this, two stray ticks minutes apart
   would add up and a third would fire a surprise change. Batches inside
   one roll arrive 50 to 110 ms apart, sometimes up to about 330 ms, so the
   500 ms default keeps a roll together and separates touches.

3. CAP. One batch passes at most ``max_ticks_per_batch`` ticks, and the
   excess is DISCARDED, not carried. Carrying it would keep the volume
   moving after the wheel stopped, which reads as a fault. A batch is about
   50 ms of wheel movement, so the cap is a speed limit: with the volume
   plugin's 0.75 dB per tick, 3 ticks per batch is at most 45 dB per second
   of continuous fast rolling, and a slow roll (1 to 3 ticks per batch, the
   measured norm) is unchanged.

The defaults are David's "a bit less sensitive": a single accidental tick
does nothing, a deliberate roll starts on its third tick, and no batch jumps
more than three ticks. All three are config keys next to VOLUME_INCREMENT
(THUMB_WHEEL_DEAD_ZONE_TICKS, THUMB_WHEEL_GESTURE_GAP_MS,
THUMB_WHEEL_MAX_TICKS_PER_BATCH) and are documented in
knowledge/helpdoc/config_descriptions.toml.

Out-of-range settings are clamped rather than refused: a dead zone or cap
below 1 becomes 1 (the wheel must never be switched off by a typo), and a
negative gap becomes 0. Fractions are truncated to whole ticks.
"""

import time
from typing import Callable

DEFAULT_DEAD_ZONE_TICKS = 3
DEFAULT_GESTURE_GAP_MS = 500
DEFAULT_MAX_TICKS_PER_BATCH = 3


class ThumbWheelFilter:
    """Hold, release, and cap the ticks of one thumb wheel batch at a time.

    :flow: High-Resolution Mouse Input
    :step: 3.5
    :description: Dead zone, gesture gap, and per-batch cap applied to each
        drained batch before zone routing.
    :data_in: Signed tick sum of one drained batch (int).
    :data_out: Signed ticks the zones may act on (int), often 0.
    :notes: Pure and single-threaded; MouseHandler.process_hid_events is its
        only caller and runs on the event loop. ``clock`` is injectable so
        tests control the gesture gap exactly.
    """

    def __init__(
        self,
        dead_zone_ticks: int = DEFAULT_DEAD_ZONE_TICKS,
        gesture_gap_ms: int = DEFAULT_GESTURE_GAP_MS,
        max_ticks_per_batch: int = DEFAULT_MAX_TICKS_PER_BATCH,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.dead_zone_ticks = max(1, int(dead_zone_ticks))
        self.gesture_gap_s = max(0.0, float(gesture_gap_ms)) / 1000.0
        self.max_ticks_per_batch = max(1, int(max_ticks_per_batch))
        self._clock = clock
        self._held_ticks = 0
        self._gesture_open = False
        self._last_batch_at: float | None = None

    def filter_batch(self, delta: int, at: float | None = None) -> int:
        """Return the ticks of this batch the zones may act on.

        ``delta`` is the signed tick sum of one 50 ms batch. ``at`` is the
        batch's arrival time on the ``clock`` scale (the listener stamps
        ``time.monotonic()`` on every batch it queues); the gesture gap is
        measured between arrival times, so a batch that waited in the queue
        behind a slow zone action is not mistaken for a pause in the roll
        (wh-mouse-wheel-sensitivity.1.1). Without ``at`` the clock is read
        now. A zero batch changes nothing and returns 0.
        """
        if delta == 0:
            return 0

        now = self._clock() if at is None else at
        if (
            self._last_batch_at is not None
            and now - self._last_batch_at > self.gesture_gap_s
        ):
            # Rule 2: the pause ended the previous gesture.
            self._held_ticks = 0
            self._gesture_open = False
        self._last_batch_at = now

        self._held_ticks += delta
        if not self._gesture_open:
            # Rule 1: hold until the dead zone is crossed.
            if abs(self._held_ticks) < self.dead_zone_ticks:
                return 0
            self._gesture_open = True

        # Rule 3: cap this batch; the excess is discarded, not carried.
        cap = self.max_ticks_per_batch
        passed = max(-cap, min(cap, self._held_ticks))
        self._held_ticks = 0
        return passed
