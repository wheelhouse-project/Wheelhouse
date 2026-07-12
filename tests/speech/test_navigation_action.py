"""Tests for cursor_navigate action function wiring."""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from services.wheelhouse.speech.actions import ActionFunctions


@pytest.fixture
def action_functions():
    """ActionFunctions with mocked speech_handler."""
    handler = Mock()
    handler.app = Mock()
    handler.app.send_request = AsyncMock(return_value={"status": "success"})
    return ActionFunctions(handler)


class TestCursorNavigateRegistration:
    def test_cursor_navigate_registered(self, action_functions):
        """cursor_navigate should be in the function registry."""
        functions = action_functions.get_functions()
        assert "cursor_navigate" in functions


class TestCursorNavigateExecution:
    @pytest.mark.asyncio
    async def test_single_go_command_sends_hotkey(self, action_functions):
        """'go right' sends a single hotkey action via IPC."""
        result = await action_functions.cursor_navigate("go right")
        action_functions.speech_handler.app.send_request.assert_called_once_with(
            "hotkey_action", {"keys": ["right"], "repeat": 1}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_chained_commands_send_multiple_hotkeys(self, action_functions):
        """'go home then grab to end' sends two sequential hotkey actions."""
        result = await action_functions.cursor_navigate("go home then grab to end")
        assert action_functions.speech_handler.app.send_request.call_count == 2
        calls = action_functions.speech_handler.app.send_request.call_args_list
        assert calls[0].args == ("hotkey_action", {"keys": ["home"], "repeat": 1})
        assert calls[1].args == ("hotkey_action", {"keys": ["shift", "end"], "repeat": 1})
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_input_sends_insert_text(self, action_functions):
        """'go banana' falls through to dictation via send_command."""
        action_functions.speech_handler.app.send_command = AsyncMock()
        result = await action_functions.cursor_navigate("go banana")
        assert result is None
        action_functions.speech_handler.app.send_command.assert_called_once()
        payload = action_functions.speech_handler.app.send_command.call_args[0][0]
        assert payload["action"] == "intelligent_insert_text"
        assert "go banana" in str(payload["params"])
