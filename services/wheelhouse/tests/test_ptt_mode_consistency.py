"""Continuous speech excludes PTT mode; a temporary hold remains legitimate.

Route coverage deliberately includes already-correct PTT entry. Red regressions
cover continuous activation and conflicting startup settings (wh-ptt-button-color-inconsistent).
"""
import asyncio
from queue import Empty, Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from event_bus import EventBus
from state_manager import StateManager
from tests.test_gui_release_does_not_guess_speech import manager  # noqa: F401

pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


@pytest.fixture
def state(mock_config, manager, monkeypatch):
    monkeypatch.setattr("gui.send_notice", lambda *args, **kwargs: True)
    loop = asyncio.new_event_loop()
    sm = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)
    yield sm
    if sm._ptt_active:
        sm.ptt_stop()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def deliver(manager, state):
    """Use the production dispatcher and GUI queue consumer, without live IPC."""
    from main import LogicController
    controller = Mock(state_manager=state)
    while True:
        try:
            command = manager.commands_to_logic_queue.get_nowait()
        except Empty:
            break
        LogicController._build_gui_handler_map(controller, command)[command["action"]]()
    manager._check_queues_and_events()


def menu_action(manager, label, backend):
    if backend == "qt":
        menu = manager._create_menu(is_tray_menu=False)
        next(a for a in menu.actions() if a.text() == label).trigger()
        menu.close()
    else:
        # Keep pystray's real Menu/MenuItem binding; only the live icon is mocked.
        import pystray
        with patch("gui.pystray", pystray):
            menu = manager._create_menu(is_tray_menu=True)
            next(a for a in menu.items if a.text == label)(None)


def enter_ptt(manager, state, route):
    if route in ("qt", "pystray"):
        menu_action(manager, "Push-to-Talk Mode", route)
    elif route == "floating_double":
        manager._on_double_click()
    elif route == "tray_double":
        manager._tray_click_timer = Mock()
        manager._tray_click_timer.is_alive.return_value = True
        manager._on_tray_left_click()
    else:
        from speech.actions import ActionFunctions
        actions = ActionFunctions.__new__(ActionFunctions)
        actions.speech_handler = SimpleNamespace(logic_controller=SimpleNamespace(
            state_manager=state, state_to_gui_queue=manager.state_from_logic_queue))
        actions.set_speech_interaction_mode("push_to_talk")
    deliver(manager, state)


@pytest.mark.parametrize("route", ["qt", "pystray", "floating_double", "tray_double", "voice"])
def test_ptt_entry_routes_disable_continuous_and_show_blue(manager, state, route):
    """Baseline guard: breaking any entry route must leave an incorrect state."""
    state.toggle_speech_enabled_state()
    manager._check_queues_and_events()
    enter_ptt(manager, state, route)
    assert state._speech_interaction_mode == "push_to_talk"
    assert state._speech_enabled is False
    assert state.speech_enabled is False
    assert manager.speech_interaction_mode == "push_to_talk"
    assert manager.speech_enabled is False
    manager.button.set_ptt_mode.assert_called_with(True)
    manager.button.set_state.assert_called_with(False)


@pytest.mark.parametrize("route", ["qt", "pystray", "floating_single", "tray_single", "ipc"])
def test_continuous_activation_leaves_ptt_mode(manager, state, route):
    """Red: an enable command must never leave continuous speech in PTT mode."""
    state.set_speech_interaction_mode("push_to_talk")
    manager._check_queues_and_events()
    if route in ("qt", "pystray"):
        menu_action(manager, "Speech Enabled", route)
    elif route == "floating_single":
        # A deferred click can arrive after Logic entered PTT, before GUI polls.
        manager._on_deferred_single_click()
    elif route == "tray_single":
        manager.speech_interaction_mode = "toggle"  # stale pre-poll state
        manager._on_deferred_tray_single_click()
    else:
        manager.commands_to_logic_queue.put_nowait({"action": "toggle_speech_enabled_state"})
    deliver(manager, state)
    assert state._speech_interaction_mode == "toggle"
    assert state.speech_enabled is True
    assert state.config_service.get("speech.interaction_mode") == "toggle"
    assert manager.speech_interaction_mode == "toggle"
    manager.button.set_ptt_mode.assert_called_with(False)
    manager.button.set_state.assert_called_with(True)


