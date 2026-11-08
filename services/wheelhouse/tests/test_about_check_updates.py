"""The manual update check in the About Wheelhouse window (wh-about-check-updates).

update_check.check_now sends the same one request as the automatic check
and reads and writes no state. The Logic process runs it off the event loop
and sends one ``update_check_result`` to the GUI process. The GUI process
shows the result inside the About Wheelhouse window, which has a "Check for
updates" button and, for a newer release, an "Update now" button that runs
the existing ``update_now`` path. The voice command "check for updates"
opens the window and starts the same check.

The acceptance item that each class covers is in its docstring.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import tomllib
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest

import update_check

from PySide6.QtCore import Qt  # noqa: E402

_OFF_SCREEN = Qt.WidgetAttribute.WA_DontShowOnScreen

pytestmark = pytest.mark.usefixtures("qapp", "about_window_off_screen")


@pytest.fixture
def about_window_off_screen(monkeypatch):
    """Keep every About window and About box these tests make off the screen.

    Boss ruling 2026-10-25 11:22: a test must never put a real window on
    the desktop. Every AboutWindow gets WA_DontShowOnScreen as it is built,
    including the ones GuiManager.show_about_dialog builds, so isVisible()
    still answers True. A QMessageBox that gui.py may hold (the About box
    before wh-about-check-updates) is replaced, so a red run against the
    old code cannot open a modal box either.
    """
    import gui

    monkeypatch.setattr(gui, "QMessageBox", MagicMock(), raising=False)
    try:
        import about_window
    except ImportError:
        # The code before the change has no AboutWindow to build.
        yield
        return

    original_init = about_window.AboutWindow.__init__

    def off_screen_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.setAttribute(_OFF_SCREEN, True)

    monkeypatch.setattr(about_window.AboutWindow, "__init__", off_screen_init)
    yield


def _assert_off_screen(window):
    assert window.testAttribute(_OFF_SCREEN), "an About window reached the screen"

_SERVICE_DIR = Path(__file__).parent.parent
_SYSTEM_PATTERNS = _SERVICE_DIR / "speech" / "config" / "patterns.toml"

RESULT_ACTION = "update_check_result"

CHECKING_TEXT = "Checking for updates..."
NEWER_TEXT = "Wheelhouse 2.0.0 is available"
CURRENT_TEXT = "You have the newest version (1.4.0)."
FAILED_TEXT = (
    "Wheelhouse could not reach GitHub. Check your internet connection and "
    "try again."
)
UNVERSIONED_TEXT = (
    "The newest release is Wheelhouse 2.0.0. This copy of Wheelhouse has no "
    "version number, so Wheelhouse cannot compare them."
)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release_body(tag="v2.0.0"):
    return json.dumps({"tag_name": tag, "name": "x"}).encode()


def _urlopen(body=None, *, error=None):
    """A patch of urlopen, so the real http_get path runs."""
    if error is not None:
        return patch.object(update_check.urllib.request, "urlopen", side_effect=error)
    return patch.object(
        update_check.urllib.request,
        "urlopen",
        return_value=_FakeResponse(_release_body() if body is None else body),
    )


def _assert_the_automatic_request(urlopen, version):
    """The request is the automatic check's request: same URL, User-Agent, timeout."""
    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert request.full_url == update_check.RELEASES_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.header_items() == [
        ("User-agent", update_check.USER_AGENT_PREFIX + version)
    ]
    assert urlopen.call_args.kwargs == {
        "timeout": update_check.REQUEST_TIMEOUT_SECONDS
    }


# ---------------------------------------------------------------------------
# update_check.check_now
# ---------------------------------------------------------------------------


