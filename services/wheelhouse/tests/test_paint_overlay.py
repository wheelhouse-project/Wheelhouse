"""Tests for the paint_overlay Logic -> GUI event schema (wh-9gkh5k).

The schema defines the Logic-to-GUI event that drives the numbered-overlay
paint during Phase 1.5 of the voice-element-clicking feature (epic
wh-l4h.1). Mirrors the SnapshotItemClickedEvent test coverage.

Coverage:
  * Round-trip (to_dict -> from_dict); to_dict carries the action key.
  * The WalkSnapshotSummary is FLATTENED into the top-level wire dict --
    snapshot_id / created_at_monotonic / items are top-level keys, NOT
    nested under a 'snapshot_summary' key.
  * from_dict raises PaintOverlayEventSchemaError on every malformed shape
    (not a mapping, missing/wrong action, missing/non-int overlay_session_id
    or paint_generation, bool supplied for an int field, malformed nested
    summary) -- never an unhandled KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

import json

import pytest

from services.wheelhouse.shared.paint_overlay import (
    ACTION_NAME,
    PaintOverlayEvent,
    PaintOverlayEventSchemaError,
)
from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem


def _summary() -> WalkSnapshotSummary:
    return WalkSnapshotSummary(
        snapshot_id="snap-1",
        items=[
            WalkSnapshotSummaryItem(
                item_id="m1",
                display_number=1,
                name="Cancel",
                role="Button",
                bounds=(10, 20, 30, 40),
                monitor_id=0,
            ),
            WalkSnapshotSummaryItem(
                item_id="m2",
                display_number=2,
                name="OK",
                role="Button",
                bounds=(50, 60, 70, 80),
                monitor_id=1,
            ),
        ],
        created_at_monotonic=123.5,
    )


def _evt() -> PaintOverlayEvent:
    return PaintOverlayEvent(
        overlay_session_id=7,
        paint_generation=2,
        summary=_summary(),
    )


def test_round_trip():
    evt = _evt()
    payload = evt.to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["overlay_session_id"] == 7
    assert payload["paint_generation"] == 2
    restored = PaintOverlayEvent.from_dict(payload)
    assert restored == evt


def test_summary_is_flattened_at_top_level():
    """The summary fields ride at the TOP level, not nested under a key."""
    payload = _evt().to_dict()
    # The three summary keys appear at the top level.
    assert payload["snapshot_id"] == "snap-1"
    assert payload["created_at_monotonic"] == 123.5
    assert isinstance(payload["items"], list)
    assert len(payload["items"]) == 2
    assert payload["items"][0]["item_id"] == "m1"
    # And there is NO nested 'snapshot_summary' key.
    assert "snapshot_summary" not in payload


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict("not a mapping")


def test_missing_action_raises():
    payload = _evt().to_dict()
    del payload["action"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_wrong_action_raises():
    payload = _evt().to_dict()
    payload["action"] = "other"
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_overlay_session_id_raises():
    payload = _evt().to_dict()
    del payload["overlay_session_id"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_non_int_overlay_session_id_raises():
    payload = _evt().to_dict()
    payload["overlay_session_id"] = "7"
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bool_overlay_session_id_raises():
    payload = _evt().to_dict()
    payload["overlay_session_id"] = True
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_paint_generation_raises():
    payload = _evt().to_dict()
    del payload["paint_generation"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_non_int_paint_generation_raises():
    payload = _evt().to_dict()
    payload["paint_generation"] = 1.5
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bool_paint_generation_raises():
    payload = _evt().to_dict()
    payload["paint_generation"] = False
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_summary_snapshot_id_raises():
    payload = _evt().to_dict()
    del payload["snapshot_id"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_non_str_summary_snapshot_id_raises():
    payload = _evt().to_dict()
    payload["snapshot_id"] = 5
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_summary_items_raises():
    payload = _evt().to_dict()
    del payload["items"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_items_not_a_list_raises():
    payload = _evt().to_dict()
    payload["items"] = tuple(payload["items"])
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_malformed_item_member_raises():
    payload = _evt().to_dict()
    payload["items"][0]["display_number"] = "1"
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_created_at_monotonic_raises():
    payload = _evt().to_dict()
    del payload["created_at_monotonic"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_round_trip_empty_items():
    evt = PaintOverlayEvent(
        overlay_session_id=0,
        paint_generation=0,
        summary=WalkSnapshotSummary(
            snapshot_id="s",
            items=[],
            created_at_monotonic=0.0,
        ),
    )
    assert PaintOverlayEvent.from_dict(evt.to_dict()) == evt


# ---------------------------------------------------------------------------
# JSON-bridge round-trip (tuple -> list on the wire) -- wh-n29v.2.1
# ---------------------------------------------------------------------------


def test_round_trip_bounds_as_list_from_wire():
    """bounds arrives as a list after the process boundary; from_dict accepts it."""
    payload = _evt().to_dict()
    for item in payload["items"]:
        item["bounds"] = list(item["bounds"])
    assert PaintOverlayEvent.from_dict(payload) == _evt()


def test_json_round_trip():
    """A real json.dumps/json.loads cycle round-trips to an equal event."""
    evt = _evt()
    restored = PaintOverlayEvent.from_dict(json.loads(json.dumps(evt.to_dict())))
    assert restored == evt


# ---------------------------------------------------------------------------
# Flattened-summary validation branches -- wh-n29v.2.2
# ---------------------------------------------------------------------------


def test_non_finite_created_at_monotonic_raises():
    for bad in (float("nan"), float("inf"), float("-inf")):
        payload = _evt().to_dict()
        payload["created_at_monotonic"] = bad
        with pytest.raises(PaintOverlayEventSchemaError):
            PaintOverlayEvent.from_dict(payload)


def test_non_number_created_at_monotonic_raises():
    payload = _evt().to_dict()
    payload["created_at_monotonic"] = "123.5"
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_missing_nested_item_field_raises():
    payload = _evt().to_dict()
    del payload["items"][0]["item_id"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_non_mapping_items_member_raises():
    payload = _evt().to_dict()
    payload["items"] = [1]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_malformed_bounds_wrong_length_raises():
    payload = _evt().to_dict()
    payload["items"][0]["bounds"] = [1, 2, 3]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_malformed_bounds_non_int_member_raises():
    payload = _evt().to_dict()
    payload["items"][0]["bounds"] = [1, 2, 3, "4"]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


# ---------------------------------------------------------------------------
# to_dict fails loudly on a None summary -- wh-n29v.3.1
# ---------------------------------------------------------------------------


def test_to_dict_none_summary_raises():
    # summary is a non-optional field; constructing with None violates the type.
    # to_dict must fail loudly rather than emit a payload missing the flattened
    # summary fields (which the GUI would drop at from_dict).
    evt = PaintOverlayEvent(
        overlay_session_id=1,
        paint_generation=1,
        summary=None,  # type: ignore[arg-type]
    )
    with pytest.raises(PaintOverlayEventSchemaError):
        evt.to_dict()


# ---------------------------------------------------------------------------
# bool-as-int and exact-type fences on the flattened summary -- wh-n29v.3.2
# ---------------------------------------------------------------------------


def test_bool_created_at_monotonic_raises():
    payload = _evt().to_dict()
    payload["created_at_monotonic"] = True
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bool_item_display_number_raises():
    payload = _evt().to_dict()
    payload["items"][0]["display_number"] = True
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bool_item_monitor_id_raises():
    payload = _evt().to_dict()
    payload["items"][0]["monitor_id"] = True
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bool_bounds_member_raises():
    payload = _evt().to_dict()
    payload["items"][0]["bounds"] = [True, 2, 3, 4]
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_items_list_subclass_raises():
    class _ListSubclass(list):
        pass

    payload = _evt().to_dict()
    payload["items"] = _ListSubclass(payload["items"])
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)


def test_bounds_list_subclass_raises():
    class _ListSubclass(list):
        pass

    payload = _evt().to_dict()
    payload["items"][0]["bounds"] = _ListSubclass([1, 2, 3, 4])
    with pytest.raises(PaintOverlayEventSchemaError):
        PaintOverlayEvent.from_dict(payload)
