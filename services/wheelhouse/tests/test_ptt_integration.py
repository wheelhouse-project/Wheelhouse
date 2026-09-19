"""Integration tests for push-to-talk full cycle.

Tests PTT state transitions, event subscriptions, and mode switching.
Uses real EventBus for subscription verification, with mocked loop.create_task
to capture event publication (same pattern as unit tests).
"""

import asyncio
import math
from unittest.mock import Mock

import pytest

from state_manager import StateManager
from event_bus import EventBus
from events import PTTStartedEvent, PTTStoppedEvent, PTTMuteStateEvent

from tests.test_gui_release_does_not_guess_speech import manager as ptt_pending_manager


@pytest.mark.usefixtures("qapp", "mock_editor_window")
def test_ptt_pending_sonos_refusal_reaches_real_gui(ptt_pending_manager, mock_config, monkeypatch):
    """Drive the real command dispatcher and both real state participants."""
    from main import LogicController
    from gui import FloatingButton
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    manager = ptt_pending_manager
    button = FloatingButton()
    manager.button = button
    button.set_indeterminate(False)
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)
    try:
        state.set_speech_interaction_mode("push_to_talk")
        state._set_speech_suppressed_by_sonos(True)
        manager._check_queues_and_events()
        manager._start_ptt()
        assert button._is_enabled is False
        assert "Waiting" in button.accessibleDescription()
        command = manager.commands_to_logic_queue.get_nowait()
        controller = Mock(state_manager=state)
        LogicController._build_gui_handler_map(controller, command)[command["action"]]()
        assert state.speech_enabled is False
        manager._check_queues_and_events()
        assert button._is_enabled is False
        assert "Sonos" in button.accessibleDescription()
        assert button.toolTip() == button.accessibleDescription()
        assert manager.speech_interaction_mode == "push_to_talk"
        manager._stop_ptt()
        command = manager.commands_to_logic_queue.get_nowait()
        LogicController._build_gui_handler_map(controller, command)[command["action"]]()
        manager._check_queues_and_events()
        assert state._ptt_active is False
        assert state.speech_enabled is False
    finally:
        if state._ptt_active:
            state.ptt_stop()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        button.close()
        button.deleteLater()


@pytest.mark.usefixtures("qapp", "mock_editor_window")
@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [True, False])
async def test_ptt_pending_settings_ack_keeps_hold_feedback(ptt_pending_manager, tmp_path, monkeypatch, qtbot, saved):
    """A settings result and broadcast cannot acknowledge an unrelated PTT hold."""
    import tomllib
    import tomli_w
    from config_service import ConfigService
    from gui import FloatingButton
    from main import LogicController
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    monkeypatch.setattr("soft_allow_write_failed_toast.SoftAllowWriteFailedToast", Mock())
    path = tmp_path / "config.toml"
    path.write_text("FLOATING_BUTTON_SIZE = 50\nFLOATING_BUTTON_POS = [100, 100]\n")
    config = ConfigService(str(path))
    manager = ptt_pending_manager
    button = FloatingButton()
    qtbot.addWidget(button)
    manager.button = button
    button.set_indeterminate(False)
    state = StateManager(config, EventBus(), asyncio.get_running_loop(), manager.state_from_logic_queue, None)
    controller = Mock(state_manager=state)
    tasks = []
    controller.create_task_with_error_handling.side_effect = lambda coro, name: tasks.append(coro)
    try:
        state._set_speech_suppressed_by_sonos(True)
        manager._check_queues_and_events()
        manager._start_ptt()
        hold = manager.commands_to_logic_queue.get_nowait()
        assert hold["action"] == "ptt_start"
        manager.send_size_change_command(80)
        setting = manager.commands_to_logic_queue.get_nowait()
        assert setting["action"] == "set_config_value"
        assert setting["request_id"] != hold["request_id"]
        assert manager.settings_pending and manager._ptt_feedback == "pending"
        if not saved:
            def fail_write(*args, **kwargs):
                raise OSError("disk full")
            monkeypatch.setattr(tomli_w, "dump", fail_write)
        LogicController._build_gui_handler_map(controller, setting)[setting["action"]]()
        await tasks.pop()
        manager._check_queues_and_events()
        expected = 80 if saved else 50
        assert not manager.settings_pending
        assert manager._settings_confirmed["FLOATING_BUTTON_SIZE"] == expected
        assert tomllib.loads(path.read_text())["FLOATING_BUTTON_SIZE"] == expected
        assert manager._ptt_request_id == hold["request_id"]
        assert manager._ptt_feedback == "pending" and button._is_enabled is False
        assert "Waiting" in button.accessibleDescription()
        LogicController._build_gui_handler_map(controller, hold)[hold["action"]]()
        manager._check_queues_and_events()
        assert manager._ptt_feedback == "refused" and button._is_enabled is False
        assert "Sonos" in button.accessibleDescription()
        assert button.toolTip() == button.accessibleDescription()
        assert manager._settings_confirmed["FLOATING_BUTTON_SIZE"] == expected
        manager._stop_ptt()
        stop = manager.commands_to_logic_queue.get_nowait()
        assert stop["action"] == "ptt_stop"
        LogicController._build_gui_handler_map(controller, stop)[stop["action"]]()
        manager._check_queues_and_events()
        assert not state._ptt_active and not manager.settings_pending
        assert manager.commands_to_logic_queue.empty()
    finally:
        if state._ptt_active:
            state.ptt_stop()
        await asyncio.sleep(0)


