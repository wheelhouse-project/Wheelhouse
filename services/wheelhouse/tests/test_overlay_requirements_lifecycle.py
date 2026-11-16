"""Lifecycle requirements of the numbered overlay, tested at the process boundary.

Bead: wh-overlay-refactor-pilot.1. Each test pins one CONFIRMED row of the
table "Lifecycle: show, walk, paint, hide, failures, lease, restart" in
docs/design/overlay-requirements.md and is named after its requirement id.
Rows that need the paused state (LC-31 to LC-36, and the microphone part of
LC-37) are left out: nothing in the shipped app sends the microphone pause or
resume event to the overlay, so the paused state cannot be reached from a
process boundary.

Every test drives a real ``LogicController`` built through its constructor
(tests/overlay_boundary_harness.py says how) and asserts only on what leaves
the Logic process: frames for the Input process, dicts for the GUI process,
and Windows error notifications. No test names a private member of
``LogicController``, patches one of its methods, or reads its state.

Each test also sees at least one positive output (a walk, a paint, a clear, a
click), so a test that expects silence cannot pass because the overlay is
broken as a whole.
"""

from __future__ import annotations

import asyncio

import pytest

import tests.overlay_boundary_harness as harness
from tests.overlay_boundary_harness import (
    NUMBERED_WINDOW,
    OTHER_WINDOW,
    item_id,
    overlay_boundary,
    paint_ack,
    walk_failed_reply,
    walk_ok_reply,
    write_config,
)

pytestmark = pytest.mark.journey

# Longer than the first-walk deadline (screen_read_timeout_ms 10000 plus
# PRE_WALK_MARGIN_MS 250) and the walk request's own reply timeout.
_PAST_WALK_DEADLINE_S = 21.0
# Longer than response_timeout_ms (3000 ms in config.toml.example).
_PAST_RESPONSE_TIMEOUT_S = 3.5
# Longer than the 1000 ms paint wait.
_PAST_PAINT_DEADLINE_S = 1.2
# Longer than the 5000 ms clear-acknowledgement deadline.
_PAST_CLEAR_ACK_DEADLINE_S = 5.2
# The keepalive interval: snapshot_ttl_seconds 30 / 2.
_KEEPALIVE_INTERVAL_S = 15.0


def _pair(message: dict) -> tuple:
    return (message["overlay_session_id"], message["paint_generation"])


def _actions(messages) -> list:
    return [getattr(m, "action", None) or m["action"] for m in messages]


def _notices_containing(b, text: str) -> list:
    return [p for p in b.notifications if text in p.message]


async def _fail_walk(b, walk, kind: str) -> None:
    """Make one walk fail: a failure reply, or no reply until the deadline."""

    if kind == "timeout":
        await b.advance(_PAST_WALK_DEADLINE_S)
    else:
        b.reply(walk, walk_failed_reply(walk.params, kind))
    await b.flush()


# ---------------------------------------------------------------------------
# Show and hide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase", ["show numbers", "apply numbers", "show numbers here"],
)
async def test_lc_1_show_numbers_while_closed_sends_one_walk(tmp_path, phrase):
    """LC-1: 'show numbers' with the overlay closed sends start_overlay_walk.

    Rule: Logic sends start_overlay_walk (scope focused_window, new
    overlay_session_id, paint_generation) to Input. Nothing else is sent until
    Input answers.
    """

    async with overlay_boundary(tmp_path) as b:
        mark = b.mark()
        await b.say(phrase)
        walk = await b.next_frame("start_overlay_walk")
        assert walk.params["scope"] == "focused_window"
        assert isinstance(walk.params["overlay_session_id"], int)
        assert isinstance(walk.params["paint_generation"], int)
        assert walk.request_id is not None
        frames, gui = await b.quiet(mark, 0.3)
        assert _actions(frames) == ["start_overlay_walk"]
        assert gui == []


@pytest.mark.parametrize(
    "phrase", ["hide numbers", "hyde numbers", "dismiss numbers"],
)
async def test_lc_2_hide_numbers_clears_unpins_and_closes_at_once(
    tmp_path, phrase,
):
    """LC-2: 'hide numbers' with the overlay open clears, unpins and closes.

    Rule: Logic sends clear_overlay to GUI and unpin_snapshot to Input for
    every pinned snapshot, and treats the overlay as closed at once without
    waiting for a cleared acknowledgement.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        await b.say(phrase)
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        # No cleared acknowledgement is sent. The overlay is closed anyway:
        # the next show starts a NEW session, which only a closed overlay does
        # (a painted one refreshes in the same session, LC-7).
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert walk.params["overlay_session_id"] != paint["overlay_session_id"]


async def test_lc_3_hide_numbers_while_closed_sends_nothing(tmp_path):
    """LC-3: 'hide numbers' while the overlay is closed sends nothing.

    Rule: Logic sends nothing to GUI or Input.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("hide numbers")
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        # The same boundary answers a show, so the silence above is the
        # hide's and not a dead overlay's.
        await b.say("show numbers")
        await b.next_frame("start_overlay_walk")
        assert _actions(b.frames_since(mark)) == ["start_overlay_walk"]


@pytest.mark.parametrize(
    "click_settings, by_name_click_works",
    [
        pytest.param({"enabled": False}, False, id="click-disabled"),
        pytest.param({"overlay_enabled": False}, True, id="overlay-disabled"),
        pytest.param(
            {"overlay_badge_theme": "purple"}, True, id="bad-overlay-key",
        ),
    ],
)
async def test_lc_4_show_and_hide_do_nothing_while_overlay_off(
    tmp_path, click_settings, by_name_click_works,
):
    """LC-4: with clicking or the overlay off, show and hide send nothing.

    Rule: Logic sends nothing to Input or GUI. By-name click stays operative
    when only an overlay key is bad.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        # Positive output: the started Logic process did talk to the GUI.
        assert {"action": "reset_overlay"} in b.gui
        mark = b.mark()
        await b.say("show numbers")
        await b.say("hide numbers")
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        if by_name_click_works:
            await b.say("click save")
            click = await b.next_frame("click_element")
            assert "save" in repr(click.params).lower()


# ---------------------------------------------------------------------------
# Show while the overlay is already open
# ---------------------------------------------------------------------------


async def test_lc_7_show_numbers_while_painted_refreshes_without_clearing(
    tmp_path,
):
    """LC-7: 'show numbers' while painted sends a walk one generation higher.

    Rule: Logic sends a new start_overlay_walk with paint_generation raised by
    one. The old badges stay visible until the new paint replaces them.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert _pair(paint2) == (sid, gen + 1)
        await b.flush()
        before_paint = []
        for message in b.gui_since(mark):
            if message is paint2:
                break
            before_paint.append(message["action"])
        assert "clear_overlay" not in before_paint


