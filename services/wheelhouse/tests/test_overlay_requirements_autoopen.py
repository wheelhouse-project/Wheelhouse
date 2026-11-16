"""Auto-open and overlay-settings requirements, tested at the process boundary.

Bead: wh-overlay-refactor-pilot.1. Each test pins one CONFIRMED row of the
table "Auto-open on an ambiguous click, and the overlay settings" in
docs/design/overlay-requirements.md and is named after its requirement id.
AO-5 is a NOT_REQUIRED row and has no test. AO-30 was a CODE_ONLY row until
David confirmed it on 2026-11-06 ("keep today's behavior"). No row of this
table needs the paused state except as one
entry of AO-10's list; that entry is left out, because nothing in the shipped
app sends the microphone pause event.

Every test drives a real ``LogicController`` built through its constructor
(tests/overlay_boundary_harness.py says how) and asserts only on what leaves
the Logic process: frames for the Input process, dicts for the GUI process,
typed words, and the Win32 focus hooks Logic registers. No test names a
private member of ``LogicController``, patches one of its methods, or reads
its state.

THE AMBIGUOUS CLICK REPLY. The harness answers every click_element request
by itself with an ok click, so it cannot say "ambiguous". This file adds one
helper, ``ScriptedClickReplies``, that stands in front of the real
``WheelHouseApp.response_queue`` (the queue the Input process answers on).
When the harness puts its ok answer for a click_element request, the helper
puts the reply the test scripted instead, with the same request_id. That is
the reply a real Input process sends for an ambiguous match; nothing inside
Logic is touched. The harness docstring names this queue as the place every
Input reply arrives.

Each test also sees at least one positive output (a show_numbered_overlay
request, a paint, a notice, a click), so a test that expects silence cannot
pass because the overlay is broken as a whole.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Iterable, Optional

import pytest

from tests.overlay_boundary_harness import (
    NUMBERED_WINDOW,
    OTHER_WINDOW,
    FakeFocusHooks,
    item_id,
    overlay_boundary,
    settle,
    summary,
    walk_failed_reply,
    walk_ok_reply,
    write_config,
)

pytestmark = pytest.mark.journey

# The id of the snapshot the ambiguous click walk produced.
CLICK_SNAP = "click-snap"
# The click walk found five controls; two of them are the finalists.
CLICK_NUMBERS = (1, 2, 3, 4, 5)
FINALISTS = (2, 4)
# Longer than every deadline an auto-open build has: the show_numbered_overlay
# reply timeout (response_timeout_ms 3000) and the machine's walk deadline
# (screen_read_timeout_ms 10000 plus PRE_WALK_MARGIN_MS 250).
_PAST_WALK_DEADLINE_S = 21.0
# Longer than the 1000 ms paint wait.
_PAST_PAINT_DEADLINE_S = 1.2


# ---------------------------------------------------------------------------
# The Input process's answer to click_element
# ---------------------------------------------------------------------------


class ScriptedClickReplies:
    """The response queue, with scripted answers for click_element requests.

    ``pending`` holds builders, oldest first. Each takes the trace_id of the
    request and returns the reply payload. A click_element answer the harness
    puts while ``pending`` is empty goes through unchanged (an ok click).
    """

    def __init__(self, real) -> None:
        self.real = real
        self.pending: list = []

    def put(self, message, *args, **kwargs):
        if (
            isinstance(message, dict)
            and message.get("action") == "click_element"
            and self.pending
        ):
            build = self.pending.pop(0)
            payload = dict(build(message.get("trace_id") or ""))
            payload["request_id"] = message["request_id"]
            payload["action"] = "click_element"
            message = payload
        return self.real.put(message, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


def scripted_replies(b) -> ScriptedClickReplies:
    """Put ``ScriptedClickReplies`` in front of the boundary's response queue."""

    scripted = ScriptedClickReplies(b.app.response_queue)
    b.app.response_queue = scripted
    return scripted


_UNSET = object()


def ambiguous_reply(
    *, snapshot_id=CLICK_SNAP, ids=_UNSET, with_summary: bool = True,
) -> Callable[[str], dict]:
    """A builder for an ambiguous click_element reply.

    By default the reply carries the click walk's five controls and the two
    finalists' item ids, as the Input process sends them.
    """

    def build(trace_id: str) -> dict:
        from services.wheelhouse.shared.click_element import (
            ClickElementResponse,
        )

        sid = snapshot_id if snapshot_id is not None else CLICK_SNAP
        finalist_ids = (
            tuple(item_id(sid, n) for n in FINALISTS) if ids is _UNSET else ids
        )
        payload = ClickElementResponse(
            status="ok",
            outcome="ambiguous",
            reason=None,
            matched_names=("save draft", "save all"),
            snapshot_id=snapshot_id,
            snapshot_summary=(
                summary(sid, *CLICK_NUMBERS) if with_summary else None
            ),
            matched_name=None,
            trace_id=trace_id,
            ambiguous_item_ids=(
                finalist_ids if isinstance(finalist_ids, tuple) else None
            ),
        ).to_dict()
        if finalist_ids is not None and not isinstance(finalist_ids, tuple):
            # A value the schema rejects (not a list or tuple).
            payload["ambiguous_item_ids"] = finalist_ids
        return payload

    return build


def filtered_summary(snapshot_id: str, finalists: Iterable[int]):
    """The finalists of a click walk renumbered 1..K, as Input builds them."""

    from services.wheelhouse.ui.element_types import (
        WalkSnapshotSummary,
        WalkSnapshotSummaryItem,
    )

    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id=item_id(snapshot_id, original),
                display_number=k,
                name=f"save {original}",
                role="Button",
                bounds=(10 * original, 10 * original,
                        10 * original + 8, 10 * original + 8),
                monitor_id=0,
            )
            for k, original in enumerate(finalists, start=1)
        ],
        created_at_monotonic=1.0,
    )


