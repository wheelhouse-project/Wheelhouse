"""The first-run notice (wh-first-run-notice).

On the first start after an install, Wheelhouse shows a window that tells a
new user how to see that Wheelhouse hears them (the round red button) and to
say "help". Later starts show nothing.

Three parts, three classes of test:

  * Logic: LogicController._run_first_run_notice reads a marker file once,
    off the event loop, and sends the GUI one message when the marker says
    the notice was not shown. It does not write the marker. The GUI process
    answers with a ``first_run_notice_shown`` command once the window is on
    screen, and the handler for that command writes the marker, off the
    event loop (wh-first-run-notice.1.1). The marker reader and writer are
    the existing load_hint_shown and mark_hint_shown, so a missing file is a
    first start and an unreadable file is a later start.
  * GUI: GuiManager shows one FirstRunNoticeWindow for the
    ``open_first_run_notice`` action and then sends the one
    ``first_run_notice_shown`` command; its Open help button sends the same
    command the assistant explanation window's button sends; its Close
    button sends nothing.
  * Window: every approved string, word for word, both buttons, the
    Escape key.

The wording is David's, approved at 18:18 on the day the bead was
implemented (bead wh-first-run-notice). Assertions are exact, not
substrings: the words are the deliverable.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

import pytest

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

APPROVED_TITLE = "Welcome to Wheelhouse"
APPROVED_PARAGRAPHS = (
    "The round red button shows that Wheelhouse hears you. The button pulses"
    " from red to orange while you speak.",
    "Say the word help on its own to open the help page.",
    "Wheelhouse shows this message once.",
)
OPEN_HELP_LABEL = "Open help"
CLOSE_LABEL = "Close"

SHOW_MESSAGE = {"action": "open_first_run_notice"}
SHOWN_COMMAND = {"action": "first_run_notice_shown"}


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


def _controller(ready=True):
    """A stand-in ``self`` for LogicController methods.

    ``ready`` sets the speech-provider ready event before the notice task
    runs, so the tests that are not about the wait do not wait. A test
    about the wait passes False and sets the event itself
    (wh-first-run-notice.2).
    """
    from main import LogicController

    state_manager = MagicMock()
    state_manager.state_to_gui_queue = Queue()
    controller = MagicMock(spec=LogicController)
    controller.state_manager = state_manager
    controller._speech_provider_ready = asyncio.Event()
    if ready:
        controller._speech_provider_ready.set()
    return controller


def _gui_messages(controller):
    messages = []
    gui_queue = controller.state_manager.state_to_gui_queue
    while True:
        try:
            messages.append(gui_queue.get_nowait())
        except Empty:
            return messages


async def _command_arrives(controller):
    """Deliver the GUI process's ``first_run_notice_shown`` command.

    Goes through the real command table, so a missing or misnamed entry
    fails here, then runs the task the table creates. Returns the names of
    the tasks the table created.
    """
    from main import LogicController

    controller._build_gui_handler_map = (
        LogicController._build_gui_handler_map.__get__(controller)
    )
    controller._handle_first_run_notice_shown = (
        LogicController._handle_first_run_notice_shown.__get__(controller)
    )
    tasks = []
    controller.create_task_with_error_handling = MagicMock(
        side_effect=lambda coro, name: tasks.append((coro, name))
    )
    handler_map = controller._build_gui_handler_map(dict(SHOWN_COMMAND))
    assert "first_run_notice_shown" in handler_map
    handler_map["first_run_notice_shown"]()
    for coro, _name in tasks:
        await coro
    return [name for _coro, name in tasks]


@pytest.fixture
def marker(tmp_path):
    """Point the first-run marker at a temporary file that does not exist."""
    path = tmp_path / "first_run_notice_shown.toml"
    with patch("main.default_first_run_notice_marker_path", return_value=path):
        yield path


async def _run(controller):
    from main import LogicController

    await LogicController._run_first_run_notice(controller)


class TestTheFirstStartShowsTheNotice:
    @pytest.mark.asyncio
    async def test_with_no_marker_the_gui_is_sent_one_show_message(self, marker):
        controller = _controller()
        await _run(controller)
        assert _gui_messages(controller) == [SHOW_MESSAGE]

    @pytest.mark.asyncio
    async def test_the_queue_put_alone_writes_no_marker(self, marker):
        # The put only hands the message to the transport. The GUI process
        # may never read it, so the marker waits for the command the GUI
        # process sends once the window is on screen (wh-first-run-notice.1.1).
        controller = _controller()
        seen = []
        real_put = controller.state_manager.state_to_gui_queue.put_nowait

        def put(message):
            seen.append(marker.exists())
            real_put(message)

        controller.state_manager.state_to_gui_queue.put_nowait = put
        await _run(controller)  # no command comes back from the GUI process
        assert seen == [False]
        assert _gui_messages(controller) == [SHOW_MESSAGE]
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_a_gui_that_never_shows_the_window_gets_the_message_again(
        self, marker
    ):
        # Two starts, and the GUI process answers neither: the marker file
        # stays absent, so each start queues the message.
        first = _controller()
        await _run(first)
        second = _controller()
        await _run(second)
        assert _gui_messages(first) == [SHOW_MESSAGE]
        assert _gui_messages(second) == [SHOW_MESSAGE]
        assert not marker.exists()


class TestTheGuiCommandWritesTheMarker:
    @pytest.mark.asyncio
    async def test_the_command_from_the_gui_writes_the_marker(self, marker):
        from services.wheelhouse.click_first_use_hint import load_hint_shown

        controller = _controller()
        assert not marker.exists()
        names = await _command_arrives(controller)
        assert names == ["FirstRunNoticeShown"]
        assert marker.exists()
        assert load_hint_shown(marker) is True

    @pytest.mark.asyncio
    async def test_a_second_command_writes_the_marker_again_without_an_error(
        self, marker
    ):
        from services.wheelhouse.click_first_use_hint import load_hint_shown

        controller = _controller()
        await _command_arrives(controller)
        assert marker.exists()
        # The second command: the marker exists already and the write must
        # neither raise nor lose the marker.
        await _command_arrives(controller)
        assert marker.exists()
        assert load_hint_shown(marker) is True

    @pytest.mark.asyncio
    async def test_a_start_after_the_command_shows_nothing(self, marker):
        first = _controller()
        await _run(first)
        await _command_arrives(first)
        second = _controller()
        await _run(second)
        assert _gui_messages(first) == [SHOW_MESSAGE]
        assert _gui_messages(second) == []

    @pytest.mark.asyncio
    async def test_a_marker_write_that_fails_raises_nothing(self, marker):
        # The task is created through create_task_with_error_handling, which
        # shuts Wheelhouse down on an escaping exception.
        controller = _controller()
        escaped = None
        with patch(
            "services.wheelhouse.click_first_use_hint.mark_hint_shown",
            side_effect=OSError("disk full"),
        ):
            try:
                await _command_arrives(controller)
            except Exception as exc:  # noqa: BLE001 -- the test names it below
                escaped = exc
        assert escaped is None
        assert not marker.exists()


class TestALaterStartDoesNot:
    @pytest.mark.asyncio
    async def test_with_a_marker_the_gui_is_sent_nothing(self, marker):
        from services.wheelhouse.click_first_use_hint import mark_hint_shown

        assert mark_hint_shown(marker) is True
        controller = _controller()
        await _run(controller)
        assert _gui_messages(controller) == []

    @pytest.mark.asyncio
    async def test_a_marker_that_is_not_rewritten(self, marker):
        from services.wheelhouse.click_first_use_hint import mark_hint_shown

        assert mark_hint_shown(marker) is True
        before = marker.read_bytes()
        await _run(_controller())
        assert marker.read_bytes() == before

    @pytest.mark.asyncio
    async def test_an_unreadable_marker_counts_as_shown(self, marker):
        # The existing loader reads a corrupt file as "already shown", so a
        # damaged marker never repeats the notice on every start.
        marker.write_text("this is = not [valid toml", encoding="utf-8")
        controller = _controller()
        await _run(controller)
        assert _gui_messages(controller) == []


class TestFailuresAreContained:
    @pytest.mark.asyncio
    async def test_no_gui_queue_raises_nothing_and_writes_no_marker(self, marker):
        # A notice nobody could be sent must not be marked as shown.
        controller = _controller()
        controller.state_manager.state_to_gui_queue = None
        await _run(controller)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_a_full_queue_raises_nothing_and_writes_no_marker(self, marker):
        controller = _controller()
        controller.state_manager.state_to_gui_queue = Queue(maxsize=1)
        controller.state_manager.state_to_gui_queue.put_nowait("full")
        await _run(controller)
        assert not marker.exists()


@pytest.fixture
def marker_read(marker):
    """A threading.Event that is set once the notice task has read the marker.

    The read runs in a worker thread, so a fixed number of event-loop turns
    can end before it does. A test that checked the queue then would pass
    against code that sends the message without waiting for the provider.
    """
    from services.wheelhouse import click_first_use_hint

    real_load = click_first_use_hint.load_hint_shown
    done = threading.Event()

    def load(path):
        try:
            return real_load(path)
        finally:
            done.set()

    with patch.object(click_first_use_hint, "load_hint_shown", load):
        yield done


async def _after_the_read(done, turns=20):
    """Wait for the marker read, then give the event loop ``turns`` turns."""
    for _ in range(500):
        if done.is_set():
            break
        await asyncio.sleep(0.01)
    assert done.is_set(), "the notice task never read the marker"
    for _ in range(turns):
        await asyncio.sleep(0)


class TestTheNoticeWaitsForTheSpeechProvider:
    """The notice opens only after the speech provider reports ready.

    David at 10:47 on 2026-10-10: the notice opened under the loading
    window while Parakeet was still loading (wh-first-run-notice.2).
    """

    @pytest.mark.asyncio
    async def test_nothing_is_sent_before_the_provider_is_ready(
        self, marker, marker_read
    ):
        controller = _controller(ready=False)
        task = asyncio.create_task(_run(controller))
        await _after_the_read(marker_read)
        try:
            assert _gui_messages(controller) == []
            assert not task.done()
        finally:
            task.cancel()

    @pytest.mark.asyncio
    async def test_the_message_is_sent_once_the_provider_is_ready(
        self, marker, marker_read
    ):
        controller = _controller(ready=False)
        task = asyncio.create_task(_run(controller))
        await _after_the_read(marker_read)
        controller._speech_provider_ready.set()
        await asyncio.wait_for(task, timeout=5)
        assert _gui_messages(controller) == [SHOW_MESSAGE]

    @pytest.mark.asyncio
    async def test_a_start_that_never_becomes_ready_sends_nothing_and_writes_no_marker(
        self, marker, marker_read
    ):
        controller = _controller(ready=False)
        task = asyncio.create_task(_run(controller))
        await _after_the_read(marker_read)
        assert _gui_messages(controller) == []
        assert not task.done(), "the notice task stopped instead of waiting"
        # Shutdown cancels the task. The cancel must reach the task's
        # owner, which logs it at debug level and does not shut down
        # (LogicController._handle_task_completion).
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _gui_messages(controller) == []
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_the_next_start_that_becomes_ready_shows_the_notice(
        self, marker, marker_read
    ):
        first = _controller(ready=False)
        task = asyncio.create_task(_run(first))
        await _after_the_read(marker_read)
        assert _gui_messages(first) == []
        assert not task.done(), "the notice task stopped instead of waiting"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = _controller(ready=True)
        await asyncio.wait_for(_run(second), timeout=5)
        assert _gui_messages(first) == []
        assert _gui_messages(second) == [SHOW_MESSAGE]

    @pytest.mark.asyncio
    async def test_with_a_marker_the_start_does_not_wait_for_ready(
        self, marker, marker_read
    ):
        # A restart from the tray menu after the notice was shown reads the
        # marker and ends at once, whether or not the provider is ready.
        from services.wheelhouse.click_first_use_hint import mark_hint_shown

        assert mark_hint_shown(marker) is True
        controller = _controller(ready=False)
        task = asyncio.create_task(_run(controller))
        try:
            await _after_the_read(marker_read)
            assert task.done()
            assert _gui_messages(controller) == []
        finally:
            task.cancel()

    @pytest.mark.asyncio
    async def test_the_ready_callback_sets_the_event_from_another_thread(self):
        # The launcher calls the callback on whatever thread delivered the
        # ready. The callback must hand the set to the event loop.
        from main import LogicController

        controller = _controller(ready=False)
        callback = LogicController._speech_provider_ready_callback(controller)
        caller = threading.Thread(target=callback)
        caller.start()
        caller.join(timeout=5)
        for _ in range(500):
            if controller._speech_provider_ready.is_set():
                break
            await asyncio.sleep(0.01)
        assert controller._speech_provider_ready.is_set()

    def test_main_gives_the_callback_to_the_launcher(self):
        from main import LogicController

        source = inspect.getsource(LogicController.main)
        assert "set_provider_ready_callback(" in source
        assert "self._speech_provider_ready_callback()" in source


# ---------------------------------------------------------------------------
# The speech provider launcher
# ---------------------------------------------------------------------------


def _launcher(tmp_path, current=1):
    """A launcher with two stamped launches; ``current`` is the live one."""
    from stt.remote_stt_launcher import RemoteSTTLauncher

    launcher = RemoteSTTLauncher(
        services_dir=tmp_path / "providers",
        app_data_dir=tmp_path / "appdata",
    )
    launcher._launch_generations = {"parakeet": 1, "google": 2}
    launcher._current_launch_generation = current
    launcher._provider_ready_event.clear()
    launcher._provider_stopped = MagicMock()
    launcher._hide_working = MagicMock()
    launcher._notify = MagicMock()
    return launcher


class TestTheLauncherReportsAnAcceptedReady:
    def test_a_ready_from_the_current_launch_calls_the_callback_once(
        self, tmp_path
    ):
        launcher = _launcher(tmp_path, current=1)
        callback = MagicMock()
        launcher.set_provider_ready_callback(callback)
        launcher.signal_provider_ready(1)
        callback.assert_called_once_with()

    def test_a_ready_from_a_replaced_launch_does_not(self, tmp_path):
        # The user switched from parakeet (launch 1) to google (launch 2),
        # and parakeet's queued ready arrives afterwards.
        launcher = _launcher(tmp_path, current=2)
        callback = MagicMock()
        launcher.set_provider_ready_callback(callback)
        launcher.signal_provider_ready(1)
        callback.assert_not_called()

    def test_a_ready_after_a_failed_startup_does_not(self, tmp_path):
        launcher = _launcher(tmp_path, current=1)
        callback = MagicMock()
        launcher.set_provider_ready_callback(callback)
        launcher.signal_provider_startup_failed(1)
        launcher.signal_provider_ready(1)
        callback.assert_not_called()

    def test_a_callback_that_raises_does_not_stop_the_ready(self, tmp_path):
        # The ready arrives on the WebSocket reader; an exception there
        # would end the reader for the rest of the session.
        launcher = _launcher(tmp_path, current=1)
        launcher.set_provider_ready_callback(
            MagicMock(side_effect=RuntimeError("closed loop"))
        )
        escaped = None
        try:
            launcher.signal_provider_ready(1)
        except Exception as exc:  # noqa: BLE001 -- the test names it below
            escaped = exc
        assert escaped is None
        assert not launcher.is_starting

    def test_with_no_callback_a_ready_raises_nothing(self, tmp_path):
        launcher = _launcher(tmp_path, current=1)
        launcher.signal_provider_ready(1)
        assert not launcher.is_starting


class TestAReadFailureIsContained:
    @pytest.mark.asyncio
    async def test_a_marker_read_that_fails_raises_nothing(self, marker):
        # The task is created through create_task_with_error_handling, which
        # shuts Wheelhouse down on an escaping exception.
        controller = _controller()
        escaped = None
        with patch(
            "services.wheelhouse.click_first_use_hint.load_hint_shown",
            side_effect=OSError("disk error"),
        ):
            try:
                await _run(controller)
            except Exception as exc:  # noqa: BLE001 -- the test names it below
                escaped = exc
        assert escaped is None
        assert _gui_messages(controller) == []


class TestTheMarkerFileWorkLeavesTheEventLoop:
    @pytest.mark.asyncio
    async def test_the_read_runs_in_a_worker_thread(self, marker):
        loop_thread = threading.get_ident()
        seen = {}

        def fake_load(path):
            seen["load"] = threading.get_ident()
            return False

        with patch(
            "services.wheelhouse.click_first_use_hint.load_hint_shown", fake_load
        ):
            await _run(_controller())

        read_off_loop = seen.get("load") not in (None, loop_thread)
        assert read_off_loop is True

    @pytest.mark.asyncio
    async def test_the_write_runs_in_a_worker_thread(self, marker):
        loop_thread = threading.get_ident()
        seen = {}

        def fake_mark(path):
            seen["mark"] = threading.get_ident()
            return True

        with patch(
            "services.wheelhouse.click_first_use_hint.mark_hint_shown", fake_mark
        ):
            await _command_arrives(_controller())

        write_off_loop = seen.get("mark") not in (None, loop_thread)
        assert write_off_loop is True

    @pytest.mark.asyncio
    async def test_the_marker_is_read_once(self, marker):
        calls = []

        def fake_load(path):
            calls.append(path)
            return True

        with patch(
            "services.wheelhouse.click_first_use_hint.load_hint_shown", fake_load
        ):
            await _run(_controller())
        assert calls == [marker]


class TestTheMarkerPath:
    def test_it_is_the_first_run_file_under_data(self):
        from main import default_first_run_notice_marker_path

        path = default_first_run_notice_marker_path()
        assert path.name == "first_run_notice_shown.toml"
        assert path.parent.name == "data"
        assert path.parent.parent == wheelhouse_dir

    def test_it_is_not_the_help_explainer_marker(self):
        from main import (
            default_first_run_notice_marker_path,
            default_help_explainer_marker_path,
        )

        assert (
            default_first_run_notice_marker_path()
            != default_help_explainer_marker_path()
        )


class TestStartupRegistration:
    def test_the_notice_task_is_registered_with_the_other_startup_tasks(self):
        from main import LogicController

        controller = _controller()
        coroutines = LogicController._startup_task_coroutines(controller)
        for coroutine in coroutines.values():
            if inspect.iscoroutine(coroutine):
                coroutine.close()  # the mock builds it; nothing awaits it
        assert "FirstRunNotice" in coroutines

    def test_main_creates_every_registered_task_through_the_error_handler(self):
        from main import LogicController

        source = inspect.getsource(LogicController.main)
        assert "self._startup_task_coroutines().items()" in source


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


@pytest.fixture
def window(qapp):
    from first_run_notice_window import FirstRunNoticeWindow

    win = FirstRunNoticeWindow()
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()


class TestApprovedText:
    def test_the_title_is_the_approved_title(self, window):
        from first_run_notice_window import FIRST_RUN_NOTICE_TITLE

        assert FIRST_RUN_NOTICE_TITLE == APPROVED_TITLE
        assert window.windowTitle() == APPROVED_TITLE

    def test_the_three_paragraphs_are_the_approved_words(self, window):
        from first_run_notice_window import FIRST_RUN_NOTICE_BODY_PARAGRAPHS

        assert tuple(FIRST_RUN_NOTICE_BODY_PARAGRAPHS) == APPROVED_PARAGRAPHS

    def test_the_body_shows_the_three_paragraphs_in_order(self, window):
        assert window._body_label.text() == "\n\n".join(APPROVED_PARAGRAPHS)

    def test_the_buttons_carry_the_approved_labels(self, window):
        assert window._help_button.text() == OPEN_HELP_LABEL
        assert window._close_button.text() == CLOSE_LABEL

    def test_the_visible_text_is_the_accessible_name(self, window):
        for button, label in (
            (window._help_button, OPEN_HELP_LABEL),
            (window._close_button, CLOSE_LABEL),
        ):
            assert (button.accessibleName() or button.text()) == label

    def test_the_title_string_appears_in_the_module_source(self):
        # The help-document build searches this .py file for the exact title.
        import first_run_notice_window

        source = Path(first_run_notice_window.__file__).read_text(encoding="utf-8")
        assert f'"{APPROVED_TITLE}"' in source


class TestTheWindowsButtons:
    def test_open_help_reports_the_choice_and_closes(self, window):
        seen = []
        window.help_chosen.connect(lambda: seen.append("help"))
        window.show()
        window._help_button.click()
        assert seen == ["help"]
        assert window.isVisible() is False

    def test_close_reports_nothing_and_closes(self, window):
        seen = []
        window.help_chosen.connect(lambda: seen.append("help"))
        window.show()
        window._close_button.click()
        assert seen == []
        assert window.isVisible() is False

    def test_escape_reports_nothing_and_closes(self, window):
        seen = []
        window.help_chosen.connect(lambda: seen.append("help"))
        window.show()
        window.reject()  # what the Escape key calls on a QDialog
        assert seen == []
        assert window.isVisible() is False

    def test_the_window_is_not_modal(self, window):
        from PySide6.QtCore import Qt

        assert window.isModal() is False
        assert window.windowModality() == Qt.WindowModality.NonModal

    def test_open_help_is_the_default_button(self, window):
        assert window._help_button.isDefault() is True
        assert window._close_button.isDefault() is False


# ---------------------------------------------------------------------------
# The GUI process
# ---------------------------------------------------------------------------


def _connected_handler(window_class):
    """The function the manager connected to the window's help_chosen signal."""
    connect = window_class.return_value.help_chosen.connect
    assert connect.call_args is not None
    return connect.call_args.args[0]


