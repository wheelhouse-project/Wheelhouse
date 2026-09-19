"""A bare grid number clicks its badge when the numbered overlay is up.

wh-overlay-count-homophones.1.1. The three grid patterns
(grid-number-word, grid-number-digit, grid-number-prefixed) match the
whole utterance for every spoken 1..9, so the shipped router classifies
"six", "7", "number three" -- and, since this branch, "to", "too" and
"for" -- as COMMAND and finalizes them into
``ActionFunctions.grid_number_command``. They therefore never reach the
bare-number hold in SpeechProcessor, which lives under the DICTATE
branch. With the mouse grid closed the old fallback typed the words, so
a bare number typed instead of clicking badge N even while badges were
showing -- against the contract in the module docstring of
tests/test_speech_processor_bare_number.py and David's ruling of
2026-08-27 (wh-click-number-dictation.1.2).

Every test here builds the REAL PatternCatalog, passes it to the
SpeechProcessor constructor so the real SpeechRouter reads it, and uses
the REAL TextParser and ActionFunctions. No stub catalog: a MagicMock
catalog makes the router answer DICTATE for every word, which is exactly
the model that hid this defect.

Only the logic controller is faked, because it owns the process
boundary: it records the grid delegation and the click delegation and
answers whether the grid consumed the number.

Precedence under test: the grid gets the number first; the numbered
overlay is consulted only when the grid did not consume it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

from services.wheelhouse.click_overlay_state import OverlayState
from speech.command_engine import TextParser
from speech.pattern_catalog import PatternCatalog
from speech.speech_processor import SpeechProcessor
from speech.word_event import WordEvent


PATTERNS = wheelhouse_dir / "speech" / "config" / "patterns.toml"


# ---------------------------------------------------------------------------
# Fixture doubles (only the process boundary is faked)
# ---------------------------------------------------------------------------

class _RecordingApp:
    """Records the IPC payloads Logic would send to Input."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: Optional[dict] = None,
                           timeout_s: Optional[float] = None):
        payload = {"action": action, "params": params or {}}
        self.actions.append(payload)
        if action == "retract":
            return {"status": "not_retracted", "reason": "nothing_to_retract"}
        return {"status": "ok"}

    def inserted_texts(self) -> List[str]:
        return [
            a["params"].get("insertion_string")
            for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]


class _FakeOverlayMachine:
    def __init__(self, state: OverlayState):
        self.state = state


class _FakeLogicController:
    """Records what Logic was asked to do, and whether the grid consumed."""

    def __init__(self, overlay_state: OverlayState, grid_consumes: bool):
        self.click_overlay_state = _FakeOverlayMachine(overlay_state)
        self.grid_consumes = grid_consumes
        self.grid_calls: List[tuple] = []
        self.clicks: List[Any] = []

    async def handle_grid_command(self, command, trace_id, **kwargs):
        self.grid_calls.append((command, kwargs))
        return self.grid_consumes

    async def forward_click_element(self, query, trace_id):
        self.clicks.append(query)


def _make_stack(overlay_state: OverlayState, *, grid_consumes: bool = False):
    """Real catalog, real router, real parser, real actions."""
    catalog = PatternCatalog(str(PATTERNS))
    lc = _FakeLogicController(overlay_state, grid_consumes)

    speech_handler = MagicMock()
    speech_handler.logic_controller = lc

    app = _RecordingApp()
    speech_handler.app = app

    text_parser = TextParser(speech_handler, catalog)
    processor = SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        logic_controller=lc,
    )
    speech_handler.speech_processor = processor
    return processor, app, lc


def _word(text: str, start: bool = False, uid: int = 1) -> WordEvent:
    return WordEvent(
        word=text,
        start_of_utterance=start,
        end_of_utterance=False,
        utterance_id=uid,
    )


def _end_marker(uid: int = 1) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=uid,
        is_utterance_end_marker=True,
    )


def _speak(processor, spoken: str) -> None:
    async def _run():
        words = spoken.split()
        for index, one in enumerate(words):
            await processor.process_word_event(_word(one, start=(index == 0)))
        await processor.process_word_event(_end_marker())

    asyncio.run(_run())


# The whole shadowed class: the pre-existing grid vocabulary plus the
# three homophones this branch added. ``name`` is what
# ClickCommandParser leaves after the "number" filler is dropped.
SHADOWED = [
    pytest.param("six", "six", id="word-six"),
    pytest.param("four", "four", id="word-four"),
    pytest.param("7", "7", id="digit-7"),
    pytest.param("number three", "three", id="prefixed-three"),
    pytest.param("for", "for", id="homophone-for"),
    pytest.param("to", "to", id="homophone-to"),
    pytest.param("too", "too", id="homophone-too"),
]


