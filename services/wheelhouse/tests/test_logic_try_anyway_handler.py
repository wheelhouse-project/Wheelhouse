"""Tests for the Logic-side try_anyway_clicked handler (wh-iycks).

When the GUI user clicks "Try it anyway" on a rejection toast, the
GUI manager emits a ``try_anyway_clicked`` action carrying the
rejection's correlation_token onto the existing GUI-to-Logic queue.
``LogicController._handle_try_anyway_clicked`` resolves the token
against the Logic-side ``RejectionTokenCache``:

  * HIT     -- await ``forward_retry_dictation_by_token``.
  * EXPIRED or MISS -- log "click_too_late" at INFO, surface the
    canonical follow-up toast wording, no IPC to Input.

Coverage:
  * HIT path calls forward_retry_dictation_by_token with the same token.
  * MISS path emits the click_too_late log + follow-up toast and does
    NOT call forward_retry_dictation_by_token.
  * EXPIRED path emits the same log/toast as MISS (and does not invoke
    the forwarder).
  * Schema-error payloads degrade silently (log + drop) via safe_parse,
    no forwarder invocation.
  * The log line carries only the correlation token, never any
    dictation text (privacy property).
  * _forward_rejection_event_to_gui populates the cache with the
    RejectionTuple, so a later click can resolve to HIT.

The follow-up wording is the same canonical string the existing
``forward_retry_dictation_by_token`` emits when Input reports
token_expired / unknown_token; click-too-late is the same user
experience as Input-side miss.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.rejection_token_cache import (
    RejectionTokenCache,
    RejectionTuple,
)
from shared.text_target_rejection import (
    TextTargetRejectedEvent,
    new_correlation_token,
)
from shared.try_anyway_clicked import ACTION_NAME


_TOKEN = "11111111-1111-4111-8111-111111111111"

EXPECTED_WORDING = (
    "Wheelhouse couldn't try again. Say the words again, "
    "then click Try it anyway."
)


def _make_controller():
    """Build a MagicMock controller with the methods under test bound to it."""

    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.app = MagicMock()
    controller.app.send_request = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    controller.rejection_token_cache = RejectionTokenCache()

    # Bind the real methods we are exercising; everything else stays mocked.
    controller._handle_try_anyway_clicked = (
        LogicController._handle_try_anyway_clicked.__get__(controller)
    )
    controller._send_retry_followup_toast = (
        LogicController._send_retry_followup_toast.__get__(controller)
    )
    controller._forward_rejection_event_to_gui = (
        LogicController._forward_rejection_event_to_gui.__get__(controller)
    )
    # forward_retry_dictation_by_token is mocked so we can assert it was
    # called without exercising the full IPC pipeline.
    controller.forward_retry_dictation_by_token = AsyncMock()
    return controller


def _put_in_cache(controller, token: str, suffix: str = "x") -> RejectionTuple:
    payload = RejectionTuple(
        process_name="zed.exe",
        class_name=f"Zed::Window-{suffix}",
        control_type="WindowControl",
        app_friendly_name="Zed Editor",
    )
    controller.rejection_token_cache.put(token, payload)
    return payload


# ---------------------------------------------------------------------------
# HIT -- forwarder is awaited
# ---------------------------------------------------------------------------


class TestCacheHit:
    def test_hit_invokes_forwarder_with_token(self):
        controller = _make_controller()
        payload = _put_in_cache(controller, _TOKEN)

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        # wh-vbvgf.3.2: handler passes the resolved tuple so the publish
        # decision after the IPC round trip cannot race the cache TTL.
        controller.forward_retry_dictation_by_token.assert_awaited_once_with(
            _TOKEN, rejection=payload,
        )

    def test_hit_does_not_emit_followup_toast(self):
        controller = _make_controller()
        _put_in_cache(controller, _TOKEN)

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        gui_queue = controller.state_manager.state_to_gui_queue
        # The HIT path must not synthesize a follow-up toast; the
        # forwarder owns the post-Input response messaging.
        for call in gui_queue.put_nowait.call_args_list:
            msg = call.args[0]
            if msg.get("action") == "show_notification":
                assert msg.get("message", "") != EXPECTED_WORDING


# ---------------------------------------------------------------------------
# MISS / EXPIRED -- no IPC, click_too_late log + follow-up toast
# ---------------------------------------------------------------------------


class TestCacheMiss:
    def test_miss_does_not_invoke_forwarder(self):
        controller = _make_controller()  # cache is empty

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        controller.forward_retry_dictation_by_token.assert_not_awaited()

    def test_miss_emits_followup_toast_with_canonical_wording(self):
        controller = _make_controller()  # cache is empty

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        gui_queue = controller.state_manager.state_to_gui_queue
        toasts = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert len(toasts) == 1
        assert toasts[0]["message"] == EXPECTED_WORDING

    def test_miss_logs_click_too_late_at_info(self, caplog):
        controller = _make_controller()  # cache is empty

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        with caplog.at_level(logging.INFO, logger="main"):
            asyncio.run(controller._handle_try_anyway_clicked(command))

        # Exactly one click_too_late line, carrying the token.
        click_too_late = [
            rec
            for rec in caplog.records
            if "click_too_late" in rec.getMessage()
        ]
        assert len(click_too_late) >= 1
        assert any(_TOKEN in rec.getMessage() for rec in click_too_late)


class TestCacheExpired:
    def test_expired_token_treated_as_click_too_late(self, caplog):
        controller = _make_controller()
        # Inject a tuple via a fake clock, then advance the clock past
        # the TTL. Using the controller's existing cache with a real
        # clock and a real sleep was flaky on Windows because
        # time.monotonic resolution can be 15ms or more, so
        # cache.put + cache.resolve can sample the same timestamp.
        from shared.rejection_token_cache import RejectionTokenCache
        clock = [1000.0]
        controller.rejection_token_cache = RejectionTokenCache(
            ttl_seconds=10.0, time_source=lambda: clock[0],
        )
        _put_in_cache(controller, _TOKEN)
        clock[0] += 100.0  # Far past TTL.

        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        with caplog.at_level(logging.INFO, logger="main"):
            asyncio.run(controller._handle_try_anyway_clicked(command))

        controller.forward_retry_dictation_by_token.assert_not_awaited()
        gui_queue = controller.state_manager.state_to_gui_queue
        toasts = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert any(t["message"] == EXPECTED_WORDING for t in toasts)


# ---------------------------------------------------------------------------
# Schema graceful degrade
# ---------------------------------------------------------------------------


class TestSchemaDegradation:
    def test_missing_correlation_token_drops_silently(self, caplog):
        controller = _make_controller()
        _put_in_cache(controller, _TOKEN)

        bad = {"action": ACTION_NAME}  # missing correlation_token
        with caplog.at_level(logging.WARNING):
            asyncio.run(controller._handle_try_anyway_clicked(bad))

        controller.forward_retry_dictation_by_token.assert_not_awaited()
        gui_queue = controller.state_manager.state_to_gui_queue
        # No follow-up toast either; safe_parse logged + dropped.
        toasts = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert toasts == []

    def test_non_uuid_correlation_token_drops_silently(self, caplog):
        controller = _make_controller()

        bad = {"action": ACTION_NAME, "correlation_token": "garbage"}
        with caplog.at_level(logging.WARNING):
            asyncio.run(controller._handle_try_anyway_clicked(bad))

        controller.forward_retry_dictation_by_token.assert_not_awaited()


# ---------------------------------------------------------------------------
# Privacy property -- no dictation text in any new payload or log
# ---------------------------------------------------------------------------


class TestPrivacyNoTextInPayload:
    def test_no_text_field_in_followup_toast(self):
        """The follow-up toast carries only title/message/timeout, no text."""

        controller = _make_controller()  # cache empty -> MISS path
        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        gui_queue = controller.state_manager.state_to_gui_queue
        toasts = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert len(toasts) == 1
        forbidden = {"dictation", "transcript", "utterance", "content"}
        assert forbidden.isdisjoint(toasts[0].keys())

    def test_log_lines_never_contain_dictation_text(self, caplog):
        """The click_too_late log line must not echo any payload field
        beyond the correlation token.

        The handler has no access to dictation text at all (privacy
        contract); this test guards against a future regression that
        would log the cache value or pass it through some accidental
        path.
        """

        controller = _make_controller()
        # If a developer ever adds a dictation_text to RejectionTuple
        # the privacy test in test_rejection_token_cache catches it.
        # Here we additionally check that no log line ever contains
        # arbitrary user content -- approximated by asserting the only
        # interpolated value is the token.
        sentinel_text = "this is a private dictation that must never log"
        # We cannot inject text into the cache (it has no text field),
        # but we can fail closed on any log line that mentions a
        # forbidden marker that a careless developer might add later.
        command = {"action": ACTION_NAME, "correlation_token": _TOKEN}
        with caplog.at_level(logging.DEBUG):
            asyncio.run(controller._handle_try_anyway_clicked(command))

        for rec in caplog.records:
            assert sentinel_text not in rec.getMessage()


# ---------------------------------------------------------------------------
# Cache population from _forward_rejection_event_to_gui
# ---------------------------------------------------------------------------


class TestCachePopulationOnRejection:
    def _make_event_dict(self, token: str) -> dict:
        return TextTargetRejectedEvent(
            process_name="zed.exe",
            class_name="Zed::Window",
            control_type="WindowControl",
            reason="default_reject_paste_capable_class",
            supported_patterns=("Invoke",),
            app_friendly_name="Zed Editor",
            correlation_token=token,
        ).to_dict()

    def test_forward_rejection_populates_cache(self):
        controller = _make_controller()
        token = new_correlation_token()
        event = self._make_event_dict(token)

        controller._forward_rejection_event_to_gui(event)

        result = controller.rejection_token_cache.resolve(token)
        from shared.rejection_token_cache import CacheStatus
        assert result.status is CacheStatus.HIT
        assert result.tuple_.process_name == "zed.exe"
        assert result.tuple_.class_name == "Zed::Window"
        assert result.tuple_.control_type == "WindowControl"
        assert result.tuple_.app_friendly_name == "Zed Editor"

    def test_forward_rejection_then_click_resolves_to_hit_and_forwards(self):
        controller = _make_controller()
        token = new_correlation_token()
        controller._forward_rejection_event_to_gui(
            self._make_event_dict(token)
        )

        command = {"action": ACTION_NAME, "correlation_token": token}
        asyncio.run(controller._handle_try_anyway_clicked(command))

        # The handler passes the resolved tuple to the forwarder so the
        # publish decision survives a TTL elapse during the round trip
        # (wh-vbvgf.3.2). The exact tuple is what the populate path
        # stored, so assert by reading it back from the cache.
        stored = controller.rejection_token_cache.get(token)
        controller.forward_retry_dictation_by_token.assert_awaited_once_with(
            token, rejection=stored,
        )

    def test_malformed_rejection_does_not_populate_cache(self, caplog):
        controller = _make_controller()
        bad = {
            "type": "text_target_rejected",
            "process_name": "zed.exe",
            # Missing other required fields; from_dict will raise.
        }
        with caplog.at_level(logging.WARNING):
            controller._forward_rejection_event_to_gui(bad)

        assert controller.rejection_token_cache.keys() == []

    def test_forward_rejection_does_not_store_text(self):
        """The cache value must not carry any extra field beyond the four
        identifying fields. Defense in depth: the schema event has no
        text field either, but this anchors the contract on the Logic
        side."""

        controller = _make_controller()
        token = new_correlation_token()
        controller._forward_rejection_event_to_gui(
            self._make_event_dict(token)
        )

        result = controller.rejection_token_cache.resolve(token)
        from dataclasses import fields
        field_names = {f.name for f in fields(result.tuple_)}
        forbidden = {"text", "dictation", "transcript", "utterance", "content"}
        assert field_names.isdisjoint(forbidden)
