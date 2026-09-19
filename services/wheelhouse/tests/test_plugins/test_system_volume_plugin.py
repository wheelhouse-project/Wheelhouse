"""Tests for SystemVolumePlugin.

Covers: initialization, config validation, volume adjustment,
dB scale conversion, device connection, COM error recovery, health status.

P3-T3 of the test coverage improvement plan.
"""

import asyncio
import logging
import threading

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock, PropertyMock

from pycaw.pycaw import EDataFlow, ERole

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.events import (
    VolumeAdjustCommand, PTTStartedEvent, PTTStoppedEvent, PTTMuteStateEvent,
    SystemConfigurationErrorEvent,
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


@pytest.fixture(autouse=True)
def app_data_dir(tmp_path, monkeypatch):
    """Keep the push-to-talk volume record out of the real %APPDATA%.

    Every hold writes that record in the directory from
    utils.system.get_app_data_path (wh-ptt-mute-orphaned-on-process-loss),
    and many tests in this file start a hold.
    """
    from services.wheelhouse.utils import system
    directory = tmp_path / "app_data"
    directory.mkdir()
    monkeypatch.setattr(system, "get_app_data_path", lambda: str(directory))
    return directory


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
    async def test_named_device_warns_and_uses_the_default_device(
        self, mock_event_bus, caplog
    ):
        """A configured device name is reported and ignored.

        David chose this on 2026-08-27 (C4) over making named devices
        work: the named branch returned a pycaw AudioDevice wrapper,
        whose Activate does not exist, so the plugin ended FAILED and
        the user had no volume control at all. It now falls back to the
        default device and says so (wh-named-device-doc-removal).
        """
        config = _make_config(
            {"plugins.system_volume.device_type": "My Headphones"}
        )

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(return_value=Mock())
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            with caplog.at_level(logging.WARNING):
                await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._device_type == "default"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a named device must warn, not pass silently"
        assert "My Headphones" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_named_device_is_reported_to_the_user_not_only_the_log(
        self, mock_event_bus
    ):
        """The report reaches the user, not only the Logic process log.

        deepseek round 1 (wh-named-device-doc-removal.1.2). The warning
        above lands once, at startup, in a log file this audience does
        not open, so volume control silently drives a different device
        from the one the user configured. WheelHouse already carries a
        channel for exactly this -- a configuration value rejected during
        initialization -- in SystemConfigurationErrorEvent, which
        StateManager bridges to a Windows notice, and AudioMonitor
        publishes it from its own initialize for the same class of
        problem.
        """
        config = _make_config(
            {"plugins.system_volume.device_type": "My Headphones"}
        )

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(return_value=Mock())
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            await p.initialize(config, mock_event_bus)

        published = [
            call.args[0]
            for call in mock_event_bus.publish.call_args_list
            if call.args
        ]
        reports = [
            e for e in published
            if isinstance(e, SystemConfigurationErrorEvent)
        ]
        assert len(reports) == 1, (
            f"expected one configuration report, got {reports!r}"
        )
        report = reports[0]
        assert report.service_name, "the report must name the service"
        assert "My Headphones" in report.error_message, (
            "the report must carry the value the user configured, or they "
            f"cannot tell which setting is meant: {report.error_message!r}"
        )
        assert "default" in report.user_action, report.user_action
        assert "communications" in report.user_action, report.user_action

    @pytest.mark.asyncio
    async def test_a_supported_device_type_reports_nothing_to_the_user(
        self, mock_event_bus
    ):
        """The baseline the test above is measured against.

        A user whose device_type is one of the two supported values has
        nothing to fix, so nothing must reach their screen.
        """
        for device_type in ("default", "communications"):
            config = _make_config(
                {"plugins.system_volume.device_type": device_type}
            )
            mock_event_bus.publish.reset_mock()

            with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
                mock_au.GetSpeakers = Mock(return_value=Mock())
                from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
                p = SystemVolumePlugin()
                await p.initialize(config, mock_event_bus)

            published = [
                call.args[0]
                for call in mock_event_bus.publish.call_args_list
                if call.args
            ]
            assert not [
                e for e in published
                if isinstance(e, SystemConfigurationErrorEvent)
            ], f"device_type={device_type!r} must not report a problem"

    @pytest.mark.asyncio
    async def test_communications_device_is_left_alone(self, mock_event_bus, caplog):
        """Only a device NAME is redirected; communications is untouched."""
        config = _make_config(
            {"plugins.system_volume.device_type": "communications"}
        )

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(return_value=Mock())
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            with caplog.at_level(logging.WARNING):
                await p.initialize(config, mock_event_bus)

        assert p._device_type == "communications"
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_the_status_reports_the_device_actually_used(self, mock_event_bus):
        """Status must not echo back a name the plugin is not using."""
        config = _make_config(
            {"plugins.system_volume.device_type": "My Headphones"}
        )

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetSpeakers = Mock(return_value=Mock())
            from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
            p = SystemVolumePlugin()
            await p.initialize(config, mock_event_bus)

        assert p.get_health_status()["device_type"] == "default"

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

    def test_communications_asks_windows_for_the_communications_role(self):
        """The endpoint comes from the role, not from a name that reads like it.

        A pycaw AudioDevice from GetAllDevices carries only FriendlyName and
        EndpointVolume, so _connect_audio_device's device.Activate() raised
        AttributeError and the plugin ended in PluginState.FAILED -- for any
        user with "communications" in some device's FriendlyName. The spec on
        the wrapper below is what makes this test able to tell the two apart:
        it has no Activate, exactly like the real one
        (wh-named-device-doc-removal.1.3).

        The FriendlyName scan was also the wrong question. Windows records
        which endpoint is the communications device as a role on the endpoint,
        which is what GetDefaultAudioEndpoint(eRender, eCommunications)
        returns; a device called "Communications Headset" that Windows has NOT
        been told to use for communications is a different device.
        """
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "communications"

        # No Activate, like the real pycaw AudioDevice.
        wrapper = Mock(spec=["FriendlyName"])
        wrapper.FriendlyName = "Communications Headset"
        endpoint = Mock()
        enumerator = Mock()
        enumerator.GetDefaultAudioEndpoint = Mock(return_value=endpoint)

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetDeviceEnumerator = Mock(return_value=enumerator)
            mock_au.GetAllDevices = Mock(return_value=[wrapper])
            mock_au.GetSpeakers = Mock(return_value=Mock())
            result = p._get_audio_device()

        assert result is endpoint, (
            "the communications branch must return the raw endpoint Windows "
            f"names for the role, got {result!r}"
        )
        enumerator.GetDefaultAudioEndpoint.assert_called_once_with(
            EDataFlow.eRender.value, ERole.eCommunications.value
        )

    @pytest.mark.asyncio
    async def test_communications_device_connects_instead_of_failing(self):
        """The whole point: startup completes rather than raising.

        _get_audio_device returning the wrapper is only the mechanism; what
        the user saw was the plugin in PluginState.FAILED and no volume
        control. This drives _connect_audio_device, the caller that does the
        Activate, so the fix is pinned at the level the defect was reported
        at rather than one layer below it.
        """
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "communications"

        wrapper = Mock(spec=["FriendlyName"])
        wrapper.FriendlyName = "Communications Headset"
        endpoint = Mock()
        enumerator = Mock()
        enumerator.GetDefaultAudioEndpoint = Mock(return_value=endpoint)

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetDeviceEnumerator = Mock(return_value=enumerator)
            mock_au.GetAllDevices = Mock(return_value=[wrapper])
            mock_au.GetSpeakers = Mock(return_value=Mock())
            await p._connect_audio_device()

        assert p._volume_interface is not None
        assert p._last_error is None
        endpoint.Activate.assert_called_once()

    def test_communications_falls_back_to_the_default_when_the_role_fails(self):
        """A machine with no communications endpoint still gets volume control.

        The old branch fell back when its FriendlyName scan found nothing.
        The role lookup cannot "find nothing" -- it raises -- so the fallback
        moves to the failure path and is kept rather than dropped.
        """
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "communications"

        mock_speaker = Mock()
        enumerator = Mock()
        enumerator.GetDefaultAudioEndpoint = Mock(
            side_effect=OSError("no communications endpoint")
        )

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetDeviceEnumerator = Mock(return_value=enumerator)
            mock_au.GetSpeakers = Mock(return_value=mock_speaker)
            result = p._get_audio_device()

        assert result is mock_speaker

    def test_an_unrecognised_device_type_uses_the_default_device(self):
        """The name-matching branch is gone, so no wrapper can escape.

        It previously returned an AudioUtilities.GetAllDevices() item --
        a pycaw AudioDevice with no Activate -- and the old test
        test_get_audio_device_named_found asserted exactly that, pinning
        the defect (wh-named-device-doc-removal). initialize now
        redirects a name to "default", and this test covers the case
        where something sets the attribute directly anyway.
        """
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        p = SystemVolumePlugin()
        p._device_type = "My Headphones"

        mock_device = Mock()
        mock_device.FriendlyName = "My Headphones"
        mock_speaker = Mock()

        with patch("services.wheelhouse.plugins.system_volume_plugin.AudioUtilities") as mock_au:
            mock_au.GetAllDevices = Mock(return_value=[mock_device])
            mock_au.GetSpeakers = Mock(return_value=mock_speaker)
            result = p._get_audio_device()

        assert result == mock_speaker

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

@pytest.fixture
def synthetic_metered_endpoint(monkeypatch):
    """The existing PTT unit fixtures all represent this same multimedia device."""
    from services.wheelhouse.plugins.system_volume_plugin import AudioUtilities
    identifier = "synthetic-ptt-speakers"
    monkeypatch.setattr(AudioUtilities, "GetSpeakers", lambda: Mock(
        GetId=Mock(return_value=identifier)))
    return identifier


class TestPTTAudioMuting:
    """Test that SystemVolumePlugin mutes/restores volume during PTT."""

    @pytest.fixture
    def plugin_with_volume(self, synthetic_metered_endpoint):
        """Plugin with mocked volume interface."""
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._min_volume_db = -65.25
        plugin._state = PluginState.RUNNING
        return plugin

    @pytest.mark.asyncio
    async def test_ptt_started_saves_and_mutes(self, plugin_with_volume):
        plugin = plugin_with_volume
        plugin._volume_interface.GetMasterVolumeLevel.return_value = -10.0

        event = PTTStartedEvent(source="floating_button", hold_id=1)
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

        event = PTTStartedEvent(source="floating_button", hold_id=1)
        await plugin._handle_ptt_started(event)  # Should not raise

        assert plugin._pre_ptt_volume_db is None


class TestPTTMuteIsReported:
    """Every path out of the mute has to say whether it worked.

    StateManager lets a hold ignore audio suppression only while the speakers
    are known to be silenced. Silence from this plugin means the hold stops
    listening, so a successful mute that reports nothing costs the user the
    hold (wh-ptt-audio-override).
    """

    @pytest.fixture
    def plugin_with_bus(self, mock_event_bus, synthetic_metered_endpoint):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._volume_interface.GetMasterVolumeLevel.return_value = -10.0
        plugin._min_volume_db = -65.25
        plugin._state = PluginState.RUNNING
        plugin._event_bus = mock_event_bus
        return plugin, mock_event_bus

    @staticmethod
    def _published_mute_states(bus):
        return [
            call.args[0]
            for call in bus.publish.call_args_list
            if isinstance(call.args[0], PTTMuteStateEvent)
        ]

    @pytest.mark.asyncio
    async def test_a_successful_mute_is_reported(self, plugin_with_bus):
        plugin, bus = plugin_with_bus

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        events = self._published_mute_states(bus)
        assert len(events) == 1
        assert events[0].muted is True

    @pytest.mark.asyncio
    async def test_no_audio_device_is_reported_as_not_muted(self, plugin_with_bus):
        plugin, bus = plugin_with_bus
        plugin._volume_interface = None

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        events = self._published_mute_states(bus)
        assert len(events) == 1
        assert events[0].muted is False
        assert events[0].reason == "no_device"

    @pytest.mark.asyncio
    async def test_a_failed_mute_is_reported_as_not_muted(self, plugin_with_bus):
        plugin, bus = plugin_with_bus
        plugin._volume_interface.SetMasterVolumeLevel.side_effect = OSError("device gone")

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        events = self._published_mute_states(bus)
        assert len(events) == 1
        assert events[0].muted is False
        assert events[0].reason == "error"

    @pytest.mark.asyncio
    async def test_no_event_bus_does_not_raise(self, plugin_with_bus):
        """The plugin can run before start attaches a bus."""
        plugin, _ = plugin_with_bus
        plugin._event_bus = None

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        plugin._volume_interface.SetMasterVolumeLevel.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_report_names_the_hold_it_answers(self, plugin_with_bus):
        """StateManager ignores a report whose hold number is not the running
        hold, so a report that drops the number would never be acted on
        (wh-ptt-audio-override.1.1)."""
        plugin, bus = plugin_with_bus

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=7))

        events = self._published_mute_states(bus)
        assert len(events) == 1
        assert events[0].hold_id == 7

    @pytest.mark.asyncio
    async def test_a_no_device_report_names_the_hold_it_answers(self, plugin_with_bus):
        plugin, bus = plugin_with_bus
        plugin._volume_interface = None

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=8))

        assert self._published_mute_states(bus)[0].hold_id == 8

    @pytest.mark.asyncio
    async def test_a_failed_mute_report_names_the_hold_it_answers(self, plugin_with_bus):
        plugin, bus = plugin_with_bus
        plugin._volume_interface.SetMasterVolumeLevel.side_effect = OSError("device gone")

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=9))

        assert self._published_mute_states(bus)[0].hold_id == 9


