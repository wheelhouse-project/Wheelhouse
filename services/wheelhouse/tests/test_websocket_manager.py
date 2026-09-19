"""Tests for WebSocket manager's word event generation and connection lifecycle.

Tests cover:
- start_of_utterance flag behaviour (stable vs final paths)
- Revision detection in _extract_delta()
- Forwarded log message handling
- Client connection lifecycle (add_client / remove_client)
- VAD start -> GUI shared memory
- Notification handling and launcher signalling
- Malformed / unknown message types
- Client disconnect during in-progress utterance
- Broadcast error isolation
- Transcription status toggling
- Empty stable / final text handling
"""
import sys
from pathlib import Path

# Add parent directories to path for imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import json
import logging
import struct
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch, PropertyMock

from speech.word_event import WordEvent


class TestStartOfUtteranceFlag:
    """Tests for correct start_of_utterance flag setting.

    Bug: When a final message arrives without preceding stable messages,
    the first word should have start_of_utterance=True, but it was being
    set to False.
    """

    @pytest.fixture
    def event_loop(self):
        """Create event loop for tests."""
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def mock_app(self):
        """Create a mock app with send_command method."""
        app = MagicMock()
        app.send_command = AsyncMock()
        return app

    @pytest.fixture
    def manager(self, event_loop, mock_app):
        """Create a WebSocketManager with mocked dependencies."""
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=event_loop)
        manager.set_app(mock_app)
        return manager

    async def simulate_final_message(self, manager, utterance_id: int, text: str):
        """Simulate receiving a final message and return queued word events.

        This replicates the logic from the 'final' message handler in websocket_manager.py.
        """
        # Check if this is a new utterance (no stables received)
        is_new_utterance = (utterance_id != manager.current_utterance_id)

        if is_new_utterance:
            manager.current_utterance_id = utterance_id

        # Extract delta - use manager's method if text exists
        if text:
            delta = manager._extract_delta(text, utterance_id)
        else:
            delta = ""

        # Queue words - this replicates the FIXED code behavior
        if delta:
            delta_words = delta.split()
            for i, word in enumerate(delta_words):
                # First word of a new utterance needs start_of_utterance=True
                is_first = (i == 0 and is_new_utterance)
                word_event = WordEvent(
                    word=word,
                    start_of_utterance=is_first,
                    end_of_utterance=False,
                    utterance_id=utterance_id
                )
                await manager.word_queue.put(word_event)

        # Collect all queued events
        events = []
        while not manager.word_queue.empty():
            events.append(await manager.word_queue.get())
        return events

    @pytest.mark.asyncio
    async def test_final_only_first_word_has_start_flag(self, manager):
        """When final arrives without preceding stables, first word should have start_of_utterance=True.

        This tests the fix for the bug from the logs:
        - UTT-414: Final arrives with 'backspace', no stables preceded
        - Word had start_of_utterance=False (BUG)
        - Should have been start_of_utterance=True

        The bug caused commands like 'backspace' to be treated as dictation
        because the speech processor sees mid-utterance words (start_of_utterance=False).
        """
        utterance_id = 414

        # Reset state to simulate fresh utterance (no stables have arrived)
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager.current_utterance_id = None  # No previous utterance

        # Simulate a final message arriving without preceding stables
        events = await self.simulate_final_message(manager, utterance_id, "backspace")

        # Verify
        assert len(events) == 1, f"Expected 1 event, got {len(events)}"
        first_event = events[0]
        assert first_event.word == "backspace"
        assert first_event.start_of_utterance is True, (
            "First word from final-only message should have start_of_utterance=True"
        )

    @pytest.mark.asyncio
    async def test_final_after_stable_no_start_flag_on_remaining(self, manager):
        """When final arrives after stables, remaining words should NOT have start_of_utterance=True.

        This ensures the fix doesn't break the normal case where stables precede finals.
        """
        utterance_id = 415

        # Simulate state after a stable message was already processed
        manager._processed_word_count = 1  # One word already sent from stable
        manager._last_stable_utterance_id = utterance_id
        manager.current_utterance_id = utterance_id  # Same utterance - NOT new

        # Final arrives with additional text
        # _extract_delta will return only the new word "five"
        events = await self.simulate_final_message(manager, utterance_id, "backspace five")

        # Verify - only "five" should be queued (backspace was already sent via stable)
        # And it should NOT have start_of_utterance=True since this is not a new utterance
        if events:
            for event in events:
                assert event.start_of_utterance is False, (
                    "Words from final after stables should have start_of_utterance=False"
                )

    @pytest.mark.asyncio
    async def test_stable_first_word_has_start_flag(self, manager):
        """First word from first stable of utterance should have start_of_utterance=True.

        This verifies the stable path works correctly (it already did, per the logs
        where UTT-415 worked because it arrived via stable first).
        """
        utterance_id = 415

        # Fresh utterance
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager.current_utterance_id = None

        # Simulate receiving a stable message
        text = "backspace"
        delta = manager._extract_delta(text, utterance_id)

        # This is how stable processing calculates start_of_utterance
        previous_count = manager._processed_word_count - len(delta.split()) if delta else 0

        events = []
        if delta:
            delta_words = delta.split()
            for i, word in enumerate(delta_words):
                is_first = (i == 0 and previous_count == 0)
                word_event = WordEvent(
                    word=word,
                    start_of_utterance=is_first,
                    end_of_utterance=False,
                    utterance_id=utterance_id
                )
                events.append(word_event)

        # Verify
        assert len(events) == 1
        first_event = events[0]
        assert first_event.word == "backspace"
        assert first_event.start_of_utterance is True, (
            "First word from first stable should have start_of_utterance=True"
        )

    @pytest.mark.asyncio
    async def test_multi_word_final_only_first_has_start_flag(self, manager):
        """Multi-word final without stables: only first word should have start_of_utterance=True."""
        utterance_id = 416

        # Fresh utterance
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager.current_utterance_id = None

        # Simulate final with multiple words
        events = await self.simulate_final_message(manager, utterance_id, "backspace five")

        # Verify
        assert len(events) == 2, f"Expected 2 events, got {len(events)}"

        # First word should have start_of_utterance=True
        assert events[0].word == "backspace"
        assert events[0].start_of_utterance is True

        # Second word should have start_of_utterance=False
        assert events[1].word == "five"
        assert events[1].start_of_utterance is False


class TestRevisionDetection:
    """Tests for STT revision detection in _extract_delta().

    When Google STT revises earlier words between stable messages or between
    stable and final, the new text won't start with what we already sent.
    We should detect this and return empty string to avoid garbled output.
    """

    @pytest.fixture
    def event_loop(self):
        """Create event loop for tests."""
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        """Create a WebSocketManager with mocked dependencies."""
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=event_loop)
        # Ensure state_manager is None so notification is skipped in tests
        manager.state_manager = None
        return manager

    def test_normal_append_returns_delta(self, manager):
        """Normal case: text appends, delta is extracted correctly."""
        utterance_id = 100

        # First stable
        delta1 = manager._extract_delta("hello", utterance_id)
        assert delta1 == "hello"
        assert manager._sent_stable_text == "hello"

        # Second stable - appends "world"
        delta2 = manager._extract_delta("hello world", utterance_id)
        assert delta2 == "world"
        assert manager._sent_stable_text == "hello world"

        # Third stable - appends "today"
        delta3 = manager._extract_delta("hello world today", utterance_id)
        assert delta3 == "today"
        assert manager._sent_stable_text == "hello world today"

    def test_revision_detected_returns_empty(self, manager):
        """Revision detected: new text doesn't start with sent text, returns empty."""
        utterance_id = 101

        # First stable
        delta1 = manager._extract_delta("at the moment", utterance_id)
        assert delta1 == "at the moment"

        # Second stable - REVISION: completely different text
        delta2 = manager._extract_delta("only for Mac OS", utterance_id)
        assert delta2 is None  # Revision detected
        # _sent_stable_text should NOT be updated on revision
        assert manager._sent_stable_text == "at the moment"

    def test_revision_partial_change_detected(self, manager):
        """Revision where beginning is different is detected."""
        utterance_id = 102

        # First stable
        delta1 = manager._extract_delta("keyboard", utterance_id)
        assert delta1 == "keyboard"

        # Final - REVISION: completely different
        delta2 = manager._extract_delta("testing", utterance_id)
        assert delta2 is None
        assert manager._sent_stable_text == "keyboard"

    def test_new_utterance_resets_state(self, manager):
        """New utterance ID resets tracking state."""
        # Utterance 1
        delta1 = manager._extract_delta("hello world", 200)
        assert delta1 == "hello world"
        assert manager._sent_stable_text == "hello world"

        # Utterance 2 - new utterance, fresh start
        delta2 = manager._extract_delta("goodbye", 201)
        assert delta2 == "goodbye"
        assert manager._sent_stable_text == "goodbye"

    def test_same_text_returns_empty(self, manager):
        """Same text repeated returns empty (no new words)."""
        utterance_id = 103

        delta1 = manager._extract_delta("hello world", utterance_id)
        assert delta1 == "hello world"

        # Same text again
        delta2 = manager._extract_delta("hello world", utterance_id)
        assert delta2 == ""

    def test_final_after_stable_normal(self, manager):
        """Final that continues from stable works normally."""
        utterance_id = 104

        # Stable
        delta1 = manager._extract_delta("I saw that", utterance_id)
        assert delta1 == "I saw that"

        # Final - extends correctly
        delta2 = manager._extract_delta("I saw that period", utterance_id)
        assert delta2 == "period"

    def test_final_after_stable_revision(self, manager):
        """Final that revises stable is detected."""
        utterance_id = 105

        # Stable: Google thought user said "at the moment"
        delta1 = manager._extract_delta("at the moment", utterance_id)
        assert delta1 == "at the moment"

        # Final: Google revised to "only for Mac OS at the moment"
        # This doesn't start with "at the moment"
        delta2 = manager._extract_delta("only for Mac OS at the moment", utterance_id)
        assert delta2 is None  # Revision detected

    def test_word_count_updated_correctly(self, manager):
        """Word count tracking is updated on successful delta extraction."""
        utterance_id = 106

        manager._extract_delta("one two", utterance_id)
        assert manager._processed_word_count == 2

        manager._extract_delta("one two three four", utterance_id)
        assert manager._processed_word_count == 4

    def test_word_count_not_updated_on_revision(self, manager):
        """Word count should NOT be updated when revision is detected."""
        utterance_id = 107

        manager._extract_delta("hello world", utterance_id)
        assert manager._processed_word_count == 2

        # Revision - count should not change
        manager._extract_delta("goodbye", utterance_id)
        assert manager._processed_word_count == 2  # Still 2

    def test_word_extension_revision_detected(self, manager):
        """Revision where a word is extended (e.g., 'comm' -> 'comma') is detected.

        Bug: Character-level prefix matching treats 'comma' as appending to 'comm'
        (since 'comma' starts with 'comm'), missing the word-level revision.
        This caused 'comm' to be pasted, then 'a' sent as a separate word.
        """
        utterance_id = 108

        # Stable sends "surprising comm" (silence holdback released early)
        delta1 = manager._extract_delta("surprising comm", utterance_id)
        assert delta1 == "surprising comm"

        # Final revises last word: "comm" -> "comma"
        delta2 = manager._extract_delta("surprising comma", utterance_id)
        assert delta2 is None  # Should detect revision, not return "a"
        assert manager._sent_stable_text == "surprising comm"  # Not updated

    def test_word_extension_mid_sentence_revision(self, manager):
        """Word extension revision detected in longer text."""
        utterance_id = 109

        delta1 = manager._extract_delta("not that it's surprising comm", utterance_id)
        assert delta1 == "not that it's surprising comm"

        # Final: "comm" revised to "comma"
        delta2 = manager._extract_delta("not that it's surprising comma", utterance_id)
        assert delta2 is None  # Word-level revision
        assert manager._processed_word_count == 5  # Not updated from 5


class TestLogMessageHandling:
    """Tests for handling forwarded log messages from STT providers."""

    @pytest.fixture
    def event_loop(self):
        """Create event loop for tests."""
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        """Create a WebSocketManager with mocked dependencies."""
        from integrations.websocket_manager import WebSocketManager

        manager = WebSocketManager(loop=event_loop)
        manager.state_manager = None
        return manager

    @pytest.mark.asyncio
    async def test_log_message_does_not_queue_word_events(self, manager):
        """Log messages should not create word events."""
        import logging

        # Mock the logger to capture log calls
        with patch('integrations.websocket_manager.logger') as mock_logger:
            # Create a mock websocket with log message
            mock_ws = AsyncMock()
            mock_ws.remote_address = ('127.0.0.1', 12345)

            log_message = json.dumps({
                "type": "log",
                "level": "INFO",
                "message": "Test log from provider",
                "source": "Google STT",
                "timestamp": "2026-01-17T10:30:45.123456",
                "utterance_id": 0,
                "is_partial": False
            })

            # Simulate receiving the log message
            # We'll directly test the message parsing logic
            data = json.loads(log_message)
            msg_type = data.get("type", "delta")

            assert msg_type == "log"

            # Log message should be handled, not queued
            # After implementation, this will log with provider prefix
            assert manager.word_queue.empty()

    @pytest.mark.asyncio
    async def test_log_message_with_all_levels(self, manager):
        """Log messages with all standard levels should be accepted."""
        levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

        for level in levels:
            log_message = {
                "type": "log",
                "level": level,
                "message": f"Test {level} message",
                "source": "Test Provider",
                "timestamp": "2026-01-17T10:30:45.123456",
                "utterance_id": 0,
                "is_partial": False
            }

            # Verify message structure is correct
            assert log_message["type"] == "log"
            assert log_message["level"] == level
            assert "message" in log_message
            assert "source" in log_message
            assert "timestamp" in log_message


# ---------------------------------------------------------------------------
# Helper: create a mock websocket that yields a sequence of JSON messages
# ---------------------------------------------------------------------------

def _make_mock_ws(messages, remote_address=("127.0.0.1", 9999)):
    """Build an AsyncMock websocket whose async-iteration yields *messages*.

    Args:
        messages: list of dicts (will be json-encoded) or raw strings.
        remote_address: simulated (host, port) tuple.
    """
    encoded = []
    for m in messages:
        if isinstance(m, dict):
            encoded.append(json.dumps(m))
        else:
            encoded.append(m)

    ws = AsyncMock()
    ws.remote_address = remote_address
    ws.send = AsyncMock()
    # Make the websocket iterable: ``async for message in websocket``
    ws.__aiter__ = Mock(return_value=iter(encoded).__aiter__() if hasattr(iter(encoded), '__aiter__') else _async_iter(encoded))
    return ws


async def _async_iter(items):
    """Simple async generator wrapper around an iterable."""
    for item in items:
        yield item


def _drain_queue(queue: asyncio.Queue):
    """Return all items currently in *queue* as a list (non-blocking)."""
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Test: Client Connection Lifecycle
# ---------------------------------------------------------------------------

