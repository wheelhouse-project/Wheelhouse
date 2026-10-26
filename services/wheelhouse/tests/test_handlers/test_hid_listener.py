"""Tests for HIDListener handler.

Tests Logitech HID device event processing including:
- Raw data parsing (_raw_data_event_handler)
- Event batching (_accumulate_and_maybe_send)
- Queue management (_safe_enqueue_event)
- Device lifecycle (start/stop)
- Adversarial: short data, zero deltas, queue full
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def event_loop_for_hid():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def hid_event_queue(event_loop_for_hid):
    return asyncio.Queue(maxsize=50)


@pytest.fixture
def hid_listener(event_loop_for_hid, hid_event_queue):
    """HIDListener with mocked pywinusb."""
    with patch("handlers.hid_listener.hid"):
        from handlers.hid_listener import HIDListener
        listener = HIDListener(
            loop=event_loop_for_hid,
            event_queue=hid_event_queue,
        )
        yield listener


# Wheelhouse's own HID++ software ID, in the low nibble of byte 3 of its
# Root getFeature request and of the answer (function 0 in the high nibble).
# Logi Options+ uses 0xA; 0 marks a device notification.
WHEELHOUSE_SWID = 0x0E


def _answer_thumb_wheel_query(listener, slot, feature_index):
    """Deliver the receiver's answer to Wheelhouse's own getFeature(0x2150)
    query: slot ``slot`` has THUMB_WHEEL at ``feature_index``. Reports from
    that slot and feature index are thumb-wheel notifications from then on
    (wh-thumb-wheel-not-detected)."""
    listener._raw_data_event_handler(
        [0x11, slot, 0x00, WHEELHOUSE_SWID, feature_index, 0x00, 0x01] + [0x00] * 13
    )


# ===========================================================================
# Initialization
# ===========================================================================

class TestInitialization:
    """Test HIDListener construction."""

    def test_default_vid_and_pids(self, hid_listener):
        """Default targets Logitech VID and known PIDs."""
        assert hid_listener.target_vid == 0x046D
        assert 0xC52B in hid_listener.target_pids
        assert 0xC539 in hid_listener.target_pids
        assert 0xB023 in hid_listener.target_pids

    def test_custom_vid_and_pids(self, event_loop_for_hid, hid_event_queue):
        """Can override VID and PIDs."""
        with patch("handlers.hid_listener.hid"):
            from handlers.hid_listener import HIDListener
            listener = HIDListener(
                loop=event_loop_for_hid,
                event_queue=hid_event_queue,
                target_vid=0x1234,
                target_pids={0xABCD},
            )
            assert listener.target_vid == 0x1234
            assert listener.target_pids == {0xABCD}

    def test_initial_batch_state(self, hid_listener):
        """Batching accumulator starts at zero."""
        assert hid_listener.delta_accumulator == 0
        assert hid_listener.batch_interval == 0.05


# ===========================================================================
# _raw_data_event_handler parsing
# ===========================================================================

class TestRawDataParsing:
    """Test HID report parsing logic."""

    def test_short_data_ignored(self, hid_listener):
        """Reports shorter than minimum length are silently dropped."""
        hid_listener._raw_data_event_handler([0x00, 0x02, 0x13])
        # Should not crash

    def test_non_target_page_ignored(self, hid_listener):
        """Reports for non-target pages are silently ignored."""
        # Page 0x0000 - not a target page
        data = [0x00, 0x00, 0x00, 0x00, 0xFF, 0x05]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_target_page_1302_positive_delta(self, hid_listener):
        """Page 0x1302 (Bolt receiver) with positive delta dispatches event."""
        # Page = 0x1302, usage_id = 0x0000, delta = 5
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 5]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(5)

    def test_target_page_1302_negative_delta(self, hid_listener):
        """Page 0x1302 (Bolt receiver) with negative delta (>127) dispatches event."""
        # delta byte = 250 -> signed = 250 - 256 = -6. Byte 4 is the high
        # byte of the int16 rotation, 0xFF for a negative one: one direction
        # rule now serves every answered slot (wh-thumb-wheel-not-detected).
        data = [0x00, 0x02, 0x13, 0x00, 0xFF, 250]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-6)

    def test_target_page_1302_zero_delta_skipped(self, hid_listener):
        """Page 0x1302 with delta=0 does not dispatch event."""
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 0]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_target_page_1303_up_direction(self, hid_listener):
        """Page 0x1303 with UP usage_id and negative delta dispatches."""
        # Page = 0x1303, usage_id = 0xFF00 (UP), delta = -3
        data = [0x00, 0x03, 0x13, 0x00, 0xFF, 253]  # 253 - 256 = -3
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x03, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-3)

    def test_target_page_1303_down_direction(self, hid_listener):
        """Page 0x1303 with DOWN usage_id and positive delta dispatches."""
        # Page = 0x1303, usage_id = 0x0000 (DOWN), delta = 4
        data = [0x00, 0x03, 0x13, 0x00, 0x00, 4]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x03, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(4)

    def test_target_page_1303_wrong_direction_skipped(self, hid_listener):
        """Page 0x1303 with UP usage_id but positive delta is skipped."""
        # usage_id = 0xFF00 (UP) but delta = +3 (wrong direction)
        data = [0x00, 0x03, 0x13, 0x00, 0xFF, 3]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x03, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_target_page_0F02_processes(self, hid_listener):
        """Page 0x0F02 is in TARGET_PAGE and processes correctly."""
        # Page = 0x0F02, usage_id = 0x0000 (DOWN), delta = 2
        data = [0x00, 0x02, 0x0F, 0x00, 0x00, 2]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x0F)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(2)


# ===========================================================================
# _accumulate_and_maybe_send batching
# ===========================================================================

class TestBatching:
    """Test event batching/throttling logic."""

    def test_accumulates_deltas_within_window(self, hid_listener):
        """Multiple deltas within batch window are accumulated."""
        hid_listener.last_batch_time = time.time()

        hid_listener._accumulate_and_maybe_send(3)
        hid_listener._accumulate_and_maybe_send(5)

        # Within batch window - shouldn't have sent yet
        assert hid_listener.delta_accumulator == 8

    def test_sends_after_batch_interval(self, hid_listener):
        """Accumulated delta sent after batch interval elapses."""
        # Set last batch time in the past
        hid_listener.last_batch_time = time.time() - 1.0
        hid_listener.delta_accumulator = 10

        hid_listener._accumulate_and_maybe_send(5)

        # Should have reset accumulator
        assert hid_listener.delta_accumulator == 0

    def test_zero_accumulated_delta_not_sent(self, hid_listener):
        """Net-zero accumulation doesn't enqueue an event."""
        hid_listener.last_batch_time = time.time() - 1.0
        hid_listener.delta_accumulator = -5

        # Add +5 to make net zero
        mock_loop = Mock()
        hid_listener.loop = mock_loop

        hid_listener._accumulate_and_maybe_send(5)
        mock_loop.call_soon_threadsafe.assert_not_called()

    def test_batch_calls_loop_call_soon_threadsafe(self, hid_listener):
        """Batched events use call_soon_threadsafe for thread safety."""
        hid_listener.last_batch_time = time.time() - 1.0
        hid_listener.delta_accumulator = 3

        mock_loop = Mock()
        hid_listener.loop = mock_loop

        hid_listener._accumulate_and_maybe_send(2)
        mock_loop.call_soon_threadsafe.assert_called_once()

    def test_batched_event_carries_its_arrival_time(self, hid_listener):
        """Each 50 ms batch is stamped with time.monotonic() when it is
        queued, so the consumer can tell a queue wait from a pause in the
        roll (wh-mouse-wheel-sensitivity.1.1)."""
        hid_listener.last_batch_time = time.time() - 1.0
        mock_loop = Mock()
        hid_listener.loop = mock_loop
        before = time.monotonic()

        hid_listener._accumulate_and_maybe_send(2)

        after = time.monotonic()
        _callback, event = mock_loop.call_soon_threadsafe.call_args[0]
        assert (event["type"], event["delta"]) == ("thumb_wheel", 2)
        assert before <= event["at"] <= after


