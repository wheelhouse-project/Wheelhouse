r"""A Pattern Manager phrase of three or more words, end to end.

wh-multiword-phrase-replacement, found as wh-codex-merge-audit.15.1.2.

Pattern Manager simple mode saves the phrase "paste my signature" as
``\b(?:paste\ my\ signature)\b``: speech/phrase_expression.py passes the
phrase through ``re.escape``, which writes each internal space as an
escaped space. These tests speak such phrases through the whole pipeline
(WordEvent -> SpeechProcessor -> TextParser -> UIActionHandler) and read
what was typed.

The fixture builds the user file through the real producer and writer
(``PatternManager.create_pattern`` with ``phrases=[...]``) and loads it
beside the shipped patterns.toml, so the shipped command
``^paste(?: that| here)?$`` (whole_utterance_only) collides with the first
word of "paste my signature" and "paste date" exactly as it does for a
user.

Word shape: the shipped remote path. Every real word carries
``end_of_utterance=False``, the first word of each utterance carries
``start_of_utterance=True``, and a separate empty end marker closes each
utterance. tests/e2e/test_e2e_utterance_end_replacement.py records why
this shape matters.
"""
import asyncio
from pathlib import Path

import pytest

from services.wheelhouse.tests.e2e.e2e_harness import E2EPipelineHarness
from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager


SHIPPED_PATTERNS = (
    Path(__file__).resolve().parent.parent.parent
    / "speech" / "config" / "patterns.toml"
)

# The smallest system file PatternManager accepts, in the shape
# tests/test_pattern_phrases.py uses. The manager only writes the user
# file; the catalog below loads the SHIPPED system file instead.
SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "s"] }\n'
    ']\n'
)

# Replacement texts that TextPerfector leaves unchanged apart from a
# leading space: each starts with a capital letter or a digit, so
# sentence-start capitalization has nothing to change.
SIGNATURE_OUTPUT = "Best regards, Sam Rivera"
HERE_WE_GO_OUTPUT = "Liftoff!"
DATE_OUTPUT = "2026-09-13"
SINCERELY_OUTPUT = "Yours truly,"
ZOOM_PHRASE_OUTPUT = "Magnified view"

PHRASE_OUTPUTS = {
    "paste my signature": SIGNATURE_OUTPUT,
    "here we go": HERE_WE_GO_OUTPUT,
    "paste date": DATE_OUTPUT,
    "sincerely": SINCERELY_OUTPUT,
    # Opens with the whole shipped command ``^zoom in$``; see
    # TestACommandThatOpensASavedPhrase.
    "zoom in closer": ZOOM_PHRASE_OUTPUT,
}


@pytest.fixture(scope="module")
def phrase_catalog(tmp_path_factory):
    """The shipped catalog plus a user file saved by Pattern Manager."""
    directory = tmp_path_factory.mktemp("multiword_phrase_e2e")
    system_file = directory / "system_patterns.toml"
    system_file.write_text(SYSTEM_CONTENT, encoding="utf-8")
    user_file = directory / "user_patterns.toml"
    manager = PatternManager(str(system_file), str(user_file))
    for phrase, output in PHRASE_OUTPUTS.items():
        result = manager.create_pattern(
            trigger="",
            pattern_type="replacement",
            action_type="text",
            action_params={"output": output},
            phrases=[phrase],
        )
        assert result["success"], (phrase, result)
    return PatternCatalog(str(SHIPPED_PATTERNS), user_patterns_file=str(user_file))


@pytest.fixture
async def harness(phrase_catalog):
    """Create and start an E2E pipeline harness on the phrase catalog."""
    h = E2EPipelineHarness(catalog=phrase_catalog)
    await h.start()
    yield h
    await h.stop()


async def _speak(harness, words):
    """Say ``words`` as one utterance in the shipped remote word shape."""
    for position, word in enumerate(words):
        await harness.send_word(
            word,
            start_of_utterance=(position == 0),
            end_of_utterance=False,
            delay_before_ms=0 if position == 0 else 50,
        )
    await harness.send_utterance_end_marker(harness._utterance_counter)


def _stripped(harness):
    """Each delivery without the spacing TextPerfector puts around it."""
    return [text.strip() for text in harness.recording.text_deliveries]


def _joined_words(harness):
    """Everything typed, in lower case, with one space between words."""
    return " ".join(" ".join(harness.recording.text_deliveries).lower().split())


