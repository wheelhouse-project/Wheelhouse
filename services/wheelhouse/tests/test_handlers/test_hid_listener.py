"""Tests for HIDListener handler.

Tests Logitech HID device event processing including:
- Raw data parsing (_raw_data_event_handler)
- Event batching (_accumulate_and_maybe_send)
- Queue management (_safe_enqueue_event)
- Device lifecycle (start/stop)
- Adversarial: short data, zero deltas, queue full
"""

import asyncio
import threading
import time
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
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(5)

    def test_target_page_1302_negative_delta(self, hid_listener):
        """Page 0x1302 (Bolt receiver) with negative delta (>127) dispatches event."""
        # delta byte = 250 -> signed = 250 - 256 = -6
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 250]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-6)

    def test_target_page_1302_zero_delta_skipped(self, hid_listener):
        """Page 0x1302 with delta=0 does not dispatch event."""
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 0]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_target_page_1303_up_direction(self, hid_listener):
        """Page 0x1303 with UP usage_id and negative delta dispatches."""
        # Page = 0x1303, usage_id = 0xFF00 (UP), delta = -3
        data = [0x00, 0x03, 0x13, 0x00, 0xFF, 253]  # 253 - 256 = -3
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-3)

    def test_target_page_1303_down_direction(self, hid_listener):
        """Page 0x1303 with DOWN usage_id and positive delta dispatches."""
        # Page = 0x1303, usage_id = 0x0000 (DOWN), delta = 4
        data = [0x00, 0x03, 0x13, 0x00, 0x00, 4]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(4)

    def test_target_page_1303_wrong_direction_skipped(self, hid_listener):
        """Page 0x1303 with UP usage_id but positive delta is skipped."""
        # usage_id = 0xFF00 (UP) but delta = +3 (wrong direction)
        data = [0x00, 0x03, 0x13, 0x00, 0xFF, 3]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_not_called()

    def test_target_page_0F02_processes(self, hid_listener):
        """Page 0x0F02 is in TARGET_PAGE and processes correctly."""
        # Page = 0x0F02, usage_id = 0x0000 (DOWN), delta = 2
        data = [0x00, 0x02, 0x0F, 0x00, 0x00, 2]
        hid_listener._accumulate_and_maybe_send = Mock()
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
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(127)

    def test_signed_byte_boundary_128(self, hid_listener):
        """Delta byte 128 is negative (-128, smallest negative signed byte)."""
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 128]
        hid_listener._accumulate_and_maybe_send = Mock()
        hid_listener._raw_data_event_handler(data)
        hid_listener._accumulate_and_maybe_send.assert_called_once_with(-128)

    def test_signed_byte_boundary_255(self, hid_listener):
        """Delta byte 255 is -1."""
        data = [0x00, 0x02, 0x13, 0x00, 0x00, 255]
        hid_listener._accumulate_and_maybe_send = Mock()
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