async def test_lc_8_show_during_refresh_drops_the_older_walk_reply(tmp_path):
    """LC-8: 'show numbers' during a refresh walk supersedes that walk.

    Rule: Logic sends a newer start_overlay_walk (generation raised) and drops
    the older walk's reply when it arrives.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await b.say("show numbers")
        refresh1 = await b.next_frame("start_overlay_walk")
        assert _pair(refresh1.params) == (sid, gen + 1)
        await b.say("show numbers")
        refresh2 = await b.next_frame("start_overlay_walk")
        assert _pair(refresh2.params) == (sid, gen + 2)

        mark = b.mark()
        b.reply(refresh1, walk_ok_reply(refresh1.params, snapshot_id="snap-old"))
        frames, gui = await b.quiet(mark, 0.3)
        assert [f for f in frames if f.params.get("snapshot_id") == "snap-old"] == []
        assert [m for m in gui if m["action"] == "paint_overlay"] == []

        b.reply(refresh2, walk_ok_reply(refresh2.params, snapshot_id="snap-new"))
        paint3 = await b.next_gui("paint_overlay")
        assert _pair(paint3) == (sid, gen + 2)
        assert paint3["snapshot_id"] == "snap-new"
        assert "snap-old" not in [
            f.params.get("snapshot_id") for f in b.frames_since(mark)
        ]


async def test_lc_9_show_during_first_walk_restarts_without_a_clear(tmp_path):
    """LC-9 (walk in flight): 'show numbers' during the first walk restarts it.

    Rule: Logic sends clear_overlay (only if a paint was already sent),
    unpin_snapshot for the old snapshot, and a new start_overlay_walk with
    generation raised.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] == walk1.params["overlay_session_id"]
        assert walk2.params["paint_generation"] == walk1.params["paint_generation"] + 1
        _frames, gui = await b.quiet(mark, 0.2)
        # No paint was sent, so no clear is owed.
        assert "clear_overlay" not in _actions(gui)


async def test_lc_9_show_during_first_paint_clears_unpins_and_restarts(
    tmp_path,
):
    """LC-9 (paint in flight): 'show numbers' during the first paint restarts.

    Rule: Logic sends clear_overlay (only if a paint was already sent),
    unpin_snapshot for the old snapshot, and a new start_overlay_walk with
    generation raised.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.answer_walk(
            lambda p: walk_ok_reply(p, snapshot_id="snap-1"),
        )
        paint1 = await b.next_gui("paint_overlay")
        await b.say("show numbers")
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint1)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] == walk1.params["overlay_session_id"]
        assert walk2.params["paint_generation"] == walk1.params["paint_generation"] + 1


async def _enter_error_state(b) -> dict:
    """Reach the error state the only way a boundary can.

    The GUI acknowledges a paint at the CURRENT pair while the first walk is
    still in flight (no paint was ever sent): an invalid transition, which the
    machine answers by entering error. Returns the walk's params.

    Two outputs show the state is error and nothing else: the walk's own ok
    reply then paints nothing (a walk in flight would paint it, LC-11), and a
    bare number is refused with the numbers_not_showing notice (a closed
    overlay would click a control by that name, LC-28).
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
    frames, gui = await b.quiet(mark, 0.2)
    assert "paint_overlay" not in _actions(gui)
    assert "pin_snapshot" not in _actions(frames)
    await b.say("click seven")
    notice = await b.next_gui("show_click_notice")
    assert notice["reason"] == "numbers_not_showing"
    assert "click_element" not in _actions(b.frames_since(mark))
    return walk.params


async def test_lc_10_show_numbers_in_error_starts_a_new_session(tmp_path):
    """LC-10: 'show numbers' in the error state starts a new session.

    Rule: Logic unpins any leftover snapshot, starts a new session, and sends
    start_overlay_walk.
    """

    async with overlay_boundary(tmp_path) as b:
        walk1 = await _enter_error_state(b)
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        # A new session; a show during a walk keeps the session (LC-9).
        assert walk2.params["overlay_session_id"] != walk1["overlay_session_id"]
        assert walk2.params["scope"] == "focused_window"


# ---------------------------------------------------------------------------
# The walk reply
# ---------------------------------------------------------------------------