class TestCheckNowSendsTheAutomaticRequest:
    """Acceptance item 1: one request with the URL, User-Agent and timeout of
    the automatic check."""

    def test_one_request_through_the_real_http_path(self):
        with _urlopen() as urlopen:
            result = update_check.check_now(get_version=lambda: "1.4.0")
        _assert_the_automatic_request(urlopen, "1.4.0")
        assert result.status == "newer"

    def test_a_copy_without_a_version_still_sends_the_request(self):
        with _urlopen() as urlopen:
            result = update_check.check_now(get_version=lambda: "unknown")
        _assert_the_automatic_request(urlopen, "unknown")
        assert result.status == "unversioned"


class TestCheckNowResults:
    """Acceptance item 3, the Logic half: each answer gives one status."""

    def test_a_newer_release(self):
        result = update_check.check_now(
            get_version=lambda: "1.4.0", fetch=lambda running: "v2.0.0"
        )
        assert result == update_check.ManualCheckResult(
            status="newer", installed="1.4.0", version="2.0.0", tag="v2.0.0"
        )

    @pytest.mark.parametrize("tag", ["v1.4.0", "1.4", "v1.3.9"])
    def test_the_same_or_an_older_release(self, tag):
        result = update_check.check_now(
            get_version=lambda: "1.4.0", fetch=lambda running: tag
        )
        assert result.status == "current"
        assert result.installed == "1.4.0"

    def test_github_not_reached(self):
        def fetch(running):
            raise update_check.UpdateCheckError("request failed: offline")

        result = update_check.check_now(get_version=lambda: "1.4.0", fetch=fetch)
        assert result.status == "failed"

    def test_a_deeply_nested_answer_is_an_unreadable_answer(self):
        # fetch_latest_tag lets RecursionError escape (crewcut in
        # update_check.fetch_latest_tag); the manual check names it.
        with _urlopen(b"[" * 100_000 + b"]" * 100_000):
            result = update_check.check_now(get_version=lambda: "1.4.0")
        assert result.status == "failed"

    @pytest.mark.parametrize(
        "body", [b"not json", b"{}", json.dumps({"tag_name": 5}).encode()]
    )
    def test_an_unreadable_answer(self, body):
        with _urlopen(body):
            result = update_check.check_now(get_version=lambda: "1.4.0")
        assert result.status == "failed"

    def test_a_tag_that_is_not_a_version_is_an_unreadable_answer(self):
        result = update_check.check_now(
            get_version=lambda: "1.4.0", fetch=lambda running: "nightly"
        )
        assert result.status == "failed"

    def test_a_network_error_through_the_real_path(self):
        with _urlopen(error=OSError("no route")):
            result = update_check.check_now(get_version=lambda: "1.4.0")
        assert result.status == "failed"

    def test_the_running_version_without_a_number(self):
        result = update_check.check_now(
            get_version=lambda: "unknown", fetch=lambda running: "v2.0.0"
        )
        assert result.status == "unversioned"
        assert result.version == "2.0.0"


class TestCheckNowLeavesTheScheduleAlone:
    """Acceptance item 4: the manual check reads no config and leaves the
    state file as it was."""

    def _later_wait_state(self, path):
        # A Later record written one day ago: the 7-day wait is running.
        import time

        now = time.time()
        update_check.save_state(
            path,
            update_check.UpdateCheckState(
                last_check=now - 3600,
                notified_version="2.0.0",
                notified_at=now - 86400,
            ),
        )
        return path.read_bytes()

    def test_during_a_later_wait_the_state_file_is_unchanged(self, tmp_path):
        path = tmp_path / update_check.STATE_FILE_NAME
        before = self._later_wait_state(path)
        with patch.object(update_check, "state_file_path", return_value=path), \
             patch.object(update_check, "save_state") as save, \
             _urlopen() as urlopen:
            result = update_check.check_now(get_version=lambda: "1.4.0")
        urlopen.assert_called_once()
        save.assert_not_called()
        assert result.status == "newer"
        assert path.read_bytes() == before

    def test_no_state_file_is_written_when_there_is_none(self, tmp_path):
        path = tmp_path / update_check.STATE_FILE_NAME
        with patch.object(update_check, "state_file_path", return_value=path), \
             _urlopen():
            update_check.check_now(get_version=lambda: "1.4.0")
        assert not path.exists()
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


