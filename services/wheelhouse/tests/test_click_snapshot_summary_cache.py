"""Tests for ClickSnapshotSummaryCache (wh-jfavj).

The cache is the Logic-side store of WalkSnapshotSummary objects keyed by
snapshot_id during the Phase 1.5 numbered-overlay flow (epic wh-l4h.1). It
mirrors RejectionTokenCache: TTL expiry, oldest-first eviction past
max_entries, replacement resets expiry, three-way resolve (HIT/EXPIRED/MISS).

Coverage:
  * Constructor validation (ttl_seconds > 0, max_entries >= 1).
  * resolve HIT / EXPIRED / MISS with an injected fake clock, at and across
    the TTL boundary; EXPIRED prunes the entry.
  * Eviction past max_entries (oldest-first).
  * Replacement resets expiry.
  * clear() drops everything.
"""

from __future__ import annotations

import pytest

from click_snapshot_summary_cache import (
    CacheStatus,
    ClickSnapshotSummaryCache,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary(snapshot_id: str = "s1") -> WalkSnapshotSummary:
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id="m1",
                display_number=1,
                name="Cancel",
                role="Button",
                bounds=(0, 0, 10, 10),
                monitor_id=0,
            )
        ],
        created_at_monotonic=0.0,
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_rejects_non_positive_ttl():
    with pytest.raises(ValueError):
        ClickSnapshotSummaryCache(ttl_seconds=0)


def test_rejects_max_entries_below_one():
    with pytest.raises(ValueError):
        ClickSnapshotSummaryCache(max_entries=0)


# ---------------------------------------------------------------------------
# HIT / EXPIRED / MISS
# ---------------------------------------------------------------------------


def test_miss_for_unknown_id():
    cache = ClickSnapshotSummaryCache()
    result = cache.resolve("never-stored")
    assert result.status is CacheStatus.MISS
    assert result.summary is None


def test_hit_within_ttl():
    clock = [1000.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    summary = _summary()
    cache.put("s1", summary)
    clock[0] += 5.0
    result = cache.resolve("s1")
    assert result.status is CacheStatus.HIT
    assert result.summary is summary


def test_just_before_boundary_is_hit():
    clock = [0.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    cache.put("s1", _summary())
    clock[0] = 9.999
    assert cache.resolve("s1").status is CacheStatus.HIT


def test_at_boundary_is_expired():
    # resolve uses >= ttl_seconds, so exactly at the boundary is EXPIRED.
    clock = [0.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    cache.put("s1", _summary())
    clock[0] = 10.0
    result = cache.resolve("s1")
    assert result.status is CacheStatus.EXPIRED
    assert result.summary is None


def test_expired_entry_is_pruned():
    clock = [0.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    cache.put("s1", _summary())
    clock[0] = 50.0
    assert cache.resolve("s1").status is CacheStatus.EXPIRED
    assert len(cache) == 0
    # A second resolve is now a MISS (entry pruned).
    assert cache.resolve("s1").status is CacheStatus.MISS


# ---------------------------------------------------------------------------
# Eviction + replacement
# ---------------------------------------------------------------------------


def test_oldest_evicted_first_past_max_entries():
    cache = ClickSnapshotSummaryCache(max_entries=2)
    cache.put("a", _summary("a"))
    cache.put("b", _summary("b"))
    cache.put("c", _summary("c"))  # evicts "a"
    assert cache.resolve("a").status is CacheStatus.MISS
    assert cache.resolve("b").status is CacheStatus.HIT
    assert cache.resolve("c").status is CacheStatus.HIT
    assert len(cache) == 2


def test_replacement_resets_expiry():
    clock = [0.0]
    cache = ClickSnapshotSummaryCache(
        ttl_seconds=10.0, time_source=lambda: clock[0]
    )
    cache.put("s1", _summary())
    clock[0] = 8.0
    new_summary = _summary()
    cache.put("s1", new_summary)  # resets expiry to t=8
    clock[0] = 16.0  # 8s after the replacement: still fresh
    result = cache.resolve("s1")
    assert result.status is CacheStatus.HIT
    assert result.summary is new_summary


def test_replacement_moves_entry_to_newest_for_eviction():
    cache = ClickSnapshotSummaryCache(max_entries=2)
    cache.put("a", _summary("a"))
    cache.put("b", _summary("b"))
    cache.put("a", _summary("a"))  # refresh -> "a" newest, "b" oldest
    cache.put("c", _summary("c"))  # evicts "b", not "a"
    assert cache.resolve("a").status is CacheStatus.HIT
    assert cache.resolve("b").status is CacheStatus.MISS
    assert cache.resolve("c").status is CacheStatus.HIT


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


def test_clear_drops_everything():
    cache = ClickSnapshotSummaryCache()
    cache.put("a", _summary("a"))
    cache.put("b", _summary("b"))
    cache.clear()
    assert len(cache) == 0
    assert cache.resolve("a").status is CacheStatus.MISS


def test_clear_when_empty_is_safe():
    cache = ClickSnapshotSummaryCache()
    cache.clear()
    assert len(cache) == 0