@pytest.mark.usefixtures("qapp", "mock_editor_window")
def test_ptt_pending_release_refreshes_continuous_accessibility(ptt_pending_manager, mock_config, monkeypatch):
    """After a real hold ends, accessible feedback follows ordinary Logic state."""
    from main import LogicController
    from gui import FloatingButton
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    manager = ptt_pending_manager
    button = FloatingButton()
    manager.button = button
    button.set_indeterminate(False)
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)

    def dispatch_hold_command(action):
        command = manager.commands_to_logic_queue.get_nowait()
        assert command["action"] == action
        controller = Mock(state_manager=state)
        LogicController._build_gui_handler_map(controller, command)[action]()
        manager._check_queues_and_events()

    try:
        state.set_speech_interaction_mode("push_to_talk")
        manager._check_queues_and_events()
        manager._start_ptt()
        assert button._is_enabled is False
        assert "Waiting" in button.accessibleDescription()
        dispatch_hold_command("ptt_start")
        assert state.speech_enabled is True and button._is_enabled is True
        assert button.accessibleDescription() == "Listening. Release to stop push to talk."
        manager._stop_ptt()
        dispatch_hold_command("ptt_stop")
        assert state._ptt_active is False and button._is_enabled is False
        assert manager._ptt_request_id is None and manager._ptt_feedback == ""
        assert button.accessibleDescription() == "Push to talk released."
        state.set_speech_interaction_mode("toggle")
        state.toggle_speech_enabled_state()
        manager._check_queues_and_events()
        assert state.speech_enabled is True and button._is_enabled is True
        assert button.accessibleDescription() == "Listening."
        assert button.toolTip() == "Listening."
        state.toggle_speech_enabled_state()
        manager._check_queues_and_events()
        assert state.speech_enabled is False and button._is_enabled is False
        assert button.accessibleDescription() == "Not listening."
        assert button.toolTip() == "Not listening."
        assert manager.commands_to_logic_queue.empty()
    finally:
        if state._ptt_active:
            state.ptt_stop()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        button.close()
        button.deleteLater()


