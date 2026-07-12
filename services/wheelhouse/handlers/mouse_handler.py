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
import logging
import threading
import time
from typing import Any

from pynput import mouse

from handlers.audio_monitor import AudioMonitor
from .hid_listener import HIDListener
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config_service import ConfigService

logger = logging.getLogger(__name__)

class MouseHandler:
    """Handles mouse movements, clicks, and thumb wheel events."""

    def __init__(self, loop: asyncio.AbstractEventLoop, config_service: "ConfigService", app: Any, audio_monitor: AudioMonitor, bravia_control: Any, software_dimmer: Any, event_bus: Any = None):
        self.loop = loop
        self.config_service = config_service
        self.app = app
        self.audio_monitor = audio_monitor
        self.event_bus = event_bus
        
        self.mouse_x = 0
        self.last_mouse_event = 0.0
        self.debounce_delay = 0.1

        # HID event processing - HIDListener now does the batching
        self.hid_event_queue = asyncio.Queue(maxsize=50)
        self.hid_listener = HIDListener(self.loop, self.hid_event_queue)
        self.process_hid_task = None
        
        self.pynput_listener = None
        self._pynput_thread = None

        self.brightness_accumulator = 0.0  # Accumulator for fractional brightness changes
        
        # Load configuration values at init time (fail-fast principle)
        self.brightness_increment = config_service.get("BRIGHTNESS_INCREMENT", 0.25)
        self.volume_increment = config_service.get("VOLUME_INCREMENT", 0.5)
        self.side_offset = config_service.get("SIDE_OFFSET", 10)
        
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
        :notes: Long-running asyncio task consuming hid_event_queue from step 2. Each event contains aggregated delta over 50ms window, ready for immediate processing. Routing logic: if mouse_x < side_offset (left side of screen), routes to brightness handler (step 4A). Otherwise routes to volume handler (step 4B). Mouse position tracked by on_move() listener. Uses asyncio queue.get() for non-blocking event reception.
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
                
                # Aggregate deltas from all drained events
                total_delta = 0
                for e in events:
                    if e and e.get("type") == "thumb_wheel":
                        total_delta += e.get("delta", 0)
                    self.hid_event_queue.task_done()
                
                if total_delta != 0:
                    logger.debug(f"Processed batch of {len(events)} events, total_delta={total_delta}")
                    if self.mouse_x < self.side_offset:
                        await self._handle_brightness_zone_event(total_delta)
                    else:
                        await self._handle_volume_zone_event(total_delta)
                    
            except asyncio.CancelledError:
                logger.debug("HID event processor task cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in process_hid_events: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    def on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        """Mouse click event handler (currently no-op).
        
        Reserved for future mouse click-based brightness/volume controls.
        """
        pass

    def on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        """Mouse scroll wheel event handler (currently no-op).
        
        Scroll wheel functionality delegated to HID thumb wheel processing.
        """
        pass

    def on_move(self, x: int, y: int) -> None:
        """Mouse movement event handler.
        
        :flow: High-Resolution Mouse Input
        :step: 1
        :produces_for: High-Resolution Mouse Input
        :description: Captures mouse position for brightness/volume zone routing and Atmos hover detection.
        :data_in: x, y coordinates from pynput mouse listener.
        :data_out: Updates self.mouse_x for zone detection, queues position for Atmos trigger check.
        :notes: Runs in pynput's background thread. Debounces updates (0.1s min interval). Mouse x
            position used by HID processor (step 3) to route thumb wheel events to brightness
            (left) vs volume (right).
        """
        try:
            current_time = time.time()
            if current_time - self.last_mouse_event > self.debounce_delay:
                self.last_mouse_event = current_time
                self.mouse_x = x
        except Exception as e:
            logger.error(f"Error in on_move: {e}")

    async def _adjust_brightness_staged(self, brightness_change_step: int):
        """:flow: High-Resolution Mouse Input
        :step: 4
        :description: Branch A: Publishes BrightnessAdjustCommand to EventBus for coordinated brightness control
        :data_in: brightness_change_step integer from thumb wheel delta
        :data_out: BrightnessAdjustCommand event on EventBus
        :notes: Brightness routing branch when mouse in left screen zone (x < side_offset). Publishes BrightnessAdjustCommand(delta=brightness_change_step) to EventBus. BrightnessCoordinator (subscriber) handles all staging logic: hardware-first (TV/monitor via plugins), cascade to software dimmers (f.lux, overlay) on overflow. MouseHandler remains pure input router with no brightness implementation knowledge. This decoupling allows adding new brightness methods without modifying input handling.
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
        :notes: Brightness routing branch when mouse in left screen zone. Accumulates fractional changes to support high-resolution wheels. Triggers actual adjustment (step 5) only when accumulated change exceeds threshold (±2.0).
        """
        """Handles HID events when the mouse is in the brightness control zone."""
        # Apply sensitivity scaling from config
        scaled_change = -delta * self.brightness_increment
        self.brightness_accumulator += scaled_change

        logger.debug(f"BRIGHTNESS zone: Delta={delta}, ScaledChange={scaled_change:.2f}, Accumulator={self.brightness_accumulator:.2f}")

        # Check if the accumulator has reached the minimum Bravia step (±2.0)
        # Bravia hardware uses 0-50 range, so minimum meaningful change on 0-100 scale is 2
        if abs(self.brightness_accumulator) >= 2.0:
            # Get the integer part for the action and keep the fractional part in the accumulator
            steps_to_take = int(self.brightness_accumulator)
            self.brightness_accumulator -= steps_to_take

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
        
        logger.debug(f"VOLUME zone (X={self.mouse_x}): Delta={delta}, VolumeChangeStep={volume_change_step}, SonosVolumeChange={actual_volume_change_for_sonos}")
        
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
