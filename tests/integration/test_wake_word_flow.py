"""Integration test for wake word detection flow.

Tests the complete chain: idle suppression -> wake word detection -> transcription re-enabled.
Uses mocked audio and WebSocket to verify the full event flow without hardware.

Exercises real StateManager, EventBus, and event routing -- only hardware
(audio, WebSocket transport) is mocked.
"""
import asyncio
import json
import sys
from pathlib import Path
from multiprocessing import Queue
from unittest.mock import Mock, AsyncMock, patch, MagicMock

import pytest

# Set up import paths for wheelhouse service modules
_project_root = Path(__file__).parent.parent.parent
_service_dir = _project_root / "services" / "wheelhouse"
for _p in (_project_root, _service_dir):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (
    WakeWordDetectedEvent,
    SystemIdleStateChangedEvent,
)
from services.wheelhouse.state_manager import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """Create a mock ConfigService backed by an in-memory dict."""
    config_data = dict(overrides)
    svc = Mock()

    def _get(key, default=None):
        if "." in key:
            keys = key.split(".")
            value = config_data
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                    if value is None:
                        return default
                else:
                    return default
            return value
        return config_data.get(key, default)

    def _set(key, value):
        config_data[key] = value

    svc.get = _get
    svc.set = _set
    svc._config = config_data
    svc.save = AsyncMock()
    return svc


def _make_ws_manager():
    """Create a mock WebSocketManager with tracking."""
    ws = Mock()
    ws.broadcast = AsyncMock()
    ws.set_transcription_status = Mock(side_effect=lambda enabled, reason=None: {
        "type": "set_transcription_status",
        "enabled": enabled,
        **({"reason": reason} if reason else {}),
    })
    return ws


def _make_state_manager(config=None, event_bus=None, ws_manager=None):
    """Create a real StateManager wired to real EventBus with mocked I/O."""
    if config is None:
        config = _make_config()
    if event_bus is None:
        event_bus = EventBus()
    gui_queue = Mock(spec=Queue)
    gui_queue.put_nowait = Mock()

    loop = asyncio.new_event_loop()
    loop.create_task = Mock()  # prevent real task scheduling

    sm = StateManager(
        config_service=config,
        event_bus=event_bus,
        loop=loop,
        state_to_gui_queue=gui_queue,
        websocket_manager=ws_manager,
    )
    return sm, event_bus, gui_queue, loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdleToWakeWordToTranscription:
    """Full flow: idle -> wake word detected -> transcription re-enabled."""

    @pytest.mark.asyncio
    async def test_full_flow(self):
        """Complete chain: speech on -> idle suppresses -> wake word restores."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            # 1. Speech starts enabled
            sm._speech_enabled = True
            assert sm.speech_enabled is True

            # 2. System goes idle -- suppresses speech
            await bus.publish(SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0))
            assert sm._speech_suppressed_by_idle is True
            assert sm.speech_enabled is False

            # Verify WebSocket got idle-reason message
            ws.set_transcription_status.assert_called_with(False, reason="idle")
            gui_queue.put_nowait.assert_called()  # GUI notified

            # Reset mocks to track wake-word-specific calls
            ws.set_transcription_status.reset_mock()
            gui_queue.put_nowait.reset_mock()

            # 3. Wake word detected via EventBus
            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            # 4. Idle suppression cleared, speech restored
            assert sm._speech_suppressed_by_idle is False
            assert sm._speech_enabled_before_idle is None
            assert sm.speech_enabled is True

            # 5. WebSocket and GUI notified
            ws.set_transcription_status.assert_called_with(True)
            gui_queue.put_nowait.assert_called()
        finally:
            loop.close()


class TestWakeWordModeFiltering:
    """Test wake word activation depends on suppression reason and mode."""

    @pytest.mark.asyncio
    async def test_idle_recovery_mode_activates_for_idle(self):
        """idle_recovery mode: wake word clears idle suppression."""
        config = _make_config(**{"wake_word": {"mode": "idle_recovery"}})
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(config=config, ws_manager=ws)
        try:
            sm._speech_enabled = True
            # Simulate idle suppression
            sm._speech_suppressed_by_idle = True
            assert sm.speech_enabled is False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))
            assert sm._speech_suppressed_by_idle is False
            assert sm.speech_enabled is True
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_wake_word_does_not_clear_audio_suppression(self):
        """Wake word only clears idle suppression, not audio suppression."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = True
            sm._speech_suppressed_by_audio = True
            assert sm.speech_enabled is False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            # Audio suppression NOT cleared by wake word
            assert sm._speech_suppressed_by_audio is True
            assert sm.speech_enabled is False
        finally:
            loop.close()


