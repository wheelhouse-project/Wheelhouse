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
import gc
import logging
import pickle
import struct
import time
from multiprocessing import Event, Queue
from queue import Empty
from unittest.mock import Mock, AsyncMock, MagicMock, patch, PropertyMock

import pytest


class AliasingDeepcopyValue:
    """Picklable value whose __deepcopy__ returns itself.

    wh-overlay-slow-uia-stale-badges.14.30: a payload value like this
    passes _serialize_final_payload but defeats a copy.deepcopy-based
    snapshot -- the "copy" is the same object, so a caller mutation
    after acceptance changes the delivered bytes. Module-level so
    pickle can resolve it by qualified name.
    """

    def __init__(self, value):
        self.value = value

    def __deepcopy__(self, memo):
        return self


_live_reducer_registry = {}


def _resolve_live_reducer(key, _serialized_value):
    """Reduction callable that returns the LIVE registered object."""
    return _live_reducer_registry[key]


class ReducerAliasValue:
    """Picklable value whose __reduce__ resolves to a live shared object.

    wh-overlay-slow-uia-stale-badges.14.32: pickle.loads executes this
    reduction callable, so a loads-based snapshot hands back the
    caller's live object, and any re-serialization at delivery time
    embeds the value as it is THEN -- the serialized args tuple carries
    self.value at each pickling. Module-level so pickle can resolve the
    callable by qualified name.
    """

    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __reduce__(self):
        return (_resolve_live_reducer, (self.key, self.value))


class DeepcopyRaisesValue:
    """Picklable value whose __deepcopy__ raises.

    wh-overlay-slow-uia-stale-badges.14.30: this value is valid for the
    pickle-based transport, but a copy.deepcopy-based snapshot raises on
    it, so send_command breaks its accepted/dropped boolean contract and
    send_request fails before registering its future.
    """

    def __init__(self, value):
        self.value = value

    def __deepcopy__(self, memo):
        raise RuntimeError("deepcopy is not supported by this value")


class UnhashableStrAction(str):
    """Picklable str subclass that is not hashable.

    wh-overlay-slow-uia-stale-badges.14.36: isinstance(action, str)
    admits this value, and the frozenset membership test then raises
    TypeError on its disabled hash. Module-level so pickle can resolve
    the class by qualified name.
    """

    __hash__ = None  # type: ignore[assignment]


class HashRaisesStrAction(str):
    """Picklable str subclass whose hash raises its own exception type.

    wh-overlay-slow-uia-stale-badges.14.36: the membership test
    propagates whatever the subclass hash raises, so an except-TypeError
    guard would be an incomplete fix; only an exact-type classifier
    avoids calling the subclass hash at all.
    """

    def __hash__(self):
        raise RuntimeError("hash is not supported by this value")


class EqRaisesStrAction(str):
    """Picklable str subclass whose equality raises.

    wh-overlay-slow-uia-stale-badges.14.43: stored as a dict key, it
    keeps the built-in str hash, so a later exact-str lookup or insert
    that probes its bucket calls the stored key's __eq__ inside the
    rich comparison -- and raises out of a bare payload.get or
    payload[...] = ... . Module-level so pickle can resolve it.
    """

    __hash__ = str.__hash__

    def __eq__(self, other):
        raise RuntimeError("eq is not supported by this value")


class DeadlineMutatingValue:
    """Value whose __reduce__ rewrites the payload's delivery deadline.

    wh-overlay-slow-uia-stale-badges.14.44: reducers run while
    _serialize_final_payload pickles the payload, AFTER the sender
    stamped the deadline and BEFORE the queued dict copy. Overwriting
    an existing key keeps the dict's size and insertion order, so
    pickling continues cleanly while the queued copy takes the
    mutated deadline.
    """

    def __init__(self, target):
        self.target = target

    def __reduce__(self):
        self.target["_delivery_deadline_monotonic"] = time.monotonic() + 3600.0
        return (str, ("x",))


class AbandonMutatingValue:
    """Value whose __reduce__ marks the payload abandoned.

    wh-overlay-slow-uia-stale-badges.14.46: the .14.44 restamp covers
    trace_id, deadline, and anchor, but _delivery_abandoned was still
    copied from the post-serialization caller dict -- and the sender
    reads it as a cancellation, silently dropping an accepted command.
    """

    def __init__(self, target):
        self.target = target

    def __reduce__(self):
        self.target["_delivery_abandoned"] = True
        return (str, ("x",))


class RaisesOnTruthValue(str):
    """Picklable str subclass whose truthiness raises.

    wh-overlay-slow-uia-stale-badges.14.49: the sender's diagnostic
    sites render the request id as `payload.get("request_id") or "-"`,
    so a value whose __bool__ raises breaks the milestone even when no
    dict key is poisoned.
    """

    def __bool__(self):
        raise RuntimeError("truthiness is not supported by this value")


class EnvelopeControlRewriter:
    """Value whose __reduce__ rewrites its containing request envelope.

    wh-overlay-slow-uia-stale-badges.14.50: send_request pickles the
    live shared envelope, so a reducer on a value inside params can
    walk gc.get_referrers back to the envelope and overwrite the
    sender's control metadata in place -- corrupting both the frame
    (for keys pickle has not reached yet) and the queued entry.
    """

    def __init__(self, field, value):
        self.field = field
        self.value = value

    def __reduce__(self):
        params = next(
            (
                ref for ref in gc.get_referrers(self)
                if isinstance(ref, dict) and ref.get("rewriter") is self
            ),
            None,
        )
        if params is not None:
            envelope = next(
                (
                    ref for ref in gc.get_referrers(params)
                    if isinstance(ref, dict) and "_delivery_deadline_monotonic" in ref
                ),
                None,
            )
            if envelope is not None:
                envelope[self.field] = self.value
        return (str, ("x",))


class AbandonSwappingRewriter:
    """Value whose __reduce__ fabricates a cancellation mark.

    wh-overlay-slow-uia-stale-badges.14.52: adding a key alone would
    change the dict's size mid-pickle, but deleting an
    already-serialized control and adding the mark keeps the size, so
    pickling finishes cleanly and the live envelope comes back marked
    as cancelled without the caller ever cancelling.
    """

    def __reduce__(self):
        params = next(
            (
                ref for ref in gc.get_referrers(self)
                if isinstance(ref, dict) and ref.get("rewriter") is self
            ),
            None,
        )
        if params is not None:
            envelope = next(
                (
                    ref for ref in gc.get_referrers(params)
                    if isinstance(ref, dict) and "_pipeline_elapsed_anchor" in ref
                ),
                None,
            )
            if envelope is not None:
                del envelope["_pipeline_elapsed_anchor"]
                envelope["_delivery_abandoned"] = True
        return (str, ("x",))


class ExplodingFiniteFloat(float):
    """Finite float subclass whose comparison raises.

    wh-overlay-slow-uia-stale-badges.14.53: it passes isinstance and
    math.isfinite, so the request was accepted and queued; the raise
    then came from asyncio.wait_for's own `timeout <= 0` test, after
    the payload was already in the outbound queue.
    """

    def __le__(self, other):
        raise RuntimeError("comparison exploded")


class ExplodingFloatConversionInt(int):
    """Int subclass whose float conversion raises."""

    def __float__(self):
        raise RuntimeError("float conversion exploded")


class FormatBombAction(str):
    """Picklable str subclass whose format conversion raises.

    wh-overlay-slow-uia-stale-badges.14.58: the sender interpolated the
    caller's action into the queue-full IpcDeliveryError message and
    into the timeout log. An f-string calls format(), so this shape
    replaced both documented outcomes with its own exception.
    """

    def __format__(self, spec):
        raise RuntimeError("action formatting exploded")


class AnchorKeyPoisoningRewriter:
    """Value whose __reduce__ re-keys the envelope during serialization.

    wh-overlay-slow-uia-stale-badges.14.57: it deletes the
    already-encoded _pipeline_elapsed_anchor and stores a hash-colliding
    key with the same value whose __eq__ raises. The dict's size is
    unchanged, so pickling completes, and the sender's post-freeze
    restamp then probed the poisoned key.
    """

    def __reduce__(self):
        params = next(
            (
                ref for ref in gc.get_referrers(self)
                if isinstance(ref, dict) and ref.get("rewriter") is self
            ),
            None,
        )
        if params is not None:
            envelope = next(
                (
                    ref for ref in gc.get_referrers(params)
                    if isinstance(ref, dict) and "_pipeline_elapsed_anchor" in ref
                ),
                None,
            )
            if envelope is not None:
                del envelope["_pipeline_elapsed_anchor"]
                envelope[EqRaisesStrAction("_pipeline_elapsed_anchor")] = 0.0
        return (str, ("x",))


class NameBombMeta(type):
    """Metaclass whose class-name access raises.

    wh-overlay-slow-uia-stale-badges.14.61: the sender's controlled-drop
    handlers and its invalid-timeout reject path all rendered
    type(value).__name__ while building a diagnostic, so a caller value
    of this shape replaced each documented outcome with its own
    exception.
    """

    @property
    def __name__(cls):
        raise RuntimeError("type name access exploded")


class NameBombTimeout(metaclass=NameBombMeta):
    """Non-numeric timeout whose class name refuses to render."""


class NameBombStampPayload(dict, metaclass=NameBombMeta):
    """Payload that refuses the sender's stamping writes and its own name.

    The stamping try promises a False return for a caller mapping it
    cannot stamp, so its warning must not read the type name directly.
    """

    def __setitem__(self, key, value):
        raise RuntimeError("stamp write exploded")


class NameBombQueuedPayload(dict, metaclass=NameBombMeta):
    """Payload that accepts stamping but refuses the queued copy.

    dict(payload) takes the fast path for a dict subclass unless the
    subclass supplies its own __iter__, so both are overridden: the
    slow path then calls keys() and raises inside the queued-copy try.
    """

    def __iter__(self):
        raise RuntimeError("payload iteration exploded")

    def keys(self):
        raise RuntimeError("key enumeration exploded")


