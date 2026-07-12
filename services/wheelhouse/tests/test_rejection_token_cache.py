"""Tests for the Logic-side rejection token cache (wh-iycks).

The Logic process owns this cache. It maps a correlation_token (uuid4
string) to the identifying RejectionTuple from a text_target_rejected
event so that a later try_anyway_clicked event can resolve to the
original rejection's process/class/control_type without that text ever
crossing processes.

Coverage mirrors the Input-side ``test_rejection_text_cache``:
  * put + resolve round-trip (HIT).
  * unknown token -> MISS.
  * expired entry -> EXPIRED on access (and is pruned).
  * max_entries enforced -- oldest evicted on overflow -> MISS.
  * repeated put with the same token replaces the value and resets
    the TTL.
  * keys() reports active tokens (not expired ones).
  * Privacy property: RejectionTuple has no dictation-text field.
"""

from __future__ import annotations

import dataclasses

import pytest

from shared.rejection_token_cache import (
    CacheResult,
    CacheStatus,
    RejectionTokenCache,
    RejectionTuple,
)


class _FakeClock:
    """Manually advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tuple(suffix: str = "1") -> RejectionTuple:
    return RejectionTuple(
        process_name=f"zed.exe",
        class_name=f"Zed::Window-{suffix}",
        control_type="WindowControl",
        app_friendly_name="Zed Editor",
    )


# ---------------------------------------------------------------------------
# Basic put/resolve
# ---------------------------------------------------------------------------


def test_put_then_resolve_returns_hit():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    payload = _tuple("a")
    cache.put("tok-1", payload)

    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.tuple_ == payload


def test_resolve_returns_miss_for_unknown_token():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    result = cache.resolve("never-stored")
    assert result.status is CacheStatus.MISS
    assert result.tuple_ is None


def test_resolve_returns_expired_for_ttl_elapsed():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", _tuple())
    clock.advance(60.001)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.EXPIRED
    assert result.tuple_ is None


def test_entry_just_inside_ttl_is_returned():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    payload = _tuple()
    cache.put("tok-1", payload)
    clock.advance(59.999)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.tuple_ == payload


def test_resolve_prunes_expired_entry():
    """An EXPIRED resolve removes the entry so a follow-up resolve returns MISS."""

    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", _tuple())
    clock.advance(61.0)
    first = cache.resolve("tok-1")
    second = cache.resolve("tok-1")
    assert first.status is CacheStatus.EXPIRED
    assert second.status is CacheStatus.MISS


# ---------------------------------------------------------------------------
# max_entries / eviction
# ---------------------------------------------------------------------------


def test_max_entries_evicts_oldest():
    clock = _FakeClock()
    cache = RejectionTokenCache(
        ttl_seconds=60.0, max_entries=3, time_source=clock,
    )
    cache.put("a", _tuple("a"))
    clock.advance(0.001)
    cache.put("b", _tuple("b"))
    clock.advance(0.001)
    cache.put("c", _tuple("c"))
    clock.advance(0.001)
    cache.put("d", _tuple("d"))  # evicts 'a'

    assert cache.resolve("a").status is CacheStatus.MISS
    assert cache.resolve("b").status is CacheStatus.HIT
    assert cache.resolve("c").status is CacheStatus.HIT
    assert cache.resolve("d").status is CacheStatus.HIT


def test_resolve_returns_miss_for_evicted_token():
    """Eviction under max_entries pressure is reported as MISS, not EXPIRED."""

    clock = _FakeClock()
    cache = RejectionTokenCache(
        ttl_seconds=60.0, max_entries=2, time_source=clock,
    )
    cache.put("a", _tuple("a"))
    cache.put("b", _tuple("b"))
    cache.put("c", _tuple("c"))  # evicts 'a'
    assert cache.resolve("a").status is CacheStatus.MISS


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


def test_repeated_put_replaces_value():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    first = _tuple("first")
    second = _tuple("second")
    cache.put("tok-1", first)
    cache.put("tok-1", second)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.tuple_ == second


def test_repeated_put_resets_ttl():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", _tuple("first"))
    clock.advance(50.0)
    cache.put("tok-1", _tuple("second"))
    clock.advance(20.0)  # 70s after first put, 20s after second
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.tuple_.class_name.endswith("second")


# ---------------------------------------------------------------------------
# keys()
# ---------------------------------------------------------------------------


def test_keys_lists_active_tokens_only():
    clock = _FakeClock()
    cache = RejectionTokenCache(ttl_seconds=60.0, time_source=clock)
    cache.put("a", _tuple("a"))
    clock.advance(30.0)
    cache.put("b", _tuple("b"))
    clock.advance(35.0)  # a is 65s old (expired), b is 35s old (alive)
    keys = cache.keys()
    assert "a" not in keys
    assert "b" in keys


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_ttl_is_sixty_seconds():
    """The bead spec calls for a 60-second TTL; the default should match."""

    cache = RejectionTokenCache()
    assert cache.ttl_seconds == 60.0


def test_default_time_source_uses_monotonic():
    """Without a custom time source, the cache uses time.monotonic."""

    cache = RejectionTokenCache()
    cache.put("tok-1", _tuple())
    assert cache.resolve("tok-1").status is CacheStatus.HIT


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_cache_result_is_immutable():
    result = CacheResult(CacheStatus.HIT, _tuple())
    with pytest.raises(Exception):
        result.tuple_ = None  # type: ignore[misc]


def test_rejection_tuple_is_immutable():
    payload = _tuple()
    with pytest.raises(Exception):
        payload.process_name = "other.exe"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Privacy property -- tuple does NOT carry dictation text
# ---------------------------------------------------------------------------


def test_rejection_tuple_has_no_text_field():
    """Privacy property: the Logic-side cache value carries no dictation text.

    This is the structural fence for the wh-x4mv.2 round-2 privacy
    contract on the Logic side. A reviewer who is tempted to add a
    text field to RejectionTuple has to delete this assertion first,
    which surfaces the privacy decision.
    """

    field_names = {f.name for f in dataclasses.fields(RejectionTuple)}
    assert field_names == {
        "process_name",
        "class_name",
        "control_type",
        "app_friendly_name",
    }
    forbidden = {"text", "dictation", "transcript", "utterance", "content"}
    assert field_names.isdisjoint(forbidden)
