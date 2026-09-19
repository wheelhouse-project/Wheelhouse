"""Push-to-talk release must not overwrite the user's own setting.

wh-ptt-release-disables-speech. David reported this on 2026-08-27.

He was playing sound through the speakers with speech switched on. The audio
monitor suppressed listening, which is correct: the microphone would otherwise
hear the speakers. He then held the push-to-talk button, spoke, and released
it. The sound stopped a few seconds later, but listening did not come back. He
had to click the button.

The log in wheelhouse.log.3 shows why:

    11:48:48  audio starts, suppression engages, user_enabled=True
    11:49:04  hold starts, mute confirmed, dictation works
    11:49:08  release: "Transcription status set to: DISABLED (reason=ptt)"
    11:49:18  the audio monitor lifts suppression, user_enabled=False

There is no [USER TOGGLE] line between 11:48:48 and 11:49:18, so nothing the
user did turned the setting off. ``StateManager.ptt_stop`` did it: an ordinary
release forced ``_speech_enabled`` to False. When suppression lifted ten
seconds later there was no enabled setting left to un-suppress.

A hold borrows the microphone for its own length. It does not decide whether
speech is on afterwards. The release therefore puts back exactly the value
that was there before the hold, and lets audio suppression, Sonos suppression
and the idle pause decide the rest.

The safety cutoff restores the pre-hold setting as well, from David's ruling
of 2026-08-27. It used to force speech off, on the reading that a hold the
user never released carried no decision to honour. A lost release is not a
decision to switch speech off either, so the cutoff now ends the hold exactly
where a release and both cancellations end it, and audio suppression, Sonos
suppression and the idle pause decide the rest.

wh-ptt-release-disables-speech.1.1 is the second half of this file. A hold
mutes the speakers, and when plugins.system_volume.device_type is "default" the
audio monitor meters the same endpoint the mute turns down, so a monitor poll
landing inside the hold reports silence that the music never produced. When it
is "communications" the plugin asks Windows for the communications render
endpoint by role. Where Windows supplies a distinct one, the monitor still
reads the multimedia default, which is wh-ptt-comms-endpoint-mismatch rather
than this defect; where that role is unavailable or names the multimedia
default, the plugin drives the metered endpoint too and the case above
applies. The release cannot tell that report apart from the music really
ending, so it stops trusting it: a hold that began over playing sound with the
mute confirmed tells the engine off, and half a second later a re-check sends
the computed speech state again, whatever the monitor has published by then. A
new hold cancels that re-check. Nothing orders the wait after the volume
restore, so the re-check can still read the mute's own silence; the docstring
of _ptt_audio_recheck gives the limit in full.
"""
import asyncio
from unittest.mock import Mock

import pytest

