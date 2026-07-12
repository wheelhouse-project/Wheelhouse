"""Replacement-remainder regression tests (wh-oe7u.1, wh-oe7u.2).

The shared fix rewrites SpeechProcessor._process_remainder so that:

- Commands inside a replacement remainder are dictated, not executed
  (wh-oe7u.1). The previous implementation routed remainder text through
  TextParser.parse_and_execute, which happily executed any non-hotword
  command pattern that matched. ``hello period backspace`` therefore
  fired a backspace IPC action even though ``backspace`` arrived
  mid-utterance (the truth table says mid-utterance commands are
  dictation).

- Multi-replacement remainders preserve spoken order (wh-oe7u.2). The
  previous implementation collapsed before/after into a single remainder,
  so a later-spoken replacement could execute before earlier-spoken text
  was dictated. Earliest-start selection inside the helper guarantees
  spoken order even when the catalog lists the later-spoken replacement
  earlier.
"""
import sys
from pathlib import Path
from typing import List

# Path setup mirrors test_remainder_execution_order.py so the existing
# harness imports resolve identically.
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

from test_speech_pipeline import SpeechPipelineHarness, CapturedOutput


def _action_sequence(outputs: List[CapturedOutput]) -> List[str]:
    """Flatten captured outputs into a comparable sequence.

    Encodes:
      - keypress  -> "key:<name>"
      - text  insertion -> "text:<value>"
      - other   -> "<action>:<params>"
    """
    seq = []
    for o in outputs:
        if o.action == "keypress":
            seq.append(f"key:{o.params.get('key', '?')}")
        elif o.action == "intelligent_insert_text":
            seq.append(f"text:{o.params.get('insertion_string', '')}")
        else:
            seq.append(f"{o.action}:{o.params}")
    return seq


@pytest.fixture
def harness():
    return SpeechPipelineHarness()


@pytest.fixture
async def running_harness(harness):
    await harness.start()
    yield harness
    await harness.stop()


