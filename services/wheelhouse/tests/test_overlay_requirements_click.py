"""Click N and badge-click requirements of the numbered overlay, at the boundary.

Bead: wh-overlay-refactor-pilot.1. Each test pins one CONFIRMED row of the
table "Click N and badge clicks" in docs/design/overlay-requirements.md and is
named after its requirement id. Parts of a row that need the paused state are
left out: nothing in the shipped app sends the microphone pause event to the
overlay, so the paused state cannot be reached from a process boundary.

Every test drives a real ``LogicController`` built through its constructor
(tests/overlay_boundary_harness.py says how) and asserts only on what leaves
the Logic process: frames for the Input process (click_element,
click_snapshot_item, start_overlay_walk, unpin_snapshot, and the words typed
through intelligent_insert_text), and dicts for the GUI process
(show_click_notice, clear_overlay, paint_overlay). No test names a private
member of ``LogicController``, patches one of its methods, or reads its state.

MANUAL CLICK REPLIES. The harness answers every click_element and
click_snapshot_item request with an ok reply at once. A test that needs a
refusal, a late reply, or no reply at all uses the ``manual_clicks`` fixture,
which empties the harness's own set of auto-answered click actions for that
test (a harness constant, not a member of ``LogicController``). The test then
answers each click itself with ``click_reply``, built through the production
``ClickElementResponse`` schema.

Each test also sees at least one positive output (a click, a notice, a walk,
typed words), so a test that expects silence cannot pass because the overlay
is broken as a whole.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import tests.overlay_boundary_harness as harness_module
from tests.overlay_boundary_harness import (
    NUMBERED_WINDOW,
    OTHER_WINDOW,
    item_id,
    overlay_boundary,
    paint_ack,
    settle,
    summary,
    walk_failed_reply,
    walk_ok_reply,
    write_config,
)

pytestmark = pytest.mark.journey

# Longer than [click] response_timeout_ms (3000 ms in config.toml.example).
_PAST_CLICK_TIMEOUT_S = 3.2
# Longer than the settle deadline: screen_read_timeout_ms 10000 plus
# PRE_WALK_MARGIN_MS 250 plus the 5000 ms sentence wait (main.py
# make_click_overlay_state_machine).
_PAST_SETTLE_DEADLINE_S = 15.5
# Short of the settle read's own reply limit (screen_read_timeout_ms 10000).
_BEFORE_SETTLE_DEADLINE_S = 5.0
# Past that reply limit, and short of the settle deadline.
_PAST_SETTLE_READ_LIMIT_S = 10.5
# Longer than the 200 ms hold of a 'click N' in flight.
_PAST_HOLD_S = 0.35

_TYPED = frozenset({"intelligent_insert_text"})


# ---------------------------------------------------------------------------
# Helpers (this file only; the harness is shared and is not edited)
# ---------------------------------------------------------------------------


@pytest.fixture
def manual_clicks(monkeypatch):
    """Turn off the harness's automatic ok reply to click requests."""

    monkeypatch.setattr(harness_module, "_AUTO_CLICKED", frozenset())


def click_reply(
    params: dict, *, outcome: str = "ok", reason=None,
    matched_names=(), snapshot_id=None, snapshot_summary=None,
    matched_name=None,
) -> dict:
    """A click reply from the Input process, through ``ClickElementResponse``."""

    from services.wheelhouse.shared.click_element import ClickElementResponse

    return ClickElementResponse(
        status="ok",
        outcome=outcome,
        reason=reason,
        matched_names=tuple(matched_names),
        snapshot_id=snapshot_id,
        snapshot_summary=snapshot_summary,
        matched_name=matched_name,
        trace_id=params.get("trace_id", ""),
    ).to_dict()


def renamed_walk_reply(params: dict, *, snapshot_id: str, renamed: dict) -> dict:
    """A walk ok reply whose badges 1..3 carry the names in ``renamed``.

    Badges not named in ``renamed`` keep the name, role and bounds that
    ``summary`` gives them, so they are the same control as before a swap.
    """

    from services.wheelhouse.shared.start_overlay_walk import (
        StartOverlayWalkResponse,
    )
    from services.wheelhouse.ui.element_types import (
        WalkSnapshotSummary,
        WalkSnapshotSummaryItem,
    )

    base = summary(snapshot_id, 1, 2, 3)
    items = [
        WalkSnapshotSummaryItem(
            item_id=i.item_id,
            display_number=i.display_number,
            name=renamed.get(i.display_number, i.name),
            role=i.role,
            bounds=i.bounds,
            monitor_id=i.monitor_id,
        )
        for i in base.items
    ]
    reply = walk_ok_reply(params, snapshot_id=snapshot_id)
    rebuilt = StartOverlayWalkResponse.from_dict(reply)
    return StartOverlayWalkResponse(
        status=rebuilt.status,
        outcome=rebuilt.outcome,
        reason=rebuilt.reason,
        snapshot_id=snapshot_id,
        snapshot_summary=WalkSnapshotSummary(
            snapshot_id=snapshot_id, items=items,
            created_at_monotonic=base.created_at_monotonic,
        ),
        trace_id=rebuilt.trace_id,
        overlay_session_id=rebuilt.overlay_session_id,
        paint_generation=rebuilt.paint_generation,
        foreground_window=rebuilt.foreground_window,
        foreground_pid=rebuilt.foreground_pid,
        foreground_process_name=rebuilt.foreground_process_name,
        foreground_window_creation_time=rebuilt.foreground_window_creation_time,
    ).to_dict()


def snapshot_item_clicked(snapshot_id: str, display_number) -> dict:
    """A GUI badge click, through ``SnapshotItemClickedEvent``."""

    from services.wheelhouse.shared.snapshot_item_clicked import (
        SnapshotItemClickedEvent,
    )

    return SnapshotItemClickedEvent(
        snapshot_id=snapshot_id, display_number=display_number,
    ).to_dict()


def _pair(message: dict) -> tuple:
    return (message["overlay_session_id"], message["paint_generation"])


def _actions(messages) -> list:
    return [getattr(m, "action", None) or m["action"] for m in messages]


def _notices(b, mark) -> list:
    return b.gui_since(mark, actions={"show_click_notice"})


def _clicks(b, mark) -> list:
    return b.frames_since(mark, actions={"click_element", "click_snapshot_item"})


def _typed(b, mark) -> list:
    return [
        f.params.get("insertion_string") for f in b.frames_since(mark, _TYPED)
    ]


async def _next_typed(b, timeout_s: float = 5.0):
    """Wait for the words Logic types, and answer the insert as Input does."""

    frame = await b.next_frame("intelligent_insert_text", timeout_s=timeout_s)
    b.reply(frame, {"status": "ok"})
    await asyncio.sleep(0)
    return frame.params.get("insertion_string")


async def _painted_click_in_flight(b, number_word: str = "two"):
    """Painted snap-1, then 'click N' sent to Input and not yet answered."""

    paint = await b.show_numbers_painted("snap-1")
    mark = b.mark()
    await b.say(f"click {number_word}")
    click = await b.next_frame("click_snapshot_item")
    return paint, mark, click


async def _enter_error_state(b) -> None:
    """Reach the error state the only way a boundary can.

    The GUI acknowledges a paint at the CURRENT pair while the first walk is
    still in flight (no paint was ever sent): an invalid transition, which the
    machine answers by entering error. The walk's own ok reply then paints
    nothing, which shows the state is no longer walk_in_flight.
    """

    await b.say("show numbers")
    walk = await b.next_frame("start_overlay_walk")
    b.send_gui(paint_ack(
        "painted", walk.params["overlay_session_id"],
        walk.params["paint_generation"],
    ))
    await b.settle_gui()
    mark = b.mark()
    b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-in-error"))
    _frames, gui = await b.quiet(mark, 0.2)
    assert "paint_overlay" not in _actions(gui)


# ---------------------------------------------------------------------------
# Routing of a spoken 'click ...'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word, name", [("seven", "seven"), ("7", "7")])
async def test_ck_1_click_n_while_closed_is_a_by_name_click(
    tmp_path, word, name,
):
    """CK-1: 'click N' with the overlay closed is a by-name click.

    Rule: Logic sends click_element to Input with the spoken text as the
    by-name query; the number is looked up as a control name.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say(f"click {word}")
        click = await b.next_frame("click_element")
        assert click.params["query"].name == name
        assert click.params["query"].role is None
        await b.flush()
        assert _actions(_clicks(b, mark)) == ["click_element"]


@pytest.mark.parametrize("state", ["closed", "walk_in_flight", "painted", "error"])
@pytest.mark.parametrize(
    "spoken, name, role",
    [("save", "save", None), ("seven button", "seven", "Button")],
)
async def test_ck_2_click_text_is_by_name_in_every_state(
    tmp_path, state, spoken, name, role,
):
    """CK-2: 'click <text>' (not a number, or with a role word) is by-name.

    Rule: Logic sends click_element to Input as a by-name query; the overlay
    state does not change the routing.
    """

    async with overlay_boundary(tmp_path) as b:
        if state == "walk_in_flight":
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")
        elif state == "painted":
            await b.show_numbers_painted("snap-1")
        elif state == "error":
            await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        await b.say(f"click {spoken}")
        click = await b.next_frame("click_element")
        assert click.params["query"].name == name
        assert click.params["query"].role == role
        await b.flush()
        assert _actions(_clicks(b, mark)) == ["click_element"]
        assert _notices(b, mark) == []


@pytest.mark.parametrize(
    "spoken, gesture",
    [("click two", None), ("right click two", "right_click"),
     ("double click two", "double_click")],
)
async def test_ck_3_click_n_while_painted_sends_click_snapshot_item(
    tmp_path, spoken, gesture,
):
    """CK-3: 'click N' while painted, badge N exists: click_snapshot_item.

    Rule: Logic sends click_snapshot_item(snapshot_id, item_id) to Input; with
    a spoken gesture ('right click 3') the payload also carries gesture. The
    default gesture is omitted from the payload.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        mark = b.mark()
        await b.say(spoken)
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 2)
        if gesture is None:
            assert "gesture" not in click.params
        else:
            assert click.params["gesture"] == gesture
        await b.flush()
        assert _actions(_clicks(b, mark)) == ["click_snapshot_item"]


