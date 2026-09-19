"""Scroll count regressions for wh-scroll-word-counts-extend.

The unified number parser already supplies this feature on dev. Exercise the
shipped patterns through the real speech pipeline to the captured Input IPC
payload; no desktop input is sent. These catch truncated compound counts,
restoring the former ten-word limit, wrong directions, and premature scrolling
of ordinary dictation.
"""

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness


async def speak(phrase):
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        await harness.send_utterance(phrase.split(), inter_word_delay_ms=0)
        await harness.send_utterance_end_marker(harness._utterance_counter)
        return harness.get_outputs(), harness.mock_app.get_dictation_texts()
    finally:
        await harness.stop()


# Literal expectations are independent of the production number parser.
COUNTS = [
    ("", 1), ("one", 1), ("ten", 10), ("eleven", 11), ("twelve", 12),
    ("thirteen", 13), ("fourteen", 14), ("fifteen", 15), ("sixteen", 16),
    ("seventeen", 17), ("eighteen", 18), ("nineteen", 19), ("twenty", 20),
    ("twenty one", 21), ("twenty-nine", 29), ("thirty", 30),
    ("thirty seven", 37), ("forty", 40), ("forty-nine", 49), ("fifty", 50),
    ("1", 1), ("11", 11), ("49", 49), ("50", 50),
    ("fifty one", 50), ("51", 50), ("one hundred", 50), ("100", 50),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
@pytest.mark.parametrize("count, clicks", COUNTS)
async def test_complete_scroll_count_reaches_input(direction, count, clicks):
    outputs, dictated = await speak(f"scroll {direction} {count}".strip())
    assert [(out.action, out.params) for out in outputs
            if out.action != "end_utterance"] == [
        ("scroll_wheel", {"direction": direction, "clicks": clicks})
    ]
    assert dictated == []


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
@pytest.mark.parametrize("count", ["zero", "0"])
async def test_zero_count_sends_no_input(direction, count):
    outputs, dictated = await speak(f"scroll {direction} {count}")
    assert [out for out in outputs if out.action != "end_utterance"] == []
    assert dictated == []


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
async def test_streamed_compound_waits_for_the_final_marker(direction):
    """A provider can deliver words before its separate final-result marker."""
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        for index, word in enumerate(["scroll", direction, "twenty", "three"]):
            await harness.send_word(word, start_of_utterance=index == 0)
            assert harness.get_outputs() == []
        await harness.send_utterance_end_marker(harness._utterance_counter)
        assert [(out.action, out.params) for out in harness.get_outputs()
                if out.action != "end_utterance"] == [
            ("scroll_wheel", {"direction": direction, "clicks": 23})
        ]
    finally:
        await harness.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", [
    "scroll down minus one", "scroll down -1", "scroll down twenty and three",
    "scroll down one and and two", "scroll down first", "scroll down fifty cats",
    "scroll down to the bottom", "scroll up eleven pages in the document",
    "please scroll right twenty three", "scroll left one thousand",
])
async def test_invalid_count_or_sentence_stays_dictation(phrase):
    outputs, dictated = await speak(phrase)
    assert all(out.action in {"intelligent_insert_text", "end_utterance"}
               for out in outputs)
    assert " ".join(text.strip() for text in dictated).lower() == phrase
