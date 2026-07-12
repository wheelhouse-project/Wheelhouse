"""Tests for the ShowNumberedOverlayResponse IPC schema (wh-jfavj / wh-n29v.79).

The schema defines the Input -> Logic reply for the show_numbered_overlay
action (Phase 1.5 of the voice-element-clicking feature, epic wh-l4h.1).
It is a request-correlated Schema A response (handler-owned, correlated by
request_id via the _HANDLES_OWN_RESPONSE machinery), not a type-routed
unsolicited event.

``show_numbered_overlay`` re-uses an EXISTING walk snapshot id, so unlike the
sibling ``StartOverlayWalkResponse`` (a fresh walk with no input snapshot) its
authoritative ``outcome`` set INCLUDES ``snapshot_expired`` -- a stale/gone
snapshot id (or a foreground-identity mismatch) is a feature failure with no
paintable snapshot.

Coverage:
  * Round-trip (to_dict -> from_dict) for every outcome literal:
    ok (with a populated summary), snapshot_expired, no_targets,
    execution_failed, error, each with the envelope the handler emits for it.
  * The echoed overlay_session_id / paint_generation / trace_id survive the
    round-trip.
  * Nested snapshot_summary item bounds normalization: a list becomes a tuple.
  * The status/outcome split mapping (outcome=ok -> status=ok; any non-ok
    outcome still rides transport status=ok; status=error is reserved for a
    handler-level crash/degrade paired with outcome=error).
  * The defensive parse-only ``not_implemented`` status literal: from_dict
    ACCEPTS the surviving stub's envelope (status=not_implemented,
    outcome=execution_failed, no snapshot) even though the REAL handler emits
    status only in {ok, error}.
  * Cross-field invariants (status/outcome error pairing; ok requires a
    snapshot; failure outcomes {snapshot_expired, execution_failed, error}
    carry no snapshot; snapshot_id/summary must agree; no_targets keeps the
    snapshot-or-None flexibility).
  * from_dict raises ShowNumberedOverlayResponseSchemaError on every malformed
    shape (not a mapping, missing/wrong-type fields, status/outcome outside its
    closed set, malformed nested summary, bool int rejection) -- never an
    unhandled KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterator

import pytest

from services.wheelhouse.shared.show_numbered_overlay import (
    ShowNumberedOverlayResponse,
    ShowNumberedOverlayResponseSchemaError,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary(num_items: int = 2, snapshot_id: str = "s1") -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"m{i}",
            display_number=i,
            name=f"Cancel {i}",
            role="Button",
            bounds=(10 * i, 20 * i, 80 + i, 30 + i),
            monitor_id=i % 2,
        )
        for i in range(1, num_items + 1)
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=items,
        created_at_monotonic=123.456,
    )


def _ok_response() -> ShowNumberedOverlayResponse:
    return ShowNumberedOverlayResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id="s1",
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
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "ok"
    assert restored.status == "ok"
    assert restored.snapshot_summary is not None
    assert len(restored.snapshot_summary.items) == 3
    assert restored.overlay_session_id == 7
    assert restored.paint_generation == 2
    assert restored.trace_id == "trace-1"


def test_round_trip_snapshot_expired():
    # snapshot_expired is the schema-specific outcome the sibling
    # StartOverlayWalkResponse cannot have: show_numbered_overlay re-uses an
    # EXISTING snapshot id, so it can find that id stale/gone (or the
    # foreground identity has changed) and report a feature failure with no
    # paintable snapshot (design doc line 416).
    resp = ShowNumberedOverlayResponse(
        status="ok",
        outcome="snapshot_expired",
        reason="stale_snapshot_id",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="trace-exp",
        overlay_session_id=5,
        paint_generation=3,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "snapshot_expired"
    # A feature failure still rides transport status=ok.
    assert restored.status == "ok"
    assert restored.snapshot_id is None
    assert restored.snapshot_summary is None
    assert restored.reason == "stale_snapshot_id"


def test_round_trip_no_targets():
    resp = ShowNumberedOverlayResponse(
        status="ok",
        outcome="no_targets",
        reason=None,
        snapshot_id="s2",
        snapshot_summary=None,
        trace_id="trace-2",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "no_targets"
    assert restored.status == "ok"


def test_round_trip_no_targets_with_empty_summary():
    # The schema permits no_targets to carry an empty-items summary too.
    resp = ShowNumberedOverlayResponse(
        status="ok",
        outcome="no_targets",
        reason=None,
        snapshot_id="s2b",
        snapshot_summary=WalkSnapshotSummary(
            snapshot_id="s2b", items=[], created_at_monotonic=5.0
        ),
        trace_id="trace-2b",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.items == []


def test_round_trip_execution_failed():
    # A failure outcome carries no snapshot: the handler emits
    # execution_failed with snapshot_id=None and summary=None.
    resp = ShowNumberedOverlayResponse(
        status="ok",
        outcome="execution_failed",
        reason="foreground_changed",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="trace-3",
        overlay_session_id=2,
        paint_generation=1,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.outcome == "execution_failed"
    assert restored.reason == "foreground_changed"
    # A feature failure still rides transport status=ok.
    assert restored.status == "ok"


def test_round_trip_error_status():
    resp = ShowNumberedOverlayResponse(
        status="error",
        outcome="error",
        reason="unexpected",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="trace-4",
        overlay_session_id=3,
        paint_generation=4,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.status == "error"
    assert restored.outcome == "error"


def test_round_trip_not_implemented_defensive_parse():
    # The surviving Phase 1.5 stub emits status=not_implemented; the REAL
    # handler emits status only in {ok, error}, but from_dict RETAINS
    # not_implemented as a defensive parse literal. The stub's envelope --
    # (status=not_implemented, outcome=execution_failed, no snapshot) --
    # satisfies cross-field rules (a) and (d) and round-trips.
    resp = ShowNumberedOverlayResponse(
        status="not_implemented",
        outcome="execution_failed",
        reason="not_implemented",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="",
        overlay_session_id=0,
        paint_generation=0,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.status == "not_implemented"
    assert restored.outcome == "execution_failed"


# ---------------------------------------------------------------------------
# not_implemented is pinned to exactly one envelope (wh-n29v.80.1). The
# surviving stub emits status=not_implemented ONLY with outcome=execution_failed
# (and, via rule (d), no snapshot). A not_implemented payload paired with any
# other outcome could not have come from the stub and must not cross the
# wh-uf54 trust boundary parsing as trustworthy.
# ---------------------------------------------------------------------------


def test_not_implemented_with_outcome_ok_populated_snapshot_raises():
    # not_implemented + outcome=ok (with a populated snapshot) is not the stub
    # envelope: the new not_implemented rule rejects it.
    payload = {
        "status": "not_implemented",
        "outcome": "ok",
        "reason": None,
        "snapshot_id": "s1",
        "snapshot_summary": {
            "snapshot_id": "s1",
            "created_at_monotonic": 1.0,
            "items": [
                {
                    "item_id": "m1",
                    "display_number": 1,
                    "name": "Cancel",
                    "role": "Button",
                    "bounds": [1, 2, 3, 4],
                    "monitor_id": 0,
                }
            ],
        },
        "trace_id": "",
        "overlay_session_id": 0,
        "paint_generation": 0,
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_not_implemented_with_outcome_ok_no_snapshot_raises():
    # not_implemented + outcome=ok with snapshot_id=None / summary=None still
    # RAISES: it fails the new not_implemented rule (status must pair with
    # execution_failed) before/independent of rule (b).
    payload = {
        "status": "not_implemented",
        "outcome": "ok",
        "reason": None,
        "snapshot_id": None,
        "snapshot_summary": None,
        "trace_id": "",
        "overlay_session_id": 0,
        "paint_generation": 0,
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_not_implemented_with_outcome_no_targets_raises():
    payload = {
        "status": "not_implemented",
        "outcome": "no_targets",
        "reason": None,
        "snapshot_id": None,
        "snapshot_summary": None,
        "trace_id": "",
        "overlay_session_id": 0,
        "paint_generation": 0,
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_not_implemented_with_outcome_snapshot_expired_raises():
    payload = {
        "status": "not_implemented",
        "outcome": "snapshot_expired",
        "reason": None,
        "snapshot_id": None,
        "snapshot_summary": None,
        "trace_id": "",
        "overlay_session_id": 0,
        "paint_generation": 0,
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_not_implemented_execution_failed_stub_envelope_still_parses():
    # POSITIVE guard: the stub's actual envelope (not_implemented +
    # execution_failed + None + None) is NOT rejected by the new rule and
    # round-trips. This is the same envelope the stub smoke test exercises.
    resp = ShowNumberedOverlayResponse(
        status="not_implemented",
        outcome="execution_failed",
        reason="not_implemented",
        snapshot_id=None,
        snapshot_summary=None,
        trace_id="",
        overlay_session_id=0,
        paint_generation=0,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored == resp
    assert restored.status == "not_implemented"
    assert restored.outcome == "execution_failed"


def test_bounds_list_normalizes_to_tuple():
    payload = {
        "status": "ok",
        "outcome": "ok",
        "reason": None,
        "snapshot_id": "s1",
        "snapshot_summary": {
            "snapshot_id": "s1",
            "created_at_monotonic": 1.0,
            "items": [
                {
                    "item_id": "m1",
                    "display_number": 1,
                    "name": "Cancel",
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
    restored = ShowNumberedOverlayResponse.from_dict(payload)
    assert restored.snapshot_summary is not None
    assert restored.snapshot_summary.items[0].bounds == (1, 2, 3, 4)
    assert isinstance(restored.snapshot_summary.items[0].bounds, tuple)


# ---------------------------------------------------------------------------
# Outcome literal set is exactly
# {ok, snapshot_expired, no_targets, execution_failed, error}.
# ---------------------------------------------------------------------------


# Per-outcome CROSS-FIELD-VALID envelopes. Each literal is paired with the
# envelope the handler actually emits for it.
_VALID_ENVELOPE_BY_OUTCOME = {
    # ok / no_targets ride status=ok and may carry a snapshot + matching summary.
    "ok": ("ok", "s1", _summary(1)),
    "no_targets": ("ok", "s1", WalkSnapshotSummary(
        snapshot_id="s1", items=[], created_at_monotonic=1.0)),
    # snapshot_expired / execution_failed ride status=ok with no snapshot.
    "snapshot_expired": ("ok", None, None),
    "execution_failed": ("ok", None, None),
    # error is the only status=error case.
    "error": ("error", None, None),
}


@pytest.mark.parametrize(
    "outcome",
    ["ok", "snapshot_expired", "no_targets", "execution_failed", "error"],
)
def test_each_outcome_literal_accepted(outcome):
    status, snapshot_id, summary = _VALID_ENVELOPE_BY_OUTCOME[outcome]
    resp = ShowNumberedOverlayResponse(
        status=status,
        outcome=outcome,
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=summary,
        trace_id="t",
        overlay_session_id=1,
        paint_generation=0,
    )
    restored = ShowNumberedOverlayResponse.from_dict(resp.to_dict())
    assert restored.outcome == outcome


# ---------------------------------------------------------------------------
# Cross-field invariants. The per-field parsing validates each field in
# isolation; these rules reject payloads whose field COMBINATION the handler
# could never emit. The handler's only emit combinations are:
#   (status=ok,    outcome=ok,               snapshot_id set, summary set)
#   (status=ok,    outcome=no_targets,       snapshot_id set OR None, summary or None)
#   (status=ok,    outcome=snapshot_expired, snapshot_id=None, summary=None)
#   (status=ok,    outcome=execution_failed, snapshot_id=None, summary=None)
#   (status=error, outcome=error,            snapshot_id=None, summary=None)
# Plus the surviving stub: (status=not_implemented, outcome=execution_failed,
# snapshot_id=None, summary=None), which satisfies rules (a) and (d).
# ---------------------------------------------------------------------------


def test_status_ok_with_outcome_error_raises():
    # Rule (a): status is "error" iff outcome is "error".
    payload = _ok_response().to_dict()
    payload["outcome"] = "error"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_status_error_with_outcome_ok_raises():
    # Rule (a), the other direction: status=error with a non-error outcome.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_ok_with_none_snapshot_id_raises():
    # Rule (b): outcome=ok REQUIRES a non-empty snapshot_id.
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = None
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_ok_with_empty_snapshot_id_raises():
    # Rule (b): an empty-string snapshot_id is parseable as a str but is not a
    # usable id, so ok must still reject it.
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = ""
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_ok_with_none_summary_raises():
    # Rule (b): outcome=ok REQUIRES a non-None summary -- the badges to paint.
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = None
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_ok_summary_snapshot_id_disagreement_raises():
    # Rule (c): when both the top-level snapshot_id and a summary are present,
    # they must name the SAME snapshot.
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = "s99"  # summary still says s1
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_no_targets_with_none_summary_still_accepted():
    # The cross-field presence rule is scoped to outcome=ok ONLY; no_targets
    # keeps its documented snapshot-or-None flexibility (it is a successful walk
    # of an empty window, NOT in the failure set rule (d) constrains).
    payload = _ok_response().to_dict()
    payload["outcome"] = "no_targets"
    payload["snapshot_summary"] = None
    restored = ShowNumberedOverlayResponse.from_dict(payload)
    assert restored.outcome == "no_targets"
    assert restored.snapshot_summary is None


def test_no_targets_with_populated_summary_raises():
    # Rule (e) (wh-overlay-no-targets-summary-guard): no_targets means nothing
    # is paintable, so a POPULATED-items summary contradicts the outcome. The
    # real handler emits no_targets only when the (possibly filtered) summary
    # has no items ("never a populated one"), so a populated-items no_targets
    # could not have come from it and must not cross the boundary as
    # trustworthy data. The empty-items-or-None flexibility stays (sibling
    # test above).
    payload = _ok_response().to_dict()  # summary carries items; ids agree
    payload["outcome"] = "no_targets"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_snapshot_expired_with_snapshot_id_raises():
    # Rule (d): snapshot_expired joins the failure set, so it carries NO
    # snapshot -- a stale/gone snapshot has nothing to paint. A snapshot-bearing
    # snapshot_expired could not have come from the handler.
    payload = _ok_response().to_dict()
    payload["outcome"] = "snapshot_expired"
    payload["snapshot_summary"] = None
    payload["snapshot_id"] = "s3"  # illegal: an expired snapshot carries none
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_snapshot_expired_with_summary_raises():
    # Rule (d): snapshot_expired must also carry a None summary.
    payload = _ok_response().to_dict()
    payload["outcome"] = "snapshot_expired"
    payload["snapshot_id"] = None
    # snapshot_summary stays the populated _ok_response() summary -- illegal.
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_execution_failed_with_snapshot_id_raises():
    # Rule (d): a failure outcome carries NO snapshot.
    payload = _ok_response().to_dict()
    payload["outcome"] = "execution_failed"
    payload["snapshot_summary"] = None
    payload["snapshot_id"] = "s3"  # illegal: a failure carries no snapshot id
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_execution_failed_with_summary_raises():
    # Rule (d): execution_failed must also carry a None summary.
    payload = _ok_response().to_dict()
    payload["outcome"] = "execution_failed"
    payload["snapshot_id"] = None
    # snapshot_summary stays the populated _ok_response() summary -- illegal.
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_error_with_snapshot_id_raises():
    # Rule (d): the error outcome (status=error) carries no snapshot id either.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    payload["outcome"] = "error"
    payload["snapshot_summary"] = None
    payload["snapshot_id"] = "s3"  # illegal: an error carries no snapshot id
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_error_with_summary_raises():
    # Rule (d): the error outcome carries a None summary.
    payload = _ok_response().to_dict()
    payload["status"] = "error"
    payload["outcome"] = "error"
    payload["snapshot_id"] = None
    # snapshot_summary stays the populated _ok_response() summary -- illegal.
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError, never a bare error
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(["not", "a", "mapping"])


def test_missing_status_raises():
    payload = _ok_response().to_dict()
    del payload["status"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_str_status_raises():
    payload = _ok_response().to_dict()
    payload["status"] = 1
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_status_outside_closed_set_raises():
    payload = _ok_response().to_dict()
    payload["status"] = "maybe"  # not in the parse set {ok, error, not_implemented}
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_outcome_raises():
    payload = _ok_response().to_dict()
    del payload["outcome"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_str_outcome_raises():
    payload = _ok_response().to_dict()
    payload["outcome"] = 0
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_outcome_outside_closed_set_raises():
    payload = _ok_response().to_dict()
    payload["outcome"] = "maybe"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_reason_raises():
    payload = _ok_response().to_dict()
    del payload["reason"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_str_reason_raises():
    payload = _ok_response().to_dict()
    payload["reason"] = 5
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_snapshot_id_raises():
    payload = _ok_response().to_dict()
    del payload["snapshot_id"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_str_snapshot_id_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_id"] = 7
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_snapshot_summary_key_raises():
    payload = _ok_response().to_dict()
    del payload["snapshot_summary"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_not_a_mapping_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = 5
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_missing_field_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {"snapshot_id": "s1"}
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_non_finite_created_at_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "s1",
        "created_at_monotonic": float("nan"),
        "items": [],
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_items_not_a_list_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "s1",
        "created_at_monotonic": 1.0,
        "items": ("not", "a", "list"),  # tuple rejected
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_item_bad_bounds_length_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "s1",
        "created_at_monotonic": 1.0,
        "items": [
            {
                "item_id": "m1",
                "display_number": 1,
                "name": "Cancel",
                "role": "Button",
                "bounds": [1, 2, 3],  # only 3
                "monitor_id": 0,
            }
        ],
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_summary_item_bool_display_number_raises():
    payload = _ok_response().to_dict()
    payload["snapshot_summary"] = {
        "snapshot_id": "s1",
        "created_at_monotonic": 1.0,
        "items": [
            {
                "item_id": "m1",
                "display_number": True,  # bool rejected
                "name": "Cancel",
                "role": "Button",
                "bounds": [1, 2, 3, 4],
                "monitor_id": 0,
            }
        ],
    }
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_trace_id_raises():
    payload = _ok_response().to_dict()
    del payload["trace_id"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_str_trace_id_raises():
    payload = _ok_response().to_dict()
    payload["trace_id"] = 9
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    del payload["overlay_session_id"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_bool_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    payload["overlay_session_id"] = True  # bool is a subclass of int; reject
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_int_overlay_session_id_raises():
    payload = _ok_response().to_dict()
    payload["overlay_session_id"] = "7"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_missing_paint_generation_raises():
    payload = _ok_response().to_dict()
    del payload["paint_generation"]
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_bool_paint_generation_raises():
    payload = _ok_response().to_dict()
    payload["paint_generation"] = False
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_non_int_paint_generation_raises():
    payload = _ok_response().to_dict()
    payload["paint_generation"] = 1.5
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# Hostile Mapping subclass (wh-n29v.81.1). A Mapping whose __getitem__ or
# __contains__ raises passes the isinstance(payload, Mapping) gate but then
# bubbles a RAW AttributeError / TypeError. from_dict promises (docstring +
# wh-uf54) that only the typed ShowNumberedOverlayResponseSchemaError escapes,
# so the wrapper around the parse body must convert these to the typed error,
# at BOTH the top level and inside the nested snapshot_summary path.
# ---------------------------------------------------------------------------


class _GetItemRaisesMapping(Mapping):
    """A Mapping whose __getitem__ raises AttributeError (passes isinstance)."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = keys

    def __getitem__(self, key: Any):
        raise AttributeError(f"hostile __getitem__ for {key!r}")

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


