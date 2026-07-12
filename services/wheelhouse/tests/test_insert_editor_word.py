"""Tests for the insert_editor_word IPC contract (wh-g2-refactor.14).

The schema defines the Logic -> GUI per-word insert request that fires
once per ``WordEvent`` reaching the persistent dictation editor. Each
request becomes a separate round trip on the same queues used by
``retract_editor_text``. Section 5 of
``docs/design/2026-05-20-g2-refactor-design-refinements.md`` is the
authoritative reference; the rebuild-fence fields (round 2 / codex
7.5) and the ``session_mismatch`` change (round 2 / codex 7.4) are
both modelled here.

Coverage:
  * Request round-trips through to_dict / from_dict.
  * Request rejects empty / non-string text, empty request_id /
    utterance_id, and negative or non-int editor_generation.
  * Response round-trips for each enumerated failure_reason.
  * Response rejects negative chars_inserted and unknown failure
    reasons.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.insert_editor_word import (
    ACTION_NAME_REQUEST,
    ACTION_NAME_RESPONSE,
    ALLOWED_FAILURE_REASONS,
    FAILURE_EDITOR_REBUILT,
    FAILURE_EDITOR_UNAVAILABLE,
    FAILURE_NO_ACTIVE_SESSION,
    FAILURE_SESSION_MISMATCH,
    FAILURE_STALE_GENERATION,
    FAILURE_SUCCESS,
    InsertEditorWordRequest,
    InsertEditorWordResponse,
    InsertEditorWordSchemaError,
)


_RID = "abcd" * 8
_UID = "u" * 16


def test_action_name_constants_match_design_doc():
    assert ACTION_NAME_REQUEST == "insert_editor_word"
    assert ACTION_NAME_RESPONSE == "insert_editor_word_response"


def test_failure_reason_constants_match_design_doc():
    assert FAILURE_SUCCESS == ""
    assert FAILURE_NO_ACTIVE_SESSION == "no_active_session"
    assert FAILURE_SESSION_MISMATCH == "session_mismatch"
    assert FAILURE_EDITOR_UNAVAILABLE == "editor_unavailable"
    assert FAILURE_STALE_GENERATION == "stale_generation"
    assert FAILURE_EDITOR_REBUILT == "editor_rebuilt"
    assert ALLOWED_FAILURE_REASONS == frozenset({
        FAILURE_SUCCESS,
        FAILURE_NO_ACTIVE_SESSION,
        FAILURE_SESSION_MISMATCH,
        FAILURE_EDITOR_UNAVAILABLE,
        FAILURE_STALE_GENERATION,
        FAILURE_EDITOR_REBUILT,
    })


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def test_request_round_trip():
    original = InsertEditorWordRequest(
        request_id=_RID,
        text="hello",
        utterance_id=_UID,
        editor_generation=2,
    )
    restored = InsertEditorWordRequest.from_dict(original.to_dict())
    assert restored == original


def test_request_to_dict_shape():
    req = InsertEditorWordRequest(
        request_id=_RID,
        text="hello",
        utterance_id=_UID,
        editor_generation=2,
    )
    d = req.to_dict()
    assert d["action"] == ACTION_NAME_REQUEST
    assert d["request_id"] == _RID
    assert d["text"] == "hello"
    assert d["utterance_id"] == _UID
    assert d["editor_generation"] == 2


def test_request_is_immutable():
    req = InsertEditorWordRequest(
        request_id=_RID,
        text="x",
        utterance_id=_UID,
        editor_generation=0,
    )
    with pytest.raises(Exception):
        req.text = "y"  # type: ignore[misc]


def test_request_rejects_empty_request_id():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id="",
            text="hello",
            utterance_id=_UID,
            editor_generation=0,
        )


def test_request_rejects_non_string_request_id():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=42,  # type: ignore[arg-type]
            text="hello",
            utterance_id=_UID,
            editor_generation=0,
        )


def test_request_rejects_empty_text():
    """Section 5: 'Empty string is invalid (caller filters).'"""
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=_RID,
            text="",
            utterance_id=_UID,
            editor_generation=0,
        )


def test_request_rejects_non_string_text():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=_RID,
            text=None,  # type: ignore[arg-type]
            utterance_id=_UID,
            editor_generation=0,
        )


def test_request_rejects_empty_utterance_id():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=_RID,
            text="hello",
            utterance_id="",
            editor_generation=0,
        )


def test_request_rejects_negative_editor_generation():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=_RID,
            text="hello",
            utterance_id=_UID,
            editor_generation=-1,
        )


def test_request_rejects_bool_editor_generation():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest(
            request_id=_RID,
            text="hello",
            utterance_id=_UID,
            editor_generation=False,  # type: ignore[arg-type]
        )


def test_request_from_dict_rejects_wrong_action():
    payload = {
        "action": "not_insert",
        "request_id": _RID,
        "text": "x",
        "utterance_id": _UID,
        "editor_generation": 0,
    }
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordRequest.from_dict(payload)


def test_request_from_dict_rejects_missing_fields():
    base = {
        "action": ACTION_NAME_REQUEST,
        "request_id": _RID,
        "text": "x",
        "utterance_id": _UID,
        "editor_generation": 0,
    }
    for missing in ("request_id", "text", "utterance_id", "editor_generation"):
        payload = dict(base)
        del payload[missing]
        with pytest.raises(InsertEditorWordSchemaError):
            InsertEditorWordRequest.from_dict(payload)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def test_response_success_round_trip():
    r = InsertEditorWordResponse(
        request_id=_RID,
        chars_inserted=5,
        failure_reason=FAILURE_SUCCESS,
    )
    restored = InsertEditorWordResponse.from_dict(r.to_dict())
    assert restored == r


@pytest.mark.parametrize("reason", sorted(ALLOWED_FAILURE_REASONS - {FAILURE_SUCCESS}))
def test_response_round_trips_each_failure_reason(reason):
    original = InsertEditorWordResponse(
        request_id=_RID,
        chars_inserted=0,
        failure_reason=reason,
    )
    restored = InsertEditorWordResponse.from_dict(original.to_dict())
    assert restored == original


def test_response_rejects_negative_chars_inserted():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordResponse(
            request_id=_RID,
            chars_inserted=-1,
            failure_reason=FAILURE_SUCCESS,
        )


def test_response_rejects_unknown_failure_reason():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordResponse(
            request_id=_RID,
            chars_inserted=0,
            failure_reason="not_a_known_reason",
        )


def test_response_rejects_empty_request_id():
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordResponse(
            request_id="",
            chars_inserted=0,
            failure_reason=FAILURE_SUCCESS,
        )


def test_response_from_dict_rejects_wrong_action():
    payload = {
        "action": "not_insert_response",
        "request_id": _RID,
        "chars_inserted": 0,
        "failure_reason": FAILURE_SUCCESS,
    }
    with pytest.raises(InsertEditorWordSchemaError):
        InsertEditorWordResponse.from_dict(payload)


def test_response_from_dict_rejects_missing_fields():
    base = {
        "action": ACTION_NAME_RESPONSE,
        "request_id": _RID,
        "chars_inserted": 0,
        "failure_reason": FAILURE_SUCCESS,
    }
    for missing in ("request_id", "chars_inserted", "failure_reason"):
        payload = dict(base)
        del payload[missing]
        with pytest.raises(InsertEditorWordSchemaError):
            InsertEditorWordResponse.from_dict(payload)
