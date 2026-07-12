"""Tests for the overlay_state_changed GUI -> Logic event schema (wh-9gkh5k).

The schema defines the GUI-to-Logic event that reports the numbered-overlay
paint outcome back to Logic during Phase 1.5 of the voice-element-clicking
feature (epic wh-l4h.1). Mirrors the SnapshotItemClickedEvent test coverage.

Coverage:
  * Round-trip (to_dict -> from_dict); to_dict carries the action key.
  * Defaults: monitor_ids defaults to (); snapshot_id defaults to None.
  * monitor_ids round-trips through a JSON list and normalizes to a tuple.
  * snapshot_id accepts None and a str.
  * from_dict raises OverlayStateChangedEventSchemaError on every malformed
    shape (not a mapping, missing/wrong action, missing/wrong-type or
    out-of-set state, missing/non-int overlay_session_id or paint_generation,
    bool supplied for an int field, non-list/tuple monitor_ids, non-int
    monitor_ids member, bool monitor_ids member, non-str non-None
    snapshot_id) -- never an unhandled KeyError / TypeError / AttributeError.
"""

from __future__ import annotations

import json

import pytest

from services.wheelhouse.shared.overlay_state_changed import (
    ACTION_NAME,
    OverlayStateChangedEvent,
    OverlayStateChangedEventSchemaError,
)


def _payload(**overrides):
    base = {
        "action": ACTION_NAME,
        "state": "painted",
        "overlay_session_id": 4,
        "paint_generation": 2,
        "monitor_ids": [0, 1],
        "snapshot_id": "snap-1",
    }
    base.update(overrides)
    return base


def test_round_trip():
    evt = OverlayStateChangedEvent(
        state="painted",
        overlay_session_id=4,
        paint_generation=2,
        monitor_ids=(0, 1),
        snapshot_id="snap-1",
    )
    payload = evt.to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["state"] == "painted"
    assert payload["overlay_session_id"] == 4
    assert payload["paint_generation"] == 2
    # monitor_ids serializes as a list for JSON friendliness.
    assert payload["monitor_ids"] == [0, 1]
    assert payload["snapshot_id"] == "snap-1"
    restored = OverlayStateChangedEvent.from_dict(payload)
    assert restored == evt
    # And the normalized field is a tuple again.
    assert isinstance(restored.monitor_ids, tuple)


def test_defaults():
    evt = OverlayStateChangedEvent(
        state="cleared",
        overlay_session_id=0,
        paint_generation=0,
    )
    assert evt.monitor_ids == ()
    assert evt.snapshot_id is None
    # Round-trips with the defaulted fields emitted.
    payload = evt.to_dict()
    assert payload["monitor_ids"] == []
    assert payload["snapshot_id"] is None
    assert OverlayStateChangedEvent.from_dict(payload) == evt


def test_all_allowed_states_round_trip():
    for state in ("painted", "failed", "cleared"):
        evt = OverlayStateChangedEvent(
            state=state, overlay_session_id=1, paint_generation=1
        )
        assert OverlayStateChangedEvent.from_dict(evt.to_dict()) == evt


def test_monitor_ids_normalizes_from_tuple_payload():
    # A payload may already carry a tuple (e.g. an in-process construction).
    payload = _payload(monitor_ids=(2, 3, 4))
    restored = OverlayStateChangedEvent.from_dict(payload)
    assert restored.monitor_ids == (2, 3, 4)
    assert isinstance(restored.monitor_ids, tuple)


def test_monitor_ids_accepts_empty():
    payload = _payload(monitor_ids=[])
    restored = OverlayStateChangedEvent.from_dict(payload)
    assert restored.monitor_ids == ()


def test_snapshot_id_accepts_none():
    payload = _payload(snapshot_id=None)
    restored = OverlayStateChangedEvent.from_dict(payload)
    assert restored.snapshot_id is None


def test_snapshot_id_accepts_str():
    payload = _payload(snapshot_id="abc")
    restored = OverlayStateChangedEvent.from_dict(payload)
    assert restored.snapshot_id == "abc"


# ---------------------------------------------------------------------------
# Malformed shapes -- always the typed SchemaError
# ---------------------------------------------------------------------------


def test_not_a_mapping_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict("not a mapping")


def test_missing_action_raises():
    payload = _payload()
    del payload["action"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_wrong_action_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(action="other"))


def test_missing_state_raises():
    payload = _payload()
    del payload["state"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_non_str_state_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(state=1))


def test_state_outside_closed_set_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(state="painting"))


def test_missing_overlay_session_id_raises():
    payload = _payload()
    del payload["overlay_session_id"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_non_int_overlay_session_id_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(overlay_session_id="4"))


def test_bool_overlay_session_id_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(overlay_session_id=True))


def test_missing_paint_generation_raises():
    payload = _payload()
    del payload["paint_generation"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_non_int_paint_generation_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(paint_generation=2.0))


def test_bool_paint_generation_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(paint_generation=False))


def test_missing_monitor_ids_raises():
    payload = _payload()
    del payload["monitor_ids"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_non_sequence_monitor_ids_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(monitor_ids="01"))


def test_non_int_monitor_ids_member_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(monitor_ids=[0, "1"]))


def test_bool_monitor_ids_member_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(monitor_ids=[0, True]))


def test_missing_snapshot_id_raises():
    payload = _payload()
    del payload["snapshot_id"]
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(payload)


def test_non_str_non_none_snapshot_id_raises():
    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(_payload(snapshot_id=5))


# ---------------------------------------------------------------------------
# JSON-bridge round-trip (monitor_ids tuple -> list on the wire) -- wh-n29v.2.1
# ---------------------------------------------------------------------------


def test_json_round_trip():
    """A real json.dumps/json.loads cycle round-trips to an equal event;
    monitor_ids crosses as a JSON list and normalizes back to a tuple."""
    evt = OverlayStateChangedEvent(
        state="painted",
        overlay_session_id=4,
        paint_generation=2,
        monitor_ids=(0, 1),
        snapshot_id="snap-1",
    )
    restored = OverlayStateChangedEvent.from_dict(
        json.loads(json.dumps(evt.to_dict()))
    )
    assert restored == evt
    assert isinstance(restored.monitor_ids, tuple)


# ---------------------------------------------------------------------------
# Exact-type fence on monitor_ids rejects list/tuple subclasses -- wh-n29v.4.1
# ---------------------------------------------------------------------------


def test_monitor_ids_list_subclass_raises():
    class _ListSubclass(list):
        pass

    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(
            _payload(monitor_ids=_ListSubclass([0, 1]))
        )


def test_monitor_ids_tuple_subclass_raises():
    class _TupleSubclass(tuple):
        pass

    with pytest.raises(OverlayStateChangedEventSchemaError):
        OverlayStateChangedEvent.from_dict(
            _payload(monitor_ids=_TupleSubclass((0, 1)))
        )
