"""In toggle mode, every speech-off message keeps the wake word armed.

wh-wake-word-stop-listening.1.1. The STT provider arms its wake-word
detector from the reason in the last set_transcription_status message it
received (shared_stt.wake_word_detector.should_listen_for_wake_word). With
listening off in toggle mode, a later message that disables transcription
with no reason, or with reason "ptt", disarmed the detector, so the wake word
could no longer switch listening back on.

Each test drives the real StateManager against the real WebSocketManager and
reads the last message it produced, which is the message the live provider
holds. The test also compares that message with the one a reconnecting
provider is sent (get_current_status_message), so the live and the
reconnecting provider cannot disagree.

Push-to-talk interaction mode keeps its old reasons (boss ruling R4 and
option 2): its release still sends "ptt".
"""

import asyncio
from unittest.mock import Mock

import pytest

from events import SystemIdleStateChangedEvent
from integrations.websocket_manager import WebSocketManager
from state_manager import StateManager

try:  # The shared STT package is not installed in the wheelhouse venv.
    from shared_stt.wake_word_detector import should_listen_for_wake_word
except ImportError:  # pragma: no cover - depends on the venv
    _ARMED = ("idle", "manual", "startup", "audio", "sonos")

    def should_listen_for_wake_word(mode, reason):
        return reason is not None and reason in _ARMED


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    yield loop
    loop.close()


@pytest.fixture
def engine(loop):
    """The real manager, recording every status message it returns."""
    ws = WebSocketManager(loop)
    ws.broadcast = Mock()
    ws.sent = []
    real = ws.set_transcription_status

    def recording(enabled, reason=None):
        message = real(enabled, reason=reason)
        ws.sent.append(message)
        return message

    ws.set_transcription_status = recording
    return ws


def _make(mock_config, mock_event_bus, mock_gui_queue, loop, engine,
          mode="toggle", enabled=None):
    mock_config._config["speech"] = {"interaction_mode": mode}
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=None,
    )
    if enabled is not None:
        mgr._speech_enabled = enabled
    elif mode == "toggle":
        mgr._speech_enabled = True
    # Push-to-talk mode starts idle; the constructor already set False.
    mgr.speech_notifier._send_notification = Mock()
    mgr.websocket_manager = engine
    return mgr


@pytest.fixture
def make(mock_config, mock_event_bus, mock_gui_queue, loop, engine):
    def _factory(**kwargs):
        return _make(mock_config, mock_event_bus, mock_gui_queue, loop, engine, **kwargs)
    return _factory


def _assert_armed(sm, engine):
    last = engine.sent[-1]
    assert sm.speech_enabled is False
    assert last["enabled"] is False
    assert should_listen_for_wake_word("idle_recovery", last.get("reason")), last
    # A provider that reconnects now is told the same thing.
    assert engine.get_current_status_message() == last


def _switch_off(sm, how):
    if how == "toggle":
        sm.toggle_speech_enabled_state()
    else:
        sm.disable_speech_by_user("Stop listening")


async def _idle(sm, is_idle):
    await sm._handle_idle_state_changed(
        SystemIdleStateChangedEvent(is_idle=is_idle, idle_duration_seconds=600.0)
    )


@pytest.mark.parametrize("how", ["toggle", "stop-listening"])
class TestAPauseThatEndsWhileListeningIsOff:
    """Sequences A, B and C of the finding."""

    def test_a_sound_starts_and_stops(self, make, engine, how):
        sm = make()
        _switch_off(sm, how)
        sm.set_speech_suppressed_by_audio(True)
        sm.set_speech_suppressed_by_audio(False)
        _assert_armed(sm, engine)

    async def test_b_idle_pause_and_the_user_returns(self, make, engine, how):
        sm = make()
        _switch_off(sm, how)
        await _idle(sm, True)
        await _idle(sm, False)
        _assert_armed(sm, engine)

    def test_c_sonos_starts_and_stops(self, make, engine, how):
        sm = make()
        _switch_off(sm, how)
        sm._set_speech_suppressed_by_sonos(True)
        sm._set_speech_suppressed_by_sonos(False)
        _assert_armed(sm, engine)


def test_a_after_a_start_with_listening_off(make, engine):
    sm = make(enabled=False)
    assert engine.sent[-1].get("reason") == "startup"
    sm.set_speech_suppressed_by_audio(True)
    sm.set_speech_suppressed_by_audio(False)
    _assert_armed(sm, engine)


def test_sound_ends_while_sonos_still_plays(make, engine):
    """Listening on, two pauses overlap, the first one ends."""
    sm = make()
    sm.set_speech_suppressed_by_audio(True)
    sm._set_speech_suppressed_by_sonos(True)
    sm.set_speech_suppressed_by_audio(False)
    _assert_armed(sm, engine)
    assert engine.sent[-1]["reason"] == "sonos"


class TestAHoldInToggleMode:
    """Sequence D: the floating-button hold has no mode gate."""

    def test_d_hold_and_release_while_off(self, make, engine):
        sm = make()
        sm.toggle_speech_enabled_state()
        sm.ptt_start()
        sm.ptt_stop()
        _assert_armed(sm, engine)

    def test_failed_mute_during_a_hold_over_sound(self, make, engine):
        sm = make()
        sm.toggle_speech_enabled_state()
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(False, sm._ptt_hold_id)
        _assert_armed(sm, engine)

    def test_recheck_after_a_hold_over_sound(self, make, engine):
        sm = make()
        sm.toggle_speech_enabled_state()
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        sm.ptt_stop()
        _assert_armed(sm, engine)
        sm._ptt_audio_recheck()
        _assert_armed(sm, engine)

    def test_release_told_off_for_the_holds_own_mute(self, make, engine):
        """Listening on, the sound's silence may be the hold's own mute."""
        sm = make()
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        sm.set_speech_suppressed_by_audio(False)
        sm.ptt_stop()
        last = engine.sent[-1]
        assert last["enabled"] is False
        assert should_listen_for_wake_word("idle_recovery", last.get("reason")), last
        assert engine.get_current_status_message() == last


class TestLeavingPushToTalk:
    """Sequence E: a switch to toggle mode while listening is off."""

    def test_e_switch_by_voice_during_a_hold_then_release(self, make, engine):
        sm = make(mode="push_to_talk")
        sm.ptt_start()
        sm.set_speech_interaction_mode("toggle")
        sm.ptt_stop()
        _assert_armed(sm, engine)

    def test_e_switch_between_holds(self, make, engine):
        sm = make(mode="push_to_talk")
        sm.ptt_start()
        sm.ptt_stop()
        assert engine.sent[-1]["reason"] == "ptt"
        sm.set_speech_interaction_mode("toggle")
        _assert_armed(sm, engine)


class TestPushToTalkModeUnchanged:
    """R4: push-to-talk interaction mode sends what it sent on eb2b2f7b."""

    def test_release_still_sends_ptt(self, make, engine):
        sm = make(mode="push_to_talk")
        sm.ptt_start()
        sm.ptt_stop()
        assert engine.sent[-1] == {
            "type": "set_transcription_status", "enabled": False, "reason": "ptt"}

    def test_sound_ending_between_holds_still_sends_no_reason(self, make, engine):
        sm = make(mode="push_to_talk")
        sm.set_speech_suppressed_by_audio(True)
        sm.set_speech_suppressed_by_audio(False)
        assert engine.sent[-1] == {"type": "set_transcription_status", "enabled": False}
