"""Integration test for the full retraction pipeline.

Tests the flow: WebSocketManager detects disagreement -> queues retraction
marker -> SpeechProcessor handles retraction -> sends retract IPC -> replays
corrected words.

All IPC boundaries are mocked (no actual processes or shared memory).
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import pytest
from typing import List, Dict, Any, Optional
from unittest.mock import MagicMock

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.domain import ProcessingMode


class RecordingApp:
    """Mock app that records all IPC calls and returns configurable responses."""

    def __init__(self):
        self.commands: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.retract_response = {"status": "retracted", "chars": 0}

    async def send_command(self, payload: dict):
        self.commands.append(payload)

    async def send_request(self, action: str, params: Optional[dict] = None, timeout_s: Optional[float] = None):
        entry = {"action": action, "params": params or {}}
        self.requests.append(entry)
        if action == "retract":
            return self.retract_response
        return {"status": "ok"}

    @property
    def all_actions(self):
        """All actions in order (commands + requests)."""
        return self.commands + self.requests

    def get_insert_texts(self):
        """Get all intelligent_insert_text payloads in order."""
        return [
            r["params"]["insertion_string"]
            for r in self.requests
            if r["action"] == "intelligent_insert_text"
        ]


class NoOpTextParser:
    """Text parser that never matches commands (all text goes to dictation)."""

    async def parse_and_execute(self, text, return_remainder=False):
        if return_remainder:
            return False, text
        return False


def make_pipeline(retract_response=None):
    """Create a SpeechProcessor wired for pipeline testing."""
    app = RecordingApp()
    if retract_response:
        app.retract_response = retract_response

    queue = asyncio.Queue()
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    # wh-2vz: real PatternCatalog returns None for words not in the
    # trailing-commands map. MagicMock's default truthy return would
    # make SpeechProcessor capture every word as a trailing candidate.
    catalog.get_trailing_command.return_value = None

    processor = SpeechProcessor(
        word_queue=queue,
        catalog=catalog,
        text_parser=NoOpTextParser(),
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=1000,
        hotword="x-ray",
    )
    processor.context_mirror = MagicMock()
    processor.context_mirror.read_context.return_value = {
        "app_name": "test.exe",
        "window_title": "Test",
    }
    return processor, app, queue


class TestRetractionPipeline:
    """Full retraction pipeline tests."""

    @pytest.mark.asyncio
    async def test_stable_words_then_retraction_then_replay(self):
        """Stables pasted, final disagrees, retraction fires, replay inserts corrected text."""
        proc, app, queue = make_pipeline(
            retract_response={"status": "retracted", "chars": 15}
        )

        # Simulate: stable words "hello whirled" were already processed
        # (they would have been sent as WordEvents and pasted)
        word1 = WordEvent(word="hello", start_of_utterance=True, end_of_utterance=False, utterance_id=1)
        word2 = WordEvent(word="whirled", start_of_utterance=False, end_of_utterance=False, utterance_id=1)

        await proc.process_word_event(word1)
        await proc.process_word_event(word2)

        # Verify stable words were sent to dictation
        assert app.get_insert_texts() == ["hello", "whirled"]

        # Now the retraction marker arrives (final = "hello world")
        retraction = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        )
        await proc.process_word_event(retraction)

        # Verify retract IPC was sent
        retract_reqs = [r for r in app.requests if r["action"] == "retract"]
        assert len(retract_reqs) == 1

        # Verify replayed words were sent to dictation
        all_inserts = app.get_insert_texts()
        # First two are originals, next two are replay
        assert all_inserts == ["hello", "whirled", "hello", "world"]

    @pytest.mark.asyncio
    async def test_retraction_blocked_by_user_interaction(self):
        """When retract IPC returns not_retracted, no replay happens."""
        proc, app, queue = make_pipeline(
            retract_response={"status": "not_retracted", "reason": "user_interacted"}
        )

        # Stable word
        word1 = WordEvent(word="hello", start_of_utterance=True, end_of_utterance=False, utterance_id=1)
        await proc.process_word_event(word1)

        # Retraction marker
        retraction = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hey",
        )
        await proc.process_word_event(retraction)

        # Retract was attempted
        retract_reqs = [r for r in app.requests if r["action"] == "retract"]
        assert len(retract_reqs) == 1

        # But no replay happened
        all_inserts = app.get_insert_texts()
        assert all_inserts == ["hello"]  # Only the original stable word

    @pytest.mark.asyncio
    async def test_end_utterance_after_retraction_and_replay(self):
        """End utterance marker processes correctly after retraction + replay."""
        proc, app, queue = make_pipeline(
            retract_response={"status": "retracted", "chars": 5}
        )

        # Stable word
        word1 = WordEvent(word="hello", start_of_utterance=True, end_of_utterance=False, utterance_id=1)
        await proc.process_word_event(word1)

        # Retraction
        retraction = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hey",
        )
        await proc.process_word_event(retraction)

        # End marker
        end = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=1,
            is_utterance_end_marker=True,
        )
        await proc.process_word_event(end)

        # end_utterance command was sent
        end_cmds = [
            c for c in app.commands
            if c.get("action") == "end_utterance"
        ]
        assert len(end_cmds) == 1
