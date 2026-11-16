"""Boundary tests for area G2 of GuiManager: the overlay and grid window
messages (wh-full-refactor.7.1, stage A of place 4).

Each test names its row OG-n of the G2 notes. A test enters only through the
inputs that GuiManager receives in production: a Logic message on the state
queue (read by the queue pump), and the expiry of a timer that GuiManager
armed (the fake clock of tests/overlay_gui_g2_harness.py moves, and each due
timer fires). A test reads only what leaves GuiManager: the commands on the
queue to Logic, and the calls on the fake operating system seam of
tests/overlay_paint_seam_harness.py (windows made and destroyed). The
OverlayPaintWindowManager objects are the real ones.

No test names the attribute that holds a timer. A test does not tell the
lease timer from the teardown retry timer: it moves the clock and asserts the
effect.
"""

from __future__ import annotations

from queue import Queue

import pytest

from shared.clear_overlay import ClearOverlayEvent
from shared.paint_grid import PaintGridEvent
from shared.paint_grid_pin import PaintGridPinEvent
from shared.paint_overlay import PaintOverlayEvent
from tests.overlay_gui_g2_harness import gui_rig
from tests.overlay_paint_seam_harness import (
    PRIMARY,
    SECOND,
    FakeMonitors,
    badge_summary,
)

# GuiManager construction without real dialogs (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")

SECOND_MS = 1000
# The first lease without a renew (LC-30: "first lease 90 s without renew").
FIRST_LEASE_MS = 90 * SECOND_MS
# A span long enough for any number of teardown retries (each retry waits
# a few seconds); the tests do not depend on the retry interval.
RETRY_SPAN_MS = 10 * SECOND_MS
DAY_MS = 24 * 3600 * SECOND_MS

# One badge box on each fake monitor, as (x, y, width, height).
BOXES = ((200, 200, 120, 40), (2300, 300, 120, 40))


# --- inputs -------------------------------------------------------------------


def paint_msg(session: int, generation: int, snapshot_id: str = "snap-1") -> dict:
    return PaintOverlayEvent(
        overlay_session_id=session, paint_generation=generation,
        summary=badge_summary(BOXES, snapshot_id=snapshot_id),
    ).to_dict()


def clear_msg(session: int, generation: int) -> dict:
    return ClearOverlayEvent(
        overlay_session_id=session, paint_generation=generation,
    ).to_dict()


def renew_msg(session, generation, **extra) -> dict:
    message = {
        "action": "overlay_lease_renew",
        "overlay_session_id": session,
        "paint_generation": generation,
    }
    message.update(extra)
    return message


RESET = {"action": "reset_overlay"}


def grid_msg(monitor=PRIMARY) -> dict:
    return PaintGridEvent(
        monitor_left=monitor.left, monitor_top=monitor.top,
        monitor_width=monitor.width, monitor_height=monitor.height,
        left=monitor.left, top=monitor.top,
        width=monitor.width, height=monitor.height,
    ).to_dict()


def pin_msg(x: int = 300, y: int = 300) -> dict:
    return PaintGridPinEvent(x=x, y=y).to_dict()


CLEAR_GRID = {"action": "clear_grid"}


# --- outputs ------------------------------------------------------------------


def ack(state: str, session: int, generation: int) -> dict:
    """A teardown report (cleared or expired) as GuiManager sends it."""
    return {
        "action": "overlay_state_changed",
        "state": state,
        "overlay_session_id": session,
        "paint_generation": generation,
        "monitor_ids": [],
        "snapshot_id": None,
    }


def assert_paint_report(message: dict, state: str, session: int,
                        generation: int, snapshot_id="snap-1") -> None:
    assert message["action"] == "overlay_state_changed"
    assert message["state"] == state
    assert (message["overlay_session_id"], message["paint_generation"]) == (
        session, generation,
    )
    assert message["snapshot_id"] == snapshot_id


@pytest.fixture
def rig():
    with gui_rig(FakeMonitors(monitors=[PRIMARY, SECOND])) as fake:
        yield fake


def painted(rig, session=1, generation=1):
    """Paint badges at the pair (set-up); return the new window handles."""
    mark = len(rig.seam.windows)
    rig.send(paint_msg(session, generation))
    reports = rig.to_logic()
    assert len(reports) == 1
    assert_paint_report(reports[0], "painted", session, generation)
    handles = [w.hwnd for w in rig.seam.windows_since(mark)]
    assert len(handles) == len(BOXES)
    return handles


