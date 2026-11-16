"""High-precision mouse input handling with event-based coordination.

This module provides pure input routing for mouse movements, clicks, and specialized
hardware input through HID devices. It publishes events to the EventBus for brightness
and volume control, delegating all coordination logic to dedicated coordinators and
plugins. This design keeps MouseHandler focused solely on input capture and routing.

Key Classes:
  - MouseHandler: Pure input router for mouse and HID events.

Key Features:
  - High-resolution mouse tracking and movement processing
  - HID thumb wheel integration for volume and brightness zones
  - Event-based communication via EventBus (BrightnessAdjustCommand, VolumeAdjustCommand)
  - Multi-screen coordinate management

Event-Driven Architecture:
  - Publishes BrightnessAdjustCommand for brightness control
  - Publishes VolumeAdjustCommand for volume control
  - BrightnessCoordinator handles all brightness staging logic
  - Plugins handle device-specific control (BraviaPlugin, SonosPlugin)

Typical Usage:
  from handlers.mouse_handler import MouseHandler
  
  mouse_handler = MouseHandler(
      loop=event_loop,
      config=config,
      app=app_interface,
      audio_monitor=audio_monitor,
      event_bus=event_bus
  )
  
  # Start mouse monitoring tasks
  tasks = mouse_handler.start()
"""

import asyncio
from dataclasses import dataclass
import logging
import threading
from typing import Any, Callable, List, Optional

from pynput import mouse