@pytest.mark.parametrize(("spoken", "name"), SHADOWED)
def test_badges_showing_and_grid_closed_clicks_the_badge(spoken, name):
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name
    assert lc.clicks[0].role is None
    # The grid was asked first and refused; only then did the overlay run.
    assert lc.grid_calls and lc.grid_calls[0][0] == "refine"


@pytest.mark.parametrize(("spoken", "name"), SHADOWED)
def test_refresh_in_flight_also_clicks_the_badge(spoken, name):
    processor, app, lc = _make_stack(OverlayState.REFRESH_IN_FLIGHT)
    _speak(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name


@pytest.mark.parametrize(("spoken", "name"), SHADOWED)
def test_badges_not_showing_still_types_the_words(spoken, name):
    processor, app, lc = _make_stack(OverlayState.CLOSED)
    _speak(processor, spoken)

    assert lc.clicks == []
    assert app.inserted_texts() == [spoken]


@pytest.mark.parametrize(("spoken", "cell"), [
    pytest.param("six", 6, id="word-six"),
    pytest.param("7", 7, id="digit-7"),
    pytest.param("number three", 3, id="prefixed-three"),
    pytest.param("for", 4, id="homophone-for"),
    pytest.param("to", 2, id="homophone-to"),
    pytest.param("too", 2, id="homophone-too"),
])
def test_grid_open_refines_the_grid_and_never_clicks_a_badge(spoken, cell):
    """Precedence: the grid consumes the number, so the overlay never runs.

    The overlay is PAINTED here on purpose. Mutual exclusion keeps at
    most one of the two open, but if both somehow are, the grid still
    wins because it consumed the number first.
    """
    processor, app, lc = _make_stack(
        OverlayState.PAINTED, grid_consumes=True,
    )
    _speak(processor, spoken)

    assert lc.clicks == []
    assert app.inserted_texts() == []
    assert lc.grid_calls and lc.grid_calls[0][0] == "refine"
    assert lc.grid_calls[0][1].get("number") == cell


def test_a_number_outside_the_grid_vocabulary_still_uses_the_hold():
    """"70" matches no grid pattern, so it takes the bare-number hold.

    This is the path that already worked. It must keep working: the fix
    adds a second way in, it does not replace the hold.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, "70")

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == "70"
    assert lc.grid_calls == []


def test_a_multiword_number_still_extends_the_hold():
    """"twenty for" -> badge 24 through the hold, unchanged by this fix.

    "twenty" matches no grid pattern, so the hold opens on it and "for"
    extends it. The grid never sees the utterance. A bare hold keeps the
    SPOKEN words in the click command it builds, so the query carries
    "twenty for" and main.py resolves it to 24 with the same aliases --
    that resolution is covered in tests/test_voice_overlay_routing.py.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, "twenty for")

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == "twenty for"
    assert lc.grid_calls == []


# ---------------------------------------------------------------------------
# wh-overlay-count-homophones.1.3: the multi-word half of the same class
# ---------------------------------------------------------------------------
#
# The class .1.1 fixed is the utterance the three grid patterns match in
# FULL. This is the rest of it: a spoken badge number of two or more
# words whose FIRST word one of those patterns claims. The router
# classifies that first word as a COMMAND and buffers it, the pair
# matches no pattern, and the buffer finalizes as dictation -- so the
# words typed. Because every spoken form of 100..999 begins with a unit
# word one..nine, no verb-less form of any badge 100..999 reached the
# hold at all.
#
# ``name`` is what ClickCommandParser leaves after the "number"/"numbers"
# filler is dropped. A bare badge click keeps the SPOKEN words and
# main.py resolves them, the same contract
# test_a_multiword_number_still_extends_the_hold records for the hold.

MULTIWORD_SHADOWED = [
    pytest.param("one twelve", "one twelve", id="one-twelve-is-112"),
    pytest.param("one hundred", "one hundred", id="one-hundred-is-100"),
    pytest.param("nine nine nine", "nine nine nine", id="nine-nine-nine-is-999"),
    pytest.param("one seven", "one seven", id="one-seven-is-17"),
    pytest.param("one zero five", "one zero five", id="one-zero-five-is-105"),
    pytest.param("number seventy four", "seventy four", id="number-seventy-four-is-74"),
    pytest.param("numbers 75", "75", id="numbers-75"),
    pytest.param("for two", "for two", id="homophone-led-for-two-is-42"),
    pytest.param("to five", "to five", id="homophone-led-to-five-is-25"),
]


