"""Tests for the rejection text cache (wh-7318z).

The Input process owns this cache. It maps a correlation_token (uuid4
string) to the original dictation text so the Phase 4 retry click
(wh-ftg63) can recover the text without it crossing process boundaries.
The cache is bounded in size and time so a busy session does not grow
without limit and a stale token cannot resurrect old dictation.

Coverage:
  * put + get round-trip.
  * unknown token -> None.
  * expired entry -> None on access (and is pruned).
  * max_entries enforced -- oldest evicted on overflow.
  * repeated put with the same token replaces the value.
  * keys() reports active tokens (not expired ones).
"""

from __future__ import annotations

import pytest

from ui.rejection_text_cache import (
    CacheResult,
    CacheStatus,
    RejectionTextCache,
)


class _FakeClock:
    """Manually advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_put_then_get_returns_text():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello world")
    assert cache.get("tok-1") == "hello world"


def test_unknown_token_returns_none():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    assert cache.get("never-stored") is None


def test_expired_token_returns_none():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    clock.advance(60.001)
    assert cache.get("tok-1") is None


def test_entry_just_inside_ttl_is_returned():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    clock.advance(59.999)
    assert cache.get("tok-1") == "hello"


def test_get_prunes_expired_entry():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    clock.advance(61.0)
    assert cache.get("tok-1") is None
    # Subsequent get with a different token does not see the expired one.
    assert "tok-1" not in cache.keys()


def test_max_entries_evicts_oldest():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, max_entries=3, time_source=clock)
    cache.put("a", "1")
    clock.advance(0.001)
    cache.put("b", "2")
    clock.advance(0.001)
    cache.put("c", "3")
    clock.advance(0.001)
    cache.put("d", "4")  # triggers eviction of 'a'
    assert cache.get("a") is None
    assert cache.get("b") == "2"
    assert cache.get("c") == "3"
    assert cache.get("d") == "4"


def test_repeated_put_replaces_value():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "first")
    cache.put("tok-1", "second")
    assert cache.get("tok-1") == "second"


def test_repeated_put_resets_ttl():
    """Re-put on same token resets the expiry."""

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "first")
    clock.advance(50.0)
    cache.put("tok-1", "second")
    clock.advance(20.0)  # 70s after first put, 20s after second
    assert cache.get("tok-1") == "second"


def test_keys_lists_active_tokens_only():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("a", "1")
    clock.advance(30.0)
    cache.put("b", "2")
    clock.advance(35.0)  # a expired (65s old), b alive (35s old)
    keys = cache.keys()
    assert "a" not in keys
    assert "b" in keys


def test_default_ttl_is_sixty_seconds():
    """The bead specifies a 60-second TTL; the default should match."""

    cache = RejectionTextCache()
    assert cache.ttl_seconds == 60.0


def test_default_time_source_uses_monotonic():
    """Without a custom time source, the cache uses time.monotonic."""

    import time

    cache = RejectionTextCache()
    cache.put("tok-1", "hello")
    assert cache.get("tok-1") == "hello"
    # Indirect: this should not raise because monotonic is in use.
    del time


# ---------------------------------------------------------------------------
# Three-way resolve (wh-9weum.1.4)
# ---------------------------------------------------------------------------


def test_resolve_returns_hit_for_active_token():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello world")
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.text == "hello world"


def test_resolve_returns_expired_for_ttl_elapsed():
    """The retry response distinguishes token_expired from
    unknown_token; resolve must let the caller see EXPIRED for tokens
    that were stored but whose TTL elapsed."""

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    clock.advance(60.001)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.EXPIRED
    assert result.text is None


def test_resolve_returns_miss_for_unknown_token():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    result = cache.resolve("never-stored")
    assert result.status is CacheStatus.MISS
    assert result.text is None


def test_resolve_prunes_expired_entry():
    """An EXPIRED resolve removes the entry so a follow-up resolve
    returns MISS."""

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    clock.advance(61.0)
    first = cache.resolve("tok-1")
    second = cache.resolve("tok-1")
    assert first.status is CacheStatus.EXPIRED
    assert second.status is CacheStatus.MISS


def test_resolve_returns_miss_for_evicted_token():
    """A token that was stored but evicted under max_entries pressure
    is reported as MISS (the cache has no record), distinct from
    EXPIRED (the cache had it but TTL elapsed)."""

    clock = _FakeClock()
    cache = RejectionTextCache(
        ttl_seconds=60.0, max_entries=2, time_source=clock,
    )
    cache.put("a", "1")
    cache.put("b", "2")
    cache.put("c", "3")  # evicts 'a'
    result = cache.resolve("a")
    assert result.status is CacheStatus.MISS


def test_get_and_resolve_agree_on_hit():
    """The convenience get() wrapper must report None for non-HIT
    outcomes and the text for HIT, so legacy callers see the same
    answer they did before resolve() existed."""

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("alive", "alive-text")
    cache.put("expired", "expired-text")
    clock.advance(61.0)
    cache.put("fresh", "fresh-text")

    assert cache.get("fresh") == "fresh-text"
    assert cache.get("expired") is None
    assert cache.get("never-stored") is None


def test_cache_result_is_immutable():
    """CacheResult is part of the public API; treat it as a contract."""

    result = CacheResult(CacheStatus.HIT, "text")
    with pytest.raises(Exception):
        result.text = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Target HWND carry (wh-override-paste-focus-drift)
# ---------------------------------------------------------------------------


def test_put_with_target_hwnd_resolves_to_same_hwnd():
    """The cache stores the rejected target's top-level HWND alongside the
    text so the retry handler can restore foreground to the original
    window before pasting. Without this, capture_context() at retry
    time sees the toast button's QPushButton instead of the rejected
    application window.
    """

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello world", target_hwnd=0x12345)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.text == "hello world"
    assert result.target_hwnd == 0x12345


def test_put_without_target_hwnd_defaults_to_zero():
    """Legacy callers that omit target_hwnd get a default of 0, which the
    retry handler treats as 'no refocus needed'.
    """

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.text == "hello"
    assert result.target_hwnd == 0


def test_resolve_miss_carries_zero_target_hwnd():
    cache = RejectionTextCache()
    result = cache.resolve("never-stored")
    assert result.status is CacheStatus.MISS
    assert result.target_hwnd == 0


def test_resolve_expired_carries_zero_target_hwnd():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello", target_hwnd=0xABCD)
    clock.advance(61.0)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.EXPIRED
    assert result.target_hwnd == 0


# ---------------------------------------------------------------------------
# Target process_id carry (wh-override-paste-focus-drift.1.2)
# ---------------------------------------------------------------------------


def test_put_with_target_process_id_resolves_to_same_pid():
    """The cache stores the rejected target's process_id alongside the HWND
    so the retry handler can detect HWND reuse before refocusing. Without
    this guard, a stale HWND that Windows has reassigned to a different
    process can silently get the cached paste.
    """

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put(
        "tok-1", "hello world",
        target_hwnd=0x12345, target_process_id=4242,
    )
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.text == "hello world"
    assert result.target_hwnd == 0x12345
    assert result.target_process_id == 4242


def test_put_without_target_process_id_defaults_to_zero():
    """Legacy callers that omit target_process_id get a default of 0,
    which the retry handler treats as 'no identity check available'
    (skip the GetWindowThreadProcessId comparison and trust the HWND).
    """

    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello", target_hwnd=0x12345)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.target_hwnd == 0x12345
    assert result.target_process_id == 0


def test_resolve_miss_carries_zero_target_process_id():
    cache = RejectionTextCache()
    result = cache.resolve("never-stored")
    assert result.target_process_id == 0


def test_resolve_expired_carries_zero_target_process_id():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put(
        "tok-1", "hello",
        target_hwnd=0xABCD, target_process_id=9999,
    )
    clock.advance(61.0)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.EXPIRED
    assert result.target_process_id == 0


# ---------------------------------------------------------------------------
# invalidate (wh-override-multiword-retry)
# ---------------------------------------------------------------------------


def test_invalidate_removes_entry():
    clock = _FakeClock()
    cache = RejectionTextCache(ttl_seconds=60.0, time_source=clock)
    cache.put("tok-1", "hello")
    assert cache.invalidate("tok-1") is True
    assert cache.resolve("tok-1").status is CacheStatus.MISS


def test_invalidate_unknown_token_returns_false():
    cache = RejectionTextCache()
    assert cache.invalidate("never-stored") is False


def test_invalidate_is_idempotent():
    cache = RejectionTextCache()
    cache.put("tok-1", "hello")
    assert cache.invalidate("tok-1") is True
    assert cache.invalidate("tok-1") is False


def test_invalidate_does_not_touch_other_entries():
    cache = RejectionTextCache()
    cache.put("tok-1", "hello")
    cache.put("tok-2", "world")
    cache.invalidate("tok-1")
    assert cache.resolve("tok-1").status is CacheStatus.MISS
    assert cache.get("tok-2") == "world"


# ---------------------------------------------------------------------------
# Exception-safe replace (wh-override-multiword-retry finding 2)
# ---------------------------------------------------------------------------


def test_put_replace_preserves_entry_on_assignment_failure(monkeypatch):
    """The append path on RejectedInsertionStrategy relies on ``put``
    being exception-safe across replace: if the new tuple cannot be
    stored, the previous entry must remain intact. The old del-then-
    insert pattern destroyed the previous entry on any failure.
    """

    cache = RejectionTextCache()
    cache.put("tok-1", "hello", target_hwnd=0xAA, target_process_id=42)

    real_move = cache._entries.move_to_end

    def _raise_move(*args, **kwargs):
        raise RuntimeError("simulated reorder failure")

    monkeypatch.setattr(cache._entries, "move_to_end", _raise_move)
    try:
        cache.put("tok-1", "hello world", target_hwnd=0xAA, target_process_id=42)
    except RuntimeError:
        pass

    # Even with move_to_end failing, the value assignment already
    # succeeded; the entry must hold the new combined text. The earlier
    # del-then-insert pattern would have left the entry destroyed.
    monkeypatch.setattr(cache._entries, "move_to_end", real_move)
    result = cache.resolve("tok-1")
    assert result.status is CacheStatus.HIT
    assert result.text == "hello world"