class TestTheSpeakersAreTurnedDownAndBackUpInOrder:
    """A mute and a restore must never overlap.

    Both run two Core Audio calls in worker threads through asyncio.to_thread,
    and StateManager only schedules the events that start them, so without
    something ordering them a release can pass a press
    (wh-ptt-audio-override.1.4).
    """

    PRE_PTT_LEVEL = -10.0

    @pytest.fixture
    def plugin_with_bus(self, mock_event_bus, synthetic_metered_endpoint):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._volume_interface.GetMasterVolumeLevel.return_value = self.PRE_PTT_LEVEL
        plugin._min_volume_db = -65.25
        plugin._state = PluginState.RUNNING
        plugin._event_bus = mock_event_bus
        return plugin, mock_event_bus

    @pytest.mark.asyncio
    async def test_a_release_during_the_first_reading_still_restores_the_speakers(
        self, plugin_with_bus
    ):
        """The release used to arrive before the reading recorded the old
        level, so the restore found nothing to put back and returned. The mute
        that followed then had nothing left to undo it, and the speakers
        stayed silent until the next hold."""
        plugin, _ = plugin_with_bus
        reading_started = threading.Event()
        let_reading_finish = threading.Event()

        def slow_read():
            reading_started.set()
            assert let_reading_finish.wait(5), "the test never released the reading"
            return self.PRE_PTT_LEVEL

        plugin._volume_interface.GetMasterVolumeLevel.side_effect = slow_read

        start = asyncio.create_task(
            plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        )
        await asyncio.to_thread(reading_started.wait, 5)
        stop = asyncio.create_task(
            plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))
        )
        await asyncio.sleep(0)
        let_reading_finish.set()
        await asyncio.gather(start, stop)

        levels = [call.args[0] for call in plugin._volume_interface.SetMasterVolumeLevel.call_args_list]
        assert levels == [plugin._min_volume_db, self.PRE_PTT_LEVEL]

    @pytest.mark.asyncio
    async def test_an_old_restore_cannot_turn_the_speakers_back_up_during_a_newer_hold(
        self, plugin_with_bus
    ):
        """A slow restore for one hold used to finish after the next hold had
        already muted, so the speakers came back while the new hold was still
        listening to them."""
        plugin, _ = plugin_with_bus
        restore_started = threading.Event()
        let_restore_finish = threading.Event()
        mute_done = threading.Event()
        finished = []

        def set_level(db, _context=None):
            if db == self.PRE_PTT_LEVEL:
                restore_started.set()
                assert let_restore_finish.wait(5), "the test never released the restore"
                finished.append(db)
                return
            finished.append(db)
            mute_done.set()

        plugin._volume_interface.SetMasterVolumeLevel.side_effect = set_level

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        mute_done.clear()
        stop = asyncio.create_task(
            plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))
        )
        await asyncio.to_thread(restore_started.wait, 5)
        start = asyncio.create_task(
            plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=2))
        )
        # Give the new hold half a second to mute while the old restore is
        # still running. Nothing may get through in that time.
        assert not await asyncio.to_thread(mute_done.wait, 0.5), (
            "the new hold muted while the previous restore was still running"
        )
        let_restore_finish.set()
        await asyncio.gather(stop, start)

        assert finished == [plugin._min_volume_db, self.PRE_PTT_LEVEL, plugin._min_volume_db]


