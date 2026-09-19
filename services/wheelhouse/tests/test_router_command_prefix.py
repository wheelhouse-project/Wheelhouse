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
        # The vehicle for this contract was "select all hello" until
        # 2026-08-26. wh-cmd-prefix-word-loss gave ^select all$
        # whole_utterance_only, so it no longer splits at all, and
        # tests/test_router_command_prefix_word_loss.py owns that
        # behaviour now. The backspace family keeps the split -- it is the
        # motivating case of this feature -- and its optional count is the
        # one remaining place where two DIFFERENT prefixes of the same
        # buffer are both valid commands, so longest-first ordering is
        # still observable.
        #
        # The first assertion is what makes this a longest-first test
        # rather than an only-match test: "back space" on its own is a
        # complete command, so the loop had a shorter valid answer
        # available and rejected it.
        shorter = router._resolve_finalization(
            ["back", "space"], hotword_active=False
        )
        assert shorter.action == Action.EXECUTE
        assert shorter.payload == "back space"

        decision = router._resolve_finalization(
            ["back", "space", "3", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space 3"
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
        # '^fix' requires the hotword. Without it, 'fix hello' must not
        # reach the AI server -- it dictates, exactly as before the fix.
        #
        # This pair used 'save' until 2026-08-20, when every single-word
        # command traded requires_hotword for whole_utterance_only and a
        # prefix split stopped being available to it. '^fix' is the one
        # single-word command that still requires the hotword.
        decision = router._resolve_finalization(
            ["fix", "hello"], hotword_active=False
        )
        assert decision.action == Action.DICTATE
        assert decision.payload == "fix hello"

    def test_hotword_gated_prefix_splits_with_hotword(self, router):
        decision = router._resolve_finalization(
            ["fix", "hello"], hotword_active=True
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "fix"
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
    def test_impossible_command_buffer_defers_to_the_utterance_end(self, router):
        # 'backspace' is buffered; 'hello' makes the command impossible.
        # This test originally pinned the mid-utterance word-speed
        # split. wh-whole-utterance-command-matching.3 superseded that:
        # the impossible buffer now DEFERS on the fixed command timeout,
        # and the split runs at the end marker or the timeout (the two
        # tests below). Updated with David's explicit word (item 23,
        # QUESTIONS-2026-08-29.md, relayed by the boss 2026-08-30); the
        # 2026-08-18 must-pass-unmodified ruling stays in force for
        # every other test in this file.
        event = WordEvent("hello", start_of_utterance=False, end_of_utterance=False)
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["backspace"]
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000
        assert decision.reason == "Impossible buffer deferred to utterance end"

    def test_end_of_utterance_split(self, router):
        event = WordEvent("world", start_of_utterance=False, end_of_utterance=True)
        decision = router.decide(
            event, ProcessingMode.COMMAND_BUFFERING, ["backspace", "hello"]
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


# The command patterns TestStandalonePunctBeforeCount uses as vehicles.
# Every one of them must keep the prefix split. A vehicle that carries
# whole_utterance_only is stopped by the whole-utterance gate BEFORE the
# punctuation guard runs, so its rows still pass while exercising nothing.
# wh-cmd-prefix-word-loss.1.2 found five rows in exactly that state.
#
# "undu" and "redu" were vehicles here until 2026-09-03, when
# wh-number-words-one-parser.1.27 gave both whole_utterance_only so a
# two-word count would stop being severed. Every row that rode them moved
# to "back space", which has the identical ^word\s*(\d+)?$ shape, so the
# wh-midword-punct-severs-count contract is unchanged. "back space" is now
# the ONLY command that still splits, so this tuple has one entry and
# test_every_vehicle_still_splits is the whole of the protection: flag
# that pattern and this class stops testing anything.
_PUNCT_GUARD_VEHICLES = (
    r"^back ?space\s*(\d+)?$",
)


class TestStandalonePunctBeforeCount:
    """wh-midword-punct-severs-count.3.1: a standalone STT punctuation
    token between an optional-count command and its number ("delete , 3"
    from spoken "delete 3") must not let the prefix loop fire the
    countless command and dictate the number. The whole-buffer matcher
    already bails on this shape; the prefix loop must fall through to
    dictation too, not revive the spurious-command class reviewer_0
    removed at the matcher."""

    def test_every_vehicle_still_splits(self, catalog):
        """Each vehicle above must be absent from the whole_utterance_only
        sweep. This is the guard on the guard: without it, a later sweep
        can flag a vehicle and every row in this class keeps passing while
        the punctuation and count code is never entered."""
        by_raw = {
            p["raw_pattern"]: p
            for p in catalog.get_all_patterns()
            if p.get("raw_pattern") in _PUNCT_GUARD_VEHICLES
        }
        missing = sorted(set(_PUNCT_GUARD_VEHICLES) - set(by_raw))
        assert not missing, (
            f"vehicle pattern(s) no longer in the catalog: {missing}. "
            "Update _PUNCT_GUARD_VEHICLES and the rows that use them."
        )
        flagged = sorted(
            raw for raw, pat in by_raw.items() if pat.get("whole_utterance_only")
        )
        assert not flagged, (
            f"vehicle pattern(s) now whole_utterance_only: {flagged}. "
            "Their rows no longer reach the optional-count punctuation "
            "guard -- move each row to a command that still splits."
        )

    @pytest.mark.parametrize(
        "buffer",
        [
            ["back", "space", ",", "3"],
            # wh-midword-punct-severs-count.4.2: multiple standalone
            # punctuation tokens are all skipped before the number.
            ["back", "space", ",", ",", "3"],
            # Punctuation attached to the number itself is stripped
            # before the words_to_int check.
            ["back", "space", ",", "3,"],
            # The mishearing command variants share the same
            # ^word\s*(\d+)?$ shape and are equally vulnerable.
            #
            # Vehicle history, 2026-08-26. This table used "delete",
            # "undo", "redo" and "undue". wh-cmd-prefix-word-loss gave all
            # four whole_utterance_only, which stops each buffer at the
            # whole-utterance gate BEFORE this punctuation guard runs, so
            # those rows kept passing while exercising nothing. The undue
            # row went first (wh-cmd-prefix-word-loss.1.1); codex found the
            # rest of the class (wh-cmd-prefix-word-loss.1.2). The "undu"
            # and "redu" rows lasted until 2026-09-03, when
            # wh-number-words-one-parser.1.27 flagged both to stop a
            # two-word count being severed; every row now rides "back
            # space", the one command that still splits, so the
            # wh-midword-punct-severs-count contract is unchanged and the
            # guard is actually entered. The two rows those aliases
            # carried were the plain ["<cmd>", ",", "<digit>"] shape,
            # which the first row already covers.
            # test_every_vehicle_still_splits keeps it that way.
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
        # suppress the command. 'back space 3, hello' = backspace three,
        # then dictate ', hello'.
        #
        # This used 'delete' until 2026-08-26, when wh-cmd-prefix-word-loss
        # gave ^delete\s*(\d+)?$ whole_utterance_only and it stopped
        # splitting. 'back space' has the identical ^word\s*(\d+)?$ shape
        # and is one of the four patterns that keep the split, so the
        # guard's contract is unchanged -- only the vehicle moved. The
        # parametrized case above already used ["back", "space", ",", "3"].
        decision = router._resolve_finalization(
            ["back", "space", "3", ",", "hello"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space 3"

    def test_homophone_number_word_suppresses_command(self, router):
        # wh-midword-punct-severs-count.4.1: words_to_int maps the
        # homophones for/to/too to numbers, so 'undu , for example' is
        # suppressed to dictation. This is the accepted, documented
        # trade-off -- it matches the system-wide count-word definition
        # ('undu for' with no comma already fires four undos) and
        # keeps the guard consistent with the command engine.
        #
        # Vehicle moved from "delete" on 2026-08-26 per
        # wh-cmd-prefix-word-loss.1.2: "delete" became
        # whole_utterance_only, so the delete form dictated without ever
        # reaching the homophone check.
        for tail in ("for", "to", "too"):
            decision = router._resolve_finalization(
                ["back", "space", ",", tail, "example"], hotword_active=False
            )
            assert decision.action == Action.DICTATE, (
                f"'back space , {tail} example' is expected to dictate per "
                f"the homophone trade-off; got {decision.action}."
            )

    def test_leading_command_then_noncount_dictation_still_fires(self, router):
        # Guard against over-rejection: a leading optional-count command
        # followed by NON-count dictation must still execute (the
        # wh-cmd-prefix-not-split contract). Only a severed COUNT -- a
        # number reachable past standalone punctuation -- triggers the
        # fall-through. 'hello' is not a count, so the command still fires.
        #
        # Vehicle moved from 'delete' to 'back space' on 2026-08-26 for the
        # reason given in test_filled_count_prefix_with_trailing_punct_
        # still_fires above: 'delete' became whole_utterance_only under
        # wh-cmd-prefix-word-loss, so "delete hello world" now dictates in
        # full, which is the point of that bead and is asserted in
        # tests/test_router_command_prefix_word_loss.py. What this test
        # still owns is that the count guard does not over-reject.
        decision = router._resolve_finalization(
            ["back", "space", "hello", "world"], hotword_active=False
        )
        assert decision.action == Action.EXECUTE
        assert decision.payload == "back space"
        assert decision.remainder == "hello world"

    def test_number_word_count_also_severed(self, router):
        # words_to_int accepts number words, so 'undu , three' is the
        # same severed-count shape as 'undu , 3' and must dictate.
        #
        # Vehicle moved from "delete" on 2026-08-26 per
        # wh-cmd-prefix-word-loss.1.2, for the same reason as the
        # homophone test above.
        decision = router._resolve_finalization(
            ["back", "space", ",", "three"], hotword_active=False
        )
        assert decision.action == Action.DICTATE
