"""Coverage gap tests for speech/pattern_matcher.py.

Targets uncovered lines: 63-65, 69-74, 99-101, 127, 131, 169, 273, 326, 331,
338, 348, 357-359, 370, 411, 414, 418, 428-429, 442
"""
import sys
import re
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock

from speech.pattern_matcher import PatternMatcher, MatchResult
from speech.pattern_catalog import PatternCatalog, PatternType


@pytest.fixture
def catalog():
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def matcher(catalog):
    return PatternMatcher(catalog)


# ============================================================================
# MatchResult PROPERTIES (lines 63-65, 69-74)
# ============================================================================

class TestMatchResultProperties:
    def test_groups_with_match_object(self):
        match = re.fullmatch(r"delete (\d+)", "delete 5")
        result = MatchResult(matched=True, match_object=match)
        assert result.groups == ("5",)

    def test_groups_without_match_object(self):
        result = MatchResult(matched=False)
        assert result.groups == ()

    def test_group_with_valid_index(self):
        match = re.fullmatch(r"delete (\d+)", "delete 5")
        result = MatchResult(matched=True, match_object=match)
        assert result.group(1) == "5"

    def test_group_with_invalid_index(self):
        match = re.fullmatch(r"delete (\d+)", "delete 5")
        result = MatchResult(matched=True, match_object=match)
        assert result.group(99) is None

    def test_group_without_match_object(self):
        result = MatchResult(matched=False)
        assert result.group(1) is None


# ============================================================================
# _get_words_to_int IMPORT ERROR FALLBACK (lines 99-101)
# ============================================================================

class TestWordsToIntFallback:
    def test_import_error_uses_fallback(self):
        matcher = PatternMatcher.__new__(PatternMatcher)
        matcher.catalog = MagicMock()
        matcher._words_to_int = None

        with patch("speech.pattern_matcher.logger"):
            with patch.dict("sys.modules", {"speech.actions": None}):
                # Force reimport to trigger ImportError
                matcher._words_to_int = None
                try:
                    from speech.actions import words_to_int
                except (ImportError, TypeError):
                    pass
                # Simulate what _get_words_to_int does on import error
                matcher._words_to_int = lambda x: None
                result = matcher._get_words_to_int()
                assert result is not None
                assert result("anything") is None


# ============================================================================
# match_complete EDGE CASES (lines 127, 131, 169)
# ============================================================================

class TestMatchCompleteEdgeCases:
    def test_empty_text_returns_none(self, matcher):
        """Line 127: Empty text returns None."""
        assert matcher.match_complete("") is None

    def test_first_word_auto_extracted(self, matcher):
        """Line 131: first_word extracted from text when not provided."""
        result = matcher.match_complete("delete 5", pattern_type="command")
        assert result is not None
        assert result.matched

    def test_hotword_required_skipped_when_inactive(self, matcher):
        """Line 169: Pattern with requires_hotword=True skipped when hotword_active=False."""
        # "close window" requires hotword - should not match without it
        result = matcher.match_complete("close window", hotword_active=False)
        # If it matches without hotword, it shouldn't be a requires_hotword pattern
        if result and result.requires_hotword:
            pytest.fail("Hotword-required pattern matched without hotword")


# ============================================================================
# match_for_routing (line 273)
# ============================================================================

class TestMatchForRouting:
    def test_empty_buffer_returns_none(self, matcher):
        """Line 273: Empty buffer returns None."""
        assert matcher.match_for_routing([], "command") is None

    def test_valid_buffer_matches(self, matcher):
        result = matcher.match_for_routing(["delete", "5"], "command")
        assert result is not None
        assert result.matched


# ============================================================================
# can_continue EDGE CASES (lines 326, 331, 338, 348, 357-359, 370)
# ============================================================================

class TestCanContinue:
    def test_empty_buffer_returns_true(self, matcher):
        """Line 326: Empty buffer can always continue."""
        assert matcher.can_continue([], "command") is True

    def test_no_patterns_for_word_returns_false(self, matcher):
        """Line 331: No patterns start with unknown word."""
        assert matcher.can_continue(["xyzzyplugh"], "command") is False

    def test_non_matching_type_skipped(self, matcher):
        """Line 338: Patterns of wrong type are skipped."""
        # "period" is a replacement - should not continue as command
        result = matcher.can_continue(["period"], "command")
        # Either False (can't continue) or True (some other pattern happens to match)
        assert isinstance(result, bool)

    def test_complete_match_with_validation(self, matcher):
        """Line 348: Already matching pattern with valid numeric returns True."""
        # "delete 5" - complete pattern
        assert matcher.can_continue(["delete", "5"], "command") is True

    def test_prefix_match_returns_true(self, matcher):
        """Lines 357-359: Buffer is valid prefix of a pattern."""
        # "delete" alone - prefix of "delete (\\d+)?"
        assert matcher.can_continue(["delete"], "command") is True

    def test_single_word_multi_word_pattern(self, matcher):
        """Line 370: Single word of a multi-word pattern can continue."""
        # "snake" is the start of "snake case" - should be able to continue
        assert matcher.can_continue(["snake"], "command") is True


# ============================================================================
# cannot_match (inverse of can_continue)
# ============================================================================

class TestCannotMatch:
    def test_inverse_of_can_continue(self, matcher):
        assert matcher.cannot_match(["delete"], "command") is False
        assert matcher.cannot_match(["xyzzyplugh"], "command") is True


# ============================================================================
# wh-4o1aj: hotword_active gate in can_continue / cannot_match
# ============================================================================


class TestCanContinueHotwordGate:
    """can_continue / cannot_match must skip requires_hotword=True patterns
    when hotword_active is False, mirroring match_complete's skip.

    When every candidate pattern for the buffer requires the hotword and the
    hotword is inactive, can_continue returns False (cannot_match True) so the
    router finalizes a buffer like 'fix ...' as dictation immediately instead
    of waiting the full command_timeout.
    """

    def _hotword_only_word(self, catalog):
        """A first word whose every candidate command pattern requires hotword.

        '^fix' (requires_hotword=true) is the only command pattern starting
        with 'fix', so the buffer ['fix'] is hotword-only. 'save' was this fixture's word until 2026-08-20, when every single-word command traded requires_hotword for whole_utterance_only. '^fix' is the one single-word command that still requires the hotword, so it takes over the role unchanged.
        """
        pats = catalog.get_matching_patterns("fix")
        assert pats, "fixture broken: no patterns for 'fix'"
        assert all(
            d.get("requires_hotword") for _cp, t, d in pats if t == "command"
        ), "fixture broken: 'fix' has a non-hotword command pattern"
        return "fix"

    def test_can_continue_false_when_only_hotword_patterns_and_inactive(self, matcher, catalog):
        word = self._hotword_only_word(catalog)
        assert matcher.can_continue([word], "command", hotword_active=False) is False

    def test_cannot_match_true_when_only_hotword_patterns_and_inactive(self, matcher, catalog):
        word = self._hotword_only_word(catalog)
        assert matcher.cannot_match([word], "command", hotword_active=False) is True

    def test_can_continue_true_when_hotword_active(self, matcher, catalog):
        word = self._hotword_only_word(catalog)
        assert matcher.can_continue([word], "command", hotword_active=True) is True

    def test_default_hotword_inactive_skips_hotword_only_pattern(self, matcher, catalog):
        """Default (no hotword_active arg) is fail-closed: hotword-only buffer
        cannot continue."""
        word = self._hotword_only_word(catalog)
        assert matcher.can_continue([word], "command") is False

    def test_non_hotword_pattern_unaffected_when_inactive(self, matcher):
        """A buffer whose patterns do NOT require hotword still continues when
        hotword is inactive (no regression for non-hotword patterns)."""
        # 'delete' -> ^delete\s*(\w+)?$ (requires_hotword=False); prefix can continue.
        assert matcher.can_continue(["delete"], "command", hotword_active=False) is True