class TestRemainderRefusesCommandExecution:
    """wh-oe7u.1: A non-hotword command inside a replacement remainder
    must be DICTATED, not executed.

    The bug: SpeechProcessor._process_remainder routed remainder text
    through TextParser.parse_and_execute(authorized_command=False).
    PatternMatcher.match_single_pattern only blocks hotword-required
    patterns when authorized_command is False; non-hotword commands
    such as ``backspace``/``delete``/``enter`` remain executable. The
    fix restricts remainder processing to replacement patterns; any
    command words in a remainder fall through to dictation.
    """

    @pytest.mark.asyncio
    async def test_hello_period_backspace_dictates_backspace(
        self, running_harness
    ):
        """``hello period backspace`` must dictate ``backspace`` after the
        period -- it must NOT fire a backspace key press."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("period", delay_before_ms=50)
        await running_harness.send_word("backspace", delay_before_ms=50)
        await running_harness.wait_for_timeout(500)

        outputs = running_harness.get_outputs()
        seq = _action_sequence(outputs)

        # No backspace IPC action should ever fire for this utterance.
        assert not any(
            o.action == "keypress" and o.params.get("key") == "backspace"
            for o in outputs
        ), (
            f"backspace key fired from a replacement remainder; the "
            f"truth table says mid-utterance command words are dictation "
            f"(wh-oe7u.1).\nActions: {seq}"
        )

        # The literal word "backspace" must appear as dictated text.
        text_outputs = [o for o in outputs if o.action == "intelligent_insert_text"]
        joined_text = " ".join(
            o.params.get("insertion_string", "") for o in text_outputs
        ).lower()
        assert "backspace" in joined_text, (
            f"backspace was suppressed instead of dictated.\n"
            f"Text outputs: {[o.params for o in text_outputs]}"
        )


class TestRemainderPreservesSpokenOrder:
    """wh-oe7u.2: Multi-replacement remainders must preserve spoken
    order even when the catalog lists the later-spoken replacement
    before the earlier-spoken one.

    Production catalog lists ``period`` before ``comma``. A buggy
    "first-by-catalog" implementation would pick the period match in
    a remainder like ``world comma friend period`` even though comma
    was spoken first. Earliest-start selection in the new helper
    guarantees spoken order.
    """

    @pytest.mark.asyncio
    async def test_hello_period_world_comma_friend_preserves_order(
        self, running_harness
    ):
        """``hello period world comma friend`` produces in order:
        hello (text), . (period), world (text), , (comma), friend (text)."""
        await running_harness.send_word("hello", start_of_utterance=True)
        await running_harness.send_word("period", delay_before_ms=50)
        await running_harness.send_word("world", delay_before_ms=50)
        await running_harness.send_word("comma", delay_before_ms=50)
        await running_harness.send_word("friend", delay_before_ms=50)
        await running_harness.wait_for_timeout(800)

        outputs = running_harness.get_outputs()
        seq = _action_sequence(outputs)

        # Find the index of each token's first emission.
        def _find_first(predicate):
            for i, s in enumerate(seq):
                if predicate(s):
                    return i
            return None

        i_hello = _find_first(lambda s: "hello" in s.lower())
        i_period = _find_first(lambda s: s.startswith("text:.") or s == "text:.")
        i_world = _find_first(lambda s: "world" in s.lower())
        i_comma = _find_first(lambda s: s.startswith("text:,") or s == "text:,")
        i_friend = _find_first(lambda s: "friend" in s.lower())

        assert i_hello is not None, f"'hello' missing from seq: {seq}"
        assert i_period is not None, f"period missing from seq: {seq}"
        assert i_world is not None, f"'world' missing from seq: {seq}"
        assert i_comma is not None, f"comma missing from seq: {seq}"
        assert i_friend is not None, f"'friend' missing from seq: {seq}"

        assert i_hello < i_period < i_world < i_comma < i_friend, (
            f"Spoken order not preserved: hello@{i_hello}, "
            f"period@{i_period}, world@{i_world}, comma@{i_comma}, "
            f"friend@{i_friend}.\nFull sequence: {seq}\n"
            f"Catalog lists period BEFORE comma; a 'first-by-catalog' "
            f"implementation would have picked period before comma "
            f"in the remainder, reordering the output (wh-oe7u.2)."
        )


class TestRemainderHelperEarliestSelection:
    """Direct unit tests on the earliest-replacement selection helper.

    The helper iterates replacement patterns in catalog order, runs
    compiled_pattern.search on the candidate text, applies validation
    via PatternMatcher.validate_numeric, skips greedy patterns, and
    selects the winner by:
      1. Lowest match.start()
      2. Longest match.end()  (multi-word > single on same start)
      3. Catalog order        (first-listed wins on identical span)
    """

    def _harness(self):
        h = SpeechPipelineHarness()
        return h

    def test_helper_skips_command_patterns(self):
        """The helper considers replacement patterns only. Even though
        ``^back ?space ...$`` would fullmatch the text ``backspace``,
        the helper must not return it."""
        h = self._harness()
        winner = h.processor._find_earliest_replacement("backspace")
        assert winner is None, (
            "Helper returned a command pattern; remainder processing "
            "must be replacement-only (wh-oe7u.1)."
        )

    def test_helper_picks_earliest_start_not_first_in_catalog(self):
        """Production catalog lists ``period`` before ``comma``.
        For text ``world comma friend period``, the earliest-start
        match is ``comma`` -- the helper must pick comma even though
        period appears first in the catalog (wh-oe7u.2)."""
        h = self._harness()
        winner = h.processor._find_earliest_replacement(
            "world comma friend period"
        )
        assert winner is not None, "no replacement matched at all"
        match, _data = winner
        # The matched span must contain comma, not period.
        matched_text = "world comma friend period"[match.start():match.end()]
        assert "comma" in matched_text.lower(), (
            f"Helper picked {matched_text!r} (catalog-first behavior). "
            f"Earliest-start selection should pick comma "
            f"(start=6) over period (start=20)."
        )

    def test_helper_returns_none_for_text_with_no_replacement(self):
        h = self._harness()
        winner = h.processor._find_earliest_replacement("just plain words")
        assert winner is None