async def test_ck_4_click_n_while_painted_without_badge_n_shows_notice(
    tmp_path,
):
    """CK-4: 'click N' while painted and no badge N shows no_badge_numbered.

    Rule: Logic sends a click notice with reason no_badge_numbered to the GUI;
    no click_snapshot_item, no click_element, nothing typed. (The number-absent
    case; an expired snapshot or a missing pin is not reachable from the
    boundary while the overlay is painted.)
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        mark = b.mark()
        await b.say("click nine")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "no_badge_numbered"
        assert notice["snapshot_id"] == "snap-1"
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []
        # The same painted overlay still clicks a number it has.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)


@pytest.mark.parametrize("phase", ["walk_unanswered", "new_list_not_painted"])
async def test_ck_5_click_n_during_refresh_uses_the_visible_snapshot(
    tmp_path, phase,
):
    """CK-5: 'click N' during a refresh resolves against the visible snapshot.

    Rule: Logic sends click_snapshot_item for the visible snapshot's item; a
    miss gives the no_badge_numbered notice carrying the visible snapshot id.
    The visible snapshot is the prior pin when the new list is pinned but not
    yet painted, else the current pin.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        if phase == "new_list_not_painted":
            b.reply(refresh, walk_ok_reply(
                refresh.params, snapshot_id="snap-2", numbers=(1, 2, 3, 4, 5),
            ))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
        await b.flush()
        mark = b.mark()
        # Badge 5 exists only in the not-yet-painted list: a miss.
        await b.say("click five")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "no_badge_numbered"
        assert notice["snapshot_id"] == "snap-1"
        assert _clicks(b, mark) == []
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 2)


@pytest.mark.parametrize("held_in", ["walk_in_flight", "paint_in_flight"])
async def test_ck_7_held_click_n_resolves_once_the_overlay_paints(
    tmp_path, held_in,
):
    """CK-7: a 'click N' held during a walk or paint clicks once painted.

    Rule: 'click N' in walk_in_flight or paint_in_flight, and 200 ms later the
    state is painted with badge N: Logic sends click_snapshot_item for badge
    N, resolved at the end of the 200 ms hold. (The paused state is left out.)
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        if held_in == "walk_in_flight":
            await b.say("click two")
            b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-1"))
            paint = await b.next_gui("paint_overlay")
        else:
            b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-1"))
            paint = await b.next_gui("paint_overlay")
            await b.say("click two")
        # Held: nothing was clicked yet.
        assert _clicks(b, mark) == []
        b.send_gui(paint_ack(
            "painted", *_pair(paint), paint.get("snapshot_id"),
        ))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 2)
        await b.flush()
        assert _actions(_clicks(b, mark)) == ["click_snapshot_item"]
        assert _notices(b, mark) == []


@pytest.mark.parametrize("after_hold", ["still_in_flight", "closed"])
async def test_ck_8_held_click_n_that_cannot_resolve_shows_notice(
    tmp_path, after_hold,
):
    """CK-8: a held 'click N' still unresolvable after 200 ms shows a notice.

    Rule: Logic sends a click notice with reason numbers_not_showing; no
    click, nothing typed. (Still in flight, or closed; paused is left out.)
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await b.say("click two")
        if after_hold == "closed":
            await b.say("hide numbers")
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "numbers_not_showing"
        await b.quiet(mark, _PAST_HOLD_S)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []
        assert walk.params["scope"] == "focused_window"


# ---------------------------------------------------------------------------
# The 200 ms hold: CK-6, CK-9, CK-10, CK-11
# ---------------------------------------------------------------------------
#
# TIME. The hold is a 200 ms ``call_later`` on the Logic loop. The harness
# clock can only move the loop clock as a whole, and the harness's own
# collectors poll on that same clock, so no input can delay the hold alone.
# A test that must land an input inside the hold therefore sends it in real
# time at once, as test_ck_7 does. CK-10 also measures that it did, because a
# late input there would let the test pass without testing the rule.

# The real-time budget for the inputs CK-10 lands inside the hold, measured
# from the end of the held utterance. The hold was armed during that
# utterance, so the budget is short of 200 ms.
_INSIDE_HOLD_BUDGET_S = 0.15


async def _foreground(b, window) -> None:
    """Windows brings ``window`` to the front and reports it to the hook.

    Copied from tests/test_overlay_requirements_rewalk.py (a test module is
    not imported from another).
    """

    b.desktop.bring_to_front(window)
    b.hooks.foreground(window.hwnd)
    await settle()


@pytest.mark.parametrize("walk", ["unanswered", "painted_during_hold"])
async def test_ck_6_click_n_during_refresh_for_another_window_is_held(
    tmp_path, walk,
):
    """CK-6: 'click N' while a focus-change refresh is in flight is held.

    Rule: state refresh_in_flight, and the visible snapshot belongs to a
    window that is no longer the foreground window: Logic holds the click
    under the 200 ms rule instead of sending click_snapshot_item against the
    stale window. (A same-window refresh clicks the visible list at once;
    that is CK-5.)
    """

    # keep_numbers on: with it off a change of window clears and unpins the
    # overlay instead of refreshing it (RW-5).
    config = write_config(tmp_path, overlay_keep_numbers=True)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await _foreground(b, OTHER_WINDOW)
        refresh = await b.next_frame("start_overlay_walk")
        assert _pair(refresh.params) == (sid, gen + 1)
        if walk == "painted_during_hold":
            # The new list is built and sent to the GUI; snap-1 is still the
            # visible list until the GUI acknowledges the new paint.
            b.reply(refresh, walk_ok_reply(
                refresh.params, snapshot_id="snap-2", read_from=OTHER_WINDOW,
            ))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
        await b.flush()
        mark = b.mark()
        await b.say("click two")
        if walk == "unanswered":
            # Nothing paints during the hold: the hold ends in a notice.
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "numbers_not_showing"
            await b.quiet(mark, _PAST_HOLD_S)
            assert _clicks(b, mark) == []
            assert _typed(b, mark) == []
            return
        b.send_gui(paint_ack("painted", *_pair(paint2), "snap-2"))
        # The held number resolves against the list painted during the hold,
        # never against the left window's snap-1.
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-2"
        assert click.params["item_id"] == item_id("snap-2", 2)
        await b.quiet(mark, _PAST_HOLD_S)
        assert [f.params["snapshot_id"] for f in _clicks(b, mark)] == ["snap-2"]
        assert _notices(b, mark) == []


async def test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason(
    tmp_path,
):
    """CK-9: a held 'click N' that the router answers with a notice shows it.

    Rule: held 'click N' and, after 200 ms, the router answers with a notice
    (miss, error, or settling): Logic sends the click notice for that reason
    instead of numbers_not_showing. (The miss case: the list painted during
    the hold has no badge N, so the notice is no_badge_numbered and carries
    the painted snapshot.)
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(
            p, snapshot_id="snap-1", numbers=(1, 2, 3),
        ))
        paint = await b.next_gui("paint_overlay")
        mark = b.mark()
        # Held in paint_in_flight; the paint is acknowledged inside the hold.
        await b.say("click nine")
        b.send_gui(paint_ack("painted", *_pair(paint), "snap-1"))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "no_badge_numbered"
        assert notice["snapshot_id"] == "snap-1"
        assert notice["spoken_name"] == "nine"
        await b.quiet(mark, _PAST_HOLD_S)
        assert len(_notices(b, mark)) == 1
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []
        # The painted list clicks a number it has.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)


async def test_ck_10_hold_across_a_new_paint_generation_clicks_nothing(
    tmp_path,
):
    """CK-10: a held 'click N' whose overlay generation changed clicks nothing.

    Rule: held 'click N' and the overlay session or paint generation changed
    during the 200 ms hold: Logic sends a numbers_not_showing notice and sends
    no click_snapshot_item, even if the new overlay now has badge N.
    """

    # Debounce 0: the foreground event restarts the build at once.
    config = write_config(tmp_path, overlay_focus_debounce_ms=0)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint1 = await b.next_gui("paint_overlay")
        mark = b.mark()
        # Held in paint_in_flight, armed at paint1's pair.
        await b.say("click two")
        held_at = time.monotonic()
        # A foreground event during the first paint restarts the build at a
        # new generation (RW-2), and the new list, which has badge 2, paints.
        await _foreground(b, NUMBERED_WINDOW)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) != _pair(paint1)
        b.reply(walk2, walk_ok_reply(walk2.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        b.send_gui(paint_ack("painted", *_pair(paint2), "snap-2"))
        sent_in = time.monotonic() - held_at
        assert sent_in < _INSIDE_HOLD_BUDGET_S, (
            f"the new paint was acknowledged {sent_in:.3f}s after the held "
            f"utterance, too late to land inside the 200 ms hold; this run "
            f"cannot test CK-10"
        )
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "numbers_not_showing"
        assert notice["snapshot_id"] is None
        await b.quiet(mark, _PAST_HOLD_S)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []
        # The new overlay does have badge 2: a fresh 'click two' clicks it.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-2"
        assert click.params["item_id"] == item_id("snap-2", 2)


@pytest.mark.parametrize("after_hold", ["still_in_flight", "painted"])
async def test_ck_11_second_held_click_n_replaces_the_first(
    tmp_path, after_hold,
):
    """CK-11: a second held 'click N' cancels the first hold and re-arms.

    Rule: a second numeric 'click N' arrives while an earlier held click is
    still waiting in its 200 ms hold: Logic cancels the first hold and arms a
    new one; the first spoken number produces no click and no notice.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        # Both held in walk_in_flight, the second well inside the first's
        # 200 ms.
        await b.say("click one")
        await b.say("click two")
        if after_hold == "still_in_flight":
            # The second hold runs its 200 ms and finds no numbers.
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "numbers_not_showing"
            assert notice["spoken_name"] == "two"
            await b.quiet(mark, _PAST_HOLD_S)
            assert len(_notices(b, mark)) == 1
            assert _clicks(b, mark) == []
            assert _typed(b, mark) == []
            return
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        b.send_gui(paint_ack("painted", *_pair(paint), "snap-1"))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)
        await b.quiet(mark, _PAST_HOLD_S)
        assert [f.params["item_id"] for f in _clicks(b, mark)] == [
            item_id("snap-1", 2),
        ]
        assert _notices(b, mark) == []
        assert _typed(b, mark) == []


