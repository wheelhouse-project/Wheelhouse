"""Bare-number and spoken-click requirements (area S3), at the boundary.

Bead: wh-full-refactor.4.1 (stage A of place 2). Each test pins one row BN-n of
the table "Bare-number holds" in docs/design/overlay-stack-requirements.md and
is named after its row.

Every test drives a real ``SpeechProcessor`` inside the Logic process built by
tests/overlay_boundary_harness.py, through the word events and the utterance
end that the STT path gives it (``OverlayBoundary.say``, or ``WordEvent`` values
given to the public ``process_word_event``). Some tests also change the click
command through user_patterns.toml, the file the Pattern Manager writes in the
user data folder. A test asserts only on what leaves
the processor: the click_snapshot_item and click_element commands for the Input
process, and the words typed through intelligent_insert_text. Nothing here names
a private member of a production class.

TYPED WORDS. The harness records but does not answer intelligent_insert_text
requests, and a typed word waits for its reply. ``typing_answered`` answers
each one with an ok reply, as the Input process does.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from speech.word_event import WordEvent
from tests.overlay_boundary_harness import overlay_boundary, settle

pytestmark = pytest.mark.journey

_BADGES = (1, 2, 3, 4, 23, 24, 74, 75, 112)
_TYPED = frozenset({"intelligent_insert_text"})


async def _answer_typing(b) -> None:
    seen = set()
    while True:
        for frame in list(b.frames):
            if frame.action in _TYPED and frame.index not in seen:
                seen.add(frame.index)
                if frame.request_id is not None:
                    b.reply(frame, {"status": "ok"})
        await asyncio.sleep(0.005)


async def _answer_retract(b) -> None:
    """Answer each retract request as the Input process does when nothing of
    the utterance was typed (the words were held in a command buffer)."""

    seen = set()
    while True:
        for frame in list(b.frames):
            if frame.action == "retract" and frame.index not in seen:
                seen.add(frame.index)
                if frame.request_id is not None:
                    b.reply(frame, {
                        "status": "not_retracted", "reason": "nothing_to_retract",
                    })
        await asyncio.sleep(0.005)


@asynccontextmanager
async def typing_answered(b):
    tasks = [
        asyncio.ensure_future(_answer_typing(b)),
        asyncio.ensure_future(_answer_retract(b)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _typed(b, mark) -> list:
    return [f.params.get("insertion_string") for f in b.frames_since(mark, _TYPED)]


def _badges_clicked(b, mark) -> list:
    return [
        f.params.get("item_id") for f in b.frames_since(mark, {"click_snapshot_item"})
    ]


def _other_clicks(b, mark) -> list:
    return [f.action for f in b.frames_since(mark, {"click_element"})]


def _item(number: int) -> str:
    return f"snap-1-item-{number}"


async def _painted(b):
    return await b.show_numbers_painted("snap-1", _BADGES)


def snapshot_item_clicked(snapshot_id: str, display_number: int) -> dict:
    """A GUI badge click, through ``SnapshotItemClickedEvent``."""

    from services.wheelhouse.shared.snapshot_item_clicked import (
        SnapshotItemClickedEvent,
    )

    return SnapshotItemClickedEvent(
        snapshot_id=snapshot_id, display_number=display_number,
    ).to_dict()


def _event(word, *, start=False, end=False, utterance=9001, **flags) -> WordEvent:
    return WordEvent(
        word=word, start_of_utterance=start, end_of_utterance=end,
        utterance_id=utterance, **flags,
    )


async def _end_marker(b, utterance=9001) -> None:
    await b.speech_processor.process_word_event(
        _event("", end=True, utterance=utterance, is_utterance_end_marker=True)
    )
    await settle()


# ---------------------------------------------------------------------------
# What opens a hold, and what the hold does at the utterance end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, badge",
    [("74", 74), ("74.", 74), ("seventy four", 74), ("twenty three", 23),
     ("twenty for", 24), ("one twelve", 112), ("one hundred twelve", 112)],
)
async def test_bn_1_number_that_opens_the_utterance_clicks_its_badge(
    tmp_path, spoken, badge,
):
    """BN-1: a number 1..999 that is the whole utterance clicks its badge.

    A trailing ".", ",", "!", "?", ";" or ":" on the last word is ignored.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(badge)]
        assert _typed(b, mark) == []


