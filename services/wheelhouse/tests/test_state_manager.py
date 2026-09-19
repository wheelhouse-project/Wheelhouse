"""Tests for StateManager - speech state, suppression, and GUI sync.

Covers:
- speech_enabled computed property with all suppression combinations
- toggle_speech_enabled_state behavior
- Audio/Sonos/idle suppression setters
- send_state_update puts messages on GUI queue
- set_config_value delegates to config_service
- Event handler wiring
- STT provider/mode helpers
"""

import asyncio
import logging
from unittest.mock import Mock, AsyncMock, patch, MagicMock

import pytest

from state_manager import StateManager
from config_service import DEFAULT_STT_PROVIDER
from events import (
    SonosStateChangedEvent,
    AudioStateChangedEvent,
    SystemConfigurationErrorEvent,
    SystemIdleStateChangedEvent,
    WakeWordDetectedEvent,
    PTTStartedEvent,
    PTTStoppedEvent,
)


@pytest.fixture
def sm(mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
    """Create a StateManager with mocked dependencies."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()  # prevent actual task creation
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=mock_websocket_manager,
    )
    yield mgr
    loop.close()


@pytest.fixture
def sm_no_ws(mock_config, mock_event_bus, mock_gui_queue):
    """StateManager without a WebSocketManager (None)."""
    loop = asyncio.new_event_loop()
    loop.create_task = Mock()
    mgr = StateManager(
        config_service=mock_config,
        event_bus=mock_event_bus,
        loop=loop,
        state_to_gui_queue=mock_gui_queue,
        websocket_manager=None,
    )
    yield mgr
    loop.close()


# -----------------------------------------------------------------------
# speech_enabled computed property
# -----------------------------------------------------------------------

class TestSettingsAcknowledgement:
    @pytest.fixture(autouse=True)
    def staged_config_double(self, mock_config):
        mock_config.get_persisted.side_effect = mock_config.get
        async def save(*, values=None):
            saved = mock_config.save.return_value
            if saved is True:
                for key, value in (values or {}).items():
                    mock_config.set(key, value)
            return saved
        mock_config.save.side_effect = save

    @pytest.mark.asyncio
    async def test_settings_ack_broadcast_hides_unsaved_memory(self, sm, mock_config, mock_gui_queue):
        mock_config.set('FLOATING_BUTTON_SIZE', 50)
        async def save(*, values=None):
            sm.send_state_update()
            message = mock_gui_queue.put_nowait.call_args.args[0]
            assert message['FLOATING_BUTTON_SIZE'] == 50
            return True
        mock_config.save.side_effect = save
        assert await sm.set_config_value('FLOATING_BUTTON_SIZE', 80, request_id='pending')

    @pytest.mark.asyncio
    async def test_settings_ack_reconcile_waits_for_inflight_write(self, sm, mock_config, mock_gui_queue):
        mock_config.set('FLOATING_BUTTON_SIZE', 50)
        started, release = asyncio.Event(), asyncio.Event()
        async def save(*, values=None):
            started.set()
            await release.wait()
            return False
        mock_config.save.side_effect = save
        writer = asyncio.create_task(sm.set_config_value('FLOATING_BUTTON_SIZE', 80, request_id='w'))
        await started.wait()
        reader = asyncio.create_task(sm.get_config_values(['FLOATING_BUTTON_SIZE'], 'r'))
        await asyncio.sleep(0)
        assert not reader.done()
        release.set()
        await asyncio.gather(writer, reader)
        message = mock_gui_queue.put_nowait.call_args.args[0]
        assert message == {'action': 'config_values_result', 'request_id': 'r',
                           'values': {'FLOATING_BUTTON_SIZE': 50}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize('saved', [True, False])
    async def test_settings_ack_carries_request_and_saved(self, sm, mock_config, mock_gui_queue, saved):
        mock_config.set('FLOATING_BUTTON_SIZE', 50)
        mock_config.save.return_value = saved
        await sm.set_config_value('FLOATING_BUTTON_SIZE', 80, request_id='request-1')
        messages = [c.args[0] for c in mock_gui_queue.put_nowait.call_args_list]
        acks = [m for m in messages if m['action'] == 'config_write_result']
        assert len(acks) == 1
        ack = acks[0]
        assert ack['request_id'] == 'request-1'
        assert ack['saved'] is saved
        assert ack['values'] == {'FLOATING_BUTTON_SIZE': 80 if saved else 50}
        assert mock_config.get('FLOATING_BUTTON_SIZE') == (80 if saved else 50)

    @pytest.mark.asyncio
    async def test_settings_ack_handler_keeps_request_id(self, sm, mock_config, mock_gui_queue):
        from main import LogicController
        controller = MagicMock(spec=LogicController)
        controller.state_manager = sm
        tasks = []
        controller.create_task_with_error_handling.side_effect = lambda coro, name: tasks.append(coro)
        mock_config.save.return_value = True
        command = {'action': 'set_config_value', 'key': 'FLOATING_BUTTON_SIZE',
                   'value': 80, 'request_id': 'through-handler'}
        LogicController._build_gui_handler_map(controller, command)[command['action']]()
        await tasks[0]
        assert any(c.args[0].get('request_id') == 'through-handler'
                   for c in mock_gui_queue.put_nowait.call_args_list)


@pytest.mark.parametrize("suppressed,expected", [
    ("sonos", "Sonos"), ("audio", "System audio"), ("idle", "idle"),
    ("manual", "disabled"), ("none", ""),
])
def test_ptt_pending_state_carries_actual_reason(sm, mock_gui_queue, suppressed, expected, monkeypatch):
    monkeypatch.setattr(sm.loop, "create_task", lambda coroutine: coroutine.close())
    sm.ptt_start(request_id="current-gui-hold")
    if suppressed in ("sonos", "audio", "idle"):
        setattr(sm, "_speech_suppressed_by_" + suppressed, True)
    if suppressed == "audio":
        sm.ptt_confirm_mute(False, sm._ptt_hold_id, "device unavailable")
    if suppressed == "manual":
        sm._speech_enabled = False
    sm.send_state_update()
    state = mock_gui_queue.put_nowait.call_args.args[0]
    assert state["ptt_request_id"] == "current-gui-hold"
    assert state["speech_enabled"] is (suppressed == "none")
    assert expected in state["ptt_refusal_reason"]
    if suppressed == "none":
        assert state["ptt_refusal_reason"] == ""


def test_ptt_pending_disabled_sonos_suppression_has_no_refusal(sm, mock_config, mock_gui_queue, monkeypatch):
    monkeypatch.setattr(sm.loop, "create_task", lambda coroutine: coroutine.close())
    mock_config.set("ENABLE_SONOS_SUPPRESSION", False)
    sm._speech_suppressed_by_sonos = True
    sm.ptt_start(request_id="hold")
    state = mock_gui_queue.put_nowait.call_args.args[0]
    assert state["speech_enabled"] is True
    assert state["ptt_refusal_reason"] == ""

class TestSpeechEnabledProperty:
    """Test the computed speech_enabled property with suppression combos."""

    def test_enabled_when_no_suppressions(self, sm):
        sm._speech_enabled = True
        assert sm.speech_enabled is True

    def test_disabled_when_user_disabled(self, sm):
        sm._speech_enabled = False
        assert sm.speech_enabled is False

    def test_disabled_when_audio_suppressed(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        assert sm.speech_enabled is False

    def test_disabled_when_sonos_suppressed(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_sonos = True
        assert sm.speech_enabled is False

    def test_disabled_when_idle_suppressed(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        assert sm.speech_enabled is False

    def test_disabled_when_multiple_suppressions(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm._speech_suppressed_by_sonos = True
        assert sm.speech_enabled is False

    def test_audio_suppression_follows_the_startup_decision(self, sm):
        """With the startup decision off, the audio flag is ignored."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm.apply_audio_suppression_decision(False)
        assert sm.speech_enabled is True

    def test_sonos_suppression_respects_config_flag(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_sonos = True
        sm.config_service._config["ENABLE_SONOS_SUPPRESSION"] = False
        assert sm.speech_enabled is True

    def test_idle_suppression_respects_config_flag(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        sm.config_service._config["ENABLE_IDLE_SUPPRESSION"] = False
        assert sm.speech_enabled is True

    def test_user_disabled_overrides_all(self, sm):
        """Even without suppressions, user toggle wins."""
        sm._speech_enabled = False
        sm._speech_suppressed_by_audio = False
        sm._speech_suppressed_by_sonos = False
        sm._speech_suppressed_by_idle = False
        assert sm.speech_enabled is False


# -----------------------------------------------------------------------
# toggle_speech_enabled_state
# -----------------------------------------------------------------------

class TestToggle:
    """Test toggle_speech_enabled_state behavior."""

    def test_toggle_on_when_off(self, sm):
        sm._speech_enabled = False
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is True
        assert sm.speech_enabled is True

    def test_toggle_off_when_on(self, sm):
        sm._speech_enabled = True
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is False

    def test_toggle_on_clears_all_suppressions(self, sm):
        """Toggling ON should clear all suppression flags."""
        sm._speech_enabled = False
        sm._speech_suppressed_by_audio = True
        sm._speech_suppressed_by_sonos = True
        sm._speech_suppressed_by_idle = True
        sm.toggle_speech_enabled_state()
        assert sm._speech_suppressed_by_audio is False
        assert sm._speech_suppressed_by_sonos is False
        assert sm._speech_suppressed_by_idle is False
        assert sm.speech_enabled is True

    def test_toggle_on_from_suppressed_state(self, sm):
        """If speech was enabled but suppressed, toggle should enable."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True  # speech_enabled=False
        sm.toggle_speech_enabled_state()
        # Was OFF (suppressed), so toggle enables and clears suppression
        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False
        assert sm.speech_enabled is True

    def test_toggle_increments_counter(self, sm):
        assert sm._toggle_counter == 0
        sm.toggle_speech_enabled_state()
        assert sm._toggle_counter == 1
        sm.toggle_speech_enabled_state()
        assert sm._toggle_counter == 2

    def test_toggle_sends_state_update(self, sm, mock_gui_queue):
        sm._speech_enabled = True
        sm.toggle_speech_enabled_state()
        mock_gui_queue.put_nowait.assert_called()

    def test_toggle_broadcasts_to_websocket(self, sm, mock_websocket_manager):
        sm._speech_enabled = True
        sm.toggle_speech_enabled_state()
        mock_websocket_manager.set_transcription_status.assert_called()


# -----------------------------------------------------------------------
# Suppression setters
# -----------------------------------------------------------------------

class TestAudioSuppression:

    def test_set_audio_suppressed_true(self, sm):
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        assert sm._speech_suppressed_by_audio is True

    def test_set_audio_suppressed_false(self, sm):
        sm._speech_suppressed_by_audio = True
        sm.set_speech_suppressed_by_audio(False)
        assert sm._speech_suppressed_by_audio is False

    def test_no_op_when_same_state(self, sm, mock_gui_queue):
        sm._speech_suppressed_by_audio = False
        sm.set_speech_suppressed_by_audio(False)
        # Should not trigger state update since no change
        mock_gui_queue.put_nowait.assert_not_called()

    def test_sends_state_update_on_change(self, sm, mock_gui_queue):
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        mock_gui_queue.put_nowait.assert_called()


class TestSonosSuppression:

    def test_set_sonos_suppressed_true(self, sm):
        sm._speech_enabled = True
        sm._set_speech_suppressed_by_sonos(True)
        assert sm._speech_suppressed_by_sonos is True

    def test_no_op_when_same_state(self, sm, mock_gui_queue):
        sm._speech_suppressed_by_sonos = False
        sm._set_speech_suppressed_by_sonos(False)
        mock_gui_queue.put_nowait.assert_not_called()


# -----------------------------------------------------------------------
# Idle suppression (via event handler)
# -----------------------------------------------------------------------

class TestIdleSuppression:

    @pytest.mark.asyncio
    async def test_idle_suppresses_speech(self, sm):
        sm._speech_enabled = True
        event = SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
        await sm._handle_idle_state_changed(event)
        assert sm._speech_suppressed_by_idle is True
        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_active_restores_speech(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        event = SystemIdleStateChangedEvent(is_idle=False, idle_duration_seconds=0.0)
        await sm._handle_idle_state_changed(event)
        assert sm._speech_suppressed_by_idle is False
        assert sm.speech_enabled is True

    @pytest.mark.asyncio
    async def test_idle_saves_speech_state(self, sm):
        sm._speech_enabled = True
        event = SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=300.0)
        await sm._handle_idle_state_changed(event)
        assert sm._speech_enabled_before_idle is True

    @pytest.mark.asyncio
    async def test_idle_no_op_when_already_idle(self, sm, mock_gui_queue):
        sm._speech_suppressed_by_idle = True
        event = SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=900.0)
        await sm._handle_idle_state_changed(event)
        # Should not send state update since already suppressed
        mock_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_active_clears_saved_state(self, sm):
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        sm._speech_enabled_before_idle = True
        event = SystemIdleStateChangedEvent(is_idle=False, idle_duration_seconds=0.0)
        await sm._handle_idle_state_changed(event)
        assert sm._speech_enabled_before_idle is None


# -----------------------------------------------------------------------
# send_state_update
# -----------------------------------------------------------------------

class TestSendStateUpdate:

    def test_puts_state_dict_on_queue(self, sm, mock_gui_queue):
        sm._speech_enabled = True
        sm.send_state_update()
        mock_gui_queue.put_nowait.assert_called_once()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["action"] == "state_update"
        assert state["speech_enabled"] is True

    def test_includes_expected_keys(self, sm, mock_gui_queue):
        sm.send_state_update()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        expected_keys = {
            "action", "speech_enabled", "button_visible",
            "FLOATING_BUTTON_SIZE", "FLOATING_BUTTON_POS",
            "SHOW_SPEECH_PULSE",
            "settings_persisted", "stt_provider",
            "stt_providers_available", "stt_provider_display_names",
            "ai_provider", "ai_providers_available",
            "ai_provider_display_names",
            "interim_results_enabled", "debug_mode",
            "speech_interaction_mode", "ptt_active", "ptt_request_id", "ptt_refusal_reason",
        }
        assert expected_keys == set(state.keys())

    def test_debug_mode_default_false(self, sm, mock_gui_queue):
        sm.send_state_update()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["debug_mode"] is False

    def test_debug_mode_true_when_set(self, sm, mock_gui_queue):
        sm.debug_mode = True
        sm.send_state_update()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["debug_mode"] is True

    def test_survives_queue_error(self, sm, mock_gui_queue):
        mock_gui_queue.put_nowait.side_effect = Exception("queue full")
        # Should not raise
        sm.send_state_update()


# -----------------------------------------------------------------------
# Event handler wiring
# -----------------------------------------------------------------------

class TestEventHandlerWiring:

    @pytest.mark.asyncio
    async def test_sonos_event_updates_suppression(self, sm):
        await sm._handle_sonos_state_changed(SonosStateChangedEvent(is_playing=True))
        assert sm._speech_suppressed_by_sonos is True

    @pytest.mark.asyncio
    async def test_audio_event_updates_suppression(self, sm):
        await sm._handle_audio_state_changed(AudioStateChangedEvent(is_playing=True))
        assert sm._speech_suppressed_by_audio is True

    @pytest.mark.asyncio
    async def test_config_error_sends_notification(self, sm, mock_gui_queue):
        event = SystemConfigurationErrorEvent(
            service_name="AudioMonitor",
            error_message="Device not found",
            user_action="Check audio settings",
        )
        await sm._handle_system_config_error(event)
        mock_gui_queue.put_nowait.assert_called()
        notification = mock_gui_queue.put_nowait.call_args[0][0]
        assert notification["action"] == "show_notification"
        assert "AudioMonitor" in notification["title"]

    @pytest.mark.asyncio
    async def test_config_error_survives_queue_failure(self, sm, mock_gui_queue):
        mock_gui_queue.put_nowait.side_effect = Exception("queue full")
        event = SystemConfigurationErrorEvent(
            service_name="Test",
            error_message="err",
            user_action="fix",
        )
        # Should not raise
        await sm._handle_system_config_error(event)


# -----------------------------------------------------------------------
# set_config_value
# -----------------------------------------------------------------------

class TestSetConfigValue:

    @pytest.mark.asyncio
    async def test_delegates_to_config_service(self, sm, mock_config):
        await sm.set_config_value("FLOATING_BUTTON_SIZE", 75)
        assert mock_config.get("FLOATING_BUTTON_SIZE") == 75

    @pytest.mark.asyncio
    async def test_saves_config(self, sm, mock_config):
        await sm.set_config_value("key", "val")
        mock_config.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_state_update(self, sm, mock_gui_queue):
        await sm.set_config_value("key", "val")
        mock_gui_queue.put_nowait.assert_called()


# -----------------------------------------------------------------------
# toggle_button_visibility
# -----------------------------------------------------------------------

class TestToggleButtonVisibility:

    @pytest.mark.asyncio
    async def test_toggles_visibility(self, sm, mock_config):
        mock_config._config["FLOATING_BUTTON_VISIBLE"] = True
        await sm.toggle_button_visibility()
        assert mock_config.get("FLOATING_BUTTON_VISIBLE") is False

    @pytest.mark.asyncio
    async def test_toggles_visibility_off_to_on(self, sm, mock_config):
        mock_config._config["FLOATING_BUTTON_VISIBLE"] = False
        await sm.toggle_button_visibility()
        assert mock_config.get("FLOATING_BUTTON_VISIBLE") is True


# -----------------------------------------------------------------------
# STT registration helpers
# -----------------------------------------------------------------------

class TestSTTRegistration:

    def test_register_stt_connection(self, sm):
        conn = Mock()
        sm.register_stt_connection(conn)
        assert sm.stt_websocket_connection is conn

    def test_unregister_stt_connection(self, sm):
        sm.stt_websocket_connection = Mock()
        sm.unregister_stt_connection()
        assert sm.stt_websocket_connection is None

    def test_set_remote_stt_launcher(self, sm):
        launcher = Mock()
        sm.set_remote_stt_launcher(launcher)
        assert sm._remote_stt_launcher is launcher


# -----------------------------------------------------------------------
# STT provider helpers
# -----------------------------------------------------------------------

class TestSTTHelpers:

    def test_get_stt_provider_default(self, sm):
        # With no stt.last_provider in config, the remote default is the local
        # offline provider, not a cloud one (wh-stt-fallback-default-google).
        assert sm._get_current_stt_provider() == DEFAULT_STT_PROVIDER

    def test_get_stt_provider_prefers_running_record(self, sm, mock_config):
        # The launcher's actually-started provider outranks the config value:
        # a malformed stt section can make the config unrepairable while a
        # fallback engine runs (provider-removal review .1.6).
        mock_config._config["stt"] = {"last_provider": "parakeet_tdt"}
        sm.set_running_remote_stt_provider("google_stt")
        assert sm._get_current_stt_provider() == "google_stt"

    def test_get_stt_provider_reads_config_without_record(self, sm, mock_config):
        mock_config._config["stt"] = {"last_provider": "google_stt"}
        assert sm._get_current_stt_provider() == "google_stt"

    def test_a_stopped_engine_selects_nothing(self, sm, mock_config):
        # Clearing the record alone would not help: the config still names
        # the provider the user chose, so the tray would put the check mark
        # straight back on an engine that is not running
        # (wh-remote-stt-robustness, Gap A).
        mock_config._config["stt"] = {"last_provider": "google_stt"}
        sm.set_running_remote_stt_provider("google_stt")

        sm.set_remote_stt_stopped()

        assert sm._get_current_stt_provider() is None

    def test_a_stopped_engine_selects_nothing_without_a_record(self, sm, mock_config):
        mock_config._config["stt"] = {"last_provider": "google_stt"}

        sm.set_remote_stt_stopped()

        assert sm._get_current_stt_provider() is None

    def test_a_stop_naming_another_provider_is_ignored(self, sm, mock_config):
        # A startup monitor left over from an earlier engine can report
        # its failure after a replacement is already running; blanking the
        # display for the running engine would be worse than the bug this
        # state fixes.
        mock_config._config["stt"] = {"last_provider": "google_stt"}
        sm.set_running_remote_stt_provider("parakeet_tdt")

        sm.set_remote_stt_stopped("google_stt")

        assert sm._get_current_stt_provider() == "parakeet_tdt"

    def test_a_stop_naming_the_running_provider_is_honoured(self, sm, mock_config):
        mock_config._config["stt"] = {"last_provider": "google_stt"}
        sm.set_running_remote_stt_provider("parakeet_tdt")

        sm.set_remote_stt_stopped("parakeet_tdt")

        assert sm._get_current_stt_provider() is None

    def test_a_later_start_clears_the_stopped_state(self, sm, mock_config):
        mock_config._config["stt"] = {"last_provider": "google_stt"}
        sm.set_remote_stt_stopped()

        sm.set_running_remote_stt_provider("parakeet_tdt")

        assert sm._get_current_stt_provider() == "parakeet_tdt"
        # The flag itself, because the reader cannot show it: a record is
        # read before the flag, so a start that left the flag set would
        # answer correctly here and wrongly the moment the record is
        # cleared again. Only the attribute distinguishes the two.
        assert sm._remote_stt_confirmed_stopped is False

    def test_a_late_stop_cannot_blank_a_replacement_recorded_meanwhile(
        self, sm, mock_config
    ):
        # The stop signal runs on the launcher's startup-monitor thread;
        # the replacement is recorded on the event loop. The guard read
        # and the two writes inside set_remote_stt_stopped are separate
        # steps, so unless the two setters are serialised the loop can
        # record the replacement in between and the stop then blanks an
        # engine that is running, with nothing to correct it
        # (round 1 finding wh-remote-stt-robustness.1.2).
        #
        # The property below is the seam that makes the interleaving
        # deterministic: it parks the monitor thread between the guard
        # and the first write. Serialised, the loop thread waits and the
        # park times out; unserialised, the loop thread records the
        # replacement and the stop wipes it.
        import threading

        mock_config._config["stt"] = {"last_provider": "google_stt"}

        record = {"value": None}
        monitor = {"thread": None}
        reads = {"n": 0}
        parked = threading.Event()
        replaced = threading.Event()

        def _get(self):
            value = record["value"]
            if threading.current_thread() is monitor["thread"]:
                reads["n"] += 1
                # The guard reads the record twice. Park after the second
                # read, which is the only moment a lock that covered just
                # the writes would leave unprotected.
                if reads["n"] == 2 and not parked.is_set():
                    parked.set()
                    replaced.wait(timeout=0.5)
            return value

        def _set(self, value):
            record["value"] = value

        with patch.object(
            StateManager,
            "_running_remote_stt_provider",
            property(_get, _set),
            create=True,
        ):
            sm.set_running_remote_stt_provider("google_stt")

            monitor["thread"] = threading.Thread(
                target=sm.set_remote_stt_stopped, args=("google_stt",)
            )
            monitor["thread"].start()
            assert parked.wait(timeout=2), "the stop never finished its guard"

            sm.set_running_remote_stt_provider("parakeet_tdt")
            replaced.set()
            monitor["thread"].join(timeout=2)
            assert not monitor["thread"].is_alive()

            assert sm._get_current_stt_provider() == "parakeet_tdt"

    def test_get_available_providers_no_launcher(self, sm):
        # Remote mode but no launcher: nothing is discovered.
        result = sm._get_available_stt_providers()
        assert isinstance(result, list)

    def test_get_provider_display_names_no_launcher(self, sm):
        result = sm._get_provider_display_names()
        assert isinstance(result, dict)

    def test_get_available_providers_with_launcher(self, sm):
        launcher = Mock()
        launcher.get_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud"},
            {"name": "parakeet_tdt", "display_name": "Parakeet v3 (GPU)"},
        ]
        sm._remote_stt_launcher = launcher
        result = sm._get_available_stt_providers()
        assert result == ["google_stt", "parakeet_tdt"]

    def test_get_provider_display_names_with_launcher(self, sm):
        launcher = Mock()
        launcher.get_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud"},
            {"name": "parakeet_tdt", "display_name": "Parakeet v3 (GPU)"},
        ]
        sm._remote_stt_launcher = launcher
        result = sm._get_provider_display_names()
        assert result["google_stt"] == "Google Cloud"
        assert result["parakeet_tdt"] == "Parakeet v3 (GPU)"


# -----------------------------------------------------------------------
# No WebSocket manager
# -----------------------------------------------------------------------

class TestWithoutWebSocketManager:

    def test_toggle_without_ws_manager(self, sm_no_ws):
        sm_no_ws._speech_enabled = True
        # Should not raise when websocket_manager is None
        sm_no_ws.toggle_speech_enabled_state()
        assert sm_no_ws._speech_enabled is False

    def test_audio_suppression_without_ws_manager(self, sm_no_ws):
        sm_no_ws._speech_enabled = True
        sm_no_ws.set_speech_suppressed_by_audio(True)
        assert sm_no_ws._speech_suppressed_by_audio is True


# -----------------------------------------------------------------------
# Adversarial: Queue-full scenarios
# -----------------------------------------------------------------------

class TestQueueFullScenarios:
    """Test behavior when the GUI queue is full (realistic IPC pressure)."""

    def test_send_state_update_survives_queue_full(self, sm, mock_gui_queue):
        """queue.Full is the actual exception put_nowait raises on a full queue."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        # Must not raise - state manager logs and continues
        sm.send_state_update()

    def test_toggle_survives_queue_full(self, sm, mock_gui_queue):
        """Toggle should complete even if GUI queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        sm._speech_enabled = True
        sm.toggle_speech_enabled_state()
        # State change still happened despite queue failure
        assert sm._speech_enabled is False

    def test_toggle_on_survives_queue_full(self, sm, mock_gui_queue):
        """Toggle ON should clear suppressions even if GUI queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        sm._speech_enabled = False
        sm._speech_suppressed_by_audio = True
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False

    def test_audio_suppression_survives_queue_full(self, sm, mock_gui_queue):
        """Audio suppression change should update state even if queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        assert sm._speech_suppressed_by_audio is True
        assert sm.speech_enabled is False

    def test_sonos_suppression_survives_queue_full(self, sm, mock_gui_queue):
        """Sonos suppression change should update state even if queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        sm._speech_enabled = True
        sm._set_speech_suppressed_by_sonos(True)
        assert sm._speech_suppressed_by_sonos is True
        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_idle_suppression_survives_queue_full(self, sm, mock_gui_queue):
        """Idle handler should update state even if queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        sm._speech_enabled = True
        event = SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
        await sm._handle_idle_state_changed(event)
        assert sm._speech_suppressed_by_idle is True
        assert sm.speech_enabled is False

    @pytest.mark.asyncio
    async def test_config_error_notification_survives_queue_full(self, sm, mock_gui_queue):
        """Config error notification should not raise when queue is full."""
        import queue
        mock_gui_queue.put_nowait.side_effect = queue.Full()
        event = SystemConfigurationErrorEvent(
            service_name="Test",
            error_message="err",
            user_action="fix",
        )
        await sm._handle_system_config_error(event)


# -----------------------------------------------------------------------
# Adversarial: Rapid concurrent state changes
# -----------------------------------------------------------------------

class TestRapidStateChanges:
    """Test state consistency under rapid interleaved operations.

    These simulate realistic scenarios where multiple suppression sources
    and user toggles fire in quick succession (e.g., Sonos starts playing
    while audio is detected and user is toggling).
    """

    def test_rapid_toggles_maintain_consistency(self, sm):
        """Rapid toggle sequence should always leave consistent state."""
        sm._speech_enabled = True
        for _ in range(20):
            sm.toggle_speech_enabled_state()
        # Even number of toggles from enabled -> back to enabled
        assert sm._speech_enabled is True
        assert sm.speech_enabled is True

    def test_rapid_toggles_odd_count(self, sm):
        """Odd number of rapid toggles should flip state."""
        sm._speech_enabled = True
        for _ in range(21):
            sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is False
        assert sm.speech_enabled is False

    def test_suppression_toggle_interleave(self, sm):
        """Audio suppression fires between user toggles."""
        sm._speech_enabled = True

        # User toggles off
        sm.toggle_speech_enabled_state()
        assert sm.speech_enabled is False

        # Audio suppression arrives (no-op since already off)
        sm.set_speech_suppressed_by_audio(True)
        assert sm.speech_enabled is False

        # User toggles back on - should clear suppression
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False
        assert sm.speech_enabled is True

        # Audio suppression clears (already cleared by toggle)
        sm.set_speech_suppressed_by_audio(False)
        assert sm.speech_enabled is True

    def test_multiple_suppression_sources_interleaved(self, sm):
        """Audio and Sonos suppression arrive in interleaved order."""
        sm._speech_enabled = True
        assert sm.speech_enabled is True

        # Audio suppresses
        sm.set_speech_suppressed_by_audio(True)
        assert sm.speech_enabled is False

        # Sonos also suppresses
        sm._set_speech_suppressed_by_sonos(True)
        assert sm.speech_enabled is False

        # Audio clears, but Sonos still active
        sm.set_speech_suppressed_by_audio(False)
        assert sm.speech_enabled is False

        # Sonos clears - now should be enabled
        sm._set_speech_suppressed_by_sonos(False)
        assert sm.speech_enabled is True

    def test_all_three_suppressions_interleaved(self, sm):
        """Audio, Sonos, and idle suppression in interleaved sequence."""
        sm._speech_enabled = True

        sm.set_speech_suppressed_by_audio(True)
        sm._set_speech_suppressed_by_sonos(True)
        sm._speech_suppressed_by_idle = True
        assert sm.speech_enabled is False

        # Clear one at a time - should stay disabled until all cleared
        sm.set_speech_suppressed_by_audio(False)
        assert sm.speech_enabled is False

        sm._set_speech_suppressed_by_sonos(False)
        assert sm.speech_enabled is False

        sm._speech_suppressed_by_idle = False
        assert sm.speech_enabled is True

    def test_toggle_during_multiple_suppressions(self, sm):
        """User toggle while multiple suppressions active clears them all."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm._speech_suppressed_by_sonos = True
        sm._speech_suppressed_by_idle = True
        assert sm.speech_enabled is False

        # User toggles - should enable and clear all suppressions
        sm.toggle_speech_enabled_state()
        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is False
        assert sm._speech_suppressed_by_sonos is False
        assert sm._speech_suppressed_by_idle is False
        assert sm.speech_enabled is True

    def test_suppression_arrives_immediately_after_toggle_on(self, sm):
        """Suppression event fires right after user enables speech."""
        sm._speech_enabled = False
        sm.toggle_speech_enabled_state()
        assert sm.speech_enabled is True

        # Audio suppression arrives immediately
        sm.set_speech_suppressed_by_audio(True)
        assert sm.speech_enabled is False

        # State is correct: user enabled, but suppressed by audio
        assert sm._speech_enabled is True
        assert sm._speech_suppressed_by_audio is True

    def test_rapid_audio_suppression_flapping(self, sm):
        """Audio suppression flapping (on/off rapidly) leaves correct state."""
        sm._speech_enabled = True

        for _ in range(50):
            sm.set_speech_suppressed_by_audio(True)
            sm.set_speech_suppressed_by_audio(False)

        # After even number of on/off cycles, should be unsuppressed
        assert sm._speech_suppressed_by_audio is False
        assert sm.speech_enabled is True

    def test_toggle_counter_accuracy_under_rapid_changes(self, sm):
        """Toggle counter stays accurate under rapid operations."""
        sm._speech_enabled = True
        n = 15
        for _ in range(n):
            sm.toggle_speech_enabled_state()
        assert sm._toggle_counter == n


# -----------------------------------------------------------------------
# Suppression reason passed through WebSocket
# -----------------------------------------------------------------------

class TestSuppressionReason:
    """Each suppression pathway passes the correct reason to set_transcription_status."""

    def test_idle_suppression_sends_reason_idle(self, sm, mock_websocket_manager):
        """When idle suppression activates, reason='idle' is passed."""
        sm._speech_enabled = True
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            sm._handle_idle_state_changed(
                SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0)
            )
        )
        loop.close()
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="idle")

    def test_idle_restore_sends_no_reason(self, sm, mock_websocket_manager):
        """When re-enabling from idle, no reason is passed (default None)."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            sm._handle_idle_state_changed(
                SystemIdleStateChangedEvent(is_idle=False, idle_duration_seconds=0.0)
            )
        )
        loop.close()
        mock_websocket_manager.set_transcription_status.assert_called_with(True)

    def test_audio_suppression_sends_reason_audio(self, sm, mock_websocket_manager):
        """When audio suppression activates, reason='audio' is passed."""
        sm._speech_enabled = True
        sm.set_speech_suppressed_by_audio(True)
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="audio")

    def test_audio_restore_sends_no_reason(self, sm, mock_websocket_manager):
        """When audio suppression clears, no reason is passed."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm.set_speech_suppressed_by_audio(False)
        mock_websocket_manager.set_transcription_status.assert_called_with(True)

    def test_sonos_suppression_sends_reason_sonos(self, sm, mock_websocket_manager):
        """When Sonos suppression activates, reason='sonos' is passed."""
        sm._speech_enabled = True
        sm._set_speech_suppressed_by_sonos(True)
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="sonos")

    def test_sonos_restore_sends_no_reason(self, sm, mock_websocket_manager):
        """When Sonos suppression clears, no reason is passed."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_sonos = True
        sm._set_speech_suppressed_by_sonos(False)
        mock_websocket_manager.set_transcription_status.assert_called_with(True)

    def test_manual_toggle_off_sends_reason_manual(self, sm, mock_websocket_manager):
        """When user manually disables speech, reason='manual' is passed."""
        sm._speech_enabled = True
        sm.toggle_speech_enabled_state()
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="manual")

    def test_manual_toggle_on_sends_no_reason(self, sm, mock_websocket_manager):
        """When user manually enables speech, no reason is passed."""
        sm._speech_enabled = False
        sm.toggle_speech_enabled_state()
        mock_websocket_manager.set_transcription_status.assert_called_with(True)


# -----------------------------------------------------------------------
# Wake word detection handling
# -----------------------------------------------------------------------

class TestWakeWordDetection:
    """Test wake word detection clears idle suppression."""

    @pytest.mark.asyncio
    async def test_wake_word_clears_idle_suppression(self, sm, mock_websocket_manager):
        """WakeWordDetectedEvent clears idle suppression and re-enables speech."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        sm._speech_enabled_before_idle = True
        assert sm.speech_enabled is False

        event = WakeWordDetectedEvent(keyword="computer")
        await sm._handle_wake_word_detected(event)

        assert sm._speech_suppressed_by_idle is False
        assert sm._speech_enabled_before_idle is None
        assert sm.speech_enabled is True
        mock_websocket_manager.set_transcription_status.assert_called_with(True)

    @pytest.mark.asyncio
    async def test_wake_word_ignored_when_not_idle_suppressed(self, sm, mock_websocket_manager):
        """WakeWordDetectedEvent is ignored when idle suppression is not active."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = False

        event = WakeWordDetectedEvent(keyword="computer")
        await sm._handle_wake_word_detected(event)

        # No state changes, no broadcast
        mock_websocket_manager.set_transcription_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_wake_word_sends_state_update(self, sm, mock_gui_queue):
        """Wake word handler sends state update to GUI when clearing suppression."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = True
        sm._speech_enabled_before_idle = True

        event = WakeWordDetectedEvent(keyword="computer")
        await sm._handle_wake_word_detected(event)

        mock_gui_queue.put_nowait.assert_called()

    @pytest.mark.asyncio
    async def test_wake_word_no_state_update_when_not_suppressed(self, sm, mock_gui_queue):
        """No GUI update when wake word fires but not idle-suppressed."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_idle = False

        event = WakeWordDetectedEvent(keyword="computer")
        await sm._handle_wake_word_detected(event)

        mock_gui_queue.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_wake_word_subscribes_to_event(self, sm, mock_event_bus):
        """StateManager subscribes to WakeWordDetectedEvent."""
        # Check that subscribe was called with WakeWordDetectedEvent
        # Compare by name since test and source use different import paths
        calls = [c for c in mock_event_bus.subscribe.call_args_list
                 if c[0][0].__name__ == "WakeWordDetectedEvent"]
        assert len(calls) == 1


# -----------------------------------------------------------------------
# PTT (Push-to-Talk) state transitions
# -----------------------------------------------------------------------

class TestPTTState:
    """Test push-to-talk state management."""

    def test_initial_interaction_mode_from_config(self, mock_config, mock_event_bus, mock_gui_queue, mock_websocket_manager):
        mock_config._config["speech"] = {"interaction_mode": "push_to_talk"}
        loop = asyncio.new_event_loop()
        loop.create_task = Mock()
        mgr = StateManager(
            config_service=mock_config,
            event_bus=mock_event_bus,
            loop=loop,
            state_to_gui_queue=mock_gui_queue,
            websocket_manager=mock_websocket_manager,
        )
        assert mgr._speech_interaction_mode == "push_to_talk"
        loop.close()

    def test_initial_interaction_mode_defaults_to_toggle(self, sm):
        assert sm._speech_interaction_mode == "toggle"

    def test_ptt_not_active_initially(self, sm):
        assert sm._ptt_active is False

    def test_ptt_start_enables_speech(self, sm):
        sm._speech_enabled = False
        sm.ptt_start()
        assert sm._ptt_active is True
        assert sm._speech_enabled is True
        assert sm.speech_enabled is True

    def test_ptt_start_clears_idle_suppression(self, sm):
        sm._speech_enabled = False
        sm._speech_suppressed_by_idle = True
        sm.ptt_start()
        assert sm._speech_suppressed_by_idle is False

    def test_ptt_start_broadcasts_to_websocket(self, sm, mock_websocket_manager):
        sm.ptt_start()
        mock_websocket_manager.set_transcription_status.assert_called_with(True, reason="ptt")

    def test_ptt_start_publishes_event(self, sm, mock_event_bus):
        sm.ptt_start()
        mock_event_bus.publish.assert_called_once()
        event = mock_event_bus.publish.call_args[0][0]
        assert event.__class__.__name__ == "PTTStartedEvent"

    def test_ptt_start_stamps_the_event_with_the_hold_number(self, sm, mock_event_bus):
        """SystemVolumePlugin echoes this number back on its mute report, and
        that is how StateManager tells one hold's answer from another's
        (wh-ptt-audio-override.1.1)."""
        sm.ptt_start()
        event = mock_event_bus.publish.call_args[0][0]
        assert event.hold_id == sm._ptt_hold_id
        assert event.hold_id > 0

    def test_ptt_start_sends_state_update(self, sm, mock_gui_queue):
        sm.ptt_start()
        mock_gui_queue.put_nowait.assert_called()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["action"] == "state_update"
        assert state["ptt_active"] is True

    def test_ptt_stop_clears_the_active_flag(self, sm):
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop()
        assert sm._ptt_active is False

    def test_ptt_stop_publishes_event(self, sm, mock_event_bus):
        sm._ptt_active = True
        sm.ptt_stop()
        mock_event_bus.publish.assert_called_once()
        event = mock_event_bus.publish.call_args[0][0]
        assert event.__class__.__name__ == "PTTStoppedEvent"
        assert event.reason == "released"

    def test_ptt_stop_noop_when_not_active(self, sm, mock_event_bus):
        sm._ptt_active = False
        sm.ptt_stop()
        mock_event_bus.publish.assert_not_called()

    def test_ptt_stop_broadcasts_to_websocket(self, sm, mock_websocket_manager):
        """The release tells the engine the restored value, tagged "ptt".

        This test is the only one that pins the reason keyword; the value
        itself is covered by
        test_an_ordinary_release_leaves_the_engine_on_when_speech_was_on.
        It asserted False until wh-ptt-release-disables-speech.1.9: it set
        _ptt_active directly, so ptt_start never ran, _speech_before_ptt
        stayed unset, and ptt_stop restored the getattr default False. The
        assertion matched that default rather than any contract, and it
        would have passed under a full revert of 5719a1fa.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop()
        mock_websocket_manager.set_transcription_status.assert_called_with(True, reason="ptt")

    def test_ptt_stop_drag_cancel_restores_speech(self, sm):
        """Drag cancel restores speech to pre-PTT state."""
        sm._speech_enabled = True
        sm.ptt_start()  # Saves _speech_before_ptt = True
        assert sm._speech_enabled is True
        sm.ptt_stop(reason="drag_cancel")
        assert sm._speech_enabled is True  # Restored, not forced off

    def test_ptt_stop_drag_cancel_keeps_speech_off(self, sm):
        """Drag cancel keeps speech off if it was off before PTT."""
        sm._speech_enabled = False
        sm.ptt_start()  # Saves _speech_before_ptt = False
        assert sm._speech_enabled is True  # PTT turns it on
        sm.ptt_stop(reason="drag_cancel")
        assert sm._speech_enabled is False  # Restored to pre-PTT state

    def test_ptt_stop_gesture_cancel_restores_speech(self, sm):
        """A press whose release was taken away decided nothing.

        The context menu opening or the button being hidden interrupts the hold
        the same way a drag does, so speech goes back to what the user had
        rather than being forced off.
        """
        sm._speech_enabled = True
        sm.ptt_start()  # Saves _speech_before_ptt = True
        sm.ptt_stop(reason="gesture_cancel")
        assert sm._speech_enabled is True  # Restored, not forced off

    def test_ptt_stop_gesture_cancel_keeps_speech_off(self, sm):
        """Gesture cancel keeps speech off if it was off before PTT."""
        sm._speech_enabled = False
        sm.ptt_start()
        assert sm._speech_enabled is True  # PTT turns it on
        sm.ptt_stop(reason="gesture_cancel")
        assert sm._speech_enabled is False

    def test_ptt_stop_released_restores_the_pre_hold_setting(self, sm):
        """An ordinary release puts back what was there before the hold.

        This test previously asserted the opposite, on the reading that an
        ordinary release is itself a decision to turn speech off. David
        reported the consequence on 2026-08-27: a hold taken while speech was
        on but audio-suppressed ended with his own setting overwritten, and
        listening never came back when the sound stopped
        (wh-ptt-release-disables-speech).
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop(reason="released")
        assert sm._speech_enabled is True

    def test_ptt_stop_released_keeps_speech_off_in_push_to_talk_mode(self, sm):
        """Speech is off between holds there, so the release ends off."""
        sm._speech_enabled = False
        sm.ptt_start()
        sm.ptt_stop(reason="released")
        assert sm._speech_enabled is False

    def test_a_stop_after_the_safety_timeout_still_corrects_the_display(self, sm):
        """The interface asked to end a hold that already ended on its own.

        The safety timeout ends a hold the user never released. If the button
        is then hidden or the context menu opens, the interface cancels the
        hold it still believes in and puts speech back to what it was before
        -- showing the microphone as open while it is shut. Nothing here can
        stop that guess being made, so the answer is to send the real state
        back straight away and correct it.

        Speech is off before the hold here, so the cutoff's restore leaves it
        off and there is a wrong guess to correct (David, 2026-08-27: the
        cutoff restores rather than forcing speech off).
        """
        sm._speech_enabled = False
        sm.ptt_start()
        sm._ptt_safety_timeout()
        assert sm._speech_enabled is False
        sm.state_to_gui_queue.put_nowait.reset_mock()

        sm.ptt_stop(reason="gesture_cancel")

        updates = [
            c[0][0]
            for c in sm.state_to_gui_queue.put_nowait.call_args_list
            if c[0][0].get("action") == "state_update"
        ]
        assert updates, "no state update was sent to correct the display"
        assert updates[-1]["speech_enabled"] is False
        assert updates[-1]["ptt_active"] is False

    def test_a_stop_on_a_hold_that_never_ran_changes_nothing(self, sm, mock_event_bus):
        """Correcting the display must not look like ending a real hold."""
        sm._speech_enabled = False
        sm._ptt_active = False

        sm.ptt_stop(reason="gesture_cancel")

        assert sm._speech_enabled is False
        mock_event_bus.publish.assert_not_called()

    def test_set_interaction_mode(self, sm):
        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_interaction_mode == "push_to_talk"

    def test_set_interaction_mode_rejects_invalid(self, sm):
        sm.set_speech_interaction_mode("invalid_mode")
        assert sm._speech_interaction_mode == "toggle"  # Unchanged

    def test_set_interaction_mode_sends_state_update(self, sm, mock_gui_queue):
        sm.set_speech_interaction_mode("push_to_talk")
        mock_gui_queue.put_nowait.assert_called()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["speech_interaction_mode"] == "push_to_talk"

    def test_set_interaction_mode_disables_speech(self, sm):
        """Mode switch disables speech for clean state transition."""
        sm._speech_enabled = True
        sm.set_speech_interaction_mode("push_to_talk")
        assert sm._speech_enabled is False

    def test_set_interaction_mode_broadcasts_disable(self, sm, mock_websocket_manager):
        """Mode switch broadcasts speech disabled to STT clients."""
        sm._speech_enabled = True
        sm.set_speech_interaction_mode("push_to_talk")
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="manual")

    def test_set_interaction_mode_noop_when_speech_off(self, sm, mock_websocket_manager):
        """Mode switch skips disable broadcast when speech already off."""
        sm._speech_enabled = False
        sm.set_speech_interaction_mode("push_to_talk")
        mock_websocket_manager.set_transcription_status.assert_not_called()

    def test_state_update_includes_interaction_mode(self, sm, mock_gui_queue):
        sm._speech_interaction_mode = "push_to_talk"
        sm.send_state_update()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["speech_interaction_mode"] == "push_to_talk"

    def test_state_update_includes_ptt_active(self, sm, mock_gui_queue):
        sm._ptt_active = True
        sm.send_state_update()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["ptt_active"] is True

    def test_safety_timeout_stops_ptt(self, sm):
        """The cutoff ends the hold and puts the pre-hold setting back.

        It forced speech off until David's ruling of 2026-08-27; a lost
        release is not a decision to switch speech off.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm._ptt_safety_timeout()
        assert sm._ptt_active is False
        assert sm._speech_enabled is True

    def test_ptt_stop_clears_safety_handle(self, sm):
        sm._ptt_active = True
        sm._speech_enabled = True
        sm._ptt_safety_handle = Mock()
        sm.ptt_stop()
        assert sm._ptt_safety_handle is None

    def test_set_interaction_mode_persists_to_config(self, sm, mock_config):
        sm.set_speech_interaction_mode("push_to_talk")
        # mock_config.set is a real function that writes to _config dict
        assert mock_config._config["speech"]["interaction_mode"] == "push_to_talk"
        # save() was scheduled via loop.create_task
        sm.loop.create_task.assert_called()


