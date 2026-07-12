"""Tests for the Logic-side retry_dictation_by_token forwarder (wh-ftg63).

When the user clicks "Try it anyway", the GUI emits a
``try_anyway_clicked`` event with the correlation_token. Logic forwards
a ``retry_dictation_by_token`` request to the Input process. On the
non-success ``token_expired`` and ``unknown_token`` responses the Logic
process surfaces a one-line follow-up toast:

    "WheelHouse couldn't try again. Say the words again, then click
    Try it anyway."

The follow-up toast rides the existing ``show_notification`` GUI
action (used elsewhere in main.py for short user-facing notes); we do
NOT introduce a new IPC event for it in this bead.

On a ``success`` response, Logic does NOT emit a follow-up toast; the
verified-retry counter increment is wh-mv5ih territory and out of scope
here.

The wording string is the contract per the bead spec; a regression in
the wording will be caught by the wording-text assertion. The GUI
side that renders the show_notification is already wired in gui.py
(see action == "show_notification").

Privacy contract: the dictation text never appears in any IPC payload
this forwarder produces. Logic never sees the text in the first place
(Input owns the text cache). The forwarder only knows the
correlation_token and the response status.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.wheelhouse.shared.retry_dictation_by_token import (
    OVERRIDE_CLIPBOARD_ONLY,
    RETRY_OUTCOME_VERIFIED,
    RetryDictationByTokenResponse,
    STATUS_SUCCESS,
    STATUS_TOKEN_EXPIRED,
    STATUS_UNKNOWN_TOKEN,
)


_TOKEN = "11111111-1111-4111-8111-111111111111"

# Canonical follow-up wording per the bead spec.
EXPECTED_WORDING = (
    "WheelHouse couldn't try again. Say the words again, "
    "then click Try it anyway."
)


def _make_controller():
    from main import LogicController
    from event_bus import EventBus
    from shared.rejection_token_cache import RejectionTokenCache

    controller = MagicMock(spec=LogicController)
    controller.app = MagicMock()
    controller.app.send_request = AsyncMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = MagicMock()
    # wh-mv5ih: forward_retry_dictation_by_token reads
    # self.rejection_token_cache and self.event_bus on the success
    # branch. Fresh empty cache + real EventBus are inert here -- the
    # tests in this file cover the IPC-shape and follow-up-toast
    # behaviour, not the verified-retry signal (which has its own
    # test module test_logic_retry_verified_signal.py).
    controller.rejection_token_cache = RejectionTokenCache()
    controller.event_bus = EventBus()
    # wh-82lnx / wh-82lnx.2.2: token-dedup state required by
    # forward_retry_dictation_by_token. Both are inert for the IPC-
    # shape and follow-up-toast tests in this file; the dedup
    # behaviour itself is covered in test_logic_retry_verified_signal.py.
    from shared.consumed_token_set import ConsumedTokenSet
    controller.consumed_retry_tokens = ConsumedTokenSet()
    controller._in_flight_retry_tokens = set()
    # Bind the helper so the forwarder's call to self._send_retry_followup_toast
    # exercises the real implementation (which puts on the gui queue).
    controller._send_retry_followup_toast = (
        LogicController._send_retry_followup_toast.__get__(controller)
    )
    return controller


# ---------------------------------------------------------------------------
# Success path: no follow-up toast emitted by this forwarder
# ---------------------------------------------------------------------------


class TestSuccessResponse:
    def test_success_response_does_not_emit_follow_up_toast(self):
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        # Bind the unbound method.
        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        asyncio.run(forward(correlation_token=_TOKEN))

        # show_notification was NOT pushed onto the GUI queue.
        gui_queue = controller.state_manager.state_to_gui_queue
        for call in gui_queue.put_nowait.call_args_list:
            msg = call.args[0]
            if msg.get("action") == "show_notification":
                # Must not be the retry follow-up wording.
                assert msg.get("message", "") != EXPECTED_WORDING


# ---------------------------------------------------------------------------
# token_expired forwarding
# ---------------------------------------------------------------------------


class TestTokenExpiredForwarding:
    def test_token_expired_emits_follow_up_toast(self):
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.return_value = {
            "status": STATUS_TOKEN_EXPIRED,
            "retry_outcome": None,
            "reason": "",
        }

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        asyncio.run(forward(correlation_token=_TOKEN))

        gui_queue = controller.state_manager.state_to_gui_queue
        # At least one show_notification put with the canonical wording.
        toast_msgs = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert len(toast_msgs) == 1
        assert toast_msgs[0].get("message") == EXPECTED_WORDING

    def test_unknown_token_emits_follow_up_toast(self):
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.return_value = {
            "status": STATUS_UNKNOWN_TOKEN,
            "retry_outcome": None,
            "reason": "",
        }

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        asyncio.run(forward(correlation_token=_TOKEN))

        gui_queue = controller.state_manager.state_to_gui_queue
        toast_msgs = [
            call.args[0]
            for call in gui_queue.put_nowait.call_args_list
            if call.args[0].get("action") == "show_notification"
        ]
        assert len(toast_msgs) == 1
        assert toast_msgs[0].get("message") == EXPECTED_WORDING


# ---------------------------------------------------------------------------
# IPC payload shape
# ---------------------------------------------------------------------------


class TestRequestPayloadShape:
    def test_send_request_uses_canonical_action_and_params(self):
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.return_value = {
            "status": STATUS_SUCCESS,
            "retry_outcome": RETRY_OUTCOME_VERIFIED,
            "reason": "",
        }

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        asyncio.run(forward(correlation_token=_TOKEN))

        controller.app.send_request.assert_awaited_once()
        call_args = controller.app.send_request.await_args
        # First positional arg or kwarg "action".
        action = call_args.args[0] if call_args.args else call_args.kwargs.get("action")
        assert action == "retry_dictation_by_token"
        # The params must carry the correlation_token and the
        # clipboard_only override.
        params = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("params") or {}
        )
        assert params.get("correlation_token") == _TOKEN
        assert params.get("override_strategy") == OVERRIDE_CLIPBOARD_ONLY


# ---------------------------------------------------------------------------
# Privacy property
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_no_text_field_in_request_payload(self):
        # The request payload is constructed with only correlation_token
        # and override_strategy; no dictation-text field is allowed.
        # The schema enforces this; this test guards the call site.
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.return_value = {
            "status": STATUS_TOKEN_EXPIRED,
            "retry_outcome": None,
            "reason": "",
        }

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        asyncio.run(forward(correlation_token=_TOKEN))

        call_args = controller.app.send_request.await_args
        params = (
            call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs.get("params") or {}
        )
        # Defense in depth: the only allowed fields are the two in the
        # schema. A future bug that smuggled the text field in would
        # surface here.
        assert set(params.keys()) <= {
            "correlation_token", "override_strategy",
        }


# ---------------------------------------------------------------------------
# Send-request failure (timeout, IPC error) does not crash the forwarder
# ---------------------------------------------------------------------------


class TestSendRequestFailure:
    def test_timeout_logs_and_returns(self, caplog):
        # The call site must catch send_request failures so the click
        # handler does not bubble an exception out to the GUI command
        # listener (which would log a generic error and orphan the
        # correlation_token).
        from main import LogicController

        controller = _make_controller()
        controller.app.send_request.side_effect = asyncio.TimeoutError()

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        with caplog.at_level(logging.WARNING):
            # Must not raise.
            asyncio.run(forward(correlation_token=_TOKEN))

    def test_malformed_response_logs_and_returns(self, caplog):
        from main import LogicController

        controller = _make_controller()
        # Status field missing.
        controller.app.send_request.return_value = {"retry_outcome": None}

        forward = LogicController.forward_retry_dictation_by_token.__get__(
            controller
        )

        with caplog.at_level(logging.WARNING):
            asyncio.run(forward(correlation_token=_TOKEN))