def show_reply(params: dict, outcome: str = "ok", *, gen_offset: int = 0) -> dict:
    """A show_numbered_overlay reply, through ``ShowNumberedOverlayResponse``.

    ``outcome`` is ``ok``, ``snapshot_expired``, ``no_targets``,
    ``execution_failed``, ``error`` (a handler crash), or ``malformed`` (a dict
    the schema rejects). ``gen_offset`` makes the echoed paint_generation
    differ from the request's.
    """

    from services.wheelhouse.shared.show_numbered_overlay import (
        ShowNumberedOverlayResponse,
    )

    if outcome == "malformed":
        return {"status": "ok", "outcome": "ok"}
    sid = params["snapshot_id"]
    ok = outcome == "ok"
    return ShowNumberedOverlayResponse(
        status="error" if outcome == "error" else "ok",
        outcome=outcome,
        reason=None if ok else f"harness_{outcome}",
        snapshot_id=sid if ok else None,
        snapshot_summary=filtered_summary(sid, FINALISTS) if ok else None,
        trace_id=params["trace_id"],
        overlay_session_id=params["overlay_session_id"],
        paint_generation=params["paint_generation"] + gen_offset,
    ).to_dict()


# ---------------------------------------------------------------------------
# Common steps
# ---------------------------------------------------------------------------


def _pair(message: dict) -> tuple:
    return (message["overlay_session_id"], message["paint_generation"])


def _actions(messages) -> list:
    return [getattr(m, "action", None) or m["action"] for m in messages]


def _ambiguous_notices(messages) -> list:
    return [
        m for m in messages
        if m.get("action") == "show_click_notice"
        and m.get("outcome") == "ambiguous"
    ]


async def ambiguous_click(b, scripted, reply=None) -> None:
    """'click save', answered by Input with an ambiguous match."""

    scripted.pending.append(reply or ambiguous_reply())
    await b.say("click save")
    await b.next_frame("click_element")


async def auto_open(b, scripted):
    """An ambiguous 'click save' that auto-opens; returns the build request."""

    await ambiguous_click(b, scripted)
    return await b.next_frame("show_numbered_overlay")


async def auto_open_painted(b, scripted) -> tuple:
    """Auto-open answered ok and the paint acknowledged.

    Returns ``(show_request, paint)``. Waits for the first lease renew, which
    Logic sends only for a painted overlay.
    """

    show = await auto_open(b, scripted)
    b.reply(show, show_reply(show.params))
    paint = await b.next_gui("paint_overlay")
    await b.gui_ack("painted", paint)
    await b.next_gui("overlay_lease_renew")
    await b.flush()
    return show, paint


async def assert_closed_by_new_session(b, old_session_id: int) -> None:
    """The overlay is closed: 'show numbers' starts a new session."""

    await b.say("show numbers")
    walk = await b.next_frame("start_overlay_walk")
    assert walk.params["overlay_session_id"] != old_session_id


async def say_answering_typing(b, text: str) -> list:
    """Say ``text`` and answer every typing request it makes.

    Returns the strings Logic asked the Input process to type.
    """

    start = len(b.frames)
    answered: set = set()
    task = b.say_soon(text)
    for _ in range(200):
        for frame in b.frames[start:]:
            if (
                frame.action == "intelligent_insert_text"
                and frame.request_id is not None
                and frame.index not in answered
            ):
                answered.add(frame.index)
                b.reply(frame, {"status": "ok"})
        if task.done():
            break
        await asyncio.sleep(0.02)
    await asyncio.wait_for(task, 5.0)
    await b.flush()
    return [
        f.params.get("insertion_string") for f in b.frames[start:]
        if f.action == "intelligent_insert_text"
    ]


# ---------------------------------------------------------------------------
# The auto-open itself
# ---------------------------------------------------------------------------


async def test_ao_1_ambiguous_click_opens_overlay_instead_of_notice(tmp_path):
    """AO-1: an ambiguous click with the overlay closed auto-opens it.

    Rule: Logic sends no click notice to the GUI and sends a
    show_numbered_overlay command to Input.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        mark = b.mark()
        await auto_open(b, scripted)
        frames, gui = await b.quiet(mark, 0.3)
        assert "show_numbered_overlay" in _actions(frames)
        assert "show_click_notice" not in _actions(gui)


async def test_ao_2_auto_open_request_reuses_click_snapshot_with_filter(
    tmp_path,
):
    """AO-2: the auto-open request carries the click snapshot and the filter.

    Rule: show_numbered_overlay carries the reuse snapshot_id from the click
    response, item_id_filter equal to ambiguous_item_ids as a list, and the
    new overlay_session_id and paint_generation. No fresh walk is sent.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        # An earlier session, so "new session id" has something to differ from.
        earlier = await b.show_numbers_painted("snap-earlier")
        await b.say("hide numbers")
        await b.next_gui("clear_overlay")
        await b.flush()
        mark = b.mark()
        show = await auto_open(b, scripted)
        assert show.params["snapshot_id"] == CLICK_SNAP
        assert show.params["item_id_filter"] == [
            item_id(CLICK_SNAP, n) for n in FINALISTS
        ]
        assert isinstance(show.params["item_id_filter"], list)
        assert isinstance(show.params["overlay_session_id"], int)
        assert isinstance(show.params["paint_generation"], int)
        assert show.params["overlay_session_id"] != earlier["overlay_session_id"]
        assert show.request_id is not None
        frames, _gui = await b.quiet(mark, 0.3)
        assert "start_overlay_walk" not in _actions(frames)


