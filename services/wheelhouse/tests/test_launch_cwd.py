"""Programs and browsers that Wheelhouse starts for the user must not get a
current directory inside the app folder (wh-installer-update-folder-locked).

Wheelhouse runs with services\\wheelhouse as its current directory. A program
it starts inherits that directory and can outlive Wheelhouse, which then keeps
an update from deleting the app folder. Each call site below is driven with
the launch call replaced by a recorder; the assertion is the start folder that
the call receives.

The app folder stands in as ``APP_DIR`` (this package's folder). The start
folder must be an existing directory outside it.
"""
import asyncio
import os
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

APP_DIR = Path(__file__).resolve().parent.parent

_REAL_WEBBROWSER_OPEN = webbrowser.open


def _outside_app(folder) -> bool:
    folder = Path(folder).resolve()
    return folder.is_dir() and APP_DIR not in (folder, *folder.parents)


@pytest.fixture
def profile(monkeypatch, tmp_path):
    """A user profile folder that is not the real one."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def startfile(monkeypatch):
    """Record os.startfile calls as (args, kwargs); nothing is launched."""
    calls = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(os, "startfile", record, raising=False)
    return calls


@pytest.fixture
def real_browser_open(monkeypatch):
    """webbrowser.open as shipped, with the remaining registered browsers
    removed so a failure cannot reach a real browser."""
    from utils.launch_cwd import ensure_browser_start_folder

    ensure_browser_start_folder()
    monkeypatch.setattr(webbrowser, "open", _REAL_WEBBROWSER_OPEN)
    monkeypatch.setattr(
        webbrowser, "_tryorder", [webbrowser._tryorder[0]], raising=False
    )


@pytest.fixture
def unregistered_browser_open(monkeypatch):
    """webbrowser.open as shipped, in a process where nothing has registered
    OutsideAppBrowser yet, and with only the Windows default left to try."""
    if webbrowser._tryorder is None:
        webbrowser.register_standard_browsers()
    monkeypatch.setattr(webbrowser, "open", _REAL_WEBBROWSER_OPEN)
    monkeypatch.setattr(
        webbrowser,
        "_browsers",
        {k: v for k, v in webbrowser._browsers.items()
         if k != "wheelhouse-outside-app"},
    )
    monkeypatch.setattr(webbrowser, "_tryorder", ["windows-default"])


# ---------------------------------------------------------------- helper


class TestLaunchStartFolder:
    def test_it_is_the_user_profile_folder(self, profile):
        from utils.launch_cwd import launch_start_folder

        assert launch_start_folder() == str(profile)

    def test_a_missing_profile_folder_falls_back_to_an_existing_one(
        self, monkeypatch, tmp_path
    ):
        from utils.launch_cwd import launch_start_folder

        gone = tmp_path / "no_such_profile"
        monkeypatch.setenv("USERPROFILE", str(gone))
        monkeypatch.setenv("HOME", str(gone))
        folder = launch_start_folder()
        assert folder != str(gone)
        assert _outside_app(folder)


class TestBrowserStartFolder:
    def test_opening_a_url_starts_it_outside_the_app_folder(
        self, profile, startfile, real_browser_open
    ):
        assert webbrowser.open("https://example.test/") is True
        assert startfile == [
            (("https://example.test/",), {"cwd": str(profile)})
        ]

    def test_a_shell_refusal_returns_false_like_the_default_browser(self):
        from utils.launch_cwd import OutsideAppBrowser

        def refuse(*args, **kwargs):
            raise OSError("no handler")

        with patch.object(os, "startfile", refuse, create=True):
            assert OutsideAppBrowser().open("https://example.test/") is False

    def test_registering_twice_adds_one_browser(self):
        from utils.launch_cwd import ensure_browser_start_folder

        ensure_browser_start_folder()
        before = list(webbrowser._tryorder)
        ensure_browser_start_folder()
        assert list(webbrowser._tryorder) == before


# ------------------------------------------------------------ call sites


@pytest.fixture
def action_funcs():
    from speech.actions import ActionFunctions

    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


class TestSpeechActionSites:
    def test_importing_the_actions_module_registers_the_browser(self):
        import subprocess

        code = (
            "import speech.actions, webbrowser; "
            "print('wheelhouse-outside-app' in webbrowser._browsers)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=APP_DIR, timeout=120,
            capture_output=True, text=True,
        )
        assert out.stdout.strip().endswith("True"), out.stderr[-500:]

    def test_gsearch(self, profile, startfile, real_browser_open, action_funcs):
        asyncio.run(action_funcs.GSearch("hello"))
        assert len(startfile) == 1
        assert startfile[0][1]["cwd"] == str(profile)

    def test_open_url(self, profile, startfile, real_browser_open, action_funcs):
        asyncio.run(action_funcs.open_url("https://example.test/a"))
        assert len(startfile) == 1
        assert startfile[0][1]["cwd"] == str(profile)

    def test_run_program(self, profile, action_funcs):
        with patch("speech.actions.subprocess.Popen") as popen:
            asyncio.run(action_funcs.run_program("notepad.exe"))
        assert popen.call_args.kwargs["cwd"] == str(profile)
        assert _outside_app(popen.call_args.kwargs["cwd"])


# ------------------------------------------------------------- Qt links


class TestQtLinkSites:
    def test_about_window_website_link(
        self, profile, startfile, unregistered_browser_open
    ):
        import about_window

        about_window.AboutWindow.open_website(SimpleNamespace())
        assert len(startfile) == 1
        assert startfile[0][1]["cwd"] == str(profile)

    def test_pattern_help_external_link(
        self, profile, startfile, unregistered_browser_open
    ):
        import pattern_help_dialog
        from PySide6.QtCore import QUrl

        pattern_help_dialog.PatternHelpDialog._on_anchor_clicked(
            SimpleNamespace(), QUrl("https://example.test/regex")
        )
        assert len(startfile) == 1
        assert startfile[0][1]["cwd"] == str(profile)
