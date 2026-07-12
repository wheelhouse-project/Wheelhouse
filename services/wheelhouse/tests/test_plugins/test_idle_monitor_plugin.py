"""Tests for IdleMonitorPlugin.

Covers: initialization, configuration validation, monitoring loop,
state change detection, error handling, and health status reporting.

P3-T3 of the test coverage improvement plan.
"""

import asyncio
import ctypes
import logging

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock, PropertyMock

from services.wheelhouse.plugins.base import PluginState
from services.wheelhouse.plugins.idle_monitor_plugin import IdleMonitorPlugin
from services.wheelhouse.events import SystemIdleStateChangedEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin():
    """Fresh IdleMonitorPlugin instance."""
    return IdleMonitorPlugin()


@pytest.fixture
def mock_config():
    """Mock ConfigService with idle monitor config."""
    config = Mock()
    config_data = {
        "plugins.idle_monitor.idle_timeout_minutes": 5,
        "plugins.idle_monitor.polling_interval_seconds": 10,
    }
    config.get = lambda key, default=None: config_data.get(key, default)
    return config


@pytest.fixture
def mock_event_bus():
    """Mock EventBus."""
    bus = Mock()
    bus.publish = AsyncMock()
    bus.subscribe = Mock()
    return bus


# ---------------------------------------------------------------------------
# Constructor / name
# ---------------------------------------------------------------------------

class TestIdleMonitorPluginInit:
    def test_initial_state_is_uninitialized(self, plugin):
        assert plugin.state == PluginState.UNINITIALIZED

    def test_name_is_idle_monitor(self, plugin):
        assert plugin.name == "idle_monitor"

    def test_initial_idle_state_is_false(self, plugin):
        assert plugin._is_idle is False

    def test_initial_consecutive_errors_is_zero(self, plugin):
        assert plugin._consecutive_errors == 0

    def test_default_timeout_is_five_minutes(self, plugin):
        assert plugin.idle_timeout_minutes == 5

    def test_default_polling_interval_is_ten_seconds(self, plugin):
        assert plugin.polling_interval_seconds == 10


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestIdleMonitorInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sets_state_to_initialized(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)
        assert plugin.state == PluginState.INITIALIZED

    @pytest.mark.asyncio
    async def test_initialize_loads_config_values(self, plugin, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.idle_monitor.idle_timeout_minutes": 10,
            "plugins.idle_monitor.polling_interval_seconds": 30,
        }.get(key, default)

        await plugin.initialize(config, mock_event_bus)
        assert plugin.idle_timeout_minutes == 10
        assert plugin.polling_interval_seconds == 30

    @pytest.mark.asyncio
    async def test_initialize_uses_defaults_when_config_missing(self, plugin, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: default

        await plugin.initialize(config, mock_event_bus)
        assert plugin.idle_timeout_minutes == 5
        assert plugin.polling_interval_seconds == 10

    @pytest.mark.asyncio
    async def test_initialize_rejects_zero_timeout(self, plugin, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.idle_monitor.idle_timeout_minutes": 0,
            "plugins.idle_monitor.polling_interval_seconds": 10,
        }.get(key, default)

        with pytest.raises(ValueError, match="idle_timeout_minutes must be positive"):
            await plugin.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_negative_timeout(self, plugin, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.idle_monitor.idle_timeout_minutes": -1,
            "plugins.idle_monitor.polling_interval_seconds": 10,
        }.get(key, default)

        with pytest.raises(ValueError, match="idle_timeout_minutes must be positive"):
            await plugin.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_rejects_zero_polling_interval(self, plugin, mock_event_bus):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.idle_monitor.idle_timeout_minutes": 5,
            "plugins.idle_monitor.polling_interval_seconds": 0,
        }.get(key, default)

        with pytest.raises(ValueError, match="polling_interval_seconds must be positive"):
            await plugin.initialize(config, mock_event_bus)

    @pytest.mark.asyncio
    async def test_initialize_warns_on_aggressive_polling(self, plugin, mock_config, mock_event_bus, caplog):
        config = Mock()
        config.get = lambda key, default=None: {
            "plugins.idle_monitor.idle_timeout_minutes": 5,
            "plugins.idle_monitor.polling_interval_seconds": 2,
        }.get(key, default)

        with caplog.at_level(logging.WARNING):
            await plugin.initialize(config, mock_event_bus)

        assert any("aggressive" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_initialize_stores_event_bus_reference(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)
        assert plugin._event_bus is mock_event_bus


# ---------------------------------------------------------------------------
# Start / Stop lifecycle
# ---------------------------------------------------------------------------

class TestIdleMonitorStartStop:
    @pytest.mark.asyncio
    async def test_start_sets_state_to_running(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)

        with patch.object(plugin, '_get_idle_duration_seconds', return_value=0.0):
            await plugin.start()

        assert plugin.state == PluginState.RUNNING
        # Cleanup
        await plugin.stop()

    @pytest.mark.asyncio
    async def test_start_creates_monitor_task(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)

        with patch.object(plugin, '_get_idle_duration_seconds', return_value=0.0):
            await plugin.start()

        assert plugin._monitor_task is not None
        assert not plugin._monitor_task.done()
        await plugin.stop()

    @pytest.mark.asyncio
    async def test_start_fails_when_api_unavailable(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=RuntimeError("API fail")):
            await plugin.start()

        assert plugin.state == PluginState.FAILED
        assert plugin._monitor_task is None

    @pytest.mark.asyncio
    async def test_stop_sets_state_to_stopped(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)

        with patch.object(plugin, '_get_idle_duration_seconds', return_value=0.0):
            await plugin.start()
        await plugin.stop()

        assert plugin.state == PluginState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_cancels_monitor_task(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)

        with patch.object(plugin, '_get_idle_duration_seconds', return_value=0.0):
            await plugin.start()

        task = plugin._monitor_task
        await plugin.stop()
        assert task.done()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self, plugin, mock_config, mock_event_bus):
        await plugin.initialize(mock_config, mock_event_bus)
        await plugin.stop()
        assert plugin.state == PluginState.STOPPED


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------