# ===========================================================================
# Tail flush (wh-mouse-wheel-sensitivity)
# ===========================================================================

class TestTailFlush:
    """Ticks that arrive after the last send of a roll must not wait for the
    next roll. The listener arms a timer for the rest of the 50 ms window and
    flushes on the event loop thread."""

    def test_within_window_arms_a_tail_flush(self, hid_listener):
        hid_listener.last_batch_time = time.time()
        mock_loop = Mock()
        hid_listener.loop = mock_loop

        hid_listener._accumulate_and_maybe_send(2)

        mock_loop.call_soon_threadsafe.assert_called_once()
        callback, delay = mock_loop.call_soon_threadsafe.call_args[0]
        assert callback == hid_listener._arm_tail_flush
        assert 0.0 <= delay <= hid_listener.batch_interval

    def test_send_branch_does_not_arm_a_tail_flush(self, hid_listener):
        """A batch that goes out at once leaves nothing to flush."""
        hid_listener.last_batch_time = time.time() - 1.0
        mock_loop = Mock()
        hid_listener.loop = mock_loop

        hid_listener._accumulate_and_maybe_send(2)

        callbacks = [c[0][0] for c in mock_loop.call_soon_threadsafe.call_args_list]
        assert hid_listener._arm_tail_flush not in callbacks

    def test_flush_tail_sends_held_delta(self, hid_listener, hid_event_queue):
        hid_listener.delta_accumulator = 4

        hid_listener._flush_tail()

        assert not hid_event_queue.empty(), "the held delta never reached the queue"
        event = hid_event_queue.get_nowait()
        assert (event["type"], event["delta"]) == ("thumb_wheel", 4)
        assert hid_listener.delta_accumulator == 0

    def test_tail_event_carries_its_arrival_time(self, hid_listener, hid_event_queue):
        """The consumer measures the gesture gap from this stamp, not from
        when it gets round to the batch (wh-mouse-wheel-sensitivity.1.1)."""
        hid_listener.delta_accumulator = 4
        before = time.monotonic()

        hid_listener._flush_tail()

        after = time.monotonic()
        event = hid_event_queue.get_nowait()
        assert before <= event["at"] <= after

    def test_flush_tail_with_nothing_held_sends_nothing(self, hid_listener, hid_event_queue):
        hid_listener.delta_accumulator = 0

        hid_listener._flush_tail()

        assert hid_event_queue.empty()

    def test_flush_tail_starts_a_new_window(self, hid_listener, hid_event_queue):
        """The tick after a flush accumulates instead of going out alone."""
        hid_listener.last_batch_time = time.time() - 1.0
        hid_listener.delta_accumulator = 2
        hid_listener._flush_tail()
        hid_event_queue.get_nowait()

        hid_listener._accumulate_and_maybe_send(1)

        assert hid_listener.delta_accumulator == 1
        assert hid_event_queue.empty()

    def test_arm_replaces_an_earlier_timer(self, hid_listener):
        hid_listener._arm_tail_flush(5.0)
        first = hid_listener._tail_flush_handle

        hid_listener._arm_tail_flush(5.0)

        assert first.cancelled()
        assert hid_listener._tail_flush_handle is not first
        hid_listener._tail_flush_handle.cancel()

    def test_tail_reaches_queue_without_a_following_report(
        self, hid_listener, hid_event_queue, event_loop_for_hid
    ):
        """End-to-end on a real loop: two ticks, then silence, then the batch."""
        hid_listener.last_batch_time = time.time()

        hid_listener._accumulate_and_maybe_send(2)
        event_loop_for_hid.run_until_complete(asyncio.sleep(0.15))

        assert not hid_event_queue.empty(), "the tail batch never reached the queue"
        event = hid_event_queue.get_nowait()
        assert (event["type"], event["delta"]) == ("thumb_wheel", 2)
        assert hid_listener.delta_accumulator == 0

    def test_closed_loop_does_not_raise(self, hid_listener):
        """The HID thread can outlive the loop by a moment at shutdown."""
        hid_listener.last_batch_time = time.time()
        mock_loop = Mock()
        mock_loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
        hid_listener.loop = mock_loop

        hid_listener._accumulate_and_maybe_send(1)

        assert hid_listener.delta_accumulator == 1

    def test_stop_cancels_an_armed_flush(self, hid_listener, event_loop_for_hid):
        hid_listener._arm_tail_flush(5.0)
        handle = hid_listener._tail_flush_handle

        hid_listener.stop()
        event_loop_for_hid.run_until_complete(asyncio.sleep(0))

        assert handle.cancelled()
        assert hid_listener._tail_flush_handle is None


# ===========================================================================
# _safe_enqueue_event
# ===========================================================================

class TestSafeEnqueue:
    """Test queue management with overflow handling."""

    def test_normal_enqueue(self, hid_listener, hid_event_queue):
        """Events enqueued when queue has space."""
        event = {"type": "thumb_wheel", "delta": 5}
        hid_listener._safe_enqueue_event(event)
        assert not hid_event_queue.empty()

    def test_queue_full_evicts_oldest(self, event_loop_for_hid):
        """Full queue evicts oldest item to make room."""
        small_queue = asyncio.Queue(maxsize=2)
        small_queue.put_nowait({"type": "thumb_wheel", "delta": 1})
        small_queue.put_nowait({"type": "thumb_wheel", "delta": 2})

        with patch("handlers.hid_listener.hid"):
            from handlers.hid_listener import HIDListener
            listener = HIDListener(event_loop_for_hid, small_queue)

        new_event = {"type": "thumb_wheel", "delta": 3}
        listener._safe_enqueue_event(new_event)

        # Should have evicted oldest (delta=1) and added new (delta=3)
        assert small_queue.qsize() == 2


# ===========================================================================
# start / stop lifecycle
# ===========================================================================

