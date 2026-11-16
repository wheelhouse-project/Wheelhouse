"""The wake word does not turn listening on while the computer plays sound.

wh-playback-disable-fails.1. David chose option 2 on 2026-11-10: the wake
word "computer" must not turn speech on while the computer plays sound. A
video that says the wake word turned listening on six times in 146 s on
2026-11-09, and the audio monitor did not start the sound pause again until
it saw quiet and then new sound. This changes part of ruling R1 of
wh-wake-word-stop-listening: the wake word still ends every other pause.

Covers the acceptance items of wh-playback-disable-fails.1:
- item 1: during the sound pause the wake word changes no flag, and
  listening stays off, also when another pause holds it off too
- item 2: with no sound pause the wake word turns listening on and clears
  every pause, as before
- item 3: the speech toggle still turns listening on during the sound pause
- item 4: one log line names the wake word and the reason; no notice
"""

import asyncio
import logging
from unittest.mock import Mock

import pytest

from events import SystemIdleStateChangedEvent, WakeWordDetectedEvent
from state_manager import StateManager


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
    mgr._audio_suppression_active = True
    mgr.speech_notifier._send_notification = Mock()
    yield mgr
    loop.close()


def _sound_starts(sm):
    sm.set_speech_suppressed_by_audio(True)


def _user_switches_off(sm):
    sm.toggle_speech_enabled_state()


async def _idle_starts(sm):
    await sm._handle_idle_state_changed(
        SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
    )


def _sonos_starts(sm):
    sm._set_speech_suppressed_by_sonos(True)


async def _run(sm, steps):
    for step in steps:
        result = step(sm)
        if asyncio.iscoroutine(result):
            await result


async def _wake_word(sm, keyword="computer"):
    await sm._handle_wake_word_detected(WakeWordDetectedEvent(keyword=keyword))


def _flags(sm):
    return {
        "user": sm._speech_enabled,
        "audio": sm._speech_suppressed_by_audio,
        "sonos": sm._speech_suppressed_by_sonos,
        "idle": sm._speech_suppressed_by_idle,
        "before_idle": sm._speech_enabled_before_idle,
    }


# Each way the sound pause can be active when the wake word comes: alone,
# or together with another pause that also holds listening off.
_SOUND_PAUSE_STATES = [
    pytest.param([_sound_starts], id="sound"),
    pytest.param([_user_switches_off, _sound_starts], id="user-off-then-sound"),
    pytest.param([_idle_starts, _sound_starts], id="idle-then-sound"),
    pytest.param([_sonos_starts, _sound_starts], id="sonos-then-sound"),
]


class TestWakeWordDuringTheSoundPause:
    """Acceptance item 1."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", _SOUND_PAUSE_STATES)
    async def test_listening_stays_off(self, sm, steps):
        await _run(sm, steps)
        assert sm.speech_enabled is False

        await _wake_word(sm)

        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", _SOUND_PAUSE_STATES)
    async def test_every_pause_flag_keeps_its_value(self, sm, steps):
        await _run(sm, steps)
        before = _flags(sm)

        await _wake_word(sm)

        assert _flags(sm) == before

    @pytest.mark.asyncio
    async def test_the_engine_is_told_listening_stays_off_for_the_sound(
        self, sm, mock_websocket_manager
    ):
        """The provider turns transcription on by itself when it hears the
        wake word, so WheelHouse tells it again that listening is off. The
        reason "audio" keeps the detector armed, as ruling R2 says."""
        _sound_starts(sm)
        mock_websocket_manager.reset_mock()

        await _wake_word(sm)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="audio"
        )
        sm.loop.create_task.assert_called()

    @pytest.mark.asyncio
    async def test_a_wake_word_after_the_sound_stops_turns_listening_on(self, sm):
        _sound_starts(sm)
        await _wake_word(sm)
        sm.set_speech_suppressed_by_audio(False)
        _user_switches_off(sm)
        assert sm.speech_enabled is False

        await _wake_word(sm)

        assert sm.speech_enabled is True


class TestWakeWordWithoutTheSoundPause:
    """Acceptance item 2: the wake word behaves as before (ruling R1)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param([_user_switches_off], id="user-off"),
            pytest.param([_idle_starts], id="idle"),
            pytest.param([_sonos_starts], id="sonos"),
            pytest.param([_user_switches_off, _sonos_starts], id="user-off-and-sonos"),
        ],
    )
    async def test_the_wake_word_turns_listening_on_and_clears_every_pause(
        self, sm, steps
    ):
        await _run(sm, steps)
        assert sm.speech_enabled is False

        await _wake_word(sm)

        assert sm.speech_enabled is True
        assert _flags(sm) == {
            "user": True, "audio": False, "sonos": False,
            "idle": False, "before_idle": None,
        }

    @pytest.mark.asyncio
    async def test_with_the_startup_decision_off_sound_does_not_stop_the_wake_word(
        self, sm
    ):
        """With audio suppression off at startup the sound pause never holds
        listening off, so the wake word ends the user's switch-off as before
        and clears the stale sound flag with every other pause."""
        sm._audio_suppression_active = False
        _sound_starts(sm)
        _user_switches_off(sm)
        assert sm.speech_enabled is False

        await _wake_word(sm)

        assert sm.speech_enabled is True
        assert sm._speech_suppressed_by_audio is False