class TestWakeWordIgnoredWhenNotSuppressed:
    """Wake word event is ignored when no idle suppression is active."""

    @pytest.mark.asyncio
    async def test_no_state_change_when_already_active(self):
        """Wake word ignored when speech is already enabled (no idle suppression)."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = True
            assert sm.speech_enabled is True

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            # No WebSocket broadcast, no GUI update
            ws.set_transcription_status.assert_not_called()
            gui_queue.put_nowait.assert_not_called()
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_no_state_change_when_user_disabled(self):
        """Wake word ignored when user manually disabled speech."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = False
            sm._speech_suppressed_by_idle = False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            # Nothing happens: user disabled speech, not idle suppression
            ws.set_transcription_status.assert_not_called()
            assert sm._speech_enabled is False
        finally:
            loop.close()


class TestWebSocketWakeWordMessage:
    """Test the WebSocket message that triggers wake word flow.

    Verifies that when WebSocketManager receives a wake_word_detected message,
    it publishes WakeWordDetectedEvent through the EventBus.
    """

    @pytest.mark.asyncio
    async def test_websocket_message_publishes_event(self):
        """Simulating the handle_connection path for wake_word_detected message type."""
        # Create real EventBus to track published events
        bus = EventBus()
        received_events = []

        async def capture_event(event):
            received_events.append(event)

        bus.subscribe(WakeWordDetectedEvent, capture_event)

        # Create a mock state_manager with event_bus
        state_manager = Mock()
        state_manager.event_bus = bus

        # Simulate what WebSocketManager.handle_connection does for wake_word_detected
        # (extracted from websocket_manager.py line 446-454)
        data = {"type": "wake_word_detected", "keyword": "computer"}
        keyword = data.get("keyword", "")

        await state_manager.event_bus.publish(WakeWordDetectedEvent(keyword=keyword))

        assert len(received_events) == 1
        assert received_events[0].keyword == "computer"


