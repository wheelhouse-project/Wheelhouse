"""Tests for LogicRebuildFanout (wh-g2-refactor.17).

Covers the Logic-side handler that consumes the ``editor_rebuilt``
notification, bumps the observed generation, and fans out failures to
every pending ``insert_editor_word`` / ``retract_editor_text`` future
whose stored generation is at or below the retired generation.

Section 6 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
is the authoritative reference; this module implements its
``_handle_editor_rebuilt`` pseudocode.

Coverage includes the slice's acceptance test: "force a rebuild while
five pending requests are outstanding; all five futures resolve with
RebuildLost." The test registers five futures in the pending map,
runs the handler against an ``editor_rebuilt`` notification, and
asserts every future resolves with a payload that
``RebuildLost.from_payload`` can convert to a ``RebuildLost``
exception with ``failure_reason == "editor_rebuilt"``.
"""

from __future__ import annotations

import asyncio

import pytest

from services.wheelhouse.shared.editor_pending_request import (
    EditorPendingRequestMap,
)
from services.wheelhouse.shared.editor_rebuild import RebuildLost
from services.wheelhouse.shared.editor_rebuilt import (
    ACTION_NAME as EDITOR_REBUILT_ACTION,
    EditorRebuiltNotification,
)
from services.wheelhouse.shared.editor_rebuilt_handler import (
    LogicRebuildFanout,
    build_rebuild_lost_payload,
)
from services.wheelhouse.shared.insert_editor_word import (
    FAILURE_EDITOR_REBUILT as INSERT_FAILURE_EDITOR_REBUILT,
    InsertEditorWordResponse,
)
from services.wheelhouse.shared.retract_editor_text import (
    RetractEditorTextResponse,
)


# The asyncio mark is applied per-test below rather than at module
# scope so the synchronous "canonical fan-out payload" assertions can
# run without an event loop. Mixing async and sync in the same file
# is fine; pytestmark = pytest.mark.asyncio would warn on the sync
# ones.


def _make_notification_payload(old: int, new: int, reason: str = "r") -> dict:
    return EditorRebuiltNotification(
        old_generation=old, new_generation=new, reason=reason,
    ).to_dict()


# ---------------------------------------------------------------------------
# Canonical fan-out payload (synchronous)
# ---------------------------------------------------------------------------


def test_canonical_payload_carries_every_required_key():
    """The fan-out payload must satisfy both producers' post-await reads.

    Insert producer reads ``chars_inserted`` and ``failure_reason``.
    Retract producer reads ``chars_requested``, ``chars_removed``,
    ``replay_chars``, and ``failure_reason``.
    """
    payload = build_rebuild_lost_payload()
    for key in (
        "chars_inserted",
        "chars_requested",
        "chars_removed",
        "replay_chars",
        "failure_reason",
    ):
        assert key in payload, f"fan-out payload missing key {key!r}"
    assert payload["failure_reason"] == INSERT_FAILURE_EDITOR_REBUILT


def test_canonical_payload_passes_insert_response_validator():
    """The synthetic payload must pass the insert response validator.

    The pending-request map fans the SAME payload out to every
    waiting future; if the validator rejects it on the insert side,
    test code that constructs an ``InsertEditorWordResponse`` from the
    fan-out result would crash. (The producer skips that construction
    on the rebuild branch in practice, but the schema-clean check is
    still a useful regression fence.)
    """
    payload = build_rebuild_lost_payload()
    response = InsertEditorWordResponse(
        request_id="abc" * 4,
        chars_inserted=payload["chars_inserted"],
        failure_reason=payload["failure_reason"],
    )
    assert response.failure_reason == INSERT_FAILURE_EDITOR_REBUILT


def test_canonical_payload_carries_retract_abandon_sentinel():
    """``chars_requested == -1`` is the abandon-path sentinel.

    ``RetractEditorTextResponse`` allows ``chars_requested >= -1`` so
    the synthetic payload can flag "request was abandoned" without
    pretending to echo a positive value. The retract producer's
    boundary-mismatch check skips its comparison on this sentinel.
    """
    payload = build_rebuild_lost_payload()
    assert payload["chars_requested"] == -1
    # The retract response validator accepts -1 in non-success paths.
    response = RetractEditorTextResponse(
        request_id="abc" * 4,
        chars_requested=payload["chars_requested"],
        chars_removed=payload["chars_removed"],
        replay_chars=payload["replay_chars"],
        failure_reason=payload["failure_reason"],
    )
    assert response.chars_requested == -1


