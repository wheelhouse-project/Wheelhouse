"""Tests for the GUI-process verified-paste helper's elevation gate
(wh-elevated-target-notice.1.2).

An elevated terminal silently discards SendInput from a medium-integrity
process (UIPI) while every existing verification step -- foreground
match, clipboard poll, SendInput event counts -- still passes, so the
helper would report SUCCESS for a paste that never arrived. The helper
must refuse the paste up front when the captured terminal window is
elevated, and keep the fail-open contract (proceed on "unknown" or a
broken check). This is the defense-in-depth layer behind the
FocusRedirectPolicy's elevated_terminal decline: it covers a terminal
HWND that stops being trustworthy between the redirect decision and
Enter (window-handle recycling) and any future editor entry path.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from utils import gui_terminal_paste as mod
from utils.gui_terminal_paste import PasteOutcome, paste_into_terminal

_HWND = 0x4321
_TEXT = "echo hello"


class _FakeClipboard:
    """Stateful pyperclip stand-in: copy() is immediately visible."""

    def __init__(self) -> None:
        self.value = "previous-contents"

    def copy(self, text: str) -> None:
        self.value = text

    def paste(self) -> str:
        return self.value


@pytest.fixture
def happy_path(monkeypatch):
    """Stub every Win32/clipboard boundary for a full-success run."""
    fake_win32gui = Mock()
    fake_win32gui.IsWindow.return_value = True
    fake_win32gui.IsIconic.return_value = False
    fake_win32gui.GetForegroundWindow.return_value = _HWND
    monkeypatch.setattr(mod, "win32gui", fake_win32gui)

    clipboard = _FakeClipboard()
    monkeypatch.setattr(mod, "pyperclip", clipboard)

    presses: list[tuple[str, ...]] = []

    def fake_press(*keys: str):
        presses.append(keys)
        return (True, len(keys) * 2, len(keys) * 2)

    monkeypatch.setattr(mod, "verified_press_keys", fake_press)
    monkeypatch.setattr(mod, "_send_modifier_keyups", lambda _keys: None)
    monkeypatch.setattr(mod, "_FOREGROUND_SETTLE_S", 0.0)
    monkeypatch.setattr(mod, "_POST_PASTE_SETTLE_S", 0.0)

    return fake_win32gui, clipboard, presses


class TestElevatedTargetRefusal:
    def test_elevated_target_refused_before_any_side_effect(
        self, monkeypatch, happy_path
    ):
        fake_win32gui, _clipboard, presses = happy_path
        # Replace the clipboard with a Mock so "never touched" is
        # provable, not just "unchanged".
        fake_clip = Mock()
        monkeypatch.setattr(mod, "pyperclip", fake_clip)

        elevation = Mock(return_value="elevated")
        monkeypatch.setattr(
            mod, "elevation_state_of_hwnd", elevation, raising=False,
        )

        outcome = paste_into_terminal(_TEXT, _HWND)

        assert outcome is PasteOutcome.ELEVATED_TARGET
        elevation.assert_called_once_with(_HWND)
        fake_win32gui.SetForegroundWindow.assert_not_called()
        fake_clip.copy.assert_not_called()
        fake_clip.paste.assert_not_called()
        assert presses == []

    def test_dead_hwnd_short_circuits_before_elevation_check(
        self, monkeypatch, happy_path
    ):
        fake_win32gui, _clipboard, _presses = happy_path
        fake_win32gui.IsWindow.return_value = False

        elevation = Mock(return_value="elevated")
        monkeypatch.setattr(
            mod, "elevation_state_of_hwnd", elevation, raising=False,
        )

        outcome = paste_into_terminal(_TEXT, _HWND)

        assert outcome is PasteOutcome.INVALID_HWND
        elevation.assert_not_called()

    @pytest.mark.parametrize("state", ["not_elevated", "unknown"])
    def test_not_elevated_or_unknown_proceeds_to_success(
        self, monkeypatch, happy_path, state
    ):
        _fake_win32gui, clipboard, presses = happy_path

        elevation = Mock(return_value=state)
        monkeypatch.setattr(
            mod, "elevation_state_of_hwnd", elevation, raising=False,
        )

        outcome = paste_into_terminal(_TEXT, _HWND)

        assert outcome is PasteOutcome.SUCCESS
        elevation.assert_called_once_with(_HWND)
        assert presses == [("ctrl", "v"), ("enter",)]
        # Original clipboard restored on the way out.
        assert clipboard.value == "previous-contents"

    def test_broken_elevation_check_fails_open(
        self, monkeypatch, happy_path
    ):
        _fake_win32gui, _clipboard, presses = happy_path

        elevation = Mock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            mod, "elevation_state_of_hwnd", elevation, raising=False,
        )

        outcome = paste_into_terminal(_TEXT, _HWND)

        assert outcome is PasteOutcome.SUCCESS
        assert presses == [("ctrl", "v"), ("enter",)]
