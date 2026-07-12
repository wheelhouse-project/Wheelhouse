"""Tests for AudioMonitor handler.

Tests audio monitoring including:
- Spatial sound config validation (fail-fast)
- Event publishing on audio state change
- Monitoring loop behavior
- start_atmos guards
- Adversarial: missing config, config errors, rapid state changes
"""

import asyncio
import os
from unittest.mock import Mock, AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pycaw():
    """Mock pycaw audio utilities."""
    with patch("handlers.audio_monitor.AudioUtilities") as mock_au, \
         patch("handlers.audio_monitor.IAudioMeterInformation") as mock_ami, \
         patch("handlers.audio_monitor.CLSCTX_ALL"), \
         patch("handlers.audio_monitor.pythoncom"):
        yield mock_au, mock_ami


@pytest.fixture
def audio_monitor_factory(mock_config, mock_event_bus, mock_pycaw):
    """Factory for creating AudioMonitor with various configs."""
    def _create(config_overrides=None):
        loop = asyncio.new_event_loop()

        # Set up default config
        mock_config._config["plugins"] = {"bravia": {"device_name": "Sony TV"}}
        mock_config._config["SPATIAL_SOUND_EXEC"] = ""
        mock_config._config["SPATIAL_SOUND_FORMAT"] = ""

        if config_overrides:
            for key, val in config_overrides.items():
                mock_config.set(key, val)

        from handlers.audio_monitor import AudioMonitor
        monitor = AudioMonitor(loop, mock_config, mock_event_bus)
        return monitor, loop

    yield _create


@pytest.fixture
def audio_monitor(audio_monitor_factory):
    """Default AudioMonitor with spatial sound disabled."""
    monitor, loop = audio_monitor_factory()
    yield monitor
    loop.close()


# ===========================================================================
# Spatial sound configuration validation
# ===========================================================================

class TestSpatialSoundConfig:
    """Test _validate_and_configure_spatial_sound."""

    def test_disabled_when_exec_empty(self, audio_monitor):
        """Spatial sound disabled when SPATIAL_SOUND_EXEC is empty."""
        assert audio_monitor.spatial_sound_enabled is False

    def test_disabled_when_format_empty(self, audio_monitor_factory):
        """Spatial sound disabled when SPATIAL_SOUND_FORMAT is empty."""
        monitor, loop = audio_monitor_factory({
            "SPATIAL_SOUND_EXEC": "C:\\some\\exec.exe",
            "SPATIAL_SOUND_FORMAT": "",
        })
        assert monitor.spatial_sound_enabled is False
        loop.close()

    def test_raises_when_bravia_device_name_missing(self, mock_config, mock_event_bus, mock_pycaw):
        """Raises ValueError when plugins.bravia.device_name not configured."""
        loop = asyncio.new_event_loop()
        mock_config._config.clear()

        from handlers.audio_monitor import AudioMonitor
        with pytest.raises(ValueError, match="plugins.bravia.device_name"):
            AudioMonitor(loop, mock_config, mock_event_bus)
        loop.close()

    def test_enabled_when_all_config_present_and_exec_exists(
        self, mock_config, mock_event_bus, mock_pycaw
    ):
        """Spatial sound enabled when all config present and executable exists."""
        loop = asyncio.new_event_loop()
        mock_config._config["plugins"] = {"bravia": {"device_name": "Sony TV"}}
        mock_config._config["SPATIAL_SOUND_EXEC"] = "C:\\valid\\exec.exe"
        mock_config._config["SPATIAL_SOUND_FORMAT"] = "DolbyAtmos"

        with patch("os.path.exists", return_value=True):
            from handlers.audio_monitor import AudioMonitor
            monitor = AudioMonitor(loop, mock_config, mock_event_bus)

        assert monitor.spatial_sound_enabled is True
        assert monitor.spatial_sound_exec == "C:\\valid\\exec.exe"
        assert monitor.spatial_sound_format == "DolbyAtmos"
        assert monitor.sony_tv_name == "Sony TV"
        loop.close()

    @pytest.mark.asyncio
    async def test_disabled_when_exec_not_found(
        self, mock_config, mock_event_bus, mock_pycaw
    ):
        """Spatial sound disabled when executable doesn't exist on disk."""
        loop = asyncio.get_event_loop()
        mock_config._config["plugins"] = {"bravia": {"device_name": "Sony TV"}}
        mock_config._config["SPATIAL_SOUND_EXEC"] = "C:\\nonexistent\\exec.exe"
        mock_config._config["SPATIAL_SOUND_FORMAT"] = "DolbyAtmos"

        with patch("os.path.exists", return_value=False):
            from handlers.audio_monitor import AudioMonitor
            monitor = AudioMonitor(loop, mock_config, mock_event_bus)

        # Let the create_task schedule run
        await asyncio.sleep(0)

        assert monitor.spatial_sound_enabled is False
        # Should have published an error event
        mock_event_bus.publish.assert_called()