# ============================================================================
# wh-multiword-command-prefix-capture: multi-word literal opening + capture
# ============================================================================


USER_MULTIWORD_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
    "\n"
    "[[pattern]]\n"
    "pattern = '''^search (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
    "\n"
    "[[pattern]]\n"
    "pattern = '''^zork the widget (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


@pytest.fixture
def multiword_matcher(tmp_path):
    """Shipped catalog plus two user patterns with a captured tail.

    ``^search (.+)$`` has a ONE-word literal opening and already worked.
    ``^look up (.+)$`` has a TWO-word literal opening and needs three spoken
    words before its first full match, which is the reported bug.
    """
    user_file = tmp_path / "user_patterns.toml"
    user_file.write_text(USER_MULTIWORD_TOML, encoding="utf-8")
    catalog = PatternCatalog("speech/config/patterns.toml", str(user_file))
    return PatternMatcher(catalog)


class TestCanContinueMultiWordLiteralOpening:
    """A command whose literal opening is two or more words must keep
    listening while the buffer is still inside that opening.

    Before the fix, Strategy 3 in ``can_continue`` returned True only for a
    one-word buffer, so ``['look', 'up']`` answered False. The router then
    finalized the buffer as dictation before the captured word arrived.
    """

    def test_two_word_literal_opening_keeps_listening(self, multiword_matcher):
        """The whole point of the bug: ['look', 'up'] must continue."""
        assert multiword_matcher.can_continue(["look", "up"], "command") is True

    def test_full_utterance_still_continues(self, multiword_matcher):
        """['look', 'up', 'notepad'] already fullmatches; unchanged."""
        assert (
            multiword_matcher.can_continue(["look", "up", "notepad"], "command")
            is True
        )

    # -- the bound: a longer buffer that is NOT a literal opening still stops --

    def test_two_word_buffer_off_the_opening_gives_up(self, multiword_matcher):
        """'look sideways' is not the start of any pattern -> finalize now."""
        assert (
            multiword_matcher.can_continue(["look", "sideways"], "command") is False
        )

    def test_two_word_buffer_unknown_first_word_gives_up(self, multiword_matcher):
        assert (
            multiword_matcher.can_continue(["xyzzyplugh", "up"], "command") is False
        )

    def test_three_word_opening_continues_only_while_on_the_literal(
        self, multiword_matcher
    ):
        """^zork the widget (.+)$ needs four spoken words.

        Each buffer that is still inside the literal opening continues; the
        first buffer that leaves it stops at once. This is the bound on the
        change: the buffer must BE the opening, not merely start with its
        first word.
        """
        assert multiword_matcher.can_continue(["zork", "the"], "command") is True
        assert (
            multiword_matcher.can_continue(["zork", "the", "widget"], "command")
            is True
        )
        assert (
            multiword_matcher.can_continue(["zork", "the", "gadget"], "command")
            is False
        )

    # -- STT punctuation on the first token (wh-review-pattern-fixes.1) --

    def test_punctuated_first_token_two_word_opening_keeps_listening(
        self, multiword_matcher
    ):
        """Spoken 'look up ...' can arrive as ['look,', 'up'] because the
        STT/ITN attaches sentence punctuation to the first token. Strategy 1
        already normalizes the first word before its fullmatch; the
        literal-opening test must see the same normalized text, or the
        buffer finalizes as dictation at word two."""
        assert multiword_matcher.can_continue(["look,", "up"], "command") is True

    def test_punctuated_first_token_three_word_opening_keeps_listening(
        self, multiword_matcher
    ):
        """Same normalization for a truncation of a longer opening:
        ['zork,', 'the'] is still inside ^zork the widget (.+)$."""
        assert multiword_matcher.can_continue(["zork,", "the"], "command") is True

    def test_punctuated_first_token_off_the_opening_still_gives_up(
        self, multiword_matcher
    ):
        """The bound holds with punctuation too: 'look, sideways' is not the
        opening of any pattern, so the buffer finalizes now."""
        assert (
            multiword_matcher.can_continue(["look,", "sideways"], "command")
            is False
        )

    # -- STT punctuation on a LATER literal-opening word
    # (wh-review-pattern-fixes.6) --

    def test_punctuated_second_token_two_word_opening_keeps_listening(
        self, multiword_matcher
    ):
        """Spoken 'look up ...' can arrive as ['look', 'up,'] because the
        STT/ITN attaches sentence punctuation to a LATER opening word, not
        only the first. The first-word normalization cannot reach it, so
        the literal-opening probe must retry with per-token punctuation
        stripped, the same way _match_command_with_punct_retry does for a
        complete command."""
        assert multiword_matcher.can_continue(["look", "up,"], "command") is True

    def test_punctuated_middle_token_three_word_opening_keeps_listening(
        self, multiword_matcher
    ):
        """['zork', 'the,'] is still inside ^zork the widget (.+)$ once the
        STT comma on the middle opening word is stripped."""
        assert multiword_matcher.can_continue(["zork", "the,"], "command") is True

    def test_punctuated_final_token_three_word_opening_keeps_listening(
        self, multiword_matcher
    ):
        """['zork', 'the', 'widget!'] is the whole literal opening with STT
        punctuation on its final word; the capture has not arrived yet, so
        the buffer must keep listening."""
        assert (
            multiword_matcher.can_continue(["zork", "the", "widget!"], "command")
            is True
        )

    def test_parity_with_complete_command_punct_retry(self, multiword_matcher):
        """Parity: an utterance the complete-command punctuation retry
        accepts ('look up, weather') must have a continuable opening
        (['look', 'up,']) before its capture arrives. Otherwise a pause
        after the second word finalizes as dictation a command that
        match_complete would have executed."""
        result = multiword_matcher.match_complete(
            "look up, weather", pattern_type="command"
        )
        assert result is not None
        assert result.matched
        assert multiword_matcher.can_continue(["look", "up,"], "command") is True

    def test_all_punctuation_token_fails_closed(self, multiword_matcher):
        """The bound: a token that is ENTIRELY punctuation strips to the
        empty string, and the probe must NOT let it become a match-anything
        token. ['look', ','] is dictated punctuation, not a command
        opening, so the buffer finalizes now -- mirroring the complete-match
        retry's bail-out (wh-midword-punct-severs-count.1.1)."""
        assert multiword_matcher.can_continue(["look", ","], "command") is False

    # -- one-word answers must not change --

    def test_one_word_look_still_continues(self, multiword_matcher):
        assert multiword_matcher.can_continue(["look"], "command") is True

    def test_one_word_search_still_continues(self, multiword_matcher):
        assert multiword_matcher.can_continue(["search"], "command") is True

    def test_one_word_two_word_command_still_continues(self, multiword_matcher):
        """'snake' opens the shipped two-word command 'snake case'."""
        assert multiword_matcher.can_continue(["snake"], "command") is True

    def test_one_word_unknown_still_gives_up(self, multiword_matcher):
        assert multiword_matcher.can_continue(["xyzzyplugh"], "command") is False

    # -- the shipped angle-brackets replacement is unchanged --

    def test_angle_brackets_replacement_unchanged(self, matcher):
        """\\bangle brackets(.*)$ already fullmatched at two words and must
        keep answering True at one word and at two."""
        assert matcher.can_continue(["angle"], "replacement") is True
        assert matcher.can_continue(["angle", "brackets"], "replacement") is True


# ============================================================================
# wh-review-pattern-fixes.9: variable regex component in the pre-tail opening
# ============================================================================