class TestTheWholePhraseTypesItsReplacement:
    """Criterion 2 of wh-multiword-phrase-replacement.

    The two sequences recorded on wh-codex-merge-audit.15.1.2: the phrase
    said in one breath after a word that opens a shipped command, and the
    phrase split by an utterance end after its second word.
    """

    @pytest.mark.asyncio
    async def test_multiword_phrase_in_one_breath_types_the_replacement(
            self, harness):
        r"""Sequence 1: "please paste my signature now" in one utterance.

        "paste" also opens the shipped command ``^paste(?: that| here)?$``,
        so the router buffers it in MID_REPLACEMENT_BUFFERING, and in that
        mode a buffer the router judges impossible is finalized at once.
        The replacement must be typed between the two dictated words.
        """
        await _speak(harness, ["please", "paste", "my", "signature", "now"])
        await asyncio.sleep(0.3)

        delivered = harness.recording.text_deliveries
        assert SIGNATURE_OUTPUT in _stripped(harness), (
            "The saved phrase 'paste my signature' must type its "
            f"replacement; got {delivered}"
        )
        assert len(delivered) == 3, (
            f"Expected please / replacement / now, got {delivered}")
        assert delivered[0].strip().lower() == "please", f"Got {delivered}"
        assert delivered[1].strip() == SIGNATURE_OUTPUT, f"Got {delivered}"
        assert delivered[2].strip().lower() == "now", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_multiword_phrase_paused_after_two_words_types_the_replacement(
            self, harness):
        """Sequence 2: "here we", the utterance ends, then "go".

        The processor holds words across an utterance end only when the
        router calls them an unfinished replacement name. The 150 ms pause
        is inside the harness release deadline (replacement_timeout_ms=400),
        so a held "here we" is still waiting when "go" opens the next
        utterance. The 600 ms wait at the end lets that deadline pass, so
        a wrong extra delivery would show.
        """
        await _speak(harness, ["here", "we"])
        await asyncio.sleep(0.15)
        await _speak(harness, ["go"])
        await asyncio.sleep(0.6)

        assert _stripped(harness) == [HERE_WE_GO_OUTPUT], (
            "'here we', an utterance end, then 'go' must type the "
            f"replacement; got {harness.recording.text_deliveries}"
        )


class TestShorterPhrasesAndUnfinishedOpenings:
    """Guards: behavior that passes at 88e6f6ff and must not change.

    The whole phrase said in one utterance, a two-word phrase after the same
    command collision, and a one-word phrase all type their replacements
    (acceptance criterion 4). An opening that the user never completes,
    "here we are", types every word, whether or not an utterance end falls
    after "we".
    """

    @pytest.mark.asyncio
    async def test_multiword_phrase_in_one_utterance_still_types_the_replacement(
            self, harness):
        await _speak(harness, ["here", "we", "go"])
        await asyncio.sleep(0.3)

        assert _stripped(harness) == [HERE_WE_GO_OUTPUT], (
            f"Got {harness.recording.text_deliveries}")

    @pytest.mark.asyncio
    async def test_multiword_phrase_two_word_phrase_after_a_collision_still_fires(
            self, harness):
        await _speak(harness, ["please", "paste", "date", "now"])
        await asyncio.sleep(0.3)

        delivered = harness.recording.text_deliveries
        assert len(delivered) == 3, (
            f"Expected please / date / now, got {delivered}")
        assert delivered[0].strip().lower() == "please", f"Got {delivered}"
        assert delivered[1].strip() == DATE_OUTPUT, f"Got {delivered}"
        assert delivered[2].strip().lower() == "now", f"Got {delivered}"

    @pytest.mark.asyncio
    async def test_multiword_phrase_one_word_phrase_still_fires(self, harness):
        await _speak(harness, ["sincerely"])
        await asyncio.sleep(0.3)

        assert _stripped(harness) == [SINCERELY_OUTPUT], (
            f"Got {harness.recording.text_deliveries}")

    @pytest.mark.asyncio
    async def test_multiword_phrase_unfinished_opening_types_every_word(
            self, harness):
        await _speak(harness, ["here", "we", "are"])
        await asyncio.sleep(0.3)

        assert HERE_WE_GO_OUTPUT not in _stripped(harness), (
            f"Got {harness.recording.text_deliveries}")
        assert _joined_words(harness) == "here we are", (
            f"Got {harness.recording.text_deliveries}")

    @pytest.mark.asyncio
    async def test_multiword_phrase_unfinished_opening_across_the_end_types_every_word(
            self, harness):
        """The paused shape of the test above. The wait at the end is past
        the release deadline, so words held at the utterance end have been
        released by the time the deliveries are read."""
        await _speak(harness, ["here", "we"])
        await asyncio.sleep(0.15)
        await _speak(harness, ["are"])
        await asyncio.sleep(0.6)

        assert HERE_WE_GO_OUTPUT not in _stripped(harness), (
            f"Got {harness.recording.text_deliveries}")
        assert _joined_words(harness) == "here we are", (
            f"Got {harness.recording.text_deliveries}")


class TestACommandThatOpensASavedPhrase:
    """Guard: a shipped two-word command still runs when a saved phrase
    opens with the same two words.

    The shipped ``^zoom in$`` (doc_id zoom-in in speech/config/patterns.toml,
    whole_utterance_only, no hotword) presses Ctrl+Plus. The saved phrase
    "zoom in closer" opens with the same two words. At 88e6f6ff the catalog
    holds no matcher for the opening "zoom in"; once the escaped space is a
    separator, the buffer ["zoom", "in"] is an opening of that phrase as
    well as the whole command. "zoom in" said as the whole utterance must
    still run the command, and none of its words may be typed.
    """

    @pytest.mark.asyncio
    async def test_multiword_phrase_shared_opening_still_runs_the_command(
            self, harness):
        """The 600 ms wait lets the release deadline
        (replacement_timeout_ms=400) pass, so words held at the utterance
        end and released later would show."""
        await _speak(harness, ["zoom", "in"])
        await asyncio.sleep(0.6)

        keys = harness.recording.get_keystroke_keys()
        assert ("ctrl", "+") in keys, (
            "'zoom in' said as the whole utterance must run the shipped "
            f"command; got keys {keys} and text "
            f"{harness.recording.text_deliveries}")
        assert harness.recording.text_deliveries == [], (
            "'zoom in' must not type any word; got "
            f"{harness.recording.text_deliveries}")
