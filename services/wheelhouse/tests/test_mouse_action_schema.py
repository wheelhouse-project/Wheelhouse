"""Schema tests for MouseActionResponse (wh-input-mouse-primitives).

``shared/mouse_action.py`` carries the Input -> Logic reply for the three
mouse-grid pointer actions (``click_point``, ``move_pointer``,
``perform_drag``). Like ``ClickElementResponse`` and ``PinSnapshotResponse``
it is a request-correlated Schema A response: the Input handler emits exactly
one per ``request_id`` via the ``_HANDLES_OWN_RESPONSE`` machinery, so the
schema carries no routing ``type`` key and ``status`` is the Schema A status
field.

``from_dict`` must degrade gracefully per wh-uf54: only the typed
``MouseActionResponseSchemaError`` escapes, never a raw KeyError / TypeError /
AttributeError.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from services.wheelhouse.shared.mouse_action import (
    MouseActionResponse,
    MouseActionResponseSchemaError,
)


def _ok_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "outcome": "ok",
        "reason": None,
        "trace_id": "trace-1",
    }


def test_round_trips_a_success():
    response = MouseActionResponse(
        status="ok", outcome="ok", reason=None, trace_id="trace-1",
    )

    assert MouseActionResponse.from_dict(response.to_dict()) == response


def test_round_trips_a_failure_with_a_reason():
    response = MouseActionResponse(
        status="error",
        outcome="execution_failed",
        reason="cursor_did_not_land",
        trace_id="trace-2",
    )

    assert MouseActionResponse.from_dict(response.to_dict()) == response


def test_to_dict_emits_only_the_contract_fields():
    payload = MouseActionResponse(
        status="ok", outcome="ok", reason=None, trace_id="t",
    ).to_dict()

    assert set(payload) == {"status", "outcome", "reason", "trace_id"}


@pytest.mark.parametrize("field", ["status", "outcome", "reason", "trace_id"])
def test_missing_field_raises_the_typed_error(field):
    payload = _ok_payload()
    del payload[field]

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


@pytest.mark.parametrize("status", ["OK", "", "not_implemented", "success"])
def test_status_outside_the_closed_set_raises(status):
    payload = _ok_payload()
    payload["status"] = status

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


@pytest.mark.parametrize(
    "outcome", ["OK", "", "not_found", "ambiguous", "failed"],
)
def test_outcome_outside_the_closed_set_raises(outcome):
    payload = _ok_payload()
    payload["outcome"] = outcome

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_reason_may_be_any_string():
    # The reason tag set is deliberately OPEN so the primitives can grow new
    # tags without a schema change, matching ClickElementResponse.
    payload = _ok_payload()
    payload["status"] = "error"
    payload["outcome"] = "execution_failed"
    payload["reason"] = "a_tag_the_schema_has_never_heard_of"

    assert MouseActionResponse.from_dict(payload).reason == (
        "a_tag_the_schema_has_never_heard_of"
    )


@pytest.mark.parametrize("bad", [5, [], {}, True])
def test_non_string_reason_raises(bad):
    payload = _ok_payload()
    payload["reason"] = bad

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


@pytest.mark.parametrize("bad", [None, 5, ["t"]])
def test_non_string_trace_id_raises(bad):
    payload = _ok_payload()
    payload["trace_id"] = bad

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


@pytest.mark.parametrize("payload", [None, "ok", 5, ["status"]])
def test_non_mapping_payload_raises(payload):
    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_extra_wire_keys_are_ignored():
    # The Input handler attaches request_id + action to the same dict before
    # putting it on the response queue, so from_dict must tolerate them.
    payload = _ok_payload()
    payload["request_id"] = "req-1"
    payload["action"] = "click_point"

    assert MouseActionResponse.from_dict(payload).status == "ok"


def test_hostile_mapping_degrades_to_the_typed_error():
    class Hostile(Mapping):
        def __contains__(self, key):  # noqa: D105
            raise AttributeError("hostile")

        def __getitem__(self, key):  # noqa: D105
            raise KeyError(key)

        def __iter__(self):  # noqa: D105
            return iter(())

        def __len__(self):  # noqa: D105
            return 0

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(Hostile())


# ---------------------------------------------------------------------------
# status/outcome/reason contract (wh-mouse-grid.1.22).
#
# The Input handler emits exactly two shapes: status="ok" + outcome="ok" +
# reason=None, or status="error" + outcome="execution_failed" + a nonempty
# reason. Validating the three fields independently accepted impossible
# hybrids -- status="error" with outcome="ok" parsed, and Logic (which decides
# success from outcome alone) silently treated the failed reply as a completed
# click with no stuck-button notice. from_dict enforces the pair rule and the
# reason rule; the reason TAG set stays open (any nonempty string).
# ---------------------------------------------------------------------------


def test_error_status_with_ok_outcome_raises():
    payload = _ok_payload()
    payload["status"] = "error"
    payload["reason"] = "release_failed"

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_error_status_with_ok_outcome_and_no_reason_raises():
    payload = _ok_payload()
    payload["status"] = "error"

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_ok_status_with_execution_failed_outcome_raises():
    payload = _ok_payload()
    payload["outcome"] = "execution_failed"
    payload["reason"] = "sendinput_error"

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_success_with_a_reason_raises():
    payload = _ok_payload()
    payload["reason"] = "release_failed"

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_failure_without_a_reason_raises():
    payload = _ok_payload()
    payload["status"] = "error"
    payload["outcome"] = "execution_failed"

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)


def test_failure_with_an_empty_reason_raises():
    payload = _ok_payload()
    payload["status"] = "error"
    payload["outcome"] = "execution_failed"
    payload["reason"] = ""

    with pytest.raises(MouseActionResponseSchemaError):
        MouseActionResponse.from_dict(payload)