async def test_ao_3_pin_and_paint_wait_for_the_build_reply(tmp_path):
    """AO-3: no pin or paint before the reply; then pin, then a 1..K paint.

    Rule: Logic sends no pin or paint command before the show_numbered_overlay
    reply arrives; after an ok reply it sends pin_snapshot for the snapshot_id
    and then paint_overlay carrying the filtered, renumbered 1..K list.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        mark = b.mark()
        show = await auto_open(b, scripted)
        frames, gui = await b.quiet(mark, 0.3)
        assert "pin_snapshot" not in _actions(frames)
        assert "paint_overlay" not in _actions(gui)

        b.reply(show, show_reply(show.params))
        paint = await b.next_gui("paint_overlay")
        # The paint waits behind the pin, so the pin is already out.
        pins = [f for f in b.frames_since(mark) if f.action == "pin_snapshot"]
        assert [p.params["snapshot_id"] for p in pins] == [CLICK_SNAP]
        assert _pair(pins[0].params) == _pair(show.params)
        assert _pair(paint) == _pair(show.params)
        assert paint["snapshot_id"] == CLICK_SNAP
        assert [i["display_number"] for i in paint["items"]] == [1, 2]
        assert [i["item_id"] for i in paint["items"]] == [
            item_id(CLICK_SNAP, n) for n in FINALISTS
        ]


async def test_ao_4_auto_open_closes_the_mouse_grid_first(tmp_path):
    """AO-4: an auto-open with the mouse grid open closes the grid first.

    Rule: Logic closes the mouse grid before the overlay starts.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.say("show grid")
        await b.next_gui("paint_grid")
        await b.flush()
        start = len(b.gui)
        await auto_open(b, scripted)
        await b.flush()
        later = [
            m.get("action") for m in b.gui[start:] if isinstance(m, dict)
        ]
        assert "clear_grid" in later
        # The overlay starts with its walking cue; the grid closed before it.
        assert "overlay_walk_cue" in later
        assert later.index("clear_grid") < later.index("overlay_walk_cue")


# ---------------------------------------------------------------------------
# When the auto-open does not fire
# ---------------------------------------------------------------------------


async def _assert_plain_ambiguous_notice(b, scripted, reply=None) -> dict:
    """An ambiguous click shows the plain notice and opens nothing."""

    await b.flush()
    mark = b.mark()
    await ambiguous_click(b, scripted, reply)
    notice = await b.next_gui("show_click_notice")
    frames, gui = await b.quiet(mark, 0.3)
    assert "show_numbered_overlay" not in _actions(frames)
    assert "start_overlay_walk" not in _actions(frames)
    assert "paint_overlay" not in _actions(gui)
    return notice


@pytest.mark.parametrize(
    "click_settings",
    [
        pytest.param({"overlay_enabled": False}, id="overlay-disabled"),
        pytest.param({"overlay_badge_theme": "purple"}, id="bad-overlay-key"),
    ],
)
async def test_ao_6_overlay_off_gives_the_plain_ambiguous_notice(
    tmp_path, click_settings,
):
    """AO-6: with the overlay off, an ambiguous click shows the plain notice.

    Rule: Logic sends the plain ambiguous click notice (show_click_notice) to
    the GUI and sends no show_numbered_overlay command.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        notice = await _assert_plain_ambiguous_notice(b, scripted)
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"
        assert notice["snapshot_id"] == CLICK_SNAP


async def test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice(tmp_path):
    """AO-7: with overlay_auto_open_on_ambiguous false, no auto-open.

    Rule: Logic sends the plain ambiguous click notice to the GUI and sends no
    show_numbered_overlay command.
    """

    config = write_config(tmp_path, overlay_auto_open_on_ambiguous=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        notice = await _assert_plain_ambiguous_notice(b, scripted)
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"
        # The overlay itself is on: 'show numbers' still walks.
        await b.say("show numbers")
        await b.next_frame("start_overlay_walk")


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            ambiguous_reply(snapshot_id=None, with_summary=False),
            id="no-snapshot-id",
        ),
        pytest.param(ambiguous_reply(ids=None), id="ids-missing"),
        pytest.param(ambiguous_reply(ids=()), id="ids-empty"),
    ],
)
async def test_ao_8_unusable_click_reply_gives_the_plain_notice(
    tmp_path, reply,
):
    """AO-8: no snapshot_id, or no finalist ids, means no auto-open.

    Rule: Logic sends the plain ambiguous click notice to the GUI and sends no
    show_numbered_overlay command.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        notice = await _assert_plain_ambiguous_notice(b, scripted, reply)
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"


