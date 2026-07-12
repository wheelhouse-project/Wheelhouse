"""Tests for the Logic-side display_number -> item_id resolver (wh-jfavj).

``resolve_display_number`` maps a (snapshot_id, display_number) pair to the
matching item's item_id via the ClickSnapshotSummaryCache. This is step 6
of the v5 GUI-to-Logic round-trip (epic wh-l4h.1): a snapshot_item_clicked
event carries a display number, and Logic resolves it to the item_id the
Input-side click_snapshot_item action needs.

Coverage:
  * FOUND returns the correct item_id.
  * NOT_FOUND when the snapshot is live but no item carries that display
    number (distinct from SNAPSHOT_EXPIRED per the chosen semantics).
  * SNAPSHOT_EXPIRED on a cache MISS (never stored / evicted) and on a
    cache EXPIRED (TTL elapsed).
"""

from __future__ import annotations

from click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
    ResolveOutcome,
    resolve_display_number,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary(snapshot_id: str = "s1", count: int = 3) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"item-{i}",
            display_number=i,
            name=f"Control {i}",
            role="Button",
            bounds=(i, i, i + 5, i + 5),
            monitor_id=0,
        )
        for i in range(1, count + 1)
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=items,
        created_at_monotonic=0.0,
    )


def test_found_returns_item_id():
    cache = ClickSnapshotSummaryCache()
    cache.put("s1", _summary("s1", count=3))
    result = resolve_display_number(cache, "s1", 2)
    assert result.outcome is ResolveOutcome.FOUND
    assert result.item_id == "item-2"


def test_found_first_and_last():
    cache = ClickSnapshotSummaryCache()
    cache.put("s1", _summary("s1", count=3))
    assert resolve_display_number(cache, "s1", 1).item_id == "item-1"
    assert resolve_display_number(cache, "s1", 3).item_id == "item-3"


def test_not_found_when_live_but_display_number_absent():
    cache = ClickSnapshotSummaryCache()
    cache.put("s1", _summary("s1", count=3))
    result = resolve_display_number(cache, "s1", 9)
    assert result.outcome is ResolveOutcome.NOT_FOUND
    assert result.item_id is None


def test_snapshot_expired_on_miss():
    cache = ClickSnapshotSummaryCache()
    # nothing stored under this id
    result = resolve_display_number(cache, "never-stored", 1)
    assert result.outcome is ResolveOutcome.SNAPSHOT_EXPIRED
    assert result.item_id is None


def test_snapshot_expired_on_ttl_elapsed():
    clock = [0.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    cache.put("s1", _summary("s1", count=2))
    clock[0] = 100.0  # past TTL
    result = resolve_display_number(cache, "s1", 1)
    assert result.outcome is ResolveOutcome.SNAPSHOT_EXPIRED
    assert result.item_id is None