class TestLifecycle:
    """Test device lifecycle management."""

    def test_start_returns_false_when_no_devices(self, hid_listener):
        """start() returns False when no matching HID devices found."""
        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = []
            result = hid_listener.start()
            assert result is False

    def test_start_already_running_returns_true(self, hid_listener):
        """start() returns True when thread already running."""
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        hid_listener.listener_thread = mock_thread

        result = hid_listener.start()
        assert result is True

    def test_stop_when_not_running(self, hid_listener):
        """stop() when no thread is running doesn't crash."""
        hid_listener.listener_thread = None
        hid_listener.stop()  # Should not raise

    def test_stop_sets_stop_event(self, hid_listener):
        """stop() signals the stop event to thread."""
        mock_thread = Mock()
        mock_thread.is_alive.return_value = True
        hid_listener.listener_thread = mock_thread

        hid_listener.stop()
        assert hid_listener._stop_event.is_set()
        mock_thread.join.assert_called_once_with(timeout=1.0)

    def test_close_all_devices(self, hid_listener):
        """_close_all_devices closes and clears all opened devices."""
        mock_dev1 = MagicMock()
        mock_dev1.is_opened.return_value = True
        mock_dev2 = MagicMock()
        mock_dev2.is_opened.return_value = True

        hid_listener.opened_devices = [mock_dev1, mock_dev2]
        hid_listener._close_all_devices()

        mock_dev1.set_raw_data_handler.assert_called_with(None)
        mock_dev1.close.assert_called_once()
        mock_dev2.close.assert_called_once()
        assert hid_listener.opened_devices == []

    def test_close_all_devices_handles_close_error(self, hid_listener):
        """Device close errors are logged but don't crash."""
        mock_dev = MagicMock()
        mock_dev.is_opened.return_value = True
        mock_dev.close.side_effect = Exception("Device error")

        hid_listener.opened_devices = [mock_dev]
        hid_listener._close_all_devices()  # Should not raise
        assert hid_listener.opened_devices == []


# ===========================================================================
# _find_and_open_devices
# ===========================================================================

class TestFindAndOpenDevices:
    """Test HID device discovery and opening."""

    def test_finds_matching_devices(self, hid_listener):
        """Opens devices matching target VID and PIDs."""
        mock_dev = MagicMock()
        mock_dev.vendor_id = 0x046D
        mock_dev.product_id = 0xC52B
        mock_dev.product_name = "MX Master"
        mock_dev.device_path = "\\\\?\\hid#123"

        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = [mock_dev]
            result = hid_listener._find_and_open_devices()

        assert result is True
        mock_dev.open.assert_called_once_with(shared=True)
        mock_dev.set_raw_data_handler.assert_called_once()

    def test_ignores_non_matching_devices(self, hid_listener):
        """Devices with wrong VID are ignored."""
        mock_dev = MagicMock()
        mock_dev.vendor_id = 0x1234  # Not Logitech
        mock_dev.product_id = 0xC52B

        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = [mock_dev]
            result = hid_listener._find_and_open_devices()

        assert result is False

    def test_handles_open_failure(self, hid_listener):
        """Continues if one device interface fails to open."""
        mock_dev1 = MagicMock()
        mock_dev1.vendor_id = 0x046D
        mock_dev1.product_id = 0xC52B
        mock_dev1.product_name = "MX Master"
        mock_dev1.device_path = "path1"
        mock_dev1.open.side_effect = Exception("Access denied")

        mock_dev2 = MagicMock()
        mock_dev2.vendor_id = 0x046D
        mock_dev2.product_id = 0xC52B
        mock_dev2.product_name = "MX Master"
        mock_dev2.device_path = "path2"

        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = [mock_dev1, mock_dev2]
            result = hid_listener._find_and_open_devices()

        # At least one opened
        assert result is True

    def test_returns_false_when_all_open_fail(self, hid_listener):
        """Returns False when all device interfaces fail to open."""
        mock_dev = MagicMock()
        mock_dev.vendor_id = 0x046D
        mock_dev.product_id = 0xC52B
        mock_dev.product_name = "MX Master"
        mock_dev.device_path = "path1"
        mock_dev.open.side_effect = Exception("Access denied")

        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = [mock_dev]
            result = hid_listener._find_and_open_devices()

        assert result is False


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:
    """Adversarial tests for robustness."""

    def test_raw_data_handler_with_index_error(self, hid_listener):
        """IndexError from short target page report is caught."""
        # Page = 0x1302 but data too short for delta byte
        data = [0x00, 0x02, 0x13, 0x00, 0x00]
        hid_listener._raw_data_event_handler(data)  # Should not raise

    def test_raw_data_handler_with_empty_data(self, hid_listener):
        """Empty data list is handled gracefully."""
        hid_listener._raw_data_event_handler([])  # Should not raise

    def test_signed_byte_boundary_127(self, hid_listener):
        """Delta byte 127 is positive (largest positive signed byte)."""
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 127]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(127)

    def test_signed_byte_boundary_128(self, hid_listener):
        """Delta byte 128 is negative (-128, smallest negative signed byte)."""
        # High byte 0xFF: int16 rotation -128 (wh-thumb-wheel-not-detected).
        data = [0x00, 0x02, 0x13, 0x00, 0xFF, 128]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-128)

    def test_signed_byte_boundary_255(self, hid_listener):
        """Delta byte 255 is -1."""
        # High byte 0xFF: int16 rotation -1 (wh-thumb-wheel-not-detected).
        data = [0x00, 0x02, 0x13, 0x00, 0xFF, 255]
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-1)

    def test_concurrent_batch_accumulation(self, hid_listener):
        """Thread-safe batch accumulation under concurrent access."""
        hid_listener.last_batch_time = time.time()  # Fresh window

        errors = []

        def add_deltas():
            try:
                for _ in range(100):
                    hid_listener._accumulate_and_maybe_send(1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_deltas) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ===========================================================================
# THUMB_WHEEL discovery by HID++ getFeature (wh-thumb-wheel-not-detected)
# ===========================================================================

CAPTURE = Path(__file__).resolve().parents[1] / "fixtures" / "thumb_wheel_mavencore_capture.txt"


def _capture_reports():
    """(interface short name, report bytes) for every report captured on
    mavencore while David turned the MX Master 4 thumb wheel (Bolt receiver
    0xC548, mouse on slot 6, THUMB_WHEEL at feature index 0x13): up for
    10 s, then down for 10 s."""
    rows = []
    for line in CAPTURE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        rows.append((parts[1], [int(b, 16) for b in parts[4:]]))
    return rows


def _long_reports():
    """Every report on the HID++ long collection (mi_02 col02), in order."""
    return [r for iface, r in _capture_reports() if iface.endswith("mi_02&col02")]


def _is_notification(report):
    """A THUMB_WHEEL event from slot 6: 11 06 13 00 <rotation int16 BE> ..."""
    return report[:4] == [0x11, 0x06, 0x13, 0x00]