@pytest.mark.parametrize("spoken", ["number seventy four", "numbers 74"])
async def test_bn_2_number_filler_before_the_number_clicks_its_badge(
    tmp_path, spoken,
):
    """BN-2: "number" or "numbers" before the number still clicks the badge."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(74)]
        assert _typed(b, mark) == []


@pytest.mark.parametrize("spoken", ["seventy four", "74"])
async def test_bn_3_number_is_typed_when_no_badges_are_showing(tmp_path, spoken):
    """BN-3: with the overlay closed a spoken number is typed, not clicked."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await b.flush()
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert "".join(_typed(b, mark)).replace(" ", "") == spoken.replace(" ", "")
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


@pytest.mark.parametrize("spoken", ["1000", "0"])
async def test_bn_4_number_outside_1_to_999_is_typed(tmp_path, spoken):
    """BN-4: 0 and 1000 are not badge numbers; the words are typed."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == [spoken]
        assert _badges_clicked(b, mark) == []


async def test_bn_5_number_in_the_middle_of_an_utterance_is_typed(tmp_path):
    """BN-5: a number that does not open the utterance never clicks."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say("hello 74")
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["hello", "74"]
        assert _badges_clicked(b, mark) == []


async def test_bn_6_word_that_does_not_extend_the_number_types_both(tmp_path):
    """BN-6: "74" then a word that is no number part types "74" then the word."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say("74 pack")
        await b.quiet(mark, 0.1)
        typed = _typed(b, mark)
        assert " ".join(typed).split() == ["74", "pack"]
        assert _badges_clicked(b, mark) == []


@pytest.mark.parametrize(
    "spoken, typed",
    [pytest.param("hello number 74", ["hello", "number 74"], id="number-filler"),
     pytest.param("hello one twelve", ["hello", "one", "twelve"], id="unit-word")],
)
async def test_bn_7_number_with_other_words_before_it_is_not_a_click(
    tmp_path, spoken, typed,
):
    """BN-7: a number after other words of the utterance types; nothing clicks.

    "hello number 74": "number" after "hello" opens a mid-utterance buffer
    for the replacement "number sign"; the last word finalizes it as the
    payload "number 74", which is not the whole utterance, so it types as
    one text. "hello one twelve": each word types as it arrives.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == typed
        assert _badges_clicked(b, mark) == []


async def test_bn_8_held_number_is_typed_when_the_badges_vanish(tmp_path):
    """BN-8: badges gone before the utterance end: the held number is typed."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(
            _event("74", start=True, end=True)
        )
        await settle()
        # The user clicks badge 2 on screen: the click closes the overlay.
        b.send_gui(snapshot_item_clicked("snap-1", 2))
        await b.settle_gui()
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["74"]
        assert _badges_clicked(b, mark) == [_item(2)]


async def test_bn_9_new_utterance_types_the_held_number_first(tmp_path):
    """BN-9: a new utterance start types the held number, then its own words."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(
            _event("74", start=True, utterance=9001)
        )
        await settle()
        await b.speech_processor.process_word_event(
            _event("hello", start=True, end=True, utterance=9002)
        )
        await _end_marker(b, 9002)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["74", "hello"]
        assert _badges_clicked(b, mark) == []