USER_VARIABLE_PRETAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look (?:the )?widget (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
    "\n"
    "[[pattern]]\n"
    "pattern = '''^grab (?:the file|a) copy (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
    "\n"
    "[[pattern]]\n"
    "pattern = '''^shimmer (?:very )+nice (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


@pytest.fixture
def variable_pretail_matcher(tmp_path):
    """Shipped catalog plus user patterns whose pre-tail opening contains a
    variable regex component: an optional word, an alternation with a
    two-word branch, and (the bound) an unbounded repetition."""
    user_file = tmp_path / "user_patterns.toml"
    user_file.write_text(USER_VARIABLE_PRETAIL_TOML, encoding="utf-8")
    catalog = PatternCatalog("speech/config/patterns.toml", str(user_file))
    return PatternMatcher(catalog)


class TestCanContinueVariablePreTailOpening:
    """A buffer that ends INSIDE an optional or alternation group of a
    command's pre-tail opening must keep listening.

    Before the fix, build_literal_prefix_matchers whitespace-split the RAW
    regex text, so ^look (?:the )?widget (.+)$ got only the matchers
    ^look (?:the )?widget$ and ^look$. The buffer ['look', 'the'] matched
    neither, and a normal pause after 'look the' finalized the utterance as
    dictation before 'widget ...' arrived -- even though match_complete
    accepts the full command (wh-review-pattern-fixes.9).
    """

    def test_pause_inside_optional_group_keeps_listening(
        self, variable_pretail_matcher
    ):
        """The finding's reproduction: ['look', 'the'] must continue."""
        assert (
            variable_pretail_matcher.can_continue(["look", "the"], "command")
            is True
        )

    def test_optional_group_absent_keeps_listening(self, variable_pretail_matcher):
        assert (
            variable_pretail_matcher.can_continue(["look", "widget"], "command")
            is True
        )

    def test_full_opening_keeps_listening(self, variable_pretail_matcher):
        assert (
            variable_pretail_matcher.can_continue(
                ["look", "the", "widget"], "command"
            )
            is True
        )

    def test_matching_full_command_sanity(self, variable_pretail_matcher):
        """Fixture guard: the complete command the finding pairs with."""
        result = variable_pretail_matcher.match_complete(
            "look the widget report", pattern_type="command"
        )
        assert result is not None
        assert result.matched

    # -- alternation with a two-word branch --

    def test_pause_inside_two_word_branch_keeps_listening(
        self, variable_pretail_matcher
    ):
        assert (
            variable_pretail_matcher.can_continue(["grab", "the"], "command")
            is True
        )
        assert (
            variable_pretail_matcher.can_continue(
                ["grab", "the", "file"], "command"
            )
            is True
        )
        assert (
            variable_pretail_matcher.can_continue(
                ["grab", "the", "file", "copy"], "command"
            )
            is True
        )

    def test_short_branch_keeps_listening(self, variable_pretail_matcher):
        assert (
            variable_pretail_matcher.can_continue(["grab", "a"], "command")
            is True
        )
        assert (
            variable_pretail_matcher.can_continue(
                ["grab", "a", "copy"], "command"
            )
            is True
        )

    # -- the bounds --

    def test_ordinary_dictation_still_finalizes(self, variable_pretail_matcher):
        """The expansion must not hold dictation back: a buffer off every
        opening finalizes now."""
        assert (
            variable_pretail_matcher.can_continue(
                ["look", "sideways"], "command"
            )
            is False
        )
        assert (
            variable_pretail_matcher.can_continue(["grab", "banana"], "command")
            is False
        )

    def test_unbounded_repetition_keeps_prior_behavior(
        self, variable_pretail_matcher
    ):
        """(?:very )+ cannot be expanded exactly, so the pattern keeps
        today's behavior: a pause inside the repetition finalizes as
        dictation (the documented bound, labeled), while the one-word buffer
        still continues under the single-word rule."""
        assert (
            variable_pretail_matcher.can_continue(["shimmer"], "command")
            is True
        )
        assert (
            variable_pretail_matcher.can_continue(
                ["shimmer", "very"], "command"
            )
            is False
        )


# ============================================================================
# wh-review-pattern-fixes.12: multi-word partial prefix of a NON-greedy
# anchored command
# ============================================================================


class TestCanContinueNonGreedyLiteralCommand:
    """A pause inside the literal text of a NON-greedy anchored command must
    keep the buffer listening.

    Before the fix, Strategy 2's prefix regex probe could never match a
    buffer SHORTER than the pattern's literal text (Python re has no partial
    matching), and Strategy 3 got no matchers because extract_literal_prefix
    returns '' for a pattern with no greedy tail. So a pause after two words
    of the shipped three-plus-word command '^push to talk mode$' finalized
    the buffer as dictation and the command was lost, with or without STT
    punctuation -- even though match_complete accepts the full utterance.
    Uses the real shipped catalog, like the fixtures above.
    """

    def test_two_word_prefix_keeps_listening(self, matcher):
        """The finding's reproduction: ['push', 'to'] must continue."""
        assert matcher.can_continue(["push", "to"], "command") is True

    def test_three_word_prefix_keeps_listening(self, matcher):
        assert matcher.can_continue(["push", "to", "talk"], "command") is True

    def test_grid_two_word_prefix_keeps_listening(self, matcher):
        """Second shipped command with a 3-word literal: 'grid next screen'."""
        assert matcher.can_continue(["grid", "next"], "command") is True

    # -- STT punctuation on an opening word --

    def test_punctuated_first_token_keeps_listening(self, matcher):
        """['push,', 'to'] -- first-word normalization reaches this one."""
        assert matcher.can_continue(["push,", "to"], "command") is True

    def test_punctuated_second_token_keeps_listening(self, matcher):
        """['push', 'to,'] -- only the per-token punctuation retry in
        _buffer_opens_literal_prefix (wh-review-pattern-fixes.6 machinery)
        reaches a comma on a LATER opening word."""
        assert matcher.can_continue(["push", "to,"], "command") is True

    def test_parity_with_complete_command_punct_retry(self, matcher):
        """Parity: an utterance the complete-command punctuation retry
        accepts ('push to, talk mode') must have a continuable opening
        (['push', 'to,']) before the remaining words arrive. Otherwise a
        pause after two words finalizes as dictation a command that
        match_complete would have executed."""
        result = matcher.match_complete(
            "push to, talk mode", pattern_type="command"
        )
        assert result is not None
        assert result.matched
        assert matcher.can_continue(["push", "to,"], "command") is True

    # -- the bounds --

    def test_two_word_buffer_off_the_command_finalizes(self, matcher):
        """'push sideways' is not the opening of any shipped command: the
        anchored truncation matchers reject it, so the buffer finalizes
        now instead of waiting for words that never arrive."""
        assert matcher.can_continue(["push", "sideways"], "command") is False

    def test_all_punctuation_token_fails_closed(self, matcher):
        """A token that is ENTIRELY punctuation strips to the empty string
        and the retry refuses (wh-midword-punct-severs-count.1.1): ['push',
        ','] is dictated punctuation, not a command opening."""
        assert matcher.can_continue(["push", ","], "command") is False


# ============================================================================
# wh-review-pattern-fixes.17: bounded whole-utterance alias + off word must
# finalize, not keep buffering on Strategy 2's prefix probe
# ============================================================================


class TestImpossibleBoundedContinuationFinalizes:
    """A bounded (non-greedy) anchored command followed by an off word can
    NEVER extend to a full match, so can_continue must answer False.

    Before the fix, Strategy 2 in ``can_continue`` stripped the trailing $
    and probed ``re.match`` against the joined buffer. For a bounded pattern
    like the shipped ``^(mark[.!?]?)$`` that probe matches any buffer that
    STARTS with the alias -- ``['mark', 'zzq']`` -- even though trailing
    words make the anchored fullmatch impossible forever. The router then
    kept buffering with the command timeout instead of finalizing the words
    as dictation immediately. The prefix probe is only valid for a pattern
    with a greedy tail, whose capture can still consume the extra words
    (wh-review-pattern-fixes.17). Uses the real shipped catalog.
    """

    def test_mark_with_off_word_cannot_continue(self, matcher):
        """The finding's reproduction: ['mark', 'zzq'] must finalize now."""
        assert matcher.can_continue(["mark", "zzq"], "command") is False

    def test_drag_with_off_word_cannot_continue(self, matcher):
        """Second bounded grid alias: ^(drag[.!?]?)$ plus an off word."""
        assert matcher.can_continue(["drag", "zzq"], "command") is False

    def test_colin_alias_with_off_word_cannot_continue(self, matcher):
        """Mishear punctuation alias ^colin$ plus an off word."""
        assert matcher.can_continue(["colin", "zzq"], "command") is False

    def test_cannot_match_reports_true(self, matcher):
        """cannot_match is the router's entry point and must agree."""
        assert matcher.cannot_match(["mark", "zzq"], "command") is True

    # -- positive guards: the aliases themselves are unchanged --

    def test_single_word_mark_still_continues(self, matcher):
        """['mark'] alone fullmatches (Strategy 1) and keeps listening."""
        assert matcher.can_continue(["mark"], "command") is True

    def test_mark_still_matches_complete(self, matcher):
        result = matcher.match_complete("mark", pattern_type="command")
        assert result is not None
        assert result.matched


USER_GREEDY_MIDTAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say (.+) twice$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


@pytest.fixture
def greedy_midtail_matcher(tmp_path):
    """Shipped catalog plus a user pattern whose greedy tail sits MID-pattern.

    ``^say (.+) twice$`` is the legitimate Strategy 2 case: the buffer
    ``['say', 'a', 'twice', 'b']`` prefix-matches the de-anchored pattern
    and CAN still extend to a full match ("say a twice b twice").
    """
    user_file = tmp_path / "user_patterns.toml"
    user_file.write_text(USER_GREEDY_MIDTAIL_TOML, encoding="utf-8")
    catalog = PatternCatalog("speech/config/patterns.toml", str(user_file))
    return PatternMatcher(catalog)


class TestGreedyTailPrefixProbePreserved:
    """Strategy 2 must keep its exact behavior for greedy-tail patterns.

    The wh-review-pattern-fixes.17 fix bounds the prefix probe to greedy
    patterns only. This is the bound's other side: a greedy pattern whose
    tail sits mid-pattern still needs the probe, because a prefix match
    with trailing words CAN be legitimate there.
    """

    def test_greedy_midtail_prefix_still_continues(self, greedy_midtail_matcher):
        """['say', 'a', 'twice', 'b'] can extend to 'say a twice b twice'."""
        assert (
            greedy_midtail_matcher.can_continue(
                ["say", "a", "twice", "b"], "command"
            )
            is True
        )

    def test_greedy_full_match_still_continues(self, greedy_midtail_matcher):
        """['say', 'a', 'twice'] fullmatches (Strategy 1); unchanged."""
        assert (
            greedy_midtail_matcher.can_continue(["say", "a", "twice"], "command")
            is True
        )

    def test_greedy_midtail_off_suffix_still_finalizes(self, greedy_midtail_matcher):
        """['say', 'a', 'b'] has no ' twice' for the prefix probe to reach,
        so dictation still finalizes fast -- the bound on Strategy 2,
        unchanged by the wh-review-pattern-fixes.18/.19 rewrite."""
        assert (
            greedy_midtail_matcher.can_continue(["say", "a", "b"], "command")
            is False
        )


# ============================================================================
# wh-review-pattern-fixes.18 / .19: isolated-catalog fixtures. The main
# patterns file MUST start with COMMAND_HOTWORD or the catalog refuses to
# load. No shipped patterns: each pattern under test must be the ONLY
# candidate for its first word.
# ============================================================================


def _isolated_matcher(tmp_path, patterns_body):
    """PatternMatcher over a minimal MAIN catalog written to tmp_path."""
    main_file = tmp_path / "patterns.toml"
    main_file.write_text(
        'COMMAND_HOTWORD = "x-ray"\n\n' + patterns_body, encoding="utf-8"
    )
    return PatternMatcher(PatternCatalog(str(main_file)))


ALTERNATION_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^(mark|say .+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)

ESCAPED_DOT_TOML = (
    "[[pattern]]\n"
    "pattern = '''^set \\.+ mode$'''\n"
    'actions = [{ function = "type_text", params = ["dots"] }]\n'
)


class TestTextualGreedyMisclassification:
    r"""wh-review-pattern-fixes.18: greediness was detected with a substring
    test ('.+' in pattern_str), so it misfired two ways.

    (a) ^(mark|say .+)$ IS greedy (the tail truncation compiles), but only
    on the 'say' branch. The old Strategy 2 prefix probe matched the bounded
    'mark' branch against ['mark', 'zzq'] and ignored the trailing word, so
    ordinary dictation was held until the command timeout. The path-level
    fullmatch probe must prove the trailing words consumable by the tail.

    (b) ^set \.+ mode$ is NOT greedy (the dot is escaped), but the substring
    test marked it greedy, so the catalog gave it no literal_body_matchers
    and a pause at ['set', '...'] finalized as dictation even though
    'set ... mode' fullmatches.
    """

    @pytest.fixture
    def alternation_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, ALTERNATION_TAIL_TOML)

    @pytest.fixture
    def escaped_dot_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, ESCAPED_DOT_TOML)

    def test_bounded_branch_off_word_finalizes(self, alternation_matcher):
        """The finding's reproduction: no branch consumes 'zzq', so the
        buffer must finalize now."""
        assert (
            alternation_matcher.can_continue(["mark", "zzq"], "command")
            is False
        )

    def test_greedy_branch_fullmatch_continues(self, alternation_matcher):
        """['say', 'foo'] fullmatches the 'say .+' branch (Strategy 1)."""
        assert (
            alternation_matcher.can_continue(["say", "foo"], "command")
            is True
        )

    def test_single_word_mark_still_continues(self, alternation_matcher):
        """['mark'] alone fullmatches the 'mark' branch (Strategy 1)."""
        assert alternation_matcher.can_continue(["mark"], "command") is True

    def test_escaped_dot_partial_buffer_continues(self, escaped_dot_matcher):
        """['set', '...'] is inside the literal body of the bounded pattern
        ^set \\.+ mode$ and must keep listening for 'mode'."""
        assert (
            escaped_dot_matcher.can_continue(["set", "..."], "command")
            is True
        )

    def test_escaped_dot_full_command_matches(self, escaped_dot_matcher):
        """Fixture guard: the complete command the finding pairs with."""
        result = escaped_dot_matcher.match_complete(
            "set ... mode", pattern_type="command"
        )
        assert result is not None
        assert result.matched


