"""End-to-end tests for trailing-position commands (wh-2vz).

A trailing-position command word appears at the END of an utterance.
The words BEFORE it are treated as dictation and inserted. The trailing
word itself is stripped from the transcription and is NOT inserted; it
fires the associated action after the dictation completes.

v1 contract:

- The trailing intercept fires when the ``is_utterance_end_marker``
  arrives (NOT on the per-word ``end_of_utterance=True`` flag, which
  the default remote-STT path never sets on real words -- the marker
  is the only end-of-utterance signal both STT paths share).
- A word that matches the trailing-command map is held back from
  dictation as a "pending candidate". When the next regular event
  arrives, the candidate is dictated as text (proving it was not the
  last word). When the end-marker arrives, the candidate's action is
  fired and the word is dropped.
- Multi-word DICTATE payloads (router buffer finalizations like
  "comma submit" producing the dictation "comma submit" via the
  replacement remainder path) are NEVER treated as trailing. Only
  single-word DICTATE payloads can become candidates.
- The "literal (.+)$" replacement still produces a real text insert
  of the trailing word, bypassing the candidate path entirely because
  EXECUTE-with-remainder runs `_process_remainder`, which calls the
  dictation IPC directly without going through the DICTATE branch
  that would capture the candidate.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import logging

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'

_TRAILING_SUBMIT = """
[[pattern]]
pattern = '''literal (.+)$'''
actions = [
    { function = "insert_text", params = ["g1"] }
]

[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""


@pytest.fixture
def trailing_patterns_path(tmp_path):
    """Minimal patterns.toml with one trailing entry and the literal escape."""
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + _TRAILING_SUBMIT, encoding="utf-8")
    return str(p)


@pytest.fixture
async def trailing_harness(trailing_patterns_path):
    harness = SpeechPipelineHarness(patterns_path=trailing_patterns_path)
    await harness.start()
    yield harness
    await harness.stop()


def _hotkey_actions(harness):
    """Filter captured outputs to the hotkey_action entries from press_keys."""
    return [
        out for out in harness.get_outputs() if out.action == "hotkey_action"
    ]


async def _send_utterance_with_end_marker(
    harness, words_with_flags, utterance_id=1,
):
    """Send a sequence of words then the is_utterance_end_marker.

    ``words_with_flags`` is a list of (word, start_of_utterance,
    end_of_utterance) tuples. The end-marker mirrors what both the
    remote and in-process STT paths produce after the final word.
    """
    for word, start, end in words_with_flags:
        await harness.send_word(
            word,
            start_of_utterance=start,
            end_of_utterance=end,
            utterance_id=utterance_id,
        )
    await harness.send_utterance_end_marker(utterance_id)


# ----------------------------------------------------------------------------
# Motivating case: "hello world submit"
# ----------------------------------------------------------------------------

class TestTrailingCommandMotivating:
    @pytest.mark.asyncio
    async def test_hello_world_submit_inserts_prefix_then_presses_enter(
        self, trailing_harness,
    ):
        """The classic case from the wh-2vz bead description."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("hello", True, False),
                ("world", False, False),
                # Remote STT path: end_of_utterance=False on real words.
                # The marker that follows is the trigger.
                ("submit", False, False),
            ],
        )
        await asyncio.sleep(0.05)

        texts = trailing_harness.get_dictation_texts()
        assert texts == ["hello", "world"], (
            f"prefix should be inserted verbatim; got {texts!r}"
        )

        hotkeys = _hotkey_actions(trailing_harness)
        assert len(hotkeys) == 1, (
            f"trailing action should fire exactly once; got {hotkeys!r}"
        )
        assert hotkeys[0].params.get("keys") == ["enter"]
        assert hotkeys[0].params.get("repeat") == 1

    @pytest.mark.asyncio
    async def test_hello_world_submit_in_process_path_same_outcome(
        self, trailing_harness,
    ):
        """In-process STT also delivers the end-marker; same outcome."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("hello", True, False),
                ("world", False, False),
                # In-process STT path: end_of_utterance=True on the
                # last real word. The trailing intercept must NOT
                # depend on this flag; the end-marker that follows is
                # the trigger.
                ("submit", False, True),
            ],
        )
        await asyncio.sleep(0.05)

        assert trailing_harness.get_dictation_texts() == ["hello", "world"]
        hotkeys = _hotkey_actions(trailing_harness)
        assert len(hotkeys) == 1
        assert hotkeys[0].params.get("keys") == ["enter"]