async def test_bn_9b_buffer_left_open_is_typed_before_the_new_utterance(tmp_path):
    """BN-9b: "number" is still buffered when a new utterance starts.

    The end marker of utterance 9001 never arrives. The new utterance closes
    the open buffer first: "number" is typed, then the new words.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(
            _event("number", start=True, utterance=9001)
        )
        await settle()
        await b.speech_processor.process_word_event(
            _event("hello", start=True, end=True, utterance=9002)
        )
        await _end_marker(b, 9002)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["number", "hello"]
        assert _badges_clicked(b, mark) == []


async def test_bn_10_dropped_hold_types_nothing_and_clicks_nothing(tmp_path):
    """BN-10: drop_bare_number_hold discards the held number silently."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(
            _event("74", start=True, end=True)
        )
        await settle()
        b.speech_processor.drop_bare_number_hold("test")
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == []
        assert _badges_clicked(b, mark) == []


# ---------------------------------------------------------------------------
# Spoken "click N" and the grid's number words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken, badge",
    [("click 74", 74), ("tap 74", 74), ("click 74.", 74),
     ("tap seventy four", 74), ("click number 74", 74), ("click for", 4)],
)
async def test_bn_11_spoken_click_with_a_number_clicks_the_badge(
    tmp_path, spoken, badge,
):
    """BN-11: click or tap plus a number 1..999 clicks that badge."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(badge)]
        assert _typed(b, mark) == []


@pytest.mark.parametrize("spoken, badge", [("two", 2), ("four", 4)])
async def test_bn_12_grid_number_word_clicks_a_badge_when_numbers_show(
    tmp_path, spoken, badge,
):
    """BN-12: a number 1..9 that the grid patterns claim still clicks its badge."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(badge)]
        assert _typed(b, mark) == []


