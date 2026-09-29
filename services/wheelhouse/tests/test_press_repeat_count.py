"""A repeat count on the press command (wh-voice-access-parity.2.12).

"press tab 3 times" presses Tab three times. The count comes from a (\\d+)
group in the shipped press-keys pattern, per David's ruling of 2026-09-27
19:09: pattern_transform.py widens that group so the speech engine's digits
and number words both match ("press tab three times"). press_keys reads the
count with the same words_to_int reading that press() uses, and clamps it
to HOTKEY_REPEAT_CAP (30) rather than press()'s 50, because its payload is
a hotkey_action, which pauses 0.1 s between repeats (Boss e8 ruling
2026-09-27 19:57).

The word "times" (or "time") is required. Without it a trailing digit stays
part of the key names, exactly as before: "press control 2" presses Ctrl+2.
"press f 5" presses F5, because press_keys reads "f" and a number as one
function key (wh-press-f-number-function-key, David 2026-09-27 22:34).

The tests run at three levels: the action function, the rule engine over
the REAL shipped patterns.toml, and the speech processor's dictation
fallback.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
patterns_path = wheelhouse_dir / "speech" / "config" / "patterns.toml"
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
from typing import Any, Dict, List

import pytest
from unittest.mock import MagicMock

from speech.actions import HOTKEY_REPEAT_CAP, ActionFailed, ActionFunctions
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor


class MockApp:
    """Minimal mock app that records every payload sent to the UI."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: dict):
        self.actions.append({"action": action, "params": params})
        return {"status": "success"}


@pytest.fixture
def mock_app():
    return MockApp()


@pytest.fixture
def action_funcs(mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return ActionFunctions(handler)


@pytest.fixture(scope="module")
def catalog():
    return PatternCatalog(str(patterns_path))


@pytest.fixture
def parser(catalog, mock_app):
    handler = MagicMock()
    handler.app = mock_app
    return TextParser(handler, catalog)


@pytest.fixture
def processor(catalog, parser, mock_app):
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=parser,
        app=mock_app,
        replacement_timeout_ms=400,
        command_timeout_ms=1000,
    )


def _spoken_ids(value):
    """A case id part with no space in it.

    A string argument (the spoken phrase, a key name) is hyphenated; a list
    or a number gets an empty part. The mutation gate runner reads a failed
    test's name up to its first space, so an id holding a space could never
    match.
    """
    return value.replace(" ", "-") if isinstance(value, str) else ""


def _hotkeys(app: MockApp) -> List[Dict[str, Any]]:
    return [a for a in app.actions if a.get("action") == "hotkey_action"]


def _key_presses(app: MockApp) -> List[Dict[str, Any]]:
    return [a for a in app.actions if a.get("action") == "press_key_action"]


def _dictation(app: MockApp) -> List[Dict[str, Any]]:
    return [
        a for a in app.actions if a.get("action") == "intelligent_insert_text"
    ]


# ============================================================================
# The action function
# ============================================================================

class TestPressKeysRepeatParameter:
    def test_no_count_presses_once(self, action_funcs):
        result = action_funcs.press_keys("tab")
        assert result["params"] == {"keys": ["tab"], "repeat": 1}

    def test_none_count_presses_once(self, action_funcs):
        """An unmatched optional group arrives as None."""
        result = action_funcs.press_keys("tab", None)
        assert result["params"] == {"keys": ["tab"], "repeat": 1}

    def test_digit_count(self, action_funcs):
        result = action_funcs.press_keys("tab", "3")
        assert result["params"] == {"keys": ["tab"], "repeat": 3}

    def test_word_count(self, action_funcs):
        result = action_funcs.press_keys("tab", "three")
        assert result["params"] == {"keys": ["tab"], "repeat": 3}

    def test_count_applies_to_a_combination(self, action_funcs):
        result = action_funcs.press_keys("control z", "2")
        assert result["params"] == {"keys": ["ctrl", "z"], "repeat": 2}

    def test_count_capped_at_the_hotkey_repeat_cap(self, action_funcs):
        """30, not press()'s 50: hotkey_action pauses between repeats."""
        assert HOTKEY_REPEAT_CAP == 30
        result = action_funcs.press_keys("tab", "31")
        assert result["params"]["repeat"] == 30

    def test_count_at_the_cap_is_kept(self, action_funcs):
        result = action_funcs.press_keys("tab", "30")
        assert result["params"]["repeat"] == 30

    def test_zero_count_presses_once(self, action_funcs):
        result = action_funcs.press_keys("tab", "0")
        assert result["params"]["repeat"] == 1

    def test_unreadable_count_presses_once(self, action_funcs):
        result = action_funcs.press_keys("tab", "banana")
        assert result["params"]["repeat"] == 1

    def test_press_and_press_keys_read_a_count_the_same_way(self, action_funcs):
        """A2: one reading shared by both functions, up to the lower cap."""
        for spoken in (None, "0", "1", "3", "three", "29", "30", "banana"):
            assert (
                action_funcs.press_keys("tab", spoken)["params"]["repeat"]
                == action_funcs.press("tab", spoken)["params"]["repeat"]
            ), spoken

    def test_press_keeps_its_own_cap_of_50(self, action_funcs):
        """Boss e8 ruling 2026-09-27 19:57: press() stays at 50."""
        assert action_funcs.press("tab", "40")["params"]["repeat"] == 40
        assert action_funcs.press("tab", "51")["params"]["repeat"] == 50
        assert action_funcs.press_keys("tab", "40")["params"]["repeat"] == 30