@pytest.mark.usefixtures("qapp", "mock_editor_window")
@pytest.mark.parametrize("released", [False, True])
def test_ptt_pending_logic_restart_can_report_actual_listening(ptt_pending_manager, mock_config, monkeypatch, released):
    """Lose the start command, then deliver a fresh Logic participant's states."""
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    manager = ptt_pending_manager
    manager._start_ptt()
    lost = manager.commands_to_logic_queue.get_nowait()
    assert lost["action"] == "ptt_start" and lost["request_id"]
    manager._ptt_pending_timeout()
    assert "not been confirmed" in manager.button.set_ptt_feedback.call_args.args[1]
    assert manager.commands_to_logic_queue.qsize() == 1
    assert manager.commands_to_logic_queue.get_nowait() == {"action": "request_initial_state"}
    assert manager.commands_to_logic_queue.empty()  # No repeated start after loss.
    if released:
        manager._stop_ptt()
        assert manager.commands_to_logic_queue.get_nowait()["action"] == "ptt_stop"
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)
    try:
        assert state._ptt_request_id is None and state._ptt_active is False
        state.set_speech_interaction_mode("push_to_talk")
        manager._check_queues_and_events()
        manager.button.set_state.assert_called_with(False)
        state.set_speech_interaction_mode("toggle")
        state.toggle_speech_enabled_state()
        assert state.speech_enabled is True
        manager._check_queues_and_events()
        manager.button.set_state.assert_called_with(True)
        assert manager._ptt_feedback == ""
        assert manager._ptt_request_id is None
        assert manager.commands_to_logic_queue.empty()
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


@pytest.mark.usefixtures("qapp", "mock_editor_window")
@pytest.mark.parametrize("sonos", [False, True])
def test_ptt_pending_new_gui_request_acknowledges_existing_hold(ptt_pending_manager, mock_config, monkeypatch, sonos):
    """A fresh GUI token obtains feedback without restarting Logic's hold."""
    from main import LogicController
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    manager = ptt_pending_manager
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)
    try:
        state.set_speech_interaction_mode("push_to_talk")
        state._set_speech_suppressed_by_sonos(sonos)
        state.ptt_start(request_id="lost-gui-request")
        manager._check_queues_and_events()
        hold_id = state._ptt_hold_id
        timeout = state._ptt_safety_handle
        mute_wait = state._ptt_mute_confirm_handle
        manager._start_ptt()
        command = manager.commands_to_logic_queue.get_nowait()
        manager.button.set_state.assert_called_with(False)
        controller = Mock(state_manager=state)
        LogicController._build_gui_handler_map(controller, command)[command["action"]]()
        assert state._ptt_hold_id == hold_id
        assert state._ptt_safety_handle is timeout
        assert state._ptt_mute_confirm_handle is mute_wait
        manager._check_queues_and_events()
        assert state._ptt_request_id == command["request_id"]
        assert manager._ptt_feedback == ("refused" if sonos else "")
        manager.button.set_state.assert_called_with(not sonos)
        if sonos:
            assert "Sonos" in manager.button.set_ptt_feedback.call_args.args[1]
        manager._stop_ptt()
        command = manager.commands_to_logic_queue.get_nowait()
        assert command["action"] == "ptt_stop"
        LogicController._build_gui_handler_map(controller, command)[command["action"]]()
        manager._check_queues_and_events()
        assert state._ptt_active is False and state.speech_enabled is False
        manager.button.set_state.assert_called_with(False)
    finally:
        if state._ptt_active:
            state.ptt_stop()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


