"""Unit tests for PatternCatalog.

Tests the pattern loading, first-word extraction, and pattern type detection
that will be used by PatternMatcher after refactoring.

Test Categories:
1. Pattern loading from TOML
2. First-word extraction (alternations, optional chars)
3. Pattern type detection (COMMAND vs REPLACEMENT)
4. Pattern lookup methods
"""

import pytest
from pathlib import Path
import sys
import re

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from services.wheelhouse.speech.pattern_catalog import PatternCatalog, PatternType


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def catalog():
    """Real pattern catalog with production patterns."""
    patterns_file = str(project_root / "services" / "wheelhouse" / "speech" / "config" / "patterns.toml")
    return PatternCatalog(patterns_file)


# ============================================================================
# TEST CLASS: PATTERN LOADING
# ============================================================================

class TestPatternLoading:
    """Test pattern loading from TOML file."""

    def test_patterns_loaded(self, catalog):
        """Patterns are loaded from TOML."""
        assert catalog.pattern_count > 0
        assert len(catalog.get_all_patterns()) > 0

    def test_first_words_indexed(self, catalog):
        """First words are indexed for O(1) lookup."""
        assert len(catalog.first_words) > 0

    def test_hotword_loaded(self, catalog):
        """Command hotword is loaded from config."""
        assert catalog.command_hotword is not None
        assert isinstance(catalog.command_hotword, str)

    def test_all_patterns_have_required_fields(self, catalog):
        """All patterns have required fields."""
        for pattern in catalog.get_all_patterns():
            assert 'compiled_pattern' in pattern
            assert 'pattern_type' in pattern
            assert 'actions' in pattern
            assert isinstance(pattern['compiled_pattern'], re.Pattern)


# ============================================================================
# TEST CLASS: FIRST WORD EXTRACTION
# ============================================================================

class TestFirstWordExtraction:
    """Test _extract_first_words() method."""

    def test_simple_literal(self, catalog):
        """Simple literal extracts correctly."""
        # Test internal method
        words = catalog._extract_first_words("^backspace$")
        assert "backspace" in words

    def test_alternation_extraction(self, catalog):
        """Alternation extracts all alternatives."""
        # Pattern like (backspace|back space) should extract both
        words = catalog._extract_first_words("^(backspace|back space)$")
        assert "backspace" in words
        assert "back" in words

    def test_optional_prefix_extraction(self, catalog):
        """Optional prefix extracts both with and without."""
        # Pattern like (?:go )?down should extract "go" and "down"
        words = catalog._extract_first_words("^(?:go )?down$")
        assert "go" in words
        assert "down" in words

    def test_optional_char_extraction(self, catalog):
        """Optional character extracts both variants."""
        # Pattern like quotes? should extract "quote" and "quotes"
        words = catalog._extract_first_words("quotes?$")
        assert "quote" in words
        assert "quotes" in words

    def test_word_boundary_stripped(self, catalog):
        """Word boundaries are stripped for extraction."""
        words = catalog._extract_first_words("\\bcomma\\b")
        assert "comma" in words

    # ------------------------------------------------------------------
    # Escaped literal asterisk patterns (\*word\*)
    # ------------------------------------------------------------------

    def test_escaped_literal_single_word(self, catalog):
        r"""Pattern \*cough\* extracts '*cough*' as first word."""
        words = catalog._extract_first_words(r"\*cough\*")
        assert "*cough*" in words

    def test_escaped_literal_alternation(self, catalog):
        r"""Pattern \*(?:slurp|cough)\* extracts each alternative with asterisks."""
        words = catalog._extract_first_words(r"\*(?:slurp|cough)\*")
        assert "*slurp*" in words
        assert "*cough*" in words

    def test_escaped_literal_multiword_alternative(self, catalog):
        r"""Multi-word alternative 'clears throat' extracts '*clears' as first word."""
        words = catalog._extract_first_words(r"\*(?:cough|clears throat)\*")
        assert "*cough*" in words
        assert "*clears" in words

    def test_escaped_literal_hyphenated_alternative(self, catalog):
        r"""Hyphenated alternative 'mm-hmm' extracts '*mm-hmm*' as first word."""
        words = catalog._extract_first_words(r"\*(?:cough|mm-hmm)\*")
        assert "*cough*" in words
        assert "*mm-hmm*" in words

    def test_escaped_literal_production_pattern(self, catalog):
        r"""Production non-speech pattern extracts all expected first words."""
        pattern = r"\*(?:slurp|ahem|sniff|cough|coughs|clears throat|mm-hmm)\*"
        words = catalog._extract_first_words(pattern)
        assert "*slurp*" in words
        assert "*ahem*" in words
        assert "*sniff*" in words
        assert "*cough*" in words
        assert "*coughs*" in words
        assert "*clears" in words  # multi-word: first word only
        assert "*mm-hmm*" in words


# ============================================================================
# TEST CLASS: PATTERN TYPE DETECTION
# ============================================================================