def live(rig) -> set:
    return {w.hwnd for w in rig.seam.live_windows()}


# --- OG-1 to OG-5: paint_overlay ---------------------------------------------


def test_og_1_accepted_paint_shows_badges_and_reports_painted(rig):
    rig.send(paint_msg(1, 1))

    reports = rig.to_logic()
    assert len(reports) == 1
    assert_paint_report(reports[0], "painted", 1, 1)
    assert len(reports[0]["monitor_ids"]) == 2
    assert len(rig.seam.live_windows()) == 2


def test_og_2_failed_paint_reports_failed_and_arms_the_lease(rig):
    rig.seam.create_fails = True

    rig.send(paint_msg(1, 1))

    reports = rig.to_logic()
    assert len(reports) == 1
    assert_paint_report(reports[0], "failed", 1, 1)
    rig.advance(FIRST_LEASE_MS)
    assert rig.to_logic() == [ack("expired", 1, 1)]


def test_og_3_lease_runs_out_after_90_s_and_removes_the_badges(rig):
    painted(rig, 1, 1)

    rig.advance(FIRST_LEASE_MS - 1)
    assert rig.to_logic() == []
    assert len(rig.seam.live_windows()) == 2

    rig.advance(1)
    assert rig.to_logic() == [ack("expired", 1, 1)]
    assert rig.seam.live_windows() == []

    rig.advance(DAY_MS)
    assert rig.to_logic() == []


def test_og_3b_newer_accepted_paint_restarts_the_lease_at_its_pair(rig):
    painted(rig, 1, 1)
    rig.advance(50 * SECOND_MS)
    rig.send(paint_msg(1, 2))  # a refresh; it can reuse the windows
    (report,) = rig.to_logic()
    assert_paint_report(report, "painted", 1, 2)

    rig.advance(40 * SECOND_MS)  # 90 s after the first paint
    assert rig.to_logic() == []

    rig.advance(50 * SECOND_MS)  # 90 s after the second paint
    assert rig.to_logic() == [ack("expired", 1, 2)]
    assert rig.seam.live_windows() == []


def test_og_4_stale_paint_reports_nothing_and_keeps_the_lease(rig):
    handles = painted(rig, 1, 2)
    rig.advance(50 * SECOND_MS)
    mark = len(rig.seam.windows)

    rig.send(paint_msg(1, 1))

    assert rig.to_logic() == []
    assert rig.seam.windows_since(mark) == []
    rig.advance(40 * SECOND_MS)  # 90 s after the accepted paint
    assert rig.to_logic() == [ack("expired", 1, 2)]
    assert live(rig).isdisjoint(handles)


@pytest.mark.parametrize("bad", [
    pytest.param({"action": "paint_overlay", "overlay_session_id": 1,
                  "paint_generation": 1}, id="no-summary"),
    pytest.param(dict(paint_msg(1, 1), paint_generation=True), id="bool-generation"),
])
def test_og_5_malformed_paint_shows_and_reports_nothing(rig, bad):
    rig.send(bad, grid_msg())

    assert rig.to_logic() == []
    # Only the grid window of the next message in the same pump run.
    assert len(rig.seam.live_windows()) == 1
    rig.advance(DAY_MS)
    assert rig.to_logic() == []


# --- OG-6 to OG-10: clear_overlay and the teardown retry ---------------------


def test_og_6_accepted_clear_removes_badges_reports_cleared_and_ends_the_lease(rig):
    painted(rig, 1, 1)

    rig.send(clear_msg(1, 1))

    assert rig.to_logic() == [ack("cleared", 1, 1)]
    assert rig.seam.live_windows() == []
    rig.advance(DAY_MS)
    assert rig.to_logic() == []


def test_og_7_stale_clear_reports_nothing_and_keeps_the_lease(rig):
    handles = painted(rig, 2, 5)

    rig.send(clear_msg(2, 4))

    assert rig.to_logic() == []
    assert live(rig) == set(handles)
    rig.advance(FIRST_LEASE_MS)
    assert rig.to_logic() == [ack("expired", 2, 5)]
    assert rig.seam.live_windows() == []