def _controller():
    """A stand-in ``self`` for LogicController methods, with a real loop task."""
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller.config_service = MagicMock()
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = Queue()
    controller._manual_update_check_running = False

    def create_task(coro, name):
        return asyncio.get_running_loop().create_task(coro, name=name)

    controller.create_task_with_error_handling.side_effect = create_task
    # A plain function, not side_effect: the spec makes this attribute an
    # AsyncMock, which would wrap the real coroutine in one more await.
    controller._run_manual_update_check = (
        lambda: LogicController._run_manual_update_check(controller)
    )
    return controller


def _gui_messages(controller):
    messages = []
    gui_queue = controller.state_manager.state_to_gui_queue
    while True:
        try:
            messages.append(gui_queue.get_nowait())
        except Empty:
            return messages


def _start(controller):
    from main import LogicController

    return LogicController._start_manual_update_check(controller)


class TestLogicRunsTheManualCheck:
    """Acceptance items 1, 4 and 5, the Logic half."""

    def test_the_gui_command_reaches_the_starter(self):
        from main import LogicController

        controller = MagicMock(spec=LogicController)
        controller.state_manager = MagicMock()
        controller.config_service = MagicMock()
        command = {"action": "check_for_updates"}
        handlers = LogicController._build_gui_handler_map(controller, command)
        handlers["check_for_updates"]()
        controller._start_manual_update_check.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_the_check_runs_in_a_worker_thread_and_sends_one_result(self):
        controller = _controller()
        threads = []

        def fake_check_now():
            threads.append(threading.current_thread())
            return update_check.ManualCheckResult(
                status="newer", installed="1.4.0", version="2.0.0", tag="v2.0.0"
            )

        with patch.object(update_check, "check_now", fake_check_now), \
             patch("main.asyncio.to_thread", wraps=asyncio.to_thread) as to_thread:
            task = _start(controller)
            await task
        to_thread.assert_called_once_with(fake_check_now)
        assert threads and threads[0] is not threading.main_thread()
        assert _gui_messages(controller) == [{
            "action": RESULT_ACTION,
            "status": "newer",
            "installed": "1.4.0",
            "version": "2.0.0",
            "tag": "v2.0.0",
        }]
        assert controller._manual_update_check_running is False

    @pytest.mark.asyncio
    async def test_a_second_command_while_one_runs_sends_no_second_request(self):
        controller = _controller()
        release = threading.Event()
        calls = []

        def fake_check_now():
            calls.append(1)
            release.wait(5)
            return update_check.ManualCheckResult(status="current", installed="1.4.0")

        with patch.object(update_check, "check_now", fake_check_now):
            first = _start(controller)
            for _ in range(5):
                await asyncio.sleep(0)
            second = _start(controller)
            release.set()
            await first
        assert second is None
        assert calls == [1]
        messages = _gui_messages(controller)
        assert [m["status"] for m in messages] == ["current"]

        # The guard ends with the check: a later command checks again.
        with patch.object(update_check, "check_now", fake_check_now):
            await _start(controller)
        assert calls == [1, 1]

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_still_sends_a_failed_result(self):
        controller = _controller()

        def broken():
            raise RuntimeError("boom")

        with patch.object(update_check, "check_now", broken):
            await _start(controller)
        messages = _gui_messages(controller)
        assert len(messages) == 1
        assert messages[0]["action"] == RESULT_ACTION
        assert messages[0]["status"] == "failed"
        assert controller._manual_update_check_running is False

    @pytest.mark.asyncio
    async def test_a_full_gui_queue_does_not_escape_the_task(self):
        controller = _controller()
        controller.state_manager.state_to_gui_queue = Queue(maxsize=1)
        controller.state_manager.state_to_gui_queue.put_nowait("full")

        with patch.object(
            update_check,
            "check_now",
            lambda: update_check.ManualCheckResult(status="current", installed="1.4.0"),
        ):
            await _start(controller)  # must not raise
        assert controller._manual_update_check_running is False

    @pytest.mark.asyncio
    async def test_the_check_runs_with_the_setting_off_and_leaves_the_state(
        self, tmp_path
    ):
        """Acceptance items 1 and 4 end to end through the real request path:
        CHECK_FOR_UPDATES is false, a Later wait runs, and the state file
        stays byte for byte as it was."""
        import time

        controller = _controller()
        controller.config_service.get.return_value = False
        path = tmp_path / update_check.STATE_FILE_NAME
        now = time.time()
        update_check.save_state(
            path,
            update_check.UpdateCheckState(
                last_check=now - 60, notified_version="2.0.0", notified_at=now - 60
            ),
        )
        before = path.read_bytes()
        with patch.object(update_check, "state_file_path", return_value=path), \
             patch.object(update_check, "get_app_version", return_value="1.4.0"), \
             _urlopen() as urlopen:
            await _start(controller)
        _assert_the_automatic_request(urlopen, "1.4.0")
        assert path.read_bytes() == before
        assert [m["status"] for m in _gui_messages(controller)] == ["newer"]


