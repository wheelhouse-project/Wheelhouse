"""Boundary tests for area P4 of OverlayPaintWindowManager: clear, lease
expiry and teardown (wh-full-refactor.7.1, stage A of place 4).

Each test names its row OT-n of the P4 notes. A test enters only through the
public methods that GuiManager calls (clear, expire_lease, reset,
retry_teardown, teardown_pending, has_deferred_teardown_ack, clear_all) and
reads only their return values and the calls on the fake operating system
seam of tests/overlay_paint_seam_harness.py. ``paint`` is used only to put
windows on the screen before a teardown; no test here is about paint.
"""

from __future__ import annotations

import pytest

from tests.overlay_paint_seam_harness import (
    PRIMARY,
    SECOND,
    FakeMonitors,
    badge_summary,
    paint_seam,
)

# One badge box on each fake monitor, as (x, y, width, height) in physical
# pixels of the virtual desktop.
BOX_PRIMARY = (200, 200, 120, 40)
BOX_SECOND = (2300, 300, 120, 40)


def ack(state: str, session: int, generation: int) -> dict:
    """The overlay_state_changed dict a teardown returns to GuiManager."""
    return {
        "action": "overlay_state_changed",
        "state": state,
        "overlay_session_id": session,
        "paint_generation": generation,
        "monitor_ids": [],
        "snapshot_id": None,
    }


@pytest.fixture
def seam(qapp):
    with paint_seam(FakeMonitors(monitors=[PRIMARY, SECOND])) as fake:
        yield fake


def painted(seam, manager, pair=(1, 1), boxes=(BOX_PRIMARY, BOX_SECOND)):
    """Paint badges at ``pair`` (set-up only); return the new window handles."""
    mark = len(seam.windows)
    result = manager.paint(badge_summary(boxes), *pair)
    assert result is not None and result["state"] == "painted"
    handles = [w.hwnd for w in seam.windows_since(mark)]
    assert len(handles) == len(boxes)
    return handles


def teardown(manager, kind: str, pair):
    if kind == "clear":
        return manager.clear(*pair)
    return manager.expire_lease(*pair)


STATE = {"clear": "cleared", "expire": "expired"}


# --- OT-1 / OT-2: an accepted teardown -------------------------------------


@pytest.mark.parametrize("kind", ["clear", "expire"])
@pytest.mark.parametrize(
    "pair", [(1, 1), (1, 2), (2, 0)], ids=["same-pair", "newer-gen", "newer-session"],
)
def test_ot_1_accepted_teardown_destroys_every_window_and_acks(seam, kind, pair):
    manager = seam.manager()
    handles = painted(seam, manager, (1, 1))
    mark = len(seam.destroy_calls)

    result = teardown(manager, kind, pair)

    assert result == ack(STATE[kind], *pair)
    assert sorted(seam.destroys_since(mark)) == sorted(handles)
    assert seam.live_windows() == []
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False


@pytest.mark.parametrize("kind", ["clear", "expire"])
def test_ot_2_teardown_with_no_window_still_acks(seam, kind):
    manager = seam.manager()

    result = teardown(manager, kind, (3, 4))

    assert result == ack(STATE[kind], 3, 4)
    assert seam.destroy_calls == []
    assert manager.teardown_pending is False


# --- OT-3: a stale teardown --------------------------------------------------


@pytest.mark.parametrize("kind", ["clear", "expire"])
@pytest.mark.parametrize(
    "older", [(2, 4), (1, 9)], ids=["older-gen", "older-session"],
)
def test_ot_3_stale_teardown_returns_none_and_destroys_nothing(seam, kind, older):
    manager = seam.manager()
    handles = painted(seam, manager, (2, 5))
    mark = len(seam.destroy_calls)

    result = teardown(manager, kind, older)

    assert result is None
    assert seam.destroys_since(mark) == []
    assert sorted(w.hwnd for w in seam.live_windows()) == sorted(handles)


@pytest.mark.parametrize("kind", ["clear", "expire"])
def test_ot_3b_teardown_older_than_an_accepted_clear_is_stale(seam, kind):
    manager = seam.manager()
    assert manager.clear(1, 3) == ack("cleared", 1, 3)

    assert teardown(manager, kind, (1, 2)) is None


# --- OT-4: an accepted teardown blocks a late paint at its pair ----------------


@pytest.mark.parametrize("kind", ["clear", "expire"])
@pytest.mark.parametrize("late", [(1, 2), (1, 1)], ids=["same-pair", "older-pair"])
def test_ot_4_late_paint_at_or_below_the_teardown_pair_shows_nothing(seam, kind, late):
    manager = seam.manager()
    painted(seam, manager, (1, 1))
    assert teardown(manager, kind, (1, 2)) == ack(STATE[kind], 1, 2)
    mark = len(seam.windows)

    assert manager.paint(badge_summary([BOX_PRIMARY]), *late) is None
    assert seam.windows_since(mark) == []
    assert seam.live_windows() == []