@pytest.mark.usefixtures("qapp", "mock_editor_window")
def test_ptt_pending_timeout_refreshes_an_early_restart_state(ptt_pending_manager, mock_config, monkeypatch):
    """An uncorrelated state arriving before expiry needs a fresh read afterward."""
    from main import LogicController
    monkeypatch.setattr("gui.send_notice", Mock(return_value=True))
    manager = ptt_pending_manager
    manager._start_ptt()
    assert manager.commands_to_logic_queue.get_nowait()["action"] == "ptt_start"
    loop = asyncio.new_event_loop()
    state = StateManager(mock_config, EventBus(), loop, manager.state_from_logic_queue, None)
    try:
        state.set_speech_interaction_mode("push_to_talk")
        state.set_speech_interaction_mode("toggle")
        state.toggle_speech_enabled_state()
        assert state.speech_enabled is True and state._ptt_request_id is None
        manager._check_queues_and_events()
        manager.button.set_state.assert_called_with(False)  # Not the hold's acknowledgement.
        manager._ptt_pending_timeout()
        manager.button.set_state.assert_called_with(False)
        assert manager.commands_to_logic_queue.qsize() == 1
        refresh = manager.commands_to_logic_queue.get_nowait()
        assert refresh == {"action": "request_initial_state"}  # Read, never retry start.
        manager._ptt_pending_timeout()
        assert manager.commands_to_logic_queue.empty()  # One bounded recovery request.
        controller = Mock(state_manager=state)
        LogicController._build_gui_handler_map(controller, refresh)[refresh["action"]]()
        manager._check_queues_and_events()
        manager.button.set_state.assert_called_with(True)
        assert manager._ptt_feedback == ""
        assert manager._ptt_request_id is None
        manager._stop_ptt()
        stop = manager.commands_to_logic_queue.get_nowait()
        assert stop["action"] == "ptt_stop"
        LogicController._build_gui_handler_map(controller, stop)[stop["action"]]()
        assert state.speech_enabled is True  # Already inactive; do not guess an off state.
    finally:
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


@pytest.fixture
def real_event_bus():
    return EventBus()