# ---------------------------------------------------------------------------
# The About Wheelhouse window
# ---------------------------------------------------------------------------


@pytest.fixture
def window(about_window_off_screen):
    from about_window import AboutWindow

    win = AboutWindow(version="1.4.0")
    _assert_off_screen(win)
    yield win
    win.close()
    win.deleteLater()


class TestTheAboutWindow:
    """Acceptance items 1, 3 and 5, the window half."""

    def test_the_window_names_the_program_and_the_version(self, window):
        assert window.windowTitle() == "About Wheelhouse"
        text = window.about_text()
        assert "Wheelhouse" in text
        assert "Version 1.4.0" in text
        assert 'say "open voice access help"' in text
        assert not window.isModal()

    def test_the_window_shows_a_link_to_the_website(self, window):
        """Acceptance item 8: the label shows the address and links to it."""
        from PySide6.QtWidgets import QLabel

        label = window.findChild(QLabel, "about_website")
        assert label is not None
        assert "https://wheelhouse-project.org" in re.sub(
            r"<[^>]+>", "", label.text()
        )
        assert re.findall(r'href="([^"]*)"', label.text()) == [
            "https://wheelhouse-project.org"
        ]
        assert not label.openExternalLinks()

    def test_a_click_on_the_website_link_opens_the_address_in_the_browser(
        self, window
    ):
        """Acceptance item 8: the open call is patched, no browser starts."""
        from PySide6.QtWidgets import QLabel

        label = window.findChild(QLabel, "about_website")
        assert label is not None
        with patch("about_window.open_in_browser") as open_in_browser:
            label.linkActivated.emit("https://wheelhouse-project.org")
        open_in_browser.assert_called_once_with("https://wheelhouse-project.org")

    def test_it_has_a_check_button_and_no_update_button_at_first(self, window):
        window.show()
        assert window.check_button.text() == "Check for updates"
        assert window.check_button.isVisible()
        assert not window.update_button.isVisible()
        assert window.status_text() == ""

    def test_the_check_button_asks_once_and_shows_that_it_is_checking(self, window):
        window.show()
        asked = []
        window.check_requested.connect(lambda: asked.append(1))
        window.check_button.click()
        assert asked == [1]
        assert window.status_text() == CHECKING_TEXT
        assert not window.check_button.isEnabled()
        # A second press while the check runs does nothing.
        window.check_button.click()
        assert asked == [1]

    def test_a_newer_release_shows_the_version_and_update_now(self, window):
        window.show()
        window.start_check()
        window.show_result({
            "action": RESULT_ACTION, "status": "newer",
            "installed": "1.4.0", "version": "2.0.0", "tag": "v2.0.0",
        })
        assert window.status_text() == NEWER_TEXT
        assert window.update_button.isVisible()
        assert window.update_button.text() == "Update now"
        assert window.check_button.isEnabled()
        chosen = []
        window.update_requested.connect(lambda v, t: chosen.append((v, t)))
        window.update_button.click()
        assert chosen == [("2.0.0", "v2.0.0")]

    def test_a_second_update_now_press_sends_nothing(self, window):
        """The About window sends one update_now per attempt.

        The update notice closes when pressed; the About window stays
        open, so its Update now button must stop after one press. A new
        "newer" result enables it again.
        """
        newer = {
            "action": RESULT_ACTION, "status": "newer",
            "installed": "1.4.0", "version": "2.0.0", "tag": "v2.0.0",
        }
        window.show()
        window.start_check()
        window.show_result(newer)
        chosen = []
        window.update_requested.connect(lambda v, t: chosen.append((v, t)))
        window.update_button.click()
        window.update_button.click()
        assert chosen == [("2.0.0", "v2.0.0")]
        assert not window.update_button.isEnabled()
        window.start_check()
        window.show_result(newer)
        assert window.update_button.isEnabled()
        window.update_button.click()
        assert chosen == [("2.0.0", "v2.0.0"), ("2.0.0", "v2.0.0")]

    def test_the_same_release_says_this_is_the_newest(self, window):
        window.show()
        window.start_check()
        window.show_result({
            "action": RESULT_ACTION, "status": "current",
            "installed": "1.4.0", "version": "1.4.0", "tag": "v1.4.0",
        })
        assert window.status_text() == CURRENT_TEXT
        assert not window.update_button.isVisible()
        assert window.check_button.isEnabled()

    def test_github_not_reached(self, window):
        window.show()
        window.start_check()
        window.show_result({"action": RESULT_ACTION, "status": "failed"})
        assert window.status_text() == FAILED_TEXT
        assert not window.update_button.isVisible()
        assert window.check_button.isEnabled()

    def test_a_copy_without_a_version(self, window):
        window.show()
        window.start_check()
        window.show_result({
            "action": RESULT_ACTION, "status": "unversioned",
            "installed": "unknown", "version": "2.0.0", "tag": "v2.0.0",
        })
        assert window.status_text() == UNVERSIONED_TEXT
        assert not window.update_button.isVisible()

    def test_update_now_hides_again_after_a_later_current_result(self, window):
        window.show()
        window.show_result({
            "status": "newer", "installed": "1.4.0",
            "version": "2.0.0", "tag": "v2.0.0",
        })
        window.show_result({"status": "current", "installed": "1.4.0"})
        assert not window.update_button.isVisible()

    @pytest.mark.parametrize("message", [
        {"status": "newer", "installed": "1.4.0"},
        {"status": "newer", "installed": "1.4.0", "version": "2.0.0", "tag": ""},
        {"status": "something else"},
        {},
    ])
    def test_a_malformed_result_shows_the_failure_text(self, window, message):
        window.show()
        window.show_result(message)
        assert window.status_text() == FAILED_TEXT
        assert not window.update_button.isVisible()

    def test_the_newer_text_is_the_update_notice_title(self, window):
        import gui

        with patch.object(gui, "UPDATE_NOTICE_TITLE", "Title {version} here"):
            window.show_result({
                "status": "newer", "installed": "1.4.0",
                "version": "2.0.0", "tag": "v2.0.0",
            })
        assert window.status_text() == "Title 2.0.0 here"


