"""Tests for SonosPlugin.

Covers: initialization, volume control, playback monitoring,
speech suppression logic, local audio detection, error handling, health status.

P3-T3 of the test coverage improvement plan.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock, PropertyMock

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.sonos_plugin import SonosPlugin
import services.wheelhouse.plugins.sonos_plugin as sonos_mod
from services.wheelhouse.events import VolumeAdjustCommand, SonosStateChangedEvent


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
def mock_volume_router_sonos():
    """VolumeRouter that selects Sonos."""
    router = Mock()
    router.use_sonos = True
    router.use_system_volume = False
    router.sonos_ip = "192.168.1.50"
    router.sonos_name = "Office"
    return router


@pytest.fixture
def mock_volume_router_no_sonos():
    """VolumeRouter without Sonos."""
    router = Mock()
    router.use_sonos = False
    router.use_system_volume = True
    router.sonos_ip = None
    router.sonos_name = None
    return router


@pytest.fixture
def mock_player():
    """Mock SoCo player."""
    player = Mock()
    player.volume = 50
    player.ip_address = "192.168.1.50"
    player.player_name = "Office"
    player.get_current_track_info = Mock(return_value={"uri": "x-sonosapi-stream:some_station"})
    player.get_current_transport_info = Mock(return_value={"current_transport_state": "PLAYING"})
    return player


def _make_config(speaker_ip=None, polling_interval=2):
    config = Mock()
    config.get = lambda key, default=None: {
        "plugins.sonos.speaker_ip": speaker_ip,
        "plugins.sonos.polling_interval": polling_interval,
    }.get(key, default)
    return config


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestSonosPluginInit:
    def test_name_is_sonos(self):
        p = SonosPlugin()
        assert p.name == "sonos"

    def test_initial_state(self):
        p = SonosPlugin()
        assert p.state == PluginState.UNINITIALIZED


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestSonosInitialize:
    @pytest.mark.asyncio
    async def test_initialize_with_auto_discovered_sonos(self, mock_event_bus, mock_volume_router_sonos):
        config = _make_config()

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert p.state == PluginState.INITIALIZED
        assert p._speaker_ip == "192.168.1.50"
        assert p._sonos_available is True

    @pytest.mark.asyncio
    async def test_initialize_with_config_fallback(self, mock_event_bus, mock_volume_router_no_sonos):
        config = _make_config(speaker_ip="192.168.1.99")

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_no_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert p._speaker_ip == "192.168.1.99"
        assert p._sonos_available is True

    @pytest.mark.asyncio
    async def test_initialize_without_sonos(self, mock_event_bus, mock_volume_router_no_sonos):
        config = _make_config()  # No speaker_ip

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_no_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert p._sonos_available is False
        assert p.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_loads_polling_interval(self, mock_event_bus, mock_volume_router_sonos):
        config = _make_config(polling_interval=5)

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert p._polling_interval == 5


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestSonosStartStop:
    @pytest.mark.asyncio
    async def test_start_subscribes_when_router_selects_sonos(self, mock_event_bus, mock_volume_router_sonos):
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._sonos_available = True
        p._speaker_ip = "192.168.1.50"
        p._polling_interval = 2
        p._state = PluginState.INITIALIZED

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            await p.start()

        mock_event_bus.subscribe.assert_called_once_with(
            VolumeAdjustCommand, p._handle_volume_adjust
        )
        assert p.state == PluginState.RUNNING
        assert p._monitor_task is not None
        await p.stop()

    @pytest.mark.asyncio
    async def test_start_no_subscribe_when_no_sonos(self, mock_event_bus):
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._sonos_available = False
        p._state = PluginState.INITIALIZED

        await p.start()
        assert p.state == PluginState.RUNNING
        mock_event_bus.subscribe.assert_not_called()
        assert p._monitor_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_monitor_task(self, mock_event_bus, mock_volume_router_sonos):
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._sonos_available = True
        p._speaker_ip = "192.168.1.50"
        p._polling_interval = 2
        p._state = PluginState.INITIALIZED

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            await p.start()

        task = p._monitor_task
        await p.stop()
        assert task.done()
        assert p.state == PluginState.STOPPED


# ---------------------------------------------------------------------------
# Volume adjustment
# ---------------------------------------------------------------------------

class TestSonosVolumeAdjust:
    """Tests for Sonos volume adjustment.

    We patch asyncio.to_thread to run synchronously since the lambdas
    just access Mock attributes (no real I/O).
    """

    @staticmethod
    async def _sync_to_thread(func, *args):
        """Run to_thread target synchronously."""
        return func(*args)

    @pytest.mark.asyncio
    async def test_volume_up(self, mock_event_bus, mock_player):
        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=mock_player):
            with patch.object(asyncio, 'to_thread', side_effect=self._sync_to_thread):
                event = VolumeAdjustCommand(delta=5)
                await p._handle_volume_adjust(event)

        assert mock_player.volume == 55

    @pytest.mark.asyncio
    async def test_volume_down(self, mock_event_bus, mock_player):
        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=mock_player):
            with patch.object(asyncio, 'to_thread', side_effect=self._sync_to_thread):
                event = VolumeAdjustCommand(delta=-10)
                await p._handle_volume_adjust(event)

        assert mock_player.volume == 40

    @pytest.mark.asyncio
    async def test_volume_clamped_at_100(self, mock_event_bus, mock_player):
        mock_player.volume = 95

        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=mock_player):
            with patch.object(asyncio, 'to_thread', side_effect=self._sync_to_thread):
                event = VolumeAdjustCommand(delta=10)
                await p._handle_volume_adjust(event)

        assert mock_player.volume == 100

    @pytest.mark.asyncio
    async def test_volume_clamped_at_0(self, mock_event_bus, mock_player):
        mock_player.volume = 3

        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=mock_player):
            with patch.object(asyncio, 'to_thread', side_effect=self._sync_to_thread):
                event = VolumeAdjustCommand(delta=-10)
                await p._handle_volume_adjust(event)

        assert mock_player.volume == 0

    @pytest.mark.asyncio
    async def test_volume_no_change_at_limit(self, mock_event_bus, mock_player):
        """Already at 100, delta +5 should log but not set."""
        mock_player.volume = 100

        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=mock_player):
            with patch.object(asyncio, 'to_thread', side_effect=self._sync_to_thread):
                event = VolumeAdjustCommand(delta=5)
                await p._handle_volume_adjust(event)

        assert mock_player.volume == 100

    @pytest.mark.asyncio
    async def test_volume_player_unavailable(self, mock_event_bus):
        p = SonosPlugin()
        p._event_bus = mock_event_bus

        with patch.object(p, '_get_player', new_callable=AsyncMock, return_value=None):
            event = VolumeAdjustCommand(delta=5)
            await p._handle_volume_adjust(event)  # Should not raise


# ---------------------------------------------------------------------------
# Player connection
# ---------------------------------------------------------------------------

class TestSonosPlayerConnection:
    def test_get_player_sync_by_ip(self):
        p = SonosPlugin()
        p._speaker_ip = "192.168.1.50"

        with patch.object(sonos_mod, "SoCo") as mock_soco:
            mock_soco.return_value = Mock()
            result = p._get_player_sync()

        mock_soco.assert_called_with("192.168.1.50")
        assert result is not None

    def test_get_player_sync_by_name(self):
        p = SonosPlugin()
        p._speaker_ip = "Office"

        with patch.object(sonos_mod.soco, "discovery") as mock_disc:
            mock_disc.by_name = Mock(return_value=Mock())
            result = p._get_player_sync()

        mock_disc.by_name.assert_called_with("Office")
        assert result is not None

    def test_get_player_sync_exception_returns_none(self):
        from soco.exceptions import SoCoException
        p = SonosPlugin()
        p._speaker_ip = "192.168.1.50"

        with patch.object(sonos_mod, "SoCo", side_effect=SoCoException("timeout")):
            result = p._get_player_sync()

        assert result is None


# ---------------------------------------------------------------------------
# Playback monitoring / speech suppression
# ---------------------------------------------------------------------------

class TestSonosMonitorPlayback:
    @pytest.mark.asyncio
    async def test_music_playing_publishes_suppression(self, mock_event_bus, mock_player):
        """Streaming music should suppress speech."""
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        # Should have published SonosStateChangedEvent(is_playing=True)
        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert len(sonos_events) >= 1
        assert sonos_events[0].is_playing is True

    @pytest.mark.asyncio
    async def test_local_audio_does_not_suppress(self, mock_event_bus, mock_player):
        """Line-in (x-rincon-stream:) should NOT suppress speech."""
        mock_player.get_current_track_info = Mock(return_value={"uri": "x-rincon-stream:RINCON_123"})
        mock_player.get_current_transport_info = Mock(return_value={"current_transport_state": "PLAYING"})

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert len(sonos_events) >= 1
        assert sonos_events[0].is_playing is False  # Not suppressed

    @pytest.mark.asyncio
    async def test_tv_audio_does_not_suppress(self, mock_event_bus, mock_player):
        """HDMI ARC (x-sonos-htastream:) should NOT suppress."""
        mock_player.get_current_track_info = Mock(return_value={"uri": "x-sonos-htastream:RINCON_123"})
        mock_player.get_current_transport_info = Mock(return_value={"current_transport_state": "PLAYING"})

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert sonos_events[0].is_playing is False

    @pytest.mark.asyncio
    async def test_stopped_playback_no_suppression(self, mock_event_bus, mock_player):
        """STOPPED transport state should not suppress."""
        mock_player.get_current_transport_info = Mock(return_value={"current_transport_state": "STOPPED"})

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert sonos_events[0].is_playing is False

    @pytest.mark.asyncio
    async def test_player_unreachable_clears_suppression(self, mock_event_bus):
        """When player not found, should publish is_playing=False."""
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"
        p._previous_suppression_state = True  # Was suppressed

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return None

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert sonos_events[0].is_playing is False

    @pytest.mark.asyncio
    async def test_no_speaker_ip_exits_immediately(self, mock_event_bus):
        """Monitor should exit early if no speaker IP configured."""
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = None
        p._state = PluginState.RUNNING

        await p._monitor_playback()
        mock_event_bus.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Fast-fail request timeout + unreachable-transition log gating (wh-1f9c)
# ---------------------------------------------------------------------------

@pytest.fixture
def restore_soco_request_timeout():
    """Save/restore the module-global SoCo request timeout around a test."""
    import soco.config as soco_config

    saved = soco_config.REQUEST_TIMEOUT
    yield soco_config
    soco_config.REQUEST_TIMEOUT = saved


class TestSonosRequestTimeout:
    @pytest.mark.asyncio
    async def test_initialize_sets_fast_fail_timeout(
        self, mock_event_bus, mock_volume_router_sonos, restore_soco_request_timeout
    ):
        """initialize must replace SoCo's 20s default with a fast-fail
        (connect, read) pair so an unreachable speaker cannot stall the
        2s poll loop for 20s per iteration."""
        soco_config = restore_soco_request_timeout
        soco_config.REQUEST_TIMEOUT = 20.0
        config = _make_config()

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert soco_config.REQUEST_TIMEOUT == (2.0, 5.0)

    @pytest.mark.asyncio
    async def test_timeout_configurable(
        self, mock_event_bus, mock_volume_router_sonos, restore_soco_request_timeout
    ):
        soco_config = restore_soco_request_timeout
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.sonos.request_connect_timeout": 1.0,
            "plugins.sonos.request_read_timeout": 3.0,
        }.get(key, default)

        with patch.object(sonos_mod, "get_volume_router", return_value=mock_volume_router_sonos):
            p = SonosPlugin()
            await p.initialize(config, mock_event_bus)

        assert soco_config.REQUEST_TIMEOUT == (1.0, 3.0)


class TestSonosUnreachableLogGating:
    """The speaker dropping must produce ONE warning, quiet debug lines while
    it stays down, and one info line on recovery -- not an ERROR every 2s
    poll (wh-1f9c)."""

    @pytest.mark.asyncio
    async def test_repeated_failures_warn_once_then_debug(
        self, mock_event_bus, caplog
    ):
        from soco.exceptions import SoCoException

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fail_player():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                p._state = PluginState.STOPPED
            raise SoCoException("connect timeout")

        with patch.object(p, '_get_player', side_effect=fail_player):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                import logging
                with caplog.at_level(logging.DEBUG, logger=sonos_mod.logger.name):
                    await p._monitor_playback()

        records = [r for r in caplog.records if "unreachable" in r.getMessage().lower()]
        warnings = [r for r in records if r.levelname == "WARNING"]
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        debugs = [r for r in records if r.levelname == "DEBUG"]
        assert len(warnings) == 1, "exactly one WARNING on the drop transition"
        assert len(errors) == 0, "no ERROR spam while the speaker is down"
        assert len(debugs) >= 1, "later failures logged quietly at DEBUG"

    @pytest.mark.asyncio
    async def test_read_timeout_uses_same_gate(self, mock_event_bus, caplog):
        """A read timeout (requests Timeout, not ConnectionError) must take
        the same quiet path, not the generic ERROR handler."""
        import requests

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def slow_player():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                p._state = PluginState.STOPPED
            raise requests.exceptions.ReadTimeout("read timed out")

        with patch.object(p, '_get_player', side_effect=slow_player):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                import logging
                with caplog.at_level(logging.DEBUG, logger=sonos_mod.logger.name):
                    await p._monitor_playback()

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "unreachable" in r.getMessage().lower()
        ]
        assert len(errors) == 0
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_recovery_logs_info(self, mock_event_bus, mock_player, caplog):
        from soco.exceptions import SoCoException

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def flaky_player():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SoCoException("connect timeout")
            if call_count >= 2:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=flaky_player):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                import logging
                with caplog.at_level(logging.DEBUG, logger=sonos_mod.logger.name):
                    await p._monitor_playback()

        infos = [
            r for r in caplog.records
            if r.levelname == "INFO" and "reachable again" in r.getMessage()
        ]
        assert len(infos) == 1

    @pytest.mark.asyncio
    async def test_player_not_found_uses_same_gate(self, mock_event_bus, caplog):
        """The 'player not found' branch shares the transition gate: one
        WARNING, then DEBUG, never an ERROR."""
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def no_player():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                p._state = PluginState.STOPPED
            return None

        with patch.object(p, '_get_player', side_effect=no_player):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                import logging
                with caplog.at_level(logging.DEBUG, logger=sonos_mod.logger.name):
                    await p._monitor_playback()

        records = [r for r in caplog.records if "unreachable" in r.getMessage().lower()]
        warnings = [r for r in records if r.levelname == "WARNING"]
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(warnings) == 1
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_startup_success_logs_no_recovery_info(
        self, mock_event_bus, mock_player, caplog
    ):
        """A normal healthy start (never unreachable) must not log the
        'reachable again' recovery line."""
        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                import logging
                with caplog.at_level(logging.DEBUG, logger=sonos_mod.logger.name):
                    await p._monitor_playback()

        infos = [
            r for r in caplog.records
            if r.levelname == "INFO" and "reachable again" in r.getMessage()
        ]
        assert len(infos) == 0


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestSonosHealth:
    def test_healthy_with_player(self):
        p = SonosPlugin()
        p._state = PluginState.RUNNING
        p._player = Mock()
        # Simulate running task
        p._monitor_task = Mock()
        p._monitor_task.done = Mock(return_value=False)

        status = p.get_health_status()
        assert status["status"] == "healthy"
        assert status["connected"] is True

    def test_unhealthy_task_died(self):
        p = SonosPlugin()
        p._state = PluginState.RUNNING
        p._player = Mock()
        p._monitor_task = Mock()
        p._monitor_task.done = Mock(return_value=True)

        status = p.get_health_status()
        assert status["status"] == "unhealthy"

    def test_degraded_with_error(self):
        p = SonosPlugin()
        p._state = PluginState.RUNNING
        p._player = None
        p._monitor_task = Mock()
        p._monitor_task.done = Mock(return_value=False)
        p._last_error = "network timeout"

        status = p.get_health_status()
        assert status["status"] == "degraded"

    def test_health_includes_speaker_ip(self):
        p = SonosPlugin()
        p._speaker_ip = "192.168.1.50"
        status = p.get_health_status()
        assert status["speaker_ip"] == "192.168.1.50"


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------

class TestSonosAdversarial:
    @pytest.mark.asyncio
    async def test_soco_exception_during_volume(self, mock_event_bus, mock_player):
        """SoCo exception during volume adjust should be caught."""
        from soco.exceptions import SoCoException

        p = SonosPlugin()
        p._event_bus = mock_event_bus

        async def fail_player():
            raise SoCoException("network error")

        with patch.object(p, '_get_player', side_effect=fail_player):
            event = VolumeAdjustCommand(delta=5)
            await p._handle_volume_adjust(event)  # Should not raise

        assert p._last_error is not None

    @pytest.mark.asyncio
    async def test_error_during_monitoring_clears_suppression(self, mock_event_bus):
        """Network error during monitoring should clear suppression state."""
        from soco.exceptions import SoCoException

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"
        p._previous_suppression_state = True  # Was suppressed

        call_count = 0

        async def fail_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
                return None
            raise SoCoException("timeout")

        with patch.object(p, '_get_player', side_effect=fail_player):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        # Should have published is_playing=False to clear suppression
        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert any(e.is_playing is False for e in sonos_events)

    @pytest.mark.asyncio
    async def test_rincon_local_source_not_suppressed(self, mock_event_bus, mock_player):
        """x-rincon: (grouped Sonos playing local) should NOT suppress."""
        mock_player.get_current_track_info = Mock(return_value={"uri": "x-rincon:RINCON_123"})
        mock_player.get_current_transport_info = Mock(return_value={"current_transport_state": "PLAYING"})

        p = SonosPlugin()
        p._event_bus = mock_event_bus
        p._speaker_ip = "192.168.1.50"

        call_count = 0

        async def fake_get_player():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                p._state = PluginState.STOPPED
            return mock_player

        with patch.object(p, '_get_player', side_effect=fake_get_player):
            with patch.object(asyncio, 'sleep', new_callable=AsyncMock):
                p._state = PluginState.RUNNING
                await p._monitor_playback()

        published = [call[0][0] for call in mock_event_bus.publish.call_args_list]
        sonos_events = [e for e in published if isinstance(e, SonosStateChangedEvent)]
        assert sonos_events[0].is_playing is False
