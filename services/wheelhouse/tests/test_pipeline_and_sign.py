r"""The spoken name for '&', and the words that are no longer aliases.

wh-and-sign-index-defect, acceptance A2 and A3. The catalog tests in
tests/test_pattern_catalog_and_sign.py measure the first-word index
directly; these say the words and check what the pipeline actually
inserts, which is the thing the user sees. An index entry that never
reaches an insertion would satisfy the catalog tests and still leave the
bug in place, so both levels are needed.

The shipped pattern read ``\b(?:ampersand|and sign(?:ed)?)\b`` and it
listed three aliases. Only "ampersand" reached the first-word index, so
"and sign" and "and signed" were typed as words. Repairing the extractor
made all three reachable, and measurement then showed what that costs:
this entry is a replacement, so it matches ANYWHERE in an utterance, and
"he read and signed the form" typed "he read & the form" while "please
come and sign" typed "please come &". David answered
QUESTIONS-2026-09-05 item 94 with option three -- drop the two aliases.
The shipped entry is ``\bampersand\b`` now (patterns.toml:3338, doc_id
punct-ampersand), it still fires anywhere, and saying "and sign" types
the words by design. wh-and-sign-index-defect.1.1 holds the
measurements.

TestTheRemovedAliasesTypeTheirWords is what pins that design decision.
Typing those words is the intended outcome, not an accident, so it is
asserted rather than left to the absence of a test: without it, an alias
put back into patterns.toml would break no test at this level.

TestOrdinaryDictationKeepsItsWords is the negative control for the whole
change, and it is NOT red-first evidence: its first two tests pass before
the repair and must keep passing after it. Adding "and" to the first-word
index would mean the router searches this pattern for every utterance
that starts with "and", and those utterances must still be typed as
words. Its four-sentence case is different -- those are the sentences
that measurably lost a word while the aliases were indexed, so they ARE
red-first evidence for item 94.

Those tests assert the WORDS the pipeline typed, not how it grouped them
into insertions, and the difference is load-bearing. A word in the
replacement first-word index makes the router open a replacement buffer
on it (router.py, "Fresh replacement buffering after impossible
command"); the buffer cannot match, and the router defers the impossible
buffer to the utterance end rather than releasing it at word speed
(router.py, "Impossible buffer deferred to utterance end", the
wh-whole-utterance-command-matching.3 behaviour). The end marker then
finalizes the whole remainder as dictation in ONE insertion. That is the
shipped behaviour for every word already in that index -- "open",
"question", "period".

Neither form can be reconstructed by a plain join: an insertion string
carries the spaces INSIDE it but not the space between one insertion and
the next, which intelligent_insert_text supplies at the receiving
application. So these tests join on a space and split on whitespace, and
compare the word sequence. That is exactly what acceptance A3 asks -- the
words are typed. Each test also asserts no "&" appears, which is the
stray-mark failure the repair could plausibly cause.
"""
import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from tests.test_speech_pipeline import SpeechPipelineHarness


async def _speak(words, inter_word_delay_ms=50):
    """Say ``words`` as ONE utterance; return (insertions, actions).

    One utterance means every word carries the same utterance id, the
    first carries start_of_utterance and the last end_of_utterance,
    followed by the end marker the STT bridge sends on a final result.

    The 0.3 s wait after the marker is a settling window for the
    negative assertions, not a race margin for the expected output: the
    last word carries end_of_utterance, so the pipeline decides and
    emits inside send_utterance above. It is the same helper shape and
    the same reasoning as tests/test_pipeline_punctuation_names.py,
    which records the measurement behind that number.
    """
    harness = SpeechPipelineHarness()
    await harness.start()
    try:
        await harness.send_utterance(
            words, inter_word_delay_ms=inter_word_delay_ms
        )
        await harness.send_utterance_end_marker(harness._utterance_counter)
        await asyncio.sleep(0.3)
        outputs = harness.get_outputs()
    finally:
        await harness.stop()
    return (
        [
            out.params.get("insertion_string", "")
            for out in outputs
            if out.action == "intelligent_insert_text"
        ],
        [out.action for out in outputs],
    )