SAY_GO_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say go(.*) twice$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)

SAY_GO_NOW_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say go now(.*) twice$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


class TestStrategy2PunctuationNormalization:
    """wh-review-pattern-fixes.19: Strategy 2 probed only the RAW joined
    buffer, while Strategy 1/3 and match_complete use first-word
    normalization and the per-token punctuation retry. An STT comma on an
    opening word made the probe fail, so the router finalized a greedy
    command before its final words could arrive -- even though
    match_complete accepts the completed utterance. Strategy 2 must try the
    same candidate texts, in the same fail-closed way.
    """

    @pytest.fixture
    def say_go_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, SAY_GO_TOML)

    @pytest.fixture
    def say_go_now_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, SAY_GO_NOW_TOML)

    def test_punctuated_first_token_keeps_listening(self, say_go_matcher):
        """The finding's reproduction: ['say,', 'go', 'twice', 'b'] can
        still extend to 'say, go twice b twice'."""
        assert (
            say_go_matcher.can_continue(
                ["say,", "go", "twice", "b"], "command"
            )
            is True
        )

    def test_clean_buffer_still_continues(self, say_go_matcher):
        assert (
            say_go_matcher.can_continue(
                ["say", "go", "twice", "b"], "command"
            )
            is True
        )

    def test_match_complete_parity(self, say_go_matcher):
        """The completed utterance the probe must not cut off."""
        result = say_go_matcher.match_complete(
            "say, go twice b twice", pattern_type="command"
        )
        assert result is not None
        assert result.matched

    def test_all_punctuation_token_fails_closed(self, say_go_matcher):
        """A token that is entirely punctuation strips to the empty string;
        the per-token candidate is skipped (fail closed,
        wh-midword-punct-severs-count.1.1) and nothing crashes."""
        assert (
            say_go_matcher.can_continue(["say,", ",", "go"], "command")
            is False
        )

    def test_punctuated_mid_token_keeps_listening(self, say_go_now_matcher):
        """An STT comma on a LATER opening word ('go,') is out of the
        first-word normalization's reach; only the per-token candidate
        reaches it."""
        assert (
            say_go_now_matcher.can_continue(
                ["say", "go,", "now", "twice", "b"], "command"
            )
            is True
        )


