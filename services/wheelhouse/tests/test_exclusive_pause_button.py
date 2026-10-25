"""The floating button hides while an app holds the speaker in exclusive mode.

wh-kodi-pause-hide-button, Logic half. StateManager knows the pause; the
button belongs to the GUI process, so the manager sends it one snapshot key,
``button_hidden_for_exclusive_speaker``. These tests cover when that key
turns on and off:

- it turns on when the pause has lasted the wait, and off when the pause has
  stayed ended for the wait (acceptance 1 and 2)
- a pause or a gap shorter than the wait never changes it (acceptance 2)
- listening still pauses and resumes at once (acceptance 2)
- hiding and showing never write a setting (acceptance 3)
- the user's own write of FLOATING_BUTTON_VISIBLE wins (acceptance 4)
- only the exclusive-speaker pause, with its gate open, sets it (acceptance 5)
- every snapshot carries it, so a GUI that starts mid-pause hides at once
  (acceptance 6 and BOSS RULING 10:45)

The timers are driven by hand: the manager's loop is replaced by a recorder
whose ``call_later`` stores the callback, and a test fires it when it wants
the wait to be over. No test sleeps.
"""

import asyncio
from unittest.mock import Mock

import pytest

import state_manager as state_manager_module
from events import (
    SpeakerExclusiveStateChangedEvent,
    SystemIdleStateChangedEvent,
)
from state_manager import StateManager

KEY = "button_hidden_for_exclusive_speaker"
SETTING = "FLOATING_BUTTON_VISIBLE"


class _Handle:
    def __init__(self, delay, callback, args):
        self.delay = delay
        self.callback = callback
        self.args = args
        self.cancelled = False
        self.fired = False

    def cancel(self):
        self.cancelled = True

    @property
    def pending(self):
        return not self.cancelled and not self.fired

    def fire(self):
        assert self.pending, "fired a timer that was cancelled or already fired"
        self.fired = True
        self.callback(*self.args)


class _Timers:
    """Stands in for the event loop's call_later."""

    def __init__(self):
        self.handles = []

    def call_later(self, delay, callback, *args):
        handle = _Handle(delay, callback, args)
        self.handles.append(handle)
        return handle

    @property
    def pending(self):
        return [h for h in self.handles if h.pending]

    def fire_pending(self):
        pending = self.pending
        assert len(pending) == 1, f"expected one pending timer, found {len(pending)}"
        pending[0].fire()


@pytest.fixture
def timers():
    return _Timers()


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager, timers):
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    loop.call_later = timers.call_later
    mock_config.set(SETTING, True)
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


async def _hold(sm, held=True):
    await sm._handle_exclusive_speaker_state_changed(
        SpeakerExclusiveStateChangedEvent(
            held=held, endpoint_name="Speakers (Test)", method="shared-mode check"
        )
    )


def _snapshots(queue):
    return [
        c.args[0]
        for c in queue.put_nowait.call_args_list
        if c.args[0].get("action") == "state_update"
    ]


def _last(queue):
    return _snapshots(queue)[-1]


async def _hidden_pause(sm, timers):
    """A pause that has lasted the wait: the key is True."""
    await _hold(sm, True)
    timers.fire_pending()
    assert sm._button_hidden_for_exclusive_speaker is True


