"""Tests for monitors.py - System resource monitoring.

Tests cover:
- monitor_resources async loop behavior
- Resource threshold logging levels
- Notification on high usage
- Cancellation handling (caught internally, returns normally)
- Error recovery in monitoring loop
"""

import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock

import pytest


class TestMonitorResources:
    """Tests for the monitor_resources async function.

    Note: monitor_resources catches CancelledError internally and returns
    normally, so tests should NOT expect CancelledError to propagate.
    """

    @pytest.fixture
    def mock_psutil(self):
        with patch("utils.monitors.psutil") as m:
            m.cpu_percent.return_value = 25.0
            mem = Mock()
            mem.percent = 40.0
            mem.used = 4 * 1024 * 1024 * 1024  # 4GB
            mem.total = 16 * 1024 * 1024 * 1024  # 16GB
            m.virtual_memory.return_value = mem
            yield m

    @pytest.fixture
    def mock_notification(self):
        with patch("utils.monitors.notification") as m:
            yield m

    def _make_fast_sleep(self, max_loops=10):
        """Create a fast_sleep that cancels after max_loops iterations."""
        loop_count = {"n": 0}

        async def fast_sleep(duration):
            loop_count["n"] += 1
            if loop_count["n"] > max_loops:
                raise asyncio.CancelledError()

        return fast_sleep, loop_count

    @pytest.mark.asyncio
    async def test_cancellation_stops_loop(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        fast_sleep, _ = self._make_fast_sleep(max_loops=5)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            # Should return normally (catches CancelledError internally)
            await monitor_resources()

    @pytest.mark.asyncio
    async def test_normal_usage_checks_resources(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        mock_psutil.cpu_percent.return_value = 10.0

        fast_sleep, _ = self._make_fast_sleep(max_loops=10)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            await monitor_resources()

        # Should have checked resources at least once
        assert mock_psutil.cpu_percent.call_count >= 1

    @pytest.mark.asyncio
    async def test_high_usage_sends_notification(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        mock_psutil.cpu_percent.return_value = 97.0
        mem = Mock()
        mem.percent = 96.0
        mem.used = 15 * 1024 * 1024 * 1024
        mem.total = 16 * 1024 * 1024 * 1024
        mock_psutil.virtual_memory.return_value = mem

        fast_sleep, _ = self._make_fast_sleep(max_loops=10)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            await monitor_resources()

        mock_notification.notify.assert_called()

    @pytest.mark.asyncio
    async def test_moderate_usage_no_notification(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        mock_psutil.cpu_percent.return_value = 60.0
        mem = Mock()
        mem.percent = 75.0
        mem.used = 12 * 1024 * 1024 * 1024
        mem.total = 16 * 1024 * 1024 * 1024
        mock_psutil.virtual_memory.return_value = mem

        fast_sleep, _ = self._make_fast_sleep(max_loops=10)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            await monitor_resources()

        mock_notification.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_in_loop_recovers(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        call_count = 0

        def failing_cpu(interval=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("psutil broken")
            return 25.0

        mock_psutil.cpu_percent.side_effect = failing_cpu

        fast_sleep, _ = self._make_fast_sleep(max_loops=25)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            await monitor_resources()

        # Should have attempted cpu_percent more than once (recovered from error)
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_notification_failure_doesnt_crash(self, mock_psutil, mock_notification):
        from utils.monitors import monitor_resources

        mock_psutil.cpu_percent.return_value = 99.0
        mem = Mock()
        mem.percent = 99.0
        mem.used = 15 * 1024 * 1024 * 1024
        mem.total = 16 * 1024 * 1024 * 1024
        mock_psutil.virtual_memory.return_value = mem

        mock_notification.notify.side_effect = RuntimeError("notification broken")

        fast_sleep, _ = self._make_fast_sleep(max_loops=10)

        with patch("utils.monitors.asyncio.sleep", side_effect=fast_sleep):
            # Should return normally despite notification errors
            await monitor_resources()