async def test_ao_8_malformed_finalist_ids_give_malformed_response_notice(
    tmp_path,
):
    """AO-8 (note): a malformed ambiguous_item_ids makes the reply malformed.

    Rule: a malformed ambiguous_item_ids makes the whole click response
    malformed, so it gets a malformed_response execution_failed notice and no
    show_numbered_overlay command.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        notice = await _assert_plain_ambiguous_notice(
            b, scripted, ambiguous_reply(ids="click-snap-item-2"),
        )
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "malformed_response"


async def test_ao_9_click_disabled_sends_no_input_request(tmp_path):
    """AO-9: with [click] enabled false, no click request reaches Input.

    Rule: No Input request is sent; Logic shows only the one-time
    disabled_by_config notice (once per session). This test pins the "no
    request" half through both click commands: 'double click save' (the
    gesture command, which shows the notice itself, once) and 'x-ray click
    save' (the safety word makes the speech processor show it).
    """

    config = write_config(tmp_path, enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        scripted.pending.append(ambiguous_reply())
        await b.flush()
        mark = b.mark()
        await b.say("double click save")
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "disabled_by_config"
        await b.say("double click save")
        await b.say("x-ray click save")
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert "show_numbered_overlay" not in _actions(frames)
        notices = [m for m in gui if m["action"] == "show_click_notice"]
        assert {n["reason"] for n in notices} == {"disabled_by_config"}
        # The scripted ambiguous reply was never asked for.
        assert len(scripted.pending) == 1


async def test_ao_9_gesture_click_disabled_notice_shows_once(tmp_path):
    """AO-9 (gesture command): the disabled notice shows once per session.

    Rule: Logic shows only the one-time disabled_by_config notice (once per
    session).
    """

    config = write_config(tmp_path, enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        mark = b.mark()
        for _ in range(3):
            await b.say("double click save")
        await b.flush()
        notices = [
            m for m in b.gui_since(mark) if m["action"] == "show_click_notice"
        ]
        assert [n["reason"] for n in notices] == ["disabled_by_config"]


async def test_ao_9_safety_word_click_disabled_notice_shows_once(tmp_path):
    """AO-9 (safety-word click): the disabled notice shows once per session.

    Rule: Logic shows only the one-time disabled_by_config notice (once per
    session).
    """

    config = write_config(tmp_path, enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        mark = b.mark()
        for _ in range(3):
            await b.say("x-ray click save")
        await b.flush()
        notices = [
            m for m in b.gui_since(mark) if m["action"] == "show_click_notice"
        ]
        # Positive output: the first one was shown.
        assert notices
        assert [n["reason"] for n in notices] == ["disabled_by_config"]


@pytest.mark.parametrize("open_state", ["walk_in_flight", "painted"])
async def test_ao_10_ambiguous_click_does_not_take_over_an_open_overlay(
    tmp_path, open_state,
):
    """AO-10: an ambiguous click while the overlay is open shows the notice.

    Rule: Logic sends the plain ambiguous click notice to the GUI; the open
    overlay is not changed and no show_numbered_overlay command is sent.
    (The paused entry of the rule's state list is out of reach.)
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        walk = None
        if open_state == "painted":
            paint = await b.show_numbers_painted("snap-1")
        else:
            await b.say("show numbers")
            walk = await b.next_frame("start_overlay_walk")
        await b.flush()
        mark = b.mark()
        await ambiguous_click(b, scripted)
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "ambiguous"
        frames, gui = await b.quiet(mark, 0.3)
        assert "show_numbered_overlay" not in _actions(frames)
        assert "clear_overlay" not in _actions(gui)
        assert "unpin_snapshot" not in _actions(frames)
        assert "start_overlay_walk" not in _actions(frames)
        if open_state == "painted":
            # The open overlay still answers 'click N' from its own badges.
            await b.say("click two")
            click = await b.next_frame("click_snapshot_item")
            assert click.params["snapshot_id"] == "snap-1"
            assert click.params["item_id"] == item_id("snap-1", 2)
            assert _pair(paint)  # the paint the overlay kept
        else:
            # The walk in flight still paints its own reply.
            b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-1"))
            paint = await b.next_gui("paint_overlay")
            assert paint["snapshot_id"] == "snap-1"
            assert _pair(paint) == _pair(walk.params)


# ---------------------------------------------------------------------------
# The auto-opened overlay
# ---------------------------------------------------------------------------


async def test_ao_11_cached_list_is_only_the_renumbered_finalists(tmp_path):
    """AO-11: after the auto-open, numbers name only the K finalists.

    Rule: The cache entry for that snapshot_id holds only the K finalists
    numbered 1..K; the unfiltered list is gone. Badge 1 is the first
    finalist (not the click walk's control 1), and the click walk's numbers
    above K name nothing.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await auto_open_painted(b, scripted)
        mark = b.mark()
        for spoken in ("click three", "click five"):
            await b.say(spoken)
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "no_badge_numbered"
        frames, _gui = await b.quiet(mark, 0.2)
        assert "click_snapshot_item" not in _actions(frames)
        assert "click_element" not in _actions(frames)
        # Asked last: a successful badge click closes the numbers.
        await b.say("click one")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == CLICK_SNAP
        assert click.params["item_id"] == item_id(CLICK_SNAP, FINALISTS[0])
        assert click.params["item_id"] != item_id(CLICK_SNAP, 1)


@pytest.mark.parametrize("number, spoken", [(1, "one"), (2, "two")])
async def test_ao_12_click_n_on_auto_open_clicks_that_finalist(
    tmp_path, number, spoken,
):
    """AO-12: 'click N' with N in 1..K clicks the finalist that carried N.

    Rule: Logic sends click_snapshot_item with the snapshot_id and the item_id
    of the finalist that carried badge N.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await auto_open_painted(b, scripted)
        await b.say(f"click {spoken}")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == CLICK_SNAP
        assert click.params["item_id"] == item_id(
            CLICK_SNAP, FINALISTS[number - 1],
        )


async def test_ao_13_click_n_above_k_shows_no_badge_notice(tmp_path):
    """AO-13: 'click N' with N greater than K shows the no_badge notice.

    Rule: Logic shows the no_badge_numbered notice and sends no
    click_snapshot_item or click_element command.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await auto_open_painted(b, scripted)
        mark = b.mark()
        await b.say("click seven")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "no_badge_numbered"
        frames, _gui = await b.quiet(mark, 0.3)
        assert "click_snapshot_item" not in _actions(frames)
        assert "click_element" not in _actions(frames)


@pytest.mark.parametrize("phrase", ["hide numbers", "dismiss numbers"])
async def test_ao_15_hide_numbers_closes_auto_open_without_notice(
    tmp_path, phrase,
):
    """AO-15: 'hide numbers' on an auto-opened overlay closes it quietly.

    Rule: Logic sends clear_overlay and unpin_snapshot, and the overlay
    closes; the earlier ambiguous notice is not shown.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        _show, paint = await auto_open_painted(b, scripted)
        await b.say(phrase)
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == CLICK_SNAP
        await b.quiet(b.mark(), 0.3)
        assert _ambiguous_notices(b.gui_since(start)) == []
        # Closed: a number is a control name again.
        await b.say("click two")
        await b.next_frame("click_element")


# ---------------------------------------------------------------------------
# Auto-open failures: the suppressed notice is the delayed fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, gen_offset",
    [
        pytest.param("snapshot_expired", 0, id="snapshot-expired"),
        pytest.param("no_targets", 0, id="no-targets"),
        pytest.param("execution_failed", 0, id="execution-failed"),
        pytest.param("error", 0, id="handler-error"),
        pytest.param("malformed", 0, id="malformed"),
        pytest.param("ok", 1, id="echoed-generation-differs"),
    ],
)
async def test_ao_18_failed_build_reply_fires_the_suppressed_notice(
    tmp_path, outcome, gen_offset,
):
    """AO-18: a failed show_numbered_overlay reply fires the held notice.

    Rule: Logic sends the suppressed ambiguous click notice to the GUI
    (delayed fallback), then clears and unpins, and the overlay returns to
    closed. In walk_in_flight nothing is pinned or painted yet, so nothing
    is left on screen to clear and no pin is left to drop. (The send-failure
    case cannot be produced at the boundary.)
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        show = await auto_open(b, scripted)
        b.reply(show, show_reply(show.params, outcome, gen_offset=gen_offset))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"
        assert notice["snapshot_id"] == CLICK_SNAP
        await b.flush()
        assert "paint_overlay" not in _actions(b.gui_since(start))
        assert "pin_snapshot" not in _actions(b.frames_since(start))
        assert len(_ambiguous_notices(b.gui_since(start))) == 1
        await assert_closed_by_new_session(b, show.params["overlay_session_id"])


async def test_ao_19_no_build_reply_fires_the_suppressed_notice(tmp_path):
    """AO-19: no show_numbered_overlay reply by the deadline fires the notice.

    Rule: Logic sends the suppressed ambiguous click notice and the overlay
    returns to closed.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        show = await auto_open(b, scripted)
        mark = b.mark()
        _frames, gui = await b.quiet(mark, 0.2)
        assert "show_click_notice" not in _actions(gui)
        await b.advance(_PAST_WALK_DEADLINE_S)
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"
        await b.flush()
        assert len(_ambiguous_notices(b.gui_since(start))) == 1
        assert "paint_overlay" not in _actions(b.gui_since(start))
        await assert_closed_by_new_session(b, show.params["overlay_session_id"])