async def test_ck_12_click_n_in_error_state_shows_notice(tmp_path):
    """CK-12: 'click N' in the error state shows numbers_not_showing.

    Rule: Logic sends a click notice with reason numbers_not_showing, no
    snapshot id; no click, no by-name search.
    """

    async with overlay_boundary(tmp_path) as b:
        await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        await b.say("click seven")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "numbers_not_showing"
        assert notice["snapshot_id"] is None
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []


# ---------------------------------------------------------------------------
# After a badge click: settle, clear, refresh
# ---------------------------------------------------------------------------


def _settle_config(tmp_path):
    return write_config(
        tmp_path, overlay_settle_after_click=True, overlay_keep_numbers=True,
    )


async def test_ck_13_click_n_while_settling_shows_numbers_updating(tmp_path):
    """CK-13: 'click N' in post_click_settling shows numbers_updating.

    Rule: Logic sends a click notice with reason numbers_updating, no snapshot
    id; no click, no by-name search. (The state exists only with
    overlay_settle_after_click on.)
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        await b.flush()
        mark = b.mark()
        await b.say("click three")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "numbers_updating"
        assert notice["snapshot_id"] is None
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []


async def test_ck_14_settle_clears_at_once_then_reads_and_repaints(tmp_path):
    """CK-14: with settle and keep-numbers on, a badge click clears then re-reads.

    Rule: Logic sends clear_overlay at once, then re-reads the window with a
    walk build; the repainted numbers return on their own.
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        mark = b.mark()
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint1)
        walk = await b.next_frame("start_overlay_walk")
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        # The clear left before the read was sent.
        order = [
            ("gui", m["action"]) for m in b.gui_since(mark)
            if m["action"] in ("clear_overlay", "paint_overlay")
        ]
        assert order[0] == ("gui", "clear_overlay")
        await b.gui_ack("painted", paint2)
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-2", 3)


async def test_ck_15_settling_without_repaint_closes_at_the_deadline(tmp_path):
    """CK-15: post_click_settling with no repaint closes at the settle deadline.

    Rule: Logic unpins the snapshot and closes the overlay with no notice; the
    user must say 'show numbers' again.

    Note: with the shipped bounds the unanswered settle read fails at its own
    10 s reply limit, before the 15.25 s settle deadline, and that failure
    closes the overlay with the same outputs. This test steps past the reply
    limit first, so it pins the read-failure close; the deadline close itself
    cannot be reached at the boundary.
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        await b.flush()
        mark = b.mark()
        # Before the deadline the overlay is still settling: a number is
        # still refused, not looked up by name.
        await b.advance(_BEFORE_SETTLE_DEADLINE_S)
        await b.say("click three")
        still = await b.next_gui("show_click_notice")
        assert still["reason"] == "numbers_updating"
        assert _actions(b.frames_since(mark, {"unpin_snapshot"})) == []
        mark = b.mark()
        # Past the settle read's own 10 s reply limit but short of the
        # settle deadline: the failed read is what closes the overlay, and it
        # closes it with no notice too.
        await b.advance(_PAST_SETTLE_READ_LIMIT_S - _BEFORE_SETTLE_DEADLINE_S)
        unpin = await b.next_frame(
            "unpin_snapshot", where=lambda p: p["snapshot_id"] == "snap-1",
        )
        assert unpin.params["snapshot_id"] == "snap-1"
        await b.flush()
        assert _notices(b, mark) == []
        await b.advance(_PAST_SETTLE_DEADLINE_S - _PAST_SETTLE_READ_LIMIT_S)
        await b.flush()
        assert _notices(b, mark) == []
        # Closed: a number is a control name again.
        await b.say("click two")
        await b.next_frame("click_element")


async def test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins(tmp_path):
    """CK-17: an ok badge click with overlay_keep_numbers off removes the numbers.

    Rule: Logic sends clear_overlay and unpin_snapshot and shows no new
    numbers; no click notice.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        mark = b.mark()
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        await b.quiet(mark, 0.3)
        assert "start_overlay_walk" not in _actions(b.frames_since(mark))
        assert "paint_overlay" not in _actions(b.gui_since(mark))
        assert _notices(b, mark) == []


async def test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place(tmp_path):
    """CK-18: an ok badge click with keep-numbers on and settle off re-walks.

    Rule: Logic starts a re-walk (refresh_in_flight); the old badges stay on
    screen until the new ones paint.
    """

    config = write_config(tmp_path, overlay_keep_numbers=True)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        await b.flush()
        before_paint = []
        for message in b.gui_since(mark):
            if message is paint2:
                break
            before_paint.append(message["action"])
        assert "clear_overlay" not in before_paint


