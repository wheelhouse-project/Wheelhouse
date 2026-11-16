"""A count word in interim text makes the router wait for the final.

wh-itn-lone-number-ten.1.1. Parakeet runs apply_itn on every interim
decode, and the agreed interim words reach the router as stable words
(audio_processor.py releases them after a 300 ms silence). The router
waits for a count only while the trailing number word can still grow
(router.py _count_can_still_grow -> number_phrase_can_extend), and a
digit token cannot grow. So the interim text must keep "backspace
twenty" as words: if it typed "backspace 20", the router would press
backspace 20 times at once, and the final "backspace 23" could not
correct it (speech_processor.py skips the retraction after a command
ran in the utterance).

Both provider texts come from the real apply_itn, so the test fails
when the interim keyword is ignored.
"""
import asyncio

import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness
from services.wheelhouse.tests.e2e.test_e2e_held_prefix_restore_scope import (
    _load_apply_itn,
    _start_utterance,
)


@pytest.fixture
async def harness(pattern_catalog):
    h = E2EPipelineHarness(
        catalog=pattern_catalog,
        action_handler_foreground=True,
    )
    await h.start()
    yield h
    await h.stop()


def _backspace_presses(harness):
    return sum(
        1 for keys, _ in harness.recording.keystrokes if keys == ("backspace",)
    )


@pytest.mark.asyncio
async def test_a_trailing_tens_word_in_interim_text_waits_for_the_count(
    harness,
):
    apply_itn = _load_apply_itn()
    stable_words = apply_itn("backspace twenty", interim=True).split()
    final_text = apply_itn("backspace twenty three")
    assert final_text == "backspace 23"

    await _start_utterance(harness, 1)
    for position, word in enumerate(stable_words):
        await harness.send_word(
            word,
            start_of_utterance=(position == 0),
            delay_before_ms=0 if position == 0 else 50,
        )
    utterance_id = harness._utterance_counter

    # The 300 ms silence after which audio_processor releases the
    # agreed interim words as stable words.
    await asyncio.sleep(0.3)
    assert _backspace_presses(harness) == 0, (
        "The stable text can still grow into a larger count, so the "
        f"router must wait. Stable words: {stable_words!r}. "
        f"Keystrokes: {harness.recording.keystrokes!r}"
    )

    await harness.send_retraction_marker(utterance_id, final_text)
    await asyncio.sleep(1.5)
    assert _backspace_presses(harness) == 23, (
        f"The final {final_text!r} must press backspace 23 times. "
        f"Stable words: {stable_words!r}. "
        f"Keystrokes: {harness.recording.keystrokes!r}"
    )
