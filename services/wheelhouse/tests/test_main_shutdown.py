"""Tests for LogicController shutdown ordering.

Verifies that shutdown_services() is always called during shutdown,
even when request_shutdown() runs first (the signal-handler path).
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestShutdownOrdering:
    """Verify shutdown_services is called regardless of how shutdown is triggered."""

    @pytest.fixture
    def controller(self):
        """Create a minimal LogicController with mocked dependencies."""
        from main import LogicController
        from services.wheelhouse.numbered_overlay.controller import (
            NumberedOverlayController,
        )
        from services.wheelhouse.ui.click_config import ClickConfig

        ctrl = LogicController.__new__(LogicController)
        # The public constructor, as main.py builds it.
        ctrl.click_config = ClickConfig.from_raw({})
        ctrl.overlay = NumberedOverlayController(ctrl, clock=time.monotonic)
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

    @pytest.mark.asyncio
    async def test_shutdown_cancels_clear_ack_watchdog(self, controller, caplog):
        """wh-overlay-slow-uia-stale-badges.18.9: an armed clear-ack
        deadline must not survive into the slow service teardown -- a
        5000 ms fire during an intentional exit would report a false
        user-visible overlay clear fault. shutdown() cancels the timer,
        clears the pending slot, and reports nothing.

        The deadline is armed the way the app arms it: badges are painted,
        then "hide numbers" sends clear_overlay to the GUI. The event loop's
        call_later hands back a MagicMock timer and keeps the callback, so
        the test can see the cancel and can then run the callback as a
        deadline that was already queued when shutdown began.
        """
        import logging

        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
            PaintAckState,
        )

        loop = asyncio.get_running_loop()
        controller.loop = loop
        controller.grid_overlay_state = None
        controller.app.send_request = AsyncMock(return_value={"status": "ok"})
        gui_messages: list = []
        controller.state_manager.state_to_gui_queue.put_nowait = (
            gui_messages.append
        )

        # Badges are painted (pure machine transitions; no effects run).
        machine = controller.overlay.click_overlay_state
        machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = machine.overlay_session_id, machine.paint_generation
        machine.apply(OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
            paint_generation=gen, snapshot_id="snap-1",
        ))
        machine.apply(OverlayEvent(
            OverlayEventKind.PAINT_ACK, overlay_session_id=sid,
            paint_generation=gen, paint_state=PaintAckState.PAINTED,
        ))
        assert machine.state is OverlayState.PAINTED

        # "hide numbers": the clear goes to the GUI and arms the deadline.
        armed: list = []
        real_call_later = loop.call_later

        def _call_later(delay, callback, *args, **kwargs):
            timer = MagicMock()
            armed.append((delay, callback, args, timer))
            return timer

        loop.call_later = _call_later
        try:
            batch = await controller.overlay.handle_overlay_command(
                "hide", "tr-hide",
            )
            assert batch is not None
            await batch
        finally:
            loop.call_later = real_call_later
        assert [m["action"] for m in gui_messages] == ["clear_overlay"]
        deadlines = [a for a in armed if a[0] == 5.0]
        assert len(deadlines) == 1, armed
        _delay, callback, args, timer = deadlines[0]

        caplog.set_level(logging.WARNING)
        await controller.shutdown()

        timer.cancel.assert_called_once()
        # A deadline already queued on the loop when shutdown began fires
        # after it: it must report nothing.
        callback(*args)
        reports = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "badges may still be on the screen" in r.getMessage()
        ]
        assert reports == []
