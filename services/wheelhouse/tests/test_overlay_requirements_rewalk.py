"""Re-walk trigger requirements of the numbered overlay, at the process boundary.

Bead: wh-overlay-refactor-pilot.1. Each test pins one CONFIRMED row of the
table "Re-walk triggers: focus, menu, Pattern Manager tree change, browser
refresh" in docs/design/overlay-requirements.md and is named after its
requirement id. NOT_REQUIRED rows (RW-9, RW-10, RW-11, RW-17, RW-18,
RW-20, RW-22, RW-27) have no test here. RW-14 was a CODE_ONLY row until David
confirmed it on 2026-11-06 ("keep today's behavior"). RW-30 is a
CONFLICT row and the paused part of every row is left out: nothing in the
shipped app sends the microphone pause or resume event to the overlay, so the
paused state cannot be reached from a process boundary.

Every test drives a real ``LogicController`` built through its constructor
(tests/overlay_boundary_harness.py says how) and asserts only on what leaves
the Logic process: frames for the Input process and dicts for the GUI process.
No test names a private member of ``LogicController``, patches one of its
methods, or reads its state.

TIME. The focus debounce and the browser refresh age read the real monotonic
clock (main.py imports ``time`` inside those functions, so the harness's
offset clock does not reach them), while the settle timer and the keepalive
tick run on the loop clock. So the debounce tests wait in real time, with a
debounce of 800 ms so that a check made just after a coalesced event cannot
overlap the window end, and the browser refresh tests set
``overlay_browser_refresh_seconds`` to 1 and wait just over one real second
before they advance the loop clock to the keepalive tick. Tests that send
several events and are not about the debounce set
``overlay_focus_debounce_ms`` to 0, so every event fires.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager

import pytest

from services.wheelhouse.shared.pattern_manager_tree_changed import (
    PatternManagerTreeChangedEvent,
)
from tests.overlay_boundary_harness import (
    NUMBERED_WINDOW,
    OTHER_WINDOW,
    FakeFocusHooks,
    Window,
    item_id,
    overlay_boundary,
    paint_ack,
    settle,
    walk_ok_reply,
    write_config,
)

pytestmark = pytest.mark.journey

# Win32 menu pop-up event ids (winuser.h).
_MENUPOPUPSTART = 0x0006
_MENUPOPUPEND = 0x0007

# A debounce long enough that a check right after a coalesced event ends well
# inside the window, and short enough to keep each test near two seconds.
_DEBOUNCE_MS = 800
_DEBOUNCE_S = _DEBOUNCE_MS / 1000.0
# The keepalive interval: snapshot_ttl_seconds 30 / 2.
_KEEPALIVE_INTERVAL_S = 15.0
# Longer than the first-walk deadline (screen_read_timeout_ms 10000 plus
# PRE_WALK_MARGIN_MS 250) and the walk request's own reply timeout.
_PAST_WALK_DEADLINE_S = 21.0

BROWSER_WINDOW = Window(
    hwnd=0x6C01, pid=6301, process_name="chrome.exe",
    class_name="Chrome_WidgetWin_1", create_time_s=1_700_000_200.0,
)
# A dialog the numbered application owns: a top-level window of its own in the
# same process (ruling R-B counts it as a change of window).
OWNED_DIALOG = Window(
    hwnd=0x4A77, pid=NUMBERED_WINDOW.pid,
    process_name=NUMBERED_WINDOW.process_name, class_name="#32770",
    create_time_s=1_700_000_010.0,
)
TOOLTIP_WINDOW = Window(
    hwnd=0x7A01, pid=OTHER_WINDOW.pid, process_name=OTHER_WINDOW.process_name,
    class_name="tooltips_class32", create_time_s=1_700_000_300.0,
)
# A child window inside the numbered window (its GA_ROOT ancestor is the
# numbered window; ``_window_inside`` makes the fake desktop say so).
CHILD_WINDOW = Window(
    hwnd=0x4A02, pid=NUMBERED_WINDOW.pid,
    process_name=NUMBERED_WINDOW.process_name, class_name="JourneyChild",
    create_time_s=1_700_000_000.0,
)
# A window of a WheelHouse process: this test process stands in for Logic.
WHEELHOUSE_WINDOW = Window(
    hwnd=0x8A01, pid=os.getpid(), process_name="python.exe",
    class_name="Qt5QWindowIcon", create_time_s=1_700_000_400.0,
)
# A handle the desktop does not know: every Win32 read of it fails.
UNREADABLE_HWND = 0x9F01
# The Pattern Manager closed and opened again: a new top-level handle.
REOPENED_PATTERN_MANAGER = Window(
    hwnd=0x4C01, pid=4401, process_name="pythonw.exe",
    class_name="Qt5QWindowIcon", create_time_s=1_700_000_500.0,
)


def _pair(message: dict) -> tuple:
    return (message["overlay_session_id"], message["paint_generation"])


def _actions(messages) -> list:
    return [getattr(m, "action", None) or m["action"] for m in messages]


def _tree(hwnd: int, sequence: int) -> dict:
    """A pattern_manager_tree_changed message, through the production schema."""

    return PatternManagerTreeChangedEvent(hwnd=hwnd, sequence=sequence).to_dict()


@contextmanager
def _window_inside(child_hwnd: int, root_hwnd: int):
    """Make the fake desktop report ``child_hwnd`` as inside ``root_hwnd``.

    A Win32 seam (``win32gui.GetAncestor``), not a member of
    ``LogicController``. The harness's fake answers every window as its own
    root; this wraps that answer for one handle and puts it back on exit,
    before the harness restores the real call.
    """

    import win32gui

    outer = win32gui.GetAncestor

    def _ancestor(hwnd, flag):
        if int(hwnd) == child_hwnd:
            return root_hwnd
        return outer(hwnd, flag)

    win32gui.GetAncestor = _ancestor
    try:
        yield
    finally:
        win32gui.GetAncestor = outer


async def _foreground(b, window_or_hwnd, *, bring_to_front: bool = True) -> None:
    """Windows reports a new foreground window (and, normally, it is in front)."""

    if isinstance(window_or_hwnd, Window):
        if bring_to_front:
            b.desktop.bring_to_front(window_or_hwnd)
        hwnd = window_or_hwnd.hwnd
    else:
        hwnd = window_or_hwnd
    b.hooks.foreground(hwnd)
    await settle()


async def _nothing_until_the_window_end(b, mark, fired_at: float) -> None:
    """Move the clock to just before the debounce window ends; nothing is sent.

    ``fired_at`` is the clock time of the event that opened the window. The
    check stops 0.1 s short of the window end, so a trailing re-walk that
    fires early is seen here.
    """

    left = _DEBOUNCE_S - (b.clock.monotonic() - fired_at) - 0.1
    assert left > 0, f"the window already ended {-left:.3f}s ago"
    await b.advance(left)
    await b.flush()
    assert _actions(b.frames_since(mark)) == []
    assert _actions(b.gui_since(mark)) == []
    await b.advance(0.15)


async def _menu(b, event_id: int) -> None:
    b.hooks.menu_popup(event_id)
    await settle()


async def _send_tree(b, hwnd: int, sequence: int) -> None:
    b.send_gui(_tree(hwnd, sequence))
    await b.settle_gui()


async def _trigger(b, kind: str, *, sequence: int = 1) -> None:
    """One re-walk trigger over the numbered window."""

    if kind == "foreground":
        await _foreground(b, NUMBERED_WINDOW)
    elif kind == "foreground_other":
        await _foreground(b, OTHER_WINDOW)
    elif kind == "menu_start":
        await _menu(b, _MENUPOPUPSTART)
    elif kind == "menu_end":
        await _menu(b, _MENUPOPUPEND)
    elif kind == "tree":
        await _send_tree(b, NUMBERED_WINDOW.hwnd, sequence)
    else:  # pragma: no cover -- a typo in a parametrize list
        raise AssertionError(kind)


def _clears_before(b, mark, paint: dict) -> list:
    """GUI clear_overlay messages after ``mark`` and before ``paint``."""

    found = []
    for message in b.gui_since(mark):
        if message is paint:
            break
        if message["action"] == "clear_overlay":
            found.append(message)
    return found


async def _enter_error_state(b) -> dict:
    """Reach the error state the only way a boundary can.

    Copied from tests/test_overlay_requirements_lifecycle.py (a test module is
    not imported from another). The GUI acknowledges a paint at the CURRENT
    pair while the first walk is still in flight (no paint was ever sent): an
    invalid transition, which the machine answers by entering error. Two
    outputs show the state is error: the walk's own ok reply then paints
    nothing, and a bare number is refused with the numbers_not_showing notice.
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