def _rotation(report):
    return int.from_bytes(bytes(report[4:6]), "big", signed=True)


def _expected_deltas():
    return [_rotation(r) for r in _long_reports() if _is_notification(r) and _rotation(r) != 0]


def _deltas_from(listener, reports):
    listener._accumulate_and_maybe_send = Mock()
    for report in reports:
        listener._raw_data_event_handler(report)
    return [c.args[0] for c in listener._accumulate_and_maybe_send.call_args_list]


def _get_feature_request(slot):
    """The one HID++ request Wheelhouse may send: Root (index 0x00) function 0
    getFeature(0x2150 THUMB_WHEEL), short report, Wheelhouse's software ID."""
    return [0x10, slot, 0x00, WHEELHOUSE_SWID, 0x21, 0x50, 0x00]


def _bolt_receiver(features):
    """Answers a getFeature request as a Bolt receiver does: a long answer
    with the feature index for a paired slot (0 when the device has no
    THUMB_WHEEL), and a short HID++ 1.0 error for an unreachable slot."""
    def answer(request):
        slot, fn_swid = request[1], request[3]
        if slot in features:
            return [[0x11, slot, 0x00, fn_swid, features[slot], 0x00, 0x01] + [0x00] * 13]
        return [[0x10, slot, 0x8F, 0x00, fn_swid, 0x09, 0x00]]
    return answer


class _FakeShortInterface:
    """The receiver's HID++ short collection (mi_02 col01, usage page 0xFF00,
    usage 0x0001) as pywinusb shows it. ``answer`` returns the reports the
    receiver sends back, delivered to the raw data handler; ``block`` makes
    send_output_report wait, as pywinusb's overlapped write can."""

    vendor_id = 0x046D
    product_id = 0xC548
    product_name = "USB Receiver"
    device_path = r"\\?\hid#vid_046d&pid_c548&mi_02&col01#fake"

    def __init__(self, answer=None, block=None):
        self.hid_caps = SimpleNamespace(
            usage_page=0xFF00, usage=0x0001, output_report_byte_length=7)
        self.answer = answer
        self.block = block
        self.handler = None
        self.sent = []
        self.send_entered = threading.Event()
        self._lock = threading.Lock()

    def open(self, shared=False):
        pass

    def is_opened(self):
        return True

    def is_plugged(self):
        return True

    def set_raw_data_handler(self, handler):
        self.handler = handler

    def close(self):
        pass

    def send_output_report(self, data):
        with self._lock:
            self.sent.append(list(data))
        self.send_entered.set()
        if self.block is not None:
            self.block.wait(5.0)
        if self.answer is not None and self.handler is not None:
            for report in self.answer(list(data)):
                self.handler(report)

    def sent_copy(self):
        with self._lock:
            return list(self.sent)


def _wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class TestMavencoreCapture:
    """The replayed reports from mavencore (slot 6, page 0x1306)."""

    def test_capture_holds_what_the_tests_replay(self):
        reports = _long_reports()
        notifications = [r for r in reports if _is_notification(r)]
        assert len(notifications) == 509
        assert sum(1 for r in notifications if r[4] == 0x00) == 283
        assert sum(1 for r in notifications if r[4] == 0xFF) == 226
        options_plus = [r for r in reports if r[2] == 0x13 and r[3] != 0x00]
        assert options_plus, "the capture lost the Options+ THUMB_WHEEL answers"
        expected = _expected_deltas()
        assert expected and expected[0] > 0 and expected[-1] < 0

    def test_notifications_dropped_without_a_getfeature_answer(self, hid_listener):
        assert _deltas_from(hid_listener, _long_reports()) == []

    def test_notifications_act_after_the_getfeature_answer(self, hid_listener):
        """The defect on mavencore: every slot-6 report was dropped."""
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)

        deltas = _deltas_from(hid_listener, _long_reports())

        assert deltas == _expected_deltas()
        assert deltas[0] > 0 and deltas[-1] < 0

    def test_slot_6_gives_the_same_deltas_as_slot_3_on_ikon(self, hid_listener):
        """The same reports from Ikon's slot 3 give the same up/down result."""
        on_ikon = [[r[0], 0x03] + r[2:] for r in _long_reports()]
        _answer_thumb_wheel_query(hid_listener, 0x03, 0x13)
        ikon_deltas = _deltas_from(hid_listener, on_ikon)

        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        mavencore_deltas = _deltas_from(hid_listener, _long_reports())

        assert mavencore_deltas == ikon_deltas == _expected_deltas()

    def test_options_plus_answers_never_turn_the_wheel(self, hid_listener):
        """Options+ requests and answers on slot 6 carry its software ID in
        byte 3; only byte 3 = 0x00 is a device notification."""
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        captured = [r for r in _long_reports() if r[2] == 0x13 and r[3] != 0x00]
        shaped_like_turns = [
            [0x11, 0x06, 0x13, 0x0A, 0x00, 0x01] + [0x00] * 14,
            [0x11, 0x06, 0x13, 0x1A, 0xFF, 0xFF] + [0x00] * 14,
        ]

        assert _deltas_from(hid_listener, captured + shaped_like_turns) == []

    def test_own_getfeature_answer_is_never_a_wheel_turn(self, hid_listener):
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)
        hid_listener._raw_data_event_handler([0x10, 0x05, 0x8F, 0x00, WHEELHOUSE_SWID, 0x09, 0x00])

        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_options_plus_getfeature_answer_is_not_taken_as_ours(self, hid_listener):
        """A Root getFeature answer with Options+'s software ID 0xA does not
        register the slot."""
        hid_listener._raw_data_event_handler(
            [0x11, 0x06, 0x00, 0x0A, 0x13, 0x00, 0x01] + [0x00] * 13)

        assert _deltas_from(hid_listener, _long_reports()) == []

    def test_answer_with_feature_index_zero_registers_nothing(self, hid_listener):
        """Feature index 0 means the device has no THUMB_WHEEL."""
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x00)

        assert _deltas_from(hid_listener, _long_reports()) == []

    def test_new_answer_for_a_slot_replaces_its_feature_index(self, hid_listener):
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x00)

        assert _deltas_from(hid_listener, _long_reports()) == []


class TestFixedPagesRemoved:
    """The hard-coded pages 0x1302, 0x1303, 0x0F02 are gone: a report on one
    of them from a slot that did not answer the query is dropped."""

    @pytest.mark.parametrize("data", [
        [0x00, 0x02, 0x13, 0x00, 0x00, 5],
        [0x00, 0x02, 0x13, 0x00, 0xFF, 250],
        [0x00, 0x03, 0x13, 0x00, 0xFF, 253],
        [0x00, 0x03, 0x13, 0x00, 0x00, 4],
        [0x00, 0x02, 0x0F, 0x00, 0x00, 2],
    ])
    def test_unanswered_slot_on_a_former_fixed_page_is_dropped(self, hid_listener, data):
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)

        hid_listener._raw_data_event_handler(data)

        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_answer_for_another_feature_index_does_not_open_the_slot(self, hid_listener):
        """Slot 2 answered with feature index 0x0F: page 0x1302 stays closed."""
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x0F)

        hid_listener._raw_data_event_handler([0x00, 0x02, 0x13, 0x00, 0x00, 5])

        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_rotation_beyond_one_byte_is_dropped_as_on_ikon(self, hid_listener):
        """High byte 0x00 with a low byte above 127 is rotation +128..+255,
        not a negative turn; slot 3 on Ikon dropped it before this change."""
        hid_listener._accumulate_and_maybe_send = Mock()
        _answer_thumb_wheel_query(hid_listener, 0x02, 0x13)

        hid_listener._raw_data_event_handler([0x00, 0x02, 0x13, 0x00, 0x00, 250])

        hid_listener._accumulate_and_maybe_send.assert_not_called()


