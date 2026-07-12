"""Tests for WheelHouseApp IPC interface (P1-T4).

Tests the async application interface that enables communication between
the main WheelHouse service and the UI input synthesis process via shared
memory and multiprocessing primitives.

Key behaviors tested:
- Initialization with SharedMemory, events, queues
- start() creates background tasks and optionally starts WebSocket
- stop()/shutdown() cancels background tasks
- send_command() fire-and-forget enqueuing
- send_request() request-response with futures
- _frame_and_write() pickle serialization to shared memory
- _demux_loop() response demultiplexing
- _sender_loop() serialized IPC sends
- _await_event_state() polling with timeout
"""
import asyncio
import pickle
import struct
from multiprocessing import Event, Queue
from queue import Empty
from unittest.mock import Mock, AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_shm():
    """Mock SharedMemory with a real bytearray as buf."""
    shm = MagicMock()
    buf = bytearray(1024 * 64)
    shm.buf = buf
    shm.size = 1024 * 64
    shm.name = "test_shm"
    return shm


@pytest.fixture
def mock_command_ready_event():
    """Mock multiprocessing Event for command signaling."""
    event = MagicMock()
    event.is_set.return_value = False
    event.set = Mock()
    event.clear = Mock()
    return event


@pytest.fixture
def mock_ui_ready_event():
    """Mock multiprocessing Event for UI readiness."""
    event = MagicMock()
    event.is_set.return_value = True
    return event


@pytest.fixture
def mock_response_queue():
    """Mock multiprocessing Queue for responses."""
    q = MagicMock()
    q.get_nowait = Mock(side_effect=Empty)
    return q


@pytest.fixture
def app(mock_shm, mock_command_ready_event, mock_ui_ready_event, mock_response_queue):
    """Create a WheelHouseApp with all dependencies mocked."""
    with patch("app.shared_memory.SharedMemory", return_value=mock_shm):
        from app import WheelHouseApp
        instance = WheelHouseApp(
            shm_name="test_shm",
            command_ready_event=mock_command_ready_event,
            ui_ready_event=mock_ui_ready_event,
            response_queue=mock_response_queue,
            shm_bytes=1024 * 64,
            response_timeout_s=2.0,
        )
    return instance


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:
    """Test WheelHouseApp constructor."""

    def test_creates_shared_memory(self, mock_command_ready_event, mock_ui_ready_event, mock_response_queue):
        """Constructor opens SharedMemory with given name."""
        with patch("app.shared_memory.SharedMemory") as mock_shm_cls:
            mock_shm_cls.return_value = MagicMock(buf=bytearray(1024), size=1024)
            from app import WheelHouseApp
            WheelHouseApp(
                shm_name="my_shm",
                command_ready_event=mock_command_ready_event,
                ui_ready_event=mock_ui_ready_event,
                response_queue=mock_response_queue,
            )
            mock_shm_cls.assert_called_once_with(name="my_shm")

    def test_stores_parameters(self, app, mock_command_ready_event, mock_ui_ready_event, mock_response_queue):
        """Constructor stores all provided parameters."""
        assert app.shm_name == "test_shm"
        assert app.command_ready_event is mock_command_ready_event
        assert app.ui_ready_event is mock_ui_ready_event
        assert app.response_queue is mock_response_queue
        assert app.shm_bytes == 1024 * 64
        assert app.response_timeout_s == 2.0

    def test_initial_state(self, app):
        """Constructor initializes empty state."""
        assert app.websocket_manager is None
        assert app.response_futures == {}
        assert app.demuxer_task is None
        assert app._sender_task is None

    def test_default_timeout(self, mock_command_ready_event, mock_ui_ready_event, mock_response_queue):
        """Default response timeout is 5 seconds."""
        with patch("app.shared_memory.SharedMemory") as mock_shm_cls:
            mock_shm_cls.return_value = MagicMock(buf=bytearray(1024), size=1024)
            from app import WheelHouseApp
            instance = WheelHouseApp(
                shm_name="test",
                command_ready_event=mock_command_ready_event,
                ui_ready_event=mock_ui_ready_event,
                response_queue=mock_response_queue,
            )
            assert instance.response_timeout_s == 5.0


# ---------------------------------------------------------------------------
# get_screen_dimensions
# ---------------------------------------------------------------------------