class TestTheSpeechEngineIsToldTheSameThingAsTheDisplay:
    """Ending a hold must tell the speech engine what the display shows.

    The interface and the speech engine learn about the end of a hold through
    two separate channels. If they disagree, the user sees an open microphone
    that cannot hear anything, and nothing repairs it until some other change
    happens.
    """

    def _told(self, mock_websocket_manager):
        """Return the on-or-off value the speech engine was last told."""
        return mock_websocket_manager.set_transcription_status.call_args[0][0]

    def test_a_cancelled_hold_leaves_the_engine_on_when_speech_was_on(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop(reason="drag_cancel")
        assert sm.speech_enabled is True
        assert self._told(mock_websocket_manager) is True

    def test_a_cancelled_hold_leaves_the_engine_off_when_speech_was_off(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = False
        sm.ptt_start()
        sm.ptt_stop(reason="drag_cancel")
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False

    def test_a_taken_release_leaves_the_engine_on_when_speech_was_on(
        self, sm, mock_websocket_manager
    ):
        """The context menu or a hidden button interrupts the hold the same way."""
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop(reason="gesture_cancel")
        assert sm.speech_enabled is True
        assert self._told(mock_websocket_manager) is True

    def test_a_taken_release_leaves_the_engine_off_when_speech_was_off(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = False
        sm.ptt_start()
        sm.ptt_stop(reason="gesture_cancel")
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False

    def test_an_ordinary_release_leaves_the_engine_on_when_speech_was_on(
        self, sm, mock_websocket_manager
    ):
        """The release restores the setting, so the engine keeps listening.

        This test previously asserted the opposite
        (wh-ptt-release-disables-speech); see
        TestPTTState.test_ptt_stop_released_restores_the_pre_hold_setting.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop(reason="released")
        assert sm.speech_enabled is True
        assert self._told(mock_websocket_manager) is True

    def test_an_ordinary_release_leaves_the_engine_off_when_speech_was_off(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = False
        sm.ptt_start()
        sm.ptt_stop(reason="released")
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False

    def test_the_safety_cutoff_leaves_the_engine_on_when_speech_was_on(
        self, sm, mock_websocket_manager
    ):
        """The cutoff restores like every other ending (David, 2026-08-27).

        It turned the engine off until that ruling, because it forced the
        setting off first. A lost release is not a decision to switch speech
        off, so the display and the engine both get the restored value.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm.ptt_stop(reason="safety_timeout")
        assert sm.speech_enabled is True
        assert self._told(mock_websocket_manager) is True

    def test_the_safety_cutoff_leaves_the_engine_off_when_speech_was_off(
        self, sm, mock_websocket_manager
    ):
        sm._speech_enabled = False
        sm.ptt_start()
        sm.ptt_stop(reason="safety_timeout")
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False

    def test_a_cancelled_hold_under_suppression_still_leaves_the_engine_off(
        self, sm, mock_websocket_manager
    ):
        """Restoring the user's setting does not override a suppression.

        Speech was on before the hold, so the setting is put back on. Sound is
        playing, though, so the real answer is still off, and that is what the
        speech engine must be told -- not the raw setting.
        """
        sm._speech_enabled = True
        sm.ptt_start()
        sm._speech_suppressed_by_audio = True
        sm.ptt_stop(reason="drag_cancel")
        assert sm._speech_enabled is True
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False

    def test_the_engine_is_told_exactly_what_the_display_is_told(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        """The two channels agree by construction, not by coincidence."""
        sm._speech_enabled = True
        sm.ptt_start()
        mock_gui_queue.put_nowait.reset_mock()
        sm.ptt_stop(reason="gesture_cancel")
        shown = mock_gui_queue.put_nowait.call_args[0][0]["speech_enabled"]
        assert self._told(mock_websocket_manager) is shown

    def _shown(self, mock_gui_queue):
        """Return the on-or-off value the button was last shown."""
        return mock_gui_queue.put_nowait.call_args[0][0]["speech_enabled"]

    def test_the_start_of_a_hold_tells_the_engine_what_the_button_shows(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        sm.ptt_start()
        assert self._told(mock_websocket_manager) is True
        assert self._shown(mock_gui_queue) is True

    def test_a_hold_never_writes_the_setting_the_audio_monitor_owns(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        """Starting a hold must not clear the audio-suppression setting.

        The audio monitor owns that setting and reports only when the answer
        changes, so nothing puts it back. A hold shorter than one check would
        leave it cleared for as long as the sound keeps playing, which switches
        off audio suppression for good.

        The hold still has to hear the user, because it mutes the speakers.
        It gets that from a separate override that every ending destroys, so
        both channels say on while the setting itself stays exactly what the
        monitor measured (wh-ptt-audio-override).
        """
        sm._speech_suppressed_by_audio = True
        sm.ptt_start()
        assert sm._speech_suppressed_by_audio is True
        assert sm.speech_enabled is True
        assert self._told(mock_websocket_manager) is True
        assert self._shown(mock_gui_queue) is True

    def test_a_hold_that_begins_and_ends_between_two_checks_changes_nothing(
        self, sm
    ):
        """The whole hold fits inside one monitor check, so no report arrives.

        This is the case that made the earlier version permanent: the sound is
        still playing at the end, so the monitor sees no change and sends
        nothing, and there is no other path that would put the setting back.
        """
        sm._speech_suppressed_by_audio = True
        sm.ptt_start()
        sm.ptt_stop(reason="released")
        assert sm._speech_suppressed_by_audio is True

    def test_a_cancelled_hold_also_leaves_that_setting_alone(self, sm):
        sm._speech_suppressed_by_audio = True
        sm.ptt_start()
        sm.ptt_stop(reason="drag_cancel")
        assert sm._speech_suppressed_by_audio is True

    def test_a_hold_that_starts_while_sonos_is_playing_does_not_listen(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        """Sonos plays on a separate speaker that the hold cannot mute.

        The design leaves this suppression alone on purpose. The hold therefore
        hears nothing, and both the engine and the button must say so.
        """
        sm._speech_suppressed_by_sonos = True
        sm.ptt_start()
        assert sm._speech_suppressed_by_sonos is True
        assert sm.speech_enabled is False
        assert self._told(mock_websocket_manager) is False
        assert self._shown(mock_gui_queue) is False

    def test_a_hold_that_starts_after_an_idle_pause_listens(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        """Holding the button proves the user is there, so the idle pause ends."""
        sm._speech_suppressed_by_idle = True
        sm.ptt_start()
        assert sm._speech_suppressed_by_idle is False
        assert self._told(mock_websocket_manager) is True
        assert self._shown(mock_gui_queue) is True

    def test_the_two_channels_agree_at_the_start_of_every_hold(
        self, sm, mock_websocket_manager, mock_gui_queue
    ):
        """Whatever is suppressing speech, the engine and the button match."""
        for flag in (
            "_speech_suppressed_by_audio",
            "_speech_suppressed_by_sonos",
            "_speech_suppressed_by_idle",
        ):
            sm._ptt_active = False
            sm._speech_suppressed_by_audio = False
            sm._speech_suppressed_by_sonos = False
            sm._speech_suppressed_by_idle = False
            setattr(sm, flag, True)
            mock_gui_queue.put_nowait.reset_mock()
            mock_websocket_manager.set_transcription_status.reset_mock()
            sm.ptt_start()
            # Read both into plain values before asserting. Comparing the two
            # helper calls directly makes pytest try to describe the mocks when
            # it builds the failure message, and it cannot.
            told = self._told(mock_websocket_manager)
            shown = self._shown(mock_gui_queue)
            assert told is shown, f"{flag}: engine was told {told}, button showed {shown}"


class TestTheDebugFlagMatchesTheRealLoggingLevel:
    """The published debug_mode must match the level the process really runs at.

    main.py applies the settings file's LOG_LEVEL in setup_logging (line 11635)
    before it builds the StateManager (line 11661), and after every toggle it
    keeps the flag in step with the same comparison these tests pin
    (main.py:2242). A literal False in the constructor broke exactly one case:
    a startup whose LOG_LEVEL is already DEBUG. Both menus draw the Debug
    entry's checkmark from this flag, so the entry stood unchecked while
    detailed logging was on, and the first click turned it off.
    Finding wh-audio-suppression-floating-menu.1.1.
    """

    @pytest.fixture
    def root_logger(self):
        """Put the root logger back at whatever level the test found it."""
        root = logging.getLogger()
        original = root.level
        yield root
        root.setLevel(original)

    @pytest.fixture
    def build(self, mock_config, mock_event_bus, mock_gui_queue):
        """Build StateManagers and close their event loops afterwards."""
        loops = []

        def make():
            loop = asyncio.new_event_loop()
            loop.create_task = Mock()  # prevent actual task creation
            loops.append(loop)
            return StateManager(
                config_service=mock_config,
                event_bus=mock_event_bus,
                loop=loop,
                state_to_gui_queue=mock_gui_queue,
                websocket_manager=None,
            )

        yield make
        for loop in loops:
            loop.close()

    def test_a_debug_startup_reports_debug_mode(self, root_logger, build):
        root_logger.setLevel(logging.DEBUG)
        assert build().debug_mode is True

    def test_an_info_startup_does_not_report_debug_mode(self, root_logger, build):
        root_logger.setLevel(logging.INFO)
        assert build().debug_mode is False

    def test_the_menus_receive_the_same_answer(
        self, root_logger, build, mock_gui_queue
    ):
        """The menus read the flag from the state message, not from the object.

        _create_menu draws the checkmark from the copy gui.py stored when it
        consumed this message, so the message is what the user finally sees.
        """
        root_logger.setLevel(logging.DEBUG)
        manager = build()
        mock_gui_queue.put_nowait.reset_mock()
        manager.send_state_update()
        published = [
            call.args[0] for call in mock_gui_queue.put_nowait.call_args_list
        ]
        assert published, "send_state_update put nothing on the GUI queue"
        assert published[-1]["debug_mode"] is True