async def test_ck_19_ok_click_shows_no_notice_and_cancels_the_held_click(
    tmp_path, manual_clicks,
):
    """CK-19: an ok click reply shows no notice and cancels a held 'click N'.

    Rule: Logic shows no click notice, and cancels any held 'click N' armed
    against the same overlay generation. The cancelled hold sends no click;
    its words get the numbers_updating notice.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        mark = b.mark()
        # Held in paint_in_flight, at the paint's pair.
        await b.say("click one")
        b.send_gui(paint_ack("painted", *_pair(paint), "snap-1"))
        await b.next_gui("overlay_lease_renew")
        # Painted now, the hold not yet fired: this click resolves at once.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "numbers_updating"
        assert notice["spoken_name"] == "one"
        await b.quiet(mark, _PAST_HOLD_S)
        # Badge 1 was never clicked, and the ok itself showed no notice.
        assert [f.params["item_id"] for f in _clicks(b, mark)] == [
            item_id("snap-1", 2),
        ]
        assert len(_notices(b, mark)) == 1


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous", "execution_failed"])
async def test_ck_20_refused_badge_click_shows_the_reply_as_a_notice(
    tmp_path, manual_clicks, outcome,
):
    """CK-20: a refused click_snapshot_item reply becomes a click notice.

    Rule: Logic sends a click notice carrying the reply's outcome, reason,
    matched names and trace id; the snapshot id falls back to the dispatched
    snapshot.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        b.reply(click, click_reply(
            click.params, outcome=outcome, reason="harness_reason",
            matched_names=("Alpha", "Beta"),
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == outcome
        assert notice["reason"] == "harness_reason"
        assert tuple(notice["matched_names"]) == ("Alpha", "Beta")
        assert notice["trace_id"] == click.params["trace_id"]
        assert notice["snapshot_id"] == "snap-1"
        await b.quiet(mark, 0.2)
        assert len(_notices(b, mark)) == 1
        assert _typed(b, mark) == []


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("bounds_invalid", id="CK-21-bounds_invalid"),
        pytest.param("bounds_stale", id="CK-21-bounds_stale"),
        pytest.param("stale_overlay_generation", id="CK-22"),
    ],
)
async def test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints(
    tmp_path, manual_clicks, reason,
):
    """CK-21 and CK-22: a stale-position or consumed-list refusal re-walks.

    CK-21 rule: a bounds_invalid or bounds_stale refusal, same overlay
    generation, state painted: Logic shows the refusal notice and starts a
    re-walk and repaint so the next 'click N' sees fresh positions.
    CK-22 rule: a stale_overlay_generation refusal: Logic shows the refusal
    notice and starts a re-walk and repaint.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1, mark, click = await _painted_click_in_flight(b)
        sid, gen = _pair(paint1)
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason=reason,
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == reason
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        await b.gui_ack("painted", paint2)
        await b.say("click three")
        click2 = await b.next_frame("click_snapshot_item")
        assert click2.params["item_id"] == item_id("snap-2", 3)


async def test_ck_24_verification_timeout_refusal_starts_no_rewalk(
    tmp_path, manual_clicks,
):
    """CK-24: a verification_timeout refusal shows the notice and no re-walk.

    Rule: Logic shows the refusal notice and starts no re-walk, so a slow
    application is not hit with more UIA load. (Only verification_timeout is
    tested: the disabled, COM and item_not_found part of the row is stated
    only in a code comment.)
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        b.reply(click, click_reply(
            click.params, outcome="execution_failed",
            reason="verification_timeout",
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "verification_timeout"
        await b.quiet(mark, 0.3)
        assert "start_overlay_walk" not in _actions(b.frames_since(mark))
        assert "paint_overlay" not in _actions(b.gui_since(mark))


@pytest.mark.parametrize(
    "reason", ["disabled", "invoke_com_error", "item_not_found"],
)
async def test_ck_24_other_refusals_start_no_rewalk(
    tmp_path, manual_clicks, reason,
):
    """CK-24 (other reasons): disabled, COM and item_not_found refusals.

    Rule: a click_snapshot_item refusal with reason disabled, an Invoke or COM
    failure, or item_not_found shows the refusal notice and starts no
    re-walk: a re-walk finds the same control in the same place, so it cannot
    change the outcome.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason=reason,
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == reason
        await b.quiet(mark, 0.3)
        assert "start_overlay_walk" not in _actions(b.frames_since(mark))
        assert "paint_overlay" not in _actions(b.gui_since(mark))


async def test_ck_25_unanswered_badge_click_shows_timeout_notice(
    tmp_path, manual_clicks,
):
    """CK-25: no click_snapshot_item reply within response_timeout_ms.

    Rule: Logic sends a click notice with reason timeout (execution_failed);
    nothing is typed.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, _click = await _painted_click_in_flight(b)
        await b.quiet(mark, 0.1)
        assert _notices(b, mark) == []
        await b.advance(_PAST_CLICK_TIMEOUT_S)
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "timeout"
        assert notice["snapshot_id"] == "snap-1"
        await b.flush()
        assert _typed(b, mark) == []


async def test_ck_27_late_refusal_corrects_the_timeout_notice(
    tmp_path, manual_clicks,
):
    """CK-27: a late refusal inside the grace window replaces the timeout text.

    Rule: after a timeout notice, the true reply arrives within the 15 s grace
    window and refuses the click, and no newer click notice was attempted or
    shown: Logic sends a corrected click notice with the true refusal reason.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        await b.advance(_PAST_CLICK_TIMEOUT_S)
        timeout = await b.next_gui("show_click_notice")
        assert timeout["reason"] == "timeout"
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason="disabled",
        ))
        corrected = await b.next_gui("show_click_notice")
        assert corrected["reason"] == "disabled"
        assert corrected["trace_id"] == click.params["trace_id"]
        assert corrected["snapshot_id"] == "snap-1"


async def test_ck_29_late_ok_reply_shows_no_notice(tmp_path, manual_clicks):
    """CK-29 (notice half): a late ok reply after the timeout shows no notice.

    Rule: Logic sends no notice. (The bookkeeping half of the row, the held
    click and the post-click clear, is stated only in code and is not tested.)
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        await b.advance(_PAST_CLICK_TIMEOUT_S)
        timeout = await b.next_gui("show_click_notice")
        assert timeout["reason"] == "timeout"
        after_timeout = b.mark()
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        await b.quiet(after_timeout, 0.3)
        assert _notices(b, after_timeout) == []
        assert len(_notices(b, mark)) == 1


async def test_ck_29_late_ok_reply_clears_like_an_on_time_ok(
    tmp_path, manual_clicks,
):
    """CK-29 (bookkeeping half): a late ok reply removes the numbers.

    Rule: after the timeout notice, a late ok reply to a badge click makes
    Logic apply the same post-click step as an on-time ok. With
    overlay_keep_numbers off (CK-17) that is clear_overlay and unpin_snapshot.
    """

    async with overlay_boundary(tmp_path) as b:
        paint, mark, click = await _painted_click_in_flight(b)
        await b.advance(_PAST_CLICK_TIMEOUT_S)
        timeout = await b.next_gui("show_click_notice")
        assert timeout["reason"] == "timeout"
        await b.flush()
        assert "clear_overlay" not in _actions(b.gui_since(mark))
        assert "unpin_snapshot" not in _actions(b.frames_since(mark))
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"


async def test_ck_31_second_badge_click_while_first_in_flight_is_refused(
    tmp_path, manual_clicks,
):
    """CK-31: a badge click while an earlier one awaits its reply is refused.

    Rule: Logic sends a click notice with reason overlay_click_in_flight and
    sends no second click_snapshot_item.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b, "two")
        await b.say("click three")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "overlay_click_in_flight"
        await b.quiet(mark, 0.2)
        assert [f.params["item_id"] for f in _clicks(b, mark)] == [
            item_id("snap-1", 2),
        ]
        assert click.params["item_id"] == item_id("snap-1", 2)


async def test_ck_32_badge_click_carries_the_visible_pair(tmp_path):
    """CK-32 (known pair): the click carries the visible list's pair.

    Rule: the click_snapshot_item command carries overlay_session_id and
    paint_generation of the visible list.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert _pair(click.params) == _pair(paint)


async def test_ck_32_badge_click_omits_an_unknown_pair(tmp_path, manual_clicks):
    """CK-32 (unknown pair): with no painted list, both fields are omitted.

    Rule: overlay_session_id and paint_generation are omitted when unknown. A
    by-name reply caches a snapshot that was never painted, and a GUI badge
    click on it has no visible pair.
    """

    async with overlay_boundary(tmp_path) as b:
        say = b.say_soon("click save")
        by_name = await b.next_frame("click_element")
        b.reply(by_name, click_reply(
            by_name.params, snapshot_id="snap-byname",
            snapshot_summary=summary("snap-byname", 1, 2),
            matched_name="save",
        ))
        await say
        b.send_gui(snapshot_item_clicked("snap-byname", 2))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-byname", 2)
        assert "overlay_session_id" not in click.params
        assert "paint_generation" not in click.params


# ---------------------------------------------------------------------------
# The renumber guard
# ---------------------------------------------------------------------------


async def _refresh_painted(b, renamed: dict) -> dict:
    """Painted snap-1, then a re-said 'show numbers' repaints snap-2."""

    await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
    await b.say("show numbers")
    walk = await b.next_frame("start_overlay_walk")
    b.reply(walk, renamed_walk_reply(
        walk.params, snapshot_id="snap-2", renamed=renamed,
    ))
    paint2 = await b.next_gui("paint_overlay")
    assert paint2["snapshot_id"] == "snap-2"
    await b.gui_ack("painted", paint2)
    await b.flush()
    return paint2


async def test_ck_34_click_n_after_a_renumbering_repaint_is_refused(tmp_path):
    """CK-34: 'click N' within 3 s of a repaint that changed badge N is refused.

    Rule: Logic sends a click notice with reason overlay_numbers_changed and
    sends no click_snapshot_item.
    """

    async with overlay_boundary(tmp_path) as b:
        await _refresh_painted(b, renamed={2: "a different control"})
        mark = b.mark()
        await b.say("click two")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "overlay_numbers_changed"
        assert notice["snapshot_id"] == "snap-2"
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []


async def test_ck_35_click_n_after_a_repaint_that_kept_badge_n_clicks(tmp_path):
    """CK-35: 'click N' within 3 s of a repaint that kept badge N clicks.

    Rule: badge N has the same name, role and bounds before and after the
    swap: Logic sends click_snapshot_item as usual.
    """

    async with overlay_boundary(tmp_path) as b:
        await _refresh_painted(b, renamed={2: "a different control"})
        mark = b.mark()
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-2"
        assert click.params["item_id"] == item_id("snap-2", 3)
        assert _notices(b, mark) == []


async def test_ck_35_click_n_new_after_a_repaint_clicks(tmp_path):
    """CK-35 (new number): a number the old list did not have clicks.

    Rule: N is new after the swap: Logic sends click_snapshot_item as usual.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2))
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        b.reply(walk, walk_ok_reply(
            walk.params, snapshot_id="snap-2", numbers=(1, 2, 3),
        ))
        paint2 = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint2)
        mark = b.mark()
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-2", 3)
        assert _notices(b, mark) == []


# ---------------------------------------------------------------------------
# A badge clicked with the mouse (GUI message)
# ---------------------------------------------------------------------------


async def test_ck_37_gui_badge_click_sends_click_snapshot_item(
    tmp_path, manual_clicks,
):
    """CK-37: GUI snapshot_item_clicked for a cached number clicks it.

    Rule: Logic sends click_snapshot_item to Input with the default gesture;
    failure notices follow the same rules as a voice badge click.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        mark = b.mark()
        b.send_gui(snapshot_item_clicked("snap-1", 3))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 3)
        assert "gesture" not in click.params
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason="disabled",
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "disabled"
        assert notice["snapshot_id"] == "snap-1"
        assert notice["trace_id"] == click.params["trace_id"]
        await b.flush()
        assert len(_clicks(b, mark)) == 1


# ---------------------------------------------------------------------------
# Clicking turned off, and the safety word
# ---------------------------------------------------------------------------


async def test_ck_41_click_disabled_sends_no_click_and_types_or_notices(
    tmp_path,
):
    """CK-41: with click.enabled false, no click is sent for any 'click ...'.

    Rule: Logic sends no click_element and no overlay click, even for 'click
    N'; with the safety word it shows disabled_by_config, without it the words
    are typed. (The once-per-session half is the next test.)
    """

    config = write_config(tmp_path, enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        mark = b.mark()
        b.say_soon("click save")
        typed = await _next_typed(b)
        assert "click" in typed.lower() and "save" in typed.lower()
        b.say_soon("click three")
        typed = await _next_typed(b)
        assert "three" in typed.lower() or "3" in typed
        await b.say("x-ray click save")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "disabled_by_config"
        await b.quiet(mark, 0.3)
        assert len(_typed(b, mark)) == 2
        assert _clicks(b, mark) == []


async def test_ck_41_disabled_notice_shows_once_per_session(tmp_path):
    """CK-41 (once per session): the disabled_by_config notice shows once.

    Rule: with the safety word Logic shows disabled_by_config once per
    session.
    """

    config = write_config(tmp_path, enabled=False)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        mark = b.mark()
        await b.say("x-ray click save")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "disabled_by_config"
        await b.say("x-ray click open")
        await b.quiet(mark, 0.3)
        notices = _notices(b, mark)
        assert {n["reason"] for n in notices} == {"disabled_by_config"}
        assert len(notices) == 1


@pytest.mark.parametrize(
    "outcome, reason",
    [("not_found", None), ("execution_failed", "walk_deadline_exceeded")],
)
async def test_ck_42_by_name_miss_without_safety_word_types_the_words(
    tmp_path, manual_clicks, outcome, reason,
):
    """CK-42: a by-name miss without the safety word types the utterance.

    Rule: the whole utterance is typed as dictation and no click notice is
    shown.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        b.say_soon("click save")
        click = await b.next_frame("click_element")
        b.reply(click, click_reply(click.params, outcome=outcome, reason=reason))
        typed = await _next_typed(b)
        assert "click" in typed.lower() and "save" in typed.lower()
        await b.quiet(mark, 0.2)
        assert _notices(b, mark) == []


@pytest.mark.parametrize(
    "outcome, reason",
    [("not_found", None), ("execution_failed", "walk_deadline_exceeded")],
)
async def test_ck_43_by_name_miss_with_safety_word_shows_the_notice(
    tmp_path, manual_clicks, outcome, reason,
):
    """CK-43: a by-name miss with the safety word shows the click notice.

    Rule: Logic sends the click notice and nothing is typed.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        say = b.say_soon("x-ray click save")
        click = await b.next_frame("click_element")
        b.reply(click, click_reply(click.params, outcome=outcome, reason=reason))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == outcome
        assert notice["reason"] == reason
        await say
        await b.quiet(mark, 0.3)
        assert _typed(b, mark) == []


# ---------------------------------------------------------------------------
# Number words, and the mouse grid's claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word, number",
    [("to", 2), ("too", 2), ("for", 4), ("two", 2), ("4", 4), ("twelve", 12)],
)
async def test_ck_47_number_words_and_homophones_route_as_numbers(
    tmp_path, word, number,
):
    """CK-47: 'click' + a number word, digit or homophone is a numbered click.

    Rule: 'to', 'too' and 'for' read as 2, 2 and 4, and any 1..999 number word
    or digit as its number, routed as a numbered click.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3, 4, 12))
        mark = b.mark()
        await b.say(f"click {word}")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", number)
        assert "click_element" not in _actions(_clicks(b, mark))


@pytest.mark.parametrize("word", ["first", "1000"])
async def test_ck_47_ordinals_and_out_of_range_numbers_are_by_name(
    tmp_path, word,
):
    """CK-47 (by-name half): ordinals and out-of-range numbers are by-name.

    Rule: ordinals and numbers outside 1..999 are by-name queries, even while
    the overlay is painted.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        mark = b.mark()
        await b.say(f"click {word}")
        click = await b.next_frame("click_element")
        assert click.params["query"].name == word
        await b.flush()
        assert "click_snapshot_item" not in _actions(_clicks(b, mark))


async def test_ck_48_bare_number_while_grid_open_belongs_to_the_grid(tmp_path):
    """CK-48: 'click' + a bare number 1..9 while the mouse grid is open.

    Rule: the grid consumes the number (refine or act); no overlay routing, no
    by-name search, no dictation.
    """

    from services.wheelhouse.shared.mouse_action import MouseActionResponse

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        await b.say("show grid")
        await b.flush()
        assert any(
            "grid" in str(m.get("action", "")) for m in b.gui
            if isinstance(m, dict)
        ), "the grid did not open"
        mark = b.mark()
        say = b.say_soon("click three")
        # Positive output: the grid acts on the number with a pointer click.
        point = await b.next_frame("click_point")
        b.reply(point, MouseActionResponse(
            status="ok", outcome="ok", reason=None,
            trace_id=point.params.get("trace_id", ""),
        ).to_dict())
        await asyncio.wait_for(say, 10.0)
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        assert _typed(b, mark) == []


# ---------------------------------------------------------------------------
# Round 3: settling, re-walk refusals, GUI badge clicks, failed clicks
# ---------------------------------------------------------------------------

# The words the speech processor shows when 'click' has no usable target
# (speech/actions.py click_element, the unparseable-target StepFailed).
_NO_TARGET_NOTICE = "Say the name or number of what to click."

# A Chromium-family window: the first-use screen-reader hint (CK-49) is shown
# only for a click into a process on the [click] browser_processes list.
BROWSER_WINDOW = harness_module.Window(
    hwnd=0x6C01, pid=6301, process_name="chrome.exe",
    class_name="Chrome_WidgetWin_1", create_time_s=1_700_000_200.0,
)

# A spoken target too large for one shared-memory frame (app.py
# _DEFAULT_SHARED_MEMORY_BYTES, 64 KiB, the size the harness also gives its
# command buffer), so the click request fails to send.
_OVERSIZED_TARGET = "x" * (70 * 1024)


def _spoken(text: str, safety_word: bool) -> str:
    return f"x-ray {text}" if safety_word else text


async def test_ck_16_show_numbers_while_settling_walks_fresh_at_once(tmp_path):
    """CK-16: 'show numbers' in post_click_settling walks fresh at once.

    Rule: Logic unpins the old snapshot and starts a fresh walk
    (start_overlay_walk) at once instead of waiting for the settle read.
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        settle_read = await b.next_frame("start_overlay_walk")
        assert settle_read.params.get("settle") is True
        await b.flush()
        # The settle read is never answered.
        await b.say("show numbers")
        unpin = await b.next_frame(
            "unpin_snapshot", timeout_s=1.0,
            where=lambda p: p["snapshot_id"] == "snap-1",
        )
        assert unpin.params["snapshot_id"] == "snap-1"
        fresh = await b.next_frame("start_overlay_walk", timeout_s=1.0)
        assert "settle" not in fresh.params
        assert fresh.params["overlay_session_id"] == paint1["overlay_session_id"]
        assert (
            fresh.params["paint_generation"]
            > settle_read.params["paint_generation"]
        )
        b.reply(fresh, walk_ok_reply(fresh.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        await b.gui_ack("painted", paint2)
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-2", 3)


@pytest.mark.parametrize(
    "reason", ["target_moved_offscreen", "popup_closed", "taskbar_closed"],
)
async def test_ck_23_moved_or_closed_target_refusal_rewalks_and_repaints(
    tmp_path, manual_clicks, reason,
):
    """CK-23: a target_moved_offscreen, popup_closed or taskbar_closed refusal.

    Rule: Logic shows the refusal notice and starts a re-walk and repaint.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1, _mark, click = await _painted_click_in_flight(b)
        sid, gen = _pair(paint1)
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason=reason,
        ))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == reason
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        await b.gui_ack("painted", paint2)
        await b.say("click three")
        click2 = await b.next_frame("click_snapshot_item")
        assert click2.params["item_id"] == item_id("snap-2", 3)