async def _enter_post_click_settling(b) -> None:
    """Painted, then a badge click, with overlay_settle_after_click on.

    Needs overlay_keep_numbers on as well; with it off a badge click closes
    the overlay.

    Positive outputs that show the state: the click goes out as
    click_snapshot_item, the badges are cleared at once, and the settle read
    goes out as a start_overlay_walk.
    """

    paint = await b.show_numbers_painted("snap-1")
    await b.say("click two")
    click = await b.next_frame("click_snapshot_item")
    assert click.params["item_id"] == item_id("snap-1", 2)
    clear = await b.next_gui("clear_overlay")
    assert clear["overlay_session_id"] == paint["overlay_session_id"]
    await b.next_frame("start_overlay_walk")


# ---------------------------------------------------------------------------
# Foreground changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keep_numbers, window",
    [
        pytest.param(False, NUMBERED_WINDOW, id="keep-off-same-window"),
        pytest.param(True, OTHER_WINDOW, id="keep-on-other-window"),
    ],
)
async def test_rw_1_foreground_change_while_painted_refreshes(
    tmp_path, keep_numbers, window,
):
    """RW-1: a foreground event over a painted overlay starts a refresh walk.

    Rule: Logic sends Input a start_overlay_walk build (REFRESH) and keeps the
    old badges on the GUI until the new paint replaces them.
    """

    config = write_config(tmp_path, overlay_keep_numbers=keep_numbers)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        await _foreground(b, window)
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert _pair(paint2) == (sid, gen + 1)
        await b.flush()
        assert _clears_before(b, mark, paint2) == []
        unpins = [
            f.params["snapshot_id"] for f in b.frames_since(mark)
            if f.action == "unpin_snapshot"
        ]
        assert "snap-1" not in unpins


async def test_rw_2_foreground_during_first_walk_supersedes_without_clear(
    tmp_path,
):
    """RW-2 (walk in flight): a foreground event restarts the first walk.

    Rule: Logic clears the GUI badges only if a paint was dispatched, unpins
    the old snapshot, and sends Input a new start_overlay_walk (SUPERSEDE) at
    a new generation.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await _foreground(b, NUMBERED_WINDOW)
        walk2 = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert walk2.params["overlay_session_id"] == walk1.params["overlay_session_id"]
        assert walk2.params["paint_generation"] == walk1.params["paint_generation"] + 1
        frames, gui = await b.quiet(mark, 0.2)
        # No paint was sent and nothing was pinned: no clear, no unpin.
        assert "clear_overlay" not in _actions(gui)
        assert "unpin_snapshot" not in _actions(frames)


async def test_rw_2_foreground_during_first_paint_clears_unpins_and_restarts(
    tmp_path,
):
    """RW-2 (paint in flight): a foreground event clears, unpins, restarts.

    Rule: Logic clears the GUI badges only if a paint was dispatched, unpins
    the old snapshot, and sends Input a new start_overlay_walk (SUPERSEDE) at
    a new generation.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.answer_walk(
            lambda p: walk_ok_reply(p, snapshot_id="snap-1"),
        )
        paint1 = await b.next_gui("paint_overlay")
        await _foreground(b, NUMBERED_WINDOW)
        clear = await b.next_gui("clear_overlay")
        assert _pair(clear) == _pair(paint1)
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        walk2 = await b.next_frame("start_overlay_walk")
        assert walk2.params["overlay_session_id"] == walk1.params["overlay_session_id"]
        assert walk2.params["paint_generation"] == walk1.params["paint_generation"] + 1


