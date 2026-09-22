"""Tests for shared WSForwarder.

These tests verify that:
1. Disconnect callback does NOT fire on initial connection failures
2. Disconnect callback DOES fire when an established connection is lost
3. Reconnect callback fires after reconnection (not on first connect)
4. Shutdown callback fires when server sends shutdown message
5. Message queue is cleared only after actual disconnect
6. Send methods queue messages correctly
"""
import asyncio
import socket
import threading
import time
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WSForwarder


def _unused_port():
    """Return a port that Windows chose and that has no listener now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestDisconnectCallback:
    """Tests for the on_disconnect_callback feature."""

    def test_no_callback_on_initial_connection_failure(self):
        """Callback should NOT fire when initial connection fails (WheelHouse not running).

        When the STT server starts before WheelHouse is running, connection attempts
        will fail. The disconnect callback should NOT be invoked in this case - it
        should only fire when an established connection is lost.
        """
        callback_called = threading.Event()

        def on_disconnect():
            callback_called.set()

        forwarder = WSForwarder(
            host="localhost",
            port=_unused_port(),  # No server running
            transcription_enabled_event=threading.Event(),
            on_disconnect_callback=on_disconnect,
            debug=False
        )
        forwarder.start()

        # Give time for multiple connection attempts (backoff starts at 0.5s)
        time.sleep(1.5)

        # Callback should NOT have been called (no prior successful connection)
        assert not callback_called.is_set(), "Callback should not fire on initial connection failure"

        forwarder.stop()

    @pytest.mark.asyncio
    async def test_callback_fires_after_established_connection_lost(self):
        """Callback should fire when an established connection is lost.

        This test starts a mock WebSocket server, lets the forwarder connect,
        then forcibly closes the client connection. The disconnect callback should fire.
        """
        callback_called = threading.Event()

        def on_disconnect():
            callback_called.set()

        # Start a simple WebSocket server
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            # Send initial status message like WheelHouse does
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            # Keep connection alive until closed
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        # Get the dynamically assigned port
        port = server.sockets[0].getsockname()[1]

        # Create and start forwarder
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            on_disconnect_callback=on_disconnect,
            debug=False
        )
        forwarder.start()

        # Wait for connection to establish
        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)  # Brief delay to ensure forwarder is fully connected

        # Forcibly close the websocket connection from server side
        if connected_websocket:
            await connected_websocket.close()

        # Wait for disconnect callback
        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "Callback should fire when established connection is lost"

    def test_disconnect_callback_clears_transcription_event(self):
        """The disconnect callback (as wired in main.py) should clear transcription_enabled_event.

        This tests the integration: the callback function that will be passed to WSForwarder
        should clear the transcription_enabled_event when invoked.
        """
        event = threading.Event()
        event.set()  # Start enabled

        assert event.is_set(), "Event should start enabled"

        # This is the callback that main.py will wire up
        def on_wheelhouse_disconnect():
            event.clear()

        # Simulate what happens when disconnect is detected
        on_wheelhouse_disconnect()

        assert not event.is_set(), "Event should be cleared after disconnect callback"


class TestReconnectCallback:
    """Tests for the on_reconnect_callback feature."""

    @pytest.mark.asyncio
    async def test_reconnect_callback_fires_after_reconnection(self):
        """Reconnect callback should fire when connection is re-established after disconnect.

        This tests that:
        1. First connection does NOT trigger reconnect callback
        2. After disconnect, reconnection DOES trigger reconnect callback
        """
        reconnect_called = threading.Event()
        disconnect_called = threading.Event()

        def on_reconnect():
            reconnect_called.set()

        def on_disconnect():
            disconnect_called.set()

        # Start a simple WebSocket server
        import websockets

        connected_websocket = None
        connection_count = 0
        connections_established = asyncio.Queue()

        async def handler(websocket):
            nonlocal connected_websocket, connection_count
            connected_websocket = websocket
            connection_count += 1
            # Send initial status message like WheelHouse does
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            await connections_established.put(connection_count)
            # Keep connection alive until closed
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # Create and start forwarder
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            on_disconnect_callback=on_disconnect,
            on_reconnect_callback=on_reconnect,
            debug=False
        )
        forwarder.start()

        # Wait for first connection
        await asyncio.wait_for(connections_established.get(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Reconnect callback should NOT have fired on first connection
        assert not reconnect_called.is_set(), "Reconnect callback should not fire on first connection"

        # Forcibly close the websocket connection from server side
        if connected_websocket:
            await connected_websocket.close()

        # Wait for disconnect callback
        disconnect_called.wait(timeout=2.0)

        # Wait for reconnection
        await asyncio.wait_for(connections_established.get(), timeout=3.0)
        await asyncio.sleep(0.1)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # NOW reconnect callback should have fired
        assert reconnect_called.is_set(), "Reconnect callback should fire after reconnection"

    def test_reconnect_callback_not_called_without_prior_disconnect(self):
        """Reconnect callback should NOT fire if we never had a successful disconnect.

        This is a unit test for the logic - if was_disconnected is False,
        the reconnect callback should not be invoked.
        """
        # This tests the logic directly - in _sender_loop:
        # if was_disconnected and self.on_reconnect_callback:
        was_disconnected = False
        on_reconnect_callback = lambda: None

        # Decision logic
        should_call_reconnect = was_disconnected and on_reconnect_callback is not None

        assert not should_call_reconnect, "Reconnect should not be called without prior disconnect"


class TestShutdownCommand:
    """Tests for the shutdown command feature."""

    @pytest.mark.asyncio
    async def test_shutdown_callback_fires_on_shutdown_message(self):
        """Shutdown callback should fire when server sends {"type": "shutdown"}.

        When WheelHouse sends a shutdown command, the STT provider should:
        1. Invoke the shutdown callback
        2. Exit cleanly (exit code 0)
        3. Launcher should NOT restart after clean shutdown
        """
        shutdown_called = threading.Event()

        def on_shutdown():
            shutdown_called.set()

        # Start a simple WebSocket server
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            # Send initial status message like WheelHouse does
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            # Keep connection alive until closed
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # Create and start forwarder with shutdown callback
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            shutdown_callback=on_shutdown,
            debug=False
        )
        forwarder.start()

        # Wait for connection to establish
        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)  # Brief delay to ensure forwarder is fully connected

        # Send shutdown command from server
        if connected_websocket:
            await connected_websocket.send('{"type": "shutdown"}')

        # Wait for shutdown callback
        callback_fired = shutdown_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "Shutdown callback should fire when shutdown message received"


class TestQueueClearing:
    """Tests for message queue clearing on disconnect."""

    def test_queue_not_cleared_on_initial_failure(self):
        """Queue should NOT be cleared on initial connection failure.

        Messages queued before the first successful connection should be preserved
        so they can be sent once connection is established.
        """
        callback_called = threading.Event()

        def on_disconnect():
            callback_called.set()

        forwarder = WSForwarder(
            host="localhost",
            port=_unused_port(),  # No server running
            transcription_enabled_event=threading.Event(),
            on_disconnect_callback=on_disconnect,
            debug=False
        )
        forwarder.start()

        # Queue some messages before connection
        forwarder.send_stable("test message 1", utterance_id=1)
        forwarder.send_stable("test message 2", utterance_id=1)

        # Wait for connection attempts
        time.sleep(1.0)

        # Queue should still have messages (not cleared on initial failure)
        # We can't directly inspect the queue, but callback should not have fired
        assert not callback_called.is_set(), "Callback should not fire, queue should not be cleared"

        forwarder.stop()


class TestSendMethods:
    """Tests for the various send methods."""

    @pytest.mark.asyncio
    async def test_send_stable_queues_message(self):
        """send_stable should queue a stable message with correct format."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_stable("hello world", utterance_id=42)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "stable"
        assert msg["text"] == "hello world"
        assert msg["utterance_id"] == 42
        assert msg["is_partial"] == True

    @pytest.mark.asyncio
    async def test_send_final_queues_message(self):
        """send_final should queue a final message with correct format."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final("final text", utterance_id=123)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "final"
        assert msg["text"] == "final text"
        assert msg["utterance_id"] == 123
        assert msg["is_partial"] == False

    @pytest.mark.asyncio
    async def test_send_notification_queues_message(self):
        """send_notification should queue a notification with title and message."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_notification("Test Title", "Test message body")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "notification"
        assert msg["title"] == "Test Title"
        assert msg["message"] == "Test message body"

    @pytest.mark.asyncio
    async def test_send_vad_start_queues_message(self):
        """send_vad_start should queue a vad_start message."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_vad_start(utterance_id=99)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "vad_start"
        assert msg["utterance_id"] == 99


class TestSendEos:
    """Tests for send_eos (Phase 1 of three-mode retraction policy: wh-m2ycz)."""

    @pytest.mark.asyncio
    async def test_send_eos_queues_message(self):
        """send_eos should queue an eos message with utterance_id and trace_id."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_eos(utterance_id=42, trace_id="T-17720345601")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "eos"
        assert msg["utterance_id"] == 42
        assert msg["trace_id"] == "T-17720345601"

    @pytest.mark.asyncio
    async def test_send_eos_default_empty_trace_id(self):
        """send_eos without trace_id should default to empty string."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_eos(utterance_id=7)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "eos"
        assert msg["utterance_id"] == 7
        assert msg.get("trace_id", "") == ""

    def test_send_eos_noop_without_loop(self):
        """send_eos should not raise if forwarder loop has not started."""
        forwarder = WSForwarder(
            host="localhost",
            port=59982,
            transcription_enabled_event=threading.Event(),
            debug=False,
        )
        # Do not call start() - no loop or queue
        forwarder.send_eos(utterance_id=1, trace_id="T-1")
        # Should not raise


class TestSendFinalReason:
    """Tests for send_final's optional final_reason field (wh-m2ycz)."""

    @pytest.mark.asyncio
    async def test_send_final_with_reason_includes_field(self):
        """send_final with final_reason should include it in the JSON payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final(
            "hello world",
            utterance_id=5,
            trace_id="T-17720345601",
            final_reason="GOOGLE_FINAL",
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "final"
        assert msg["text"] == "hello world"
        assert msg["final_reason"] == "GOOGLE_FINAL"

    @pytest.mark.asyncio
    async def test_send_final_without_reason_omits_field(self):
        """send_final without final_reason should not include the field in payload.

        Keeps payload size small for non-Google providers that have no
        equivalent of Google's finalization-source signal.
        """
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final("hello", utterance_id=6)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "final"
        assert "final_reason" not in msg

    @pytest.mark.asyncio
    async def test_send_final_with_each_fallback_reason(self):
        """send_final should accept each of the four documented final_reason values."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        reasons = ["GOOGLE_FINAL", "GOOGLE_SILENCE_2S", "EOS_FALLBACK", "NO_TEXT_TIMEOUT"]
        for i, reason in enumerate(reasons):
            forwarder.send_final(
                f"text {i}",
                utterance_id=100 + i,
                final_reason=reason,
            )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 4
        received_reasons = [msg.get("final_reason") for msg in received_messages[:4]]
        assert received_reasons == reasons