def test_og_8_incomplete_clear_reports_later_once_and_ends_the_lease(rig):
    handles = painted(rig, 1, 1)
    survivor = handles[0]
    rig.seam.destroy_fails = {survivor}

    rig.send(clear_msg(1, 1))
    assert rig.to_logic() == []
    assert live(rig) == {survivor}

    mark = len(rig.seam.destroy_calls)
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark).count(survivor) >= 2

    rig.seam.destroy_fails = set()
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("cleared", 1, 1)]
    assert rig.seam.live_windows() == []

    mark = len(rig.seam.destroy_calls)
    rig.advance(DAY_MS)
    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark) == []


def test_og_9_late_cleared_report_keeps_the_lease_of_a_newer_paint(rig):
    first = painted(rig, 1, 1)
    rig.seam.destroy_fails = {first[0]}
    rig.send(clear_msg(1, 1))
    assert rig.to_logic() == []
    second = painted(rig, 1, 2)

    rig.seam.destroy_fails = set()
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("cleared", 1, 1)]
    assert live(rig) == set(second)

    rig.advance(FIRST_LEASE_MS - RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("expired", 1, 2)]
    assert rig.seam.live_windows() == []


def test_og_10_malformed_clear_removes_nothing_and_keeps_the_lease(rig):
    handles = painted(rig, 1, 1)

    rig.send({"action": "clear_overlay", "overlay_session_id": 1})

    assert rig.to_logic() == []
    assert live(rig) == set(handles)
    rig.advance(FIRST_LEASE_MS)
    assert rig.to_logic() == [ack("expired", 1, 1)]


# --- OG-11 to OG-15: overlay_lease_renew --------------------------------------


@pytest.mark.parametrize("generation", [5, 3], ids=["armed-pair", "older-generation"])
def test_og_11_renew_extends_the_lease_and_expiry_keeps_the_armed_pair(rig, generation):
    painted(rig, 1, 5)
    rig.advance(80 * SECOND_MS)

    rig.send(renew_msg(1, generation, lease_ms=60 * SECOND_MS))

    rig.advance(60 * SECOND_MS - 1)  # 140 s after the paint, less 1 ms
    assert rig.to_logic() == []
    assert len(rig.seam.live_windows()) == 2
    rig.advance(1)
    assert rig.to_logic() == [ack("expired", 1, 5)]
    assert rig.seam.live_windows() == []


@pytest.mark.parametrize("session, generation", [
    pytest.param(2, 5, id="newer-session"),
    pytest.param(0, 9, id="older-session"),
    pytest.param(1, 6, id="newer-generation"),
    pytest.param(True, 5, id="bool-session"),
    pytest.param(1, True, id="bool-generation"),
    pytest.param("1", 5, id="text-session"),
    pytest.param(1, "5", id="text-generation"),
])
def test_og_12_renew_for_another_pair_is_ignored(rig, session, generation):
    painted(rig, 1, 5)
    rig.advance(80 * SECOND_MS)

    rig.send(renew_msg(session, generation, lease_ms=60 * SECOND_MS))

    rig.advance(10 * SECOND_MS)  # 90 s after the paint
    assert rig.to_logic() == [ack("expired", 1, 5)]
    assert rig.seam.live_windows() == []


@pytest.mark.parametrize("extra", [
    pytest.param({}, id="missing"),
    pytest.param({"lease_ms": 0}, id="zero"),
    pytest.param({"lease_ms": -5}, id="negative"),
    pytest.param({"lease_ms": True}, id="bool"),
    pytest.param({"lease_ms": "60000"}, id="text"),
    pytest.param({"lease_ms": 60000.0}, id="float"),
])
def test_og_13_renew_with_a_bad_lease_ms_still_renews(rig, extra):
    painted(rig, 1, 5)
    rig.advance(50 * SECOND_MS)

    rig.send(renew_msg(1, 5, **extra))

    rig.advance(40 * SECOND_MS)  # 90 s after the paint
    assert rig.to_logic() == []
    assert len(rig.seam.live_windows()) == 2
    rig.advance(50 * SECOND_MS)  # 90 s after the renew
    assert rig.to_logic() == [ack("expired", 1, 5)]


