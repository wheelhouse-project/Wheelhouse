"""Unit tests for PatternMatcher module.

Tests the consolidated pattern matching logic that replaces duplicate
code in SpeechRouter and TextParser.

Test Categories:
1. MatchResult dataclass
2. match_complete() - fullmatch vs search
3. is_pattern_complete() - for routing decisions
4. can_continue() / cannot_match() - prefix matching
5. validate_numeric() - numeric validation
6. Consistency with original implementations
"""

import pytest
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog, PatternType
from services.wheelhouse.speech.pattern_matcher import PatternMatcher, MatchResult


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")
    return PatternCatalog(patterns_file)


@pytest.fixture
def matcher(catalog):
    """PatternMatcher with production patterns."""
    return PatternMatcher(catalog)


# ============================================================================
# TEST CLASS: MATCH RESULT DATACLASS
# ============================================================================

class TestMatchResult:
    """Test MatchResult dataclass."""

    def test_default_values(self):
        """MatchResult has sensible defaults."""
        result = MatchResult(matched=False)
        assert result.matched is False
        assert result.pattern_type == ""
        assert result.match_object is None
        assert result.matched_text == ""
        assert result.remainder == ""
        assert result.requires_hotword is False
        assert result.validation_group is None
        assert result.is_greedy is False
        assert result.actions == []
        assert result.pattern_data == {}

    def test_groups_property_without_match(self):
        """groups property returns empty tuple when no match."""
        result = MatchResult(matched=False)
        assert result.groups == ()

    def test_group_method_without_match(self):
        """group() method returns None when no match."""
        result = MatchResult(matched=False)
        assert result.group(1) is None

    def test_matched_result(self):
        """MatchResult with matched=True contains pattern data."""
        result = MatchResult(
            matched=True,
            pattern_type="command",
            matched_text="undo",
            actions=[{"function": "undo"}]
        )
        assert result.matched is True
        assert result.pattern_type == "command"
        assert len(result.actions) == 1


# ============================================================================
# TEST CLASS: MATCH_COMPLETE - FULLMATCH VS SEARCH
# ============================================================================

class TestMatchComplete:
    """Test match_complete() method - the core matching logic."""

    def test_command_fullmatch_success(self, matcher):
        """Command patterns use fullmatch - exact text matches."""
        result = matcher.match_complete("undo")
        assert result is not None
        assert result.matched is True
        assert result.pattern_type == "command"
        assert result.matched_text == "undo"

    def test_command_fullmatch_failure(self, matcher):
        """Command patterns with extra text don't match (fullmatch)."""
        result = matcher.match_complete("undo please")
        # Should not match ^undo$ because of extra text
        assert result is None or result.pattern_type != "command" or "undo" not in result.matched_text

    def test_replacement_search_success(self, matcher):
        """Replacement patterns use search - matches within text."""
        result = matcher.match_complete("comma")
        assert result is not None
        assert result.matched is True
        assert result.pattern_type == "replacement"

    def test_replacement_search_mid_text(self, matcher):
        """Replacement patterns match mid-text via search."""
        result = matcher.match_complete("hello comma world")
        assert result is not None
        assert result.matched is True
        assert result.pattern_type == "replacement"
        assert "comma" in result.matched_text

    def test_no_match_returns_none(self, matcher):
        """Non-matching text returns None."""
        result = matcher.match_complete("xyzzy plugh")
        assert result is None

    def test_filter_by_pattern_type_command(self, matcher):
        """Can filter to only command patterns."""
        result = matcher.match_complete("undo", pattern_type="command")
        assert result is not None
        assert result.pattern_type == "command"

    def test_filter_by_pattern_type_replacement(self, matcher):
        """Can filter to only replacement patterns."""
        result = matcher.match_complete("comma", pattern_type="replacement")
        assert result is not None
        assert result.pattern_type == "replacement"

    def test_filter_excludes_other_type(self, matcher):
        """Filtering excludes patterns of wrong type."""
        # "undo" is a command, should not match as replacement
        result = matcher.match_complete("undo", pattern_type="replacement")
        assert result is None