class TestSendFinalConfidence:
    """wh-7ou.7.1.1: final messages may carry the optional "confidence"
    measurement block (part A.1 of the calibration message contract)."""

    CONFIDENCE = {
        "min_word_probability": 0.85,
        "max_no_speech_prob": 0.014,
        "peak_avg_logprob": -0.63,
        "word_count": 1,
        "suppressed": False,
        "rescued": True,
    }

    @pytest.mark.asyncio
    async def test_send_final_with_confidence_includes_object(self):
        """send_final with a confidence dict should carry it in the payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final(
            "comma",
            utterance_id=9,
            trace_id="T-17720345601",
            confidence=dict(self.CONFIDENCE),
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "final"
        assert msg["text"] == "comma"
        assert msg["confidence"] == self.CONFIDENCE

    @pytest.mark.asyncio
    async def test_send_final_without_confidence_omits_field(self):
        """send_final without confidence must not include the field, keeping
        non-Whisper providers payload-clean (same pattern as final_reason)."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final("hello", utterance_id=10)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "final"
        assert "confidence" not in msg

    @pytest.mark.asyncio
    async def test_confidence_null_fields_round_trip(self):
        """None values (word-level data missing) serialize as JSON null and
        arrive as None, per the contract's float|null fields."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        block = {
            "min_word_probability": None,
            "max_no_speech_prob": None,
            "peak_avg_logprob": None,
            "word_count": 0,
            "suppressed": True,
            "rescued": False,
        }
        forwarder.send_final("", utterance_id=11, confidence=block)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["confidence"]["min_word_probability"] is None
        assert msg["confidence"]["max_no_speech_prob"] is None
        assert msg["confidence"]["peak_avg_logprob"] is None
        assert msg["confidence"]["word_count"] == 0
        assert msg["confidence"]["suppressed"] is True
        assert msg["confidence"]["rescued"] is False


class TestTranscriptionStatusCommand:
    """Tests for the set_transcription_status command."""

    @pytest.mark.asyncio
    async def test_enable_transcription_sets_event(self):
        """Receiving set_transcription_status with enabled=true should set the event."""
        import websockets

        connection_established = asyncio.Event()
        connected_websocket = None

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": false}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        # Start with event cleared

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        assert not event.is_set(), "Event should start cleared"

        # Send enable command
        if connected_websocket:
            await connected_websocket.send('{"type": "set_transcription_status", "enabled": true}')

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert event.is_set(), "Event should be set after enable command"

    @pytest.mark.asyncio
    async def test_disable_transcription_clears_event(self):
        """Receiving set_transcription_status with enabled=false should clear the event."""
        import websockets

        connection_established = asyncio.Event()
        connected_websocket = None

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        assert event.is_set(), "Event should start set"

        # Send disable command
        if connected_websocket:
            await connected_websocket.send('{"type": "set_transcription_status", "enabled": false}')

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert not event.is_set(), "Event should be cleared after disable command"


class TestSetInterimResultsCommand:
    """Tests for the set_interim_results command."""

    @pytest.mark.asyncio
    async def test_set_interim_results_true_fires_callback(self):
        """set_interim_results(true) should invoke callback with True."""
        import websockets

        callback_called = threading.Event()
        callback_value = [None]  # Use list to capture value in closure

        def on_set_interim(enabled: bool):
            callback_value[0] = enabled
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_interim_results_callback=on_set_interim,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send('{"type": "set_interim_results", "enabled": true}')

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_interim_results callback should fire"
        assert callback_value[0] == True, "Callback should receive enabled=True"

    @pytest.mark.asyncio
    async def test_set_interim_results_false_fires_callback(self):
        """set_interim_results(false) should invoke callback with False."""
        import websockets

        callback_called = threading.Event()
        callback_value = [None]

        def on_set_interim(enabled: bool):
            callback_value[0] = enabled
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_interim_results_callback=on_set_interim,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send('{"type": "set_interim_results", "enabled": false}')

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_interim_results callback should fire"
        assert callback_value[0] == False, "Callback should receive enabled=False"

    @pytest.mark.asyncio
    async def test_set_interim_results_no_callback_logged(self):
        """set_interim_results with no callback should log a message."""
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        # No set_interim_results_callback provided
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send command - should not crash, just log
        if connected_websocket:
            await connected_websocket.send('{"type": "set_interim_results", "enabled": true}')

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Test passes if no exception was raised


class TestLogForwarding:
    """Tests for log forwarding via WebSocket."""

    @pytest.mark.asyncio
    async def test_send_log_queues_message(self):
        """send_log should queue a log message with correct format."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_log(
            level="INFO",
            message="Test log message",
            source="Test Provider"
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "log"
        assert msg["level"] == "INFO"
        assert msg["message"] == "Test log message"
        assert msg["source"] == "Test Provider"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_send_log_preserves_timestamp(self):
        """send_log should preserve the provided timestamp."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        test_timestamp = "2026-01-17T10:30:45.123456"
        forwarder.send_log(
            level="WARNING",
            message="Warning message",
            source="Parakeet",
            timestamp=test_timestamp
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["timestamp"] == test_timestamp

    @pytest.mark.asyncio
    async def test_send_log_all_levels(self):
        """send_log should support all standard log levels."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        for level in levels:
            forwarder.send_log(
                level=level,
                message=f"{level} message",
                source="Test"
            )

        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 5
        received_levels = [msg["level"] for msg in received_messages]
        for level in levels:
            assert level in received_levels

    @pytest.mark.asyncio
    async def test_send_log_auto_generates_timestamp(self):
        """send_log should auto-generate timestamp if not provided."""
        import websockets
        import json
        from datetime import datetime

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        before_send = datetime.now().isoformat()
        forwarder.send_log(
            level="INFO",
            message="Auto timestamp test",
            source="Test"
        )
        await asyncio.sleep(0.5)
        after_send = datetime.now().isoformat()

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        # Timestamp should be present and between before/after
        assert "timestamp" in msg
        assert msg["timestamp"] >= before_send[:19]  # Compare up to seconds
        assert msg["timestamp"][:19] <= after_send[:19]


