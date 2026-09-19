"""Spoken number-word counts must reach the command's repeat count.

David reported 2026-09-02: "when I say Backspace fifteen or any number
greater than ten the backspace is executed only once and the word fifteen
is dictated." (wh-number-words-one-parser)

The cause is a contract break between two services. The shipped count
captures are digit-only -- speech/config/patterns.toml carries
``^back ?space\\s*(\\d+)?$`` -- and the pattern loader widens ``(\\d+)``
to a single ``(\\w+)`` token, then asks words_to_int whether the captured
word is a number. words_to_int knew one..ten only, so "fifteen" was
refused, the match was dropped, the bare command word fired alone, and
the count word was dictated. The speech service relied on the STT
service's inverse text normalization to send digits, and that service
now keeps a lone number word as a word by David's 2026-08-25 direction.

These tests run the whole pipeline the user runs: word events in at one
end, the press_key_action payload out at the other, against the
PRODUCTION patterns.toml rather than a fixture copy, because the defect
lived in the shipped patterns and their loader.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness

PRODUCTION_PATTERNS = str(
    Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"
)


@pytest.fixture
async def harness():
    """The real pipeline over the shipped patterns.toml."""
    made = SpeechPipelineHarness(patterns_path=PRODUCTION_PATTERNS)
    await made.start()
    yield made
    await made.stop()


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


def _presses(harness):
    """Every press_key_action payload the pipeline sent."""
    return [
        out for out in harness.get_outputs()
        if out.action == "press_key_action"
    ]


def _inserted(harness):
    """Every text-bearing payload, whatever the action name."""
    pieces = []
    for out in harness.get_outputs():
        for key in ("insertion_string", "text"):
            value = out.params.get(key)
            if value:
                pieces.append(value)
    return pieces


def _one_press(harness, key):
    """Assert exactly one press of ``key`` and return its repeat count."""
    presses = _presses(harness)
    assert len(presses) == 1, (
        f"expected exactly one press_key_action; got {presses!r}, "
        f"inserted={_inserted(harness)!r}"
    )
    assert presses[0].params.get("key") == key, presses[0].params
    return presses[0].params.get("repeat")


class TestASpokenCountAboveTenReachesTheCommand:
    """The reported defect and the two other shapes David named."""

    @pytest.mark.asyncio
    async def test_backspace_fifteen_presses_backspace_fifteen_times(
            self, harness):
        """The exact report: one press and a dictated word before the fix."""
        await _speak(harness, ["backspace", "fifteen"])

        assert _one_press(harness, "backspace") == 15
        assert "fifteen" not in _inserted(harness), (
            "the count word must be consumed by the command, not typed"
        )

    @pytest.mark.asyncio
    async def test_delete_twenty_three_presses_delete_twenty_three_times(
            self, harness):
        """A two-word count, which no single-token capture can hold."""
        await _speak(harness, ["delete", "twenty", "three"])

        assert _one_press(harness, "del") == 23

    @pytest.mark.asyncio
    async def test_tab_eleven_presses_tab_eleven_times(self, harness):
        """A different command block, to prove the fix is not backspace-only."""
        await _speak(harness, ["tab", "eleven"])

        assert _one_press(harness, "tab") == 11


class TestTheCountsThatAlreadyWorkedAreUnchanged:
    """The fix must not move the behaviour that shipped correctly."""

    @pytest.mark.asyncio
    async def test_backspace_five_still_presses_five_times(self, harness):
        """A word count at or below ten went through the old table."""
        await _speak(harness, ["backspace", "five"])

        assert _one_press(harness, "backspace") == 5

    @pytest.mark.asyncio
    async def test_backspace_digit_three_still_presses_three_times(
            self, harness):
        """A digit count is what the patterns were written for."""
        await _speak(harness, ["backspace", "3"])

        assert _one_press(harness, "backspace") == 3

    @pytest.mark.asyncio
    async def test_delete_fifty_one_is_capped_at_fifty(self, harness):
        """press() clamps at 50; the parser reading 51 must not change that.

        This uses "delete" rather than "backspace" because the count spans
        two words. Only a whole_utterance_only pattern sees both of them:
        the backspace entry has no such flag, so the engine commits at
        "backspace fifty" for 50 and dictates "one", which would satisfy
        the assertion below without the parser ever reading 51.
        """
        await _speak(harness, ["delete", "fifty", "one"])

        assert _one_press(harness, "del") == 50
        assert _inserted(harness) == [], (
            "the whole count must be consumed by the command, not typed; "
            f"got {_inserted(harness)!r}"
        )

    @pytest.mark.asyncio
    async def test_a_bare_number_word_is_still_dictated(self, harness):
        """A count with no command in front of it is text, not a command."""
        await _speak(harness, ["fifteen"])

        assert _presses(harness) == [], (
            "a bare number word must press nothing"
        )
        assert any("fifteen" in piece for piece in _inserted(harness)), (
            f"the bare word must still be typed; got {_inserted(harness)!r}"
        )


class TestTheFillerTokenReachesTheCommandToo:
    """deepseek finding wh-number-words-one-parser.1.1.

    parse_number_word drops one leading "number"/"numbers" filler, because
    at the numbered badges users say "click number three". The widened
    capture body counted that filler against its five-token phrase cap and
    had no way to put it in front of a digit run, so two shapes the parser
    reads were refused by every count pattern and dictated instead.
    """

    @pytest.mark.asyncio
    async def test_a_filler_before_a_word_count_presses_that_many_times(
            self, harness):
        """Four tokens: the filler fits under the old cap, so this passed."""
        await _speak(harness, ["delete", "number", "twelve"])

        assert _one_press(harness, "del") == 12

    @pytest.mark.asyncio
    async def test_a_filler_before_a_digit_count_presses_that_many_times(
            self, harness):
        """The sibling shape: the old body had no filler before a digit run.

        "numbers 75" is an example in the parser's own docstring, so this
        was never an exotic input.
        """
        await _speak(harness, ["delete", "number", "12"])

        assert _one_press(harness, "del") == 12
        assert "12" not in "".join(_inserted(harness)), (
            "the count must be consumed by the command, not typed"
        )

    @pytest.mark.asyncio
    async def test_a_filler_before_the_longest_phrase_is_read_and_clamped(
            self, harness):
        """Six tokens with the filler, one over the old five-token cap.

        press() clamps at 50, so 123 arrives as 50. The point of the test is
        that a press happens at all: before the fix the pattern did not
        match and the whole phrase was typed.
        """
        await _speak(
            harness,
            ["delete", "number", "one", "hundred", "and", "twenty", "three"],
        )

        assert _one_press(harness, "del") == 50
        assert "hundred" not in "".join(_inserted(harness)), (
            "the count phrase must be consumed by the command, not typed"
        )

    @pytest.mark.asyncio
    async def test_a_filler_that_is_not_leading_is_not_part_of_a_count(
            self, harness):
        """The parser drops ONE LEADING filler and nothing else.

        A doubled filler leaves an unreadable remainder, so the utterance
        must not press anything. This one is a boundary guard, not
        red-first evidence: it was green before the fix too, because the
        old body admitted the phrase and the numeric validation then
        dropped the match. It fails if a later change makes the filler
        repeatable.
        """
        await _speak(harness, ["delete", "number", "number", "three"])

        assert _presses(harness) == [], (
            "a doubled filler is not a count the parser can read"
        )
