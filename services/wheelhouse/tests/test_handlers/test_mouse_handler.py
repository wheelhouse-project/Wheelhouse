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
from unittest.mock import Mock, AsyncMock, MagicMock, call, patch

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
    # wh-mouse-wheel-sensitivity: make the thumb wheel filter pass every
    # batch unchanged, so the routing tests below keep their exact numbers.
    # TestThumbWheelFilterInLoop builds its own filter with real settings.
    mock_config._config["THUMB_WHEEL_DEAD_ZONE_TICKS"] = 1
    mock_config._config["THUMB_WHEEL_GESTURE_GAP_MS"] = 500
    mock_config._config["THUMB_WHEEL_MAX_TICKS_PER_BATCH"] = 100

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

    @staticmethod
    def _published_deltas(mouse_handler):
        return [c.args[0].delta for c in mouse_handler.event_bus.publish.call_args_list]

    @pytest.mark.asyncio
    async def test_one_hardware_step_per_command(self, mouse_handler):
        """Regression for wh-brightness-wheel-one-step. With David's
        BRIGHTNESS_INCREMENT of 1.0, a drain of 14 ticks (eight batches queued
        behind one 450 ms Samsung round trip) must reach the TV as at most two
        native steps (4 on the 0-100 scale; David set two on 2026-09-14 after
        trying one), not as 14, which the Samsung plugin turned into seven."""
        mouse_handler.brightness_increment = 1.0
        await mouse_handler._handle_brightness_zone_event(-14)
        assert self._published_deltas(mouse_handler) == [4]

    @pytest.mark.asyncio
    async def test_one_hardware_step_per_command_when_dimming(self, mouse_handler):
        """The same limit in the other direction."""
        mouse_handler.brightness_increment = 1.0
        await mouse_handler._handle_brightness_zone_event(14)
        assert self._published_deltas(mouse_handler) == [-4]

    @pytest.mark.asyncio
    async def test_excess_ticks_are_discarded_not_carried(self, mouse_handler):
        """The ticks beyond one step are dropped, not saved for later:
        carrying them would keep the TV moving after the wheel stops. After
        the capped command, one more tick (1.0, below the 2.0 step) publishes
        nothing."""
        mouse_handler.brightness_increment = 1.0
        await mouse_handler._handle_brightness_zone_event(-14)
        assert mouse_handler.brightness_accumulator == pytest.approx(0.0)
        await mouse_handler._handle_brightness_zone_event(-1)
        assert self._published_deltas(mouse_handler) == [4]

    @pytest.mark.asyncio
    async def test_exactly_one_step_passes_unchanged(self, mouse_handler):
        """Bounding test, green before and after: two ticks at 1.0 are exactly
        one step and go through as 2."""
        mouse_handler.brightness_increment = 1.0
        await mouse_handler._handle_brightness_zone_event(-2)
        assert self._published_deltas(mouse_handler) == [2]

    @pytest.mark.asyncio
    async def test_three_ticks_pass_as_three_below_the_cap(self, mouse_handler):
        """Bounding test: 3 at 1.0 is under the two-step cap of 4 and goes
        through as 3 (the Samsung plugin rounds that to one native step)."""
        mouse_handler.brightness_increment = 1.0
        await mouse_handler._handle_brightness_zone_event(-3)
        assert self._published_deltas(mouse_handler) == [3]

    @pytest.mark.asyncio
    async def test_fraction_below_a_step_is_still_kept(self, mouse_handler):
        """Bounding test, green before and after: with the default 0.25
        increment, 10 ticks are 2.5; the step of 2 goes out and 0.5 stays so
        a slow roll keeps its fine control."""
        await mouse_handler._handle_brightness_zone_event(-10)
        assert self._published_deltas(mouse_handler) == [2]
        assert mouse_handler.brightness_accumulator == pytest.approx(0.5)


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
# Thumb wheel filter in the processing loop (wh-mouse-wheel-sensitivity)
# ===========================================================================

