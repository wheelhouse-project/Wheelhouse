"""Visibility commands use the acknowledged settings path; fixtures only."""

import asyncio
import inspect
import threading
import tomllib
from queue import Full
from unittest.mock import MagicMock, patch

import pytest

from tests.test_gui_release_does_not_guess_speech import manager as base_manager


pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")
KEY = "FLOATING_BUTTON_VISIBLE"


@pytest.fixture
def manager(base_manager):
    with patch("gui.send_notice"), patch(
        "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
    ):
        base_manager.state_from_logic_queue.put_nowait({
            "action": "state_update", "button_visible": True,
            "settings_persisted": {KEY: True},
            "FLOATING_BUTTON_SIZE": 50, "FLOATING_BUTTON_POS": [100, 100],
        })
        base_manager._check_queues_and_events()
        while not base_manager.commands_to_logic_queue.empty():
            base_manager.commands_to_logic_queue.get_nowait()
        yield base_manager


@pytest.fixture
def make_state(manager, tmp_path):
    def make(initial=True):
        from config_service import ConfigService
        from main import LogicController
        from state_manager import StateManager

        path = tmp_path / "config.toml"
        path.write_bytes(f"{KEY} = {str(initial).lower()}\n".encode())
        config = ConfigService(str(path))
        state = StateManager(
            config, MagicMock(), asyncio.get_running_loop(),
            manager.state_from_logic_queue, None,
        )
        controller = MagicMock(spec=LogicController)
        controller.state_manager = state

        async def dispatch(command):
            pending = []
            controller.create_task_with_error_handling.side_effect = (
                lambda coro, name: pending.append(coro)
            )
            result = LogicController._build_gui_handler_map(controller, command)[command["action"]]()
            if inspect.isawaitable(result):
                await result
            for coro in pending:
                await coro

        manager.button_visible = initial
        manager._settings_confirmed[KEY] = initial
        return config, path, dispatch

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [True, False], ids=["saved", "disk-failure"])
@pytest.mark.parametrize("initial", [True, False], ids=["hide", "show"])
async def test_visibility_round_trip_uses_real_handler_and_saved_outcome(
    manager, make_state, monkeypatch, saved, initial,
):
    import tomli_w

    config, path, dispatch = make_state(initial)
    if not saved:
        def refuse_write(*args, **kwargs):
            raise OSError("synthetic disk full")
        monkeypatch.setattr(tomli_w, "dump", refuse_write)
    manager.toggle_button_visibility()
    command = manager.commands_to_logic_queue.get_nowait()
    await dispatch(command)
    manager._check_queues_and_events()

    expected = not initial if saved else initial
    assert config.get(KEY) is expected
    assert tomllib.loads(path.read_text())[KEY] is expected
    assert manager.button_visible is expected
    assert manager._settings_confirmed[KEY] is expected
    assert "request_id" in command
    assert not manager.settings_pending
    if not saved:
        assert "couldn't save" in manager.settings_status_text.lower()
        manager._settings_notice.setAccessibleName.assert_called_with(manager.settings_status_text)


def test_visibility_toggle_is_pending_until_confirmation(manager):
    manager.toggle_button_visibility()
    assert manager.settings_pending
    assert "waiting for confirmation" in manager.settings_status_text.lower()
    manager._settings_notice.setAccessibleName.assert_called_with(manager.settings_status_text)


def test_repeated_toggles_follow_latest_pending_target(manager):
    commands = []
    for _ in range(3):
        manager.toggle_button_visibility()
        commands.append(manager.commands_to_logic_queue.get_nowait())
    assert [command.get("value") for command in commands] == [False, True, False]
    assert len({command.get("request_id") for command in commands}) == 3


def test_visibility_queue_refusal_keeps_existing_request_and_feedback(manager):
    manager.toggle_button_visibility()
    first = manager.commands_to_logic_queue.get_nowait()
    with patch.object(manager.commands_to_logic_queue, "put_nowait", side_effect=Full):
        manager.toggle_button_visibility()
    assert "couldn't send" in manager.settings_status_text.lower()
    assert manager.settings_pending
    assert manager._settings_latest[KEY] == first["request_id"]
    assert manager._settings_requests[first["request_id"]]["values"][KEY] is False


