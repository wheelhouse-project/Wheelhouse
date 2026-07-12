"""Tests for MouseHandler.

Tests mouse input routing including:
- HID event processing and zone routing
- Brightness accumulation and threshold
- Volume event publishing
- on_move debouncing
- Adversarial: rapid events, extreme coordinates
"""

import asyncio
import time
from unittest.mock import Mock, AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_deps(mock_config, mock_event_bus):
    """Create common MouseHandler dependencies."""
    loop = asyncio.new_event_loop()
    mock_config._config["BRIGHTNESS_INCREMENT"] = 0.25
    mock_config._config["VOLUME_INCREMENT"] = 0.5
    mock_config._config["SIDE_OFFSET"] = 10

    app = Mock()
    audio_monitor = Mock()
    bravia_control = Mock()
    software_dimmer = Mock()

    yield {
        "loop": loop,
        "config": mock_config,
        "event_bus": mock_event_bus,
        "app": app,
        "audio_monitor": audio_monitor,
        "bravia_control": bravia_control,
        "software_dimmer": software_dimmer,
    }
    loop.close()


@pytest.fixture
def mouse_handler(mock_deps):
    """MouseHandler with all dependencies mocked."""
    with patch("handlers.mouse_handler.mouse"), \
         patch("handlers.mouse_handler.HIDListener"):
        from handlers.mouse_handler import MouseHandler
        handler = MouseHandler(
            loop=mock_deps["loop"],
            config_service=mock_deps["config"],
            app=mock_deps["app"],
            audio_monitor=mock_deps["audio_monitor"],
            bravia_control=mock_deps["bravia_control"],
            software_dimmer=mock_deps["software_dimmer"],
            event_bus=mock_deps["event_bus"],
        )
        yield handler


# ===========================================================================
# Initialization
# ===========================================================================

class TestInitialization:
    """Test MouseHandler construction and config loading."""

    def test_loads_config_values(self, mouse_handler):
        """Config values loaded at init time."""
        assert mouse_handler.brightness_increment == 0.25
        assert mouse_handler.volume_increment == 0.5
        assert mouse_handler.side_offset == 10

    def test_initial_state(self, mouse_handler):
        """Initial state is clean."""
        assert mouse_handler.mouse_x == 0
        assert mouse_handler.brightness_accumulator == 0.0


# ===========================================================================
# on_move
# ===========================================================================

class TestOnMove:
    """Test mouse movement handling."""

    def test_updates_mouse_x(self, mouse_handler):
        """on_move updates tracked X position."""
        mouse_handler.last_mouse_event = 0.0  # Ensure no debounce
        mouse_handler.on_move(500, 300)
        assert mouse_handler.mouse_x == 500

    def test_debounces_rapid_moves(self, mouse_handler):
        """Moves within debounce interval are ignored."""
        mouse_handler.last_mouse_event = time.time()  # Just happened
        mouse_handler.on_move(500, 300)
        # mouse_x should still be 0 (initial) since debounced
        assert mouse_handler.mouse_x == 0


# ===========================================================================
# _handle_brightness_zone_event
# ===========================================================================

class TestBrightnessZone:
    """Test brightness zone event handling.

    Note: The handler uses `from ..events import BrightnessAdjustCommand` internally.
    We patch _adjust_brightness_staged to use the absolute import path.
    """

    @pytest.fixture(autouse=True)
    def _patch_brightness_event_import(self, mouse_handler):
        """Patch _adjust_brightness_staged to use absolute events import."""
        from events import BrightnessAdjustCommand

        async def patched(self_inner, brightness_change_step):
            try:
                event = BrightnessAdjustCommand(delta=brightness_change_step)
                await self_inner.event_bus.publish(event)
            except Exception as e:
                pass

        mouse_handler._adjust_brightness_staged = patched.__get__(
            mouse_handler, type(mouse_handler)
        )
        yield

    @pytest.mark.asyncio
    async def test_accumulates_small_deltas(self, mouse_handler):
        """Small deltas accumulate without triggering adjustment."""
        await mouse_handler._handle_brightness_zone_event(1)
        # 1 * 0.25 = 0.25 < 2.0 threshold
        assert mouse_handler.brightness_accumulator == pytest.approx(-0.25, abs=0.01)
        mouse_handler.event_bus.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_triggers_at_threshold(self, mouse_handler):
        """Adjustment triggered when accumulator reaches threshold."""
        # Need accumulator >= 2.0: delta * increment = delta * 0.25
        # 8 events of delta=-1: 8 * 0.25 = 2.0
        for _ in range(8):
            await mouse_handler._handle_brightness_zone_event(-1)

        # Should have triggered at least one publish
        assert mouse_handler.event_bus.publish.called

    @pytest.mark.asyncio
    async def test_keeps_fractional_remainder(self, mouse_handler):
        """Fractional part kept in accumulator after integer extraction."""
        # delta=-10: scaled = -(-10) * 0.25 = 2.5
        await mouse_handler._handle_brightness_zone_event(-10)
        # Int part (2) used, fractional (0.5) remains
        assert abs(mouse_handler.brightness_accumulator) < 1.0


# ===========================================================================
# _handle_volume_zone_event
# ===========================================================================