class TestAVolumeChangeCannotUndoTheHoldsMute:
    """Turning the volume up during a hold must not make the speakers audible.

    The hold ignores audio suppression only because the speakers are known to
    be silent. _handle_volume_adjust is the other writer of
    SetMasterVolumeLevel, and the mouse wheel in the right screen zone can
    fire it at any moment (wh-ptt-audio-override.1.7).
    """

    PRE_PTT_LEVEL = -10.0

    @pytest.fixture
    def plugin_with_bus(self, mock_event_bus, synthetic_metered_endpoint):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._volume_interface.GetMasterVolumeLevel.return_value = self.PRE_PTT_LEVEL
        plugin._min_volume_db = -65.25
        plugin._max_volume_db = 0.0
        plugin._volume_step_db = 3.0
        plugin._state = PluginState.RUNNING
        plugin._event_bus = mock_event_bus
        return plugin, mock_event_bus

    @pytest.mark.asyncio
    async def test_the_speakers_stay_silent_when_the_volume_goes_up_mid_hold(
        self, plugin_with_bus
    ):
        """A wheel turn used to raise the very endpoint the mute lowered,
        while the hold went on ignoring audio suppression."""
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        plugin._volume_interface.GetMasterVolumeLevel.return_value = plugin._min_volume_db

        await plugin._handle_volume_adjust(VolumeAdjustCommand(delta=2))

        levels = [c.args[0] for c in plugin._volume_interface.SetMasterVolumeLevel.call_args_list]
        assert levels == [plugin._min_volume_db], (
            "the volume change reached the speakers while the hold was listening"
        )

    @pytest.mark.asyncio
    async def test_a_volume_change_made_during_a_hold_survives_the_release(
        self, plugin_with_bus
    ):
        """The user's wheel turn must not be thrown away. The restore puts
        back the level the user asked for, not the level before the turn."""
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        plugin._volume_interface.GetMasterVolumeLevel.return_value = plugin._min_volume_db

        await plugin._handle_volume_adjust(VolumeAdjustCommand(delta=2))
        await plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))

        levels = [c.args[0] for c in plugin._volume_interface.SetMasterVolumeLevel.call_args_list]
        expected = self.PRE_PTT_LEVEL + 2 * plugin._volume_step_db
        assert levels == [plugin._min_volume_db, expected], (
            f"expected the restore to put back {expected}dB, the level the user asked for"
        )

    @pytest.mark.asyncio
    async def test_a_volume_change_outside_a_hold_still_reaches_the_speakers(
        self, plugin_with_bus
    ):
        """The control. Ordinary volume control must be untouched."""
        plugin, _ = plugin_with_bus

        await plugin._handle_volume_adjust(VolumeAdjustCommand(delta=2))

        levels = [c.args[0] for c in plugin._volume_interface.SetMasterVolumeLevel.call_args_list]
        assert levels == [self.PRE_PTT_LEVEL + 2 * plugin._volume_step_db]