@pytest.mark.asyncio
async def test_visibility_unknown_outcome_reconciles_without_toggling_again(manager, make_state):
    config, path, dispatch = make_state()
    manager.toggle_button_visibility()
    command = manager.commands_to_logic_queue.get_nowait()
    await dispatch(command)
    # Drop the acknowledgement and accompanying state broadcast.
    while not manager.state_from_logic_queue.empty():
        manager.state_from_logic_queue.get_nowait()
    assert manager.settings_pending
    manager._settings_requests[command["request_id"]]["deadline"] = 0
    manager._check_settings_timeout()
    read = manager.commands_to_logic_queue.get_nowait()
    assert read == {"action": "get_config_values", "request_id": command["request_id"], "keys": [KEY]}
    assert "outcome unknown" in manager.settings_status_text.lower()
    await dispatch(read)
    manager._check_queues_and_events()
    assert not manager.settings_pending
    assert manager.button_visible is False
    assert config.get(KEY) is False
    assert tomllib.loads(path.read_text())[KEY] is False


def _invoke_tray_visibility(manager, count=1):
    """Invoke the real menu callback on the thread used by pystray's run loop."""
    item = next(item for item in manager._create_menu(is_tray_menu=True)
                if item.text == "Show Floating Button")
    failures = []

    def invoke():
        try:
            for _ in range(count):
                item(manager.icon)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "tray callback blocked on queue dispatch"
    assert not failures, failures


@pytest.mark.parametrize("refuse_second", [False, True])
def test_tray_visibility_defers_target_registration_and_notice_to_qt(
    manager, qtbot, qapp, monkeypatch, refuse_second,
):
    from PySide6.QtCore import QThread, QTimer
    import gui

    assert QThread.currentThread() == manager.thread() == qapp.thread()
    observed = []
    uuid4, enqueue, show_status = gui.uuid.uuid4, manager.commands_to_logic_queue.put_nowait, manager._settings_show_status
    attempts = []

    def new_request_id():
        observed.append(("uuid", QThread.currentThread() == qapp.thread()))
        return uuid4()

    def enqueue_setting(command):
        observed.append(("enqueue", QThread.currentThread() == qapp.thread()))
        attempts.append(command)
        if refuse_second and len(attempts) == 2:
            raise Full
        enqueue(command)

    def status(*args, **kwargs):
        observed.append(("notice", QThread.currentThread() == qapp.thread()))
        show_status(*args, **kwargs)

    monkeypatch.setattr(gui.uuid, "uuid4", new_request_id)
    monkeypatch.setattr(manager.commands_to_logic_queue, "put_nowait", enqueue_setting)
    monkeypatch.setattr(manager, "_settings_show_status", status)
    _invoke_tray_visibility(manager, count=3)
    assert observed == [], "tray callback touched settings or Qt before dispatch"
    assert not manager.settings_pending
    assert manager.commands_to_logic_queue.empty()
    # A confirmed state change before Qt handles the queued clicks must affect
    # the first target; subsequent clicks use only successfully enqueued writes.
    manager.button_visible = False
    manager._settings_confirmed[KEY] = False
    QTimer.singleShot(0, manager._check_queues_and_events)
    qtbot.waitUntil(lambda: len(attempts) == 3)
    assert all(on_qt_thread for _, on_qt_thread in observed)
    assert [command['value'] for command in attempts] == ([True, False, False] if refuse_second else [True, False, True])
    assert len({command['request_id'] for command in attempts}) == 3
    queued = []
    while not manager.commands_to_logic_queue.empty():
        queued.append(manager.commands_to_logic_queue.get_nowait())
    assert queued == ([attempts[0], attempts[2]] if refuse_second else attempts)
    latest = attempts[-1]
    assert manager._settings_latest[KEY] == latest['request_id']
    assert manager._settings_requests[latest['request_id']]['values'][KEY] == latest['value']
    assert manager.settings_pending


def test_tray_dispatch_refusal_preserves_inflight_write_and_avoids_qt(manager):
    manager.toggle_button_visibility()
    first = manager.commands_to_logic_queue.get_nowait()
    with patch.object(manager.state_from_logic_queue, "put_nowait", side_effect=Full), \
         patch.object(manager, "_settings_show_status") as status, \
         patch("gui.send_notice") as notice:
        _invoke_tray_visibility(manager)
    status.assert_not_called()
    assert manager.commands_to_logic_queue.empty()
    assert manager._settings_latest[KEY] == first['request_id']
    assert manager._settings_requests[first['request_id']]['values'][KEY] is False
    notice.assert_called_once()
    assert "couldn't change" in notice.call_args.args[1].lower()
