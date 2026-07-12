"""HID device listener for Logitech mouse thumb wheel events.

This module provides specialized hardware input handling for Logitech MX mouse
devices, specifically the thumb wheel functionality. It uses the Windows HID
API to directly monitor hardware events from specific device interfaces,
enabling precise control over volume and brightness through thumb wheel
interactions.

Key Classes:
  - HIDListener: Low-level HID event processor for Logitech devices.

Key Features:
  - Direct HID interface communication with Logitech MX mice
  - Thumb wheel event detection and processing
  - Multiple device interface support for device compatibility
  - Raw HID data parsing and event extraction
  - EventBus integration for decoupled event publishing
  - ConfigService integration for flexible device configuration
  - Device discovery and automatic interface selection

Hardware Support:
  - Logitech MX Master series mice (PIDs: 0xC52B, 0xC539, 0xB023)
  - Thumb wheel up/down direction detection
  - Raw delta value processing for proportional control

Typical Usage:
  from handlers.hid_listener import HIDListener
  from events import ThumbWheelEvent
  
  # Subscribe to thumb wheel events
  async def handle_thumb_wheel(event: ThumbWheelEvent):
      print(f"Thumb wheel moved: delta={event.delta}")
  
  event_bus.subscribe(ThumbWheelEvent, handle_thumb_wheel)
  
  # Create and start HID listener
  hid_listener = HIDListener(
      loop=event_loop,
      event_bus=event_bus,
      config_service=config_service
  )
  
  # Start listening in background thread
  hid_listener.start()
"""
# handlers/hid_listener.py

import asyncio
import logging
import struct
import threading
import time
from typing import Dict, Optional, Union, Any, Callable, List, Set

import pywinusb.hid as hid

logger = logging.getLogger(__name__)

# Constants based on the working test_thumb_wheel.py
LOGITECH_VID = 0x046D
LOGITECH_PIDS: Set[int] = {0xC52B, 0xC539, 0xB023, 0xC548}
TARGET_PAGE: Set[int] = {0x1302, 0x1303, 0x0F02} 
USAGE_ID_FOR_UP_DIRECTION = 0xFF00
USAGE_ID_FOR_DOWN_DIRECTION = 0x000
DELTA_BYTE_INDEX = 5
# --- End Constants ---