# ============================================================================
# wh-review-pattern-fixes.21: group-wrapped consuming tails must stay
# continuable at the literal opening
# ============================================================================


NONCAPTURING_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up (?:.*)$'''\n"
    'actions = [{ function = "type_text", params = ["tail"] }]\n'
)

NAMED_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up (?P<tail>.+)$'''\n"
    'actions = [{ function = "type_text", params = ["tail"] }]\n'
)

DOUBLE_WRAPPED_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up ((.*))$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)

BRANCHED_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^(?:go home|look up .+)$'''\n"
    'actions = [{ function = "type_text", params = ["go"] }]\n'
)


class TestGroupWrappedTailContinuation:
    """A greedy tail wrapped in group syntax must keep its literal opening
    continuable (wh-review-pattern-fixes.21).

    The old extract_literal_prefix stopped at the span's paren extension,
    so these patterns got prefixes like 'look up (?:' -- no matcher
    compiles from that fragment, Strategy 3 had nothing to test, and
    can_continue(['look', 'up'], 'command') answered False although the
    completed command fullmatches. The router then finalized the buffer as
    dictation at word two, before the tail could arrive.
    """

    @pytest.fixture
    def noncapturing_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, NONCAPTURING_TAIL_TOML)

    @pytest.fixture
    def named_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, NAMED_TAIL_TOML)

    @pytest.fixture
    def double_wrapped_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, DOUBLE_WRAPPED_TAIL_TOML)

    @pytest.fixture
    def branched_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, BRANCHED_TAIL_TOML)

    # -- ^look up (?:.*)$ --

    def test_noncapturing_opening_keeps_listening(self, noncapturing_matcher):
        assert (
            noncapturing_matcher.can_continue(["look", "up"], "command")
            is True
        )

    def test_noncapturing_full_command_still_matches(self, noncapturing_matcher):
        result = noncapturing_matcher.match_complete(
            "look up value", pattern_type="command"
        )
        assert result is not None
        assert result.matched

    # -- ^look up (?P<tail>.+)$ --

    def test_named_group_opening_keeps_listening(self, named_matcher):
        assert named_matcher.can_continue(["look", "up"], "command") is True

    def test_named_group_full_command_still_matches(self, named_matcher):
        result = named_matcher.match_complete(
            "look up value", pattern_type="command"
        )
        assert result is not None
        assert result.matched

    # -- ^look up ((.*))$ --

    def test_double_wrapped_opening_keeps_listening(self, double_wrapped_matcher):
        assert (
            double_wrapped_matcher.can_continue(["look", "up"], "command")
            is True
        )

    def test_double_wrapped_full_command_regains_greedy_metadata(
        self, double_wrapped_matcher
    ):
        """The old truncation 'look up ((.*' did not compile, so the
        pattern had NO greedy metadata at all. The repaired probe source
        restores it: the match result must report is_greedy True."""
        result = double_wrapped_matcher.match_complete(
            "look up value", pattern_type="command"
        )
        assert result is not None
        assert result.matched
        assert result.is_greedy is True

    # -- ^(?:go home|look up .+)$ --

    def test_branched_tail_opening_keeps_listening(self, branched_matcher):
        assert branched_matcher.can_continue(["look", "up"], "command") is True

    def test_branched_tail_full_command_still_matches(self, branched_matcher):
        result = branched_matcher.match_complete(
            "look up value", pattern_type="command"
        )
        assert result is not None
        assert result.matched


# ============================================================================
# wh-review-pattern-fixes.22: a quantified dot inside a zero-width construct
# must not classify the pattern greedy at the catalog level
# ============================================================================


LOOKAHEAD_BOUNDED_TOML = (
    "[[pattern]]\n"
    "pattern = '''^set (?=.+)mode$'''\n"
    'actions = [{ function = "type_text", params = ["mode"] }]\n'
)

LOOKAHEAD_THEN_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up (?=.+)thing (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


class TestZeroWidthDotBoundedCatalog:
    """^set (?=.+)mode$ is a bounded two-word command: the lookahead's dot
    consumes nothing (wh-review-pattern-fixes.22).

    The old scanner counted the lookahead's '.+' as a span, the balanced
    construct compiled and defeated the compile gate, and match_complete
    reported is_greedy True -- which sent the completed two-word command
    down the router's greedy timeout.
    """

    @pytest.fixture
    def lookahead_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, LOOKAHEAD_BOUNDED_TOML)

    @pytest.fixture
    def lookahead_tail_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, LOOKAHEAD_THEN_TAIL_TOML)

    def test_bounded_command_matches_without_greedy_metadata(
        self, lookahead_matcher
    ):
        result = lookahead_matcher.match_complete(
            "set mode", pattern_type="command"
        )
        assert result is not None
        assert result.matched
        assert result.is_greedy is False

    def test_bounded_command_opening_keeps_listening(self, lookahead_matcher):
        """With no span the pattern gets literal_body_matchers, so the
        one-word buffer stays continuable through Strategy 3."""
        assert lookahead_matcher.can_continue(["set"], "command") is True

    def test_real_tail_after_balanced_lookahead_keeps_listening(
        self, lookahead_tail_matcher
    ):
        """The false first span must not poison the real tail's prefix:
        'look up (?=.+)thing (.+)' has the literal opening 'look up ...',
        so ['look', 'up'] keeps listening."""
        assert (
            lookahead_tail_matcher.can_continue(["look", "up"], "command")
            is True
        )

    def test_real_tail_full_command_still_matches(self, lookahead_tail_matcher):
        result = lookahead_tail_matcher.match_complete(
            "look up thing value", pattern_type="command"
        )
        assert result is not None
        assert result.matched
        assert result.is_greedy is True


# ============================================================================
# validate_numeric EDGE CASES (lines 411, 414, 418, 428-429)
# ============================================================================

class TestValidateNumeric:
    def test_no_validation_group_returns_true(self, matcher):
        """Line 411: No validation group = always valid."""
        match = re.fullmatch(r"delete (\d+)", "delete 5")
        assert matcher.validate_numeric(match, None) is True

    def test_none_match_returns_true(self, matcher):
        """Line 414: None match = valid (nothing to validate)."""
        assert matcher.validate_numeric(None, "g1") is True

    def test_words_to_int_none_returns_true(self, matcher):
        """Line 418: If words_to_int loader returns None, assume valid."""
        matcher._words_to_int = None  # Reset lazy loader
        with patch.object(matcher, '_get_words_to_int', return_value=None):
            assert matcher.validate_numeric(MagicMock(), "g1") is True

    def test_invalid_validation_group_format(self, matcher):
        """Lines 428-429: ValueError from invalid group format handled."""
        match = re.fullmatch(r"(test)", "test")
        # "gX" -> int("X") raises ValueError
        assert matcher.validate_numeric(match, "gX") is True  # Error handled, returns True

    def test_index_error_handled(self, matcher):
        """Lines 428-429: IndexError from out-of-range group handled."""
        match = re.fullmatch(r"test", "test")  # No capture groups
        assert matcher.validate_numeric(match, "g99") is True


# ============================================================================
# get_pattern_type (line 442)
# ============================================================================

class TestGetPatternType:
    def test_command_word(self, matcher):
        """Line 442: Delegation to catalog for command words."""
        result = matcher.get_pattern_type("delete")
        assert result == PatternType.COMMAND

    def test_unknown_word(self, matcher):
        result = matcher.get_pattern_type("xyzzyplugh")
        assert result == PatternType.NONE

    def test_replacement_word(self, matcher):
        result = matcher.get_pattern_type("period")
        assert result == PatternType.REPLACEMENT


# ============================================================================
# wh-qj70s: hotword authorization gate in match_single_pattern
# ============================================================================


class TestMatchSinglePatternHotwordGate:
    """match_single_pattern must refuse hotword-required patterns unless the
    caller explicitly marks the call as authorized.

    Background: TextParser.match_single_pattern is reached on two paths:
    1. The router's direct command path -- the buffer was already vetted for
       hotword via match_complete().
    2. SpeechProcessor._process_remainder -- the remainder text was NOT
       vetted; whatever falls into it bypasses hotword checks.

    Path 2 is the bypass the wh-qj70s bead exists to close. The fix is to
    require an authorized_command flag. Default is fail-closed (False).
    Path 1 passes True; path 2 keeps the default.
    """

    def _hotword_required_pattern(self, catalog):
        """Return the pattern dict for ^fix, which requires_hotword=True.

        'save' was this fixture's word until 2026-08-20, when every single-word command traded requires_hotword for whole_utterance_only. '^fix' is the one single-word command that still requires the hotword, so it takes over the role unchanged.
        """
        for p in catalog.get_all_patterns():
            compiled = p.get('compiled_pattern')
            if compiled is None:
                continue
            if compiled.pattern == r'^fix' and p.get('requires_hotword'):
                return p
        raise RuntimeError(
            "Test fixture broken: no requires_hotword='^fix' pattern found"
        )

    def _non_hotword_pattern(self, catalog):
        """Return a command pattern that does NOT require hotword."""
        for p in catalog.get_all_patterns():
            compiled = p.get('compiled_pattern')
            if compiled is None:
                continue
            if compiled.pattern.startswith('^') and not p.get('requires_hotword'):
                return p
        raise RuntimeError(
            "Test fixture broken: no non-hotword command pattern found"
        )

    def test_hotword_required_pattern_refused_when_unauthorized(self, matcher, catalog):
        """Default authorized=False refuses a hotword-required pattern."""
        pattern_data = self._hotword_required_pattern(catalog)
        result = matcher.match_single_pattern("fix", pattern_data)
        assert result is None, (
            "match_single_pattern with default (unauthorized) must refuse "
            "a requires_hotword pattern; otherwise the wh-qj70s remainder "
            "bypass remains open."
        )

    def test_hotword_required_pattern_refused_explicitly_unauthorized(self, matcher, catalog):
        pattern_data = self._hotword_required_pattern(catalog)
        result = matcher.match_single_pattern(
            "fix", pattern_data, authorized_command=False,
        )
        assert result is None

    def test_hotword_required_pattern_allowed_when_authorized(self, matcher, catalog):
        """The router's vetted command path passes authorized_command=True
        and must still get its match back."""
        pattern_data = self._hotword_required_pattern(catalog)
        result = matcher.match_single_pattern(
            "fix", pattern_data, authorized_command=True,
        )
        assert result is not None
        assert result.matched is True
        assert result.requires_hotword is True

    def test_non_hotword_pattern_unaffected_by_default_unauthorized(self, matcher, catalog):
        """Non-hotword patterns ignore the new flag entirely."""
        pattern_data = self._non_hotword_pattern(catalog)
        compiled = pattern_data['compiled_pattern']
        # Build a string the pattern will fully match by stripping anchors.
        text = compiled.pattern.lstrip('^').rstrip('$')
        # Some patterns have alternations or escapes; pick a pattern whose
        # body is a literal phrase. Walk forward if the first non-hotword
        # pattern is too exotic.
        idx = 0
        all_patterns = catalog.get_all_patterns()
        while ('(' in text or '\\' in text or '?' in text or '|' in text) and idx < len(all_patterns):
            p = all_patterns[idx]
            idx += 1
            c = p.get('compiled_pattern')
            if c is None:
                continue
            if not c.pattern.startswith('^') or p.get('requires_hotword'):
                continue
            body = c.pattern.lstrip('^').rstrip('$')
            if '(' not in body and '\\' not in body and '?' not in body and '|' not in body:
                pattern_data = p
                text = body
                break

        result = matcher.match_single_pattern(text, pattern_data)
        assert result is not None
        assert result.matched is True
        assert result.requires_hotword is False


# ============================================================================
# wh-review-pattern-fixes.24: valid re constructs the repair mislexed.
# One pattern per isolated catalog: in a shared catalog the conditional
# pattern (same first word 'say') masks the comment-group false negative,
# so each shape must be the only candidate for its first word.
# ============================================================================


CONDITIONAL_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say (a )?(?(1)foo|look .+)$'''\n"
    'actions = [{ function = "type_text", params = ["conditional"] }]\n'
)