# ============================================================================
# The shipped pattern, through the rule engine
# ============================================================================

class TestSpokenCountThroughTheRuleEngine:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken,keys,repeat",
        [
            ("press tab 3 times", ["tab"], 3),
            ("press tab three times", ["tab"], 3),
            ("press control z 2 times", ["ctrl", "z"], 2),
            ("press control z two times", ["ctrl", "z"], 2),
            ("press down arrow twenty times", ["down"], 20),
            ("press tab twenty three times", ["tab"], 23),
            ("press tab 1 time", ["tab"], 1),
            ("press tab one time", ["tab"], 1),
            ("press tab sixty times", ["tab"], 30),
            ("press 3 3 times", ["3"], 3),
        ],
        ids=_spoken_ids,
    )
    async def test_count_presses_the_keys_that_many_times(
        self, parser, mock_app, spoken, keys, repeat
    ):
        assert await parser.parse_and_execute(spoken) is True
        hotkeys = _hotkeys(mock_app)
        assert len(hotkeys) == 1, mock_app.actions
        assert hotkeys[0]["params"] == {"keys": keys, "repeat": repeat}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken,keys",
        [
            # A3: every one of these behaves exactly as before the count.
            ("press enter", ["enter"]),
            ("press control alt delete", ["ctrl", "alt", "delete"]),
            ("press F5", ["f5"]),
            # No "times", so the 5 is not a count. "f" and a number name
            # one function key (wh-press-f-number-function-key).
            ("press f 5", ["f5"]),
            ("press control 2", ["ctrl", "2"]),
            # Key order independence.
            ("press delete control", ["ctrl", "delete"]),
            # Hyphen handling.
            ("press f-11", ["f11"]),
            ("press control-alt delete", ["ctrl", "alt", "delete"]),
        ],
        ids=_spoken_ids,
    )
    async def test_no_count_presses_once_as_before(
        self, parser, mock_app, spoken, keys
    ):
        assert await parser.parse_and_execute(spoken) is True
        hotkeys = _hotkeys(mock_app)
        assert len(hotkeys) == 1, mock_app.actions
        assert hotkeys[0]["params"] == {"keys": keys, "repeat": 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken",
        ["press xyzzy", "press xyzzy 3 times", "press tab times"],
        ids=_spoken_ids,
    )
    async def test_unrecognized_key_word_reports_no_match(
        self, parser, mock_app, spoken
    ):
        assert await parser.parse_and_execute(spoken) is False
        assert _hotkeys(mock_app) == []


class TestOtherCountRowsUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken,key,repeat",
        [
            ("tab 3", "tab", 3),
            ("indent three", "tab", 3),
            ("backspace 4", "backspace", 4),
            ("delete 2", "del", 2),
        ],
        ids=_spoken_ids,
    )
    async def test_count_row_presses_as_before(
        self, parser, mock_app, spoken, key, repeat
    ):
        assert await parser.parse_and_execute(spoken) is True
        presses = _key_presses(mock_app)
        assert len(presses) == 1, mock_app.actions
        assert presses[0]["params"] == {"key": key, "repeat": repeat}


# ============================================================================
# Function keys spoken as "f" and a number (wh-press-f-number-function-key)
# ============================================================================