async def test_bn_13_try_badge_click_answers_for_the_grid_fallback(tmp_path):
    """BN-13: the public badge click answers True when it clicks, else False.

    False cases checked here: no badges show, and the words name no number.
    The third False case, the click pattern does not execute, is test_bn_13b.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await b.flush()
        mark = b.mark()
        assert await b.speech_processor.try_bare_number_badge_click("74") is False
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == []
        await _painted(b)
        mark = b.mark()
        assert await b.speech_processor.try_bare_number_badge_click(
            "seventy four"
        ) is True
        assert await b.speech_processor.try_bare_number_badge_click(
            "pack"
        ) is False
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(74)]


# ---------------------------------------------------------------------------
# User pattern overrides of the click command
# ---------------------------------------------------------------------------
#
# A user changes a shipped command in the Pattern Manager, which writes the
# entry to user_patterns.toml in the user data folder; an entry with the
# shipped doc_id replaces the shipped pattern (PatternCatalog). The harness
# points the user data folder at tmp_path / "user-data" (overlay_boundary),
# so a test writes the file there before it starts the boundary.

# The shipped click_element entry of speech/config/patterns.toml, with the
# safety word required: "click 74" said without "x-ray" no longer matches it.
_CLICK_NEEDS_SAFETY_WORD = r"""
[[pattern]]
pattern = '''^(?:click|clicks|tap)\s+(.+)$'''
doc_id = "click-element"
requires_hotword = true
actions = [
    { function = "click_element", params = ["g1"], awaits_done = true }
]
"""

# The same entry with a different trigger: "click 74" matches nothing.
_CLICK_SAYS_PRESS_ITEM = r"""
[[pattern]]
pattern = '''^press item\s+(.+)$'''
doc_id = "click-element"
actions = [
    { function = "click_element", params = ["g1"], awaits_done = true }
]
"""


def _user_patterns(tmp_path, text: str) -> None:
    folder = tmp_path / "user-data"
    folder.mkdir(exist_ok=True)
    (folder / "user_patterns.toml").write_text(text, encoding="utf-8")


async def test_bn_13b_badge_click_answers_false_when_the_click_pattern_misses(
    tmp_path,
):
    """BN-13b: the third False case: the click command matches no pattern."""

    _user_patterns(tmp_path, _CLICK_SAYS_PRESS_ITEM)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        assert await b.speech_processor.try_bare_number_badge_click("74") is False
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


@pytest.mark.parametrize(
    "spoken, badge",
    [pytest.param("click 74", 74, id="digits"),
     pytest.param("click 74.", 74, id="digits-period"),
     pytest.param("tap seventy four", 74, id="tap-words"),
     pytest.param("click number 74", 74, id="number-filler")],
)
async def test_bn_15_spoken_click_hold_clicks_at_the_utterance_end(
    tmp_path, spoken, badge,
):
    """BN-15: the click pattern needs the safety word, and none was said.

    say() sets the end-of-utterance flag on the last word, so the router
    finalizes "click 74" as dictation at that word; the spoken-click hold
    keeps it, and the end marker clicks the badge. Nothing is typed.
    """

    _user_patterns(tmp_path, _CLICK_NEEDS_SAFETY_WORD)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.say(spoken)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(badge)]
        assert _typed(b, mark) == []
        assert _other_clicks(b, mark) == []


async def test_bn_15b_held_spoken_click_is_typed_when_no_end_marker_comes(
    tmp_path,
):
    """BN-15b: the last word ends the utterance, but no end marker arrives.

    The release deadline (command_timeout_ms, 700 ms in the example config)
    types the held words as one text. Nothing is clicked.
    """

    _user_patterns(tmp_path, _CLICK_NEEDS_SAFETY_WORD)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        await b.speech_processor.start()
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("click", start=True))
        await settle()
        await b.speech_processor.process_word_event(_event("74", end=True))
        await settle()
        await b.flush()
        assert _typed(b, mark) == []
        # Real time in this test: 3 steps of 0.03 s plus the settle waits of
        # advance(), the quiet window and the paint setup: under 0.6 s.
        for _ in range(3):
            await b.advance(0.5)
            await asyncio.sleep(0.03)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["click 74"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


async def test_bn_15c_spoken_click_is_typed_when_no_badges_show(tmp_path):
    """BN-15c: the same override, the overlay closed: "click 74" is typed whole."""

    _user_patterns(tmp_path, _CLICK_NEEDS_SAFETY_WORD)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await b.flush()
        mark = b.mark()
        await b.say("click 74")
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["click 74"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


async def test_bn_15e_held_spoken_click_is_typed_before_the_next_utterance(
    tmp_path,
):
    """BN-15e: as BN-15b, but a new utterance starts before an end marker.

    The hold opens at "74" (end-of-utterance flag, no end marker). The new
    utterance types the held words as one text, then its own words.
    Nothing is clicked (wh-number-badge-problems.1.3).
    """

    _user_patterns(tmp_path, _CLICK_NEEDS_SAFETY_WORD)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("click", start=True))
        await settle()
        await b.speech_processor.process_word_event(_event("74", end=True))
        await settle()
        await b.speech_processor.process_word_event(
            _event("hello", start=True, end=True, utterance=9002)
        )
        await _end_marker(b, 9002)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["click 74", "hello"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


async def test_bn_15f_held_spoken_click_is_typed_before_a_later_word(tmp_path):
    """BN-15f: as BN-15b, but one more word of the same utterance arrives.

    The hold opens at "74" (end-of-utterance flag, no end marker). The later
    word proves that "click 74" was not the whole utterance: the held words
    are typed as one text, then the later word. Nothing is clicked
    (wh-number-badge-problems.1.3).
    """

    _user_patterns(tmp_path, _CLICK_NEEDS_SAFETY_WORD)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("click", start=True))
        await settle()
        await b.speech_processor.process_word_event(_event("74", end=True))
        await settle()
        await b.speech_processor.process_word_event(_event("please"))
        await settle()
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["click 74", "please"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


# ---------------------------------------------------------------------------
# Markers from the STT path, shutdown, and the refresh state
# ---------------------------------------------------------------------------


async def test_bn_3b_number_is_a_click_while_a_refresh_walk_is_in_flight(tmp_path):
    """BN-3b: a refresh walk is in flight, so badges still show: the number clicks."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        await b.say("show numbers")
        await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await b.say("74")
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(74)]
        assert _typed(b, mark) == []