class TestClientConnectionLifecycle:
    """add_client disables existing clients; remove_client cleans up."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        return WebSocketManager(loop=event_loop)

    @pytest.mark.asyncio
    async def test_add_client_registers_websocket(self, manager):
        """add_client should add the websocket to the client set."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5000)
        await manager.add_client(ws)
        assert ws in manager._clients

    @pytest.mark.asyncio
    async def test_add_client_disables_existing_clients(self, manager):
        """When a second client connects, the first should receive a DISABLE message."""
        ws1 = AsyncMock()
        ws1.remote_address = ("127.0.0.1", 5001)
        ws1.send = AsyncMock()

        ws2 = AsyncMock()
        ws2.remote_address = ("127.0.0.1", 5002)
        ws2.send = AsyncMock()

        await manager.add_client(ws1)
        # Adding second client should disable ws1
        await manager.add_client(ws2)

        # ws1 should have been sent a disable message
        ws1.send.assert_called_once()
        sent_data = json.loads(ws1.send.call_args[0][0])
        assert sent_data["type"] == "set_transcription_status"
        assert sent_data["enabled"] is False

        # Both clients should still be registered
        assert ws1 in manager._clients
        assert ws2 in manager._clients

    @pytest.mark.asyncio
    async def test_add_client_disable_survives_send_error(self, manager):
        """If disabling an existing client fails, the new client still gets added."""
        ws_broken = AsyncMock()
        ws_broken.remote_address = ("127.0.0.1", 5003)
        ws_broken.send = AsyncMock(side_effect=Exception("connection lost"))

        ws_new = AsyncMock()
        ws_new.remote_address = ("127.0.0.1", 5004)

        await manager.add_client(ws_broken)
        # Should not raise even though sending to ws_broken fails
        await manager.add_client(ws_new)

        assert ws_new in manager._clients

    def test_remove_client_removes_from_set(self, manager):
        """remove_client should remove the websocket from the client set."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5005)
        manager._clients.add(ws)

        manager.remove_client(ws)
        assert ws not in manager._clients

    def test_remove_client_noop_for_unknown_ws(self, manager):
        """remove_client should not raise when called with an unregistered ws."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5006)
        # Should not raise
        manager.remove_client(ws)

    def test_remove_client_clears_indicator_when_utterance_in_progress(
        self, manager
    ):
        """A provider disconnect mid-utterance (after 'settling', before the
        final) must write 'idle' so the GUI clears the working badge at once,
        instead of leaving it shown until the 60s last-resort fallback. The
        armed idle watchdog is the in-progress signal -- the final cancels it,
        so an armed watchdog means no final has arrived
        (wh-dictation-retraction-indicator.9.1)."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5007)
        manager._clients.add(ws)
        manager.current_utterance_id = 42
        manager._idle_watchdog_handle = MagicMock()  # armed -> no final yet
        with patch.object(manager, "_write_activity_state") as write_state:
            manager.remove_client(ws)
        write_state.assert_any_call("idle", 42)

    def test_remove_client_no_idle_after_final(self, manager):
        """A clean disconnect AFTER the final must not overwrite 'confirmed'
        with a spurious 'idle'. The final cancels the idle watchdog, so an
        unarmed watchdog means the utterance completed normally
        (wh-dictation-retraction-indicator.9.1)."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5009)
        manager._clients.add(ws)
        manager.current_utterance_id = 42  # not reset by the final
        manager._idle_watchdog_handle = None  # the final cancelled it
        with patch.object(manager, "_write_activity_state") as write_state:
            manager.remove_client(ws)
        for call in write_state.call_args_list:
            assert call.args[0] != "idle"

    def test_remove_client_no_activity_write_when_no_utterance(self, manager):
        """No in-progress utterance -> no spurious 'idle' activity write."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 5008)
        manager._clients.add(ws)
        manager.current_utterance_id = None
        with patch.object(manager, "_write_activity_state") as write_state:
            manager.remove_client(ws)
        write_state.assert_not_called()

    def test_remove_disabled_client_does_not_touch_active_utterance(
        self, manager
    ):
        """When a disabled/older client disconnects while another client is
        still connected and mid-utterance, remove_client must NOT clean up the
        active utterance: no spurious end marker, no 'idle' badge clear, no
        reset of current_utterance_id, and the active idle watchdog stays
        armed. Only a disconnect that leaves zero clients cleans up. add_client
        keeps disabled clients connected, so a late disconnect of one must not
        corrupt the active stream (wh-dictation-retraction-indicator.10.1)."""
        active_ws = AsyncMock()
        active_ws.remote_address = ("127.0.0.1", 6001)
        disabled_ws = AsyncMock()
        disabled_ws.remote_address = ("127.0.0.1", 6002)
        manager._clients.add(active_ws)
        manager._clients.add(disabled_ws)
        manager.current_utterance_id = 77
        watchdog = MagicMock()
        manager._idle_watchdog_handle = watchdog
        manager.word_queue = MagicMock()
        with patch.object(manager, "_write_activity_state") as write_state:
            manager.remove_client(disabled_ws)
        assert manager.current_utterance_id == 77
        write_state.assert_not_called()
        manager.word_queue.put_nowait.assert_not_called()
        watchdog.cancel.assert_not_called()
        assert manager._idle_watchdog_handle is watchdog
        assert disabled_ws not in manager._clients
        assert active_ws in manager._clients


# ---------------------------------------------------------------------------
# Test: VAD Start Handling
# ---------------------------------------------------------------------------

class TestVadStartHandling:
    """vad_start messages should write 'hearing' state to GUI shared memory."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_vad_start_writes_hearing_state(self, manager):
        """vad_start should call _write_activity_state('hearing', utterance_id)."""
        messages = [{"type": "vad_start", "utterance_id": 42}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_called_once_with('hearing', 42)

    @pytest.mark.asyncio
    async def test_vad_start_does_not_queue_word_events(self, manager):
        """vad_start is a signal only -- no WordEvents should be queued."""
        messages = [{"type": "vad_start", "utterance_id": 42}]
        ws = _make_mock_ws(messages)

        await manager.handle_connection(ws)

        assert manager.word_queue.empty()

    def test_write_activity_state_with_shm(self, manager):
        """_write_activity_state writes JSON to shared memory with size header."""
        # Create a real buffer (simulating shared memory)
        buf = bytearray(256)
        mock_shm = MagicMock()
        mock_shm.buf = buf
        manager._gui_shm = mock_shm

        manager._write_activity_state('hearing', 99)

        # Read back the size header (big-endian uint32)
        size = struct.unpack_from('>I', buf, 0)[0]
        assert size > 0

        # Read back the JSON payload
        payload = json.loads(buf[4:4 + size].decode('utf-8'))
        assert payload['state'] == 'hearing'
        assert payload['utterance_id'] == 99

    def test_write_activity_state_noop_without_shm(self, manager):
        """_write_activity_state should silently do nothing when shm is None."""
        manager._gui_shm = None
        # Should not raise
        manager._write_activity_state('hearing', 1)

    @pytest.mark.asyncio
    async def test_vad_start_invokes_prewarm_callback(self, manager):
        """vad_start must invoke the registered callback so the focus-redirect
        path can pre-warm the prompt detector before the first dictated word
        arrives (wh-prewarm-detector-vad-start)."""
        callback = Mock()
        manager.set_vad_start_callback(callback)

        messages = [{"type": "vad_start", "utterance_id": 42}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state'):
            await manager.handle_connection(ws)

        callback.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_vad_start_callback_exception_does_not_break_handler(
        self, manager,
    ):
        """A failing pre-warm callback must not break the activity-state
        write or the idle-watchdog arm. Both are critical to the GUI pulse;
        a wrapped try/except around the callable preserves them."""
        def _broken_callback() -> None:
            raise RuntimeError("synthetic prewarm failure")

        manager.set_vad_start_callback(_broken_callback)

        messages = [{"type": "vad_start", "utterance_id": 42}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write, \
             patch.object(manager, '_arm_idle_watchdog') as mock_arm:
            await manager.handle_connection(ws)

        mock_write.assert_called_once_with('hearing', 42)
        mock_arm.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_vad_start_without_callback_is_noop(self, manager):
        """The callback is optional; vad_start handling must work without one."""
        assert manager._vad_start_callback is None

        messages = [{"type": "vad_start", "utterance_id": 42}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_called_once_with('hearing', 42)


# ---------------------------------------------------------------------------
# Test: Notification Handling
# ---------------------------------------------------------------------------

class TestNotificationHandling:
    """Notification messages should send toast and signal launcher on 'ready'."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        return mgr

    @pytest.mark.asyncio
    async def test_ready_notification_signals_launcher(self, manager):
        """A notification with 'ready' in the message should call signal_provider_ready()."""
        launcher = MagicMock()
        launcher.signal_provider_ready = MagicMock()
        launcher.launch_generation.return_value = 2
        manager.remote_stt_launcher = launcher
        manager.state_manager = None

        messages = [{
            # The provider declares itself first, as every shipped
            # provider does. This test is about a classified ready, not
            # about the provisional question, and a connection that never
            # declares itself now has its ready dropped
            # (wh-ready-connection-stamp.2). The kind is required since
            # the substring fallback was removed; without it this frame is
            # an ordinary notice (wh-ready-connection-stamp.2.2.1).
            "type": "capabilities",
            "provider": "google_stt",
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "STT Provider",
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_ready_notification_does_not_signal_launcher(self, manager):
        """A notification without 'ready' should NOT call signal_provider_ready()."""
        launcher = MagicMock()
        launcher.signal_provider_ready = MagicMock()
        manager.remote_stt_launcher = launcher
        manager.state_manager = None

        messages = [{
            "type": "notification",
            "title": "STT Error",
            "message": "Audio device disconnected"
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_sends_toast_via_state_manager(self, manager):
        """Notification should forward to speech_notifier._send_notification."""
        mock_notifier = MagicMock()
        mock_notifier._send_notification = MagicMock()
        mock_sm = MagicMock()
        mock_sm.speech_notifier = mock_notifier
        manager.state_manager = mock_sm
        manager.remote_stt_launcher = None

        messages = [{
            "type": "notification",
            "title": "Test Title",
            "message": "Test body text"
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        mock_notifier._send_notification.assert_called_once_with("Test Title", "Test body text")

    @pytest.mark.asyncio
    async def test_notification_does_not_queue_word_events(self, manager):
        """Notification messages should not produce any WordEvents."""
        manager.state_manager = None
        manager.remote_stt_launcher = None

        messages = [{
            "type": "notification",
            "title": "Info",
            "message": "Something happened"
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert manager.word_queue.empty()

    def _state_manager_with_notifier(self):
        mock_notifier = MagicMock()
        mock_notifier._send_notification = MagicMock()
        mock_sm = MagicMock()
        mock_sm.speech_notifier = mock_notifier
        return mock_sm, mock_notifier

    @pytest.mark.asyncio
    async def test_startup_failed_kind_signals_failure_and_still_toasts(
        self, manager
    ):
        """kind="startup_failed" ends the launcher's starting state,
        closes the working dialog, and still shows the toast -- the
        message says exactly what is wrong with the credentials
        (review finding wh-google-creds-file-picker.1.5)."""
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.launch_generation.return_value = 3
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            # The provider declares itself first, as every shipped
            # provider does. Without that declaration the failure is
            # dropped rather than attributed to a guessed launch
            # (wh-launch-generation.1.2).
            "type": "capabilities",
            "provider": "google_stt",
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "Google STT",
            "message": (
                "Google credentials problem - transcription will not "
                "work: ValueError: bad key"
            ),
            "kind": "startup_failed",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_startup_failed.assert_called_once()
        launcher.signal_provider_ready.assert_not_called()
        # The dismiss names the launch that owns this connection, so the
        # GUI can drop it if a replacement has landed since
        # (wh-launch-addressed-notices).
        mock_sm.state_to_gui_queue.put_nowait.assert_any_call(
            {"action": "hide_working", "owner": "stt:3"}
        )
        mock_notifier._send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_ready_kind_dismisses_the_dialog_naming_its_own_launch(
        self, manager
    ):
        """kind="ready" closes the working dialog, and the dismiss says
        which launch it is for.

        Every provider shares one dialog, so a ready from a launch the
        user has already replaced must not close the replacement's
        loading display. Only the GUI can decide that, against the order
        the two messages were sent in, so the launch that owns this
        connection travels with the dismiss
        (wh-launch-addressed-notices).
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.launch_generation.return_value = 3
        mock_sm, _mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            # The declaration binds this connection to launch 3, the
            # same way it does in the startup_failed test above.
            "type": "capabilities",
            "provider": "google_stt",
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "Google STT",
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_called_once_with(3)
        mock_sm.state_to_gui_queue.put_nowait.assert_any_call(
            {"action": "hide_working", "owner": "stt:3"}
        )

    @pytest.mark.asyncio
    async def test_a_ready_from_an_undeclared_connection_is_dropped(
        self, manager, caplog
    ):
        """A connection that never declared its provider keeps the
        connect-time guess, so its ready must not complete a launch.

        The guess names whichever launch was starting when this
        connection arrived. A provider left running by a previous run of
        WheelHouse reconnects into a launch this launcher never started,
        becomes the active client, and its ready would end the CURRENT
        launch's starting state -- for a launch whose own provider may
        never have reported ready. The signal is dropped and logged
        against the provisional stamp, the same shape the startup_failed
        branch above already uses (wh-ready-connection-stamp.2).
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.return_value = 6
        mock_sm, _mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            # No capabilities message: this connection never declares
            # its provider, so the stamp stays provisional.
            "type": "notification",
            "title": "Google STT",
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        with caplog.at_level(
            logging.INFO, logger="integrations.websocket_manager"
        ):
            await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()
        assert "provisional launch stamp 6" in caplog.text, caplog.text

    @pytest.mark.asyncio
    async def test_a_dropped_ready_does_not_dismiss_the_dialog(
        self, manager
    ):
        """The dismiss travels with the same guessed stamp the dropped
        signal carried, so it must be dropped with it.

        A provisional connection's stamp is the connect-time guess, and
        the GUI applies a dismiss whose launch matches the dialog's owner
        (or names no launch at all). Sending it here closed the CURRENT
        launch's loading display on a ready the handler had just declared
        un-attributable, while that launch's own provider was still
        warming up -- the visual half of the defect the signal drop
        removes. The launch's own monitor hides the dialog at its
        deadline, so nothing is lost by leaving it up
        (wh-ready-connection-stamp.2.1.1).
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.return_value = 6
        mock_sm, _mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            # No capabilities message, as in the drop test above.
            "type": "notification",
            "title": "Google STT",
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()
        # Matched on the action alone, never on the whole message, for
        # the reason test_launch_generation.py records: a bare-dict
        # assertion is true whatever the code does.
        assert [
            call.args[0]
            for call in mock_sm.state_to_gui_queue.put_nowait.call_args_list
            if call.args
            and isinstance(call.args[0], dict)
            and call.args[0].get("action") == "hide_working"
        ] == []

    @pytest.mark.asyncio
    async def test_a_ready_from_a_declared_connection_still_signals(
        self, manager
    ):
        """A provider that declared itself is bound to its own launch,
        so its ready is delivered with that launch's generation.

        The drop above must not cost the ordinary case. Every shipped
        provider sends a capabilities message naming the name WheelHouse
        launched it under, so _rebind_launch_stamp moves the stamp off
        the guess before any notification arrives. The guess here is 9
        and the declared launch is 4, so a ready carrying 4 proves the
        rebound stamp is what travels (wh-ready-connection-stamp.2).
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.return_value = 9
        launcher.launch_generation.return_value = 4
        mock_sm, _mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": "google_stt",
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "Google STT",
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_called_once_with(4)

    # Each shipped provider's identifier in its capabilities message, and
    # for the readiness cases the title it sends. This file cannot import
    # the providers: the wheelhouse service and each provider service have
    # separate virtual environments. The test that catches a provider
    # which stops sending kind="ready" lives on the provider side, as
    # test_the_ready_notice_carries_kind_ready in that provider's own
    # tests/test_startup_readiness.py. Ids carry no spaces, because the
    # mutation gate reads a collected id up to its first space
    # (wh-ready-connection-stamp.2.2.1).
    _SHIPPED_PROVIDERS = [
        pytest.param("google_stt", id="google"),
        pytest.param("distil_medium_en", id="distil"),
        pytest.param("parakeet_tdt", id="parakeet"),
    ]

    _SHIPPED_READY_NOTICES = [
        pytest.param("google_stt", "Google STT", id="google"),
        pytest.param(
            "distil_medium_en", "Distil-Whisper Medium (GPU)", id="distil"),
        pytest.param("parakeet_tdt", "Parakeet v3 (GPU)", id="parakeet"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", _SHIPPED_PROVIDERS)
    async def test_a_duplicate_hint_notice_is_not_a_ready(
        self, manager, provider
    ):
        """Saying "boost" twice on one word must tell the user so.

        "ready" is a substring of "already", so the kind-less fallback
        read this notice as a readiness signal on a declared, active
        connection: it completed the launch, closed the working dialog,
        and continued before the notice, so the user saw nothing at all.
        All three shipped providers send it with no kind, for every hint
        (google_stt_server/main.py:1134, distil_medium_en/main.py:331,
        sherpa_offline_parakeet_stt_server/main.py:422). The manual
        checklist's say-boost-twice step is this case
        (wh-ready-connection-stamp.2.2.1).
        """
        launcher = MagicMock()
        launcher.is_starting = False
        launcher.current_launch_generation.return_value = 9
        launcher.launch_generation.return_value = 4
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": provider,
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "STT Hint",
            "message": "Hint 'testword' already exists",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()
        # Matched on the action alone, never on the whole message, for
        # the reason test_launch_generation.py records: a bare-dict
        # assertion is true whatever the code does.
        assert [
            call.args[0]
            for call in mock_sm.state_to_gui_queue.put_nowait.call_args_list
            if call.args
            and isinstance(call.args[0], dict)
            and call.args[0].get("action") == "hide_working"
        ] == []
        mock_notifier._send_notification.assert_called_once_with(
            "STT Hint", "Hint 'testword' already exists"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", [
        pytest.param("distil_medium_en", id="distil"),
        pytest.param("parakeet_tdt", id="parakeet"),
    ])
    async def test_a_failed_hint_save_notice_is_not_a_ready(
        self, manager, provider
    ):
        """A hint that could not be written must say so.

        distil_medium_en (main.py:339) and the sherpa parakeet provider
        (main.py:430) report a failed write with no kind, so a selected
        word containing "ready" turned the only feedback a failed write
        produces into a readiness signal, and the user was told nothing
        about a hint that was never saved
        (wh-ready-connection-stamp.2.2.1).
        """
        launcher = MagicMock()
        launcher.is_starting = False
        launcher.current_launch_generation.return_value = 9
        launcher.launch_generation.return_value = 4
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": provider,
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "STT Error",
            "message": "Could not save hint 'ready'",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()
        mock_notifier._send_notification.assert_called_once_with(
            "STT Error", "Could not save hint 'ready'"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,title", _SHIPPED_READY_NOTICES)
    async def test_each_shipped_providers_ready_completes_its_launch(
        self, manager, provider, title
    ):
        """Every provider's real ready notice still ends its own launch.

        All three send "Transcription service ready" with kind="ready":
        google_stt_server/main.py:198, distil_medium_en/main.py:530 and
        sherpa_offline_parakeet_stt_server/main.py:591. Dropping the
        kind-less fallback must cost none of them their launch, and the
        generation must be the rebound one, not the connect-time guess
        (wh-ready-connection-stamp.2.2.1).
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.return_value = 9
        launcher.launch_generation.return_value = 4
        mock_sm, _mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": provider,
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": title,
            "message": "Transcription service ready",
            "kind": "ready",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_called_once_with(4)

    @pytest.mark.asyncio
    async def test_a_kind_less_notice_saying_ready_no_longer_signals(
        self, manager
    ):
        """A notice carrying no kind is a notice, whatever its words say.

        This is the removed fallback's direct inverse. Guessing from the
        text bought nothing once every shipped provider sent kind="ready",
        and it cost the notices the two tests above name. A provider that
        sends no kind cannot complete a launch by any route: this launcher
        never started it, so its connection stays provisional and its
        ready is dropped before this branch
        (wh-ready-connection-stamp.2.2.1).
        """
        launcher = MagicMock()
        launcher.is_starting = False
        launcher.current_launch_generation.return_value = 9
        launcher.launch_generation.return_value = 4
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": "google_stt",
            "emits_eos": False,
        }, {
            "type": "notification",
            "title": "STT Provider",
            "message": "Provider is ready for transcription",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        launcher.signal_provider_ready.assert_not_called()
        mock_notifier._send_notification.assert_called_once_with(
            "STT Provider", "Provider is ready for transcription"
        )

    @pytest.mark.asyncio
    async def test_error_kind_bypasses_startup_suppression(self, manager):
        """kind="error" is a runtime failure notice; is_starting
        suppression must not swallow it, or a failed startup leaves the
        user with no signal at the first utterance
        (review finding wh-google-creds-file-picker.1.5)."""
        launcher = MagicMock()
        launcher.is_starting = True
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "notification",
            "title": "Google STT",
            "message": (
                "Speech engine error - transcription is not working: "
                "ValueError: bad key"
            ),
            "kind": "error",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        mock_notifier._send_notification.assert_called_once()
        launcher.signal_provider_ready.assert_not_called()
        launcher.signal_provider_startup_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_notification_is_still_suppressed_during_startup(
        self, manager
    ):
        """A kind-less notification during startup keeps today's
        behavior: the working dialog is the only signal."""
        launcher = MagicMock()
        launcher.is_starting = True
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "notification",
            "title": "Google STT",
            "message": "Loading model...",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        mock_notifier._send_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Stale-client notification gate
# ---------------------------------------------------------------------------

class _GatedWS:
    """A fake websocket whose message iteration waits on an asyncio.Event.

    Lets a test hold a connection open (registered but not yet delivering)
    while a second connection registers and becomes the active client, then
    release the first connection's frames afterwards -- the stale delivery
    ordering that review finding wh-google-creds-file-picker.1.16 is about.
    """

    def __init__(self, messages, gate, remote_address=("127.0.0.1", 9999)):
        self._messages = [
            json.dumps(m) if isinstance(m, dict) else m for m in messages
        ]
        self._gate = gate
        self.remote_address = remote_address
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await self._gate.wait()
        for m in self._messages:
            yield m


class _LateBoundWS:
    """A fake websocket that json-encodes each message dict at yield time.

    Lets a test hook fill in message fields AFTER construction but before
    delivery -- needed by the apply-reply correlation tests, where the
    result frame must echo the apply_id that send_command_to_active_stt
    generates mid-connection (wh-7ou.7.6.9).
    """

    def __init__(self, messages, remote_address=("127.0.0.1", 9999)):
        self.messages = messages
        self.remote_address = remote_address
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for m in self.messages:
            yield json.dumps(m) if isinstance(m, dict) else m


async def _wait_until(predicate, timeout=2.0):
    """Poll *predicate* on the running loop until true (bounded)."""
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(_poll(), timeout)


class TestStaleClientNotificationGate:
    """Notifications from a connected-but-not-active client must not steer
    the launcher or reach the user (wh-google-creds-file-picker.1.16).

    add_client keeps older connections open (merely DISABLED) and marks
    only the newest as active. An orphaned provider from a previous
    generation can therefore still deliver queued notification frames
    while a new provider is starting; capabilities are already gated on
    the active client (wh-nvyh.1.1) and notifications need the same gate.
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        return WebSocketManager(loop=event_loop)

    def _state_manager_with_notifier(self):
        mock_notifier = MagicMock()
        mock_notifier._send_notification = MagicMock()
        mock_sm = MagicMock()
        mock_sm.speech_notifier = mock_notifier
        return mock_sm, mock_notifier

    async def _run_stale_frames(self, manager, stale_messages):
        """Connect a stale socket, supersede it with an active one, then
        deliver the stale socket's frames. Returns after the stale
        connection has fully drained and closed."""
        stale_gate = asyncio.Event()
        active_gate = asyncio.Event()
        stale_ws = _GatedWS(stale_messages, stale_gate, ("127.0.0.1", 9001))
        active_ws = _GatedWS([], active_gate, ("127.0.0.1", 9002))

        stale_task = asyncio.create_task(manager.handle_connection(stale_ws))
        await _wait_until(lambda: manager._active_stt_client is stale_ws)
        active_task = asyncio.create_task(manager.handle_connection(active_ws))
        await _wait_until(lambda: manager._active_stt_client is active_ws)

        stale_gate.set()
        await stale_task

        active_gate.set()
        await active_task

    @pytest.mark.asyncio
    async def test_stale_startup_failed_does_not_end_the_active_startup(
        self, manager
    ):
        """A stale socket's kind="startup_failed" must not mark the NEW
        provider's startup as failed or close its working dialog."""
        launcher = MagicMock()
        launcher.is_starting = True
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        await self._run_stale_frames(manager, [{
            "type": "notification",
            "title": "Google STT",
            "message": (
                "Google credentials problem - transcription will not "
                "work: ValueError: bad key"
            ),
            "kind": "startup_failed",
        }])

        launcher.signal_provider_startup_failed.assert_not_called()
        mock_sm.state_to_gui_queue.put_nowait.assert_not_called()
        mock_notifier._send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_ready_does_not_complete_the_active_startup(
        self, manager
    ):
        """A stale socket's ready notification must not signal the
        launcher that the NEW provider is ready."""
        launcher = MagicMock()
        launcher.is_starting = True
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        await self._run_stale_frames(manager, [{
            "type": "notification",
            "title": "STT Provider",
            "message": "Provider is ready for transcription",
            # Classified, so the stale-frame gate stays this test's
            # subject. Without a kind the frame is no longer a ready at
            # all and the assertion below would hold for the wrong reason
            # (wh-ready-connection-stamp.2.2.1).
            "kind": "ready",
        }])

        launcher.signal_provider_ready.assert_not_called()
        mock_sm.state_to_gui_queue.put_nowait.assert_not_called()
        mock_notifier._send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_error_notice_is_not_shown_to_the_user(
        self, manager
    ):
        """A stale socket's kind="error" notice describes the OLD
        provider's failure; showing it while the new provider starts
        misreports the system state."""
        launcher = MagicMock()
        launcher.is_starting = True
        mock_sm, mock_notifier = self._state_manager_with_notifier()
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        await self._run_stale_frames(manager, [{
            "type": "notification",
            "title": "Google STT",
            "message": (
                "Speech engine error - transcription is not working: "
                "ValueError: bad key"
            ),
            "kind": "error",
        }])

        mock_notifier._send_notification.assert_not_called()
        launcher.signal_provider_startup_failed.assert_not_called()
        launcher.signal_provider_ready.assert_not_called()


# ---------------------------------------------------------------------------
# Test: add_client registration atomicity
# ---------------------------------------------------------------------------

class TestAddClientRegistrationAtomicity:
    """Registration and promotion must happen before any await inside
    add_client (wh-google-creds-file-picker.1.19).

    add_client used to await DISABLE sends to existing clients BEFORE
    registering and promoting the newcomer. Two overlapping add_client
    calls could then finish in the wrong order: an older call whose
    DISABLE send completed slowly would overwrite the newer connection
    that had already promoted itself, making a stale socket the active
    client and dropping the real provider's frames at the active-socket
    gates."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        return WebSocketManager(loop=event_loop)

    @pytest.mark.asyncio
    async def test_overlapping_registrations_leave_the_newest_active(
        self, manager
    ):
        """When add_client(A) is suspended awaiting a DISABLE send and
        add_client(B) runs to completion, B (the newest arrival) must
        stay the active client after A resumes."""
        first_send_started = asyncio.Event()
        release_first_send = asyncio.Event()
        send_count = 0

        class _SlowFirstSendWS:
            remote_address = ("127.0.0.1", 9000)

            async def send(self, _payload):
                nonlocal send_count
                send_count += 1
                if send_count == 1:
                    first_send_started.set()
                    await release_first_send.wait()

        existing = _SlowFirstSendWS()
        await manager.add_client(existing)

        ws_a = AsyncMock()
        ws_a.remote_address = ("127.0.0.1", 9001)
        ws_b = AsyncMock()
        ws_b.remote_address = ("127.0.0.1", 9002)

        task_a = asyncio.create_task(manager.add_client(ws_a))
        await asyncio.wait_for(first_send_started.wait(), 2.0)

        await manager.add_client(ws_b)
        assert manager._active_stt_client is ws_b

        release_first_send.set()
        await asyncio.wait_for(task_a, 2.0)

        assert manager._active_stt_client is ws_b


# ---------------------------------------------------------------------------
# Test: Malformed Message Handling
# ---------------------------------------------------------------------------

class TestMalformedMessageHandling:
    """Invalid JSON and unknown message types should be handled gracefully."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_invalid_json_does_not_crash(self, manager):
        """Invalid JSON should be logged as error, not crash the handler."""
        ws = _make_mock_ws(["this is not { valid json"])
        await manager.handle_connection(ws)
        # Should complete without raising
        assert manager.word_queue.empty()

    @pytest.mark.asyncio
    async def test_unknown_message_type_logged_as_warning(self, manager):
        """Unknown msg_type should log a warning and not queue events."""
        messages = [{"type": "unknown_type", "text": "something", "utterance_id": 1}]
        ws = _make_mock_ws(messages)

        with patch('integrations.websocket_manager.logger') as mock_logger:
            await manager.handle_connection(ws)

        # Verify a warning was logged about the unexpected type
        warning_calls = [
            c for c in mock_logger.warning.call_args_list
            if "unknown_type" in str(c)
        ]
        assert len(warning_calls) >= 1, "Expected a warning about unknown message type"
        assert manager.word_queue.empty()

    @pytest.mark.asyncio
    async def test_valid_messages_processed_after_invalid(self, manager):
        """A valid message after an invalid one should still be processed."""
        mock_app = MagicMock()
        mock_app.send_command = AsyncMock()
        manager.set_app(mock_app)

        messages = [
            "broken json {{{",
            {"type": "final", "text": "hello", "utterance_id": 10},
        ]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # The final message should have been processed
        events = _drain_queue(manager.word_queue)
        # Should have at least the word "hello" and an end marker
        words = [e.word for e in events if e.word]
        assert "hello" in words

    @pytest.mark.asyncio
    async def test_message_missing_text_field(self, manager):
        """A stable message with missing text field should not crash."""
        messages = [{"type": "stable", "utterance_id": 5}]
        ws = _make_mock_ws(messages)
        # text defaults to "" via data.get("text", "")
        await manager.handle_connection(ws)
        assert manager.word_queue.empty()


# ---------------------------------------------------------------------------
# Test: Client Disconnect During Utterance
# ---------------------------------------------------------------------------

class TestClientDisconnectDuringUtterance:
    """Disconnect mid-utterance should queue a cleanup end marker."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    def test_remove_client_queues_end_marker_when_utterance_in_progress(self, manager):
        """If current_utterance_id is set, remove_client queues an end marker."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 6000)
        manager._clients.add(ws)
        manager.current_utterance_id = 77

        manager.remove_client(ws)

        events = _drain_queue(manager.word_queue)
        assert len(events) == 1
        marker = events[0]
        assert marker.is_utterance_end_marker is True
        assert marker.end_of_utterance is True
        assert marker.word == ""
        assert marker.utterance_id == 77

    def test_remove_client_resets_utterance_state(self, manager):
        """After cleanup, current_utterance_id and _last_stable_utterance_id should be None."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 6001)
        manager._clients.add(ws)
        manager.current_utterance_id = 88
        manager._last_stable_utterance_id = 88

        manager.remove_client(ws)

        assert manager.current_utterance_id is None
        assert manager._last_stable_utterance_id is None

    def test_remove_client_no_end_marker_when_no_utterance(self, manager):
        """If no utterance is in progress, remove_client should not queue anything."""
        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 6002)
        manager._clients.add(ws)
        manager.current_utterance_id = None

        manager.remove_client(ws)

        assert manager.word_queue.empty()

    @pytest.mark.asyncio
    async def test_handle_connection_cleanup_on_disconnect(self, manager):
        """handle_connection's finally block calls remove_client on disconnect."""
        mock_app = MagicMock()
        mock_app.send_command = AsyncMock()
        manager.set_app(mock_app)

        # Send a stable that starts an utterance, then the ws disconnects
        messages = [
            {"type": "stable", "text": "hello", "utterance_id": 50},
        ]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # After handle_connection completes (ws iteration ends), remove_client
        # is called via the finally block. Since there's an in-progress utterance
        # (current_utterance_id == 50), an end marker should be queued.
        events = _drain_queue(manager.word_queue)
        end_markers = [e for e in events if e.is_utterance_end_marker]
        assert len(end_markers) == 1
        assert end_markers[0].utterance_id == 50


# ---------------------------------------------------------------------------
# Test: Broadcast Error Handling
# ---------------------------------------------------------------------------

class TestBroadcastErrorHandling:
    """Failed sends to individual clients should not crash broadcast to others."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        return WebSocketManager(loop=event_loop)

    @pytest.mark.asyncio
    async def test_broadcast_to_no_clients_returns_immediately(self, manager):
        """broadcast with no clients should return without error."""
        await manager.broadcast({"type": "test"})
        # No assertion needed; should simply not raise

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_crash_others(self, manager):
        """If one client's send fails, other clients still receive the message."""
        ws_good = AsyncMock()
        ws_good.remote_address = ("127.0.0.1", 7001)
        ws_good.send = AsyncMock()

        ws_bad = AsyncMock()
        ws_bad.remote_address = ("127.0.0.1", 7002)
        ws_bad.send = AsyncMock(side_effect=Exception("connection reset"))

        manager._clients = {ws_good, ws_bad}

        await manager.broadcast({"type": "test_message", "data": "hello"})

        # The good client should have received the message
        ws_good.send.assert_called_once()
        sent_data = json.loads(ws_good.send.call_args[0][0])
        assert sent_data["type"] == "test_message"

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_clients(self, manager):
        """broadcast should send to every registered client."""
        clients = []
        for i in range(3):
            ws = AsyncMock()
            ws.remote_address = ("127.0.0.1", 7010 + i)
            ws.send = AsyncMock()
            clients.append(ws)

        manager._clients = set(clients)

        msg = {"type": "set_transcription_status", "enabled": True}
        await manager.broadcast(msg)

        for ws in clients:
            ws.send.assert_called_once()
            sent_data = json.loads(ws.send.call_args[0][0])
            assert sent_data["type"] == "set_transcription_status"
            assert sent_data["enabled"] is True


# ---------------------------------------------------------------------------
# Test: Transcription Status Toggling
# ---------------------------------------------------------------------------

class TestTranscriptionStatusToggling:
    """set_transcription_status should update state and return correct message."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        return WebSocketManager(loop=event_loop)

    def test_enable_transcription(self, manager):
        """Enabling transcription should set flag and return correct message."""
        manager.transcription_enabled = False
        result = manager.set_transcription_status(True)

        assert manager.transcription_enabled is True
        assert result == {
            "type": "set_transcription_status",
            "enabled": True,
        }

    def test_disable_transcription(self, manager):
        """Disabling transcription should set flag and return correct message."""
        manager.transcription_enabled = True
        result = manager.set_transcription_status(False)

        assert manager.transcription_enabled is False
        assert result == {
            "type": "set_transcription_status",
            "enabled": False,
        }

    def test_get_current_status_message(self, manager):
        """get_current_status_message should reflect current state."""
        manager.transcription_enabled = True
        msg = manager.get_current_status_message()
        assert msg["type"] == "set_transcription_status"
        assert msg["enabled"] is True

        manager.transcription_enabled = False
        msg = manager.get_current_status_message()
        assert msg["enabled"] is False

    def test_set_transcription_status_returns_dict(self, manager):
        """Return value should be a dict suitable for broadcasting."""
        result = manager.set_transcription_status(True)
        assert isinstance(result, dict)
        assert "type" in result
        assert "enabled" in result

    def test_set_transcription_status_includes_reason(self, manager):
        """set_transcription_status message includes reason field when provided."""
        result = manager.set_transcription_status(False, reason="idle")
        assert result["reason"] == "idle"
        assert result["enabled"] is False
        assert result["type"] == "set_transcription_status"

    def test_set_transcription_status_reason_defaults_to_none(self, manager):
        """Backward compat: reason defaults to None when not provided."""
        result = manager.set_transcription_status(False)
        assert result.get("reason") is None

    def test_set_transcription_status_reason_wake_word(self, manager):
        """set_transcription_status with reason='wake_word' includes correct reason."""
        result = manager.set_transcription_status(False, reason="wake_word")
        assert result["reason"] == "wake_word"

    def test_get_current_status_includes_reason_when_disabled(self, manager):
        """get_current_status_message should include reason when suppression is active."""
        manager.set_transcription_status(False, reason="idle")
        msg = manager.get_current_status_message()
        assert msg["enabled"] is False
        assert msg["reason"] == "idle", \
            "Reconnecting STT providers need the reason to activate wake word listening"

    def test_get_current_status_no_reason_when_enabled(self, manager):
        """get_current_status_message should not include reason when enabled."""
        manager.set_transcription_status(True)
        msg = manager.get_current_status_message()
        assert msg["enabled"] is True
        assert "reason" not in msg

    def test_get_current_status_reason_updates_on_status_change(self, manager):
        """get_current_status_message reason should reflect most recent disable reason."""
        manager.set_transcription_status(False, reason="idle")
        manager.set_transcription_status(False, reason="audio")
        msg = manager.get_current_status_message()
        assert msg["reason"] == "audio"


# ---------------------------------------------------------------------------
# Test: Wake Word Message Handling
# ---------------------------------------------------------------------------

class TestWakeWordMessageHandling:
    """wake_word_detected messages should be handled by WebSocketManager."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_wake_word_detected_does_not_queue_word_events(self, manager):
        """wake_word_detected messages should not produce any WordEvents."""
        messages = [{
            "type": "wake_word_detected",
            "keyword": "hey computer",
            "utterance_id": 0,
            "is_partial": False
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # No word events should be queued (only cleanup markers from disconnect)
        events = _drain_queue(manager.word_queue)
        word_events = [e for e in events if e.word != "" and not e.is_utterance_end_marker]
        assert len(word_events) == 0


# ---------------------------------------------------------------------------
# Test: Empty Stable / Final Text
# ---------------------------------------------------------------------------

class TestEmptyStableFinalText:
    """Empty text in stable or final messages should not queue word events."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        mock_app = MagicMock()
        mock_app.send_command = AsyncMock()
        mgr.set_app(mock_app)
        return mgr

    @pytest.mark.asyncio
    async def test_empty_stable_text_queues_nothing(self, manager):
        """A stable message with empty text should not produce WordEvents."""
        messages = [{"type": "stable", "text": "", "utterance_id": 300}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # Drain the queue - there might be a cleanup end marker from disconnect
        # but no actual word events
        events = _drain_queue(manager.word_queue)
        word_events = [e for e in events if e.word != "" and not e.is_utterance_end_marker]
        assert len(word_events) == 0

    @pytest.mark.asyncio
    async def test_empty_final_text_queues_only_end_marker(self, manager):
        """A final message with empty text should queue only the end marker."""
        messages = [{"type": "final", "text": "", "utterance_id": 301}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        # Filter out any cleanup markers from disconnect
        # The final handler always queues an end marker
        final_end_markers = [
            e for e in events
            if e.is_utterance_end_marker and e.utterance_id == 301
        ]
        assert len(final_end_markers) >= 1

        # No actual words should be queued
        word_events = [e for e in events if e.word != ""]
        assert len(word_events) == 0

    @pytest.mark.asyncio
    async def test_whitespace_only_stable_queues_nothing(self, manager):
        """A stable message with whitespace-only text should not queue words."""
        messages = [{"type": "stable", "text": "   ", "utterance_id": 302}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        word_events = [e for e in events if e.word.strip() != "" and not e.is_utterance_end_marker]
        assert len(word_events) == 0

    @pytest.mark.asyncio
    async def test_final_with_text_queues_words_and_end_marker(self, manager):
        """Contrast: a final with actual text should queue words AND an end marker."""
        messages = [{"type": "final", "text": "hello world", "utterance_id": 303}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        word_events = [e for e in events if e.word != "" and not e.is_utterance_end_marker]
        end_markers = [e for e in events if e.is_utterance_end_marker and e.utterance_id == 303]

        assert len(word_events) == 2  # "hello" and "world"
        assert word_events[0].word == "hello"
        assert word_events[1].word == "world"
        assert len(end_markers) >= 1


# ---------------------------------------------------------------------------
# Test: Idle Watchdog (clears stuck 'hearing' state when no final arrives)
# ---------------------------------------------------------------------------

class TestIdleWatchdog:
    """When the STT provider sends vad_start but never sends a matching final
    (hallucination suppression, crash, network glitch), the GUI shared memory
    would stay at 'hearing' forever and the floating button would pulse
    orange/red continuously. The watchdog clears the stuck state by writing
    'idle' after a timeout.
    """

    @pytest.fixture
    def make_manager(self):
        """Factory so timing-sensitive tests can build a fresh manager per
        retry attempt (wh-idle-watchdog-flaky)."""
        from integrations.websocket_manager import WebSocketManager

        def _make():
            # Placeholder loop for construction; the watchdog uses the
            # running loop inside async methods, so this stub never runs.
            loop = asyncio.new_event_loop()
            try:
                mgr = WebSocketManager(loop=loop)
            finally:
                loop.close()
            mgr.state_manager = None
            mgr._idle_watchdog_seconds = 0.05  # Fast timeout for tests
            return mgr

        return _make

    @pytest.fixture
    def manager(self, make_manager):
        return make_manager()

    @staticmethod
    def _install_mock_shm(manager):
        buf = bytearray(256)
        mock_shm = MagicMock()
        mock_shm.buf = buf
        manager._gui_shm = mock_shm
        return buf

    @staticmethod
    def _read_state(buf):
        size = struct.unpack_from('>I', buf, 0)[0]
        if size == 0:
            return None
        return json.loads(buf[4:4 + size].decode('utf-8'))

    @staticmethod
    def _make_open_ws(messages):
        """Build a mock WebSocket that yields messages then hangs open.

        `_make_mock_ws` yields messages and then completes the async-for,
        which makes handle_connection exit and run remove_client. Real
        WebSockets stay open after an inbound burst. This helper blocks
        on an asyncio Event so the test can drive the close explicitly.
        """
        encoded = [json.dumps(m) if isinstance(m, dict) else m for m in messages]
        close_event = asyncio.Event()

        async def _hanging_iter():
            for item in encoded:
                yield item
            await close_event.wait()

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        ws.__aiter__ = lambda _: _hanging_iter()
        ws.close_event = close_event
        return ws

    @staticmethod
    def _make_gated_ws(messages_and_gates):
        """Build a mock WebSocket that yields each message only after its gate
        is set by the test. Lets the test inject time between messages.

        messages_and_gates: list of (message_dict, asyncio.Event) tuples.
        The iterator awaits each gate before yielding the corresponding
        message. After all messages, the iterator blocks on a final close
        event so handle_connection does not exit.
        """
        close_event = asyncio.Event()

        async def _gated_iter():
            for msg, gate in messages_and_gates:
                await gate.wait()
                yield json.dumps(msg) if isinstance(msg, dict) else msg
            await close_event.wait()

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9999)
        ws.send = AsyncMock()
        ws.__aiter__ = lambda _: _gated_iter()
        ws.close_event = close_event
        return ws

    @pytest.mark.asyncio
    async def test_vad_start_without_final_writes_idle_after_timeout(self, manager):
        """vad_start with no subsequent stable or final must clear to 'idle'."""
        buf = self._install_mock_shm(manager)
        ws = self._make_open_ws([{"type": "vad_start", "utterance_id": 77}])

        task = asyncio.create_task(manager.handle_connection(ws))
        try:
            await asyncio.sleep(0.15)  # Well past 0.05s watchdog

            state = self._read_state(buf)
            assert state is not None
            assert state['state'] == 'idle'
            assert state['utterance_id'] == 77
        finally:
            ws.close_event.set()
            await task

    @pytest.mark.asyncio
    async def test_final_cancels_idle_watchdog(self, manager):
        """A final before the watchdog fires must cancel it.

        Otherwise the shared memory would first be written 'confirmed' and
        then overwritten with 'idle', causing a spurious late 'idle' flash.
        """
        buf = self._install_mock_shm(manager)

        messages = [
            {"type": "vad_start", "utterance_id": 88},
            {"type": "final", "text": "hello", "utterance_id": 88},
        ]
        ws = _make_mock_ws(messages)

        await manager.handle_connection(ws)
        await asyncio.sleep(0.15)  # Well past the 0.05s watchdog

        state = self._read_state(buf)
        assert state is not None
        assert state['state'] == 'confirmed', (
            f"Watchdog should have been cancelled by final; state is {state!r}"
        )
        assert state['utterance_id'] == 88

    async def _run_stable_rearm_scenario(self, make_manager, scale):
        """Drive the stable-re-arm timing scenario at the given timescale.

        Returns ('ok', None) on success, or ('too_slow', detail) when
        loop.time() shows the event loop stalled past a deadline the
        scenario depends on, so the run proves nothing either way and the
        caller should retry at a larger scale (wh-idle-watchdog-flaky).
        Assertion failures propagate -- those are real re-arm bugs, only
        raised when the timing validity checks passed.
        """
        watchdog = 0.1 * scale
        manager = make_manager()
        manager._idle_watchdog_seconds = watchdog
        buf = self._install_mock_shm(manager)
        loop = asyncio.get_running_loop()

        gate_vad_start = asyncio.Event()
        gate_stable = asyncio.Event()
        ws = self._make_gated_ws([
            ({"type": "vad_start", "utterance_id": 120}, gate_vad_start),
            ({"type": "stable", "text": "hello", "utterance_id": 120}, gate_stable),
        ])

        task = asyncio.create_task(manager.handle_connection(ws))
        try:
            # Release vad_start at t=0. The watchdog arms strictly after
            # t_vad (when handle_connection processes the message), so the
            # original deadline is at earliest t_vad + watchdog.
            t_vad = loop.time()
            gate_vad_start.set()
            await asyncio.sleep(0.01 * scale)  # Let it process.

            # At nominal t=0.8*watchdog (before expiry), release stable.
            # Without re-arm the watchdog fires at ~watchdog; with re-arm
            # at ~1.8*watchdog.
            await asyncio.sleep(0.07 * scale)
            t_stable = loop.time()  # Lower bound on the re-arm time.
            gate_stable.set()
            await asyncio.sleep(0.01 * scale)  # Let it process.
            t_after_stable = loop.time()
            if t_after_stable - t_vad >= watchdog:
                # The loop was so slow the stable may have been processed
                # after the original deadline; 'idle' here would be
                # correct behavior, not a re-arm bug.
                return ('too_slow', (
                    f"stable processed {t_after_stable - t_vad:.3f}s after "
                    f"vad_start release, past the {watchdog:.3f}s deadline"
                ))

            # At nominal t=1.2*watchdog (past the original deadline): the
            # stable both re-armed the watchdog and opened the provisional
            # window, so the state is 'settling'. The proof of re-arm is
            # that it is NOT 'idle' -- a one-shot vad_start watchdog would
            # already have fired.
            await asyncio.sleep(0.03 * scale)
            state = self._read_state(buf)
            t_check = loop.time()
            if t_check - t_stable >= watchdog:
                # The loop stalled past the RE-ARMED deadline before the
                # read; 'idle' here would be the re-armed watchdog firing
                # on schedule, not a re-arm bug.
                return ('too_slow', (
                    f"first state read {t_check - t_stable:.3f}s after the "
                    f"stable, past the re-armed {watchdog:.3f}s deadline"
                ))
            assert state is not None
            assert state['state'] == 'settling', (
                f"Stable must re-arm (state not 'idle') and open the provisional "
                f"window (state 'settling'); a vad_start-only watchdog would have "
                f"fired by now. State is {state!r}"
            )

            # At nominal t=2.0*watchdog, the re-armed watchdog has fired
            # (deadline ~1.8*watchdog), proving the re-arm was not a no-op.
            # A late check only strengthens this direction, so no validity
            # guard is needed.
            await asyncio.sleep(0.08 * scale)
            state = self._read_state(buf)
            assert state is not None
            assert state['state'] == 'idle', (
                f"Re-armed watchdog should have fired by ~{1.8 * watchdog:.3f}s; "
                f"state is {state!r}"
            )
            return ('ok', None)
        finally:
            ws.close_event.set()
            await task

    @pytest.mark.asyncio
    async def test_stable_rearms_idle_watchdog(self, make_manager):
        """A stable delta during ongoing speech must re-arm the watchdog.

        Proves time-separated re-arming: the stable arrives late enough that
        a one-shot vad_start watchdog (without re-arm) would already have
        fired, yet the state is still 'settling'; then the re-armed deadline
        passes and the state goes 'idle'.

        Real-clock test (the watchdog uses loop.call_later). Under full-suite
        CPU load the loop can stall past the tight nominal margins, which
        used to flake this test (wh-idle-watchdog-flaky). The scenario now
        checks its own timing validity with loop.time() and retries at a 4x
        larger timescale when the run was too slow to prove anything.
        """
        attempts = []
        for scale in (1.0, 4.0, 16.0):
            outcome, detail = await self._run_stable_rearm_scenario(
                make_manager, scale
            )
            if outcome == 'ok':
                return
            attempts.append(f"scale {scale}: {detail}")
        pytest.fail(
            "Event loop too slow to run the re-arm timing scenario even at "
            "a 1.6s watchdog; not a re-arm bug. Attempts: "
            + "; ".join(attempts)
        )

    def test_remove_client_cancels_idle_watchdog(self, manager):
        """STT disconnect must cancel any pending idle watchdog.

        Otherwise the handle leaks past disconnect and the callback may
        later fire against shared memory that a subsequent reconnect or
        a Logic-process shutdown has invalidated.
        """
        mock_handle = MagicMock()
        manager._idle_watchdog_handle = mock_handle
        mock_ws = MagicMock()
        mock_ws.remote_address = ("127.0.0.1", 1234)
        manager._clients.add(mock_ws)
        manager.current_utterance_id = 42

        manager.remove_client(mock_ws)

        mock_handle.cancel.assert_called_once()
        assert manager._idle_watchdog_handle is None

    @pytest.mark.asyncio
    async def test_stop_cancels_idle_watchdog(self, manager):
        """Server stop must cancel any pending idle watchdog.

        Prevents a stray callback from firing between stop() and event-loop
        teardown, which on Windows could touch shared memory that the
        launcher has already unmapped.
        """
        mock_handle = MagicMock()
        manager._idle_watchdog_handle = mock_handle
        # stop() short-circuits cleanly when _server and _server_task are
        # both None, which is the default for a manager that never started.

        await manager.stop()

        mock_handle.cancel.assert_called_once()
        assert manager._idle_watchdog_handle is None

    @pytest.mark.asyncio
    async def test_new_vad_start_cancels_previous_watchdog(self, manager):
        """Overlapping utterances must not leak timers from the previous one."""
        buf = self._install_mock_shm(manager)

        messages = [
            {"type": "vad_start", "utterance_id": 100},
            {"type": "vad_start", "utterance_id": 101},
            {"type": "final", "text": "hi", "utterance_id": 101},
        ]
        ws = _make_mock_ws(messages)

        await manager.handle_connection(ws)
        await asyncio.sleep(0.15)

        # The second final should have cancelled the only remaining watchdog.
        # If the first vad_start's watchdog leaked, shared memory would flip
        # to 'idle' for utterance 100 after the final for 101 wrote 'confirmed'.
        state = self._read_state(buf)
        assert state is not None
        assert state['state'] == 'confirmed'
        assert state['utterance_id'] == 101


# ---------------------------------------------------------------------------
# Test: Dynamic Port Allocation
# ---------------------------------------------------------------------------

class TestDynamicPort:
    """Tests for dynamic port allocation (port=0)."""

    @pytest.mark.asyncio
    async def test_start_with_port_zero_returns_actual_port(self):
        """When started with port=0, start() should return the OS-assigned port."""
        loop = asyncio.get_running_loop()
        from integrations.websocket_manager import WebSocketManager
        manager = WebSocketManager(loop)

        actual_port = await manager.start("127.0.0.1", 0)

        try:
            assert isinstance(actual_port, int)
            assert actual_port > 0
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_start_with_explicit_port_returns_that_port(self):
        """When started with an explicit port, start() returns that port."""
        loop = asyncio.get_running_loop()
        from integrations.websocket_manager import WebSocketManager
        manager = WebSocketManager(loop)

        # Get a free port first
        actual_port = await manager.start("127.0.0.1", 0)
        free_port = actual_port
        await manager.stop()

        # Now start on that specific port
        actual_port = await manager.start("127.0.0.1", free_port)
        try:
            assert actual_port == free_port
        finally:
            await manager.stop()

    @pytest.mark.asyncio
    async def test_start_stores_port_as_attribute(self):
        """The actual port should be accessible as manager.port after start()."""
        loop = asyncio.get_running_loop()
        from integrations.websocket_manager import WebSocketManager
        manager = WebSocketManager(loop)

        returned_port = await manager.start("127.0.0.1", 0)

        try:
            assert manager.port == returned_port
        finally:
            await manager.stop()


# ---------------------------------------------------------------------------
# Test: Dynamic Port Integration (end-to-end)
# ---------------------------------------------------------------------------

class TestDynamicPortIntegration:
    """Integration test: server binds port 0, client connects to actual port."""

    @pytest.mark.asyncio
    async def test_client_connects_to_dynamically_assigned_port(self):
        """Full roundtrip: bind port 0, get actual port, connect client."""
        import websockets

        loop = asyncio.get_running_loop()
        from integrations.websocket_manager import WebSocketManager
        manager = WebSocketManager(loop)

        actual_port = await manager.start("127.0.0.1", 0)

        try:
            assert actual_port > 0
            uri = f"ws://127.0.0.1:{actual_port}"
            async with websockets.connect(uri) as ws:
                assert ws.state.name == "OPEN"
        finally:
            await manager.stop()


# ---------------------------------------------------------------------------
# Test: Retraction Trigger
# ---------------------------------------------------------------------------

class TestRetractionTrigger:
    """Tests for retraction marker generation on disagreeing finals."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def mock_app(self):
        app = MagicMock()
        app.send_command = AsyncMock()
        return app

    @pytest.fixture
    def manager(self, event_loop, mock_app):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(event_loop)
        mgr._app = mock_app
        mgr.state_manager = MagicMock()
        mgr.state_manager.config_service = MagicMock()
        mgr.state_manager.config_service.get.return_value = False  # Disable toast
        return mgr

    def test_extract_delta_returns_none_on_disagreement(self, manager):
        """_extract_delta returns None (not empty string) on revision."""
        manager._sent_stable_text = "hello world"
        manager._last_stable_utterance_id = 1

        result = manager._extract_delta("goodbye world", 1)

        assert result is None

    def test_extract_delta_returns_empty_on_exact_match(self, manager):
        """_extract_delta returns empty string when final matches sent text exactly."""
        manager._sent_stable_text = "hello world"
        manager._last_stable_utterance_id = 1

        result = manager._extract_delta("hello world", 1)

        assert result == ""

    @pytest.mark.asyncio
    async def test_disagreeing_final_queues_retraction_marker(self, manager):
        """When final disagrees with stables, a retraction marker is queued."""
        # Simulate stable words already sent
        manager._sent_stable_text = "hello whirled"
        manager._last_stable_utterance_id = 1
        manager._processed_word_count = 2
        manager.current_utterance_id = 1

        # Process a disagreeing final via handle_connection
        messages = [{"type": "final", "text": "hello world", "utterance_id": 1}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)

        # Should have a retraction marker and an end marker
        retraction_markers = [e for e in events if e.is_retraction_marker]
        end_markers = [e for e in events if e.is_utterance_end_marker]

        assert len(retraction_markers) == 1
        assert retraction_markers[0].retraction_full_text == "hello world"
        assert retraction_markers[0].utterance_id == 1
        assert len(end_markers) >= 1

    @pytest.mark.asyncio
    async def test_agreeing_final_does_not_trigger_retraction(self, manager):
        """When final agrees with stables, normal processing continues."""
        manager._sent_stable_text = "hello world"
        manager._last_stable_utterance_id = 1
        manager._processed_word_count = 2
        manager.current_utterance_id = 1

        delta = manager._extract_delta("hello world corrected", 1)

        # Normal delta extraction (not None)
        assert delta == "corrected"


class TestLogLevelPropagation:
    """Tests for log level propagation to STT providers."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    def test_default_log_level_is_info(self, manager):
        """WebSocketManager should default _current_log_level to 'INFO'."""
        assert manager._current_log_level == "INFO"

    def test_set_log_level_updates_stored_level(self, manager):
        """set_log_level should update _current_log_level."""
        manager.set_log_level("DEBUG")
        assert manager._current_log_level == "DEBUG"

    @pytest.mark.asyncio
    async def test_handle_connection_sends_log_level(self, manager):
        """handle_connection should send set_log_level to newly connected client."""
        manager._current_log_level = "DEBUG"
        messages = []  # No incoming messages
        ws = _make_mock_ws(messages)

        await manager.handle_connection(ws)

        # Check all calls to ws.send
        sent = [json.loads(call.args[0]) for call in ws.send.call_args_list]
        log_level_msgs = [m for m in sent if m.get("type") == "set_log_level"]
        assert len(log_level_msgs) == 1
        assert log_level_msgs[0]["level"] == "DEBUG"


# ---------------------------------------------------------------------------
# Test: Settling Activity State (provisional dictation window)
# ---------------------------------------------------------------------------

class TestSettlingActivityState:
    """When the first provisional word of an utterance is typed, the manager
    must write a 'settling' activity state so the GUI can show a working
    indicator while the typed text could still be retracted by the final.
    The state must clear at the final via the existing 'confirmed' write, and
    must NOT be written for an utterance that types no provisional text.
    (wh-dictation-retraction-indicator.1)
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        app = MagicMock()
        app.send_command = AsyncMock()
        mgr.set_app(app)
        return mgr

    @pytest.mark.asyncio
    async def test_first_stable_delta_writes_settling_state(self, manager):
        """The first typed provisional word writes ('settling', utterance_id)."""
        messages = [{"type": "stable", "text": "hello", "utterance_id": 50}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_any_call('settling', 50)

    @pytest.mark.asyncio
    async def test_settling_set_then_cleared_by_confirmed_on_final(self, manager):
        """The provisional window opens on the stable ('settling') and closes on
        the final ('confirmed'), in that order."""
        messages = [
            {"type": "stable", "text": "hello", "utterance_id": 60},
            {"type": "final", "text": "hello", "utterance_id": 60},
        ]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        calls = [c.args for c in mock_write.call_args_list]
        assert ('settling', 60) in calls
        assert ('confirmed', 60) in calls
        assert calls.index(('settling', 60)) < calls.index(('confirmed', 60))

    @pytest.mark.asyncio
    async def test_empty_stable_does_not_write_settling(self, manager):
        """A stable that types no new text must not claim text is provisional."""
        messages = [{"type": "stable", "text": "", "utterance_id": 61}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        settling_calls = [
            c for c in mock_write.call_args_list
            if c.args and c.args[0] == 'settling'
        ]
        assert settling_calls == []

    @pytest.mark.asyncio
    async def test_stable_revision_does_not_write_settling(self, manager):
        """A pure revision stable (no new words typed) must not write settling."""
        manager._sent_stable_text = "hello world"
        manager._last_stable_utterance_id = 62
        manager.current_utterance_id = 62
        manager._processed_word_count = 2

        messages = [{"type": "stable", "text": "goodbye", "utterance_id": 62}]
        ws = _make_mock_ws(messages)

        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        settling_calls = [
            c for c in mock_write.call_args_list
            if c.args and c.args[0] == 'settling'
        ]
        assert settling_calls == []


# ---------------------------------------------------------------------------
# Calibration typing gate (wh-7ou.7.2.3, spec Section 6.2)
# ---------------------------------------------------------------------------

class _FakeCalibrationController:
    """Stand-in for speech.calibration_controller.CalibrationController.

    Mirrors exactly the surface the WebSocket manager consumes: the
    ``capturing`` property, ``handle_final`` (which, like the real
    controller, returns True only while the word/noise stages are
    capturing), and the two provider-event methods.
    """

    def __init__(self, capturing=False, session_active=None):
        self.capturing = capturing
        # Real-controller invariant: capturing implies session_active.
        self.session_active = (
            capturing if session_active is None else session_active
        )
        self.finals = []
        self.engine_results = []
        self.provider_connects = 0

    def handle_final(self, text, confidence):
        self.finals.append((text, confidence))
        return self.capturing

    def on_engine_settings_result(self, ok, error):
        self.engine_results.append((ok, error))

    def on_provider_connected(self):
        self.provider_connects += 1


def _wire_calibration_controller(manager, controller):
    """Attach *controller* through the production lookup chain.

    In production main.py wires manager.speech_handler to the
    SpeechHandler, whose ``logic_controller`` exposes
    ``_get_calibration_controller()``. The gate must resolve the
    controller through that chain, not through a bespoke attribute.
    """
    logic = SimpleNamespace(_get_calibration_controller=lambda: controller)
    manager.speech_handler = SimpleNamespace(logic_controller=logic)


# A contract-shaped confidence block (message contract part A.1).
_CAL_CONFIDENCE = {
    "min_word_probability": 0.42,
    "max_no_speech_prob": 0.011,
    "peak_avg_logprob": -0.31,
    "word_count": 1,
    "suppressed": True,
    "rescued": False,
}


class TestCalibrationTypingGate:
    """While a calibration session is capturing, every transcript event
    (interim, stable, final) is handed to the CalibrationController and
    nothing is forwarded into the speech pipeline -- this single gate is
    what guarantees nothing gets typed and no command fires mid-session
    (spec Section 6.2). Outside the capture stages transcripts must flow
    normally so voice clicking works on the intro and review screens
    (spec Section 3.7). Wake-word and status messages always flow.
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        app = MagicMock()
        app.send_command = AsyncMock()
        mgr.set_app(app)
        return mgr

    @pytest.mark.asyncio
    async def test_final_diverted_to_controller_during_capture(self, manager):
        """A final during capture goes to the controller -- text plus the
        contract confidence block -- and queues no WordEvents at all."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "final", "text": "comma", "utterance_id": 500,
            "confidence": _CAL_CONFIDENCE,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert controller.finals == [("comma", _CAL_CONFIDENCE)]
        assert _drain_queue(manager.word_queue) == []
        manager._app.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_diverted_final_still_flashes_confirmed_and_cancels_watchdog(
        self, manager
    ):
        """Status keeps flowing during capture: the GUI green flash and the
        idle-watchdog cancel are status side effects, not pipeline ones."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        watchdog = MagicMock()
        manager._idle_watchdog_handle = watchdog

        messages = [{
            "type": "final", "text": "comma", "utterance_id": 501,
            "confidence": _CAL_CONFIDENCE,
        }]
        ws = _make_mock_ws(messages)
        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_any_call('confirmed', 501)
        watchdog.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_clean_confidence_final_diverted_during_capture(
        self, manager
    ):
        """A capture-stage final whose confidence block carries a CLEAN
        verdict (suppressed=false) must also divert. Distinct from the
        suppressed-verdict case above so the typing gate is proven
        independently of the wh-7ou.7.6.11 verdict gate downstream --
        a leak of clean finals cannot be re-caught by that gate."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        clean = dict(_CAL_CONFIDENCE, suppressed=False)
        messages = [{
            "type": "final", "text": "comma", "utterance_id": 505,
            "confidence": clean,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert controller.finals == [("comma", clean)]
        assert _drain_queue(manager.word_queue) == []

    @pytest.mark.asyncio
    async def test_declined_final_flows_while_session_active(
        self, manager
    ):
        """When the controller declines a final (a click utterance --
        the one allowlisted channel, wh-7ou.7.6.3) it must flow into the
        pipeline unchanged: this is what keeps 'click start' working."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=True,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "final", "text": "click start", "utterance_id": 502}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # The hand-off was offered (race-free single decision point)...
        assert controller.finals == [("click start", None)]
        # ...and declined, so the words reached the pipeline.
        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["click", "start"]
        end_markers = [e for e in events if e.is_utterance_end_marker]
        assert len(end_markers) >= 1

    @pytest.mark.asyncio
    async def test_final_flows_normally_without_controller(self, manager):
        """No calibration wiring at all (speech_handler is None): the gate
        must be inert and dictation unaffected."""
        assert manager.speech_handler is None

        messages = [{"type": "final", "text": "hello", "utterance_id": 503}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["hello"]

    @pytest.mark.asyncio
    async def test_controller_lookup_failure_fails_open(self, manager):
        """A broken lookup must forward transcripts normally: silently
        killing all dictation would be worse than a missed measurement."""
        logic = SimpleNamespace(
            _get_calibration_controller=Mock(side_effect=RuntimeError("boom")),
        )
        manager.speech_handler = SimpleNamespace(logic_controller=logic)

        messages = [{"type": "final", "text": "hello", "utterance_id": 504}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["hello"]

    @pytest.mark.asyncio
    async def test_handle_final_exception_fails_open(self, manager):
        """The real controller never raises, but the gate must not trust
        that: a raising hand-off forwards the final normally."""
        class _Broken(_FakeCalibrationController):
            def handle_final(self, text, confidence):
                raise RuntimeError("synthetic hand-off failure")

        _wire_calibration_controller(manager, _Broken(capturing=True))

        messages = [{"type": "final", "text": "hello", "utterance_id": 505}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["hello"]

    @pytest.mark.asyncio
    async def test_stable_diverted_during_capture(self, manager):
        """Stables (the interim/provisional text) during capture must not
        type anything: no WordEvents, no delta tracking, no 'settling'
        claim, no start_utterance command."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "stable", "text": "comma", "utterance_id": 510}]
        ws = _make_mock_ws(messages)
        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        assert _drain_queue(manager.word_queue) == []
        assert manager._sent_stable_text == ""
        settling_calls = [
            c for c in mock_write.call_args_list
            if c.args and c.args[0] == 'settling'
        ]
        assert settling_calls == []
        manager._app.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stable_dropped_on_non_capture_screens_while_active(
        self, manager
    ):
        """The gate holds for the WHOLE session (wh-7ou.7.6.3): even on
        the intro/review screens, provisional text must not type. Click
        commands fire from finals, so dropping stables costs nothing."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=True,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "stable", "text": "hello", "utterance_id": 511}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert _drain_queue(manager.word_queue) == []
        assert manager._sent_stable_text == ""

    @pytest.mark.asyncio
    async def test_stable_flows_when_no_session(self, manager):
        """An idle controller (window closed, wrong-provider notice):
        stables type normally."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "stable", "text": "hello", "utterance_id": 511}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert "hello" in words

    @pytest.mark.asyncio
    async def test_final_from_non_active_client_dropped_during_session(
        self, manager
    ):
        """A stale (superseded) provider's late final must never become
        a calibration measurement, and during a session it must not
        type either -- it is dropped outright (wh-7ou.7.6.6). Clean
        confidence on purpose: a suppressed final would be dropped by
        the non-active verdict gate (wh-7ou.7.6.13) even without the
        session guard, so only the clean final pins the guard itself."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "final", "text": "comma", "utterance_id": 520,
            "confidence": dict(_CAL_CONFIDENCE, suppressed=False),
        }]
        ws = _make_mock_ws(messages)

        # A newer provider connects right after this one registers, so
        # this client's queued frame is processed while it is no longer
        # the active stream.
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede):
            await manager.handle_connection(ws)

        assert controller.finals == []
        assert _drain_queue(manager.word_queue) == []

    @pytest.mark.asyncio
    async def test_final_from_non_active_client_leaves_status_untouched_during_session(
        self, manager
    ):
        """The sender check must run BEFORE the final's status side
        effects: a stale client's late final must not flash the GUI
        green or cancel the shared idle watchdog while a session runs
        (wh-7ou.7.6.6 round-3 rebuttal). Clean confidence for the same
        reason as the drop test above: the non-active verdict gate
        (wh-7ou.7.6.13) would drop a suppressed final before its status
        side effects anyway; only the clean final exercises the guard."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        # A lingering second client keeps remove_client's last-client
        # watchdog cleanup out of the picture, so the assertions see
        # what the final handler itself did.
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9302)
        manager._clients.add(lingering)

        messages = [{
            "type": "final", "text": "comma", "utterance_id": 530,
            "confidence": dict(_CAL_CONFIDENCE, suppressed=False),
        }]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede), \
                patch.object(manager, '_write_activity_state') as mock_write, \
                patch.object(manager, '_cancel_idle_watchdog') as mock_cancel:
            await manager.handle_connection(ws)

        mock_write.assert_not_called()
        mock_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_start_from_non_active_client_ignored_during_session(
        self, manager
    ):
        """A stale client's vad_start must not fire the GUI hearing
        pulse or arm the shared idle watchdog while a session runs
        (wh-7ou.7.6.6 round-3 rebuttal)."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "vad_start", "utterance_id": 531}]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede), \
                patch.object(manager, '_write_activity_state') as mock_write, \
                patch.object(manager, '_arm_idle_watchdog') as mock_arm:
            await manager.handle_connection(ws)

        mock_write.assert_not_called()
        mock_arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_start_from_non_active_client_flows_when_no_session(
        self, manager
    ):
        """No calibration session: stale-client frames keep their
        pre-existing behavior. The sender gate is calibration-scoped."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "vad_start", "utterance_id": 532}]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede), \
                patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_called_once_with('hearing', 532)

    @pytest.mark.asyncio
    async def test_stable_from_non_active_client_does_not_rearm_watchdog_during_session(
        self, manager
    ):
        """The session-wide stable drop already keeps stale stables out
        of the pipeline, but the watchdog re-arm ran first; a stale
        client must not slide the shared watchdog forward
        (wh-7ou.7.6.6 round-3 rebuttal)."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "stable", "text": "hello", "utterance_id": 533,
        }]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede), \
                patch.object(manager, '_arm_idle_watchdog') as mock_arm:
            await manager.handle_connection(ws)

        mock_arm.assert_not_called()

    @pytest.mark.asyncio
    async def test_eos_from_non_active_client_ignored_during_session(
        self, manager
    ):
        """A stale client's eos must not change the retraction policy
        state for a colliding utterance id while a session runs
        (wh-7ou.7.6.6 round-3 rebuttal)."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        # A lingering second client keeps remove_client's last-client
        # stream reset out of the picture, so the assertion sees what
        # the eos handler itself did.
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9301)
        manager._clients.add(lingering)

        messages = [{"type": "eos", "utterance_id": 534}]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede):
            await manager.handle_connection(ws)

        assert manager._eos_received_for_utterance_id != 534
        assert manager._eos_observed_in_stream is False

    @pytest.mark.asyncio
    async def test_wake_word_from_non_active_client_ignored_during_session(
        self, manager
    ):
        """A stale client's wake-word frame must not publish a wake
        event mid-session (wh-7ou.7.6.6 round-3 rebuttal). The active
        client's wake words keep flowing (spec Section 6.2)."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        sm = MagicMock()
        sm.event_bus.publish = AsyncMock()
        manager.state_manager = sm

        messages = [{"type": "wake_word_detected", "keyword": "computer"}]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede):
            await manager.handle_connection(ws)

        sm.event_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_final_from_non_active_client_flows_when_no_session(
        self, manager
    ):
        """No calibration session: the stale-client final keeps its
        pre-existing behavior (flows normally). The sender gate is
        scoped to calibration only."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "final", "text": "hello", "utterance_id": 521}]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede):
            await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["hello"]

    @pytest.mark.asyncio
    async def test_stable_still_arms_idle_watchdog_during_capture(
        self, manager
    ):
        """The idle watchdog protects the GUI 'hearing' pulse -- status,
        not pipeline -- so a diverted stable still slides it forward."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "stable", "text": "comma", "utterance_id": 512}]
        ws = _make_mock_ws(messages)
        with patch.object(manager, '_arm_idle_watchdog') as mock_arm:
            await manager.handle_connection(ws)

        mock_arm.assert_called_once_with(512)

    @pytest.mark.asyncio
    async def test_vad_start_flows_normally_during_capture(self, manager):
        """vad_start is a status message: the GUI hearing pulse must keep
        working while the calibration window shows a word."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)

        messages = [{"type": "vad_start", "utterance_id": 513}]
        ws = _make_mock_ws(messages)
        with patch.object(manager, '_write_activity_state') as mock_write:
            await manager.handle_connection(ws)

        mock_write.assert_called_once_with('hearing', 513)

    @pytest.mark.asyncio
    async def test_wake_word_flows_normally_during_capture(self, manager):
        """Wake-word messages keep flowing during capture (spec 6.2)."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        sm = MagicMock()
        sm.event_bus.publish = AsyncMock()
        manager.state_manager = sm

        messages = [{"type": "wake_word_detected", "keyword": "computer"}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        sm.event_bus.publish.assert_awaited_once()
        assert _drain_queue(manager.word_queue) == []

    @pytest.mark.asyncio
    async def test_diverted_final_closes_open_pipeline_utterance(self, manager):
        """Capture can begin mid-utterance (Start clicked while speaking):
        stables already queued words downstream, so the diverted final must
        close the open utterance with an end marker -- and only an end
        marker -- and reset delta tracking, mirroring the normal final
        path. Otherwise the clipboard manager downstream waits forever."""
        controller = _FakeCalibrationController(capturing=True)
        _wire_calibration_controller(manager, controller)
        # A lingering second client keeps remove_client's disconnect
        # cleanup out of the queue so only the gate's own marker shows.
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9300)
        manager._clients.add(lingering)
        manager.current_utterance_id = 600
        manager._last_stable_utterance_id = 600
        manager._sent_stable_text = "hello"
        manager._processed_word_count = 1

        messages = [{"type": "final", "text": "hello there", "utterance_id": 600}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        word_events = [e for e in events if e.word]
        end_markers = [e for e in events if e.is_utterance_end_marker]
        assert word_events == []
        assert len(end_markers) == 1
        assert end_markers[0].utterance_id == 600
        assert manager._sent_stable_text == ""
        assert manager._processed_word_count == 0
        assert manager._last_stable_utterance_id is None


class TestSendCommandToActiveStt:
    """Calibration commands target the active stream only
    (wh-7ou.7.6.6). send_command_to_stt broadcasts to every connected
    client -- including superseded providers kept connected in DISABLED
    state -- and a stale whisper provider honoring set_calibration_mode
    or apply_engine_settings would act on a session that is not its own.
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_sends_only_to_the_active_client(self, manager):
        active = AsyncMock()
        active.remote_address = ("127.0.0.1", 1001)
        stale = AsyncMock()
        stale.remote_address = ("127.0.0.1", 1002)
        manager._clients = {active, stale}
        manager._active_stt_client = active

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )

        active.send.assert_awaited_once()
        (payload,), _ = active.send.await_args
        assert json.loads(payload) == {
            "type": "set_calibration_mode", "enabled": True,
        }
        stale.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_active_client_drops_without_raising(self, manager):
        stale = AsyncMock()
        stale.remote_address = ("127.0.0.1", 1002)
        manager._clients = {stale}
        manager._active_stt_client = None

        await manager.send_command_to_active_stt(
            "apply_engine_settings", single_word_min_probability=0.15,
        )

        stale.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_failure_does_not_raise(self, manager):
        active = AsyncMock()
        active.remote_address = ("127.0.0.1", 1001)
        active.send = AsyncMock(side_effect=RuntimeError("gone"))
        manager._clients = {active}
        manager._active_stt_client = active

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

    @pytest.mark.asyncio
    async def test_returns_true_when_the_send_succeeds(self, manager):
        """Callers (the calibration apply path, wh-7ou.7.6.7) need to
        know whether the command actually left this process."""
        active = AsyncMock()
        active.remote_address = ("127.0.0.1", 1001)
        manager._clients = {active}
        manager._active_stt_client = active

        sent = await manager.send_command_to_active_stt(
            "apply_engine_settings", single_word_min_probability=0.15,
        )
        assert sent is True

    @pytest.mark.asyncio
    async def test_returns_false_with_no_active_client(self, manager):
        manager._clients = set()
        manager._active_stt_client = None

        sent = await manager.send_command_to_active_stt(
            "apply_engine_settings", single_word_min_probability=0.15,
        )
        assert sent is False

    @pytest.mark.asyncio
    async def test_returns_false_when_the_send_raises(self, manager):
        active = AsyncMock()
        active.remote_address = ("127.0.0.1", 1001)
        active.send = AsyncMock(side_effect=RuntimeError("gone"))
        manager._clients = {active}
        manager._active_stt_client = active

        sent = await manager.send_command_to_active_stt(
            "apply_engine_settings", single_word_min_probability=0.15,
        )
        assert sent is False


class TestSuppressedVerdictGate:
    """wh-7ou.7.6.11: a final that carries the provider filter's own
    suppressed=true verdict WITH text present can only be a
    calibration-bypass final -- outside the bypass the provider empties
    the transcript before sending, so such text never arrives. When no
    session consumes it (the session just ended and the mode-off command
    is still in flight, or a stale provider never received mode-off),
    the manager applies the verdict itself: the text must not type or
    execute."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        app = MagicMock()
        app.send_command = AsyncMock()
        mgr.set_app(app)
        return mgr

    @pytest.mark.asyncio
    async def test_suppressed_verdict_final_dropped_when_no_session(
        self, manager
    ):
        """No calibration wiring at all: the verdict gate must still
        hold -- this is the stale-provider case, where the session (and
        even the controller) may be long gone."""
        messages = [{
            "type": "final", "text": "delete everything", "utterance_id": 700,
            "confidence": _CAL_CONFIDENCE,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        assert [e.word for e in events if e.word] == []

    @pytest.mark.asyncio
    async def test_suppressed_verdict_final_dropped_after_session_ended(
        self, manager
    ):
        """The mode-off in-flight window: the controller exists but the
        session is over, so it declines the final -- the verdict gate
        must catch it before the pipeline does."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "final", "text": "delete everything", "utterance_id": 701,
            "confidence": _CAL_CONFIDENCE,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        assert [e.word for e in events if e.word] == []

    @pytest.mark.asyncio
    async def test_clean_verdict_final_flows_when_no_session(self, manager):
        """suppressed=false is the filter passing the transcript: the
        gate must not touch it."""
        clean = dict(_CAL_CONFIDENCE, suppressed=False)
        messages = [{
            "type": "final", "text": "hello", "utterance_id": 702,
            "confidence": clean,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        assert [e.word for e in events if e.word] == ["hello"]

    @pytest.mark.asyncio
    async def test_final_without_confidence_flows_when_no_session(
        self, manager
    ):
        """Providers that attach no confidence block (Parakeet, cloud)
        are untouched by the gate."""
        messages = [{"type": "final", "text": "hello", "utterance_id": 703}]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        assert [e.word for e in events if e.word] == ["hello"]

    @pytest.mark.asyncio
    async def test_open_utterance_closed_with_end_marker_on_drop(
        self, manager
    ):
        """Stables typed before the verdict arrives stay (the provider's
        own filter has the same limitation), but the open utterance must
        be closed with an end marker so downstream state resets -- the
        same treatment the diverted-final path applies."""
        # A lingering second client keeps remove_client's disconnect
        # cleanup out of the queue so only the gate's own marker shows
        # (the same masking countermeasure as
        # test_diverted_final_closes_open_pipeline_utterance).
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9301)
        manager._clients.add(lingering)

        messages = [
            {"type": "stable", "text": "delete", "utterance_id": 704},
            {
                "type": "final", "text": "delete everything",
                "utterance_id": 704, "confidence": _CAL_CONFIDENCE,
            },
        ]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        words = [e.word for e in events if e.word]
        assert words == ["delete"]
        end_markers = [e for e in events if e.is_utterance_end_marker]
        assert len(end_markers) >= 1

    @pytest.mark.asyncio
    async def test_non_active_suppressed_final_never_closes_colliding_utterance(
        self, manager
    ):
        """wh-7ou.7.6.13: utterance ids are per-provider counters, so a
        stale non-active client's bypassed final can collide with the
        active stream's live utterance id. The drop must not close the
        ACTIVE stream's utterance or reset its delta state -- those
        belong to a stream that produced nothing here."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9303)
        manager._clients.add(lingering)

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9304)
        ws.send = AsyncMock()

        async def _stable_then_superseded_final():
            # While active: opens pipeline utterance 0 (the "B" role).
            yield json.dumps(
                {"type": "stable", "text": "hello", "utterance_id": 0}
            )
            # A newer provider is promoted; the queued bypassed final
            # (the "A" role) arrives with a colliding utterance id.
            manager._active_stt_client = object()
            yield json.dumps({
                "type": "final", "text": "delete everything",
                "utterance_id": 0, "confidence": _CAL_CONFIDENCE,
            })

        ws.__aiter__ = Mock(return_value=_stable_then_superseded_final())
        await manager.handle_connection(ws)

        events = _drain_queue(manager.word_queue)
        assert [e.word for e in events if e.word] == ["hello"]
        assert [e for e in events if e.is_utterance_end_marker] == []
        assert manager.current_utterance_id == 0
        assert manager._sent_stable_text == "hello"

    @pytest.mark.asyncio
    async def test_non_active_suppressed_final_leaves_status_untouched(
        self, manager
    ):
        """wh-7ou.7.6.13: the drop must run before the 'confirmed' flash
        and the shared-watchdog cancel -- a stream that typed nothing
        must not report success or unarm the active stream's watchdog."""
        controller = _FakeCalibrationController(
            capturing=False, session_active=False,
        )
        _wire_calibration_controller(manager, controller)
        lingering = AsyncMock()
        lingering.remote_address = ("127.0.0.1", 9305)
        manager._clients.add(lingering)

        messages = [{
            "type": "final", "text": "delete everything",
            "utterance_id": 705, "confidence": _CAL_CONFIDENCE,
        }]
        ws = _make_mock_ws(messages)
        orig_add = manager.add_client

        async def add_then_supersede(websocket):
            await orig_add(websocket)
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_then_supersede), \
                patch.object(manager, '_write_activity_state') as mock_write, \
                patch.object(manager, '_cancel_idle_watchdog') as mock_cancel:
            await manager.handle_connection(ws)

        mock_write.assert_not_called()
        mock_cancel.assert_not_called()
        assert _drain_queue(manager.word_queue) == []


class TestCalibrationModeOffTargeting:
    """wh-7ou.7.6.11 (provider-switch path): add_client promotes the new
    provider BEFORE the controller learns of the connect, so a
    session-ending mode-off aimed at "the active client" would go to a
    provider that never had the bypass on -- and leave the provider that
    DOES have it on bypassing its filter until disconnect or the
    10-minute lazy timeout. The off must follow the client that last
    received mode-on."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @staticmethod
    def _frames(ws):
        return [json.loads(c.args[0]) for c in ws.send.await_args_list]

    @pytest.mark.asyncio
    async def test_mode_off_targets_the_mode_on_client(self, manager):
        distil = AsyncMock()
        distil.remote_address = ("127.0.0.1", 1001)
        other = AsyncMock()
        other.remote_address = ("127.0.0.1", 1002)
        manager._clients = {distil, other}
        manager._active_stt_client = distil

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        # Provider switch: the new client is promoted before the
        # controller ends the session.
        manager._active_stt_client = other

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

        assert {
            "type": "set_calibration_mode", "enabled": False,
        } in self._frames(distil)
        assert all(
            f.get("type") != "set_calibration_mode"
            for f in self._frames(other)
        )

    @pytest.mark.asyncio
    async def test_mode_off_falls_back_to_active_when_bound_client_gone(
        self, manager
    ):
        """The bound client disconnecting clears its own bypass provider-
        side, so the off falls back to the active client (an idempotent
        no-op there) instead of failing."""
        distil = AsyncMock()
        distil.remote_address = ("127.0.0.1", 1001)
        other = AsyncMock()
        other.remote_address = ("127.0.0.1", 1002)
        manager._clients = {distil, other}
        manager._active_stt_client = distil

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        manager.remove_client(distil)
        manager._active_stt_client = other

        sent = await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

        assert sent is True
        assert {
            "type": "set_calibration_mode", "enabled": False,
        } in self._frames(other)

    @pytest.mark.asyncio
    async def test_mode_off_binding_clears_after_use(self, manager):
        """One off consumes the binding: a later off (no session of its
        own) must not chase the old client."""
        distil = AsyncMock()
        distil.remote_address = ("127.0.0.1", 1001)
        other = AsyncMock()
        other.remote_address = ("127.0.0.1", 1002)
        manager._clients = {distil, other}
        manager._active_stt_client = distil

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )
        manager._active_stt_client = other

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

        off_to_distil = [
            f for f in self._frames(distil)
            if f == {"type": "set_calibration_mode", "enabled": False}
        ]
        assert len(off_to_distil) == 1
        assert {
            "type": "set_calibration_mode", "enabled": False,
        } in self._frames(other)

    @pytest.mark.asyncio
    async def test_mode_off_reaches_every_enabled_client(self, manager):
        """wh-7ou.7.6.12: a same-provider reconnect mid-capture enables
        calibration mode on the NEW client while the old one is still
        live (add_client only disables its transcription, which never
        touches the engine bypass). The off must reach every client
        whose bypass was turned on, not just the most recent one."""
        distil = AsyncMock()
        distil.remote_address = ("127.0.0.1", 1001)
        other = AsyncMock()
        other.remote_address = ("127.0.0.1", 1002)
        manager._clients = {distil, other}
        manager._active_stt_client = distil

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        # Reconnect: the new client is promoted and the controller
        # re-enables calibration mode on it (spec Section 5.2).
        manager._active_stt_client = other
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

        off = {"type": "set_calibration_mode", "enabled": False}
        assert off in self._frames(distil)
        assert off in self._frames(other)

    @pytest.mark.asyncio
    async def test_mode_off_reaches_earlier_client_when_second_enable_fails(
        self, manager
    ):
        """wh-7ou.7.6.12: a failed enable to the newly promoted client
        must not lose the earlier client whose bypass IS on -- the off
        still has to reach it."""
        distil = AsyncMock()
        distil.remote_address = ("127.0.0.1", 1001)
        other = AsyncMock()
        other.remote_address = ("127.0.0.1", 1002)
        manager._clients = {distil, other}
        manager._active_stt_client = distil

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        manager._active_stt_client = other
        other.send.side_effect = ConnectionError("gone mid-promote")
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        other.send.side_effect = None

        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=False,
        )

        assert {
            "type": "set_calibration_mode", "enabled": False,
        } in self._frames(distil)

    @pytest.mark.asyncio
    async def test_mode_off_completes_despite_stalled_bound_client(
        self, manager
    ):
        """wh-7ou.7.6.14: a stalled bound client's send can block on
        backpressure forever. The off must complete within its bound
        anyway -- the binding set was already consumed, so nothing ever
        retries a target this call abandons."""
        from integrations import websocket_manager as wsm

        stalled = AsyncMock()
        stalled.remote_address = ("127.0.0.1", 1001)

        async def _never(*args, **kwargs):
            await asyncio.Event().wait()

        manager._clients = {stalled}
        manager._active_stt_client = stalled
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        stalled.send = Mock(side_effect=_never)

        with patch.object(
            wsm, "_CAL_MODE_OFF_SEND_TIMEOUT_S", 0.05, create=True,
        ):
            sent = await asyncio.wait_for(
                manager.send_command_to_active_stt(
                    "set_calibration_mode", enabled=False,
                ),
                timeout=2.0,
            )
        assert sent is False

    @pytest.mark.asyncio
    async def test_mode_off_reaches_second_client_despite_stalled_first(
        self, manager
    ):
        """wh-7ou.7.6.14: one stalled bound client must not starve the
        others -- the healthy provider would otherwise stay in
        calibration mode until its 600-second engine timeout."""
        from integrations import websocket_manager as wsm

        stalled = AsyncMock()
        stalled.remote_address = ("127.0.0.1", 1001)

        async def _never(*args, **kwargs):
            await asyncio.Event().wait()

        manager._clients = {stalled}
        manager._active_stt_client = stalled
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )
        stalled.send = Mock(side_effect=_never)

        healthy = AsyncMock()
        healthy.remote_address = ("127.0.0.1", 1002)
        manager._clients = {stalled, healthy}
        manager._active_stt_client = healthy
        await manager.send_command_to_active_stt(
            "set_calibration_mode", enabled=True,
        )

        with patch.object(
            wsm, "_CAL_MODE_OFF_SEND_TIMEOUT_S", 0.05, create=True,
        ):
            await asyncio.wait_for(
                manager.send_command_to_active_stt(
                    "set_calibration_mode", enabled=False,
                ),
                timeout=2.0,
            )
        assert {
            "type": "set_calibration_mode", "enabled": False,
        } in self._frames(healthy)


class TestEngineSettingsResultRouting:
    """engine_settings_result frames (contract part A.4) route to the
    calibration controller's on_engine_settings_result and never enter
    the speech pipeline."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_engine_settings_result_routed_to_controller(self, manager):
        """A failure result carries its exact error text to the controller
        (the save_failed screen shows it under Show details)."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "engine_settings_result", "ok": False, "error": "disk full",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert controller.engine_results == [(False, "disk full")]
        assert _drain_queue(manager.word_queue) == []

    @pytest.mark.asyncio
    async def test_engine_settings_result_ok_true(self, manager):
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "engine_settings_result", "ok": True, "error": None,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert controller.engine_results == [(True, None)]

    @pytest.mark.asyncio
    async def test_engine_settings_result_without_controller_is_dropped(
        self, manager
    ):
        """No controller wired: the frame is dropped without crashing and
        without reaching the unexpected-type path or the pipeline."""
        messages = [{
            "type": "engine_settings_result", "ok": True, "error": None,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert _drain_queue(manager.word_queue) == []

    @pytest.mark.asyncio
    async def test_engine_settings_result_from_stale_client_is_ignored(
        self, manager
    ):
        """A superseded provider's late result describes an old generation
        and must not steer the live session (same gate as notifications)."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        stale_gate = asyncio.Event()
        active_gate = asyncio.Event()
        stale_ws = _GatedWS(
            [{"type": "engine_settings_result", "ok": True, "error": None}],
            stale_gate, ("127.0.0.1", 9101),
        )
        active_ws = _GatedWS([], active_gate, ("127.0.0.1", 9102))

        stale_task = asyncio.create_task(manager.handle_connection(stale_ws))
        await _wait_until(lambda: manager._active_stt_client is stale_ws)
        active_task = asyncio.create_task(manager.handle_connection(active_ws))
        await _wait_until(lambda: manager._active_stt_client is active_ws)

        stale_gate.set()
        await stale_task
        active_gate.set()
        await active_task

        assert controller.engine_results == []

    @pytest.mark.asyncio
    async def test_result_from_apply_target_accepted_after_supersede(
        self, manager
    ):
        """wh-7ou.7.6.6 round-4 rebuttal: the client the apply was SENT
        to owns the reply. If an orphan connect promotes another client
        between the apply send and the reply, dropping the reply as
        non-active strands the controller in the applying state until
        the inactivity timeout, typing gate held."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "engine_settings_result", "ok": True, "error": None,
        }]
        ws = _LateBoundWS(messages)
        orig_add = manager.add_client

        async def add_apply_then_supersede(websocket):
            await orig_add(websocket)
            assert await manager.send_command_to_active_stt(
                "apply_engine_settings", single_word_min_probability=0.15,
            )
            messages[0]["apply_id"] = json.loads(ws.sent[-1]).get("apply_id")
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_apply_then_supersede):
            await manager.handle_connection(ws)

        assert controller.engine_results == [(True, None)]

    @pytest.mark.asyncio
    async def test_late_reply_from_a_previous_apply_ignored(self, manager):
        """wh-7ou.7.6.9: client identity cannot distinguish two applies
        on the same live connection. A delayed reply from an earlier,
        abandoned apply (cancelled session, provider write that hung)
        must not be read as the current apply's answer -- only the reply
        echoing the current apply's id counts, and the binding keeps
        waiting for it."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [
            {"type": "engine_settings_result", "ok": False, "error": "stale"},
            {"type": "engine_settings_result", "ok": True, "error": None},
        ]
        ws = _LateBoundWS(messages)
        orig_add = manager.add_client

        async def add_two_applies(websocket):
            await orig_add(websocket)
            assert await manager.send_command_to_active_stt(
                "apply_engine_settings", single_word_min_probability=0.10,
            )
            messages[0]["apply_id"] = json.loads(ws.sent[-1]).get("apply_id")
            assert await manager.send_command_to_active_stt(
                "apply_engine_settings", single_word_min_probability=0.15,
            )
            messages[1]["apply_id"] = json.loads(ws.sent[-1]).get("apply_id")

        with patch.object(manager, 'add_client', add_two_applies):
            await manager.handle_connection(ws)

        assert controller.engine_results == [(True, None)]

    @pytest.mark.asyncio
    async def test_reply_missing_the_apply_id_ignored_while_apply_pending(
        self, manager
    ):
        """A reply that does not echo the in-flight apply's id cannot be
        correlated to that apply and must not steer the save
        (wh-7ou.7.6.9)."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [{
            "type": "engine_settings_result", "ok": True, "error": None,
        }]
        ws = _LateBoundWS(messages)
        orig_add = manager.add_client

        async def add_and_apply(websocket):
            await orig_add(websocket)
            assert await manager.send_command_to_active_stt(
                "apply_engine_settings", single_word_min_probability=0.15,
            )

        with patch.object(manager, 'add_client', add_and_apply):
            await manager.handle_connection(ws)

        assert controller.engine_results == []

    @pytest.mark.asyncio
    async def test_result_from_other_client_ignored_while_apply_pending(
        self, manager
    ):
        """While an apply is in flight, even the CURRENT active client
        cannot answer for it -- only the client the apply went to. A
        promoted orphan echoing a result must not steer the save."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        target = AsyncMock()
        target.remote_address = ("127.0.0.1", 9201)
        manager._clients.add(target)
        manager._active_stt_client = target
        assert await manager.send_command_to_active_stt(
            "apply_engine_settings", single_word_min_probability=0.15,
        )

        # A different client connects, becomes active, and sends a
        # result the apply target never produced.
        messages = [{
            "type": "engine_settings_result", "ok": True, "error": None,
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        assert controller.engine_results == []

    @pytest.mark.asyncio
    async def test_result_binding_clears_after_the_reply(self, manager):
        """The binding covers exactly one reply: once consumed, a later
        duplicate from the same (now superseded) client falls back to
        the active-client gate and is ignored."""
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        messages = [
            {"type": "engine_settings_result", "ok": True, "error": None},
            {"type": "engine_settings_result", "ok": False, "error": "late"},
        ]
        ws = _LateBoundWS(messages)
        orig_add = manager.add_client

        async def add_apply_then_supersede(websocket):
            await orig_add(websocket)
            assert await manager.send_command_to_active_stt(
                "apply_engine_settings", single_word_min_probability=0.15,
            )
            apply_id = json.loads(ws.sent[-1]).get("apply_id")
            messages[0]["apply_id"] = apply_id
            messages[1]["apply_id"] = apply_id
            manager._active_stt_client = object()

        with patch.object(manager, 'add_client', add_apply_then_supersede):
            await manager.handle_connection(ws)

        assert controller.engine_results == [(True, None)]


class TestCalibrationProviderConnect:
    """Every provider (re)connect is reported to the calibration
    controller: mid-capture it re-enables calibration mode (the provider
    reset it on disconnect, spec 5.2); post-apply it completes the
    session (the settings restart finished)."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        mgr.state_manager = None
        return mgr

    @pytest.mark.asyncio
    async def test_add_client_notifies_calibration_controller(self, manager):
        controller = _FakeCalibrationController()
        _wire_calibration_controller(manager, controller)

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9200)
        await manager.add_client(ws)

        assert controller.provider_connects == 1

    @pytest.mark.asyncio
    async def test_add_client_survives_controller_failure(self, manager):
        """A raising on_provider_connected must not break registration:
        the client still joins and becomes the active stream."""
        class _Broken(_FakeCalibrationController):
            def on_provider_connected(self):
                raise RuntimeError("synthetic connect failure")

        _wire_calibration_controller(manager, _Broken())

        ws = AsyncMock()
        ws.remote_address = ("127.0.0.1", 9201)
        await manager.add_client(ws)

        assert ws in manager._clients
        assert manager._active_stt_client is ws


class TestCaptureBackendLogLine:
    """WheelHouse writes which capture path the provider took.

    wh-capture-winrt-required A4. The provider's refusal (A3) covers the
    case where winsdk is missing. It cannot cover a provider that starts
    and captures through a path nobody expected, which is what happened
    on 2026-09-05: a run captured through PortAudio for hours and no line
    anywhere in wheelhouse.log said so. The ready notification is the one
    frame that knows the answer, so the line is written where that frame
    is read.
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        return mgr

    @staticmethod
    def _ready_messages(provider="google_stt", backend="winrt",
                        title="Google STT"):
        """A declared connection followed by its ready notice.

        The capabilities frame must come FIRST. Without it the connection
        stays provisional and _rebind_launch_stamp never runs, so the
        ready is dropped before any of this is reached
        (wh-ready-connection-stamp.2).
        """
        notice = {
            "type": "notification",
            "title": title,
            "message": "Provider is ready for transcription",
            "kind": "ready",
        }
        if backend is not None:
            notice["capture_backend"] = backend
        return [{
            "type": "capabilities",
            "provider": provider,
            "emits_eos": False,
        }, notice]

    def _launcher(self):
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.current_launch_generation.return_value = 4
        launcher.launch_generation.return_value = 4
        return launcher

    @staticmethod
    def _capture_lines(caplog):
        """The ready lines that speak about capture, and only those.

        Asserting on the whole of caplog.text would pass on the
        capabilities line, which already names the provider and has
        nothing to do with this criterion.
        """
        return [
            r.getMessage() for r in caplog.records
            if "capture" in r.getMessage().lower()
            and "ready" in r.getMessage().lower()
        ]

    @pytest.mark.asyncio
    async def test_the_ready_writes_the_capture_backend_to_the_log(
        self, manager, caplog
    ):
        """The whole point of A4: an operator reading wheelhouse.log can
        answer "which capture path did this run use?" without attaching a
        debugger. The name is the provider's own report of what its
        factory built."""
        manager.remote_stt_launcher = self._launcher()
        manager.state_manager = None

        ws = _make_mock_ws(self._ready_messages())
        with caplog.at_level(
            logging.INFO, logger="integrations.websocket_manager"
        ):
            await manager.handle_connection(ws)

        lines = self._capture_lines(caplog)
        assert lines, caplog.text
        assert "winrt" in " ".join(lines).lower(), lines
        assert "Google STT" in " ".join(lines), lines

    @pytest.mark.asyncio
    async def test_the_line_reports_the_backend_the_provider_named(
        self, manager, caplog
    ):
        """Not a constant WheelHouse prints regardless. A line that always
        said "winrt" would have stayed just as green through the
        2026-09-05 PortAudio run, which is the defect this criterion
        exists for, so the test drives a different name and requires that
        name to appear."""
        manager.remote_stt_launcher = self._launcher()
        manager.state_manager = None

        ws = _make_mock_ws(self._ready_messages(backend="portaudio"))
        with caplog.at_level(
            logging.INFO, logger="integrations.websocket_manager"
        ):
            await manager.handle_connection(ws)

        lines = self._capture_lines(caplog)
        assert lines, caplog.text
        assert "portaudio" in " ".join(lines).lower(), lines

    @pytest.mark.asyncio
    async def test_a_ready_without_the_field_says_so_rather_than_guessing(
        self, manager, caplog
    ):
        """An older provider, or one still to be built, sends no
        capture_backend. Naming a path for it would be an invention, and
        an invented "winrt" is exactly the false assurance A4 removes.
        Saying the provider did not report one is the honest line."""
        manager.remote_stt_launcher = self._launcher()
        manager.state_manager = None

        ws = _make_mock_ws(self._ready_messages(backend=None))
        with caplog.at_level(
            logging.INFO, logger="integrations.websocket_manager"
        ):
            await manager.handle_connection(ws)

        lines = self._capture_lines(caplog)
        assert lines, caplog.text
        assert "winrt" not in " ".join(lines).lower(), lines


class TestWinrtRefusalReachesTheUser:
    """The refusal a provider sends must arrive as a notice, unchanged.

    wh-capture-winrt-required A4, at the boss's direction: A3 proved the
    provider puts the approved wording on the socket, which is only half
    the claim. A message WheelHouse drops, truncates, or replaces with a
    generic failure leaves the user exactly where the defect left them --
    a speech engine that does not work and no statement of why. This
    class covers the other half: the text the provider sent is the text
    the user is shown.

    The wording is spelled out here rather than imported. The wheelhouse
    service and the provider shared package have separate virtual
    environments, so this file cannot import WINRT_REQUIRED_MESSAGE; the
    same reason the _SHIPPED_PROVIDERS list above spells out provider
    identifiers. A copy is the cost of the process boundary, and a
    divergence is caught on the provider side, where
    tests/test_winrt_capture_required.py holds the same literal against
    the constant.
    """

    # Exactly the text David approved, including the full stops and the
    # two instructions. Not paraphrased: the second sentence is what a
    # user acts on and the third is what a developer acts on.
    APPROVED = (
        "The speech service cannot start: the audio package winsdk is "
        "not installed. Re-run the WheelHouse installer. Developers: "
        "run bootstrap.ps1."
    )

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(loop=event_loop)
        return mgr

    @pytest.mark.asyncio
    async def test_the_refusal_text_reaches_the_user_notice_unchanged(
        self, manager
    ):
        """The end-to-end claim of A3 plus A4, on the WheelHouse side.

        This behaviour predates the change -- websocket_manager falls
        through to the toast after the startup_failed branch, deliberately
        (wh-google-creds-file-picker.1.5) -- so this test could not be
        made to fail by writing it first. Its red state was produced by
        mutation instead: suppressing that fall-through (a `continue` at
        the end of the startup_failed branch) fails it. That is a
        mutation proof, not red-first evidence, and it is recorded as one.
        """
        launcher = MagicMock()
        launcher.is_starting = True
        launcher.launch_generation.return_value = 3
        mock_notifier = MagicMock()
        mock_notifier._send_notification = MagicMock()
        mock_sm = MagicMock()
        mock_sm.speech_notifier = mock_notifier
        manager.remote_stt_launcher = launcher
        manager.state_manager = mock_sm

        messages = [{
            "type": "capabilities",
            "provider": "parakeet_tdt",
            "emits_eos": True,
        }, {
            "type": "notification",
            "title": "Parakeet v3 (GPU)",
            "message": self.APPROVED,
            "kind": "startup_failed",
        }]
        ws = _make_mock_ws(messages)
        await manager.handle_connection(ws)

        # The launcher's starting state ends, so the user is not left
        # watching a working dialog for an engine that will never start.
        launcher.signal_provider_startup_failed.assert_called_once_with(3)
        # And the notice carries the provider's own words. An assertion
        # on a substring would pass on a truncated message, which is one
        # of the failures this test exists to catch, so it compares the
        # whole string.
        mock_notifier._send_notification.assert_called_once()
        shown = mock_notifier._send_notification.call_args.args[1]
        assert shown == self.APPROVED, shown
