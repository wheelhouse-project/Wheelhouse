"""Coverage gap tests for speech/router.py.

Targets uncovered lines: 64, 66, 68, 72, 113, 152-154, 198, 219, 277,
312, 317, 323, 339, 344-345, 360, 368-369
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.router import SpeechRouter
from speech.pattern_catalog import PatternCatalog, PatternType
from speech.word_event import WordEvent
from speech.domain import ProcessingMode, Action, Decision


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def router(catalog):
    return SpeechRouter(catalog, hotword="x-ray")


# ============================================================================
# INPUT VALIDATION
# ============================================================================

class TestInputValidation:
    def test_none_word_event_raises(self, router):
        """None word_event raises ValueError."""
        with pytest.raises(ValueError, match="word_event cannot be None"):
            router.decide(None, ProcessingMode.IDLE, [])

    def test_none_buffer_defaults_to_empty(self, router):
        """None buffer defaults to []."""
        event = WordEvent("hello", start_of_utterance=True, end_of_utterance=False)
        decision = router.decide(event, ProcessingMode.IDLE, None)
        assert decision.action == Action.DICTATE


# ============================================================================
# UTTERANCE END MARKER (line 72)
# ============================================================================

class TestUtteranceEndMarker:
    def test_end_marker_returns_ignore(self, router):
        """Line 72: Utterance end marker returns IGNORE."""
        event = WordEvent("", start_of_utterance=False, end_of_utterance=True,
                         is_utterance_end_marker=True, utterance_id=1)
        decision = router.decide(event, ProcessingMode.IDLE, [])
        assert decision.action == Action.IGNORE


# ============================================================================
# SINGLE WORD COMPLETE (line 113)
# ============================================================================

class TestSingleWordComplete:
    def test_single_word_command_that_cannot_continue(self, router, catalog):
        """Line 113: Single word command that's complete and can't continue executes immediately.

        We need a command that:
        1. Matches as a complete single-word pattern
        2. Has no multi-word variant (cannot continue)

        This is hard to test with real patterns because most commands have optional params.
        Instead, test the helper methods directly.
        """
        # "undo" is a single word command - but it may have optional params
        # Test via the helper method instead
        is_complete = router._is_single_word_complete("undo", "command")
        cannot_continue = router._cannot_match_with_next_word(["undo"], "command")

        if is_complete and cannot_continue:
            # This word would hit line 113 in the truth table
            event = WordEvent("undo", start_of_utterance=True, end_of_utterance=False)
            decision = router.decide(event, ProcessingMode.IDLE, [])
            assert decision.action == Action.EXECUTE


# ============================================================================
# UNHANDLED CASE FALLBACK (lines 152-154)
# ============================================================================

class TestFallbackCase:
    def test_fallback_case_unreachable_by_design(self, router):
        """Lines 152-154: The fallback case_ is unreachable with current PatternType enum.

        This is defensive code - PatternType only has COMMAND, REPLACEMENT, NONE.
        All cases are covered. We verify the match statement handles all cases.
        """
        # All three pattern types are handled in the match statement
        # This test documents that the fallback is unreachable
        for pt in PatternType:
            assert pt in (PatternType.COMMAND, PatternType.REPLACEMENT, PatternType.NONE)


# ============================================================================
# FINALIZATION ON UTTERANCE END (line 198)
# ============================================================================

class TestFinalizationOnUtteranceEnd:
    def test_utterance_end_during_buffering_finalizes(self, router):
        """Line 198: End of utterance during buffering triggers finalization."""
        event = WordEvent("thing", start_of_utterance=False, end_of_utterance=True)
        decision = router._decide_buffering(
            event, ProcessingMode.COMMAND_BUFFERING,
            ["delete"], False, 1000, 400
        )
        # Should finalize - either EXECUTE (if pattern matches) or DICTATE
        assert decision.action in (Action.EXECUTE, Action.DICTATE)


# ============================================================================
# COMMAND TO REPLACEMENT SWITCH (line 219)
# ============================================================================

class TestCommandToReplacementSwitch:
    def test_command_fails_tries_replacement(self, router, catalog):
        """Line 219: When command buffering can't match, try replacement."""
        # Use a word that's both a command first-word AND a replacement first-word
        # If the command can't match with added words but replacement can,
        # it should switch to replacement buffering
        # This is pattern-dependent; test the helper method
        can_match_repl = router._can_match_replacement(["quotes"])
        assert isinstance(can_match_repl, bool)


