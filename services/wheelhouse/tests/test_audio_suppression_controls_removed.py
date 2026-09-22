"""The audio suppression switch is gone (wh-audio-suppression-auto).

David ruled on 2026-09-17 that the sound pause is no longer a user switch.
The menu entries, the two spoken commands "audio suppression on" and "audio
suppression off", the three notices, the state manager's setter and the GUI's
sender were removed, with no alias and no replacement.

tests/test_gui.py::TestRemovedMenuItems checks the two menus and the GUI
attribute that held the checkmark. Each test below checks one other removed
part, so a change that brings a part back fails the test that names it.

An absence assertion can pass for the wrong reason: a table that failed to
build lacks the removed name too. So each test that reads a table first
asserts that a neighbour that stays in that table is present.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

SERVICE_ROOT = Path(__file__).resolve().parent.parent
PATTERNS_PATH = SERVICE_ROOT / "speech" / "config" / "patterns.toml"
KNOWLEDGE_ROOT = SERVICE_ROOT / "knowledge"

REMOVED_ACTIONS = ("audio_suppression_off", "audio_suppression_on")

# Matches the words "audio suppression on" and "audio suppression off" as a
# spoken command offer. Does not match the quote of the log line
# "Audio suppression off: Windows reports an echo canceller", which
# utils/audio_suppression_decision.py writes; that quote always continues
# with a colon (wh-release-after-1-0-8.9).
SPOKEN_COMMAND_TEXT = re.compile(
    r"\baudio suppression o(?:n|ff)\b(?!\s*:)", re.IGNORECASE
)


def test_the_gui_has_no_audio_suppression_sender():
    from gui import GuiManager

    assert not hasattr(GuiManager, "toggle_audio_suppression")
    assert not hasattr(GuiManager, "_request_tray_audio_suppression_toggle")


def test_the_gui_does_not_stage_an_audio_suppression_write():
    """The staged settings path no longer names the removed command.

    ``send_command`` sends an acknowledged settings write through
    ``_send_settings_command`` and every other action straight onto the
    queue to Logic.
    """
    from gui import GuiManager

    # Guard: this stand-in does reach the staged path for a settings write
    # that stays, so the assert_not_called below is not vacuous.
    guard = MagicMock()
    GuiManager.send_command(
        guard, {"action": "set_config_value", "key": "SHOW_SPEECH_PULSE",
                "value": False}
    )
    guard._send_settings_command.assert_called_once()

    manager = MagicMock()
    GuiManager.send_command(
        manager, {"action": "set_audio_suppression_enabled", "value": False}
    )

    manager._send_settings_command.assert_not_called()
    manager.commands_to_logic_queue.put_nowait.assert_called_once()


def test_the_logic_handler_map_has_no_audio_suppression_setter():
    from main import LogicController

    handler_map = LogicController._build_gui_handler_map(MagicMock(), {})

    assert "set_config_value" in handler_map
    assert "set_audio_suppression_enabled" not in handler_map


def test_the_state_manager_has_no_audio_suppression_setter():
    from state_manager import StateManager

    assert hasattr(StateManager, "set_config_value")
    assert not hasattr(StateManager, "set_audio_suppression_enabled")


def test_the_speech_handler_has_no_audio_suppression_matcher():
    """The matcher answered one question: is this final the off command?

    Nothing asks any more. The websocket transcript gates that asked were
    removed with the recovery window they guarded.
    """
    import speech.speech_handler as speech_handler
    from speech.speech_handler import SpeechHandler

    assert hasattr(SpeechHandler, "initialize_speech_processor")
    assert not hasattr(SpeechHandler, "text_switches_audio_suppression_off")
    assert not hasattr(speech_handler, "AUDIO_SUPPRESSION_OFF_ACTION")


def test_the_state_manager_has_no_audio_suppression_notices():
    import state_manager

    assert hasattr(state_manager, "SPEECH_OFF_NOTICE")
    names = (
        "AUDIO_SUPPRESSION_OFF_NOTICE",
        "AUDIO_SUPPRESSION_ON_NOTICE",
        "AUDIO_SUPPRESSION_SAVE_FAILED_NOTICE",
    )
    assert [name for name in names if hasattr(state_manager, name)] == []


def test_the_action_functions_have_no_audio_suppression_actions():
    from speech.actions import ActionFunctions

    names = (*REMOVED_ACTIONS, "_set_audio_suppression")
    assert [name for name in names if hasattr(ActionFunctions, name)] == []

    functions = ActionFunctions(MagicMock()).get_functions()
    assert "set_speech_interaction_mode" in functions
    assert [name for name in REMOVED_ACTIONS if name in functions] == []


def test_the_catalog_has_no_audio_suppression_actions():
    from speech.action_catalog import CATALOG_BY_NAME

    assert "set_speech_interaction_mode" in CATALOG_BY_NAME
    assert [name for name in REMOVED_ACTIONS if name in CATALOG_BY_NAME] == []


def test_no_shipped_pattern_calls_an_audio_suppression_action():
    """Every action step of every entry in the shipped patterns.toml."""
    data = tomllib.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    functions = []
    for value in data.values():
        if not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, dict):
                continue
            for step in entry.get("actions") or []:
                if isinstance(step, dict):
                    functions.append(step.get("function"))

    assert "set_speech_interaction_mode" in functions
    assert [name for name in functions if name in REMOVED_ACTIONS] == []


def test_no_knowledge_file_offers_an_audio_suppression_command():
    """The bead's criterion 6 search, as a test.

    Every file under knowledge/ is read, at any depth. A file that is not
    UTF-8 text cannot hold the words, so it is skipped.
    """
    files = sorted(path for path in KNOWLEDGE_ROOT.rglob("*") if path.is_file())
    # Guard: the walk reached the files that held the commands.
    # command_descriptions.toml lives only under knowledge/helpdoc/, which
    # the release manifest prunes, so it is required exactly where that
    # directory is present (wh-test-release-2026-09.2.3). The scan below
    # reads every remaining knowledge file in both cases.
    names = {path.name for path in files}
    expected = {"wheelhouse_reference.md"}
    if (KNOWLEDGE_ROOT / "helpdoc").is_dir():
        expected.add("command_descriptions.toml")
    assert expected <= names

    hits = []
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if SPOKEN_COMMAND_TEXT.search(line):
                hits.append(
                    f"{path.relative_to(KNOWLEDGE_ROOT).as_posix()}:{number}:"
                    f" {line.strip()[:120]}"
                )
    assert hits == []
