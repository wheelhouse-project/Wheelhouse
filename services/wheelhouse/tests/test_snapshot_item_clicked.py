"""Tests for the snapshot_item_clicked GUI -> Logic event schema (wh-jfavj).

The schema defines the GUI-to-Logic event the GUI manager emits when the
user clicks a numbered overlay item (Phase 1.5 of the voice-element-clicking
feature, epic wh-l4h.1). Mirrors try_anyway_clicked.py.

Coverage:
  * Round-trip (to_dict -> from_dict); to_dict carries the action key.
  * from_dict raises SnapshotItemClickedSchemaError on every malformed
    shape (not a mapping, missing/wrong action, missing/non-str
    snapshot_id, missing/non-int display_number, bool display_number,
    display_number < 1) -- never an unhandled KeyError / TypeError /
    AttributeError.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.snapshot_item_clicked import (
    ACTION_NAME,
    SnapshotItemClickedEvent,
    SnapshotItemClickedSchemaError,
)


def test_round_trip():
    evt = SnapshotItemClickedEvent(snapshot_id="s1", display_number=3)
    payload = evt.to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["snapshot_id"] == "s1"
    assert payload["display_number"] == 3
    restored = SnapshotItemClickedEvent.from_dict(payload)
    assert restored == evt


def test_round_trip_display_number_one():
    evt = SnapshotItemClickedEvent(snapshot_id="abc", display_number=1)
    assert SnapshotItemClickedEvent.from_dict(evt.to_dict()) == evt


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict("not a mapping")


def test_missing_action_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"snapshot_id": "s1", "display_number": 1}
        )


def test_wrong_action_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": "other", "snapshot_id": "s1", "display_number": 1}
        )


def test_missing_snapshot_id_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "display_number": 1}
        )


def test_non_str_snapshot_id_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": 5, "display_number": 1}
        )


def test_missing_display_number_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": "s1"}
        )


def test_non_int_display_number_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": "s1", "display_number": "1"}
        )


def test_bool_display_number_raises():
    # bool is a subclass of int; it must be rejected explicitly.
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": "s1", "display_number": True}
        )


def test_display_number_below_one_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": "s1", "display_number": 0}
        )


def test_negative_display_number_raises():
    with pytest.raises(SnapshotItemClickedSchemaError):
        SnapshotItemClickedEvent.from_dict(
            {"action": ACTION_NAME, "snapshot_id": "s1", "display_number": -2}
        )


# The Phase 1.5 not_implemented stub for click_snapshot_item is gone: the real
# handler (wh-tab7j) no longer returns not_implemented. The handler's behaviour
# tests live in tests/test_ui/test_click_snapshot_item_handler.py.