class TestGetScreenDimensions:
    """Test get_screen_dimensions helper."""

    def test_returns_tuple(self, app):
        """Returns (width, height) tuple."""
        result = app.get_screen_dimensions()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_default_resolution(self, app):
        """Default resolution is 1920x1080."""
        w, h = app.get_screen_dimensions()
        assert w == 1920
        assert h == 1080


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    """Test WheelHouseApp.start() method."""

    @pytest.mark.asyncio
    async def test_start_creates_websocket_manager_from_handler(self, app):
        """start() creates WebSocketManager from a simple text handler."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler, start_websocket=True)

            mock_ws_cls.assert_called_once()
            assert app.websocket_manager is mock_ws_instance

    @pytest.mark.asyncio
    async def test_start_creates_websocket_manager_from_speech_handler(self, app):
        """start() detects speech handler object and wraps process_transcription."""
        handler = Mock()
        handler.process_transcription = Mock()

        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler, start_websocket=True)

            # Should pass process_transcription as text_handler
            call_kwargs = mock_ws_cls.call_args
            assert call_kwargs[1]["text_handler"] is handler.process_transcription
            # Should also store full speech_handler
            assert mock_ws_instance.speech_handler is handler

    @pytest.mark.asyncio
    async def test_start_with_websocket_connects(self, app):
        """start(start_websocket=True) calls websocket_manager.start()."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("myhost", 9999, handler, start_websocket=True)

            mock_ws_instance.start.assert_awaited_once_with("myhost", 9999)

    @pytest.mark.asyncio
    async def test_start_without_websocket_skips_connect(self, app):
        """start(start_websocket=False) skips websocket connection."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler, start_websocket=False)

            mock_ws_instance.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_launches_demuxer_task(self, app):
        """start() creates the demuxer background task."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler, start_websocket=False)

            assert app.demuxer_task is not None
            assert not app.demuxer_task.done()

            # Cleanup
            app.demuxer_task.cancel()
            try:
                await app.demuxer_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_start_launches_sender_task(self, app):
        """start() creates the sender background task."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler, start_websocket=False)

            assert app._sender_task is not None
            assert not app._sender_task.done()

            # Cleanup
            app._sender_task.cancel()
            app.demuxer_task.cancel()
            try:
                await asyncio.gather(app._sender_task, app.demuxer_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    """Test WheelHouseApp.stop() method."""

    @pytest.mark.asyncio
    async def test_stop_cancels_demuxer(self, app):
        """stop() cancels the demuxer task."""
        app.demuxer_task = asyncio.create_task(asyncio.sleep(999))
        app._sender_task = asyncio.create_task(asyncio.sleep(999))
        app.websocket_manager = None

        await app.stop()

        assert app.demuxer_task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_cancels_sender(self, app):
        """stop() cancels the sender task."""
        app.demuxer_task = asyncio.create_task(asyncio.sleep(999))
        app._sender_task = asyncio.create_task(asyncio.sleep(999))
        app.websocket_manager = None

        await app.stop()

        assert app._sender_task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_stops_websocket_manager(self, app):
        """stop() stops the websocket manager if present."""
        app.demuxer_task = None
        app._sender_task = None
        app.websocket_manager = MagicMock()
        app.websocket_manager.stop = AsyncMock()

        await app.stop()

        # websocket_manager.stop() is called via create_task
        # Give event loop time to run the task
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_stop_with_no_tasks(self, app):
        """stop() handles case where no tasks exist."""
        app.demuxer_task = None
        app._sender_task = None
        app.websocket_manager = None

        # Should not raise
        await app.stop()


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------

class TestShutdown:
    """Test WheelHouseApp.shutdown() method."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_demuxer(self, app):
        """shutdown() cancels demuxer task."""
        app.demuxer_task = asyncio.create_task(asyncio.sleep(999))
        app._sender_task = None

        await app.shutdown()

        assert app.demuxer_task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_sender(self, app):
        """shutdown() cancels sender task."""
        app.demuxer_task = None
        app._sender_task = asyncio.create_task(asyncio.sleep(999))

        await app.shutdown()

        assert app._sender_task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_handles_already_done_tasks(self, app):
        """shutdown() handles tasks that are already done."""
        task = asyncio.create_task(asyncio.sleep(0))
        await task  # Let it complete
        app.demuxer_task = task
        app._sender_task = None

        # Should not raise
        await app.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_with_no_tasks(self, app):
        """shutdown() handles case where no tasks exist."""
        app.demuxer_task = None
        app._sender_task = None

        # Should not raise
        await app.shutdown()


# ---------------------------------------------------------------------------
# _frame_and_write()
# ---------------------------------------------------------------------------

