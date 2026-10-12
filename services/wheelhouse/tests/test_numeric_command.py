"""The "numeric" command types the digits of a spoken number.

Bead wh-pattern-variants-numeric.1. Saying "numeric twenty three" types
"23"; "numeric five" types "5"; "numeric 23" types "23". The command is one
pattern entry in speech/config/patterns.toml (``^numeric (\\d+)$``, doc_id
"numeric-digits") that calls the number_digits action.

The pipeline tests run the whole path the user runs: word events in at one
end, the text-insertion payload out at the other, against the PRODUCTION
patterns.toml. The loader widens the ``(\\d+)`` capture so the regex accepts
number words, but the captured value still holds the spoken words, so the
action must convert it. The action tests pin that conversion, and the
absence of the period that number_point adds.
"""
import os
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio

import pytest

from speech.action_catalog import ACTION_CATALOG
from speech.actions import ActionFunctions
from tests.test_speech_pipeline import SpeechPipelineHarness

PRODUCTION_PATTERNS = (
    Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"
)


@pytest.fixture
async def harness():
    """The real pipeline over the shipped patterns.toml."""
    made = SpeechPipelineHarness(patterns_path=str(PRODUCTION_PATTERNS))
    await made.start()
    yield made
    await made.stop()


@pytest.fixture
def action_funcs():
    handler = MagicMock()
    handler.app = MagicMock()
    return ActionFunctions(handler)


async def _speak(harness, words):
    """Send one utterance word by word, then its end marker."""
    for index, word in enumerate(words):
        await harness.send_word(
            word,
            start_of_utterance=(index == 0),
            end_of_utterance=False,
        )
    await harness.send_utterance_end_marker(harness._utterance_counter)
    await asyncio.sleep(0.1)


def _inserted(harness):
    """Every text-bearing payload, whatever the action name."""
    pieces = []
    for out in harness.get_outputs():
        for key in ("insertion_string", "text"):
            value = out.params.get(key)
            if value:
                pieces.append(value)
    return pieces


def _numeric_entries():
    with PRODUCTION_PATTERNS.open("rb") as handle:
        entries = tomllib.load(handle)["pattern"]
    return [e for e in entries if e.get("doc_id") == "numeric-digits"]


class TestNumericThroughThePipeline:
    @pytest.mark.asyncio
    async def test_numeric_twenty_three_types_23(self, harness):
        await _speak(harness, ["numeric", "twenty", "three"])

        assert _inserted(harness) == ["23"]

    @pytest.mark.asyncio
    async def test_numeric_five_types_5(self, harness):
        await _speak(harness, ["numeric", "five"])

        assert _inserted(harness) == ["5"]

    @pytest.mark.asyncio
    async def test_numeric_digits_types_the_same_digits(self, harness):
        await _speak(harness, ["numeric", "23"])

        assert _inserted(harness) == ["23"]

    @pytest.mark.asyncio
    async def test_numeric_alone_is_dictated_not_a_command(self, harness):
        """The command needs a number; the bare word stays text."""
        await _speak(harness, ["numeric"])

        assert not any(
            piece.strip().isdigit() for piece in _inserted(harness)
        ), _inserted(harness)


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "x x twenty thirty forty fifty sixty seventy eighty ninety".split()


def _number_words(n):
    """The words a speaker says for ``n`` in 0..999."""
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    out = [_ONES[n // 100], "hundred"]
    if n % 100:
        out += _number_words(n % 100)
    return out


async def _typed_by(harness, words):
    """Speak ``words`` as one utterance and return what was typed."""
    harness.clear_outputs()
    await _speak(harness, words)
    return _inserted(harness)


class TestNumericAcceptedRange:
    """The measured range (wh-pattern-variants-numeric.1).

    Spoken words: 0 ("zero") through 999. The parser behind
    words_to_int stops at 999, so "one thousand" is not a number to the
    pattern and the whole utterance is dictated as text. A run of digits is
    NOT capped at 999: words_to_int converts a digit string with int().

    The full sweep of every number from 0 to 999 takes about a minute, so it
    runs only when NUMERIC_FULL_SWEEP is set:

        NUMERIC_FULL_SWEEP=1 python scripts/run_tests.py -k test_every_number_from_0_to_999
    """

    @pytest.mark.asyncio
    async def test_the_edges_of_the_range(self, harness):
        cases = [
            (["numeric", "zero"], ["0"]),
            (["numeric", "one"], ["1"]),
            (["numeric"] + _number_words(19), ["19"]),
            (["numeric"] + _number_words(20), ["20"]),
            (["numeric"] + _number_words(100), ["100"]),
            (["numeric"] + _number_words(999), ["999"]),
            (["numeric", "one", "hundred", "and", "twenty", "three"], ["123"]),
            (["numeric", "twenty-three"], ["23"]),
            (["numeric", "one", "thousand"], ["numeric one thousand"]),
            (["numeric", "1000"], ["1000"]),
            (["numeric", "12345"], ["12345"]),
            (["numeric", "007"], ["7"]),
        ]
        for words, expected in cases:
            assert await _typed_by(harness, words) == expected, words

    @pytest.mark.skipif(
        not os.environ.get("NUMERIC_FULL_SWEEP"),
        reason="set NUMERIC_FULL_SWEEP=1 to run the one-minute full sweep",
    )
    @pytest.mark.timeout(600)
    @pytest.mark.asyncio
    async def test_every_number_from_0_to_999_is_typed_as_digits(self, harness):
        wrong = {}
        for n in range(0, 1000):
            typed = await _typed_by(harness, ["numeric"] + _number_words(n))
            if typed != [str(n)]:
                wrong[n] = typed
        assert wrong == {}, wrong


class TestNumericPatternEntry:
    def test_exactly_one_entry_with_the_agreed_fields(self):
        entries = _numeric_entries()

        assert len(entries) == 1, entries
        entry = entries[0]
        assert entry["pattern"] == r"^numeric (\d+)$"
        assert entry["whole_utterance_only"] is True
        assert entry["actions"] == [
            {"function": "number_digits", "params": ["g1"]}
        ]


class TestNumberDigitsAction:
    def test_word_number_becomes_digits(self, action_funcs):
        result = action_funcs.number_digits("twenty three")
        assert result == {
            "action": "intelligent_insert_text",
            "params": {"insertion_string": "23"},
        }

    def test_digit_string_stays_digits(self, action_funcs):
        result = action_funcs.number_digits("5")
        assert result["params"]["insertion_string"] == "5"

    def test_no_period_is_added(self, action_funcs):
        """number_point adds a period; this command must not."""
        result = action_funcs.number_digits("one")
        assert result["params"]["insertion_string"] == "1"

    def test_unreadable_words_are_typed_as_spoken(self, action_funcs):
        result = action_funcs.number_digits("banana")
        assert result["params"]["insertion_string"] == "banana"

    def test_action_is_registered(self, action_funcs):
        assert "number_digits" in action_funcs.get_functions()


class TestNumberDigitsCatalogEntry:
    def test_entry_has_the_agreed_shape(self):
        entries = [e for e in ACTION_CATALOG if e["name"] == "number_digits"]

        assert len(entries) == 1
        entry = entries[0]
        assert entry["audience"] == "advanced"
        assert entry["group"] == "Text"
        assert "numeric" in entry["example"]
        for key in ("label", "summary", "params"):
            assert entry[key]
