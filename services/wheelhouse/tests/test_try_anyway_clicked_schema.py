"""Tests for the try_anyway_clicked GUI -> Logic event schema (wh-iycks).

The schema is the contract for the GUI-to-Logic message that fires
when the user clicks "Try it anyway" on a rejection toast. The Logic
handler validates payloads via :func:`safe_parse` so a malformed sender
(a version-skewed GUI, a bug in send_command) cannot crash the
GUI command listener.
"""

from __future__ import annotations

import uuid

import pytest

from shared.try_anyway_clicked import (
    ACTION_NAME,
    TryAnywayClickedEvent,
    TryAnywayClickedSchemaError,
)


def _token() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_to_dict_round_trips_through_from_dict():
    token = _token()
    event = TryAnywayClickedEvent(correlation_token=token)
    payload = event.to_dict()
    assert payload["action"] == ACTION_NAME
    assert payload["correlation_token"] == token
    parsed = TryAnywayClickedEvent.from_dict(payload)
    assert parsed == event


def test_action_name_constant_is_canonical():
    assert ACTION_NAME == "try_anyway_clicked"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestRejection:
    def test_non_mapping_payload_raises(self):
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict("not a dict")

    def test_missing_action_raises(self):
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({"correlation_token": _token()})

    def test_wrong_action_raises(self):
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({
                "action": "show_rejection_toast",
                "correlation_token": _token(),
            })

    def test_missing_correlation_token_raises(self):
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({"action": ACTION_NAME})

    def test_non_uuid_correlation_token_raises(self):
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({
                "action": ACTION_NAME,
                "correlation_token": "not-a-uuid",
            })

    def test_uuid_other_version_raises(self):
        # uuid1 is version 1; the contract is uuid4.
        non_v4 = str(uuid.uuid1())
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({
                "action": ACTION_NAME,
                "correlation_token": non_v4,
            })

    def test_correlation_token_with_whitespace_raises(self):
        # Canonical-form check rejects leading whitespace so a sender
        # cannot smuggle non-token bytes into the trusted field.
        token = _token()
        with pytest.raises(TryAnywayClickedSchemaError):
            TryAnywayClickedEvent.from_dict({
                "action": ACTION_NAME,
                "correlation_token": " " + token,
            })


# ---------------------------------------------------------------------------
# Privacy property
# ---------------------------------------------------------------------------


def test_to_dict_carries_only_action_and_correlation_token():
    """Privacy property: the wire payload has no dictation-text field.

    The Logic process must not see the dictation text. The structural
    fence is that the dataclass has only one user-supplied field and
    to_dict emits only ``action`` and ``correlation_token``.
    """

    event = TryAnywayClickedEvent(correlation_token=_token())
    payload = event.to_dict()
    assert set(payload.keys()) == {"action", "correlation_token"}
    forbidden = {"text", "dictation", "transcript", "utterance", "content"}
    assert set(payload.keys()).isdisjoint(forbidden)