def _commands(mgr):
    sent = []
    while not mgr.commands_to_logic_queue.empty():
        sent.append(mgr.commands_to_logic_queue.get_nowait())
    return sent


@pytest.fixture
def steal_foreground():
    with patch("terminal_editor_window._steal_foreground") as steal:
        yield steal


@pytest.fixture
def fake_window(steal_foreground):
    with patch("first_run_notice_window.FirstRunNoticeWindow") as cls:
        cls.return_value = MagicMock()
        cls.return_value.isVisible.return_value = False
        yield cls


# Imported for its fixtures: the manager fixture needs a live QApplication
# and the editor-window replacement, as in test_gui_help_explainer.py.
from tests.test_gui_release_does_not_guess_speech import manager  # noqa: E402,F401


@pytest.fixture
def loading_window(qapp, manager):
    """Give the manager a real, hidden loading window.

    The manager fixture replaces WorkingDialog with a MagicMock, whose
    isVisible() answers a true MagicMock. A real dialog answers the question
    the notice asks (wh-first-run-notice.2).
    """
    from gui import WorkingDialog

    dialog = WorkingDialog()
    manager.working_dialog = dialog
    yield dialog
    dialog.close()


class TestTheGuiShowsTheNotice:
    pytestmark = pytest.mark.usefixtures(
        "qapp", "mock_editor_window", "loading_window"
    )

    def test_the_logic_message_opens_the_window(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        fake_window.assert_called_once()
        fake_window.return_value.show.assert_called_once()

    def test_a_second_message_raises_the_one_window(self, manager, fake_window):
        for _ in range(2):
            manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
            manager._check_queues_and_events()

        fake_window.assert_called_once()
        window = fake_window.return_value
        assert window.show.call_count == 2
        assert window.raise_.call_count == 2
        assert window.activateWindow.call_count == 2

    def test_the_window_is_brought_to_the_front(
        self, manager, fake_window, steal_foreground
    ):
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        steal_foreground.assert_called_once()

    def test_a_foreground_failure_does_not_stop_the_window(
        self, manager, fake_window, steal_foreground
    ):
        steal_foreground.side_effect = OSError("no window handle")
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        fake_window.return_value.show.assert_called_once()

    def test_the_help_choice_opens_help_directly(self, manager, fake_window):
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()
        _commands(manager)  # the shown command is not under test here
        # The window's signal is connected to the manager's handler.
        _connected_handler(fake_window)()

        sent = _commands(manager)
        assert len(sent) == 1
        assert sent[0]["action"] == "open_help_online"
        assert sent[0]["explained"] is True

    def test_the_help_choice_carries_no_setting_change(self, manager, fake_window):
        # A do_not_show_again key would make the Logic process write the
        # assistant window's setting and marker (start_help_online).
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()
        _commands(manager)  # the shown command is not under test here
        _connected_handler(fake_window)()

        sent = _commands(manager)
        assert len(sent) == 1
        assert "do_not_show_again" not in sent[0]

    def test_showing_the_window_sends_the_shown_command_once(
        self, manager, fake_window
    ):
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        assert _commands(manager) == [SHOWN_COMMAND]

    def test_the_shown_command_follows_the_window_being_shown(
        self, manager, fake_window
    ):
        # The Logic process writes its marker on this command, so it must
        # not leave this process before the window is on screen.
        queue_empty_at_show = []
        fake_window.return_value.show.side_effect = lambda: (
            queue_empty_at_show.append(manager.commands_to_logic_queue.empty())
        )
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        assert queue_empty_at_show == [True]
        assert _commands(manager) == [SHOWN_COMMAND]

    def test_a_window_that_fails_to_show_sends_no_shown_command(
        self, manager, fake_window
    ):
        fake_window.return_value.show.side_effect = RuntimeError("no display")
        manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
        manager._check_queues_and_events()

        assert _commands(manager) == []

    def test_a_second_message_shows_no_second_window_and_reports_again(
        self, manager, fake_window
    ):
        for _ in range(2):
            manager.state_from_logic_queue.put_nowait(dict(SHOW_MESSAGE))
            manager._check_queues_and_events()

        fake_window.assert_called_once()
        assert _commands(manager) == [SHOWN_COMMAND, SHOWN_COMMAND]


def _hide(owner):
    return {"action": "hide_working", "owner": owner}


class TestTheGuiWaitsForTheLoadingWindow:
    """The notice never opens under the always-on-top loading window.

    David's screenshots at 10:46 on 2026-10-10 showed the notice under
    "Loading Parakeet v3 (CPU)..." (wh-first-run-notice.2).
    """

    pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

    def _deliver(self, manager, message):
        manager.state_from_logic_queue.put_nowait(dict(message))
        manager._check_queues_and_events()

    def test_a_message_while_the_loading_window_is_up_opens_nothing(
        self, manager, fake_window, loading_window
    ):
        loading_window.show_working("Loading Parakeet v3 (CPU)", "stt:1")
        self._deliver(manager, SHOW_MESSAGE)

        fake_window.return_value.show.assert_not_called()
        assert _commands(manager) == []

    def test_the_held_notice_opens_when_the_loading_window_closes(
        self, manager, fake_window, loading_window
    ):
        loading_window.show_working("Loading Parakeet v3 (CPU)", "stt:1")
        self._deliver(manager, SHOW_MESSAGE)
        self._deliver(manager, _hide("stt:1"))

        assert not loading_window.isVisible()
        fake_window.return_value.show.assert_called_once()
        assert _commands(manager) == [SHOWN_COMMAND]

    def test_a_hide_that_leaves_the_loading_window_up_opens_nothing(
        self, manager, fake_window, loading_window
    ):
        # The user switched to launch 2. The hide from replaced launch 1
        # arrives and the dialog ignores it, so the window stays up.
        loading_window.show_working("Loading Google", "stt:2")
        self._deliver(manager, SHOW_MESSAGE)
        self._deliver(manager, _hide("stt:1"))

        assert loading_window.isVisible()
        fake_window.return_value.show.assert_not_called()
        assert _commands(manager) == []

        self._deliver(manager, _hide("stt:2"))
        fake_window.return_value.show.assert_called_once()
        assert _commands(manager) == [SHOWN_COMMAND]

    def test_the_held_notice_opens_once(
        self, manager, fake_window, loading_window
    ):
        loading_window.show_working("Loading Parakeet v3 (CPU)", "stt:1")
        self._deliver(manager, SHOW_MESSAGE)
        self._deliver(manager, _hide("stt:1"))
        loading_window.show_working("Thinking", "ai:x")
        self._deliver(manager, _hide("ai:x"))

        fake_window.return_value.show.assert_called_once()
        assert _commands(manager) == [SHOWN_COMMAND]

    def test_a_hide_with_no_held_notice_opens_nothing(
        self, manager, fake_window, loading_window
    ):
        loading_window.show_working("Loading Parakeet v3 (CPU)", "stt:1")
        self._deliver(manager, _hide("stt:1"))

        fake_window.assert_not_called()
        assert _commands(manager) == []