async def test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair(tmp_path):
    """LC-11: an ok walk reply pins the snapshot and paints it at one pair.

    Rule: Logic sends pin_snapshot (session id, snapshot id, generation) to
    Input and paint_overlay with the same pair to GUI. The overlay is not yet
    treated as painted.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        walk = await b.answer_walk(
            lambda p: walk_ok_reply(p, snapshot_id="snap-1", numbers=(4, 5)),
        )
        pin = await b.next_frame("pin_snapshot")
        assert pin.params["snapshot_id"] == "snap-1"
        assert _pair(pin.params) == _pair(walk.params)
        paint = await b.next_gui("paint_overlay")
        assert _pair(paint) == _pair(walk.params)
        assert paint["snapshot_id"] == "snap-1"
        assert [i["display_number"] for i in paint["items"]] == [4, 5]
        # Not yet painted: the badge lease is renewed only for a painted
        # overlay, and none is renewed until the GUI acknowledges.
        _frames, gui = await b.quiet(mark, 0.2)
        assert "overlay_lease_renew" not in _actions(gui)
        await b.gui_ack("painted", paint)
        renew = await b.next_gui("overlay_lease_renew")
        assert _pair(renew) == _pair(paint)


@pytest.mark.parametrize("superseded_by", ["hide", "show"])
async def test_lc_13_late_walk_reply_is_neither_pinned_nor_painted(
    tmp_path, superseded_by,
):
    """LC-13: a walk reply for a passed generation or after hide is dropped.

    Rule: Logic sends no paint_overlay and no pin_snapshot for it.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        if superseded_by == "hide":
            await b.say("hide numbers")
        else:
            await b.say("show numbers")
            walk2 = await b.next_frame("start_overlay_walk")
        await b.flush()
        mark = b.mark()
        b.reply(walk1, walk_ok_reply(walk1.params, snapshot_id="snap-late"))
        frames, gui = await b.quiet(mark, 0.3)
        assert "pin_snapshot" not in _actions(frames)
        assert "paint_overlay" not in _actions(gui)
        if superseded_by == "show":
            # The current walk still paints, so the drop above is selective.
            b.reply(walk2, walk_ok_reply(walk2.params, snapshot_id="snap-now"))
            paint = await b.next_gui("paint_overlay")
            assert paint["snapshot_id"] == "snap-now"
            assert [
                f.params["snapshot_id"] for f in b.frames_since(mark)
                if f.action == "pin_snapshot"
            ] == ["snap-now"]


@pytest.mark.parametrize(
    "kind", ["execution_failed", "error", "malformed", "timeout"],
)
async def test_lc_14_failed_first_walk_shows_notice_and_closes(tmp_path, kind):
    """LC-14: a failed first walk shows numbers_not_drawn and closes.

    Rule: Logic shows a click notice (outcome execution_failed, reason
    numbers_not_drawn), sends no paint, unpins any pin, and the overlay is
    closed.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await _fail_walk(b, walk1, kind)
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "numbers_not_drawn"
        assert "paint_overlay" not in _actions(b.gui_since(mark))
        assert "pin_snapshot" not in _actions(b.frames_since(mark))
        # Closed: the next show starts a new session.
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] != walk1.params["overlay_session_id"]


# ---------------------------------------------------------------------------
# The paint acknowledgement
# ---------------------------------------------------------------------------


async def test_lc_16_painted_ack_makes_click_n_resolve_to_the_list(tmp_path):
    """LC-16: a painted acknowledgement makes 'click N' use the painted list.

    Rule: The overlay is treated as showing: 'click N' resolves against the
    painted list. Logic sends nothing further.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.flush()
        mark = b.mark()
        await b.gui_ack("painted", paint)
        await b.quiet(mark, 0.2)
        # Only the keepalive runs: the badge lease renew to GUI (and the
        # store refresh to Input, which is not an overlay command).
        assert _actions(b.gui_since(mark)) == ["overlay_lease_renew"]
        assert b.frames_since(mark) == []
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 2)


@pytest.mark.parametrize("how", ["failed", "no_ack"])
async def test_lc_17_failed_or_missing_paint_ack_clears_and_closes(
    tmp_path, how,
):
    """LC-17: a failed or missing first paint acknowledgement closes.

    Rule: Logic sends clear_overlay and unpin_snapshot, fires the pending
    ambiguous-click notice if one exists (no generic notice), and the overlay
    is closed.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.answer_walk(
            lambda p: walk_ok_reply(p, snapshot_id="snap-1"),
        )
        paint = await b.next_gui("paint_overlay")
        mark = b.mark()
        if how == "failed":
            await b.gui_ack("failed", paint)
            # The failed acknowledgement itself closes the overlay: the clear
            # is out before the paint deadline (about 1 s after the paint)
            # could close it for the no_ack reason instead.
            await b.flush()
            assert "clear_overlay" in _actions(b.gui_since(mark))
        else:
            await b.advance(_PAST_PAINT_DEADLINE_S)
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        await b.flush()
        assert "show_click_notice" not in _actions(b.gui_since(mark))
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] != walk1.params["overlay_session_id"]


async def test_lc_18_acknowledgement_at_another_pair_is_ignored(tmp_path):
    """LC-18: an overlay_state_changed at another pair is ignored.

    Rule: Logic ignores it: no state change, no message to Input or GUI.
    Covers painted, failed and cleared acknowledgements alike.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        sid, gen = _pair(paint)
        await b.flush()
        mark = b.mark()
        for state in ("painted", "failed", "cleared"):
            b.send_gui(paint_ack(state, sid, gen + 7, "snap-1"))
            b.send_gui(paint_ack(state, sid + 7, gen, "snap-1"))
        await b.settle_gui()
        frames, gui = await b.quiet(mark, 0.1)
        # A painted ack would start the lease renew; a failed ack would
        # clear (LC-17). Neither happened.
        assert frames == []
        assert gui == []
        # The same paint still accepts its own acknowledgement.
        await b.gui_ack("painted", paint)
        renew = await b.next_gui("overlay_lease_renew")
        assert _pair(renew) == (sid, gen)


@pytest.mark.parametrize("when", ["painted", "after_hide"])
async def test_lc_19_cleared_acknowledgement_changes_nothing(tmp_path, when):
    """LC-19: a cleared acknowledgement at the current pair changes nothing.

    Rule: Logic changes nothing and sends nothing (after the user hid the
    numbers, or while painted; the paused case is out of reach).
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        if when == "after_hide":
            await b.say("hide numbers")
            await b.next_gui("clear_overlay")
            await b.next_frame("unpin_snapshot")
        await b.flush()
        mark = b.mark()
        b.send_gui(paint_ack("cleared", *_pair(paint), "snap-1"))
        await b.settle_gui()
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        await b.say("click two")
        if when == "painted":
            # Still painted: the number still names the painted control.
            click = await b.next_frame("click_snapshot_item")
            assert click.params["item_id"] == item_id("snap-1", 2)
        else:
            # Still closed: the number is a control name.
            await b.next_frame("click_element")


# ---------------------------------------------------------------------------
# Refresh walks
# ---------------------------------------------------------------------------


async def test_lc_20_refresh_unpins_the_old_snapshot_after_the_new_paint(
    tmp_path,
):
    """LC-20: a refresh pins the new snapshot, then unpins the old one.

    Rule: Logic sends pin_snapshot for the new snapshot with the paint, then
    unpin_snapshot for the old one after the acknowledgement. Exactly one
    snapshot stays pinned.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("show numbers")
        refresh = await b.answer_walk(
            lambda p: walk_ok_reply(p, snapshot_id="snap-2"),
        )
        pin = await b.next_frame(
            "pin_snapshot", where=lambda p: p["snapshot_id"] == "snap-2",
        )
        assert _pair(pin.params) == _pair(refresh.params)
        paint2 = await b.next_gui("paint_overlay")
        assert _pair(paint2) == _pair(refresh.params)
        await b.quiet(b.mark(), 0.2)
        assert "unpin_snapshot" not in _actions(b.frames)
        await b.gui_ack("painted", paint2)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        await b.flush()
        assert [
            f.params["snapshot_id"] for f in b.frames
            if f.action == "unpin_snapshot"
        ] == ["snap-1"]