@pytest.mark.parametrize("how", ["failed", "no_ack"])
async def test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice(
    tmp_path, how,
):
    """AO-20 (failed ack) and AO-21 (no ack in 1000 ms) on an auto-open paint.

    Rule: Logic sends the suppressed ambiguous click notice (and no generic
    'numbers could not be drawn' notice), clears the screen, unpins, and the
    overlay returns to closed.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        show = await auto_open(b, scripted)
        b.reply(show, show_reply(show.params))
        paint = await b.next_gui("paint_overlay")
        if how == "failed":
            await b.gui_ack("failed", paint)
        else:
            await b.advance(_PAST_PAINT_DEADLINE_S)
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "ambiguous"
        assert notice["spoken_name"] == "save"
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == CLICK_SNAP
        await b.flush()
        notices = [
            m for m in b.gui_since(start) if m["action"] == "show_click_notice"
        ]
        assert [n["outcome"] for n in notices] == ["ambiguous"]
        assert "numbers_not_drawn" not in [n["reason"] for n in notices]
        await assert_closed_by_new_session(b, show.params["overlay_session_id"])


async def test_ao_22_painted_auto_open_never_fires_the_held_notice(tmp_path):
    """AO-22: once an auto-open paints, the held notice is never sent.

    Rule: The suppressed ambiguous click notice is never sent, even if a
    later refresh fails. The later refresh failure fires only the generic
    'numbers could not be drawn' notice.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        _show, paint = await auto_open_painted(b, scripted)
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        b.desktop.bring_to_front(OTHER_WINDOW)
        b.reply(refresh, walk_failed_reply(refresh.params, "execution_failed"))
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint["overlay_session_id"]
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "numbers_not_drawn"
        await b.quiet(b.mark(), 0.3)
        assert _ambiguous_notices(b.gui_since(start)) == []