class TestThumbWheelQuery:
    """The read-only getFeature(0x2150) query to receiver slots 1-6."""

    def _listener_with(self, hid_listener, device):
        hid_listener.opened_devices = [device]
        device.handler = hid_listener._raw_data_event_handler
        return hid_listener

    def test_query_sends_only_getfeature_with_its_own_software_id(self, hid_listener):
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        assert device.sent_copy() == [_get_feature_request(slot) for slot in range(1, 7)]
        assert WHEELHOUSE_SWID not in (0x00, 0x0A)

    def test_query_round_opens_the_answered_slot(self, hid_listener):
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        assert _deltas_from(listener, _long_reports()) == _expected_deltas()

    def test_query_is_sent_only_on_the_hidpp_short_interface(self, hid_listener):
        other = MagicMock()
        other.hid_caps = SimpleNamespace(usage_page=0xFF00, usage=0x0002,
                                         output_report_byte_length=20)
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        listener = self._listener_with(hid_listener, device)
        listener.opened_devices = [other, device]

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        other.send_output_report.assert_not_called()
        assert len(device.sent_copy()) == 6

    def test_info_line_for_each_slot_found(self, hid_listener, caplog):
        device = _FakeShortInterface(answer=_bolt_receiver({3: 0x13, 6: 0x13, 2: 0x00}))
        listener = self._listener_with(hid_listener, device)

        with caplog.at_level(logging.INFO, logger="handlers.hid_listener"), \
                patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        found = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO and "THUMB_WHEEL" in r.getMessage()]
        assert len(found) == 2, found
        assert "slot 3" in found[0] and "0x13" in found[0]
        assert "slot 6" in found[1] and "0x13" in found[1]
        assert not any("no receiver slot answered" in m for m in found)

    def test_one_info_line_when_no_slot_answers(self, hid_listener, caplog):
        device = _FakeShortInterface(answer=_bolt_receiver({}))
        listener = self._listener_with(hid_listener, device)

        with caplog.at_level(logging.INFO, logger="handlers.hid_listener"), \
                patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert sum("no receiver slot answered" in m for m in lines) == 1, lines
        assert len(device.sent_copy()) == 6

    def test_one_info_line_when_the_receiver_is_silent(self, hid_listener, caplog):
        device = _FakeShortInterface()  # send does nothing, nothing answers
        listener = self._listener_with(hid_listener, device)

        started = time.monotonic()
        with caplog.at_level(logging.INFO, logger="handlers.hid_listener"), \
                patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._query_thumb_wheel_slots(range(1, 7))
        elapsed = time.monotonic() - started

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert sum("no receiver slot answered" in m for m in lines) == 1, lines
        assert len(device.sent_copy()) == 6
        assert elapsed < 2.0, f"each query must stop at its time limit ({elapsed:.2f}s)"

    def test_a_failing_send_ends_quietly(self, hid_listener):
        device = _FakeShortInterface()
        device.send_output_report = Mock(side_effect=Exception("device closed"))
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._query_thumb_wheel_slots(range(1, 7))  # must not raise

    def test_link_up_notification_queries_that_slot(self, hid_listener):
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)
        connected = [r for iface, r in _capture_reports() if r[:3] == [0x10, 0x06, 0x41]][0]
        assert connected == [0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0]

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._raw_data_event_handler(connected)
            assert _wait_for(lambda: len(device.sent_copy()) >= 1)
            time.sleep(0.2)

        assert device.sent_copy() == [_get_feature_request(6)]

    def test_link_lost_notification_sends_nothing(self, hid_listener):
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)
        lost = [r for iface, r in _capture_reports() if r[:3] == [0x10, 0x06, 0x41]][1]
        assert lost == [0x10, 0x06, 0x41, 0x10, 0x42, 0x42, 0xB0]

        listener._raw_data_event_handler(lost)
        time.sleep(0.3)

        assert device.sent_copy() == []

    def test_link_lost_keeps_the_answered_slot(self, hid_listener):
        _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
        hid_listener._raw_data_event_handler([0x10, 0x06, 0x41, 0x10, 0x42, 0x42, 0xB0])

        assert _deltas_from(hid_listener, _long_reports()) == _expected_deltas()

    def test_reconnect_answer_reopens_the_slot(self, hid_listener):
        """After the receiver reports slot 6 connected, its answer opens it."""
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._raw_data_event_handler([0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0])
            assert _wait_for(lambda: len(device.sent_copy()) >= 1)
            time.sleep(0.2)

        assert _deltas_from(listener, _long_reports()) == _expected_deltas()


class TestThumbWheelQueryLifecycle:
    """start() asks slots 1-6 without waiting, and stop() never waits for it."""

    def _start(self, listener, device):
        with patch("handlers.hid_listener.hid") as mock_hid:
            mock_hid.find_all_hid_devices.return_value = [device]
            started = time.monotonic()
            result = listener.start()
            return result, time.monotonic() - started

    def test_start_queries_slots_1_to_6(self, hid_listener):
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
                result, _ = self._start(hid_listener, device)
                assert result is True
                assert _wait_for(lambda: len(device.sent_copy()) == 6)
        finally:
            hid_listener.stop()

        assert device.sent_copy() == [_get_feature_request(slot) for slot in range(1, 7)]
        assert _deltas_from(hid_listener, _long_reports()) == _expected_deltas()

    def test_blocked_send_delays_neither_start_nor_stop(self, hid_listener):
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        try:
            result, start_time = self._start(hid_listener, device)
            assert result is True
            assert device.send_entered.wait(2.0), "start() sent no query"
            assert start_time < 0.6, f"start() waited {start_time:.2f}s"

            stop_started = time.monotonic()
            hid_listener.stop()
            stop_time = time.monotonic() - stop_started
            assert stop_time < 0.6, f"stop() waited {stop_time:.2f}s"
        finally:
            release.set()
            hid_listener.stop()

        time.sleep(0.3)
        assert len(device.sent_copy()) == 1, "the round went on after stop()"

    def test_silent_receiver_does_not_stop_the_listener(self, hid_listener):
        """While a query waits for its time limit, reports from an answered
        slot still act."""
        device = _FakeShortInterface()
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 1.0):
                result, start_time = self._start(hid_listener, device)
                assert result is True and start_time < 0.6
                assert device.send_entered.wait(2.0), "start() sent no query"
                _answer_thumb_wheel_query(hid_listener, 0x06, 0x13)
                hid_listener._accumulate_and_maybe_send = Mock()
                hid_listener._raw_data_event_handler(
                    [0x11, 0x06, 0x13, 0x00, 0x00, 0x01, 0x00, 0x27, 0x02, 0x02] + [0] * 10)
                hid_listener._accumulate_and_maybe_send.assert_called_once_with(1)
        finally:
            hid_listener.stop()


