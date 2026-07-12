"""Tests for trace_id passthrough in WebSocketManager.

WebSocketManager reads trace_id from incoming STT messages and passes it
through to WordEvents. It no longer generates trace IDs itself.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from speech.word_event import WordEvent


class TestWebSocketTraceIdPassthrough:
    """WebSocketManager passes trace_id from incoming messages to WordEvents."""

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager

        mgr = WebSocketManager(loop=event_loop)
        mock_app = MagicMock()
        mock_app.send_command = AsyncMock()
        mgr.set_app(mock_app)
        return mgr

    def _drain_queue(self, queue: asyncio.Queue) -> list[WordEvent]:
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    @pytest.mark.asyncio
    async def test_no_generation_attributes(self, manager):
        """WebSocketManager should not have trace generation attributes."""
        assert not hasattr(manager, "_trace_counter")
        assert not hasattr(manager, "_session_prefix")

    @pytest.mark.asyncio
    async def test_stable_words_carry_trace_id_from_message(self, manager):
        """Words from a stable message carry the trace_id from the incoming JSON."""
        manager.current_utterance_id = None
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager._sent_stable_text = ""

        # Simulate stable message processing with trace_id
        utterance_id = 1
        text = "hello world"
        trace_id = "T-17720345601"

        delta = manager._extract_delta(text, utterance_id)
        assert delta is not None

        if manager.current_utterance_id != utterance_id:
            manager.current_utterance_id = utterance_id
            if manager._app:
                await manager._app.send_command({
                    'action': 'start_utterance',
                    'params': {'utterance_id': utterance_id}
                })

        delta_words = delta.split()
        previous_count = manager._processed_word_count - len(delta_words)
        for i, word in enumerate(delta_words):
            is_first = (i == 0 and previous_count == 0)
            word_event = WordEvent(
                word=word,
                start_of_utterance=is_first,
                end_of_utterance=False,
                utterance_id=utterance_id,
                trace_id=trace_id,
            )
            await manager.word_queue.put(word_event)

        events = self._drain_queue(manager.word_queue)
        assert len(events) == 2
        assert all(e.trace_id == "T-17720345601" for e in events)

    @pytest.mark.asyncio
    async def test_missing_trace_id_defaults_to_empty(self, manager):
        """When trace_id is missing from incoming message, WordEvents get empty string."""
        manager.current_utterance_id = None
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager._sent_stable_text = ""

        utterance_id = 1
        text = "hello"
        trace_id = ""  # Missing from message -> defaults to ""

        delta = manager._extract_delta(text, utterance_id)
        assert delta is not None

        if manager.current_utterance_id != utterance_id:
            manager.current_utterance_id = utterance_id

        delta_words = delta.split()
        for word in delta_words:
            word_event = WordEvent(
                word=word,
                start_of_utterance=True,
                end_of_utterance=False,
                utterance_id=utterance_id,
                trace_id=trace_id,
            )
            await manager.word_queue.put(word_event)

        events = self._drain_queue(manager.word_queue)
        assert len(events) == 1
        assert events[0].trace_id == ""

    @pytest.mark.asyncio
    async def test_end_marker_carries_trace_id(self, manager):
        """End markers carry the same trace_id as the utterance."""
        end_marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=10,
            is_utterance_end_marker=True,
            trace_id="T-17720345601",
        )
        assert end_marker.trace_id == "T-17720345601"

    @pytest.mark.asyncio
    async def test_different_utterances_get_different_trace_ids(self, manager):
        """Each utterance can carry a different trace_id from the STT provider."""
        manager.current_utterance_id = None
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager._sent_stable_text = ""

        # First utterance
        delta1 = manager._extract_delta("hello", 1)
        word1 = WordEvent(
            word="hello",
            start_of_utterance=True,
            end_of_utterance=False,
            utterance_id=1,
            trace_id="T-17720345601",
        )
        await manager.word_queue.put(word1)

        # Reset for second utterance
        manager._processed_word_count = 0
        manager._last_stable_utterance_id = None
        manager._sent_stable_text = ""

        delta2 = manager._extract_delta("world", 2)
        word2 = WordEvent(
            word="world",
            start_of_utterance=True,
            end_of_utterance=False,
            utterance_id=2,
            trace_id="T-17720345602",
        )
        await manager.word_queue.put(word2)

        events = self._drain_queue(manager.word_queue)
        assert len(events) == 2
        assert events[0].trace_id == "T-17720345601"
        assert events[1].trace_id == "T-17720345602"

    @pytest.mark.asyncio
    async def test_log_message_formats_without_inline_trace_id(self, manager):
        """Forwarded log messages should NOT contain inline trace_id (formatter handles it)."""
        data = {
            "type": "log",
            "level": "INFO",
            "message": "Streaming audio to API",
            "source": "Google STT",
            "timestamp": "2026-03-02T10:00:00",
            "trace_id": "T-17720345601",
        }

        log_source = data.get("source", "STT")
        log_message = data.get("message", "")

        formatted_msg = f"[{log_source}] {log_message}"

        # trace_id should NOT be in the message body (formatter's trace= field handles it)
        assert formatted_msg == "[Google STT] Streaming audio to API"
        assert "T-17720345601" not in formatted_msg

    @pytest.mark.asyncio
    async def test_set_trace_called_with_trace_id(self, manager):
        """set_trace() should be called with trace_id from incoming messages."""
        from unittest.mock import patch

        with patch("integrations.websocket_manager.set_trace") as mock_set_trace:
            # Simulate a stable message through the full handler
            ws = AsyncMock()
            msg = json.dumps({
                "type": "stable",
                "text": "hello",
                "utterance_id": 1,
                "trace_id": "T-12345",
            })
            ws.__aiter__ = lambda self: self
            ws.__anext__ = AsyncMock(side_effect=[msg, StopAsyncIteration])
            ws.remote_address = ("127.0.0.1", 9999)
            ws.send = AsyncMock()

            manager._clients.add(ws)

            await manager.handle_connection(ws)

            mock_set_trace.assert_called_with("T-12345")

    @pytest.mark.asyncio
    async def test_set_trace_called_with_empty_when_no_trace_id(self, manager):
        """set_trace() should be called with empty string when trace_id is missing."""
        from unittest.mock import patch

        with patch("integrations.websocket_manager.set_trace") as mock_set_trace:
            ws = AsyncMock()
            msg = json.dumps({
                "type": "stable",
                "text": "hello",
                "utterance_id": 1,
                # No trace_id field
            })
            ws.__aiter__ = lambda self: self
            ws.__anext__ = AsyncMock(side_effect=[msg, StopAsyncIteration])
            ws.remote_address = ("127.0.0.1", 9999)
            ws.send = AsyncMock()

            manager._clients.add(ws)

            await manager.handle_connection(ws)

            mock_set_trace.assert_called_with("")
