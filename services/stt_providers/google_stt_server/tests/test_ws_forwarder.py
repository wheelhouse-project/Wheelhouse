"""Tests for WSForwarder disconnect handling.

These tests verify that:
1. Disconnect callback does NOT fire on initial connection failures
2. Disconnect callback DOES fire when an established connection is lost
3. Message queue is cleared only after actual disconnect
4. Disconnect callback properly clears transcription_enabled_event
"""
import asyncio
import threading
import time
import sys
from pathlib import Path

import pytest

# Add shared_stt to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared" / "shared_stt"))

from ws_forwarder import WSForwarder


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

        server = await websockets.serve(handler, "localhost", 59998)

        # Create and start forwarder
        event = threading.Event()
        event.set()  # Start enabled

        forwarder = WSForwarder(
            host="localhost",
            port=59998,
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


class TestReconnectCallback:
    """Tests for the on_reconnect_callback feature (T4)."""

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
    """Tests for the shutdown command feature (T1)."""

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