@pytest.mark.parametrize(
    "keep_numbers, trigger",
    [
        pytest.param(False, "foreground", id="foreground-same-window"),
        pytest.param(True, "foreground_other", id="foreground-keep-on"),
        pytest.param(False, "menu_end", id="menu"),
        pytest.param(False, "tree", id="tree-change"),
    ],
)
async def test_rw_3_trigger_during_refresh_supersedes_and_keeps_old_badges(
    tmp_path, keep_numbers, trigger,
):
    """RW-3: a trigger during a refresh walk supersedes it; old badges stay.

    Rule: Logic supersedes the refresh: sends a new walk request at a new
    generation and keeps the visible old badges pinned.
    """

    config = write_config(tmp_path, overlay_keep_numbers=keep_numbers)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await b.say("show numbers")
        refresh1 = await b.next_frame("start_overlay_walk")
        assert _pair(refresh1.params) == (sid, gen + 1)
        mark = b.mark()
        await _trigger(b, trigger)
        refresh2 = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert _pair(refresh2.params) == (sid, gen + 2)
        b.reply(refresh2, walk_ok_reply(refresh2.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        await b.flush()
        assert _clears_before(b, mark, paint2) == []
        assert "snap-1" not in [
            f.params["snapshot_id"] for f in b.frames_since(mark)
            if f.action == "unpin_snapshot"
        ]


@pytest.mark.parametrize("state", ["closed", "post_click_settling", "error"])
@pytest.mark.parametrize(
    "trigger", ["foreground_other", "menu_start", "menu_end"],
)
async def test_rw_4_trigger_in_idle_states_sends_nothing(
    tmp_path, state, trigger,
):
    """RW-4: a trigger in closed, post_click_settling or error sends nothing.

    Rule: Logic sends nothing to Input or GUI. (The paused state is out of
    reach; the tree-change trigger in these states is RW-21.)
    """

    # keep_numbers on: with it off a badge click removes the numbers and the
    # overlay closes instead of settling (main.py click-ok path).
    config = write_config(
        tmp_path, overlay_settle_after_click=True, overlay_keep_numbers=True,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        if state == "closed":
            await b.show_numbers_painted("snap-1")
            await b.say("hide numbers")
            await b.next_gui("clear_overlay")
            await b.next_frame("unpin_snapshot")
        elif state == "post_click_settling":
            await _enter_post_click_settling(b)
        else:
            await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        await _trigger(b, trigger)
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        if state == "closed":
            # The same boundary still answers a show.
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")


@pytest.mark.parametrize("phase", ["painted", "refresh_in_flight"])
@pytest.mark.parametrize(
    "window", [OTHER_WINDOW, OWNED_DIALOG], ids=["other-app", "owned-dialog"],
)
async def test_rw_5_change_of_window_with_keep_off_clears_and_unpins(
    tmp_path, phase, window,
):
    """RW-5: keep_numbers off and a different top-level window comes in front.

    Rule: Logic sends clear_overlay to the GUI, unpins the snapshot to Input,
    and sends no walk. A dialog owned by the numbered window counts as a
    change of window.
    """

    async with overlay_boundary(
        tmp_path, windows=(NUMBERED_WINDOW, OTHER_WINDOW, OWNED_DIALOG),
    ) as b:
        paint = await b.show_numbers_painted("snap-1")
        if phase == "refresh_in_flight":
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")
        await b.flush()
        mark = b.mark()
        await _foreground(b, window)
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint["overlay_session_id"]
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        frames, _gui = await b.quiet(mark, 0.3)
        assert "start_overlay_walk" not in _actions(frames)


@pytest.mark.parametrize(
    "target",
    ["tooltip", "numbered", "child", "wheelhouse", "unreadable"],
)
async def test_rw_6_non_window_change_with_keep_off_refreshes(tmp_path, target):
    """RW-6: a foreground event that is not a change of window refreshes.

    Rule: with overlay_keep_numbers off, a tooltip, the numbered window itself
    or a child inside it, a WheelHouse process window, or an unreadable window
    keeps the numbers and does the normal refresh walk instead of clearing.
    """

    windows = (
        NUMBERED_WINDOW, OTHER_WINDOW, TOOLTIP_WINDOW, CHILD_WINDOW,
        WHEELHOUSE_WINDOW,
    )
    async with overlay_boundary(tmp_path, windows=windows) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        with _window_inside(CHILD_WINDOW.hwnd, NUMBERED_WINDOW.hwnd):
            if target == "tooltip":
                await _foreground(b, TOOLTIP_WINDOW)
            elif target == "numbered":
                await _foreground(b, NUMBERED_WINDOW)
            elif target == "child":
                await _foreground(b, CHILD_WINDOW)
            elif target == "wheelhouse":
                await _foreground(b, WHEELHOUSE_WINDOW)
            else:
                await _foreground(b, UNREADABLE_HWND)
            walk = await b.next_frame("start_overlay_walk")
            assert _pair(walk.params) == (sid, gen + 1)
            frames, gui = await b.quiet(mark, 0.2)
        assert "clear_overlay" not in _actions(gui)
        assert "unpin_snapshot" not in _actions(frames)


@pytest.mark.parametrize("phase", ["walk_in_flight", "paint_in_flight"])
async def test_rw_7_other_window_during_first_build_restarts_not_dismisses(
    tmp_path, phase,
):
    """RW-7: keep_numbers off; another window during the first build restarts.

    Rule: a foreground event while the first walk or paint is in flight is an
    ordinary focus change (restart), never a dismissal: the session goes on
    with a new walk. (The closed case is RW-4; paused is out of reach.)
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        if phase == "walk_in_flight":
            walk1 = await b.next_frame("start_overlay_walk")
        else:
            walk1 = await b.answer_walk(
                lambda p: walk_ok_reply(p, snapshot_id="snap-1"),
            )
            await b.next_gui("paint_overlay")
        await _foreground(b, OTHER_WINDOW)
        walk2 = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert walk2.params["overlay_session_id"] == walk1.params["overlay_session_id"]
        assert walk2.params["paint_generation"] == walk1.params["paint_generation"] + 1
        # Not a dismissal: the session's new walk still paints.
        b.reply(walk2, walk_ok_reply(walk2.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert _pair(paint2) == _pair(walk2.params)


# ---------------------------------------------------------------------------
# The debounce and its trailing re-walk
# ---------------------------------------------------------------------------


async def test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk(
    tmp_path,
):
    """RW-8: foreground events inside the debounce window send nothing now.

    Rule: Logic sends nothing now and arms one trailing re-walk for the end of
    the debounce window.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        fired_at = b.clock.monotonic()
        await _foreground(b, NUMBERED_WINDOW)
        walk1 = await b.next_frame("start_overlay_walk")
        assert _pair(walk1.params) == (sid, gen + 1)
        mark = b.mark()
        await _foreground(b, NUMBERED_WINDOW)
        await _foreground(b, NUMBERED_WINDOW)
        frames, gui = await b.quiet(mark, 0.2)
        assert b.clock.monotonic() - fired_at < _DEBOUNCE_S
        assert frames == []
        assert gui == []
        await _nothing_until_the_window_end(b, mark, fired_at)
        walk2 = await b.next_frame("start_overlay_walk")
        assert b.clock.monotonic() - fired_at >= _DEBOUNCE_S - 0.05
        assert _pair(walk2.params) == (sid, gen + 2)
        await b.advance(_DEBOUNCE_S)
        frames, _gui = await b.quiet(mark, 0.1)
        assert _actions(frames) == ["start_overlay_walk"]


@pytest.mark.parametrize("trigger", ["foreground", "menu_end"])
async def test_rw_12_burst_ending_on_another_app_clears_instead_of_walking(
    tmp_path, trigger,
):
    """RW-12: a coalesced burst ending on another application clears.

    Rule: with overlay_keep_numbers off and numbers showing, when the window
    in front at the trailing timer is a different application window, the
    trailing re-walk clears the numbers (clear_overlay, unpin) instead of
    walking.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint = await b.show_numbers_painted("snap-1")
        await _trigger(b, trigger)
        await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await _foreground(b, OTHER_WINDOW)
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        await b.advance(_DEBOUNCE_S)
        clear = await b.next_gui("clear_overlay")
        assert clear["overlay_session_id"] == paint["overlay_session_id"]
        unpin = await b.next_frame("unpin_snapshot")
        assert unpin.params["snapshot_id"] == "snap-1"
        frames, _gui = await b.quiet(mark, 0.2)
        assert "start_overlay_walk" not in _actions(frames)


async def test_rw_13_burst_ending_on_numbered_window_refreshes(tmp_path):
    """RW-13: a coalesced burst that ends back on the numbered window refreshes.

    Rule: the trailing action is a normal refresh walk, not a clear.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await _foreground(b, NUMBERED_WINDOW)
        await b.next_frame("start_overlay_walk")
        mark = b.mark()
        await _foreground(b, OTHER_WINDOW)
        await _foreground(b, NUMBERED_WINDOW)
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        await b.advance(_DEBOUNCE_S)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (sid, gen + 2)
        frames, gui = await b.quiet(mark, 0.2)
        assert "clear_overlay" not in _actions(gui)
        assert "unpin_snapshot" not in _actions(frames)


# ---------------------------------------------------------------------------
# Menu pop-up events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_id", [_MENUPOPUPSTART, _MENUPOPUPEND], ids=["start", "end"],
)
async def test_rw_15_menu_popup_while_painted_refreshes(tmp_path, event_id):
    """RW-15: a menu pop-up start or end over a painted overlay refreshes.

    Rule: Logic starts a refresh walk (start_overlay_walk) so badges for the
    gone or new menu items are rebuilt; the old badges stay up until the new
    paint. Menu events never dismiss numbers on their own.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        await _menu(b, event_id)
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        await b.flush()
        assert _clears_before(b, mark, paint2) == []
        assert "unpin_snapshot" not in _actions(
            [f for f in b.frames_since(mark) if f.params.get("snapshot_id") == "snap-1"]
        )


async def test_rw_16_menu_close_inside_debounce_is_not_lost(tmp_path):
    """RW-16: a menu event inside the shared debounce window arms the re-walk.

    Rule: Logic sends nothing now and arms the trailing re-walk, so the final
    menu close is not lost. Foreground and menu events share one debounce
    clock.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        # A foreground event anchors the shared clock.
        await _foreground(b, NUMBERED_WINDOW)
        walk1 = await b.next_frame("start_overlay_walk")
        assert _pair(walk1.params) == (sid, gen + 1)
        mark = b.mark()
        await _menu(b, _MENUPOPUPSTART)
        await _menu(b, _MENUPOPUPEND)
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        await b.advance(_DEBOUNCE_S)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (sid, gen + 2)
        await b.advance(_DEBOUNCE_S)
        frames, _gui = await b.quiet(mark, 0.1)
        assert _actions(frames) == ["start_overlay_walk"]


# ---------------------------------------------------------------------------
# Pattern Manager tree changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["painted", "refresh_in_flight"])
async def test_rw_19_tree_change_for_the_pinned_window_refreshes(
    tmp_path, phase,
):
    """RW-19: pattern_manager_tree_changed for the numbered window refreshes.

    Rule: with the overlay painted or refresh_in_flight, the handle equal to
    the pinned window handle, and a newer sequence, Logic starts a refresh
    walk (start_overlay_walk) for the Pattern Manager window.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        expected_gen = gen + 1
        if phase == "refresh_in_flight":
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")
            expected_gen = gen + 2
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 1)
        walk = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert _pair(walk.params) == (sid, expected_gen)
        frames, gui = await b.quiet(mark, 0.2)
        assert "clear_overlay" not in _actions(gui)
        assert _actions(frames) == ["start_overlay_walk"]


@pytest.mark.parametrize("state", ["closed", "post_click_settling", "error"])
async def test_rw_21_tree_change_in_idle_states_is_dropped(tmp_path, state):
    """RW-21: pattern_manager_tree_changed in an idle state is dropped.

    Rule: in closed, post_click_settling or error state Logic drops it and
    sends nothing. (Paused is out of reach.)
    """

    # keep_numbers on: with it off a badge click removes the numbers and the
    # overlay closes instead of settling (main.py click-ok path).
    config = write_config(
        tmp_path, overlay_settle_after_click=True, overlay_keep_numbers=True,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        if state == "closed":
            await b.show_numbers_painted("snap-1")
            await b.say("hide numbers")
            await b.next_gui("clear_overlay")
            await b.next_frame("unpin_snapshot")
        elif state == "post_click_settling":
            await _enter_post_click_settling(b)
        else:
            await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 1)
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        if state == "closed":
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")


async def test_rw_23_old_sequence_dropped_and_new_handle_restarts_it(tmp_path):
    """RW-23: a tree change at or below the last applied sequence is dropped.

    Rule: for the same handle as the last applied event and a sequence at or
    below the last applied one, Logic drops it and sends nothing; a different
    handle restarts the sequence so its first event is accepted.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=0)
    async with overlay_boundary(
        tmp_path,
        config_path=config,
        windows=(NUMBERED_WINDOW, OTHER_WINDOW, REOPENED_PATTERN_MANAGER),
    ) as b:
        await b.show_numbers_painted("snap-1")
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 5)
        await b.next_frame("start_overlay_walk")
        await b.flush()
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 5)
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 4)
        frames, gui = await b.quiet(mark, 0.3)
        assert frames == []
        assert gui == []
        # A newer sequence for the same window is still accepted.
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 6)
        await b.next_frame("start_overlay_walk", timeout_s=2.0)

        # The dialog closes and opens again: a new handle whose counter
        # starts again at 1, below the old window's 6.
        await b.say("hide numbers")
        await b.next_gui("clear_overlay")
        b.desktop.bring_to_front(REOPENED_PATTERN_MANAGER)
        paint = await b.show_numbers_painted(
            "snap-r", read_from=REOPENED_PATTERN_MANAGER,
        )
        mark = b.mark()
        await _send_tree(b, REOPENED_PATTERN_MANAGER.hwnd, 1)
        walk = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert walk.params["overlay_session_id"] == paint["overlay_session_id"]
        assert walk.params["paint_generation"] == paint["paint_generation"] + 1


