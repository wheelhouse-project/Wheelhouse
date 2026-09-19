"""Regression guard for the retraction-replay live sequence (item 19(b)).

wh-whole-utterance-command-matching.3, boss ruling 2026-08-30 on David's
item 19(b) (QUESTIONS-2026-08-29.md): the suspected leak -- "a bare
number armed by the last word of a retraction replay carries over into
the next utterance" -- was DISPROVED by probe at 48aceeed. The sole
producer of retraction markers (websocket_manager._handle_mode3_retract)
unconditionally queues an utterance-end marker right behind every
retraction marker, and the end-marker branch has no stale-id filtering,
so that marker always consumes the replay-armed number: a click while
badges show (the UTT-30 fix), dictation otherwise. 19(b) is therefore a
regression-guard test only, no code change -- the exact mirror of the
19(a) resolution.

This file pins the LIVE sequence end to end, across the utterance
boundary that the leak claim named. The absorbed Stage-2 suite
(test_speech_processor_bare_number.py) already pins the click itself;
what it does not cover is the NEXT utterance staying clean, which is
the half the leak claim was about.
"""
import pytest

from services.wheelhouse.tests.test_speech_processor_bare_number import (
    OverlayState,
    end_marker,
    make_processor,
    retraction_marker,
    word,
)
from speech.domain import ProcessingMode


class TestRetractionReplayLiveSequence:

    @pytest.mark.asyncio
    async def test_replay_armed_number_clicks_and_the_next_utterance_stays_clean(
        self,
    ):
        """Live shape: STABLE 'click' command-buffered, FINAL rewrote
        the utterance to '70', retraction marker + the end marker its
        producer always queues, then a second utterance. The replayed
        bare number must click badge 70 in ITS OWN utterance, and
        nothing of it may reach utterance 2."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.buffer = ["click"]
        proc.mode = ProcessingMode.COMMAND_BUFFERING

        await proc.process_word_event(retraction_marker("70"))
        await proc.process_word_event(end_marker())

        # The consume happened at the retraction's own end marker,
        # BEFORE any utterance-2 event exists.
        assert parser.executed == ["click 70"]
        assert proc._held_tail is None

        await proc.process_word_event(word("hello", start=True, uid=2))
        await proc.process_word_event(end_marker(uid=2))

        assert parser.executed == ["click 70"]
        assert app.inserted_texts() == ["hello"]
        assert proc._held_tail is None

    @pytest.mark.asyncio
    async def test_replay_armed_number_dictates_in_its_own_utterance_when_badges_vanish(
        self,
    ):
        """Same live shape, but the overlay closes between the replay
        arming the hold and the queued end marker consuming it: the
        number dictates -- still inside its own utterance, never
        utterance 2."""
        proc, app, parser = make_processor(OverlayState.PAINTED)
        proc.buffer = ["click"]
        proc.mode = ProcessingMode.COMMAND_BUFFERING

        await proc.process_word_event(retraction_marker("70"))
        # Badges vanish before the queued end marker arrives.
        proc.logic_controller.click_overlay_state.state = OverlayState.CLOSED
        await proc.process_word_event(end_marker())

        assert parser.executed == []
        assert app.inserted_texts() == ["70"]

        await proc.process_word_event(word("hello", start=True, uid=2))
        await proc.process_word_event(end_marker(uid=2))

        assert app.inserted_texts() == ["70", "hello"]
        assert proc._held_tail is None
