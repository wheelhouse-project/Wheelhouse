"""Tests for the StartOverlayWalkResponse IPC schema (wh-n29v.37).

The schema defines the Input -> Logic reply for the ``start_overlay_walk``
action (Phase 1.5 of the voice-element-clicking feature, epic wh-l4h.1).
``start_overlay_walk`` walks the focused window FROM SCRATCH (no prior
``click_element`` request), so its authoritative ``outcome`` set deliberately
OMITS ``snapshot_expired`` -- a fresh walk has no input snapshot to expire.

Like ``ShowNumberedOverlayResponse`` / ``ClickElementResponse``, this is a
request-correlated Schema A response (handler-owned, correlated by request_id
via the ``_HANDLES_OWN_RESPONSE`` machinery), not a type-routed unsolicited
event.

Coverage:
  * Round-trip (to_dict -> from_dict) for every outcome literal:
    ok (with a populated summary), no_targets, execution_failed, error.
  * The echoed overlay_session_id / paint_generation / trace_id survive the
    round-trip.
  * Nested snapshot_summary item bounds normalization: a list becomes a tuple.
  * The status/outcome split mapping (outcome=ok -> status=ok; any non-ok
    outcome still rides transport status=ok; status=error is reserved for a
    handler-level crash/degrade).
  * from_dict raises StartOverlayWalkResponseSchemaError on every malformed
    shape (not a mapping, missing/wrong-type fields, status/outcome outside
    its closed set, malformed nested summary) -- never an unhandled
    KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
    StartOverlayWalkResponseSchemaError,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary(num_items: int = 2) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"uia-{i}",
            display_number=i,
            name=f"Item {i}",
            role="Button",
            bounds=(10 * i, 20 * i, 80 + i, 30 + i),
            monitor_id=i % 2,
        )
        for i in range(1, num_items + 1)
    ]
    return WalkSnapshotSummary(
        snapshot_id="walk-1",
        items=items,
        created_at_monotonic=123.456,
    )


def _ok_response() -> StartOverlayWalkResponse:
    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id="walk-1",
        snapshot_summary=_summary(3),
        trace_id="trace-1",
        overlay_session_id=7,
        paint_generation=2,
    )


# ---------------------------------------------------------------------------
# Round-trip for every outcome literal
# ---------------------------------------------------------------------------


def test_round_trip_ok_with_populated_summary():
    resp = _ok_response()
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "ok"
    assert restored.status == "ok"
    assert restored.snapshot_summary is not None
    assert len(restored.snapshot_summary.items) == 3
    assert restored.overlay_session_id == 7
    assert restored.paint_generation == 2
    assert restored.trace_id == "trace-1"


def test_round_trip_no_targets():
    resp = StartOverlayWalkResponse(
        status="ok",
        outcome="no_targets",
        reason=None,
        snapshot_id="walk-2",
        snapshot_summary=None,
        trace_id="trace-2",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "no_targets"
    assert restored.status == "ok"


def test_round_trip_no_targets_with_empty_summary():
    # The schema permits no_targets to carry an empty-items summary too.
    resp = StartOverlayWalkResponse(
        status="ok",
        outcome="no_targets",
        reason=None,
        snapshot_id="walk-2b",
        snapshot_summary=WalkSnapshotSummary(
            snapshot_id="walk-2b", items=[], created_at_monotonic=5.0
        ),
        trace_id="trace-2b",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.items == []


def test_round_trip_execution_failed():
    # A failure outcome carries no snapshot (reviewer_1 finding 39.3): the
    # handler emits execution_failed with snapshot_id=None and summary=None.
    resp = StartOverlayWalkResponse(
        status="ok",
        outcome="execution_failed",
        reason="walk_deadline_exceeded",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="trace-3",
        overlay_session_id=2,
        paint_generation=1,
    )
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "execution_failed"
    assert restored.reason == "walk_deadline_exceeded"
    # A feature failure still rides transport status=ok.
    assert restored.status == "ok"


def test_round_trip_error_status():
    resp = StartOverlayWalkResponse(
        status="error",
        outcome="error",
        reason="unexpected",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="trace-4",
        overlay_session_id=3,
        paint_generation=4,
    )
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.status == "error"
    assert restored.outcome == "error"


def test_bounds_list_normalizes_to_tuple():
    payload = {
        "status": "ok",
        "outcome": "ok",
        "reason": None,
        "snapshot_id": "walk-1",
        "snapshot_summary": {
            "snapshot_id": "walk-1",
            "created_at_monotonic": 1.0,
            "items": [
                {
                    "item_id": "uia-1",
                    "display_number": 1,
                    "name": "Item 1",
                    "role": "Button",
                    "bounds": [1, 2, 3, 4],  # list on the wire
                    "monitor_id": 0,
                }
            ],
        },
        "trace_id": "t",
        "overlay_session_id": 1,
        "paint_generation": 0,
    }
    restored = StartOverlayWalkResponse.from_dict(payload)
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.items[0].bounds == (1, 2, 3, 4)
    assert isinstance(restored.snapshot_summary.items[0].bounds, tuple)


# ---------------------------------------------------------------------------
# Outcome literal set is exactly {ok, no_targets, execution_failed, error}
# and OMITS snapshot_expired.
# ---------------------------------------------------------------------------


def test_snapshot_expired_outcome_is_rejected():
    # snapshot_expired is valid for show_numbered_overlay but NOT here: a
    # fresh walk has no input snapshot to expire.
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(
            {
                "status": "ok",
                "outcome": "snapshot_expired",
                "reason": None,
                "snapshot_id": "walk-1",
                "snapshot_summary": None,
                "trace_id": "t",
                "overlay_session_id": 1,
                "paint_generation": 0,
            }
        )


# Per-outcome CROSS-FIELD-VALID envelopes. The closed-set membership of the
# outcome literal is what this asserts, so each literal is paired with the
# envelope the handler actually emits for it (reviewer_1 finding 39.2 added the
# cross-field rules, so a bare snapshot_id=None / summary=None envelope is no
# longer valid for ok or error).
_VALID_ENVELOPE_BY_OUTCOME = {
    # ok / no_targets ride status=ok and carry a snapshot + matching summary.
    "ok": ("ok", "walk-1", _summary(1)),
    "no_targets": ("ok", "walk-1", WalkSnapshotSummary(
        snapshot_id="walk-1", items=[], created_at_monotonic=1.0)),
    # execution_failed rides status=ok with no snapshot.
    "execution_failed": ("ok", None, None),
    # error is the only status=error case.
    "error": ("error", None, None),
}


@pytest.mark.parametrize(
    "outcome", ["ok", "no_targets", "execution_failed", "error"]
)
def test_each_outcome_literal_accepted(outcome):
    status, snapshot_id, summary = _VALID_ENVELOPE_BY_OUTCOME[outcome]
    resp = StartOverlayWalkResponse(
        status=status,
        outcome=outcome,
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=summary,
        trace_id="t",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = StartOverlayWalkResponse.from_dict(resp.to_dict())
    assert restored.outcome == outcome


# ---------------------------------------------------------------------------
# Cross-field invariants (reviewer_1 finding 39.2). The per-field parsing
# validates each field in isolation; these rules reject payloads whose field
# COMBINATION the handler could never emit, so a malformed Input payload cannot
# cross the graceful-degrade boundary looking trustworthy. The handler's only
# emit combinations are:
#   (status=ok,    outcome=ok,              snapshot_id set, summary set)
#   (status=ok,    outcome=no_targets,      snapshot_id set, summary set)
#   (status=ok,    outcome=execution_failed, snapshot_id=None, summary=None)
#   (status=error, outcome=error,           snapshot_id=None, summary=None)
# ---------------------------------------------------------------------------


def test_status_ok_with_outcome_error_raises():
    # Rule (a): status is "error" iff outcome is "error". status=ok+outcome=error
    # is a mismatch the handler never emits (an unexpected error sets BOTH).
    payload = _ok_response().to_dict()
    payload["outcome"] = "error"
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_status_error_with_outcome_ok_raises():
    # Rule (a), the other direction: status=error with a non-error outcome.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_ok_with_none_snapshot_id_raises():
    # Rule (b): outcome=ok REQUIRES a non-empty snapshot_id (the overlay paints a
    # snapshot and a later click_snapshot_item keys on its id).
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = None
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_ok_with_empty_snapshot_id_raises():
    # Rule (b): an empty-string snapshot_id is parseable as a str but is not a
    # usable id, so ok must still reject it.
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = ""
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_ok_with_none_summary_raises():
    # Rule (b): outcome=ok REQUIRES a non-None summary -- the badges to paint.
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = None
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_ok_summary_snapshot_id_disagreement_raises():
    # Rule (c): when both the top-level snapshot_id and a summary are present,
    # they must name the SAME snapshot -- Logic keys the retained summary by the
    # top-level id, so a disagreement would mis-key "click N".
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = "walk-99"  # summary still says walk-1
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_no_targets_with_none_summary_still_accepted():
    # Design boundary: the cross-field presence rule is scoped to outcome=ok
    # ONLY, matching the finding's wording and the schema docstring, which
    # documents that no_targets may carry an empty-items summary OR None. So a
    # no_targets payload with a None summary stays ACCEPTED (it is not the ok
    # case rule (b) constrains). This guards against over-tightening rule (b) to
    # {ok, no_targets}, which would reject a documented-legal shape.
    payload = _ok_response().to_dict()
    payload["outcome"] = "no_targets"
    payload["snapshot_summary"] = None
    restored = StartOverlayWalkResponse.from_dict(payload)
    assert restored.outcome == "no_targets"
    assert restored.snapshot_summary is None


def test_no_targets_with_populated_summary_raises():
    # Rule (e) (wh-overlay-no-targets-summary-guard): no_targets means the walk
    # found nothing clickable, so a POPULATED-items summary contradicts the
    # outcome. The real producer emits no_targets exclusively with an
    # empty-items summary (element_finder.overlay_walk sets no_targets iff
    # matches is empty), so a populated-items no_targets could not have come
    # from it and must not cross the boundary as trustworthy data. The
    # empty-items-or-None flexibility stays (sibling test above).
    payload = _ok_response().to_dict()  # summary carries 3 items; ids agree
    payload["outcome"] = "no_targets"
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_execution_failed_with_snapshot_id_raises():
    # Rule (d) (reviewer_1 finding 39.3): a failure outcome carries NO snapshot.
    # The handler emits execution_failed only with snapshot_id=None and
    # summary=None, so a snapshot-bearing failure could not have come from it and
    # must not cross the boundary as trustworthy data.
    payload = _ok_response().to_dict()
    payload["outcome"] = "execution_failed"
    payload["snapshot_summary"] = None
    payload["snapshot_id"] = "walk-3"  # illegal: a failure carries no snapshot id
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_execution_failed_with_summary_raises():
    # Rule (d): execution_failed must also carry a None summary.
    payload = _ok_response().to_dict()
    payload["outcome"] = "execution_failed"
    payload["snapshot_id"] = None
    # snapshot_summary stays the populated _ok_response() summary -- illegal here.
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_error_with_snapshot_id_raises():
    # Rule (d): the error outcome (status=error) carries no snapshot id either.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    payload["outcome"] = "error"
    payload["snapshot_summary"] = None
    payload["snapshot_id"] = "walk-3"  # illegal: an error carries no snapshot id
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_error_with_summary_raises():
    # Rule (d): the error outcome carries a None summary.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    payload["outcome"] = "error"
    payload["snapshot_id"] = None
    # snapshot_summary stays the populated _ok_response() summary -- illegal here.
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError, never a bare error
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(["not", "a", "mapping"])


def test_missing_status_raises():
    payload = _ok_response().to_dict()
    del payload["status"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_str_status_raises():
    payload = _ok_response().to_dict()
    payload["status"] = 1
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_status_outside_closed_set_raises():
    payload = _ok_response().to_dict()
    payload["status"] = "not_implemented"  # not in the emitted status set here
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_outcome_raises():
    payload = _ok_response().to_dict()
    del payload["outcome"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_str_outcome_raises():
    payload = _ok_response().to_dict()
    payload["outcome"] = 0
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_outcome_outside_closed_set_raises():
    payload = _ok_response().to_dict()
    payload["outcome"] = "maybe"
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_reason_raises():
    payload = _ok_response().to_dict()
    del payload["reason"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_str_reason_raises():
    payload = _ok_response().to_dict()
    payload["reason"] = 5
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_snapshot_id_raises():
    payload = _ok_response().to_dict()
    del payload["snapshot_id"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_str_snapshot_id_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = 7
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_snapshot_summary_key_raises():
    payload = _ok_response().to_dict()
    del payload["snapshot_summary"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_summary_not_a_mapping_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = 5
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_summary_missing_field_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {"snapshot_id": "walk-1"}
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_summary_non_finite_created_at_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "walk-1",
        "created_at_monotonic": float("nan"),
        "items": [],
    }
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_summary_items_not_a_list_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "walk-1",
        "created_at_monotonic": 1.0,
        "items": ("not", "a", "list"),
    }
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_trace_id_raises():
    payload = _ok_response().to_dict()
    del payload["trace_id"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_str_trace_id_raises():
    payload = _ok_response().to_dict()
    payload["trace_id"] = 9
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    del payload["overlay_session_id"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_bool_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    payload["overlay_session_id"] = True  # bool is a subclass of int; reject
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_int_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    payload["overlay_session_id"] = "7"
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_missing_paint_generation_raises():
    payload = _ok_response().to_dict()
    del payload["paint_generation"]
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_bool_paint_generation_raises():
    payload = _ok_response().to_dict()
    payload["paint_generation"] = False
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)


def test_non_int_paint_generation_raises():
    payload = _ok_response().to_dict()
    payload["paint_generation"] = 1.5
    with pytest.raises(StartOverlayWalkResponseSchemaError):
        StartOverlayWalkResponse.from_dict(payload)