class TestFunctionKeySpokenAsFAndANumber:
    """The speech engine often writes F5 as "f 5" or "f five".

    press_keys reads "f" followed by a number N as the one key fN when fN is
    a key name Wheelhouse knows (f1 through f12). David, 2026-09-27 22:34:
    "the correct behavior is pressing F5".
    """

    @pytest.mark.parametrize(
        "sequence,keys",
        [
            # A1
            ("f 5", ["f5"]),
            ("f five", ["f5"]),
            ("f 12", ["f12"]),
            ("f twelve", ["f12"]),
            ("f 1", ["f1"]),
            ("alt f 4", ["alt", "f4"]),
            ("control f 5", ["ctrl", "f5"]),
            ("f 5 control", ["ctrl", "f5"]),
            # The number reader accepts the homophone "for" for "four".
            ("alt f for", ["alt", "f4"]),
            # A hyphenated "f-five" splits into "f" and "five".
            pytest.param("f-five", ["f5"], id="f-hyphen-five"),
        ],
        ids=_spoken_ids,
    )
    def test_f_and_a_number_is_one_function_key(
        self, action_funcs, sequence, keys
    ):
        result = action_funcs.press_keys(sequence)
        assert result["params"] == {"keys": keys, "repeat": 1}

    @pytest.mark.parametrize(
        "sequence,keys",
        [
            # A2: unchanged.
            ("f5", ["f5"]),
            ("f-11", ["f11"]),
            ("f", ["f"]),
            ("control f", ["ctrl", "f"]),
            ("control 2", ["ctrl", "2"]),
            ("shift 5", ["shift", "5"]),
            # No key named f0, so "f" and "0" stay two keys.
            ("f 0", ["f", "0"]),
        ],
        ids=_spoken_ids,
    )
    def test_other_sequences_are_unchanged(self, action_funcs, sequence, keys):
        result = action_funcs.press_keys(sequence)
        assert result["params"] == {"keys": keys, "repeat": 1}

    @pytest.mark.parametrize(
        "sequence", ["f 13", "f thirteen", "f zero"], ids=_spoken_ids
    )
    def test_f_and_a_number_with_no_function_key_still_fails(
        self, action_funcs, sequence
    ):
        """No key named f13 or f0: a number word stays an unknown key name.

        A digit is itself a key name ("f 0" presses F and 0), but a number
        word such as "thirteen" or "zero" is not, so the command fails as
        it did before and the words go to dictation.
        """
        with pytest.raises(ActionFailed):
            action_funcs.press_keys(sequence)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken,keys,repeat",
        [
            # A1 through the shipped pattern.
            ("press f 5", ["f5"], 1),
            ("press f five", ["f5"], 1),
            ("press f 12", ["f12"], 1),
            ("press alt f 4", ["alt", "f4"], 1),
            ("press control f 5", ["ctrl", "f5"], 1),
            # A2 through the shipped pattern.
            ("press F5", ["f5"], 1),
            ("press f-11", ["f11"], 1),
            ("press f", ["f"], 1),
            ("press control f", ["ctrl", "f"], 1),
            ("press control 2", ["ctrl", "2"], 1),
            ("press shift 5", ["shift", "5"], 1),
            ("press f 0", ["f", "0"], 1),
            # A3: a count after the function key.
            ("press f 5 3 times", ["f5"], 3),
            ("press f 12 two times", ["f12"], 2),
            # A3: the count group takes the number, as for "press tab 5
            # times", so F is pressed five times.
            ("press f 5 times", ["f"], 5),
            ("press f five times", ["f"], 5),
        ],
        ids=_spoken_ids,
    )
    async def test_through_the_shipped_pattern(
        self, parser, mock_app, spoken, keys, repeat
    ):
        assert await parser.parse_and_execute(spoken) is True
        hotkeys = _hotkeys(mock_app)
        assert len(hotkeys) == 1, mock_app.actions
        assert hotkeys[0]["params"] == {"keys": keys, "repeat": repeat}

    @pytest.mark.asyncio
    async def test_f_13_through_the_shipped_pattern_reports_no_match(
        self, parser, mock_app
    ):
        assert await parser.parse_and_execute("press f 13") is False
        assert _hotkeys(mock_app) == []


# ============================================================================
# "f", two spoken numbers, and "times" (wh-press-f-two-numbers-count)
# ============================================================================

