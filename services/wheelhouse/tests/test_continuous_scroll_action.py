"""The Logic-side actions for continuous scrolling
(wh-voice-access-parity.2.3.3, part two).

``ActionFunctions.start_continuous_scroll`` and
``ActionFunctions.stop_continuous_scroll`` are the two functions patterns.toml
names. Each builds one IPC payload for the Input process. They hold no timer
and no state: the repeating timer lives in the Input process, because that is
the process that owns SendInput and the process whose command loop the stop
word has to reach.

The start function follows the same refusal rule the discrete ``scroll``
function follows. An unknown direction returns None, which the command engine
treats as "nothing to send", so a malformed pattern cannot scroll in some
arbitrary default direction and cannot start a timer that the user then has
to stop.

The stop function takes no arguments at all. Stopping is the same act
whichever way the scroll was going, and a stop that carried a direction could
refuse to stop a scroll going the other way.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

from speech.actions import ActionFunctions


@pytest.fixture
def action_funcs():
    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


class TestContinuousScrollRegistration:
    def test_start_is_registered_under_the_name_patterns_use(self, action_funcs):
        functions = action_funcs.get_functions()
        assert "start_continuous_scroll" in functions
        assert functions["start_continuous_scroll"] == (
            action_funcs.start_continuous_scroll
        )

    def test_stop_is_registered_under_the_name_patterns_use(self, action_funcs):
        functions = action_funcs.get_functions()
        assert "stop_continuous_scroll" in functions
        assert functions["stop_continuous_scroll"] == (
            action_funcs.stop_continuous_scroll
        )


class TestStartPayload:
    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_each_direction_builds_its_own_payload(self, action_funcs, direction):
        assert action_funcs.start_continuous_scroll(direction) == {
            "action": "start_continuous_scroll",
            "params": {"direction": direction},
        }

    @pytest.mark.parametrize("spoken", ["UP", " Down ", "Left"])
    def test_case_and_spacing_are_normalised(self, action_funcs, spoken):
        payload = action_funcs.start_continuous_scroll(spoken)
        assert payload["params"]["direction"] == spoken.strip().lower()

    @pytest.mark.parametrize(
        "direction", ["sideways", "", "none", "diagonally", "downwards"]
    )
    def test_an_unknown_direction_sends_nothing(self, action_funcs, direction):
        """A timer the user never asked for is worse than doing nothing."""
        assert action_funcs.start_continuous_scroll(direction) is None

    @pytest.mark.parametrize("direction", [None, 3, ["down"], object()])
    def test_a_direction_that_is_not_text_sends_nothing(
        self, action_funcs, direction
    ):
        assert action_funcs.start_continuous_scroll(direction) is None


class TestStopPayload:
    def test_stop_builds_a_payload_with_no_parameters(self, action_funcs):
        assert action_funcs.stop_continuous_scroll() == {
            "action": "stop_continuous_scroll",
            "params": {},
        }

    def test_stop_never_refuses(self, action_funcs):
        """Two stops in a row both send. The second is harmless and the user
        who repeats the word must not be told to say it again."""
        first = action_funcs.stop_continuous_scroll()
        second = action_funcs.stop_continuous_scroll()
        assert first == second
        assert second is not None
