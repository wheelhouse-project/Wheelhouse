"""Tests for cursor navigation utterance parser."""

import pytest

from speech.navigation.parser import NavigationParser
from speech.navigation.models import NavigationCommand


class TestLandmarks:
    @pytest.mark.parametrize("utterance,landmark", [
        ("go home", "home"),
        ("go end", "end"),
        ("go top", "top"),
        ("go bottom", "bottom"),
    ])
    def test_simple_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    @pytest.mark.parametrize("utterance,landmark", [
        ("go start of word", "start_of_word"),
        ("go beginning of word", "start_of_word"),
        ("go end of word", "end_of_word"),
        ("go start of paragraph", "start_of_paragraph"),
        ("go end of paragraph", "end_of_paragraph"),
    ])
    def test_compound_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]


class TestOptionalToAfterGo:
    """wh-ed4: 'go to <landmark>' must parse identically to 'go <landmark>'."""

    @pytest.mark.parametrize("utterance,landmark", [
        ("go to home", "home"),
        ("go to end", "end"),
        ("go to top", "top"),
        ("go to bottom", "bottom"),
    ])
    def test_go_to_simple_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    @pytest.mark.parametrize("utterance,landmark", [
        ("go to start of word", "start_of_word"),
        ("go to end of word", "end_of_word"),
        ("go to start of paragraph", "start_of_paragraph"),
        ("go to end of paragraph", "end_of_paragraph"),
    ])
    def test_go_to_compound_landmark(self, utterance, landmark):
        cmds = NavigationParser.parse(utterance)
        assert cmds == [NavigationCommand(verb="go", kind="landmark", landmark=landmark)]

    def test_bare_go_to_is_unparseable(self):
        assert NavigationParser.parse("go to") is None


class TestRelative:
    def test_go_right_default(self):
        cmds = NavigationParser.parse("go right")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="right", count=1, unit="character"
        )]

    def test_go_left_with_count_and_unit(self):
        cmds = NavigationParser.parse("go left three words")
        assert cmds == [NavigationCommand(
            verb="go", kind="relative", direction="left", count=3, unit="word"
        )]


class TestGrabToUnchanged:
    """Ensure wh-ed4 fix does not regress existing 'grab to <landmark>' behavior."""

    def test_grab_to_end(self):
        cmds = NavigationParser.parse("grab to end")
        assert cmds == [NavigationCommand(verb="grab", kind="landmark", landmark="end")]

    def test_grab_relative(self):
        cmds = NavigationParser.parse("grab left two words")
        assert cmds == [NavigationCommand(
            verb="grab", kind="relative", direction="left", count=2, unit="word"
        )]


class TestChaining:
    def test_then_chain_with_optional_to(self):
        cmds = NavigationParser.parse("go to end then go home")
        assert cmds == [
            NavigationCommand(verb="go", kind="landmark", landmark="end"),
            NavigationCommand(verb="go", kind="landmark", landmark="home"),
        ]
