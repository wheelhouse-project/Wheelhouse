"""Tests for NavigationExecutor - NavigationCommand to IPC hotkey action conversion."""

import pytest

from services.wheelhouse.speech.navigation.models import NavigationCommand
from services.wheelhouse.speech.navigation.executor import NavigationExecutor


def _action(keys, repeat=1):
    """Helper to build expected action dict."""
    return {"action": "hotkey_action", "params": {"keys": keys, "repeat": repeat}}


class TestRelativeGoActions:
    """go + relative -> arrow keys without shift."""

    def test_right_one_character(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="right", count=1, unit="character")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["right"])]

    def test_left_three_characters(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="left", count=3, unit="character")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["left"], repeat=3)]

    def test_right_two_words(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="right", count=2, unit="word")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["ctrl", "right"], repeat=2)]

    def test_left_one_word(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="left", count=1, unit="word")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["ctrl", "left"])]

    def test_right_paragraph(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="right", count=1, unit="paragraph")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["ctrl", "down"])]

    def test_left_two_paragraphs(self):
        cmd = NavigationCommand(verb="go", kind="relative", direction="left", count=2, unit="paragraph")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["ctrl", "up"], repeat=2)]


class TestRelativeGrabActions:
    """grab + relative -> shift + arrow keys."""

    def test_grab_right_three_words(self):
        cmd = NavigationCommand(verb="grab", kind="relative", direction="right", count=3, unit="word")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["shift", "ctrl", "right"], repeat=3)]

    def test_grab_left_one_character(self):
        cmd = NavigationCommand(verb="grab", kind="relative", direction="left", count=1, unit="character")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["shift", "left"])]

    def test_grab_right_paragraph(self):
        cmd = NavigationCommand(verb="grab", kind="relative", direction="right", count=2, unit="paragraph")
        result = NavigationExecutor.to_actions([cmd])
        assert result == [_action(["shift", "ctrl", "down"], repeat=2)]


class TestLandmarkGoActions:
    """go + landmark -> key combos without shift."""

    def test_go_home(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="home")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["home"])]

    def test_go_end(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="end")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["end"])]

    def test_go_top(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="top")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "home"])]

    def test_go_bottom(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="bottom")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "end"])]

    def test_go_start_of_word(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="start_of_word")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "left"])]

    def test_go_end_of_word(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="end_of_word")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "right"])]

    def test_go_start_of_paragraph(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="start_of_paragraph")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "up"])]

    def test_go_end_of_paragraph(self):
        cmd = NavigationCommand(verb="go", kind="landmark", landmark="end_of_paragraph")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["ctrl", "down"])]


class TestLandmarkGrabActions:
    """grab + landmark -> shift + key combos."""

    def test_grab_to_end(self):
        cmd = NavigationCommand(verb="grab", kind="landmark", landmark="end")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["shift", "end"])]

    def test_grab_to_top(self):
        cmd = NavigationCommand(verb="grab", kind="landmark", landmark="top")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["shift", "ctrl", "home"])]

    def test_grab_to_bottom(self):
        cmd = NavigationCommand(verb="grab", kind="landmark", landmark="bottom")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["shift", "ctrl", "end"])]

    def test_grab_to_end_of_paragraph(self):
        cmd = NavigationCommand(verb="grab", kind="landmark", landmark="end_of_paragraph")
        assert NavigationExecutor.to_actions([cmd]) == [_action(["shift", "ctrl", "down"])]


class TestChainedActions:
    """Multiple commands produce multiple actions in order."""

    def test_go_home_then_grab_to_end(self):
        cmds = [
            NavigationCommand(verb="go", kind="landmark", landmark="home"),
            NavigationCommand(verb="grab", kind="landmark", landmark="end"),
        ]
        result = NavigationExecutor.to_actions(cmds)
        assert result == [_action(["home"]), _action(["shift", "end"])]

    def test_three_command_chain(self):
        cmds = [
            NavigationCommand(verb="go", kind="landmark", landmark="home"),
            NavigationCommand(verb="go", kind="relative", direction="right", count=3, unit="word"),
            NavigationCommand(verb="grab", kind="landmark", landmark="end"),
        ]
        result = NavigationExecutor.to_actions(cmds)
        assert len(result) == 3
        assert result[0] == _action(["home"])
        assert result[1] == _action(["ctrl", "right"], repeat=3)
        assert result[2] == _action(["shift", "end"])
