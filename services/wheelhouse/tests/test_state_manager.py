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
from unittest.mock import Mock, AsyncMock, patch, MagicMock

import pytest

from state_manager import StateManager
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

    def test_audio_suppression_respects_config_flag(self, sm):
        """If ENABLE_AUDIO_SUPPRESSION is False, audio flag is ignored."""
        sm._speech_enabled = True
        sm._speech_suppressed_by_audio = True
        sm.config_service._config["ENABLE_AUDIO_SUPPRESSION"] = False
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
            "SHOW_SPEECH_PULSE", "stt_mode", "stt_provider",
            "stt_providers_available", "stt_provider_display_names",
            "ai_provider", "ai_providers_available",
            "ai_provider_display_names",
            "interim_results_enabled", "debug_mode",
            "speech_interaction_mode", "ptt_active",
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

    def test_set_stt_manager(self, sm):
        mgr = Mock()
        sm.set_stt_manager(mgr)
        assert sm._stt_manager is mgr

    def test_set_remote_stt_launcher(self, sm):
        launcher = Mock()
        sm.set_remote_stt_launcher(launcher)
        assert sm._remote_stt_launcher is launcher


# -----------------------------------------------------------------------
# STT mode/provider helpers
# -----------------------------------------------------------------------

class TestSTTHelpers:

    def test_get_stt_mode_default(self, sm):
        assert sm._get_current_stt_mode() == "remote"

    def test_get_stt_mode_from_config(self, sm, mock_config):
        mock_config._config["stt"] = {"mode": "in_process"}
        assert sm._get_current_stt_mode() == "in_process"

    def test_get_stt_provider_default(self, sm):
        assert sm._get_current_stt_provider() == "google_stt"

    def test_get_available_providers_no_launcher(self, sm):
        # Remote mode but no launcher - falls through to in-process check
        result = sm._get_available_stt_providers()
        assert isinstance(result, list)

    def test_get_provider_display_names_no_launcher(self, sm):
        result = sm._get_provider_display_names()
        assert isinstance(result, dict)

    def test_get_zipformer_variant_no_launcher(self, sm):
        assert sm._get_zipformer_variant() == "zipformer_cpu"

    def test_get_available_providers_with_launcher(self, sm):
        launcher = Mock()
        launcher.get_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud"},
            {"name": "zipformer", "display_name": "Zipformer"},
        ]
        sm._remote_stt_launcher = launcher
        result = sm._get_available_stt_providers()
        assert "google_stt" in result
        assert "zipformer_cpu" in result
        assert "zipformer_gpu" in result

    def test_get_provider_display_names_with_launcher(self, sm):
        launcher = Mock()
        launcher.get_providers.return_value = [
            {"name": "google_stt", "display_name": "Google Cloud"},
            {"name": "zipformer", "display_name": "Zipformer"},
        ]
        sm._remote_stt_launcher = launcher
        result = sm._get_provider_display_names()
        assert result["google_stt"] == "Google Cloud"
        assert result["zipformer_cpu"] == "Zipformer CPU"
        assert result["zipformer_gpu"] == "Zipformer GPU"


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

    def test_ptt_start_sends_state_update(self, sm, mock_gui_queue):
        sm.ptt_start()
        mock_gui_queue.put_nowait.assert_called()
        state = mock_gui_queue.put_nowait.call_args[0][0]
        assert state["action"] == "state_update"
        assert state["ptt_active"] is True

    def test_ptt_stop_disables_speech(self, sm):
        sm._speech_enabled = True
        sm._ptt_active = True
        sm.ptt_stop()
        assert sm._ptt_active is False
        assert sm._speech_enabled is False

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
        sm._ptt_active = True
        sm._speech_enabled = True
        sm.ptt_stop()
        mock_websocket_manager.set_transcription_status.assert_called_with(False, reason="ptt")

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
        sm._ptt_active = True
        sm._speech_enabled = True
        sm._ptt_safety_timeout()
        assert sm._ptt_active is False
        assert sm._speech_enabled is False

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