async def test_rw_24_typing_burst_inside_debounce_yields_one_walk(tmp_path):
    """RW-24: accepted tree changes inside the debounce window yield one walk.

    Rule: Logic sends nothing now, records the sequence as applied, and the
    trailing re-walk fires at the window end; a burst of typing yields one
    walk.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 1)
        walk1 = await b.next_frame("start_overlay_walk")
        assert _pair(walk1.params) == (sid, gen + 1)
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 2)
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 3)
        frames, gui = await b.quiet(mark, 0.1)
        assert frames == []
        assert gui == []
        await b.advance(_DEBOUNCE_S)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (sid, gen + 2)
        # Past the window again: sequence 3 was recorded as applied while the
        # burst was coalesced, so a repeat of it is dropped ...
        await b.advance(_DEBOUNCE_S + 0.1)
        await b.flush()
        assert _actions(b.frames_since(mark)) == ["start_overlay_walk"]
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 3)
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []
        # ... and the next sequence walks at once.
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 4)
        walk3 = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert _pair(walk3.params) == (sid, gen + 3)


async def test_rw_40_tree_change_after_a_foreground_event_shares_its_debounce(
    tmp_path,
):
    """RW-40: a tree change inside a foreground event's window is coalesced.

    Rule: the Pattern Manager tree change and the foreground event share one
    debounce clock. A tree change that arrives inside the window a foreground
    event opened sends nothing now, and one trailing re-walk fires at the
    window end.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=_DEBOUNCE_MS)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        fired_at = b.clock.monotonic()
        await _foreground(b, NUMBERED_WINDOW)
        walk1 = await b.next_frame("start_overlay_walk")
        assert _pair(walk1.params) == (sid, gen + 1)
        mark = b.mark()
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 1)
        frames, gui = await b.quiet(mark, 0.2)
        assert b.clock.monotonic() - fired_at < _DEBOUNCE_S
        assert frames == []
        assert gui == []
        await _nothing_until_the_window_end(b, mark, fired_at)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (sid, gen + 2)
        await b.advance(_DEBOUNCE_S)
        frames, _gui = await b.quiet(mark, 0.1)
        assert _actions(frames) == ["start_overlay_walk"]