from state_manager import StateManager


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
    """A StateManager whose event loop never runs a real task."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    yield mgr
    loop.close()


def _told(mock_websocket_manager):
    """Return the on-or-off value the speech engine was last told."""
    return mock_websocket_manager.set_transcription_status.call_args[0][0]


class TestReleaseRestoresTheSetting:
    def test_release_restores_the_setting_after_a_suppressed_hold(self, sm):
        """The exact case David reported.

        Speech is on, sound is playing, so listening is suppressed. The hold
        and the release must leave the setting exactly as they found it.
        """
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        assert sm.speech_enabled is False, "audio suppression should be in force"

        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is True

    def test_listening_returns_when_the_sound_stops_with_no_click(self, sm):
        """The whole sequence David walked through, end to end."""
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        sm.ptt_stop(reason="released")

        # Still suppressed: the sound has not stopped yet.
        assert sm.speech_enabled is False

        # The audio monitor reports silence. Nothing else happens -- no click,
        # no toggle.
        sm.set_speech_suppressed_by_audio(False)

        assert sm.speech_enabled is True

    def test_release_leaves_speech_off_when_it_was_off_before_the_hold(self, sm):
        """Push-to-talk mode is unchanged.

        Speech is off between holds there, so the restored value is False and
        the release ends exactly where it used to.
        """
        sm._speech_enabled = False
        sm.ptt_start()
        assert sm._speech_enabled is True, "the hold itself switches speech on"

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is False
        assert sm.speech_enabled is False

    def test_the_engine_is_told_the_real_answer_while_the_sound_plays(
        self, sm, mock_websocket_manager
    ):
        """Restoring the setting must not start the engine listening.

        The setting goes back to on, but the speakers are still playing, so
        the answer the speech engine gets is still off.
        """
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is True
        assert _told(mock_websocket_manager) is False

    def test_the_engine_is_told_speech_is_on_when_nothing_suppresses_it(
        self, sm, mock_websocket_manager
    ):
        """A release with no suppression in force leaves the engine listening."""
        sm._speech_enabled = True
        sm.ptt_start()

        sm.ptt_stop(reason="released")

        assert sm.speech_enabled is True
        assert _told(mock_websocket_manager) is True

    def test_the_state_update_shows_the_restored_setting(self, sm, mock_gui_queue):
        """The button must not show speech off after the release."""
        sm._speech_enabled = True
        sm.ptt_start()
        mock_gui_queue.put_nowait.reset_mock()

        sm.ptt_stop(reason="released")

        updates = [
            c[0][0]
            for c in mock_gui_queue.put_nowait.call_args_list
            if c[0][0].get("action") == "state_update"
        ]
        assert updates, "the release sent no state update"
        assert updates[-1]["speech_enabled"] is True
        assert updates[-1]["ptt_active"] is False


class TestTheSafetyCutoffRestoresTheSettingToo:
    """David's ruling of 2026-08-27.

    The cutoff ends a hold whose release never arrived. That is a lost
    release, not a decision to switch speech off, so the cutoff restores the
    pre-hold setting exactly as the release and both cancellations do.
    """

    def test_the_safety_cutoff_restores_the_pre_hold_setting(self, sm):
        sm._speech_enabled = True
        sm.ptt_start()

        sm._ptt_safety_timeout()

        assert sm._ptt_active is False
        assert sm._speech_enabled is True

    def test_the_safety_cutoff_keeps_speech_off_when_it_was_off(self, sm):
        """Push-to-talk mode is unchanged: speech is off between holds."""
        sm._speech_enabled = False
        sm.ptt_start()

        sm._ptt_safety_timeout()

        assert sm._speech_enabled is False

    def test_the_safety_cutoff_tells_the_engine_the_restored_answer(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = True
        sm.ptt_start()

        sm.ptt_stop(reason="safety_timeout")

        assert _told(mock_websocket_manager) is True

    def test_an_explicit_decision_during_a_held_hold_survives_the_cutoff(self, sm):
        """The cutoff restores the newest decision, not a stale one."""
        sm._speech_enabled = True
        sm.ptt_start()
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is False

        sm._ptt_safety_timeout()

        assert sm._speech_enabled is False


class TestAMonitorPollDuringTheHold:
    """wh-ptt-release-disables-speech.1.1.

    ``SystemVolumePlugin._mute_for_ptt`` drives the default render endpoint's
    master volume to ``_min_volume_db`` (-65.25 dB). ``AudioMonitor``
    (``is_audio_playing``) meters that same endpoint and calls anything under
    a peak of 0.05 silence, polls every 10 seconds while it believes sound is
    playing, and publishes only when its answer changes. A hold longer than
    one poll therefore hands the release a cleared audio-suppression flag that
    the hold's own mute produced, and nothing puts it back, because the music
    never stopped.
    """

    def _hold_over_music_with_a_poll_inside_it(self, sm, muted=True):
        """Music playing, the hold mutes it, a monitor poll lands mid-hold."""
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(muted, sm._ptt_hold_id)
        # The poll meters the muted endpoint and reports silence the music
        # never produced.
        sm.set_speech_suppressed_by_audio(False)

    @pytest.mark.parametrize(
        "reason", ["released", "drag_cancel", "gesture_cancel", "safety_timeout"]
    )
    def test_the_ending_does_not_start_the_engine_listening(
        self, sm, mock_websocket_manager, reason
    ):
        """Every ending that takes the restore shares the cell."""
        self._hold_over_music_with_a_poll_inside_it(sm)

        sm.ptt_stop(reason=reason)

        assert _told(mock_websocket_manager) is False

    def test_the_setting_itself_is_not_written(self, sm):
        """Only what the engine is told changes; the monitor owns the rest."""
        self._hold_over_music_with_a_poll_inside_it(sm)

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False

    def test_a_failed_mute_keeps_todays_answer(self, sm, mock_websocket_manager):
        """The speakers were never turned down, so the monitor's answer stands.

        ``ptt_confirm_mute(False, ...)`` withdraws the override at once, and
        the monitor is metering live speakers, so a mid-hold silence report
        there is the music really ending.
        """
        self._hold_over_music_with_a_poll_inside_it(sm, muted=False)

        sm.ptt_stop(reason="released")

        assert _told(mock_websocket_manager) is True

    def test_a_never_answered_mute_keeps_todays_answer(
        self, sm, mock_websocket_manager
    ):
        """The volume plugin is not loaded, so nothing reports at all."""
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm._ptt_mute_unconfirmed()
        sm.set_speech_suppressed_by_audio(False)

        sm.ptt_stop(reason="released")

        assert _told(mock_websocket_manager) is True

    def test_a_hold_that_began_in_silence_is_untouched(
        self, sm, mock_websocket_manager
    ):
        """No sound at the press means no mute-produced silence to distrust."""
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)

        sm.ptt_stop(reason="released")

        assert _told(mock_websocket_manager) is True
        assert sm._ptt_audio_recheck_handle is None


class TestTheRecheckAfterTheEnding:
    """The monitor cannot be waited for, so the ending asks it again.

    ``AudioMonitor`` publishes only on a change and, after a silence
    measurement, polls every 0.1 seconds. If the music really did stop during
    the hold, the monitor is already at silence and will never publish again,
    so nothing would ever tell the engine to listen. The re-check is what
    closes that.
    """

    def _held_over_music_and_released(self, sm):
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        sm.ptt_start()
        sm.ptt_confirm_mute(True, sm._ptt_hold_id)
        sm.set_speech_suppressed_by_audio(False)
        sm.ptt_stop(reason="released")

    def test_the_ending_arms_the_recheck(self, sm):
        self._held_over_music_and_released(sm)

        assert sm._ptt_audio_recheck_handle is not None

    def test_the_recheck_tells_the_engine_to_listen_when_the_sound_really_stopped(
        self, sm, mock_websocket_manager
    ):
        self._held_over_music_and_released(sm)
        assert _told(mock_websocket_manager) is False

        sm._ptt_audio_recheck()

        assert _told(mock_websocket_manager) is True

    def test_the_recheck_leaves_the_engine_off_when_the_music_came_back(
        self, sm, mock_websocket_manager
    ):
        self._held_over_music_and_released(sm)
        # The volume is back, so the monitor meters the music again.
        sm.set_speech_suppressed_by_audio(True)

        sm._ptt_audio_recheck()

        assert _told(mock_websocket_manager) is False

    def test_a_new_hold_cancels_the_recheck(self, sm):
        self._held_over_music_and_released(sm)
        handle = sm._ptt_audio_recheck_handle

        sm.ptt_start()

        assert handle.cancelled()
        assert sm._ptt_audio_recheck_handle is None

    def test_the_recheck_says_nothing_while_a_hold_is_running(
        self, sm, mock_websocket_manager
    ):
        """A late re-check must not overrule the hold it landed inside."""
        self._held_over_music_and_released(sm)
        sm.ptt_start()
        mock_websocket_manager.set_transcription_status.reset_mock()

        sm._ptt_audio_recheck()

        mock_websocket_manager.set_transcription_status.assert_not_called()


class TestCancellationsAreUnchanged:
    @pytest.mark.parametrize("reason", ["drag_cancel", "gesture_cancel"])
    def test_a_cancelled_hold_still_restores_the_setting(self, sm, reason):
        sm._speech_enabled = True
        sm.ptt_start()

        sm.ptt_stop(reason=reason)

        assert sm._speech_enabled is True

    @pytest.mark.parametrize("reason", ["drag_cancel", "gesture_cancel"])
    def test_a_cancelled_hold_still_keeps_speech_off(self, sm, reason):
        sm._speech_enabled = False
        sm.ptt_start()

        sm.ptt_stop(reason=reason)

        assert sm._speech_enabled is False


class TestAnExplicitDecisionDuringAHoldWins:
    """wh-ptt-release-disables-speech.1.2.

    The release restores the value saved at ptt_start. Nothing in
    ``services/wheelhouse/speech/`` knows a hold is running, so the shipped
    voice pattern "push to talk mode" can land while the button is still
    held. Without care the release would put back the pre-hold value and
    silently undo the switch the user just spoke.
    """

    def test_a_mode_switch_during_a_hold_survives_the_release(self, sm):
        sm._speech_enabled = True
        sm.ptt_start()

        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_enabled is False, "the mode switch turns speech off"

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is False
        assert sm._speech_interaction_mode == "push_to_talk"

    @pytest.mark.parametrize("reason", ["drag_cancel", "gesture_cancel"])
    def test_a_mode_switch_during_a_hold_survives_a_cancellation(self, sm, reason):
        """The two cancellations already had this cell before the release did."""
        sm._speech_enabled = True
        sm.ptt_start()
        sm.set_speech_interaction_mode("push_to_talk")

        sm.ptt_stop(reason=reason)

        assert sm._speech_enabled is False

    def test_the_engine_is_not_told_to_listen_after_the_switch(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = True
        sm.ptt_start()
        sm.set_speech_interaction_mode("push_to_talk")

        sm.ptt_stop(reason="released")

        assert sm.speech_enabled is False
        assert _told(mock_websocket_manager) is False

    def test_a_switch_before_the_hold_still_restores_normally(self, sm):
        """Only a switch DURING the hold moves the saved value."""
        sm._speech_enabled = True
        sm.set_speech_interaction_mode("push_to_talk")
        sm._speech_enabled = True

        sm.ptt_start()
        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is True

    def test_a_toggle_off_during_a_hold_survives_the_release(self, sm):
        """wh-ptt-release-disables-speech.1.6, the toggle-off branch.

        The tray single click in toggle mode reaches
        GuiManager.send_toggle_speech_command without checking whether a hold
        is running, and main.py maps that command straight to
        toggle_speech_enabled_state, so this is a reachable producer.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        assert sm.speech_enabled is True, "the hold holds the microphone open"

        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is False, "the toggle turns speech off"

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is False

    def test_a_toggle_on_during_a_hold_survives_the_release(self, sm):
        """wh-ptt-release-disables-speech.1.6, the toggle-on branch.

        Sonos suppression is what makes the computed answer False during the
        hold, so the toggle takes its enable branch. The push-to-talk audio
        override covers only audio suppression, so Sonos stays in force.
        """
        sm._speech_enabled = False
        sm._set_speech_suppressed_by_sonos(True)
        sm.ptt_start()
        assert sm.speech_enabled is False, "Sonos keeps the hold's answer off"

        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is True, "the toggle turns speech on"
        assert sm._speech_suppressed_by_sonos is False, "and clears suppression"

        sm.ptt_stop(reason="released")

        assert sm._speech_enabled is True
        assert sm.speech_enabled is True

    def test_the_engine_is_told_the_toggled_answer_after_the_release(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = True
        sm.ptt_start()
        sm.toggle_speech_enabled_state()

        sm.ptt_stop(reason="released")

        assert sm.speech_enabled is False
        assert _told(mock_websocket_manager) is False
