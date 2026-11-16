"""Tests for MouseHandler.

Tests mouse input routing including:
- HID event processing and zone routing
- Brightness accumulation and threshold
- Volume event publishing
- on_move stores every move
- Adversarial: rapid events, extreme coordinates
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
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
        mouse_handler.on_move(500, 300)
        assert mouse_handler.mouse_x == 500

    def test_the_last_move_of_a_flick_is_kept(self, mouse_handler):
        """A flick that ends right after an earlier move leaves mouse_x at its end.

        The pointer sends no further move once it stops, so a dropped last
        move left the wheel on the wrong zone (wh-wheel-zone-timeout-jump).
        """
        mouse_handler.on_move(300, 200)
        mouse_handler.on_move(0, 200)
        assert mouse_handler.mouse_x == 0


# ===========================================================================
# on_click / on_scroll: the numbered overlay's mouse refresh
# (wh-overlay-refresh-after-mouse)
# ===========================================================================


@asynccontextmanager
async def _overlay_entry(tmp_path, *, numbers_show=True):
    """A real Logic overlay, with its numbers painted or never shown.

    The Logic process of tests/overlay_boundary_harness.py: "show numbers",
    the walk answered and the paint acknowledged when ``numbers_show``.
    Yields ``(boundary, paint)``: ``paint`` is the paint_overlay message,
    or None. The overlay's hook entry is
    ``boundary.controller.overlay.note_pointer_input``.
    """
    from tests.overlay_boundary_harness import overlay_boundary

    async with overlay_boundary(tmp_path) as boundary:
        paint = None
        if numbers_show:
            paint = await boundary.show_numbers_painted("snap-1")
        yield boundary, paint


@contextmanager
def _posting_to(boundary, loop):
    """The overlay posts to ``loop`` (a stand-in) instead of the Logic loop.

    ``note_pointer_input`` runs on the pynput hook thread, reads one bool
    and posts at most one call to ``LogicController.loop``. The stand-in is
    in place only while the hook callbacks run, so the boundary's own awaits
    still use the real loop.
    """
    real = boundary.controller.loop
    boundary.controller.loop = loop
    try:
        yield
    finally:
        boundary.controller.loop = real


class TestPointerInputForOverlay:
    """Clicks and wheel steps reach the overlay without delaying the input.

    pynput runs these callbacks inside the WH_MOUSE_LL hook procedure, so
    every click and wheel step waits for them, and an exception that leaves
    a callback stops the listener (acceptance item 5).
    """

    def test_click_and_wheel_reach_the_listener_with_the_injected_flag(
        self, mouse_handler,
    ):
        listener = Mock()
        mouse_handler.set_pointer_input_listener(listener)
        mouse_handler.on_click(5, 6, "left", True, True)
        mouse_handler.on_click(5, 6, "left", False, False)
        mouse_handler.on_scroll(5, 6, 0, -1, True)
        mouse_handler.on_scroll(5, 6, 0, 1, False)
        assert listener.call_args_list == [
            call(True, True), call(True, False),
            call(False, True), call(False, False),
        ]

    def test_installed_pynput_passes_the_injected_flag(self, mouse_handler):
        """The installed pynput calls on_click and on_scroll with ``injected``.

        pynput 1.8 wraps a callback by its parameter count
        (pynput/_util/__init__.py ``_wrap``); a callback with fewer than five
        parameters never sees the flag.
        """
        from pynput.mouse import Button, Listener

        listener = Mock()
        mouse_handler.set_pointer_input_listener(listener)
        hook = Listener(
            on_move=mouse_handler.on_move,
            on_click=mouse_handler.on_click,
            on_scroll=mouse_handler.on_scroll,
        )
        hook.on_click(1, 2, Button.left, True, True)
        hook.on_scroll(1, 2, 0, -1, True)
        assert listener.call_args_list == [call(True, True), call(False, True)]

    def test_no_listener_is_a_no_op(self, mouse_handler):
        assert mouse_handler.on_click(1, 2, "left", True, False) is None
        assert mouse_handler.on_scroll(1, 2, 0, 1, False) is None

    async def test_overlay_entry_posts_one_call_per_event_and_writes_no_log(
        self, mouse_handler, caplog, tmp_path,
    ):
        import logging

        from tests.overlay_boundary_harness import settle

        loop = Mock()
        async with _overlay_entry(tmp_path, numbers_show=True) as (b, paint):
            overlay = b.controller.overlay
            mouse_handler.set_pointer_input_listener(overlay.note_pointer_input)
            caplog.clear()
            with _posting_to(b, loop), caplog.at_level(logging.DEBUG):
                mouse_handler.on_click(5, 6, "left", True, False)
                assert loop.call_soon_threadsafe.call_count == 1
                mouse_handler.on_scroll(5, 6, 0, -1, True)
                assert loop.call_soon_threadsafe.call_count == 2
                records = list(caplog.records)
            assert [name for name, _a, _k in loop.method_calls] == [
                "call_soon_threadsafe", "call_soon_threadsafe",
            ]
            posted = [c.args for c in loop.call_soon_threadsafe.call_args_list]
            assert [args[1:] for args in posted] == [(True, False), (False, True)]
            assert records == []
            # The posted calls are the overlay's: run on the Logic loop, they
            # give one refresh once the quiet time has passed.
            mark = b.mark()
            for args in posted:
                b.loop.call_soon(*args)
            await settle()
            await b.advance(0.6)
            frames, _gui = await b.quiet(mark, 0.05)
            walks = [f for f in frames if f.action == "start_overlay_walk"]
            assert [
                (w.params["overlay_session_id"], w.params["paint_generation"])
                for w in walks
            ] == [(paint["overlay_session_id"], paint["paint_generation"] + 1)]

    async def test_overlay_entry_posts_nothing_while_no_numbers_can_show(
        self, mouse_handler, caplog, tmp_path,
    ):
        import logging

        loop = Mock()
        async with _overlay_entry(tmp_path, numbers_show=False) as (b, _paint):
            mouse_handler.set_pointer_input_listener(
                b.controller.overlay.note_pointer_input
            )
            caplog.clear()
            with _posting_to(b, loop), caplog.at_level(logging.DEBUG):
                mouse_handler.on_click(5, 6, "left", True, False)
                mouse_handler.on_scroll(5, 6, 0, -1, False)
                records = list(caplog.records)
        assert loop.method_calls == []
        assert records == []

    @pytest.mark.parametrize("where", ["listener", "closed_loop"])
    async def test_an_exception_does_not_leave_the_callback(
        self, mouse_handler, caplog, tmp_path, where,
    ):
        import logging

        async with AsyncExitStack() as stack:
            loop = Mock()
            if where == "listener":
                listener = Mock(side_effect=RuntimeError("listener failed"))
            else:
                loop.call_soon_threadsafe.side_effect = RuntimeError(
                    "Event loop is closed"
                )
                b, _paint = await stack.enter_async_context(
                    _overlay_entry(tmp_path, numbers_show=True)
                )
                stack.enter_context(_posting_to(b, loop))
                listener = b.controller.overlay.note_pointer_input
            mouse_handler.set_pointer_input_listener(listener)
            escaped = []
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                for callback, args in (
                    (mouse_handler.on_click, (5, 6, "left", True, False)),
                    (mouse_handler.on_scroll, (5, 6, 0, -1, False)),
                ):
                    try:
                        callback(*args)
                    except Exception as exc:  # noqa: BLE001 -- the assertion below
                        escaped.append(exc)
                records = list(caplog.records)
            if where == "closed_loop":
                # The overlay's numbers show, so each event reached the
                # closed loop.
                assert loop.call_soon_threadsafe.call_count == 2
        assert escaped == []
        assert records == []


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
# The zone comes from the batch's own stamp (wh-wheel-zone-timeout-jump)
# ===========================================================================

def _wheel_batch(delta, x, at=100.0):
    """One queued batch as HIDListener sends it when it has a pointer reader."""
    return {"type": "thumb_wheel", "delta": delta, "at": at, "x": x}


def _record_zone_calls(handler):
    """Replace both zone handlers; returns their calls in order."""
    calls = []

    async def brightness(ticks):
        calls.append(("brightness", ticks))

    async def volume(ticks):
        calls.append(("volume", ticks))

    handler._handle_brightness_zone_event = brightness
    handler._handle_volume_zone_event = volume
    return calls


async def _stop_processor(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestZoneFromBatchStamp:
    """Regression for wh-wheel-zone-timeout-jump. Log of 2026-11-01: a Samsung
    timeout held the loop for 3 seconds, the wheel batches queued behind it,
    and the sum went to the zone of the pointer at the end of the wait:
    'VOLUME zone (X=1127): Delta=-29', Sonos 14 to 43."""

    @pytest.mark.asyncio
    async def test_left_edge_movement_behind_a_tv_wait_never_reaches_volume(self, mouse_handler):
        """Acceptance item 1, forward."""
        calls = _record_zone_calls(mouse_handler)
        waiting_for_tv = asyncio.Event()
        tv_answered = asyncio.Event()

        async def brightness_waits_for_the_tv(ticks):
            calls.append(("brightness", ticks))
            waiting_for_tv.set()
            await tv_answered.wait()

        mouse_handler._handle_brightness_zone_event = brightness_waits_for_the_tv
        queue = mouse_handler.hid_event_queue
        mouse_handler.mouse_x = 5
        queue.put_nowait(_wheel_batch(3, x=5))
        task = asyncio.create_task(mouse_handler.process_hid_events())
        await asyncio.wait_for(waiting_for_tv.wait(), timeout=1.0)

        queue.put_nowait(_wheel_batch(-14, x=5))
        queue.put_nowait(_wheel_batch(-15, x=5))
        mouse_handler.mouse_x = 1127
        tv_answered.set()
        await asyncio.sleep(0.05)
        await _stop_processor(task)

        assert calls == [("brightness", 3), ("brightness", -29)]

    @pytest.mark.asyncio
    async def test_volume_zone_movement_behind_a_wait_never_reaches_brightness(self, mouse_handler):
        """Acceptance item 1, reverse."""
        calls = _record_zone_calls(mouse_handler)
        waiting = asyncio.Event()
        answered = asyncio.Event()

        async def volume_waits_for_the_speaker(ticks):
            calls.append(("volume", ticks))
            waiting.set()
            await answered.wait()

        mouse_handler._handle_volume_zone_event = volume_waits_for_the_speaker
        queue = mouse_handler.hid_event_queue
        mouse_handler.mouse_x = 500
        queue.put_nowait(_wheel_batch(3, x=500))
        task = asyncio.create_task(mouse_handler.process_hid_events())
        await asyncio.wait_for(waiting.wait(), timeout=1.0)

        queue.put_nowait(_wheel_batch(-14, x=500))
        queue.put_nowait(_wheel_batch(-15, x=500))
        mouse_handler.mouse_x = 5
        answered.set()
        await asyncio.sleep(0.05)
        await _stop_processor(task)

        assert calls == [("volume", 3), ("volume", -29)]

    @pytest.mark.asyncio
    async def test_real_listener_stamp_decides_the_zone(self, mock_deps):
        """The real HIDListener and MouseHandler together: the listener reads
        the handler's mouse_x at the send, and the pointer moves after that."""
        with patch("handlers.mouse_handler.mouse"), patch("handlers.hid_listener.hid"):
            from handlers.mouse_handler import MouseHandler
            handler = MouseHandler(
                loop=asyncio.get_running_loop(),
                config_service=mock_deps["config"],
                app=mock_deps["app"],
                audio_monitor=mock_deps["audio_monitor"],
                bravia_control=mock_deps["bravia_control"],
                software_dimmer=mock_deps["software_dimmer"],
                event_bus=mock_deps["event_bus"],
            )
        calls = _record_zone_calls(handler)
        handler.mouse_x = 5
        handler.hid_listener.delta_accumulator = 3
        handler.hid_listener._flush_tail()
        handler.mouse_x = 1127

        await _run_one_drain(handler)

        assert calls == [("brightness", 3)]

    @pytest.mark.parametrize("stamp, zone", [
        pytest.param(0, "brightness", id="the-left-screen-edge"),
        pytest.param(9, "brightness", id="one-inside-the-left-edge"),
        pytest.param(10, "volume", id="at-the-side-offset"),
    ])
    @pytest.mark.asyncio
    async def test_the_stamp_decides_against_the_side_offset(self, mouse_handler, stamp, zone):
        """The pointer stands in the other zone, so only the stamp can give
        this result."""
        mouse_handler.mouse_x = 500 if zone == "brightness" else 0
        calls = _record_zone_calls(mouse_handler)
        mouse_handler.hid_event_queue.put_nowait(_wheel_batch(3, x=stamp))

        await _run_one_drain(mouse_handler)

        assert calls == [(zone, 3)]

    @pytest.mark.asyncio
    async def test_a_drain_with_both_zones_gives_each_its_own_ticks_in_order(self, mouse_handler):
        calls = _record_zone_calls(mouse_handler)
        for delta, x in ((1, 5), (2, 5), (4, 500), (8, 5)):
            mouse_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=x))

        await _run_one_drain(mouse_handler)

        assert calls == [("brightness", 3), ("volume", 4), ("brightness", 8)]

    @pytest.mark.asyncio
    async def test_a_drain_with_one_zone_makes_one_call_with_the_sum(self, mouse_handler):
        """Bounding test: a stamped drain in one zone behaves as an unstamped
        drain does (test_aggregates_multiple_queued_events). Green before and
        after."""
        mouse_handler.mouse_x = 500
        calls = _record_zone_calls(mouse_handler)
        for delta in (2, 3, 4):
            mouse_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=500))

        await _run_one_drain(mouse_handler)

        assert calls == [("volume", 9)]

    @pytest.mark.asyncio
    async def test_a_run_that_nets_to_zero_reaches_no_zone(self, mouse_handler):
        """The first run passes +2 and -2; only the second run is acted on."""
        calls = _record_zone_calls(mouse_handler)
        for delta, x in ((2, 5), (-2, 5), (4, 500)):
            mouse_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=x))

        await _run_one_drain(mouse_handler)

        assert calls == [("volume", 4)]

    @pytest.mark.asyncio
    async def test_every_drained_batch_is_filtered_before_the_first_zone_acts(self, mouse_handler):
        """The first zone's wait must not hold back the filtering of the batches
        drained with it: the runs are final before any await."""
        real_filter_batch = mouse_handler.thumb_wheel_filter.filter_batch
        filter_batch = Mock(side_effect=real_filter_batch)
        mouse_handler.thumb_wheel_filter.filter_batch = filter_batch
        filtered_when_a_zone_acted = []

        async def record_filtered_count(_ticks):
            filtered_when_a_zone_acted.append(filter_batch.call_count)

        mouse_handler._handle_brightness_zone_event = record_filtered_count
        mouse_handler._handle_volume_zone_event = record_filtered_count
        for delta, x in ((1, 5), (2, 500), (4, 5)):
            mouse_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=x))

        await _run_one_drain(mouse_handler)

        assert filtered_when_a_zone_acted == [3, 3, 3]

    @pytest.mark.asyncio
    async def test_each_run_logs_its_zone_stamp_and_ticks(self, mouse_handler, caplog):
        """The bead's log could not show where the pointer was. Each run names
        the X of its last batch, not the current position."""
        _record_zone_calls(mouse_handler)
        mouse_handler.mouse_x = 1127
        for delta, x in ((1, 3), (2, 7), (4, 500)):
            mouse_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=x))

        with caplog.at_level("DEBUG", logger="handlers.mouse_handler"):
            await _run_one_drain(mouse_handler)

        lines = [r.getMessage() for r in caplog.records if "zone=" in r.getMessage()]
        assert len(lines) == 2
        assert "zone=brightness" in lines[0] and "x=7" in lines[0] and "ticks=3" in lines[0]
        assert "zone=volume" in lines[1] and "x=500" in lines[1] and "ticks=4" in lines[1]