class TestIdleMonitorLoop:
    @pytest.mark.asyncio
    async def test_publishes_event_on_idle_transition(self, plugin, mock_config, mock_event_bus):
        """When idle duration crosses threshold, should publish idle event."""
        await plugin.initialize(mock_config, mock_event_bus)

        # Threshold = 5 min = 300s; return 400s (idle)
        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 400.0  # Above threshold
            raise asyncio.CancelledError()  # Stop loop

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await plugin.initialize(mock_config, mock_event_bus)
                plugin._state = PluginState.RUNNING
                try:
                    await plugin._monitor_loop()
                except asyncio.CancelledError:
                    pass

        mock_event_bus.publish.assert_called_once()
        event = mock_event_bus.publish.call_args[0][0]
        assert isinstance(event, SystemIdleStateChangedEvent)
        assert event.is_idle is True
        assert event.idle_duration_seconds == 400.0

    @pytest.mark.asyncio
    async def test_publishes_event_on_active_transition(self, plugin, mock_config, mock_event_bus):
        """When system becomes active after being idle, should publish active event."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 400.0  # Idle
            if call_count == 2:
                return 5.0  # Active
            raise asyncio.CancelledError()

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                try:
                    await plugin._monitor_loop()
                except asyncio.CancelledError:
                    pass

        assert mock_event_bus.publish.call_count == 2
        # First call: idle
        event1 = mock_event_bus.publish.call_args_list[0][0][0]
        assert event1.is_idle is True
        # Second call: active
        event2 = mock_event_bus.publish.call_args_list[1][0][0]
        assert event2.is_idle is False

    @pytest.mark.asyncio
    async def test_no_event_when_state_unchanged(self, plugin, mock_config, mock_event_bus):
        """Consecutive active readings should NOT publish events."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return 5.0  # Active (below 300s threshold)
            raise asyncio.CancelledError()

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                try:
                    await plugin._monitor_loop()
                except asyncio.CancelledError:
                    pass

        # No events because state never changed (started active, stayed active)
        mock_event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_increments_consecutive_counter(self, plugin, mock_config, mock_event_bus):
        """API errors should increment consecutive error counter."""
        await plugin.initialize(mock_config, mock_event_bus)

        def fake_idle():
            raise RuntimeError("API fail")

        async def stop_after_sleep(_):
            plugin._state = PluginState.STOPPED

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', side_effect=stop_after_sleep):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        assert plugin._consecutive_errors == 1

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_errors(self, plugin, mock_config, mock_event_bus):
        """Successful poll should reset consecutive error counter."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("API fail")
            if call_count == 2:
                return 5.0  # Success
            plugin._state = PluginState.STOPPED
            return 5.0

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        assert plugin._consecutive_errors == 0

    @pytest.mark.asyncio
    async def test_three_consecutive_errors_fails_plugin(self, plugin, mock_config, mock_event_bus):
        """Three consecutive errors should set plugin to FAILED."""
        await plugin.initialize(mock_config, mock_event_bus)

        def fake_idle():
            raise RuntimeError("API fail")

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        assert plugin._state == PluginState.FAILED
        assert plugin._consecutive_errors == 3

    @pytest.mark.asyncio
    async def test_loop_exits_when_state_changes(self, plugin, mock_config, mock_event_bus):
        """Loop should exit when plugin state is no longer RUNNING."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                plugin._state = PluginState.STOPPING
            return 5.0

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        # Should have iterated only once (checked state, exited on second check)
        assert call_count <= 2


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------

