"""Tests for SpeechProcessor retraction handling.

Covers:
- _command_executed_in_utterance flag lifecycle
- Retraction marker skipped when command was executed
- Retraction marker triggers retract IPC and replays words on success
- Retraction marker drops final when retract IPC returns not_retracted
- Buffer and timeout cancelled before retraction
- Replay words have correct start_of_utterance flags
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
from typing import List, Dict, Any, Optional, Union
from unittest.mock import MagicMock, AsyncMock, patch

from speech.word_event import WordEvent
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.domain import ProcessingMode, Action, Decision


# ============================================================================
# MOCK COMPONENTS
# ============================================================================

class MockApp:
    """Mock app recording IPC calls.

    ``_retract_response`` may be a single dict (returned for every
    retract call) or a list of dicts (each call pops the next response,
    cycling on the last). The list form survives from the removed
    editor_unconfirmed retry path; the remaining tests use the single-
    dict form. The wh-g2-refactor.14 slice removed the retry; see the
    Section 2 deepseek-concern-F discussion in
    docs/design/2026-05-20-g2-refactor-design-refinements.md for the
    rationale.
    """

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self._retract_response: Union[Dict[str, Any], List[Dict[str, Any]]] = {
            "status": "retracted", "chars": 10,
        }
        self._retract_call_index = 0

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: Optional[dict] = None, timeout_s: Optional[float] = None):
        payload = {"action": action, "params": params or {}}
        self.actions.append(payload)
        if action == "retract":
            if isinstance(self._retract_response, list):
                idx = min(self._retract_call_index, len(self._retract_response) - 1)
                self._retract_call_index += 1
                return self._retract_response[idx]
            return self._retract_response
        return {"status": "ok"}

    def clear(self):
        self.actions.clear()
        self._retract_call_index = 0


class MockTextParser:
    """Mock text parser that tracks executed commands.

    last_executed_pattern_type mirrors the real parser's contract
    (wh-med0): set to 'command' or 'replacement' on a match, None
    otherwise. Tests opting into a specific match type override this
    on the instance.
    """

    def __init__(self):
        self.executed: List[str] = []
        self.last_executed_pattern_type: Optional[str] = "command"

    async def parse_and_execute(self, text, return_remainder=False, authorized_command=False):
        self.executed.append(text)
        if return_remainder:
            return True, ""
        return True


def make_processor(app=None, command_timeout_ms=1000, replacement_timeout_ms=700):
    """Create a SpeechProcessor with mocked dependencies."""
    app = app or MockApp()
    queue = asyncio.Queue()
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    # wh-2vz: in real PatternCatalog, get_trailing_command returns None
    # for any word that is not registered as a trailing-position command.
    # MagicMock's default truthy return would make SpeechProcessor treat
    # every word as a trailing candidate.
    catalog.get_trailing_command.return_value = None
    text_parser = MockTextParser()

    processor = SpeechProcessor(
        word_queue=queue,
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        replacement_timeout_ms=replacement_timeout_ms,
        command_timeout_ms=command_timeout_ms,
        hotword="x-ray",
    )
    # Mock context mirror
    processor.context_mirror = MagicMock()
    processor.context_mirror.read_context.return_value = {
        "app_name": "test.exe",
        "window_title": "Test Window",
    }
    return processor


# ============================================================================
# TESTS: Command Execution Flag
# ============================================================================

class TestCommandExecutedFlag:
    """_command_executed_in_utterance flag lifecycle."""

    @pytest.mark.asyncio
    async def test_flag_starts_false(self):
        proc = make_processor()
        assert proc._command_executed_in_utterance is False

    @pytest.mark.asyncio
    async def test_flag_set_on_command_execution(self):
        proc = make_processor()
        proc.text_parser.last_executed_pattern_type = "command"
        await proc._execute_command("test command")
        assert proc._command_executed_in_utterance is True

    @pytest.mark.asyncio
    async def test_flag_not_set_on_replacement_execution(self):
        """wh-med0: a replacement match must NOT block subsequent retraction.

        Replacements are pure text substitutions (e.g. 'period' -> '.').
        They are dictation under a different spelling. If STT later
        revises the trigger word away, the corrected text must replay
        cleanly. Treating replacements as commands silently dropped the
        correction in the field."""
        proc = make_processor()
        proc.text_parser.last_executed_pattern_type = "replacement"
        await proc._execute_command("period")
        assert proc._command_executed_in_utterance is False

    @pytest.mark.asyncio
    async def test_flag_reset_on_new_utterance(self):
        proc = make_processor()
        proc._command_executed_in_utterance = True

        # Process a word with start_of_utterance=True
        word = WordEvent(
            word="hello",
            start_of_utterance=True,
            end_of_utterance=False,
            utterance_id=1,
        )
        await proc.process_word_event(word)
        assert proc._command_executed_in_utterance is False


# ============================================================================
# TESTS: Retraction Marker Handling
# ============================================================================

class TestRetractionMarker:
    """Retraction marker processing in SpeechProcessor."""

    @pytest.mark.asyncio
    async def test_retraction_skipped_when_command_executed(self):
        app = MockApp()
        proc = make_processor(app=app)
        proc._command_executed_in_utterance = True

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="corrected text",
        )
        await proc.process_word_event(marker)

        # No retract IPC should have been sent
        retract_calls = [a for a in app.actions if a.get("action") == "retract"]
        assert len(retract_calls) == 0

    @pytest.mark.asyncio
    async def test_retraction_sends_ipc_and_replays_on_success(self):
        app = MockApp()
        app._retract_response = {"status": "retracted", "chars": 10}
        proc = make_processor(app=app)
        proc._command_executed_in_utterance = False

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        )
        await proc.process_word_event(marker)

        # Retract IPC was sent
        retract_calls = [a for a in app.actions if a.get("action") == "retract"]
        assert len(retract_calls) == 1

        # Replay words were sent as dictation (intelligent_insert_text)
        insert_calls = [
            a for a in app.actions
            if a.get("action") == "intelligent_insert_text"
        ]
        assert len(insert_calls) == 2  # "hello" and "world"

    @pytest.mark.asyncio
    async def test_retraction_drops_final_when_not_retracted(self):
        app = MockApp()
        app._retract_response = {"status": "not_retracted", "reason": "user_interacted"}
        proc = make_processor(app=app)
        proc._command_executed_in_utterance = False

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        )
        await proc.process_word_event(marker)

        # Retract IPC was sent
        retract_calls = [a for a in app.actions if a.get("action") == "retract"]
        assert len(retract_calls) == 1

        # No replay words sent
        insert_calls = [
            a for a in app.actions
            if a.get("action") == "intelligent_insert_text"
        ]
        assert len(insert_calls) == 0

    @pytest.mark.asyncio
    async def test_retraction_cancels_active_buffer(self):
        app = MockApp()
        proc = make_processor(app=app)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc.buffer = ["delete"]
        proc.timeout_task = asyncio.create_task(asyncio.sleep(10))

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello",
        )
        await proc.process_word_event(marker)

        assert proc.mode == ProcessingMode.IDLE
        assert proc.buffer == []
        assert proc.timeout_task is None or proc.timeout_task.done()

    @pytest.mark.asyncio
    async def test_replay_first_word_has_start_of_utterance(self):
        """First replay word should have start_of_utterance=True for command detection."""
        app = MockApp()
        app._retract_response = {"status": "retracted", "chars": 10}
        proc = make_processor(app=app)

        # Track words processed by the router
        processed_words = []
        original_decide = proc.router.decide
        def tracking_decide(word_event, *args, **kwargs):
            processed_words.append(word_event)
            # Return DICTATE for simplicity
            return Decision(action=Action.DICTATE, payload=word_event.word)
        proc.router.decide = tracking_decide

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=42,
            is_retraction_marker=True,
            retraction_full_text="delete five",
        )
        await proc.process_word_event(marker)

        assert len(processed_words) == 2
        assert processed_words[0].word == "delete"
        assert processed_words[0].start_of_utterance is True
        assert processed_words[0].utterance_id == 42
        assert processed_words[1].word == "five"
        assert processed_words[1].start_of_utterance is False
        assert processed_words[1].utterance_id == 42


# ============================================================================
# TESTS: every "not_retracted" reason is terminal (no retry, no replay)
# ============================================================================
#
# The earlier wh-t81d9.1 retry on editor_unconfirmed was removed under
# wh-g2-refactor.14. Production code stopped emitting editor_unconfirmed
# (the UIActionHandler.retract path no longer returns that reason), so
# the retry was dead. Section 2 of the G2 design refinements collapses
# retract and replay into a single Qt main-thread call, which closes
# the paste-vs-ack data-loss window structurally; a retry was the
# workaround for a window that no longer exists once the G2 path
# lands.
#
# These tests now cover the post-removal contract: every reason
# terminates the retract attempt without retry, without replay, and
# without sleeping. The removed tests
# (TestEditorUnconfirmedRetry.test_retries_once_on_editor_unconfirmed_then_replays
# and ...test_no_replay_when_editor_unconfirmed_persists) exercised
# the retry path the consumer no longer takes.


class TestRetractTerminalReasons:
    """Every "not_retracted" reason is terminal (wh-g2-refactor.14)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        [
            "focus_drifted",
            "editor_focus_lost",
            "partial_send",
            "editor_stale",
            "paste_unverified",
            "user_interacted",
            "simple_paste",
            "nothing_to_retract",
            # wh-g2-refactor.14: editor_unconfirmed is now treated the
            # same as every other reason -- no retry. Production code
            # no longer emits it; the parametrised case here
            # documents that even if it were to appear (e.g. from a
            # stale plugin), the consumer would NOT retry.
            "editor_unconfirmed",
        ],
    )
    async def test_no_retry_on_terminal_reasons(self, reason):
        """All "not_retracted" reasons must NOT trigger a retry,
        and must NOT replay."""
        app = MockApp()
        app._retract_response = {"status": "not_retracted", "reason": reason}
        proc = make_processor(app=app)

        marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        )
        with patch("speech.speech_processor.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await proc.process_word_event(marker)
            mock_sleep.assert_not_called()

        retract_calls = [a for a in app.actions if a.get("action") == "retract"]
        assert len(retract_calls) == 1, (
            f"Expected no retry on reason={reason}; got {len(retract_calls)} calls"
        )

        insert_calls = [
            a for a in app.actions
            if a.get("action") == "intelligent_insert_text"
        ]
        assert len(insert_calls) == 0, (
            f"Expected no replay on reason={reason}"
        )
