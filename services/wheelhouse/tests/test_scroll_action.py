"""Tests for the spoken scroll command's Logic-side action
(wh-voice-access-parity.2.3, part one -- discrete scrolling only).

``ActionFunctions.scroll`` is the function patterns.toml names as ``scroll``.
It builds the IPC payload the Input process executes as ``scroll_wheel``. The
whole job is turning a spoken direction plus an optional spoken number into
``{"action": "scroll_wheel", "params": {"direction": ..., "clicks": ...}}``.

Two shapes it must survive, both taken from how ``hk`` already behaves:

* The count comes from an OPTIONAL regex group. An utterance with no number
  passes ``None`` (or the string ``"None"``, which is what a resolved capture
  group looks like when the pattern engine stringifies an unmatched group), and
  that must mean one notch, not a refusal.
* A spoken number may arrive as a word rather than a digit, so the count goes
  through ``words_to_int`` exactly as the hotkey repeat count does.

The count is CLAMPED here rather than refused, again matching ``hk``: a user who
says "scroll down one hundred" gets the cap, not silence. The primitive below
still refuses an out-of-range count, because that can only come from a
malformed message rather than from speech.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

from speech.actions import ActionFunctions
from utils.win_input_sender import MAX_SCROLL_CLICKS


@pytest.fixture
def action_funcs():
    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


class TestScrollRegistration:
    def test_scroll_is_registered_under_the_name_patterns_use(self, action_funcs):
        assert "scroll" in action_funcs.get_functions()
        assert action_funcs.get_functions()["scroll"] == action_funcs.scroll


class TestScrollPayload:
    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_each_direction_builds_a_one_notch_payload(
        self, action_funcs, direction
    ):
        assert action_funcs.scroll(direction) == {
            "action": "scroll_wheel",
            "params": {"direction": direction, "clicks": 1},
        }

    def test_a_spoken_digit_becomes_the_notch_count(self, action_funcs):
        payload = action_funcs.scroll("down", "5")
        assert payload["params"] == {"direction": "down", "clicks": 5}

    def test_a_spoken_number_word_becomes_the_notch_count(self, action_funcs):
        payload = action_funcs.scroll("up", "three")
        assert payload["params"] == {"direction": "up", "clicks": 3}

    @pytest.mark.parametrize("empty", [None, "None", ""])
    def test_an_unmatched_optional_group_means_one_notch(
        self, action_funcs, empty
    ):
        payload = action_funcs.scroll("left", empty)
        assert payload["params"] == {"direction": "left", "clicks": 1}

    def test_the_direction_is_matched_without_regard_to_case(self, action_funcs):
        payload = action_funcs.scroll("DOWN")
        assert payload["params"]["direction"] == "down"

    def test_a_count_above_the_cap_is_clamped_not_refused(self, action_funcs):
        payload = action_funcs.scroll("down", "500")
        assert payload["params"]["clicks"] == MAX_SCROLL_CLICKS

    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    @pytest.mark.parametrize("zero", ["0", "zero"])
    def test_an_explicit_zero_sends_nothing(self, action_funcs, direction, zero):
        """Zero is a number the user said on purpose, so scroll nothing.

        Both spoken forms reach the action: ``words_to_int`` returns 0 for the
        digit and for the word, so the pattern's numeric validation accepts
        each of them rather than rejecting the utterance.
        """
        assert action_funcs.scroll(direction, zero) is None

    def test_a_count_too_long_to_convert_falls_back_to_one_notch(
        self, action_funcs
    ):
        # A digit run longer than sys.get_int_max_str_digits() is still a
        # count this function cannot read, so it takes the same one-notch
        # fallback as any other unreadable count. It must not raise: the
        # count reaches words_to_int with no exception handler around it
        # (wh-voice-access-parity.2.3.2.6).
        #
        # Catch the exception and assert on it rather than letting it
        # propagate, for the reason recorded in
        # wh-voice-access-parity.2.3.2.4: a test that raises under a
        # mutation is indistinguishable from a test that caught it.
        raised = None
        payload = None
        try:
            payload = action_funcs.scroll(
                "down", "1" * (sys.get_int_max_str_digits() + 1)
            )
        except ValueError as exc:
            raised = exc
        assert raised is None, f"scroll raised on an unreadable count: {raised}"
        assert payload is not None
        assert payload["params"]["clicks"] == 1

    @pytest.mark.parametrize("not_a_number", ["banana", "-4"])
    def test_a_count_that_is_not_a_number_falls_back_to_one_notch(
        self, action_funcs, not_a_number
    ):
        """A count this function cannot read is a recognition failure, not an
        instruction, so scrolling once beats doing nothing.

        "-4" belongs in this case rather than with the explicit zero above.
        ``words_to_int`` reads a count with ``str.isdigit``, which is False for
        "-4", so a negative comes back as None exactly as "banana" does. This
        function cannot tell the two apart without parsing a minus sign itself,
        and a negative cannot arrive from speech anyway: the widened word
        capture holds no minus sign.

        The removed ``test_an_unusable_count_falls_back_to_one_notch`` asserted
        this same fallback for "0" as well, which is the defect codex filed as
        wh-voice-access-parity.2.3.2.1.
        """
        payload = action_funcs.scroll("up", not_a_number)
        # Assert the payload exists before indexing it. Without this line a
        # mutation that withholds the payload makes the next line raise
        # TypeError instead of failing an assertion, and the mutation gate
        # cannot tell a real catch from a crash
        # (wh-voice-access-parity.2.3.2.4).
        assert payload is not None
        assert payload["params"]["clicks"] == 1

    @pytest.mark.parametrize("direction", ["sideways", "", None, "middle"])
    def test_an_unknown_direction_sends_nothing(self, action_funcs, direction):
        assert action_funcs.scroll(direction) is None