class TestAFailedMuteLeavesNoTraceOfAHold:
    """A mute that raised did not turn the speakers down.

    _pre_ptt_volume_db is what the rest of the plugin reads to decide whether
    a hold has the speakers down, so a mute that failed must not leave it set
    (wh-ptt-audio-override.1.9).
    """

    PRE_PTT_LEVEL = -10.0

    @pytest.fixture
    def plugin_with_bus(self, mock_event_bus, synthetic_metered_endpoint):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._volume_interface.GetMasterVolumeLevel.return_value = self.PRE_PTT_LEVEL
        plugin._min_volume_db = -65.25
        plugin._max_volume_db = 0.0
        plugin._volume_step_db = 3.0
        plugin._state = PluginState.RUNNING
        plugin._event_bus = mock_event_bus
        return plugin, mock_event_bus

    @pytest.mark.asyncio
    async def test_a_volume_change_reaches_the_speakers_when_the_mute_failed(
        self, plugin_with_bus
    ):
        """The speakers are still loud after a failed mute, so the wheel turn
        belongs on the device now. It used to be held back until the release,
        because the failed mute left the saved level behind."""
        plugin, _ = plugin_with_bus
        levels = []

        def set_level(db, _context=None):
            levels.append(db)
            if len(levels) == 1:
                raise OSError("device gone")

        plugin._volume_interface.SetMasterVolumeLevel.side_effect = set_level

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        await plugin._handle_volume_adjust(VolumeAdjustCommand(delta=2))

        expected = self.PRE_PTT_LEVEL + 2 * plugin._volume_step_db
        assert levels[1:] == [expected], (
            "the volume change was held back for a hold whose mute never happened"
        )

    @pytest.mark.asyncio
    async def test_a_failed_mute_leaves_nothing_for_the_release_to_restore(
        self, plugin_with_bus
    ):
        """The release used to write the old level back to a device that was
        never turned down."""
        plugin, _ = plugin_with_bus
        plugin._volume_interface.SetMasterVolumeLevel.side_effect = OSError("device gone")

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        assert plugin._pre_ptt_volume_db is None, (
            "a mute that raised left the saved level behind"
        )

        plugin._volume_interface.SetMasterVolumeLevel.side_effect = None
        plugin._volume_interface.SetMasterVolumeLevel.reset_mock()
        await plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))

        plugin._volume_interface.SetMasterVolumeLevel.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_mute_that_worked_still_records_the_level_to_restore(
        self, plugin_with_bus
    ):
        """The control. A successful mute must still save the old level."""
        plugin, _ = plugin_with_bus

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        assert plugin._pre_ptt_volume_db == self.PRE_PTT_LEVEL


