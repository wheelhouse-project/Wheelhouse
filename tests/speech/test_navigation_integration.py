"""Integration tests for cursor navigation: utterance -> pattern -> parse -> execute -> IPC."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from services.wheelhouse.speech.command_engine import TextParser
from services.wheelhouse.speech.pattern_catalog import PatternCatalog


@pytest.fixture
def mock_handler():
    handler = Mock()
    handler.app = Mock()
    handler.app.send_request = AsyncMock(return_value={"status": "success"})
    handler.app.send_command = AsyncMock()
    return handler


@pytest.fixture
def text_parser(mock_handler):
    project_root = Path(__file__).resolve().parents[2]
    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")
    catalog = PatternCatalog(patterns_file)
    p = TextParser(mock_handler, catalog)
    # wh-z69w: simulate router-vetted command mode (wh-qj70s hotword gate).
    orig = p.parse_and_execute
    p.parse_and_execute = (
        lambda text, **kw: orig(text, **{"authorized_command": True, **kw})
    )
    return p


class TestFullPipeline:
    """End-to-end: spoken utterance -> IPC hotkey actions."""

    @pytest.mark.asyncio
    async def test_go_right_two_words(self, text_parser, mock_handler):
        matched = await text_parser.parse_and_execute("go right two words")
        assert matched is True
        mock_handler.app.send_request.assert_called_once_with(
            "hotkey_action", {"keys": ["ctrl", "right"], "repeat": 2}
        )

    @pytest.mark.asyncio
    async def test_grab_to_end(self, text_parser, mock_handler):
        matched = await text_parser.parse_and_execute("grab to end")
        assert matched is True
        mock_handler.app.send_request.assert_called_once_with(
            "hotkey_action", {"keys": ["shift", "end"], "repeat": 1}
        )

    @pytest.mark.asyncio
    async def test_chained_go_home_grab_to_end(self, text_parser, mock_handler):
        matched = await text_parser.parse_and_execute("go home then grab to end")
        assert matched is True
        assert mock_handler.app.send_request.call_count == 2
        calls = mock_handler.app.send_request.call_args_list
        assert calls[0].args == ("hotkey_action", {"keys": ["home"], "repeat": 1})
        assert calls[1].args == ("hotkey_action", {"keys": ["shift", "end"], "repeat": 1})

    @pytest.mark.asyncio
    async def test_invalid_falls_through_to_dictation(self, text_parser, mock_handler):
        """'go banana' matches the pattern but parser fails -> insert as text."""
        matched = await text_parser.parse_and_execute("go banana")
        assert matched is True  # Pattern matched (^go\s+.+)
        # Should have sent insert_text via send_command
        mock_handler.app.send_command.assert_called_once()
        payload = mock_handler.app.send_command.call_args[0][0]
        assert payload["action"] == "intelligent_insert_text"
        assert "go banana" in str(payload["params"])

    @pytest.mark.asyncio
    async def test_unrelated_command_not_affected(self, text_parser, mock_handler):
        """Existing commands like 'undo' should still work."""
        matched = await text_parser.parse_and_execute("undo")
        assert matched is True