# A THUMB_WHEEL notification from mavencore's slot 6 (feature index 0x13),
# rotation +1: the form of the 509 captured notifications.
SLOT_6_TURN = [0x11, 0x06, 0x13, 0x00, 0x00, 0x01, 0x00, 0x27, 0x02, 0x02] + [0x00] * 10


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class TestQueryAfterNotification:
    """A HID++ notification from a slot with no answer yet starts a getFeature
    query for that slot, at most once per slot in 5 seconds
    (wh-thumb-wheel-not-detected, boss ruling 15:19)."""

    def _listener_with(self, hid_listener, device, clock=None):
        hid_listener.opened_devices = [device]
        device.handler = hid_listener._raw_data_event_handler
        hid_listener._monotonic = clock or _FakeClock()
        return hid_listener

    def _wait_idle(self, listener, slot):
        assert _wait_for(lambda: slot not in getattr(
            listener, "_slot_queries_in_progress", set())), "the query never ended"

    def test_timed_out_slot_is_queried_after_its_notification(self, hid_listener):
        """The start query to slot 6 timed out (mouse asleep). The next wheel
        turn from slot 6 asks again; after the answer the turns act."""
        device = _FakeShortInterface()  # silent: the start query times out
        listener = self._listener_with(hid_listener, device)
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._query_thumb_wheel_slots((6,))
            assert device.sent_copy() == [_get_feature_request(6)]
            device.answer = _bolt_receiver({6: 0x13})  # the mouse woke up

            listener._raw_data_event_handler(SLOT_6_TURN)

            assert _wait_for(lambda: len(device.sent_copy()) == 2), \
                "a turn from the unanswered slot started no query"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)] * 2
        assert _deltas_from(listener, _long_reports()) == _expected_deltas()

    def test_one_query_per_slot_in_five_seconds(self, hid_listener):
        clock = _FakeClock(100.0)
        device = _FakeShortInterface()  # never answers
        listener = self._listener_with(hid_listener, device, clock)
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            for _ in range(10):
                listener._raw_data_event_handler(SLOT_6_TURN)
            assert _wait_for(lambda: len(device.sent_copy()) >= 1), \
                "a turn from the unanswered slot started no query"
            self._wait_idle(listener, 6)

            clock.now = 104.9
            for _ in range(10):
                listener._raw_data_event_handler(SLOT_6_TURN)
            time.sleep(0.3)
            assert device.sent_copy() == [_get_feature_request(6)]

            clock.now = 105.0
            listener._raw_data_event_handler(SLOT_6_TURN)
            assert _wait_for(lambda: len(device.sent_copy()) == 2), \
                "a turn 5 s after the last query started no query"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)] * 2

    def test_no_thumb_wheel_answer_stops_queries_until_link_up(self, hid_listener):
        clock = _FakeClock(100.0)
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x00}))
        listener = self._listener_with(hid_listener, device, clock)
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._query_thumb_wheel_slots((6,))
            clock.now = 200.0
            for _ in range(5):
                listener._raw_data_event_handler(SLOT_6_TURN)
            time.sleep(0.3)
            assert device.sent_copy() == [_get_feature_request(6)], \
                "a slot that answered 'no THUMB_WHEEL' was queried again"

            # A new device links on slot 6; it does not answer in time.
            device.answer = None
            listener._raw_data_event_handler([0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0])
            assert _wait_for(lambda: len(device.sent_copy()) == 2), "0x41 sent no query"
            time.sleep(0.2)

            clock.now = 300.0
            listener._raw_data_event_handler(SLOT_6_TURN)
            assert _wait_for(lambda: len(device.sent_copy()) == 3), \
                "the 0x41 link-up did not clear the 'no THUMB_WHEEL' state"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)] * 3

    def test_answered_slot_is_not_queried_by_its_notifications(self, hid_listener):
        clock = _FakeClock(100.0)
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device, clock)
        _answer_thumb_wheel_query(listener, 0x06, 0x13)

        deltas = _deltas_from(listener, _long_reports())
        clock.now = 200.0
        listener._raw_data_event_handler([0x11, 0x06, 0x05, 0x00] + [0x00] * 16)
        time.sleep(0.3)

        assert deltas == _expected_deltas()
        assert device.sent_copy() == []

    def test_own_answer_and_error_reports_start_no_query(self, hid_listener):
        """Slots 4 and 5 have no answer; the error reports to Wheelhouse's own
        query and its answer carry their slot but are not notifications."""
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)

        listener._raw_data_event_handler([0x10, 0x05, 0x8F, 0x00, WHEELHOUSE_SWID, 0x09, 0x00])
        listener._raw_data_event_handler([0x11, 0x04, 0xFF, 0x00, WHEELHOUSE_SWID, 0x05] + [0] * 14)
        _answer_thumb_wheel_query(listener, 0x03, 0x13)
        _answer_thumb_wheel_query(listener, 0x02, 0x00)
        time.sleep(0.3)

        assert device.sent_copy() == []

    def test_options_plus_answer_starts_no_query(self, hid_listener):
        """An answer to Options+ (software ID 0xA) is not a notification."""
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)

        listener._raw_data_event_handler([0x11, 0x06, 0x13, 0x0A, 0x00, 0x01] + [0x00] * 14)
        listener._raw_data_event_handler([0x11, 0x06, 0x00, 0x0A, 0x13, 0x00, 0x01] + [0x00] * 13)
        time.sleep(0.3)

        assert device.sent_copy() == []

    def test_trigger_does_not_block_the_reader_thread(self, hid_listener):
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        listener = self._listener_with(hid_listener, device)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
                started = time.monotonic()
                listener._raw_data_event_handler(SLOT_6_TURN)
                handler_time = time.monotonic() - started
                assert device.send_entered.wait(2.0), \
                    "a turn from the unanswered slot started no query"
                assert handler_time < 0.5, f"the handler waited {handler_time:.2f}s"

                # While the send is blocked, the reader thread still records
                # an answer for another slot: no lock is held during the send.
                started = time.monotonic()
                _answer_thumb_wheel_query(listener, 0x03, 0x13)
                assert time.monotonic() - started < 0.5
                release.set()
                self._wait_idle(listener, 6)
        finally:
            release.set()

        assert _deltas_from(listener, [[0x11, 0x03] + r[2:] for r in _long_reports()]) \
            == _expected_deltas()

    def test_no_second_query_while_one_is_in_progress(self, hid_listener):
        clock = _FakeClock(100.0)
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        listener = self._listener_with(hid_listener, device, clock)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
                listener._raw_data_event_handler(SLOT_6_TURN)
                assert device.send_entered.wait(2.0), \
                    "a turn from the unanswered slot started no query"
                clock.now = 200.0  # past the 5 s limit, but the first still runs
                listener._raw_data_event_handler(SLOT_6_TURN)
                release.set()
                self._wait_idle(listener, 6)
                time.sleep(0.3)
        finally:
            release.set()

        assert device.sent_copy() == [_get_feature_request(6)]

    def test_no_query_after_stop(self, hid_listener):
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)
        listener._stop_event.set()

        listener._raw_data_event_handler(SLOT_6_TURN)
        time.sleep(0.3)

        assert device.sent_copy() == []

    def _count_starts(self, listener):
        """Record each query round this listener starts. Other tests' query
        threads can still run, so a count of threads by name is not used."""
        starts = []
        start = listener._start_thumb_wheel_query

        def counting_start(*args, **kwargs):
            starts.append(args)
            return start(*args, **kwargs)

        listener._start_thumb_wheel_query = counting_start
        return starts

    def test_link_up_burst_starts_one_round_per_slot(self, hid_listener):
        """A weak link sends lost-link and link-up pairs while the first query
        to slot 6 is blocked in its send. The burst starts no more rounds
        while that query runs, and one more round after it ends, because the
        first query may have asked the device before the last link-up
        (wh-thumb-wheel-not-detected.1.1, .1.2, .1.3)."""
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        listener = self._listener_with(hid_listener, device)
        starts = self._count_starts(listener)
        link_up = [0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0]
        link_lost = [0x10, 0x06, 0x41, 0x10, 0x42, 0x42, 0xB0]
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
                listener._raw_data_event_handler(link_up)
                assert device.send_entered.wait(2.0), "the link-up started no query"
                for _ in range(10):
                    listener._raw_data_event_handler(link_lost)
                    listener._raw_data_event_handler(link_up)
                time.sleep(0.2)
                assert len(starts) == 1, f"{len(starts)} query rounds for one slot"
                release.set()
                assert _wait_for(lambda: len(device.sent_copy()) >= 2), \
                    "the link-ups during the query started no query after it"
                self._wait_idle(listener, 6)
                time.sleep(0.3)
        finally:
            release.set()

        assert len(starts) == 2, f"{len(starts)} query rounds for one slot"
        assert device.sent_copy() == [_get_feature_request(6)] * 2

    def test_replacement_mouse_linked_during_a_query_is_asked(self, hid_listener):
        """The old mouse on slot 6 answers index 0x0F; a replacement mouse
        with index 0x13 then links on slot 6 before that query ends. A query
        after the link-up asks the replacement, so its turns act
        (wh-thumb-wheel-not-detected.1.2)."""
        old_mouse = _bolt_receiver({6: 0x0F})
        new_mouse = _bolt_receiver({6: 0x13})
        link_up = [0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0]
        link_lost = [0x10, 0x06, 0x41, 0x10, 0x42, 0x42, 0xB0]
        requests = []

        def answer(request):
            requests.append(request)
            if len(requests) == 1:
                return old_mouse(request) + [link_lost, link_up]
            return new_mouse(request)

        device = _FakeShortInterface(answer=answer)
        listener = self._listener_with(hid_listener, device)
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._raw_data_event_handler(link_up)
            assert _wait_for(lambda: len(device.sent_copy()) >= 2), \
                "the replacement mouse was never asked"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)] * 2
        assert _deltas_from(listener, _long_reports()) == _expected_deltas()

    def test_failed_start_after_a_query_leaves_the_slot_free(self, hid_listener):
        """A link-up arrives during a query; the query that should follow it
        does not start. The slot keeps no "in progress" mark, so the next
        link-up asks it."""
        link_up = [0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0]
        requests = []

        def answer(request):
            requests.append(request)
            return [link_up] if len(requests) == 1 else []

        device = _FakeShortInterface(answer=answer)
        listener = self._listener_with(hid_listener, device)
        start = listener._start_thumb_wheel_query
        starts = []

        def second_start_fails(*args, **kwargs):
            starts.append(args)
            return False if len(starts) == 2 else start(*args, **kwargs)

        listener._start_thumb_wheel_query = second_start_fails
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._raw_data_event_handler(link_up)
            assert _wait_for(lambda: len(starts) == 2), "no query followed the link-up"
            self._wait_idle(listener, 6)

            listener._raw_data_event_handler(link_up)
            assert _wait_for(lambda: len(device.sent_copy()) == 2), \
                "the slot was never queried again after the failed start"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)] * 2


