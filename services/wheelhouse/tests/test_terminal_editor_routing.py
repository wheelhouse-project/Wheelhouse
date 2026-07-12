"""Tests for terminal editor event routing in LogicController."""
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
        """te_cancelled from GUI should send terminal_editor_cancelled to Input."""
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.app = AsyncMock()
        controller._handle_te_cancelled = LogicController._handle_te_cancelled.__get__(controller)

        await controller._handle_te_cancelled()
        controller.app.send_command.assert_called_once_with("terminal_editor_cancelled")


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

        await controller._handle_te_event_ack("rid-1", "show", 99999)
        controller.app.send_command.assert_called_once_with(
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
        controller.app.send_command.assert_not_called()
