"""Tests for the retract_editor_text IPC contract (wh-g2-refactor.14).

The schema defines the Logic -> GUI retract-and-replay request that fires
when the speech processor needs to undo previously-inserted text in the
persistent dictation editor and optionally replay corrected text in the
same Qt main-thread call. Section 2 of
docs/design/2026-05-20-g2-refactor-design-refinements.md is the
authoritative reference; the response schema includes the round-1 codex
``chars_requested`` echo and the round-2 codex ``editor_generation`` /
``stale_generation`` / ``editor_rebuilt`` fields.

Coverage:
  * Request round-trips through to_dict / from_dict and rejects malformed
    payloads (missing fields, wrong types, empty request_id /
    utterance_id, non-positive chars_requested, negative
    editor_generation, non-string replay_text).
  * replay_text == "" is valid (retract-only call).
  * Response round-trips for each enumerated failure_reason and validates
    the success-path invariant chars_removed == chars_requested.
  * Response schema enforces non-negative chars_removed and replay_chars.
  * Both schemas use the boundary-validation pattern other shared schemas
    follow (wh-uf54).
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.retract_editor_text import (
    ACTION_NAME_REQUEST,
    ACTION_NAME_RESPONSE,
    ALLOWED_FAILURE_REASONS,
    FAILURE_EDITOR_REBUILT,
    FAILURE_EDITOR_UNAVAILABLE,
    FAILURE_LEDGER_UNDERRUN,
    FAILURE_NO_ACTIVE_SESSION,
    FAILURE_REPLAY_FAILED,
    FAILURE_SESSION_MISMATCH,
    FAILURE_STALE_GENERATION,
    FAILURE_SUCCESS,
    RetractEditorTextRequest,
    RetractEditorTextResponse,
    RetractEditorTextSchemaError,
)


_RID = "abcd" * 8                # 32-char uuid4 hex
_UID = "u" * 16                  # arbitrary utterance id


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_action_name_constants_match_design_doc():
    assert ACTION_NAME_REQUEST == "retract_editor_text"
    assert ACTION_NAME_RESPONSE == "retract_editor_text_response"


def test_failure_reason_constants_match_design_doc():
    assert FAILURE_SUCCESS == ""
    assert FAILURE_LEDGER_UNDERRUN == "ledger_underrun"
    assert FAILURE_NO_ACTIVE_SESSION == "no_active_session"
    assert FAILURE_SESSION_MISMATCH == "session_mismatch"
    assert FAILURE_EDITOR_UNAVAILABLE == "editor_unavailable"
    assert FAILURE_REPLAY_FAILED == "replay_failed"
    assert FAILURE_STALE_GENERATION == "stale_generation"
    assert FAILURE_EDITOR_REBUILT == "editor_rebuilt"
    assert ALLOWED_FAILURE_REASONS == frozenset({
        FAILURE_SUCCESS,
        FAILURE_LEDGER_UNDERRUN,
        FAILURE_NO_ACTIVE_SESSION,
        FAILURE_SESSION_MISMATCH,
        FAILURE_EDITOR_UNAVAILABLE,
        FAILURE_REPLAY_FAILED,
        FAILURE_STALE_GENERATION,
        FAILURE_EDITOR_REBUILT,
    })


# ---------------------------------------------------------------------------
# Request: round-trip and shape
# ---------------------------------------------------------------------------


def test_request_to_dict_shape():
    req = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=5,
        utterance_id=_UID,
        replay_text="hello",
        editor_generation=3,
    )
    payload = req.to_dict()
    assert payload["action"] == ACTION_NAME_REQUEST
    assert payload["request_id"] == _RID
    assert payload["chars_requested"] == 5
    assert payload["utterance_id"] == _UID
    assert payload["replay_text"] == "hello"
    assert payload["editor_generation"] == 3


def test_request_round_trip_via_dict():
    original = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=12,
        utterance_id=_UID,
        replay_text="corrected text",
        editor_generation=0,
    )
    restored = RetractEditorTextRequest.from_dict(original.to_dict())
    assert restored == original


def test_request_round_trip_retract_only():
    """replay_text == '' means retract-only (round 1 F1 design)."""
    original = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=3,
        utterance_id=_UID,
        replay_text="",
        editor_generation=1,
    )
    restored = RetractEditorTextRequest.from_dict(original.to_dict())
    assert restored == original
    assert restored.replay_text == ""


def test_request_is_immutable():
    req = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=1,
        utterance_id=_UID,
        replay_text="",
        editor_generation=0,
    )
    with pytest.raises(Exception):
        req.request_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Request: validation
# ---------------------------------------------------------------------------


def test_request_rejects_empty_request_id():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id="",
            chars_requested=1,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_non_string_request_id():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=123,  # type: ignore[arg-type]
            chars_requested=1,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_zero_chars_requested():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=0,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_negative_chars_requested():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=-3,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_non_int_chars_requested():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested="5",  # type: ignore[arg-type]
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_empty_utterance_id():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id="",
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_non_string_utterance_id():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id=42,  # type: ignore[arg-type]
            replay_text="",
            editor_generation=0,
        )


def test_request_rejects_non_string_replay_text():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id=_UID,
            replay_text=None,  # type: ignore[arg-type]
            editor_generation=0,
        )


def test_request_rejects_negative_editor_generation():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id=_UID,
            replay_text="",
            editor_generation=-1,
        )


def test_request_rejects_non_int_editor_generation():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id=_UID,
            replay_text="",
            editor_generation="0",  # type: ignore[arg-type]
        )


def test_request_rejects_bool_editor_generation_isolated_from_int():
    """bool is a subclass of int in Python; the schema must reject it."""
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=1,
            utterance_id=_UID,
            replay_text="",
            editor_generation=True,  # type: ignore[arg-type]
        )


def test_request_rejects_bool_chars_requested_isolated_from_int():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=True,  # type: ignore[arg-type]
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_from_dict_rejects_non_mapping():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest.from_dict("not a mapping")  # type: ignore[arg-type]


def test_request_from_dict_rejects_wrong_action():
    payload = {
        "action": "not_retract",
        "request_id": _RID,
        "chars_requested": 1,
        "utterance_id": _UID,
        "replay_text": "",
        "editor_generation": 0,
    }
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest.from_dict(payload)


def test_request_from_dict_rejects_missing_fields():
    for missing in (
        "request_id",
        "chars_requested",
        "utterance_id",
        "replay_text",
        "editor_generation",
    ):
        payload = {
            "action": ACTION_NAME_REQUEST,
            "request_id": _RID,
            "chars_requested": 1,
            "utterance_id": _UID,
            "replay_text": "",
            "editor_generation": 0,
        }
        del payload[missing]
        with pytest.raises(RetractEditorTextSchemaError):
            RetractEditorTextRequest.from_dict(payload)


# ---------------------------------------------------------------------------
# Response: round-trip and shape
# ---------------------------------------------------------------------------


def test_response_success_round_trip():
    original = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=5,
        chars_removed=5,
        replay_chars=8,
        failure_reason=FAILURE_SUCCESS,
    )
    payload = original.to_dict()
    assert payload["action"] == ACTION_NAME_RESPONSE
    restored = RetractEditorTextResponse.from_dict(payload)
    assert restored == original


@pytest.mark.parametrize("reason", sorted(ALLOWED_FAILURE_REASONS - {FAILURE_SUCCESS}))
def test_response_round_trips_each_failure_reason(reason):
    original = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=4,
        chars_removed=0 if reason in (
            FAILURE_NO_ACTIVE_SESSION,
            FAILURE_SESSION_MISMATCH,
            FAILURE_EDITOR_UNAVAILABLE,
            FAILURE_STALE_GENERATION,
            FAILURE_EDITOR_REBUILT,
        ) else 2,
        replay_chars=0,
        failure_reason=reason,
    )
    restored = RetractEditorTextResponse.from_dict(original.to_dict())
    assert restored == original


def test_response_is_immutable():
    r = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=1,
        chars_removed=1,
        replay_chars=0,
        failure_reason=FAILURE_SUCCESS,
    )
    with pytest.raises(Exception):
        r.request_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Response: validation
# ---------------------------------------------------------------------------


def test_response_rejects_empty_request_id():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id="",
            chars_requested=1,
            chars_removed=1,
            replay_chars=0,
            failure_reason=FAILURE_SUCCESS,
        )


def test_response_rejects_unknown_failure_reason():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=1,
            chars_removed=0,
            replay_chars=0,
            failure_reason="not_a_known_reason",
        )


def test_response_rejects_negative_chars_removed():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=1,
            chars_removed=-1,
            replay_chars=0,
            failure_reason=FAILURE_LEDGER_UNDERRUN,
        )


def test_response_rejects_negative_replay_chars():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=1,
            chars_removed=1,
            replay_chars=-1,
            failure_reason=FAILURE_SUCCESS,
        )


def test_response_allows_minus_one_chars_requested_for_synthetic_rebuild():
    """The editor_rebuilt fan-out synthesises chars_requested == -1.

    Section 6's Logic-side handler builds the synthetic failure
    response with ``chars_requested == -1`` because the request is
    being abandoned; the boundary check in the speech processor will
    log the mismatch and drop the word. The schema MUST allow -1 so
    that synthetic case is well-formed.
    """
    r = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=-1,
        chars_removed=0,
        replay_chars=0,
        failure_reason=FAILURE_EDITOR_REBUILT,
    )
    assert r.chars_requested == -1


def test_response_rejects_chars_requested_below_minus_one():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=-2,
            chars_removed=0,
            replay_chars=0,
            failure_reason=FAILURE_EDITOR_REBUILT,
        )


def test_response_success_requires_chars_removed_eq_chars_requested():
    """Round 1 / codex finding C, wh-g2-refactor.5.3.

    A success response is malformed if chars_removed != chars_requested.
    The lazy validator hash equality is the source of truth, but the
    schema can already catch this structural violation at the boundary.
    """
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=5,
            chars_removed=3,  # mismatch on the success path is malformed
            replay_chars=0,
            failure_reason=FAILURE_SUCCESS,
        )


def test_response_ledger_underrun_allows_partial_chars_removed():
    """ledger_underrun is the non-success branch chars_removed < chars_requested rides."""
    r = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=5,
        chars_removed=2,
        replay_chars=0,
        failure_reason=FAILURE_LEDGER_UNDERRUN,
    )
    assert r.chars_removed == 2


def test_response_replay_failed_allows_nonzero_chars_removed():
    """replay_failed: retract succeeded, replay raised. chars_removed == chars_requested."""
    r = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=4,
        chars_removed=4,
        replay_chars=1,  # partial replay
        failure_reason=FAILURE_REPLAY_FAILED,
    )
    assert r.failure_reason == FAILURE_REPLAY_FAILED


def test_response_from_dict_rejects_non_mapping():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse.from_dict(42)  # type: ignore[arg-type]


def test_response_from_dict_rejects_wrong_action():
    payload = {
        "action": "not_retract_response",
        "request_id": _RID,
        "chars_requested": 1,
        "chars_removed": 1,
        "replay_chars": 0,
        "failure_reason": FAILURE_SUCCESS,
    }
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse.from_dict(payload)


def test_response_from_dict_rejects_missing_fields():
    for missing in (
        "request_id",
        "chars_requested",
        "chars_removed",
        "replay_chars",
        "failure_reason",
    ):
        payload = {
            "action": ACTION_NAME_RESPONSE,
            "request_id": _RID,
            "chars_requested": 1,
            "chars_removed": 1,
            "replay_chars": 0,
            "failure_reason": FAILURE_SUCCESS,
        }
        del payload[missing]
        with pytest.raises(RetractEditorTextSchemaError):
            RetractEditorTextResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# whole_utterance mode (wh-editor-retract-ledger-authoritative)
# ---------------------------------------------------------------------------
#
# MODE3 retraction is ledger-authoritative: the request carries
# whole_utterance=True and the GUI peels ALL ledger runs, so
# chars_requested becomes advisory (the speech-side mirror value, kept
# for diagnostics) and the success invariant chars_removed ==
# chars_requested holds only in counted mode.


def test_request_whole_utterance_defaults_false():
    req = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=5,
        utterance_id=_UID,
        replay_text="",
        editor_generation=0,
    )
    assert req.whole_utterance is False
    assert req.to_dict()["whole_utterance"] is False


def test_request_whole_utterance_round_trip():
    original = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=5,
        utterance_id=_UID,
        replay_text="corrected",
        editor_generation=2,
        whole_utterance=True,
    )
    restored = RetractEditorTextRequest.from_dict(original.to_dict())
    assert restored == original
    assert restored.whole_utterance is True


def test_request_from_dict_missing_whole_utterance_defaults_false():
    """Old-producer payloads without the field parse as counted mode
    (wh-uf54 graceful degradation)."""
    req = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=5,
        utterance_id=_UID,
        replay_text="",
        editor_generation=0,
    )
    payload = req.to_dict()
    del payload["whole_utterance"]
    restored = RetractEditorTextRequest.from_dict(payload)
    assert restored.whole_utterance is False


def test_request_whole_utterance_allows_zero_chars_requested():
    """The mirror can legitimately read 0 when every insert response
    timed out; whole-utterance mode must still be expressible."""
    req = RetractEditorTextRequest(
        request_id=_RID,
        chars_requested=0,
        utterance_id=_UID,
        replay_text="heal",
        editor_generation=0,
        whole_utterance=True,
    )
    assert req.chars_requested == 0


def test_request_counted_mode_still_rejects_zero_chars_requested():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=0,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
        )


def test_request_whole_utterance_rejects_negative_chars_requested():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=-1,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
            whole_utterance=True,
        )


def test_request_rejects_non_bool_whole_utterance():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextRequest(
            request_id=_RID,
            chars_requested=5,
            utterance_id=_UID,
            replay_text="",
            editor_generation=0,
            whole_utterance=1,  # type: ignore[arg-type]
        )


def test_response_whole_utterance_defaults_false():
    resp = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=5,
        chars_removed=5,
        replay_chars=0,
        failure_reason="",
    )
    assert resp.whole_utterance is False
    assert resp.to_dict()["whole_utterance"] is False


def test_response_whole_utterance_relaxes_success_invariant():
    """In whole-utterance mode the GUI removes the ledger's true total,
    which may exceed the advisory chars_requested (mirror drift is the
    whole point)."""
    resp = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=3,
        chars_removed=11,
        replay_chars=0,
        failure_reason="",
        whole_utterance=True,
    )
    assert resp.chars_removed == 11


def test_response_counted_mode_still_enforces_success_invariant():
    with pytest.raises(RetractEditorTextSchemaError):
        RetractEditorTextResponse(
            request_id=_RID,
            chars_requested=3,
            chars_removed=11,
            replay_chars=0,
            failure_reason="",
        )


def test_response_whole_utterance_round_trip():
    original = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=0,
        chars_removed=7,
        replay_chars=5,
        failure_reason="",
        whole_utterance=True,
    )
    restored = RetractEditorTextResponse.from_dict(original.to_dict())
    assert restored == original


def test_response_from_dict_missing_whole_utterance_defaults_false():
    resp = RetractEditorTextResponse(
        request_id=_RID,
        chars_requested=5,
        chars_removed=5,
        replay_chars=0,
        failure_reason="",
    )
    payload = resp.to_dict()
    del payload["whole_utterance"]
    restored = RetractEditorTextResponse.from_dict(payload)
    assert restored.whole_utterance is False