# ----------------------------------------------------------------------------
# Single-word "submit" utterance
# ----------------------------------------------------------------------------

class TestTrailingCommandSingleWord:
    @pytest.mark.asyncio
    async def test_lone_submit_fires_action_with_no_dictation(
        self, trailing_harness,
    ):
        """User says only "submit" -- no text is inserted, Enter fires."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [("submit", True, False)],
        )
        await asyncio.sleep(0.05)

        assert trailing_harness.get_dictation_texts() == []
        hotkeys = _hotkey_actions(trailing_harness)
        assert len(hotkeys) == 1
        assert hotkeys[0].params.get("keys") == ["enter"]


# ----------------------------------------------------------------------------
# Escape hatch: "literal submit"
# ----------------------------------------------------------------------------

class TestTrailingCommandLiteralEscape:
    @pytest.mark.asyncio
    async def test_literal_submit_inserts_word_and_does_not_press_enter(
        self, trailing_harness,
    ):
        """"literal submit" must insert "submit" as text and NOT fire Enter.

        The "literal (.+)$" pattern matches the full buffer once both
        words arrive. The router returns EXECUTE on the match, which
        runs insert_text via the action layer -- the trailing
        candidate path is never reached because the word "submit"
        never appears as a standalone DICTATE payload.
        """
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("literal", True, False),
                ("submit", False, False),
            ],
        )
        # Give the buffer's end-of-utterance finalize and the IPC round
        # trip enough time to flush.
        await asyncio.sleep(0.1)

        assert _hotkey_actions(trailing_harness) == [], (
            "literal escape must not fire Enter"
        )
        # The insert_text action emits a generic ``insert_text`` IPC, not
        # ``intelligent_insert_text``. Assert via the literal text in
        # the params instead of by action name.
        word_payloads = [
            out.params for out in trailing_harness.get_outputs()
            if out.params.get("text") == "submit"
            or out.params.get("insertion_string") == "submit"
        ]
        assert word_payloads, (
            f"the word 'submit' must be inserted as text; "
            f"outputs were {trailing_harness.get_outputs()!r}"
        )


# ----------------------------------------------------------------------------
# Negative case: "submit" mid-utterance is NOT a trailing intercept
# ----------------------------------------------------------------------------

class TestTrailingCommandMidUtteranceIgnored:
    @pytest.mark.asyncio
    async def test_submit_followed_by_more_words_is_dictated_verbatim(
        self, trailing_harness,
    ):
        """If "submit" is not the last word, it is just dictation text.

        Example: "submit your homework". The held candidate gets
        flushed as text when the next regular word arrives.
        """
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("submit", True, False),
                ("your", False, False),
                ("homework", False, False),
            ],
        )
        await asyncio.sleep(0.05)

        assert _hotkey_actions(trailing_harness) == []
        texts = trailing_harness.get_dictation_texts()
        assert texts == ["submit", "your", "homework"], (
            f"all three words should be dictated verbatim; got {texts!r}"
        )


# ----------------------------------------------------------------------------
# Retraction safety: a fired trailing action blocks retraction
# ----------------------------------------------------------------------------

class TestTrailingCommandBlocksRetraction:
    @pytest.mark.asyncio
    async def test_trailing_action_marks_command_executed_for_retraction(
        self, trailing_harness,
    ):
        """Firing the trailing action must flip the per-utterance command
        flag the retraction path reads. Without this, an STT revision of
        the final could try to retract the Enter press -- which is not
        retractable."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("hi", True, False),
                ("submit", False, False),
            ],
        )
        await asyncio.sleep(0.05)

        # The flag lives on the processor.
        assert (
            trailing_harness.processor._command_executed_in_utterance is True
        )


# ----------------------------------------------------------------------------
# Pending-candidate lifecycle: held words flush correctly
# ----------------------------------------------------------------------------

