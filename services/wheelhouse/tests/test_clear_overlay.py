"""Tests for the clear_overlay Logic -> GUI event schema (wh-9gkh5k).

The schema defines the Logic-to-GUI event that tears down the numbered
overlay during Phase 1.5 of the voice-element-clicking feature (epic
wh-l4h.1). Mirrors the SnapshotItemClickedEvent test coverage.

Coverage:
  * Round-trip (to_dict -> from_dict); to_dict carries the action key.
  * from_dict raises ClearOverlayEventSchemaError on every malformed shape
    (not a mapping, missing/wrong action, missing/non-int overlay_session_id
    or paint_generation, bool supplied for an int field) -- never an
    unhandled KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.clear_overlay import (
    ACTION_NAME,
    ClearOverlayEvent,
    ClearOverlayEventSchemaError,
)


def test_round_trip():
    evt = ClearOverlayEvent(overlay_session_id=5, paint_generation=3)
    payload = evt.to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["overlay_session_id"] == 5
    assert payload["paint_generation"] == 3
    restored = ClearOverlayEvent.from_dict(payload)
    assert restored == evt


def test_round_trip_zeros():
    evt = ClearOverlayEvent(overlay_session_id=0, paint_generation=0)
    assert ClearOverlayEvent.from_dict(evt.to_dict()) == evt


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict("not a mapping")


def test_missing_action_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"overlay_session_id": 1, "paint_generation": 1}
        )


def test_wrong_action_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": "other", "overlay_session_id": 1, "paint_generation": 1}
        )


def test_missing_overlay_session_id_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "paint_generation": 1}
        )


def test_non_int_overlay_session_id_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "overlay_session_id": "1",
             "paint_generation": 1}
        )


def test_bool_overlay_session_id_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "overlay_session_id": True,
             "paint_generation": 1}
        )


def test_missing_paint_generation_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "overlay_session_id": 1}
        )


def test_non_int_paint_generation_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "overlay_session_id": 1,
             "paint_generation": 1.5}
        )


def test_bool_paint_generation_raises():
    with pytest.raises(ClearOverlayEventSchemaError):
        ClearOverlayEvent.from_dict(
            {"action": ACTION_NAME, "overlay_session_id": 1,
             "paint_generation": False}
        )
