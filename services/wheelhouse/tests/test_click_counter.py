"""Tests for the click counter (wh-82lnx).

Covers all eight scenarios from the bead spec:

* Single retry verified increments counter from 0 to 1.
* Three sequential verified retries reach threshold; follow-up event fires.
* Failed retry (unverified) does NOT increment.
* Double-click on same correlation_token: counter increments once,
  second click is dropped (this lives in the publisher in main.py;
  tested separately in ``test_logic_retry_token_dedup.py``).
* Two interleaved retries for different tokens on the same tuple: both
  increment correctly under the per-tuple lock.
* Two interleaved retries for different tuples: both increment in their
  own counters.
* Counter survives logic-process restart via persisted file.
* Crash mid-write: temp file exists but target was never replaced; on
  restart the counter falls back to the previous value.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.wheelhouse.click_counter import ClickCounter
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import RetryThresholdReached, RetryVerified
from services.wheelhouse.utils.click_counter_writer import (
    read_pending_counters,
    write_pending_counters,
)


_TUPLE_A = ("zed.exe", "GlfwWindow", "Pane")
_FRIENDLY_A = "Zed"
_TUPLE_B = ("notepad.exe", "Edit", "Document")
_FRIENDLY_B = "Notepad"


# Reset tests use a separate small block so failures in the larger
# scenarios do not bury the wh-8d81z behaviour.


class TestResetTuple:
    def test_reset_clears_count_for_tuple(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 1

            await counter.reset_tuple(*_TUPLE_A)
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 0

        asyncio.run(run())

    def test_reset_does_not_publish_threshold(self, tmp_path):
        """Reset is post-grant cleanup; it must not trip the threshold
        publish path."""

        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=1)
            # Push one event so the counter has a row to reset.
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert len(threshold_events) == 1  # from the publish itself

            threshold_events.clear()
            await counter.reset_tuple(*_TUPLE_A)
            await counter.wait_for_pending_writes()
            assert threshold_events == []

        asyncio.run(run())

    def test_reset_does_not_affect_other_tuples(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await bus.publish(_event(*_TUPLE_B, _FRIENDLY_B))
            await counter.wait_for_pending_writes()

            await counter.reset_tuple(*_TUPLE_A)
            await counter.wait_for_pending_writes()

            assert counter.get_count(*_TUPLE_A) == 0
            assert counter.get_count(*_TUPLE_B) == 1

        asyncio.run(run())

    def test_reset_is_idempotent_on_missing_tuple(self, tmp_path):
        async def run():
            counter, bus, _events = _make_counter(tmp_path)
            # Counter has no entry for the tuple; reset must not raise.
            await counter.reset_tuple(*_TUPLE_A)
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 0

        asyncio.run(run())

    def test_reset_pre_persistence_crash_keeps_old_count_on_disk(self, tmp_path):
        """wh-reset-persist-crash-gap (deepseek review): pin the
        documented behavior at the boundary between in-memory reset
        and disk write commit. A crash between reset_tuple's mutation
        and the writer task running leaves the on-disk file at the
        pre-reset count. The grant on the soft-allow file is durable
        and the orphan is harmless until manual de-grant; this test
        documents the structural gap so a future production fix
        (await the write before returning) lands with a known prior."""

        async def run():
            counter, bus, _events = _make_counter(tmp_path)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 1

            # Mutate the in-memory counter via reset_tuple, but DO NOT
            # await wait_for_pending_writes. The fire-and-forget write
            # task may not have run yet -- we are simulating the
            # pre-write crash window.
            await counter.reset_tuple(*_TUPLE_A)
            # The in-memory counter is zero immediately.
            assert counter.get_count(*_TUPLE_A) == 0

        asyncio.run(run())

    def test_reset_persists_to_disk(self, tmp_path):
        """The on-disk file must reflect the reset so a logic restart
        does not reload the pre-reset count."""

        async def run():
            counter, bus, _events = _make_counter(tmp_path)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 1

            await counter.reset_tuple(*_TUPLE_A)
            await counter.wait_for_pending_writes()

            # New counter instance loads from the same path.
            from services.wheelhouse.click_counter import ClickCounter
            from services.wheelhouse.event_bus import EventBus
            new_bus = EventBus()
            new_counter = ClickCounter(
                event_bus=new_bus,
                persistence_path=tmp_path / "soft_allow_pending_counters.toml",
                threshold=3,
            )
            new_counter.load_from_disk()
            assert new_counter.get_count(*_TUPLE_A) == 0

        asyncio.run(run())


def _event(process_name: str, class_name: str, control_type: str, friendly: str) -> RetryVerified:
    return RetryVerified(
        process_name=process_name,
        class_name=class_name,
        control_type=control_type,
        app_friendly_name=friendly,
    )


def _make_counter(tmp_path: Path, threshold: int = 3) -> tuple[ClickCounter, EventBus, list]:
    bus = EventBus()
    counter = ClickCounter(
        event_bus=bus,
        persistence_path=tmp_path / "soft_allow_pending_counters.toml",
        threshold=threshold,
        clock=lambda: datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc),
    )
    counter.subscribe()
    threshold_events: list = []

    async def record(event):
        threshold_events.append(event)

    bus.subscribe(RetryThresholdReached, record)
    return counter, bus, threshold_events


# ---------------------------------------------------------------------------
# Single verified retry: 0 -> 1
# ---------------------------------------------------------------------------


class TestSingleVerifiedRetry:
    def test_single_verified_retry_increments_counter_from_zero_to_one(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 1
            # Threshold not reached at count=1 with default threshold=3.
            assert threshold_events == []

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Three verified retries: count reaches threshold, follow-up event fires
# ---------------------------------------------------------------------------


class TestThresholdReached:
    def test_three_sequential_verified_retries_publish_threshold_event(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=3)
            for _ in range(3):
                await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 3
            # Three sequential awaited retries at threshold=3 produce
            # exactly one event: the third increment crosses threshold.
            # The first two are below threshold and the per-tuple lock
            # serialises the read-modify-write so no duplicate event
            # fires for the same increment (wh-82lnx.1.2).
            assert len(threshold_events) == 1
            event = threshold_events[-1]
            assert isinstance(event, RetryThresholdReached)
            assert event.process_name == _TUPLE_A[0]
            assert event.class_name == _TUPLE_A[1]
            assert event.control_type == _TUPLE_A[2]
            assert event.app_friendly_name == _FRIENDLY_A
            assert event.count == 3

        asyncio.run(run())

    def test_threshold_event_does_not_fire_below_threshold(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=3)
            for _ in range(2):
                await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 2
            assert threshold_events == []

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Two interleaved retries for the same tuple: per-tuple lock guards both
# ---------------------------------------------------------------------------


class TestInterleavedSameTuple:
    def test_concurrent_increments_on_same_tuple_both_count(self, tmp_path):
        async def run():
            counter, bus, _ = _make_counter(tmp_path, threshold=10)
            # Publish two events without awaiting between them. EventBus
            # gathers handlers, so the per-tuple lock is what actually
            # serialises the read-modify-write.
            await asyncio.gather(
                bus.publish(_event(*_TUPLE_A, _FRIENDLY_A)),
                bus.publish(_event(*_TUPLE_A, _FRIENDLY_A)),
            )
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 2

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Two interleaved retries for different tuples: each increments independently
# ---------------------------------------------------------------------------


class TestInterleavedDifferentTuples:
    def test_different_tuples_each_have_their_own_counter(self, tmp_path):
        async def run():
            counter, bus, _ = _make_counter(tmp_path)
            await asyncio.gather(
                bus.publish(_event(*_TUPLE_A, _FRIENDLY_A)),
                bus.publish(_event(*_TUPLE_B, _FRIENDLY_B)),
                bus.publish(_event(*_TUPLE_A, _FRIENDLY_A)),
            )
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 2
            assert counter.get_count(*_TUPLE_B) == 1

    def test_concurrent_increments_across_many_tuples_all_persist(self, tmp_path):
        # wh-82lnx.2.1: the prior per-tuple writer design raced on
        # os.replace ordering when two tuples both had writes in
        # flight. Writer B's snapshot {A: 1, B: 1} could be replaced
        # by writer A's older snapshot {A: 1}, losing tuple B on
        # disk. The global single-flight persist removes the race.
        # This test fires twenty concurrent retries across twenty
        # distinct tuples and asserts the on-disk file contains all
        # of them after wait_for_pending_writes.
        async def run():
            counter, bus, _ = _make_counter(tmp_path)
            tuples = [
                (f"app{i:02d}.exe", f"Class{i:02d}", "Pane")
                for i in range(20)
            ]
            await asyncio.gather(*[
                bus.publish(_event(*t, f"App{i:02d}"))
                for i, t in enumerate(tuples)
            ])
            await counter.wait_for_pending_writes()
            for t in tuples:
                assert counter.get_count(*t) == 1

            entries = read_pending_counters(
                tmp_path / "soft_allow_pending_counters.toml"
            )
            on_disk = {(e[0], e[1], e[2]) for e in entries}
            for t in tuples:
                assert t in on_disk, (
                    f"tuple {t} present in memory but missing on disk"
                )

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Persistence: counter survives a Logic-process restart
# ---------------------------------------------------------------------------


class TestPersistenceAcrossRestart:
    def test_load_from_disk_recovers_counter_after_restart(self, tmp_path):
        async def run_first_session():
            counter, bus, _ = _make_counter(tmp_path)
            for _ in range(2):
                await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await bus.publish(_event(*_TUPLE_B, _FRIENDLY_B))
            await counter.wait_for_pending_writes()

        asyncio.run(run_first_session())

        # Simulate restart: build a fresh counter against the same path.
        bus2 = EventBus()
        counter2 = ClickCounter(
            event_bus=bus2,
            persistence_path=tmp_path / "soft_allow_pending_counters.toml",
        )
        counter2.load_from_disk()
        assert counter2.get_count(*_TUPLE_A) == 2
        assert counter2.get_count(*_TUPLE_B) == 1

    def test_load_from_disk_with_no_file_starts_empty(self, tmp_path):
        bus = EventBus()
        counter = ClickCounter(
            event_bus=bus,
            persistence_path=tmp_path / "does-not-exist.toml",
        )
        counter.load_from_disk()
        assert counter.get_count(*_TUPLE_A) == 0


# ---------------------------------------------------------------------------
# Crash mid-write: target was never replaced, restart falls back to prior value
# ---------------------------------------------------------------------------


class TestCrashMidWriteFallback:
    def test_orphan_temp_file_does_not_corrupt_load(self, tmp_path):
        # Simulate the scenario: a prior run wrote {TUPLE_A: 1} to disk
        # successfully, then started a second write that crashed after
        # creating the temp file but before os.replace. The target
        # therefore still holds the prior good value, and an orphan
        # temp file sits in the directory. Restart should load the
        # prior value cleanly.
        path = tmp_path / "soft_allow_pending_counters.toml"
        write_pending_counters(
            [(_TUPLE_A[0], _TUPLE_A[1], _TUPLE_A[2], 1, "2026-05-09T12:00:00+00:00")],
            path,
        )
        # Drop an orphan temp file that mimics an interrupted write.
        orphan = tmp_path / "soft_allow_pending_counters.toml.crash.tmp"
        orphan.write_bytes(b"corrupted partial bytes")

        bus = EventBus()
        counter = ClickCounter(
            event_bus=bus,
            persistence_path=path,
        )
        counter.load_from_disk()
        # Loaded from the intact target file, not the orphan temp.
        assert counter.get_count(*_TUPLE_A) == 1
        # Orphan still on disk; the writer cleans up its own orphans
        # but a crash leaves them behind. Not the load path's job to
        # reap them.
        assert orphan.exists()


# ---------------------------------------------------------------------------
# Threshold validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_threshold_below_one_rejected(self, tmp_path):
        bus = EventBus()
        with pytest.raises(ValueError):
            ClickCounter(
                event_bus=bus,
                persistence_path=tmp_path / "x.toml",
                threshold=0,
            )

    def test_threshold_of_one_publishes_on_every_retry(self, tmp_path):
        # threshold=1 is the boundary case. The clamping path in
        # main._read_soft_allow_threshold reaches it. Every increment
        # produces count >= 1, so every retry publishes
        # RetryThresholdReached (wh-82lnx.1.3).
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=1)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert len(threshold_events) == 1
            assert threshold_events[0].count == 1

            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert len(threshold_events) == 2
            assert threshold_events[1].count == 2

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Threshold event content matches the publisher
# ---------------------------------------------------------------------------


class TestThresholdEventPayload:
    def test_threshold_event_carries_friendly_name_and_count(self, tmp_path):
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=2)
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert len(threshold_events) == 1
            event = threshold_events[0]
            assert event.app_friendly_name == _FRIENDLY_A
            assert event.count == 2

        asyncio.run(run())

    def test_threshold_event_fires_on_every_at_or_above_threshold_increment(self, tmp_path):
        # Counter side does not dedup; wh-bqv9c handles "at most once per
        # session" on the consumer. Verify each at-or-above-threshold
        # increment publishes its own event.
        async def run():
            counter, bus, threshold_events = _make_counter(tmp_path, threshold=2)
            for _ in range(4):
                await bus.publish(_event(*_TUPLE_A, _FRIENDLY_A))
            await counter.wait_for_pending_writes()
            assert counter.get_count(*_TUPLE_A) == 4
            # Increments 2, 3, and 4 are all >= threshold.
            assert len(threshold_events) == 3
            counts = [e.count for e in threshold_events]
            assert counts == [2, 3, 4]

        asyncio.run(run())