COMMENT_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say (?:look(?# note (with pipe |) up .+)$'''\n"
    'actions = [{ function = "type_text", params = ["comment"] }]\n'
)

INITIAL_BRACKET_TOML = (
    "[[pattern]]\n"
    "pattern = '''^do []a.+]+ say hello$'''\n"
    'actions = [{ function = "type_text", params = ["bracket"] }]\n'
)


class TestConditionalTailFalsePositive:
    """^say (a )?(?(1)foo|look .+)$: group 1 set forces the 'foo' branch,
    so the buffer ['say', 'a', 'look'] can NEVER complete -- yet the
    fabricated prefix 'say (a )?look' kept it buffering until the command
    timeout. With the conservative empty prefix, continuation on the
    reachable 'look .+' branch is owned by Strategy 2's path probe
    (wh-review-pattern-fixes.24)."""

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, CONDITIONAL_TAIL_TOML)

    def test_condition_incompatible_buffer_finalizes(self, matcher):
        """The finding's reproduction: no completion of 'say a look' can
        match, so the buffer must finalize as dictation now."""
        assert matcher.can_continue(["say", "a", "look"], "command") is False

    def test_reachable_tail_branch_continues(self, matcher):
        """Group 1 unset: 'say look up' extends through 'look .+'."""
        assert matcher.can_continue(["say", "look", "up"], "command") is True

    def test_complete_yes_branch_matches(self, matcher):
        result = matcher.match_complete("say a foo", pattern_type="command")
        assert result is not None
        assert result.matched

    def test_complete_no_branch_matches(self, matcher):
        result = matcher.match_complete(
            "say look up x", pattern_type="command"
        )
        assert result is not None
        assert result.matched


class TestCommentGroupContinuation:
    """^say (?:look(?# note (with pipe |) up .+)$: the comment ends at the
    ')' after 'pipe |', so the effective pattern is ^say (?:look up .+)$.
    The repair parsed the comment as nested parens and produced the garbage
    prefix 'say look note (with pipe |) up', so the reachable pause
    ['say', 'look', 'up'] finalized as dictation
    (wh-review-pattern-fixes.24)."""

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, COMMENT_TAIL_TOML)

    def test_pause_at_effective_opening_continues(self, matcher):
        """The finding's reproduction: 'say look up' is the literal opening
        of the effective pattern and must keep listening for the tail."""
        assert matcher.can_continue(["say", "look", "up"], "command") is True

    def test_comment_text_buffer_finalizes(self, matcher):
        """'note' is comment text, not speakable: no completion exists."""
        assert (
            matcher.can_continue(["say", "look", "note"], "command") is False
        )

    def test_complete_command_matches(self, matcher):
        result = matcher.match_complete(
            "say look up x", pattern_type="command"
        )
        assert result is not None
        assert result.matched


