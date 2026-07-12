"""Tests for ConsumedTokenSet (wh-82lnx)."""
from __future__ import annotations

import pytest

from services.wheelhouse.shared.consumed_token_set import ConsumedTokenSet


_TOKEN_A = "11111111-1111-4111-8111-111111111111"
_TOKEN_B = "22222222-2222-4222-8222-222222222222"


class TestBasicMembership:
    def test_unknown_token_not_in_set(self):
        s = ConsumedTokenSet()
        assert _TOKEN_A not in s

    def test_added_token_is_in_set(self):
        s = ConsumedTokenSet()
        s.add(_TOKEN_A)
        assert _TOKEN_A in s
        assert _TOKEN_B not in s

    def test_double_add_is_idempotent(self):
        s = ConsumedTokenSet()
        s.add(_TOKEN_A)
        s.add(_TOKEN_A)
        assert _TOKEN_A in s
        assert len(s) == 1


class TestTTLExpiry:
    def test_expired_token_returns_false_and_is_pruned(self):
        clock = [1000.0]
        s = ConsumedTokenSet(ttl_seconds=10.0, time_source=lambda: clock[0])
        s.add(_TOKEN_A)
        assert _TOKEN_A in s
        clock[0] += 100.0
        assert _TOKEN_A not in s
        assert len(s) == 0

    def test_token_within_ttl_still_present(self):
        clock = [1000.0]
        s = ConsumedTokenSet(ttl_seconds=10.0, time_source=lambda: clock[0])
        s.add(_TOKEN_A)
        clock[0] += 5.0
        assert _TOKEN_A in s

    def test_re_add_resets_ttl(self):
        clock = [1000.0]
        s = ConsumedTokenSet(ttl_seconds=10.0, time_source=lambda: clock[0])
        s.add(_TOKEN_A)
        clock[0] += 8.0
        s.add(_TOKEN_A)
        clock[0] += 8.0
        assert _TOKEN_A in s


class TestOverflowEviction:
    def test_oldest_evicted_first(self):
        s = ConsumedTokenSet(max_entries=2)
        s.add("a")
        s.add("b")
        s.add("c")
        assert "a" not in s
        assert "b" in s
        assert "c" in s


class TestConstructorValidation:
    def test_zero_ttl_rejected(self):
        with pytest.raises(ValueError):
            ConsumedTokenSet(ttl_seconds=0.0)

    def test_zero_max_entries_rejected(self):
        with pytest.raises(ValueError):
            ConsumedTokenSet(max_entries=0)
