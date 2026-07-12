"""Tests for BraviaPlugin.

Covers: initialization with EDID/SSDP discovery, brightness adjustment,
overflow cascade, PSK validation, health status, and graceful degradation.

P3-T3 of the test coverage improvement plan.
"""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.bravia_plugin import BraviaPlugin
import services.wheelhouse.plugins.bravia_plugin as bravia_mod
from services.wheelhouse.events import (
    HardwareBrightnessCommand,
    BrightnessStateChanged,
    BrightnessOverflowEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_event_bus():
    bus = Mock()
    bus.publish = AsyncMock()
    bus.subscribe = Mock()
    return bus


@pytest.fixture
def mock_bravia_control():
    """Mock BraviaControl."""
    ctrl = AsyncMock()
    ctrl.get_brightness = AsyncMock(return_value=50)
    ctrl.adjust_brightness = AsyncMock(return_value=True)
    return ctrl


@pytest.fixture
def mock_sony_display():
    """Mock display object for EDID discovery."""
    display = Mock()
    display.model = "SONY TV XR"
    return display


def _make_config(psk="my_secret_psk", extra=None):
    """Create mock config with bravia section."""
    plugins = {"bravia": {"psk": psk}}
    if extra:
        plugins["bravia"].update(extra)

    config = Mock()
    config.get = lambda key, default=None: {
        "plugins": plugins,
    }.get(key, default)
    return config


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestBraviaPluginInit:
    def test_name_is_bravia(self):
        p = BraviaPlugin()
        assert p.name == "bravia"

    def test_initial_state_is_uninitialized(self):
        p = BraviaPlugin()
        assert p.state == PluginState.UNINITIALIZED


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestBraviaInitialize:
    @pytest.mark.asyncio
    async def test_initialize_with_sony_display_and_ssdp(self, mock_event_bus, mock_sony_display):
        """Happy path: Sony display found via EDID, Bravia found via SSDP."""
        config = _make_config()

        with patch.object(bravia_mod, 'discover_displays', new_callable=AsyncMock, return_value=[mock_sony_display]):
            with patch.object(bravia_mod, 'find_sony_displays', return_value=[mock_sony_display]):
                with patch.object(bravia_mod, 'discover_bravia_ssdp', new_callable=AsyncMock, return_value="192.168.1.100"):
                    with patch.object(bravia_mod, 'validate_bravia_api', new_callable=AsyncMock, return_value=True):
                        with patch.object(bravia_mod, 'BraviaControl') as mock_ctor:
                            p = BraviaPlugin()
                            await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._bravia_control is not None

    @pytest.mark.asyncio
    async def test_initialize_no_sony_display(self, mock_event_bus):
        """No Sony display connected - plugin initializes but inactive."""
        config = _make_config()

        with patch.object(bravia_mod, 'discover_displays', new_callable=AsyncMock, return_value=[]):
            with patch.object(bravia_mod, 'find_sony_displays', return_value=[]):
                p = BraviaPlugin()
                await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._bravia_control is None

    @pytest.mark.asyncio
    async def test_initialize_no_psk_configured(self, mock_event_bus):
        """Missing PSK should raise ValueError."""
        config = _make_config(psk="")

        p = BraviaPlugin()
        with pytest.raises(ValueError, match="psk must be configured"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_placeholder_psk(self, mock_event_bus):
        """Placeholder PSK should raise ValueError."""
        config = _make_config(psk="your_psk_here")

        p = BraviaPlugin()
        with pytest.raises(ValueError, match="psk must be configured"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_ssdp_fails(self, mock_event_bus, mock_sony_display):
        """Sony display found but SSDP discovery fails."""
        config = _make_config()

        with patch.object(bravia_mod, 'discover_displays', new_callable=AsyncMock, return_value=[mock_sony_display]):
            with patch.object(bravia_mod, 'find_sony_displays', return_value=[mock_sony_display]):
                with patch.object(bravia_mod, 'discover_bravia_ssdp', new_callable=AsyncMock, return_value=None):
                    p = BraviaPlugin()
                    with pytest.raises(ValueError, match="no Bravia TV found"):
                        await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_psk_rejected(self, mock_event_bus, mock_sony_display):
        """TV found but PSK rejected."""
        config = _make_config()

        with patch.object(bravia_mod, 'discover_displays', new_callable=AsyncMock, return_value=[mock_sony_display]):
            with patch.object(bravia_mod, 'find_sony_displays', return_value=[mock_sony_display]):
                with patch.object(bravia_mod, 'discover_bravia_ssdp', new_callable=AsyncMock, return_value="192.168.1.100"):
                    with patch.object(bravia_mod, 'validate_bravia_api', new_callable=AsyncMock, return_value=False):
                        p = BraviaPlugin()
                        with pytest.raises(ValueError, match="rejected PSK"):
                            await p.initialize(config, mock_event_bus)


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestBraviaStartStop:
    @pytest.mark.asyncio
    async def test_start_with_hardware_subscribes(self, mock_event_bus, mock_bravia_control):
        p = BraviaPlugin()
        p._config = Mock()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.INITIALIZED

        await p.start()
        assert p.state == PluginState.RUNNING
        mock_event_bus.subscribe.assert_called_once_with(
            HardwareBrightnessCommand, p._handle_brightness_command
        )

    @pytest.mark.asyncio
    async def test_start_without_hardware_skips_subscription(self, mock_event_bus):
        p = BraviaPlugin()
        p._config = Mock()
        p._event_bus = mock_event_bus
        p._bravia_control = None
        p._state = PluginState.INITIALIZED

        await p.start()
        assert p.state == PluginState.RUNNING
        mock_event_bus.subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_initial_brightness_check(self, mock_event_bus, mock_bravia_control):
        p = BraviaPlugin()
        p._config = Mock()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.INITIALIZED

        await p.start()
        # Should have published initial state
        assert mock_event_bus.publish.call_count >= 1

    @pytest.mark.asyncio
    async def test_start_survives_connectivity_failure(self, mock_event_bus, mock_bravia_control):
        """If TV is offline during initial check, should still start."""
        mock_bravia_control.get_brightness = AsyncMock(return_value=None)

        p = BraviaPlugin()
        p._config = Mock()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.INITIALIZED

        await p.start()
        assert p.state == PluginState.RUNNING

    @pytest.mark.asyncio
    async def test_stop(self, mock_event_bus):
        p = BraviaPlugin()
        p._state = PluginState.RUNNING
        p._event_bus = mock_event_bus

        await p.stop()
        assert p.state == PluginState.STOPPED


# ---------------------------------------------------------------------------
# Brightness command handling
# ---------------------------------------------------------------------------

class TestBraviaBrightnessCommand:
    @pytest.mark.asyncio
    async def test_adjust_brightness_normal(self, mock_event_bus, mock_bravia_control):
        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=10)
        await p._handle_brightness_command(event)

        mock_bravia_control.adjust_brightness.assert_called_with(10)

    @pytest.mark.asyncio
    async def test_minimum_delta_enforcement(self, mock_event_bus, mock_bravia_control):
        """Delta of 1 should be scaled up to 2 for Bravia's 0-50 range."""
        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=1)
        await p._handle_brightness_command(event)

        mock_bravia_control.adjust_brightness.assert_called_with(2)

    @pytest.mark.asyncio
    async def test_negative_minimum_delta(self, mock_event_bus, mock_bravia_control):
        """Negative delta of -1 should be scaled to -2."""
        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=-1)
        await p._handle_brightness_command(event)

        mock_bravia_control.adjust_brightness.assert_called_with(-2)

    @pytest.mark.asyncio
    async def test_overflow_at_max(self, mock_event_bus, mock_bravia_control):
        """At max brightness, dim-up should overflow."""
        mock_bravia_control.get_brightness = AsyncMock(return_value=100)

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=5)
        await p._handle_brightness_command(event)

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].reason == "at_hardware_limit"

    @pytest.mark.asyncio
    async def test_overflow_at_min(self, mock_event_bus, mock_bravia_control):
        """At min brightness, dim-down should overflow."""
        mock_bravia_control.get_brightness = AsyncMock(return_value=0)

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=-5)
        await p._handle_brightness_command(event)

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].reason == "at_hardware_limit"

    @pytest.mark.asyncio
    async def test_overflow_when_tv_offline(self, mock_event_bus, mock_bravia_control):
        """TV offline should trigger device_offline overflow."""
        mock_bravia_control.get_brightness = AsyncMock(return_value=None)

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=5)
        await p._handle_brightness_command(event)

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].reason == "device_offline"

    @pytest.mark.asyncio
    async def test_adjust_returns_none_during_operation(self, mock_event_bus, mock_bravia_control):
        """TV goes offline during adjust - should overflow."""
        mock_bravia_control.adjust_brightness = AsyncMock(return_value=None)

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=5)
        await p._handle_brightness_command(event)

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) >= 1
        assert overflow_events[0].reason == "device_offline"

    @pytest.mark.asyncio
    async def test_adjust_returns_false(self, mock_event_bus, mock_bravia_control):
        """Command rejected but TV online - no overflow, just error logged."""
        mock_bravia_control.adjust_brightness = AsyncMock(return_value=False)

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=5)
        await p._handle_brightness_command(event)

        # No overflow - just an error
        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        overflow_events = [e for e in published if isinstance(e, BrightnessOverflowEvent)]
        assert len(overflow_events) == 0

    @pytest.mark.asyncio
    async def test_zero_delta(self, mock_event_bus, mock_bravia_control):
        """Zero delta should not enforce minimum."""
        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=0)
        await p._handle_brightness_command(event)

        mock_bravia_control.adjust_brightness.assert_called_with(0)


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestBraviaHealth:
    def test_healthy_with_known_brightness(self):
        p = BraviaPlugin()
        p._state = PluginState.RUNNING
        p._last_known_brightness = 50
        status = p.get_health_status()
        assert status["status"] == "healthy"

    def test_degraded_without_known_brightness(self):
        p = BraviaPlugin()
        p._state = PluginState.RUNNING
        p._last_known_brightness = None
        status = p.get_health_status()
        assert status["status"] == "degraded"

    def test_unhealthy_when_failed(self):
        p = BraviaPlugin()
        p._state = PluginState.FAILED
        status = p.get_health_status()
        assert status["status"] == "unhealthy"

    def test_health_includes_tv_connected(self):
        p = BraviaPlugin()
        p._last_known_brightness = 75
        status = p.get_health_status()
        assert status["details"]["tv_connected"] is True

    def test_health_includes_error(self):
        p = BraviaPlugin()
        p._last_error = "timeout"
        status = p.get_health_status()
        assert status["details"]["error"] == "timeout"


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------

class TestBraviaAdversarial:
    @pytest.mark.asyncio
    async def test_exception_in_command_handler(self, mock_event_bus, mock_bravia_control):
        """Exception during brightness command should be caught."""
        mock_bravia_control.get_brightness = AsyncMock(side_effect=RuntimeError("network gone"))

        p = BraviaPlugin()
        p._event_bus = mock_event_bus
        p._bravia_control = mock_bravia_control
        p._state = PluginState.RUNNING

        event = HardwareBrightnessCommand(delta=5)
        await p._handle_brightness_command(event)  # Should not raise

        assert p._last_error is not None

    @pytest.mark.asyncio
    async def test_publish_state_updates_last_known(self, mock_event_bus):
        """_publish_state_change should update tracking fields."""
        p = BraviaPlugin()
        p._event_bus = mock_event_bus

        await p._publish_state_change(75)

        assert p._last_known_brightness == 75
        assert p._last_health_check is not None
