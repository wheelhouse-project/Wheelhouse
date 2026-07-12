"""Coverage gap tests for speech/command_engine.py.

Targets uncovered lines: 93-94, 120, 126-127, 132-133, 152, 161-168, 173-175
"""
import sys
import re
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.send_command = AsyncMock()
    app.send_request = AsyncMock(return_value={"status": "success"})
    return app


@pytest.fixture
def parser(catalog, mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return TextParser(handler, catalog)


# ============================================================================
# NUMERIC VALIDATION REJECTION (lines 93-94)
# ============================================================================

class TestNumericValidation:
    @pytest.mark.asyncio
    async def test_validation_failure_rejects_rule(self, parser):
        """Lines 93-94: Invalid numeric group causes rejection."""
        match = re.fullmatch(r"delete (\w+)", "delete banana")
        steps = [{"function": "press", "params": ["delete", "g1"]}]

        result = await parser._execute_rule(match, steps, validation_group="g1")
        assert result is False  # Rejected because "banana" is not a valid number


# ============================================================================
# PARAMETER RESOLUTION - STRING REPLACEMENT (line 120)
# ============================================================================

class TestParameterResolution:
    @pytest.mark.asyncio
    async def test_embedded_group_replacement(self, parser):
        """Line 120: String replacement for embedded markers like '(g1)'."""
        match = re.fullmatch(r"wrap (\w+)", "wrap hello")
        # Action with embedded g1 in a larger string
        steps = [{"function": "type_text", "params": ["prefix_g1_suffix"]}]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True

        # Verify the function was called with resolved text
        # type_text returns a dict, which gets sent to app
        calls = parser.speech_handler.app.send_command.call_args_list
        assert len(calls) > 0
        payload = calls[0][0][0]  # First call, first arg
        assert "prefix_hello_suffix" in payload.get("params", {}).get("text", "")


# ============================================================================
# MISSING FUNCTION HANDLER (lines 126-127)
# ============================================================================

class TestMissingFunction:
    @pytest.mark.asyncio
    async def test_unknown_function_skipped(self, parser):
        """Lines 126-127: Missing action function logs error and continues."""
        match = re.fullmatch(r"test", "test")
        steps = [
            {"function": "nonexistent_function", "params": []},
            {"function": "type_text", "params": ["hello"]},
        ]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True  # Should succeed (skips bad function, runs next)

        # The type_text action should still have been sent
        assert parser.speech_handler.app.send_command.call_count >= 1


# ============================================================================
# ASYNC FUNCTION EXECUTION (lines 132-133)
# ============================================================================

class TestAsyncFunctionExecution:
    @pytest.mark.asyncio
    async def test_async_function_awaited(self, parser):
        """Lines 132-133: Async functions are awaited properly."""
        match = re.fullmatch(r"test", "test")
        # "sleep" is an async function in ActionFunctions
        steps = [{"function": "sleep", "params": ["0.001"]}]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True

        # No UI commands should have been sent (sleep is a local action)
        assert parser.speech_handler.app.send_command.call_count == 0


# ============================================================================
# SEND_REQUEST FOR AWAITS_DONE (line 152)
# ============================================================================

class TestAwaitsDone:
    @pytest.mark.asyncio
    async def test_awaits_done_uses_send_request(self, parser):
        """Line 152: When awaits_done=True, use send_request instead of send_command."""
        match = re.fullmatch(r"test", "test")
        steps = [{"function": "type_text", "params": ["hello"], "awaits_done": True}]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True

        # Should have used send_request, not send_command
        assert parser.speech_handler.app.send_request.call_count == 1
        assert parser.speech_handler.app.send_command.call_count == 0


# ============================================================================
# CONTEXT STORAGE FOR STRING RETURNS (lines 161-168)
# ============================================================================

class TestContextStorage:
    @pytest.mark.asyncio
    async def test_string_return_stored_in_context(self, parser):
        """Lines 161-168: String returns are stored in context for chaining."""
        match = re.fullmatch(r"test", "test")
        # "date" returns a string (formatted date)
        # If followed by another action that uses "date" key, it should resolve
        steps = [
            {"function": "date", "params": ["%Y"]},
            {"function": "type_text", "params": ["date"]},
        ]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is True

        # The second step should have received the date string (via context substitution)
        # since "date" as a param resolves to the stored return value
        calls = parser.speech_handler.app.send_command.call_args_list
        assert len(calls) == 1
        payload = calls[0][0][0]
        text = payload.get("params", {}).get("text", "")
        # Should be the year, not the literal string "date"
        assert text.isdigit() and len(text) == 4


# ============================================================================
# EXCEPTION HANDLING (lines 173-175)
# ============================================================================

class TestExceptionHandling:
    @pytest.mark.asyncio
    async def test_rule_execution_error_returns_false(self, parser):
        """Lines 173-175: Exception during rule execution returns False."""
        match = re.fullmatch(r"test", "test")
        # Register a function that raises
        parser.action_functions._functions["boom"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        steps = [{"function": "boom", "params": []}]

        result = await parser._execute_rule(match, steps, validation_group=None)
        assert result is False


# ============================================================================
# SEARCH PATTERN: ctrl+c -> capture_clipboard -> gs(clipboard_text)  (wh-5ew)
# ============================================================================


class TestSearchPattern:
    """The 'search' command copies the selection and Google-searches it.

    Pattern (services/wheelhouse/speech/config/patterns.toml):
        pattern = '^search$'
        actions = [
            { function = "hk", params = ["ctrl", "c"], awaits_done = true },
            { function = "capture_clipboard", params = [], awaits_done = true },
            { function = "gs", params = ["capture_clipboard"] }
        ]

    The 'capture_clipboard' token in the gs params must resolve to the actual
    clipboard content (via context substitution at command_engine.py:110-121),
    not pass through as the literal string "capture_clipboard".
    """

    @pytest.mark.asyncio
    async def test_gs_receives_clipboard_content_not_literal(self, parser):
        match = re.fullmatch(r"^search$", "search")
        steps = [
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
            {"function": "capture_clipboard", "params": [], "awaits_done": True},
            {"function": "gs", "params": ["capture_clipboard"]},
        ]

        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.return_value = "selected query text"
        with patch.dict("sys.modules", {"pyperclip": mock_pyperclip}), \
             patch("speech.actions.webbrowser.open") as mock_open:
            result = await parser._execute_rule(match, steps, validation_group=None)

        assert result is True
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        assert "selected+query+text" in url or "selected%20query%20text" in url
        assert "capture_clipboard" not in url


# ============================================================================
# INTEGRATION: parse_and_execute with return_remainder
# ============================================================================

class TestParseAndExecute:
    @pytest.mark.asyncio
    async def test_no_match_returns_false(self, parser):
        result = await parser.parse_and_execute("xyzzy zorp blam")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_match_with_remainder(self, parser):
        matched, remainder = await parser.parse_and_execute("xyzzy zorp blam", return_remainder=True)
        assert matched is False
        assert remainder == "xyzzy zorp blam"

    @pytest.mark.asyncio
    async def test_match_returns_true(self, parser):
        result = await parser.parse_and_execute("delete")
        assert result is True

    @pytest.mark.asyncio
    async def test_match_with_remainder(self, parser):
        matched, remainder = await parser.parse_and_execute("delete", return_remainder=True)
        assert matched is True


# ============================================================================
# wh-med0: last_executed_pattern_type carries the matched pattern's type
# so SpeechProcessor can distinguish a true command (blocks retract) from
# a replacement (retractable text substitution).
# ============================================================================

class TestLastExecutedPatternType:
    @pytest.mark.asyncio
    async def test_initial_value_is_none(self, parser):
        assert parser.last_executed_pattern_type is None

    @pytest.mark.asyncio
    async def test_replacement_match_sets_replacement_type(self, parser):
        """The 'period' pattern in patterns.toml is a replacement that
        inserts '.'. The parser must record this so SpeechProcessor does
        NOT treat it as a command."""
        result = await parser.parse_and_execute("period")
        assert result is True
        assert parser.last_executed_pattern_type == "replacement"

    @pytest.mark.asyncio
    async def test_command_match_sets_command_type(self, parser):
        """A real command must record type='command'."""
        result = await parser.parse_and_execute("delete")
        assert result is True
        assert parser.last_executed_pattern_type == "command"

    @pytest.mark.asyncio
    async def test_no_match_resets_to_none(self, parser):
        """A no-match call must clear any stale value left from a prior
        successful match."""
        # Prime with a successful match.
        await parser.parse_and_execute("delete")
        assert parser.last_executed_pattern_type == "command"

        # No-match call clears the slot.
        await parser.parse_and_execute("xyzzy zorp blam")
        assert parser.last_executed_pattern_type is None
