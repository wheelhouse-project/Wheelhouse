"""Tests for the text_target_grant_prompt IPC event schema (wh-bqv9c).

The schema defines the Logic -> GUI event the click counter forwards
when a tuple's verified-retry counter reaches the soft-allow threshold
(wh-82lnx publishes ``RetryThresholdReached`` on the EventBus; the
Logic handler that subscribes to that event forwards the GUI-bound
payload through this schema).

The GUI uses the payload to render the three-strikes follow-up toast
("Always type into <App> when you do this?"). The schema is parallel
to ``text_target_rejection`` -- platform metadata only, no dictation
text -- but adds the per-tuple ``count`` value so the toast body can
say how many times the user retried.

Coverage:
  * Round-trip through to_dict / from_dict.
  * to_dict carries MSG_TYPE.
  * from_dict rejects mismatched type, missing fields, wrong types.
  * count must be a positive int (>= 1); other values are rejected.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.text_target_grant_prompt import (
    MSG_TYPE,
    TextTargetGrantPromptEvent,
    TextTargetGrantPromptSchemaError,
)


def _sample_event(**overrides) -> TextTargetGrantPromptEvent:
    fields = dict(
        process_name="zed.exe",
        class_name="zed::Workspace",
        control_type="Pane",
        app_friendly_name="Zed",
        count=3,
    )
    fields.update(overrides)
    return TextTargetGrantPromptEvent(**fields)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_carries_msg_type():
    payload = _sample_event().to_dict()
    assert payload["type"] == MSG_TYPE
    assert payload["type"] == "text_target_grant_prompt"


def test_to_dict_includes_all_fields():
    payload = _sample_event().to_dict()
    assert payload["process_name"] == "zed.exe"
    assert payload["class_name"] == "zed::Workspace"
    assert payload["control_type"] == "Pane"
    assert payload["app_friendly_name"] == "Zed"
    assert payload["count"] == 3


def test_round_trip_via_dict():
    original = _sample_event()
    restored = TextTargetGrantPromptEvent.from_dict(original.to_dict())
    assert restored == original


def test_event_is_immutable():
    event = _sample_event()
    with pytest.raises(Exception):
        event.process_name = "other.exe"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping():
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict("not a mapping")  # type: ignore[arg-type]


def test_from_dict_rejects_missing_type():
    payload = _sample_event().to_dict()
    del payload["type"]
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


def test_from_dict_rejects_wrong_type():
    payload = _sample_event().to_dict()
    payload["type"] = "text_target_rejected"
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


@pytest.mark.parametrize(
    "missing",
    ["process_name", "class_name", "control_type", "app_friendly_name", "count"],
)
def test_from_dict_rejects_missing_field(missing):
    payload = _sample_event().to_dict()
    del payload[missing]
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


@pytest.mark.parametrize(
    "field",
    ["process_name", "class_name", "control_type", "app_friendly_name"],
)
def test_from_dict_rejects_non_string_field(field):
    payload = _sample_event().to_dict()
    payload[field] = 42
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


@pytest.mark.parametrize("bad_count", ["3", 3.0, None, [3]])
def test_from_dict_rejects_non_int_count(bad_count):
    payload = _sample_event().to_dict()
    payload["count"] = bad_count
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


@pytest.mark.parametrize("bad_count", [0, -1, -100])
def test_from_dict_rejects_non_positive_count(bad_count):
    payload = _sample_event().to_dict()
    payload["count"] = bad_count
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


def test_from_dict_rejects_bool_for_count():
    """``bool`` is a subclass of ``int`` in Python; reject it explicitly
    so a sender that drops ``True`` into the count field cannot pass
    schema validation as ``1``."""

    payload = _sample_event().to_dict()
    payload["count"] = True
    with pytest.raises(TextTargetGrantPromptSchemaError):
        TextTargetGrantPromptEvent.from_dict(payload)


# ---------------------------------------------------------------------------
# Privacy contract: the schema carries no user content.
# ---------------------------------------------------------------------------


def test_schema_carries_no_dictation_text_field():
    """The payload must not have any field that smells like user text."""

    payload = _sample_event().to_dict()
    forbidden = {"text", "dictation", "transcript", "utterance", "content",
                 "correlation_token"}
    assert set(payload.keys()).isdisjoint(forbidden)
