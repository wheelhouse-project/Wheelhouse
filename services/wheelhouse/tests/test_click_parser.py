"""Tests for the ClickCommandParser (wh-vjwdl).

Covers the v5 "Voice Command Grammar" parse rules: the text after the spoken
word "click" is turned into an ElementQuery. An optional trailing role keyword
(button/link/menu/tab/checkbox/check box/box/field/input) maps to a UIA
control-type NAME string; the remaining words (with a leading "the" dropped)
form the name. ordinal and spatial are always None in this Phase 1 slice.

Authoritative grammar: docs/plans/2026-05-21-voice-element-clicking-design-v5.md
"Voice Command Grammar" (the role-keyword map and the worked examples).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.click_parser import ClickCommandParser
from ui.element_types import ElementQuery


class TestV5Examples:
    """Every worked example in the v5 grammar table."""

    @pytest.mark.parametrize("target,name,role", [
        ("cancel", "cancel", None),
        ("the cancel button", "cancel", "Button"),
        ("the commands link", "commands", "Hyperlink"),
        ("file menu", "file", "MenuItem"),
        ("the search box", "search", "Edit"),
        ("settings", "settings", None),
    ])
    def test_v5_example(self, target, name, role):
        q = ClickCommandParser.parse(target)
        assert q is not None
        assert q.name == name
        assert q.role == role
        assert q.ordinal is None
        assert q.spatial is None
        assert q.raw_utterance == target


class TestRoleKeywordMap:
    """Each spoken role keyword maps to the correct control-type name."""

    @pytest.mark.parametrize("keyword,role", [
        ("button", "Button"),
        ("link", "Hyperlink"),
        ("menu", "MenuItem"),
        ("tab", "TabItem"),
        ("checkbox", "CheckBox"),
        ("box", "Edit"),
        ("field", "Edit"),
        ("input", "Edit"),
    ])
    def test_single_word_role_keyword(self, keyword, role):
        # "save <keyword>" -> name=save, role=<mapped>
        q = ClickCommandParser.parse(f"save {keyword}")
        assert q is not None
        assert q.name == "save"
        assert q.role == role

    def test_two_word_check_box(self):
        q = ClickCommandParser.parse("remember check box")
        assert q is not None
        assert q.name == "remember"
        assert q.role == "CheckBox"

    def test_two_word_check_box_with_the(self):
        q = ClickCommandParser.parse("the remember me check box")
        assert q is not None
        assert q.name == "remember me"
        assert q.role == "CheckBox"

    def test_no_role_spoken_is_none(self):
        q = ClickCommandParser.parse("some random thing")
        assert q is not None
        assert q.name == "some random thing"
        assert q.role is None


class TestLeadingThe:
    """A leading 'the' is dropped from the name."""

    def test_leading_the_dropped(self):
        q = ClickCommandParser.parse("the settings")
        assert q is not None
        assert q.name == "settings"
        assert q.role is None

    def test_leading_the_dropped_with_role(self):
        q = ClickCommandParser.parse("the cancel button")
        assert q is not None
        assert q.name == "cancel"
        assert q.role == "Button"

    def test_non_leading_the_kept(self):
        # "the" only stripped when it leads; an internal "the" stays.
        q = ClickCommandParser.parse("close the dialog")
        assert q is not None
        assert q.name == "close the dialog"
        assert q.role is None


class TestEmptyAndWhitespace:
    """Empty / whitespace-only input returns None (falls through to dictation)."""

    @pytest.mark.parametrize("target", ["", "   ", "\t", "the", "  the  "])
    def test_returns_none(self, target):
        # "the" alone collapses to empty name after dropping the article.
        assert ClickCommandParser.parse(target) is None


class TestRoleKeywordOnly:
    """Documented behaviour: a role keyword with no name word becomes the name.

    "click button" -> the single word "button" is the spoken target; there is
    no other word to serve as the name, so the role keyword is treated as the
    name (role=None) rather than yielding an empty-name query. Stripping it
    would leave nothing to search for. A control literally named "button" is
    a more useful target than an empty query.
    """

    @pytest.mark.parametrize("target,name", [
        ("button", "button"),
        ("link", "link"),
        ("menu", "menu"),
        ("box", "box"),
        ("the button", "button"),
        ("check box", "check box"),
    ])
    def test_role_keyword_only_becomes_name(self, target, name):
        q = ClickCommandParser.parse(target)
        assert q is not None
        assert q.name == name
        assert q.role is None


class TestNormalization:
    """Surrounding/internal whitespace is normalized; raw_utterance is preserved."""

    def test_extra_whitespace_collapsed(self):
        q = ClickCommandParser.parse("  the   cancel    button  ")
        assert q is not None
        assert q.name == "cancel"
        assert q.role == "Button"
        # raw_utterance keeps exactly what was passed in.
        assert q.raw_utterance == "  the   cancel    button  "

    def test_returns_element_query_type(self):
        q = ClickCommandParser.parse("ok")
        assert isinstance(q, ElementQuery)


class TestTrailingPunctuation:
    """Local STT appends sentence punctuation to the final word; the parser
    strips it so the name (and trailing role detection) are not polluted."""

    @pytest.mark.parametrize("target,name,role", [
        ("cancel.", "cancel", None),
        ("cancel,", "cancel", None),
        ("the cancel button.", "cancel", "Button"),
        ("the cancel button?", "cancel", "Button"),
        ("file menu!", "file", "MenuItem"),
        ("settings...", "settings", None),
    ])
    def test_trailing_punct_stripped(self, target, name, role):
        q = ClickCommandParser.parse(target)
        assert q is not None
        assert q.name == name
        assert q.role == role
        # raw_utterance still preserves the original spoken text.
        assert q.raw_utterance == target

    @pytest.mark.parametrize("target", [".", "the .", "  ?  ", "the.", "THE!", "the?"])
    def test_punctuation_only_returns_none(self, target):
        # A target that collapses to punctuation only -- or to a lone (possibly
        # punctuated) article -- falls through to dictation. "the." must NOT
        # become name="the": punctuation is stripped before the article drop
        # (wh-9f3t.52.1).
        assert ClickCommandParser.parse(target) is None