def test_canonical_payload_is_a_fresh_dict_each_call():
    """The builder must NOT return a shared mutable singleton."""
    a = build_rebuild_lost_payload()
    b = build_rebuild_lost_payload()
    assert a is not b
    a["chars_inserted"] = 999
    assert b["chars_inserted"] == 0


# ---------------------------------------------------------------------------
# Acceptance criterion: five pending requests resolve with RebuildLost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_with_five_pending_requests_resolves_all_as_rebuild_lost():
    """Slice acceptance criterion: a rebuild fired while five pending
    requests are outstanding leaves no in-flight requests hanging.

    All five futures resolve with a payload that ``RebuildLost.from_payload``
    can convert to a ``RebuildLost`` exception with ``failure_reason ==
    "editor_rebuilt"``.
    """
    pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(pending_maps=[pending], initial_generation=0)

    futures = [
        pending.register(f"rid-{i}", generation=0) for i in range(5)
    ]
    assert pending.in_flight() == 5
    assert all(not f.done() for f in futures)

    consumed = handler.handle_notification(
        _make_notification_payload(old=0, new=1, reason="rdp_reconnect"),
    )
    assert consumed is True
    assert handler.observed_generation == 1

    # Every future must be resolved.
    results = [await asyncio.wait_for(f, timeout=0.1) for f in futures]
    for payload in results:
        rebuild_lost = RebuildLost.from_payload(
            payload,
            old_generation=0,
            new_generation=1,
            reason="rdp_reconnect",
        )
        assert isinstance(rebuild_lost, RebuildLost), (
            f"payload {payload!r} did not match RebuildLost shape"
        )
        assert rebuild_lost.failure_reason == "editor_rebuilt"


# ---------------------------------------------------------------------------
# Fan-out across multiple pending maps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_fans_out_across_both_pending_maps():
    """The Logic integration carries two maps (insert + retract).

    The handler must invoke fail_at_or_below on each.
    """
    insert_pending = EditorPendingRequestMap()
    retract_pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(
        pending_maps=[insert_pending, retract_pending],
        initial_generation=0,
    )

    insert_futures = [
        insert_pending.register(f"ins-{i}", generation=0) for i in range(3)
    ]
    retract_futures = [
        retract_pending.register(f"ret-{i}", generation=0) for i in range(2)
    ]

    handler.handle_notification(
        _make_notification_payload(old=0, new=1, reason="r"),
    )

    for f in insert_futures + retract_futures:
        result = await asyncio.wait_for(f, timeout=0.1)
        assert result["failure_reason"] == "editor_rebuilt"


@pytest.mark.asyncio
async def test_handler_only_fails_futures_at_or_below_retired_generation():
    """A future stamped with the NEW generation must NOT be failed."""
    pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(pending_maps=[pending], initial_generation=0)

    stale = pending.register("stale", generation=0)
    fresh = pending.register("fresh", generation=1)

    handler.handle_notification(
        _make_notification_payload(old=0, new=1, reason="r"),
    )

    result = await asyncio.wait_for(stale, timeout=0.1)
    assert result["failure_reason"] == "editor_rebuilt"
    assert not fresh.done(), (
        "future stamped with the new generation must survive the fan-out"
    )


# ---------------------------------------------------------------------------
# Idempotency: delivered exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_notification_does_not_double_apply():
    """The slice spec: ``editor_rebuilt`` is delivered exactly once per rebuild.

    The handler defensively ignores a duplicate delivery (same
    (old, new) key) so a double-send from a misbehaving GUI cannot
    re-resolve newly-registered futures with the rebuild payload.
    """
    pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(pending_maps=[pending], initial_generation=0)

    f1 = pending.register("rid-1", generation=0)
    notif = _make_notification_payload(old=0, new=1, reason="r")
    assert handler.handle_notification(notif) is True
    await asyncio.wait_for(f1, timeout=0.1)

    # A second future registered AFTER the first delivery is stamped
    # with generation=1 (the new one) so it would survive any fan-out.
    # Re-deliver the same notification: the duplicate must be a no-op.
    f2 = pending.register("rid-2", generation=1)
    assert handler.handle_notification(notif) is False
    assert not f2.done()


