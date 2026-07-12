"""Tests for the activate_window launch fallback (wh-activate-launch-fallback).

"x-ray notepad" with Notepad not running matched the pattern, reached the
Input process, found no window, and silently did nothing -- activate was
activation-only. For process (.exe) targets, _handle_activate_window now
falls back to launching the executable via os.startfile (ShellExecute:
resolves System32, PATH, and the App Paths registry, which covers
notepad.exe, brave.exe, msedge.exe, and code.exe). Title-regex targets
keep the activation-only behavior, and a launch failure degrades to the
previous warning-only outcome instead of raising.
"""
import os
import threading
from unittest.mock import MagicMock

import pytest

import input_proc


@pytest.fixture
def startfile_calls(monkeypatch):
    """Record os.startfile calls without launching anything.

    raising=False because os.startfile only exists on Windows; on a
    non-Windows sandbox the attribute is created for the test.
    """
    calls = []
    monkeypatch.setattr(
        os, "startfile", lambda target: calls.append(target), raising=False
    )
    return calls


def _handle(monkeypatch, target, *, found_hwnd, request_id=None):
    """Drive _handle_activate_window with the window search stubbed out."""
    monkeypatch.setattr(
        input_proc, "_find_window_by_target", lambda t, logger: found_hwnd
    )
    activated = []
    monkeypatch.setattr(
        input_proc,
        "_activate_window_impl",
        lambda hwnd, logger: activated.append(hwnd) or True,
    )
    is_internal_action = threading.Event()
    input_proc._handle_activate_window(
        {"target": target},
        request_id,
        MagicMock(),
        "activate_window",
        is_internal_action,
        50,
        10,
        {},
        MagicMock(),
    )
    assert not is_internal_action.is_set()
    return activated


class TestActivateWindowLaunchFallback:
    def test_exe_target_launches_when_no_window_found(
        self, monkeypatch, startfile_calls
    ):
        activated = _handle(monkeypatch, "notepad.exe", found_hwnd=None)
        assert startfile_calls == ["notepad.exe"]
        assert activated == []

    def test_exe_target_activates_existing_window_without_launching(
        self, monkeypatch, startfile_calls
    ):
        activated = _handle(monkeypatch, "notepad.exe", found_hwnd=4242)
        assert startfile_calls == []
        assert activated == [4242]

    def test_title_target_never_launches(self, monkeypatch, startfile_calls):
        activated = _handle(monkeypatch, "Untitled - Notepad", found_hwnd=None)
        assert startfile_calls == []
        assert activated == []

    def test_launch_failure_degrades_to_warning(self, monkeypatch):
        def boom(target):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        # Must not raise; the handler swallows the launch failure exactly
        # like the old no-window case.
        activated = _handle(monkeypatch, "ghost.exe", found_hwnd=None)
        assert activated == []