class TestFrameAndWrite:
    """Test shared memory write framing."""

    def test_writes_pickle_with_size_header(self, app, mock_shm):
        """Writes 4-byte big-endian size + pickled payload to shared memory."""
        payload = {"action": "click", "x": 100}
        app._frame_and_write(payload)

        # Read back the size header
        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        # Read back the payload
        data = bytes(mock_shm.buf[4:4 + size])
        result = pickle.loads(data)

        assert result == payload

    def test_writes_correct_size(self, app, mock_shm):
        """Size header matches actual pickled data size."""
        payload = {"key": "value"}
        app._frame_and_write(payload)

        expected_data = pickle.dumps(payload)
        expected_size = len(expected_data)
        actual_size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]

        assert actual_size == expected_size

    def test_raises_on_oversized_payload(self, app, mock_shm):
        """Raises ValueError when payload exceeds shared memory capacity."""
        # Create a payload larger than shm.size - 4
        mock_shm.size = 100
        large_payload = {"data": "x" * 1000}

        with pytest.raises(ValueError, match="exceeds shared memory capacity"):
            app._frame_and_write(large_payload)

    def test_handles_complex_payload(self, app, mock_shm):
        """Handles complex nested payload structures."""
        payload = {
            "action": "complex",
            "params": {
                "nested": {"deep": True},
                "list": [1, 2, 3],
                "none": None,
            },
        }
        app._frame_and_write(payload)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = bytes(mock_shm.buf[4:4 + size])
        result = pickle.loads(data)
        assert result == payload


# ---------------------------------------------------------------------------
# _await_event_state()
# ---------------------------------------------------------------------------

class TestAwaitEventState:
    """Test event polling with timeout."""

    @pytest.mark.asyncio
    async def test_returns_true_when_state_matches(self, app, mock_command_ready_event):
        """Returns True immediately when event state matches desired."""
        mock_command_ready_event.is_set.return_value = True

        result = await app._await_event_state(desired_set=True, timeout_s=1.0)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self, app, mock_command_ready_event):
        """Returns False when timeout elapses without state match."""
        mock_command_ready_event.is_set.return_value = True

        result = await app._await_event_state(desired_set=False, timeout_s=0.05)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, app, mock_command_ready_event):
        """Returns False when is_set() raises an exception."""
        mock_command_ready_event.is_set.side_effect = OSError("event closed")

        result = await app._await_event_state(desired_set=True, timeout_s=0.05)

        assert result is False

    @pytest.mark.asyncio
    async def test_polls_until_state_changes(self, app, mock_command_ready_event):
        """Polls repeatedly until state matches desired value."""
        call_count = 0
        def side_effect():
            nonlocal call_count
            call_count += 1
            # Return True (set) after 3 polls
            return call_count >= 3

        mock_command_ready_event.is_set.side_effect = side_effect

        result = await app._await_event_state(desired_set=True, timeout_s=1.0, poll_s=0.01)

        assert result is True
        assert call_count >= 3


# ---------------------------------------------------------------------------
# send_command()
# ---------------------------------------------------------------------------

class TestSendCommand:
    """Test fire-and-forget command sending."""

    @pytest.mark.asyncio
    async def test_enqueues_dict_payload(self, app):
        """send_command with dict enqueues directly."""
        payload = {"action": "click", "params": {"x": 100}}

        await app.send_command(payload)

        item = app._outbound_q.get_nowait()
        assert item == payload

    @pytest.mark.asyncio
    async def test_string_action_wraps_as_dict(self, app):
        """send_command with string wraps as {action, params}."""
        await app.send_command("click", params={"x": 100})

        item = app._outbound_q.get_nowait()
        assert item["action"] == "click"
        assert item["params"] == {"x": 100}
        assert "trace_id" in item

    @pytest.mark.asyncio
    async def test_string_action_default_empty_params(self, app):
        """send_command with string and no params uses empty dict."""
        await app.send_command("noop")

        item = app._outbound_q.get_nowait()
        assert item["action"] == "noop"
        assert item["params"] == {}
        assert "trace_id" in item


# ---------------------------------------------------------------------------
# send_request()
# ---------------------------------------------------------------------------

