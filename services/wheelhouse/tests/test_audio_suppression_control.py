"""Tests for what a sound pause does, and what it tells the user.

When sound plays above the speaker peak threshold, WheelHouse tells the
provider to stop transcribing. The pause ends when the sound stops, and
nothing the user says can shorten it.

Covers:
- a wake word during a sound pause leaves listening off and publishes
  nothing, while one wake word still ends an idle pause
- the state manager carries no recovery window
- a pause sends no notification (the listening-paused notice was removed by
  wh-audio-pause-notice-repeats)
- the user's own speech-off notice

wh-audio-suppression-auto removed the setter set_audio_suppression_enabled,
its notices and the ENABLE_AUDIO_SUPPRESSION key of the state update (stage
1), then the 15-second recovery window a wake word used to open during a
sound pause (stage 2), together with their tests. The window existed to
carry one command, "audio suppression off", and stage 1 removed that
command.
"""

import asyncio
from unittest.mock import Mock

import pytest

import state_manager
from state_manager import StateManager
from events import WakeWordDetectedEvent


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
    """A StateManager whose loop never runs, so nothing it schedules fires."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()  # prevent actual task creation
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


def _sound_starts(sm):
    """The audio monitor reports sound above the threshold."""
    sm.set_speech_suppressed_by_audio(True)


async def _wake_word(sm, keyword="computer"):
    await sm._handle_wake_word_detected(WakeWordDetectedEvent(keyword=keyword))


# -----------------------------------------------------------------------
# A wake word does not end a sound pause (wh-audio-suppression-auto)
# -----------------------------------------------------------------------

class TestWakeWordDuringASoundPause:
    """The sound pause runs to its end; a wake word cannot shorten it.

    wh-audio-suppression-auto removed the recovery window. It existed to
    carry one command, "audio suppression off", and that command is gone,
    so a window that reopened listening while the speakers still played
    would only feed the recogniser the sound.
    """

    @pytest.mark.asyncio
    async def test_a_wake_word_during_a_sound_pause_leaves_listening_off(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        _sound_starts(sm)
        assert sm.speech_enabled is False
        mock_websocket_manager.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _wake_word(sm)

        assert sm.speech_enabled is False
        mock_websocket_manager.broadcast.assert_not_called()
        mock_websocket_manager.set_transcription_status.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wake_word_during_idle_and_sound_pauses_clears_only_idle(
        self, sm
    ):
        """One wake word still ends the idle pause and only that one.

        The audio monitor owns ``_speech_suppressed_by_audio`` and the
        sound is still playing, so nothing here writes that field.
        """
        _sound_starts(sm)
        sm._speech_suppressed_by_idle = True

        await _wake_word(sm)

        assert sm._speech_suppressed_by_idle is False
        assert sm._speech_suppressed_by_audio is True
        assert sm.speech_enabled is False

    def test_the_state_manager_has_no_recovery_window(self, sm):
        assert not hasattr(StateManager, "audio_recovery_window_open")
        assert not hasattr(state_manager, "AUDIO_RECOVERY_WINDOW_SECONDS")
        assert not hasattr(sm, "_audio_recovery_override")


# -----------------------------------------------------------------------
# A pause sends no notice (wh-audio-pause-notice-repeats)
# -----------------------------------------------------------------------

class TestNoPauseNotice:

    def test_a_pause_sends_no_notification(self, sm):
        """Sound pausing listening tells the user nothing.

        David removed the listening-paused notice on 2026-09-16: every dip in
        the sound ended one pause and started the next, so the notice
        repeated. The tray checkmark and the floating button still show the
        pause.
        """
        sm.set_wake_word_available(True)

        _sound_starts(sm)

        assert sm.speech_enabled is False
        sm.speech_notifier._send_notification.assert_not_called()

    def test_the_detector_is_unavailable_until_a_provider_says_otherwise(self, sm):
        assert sm._wake_word_available is False


# -----------------------------------------------------------------------
# The user's own speech-off
# -----------------------------------------------------------------------

class TestSpeechOffNotice:

    def test_the_user_toggling_speech_off_is_told_why(self, sm):
        sm.toggle_speech_enabled_state()

        sm.speech_notifier._send_notification.assert_called_once_with(
            "Wheelhouse", "Speech is off. You switched it off."
        )

    def test_the_user_toggling_speech_on_gets_no_speech_off_notice(self, sm):
        sm._speech_enabled = False

        sm.toggle_speech_enabled_state()

        sm.speech_notifier._send_notification.assert_not_called()