# ---------------------------------------------------------------------------
# Observed-generation accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observed_generation_advances_on_notification():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=5)
    assert handler.observed_generation == 5
    handler.handle_notification(
        _make_notification_payload(old=5, new=6, reason="r"),
    )
    assert handler.observed_generation == 6


@pytest.mark.asyncio
async def test_observed_generation_does_not_regress_on_older_notification():
    """If the handler somehow sees an out-of-order notification carrying a
    smaller ``new_generation`` than the current observed value, the
    counter must NOT regress AND the notification must be dropped
    (return False) without resolving any pending futures. Round 1 /
    gemini finding wh-g2-refactor.29.4 sub-point 1: the prior test did
    not pin the side-effect on the stale path; this revision does.
    """
    pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(pending_maps=[pending], initial_generation=10)
    # Register a future stamped with a generation already eligible for
    # cleanup by the time observed=10 reached its current value. A
    # winning notification at gen 10 would have failed it; the stale
    # notification below must not re-resolve it.
    f = pending.register("rid-stale", generation=4)

    result = handler.handle_notification(
        _make_notification_payload(old=3, new=4, reason="r"),
    )

    assert result is False
    assert handler.observed_generation == 10
    # The stale notification does not run fan-out -- the winning
    # notification that drove observed to 10 was responsible for
    # cleaning up <=10 futures. The future remains in whatever state
    # it was in before; in this test no winning notification ran, so
    # it is still pending. The point of the assertion is that the
    # stale notification did not re-fail it.
    assert not f.done()


@pytest.mark.asyncio
async def test_handler_with_empty_pending_maps_still_processes_notification():
    """Round 1 / gemini finding wh-g2-refactor.29.4 sub-point 2:
    explicit coverage for the no-maps case. The notification advances
    ``observed_generation`` and returns True even when there are no
    pending maps to fan out over.
    """
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    result = handler.handle_notification(
        _make_notification_payload(old=0, new=1, reason="r"),
    )
    assert result is True
    assert handler.observed_generation == 1


# ---------------------------------------------------------------------------
# Schema-error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_drops_malformed_notification():
    """Malformed payloads degrade gracefully (wh-uf54)."""
    pending = EditorPendingRequestMap()
    handler = LogicRebuildFanout(pending_maps=[pending], initial_generation=0)
    f = pending.register("rid-1", generation=0)

    # Missing required field.
    bad_payload = {
        "action": EDITOR_REBUILT_ACTION,
        "old_generation": 0,
        "new_generation": 1,
        # 'reason' missing
    }
    assert handler.handle_notification(bad_payload) is False
    # Future must NOT be resolved.
    assert not f.done()


@pytest.mark.asyncio
async def test_handler_drops_payload_with_wrong_action():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    bad = {
        "action": "te_event_ack",
        "old_generation": 0,
        "new_generation": 1,
        "reason": "r",
    }
    assert handler.handle_notification(bad) is False


@pytest.mark.asyncio
async def test_handler_drops_non_mapping_payload():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    assert handler.handle_notification([1, 2, 3]) is False


# ---------------------------------------------------------------------------
# is_rebuild_lost helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_rebuild_lost_returns_true_for_editor_rebuilt():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    payload = build_rebuild_lost_payload()
    assert handler.is_rebuild_lost(payload) is True


@pytest.mark.asyncio
async def test_is_rebuild_lost_returns_false_for_success():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    assert handler.is_rebuild_lost({"failure_reason": ""}) is False


@pytest.mark.asyncio
async def test_is_rebuild_lost_returns_true_for_stale_generation():
    """Round 1 / deepseek finding wh-g2-refactor.30.1: a stale_generation
    response is also a rebuild fence (per-request GUI rejection rather
    than bulk fan-out, but semantically equivalent at the producer
    level). ``is_rebuild_lost`` must match both reasons so a producer
    using it as the rebuild-abandonment gate sees both paths.
    """
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    assert handler.is_rebuild_lost(
        {"failure_reason": "stale_generation"},
    ) is True


@pytest.mark.asyncio
async def test_is_rebuild_lost_returns_false_for_other_failure_reasons():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    assert handler.is_rebuild_lost(
        {"failure_reason": "no_active_session"},
    ) is False


@pytest.mark.asyncio
async def test_is_rebuild_lost_returns_false_for_non_mapping():
    handler = LogicRebuildFanout(pending_maps=[], initial_generation=0)
    assert handler.is_rebuild_lost(["nope"]) is False