class TestSendRequest:
    """Test request-response command sending."""

    @pytest.mark.asyncio
    async def test_enqueues_payload_with_request_id(self, app):
        """send_request enqueues payload containing request_id."""
        # Don't await the future - just check enqueue happened
        # We'll resolve the future manually
        task = asyncio.create_task(
            app.send_request("get_clipboard", params={"format": "text"}, timeout_s=0.1)
        )
        await asyncio.sleep(0.01)  # Let it enqueue

        item = app._outbound_q.get_nowait()
        assert item["action"] == "get_clipboard"
        assert item["params"] == {"format": "text"}
        assert "request_id" in item

        # Resolve the future to let task complete
        request_id = item["request_id"]
        if request_id in app.response_futures:
            app.response_futures[request_id].set_result({"status": "ok", "request_id": request_id})

        try:
            await task
        except asyncio.TimeoutError:
            pass  # Fine if timeout beats our resolution

    @pytest.mark.asyncio
    async def test_creates_future_for_request(self, app):
        """send_request creates a Future tracked in response_futures."""
        task = asyncio.create_task(
            app.send_request("test_action", timeout_s=0.1)
        )
        await asyncio.sleep(0.01)

        # Should have exactly one pending future
        assert len(app.response_futures) == 1

        # Cancel to clean up
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_returns_response_when_future_resolved(self, app):
        """send_request returns the response when future is resolved."""
        task = asyncio.create_task(
            app.send_request("test_action", timeout_s=2.0)
        )
        await asyncio.sleep(0.01)

        # Get the request_id from the queue
        item = app._outbound_q.get_nowait()
        request_id = item["request_id"]

        # Resolve the future
        response = {"request_id": request_id, "status": "done", "data": "hello"}
        app.response_futures[request_id].set_result(response)

        result = await task
        assert result["status"] == "done"
        assert result["data"] == "hello"

    @pytest.mark.asyncio
    async def test_raises_timeout_error(self, app):
        """send_request raises TimeoutError when no response arrives."""
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("slow_action", timeout_s=0.05)

    @pytest.mark.asyncio
    async def test_cleans_up_future_on_timeout(self, app):
        """Future is removed from response_futures after timeout."""
        try:
            await app.send_request("slow_action", timeout_s=0.05)
        except asyncio.TimeoutError:
            pass

        assert len(app.response_futures) == 0

    @pytest.mark.asyncio
    async def test_uses_default_timeout(self, app):
        """send_request uses response_timeout_s when timeout_s not specified."""
        app.response_timeout_s = 0.05

        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("slow_action")

    @pytest.mark.asyncio
    async def test_custom_timeout_overrides_default(self, app):
        """send_request uses provided timeout_s over default."""
        app.response_timeout_s = 10.0  # Very long default

        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("slow_action", timeout_s=0.05)


# ---------------------------------------------------------------------------
# _send_one()
# ---------------------------------------------------------------------------

class TestSendOne:
    """Test single payload send coordination."""

    @pytest.mark.asyncio
    async def test_writes_to_shared_memory(self, app, mock_shm, mock_command_ready_event):
        """_send_one writes payload to shared memory."""
        mock_command_ready_event.is_set.return_value = False
        payload = {"action": "test"}

        await app._send_one(payload)

        # Verify data was written
        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        assert size > 0
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data == payload

    @pytest.mark.asyncio
    async def test_sets_command_ready_event(self, app, mock_command_ready_event):
        """_send_one signals command_ready_event after writing."""
        mock_command_ready_event.is_set.return_value = False

        await app._send_one({"action": "test"})

        mock_command_ready_event.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_set_event_failure(self, app, mock_command_ready_event):
        """_send_one handles command_ready_event.set() failure gracefully."""
        mock_command_ready_event.is_set.return_value = False
        mock_command_ready_event.set.side_effect = OSError("broken event")

        # Should not raise
        await app._send_one({"action": "test"})


# ---------------------------------------------------------------------------
# _demux_loop()
# ---------------------------------------------------------------------------

class TestDemuxLoop:
    """Test response demultiplexer background task."""

    @pytest.mark.asyncio
    async def test_resolves_matching_future(self, app, mock_response_queue):
        """Demuxer resolves future when response_queue has matching request_id."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request_id = "test-uuid-123"
        app.response_futures[request_id] = future

        response = {"request_id": request_id, "status": "done", "data": "result"}

        call_count = 0
        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect

        # Start demuxer, let it process one response, then cancel
        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert future.done()
        assert future.result() == response

    @pytest.mark.asyncio
    async def test_sets_exception_on_error_response(self, app, mock_response_queue):
        """Demuxer sets exception when response contains error."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request_id = "test-uuid-err"
        app.response_futures[request_id] = future

        response = {"request_id": request_id, "error": True, "message": "something failed"}

        call_count = 0
        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect

        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert future.done()
        with pytest.raises(RuntimeError, match="something failed"):
            future.result()

    @pytest.mark.asyncio
    async def test_ignores_unknown_request_id(self, app, mock_response_queue):
        """Demuxer logs warning for unknown request_id, doesn't crash."""
        response = {"request_id": "unknown-uuid", "status": "done"}

        call_count = 0
        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect

        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should not have crashed - no assertion needed beyond reaching here

    @pytest.mark.asyncio
    async def test_cancellation_resolves_pending_futures(self, app, mock_response_queue):
        """When demuxer is cancelled, pending futures get CancelledError."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        app.response_futures["pending-uuid"] = future

        mock_response_queue.get_nowait.side_effect = Empty

        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert future.done()
        with pytest.raises(asyncio.CancelledError):
            future.result()

    @pytest.mark.asyncio
    async def test_skips_already_done_futures(self, app, mock_response_queue):
        """Demuxer skips futures that are already resolved."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result({"already": "done"})
        request_id = "already-done-uuid"
        app.response_futures[request_id] = future

        response = {"request_id": request_id, "status": "late"}

        call_count = 0
        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect

        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Original result should be preserved
        assert future.result() == {"already": "done"}