# ---------------------------------------------------------------------------
# The overlay settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "click_settings",
    [
        pytest.param({"overlay_enabled": False}, id="overlay-disabled"),
        pytest.param({"overlay_badge_corner": "middle"}, id="bad-overlay-key"),
    ],
)
async def test_ao_26_click_number_with_overlay_off_clicks_by_name(
    tmp_path, click_settings,
):
    """AO-26: with the overlay off, 'click 7' is a by-name click for '7'.

    Rule: Logic sends a by-name click_element query for the name '7' to Input
    (the overlay state is closed), exactly as for any other name.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        mark = b.mark()
        # The overlay really is off: 'show numbers' sends nothing.
        await b.say("show numbers")
        await b.say("click 7")
        click = await b.next_frame("click_element")
        assert click.params["query"].name == "7"
        assert click.params["query"].role is None
        frames, gui = await b.quiet(mark, 0.3)
        assert _actions(frames) == ["click_element"]
        assert gui == []


# Each overlay-track key with a value of the wrong type or out of range.
_BAD_OVERLAY_VALUES = [
    ("overlay_enabled", "yes"),
    ("overlay_badge_font_pt", 5),
    ("overlay_badge_font_pt", 97),
    ("overlay_badge_font_pt", "8"),
    ("overlay_badge_corner", "middle"),
    ("overlay_badge_trailing_space", 1),
    ("overlay_badge_shadow", "no"),
    ("overlay_badge_theme", "purple"),
    ("overlay_auto_open_on_ambiguous", 1),
    ("overlay_focus_debounce_ms", 5001),
    ("overlay_focus_debounce_ms", -1),
    ("overlay_bounds_tolerance_physical_px", 201),
    ("overlay_bounds_tolerance_logical_px", 201),
    ("snapshot_store_capacity", 0),
    ("snapshot_store_capacity", 65),
    ("overlay_browser_refresh_seconds", 301),
    ("overlay_settle_after_click", 0),
    ("overlay_keep_numbers", "false"),
    ("grid_min_cell_px", 0),
    ("grid_min_cell_px", 501),
    ("drag_duration_ms", 5001),
]


@pytest.mark.parametrize(
    "key, value", [
        pytest.param(k, v, id=f"{k}={v!r}") for k, v in _BAD_OVERLAY_VALUES
    ],
)
async def test_ao_27_bad_overlay_key_turns_off_only_the_overlay(
    tmp_path, key, value,
):
    """AO-27: a bad overlay-track key turns off the overlay, not clicking.

    Rule: Only the overlay is disabled (overlay_enabled_effective false):
    by-name click keeps working; AO-6, AO-24 and AO-25 apply ('show numbers'
    sends nothing, and an ambiguous click gets the plain notice).
    """

    config = write_config(tmp_path, **{key: value})
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        # By-name click still reaches Input, and its ambiguous answer gets
        # the plain notice instead of an auto-open.
        notice = await _assert_plain_ambiguous_notice(b, scripted)
        assert notice["outcome"] == "ambiguous"


@pytest.mark.parametrize(
    "click_settings, feature_on",
    [
        pytest.param({}, True, id="control-all-valid"),
        pytest.param({"enabled": False}, False, id="click-disabled"),
        pytest.param(
            {"response_timeout_ms": "fast"}, False, id="bad-phase-1-key",
        ),
    ],
)
async def test_ao_28_click_off_turns_off_the_whole_feature(
    tmp_path, click_settings, feature_on,
):
    """AO-28: an invalid Phase 1 key or enabled false turns off everything.

    Rule: The whole feature is off including the overlay: no show/hide
    numbers handling, no auto-open, no focus-hook follow, and a click command
    gets the one-time disabled_by_config notice (or types the words when the
    caller defers it). The control case (all keys valid) shows each of these
    outputs exists when the feature is on.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        await b.flush()
        # No focus-hook follow: Logic registers no Win32 focus hook at all.
        assert bool(FakeFocusHooks.instances) is feature_on

        mark = b.mark()
        await b.say("show numbers")
        await b.say("hide numbers")
        frames, gui = await b.quiet(mark, 0.3)
        if feature_on:
            assert "start_overlay_walk" in _actions(frames)
            return
        assert frames == []
        assert gui == []

        # No auto-open: no click request reaches Input to come back ambiguous.
        scripted.pending.append(ambiguous_reply())
        typed = await say_answering_typing(b, "click save")
        # Without the safety word the caller defers the notice: the words
        # are typed and no notice appears.
        assert typed == ["click save"]
        assert "show_click_notice" not in _actions(b.gui_since(mark))
        # The gesture click shows the disabled notice, once.
        await b.say("double click save")
        await b.say("double click save")
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        notices = [m for m in gui if m["action"] == "show_click_notice"]
        assert [n["reason"] for n in notices] == ["disabled_by_config"]
        assert len(scripted.pending) == 1


# ---------------------------------------------------------------------------
# After the auto-open: the whole window again, and no stale filter
# ---------------------------------------------------------------------------

# The whole window, as a fresh walk reads it after the auto-open.
FULL_SNAP = "snap-full"
FULL_NUMBERS = (1, 2, 3, 4, 5, 6)
_MENUPOPUPEND = 0x0007


async def _foreground(b, window) -> None:
    """Windows reports a new foreground window, and it is in front."""

    b.desktop.bring_to_front(window)
    b.hooks.foreground(window.hwnd)
    await settle()


async def _assert_whole_window_walk(b, walk, *, after) -> None:
    """``walk`` is an unfiltered walk; its reply paints every control.

    Then 'click 6' clicks control 6 of the fresh walk: a number no auto-open
    list had.
    """

    assert "item_id_filter" not in walk.params
    b.reply(walk, walk_ok_reply(
        walk.params, snapshot_id=FULL_SNAP, numbers=FULL_NUMBERS,
    ))
    paint = await b.next_gui("paint_overlay")
    assert paint["snapshot_id"] == FULL_SNAP
    assert [i["display_number"] for i in paint["items"]] == list(FULL_NUMBERS)
    assert [i["item_id"] for i in paint["items"]] == [
        item_id(FULL_SNAP, n) for n in FULL_NUMBERS
    ]
    await b.gui_ack("painted", paint)
    assert "show_numbered_overlay" not in _actions(b.frames_since(after))
    await b.say("click six")
    click = await b.next_frame("click_snapshot_item")
    assert click.params["snapshot_id"] == FULL_SNAP
    assert click.params["item_id"] == item_id(FULL_SNAP, 6)