# ===========================================================================
# is_audio_playing
# ===========================================================================

class TestIsAudioPlaying:
    """Test audio level detection."""

    def test_returns_true_when_peak_above_threshold(self, audio_monitor, mock_pycaw):
        """Detects audio when peak > 0.05."""
        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.5

        assert audio_monitor.is_audio_playing() is True

    def test_returns_false_when_peak_below_threshold(self, audio_monitor, mock_pycaw):
        """No audio when peak <= 0.05."""
        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.01

        assert audio_monitor.is_audio_playing() is False

    def test_returns_false_on_exception(self, audio_monitor, mock_pycaw):
        """COM errors return False (safe default)."""
        mock_au, _ = mock_pycaw
        mock_au.GetSpeakers.side_effect = Exception("COM error")
        assert audio_monitor.is_audio_playing() is False


# ===========================================================================
# start_atmos
# ===========================================================================

class TestStartAtmos:
    """Test spatial sound activation guards."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, audio_monitor):
        """start_atmos silently returns when spatial sound disabled."""
        audio_monitor.spatial_sound_enabled = False
        # Should not raise
        await audio_monitor.start_atmos()

    @pytest.mark.asyncio
    async def test_skips_when_no_tv_name(self, audio_monitor):
        """start_atmos returns when sony_tv_name is None."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.sony_tv_name = None
        await audio_monitor.start_atmos()

    @pytest.mark.asyncio
    async def test_skips_when_no_format(self, audio_monitor):
        """start_atmos returns when spatial_sound_format is None."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.sony_tv_name = "Sony TV"
        audio_monitor.spatial_sound_format = None
        await audio_monitor.start_atmos()

    @pytest.mark.asyncio
    async def test_calls_set_spatial_sound_when_enabled(self, audio_monitor):
        """start_atmos delegates to set_spatial_sound when fully configured."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.sony_tv_name = "Sony TV"
        audio_monitor.spatial_sound_format = "DolbyAtmos"

        with patch.object(audio_monitor, "set_spatial_sound", new_callable=AsyncMock) as mock_set:
            await audio_monitor.start_atmos()
            mock_set.assert_called_once_with("Sony TV", "DolbyAtmos")


# ===========================================================================
# set_spatial_sound
# ===========================================================================

class TestSetSpatialSound:
    """Test spatial sound subprocess execution."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, audio_monitor):
        """Silent skip when spatial sound disabled."""
        audio_monitor.spatial_sound_enabled = False
        await audio_monitor.set_spatial_sound("Sony TV", "DolbyAtmos")

    @pytest.mark.asyncio
    async def test_runs_external_command(self, audio_monitor):
        """Executes configured executable with correct arguments."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.spatial_sound_exec = "C:\\tools\\spatial.exe"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"OK", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await audio_monitor.set_spatial_sound("Sony TV", "DolbyAtmos")

            mock_exec.assert_called_once()
            call_args = mock_exec.call_args[0]
            assert call_args[0] == "C:\\tools\\spatial.exe"
            assert "/SetSpatial" in call_args
            assert "Sony TV" in call_args
            assert "DolbyAtmos" in call_args

    @pytest.mark.asyncio
    async def test_handles_subprocess_failure(self, audio_monitor):
        """Non-zero return code logged but doesn't raise."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.spatial_sound_exec = "C:\\tools\\spatial.exe"

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"Error: device not found")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await audio_monitor.set_spatial_sound("Sony TV", "DolbyAtmos")
            # Should not raise

    @pytest.mark.asyncio
    async def test_handles_subprocess_exception(self, audio_monitor):
        """Subprocess launch failure doesn't crash."""
        audio_monitor.spatial_sound_enabled = True
        audio_monitor.spatial_sound_exec = "C:\\tools\\spatial.exe"

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("not found")):
            await audio_monitor.set_spatial_sound("Sony TV", "DolbyAtmos")
            # Should not raise