# ---------------------------------------------------------------------------
# Browser proactive refresh
# ---------------------------------------------------------------------------


async def test_rw_25_stale_paint_over_browser_refreshes_at_keepalive_tick(
    tmp_path,
):
    """RW-25: a painted overlay over a browser refreshes on the keepalive tick.

    Rule: when the latest paint is at least overlay_browser_refresh_seconds
    old, Logic starts a refresh walk (start_overlay_walk) with no spoken
    command; the real delay is rounded up to the 15 s keepalive tick.
    """

    config = write_config(tmp_path, overlay_browser_refresh_seconds=1)
    async with overlay_boundary(
        tmp_path, config_path=config,
        windows=(BROWSER_WINDOW, NUMBERED_WINDOW, OTHER_WINDOW),
    ) as b:
        paint1 = await b.show_numbers_painted(
            "snap-1", read_from=BROWSER_WINDOW,
        )
        sid, gen = _pair(paint1)
        mark = b.mark()
        # Older than the 1 s window, but no keepalive tick yet: no walk.
        await b.advance(1.1)
        frames, gui = await b.quiet(mark, 0.05)
        assert frames == []
        assert gui == []
        await b.advance(_KEEPALIVE_INTERVAL_S)
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(
            walk.params, snapshot_id="snap-2", read_from=BROWSER_WINDOW,
        ))
        paint2 = await b.next_gui("paint_overlay")
        await b.flush()
        assert _clears_before(b, mark, paint2) == []