from handlers.audio_monitor import AudioMonitor
from .hid_listener import HIDListener
from .thumb_wheel_filter import (
    DEFAULT_DEAD_ZONE_TICKS,
    DEFAULT_GESTURE_GAP_MS,
    DEFAULT_MAX_TICKS_PER_BATCH,
    ThumbWheelFilter,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config_service import ConfigService

logger = logging.getLogger(__name__)

# One native step of the TV backlight on the 0-100 scale the brightness
# plugins take. The Samsung and Bravia TVs expose a 0-50 range, so a step is
# 2. The brightness zone waits for this much before it sends a command
# (wh-brightness-wheel-one-step).
BRIGHTNESS_STEP = 2
# The most native steps one command may move the TV when the config key
# BRIGHTNESS_STEPS_PER_COMMAND is absent. One step per command was too
# slow for David (a Samsung round trip is about 400 ms, so one step per
# command is two to three steps per second); he chose two on 2026-09-14
# and then asked for the number to be configurable
# (wh-brightness-wheel-two-steps).
DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND = 2

BRIGHTNESS_ZONE = "brightness"
VOLUME_ZONE = "volume"


@dataclass
class _WheelRun:
    """Consecutive drained batches of one zone (wh-wheel-zone-timeout-jump)."""

    zone: str
    last_x: int
    ticks: int = 0


class MouseHandler:
    """Handles mouse movements, clicks, and thumb wheel events."""

    def __init__(self, loop: asyncio.AbstractEventLoop, config_service: "ConfigService", app: Any, audio_monitor: AudioMonitor, bravia_control: Any, software_dimmer: Any, event_bus: Any = None):
        self.loop = loop
        self.config_service = config_service
        self.app = app
        self.audio_monitor = audio_monitor
        self.event_bus = event_bus
        
        self.mouse_x = 0

        # HID event processing - HIDListener now does the batching
        self.hid_event_queue = asyncio.Queue(maxsize=50)
        self.hid_listener = HIDListener(
            self.loop, self.hid_event_queue, pointer_x=lambda: self.mouse_x
        )
        self.process_hid_task = None
        # The zone of the last wheel batch; None until the first batch.
        # process_hid_events resets the filter when this changes.
        self._last_wheel_zone: Optional[str] = None
        
        self.pynput_listener = None
        self._pynput_thread = None
        # wh-overlay-refresh-after-mouse: hears each click and wheel step on
        # the hook thread; None until ServiceManager connects the overlay.
        self.pointer_input_listener: Optional[Callable[[bool, bool], None]] = None

        self.brightness_accumulator = 0.0  # Accumulator for fractional brightness changes
        
        # Load configuration values at init time (fail-fast principle)
        self.brightness_increment = config_service.get("BRIGHTNESS_INCREMENT", 0.25)
        self.volume_increment = config_service.get("VOLUME_INCREMENT", 0.5)
        self.side_offset = config_service.get("SIDE_OFFSET", 10)
        # Native TV steps per brightness command, on the 0-100 scale the
        # plugins take. A value below 1 would clamp every command to nothing
        # and silently disable the brightness zone, so it is floored at 1.
        steps_per_command = max(1, int(config_service.get(
            "BRIGHTNESS_STEPS_PER_COMMAND", DEFAULT_BRIGHTNESS_STEPS_PER_COMMAND
        )))
        self.brightness_max_per_command = steps_per_command * BRIGHTNESS_STEP
        # wh-mouse-wheel-sensitivity: every drained batch passes through this
        # filter before a zone sees it (dead zone, gesture gap, per-batch
        # cap; see handlers/thumb_wheel_filter.py). Read at init for the same
        # fail-fast reason as the three values above.
        self.thumb_wheel_filter = ThumbWheelFilter(
            dead_zone_ticks=config_service.get(
                "THUMB_WHEEL_DEAD_ZONE_TICKS", DEFAULT_DEAD_ZONE_TICKS
            ),
            gesture_gap_ms=config_service.get(
                "THUMB_WHEEL_GESTURE_GAP_MS", DEFAULT_GESTURE_GAP_MS
            ),
            max_ticks_per_batch=config_service.get(
                "THUMB_WHEEL_MAX_TICKS_PER_BATCH", DEFAULT_MAX_TICKS_PER_BATCH
            ),
        )

        logger.debug("MouseHandler initialized for event-based brightness and volume control.")

    async def process_hid_events(self):
        """
        Process pre-batched thumb wheel events from HIDListener.

        :flow: High-Resolution Mouse Input
        :step: 3
        :consumes_from: High-Resolution Mouse Input
        :description: Routes pre-batched thumb wheel events to brightness or volume handlers
        :data_in: Pre-batched event dictionaries from hid_event_queue
        :data_out: Calls to _handle_brightness_zone_event or _handle_volume_zone_event
        :notes: Long-running asyncio task consuming hid_event_queue from step 2. Each event contains aggregated delta over 50ms window plus its arrival time ("at", time.monotonic()), ready for immediate processing. Each drained batch first passes through ThumbWheelFilter on its own, in arrival order (step 3.5, wh-mouse-wheel-sensitivity): ticks below the dead zone are held, a pause longer than the gesture gap between arrival times forgets them, and one batch passes at most the cap. The zone acts on the sum of what passed; a drain the filter reduces to zero reaches no zone. Batches are filtered one by one, not as their sum, because the zone handlers await their plugins and later batches queue behind that wait (wh-mouse-wheel-sensitivity.1.1). Routing logic (wh-wheel-zone-timeout-jump): the zone of a batch comes from its own "x" stamp, the pointer position HIDListener read when it sent the batch, so a batch that waits behind a slow TV or speaker keeps the zone it was rolled in; a batch with no stamp uses mouse_x at drain time. x < side_offset (left side of screen) is the brightness zone (step 4A); otherwise the volume zone (step 4B). The filter is reset before a batch whose zone differs from the previous batch's zone, also across drains, so ticks held in one zone never reach the other. Consecutive batches of one zone form a run. The runs are final before any await: each run, in arrival order, whose passed ticks sum to nonzero makes one handler call with that sum, so a drain with one zone makes one call. Mouse position tracked by on_move() listener. Uses asyncio queue.get() for non-blocking event reception.
        """
        logger.debug("Starting HID event processor for pre-batched events.")
        
        while True:
            try:
                # Wait for at least one event
                event = await self.hid_event_queue.get()
                events = [event]
                
                # Drain any other pending events immediately
                while not self.hid_event_queue.empty():
                    try:
                        events.append(self.hid_event_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                
                # Filter each drained batch on its own, in arrival order, and
                # act on the sum of what passed in each run of one zone.
                # Batches queue up here while a zone awaits its plugin, and
                # each one is 50 ms of real movement with its own cap
                # (wh-mouse-wheel-sensitivity.1.1) and its own zone
                # (wh-wheel-zone-timeout-jump). No await until the runs are
                # final.
                total_delta = 0
                runs: List[_WheelRun] = []
                for e in events:
                    if e and e.get("type") == "thumb_wheel":
                        delta = e.get("delta", 0)
                        total_delta += delta
                        if delta != 0:
                            x = e.get("x", self.mouse_x)
                            zone = BRIGHTNESS_ZONE if x < self.side_offset else VOLUME_ZONE
                            # Held ticks belong to the zone they were rolled
                            # in; the next batch must not release them into
                            # the other zone.
                            if self._last_wheel_zone not in (None, zone):
                                self.thumb_wheel_filter.reset()
                            self._last_wheel_zone = zone
                            if not runs or runs[-1].zone != zone:
                                runs.append(_WheelRun(zone, x))
                            run = runs[-1]
                            run.last_x = x
                            run.ticks += self.thumb_wheel_filter.filter_batch(
                                delta, at=e.get("at")
                            )
                    self.hid_event_queue.task_done()

                ticks_passed = sum(run.ticks for run in runs)
                if total_delta != 0 or ticks_passed != 0:
                    logger.debug(
                        f"Processed batch of {len(events)} events, "
                        f"total_delta={total_delta}, ticks_passed={ticks_passed}"
                    )
                # The routing decision reads what PASSED, never the raw sum:
                # per-batch capping and dead-zone release are not linear, so
                # raw deltas can cancel (+4, -2, -2) while the passed ticks do
                # not (3, -2, -2 = -1), and the reverse
                # (wh-mouse-wheel-sensitivity.1.2).
                for run in runs:
                    if run.ticks == 0:
                        # Held below the dead zone or cancelled; nothing for
                        # a zone to do.
                        continue
                    logger.debug(
                        f"Thumb wheel run: zone={run.zone}, x={run.last_x}, "
                        f"ticks={run.ticks}"
                    )
                    if run.zone == BRIGHTNESS_ZONE:
                        await self._handle_brightness_zone_event(run.ticks)
                    else:
                        await self._handle_volume_zone_event(run.ticks)

            except asyncio.CancelledError:
                logger.debug("HID event processor task cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in process_hid_events: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    def set_pointer_input_listener(
        self, listener: Optional[Callable[[bool, bool], None]],
    ) -> None:
        """Set the callable that hears each mouse click and wheel step.

        wh-overlay-refresh-after-mouse: ServiceManager connects the numbered
        overlay's ``note_pointer_input`` here, so the overlay can read its
        numbers again after the user clicks or scrolls. ``listener(click,
        injected)`` runs on the pynput hook thread; see ``on_click``.
        """
        self.pointer_input_listener = listener

    def on_click(
        self, x: int, y: int, button: Any, pressed: bool,
        injected: bool = False,
    ) -> None:
        """Mouse button press or release: tell the pointer-input listener.

        Runs inside pynput's WH_MOUSE_LL hook procedure, so the click waits
        for this method; see ``_report_pointer_input``. ``injected`` is True
        for an event a program sent (SendInput), as pynput 1.8 reports it.
        The coordinates are not used and are never logged.
        """
        self._report_pointer_input(True, injected)

    def on_scroll(
        self, x: int, y: int, dx: int, dy: int, injected: bool = False,
    ) -> None:
        """Mouse wheel step: tell the pointer-input listener.

        Runs inside the hook procedure like ``on_click``. The thumb wheel
        does not come here; it arrives as HID reports (``process_hid_events``).
        """
        self._report_pointer_input(False, injected)

    def _report_pointer_input(self, click: bool, injected: bool) -> None:
        """Hand one click or wheel event to the listener. Never raises, never logs.

        Windows waits for the hook procedure before it passes the event on,
        and pynput stops the listener when an exception leaves a callback
        (pynput/_util/__init__.py ``_emitter``). So this method writes no log
        line and lets nothing out, and the listener must do no more than
        read a flag and post one call to the Logic loop
        (wh-overlay-refresh-after-mouse, acceptance item 5).
        """
        try:
            listener = self.pointer_input_listener
            if listener is not None:
                listener(click, bool(injected))
        except Exception:  # noqa: BLE001 -- nothing may leave the hook
            pass

    def on_move(self, x: int, y: int) -> None:
        """Mouse movement event handler.
        
        :flow: High-Resolution Mouse Input
        :step: 1
        :produces_for: High-Resolution Mouse Input
        :description: Captures mouse position for brightness/volume zone routing and Atmos hover detection.
        :data_in: x, y coordinates from pynput mouse listener.
        :data_out: Updates self.mouse_x for zone detection, queues position for Atmos trigger check.
        :notes: Runs in pynput's background thread. Stores every move: a pointer that stops sends no
            further move, so a dropped last move would leave the wheel on the wrong zone
            (wh-wheel-zone-timeout-jump). HIDListener reads mouse_x when it sends a thumb wheel
            batch and stamps the batch with it. The HID processor (step 3) routes each batch to
            brightness (left) vs volume (right) by that stamp, and reads mouse_x itself only for
            a batch that carries no stamp.
        """
        self.mouse_x = x

    async def _adjust_brightness_staged(self, brightness_change_step: int):
        """:flow: High-Resolution Mouse Input
        :step: 4
        :description: Branch A: Publishes BrightnessAdjustCommand to EventBus for coordinated brightness control
        :data_in: brightness_change_step integer from thumb wheel delta
        :data_out: BrightnessAdjustCommand event on EventBus
        :notes: Brightness routing branch when mouse in left screen zone (x < side_offset). Publishes BrightnessAdjustCommand(delta=brightness_change_step) to EventBus. BrightnessCoordinator (subscriber) handles all staging logic: hardware-first (TV/monitor via plugins), cascade to software dimmers (overlay, gamma dimmer) on overflow. MouseHandler remains pure input router with no brightness implementation knowledge. This decoupling allows adding new brightness methods without modifying input handling.
        """
        try:
            from ..events import BrightnessAdjustCommand
            event = BrightnessAdjustCommand(delta=brightness_change_step)
            await self.event_bus.publish(event)
            logger.debug(f"Published BrightnessAdjustCommand(delta={brightness_change_step})")
        except Exception as e:
            logger.error(f"Error publishing BrightnessAdjustCommand: {e}")


    async def _handle_brightness_zone_event(self, delta: int):
        """:flow: High-Resolution Mouse Input
        :step: 4
        :description: Branch A: Processes brightness zone thumb wheel events
        :data_in: delta integer from thumb wheel
        :data_out: Calls _adjust_brightness_staged if threshold met
        :notes: Brightness routing branch when mouse in left screen zone. Accumulates fractional changes to support high-resolution wheels. Triggers actual adjustment (step 5) only when the accumulated change reaches one native TV step (BRIGHTNESS_STEP, 2 on the 0-100 scale), and sends at most BRIGHTNESS_STEPS_PER_COMMAND native steps per command (default 2; self.brightness_max_per_command on the 0-100 scale), discarding the excess, so the batches that queue behind a 450 ms TV round trip cannot move the TV several steps at once (wh-brightness-wheel-one-step).
        """
        """Handles HID events when the mouse is in the brightness control zone."""
        # Apply sensitivity scaling from config
        scaled_change = -delta * self.brightness_increment
        self.brightness_accumulator += scaled_change

        logger.debug(f"BRIGHTNESS zone: Delta={delta}, ScaledChange={scaled_change:.2f}, Accumulator={self.brightness_accumulator:.2f}")

        # Check if the accumulator has reached one native TV step. The Samsung
        # and Bravia TVs use a 0-50 range, so one native step is 2 on the
        # 0-100 scale the plugins take.
        if abs(self.brightness_accumulator) >= BRIGHTNESS_STEP:
            # Get the integer part for the action and keep the fractional part in the accumulator
            steps_to_take = int(self.brightness_accumulator)
            self.brightness_accumulator -= steps_to_take

            # At most self.brightness_max_per_command per command, and the
            # excess is DISCARDED, not carried. A TV round trip takes about 450 ms (read, write, read
            # back), so the batches that queue behind it can sum to 14 ticks;
            # sent as one command, the Samsung plugin moved the TV seven
            # native steps at once (wh-brightness-wheel-one-step). Carrying
            # the excess instead would keep the TV moving after the wheel
            # stopped. The result: brightness moves at most that many steps per
            # round trip while the wheel turns, whatever the speed of the roll.
            if abs(steps_to_take) > self.brightness_max_per_command:
                steps_to_take = (self.brightness_max_per_command if steps_to_take > 0
                                 else -self.brightness_max_per_command)

            if steps_to_take != 0:
                logger.debug(f"  -> Triggering brightness change of {steps_to_take} steps. Accumulator is now {self.brightness_accumulator:.2f}.")
                await self._adjust_brightness_staged(steps_to_take)


    async def _handle_volume_zone_event(self, delta: int):
        """:flow: High-Resolution Mouse Input
        :step: 4
        :description: Branch B: Publishes VolumeAdjustCommand to EventBus for plugin-based volume control (Sonos/System)
        :data_in: delta integer from thumb wheel (negative=up, positive=down)
        :data_out: VolumeAdjustCommand event on EventBus consumed by volume plugins
        :produces_for: High-Resolution Mouse Input
        :notes: Volume routing branch when mouse in right screen zone (x >= side_offset). Inverts thumb wheel delta (up=increase volume) and applies VOLUME_INCREMENT config multiplier. Publishes VolumeAdjustCommand to EventBus for consumption by active volume plugin - typically SonosPlugin (step 5, Sonos speaker volume via network) or SystemVolumePlugin (Windows Core Audio). Plugin-based architecture allows MouseHandler to remain hardware-agnostic - adding new volume targets requires only a new plugin, no input handling changes. See step 5 for Sonos implementation details.
        """
        volume_change_step = -delta # Invert delta for volume (thumb wheel up = volume up)
        actual_volume_change_for_sonos = volume_change_step * self.volume_increment
        
        logger.debug(f"VOLUME zone: Delta={delta}, VolumeChangeStep={volume_change_step}, SonosVolumeChange={actual_volume_change_for_sonos}")
        
        # Publish volume adjustment event for plugin system
        try:
            from ..events import VolumeAdjustCommand
            event = VolumeAdjustCommand(delta=actual_volume_change_for_sonos)
            await self.event_bus.publish(event)
            logger.debug(f"Published VolumeAdjustCommand(delta={actual_volume_change_for_sonos})")
        except Exception as e:
            logger.error(f"Error publishing VolumeAdjustCommand: {e}")




    def _pynput_mouse_listener_thread(self) -> None:
        logger.debug("Pynput mouse listener thread entering execution...")
        try:
            self.pynput_listener = mouse.Listener(on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll)
            self.pynput_listener.start()
            self.pynput_listener.join()
        except Exception as e:
            logger.error(f"Error in pynput mouse listener thread: {e}", exc_info=True)
            if self.loop and not self.loop.is_closed() and hasattr(self.app, 'error_event'):
                self.loop.call_soon_threadsafe(self.app.error_event.set)
        finally:
            logger.debug("Pynput mouse listener thread finished.")

    def start(self):
        """Starts all mouse and HID listeners and returns their tasks."""
        tasks = []
        self._pynput_thread = threading.Thread(target=self._pynput_mouse_listener_thread, daemon=True)
        self._pynput_thread.start()
        logger.info("Pynput mouse listener thread started.")

        self.hid_listener.start()
        logger.info("HID listener started.")

        self.process_hid_task = self.loop.create_task(self.process_hid_events())
        tasks.append(self.process_hid_task)
        logger.info("HID event processing task started.")

        logger.debug(f"MouseHandler.start returning {len(tasks)} tasks (queue-based HID processing with HID-side batching).")
        return tasks



    def stop_listeners(self) -> None:
        """Stop all mouse listeners (HID and pynput) gracefully.
        
        Joins pynput thread with 1s timeout, logs warning if thread doesn't stop.
        """
        logger.debug("Stopping listeners in MouseHandler...")
        try:
            if self.hid_listener:
                self.hid_listener.stop()
        except Exception as e:
            logger.error(f"Error stopping HID listener: {e}")

        try:
            if self.pynput_listener:
                logger.debug("Stopping pynput mouse listener...")
                self.pynput_listener.stop()
                if self._pynput_thread and self._pynput_thread.is_alive():
                    self._pynput_thread.join(timeout=1.0)
                    if self._pynput_thread.is_alive():
                        logger.warning("Pynput mouse listener thread did not stop in time.")
                self.pynput_listener = None
                self._pynput_thread = None
                logger.debug("Pynput mouse listener stopped.")
            else:
                logger.debug("Pynput mouse listener was not active or already stopped.")
        except Exception as e:
            logger.error(f"Error stopping pynput mouse listener: {e}")