@pytest.mark.parametrize(
    "state",
    ["paint_in_flight", "refresh_in_flight", "post_click_settling", "error"],
)
async def test_ck_40_gui_badge_click_resolves_in_any_state(
    tmp_path, manual_clicks, state,
):
    """CK-40: a GUI badge click resolves against its named snapshot in any state.

    Rule: Logic resolves snapshot_item_clicked against the named snapshot
    only; the state router and the hold rule do not apply. (Painted is
    CK-37. A spoken 'click N' in these states is held or refused instead.)
    """

    config = (
        _settle_config(tmp_path) if state == "post_click_settling" else None
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        named, number = "snap-1", 3
        if state == "paint_in_flight":
            await b.say("show numbers")
            await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
            await b.next_gui("paint_overlay")
        elif state == "refresh_in_flight":
            await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
            await b.say("show numbers")
            refresh = await b.next_frame("start_overlay_walk")
            # The new list is pinned and sent to the GUI but not painted: the
            # badges on screen are still snap-1.
            b.reply(refresh, walk_ok_reply(
                refresh.params, snapshot_id="snap-2", numbers=(1, 2, 3, 4),
            ))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
        elif state == "post_click_settling":
            await b.show_numbers_painted("snap-1")
            await b.say("click two")
            first = await b.next_frame("click_snapshot_item")
            b.reply(first, click_reply(first.params, matched_name="control 2"))
            await b.next_gui("clear_overlay")
            await b.next_frame("start_overlay_walk")
        else:
            await _enter_error_state(b)
            say = b.say_soon("click save")
            by_name = await b.next_frame("click_element")
            b.reply(by_name, click_reply(
                by_name.params, snapshot_id="snap-byname",
                snapshot_summary=summary("snap-byname", 1, 2, 3),
                matched_name="save",
            ))
            await say
            named = "snap-byname"
        await b.flush()
        mark = b.mark()
        b.send_gui(snapshot_item_clicked(named, number))
        click = await b.next_frame("click_snapshot_item", timeout_s=1.0)
        assert click.params["snapshot_id"] == named
        assert click.params["item_id"] == item_id(named, number)
        await b.quiet(mark, _PAST_HOLD_S)
        assert len(_clicks(b, mark)) == 1
        assert _notices(b, mark) == []


async def test_ck_40_gui_badge_click_skips_the_renumber_guard(tmp_path):
    """CK-40 (renumber guard): a GUI badge click after a renumbering repaint.

    Rule: the renumber guard does not apply to a GUI badge click: Logic sends
    click_snapshot_item for the badge clicked, where a spoken 'click N' for
    the same badge is refused (CK-34).
    """

    async with overlay_boundary(tmp_path) as b:
        await _refresh_painted(b, renamed={2: "a different control"})
        mark = b.mark()
        b.send_gui(snapshot_item_clicked("snap-2", 2))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-2"
        assert click.params["item_id"] == item_id("snap-2", 2)
        await b.quiet(mark, 0.2)
        assert _notices(b, mark) == []


async def test_ck_40_gui_badge_click_is_refused_while_a_click_is_in_flight(
    tmp_path, manual_clicks,
):
    """CK-40 (in-flight guard): the in-flight guard applies to a GUI click.

    Rule: a GUI badge click while an earlier badge click awaits its reply is
    refused with overlay_click_in_flight; no second click_snapshot_item.
    """

    async with overlay_boundary(tmp_path) as b:
        _paint, mark, click = await _painted_click_in_flight(b, "two")
        b.send_gui(snapshot_item_clicked("snap-1", 3))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "overlay_click_in_flight"
        await b.quiet(mark, 0.2)
        assert [f.params["item_id"] for f in _clicks(b, mark)] == [
            item_id("snap-1", 2),
        ]
        assert click.params["item_id"] == item_id("snap-1", 2)


@pytest.mark.parametrize("safety_word", [False, True], ids=["plain", "x-ray"])
@pytest.mark.parametrize(
    "failure", ["timeout", "send_request_failed", "malformed_response"],
)
async def test_ck_44_failed_by_name_click_shows_notice_and_types_nothing(
    tmp_path, manual_clicks, failure, safety_word,
):
    """CK-44: a by-name click that times out, fails to send, or is malformed.

    Rule: with or without the safety word, Logic sends the timeout,
    send_request_failed or malformed_response notice and nothing is typed,
    because the click may still run.

    The send failure is a spoken target larger than one shared-memory frame.
    Typing those same words would fail to send too, so in that case the
    "nothing typed" check is weak; the notice check carries it.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        target = _OVERSIZED_TARGET if failure == "send_request_failed" else "save"
        say = b.say_soon(_spoken(f"click {target}", safety_word))
        if failure == "timeout":
            await b.next_frame("click_element")
            await b.advance(_PAST_CLICK_TIMEOUT_S)
        elif failure == "malformed_response":
            click = await b.next_frame("click_element")
            b.reply(click, {"status": "ok", "outcome": "no_such_outcome"})
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == failure
        await asyncio.wait_for(say, 5.0)
        await b.quiet(mark, 0.3)
        assert _typed(b, mark) == []
        assert len(_notices(b, mark)) == 1


async def _router_notice_setup(b, reason: str) -> str:
    """Bring the overlay to where 'click N' gets ``reason``; return the N."""

    if reason == "no_badge_numbered":
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        return "nine"
    if reason == "numbers_not_showing":
        await _enter_error_state(b)
        return "seven"
    if reason == "numbers_updating":
        await b.show_numbers_painted("snap-1")
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        return "three"
    await _refresh_painted(b, renamed={2: "a different control"})
    return "two"


@pytest.mark.parametrize("safety_word", [False, True], ids=["plain", "x-ray"])
@pytest.mark.parametrize(
    "reason",
    ["no_badge_numbered", "numbers_not_showing", "numbers_updating",
     "overlay_numbers_changed"],
)
async def test_ck_45_click_n_notice_types_nothing(tmp_path, reason, safety_word):
    """CK-45: 'click N' answered by a router or renumber notice types nothing.

    Rule: with or without the safety word, Logic sends the notice and nothing
    is typed as dictation; the words 'click N' never reach the document.
    """

    config = (
        _settle_config(tmp_path) if reason == "numbers_updating" else None
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        number = await _router_notice_setup(b, reason)
        await b.flush()
        mark = b.mark()
        say = b.say_soon(_spoken(f"click {number}", safety_word))
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == reason
        await asyncio.wait_for(say, 5.0)
        await b.quiet(mark, 0.3)
        assert _typed(b, mark) == []
        assert _clicks(b, mark) == []
        assert len(_notices(b, mark)) == 1


@pytest.mark.parametrize("target", ["the", "."])
async def test_ck_46_unusable_target_without_safety_word_is_typed(
    tmp_path, target,
):
    """CK-46 (no safety word): 'click' with an unusable target is typed.

    Rule: an empty or article-only target, without the safety word: the words
    are typed as dictation; no click and no notice.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        b.say_soon(f"click {target}")
        typed = await _next_typed(b)
        assert typed == f"click {target}"
        await b.quiet(mark, 0.3)
        assert _typed(b, mark) == [f"click {target}"]
        assert _clicks(b, mark) == []
        assert _notices(b, mark) == []
        assert b.gui_since(mark, actions={"show_notification"}) == []


@pytest.mark.parametrize("target", ["the", "."])
async def test_ck_46_unusable_target_with_safety_word_shows_notice(
    tmp_path, target,
):
    """CK-46 (safety word): 'x-ray click' with an unusable target notices.

    Rule: with the safety word a notice 'Say the name or number of what to
    click.' shows and nothing is typed; no click is sent.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say(f"x-ray click {target}")
        shown = await b.next_gui("show_notification")
        assert shown["message"] == _NO_TARGET_NOTICE
        await b.quiet(mark, 0.3)
        assert _typed(b, mark) == []
        assert _clicks(b, mark) == []


async def test_ck_49_badge_click_skips_the_first_use_browser_hint(tmp_path):
    """CK-49: a 'click N' routed to a badge click shows no first-use hint.

    Rule: Logic skips the first-use screen-reader hint for a badge click;
    only by-name clicks can show it. (A by-name click into the same browser
    window afterwards shows it, so the hint is reachable here.)
    """

    async with overlay_boundary(
        tmp_path, windows=(BROWSER_WINDOW, OTHER_WINDOW),
    ) as b:
        await b.show_numbers_painted("snap-1", read_from=BROWSER_WINDOW)
        mark = b.mark()
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)
        await b.quiet(mark, 0.3)
        assert b.gui_since(mark, actions={"click_first_use_hint"}) == []
        # The badge click closed the numbers; a by-name click into the same
        # browser window shows the hint.
        await b.say("click save")
        await b.next_frame("click_element")
        hint = await b.next_gui("click_first_use_hint")
        assert hint["message"]
        assert _notices(b, mark) == []


async def test_ck_50_settle_walk_names_the_clicked_snapshot(tmp_path):
    """CK-50: the settle walk names the snapshot that the click used.

    Rule: after a badge click with overlay_settle_after_click on, the
    start_overlay_walk that re-reads the window carries settle true and
    compare_snapshot_id equal to the snapshot of the clicked badge. A
    'show numbers' walk carries neither field.
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.say("show numbers")
        first = await b.next_frame("start_overlay_walk")
        assert "settle" not in first.params
        assert "compare_snapshot_id" not in first.params
        b.reply(first, walk_ok_reply(first.params, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint)
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        settle_read = await b.next_frame("start_overlay_walk")
        assert settle_read.params.get("settle") is True
        assert settle_read.params.get("compare_snapshot_id") == "snap-1"


async def test_ck_51_unanswered_badge_click_raises_one_error_notification(
    tmp_path, manual_clicks,
):
    """CK-51: one unanswered badge click raises one Windows error notification.

    Rule: when click_snapshot_item gets no reply, the request layer logs the
    fault at ERROR once, and Logic logs its own line about the timeout below
    ERROR, so the user sees one error notification, not two
    (wh-duplicate-error-popup).
    """

    async with overlay_boundary(tmp_path) as b:
        await _painted_click_in_flight(b)
        before = len(b.notifications)
        await b.advance(_PAST_CLICK_TIMEOUT_S)
        await b.next_gui("show_click_notice")
        await b.flush()
        assert len(b.notifications) - before == 1


# The WinEvent id of a menu closing (EVENT_SYSTEM_MENUPOPUPEND).
_MENUPOPUPEND = 0x0007


@pytest.mark.parametrize(
    "settle_after_click", [False, True], ids=["settle-off", "settle-on"],
)
async def test_ck_52_ok_click_with_keep_numbers_off_clears_after_a_refresh_started(
    tmp_path, manual_clicks, settle_after_click,
):
    """CK-52: keep-numbers off, a refresh between the click and its ok reply.

    Rule: with overlay_keep_numbers off, an ok reply to a badge click in the
    overlay session the click was sent in makes Logic send clear_overlay and
    unpin the clicked snapshot at once, even when a refresh started (here a
    menu closing) between the click and the reply. The refresh walk's reply
    then paints nothing. This holds with overlay_settle_after_click on too.
    """

    config = write_config(
        tmp_path, overlay_keep_numbers=False,
        overlay_settle_after_click=settle_after_click,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint, mark, click = await _painted_click_in_flight(b)
        sid, gen = _pair(paint)
        b.hooks.menu_popup(_MENUPOPUPEND)
        await settle()
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == sid
        unpin = await b.next_frame(
            "unpin_snapshot", where=lambda p: p["snapshot_id"] == "snap-1",
        )
        assert unpin.params["snapshot_id"] == "snap-1"
        # The refresh walk answers late: nothing is painted.
        after = b.mark()
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        # The injected clock passes the 0.3 s window, then flush collects
        # every message that Logic sent in it (no real wait).
        await b.advance(0.3)
        await b.flush()
        assert "paint_overlay" not in _actions(b.gui_since(after))
        assert _notices(b, mark) == []
        # Closed: a number is a control name again. The by-name click waits
        # for its reply, so the test answers it (no 3 s request timeout).
        spoken = b.say_soon("click two")
        by_name = await b.next_frame("click_element")
        b.reply(by_name, click_reply(by_name.params, matched_name="two"))
        await asyncio.wait_for(spoken, 1.0)


@pytest.mark.parametrize("refresh_ends", ["paint", "fallback"])
async def test_ck_53_ok_click_during_a_refresh_rewalks_once_the_refresh_ends(
    tmp_path, manual_clicks, refresh_ends,
):
    """CK-53: keep-numbers on, an ok reply lands while a refresh is in flight.

    Rule: with overlay_keep_numbers on and overlay_settle_after_click off, a
    badge click sent during a refresh whose ok reply arrives while that
    refresh is still in flight makes Logic start the post-click re-walk
    (start_overlay_walk at the next paint generation) once the refresh ends:
    after its new list is painted, or after it fails and the old list stays.
    Nothing is walked before the refresh ends.
    """

    config = write_config(tmp_path, overlay_keep_numbers=True)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        assert _pair(refresh.params) == (sid, gen + 1)
        mark = b.mark()
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        await b.quiet(mark, 0.2)
        assert "start_overlay_walk" not in _actions(b.frames_since(mark))
        if refresh_ends == "paint":
            b.reply(refresh, walk_ok_reply(refresh.params, snapshot_id="snap-2"))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
            await b.gui_ack("painted", paint2)
        else:
            b.reply(refresh, walk_failed_reply(refresh.params, "execution_failed"))
        rewalk = await b.next_frame("start_overlay_walk")
        assert _pair(rewalk.params) == (sid, gen + 2)
        b.reply(rewalk, walk_ok_reply(rewalk.params, snapshot_id="snap-3"))
        paint3 = await b.next_gui("paint_overlay")
        assert paint3["snapshot_id"] == "snap-3"
        assert _notices(b, mark) == []


@pytest.mark.parametrize("first_reply", ["ok", "refused", "timeout"])
async def test_ck_54_next_badge_click_is_sent_after_the_first_is_answered(
    tmp_path, manual_clicks, first_reply,
):
    """CK-54: a badge click's reply or timeout ends the in-flight refusal.

    Rule: after a badge click gets its reply (ok or a refusal) or its
    response_timeout_ms passes, the next 'click N' that resolves to a badge
    is sent as click_snapshot_item; it is not refused with
    overlay_click_in_flight (CK-31). (overlay_keep_numbers is on so the
    numbers stay after an ok click.)
    """

    config = write_config(tmp_path, overlay_keep_numbers=True)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        _paint, mark, click = await _painted_click_in_flight(b)
        if first_reply == "ok":
            b.reply(click, click_reply(click.params, matched_name="control 2"))
            # The post-click refresh starts; the old list stays visible.
            await b.next_frame("start_overlay_walk")
        elif first_reply == "refused":
            b.reply(click, click_reply(
                click.params, outcome="execution_failed", reason="disabled",
            ))
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "disabled"
        else:
            await b.advance(_PAST_CLICK_TIMEOUT_S)
            notice = await b.next_gui("show_click_notice")
            assert notice["reason"] == "timeout"
        await b.flush()
        await b.say("click three")
        second = await b.next_frame("click_snapshot_item")
        assert second.params["item_id"] == item_id("snap-1", 3)
        await b.flush()
        assert "overlay_click_in_flight" not in [
            n["reason"] for n in _notices(b, mark)
        ]


# The keepalive interval (main.py class default 15 s): one lease renew each.
_KEEPALIVE_INTERVAL_S = 15.0


@pytest.mark.parametrize(
    "refresh", ["failed", "in_flight", "painted"],
)
async def test_ck_55_badge_click_and_lease_renew_carry_the_list_on_screen(
    tmp_path, manual_clicks, refresh,
):
    """CK-55: the pair a badge click and a lease renew carry follows the paint.

    Rule: after a refresh fails and the old badges stay, a spoken or GUI
    badge click and the next overlay_lease_renew carry the pair of the paint
    still on screen, not the refresh's newer generation. While a refresh is
    in flight, a badge click carries the pair of the list on screen. After a
    refresh paints its new list, both carry the new paint's pair.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        expected = (sid, gen)
        snapshot = "snap-1"
        if refresh == "failed":
            b.reply(walk, walk_failed_reply(walk.params, "execution_failed"))
            await settle()
        elif refresh == "painted":
            b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
            paint2 = await b.next_gui("paint_overlay")
            await b.gui_ack("painted", paint2)
            expected = _pair(paint2)
            assert expected == (sid, gen + 1)
            snapshot = "snap-2"
        await b.flush()
        mark = b.mark()
        if refresh != "in_flight":
            await b.advance(_KEEPALIVE_INTERVAL_S)
            await b.next_gui("overlay_lease_renew")
            await b.flush()
            renews = b.gui_since(mark, actions={"overlay_lease_renew"})
            assert renews
            assert [_pair(r) for r in renews] == [expected] * len(renews)
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == snapshot
        assert _pair(click.params) == expected
        if refresh == "in_flight":
            return
        # A refusal that starts no re-walk keeps the list on screen; a GUI
        # badge click then carries the same pair.
        b.reply(click, click_reply(
            click.params, outcome="execution_failed", reason="disabled",
        ))
        await b.next_gui("show_click_notice")
        b.send_gui(snapshot_item_clicked(snapshot, 3))
        gui_click = await b.next_frame("click_snapshot_item")
        assert gui_click.params["item_id"] == item_id(snapshot, 3)
        assert _pair(gui_click.params) == expected


# Longer than the 3 s renumber grace window (_OVERLAY_RENUMBER_GRACE_SECONDS).
_PAST_RENUMBER_WINDOW_S = 3.2


async def test_ck_56_click_n_after_the_renumber_window_clicks(tmp_path):
    """CK-56: 'click N' more than 3 s after a renumbering repaint clicks.

    Rule: badge N changed across a repaint (CK-34), and the 'click N' comes
    more than 3 s after that repaint: Logic sends click_snapshot_item for
    badge N of the new list and shows no notice.
    """

    async with overlay_boundary(tmp_path) as b:
        await _refresh_painted(b, renamed={2: "a different control"})
        await b.advance(_PAST_RENUMBER_WINDOW_S)
        await b.flush()
        mark = b.mark()
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-2"
        assert click.params["item_id"] == item_id("snap-2", 2)
        await b.flush()
        assert _notices(b, mark) == []


async def _repaint(b, snapshot_id: str, renamed: dict) -> dict:
    """A re-said 'show numbers' repaints ``snapshot_id`` (badges 1..3)."""

    await b.say("show numbers")
    walk = await b.next_frame("start_overlay_walk")
    b.reply(walk, renamed_walk_reply(
        walk.params, snapshot_id=snapshot_id, renamed=renamed,
    ))
    paint = await b.next_gui("paint_overlay")
    assert paint["snapshot_id"] == snapshot_id
    await b.gui_ack("painted", paint)
    await b.flush()
    return paint


@pytest.mark.parametrize("chain", ["middle_list", "later_swap_window"])
async def test_ck_57_renumbering_anywhere_in_a_repaint_chain_is_refused(
    tmp_path, chain,
):
    """CK-57: a chain of repaints: each swap is checked in its own window.

    Rule: 'click N' is refused with overlay_numbers_changed, and no
    click_snapshot_item is sent, when ANY repaint whose 3 s window is still
    open changed badge N (wh-overlay-slow-uia-stale-badges.4). Here badge 2
    is renamed by a first repaint and named back by a second one, so the
    list before the chain and the list after it agree on badge 2 and only
    the middle list differs. Each swap's 3 s window runs from that swap:
    after the first window ends, the second swap still refuses.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        await _repaint(b, "snap-2", renamed={2: "a different control"})
        if chain == "later_swap_window":
            await b.advance(2.0)
        await _repaint(b, "snap-3", renamed={})
        if chain == "later_swap_window":
            # The first swap is now past its 3 s window; the second is not.
            await b.advance(_PAST_RENUMBER_WINDOW_S - 2.0)
        await b.flush()
        mark = b.mark()
        await b.say("click two")
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "overlay_numbers_changed"
        assert notice["snapshot_id"] == "snap-3"
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        # Badge 3 never changed: it clicks.
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-3", 3)


@pytest.mark.parametrize("refresh_ends", ["fails", "paints_a_new_list"])
async def test_ck_58_settle_click_reply_after_a_focus_refresh_clears_the_clicked_list(
    tmp_path, manual_clicks, refresh_ends,
):
    """CK-58: settle on, a focus refresh starts between a badge click and its reply.

    Rule: with overlay_settle_after_click and overlay_keep_numbers on, a
    badge click is sent, a focus change starts a refresh before the click's
    ok reply arrives, and the reply arrives while that refresh is in flight.
    When the refresh fails and the clicked list is still on screen, Logic
    sends clear_overlay and starts the settle read, as for an ok reply with
    no refresh (CK-14). When the refresh paints a new list, Logic sends no
    clear_overlay and 'click N' clicks the new list
    (wh-overlay-slow-uia-stale-badges.15).
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1, mark, click = await _painted_click_in_flight(b)
        sid, gen = _pair(paint1)
        await _foreground(b, NUMBERED_WINDOW)
        refresh = await b.next_frame("start_overlay_walk")
        assert _pair(refresh.params) == (sid, gen + 1)
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        await b.quiet(mark, 0.2)
        # The refresh is still in flight: nothing is cleared yet.
        assert "clear_overlay" not in _actions(b.gui_since(mark))
        if refresh_ends == "fails":
            b.reply(refresh, walk_failed_reply(refresh.params, "execution_failed"))
            clear = await b.next_gui("clear_overlay")
            assert clear["overlay_session_id"] == sid
            settle_read = await b.next_frame("start_overlay_walk")
            assert settle_read.params.get("settle") is True
            assert settle_read.params.get("compare_snapshot_id") == "snap-1"
        else:
            b.reply(refresh, walk_ok_reply(refresh.params, snapshot_id="snap-2"))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
            await b.gui_ack("painted", paint2)
            await b.quiet(mark, 0.3)
            assert "clear_overlay" not in _actions(b.gui_since(mark))
            await b.say("click three")
            second = await b.next_frame("click_snapshot_item")
            assert second.params["item_id"] == item_id("snap-2", 3)
        assert _notices(b, mark) == []


@pytest.mark.parametrize(
    "repaint", ["focus_swap", "settle_swap", "focus_restore"],
)
async def test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked(
    tmp_path, repaint,
):
    """CK-59: the renumber guard after a focus or settle repaint.

    Rule: 'click N' within 3 s of a focus-change refresh or a post-click
    settle repaint that replaced the snapshot and changed badge N is refused
    with overlay_numbers_changed, and no click_snapshot_item is sent
    (wh-overlay-slow-uia-stale-badges.4). A failed refresh that restores the
    same snapshot arms nothing: 'click N' clicks the visible list.
    """

    config = (
        _settle_config(tmp_path) if repaint == "settle_swap"
        else write_config(tmp_path)
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.show_numbers_painted("snap-1", numbers=(1, 2, 3))
        if repaint == "settle_swap":
            await b.say("click one")
            await b.next_frame("click_snapshot_item")
            await b.next_gui("clear_overlay")
        else:
            await _foreground(b, NUMBERED_WINDOW)
        walk = await b.next_frame("start_overlay_walk")
        if repaint == "focus_restore":
            b.reply(walk, walk_failed_reply(walk.params, "execution_failed"))
        else:
            b.reply(walk, renamed_walk_reply(
                walk.params, snapshot_id="snap-2",
                renamed={2: "a different control"},
            ))
            paint2 = await b.next_gui("paint_overlay")
            assert paint2["snapshot_id"] == "snap-2"
            await b.gui_ack("painted", paint2)
        await b.flush()
        mark = b.mark()
        await b.say("click two")
        if repaint == "focus_restore":
            click = await b.next_frame("click_snapshot_item")
            assert click.params["item_id"] == item_id("snap-1", 2)
            assert _notices(b, mark) == []
            return
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "overlay_numbers_changed"
        assert notice["snapshot_id"] == "snap-2"
        await b.quiet(mark, 0.2)
        assert _clicks(b, mark) == []
        # Badge 3 kept its name, role and bounds: it clicks.
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-2", 3)


async def test_ck_60_settling_keeps_the_snapshot_alive_without_a_lease_renew(
    tmp_path,
):
    """CK-60: post_click_settling keeps the clicked snapshot alive.

    Rule: with overlay_settle_after_click on, while the overlay is in
    post_click_settling, each keepalive tick sends refresh_overlay_snapshot
    for the pinned (clicked) snapshot and sends no overlay_lease_renew,
    because no badges are on screen.
    """

    config = _settle_config(tmp_path)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.show_numbers_painted("snap-1")
        # Place the next keepalive tick inside the settle read's 10 s limit.
        await b.advance(_KEEPALIVE_INTERVAL_S - 5.0)
        await b.flush()
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        await b.next_gui("clear_overlay")
        await b.next_frame("start_overlay_walk")
        await b.flush()
        mark = b.mark()
        await b.advance(5.5)
        await b.flush()
        refresh = b.frames_since(mark, actions={"refresh_overlay_snapshot"})
        assert [f.params["snapshot_id"] for f in refresh] == ["snap-1"]
        assert "overlay_lease_renew" not in _actions(b.gui_since(mark))
        assert "paint_overlay" not in _actions(b.gui_since(mark))



@pytest.mark.parametrize("keep_numbers", [True, False], ids=["keep_on", "keep_off"])
async def test_ck_61_ok_click_reply_after_hide_numbers_sends_nothing(
    tmp_path, manual_clicks, keep_numbers,
):
    """CK-61: an ok badge-click reply that arrives after 'hide numbers'.

    Rule: the overlay the click was sent against was closed before Input
    answered. Logic sends nothing for the reply (no walk, no clear, no
    unpin), shows no notice and raises no error notification, with
    overlay_keep_numbers on or off. The overlay stays closed: a 'click N'
    right after the reply is a by-name search, not a 'numbers not showing'
    notice. A later 'show numbers' and 'click N' work as usual.
    """

    config = write_config(tmp_path, overlay_keep_numbers=keep_numbers)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        _paint1, _mark, click = await _painted_click_in_flight(b)
        await b.say("hide numbers")
        clear = await b.next_gui("clear_overlay")
        await b.gui_ack("cleared", clear)
        await b.next_frame("unpin_snapshot")
        await b.flush()
        before = len(b.notifications)
        mark = b.mark()
        b.reply(click, click_reply(click.params, matched_name="control 2"))
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        assert len(b.notifications) == before
        mark = b.mark()
        b.say_soon("click three")
        by_name = await b.next_frame("click_element")
        b.reply(by_name, click_reply(by_name.params, matched_name="three"))
        await b.flush()
        assert _notices(b, mark) == []
        await b.show_numbers_painted("snap-2")
        await b.say("click three")
        second = await b.next_frame("click_snapshot_item")
        assert second.params["item_id"] == item_id("snap-2", 3)


@pytest.mark.parametrize("saved_value", [True, False], ids=["turned_on", "turned_off"])
async def test_ck_62_saved_keep_numbers_applies_at_the_next_click(
    tmp_path, saved_value,
):
    """CK-62: a saved overlay_keep_numbers applies at the next click.

    Rule: the Settings window saves click.overlay_keep_numbers through Logic
    (set_config_value). The next successful badge click follows the saved
    value with no restart: turned on, Logic starts a refresh walk and sends
    no clear_overlay; turned off, Logic sends clear_overlay and unpins the
    clicked snapshot.
    """

    config = write_config(tmp_path, overlay_keep_numbers=not saved_value)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint = await b.show_numbers_painted("snap-1")
        b.send_gui({
            "action": "set_config_value",
            "key": "click.overlay_keep_numbers",
            "value": saved_value,
            "request_id": "settings-save-1",
        })
        await b.settle_gui()
        # settle_gui returns when the GUI listener handled set_config_value.
        # The handler starts a task that awaits the save to disk and only then
        # applies the value (main.py _set_config_value_and_apply), so the click
        # can go out before the apply. StateManager._save_gui_settings sends
        # config_write_result with no await before that apply, so this wait
        # ends after the apply (LEAD DECISION 30). Proof: a forced 1.0 s delay
        # in config_service do_save fails both ids without this wait and
        # passes both with it. With this wait, 10 runs of ck_62 alone with no
        # processor pin gave 2 passed each time.
        result = await b.next_gui("config_write_result")
        assert result["saved"] is True
        await b.flush()
        mark = b.mark()
        await b.say("click two")
        await b.next_frame("click_snapshot_item")
        if saved_value:
            walk = await b.next_frame("start_overlay_walk")
            assert walk.params["overlay_session_id"] == paint["overlay_session_id"]
            await b.quiet(mark, 0.2)
            assert "clear_overlay" not in _actions(b.gui_since(mark))
        else:
            clear = await b.next_gui("clear_overlay")
            assert _pair(clear) == _pair(paint)
            unpin = await b.next_frame("unpin_snapshot")
            assert unpin.params["snapshot_id"] == "snap-1"
            await b.quiet(mark, 0.2)
            assert "start_overlay_walk" not in _actions(b.frames_since(mark))