@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_a_multiword_badge_number_led_by_a_grid_word_clicks_its_badge(
    spoken, name,
):
    """The finalized payload clicks the badge instead of typing.

    Red before the fix: every case typed the whole utterance and clicked
    nothing.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name
    assert lc.grid_calls == []


@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_a_multiword_badge_number_also_clicks_while_a_refresh_is_in_flight(
    spoken, name,
):
    """refresh_in_flight is the other badges-showing state."""
    processor, app, lc = _make_stack(OverlayState.REFRESH_IN_FLIGHT)
    _speak(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name


@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_a_multiword_badge_number_still_types_when_no_badges_show(
    spoken, name,
):
    """With the overlay closed these words are ordinary dictation.

    This is the pre-fix behaviour for every one of them, and it must not
    change: the badges-showing gate is the whole of what separates a
    badge pick from a sentence.
    """
    processor, app, lc = _make_stack(OverlayState.CLOSED)
    _speak(processor, spoken)

    assert lc.clicks == []
    assert app.inserted_texts() == [spoken]


def test_a_multiword_payload_that_is_not_only_a_number_still_types():
    """"one twelve is my number" names no badge, so it dictates.

    The number parse is what separates the two, and it must read the
    WHOLE payload. A check that merely found a number inside the payload
    would click badge 112 and swallow the sentence.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, "one twelve is my number")

    assert lc.clicks == []
    assert app.inserted_texts() == ["one twelve is my number"]


@pytest.mark.parametrize(
    ("spoken", "number"),
    [
        pytest.param("click one twelve", 112, id="click-one-twelve"),
        pytest.param("tap one twelve", 112, id="tap-one-twelve"),
        pytest.param("click number seventy four", 74, id="click-number-74"),
    ],
)
def test_the_spoken_click_verb_forms_are_unchanged(spoken, number):
    """The verb forms already worked, through the spoken-click hold.

    They must keep taking that path: it resolves the number to DIGITS,
    which the bare path deliberately does not, so a regression here would
    show up as a spoken-word query name instead of a digit one.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == str(number)


@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED[:3])
def test_an_open_grid_never_receives_a_multiword_badge_number(spoken, name):
    """An open grid is not consulted at all here, so it loses nothing.

    Grid-first precedence is a real constraint on this fix, but it does
    not bind on this input, and the reason is worth pinning rather than
    assuming. A grid pattern claims the FIRST word alone; the second word
    then makes the pair match no pattern, so the router finalizes the
    buffer as DICTATION and ``grid_number_command`` is never called --
    with the grid open or closed. Measured: ``lc.grid_calls`` stays empty
    even with ``grid_consumes=True``.

    So the badge click here takes nothing from the grid. That matches the
    bare-number hold, which has always looked only at the overlay state:
    "70" with the grid open and badges painted clicks badge 70 today. The
    single-word half of the class is where precedence does bind, and
    test_grid_open_refines_the_grid_and_never_clicks_a_badge pins it.
    """
    processor, app, lc = _make_stack(
        OverlayState.PAINTED, grid_consumes=True,
    )
    _speak(processor, spoken)

    assert lc.grid_calls == []
    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name


# ---------------------------------------------------------------------------
# wh-overlay-count-homophones.1.4 (codex round 3): the buffer timer must not
# click. The DICTATE branch also serves the 1000 ms no-end-marker fallback,
# which finalizes MID-utterance, so a user who pauses past the timer would
# have a badge clicked under them and could not take it back.
# ---------------------------------------------------------------------------

def _speak_then_timeout(processor, spoken: str, rest: str = "") -> None:
    """Speak ``spoken``, fire the buffer timer, then speak ``rest``.

    No end marker reaches the processor until after ``rest``, so the
    finalization at the timer has no confirmed utterance end behind it.
    """
    async def _run():
        words = spoken.split()
        for index, one in enumerate(words):
            await processor.process_word_event(_word(one, start=(index == 0)))
        await processor.process_word_event(
            WordEvent.timeout_finalize(
                token=processor.timeout_token, utterance_id=1,
            )
        )
        for one in rest.split():
            await processor.process_word_event(_word(one))
        await processor.process_word_event(_end_marker())

    asyncio.run(_run())


@pytest.mark.parametrize(
    "state",
    [OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT],
    ids=["painted", "refresh"],
)
@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_the_buffer_timer_never_clicks_a_badge(spoken, name, state):
    """A timer finalization types the words; only a confirmed end clicks.

    Red before the gate: every one of these clicked its badge at the
    timer, in both badge-showing states.
    """
    processor, app, lc = _make_stack(state)
    _speak_then_timeout(processor, spoken)

    assert lc.clicks == []
    assert app.inserted_texts() == [spoken]


def test_a_pause_mid_sentence_keeps_every_word():
    """The whole reachable harm, in one case.

    "one twelve" pause "is my number": before the gate the pause clicked
    badge 112 and the first two words never typed, and the click could
    not be taken back -- a click is irreversible and the command flag
    disables retraction.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak_then_timeout(processor, "one twelve", "is my number")

    assert lc.clicks == []
    assert " ".join(app.inserted_texts()).split() == [
        "one", "twelve", "is", "my", "number",
    ]