@pytest.mark.parametrize("kind", ["execution_failed", "timeout"])
async def test_lc_21_failed_refresh_on_same_window_keeps_the_numbers(
    tmp_path, kind,
):
    """LC-21: a failed refresh on the same foreground window keeps the numbers.

    Rule: Logic sends nothing: the old badges stay, the old snapshot stays
    pinned, and the overlay stays painted.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await _fail_walk(b, refresh, kind)
        frames, gui = await b.quiet(mark, 0.2)
        # The keepalive keeps running (a timeout advances the clock past its
        # ticks); nothing else leaves Logic.
        assert frames == []
        assert set(_actions(gui)) <= {"overlay_lease_renew"}
        await b.say("click one")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 1)


@pytest.mark.parametrize("kind", ["execution_failed", "timeout"])
async def test_lc_22_failed_refresh_after_window_left_clears_and_closes(
    tmp_path, kind,
):
    """LC-22: a failed refresh after the numbered window left closes the overlay.

    Rule: Logic sends clear_overlay and unpin_snapshot, shows the
    numbers_not_drawn notice, and the overlay is closed.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        b.desktop.bring_to_front(OTHER_WINDOW)
        await _fail_walk(b, refresh, kind)
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint["overlay_session_id"]
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        notice = await b.next_gui("show_click_notice")
        assert notice["reason"] == "numbers_not_drawn"
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] != paint["overlay_session_id"]


# ---------------------------------------------------------------------------
# Clear acknowledgement, badge lease, restart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_lc_26_unacknowledged_clear_raises_an_error_notification(
    tmp_path, acknowledged,
):
    """LC-26: a clear with no acknowledgement in 5000 ms raises an error.

    Rule: Logic reports an error (logged, which raises a Windows error
    notification) saying the badges may still be on screen. It does not retry
    and does not change state.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("hide numbers")
        clear = await b.next_gui("clear_overlay")
        if acknowledged:
            b.send_gui(paint_ack("cleared", *_pair(clear)))
            await b.settle_gui()
        mark = b.mark()
        await b.advance(_PAST_CLEAR_ACK_DEADLINE_S)
        await b.flush()
        reports = _notices_containing(b, "badges may still be on the screen")
        if acknowledged:
            assert reports == []
        else:
            assert len(reports) == 1
            assert reports[0].levelname == "ERROR"
        # No retry: no second clear.
        assert "clear_overlay" not in _actions(b.gui_since(mark))



async def test_lc_46_unacknowledged_clear_log_advises_show_numbers(
    tmp_path, caplog,
):
    """LC-46: the unacknowledged-clear log line tells the user to say show numbers.

    Rule: the ERROR log line behind the LC-26 notification ends with advice
    that works from the closed state that a hide leaves: say 'show numbers'
    to paint again. It does not advise 'hide numbers', which does nothing
    in the closed state. The notification itself shows only the first
    sentence and points to the log for the rest.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("hide numbers")
        await b.next_gui("clear_overlay")
        caplog.clear()
        await b.advance(_PAST_CLEAR_ACK_DEADLINE_S)
        await b.flush()
        lines = [
            r.getMessage() for r in caplog.records
            if r.levelname == "ERROR"
            and "badges may still be on the screen" in r.getMessage()
        ]
        assert len(lines) == 1
        assert "say 'show numbers' to paint again" in lines[0]
        assert "hide numbers" not in lines[0]


async def test_lc_27_painted_overlay_renews_its_lease_every_interval(tmp_path):
    """LC-27: a painted overlay renews its badge lease about every 15 s.

    Rule: About every 15 seconds Logic sends an overlay_lease_renew message to
    GUI carrying lease_ms (at least 60000) at the visible pair, and re-puts
    the snapshot so it does not expire.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        mark = b.mark()
        await b.advance(_KEEPALIVE_INTERVAL_S)
        renew = await b.next_gui("overlay_lease_renew")
        assert _pair(renew) == _pair(paint)
        assert renew["lease_ms"] >= 60000
        refresh = [
            f for f in b.frames_since(mark, actions={"refresh_overlay_snapshot"})
        ]
        assert [f.params["snapshot_id"] for f in refresh] == ["snap-1"]
        await b.advance(_KEEPALIVE_INTERVAL_S)
        renew2 = await b.next_gui("overlay_lease_renew")
        assert _pair(renew2) == _pair(paint)
        # 30 s after the paint, past snapshot_ttl_seconds, the numbers still
        # name their controls.
        await b.say("click three")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 3)


async def test_lc_28_expired_lease_report_clears_and_closes(tmp_path):
    """LC-28: GUI reports the badge lease expired; Logic closes the overlay.

    Rule: Logic sends clear_overlay and unpin_snapshot and the overlay is
    closed, so 'click N' falls back to by-name. An error is logged.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        b.send_gui(paint_ack("expired", *_pair(paint), "snap-1"))
        await b.settle_gui()
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        reports = _notices_containing(b, "expired the badge lease")
        assert len(reports) == 1
        assert reports[0].levelname == "ERROR"
        await b.say("click two")
        await b.next_frame("click_element")
        assert "click_snapshot_item" not in _actions(b.frames)


