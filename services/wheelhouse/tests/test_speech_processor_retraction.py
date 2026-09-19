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
    async def test_flag_not_set_on_dictation_fallback_execution(self):
        """wh-mouse-grid.1.27: a closed-grid fallback is plain dictation.

        The bare grid words (mark / click / 1-9) match command patterns
        but fall back to dictation when the grid is closed; the parser
        records that parse as 'dictation_fallback'. It must NOT block
        retraction -- an STT revision of the typed words ('mark' ->
        'march') has to replay like any other dictation."""
        proc = make_processor()
        proc.text_parser.last_executed_pattern_type = "dictation_fallback"
        await proc._execute_command("mark")
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


# ============================================================================
# TESTS: nothing_to_retract with a buffered command replays the final
# ============================================================================
#
# wh-click-number-dictation: when every word of the utterance was held in
# the Logic-side command buffer (e.g. "click three" buffering toward
# ^click\s+(.+)$), nothing was ever pasted, so the Input process answers
# the retract IPC with nothing_to_retract. The old code treated that as
# terminal and dropped the corrected final -- a Whisper FINAL rewrite
# ("click three" -> "click 3") therefore silently killed the command
# (wheelhouse.log 2026-08-08 13:39, UTT-14/15). The Input side already
# has the mirror rule for its own letter buffer (wh-j3mgc: buffered
# letters + no paste -> report retracted so the final replays); this is
# the Logic-buffer analog. nothing_to_retract stays terminal when the
# buffer was empty, and every OTHER reason stays terminal even with a
# buffered command.


class TestBufferedCommandRetraction:
    """nothing_to_retract + a non-empty command buffer must replay."""

    def _marker(self, text="click 3"):
        return WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=7,
            is_retraction_marker=True,
            retraction_full_text=text,
        )

    def _track_router(self, proc):
        processed = []
        def tracking_decide(word_event, *args, **kwargs):
            processed.append(word_event)
            return Decision(action=Action.DICTATE, payload=word_event.word)
        proc.router.decide = tracking_decide
        return processed

    @pytest.mark.asyncio
    async def test_replays_final_when_buffer_held_words(self):
        app = MockApp()
        app._retract_response = {
            "status": "not_retracted", "reason": "nothing_to_retract",
        }
        proc = make_processor(app=app)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc.buffer = ["click", "three"]
        processed = self._track_router(proc)

        await proc.process_word_event(self._marker("click 3"))

        retract_calls = [a for a in app.actions if a.get("action") == "retract"]
        assert len(retract_calls) == 1
        assert [w.word for w in processed] == ["click", "3"], (
            "The corrected final must replay through the router when the "
            "utterance's words were held in the command buffer (nothing "
            "was ever pasted, so the screen shows none of them)."
        )
        assert processed[0].start_of_utterance is True
        assert processed[1].start_of_utterance is False

    @pytest.mark.asyncio
    async def test_no_replay_when_buffer_was_empty(self):
        """Empty buffer + nothing pasted stays terminal (old contract)."""
        app = MockApp()
        app._retract_response = {
            "status": "not_retracted", "reason": "nothing_to_retract",
        }
        proc = make_processor(app=app)
        processed = self._track_router(proc)

        await proc.process_word_event(self._marker())

        assert processed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        ["user_interacted", "focus_drifted", "simple_paste", "paste_unverified"],
    )
    async def test_other_reasons_stay_terminal_with_buffered_words(self, reason):
        """Only nothing_to_retract earns the buffered-command replay."""
        app = MockApp()
        app._retract_response = {"status": "not_retracted", "reason": reason}
        proc = make_processor(app=app)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc.buffer = ["click", "three"]
        processed = self._track_router(proc)

        await proc.process_word_event(self._marker())

        assert processed == [], f"reason={reason} must not replay"

    @pytest.mark.asyncio
    async def test_replays_buffered_words_when_final_has_fewer(self):
        """A final that SHRINKS the buffered command replays the buffer.

        Live case (wheelhouse.log 2026-08-08 14:21, UTT-21): 'click 136'
        fully buffered, the provider's final arrived as just '1'. Replaying
        the final loses the command and types a stray '1'; the buffered
        words are the fuller transcript. The word-count rule keeps the
        wanted rewrites ('click three' -> 'click 3', equal count -> final
        wins) while surviving the truncation.
        """
        app = MockApp()
        app._retract_response = {
            "status": "not_retracted", "reason": "nothing_to_retract",
        }
        proc = make_processor(app=app)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc.buffer = ["click", "136"]
        processed = self._track_router(proc)

        await proc.process_word_event(self._marker("1"))

        assert [w.word for w in processed] == ["click", "136"], (
            "The truncated final ('1') must not replace the fuller "
            "buffered command ('click 136')."
        )
        assert processed[0].start_of_utterance is True

    @pytest.mark.asyncio
    async def test_replays_final_when_word_counts_are_equal(self):
        """Equal word count trusts the final (the number-rewrite case)."""
        app = MockApp()
        app._retract_response = {
            "status": "not_retracted", "reason": "nothing_to_retract",
        }
        proc = make_processor(app=app)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc.buffer = ["click", "three"]
        processed = self._track_router(proc)

        await proc.process_word_event(self._marker("click 3"))

        assert [w.word for w in processed] == ["click", "3"]