class TestHeldTicksStayInTheirZone:
    """ThumbWheelFilter holds ticks below the dead zone. Without a reset at a
    zone change, ticks rolled at the left edge would be released into the
    volume zone by the next batch, which acceptance item 1 forbids."""

    @pytest.fixture
    def filtered_handler(self, mock_deps):
        return _build_handler(mock_deps, {
            "THUMB_WHEEL_DEAD_ZONE_TICKS": 3,
            "THUMB_WHEEL_GESTURE_GAP_MS": 500,
            "THUMB_WHEEL_MAX_TICKS_PER_BATCH": 3,
        })

    @pytest.mark.asyncio
    async def test_ticks_held_at_the_left_edge_never_reach_volume(self, filtered_handler):
        calls = _record_zone_calls(filtered_handler)
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(2, x=5, at=100.0))
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(1, x=500, at=100.05))

        await _run_one_drain(filtered_handler)

        assert calls == []

    @pytest.mark.asyncio
    async def test_ticks_held_in_the_volume_zone_never_reach_brightness(self, filtered_handler):
        calls = _record_zone_calls(filtered_handler)
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(2, x=500, at=100.0))
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(1, x=5, at=100.05))

        await _run_one_drain(filtered_handler)

        assert calls == []

    @pytest.mark.asyncio
    async def test_the_reset_works_across_two_drains(self, filtered_handler):
        calls = _record_zone_calls(filtered_handler)
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(2, x=5, at=100.0))
        await _run_one_drain(filtered_handler)

        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(1, x=500, at=100.05))
        await _run_one_drain(filtered_handler)

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_zone_change_closes_an_open_gesture(self, filtered_handler):
        """The left edge opens a gesture; the first tick in the volume zone
        meets the dead zone again."""
        calls = _record_zone_calls(filtered_handler)
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(3, x=5, at=100.0))
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(1, x=500, at=100.05))

        await _run_one_drain(filtered_handler)

        assert calls == [("brightness", 3)]

    @pytest.mark.asyncio
    async def test_the_new_zone_counts_its_own_ticks_after_a_change(self, filtered_handler):
        """Bounding test: one reset for each change of zone. After the change,
        the ticks of the new zone build up to the dead zone as usual."""
        calls = _record_zone_calls(filtered_handler)
        for delta, x, at in ((2, 5, 100.0), (2, 500, 100.05), (1, 500, 100.1)):
            filtered_handler.hid_event_queue.put_nowait(_wheel_batch(delta, x=x, at=at))

        await _run_one_drain(filtered_handler)

        assert calls == [("volume", 3)]

    @pytest.mark.asyncio
    async def test_two_drains_in_one_zone_still_share_the_dead_zone(self, filtered_handler):
        """Bounding test: only a zone CHANGE resets. Green before and after."""
        calls = _record_zone_calls(filtered_handler)
        filtered_handler.mouse_x = 5
        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(2, x=5, at=100.0))
        await _run_one_drain(filtered_handler)

        filtered_handler.hid_event_queue.put_nowait(_wheel_batch(1, x=5, at=100.05))
        await _run_one_drain(filtered_handler)

        assert calls == [("brightness", 3)]


class TestPointerReader:
    """MouseHandler gives HIDListener the means to stamp each batch."""

    def test_the_listener_reader_returns_the_current_mouse_x(self, mock_deps):
        """A reader that returned the value at construction would stamp every
        batch with the starting position."""
        with patch("handlers.mouse_handler.mouse"), \
             patch("handlers.mouse_handler.HIDListener") as listener_class:
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

        reader = listener_class.call_args.kwargs["pointer_x"]
        handler.mouse_x = 7
        assert reader() == 7
        handler.mouse_x = 900
        assert reader() == 900


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