@pytest.mark.parametrize("kind", ["clear", "expire"])
def test_ot_4b_paint_newer_than_the_teardown_pair_shows(seam, kind):
    manager = seam.manager()
    assert teardown(manager, kind, (1, 2)) == ack(STATE[kind], 1, 2)

    handles = painted(seam, manager, (1, 3), boxes=(BOX_PRIMARY,))

    assert [w.hwnd for w in seam.live_windows()] == handles


# --- OT-5 to OT-8: a teardown that leaves a window on the screen --------------


@pytest.mark.parametrize("kind", ["clear", "expire"])
@pytest.mark.parametrize("failure", ["returns-zero", "raises"])
def test_ot_5_teardown_with_a_surviving_window_withholds_the_ack(seam, kind, failure):
    manager = seam.manager()
    painted(seam, manager, (1, 1))
    if failure == "raises":
        seam.destroy_raises = True
    else:
        seam.destroy_fails_all = True

    result = teardown(manager, kind, (1, 2))

    assert result is None
    assert manager.teardown_pending is True
    assert manager.has_deferred_teardown_ack is True
    assert len(seam.live_windows()) == 2


@pytest.mark.parametrize("kind", ["clear", "expire"])
def test_ot_6_retry_while_destroy_still_fails_retries_only_the_survivor(seam, kind):
    manager = seam.manager()
    first, second = painted(seam, manager, (1, 1))
    seam.destroy_fails = {second}
    assert teardown(manager, kind, (1, 2)) is None
    mark = len(seam.destroy_calls)

    assert manager.retry_teardown() is None

    assert seam.destroys_since(mark) == [second]
    assert [w.hwnd for w in seam.live_windows()] == [second]
    assert manager.teardown_pending is True
    assert manager.has_deferred_teardown_ack is True


@pytest.mark.parametrize("kind", ["clear", "expire"])
def test_ot_7_retry_that_destroys_the_survivor_releases_the_ack_once(seam, kind):
    manager = seam.manager()
    _first, second = painted(seam, manager, (1, 1))
    seam.destroy_fails = {second}
    assert teardown(manager, kind, (1, 2)) is None
    seam.destroy_fails = set()

    assert manager.retry_teardown() == ack(STATE[kind], 1, 2)

    assert seam.live_windows() == []
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False
    mark = len(seam.destroy_calls)
    assert manager.retry_teardown() is None
    assert seam.destroys_since(mark) == []


def test_ot_8_newest_incomplete_teardown_owns_the_parked_ack(seam):
    manager = seam.manager()
    handles = painted(seam, manager, (1, 1))
    seam.destroy_fails_all = True
    assert manager.clear(1, 2) is None
    mark = len(seam.destroy_calls)

    assert manager.expire_lease(1, 3) is None
    # The second teardown retried the survivors of the first.
    assert sorted(seam.destroys_since(mark)) == sorted(handles)
    seam.destroy_fails_all = False

    assert manager.retry_teardown() == ack("expired", 1, 3)
    assert manager.retry_teardown() is None
    assert seam.live_windows() == []


# --- OT-9 / OT-10: a retry never touches the windows of a later paint ---------


def test_ot_9_retry_never_destroys_the_window_of_a_later_paint(seam):
    manager = seam.manager()
    (old,) = painted(seam, manager, (1, 1), boxes=(BOX_PRIMARY,))
    seam.destroy_fails = {old}
    assert manager.clear(1, 2) is None
    (new,) = painted(seam, manager, (1, 3), boxes=(BOX_PRIMARY,))
    mark = len(seam.destroy_calls)

    assert manager.retry_teardown() is None

    assert seam.destroys_since(mark) == [old]
    assert sorted(w.hwnd for w in seam.live_windows()) == sorted([old, new])
    assert manager.teardown_pending is True


def test_ot_9b_paint_destroys_the_survivor_and_the_retry_releases_the_ack(seam):
    manager = seam.manager()
    (old,) = painted(seam, manager, (1, 1), boxes=(BOX_PRIMARY,))
    seam.destroy_fails = {old}
    assert manager.clear(1, 2) is None
    seam.destroy_fails = set()
    mark = len(seam.destroy_calls)
    (new,) = painted(seam, manager, (1, 3), boxes=(BOX_PRIMARY,))
    assert seam.destroys_since(mark) == [old]
    # A paint does not pay the teardown debt; the next retry does.
    assert manager.teardown_pending is True
    mark = len(seam.destroy_calls)

    assert manager.retry_teardown() == ack("cleared", 1, 2)

    assert seam.destroys_since(mark) == []
    assert [w.hwnd for w in seam.live_windows()] == [new]
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False


def test_ot_10_retry_without_debt_or_parked_ack_does_nothing(seam):
    manager = seam.manager()
    handles = painted(seam, manager, (1, 1))
    mark = len(seam.destroy_calls)

    assert manager.retry_teardown() is None

    assert seam.destroys_since(mark) == []
    assert sorted(w.hwnd for w in seam.live_windows()) == sorted(handles)


# --- OT-11: a later clean teardown pays the debt -------------------------------