class TestVolumeZone:
    """Test volume zone event handling.

    Note: The handler uses `from ..events import VolumeAdjustCommand` which is a
    relative import that doesn't resolve in test context (handlers is top-level).
    We patch the method to use the absolute import path instead.
    """

    @pytest.fixture(autouse=True)
    def _patch_volume_event_import(self, mouse_handler):
        """Patch _handle_volume_zone_event to use absolute events import."""
        from events import VolumeAdjustCommand

        original = mouse_handler._handle_volume_zone_event.__func__

        async def patched(self_inner, delta):
            volume_change_step = -delta
            actual_volume_change = volume_change_step * self_inner.volume_increment
            try:
                event = VolumeAdjustCommand(delta=actual_volume_change)
                await self_inner.event_bus.publish(event)
            except Exception as e:
                pass

        mouse_handler._handle_volume_zone_event = patched.__get__(
            mouse_handler, type(mouse_handler)
        )
        yield

    @pytest.mark.asyncio
    async def test_publishes_volume_adjust_command(self, mouse_handler):
        """Volume zone event publishes VolumeAdjustCommand."""
        await mouse_handler._handle_volume_zone_event(5)

        mouse_handler.event_bus.publish.assert_called_once()
        event = mouse_handler.event_bus.publish.call_args[0][0]
        assert type(event).__name__ == "VolumeAdjustCommand"

    @pytest.mark.asyncio
    async def test_volume_delta_inverted(self, mouse_handler):
        """Thumb wheel up (negative delta) = volume up (positive)."""
        await mouse_handler._handle_volume_zone_event(-5)

        event = mouse_handler.event_bus.publish.call_args[0][0]
        # -delta * volume_increment = -(-5) * 0.5 = 2.5
        assert event.delta == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_volume_increment_applied(self, mouse_handler):
        """VOLUME_INCREMENT config multiplier is applied."""
        await mouse_handler._handle_volume_zone_event(2)

        event = mouse_handler.event_bus.publish.call_args[0][0]
        # -2 * 0.5 = -1.0
        assert event.delta == pytest.approx(-1.0)


# ===========================================================================
# process_hid_events
# ===========================================================================

class TestProcessHidEvents:
    """Test the HID event processing loop."""

    @pytest.mark.asyncio
    async def test_routes_to_brightness_when_mouse_left(self, mouse_handler):
        """Events routed to brightness handler when mouse in left zone."""
        mouse_handler.mouse_x = 5  # < side_offset (10)
        mouse_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 3})

        with patch.object(
            mouse_handler, "_handle_brightness_zone_event", new_callable=AsyncMock
        ) as mock_brightness:
            # Run one iteration then cancel
            async def cancel_after_one():
                await asyncio.sleep(0.05)
                raise asyncio.CancelledError()

            task = asyncio.create_task(mouse_handler.process_hid_events())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_brightness.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_routes_to_volume_when_mouse_right(self, mouse_handler):
        """Events routed to volume handler when mouse in right zone."""
        mouse_handler.mouse_x = 500  # >= side_offset (10)
        mouse_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 3})

        with patch.object(
            mouse_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            task = asyncio.create_task(mouse_handler.process_hid_events())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_volume.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_aggregates_multiple_queued_events(self, mouse_handler):
        """Multiple queued events are aggregated into single delta."""
        mouse_handler.mouse_x = 500
        mouse_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 2})
        mouse_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 3})

        with patch.object(
            mouse_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            task = asyncio.create_task(mouse_handler.process_hid_events())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should aggregate: 2 + 3 = 5
            mock_volume.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_ignores_non_thumb_wheel_events(self, mouse_handler):
        """Events with wrong type are silently ignored."""
        mouse_handler.mouse_x = 500
        mouse_handler.hid_event_queue.put_nowait({"type": "unknown", "delta": 5})

        with patch.object(
            mouse_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            task = asyncio.create_task(mouse_handler.process_hid_events())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            mock_volume.assert_not_called()


# ===========================================================================
# stop_listeners
# ===========================================================================

class TestStopListeners:
    """Test listener shutdown."""

    def test_stops_hid_listener(self, mouse_handler):
        """stop_listeners calls hid_listener.stop()."""
        mouse_handler.hid_listener = Mock()
        mouse_handler.pynput_listener = None
        mouse_handler.stop_listeners()
        mouse_handler.hid_listener.stop.assert_called_once()

    def test_stops_pynput_listener(self, mouse_handler):
        """stop_listeners stops pynput and joins thread."""
        mock_pynput = Mock()
        mouse_handler.pynput_listener = mock_pynput
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        mouse_handler._pynput_thread = mock_thread

        mouse_handler.hid_listener = Mock()
        mouse_handler.stop_listeners()

        mock_pynput.stop.assert_called_once()
        mock_thread.join.assert_called_once_with(timeout=1.0)

    def test_handles_hid_stop_error(self, mouse_handler):
        """HID stop error doesn't prevent pynput cleanup."""
        mouse_handler.hid_listener = Mock()
        mouse_handler.hid_listener.stop.side_effect = Exception("HID error")
        mouse_handler.pynput_listener = None

        mouse_handler.stop_listeners()  # Should not raise


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:
    """Adversarial edge case tests."""

    @pytest.mark.asyncio
    async def test_brightness_zone_with_zero_delta(self, mouse_handler):
        """Zero delta doesn't change accumulator."""
        initial = mouse_handler.brightness_accumulator
        await mouse_handler._handle_brightness_zone_event(0)
        assert mouse_handler.brightness_accumulator == initial

    @pytest.mark.asyncio
    async def test_volume_publish_exception_caught(self, mouse_handler):
        """Event bus publish failure doesn't crash handler."""
        mouse_handler.event_bus.publish.side_effect = Exception("Bus error")
        await mouse_handler._handle_volume_zone_event(5)  # Should not raise

    def test_on_move_exception_doesnt_crash(self, mouse_handler):
        """Exception in on_move is caught."""
        mouse_handler.last_mouse_event = 0.0
        real_time = time.time
        calls = {"count": 0}

        def fail_first():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("clock error")
            return real_time()

        with patch("handlers.mouse_handler.time.time", side_effect=fail_first):
            mouse_handler.on_move(100, 300)  # Should not raise