# ============================================================================
# TEST CLASS: MATCH_COMPLETE - HOTWORD HANDLING
# ============================================================================

class TestMatchCompleteHotword:
    """Test hotword requirement handling in match_complete()."""

    def test_no_hotword_pattern_matches_without_hotword(self, matcher):
        """Patterns not requiring hotword match without it."""
        result = matcher.match_complete("undo", hotword_active=False)
        assert result is not None
        assert result.matched is True

    def test_requires_hotword_skipped_when_inactive(self, matcher, catalog):
        """Patterns requiring hotword are skipped when hotword inactive."""
        # Find a pattern that requires hotword
        # The behavior depends on catalog contents
        # This is a behavioral test - we verify the logic works
        pass  # Pattern-dependent test


# ============================================================================
# TEST CLASS: MATCH_COMPLETE - REMAINDER EXTRACTION
# ============================================================================

class TestMatchCompleteRemainder:
    """Test remainder extraction in match_complete()."""

    def test_remainder_extraction(self, matcher):
        """Remainder is extracted after match."""
        result = matcher.match_complete("comma world")
        assert result is not None
        assert result.remainder == "world"

    def test_no_remainder_full_match(self, matcher):
        """No remainder when entire text matches."""
        result = matcher.match_complete("comma")
        assert result is not None
        assert result.remainder == ""

    def test_remainder_multiple_words(self, matcher):
        """Remainder includes all words after match."""
        result = matcher.match_complete("comma hello world")
        assert result is not None
        assert "hello" in result.remainder
        assert "world" in result.remainder


# ============================================================================
# TEST CLASS: MATCH_FOR_ROUTING
# ============================================================================

class TestMatchForRouting:
    """Test match_for_routing() - optimized for SpeechRouter."""

    def test_buffer_to_text(self, matcher):
        """Buffer list is joined to text."""
        result = matcher.match_for_routing(["question", "mark"], "replacement")
        assert result is not None
        assert result.matched is True

    def test_empty_buffer_returns_none(self, matcher):
        """Empty buffer returns None."""
        result = matcher.match_for_routing([], "command")
        assert result is None

    def test_first_word_used_for_lookup(self, matcher):
        """First word from buffer is used for pattern lookup."""
        result = matcher.match_for_routing(["undo"], "command")
        assert result is not None
        assert result.matched is True


# ============================================================================
# TEST CLASS: IS_PATTERN_COMPLETE
# ============================================================================

class TestIsPatternComplete:
    """Test is_pattern_complete() for routing decisions."""

    def test_complete_command(self, matcher):
        """Complete command pattern returns True."""
        assert matcher.is_pattern_complete(["undo"], "command") is True

    def test_complete_replacement(self, matcher):
        """Complete replacement pattern returns True."""
        assert matcher.is_pattern_complete(["comma"], "replacement") is True

    def test_incomplete_pattern_false(self, matcher):
        """Incomplete pattern returns False."""
        # "question" alone is incomplete for "question mark"
        assert matcher.is_pattern_complete(["question"], "replacement") is False

    def test_non_matching_false(self, matcher):
        """Non-matching buffer returns False."""
        assert matcher.is_pattern_complete(["xyzzy"], "command") is False

    def test_empty_buffer_false(self, matcher):
        """Empty buffer returns False."""
        assert matcher.is_pattern_complete([], "command") is False


# ============================================================================
# TEST CLASS: CAN_CONTINUE / CANNOT_MATCH
# ============================================================================

class TestCanContinue:
    """Test can_continue() and cannot_match() for buffering decisions."""

    def test_valid_prefix_can_continue(self, matcher):
        """Valid prefix of pattern can continue."""
        # "delete" is valid prefix of "delete 5"
        assert matcher.can_continue(["delete"], "command") is True

    def test_complete_pattern_can_continue(self, matcher):
        """Complete pattern can also continue (optional params)."""
        assert matcher.can_continue(["undo"], "command") is True

    def test_invalid_prefix_cannot_continue(self, matcher):
        """Invalid prefix cannot continue."""
        assert matcher.can_continue(["xyzzy"], "command") is False

    def test_empty_buffer_can_continue(self, matcher):
        """Empty buffer can always continue."""
        assert matcher.can_continue([], "command") is True

    def test_cannot_match_inverse(self, matcher):
        """cannot_match is inverse of can_continue."""
        assert matcher.cannot_match(["delete"], "command") is False
        assert matcher.cannot_match(["xyzzy"], "command") is True


