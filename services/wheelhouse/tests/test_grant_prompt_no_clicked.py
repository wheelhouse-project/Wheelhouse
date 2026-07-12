"""Tests for the grant_prompt_no_clicked GUI -> Logic IPC schema (wh-vdt1t).

When the user clicks No on the three-strikes follow-up toast, the GUI
manager forwards a ``grant_prompt_no_clicked`` action onto the
commands_to_logic_queue carrying the identity tuple. The Logic handler
records the tuple in a per-run suppression set so subsequent
RetryThresholdReached events for the same tuple do NOT re-fire the
follow-up toast in this run, even across a GUI restart.

The counter is intentionally NOT reset on No (per bead spec wh-vdt1t):
"It does NOT reset to 0 or advance to a never-ask state. The next
verified retry increments the counter further (4, 5, 6, ...)".

Coverage:
  * Round-trip through to_dict / from_dict.
  * to_dict carries ACTION_NAME in the "action" key.
  * from_dict rejects mismatched action, missing fields, wrong types.
  * Privacy contract: the schema carries no dictation text and no
    correlation_token.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.grant_prompt_no_clicked import (
    ACTION_NAME,
    GrantPromptNoClickedEvent,
    GrantPromptNoClickedSchemaError,
)


def _sample_event(**overrides) -> GrantPromptNoClickedEvent:
    fields = dict(
        process_name="zed.exe",
        class_name="zed::Workspace",
        control_type="Pane",
    )
    fields.update(overrides)
    return GrantPromptNoClickedEvent(**fields)


def test_to_dict_carries_action_name():
    payload = _sample_event().to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["action"] == "grant_prompt_no_clicked"


def test_to_dict_includes_all_fields():
    payload = _sample_event().to_dict()
    assert payload["process_name"] == "zed.exe"
    assert payload["class_name"] == "zed::Workspace"
    assert payload["control_type"] == "Pane"


def test_round_trip_via_dict():
    original = _sample_event()
    restored = GrantPromptNoClickedEvent.from_dict(original.to_dict())
    assert restored == original


def test_event_is_immutable():
    event = _sample_event()
    with pytest.raises(Exception):
        event.process_name = "other.exe"  # type: ignore[misc]


def test_from_dict_rejects_non_mapping():
    with pytest.raises(GrantPromptNoClickedSchemaError):
        GrantPromptNoClickedEvent.from_dict("not a mapping")  # type: ignore[arg-type]


def test_from_dict_rejects_missing_action():
    payload = _sample_event().to_dict()
    del payload["action"]
    with pytest.raises(GrantPromptNoClickedSchemaError):
        GrantPromptNoClickedEvent.from_dict(payload)


def test_from_dict_rejects_wrong_action():
    payload = _sample_event().to_dict()
    payload["action"] = "grant_prompt_yes_clicked"
    with pytest.raises(GrantPromptNoClickedSchemaError):
        GrantPromptNoClickedEvent.from_dict(payload)


@pytest.mark.parametrize(
    "missing", ["process_name", "class_name", "control_type"],
)
def test_from_dict_rejects_missing_field(missing):
    payload = _sample_event().to_dict()
    del payload[missing]
    with pytest.raises(GrantPromptNoClickedSchemaError):
        GrantPromptNoClickedEvent.from_dict(payload)


@pytest.mark.parametrize(
    "field", ["process_name", "class_name", "control_type"],
)
def test_from_dict_rejects_non_string_field(field):
    payload = _sample_event().to_dict()
    payload[field] = 42
    with pytest.raises(GrantPromptNoClickedSchemaError):
        GrantPromptNoClickedEvent.from_dict(payload)


def test_schema_carries_no_text_or_token():
    payload = _sample_event().to_dict()
    forbidden = {
        "text", "dictation", "transcript", "utterance", "content",
        "correlation_token",
    }
    assert set(payload.keys()).isdisjoint(forbidden)