class TestFunctionKeyAndCountSpokenAsTwoNumbers:
    """"press f five three times" presses F5 three times.

    The count group is lazy-keyed, so it takes "five three", which the
    number reader combines digit by digit into 53. press_keys splits such a
    count when the keys end with "f" and the count's first word alone names
    a function key (f1 to f12): that word joins the keys and the rest is
    the count. The digit form "press f 5 3 times" already works, because a
    digit run is one number and cannot swallow the next one.
    """

    @pytest.mark.parametrize(
        "sequence,count,keys,repeat",
        [
            ("f", "five three", ["f5"], 3),
            ("f", "one two", ["f1"], 2),
            # The number reader reads a hyphen as a space.
            pytest.param(
                "f", "five-three", ["f5"], 3, id="f-hyphen-five-three"
            ),
            ("control f", "five three", ["ctrl", "f5"], 3),
            ("f", "five twenty", ["f5"], 20),
        ],
        ids=_spoken_ids,
    )
    def test_first_number_is_the_function_key(
        self, action_funcs, sequence, count, keys, repeat
    ):
        result = action_funcs.press_keys(sequence, count)
        assert result["params"] == {"keys": keys, "repeat": repeat}

    @pytest.mark.parametrize(
        "sequence,count,keys,repeat",
        [
            # One number: F pressed that many times.
            ("f", "five", ["f"], 5),
            # "twenty one" is one number; twenty names no function key.
            ("f", "twenty one", ["f"], 21),
            ("f", "twenty-one", ["f"], 21),
            # "one hundred": "hundred" alone is no count.
            ("f", "one hundred", ["f"], 30),
            # "one zero": zero is no count, so the reading stays 10.
            ("f", "one zero", ["f"], 10),
            # Thirteen names no function key.
            ("f", "thirteen", ["f"], 13),
            # Only after "f".
            ("tab", "five three", ["tab"], 30),
            ("f 5", "three", ["f5"], 3),
        ],
        ids=_spoken_ids,
    )
    def test_other_counts_are_unchanged(
        self, action_funcs, sequence, count, keys, repeat
    ):
        result = action_funcs.press_keys(sequence, count)
        assert result["params"] == {"keys": keys, "repeat": repeat}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spoken,keys,repeat",
        [
            # Acceptance 1.
            ("press f five three times", ["f5"], 3),
            # Acceptance 2.
            ("press f 5 3 times", ["f5"], 3),
            # Acceptance 3.
            ("press f five times", ["f"], 5),
            ("press f one two times", ["f1"], 2),
            ("press alt f four two times", ["alt", "f4"], 2),
            # Unchanged: one number, or not after "f".
            ("press f twenty one times", ["f"], 21),
            ("press tab five three times", ["tab"], 30),
        ],
        ids=_spoken_ids,
    )
    async def test_through_the_shipped_pattern(
        self, parser, mock_app, spoken, keys, repeat
    ):
        assert await parser.parse_and_execute(spoken) is True
        hotkeys = _hotkeys(mock_app)
        assert len(hotkeys) == 1, mock_app.actions
        assert hotkeys[0]["params"] == {"keys": keys, "repeat": repeat}

    @pytest.mark.asyncio
    async def test_f_thirteen_two_times_reports_no_match(
        self, parser, mock_app
    ):
        """Unchanged: the number reader cannot read "thirteen two", so the
        numeric validation refuses the match and the words are dictated."""
        assert await parser.parse_and_execute("press f thirteen two times") is False
        assert _hotkeys(mock_app) == []

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Acceptance 4 is not reachable from press_keys: the widened "
            "count group takes 'twelve two', the number reader returns "
            "None, and the numeric validation refuses the whole match "
            "before press_keys runs (wh-press-f-two-numbers-count)."
        ),
    )
    async def test_f_twelve_two_times_presses_f12_twice(
        self, parser, mock_app
    ):
        assert await parser.parse_and_execute("press f twelve two times") is True
        hotkeys = _hotkeys(mock_app)
        assert len(hotkeys) == 1, mock_app.actions
        assert hotkeys[0]["params"] == {"keys": ["f12"], "repeat": 2}


# ============================================================================
# The speech processor's dictation fallback
# ============================================================================

class TestUnrecognizedKeyWithCountIsDictated:
    @pytest.mark.asyncio
    async def test_the_whole_phrase_is_typed(self, processor, mock_app):
        await processor._execute_command("press xyzzy 3 times")

        assert _hotkeys(mock_app) == []
        dictated = _dictation(mock_app)
        assert len(dictated) == 1
        assert dictated[0]["params"]["insertion_string"] == "press xyzzy 3 times"