class TestQueryLimitsAndFilters:
    """Edges of the getFeature query that the mutation gate
    tests/mutation_gate_thumb_wheel_slot_query.py showed no other test pins
    (wh-thumb-wheel-not-detected)."""

    def _listener_with(self, hid_listener, device, clock=None):
        hid_listener.opened_devices = [device]
        device.handler = hid_listener._raw_data_event_handler
        hid_listener._monotonic = clock or _FakeClock()
        return hid_listener

    def _wait_idle(self, listener, slot):
        assert _wait_for(lambda: slot not in listener._slot_queries_in_progress), \
            "the query never ended"

    def test_shipped_time_limit_is_one_second_per_slot(self):
        """Every other test patches the limit; this pins the shipped value."""
        from handlers import hid_listener as module

        assert module.HIDPP_QUERY_TIMEOUT == 1.0

    def test_request_is_padded_to_the_output_report_length(self, hid_listener):
        """pywinusb sends only a report of the interface's full length."""
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}))
        device.hid_caps.output_report_byte_length = 20
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots((6,))

        assert device.sent_copy() == [_get_feature_request(6) + [0x00] * 13]

    @pytest.mark.parametrize("reply", [
        pytest.param([0x11, 0x06, 0x00, WHEELHOUSE_SWID, 0x13, 0x00, 0x01] + [0x00] * 13,
                     id="answer"),
        pytest.param([0x10, 0x06, 0x8F, 0x00, WHEELHOUSE_SWID, 0x09, 0x00],
                     id="receiver-error"),
        pytest.param([0x11, 0x06, 0xFF, 0x00, WHEELHOUSE_SWID, 0x05] + [0x00] * 14,
                     id="device-error"),
    ])
    def test_each_reply_ends_that_slot_wait_at_once(self, hid_listener, reply):
        """An answer or an error report to Wheelhouse's own query ends the
        wait for that slot; the time limit is only for a slot that is silent."""
        device = _FakeShortInterface(answer=lambda request: [list(reply)])
        listener = self._listener_with(hid_listener, device)

        started = time.monotonic()
        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 3.0):
            listener._query_thumb_wheel_slots((6,))
        elapsed = time.monotonic() - started

        assert elapsed < 1.5, f"the reply did not end the wait ({elapsed:.2f}s)"

    def test_error_for_another_program_does_not_end_the_wait(self, hid_listener):
        """Logi Options+ (software ID 0xA) gets an error for slot 6 while
        Wheelhouse waits; Wheelhouse's own answer comes after it."""
        options_plus_error = [0x10, 0x06, 0x8F, 0x00, 0x0A, 0x09, 0x00]
        own_answer = [0x11, 0x06, 0x00, WHEELHOUSE_SWID, 0x13, 0x00, 0x01] + [0x00] * 13
        timers = []

        def answer(request):
            timer = threading.Timer(0.3, device.handler, args=(own_answer,))
            timers.append(timer)
            timer.start()
            return [options_plus_error]

        device = _FakeShortInterface(answer=answer)
        listener = self._listener_with(hid_listener, device)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 3.0):
                result = listener._ask_thumb_wheel_feature_index(device, 6)
        finally:
            for timer in timers:
                timer.join(2.0)

        assert result == 0x13

    def test_round_where_every_answer_is_zero_logs_the_no_slot_line(self, hid_listener, caplog):
        """Feature index 0 is "no THUMB_WHEEL", so no slot was found."""
        device = _FakeShortInterface(answer=_bolt_receiver({3: 0x00, 6: 0x00}))
        listener = self._listener_with(hid_listener, device)

        with caplog.at_level(logging.INFO, logger="handlers.hid_listener"), \
                patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
            listener._query_thumb_wheel_slots(range(1, 7))

        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert sum("no receiver slot answered" in m for m in lines) == 1, lines
        assert not any("Thumb wheel found" in m for m in lines), lines

    def test_query_thread_is_a_daemon(self, hid_listener):
        """A query blocked in a send must not keep the process alive."""
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        listener = self._listener_with(hid_listener, device)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
                assert listener._start_thumb_wheel_query((6,)) is True
                assert device.send_entered.wait(2.0), "the query sent nothing"
                threads = [t for t in threading.enumerate()
                           if t.name == "HIDPPThumbWheelQuery"]
                assert threads, "no query thread is running"
                assert all(t.daemon for t in threads), threads
        finally:
            release.set()

    def test_round_sends_nothing_more_after_stop(self, hid_listener):
        """stop() lands while the send to slot 1 is blocked; once that send
        returns, the round asks no further slot."""
        release = threading.Event()
        device = _FakeShortInterface(block=release)
        listener = self._listener_with(hid_listener, device)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
                assert listener._start_thumb_wheel_query(range(1, 7)) is True
                assert device.send_entered.wait(2.0), "the round sent nothing"
                listener._stop_event.set()
                release.set()
                time.sleep(0.6)
        finally:
            release.set()

        assert device.sent_copy() == [_get_feature_request(1)]

    @pytest.mark.parametrize("slot", [
        pytest.param(0x00, id="slot-0x00"),
        pytest.param(0x07, id="slot-0x07"),
        pytest.param(0xFF, id="slot-0xff"),
    ])
    def test_link_up_outside_slots_1_to_6_sends_nothing(self, hid_listener, slot):
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._raw_data_event_handler([0x10, slot, 0x41, 0x10, 0x02, 0x42, 0xB0])
            time.sleep(0.3)

        assert device.sent_copy() == []

    @pytest.mark.parametrize("report", [
        pytest.param([0x02, 0x06, 0x13, 0x00, 0x00, 0x01, 0x00, 0x00],
                     id="report-id-0x02"),
        pytest.param([0x11, 0x00, 0x13, 0x00, 0x00, 0x01] + [0x00] * 14,
                     id="slot-0x00"),
        pytest.param([0x11, 0x07, 0x13, 0x00, 0x00, 0x01] + [0x00] * 14,
                     id="slot-0x07"),
        pytest.param([0x11, 0xFF, 0x13, 0x00, 0x00, 0x01] + [0x00] * 14,
                     id="slot-0xff"),
        pytest.param([0x10, 0x06, 0x8F, 0x00, 0x0A, 0x09, 0x00],
                     id="options-plus-receiver-error"),
        pytest.param([0x11, 0x06, 0x00, 0x10, 0x13, 0x00, 0x01] + [0x00] * 13,
                     id="root-feature-report"),
    ])
    def test_report_that_is_not_a_notification_starts_no_query(self, hid_listener, report):
        """Only 10/11 <slot 1-6> <feature index 1-0x7F> <software ID 0> is a
        notification; slot 6 has no answer yet in every case."""
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            listener._raw_data_event_handler(report)
            time.sleep(0.3)

        assert device.sent_copy() == []

    def test_failed_thread_start_leaves_the_slot_free_for_the_next_query(self, hid_listener):
        clock = _FakeClock(100.0)
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device, clock)

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            with patch.object(threading.Thread, "start",
                              side_effect=RuntimeError("can't start new thread")):
                listener._raw_data_event_handler(SLOT_6_TURN)
            assert device.sent_copy() == []

            clock.now = 105.0
            listener._raw_data_event_handler(SLOT_6_TURN)
            assert _wait_for(lambda: len(device.sent_copy()) == 1), \
                "the slot was never queried again after the failed start"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)]

    def test_failed_link_up_start_leaves_the_slot_free(self, hid_listener):
        """A link-up whose query thread did not start leaves no "in progress"
        mark, so the next link-up asks the slot."""
        device = _FakeShortInterface()
        listener = self._listener_with(hid_listener, device)
        link_up = [0x10, 0x06, 0x41, 0x10, 0x02, 0x42, 0xB0]

        with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.05):
            with patch.object(threading.Thread, "start",
                              side_effect=RuntimeError("can't start new thread")):
                listener._raw_data_event_handler(link_up)
            assert device.sent_copy() == []

            listener._raw_data_event_handler(link_up)
            assert _wait_for(lambda: len(device.sent_copy()) == 1), \
                "the slot was never queried again after the failed start"
            self._wait_idle(listener, 6)

        assert device.sent_copy() == [_get_feature_request(6)]

    def test_notification_round_skips_a_slot_answered_while_it_waited(self, hid_listener):
        """The start round holds the round lock; slot 6 sends a notification
        and its query waits. The start round then gets slot 6's answer, so
        the waiting query sends nothing."""
        release = threading.Event()
        device = _FakeShortInterface(answer=_bolt_receiver({6: 0x13}), block=release)
        listener = self._listener_with(hid_listener, device)
        try:
            with patch("handlers.hid_listener.HIDPP_QUERY_TIMEOUT", 0.2):
                assert listener._start_thumb_wheel_query(range(1, 7)) is True
                assert device.send_entered.wait(2.0), "the start round sent nothing"
                listener._raw_data_event_handler(SLOT_6_TURN)
                assert 6 in listener._slot_queries_in_progress
                release.set()
                self._wait_idle(listener, 6)
                time.sleep(0.2)
        finally:
            release.set()

        assert device.sent_copy() == [_get_feature_request(slot) for slot in range(1, 7)]
