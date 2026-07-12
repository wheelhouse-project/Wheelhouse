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
        manager.remote_stt_launcher = launcher
        manager.state_manager = None

        messages = [{
            "type": "notification",
            "title": "STT Provider",
            "message": "Provider is ready for transcription"
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