# ===========================================================================
# start()
# ===========================================================================

class TestStart:
    """Test start method creates monitoring task."""

    def test_start_creates_task(self, audio_monitor):
        """start() returns a task from create_task."""
        mock_task = Mock()
        audio_monitor.loop.create_task = Mock(return_value=mock_task)

        result = audio_monitor.start()
        assert result is mock_task
        audio_monitor.loop.create_task.assert_called_once()


# ===========================================================================
# monitor_audio loop
# ===========================================================================

class TestMonitorAudioLoop:
    """Test the main monitoring loop behavior."""

    @pytest.mark.asyncio
    async def test_first_run_sets_initial_state(self, audio_monitor, mock_pycaw):
        """First poll sets initial state without publishing event."""
        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.5

        # Run one iteration then cancel
        call_count = 0
        original_sleep = asyncio.sleep

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()
            await original_sleep(0)

        with patch("handlers.audio_monitor.pythoncom"):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await audio_monitor.monitor_audio()

        assert audio_monitor._first_run is False
        assert audio_monitor._previous_audio_state is True
        # No event published on first run
        audio_monitor.event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_publishes_event_on_state_change(self, audio_monitor, mock_pycaw):
        """Publishes AudioStateChangedEvent when audio state transitions."""
        # Pre-set state past first run
        audio_monitor._first_run = False
        audio_monitor._previous_audio_state = False

        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.5  # Audio playing

        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()

        with patch("handlers.audio_monitor.pythoncom"):
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await audio_monitor.monitor_audio()

        # Should have published AudioStateChangedEvent
        audio_monitor.event_bus.publish.assert_called()
        event = audio_monitor.event_bus.publish.call_args[0][0]
        assert type(event).__name__ == "AudioStateChangedEvent"
        assert event.is_playing is True


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:
    """Adversarial edge case tests."""

    def test_threshold_boundary_exactly_005(self, audio_monitor, mock_pycaw):
        """Peak value exactly at threshold (0.05) is NOT playing."""
        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.05

        assert audio_monitor.is_audio_playing() is False

    def test_threshold_boundary_just_above(self, audio_monitor, mock_pycaw):
        """Peak value just above threshold IS playing."""
        mock_au, mock_ami = mock_pycaw
        mock_device = MagicMock()
        mock_au.GetSpeakers.return_value = mock_device

        mock_interface = MagicMock()
        mock_device.Activate.return_value = mock_interface

        mock_meter = MagicMock()
        mock_interface.QueryInterface.return_value = mock_meter
        mock_meter.GetPeakValue.return_value = 0.051

        assert audio_monitor.is_audio_playing() is True

    def test_config_with_whitespace_only_exec(self, mock_config, mock_event_bus, mock_pycaw):
        """Whitespace-only SPATIAL_SOUND_EXEC treated as empty."""
        loop = asyncio.new_event_loop()
        mock_config._config["plugins"] = {"bravia": {"device_name": "Sony TV"}}
        mock_config._config["SPATIAL_SOUND_EXEC"] = "   "
        mock_config._config["SPATIAL_SOUND_FORMAT"] = "DolbyAtmos"

        from handlers.audio_monitor import AudioMonitor
        monitor = AudioMonitor(loop, mock_config, mock_event_bus)
        assert monitor.spatial_sound_enabled is False
        loop.close()

    def test_config_with_whitespace_only_tv_name(self, mock_config, mock_event_bus, mock_pycaw):
        """Whitespace-only TV name treated as empty, disabling spatial sound."""
        loop = asyncio.new_event_loop()
        mock_config._config["plugins"] = {"bravia": {"device_name": "   "}}
        mock_config._config["SPATIAL_SOUND_EXEC"] = "C:\\exec.exe"
        mock_config._config["SPATIAL_SOUND_FORMAT"] = "DolbyAtmos"

        from handlers.audio_monitor import AudioMonitor
        # Whitespace-only name should still disable spatial sound via the empty check
        monitor = AudioMonitor(loop, mock_config, mock_event_bus)
        assert monitor.spatial_sound_enabled is False
        loop.close()
