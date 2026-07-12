"""TextPerfector line-separator and paragraph-separator handling.

wh-perfector-line-paragraph-sep: Qt's QPlainTextEdit interprets
Shift+Enter as InsertLineSeparator and inserts U+2028 LINE SEPARATOR.
The 'new line' voice command fires hk(shift, enter), so its newline
shows up as U+2028 in the editor. UIA TextPattern then returns that
character to the input process when it reads preceding_chars before
inserting the next dictated word. TextPerfector's spacing decision
relies on str.endswith with string.whitespace, which is
' \\t\\n\\r\\v\\f' and does NOT include U+2028 or U+2029. The result
was that the next word after a Shift+Enter received an unwanted leading
space.

This test file pins the new behavior: U+2028 (LINE SEPARATOR) and
U+2029 (PARAGRAPH SEPARATOR) are treated as whitespace for the
spacing decision, alongside the existing whitespace characters.
"""
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from ui.text_perfector import TextPerfector  # noqa: E402


LINE_SEPARATOR = " "
PARAGRAPH_SEPARATOR = " "


@pytest.fixture
def perfector():
    return TextPerfector()


class TestUnicodeLineParagraphSeparators:
    def test_no_leading_space_after_line_separator(self, perfector):
        # Shift+Enter in QPlainTextEdit inserts U+2028. The next dictated
        # word must NOT receive a leading space.
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text" + LINE_SEPARATOR,
            has_selection=False,
        )
        assert not result.startswith(" "), (
            f"Expected no leading space after U+2028, got {result!r}"
        )

    def test_no_leading_space_after_paragraph_separator(self, perfector):
        # U+2029 is the paragraph separator. Same rule applies.
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text" + PARAGRAPH_SEPARATOR,
            has_selection=False,
        )
        assert not result.startswith(" "), (
            f"Expected no leading space after U+2029, got {result!r}"
        )


class TestExistingWhitespaceStillSuppresses:
    """Regression check: the standard whitespace cases must still
    suppress the leading space, otherwise normal sentence flow breaks.
    """

    def test_no_leading_space_after_newline(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text\n",
            has_selection=False,
        )
        assert not result.startswith(" ")

    def test_no_leading_space_after_space(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text ",
            has_selection=False,
        )
        assert not result.startswith(" ")

    def test_no_leading_space_after_tab(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text\t",
            has_selection=False,
        )
        assert not result.startswith(" ")


class TestNonWhitespaceStillAddsSpace:
    """Regression check: a real word-after-word case still adds the
    expected single leading space, otherwise sentences run together.
    """

    def test_leading_space_after_word(self, perfector):
        result = perfector.perfected_string(
            insertion_string="world",
            preceding_chars="hello",
            has_selection=False,
        )
        assert result.startswith(" "), (
            f"Expected leading space after a word, got {result!r}"
        )


class TestCapitalizationAfterNewline:
    """wh-cap-after-newline: a newline character in preceding_chars must
    act as a sentence boundary so the next dictated word is capitalized.

    Both the 'new line' voice command (which fires hk(enter) and yields
    a \\n in plain text targets) and the Shift+Enter pathway in the
    terminal dictation editor (which inserts U+2028 LINE SEPARATOR)
    should trigger sentence-start capitalization for the following
    word."""

    def test_capitalizes_after_lf(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text\n",
            has_selection=False,
        )
        assert "Hello" in result, (
            f"Expected the next word after \\n to be capitalized, got {result!r}"
        )

    def test_capitalizes_after_cr(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text\r",
            has_selection=False,
        )
        assert "Hello" in result, (
            f"Expected the next word after \\r to be capitalized, got {result!r}"
        )

    def test_capitalizes_after_line_separator(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text" + LINE_SEPARATOR,
            has_selection=False,
        )
        assert "Hello" in result, (
            f"Expected the next word after U+2028 to be capitalized, got {result!r}"
        )

    def test_capitalizes_after_paragraph_separator(self, perfector):
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text" + PARAGRAPH_SEPARATOR,
            has_selection=False,
        )
        assert "Hello" in result, (
            f"Expected the next word after U+2029 to be capitalized, got {result!r}"
        )

    def test_capitalizes_after_newline_with_trailing_spaces(self, perfector):
        """Trailing horizontal whitespace after the newline must not block
        capitalization. A real editor may leave trailing spaces on the
        previous line, and the user-visible boundary is still the newline."""
        result = perfector.perfected_string(
            insertion_string="hello",
            preceding_chars="prior text\n   ",
            has_selection=False,
        )
        assert "Hello" in result, (
            f"Expected capitalization after newline + spaces, got {result!r}"
        )

    def test_does_not_capitalize_mid_sentence(self, perfector):
        """Sanity check: a normal mid-sentence word must not be
        capitalized. Catches an over-broad rewrite that would treat any
        preceding_chars as a sentence boundary."""
        result = perfector.perfected_string(
            insertion_string="world",
            preceding_chars="hello",
            has_selection=False,
        )
        assert "world" in result
        assert "World" not in result, (
            f"Mid-sentence word must not be capitalized, got {result!r}"
        )
