"""Tests for ui/settle_detector.py -- the post-click settle detector.

Decision record: wh-overlay-slow-uia-stale-badges.10, comment "DECISION,
David, 2026-08-27: OPTION 3". Two matching reads prove a settled window; a
structure event arriving inside the pair's observation window voids the
match and restarts it; on an expired maximum the detector returns the last
completed read with ``settled=False`` so the caller always has something to
paint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from ui.settle_detector import (
    DEFAULT_SETTLE_MAX_MS,
    EventTimes,
    SettleResult,
    StructureEventListener,
    signature_of,
    wait_for_settled_window,
)


@dataclass(frozen=True)
class FakeMatch:
    """The three fields signature_of reads, nothing else."""

    control_type_id: int
    name: str
    bounds: tuple[int, int, int, int]


BUTTON_A = FakeMatch(50000, "OK", (10, 10, 50, 20))
BUTTON_B = FakeMatch(50000, "Cancel", (70, 10, 50, 20))
LINK_C = FakeMatch(50005, "Help", (10, 40, 30, 15))


@dataclass
class FakeReader:
    """Scripted reads. Each read returns the next match list and advances
    the fake clock by the scripted duration. The last script entry repeats
    if the loop reads more often than scripted."""

    script: list[tuple[list[FakeMatch], float]]
    clock: "FakeClock"
    calls: int = 0
    spans: list[tuple[float, float]] = field(default_factory=list)

    def __call__(self) -> list[FakeMatch]:
        entry = self.script[min(self.calls, len(self.script) - 1)]
        matches, duration = entry
        start = self.clock.now()
        self.clock.advance(duration)
        self.spans.append((start, self.clock.now()))
        self.calls += 1
        return list(matches)


class FakeClock:
    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    def advance(self, ms: float) -> None:
        self._now += ms


class ScriptedClock:
    """Returns one scripted sample per call; the final value repeats.
    ``last`` is the most recent sample handed out, so a reader can record
    what the clock said at the moment it was asked to read."""

    def __init__(self, samples: list[float]) -> None:
        self._samples = list(samples)
        self.last = 0.0

    def now(self) -> float:
        if len(self._samples) > 1:
            self.last = self._samples.pop(0)
        else:
            self.last = self._samples[0]
        return self.last


def events_at(timestamps: list[float]):
    """events_between callable over a fixed list of event times."""

    def _between(start_ms: float, end_ms: float) -> bool:
        return any(start_ms <= t <= end_ms for t in timestamps)

    return _between


def run(
    script: list[tuple[list[FakeMatch], float]],
    event_times: Optional[list[float]],
    max_ms: float = DEFAULT_SETTLE_MAX_MS,
) -> tuple[SettleResult, FakeReader]:
    clock = FakeClock()
    reader = FakeReader(script=script, clock=clock)
    events_between = None if event_times is None else events_at(event_times)
    result = wait_for_settled_window(
        reader,
        events_between=events_between,
        now_ms=clock.now,
        max_ms=max_ms,
    )
    return result, reader


class TestSignatureOf:
    def test_signature_is_ordered_type_name_bounds(self) -> None:
        sig = signature_of([BUTTON_A, LINK_C])
        assert sig == [
            (50000, "OK", (10, 10, 50, 20)),
            (50005, "Help", (10, 40, 30, 15)),
        ]

    def test_same_count_different_name_differs(self) -> None:
        assert signature_of([BUTTON_A]) != signature_of([BUTTON_B])

    def test_same_control_moved_differs(self) -> None:
        moved = FakeMatch(50000, "OK", (10, 30, 50, 20))
        assert signature_of([BUTTON_A]) != signature_of([moved])

    def test_order_matters(self) -> None:
        assert signature_of([BUTTON_A, LINK_C]) != signature_of([LINK_C, BUTTON_A])


class TestWaitForSettledWindow:
    def test_settles_on_two_matching_reads_without_events(self) -> None:
        result, reader = run([([BUTTON_A], 250.0), ([BUTTON_A], 250.0)], [])
        assert result.settled is True
        assert result.read_count == 2
        assert result.voided_pairs == 0
        assert result.matches == [BUTTON_A]
        assert result.signature == signature_of([BUTTON_A])

    def test_change_then_settle_takes_three_reads(self) -> None:
        result, _ = run(
            [([BUTTON_A], 250.0), ([BUTTON_B], 250.0), ([BUTTON_B], 250.0)],
            [],
        )
        assert result.settled is True
        assert result.read_count == 3
        assert result.matches == [BUTTON_B]

    def test_event_during_shared_read_voids_both_pairs(self) -> None:
        # Reads span [0,250], [250,500], [500,750], [750,1000]. An event at
        # 300 lands inside read 1's own span. Read 1 is the second read of
        # pair (0,1) and the first read of pair (1,2), and a read whose span
        # contains an event is not trustworthy in either pair -- so both
        # pairs restart and the detector settles on pair (2,3).
        result, _ = run(
            [([BUTTON_A], 250.0)] * 4,
            [300.0],
        )
        assert result.settled is True
        assert result.read_count == 4
        assert result.voided_pairs == 2

    def test_event_during_first_read_of_pair_voids_it(self) -> None:
        # Event at 100 lands inside read 0's own span; the tree changed
        # while read 0 ran, so pair (0,1) is not trustworthy.
        result, _ = run(
            [([BUTTON_A], 250.0)] * 3,
            [100.0],
        )
        assert result.settled is True
        assert result.read_count == 3
        assert result.voided_pairs == 1

    def test_event_before_the_pair_does_not_void(self) -> None:
        # The event happened before the first read started; nothing observed
        # is stale. (Negative time stands in for "before the detector ran".)
        result, _ = run(
            [([BUTTON_A], 250.0), ([BUTTON_A], 250.0)],
            [-50.0],
        )
        assert result.settled is True
        assert result.read_count == 2
        assert result.voided_pairs == 0

    def test_never_matching_returns_last_read_at_deadline(self) -> None:
        script = [
            ([BUTTON_A], 400.0),
            ([BUTTON_B], 400.0),
            ([LINK_C], 400.0),
            ([BUTTON_A, LINK_C], 400.0),
        ]
        result, _ = run(script, [], max_ms=1500.0)
        assert result.settled is False
        # Reads start at 0, 400, 800, 1200; the read that would start at
        # 1600 is past the deadline and never starts.
        assert result.read_count == 4
        assert result.matches == [BUTTON_A, LINK_C]

    def test_event_storm_returns_last_read_at_deadline(self) -> None:
        # Matching reads, but an event lands in every observation window:
        # the maximum decides, and the caller still gets the last read.
        result, _ = run(
            [([BUTTON_A], 250.0)] * 12,
            [float(t) for t in range(0, 3000, 100)],
            max_ms=1500.0,
        )
        assert result.settled is False
        assert result.matches == [BUTTON_A]
        assert result.voided_pairs >= 1

    def test_deadline_expired_after_first_read_returns_it(self) -> None:
        result, _ = run([([BUTTON_A], 1600.0)], [], max_ms=1500.0)
        assert result.settled is False
        assert result.read_count == 1
        assert result.matches == [BUTTON_A]

    def test_window_vanishing_mid_detection_returns_last_read(self) -> None:
        # The clicked application closes mid-detection: walk_window re-raises
        # a stale-window error (wh-overlay-slow-uia-stale-badges.10.1.1). The
        # loop already holds the last completed read and must return it.
        clock = FakeClock()
        calls = 0

        def reader() -> list[FakeMatch]:
            nonlocal calls
            calls += 1
            if calls == 1:
                clock.advance(250.0)
                return [BUTTON_A]
            raise OSError("window closed")

        result = wait_for_settled_window(
            reader,
            events_between=events_at([]),
            now_ms=clock.now,
            max_ms=1500.0,
        )
        assert result.settled is False
        assert result.read_count == 1
        assert result.matches == [BUTTON_A]
        assert result.signature == signature_of([BUTTON_A])

    def test_first_read_raising_propagates(self) -> None:
        # With no completed read there is nothing to return; the caller's
        # normal walk-failure handling applies.
        def reader() -> list[FakeMatch]:
            raise OSError("window closed")

        with pytest.raises(OSError):
            wait_for_settled_window(
                reader, events_between=None, now_ms=FakeClock().now
            )

    def test_read_never_starts_at_or_after_the_deadline_sample(self) -> None:
        # Review finding wh-overlay-slow-uia-stale-badges.10.1.5: the old
        # loop checked the clock, then sampled again for the recorded read
        # start; a clock hop between the two samples let a read begin past
        # the deadline. The guard sample and the read start must be one
        # sample.
        clock = ScriptedClock([0.0, 0.0, 1400.0, 1600.0, 1700.0, 1800.0])
        starts: list[float] = []

        def read() -> list[FakeMatch]:
            starts.append(clock.last)
            return [BUTTON_A]

        wait_for_settled_window(read, now_ms=clock.now, max_ms=1500.0)
        assert len(starts) == 2
        assert all(s < 1500.0 for s in starts), starts

    def test_read_does_not_start_at_exactly_the_deadline(self) -> None:
        # Boundary pin for the deadline guard: a sample equal to the
        # deadline must stop the loop. Proven by gate mutation M12.
        clock = ScriptedClock([0.0, 0.0, 1500.0, 1600.0])
        calls = {"count": 0}

        def read() -> list[FakeMatch]:
            calls["count"] += 1
            return [BUTTON_A]

        result = wait_for_settled_window(read, now_ms=clock.now, max_ms=1500.0)
        assert calls["count"] == 1
        assert result.settled is False
        assert result.read_count == 1

    def test_no_events_callable_degrades_to_plain_two_reads(self) -> None:
        result, _ = run([([BUTTON_A], 250.0), ([BUTTON_A], 250.0)], None)
        assert result.settled is True
        assert result.voided_pairs == 0

    def test_elapsed_ms_reports_clock_delta(self) -> None:
        result, _ = run([([BUTTON_A], 250.0), ([BUTTON_A], 250.0)], [])
        assert result.elapsed_ms == pytest.approx(500.0)

    def test_default_max_is_1500_ms(self) -> None:
        assert DEFAULT_SETTLE_MAX_MS == 1500


class TestEventTimes:
    def test_any_between_is_inclusive(self) -> None:
        times = EventTimes()
        times.add(100.0)
        assert times.any_between(100.0, 100.0) is True
        assert times.any_between(0.0, 99.9) is False
        assert times.any_between(100.1, 200.0) is False

    def test_event_store_is_bounded(self) -> None:
        # Review finding wh-overlay-slow-uia-stale-badges.10.1.6: a
        # provider that never stops firing must not grow the store without
        # bound. The store keeps the newest entries and drops the oldest.
        times = EventTimes()
        for i in range(5000):
            times.add(float(i))
        assert len(times._times) == 4096
        assert times.any_between(4999.0, 4999.0) is True
        assert times.any_between(5000.0, 6000.0) is False

    def test_eviction_cannot_hide_an_in_window_event(self) -> None:
        # Review finding wh-overlay-slow-uia-stale-badges.10.1.10: a storm
        # arriving after the observation window closes must not evict the
        # one event that voids the pair. Lost evidence at or after the
        # query start answers True, which voids -- the safe direction.
        times = EventTimes()
        times.add(5.0)
        for _ in range(5000):
            times.add(9999.0)
        assert times.any_between(0.0, 10.0) is True

    def test_empty_has_no_events(self) -> None:
        assert EventTimes().any_between(0.0, 1e9) is False


class FakeAutomation:
    """Stands in for the IUIAutomation object inside the listener."""

    def __init__(self, fail_structure: bool = False, fail_events: bool = False):
        self.fail_structure = fail_structure
        self.fail_events = fail_events
        self.structure_handlers: list[Any] = []
        self.event_handlers: list[tuple[int, Any]] = []
        self.removed = False

    def AddStructureChangedEventHandler(self, element, scope, cache, handler):
        if self.fail_structure:
            raise OSError("registration refused")
        self.structure_handlers.append(handler)

    def AddAutomationEventHandler(self, event_id, element, scope, cache, handler):
        if self.fail_events:
            raise OSError("registration refused")
        self.event_handlers.append((int(event_id), handler))

    def RemoveAllEventHandlers(self) -> None:
        self.removed = True


class TestStructureEventListener:
    def test_start_registers_and_records_through_event_times(self) -> None:
        clock = FakeClock()
        automation = FakeAutomation()
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=clock.now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is True
        assert listener.available is True
        clock.advance(120.0)
        listener._record()
        assert listener.any_event_between(0.0, 200.0) is True
        assert listener.any_event_between(121.0, 200.0) is False

    def test_all_registrations_failing_leaves_listener_unavailable(self) -> None:
        automation = FakeAutomation(fail_structure=True, fail_events=True)
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=FakeClock().now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is False
        assert listener.available is False
        assert listener.any_event_between(0.0, 1e9) is False

    def test_automation_factory_raising_leaves_listener_unavailable(self) -> None:
        def boom() -> Any:
            raise OSError("no COM today")

        listener = StructureEventListener(
            hwnd=1234,
            now_ms=FakeClock().now,
            create_automation_fn=boom,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is False
        assert listener.available is False

    def test_partial_registration_still_counts_as_available(self) -> None:
        automation = FakeAutomation(fail_structure=True, fail_events=False)
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=FakeClock().now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is True
        assert listener.available is True

    def test_stop_removes_all_handlers(self) -> None:
        automation = FakeAutomation()
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=FakeClock().now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        listener.start()
        listener.stop()
        assert automation.removed is True

    def test_record_after_stop_does_not_append(self) -> None:
        # A stopped listener whose handlers are still registered (stop() never
        # ran to completion, or RemoveAllEventHandlers failed) must not grow
        # the store forever (wh-overlay-slow-uia-stale-badges.10.1.2).
        clock = FakeClock()
        automation = FakeAutomation()
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=clock.now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is True
        listener.stop()
        clock.advance(50.0)
        listener._record()
        assert listener._times.any_between(0.0, 1e9) is False

    def test_fired_handlers_record_through_event_times(self) -> None:
        # Fire the captured handlers through the same method names the COM
        # vtable calls, so a severed handler-to-store wiring cannot pass
        # green (wh-overlay-slow-uia-stale-badges.10.1.3).
        clock = FakeClock()
        automation = FakeAutomation()
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=clock.now,
            create_automation_fn=lambda: automation,
            element_from_hwnd_fn=lambda a, h: object(),
        )
        assert listener.start() is True

        clock.advance(10.0)
        handler = automation.structure_handlers[0]
        handler.IUIAutomationStructureChangedEventHandler_HandleStructureChangedEvent(
            None, 0, None
        )

        clock.advance(10.0)
        event_handlers = [h for _, h in automation.event_handlers]
        # One distinct handler object per event id, all feeding one store.
        assert len({id(h) for h in event_handlers}) == 3
        event_handlers[0].IUIAutomationEventHandler_HandleAutomationEvent(None, 0)

        assert listener.any_event_between(10.0, 10.0) is True
        assert listener.any_event_between(20.0, 20.0) is True
        assert listener.any_event_between(11.0, 19.0) is False

    def test_stop_before_start_is_harmless(self) -> None:
        listener = StructureEventListener(
            hwnd=1234,
            now_ms=FakeClock().now,
            create_automation_fn=lambda: FakeAutomation(),
            element_from_hwnd_fn=lambda a, h: object(),
        )
        listener.stop()
        assert listener.available is False
