"""Per-utterance word list (wh-whole-utterance-command-matching, Stage 1).

Stage 1 builds ``SpeechProcessor._words_this_utterance``: the words of
the current utterance, in spoken order, regardless of which Action the
router resolves each word to. Nothing reads the list yet -- Stage 3
moves the command-prefix loop and the cannot-match fallback onto the
utterance-end marker over this list. No behaviour change in this stage.

The trap the bead names: replacement words travel through
Action.EXECUTE, not Action.DICTATE, so a list built inside the DICTATE
branch would miss them. These tests pin the list to the WordEvent
arrival point instead:

- plain dictation words are recorded;
- words consumed by a replacement EXECUTE are recorded;
- a word that completes a HELD replacement prefix is recorded (that
  word is consumed by _resolve_pending_replacement_prefix before the
  main routing runs, so an append site placed after that pass would
  miss it);
- a new utterance resets the list;
- marker events (utterance end, timeout sentinel) append nothing;
- a retraction replay rebuilds the list with the corrected words;
- an editor-path retraction mirrors the corrected final into the list
  (the GUI replays inline, so the corrected words never re-enter
  process_word_event).
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))
sys.path.insert(0, str(test_file.parent))

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from unittest.mock import AsyncMock, MagicMock

from speech.word_event import WordEvent
from speech.speech_processor import SpeechProcessor

from test_speech_pipeline import SpeechPipelineHarness


# ============================================================================
# FIXTURES -- pipeline harness (real catalog, real router)
# ============================================================================

@pytest.fixture
async def running_harness():
    harness = SpeechPipelineHarness()
    await harness.start()
    yield harness
    await harness.stop()


# ============================================================================
# FIXTURES -- retraction processor (mocked catalog, controllable retract IPC)
# Mirrors the make_processor idiom in test_speech_processor_retraction.py.
# ============================================================================

class RetractMockApp:
    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self._retract_response: Dict[str, Any] = {
            "status": "retracted", "chars": 10,
        }

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(
        self,
        action: str,
        params: Optional[dict] = None,
        timeout_s: Optional[float] = None,
    ):
        self.actions.append({"action": action, "params": params or {}})
        if action == "retract":
            return self._retract_response
        return {"status": "ok"}


class RetractMockTextParser:
    def __init__(self):
        self.executed: List[str] = []
        self.last_executed_pattern_type: Optional[str] = "command"

    async def parse_and_execute(
        self, text, return_remainder=False, authorized_command=False
    ):
        self.executed.append(text)
        if return_remainder:
            return True, ""
        return True


def make_retraction_processor(app=None):
    app = app or RetractMockApp()
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    catalog.get_trailing_command.return_value = None
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=RetractMockTextParser(),
        app=app,
        replacement_timeout_ms=700,
        command_timeout_ms=1000,
        hotword="x-ray",
    )
    return processor


async def send_words(processor: SpeechProcessor, words: List[str]):
    for i, word in enumerate(words):
        await processor.process_word_event(WordEvent(
            word=word,
            start_of_utterance=(i == 0),
            end_of_utterance=False,
            utterance_id=1,
        ))


# ============================================================================
# TESTS -- recording at arrival
# ============================================================================

class TestWordListRecording:

    @pytest.mark.asyncio
    async def test_plain_dictation_words_recorded(self, running_harness):
        """Ordinary dictation words land in the list in spoken order."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("world", delay_before_ms=50)

        assert running_harness.processor._words_this_utterance == [
            "hello", "world",
        ]

    @pytest.mark.asyncio
    async def test_replacement_words_through_execute_recorded(
        self, running_harness
    ):
        """The bead's named trap: 'question mark' resolves through
        Action.EXECUTE (the '?' replacement), not Action.DICTATE. The
        spoken words must still be recorded."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("question", delay_before_ms=50)
        await running_harness.send_word("mark", delay_before_ms=50)

        assert running_harness.processor._words_this_utterance == [
            "hello", "question", "mark",
        ]
        # Behaviour unchanged: the replacement still executed.
        assert "?" in "".join(running_harness.get_dictation_texts())

    @pytest.mark.asyncio
    async def test_word_completing_held_replacement_prefix_recorded(
        self, running_harness
    ):
        """A held replacement prefix ('question' after its buffer timed
        out) is completed by a late 'mark'. That completing word is
        consumed by _resolve_pending_replacement_prefix, which returns
        before the main routing -- the append must run before that pass
        or the list misses the word."""
        await running_harness.send_word("question", start_of_utterance=True)
        # Replacement timeout (400ms in the harness) fires and the
        # word-hold arms instead of dictating.
        await running_harness.wait_for_timeout(500)
        assert (
            running_harness.processor._pending_replacement_prefix
            == ["question"]
        ), "test precondition: the replacement-prefix hold must be armed"

        await running_harness.send_word("mark")

        assert running_harness.processor._words_this_utterance == [
            "question", "mark",
        ]
        # The completion still types "?", but no longer on arrival.
        # wh-spaced-punctuation-names-unresolved.3 (the ruling on that
        # bead): a completed name fires its mark only when the
        # completing word ENDS its utterance. "mark" here carries
        # end_of_utterance=False, so the completed pair stays held and
        # the release deadline fires it. Wait past that deadline before
        # asking. The word-list assertion above is this test's subject
        # and is unaffected -- the append still runs before the hold
        # pass consumes the event.
        await asyncio.sleep(
            running_harness.processor.replacement_timeout_ms / 1000.0 + 0.2
        )
        assert "?" in "".join(running_harness.get_dictation_texts())

    @pytest.mark.asyncio
    async def test_new_utterance_resets_list(self, running_harness):
        """start_of_utterance begins a fresh list."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("world", delay_before_ms=50)
        await running_harness.send_word(
            "again", start_of_utterance=True, delay_before_ms=50
        )

        assert running_harness.processor._words_this_utterance == ["again"]

    @pytest.mark.asyncio
    async def test_markers_do_not_append(self, running_harness):
        """The utterance-end marker and the timeout sentinel carry
        word='' and must not append entries."""
        await running_harness.send_word(
            "hello", start_of_utterance=True, utterance_id=7
        )
        await running_harness.send_utterance_end_marker(utterance_id=7)
        # A stale timeout sentinel (token 0 matches a fresh processor
        # only; send a deliberately stale one) must also append nothing.
        await running_harness.processor.process_word_event(
            WordEvent.timeout_finalize(token=-1)
        )

        assert running_harness.processor._words_this_utterance == ["hello"]