def test_ptt_startup_overrides_continuous_startup_setting(mock_config):
    """Red: saved PTT mode must start idle even with speech-on-start enabled."""
    mock_config.set("SPEECH_ENABLED_ON_STARTUP", True)
    mock_config.set("speech.interaction_mode", "push_to_talk")
    loop = asyncio.new_event_loop()
    try:
        sm = StateManager(mock_config, EventBus(), loop, Queue(), None)
        assert sm._speech_interaction_mode == "push_to_talk"
        assert sm._speech_enabled is False
        assert sm.speech_enabled is False
    finally:
        loop.close()


@pytest.mark.parametrize("ending", ["released", "drag_cancel", "gesture_cancel", "timeout"])
def test_hold_temporarily_listens_in_ptt_and_returns_idle(state, ending):
    """Baseline guard: exclusivity must not disable the legitimate hold."""
    state.set_speech_interaction_mode("push_to_talk")
    state.ptt_start()
    assert state._speech_interaction_mode == "push_to_talk"
    assert state.speech_enabled is True
    if ending == "timeout":
        state._ptt_safety_timeout()
    else:
        state.ptt_stop(reason=ending)
    assert state._speech_interaction_mode == "push_to_talk"
    assert state.speech_enabled is False


def test_explicit_enable_during_suppressed_hold_survives_release(state):
    """Red: suppression makes enable reachable during a hold; release honors it."""
    state.set_speech_interaction_mode("push_to_talk")
    state._speech_suppressed_by_sonos = True
    state.ptt_start()
    assert state.speech_enabled is False
    state.toggle_speech_enabled_state()
    assert state._speech_interaction_mode == "toggle"
    assert state.speech_enabled is True
    state.ptt_stop()
    assert state._speech_interaction_mode == "toggle"
    assert state.speech_enabled is True


def test_startup_tells_engine_the_state_the_button_shows(mock_config):
    """Red: attaching the manager must correct its enabled-by-default flag.

    Every other set_transcription_status call site is event driven, so a quiet
    startup left the engine on its stored True default while the button showed
    idle (wh-audit2-ptt-mode-review.1).
    """
    from integrations.websocket_manager import WebSocketManager

    mock_config.set("SPEECH_ENABLED_ON_STARTUP", True)
    mock_config.set("speech.interaction_mode", "push_to_talk")
    loop = asyncio.new_event_loop()
    try:
        sm = StateManager(mock_config, EventBus(), loop, Queue(), None)
        engine = WebSocketManager(loop)
        # What main.py does once the app has started and the manager exists.
        sm.websocket_manager = engine
        told = engine.get_current_status_message()["enabled"]
        assert told == sm.speech_enabled, (
            f"engine told transcription enabled={told} while the user "
            f"interface shows speech_enabled={sm.speech_enabled}"
        )
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


def test_toggle_startup_tells_engine_speech_is_on(mock_config):
    """Red: the attach must report the computed value, not a constant off.

    The opposite configuration to the test above, and the shipped one: toggle
    mode with speech-on-start, where speech_enabled is True. The manager is
    started on False first, so the assertion measures what the attach wrote
    rather than what was already stored; between the two tests no constant
    can satisfy both (wh-audit2-ptt-mode-review.1).
    """
    from integrations.websocket_manager import WebSocketManager

    mock_config.set("SPEECH_ENABLED_ON_STARTUP", True)
    mock_config.set("speech.interaction_mode", "toggle")
    loop = asyncio.new_event_loop()
    try:
        sm = StateManager(mock_config, EventBus(), loop, Queue(), None)
        assert sm.speech_enabled is True, "precondition: the button starts on"
        engine = WebSocketManager(loop)
        engine.set_transcription_status(False, reason="startup")
        sm.websocket_manager = engine
        told = engine.get_current_status_message()["enabled"]
        assert told is True and told == sm.speech_enabled, (
            f"engine told transcription enabled={told} while the user "
            f"interface shows speech_enabled={sm.speech_enabled}"
        )
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