async def test_ao_16_show_numbers_after_auto_open_walks_the_whole_window(
    tmp_path,
):
    """AO-16: 'show numbers' after a painted auto-open is a full refresh walk.

    Rule: Logic runs a normal full refresh walk (start_overlay_walk) and the
    badges become the whole window, not the finalists.
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        show, paint = await auto_open_painted(b, scripted)
        sid, gen = _pair(paint)
        mark = b.mark()
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        assert walk.params["overlay_session_id"] == show.params[
            "overlay_session_id"
        ]
        await _assert_whole_window_walk(b, walk, after=mark)


@pytest.mark.parametrize(
    "trigger, keep_numbers",
    [
        pytest.param("foreground_same", False, id="foreground-same-window"),
        pytest.param("foreground_other", True, id="foreground-other-keep-on"),
        pytest.param("menu_end", False, id="menu-close"),
    ],
)
async def test_ao_17_focus_rewalk_after_auto_open_paints_the_whole_window(
    tmp_path, trigger, keep_numbers,
):
    """AO-17: a focus or menu re-walk after an auto-open is unfiltered.

    Rule: after an auto-open reaches painted, a window focus change (or a menu
    opening or closing) re-walks with start_overlay_walk, unfiltered, and
    paints the whole new window, not the finalists.
    """

    config = write_config(tmp_path, overlay_keep_numbers=keep_numbers)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        scripted = scripted_replies(b)
        _show, paint = await auto_open_painted(b, scripted)
        sid, gen = _pair(paint)
        mark = b.mark()
        if trigger == "foreground_same":
            await _foreground(b, NUMBERED_WINDOW)
        elif trigger == "foreground_other":
            await _foreground(b, OTHER_WINDOW)
        else:
            b.hooks.menu_popup(_MENUPOPUPEND)
            await settle()
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        await _assert_whole_window_walk(b, walk, after=mark)


# A second ambiguous click, on another snapshot, with other finalists.
SECOND_SNAP = "click-snap-2"
SECOND_FINALISTS = (1, 3, 5)


async def _close_auto_open(b, scripted, how: str) -> dict:
    """Auto-open, then return the overlay to closed by ``how``.

    Returns the auto-open's show_numbered_overlay request params.
    """

    if how == "failed_ack":
        show = await auto_open(b, scripted)
        b.reply(show, show_reply(show.params))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("failed", paint)
        await b.next_gui("clear_overlay")
        await b.next_frame("unpin_snapshot")
        return show.params
    show, paint = await auto_open_painted(b, scripted)
    if how == "hide":
        await b.say("hide numbers")
    else:
        # overlay_keep_numbers is off: a change of window removes the numbers.
        await _foreground(b, OTHER_WINDOW)
    clear = await b.next_gui("clear_overlay")
    assert _pair(clear) == _pair(paint)
    await b.next_frame("unpin_snapshot")
    if how == "focus":
        b.desktop.bring_to_front(NUMBERED_WINDOW)
    await b.flush()
    return show.params


@pytest.mark.parametrize("how", ["hide", "focus", "failed_ack"])
async def test_ao_23_closed_auto_open_leaves_no_filter_for_later_builds(
    tmp_path, how,
):
    """AO-23: after an auto-open closes, no later build carries its filter.

    Rule: a hide-numbers command, a focus event, or a paint acknowledgement
    returns the overlay to closed: no filter from the earlier auto-open is
    used by any later show_numbered_overlay or walk request. The next
    'show numbers' walks unfiltered and paints the whole window; the next
    auto-open carries only its own finalists.

    (The apply-or-stash failure half of the row cannot be produced at the
    boundary: nothing a test can send makes the AUTO_OPEN apply raise.)
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        first = await _close_auto_open(b, scripted, how)
        mark = b.mark()
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert walk.params["overlay_session_id"] != first["overlay_session_id"]
        await _assert_whole_window_walk(b, walk, after=mark)
        await b.say("hide numbers")
        await b.next_gui("clear_overlay")
        await b.flush()
        second_ids = tuple(item_id(SECOND_SNAP, n) for n in SECOND_FINALISTS)
        scripted.pending.append(
            ambiguous_reply(snapshot_id=SECOND_SNAP, ids=second_ids),
        )
        await b.say("click save")
        show2 = await b.next_frame("show_numbered_overlay")
        assert show2.params["snapshot_id"] == SECOND_SNAP
        assert show2.params["item_id_filter"] == list(second_ids)
        assert first["item_id_filter"] == [
            item_id(CLICK_SNAP, n) for n in FINALISTS
        ]


# ---------------------------------------------------------------------------
# 'show numbers' and 'hide numbers' with the overlay turned off
# ---------------------------------------------------------------------------

_AO_30_OFF = [
    pytest.param({"overlay_enabled": False}, id="overlay-disabled"),
    pytest.param({"enabled": False}, id="click-disabled"),
]


# The frame the speech path sends Input at the end of every utterance. It is
# not overlay output and not typing.
_SPEECH_BOOKKEEPING = frozenset({"end_utterance"})


def _gui_actions_since(b, start: int) -> list:
    """Every GUI action sent after position ``start`` of the GUI log."""

    return [m.get("action") for m in b.gui[start:] if isinstance(m, dict)]


async def _say_bounded(b, text: str, *, wait_s: float = 3.0) -> tuple:
    """Say ``text``, answer its typing requests, and collect what it sent.

    Returns ``(typed, frames, gui, finished)``: the strings Logic asked Input
    to type, every other frame action except the speech bookkeeping, every
    GUI action, and whether the utterance finished within ``wait_s``.

    Unlike ``say_answering_typing`` this does not wait without a limit for
    the utterance to finish. An utterance that runs some other command and
    waits on an Input reply nobody sends is cancelled after ``wait_s``, so
    the caller's assertion on ``frames`` names the frame it sent rather than
    failing on a TimeoutError.
    """

    await b.flush()
    frame_start, gui_start = len(b.frames), len(b.gui)
    answered: set = set()
    task = b.say_soon(text)

    def _answer_typing() -> None:
        for frame in b.frames[frame_start:]:
            if (
                frame.action == "intelligent_insert_text"
                and frame.request_id is not None
                and frame.index not in answered
            ):
                answered.add(frame.index)
                b.reply(frame, {"status": "ok"})

    deadline = asyncio.get_running_loop().time() + wait_s
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        _answer_typing()
        await asyncio.sleep(0.02)
    _answer_typing()
    finished = task.done()
    if not finished:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.3)
    await settle()
    await b.flush()
    new_frames = b.frames[frame_start:]
    typed = [
        f.params.get("insertion_string") for f in new_frames
        if f.action == "intelligent_insert_text"
    ]
    frames = [
        f.action for f in new_frames
        if f.action not in _SPEECH_BOOKKEEPING
        and f.action != "intelligent_insert_text"
    ]
    return typed, frames, _gui_actions_since(b, gui_start), finished


