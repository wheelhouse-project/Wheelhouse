"""Tests for NavigationParser - utterance to NavigationCommand conversion."""

import pytest

from services.wheelhouse.speech.navigation.models import NavigationCommand
from services.wheelhouse.speech.navigation.parser import NavigationParser


class TestRelativeCommands:
    """go/grab + direction [count] [unit]"""

    def test_go_right_default(self):
        """'go right' = 1 character right."""
        result = NavigationParser.parse("go right")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="right", count=1, unit="character")]

    def test_go_left_default(self):
        result = NavigationParser.parse("go left")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="left", count=1, unit="character")]

    def test_go_right_with_word_count(self):
        """'go left three' = 3 characters left."""
        result = NavigationParser.parse("go left three")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="left", count=3, unit="character")]

    def test_go_right_with_digit_count(self):
        """'go right 5' = 5 characters right."""
        result = NavigationParser.parse("go right 5")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="right", count=5, unit="character")]

    def test_go_right_with_count_and_unit(self):
        """'go right two words' = 2 words right."""
        result = NavigationParser.parse("go right two words")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="right", count=2, unit="word")]

    def test_go_left_paragraph(self):
        """'go left paragraph' = 1 paragraph left."""
        result = NavigationParser.parse("go left paragraph")
        assert result == [NavigationCommand(verb="go", kind="relative", direction="left", count=1, unit="paragraph")]

    def test_plural_unit_characters(self):
        """'go right three characters' normalizes to 'character'."""
        result = NavigationParser.parse("go right three characters")
        assert result[0].unit == "character"

    def test_plural_unit_words(self):
        """'go right two words' normalizes to 'word'."""
        result = NavigationParser.parse("go right two words")
        assert result[0].unit == "word"

    def test_plural_unit_paragraphs(self):
        """'go left two paragraphs' normalizes to 'paragraph'."""
        result = NavigationParser.parse("go left two paragraphs")
        assert result[0].unit == "paragraph"

    def test_count_clamped_to_50(self):
        """Counts above 50 are clamped, not rejected."""
        result = NavigationParser.parse("go right 99")
        assert result[0].count == 50

    def test_grab_relative(self):
        """'grab right three words' = select 3 words right."""
        result = NavigationParser.parse("grab right three words")
        assert result == [NavigationCommand(verb="grab", kind="relative", direction="right", count=3, unit="word")]


class TestLandmarkCommands:
    """go + landmark / grab to + landmark"""

    def test_go_home(self):
        result = NavigationParser.parse("go home")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="home")]

    def test_go_end(self):
        result = NavigationParser.parse("go end")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="end")]

    def test_go_top(self):
        result = NavigationParser.parse("go top")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="top")]

    def test_go_bottom(self):
        result = NavigationParser.parse("go bottom")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="bottom")]

    def test_go_start_of_word(self):
        result = NavigationParser.parse("go start of word")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="start_of_word")]

    def test_go_beginning_of_word(self):
        """'beginning' is alias for 'start'."""
        result = NavigationParser.parse("go beginning of word")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="start_of_word")]

    def test_go_end_of_word(self):
        result = NavigationParser.parse("go end of word")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="end_of_word")]

    def test_go_start_of_paragraph(self):
        result = NavigationParser.parse("go start of paragraph")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="start_of_paragraph")]

    def test_go_end_of_paragraph(self):
        result = NavigationParser.parse("go end of paragraph")
        assert result == [NavigationCommand(verb="go", kind="landmark", landmark="end_of_paragraph")]

    def test_grab_to_end(self):
        """'grab to end' = select to end of line."""
        result = NavigationParser.parse("grab to end")
        assert result == [NavigationCommand(verb="grab", kind="landmark", landmark="end")]

    def test_grab_to_end_of_paragraph(self):
        result = NavigationParser.parse("grab to end of paragraph")
        assert result == [NavigationCommand(verb="grab", kind="landmark", landmark="end_of_paragraph")]

    def test_grab_to_top(self):
        result = NavigationParser.parse("grab to top")
        assert result == [NavigationCommand(verb="grab", kind="landmark", landmark="top")]


class TestChaining:
    """Commands joined by 'then'."""

    def test_go_home_then_grab_to_end(self):
        result = NavigationParser.parse("go home then grab to end")
        assert len(result) == 2
        assert result[0] == NavigationCommand(verb="go", kind="landmark", landmark="home")
        assert result[1] == NavigationCommand(verb="grab", kind="landmark", landmark="end")

    def test_go_left_then_grab_right(self):
        result = NavigationParser.parse("go left two words then grab right four words")
        assert len(result) == 2
        assert result[0] == NavigationCommand(verb="go", kind="relative", direction="left", count=2, unit="word")
        assert result[1] == NavigationCommand(verb="grab", kind="relative", direction="right", count=4, unit="word")

    def test_triple_chain(self):
        result = NavigationParser.parse("go home then go right three words then grab to end")
        assert len(result) == 3


class TestInvalidInput:
    """Invalid input returns None (dictation fallthrough)."""

    def test_go_unknown_word(self):
        """'go banana' has no valid direction or landmark."""
        assert NavigationParser.parse("go banana") is None

    def test_go_right_trailing_junk(self):
        """'go right three words extra' has trailing tokens after valid parse."""
        assert NavigationParser.parse("go right three words extra") is None

    def test_grab_landmark_without_to(self):
        """'grab home' requires 'to' before landmark. Falls through."""
        assert NavigationParser.parse("grab home") is None

    def test_invalid_segment_in_chain(self):
        """One bad segment fails the entire chain."""
        assert NavigationParser.parse("go home then go banana") is None

    def test_empty_after_verb(self):
        """'go ' with trailing space but no content. Pattern won't match this
        (^go\\s+.+ requires chars), but parser handles defensively."""
        assert NavigationParser.parse("go ") is None

    def test_unknown_verb(self):
        """Only 'go' and 'grab' are valid verbs."""
        assert NavigationParser.parse("move right") is None

    def test_go_end_of_banana(self):
        """'go end of banana' - invalid unit after 'of'."""
        assert NavigationParser.parse("go end of banana") is None

    def test_grab_to_without_landmark(self):
        """'grab to banana' - invalid landmark after 'to'."""
        assert NavigationParser.parse("grab to banana") is None


class TestCaseInsensitivity:
    """Parser should handle mixed case from STT."""

    def test_mixed_case(self):
        result = NavigationParser.parse("Go Right Three Words")
        assert result[0] == NavigationCommand(verb="go", kind="relative", direction="right", count=3, unit="word")
