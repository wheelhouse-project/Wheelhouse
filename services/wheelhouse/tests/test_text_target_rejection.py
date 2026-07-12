"""Tests for the text_target_rejected IPC event schema (wh-hqipv).

The schema defines the unsolicited Input -> Logic event emitted when an
insertion strategy rejects a focused control as not a valid text target.
Logic forwards the event to the GUI, which renders an advisory toast
(wh-9weum, Phase 2). The correlation_token threads the round trip from
rejection through the optional Try-it-anyway retry-click in Phase 4.

Coverage:
  * The dataclass round-trips through to_dict / from_dict.
  * to_dict carries MSG_TYPE in the "type" key.
  * from_dict rejects mismatched "type", missing fields, wrong types.
  * supported_patterns is normalized to a tuple on parse, so a sender
    that puts a list (e.g. via JSON-bridged transport) is accepted.
  * new_correlation_token returns a uuid4 string.
"""

from __future__ import annotations

import uuid

import pytest

from services.wheelhouse.shared.text_target_rejection import (
    MSG_TYPE,
    TextTargetRejectedEvent,
    TextTargetRejectedSchemaError,
    new_correlation_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_event(**overrides) -> TextTargetRejectedEvent:
    fields = dict(
        process_name="zed.exe",
        class_name="zed::Workspace",
        control_type="Pane",
        reason="no_text_pattern",
        supported_patterns=("Invoke", "ScrollItem"),
        app_friendly_name="Zed",
        correlation_token="11111111-1111-4111-8111-111111111111",
    )
    fields.update(overrides)
    return TextTargetRejectedEvent(**fields)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_carries_msg_type():
    event = _sample_event()
    payload = event.to_dict()
    assert payload["type"] == MSG_TYPE
    assert payload["type"] == "text_target_rejected"


def test_to_dict_includes_all_fields():
    event = _sample_event()
    payload = event.to_dict()
    assert payload["process_name"] == "zed.exe"
    assert payload["class_name"] == "zed::Workspace"
    assert payload["control_type"] == "Pane"
    assert payload["reason"] == "no_text_pattern"
    assert payload["supported_patterns"] == ("Invoke", "ScrollItem")
    assert payload["app_friendly_name"] == "Zed"
    assert payload["correlation_token"] == "11111111-1111-4111-8111-111111111111"


def test_round_trip_via_dict():
    original = _sample_event()
    payload = original.to_dict()
    restored = TextTargetRejectedEvent.from_dict(payload)
    assert restored == original


def test_from_dict_normalizes_list_to_tuple():
    payload = _sample_event().to_dict()
    payload["supported_patterns"] = ["Invoke", "ScrollItem"]  # JSON-style
    restored = TextTargetRejectedEvent.from_dict(payload)
    assert restored.supported_patterns == ("Invoke", "ScrollItem")
    assert isinstance(restored.supported_patterns, tuple)


def test_event_is_immutable():
    event = _sample_event()
    with pytest.raises(Exception):
        event.process_name = "other.exe"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_from_dict_rejects_wrong_type():
    payload = _sample_event().to_dict()
    payload["type"] = "shell_busy"
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict(payload)


def test_from_dict_rejects_missing_type():
    payload = _sample_event().to_dict()
    del payload["type"]
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict(payload)


@pytest.mark.parametrize(
    "missing_field",
    [
        "process_name",
        "class_name",
        "control_type",
        "reason",
        "supported_patterns",
        "app_friendly_name",
        "correlation_token",
    ],
)
def test_from_dict_rejects_missing_field(missing_field: str):
    payload = _sample_event().to_dict()
    del payload[missing_field]
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert missing_field in str(exc_info.value)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("process_name", 42),
        ("class_name", None),
        ("control_type", []),
        ("reason", 1.5),
        ("app_friendly_name", b"bytes-not-str"),
        ("correlation_token", 0),
    ],
)
def test_from_dict_rejects_non_string_field(field: str, bad_value):
    payload = _sample_event().to_dict()
    payload[field] = bad_value
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert field in str(exc_info.value)


def test_from_dict_rejects_non_iterable_supported_patterns():
    payload = _sample_event().to_dict()
    payload["supported_patterns"] = 42
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert "supported_patterns" in str(exc_info.value)


def test_from_dict_rejects_broken_iterable_supported_patterns():
    """wh-9weum.1.1: a malformed iterable that raises during iteration
    must not bubble TypeError up past safe_parse. The schema must
    restrict accepted shapes to list / tuple."""

    class BrokenIterable:
        def __iter__(self):
            raise RuntimeError("malicious iterable")

    payload = _sample_event().to_dict()
    payload["supported_patterns"] = BrokenIterable()
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert "supported_patterns" in str(exc_info.value)


def test_from_dict_rejects_set_supported_patterns():
    """A set is iterable but unordered; not in the documented wire shape."""

    payload = _sample_event().to_dict()
    payload["supported_patterns"] = {"Invoke", "ScrollItem"}
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict(payload)


def test_from_dict_rejects_non_string_supported_pattern_member():
    payload = _sample_event().to_dict()
    payload["supported_patterns"] = ("Invoke", 42)
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert "supported_patterns" in str(exc_info.value)


def test_from_dict_accepts_empty_supported_patterns():
    payload = _sample_event().to_dict()
    payload["supported_patterns"] = ()
    restored = TextTargetRejectedEvent.from_dict(payload)
    assert restored.supported_patterns == ()


def test_from_dict_rejects_non_dict_input():
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict("not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Correlation token helper
# ---------------------------------------------------------------------------


def test_new_correlation_token_is_uuid4_string():
    token = new_correlation_token()
    assert isinstance(token, str)
    parsed = uuid.UUID(token)
    assert parsed.version == 4


def test_new_correlation_token_is_unique():
    tokens = {new_correlation_token() for _ in range(50)}
    assert len(tokens) == 50


# ---------------------------------------------------------------------------
# correlation_token shape (wh-9weum.1.3)
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_uuid_correlation_token():
    """Plain strings that are not uuid-shaped must be rejected to keep
    the token field opaque (no leak channel for arbitrary content)."""

    payload = _sample_event().to_dict()
    payload["correlation_token"] = "not-a-uuid"
    with pytest.raises(TextTargetRejectedSchemaError) as exc_info:
        TextTargetRejectedEvent.from_dict(payload)
    assert "correlation_token" in str(exc_info.value)


def test_from_dict_rejects_non_v4_uuid_correlation_token():
    """uuid1 (time-based) and uuid5 (name-based) must be rejected."""

    # uuid1: 4f7e3c70-2c3a-11ee-be56-0242ac120002 (real-looking v1)
    payload = _sample_event().to_dict()
    payload["correlation_token"] = "4f7e3c70-2c3a-11ee-be56-0242ac120002"
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict(payload)


def test_from_dict_rejects_non_canonical_uuid_form():
    """The braced/urn forms uuid.UUID accepts must be rejected so a
    sender cannot smuggle extra characters through the token field."""

    payload = _sample_event().to_dict()
    payload["correlation_token"] = "{11111111-1111-4111-8111-111111111111}"
    with pytest.raises(TextTargetRejectedSchemaError):
        TextTargetRejectedEvent.from_dict(payload)


def test_round_trip_preserves_valid_uuid4_token():
    """Defensive: verify the validator does not break the existing
    valid-token round trip."""

    original = _sample_event()
    restored = TextTargetRejectedEvent.from_dict(original.to_dict())
    assert restored.correlation_token == original.correlation_token
