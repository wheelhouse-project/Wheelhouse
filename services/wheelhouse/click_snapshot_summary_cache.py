"""Logic-side bounded TTL cache mapping snapshot_id to WalkSnapshotSummary
(wh-jfavj).

The Logic process owns this cache. During the Phase 1.5 numbered-overlay
flow (epic wh-l4h.1), Logic retains a copy of the ``WalkSnapshotSummary``
it forwarded to the GUI so a later ``snapshot_item_clicked`` event can be
resolved without the GUI tracking item_ids: the overlay paints display
numbers, the click reports a display number, and Logic maps the display
number back to the ``item_id`` for the ``click_snapshot_item`` request.
The full lifecycle is in ``docs/plans/2026-05-21-voice-element-clicking-
design-v5.md`` under "GUI-to-Logic round-trip (v5 added)", step 3 (retain)
and step 6 (resolve + dispatch).

POPULATION insertion point: the ``put`` call that stores a summary lives
in the ``click_element`` / ``show_numbered_overlay`` awaiter callback,
which is wh-tab7j, NOT this slice. This slice ships the cache, the
display_number -> item_id resolver, and their tests; wh-tab7j wires the
put.

This cache mirrors ``shared.rejection_token_cache.RejectionTokenCache``:
an ``OrderedDict`` with monotonic ``time_source`` injection, oldest-first
eviction past ``max_entries``, and a three-way ``resolve`` (HIT / EXPIRED
/ MISS) where EXPIRED prunes the entry. The call site wires ``ttl_seconds``
to the ``[click]`` config block's ``snapshot_ttl_seconds`` (default 30,
int >= 1).

Eviction triggers (three):
  1. TTL expiry -- ``resolve`` past ``ttl_seconds`` prunes and returns
     EXPIRED.
  2. snapshot_id replacement -- a second ``put`` for the same snapshot_id
     replaces the value and resets its expiry.
  3. Logic-process shutdown -- a caller-driven ``clear()`` drops every
     entry. Shutdown clear is a caller responsibility (the Logic process
     calls ``clear()`` on teardown); this module only exposes the method.

Thread-safety: not thread-safe. The cache is owned by the Logic process
and is driven from the same single-thread asyncio event loop that handles
GuiManager IPC. See architecture-overview.md "Concurrency" notes for the
broader contract.
"""

from __future__ import annotations

import enum
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from ui.element_types import WalkSnapshotSummary


_DEFAULT_TTL_SECONDS = 30.0
_DEFAULT_MAX_ENTRIES = 32


class CacheStatus(enum.Enum):
    """Outcome of a ClickSnapshotSummaryCache lookup."""

    HIT = "hit"
    EXPIRED = "expired"
    MISS = "miss"


@dataclass(frozen=True)
class CacheResult:
    """Result of a :meth:`ClickSnapshotSummaryCache.resolve` call.

    ``summary`` is set only when ``status == HIT``; it is ``None`` for
    EXPIRED and MISS.
    """

    status: CacheStatus
    summary: Optional[WalkSnapshotSummary] = None


_MISS = CacheResult(CacheStatus.MISS, None)