@pytest.mark.parametrize("phrase", ["show numbers", "hide numbers"])
@pytest.mark.parametrize("click_settings", _AO_30_OFF)
async def test_ao_30_show_or_hide_numbers_with_overlay_off_is_a_silent_command(
    tmp_path, click_settings, phrase,
):
    """AO-30: with the overlay off, show/hide numbers is a silent command.

    Rule: overlay_enabled is false or click.enabled is false, and the user
    says 'show numbers' or 'hide numbers': the utterance is accepted as a
    command (not typed as dictation): no words are typed, no notice appears,
    and nothing is sent to Input or GUI.

    Positive controls in the same boundary: a dictated sentence afterwards is
    typed (so an empty typed list is not a broken typing path), and a click
    command afterwards reaches Input (overlay off) or shows the disabled
    notice (clicking off), so both channels carry output in this config.
    test_ao_30_control_overlay_on_show_and_hide_reach_the_boundary is the
    control with both settings on.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        typed, frames, gui, finished = await _say_bounded(b, phrase)
        assert typed == []
        # Every frame, not only the overlay set: the one frame left out is
        # the speech path's own end-of-utterance bookkeeping, which every
        # utterance sends.
        assert frames == []
        assert gui == []
        assert finished

        # Positive control 1: dictation is typed in this boundary (the speech
        # path types it word by word).
        typed = await say_answering_typing(b, "hello there")
        assert typed == ["hello", "there"]

        # Positive control 2: each channel carries output in this config.
        await b.flush()
        mark = b.mark()
        if click_settings.get("enabled", True):
            await b.say("click 7")
            click = await b.next_frame("click_element")
            assert click.params["query"].name == "7"
        else:
            await b.say("double click save")
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "disabled_by_config"
            assert b.frames_since(mark) == []


async def test_ao_30_control_overlay_on_show_and_hide_reach_the_boundary(
    tmp_path,
):
    """AO-30 control: with both settings on, show and hide numbers send output.

    Rule (the control case of AO-30, config gate overlay_enabled=true and
    click.enabled=true): 'show numbers' sends start_overlay_walk to Input and
    'hide numbers' over the painted overlay sends clear_overlay to the GUI.
    Neither is typed.
    """

    async with overlay_boundary(tmp_path) as b:
        typed, frames, _gui, finished = await _say_bounded(b, "show numbers")
        assert typed == []
        assert frames == ["start_overlay_walk"]
        assert finished
        walk = await b.next_frame("start_overlay_walk")
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint)
        typed, _frames, gui, finished = await _say_bounded(b, "hide numbers")
        assert typed == []
        assert "clear_overlay" in gui
        assert finished
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint["overlay_session_id"]


async def test_ao_30_show_numbers_with_overlay_off_leaves_the_grid_open(
    tmp_path,
):
    """AO-30: with the overlay off, 'show numbers' leaves an open grid open.

    Rule: 'show numbers' also leaves an open mouse grid open. Only the
    overlay_enabled=false case: with click.enabled false the mouse grid
    itself is off and cannot be opened.

    Positive controls: 'show grid' paints the grid first, and 'hide grid'
    afterwards sends clear_grid, so the grid was still open and a clear_grid
    is observable.
    """

    config = write_config(tmp_path, overlay_enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.say("show grid")
        await b.next_gui("paint_grid")
        typed, frames, gui, finished = await _say_bounded(b, "show numbers")
        assert typed == []
        assert frames == []
        assert gui == []
        assert finished
        await b.say("hide grid")
        await b.next_gui("clear_grid")


# ---------------------------------------------------------------------------
# The auto-open reply limit and walking cue
# ---------------------------------------------------------------------------

# Longer than response_timeout_ms 3000 and shorter than screen_read_timeout_ms.
_PAST_RESPONSE_TIMEOUT_S = 3.5
# Two response_timeout_ms (config.toml.example 3000): the snapshot lookup and
# the pin acknowledgement. An auto-open does not wait for a dictation sentence.
_AUTO_OPEN_CUE_BOUND_MS = 6000


async def test_ao_33_auto_open_walk_cue_carries_two_reply_limits(tmp_path):
    """AO-33: the auto-open walking cue carries two click reply limits.

    Rule: overlay_walk_cue with active true for an auto-open carries
    walk_timeout_ms equal to two response_timeout_ms (the snapshot lookup and
    the pin acknowledgement), with no wait for a dictation sentence
    (wh-overlay-slow-uia-stale-badges.3).
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        mark = b.mark()
        await auto_open(b, scripted)
        await b.flush()
        cues = [
            m for m in b.gui_since(mark, actions=frozenset({"overlay_walk_cue"}))
        ]
        assert [c["active"] for c in cues] == [True]
        assert cues[0]["walk_timeout_ms"] == _AUTO_OPEN_CUE_BOUND_MS


async def test_ao_34_auto_open_waits_only_the_click_reply_limit(tmp_path):
    """AO-34: an auto-open with no reply gives up after the click reply limit.

    Rule: Logic waits response_timeout_ms, not screen_read_timeout_ms, for the
    show_numbered_overlay reply, because the reply is a lookup and not a
    screen read. Then it sends the suppressed ambiguous click notice
    (wh-overlay-slow-uia-stale-badges.3).
    """

    async with overlay_boundary(tmp_path) as b:
        scripted = scripted_replies(b)
        await b.flush()
        start = b.mark()
        await auto_open(b, scripted)
        await b.advance(_PAST_RESPONSE_TIMEOUT_S)
        notice = await b.next_gui("show_click_notice", timeout_s=2.0)
        assert notice["outcome"] == "ambiguous"
        assert "paint_overlay" not in _actions(b.gui_since(start))