class TestPendingCandidateLifecycle:
    @pytest.mark.asyncio
    async def test_pending_cleared_after_action_fires(self, trailing_harness):
        """After end-marker consumes the candidate, the slot must be empty."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [("submit", True, False)],
        )
        await asyncio.sleep(0.05)

        assert trailing_harness.processor._pending_trailing_word is None

    @pytest.mark.asyncio
    async def test_pending_cleared_after_flush_as_text(self, trailing_harness):
        """After the candidate is dictated as text, the slot must be empty."""
        await _send_utterance_with_end_marker(
            trailing_harness,
            [
                ("submit", True, False),  # candidate held
                ("hello", False, False),  # candidate flushed -> dictate
            ],
        )
        await asyncio.sleep(0.05)

        assert trailing_harness.processor._pending_trailing_word is None
        # And the flushed candidate landed as ordinary dictation text.
        assert "submit" in trailing_harness.get_dictation_texts()


# ----------------------------------------------------------------------------
# wh-2vz.1.2 (codex round 1): stale timeout sentinel must not flush candidate
# ----------------------------------------------------------------------------

class TestTrailingCommandStaleTimeoutSentinel:
    """A stale timeout-finalize queue sentinel arriving while a trailing
    candidate is held must NOT flush the candidate as dictation.

    Before the wh-2vz.1.2 fix, the top-of-loop guard treated every
    non-end-marker event as proof the candidate was not last and flushed
    it as text. A timeout sentinel left in the queue by an earlier
    cancelled timeout would dictate "submit" as text before the
    sentinel's stale-token check could discard it -- and Enter would
    never fire.
    """

    @pytest.mark.asyncio
    async def test_stale_timeout_sentinel_preserves_held_candidate(
        self, trailing_harness,
    ):
        """A stale sentinel between the held candidate and the
        utterance_end marker must be a no-op, not a flush trigger."""
        from speech.word_event import WordEvent

        await trailing_harness.send_word(
            "hello", start_of_utterance=True, end_of_utterance=False,
        )
        await trailing_harness.send_word(
            "submit", start_of_utterance=False, end_of_utterance=False,
        )
        await asyncio.sleep(0.02)
        assert trailing_harness.processor._pending_trailing_word == "submit"

        # Inject a sentinel whose token does NOT match the current
        # generation. The processor's existing stale-token check would
        # classify this as a no-op IF the top-of-loop guard didn't fire
        # first.
        stale_token = trailing_harness.processor.timeout_token - 100
        stale_sentinel = WordEvent.timeout_finalize(token=stale_token)
        await trailing_harness.word_queue.put(stale_sentinel)
        await asyncio.sleep(0.02)

        # The candidate must still be held; the sentinel must not have
        # caused a flush.
        assert (
            trailing_harness.processor._pending_trailing_word == "submit"
        ), "stale sentinel must not flush trailing candidate"
        texts_so_far = trailing_harness.get_dictation_texts()
        assert "submit" not in texts_so_far, (
            f"stale sentinel must not dictate the held candidate; "
            f"texts={texts_so_far!r}"
        )

        # The real utterance_end marker should now fire Enter and clear
        # the slot.
        await trailing_harness.send_utterance_end_marker(
            trailing_harness._utterance_counter,
        )
        await asyncio.sleep(0.05)

        hotkeys = _hotkey_actions(trailing_harness)
        assert len(hotkeys) == 1, (
            f"trailing Enter must fire on utterance_end after the sentinel "
            f"was filtered; got {hotkeys!r}"
        )
        assert hotkeys[0].params.get("keys") == ["enter"]
        assert "submit" not in trailing_harness.get_dictation_texts(), (
            "the held candidate must not appear as dictation"
        )
        assert trailing_harness.processor._pending_trailing_word is None


# ----------------------------------------------------------------------------
# wh-797.21.2 (GLM-5.2 round 1): exception-path log must redact the word
# ----------------------------------------------------------------------------

class TestTrailingCommandExceptionLogRedaction:
    """When the trailing action raises, the logger.exception line carried
    the raw word while every sibling log line in the same helper redacts
    it. With transcript logging off (the release default) the exception
    line must carry a placeholder, never the word."""

    @pytest.mark.asyncio
    async def test_trailing_execution_exception_log_redacts_word(
        self, trailing_harness, caplog, monkeypatch,
    ):
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)

        async def _boom(*args, **kwargs):
            raise RuntimeError("action exploded")

        monkeypatch.setattr(
            trailing_harness.processor.text_parser, "_execute_rule", _boom,
        )

        with caplog.at_level(logging.ERROR):
            await _send_utterance_with_end_marker(
                trailing_harness,
                [("submit", True, False)],
            )
            await asyncio.sleep(0.05)

        raised_lines = [
            r.getMessage() for r in caplog.records
            if "Trailing command execution raised" in r.getMessage()
        ]
        assert len(raised_lines) == 1, (
            f"expected exactly one exception log line; got {raised_lines!r}"
        )
        assert "submit" not in raised_lines[0]
        assert "redacted" in raised_lines[0]