@pytest.mark.parametrize("lease_ms", [2**31, 10**12], ids=["int32-plus-1", "huge"])
def test_og_14_renew_above_the_timer_limit_still_renews(rig, lease_ms):
    painted(rig, 1, 5)
    rig.advance(50 * SECOND_MS)

    rig.send(renew_msg(1, 5, lease_ms=lease_ms))

    rig.advance(DAY_MS)
    assert rig.to_logic() == []
    assert len(rig.seam.live_windows()) == 2


@pytest.mark.parametrize("before", ["no-paint", "after-clear"])
def test_og_15_renew_with_no_lease_does_nothing(rig, before):
    if before == "after-clear":
        painted(rig, 1, 5)
        rig.send(clear_msg(1, 5))
        assert rig.to_logic() == [ack("cleared", 1, 5)]
    mark = len(rig.seam.destroy_calls)

    rig.send(renew_msg(1, 5, lease_ms=60 * SECOND_MS))

    rig.advance(DAY_MS)
    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark) == []


# --- OG-16, OG-17: lease expiry ----------------------------------------------


def test_og_16_expiry_with_a_surviving_window_reports_later_once(rig):
    handles = painted(rig, 1, 1)
    survivor = handles[0]
    rig.seam.destroy_fails = {survivor}

    rig.advance(FIRST_LEASE_MS)
    assert rig.to_logic() == []
    assert live(rig) == {survivor}

    mark = len(rig.seam.destroy_calls)
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark).count(survivor) >= 2

    rig.seam.destroy_fails = set()
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("expired", 1, 1)]
    assert rig.seam.live_windows() == []
    rig.advance(DAY_MS)
    assert rig.to_logic() == []


def test_og_17_expiry_below_an_accepted_clear_reports_nothing(rig):
    painted(rig, 1, 1)
    rig.seam.destroy_fails_all = True
    rig.send(clear_msg(1, 2))  # accepted, but every window survives
    assert rig.to_logic() == []

    rig.advance(FIRST_LEASE_MS)
    assert rig.to_logic() == []

    rig.seam.destroy_fails_all = False
    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("cleared", 1, 2)]
    assert rig.seam.live_windows() == []
    rig.advance(DAY_MS)
    assert rig.to_logic() == []


# --- OG-18, OG-19: reset_overlay ---------------------------------------------


def test_og_18_reset_removes_badges_ends_the_lease_and_reports_nothing(rig):
    painted(rig, 5, 5)

    rig.send(RESET)

    assert rig.to_logic() == []
    assert rig.seam.live_windows() == []
    rig.advance(DAY_MS)
    assert rig.to_logic() == []
    # A restarted Logic numbers its pairs from zero; its paint shows.
    painted(rig, 0, 1)


def test_og_19_reset_with_a_surviving_window_retries_and_reports_nothing(rig):
    handles = painted(rig, 1, 1)
    survivor = handles[0]
    rig.seam.destroy_fails = {survivor}

    rig.send(RESET)
    assert rig.to_logic() == []
    assert live(rig) == {survivor}

    mark = len(rig.seam.destroy_calls)
    rig.advance(RETRY_SPAN_MS)
    assert rig.seam.destroys_since(mark).count(survivor) >= 2

    rig.seam.destroy_fails = set()
    rig.advance(RETRY_SPAN_MS)
    assert rig.seam.live_windows() == []
    mark = len(rig.seam.destroy_calls)
    rig.advance(DAY_MS)
    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark) == []


# --- OG-20: the retry while an older report is parked -----------------------


def test_og_20_clean_clear_after_an_incomplete_one_reports_both(rig):
    handles = painted(rig, 1, 1)
    rig.seam.destroy_fails = {handles[0]}
    rig.send(clear_msg(1, 1))
    assert rig.to_logic() == []

    rig.seam.destroy_fails = set()
    rig.send(clear_msg(1, 2))
    assert rig.to_logic() == [ack("cleared", 1, 2)]
    assert rig.seam.live_windows() == []

    rig.advance(RETRY_SPAN_MS)
    assert rig.to_logic() == [ack("cleared", 1, 1)]
    rig.advance(DAY_MS)
    assert rig.to_logic() == []