class TestAHoldIsKeptOnDiskUntilTheLevelIsBack:
    """The level a hold must put back has to outlive this process.

    The level to put back was kept only in memory, so a Logic process lost
    during a hold left the speakers at the lowered level with nothing able to
    put them back. The launcher now restores from a record on disk, which the
    plugin writes before the mute, rewrites on a wheel turn during the hold,
    and deletes only after the restore worked
    (wh-ptt-mute-orphaned-on-process-loss).
    """

    PRE_PTT_LEVEL = -10.0

    @pytest.fixture
    def plugin_with_bus(self, mock_event_bus, synthetic_metered_endpoint):
        from services.wheelhouse.plugins.system_volume_plugin import SystemVolumePlugin
        plugin = SystemVolumePlugin()
        plugin._audio_device_id = synthetic_metered_endpoint
        plugin._volume_interface = MagicMock()
        plugin._volume_interface.GetMasterVolumeLevel.return_value = self.PRE_PTT_LEVEL
        plugin._min_volume_db = -65.25
        plugin._max_volume_db = 0.0
        plugin._volume_step_db = 3.0
        plugin._state = PluginState.RUNNING
        plugin._event_bus = mock_event_bus
        return plugin, mock_event_bus

    @pytest.fixture
    def record_file(self, app_data_dir):
        from services.wheelhouse.utils import ptt_volume_record
        return app_data_dir / ptt_volume_record.RECORD_FILE_NAME

    @staticmethod
    def _mute_reports(bus):
        return [
            call.args[0].muted
            for call in bus.publish.call_args_list
            if isinstance(call.args[0], PTTMuteStateEvent)
        ]

    @pytest.mark.asyncio
    async def test_the_record_is_on_disk_before_the_speakers_are_turned_down(
        self, plugin_with_bus, synthetic_metered_endpoint
    ):
        """A process lost between the device write and the record write would
        leave the speakers down with no record, so the record comes first."""
        from services.wheelhouse.utils import ptt_volume_record
        plugin, _ = plugin_with_bus
        seen_at_device_write = []

        def set_level(db, _context=None):
            seen_at_device_write.append((db, ptt_volume_record.read_record()))

        plugin._volume_interface.SetMasterVolumeLevel.side_effect = set_level

        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        assert len(seen_at_device_write) == 1
        level, record = seen_at_device_write[0]
        assert level == plugin._min_volume_db
        assert record is not None, "the speakers were turned down before the record was on disk"
        assert (record.endpoint_id, record.pre_hold_db, record.lowered_db) == (
            synthetic_metered_endpoint, self.PRE_PTT_LEVEL, plugin._min_volume_db
        )

    @pytest.mark.asyncio
    async def test_a_wheel_turn_during_the_hold_updates_the_record(self, plugin_with_bus):
        """The release puts back the level the wheel turn asked for, so a
        restore after a lost process must put back the same level."""
        from services.wheelhouse.utils import ptt_volume_record
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        plugin._volume_interface.GetMasterVolumeLevel.return_value = plugin._min_volume_db

        await plugin._handle_volume_adjust(VolumeAdjustCommand(delta=2))

        record = ptt_volume_record.read_record()
        assert record is not None
        assert record.pre_hold_db == self.PRE_PTT_LEVEL + 2 * plugin._volume_step_db, (
            "the record still holds the level from before the wheel turn"
        )
        assert record.lowered_db == plugin._min_volume_db

    @pytest.mark.asyncio
    async def test_the_record_is_deleted_after_the_level_is_back_and_not_before(
        self, plugin_with_bus, record_file
    ):
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        on_disk_at_restore = []

        def set_level(db, _context=None):
            on_disk_at_restore.append(record_file.exists())

        plugin._volume_interface.SetMasterVolumeLevel.side_effect = set_level

        await plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))

        assert on_disk_at_restore == [True], "the record was gone before the level was put back"
        assert not record_file.exists(), "the record stayed on disk after the level was put back"

    @pytest.mark.asyncio
    async def test_a_restore_that_raises_keeps_the_record(self, plugin_with_bus, record_file):
        """The speakers may still be down, so the launcher's restore still needs the record."""
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        plugin._volume_interface.SetMasterVolumeLevel.side_effect = OSError("device gone")

        await plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))

        assert record_file.exists(), "the record was deleted although the level was not put back"
        assert plugin._pre_ptt_volume_db is None

    @pytest.mark.asyncio
    async def test_a_release_with_no_audio_device_keeps_the_record(
        self, plugin_with_bus, record_file
    ):
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        plugin._volume_interface = None

        await plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released"))

        assert record_file.exists(), "the record was deleted although the level was not put back"
        assert plugin._pre_ptt_volume_db is None

    @pytest.mark.asyncio
    async def test_a_record_that_cannot_be_written_is_logged_and_the_mute_goes_on(
        self, plugin_with_bus, tmp_path, monkeypatch, caplog
    ):
        from services.wheelhouse.utils import system
        plugin, bus = plugin_with_bus
        not_a_directory = tmp_path / "not_a_directory"
        not_a_directory.write_text("")
        monkeypatch.setattr(system, "get_app_data_path", lambda: str(not_a_directory))

        with caplog.at_level(logging.WARNING):
            await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        assert "push-to-talk volume record was not written" in caplog.text
        plugin._volume_interface.SetMasterVolumeLevel.assert_called_once_with(plugin._min_volume_db, None)
        assert plugin._pre_ptt_volume_db == self.PRE_PTT_LEVEL
        assert self._mute_reports(bus) == [True]

    @pytest.mark.asyncio
    async def test_a_record_write_that_raises_is_logged_and_the_mute_goes_on(
        self, plugin_with_bus, monkeypatch, caplog
    ):
        from services.wheelhouse.utils import ptt_volume_record
        plugin, bus = plugin_with_bus

        def broken_write(*args, **kwargs):
            raise RuntimeError("the record store is broken")

        monkeypatch.setattr(ptt_volume_record, "write_record", broken_write)

        with caplog.at_level(logging.WARNING):
            await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))

        assert "the record store is broken" in caplog.text
        plugin._volume_interface.SetMasterVolumeLevel.assert_called_once_with(plugin._min_volume_db, None)
        assert plugin._pre_ptt_volume_db == self.PRE_PTT_LEVEL
        assert self._mute_reports(bus) == [True]

    @pytest.mark.asyncio
    async def test_a_release_whose_record_delete_is_slow_cannot_delete_the_record_of_the_next_hold(
        self, plugin_with_bus, monkeypatch
    ):
        """The release deletes the record with _ptt_audio_lock held. The next
        press writes its own record with the same lock held, so a slow delete
        cannot remove the record of a hold that started after the release. A
        delete after the lock is released could: the Logic process could then
        be lost during that hold with no record on disk, and the speakers
        would stay at the lowered level (wh-ptt-mute-orphaned-on-process-loss).

        The delete is held in its worker thread until the next press has
        either waited for the lock or written its record. Both events set the
        same flag, so the test does not hang whichever one happens."""
        from services.wheelhouse.utils import ptt_volume_record
        plugin, _ = plugin_with_bus
        await plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=1))
        next_press_waited_or_wrote = threading.Event()
        delete_started = threading.Event()
        let_delete_finish = threading.Event()

        class WatchedLock(asyncio.Lock):
            """An asyncio.Lock that records a caller that must wait for it."""

            async def acquire(self):
                if self.locked():
                    next_press_waited_or_wrote.set()
                return await super().acquire()

        plugin._ptt_audio_lock = WatchedLock()
        real_delete = ptt_volume_record.delete_record
        real_write = ptt_volume_record.write_record

        def slow_delete(**kwargs):
            delete_started.set()
            assert let_delete_finish.wait(5), "the test never released the delete"
            return real_delete(**kwargs)

        def watched_write(*args, **kwargs):
            written = real_write(*args, **kwargs)
            next_press_waited_or_wrote.set()
            return written

        monkeypatch.setattr(ptt_volume_record, "delete_record", slow_delete)
        stop = asyncio.create_task(plugin._handle_ptt_stopped(PTTStoppedEvent(reason="released")))
        assert await asyncio.to_thread(delete_started.wait, 5), "the release never deleted the record"
        monkeypatch.setattr(ptt_volume_record, "write_record", watched_write)
        plugin._volume_interface.GetMasterVolumeLevel.return_value = -20.0
        start = asyncio.create_task(
            plugin._handle_ptt_started(PTTStartedEvent(source="floating_button", hold_id=2))
        )
        try:
            assert await asyncio.to_thread(next_press_waited_or_wrote.wait, 5), (
                "the next press neither waited for the lock nor wrote its record"
            )
        finally:
            let_delete_finish.set()
            await asyncio.wait_for(asyncio.gather(stop, start), timeout=5)

        record = ptt_volume_record.read_record()
        assert record is not None, "the release deleted the record of the next hold"
        assert record.pre_hold_db == -20.0