# ---------------------------------------------------------------------------
# GuiManager
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(mock_editor_window, about_window_off_screen):
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
        mgr.shutdown_event.is_set.return_value = False
        yield mgr
        win = getattr(mgr, "_about_window", None)
        if win is not None:
            _assert_off_screen(win)
            win.close()
            win.deleteLater()


def _sent(manager):
    calls = manager.commands_to_logic_queue.put_nowait.call_args_list
    return [call.args[0] for call in calls]


class TestTheGuiWiring:
    """Acceptance items 1, 2, 3 and 5, the GUI process half."""

    def test_about_opens_one_window_and_reuses_it(self, manager):
        with patch("gui.get_app_version", return_value="9.9.9"):
            manager.show_about_dialog()
            first = manager._about_window
            manager.show_about_dialog()
        assert manager._about_window is first
        assert first.isVisible()
        assert "Version 9.9.9" in first.about_text()

    def test_the_button_sends_one_check_for_updates(self, manager):
        manager.show_about_dialog()
        manager._about_window.check_button.click()
        assert _sent(manager) == [{"action": "check_for_updates"}]

    def test_a_full_queue_shows_the_failure_at_once(self, manager):
        from queue import Full

        manager.commands_to_logic_queue.put_nowait.side_effect = Full()
        manager.show_about_dialog()
        manager._about_window.check_button.click()
        assert manager._about_window.status_text() == FAILED_TEXT
        assert manager._about_window.check_button.isEnabled()

    def test_the_result_reaches_the_window(self, manager):
        manager.show_about_dialog()
        manager._about_window.start_check()
        result = {
            "action": RESULT_ACTION, "status": "current",
            "installed": "1.4.0", "version": "1.4.0", "tag": "v1.4.0",
        }
        manager.state_from_logic_queue.get_nowait.side_effect = [result, Empty()]
        manager._check_queues_and_events()
        assert manager._about_window.status_text() == CURRENT_TEXT

    def test_a_result_without_a_window_is_dropped(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = [
            {"action": RESULT_ACTION, "status": "failed"}, Empty(),
        ]
        manager._check_queues_and_events()  # must not raise
        assert getattr(manager, "_about_window", None) is None

    def test_update_now_in_the_window_runs_the_existing_update_path(self, manager):
        manager.show_about_dialog()
        manager._about_window.show_result({
            "action": RESULT_ACTION, "status": "newer",
            "installed": "1.4.0", "version": "2.0.0", "tag": "v2.0.0",
        })
        manager._about_window.update_button.click()
        assert _sent(manager) == [
            {"action": "update_now", "version": "2.0.0", "tag": "v2.0.0"}
        ]

    def test_a_failed_update_lets_update_now_run_again(self, manager):
        # wh-about-check-updates.2.1: after update_failed the user can press
        # Update now again without a fresh check.
        manager.show_about_dialog()
        manager._about_window.show_result({
            "action": RESULT_ACTION, "status": "newer",
            "installed": "1.4.0", "version": "2.0.0", "tag": "v2.0.0",
        })
        manager._about_window.update_button.click()
        assert not manager._about_window.update_button.isEnabled()
        # The user closes the window while the update runs.
        manager._about_window.close()
        manager.state_from_logic_queue.get_nowait.side_effect = [
            {"action": "update_failed"}, Empty(),
        ]
        with patch.object(manager, "_show_update_failed_notice") as notice:
            manager._check_queues_and_events()
        notice.assert_called_once()
        manager.show_about_dialog()
        assert manager._about_window.update_button.isEnabled()
        manager._about_window.update_button.click()
        assert _sent(manager) == [
            {"action": "update_now", "version": "2.0.0", "tag": "v2.0.0"},
            {"action": "update_now", "version": "2.0.0", "tag": "v2.0.0"},
        ]

    def test_a_failed_update_shows_no_update_now_without_a_newer_result(
        self, manager
    ):
        manager.show_about_dialog()
        manager._about_window.show_result({
            "action": RESULT_ACTION, "status": "current",
            "installed": "1.4.0", "version": "1.4.0", "tag": "v1.4.0",
        })
        manager.state_from_logic_queue.get_nowait.side_effect = [
            {"action": "update_failed"}, Empty(),
        ]
        with patch.object(manager, "_show_update_failed_notice"):
            manager._check_queues_and_events()
        assert manager._about_window.update_button.isHidden()

    def test_a_full_queue_on_update_now_lets_it_run_again(self, manager):
        from queue import Full

        manager.show_about_dialog()
        manager._about_window.show_result({
            "action": RESULT_ACTION, "status": "newer",
            "installed": "1.4.0", "version": "2.0.0", "tag": "v2.0.0",
        })
        manager.commands_to_logic_queue.put_nowait.side_effect = Full()
        manager._about_window.update_button.click()
        assert manager._about_window.update_button.isEnabled()

    def test_the_voice_message_opens_the_window_from_the_event_loop(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = [
            {"action": "check_for_updates_by_voice"}, Empty(),
        ]
        with patch("gui.QTimer") as timer:
            manager._check_queues_and_events()
        timer.singleShot.assert_called_once_with(0, manager._open_about_and_check)

    def test_the_voice_path_opens_the_window_and_sends_one_check(self, manager):
        manager._open_about_and_check()
        assert manager._about_window.isVisible()
        assert manager._about_window.status_text() == CHECKING_TEXT
        assert _sent(manager) == [{"action": "check_for_updates"}]

    def test_the_voice_path_reuses_an_open_window(self, manager):
        manager.show_about_dialog()
        first = manager._about_window
        manager._open_about_and_check()
        assert manager._about_window is first
        assert _sent(manager) == [{"action": "check_for_updates"}]


# ---------------------------------------------------------------------------
# The voice command
# ---------------------------------------------------------------------------


def _rows():
    return tomllib.loads(_SYSTEM_PATTERNS.read_text(encoding="utf-8"))["pattern"]


def _first_owner(form):
    for row in _rows():
        source = row["pattern"]
        if not source.startswith("^"):
            continue
        if re.compile(source, re.IGNORECASE).fullmatch(form):
            return row.get("doc_id")
    return None


class TestTheVoiceCommand:
    """Acceptance item 2."""

    def test_check_for_updates_resolves_as_a_whole_utterance(self, tmp_path):
        from speech.pattern_catalog import PatternCatalog

        catalog = PatternCatalog(
            str(_SYSTEM_PATTERNS), user_patterns_file=str(tmp_path / "user.toml")
        )
        entries = [
            p for p in catalog.get_all_patterns()
            if any(a.get("function") == "check_for_updates" for a in p["actions"])
        ]
        assert len(entries) == 1
        entry = entries[0]
        compiled = entry["compiled_pattern"]
        assert compiled.fullmatch("check for updates")
        assert compiled.fullmatch("check for update")
        assert not compiled.fullmatch("please check for updates")
        assert entry["whole_utterance_only"] is True
        assert entry["doc_id"] == "check-for-updates"
        assert entry["actions"] == [{"function": "check_for_updates"}]

    @pytest.mark.parametrize("form", ["check for updates", "Check for update"])
    def test_the_spoken_forms_reach_this_row_first(self, form):
        assert _first_owner(form) == "check-for-updates"

    def test_the_action_asks_the_gui_to_open_the_window_and_check(self):
        from speech.actions import ActionFunctions

        handler = MagicMock()
        funcs = ActionFunctions(handler)
        assert "check_for_updates" in funcs.get_functions()
        asyncio.run(funcs.check_for_updates())
        queue = handler.logic_controller.state_manager.state_to_gui_queue
        queue.put_nowait.assert_called_once_with(
            {"action": "check_for_updates_by_voice"}
        )

    def test_the_action_catalog_describes_it(self):
        from speech import action_catalog

        names = [entry["name"] for entry in action_catalog.ACTION_CATALOG]
        assert names.count("check_for_updates") == 1