class TestTheSpokenNameInsertsTheMark:
    """Acceptance A2: the emitted action, not index membership."""

    @pytest.mark.asyncio
    async def test_the_spoken_name_inserts_exactly_the_mark(self):
        insertions, actions = await _speak(["ampersand"])

        assert insertions == ["&"], (
            "'ampersand' did not insert exactly '&'. "
            f"Insertions: {insertions!r}; actions: {actions!r}"
        )

    @pytest.mark.asyncio
    async def test_the_spoken_name_still_inserts_the_mark_mid_sentence(self):
        # Item 94 keeps punct-ampersand unanchored, so "ampersand" fires
        # wherever it appears. This is the half of the entry the answer
        # deliberately did NOT change.
        #
        # The sentence must not START with a command word. Measured on
        # this branch: "press the ampersand key" types every word,
        # because "press" opens a command path that claims the rest of
        # the utterance, while "the ampersand key" gives ['the', '&',
        # 'key'] and "put an ampersand here" gives ['put', 'an', '&',
        # 'here']. That command-word behaviour predates this branch and
        # is not what this test is about.
        insertions, actions = await _speak(["the", "ampersand", "key"])

        assert "&" in " ".join(insertions), (insertions, actions)


class TestTheRemovedAliasesTypeTheirWords:
    """Acceptance A2, the other half: item 94 dropped the two aliases.

    "and sign" and "and signed" are ordinary English words now. Typing
    them is the INTENDED behaviour, not an accident, so it is pinned
    here rather than left to the absence of a test.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [["and", "sign"], ["and", "signed"]],
        ids=["and-sign", "and-signed"],
    )
    async def test_the_removed_alias_types_its_words(self, words):
        insertions, actions = await _speak(words)

        assert " ".join(insertions).split() == words, (insertions, actions)
        assert "&" not in " ".join(insertions), (insertions, actions)


class TestOrdinaryDictationKeepsItsWords:
    """Acceptance A3: "and" in ordinary speech is still typed."""

    @pytest.mark.asyncio
    async def test_and_between_two_words_is_typed(self):
        insertions, actions = await _speak(["salt", "and", "pepper"])

        assert " ".join(insertions).split() == ["salt", "and", "pepper"], (
            insertions,
            actions,
        )
        assert "&" not in " ".join(insertions), (insertions, actions)

    @pytest.mark.asyncio
    async def test_and_at_the_start_of_a_sentence_is_typed(self):
        # The harder of the two: "and" is the FIRST word, so it is the
        # word the router looks up in the first-word index. This is the
        # utterance the repair newly routes into a pattern search.
        insertions, actions = await _speak(["and", "then", "we", "left"])

        assert " ".join(insertions).split() == [
            "and",
            "then",
            "we",
            "left",
        ], (insertions, actions)
        assert "&" not in " ".join(insertions), (insertions, actions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "words",
        [
            ["I", "checked", "it", "and", "signed", "it"],
            ["please", "come", "and", "sign"],
            ["he", "read", "and", "signed", "the", "form"],
            ["the", "contract", "and", "sign", "your", "name"],
        ],
        ids=[
            "checked-and-signed",
            "come-and-sign",
            "read-and-signed",
            "contract-and-sign",
        ],
    )
    async def test_a_sentence_containing_the_old_alias_types_every_word(
        self, words
    ):
        # The four sentences the reviewer_0 round measured going wrong
        # while the aliases were indexed (wh-and-sign-index-defect.1.1).
        # "he read and signed the form" typed "he read & the form" then.
        # Item 94 removed the aliases, so every word comes back.
        insertions, actions = await _speak(words)

        assert " ".join(insertions).split() == words, (insertions, actions)
        assert "&" not in " ".join(insertions), (insertions, actions)
