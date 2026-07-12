"""Unit tests for TextPerfector - text formatting logic.

These tests verify the pure logic functions for spacing, capitalization,
and escape sequence handling. No UI dependencies.
"""
import pytest
from services.wheelhouse.ui.text_perfector import TextPerfector


class TestTextPerfector:
    """Test suite for TextPerfector class."""

    @pytest.fixture
    def perfector(self):
        """Create a TextPerfector instance for testing."""
        return TextPerfector()

    # ========================================================================
    # SPACING TESTS
    # ========================================================================

    def test_spacing_empty_preceding(self, perfector):
        """First word in document should be capitalized, no space prefix."""
        result = perfector.perfected_string("hello", preceding_chars="")
        assert result == "Hello"

    def test_spacing_with_preceding_text(self, perfector):
        """Normal word after text should have space prefix."""
        result = perfector.perfected_string("world", preceding_chars="hello")
        assert result == " world"

    def test_spacing_punctuation_no_prefix(self, perfector):
        """Punctuation should not have space prefix."""
        result = perfector.perfected_string(",", preceding_chars="word")
        assert result == ","
        result = perfector.perfected_string(".", preceding_chars="word")
        assert result == "."

    def test_spacing_after_whitespace(self, perfector):
        """Word after whitespace should not have space prefix."""
        result = perfector.perfected_string("hello", preceding_chars="word ")
        assert result == "hello"

    def test_spacing_after_opening_bracket(self, perfector):
        """Word after opening bracket should not have space prefix."""
        result = perfector.perfected_string("hello", preceding_chars="(")
        assert result == "hello"
        result = perfector.perfected_string("hello", preceding_chars="[")
        assert result == "hello"

    def test_spacing_with_selection(self, perfector):
        """Text replacing selection should not have space prefix."""
        result = perfector.perfected_string("replacement", preceding_chars="before", has_selection=True)
        assert result == "replacement"

    # ========================================================================
    # CAPITALIZATION TESTS
    # ========================================================================

    def test_capitalize_first_word(self, perfector):
        """First word should be capitalized."""
        result = perfector.perfected_string("hello", preceding_chars="")
        assert result == "Hello"

    def test_capitalize_after_period(self, perfector):
        """Word after period should be capitalized."""
        result = perfector.perfected_string("hello", preceding_chars="end.")
        assert result == " Hello"

    def test_capitalize_after_question_mark(self, perfector):
        """Word after question mark should be capitalized."""
        result = perfector.perfected_string("yes", preceding_chars="really?")
        assert result == " Yes"

    def test_capitalize_after_exclamation(self, perfector):
        """Word after exclamation should be capitalized."""
        result = perfector.perfected_string("wow", preceding_chars="stop!")
        assert result == " Wow"

    def test_no_capitalize_mid_sentence(self, perfector):
        """Word mid-sentence should not be capitalized."""
        result = perfector.perfected_string("world", preceding_chars="hello")
        assert result == " world"

    def test_capitalize_after_period_with_quote(self, perfector):
        """Word after period+quote should be capitalized."""
        result = perfector.perfected_string("she", preceding_chars='said."')
        assert result == " She"

    def test_no_double_capitalize(self, perfector):
        """Already capitalized word should stay capitalized."""
        result = perfector.perfected_string("Bob", preceding_chars="")
        assert result == "Bob"

    # ========================================================================
    # ESCAPE SEQUENCE TESTS
    # ========================================================================

    def test_newline_escape(self, perfector):
        """\\n should be converted to newline character."""
        result = perfector.perfected_string("\\n", preceding_chars="")
        assert result == "\n"

    def test_tab_escape(self, perfector):
        """\\t should be converted to tab character."""
        result = perfector.perfected_string("\\t", preceding_chars="")
        assert result == "\t"

    def test_carriage_return_escape(self, perfector):
        """\\r should be converted to carriage return."""
        result = perfector.perfected_string("\\r", preceding_chars="")
        assert result == "\r"

    def test_backslash_escape(self, perfector):
        """\\\\ should be converted to single backslash."""
        result = perfector.perfected_string("\\\\", preceding_chars="")
        assert result == "\\"

    def test_mixed_escape_sequences(self, perfector):
        """Multiple escape sequences should all be converted."""
        result = perfector.perfected_string("line1\\nline2\\ttab", preceding_chars="")
        assert result == "line1\nline2\ttab"

    def test_escape_prevents_spacing(self, perfector):
        """Escape sequences should prevent normal spacing rules."""
        result = perfector.perfected_string("\\n", preceding_chars="text")
        assert result == "\n"  # No space prefix

    # ========================================================================
    # INTEGRATION TESTS
    # ========================================================================

    def test_complete_sentence_flow(self, perfector):
        """Test a complete sentence construction."""
        # First word
        result1 = perfector.perfected_string("the", preceding_chars="")
        assert result1 == "The"

        # Second word
        result2 = perfector.perfected_string("cat", preceding_chars="The")
        assert result2 == " cat"

        # Punctuation
        result3 = perfector.perfected_string(",", preceding_chars="The cat")
        assert result3 == ","

        # After comma
        result4 = perfector.perfected_string("sat", preceding_chars="The cat,")
        assert result4 == " sat"

        # Period
        result5 = perfector.perfected_string(".", preceding_chars="The cat, sat")
        assert result5 == "."

        # New sentence
        result6 = perfector.perfected_string("it", preceding_chars="The cat, sat.")
        assert result6 == " It"

    # ========================================================================
    # ACRONYM CASE FIX TESTS
    # ========================================================================

    def test_acronym_fix_gpu(self, perfector):
        """'gPU' should be corrected to 'GPU'."""
        result = perfector._fix_acronym_case("gPU")
        assert result == "GPU"

    def test_acronym_fix_cpu(self, perfector):
        """'cPU' should be corrected to 'CPU'."""
        result = perfector._fix_acronym_case("cPU")
        assert result == "CPU"

    def test_acronym_fix_two_chars(self, perfector):
        """'gP' (two chars) should be corrected to 'GP'."""
        result = perfector._fix_acronym_case("gP")
        assert result == "GP"

    def test_acronym_fix_single_char_unchanged(self, perfector):
        """Single character 'a' should not be changed."""
        result = perfector._fix_acronym_case("a")
        assert result == "a"

    def test_acronym_fix_normal_word_unchanged(self, perfector):
        """Normal lowercase word 'hello' should not be changed."""
        result = perfector._fix_acronym_case("hello")
        assert result == "hello"

    def test_acronym_fix_already_capitalized_unchanged(self, perfector):
        """Already correct 'Hello' should not be changed (first char not lowercase)."""
        result = perfector._fix_acronym_case("Hello")
        assert result == "Hello"

    def test_acronym_fix_partial_uppercase_unchanged(self, perfector):
        """'aPIs' should not be changed -- 'Is' has lowercase 's'."""
        result = perfector._fix_acronym_case("aPIs")
        assert result == "aPIs"

    def test_acronym_fix_multi_word(self, perfector):
        """Multi-word input should fix each qualifying word independently."""
        result = perfector._fix_acronym_case("the gPU is fast")
        assert result == "the GPU is fast"

    def test_acronym_fix_multi_word_multiple_acronyms(self, perfector):
        """Multiple misrecognized acronyms in one string should all be fixed."""
        result = perfector._fix_acronym_case("gPU and cPU")
        assert result == "GPU and CPU"

    def test_acronym_fix_integrated_perfected_string(self, perfector):
        """'gPU' through perfected_string (first word) should produce 'GPU'."""
        result = perfector.perfected_string("gPU", preceding_chars="")
        assert result == "GPU"

    def test_acronym_fix_integrated_mid_sentence(self, perfector):
        """'gPU' mid-sentence through perfected_string should produce ' GPU'."""
        result = perfector.perfected_string("gPU", preceding_chars="the")
        assert result == " GPU"