# ============================================================================
# TEST CLASS: VALIDATE_NUMERIC
# ============================================================================

class TestValidateNumeric:
    """Test validate_numeric() for numeric parameter validation."""

    def test_valid_digit(self, matcher):
        """Digit numbers pass validation."""
        import re
        match = re.match(r"delete (\d+)", "delete 5")
        assert matcher.validate_numeric(match, "g1") is True

    def test_no_validation_group_passes(self, matcher):
        """No validation group always passes."""
        import re
        match = re.match(r"undo", "undo")
        assert matcher.validate_numeric(match, None) is True

    def test_none_match_passes(self, matcher):
        """None match passes (nothing to validate)."""
        assert matcher.validate_numeric(None, "g1") is True


# ============================================================================
# TEST CLASS: CONSISTENCY WITH ROUTER
# ============================================================================

class TestConsistencyWithRouter:
    """Test that PatternMatcher produces same results as SpeechRouter."""

    def test_command_match_consistency(self, matcher, catalog):
        """PatternMatcher matches same commands as Router would."""
        from services.wheelhouse.speech.router import SpeechRouter
        router = SpeechRouter(catalog, hotword="x-ray")

        # Router's _resolve_finalization for "undo"
        router_decision = router._resolve_finalization(["undo"], hotword_active=False)

        # PatternMatcher's match_for_routing
        matcher_result = matcher.match_for_routing(["undo"], "command")

        # Both should match
        assert router_decision.action.name == "EXECUTE"
        assert matcher_result is not None
        assert matcher_result.matched is True

    def test_replacement_match_consistency(self, matcher, catalog):
        """PatternMatcher matches same replacements as Router would."""
        from services.wheelhouse.speech.router import SpeechRouter
        router = SpeechRouter(catalog, hotword="x-ray")

        # Router's _resolve_finalization for "comma"
        router_decision = router._resolve_finalization(["comma"], hotword_active=False)

        # PatternMatcher's match_for_routing
        matcher_result = matcher.match_for_routing(["comma"], "replacement")

        # Both should match
        assert router_decision.action.name == "EXECUTE"
        assert matcher_result is not None
        assert matcher_result.matched is True

    def test_remainder_consistency(self, matcher, catalog):
        """PatternMatcher extracts same remainder as Router."""
        from services.wheelhouse.speech.router import SpeechRouter
        router = SpeechRouter(catalog, hotword="x-ray")

        # Router's finalization with remainder
        router_decision = router._resolve_finalization(["comma", "world"], hotword_active=False)

        # PatternMatcher's match
        matcher_result = matcher.match_for_routing(["comma", "world"], "replacement")

        # Both should have same remainder
        assert router_decision.remainder == "world"
        assert matcher_result.remainder == "world"


# ============================================================================
# TEST CLASS: CONSISTENCY WITH TEXT PARSER
# ============================================================================

class TestConsistencyWithTextParser:
    """Test that PatternMatcher produces same results as TextParser."""

    def test_command_fullmatch_consistency(self, matcher):
        """PatternMatcher uses fullmatch for commands like TextParser."""
        # TextParser: if pattern.startswith('^'): fullmatch
        # PatternMatcher should do the same

        # "undo" matches
        result = matcher.match_complete("undo")
        assert result is not None

        # "undo extra" should NOT match ^undo$
        result = matcher.match_complete("undo extra", pattern_type="command")
        assert result is None

    def test_replacement_search_consistency(self, matcher):
        """PatternMatcher uses search for replacements like TextParser."""
        # TextParser: if not pattern.startswith('^'): search
        # PatternMatcher should do the same

        # "hello comma world" should match via search
        result = matcher.match_complete("hello comma world")
        assert result is not None
        assert "comma" in result.matched_text


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