class ExplodingReprTimeout:
    """Non-numeric timeout whose repr raises.

    The reject path itself rendered {effective_timeout!r}, so building
    the error message raised instead of answering ValueError.
    """

    def __repr__(self):
        raise RuntimeError("repr exploded")


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

            await app.start("localhost", 8765, handler)

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

            await app.start("localhost", 8765, handler)

            # Should pass process_transcription as text_handler
            call_kwargs = mock_ws_cls.call_args
            assert call_kwargs[1]["text_handler"] is handler.process_transcription
            # Should also store full speech_handler
            assert mock_ws_instance.speech_handler is handler

    @pytest.mark.asyncio
    async def test_start_connects_the_websocket(self, app):
        """start() calls websocket_manager.start().

        wh-in-process-capture-removal: there was a start_websocket
        flag, and the in-process mode passed False to it. The mode is
        gone, so the connection is unconditional.
        """
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("myhost", 9999, handler)

            mock_ws_instance.start.assert_awaited_once_with("myhost", 9999)

    @pytest.mark.asyncio
    async def test_start_launches_demuxer_task(self, app):
        """start() creates the demuxer background task."""
        handler = Mock()
        with patch("app.WebSocketManager") as mock_ws_cls:
            mock_ws_instance = MagicMock()
            mock_ws_instance.start = AsyncMock()
            mock_ws_cls.return_value = mock_ws_instance

            await app.start("localhost", 8765, handler)

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

            await app.start("localhost", 8765, handler)

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
    async def test_raises_on_broken_event_object(self, app, mock_command_ready_event):
        """An unreadable event raises IpcEventError instead of masquerading as a timeout.

        wh-overlay-slow-uia-stale-badges.14.2: returning False here made
        _send_one blame a delivery deadline for drops caused by a dead event.
        """
        from app import IpcEventError

        mock_command_ready_event.is_set.side_effect = OSError("event closed")

        with pytest.raises(IpcEventError):
            await app._await_event_state(desired_set=True, timeout_s=0.05)

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
        """send_command with dict enqueues the payload's content.

        The queued entry also carries _frame, the delivered bytes frozen
        at acceptance (wh-overlay-slow-uia-stale-badges.14.32).
        """
        payload = {"action": "click", "params": {"x": 100}}

        await app.send_command(payload)

        item = app._outbound_q.get_nowait()
        frame = item.pop("_frame")
        assert pickle.loads(frame) == payload
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

    @pytest.mark.asyncio
    async def test_rejects_final_serialized_payload_before_sender_queue(self, app):
        """A fire-and-forget insertion cannot silently overflow SharedMemory.

        The command engine awaits ``send_command``, so raising here makes the
        current pattern step fail before the background sender can only log
        the overflow and drop the insertion.
        """
        high_utf8_text = chr(0x4E00) * 30_000
        payload = {
            "action": "intelligent_insert_text",
            "params": {"insertion_string": high_utf8_text},
        }

        with pytest.raises(ValueError, match="final UI payload"):
            await app.send_command(payload)

        assert app._outbound_q.empty()


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
        # .14.57: params travel in the frozen frame, not in the queued
        # sender metadata.
        assert pickle.loads(item["_frame"])["params"] == {"format": "text"}
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
# Delivery protection (wh-overlay-slow-uia-stale-badges.5)
# ---------------------------------------------------------------------------