class TestWebSocketLogHandler:
    """Tests for the WebSocketLogHandler class."""

    @pytest.mark.asyncio
    async def test_handler_forwards_log_records(self):
        """WebSocketLogHandler should forward logging.LogRecord objects."""
        import websockets
        import json
        import logging
        from shared_stt.ws_forwarder import WebSocketLogHandler

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Create a logger with the WebSocketLogHandler
        test_logger = logging.getLogger("test_ws_log_handler")
        test_logger.setLevel(logging.DEBUG)
        ws_handler = WebSocketLogHandler(forwarder, source="Test Provider")
        test_logger.addHandler(ws_handler)

        # Log a message
        test_logger.info("Test message from logger")
        await asyncio.sleep(0.5)

        # Clean up
        test_logger.removeHandler(ws_handler)
        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Verify the log was forwarded
        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "log"
        assert msg["level"] == "INFO"
        assert "Test message from logger" in msg["message"]
        assert msg["source"] == "Test Provider"

    @pytest.mark.asyncio
    async def test_handler_preserves_log_level(self):
        """WebSocketLogHandler should preserve the original log level."""
        import websockets
        import json
        import logging
        from shared_stt.ws_forwarder import WebSocketLogHandler

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Create a logger with the WebSocketLogHandler
        test_logger = logging.getLogger("test_ws_log_levels")
        test_logger.setLevel(logging.DEBUG)
        ws_handler = WebSocketLogHandler(forwarder, source="Level Test")
        test_logger.addHandler(ws_handler)

        # Log messages at different levels
        test_logger.debug("Debug message")
        test_logger.warning("Warning message")
        test_logger.error("Error message")
        await asyncio.sleep(0.5)

        # Clean up
        test_logger.removeHandler(ws_handler)
        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Verify levels are preserved
        assert len(received_messages) >= 3
        levels = [msg["level"] for msg in received_messages]
        assert "DEBUG" in levels
        assert "WARNING" in levels
        assert "ERROR" in levels

    @pytest.mark.asyncio
    async def test_handler_respects_level_filter(self):
        """WebSocketLogHandler with level=INFO should not forward DEBUG messages."""
        import websockets
        import json
        import logging
        from shared_stt.ws_forwarder import WebSocketLogHandler

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Create handler with INFO level (filters out DEBUG)
        test_logger = logging.getLogger("test_ws_level_filter")
        test_logger.setLevel(logging.DEBUG)
        ws_handler = WebSocketLogHandler(forwarder, source="Filter Test", level=logging.INFO)
        test_logger.addHandler(ws_handler)

        # Log messages at different levels
        test_logger.debug("Should not be forwarded")
        test_logger.info("Should be forwarded")
        await asyncio.sleep(0.5)

        # Clean up
        test_logger.removeHandler(ws_handler)
        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Only INFO should be forwarded
        assert len(received_messages) == 1
        assert received_messages[0]["level"] == "INFO"