def _build_handler(mock_deps, config_overrides):
    """A second MouseHandler whose config lacks or changes the filter keys."""
    for key in (
        "THUMB_WHEEL_DEAD_ZONE_TICKS",
        "THUMB_WHEEL_GESTURE_GAP_MS",
        "THUMB_WHEEL_MAX_TICKS_PER_BATCH",
        "BRIGHTNESS_STEPS_PER_COMMAND",
    ):
        mock_deps["config"]._config.pop(key, None)
    mock_deps["config"]._config.update(config_overrides)
    with patch("handlers.mouse_handler.mouse"), \
         patch("handlers.mouse_handler.HIDListener"):
        from handlers.mouse_handler import MouseHandler
        return MouseHandler(
            loop=mock_deps["loop"],
            config_service=mock_deps["config"],
            app=mock_deps["app"],
            audio_monitor=mock_deps["audio_monitor"],
            bravia_control=mock_deps["bravia_control"],
            software_dimmer=mock_deps["software_dimmer"],
            event_bus=mock_deps["event_bus"],
        )


async def _run_one_drain(handler):
    task = asyncio.create_task(handler.process_hid_events())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestThumbWheelFilterSettings:
    """The handler builds its filter from config, with the shipped defaults."""

    def test_reads_filter_settings_from_config(self, mouse_handler):
        wheel = mouse_handler.thumb_wheel_filter
        assert wheel.dead_zone_ticks == 1
        assert wheel.gesture_gap_s == pytest.approx(0.5)
        assert wheel.max_ticks_per_batch == 100

    def test_missing_keys_use_shipped_defaults(self, mock_deps):
        from handlers.thumb_wheel_filter import (
            DEFAULT_DEAD_ZONE_TICKS,
            DEFAULT_GESTURE_GAP_MS,
            DEFAULT_MAX_TICKS_PER_BATCH,
        )
        handler = _build_handler(mock_deps, {})
        wheel = handler.thumb_wheel_filter
        assert wheel.dead_zone_ticks == DEFAULT_DEAD_ZONE_TICKS
        assert wheel.gesture_gap_s == pytest.approx(DEFAULT_GESTURE_GAP_MS / 1000)
        assert wheel.max_ticks_per_batch == DEFAULT_MAX_TICKS_PER_BATCH

    def test_custom_keys_reach_the_filter(self, mock_deps):
        handler = _build_handler(mock_deps, {
            "THUMB_WHEEL_DEAD_ZONE_TICKS": 5,
            "THUMB_WHEEL_GESTURE_GAP_MS": 250,
            "THUMB_WHEEL_MAX_TICKS_PER_BATCH": 2,
        })
        wheel = handler.thumb_wheel_filter
        assert wheel.dead_zone_ticks == 5
        assert wheel.gesture_gap_s == pytest.approx(0.25)
        assert wheel.max_ticks_per_batch == 2


