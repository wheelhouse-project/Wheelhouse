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
  - THUMB_WHEEL discovery: a read-only HID++ getFeature(0x2150) query finds
    the receiver slot and feature index whose reports are thumb wheel turns
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
USAGE_ID_FOR_UP_DIRECTION = 0xFF00
USAGE_ID_FOR_DOWN_DIRECTION = 0x000
DELTA_BYTE_INDEX = 5

# wh-thumb-wheel-not-detected: a thumb-wheel report's "page" (byte 1 | byte 2
# << 8) is the receiver slot of the mouse plus the THUMB_WHEEL feature index
# on that mouse, and both differ between machines (slot 3 on Ikon, slot 6 on
# mavencore). The listener asks the receiver for them with a read-only HID++
# 2.0 Root getFeature query instead of keeping a fixed list of pages.
HIDPP_SHORT_REPORT_ID = 0x10
HIDPP_LONG_REPORT_ID = 0x11
HIDPP_USAGE_PAGE = 0xFF00
HIDPP_SHORT_USAGE = 0x0001       # the short-report collection (mi_02 col01)
HIDPP_ROOT_FEATURE_INDEX = 0x00
HIDPP_RECEIVER_ERROR = 0x8F      # HID++ 1.0 error from the receiver
HIDPP_DEVICE_ERROR = 0xFF        # HID++ 2.0 error from the device
# Software ID in the low nibble of byte 3 of a request and of its answer.
# 0 marks a device notification and Logi Options+ uses 0xA.
HIDPP_SOFTWARE_ID = 0x0E
# Root function 0 (getFeature) in the high nibble, our software ID below it.
GET_FEATURE_FUNCTION_SWID = (0 << 4) | HIDPP_SOFTWARE_ID
THUMB_WHEEL_FEATURE_ID = 0x2150
RECEIVER_SLOTS = range(1, 7)
HIDPP_QUERY_TIMEOUT = 1.0        # seconds to wait for one slot's answer
DEVICE_CONNECTION_SUB_ID = 0x41  # receiver: a device connected or lost its link
LINK_NOT_ESTABLISHED = 0x40      # bit in byte 4 of the 0x41 notification
# Byte 2 of a HID++ report at or above this is a HID++ 1.0 register answer or
# an error, not a notification (HID++ 2.0 feature indexes stay below it).
HIDPP_FIRST_ANSWER_SUB_ID = 0x80
# A notification from a slot with no answer yet queries that slot again, at
# most once in this many seconds per slot.
UNANSWERED_SLOT_QUERY_INTERVAL = 5.0
# --- End Constants ---