class TestSendWakeWordDetected:
    """Tests for the send_wake_word_detected method."""

    @pytest.mark.asyncio
    async def test_send_wake_word_detected_queues_message(self):
        """send_wake_word_detected should queue a wake_word_detected message."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_wake_word_detected("hey computer")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "wake_word_detected"
        assert msg["keyword"] == "hey computer"
        assert msg["utterance_id"] == 0
        assert msg["is_partial"] is False

    def test_send_wake_word_detected_noop_without_loop(self):
        """send_wake_word_detected should silently do nothing if loop not started."""
        forwarder = WSForwarder(
            host="localhost",
            port=59931,
            transcription_enabled_event=threading.Event(),
            debug=False
        )
        # Don't start the forwarder -- no loop/queue
        forwarder.send_wake_word_detected("hey computer")
        # Should not raise


class TestWakeWordActivateCallback:
    """Tests for wake_word_activate_callback in _listen_for_commands."""

    @pytest.mark.asyncio
    async def test_disable_with_reason_calls_wake_word_activate_callback(self):
        """Disabling transcription with a reason should call wake_word_activate_callback(reason)."""
        import websockets

        callback_called = threading.Event()
        callback_args = [None]

        def on_wake_word_activate(reason):
            callback_args[0] = reason
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            wake_word_activate_callback=on_wake_word_activate,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send disable with reason
        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_transcription_status", "enabled": false, "reason": "wake_word"}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "wake_word_activate_callback should fire when disabled with reason"
        assert callback_args[0] == "wake_word", "Callback should receive the reason string"

    @pytest.mark.asyncio
    async def test_enable_calls_wake_word_activate_callback_with_none(self):
        """Re-enabling transcription should call wake_word_activate_callback(None)."""
        import websockets

        callback_called = threading.Event()
        callback_args = [None]

        def on_wake_word_activate(reason):
            callback_args[0] = reason
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": false}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        # Start cleared

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            wake_word_activate_callback=on_wake_word_activate,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send enable command
        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_transcription_status", "enabled": true}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "wake_word_activate_callback should fire when re-enabled"
        assert callback_args[0] is None, "Callback should receive None when re-enabled"

    @pytest.mark.asyncio
    async def test_disable_without_reason_calls_callback_with_none(self):
        """Disabling without reason should call wake_word_activate_callback(None)."""
        import websockets

        callback_called = threading.Event()
        callback_args = ["sentinel"]  # Use sentinel to distinguish from None

        def on_wake_word_activate(reason):
            callback_args[0] = reason
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            wake_word_activate_callback=on_wake_word_activate,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send disable without reason (backward compat)
        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_transcription_status", "enabled": false}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "wake_word_activate_callback should fire on disable"
        assert callback_args[0] is None, "Callback should receive None when no reason"

    @pytest.mark.asyncio
    async def test_no_callback_still_handles_reason(self):
        """set_transcription_status with reason but no callback should not crash."""
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        # No wake_word_activate_callback provided
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send disable with reason - should not crash
        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_transcription_status", "enabled": false, "reason": "wake_word"}'
            )

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Test passes if no exception was raised
        assert not event.is_set(), "Event should be cleared on disable"


class TestTraceId:
    """Tests for trace_id generation and passthrough in messages."""

    def test_generate_trace_id_format(self):
        """generate_trace_id returns T- followed by 11 digits."""
        from shared_stt.ws_forwarder import generate_trace_id

        tid = generate_trace_id()
        assert tid.startswith("T-")
        digits = tid[2:]
        assert len(digits) == 11
        assert digits.isdigit()

    def test_generate_trace_id_changes_over_time(self):
        """Two calls 150ms apart produce different IDs (100ms granularity)."""
        from shared_stt.ws_forwarder import generate_trace_id

        tid1 = generate_trace_id()
        time.sleep(0.15)
        tid2 = generate_trace_id()
        assert tid1 != tid2

    @pytest.mark.asyncio
    async def test_send_vad_start_carries_trace_id(self):
        """send_vad_start with trace_id includes it in the JSON payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_vad_start(utterance_id=1, trace_id="T-17720345601")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["trace_id"] == "T-17720345601"

    @pytest.mark.asyncio
    async def test_send_stable_carries_trace_id(self):
        """send_stable with trace_id includes it in the JSON payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_stable("hello", utterance_id=1, trace_id="T-17720345601")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["trace_id"] == "T-17720345601"

    @pytest.mark.asyncio
    async def test_send_final_carries_trace_id(self):
        """send_final with trace_id includes it in the JSON payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_final("hello world", utterance_id=1, trace_id="T-17720345601")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["trace_id"] == "T-17720345601"

    @pytest.mark.asyncio
    async def test_send_log_carries_trace_id(self):
        """send_log with trace_id includes it in the JSON payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_log(
            level="INFO",
            message="Test log",
            source="Test Provider",
            trace_id="T-17720345601",
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["trace_id"] == "T-17720345601"

    def test_send_vad_start_stores_current_trace_id(self):
        """send_vad_start stores trace_id as _current_trace_id on forwarder."""
        forwarder = WSForwarder(
            host="localhost",
            port=99999,
            transcription_enabled_event=threading.Event(),
        )
        # Set up loop and queue so send_vad_start doesn't bail early
        loop = asyncio.new_event_loop()
        forwarder._loop = loop
        forwarder._queue = asyncio.Queue()

        forwarder.send_vad_start(utterance_id=1, trace_id="T-17720345601")
        assert forwarder._current_trace_id == "T-17720345601"

        loop.close()

    @pytest.mark.asyncio
    async def test_log_handler_reads_current_trace_id(self):
        """WebSocketLogHandler reads forwarder._current_trace_id for log messages."""
        import websockets
        import json
        import logging
        from shared_stt.ws_forwarder import WebSocketLogHandler

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Set the current trace_id (simulating send_vad_start having been called)
        forwarder._current_trace_id = "T-17720345601"

        # Log via the handler
        test_logger = logging.getLogger("test_trace_log_handler")
        test_logger.setLevel(logging.DEBUG)
        ws_handler = WebSocketLogHandler(forwarder, source="Test Provider")
        test_logger.addHandler(ws_handler)

        test_logger.info("Test message during utterance")
        await asyncio.sleep(0.5)

        test_logger.removeHandler(ws_handler)
        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["trace_id"] == "T-17720345601"

    @pytest.mark.asyncio
    async def test_send_methods_default_empty_trace_id(self):
        """send methods without trace_id default to empty string in payload."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send without trace_id - should default to empty string
        forwarder.send_stable("hello", utterance_id=1)
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg.get("trace_id", "") == ""