class TestBrightnessStepsPerCommand:
    """BRIGHTNESS_STEPS_PER_COMMAND (wh-brightness-wheel-two-steps): how many
    native TV steps one brightness command may carry. David chose two on
    2026-09-14 and then asked for the number to be a configuration key."""

    @staticmethod
    def _sent_steps(handler):
        """The deltas the zone handed to _adjust_brightness_staged. The
        real method does a relative import that fails under pytest, so
        each test replaces it with an AsyncMock first."""
        handler._adjust_brightness_staged = AsyncMock()
        return handler

    def test_missing_key_uses_two_steps(self, mock_deps):
        from handlers.mouse_handler import BRIGHTNESS_STEP, DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND
        handler = _build_handler(mock_deps, {})
        assert DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND == 2
        assert handler.brightness_max_per_command == 2 * BRIGHTNESS_STEP

    @pytest.mark.asyncio
    async def test_three_steps_from_config(self, mock_deps):
        """Three native steps are 6 on the 0-100 scale: a 14-tick drain at
        increment 1.0 goes out as 6, not the default 4."""
        handler = self._sent_steps(_build_handler(mock_deps, {"BRIGHTNESS_STEPS_PER_COMMAND": 3}))
        handler.brightness_increment = 1.0
        await handler._handle_brightness_zone_event(-14)
        assert handler._adjust_brightness_staged.call_args_list == [call(6)]

    @pytest.mark.asyncio
    async def test_one_step_from_config(self, mock_deps):
        """One native step: the behaviour David tried first (8bfaf462)."""
        handler = self._sent_steps(_build_handler(mock_deps, {"BRIGHTNESS_STEPS_PER_COMMAND": 1}))
        handler.brightness_increment = 1.0
        await handler._handle_brightness_zone_event(14)
        assert handler._adjust_brightness_staged.call_args_list == [call(-2)]

    @pytest.mark.asyncio
    async def test_value_below_one_is_treated_as_one(self, mock_deps):
        """0 would clamp every command to nothing and silently disable the
        brightness zone; the handler floors the value at one step."""
        handler = self._sent_steps(_build_handler(mock_deps, {"BRIGHTNESS_STEPS_PER_COMMAND": 0}))
        handler.brightness_increment = 1.0
        await handler._handle_brightness_zone_event(-14)
        assert handler._adjust_brightness_staged.call_args_list == [call(2)]


