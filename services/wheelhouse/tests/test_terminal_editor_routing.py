"""Tests for terminal editor event routing in LogicController."""
import asyncio
import logging

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class TestTerminalEditorRouting:
    def test_te_show_forwarded_to_gui(self):
        """te_event:show from Input should forward as te_show to GUI."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        # Call the real method on the mock
        event_msg = {
            "type": "te_event", "event": "show",
            "text": "hello", "hwnd": 123, "rect": (0, 0, 800, 600),
        }
        LogicController._forward_te_event_to_gui(controller, event_msg)

        controller.state_manager.state_to_gui_queue.put_nowait.assert_called_once()
        gui_msg = controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        assert gui_msg["action"] == "te_show"
        assert gui_msg["text"] == "hello"
        assert gui_msg["hwnd"] == 123
        assert gui_msg["rect"] == (0, 0, 800, 600)

    def test_te_show_does_not_carry_focus_redirect_active_flag(self):
        """wh-1g6er: the focus_redirect_active flag has been removed.
        The flag has been replaced by an always-on path (the redirect
        path is now the only path). The te_show payload must NOT
        carry the legacy ``focus_redirect_active`` field.
        """
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        event_msg = {
            "type": "te_event", "event": "show",
            "text": "hi", "hwnd": 1, "rect": (0, 0, 100, 100),
        }
        LogicController._forward_te_event_to_gui(controller, event_msg)
        gui_msg = controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        assert "focus_redirect_active" not in gui_msg

    def test_te_submit_forwarded_to_gui(self):
        """te_event:submit from Input should forward as te_submit to GUI."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        event_msg = {"type": "te_event", "event": "submit"}
        LogicController._forward_te_event_to_gui(controller, event_msg)

        gui_msg = controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        assert gui_msg["action"] == "te_submit"

    def test_handle_input_event_dispatches_te_event(self):
        """_handle_input_event should dispatch te_event to _forward_te_event_to_gui."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller._handle_input_event = LogicController._handle_input_event.__get__(controller)
        controller._forward_te_event_to_gui = MagicMock()

        msg = {"type": "te_event", "event": "show", "text": "hi"}
        controller._handle_input_event(msg)
        controller._forward_te_event_to_gui.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_te_cancelled_sends_cancel_command(self):
        """te_cancelled with no rid still notifies Input (recovery shape)."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller._handle_te_cancelled = LogicController._handle_te_cancelled.__get__(controller)
        controller._te_control_send_retry_delays = ()
        controller._send_te_control_command = (
            LogicController._send_te_control_command.__get__(controller)
        )

        await controller._handle_te_cancelled()
        controller.app.send_request.assert_called_once_with(
            "terminal_editor_cancelled", {"request_id": ""},
        )

    @pytest.mark.asyncio
    async def test_te_cancelled_forwards_request_id(self):
        """wh-overlay-slow-uia-stale-badges.14.17: the cancellation
        carries the session's show request_id so the input-process
        proxy can ignore a cancellation from an older session."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller._handle_te_cancelled = LogicController._handle_te_cancelled.__get__(controller)
        controller._te_control_send_retry_delays = ()
        controller._send_te_control_command = (
            LogicController._send_te_control_command.__get__(controller)
        )

        await controller._handle_te_cancelled("rid-s")
        controller.app.send_request.assert_called_once_with(
            "terminal_editor_cancelled", {"request_id": "rid-s"},
        )


class TestTerminalEditorAckRouting:
    """wh-t81d9.2: te_event_ack round-trip from GUI to input proxy."""

    def test_te_show_forward_includes_request_id(self):
        """show events forwarded to GUI must carry the proxy-generated request_id."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.state_manager.state_to_gui_queue = MagicMock()

        event_msg = {
            "type": "te_event", "event": "show",
            "text": "hello", "hwnd": 123, "rect": (0, 0, 800, 600),
            "request_id": "abc123",
        }
        LogicController._forward_te_event_to_gui(controller, event_msg)
        gui_msg = controller.state_manager.state_to_gui_queue.put_nowait.call_args[0][0]
        assert gui_msg["request_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_te_event_ack_sends_control_command_to_input(self):
        """GUI ack reaches input proxy as a _te_event_ack control command.

        The control-command shape is what the input main loop dispatches
        directly to ``terminal_editor.on_event_ack`` without going through
        ``ui_handler``.
        """
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller._handle_te_event_ack = LogicController._handle_te_event_ack.__get__(controller)
        controller._te_control_send_retry_delays = ()
        controller._send_te_control_command = (
            LogicController._send_te_control_command.__get__(controller)
        )

        await controller._handle_te_event_ack("rid-1", "show", 99999)
        controller.app.send_request.assert_called_once_with(
            "_te_event_ack",
            {"request_id": "rid-1", "op": "show", "editor_hwnd": 99999},
        )

    @pytest.mark.asyncio
    async def test_te_event_ack_with_empty_request_id_is_dropped(self):
        """Empty request_id has nothing to ack; do not enqueue a noop command."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller._handle_te_event_ack = LogicController._handle_te_event_ack.__get__(controller)

        await controller._handle_te_event_ack("", "show", 12345)
        controller.app.send_request.assert_not_called()


class TestTeControlSendRetry:
    """wh-overlay-slow-uia-stale-badges.14.19 + .14.24: the two
    terminal-editor control handlers send through an ACKNOWLEDGED
    request with bounded retries. Queue acceptance is not delivery
    (.14.10), so send_command's True said nothing about whether the
    Input process ever received the cleanup; the handlers now use
    send_request and treat an error response, a TimeoutError, or an
    IpcDeliveryError as a failed attempt. An ERROR is logged when
    every attempt fails."""

    def _controller(self, send_results):
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller.app.send_request = AsyncMock(side_effect=send_results)
        controller._te_control_send_retry_delays = (0.0,)
        controller._send_te_control_command = (
            LogicController._send_te_control_command.__get__(controller)
        )
        controller._handle_te_cancelled = (
            LogicController._handle_te_cancelled.__get__(controller)
        )
        controller._handle_te_event_ack = (
            LogicController._handle_te_event_ack.__get__(controller)
        )
        return controller

    @pytest.mark.asyncio
    async def test_te_cancelled_acknowledged_on_first_attempt(self):
        controller = self._controller([{"status": "ok"}])
        await controller._handle_te_cancelled("rid-s")
        assert controller.app.send_request.await_count == 1
        call = controller.app.send_request.await_args_list[0]
        assert call.args == (
            "terminal_editor_cancelled", {"request_id": "rid-s"},
        )

    @pytest.mark.asyncio
    async def test_te_cancelled_retries_after_timeout(self):
        controller = self._controller([
            asyncio.TimeoutError(), {"status": "ok"},
        ])
        await controller._handle_te_cancelled("rid-s")
        assert controller.app.send_request.await_count == 2
        for call in controller.app.send_request.await_args_list:
            assert call.args == (
                "terminal_editor_cancelled", {"request_id": "rid-s"},
            )

    @pytest.mark.asyncio
    async def test_te_cancelled_retries_after_error_response(self):
        controller = self._controller([
            {"error": True, "message": "boom"}, {"status": "ok"},
        ])
        await controller._handle_te_cancelled("rid-s")
        assert controller.app.send_request.await_count == 2

    @pytest.mark.asyncio
    async def test_te_cancelled_logs_error_when_all_attempts_fail(self, caplog):
        from app import IpcDeliveryError
        controller = self._controller([
            IpcDeliveryError("queue full"), asyncio.TimeoutError(),
        ])
        with caplog.at_level(logging.ERROR):
            await controller._handle_te_cancelled("rid-s")
        assert controller.app.send_request.await_count == 2
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_te_event_ack_retries_after_timeout(self):
        controller = self._controller([
            asyncio.TimeoutError(), {"status": "ok"},
        ])
        await controller._handle_te_event_ack("rid-1", "show", 99999)
        assert controller.app.send_request.await_count == 2
        for call in controller.app.send_request.await_args_list:
            assert call.args == (
                "_te_event_ack",
                {"request_id": "rid-1", "op": "show", "editor_hwnd": 99999},
            )

    @pytest.mark.asyncio
    async def test_te_event_ack_logs_error_when_all_attempts_fail(self, caplog):
        controller = self._controller([
            asyncio.TimeoutError(), asyncio.TimeoutError(),
        ])
        with caplog.at_level(logging.ERROR):
            await controller._handle_te_event_ack("rid-1", "show", 99999)
        assert controller.app.send_request.await_count == 2
        assert any(r.levelno == logging.ERROR for r in caplog.records)


class TestInputProcTeControlHandlers:
    """wh-overlay-slow-uia-stale-badges.14.24: the input-process side of
    the acknowledged terminal-editor control protocol. Both handlers put
    the standard ok/error response when the IPC envelope carries a
    request_id, and stay silent for fire-and-forget callers -- the same
    contract as _handle_add_soft_allow_tuple (.14.14)."""

    def _q(self):
        from queue import Queue
        return Queue()

    def test_cancelled_handler_acks_success(self):
        from input_proc import _handle_terminal_editor_cancelled
        q = self._q()
        ui = MagicMock()
        _handle_terminal_editor_cancelled(
            {"request_id": "rid-s"}, "env-1", q, ui,
        )
        ui.terminal_editor_cancelled.assert_called_once_with("rid-s")
        resp = q.get_nowait()
        assert resp["request_id"] == "env-1"
        assert resp["status"] == "ok"
        assert resp["action"] == "terminal_editor_cancelled"
        assert q.empty()

    def test_cancelled_handler_fire_and_forget_no_response(self):
        from input_proc import _handle_terminal_editor_cancelled
        q = self._q()
        ui = MagicMock()
        _handle_terminal_editor_cancelled({"request_id": "rid-s"}, None, q, ui)
        ui.terminal_editor_cancelled.assert_called_once_with("rid-s")
        assert q.empty()

    def test_cancelled_handler_error_response_on_exception(self):
        from input_proc import _handle_terminal_editor_cancelled
        q = self._q()
        ui = MagicMock()
        ui.terminal_editor_cancelled.side_effect = RuntimeError("boom")
        _handle_terminal_editor_cancelled(
            {"request_id": "rid-s"}, "env-2", q, ui,
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "env-2"
        assert resp.get("error") is True
        assert resp["action"] == "terminal_editor_cancelled"
        assert q.empty()

    def test_te_event_ack_handler_acks_success(self):
        from input_proc import _handle_te_event_ack_command
        q = self._q()
        ui = MagicMock()
        _handle_te_event_ack_command(
            {"request_id": "rid-1", "op": "show", "editor_hwnd": 555},
            "env-3", q, ui,
        )
        ui.terminal_editor.on_event_ack.assert_called_once_with(
            "rid-1", "show", 555,
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "env-3"
        assert resp["status"] == "ok"
        assert resp["action"] == "_te_event_ack"
        assert q.empty()

    def test_te_event_ack_handler_zero_hwnd_becomes_none(self):
        from input_proc import _handle_te_event_ack_command
        q = self._q()
        ui = MagicMock()
        _handle_te_event_ack_command(
            {"request_id": "rid-1", "op": "submit_complete", "editor_hwnd": 0},
            None, q, ui,
        )
        ui.terminal_editor.on_event_ack.assert_called_once_with(
            "rid-1", "submit_complete", None,
        )
        assert q.empty()

    def test_te_event_ack_handler_error_response_on_exception(self):
        from input_proc import _handle_te_event_ack_command
        q = self._q()
        ui = MagicMock()
        ui.terminal_editor.on_event_ack.side_effect = RuntimeError("boom")
        _handle_te_event_ack_command(
            {"request_id": "rid-1", "op": "show", "editor_hwnd": 5},
            "env-4", q, ui,
        )
        resp = q.get_nowait()
        assert resp["request_id"] == "env-4"
        assert resp.get("error") is True
        assert resp["action"] == "_te_event_ack"
        assert q.empty()


class TestCancelIdentityChain:
    """wh-overlay-slow-uia-stale-badges.14.17: the session request_id
    travels the whole cancellation chain -- GUI window signal ->
    GuiManager forward -> Logic -> Input dispatch -> proxy."""

    def test_gui_manager_forwards_cancel_request_id(self):
        from gui import GuiManager
        manager = MagicMock(spec=GuiManager)
        manager.commands_to_logic_queue = MagicMock()

        GuiManager._on_te_cancelled(manager, "rid-s")
        manager.commands_to_logic_queue.put_nowait.assert_called_once_with(
            {"action": "te_cancelled", "request_id": "rid-s"},
        )

    def test_ui_action_handler_routes_rid_to_gated_cleanup(self):
        from ui.ui_action_handler import UIActionHandler
        handler = MagicMock(spec=UIActionHandler)
        handler.terminal_editor = MagicMock()

        UIActionHandler.terminal_editor_cancelled(handler, "rid-s")
        handler.terminal_editor.cancelled_by_gui.assert_called_once_with(
            "rid-s",
        )
        handler.terminal_editor.force_cleanup.assert_not_called()