class TestTheToggleDuringTheSoundPause:
    """Acceptance item 3. Every GUI route (the floating button, the tray
    click, the tray menu and the settings dialog) sends the action
    toggle_speech_enabled_state (gui.py send_toggle_speech_command), and
    main.py maps that action to StateManager.toggle_speech_enabled_state."""

    @pytest.mark.parametrize("steps", _SOUND_PAUSE_STATES)
    @pytest.mark.asyncio
    async def test_the_toggle_turns_listening_on(self, sm, steps):
        await _run(sm, steps)
        assert sm.speech_enabled is False

        sm.toggle_speech_enabled_state()

        assert sm.speech_enabled is True
        assert sm._speech_suppressed_by_audio is False


class TestTheIgnoredWakeWordIsLoggedOnly:
    """Acceptance item 4."""

    @pytest.mark.asyncio
    async def test_one_log_line_names_the_wake_word_and_the_sound(self, sm, caplog):
        _sound_starts(sm)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="state_manager"):
            await _wake_word(sm, keyword="computer")

        lines = [r.getMessage() for r in caplog.records if "computer" in r.getMessage()]
        assert len(lines) == 1
        assert "ignor" in lines[0].lower()
        assert "plays sound" in lines[0]

    @pytest.mark.asyncio
    async def test_no_notice_and_no_gui_state_change(self, sm, mock_gui_queue):
        _sound_starts(sm)
        sm.speech_notifier._send_notification.reset_mock()
        mock_gui_queue.put_nowait.reset_mock()

        await _wake_word(sm)

        sm.speech_notifier._send_notification.assert_not_called()
        mock_gui_queue.put_nowait.assert_not_called()


class TestWakeWordDuringTheSoundPauseInPushToTalkMode:
    """Item 1 in push-to-talk mode (wh-playback-disable-fails.1.1.3).

    The sound pause holds listening off in push-to-talk mode too, so the wake
    word changes no pause flag there, and the provider is told again that
    listening is off with the reason "audio", which the sound pause sent when
    it started (set_speech_suppressed_by_audio). Before this, the
    push-to-talk wake word cleared the idle pause during the sound pause and
    sent no status when only the sound pause held listening off.
    """

    @pytest.fixture
    def ptt(self, sm):
        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"
        return sm

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", [
        pytest.param([_sound_starts], id="sound"),
        pytest.param([_idle_starts, _sound_starts], id="idle-then-sound"),
        pytest.param([_sound_starts, _idle_starts], id="sound-then-idle"),
    ])
    async def test_every_pause_flag_keeps_its_value_in_push_to_talk(
        self, ptt, steps
    ):
        await _run(ptt, steps)
        before = _flags(ptt)
        assert ptt.speech_enabled is False

        await _wake_word(ptt)

        assert _flags(ptt) == before
        assert ptt.speech_enabled is False
        assert ptt._speech_interaction_mode == "push_to_talk"

    @pytest.mark.asyncio
    async def test_the_engine_is_told_listening_stays_off_in_push_to_talk(
        self, ptt, mock_websocket_manager
    ):
        _sound_starts(ptt)
        mock_websocket_manager.reset_mock()

        await _wake_word(ptt)

        mock_websocket_manager.set_transcription_status.assert_called_once_with(
            False, reason="audio"
        )

    @pytest.mark.asyncio
    async def test_one_log_line_names_the_sound_in_push_to_talk(self, ptt, caplog):
        _sound_starts(ptt)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="state_manager"):
            await _wake_word(ptt, keyword="computer")

        lines = [r.getMessage() for r in caplog.records if "computer" in r.getMessage()]
        assert len(lines) == 1
        assert "plays sound" in lines[0]

    @pytest.mark.asyncio
    async def test_a_wake_word_during_a_hold_leaves_the_hold_on(
        self, ptt, mock_websocket_manager
    ):
        """A push-to-talk hold lifts the sound pause (the hold mutes the
        speakers), so the wake word must not tell the provider that
        listening is off in the middle of the hold."""
        _sound_starts(ptt)
        ptt.ptt_start()
        assert ptt.speech_enabled is True
        mock_websocket_manager.reset_mock()

        await _wake_word(ptt)

        assert ptt.speech_enabled is True
        mock_websocket_manager.set_transcription_status.assert_not_called()


class TestTheStopListeningHelpNamesTheSoundPause:
    """Item 5: the Stop listening help in the action catalog (the Pattern
    Manager and the Action Reference read it) names the sound pause, as every
    other help text does (wh-playback-disable-fails.1.1.2)."""

    def test_the_catalog_entry_names_the_sound_pause_and_the_toggle(self):
        from speech import action_catalog

        entries = [e for e in action_catalog.ACTION_CATALOG
                   if e["name"] == "stop_listening"]
        assert len(entries) == 1
        summary = entries[0]["summary"]

        assert "sound that this computer plays" in summary, summary
        assert "tray icon instead" in summary, summary