class HIDListener:
    """
    Listens for HID events from Logitech MX Mouse thumb wheel by iterating
    through potential device interfaces and using a raw data handler.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        event_queue: asyncio.Queue,
        target_vid: int = LOGITECH_VID,
        target_pids: Set[int] = LOGITECH_PIDS,
    ):
        self.loop = loop
        self.event_queue = event_queue
        self.target_vid = target_vid
        self.target_pids = target_pids

        self.opened_devices: List[hid.HidDevice] = []
        self.listener_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Batching infrastructure for high-frequency events
        self.delta_accumulator = 0
        self.last_batch_time = time.time()
        self.batch_interval = 0.05  # 50ms batching window
        self._batch_lock = threading.Lock()

        logger.info(
            f"HIDListener initialized for VID={target_vid:#06x}, PIDs={{{', '.join(f'{pid:#06x}' for pid in target_pids)}}}, with {self.batch_interval*1000:.0f}ms batching")

    def _safe_enqueue_event(self, event: Dict[str, Union[str, int]]):
        """Attempt to enqueue without allowing QueueFull to propagate to the loop handler.
        If full, evict one oldest item and retry once; otherwise drop silently.
        This function must run on the event loop thread; schedule it via call_soon_threadsafe.
        
        Args:
            event: Dictionary with 'type' and 'delta' keys
        """
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                _ = self.event_queue.get_nowait()
                self.event_queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self.event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("hid_event_queue saturated; dropping event")

    def _accumulate_and_maybe_send(self, delta: int):
        """Accumulate delta and send batched event if batch interval has elapsed."""
        current_time = time.time()
        should_send = False
        accumulated_delta = 0
        
        with self._batch_lock:
            self.delta_accumulator += delta
            
            # Check if batch interval has elapsed
            if current_time - self.last_batch_time >= self.batch_interval:
                accumulated_delta = self.delta_accumulator
                self.delta_accumulator = 0
                self.last_batch_time = current_time
                should_send = (accumulated_delta != 0)
        
        if should_send:
            event: Dict[str, Union[str, int]] = {
                "type": "thumb_wheel",
                "delta": accumulated_delta,
            }
            self.loop.call_soon_threadsafe(self._safe_enqueue_event, event)
            logger.debug(f"  >>> Queued batched thumb_wheel event: delta={accumulated_delta} (batched from multiple events)")

    def _find_and_open_devices(self) -> bool:
        """Find and open all matching Logitech HID device interfaces.
        
        Returns:
            bool: True if at least one device opened successfully
        """
        self.opened_devices = []
        try:
            all_hid_devices = hid.find_all_hid_devices()
            if not all_hid_devices:
                logger.warning("No HID devices found on the system.")
                return False

            logitech_devices_interfaces = [
                dev for dev in all_hid_devices
                if dev.vendor_id == self.target_vid and dev.product_id in self.target_pids
            ]

            if not logitech_devices_interfaces:
                logger.warning(
                    f"No Logitech devices found matching VID={self.target_vid:#06x} and PIDs={{{', '.join(f'{pid:#06x}' for pid in self.target_pids)}}}. "
                    "Ensure the mouse/receiver is connected."
                )
                return False

            opened_count = 0
            for i, device_interface in enumerate(logitech_devices_interfaces):
                try:
                    device_interface.open(shared=True)
                    device_interface.set_raw_data_handler(self._raw_data_event_handler)
                    self.opened_devices.append(device_interface)
                    opened_count += 1
                    logger.info(
                        f"Successfully opened and attached handler to device/interface: "
                        f"{device_interface.product_name} (VID={device_interface.vendor_id:#06x}, PID={device_interface.product_id:#06x}, Path: {device_interface.device_path})"
                    )
                except Exception as e:
                    logger.error(
                        f"Could not open or set handler for device/interface {device_interface.product_name} "
                        f"(Path: {device_interface.device_path}): {e}. This interface might be in use or inaccessible."
                    )

            if opened_count == 0:
                logger.error("No Logitech HID interfaces could be opened successfully.")
                return False
            return True
        except Exception as e:
            logger.error(f"Error during HID device discovery and opening: {e}")
            return False

    def _raw_data_event_handler(self, data: List[int]):
        arr = bytes(data)

        # Preliminary length check
        if len(arr) < (DELTA_BYTE_INDEX + 1): # Minimum length for our target report
            return

        try:
            """:flow: High-Resolution Mouse Input
            :step: 1
            :description: Parses raw HID report bytes to extract thumb wheel scroll delta
            :data_in: Byte array from HID device (raw USB report)
            :data_out: Parsed delta value (negative=up, positive=down)
            :notes: Low-level USB HID parsing. Extracts page (bytes 1-2), usage_id (bytes 3-4), and delta (byte at DELTA_BYTE_INDEX). Only processes reports matching TARGET_PAGE. Handles signed byte conversion (>127 becomes negative). Validates direction by matching usage_id against USAGE_ID_FOR_UP_DIRECTION or USAGE_ID_FOR_DOWN_DIRECTION. Filters out reports with delta=0 (no movement).
            """
            page = arr[1] | (arr[2] << 8)
            usage_id_from_report = arr[3] | (arr[4] << 8)

            # Only log raw data and further details if it matches our target page
            if page in TARGET_PAGE:
                raw_delta_byte = arr[DELTA_BYTE_INDEX]
                delta = raw_delta_byte - 256 if raw_delta_byte > 127 else raw_delta_byte
                logger.debug(f"Thumbwheel: page={page:#06x}, usage={usage_id_from_report:#06x}, raw_delta={raw_delta_byte}, delta={delta}")

                # For page 0x1302 (Bolt receiver variant), use delta sign directly
                # For other pages (0x1303, 0x0F02), use usage_id + delta validation
                action_taken = False
                if page in (0x1203, 0x1302):
                    # Bolt receiver: use delta sign directly (positive=down, negative=up)
                    # No inversion needed - matches other receiver variants
                    action_taken = (delta != 0)
                else:
                    # Original logic for other receiver variants
                    if usage_id_from_report == USAGE_ID_FOR_UP_DIRECTION:
                        if delta < 0:
                            action_taken = True
                    elif usage_id_from_report == USAGE_ID_FOR_DOWN_DIRECTION:
                        if delta > 0:
                            action_taken = True

                if action_taken and delta != 0:
                    """:flow: High-Resolution Mouse Input
                    :step: 2
                    :produces_for: High-Resolution Mouse Input
                    :description: Batches rapid thumb wheel deltas in 50ms windows to prevent event spam
                    :data_in: Parsed delta value from step 1
                    :data_out: Accumulated delta sent to hid_event_queue every 50ms
                    :notes: Event batching/throttling mechanism. Calls _accumulate_and_maybe_send(delta) which maintains running sum of deltas. Timer-based flushing sends accumulated value to hid_event_queue after 50ms window. This prevents overwhelming MouseHandler with hundreds of individual events during rapid thumb wheel scrolling. Queue consumed by mouse_handler.py's process_hid_events() in step 3.
                    """
                    # Accumulate delta for batching (prevents high-frequency event spam)
                    self._accumulate_and_maybe_send(delta)
                elif delta != 0 : # Matched page, but not the usage IDs or delta direction
                     logger.debug(f"  Matched page, but not usage/delta condition. UsageID={usage_id_from_report:#06x}, Delta={delta}")


        except IndexError:
            # This might still happen if a report is for TARGET_PAGE but unexpectedly short
            logger.warning(f"IndexError processing raw HID data for TARGET_PAGE (length {len(arr)}): {arr}")
        except struct.error as e:
            logger.error(f"Struct error processing raw HID data: {e}. Data: {arr}")
        except Exception as e:
            logger.error(f"Unexpected error in _raw_data_event_handler: {e}. Data: {arr}", exc_info=True)


    def _listener_thread_target(self):
        """:flow: High-Resolution Mouse Input
        :step: 0
        :description: Background thread monitors device connection health
        :data_in: HID device plug state from pywinusb
        :data_out: Thread termination on device unplug
        :notes: Device health monitoring thread. Checks device plug state every 200ms via is_plugged(). If all devices unplugged, logs warning and terminates monitoring (allows graceful recovery on device reconnect). Runs independently of event processing - raw data events handled by callbacks registered in _find_and_open_devices. This thread only monitors connection health.
        """
        if not self.opened_devices:
            logger.error("No HID devices were successfully opened. Listener thread exiting.")
            return

        logger.info(f"HID listener thread started, monitoring {len(self.opened_devices)} device interface(s).")
        try:
            while not self._stop_event.is_set():
                if not any(dev.is_plugged() for dev in self.opened_devices):
                    logger.warning("All monitored HID device interfaces appear to be unplugged. Stopping listener.")
                    break
                time.sleep(0.2)

        except Exception as e:
            logger.error(f"Unexpected error in HID listener thread: {e}")
        finally:
            logger.info("HID listener thread finished.")
            if not self._stop_event.is_set():
                logger.warning("HID listener thread stopped unexpectedly (e.g., all devices unplugged).")


    def start(self) -> bool:
        """:flow: High-Resolution Mouse Input
        :step: -1
        :description: Initialize HID device monitoring and start background thread
        :data_in: Device VID/PIDs from configuration
        :data_out: Registered raw data callbacks on HID device interfaces
        :notes: Entry point for HID event flow. Calls _find_and_open_devices to enumerate matching Logitech devices and register _raw_data_event_handler callbacks for each interface. Launches _listener_thread_target (step 0) for device health monitoring. If device opening fails, returns False and flow never begins. Once started, USB reports arrive asynchronously triggering step 1 (raw HID parsing).
        """
        if self.listener_thread and self.listener_thread.is_alive():
            logger.warning("HID listener thread is already running.")
            return True

        if not self._find_and_open_devices():
            logger.error("Failed to find and open any target HID devices. Listener not started.")
            return False

        self._stop_event.clear()
        self.listener_thread = threading.Thread(
            target=self._listener_thread_target,
            name="HIDListenerThread",
            daemon=True,
        )
        self.listener_thread.start()

        time.sleep(0.1)
        if not self.listener_thread.is_alive():
            logger.error(
                "HID listener thread failed to start or exited immediately after device open. Check logs."
            )
            self._close_all_devices()
            return False

        logger.info("HID listener monitoring thread successfully started.")
        return True

    def _close_all_devices(self):
        """Close all opened HID device interfaces and cleanup.
        
        Unregisters callbacks and closes device handles.
        """
        if not self.opened_devices:
            return

        logger.info(f"Closing {len(self.opened_devices)} HID device interface(s)...")
        for device in self.opened_devices:
            if device.is_opened():
                try:
                    device.set_raw_data_handler(None)
                    device.close()
                    logger.debug(f"Closed device: {device.device_path}")
                except Exception as e:
                    logger.error(f"Error closing device {device.device_path}: {e}")
        self.opened_devices = []
        logger.info("All tracked HID device interfaces closed.")


    def stop(self):
        """Stop HID listener thread and close all devices.
        
        Signals thread termination and waits up to 1s for graceful shutdown.
        """
        if self.listener_thread and self.listener_thread.is_alive():
            logger.info("Stopping HID listener thread...")
            self._stop_event.set()
            self.listener_thread.join(timeout=1.0)

            if self.listener_thread.is_alive():
                logger.warning("HID listener thread did not stop gracefully within the timeout.")
            self.listener_thread = None
        else:
            logger.info("HID listener thread not running or already stopped.")

        self._close_all_devices()
        logger.info("HID listener stop sequence completed.")