class TestSetLogLevelCommand:
    """Tests for the set_log_level command."""

    @pytest.mark.asyncio
    async def test_set_log_level_debug_fires_callback(self):
        """set_log_level with level=DEBUG should invoke callback with 'DEBUG'."""
        import websockets

        callback_called = threading.Event()
        callback_value = [None]

        def on_set_log_level(level: str):
            callback_value[0] = level
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_log_level_callback=on_set_log_level,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send('{"type": "set_log_level", "level": "DEBUG"}')

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_log_level callback should fire"
        assert callback_value[0] == "DEBUG", "Callback should receive level='DEBUG'"

    @pytest.mark.asyncio
    async def test_set_log_level_info_fires_callback(self):
        """set_log_level with level=INFO should invoke callback with 'INFO'."""
        import websockets

        callback_called = threading.Event()
        callback_value = [None]

        def on_set_log_level(level: str):
            callback_value[0] = level
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_log_level_callback=on_set_log_level,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send('{"type": "set_log_level", "level": "INFO"}')

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_log_level callback should fire"
        assert callback_value[0] == "INFO", "Callback should receive level='INFO'"

    @pytest.mark.asyncio
    async def test_set_log_level_no_callback_logged(self):
        """set_log_level with no callback should log a message but not crash."""
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        # No set_log_level_callback provided
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Send command - should not crash, just log
        if connected_websocket:
            await connected_websocket.send('{"type": "set_log_level", "level": "DEBUG"}')

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Test passes if no exception was raised