@pytest.fixture
def sm_integrated(mock_config, real_event_bus, mock_gui_queue, mock_websocket_manager):
    """StateManager with real EventBus but mocked loop for task capture."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()  # Capture created tasks
    mgr = StateManager(
        config_service=mock_config,
        event_bus=real_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    yield mgr, real_event_bus, loop
    loop.close()


def _get_published_events(loop):
    """Extract event objects from mocked create_task calls."""
    events = []
    for call in loop.create_task.call_args_list:
        coro = call[0][0]
        # The coroutine name tells us if it's an event_bus.publish call
        if hasattr(coro, 'cr_code') and 'publish' in (coro.cr_code.co_qualname or ''):
            # Can't easily extract the event from a coroutine, so we close it
            coro.close()
        else:
            # Close coroutines to avoid warnings
            if hasattr(coro, 'close'):
                coro.close()
    return events


class TestPTTFullCycle:
    """Integration: full PTT start -> stop cycle with state verification."""

    def test_ptt_start_stop_cycle_state(self, sm_integrated):
        sm, bus, loop = sm_integrated

        # Start PTT
        sm.ptt_start(source="floating_button")

        assert sm._ptt_active is True
        assert sm._speech_enabled is True
        assert sm.speech_enabled is True
        assert sm._speech_suppressed_by_idle is False

        # Verify create_task was called (publish event + broadcast)
        assert loop.create_task.call_count >= 2  # publish + broadcast

        # Stop PTT
        loop.create_task.reset_mock()
        sm.ptt_stop()

        assert sm._ptt_active is False
        assert sm._speech_enabled is False
        assert sm.speech_enabled is False
        assert loop.create_task.call_count >= 2  # publish + broadcast

    def test_ptt_clears_idle_suppression(self, sm_integrated):
        sm, _, loop = sm_integrated
        sm._speech_suppressed_by_idle = True
        sm._speech_enabled = False

        sm.ptt_start()

        assert sm._speech_suppressed_by_idle is False
        assert sm.speech_enabled is True

    def test_mode_switching(self, sm_integrated):
        sm, _, _ = sm_integrated

        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"

        sm.set_speech_interaction_mode("toggle")
        assert sm._speech_interaction_mode == "toggle"

    def test_mode_switch_invalid_rejected(self, sm_integrated):
        sm, _, _ = sm_integrated

        sm.set_speech_interaction_mode("invalid")
        assert sm._speech_interaction_mode == "toggle"  # Unchanged

    def test_safety_timeout_method_stops_ptt(self, sm_integrated):
        sm, _, loop = sm_integrated

        sm.ptt_start()
        assert sm._ptt_active is True

        # Simulate safety timeout firing
        sm._ptt_safety_timeout()

        assert sm._ptt_active is False
        assert sm._speech_enabled is False

    def test_double_ptt_start_is_noop(self, sm_integrated):
        sm, _, loop = sm_integrated

        sm.ptt_start()
        initial_call_count = loop.create_task.call_count

        sm.ptt_start()  # Second call should be ignored
        assert loop.create_task.call_count == initial_call_count  # No new tasks

    def test_ptt_stop_without_start_is_noop(self, sm_integrated):
        sm, _, loop = sm_integrated

        initial_call_count = loop.create_task.call_count
        sm.ptt_stop()
        assert loop.create_task.call_count == initial_call_count  # No tasks created

    def test_ptt_start_sends_state_with_mode(self, sm_integrated, mock_gui_queue):
        sm, _, loop = sm_integrated
        sm._speech_interaction_mode = "push_to_talk"

        sm.ptt_start()

        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["ptt_active"] is True
        assert state["speech_interaction_mode"] == "push_to_talk"
        assert state["speech_enabled"] is True

    def test_full_lifecycle_toggle_then_ptt_then_back(self, sm_integrated, mock_gui_queue):
        """Full lifecycle: toggle mode -> PTT mode -> start -> stop -> back to toggle."""
        sm, _, loop = sm_integrated

        # Start in toggle mode
        assert sm._speech_interaction_mode == "toggle"

        # Switch to PTT mode
        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"

        # PTT cycle
        sm.ptt_start()
        assert sm._ptt_active is True
        assert sm.speech_enabled is True

        sm.ptt_stop()
        assert sm._ptt_active is False
        assert sm.speech_enabled is False

        # Switch back to toggle
        sm.set_speech_interaction_mode("toggle")
        assert sm._speech_interaction_mode == "toggle"

        # Toggle works normally
        sm.toggle_speech_enabled_state()
        assert sm.speech_enabled is True


class TestPTTOverridesAudioSuppression:
    """A hold that starts while sound is playing has to hear the user.

    The hold mutes the speakers, so the sound the audio monitor saw is on its
    way out. The monitor reports only when its answer changes, and its poll
    interval is 10 seconds while sound plays, so a short hold fits entirely
    between two polls and the monitor never sees the mute. The hold therefore
    ignores audio suppression itself, without writing the setting the monitor
    owns (wh-ptt-audio-override).
    """

    def test_a_hold_that_starts_while_sound_is_playing_listens(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)

        sm.ptt_start()

        assert sm.speech_enabled is True

    def test_the_hold_never_writes_the_setting_the_monitor_owns(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)

        sm.ptt_start()
        assert sm._speech_suppressed_by_audio is True

        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        assert sm._speech_suppressed_by_audio is True

        sm.ptt_stop()
        assert sm._speech_suppressed_by_audio is True

    def test_a_confirmed_mute_keeps_the_hold_listening(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()

        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        assert sm.speech_enabled is True

    def test_a_failed_mute_stops_the_hold_listening(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()

        sm.ptt_confirm_mute(False, sm._ptt_hold_id)

        assert sm.speech_enabled is False

    def test_no_answer_about_the_mute_stops_the_hold_listening(self, sm_integrated):
        """The volume plugin is absent, so no answer ever arrives."""
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()

        sm._ptt_mute_unconfirmed()

        assert sm.speech_enabled is False

    def test_an_answer_after_the_wait_expired_does_not_restart_listening(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm._ptt_mute_unconfirmed()

        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        assert sm.speech_enabled is False

    @pytest.mark.parametrize(
        "reason", ["released", "safety_timeout", "drag_cancel", "gesture_cancel"]
    )
    def test_every_ending_drops_the_override(self, sm_integrated, reason):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        sm.ptt_stop(reason=reason)

        assert sm._ptt_audio_override is False
        assert sm.speech_enabled is False

    def test_the_safety_cutoff_drops_the_override(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        sm._ptt_safety_timeout()

        assert sm._ptt_audio_override is False
        assert sm.speech_enabled is False

    def test_an_answer_after_the_hold_ended_does_not_restart_listening(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_stop()

        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        assert sm._ptt_audio_override is False
        assert sm.speech_enabled is False

    def test_the_override_does_not_lift_sonos_suppression(self, sm_integrated):
        """Muting this computer cannot silence a speaker in another room."""
        sm, _, _ = sm_integrated
        sm._set_speech_suppressed_by_sonos(True)

        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        assert sm.speech_enabled is False

    def test_a_hold_with_nothing_playing_is_unchanged(self, sm_integrated):
        sm, _, _ = sm_integrated

        sm.ptt_start()

        assert sm.speech_enabled is True
        assert sm._speech_suppressed_by_audio is False


class TestMuteReportsAreMatchedToTheirHold:
    """A mute report answers one hold, and only that hold.

    The mute runs in a worker thread, so its report can arrive after the user
    released the button and pressed it again. Acting on that late report as if
    it answered the new hold either destroys the new hold's wait or stops the
    new hold listening for its whole length (wh-ptt-audio-override.1.1).
    """

    @staticmethod
    def _report_for_the_running_hold(sm, muted, reason):
        return PTTMuteStateEvent(muted=muted, reason=reason, hold_id=sm._ptt_hold_id)

    async def test_a_failed_mute_from_the_previous_hold_does_not_stop_this_hold_listening(
        self, sm_integrated
    ):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        stale = self._report_for_the_running_hold(sm, muted=False, reason="error")
        sm.ptt_stop()
        sm.ptt_start()

        await sm._handle_ptt_mute_state(stale)

        assert sm._ptt_audio_override is True
        assert sm.speech_enabled is True

    async def test_a_mute_report_from_the_previous_hold_leaves_this_holds_wait_running(
        self, sm_integrated
    ):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        stale = self._report_for_the_running_hold(sm, muted=True, reason="muted")
        sm.ptt_stop()
        sm.ptt_start()

        await sm._handle_ptt_mute_state(stale)

        assert sm._ptt_mute_confirm_handle is not None
        # Nothing has answered this hold, so its own wait must still be able to
        # withdraw the override.
        sm._ptt_mute_unconfirmed()
        assert sm._ptt_audio_override is False

    async def test_the_report_for_this_hold_is_still_honoured(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()

        await sm._handle_ptt_mute_state(
            self._report_for_the_running_hold(sm, muted=False, reason="error")
        )

        assert sm._ptt_audio_override is False
        assert sm.speech_enabled is False

    async def test_a_report_that_arrives_after_the_hold_ended_is_ignored(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        report = self._report_for_the_running_hold(sm, muted=True, reason="muted")
        sm.ptt_stop()

        await sm._handle_ptt_mute_state(report)

        assert sm._ptt_audio_override is False

    def test_every_hold_gets_its_own_number(self, sm_integrated):
        sm, _, _ = sm_integrated
        sm.ptt_start()
        first = sm._ptt_hold_id
        sm.ptt_stop()
        sm.ptt_start()

        assert sm._ptt_hold_id == first + 1


class TestTheHoldTimerSettingsAreChecked:
    """A wrong number in the settings file must not leave a hold with no timers.

    ConfigService returns whatever the settings file held, so a quoted number
    reaches asyncio's call_later as a string and raises there. ptt_start had
    already switched the hold on by that point, so the hold ran with no wait to
    withdraw the override and no safety cutoff to end it
    (wh-ptt-audio-override.1.2).
    """

    def test_a_quoted_wait_time_still_gives_the_hold_both_timers(
        self, sm_integrated, mock_config
    ):
        sm, _, _ = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", "0.5")

        sm.ptt_start()

        assert sm._ptt_active is True
        assert sm._ptt_mute_confirm_handle is not None
        assert sm._ptt_safety_handle is not None

    def test_a_quoted_safety_timeout_still_gives_the_hold_both_timers(
        self, sm_integrated, mock_config
    ):
        sm, _, _ = sm_integrated
        mock_config.set("speech.ptt_safety_timeout_seconds", "30")

        sm.ptt_start()

        assert sm._ptt_active is True
        assert sm._ptt_mute_confirm_handle is not None
        assert sm._ptt_safety_handle is not None

    def test_a_wait_time_of_zero_falls_back_to_the_default(self, sm_integrated, mock_config):
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", 0)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert remaining > 0.1

    def test_a_negative_wait_time_falls_back_to_the_default(self, sm_integrated, mock_config):
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", -5)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert remaining > 0.1

    def test_a_good_wait_time_is_used_as_written(self, sm_integrated, mock_config):
        """One second is the largest wait the setting accepts, so this also
        pins that the cap itself is usable rather than rejected."""
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", 1.0)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert 0.5 < remaining <= 1.0

    def test_a_wait_long_enough_to_cover_a_hold_falls_back_to_the_default(
        self, sm_integrated, mock_config
    ):
        """The wait is a confirmation wait, and nothing tells a user that a
        large value is permission to listen over live speakers.

        Measured on this machine, the two Core Audio calls the mute makes take
        a median of 0.028 ms over thirty runs, so a two-second wait is about
        seventy thousand times the work it waits for. What the extra time
        actually buys is two seconds of recognising whatever the speakers are
        playing, which is longer than many holds (wh-ptt-audio-override.1.8).
        """
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", 2)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert 0.1 < remaining <= 0.5

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_a_wait_time_that_is_not_a_finite_number_falls_back_to_the_default(
        self, sm_integrated, mock_config, bad
    ):
        """TOML writes inf and nan as ordinary floats, and neither compares as
        less than or equal to zero, so the positivity check lets them through.
        An endless wait never withdraws the override when nothing answers about
        the mute (wh-ptt-audio-override.1.3)."""
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", bad)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert math.isfinite(remaining)
        assert 0.1 < remaining <= 0.5

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_a_safety_timeout_that_is_not_a_finite_number_falls_back_to_the_default(
        self, sm_integrated, mock_config, bad
    ):
        """An endless safety cutoff never ends a hold whose release was lost."""
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_safety_timeout_seconds", bad)

        sm.ptt_start()

        remaining = sm._ptt_safety_handle.when() - loop.time()
        assert math.isfinite(remaining)
        assert 25 < remaining <= 30

    @pytest.mark.parametrize(
        "bad",
        [1e308, 10 ** 400, 1e-9],
        ids=["huge-float", "oversized-int", "below-the-clock"],
    )
    def test_a_wait_time_outside_the_useful_range_falls_back_to_the_default(
        self, sm_integrated, mock_config, bad
    ):
        """A finite value can still be useless as a wait.

        1e308 is finite and asyncio accepts it, but the wait never expires
        during any real run, so nothing withdraws the override when the volume
        plugin answers nothing. A TOML integer of 400 digits reaches
        math.isfinite, which raises OverflowError on it. A value below the
        event loop's clock resolution expires before the two Core Audio calls
        can finish (wh-ptt-audio-override.1.5).
        """
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", bad)

        sm.ptt_start()

        remaining = sm._ptt_mute_confirm_handle.when() - loop.time()
        assert 0.1 < remaining <= 0.5

    @pytest.mark.parametrize(
        "bad",
        [1e308, 10 ** 400, 1e-9],
        ids=["huge-float", "oversized-int", "below-the-clock"],
    )
    def test_a_safety_timeout_outside_the_useful_range_falls_back_to_the_default(
        self, sm_integrated, mock_config, bad
    ):
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_safety_timeout_seconds", bad)

        sm.ptt_start()

        remaining = sm._ptt_safety_handle.when() - loop.time()
        assert 25 < remaining <= 30

    def test_a_hold_still_starts_when_a_setting_is_an_oversized_whole_number(
        self, sm_integrated, mock_config
    ):
        """math.isfinite raises OverflowError on an int too large for a float,
        so the check itself must not be what breaks the hold."""
        sm, _, loop = sm_integrated
        mock_config.set("speech.ptt_mute_confirm_seconds", 10 ** 400)
        mock_config.set("speech.ptt_safety_timeout_seconds", 10 ** 400)

        sm.ptt_start()

        assert sm._ptt_active is True
        assert sm._ptt_mute_confirm_handle is not None
        assert sm._ptt_safety_handle is not None