# ---------------------------------------------------------------------------
# _sender_loop()
# ---------------------------------------------------------------------------

class TestSenderLoop:
    """Test outbound sender background task."""

    @pytest.mark.asyncio
    async def test_processes_queued_payloads(self, app, mock_shm, mock_command_ready_event):
        """Sender loop processes payloads from outbound queue."""
        mock_command_ready_event.is_set.return_value = False
        payload = {"action": "test_send"}

        await app._outbound_q.put(payload)

        task = asyncio.create_task(app._sender_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify data was written to shared memory
        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        assert size > 0

    @pytest.mark.asyncio
    async def test_cancellation_stops_loop(self, app):
        """Sender loop terminates cleanly on cancellation."""
        task = asyncio.create_task(app._sender_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.done()

    @pytest.mark.asyncio
    async def test_continues_after_send_error(self, app, mock_command_ready_event):
        """Sender loop continues after _send_one raises."""
        mock_command_ready_event.is_set.return_value = False

        # First payload will fail, second should succeed
        error_payload = {"action": "fail"}
        ok_payload = {"action": "ok"}

        call_count = 0
        original_frame_and_write = app._frame_and_write

        def patched_frame_and_write(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("write failed")
            original_frame_and_write(payload)

        app._frame_and_write = patched_frame_and_write

        await app._outbound_q.put(error_payload)
        await app._outbound_q.put(ok_payload)

        task = asyncio.create_task(app._sender_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have attempted both
        assert call_count >= 2


# ---------------------------------------------------------------------------
# _start_demuxer / _start_sender
# ---------------------------------------------------------------------------

class TestStartHelpers:
    """Test _start_demuxer and _start_sender helper methods."""

    @pytest.mark.asyncio
    async def test_start_demuxer_creates_task(self, app):
        """_start_demuxer creates a new demuxer_task."""
        app._start_demuxer()
        assert app.demuxer_task is not None
        assert not app.demuxer_task.done()
        app.demuxer_task.cancel()
        try:
            await app.demuxer_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_start_demuxer_noop_if_running(self, app):
        """_start_demuxer does nothing if task is still running."""
        app._start_demuxer()
        first_task = app.demuxer_task

        app._start_demuxer()
        assert app.demuxer_task is first_task

        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_start_sender_creates_task(self, app):
        """_start_sender creates a new sender task."""
        app._start_sender()
        assert app._sender_task is not None
        assert not app._sender_task.done()
        app._sender_task.cancel()
        try:
            await app._sender_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_start_sender_noop_if_running(self, app):
        """_start_sender does nothing if task is still running."""
        app._start_sender()
        first_task = app._sender_task

        app._start_sender()
        assert app._sender_task is first_task

        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Dynamic port propagation
# ---------------------------------------------------------------------------

class TestDynamicPort:
    """Tests for dynamic port propagation through app.start()."""

    @pytest.mark.asyncio
    async def test_start_stores_ws_port(self, app, mock_shm):
        """app.start() should store the port returned by websocket_manager.start()."""
        text_handler = MagicMock()

        with patch("app.WebSocketManager") as MockWSM:
            mock_ws = AsyncMock()
            mock_ws.start = AsyncMock(return_value=9876)
            mock_ws.speech_handler = None
            MockWSM.return_value = mock_ws

            await app.start("127.0.0.1", 0, text_handler, start_websocket=True)

            assert app.ws_port == 9876

    @pytest.mark.asyncio
    async def test_start_without_websocket_stores_zero(self, app, mock_shm):
        """When start_websocket=False, ws_port should be 0."""
        text_handler = MagicMock()

        with patch("app.WebSocketManager") as MockWSM:
            mock_ws = AsyncMock()
            mock_ws.speech_handler = None
            MockWSM.return_value = mock_ws

            await app.start("127.0.0.1", 0, text_handler, start_websocket=False)

            assert app.ws_port == 0