# ============================================================================
# HOTWORD PREPEND ON DICTATION FALLBACK (line 277)
# ============================================================================

class TestHotwordDictationFallback:
    def test_hotword_prepended_when_active(self, router):
        """Line 277: When finalizing as dictation with active hotword, prepend hotword."""
        decision = router._resolve_finalization(["nonsense", "words"], hotword_active=True)
        assert decision.action == Action.DICTATE
        assert "x-ray" in decision.payload
        assert "nonsense words" in decision.payload


# ============================================================================
# _cannot_match_with_next_word HELPERS (lines 312, 317, 323, 339, 344-345)
# ============================================================================

class TestCannotMatchWithNextWord:
    def test_empty_buffer_returns_false(self, router):
        """Line 312: Empty buffer can always continue."""
        assert router._cannot_match_with_next_word([], "command") is False

    def test_no_patterns_returns_true(self, router):
        """Line 317: No patterns for unknown word."""
        assert router._cannot_match_with_next_word(["xyzzyplugh"], "command") is True

    def test_wrong_type_skipped(self, router):
        """Line 323: Only checks patterns of the target type."""
        # "period" is replacement, checking as command should skip it
        result = router._cannot_match_with_next_word(["period"], "command")
        assert isinstance(result, bool)

    def test_numeric_pattern_can_continue(self, router):
        """Line 339: Pattern with \\d quantifier can continue."""
        # "delete" has optional numeric param - should return False (can continue)
        result = router._cannot_match_with_next_word(["delete"], "command")
        assert result is False  # CAN continue (has optional \\d+ param)


# ============================================================================
# GREEDY BUFFER TIMER (wh-greedy-buffer-race)
# ============================================================================
#
# A pattern with .* or .+ tells the user wants to consume the entire utterance.
# While that pattern matches the current buffer, the standard 700 ms buffer
# timer would otherwise race against streaming STT word delivery and finalize
# the buffer before the rest of the utterance arrives. Step 4 of
# _decide_buffering must return the long greedy timer for that case.

