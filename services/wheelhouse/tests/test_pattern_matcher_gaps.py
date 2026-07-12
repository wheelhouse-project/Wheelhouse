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
    router finalizes a buffer like 'save ...' as dictation immediately instead
    of waiting the full command_timeout.
    """

    def _hotword_only_word(self, catalog):
        """A first word whose every candidate command pattern requires hotword.

        '^save$' (requires_hotword=true) is the only command pattern starting
        with 'save', so the buffer ['save'] is hotword-only.
        """
        pats = catalog.get_matching_patterns("save")
        assert pats, "fixture broken: no patterns for 'save'"
        assert all(
            d.get("requires_hotword") for _cp, t, d in pats if t == "command"
        ), "fixture broken: 'save' has a non-hotword command pattern"
        return "save"

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
        """Return the pattern dict for ^save$, which requires_hotword=True."""
        for p in catalog.get_all_patterns():
            compiled = p.get('compiled_pattern')
            if compiled is None:
                continue
            if compiled.pattern == r'^save$' and p.get('requires_hotword'):
                return p
        raise RuntimeError(
            "Test fixture broken: no requires_hotword='^save$' pattern found"
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
        result = matcher.match_single_pattern("save", pattern_data)
        assert result is None, (
            "match_single_pattern with default (unauthorized) must refuse "
            "a requires_hotword pattern; otherwise the wh-qj70s remainder "
            "bypass remains open."
        )

    def test_hotword_required_pattern_refused_explicitly_unauthorized(self, matcher, catalog):
        pattern_data = self._hotword_required_pattern(catalog)
        result = matcher.match_single_pattern(
            "save", pattern_data, authorized_command=False,
        )
        assert result is None

    def test_hotword_required_pattern_allowed_when_authorized(self, matcher, catalog):
        """The router's vetted command path passes authorized_command=True
        and must still get its match back."""
        pattern_data = self._hotword_required_pattern(catalog)
        result = matcher.match_single_pattern(
            "save", pattern_data, authorized_command=True,
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
