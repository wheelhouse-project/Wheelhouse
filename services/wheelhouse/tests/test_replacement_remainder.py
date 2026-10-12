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


class TestTheTextOnlyReplacementPass:
    """wh-itn-percent-digits.1.1: the pass the editor replay uses.

    ``SpeechProcessor._apply_allowed_text_replacements`` rewrites the
    text of a corrected final the way the word-by-word replay would,
    for the patterns named in ``_EDITOR_REPLAY_TEXT_DOC_IDS`` and for no
    others, and it runs no pattern action. It is built on the helper
    the tests above cover, which is replacement-only already.
    """

    def _processor(self):
        return SpeechPipelineHarness().processor

    def test_the_pass_runs_no_pattern_action(self):
        """A string the pass REWRITES sends nothing to the app.

        Acceptance item 2. The pattern's own action, run through the text
        parser, would send ``intelligent_insert_text`` to the app.
        """
        h = SpeechPipelineHarness()
        # The string must contain the word percent: only a string that
        # matches the listed pattern makes the loop body run, and only a
        # running body can run an action. A command word such as
        # "backspace" never reaches the body (its pattern is typed a
        # command, so the scan finds no replacement).
        result = h.processor._apply_allowed_text_replacements("50 percent")
        assert result == "50%", (
            "The pass did not reach its loop body, so this test proves "
            f"nothing about actions: {result!r}."
        )
        assert h.get_outputs() == [], (
            "The pass sent an action to the app: "
            f"{[(o.action, o.params) for o in h.get_outputs()]}"
        )

    def test_the_pass_leaves_a_command_word_alone(self):
        """A string that matches a command pattern comes back unchanged.

        ``backspace`` fullmatches a command pattern whose action presses
        a key. The pass must hand the string back as it came and send
        nothing to the app.
        """
        h = SpeechPipelineHarness()
        text = "backspace"
        result = h.processor._apply_allowed_text_replacements(text)
        assert result == text, (
            f"The pass changed a command string: {result!r}."
        )
        assert h.get_outputs() == [], (
            "The pass sent an action to the app: "
            f"{[(o.action, o.params) for o in h.get_outputs()]}"
        )

    def test_the_pass_writes_the_percent_symbol_against_its_number(self):
        proc = self._processor()
        assert proc._apply_allowed_text_replacements("50 percent") == "50%"

    def test_the_pass_leaves_a_replacement_outside_the_list_alone(self):
        """``period`` is a one-step text replacement too, and is not listed.

        Acceptance item 4: only the doc_ids in the one explicit list
        are applied.
        """
        proc = self._processor()
        text = "hello period world"
        assert proc._apply_allowed_text_replacements(text) == text

    def test_the_list_holds_exactly_the_percent_pattern(self):
        import sys
        module = sys.modules[type(self._processor()).__module__]
        assert list(module._EDITOR_REPLAY_TEXT_DOC_IDS) == ["punct-percent"]

    def test_the_scan_continues_after_the_text_it_wrote(self, monkeypatch):
        """A replacement that contains its own trigger is not rescanned.

        The percent pattern's text is changed, for this test only, to
        a string that matches the pattern again. A pass that restarted
        at the beginning of the string after each write would not
        finish; this one writes each match once and moves on. Acceptance
        item 3.
        """
        proc = self._processor()
        patterns = [
            p for p in proc.text_parser.patterns
            if p.get("doc_id") == "punct-percent"
        ]
        assert len(patterns) == 1
        monkeypatch.setitem(patterns[0], "actions", [
            {"function": "text", "params": ["x percent y"]},
        ])
        result = proc._apply_allowed_text_replacements(
            "a percent b percent c"
        )
        assert result == "a x percent y b x percent y c", (
            f"The scan did not move past the text it wrote: {result!r}"
        )

    def test_a_word_replacement_keeps_the_whitespace_in_front_of_it(
        self, monkeypatch,
    ):
        """The pass removes whitespace only in front of punctuation.

        ``PatternCatalog._merge_entries`` lets a user entry replace a
        system entry whole, so a user can give ``punct-percent`` a word
        as its replacement text. The pass must write that word where the
        match stood. It must not apply the rule that pulls punctuation
        against the word before it, because that rule first removes
        every whitespace character in front of the match.
        """
        proc = self._processor()
        patterns = [
            p for p in proc.text_parser.patterns
            if p.get("doc_id") == "punct-percent"
        ]
        assert len(patterns) == 1
        monkeypatch.setitem(patterns[0], "actions", [
            {"function": "text", "params": ["whole"]},
        ])
        result = proc._apply_allowed_text_replacements("a\n\npercent b")
        assert result == "a\n\nwhole b", (
            "a replacement that is a word must keep the whitespace in "
            f"front of it. Result: {result!r}"
        )

    def test_text_with_no_listed_trigger_never_scans_the_catalog(
        self, monkeypatch,
    ):
        """A correction with no percent word costs no catalog scan.

        wh-itn-percent-digits.1.1: the pass runs on every editor
        correction, so it returns the text at once when no listed pattern
        can match it. The catalog scan is ``_find_earliest_replacement``.
        """
        proc = self._processor()
        scans = []

        def counting_scan(text):
            scans.append(text)
            return None

        monkeypatch.setattr(proc, "_find_earliest_replacement", counting_scan)
        text = "hello period world"
        assert proc._apply_allowed_text_replacements(text) == text
        assert scans == [], (
            "The pass scanned the whole catalog for a text that holds no "
            f"word of a listed pattern: {scans!r}"
        )

    def test_a_run_of_exactly_the_bound_logs_nothing(
        self, monkeypatch, caplog,
    ):
        """Three matches under a bound of three is a complete pass."""
        import logging
        import sys
        proc = self._processor()
        module = sys.modules[type(proc).__module__]
        monkeypatch.setattr(
            module, "_TEXT_REPLACEMENT_PASS_MAX_STEPS", 3,
        )
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            result = proc._apply_allowed_text_replacements(
                "percent percent percent"
            )
        assert result == "%%%", f"The pass did not finish: {result!r}"
        lines = [
            r for r in caplog.records if r.name == module.__name__
        ]
        assert lines == [], (
            "The bound warned although no match was left: "
            f"{[r.getMessage() for r in lines]}"
        )

    def test_the_iteration_bound_stops_the_pass_and_logs_one_line(
        self, monkeypatch, caplog,
    ):
        """The bound is the second defence: the rest stays as it was."""
        import logging
        import sys
        proc = self._processor()
        module = sys.modules[type(proc).__module__]
        monkeypatch.setattr(
            module, "_TEXT_REPLACEMENT_PASS_MAX_STEPS", 3,
        )
        with caplog.at_level(logging.WARNING, logger=module.__name__):
            result = proc._apply_allowed_text_replacements(
                "percent percent percent percent percent"
            )
        assert result == "%%% percent percent", (
            f"The bound did not stop the pass after three steps: {result!r}"
        )
        lines = [
            r for r in caplog.records if r.name == module.__name__
        ]
        assert len(lines) == 1, (
            f"Expected one log line at the bound, got {len(lines)}."
        )