class _ContainsRaisesMapping(Mapping):
    """A Mapping whose __contains__ raises TypeError (passes isinstance)."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = keys

    def __contains__(self, key: Any) -> bool:
        raise TypeError("hostile __contains__")

    def __getitem__(self, key: Any):
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


def test_top_level_getitem_raises_yields_typed_error():
    # A hostile top-level mapping whose __getitem__ raises AttributeError must
    # surface as the typed schema error, not the raw AttributeError.
    hostile = _GetItemRaisesMapping(("status",))
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(hostile)


def test_top_level_contains_raises_yields_typed_error():
    # A hostile top-level mapping whose __contains__ raises TypeError must
    # surface as the typed schema error, not the raw TypeError.
    hostile = _ContainsRaisesMapping(("status",))
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(hostile)


def test_nested_summary_hostile_mapping_yields_typed_error():
    # A well-formed top-level payload whose snapshot_summary value is a hostile
    # Mapping must ALSO surface as the typed schema error -- proves the wrapper
    # covers the nested summary_from_dict path, so walk_snapshot_serde.py need
    # not be edited.
    payload: dict[str, Any] = _ok_response().to_dict()
    payload["snapshot_summary"] = _GetItemRaisesMapping(("snapshot_id",))
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(payload)


def test_wrapper_does_not_swallow_normal_validation():
    # Regression guard: the wrapper must NOT alter normal validation. A valid
    # payload still round-trips, and an existing typed-error case (status
    # outside the closed set) still raises the typed error.
    valid = _ok_response().to_dict()
    assert ShowNumberedOverlayResponse.from_dict(valid) == _ok_response()

    bad = _ok_response().to_dict()
    bad["status"] = "maybe"
    with pytest.raises(ShowNumberedOverlayResponseSchemaError):
        ShowNumberedOverlayResponse.from_dict(bad)


# ---------------------------------------------------------------------------
# Handler behaviour
# ---------------------------------------------------------------------------
#
# The Phase 1.5 ``not_implemented`` stub on ``UIActionHandler.show_numbered_overlay``
# was replaced by the real snapshot-lookup handler in wh-n29v.83. Its
# behaviour (snapshot lookup, summary build, item_id_filter + renumber, every
# ``outcome`` literal, and the from_dict round-trip of every emitted payload) is
# covered by ``tests/test_ui/test_show_numbered_overlay_handler.py``. This file
# stays focused on the ShowNumberedOverlayResponse SCHEMA -- including the
# ``not_implemented`` DEFENSIVE-PARSE acceptance (the surviving parse literal),
# which the round-trip tests above still exercise.