async def test_bn_17a_retraction_replay_of_a_number_clicks_its_badge(tmp_path):
    """BN-17a: "click" is command-buffered, the final is rewritten to "74"."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("click", start=True))
        await settle()
        await b.speech_processor.process_word_event(
            _event("", utterance=9001, is_retraction_marker=True,
                   retraction_full_text="74")
        )
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(74)]
        assert _typed(b, mark) == []


async def test_bn_17b_retraction_marker_types_a_held_number(tmp_path):
    """BN-17b: a retraction marker proves the held number was not the whole text."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("74", start=True))
        await settle()
        await b.speech_processor.process_word_event(
            _event("", utterance=9001, is_retraction_marker=True,
                   retraction_full_text="hello")
        )
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["74"]
        assert _badges_clicked(b, mark) == []


async def test_bn_17c_lifecycle_reset_marker_clicks_a_held_number(tmp_path):
    """BN-17c: the reset marker is a confirmed utterance end for a held number."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("74", start=True))
        await settle()
        await b.speech_processor.process_word_event(
            _event("", end=True, is_lifecycle_reset_marker=True)
        )
        await b.quiet(mark, 0.1)
        assert _badges_clicked(b, mark) == [_item(74)]
        assert _typed(b, mark) == []


async def test_bn_17d_stop_drops_a_held_number_silently(tmp_path):
    """BN-17d: shutdown neither types nor clicks a held number."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("74", start=True))
        await settle()
        await b.speech_processor.stop()
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == []
        assert _badges_clicked(b, mark) == []