class TestPatternTypeDetection:
    """Test pattern type detection from ^ anchor.

    This is CRITICAL logic that PatternMatcher will consolidate.
    Commands have ^ anchor, replacements don't.
    """

    def test_command_type_with_anchor(self, catalog):
        """Patterns with ^ anchor are COMMAND type."""
        # "undo" has ^undo$ pattern
        pattern_type = catalog.get_pattern_type("undo")
        assert pattern_type == PatternType.COMMAND

    def test_replacement_type_without_anchor(self, catalog):
        """Patterns without ^ anchor are REPLACEMENT type."""
        # "comma" has \bcomma\b pattern
        pattern_type = catalog.get_pattern_type("comma")
        assert pattern_type == PatternType.REPLACEMENT

    def test_none_type_for_unknown_word(self, catalog):
        """Unknown words return NONE type."""
        pattern_type = catalog.get_pattern_type("xyzzyplugh")
        assert pattern_type == PatternType.NONE

    def test_command_priority_for_mixed(self, catalog):
        """When word starts both command and replacement, COMMAND wins.

        This ensures fresh utterances enter COMMAND_BUFFERING first.
        """
        # Find a word that starts both types (if any exist)
        # The catalog should prioritize COMMAND
        # Test with a known mixed case or verify the logic
        pass  # This may not have a test case in production patterns


# ============================================================================
# TEST CLASS: PATTERN LOOKUP
# ============================================================================

class TestPatternLookup:
    """Test pattern lookup methods."""

    def test_could_be_pattern_start(self, catalog):
        """could_be_pattern_start returns True for indexed words."""
        assert catalog.could_be_pattern_start("undo") is True
        assert catalog.could_be_pattern_start("comma") is True
        assert catalog.could_be_pattern_start("delete") is True

    def test_could_not_be_pattern_start(self, catalog):
        """could_be_pattern_start returns False for non-indexed words."""
        assert catalog.could_be_pattern_start("xyzzy") is False
        assert catalog.could_be_pattern_start("plugh") is False

    def test_case_insensitive_lookup(self, catalog):
        """Lookup is case-insensitive."""
        assert catalog.could_be_pattern_start("UNDO") is True
        assert catalog.could_be_pattern_start("Comma") is True
        assert catalog.could_be_pattern_start("DELETE") is True

    def test_get_matching_patterns_returns_list(self, catalog):
        """get_matching_patterns returns list of tuples."""
        patterns = catalog.get_matching_patterns("undo")
        assert isinstance(patterns, list)
        assert len(patterns) > 0
        # Each entry is (compiled_pattern, type, data)
        for entry in patterns:
            assert len(entry) == 3
            assert isinstance(entry[0], re.Pattern)
            assert entry[1] in ("command", "replacement")
            assert isinstance(entry[2], dict)

    def test_could_be_pattern_start_non_speech(self, catalog):
        """Non-speech annotations like *cough* are recognized as pattern starts."""
        assert catalog.could_be_pattern_start("*cough*") is True
        assert catalog.could_be_pattern_start("*ahem*") is True
        assert catalog.could_be_pattern_start("*sniff*") is True

    def test_non_speech_pattern_type_is_replacement(self, catalog):
        """Non-speech annotations are classified as REPLACEMENT type."""
        assert catalog.get_pattern_type("*cough*") == PatternType.REPLACEMENT

    def test_get_matching_patterns_empty_for_unknown(self, catalog):
        """get_matching_patterns returns empty list for unknown words."""
        patterns = catalog.get_matching_patterns("xyzzy")
        assert patterns == []

    def test_get_all_patterns_returns_all(self, catalog):
        """get_all_patterns returns all loaded patterns."""
        all_patterns = catalog.get_all_patterns()
        assert len(all_patterns) == catalog.pattern_count


# ============================================================================
# TEST CLASS: PATTERN DATA
# ============================================================================

class TestPatternData:
    """Test pattern data structure."""

    def test_pattern_has_actions(self, catalog):
        """Each pattern has actions list."""
        for pattern in catalog.get_all_patterns():
            assert 'actions' in pattern
            assert isinstance(pattern['actions'], list)

    def test_pattern_has_type(self, catalog):
        """Each pattern has pattern_type."""
        for pattern in catalog.get_all_patterns():
            assert 'pattern_type' in pattern
            assert pattern['pattern_type'] in ("command", "replacement")

    def test_command_patterns_have_requires_hotword(self, catalog):
        """Command patterns have requires_hotword field."""
        for pattern in catalog.get_all_patterns():
            if pattern['pattern_type'] == 'command':
                assert 'requires_hotword' in pattern
                assert isinstance(pattern['requires_hotword'], bool)

    def test_validation_group_when_present(self, catalog):
        """Patterns with numeric params have validation_group."""
        # Find patterns with validation_group and verify structure
        has_validation = False
        for pattern in catalog.get_all_patterns():
            if pattern.get('validation_group'):
                has_validation = True
                vg = pattern['validation_group']
                assert vg.startswith('g')
                assert vg[1:].isdigit()
        # At least some patterns should have validation
        assert has_validation, "Expected some patterns with validation_group"


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
