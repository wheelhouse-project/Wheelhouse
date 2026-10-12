"""The update notice, from the Logic task to the request for the update
(wh-release-update-check, stage B).

update_check.py decides whether a newer release exists (test_update_check.py).
This file covers the part that shows it:

  * Logic: the background task, the message to the GUI, the startup
    registration, and the handler that receives "Update now".
  * GUI: the corner notice on its own GrantPromptToast, its two buttons,
    the command back to Logic, and its place above any prompt notice that
    is already on screen (boss ruling Q5).

The method follows test_parakeet_download_offer.py: a MagicMock(spec=
LogicController) for Logic, and a GuiManager with its Qt pieces patched for
the GUI.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import queue as queue_module
import sys
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest
from contextlib import contextmanager
from PySide6.QtCore import QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import update_check  # noqa: E402

# The GUI tests build GuiManager and real toast widgets; see the same
# marker in test_parakeet_download_offer.py (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

VERSION = "1.2.3"
TAG = "v1.2.3"
NOTICE = {"action": "update_available", "version": VERSION, "tag": TAG}

FAILURE_TITLE = "Wheelhouse update"
FAILURE_BODY = (
    "The update could not start. Try again later, or run the "
    "installer from the website."
)

NOTICE_TITLE = "Wheelhouse 1.2.3 is available"
NOTICE_BODY = (
    "A newer version of Wheelhouse is available. Update now downloads it, "
    "closes Wheelhouse, and runs the installer. Later hides this notice. "
    "Wheelhouse can show it again one week after this notice appeared."
)


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


def _controller():
    """A stand-in ``self`` for LogicController methods."""
    from main import LogicController

    state_manager = MagicMock()
    state_manager.state_to_gui_queue = Queue()
    controller = MagicMock(spec=LogicController)
    controller.config_service = MagicMock()
    controller.state_manager = state_manager
    return controller


def _gui_messages(controller):
    messages = []
    gui_queue = controller.state_manager.state_to_gui_queue
    while True:
        try:
            messages.append(gui_queue.get_nowait())
        except Empty:
            return messages


class TestLogicSendsTheNotice:
    def test_a_newer_version_puts_one_message_on_the_gui_queue(self):
        from main import _send_update_notice

        controller = _controller()
        _send_update_notice(controller.state_manager, VERSION, TAG)
        assert _gui_messages(controller) == [NOTICE]

    def test_a_queue_that_refuses_the_message_raises(self):
        # update_check.run_update_check writes no notice record at all
        # since wh-blocked-mic-message.1.2, so the version returns at the
        # next check. This message must raise rather than fail silently,
        # because the caller then logs the failure instead of believing
        # that the notice reached the GUI process.
        from main import _send_update_notice

        state_manager = MagicMock()
        state_manager.state_to_gui_queue = Queue(maxsize=1)
        state_manager.state_to_gui_queue.put_nowait("full")
        with pytest.raises(queue_module.Full):
            _send_update_notice(state_manager, VERSION, TAG)

    @pytest.mark.asyncio
    async def test_the_task_gives_the_module_the_config_and_a_notify_for_the_queue(self):
        from main import LogicController

        controller = _controller()
        seen = {}

        async def fake_run_update_check(config_service, notify, **kwargs):
            seen["config_service"] = config_service
            notify(VERSION, TAG)

        with patch.object(update_check, "run_update_check", fake_run_update_check):
            await LogicController._run_update_check(controller)

        assert seen["config_service"] is controller.config_service
        assert _gui_messages(controller) == [NOTICE]


class TestStartup:
    def test_the_update_check_is_registered_beside_the_other_startup_task(self):
        from main import LogicController

        controller = _controller()
        coroutines = LogicController._startup_task_coroutines(controller)
        for coroutine in coroutines.values():
            if inspect.iscoroutine(coroutine):
                coroutine.close()  # the mock builds it; nothing awaits it
        assert set(coroutines) == {
            "PeriodicStateUpdate", "UpdateCheck", "FirstRunNotice",
        }

    def test_main_creates_every_registered_task_through_the_error_handler(self):
        # The loop in LogicController.main cannot run in a unit test (it
        # starts the services). This pins the wiring in its source instead.
        from main import LogicController

        source = inspect.getsource(LogicController.main)
        assert "self._startup_task_coroutines().items()" in source
        assert "self.create_task_with_error_handling(coro, name)" in source

    @pytest.mark.asyncio
    async def test_a_waiting_update_check_does_not_block_other_tasks(self):
        from main import LogicController

        controller = _controller()
        release = asyncio.Event()

        async def fake_run_update_check(config_service, notify, **kwargs):
            await release.wait()  # the real module sleeps 60 s first

        other_ran = []

        async def other_task():
            other_ran.append(True)

        with patch.object(update_check, "run_update_check", fake_run_update_check):
            check = asyncio.ensure_future(
                LogicController._run_update_check(controller)
            )
            await asyncio.sleep(0)
            await asyncio.ensure_future(other_task())
            assert other_ran == [True]
            assert not check.done()
            release.set()
            await check


class TestTheNoticeIsRecordedWhenTheGuiReportsIt:
    """The Logic process writes the shown-notice record on the GUI's report
    (wh-blocked-mic-message.1.2), as it writes the first-run marker on
    ``first_run_notice_shown``."""

    def test_the_update_notice_shown_command_reaches_the_handler(self):
        from main import LogicController

        controller = _controller()
        command = {"action": "update_notice_shown", "version": VERSION}
        handlers = LogicController._build_gui_handler_map(controller, command)
        assert "update_notice_shown" in handlers, (
            "the command map has no update_notice_shown entry"
        )
        handlers["update_notice_shown"]()
        controller._handle_update_notice_shown.assert_called_once_with(VERSION)

    @pytest.mark.asyncio
    async def test_the_handler_records_the_version_and_the_time(self, tmp_path):
        import time

        from main import LogicController

        path = tmp_path / "state.toml"
        update_check.save_state(
            path, update_check.UpdateCheckState(last_check=555.0)
        )
        controller = _controller()
        before = time.time()
        with patch.object(update_check, "state_file_path", return_value=path):
            await LogicController._handle_update_notice_shown(controller, VERSION)
        after = time.time()
        state = update_check.load_state(path)
        assert state.notified_version == VERSION
        assert before <= state.notified_at <= after
        assert state.last_check == 555.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["", None, 5, ["1.2.3"]])
    async def test_a_command_without_a_version_records_nothing(
        self, tmp_path, caplog, version
    ):
        from main import LogicController

        path = tmp_path / "state.toml"
        controller = _controller()
        with patch.object(update_check, "state_file_path", return_value=path), \
             caplog.at_level(logging.WARNING):
            await LogicController._handle_update_notice_shown(controller, version)
        assert not path.exists()
        assert any("update_notice_shown" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_failed_write_logs_one_line_and_does_not_raise(
        self, tmp_path, caplog
    ):
        from main import LogicController

        controller = _controller()
        with patch.object(
            update_check, "state_file_path", return_value=tmp_path / "state.toml"
        ), patch.object(update_check, "save_state", return_value=False), \
             caplog.at_level(logging.WARNING):
            await LogicController._handle_update_notice_shown(controller, VERSION)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert VERSION in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_an_exception_in_the_write_is_logged_not_raised(
        self, tmp_path, caplog
    ):
        from main import LogicController

        controller = _controller()
        with patch.object(
            update_check, "state_file_path", return_value=tmp_path / "state.toml"
        ), patch.object(
            update_check, "record_notice_shown", side_effect=OSError("disk gone")
        ), caplog.at_level(logging.WARNING):
            await LogicController._handle_update_notice_shown(controller, VERSION)
        assert any("disk gone" in r.getMessage() for r in caplog.records)


class TestTheRequestToUpdate:
    def test_the_gui_command_reaches_the_handler(self):
        from main import LogicController

        controller = _controller()
        command = {"action": "update_now", "version": VERSION, "tag": TAG}
        handlers = LogicController._build_gui_handler_map(controller, command)
        handlers["update_now"]()
        controller._handle_update_now.assert_called_once_with(VERSION, TAG)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


@pytest.fixture
def manager():
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
        # A MagicMock event reports "set", and the queue check then
        # shuts down before it reads the queue.
        mgr.shutdown_event.is_set.return_value = False
        yield mgr
        for name in ("_update_notice_toast", "_parakeet_offer_toast",
                     "_grant_prompt_toast", "_microphone_blocked_toast"):
            toast = getattr(mgr, name, None)
            if toast is not None and hasattr(toast, "deleteLater"):
                toast.close()
                toast.deleteLater()


def _sent(manager):
    """Every command the GUI put on its queue to Logic, in order."""
    calls = manager.commands_to_logic_queue.put_nowait.call_args_list
    return [call.args[0] for call in calls]


def _report(version):
    """The command that tells Logic the update notice is on the screen."""
    return {"action": "update_notice_shown", "version": version}


class TestTheGuiNotice:
    def test_the_notice_shows_the_version_and_both_buttons_and_never_times_out(
        self, manager
    ):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_update_notice(NOTICE)
        toast_cls.return_value.show_prompt.assert_called_once_with(
            title=NOTICE_TITLE,
            body=NOTICE_BODY,
            yes_label="Update now",
            no_label="Later",
            lifetime_ms=None,
        )

    def test_the_real_notice_is_visible_with_both_labels_and_no_timer(self, manager):
        manager._show_update_notice(NOTICE)
        toast = manager._update_notice_toast
        assert toast.isVisible()
        assert toast._yes_button.text() == "Update now"
        assert toast._no_button.text() == "Later"
        assert not toast._lifetime_timer.isActive()

    def test_the_notice_has_its_own_toast_and_leaves_the_parakeet_one_alone(
        self, manager
    ):
        manager._show_update_notice(NOTICE)
        assert manager._update_notice_toast is not None
        assert manager._update_notice_toast is not manager._parakeet_offer_toast
        assert manager._update_notice_toast is not manager._grant_prompt_toast
        assert manager._parakeet_offer_toast is None

    def test_update_now_sends_the_command_with_the_tag(self, manager):
        manager._show_update_notice(NOTICE)
        manager._update_notice_toast._yes_button.click()
        assert _sent(manager) == [
            _report(VERSION),
            {"action": "update_now", "version": VERSION, "tag": TAG},
        ]

    def test_later_sends_nothing_and_closes_the_notice(self, manager):
        # The Logic process recorded the notice when the GUI reported that
        # it appeared, so Later, the X button and no answer all start the
        # same wait. Later itself sends no further command.
        manager._show_update_notice(NOTICE)
        manager._update_notice_toast._no_button.click()
        assert not manager._update_notice_toast.isVisible()
        assert _sent(manager) == [_report(VERSION)]

    def test_the_x_button_sends_nothing(self, manager):
        manager._show_update_notice(NOTICE)
        manager._update_notice_toast._dismiss_button.click()
        assert _sent(manager) == [_report(VERSION)]

    def test_a_second_notice_reuses_the_toast_and_the_click_names_its_tag(
        self, manager
    ):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_update_notice(NOTICE)
            manager._show_update_notice(
                {"action": "update_available", "version": "1.3.0", "tag": "v1.3.0"}
            )
        toast_cls.assert_called_once()
        assert toast_cls.return_value.show_prompt.call_count == 2
        manager._on_update_notice_accepted()
        assert _sent(manager)[-1] == {
            "action": "update_now", "version": "1.3.0", "tag": "v1.3.0",
        }

    @pytest.mark.parametrize("message", [
        {"action": "update_available", "version": VERSION},
        {"action": "update_available", "tag": TAG},
        {"action": "update_available", "version": "", "tag": TAG},
        {"action": "update_available", "version": VERSION, "tag": 5},
    ])
    def test_a_message_without_a_version_and_a_tag_shows_nothing(
        self, manager, message, caplog
    ):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls, \
             caplog.at_level(logging.WARNING):
            manager._show_update_notice(message)
        toast_cls.assert_not_called()
        assert any("update_available" in r.getMessage() for r in caplog.records)

    def test_the_action_reaches_the_handler(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = [NOTICE, Empty()]
        with patch.object(manager, "_show_update_notice") as show:
            manager._check_queues_and_events()
        show.assert_called_once_with(NOTICE)


class TestTheFailureNotice:
    """The corner notice that follows a failed update (boss ruling)."""

    def test_the_action_reaches_the_handler(self, manager):
        message = {"action": "update_failed"}
        manager.state_from_logic_queue.get_nowait.side_effect = [message, Empty()]
        with patch.object(manager, "_show_update_failed_notice") as show:
            manager._check_queues_and_events()
        show.assert_called_once_with(message)

    def test_the_notice_has_the_exact_text_and_closes_after_a_while(self, manager):
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_update_failed_notice({"action": "update_failed"})
        (call,) = toast_cls.return_value.show_prompt.call_args_list
        assert call.kwargs["title"] == FAILURE_TITLE
        assert call.kwargs["body"] == FAILURE_BODY
        assert isinstance(call.kwargs["lifetime_ms"], int)

    def test_the_real_notice_shows_only_the_close_button(self, manager):
        manager._show_update_failed_notice({"action": "update_failed"})
        toast = manager._update_notice_toast
        assert toast.isVisible()
        assert toast._body_label.text() == FAILURE_BODY
        assert toast._yes_button.isHidden()
        assert toast._no_button.isHidden()
        assert not toast._dismiss_button.isHidden()

    def test_the_x_button_closes_it_and_sends_nothing(self, manager):
        manager._show_update_failed_notice({"action": "update_failed"})
        manager._update_notice_toast._dismiss_button.click()
        assert not manager._update_notice_toast.isVisible()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_a_later_update_notice_brings_both_buttons_back(self, manager):
        manager._show_update_failed_notice({"action": "update_failed"})
        manager._show_update_notice(NOTICE)
        toast = manager._update_notice_toast
        assert toast._yes_button.text() == "Update now"
        assert not toast._yes_button.isHidden()
        assert not toast._no_button.isHidden()
        toast._yes_button.click()
        assert _sent(manager) == [
            _report(VERSION),
            {"action": "update_now", "version": VERSION, "tag": TAG},
        ]


class TestTheBlockedMicrophoneNotice:
    """The notice that stays on the screen when Windows blocks the
    microphone (wh-blocked-mic-message). It has its own toast, so it
    never removes the update notice and the update notice never removes
    it. Its body is the text that the provider sent, word for word: no
    file in this service keeps a copy of that sentence, so these tests
    use a sentence of their own."""

    TEXT = "Sentence from the provider, kept nowhere in the GUI. Open X."
    MESSAGE = {"action": "microphone_blocked", "message": TEXT}

    def test_the_action_reaches_the_handler(self, manager):
        manager.state_from_logic_queue.get_nowait.side_effect = [
            self.MESSAGE, Empty(),
        ]
        with patch.object(manager, "_show_microphone_blocked_notice") as show:
            manager._check_queues_and_events()
        show.assert_called_once_with(self.MESSAGE)

    def test_the_title_is_fixed_and_the_body_is_the_text_of_the_message(
        self, manager
    ):
        import gui

        assert gui.MICROPHONE_BLOCKED_TITLE == "Windows is blocking your microphone"
        with patch("grant_prompt_toast.GrantPromptToast") as toast_cls:
            manager._show_microphone_blocked_notice(self.MESSAGE)
        (call,) = toast_cls.return_value.show_prompt.call_args_list
        assert call.kwargs["title"] == gui.MICROPHONE_BLOCKED_TITLE
        assert call.kwargs["body"] == self.TEXT
        assert call.kwargs["lifetime_ms"] is None

    def test_the_gui_keeps_no_copy_of_the_sentence(self):
        import gui

        assert not hasattr(gui, "MICROPHONE_BLOCKED_BODY")

    def test_the_body_on_the_screen_is_the_text_that_came_through_the_queue(
        self, manager
    ):
        manager.state_from_logic_queue.get_nowait.side_effect = [
            self.MESSAGE, Empty(),
        ]
        manager._check_queues_and_events()
        toast = manager._microphone_blocked_toast
        assert toast.isVisible()
        assert toast._body_label.text() == self.TEXT

    @pytest.mark.parametrize("message", [
        {"action": "microphone_blocked"},
        {"action": "microphone_blocked", "message": ""},
        {"action": "microphone_blocked", "message": 7},
    ])
    def test_a_message_without_text_shows_nothing(self, manager, message):
        manager._show_microphone_blocked_notice(message)
        assert manager._microphone_blocked_toast is None
        assert manager._update_notice_toast is None

    def test_the_real_notice_stays_with_only_the_close_button(self, manager):
        manager._show_microphone_blocked_notice(self.MESSAGE)
        toast = manager._microphone_blocked_toast
        assert toast.isVisible()
        assert not toast._lifetime_timer.isActive()
        assert toast._yes_button.isHidden()
        assert toast._no_button.isHidden()
        assert not toast._dismiss_button.isHidden()

    def test_it_uses_a_toast_of_its_own(self, manager):
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        assert manager._microphone_blocked_toast is not manager._update_notice_toast

    def test_the_failure_notice_keeps_its_timer_and_this_one_has_none(
        self, manager
    ):
        manager._show_update_failed_notice({"action": "update_failed"})
        manager._show_microphone_blocked_notice(self.MESSAGE)
        assert manager._update_notice_toast._lifetime_timer.isActive()
        assert not manager._microphone_blocked_toast._lifetime_timer.isActive()

    def test_the_x_button_closes_it_and_sends_nothing(self, manager):
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._microphone_blocked_toast._dismiss_button.click()
        assert not manager._microphone_blocked_toast.isVisible()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_the_x_button_of_the_update_notice_leaves_it_on_the_screen(
        self, manager
    ):
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._update_notice_toast._dismiss_button.click()
        assert manager._microphone_blocked_toast.isVisible()
        assert not manager._update_notice_toast.isVisible()


class TestTheBlockedMicrophoneNoticeAndTheUpdateNoticeTogether:
    """Both notices stay on the screen, each with its own title and body,
    in either order of arrival (boss ruling 3, wh-blocked-mic-message)."""

    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE
    TEXT = TestTheBlockedMicrophoneNotice.TEXT

    def _assert_both_stay(self, manager):
        import gui

        update = manager._update_notice_toast
        mic = manager._microphone_blocked_toast
        assert update.isVisible() and mic.isVisible()
        assert update._title_label.text() == NOTICE_TITLE
        assert update._body_label.text() == NOTICE_BODY
        assert update._yes_button.text() == "Update now"
        assert not update._yes_button.isHidden()
        assert not update._no_button.isHidden()
        assert mic._title_label.text() == gui.MICROPHONE_BLOCKED_TITLE
        assert mic._body_label.text() == self.TEXT
        assert mic._yes_button.isHidden() and mic._no_button.isHidden()
        assert not update.geometry().intersects(mic.geometry())

    def test_the_update_notice_first_then_the_microphone_notice(
        self, manager, big_screen
    ):
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        self._assert_both_stay(manager)

    def test_the_microphone_notice_first_then_the_update_notice(
        self, manager, big_screen
    ):
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        self._assert_both_stay(manager)

    def test_the_failure_notice_does_not_remove_the_microphone_notice(
        self, manager, big_screen
    ):
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_failed_notice({"action": "update_failed"})
        update = manager._update_notice_toast
        mic = manager._microphone_blocked_toast
        assert update._title_label.text() == FAILURE_TITLE
        assert update._body_label.text() == FAILURE_BODY
        assert mic._body_label.text() == self.TEXT
        assert update.isVisible() and mic.isVisible()
        assert not update.geometry().intersects(mic.geometry())

    def test_the_microphone_notice_sits_between_the_update_notice_and_a_prompt_notice(
        self, manager, big_screen
    ):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        offer = manager._parakeet_offer_toast
        update = manager._update_notice_toast
        mic = manager._microphone_blocked_toast
        assert _bottom(mic) <= offer.y()
        assert _bottom(update) <= mic.y()
        assert not update.geometry().intersects(mic.geometry())
        assert not mic.geometry().intersects(offer.geometry())

    def test_a_prompt_notice_that_arrives_last_leaves_no_two_notices_overlapping(
        self, manager, big_screen
    ):
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager.state_from_logic_queue.get_nowait.side_effect = [
            LAUNCH_OFFER, Empty(),
        ]
        manager._check_queues_and_events()
        toasts = [
            manager._parakeet_offer_toast,
            manager._microphone_blocked_toast,
            manager._update_notice_toast,
        ]
        assert all(toast.isVisible() for toast in toasts)
        for i, first in enumerate(toasts):
            for second in toasts[i + 1:]:
                assert not first.geometry().intersects(second.geometry())
        assert _bottom(toasts[1]) <= toasts[0].y()
        assert _bottom(toasts[2]) <= toasts[1].y()

    def test_the_parakeet_offer_does_not_move(self, manager, big_screen):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        offer = manager._parakeet_offer_toast
        before = offer.geometry()
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        assert offer.geometry() == before


# ---------------------------------------------------------------------------
# Placement above a visible prompt notice (boss ruling Q5)
# ---------------------------------------------------------------------------

SCREEN_HEIGHT = 1080
LAUNCH_OFFER = {"action": "parakeet_model_offer", "kind": "launch"}
GRANT_EVENT_TITLE = "Always type into Notepad when you do this?"
# A schema-valid grant-prompt message, in the form that
# tests/test_gui_grant_prompt.py uses. The placement test below sends it
# through the queue, so the stack call in that dispatch branch is guarded.
GRANT_PROMPT_EVENT = {
    "action": "text_target_grant_prompt",
    "process_name": "notepad.exe",
    "class_name": "Edit",
    "control_type": "Edit",
    "app_friendly_name": "Notepad",
    "count": 3,
}


@pytest.fixture
def big_screen():
    """A 1920 x 1080 primary screen, so the placement does not depend on the
    machine that runs the test."""
    screen = MagicMock()
    screen.availableGeometry.return_value = QRect(0, 0, 1920, SCREEN_HEIGHT)
    with patch.object(QApplication, "primaryScreen", return_value=screen):
        yield screen


def _show_grant_prompt(manager):
    from grant_prompt_toast import GrantPromptToast

    manager._grant_prompt_toast = GrantPromptToast()
    manager._grant_prompt_toast.show_prompt(title=GRANT_EVENT_TITLE, body="Body.")
    return manager._grant_prompt_toast


def _bottom(toast):
    return toast.y() + toast.height()


class TestPlacementAboveAPromptNotice:
    def test_alone_the_notice_sits_where_every_prompt_notice_sits(
        self, manager, big_screen
    ):
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        reference = _show_grant_prompt(manager)
        # The widget's own anchor is the reference: same x, same bottom.
        assert update.x() + update.width() == reference.x() + reference.width()
        assert _bottom(update) == _bottom(reference)

    def test_it_sits_above_a_visible_parakeet_offer(self, manager, big_screen):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        offer = manager._parakeet_offer_toast
        offer_y = offer.y()
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        assert 0 <= offer.y() - _bottom(update) <= 16
        assert update.y() >= 0
        assert offer.y() == offer_y  # the Parakeet notice does not move
        assert update.isVisible() and offer.isVisible()

    def test_it_sits_above_a_visible_grant_prompt(self, manager, big_screen):
        grant = _show_grant_prompt(manager)
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        assert 0 <= grant.y() - _bottom(update) <= 16

    def test_it_sits_above_the_highest_of_two_visible_notices(
        self, manager, big_screen
    ):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        grant = _show_grant_prompt(manager)
        manager._show_update_notice(NOTICE)
        offer = manager._parakeet_offer_toast
        update = manager._update_notice_toast
        assert _bottom(update) <= min(offer.y(), grant.y())

    def test_a_closed_notice_does_not_push_it_up(self, manager, big_screen):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._parakeet_offer_toast.close()
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        reference = _show_grant_prompt(manager)
        assert _bottom(update) == _bottom(reference)

    def test_a_parakeet_offer_that_arrives_later_still_sits_below_it(
        self, manager, big_screen
    ):
        manager._show_update_notice(NOTICE)
        manager.state_from_logic_queue.get_nowait.side_effect = [
            LAUNCH_OFFER, Empty(),
        ]
        manager._check_queues_and_events()
        update = manager._update_notice_toast
        offer = manager._parakeet_offer_toast
        assert offer.isVisible()
        assert _bottom(update) <= offer.y()

    def test_a_grant_prompt_that_arrives_later_still_sits_below_it(
        self, manager, big_screen
    ):
        # The message goes through the queue, not through a direct call,
        # so this test fails if the grant-prompt dispatch branch loses its
        # stack call.
        manager._show_update_notice(NOTICE)
        manager.state_from_logic_queue.get_nowait.side_effect = [
            GRANT_PROMPT_EVENT, Empty(),
        ]
        manager._check_queues_and_events()
        grant = manager._grant_prompt_toast
        assert grant is not None and grant.isVisible()
        assert _bottom(manager._update_notice_toast) <= grant.y()

    def test_on_a_short_screen_the_notice_stays_on_the_screen(self, manager):
        screen = MagicMock()
        screen.availableGeometry.return_value = QRect(0, 0, 1920, 250)
        with patch.object(QApplication, "primaryScreen", return_value=screen):
            manager._show_parakeet_model_offer(LAUNCH_OFFER)
            manager._show_update_notice(NOTICE)
        assert manager._update_notice_toast.y() >= 0


# ---------------------------------------------------------------------------
# Placement on the common laptop screens (wh-blocked-mic-message)
# ---------------------------------------------------------------------------

# 1366x768 and 1536x824 are the screens on which three or four notices are
# taller than the screen, so the stack needs a second column.
SMALL_SCREENS = [(1366, 768), (1536, 824)]

# The same two panels, and a 1920x1080 panel, with the display scaling of
# Windows set high. Windows reports the available area in logical pixels,
# so that area shrinks with the scaling while a notice keeps about the
# same logical height: a notice sizes itself from point-based fonts,
# which scale with the display. 1920x1080 at 200 percent gives 960x540,
# 1366x768 at 150 percent gives 911x512, and 1536x824 at 150 percent
# gives 1024x549. Each height below takes off the task bar, which the
# available area excludes. On these screens the stack needs a THIRD
# column, and a third column does not fit across the width.
NARROW_SHORT_SCREENS = [(960, 492), (911, 464), (1024, 501)]

# A 1366x768 panel at 200 percent display scaling gives 683x384, less the
# task bar. Windows permits that scaling, and a user who needs it is the
# user WheelHouse is for. On this screen even the blocked-microphone
# notice does not fit above the grant prompt, and the width holds no
# second column, so the notice's only place covers the prompt.
TINY_SCREEN = (683, 336)

# A 1600x1200 panel at 200 percent display scaling gives 800x600 logical
# pixels, and the task bar leaves 800x560. The width holds one column of
# notices only: the second column would start at 800 - 424 - 8 - 400 = -32,
# which is outside the area. The height is enough for the update-failure
# notice above the grant prompt, but not above the blocked-microphone
# notice, so that notice is the one that makes the failure notice wait
# (wh-blocked-mic-message.1.3).
ONE_COLUMN_SCREEN = (800, 560)


def _measured_heights():
    """The height in pixels to give each notice in this test, by title.

    A notice sizes itself from the machine's fonts and from the display
    scaling, so its height differs from machine to machine. These four
    values were measured in this worktree on 2026-10-09 at 100 percent
    scaling, and they are the sizes that need a second column on both
    screens below. The same four notices measured 192, 140, 88 and 174
    pixels on a machine set to 300 percent scaling, where three of them
    fit in one column on a 1366x768 screen and the placement this test
    checks never happens. The test pins the heights so that the column
    rule is checked on every machine, not only on a machine whose
    scaling produces the taller notices (wh-blocked-mic-message).
    """
    import gui

    return {
        gui.PARAKEET_OFFER_TITLE: 287,
        GRANT_EVENT_TITLE: 186,
        gui.MICROPHONE_BLOCKED_TITLE: 199,
        NOTICE_TITLE: 227,
        # The update-failure notice is shorter, because it carries two
        # sentences and no buttons (wh-blocked-mic-message.1.3).
        FAILURE_TITLE: 106,
    }


@contextmanager
def _screen_of(width, height):
    """A primary screen of this size, with notices that have the heights
    of the running app.

    Both screen fixtures below use this, so every screen size pins the
    heights the same way.
    """
    from grant_prompt_toast import GrantPromptToast

    screen = MagicMock()
    screen.availableGeometry.return_value = QRect(0, 0, width, height)
    heights = _measured_heights()
    real_adjust_size = GrantPromptToast.adjustSize

    def adjust_size(self):
        real_adjust_size(self)
        measured = heights.get(self._title_label.text())
        if measured is not None:
            self.setFixedHeight(measured)

    with patch.object(QApplication, "primaryScreen", return_value=screen), \
         patch.object(GrantPromptToast, "adjustSize", adjust_size):
        yield screen


@pytest.fixture(params=SMALL_SCREENS, ids=lambda size: f"{size[0]}x{size[1]}")
def small_screen(request):
    """A primary screen of the size in the parameter, with notices that
    have the heights of the running app."""
    width, height = request.param
    with _screen_of(width, height) as screen:
        yield screen


@pytest.fixture(
    params=NARROW_SHORT_SCREENS, ids=lambda size: f"{size[0]}x{size[1]}"
)
def narrow_short_screen(request):
    """A primary screen too short for one column of notices and too
    narrow for a third column."""
    width, height = request.param
    with _screen_of(width, height) as screen:
        yield screen


@pytest.fixture
def tiny_screen():
    """A primary screen on which no notice fits above the grant prompt."""
    width, height = TINY_SCREEN
    with _screen_of(width, height) as screen:
        yield screen


@pytest.fixture
def one_column_screen():
    """A primary screen that holds one column of notices."""
    width, height = ONE_COLUMN_SCREEN
    with _screen_of(width, height) as screen:
        yield screen


def _assert_every_notice_is_clear_and_on_the_screen(toasts, screen, anchored=()):
    """No two visible notices intersect and each sits inside the available
    area of the screen.

    The Parakeet offer and the grant prompt both anchor themselves at one
    fixed point (GrantPromptToast._anchor_to_screen) and this bead may not
    change either, so those two cover each other whenever both are visible.
    The pair is the only one the check skips; ``anchored`` names it.
    """
    area = screen.availableGeometry()
    assert all(toast.isVisible() for toast in toasts)
    for toast in toasts:
        assert area.contains(toast.frameGeometry()), (
            f"{toast.frameGeometry()} is outside {area}"
        )
    for i, first in enumerate(toasts):
        for second in toasts[i + 1:]:
            if first in anchored and second in anchored:
                continue  # two notices on the same anchor, see above
            assert not first.frameGeometry().intersects(second.frameGeometry()), (
                f"{first.frameGeometry()} covers {second.frameGeometry()}"
            )


class TestPlacementOnASmallScreen:
    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE

    def test_three_notices_do_not_cover_each_other_and_stay_on_the_screen(
        self, manager, small_screen
    ):
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        _assert_every_notice_is_clear_and_on_the_screen(
            [
                manager._parakeet_offer_toast,
                manager._microphone_blocked_toast,
                manager._update_notice_toast,
            ],
            small_screen,
        )

    def test_four_notices_do_not_cover_each_other_and_stay_on_the_screen(
        self, manager, small_screen
    ):
        _show_grant_prompt(manager)
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        _assert_every_notice_is_clear_and_on_the_screen(
            [
                manager._grant_prompt_toast,
                manager._parakeet_offer_toast,
                manager._microphone_blocked_toast,
                manager._update_notice_toast,
            ],
            small_screen,
            anchored=(manager._grant_prompt_toast, manager._parakeet_offer_toast),
        )

    def test_four_notices_in_the_other_order_of_arrival(
        self, manager, small_screen
    ):
        manager._show_update_notice(NOTICE)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        _show_grant_prompt(manager)
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._stack_update_notice()
        _assert_every_notice_is_clear_and_on_the_screen(
            [
                manager._grant_prompt_toast,
                manager._parakeet_offer_toast,
                manager._microphone_blocked_toast,
                manager._update_notice_toast,
            ],
            small_screen,
            anchored=(manager._grant_prompt_toast, manager._parakeet_offer_toast),
        )


# ---------------------------------------------------------------------------
# The three placement rules on a screen that cannot hold every notice
# (wh-blocked-mic-message.1.1, boss ruling 2026-10-10 00:07)
# ---------------------------------------------------------------------------


def _assert_the_update_notice_covers_nothing(manager):
    """Rule 1 and rule 3: the update notice covers no other notice.

    A hidden update notice passes. The ruling lets it wait for a place
    that covers nothing, and it appears when the user closes a notice.
    """
    update = manager._update_notice_toast
    if update is None or not update.isVisible():
        return
    others = (
        ("the grant prompt", manager._grant_prompt_toast),
        ("the Parakeet offer", manager._parakeet_offer_toast),
        ("the blocked-microphone notice", manager._microphone_blocked_toast),
    )
    for name, other in others:
        if other is None or not other.isVisible():
            continue
        assert not update.frameGeometry().intersects(other.frameGeometry()), (
            f"the update notice at {update.frameGeometry()} covers {name} "
            f"at {other.frameGeometry()}"
        )


def _assert_the_microphone_notice_is_on_the_screen(manager):
    """Rule 2: the blocked-microphone notice never waits.

    Without the microphone no voice feature works, so this notice is
    shown even when its only place covers an anchored prompt.
    """
    notice = manager._microphone_blocked_toast
    assert notice is not None, "no blocked-microphone notice was made"
    assert notice.isVisible(), "the blocked-microphone notice is not on the screen"


class TestPlacementOnANarrowShortScreen:
    """The screen holds neither one column of notices nor a third column.

    The placement loop moves a notice that does not fit above its column
    into a column to the left. When that column would reach outside the
    available area, the loop clamps the notice to the top of the area and
    keeps the right edge of the column it is already in, so the notice
    covers the one already there. On these screens the covered notice is
    the blocked-microphone notice.

    These tests measure the three rules of the ruling and nothing else.
    They do not ask every notice to be visible, and they do not ask every
    notice to sit inside the available area, because a screen this small
    can make those two impossible at the same time.
    """

    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE

    def test_the_update_notice_does_not_cover_the_microphone_notice(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        _assert_the_update_notice_covers_nothing(manager)

    def test_the_microphone_notice_stays_on_the_screen(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        _assert_the_microphone_notice_is_on_the_screen(manager)

    def test_with_four_notices_the_update_notice_still_covers_nothing(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_parakeet_model_offer(LAUNCH_OFFER)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        _assert_the_update_notice_covers_nothing(manager)
        _assert_the_microphone_notice_is_on_the_screen(manager)

    def test_the_update_notice_first_then_the_microphone_notice(
        self, manager, narrow_short_screen
    ):
        manager._show_update_notice(NOTICE)
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        _assert_the_update_notice_covers_nothing(manager)
        _assert_the_microphone_notice_is_on_the_screen(manager)

    def test_the_update_notice_appears_when_the_microphone_notice_closes(
        self, manager, narrow_short_screen
    ):
        """A waiting update notice takes the place the closed notice left.

        These three screens hold the update notice above the grant prompt
        once the blocked-microphone notice is gone, so the user sees the
        notice in the same session. A notice that never gets a place
        returns at the next update check, which comes at most
        CHECK_INTERVAL_SECONDS (24 hours) after the last check, not at the
        next launch.
        """
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        assert not update.isVisible(), (
            "the update notice took a place that covers another notice: "
            f"{update.frameGeometry()}"
        )
        manager._microphone_blocked_toast.close()
        assert update.isVisible(), (
            "the update notice did not appear when the blocked-microphone "
            "notice closed"
        )
        _assert_the_update_notice_covers_nothing(manager)


class TestPlacementWhenNoNoticeFitsAboveThePrompt:
    """The screen is too small even for the blocked-microphone notice.

    These tests are the ones that measure rule 2 of the ruling. On the
    larger screens of NARROW_SHORT_SCREENS the blocked-microphone notice
    fits above the grant prompt and covers nothing, so a rule that made
    it wait would change nothing there. Here its only place covers the
    prompt, so the rule decides what the user sees.
    """

    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE

    def test_the_microphone_notice_covers_the_prompt_instead_of_waiting(
        self, manager, tiny_screen
    ):
        grant = _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        notice = manager._microphone_blocked_toast
        assert notice.isVisible(), (
            "the blocked-microphone notice waited instead of taking its "
            f"place at {notice.frameGeometry()}"
        )
        assert notice.frameGeometry().intersects(grant.frameGeometry()), (
            "this screen no longer makes the notice cover the prompt, so "
            "the test measures nothing: the notice is at "
            f"{notice.frameGeometry()} and the prompt at "
            f"{grant.frameGeometry()}"
        )

    def test_the_covered_prompt_stays_open(self, manager, tiny_screen):
        grant = _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        assert grant.isVisible(), "the covered grant prompt closed itself"

    def test_the_prompt_is_clear_again_when_the_notice_closes(
        self, manager, tiny_screen
    ):
        grant = _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        notice = manager._microphone_blocked_toast
        assert notice.frameGeometry().intersects(grant.frameGeometry())
        notice.close()
        assert grant.isVisible(), "the grant prompt did not stay open"
        assert not notice.isVisible(), "the notice stayed on the screen"
        assert not notice.frameGeometry().intersects(
            grant.frameGeometry()
        ) or not notice.isVisible(), "the prompt is still covered"

    def test_a_notice_that_never_appears_returns_at_the_next_update_check(
        self, manager, tiny_screen
    ):
        """Rule 3 keeps the update notice away from an anchored prompt.

        This screen has no place for the update notice at all: above the
        grant prompt it reaches over the top of the area, and the clamp
        then puts it back over the prompt. It therefore stays hidden even
        after the blocked-microphone notice closes. A notice that never
        appeared leaves no record, so it returns at the next update check,
        which comes at most CHECK_INTERVAL_SECONDS (24 hours) after the
        last check, not at the next launch. The GUI sends nothing for it,
        so the Logic process writes no record
        (wh-blocked-mic-message.1.2). Measured on 2026-10-10.
        """
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        assert not update.isVisible(), (
            "the update notice took a place that covers another notice: "
            f"{update.frameGeometry()}"
        )
        manager._microphone_blocked_toast.close()
        assert not update.isVisible(), (
            "the update notice took a place over the grant prompt at "
            f"{update.frameGeometry()}"
        )
        _assert_the_update_notice_covers_nothing(manager)
        assert _sent(manager) == [], (
            "the GUI reported a notice that never appeared: "
            f"{_sent(manager)}"
        )


class TestTheWaitingUpdateFailureNotice:
    """The update-failure notice must not count its lifetime while it waits.

    The notice closes itself after ``UPDATE_FAILED_LIFETIME_MS``. A QTimer
    runs on a hidden widget as well, and ``close()`` on a hidden widget
    still reports the notice as closed, so the waiting notice died unseen:
    the user who clicked "Update now" then got no answer at all
    (wh-blocked-mic-message.1.3). This screen holds one column only, so the
    failure notice has no free place while the blocked-microphone notice is
    up. It fits above the grant prompt once that notice is gone, so the
    user must still get the text there. The numbers: the grant prompt is
    186 pixels high and anchors at y=250, the blocked-microphone notice is
    199 high and sits at y=43, and the failure notice is 106 high. Above
    the microphone notice it would start at -71, so the clamp puts it at
    y=24, where it covers that notice. Above the grant prompt alone it
    starts at y=136 and covers nothing.
    """

    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE
    FAILURE = {"action": "update_failed"}

    def _wait_for_a_place(self, manager):
        """Show the grant prompt, the microphone notice and the failure."""
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_failed_notice(self.FAILURE)
        toast = manager._update_notice_toast
        assert not toast.isVisible(), (
            "the failure notice took a place that covers another notice: "
            f"{toast.frameGeometry()}"
        )
        return toast

    def test_the_lifetime_stops_while_the_notice_waits(
        self, manager, one_column_screen
    ):
        toast = self._wait_for_a_place(manager)
        assert not toast._lifetime_timer.isActive(), (
            "the lifetime of the hidden failure notice still counts down, "
            f"with {toast._lifetime_timer.remainingTime()} ms left"
        )

    def test_the_failure_text_reaches_the_user_after_its_lifetime(
        self, manager, one_column_screen
    ):
        """The notice appears although its whole lifetime has passed.

        The lifetime is 500 ms here, which is the shortest period
        ``show_prompt`` accepts, and the wait is longer than that. Before
        the fix the timer closed the hidden notice inside that wait.
        """
        with patch("gui.UPDATE_FAILED_LIFETIME_MS", 500):
            toast = self._wait_for_a_place(manager)
            QTest.qWait(900)
        manager._microphone_blocked_toast.close()
        assert toast.isVisible(), (
            "the failure notice did not appear when the blocked-microphone "
            "notice closed, so the user never read the text"
        )
        assert toast._title_label.text() == FAILURE_TITLE, (
            "the notice shows other text than the update failure: "
            f"{toast._title_label.text()}"
        )
        assert toast._lifetime_timer.isActive(), (
            "the failure notice appeared with no lifetime, so it stays on "
            "the screen until the user closes it"
        )
        left = toast._lifetime_timer.remainingTime()
        assert left > 400, (
            "the lifetime did not start again, so the user gets less than "
            f"the full time to read the text: {left} ms left of 500"
        )


class TestTheGuiReportsThatTheNoticeAppeared:
    """The GUI sends ``update_notice_shown`` at the one moment the update
    notice becomes visible, and at no other (wh-blocked-mic-message.1.2)."""

    MESSAGE = TestTheBlockedMicrophoneNotice.MESSAGE

    def test_the_command_goes_out_with_the_version_when_the_notice_appears(
        self, manager
    ):
        manager._show_update_notice(NOTICE)
        assert _sent(manager) == [_report(VERSION)], (
            f"the GUI sent {_sent(manager)} for a notice that appeared"
        )

    def test_the_command_names_the_version_of_the_notice_on_the_screen(
        self, manager
    ):
        manager._show_update_notice(NOTICE)
        manager._show_update_notice(
            {"action": "update_available", "version": "1.3.0", "tag": "v1.3.0"}
        )
        assert _sent(manager) == [_report(VERSION), _report("1.3.0")]

    def test_a_notice_that_waits_sends_nothing_until_it_appears(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        update = manager._update_notice_toast
        assert not update.isVisible()
        assert _sent(manager) == [], (
            f"the GUI reported a notice that waits: {_sent(manager)}"
        )
        manager._microphone_blocked_toast.close()
        assert update.isVisible()
        assert _sent(manager) == [_report(VERSION)], (
            f"the GUI sent {_sent(manager)} when the notice appeared"
        )

    def test_the_command_goes_out_once_however_often_the_stack_is_placed(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        manager._microphone_blocked_toast.close()
        manager._stack_update_notice()
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._stack_update_notice()
        assert _sent(manager) == [_report(VERSION)], (
            f"the GUI sent {_sent(manager)} for one notice"
        )

    def test_a_second_message_for_a_waiting_version_replaces_the_waiting_notice(
        self, manager, narrow_short_screen
    ):
        from grant_prompt_toast import GrantPromptToast

        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        first_toast = manager._update_notice_toast
        manager._show_update_notice(NOTICE)
        assert manager._update_notice_toast is first_toast
        assert not first_toast.isVisible()
        assert _sent(manager) == [], (
            f"the GUI reported a notice that waits: {_sent(manager)}"
        )
        manager._microphone_blocked_toast.close()
        on_screen = [
            widget for widget in QApplication.topLevelWidgets()
            if isinstance(widget, GrantPromptToast)
            and widget.isVisible()
            and widget._title_label.text() == NOTICE_TITLE
        ]
        assert on_screen == [first_toast], (
            f"{len(on_screen)} update notices are on the screen"
        )
        assert _sent(manager) == [_report(VERSION)], (
            f"the GUI sent {_sent(manager)} for one waiting notice"
        )

    def test_a_notice_whose_text_failed_to_appear_is_not_reported(self, manager):
        # show_prompt raises for the second version, so the toast still
        # carries the first text. The second version must not become
        # reportable, or the next placement would record a version the
        # user never saw (wh-blocked-mic-message.1.2).
        manager._show_update_notice(NOTICE)
        toast = manager._update_notice_toast
        assert _sent(manager) == [_report(VERSION)]
        with patch.object(toast, "show_prompt", side_effect=RuntimeError("no text")):
            manager._show_update_notice(
                {"action": "update_available", "version": "1.3.0", "tag": "v1.3.0"}
            )
        manager._stack_update_notice()
        assert _report("1.3.0") not in _sent(manager), (
            "the GUI reported a version whose text never reached the notice: "
            f"{_sent(manager)}"
        )

    def test_the_failure_notice_sends_nothing_and_the_update_notice_after_it_sends_one(
        self, manager
    ):
        manager._show_update_failed_notice({"action": "update_failed"})
        assert _sent(manager) == [], (
            f"the GUI sent {_sent(manager)} for the failure notice"
        )
        manager._show_update_notice(NOTICE)
        assert _sent(manager) == [_report(VERSION)]

    def test_a_failure_notice_that_replaces_a_waiting_update_notice_sends_nothing(
        self, manager, narrow_short_screen
    ):
        _show_grant_prompt(manager)
        manager._show_microphone_blocked_notice(self.MESSAGE)
        manager._show_update_notice(NOTICE)
        manager._show_update_failed_notice({"action": "update_failed"})
        manager._microphone_blocked_toast.close()
        toast = manager._update_notice_toast
        assert toast.isVisible() and toast._title_label.text() == FAILURE_TITLE
        assert _sent(manager) == [], (
            f"the GUI reported a version for the failure notice: {_sent(manager)}"
        )
        manager._show_update_notice(NOTICE)
        assert _sent(manager) == [_report(VERSION)]