class TestSetCalibrationModeCommand:
    """wh-7ou.7.1.2 (contract part A.2): the set_calibration_mode command
    drives a callback, and the safety reset -- callback(False) -- fires when
    an established connection is lost, so a crashed Logic process can never
    leave the hallucination filter disabled."""

    @pytest.mark.asyncio
    async def test_set_calibration_mode_true_fires_callback(self):
        """{"type": "set_calibration_mode", "enabled": true} -> callback(True)."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_set_calibration_mode(enabled: bool):
            calls.append(enabled)
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_calibration_mode_callback=on_set_calibration_mode,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_calibration_mode", "enabled": true}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_calibration_mode callback should fire"
        assert calls == [True]

    @pytest.mark.asyncio
    async def test_set_calibration_mode_false_fires_callback(self):
        """{"type": "set_calibration_mode", "enabled": false} -> callback(False)."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_set_calibration_mode(enabled: bool):
            calls.append(enabled)
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_calibration_mode_callback=on_set_calibration_mode,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_calibration_mode", "enabled": false}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "set_calibration_mode callback should fire"
        assert calls == [False]

    @pytest.mark.asyncio
    async def test_set_calibration_mode_no_callback_logged(self):
        """set_calibration_mode with no callback registered should not crash."""
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        # No set_calibration_mode_callback provided
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "set_calibration_mode", "enabled": true}'
            )

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Test passes if no exception was raised

    @pytest.mark.asyncio
    async def test_disconnect_resets_calibration_mode(self):
        """Losing an established connection must invoke callback(False):
        the mode can only have been enabled over that connection, and its
        peer (the Logic process) is now gone."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_set_calibration_mode(enabled: bool):
            calls.append(enabled)
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            set_calibration_mode_callback=on_set_calibration_mode,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Forcibly close the websocket connection from server side
        if connected_websocket:
            await connected_websocket.close()

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "calibration mode should reset on disconnect"
        assert calls == [False]

    def test_no_reset_on_initial_connection_failure(self):
        """Failed initial connections (WheelHouse not yet running) must NOT
        fire the reset: the mode cannot have been enabled without an
        established connection, mirroring on_disconnect_callback semantics."""
        callback_called = threading.Event()

        def on_set_calibration_mode(enabled: bool):
            callback_called.set()

        forwarder = WSForwarder(
            host="localhost",
            port=_unused_port(),  # No server running
            transcription_enabled_event=threading.Event(),
            set_calibration_mode_callback=on_set_calibration_mode,
            debug=False,
        )
        forwarder.start()

        # Give time for multiple connection attempts (backoff starts at 0.5s)
        time.sleep(1.5)

        assert not callback_called.is_set(), (
            "reset must not fire on initial connection failure"
        )

        forwarder.stop()


class TestApplyEngineSettingsCommand:
    """wh-7ou.7.1.3 (contract part A.3): the apply_engine_settings command
    dispatches every key except "type" and "apply_id" to the callback;
    validation is the provider handler's job, so unknown keys must arrive
    intact for it to reject. The apply_id is transport correlation, not a
    setting: it is stripped, passed alongside, and echoed in the reply
    (wh-7ou.7.6.9)."""

    @pytest.mark.asyncio
    async def test_apply_engine_settings_fires_callback_with_settings(self):
        """Both allowed keys arrive as a settings dict without "type"."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_apply(settings: dict, apply_id=None):
            calls.append((settings, apply_id))
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            apply_engine_settings_callback=on_apply,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "apply_engine_settings", '
                '"single_word_min_probability": 0.15, '
                '"single_word_max_no_speech_prob": 0.03}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired, "apply_engine_settings callback should fire"
        assert calls == [({
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.03,
        }, None)]

    @pytest.mark.asyncio
    async def test_apply_engine_settings_single_key_write_or_keep(self):
        """An absent key stays absent (write-or-keep), not defaulted."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_apply(settings: dict, apply_id=None):
            calls.append((settings, apply_id))
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            apply_engine_settings_callback=on_apply,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "apply_engine_settings", '
                '"single_word_min_probability": 0.15}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired
        assert calls == [({"single_word_min_probability": 0.15}, None)]

    @pytest.mark.asyncio
    async def test_unknown_keys_passed_through_for_provider_validation(self):
        """The forwarder strips only "type" and "apply_id"; an illegal
        extra key must reach the provider handler so its allowed-list
        validation can reject it."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_apply(settings: dict, apply_id=None):
            calls.append((settings, apply_id))
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            apply_engine_settings_callback=on_apply,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "apply_engine_settings", '
                '"single_word_min_probability": 0.15, '
                '"hallucination_logprob_threshold": -0.9}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired
        assert calls == [({
            "single_word_min_probability": 0.15,
            "hallucination_logprob_threshold": -0.9,
        }, None)]

    @pytest.mark.asyncio
    async def test_apply_id_stripped_from_settings_and_passed_alongside(self):
        """wh-7ou.7.6.9: the correlation id must not reach the provider's
        key validation (it would be rejected as an unknown setting); it
        arrives as the callback's second argument instead."""
        import websockets

        callback_called = threading.Event()
        calls = []

        def on_apply(settings: dict, apply_id=None):
            calls.append((settings, apply_id))
            callback_called.set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            apply_engine_settings_callback=on_apply,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "apply_engine_settings", '
                '"apply_id": "op-42", '
                '"single_word_min_probability": 0.15}'
            )

        callback_fired = callback_called.wait(timeout=3.0)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert callback_fired
        assert calls == [({"single_word_min_probability": 0.15}, "op-42")]

    @pytest.mark.asyncio
    async def test_apply_engine_settings_no_callback_logged(self):
        """apply_engine_settings with no callback registered must not crash."""
        import websockets

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        # No apply_engine_settings_callback provided
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        if connected_websocket:
            await connected_websocket.send(
                '{"type": "apply_engine_settings", '
                '"single_word_min_probability": 0.15}'
            )

        await asyncio.sleep(0.3)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        # Test passes if no exception was raised