def test_ot_11_later_clean_clear_pays_the_debt_and_the_old_ack_stays_parked(seam):
    manager = seam.manager()
    handles = painted(seam, manager, (1, 1))
    seam.destroy_fails_all = True
    assert manager.clear(1, 2) is None
    seam.destroy_fails_all = False

    assert manager.clear(1, 3) == ack("cleared", 1, 3)

    assert seam.live_windows() == []
    assert set(handles) <= set(seam.destroy_calls)
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is True
    mark = len(seam.destroy_calls)
    assert manager.retry_teardown() == ack("cleared", 1, 2)
    assert seam.destroys_since(mark) == []
    assert manager.has_deferred_teardown_ack is False


# --- OT-12 to OT-14: reset -------------------------------------------------------


def test_ot_12_reset_destroys_every_window_and_returns_nothing(seam):
    manager = seam.manager()
    handles = painted(seam, manager, (4, 7))
    mark = len(seam.destroy_calls)

    assert manager.reset() is None

    assert sorted(seam.destroys_since(mark)) == sorted(handles)
    assert seam.live_windows() == []
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False


def test_ot_13_reset_starts_a_fresh_generation_gate(seam):
    manager = seam.manager()
    painted(seam, manager, (4, 7))
    assert manager.clear(0, 0) is None  # stale before the reset

    manager.reset()

    assert manager.clear(0, 0) == ack("cleared", 0, 0)


def test_ot_14_reset_drops_the_parked_ack_but_keeps_the_debt(seam):
    manager = seam.manager()
    _first, second = painted(seam, manager, (1, 1))
    seam.destroy_fails = {second}
    assert manager.clear(1, 2) is None

    manager.reset()

    assert manager.teardown_pending is True
    assert manager.has_deferred_teardown_ack is False
    seam.destroy_fails = set()
    mark = len(seam.destroy_calls)
    assert manager.retry_teardown() is None
    assert seam.destroys_since(mark) == [second]
    assert seam.live_windows() == []
    assert manager.teardown_pending is False


def test_ot_14c_reset_with_a_new_survivor_records_the_debt(seam):
    manager = seam.manager()
    _first, second = painted(seam, manager, (1, 1))
    assert manager.teardown_pending is False
    seam.destroy_fails = {second}

    assert manager.reset() is None

    assert [w.hwnd for w in seam.live_windows()] == [second]
    assert manager.teardown_pending is True
    assert manager.has_deferred_teardown_ack is False
    seam.destroy_fails = set()
    assert manager.retry_teardown() is None
    assert seam.live_windows() == []
    assert manager.teardown_pending is False


def test_ot_14b_clean_reset_pays_the_debt(seam):
    manager = seam.manager()
    _first, second = painted(seam, manager, (1, 1))
    seam.destroy_fails = {second}
    assert manager.clear(1, 2) is None
    seam.destroy_fails = set()

    manager.reset()

    assert seam.live_windows() == []
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False


# --- OT-15 / OT-16: clear_all ----------------------------------------------------


def test_ot_15_clear_all_destroys_every_window_and_keeps_the_gate(seam):
    manager = seam.manager()
    handles = painted(seam, manager, (2, 5))
    mark = len(seam.destroy_calls)

    assert manager.clear_all() is None

    assert sorted(seam.destroys_since(mark)) == sorted(handles)
    assert seam.live_windows() == []
    assert manager.clear(2, 4) is None  # the mark of (2, 5) still holds


def test_ot_16_clear_all_records_no_debt_for_a_survivor(seam):
    manager = seam.manager()
    _first, second = painted(seam, manager, (1, 1))
    seam.destroy_fails = {second}

    manager.clear_all()

    assert [w.hwnd for w in seam.live_windows()] == [second]
    assert manager.teardown_pending is False
    assert manager.has_deferred_teardown_ack is False
    mark = len(seam.destroy_calls)
    assert manager.retry_teardown() is None
    assert seam.destroys_since(mark) == []
    # The next teardown retries the survivor.
    seam.destroy_fails = set()
    assert manager.clear(1, 2) == ack("cleared", 1, 2)
    assert seam.destroys_since(mark) == [second]
    assert seam.live_windows() == []


# --- OT-17: one manager's teardown leaves another manager's windows -----------


@pytest.mark.parametrize("kind", ["clear", "expire", "reset", "clear_all"])
def test_ot_17_teardown_destroys_only_the_windows_of_its_own_manager(seam, kind):
    numbered = seam.manager()
    other = seam.manager()
    mine = painted(seam, numbered, (1, 1))
    (theirs,) = painted(seam, other, (1, 1), boxes=(BOX_PRIMARY,))
    mark = len(seam.destroy_calls)

    if kind == "reset":
        numbered.reset()
    elif kind == "clear_all":
        numbered.clear_all()
    else:
        teardown(numbered, kind, (1, 2))

    assert sorted(seam.destroys_since(mark)) == sorted(mine)
    assert [w.hwnd for w in seam.live_windows()] == [theirs]
