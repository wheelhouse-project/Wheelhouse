"""Tests for the PinSnapshotResponse IPC schema (wh-n29v.41).

``PinSnapshotResponse`` is the small Schema-A acknowledgement the Input
process emits for both the ``pin_snapshot`` and ``unpin_snapshot`` actions
(Phase 1.5 of the voice-element-clicking feature, epic wh-l4h.1). Logic does
NOT block the overlay paint on this ack, but each handler still emits exactly
one ``PinSnapshotResponse`` so the Logic-side awaiting Future resolves and the
demuxer does not leak.

Like ``StartOverlayWalkResponse`` / ``ClickElementResponse``, this is a
request-correlated Schema A response (handler-owned, correlated by request_id
via the ``_HANDLES_OWN_RESPONSE`` machinery), not a type-routed unsolicited
event; ``status`` is the Schema-A transport field, so the schema carries no
routing ``type`` key.

Coverage:
  * Round-trip (to_dict -> from_dict) for every emitted shape: a successful
    pin (pinned=True), a rejected/unknown pin (pinned=False with a reason),
    a successful unpin (pinned=False), and a handler-level error
    (status=error).
  * The echoed overlay_session_id / snapshot_id survive the round-trip.
  * status is a closed set {ok, error}; a value outside it raises.
  * from_dict raises PinSnapshotResponseSchemaError on every malformed shape
    (not a mapping, missing/wrong-type fields, status outside the closed set,
    pinned not a bool) -- never an unhandled KeyError / TypeError /
    AttributeError.
  * The int field (overlay_session_id) rejects bool (an echoed True must not
    be silently read as 1).
  * None handling for reason (the success path carries reason=None).
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.pin_snapshot import (
    PinSnapshotResponse,
    PinSnapshotResponseSchemaError,
)


def _ok_pin() -> PinSnapshotResponse:
    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=7,
        snapshot_id="walk-1",
        pinned=True,
    )


def _unknown_pin() -> PinSnapshotResponse:
    return PinSnapshotResponse(
        status="ok",
        reason="unknown_snapshot",
        overlay_session_id=7,
        snapshot_id="walk-gone",
        pinned=False,
    )


def _ok_unpin() -> PinSnapshotResponse:
    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=7,
        snapshot_id="walk-1",
        pinned=False,
    )


def _error() -> PinSnapshotResponse:
    return PinSnapshotResponse(
        status="error",
        reason="unexpected_error",
        overlay_session_id=7,
        snapshot_id="walk-1",
        pinned=False,
    )


# ---------------------------------------------------------------------------
# Round-trip for every emitted shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [_ok_pin, _unknown_pin, _ok_unpin, _error],
)
def test_round_trip(factory):
    resp = factory()
    restored = PinSnapshotResponse.from_dict(resp.to_dict())
    assert restored == resp


def test_round_trip_ok_pin_fields():
    resp = _ok_pin()
    restored = PinSnapshotResponse.from_dict(resp.to_dict())
    assert restored.status == "ok"
    assert restored.reason is None
    assert restored.overlay_session_id == 7
    assert restored.snapshot_id == "walk-1"
    assert restored.pinned is True


def test_round_trip_unknown_pin_carries_reason():
    resp = _unknown_pin()
    restored = PinSnapshotResponse.from_dict(resp.to_dict())
    assert restored.pinned is False
    assert restored.reason == "unknown_snapshot"


def test_round_trip_unpin_pinned_false():
    resp = _ok_unpin()
    restored = PinSnapshotResponse.from_dict(resp.to_dict())
    assert restored.pinned is False
    assert restored.reason is None


def test_round_trip_error_status():
    resp = _error()
    restored = PinSnapshotResponse.from_dict(resp.to_dict())
    assert restored.status == "error"
    assert restored.reason == "unexpected_error"
    assert restored.pinned is False


def test_to_dict_is_json_friendly_primitives():
    payload = _ok_pin().to_dict()
    # Every value is a JSON-friendly primitive (str / int / bool / None).
    assert isinstance(payload["status"], str)
    assert payload["reason"] is None
    assert isinstance(payload["overlay_session_id"], int)
    assert isinstance(payload["snapshot_id"], str)
    assert isinstance(payload["pinned"], bool)
    # No surprise keys (no routing "type" key on a Schema-A response).
    assert set(payload) == {
        "status",
        "reason",
        "overlay_session_id",
        "snapshot_id",
        "pinned",
    }


# ---------------------------------------------------------------------------
# from_dict structural rejection.
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping():
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(["not", "a", "mapping"])


@pytest.mark.parametrize(
    "missing",
    ["status", "reason", "overlay_session_id", "snapshot_id", "pinned"],
)
def test_from_dict_rejects_missing_field(missing):
    payload = _ok_pin().to_dict()
    del payload[missing]
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_status_outside_closed_set():
    payload = _ok_pin().to_dict()
    payload["status"] = "not_implemented"
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_wrong_type_status():
    payload = _ok_pin().to_dict()
    payload["status"] = 1
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_wrong_type_snapshot_id():
    payload = _ok_pin().to_dict()
    payload["snapshot_id"] = 123
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_wrong_type_overlay_session_id():
    payload = _ok_pin().to_dict()
    payload["overlay_session_id"] = "7"
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_bool_for_overlay_session_id():
    # bool is a subclass of int; an echoed True must not be silently read as 1.
    payload = _ok_pin().to_dict()
    payload["overlay_session_id"] = True
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_non_bool_pinned():
    payload = _ok_pin().to_dict()
    payload["pinned"] = "yes"
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_rejects_int_pinned():
    # An int is NOT a bool here even though 1 is truthy: pinned is strictly bool.
    payload = _ok_pin().to_dict()
    payload["pinned"] = 1
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)


def test_from_dict_accepts_none_reason():
    payload = _ok_pin().to_dict()
    payload["reason"] = None
    restored = PinSnapshotResponse.from_dict(payload)
    assert restored.reason is None


def test_from_dict_rejects_wrong_type_reason():
    payload = _ok_pin().to_dict()
    payload["reason"] = 42
    with pytest.raises(PinSnapshotResponseSchemaError):
        PinSnapshotResponse.from_dict(payload)
