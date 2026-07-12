"""Tests for EditorPendingRequestMap (wh-g2-refactor.14).

Covers the Logic-side pending-request map that backs both
``insert_editor_word`` and ``retract_editor_text`` (Section 2 and
Section 5 of the G2 design refinements). The map is keyed by
``request_id`` and stores a ``(future, generation)`` tuple so the
rebuild fence (Section 6) can identify and fail stale futures.

Three branches are exercised:

  * Success -- ``register`` returns a future that ``complete``
    resolves, and the producer's awaited result matches the response
    payload.
  * Timeout -- a registered future that nobody completes raises
    ``asyncio.TimeoutError`` when awaited with a deadline, and the
    map's ``cleanup`` (the producer's finally) drops the entry so
    a late response hits the orphan branch.
  * Orphan -- ``complete`` invoked with a request_id that is not in
    the map returns ``False`` and logs nothing-loud (the caller logs
    a warning); a late response that arrives after a future was
    already resolved is also handled without raising.

The rebuild fan-out path (Section 6) is also covered: ``fail_at_or_below``
resolves every stored future whose generation is at or below a given
threshold with a caller-supplied payload, and returns the list of
abandoned request ids so the caller can log a count.
"""

from __future__ import annotations

import asyncio

import pytest

from services.wheelhouse.shared.editor_pending_request import (
    EditorPendingRequestMap,
)


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Success branch
# ---------------------------------------------------------------------------


async def test_register_then_complete_resolves_future():
    pending = EditorPendingRequestMap()
    future = pending.register("rid-1", generation=0)
    assert not future.done()
    resolved = pending.complete("rid-1", {"chars_inserted": 5, "failure_reason": ""})
    assert resolved is True
    result = await asyncio.wait_for(future, timeout=0.1)
    assert result == {"chars_inserted": 5, "failure_reason": ""}


async def test_register_returns_distinct_futures_for_distinct_ids():
    pending = EditorPendingRequestMap()
    f1 = pending.register("rid-1", generation=0)
    f2 = pending.register("rid-2", generation=0)
    assert f1 is not f2
    pending.complete("rid-2", {"value": "second"})
    pending.complete("rid-1", {"value": "first"})
    assert (await f1)["value"] == "first"
    assert (await f2)["value"] == "second"


async def test_register_rejects_duplicate_request_id():
    """A duplicate request id is a programmer error.

    uuid4 hex collisions are astronomically unlikely; if one occurs in
    practice it almost certainly indicates a bug in the producer
    (reusing an id). Raise so the bug surfaces immediately rather than
    silently dropping the first future.
    """
    pending = EditorPendingRequestMap()
    pending.register("rid-1", generation=0)
    with pytest.raises(ValueError):
        pending.register("rid-1", generation=0)


# ---------------------------------------------------------------------------
# Timeout branch
# ---------------------------------------------------------------------------


async def test_unresolved_future_times_out():
    pending = EditorPendingRequestMap()
    future = pending.register("rid-1", generation=0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.05)
    # The producer's finally drops the entry; the test simulates that.
    assert pending.pop("rid-1") is True
    # A late response now hits the orphan branch and returns False.
    assert pending.complete("rid-1", {"value": "late"}) is False


async def test_pop_returns_false_for_unknown_id():
    pending = EditorPendingRequestMap()
    assert pending.pop("rid-never-registered") is False


# ---------------------------------------------------------------------------
# Orphan branch
# ---------------------------------------------------------------------------


async def test_complete_unknown_id_returns_false():
    pending = EditorPendingRequestMap()
    assert pending.complete("rid-unknown", {"value": "x"}) is False


async def test_complete_already_resolved_future_returns_false():
    pending = EditorPendingRequestMap()
    future = pending.register("rid-1", generation=0)
    pending.complete("rid-1", {"value": "first"})
    # A second complete for the same id is a no-op (future already done).
    assert pending.complete("rid-1", {"value": "second"}) is False
    # The first result wins.
    assert (await future)["value"] == "first"


# ---------------------------------------------------------------------------
# Rebuild fan-out branch (Section 6)
# ---------------------------------------------------------------------------


async def test_fail_at_or_below_resolves_stale_futures():
    pending = EditorPendingRequestMap()
    f0 = pending.register("rid-0", generation=0)
    f1 = pending.register("rid-1", generation=1)
    f2 = pending.register("rid-2", generation=2)
    failure_payload = {"chars_inserted": 0, "failure_reason": "editor_rebuilt"}
    abandoned = pending.fail_at_or_below(
        old_generation=1,
        failure_payload=failure_payload,
    )
    assert sorted(abandoned) == ["rid-0", "rid-1"]
    assert (await f0) == failure_payload
    assert (await f1) == failure_payload
    assert not f2.done()


async def test_fail_at_or_below_skips_already_resolved_futures():
    pending = EditorPendingRequestMap()
    f0 = pending.register("rid-0", generation=0)
    pending.complete("rid-0", {"chars_inserted": 5, "failure_reason": ""})
    abandoned = pending.fail_at_or_below(
        old_generation=5,
        failure_payload={"chars_inserted": 0, "failure_reason": "editor_rebuilt"},
    )
    assert abandoned == []
    # The original success result wins.
    assert (await f0) == {"chars_inserted": 5, "failure_reason": ""}


async def test_fail_at_or_below_does_not_pop_entries():
    """The handler does NOT pop; producers' finally blocks do.

    Section 6 specifies this so a late response from the old editor
    still in flight when the rebuild fired finds the future already
    done and is logged-only via the existing late-response path.
    """
    pending = EditorPendingRequestMap()
    pending.register("rid-0", generation=0)
    pending.fail_at_or_below(
        old_generation=0,
        failure_payload={"chars_inserted": 0, "failure_reason": "editor_rebuilt"},
    )
    # Entry is still in the map until the producer's finally pops it.
    assert pending.pop("rid-0") is True


# ---------------------------------------------------------------------------
# Generation accounting
# ---------------------------------------------------------------------------


async def test_get_generation_returns_stored_value():
    pending = EditorPendingRequestMap()
    pending.register("rid-7", generation=42)
    assert pending.get_generation("rid-7") == 42


async def test_get_generation_returns_none_for_unknown_id():
    pending = EditorPendingRequestMap()
    assert pending.get_generation("rid-unknown") is None


async def test_in_flight_count_tracks_registrations():
    pending = EditorPendingRequestMap()
    assert pending.in_flight() == 0
    pending.register("rid-1", generation=0)
    pending.register("rid-2", generation=0)
    assert pending.in_flight() == 2
    pending.complete("rid-1", {})
    # complete does not pop; the map still holds the entry.
    assert pending.in_flight() == 2
    pending.pop("rid-1")
    assert pending.in_flight() == 1