class TestThumbWheelFilterInLoop:
    """process_hid_events passes each drained batch through the filter first."""

    @pytest.fixture
    def filtered_handler(self, mock_deps):
        return _build_handler(mock_deps, {
            "THUMB_WHEEL_DEAD_ZONE_TICKS": 3,
            "THUMB_WHEEL_GESTURE_GAP_MS": 500,
            "THUMB_WHEEL_MAX_TICKS_PER_BATCH": 3,
        })

    @pytest.mark.asyncio
    async def test_single_tick_reaches_no_zone(self, filtered_handler):
        """The slightest touch, one tick, changes nothing."""
        filtered_handler.mouse_x = 500
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 1})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_zone_crossing_reaches_volume_zone(self, filtered_handler):
        filtered_handler.mouse_x = 500
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 3})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_flick_is_capped_before_volume_zone(self, filtered_handler):
        filtered_handler.mouse_x = 500
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 12})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_filter_applies_to_brightness_zone_too(self, filtered_handler):
        filtered_handler.mouse_x = 5
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": -1})

        with patch.object(
            filtered_handler, "_handle_brightness_zone_event", new_callable=AsyncMock
        ) as mock_brightness:
            await _run_one_drain(filtered_handler)
            mock_brightness.assert_not_called()

    @pytest.mark.asyncio
    async def test_queued_batches_share_the_dead_zone(self, filtered_handler):
        """Two queued batches of 1 and 2 cross the dead zone together."""
        filtered_handler.mouse_x = 500
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 1})
        filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 2})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_each_queued_batch_gets_its_own_cap(self, filtered_handler):
        """Three 50 ms batches that queued up while the consumer was busy
        are three batches, not one flick: 3 + 3 + 3 reaches the zone as 9
        (wh-mouse-wheel-sensitivity.1.1)."""
        filtered_handler.mouse_x = 500
        for _ in range(3):
            filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": 3})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_called_once_with(9)

    @pytest.mark.asyncio
    async def test_queued_batch_arrival_times_reach_the_filter(self, filtered_handler):
        """The filter sees every drained batch on its own, in queue order,
        with the arrival time the listener stamped on it."""
        filtered_handler.mouse_x = 500
        filtered_handler.hid_event_queue.put_nowait(
            {"type": "thumb_wheel", "delta": 1, "at": 100.0})
        filtered_handler.hid_event_queue.put_nowait(
            {"type": "thumb_wheel", "delta": 2, "at": 100.05})
        filter_batch = Mock(side_effect=[0, 3])
        filtered_handler.thumb_wheel_filter.filter_batch = filter_batch

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)

        assert filter_batch.call_args_list == [
            call(1, at=100.0),
            call(2, at=100.05),
        ]
        mock_volume.assert_called_once_with(3)

    @pytest.mark.asyncio
    async def test_slow_zone_action_does_not_merge_later_batches(self, filtered_handler):
        """Regression for wh-mouse-wheel-sensitivity.1.1. The volume zone
        awaits its plugin (Sonos: three network calls), so later 50 ms
        batches queue behind it. Each keeps its own cap: two queued batches
        of 3 reach the zone as 6, not as one capped batch of 3."""
        filtered_handler.mouse_x = 500
        queue = filtered_handler.hid_event_queue
        queue.put_nowait({"type": "thumb_wheel", "delta": 3})
        seen = []

        async def slow_volume_action(ticks):
            seen.append(ticks)
            if len(seen) == 1:
                queue.put_nowait({"type": "thumb_wheel", "delta": 3})
                queue.put_nowait({"type": "thumb_wheel", "delta": 3})
                await asyncio.sleep(0.02)

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", side_effect=slow_volume_action
        ):
            await _run_one_drain(filtered_handler)

        assert seen == [3, 6]

    @pytest.mark.asyncio
    async def test_cancelling_raw_deltas_still_route_the_passed_ticks(self, filtered_handler):
        """Regression for wh-mouse-wheel-sensitivity.1.2. Queued batches
        +4, -2, -2 sum to zero raw, but the filter passes 3, -2, -2 (the cap
        clips the first), so the volume zone must receive -1, not nothing."""
        filtered_handler.mouse_x = 500
        for delta in (4, -2, -2):
            filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": delta})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
            mock_volume.assert_called_once_with(-1)

    @pytest.mark.asyncio
    async def test_cancelling_raw_deltas_route_to_the_brightness_zone_too(self, filtered_handler):
        """The sign-reversed sequence in the other zone: -4, +2, +2 passes
        -3, 2, 2, so brightness receives +1."""
        filtered_handler.mouse_x = 5
        for delta in (-4, 2, 2):
            filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": delta})

        with patch.object(
            filtered_handler, "_handle_brightness_zone_event", new_callable=AsyncMock
        ) as mock_brightness:
            await _run_one_drain(filtered_handler)
            mock_brightness.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_correction_behind_a_slow_zone_action_is_not_lost(self, filtered_handler):
        """Codex's reproduction for wh-mouse-wheel-sensitivity.1.2: a 3-tick
        batch opens the gesture and its volume action waits on the plugin;
        +4, -2, -2 queue behind it and must reach the zone as -1."""
        filtered_handler.mouse_x = 500
        queue = filtered_handler.hid_event_queue
        queue.put_nowait({"type": "thumb_wheel", "delta": 3})
        seen = []

        async def slow_volume_action(ticks):
            seen.append(ticks)
            if len(seen) == 1:
                for delta in (4, -2, -2):
                    queue.put_nowait({"type": "thumb_wheel", "delta": delta})
                await asyncio.sleep(0.02)

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", side_effect=slow_volume_action
        ):
            await _run_one_drain(filtered_handler)

        assert seen == [3, -1]

    @pytest.mark.asyncio
    async def test_wobble_that_cancels_below_the_dead_zone_reaches_no_zone(self, filtered_handler):
        """Bounds the .1.2 fix: +2 then -2 with the gesture closed passes
        nothing, so nothing is routed. Green before and after the fix."""
        filtered_handler.mouse_x = 500
        for delta in (2, -2):
            filtered_handler.hid_event_queue.put_nowait({"type": "thumb_wheel", "delta": delta})

        with patch.object(
            filtered_handler, "_handle_volume_zone_event", new_callable=AsyncMock
        ) as mock_volume:
            await _run_one_drain(filtered_handler)
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
