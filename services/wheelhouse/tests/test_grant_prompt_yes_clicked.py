"""Tests for the grant_prompt_yes_clicked GUI -> Logic IPC schema (wh-8d81z).

When the user clicks Yes on the three-strikes follow-up toast, the GUI
manager forwards a ``grant_prompt_yes_clicked`` action onto the
commands_to_logic_queue carrying the identity tuple. The Logic handler
calls ``add_soft_allow`` (writes the soft-allow file, then sends the
add_soft_allow_tuple IPC to the input process) and resets the click
counter for that tuple on success.

Coverage:
  * Round-trip through to_dict / from_dict.
  * to_dict carries ACTION_NAME in the "action" key.
  * from_dict rejects mismatched action, missing fields, wrong types.
  * Privacy contract: the schema carries no dictation text and no
    correlation_token (the rejection cache token is not threaded
    through this event).
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.grant_prompt_yes_clicked import (
    ACTION_NAME,
    GrantPromptYesClickedEvent,
    GrantPromptYesClickedSchemaError,
)


def _sample_event(**overrides) -> GrantPromptYesClickedEvent:
    fields = dict(
        process_name="zed.exe",
        class_name="zed::Workspace",
        control_type="Pane",
    )
    fields.update(overrides)
    return GrantPromptYesClickedEvent(**fields)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_carries_action_name():
    payload = _sample_event().to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["action"] == "grant_prompt_yes_clicked"


def test_to_dict_includes_all_fields():
    payload = _sample_event().to_dict()
    assert payload["process_name"] == "zed.exe"
    assert payload["class_name"] == "zed::Workspace"
    assert payload["control_type"] == "Pane"


def test_round_trip_via_dict():
    original = _sample_event()
    restored = GrantPromptYesClickedEvent.from_dict(original.to_dict())
    assert restored == original


def test_event_is_immutable():
    event = _sample_event()
    with pytest.raises(Exception):
        event.process_name = "other.exe"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_from_dict_rejects_non_mapping():
    with pytest.raises(GrantPromptYesClickedSchemaError):
        GrantPromptYesClickedEvent.from_dict("not a mapping")  # type: ignore[arg-type]


def test_from_dict_rejects_missing_action():
    payload = _sample_event().to_dict()
    del payload["action"]
    with pytest.raises(GrantPromptYesClickedSchemaError):
        GrantPromptYesClickedEvent.from_dict(payload)


def test_from_dict_rejects_wrong_action():
    payload = _sample_event().to_dict()
    payload["action"] = "try_anyway_clicked"
    with pytest.raises(GrantPromptYesClickedSchemaError):
        GrantPromptYesClickedEvent.from_dict(payload)


@pytest.mark.parametrize(
    "missing", ["process_name", "class_name", "control_type"],
)
def test_from_dict_rejects_missing_field(missing):
    payload = _sample_event().to_dict()
    del payload[missing]
    with pytest.raises(GrantPromptYesClickedSchemaError):
        GrantPromptYesClickedEvent.from_dict(payload)


@pytest.mark.parametrize(
    "field", ["process_name", "class_name", "control_type"],
)
def test_from_dict_rejects_non_string_field(field):
    payload = _sample_event().to_dict()
    payload[field] = 42
    with pytest.raises(GrantPromptYesClickedSchemaError):
        GrantPromptYesClickedEvent.from_dict(payload)


def test_schema_carries_no_text_or_token():
    """Privacy property: the payload must not have any field that
    smells like user-typed content or a correlation_token."""

    payload = _sample_event().to_dict()
    forbidden = {
        "text", "dictation", "transcript", "utterance", "content",
        "correlation_token",
    }
    assert set(payload.keys()).isdisjoint(forbidden)
