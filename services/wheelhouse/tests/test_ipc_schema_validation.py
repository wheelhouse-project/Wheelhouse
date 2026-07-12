"""Tests for the IPC schema validation helper (wh-uf54).

The helper wraps a from_dict-style parser so consumers at process
boundaries log a WARNING and drop the message when a payload is
malformed, instead of letting the schema-error exception unwind into
the message loop. The wh-9weum (text-target gate relaxation) Phase 2
and Phase 4 consumers (wh-xxko1, wh-iycks, wh-ftg63) all use this
pattern.

Coverage targets (from wh-uf54):
  * missing required field -> drop with warning.
  * wrong field type -> drop with warning.
  * empty payload -> drop with warning.
  * valid payload -> parsed dataclass returned, no warning.
  * extra unknown fields -> tolerated (the existing schemas already
    ignore unknown keys).
  * the warning log carries the supplied log_label so a real
    consumer can distinguish which boundary produced the warning.
"""

from __future__ import annotations

import logging

import pytest

from services.wheelhouse.shared.ipc_schema_validation import safe_parse
from services.wheelhouse.shared.text_target_rejection import (
    MSG_TYPE,
    TextTargetRejectedEvent,
)
from services.wheelhouse.shared.retry_dictation_by_token import (
    ACTION_NAME,
    OVERRIDE_CLIPBOARD_ONLY,
    RetryDictationByTokenRequest,
)


_TOKEN = "11111111-1111-4111-8111-111111111111"


def _good_text_target_payload() -> dict:
    return {
        "type": MSG_TYPE,
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
        "reason": "no_text_pattern",
        "supported_patterns": ("Invoke",),
        "app_friendly_name": "Zed",
        "correlation_token": _TOKEN,
    }


def _good_retry_payload() -> dict:
    return {
        "action": ACTION_NAME,
        "params": {
            "correlation_token": _TOKEN,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        },
    }


# ---------------------------------------------------------------------------
# Valid payloads
# ---------------------------------------------------------------------------


def test_safe_parse_returns_dataclass_on_valid_text_target(caplog):
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        _good_text_target_payload(),
        log_label="text_target_rejected",
    )
    assert isinstance(result, TextTargetRejectedEvent)
    assert result.correlation_token == _TOKEN
    assert caplog.records == []


def test_safe_parse_returns_dataclass_on_valid_retry(caplog):
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        RetryDictationByTokenRequest.from_action_payload,
        _good_retry_payload(),
        log_label="retry_dictation_by_token",
    )
    assert isinstance(result, RetryDictationByTokenRequest)
    assert result.correlation_token == _TOKEN
    assert caplog.records == []


# ---------------------------------------------------------------------------
# Malformed payloads -- drop + warning
# ---------------------------------------------------------------------------


def test_safe_parse_drops_missing_required_field(caplog):
    caplog.set_level(logging.WARNING)
    bad = _good_text_target_payload()
    del bad["correlation_token"]
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        bad,
        log_label="text_target_rejected",
    )
    assert result is None
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "text_target_rejected" in record.getMessage()
    assert "correlation_token" in record.getMessage()


def test_safe_parse_drops_wrong_field_type(caplog):
    caplog.set_level(logging.WARNING)
    bad = _good_text_target_payload()
    bad["process_name"] = 42
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        bad,
        log_label="text_target_rejected",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_safe_parse_drops_empty_payload(caplog):
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        {},
        log_label="text_target_rejected",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_safe_parse_drops_non_dict_payload(caplog):
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        "not a dict",
        log_label="text_target_rejected",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_safe_parse_drops_malformed_retry_request(caplog):
    caplog.set_level(logging.WARNING)
    bad = _good_retry_payload()
    bad["params"]["override_strategy"] = "send_input"
    result = safe_parse(
        RetryDictationByTokenRequest.from_action_payload,
        bad,
        log_label="retry_dictation_by_token",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert "retry_dictation_by_token" in caplog.records[0].getMessage()


# ---------------------------------------------------------------------------
# Tolerance: extra unknown fields are ignored
# ---------------------------------------------------------------------------


def test_safe_parse_ignores_extra_unknown_fields_in_text_target(caplog):
    caplog.set_level(logging.WARNING)
    payload = _good_text_target_payload()
    payload["future_field"] = "added in some later version"
    payload["another_extra"] = 123
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        payload,
        log_label="text_target_rejected",
    )
    assert isinstance(result, TextTargetRejectedEvent)
    assert caplog.records == []


def test_safe_parse_ignores_extra_unknown_params_in_retry(caplog):
    caplog.set_level(logging.WARNING)
    payload = _good_retry_payload()
    payload["params"]["future_field"] = "ignored"
    payload["unknown_top_level"] = "ignored"
    result = safe_parse(
        RetryDictationByTokenRequest.from_action_payload,
        payload,
        log_label="retry_dictation_by_token",
    )
    assert isinstance(result, RetryDictationByTokenRequest)
    assert caplog.records == []


# ---------------------------------------------------------------------------
# Warning content
# ---------------------------------------------------------------------------


def test_safe_parse_warning_includes_log_label(caplog):
    caplog.set_level(logging.WARNING)
    safe_parse(
        TextTargetRejectedEvent.from_dict,
        {},
        log_label="my_unique_label_42",
    )
    assert any(
        "my_unique_label_42" in record.getMessage() for record in caplog.records
    )


def test_safe_parse_warning_includes_error_detail(caplog):
    caplog.set_level(logging.WARNING)
    bad = _good_text_target_payload()
    del bad["process_name"]
    safe_parse(
        TextTargetRejectedEvent.from_dict,
        bad,
        log_label="text_target_rejected",
    )
    assert any(
        "process_name" in record.getMessage() for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Non-schema exceptions still propagate
# ---------------------------------------------------------------------------


def test_safe_parse_propagates_non_value_error_exceptions(caplog):
    """If the parser raises something other than ValueError, that's a bug
    in the parser itself, not malformed input. The helper should not
    swallow it."""

    def broken_parser(payload):
        del payload
        raise RuntimeError("parser bug, not a schema error")

    caplog.set_level(logging.WARNING)
    with pytest.raises(RuntimeError):
        safe_parse(broken_parser, {}, log_label="any_label")


# ---------------------------------------------------------------------------
# Regression: schema parsers must not raise non-ValueError on malformed
# input (wh-9weum.1.1, wh-9weum.1.2)
# ---------------------------------------------------------------------------


def test_safe_parse_drops_broken_iterable_supported_patterns(caplog):
    """wh-9weum.1.1: a broken iterable in supported_patterns must be
    caught by the schema and reported as a schema error. safe_parse
    then drops with a warning instead of letting TypeError /
    RuntimeError escape past the boundary."""

    class BrokenIterable:
        def __iter__(self):
            raise RuntimeError("malicious iterable")

    bad = _good_text_target_payload()
    bad["supported_patterns"] = BrokenIterable()
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        TextTargetRejectedEvent.from_dict,
        bad,
        log_label="text_target_rejected",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_safe_parse_drops_unhashable_override_strategy(caplog):
    """wh-9weum.1.2: an unhashable override_strategy must be caught by
    the schema and reported as a schema error, not TypeError."""

    bad = _good_retry_payload()
    bad["params"]["override_strategy"] = ["clipboard_only"]  # unhashable
    caplog.set_level(logging.WARNING)
    result = safe_parse(
        RetryDictationByTokenRequest.from_action_payload,
        bad,
        log_label="retry_dictation_by_token",
    )
    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