# ============================================================================
# TESTS -- retraction rebuilds the list
# ============================================================================

class TestWordListRetraction:

    @pytest.mark.asyncio
    async def test_retraction_replay_rebuilds_list(self):
        """A successful retract replays the corrected final through
        process_word_event; the list must hold the corrected words."""
        app = RetractMockApp()
        proc = make_retraction_processor(app=app)
        await send_words(proc, ["hello", "wold"])

        await proc.process_word_event(WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        ))

        assert proc._words_this_utterance == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_editor_retraction_mirrors_final_text(self):
        """The editor-path retract replays inline in the GUI, so the
        corrected words never re-enter process_word_event. The handler
        must mirror them into the list itself."""
        proc = make_retraction_processor()
        await send_words(proc, ["hello", "wold"])
        proc._used_editor_this_utterance = True
        proc.logic_controller = MagicMock()
        proc.logic_controller.retract_editor_text = AsyncMock(
            return_value=True,
        )

        await proc.process_word_event(WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        ))

        proc.logic_controller.retract_editor_text.assert_awaited_once()
        assert proc._words_this_utterance == ["hello", "world"]

    @pytest.mark.asyncio
    async def test_editor_retraction_failed_retract_keeps_list(self):
        """Codex round-2 finding wh-whole-utterance-command-matching.1.1:
        retract_editor_text returns False when the GUI did not confirm
        the retract-and-replay (timeout, echo mismatch, any
        failure_reason such as stale_generation or replay_failed). The
        mirror must NOT record the corrected words then -- the list
        keeps the spoken words, the same outcome as the legacy path's
        failed retract, where no replay runs."""
        proc = make_retraction_processor()
        await send_words(proc, ["hello", "wold"])
        proc._used_editor_this_utterance = True
        proc.logic_controller = MagicMock()
        proc.logic_controller.retract_editor_text = AsyncMock(
            return_value=False,
        )

        await proc.process_word_event(WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=1,
            is_retraction_marker=True,
            retraction_full_text="hello world",
        ))

        proc.logic_controller.retract_editor_text.assert_awaited_once()
        assert proc._words_this_utterance == ["hello", "wold"]