# ---------------------------------------------------------------------------
# Buffer timer, "number" alone, and a click that blocks a later retraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "painted, end_marker",
    [pytest.param(False, True, id="closed-end-marker"),
     pytest.param(True, True, id="painted-end-marker"),
     pytest.param(False, False, id="closed-timer-only"),
     pytest.param(True, False, id="painted-timer-only")],
)
async def test_bn_14_number_alone_is_typed_when_the_buffer_timer_ends(
    tmp_path, painted, end_marker,
):
    """BN-14: "number" alone is typed as text, with or without badges.

    The word is held as the start of "number sign" and typed when the
    release deadline passes. With the end marker the hold starts at the
    marker; with the timer only, it starts when the command buffer timer
    ends. Either way nothing is typed before time passes.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        if painted:
            await _painted(b)
        await b.flush()
        await b.speech_processor.start()
        mark = b.mark()
        if end_marker:
            await b.say("number")
        else:
            await b.speech_processor.process_word_event(
                _event("number", start=True)
            )
            await settle()
        await b.flush()
        assert _typed(b, mark) == []
        # Real time: 4 steps of 0.03 s plus the settle waits of advance()
        # and the quiet window: under 0.6 s. The clock moves 2.0 s, more
        # than command_timeout_ms plus replacement_timeout_ms (1.4 s).
        for _ in range(4):
            await b.advance(0.5)
            await asyncio.sleep(0.03)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["number"]
        assert _badges_clicked(b, mark) == []


@pytest.mark.parametrize("painted", [False, True])
async def test_bn_14b_numbers_alone_is_typed_at_the_end_marker(tmp_path, painted):
    """BN-14b: "numbers" alone is typed at the end marker; nothing clicks.

    With badges showing, the finalized word opens the bare-number hold on the
    filler, and its consume types it because the word names no number.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        if painted:
            await _painted(b)
        await b.flush()
        mark = b.mark()
        await b.say("numbers")
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == ["numbers"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []


@pytest.mark.parametrize(
    "words, typed",
    [(["number", "seventy", "four"], "number seventy four"),
     (["one", "twelve"], "one twelve")],
)
async def test_bn_16_number_payload_finalized_by_the_timer_is_typed(
    tmp_path, words, typed,
):
    """BN-16: no end marker arrives, the buffer timer ends the utterance."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        await b.speech_processor.start()
        mark = b.mark()
        for index, word in enumerate(words):
            await b.speech_processor.process_word_event(
                _event(word, start=(index == 0))
            )
            await settle()
        # The buffer timer and the finalize step need several loop turns.
        # Real time in this test: 6 steps of 0.05 s plus the two 0.02 s
        # settle waits of advance(), the 0.1 s quiet window and its settle,
        # one settle per word and the paint setup: about 0.6 s in total.
        for _ in range(6):
            await b.advance(5.0)
            await asyncio.sleep(0.05)
        await b.quiet(mark, 0.1)
        assert _typed(b, mark) == [typed]
        assert _badges_clicked(b, mark) == []


async def test_bn_18_a_number_click_blocks_a_later_retraction(tmp_path):
    """BN-18: after the click, a retraction in the same utterance does nothing."""

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("74", start=True))
        await settle()
        await _end_marker(b)
        await b.speech_processor.process_word_event(
            _event("", utterance=9001, is_retraction_marker=True,
                   retraction_full_text="75")
        )
        await b.quiet(mark, 0.2)
        assert _badges_clicked(b, mark) == [_item(74)]
        assert b.frames_since(mark, {"retract"}) == []
        assert _typed(b, mark) == []


# ---------------------------------------------------------------------------
# Typed words leave Logic before end_utterance (wh-full-refactor.4.2)
# ---------------------------------------------------------------------------
#
# end_utterance tells the Input process that the utterance is over: Input
# then restores the clipboard and resets the paste count that a retraction
# reads. A word that Logic types for the utterance must reach Input before
# that frame. The tests below read the order of the two frames. A hold that
# the end marker's own finalization opens types its words when the end
# marker consumes it, and that is the order they check.

_TYPED_AND_END = frozenset({"intelligent_insert_text", "end_utterance"})


def _typed_and_end(b, mark) -> list:
    return [
        f"typed:{f.params.get('insertion_string')}"
        if f.action == "intelligent_insert_text" else f.action
        for f in b.frames_since(mark, _TYPED_AND_END)
    ]


@pytest.mark.parametrize(
    "painted", [pytest.param(False, id="closed"), pytest.param(True, id="painted")],
)
async def test_bn_14b_typed_word_leaves_before_end_utterance(tmp_path, painted):
    """BN-14b: "numbers" alone is typed before end_utterance, badges or not.

    With badges showing, the end marker's finalization opens the bare-number
    hold on the filler, and the consume types the word because it names no
    number. With the overlay closed the word types without a hold. Both give
    the same order.
    """

    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        if painted:
            await _painted(b)
        await b.flush()
        mark = b.mark()
        await b.say("numbers")
        await b.quiet(mark, 0.1)
        assert _typed_and_end(b, mark) == ["typed:numbers", "end_utterance"]
        assert _badges_clicked(b, mark) == []


# The shipped click_element entry with a rest that cannot hold a digit:
# "click 74" matches nothing, so the spoken-click hold's consume runs no click
# and types the words as they were spoken.
_CLICK_SKIPS_NUMBERS = r"""
[[pattern]]
pattern = '''^(?:click|clicks|tap)\s+([^\d\s]\D*)$'''
doc_id = "click-element"
actions = [
    { function = "click_element", params = ["g1"], awaits_done = true }
]
"""


async def test_bn_15_held_click_typed_as_spoken_leaves_before_end_utterance(
    tmp_path,
):
    """BN-15: the end marker finalizes "click 74", and no click pattern runs it.

    No word carries the end-of-utterance flag, so the end marker finalizes the
    buffer and the spoken-click hold opens there. The consume finds no click
    pattern for "click 74" and types the words, before end_utterance.
    """

    _user_patterns(tmp_path, _CLICK_SKIPS_NUMBERS)
    async with overlay_boundary(tmp_path) as b, typing_answered(b):
        await _painted(b)
        mark = b.mark()
        await b.speech_processor.process_word_event(_event("click", start=True))
        await settle()
        await b.speech_processor.process_word_event(_event("74"))
        await settle()
        await _end_marker(b)
        await b.quiet(mark, 0.1)
        assert _typed_and_end(b, mark) == ["typed:click 74", "end_utterance"]
        assert _badges_clicked(b, mark) == []
        assert _other_clicks(b, mark) == []
