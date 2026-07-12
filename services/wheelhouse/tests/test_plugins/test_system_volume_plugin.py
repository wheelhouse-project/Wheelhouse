"""Tests for SystemVolumePlugin.

Covers: initialization, config validation, volume adjustment,
dB scale conversion, device connection, COM error recovery, health status.

P3-T3 of the test coverage improvement plan.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock, PropertyMock

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.events import VolumeAdjustCommand, PTTStartedEvent, PTTStoppedEvent


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
def mock_volume_router():
    """Mock VolumeRouter that selects system volume."""
    router = Mock()
    router.use_system_volume = True
    router.use_sonos = False
    router.sonos_ip = None
    return router


@pytest.fixture
def mock_volume_interface():
    """Mock IAudioEndpointVolume COM interface."""
    iface = Mock()
    iface.GetMasterVolumeLevel = Mock(return_value=-20.0)
    iface.SetMasterVolumeLevel = Mock()
    return iface


def _make_config(overrides=None):
    """Create mock config with system_volume section."""
    defaults = {
        "plugins.system_volume.device_type": "default",
        "plugins.system_volume.volume_step_db": 3.0,
        "plugins.system_volume.min_volume_db": -65.25,
        "plugins.system_volume.max_volume_db": 0.0,
    }
    if overrides:
        defaults.update(overrides)
    config = Mock()
    config.get = lambda key, default=None: defaults.get(key, default)
    return config


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestSystemVolumeInit:
    def test_name_is_system_volume(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        assert p.name == "system_volume"

    def test_initial_state(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        assert p.state == PluginState.UNINITIALIZED
        assert p._volume_interface is None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestSystemVolumeInitialize:
    @pytest.mark.asyncio
    async def test_initialize_with_default_config(self, mock_event_bus):
        config = _make_config()

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(return_value=Mock())
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._volume_step_db == 3.0
        assert p._device_type == "default"

    @pytest.mark.asyncio
    async def test_initialize_rejects_zero_step(self, mock_event_bus):
        config = _make_config({"plugins.system_volume.volume_step_db": 0})

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        with pytest.raises(ValueError, match="volume_step_db must be positive"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_negative_step(self, mock_event_bus):
        config = _make_config({"plugins.system_volume.volume_step_db": -1.0})

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        with pytest.raises(ValueError, match="volume_step_db must be positive"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_invalid_range(self, mock_event_bus):
        config = _make_config({
            "plugins.system_volume.min_volume_db": 0.0,
            "plugins.system_volume.max_volume_db": -10.0,
        })

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        with pytest.raises(ValueError, match="min_volume_db must be less"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_equal_range(self, mock_event_bus):
        config = _make_config({
            "plugins.system_volume.min_volume_db": 0.0,
            "plugins.system_volume.max_volume_db": 0.0,
        })

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        with pytest.raises(ValueError, match="min_volume_db must be less"):
            await p.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_core_audio_unavailable(self, mock_event_bus):
        config = _make_config()

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(side_effect=RuntimeError("COM not available"))
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            with pytest.raises(ImportError, match="Cannot access Windows Core Audio"):
                await p.initialize(config, mock_event_bus)


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestSystemVolumeStartStop:
    @pytest.mark.asyncio
    async def test_start_subscribes_when_router_selects(self, mock_event_bus, mock_volume_router):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._config = _make_config()
        p._event_bus = mock_event_bus
        p._state = PluginState.INITIALIZED

        mock_device = Mock()
        mock_device.Activate = Mock(return_value=Mock(QueryInterface=Mock(return_value=Mock())))

        with patch("services.wheelhouse.plugins.system_volume_plugin.get_volume_router", return_value=mock_volume_router):
            with patch.object(p, '_connect_audio_device', new_callable=AsyncMock):
                await p.start()

        assert p.state == PluginState.RUNNING
        # VolumeAdjustCommand + PTTStartedEvent + PTTStoppedEvent = 3 subscriptions
        assert mock_event_bus.subscribe.call_count == 3

    @pytest.mark.asyncio
    async def test_start_no_subscribe_when_router_selects_sonos(self, mock_event_bus):
        router = Mock()
        router.use_system_volume = False

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._config = _make_config()
        p._event_bus = mock_event_bus
        p._state = PluginState.INITIALIZED

        with patch("services.wheelhouse.plugins.system_volume_plugin.get_volume_router", return_value=router):
            with patch.object(p, '_connect_audio_device', new_callable=AsyncMock):
                await p.start()

        assert p.state == PluginState.RUNNING
        # PTT subscriptions always happen even when Sonos handles volume
        assert mock_event_bus.subscribe.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_releases_interface(self, mock_event_bus):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = Mock()
        p._device_name = "Test"
        p._state = PluginState.RUNNING

        await p.stop()
        assert p._volume_interface is None
        assert p._device_name is None
        assert p.state == PluginState.STOPPED


# ---------------------------------------------------------------------------
# Volume adjustment
# ---------------------------------------------------------------------------

class TestSystemVolumeAdjust:
    @pytest.mark.asyncio
    async def test_volume_up(self, mock_volume_interface):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        event = VolumeAdjustCommand(delta=1)  # +1 * 3.0 = +3.0dB
        await p._handle_volume_adjust(event)

        mock_volume_interface.SetMasterVolumeLevel.assert_called_once()
        args = mock_volume_interface.SetMasterVolumeLevel.call_args[0]
        assert args[0] == pytest.approx(-17.0, abs=0.1)  # -20 + 3 = -17

    @pytest.mark.asyncio
    async def test_volume_down(self, mock_volume_interface):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        event = VolumeAdjustCommand(delta=-2)  # -2 * 3.0 = -6.0dB
        await p._handle_volume_adjust(event)

        args = mock_volume_interface.SetMasterVolumeLevel.call_args[0]
        assert args[0] == pytest.approx(-26.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_volume_clamped_at_max(self, mock_volume_interface):
        """Volume at -1.0 dB + 3.0 dB step should clamp to 0.0."""
        mock_volume_interface.GetMasterVolumeLevel = Mock(return_value=-1.0)

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        event = VolumeAdjustCommand(delta=1)
        await p._handle_volume_adjust(event)

        args = mock_volume_interface.SetMasterVolumeLevel.call_args[0]
        assert args[0] == 0.0  # Clamped to max

    @pytest.mark.asyncio
    async def test_volume_clamped_at_min(self, mock_volume_interface):
        mock_volume_interface.GetMasterVolumeLevel = Mock(return_value=-64.0)

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        event = VolumeAdjustCommand(delta=-1)
        await p._handle_volume_adjust(event)

        args = mock_volume_interface.SetMasterVolumeLevel.call_args[0]
        assert args[0] == -65.25  # Clamped to min

    @pytest.mark.asyncio
    async def test_no_change_when_already_at_limit(self, mock_volume_interface):
        """When already at max and adjusting up, should skip set."""
        mock_volume_interface.GetMasterVolumeLevel = Mock(return_value=0.0)

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        event = VolumeAdjustCommand(delta=1)
        await p._handle_volume_adjust(event)

        mock_volume_interface.SetMasterVolumeLevel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_interface_warns(self, mock_event_bus):
        """Missing volume interface should log warning, not crash."""
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = None

        event = VolumeAdjustCommand(delta=1)
        await p._handle_volume_adjust(event)  # Should not raise


# ---------------------------------------------------------------------------
# Device connection
# ---------------------------------------------------------------------------

class TestSystemVolumeDeviceConnection:
    def test_get_audio_device_default(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "default"

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_speaker = Mock()
            mock_au.GetSpeakers = Mock(return_value=mock_speaker)
            result = p._get_audio_device()

        assert result == mock_speaker

    def test_get_audio_device_named_not_found_falls_back(self):
        """Named device not found should fall back to default."""
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "My Headphones"

        mock_device = Mock()
        mock_device.FriendlyName = "Something Else"
        mock_speaker = Mock()

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetAllDevices = Mock(return_value=[mock_device])
            mock_au.GetSpeakers = Mock(return_value=mock_speaker)
            result = p._get_audio_device()

        assert result == mock_speaker  # Fell back to default

    def test_get_audio_device_named_found(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "My Headphones"

        mock_device = Mock()
        mock_device.FriendlyName = "My Headphones"

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetAllDevices = Mock(return_value=[mock_device])
            result = p._get_audio_device()

        assert result == mock_device

    def test_get_audio_device_exception_returns_none(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "default"

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(side_effect=RuntimeError("COM error"))
            result = p._get_audio_device()

        assert result is None


# ---------------------------------------------------------------------------
# COM error recovery
# ---------------------------------------------------------------------------

class TestSystemVolumeCOMRecovery:
    @pytest.mark.asyncio
    async def test_com_error_triggers_reconnect(self, mock_volume_interface, mock_event_bus):
        """COMError during volume adjust should attempt reconnect."""
        from comtypes import COMError
        mock_volume_interface.GetMasterVolumeLevel = Mock(
            side_effect=COMError(-2147467259, "RPC server unavailable", ())
        )

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0
        p._event_bus = mock_event_bus

        with patch.object(p, '_connect_audio_device', new_callable=AsyncMock) as mock_reconnect:
            event = VolumeAdjustCommand(delta=1)
            await p._handle_volume_adjust(event)

        mock_reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_attribute_error_no_reconnect(self, mock_volume_interface):
        """AttributeError should NOT attempt reconnect."""
        mock_volume_interface.GetMasterVolumeLevel = Mock(side_effect=AttributeError("missing"))

        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._volume_interface = mock_volume_interface
        p._volume_step_db = 3.0
        p._min_volume_db = -65.25
        p._max_volume_db = 0.0

        with patch.object(p, '_connect_audio_device', new_callable=AsyncMock) as mock_reconnect:
            event = VolumeAdjustCommand(delta=1)
            await p._handle_volume_adjust(event)

        mock_reconnect.assert_not_called()


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestSystemVolumeHealth:
    def test_healthy_with_interface(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._state = PluginState.RUNNING
        p._volume_interface = Mock()
        status = p.get_health_status()
        assert status["status"] == "healthy"
        assert status["connected"] is True

    def test_degraded_running_no_interface(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._state = PluginState.RUNNING
        p._volume_interface = None
        p._last_error = None
        status = p.get_health_status()
        assert status["status"] == "degraded"

    def test_unhealthy_with_error(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._state = PluginState.RUNNING
        p._volume_interface = None
        p._last_error = "device disconnected"
        status = p.get_health_status()
        assert status["status"] == "unhealthy"

    def test_health_includes_config(self):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "communications"
        p._volume_step_db = 5.0
        status = p.get_health_status()
        assert status["device_type"] == "communications"
        assert status["volume_step_db"] == 5.0


# ---------------------------------------------------------------------------
# PTT audio muting
# ---------------------------------------------------------------------------

class TestPTTAudioMuting:
    """Test that SystemVolumePlugin mutes/restores volume during PTT."""

    @pytest.fixture
    def plugin_with_volume(self):
        """Plugin with mocked volume interface."""
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._volume_interface = MagicMock()
        plugin._min_volume_db = -65.25
        plugin._state = PluginState.RUNNING
        return plugin

    @pytest.mark.asyncio
    async def test_ptt_started_saves_and_mutes(self, plugin_with_volume):
        plugin = plugin_with_volume
        plugin._volume_interface.GetMasterVolumeLevel.return_value = -10.0

        event = PTTStartedEvent(source="floating_button")
        await plugin._handle_ptt_started(event)

        assert plugin._pre_ptt_volume_db == -10.0
        plugin._volume_interface.SetMasterVolumeLevel.assert_called_with(-65.25, None)

    @pytest.mark.asyncio
    async def test_ptt_stopped_restores_volume(self, plugin_with_volume):
        plugin = plugin_with_volume
        plugin._pre_ptt_volume_db = -10.0

        event = PTTStoppedEvent(reason="released")
        await plugin._handle_ptt_stopped(event)

        plugin._volume_interface.SetMasterVolumeLevel.assert_called_with(-10.0, None)
        assert plugin._pre_ptt_volume_db is None

    @pytest.mark.asyncio
    async def test_ptt_stopped_noop_when_no_saved_volume(self, plugin_with_volume):
        plugin = plugin_with_volume
        plugin._pre_ptt_volume_db = None

        event = PTTStoppedEvent(reason="released")
        await plugin._handle_ptt_stopped(event)

        plugin._volume_interface.SetMasterVolumeLevel.assert_not_called()

    @pytest.mark.asyncio
    async def test_ptt_started_no_interface_logs_warning(self, plugin_with_volume):
        plugin = plugin_with_volume
        plugin._volume_interface = None

        event = PTTStartedEvent(source="floating_button")
        await plugin._handle_ptt_started(event)  # Should not raise

        assert plugin._pre_ptt_volume_db is None
