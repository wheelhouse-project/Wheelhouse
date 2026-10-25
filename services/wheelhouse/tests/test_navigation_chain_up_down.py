"""Up and down parts in "then" chains, through the real speech pipeline
(wh-voice-chain-up-down).

A chain reaches only the cursor-navigate pattern, which gives the words to
NavigationParser. The parser knew only right and left, so "go up three then
go left two" was typed as text. Each up or down part of a chain must press
the same keys, the same number of times, as the same words said alone. The
single forms are the patterns.toml blocks nav-up-times, nav-up-lines,
nav-up-paragraphs and their down twins, so these tests send each part alone
through the same pipeline and compare the payloads that would go to the
Input process. No desktop input is sent.
"""

import pytest

from speech.navigation.parser import NavigationParser
from tests.test_speech_pipeline import SpeechPipelineHarness


async def speak(phrase):
    """Return the hotkey payloads and the typed text for one utterance."""
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        await harness.send_utterance(phrase.split(), inter_word_delay_ms=0)
        await harness.send_utterance_end_marker(harness._utterance_counter)
        moves = [(out.params["keys"], out.params["repeat"])
                 for out in harness.get_outputs()
                 if out.action == "hotkey_action"]
        return moves, harness.mock_app.get_dictation_texts()
    finally:
        await harness.stop()


async def parts_alone(chain):
    """The moves of each "then" part said alone, joined in spoken order."""
    expected = []
    for part in chain.split(" then "):
        moves, typed = await speak(part)
        assert moves and typed == [], f"{part!r} alone is not a move"
        expected.extend(moves)
    return expected


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", [
    "go up three then go left two",
    "go up 3 then go left 2",
    "go right two then go down four lines",
    "go up two paragraphs then go right one word",
    "go down two then go left one word then go up three times",
    "go up forty then go left two",
    "go down one paragraph then go up then go right three",
])
async def test_each_part_moves_as_the_same_words_alone(chain):
    expected = await parts_alone(chain)
    moves, typed = await speak(chain)
    assert typed == []
    assert moves == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("chain, expected", [
    ("go up three then go left two", [(["up"], 3), (["left"], 2)]),
    ("go up 3 then go left 2", [(["up"], 3), (["left"], 2)]),
    ("go right two then go down four lines", [(["right"], 2), (["down"], 4)]),
    ("go up two paragraphs then go right one word",
     [(["ctrl", "up"], 2), (["ctrl", "right"], 1)]),
    ("go down two then go left one word then go up three times",
     [(["down"], 2), (["ctrl", "left"], 1), (["up"], 3)]),
    # Boss ruling 20:10, addition 1: a count between 31 and 999 clamps to
    # 30, the same as the single "go up forty".
    ("go up forty then go left two", [(["up"], 30), (["left"], 2)]),
])
async def test_the_chain_presses_these_keys(chain, expected):
    """Literal keys, so the test does not depend only on the single forms."""
    moves, typed = await speak(chain)
    assert typed == []
    assert moves == expected


# Boss ruling 20:10, Q3 = 1: the single form reads its count through the
# widened (\d+) group and words_to_int; the chain part reads it through
# parse_number_word. Every phrase here must give the same move both ways.
COUNT_PHRASES = [
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "twenty one",
    "twenty two", "twenty three", "twenty four", "twenty five",
    "twenty six", "twenty seven", "twenty eight", "twenty nine", "thirty",
    "twenty-three", "to three", "forty", "one hundred",
    "1", "3", "23", "30", "40", "999",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("unit", ["", " lines", " paragraphs"])
@pytest.mark.parametrize("count", COUNT_PHRASES)
async def test_a_count_reads_the_same_in_a_chain_and_alone(direction, unit, count):
    part = f"go {direction} {count}{unit}"
    alone, typed_alone = await speak(part)
    assert alone and typed_alone == [], f"{part!r} alone is not a move"
    moves, typed = await speak(f"{part} then go left one")
    assert typed == []
    assert moves == alone + [(["left"], 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", [
    "go up two words then go left one",
    "go down three characters then go left one",
    "grab up three then go left one",
    "go up 0 then go left one",
    "go up 1000 then go left one",
    "go left three times then go up one",
    "go right two lines then go up one",
    "go up the stairs then go left",
])
async def test_a_chain_with_a_part_that_has_no_single_form_is_typed(chain):
    """Q1 = 1 and Q2 = 1 of the Boss ruling 20:10: these chains stay typed
    text, as at origin/dev."""
    moves, typed = await speak(chain)
    assert moves == []
    assert typed != []


@pytest.mark.asyncio
@pytest.mark.parametrize("chain, expected", [
    ("go left two then go right three words",
     [(["left"], 2), (["ctrl", "right"], 3)]),
    ("go left two then grab right three",
     [(["left"], 2), (["shift", "right"], 3)]),
])
async def test_a_chain_without_up_or_down_is_unchanged(chain, expected):
    moves, typed = await speak(chain)
    assert typed == []
    assert moves == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("single, expected", [
    ("go up three", [(["up"], 3)]),
    ("go down two lines", [(["down"], 2)]),
])
async def test_a_single_up_or_down_still_goes_through_its_pattern(
        single, expected, monkeypatch):
    """The single form keeps its patterns.toml route: cursor-navigate, the
    only caller of NavigationParser.parse, never runs."""
    calls = []
    original = NavigationParser.parse

    def recording_parse(utterance):
        calls.append(utterance)
        return original(utterance)

    monkeypatch.setattr(NavigationParser, "parse", staticmethod(recording_parse))
    moves, typed = await speak(single)
    assert typed == []
    assert moves == expected
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("single, expected", [
    ("go to up three", [(["up"], 3)]),
    ("go to left two", [(["left"], 2)]),
])
async def test_go_to_up_moves_through_cursor_navigate_as_go_to_left(
        single, expected, monkeypatch):
    """Boss ruling 23:40, item C: the parser removes "to" after "go" (wh-ed4),
    so "go to up three" said without the safety word now presses Up three
    times; at origin/dev it was typed text. "go to left two" already moved
    left at origin/dev and must not change. Both reach cursor-navigate, the
    only caller of NavigationParser.parse."""
    calls = []
    original = NavigationParser.parse

    def recording_parse(utterance):
        calls.append(utterance)
        return original(utterance)

    monkeypatch.setattr(NavigationParser, "parse", staticmethod(recording_parse))
    moves, typed = await speak(single)
    assert typed == []
    assert moves == expected
    assert calls == [single]
