"""Listening pauses while an app holds the speaker in exclusive mode.

wh-kodi-playback-echo-stutter, stage 2. The detector (a later step) publishes
SpeakerExclusiveStateChangedEvent. These tests cover the StateManager half:
the pause flag, the setting gate, the log lines, and what the pause refuses.

Covers:
- the event pauses speech and its release restores it
- a wake word, the user's switch and push-to-talk do not end the pause
- the ENABLE_AUDIO_SUPPRESSION gate, independent of the startup decision
- one INFO line at each change, naming the endpoint and the method
- every other pause still holds after the exclusive pause ends
- the engine is told a reason that does not arm the wake word
"""

import asyncio
import logging
from unittest.mock import Mock

import pytest

from events import (
    SpeakerExclusiveStateChangedEvent,
    SystemIdleStateChangedEvent,
    WakeWordDetectedEvent,
)
from state_manager import StateManager

ENDPOINT = "Speakers (Test Device)"
METHOD = "shared-mode check"


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
    """A StateManager whose loop never runs, so nothing it schedules fires."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    mgr._speech_enabled = True
    mgr.speech_notifier._send_notification = Mock()
    yield mgr
    loop.close()


async def _hold(sm, held=True, endpoint=ENDPOINT, method=METHOD):
    await sm._handle_exclusive_speaker_state_changed(
        SpeakerExclusiveStateChangedEvent(
            held=held, endpoint_name=endpoint, method=method
        )
    )


async def _wake_word(sm):
    await sm._handle_wake_word_detected(WakeWordDetectedEvent(keyword="computer"))


class TestThePauseFollowsTheEvent:

    @pytest.mark.asyncio
    async def test_the_event_pauses_speech_and_the_release_restores_it(self, sm):
        assert sm.speech_enabled is True

        await _hold(sm, held=True)
        assert sm.speech_enabled is False

        await _hold(sm, held=False)
        assert sm.speech_enabled is True

    def test_the_manager_subscribes_to_the_event(self, sm, mock_event_bus):
        # The class is matched by name: the suite imports "events" and the
        # manager imports "services.wheelhouse.events", two module objects.
        subscribed = [
            (call.args[0].__name__, call.args[1])
            for call in mock_event_bus.subscribe.call_args_list
        ]
        assert (
            "SpeakerExclusiveStateChangedEvent",
            sm._handle_exclusive_speaker_state_changed,
        ) in subscribed

    @pytest.mark.asyncio
    async def test_the_pause_does_not_change_the_user_switch(self, sm):
        await _hold(sm, held=True)
        assert sm._speech_enabled is True

        sm._speech_enabled = False
        await _hold(sm, held=False)
        assert sm._speech_enabled is False
        assert sm.speech_enabled is False


class TestTheEngineAndTheButton:

    @pytest.mark.asyncio
    async def test_the_start_tells_the_engine_and_the_button(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _hold(sm, held=True)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="exclusive_speaker"
        )
        mock_websocket_manager.broadcast.assert_called_once()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["action"] == "state_update"
        assert state["speech_enabled"] is False

    @pytest.mark.asyncio
    async def test_the_end_tells_the_engine_and_the_button(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _hold(sm, held=False)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            True, reason=None
        )
        mock_websocket_manager.broadcast.assert_called_once()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["speech_enabled"] is True

    @pytest.mark.asyncio
    async def test_a_repeat_event_tells_nobody(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _hold(sm, held=True)

        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_reason_never_arms_the_wake_word(self, sm):
        """A pause that ends while the exclusive pause holds names the exclusive reason."""
        sm._speech_enabled = False  # toggle mode, user switch off: reason would be "manual"
        await _hold(sm, held=True)

        assert sm._engine_status_reason(False, None) == "exclusive_speaker"

    @pytest.mark.asyncio
    async def test_another_pause_ending_keeps_the_exclusive_reason(
        self, sm, mock_websocket_manager
    ):
        sm.set_speech_suppressed_by_audio(True)
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        sm.set_speech_suppressed_by_audio(False)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="exclusive_speaker"
        )


class TestNothingEndsThePauseButTheRelease:

    @pytest.mark.asyncio
    async def test_a_wake_word_in_toggle_mode_does_not_end_the_pause(self, sm):
        sm._speech_enabled = False
        await _hold(sm, held=True)

        await _wake_word(sm)

        assert sm.speech_enabled is False
        assert sm._speech_suppressed_by_exclusive_speaker is True
        # The wake word does not switch the user's own setting on either.
        assert sm._speech_enabled is False

    @pytest.mark.asyncio
    async def test_a_wake_word_in_push_to_talk_mode_does_not_end_the_pause(self, sm):
        sm.set_speech_interaction_mode("push_to_talk")
        await _hold(sm, held=True)

        await _wake_word(sm)

        assert sm._speech_suppressed_by_exclusive_speaker is True
        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "user_on_before, toggled_during, on_after_release",
        [
            pytest.param(True, False, True, id="on-stays-on"),
            pytest.param(False, False, False, id="off-stays-off"),
            pytest.param(True, True, True, id="on-then-button"),
            pytest.param(False, True, True, id="off-then-button-turns-on"),
        ],
    )
    async def test_the_users_switch_does_not_end_the_pause(
        self, sm, user_on_before, toggled_during, on_after_release
    ):
        sm._speech_enabled = user_on_before
        await _hold(sm, held=True)
        assert sm.speech_enabled is False

        if toggled_during:
            sm.toggle_speech_enabled_state()
            assert sm.speech_enabled is False
            assert sm._speech_suppressed_by_exclusive_speaker is True

        await _hold(sm, held=False)

        assert sm.speech_enabled is on_after_release

    @pytest.mark.asyncio
    async def test_push_to_talk_gets_no_exception(self, sm):
        sm._speech_enabled = False
        await _hold(sm, held=True)

        sm.ptt_start(source="test")

        assert sm._ptt_active is True
        assert sm.speech_enabled is False
        reason = sm._ptt_refusal_reason()
        assert reason
        assert "speaker" in reason.lower()

    @pytest.mark.asyncio
    async def test_the_push_to_talk_audio_override_does_not_lift_it(self, sm):
        await _hold(sm, held=True)
        sm._ptt_audio_override = True

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_push_to_talk_start_tells_the_engine_listening_stays_off(
        self, sm, mock_websocket_manager
    ):
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        sm.ptt_start(source="test")

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="ptt"
        )


class TestTheSettingGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("echo_canceller_decision", [True, False])
    async def test_the_pause_applies_whatever_the_startup_decision_is(
        self, sm, echo_canceller_decision
    ):
        sm.apply_audio_suppression_decision(echo_canceller_decision)
        sm.apply_exclusive_speaker_pause_gate(True)

        await _hold(sm, held=True)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_the_pause_is_on_when_nothing_sets_the_gate(self, sm):
        sm.apply_audio_suppression_decision(False)

        await _hold(sm, held=True)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_the_pause_does_not_apply_when_the_gate_is_off(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        sm.apply_exclusive_speaker_pause_gate(False)
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _hold(sm, held=True)

        assert sm.speech_enabled is True
        assert sm._engine_status_reason(False, None) != "exclusive_speaker"
        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wake_word_with_the_gate_off_works_as_before(self, sm):
        sm.apply_exclusive_speaker_pause_gate(False)
        sm._speech_enabled = False
        await _hold(sm, held=True)

        await _wake_word(sm)

        assert sm.speech_enabled is True

    @pytest.mark.asyncio
    async def test_the_gate_off_pause_does_not_block_push_to_talk(self, sm):
        sm.apply_exclusive_speaker_pause_gate(False)
        sm._speech_enabled = False
        await _hold(sm, held=True)

        sm.ptt_start(source="test")

        assert sm.speech_enabled is True
        assert sm._ptt_refusal_reason() == ""


class TestTheLogLines:

    @pytest.mark.asyncio
    async def test_one_info_line_at_the_start_and_one_at_the_end(self, sm, caplog):
        with caplog.at_level(logging.INFO, logger="state_manager"):
            await _hold(sm, held=True)
            start_lines = [r for r in caplog.records if r.levelno == logging.INFO]
            caplog.clear()
            await _hold(sm, held=False)
            end_lines = [r for r in caplog.records if r.levelno == logging.INFO]

        for lines in (start_lines, end_lines):
            assert len(lines) == 1
            text = lines[0].getMessage()
            # The exact fragments, so the endpoint and the method cannot swap places.
            assert f"'{ENDPOINT}'" in text
            assert f"(detected by {METHOD})" in text
        assert "paused" in start_lines[0].getMessage().lower()
        assert "resum" in end_lines[0].getMessage().lower() or "ended" in end_lines[0].getMessage().lower()

    @pytest.mark.asyncio
    async def test_a_repeat_event_logs_nothing(self, sm, caplog):
        await _hold(sm, held=True)
        with caplog.at_level(logging.INFO, logger="state_manager"):
            # The first hold logs its start line whenever an earlier test left
            # INFO on; count only what the repeat events log.
            caplog.clear()
            await _hold(sm, held=True)
            await _hold(sm, held=True, method="session disconnect")

        assert [r for r in caplog.records if r.levelno >= logging.INFO] == []

    @pytest.mark.asyncio
    async def test_a_release_with_no_hold_logs_nothing(self, sm, caplog):
        with caplog.at_level(logging.INFO, logger="state_manager"):
            await _hold(sm, held=False)

        assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


class TestOtherPausesStillHold:

    @pytest.mark.asyncio
    async def test_the_sound_pause_still_pauses_after_the_exclusive_pause_ends(self, sm):
        sm.set_speech_suppressed_by_audio(True)
        await _hold(sm, held=True)
        await _hold(sm, held=False)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_the_sonos_pause_still_pauses_after_the_exclusive_pause_ends(self, sm):
        sm._set_speech_suppressed_by_sonos(True)
        await _hold(sm, held=True)
        await _hold(sm, held=False)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_the_idle_pause_still_pauses_after_the_exclusive_pause_ends(self, sm):
        await sm._handle_idle_state_changed(
            SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
        )
        await _hold(sm, held=True)
        await _hold(sm, held=False)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_the_exclusive_pause_still_pauses_after_another_pause_ends(self, sm):
        sm.set_speech_suppressed_by_audio(True)
        await _hold(sm, held=True)
        sm.set_speech_suppressed_by_audio(False)

        assert sm.speech_enabled is False


# The reasons on which the provider arms its wake-word detector, in either
# mode (services/stt_providers/shared/shared_stt/wake_word_detector.py,
# WAKE_WORD_ARMED_REASONS). On a wake word the provider turns transcription
# back on by itself, so a disable that names one of these during the
# exclusive pause lets speech through.
ARMED_REASONS = {"idle", "manual", "startup", "audio", "sonos"}


async def _start_sound_pause(sm):
    sm.set_speech_suppressed_by_audio(True)


async def _start_sonos_pause(sm):
    sm._set_speech_suppressed_by_sonos(True)


async def _start_idle_pause(sm):
    await sm._handle_idle_state_changed(
        SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
    )


async def _press_the_button(sm):
    sm.toggle_speech_enabled_state()


async def _switch_the_mode(sm):
    other = "toggle" if sm._speech_interaction_mode == "push_to_talk" else "push_to_talk"
    sm.set_speech_interaction_mode(other)


class TestNoSendArmsTheWakeWordDuringThePause:
    """Every disable sent during the pause names a reason that arms nothing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["toggle", "push_to_talk"])
    @pytest.mark.parametrize(
        "action",
        [_start_sound_pause, _start_sonos_pause, _start_idle_pause,
         _press_the_button, _switch_the_mode],
        ids=["sound-pause", "sonos-pause", "idle-pause", "button", "mode-switch"],
    )
    async def test_the_send_names_no_armed_reason(
        self, sm, mock_websocket_manager, mode, action
    ):
        sm._speech_interaction_mode = mode
        sm._audio_suppression_active = True
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        await action(sm)

        sends = mock_websocket_manager.set_transcription_status.call_args_list
        armed = [
            c for c in sends
            if c.args[0] is False and c.kwargs.get("reason") in ARMED_REASONS
        ]
        assert armed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action",
        [_start_sound_pause, _start_sonos_pause, _start_idle_pause],
        ids=["sound-pause", "sonos-pause", "idle-pause"],
    )
    async def test_another_pause_start_tells_the_engine_the_exclusive_reason(
        self, sm, mock_websocket_manager, action
    ):
        """Keeps the test above honest: each start path sends a status."""
        sm._audio_suppression_active = True
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        await action(sm)

        mock_websocket_manager.set_transcription_status.assert_called_with(
            False, reason="exclusive_speaker"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["voice", "button"])
    async def test_stop_listening_during_the_pause_tells_the_engine_the_exclusive_reason(
        self, sm, mock_websocket_manager, source
    ):
        """The "stop listening" command reaches disable_speech_by_user while paused.

        The button cannot reach it during the pause (listening is already off,
        so the toggle takes its "turn on" branch), so the call is made directly.
        """
        sm._speech_enabled = True
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        sm.disable_speech_by_user(source)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="exclusive_speaker"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["toggle", "push_to_talk"])
    async def test_a_wake_word_during_the_pause_turns_the_engine_off_again(
        self, sm, mock_websocket_manager, mode
    ):
        """The provider may already have turned itself on for the wake word."""
        sm._speech_interaction_mode = mode
        await _hold(sm, held=True)
        mock_websocket_manager.reset_mock()

        await _wake_word(sm)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="exclusive_speaker"
        )
        mock_websocket_manager.broadcast.assert_called_once()