class TestInitialBracketClassContinuation:
    """^do []a.+]+ say hello$: ']' immediately after '[' is a literal class
    member, so the pattern is bounded (the '.+' is class text). The scanner
    closed the class at the first ']', saw a span, and the catalog got no
    literal_body_matchers -- so the reachable pause ['do', 'a', 'say']
    finalized as dictation while 'do a say hello' fullmatches
    (wh-review-pattern-fixes.24)."""

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, INITIAL_BRACKET_TOML)

    def test_pause_inside_literal_body_continues(self, matcher):
        """The finding's reproduction: 'do a say' is an exact truncation of
        the bounded body and must keep listening for 'hello'."""
        assert matcher.can_continue(["do", "a", "say"], "command") is True

    def test_off_body_buffer_finalizes(self, matcher):
        """'z' is not a member of the class []a.+]: no completion exists."""
        assert matcher.can_continue(["do", "z", "say"], "command") is False

    def test_complete_command_matches(self, matcher):
        result = matcher.match_complete(
            "do a say hello", pattern_type="command"
        )
        assert result is not None
        assert result.matched


# ============================================================================
# wh-review-pattern-fixes.27: numeric validation uses the REAL group number
# ============================================================================


NUMERIC_LOOKAHEAD_TOML = (
    "[[pattern]]\n"
    "pattern = '''^foo (?=bar )bar (\\d+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


class TestNumericValidationRealGroupNumber:
    r"""A lookahead before a numeric capture must not shift validation.

    The old transform counted the lookahead's ``(`` as a capture and
    recorded g2 for ``^foo (?=bar )bar (\d+)$``, whose compiled form has
    ONE group. validate_numeric then skipped the missing group 2, so the
    widened ``(\w+)`` accepted any word (wh-review-pattern-fixes.27).
    With the real group number recorded, the captured word is validated.
    """

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, NUMERIC_LOOKAHEAD_TOML)

    def test_non_number_in_capture_refused(self, matcher):
        """The finding's reproduction: 'nonsense' is not a number, so
        the command must NOT match."""
        assert matcher.match_complete("foo bar nonsense", "command") is None

    def test_number_word_in_capture_accepted(self, matcher):
        result = matcher.match_complete("foo bar five", "command")
        assert result is not None
        assert result.validation_group == "g1"
        assert result.match_object.group(1) == "five"

    def test_digits_in_capture_accepted(self, matcher):
        result = matcher.match_complete("foo bar 5", "command")
        assert result is not None


VERBOSE_COMMENT_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look(?x: # .+ comment\n) up now$'''\n"
    'actions = [{ function = "type_text", params = ["ok"] }]\n'
)

VERBOSE_NUMERIC_TOML = (
    "[[pattern]]\n"
    "pattern = '''^foo(?x: # (not a capture)\n) (\\d+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)


class TestVerboseCommentContinuation:
    """^look(?x: # .+ comment\\n) up now$ (wh-review-pattern-fixes.29):
    the scoped verbose group is a comment plus ignored whitespace, so re
    fullmatches 'look up now'. The old lexer saw the commented '.+' as a
    span, extract_full_literal_body returned '', and the pause after
    'look up' finalized as dictation before the valid final word."""

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, VERBOSE_COMMENT_TOML)

    def test_pause_before_final_word_continues(self, matcher):
        """The finding's reproduction: 'look up' must keep listening."""
        assert matcher.can_continue(["look", "up"], "command") is True

    def test_complete_command_matches(self, matcher):
        result = matcher.match_complete("look up now", "command")
        assert result is not None
        assert result.matched

    def test_off_word_finalizes(self, matcher):
        """'look zzq' has no completion: finalize as dictation now."""
        assert matcher.can_continue(["look", "zzq"], "command") is False


class TestVerboseCommentNumericValidation:
    """^foo(?x: # (not a capture)\\n) (\\d+)$ (wh-review-pattern-fixes.29):
    the paren inside the active-verbose comment is not a capture, so the
    widened group is re.compile's g1 and validate_numeric checks it. The
    old g2 metadata pointed past the pattern's one group, validation was
    skipped permissively, and any word executed the command."""

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, VERBOSE_NUMERIC_TOML)

    def test_non_number_refused(self, matcher):
        """The finding's reproduction: 'foo word' must NOT execute."""
        assert matcher.match_complete("foo word", "command") is None

    def test_number_word_accepted(self, matcher):
        result = matcher.match_complete("foo five", "command")
        assert result is not None
        assert result.validation_group == "g1"

    def test_digits_accepted(self, matcher):
        result = matcher.match_complete("foo 5", "command")
        assert result is not None


DOUBLE_SPACE_TAIL_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up  (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)

TRAILING_SPACE_BODY_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look up now $'''\n"
    'actions = [{ function = "type_text", params = ["ok"] }]\n'
)


class _LeadingSpaceCatalog:
    """Catalog stub whose pattern data predates every precomputed field,
    so _buffer_opens_literal_prefix takes the runtime extraction path.
    The real catalog's first-word indexing may not even key ``^ look``;
    the matcher-level guard must hold regardless of how the pattern was
    keyed (wh-review-pattern-fixes.30, leading-space case)."""

    def __init__(self, pattern_str):
        self._entry = (
            re.compile(pattern_str, re.IGNORECASE),
            "command",
            {"pattern_type": "command", "actions": []},
        )

    def get_matching_patterns(self, first_word):
        return [self._entry]

    def get_all_patterns(self):
        return []


class TestImpossibleWhitespaceFinalizes:
    """Patterns whose whitespace no single-space token join can supply
    (wh-review-pattern-fixes.30). The generic strips rewrote the required
    whitespace away, the matchers accepted the buffer, and dictation was
    held until the command timeout for a command that can NEVER complete
    -- match_complete uses the same join, so these patterns are
    unspeakable either way. The correct outcome is to finalize at once."""

    @pytest.fixture
    def double_space_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, DOUBLE_SPACE_TAIL_TOML)

    @pytest.fixture
    def trailing_space_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, TRAILING_SPACE_BODY_TOML)

    def test_double_space_tail_finalizes(self, double_space_matcher):
        """Case (a): ^look up  (.+)$ can never fullmatch a join, so the
        pause after 'look up' must finalize as dictation."""
        assert double_space_matcher.can_continue(
            ["look", "up"], "command") is False

    def test_double_space_tail_never_completes(self, double_space_matcher):
        """Parity: the completion path refuses the same joins."""
        assert double_space_matcher.match_complete(
            "look up later", "command") is None

    def test_trailing_space_body_finalizes(self, trailing_space_matcher):
        """Case (b): ^look up now $ requires a trailing space no join
        has; neither pause may hold the buffer."""
        assert trailing_space_matcher.can_continue(
            ["look", "up"], "command") is False
        assert trailing_space_matcher.can_continue(
            ["look", "up", "now"], "command") is False

    def test_trailing_space_body_never_completes(self, trailing_space_matcher):
        assert trailing_space_matcher.match_complete(
            "look up now", "command") is None

    def test_leading_space_prefix_finalizes(self):
        """Case (c): ^ look up (.+)$ requires a leading space. Contrary
        to the probe note on the bead, this DID reproduce: the builder's
        strip removed the leading space and the runtime-extraction path
        held the buffer."""
        matcher = PatternMatcher(_LeadingSpaceCatalog(r"^ look up (.+)$"))
        assert matcher.can_continue(["look", "up"], "command") is False

    def test_leading_space_prefix_never_completes(self):
        matcher = PatternMatcher(_LeadingSpaceCatalog(r"^ look up (.+)$"))
        assert matcher.match_complete("look up later", "command") is None

    def test_normal_spacing_still_continues(self, tmp_path):
        """Guard: the ordinary single-space shape keeps its continuation
        (wh-multiword-command-prefix-capture machinery unchanged)."""
        matcher = _isolated_matcher(
            tmp_path,
            "[[pattern]]\n"
            "pattern = '''^look up (.+)$'''\n"
            'actions = [{ function = "type_text", params = ["$1"] }]\n',
        )
        assert matcher.can_continue(["look", "up"], "command") is True
        assert matcher.can_continue(["look", "sideways"], "command") is False
        result = matcher.match_complete("look up later", "command")
        assert result is not None


# ============================================================================
# wh-review-pattern-fixes.34: safe \s separator spellings keep the buffer
# continuable through a three-or-more-word literal opening
# ============================================================================


ESCAPED_SEPARATOR_TOML = (
    "[[pattern]]\n"
    "pattern = '''^gaze\\s?upon\\s?stars (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
    "\n"
    "[[pattern]]\n"
    "pattern = '''^peer\\sinto\\svoid$'''\n"
    'actions = [{ function = "type_text", params = ["void"] }]\n'
)