@pytest.mark.parametrize(
    "case", ["not_browser", "refresh_off", "paint_too_young"],
)
async def test_rw_26_keepalive_tick_without_conditions_starts_no_walk(
    tmp_path, case,
):
    """RW-26: a keepalive tick that does not meet the conditions walks nothing.

    Rule: when the tracked window is not a browser process, or the refresh is
    off, or the paint is younger than the window, the tick starts no
    proactive refresh walk. (The paused case is out of reach.)
    """

    settings = {
        "not_browser": {"overlay_browser_refresh_seconds": 1},
        "refresh_off": {"overlay_browser_refresh_seconds": 0},
        # The first tick comes 15 s after the paint, so a 20 s window keeps
        # the paint younger than the window at that tick.
        "paint_too_young": {"overlay_browser_refresh_seconds": 20},
    }[case]
    window = NUMBERED_WINDOW if case == "not_browser" else BROWSER_WINDOW
    config = write_config(tmp_path, **settings)
    async with overlay_boundary(
        tmp_path, config_path=config,
        windows=(window, OTHER_WINDOW),
    ) as b:
        paint = await b.show_numbers_painted("snap-1", read_from=window)
        if case != "paint_too_young":
            await b.advance(1.1)
        mark = b.mark()
        await b.advance(_KEEPALIVE_INTERVAL_S)
        # Positive output: the tick did run.
        renew = await b.next_gui("overlay_lease_renew")
        assert _pair(renew) == _pair(paint)
        frames, _gui = await b.quiet(mark, 0.3)
        assert frames == []


# ---------------------------------------------------------------------------
# Mouse clicks and wheel steps (wh-overlay-refresh-after-mouse)
# ---------------------------------------------------------------------------

# The quiet time after the last click or wheel step
# (numbered_overlay/controller.py _OVERLAY_POINTER_SETTLE_S).
_POINTER_SETTLE_S = 0.5
# Loop time to advance in a step that must stay inside the quiet time. The
# real time each step takes (thread joins, settle pauses) is added to it, so
# it is well below the quiet time.
_INSIDE_SETTLE_S = 0.25
_PAST_SETTLE_S = _POINTER_SETTLE_S + 0.1


def _mouse_burst(b, source: str) -> None:
    """One burst of mouse input, from the hook thread."""

    if source == "user-click":
        b.mouse.click(count=2)
    elif source == "user-wheel":
        b.mouse.wheel(steps=3)
    elif source == "scroll-command-wheel":
        # A WheelHouse scroll command: the Input process sends wheel events,
        # which Windows marks as injected.
        b.mouse.wheel(injected=True, steps=3)
    elif source == "mixed":
        b.mouse.click()
        b.mouse.wheel(steps=2)
        b.mouse.click(injected=True)
    else:  # pragma: no cover -- a typo in a parametrize list
        raise AssertionError(source)


@pytest.mark.parametrize(
    "source", ["user-click", "user-wheel", "scroll-command-wheel", "mixed"],
)
async def test_rw_37_click_and_wheel_burst_refreshes_once_after_quiet_time(
    tmp_path, source,
):
    """RW-37: clicks and wheel steps over painted numbers give one refresh.

    Rule: while the numbers show, each click or wheel step restarts a quiet
    time of 500 ms; when it passes, Logic sends Input one start_overlay_walk
    (REFRESH) and keeps the old badges until the new paint. Nothing goes out
    before the quiet time ends, and a series of events gives one walk.
    """

    async with overlay_boundary(tmp_path) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        mark = b.mark()
        # settle() before each advance: the hook thread's posted call runs
        # at the current time, so the quiet time starts where the test says.
        _mouse_burst(b, source)
        await settle()
        await b.advance(_INSIDE_SETTLE_S)
        _mouse_burst(b, source)
        await settle()
        await b.advance(_INSIDE_SETTLE_S)
        # Past the quiet time of the first burst, inside that of the second.
        frames, gui = await b.quiet(mark, 0.05)
        assert frames == []
        assert gui == []
        await b.advance(_PAST_SETTLE_S - _INSIDE_SETTLE_S)
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert _pair(paint2) == (sid, gen + 1)
        await b.flush()
        assert _clears_before(b, mark, paint2) == []
        await b.gui_ack("painted", paint2)
        await b.advance(_PAST_SETTLE_S)
        frames, _gui = await b.quiet(mark, 0.05)
        assert _actions(frames).count("start_overlay_walk") == 1