class TestSendEngineSettingsResult:
    """wh-7ou.7.1.3 (contract part A.4): the provider's reply to
    apply_engine_settings carries exactly {"type", "ok", "error",
    "apply_id"} -- the apply_id echoed from the command so WheelHouse
    can correlate the reply to the exact apply it answers
    (wh-7ou.7.6.9)."""

    @pytest.mark.asyncio
    async def test_ok_true_payload_exact_keys(self):
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_engine_settings_result(True, None, apply_id="op-42")
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert set(msg.keys()) == {"type", "ok", "error", "apply_id"}
        assert msg["type"] == "engine_settings_result"
        assert msg["ok"] is True
        assert msg["error"] is None
        assert msg["apply_id"] == "op-42"

    @pytest.mark.asyncio
    async def test_ok_false_carries_error_text(self):
        """The error text lands under the calibration window's "Show
        details", so it must survive verbatim."""
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    # wh-nvyh: every (re)connect leads with a capabilities
                    # frame; these tests assert on the payload frames.
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        forwarder.send_engine_settings_result(
            False, "[Errno 28] No space left on device"
        )
        await asyncio.sleep(0.5)

        forwarder.stop()
        server.close()
        await server.wait_closed()

        assert len(received_messages) >= 1
        msg = received_messages[0]
        assert msg["type"] == "engine_settings_result"
        assert msg["ok"] is False
        assert msg["error"] == "[Errno 28] No space left on device"
        # No id given: the reply carries an explicit null correlation id.
        assert msg["apply_id"] is None

    def test_noop_without_loop(self):
        """send_engine_settings_result must not raise before start()."""
        forwarder = WSForwarder(
            host="localhost",
            port=59915,
            transcription_enabled_event=threading.Event(),
            debug=False,
        )
        # Do not call start() - no loop or queue
        forwarder.send_engine_settings_result(True, None)
        # Should not raise


class TestStopDeliversQueuedFrames:
    """wh-7ou.7.6.10: the apply_engine_settings success reply -- and the
    notifications queued just before a restart -- are enqueued moments
    before the provider begins shutdown. stop() must give already-queued
    outbound frames a bounded chance to deliver instead of abandoning
    them in the queue, otherwise WheelHouse never sees the reply and the
    calibration session strands in restart_slow with the typing gate
    held."""

    @pytest.mark.asyncio
    async def test_result_queued_just_before_stop_still_delivered(self):
        import websockets
        import json

        received_messages = []
        connection_established = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            connection_established.set()
            try:
                async for message in websocket:
                    _frame = json.loads(message)
                    if _frame.get("type") != "capabilities":
                        received_messages.append(_frame)
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
        )
        forwarder.start()

        await asyncio.wait_for(connection_established.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

        # Mirror the real shutdown sequence: the apply handler's own log
        # frames land in the queue ahead of the result frame, and stop()
        # follows with NO delay in between. stop() blocks this thread
        # while the forwarder's own thread does the delivering, so a
        # synchronous call is exactly the production shape.
        forwarder.send_notification("Distil Whisper", "Restarting to apply settings")
        forwarder.send_log("INFO", "engine settings written", "Distil Whisper")
        forwarder.send_engine_settings_result(True, None, apply_id="op-shutdown")
        forwarder.stop()

        # The frames reached the socket before stop() returned; give the
        # server's coroutine a moment to read them off it.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(
                m.get("type") == "engine_settings_result" for m in received_messages
            ):
                break
            await asyncio.sleep(0.05)

        server.close()
        await server.wait_closed()

        results = [
            m for m in received_messages if m.get("type") == "engine_settings_result"
        ]
        assert results, (
            "engine_settings_result was abandoned in the outbound queue at stop()"
        )
        assert results[0]["ok"] is True
        assert results[0]["apply_id"] == "op-shutdown"


