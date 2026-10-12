"""The "settings" voice command (wh-voice-settings-window, stage B, acceptance 1).

Saying "settings" (or "setting") by itself opens the settings window. The
action follows the Pattern Manager precedent: ``open_settings_window`` sends
{"action": "open_settings_window"} on the Logic-to-GUI state queue and the
GUI process opens the window.

These tests load the real shipped patterns.toml, like
test_pattern_manager_trigger.py and test_calibration_trigger.py.
"""
import asyncio
import re
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

from speech import action_catalog
from speech.actions import ActionFunctions
from speech.pattern_catalog import PatternCatalog

_SERVICE_DIR = Path(__file__).parent.parent
_SYSTEM_PATTERNS = _SERVICE_DIR / "speech" / "config" / "patterns.toml"


def _rows():
    return tomllib.loads(_SYSTEM_PATTERNS.read_text(encoding="utf-8"))["pattern"]


def _first_owner(form):
    """doc_id of the first anchored shipped row that fullmatches ``form``."""
    for row in _rows():
        source = row["pattern"]
        if not source.startswith("^"):
            continue
        if re.compile(source, re.IGNORECASE).fullmatch(form):
            return row.get("doc_id")
    return None


def _settings_entry(catalog):
    entries = [
        p
        for p in catalog.get_all_patterns()
        if any(a.get("function") == "open_settings_window" for a in p["actions"])
    ]
    assert len(entries) == 1, "expected exactly one open_settings_window command"
    return entries[0]


def test_settings_and_setting_resolve_as_a_whole_utterance(tmp_path):
    # Hermetic: a user file that does not exist, so only the shipped file loads.
    catalog = PatternCatalog(
        str(_SYSTEM_PATTERNS), user_patterns_file=str(tmp_path / "user.toml")
    )
    entry = _settings_entry(catalog)
    compiled = entry["compiled_pattern"]

    assert compiled.fullmatch("settings")
    assert compiled.fullmatch("setting")
    assert not compiled.fullmatch("settings window")
    assert not compiled.fullmatch("my settings")

    assert entry["pattern_type"] == "command"
    assert entry["whole_utterance_only"] is True
    assert entry.get("requires_hotword", False) is False
    assert entry["doc_id"] == "settings-window"
    assert entry["actions"] == [{"function": "open_settings_window"}]


def test_settings_forms_reach_the_settings_row_first():
    assert _first_owner("settings") == "settings-window"
    assert _first_owner("setting") == "settings-window"


def test_windows_settings_still_reaches_only_its_own_row():
    assert _first_owner("windows settings") == "windows-settings"
    assert _first_owner("window settings") == "windows-settings"
    owners = [
        row.get("doc_id")
        for row in _rows()
        if row["pattern"].startswith("^")
        and re.compile(row["pattern"], re.IGNORECASE).fullmatch("windows settings")
    ]
    assert owners == ["windows-settings"]


def test_open_settings_window_action_sends_the_gui_open_message():
    handler = MagicMock()
    funcs = ActionFunctions(handler)

    assert "open_settings_window" in funcs.get_functions()
    asyncio.run(funcs.open_settings_window())

    queue = handler.logic_controller.state_manager.state_to_gui_queue
    queue.put_nowait.assert_called_once_with({"action": "open_settings_window"})


def test_the_action_catalog_describes_open_settings_window():
    names = [entry["name"] for entry in action_catalog.ACTION_CATALOG]
    assert names.count("open_settings_window") == 1