@pytest.mark.parametrize("phase", ["walk_in_flight", "paint_in_flight"])
async def test_rw_37_input_during_first_build_refreshes_after_the_paint(
    tmp_path, phase,
):
    """RW-37 (first build): input while the numbers are being read waits.

    Rule: a click or wheel step during the first walk or paint does not
    restart that build; the refresh follows once the numbers are painted.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.say("show numbers")
        walk1 = await b.next_frame("start_overlay_walk")
        if phase == "paint_in_flight":
            b.reply(walk1, walk_ok_reply(walk1.params, snapshot_id="snap-1"))
            await settle()
        mark = b.mark()
        b.mouse.wheel(steps=2)
        # The quiet time starts now, so it ends while the build is in flight.
        await settle()
        await b.advance(_PAST_SETTLE_S)
        frames, _gui = await b.quiet(mark, 0.05)
        assert "start_overlay_walk" not in _actions(frames)
        if phase == "walk_in_flight":
            b.reply(walk1, walk_ok_reply(walk1.params, snapshot_id="snap-1"))
        paint1 = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint1)
        await b.advance(_PAST_SETTLE_S)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (
            paint1["overlay_session_id"], paint1["paint_generation"] + 1,
        )


@pytest.mark.parametrize("state", ["closed", "post_click_settling", "error"])
async def test_rw_38_mouse_input_in_idle_states_sends_nothing(tmp_path, state):
    """RW-38: clicks and wheel steps in closed, post_click_settling or error.

    Rule: Logic sends nothing to Input or GUI, at once or after the quiet
    time (the RW-4 rule for the mouse trigger). A hide closes the overlay
    and cancels a pending refresh. (The paused state is out of reach.)
    """

    config = write_config(
        tmp_path, overlay_settle_after_click=True, overlay_keep_numbers=True,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        if state == "closed":
            await b.show_numbers_painted("snap-1")
            # A refresh pending when the numbers are hidden is cancelled.
            b.mouse.wheel()
            await b.say("hide numbers")
            await b.next_gui("clear_overlay")
            await b.next_frame("unpin_snapshot")
        elif state == "post_click_settling":
            await _enter_post_click_settling(b)
        else:
            await _enter_error_state(b)
        await b.flush()
        mark = b.mark()
        b.mouse.click()
        b.mouse.wheel(steps=2)
        b.mouse.wheel(injected=True)
        await b.advance(_PAST_SETTLE_S)
        frames, gui = await b.quiet(mark, 0.1)
        assert frames == []
        assert gui == []
        if state == "closed":
            # The same boundary still answers a show.
            await b.say("show numbers")
            await b.next_frame("start_overlay_walk")


async def test_rw_38_hide_cancels_a_pending_mouse_refresh(tmp_path):
    """RW-38 (close): a mouse refresh pending at "hide numbers" is cancelled.

    Rule: closing the overlay cancels a pending mouse refresh, so it cannot
    reach the next session: a new "show numbers" inside the old quiet time
    paints once, and no refresh follows that paint.
    """

    async with overlay_boundary(tmp_path) as b:
        await b.show_numbers_painted("snap-1")
        b.mouse.wheel()
        # The refresh is armed now, while the numbers show.
        await settle()
        mark = b.mark()
        await b.say("hide numbers")
        # No wait: the new show follows at once, so the old quiet time ends
        # after the new session has started, not while the overlay is closed.
        paint2 = await b.show_numbers_painted("snap-2")
        await b.advance(_PAST_SETTLE_S)
        frames, gui = await b.quiet(mark, 0.1)
        # The new session's first walk and first paint, and nothing more.
        assert _actions(frames).count("start_overlay_walk") == 1
        assert _actions(gui).count("paint_overlay") == 1
        # Control: the new session's own mouse refresh still works.
        b.mouse.wheel()
        await b.advance(_PAST_SETTLE_S)
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (
            paint2["overlay_session_id"], paint2["paint_generation"] + 1,
        )


@pytest.mark.parametrize("when", ["during", "after", "pending"])
async def test_rw_39_number_click_starts_no_second_refresh(tmp_path, when):
    """RW-39: a WheelHouse number click keeps today's single refresh.

    Rule: the clicks that a number click sends (injected, while the click is
    in flight or within the quiet time after it ends) start no mouse
    refresh, and a mouse refresh pending when the number click ends is
    cancelled; overlay_keep_numbers and overlay_settle_after_click alone
    decide what follows the click. Afterwards a click counts again.
    """

    config = write_config(
        tmp_path, overlay_keep_numbers=True, overlay_settle_after_click=False,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        if when == "during":
            # The Input process's SendInput fallback clicks before it replies.
            b.mouse.injected_clicks_on_number_click = 1
        mark = b.mark()
        if when == "pending":
            # The user turns the wheel and says the number at once.
            b.mouse.wheel()
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-1", 2)
        # The number click's own refresh (CLICK_COMPLETE).
        walk = await b.next_frame("start_overlay_walk")
        assert _pair(walk.params) == (sid, gen + 1)
        if when == "after":
            # The hook delivers the fallback click after the reply arrived.
            b.mouse.click(injected=True)
            await settle()
        await b.advance(_PAST_SETTLE_S)
        b.reply(walk, walk_ok_reply(walk.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        await b.gui_ack("painted", paint2)
        await b.advance(_PAST_SETTLE_S)
        frames, _gui = await b.quiet(mark, 0.05)
        assert _actions(frames).count("start_overlay_walk") == 1
        # Control: past the quiet time a program-sent click counts again.
        b.mouse.click(injected=True)
        await b.advance(_PAST_SETTLE_S)
        walk2 = await b.next_frame("start_overlay_walk")
        assert _pair(walk2.params) == (sid, gen + 2)


# ---------------------------------------------------------------------------
# Stale replies and the wake of a superseded build
# ---------------------------------------------------------------------------


async def test_rw_32_stale_replies_ignored_and_superseded_send_is_woken(
    tmp_path,
):
    """RW-32: old-generation replies are ignored; a supersede wakes the send.

    Rule: a walk response, timeout, or paint ack stamped with an old session
    id or paint generation is ignored and nothing is sent; a trigger that
    supersedes a build wakes the parked old send so the new walk starts
    without waiting out the old read.
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=0)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        paint1 = await b.show_numbers_painted("snap-1")
        sid, gen = _pair(paint1)
        await _foreground(b, NUMBERED_WINDOW)
        walk1 = await b.next_frame("start_overlay_walk")
        assert _pair(walk1.params) == (sid, gen + 1)
        # walk1 is never answered. Its read limit is screen_read_timeout_ms
        # (10 s); the superseding walk goes out well before that.
        await _foreground(b, NUMBERED_WINDOW)
        walk2 = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert _pair(walk2.params) == (sid, gen + 2)

        # The old walk's late ok reply: no pin, no paint.
        await b.flush()
        mark = b.mark()
        b.reply(walk1, walk_ok_reply(walk1.params, snapshot_id="snap-old"))
        frames, gui = await b.quiet(mark, 0.3)
        assert "snap-old" not in [f.params.get("snapshot_id") for f in frames]
        assert "paint_overlay" not in _actions(gui)

        # The current walk still paints.
        b.reply(walk2, walk_ok_reply(walk2.params, snapshot_id="snap-2"))
        paint2 = await b.next_gui("paint_overlay")
        assert paint2["snapshot_id"] == "snap-2"
        await b.gui_ack("painted", paint2)
        await b.next_gui("overlay_lease_renew")
        await b.flush()

        # Old-pair paint acknowledgements: nothing is sent.
        mark = b.mark()
        for state in ("failed", "painted", "expired"):
            b.send_gui(paint_ack(state, sid, gen + 1, "snap-old"))
            b.send_gui(paint_ack(state, sid, gen, "snap-1"))
        await b.settle_gui()
        frames, gui = await b.quiet(mark, 0.2)
        assert frames == []
        assert gui == []

        # The old walk's deadline passes: no clear, no notice.
        mark = b.mark()
        await b.advance(_PAST_WALK_DEADLINE_S)
        await b.flush()
        assert set(_actions(b.gui_since(mark))) <= {"overlay_lease_renew"}
        assert b.frames_since(mark) == []
        # Still painted with the current list.
        await b.say("click two")
        click = await b.next_frame("click_snapshot_item")
        assert click.params["item_id"] == item_id("snap-2", 2)