async def test_lc_47_unacknowledged_echo_clear_after_an_expired_lease_is_a_warning(
    tmp_path, caplog,
):
    """LC-47: one clear fault per pair raises one error notification.

    Rule: GUI reports the badge lease expired (LC-28), Logic logs that at
    ERROR and sends clear_overlay at the same pair, and GUI never
    acknowledges that clear. When the 5000 ms clear deadline passes, Logic
    logs the unconfirmed clear at WARNING, not ERROR, so the user sees one
    error notification for the incident
    (wh-overlay-slow-uia-stale-badges.9).
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        before = len(b.notifications)
        b.send_gui(paint_ack("expired", *_pair(paint), "snap-1"))
        await b.settle_gui()
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint)
        await b.flush()
        assert len(_notices_containing(b, "expired the badge lease")) == 1
        caplog.clear()
        await b.advance(_PAST_CLEAR_ACK_DEADLINE_S)
        await b.flush()
        unconfirmed = [
            r for r in caplog.records
            if "badges may still be on the screen" in r.getMessage()
        ]
        assert [r.levelname for r in unconfirmed] == ["WARNING"]
        assert _notices_containing(b, "badges may still be on the screen") == []
        assert len(b.notifications) - before == 1


@pytest.mark.parametrize(
    "click_settings", [{}, {"enabled": False}], ids=["enabled", "disabled"],
)
async def test_lc_29_logic_start_sends_reset_overlay_first(
    tmp_path, click_settings,
):
    """LC-29: a starting Logic process sends reset_overlay to the GUI.

    Rule: Logic sends a reset_overlay message to GUI, which removes leftover
    badge windows, resets its generation gate, and cancels its badge lease.
    """

    config = write_config(tmp_path, **click_settings)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.flush()
        overlay_messages = b.gui_since((0, 0))
        assert overlay_messages[:1] == [{"action": "reset_overlay"}]
        if not click_settings:
            # And it comes before the first paint of the new process.
            await b.show_numbers_painted("snap-1")
            assert _actions(b.gui_since((0, 0)))[0] == "reset_overlay"
            assert _actions(b.gui_since((0, 0))).count("reset_overlay") == 1


async def test_lc_30_logic_grants_only_a_bounded_badge_lease(tmp_path):
    """LC-30 (Logic half): every lease Logic grants the GUI is bounded.

    Rule: GUI removes the badges itself when the lease expires (first lease
    90 s without renew; renewals grant at least 60 s). Input drops the pinned
    snapshot at its TTL. Only the Logic half is observable here: each renew
    grants a finite lease, so a Logic that stops renewing lets it run out.
    """

    async with overlay_boundary(tmp_path) as b:
        paint = await b.show_numbers_painted("snap-1")
        assert "lease_ms" not in paint
        renews = [m for m in b.gui if m.get("action") == "overlay_lease_renew"]
        await b.advance(_KEEPALIVE_INTERVAL_S)
        renews.append(await b.next_gui("overlay_lease_renew"))
        for renew in renews:
            assert 60000 <= renew["lease_ms"] <= 4 * 60000


async def test_lc_37_hide_numbers_in_error_closes_the_overlay(tmp_path):
    """LC-37: 'hide numbers' in the error state closes the overlay.

    Rule: Logic sends clear_overlay (if a snapshot is pinned) and
    unpin_snapshot, and the overlay is closed. (The microphone part needs the
    paused state and is left out.)
    """

    async with overlay_boundary(tmp_path) as b:
        # In error a bare number is refused with a notice ...
        await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        await b.say("hide numbers")
        frames, gui = await b.quiet(mark, 0.2)
        # Nothing was pinned or painted, so no clear and no unpin is owed.
        assert "clear_overlay" not in _actions(gui)
        assert "unpin_snapshot" not in _actions(frames)
        # ... and once closed, the number is a control name again.
        await b.say("click seven")
        await b.next_frame("click_element")
        assert "show_click_notice" not in _actions(b.gui_since(mark))


# ---------------------------------------------------------------------------
# Round 2: rows David confirmed on 2026-11-04
# ---------------------------------------------------------------------------


async def _make_grid_pointer_channel_unconfirmed(b) -> None:
    """Time out a grid click, and leave its confirming probe unanswered.

    The mouse grid is opened by voice, 'click three' makes it send a pointer
    click, and nobody answers that click or the probe Logic sends after it.
    Input then looks unconfirmed to Logic until a probe is answered.
    """

    await b.say("show grid")
    await b.flush()
    assert any(
        m.get("action") == "paint_grid" for m in b.gui if isinstance(m, dict)
    ), "the grid did not open"
    say = b.say_soon("click three")
    await b.next_frame("click_point")
    await b.advance(_PAST_RESPONSE_TIMEOUT_S)
    await b.next_frame("channel_probe")
    await b.advance(_PAST_RESPONSE_TIMEOUT_S)
    await asyncio.wait_for(say, 10.0)


async def test_lc_5_show_numbers_refused_while_grid_pointer_pending(tmp_path):
    """LC-5: 'show numbers' with the grid pointer channel unconfirmed is refused.

    Rule: Logic sends no start_overlay_walk and shows a click notice with
    outcome execution_failed and reason grid_pointer_pending.
    """

    async with overlay_boundary(tmp_path) as b:
        await _make_grid_pointer_channel_unconfirmed(b)
        mark = b.mark()
        say = b.say_soon("show numbers")
        # Positive output: Logic probes the channel before it decides.
        await b.next_frame("channel_probe")
        await b.advance(_PAST_RESPONSE_TIMEOUT_S)
        await asyncio.wait_for(say, 10.0)
        notice = await b.next_gui("show_click_notice")
        while notice["reason"] != "grid_pointer_pending":
            notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        frames, gui = await b.quiet(mark, 0.2)
        assert "start_overlay_walk" not in _actions(frames)
        assert "paint_overlay" not in _actions(gui)


async def test_lc_6_show_numbers_closes_the_mouse_grid_first(tmp_path):
    """LC-6: 'show numbers' with the mouse grid open closes the grid first.

    Rule: Logic closes the mouse grid before starting the overlay walk, so
    only one of them is open.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show grid")
        await b.flush()
        assert any(
            m.get("action") == "paint_grid" for m in b.gui
            if isinstance(m, dict)
        ), "the grid did not open"
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint)
        await b.flush()
        actions = [m.get("action") for m in b.gui if isinstance(m, dict)]
        assert "clear_grid" in actions
        assert actions.index("clear_grid") < actions.index("paint_overlay")
        # Only the numbers are open: a bare number names a badge, and the
        # closed grid sends no pointer click.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)
        assert "click_point" not in _actions(b.frames)


