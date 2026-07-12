"""wh-cmd-prefix-not-split: a leading command must execute even when
dictation words follow in the same utterance.

Before this change, _resolve_finalization only tried a whole-buffer
command fullmatch. 'backspace hello world' therefore fell through to
dictation and the literal string was typed instead of one Backspace
press followed by 'hello world'. The fix searches for the LONGEST
command prefix of the buffer and returns EXECUTE with the unmatched
suffix in remainder -- the processor's existing wh-8jy machinery
executes the command and then processes the suffix (replacements
apply, the rest dictates).
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter
from speech.pattern_catalog import PatternCatalog
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def router(catalog):
    return SpeechRouter(catalog, hotword="x-ray")


class TestFinalizationCommandPrefix:
    def test_single_word_command_prefix_splits(self, router):
        # The bead's repro: 'backspace hello world' as one utterance.
        decision = router._resolve_finalization(
            ["backspace", "hello", "world"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "hello world"
        assert not decision.before_remainder

    def test_longest_command_prefix_wins(self, router):
        # 'select all' must win over any shorter interpretation.
        decision = router._resolve_finalization(
            ["select", "all", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "select all"
        assert decision.remainder == "hello"

    def test_counted_command_prefix_keeps_count(self, router):
        # ITN turns 'three' into '3'; the count belongs to the command.
        decision = router._resolve_finalization(
            ["back", "space", "3", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space 3"
        assert decision.remainder == "hello"

    def test_whole_buffer_command_unchanged(self, router):
        decision = router._resolve_finalization(
            ["backspace"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert not decision.remainder

    def test_non_command_buffer_still_dictates(self, router):
        decision = router._resolve_finalization(
            ["hello", "world"], hotword_active=False
        )
        assert decision.action == Action.DICTATE
        assert decision.payload == "hello world"

    def test_replacement_mid_buffer_unchanged(self, router):
        # wh-8jy regression guard: 'question period' has no command
        # prefix; the replacement path must keep handling it.
        decision = router._resolve_finalization(
            ["question", "period"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.before_remainder == "question"

    def test_hotword_gated_prefix_needs_hotword(self, router):
        # '^save$' requires the hotword. Without it, 'save hello' must
        # not press Ctrl+S -- it dictates, exactly as before the fix.
        decision = router._resolve_finalization(
            ["save", "hello"], hotword_active=False
        )
        assert decision.action == Action.DICTATE
        assert decision.payload == "save hello"

    def test_hotword_gated_prefix_splits_with_hotword(self, router):
        decision = router._resolve_finalization(
            ["save", "hello"], hotword_active=True
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "save"
        assert decision.remainder == "hello"

    def test_stt_punctuation_tail_discarded_not_typed(self, router):
        # wh-cmd-prefix-not-split.1.1: STT/ITN can attach punctuation to
        # the command token ('backspace,'). The matcher's punctuation-
        # retry strips it to match the command; that stripped tail is
        # STT noise between the command and the next word (the
        # wh-9f51.3 convention) and must NOT be typed into the suffix.
        decision = router._resolve_finalization(
            ["backspace,", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "hello"

    def test_command_prefix_beats_replacement_in_suffix(self, router):
        # 'backspace period': the leading command executes and the
        # replacement stays in the remainder for the processor to
        # apply, instead of the mid-buffer replacement match dictating
        # the literal word 'backspace'.
        decision = router._resolve_finalization(
            ["backspace", "period"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "period"


class TestBufferingPathSplits:
    def test_impossible_command_buffer_splits_promptly(self, router):
        # 'backspace' is buffered; 'hello' makes the command impossible.
        # The impossible-pattern path finalizes mid-utterance, so the
        # split (and the Backspace press) happens without waiting for
        # the end of the utterance.
        event = WordEvent("hello", start_of_utterance=False, end_of_utterance=False)
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["backspace"], {}
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "hello"

    def test_end_of_utterance_split(self, router):
        event = WordEvent("world", start_of_utterance=False, end_of_utterance=True)
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["backspace", "hello"], {}
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "hello world"

    def test_timeout_split(self, router):
        decision = router.decide_timeout(
            ["backspace", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "backspace"
        assert decision.remainder == "hello"


class TestPrefixLoopEfficiency:
    def test_no_command_first_word_skips_prefix_probes(self, router):
        # wh-cmd-prefix-not-split.2.1: every prefix shares the first
        # word, so when the catalog has no command pattern starting
        # with it, no prefix can match. The loop must not probe
        # len(buffer)-1 times on every dictation finalization; exactly
        # one command-typed probe (step 1, whole buffer) is allowed.
        from unittest.mock import MagicMock

        original = router.matcher.match_for_routing
        spy = MagicMock(side_effect=original)
        router.matcher.match_for_routing = spy

        decision = router._resolve_finalization(
            ["question", "alpha", "beta", "gamma", "delta"],
            hotword_active=False,
        )
        command_probes = [
            c for c in spy.call_args_list if c[0][1] == "command"
        ]
        assert len(command_probes) == 1
        assert decision.action in (Action.DICTATE, Action.EXECUTE)

    def test_numeric_validation_rejection_falls_through_to_shorter_prefix(
        self, router
    ):
        # wh-cmd-prefix-not-split.2.2: at k=3 the transformed pattern
        # back-space-(\w+)? fullmatches with 'hello' captured, but
        # numeric validation rejects it and the loop must fall through
        # to the k=2 prefix instead of losing the split.
        decision = router._resolve_finalization(
            ["back", "space", "hello", "world"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space"
        assert decision.remainder == "hello world"


class TestMidWordPunctuation:
    """wh-midword-punct-severs-count end-to-end at the router: interior
    STT punctuation must not sever a command from its count."""

    def test_interior_comma_whole_buffer_command(self, router):
        # 'back space, 3' is ONE command (3 Backspace presses), not a
        # one-press prefix plus a typed '3'.
        decision = router._resolve_finalization(
            ["back", "space,", "3"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert not decision.remainder
        # wh-midword-punct-severs-count.1.3: the whole-buffer path emits
        # the RAW payload and relies on the command engine re-matching it
        # downstream. Pin that load-bearing re-match: re-running the
        # payload through the matcher (as _execute_command does) must
        # still recover the count '3', not sever it.
        rematch = router.matcher.match_complete(
            decision.payload, pattern_type="command"
        )
        assert rematch is not None and rematch.matched
        assert rematch.group(1) == "3"

    def test_interior_comma_prefix_still_splits(self, router):
        # With dictation words after the counted command, the prefix
        # split keeps the count inside the command.
        decision = router._resolve_finalization(
            ["back", "space,", "3", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space 3"
        assert decision.remainder == "hello"


class TestStandalonePunctBeforeCount:
    """wh-midword-punct-severs-count.3.1: a standalone STT punctuation
    token between an optional-count command and its number ("delete , 3"
    from spoken "delete 3") must not let the prefix loop fire the
    countless command and dictate the number. The whole-buffer matcher
    already bails on this shape; the prefix loop must fall through to
    dictation too, not revive the spurious-command class reviewer_0
    removed at the matcher."""

    @pytest.mark.parametrize(
        "buffer",
        [
            ["delete", ",", "3"],
            ["back", "space", ",", "3"],
            ["undo", ",", "4"],
            ["redo", ",", "2"],
            # wh-midword-punct-severs-count.4.2: multiple standalone
            # punctuation tokens are all skipped before the number.
            ["delete", ",", ",", "3"],
            # Punctuation attached to the number itself is stripped
            # before the words_to_int check.
            ["delete", ",", "3,"],
            # The misspelling command variants share the same
            # ^word\s*(\d+)?$ shape and are equally vulnerable.
            ["undue", ",", "4"],
            ["redu", ",", "2"],
        ],
    )
    def test_standalone_punct_before_count_dictates(self, router, buffer):
        decision = router._resolve_finalization(buffer, hotword_active=False)
        assert decision.action == Action.DICTATE, (
            f"{buffer} must fall through to dictation, not fire a command; "
            f"got {decision.action} payload={decision.payload!r} "
            f"remainder={decision.remainder!r}"
        )

    def test_filled_count_prefix_with_trailing_punct_still_fires(self, router):
        # wh-midword-punct-severs-count.4.2: the guard only suppresses an
        # UNFILLED optional count. When the count is already filled, a
        # standalone punctuation token later in the buffer must not
        # suppress the command. 'delete 3, hello' = delete three, then
        # dictate ', hello'.
        decision = router._resolve_finalization(
            ["delete", "3", ",", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "delete 3"

    def test_homophone_number_word_suppresses_command(self, router):
        # wh-midword-punct-severs-count.4.1: words_to_int maps the
        # homophones for/to/too to numbers, so 'delete , for example' is
        # suppressed to dictation. This is the accepted, documented
        # trade-off -- it matches the system-wide count-word definition
        # ('delete for' with no comma already fires four deletes) and
        # keeps the guard consistent with the command engine.
        for tail in ("for", "to", "too"):
            decision = router._resolve_finalization(
                ["delete", ",", tail, "example"], hotword_active=False
            )
            assert decision.action == Action.DICTATE, (
                f"'delete , {tail} example' is expected to dictate per the "
                f"homophone trade-off; got {decision.action}."
            )

    def test_leading_command_then_noncount_dictation_still_fires(self, router):
        # Guard against over-rejection: a leading optional-count command
        # followed by NON-count dictation must still execute (the
        # wh-cmd-prefix-not-split contract). Only a severed COUNT -- a
        # number reachable past standalone punctuation -- triggers the
        # fall-through. 'hello' is not a count, so delete still fires.
        decision = router._resolve_finalization(
            ["delete", "hello", "world"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "delete"
        assert decision.remainder == "hello world"

    def test_number_word_count_also_severed(self, router):
        # words_to_int accepts number words, so 'delete , three' is the
        # same severed-count shape as 'delete , 3' and must dictate.
        decision = router._resolve_finalization(
            ["delete", ",", "three"], hotword_active=False
        )
        assert decision.action == Action.DICTATE
