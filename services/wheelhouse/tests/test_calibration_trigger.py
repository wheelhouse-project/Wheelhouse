# tests/test_calibration_trigger.py
"""The voice-calibration entry commands ship in patterns.toml (wh-7ou.7.2.4).

Spec: docs/superpowers/specs/2026-08-07-voice-calibration-design.md
Section 3.1: the voice commands "learn my voice" and "calibrate my voice"
(alias) open the Teach-WheelHouse-your-voice window. Section 6.4: the
action follows the Pattern Manager precedent -- it sends
{"action": "open_calibration"} on the Logic-to-GUI state queue, and the
GUI opens the window (gui.py handles "open_calibration" the same way it
handles "open_pattern_manager").

These tests load the real shipped patterns.toml, mirroring
test_pattern_manager_trigger.py.
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from speech.actions import ActionFunctions
from speech.pattern_catalog import PatternCatalog

_SERVICE_DIR = Path(__file__).parent.parent
_SYSTEM_PATTERNS = _SERVICE_DIR / "speech" / "config" / "patterns.toml"


def _open_calibration_entry(catalog):
    """The single shipped command whose action opens the calibration window."""
    entries = [
        p
        for p in catalog.get_all_patterns()
        if any(a.get("function") == "open_calibration" for a in p["actions"])
    ]
    assert len(entries) == 1, "expected exactly one open_calibration command"
    return entries[0]


def test_learn_my_voice_and_its_alias_resolve(tmp_path):
    # Hermetic: point at a user file that does not exist, so only the shipped
    # patterns.toml is loaded.
    user_file = tmp_path / "user_patterns.toml"
    catalog = PatternCatalog(str(_SYSTEM_PATTERNS), user_patterns_file=str(user_file))

    entry = _open_calibration_entry(catalog)
    compiled = entry["compiled_pattern"]

    # Both spec Section 3.1 phrases fire the same command.
    assert compiled.fullmatch("learn my voice")
    assert compiled.fullmatch("calibrate my voice")
    # Nearby non-phrases do not.
    assert not compiled.fullmatch("learn voice")
    assert not compiled.fullmatch("my voice")

    # The help document instructs users to say the bare phrases (no
    # "x-ray" prefix), so the command must not be hotword-gated.
    assert entry["pattern_type"] == "command"
    assert entry["requires_hotword"] is False


def test_open_calibration_action_sends_the_gui_open_message():
    handler = MagicMock()
    funcs = ActionFunctions(handler)

    assert "open_calibration" in funcs.get_functions()
    asyncio.run(funcs.open_calibration())

    queue = handler.logic_controller.state_manager.state_to_gui_queue
    queue.put_nowait.assert_called_once_with({"action": "open_calibration"})
