"""Click counter for verified Try-it-anyway retries (wh-82lnx).

The Logic process maintains a per-(process_name, class_name, control_type)
counter that increments on every verified retry. The wh-bqv9c three-strikes
follow-up toast subscribes to ``RetryThresholdReached`` to surface the
"Save <App> as an allowed target?" prompt when the counter reaches the
configured threshold (default 3, see ``[ui_actions.text_target].soft_allow_threshold``).

Concurrency contract:

* Per-tuple ``asyncio.Lock`` guards the read-modify-write of the in-memory
  counter. Two retries for the same tuple serialise; two retries for
  different tuples can interleave.
* GLOBAL single-flight write coalescing for persistence (wh-82lnx.2.1).
  The on-disk file is rewritten whole every time, so two writers running
  concurrently against different tuples can race on ``os.replace``
  ordering and persist a stale snapshot after a newer one. A single
  ``_persist_in_flight`` flag and ``_persist_pending`` flag together
  serialise every write through one coroutine, and a pending flag set
  while a write is running causes the running coroutine to take a fresh
  snapshot and write again before exiting. The "per-tuple" spec wording
  in the bead is preserved in spirit -- per-tuple writes still coalesce
  -- but the actual write serialisation is global because the file
  format is whole-file rewrite.
* Counter increments only on ``RetryVerified`` events. The publisher
  (``forward_retry_dictation_by_token`` in ``main.py``) gates on
  ``retry_outcome=verified`` and on a Logic-side rejection-cache hit,
  so a ``RetryVerified`` event delivered to this counter has already
  cleared both checks.

Persistence is best-effort. A write failure logs a warning but does not
block the user; the worst symptom is the user sees the standard
rejection toast a few more times before the threshold re-fires after
the file is recovered. The on-disk format is a list of
``{process_name, class_name, control_type, count, last_updated_at}``
entries; see ``utils/click_counter_writer.py``.

Threading: not thread-safe. The counter is owned by the Logic process
and is driven by the same single-thread asyncio event loop that handles
the EventBus. ``asyncio.to_thread`` shifts the actual file I/O to a
worker thread but the counter state itself is only ever mutated on the
event loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import RetryThresholdReached, RetryVerified
from services.wheelhouse.utils.click_counter_writer import (
    CounterEntry,
    read_pending_counters,
    write_pending_counters,
)

logger = logging.getLogger(__name__)


TupleKey = tuple[str, str, str]
"""(process_name, class_name, control_type)."""


_DEFAULT_THRESHOLD = 3


class ClickCounter:
    """Per-tuple counter for verified Try-it-anyway retries.

    Subscribe to ``RetryVerified`` on construction by calling
    :meth:`subscribe`. Call :meth:`load_from_disk` once at startup to
    recover any persisted state. The counter publishes
    ``RetryThresholdReached`` on every increment that brings the
    per-tuple counter to or above the threshold; wh-bqv9c handles the
    "show toast at most once per tuple per session" rule on the
    consumer side.
    """

    def __init__(
        self,
        event_bus: EventBus,
        persistence_path: Path,
        threshold: int = _DEFAULT_THRESHOLD,
        time_source: Optional[Callable[[], float]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._event_bus = event_bus
        self._path = persistence_path
        self._threshold = threshold
        self._time = time_source or time.monotonic
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._counts: dict[TupleKey, int] = {}
        self._friendly_names: dict[TupleKey, str] = {}
        self._tuple_locks: dict[TupleKey, asyncio.Lock] = {}
        # wh-82lnx.2.1: global single-flight persistence. Per-tuple
        # writers raced on os.replace ordering and could persist a
        # stale snapshot after a newer one. _persist_task holds the
        # currently running writer coroutine (one at a time);
        # _persist_pending records that a new write was requested
        # while a writer was running, so the running writer takes a
        # fresh snapshot and writes again before exiting.
        self._persist_task: asyncio.Task[None] | None = None
        self._persist_pending: bool = False

    @property
    def threshold(self) -> int:
        return self._threshold

    def subscribe(self) -> None:
        """Register the EventBus subscription. Call once at startup."""
        self._event_bus.subscribe(RetryVerified, self._on_retry_verified)

    def load_from_disk(self) -> None:
        """Populate in-memory counters from the persistence file.

        Best-effort: a missing or malformed file yields an empty
        counter map. ``app_friendly_name`` is not stored on disk
        (the rejection cache holds it; the file format only carries
        the identity triple plus count and timestamp), so the friendly
        name is filled in from the next ``RetryVerified`` event for
        that tuple.
        """
        entries = read_pending_counters(self._path)
        for entry in entries:
            process_name, class_name, control_type, count, _ = entry
            key: TupleKey = (process_name, class_name, control_type)
            self._counts[key] = count

    def get_count(self, process_name: str, class_name: str, control_type: str) -> int:
        return self._counts.get((process_name, class_name, control_type), 0)

    def snapshot_entries(self) -> list[CounterEntry]:
        """Return a sortable snapshot of all current counters.

        Used by the writer task to render the on-disk file. Sorted by
        tuple identity so the file's diff is stable across writes.
        """
        now_iso = self._clock().isoformat()
        items = sorted(self._counts.items())
        return [
            (key[0], key[1], key[2], count, now_iso)
            for key, count in items
        ]

    async def _on_retry_verified(self, event: RetryVerified) -> None:
        key: TupleKey = (
            event.process_name, event.class_name, event.control_type,
        )
        lock = self._lock_for(key)
        async with lock:
            new_count = self._counts.get(key, 0) + 1
            self._counts[key] = new_count
            self._friendly_names[key] = event.app_friendly_name
            self._schedule_write()
            if new_count >= self._threshold:
                try:
                    await self._event_bus.publish(RetryThresholdReached(
                        process_name=event.process_name,
                        class_name=event.class_name,
                        control_type=event.control_type,
                        app_friendly_name=event.app_friendly_name,
                        count=new_count,
                    ))
                except Exception as exc:
                    logger.warning(
                        "click_counter: RetryThresholdReached publish "
                        "failed for tuple=%s: %s",
                        key, exc,
                    )

    def _lock_for(self, key: TupleKey) -> asyncio.Lock:
        lock = self._tuple_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._tuple_locks[key] = lock
        return lock

    def _schedule_write(self) -> None:
        # Global single-flight: any in-memory counter change triggers
        # the same writer, which captures every counter on its next
        # snapshot. Which tuple changed does not matter.
        if self._persist_task is not None and not self._persist_task.done():
            self._persist_pending = True
            return
        self._persist_task = asyncio.create_task(
            self._persist_loop(),
            name="click_counter-persist",
        )

    async def _persist_loop(self) -> None:
        try:
            while True:
                self._persist_pending = False
                snapshot = self.snapshot_entries()
                ok = await asyncio.to_thread(
                    write_pending_counters, snapshot, self._path,
                )
                if not ok:
                    logger.warning(
                        "click_counter: persistence write failed; "
                        "in-memory state retained, on-disk state stale",
                    )
                if not self._persist_pending:
                    break
        finally:
            self._persist_task = None

    async def reset_tuple(
        self, process_name: str, class_name: str, control_type: str,
    ) -> None:
        """Drop the per-tuple counter and schedule a persistence write (wh-8d81z).

        Called by the Logic Yes-path handler after
        ``LogicController.add_soft_allow`` succeeds (disk write + IPC).
        Resetting keeps the persistence file clean and protects against
        the user later removing the soft-allow grant manually: with the
        counter at zero, the threshold check starts from scratch on the
        next ``RetryVerified`` instead of immediately re-firing the
        threshold prompt.

        Idempotent: a tuple that is not in the counter map is a no-op.

        Concurrency contract: the reset takes the same per-tuple
        ``asyncio.Lock`` as ``_on_retry_verified``. The lock prevents
        inconsistent intermediate state -- a verify and a reset for
        the same tuple cannot run concurrently -- but it does NOT
        guarantee that a verify scheduled after the reset will not
        slip in and re-increment the counter to one. Callers that
        need a guaranteed-zero post-reset should re-check
        ``get_count`` after the reset returns and reset again if the
        race fired (see ``LogicController._handle_grant_prompt_yes_clicked``
        for the wh-reset-race-concurrent-verified guard).

        Persistence: the on-disk file is rewritten via the same
        single-flight writer the increment path uses. There is a
        small window between this method returning and the disk
        write committing where the in-memory counter is zero but
        the on-disk file still carries the pre-reset count. A logic
        process crash inside that window restores the old count on
        the next run; the orphan is harmless while the soft-allow
        grant is durable and only matters if the user later removes
        the grant manually (wh-reset-persist-crash-gap).
        """
        key: TupleKey = (process_name, class_name, control_type)
        lock = self._lock_for(key)
        async with lock:
            if key not in self._counts and key not in self._friendly_names:
                return
            self._counts.pop(key, None)
            self._friendly_names.pop(key, None)
            self._schedule_write()

    async def wait_for_pending_writes(self) -> None:
        """Await the currently scheduled write task.

        Tests use this to assert disk state after a sequence of
        increments. Production callers do not need to wait; the
        writer is fire-and-forget.
        """
        task = self._persist_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
