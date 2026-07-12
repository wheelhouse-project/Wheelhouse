"""Tests for LogicController shutdown ordering.

Verifies that shutdown_services() is always called during shutdown,
even when request_shutdown() runs first (the signal-handler path).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestShutdownOrdering:
    """Verify shutdown_services is called regardless of how shutdown is triggered."""

    @pytest.fixture
    def controller(self):
        """Create a minimal LogicController with mocked dependencies."""
        from main import LogicController

        ctrl = LogicController.__new__(LogicController)
        ctrl.shutdown_requested = False
        ctrl._shutdown_complete = False
        ctrl.shutdown_event = MagicMock()
        ctrl.shutdown_event.is_set = MagicMock(return_value=False)
        ctrl.shutdown_event.set = MagicMock()
        ctrl.loop = asyncio.new_event_loop()
        ctrl.background_tasks = []

        # Mock the app
        ctrl.app = MagicMock()
        ctrl.app.stop = AsyncMock()
        ctrl.app.shutdown = AsyncMock()

        # Mock service_manager and state_manager
        ctrl.service_manager = MagicMock()
        ctrl.service_manager.shutdown_services = AsyncMock()
        ctrl.state_manager = MagicMock()
        ctrl.state_manager.cancel_pending_saves = AsyncMock()

        return ctrl

    @pytest.mark.asyncio
    async def test_shutdown_calls_shutdown_services_after_request_shutdown(self, controller):
        """shutdown() must call shutdown_services() even after request_shutdown() ran.

        This is the core bug: request_shutdown() sets shutdown_requested=True,
        then shutdown() in the finally block sees the flag and returns early,
        skipping shutdown_services() entirely. The STT process never gets a
        shutdown command.
        """
        # Simulate signal handler path: request_shutdown runs first
        controller.request_shutdown()

        # Then shutdown() runs in the finally block
        await controller.shutdown()

        # shutdown_services MUST have been called
        controller.service_manager.shutdown_services.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_stops_services_before_websocket(self, controller):
        """shutdown_services() must complete before WebSocket is stopped.

        The STT shutdown command goes over WebSocket, so WebSocket must stay
        alive until after shutdown_services() finishes.
        """
        call_order = []

        async def mock_shutdown_services():
            call_order.append("shutdown_services")

        async def mock_app_shutdown():
            call_order.append("app_shutdown")

        controller.service_manager.shutdown_services = mock_shutdown_services
        controller.app.shutdown = mock_app_shutdown

        await controller.shutdown()

        assert call_order == ["shutdown_services", "app_shutdown"]

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self, controller):
        """Calling shutdown() twice should only run teardown once."""
        await controller.shutdown()
        await controller.shutdown()

        controller.service_manager.shutdown_services.assert_awaited_once()

    def test_request_shutdown_only_signals_event(self, controller):
        """request_shutdown() should only set the shutdown event, not tear down resources."""
        controller.request_shutdown()

        # Should have signaled the event
        controller.shutdown_event.set.assert_called_once()

        # Should NOT have called app.stop() directly
        controller.app.stop.assert_not_called()
