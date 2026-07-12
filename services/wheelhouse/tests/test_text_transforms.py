"""Tests for text_transforms module."""
import pytest
from speech.text_transforms import auto_compress_spelled_letters


class TestAutoCompressSpelledLetters:
    """Test cases for auto_compress_spelled_letters function."""

    def test_full_spelled_word(self):
        """A fully spelled-out word should be compressed."""
        assert auto_compress_spelled_letters("w a s h i n g t o n") == "washington"

    def test_mid_sentence(self):
        """Spelled letters mid-sentence should be compressed."""
        result = auto_compress_spelled_letters("hello w a s h i n g t o n world")
        assert result == "hello washington world"

    def test_minimum_threshold_met(self):
        """Exactly 3 letters should trigger compression."""
        assert auto_compress_spelled_letters("c a t") == "cat"

    def test_below_threshold(self):
        """Only 2 letters should NOT trigger compression."""
        assert auto_compress_spelled_letters("a b") == "a b"

    def test_four_letters(self):
        """4 letters should be compressed."""
        assert auto_compress_spelled_letters("c a t s") == "cats"

    def test_non_alpha_unchanged(self):
        """Non-alphabetic sequences should not be affected."""
        assert auto_compress_spelled_letters("1 2 3") == "1 2 3"

    def test_empty_string(self):
        """Empty string should return empty."""
        assert auto_compress_spelled_letters("") == ""

    def test_no_spelled_letters(self):
        """Normal text without spelled letters should pass through."""
        assert auto_compress_spelled_letters("hello world") == "hello world"

    def test_mixed_case(self):
        """Mixed case letters should be compressed."""
        assert auto_compress_spelled_letters("A B C") == "ABC"

    def test_multiple_sequences(self):
        """Multiple spelled sequences in one string."""
        result = auto_compress_spelled_letters("say c a t and d o g")
        assert result == "say cat and dog"

    def test_single_letter_unchanged(self):
        """A single letter should not be affected."""
        assert auto_compress_spelled_letters("a") == "a"
