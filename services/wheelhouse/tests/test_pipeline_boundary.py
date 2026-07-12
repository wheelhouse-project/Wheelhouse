"""Cross-boundary regression tests for the speech-pipeline sprint (wh-oe7u.8).

Each per-bead fix in this sprint already lands with its own focused
regression test under the relevant component's test file. This module
adds the cross-boundary tests that no single bead naturally owns:

1. **WS-to-SpeechProcessor end-to-end ordering**: real
   ``WebSocketManager`` parses a stable + disagreeing-final sequence,
   produces ``WordEvent`` objects on the same queue a real
   ``SpeechProcessor`` consumes, and the resulting IPC actions land in
   the order ``insert -> retract -> replay-insert -> end_utterance``.

2. **te_event vs ordinary request_id demuxing** -- already covered by
   ``tests/test_app_events.py::TestEventDispatchWithRequestId``
   (``test_te_event_with_request_id_reaches_event_handler`` plus
   ``test_send_request_response_still_resolves`` exercise both shapes).
   No new test added here.

3. **Stale timeout sentinel** -- covered by
   ``tests/test_timeout_sentinel.py::TestSentinelStaleTokenIgnored``
   (``test_stale_sentinel_does_not_finalize_newer_buffer`` exercises
   the timeout-task -> queue -> processing-loop -> state-mutation
   chain). No new test added here.
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

from speech.speech_processor import SpeechProcessor


class _RecordingApp:
    """Mock app that records IPC calls in arrival order, with a
    configurable retract response.

    The ``actions`` list is the chronological event log -- one entry
    per send_command/send_request invocation -- so tests can assert on
    cross-action ordering.
    """

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self.retract_response = {"status": "retracted", "chars": 0}

    async def send_command(self, payload: dict):
        self.actions.append({
            "kind": "command",
            "action": payload.get("action"),
            "params": payload.get("params", {}),
        })

    async def send_request(
        self,
        action: str,
        params: Optional[dict] = None,
        timeout_s: Optional[float] = None,
    ):
        self.actions.append({
            "kind": "request",
            "action": action,
            "params": params or {},
        })
        if action == "retract":
            return self.retract_response
        return {"status": "ok"}


class _NoOpTextParser:
    """Text parser stub: never matches a command, all text dictates."""

    async def parse_and_execute(
        self, text, return_remainder=False, authorized_command=False,
    ):
        if return_remainder:
            return False, text
        return False


def _make_mock_ws(messages):
    """Build an AsyncMock websocket whose async-iteration yields encoded JSON."""
    encoded = [json.dumps(m) if isinstance(m, dict) else m for m in messages]

    async def _async_iter():
        for item in encoded:
            yield item

    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 9999)
    ws.send = AsyncMock()
    ws.__aiter__ = Mock(return_value=_async_iter())
    return ws


def _make_processor(word_queue: asyncio.Queue, app: _RecordingApp) -> SpeechProcessor:
    """Wire a real SpeechProcessor onto the supplied queue."""
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    proc = SpeechProcessor(
        word_queue=word_queue,
        catalog=catalog,
        text_parser=_NoOpTextParser(),
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=1000,
        hotword="x-ray",
    )
    proc.context_mirror = MagicMock()
    proc.context_mirror.read_context.return_value = {
        "app_name": "test.exe",
        "window_title": "Test",
    }
    return proc


class TestWebSocketToSpeechProcessorOrdering:
    """End-to-end cross-boundary test (wh-oe7u.8 Test 1).

    Real ``WebSocketManager`` parses a stable + disagreeing-final
    sequence and emits WordEvents onto the queue. Real
    ``SpeechProcessor`` consumes the queue and drives a fake app that
    records every IPC action. The expected sequence on the recording
    app is::

        intelligent_insert_text("<stable word(s)>")  # original stables
        retract                                      # disagreement detected
        intelligent_insert_text(...)*N               # replay corrected words
        end_utterance                                # last
    """

    @pytest.fixture
    def event_loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def app(self):
        return _RecordingApp()

    @pytest.fixture
    def ws_manager(self, event_loop):
        from integrations.websocket_manager import WebSocketManager
        mgr = WebSocketManager(event_loop)
        mgr.state_manager = MagicMock()
        mgr.state_manager.config_service = MagicMock()
        mgr.state_manager.config_service.get.return_value = False  # disable toast
        return mgr

    @pytest.mark.asyncio
    async def test_stable_then_disagreeing_final_then_end_utterance(
        self, ws_manager, app,
    ):
        """One stable then a disagreeing final yields:
        original-insert -> retract -> replay-insert(s) -> end_utterance."""
        # Drive the WebSocketManager: a stable word, then a final that
        # disagrees with the stable. The manager queues the stable as a
        # WordEvent first, then a retraction marker carrying the corrected
        # full text, then the utterance_end_marker.
        messages = [
            {"type": "stable", "text": "hello whirled", "utterance_id": 1},
            {"type": "final", "text": "hello world", "utterance_id": 1},
        ]
        ws = _make_mock_ws(messages)
        await ws_manager.handle_connection(ws)

        # Wire the SAME queue into a real SpeechProcessor and let the
        # processing loop drain it.
        processor = _make_processor(ws_manager.word_queue, app)
        await processor.start()
        try:
            # Drain. Allow generous wall time because handle_connection
            # may have queued multiple events across stable/final/end.
            for _ in range(200):
                await asyncio.sleep(0.01)
                if ws_manager.word_queue.empty() and any(
                    a.get("action") == "end_utterance" for a in app.actions
                ):
                    break
        finally:
            await processor.stop()

        # Assemble the action sequence (ignoring intermediate ones we
        # do not care about; we only assert on the canonical ordering
        # of insert/retract/replay/end).
        action_names = [a.get("action") for a in app.actions]

        # Find ordered indices for the canonical events.
        first_insert_idx = next(
            (i for i, n in enumerate(action_names)
             if n == "intelligent_insert_text"),
            None,
        )
        retract_idx = next(
            (i for i, n in enumerate(action_names) if n == "retract"),
            None,
        )
        end_idx = next(
            (i for i, n in enumerate(action_names) if n == "end_utterance"),
            None,
        )

        assert first_insert_idx is not None, (
            f"Stable insert never fired. Actions: {action_names}"
        )
        assert retract_idx is not None, (
            f"Retract IPC never fired despite disagreeing final. "
            f"Actions: {action_names}"
        )
        assert end_idx is not None, (
            f"end_utterance never fired. Actions: {action_names}"
        )

        # Required ordering: stable insert BEFORE retract,
        # retract BEFORE end_utterance, and at least one
        # replay insert between retract and end_utterance.
        assert first_insert_idx < retract_idx, (
            f"Original stable insert must fire BEFORE retract; got "
            f"insert@{first_insert_idx} retract@{retract_idx}. "
            f"Actions: {action_names}"
        )
        assert retract_idx < end_idx, (
            f"end_utterance must fire AFTER retract+replay. "
            f"Actions: {action_names}"
        )

        replay_inserts_after_retract = [
            i for i, n in enumerate(action_names)
            if n == "intelligent_insert_text" and retract_idx < i < end_idx
        ]
        assert replay_inserts_after_retract, (
            f"No replay insert between retract and end_utterance. The "
            f"retraction handler must replay the corrected final. "
            f"Actions: {action_names}"
        )