class TestDeliveryProtection:
    """The sender never destroys an unread command, and stale payloads expire.

    wh-overlay-slow-uia-stale-badges.5: _send_one used to overwrite the
    shared-memory frame after a fixed one-second wait even when the Input
    process had not read the previous command. The observed 2026-08-11 case
    destroyed a queued start_overlay_walk with no trace beyond a DEBUG line.
    """

    @pytest.mark.asyncio
    async def test_send_one_never_overwrites_unread_command(self, app, mock_shm, mock_command_ready_event):
        """An unread command in the frame survives a later send attempt."""
        app._frame_and_write({"action": "first_unread"})
        mock_command_ready_event.is_set.return_value = True  # consumer never reads

        second = {
            "action": "second",
            "_delivery_deadline_monotonic": time.monotonic() + 0.05,
        }
        await app._send_one(second)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data == {"action": "first_unread"}
        mock_command_ready_event.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_one_drops_expired_payload(self, app, mock_shm, mock_command_ready_event):
        """A payload whose deadline has passed is not written even when the frame is free."""
        mock_command_ready_event.is_set.return_value = False
        stale = {
            "action": "stale",
            "_delivery_deadline_monotonic": time.monotonic() - 0.01,
        }

        await app._send_one(stale)

        assert struct.unpack(">I", bytes(mock_shm.buf[:4]))[0] == 0
        mock_command_ready_event.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_timed_out_request_is_never_delivered_late(self, app, mock_shm, mock_command_ready_event):
        """A request the caller gave up on is not written after the consumer recovers."""
        mock_command_ready_event.is_set.return_value = True  # consumer wedged
        sender = asyncio.create_task(app._sender_loop())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await app.send_request("stale_click", timeout_s=0.1)
            await asyncio.sleep(0.2)  # the payload deadline is now clearly past
            mock_command_ready_event.is_set.return_value = False  # consumer recovers
            await asyncio.sleep(0.3)
            assert b"stale_click" not in bytes(mock_shm.buf)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_expired_click_is_dropped_and_no_late_response_fires(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """A click still queued when its caller gives up is dropped, and the
        late-response callback registered for it never fires.

        David decided this on 2026-08-16; the decision and its limits are a
        comment on wh-overlay-slow-uia-stale-badges.11. Nobody chose the
        behaviour deliberately at first -- it appeared when two features met
        in send_request, the delivery protection
        (wh-overlay-slow-uia-stale-badges.5) and the late-response registry
        (wh-overlay-slow-uia-stale-badges.6). A click that missed its moment
        must not be pressed into a window that has since changed, so the
        payload is dropped at its deadline, and a request that was never
        delivered can never produce an answer for the callback to catch.

        The registration is deliberately LEFT in place and armed. It is
        bounded: it expires after the caller's timeout plus the 15 second
        grace. This asserts both halves, because the drop alone does not
        prove what the callback does.

        The limit of the decision, which this test does NOT cover: a request
        already written to the Input process can still run late, because
        Input-side expiry is not built yet. That work is
        wh-overlay-slow-uia-stale-badges.11.
        """
        calls = []
        mock_command_ready_event.is_set.return_value = True  # consumer wedged
        sender = asyncio.create_task(app._sender_loop())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await app.send_request(
                    "click_element", timeout_s=0.1,
                    on_late_response=calls.append,
                )
            await asyncio.sleep(0.2)  # the payload deadline is now clearly past
            mock_command_ready_event.is_set.return_value = False  # consumer recovers
            await asyncio.sleep(0.3)

            assert b"click_element" not in bytes(mock_shm.buf)
            assert calls == []
            entries = list(app._late_response_callbacks.values())
            assert len(entries) == 1
            assert entries[0].armed is True
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_send_command_drops_newest_when_queue_full(self, app, caplog):
        """A full outbound queue drops the new fire-and-forget payload with an ERROR log."""
        from app import _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        with caplog.at_level(logging.ERROR):
            await app.send_command("overflow")  # must not raise

        assert app._outbound_q.qsize() == _OUTBOUND_QUEUE_MAX
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_send_request_raises_delivery_error_when_queue_full(self, app):
        """A request against a full queue fails immediately, not after its timeout."""
        from app import IpcDeliveryError, _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        start = time.monotonic()
        with pytest.raises(IpcDeliveryError):
            await app.send_request("click_element", timeout_s=5.0)
        assert time.monotonic() - start < 1.0
        assert app.response_futures == {}

    @pytest.mark.asyncio
    async def test_send_command_stamps_delivery_deadline(self, app):
        """Fire-and-forget payloads carry a delivery deadline."""
        before = time.monotonic()
        await app.send_command("type_text", {"text": "hi"})

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before + 4.0 < deadline < before + 6.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("poisoned_key", [
        "action", "trace_id", "_delivery_deadline_monotonic", "_frame",
    ])
    async def test_send_command_drops_poisoned_key_payload(
        self, app, poisoned_key,
    ):
        """wh-overlay-slow-uia-stale-badges.14.43: an exact dict whose
        stored key raises from __eq__ passes the container check, then
        raises from send_command's own lookups and inserts (the
        trace_id write, the TTL action lookups, the metadata writes,
        the queued _frame insert) -- BEFORE the reader's envelope
        boundary could drop the frame. The raw overload must reach a
        controlled acceptance/drop decision instead of raising to the
        Logic caller."""
        payload: dict = {"params": {}}
        if poisoned_key != "action":
            payload["action"] = "type_text"
        payload[EqRaisesStrAction(poisoned_key)] = "x"

        result = await app.send_command(payload)

        assert result is False
        assert app._outbound_q.qsize() == 0

    @pytest.mark.asyncio
    async def test_send_command_queued_deadline_matches_frame_under_reducer_mutation(
        self, app,
    ):
        """wh-overlay-slow-uia-stale-badges.14.44: a later value's
        __reduce__ rewrites the payload's stamped deadline while the
        frame is being pickled. The frame froze the honest deadline,
        but the queued dict copy ran AFTER serialization and took the
        mutated one -- and _send_one reads the queued deadline, so the
        stale-delivery drop was bypassed. The queued entry must carry
        the sender-stamped metadata, never a post-serialization copy
        from the live caller envelope."""
        before = time.monotonic()
        payload = {"action": "type_text", "params": {}}
        payload["_delivery_deadline_monotonic"] = 0.0
        payload["mutator"] = DeadlineMutatingValue(payload)

        result = await app.send_command(payload)

        assert result is True
        queued = app._outbound_q.get_nowait()
        frame = pickle.loads(queued["_frame"])
        assert (
            queued["_delivery_deadline_monotonic"]
            == frame["_delivery_deadline_monotonic"]
        )
        # The sender-stamped 5s TTL, not the reducer's +3600.
        assert queued["_delivery_deadline_monotonic"] < before + 60.0

    @pytest.mark.asyncio
    async def test_send_command_queued_entry_ignores_reducer_abandonment(
        self, app, mock_command_ready_event,
    ):
        """wh-overlay-slow-uia-stale-badges.14.46: a value's __reduce__
        sets _delivery_abandoned on the caller dict while the frame is
        being pickled. The frame froze the honest False, but the queued
        copy ran AFTER serialization and took True -- and _send_one
        reads the queued mark, so the accepted command was silently
        dropped as reason=cancelled. send_command entries have no
        cancellation path (the mark belongs to send_request's shared
        envelope); the queued entry must start unmarked and the frame
        must be delivered."""
        mock_command_ready_event.is_set.return_value = False
        payload = {"action": "type_text", "params": {}}
        payload["_delivery_abandoned"] = False
        payload["mutator"] = AbandonMutatingValue(payload)

        result = await app.send_command(payload)

        assert result is True
        queued = app._outbound_q.get_nowait()
        assert not queued.get("_delivery_abandoned")

        await app._send_one(queued)
        mock_command_ready_event.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_command_poisoned_request_id_key_is_a_controlled_drop(
        self, app,
    ):
        """wh-overlay-slow-uia-stale-badges.14.49: a poisoned
        request_id key used to survive acceptance (none of the sender's
        own inserts probe its bucket) and then raise inside the
        delivery milestone, which renders payload.get("request_id").
        Removing the caller's request id from the queued entry probes
        that bucket at acceptance instead, so the payload takes the
        same controlled producer drop as every other poisoned-key
        envelope (.14.43) rather than failing mid-delivery."""
        payload: dict = {"action": "type_text", "params": {}}
        payload[EqRaisesStrAction("request_id")] = "rq-poison"

        result = await app.send_command(payload)

        assert result is False
        assert app._outbound_q.qsize() == 0

    @pytest.mark.asyncio
    async def test_send_command_raising_request_id_value_still_delivers(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """wh-overlay-slow-uia-stale-badges.14.49: every sender
        diagnostic renders `payload.get("request_id") or "-"`, so a
        request id whose truthiness raises broke the delivery milestone
        and left an accepted frame with neither IPC_SENT nor a
        classified IPC_DROPPED. A fire-and-forget payload has no
        request-response contract, so the queued entry must carry no
        caller request id at all and the frame must still be
        delivered."""
        mock_command_ready_event.is_set.return_value = False
        payload: dict = {"action": "type_text", "params": {}}
        payload["request_id"] = RaisesOnTruthValue("rq-poison")

        result = await app.send_command(payload)

        assert result is True
        queued = app._outbound_q.get_nowait()
        assert "request_id" not in queued
        await app._send_one(queued)

        # The frame reached shared memory and the consumer was signaled.
        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        assert pickle.loads(bytes(mock_shm.buf[4:4 + size]))["action"] == "type_text"
        mock_command_ready_event.set.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", [
        "_delivery_deadline_monotonic", "request_id", "trace_id",
        "_pipeline_elapsed_anchor",
    ])
    async def test_send_request_controls_survive_envelope_rewriting_reducer(
        self, app, field,
    ):
        """wh-overlay-slow-uia-stale-badges.14.50: send_request pickles
        its LIVE shared envelope, so a reducer on a value inside params
        can walk gc.get_referrers back to that envelope and overwrite
        the sender's control metadata in place. With params inserted
        before the metadata, pickle had not yet encoded those keys, so
        the rewrite corrupted the frame AND the queued entry: a
        one-second request could be delivered for an hour, or resolve
        against a request id the future map does not hold. The trusted
        metadata must be frozen in the frame and restamped on the
        queued envelope."""
        poison = 99999.0 if field.endswith(("monotonic", "anchor")) else "poisoned"
        params = {"rewriter": EnvelopeControlRewriter(field, poison)}
        before = time.monotonic()

        task = asyncio.create_task(
            app.send_request("press_key_action", params, timeout_s=1.0)
        )
        await asyncio.sleep(0)
        queued = await app._outbound_q.get()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

        frame = pickle.loads(queued["_frame"])
        assert frame[field] == queued[field]
        if field == "_delivery_deadline_monotonic":
            # The caller's own 1s timeout, not the reducer's +99999.
            assert queued[field] < before + 60.0
        else:
            assert queued[field] != poison

    @pytest.mark.asyncio
    async def test_send_request_ignores_reducer_fabricated_cancellation(
        self, app, mock_command_ready_event,
    ):
        """wh-overlay-slow-uia-stale-badges.14.52: the .14.50 restamp
        leaves _delivery_abandoned alone because only the caller's
        cancellation writes it -- but a reducer runs synchronously
        during serialization, before any await. Deleting an
        already-pickled control and adding the mark keeps the dict's
        size, so pickling finishes and the queued envelope arrives
        marked as cancelled. The sender then drops an accepted request
        whose caller never cancelled it."""
        mock_command_ready_event.is_set.return_value = False
        params = {"rewriter": AbandonSwappingRewriter()}

        task = asyncio.create_task(
            app.send_request("press_key_action", params, timeout_s=1.0)
        )
        await asyncio.sleep(0)
        queued = await app._outbound_q.get()
        # Read the mark BEFORE cancelling: the caller's own cancellation
        # writes it legitimately, and that write would hide the forgery.
        fabricated = queued.get("_delivery_abandoned")
        task.cancel()
        try:
            await task
        except BaseException:
            pass

        assert not fabricated

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["explicit", "default"])
    @pytest.mark.parametrize("bad_timeout", [
        ExplodingFiniteFloat(5.0),
        ExplodingFloatConversionInt(5),
        ExplodingReprTimeout(),
    ], ids=["raising-comparison", "raising-float-conversion", "raising-repr"])
    async def test_send_request_rejects_hostile_timeout_shapes(
        self, app, source, bad_timeout,
    ):
        """wh-overlay-slow-uia-stale-badges.14.53: isinstance accepts
        subclasses, so a finite float subclass whose comparison raises
        passed validation, was registered and QUEUED, and only then
        raised from asyncio.wait_for's own `timeout <= 0` test --
        leaving a deliverable payload with no caller to receive its
        response. The sibling shapes raise from math.isfinite (an int
        subclass whose float conversion fails) and from the reject
        path's own {!r} render. All three must answer the documented
        ValueError with nothing accepted."""
        if source == "explicit":
            with pytest.raises(ValueError):
                await app.send_request("type_text", {}, timeout_s=bad_timeout)
        else:
            app.response_timeout_s = bad_timeout
            with pytest.raises(ValueError):
                await app.send_request("type_text", {})

        assert app._outbound_q.qsize() == 0
        assert app.response_futures == {}
        assert app._request_trace_meta == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["explicit", "default"])
    async def test_send_request_rejects_huge_integer_timeout(self, app, source):
        """wh-overlay-slow-uia-stale-badges.14.51: math.isfinite raises
        OverflowError for an integer too large to convert to float, so
        a huge integer timeout escaped the documented ValueError
        contract that every other invalid timeout shape uses."""
        huge = 10 ** 400
        if source == "explicit":
            with pytest.raises(ValueError):
                await app.send_request("type_text", {}, timeout_s=huge)
        else:
            app.response_timeout_s = huge
            with pytest.raises(ValueError):
                await app.send_request("type_text", {})

        assert app._outbound_q.qsize() == 0
        assert app.response_futures == {}
        assert app._request_trace_meta == {}

    @pytest.mark.asyncio
    async def test_send_command_frame_carries_no_caller_request_id(self, app):
        """wh-overlay-slow-uia-stale-badges.14.56: .14.49 popped the
        caller's request id from the QUEUED entry, which is built after
        the frame is frozen -- so the delivered bytes still carried it.
        input_proc canonicalizes a request identity from those bytes, so
        a fire-and-forget command answered, and an id that collides with
        a live request settles that caller's future with the wrong
        action's response."""
        loop = asyncio.get_running_loop()
        app.response_futures["raw-id"] = loop.create_future()
        payload = {"action": "type_text", "params": {}, "request_id": "raw-id"}

        assert await app.send_command(payload) is True

        queued = await app._outbound_q.get()
        assert "request_id" not in queued
        assert "request_id" not in pickle.loads(queued["_frame"])

    @pytest.mark.asyncio
    async def test_send_command_queue_full_drop_survives_rewritten_trace(self, app):
        """wh-overlay-slow-uia-stale-badges.14.58: the queue-full branch
        reads the LIVE payload, not the sanitized queued entry, so a
        reducer-planted raising-truthiness trace_id turned the
        documented False return into a leaked exception."""
        from app import _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        payload = {
            "action": "type_text",
            "params": {
                "rewriter": EnvelopeControlRewriter(
                    "trace_id", RaisesOnTruthValue("boom"),
                ),
            },
        }
        assert await app.send_command(payload) is False

    @pytest.mark.asyncio
    async def test_send_request_queue_full_raises_delivery_error_for_hostile_action(
        self, app,
    ):
        """wh-overlay-slow-uia-stale-badges.14.58: the IpcDeliveryError
        message interpolated the caller's action, and an f-string calls
        format(), so a str subclass with a raising __format__ replaced
        the documented overload exception."""
        from app import IpcDeliveryError, _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        with pytest.raises(IpcDeliveryError):
            await app.send_request(FormatBombAction("type_text"), {}, timeout_s=5.0)

    @pytest.mark.asyncio
    async def test_send_request_timeout_survives_hostile_action(self, app):
        """wh-overlay-slow-uia-stale-badges.14.58: the timeout log
        interpolated the same caller action, so the hostile shape
        replaced asyncio.TimeoutError on the ordinary timeout path."""
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(FormatBombAction("type_text"), {}, timeout_s=0.01)

    @pytest.mark.asyncio
    async def test_send_request_queued_entry_survives_poisoned_control_key(self, app):
        """wh-overlay-slow-uia-stale-badges.14.57: send_request wrote
        its control metadata back onto the LIVE envelope after the
        frame was frozen. A reducer can delete an already-encoded
        control and store a hash-colliding key with the same value
        whose __eq__ raises, so the restamp probed that key and leaked
        RuntimeError -- for a payload that was transport-valid."""
        params = {"rewriter": AnchorKeyPoisoningRewriter()}

        task = asyncio.create_task(
            app.send_request("press_key_action", params, timeout_s=1.0)
        )
        await asyncio.sleep(0)
        # get_nowait, never an awaited get: a send_request that raises
        # before the enqueue would otherwise hang this test instead of
        # failing it.
        assert app._outbound_q.qsize() == 1
        queued = app._outbound_q.get_nowait()
        task.cancel()
        try:
            await task
        except BaseException:
            pass

        assert queued["_pipeline_elapsed_anchor"] == 0.0
        assert queued["action"] == "press_key_action"

    @pytest.mark.asyncio
    async def test_send_request_queued_action_cannot_be_forged(self, app):
        """wh-overlay-slow-uia-stale-badges.14.57: .14.50 restamped
        every control except the action, so a reducer could make the
        queued entry and every sender diagnostic name an action the
        frozen bytes do not carry."""
        params = {"rewriter": EnvelopeControlRewriter("action", "forged_action")}

        task = asyncio.create_task(
            app.send_request("press_key_action", params, timeout_s=1.0)
        )
        await asyncio.sleep(0)
        assert app._outbound_q.qsize() == 1
        queued = app._outbound_q.get_nowait()
        task.cancel()
        try:
            await task
        except BaseException:
            pass

        assert queued["action"] == "press_key_action"

    @pytest.mark.asyncio
    async def test_send_command_stamp_drop_survives_hostile_type_name(self, app):
        """A payload that cannot be stamped still returns False.

        wh-overlay-slow-uia-stale-badges.14.61: the stamping try promises
        a False return for a caller mapping it cannot stamp, but its
        warning rendered type(payload).__name__, so a hostile metaclass
        replaced that promise with its own exception.
        """
        payload = NameBombStampPayload({"action": "press_key_action"})

        assert await app.send_command(payload) is False
        assert app._outbound_q.qsize() == 0

    @pytest.mark.asyncio
    async def test_send_command_queued_drop_survives_hostile_type_name(self, app):
        """A payload whose queued copy fails still returns False.

        wh-overlay-slow-uia-stale-badges.14.61: the second controlled-drop
        handler carried the same unsafe type-name lookup as the first.
        """
        payload = NameBombQueuedPayload({"action": "press_key_action"})

        assert await app.send_command(payload) is False
        assert app._outbound_q.qsize() == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["explicit", "default"])
    async def test_send_request_rejects_a_hostile_type_name_timeout(self, app, source):
        """An unrenderable timeout type still raises the documented ValueError.

        wh-overlay-slow-uia-stale-badges.14.61: the .14.53 reject path
        names a non-numeric timeout by its type, so a metaclass whose
        __name__ raises replaced ValueError with its own exception and
        left the caller with a different failure than the contract
        documents.
        """
        hostile = NameBombTimeout()
        if source == "explicit":
            with pytest.raises(ValueError):
                await app.send_request("get_clipboard", timeout_s=hostile)
        else:
            app.response_timeout_s = hostile
            with pytest.raises(ValueError):
                await app.send_request("get_clipboard")

        assert app._outbound_q.qsize() == 0
        assert app.response_futures == {}
        assert app._request_trace_meta == {}

    @pytest.mark.asyncio
    async def test_send_command_snapshot_is_deep(self, app, mock_shm, mock_command_ready_event):
        """A nested params mutation after enqueue does not change delivered bytes.

        wh-overlay-slow-uia-stale-badges.14.18: dict(payload) copies only
        the top level. The caller's params dict stayed shared with the
        queued snapshot, so a mutation between acceptance and delivery
        changed the framed bytes the Input process received.
        """
        mock_command_ready_event.is_set.return_value = False
        params = {"text": "accepted"}
        await app.send_command("type_text", params)
        params["text"] = "mutated-after-enqueue"

        item = app._outbound_q.get_nowait()
        await app._send_one(item)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["params"]["text"] == "accepted"

    @pytest.mark.asyncio
    async def test_send_request_params_snapshot_is_deep(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """A caller mutation after enqueue does not change delivered bytes,
        and the shared envelope still carries the cancellation mark.

        wh-overlay-slow-uia-stale-badges.14.28: send_request shares the
        ENVELOPE with the queue on purpose (the .14.7 _delivery_abandoned
        mark travels through it), but "params": params aliased the
        caller's mutable graph into that envelope, so a mutation between
        acceptance and framing changed what the Input process received.
        """
        mock_command_ready_event.is_set.return_value = False
        params = {"nested": {"text": "accepted"}}
        task = asyncio.create_task(
            app.send_request("press_key_action", params, timeout_s=1.0),
        )
        await asyncio.sleep(0)  # let the request enqueue
        params["nested"]["text"] = "mutated-after-enqueue"

        item = app._outbound_q.get_nowait()
        await app._send_one(item)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["params"]["nested"]["text"] == "accepted"

        # The cancellation mark must still reach the QUEUED envelope:
        # the params snapshot may not un-share the envelope itself.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert item.get("_delivery_abandoned") is True

    @pytest.mark.asyncio
    async def test_send_command_snapshot_ignores_deepcopy_override(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """The snapshot must not trust a value's own __deepcopy__.

        wh-overlay-slow-uia-stale-badges.14.30: a picklable value whose
        __deepcopy__ returns itself defeated the deepcopy snapshot, so a
        caller mutation after acceptance changed the delivered bytes --
        the .14.18 defect through a different door.
        """
        mock_command_ready_event.is_set.return_value = False
        obj = AliasingDeepcopyValue("accepted")
        await app.send_command("type_text", {"obj": obj})
        obj.value = "mutated-after-enqueue"

        item = app._outbound_q.get_nowait()
        await app._send_one(item)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["params"]["obj"].value == "accepted"

    @pytest.mark.asyncio
    async def test_send_request_snapshot_ignores_deepcopy_override(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """The request-side params snapshot must not trust __deepcopy__.

        wh-overlay-slow-uia-stale-badges.14.30: same defect as the
        send_command case, through the .14.28 params-only snapshot.
        """
        mock_command_ready_event.is_set.return_value = False
        obj = AliasingDeepcopyValue("accepted")
        task = asyncio.create_task(
            app.send_request("press_key_action", {"obj": obj}, timeout_s=1.0),
        )
        await asyncio.sleep(0)  # let the request enqueue
        obj.value = "mutated-after-enqueue"

        item = app._outbound_q.get_nowait()
        await app._send_one(item)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["params"]["obj"].value == "accepted"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_send_command_frames_bytes_frozen_at_acceptance(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """Delivered bytes must be the bytes validated at acceptance.

        wh-overlay-slow-uia-stale-badges.14.32: a value whose __reduce__
        resolves through a live registry defeats any loads-based
        snapshot, and re-serializing at delivery embeds the value as it
        is then. The assertion is on the RAW frame bytes: unpickling
        would execute the reducer and hide the defect.
        """
        mock_command_ready_event.is_set.return_value = False
        obj = ReducerAliasValue("cmd-frozen", "accepted")
        _live_reducer_registry["cmd-frozen"] = obj
        try:
            await app.send_command("type_text", {"obj": obj})
            obj.value = "mutated-after-enqueue"

            item = app._outbound_q.get_nowait()
            await app._send_one(item)

            size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
            frame = bytes(mock_shm.buf[4:4 + size])
            assert b"accepted" in frame
            assert b"mutated-after-enqueue" not in frame
        finally:
            del _live_reducer_registry["cmd-frozen"]

    @pytest.mark.asyncio
    async def test_send_request_frames_bytes_frozen_at_acceptance(
        self, app, mock_shm, mock_command_ready_event,
    ):
        """Same freeze on the request side; cancellation mark still works.

        wh-overlay-slow-uia-stale-badges.14.32: the request envelope is
        shared with the queue on purpose, so the freeze must come from
        the frame bytes, not from copying the envelope.
        """
        mock_command_ready_event.is_set.return_value = False
        obj = ReducerAliasValue("req-frozen", "accepted")
        _live_reducer_registry["req-frozen"] = obj
        try:
            task = asyncio.create_task(
                app.send_request("press_key_action", {"obj": obj}, timeout_s=1.0),
            )
            await asyncio.sleep(0)  # let the request enqueue
            obj.value = "mutated-after-enqueue"

            item = app._outbound_q.get_nowait()
            await app._send_one(item)

            size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
            frame = bytes(mock_shm.buf[4:4 + size])
            assert b"accepted" in frame
            assert b"mutated-after-enqueue" not in frame

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert item.get("_delivery_abandoned") is True
        finally:
            del _live_reducer_registry["req-frozen"]

    @pytest.mark.asyncio
    async def test_send_command_accepts_value_whose_deepcopy_raises(self, app):
        """A transport-valid value must not break the boolean contract.

        wh-overlay-slow-uia-stale-badges.14.30: the value pickles fine,
        so the transport accepts it, but copy.deepcopy raised on it and
        send_command leaked the exception instead of returning its
        documented accepted/dropped boolean.
        """
        result = await app.send_command(
            "type_text", {"obj": DeepcopyRaisesValue("x")},
        )
        assert result is True
        assert app._outbound_q.qsize() == 1

    @pytest.mark.asyncio
    async def test_send_request_accepts_value_whose_deepcopy_raises(self, app):
        """send_request must enqueue a transport-valid value.

        wh-overlay-slow-uia-stale-badges.14.30: copy.deepcopy raised on
        the params snapshot, so the request failed before it registered
        its future.
        """
        task = asyncio.create_task(
            app.send_request(
                "press_key_action", {"obj": DeepcopyRaisesValue("x")},
                timeout_s=1.0,
            ),
        )
        await asyncio.sleep(0)  # let the request enqueue
        assert app._outbound_q.qsize() == 1
        assert len(app.response_futures) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_timeout",
        [float("inf"), float("nan"), float("-inf"), True, "5"],
    )
    async def test_send_request_rejects_non_finite_timeout(
        self, app, bad_timeout,
    ):
        """A non-finite or non-real timeout is rejected before enqueue.

        wh-overlay-slow-uia-stale-badges.14.34: an inf or NaN timeout
        stamped a non-finite delivery deadline, the sender dropped the
        payload as invalid_deadline, and asyncio.wait_for with a
        non-finite timeout never completed -- the caller waited forever
        with its response future and trace metadata stranded. The outer
        wait_for bounds a regression to a test failure instead of a
        suite hang.
        """
        with pytest.raises(ValueError):
            await asyncio.wait_for(
                app.send_request(
                    "press_key_action", {}, timeout_s=bad_timeout,
                ),
                timeout=2.0,
            )
        assert app._outbound_q.qsize() == 0
        assert app.response_futures == {}
        assert app._request_trace_meta == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_default", [float("inf"), float("nan")])
    async def test_send_request_rejects_non_finite_default_timeout(
        self, app, bad_default,
    ):
        """The constructor default is validated the same way, and a
        durable action does not mask a non-finite value through max().

        wh-overlay-slow-uia-stale-badges.14.34: max(inf, 120) and
        max(nan, 120) both keep the non-finite value, so durable
        actions were affected the same as stale-sensitive ones.
        """
        app.response_timeout_s = bad_default
        with pytest.raises(ValueError):
            await asyncio.wait_for(
                app.send_request("intelligent_insert_text", {}),
                timeout=2.0,
            )
        assert app._outbound_q.qsize() == 0
        assert app.response_futures == {}
        assert app._request_trace_meta == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [[], {}])
    async def test_send_command_accepts_unhashable_action(self, app, bad_action):
        """An unhashable action must not crash the producer.

        wh-overlay-slow-uia-stale-badges.14.35: the durable-set
        membership test raised TypeError for an unhashable action
        before the envelope could reach the reader-side validator
        (.14.33), breaking send_command's accepted/dropped boolean
        contract. The reader is the single action-shape validator; the
        producer accepts and queues the envelope.
        """
        result = await app.send_command({"action": bad_action, "params": {}})
        assert result is True
        assert app._outbound_q.qsize() == 1

    @pytest.mark.asyncio
    async def test_send_request_enqueues_unhashable_action(self, app):
        """send_request must queue an unhashable action, not raise.

        wh-overlay-slow-uia-stale-badges.14.35: the durable-TTL
        selection raised TypeError before the request could be queued
        for the reader's standard error response.
        """
        task = asyncio.create_task(
            app.send_request([], {}, timeout_s=1.0),
        )
        await asyncio.sleep(0)  # let the request enqueue
        assert app._outbound_q.qsize() == 1
        assert len(app.response_futures) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_action", [
        UnhashableStrAction("intelligent_insert_text"),
        HashRaisesStrAction("intelligent_insert_text"),
    ], ids=["hash-disabled", "hash-raises"])
    async def test_send_command_accepts_unhashable_str_subclass_action(
        self, app, bad_action,
    ):
        """A str subclass with a broken hash must not crash the producer.

        wh-overlay-slow-uia-stale-badges.14.36: isinstance(action, str)
        admitted the subclass, and the durable-set membership test then
        called its hash -- TypeError for a disabled hash, and whatever a
        raising hash throws. Only an exact str is looked up; a subclass
        takes the stale-sensitive TTL and travels to the reader.
        """
        result = await app.send_command({"action": bad_action, "params": {}})
        assert result is True
        assert app._outbound_q.qsize() == 1
        item = app._outbound_q.get_nowait()
        remaining = item["_delivery_deadline_monotonic"] - time.monotonic()
        assert remaining < 30.0  # stale-sensitive TTL, not the durable 120s

    @pytest.mark.asyncio
    async def test_send_request_enqueues_unhashable_str_subclass_action(
        self, app,
    ):
        """send_request must queue a broken-hash str subclass, not raise.

        wh-overlay-slow-uia-stale-badges.14.36: the durable-TTL
        selection called the subclass hash before the request could be
        queued for the reader's standard error response.
        """
        task = asyncio.create_task(
            app.send_request(
                UnhashableStrAction("intelligent_insert_text"),
                {},
                timeout_s=1.0,
            ),
        )
        await asyncio.sleep(0)  # let the request enqueue
        assert app._outbound_q.qsize() == 1
        assert len(app.response_futures) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_send_request_deadline_matches_caller_timeout(self, app):
        """A request payload expires from the send queue when its caller stops waiting."""
        before = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("walk", timeout_s=0.05)

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before < deadline < before + 0.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action",
        ["click_element", "click_snapshot_item", "start_overlay_walk"],
    )
    async def test_send_request_stamps_the_awaited_window_for_click_and_walk(
        self, app, action,
    ):
        """The Input process cannot know how long this caller waits.

        wh-watchdog-stall-window. The Input process runs one command loop and
        watches for a dispatch that holds it too long, but it holds no
        reference to click_config, so its limit was a fixed constant. Both
        keys behind these three actions validate well above that constant --
        response_timeout_ms to 10000 and screen_read_timeout_ms to 60000 --
        so an operator who raises either one gets a stall reported for work
        still inside the window nobody has given up on. The window is stamped
        HERE because send_request is the one place that holds it.

        click_snapshot_item is here from review finding .1.1: the badge
        click is awaited for the same response_timeout_ms as click_element
        and reaches the same verification block through the same shared
        executor, so leaving it out left the false report in place on the
        numbered-overlay path.

        The assertion reads the FRAME rather than the queued entry: the frame
        is what delivery writes, so it is the only copy the Input process
        ever sees.
        """
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(action, timeout_s=0.05)

        item = app._outbound_q.get_nowait()
        frame = pickle.loads(item["_frame"])
        assert frame["_awaited_window_s"] == 0.05, (
            "the awaited window did not reach the frame, so the Input "
            "watchdog still judges this action by its fixed limit"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["press_key_action", "type_text"])
    async def test_send_request_leaves_other_actions_without_a_window(
        self, app, action,
    ):
        """Only the two long-window actions carry a stamp.

        wh-watchdog-stall-window. A stamp does not only RAISE the watchdog's
        limit -- for an action awaited for less than the fixed limit it would
        lower it, which is a change to actions this bead never examined. The
        absence below is what keeps the change to the three actions named.
        """
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(action, timeout_s=0.05)

        item = app._outbound_q.get_nowait()
        frame = pickle.loads(item["_frame"])
        assert frame["_awaited_window_s"] is None, (
            "an action outside the click and walk classes was stamped, so "
            "its stall limit moved with a config key it does not await"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", [
        "intelligent_insert_text",
        "start_utterance",
        "end_utterance",
        "_te_event_ack",
        "add_soft_allow_tuple",
        "set_log_level",
        "terminal_editor_cancelled",
    ])
    async def test_durable_actions_get_long_delivery_deadline(self, app, action):
        """Never-stale actions (dictation text, lifecycle, state) outlive a wedged consumer.

        wh-overlay-slow-uia-stale-badges.14.1: a uniform 5 s TTL silently
        dropped dictated words and clipboard-restoring end_utterance during
        an 8+ s UIA walk. These actions carry a long deadline instead.
        """
        from app import _DURABLE_COMMAND_TTL_S

        before = time.monotonic()
        await app.send_command(action, {})

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before + _DURABLE_COMMAND_TTL_S - 1.0 < deadline
        assert deadline < before + _DURABLE_COMMAND_TTL_S + 1.5
        assert _DURABLE_COMMAND_TTL_S >= 60.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["press", "retract", "type_text"])
    async def test_stale_sensitive_actions_keep_short_deadline(self, app, action):
        """Pointer/keystroke actions still expire on the short TTL."""
        before = time.monotonic()
        await app.send_command(action, {})

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before + 4.0 < deadline < before + 6.5

    @pytest.mark.asyncio
    async def test_skip_clipboard_restore_is_stale_sensitive(self, app):
        """skip_clipboard_restore expires on the short TTL, not the durable one.

        wh-overlay-slow-uia-stale-badges.14.15: the flag it sets in the Input
        process is unscoped, so a late delivery silently makes a LATER
        unrelated utterance skip restoring the user's clipboard. Losing a
        skip breaks the current copy visibly; delivering one late corrupts a
        future utterance invisibly. Stale-sensitive is the safe class.
        """
        before = time.monotonic()
        await app.send_command("skip_clipboard_restore", {})

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before + 4.0 < deadline < before + 6.5

    @pytest.mark.asyncio
    async def test_skip_clipboard_restore_request_expires_with_its_caller(self, app):
        """A skip_clipboard_restore request is never delivered after its caller gave up."""
        before = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("skip_clipboard_restore", timeout_s=0.05)

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before < deadline < before + 0.5

    def test_durable_actions_are_dispatchable_by_input_process(self):
        """Every allowlisted durable action must be one the Input process handles.

        wh-overlay-slow-uia-stale-badges.14.6: the set is a hand-maintained
        name list, so a typo (or a name no consumer handles) silently keeps
        an action on the short TTL. input_proc dispatches by
        getattr(ui_handler, action) except for the three actions it handles
        inline.
        """
        from app import _DURABLE_COMMAND_ACTIONS
        from ui.ui_action_handler import UIActionHandler

        input_proc_inline_actions = {"_te_event_ack", "set_log_level", "add_soft_allow_tuple"}
        for action in _DURABLE_COMMAND_ACTIONS:
            assert action in input_proc_inline_actions or hasattr(UIActionHandler, action), (
                f"durable action '{action}' matches no Input-process handler"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["intelligent_insert_text", "add_soft_allow_tuple"])
    async def test_durable_request_gets_long_delivery_deadline(self, app, action):
        """A durable action sent as a request outlives the caller's own timeout.

        wh-overlay-slow-uia-stale-badges.14.4: the normal dictation path is
        send_request('intelligent_insert_text'). The caller still waits only
        its own timeout; only the delivery budget grows.
        """
        from app import _DURABLE_COMMAND_TTL_S

        before = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(action, timeout_s=0.05)

        item = app._outbound_q.get_nowait()
        deadline = item["_delivery_deadline_monotonic"]
        assert before + _DURABLE_COMMAND_TTL_S - 1.0 < deadline
        assert deadline < before + _DURABLE_COMMAND_TTL_S + 1.5

    @pytest.mark.asyncio
    async def test_send_one_drops_payload_when_event_clears_after_deadline(
        self, app, mock_shm, mock_command_ready_event, caplog
    ):
        """A frame that frees only after the payload's deadline is not written.

        wh-overlay-slow-uia-stale-badges.14.3: _await_event_state accepts a
        state match before checking elapsed time, so without a re-check the
        sender would deliver an expired payload the moment the consumer
        recovers.
        """
        def clears_only_after_the_deadline():
            # Advance the sender's clock before reporting the frame free.
            # Windows GetTickCount64 can advance only 47ms during a 60ms
            # sleep, so elapsed wall time does not prove a 50ms deadline passed.
            clock.monotonic.return_value = payload["_delivery_deadline_monotonic"] + 0.015
            return False

        mock_command_ready_event.is_set.side_effect = clears_only_after_the_deadline
        # Replace only app's module binding; asyncio and the shared time module
        # retain their real clocks. Keep the real event polling and frame writer.
        with patch("app.time", wraps=time) as clock:
            clock.monotonic.return_value = 100.0
            payload = {
                "action": "late_click",
                "_delivery_deadline_monotonic": clock.monotonic() + 0.05,
            }
            with caplog.at_level(logging.INFO):
                await app._send_one(payload)

        mock_command_ready_event.is_set.assert_called_once()
        assert struct.unpack(">I", bytes(mock_shm.buf[:4]))[0] == 0
        mock_command_ready_event.set.assert_not_called()
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=expired" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_cancelled_request_is_never_delivered(
        self, app, mock_shm, mock_command_ready_event, caplog
    ):
        """An explicitly cancelled request is not written after the consumer recovers.

        wh-overlay-slow-uia-stale-badges.14.7: the overlay abort path cancels
        its send_request while the payload sits queued behind a busy frame.
        The future is removed, but the payload kept its live deadline, so the
        sender still delivered the abandoned overlay build once the frame
        came free -- and Input then ran a stale, potentially slow UIA walk.
        """
        mock_command_ready_event.is_set.return_value = True  # consumer busy
        sender = asyncio.create_task(app._sender_loop())
        try:
            request = asyncio.create_task(
                app.send_request("start_overlay_walk", timeout_s=30.0)
            )
            await asyncio.sleep(0.05)  # payload enqueued; sender waiting on the frame
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with caplog.at_level(logging.INFO):
                mock_command_ready_event.is_set.return_value = False  # consumer recovers
                await asyncio.sleep(0.2)
            assert b"start_overlay_walk" not in bytes(mock_shm.buf)
            mock_command_ready_event.set.assert_not_called()
            pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
            assert any("IPC_DROPPED" in m and "reason=cancelled" in m for m in pipeline)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_post_enqueue_mutation_cannot_corrupt_queued_payload(self, app):
        """The queued envelope is independent of the caller's dict.

        wh-overlay-slow-uia-stale-badges.14.12: send_command used to enqueue
        the caller-owned dict itself, so a mutation after the accepted
        enqueue (send_command has no suspension point before returning)
        reached _send_one -- a NaN deadline then wedged the single sender.
        """
        import math

        payload = {"action": "type_text", "params": {"text": "late"}}
        assert await app.send_command(payload) is True
        payload["_delivery_deadline_monotonic"] = float("nan")

        item = app._outbound_q.get_nowait()
        assert item is not payload
        deadline = item["_delivery_deadline_monotonic"]
        assert isinstance(deadline, float) and math.isfinite(deadline)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_deadline", [
        "not-a-number",
        float("nan"),
        float("inf"),
        True,
    ])
    async def test_invalid_deadline_is_dropped_with_reason(
        self, app, mock_shm, mock_command_ready_event, caplog, bad_deadline
    ):
        """A non-finite or non-numeric deadline is a classified drop, not a wedge.

        wh-overlay-slow-uia-stale-badges.14.12: a string deadline raised out
        of _send_one to the sender loop's generic catch; NaN and inf passed
        every comparison, so the payload either wedged the sender (event
        set) or was delivered arbitrarily late (event clear). bool is
        rejected too: True is not a monotonic timestamp.
        """
        mock_command_ready_event.is_set.return_value = False
        payload = {
            "action": "victim",
            "trace_id": "tr-bad-deadline",
            "request_id": "rq-bad-deadline",
            "_delivery_deadline_monotonic": bad_deadline,
        }
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)  # must not raise

        assert struct.unpack(">I", bytes(mock_shm.buf[:4]))[0] == 0
        mock_command_ready_event.set.assert_not_called()
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-bad-deadline" in m and "rq-bad-deadline" in m for m in errors)
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=invalid_deadline" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_sender_continues_after_invalid_deadline(
        self, app, mock_shm, mock_command_ready_event, caplog
    ):
        """An invalid-deadline payload does not block the next queued payload."""
        mock_command_ready_event.is_set.return_value = False
        app._outbound_q.put_nowait({
            "action": "poisoned",
            "_delivery_deadline_monotonic": float("nan"),
        })
        app._outbound_q.put_nowait({
            "action": "healthy",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        })
        sender = asyncio.create_task(app._sender_loop())
        try:
            with caplog.at_level(logging.INFO):
                await asyncio.sleep(0.2)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["action"] == "healthy"
        assert b"poisoned" not in bytes(mock_shm.buf)
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=invalid_deadline" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_send_command_reports_accepted_and_dropped(self, app):
        """send_command returns True when enqueued and False when dropped.

        wh-overlay-slow-uia-stale-badges.14.10: add_soft_allow retries an IPC
        failure, but a queue-full drop returned like a success, so a dropped
        grant was reported delivered while the Input process never got it.
        """
        from app import _OUTBOUND_QUEUE_MAX

        assert await app.send_command("fill", {"i": 0}) is True
        for i in range(1, _OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        assert await app.send_command("overflow") is False


class TestDropDiagnostics:
    """Every drop is attributable after the fact (wh-overlay-slow-uia-stale-badges.14.2).

    Drop logs name the payload's trace_id/request_id, the pipeline record
    counts drops instead of counting them as sent, and an unreadable event
    object is reported as its own cause rather than as a deadline drop.
    """

    @pytest.mark.asyncio
    async def test_expired_drop_log_names_trace_and_request_ids(self, app, caplog):
        """The expired-payload drop line identifies which payload died."""
        stale = {
            "action": "stale",
            "trace_id": "tr-expired-123",
            "request_id": "rq-expired-9",
            "_delivery_deadline_monotonic": time.monotonic() - 0.01,
        }
        with caplog.at_level(logging.ERROR, logger="app"):
            await app._send_one(stale)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-expired-123" in m and "rq-expired-9" in m for m in errors)

    @pytest.mark.asyncio
    async def test_unread_frame_drop_log_names_trace_id(self, app, mock_command_ready_event, caplog):
        """The refuse-to-overwrite drop line identifies which payload died."""
        mock_command_ready_event.is_set.return_value = True  # consumer never reads
        payload = {
            "action": "late",
            "trace_id": "tr-unread-456",
            "_delivery_deadline_monotonic": time.monotonic() + 0.05,
        }
        with caplog.at_level(logging.ERROR, logger="app"):
            await app._send_one(payload)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-unread-456" in m for m in errors)

    @pytest.mark.asyncio
    async def test_queue_full_drop_log_names_trace_id(self, app, caplog):
        """The queue-full drop line identifies which payload died."""
        from app import _OUTBOUND_QUEUE_MAX
        from utils.trace_context import current_trace_id

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        token = current_trace_id.set("tr-full-789")
        try:
            with caplog.at_level(logging.ERROR, logger="app"):
                await app.send_command("overflow")
        finally:
            current_trace_id.reset(token)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-full-789" in m for m in errors)

    @pytest.mark.asyncio
    async def test_queue_full_command_is_not_counted_as_sent(self, app, caplog):
        """A fire-and-forget payload dropped at enqueue produces IPC_DROPPED, not IPC_SENT."""
        from app import _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        with caplog.at_level(logging.INFO, logger="wheelhouse.pipeline"):
            caplog.clear()
            await app.send_command("overflow")

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert not any("IPC_SENT" in m for m in pipeline)
        assert any("IPC_DROPPED" in m and "reason=queue_full" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_queue_full_request_is_not_counted_as_sent(self, app, caplog):
        """A request refused at enqueue produces IPC_DROPPED, not IPC_SENT."""
        from app import IpcDeliveryError, _OUTBOUND_QUEUE_MAX

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        with caplog.at_level(logging.INFO, logger="wheelhouse.pipeline"):
            caplog.clear()
            with pytest.raises(IpcDeliveryError):
                await app.send_request("click_element", timeout_s=5.0)

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert not any("IPC_SENT" in m for m in pipeline)
        assert any("IPC_DROPPED" in m and "reason=queue_full" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_expired_drop_emits_pipeline_ipc_dropped(self, app, caplog):
        """A deadline-expired payload is counted as dropped in the pipeline record."""
        stale = {
            "action": "stale",
            "_delivery_deadline_monotonic": time.monotonic() - 0.01,
        }
        with caplog.at_level(logging.INFO, logger="wheelhouse.pipeline"):
            await app._send_one(stale)

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=expired" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_unread_frame_drop_emits_pipeline_ipc_dropped(self, app, mock_command_ready_event, caplog):
        """A refuse-to-overwrite drop is counted as dropped in the pipeline record."""
        mock_command_ready_event.is_set.return_value = True
        payload = {
            "action": "late",
            "_delivery_deadline_monotonic": time.monotonic() + 0.05,
        }
        with caplog.at_level(logging.INFO, logger="wheelhouse.pipeline"):
            await app._send_one(payload)

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=unread_frame" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_event_error_drop_blames_the_event_not_the_deadline(self, app, mock_command_ready_event, caplog):
        """A dead event object is reported as the cause; the deadline wording never appears."""
        mock_command_ready_event.is_set.side_effect = OSError("event closed")
        payload = {
            "action": "victim",
            "trace_id": "tr-event-1",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        }
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("command_ready_event" in m and "tr-event-1" in m for m in errors)
        assert not any("did not read the previous command" in m for m in errors)
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=event_error" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_set_event_failure_emits_correlated_drop(self, app, mock_command_ready_event, caplog):
        """A payload written but never signaled is recorded as a drop, with ids.

        wh-overlay-slow-uia-stale-badges.14.5: the Input process is never
        notified of the written frame, so the payload is undelivered; without
        a terminal IPC_DROPPED it stays counted as sent.
        """
        mock_command_ready_event.is_set.return_value = False
        mock_command_ready_event.set.side_effect = RuntimeError("event dead")
        payload = {
            "action": "victim",
            "trace_id": "tr-set-1",
            "request_id": "rq-set-2",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        }
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-set-1" in m and "rq-set-2" in m for m in errors)
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=event_set_failed" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_event_error_after_write_does_not_drop(self, app, mock_shm, mock_command_ready_event, caplog):
        """The best-effort pickup confirmation swallows an event failure after delivery."""
        mock_command_ready_event.is_set.side_effect = [False, OSError("event closed")]
        payload = {
            "action": "delivered",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        }
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)

        size = struct.unpack(">I", bytes(mock_shm.buf[:4]))[0]
        data = pickle.loads(bytes(mock_shm.buf[4:4 + size]))
        assert data["action"] == "delivered"
        mock_command_ready_event.set.assert_called_once()
        assert not any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.asyncio
    async def test_write_failure_emits_correlated_drop(
        self, app, mock_command_ready_event, caplog
    ):
        """A shared-memory write failure is ONE terminal, attributable drop.

        wh-overlay-slow-uia-stale-badges.14.8: a _frame_and_write failure
        used to escape to the sender loop's generic catch -- no ids, no
        IPC_DROPPED -- while the payload was already counted as IPC_SENT.

        wh-overlay-slow-uia-stale-badges.14.39: the helper ALSO logged
        its own uncorrelated ERROR before re-raising, so one failure
        produced two ERROR records and therefore two Windows error
        notifications (the notifier rate-limits on the rendered
        message, and the two messages differ). The failure is driven
        through the real _frame_and_write here -- a buffer that raises
        on write -- because monkeypatching the helper hid its log.
        """
        mock_command_ready_event.is_set.return_value = False

        class ExplodingBuf:
            def __setitem__(self, key, value):
                raise RuntimeError("buffer torn down")

        app.shm.buf = ExplodingBuf()
        payload = {
            "action": "victim",
            "trace_id": "tr-write-1",
            "request_id": "rq-write-2",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        }
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)  # must not raise

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "victim" in errors[0]
        assert "tr-write-1" in errors[0] and "rq-write-2" in errors[0]
        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert any("IPC_DROPPED" in m and "reason=write_error" in m for m in pipeline)
        assert not any("IPC_SENT" in m for m in pipeline)
        mock_command_ready_event.set.assert_not_called()

        # The sender continues: the next payload delivers normally.
        app.shm.buf = MagicMock()
        await app._send_one({
            "action": "survivor",
            "trace_id": "tr-write-3",
            "_delivery_deadline_monotonic": time.monotonic() + 5.0,
        })
        mock_command_ready_event.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_queue_full_request_error_log_names_ids(self, app, caplog):
        """The request-failure ERROR record carries trace_id and request_id.

        wh-overlay-slow-uia-stale-badges.14.9: on a full queue the pipeline
        IPC_DROPPED record had both ids, but the operational ERROR record
        describing the failed request had neither.
        """
        from app import IpcDeliveryError, _OUTBOUND_QUEUE_MAX
        from utils.trace_context import current_trace_id

        for i in range(_OUTBOUND_QUEUE_MAX):
            await app.send_command("fill", {"i": i})

        token = current_trace_id.set("tr-reqfull-1")
        try:
            with caplog.at_level(logging.INFO):
                with pytest.raises(IpcDeliveryError):
                    await app.send_request("click_element", timeout_s=5.0)
        finally:
            current_trace_id.reset(token)

        request_id = next(
            m.split("request_id=")[1].split()[0]
            for m in (
                r.getMessage() for r in caplog.records
                if r.name == "wheelhouse.pipeline"
            )
            if "IPC_DROPPED" in m and "reason=queue_full" in m
        )
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("tr-reqfull-1" in m and request_id in m for m in errors)

    @pytest.mark.asyncio
    async def test_post_enqueue_drop_is_not_counted_as_sent(
        self, app, mock_command_ready_event, caplog, monkeypatch
    ):
        """A payload dropped after a successful enqueue never produces IPC_SENT.

        wh-overlay-slow-uia-stale-badges.14.11: IPC_SENT used to fire at
        enqueue, so a payload the sender later dropped (unread frame,
        expiry, write failure) produced BOTH pipeline outcomes and inflated
        the sent count during the exact slow-consumer failure this work
        makes diagnosable.
        """
        monkeypatch.setattr("app._COMMAND_DELIVERY_TTL_S", 0.05)
        mock_command_ready_event.is_set.return_value = True  # consumer wedged

        with caplog.at_level(logging.INFO):
            assert await app.send_command("press", {"key": "enter"}) is True
            await app._send_one(app._outbound_q.get_nowait())

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert not any("IPC_SENT" in m for m in pipeline)
        assert sum(1 for m in pipeline if "IPC_DROPPED" in m) == 1

    @pytest.mark.asyncio
    async def test_request_post_enqueue_drop_is_not_counted_as_sent(
        self, app, mock_command_ready_event, caplog
    ):
        """A request the sender drops after enqueue never produces IPC_SENT."""
        mock_command_ready_event.is_set.return_value = True  # consumer wedged

        with caplog.at_level(logging.INFO):
            with pytest.raises(asyncio.TimeoutError):
                await app.send_request("stale_click", timeout_s=0.05)
            await app._send_one(app._outbound_q.get_nowait())

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        assert not any("IPC_SENT" in m for m in pipeline)
        assert sum(1 for m in pipeline if "IPC_DROPPED" in m) == 1

    @pytest.mark.asyncio
    async def test_delivered_payload_emits_ipc_sent_with_ids(
        self, app, mock_command_ready_event, caplog
    ):
        """IPC_SENT fires exactly once, at delivery, and carries the payload ids."""
        from utils.trace_context import current_trace_id

        mock_command_ready_event.is_set.return_value = False
        token = current_trace_id.set("tr-sent-1")
        try:
            with caplog.at_level(logging.INFO):
                assert await app.send_command("press", {"key": "enter"}) is True
                await app._send_one(app._outbound_q.get_nowait())
        finally:
            current_trace_id.reset(token)

        pipeline = [r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"]
        sent = [m for m in pipeline if "IPC_SENT" in m]
        assert len(sent) == 1
        assert "action=press" in sent[0]
        assert "tr-sent-1" in sent[0]
        assert not any("IPC_DROPPED" in m for m in pipeline)

    @pytest.mark.asyncio
    async def test_delivered_record_carries_structured_trace_id(
        self, app, mock_command_ready_event, caplog
    ):
        """The IPC_SENT LogRecord's trace_id ATTRIBUTE matches the payload.

        The sender task inherits an empty ContextVar context at start(), and
        TraceIdFilter stamps record.trace_id from that context, so the
        structured attribute was empty even when the message text carried the
        id (wh-overlay-slow-uia-stale-badges.14.13).
        """
        from utils.trace_context import TraceIdFilter, current_trace_id

        mock_command_ready_event.is_set.return_value = False
        token = current_trace_id.set("tr-struct-1")
        try:
            assert await app.send_command("press", {"key": "enter"}) is True
        finally:
            current_trace_id.reset(token)

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.INFO):
                # Deliver with NO trace set, like the real sender task.
                await app._send_one(app._outbound_q.get_nowait())
        finally:
            caplog.handler.removeFilter(trace_filter)

        sent = [
            r for r in caplog.records
            if r.name == "wheelhouse.pipeline" and "IPC_SENT" in r.getMessage()
        ]
        assert len(sent) == 1
        assert getattr(sent[0], "trace_id", "") == "tr-struct-1"

    @pytest.mark.asyncio
    async def test_drop_record_carries_structured_trace_id(self, app, caplog):
        """A _send_one drop's ERROR and IPC_DROPPED records carry the id attribute."""
        from utils.trace_context import TraceIdFilter, current_trace_id

        token = current_trace_id.set("tr-struct-2")
        try:
            assert await app.send_command("press", {"key": "enter"}) is True
        finally:
            current_trace_id.reset(token)
        queued = app._outbound_q.get_nowait()
        queued["_delivery_deadline_monotonic"] = time.monotonic() - 1.0

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        try:
            with caplog.at_level(logging.INFO):
                await app._send_one(queued)
        finally:
            caplog.handler.removeFilter(trace_filter)

        dropped = [
            r for r in caplog.records
            if r.name == "wheelhouse.pipeline" and "IPC_DROPPED" in r.getMessage()
        ]
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(dropped) == 1
        assert getattr(dropped[0], "trace_id", "") == "tr-struct-2"
        assert errors
        assert all(getattr(r, "trace_id", "") == "tr-struct-2" for r in errors)

    @pytest.mark.asyncio
    async def test_ipc_complete_carries_request_trace_and_elapsed(self, app, caplog):
        """IPC_COMPLETE is tied to the request that completed, not the demux task.

        wh-overlay-slow-uia-stale-badges.14.23: the demux task inherits
        the startup ContextVar context, so its elapsed_ms() and the
        TraceIdFilter stamp reported an empty trace and a meaningless
        elapsed for every successful request. The record must carry the
        request's own trace_id and an elapsed computed from the anchor
        stamped at enqueue.
        """
        from queue import Queue
        from utils.trace_context import (
            TraceIdFilter, current_trace_id, trace_start_time,
        )

        app.response_queue = Queue()
        # Start the demux BEFORE any trace is set, like production startup.
        demux = asyncio.create_task(app._demux_loop())

        trace_filter = TraceIdFilter()
        caplog.handler.addFilter(trace_filter)
        t_token = current_trace_id.set("tr-complete-1")
        s_token = trace_start_time.set(time.perf_counter() - 1.0)
        try:
            with caplog.at_level(logging.INFO):
                task = asyncio.create_task(
                    app.send_request("click_element", timeout_s=2.0)
                )
                await asyncio.sleep(0)
                item = app._outbound_q.get_nowait()
                app.response_queue.put({
                    "request_id": item["request_id"],
                    "status": "ok", "action": "click_element",
                })
                result = await task
        finally:
            current_trace_id.reset(t_token)
            trace_start_time.reset(s_token)
            caplog.handler.removeFilter(trace_filter)
            demux.cancel()
            await asyncio.gather(demux, return_exceptions=True)

        assert result["status"] == "ok"
        complete = [
            r for r in caplog.records
            if r.name == "wheelhouse.pipeline" and "IPC_COMPLETE" in r.getMessage()
        ]
        assert len(complete) == 1
        assert getattr(complete[0], "trace_id", "") == "tr-complete-1"
        elapsed = float(complete[0].getMessage().split("elapsed_ms=")[1])
        assert 500.0 < elapsed < 60000.0

    @pytest.mark.asyncio
    async def test_deadline_passing_during_framing_is_not_signaled(
        self, app, mock_command_ready_event, caplog
    ):
        """A payload whose deadline passes during framing is never signaled.

        Serialization and the shared-memory copy run between the post-wait
        deadline check and command_ready_event.set(); a stall there used to
        deliver a stale payload and count it IPC_SENT
        (wh-overlay-slow-uia-stale-badges.14.16).
        """
        mock_command_ready_event.is_set.return_value = False
        payload = {
            "action": "press",
            "params": {},
            "trace_id": "tr-frame-late",
            "_delivery_deadline_monotonic": time.monotonic() + 0.05,
        }

        original_frame = app._frame_and_write

        def slow_frame(p):
            original_frame(p)
            time.sleep(0.08)

        app._frame_and_write = slow_frame
        with caplog.at_level(logging.INFO):
            await app._send_one(payload)

        mock_command_ready_event.set.assert_not_called()
        pipeline = [
            r.getMessage() for r in caplog.records if r.name == "wheelhouse.pipeline"
        ]
        assert not any("IPC_SENT" in m for m in pipeline)
        dropped = [m for m in pipeline if "IPC_DROPPED" in m]
        assert len(dropped) == 1
        assert "reason=expired" in dropped[0]


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
# Late-response registry (wh-overlay-slow-uia-stale-badges.6).
#
# send_request(on_late_response=...) registers the callback when the wait
# times out; the demuxer consults the registry BEFORE the unknown-request_id
# warning and schedules the callback with the late payload. Bounded: capacity
# 32 (evict oldest), grace _LATE_RESPONSE_GRACE_S (prune on touch).
# ---------------------------------------------------------------------------

class TestLateResponseRegistry:
    """Test the timed-out-request late-response callback registry."""

    async def _run_demux_once(self, app, mock_response_queue, responses):
        """Feed ``responses`` through the demuxer, then let it idle briefly."""
        remaining = list(responses)

        def side_effect():
            if remaining:
                return remaining.pop(0)
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect
        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_timeout_with_callback_registers_it(self, app):
        """A timed-out send_request with on_late_response registers exactly
        one entry keyed by the request_id it used."""
        cb = Mock()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=0.01, on_late_response=cb,
            )
        assert len(app._late_response_callbacks) == 1

    @pytest.mark.asyncio
    async def test_late_response_within_grace_invokes_callback_once(
        self, app, mock_response_queue, caplog,
    ):
        """A late response inside the grace window invokes the callback
        exactly once with the payload; the entry is consumed, so a REPLAY of
        the same request_id falls back to today's unknown-id warning."""
        calls = []
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=0.01, on_late_response=calls.append,
            )
        request_id = next(iter(app._late_response_callbacks))
        response = {"request_id": request_id, "status": "error",
                    "outcome": "execution_failed"}

        import logging
        with caplog.at_level(logging.WARNING):
            await self._run_demux_once(
                app, mock_response_queue, [response, dict(response)],
            )

        assert calls == [response]
        assert len(app._late_response_callbacks) == 0
        unknown = [
            r for r in caplog.records
            if "unknown/timed-out request_id" in r.getMessage()
        ]
        assert len(unknown) == 1  # only the replay warned

    @pytest.mark.asyncio
    async def test_late_response_after_grace_is_dropped_with_warning(
        self, app, mock_response_queue, monkeypatch, caplog,
    ):
        """An entry older than the grace window is pruned when the registry is
        touched, so the response gets today's unknown-id warning and the
        callback never fires.

        Both the grace AND the timeout are zero so the entry's lifetime
        (timeout + grace, wh-overlay-slow-uia-stale-badges.20.6) is exactly
        zero: the expiry equals the registration instant and the prune's
        ``expiry <= now`` drops it deterministically. A nonzero timeout races
        the coarse Windows monotonic clock -- asyncio may deliver the wait
        timeout up to one clock-resolution tick before ``time.monotonic()``
        advances past the expiry, leaving the entry live at the consult."""
        import app as app_module
        monkeypatch.setattr(app_module, "_LATE_RESPONSE_GRACE_S", 0.0)
        cb = Mock()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=0.0, on_late_response=cb,
            )
        request_id = next(iter(app._late_response_callbacks))
        response = {"request_id": request_id, "status": "error"}

        import logging
        with caplog.at_level(logging.WARNING):
            await self._run_demux_once(app, mock_response_queue, [response])

        cb.assert_not_called()
        assert any(
            "unknown/timed-out request_id" in r.getMessage()
            for r in caplog.records
        )

    def test_capacity_evicts_oldest_entry(self, app):
        """Registering beyond the 32-entry capacity evicts the OLDEST entry."""
        for i in range(33):
            app._register_late_response(f"req-{i}", Mock(), 15.0)
        assert len(app._late_response_callbacks) == 32
        assert "req-0" not in app._late_response_callbacks
        assert "req-1" in app._late_response_callbacks
        assert "req-32" in app._late_response_callbacks

    @pytest.mark.asyncio
    async def test_evicted_entry_gets_unknown_id_warning(
        self, app, mock_response_queue, caplog,
    ):
        """A response for an evicted request_id is dropped with the warning."""
        evicted_cb = Mock()
        app._register_late_response("req-evicted", evicted_cb, 15.0)
        for i in range(32):
            app._register_late_response(f"req-{i}", Mock(), 15.0)
        response = {"request_id": "req-evicted", "status": "done"}

        import logging
        with caplog.at_level(logging.WARNING):
            await self._run_demux_once(app, mock_response_queue, [response])

        evicted_cb.assert_not_called()
        assert any(
            "unknown/timed-out request_id" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_demuxer(
        self, app, mock_response_queue,
    ):
        """A raising late callback must not break the demuxer: a response for
        a live future processed AFTER the bad callback still resolves."""
        def boom(_response):
            raise RuntimeError("late callback boom")

        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=0.01, on_late_response=boom,
            )
        late_id = next(iter(app._late_response_callbacks))

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        app.response_futures["live-uuid"] = future

        await self._run_demux_once(
            app, mock_response_queue,
            [
                {"request_id": late_id, "status": "error"},
                {"request_id": "live-uuid", "status": "done"},
            ],
        )

        assert future.done()
        assert future.result() == {"request_id": "live-uuid", "status": "done"}

    @pytest.mark.asyncio
    async def test_no_callback_given_registers_nothing(self, app):
        """Without on_late_response a timeout registers nothing -- byte-for-
        byte today's behaviour (the unknown-id warning path is covered by
        test_ignores_unknown_request_id)."""
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("slow_action", timeout_s=0.01)
        assert len(app._late_response_callbacks) == 0

    @pytest.mark.asyncio
    async def test_response_in_timeout_gap_fires_callback_once(
        self, app, mock_response_queue,
    ):
        """The race cell (wh-overlay-slow-uia-stale-badges.20.1): the answer
        lands AFTER the wait timed out (wait_for cancelled the future) but
        BEFORE send_request's finally pops the future. The demuxer's normal
        branch pops a DONE future; it must fall through to the registry
        consult -- registered at future-creation time -- and fire the
        callback exactly once instead of silently dropping the response. A
        replay of the same request_id finds nothing and only warns."""
        calls = []
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.cancel()
        app.response_futures["race-uuid"] = future
        # Registered but NOT armed -- in the real race the TimeoutError
        # branch has not run yet; the done-future consult must fire anyway
        # (wh-overlay-slow-uia-stale-badges.20.7).
        app._register_late_response("race-uuid", calls.append, 15.0)
        response = {"request_id": "race-uuid", "status": "error",
                    "outcome": "execution_failed"}

        # ONE response only -- the real system sends one answer per
        # request_id. (A same-batch duplicate would be rescued through the
        # unknown-id registry consult and mask the normal-branch drop.)
        await self._run_demux_once(app, mock_response_queue, [response])

        assert calls == [response]
        assert len(app._late_response_callbacks) == 0

        # A replay after the registration was consumed only warns.
        await self._run_demux_once(app, mock_response_queue, [dict(response)])
        assert calls == [response]

    @pytest.mark.asyncio
    async def test_done_future_response_without_registration_warns(
        self, app, mock_response_queue, caplog,
    ):
        """A response consumed against a done future with NO registration must
        log a warning (wh-overlay-slow-uia-stale-badges.20.1 -- that drop used
        to be completely silent)."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.cancel()
        app.response_futures["silent-uuid"] = future
        response = {"request_id": "silent-uuid", "status": "error"}

        import logging
        with caplog.at_level(logging.WARNING):
            await self._run_demux_once(app, mock_response_queue, [response])

        assert any(
            "silent-uuid" in r.getMessage()
            and r.levelno >= logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_normal_completion_removes_registration(
        self, app, mock_response_queue,
    ):
        """The registration spans the in-flight window (created BEFORE the
        enqueue); a normally completed request must remove it, so a duplicate
        response later finds no registration and no callback fires.

        The registry is asserted empty WHILE the demuxer still runs: the
        demuxer's shutdown cleanup also clears the registry
        (wh-overlay-slow-uia-stale-badges.20.7), so an assert made after
        the cancel would pass even with the caller's own pop removed
        (defense-in-depth masking; observed as a false M8 survivor)."""
        calls = []
        send_task = asyncio.create_task(app.send_request(
            "action", timeout_s=5.0, on_late_response=calls.append,
        ))
        for _ in range(10):
            if app.response_futures:
                break
            await asyncio.sleep(0)
        request_id = next(iter(app.response_futures))
        # The registration exists while the request is IN FLIGHT.
        assert request_id in app._late_response_callbacks
        response = {"request_id": request_id, "status": "done"}

        remaining = [response]

        def side_effect():
            if remaining:
                return remaining.pop(0)
            raise Empty

        mock_response_queue.get_nowait.side_effect = side_effect
        demux_task = asyncio.create_task(app._demux_loop())
        result = await send_task
        assert result == response
        # Asserted BEFORE the demuxer is cancelled: only the caller's own
        # on-time pop can have emptied the registry at this point.
        assert len(app._late_response_callbacks) == 0
        demux_task.cancel()
        try:
            await demux_task
        except asyncio.CancelledError:
            pass
        # A duplicate finds neither a live future nor a registration.
        await self._run_demux_once(app, mock_response_queue, [dict(response)])
        assert calls == []

    @pytest.mark.asyncio
    async def test_non_timeout_failure_removes_registration(self, app):
        """A non-timeout failure (the enqueue raised) pops the registration:
        the request never reached the wire, so no late answer can exist."""
        cb = Mock()

        def _boom(_payload):
            raise RuntimeError("enqueue boom")

        # send_request enqueues with put_nowait, not put
        # (wh-overlay-slow-uia-stale-badges.5): the request/response path
        # refuses a full queue immediately instead of waiting behind it.
        app._outbound_q.put_nowait = _boom
        with pytest.raises(RuntimeError, match="enqueue boom"):
            await app.send_request(
                "action", timeout_s=1.0, on_late_response=cb,
            )
        assert len(app._late_response_callbacks) == 0
        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_timeout_entry_lives_through_timeout_plus_grace(
        self, app, mock_response_queue, monkeypatch,
    ):
        """wh-overlay-slow-uia-stale-badges.20.6: the grace runs from the
        TIMEOUT instant, not the send instant. With timeout_s=10 the entry
        must stay live through the whole awaiter period and for the full
        grace after it: an answer at timeout + 14 s still fires the
        callback. Clock-controlled: app.py's ``time`` and ``asyncio``
        references are replaced, so no real waiting happens."""
        import app as app_module

        clock = {"now": 1000.0}

        class _FakeTime:
            @staticmethod
            def monotonic():
                return clock["now"]

        class _AsyncioShim:
            """Delegate to real asyncio; wait_for times out immediately."""

            def __getattr__(self, name):
                return getattr(asyncio, name)

            @staticmethod
            async def wait_for(future, timeout):
                # Mirror the real wait_for timeout behaviour: cancel the
                # inner future, then raise.
                future.cancel()
                raise asyncio.TimeoutError()

        monkeypatch.setattr(app_module, "time", _FakeTime)
        monkeypatch.setattr(app_module, "asyncio", _AsyncioShim())

        calls = []
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=10.0, on_late_response=calls.append,
            )
        request_id = next(iter(app._late_response_callbacks))

        # Through the whole awaiter period the entry stays live.
        clock["now"] = 1010.0  # the timeout instant
        app._prune_late_responses()
        assert request_id in app._late_response_callbacks

        # 14 s AFTER the timeout -- inside the grace that must run from the
        # timeout instant -- the answer still fires the callback. (With the
        # send-anchored expiry this would read 24 s > 15 s and drop.)
        clock["now"] = 1024.0
        response = {"request_id": request_id, "status": "error",
                    "outcome": "execution_failed"}
        await self._run_demux_once(app, mock_response_queue, [response])
        assert calls == [response]

    @pytest.mark.asyncio
    async def test_cancelled_awaiter_pops_registration(
        self, app, mock_response_queue,
    ):
        """wh-overlay-slow-uia-stale-badges.20.7(1): a CANCELLED awaiter did
        not time out; its registration must be popped on the way out so a
        later response fires nothing. CancelledError is a BaseException on
        this runtime, so the pop needs its own except clause."""
        calls = []
        send_task = asyncio.create_task(app.send_request(
            "action", timeout_s=5.0, on_late_response=calls.append,
        ))
        for _ in range(10):
            if app.response_futures:
                break
            await asyncio.sleep(0)
        request_id = next(iter(app.response_futures))
        assert request_id in app._late_response_callbacks

        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task
        assert len(app._late_response_callbacks) == 0

        # A later response finds no registration; the callback never fires.
        response = {"request_id": request_id, "status": "done"}
        await self._run_demux_once(app, mock_response_queue, [response])
        assert calls == []

    @pytest.mark.asyncio
    async def test_demuxer_shutdown_clears_registry(
        self, app, mock_response_queue,
    ):
        """wh-overlay-slow-uia-stale-badges.20.7(2): the demuxer's shutdown
        cleanup clears the ENTIRE registry -- no late callback may fire
        after shutdown."""
        cb = Mock()
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request(
                "slow_action", timeout_s=0.01, on_late_response=cb,
            )
        assert len(app._late_response_callbacks) == 1

        mock_response_queue.get_nowait.side_effect = Empty
        task = asyncio.create_task(app._demux_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(app._late_response_callbacks) == 0
        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_after_on_time_answer_fires_nothing_and_warns(
        self, app, mock_response_queue, caplog,
    ):
        """wh-overlay-slow-uia-stale-badges.20.7(3): a second response in the
        SAME demux batch after an on-time answer reaches the unknown-id
        branch while the caller's registration is still live (the caller has
        not resumed yet). The entry is UNARMED -- no timeout happened -- so
        the duplicate must warn and fire nothing; the caller's own pop
        removes the entry."""
        calls = []
        send_task = asyncio.create_task(app.send_request(
            "action", timeout_s=5.0, on_late_response=calls.append,
        ))
        for _ in range(10):
            if app.response_futures:
                break
            await asyncio.sleep(0)
        request_id = next(iter(app.response_futures))
        response = {"request_id": request_id, "status": "done"}

        import logging
        with caplog.at_level(logging.WARNING):
            await self._run_demux_once(
                app, mock_response_queue, [response, dict(response)],
            )
        result = await send_task
        assert result == response
        # The duplicate never fired the callback.
        assert calls == []
        assert any(
            "duplicate" in r.getMessage().lower() for r in caplog.records
        )
        # The duplicate was recognized, not dropped as unknown.
        assert not any(
            "unknown/timed-out request_id" in r.getMessage()
            for r in caplog.records
        )


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

            await app.start("127.0.0.1", 0, text_handler)

            assert app.ws_port == 9876


class TestProducerParamsPreservation:
    """wh-overlay-slow-uia-stale-badges.14.25: the producer helpers must
    not rewrite a supplied falsy non-mapping params value into {}. Only
    an omitted params (None) defaults; every other supplied value
    travels unchanged so the input-side _extract_params gate (.14.22) is
    the single validator. Without this, send_request(
    "terminal_editor_cancelled", params=[]) delivered params={} and the
    empty-rid recovery path force-cleaned a live editor session."""

    @pytest.mark.asyncio
    async def test_send_command_string_overload_defaults_omitted_params(self, app):
        await app.send_command("press")
        item = app._outbound_q.get_nowait()
        assert item["params"] == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supplied", [[], "", 0, False])
    async def test_send_command_string_overload_preserves_supplied_falsy_params(
        self, app, supplied,
    ):
        await app.send_command("press", supplied)
        item = app._outbound_q.get_nowait()
        assert item["params"] == supplied
        assert type(item["params"]) is type(supplied)

    @pytest.mark.asyncio
    async def test_send_request_defaults_omitted_params(self, app):
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("walk", timeout_s=0.05)
        item = app._outbound_q.get_nowait()
        # .14.57: the queued entry is canonical sender metadata and no
        # longer carries params. The delivered bytes are where this
        # producer contract lives, so read the frame.
        assert pickle.loads(item["_frame"])["params"] == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("supplied", [[], "", 0, False])
    async def test_send_request_preserves_supplied_falsy_params(self, app, supplied):
        with pytest.raises(asyncio.TimeoutError):
            await app.send_request("walk", supplied, timeout_s=0.05)
        item = app._outbound_q.get_nowait()
        delivered = pickle.loads(item["_frame"])["params"]  # .14.57
        assert delivered == supplied
        assert type(delivered) is type(supplied)