class _PendingQuery:
    """One getFeature query waiting for its answer."""

    def __init__(self) -> None:
        self.answered = threading.Event()
        self.feature_index: Optional[int] = None


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
        # wh-mouse-wheel-sensitivity: the timer that flushes the ticks held
        # after a roll's last send. Loop-thread only: armed, replaced, and
        # cancelled from callbacks scheduled through call_soon_threadsafe.
        self._tail_flush_handle: Optional[asyncio.TimerHandle] = None

        # wh-thumb-wheel-not-detected: receiver slot -> THUMB_WHEEL feature
        # index, from the answers to our getFeature query. Written on the
        # pywinusb reader threads under _hidpp_lock; the hot path reads
        # _thumb_wheel_pages, an immutable set replaced whole on every change.
        # crewcut: keyed by slot only, so two receivers with a device on the
        # same slot share one entry; key it by receiver as well to remove this.
        self._hidpp_lock = threading.Lock()
        self._thumb_wheel_features: Dict[int, int] = {}
        self._thumb_wheel_pages: frozenset = frozenset()
        self._pending_queries: Dict[int, _PendingQuery] = {}
        # One query round at a time; rounds run on their own daemon threads.
        self._query_round_lock = threading.Lock()
        # Query after a notification from a slot with no answer yet, under
        # _hidpp_lock: slots that answered "no THUMB_WHEEL" (feature index 0;
        # cleared by a 0x41 link-up), slots whose notification or link-up
        # query is still running, slots that linked during that query, and
        # when each slot's last notification query started.
        self._no_thumb_wheel_slots: Set[int] = set()
        self._slot_queries_in_progress: Set[int] = set()
        self._slot_link_ups_pending: Set[int] = set()
        self._slot_query_started: Dict[int, float] = {}
        self._monotonic: Callable[[], float] = time.monotonic

        logger.info(
            f"HIDListener initialized for VID={target_vid:#06x}, PIDs={{{', '.join(f'{pid:#06x}' for pid in target_pids)}}}, with {self.batch_interval*1000:.0f}ms batching")

    def _safe_enqueue_event(self, event: Dict[str, Union[str, int, float]]):
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
        """Accumulate delta and send batched event if batch interval has elapsed.

        Runs on the HID callback thread. When the window has elapsed the sum
        goes out at once. Otherwise a flush for the rest of the window is
        armed on the event loop (wh-mouse-wheel-sensitivity): before that,
        the ticks after a roll's last send stayed in the accumulator until the
        next roll's first report, which then went out carrying them -- the
        end of one roll was lost and the first touch of the next one jumped.
        The docstring at step 2 below always described a timer flush; this is
        it.

        Accepted trade-off: the send branch does not cancel an armed flush
        (a TimerHandle may be cancelled only on the loop thread). A flush
        that fires after a normal send finds either nothing or the first ticks
        of the next window and sends them a little early; the batch is
        smaller, never lost or duplicated.
        """
        current_time = time.time()
        should_send = False
        accumulated_delta = 0
        window_remaining = None

        with self._batch_lock:
            self.delta_accumulator += delta

            # Check if batch interval has elapsed
            elapsed = current_time - self.last_batch_time
            if elapsed >= self.batch_interval:
                accumulated_delta = self.delta_accumulator
                self.delta_accumulator = 0
                self.last_batch_time = current_time
                should_send = (accumulated_delta != 0)
            else:
                window_remaining = self.batch_interval - elapsed

        if should_send:
            # "at" is the arrival stamp the consumer's filter measures the
            # gesture gap from, so a batch that waits in the queue behind a
            # slow zone action still reads as part of the roll
            # (wh-mouse-wheel-sensitivity.1.1). monotonic, like the filter.
            event: Dict[str, Union[str, int, float]] = {
                "type": "thumb_wheel",
                "delta": accumulated_delta,
                "at": time.monotonic(),
            }
            self.loop.call_soon_threadsafe(self._safe_enqueue_event, event)
            logger.debug(f"  >>> Queued batched thumb_wheel event: delta={accumulated_delta} (batched from multiple events)")
        elif window_remaining is not None:
            self._request_tail_flush(window_remaining)

    def _request_tail_flush(self, delay: float) -> None:
        """Ask the loop to arm the tail flush; HID thread side."""
        try:
            self.loop.call_soon_threadsafe(self._arm_tail_flush, delay)
        except RuntimeError:
            # The loop is closed: the Logic process is shutting down and the
            # HID thread outlived it by a moment. The held ticks go with the
            # process, which is what shutdown wants.
            logger.debug("Tail flush not armed: event loop closed")

    def _arm_tail_flush(self, delay: float) -> None:
        """Replace any armed flush with one for ``delay`` seconds; loop thread."""
        if self._tail_flush_handle is not None:
            self._tail_flush_handle.cancel()
        self._tail_flush_handle = self.loop.call_later(delay, self._flush_tail)

    def _flush_tail(self) -> None:
        """Send whatever the accumulator holds and start a new window; loop thread."""
        self._tail_flush_handle = None
        with self._batch_lock:
            accumulated_delta = self.delta_accumulator
            self.delta_accumulator = 0
            self.last_batch_time = time.time()
        if accumulated_delta != 0:
            self._safe_enqueue_event({
                "type": "thumb_wheel",
                "delta": accumulated_delta,
                "at": time.monotonic(),
            })
            logger.debug(f"  >>> Queued tail thumb_wheel event: delta={accumulated_delta} (window ended without a further report)")

    def _cancel_tail_flush(self) -> None:
        """Drop an armed flush; loop thread."""
        if self._tail_flush_handle is not None:
            self._tail_flush_handle.cancel()
            self._tail_flush_handle = None

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
            :notes: Low-level USB HID parsing. Extracts page (bytes 1-2: receiver slot | THUMB_WHEEL feature index << 8), usage_id (bytes 3-4), and delta (byte at DELTA_BYTE_INDEX). Only processes reports whose page is a slot and feature index that answered the getFeature(THUMB_WHEEL) query (_thumb_wheel_pages). Handles signed byte conversion (>127 becomes negative). Validates direction by matching usage_id against USAGE_ID_FOR_UP_DIRECTION or USAGE_ID_FOR_DOWN_DIRECTION, which also requires byte 3 = 0x00 (a device notification, not an answer to a request). Filters out reports with delta=0 (no movement). A HID++ notification from a slot with no answer yet starts a getFeature query for that slot on its own thread (_query_slot_after_notification), at most once per slot in 5 seconds.
            """
            if self._handle_hidpp_receiver_report(arr):
                return

            page = arr[1] | (arr[2] << 8)
            usage_id_from_report = arr[3] | (arr[4] << 8)

            # Only log raw data and further details if it matches an answered slot
            if page in self._thumb_wheel_pages:
                raw_delta_byte = arr[DELTA_BYTE_INDEX]
                delta = raw_delta_byte - 256 if raw_delta_byte > 127 else raw_delta_byte
                logger.debug(f"Thumbwheel: page={page:#06x}, usage={usage_id_from_report:#06x}, raw_delta={raw_delta_byte}, delta={delta}")

                # Bytes 4-5 hold the rotation as a big-endian int16, so the
                # high byte (in usage_id) must agree with the sign of the low
                # byte. This is the rule page 0x1303 (slot 3, Ikon) always used.
                action_taken = False
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
            else:
                # A notification from a slot with no answer yet asks it again.
                self._query_slot_after_notification(arr)


        except IndexError:
            # This might still happen if a report is for an answered slot but unexpectedly short
            logger.warning(f"IndexError processing raw HID data for a thumb wheel page (length {len(arr)}): {arr}")
        except struct.error as e:
            logger.error(f"Struct error processing raw HID data: {e}. Data: {arr}")
        except Exception as e:
            logger.error(f"Unexpected error in _raw_data_event_handler: {e}. Data: {arr}", exc_info=True)

    # --- THUMB_WHEEL discovery (wh-thumb-wheel-not-detected) ---------------

    def _handle_hidpp_receiver_report(self, arr: bytes) -> bool:
        """Handle an answer to our getFeature query or a receiver connection
        notification; reader thread. Returns True when the report was one of
        those, so it is never read as a wheel turn.
        """
        report_id, slot, sub_id = arr[0], arr[1], arr[2]
        if report_id not in (HIDPP_SHORT_REPORT_ID, HIDPP_LONG_REPORT_ID):
            return False

        if report_id == HIDPP_SHORT_REPORT_ID and sub_id == DEVICE_CONNECTION_SUB_ID:
            # 10 <slot> 41 <protocol> <flags> <wireless PID lo> <hi>
            link_up = not (arr[4] & LINK_NOT_ESTABLISHED)
            logger.debug(f"Receiver slot {slot}: device {'connected' if link_up else 'lost its link'}")
            if link_up and slot in RECEIVER_SLOTS and not self._stop_event.is_set():
                # A device linked on this slot: an earlier "no THUMB_WHEEL"
                # answer may belong to another device, so it no longer holds.
                with self._hidpp_lock:
                    self._no_thumb_wheel_slots.discard(slot)
                    if slot in self._slot_queries_in_progress:
                        # A round for this slot runs or waits for the round
                        # lock, and it may already have asked the device that
                        # left. That round starts one more when it ends; a
                        # burst of link-ups from a weak link starts no more.
                        self._slot_link_ups_pending.add(slot)
                        return True
                    self._slot_queries_in_progress.add(slot)
                if not self._start_thumb_wheel_query((slot,), owns_marks=True):
                    with self._hidpp_lock:
                        self._slot_queries_in_progress.discard(slot)
            return True

        # Answer: 11 <slot> 00 <function 0 | our swid> <feature index> ...
        if sub_id == HIDPP_ROOT_FEATURE_INDEX and arr[3] == GET_FEATURE_FUNCTION_SWID:
            self._record_thumb_wheel_answer(slot, arr[4])
            return True

        # Error: 10 <slot> 8F 00 <function 0 | our swid> <error> 00 (receiver),
        # or 11 <slot> FF 00 ... (device).
        if (sub_id in (HIDPP_RECEIVER_ERROR, HIDPP_DEVICE_ERROR)
                and arr[3] == HIDPP_ROOT_FEATURE_INDEX
                and arr[4] == GET_FEATURE_FUNCTION_SWID):
            logger.debug(f"Receiver slot {slot} did not answer the THUMB_WHEEL query: error {arr[5]:#04x}")
            with self._hidpp_lock:
                pending = self._pending_queries.get(slot)
            if pending is not None:
                pending.answered.set()
            return True

        return False

    def _record_thumb_wheel_answer(self, slot: int, feature_index: int) -> None:
        """Store one slot's answer and wake its waiting query; reader thread.

        Feature index 0 means the device on that slot has no THUMB_WHEEL; its
        notifications then query it no more until a 0x41 link-up. A lost link
        leaves the entry in place; the next connection asks again.
        """
        with self._hidpp_lock:
            if feature_index:
                self._thumb_wheel_features[slot] = feature_index
                self._no_thumb_wheel_slots.discard(slot)
            else:
                self._thumb_wheel_features.pop(slot, None)
                self._no_thumb_wheel_slots.add(slot)
            self._thumb_wheel_pages = frozenset(
                s | (i << 8) for s, i in self._thumb_wheel_features.items()
            )
            pending = self._pending_queries.get(slot)
        if pending is not None:
            pending.feature_index = feature_index
            pending.answered.set()
        if feature_index:
            logger.info(
                f"Thumb wheel found: receiver slot {slot} has THUMB_WHEEL at feature index "
                f"{feature_index:#04x} (page {slot | (feature_index << 8):#06x})")
        else:
            logger.debug(f"Receiver slot {slot}: device has no THUMB_WHEEL feature")

    @staticmethod
    def _is_hidpp_short_interface(device: Any) -> bool:
        """True for the receiver's HID++ short-report collection."""
        caps = getattr(device, "hid_caps", None)
        try:
            return caps.usage_page == HIDPP_USAGE_PAGE and caps.usage == HIDPP_SHORT_USAGE
        except AttributeError:
            return False

    def _query_slot_after_notification(self, arr: bytes) -> None:
        """Query a slot with no answer yet when it sends a HID++ notification;
        reader thread.

        A slot has no answer yet when its query timed out, failed, or got an
        error (the mouse was asleep or off at start). The query runs on its
        own daemon thread: this method never waits and holds no lock while
        anything is sent. At most one query starts per slot in
        UNANSWERED_SLOT_QUERY_INTERVAL seconds, and none while one is still
        running. A slot that answered "no THUMB_WHEEL" is asked again only
        after a 0x41 link-up. Answers and errors never get here:
        _handle_hidpp_receiver_report takes them first.
        """
        report_id, slot, sub_id, fn_swid = arr[0], arr[1], arr[2], arr[3]
        # A notification: 10/11 <slot> <feature index> <event | software ID 0>.
        if (report_id not in (HIDPP_SHORT_REPORT_ID, HIDPP_LONG_REPORT_ID)
                or slot not in RECEIVER_SLOTS
                or not 0 < sub_id < HIDPP_FIRST_ANSWER_SUB_ID
                or fn_swid & 0x0F):
            return
        if self._stop_event.is_set():
            return
        now = self._monotonic()
        with self._hidpp_lock:
            if (slot in self._thumb_wheel_features
                    or slot in self._no_thumb_wheel_slots
                    or slot in self._slot_queries_in_progress):
                return
            last = self._slot_query_started.get(slot)
            if last is not None and now - last < UNANSWERED_SLOT_QUERY_INTERVAL:
                return
            self._slot_query_started[slot] = now
            self._slot_queries_in_progress.add(slot)
        logger.debug(f"Receiver slot {slot} sent a notification before it answered: asking it again")
        if not self._start_thumb_wheel_query((slot,), from_notification=True):
            with self._hidpp_lock:
                self._slot_queries_in_progress.discard(slot)

    def _start_thumb_wheel_query(self, slots, from_notification: bool = False,
                                 owns_marks: bool = False) -> bool:
        """Run a query round on its own daemon thread, so neither start() nor
        a pywinusb reader thread waits for an answer or a slow write.
        Returns False when the thread did not start."""
        try:
            threading.Thread(
                target=self._query_thumb_wheel_slots,
                args=(tuple(slots), from_notification, owns_marks),
                name="HIDPPThumbWheelQuery",
                daemon=True,
            ).start()
            return True
        except RuntimeError as e:
            logger.error(f"Thumb wheel query not started: {e}")
            return False

    def _query_thumb_wheel_slots(self, slots, from_notification: bool = False,
                                 owns_marks: bool = False) -> None:
        """Ask each slot for its THUMB_WHEEL feature index; query thread.

        Sends only Root getFeature(0x2150): no write and no change to the
        reporting mode. Each slot gets HIDPP_QUERY_TIMEOUT seconds to answer.
        The answers arrive on the reader threads and are stored there.

        ``from_notification``: the round started from a notification
        (_query_slot_after_notification). It skips a slot that got an answer
        while the round waited for the round lock, and it ends the slot's
        "in progress" mark when it finishes.

        ``owns_marks``: a 0x41 link-up round set the "in progress" mark for
        its slot, so it ends the mark too. It asks the slot even when the slot
        answered, because a link-up can bring another device to the slot.

        A round that ends a mark starts one link-up round for each of its
        slots that linked while it ran, and passes the mark to that round.
        """
        slots = tuple(slots)
        try:
            with self._query_round_lock:
                asked = slots
                if from_notification:
                    with self._hidpp_lock:
                        asked = tuple(
                            s for s in slots
                            if s not in self._thumb_wheel_features
                            and s not in self._no_thumb_wheel_slots)
                    if not asked:
                        return
                interfaces = [d for d in list(self.opened_devices) if self._is_hidpp_short_interface(d)]
                found = 0
                for device in interfaces:
                    for slot in asked:
                        if self._stop_event.is_set():
                            logger.debug("Thumb wheel query ended: the listener is stopping")
                            return
                        if self._ask_thumb_wheel_feature_index(device, slot):
                            found += 1
                if not found:
                    logger.info(
                        f"Thumb wheel query: no receiver slot answered with a THUMB_WHEEL feature "
                        f"(slots {list(asked)}, {len(interfaces)} HID++ interface(s) asked)")
        finally:
            if from_notification or owns_marks:
                with self._hidpp_lock:
                    linked = ()
                    if not self._stop_event.is_set():
                        linked = tuple(s for s in slots if s in self._slot_link_ups_pending)
                    self._slot_link_ups_pending.difference_update(slots)
                    self._slot_queries_in_progress.difference_update(set(slots) - set(linked))
                for slot in linked:
                    if not self._start_thumb_wheel_query((slot,), owns_marks=True):
                        with self._hidpp_lock:
                            self._slot_queries_in_progress.discard(slot)

    def _ask_thumb_wheel_feature_index(self, device: Any, slot: int) -> Optional[int]:
        """Send one getFeature(0x2150) to ``slot`` and wait for the answer.

        Returns the feature index, 0 when the device has none, or None when
        the slot gave no answer within the time limit or the send failed.
        """
        pending = _PendingQuery()
        with self._hidpp_lock:
            self._pending_queries[slot] = pending
        try:
            request = [
                HIDPP_SHORT_REPORT_ID, slot, HIDPP_ROOT_FEATURE_INDEX, GET_FEATURE_FUNCTION_SWID,
                THUMB_WHEEL_FEATURE_ID >> 8, THUMB_WHEEL_FEATURE_ID & 0xFF, 0x00,
            ]
            request += [0x00] * (device.hid_caps.output_report_byte_length - len(request))
            device.send_output_report(request)
            pending.answered.wait(HIDPP_QUERY_TIMEOUT)
        except Exception as e:
            logger.debug(f"Thumb wheel query to receiver slot {slot} failed: {e}")
        finally:
            with self._hidpp_lock:
                if self._pending_queries.get(slot) is pending:
                    del self._pending_queries[slot]
        return pending.feature_index


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
        :notes: Entry point for HID event flow. Calls _find_and_open_devices to enumerate matching Logitech devices and register _raw_data_event_handler callbacks for each interface. Launches _listener_thread_target (step 0) for device health monitoring, then starts the getFeature(THUMB_WHEEL) query round on its own thread without waiting for it. If device opening fails, returns False and flow never begins. Once started, USB reports arrive asynchronously triggering step 1 (raw HID parsing).
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
        # Ask which slot holds a THUMB_WHEEL; returns at once (wh-thumb-wheel-not-detected).
        self._start_thumb_wheel_query(RECEIVER_SLOTS)
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
        try:
            self.loop.call_soon_threadsafe(self._cancel_tail_flush)
        except RuntimeError:
            pass  # Loop closed: no timer can fire any more.
        logger.info("HID listener stop sequence completed.")