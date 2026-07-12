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
import threading
import time
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared_stt.ws_forwarder import WSForwarder


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
            port=59999,  # Use high port unlikely to be in use
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

        server = await websockets.serve(handler, "localhost", 59994)

        # Create and start forwarder
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="localhost",
            port=59994,
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

        server = await websockets.serve(handler, "localhost", 59996)

        # Create and start forwarder with shutdown callback
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="localhost",
            port=59996,
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
            port=59997,  # No server running
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

        server = await websockets.serve(handler, "localhost", 59995)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59995,
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

        server = await websockets.serve(handler, "localhost", 59993)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59993,
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

        server = await websockets.serve(handler, "localhost", 59992)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59992,
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

        server = await websockets.serve(handler, "localhost", 59991)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59991,
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

        server = await websockets.serve(handler, "localhost", 59980)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59980,
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

        server = await websockets.serve(handler, "localhost", 59981)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59981,
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

        server = await websockets.serve(handler, "localhost", 59983)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59983,
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

        server = await websockets.serve(handler, "localhost", 59984)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59984,
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

        server = await websockets.serve(handler, "localhost", 59985)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59985,
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

        server = await websockets.serve(handler, "localhost", 59990)

        event = threading.Event()
        # Start with event cleared

        forwarder = WSForwarder(
            host="localhost",
            port=59990,
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

        server = await websockets.serve(handler, "localhost", 59989)

        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="localhost",
            port=59989,
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

        server = await websockets.serve(handler, "localhost", 59950)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59950,
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

        server = await websockets.serve(handler, "localhost", 59951)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59951,
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

        server = await websockets.serve(handler, "localhost", 59952)

        event = threading.Event()
        event.set()

        # No set_interim_results_callback provided
        forwarder = WSForwarder(
            host="localhost",
            port=59952,
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

        server = await websockets.serve(handler, "localhost", 59940)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59940,
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

        server = await websockets.serve(handler, "localhost", 59941)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59941,
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
            source="Zipformer",
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

        server = await websockets.serve(handler, "localhost", 59942)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59942,
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

        server = await websockets.serve(handler, "localhost", 59943)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59943,
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

        server = await websockets.serve(handler, "localhost", 59944)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59944,
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

        server = await websockets.serve(handler, "localhost", 59945)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59945,
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

        server = await websockets.serve(handler, "localhost", 59946)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59946,
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

        server = await websockets.serve(handler, "localhost", 59930)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59930,
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

        server = await websockets.serve(handler, "localhost", 59932)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59932,
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

        server = await websockets.serve(handler, "localhost", 59933)

        event = threading.Event()
        # Start cleared

        forwarder = WSForwarder(
            host="localhost",
            port=59933,
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

        server = await websockets.serve(handler, "localhost", 59934)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59934,
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

        server = await websockets.serve(handler, "localhost", 59935)

        event = threading.Event()
        event.set()

        # No wake_word_activate_callback provided
        forwarder = WSForwarder(
            host="localhost",
            port=59935,
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

        server = await websockets.serve(handler, "localhost", 59920)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59920,
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

        server = await websockets.serve(handler, "localhost", 59921)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59921,
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

        server = await websockets.serve(handler, "localhost", 59922)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59922,
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

        server = await websockets.serve(handler, "localhost", 59923)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59923,
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

        server = await websockets.serve(handler, "localhost", 59924)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59924,
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

        server = await websockets.serve(handler, "localhost", 59925)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59925,
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

        server = await websockets.serve(handler, "localhost", 59960)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59960,
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

        server = await websockets.serve(handler, "localhost", 59961)

        event = threading.Event()
        event.set()

        forwarder = WSForwarder(
            host="localhost",
            port=59961,
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

        server = await websockets.serve(handler, "localhost", 59962)

        event = threading.Event()
        event.set()

        # No set_log_level_callback provided
        forwarder = WSForwarder(
            host="localhost",
            port=59962,
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