def _reply_skewed_in_session(params: dict) -> dict:
    return walk_ok_reply(
        {**params, "overlay_session_id": params["overlay_session_id"] + 1000},
        snapshot_id="snap-skew",
    )


def _reply_skewed_in_generation(params: dict) -> dict:
    return walk_ok_reply(
        {**params, "paint_generation": params["paint_generation"] + 1000},
        snapshot_id="snap-skew",
    )


@pytest.mark.parametrize(
    "make_reply",
    [
        pytest.param(_reply_skewed_in_session, id="wrong-session-id"),
        pytest.param(_reply_skewed_in_generation, id="wrong-generation"),
        pytest.param(lambda params: {"status": "ok"}, id="unreadable-body"),
        pytest.param(lambda params: {"garbage": [1, 2]}, id="unreadable-other"),
    ],
)
async def test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk(
    tmp_path, make_reply,
):
    """LC-12: a walk reply with another pair, or unreadable, is a failed walk.

    Rule: Logic sends no paint_overlay and treats the walk as failed (the
    numbers_not_drawn notice, as LC-14).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        b.reply(walk1, make_reply(walk1.params))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "numbers_not_drawn"
        frames, gui = await b.quiet(mark, 0.2)
        assert "paint_overlay" not in _actions(gui)
        assert "pin_snapshot" not in _actions(frames)
        # Closed: the next show starts a new session.
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] != walk1.params["overlay_session_id"]


async def test_lc_15_no_targets_walk_reply_is_a_failed_walk(tmp_path):
    """LC-15: a no_targets first walk reply shows numbers_not_drawn and closes.

    Rule: Logic treats it as a failed walk: the numbers_not_drawn notice, no
    paint, and the overlay stays closed.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        b.reply(walk1, walk_failed_reply(walk1.params, "no_targets"))
        notice = await b.next_gui("show_click_notice")
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == "numbers_not_drawn"
        frames, gui = await b.quiet(mark, 0.2)
        assert "paint_overlay" not in _actions(gui)
        assert "pin_snapshot" not in _actions(frames)
        await b.say("show numbers")
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] != walk1.params["overlay_session_id"]


async def test_lc_23_failed_refresh_paint_clears_unpins_both_and_closes(
    tmp_path,
):
    """LC-23: a refresh paint acknowledged failed closes the overlay.

    Rule: Logic sends clear_overlay and unpin_snapshot for both snapshots and
    the overlay is closed, with no generic notice.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        mark = b.mark()
        await b.gui_ack("failed", paint2)
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint2["overlay_session_id"]
        await b.next_frame(
            "unpin_snapshot", where=lambda p: p["snapshot_id"] == "snap-1",
        )
        await b.next_frame(
            "unpin_snapshot", where=lambda p: p["snapshot_id"] == "snap-2",
        )
        await b.flush()
        assert "show_click_notice" not in _actions(b.gui_since(mark))
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert walk.params["overlay_session_id"] != paint1["overlay_session_id"]


@pytest.mark.parametrize("reply_kind", ["no_reply", "status_error", "malformed"])
@pytest.mark.parametrize("request_kind", ["pin_snapshot", "unpin_snapshot"])
async def test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens(
    tmp_path, monkeypatch, request_kind, reply_kind,
):
    """LC-25: no reply, an error reply or a malformed reply to a pin or unpin.

    Rule: Logic logs it and does nothing else: no retry, no state change, no
    notice.
    """

    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    def unanswered(action: str) -> None:
        monkeypatch.setattr(
            harness, "_AUTO_ANSWERED", harness._AUTO_ANSWERED - {action},
        )

    def bad_reply(frame) -> dict:
        if reply_kind == "status_error":
            return PinSnapshotResponse(
                status="error", reason="harness_error",
                overlay_session_id=frame.params["overlay_session_id"],
                snapshot_id=frame.params["snapshot_id"], pinned=False,
            ).to_dict()
        return {"status": "ok"}

    async with overlay_boundary(tmp_path) as b:
        if request_kind == "pin_snapshot":
            unanswered("pin_snapshot")
            await b.say("show numbers")
            await b.answer_walk(
                lambda p: walk_ok_reply(p, snapshot_id="snap-1"),
            )
            # The paint waits behind the pin reply (main.py
            # _overlay_send_pin), so the bad reply must be sent while Logic
            # still waits for it. Sent after the paint, it would reach a
            # request that had already timed out and be dropped unread.
            bad = await b.next_frame("pin_snapshot")
            notices_before = len(b.notifications)
            mark = b.mark()
            if reply_kind != "no_reply":
                b.reply(bad, bad_reply(bad))
            else:
                # no_reply: the paint follows the request timeout
                # (response_timeout_ms 3000), which the loop clock measures.
                await b.advance(_PAST_RESPONSE_TIMEOUT_S)
            paint = await b.next_gui("paint_overlay")
            await b.gui_ack("painted", paint)
            await b.next_gui("overlay_lease_renew")
        else:
            await b.show_numbers_painted("snap-1")
            unanswered("unpin_snapshot")
            await b.say("hide numbers")
            bad = await b.next_frame("unpin_snapshot")
            await b.flush()
            notices_before = len(b.notifications)
            mark = b.mark()
            if reply_kind != "no_reply":
                b.reply(bad, bad_reply(bad))
                # Real time for the reply to reach Logic before the clock
                # moves.
                await asyncio.sleep(0.3)
        await b.advance(_PAST_RESPONSE_TIMEOUT_S)
        await b.flush()
        # No retry, no notice, no other message caused by the bad reply.
        assert [
            f for f in b.frames_since(mark) if f.action == request_kind
        ] == []
        assert "show_click_notice" not in _actions(b.gui_since(mark))
        # The app layer's own request-timeout error (no_reply only) is the one
        # notification that may appear; Logic adds none.
        later = [
            n.message for n in b.notifications[notices_before:]
            if "timed out after" not in n.message
        ]
        assert later == []
        if reply_kind != "no_reply":
            assert len(b.notifications) == notices_before
        await b.say("click two")
        if request_kind == "pin_snapshot":
            # The overlay is still painted: the number names its badge.
            click = await b.next_frame("click_snapshot_item")
            assert click.params["item_id"] == item_id("snap-1", 2)
        else:
            # The hide still closed the overlay: the number is a name again.
            await b.next_frame("click_element")
        # And a later boundary output works: a new show starts a walk.
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        assert walk.params["scope"] == "focused_window"


# ---------------------------------------------------------------------------
# The walking cue
# ---------------------------------------------------------------------------

_CUE = frozenset({"overlay_walk_cue"})
# screen_read_timeout_ms 10000 + response_timeout_ms 3000 (config.toml.example)
# + the 5000 ms wait for the end of an open dictation sentence.
_WALK_CUE_BOUND_MS = 18000


def _cues(messages) -> list:
    return [m["active"] for m in messages if m.get("action") == "overlay_walk_cue"]


async def test_lc_40_walk_cue_stays_on_until_the_paint(tmp_path):
    """LC-40: a walk turns the walking cue on, and a good build leaves it on.

    Rule: when a walk starts, Logic sends overlay_walk_cue with active true to
    GUI before paint_overlay. After a successful build Logic sends no
    overlay_walk_cue with active false; the GUI removes the cue when
    paint_overlay arrives.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint)
        await b.next_gui("overlay_lease_renew")
        await b.flush()
        sent = b.gui_since(mark, actions=_CUE | {"paint_overlay"})
        assert [(m["action"], m.get("active")) for m in sent] == [
            ("overlay_walk_cue", True), ("paint_overlay", None),
        ]