def test_og_20b_retry_after_a_clean_reset_leaves_a_later_paint(rig):
    handles = painted(rig, 1, 1)
    rig.seam.destroy_fails = {handles[0]}
    rig.send(clear_msg(1, 1))
    rig.seam.destroy_fails = set()
    rig.send(RESET)
    assert rig.seam.live_windows() == []
    second = painted(rig, 0, 1)
    mark = len(rig.seam.destroy_calls)

    rig.advance(RETRY_SPAN_MS)

    assert rig.to_logic() == []
    assert rig.seam.destroys_since(mark) == []
    assert live(rig) == set(second)


# --- OG-21, OG-22: the mouse grid --------------------------------------------


def test_og_21_grid_messages_reach_the_grid_and_report_nothing(rig):
    rig.send(grid_msg())
    assert rig.to_logic() == []
    (window,) = rig.seam.live_windows()
    assert window.geometry == (PRIMARY.left, PRIMARY.top, PRIMARY.width,
                               PRIMARY.height)

    mark = len(rig.seam.composites)
    rig.send(pin_msg(300, 300))
    assert rig.to_logic() == []
    assert len(rig.seam.composites_since(mark)) == 1

    rig.send(CLEAR_GRID)
    assert rig.to_logic() == []
    assert rig.seam.live_windows() == []


def test_og_21b_pin_alone_shows_a_window(rig):
    rig.send(pin_msg(300, 300))

    assert rig.to_logic() == []
    (window,) = rig.seam.live_windows()
    assert window.left <= 300 < window.left + window.width
    assert window.top <= 300 < window.top + window.height


def test_og_21c_clear_grid_with_extra_keys_still_clears(rig):
    rig.send(grid_msg())

    rig.send(dict(CLEAR_GRID, reason="a later field"))

    assert rig.seam.live_windows() == []
    assert rig.to_logic() == []


@pytest.mark.parametrize("bad", [
    pytest.param({"action": "paint_grid", "monitor_left": 0}, id="grid-missing-keys"),
    pytest.param({"action": "paint_grid_pin", "x": "300", "y": 300}, id="pin-text-x"),
])
def test_og_21d_malformed_grid_message_changes_nothing(rig, bad):
    # A grid on the second monitor and a pin on the first: two windows.
    rig.send(grid_msg(SECOND), pin_msg(300, 300))
    grid_windows = live(rig)
    assert len(grid_windows) == 2
    mark = len(rig.seam.windows)
    destroy_mark = len(rig.seam.destroy_calls)

    rig.send(bad, paint_msg(1, 1))

    # The grid and the pin stay; the only new windows are the badges of the
    # next message in the same pump run.
    assert grid_windows <= live(rig)
    assert rig.seam.destroys_since(destroy_mark) == []
    assert len(rig.seam.windows_since(mark)) == 2
    reports = rig.to_logic()
    assert len(reports) == 1
    assert_paint_report(reports[0], "painted", 1, 1)


@pytest.mark.parametrize("teardown", ["clear", "expiry", "reset"])
def test_og_22_badge_teardown_leaves_the_grid(rig, teardown):
    rig.send(grid_msg(SECOND))
    (grid_window,) = rig.seam.live_windows()
    painted(rig, 1, 1)

    if teardown == "clear":
        rig.send(clear_msg(1, 1))
    elif teardown == "expiry":
        rig.advance(FIRST_LEASE_MS)
    else:
        rig.send(RESET)

    assert live(rig) == {grid_window.hwnd}


def test_og_22b_clear_grid_leaves_the_badges(rig):
    handles = painted(rig, 1, 1)
    rig.send(grid_msg(SECOND))

    rig.send(CLEAR_GRID)

    assert live(rig) == set(handles)
    assert rig.to_logic() == []


# --- OG-23: a full queue to Logic ---------------------------------------------


def test_og_23_full_queue_drops_the_report_and_the_pump_goes_on(qapp):
    commands = Queue(maxsize=1)
    commands.put("FILLER")
    with gui_rig(FakeMonitors(monitors=[PRIMARY, SECOND]),
                 commands_queue=commands) as rig:
        rig.send(paint_msg(1, 1), grid_msg())

        assert rig.to_logic() == ["FILLER"]
        assert len(rig.seam.live_windows()) == 3  # two badges, one grid
        rig.advance(FIRST_LEASE_MS)
        assert rig.to_logic() == [ack("expired", 1, 1)]
