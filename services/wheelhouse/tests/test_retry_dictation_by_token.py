"""Tests for the retry_dictation_by_token IPC contract (wh-wt82).

The schema defines the Logic -> Input request that fires when the user
clicks "Try it anyway" on a text-target rejection toast (wh-9weum
Phase 4). Logic looks up the correlation_token in its own token-tuple
cache, then asks Input to insert the dictation via
ClipboardOnlyStrategy. Input owns the token -> original_text cache;
Logic owns the token -> tuple cache. The request payload carries no
dictation text; only the token threads the round trip.

Coverage:
  * Request round-trips through to_action_payload / from_action_payload.
  * The action payload carries the canonical action name and a params
    dict, matching the Logic-to-Input action contract used by
    WheelHouseApp.send_request.
  * Request rejects unknown override_strategy values, missing fields,
    and wrong types.
  * Response round-trips through to_dict / from_dict for each of the
    three statuses (success, token_expired, unknown_token).
  * Response factory methods produce well-formed dataclasses.
  * Response.success requires a valid retry_outcome.
  * Response.token_expired / unknown_token reject a retry_outcome.
  * Schema rejects status values it does not know about.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.shared.retry_dictation_by_token import (
    ACTION_NAME,
    ALLOWED_OVERRIDE_STRATEGIES,
    ALLOWED_RETRY_OUTCOMES,
    ALLOWED_STATUSES,
    OVERRIDE_CLIPBOARD_ONLY,
    RETRY_OUTCOME_UNVERIFIED,
    RETRY_OUTCOME_VERIFIED,
    RetryDictationByTokenRequest,
    RetryDictationByTokenResponse,
    RetryDictationByTokenSchemaError,
    STATUS_SUCCESS,
    STATUS_TOKEN_EXPIRED,
    STATUS_UNKNOWN_TOKEN,
)


_TOKEN = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_action_name_is_canonical():
    assert ACTION_NAME == "retry_dictation_by_token"


def test_override_strategy_constants_match_bead():
    assert OVERRIDE_CLIPBOARD_ONLY == "clipboard_only"
    assert OVERRIDE_CLIPBOARD_ONLY in ALLOWED_OVERRIDE_STRATEGIES


def test_status_constants_match_bead():
    assert STATUS_SUCCESS == "success"
    assert STATUS_TOKEN_EXPIRED == "token_expired"
    assert STATUS_UNKNOWN_TOKEN == "unknown_token"
    assert ALLOWED_STATUSES == frozenset(
        {STATUS_SUCCESS, STATUS_TOKEN_EXPIRED, STATUS_UNKNOWN_TOKEN}
    )


def test_retry_outcome_constants_match_insertion_result():
    assert RETRY_OUTCOME_VERIFIED == "verified"
    assert RETRY_OUTCOME_UNVERIFIED == "unverified"
    assert ALLOWED_RETRY_OUTCOMES == frozenset(
        {RETRY_OUTCOME_VERIFIED, RETRY_OUTCOME_UNVERIFIED}
    )


# ---------------------------------------------------------------------------
# Request: round-trip
# ---------------------------------------------------------------------------


def test_request_to_action_payload_shape():
    req = RetryDictationByTokenRequest(
        correlation_token=_TOKEN,
        override_strategy=OVERRIDE_CLIPBOARD_ONLY,
    )
    payload = req.to_action_payload()
    assert payload["action"] == ACTION_NAME
    assert payload["params"]["correlation_token"] == _TOKEN
    assert payload["params"]["override_strategy"] == OVERRIDE_CLIPBOARD_ONLY


def test_request_round_trip_via_action_payload():
    original = RetryDictationByTokenRequest(
        correlation_token=_TOKEN,
        override_strategy=OVERRIDE_CLIPBOARD_ONLY,
    )
    restored = RetryDictationByTokenRequest.from_action_payload(
        original.to_action_payload()
    )
    assert restored == original


def test_request_is_immutable():
    req = RetryDictationByTokenRequest(
        correlation_token=_TOKEN,
        override_strategy=OVERRIDE_CLIPBOARD_ONLY,
    )
    with pytest.raises(Exception):
        req.correlation_token = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Request: validation
# ---------------------------------------------------------------------------


def test_request_rejects_wrong_action():
    payload = {
        "action": "shell_busy",
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_rejects_missing_action():
    payload = {
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_rejects_missing_params():
    payload = {"action": ACTION_NAME}
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_rejects_non_dict_params():
    payload = {"action": ACTION_NAME, "params": "not a dict"}
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


@pytest.mark.parametrize(
    "missing_field",
    ["correlation_token", "override_strategy"],
)
def test_request_rejects_missing_param(missing_field: str):
    payload = {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }
    del payload["params"][missing_field]
    with pytest.raises(RetryDictationByTokenSchemaError) as exc_info:
        RetryDictationByTokenRequest.from_action_payload(payload)
    assert missing_field in str(exc_info.value)


def test_request_rejects_non_string_correlation_token():
    payload = {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": 42,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_rejects_unknown_override_strategy():
    payload = {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": "send_input",
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError) as exc_info:
        RetryDictationByTokenRequest.from_action_payload(payload)
    assert "send_input" in str(exc_info.value)


def test_request_construction_rejects_unknown_override_strategy():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest(
            correlation_token=_TOKEN,
            override_strategy="send_input",
        )


def test_request_rejects_unhashable_override_strategy():
    """wh-9weum.1.2: a malformed unhashable override_strategy must
    raise the schema error, not TypeError. safe_parse only catches
    ValueError; TypeError would escape past the boundary."""

    payload = {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": ["clipboard_only"],  # unhashable list
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_construction_rejects_unhashable_override_strategy():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest(
            correlation_token=_TOKEN,
            override_strategy=["clipboard_only"],  # type: ignore[arg-type]
        )


def test_request_rejects_non_uuid_correlation_token():
    """wh-9weum.1.3: token field must be uuid4-shaped, not arbitrary
    string."""

    payload = {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": "not-a-uuid",
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest.from_action_payload(payload)


def test_request_construction_rejects_non_uuid_correlation_token():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenRequest(
            correlation_token="not-a-uuid",
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
        )


def test_request_payload_carries_no_text():
    """Privacy contract: dictation text never crosses processes in this request."""
    req = RetryDictationByTokenRequest(
        correlation_token=_TOKEN,
        override_strategy=OVERRIDE_CLIPBOARD_ONLY,
    )
    payload = req.to_action_payload()
    assert "text" not in payload["params"]
    assert "dictation" not in payload["params"]
    assert "original_text" not in payload["params"]
    # Structural assertion: only the two expected keys.
    assert set(payload["params"].keys()) == {
        "correlation_token",
        "override_strategy",
    }


# ---------------------------------------------------------------------------
# Response: factories
# ---------------------------------------------------------------------------


def test_response_success_factory_with_verified():
    resp = RetryDictationByTokenResponse.success(RETRY_OUTCOME_VERIFIED)
    assert resp.status == STATUS_SUCCESS
    assert resp.retry_outcome == RETRY_OUTCOME_VERIFIED
    assert resp.reason == ""


def test_response_success_factory_with_unverified():
    resp = RetryDictationByTokenResponse.success(RETRY_OUTCOME_UNVERIFIED)
    assert resp.status == STATUS_SUCCESS
    assert resp.retry_outcome == RETRY_OUTCOME_UNVERIFIED


def test_response_success_rejects_unknown_retry_outcome():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse.success("n/a")


def test_response_token_expired_factory():
    resp = RetryDictationByTokenResponse.token_expired(reason="ttl")
    assert resp.status == STATUS_TOKEN_EXPIRED
    assert resp.retry_outcome is None
    assert resp.reason == "ttl"


def test_response_unknown_token_factory():
    resp = RetryDictationByTokenResponse.unknown_token()
    assert resp.status == STATUS_UNKNOWN_TOKEN
    assert resp.retry_outcome is None
    assert resp.reason == ""


def test_response_token_expired_rejects_retry_outcome():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(
            status=STATUS_TOKEN_EXPIRED,
            retry_outcome=RETRY_OUTCOME_VERIFIED,
        )


def test_response_unknown_token_rejects_retry_outcome():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(
            status=STATUS_UNKNOWN_TOKEN,
            retry_outcome=RETRY_OUTCOME_UNVERIFIED,
        )


def test_response_success_requires_retry_outcome():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(status=STATUS_SUCCESS, retry_outcome=None)


def test_response_rejects_unknown_status():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(status="rejected", retry_outcome=None)


def test_response_rejects_unhashable_status():
    """wh-9weum.1.2: a malformed unhashable status must raise the
    schema error, not TypeError."""

    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(
            status=["success"],  # type: ignore[arg-type]
            retry_outcome=None,
        )


def test_response_rejects_unhashable_retry_outcome():
    """wh-9weum.1.2: same protection for retry_outcome."""

    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse(
            status=STATUS_SUCCESS,
            retry_outcome=["verified"],  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Response: round-trip
# ---------------------------------------------------------------------------


def test_response_success_round_trip():
    original = RetryDictationByTokenResponse.success(RETRY_OUTCOME_VERIFIED)
    payload = original.to_dict()
    assert payload["status"] == STATUS_SUCCESS
    assert payload["retry_outcome"] == RETRY_OUTCOME_VERIFIED
    restored = RetryDictationByTokenResponse.from_dict(payload)
    assert restored == original


def test_response_token_expired_round_trip():
    original = RetryDictationByTokenResponse.token_expired(reason="age")
    payload = original.to_dict()
    assert payload["status"] == STATUS_TOKEN_EXPIRED
    assert payload["retry_outcome"] is None
    assert payload["reason"] == "age"
    restored = RetryDictationByTokenResponse.from_dict(payload)
    assert restored == original


def test_response_unknown_token_round_trip():
    original = RetryDictationByTokenResponse.unknown_token()
    payload = original.to_dict()
    restored = RetryDictationByTokenResponse.from_dict(payload)
    assert restored == original


def test_response_from_dict_rejects_missing_status():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse.from_dict({"retry_outcome": None})


def test_response_from_dict_rejects_unknown_status():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse.from_dict(
            {"status": "rejected", "retry_outcome": None}
        )


def test_response_from_dict_rejects_non_dict():
    with pytest.raises(RetryDictationByTokenSchemaError):
        RetryDictationByTokenResponse.from_dict("not a dict")  # type: ignore[arg-type]


def test_response_is_immutable():
    resp = RetryDictationByTokenResponse.success(RETRY_OUTCOME_VERIFIED)
    with pytest.raises(Exception):
        resp.status = STATUS_TOKEN_EXPIRED  # type: ignore[misc]