@pytest.mark.parametrize("kind", ["execution_failed", "malformed", "timeout"])
async def test_lc_41_failed_walk_turns_the_walk_cue_off(tmp_path, kind):
    """LC-41: a walk that fails or times out turns the walking cue off.

    Rule: Logic sends overlay_walk_cue with active true when the walk starts
    and overlay_walk_cue with active false when the walk fails or gets no
    reply, so the GUI does not wait for its own fallback timer.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        await _fail_walk(b, walk, kind)
        await b.next_gui("show_click_notice")
        await b.flush()
        assert _cues(b.gui_since(mark, actions=_CUE)) == [True, False]


async def test_lc_42_walk_cue_carries_the_whole_walk_bound(tmp_path):
    """LC-42: the walking cue carries the longest time a walk can take.

    Rule: overlay_walk_cue with active true carries walk_timeout_ms equal to
    screen_read_timeout_ms plus response_timeout_ms plus the 5000 ms wait for
    the end of an open dictation sentence, so the GUI fallback timer does not
    remove the cue during a walk that is still allowed to finish
    (wh-overlay-slow-uia-stale-badges.3).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        await b.next_frame("start_overlay_walk")
        await b.flush()
        (cue,) = b.gui_since(mark, actions=_CUE)
        assert cue["active"] is True
        assert cue["walk_timeout_ms"] == _WALK_CUE_BOUND_MS


# ---------------------------------------------------------------------------
# The screen read limit
# ---------------------------------------------------------------------------

# Longer than response_timeout_ms 3000 and shorter than screen_read_timeout_ms
# 10000 (config.toml.example).
_BETWEEN_REPLY_AND_READ_LIMIT_S = 5.0


async def test_lc_43_a_walk_waits_for_the_screen_read_limit(tmp_path):
    """LC-43: a walk reply that comes after the click reply limit still paints.

    Rule: Logic waits screen_read_timeout_ms for a start_overlay_walk reply,
    not response_timeout_ms, so a slow screen read that answers inside that
    limit paints its numbers (wh-overlay-slow-uia-stale-badges.3).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        await b.advance(_BETWEEN_REPLY_AND_READ_LIMIT_S)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-slow"))
        paint = await b.next_gui("paint_overlay")
        assert paint["snapshot_id"] == "snap-slow"
        assert _pair(paint) == _pair(walk.params)


# ---------------------------------------------------------------------------
# One error notification for one fault
# ---------------------------------------------------------------------------


async def test_lc_44_unanswered_walk_raises_one_error_notification(tmp_path):
    """LC-44: one unanswered walk raises one Windows error notification.

    Rule: when start_overlay_walk gets no reply, the request layer logs the
    fault at ERROR once, and Logic logs its own line about the timeout below
    ERROR, so the user sees one error notification, not two
    (wh-duplicate-error-popup).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk = await b.next_frame("start_overlay_walk")
        before = len(b.notifications)
        await _fail_walk(b, walk, "timeout")
        await b.next_gui("show_click_notice")
        await b.flush()
        assert len(b.notifications) - before == 1