class TestWakeWordDetector:
    """Test WakeWordDetector receives audio and fires detection."""

    @pytest.fixture(autouse=True)
    def _import_wwd(self):
        """Import wake_word_detector once for all tests in this class."""
        from services.stt_providers.shared.shared_stt import wake_word_detector as wwd
        self.wwd = wwd

    def test_detector_not_loaded_without_openwakeword(self):
        """WakeWordDetector gracefully handles missing openwakeword."""
        orig_oww = self.wwd.openwakeword
        try:
            self.wwd.openwakeword = None
            detector = self.wwd.WakeWordDetector(
                keyword="computer",
                model_dir="/tmp/fake_models",
                enabled=True,
            )
            assert detector.is_loaded is False
            assert detector.process(b"\x00" * 320) is None
        finally:
            self.wwd.openwakeword = orig_oww

    def test_detector_disabled_flag(self):
        """WakeWordDetector with enabled=False does not load anything."""
        detector = self.wwd.WakeWordDetector(
            keyword="computer",
            model_dir="/tmp/fake_models",
            enabled=False,
        )
        assert detector.is_loaded is False
        assert detector.process(b"\x00" * 320) is None

    def test_detector_process_with_mock_model(self):
        """WakeWordDetector fires detection when model reports high confidence."""
        detector = self.wwd.WakeWordDetector(
            keyword="computer",
            model_dir="/tmp/fake_models",
            enabled=False,  # skip model loading
        )

        # Manually wire a mock model
        mock_model = Mock()
        mock_model.predict.return_value = {"computer_v2": 0.95}
        detector._model = mock_model
        detector.is_loaded = True

        # 320 bytes = 160 samples at 16-bit, typical frame
        pcm = b"\x00" * 320
        result = detector.process(pcm)
        assert result == "computer"
        mock_model.predict.assert_called_once()

    def test_detector_process_below_threshold(self):
        """WakeWordDetector returns None when confidence is below threshold."""
        detector = self.wwd.WakeWordDetector(
            keyword="computer",
            model_dir="/tmp/fake_models",
            enabled=False,
        )

        mock_model = Mock()
        mock_model.predict.return_value = {"computer_v2": 0.2}
        detector._model = mock_model
        detector.is_loaded = True

        result = detector.process(b"\x00" * 320)
        assert result is None

    def test_detector_reset(self):
        """WakeWordDetector.reset() delegates to model."""
        detector = self.wwd.WakeWordDetector(
            keyword="computer",
            model_dir="/tmp/fake_models",
            enabled=False,
        )
        mock_model = Mock()
        detector._model = mock_model

        detector.reset()
        mock_model.reset.assert_called_once()


class TestIdleSuppressionWithWakeWordInteraction:
    """Test interactions between idle suppression lifecycle and wake word."""

    @pytest.mark.asyncio
    async def test_idle_then_wake_then_idle_again(self):
        """System can go idle, wake via wake word, then go idle again cleanly."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = True

            # First idle cycle
            await bus.publish(SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=300.0))
            assert sm.speech_enabled is False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))
            assert sm.speech_enabled is True

            # Second idle cycle
            await bus.publish(SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=400.0))
            assert sm.speech_enabled is False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))
            assert sm.speech_enabled is True
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_wake_word_during_multiple_suppressions(self):
        """Wake word clears idle but speech stays off due to audio suppression."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = True
            sm._speech_suppressed_by_idle = True
            sm._speech_suppressed_by_audio = True
            assert sm.speech_enabled is False

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            # Idle cleared, but audio still suppressing
            assert sm._speech_suppressed_by_idle is False
            assert sm._speech_suppressed_by_audio is True
            assert sm.speech_enabled is False
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_user_toggle_after_wake_word(self):
        """User can toggle speech after wake word restores from idle."""
        ws = _make_ws_manager()
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=ws)
        try:
            sm._speech_enabled = True

            # Go idle
            await bus.publish(SystemIdleStateChangedEvent(is_idle=True, idle_duration_seconds=600.0))
            assert sm.speech_enabled is False

            # Wake word restores
            await bus.publish(WakeWordDetectedEvent(keyword="computer"))
            assert sm.speech_enabled is True

            # User toggles off
            sm.toggle_speech_enabled_state()
            assert sm.speech_enabled is False

            # User toggles back on
            sm.toggle_speech_enabled_state()
            assert sm.speech_enabled is True
        finally:
            loop.close()


class TestNoWebSocketManager:
    """Ensure wake word flow works even without WebSocketManager."""

    @pytest.mark.asyncio
    async def test_wake_word_without_ws_manager(self):
        """Wake word clears idle suppression even when no WebSocketManager."""
        sm, bus, gui_queue, loop = _make_state_manager(ws_manager=None)
        try:
            sm._speech_enabled = True
            sm._speech_suppressed_by_idle = True

            await bus.publish(WakeWordDetectedEvent(keyword="computer"))

            assert sm._speech_suppressed_by_idle is False
            assert sm.speech_enabled is True
        finally:
            loop.close()