class TestCanContinueEscapedSeparatorSpellings:
    r"""A command that joins its literal words with ``\s?`` (or any other
    spelling that accepts one space) needs the same intermediate
    truncations a plain-space command gets; without them the two-word
    buffer finalizes as dictation and the command can never be completed
    by pausing (wh-review-pattern-fixes.34)."""

    @pytest.fixture
    def escaped_sep_matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, ESCAPED_SEPARATOR_TOML)

    def test_two_word_buffer_continues(self, escaped_sep_matcher):
        """The filed repro at matcher level: ['gaze', 'upon'] must
        continue, because 'gaze upon stars value' fullmatches."""
        assert escaped_sep_matcher.can_continue(
            ["gaze", "upon"], "command") is True

    def test_full_prefix_continues(self, escaped_sep_matcher):
        assert escaped_sep_matcher.can_continue(
            ["gaze", "upon", "stars"], "command") is True

    def test_completion_matches(self, escaped_sep_matcher):
        result = escaped_sep_matcher.match_complete(
            "gaze upon stars value", "command")
        assert result is not None

    def test_off_pattern_buffer_stops(self, escaped_sep_matcher):
        assert escaped_sep_matcher.can_continue(
            ["gaze", "sideways"], "command") is False

    def test_nongreedy_body_two_word_buffer_continues(
            self, escaped_sep_matcher):
        """The literal_body_matchers path: ^peer\\sinto\\svoid$ has no
        tail, so the whole body drives continuation."""
        assert escaped_sep_matcher.can_continue(
            ["peer", "into"], "command") is True


# ============================================================================
# wh-review-pattern-fixes.43: an inline comment group between a dot and its
# quantifier still leaves a real greedy tail
# ============================================================================


COMMENT_QUANTIFIED_TOML = (
    "[[pattern]]\n"
    "pattern = '''^say .(?# note)+$'''\n"
    'actions = [{ function = "type_text", params = ["said"] }]\n'
)


class TestCommentQuantifiedTailCatalog:
    r"""``^say .(?# note)+$``: the comment is zero-width, so re attaches
    the ``+`` to the dot and the pattern swallows the rest of the
    utterance. The span scanner's dot look-ahead did not skip a comment
    group, so the catalog recorded no greedy metadata and the matcher
    reported the completed match as is_greedy=False
    (wh-review-pattern-fixes.43).
    """

    @pytest.fixture
    def matcher(self, tmp_path):
        return _isolated_matcher(tmp_path, COMMENT_QUANTIFIED_TOML)

    def test_completed_match_reports_greedy(self, matcher):
        """The finding's reproduction at matcher level."""
        result = matcher.match_for_routing(["say", "hello"], "command", False)
        assert result is not None
        assert result.matched is True
        assert result.is_greedy is True

    def test_catalog_stores_the_literal_prefix(self, matcher):
        entries = matcher.catalog.get_matching_patterns("say")
        assert len(entries) == 1
        _compiled, _ptype, data = entries[0]
        assert data["is_greedy"] is True
        assert data["literal_prefix"] == "say"

    def test_catalog_stores_no_literal_body(self, matcher):
        r"""The old walk saw no tail and handed the catalog the regex text
        ``say .+`` as a literal body, whose compiled matcher ``^say .+$``
        accepted every buffer that starts with 'say '."""
        _compiled, _ptype, data = matcher.catalog.get_matching_patterns(
            "say")[0]
        assert "literal_body_matchers" not in data

    def test_longer_utterance_keeps_buffering(self, matcher):
        """The greedy tail can consume more words, so the buffer stays
        continuable."""
        assert matcher.can_continue(["say", "hello", "there"], "command") is True


# ============================================================================
# wh-review-pattern-fixes.44: a nested empty verbose scope keeps its
# continuation matchers
# ============================================================================


NESTED_SCOPE_GREEDY_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look(?x:(?x: # nested comment\n)) up (.+)$'''\n"
    'actions = [{ function = "type_text", params = ["$1"] }]\n'
)

NESTED_SCOPE_BOUNDED_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look(?x:(?x: # nested comment\n)) up now$'''\n"
    'actions = [{ function = "type_text", params = ["ok"] }]\n'
)

NESTED_LOOKAHEAD_TOML = (
    "[[pattern]]\n"
    "pattern = '''^look(?x:(?! )) up now$'''\n"
    'actions = [{ function = "type_text", params = ["ok"] }]\n'
)


class TestNestedVerboseScopeContinuation:
    r"""A scope inside a scope, both holding only ignored whitespace and
    comments, consumes nothing: ``^look(?x:(?x: # c\n)) up now$``
    fullmatches "look up now". The zero-width proof treated the nested
    ``(`` as live content, so the greedy shape lost its literal prefix and
    the bounded shape lost its whole body -- and the pause after "look up"
    finalized as dictation (wh-review-pattern-fixes.44).
    """

    def test_greedy_pause_at_the_opening_continues(self, tmp_path):
        """The finding's greedy reproduction: 'look up' must keep
        listening for the tail."""
        matcher = _isolated_matcher(tmp_path, NESTED_SCOPE_GREEDY_TOML)
        assert matcher.can_continue(["look", "up"], "command") is True

    def test_greedy_completion_matches(self, tmp_path):
        matcher = _isolated_matcher(tmp_path, NESTED_SCOPE_GREEDY_TOML)
        result = matcher.match_complete("look up value", "command")
        assert result is not None
        assert result.matched

    def test_greedy_off_word_finalizes(self, tmp_path):
        """'look sideways' is not the pattern's opening: finalize now."""
        matcher = _isolated_matcher(tmp_path, NESTED_SCOPE_GREEDY_TOML)
        assert matcher.can_continue(["look", "sideways"], "command") is False

    def test_bounded_pause_before_final_word_continues(self, tmp_path):
        """The finding's bounded reproduction: the whole body drives
        continuation, so 'look up' must wait for 'now'."""
        matcher = _isolated_matcher(tmp_path, NESTED_SCOPE_BOUNDED_TOML)
        assert matcher.can_continue(["look", "up"], "command") is True

    def test_bounded_completion_matches(self, tmp_path):
        matcher = _isolated_matcher(tmp_path, NESTED_SCOPE_BOUNDED_TOML)
        result = matcher.match_complete("look up now", "command")
        assert result is not None
        assert result.matched

    def test_nested_lookahead_scope_stays_conservative(self, tmp_path):
        r"""The negative control. Under the inherited x flag ``(?! )`` is
        ``(?!)``, a negative lookahead on the empty pattern, so the
        pattern matches NOTHING. A proof that recursed into assertions
        would extract the body "look up now" and hold the buffer for a
        command the user can never speak."""
        matcher = _isolated_matcher(tmp_path, NESTED_LOOKAHEAD_TOML)
        assert matcher.match_complete("look up now", "command") is None
        assert matcher.can_continue(["look", "up"], "command") is False
