"""scratch_window.list_handles sees a minimized window (wh-dictation-paste-loss-load).

Measured 2026-10-19 22:08: David's Notepad was minimized, and the old
list_handles (the UI Automation root children) did not list it. The probe
scripts use that list as the set of windows that were open before their
launch, so a missing window is one they cannot protect. Boss ruling
22:10: list visible top-level windows of the class, minimized ones
included, and no hidden windows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "benchmarks" / "scratch_window.py"


def _load():
    spec = importlib.util.spec_from_file_location("scratch_window", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Error(Exception):
    pass


def test_list_handles_includes_a_minimized_window_and_skips_hidden_ones():
    sw = _load()
    windows = {
        101: ("Notepad", True),    # a normal window
        102: ("Notepad", True),    # minimized: still WS_VISIBLE
        103: ("Notepad", False),   # hidden
        104: ("OpusApp", True),    # another class
        105: (None, True),         # closes during the walk
    }

    def get_class_name(hwnd):
        name = windows[hwnd][0]
        if name is None:
            raise _Error("window closed")
        return name

    def enum_windows(callback, extra):
        for hwnd in windows:
            if not callback(hwnd, extra):
                break

    fake_gui = MagicMock()
    fake_gui.error = _Error
    fake_gui.EnumWindows.side_effect = enum_windows
    fake_gui.GetClassName.side_effect = get_class_name
    fake_gui.IsWindowVisible.side_effect = lambda hwnd: windows[hwnd][1]
    fake_gui.IsIconic.side_effect = lambda hwnd: hwnd == 102
    # The UI Automation root lists no window, as it did for the minimized
    # Notepad on 2026-10-19.
    empty_root = MagicMock()
    empty_root.GetChildren.return_value = []
    with patch.object(sw, "win32gui", fake_gui), \
            patch.object(sw.auto, "GetRootControl", return_value=empty_root):
        assert sw.list_handles("Notepad") == {101, 102}


def _prompt_button(exists: bool) -> MagicMock:
    button = MagicMock()
    button.Exists.return_value = exists
    return button


def _top_level(hwnd: int, has_discard_button: bool):
    """A UI Automation root child with or without a Don't-save button."""
    child = MagicMock()
    child.NativeWindowHandle = hwnd
    button = _prompt_button(has_discard_button)
    child.ButtonControl.return_value = button
    return child, button


def _click_discard_with(sw, owners: dict[int, int], children):
    """Run _click_discard(500) when the scratch window holds no prompt.

    ``owners`` maps a top-level window to the window that owns it.
    """
    scratch = MagicMock()
    scratch.ButtonControl.return_value = _prompt_button(False)
    root = MagicMock()
    root.GetChildren.return_value = children
    fake_gui = MagicMock()
    fake_gui.error = _Error
    fake_gui.GetWindow.side_effect = lambda hwnd, _flag: owners.get(hwnd, 0)
    with patch.object(sw, "win32gui", fake_gui), \
            patch.object(sw.auto, "ControlFromHandle", return_value=scratch), \
            patch.object(sw.auto, "GetRootControl", return_value=root):
        return sw._click_discard(500)


def test_click_discard_never_answers_another_programs_save_prompt():
    # wh-dictation-paste-loss-load.1.10: the fallback walked every
    # top-level window and clicked the first Don't-save button, so a save
    # prompt that David was answering in another program could lose his
    # text.
    sw = _load()
    other, other_button = _top_level(900, has_discard_button=True)

    assert _click_discard_with(sw, {900: 0}, [other]) is False
    other_button.Click.assert_not_called()


def test_click_discard_answers_a_prompt_that_the_scratch_window_owns():
    sw = _load()
    other, other_button = _top_level(900, has_discard_button=True)
    owned, owned_button = _top_level(901, has_discard_button=True)

    assert _click_discard_with(sw, {900: 0, 901: 500}, [other, owned]) is True
    other_button.Click.assert_not_called()
    owned_button.Click.assert_called_once()


def test_click_discard_skips_a_window_whose_owner_cannot_be_read():
    # A window that closes during the walk makes GetWindow raise. Its
    # owner is unknown, so its button is never clicked.
    sw = _load()
    gone, gone_button = _top_level(902, has_discard_button=True)

    def get_window(hwnd, _flag):
        raise _Error("window closed")

    scratch = MagicMock()
    scratch.ButtonControl.return_value = _prompt_button(False)
    root = MagicMock()
    root.GetChildren.return_value = [gone]
    fake_gui = MagicMock()
    fake_gui.error = _Error
    fake_gui.GetWindow.side_effect = get_window
    with patch.object(sw, "win32gui", fake_gui), \
            patch.object(sw.auto, "ControlFromHandle", return_value=scratch), \
            patch.object(sw.auto, "GetRootControl", return_value=root):
        assert sw._click_discard(500) is False
    gone_button.Click.assert_not_called()