# ---------------------------------------------------------------------------
# wh-overlay-count-homophones.1.6 (codex round 6): the Mode 1 lifecycle reset
# marker IS a confirmed utterance end, and the badge click there is intended.
# ---------------------------------------------------------------------------
#
# Codex round 6 read this as the sibling defect of .1.4 above -- a badge
# clicked irreversibly on a "mid-utterance phrase split". It is not, for
# two reasons, and these tests pin the behaviour so that reading cannot be
# acted on silently later.
#
# First, the marker is not evidence-free the way the 1000 ms buffer timer
# is. integrations/websocket_manager.py:1417-1447 reaches Mode 1 only
# when the server's final does not extend the stable, AND there was no
# stable disagreement (a revision goes to Mode 3 retract and replay), AND
# no end-of-speech arrived for the utterance, AND the server's own
# final_reason is GOOGLE_SILENCE_2S, EOS_FALLBACK or NO_TEXT_TIMEOUT.
# All three are server-side silence or end-of-speech finalizations, so
# the marker carries positive evidence that phrase 1 ended in a pause.
# The buffer timer carries none, which is why .1.4 gates it.
#
# Second, boss rulings wh-whole-utterance-command-matching.3.1.6 and
# .3.1.8 (option (a) each) already declare this marker a confirmed
# utterance end, and the lifecycle branch has clicked a HELD bare number
# at this close since before this branch's base 82e6796c
# (speech_processor.py line 1146 of that revision). Gating the multi-word
# badge click on an end-marker-only flag would make the same spoken words
# behave differently by internal close path, which is the consistency
# rule those rulings state.

def _lifecycle_reset_marker(uid: int = 1) -> WordEvent:
    """The marker integrations/websocket_manager.py:492-501 builds."""
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=uid,
        is_lifecycle_reset_marker=True,
    )


def _speak_then_lifecycle_reset(processor, spoken: str, rest: str = "") -> None:
    """Speak ``spoken``, close phrase 1 with the marker, speak ``rest``.

    The word order matches _handle_mode1_fresh_content: the marker is
    queued AHEAD of phrase 2's words, and phrase 2's first word carries
    start_of_utterance.
    """
    async def _run():
        words = spoken.split()
        for index, one in enumerate(words):
            await processor.process_word_event(_word(one, start=(index == 0)))
        await processor.process_word_event(_lifecycle_reset_marker())
        for index, one in enumerate(rest.split()):
            await processor.process_word_event(_word(one, start=(index == 0)))
        await processor.process_word_event(_end_marker())

    asyncio.run(_run())


@pytest.mark.parametrize(
    "state",
    [OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT],
    ids=["painted", "refresh"],
)
@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_a_lifecycle_reset_clicks_the_badge(spoken, name, state):
    """Phrase 1 closed by the marker clicks, exactly as an end marker does.

    These fail if the badge check is gated on a flag the end-marker
    branch alone sets: the click never fires and the words type instead.
    """
    processor, app, lc = _make_stack(state)
    _speak_then_lifecycle_reset(processor, spoken)

    assert app.inserted_texts() == []
    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == name


def test_a_lifecycle_reset_clicks_phrase_one_and_dictates_phrase_two():
    """The whole shape, in one case.

    The server merged two phrases into one utterance and finalized after
    a silence. Phrase 1 "one twelve" is a badge pick; phrase 2 "is my
    number" is dictation. Both must happen.
    """
    processor, app, lc = _make_stack(OverlayState.PAINTED)
    _speak_then_lifecycle_reset(processor, "one twelve", "is my number")

    assert len(lc.clicks) == 1
    assert lc.clicks[0].name == "one twelve"
    assert " ".join(app.inserted_texts()).split() == ["is", "my", "number"]


@pytest.mark.parametrize(("spoken", "name"), MULTIWORD_SHADOWED)
def test_a_lifecycle_reset_with_no_badges_showing_still_types(spoken, name):
    """The badges-showing gate still governs; the marker does not bypass it."""
    processor, app, lc = _make_stack(OverlayState.CLOSED)
    _speak_then_lifecycle_reset(processor, spoken)

    assert lc.clicks == []
    assert " ".join(app.inserted_texts()).split() == spoken.split()
