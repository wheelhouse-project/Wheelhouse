"""Text formatting and perfection logic.

This module handles intelligent text formatting including:
- Spacing rules (when to add prefix space)
- Capitalization (sentence-start detection)
- Escape sequence processing (\\n, \\t, etc.)
- Punctuation-aware formatting

All functions are pure logic with no UI dependencies, making them
easily testable and maintainable.
"""
import string
import unicodedata
import re
from typing import Optional


class TextPerfector:
    """Applies intelligent spacing, capitalization, and escape sequence handling to dictated text.

    This class encapsulates all pure text transformation logic, with no dependencies
    on UI state, clipboard, or Windows APIs. All methods are deterministic and testable.
    """

    def perfected_string(
        self,
        insertion_string: str,
        preceding_chars: str = "",
        has_selection: bool = False,
        capitalize: bool = True,
        **kwargs
    ) -> str:
        """Apply all formatting rules to create the final insertion string.

        Main entry point for text perfection. Applies spacing and capitalization
        rules based on context.

        Args:
            insertion_string: The raw text to be inserted
            preceding_chars: Text that appears before the insertion point (for context)
            has_selection: Whether text is currently selected (affects spacing)
            capitalize: When False, skip sentence-start capitalization and emit
                the spacing-adjusted text with its original casing. The terminal
                dictation editor passes False because its contents are submitted
                verbatim to a shell where case is significant
                (wh-editor-retract-dup.1.2). Defaults True for every other
                caller, preserving the prior behaviour.
            **kwargs: Additional context (ignored, for compatibility)

        Returns:
            Formatted text ready for insertion

        Examples:
            >>> p = TextPerfector()
            >>> p.perfected_string("hello", preceding_chars="")
            "Hello"
            >>> p.perfected_string("hello", preceding_chars="world")
            " hello"
            >>> p.perfected_string(",", preceding_chars="word")
            ","
            >>> p.perfected_string("git", preceding_chars="", capitalize=False)
            "git"
        """
        # Handle escape sequences and control characters literally
        if self._is_literal_insertion(insertion_string):
            return self._process_escape_sequences(insertion_string)

        # Fix misrecognized acronyms (e.g., "gPU" -> "GPU")
        insertion_string = self._fix_acronym_case(insertion_string)

        # Apply spacing logic
        prefix = self._determine_spacing_prefix(
            insertion_string,
            preceding_chars,
            has_selection
        )

        # Apply capitalization logic (skipped when the caller opts out)
        if capitalize:
            text = self._apply_capitalization(
                insertion_string,
                preceding_chars
            )
        else:
            text = insertion_string

        return prefix + text

    def _determine_spacing_prefix(
        self,
        insertion_string: str,
        preceding_chars: str,
        has_selection: bool
    ) -> str:
        """Determine if we need a space prefix before the insertion.

        Rules:
        1. No space if insertion is punctuation-only
        2. No space if preceding text ends with whitespace or opening bracket
        3. No space if there's a selection (replacing selected text)
        4. Otherwise, add space

        Args:
            insertion_string: Text to be inserted
            preceding_chars: Text before insertion point
            has_selection: Whether text is selected

        Returns:
            Either ' ' or '' (empty string)
        """
        # Check if insertion is only punctuation
        is_punctuation_only = all(
            c in string.punctuation
            for c in insertion_string.strip()
        )

        # Check if preceding text ends with whitespace or opening bracket or slash or backslash.
        # wh-perfector-line-paragraph-sep: include U+2028 (LINE SEPARATOR)
        # and U+2029 (PARAGRAPH SEPARATOR). Qt's QPlainTextEdit inserts
        # U+2028 for Shift+Enter (the 'new line' voice command's hotkey),
        # and UIA TextPattern returns those characters verbatim. Without
        # them in the whitespace set the next dictated word receives an
        # unwanted leading space.
        ends_with_whitespace = (
            not preceding_chars or
            preceding_chars.endswith(
                tuple(string.whitespace + '([{/\\  ')
            )
        )

        # Determine if prefix space is needed
        if is_punctuation_only or ends_with_whitespace or has_selection:
            return ''

        return ' '

    def _apply_capitalization(
        self,
        insertion_string: str,
        preceding_chars: str
    ) -> str:
        """Apply sentence-start capitalization if appropriate.

        Capitalizes the first letter if:
        - There's no preceding text (start of document), OR
        - Preceding text ends with sentence-ending punctuation (. ! ?), OR
        - Preceding text ends with a newline character (\\n, \\r, U+2028,
          or U+2029), optionally followed by horizontal whitespace. The
          'new line' voice command inserts \\n in plain-text targets and
          U+2028 in the terminal dictation editor (Qt's QPlainTextEdit
          interprets Shift+Enter as InsertLineSeparator); both act as
          sentence boundaries (wh-cap-after-newline).

        Args:
            insertion_string: Text to capitalize
            preceding_chars: Context for determining if we're at sentence start

        Returns:
            Text with capitalization applied
        """
        # Detect a trailing newline boundary before the standard cleaning
        # path, which would otherwise strip \\n / \\r (category Cc) and
        # remove U+2028 / U+2029 via .strip(), erasing the boundary.
        ends_with_newline = bool(
            re.search(r'[\n\r  ][ \t]*$', preceding_chars)
        )

        # Clean control characters from preceding text for analysis
        cleaned_preceding = "".join(
            ch for ch in preceding_chars
            if unicodedata.category(ch)[0] != 'C'
        ).strip()

        # Check if we're at sentence start
        should_capitalize = (
            not cleaned_preceding or
            ends_with_newline or
            bool(re.search(r'[.!?][\'\")]*$', cleaned_preceding))
        )

        if not should_capitalize or not insertion_string:
            return insertion_string

        # Only capitalize if first character is a lowercase letter
        first_char = insertion_string[0]
        if first_char.isalpha() and not first_char.isupper():
            return first_char.upper() + insertion_string[1:]

        return insertion_string

    def _fix_acronym_case(self, text: str) -> str:
        """Fix misrecognized acronym casing (e.g. 'gPU' -> 'GPU').

        Whisper sometimes transcribes acronyms with a lowercase first letter and
        all-uppercase remaining letters (e.g. "gPU", "cPU"). This method detects
        that pattern and uppercases the entire word.

        Rule: if a word has length >= 2, its first character is lowercase, and
        ALL remaining characters are uppercase letters, uppercase the whole word.

        Applies the fix to each word independently for multi-word input.

        Args:
            text: Input text, possibly containing misrecognized acronyms

        Returns:
            Text with qualifying words uppercased
        """
        def _fix_word(word: str) -> str:
            if len(word) < 2:
                return word
            first, rest = word[0], word[1:]
            if first.islower() and rest.isupper() and rest.isalpha():
                return word.upper()
            return word

        return " ".join(_fix_word(w) for w in text.split(" "))

    def _is_literal_insertion(self, text: str) -> bool:
        """Check if text contains escape sequences requiring literal treatment.

        Args:
            text: Text to check

        Returns:
            True if text contains escape sequences
        """
        return '\\n' in text or '\\t' in text or '\\r' in text or text.startswith('\\')

    def _process_escape_sequences(self, text: str) -> str:
        """Convert escape sequences to their actual characters.

        Handles common escape sequences:
        - \\n → newline
        - \\t → tab
        - \\r → carriage return
        - \\\\ → backslash

        Args:
            text: Text containing escape sequences

        Returns:
            Text with escape sequences converted to actual characters
        """
        escape_map = {
            '\\n': '\n',
            '\\t': '\t',
            '\\r': '\r',
            '\\\\': '\\'
        }

        result = text
        for escape_seq, actual_char in escape_map.items():
            result = result.replace(escape_seq, actual_char)

        return result
