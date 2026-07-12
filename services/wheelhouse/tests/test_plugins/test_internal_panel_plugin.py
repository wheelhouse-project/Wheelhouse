"""Tests for InternalPanelPlugin.

Covers: initialization, hardware detection, brightness adjustment,
overflow cascade, health status, and graceful degradation on desktops.

P3-T3 of the test coverage improvement plan.
"""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.internal_panel_plugin import InternalPanelPlugin
import services.wheelhouse.plugins.internal_panel_plugin as panel_mod
from services.wheelhouse.events import (
    HardwareBrightnessCommand,
    BrightnessStateChanged,
    BrightnessOverflowEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config():
    """Mock ConfigService."""
    config = Mock()
    config.get = lambda key, default=None: {
        "plugins.internal_panel.enabled": True,
    }.get(key, default)
    return config


@pytest.fixture
def mock_event_bus():
    """Mock EventBus."""
    bus = Mock()
    bus.publish = AsyncMock()
    bus.subscribe = Mock()
    return bus


@pytest.fixture
def mock_display_control():
    """Mock InternalPanelControl that reports hardware available."""
    ctrl = AsyncMock()
    ctrl.initialize = AsyncMock(return_value=True)
    ctrl.get_brightness = AsyncMock(return_value=50)
    ctrl.set_brightness = AsyncMock(return_value=True)
    return ctrl


@pytest.fixture
def plugin_with_hardware(mock_config, mock_event_bus, mock_display_control):
    """Plugin pre-configured with hardware available (no async init needed)."""
    p = InternalPanelPlugin()
    p._config = mock_config
    p._event_bus = mock_event_bus
    p._display_control = mock_display_control
    p._is_hardware_available = True
    p._current_brightness = 50
    p._state = PluginState.INITIALIZED
    return p


@pytest.fixture
def plugin_without_hardware(mock_config, mock_event_bus):
    """Plugin pre-configured without hardware (desktop machine)."""
    p = InternalPanelPlugin()
    p._config = mock_config
    p._event_bus = mock_event_bus
    p._is_hardware_available = False
    p._state = PluginState.INITIALIZED
    return p


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestInternalPanelInit:
    def test_name_is_internal_panel(self):
        p = InternalPanelPlugin()
        assert p.name == "internal_panel"

    def test_initial_state_is_uninitialized(self):
        p = InternalPanelPlugin()
        assert p.state == PluginState.UNINITIALIZED

    def test_hardware_not_available_initially(self):
        p = InternalPanelPlugin()
        assert p._is_hardware_available is False


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInternalPanelInitialize:
    @pytest.mark.asyncio
    async def test_initialize_with_hardware(self, mock_config, mock_event_bus, mock_display_control):
        with patch.object(
            panel_mod,
            "InternalPanelControl",
            return_value=mock_display_control,
        ):
            p = InternalPanelPlugin()
            await p.initialize(mock_config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._is_hardware_available is True
        assert p._current_brightness == 50

    @pytest.mark.asyncio
    async def test_initialize_without_hardware(self, mock_config, mock_event_bus):
        no_hw = AsyncMock()
        no_hw.initialize = AsyncMock(return_value=False)

        with patch.object(
            panel_mod,
            "InternalPanelControl",
            return_value=no_hw,
        ):
            p = InternalPanelPlugin()
            await p.initialize(mock_config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._is_hardware_available is False

    @pytest.mark.asyncio
    async def test_initialize_disabled_in_config(self, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.internal_panel.enabled": False,
        }.get(key, default)

        p = InternalPanelPlugin()
        await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._is_hardware_available is False
        # Should NOT have created display control
        assert p._display_control is None

    @pytest.mark.asyncio
    async def test_initialize_handles_brightness_read_failure(self, mock_config, mock_event_bus):
        ctrl = AsyncMock()
        ctrl.initialize = AsyncMock(return_value=True)
        ctrl.get_brightness = AsyncMock(return_value=None)

        with patch.object(
            panel_mod,
            "InternalPanelControl",
            return_value=ctrl,
        ):
            p = InternalPanelPlugin()
            await p.initialize(mock_config, mock_event_bus)

        assert p._is_hardware_available is False
        assert p.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_handles_exception(self, mock_config, mock_event_bus):
        with patch.object(
            panel_mod,
            "InternalPanelControl",
            side_effect=RuntimeError("WMI explosion"),
        ):
            p = InternalPanelPlugin()
            await p.initialize(mock_config, mock_event_bus)

        assert p.state == PluginState.FAILED


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestInternalPanelStartStop:
    @pytest.mark.asyncio
    async def test_start_with_hardware_subscribes(self, plugin_with_hardware, mock_event_bus):
        await plugin_with_hardware.start()
        assert plugin_with_hardware.state == PluginState.RUNNING
        mock_event_bus.subscribe.assert_called_once_with(
            HardwareBrightnessCommand,
            plugin_with_hardware._handle_brightness_command,
        )
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_start_without_hardware_does_not_subscribe(self, plugin_without_hardware, mock_event_bus):
        await plugin_without_hardware.start()
        assert plugin_without_hardware.state == PluginState.RUNNING
        mock_event_bus.subscribe.assert_not_called()
        await plugin_without_hardware.stop()

    @pytest.mark.asyncio
    async def test_stop_sets_stopped(self, plugin_with_hardware):
        await plugin_with_hardware.start()
        await plugin_with_hardware.stop()
        assert plugin_with_hardware.state == PluginState.STOPPED


# ---------------------------------------------------------------------------
# Brightness command handling
# ---------------------------------------------------------------------------

class TestInternalPanelBrightnessCommand:
    @pytest.mark.asyncio
    async def test_adjust_brightness_within_range(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.set_brightness.assert_called_with(60)
        assert plugin_with_hardware._current_brightness == 60
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_adjust_brightness_down(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=-20)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.set_brightness.assert_called_with(30)
        assert plugin_with_hardware._current_brightness == 30
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_overflow_at_maximum(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        """Delta that exceeds 100 should clamp and publish overflow."""
        plugin_with_hardware._current_brightness = 90
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=20)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.set_brightness.assert_called_with(100)
        assert plugin_with_hardware._current_brightness == 100

        # Should have published both state change and overflow
        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].delta == 10  # 90 + 20 - 100 = 10 overflow
        assert overflow_events[0].reason == "at_hardware_limit"
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_overflow_at_minimum(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        """Delta that goes below 0 should clamp and publish overflow."""
        plugin_with_hardware._current_brightness = 10
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=-30)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.set_brightness.assert_called_with(0)
        assert plugin_with_hardware._current_brightness == 0

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].delta == -20  # 10 - 30 = -20 overflow
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_ignores_command_when_hardware_unavailable(self, plugin_without_hardware, mock_event_bus):
        await plugin_without_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_without_hardware._handle_brightness_command(event)

        mock_event_bus.publish.assert_not_called()
        await plugin_without_hardware.stop()

    @pytest.mark.asyncio
    async def test_uses_wmi_when_no_cached_brightness(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        """When no cached brightness, should query WMI first."""
        plugin_with_hardware._current_brightness = None
        mock_display_control.get_brightness = AsyncMock(return_value=40)
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.get_brightness.assert_called()
        mock_display_control.set_brightness.assert_called_with(50)
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_overflow_when_device_offline(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        """When WMI read returns None (device offline), should publish overflow."""
        plugin_with_hardware._current_brightness = None
        mock_display_control.get_brightness = AsyncMock(return_value=None)
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_with_hardware._handle_brightness_command(event)

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].reason == "device_offline"
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_set_brightness_failure_does_not_update_cache(
        self, plugin_with_hardware, mock_event_bus, mock_display_control
    ):
        """Failed set_brightness should not update the cached value."""
        mock_display_control.set_brightness = AsyncMock(return_value=False)
        plugin_with_hardware._current_brightness = 50
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_with_hardware._handle_brightness_command(event)

        # Cache should remain at 50 (not updated to 60)
        assert plugin_with_hardware._current_brightness == 50
        await plugin_with_hardware.stop()


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestInternalPanelHealth:
    def test_healthy_when_running_with_hardware(self, plugin_with_hardware):
        plugin_with_hardware._state = PluginState.RUNNING
        status = plugin_with_hardware.get_health_status()
        assert status["status"] == "healthy"
        assert status["hardware_available"] is True

    def test_healthy_without_hardware(self, plugin_without_hardware):
        """No hardware is normal on desktop - still healthy."""
        plugin_without_hardware._state = PluginState.RUNNING
        status = plugin_without_hardware.get_health_status()
        assert status["status"] == "healthy"
        assert status["hardware_available"] is False

    def test_unhealthy_when_failed(self, plugin_with_hardware):
        plugin_with_hardware._state = PluginState.FAILED
        plugin_with_hardware._last_error = "WMI boom"
        status = plugin_with_hardware.get_health_status()
        assert status["status"] == "unhealthy"
        assert status["last_error"] == "WMI boom"

    def test_health_includes_current_brightness(self, plugin_with_hardware):
        plugin_with_hardware._current_brightness = 75
        status = plugin_with_hardware.get_health_status()
        assert status["current_brightness"] == 75

    def test_health_includes_brightness_range(self, plugin_with_hardware):
        status = plugin_with_hardware.get_health_status()
        assert status["brightness_range"] == (0, 100)


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------

class TestInternalPanelAdversarial:
    @pytest.mark.asyncio
    async def test_exception_in_command_handler_is_caught(
        self, plugin_with_hardware, mock_event_bus, mock_display_control
    ):
        """Exceptions in brightness handler should be caught, not crash plugin."""
        mock_display_control.set_brightness = AsyncMock(side_effect=RuntimeError("WMI exploded"))
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=10)
        await plugin_with_hardware._handle_brightness_command(event)

        # Should not raise - error is caught and logged
        assert plugin_with_hardware._last_error is not None
        await plugin_with_hardware.stop()

    @pytest.mark.asyncio
    async def test_publish_state_with_no_event_bus(self, plugin_with_hardware):
        """Publish methods should handle missing event bus gracefully."""
        plugin_with_hardware._event_bus = None
        # Should not raise
        await plugin_with_hardware._publish_brightness_state()

    @pytest.mark.asyncio
    async def test_publish_overflow_with_no_event_bus(self, plugin_with_hardware):
        """Overflow publish should handle missing event bus gracefully."""
        plugin_with_hardware._event_bus = None
        await plugin_with_hardware._publish_overflow_event(10, "test")

    @pytest.mark.asyncio
    async def test_zero_delta_command(self, plugin_with_hardware, mock_event_bus, mock_display_control):
        """Zero delta should still set brightness (target == current)."""
        plugin_with_hardware._current_brightness = 50
        await plugin_with_hardware.start()

        event = HardwareBrightnessCommand(delta=0)
        await plugin_with_hardware._handle_brightness_command(event)

        mock_display_control.set_brightness.assert_called_with(50)
        await plugin_with_hardware.stop()