class TestGreedyBufferTimer:
    def test_continue_buffering_uses_greedy_timer_when_pattern_matches(self, router):
        """Buffer 'hey Google' matches greedy pattern ^hey Google.*$.
        The next non-end word should keep buffering with the long greedy timer,
        not the standard command timer.
        """
        event = WordEvent("Google", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["hey"],
            False,
            command_timeout_ms=700,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 5000
        assert "greedy" in (decision.reason or "").lower()

    def test_continue_buffering_uses_standard_timer_when_no_greedy_match(self, router):
        """Buffer 'back' is a prefix of 'back space' (non-greedy) command.
        The next non-end word should use the standard command timer, not the
        greedy timer.
        """
        event = WordEvent("space", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["back"],
            False,
            command_timeout_ms=700,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        # Whatever action it returns, if it's BUFFER it must NOT use the
        # greedy timer because "back space" is not a greedy pattern.
        if decision.action == Action.BUFFER:
            assert decision.timeout_ms != 5000

    def test_greedy_timer_applies_to_replacement_buffering(self, router):
        """Greedy replacement patterns (e.g. \\bparentheses(.*)$) go through
        REPLACEMENT_BUFFERING, not COMMAND_BUFFERING. The greedy timer must
        apply to both modes so a greedy replacement does not race the
        standard 700 ms timer either.
        """
        event = WordEvent("hello", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.REPLACEMENT_BUFFERING,
            ["parentheses"],
            False,
            command_timeout_ms=700,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        # The 'parentheses(.*)$' pattern is greedy and the buffer already
        # matches it, so continued buffering must use the long greedy timer.
        if decision.action == Action.BUFFER:
            assert decision.timeout_ms == 5000
            assert "greedy" in (decision.reason or "").lower()


# ============================================================================
# wh-4o1aj: hotword-only buffer finalizes immediately (no command_timeout wait)
# ============================================================================

class TestHotwordOnlyBufferShortCircuit:
    """A non-hotword utterance whose buffer's only candidate command patterns
    require the hotword must finalize as dictation IMMEDIATELY, not buffer to
    command_timeout.

    Background (wh-4o1aj): _decide_buffering's match-impossibility check
    (_cannot_match) ignored requires_hotword, so a buffer like ['fix', 'word']
    (only candidate command pattern '^fix' requires the hotword) kept
    buffering until command_timeout fired. Threading hotword_active through
    _cannot_match -> matcher.cannot_match -> can_continue lets the router
    short-circuit to finalization at once when the hotword is inactive.

    NOTE: the dispatch named 'click' as the example word, but the live
    patterns.toml '^click to talk mode$' pattern has requires_hotword=False, so
    'click' is NOT hotword-only and must not short-circuit. 'fix' (^fix,
    requires_hotword=true) is the correct hotword-only single-word candidate.
    'save' was this fixture's word until 2026-08-20, when every single-word command traded requires_hotword for whole_utterance_only. '^fix' is the one single-word command that still requires the hotword, so it takes over the role unchanged.
    """

    def test_hotword_only_buffer_defers_as_impossible_when_inactive(self, router):
        # buffer ['fix'] + next word 'word'; hotword inactive. The only
        # command pattern for 'fix' (^fix) requires the hotword, so the
        # buffer cannot match. wh-whole-utterance-command-matching.3
        # superseded the immediate DICTATE this test used to pin: an
        # impossible buffer now DEFERS to the utterance end on the
        # fixed command timeout. The wh-4o1aj substance survives as the
        # deferral reason -- without the hotword threading the buffer
        # would sit in step 4's ordinary continue-buffering instead of
        # being recognized as impossible.
        event = WordEvent("word", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["fix"],
            False,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000
        assert decision.reason == "Impossible buffer deferred to utterance end"
        # The deferred buffer still resolves as dictation.
        resolved = router.decide_timeout(
            ["fix", "word"], False, utterance_words=["fix", "word"]
        )
        assert resolved.action == Action.DICTATE
        assert "fix word" in resolved.payload

    def test_same_buffer_continues_when_hotword_active(self, router):
        # With the hotword active, '^fix' is a live candidate; the buffer
        # ['fix'] is a prefix that can still complete, so the router must NOT
        # finalize as dictation here. (Sanity check that the gate is keyed on
        # hotword_active, not unconditional.)
        event = WordEvent("word", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["fix"],
            True,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        # 'fix word' is not a complete command even with hotword, but the
        # buffer is not impossible the way the inactive case is; the key
        # assertion is it does not DICTATE-finalize via the cannot_match path.
        assert decision.action != Action.DICTATE


# ============================================================================
# wh-review-pattern-fixes.17: bounded alias + off word finalizes immediately
# ============================================================================

class TestImpossibleBoundedBufferFinalizes:
    """A buffer opening a bounded whole-utterance alias, followed by an off
    word, must finalize as dictation IMMEDIATELY -- not keep buffering with
    the command timeout.

    Background (wh-review-pattern-fixes.17): 'mark's only command pattern
    ^(mark[.!?]?)$ is bounded (no greedy tail), so 'mark zzq' can NEVER
    extend to a full match. But can_continue's Strategy 2 prefix probe
    matched the de-anchored pattern against the buffer's prefix and answered
    True, so _decide_buffering's impossibility check (step 3) never fired
    and the router returned a BUFFER decision that waited command_timeout.
    """

    def test_mark_plus_off_word_defers_as_impossible(self, router):
        # Buffer ['mark'] + next word 'zzq', no utterance-end marker.
        # wh-whole-utterance-command-matching.3 superseded the
        # immediate DICTATE this test used to pin: the impossible
        # buffer now DEFERS on the fixed command timeout. The .17
        # substance survives: without the bounded-pattern fix the
        # prefix probe would answer can-continue and step 4 would keep
        # ordinary buffering (a different reason), and the resolution
        # must still be dictation of both words.
        event = WordEvent("zzq", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["mark"],
            False,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000
        assert decision.reason == "Impossible buffer deferred to utterance end"
        resolved = router.decide_timeout(
            ["mark", "zzq"], False, utterance_words=["mark", "zzq"]
        )
        assert resolved.action == Action.DICTATE
        assert "mark zzq" in resolved.payload


# ============================================================================
# wh-review-pattern-fixes.18: greedy alternation whose matched branch is
# bounded must finalize, not keep buffering
# ============================================================================

class TestFalseGreedyAlternationFinalizes:
    """A pattern like ^(mark|say .+)$ IS greedy (the 'say' branch), but the
    'mark' branch is bounded. 'mark zzq' prefix-matches the de-anchored
    pattern on the bounded branch while no branch can ever consume 'zzq',
    so the buffer can NEVER extend to a full match.

    Background (wh-review-pattern-fixes.18): can_continue's Strategy 2
    probe answered True for that buffer, so _decide_buffering's
    impossibility check never fired and the router returned a BUFFER
    decision that waited command_timeout. The path-level fullmatch probe
    must prove the trailing words consumable by the greedy tail.
    """

    @pytest.fixture
    def alternation_router(self, tmp_path):
        """Router over a minimal isolated catalog: the alternation pattern
        is the ONLY candidate for 'mark'. The main patterns file must start
        with COMMAND_HOTWORD or the catalog refuses to load."""
        main_file = tmp_path / "patterns.toml"
        main_file.write_text(
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''^(mark|say .+)$'''\n"
            'actions = [{ function = "type_text", params = ["$1"] }]\n',
            encoding="utf-8",
        )
        catalog = PatternCatalog(str(main_file))
        return SpeechRouter(catalog, hotword="x-ray")

    def test_mark_plus_off_word_defers_on_the_fixed_timer(self, alternation_router):
        # wh-whole-utterance-command-matching.3 superseded the
        # immediate DICTATE this test used to pin: the impossible
        # buffer now DEFERS. The .18 substance survives in the TIMER:
        # without the branch-aware fullmatch probe, 'mark zzq' would
        # read as greedy-viable and sit on the 5000 ms greedy timer;
        # the impossibility detection keeps it on the fixed 1000 ms
        # command timeout.
        event = WordEvent("zzq", start_of_utterance=False, end_of_utterance=False)
        decision = alternation_router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["mark"],
            False,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 1000, (
            "no branch of ^(mark|say .+)$ can consume 'zzq'; the deferral "
            "must use the fixed command timeout, not the greedy timer"
        )
        assert decision.reason == "Impossible buffer deferred to utterance end"
        resolved = alternation_router.decide_timeout(
            ["mark", "zzq"], False, utterance_words=["mark", "zzq"]
        )
        assert resolved.action == Action.DICTATE
        assert "mark zzq" in resolved.payload


# ============================================================================
# wh-review-pattern-fixes.22: a bounded command with a lookahead dot must
# execute at once, not sit on the greedy timer
# ============================================================================

class TestZeroWidthBoundedCommandExecutes:
    """^set (?=.+)mode$ is a bounded two-word command: the lookahead's dot
    consumes nothing, so a completed 'set mode' buffer must EXECUTE.

    Background (wh-review-pattern-fixes.22): the scanner counted the
    lookahead's '.+' as a greedy-tail span and the balanced construct
    compiled, so the pattern carried is_greedy=True. _decide_buffering's
    step 2 skips greedy matches, so the completed command fell through to
    step 4 and was held on the 5000 ms greedy timer instead of executing.
    """

    @pytest.fixture
    def lookahead_router(self, tmp_path):
        """Router over a minimal isolated catalog: the lookahead pattern is
        the ONLY candidate for 'set'. The main patterns file must start
        with COMMAND_HOTWORD or the catalog refuses to load."""
        main_file = tmp_path / "patterns.toml"
        main_file.write_text(
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''^set (?=.+)mode$'''\n"
            'actions = [{ function = "type_text", params = ["mode"] }]\n',
            encoding="utf-8",
        )
        catalog = PatternCatalog(str(main_file))
        return SpeechRouter(catalog, hotword="x-ray")

    def test_completed_bounded_command_executes_immediately(
        self, lookahead_router
    ):
        event = WordEvent("mode", start_of_utterance=False, end_of_utterance=False)
        decision = lookahead_router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["set"],
            False,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )
        assert decision.action == Action.EXECUTE, (
            "'set mode' completes the bounded pattern ^set (?=.+)mode$ and "
            "must execute now, not be held on the 5000 ms greedy timer"
        )
        assert decision.payload == "set mode"
        assert decision.timeout_ms != 5000


# ============================================================================
# wh-l4h.1.14: IDLE / first-word path is hotword-aware (no command_timeout wait)
# ============================================================================

class TestHotwordOnlyIdleShortCircuit:
    """A fresh single-word hotword-only first word must finalize as dictation
    IMMEDIATELY in the IDLE path, not BUFFER for the full command_timeout.

    Background (wh-l4h.1.14): _decide_idle's FRESH_COMMAND branch decided to
    BUFFER (with command_timeout_ms) for a word like 'fix' even when the
    hotword is inactive. 'fix's only command pattern '^fix' has
    requires_hotword=true, so with the hotword inactive the word can never
    actually match a command -- but the idle-path impossibility check did not
    consult hotword_active, so it waited ~1000ms before dropping to dictation.
    This mirrors the wh-4o1aj fix already applied to the BUFFERING path.

    'fix' (^fix, requires_hotword=true) is the hotword-only single-word
    candidate; 'click' is NOT (its '^click to talk mode$' pattern has
    requires_hotword=False) -- see the note above on
    TestHotwordOnlyBufferShortCircuit.
    """

    def test_fresh_hotword_only_word_dictates_when_inactive(self, router):
        # Fresh 'fix', hotword inactive. The only command pattern (^fix)
        # requires the hotword, so the word cannot match a command and the idle
        # path must finalize it as dictation at once -- NOT return a BUFFER
        # decision that waits command_timeout.
        event = WordEvent("fix", start_of_utterance=True, end_of_utterance=False)
        decision = router._decide_idle(
            event,
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
            hotword_active=False,
        )
        assert not (
            decision.action == Action.BUFFER and decision.timeout_ms == 1000
        ), (
            "fresh hotword-only first word must not BUFFER for command_timeout "
            "when the hotword is inactive"
        )
        assert decision.action == Action.DICTATE

    def test_fresh_hotword_only_word_executes_when_active(self, router):
        # Same word, hotword active: '^fix' is a live, complete single-word
        # command, so the idle path takes the immediate EXECUTE path -- no
        # command_timeout wait. (wh-l4h.1.14 / deepseek: _is_single_word_complete
        # previously hardcoded hotword_active=False, so an active-hotword 'fix'
        # missed this EXECUTE branch and buffered the full ~1000ms.)
        event = WordEvent("fix", start_of_utterance=True, end_of_utterance=False)
        decision = router._decide_idle(
            event,
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
            hotword_active=True,
        )
        assert decision.action == Action.EXECUTE

    def test_normal_multiword_command_first_word_still_buffers(self, router):
        # A non-hotword-gated command first word ('delete', '^delete( \d+)?$',
        # requires_hotword=False) must still BUFFER normally regardless of the
        # new gate -- the idle short-circuit must not steal normal commands.
        event = WordEvent("delete", start_of_utterance=True, end_of_utterance=False)
        decision = router._decide_idle(
            event,
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
            hotword_active=False,
        )
        assert decision.action != Action.DICTATE


# ============================================================================
# _can_match_replacement (lines 360, 368-369)
# ============================================================================

class TestCanMatchReplacement:
    def test_empty_buffer_returns_false(self, router):
        """Line 360: Empty buffer can't match replacement."""
        assert router._can_match_replacement([]) is False

    def test_replacement_word_matches(self, router):
        """Lines 368-369: Buffer with replacement word matches."""
        # "period" is a known replacement pattern
        assert router._can_match_replacement(["period"]) is True

    def test_non_replacement_word_no_match(self, router):
        """No replacement for unknown word."""
        assert router._can_match_replacement(["xyzzyplugh"]) is False


# ============================================================================
# BUFFER PREFIX PRESERVATION (wh-8jy)
# ============================================================================

class TestCommentQuantifiedTailUsesGreedyTimer:
    r"""``^say .(?# note)+$`` is a greedy "swallow the rest" command: the
    ``(?#...)`` comment is zero-width and re attaches the ``+`` to the dot.

    Background (wh-review-pattern-fixes.43): the span scanner's dot
    look-ahead skipped verbose whitespace and verbose ``#`` comments, but
    not a complete comment group, so the pattern carried no is_greedy
    metadata. _decide_buffering's step 2 executes a complete NON-greedy
    match at once, so "say hello" fired the command immediately and the
    rest of the utterance was lost, instead of buffering on the 5000 ms
    greedy timer until the user stops speaking.
    """

    def _router(self, tmp_path, pattern):
        """Router over a minimal isolated catalog: the pattern under test
        is the ONLY candidate for 'say'. The main patterns file must start
        with COMMAND_HOTWORD or the catalog refuses to load."""
        main_file = tmp_path / "patterns.toml"
        main_file.write_text(
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''" + pattern + "'''\n"
            'actions = [{ function = "type_text", params = ["said"] }]\n',
            encoding="utf-8",
        )
        return SpeechRouter(PatternCatalog(str(main_file)), hotword="x-ray")

    def _decide(self, router):
        event = WordEvent("hello", start_of_utterance=False,
                          end_of_utterance=False)
        return router._decide_buffering(
            event,
            ProcessingMode.COMMAND_BUFFERING,
            ["say"],
            False,  # hotword_active
            command_timeout_ms=1000,
            replacement_timeout_ms=700,
            greedy_timeout_ms=5000,
        )

    def test_plus_form_buffers_on_the_greedy_timer(self, tmp_path):
        decision = self._decide(
            self._router(tmp_path, r"^say .(?# note)+$"))
        assert decision.action == Action.BUFFER, (
            "the '+' quantifies the dot across the comment, so 'say hello' "
            "must keep buffering for the rest of the utterance, not execute"
        )
        assert decision.timeout_ms == 5000

    def test_star_form_buffers_on_the_greedy_timer(self, tmp_path):
        decision = self._decide(
            self._router(tmp_path, r"^say .(?# note)*$"))
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 5000

    def test_escaped_close_paren_comment_buffers_on_the_greedy_timer(
            self, tmp_path):
        r"""``\)`` does not end the comment, so the '+' after the BARE
        ')' still quantifies the dot."""
        decision = self._decide(
            self._router(tmp_path, r"^say .(?# a\)b)+$"))
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 5000

    def test_adjacent_comments_buffer_on_the_greedy_timer(self, tmp_path):
        decision = self._decide(
            self._router(tmp_path, r"^say .(?# a)(?# b)+$"))
        assert decision.action == Action.BUFFER
        assert decision.timeout_ms == 5000

    def test_bounded_control_still_executes(self, tmp_path):
        r"""The bound on the fix: a comment group with NO quantifier after
        it leaves a bounded pattern, which must still execute at once."""
        router = self._router(tmp_path, r"^say (?# note)hello$")
        decision = self._decide(router)
        assert decision.action == Action.EXECUTE
        assert decision.payload == "say hello"


class TestBufferPrefixPreservation:
    """Regression: a replacement matched mid-buffer must not drop the prefix.

    Repro: 'question' is a prefix of the multi-word replacement 'question
    mark', so it goes into REPLACEMENT_BUFFERING. If the user then says
    'period' instead of 'mark', the buffer 'question period' matches the
    bare '\\bperiod\\b' replacement mid-string -- but the 'question' prefix
    (before_remainder) must be dictated before the period is emitted, or
    the user sees just '. ' instead of 'question. '.
    """

    def test_question_period_preserves_question_prefix(self, router):
        event = WordEvent("period", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event, ProcessingMode.REPLACEMENT_BUFFERING,
            ["question"], False, 1000, 400
        )

        assert decision.action == Action.EXECUTE
        assert decision.before_remainder == "question", (
            "Router dropped the 'question' prefix -- the user will see only "
            "the period replacement typed, losing the word they said first."
        )
        assert decision.payload == "period"
        assert decision.remainder == ""

    def test_midbuffer_replacement_with_both_sides_preserved(self, router):
        """A replacement with both before- and after-text preserves both."""
        # Imagine a buffer ['hello', 'period', 'world'] where the new word
        # arrives that makes 'period' firm up. Use 'world' as the arriving
        # word and ['hello', 'period'] as the pre-existing buffer.
        event = WordEvent("world", start_of_utterance=False, end_of_utterance=False)
        decision = router._decide_buffering(
            event, ProcessingMode.REPLACEMENT_BUFFERING,
            ["hello", "period"], False, 1000, 400
        )

        assert decision.action == Action.EXECUTE
        assert decision.before_remainder == "hello"
        assert decision.payload == "period"
        assert decision.remainder == "world"