@pytest.mark.parametrize("kind", ["execution_failed", "timeout"])
async def test_lc_45_failed_refresh_with_no_readable_foreground_keeps_the_numbers(
    tmp_path, kind,
):
    """LC-45: a failed refresh while the foreground cannot be read keeps them.

    Rule: a refresh fails or times out while Windows reports no foreground
    window, so Logic cannot tell whether the numbered window left (LC-22) or
    stayed (LC-21). Logic keeps the numbers as LC-21 does: it sends no
    clear_overlay, no unpin_snapshot and no notice, and 'click N' still
    clicks the list on screen (wh-overlay-failed-focus-read-keeps-numbers).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        # No foreground window (GetForegroundWindow answers 0).
        b.desktop.foreground = 0
        await _fail_walk(b, refresh, kind)
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert set(_actions(gui)) <= {"overlay_lease_renew"}
        b.desktop.foreground = NUMBERED_WINDOW.hwnd
        await b.say("click one")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 1)


# ---------------------------------------------------------------------------
# A deadline that is due as a same-pair transition commits
# ---------------------------------------------------------------------------


async def test_lc_48_paint_deadline_due_as_the_painted_ack_commits_closes_nothing(
    tmp_path,
):
    """LC-48: a paint deadline already due when the painted ack commits.

    Rule: the first paint's 1000 ms deadline comes due in the same loop turn
    as the GUI's painted acknowledgement commits the painted state at the
    same pair, so the deadline's callback runs after the commit and before
    the commit's timer cancel. The callback feeds no timeout: Logic sends no
    clear_overlay and no unpin_snapshot, shows no notice, raises no error
    notification, and 'click N' clicks the painted list
    (wh-overlay-timer-cancel-race).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        paint = await b.next_gui("paint_overlay")
        await b.flush()
        mark = b.mark()
        before = len(b.notifications)
        await b.send_gui_as_time_passes(
            paint_ack(
                "painted", paint["overlay_session_id"],
                paint["paint_generation"], paint.get("snapshot_id"),
            ),
            _PAST_PAINT_DEADLINE_S,
        )
        frames, gui = await b.quiet(mark, 0.2)
        assert "unpin_snapshot" not in _actions(frames)
        assert "clear_overlay" not in _actions(gui)
        assert "show_click_notice" not in _actions(gui)
        assert len(b.notifications) == before
        await b.say("click two")
        await b.flush()
        assert "show_click_notice" not in _actions(b.gui_since(mark))
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 2)


# ---------------------------------------------------------------------------
# Effect batches leave Logic in the order the machine scheduled them
# ---------------------------------------------------------------------------


@pytest.fixture
def manual_pins(monkeypatch):
    """Turn off the harness's automatic ok reply to pin_snapshot."""

    monkeypatch.setattr(
        harness, "_AUTO_ANSWERED",
        frozenset({"unpin_snapshot", "refresh_overlay_snapshot"}),
    )


def _pin_ok_reply(params: dict) -> dict:
    """An ok pin_snapshot reply from the Input process, through PinSnapshotResponse."""

    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=params.get("overlay_session_id", 0),
        snapshot_id=params.get("snapshot_id", ""),
        pinned=True,
    ).to_dict()


async def test_lc_49_hide_while_the_pin_is_unanswered_clears_after_the_paint(
    tmp_path, manual_pins,
):
    """LC-49: 'hide numbers' while Input has not answered pin_snapshot.

    Rule: Logic sends the effects of one machine transition as one batch, and
    a later batch waits until the earlier batch is sent. The walk reply's
    batch sends pin_snapshot and waits for its reply before it sends
    paint_overlay. 'hide numbers' said in that wait sends nothing until the
    pin reply arrives. Then the paint for the closed overlay is not sent,
    GUI gets clear_overlay, and Input gets unpin_snapshot after
    pin_snapshot, so no numbers reach the screen (wh-n29v.95).
    """

    async with overlay_boundary(tmp_path) as b:
        await b.flush()
        mark = b.mark()
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        pin = await b.next_frame("pin_snapshot")
        await b.say("hide numbers")
        await b.flush()
        assert "clear_overlay" not in _actions(b.gui_since(mark))
        assert "unpin_snapshot" not in _actions(b.frames_since(mark))
        b.reply(pin, _pin_ok_reply(pin.params))
        await b.next_gui("clear_overlay")
        await b.next_frame("unpin_snapshot")
        frames, gui = await b.quiet(mark, 0.2)
        overlay_gui = [
            a for a in _actions(gui) if a in ("paint_overlay", "clear_overlay")
        ]
        assert overlay_gui == ["clear_overlay"]
        pins = [
            a for a in _actions(frames) if a in ("pin_snapshot", "unpin_snapshot")
        ]
        assert pins == ["pin_snapshot", "unpin_snapshot"]


# A screen-read limit short enough that the refresh deadline (this limit
# plus the 250 ms margin, armed when the walk reply arrives) ends inside
# the pin_snapshot reply limit (response_timeout_ms 3000).
_SHORT_SCREEN_READ_MS = 1000
# Past that refresh deadline and inside the pin reply limit.
_PAST_SHORT_REFRESH_DEADLINE_S = 1.5


async def test_lc_50_refresh_paint_held_past_the_refresh_deadline_is_not_sent(
    tmp_path, manual_pins,
):
    """LC-50: the refresh deadline passes while the new list's pin is unanswered.

    Rule: a refresh walk returned a new list, and Logic sent pin_snapshot for
    it. The refresh deadline passes before Input answers the pin, so the
    overlay falls back to the list on screen. When the pin reply then
    arrives, Logic sends no paint_overlay for the new list, and 'click N'
    clicks the list on screen (wh-overlay-slow-uia-stale-badges.19).
    The deadline is armed when the walk reply arrives, so this can occur
    only when screen_read_timeout_ms plus 250 ms is less than
    response_timeout_ms.
    """

    config = write_config(tmp_path, screen_read_timeout_ms=_SHORT_SCREEN_READ_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        await b.say("show numbers")
        await b.answer_walk(lambda p: walk_ok_reply(p, snapshot_id="snap-1"))
        first_pin = await b.next_frame("pin_snapshot")
        b.reply(first_pin, _pin_ok_reply(first_pin.params))
        paint = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint)
        await b.flush()
        await b.say("show numbers")
        refresh = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        b.reply(refresh, walk_ok_reply(refresh.params, snapshot_id="snap-2"))
        new_pin = await b.next_frame("pin_snapshot")
        assert new_pin.params["snapshot_id"] == "snap-2"
        await b.advance(_PAST_SHORT_REFRESH_DEADLINE_S)
        b.reply(new_pin, _pin_ok_reply(new_pin.params))
        _frames, gui = await b.quiet(mark, 0.3)
        assert [
            m.get("snapshot_id") for m in gui if m.get("action") == "paint_overlay"
        ] == []
        await b.say("click one")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["snapshot_id"] == "snap-1"
        assert click.params["item_id"] == item_id("snap-1", 1)