# ---------------------------------------------------------------------------
# The overlay or clicking turned off by config
# ---------------------------------------------------------------------------

# The frame the speech path sends Input at the end of every utterance. It is
# not overlay output.
_SPEECH_BOOKKEEPING = frozenset({"end_utterance"})


@pytest.mark.parametrize(
    "click_settings",
    [
        pytest.param({"overlay_enabled": False}, id="overlay-disabled"),
        pytest.param({"enabled": False}, id="click-disabled"),
    ],
)
async def test_rw_14_overlay_off_registers_no_hook_and_drops_tree_changes(
    tmp_path, click_settings,
):
    """RW-14: with the overlay or clicking off, re-walk events send nothing.

    Rule: the overlay or click feature is disabled by config (click.enabled
    false or overlay_enabled_effective false) and any foreground, menu,
    tree-change, destroy, or settle event fires: Logic sends nothing to Input
    or GUI.

    What reaches the boundary in this config:
      * foreground, menu pop-up and window destroy events are Win32 hook
        callbacks, and Logic registers no focus hook at all, so Windows
        delivers none of them to Logic. The test asserts that no hook was
        registered; it cannot fire a callback for a hook that does not exist.
      * the settle timer is armed only by a coalesced foreground or menu
        event, so it cannot fire either.
      * a pattern_manager_tree_changed message comes from the GUI process on
        its own queue, so the test sends it, for the numbered window and for
        another window, at increasing sequences.

    Positive control in the same boundary: a click command afterwards reaches
    Input (overlay off) or shows the disabled notice (clicking off), so both
    channels carry output in this config.
    test_rw_14_control_overlay_on_each_trigger_walks is the control with both
    settings on.
    """

    config = write_config(
        tmp_path, overlay_focus_debounce_ms=0, **click_settings,
    )
    async with overlay_boundary(tmp_path, config_path=config) as b:
        assert FakeFocusHooks.instances == []
        await b.flush()
        frame_start, gui_start = len(b.frames), len(b.gui)
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 1)
        await _send_tree(b, NUMBERED_WINDOW.hwnd, 2)
        await _send_tree(b, OTHER_WINDOW.hwnd, 1)
        await asyncio.sleep(0.3)
        await settle()
        await b.flush()
        assert [
            f.action for f in b.frames[frame_start:]
            if f.action not in _SPEECH_BOOKKEEPING
        ] == []
        assert [
            m.get("action") for m in b.gui[gui_start:] if isinstance(m, dict)
        ] == []

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


@pytest.mark.parametrize(
    "trigger", ["foreground", "menu_start", "menu_end", "tree"],
)
async def test_rw_14_control_overlay_on_each_trigger_walks(tmp_path, trigger):
    """RW-14 control: with both settings on, each trigger reaches Logic.

    Rule (the control case of RW-14, config gate click.enabled=true and
    overlay_enabled_effective true): Logic registers its Win32 focus hook,
    and a foreground, menu pop-up or tree-change event over a painted
    overlay sends Input a start_overlay_walk. (The destroy event needs the
    paused state, which no boundary reaches; the settle refire is RW-8 and
    RW-16.)
    """

    config = write_config(tmp_path, overlay_focus_debounce_ms=0)
    async with overlay_boundary(tmp_path, config_path=config) as b:
        assert len(FakeFocusHooks.instances) == 1
        paint = await b.show_numbers_painted("snap-1")
        mark = b.mark()
        await _trigger(b, trigger)
        walk = await b.next_frame("start_overlay_walk", timeout_s=2.0)
        assert _pair(walk.params) == (
            paint["overlay_session_id"], paint["paint_generation"] + 1,
        )
        frames, _gui = await b.quiet(mark, 0.2)
        assert _actions(frames) == ["start_overlay_walk"]