class TestTheHideAndTheShow:

    def test_the_wait_is_one_second(self):
        assert state_manager_module.EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS == 1.0

    @pytest.mark.asyncio
    async def test_the_key_is_false_when_nothing_is_paused(self, sm, mock_gui_queue):
        sm.send_state_update()
        assert _last(mock_gui_queue)[KEY] is False

    @pytest.mark.asyncio
    async def test_a_pause_that_lasts_the_wait_sets_the_key(
        self, sm, timers, mock_gui_queue
    ):
        await _hold(sm, True)
        assert [h.delay for h in timers.pending] == [
            state_manager_module.EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS
        ]
        assert _last(mock_gui_queue)[KEY] is False

        timers.fire_pending()

        assert _last(mock_gui_queue)[KEY] is True

    @pytest.mark.asyncio
    async def test_an_end_that_lasts_the_wait_clears_the_key(
        self, sm, timers, mock_gui_queue
    ):
        await _hidden_pause(sm, timers)

        await _hold(sm, False)
        assert [h.delay for h in timers.pending] == [
            state_manager_module.EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS
        ]
        assert _last(mock_gui_queue)[KEY] is True

        timers.fire_pending()

        assert _last(mock_gui_queue)[KEY] is False

    @pytest.mark.asyncio
    async def test_the_saved_setting_is_in_every_snapshot_during_the_pause(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await _hold(sm, False)
        timers.fire_pending()

        snapshots = _snapshots(mock_gui_queue)
        assert snapshots, "no snapshot was sent"
        assert {s["button_visible"] for s in snapshots} == {False}
        assert snapshots[-1][KEY] is False


class TestNoFlicker:

    @pytest.mark.asyncio
    async def test_a_pause_shorter_than_the_wait_never_sets_the_key(
        self, sm, timers, mock_gui_queue
    ):
        await _hold(sm, True)
        await _hold(sm, False)

        assert timers.pending == []
        assert all(h.cancelled for h in timers.handles)
        assert [s[KEY] for s in _snapshots(mock_gui_queue)] == [False, False]
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_a_gap_shorter_than_the_wait_keeps_the_key_true(
        self, sm, timers, mock_gui_queue
    ):
        await _hidden_pause(sm, timers)
        mock_gui_queue.put_nowait.reset_mock()

        await _hold(sm, False)
        await _hold(sm, True)

        assert timers.pending == []
        assert sm._button_hidden_for_exclusive_speaker is True
        assert [s[KEY] for s in _snapshots(mock_gui_queue)] == [True, True]

    @pytest.mark.asyncio
    async def test_listening_pauses_and_resumes_without_waiting(
        self, sm, timers, mock_gui_queue
    ):
        await _hold(sm, True)
        assert timers.pending, "the wait should still be running"
        assert _last(mock_gui_queue)["speech_enabled"] is False
        assert sm.speech_enabled is False

        await _hold(sm, False)
        assert _last(mock_gui_queue)["speech_enabled"] is True
        assert sm.speech_enabled is True


class TestTheUsersSetting:

    @pytest.mark.asyncio
    async def test_hiding_and_showing_never_write_a_setting(
        self, sm, timers, mock_config
    ):
        written = Mock(wraps=mock_config.set)
        mock_config.set = written
        mock_config.save.reset_mock()

        await _hidden_pause(sm, timers)
        await _hold(sm, False)
        timers.fire_pending()

        written.assert_not_called()
        mock_config.save.assert_not_called()
        assert mock_config.get(SETTING) is True

    @pytest.mark.asyncio
    async def test_a_setting_that_is_off_stays_off_through_the_pause(
        self, sm, timers, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await _hold(sm, False)
        timers.fire_pending()

        assert mock_config.get(SETTING) is False


class TestAUserChangeDuringThePause:

    async def _write_true(self, sm, path):
        if path == "set_config_value":
            await sm.set_config_value(SETTING, True)
        elif path == "staged":
            await sm.set_config_value(SETTING, True, request_id="r1")
        elif path == "group":
            await sm.set_config_values({SETTING: True, "SHOW_SPEECH_PULSE": True})
        elif path == "group_staged":
            await sm.set_config_values({SETTING: True}, request_id="r2")
        elif path == "voice":
            await sm.toggle_button_visibility()
        else:  # pragma: no cover
            raise AssertionError(path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path", ["set_config_value", "staged", "group", "group_staged", "voice"]
    )
    async def test_turning_the_button_on_shows_it_at_once(
        self, sm, timers, mock_gui_queue, mock_config, path
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        mock_gui_queue.put_nowait.reset_mock()
        # A save that stays pending until the test lets it finish: the
        # clearing snapshot must not wait for the disk.
        disk = asyncio.Event()

        async def slow_save(*args, **kwargs):
            await disk.wait()
            return True

        mock_config.save.side_effect = slow_save

        write = asyncio.ensure_future(self._write_true(sm, path))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not write.done(), "the save should still be pending"
        snapshots_before_the_save_finished = _snapshots(mock_gui_queue)
        disk.set()
        await write

        assert sm._button_hidden_for_exclusive_speaker is False
        assert snapshots_before_the_save_finished, "no snapshot before the save"
        assert snapshots_before_the_save_finished[0][KEY] is False

    @pytest.mark.asyncio
    async def test_the_button_stays_shown_after_the_pause_ends(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await sm.set_config_value(SETTING, True)

        await _hold(sm, False)

        assert timers.pending == []
        assert _last(mock_gui_queue)[KEY] is False
        assert _last(mock_gui_queue)["button_visible"] is True

    @pytest.mark.asyncio
    async def test_the_next_new_pause_hides_it_again_after_the_wait(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await sm.set_config_value(SETTING, True)
        await _hold(sm, False)

        await _hold(sm, True)
        assert _last(mock_gui_queue)[KEY] is False
        timers.fire_pending()

        assert _last(mock_gui_queue)[KEY] is True

    @pytest.mark.asyncio
    async def test_the_button_stays_shown_through_a_gap_and_a_new_hold(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        """The hold after the release is a NEW pause start, so it arms the
        hide again; until the wait is over the key stays False."""
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await sm.set_config_value(SETTING, True)
        await _hold(sm, False)
        await _hold(sm, True)

        assert [s[KEY] for s in _snapshots(mock_gui_queue)][-1] is False
        assert len(timers.pending) == 1

    @pytest.mark.asyncio
    async def test_turning_the_button_off_keeps_it_hidden_after_the_pause(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        await _hidden_pause(sm, timers)
        await sm.set_config_value(SETTING, False)
        assert sm._button_hidden_for_exclusive_speaker is True

        await _hold(sm, False)
        timers.fire_pending()

        last = _last(mock_gui_queue)
        assert last[KEY] is False
        assert last["button_visible"] is False

    @pytest.mark.asyncio
    async def test_a_true_over_true_write_does_not_clear_the_key(
        self, sm, timers, mock_gui_queue
    ):
        await _hidden_pause(sm, timers)

        await sm.set_config_value(SETTING, True)
        await sm.set_config_values({SETTING: True, "SHOW_SPEECH_PULSE": True})

        assert sm._button_hidden_for_exclusive_speaker is True
        assert _last(mock_gui_queue)[KEY] is True

    @pytest.mark.asyncio
    async def test_a_write_of_another_setting_does_not_clear_the_key(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)

        await sm.set_config_value("SHOW_SPEECH_PULSE", True)

        assert sm._button_hidden_for_exclusive_speaker is True

    @pytest.mark.asyncio
    async def test_turning_it_on_while_the_hide_is_waiting_cancels_the_hide(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hold(sm, True)
        assert len(timers.pending) == 1

        await sm.set_config_value(SETTING, True)

        assert timers.pending == []
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_turning_it_on_while_the_show_is_waiting_cancels_the_show(
        self, sm, timers, mock_gui_queue, mock_config
    ):
        mock_config.set(SETTING, False)
        await _hidden_pause(sm, timers)
        await _hold(sm, False)
        assert len(timers.pending) == 1

        await sm.set_config_value(SETTING, True)

        assert timers.pending == []
        assert _last(mock_gui_queue)[KEY] is False


class TestWhichPausesHide:

    @pytest.mark.asyncio
    async def test_the_pause_for_sound_from_this_computer_does_not_hide(
        self, sm, timers
    ):
        sm.set_speech_suppressed_by_audio(True)
        assert sm.speech_enabled is False
        assert timers.handles == []
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_the_sonos_pause_does_not_hide(self, sm, timers):
        sm._set_speech_suppressed_by_sonos(True)
        assert timers.handles == []
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_the_idle_pause_does_not_hide(self, sm, timers):
        await sm._handle_idle_state_changed(
            SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
        )
        assert timers.handles == []
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_the_users_speech_switch_does_not_hide(self, sm, timers):
        sm.toggle_speech_enabled_state()
        assert sm.speech_enabled is False
        assert timers.handles == []
        assert sm._button_hidden_for_exclusive_speaker is False

    @pytest.mark.asyncio
    async def test_push_to_talk_mode_still_hides(self, sm, timers, mock_gui_queue):
        sm._speech_interaction_mode = "push_to_talk"
        await _hold(sm, True)
        timers.fire_pending()
        assert _last(mock_gui_queue)[KEY] is True

    @pytest.mark.asyncio
    async def test_a_closed_gate_never_hides(self, sm, timers, mock_gui_queue):
        sm.apply_exclusive_speaker_pause_gate(False)
        await _hold(sm, True)

        assert timers.handles == []
        sm.send_state_update()
        assert _last(mock_gui_queue)[KEY] is False
        assert sm._button_hidden_for_exclusive_speaker is False


class TestAGuiThatStartsDuringThePause:

    @pytest.mark.asyncio
    async def test_the_answer_to_request_initial_state_carries_true_at_once(
        self, sm, timers, mock_gui_queue
    ):
        await _hidden_pause(sm, timers)
        handles_before = len(timers.handles)
        mock_gui_queue.put_nowait.reset_mock()

        # main.py maps the GUI command request_initial_state to this call.
        sm.send_state_update()

        assert _last(mock_gui_queue)[KEY] is True
        assert len(timers.handles) == handles_before
        assert timers.pending == []

    @pytest.mark.asyncio
    async def test_a_pause_still_inside_the_wait_is_not_reported_early(
        self, sm, timers, mock_gui_queue
    ):
        await _hold(sm, True)
        sm.send_state_update()
        assert _last(mock_gui_queue)[KEY] is False


class TestShutdown:

    @pytest.mark.asyncio
    async def test_shutdown_cancels_a_waiting_hide(self, sm, timers):
        await _hold(sm, True)
        hide = timers.pending[0]
        await sm.cancel_pending_saves()
        assert hide.cancelled

    @pytest.mark.asyncio
    async def test_shutdown_cancels_a_waiting_show(self, sm, timers):
        await _hidden_pause(sm, timers)
        await _hold(sm, False)
        show = timers.pending[0]
        await sm.cancel_pending_saves()
        assert show.cancelled
