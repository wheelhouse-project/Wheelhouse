"""Tests for bare-number click routing (wh-click-number-dictation).

Distil-Whisper drops the word "click" from short "click N" finals: the
FINAL hypothesis is a bare number ('6', '70'), which matches no command
pattern and falls through to dictation, so the spoken click never fires.

The fix: while the overlay is showing badges (state painted or
refresh_in_flight), a FINAL utterance that is JUST a number 1..999 is
held back from dictation and, at the utterance-end marker, executed as
the click command "click N". Every other case keeps today's dictation:
overlay closed or mid-transition, out-of-range numbers, numbers that are
not the whole utterance, and processors with no logic controller.

Covers:
- bare digit / word-form number + painted overlay -> "click N" executes
- bare number + closed overlay -> dictated unchanged
- out-of-range ('1000') -> dictated, never held
- number mid-utterance or followed by more words -> dictated in order
- retraction replay of a bare-number final (the dominant live failure,
  UTT-30 2026-08-08) -> clicks instead of typing the number
- overlay leaves the badge-showing states between hold and utterance
  end -> falls back to dictation
- no logic controller wired (legacy fixtures) -> dictated unchanged
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from services.wheelhouse.click_overlay_state import OverlayState
from speech.click_parser import ClickCommandParser
from speech.word_event import WordEvent
from speech.speech_processor import SpeechProcessor
from speech.domain import Action, Decision, ProcessingMode


# ============================================================================
# MOCK COMPONENTS
# ============================================================================

class MockApp:
    """Mock app recording IPC calls."""

    def __init__(self):
        self.actions: List[Dict[str, Any]] = []
        self._retract_response: Dict[str, Any] = {
            "status": "not_retracted", "reason": "nothing_to_retract",
        }

    async def send_command(self, payload: dict):
        self.actions.append(payload)

    async def send_request(self, action: str, params: Optional[dict] = None,
                           timeout_s: Optional[float] = None):
        payload = {"action": action, "params": params or {}}
        self.actions.append(payload)
        if action == "retract":
            return self._retract_response
        return {"status": "ok"}

    def inserted_texts(self) -> List[str]:
        return [
            a["params"].get("insertion_string")
            for a in self.actions
            if a.get("action") == "intelligent_insert_text"
        ]


class MockTextParser:
    """Mock text parser that records executed command text."""

    def __init__(self):
        self.executed: List[str] = []
        self.last_executed_pattern_type: Optional[str] = "command"
        # Set False to stand in for a command text that matches no
        # pattern, which is the bare-number fallback's trigger.
        self.result: bool = True

    async def parse_and_execute(self, text, return_remainder=False,
                                authorized_command=False):
        self.executed.append(text)
        if return_remainder:
            return self.result, ""
        return self.result


class FakeOverlayMachine:
    """Stands in for ClickOverlayStateMachine: just the .state field."""

    def __init__(self, state: OverlayState):
        self.state = state


def make_processor(overlay_state: Optional[OverlayState] = None,
                   logic_controller: Any = "from_state"):
    """Create a SpeechProcessor with mocked dependencies.

    overlay_state=None with the default logic_controller sentinel wires
    no controller at all (the legacy-fixture shape). Passing a state
    wires a controller whose click_overlay_state reports that state.
    """
    app = MockApp()
    queue = asyncio.Queue()
    catalog = MagicMock()
    catalog.command_hotword = "x-ray"
    catalog.lookup.return_value = None
    catalog.get_trailing_command.return_value = None
    text_parser = MockTextParser()

    if logic_controller == "from_state":
        if overlay_state is None:
            logic_controller = None
        else:
            logic_controller = MagicMock()
            logic_controller.click_overlay_state = FakeOverlayMachine(
                overlay_state,
            )

    processor = SpeechProcessor(
        word_queue=queue,
        catalog=catalog,
        text_parser=text_parser,
        app=app,
        logic_controller=logic_controller,
    )
    return processor, app, text_parser


def word(text: str, start: bool = False, uid: int = 1) -> WordEvent:
    return WordEvent(
        word=text,
        start_of_utterance=start,
        end_of_utterance=False,
        utterance_id=uid,
    )


def end_marker(uid: int = 1) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=True,
        utterance_id=uid,
        is_utterance_end_marker=True,
    )


def retraction_marker(final_text: str, uid: int = 1) -> WordEvent:
    return WordEvent(
        word="",
        start_of_utterance=False,
        end_of_utterance=False,
        utterance_id=uid,
        is_retraction_marker=True,
        retraction_full_text=final_text,
    )


# ============================================================================
# TESTS: bare number becomes a click while badges are showing
# ============================================================================

class TestBareNumberClicksWhilePainted:

    @pytest.mark.asyncio
    async def test_bare_digit_final_executes_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click 70"]
        assert app.inserted_texts() == []
        # The click is a command: later STT revisions must not retract it.
        assert proc._command_executed_in_utterance is True

    @pytest.mark.asyncio
    async def test_word_form_number_executes_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("six", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click six"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_refresh_in_flight_also_accepts(self):
        proc, app, parser = make_processor(OverlayState.REFRESH_IN_FLIGHT)
        await proc.process_word_event(word("7", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click 7"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_end_utterance_still_sent(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        end_calls = [
            a for a in app.actions if a.get("action") == "end_utterance"
        ]
        assert len(end_calls) == 1

    @pytest.mark.asyncio
    async def test_retraction_replay_bare_number_clicks(self):
        """The dominant live failure (UTT-30, 2026-08-08): STABLE 'click'
        was command-buffered, the FINAL rewrote the utterance to '70',
        and the retract+replay path typed '70' (or dropped it). With the
        overlay painted, the replayed bare number must click badge 70.
        The websocket manager queues the utterance-end marker right
        behind the retraction marker, so the test replays both."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.buffer = ["click"]
        proc.mode = ProcessingMode.COMMAND_BUFFERING

        await proc.process_word_event(retraction_marker("70"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click 70"]
        assert app.inserted_texts() == []


# ============================================================================
# TESTS: everything else keeps today's dictation
# ============================================================================

class TestBareNumberKeepsDictationOtherwise:

    @pytest.mark.asyncio
    async def test_overlay_closed_dictates(self):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]

    @pytest.mark.asyncio
    async def test_out_of_range_number_dictates(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("1000", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["1000"]

    @pytest.mark.asyncio
    async def test_number_mid_utterance_dictates_in_order(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("hello", start=True))
        await proc.process_word_event(word("6"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["hello", "6"]

    @pytest.mark.asyncio
    async def test_number_followed_by_more_words_dictates_in_order(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("6", start=True))
        await proc.process_word_event(word("pack"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["6", "pack"]

    @pytest.mark.asyncio
    async def test_overlay_closes_between_hold_and_end_marker(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("70", start=True))
        machine = proc.logic_controller.click_overlay_state
        machine.state = OverlayState.CLOSED
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]

    @pytest.mark.asyncio
    async def test_no_logic_controller_dictates(self):
        proc, app, parser = make_processor(overlay_state=None)
        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]

    @pytest.mark.asyncio
    async def test_walk_in_flight_dictates(self):
        proc, app, parser = make_processor(OverlayState.WALK_IN_FLIGHT)
        await proc.process_word_event(word("70", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]


# ============================================================================
# TESTS: a multi-word bare number is still a badge pick (wh-click-number-dictation)
# ============================================================================
#
# parse_number_word already reads the multi-word forms a user speaks at the
# badges: "twenty three" is 23, "one twelve" is 112, "one seven" is 17. The
# hold above was single-word only, so those forms dictated while "seventeen"
# clicked. The published help row says a bare spoken number ALWAYS selects the
# numbered label, so these click too.


class TestMultiWordBareNumberClicks:

    @pytest.mark.asyncio
    async def test_two_word_tens_executes_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click twenty three"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_colloquial_hundreds_pairing_executes_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("one", start=True))
        await proc.process_word_event(word("twelve"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click one twelve"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_three_word_number_executes_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("one", start=True))
        await proc.process_word_event(word("hundred"))
        await proc.process_word_event(word("five"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click one hundred five"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_retraction_replay_of_a_multi_word_number_clicks(self):
        """The replay path is the dominant live failure and the
        multi-word version of it had no test (deepseek round 1,
        wh-click-number-dictation.1.3). STABLE "click" was command-
        buffered, the FINAL rewrote the utterance to "twenty three", and
        the retract+replay must reach the same click a mid-utterance
        extension does. The replay marks only its FIRST word as opening
        the utterance, which is what lets the second word extend the
        hold rather than flush it."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.buffer = ["click"]
        proc.mode = ProcessingMode.COMMAND_BUFFERING

        await proc.process_word_event(retraction_marker("twenty three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click twenty three"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_click_is_a_command_for_retraction(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert proc._command_executed_in_utterance is True


class TestMultiWordBareNumberKeepsDictationOtherwise:

    @pytest.mark.asyncio
    async def test_words_that_stop_parsing_dictate_in_order(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(word("skidoo"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty", "three", "skidoo"]

    @pytest.mark.asyncio
    async def test_overlay_closed_dictates_every_word(self):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty", "three"]

    @pytest.mark.asyncio
    async def test_out_of_range_multi_word_dictates(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("one", start=True))
        await proc.process_word_event(word("thousand"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["one", "thousand"]

    @pytest.mark.asyncio
    async def test_overlay_closes_between_hold_and_end_marker(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        machine = proc.logic_controller.click_overlay_state
        machine.state = OverlayState.CLOSED
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty", "three"]

    @pytest.mark.asyncio
    async def test_badges_vanishing_mid_number_stops_the_accumulation(self):
        """A number interrupted by the badges going away is not a pick.

        The accumulation checks the overlay on every word, not only on
        the first one. Without that check the hold would survive the
        badges disappearing and click whatever the reopened overlay
        happens to number 23.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        machine = proc.logic_controller.click_overlay_state
        machine.state = OverlayState.CLOSED
        await proc.process_word_event(word("three"))
        machine.state = OverlayState.PAINTED
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty", "three"]

    @pytest.mark.asyncio
    async def test_a_deferred_word_the_router_claims_flushes_the_hold(self):
        """The router, not the hold, decides what a word is.

        The flush guard defers a word that could extend the number so
        the router classifies it first. When the router claims it for a
        command buffer rather than dictation, the held words dictate
        before that command runs, in the spoken order.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        assert proc._pending_bare_number_words == ["twenty"]

        proc._bare_number_deferred_word = "three"
        await proc._execute_decision(
            Decision(action=Action.BUFFER, payload="three")
        )

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty"]
        assert proc._pending_bare_number_words is None
        assert proc._bare_number_deferred_word is None

    @pytest.mark.asyncio
    async def test_a_new_utterance_word_never_joins_the_previous_number(self):
        """Two utterances must not fuse into one number when the first
        one's end marker is lost (deepseek round 1,
        wh-click-number-dictation.1.1).

        The lost terminal FINAL is a real failure mode -- it is why the
        replacement-prefix hold carries a release deadline. Without this
        guard the user said "twenty", got no end marker, then said
        "three" as their own utterance, and badge 23 was clicked: a
        number nobody spoke. The other two holds in this file already
        refuse the same fusion, the trailing hold by flushing on every
        following event and the replacement-prefix hold by testing
        start_of_utterance explicitly.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        assert proc._pending_bare_number_words == ["twenty"]

        await proc.process_word_event(word("three", start=True, uid=2))
        await proc.process_word_event(end_marker(uid=2))

        assert parser.executed == ["click three"]
        assert app.inserted_texts() == ["twenty"]

    @pytest.mark.asyncio
    async def test_a_new_utterance_word_that_is_not_a_number_also_flushes(self):
        """The baseline the case above is measured against: a new
        utterance opening on a word that cannot extend the number
        already flushed, because the guard flushed on any word it could
        not use. Kept so a future change cannot fix one half by
        breaking the other."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("hello", start=True, uid=2))
        await proc.process_word_event(end_marker(uid=2))

        assert parser.executed == []
        assert app.inserted_texts() == ["twenty", "hello"]

    @pytest.mark.asyncio
    async def test_unmatched_click_pattern_dictates_what_was_spoken(self):
        # A user pattern override can leave "click N" matching nothing.
        # The fallback dictates the words the user actually SPOKE, one
        # per word, never the synthesized "click twenty three".
        proc, app, parser = make_processor(OverlayState.PAINTED)
        parser.result = False
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click twenty three"]
        assert app.inserted_texts() == ["twenty", "three"]


class TestTheNumberFillerOpensTheHold:
    """David, 2026-08-27: "number n should click bubble n".

    parse_number_word reads "number three" as 3 for exactly this reason
    (its module docstring: users say "click number three" at the
    badges). When the recogniser drops the leading "click", the final is
    "number three" -- and "number" alone parses to None, so the hold
    never opened and both words dictated
    (wh-click-number-dictation.1.2).

    Forms with a non-parsing word INSIDE the number, "one hundred and
    five", stay deferred: David did not order them.
    """

    @pytest.mark.asyncio
    async def test_number_three_clicks_badge_three(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("number", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click number three"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_the_plural_filler_opens_the_hold_too(self):
        """STT hears "numbers" after a "numbers" command often enough that
        click_parser._NUMBER_FILLERS already carries both spellings; the
        hold uses that same set rather than a second copy."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("numbers", start=True))
        await proc.process_word_event(word("75"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click numbers 75"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_a_lone_filler_is_dictated_never_clicked(self):
        """The companion guard, and the reason this change needs one.

        Every hold before this one parsed as a number on its own, so
        "click <held>" could only ever be a badge. A hold that opens on
        a non-parsing word can reach the end marker still holding just
        that word, and "click number" does NOT drop the filler --
        click_parser only drops it in front of something that parses
        (click_parser.py:189). It would resolve BY NAME and click an
        element called "number". So the consume path checks the held
        words parse before synthesizing anything.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("number", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["number"]

    @pytest.mark.asyncio
    async def test_a_lone_plural_filler_is_dictated_never_clicked(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("numbers", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["numbers"]

    @pytest.mark.asyncio
    async def test_a_real_name_beginning_with_the_filler_still_dictates(self):
        """"number pad" is a thing on screen, not badge anything. The
        accumulation already refuses it -- "number pad" does not parse --
        and both words reach dictation in spoken order."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("number", start=True))
        await proc.process_word_event(word("pad"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["number", "pad"]

    @pytest.mark.asyncio
    async def test_the_filler_needs_the_badges_showing(self):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word("number", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["number", "three"]

    @pytest.mark.asyncio
    async def test_the_filler_only_opens_a_hold_at_the_start(self):
        """Mid-utterance the filler is an ordinary word, exactly as a
        mid-utterance digit is."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("say", start=True))
        await proc.process_word_event(word("number"))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["say", "number", "three"]

    @pytest.mark.asyncio
    async def test_the_hundred_and_form_is_still_deferred(self):
        """Pinned so the deferral is a decision on the record rather than
        a gap someone later reads as an oversight. "one hundred and" is a
        dangling connective that parse_number_word deliberately refuses,
        so the accumulation flushes at "and" (wh-click-number-dictation
        .1.2, deferred by David 2026-08-27)."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("one", start=True))
        await proc.process_word_event(word("hundred"))
        await proc.process_word_event(word("and"))
        await proc.process_word_event(word("five"))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["one", "hundred", "and", "five"]

    @pytest.mark.asyncio
    async def test_the_filler_click_is_a_command_for_retraction(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("number", start=True))
        await proc.process_word_event(word("three"))
        await proc.process_word_event(end_marker())

        assert proc._command_executed_in_utterance is True

# ============================================================================
# TESTS: the overlay changing between the spoken number and its end marker
# ============================================================================
#
# Three separate layers read the overlay state: hold time
# (_maybe_hold_bare_number), extension time (_bare_number_extends) and
# consume time (_consume_pending_bare_number). Every test above drives one
# overlay state for the whole utterance, so the three layers are
# indistinguishable there and the later two mask the first. Only a
# TRANSITION separates them (wh-click-number-dictation.1.5).


class TestOverlayChangesBetweenTheWordAndTheEndMarker:

    @pytest.mark.asyncio
    async def test_overlay_paints_between_the_spoken_number_and_its_end_marker_dictates(self):
        """The badges were NOT showing when the number was spoken, so the
        number is dictation even though they are showing by the time the
        utterance ends. Only the hold-time overlay check can refuse this:
        the consume-time check sees the painted state and would allow the
        click. Realistic trigger: the user says "show numbers" and then
        "seventy" immediately, so the word lands while the overlay is
        still walking and the end marker lands after it paints."""
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word("70", start=True))
        proc.logic_controller.click_overlay_state.state = OverlayState.PAINTED
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]


# ============================================================================
# TESTS: terminal punctuation on a bare spoken number
# ============================================================================


class TestPunctuatedBareNumberStillClicks:
    """wh-number-badge-problems.1.4, boss ruling of 2026-09-02 (overridable).

    The spoken-click hold learned to ignore the terminal punctuation
    local STT appends to an utterance's last word. The bare-number hold
    had the same gap: "74." parses to None, so a lone spoken "74" with
    badges showing was typed instead of selecting badge 74. David's
    evidence log carries no punctuated finals because his provider does
    not punctuate, but the whisper and Google providers do.

    The held words stay exactly as spoken and the command text keeps its
    punctuation, because ClickCommandParser strips the same characters
    from its own final token (click_parser.py line 170) --
    test_the_punctuated_command_text_still_names_the_badge pins that.
    """

    @pytest.mark.asyncio
    # Explicit ids: a space inside a generated id would break the mutation
    # gate's expected-catcher parsing, which reads a test id up to its
    # first space.
    @pytest.mark.parametrize("words,command", [
        pytest.param(["74."], "click 74.", id="digits-period"),
        pytest.param(["74!"], "click 74!", id="digits-exclamation"),
        pytest.param(
            ["seventy", "four?"], "click seventy four?", id="words-question",
        ),
    ])
    async def test_a_punctuated_bare_number_clicks_that_badge(
        self, words, command,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word(words[0], start=True))
        for later in words[1:]:
            await proc.process_word_event(word(later))
        await proc.process_word_event(end_marker())

        assert parser.executed == [command]
        assert app.inserted_texts() == []

    def test_the_punctuated_command_text_still_names_the_badge(self):
        """The bare path sends its words as spoken, so the badge name
        comes out of ClickCommandParser's own terminal-punctuation strip.
        Without this, the three cases above would click nothing."""
        for command in ["click 74.", "click 74!", "click seventy four?"]:
            query = ClickCommandParser.parse_command(command)
            assert query is not None
            assert query.name in ("74", "seventy four")

    @pytest.mark.asyncio
    async def test_a_punctuated_bare_number_dictates_when_the_overlay_is_closed(
        self,
    ):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word("74.", start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["74."]


# ============================================================================
# TESTS: a per-word exception mid-number cannot click an unspoken number
# ============================================================================


class TestBareNumberSurvivesAMidNumberException:

    @pytest.mark.asyncio
    async def test_an_exception_mid_number_cannot_click_a_number_nobody_spoke(self):
        """The defer and its decision are one event. _processing_loop
        catches a per-word exception and continues, so a raise between the
        flush guard's defer and _execute_decision used to leave
        _bare_number_deferred_word standing into the NEXT event. The next
        word then overwrote the stale slot, the lost word left no trace,
        and the hold clicked a number the user never spoke
        (wh-click-number-dictation.1.6).

        Here the user says "twenty three" and the decision for "three"
        raises. Without the fix the following "five" extends the hold and
        the end marker clicks badge 25, for an utterance whose spoken
        words were "twenty three five"."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        original_decide = proc.router.decide
        raised: List[str] = []

        def decide_raising_once(word_event, *args, **kwargs):
            if word_event.word == "three" and not raised:
                raised.append(word_event.word)
                raise RuntimeError("catalog reload raced the decision")
            return original_decide(word_event, *args, **kwargs)

        proc.router.decide = decide_raising_once
        loop_task = asyncio.create_task(proc._processing_loop())
        try:
            for event in (
                word("twenty", start=True),
                word("three"),
                word("five"),
                end_marker(),
            ):
                await proc.word_queue.put(event)
            await asyncio.sleep(0.1)
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        assert raised == ["three"], "the decision never raised"
        assert parser.executed == []
        assert app.inserted_texts() == ["five"]
        assert proc._pending_bare_number_words is None
        assert proc._bare_number_deferred_word is None


class TestSpokenClickNumberWhilePainted:
    """wh-number-badge-problems.2: the recogniser keeps the word "click".

    "click 74" opens command buffering, and click_element needs the
    hotword, so the router defers the buffer as impossible and the end
    marker finalizes it as dictation (click-evidence.txt lines
    21777-21806). While badges show, that finalized buffer is the badge
    click David's C7 ruling names, exactly like the bare number whose
    verb the recogniser dropped. The MagicMock catalog here does not
    command-buffer "click", so each test sets the finalized-buffer state
    the router leaves at the end marker -- the same set-up
    test_retraction_replay_bare_number_clicks uses.
    """

    @staticmethod
    def _finalized_buffer(proc, words, utterance=None):
        proc.buffer = list(words)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc._words_this_utterance = list(
            words if utterance is None else utterance
        )

    # A payload the hold refuses is typed by the DICTATE branch BEFORE the
    # deferred end_utterance, which restores the user's clipboard. A
    # payload the hold wrongly accepts is typed by the consume's fallback
    # AFTER that end_utterance, so the ordering is what tells "refused"
    # from "held, then dictated anyway" -- the consume re-derives every
    # guard from the held text, so the typed text alone cannot.
    DICTATED_BEFORE_END = ["intelligent_insert_text", "end_utterance"]

    @staticmethod
    def _ipc_order(app):
        return [action["action"] for action in app.actions]

    @pytest.mark.parametrize("words", [
        ["click", "74"],
        ["click", "number", "74"],
        ["tap", "74"],
        ["click", "seventy", "four"],
        ["Click", "Numbers", "74"],
    ])
    async def test_click_plus_a_number_clicks_that_badge_at_the_end_marker(
        self, words,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(proc, words)
        await proc.process_word_event(end_marker())
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == []
        assert proc._pending_bare_number_words is None

    async def test_a_refreshing_overlay_still_accepts_the_spoken_click(self):
        proc, app, parser = make_processor(OverlayState.REFRESH_IN_FLIGHT)
        self._finalized_buffer(proc, ["click", "74"])
        await proc.process_word_event(end_marker())
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == []

    @pytest.mark.parametrize("words", [
        ["click", "0"],
        ["click", "1000"],
        ["click", "submit", "button"],
        ["click", "74", "please"],
        ["click", "number"],
        ["press", "74"],
        ["click", "0."],
        ["click", "1000."],
        ["click", "submit", "button."],
        ["click", "74", "please."],
        ["click", "number."],
    ])
    async def test_click_plus_anything_else_still_dictates(self, words):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(proc, words)
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == [" ".join(words)]
        assert self._ipc_order(app) == self.DICTATED_BEFORE_END

    async def test_the_spoken_click_dictates_when_the_overlay_is_closed(self):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        self._finalized_buffer(proc, ["click", "74"])
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74"]
        assert self._ipc_order(app) == self.DICTATED_BEFORE_END

    async def test_the_spoken_click_must_be_the_whole_utterance(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(
            proc, ["click", "74"], utterance=["hello", "click", "74"],
        )
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74"]
        assert self._ipc_order(app) == self.DICTATED_BEFORE_END

    async def test_a_bare_badge_number_must_be_the_whole_utterance(self):
        """wh-overlay-count-homophones.1.3: the bare half of that rule.

        The DICTATE-branch check that clicks a multi-word badge number
        shares _payload_is_the_whole_utterance with the spoken-click
        hold above. Without the share it would click badge 112 here --
        "one twelve" parses and badges are showing -- and the user's
        "hello" would vanish with the rest of the sentence.
        """
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(
            proc, ["one", "twelve"], utterance=["hello", "one", "twelve"],
        )
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == ["one twelve"]
        assert self._ipc_order(app) == self.DICTATED_BEFORE_END

    # wh-number-badge-problems.1.4: local STT appends terminal punctuation
    # to the last word of an utterance ("click 74."), which is why
    # click_parser strips _TRAILING_PUNCT from its final token and why the
    # trailing-command hold re-matches a normalized form
    # (wh-whole-utterance-command-matching.3.1.7). The spoken-click hold
    # parses the same punctuated token, so it needs the same allowance --
    # in the hold AND in the consume, which parses the held text again.

    @pytest.mark.parametrize("words", [
        ["click", "74."],
        ["click", "number", "74!"],
        ["tap", "seventy", "four?"],
        ["click", "seventy-four;"],
        ["click", "74.."],
    ])
    async def test_terminal_punctuation_on_the_number_still_clicks_the_badge(
        self, words,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(proc, words)
        await proc.process_word_event(end_marker())
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == []

    async def test_a_punctuated_spoken_click_dictates_with_its_punctuation(self):
        proc, app, parser = make_processor(OverlayState.CLOSED)
        self._finalized_buffer(proc, ["click", "74."])
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74."]
        assert self._ipc_order(app) == self.DICTATED_BEFORE_END

    async def test_a_punctuated_click_that_does_not_match_keeps_its_punctuation(
        self,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        parser.result = False
        self._finalized_buffer(proc, ["click", "74."])
        await proc.process_word_event(end_marker())
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == ["click 74."]

    # wh-number-badge-problems.1.3: every dictation fallback of the spoken
    # click must type what was spoken, in the one string the pre-fix
    # finalization typed. The hold is armed here the way the buffer timer
    # arms it (a finalized "click 74" with no end marker yet), so a later
    # word, the next utterance, the overlay closing, or a click pattern
    # that does not match can each prove the fallback content.

    async def test_a_later_word_flushes_the_spoken_click_as_it_was_spoken(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc._words_this_utterance = ["click", "74"]
        assert proc._maybe_hold_bare_number("click 74") is True
        await proc.process_word_event(word("please"))
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74", "please"]

    async def test_the_next_utterance_flushes_a_spoken_click_that_lost_its_end_marker(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc._words_this_utterance = ["click", "74"]
        assert proc._maybe_hold_bare_number("click 74") is True
        await proc.process_word_event(word("hello", start=True))
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74", "hello"]

    async def test_the_overlay_closing_between_hold_and_consume_types_the_spoken_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        self._finalized_buffer(proc, ["click", "74"])
        original_send = proc._send_pending_utterance_end

        async def send_and_close_the_overlay():
            proc.logic_controller.click_overlay_state.state = OverlayState.CLOSED
            await original_send()

        proc._send_pending_utterance_end = send_and_close_the_overlay
        await proc.process_word_event(end_marker())
        assert parser.executed == []
        assert app.inserted_texts() == ["click 74"]

    async def test_a_click_pattern_that_does_not_match_types_the_spoken_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        parser.result = False
        self._finalized_buffer(proc, ["click", "number", "74"])
        await proc.process_word_event(end_marker())
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == ["click number 74"]


# ============================================================================
# TESTS: a held spoken click whose utterance-end marker never arrives
# ============================================================================

class TestASpokenClickWhoseEndMarkerNeverArrives:
    """wh-number-badge-problems.1.6 (codex round 6).

    The buffer timer finalizes "click 74" as dictation and the hold takes
    it. The utterance-end marker that would consume it is built from the
    terminal FINAL message in integrations/websocket_manager.py, so a
    dropped message takes the marker with it, and that module's idle
    watchdog (_fire_idle_watchdog) only writes the GUI activity state. A
    user who then stops speaking sends no further event, so before this
    fix the words sat in the slot for good: no click, and no text either.
    The hold now arms a release deadline and dictates the words when it
    fires -- the same shape wh-trailing-question-mark-words.2.1 gave the
    held replacement prefix.

    The bare-number hold waits the same way. Codex recorded that as a
    separate, pre-existing question outside this branch, so the arming
    here is deliberately limited to the spoken-click form; the last test
    pins that limit.
    """

    @staticmethod
    def _finalized_buffer(proc, words):
        proc.buffer = list(words)
        proc.mode = ProcessingMode.COMMAND_BUFFERING
        proc._words_this_utterance = list(words)

    @staticmethod
    def _buffer_timer(proc):
        """The sentinel the buffer timer would enqueue right now.

        The tests set the finalized-buffer state by hand rather than
        letting a command pattern buffer it (the MagicMock catalog does
        not buffer "click"), so this stands in for that timer's own
        sentinel. Every LATER sentinel in these tests is the real one the
        processor's timer enqueues.
        """
        return WordEvent.timeout_finalize(proc.timeout_token)

    async def test_a_held_spoken_click_arms_a_release_deadline(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.command_timeout_ms = 10
        self._finalized_buffer(proc, ["click", "74"])
        await proc.process_word_event(self._buffer_timer(proc))

        assert proc._pending_bare_number_words == ["click 74"]
        sentinel = await asyncio.wait_for(proc.word_queue.get(), timeout=2.0)
        assert sentinel.is_timeout_finalize_marker is True
        # A token the hold's own _reset_to_idle already made stale would
        # be discarded on arrival, which is the whole failure this guards.
        assert sentinel.timeout_token == proc.timeout_token

    async def test_the_release_deadline_dictates_the_held_spoken_click(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.command_timeout_ms = 10
        self._finalized_buffer(proc, ["click", "74"])
        await proc.process_word_event(self._buffer_timer(proc))

        sentinel = await asyncio.wait_for(proc.word_queue.get(), timeout=2.0)
        await proc.process_word_event(sentinel)

        assert parser.executed == []
        assert app.inserted_texts() == ["click 74"]
        assert proc._pending_bare_number_words is None

    async def test_the_end_marker_still_clicks_and_the_deadline_adds_nothing(
        self,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.command_timeout_ms = 10
        self._finalized_buffer(proc, ["click", "74"])
        await proc.process_word_event(self._buffer_timer(proc))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == []

        # The armed deadline still fires. Its sentinel must change
        # nothing: the consume emptied the slot and bumped the token.
        sentinel = await asyncio.wait_for(proc.word_queue.get(), timeout=2.0)
        await proc.process_word_event(sentinel)
        assert parser.executed == ["click 74"]
        assert app.inserted_texts() == []

    async def test_a_bare_number_hold_arms_no_release_deadline(self):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("74", start=True))

        assert proc._pending_bare_number_words == ["74"]
        assert proc.timeout_task is None


class TestTheHoldsAcceptTheSTTHomophones:
    """wh-overlay-count-homophones (David's decision 2026-09-03).

    The engine returns "to" and "too" for the spoken "two" and "for" for
    "four". Both holds parsed with the default aliases=False, so a badge
    number spoken as its homophone never opened a hold and the word was
    typed instead. The command counts already accept the homophones.

    What these cases do and do not prove (wh-overlay-count-homophones.1.1):
    make_processor uses a MagicMock catalog, so the router answers
    DICTATE for every word and each hold is exercised directly. That is
    the right shape for the holds' own contract, and it is NOT a model of
    the shipped router. With the real catalog a lone "to", "too" or "for"
    is a COMMAND -- the grid patterns match the whole utterance -- so it
    buffers and never reaches the bare hold; in the shipped app those
    words take the grid actions' badge fallback instead, which
    tests/test_grid_number_badge_click.py covers with the real catalog.
    The spoken-click cases ("click for") and the multi-word extension
    ("twenty for") DO run through the holds in the shipped app, because
    neither utterance matches a grid pattern.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spoken", ["to", "too", "for"])
    async def test_a_bare_homophone_opens_the_hold_and_clicks(self, spoken):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word(spoken, start=True))
        await proc.process_word_event(end_marker())

        # The bare hold keeps the spoken words; main.py resolves the
        # homophone to a badge when it parses the command text.
        assert parser.executed == ["click %s" % spoken]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    async def test_a_homophone_extends_a_multiword_hold(self):
        """"twenty for" is 24, so the second word must keep the hold open."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        await proc.process_word_event(word("twenty", start=True))
        await proc.process_word_event(word("for"))
        await proc.process_word_event(end_marker())

        assert parser.executed == ["click twenty for"]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    # Explicit ids, hyphens and no spaces: the mutation gate matches a
    # collected id by reading up to the first space, so a generated id
    # such as "words0-click 4" would truncate and match nothing.
    @pytest.mark.parametrize(("words", "command"), [
        pytest.param(["click", "for"], "click 4", id="click-for"),
        pytest.param(["click", "to"], "click 2", id="click-to"),
        pytest.param(["click", "too"], "click 2", id="click-too"),
        pytest.param(["click", "twenty", "for"], "click 24",
                     id="click-twenty-for"),
        pytest.param(["click", "number", "for"], "click 4",
                     id="click-number-for"),
    ])
    async def test_a_spoken_click_homophone_clicks_that_badge(
        self, words, command,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        TestSpokenClickNumberWhilePainted._finalized_buffer(proc, words)
        await proc.process_word_event(end_marker())

        assert parser.executed == [command]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    # wh-overlay-count-homophones.1.8. The cases above close the
    # utterance with an end marker, which the DICTATE branch's finalized
    # payload check (.1.3) also serves: it re-parses the same verb form
    # with the aliases, so the badge is clicked even when this hold
    # declines. That covers the fixture twice and hides a hold that
    # stopped opening. These cases close with the no-end-marker buffer
    # timer instead, which only the hold reaches -- the .1.4 confirmed
    # end gate keeps the .1.3 check away from a timer finalization -- so
    # a hold that declines here dictates the words and clicks nothing.
    # Explicit hyphenated ids for the same reason as the block above.
    @pytest.mark.parametrize(("words", "command"), [
        pytest.param(["click", "for"], "click 4", id="timer-click-for"),
        pytest.param(["click", "to"], "click 2", id="timer-click-to"),
        pytest.param(["click", "too"], "click 2", id="timer-click-too"),
        pytest.param(["click", "twenty", "for"], "click 24",
                     id="timer-click-twenty-for"),
        pytest.param(["click", "number", "for"], "click 4",
                     id="timer-click-number-for"),
    ])
    async def test_a_timer_held_click_homophone_clicks_that_badge(
        self, words, command,
    ):
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.command_timeout_ms = 10
        timed = TestASpokenClickWhoseEndMarkerNeverArrives
        timed._finalized_buffer(proc, words)
        await proc.process_word_event(timed._buffer_timer(proc))

        # The hold, and only the hold, can keep these words here.
        assert proc._pending_bare_number_words == [" ".join(words)]
        assert app.inserted_texts() == []

        await proc.process_word_event(end_marker())

        assert parser.executed == [command]
        assert app.inserted_texts() == []

        # The release deadline the hold armed still fires; the consume
        # already emptied the slot, so its sentinel changes nothing.
        sentinel = await asyncio.wait_for(proc.word_queue.get(), timeout=2.0)
        await proc.process_word_event(sentinel)
        assert parser.executed == [command]
        assert app.inserted_texts() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spoken", ["to", "too", "for"])
    async def test_a_bare_homophone_with_no_badges_still_types(self, spoken):
        """The closed-overlay fallback the change must not disturb.

        With no badges showing the word is ordinary speech and must be
        typed, which is what keeps "for" usable in dictation.
        """
        proc, app, parser = make_processor(OverlayState.CLOSED)
        await proc.process_word_event(word(spoken, start=True))
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == [spoken]