class ClickSnapshotSummaryCache:
    """snapshot_id -> WalkSnapshotSummary cache with TTL and bounded size."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        time_source: Optional[Callable[[], float]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._time_source: Callable[[], float] = time_source or time.monotonic
        # OrderedDict gives O(1) eviction of the oldest entry on overflow.
        # Insertion order matches the order we want to evict in: oldest
        # first.
        self._entries: "OrderedDict[str, tuple[WalkSnapshotSummary, float]]" = (
            OrderedDict()
        )

    def put(self, snapshot_id: str, summary: WalkSnapshotSummary) -> None:
        """Store ``summary`` under ``snapshot_id``. Replaces any previous
        value.

        Replacing an existing entry resets its expiry. If inserting would
        push the cache past ``max_entries``, the oldest entry is evicted.
        """

        now = self._time_source()
        if snapshot_id in self._entries:
            del self._entries[snapshot_id]
        self._entries[snapshot_id] = (summary, now)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def resolve(self, snapshot_id: str) -> CacheResult:
        """Look up ``snapshot_id`` and return a three-way outcome.

        Returns:
          * CacheResult(HIT, summary)  -- snapshot alive.
          * CacheResult(EXPIRED, None) -- snapshot was stored but its TTL
            elapsed. The entry is pruned before this method returns.
          * CacheResult(MISS, None)    -- snapshot was never stored or was
            evicted under max_entries pressure.
        """

        entry = self._entries.get(snapshot_id)
        if entry is None:
            return _MISS
        summary, stored_at = entry
        if self._time_source() - stored_at >= self.ttl_seconds:
            del self._entries[snapshot_id]
            return CacheResult(CacheStatus.EXPIRED, None)
        return CacheResult(CacheStatus.HIT, summary)

    def clear(self) -> None:
        """Drop every entry.

        Called by the Logic process on shutdown (eviction trigger 3). Safe
        to call when already empty.
        """

        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class ResolveOutcome(enum.Enum):
    """Outcome of :func:`resolve_display_number`.

    ``FOUND`` carries the resolved ``item_id``. ``NOT_FOUND`` means the
    snapshot was live but no item carried the requested display number.
    ``SNAPSHOT_EXPIRED`` collapses the cache EXPIRED and MISS states: the
    snapshot is gone, so the click arrived too late (or for an unknown
    snapshot).
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    SNAPSHOT_EXPIRED = "snapshot_expired"


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving a (snapshot_id, display_number) to an item_id.

    ``item_id`` is set only when ``outcome == FOUND``; it is ``None``
    otherwise.
    """

    outcome: ResolveOutcome
    item_id: Optional[str] = None


def resolve_display_number(
    cache: ClickSnapshotSummaryCache,
    snapshot_id: str,
    display_number: int,
) -> ResolveResult:
    """Map a (snapshot_id, display_number) to the matching item's item_id.

    This is the Logic-side resolver for step 6 of the v5 GUI-to-Logic
    round-trip: a ``snapshot_item_clicked`` event carries a display number,
    and Logic resolves it to the ``item_id`` the Input-side
    ``click_snapshot_item`` action needs.

    Returns a :class:`ResolveResult`:
      * ``FOUND`` with ``item_id`` -- a live snapshot has an item whose
        ``display_number`` matches.
      * ``NOT_FOUND`` -- the snapshot is live but no item carries that
        display number (stale overlay, out-of-range click). Kept distinct
        from ``SNAPSHOT_EXPIRED`` so the caller can log the two apart; the
        caller decides whether to collapse them at the user surface.
      * ``SNAPSHOT_EXPIRED`` -- the cache returned EXPIRED or MISS (the
        snapshot's TTL elapsed, or it was never retained / was evicted).

    wh-g4oma owns the ``snapshot_expired`` reason tag on
    ``ClickElementResponse`` and the user-facing notice wording. This
    resolver is the EMIT SITE that signals the expired condition; it does
    NOT define that schema field or wording. The caller maps
    ``SNAPSHOT_EXPIRED`` (and, per wh-g4oma's choice, possibly
    ``NOT_FOUND``) to the ``snapshot_expired`` reason tag.
    """

    result = cache.resolve(snapshot_id)
    if result.status is not CacheStatus.HIT or result.summary is None:
        # EXPIRED or MISS: the snapshot is gone. The caller surfaces this
        # as snapshot_expired (tag owned by wh-g4oma).
        return ResolveResult(ResolveOutcome.SNAPSHOT_EXPIRED, None)

    for item in result.summary.items:
        if item.display_number == display_number:
            return ResolveResult(ResolveOutcome.FOUND, item.item_id)

    return ResolveResult(ResolveOutcome.NOT_FOUND, None)