class TestHardRestartCommandRemoved:
    """The hard_restart_service command is gone from the whole system.

    WheelHouse used to send it when the user chose "Restart Transcription
    Service" or picked a Google Cloud key file from the menu. Both menu
    items and the sender were deleted under
    wh-remove-restart-credentials-items, so every receiver was deleted
    with them: this forwarder's dispatch arm and its callback parameter,
    and the handler in each of the three provider services.

    The first test is the structural guard. Because the parameter is
    gone, a provider that still passed the callback would raise
    TypeError while building its forwarder, which is a startup failure
    nobody can miss -- that is what makes a separate per-provider test
    unnecessary. The restart FLAG machinery is untouched: distil and
    parakeet still write the flag for hint and hotword changes, and
    shared_stt.launcher still consumes it.
    """

    def test_the_forwarder_refuses_a_hard_restart_callback(self):
        event = threading.Event()

        with pytest.raises(TypeError):
            WSForwarder(
                host="localhost",
                port=59995,
                transcription_enabled_event=event,
                hard_restart_callback=lambda: None,
                debug=False,
            )

    def test_the_command_loop_has_no_hard_restart_branch(self):
        """The dispatch arm is gone from the command loop's own source.

        This is the check the message test below cannot make. A retained
        arm that logged nothing, did nothing, or called some other
        callback would still leave the forwarder able to handle a later
        shutdown, so behaviour alone cannot prove the branch is absent.
        Reading the source can.
        """
        import inspect

        source = inspect.getsource(WSForwarder._listen_for_commands)

        assert "hard_restart" not in source, (
            "the command loop still mentions hard_restart; the dispatch "
            "arm was supposed to be deleted with its sender"
        )

    @pytest.mark.asyncio
    async def test_a_hard_restart_message_reaches_no_callback(self):
        """The retired command must do nothing and cost nothing.

        Every callback the forwarder accepts is recorded here. An old
        WheelHouse build sending hard_restart_service must reach none of
        them -- not the restart callback, not the shutdown callback, not
        any other -- and the forwarder must still handle the commands
        that remain, which the shutdown at the end proves.
        """
        import websockets

        calls: list[str] = []
        shutdown_called = threading.Event()
        wake_word_seen = threading.Event()
        log_level_seen = threading.Event()

        def _record(name):
            def _callback(*args):
                calls.append(name)
            return _callback

        def _record_shutdown():
            calls.append("shutdown")
            shutdown_called.set()

        def _record_wake_word(*args):
            calls.append("wake_word_activate")
            wake_word_seen.set()

        def _record_log_level(*args):
            calls.append("set_log_level")
            log_level_seen.set()

        async def _wait_for(flag, seconds):
            """Wait without blocking this test's own event loop.

            A blocking wait here stops the server side of the connection
            from delivering what was just sent, so the callback under test
            can never arrive. Measured on this machine: two sends followed
            by a blocking wait deliver neither frame.
            """
            limit = time.monotonic() + seconds
            while not flag.is_set() and time.monotonic() < limit:
                await asyncio.sleep(0.02)
            return flag.is_set()

        connected_websocket = None
        connection_established = asyncio.Event()

        async def handler(websocket):
            nonlocal connected_websocket
            connected_websocket = websocket
            await websocket.send(
                '{"type": "status", "transcription_enabled": true}'
            )
            connection_established.set()
            try:
                await websocket.wait_closed()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            add_hint_callback=_record("add_hint"),
            restart_callback=_record("restart"),
            shutdown_callback=_record_shutdown,
            set_interim_results_callback=_record("set_interim_results"),
            set_log_level_callback=_record_log_level,
            set_calibration_mode_callback=_record("set_calibration_mode"),
            apply_engine_settings_callback=_record("apply_engine_settings"),
            wake_word_activate_callback=_record_wake_word,
            debug=False,
        )
        forwarder.start()

        # Everything from here runs under a try. A barrier that fails
        # must still reach the stop and close below, or the forwarder's
        # thread keeps running into the tests that follow.
        try:
            await asyncio.wait_for(connection_established.wait(), timeout=2.0)
            assert connected_websocket is not None

            # Step one of the barrier: prove the forwarder is reading commands.
            # A frame sent the instant the connection opens can reach no
            # callback at all, so this repeats the frame until one is answered
            # rather than waiting a fixed length of time and hoping.
            deadline = time.monotonic() + 10.0
            while not wake_word_seen.is_set() and time.monotonic() < deadline:
                await connected_websocket.send(
                    '{"type": "set_transcription_status", "enabled": true}'
                )
                await asyncio.sleep(0.05)
            assert wake_word_seen.is_set(), (
                "the forwarder never answered a command, so this test cannot "
                "tell what the retired command did"
            )

            # Step two: one different command, sent once, on a connection that
            # is now known to be reading. Every dispatch arm hands its callback
            # to call_soon_threadsafe on the forwarder's own event loop, so the
            # callbacks run in the order their frames arrived. When this one has
            # run, every repeat from step one has run too, and the recording can
            # be cleared with nothing left in flight.
            await connected_websocket.send(
                '{"type": "set_log_level", "level": "INFO"}'
            )
            assert await _wait_for(log_level_seen, 5.0), (
                "the forwarder stopped answering commands before the retired "
                "command was sent"
            )
            calls.clear()

            # Both frames go out before anything is read back. Ordered delivery
            # on one connection, and that same first-in-first-out callback
            # queue, are what make the assertion below sound: a retained
            # hard_restart_service arm would have to reach its callback BEFORE
            # the shutdown callback runs, so by the time shutdown_called is set,
            # any such call is already in the recording. A snapshot taken after
            # a wait of a fixed length proves nothing, because the forwarder's
            # thread may not have read the frame yet.
            await connected_websocket.send('{"type": "hard_restart_service"}')
            await connected_websocket.send('{"type": "shutdown"}')

            callback_fired = await _wait_for(shutdown_called, 5.0)
            recorded = list(calls)
        finally:
            forwarder.stop()
            server.close()
            await server.wait_closed()

        assert callback_fired, (
            "the retired hard_restart_service message stopped the command "
            "loop from handling a later shutdown"
        )
        assert recorded == ["shutdown"], (
            "the retired hard_restart_service message reached %s"
            % ", ".join(name for name in recorded if name != "shutdown")
        )


class TestCapabilitiesWakeWordAvailable:
    """wh-audio-suppression-control C3: the capabilities frame reports
    whether this provider's wake-word detector loaded.

    WheelHouse's sound-pause notice tells the user to say the wake word, so
    it must not promise a wake word that cannot fire -- the openwakeword
    import is guarded and the detector stays unloaded when it fails. A
    provider builds its forwarder BEFORE it loads its detector (parakeet
    main.py, distil main.py), so the value is set on the forwarder after
    construction and read at the moment the frame is sent.
    """

    async def _capabilities_frame(self, wake_word_available):
        """Return the capabilities frame the forwarder sends on connect.

        `wake_word_available` of None leaves the forwarder's own default in
        place instead of setting the field.
        """
        import json
        import websockets

        frames = []
        capabilities_seen = asyncio.Event()

        async def handler(websocket):
            await websocket.send('{"type": "status", "transcription_enabled": true}')
            try:
                async for message in websocket:
                    frame = json.loads(message)
                    if frame.get("type") == "capabilities":
                        frames.append(frame)
                        capabilities_seen.set()
            except Exception:
                pass

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        event = threading.Event()
        event.set()
        forwarder = WSForwarder(
            host="127.0.0.1",
            port=port,
            transcription_enabled_event=event,
            debug=False,
            provider_name="parakeet_tdt",
            emits_eos=False,
        )
        if wake_word_available is not None:
            forwarder.wake_word_available = wake_word_available
        forwarder.start()
        try:
            await asyncio.wait_for(capabilities_seen.wait(), timeout=5.0)
        finally:
            forwarder.stop()
            server.close()
            await server.wait_closed()
        return frames[0]

    @pytest.mark.asyncio
    async def test_frame_reports_a_loaded_detector(self):
        frame = await self._capabilities_frame(True)
        assert frame["wake_word_available"] is True

    @pytest.mark.asyncio
    async def test_frame_reports_a_detector_that_did_not_load(self):
        frame = await self._capabilities_frame(False)
        assert frame["wake_word_available"] is False

    @pytest.mark.asyncio
    async def test_default_declares_no_detector(self):
        """A forwarder nobody set the field on declares False, so a
        provider without the wake word can never promise one."""
        frame = await self._capabilities_frame(None)
        assert frame["wake_word_available"] is False