class TestIdleMonitorHealth:
    def test_healthy_when_running(self, plugin):
        plugin._state = PluginState.RUNNING
        status = plugin.get_health_status()
        assert status["status"] == "healthy"
        assert status["state"] == "running"

    def test_degraded_when_running_with_errors(self, plugin):
        plugin._state = PluginState.RUNNING
        plugin._consecutive_errors = 1
        status = plugin.get_health_status()
        assert status["status"] == "degraded"

    def test_unhealthy_when_not_running(self, plugin):
        plugin._state = PluginState.FAILED
        status = plugin.get_health_status()
        assert status["status"] == "unhealthy"

    def test_health_includes_idle_state(self, plugin):
        plugin._is_idle = True
        status = plugin.get_health_status()
        assert status["is_idle"] is True

    def test_health_includes_config_values(self, plugin):
        plugin.idle_timeout_minutes = 10
        plugin.polling_interval_seconds = 30
        status = plugin.get_health_status()
        assert status["idle_timeout_minutes"] == 10
        assert status["polling_interval_seconds"] == 30

    def test_health_includes_error_count(self, plugin):
        plugin._consecutive_errors = 2
        status = plugin.get_health_status()
        assert status["consecutive_errors"] == 2

    def test_health_includes_last_state_change(self, plugin):
        plugin._last_state_change_time = 12345.0
        status = plugin.get_health_status()
        assert status["last_state_change"] == 12345.0


# ---------------------------------------------------------------------------
# Adversarial: boundary conditions and edge cases
# ---------------------------------------------------------------------------

class TestIdleMonitorAdversarial:
    @pytest.mark.asyncio
    async def test_idle_duration_exactly_at_threshold(self, plugin, mock_config, mock_event_bus):
        """Duration exactly equal to threshold should trigger idle."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 300.0  # Exactly at threshold (5 min = 300s)
            plugin._state = PluginState.STOPPED
            return 300.0

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        mock_event_bus.publish.assert_called_once()
        event = mock_event_bus.publish.call_args[0][0]
        assert event.is_idle is True

    @pytest.mark.asyncio
    async def test_idle_duration_just_below_threshold(self, plugin, mock_config, mock_event_bus):
        """Duration just below threshold should not trigger idle."""
        await plugin.initialize(mock_config, mock_event_bus)

        call_count = 0

        def fake_idle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 299.99  # Just below threshold
            plugin._state = PluginState.STOPPED
            return 299.99

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        mock_event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_rapid_idle_active_transitions(self, plugin, mock_config, mock_event_bus):
        """Rapid toggling between idle and active should publish each transition."""
        await plugin.initialize(mock_config, mock_event_bus)

        durations = [400, 5, 400, 5, 400]  # idle, active, idle, active, idle
        idx = 0

        def fake_idle():
            nonlocal idx
            if idx < len(durations):
                val = durations[idx]
                idx += 1
                return float(val)
            plugin._state = PluginState.STOPPED
            return 400.0  # Same as last state (idle) to avoid extra transition

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        # Transitions: active->idle, idle->active, active->idle, idle->active, active->idle = 5 events
        assert mock_event_bus.publish.call_count == 5

    @pytest.mark.asyncio
    async def test_error_between_successful_polls_resets_counter(self, plugin, mock_config, mock_event_bus):
        """Error counter should reset after each success, not accumulate across non-consecutive errors."""
        await plugin.initialize(mock_config, mock_event_bus)

        # Pattern: error, success, error, success, error (never 3 consecutive)
        calls = [
            RuntimeError("fail"),
            5.0,
            RuntimeError("fail"),
            5.0,
            RuntimeError("fail"),
        ]
        idx = 0

        def fake_idle():
            nonlocal idx
            if idx >= len(calls):
                plugin._state = PluginState.STOPPED
                return 5.0
            val = calls[idx]
            idx += 1
            if isinstance(val, Exception):
                raise val
            return val

        with patch.object(plugin, '_get_idle_duration_seconds', side_effect=fake_idle):
            with patch('asyncio.sleep', new_callable=AsyncMock):
                plugin._state = PluginState.RUNNING
                await plugin._monitor_loop()

        # Should NOT have failed - consecutive errors never reached 3
        assert plugin._state != PluginState.FAILED
