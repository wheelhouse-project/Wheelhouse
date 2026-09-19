"""Arrow key names and the failed-press word loss (wh-arrow-key-names-missing).

Two defects, both reported by David on 2026-08-27:

1. ``SPOKEN_KEY_MAP`` in ``speech/actions.py`` carries no arrow entries, so
   the natural spoken forms "up arrow", "down arrow", "left arrow" and
   "right arrow" all fail. ``press_keys`` logged "Unrecognized key 'arrow'"
   and the four arrow keys were unreachable by voice.

2. When ``press_keys`` could not parse the key names it returned ``None``.
   ``TextParser._execute_rule`` returned ``True`` anyway, so
   ``SpeechProcessor._execute_command`` logged "Pattern executed
   successfully" and consumed the words. Nothing pressed and nothing typed.
   The "treating as dictation" wording in the log was false.

These tests cover both defects at three levels: the action function, the
rule engine, and the speech processor.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import re
from typing import Any, Dict, List

import pytest
from unittest.mock import MagicMock

from speech.actions import ActionFailed, ActionFunctions
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor


# ============================================================================
# MOCK COMPONENTS
# ============================================================================

class MockApp:
    """Minimal mock app that records every payload sent to the UI."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: dict):
        self.actions.append({"action": action, "params": params})
        return {"status": "success"}


@pytest.fixture
def mock_app():
    return MockApp()


@pytest.fixture
def action_funcs(mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return ActionFunctions(handler)


@pytest.fixture
def catalog():
    return PatternCatalog(str(patterns_path))


@pytest.fixture
def parser(catalog, mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return TextParser(handler, catalog)


@pytest.fixture
def processor(catalog, parser, mock_app):
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=parser,
        app=mock_app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )


def _hotkey_actions(app: MockApp) -> List[Dict[str, Any]]:
    return [a for a in app.actions if a.get("action") == "hotkey_action"]


def _dictation_actions(app: MockApp) -> List[Dict[str, Any]]:
    return [
        a for a in app.actions
        if a.get("action") == "intelligent_insert_text"
    ]


# ============================================================================
# DEFECT 1: the four spoken arrow names
# ============================================================================

class TestSpokenArrowNames:
    @pytest.mark.parametrize(
        "spoken,expected",
        [
            ("up arrow", "up"),
            ("down arrow", "down"),
            ("left arrow", "left"),
            ("right arrow", "right"),
        ],
    )
    def test_arrow_phrase_presses_the_matching_key(
        self, action_funcs, spoken, expected
    ):
        result = action_funcs.press_keys(spoken)
        assert result is not None
        assert result["action"] == "hotkey_action"
        assert result["params"]["keys"] == [expected]

    def test_arrow_with_modifier(self, action_funcs):
        """'control left arrow' is ctrl plus left, modifiers first."""
        result = action_funcs.press_keys("control left arrow")
        keys = result["params"]["keys"]
        assert keys == ["ctrl", "left"]

    def test_hyphenated_arrow_from_stt(self, action_funcs):
        """STT writes 'up arrow' as 'up-arrow'; the split path recovers it."""
        result = action_funcs.press_keys("up-arrow")
        assert result["params"]["keys"] == ["up"]

    def test_bare_direction_still_works(self, action_funcs):
        """The pre-existing bare 'up' name must keep working."""
        result = action_funcs.press_keys("up")
        assert result["params"]["keys"] == ["up"]


# ============================================================================
# DEFECT 2: a failed press must not consume the words
# ============================================================================

class TestFailedPressRaises:
    def test_unrecognized_key_raises(self, action_funcs):
        with pytest.raises(ActionFailed):
            action_funcs.press_keys("xyzzy zorp")

    def test_empty_sequence_raises(self, action_funcs):
        with pytest.raises(ActionFailed):
            action_funcs.press_keys("")

    def test_whitespace_only_sequence_raises(self, action_funcs):
        with pytest.raises(ActionFailed):
            action_funcs.press_keys("   ")


class TestRuleEngineAbortsOnActionFailed:
    @pytest.mark.asyncio
    async def test_execute_rule_returns_false(self, parser):
        match = re.fullmatch(r"press (.+)", "press xyzzy")
        steps = [{"function": "press_keys", "params": ["g1"]}]

        result = await parser._execute_rule(match, steps, validation_group=None)

        assert result is False

    @pytest.mark.asyncio
    async def test_later_steps_do_not_run(self, parser, mock_app):
        """The rule stops at the failed step; nothing after it reaches the UI."""
        match = re.fullmatch(r"press (.+)", "press xyzzy")
        steps = [
            {"function": "press_keys", "params": ["g1"]},
            {"function": "type_text", "params": ["should not be typed"]},
        ]

        result = await parser._execute_rule(match, steps, validation_group=None)

        assert result is False
        assert mock_app.actions == []

    @pytest.mark.asyncio
    async def test_parse_and_execute_reports_no_match(self, parser):
        assert await parser.parse_and_execute("press xyzzy") is False

    @pytest.mark.asyncio
    async def test_parse_and_execute_runs_the_arrow(self, parser, mock_app):
        assert await parser.parse_and_execute("press up arrow") is True
        hotkeys = _hotkey_actions(mock_app)
        assert len(hotkeys) == 1
        assert hotkeys[0]["params"]["keys"] == ["up"]


class TestProcessorDictatesTheLostWords:
    @pytest.mark.asyncio
    async def test_unrecognized_key_falls_back_to_dictation(
        self, processor, mock_app
    ):
        """The words David spoke get typed instead of vanishing."""
        await processor._execute_command("press xyzzy")

        assert _hotkey_actions(mock_app) == []
        dictated = _dictation_actions(mock_app)
        assert len(dictated) == 1
        assert dictated[0]["params"]["insertion_string"] == "press xyzzy"

    @pytest.mark.asyncio
    async def test_arrow_presses_and_types_nothing(self, processor, mock_app):
        await processor._execute_command("press up arrow")

        hotkeys = _hotkey_actions(mock_app)
        assert len(hotkeys) == 1
        assert hotkeys[0]["params"]["keys"] == ["up"]
        assert _dictation_actions(mock_app) == []
