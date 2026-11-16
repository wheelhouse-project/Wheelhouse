"""Boundary tests for area U4 of the overlay stack (place 2, stage A).

Area U4 is the Input-process half of the numbered overlay: the eight methods of
``UIActionHandler`` for the overlay walk, the snapshots and "click N". Each test
sends one of the six messages from Logic (start_overlay_walk,
show_numbered_overlay, pin_snapshot, unpin_snapshot, refresh_overlay_snapshot,
click_snapshot_item) the way ``input_proc`` does, and reads only what leaves the
handler: the reply on the response queue, and the presses and mouse clicks on
the fake UI Automation tree and the fake mouse. The rows are IW-1 and up in
docs/design/overlay-stack-requirements.md. Each test name carries its row.

The harness is tests/overlay_stack_input_harness.py. One test waits in real
time: test_iw_19 sleeps 0.6 s to move the clock past a walk deadline.
"""

from __future__ import annotations

import time

import pytest
from comtypes import COMError

from tests.overlay_stack_input_harness import (
    NOTEPAD,
    OTHER,
    InputSide,
    Window,
    button_row,
    item_id_for,
    FakeUiaElement,
)
from tests.test_uia_walker import FakeRect

OFF = {"overlay_enabled": False}


def _names(reply: dict) -> list[str]:
    return [i["name"] for i in reply["snapshot_summary"]["items"]]


def _numbers(reply: dict) -> list[int]:
    return [i["display_number"] for i in reply["snapshot_summary"]["items"]]


def _side(*names: str, **kwargs) -> InputSide:
    return InputSide(trees={NOTEPAD.hwnd: button_row(*names)}, **kwargs)


# ---------------------------------------------------------------------------
# start_overlay_walk
# ---------------------------------------------------------------------------


def test_iw_1_walk_numbers_every_control_and_echoes_the_request():
    with _side("Save", "Open", "Cancel") as side:
        reply = side.walk(overlay_session_id=5, paint_generation=2,
                          trace_id="trace-1")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "ok", None)
    assert _names(reply) == ["Save", "Open", "Cancel"]
    assert _numbers(reply) == [1, 2, 3]
    assert reply["snapshot_summary"]["snapshot_id"] == reply["snapshot_id"]
    assert (reply["overlay_session_id"], reply["paint_generation"],
            reply["trace_id"]) == (5, 2, "trace-1")


def test_iw_2_walk_names_the_window_it_read():
    with _side("Save") as side:
        reply = side.walk()
    assert reply["foreground_window"] == NOTEPAD.hwnd
    assert reply["foreground_pid"] == NOTEPAD.pid
    assert reply["foreground_process_name"] == NOTEPAD.process_name
    assert reply["foreground_window_creation_time"] == NOTEPAD.creation_time_ms


def test_iw_3_walk_of_a_window_with_no_controls_is_no_targets():
    with InputSide(trees={NOTEPAD.hwnd: []}) as side:
        reply = side.walk(overlay_session_id=3, paint_generation=4)
    assert (reply["status"], reply["outcome"]) == ("ok", "no_targets")
    assert reply["snapshot_summary"]["items"] == []
    assert (reply["overlay_session_id"], reply["paint_generation"]) == (3, 4)


def test_iw_4_walk_skips_a_control_that_is_not_interactive():
    # Static text with no Invoke pattern: neither its type nor a press path
    # keeps it in the list.
    text = FakeUiaElement("just text", control_type=50020, role="text",
                          rect=FakeRect(300, 20, 400, 70), has_invoke=False)
    with InputSide(trees={NOTEPAD.hwnd: [*button_row("Save"), text]}) as side:
        reply = side.walk()
    assert _names(reply) == ["Save"]


def test_iw_5_walk_with_the_overlay_off_is_refused_and_reads_nothing():
    with _side("Save", click_config=OFF) as side:
        reply = side.walk(overlay_session_id=7, paint_generation=8,
                          trace_id="t")
        assert side.root.walks_started == 0
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "disabled_by_config")
    assert reply["snapshot_id"] is None
    assert (reply["overlay_session_id"], reply["paint_generation"],
            reply["trace_id"]) == (7, 8, "t")
    # No window was read, so the reply names no window.
    assert reply["foreground_window"] == 0
    assert reply["foreground_process_name"] == ""


def test_iw_6_walk_with_automation_unavailable_says_so_and_does_not_retry():
    with _side("Save", automation_fails=True) as side:
        first = side.walk()
        second = side.walk()
        builds = side.automation_builds
    for reply in (first, second):
        assert (reply["status"], reply["outcome"], reply["reason"]) == (
            "ok", "execution_failed", "automation_unavailable")
        assert reply["foreground_window"] == 0
    assert builds == 1


def test_iw_7_walk_past_its_deadline_is_execution_failed():
    with _side("Save", "Open") as side:
        reply = side.walk(command_dequeue_monotonic=time.monotonic() - 100.0)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "walk_deadline_exceeded")
    assert reply["snapshot_id"] is None
    # The failed walk still names the window it tried to read.
    assert reply["foreground_window"] == NOTEPAD.hwnd


def test_iw_8_walk_deadline_follows_the_config_value():
    # The deadline is the configured screen read limit (screen_read_timeout_ms)
    # minus the 250 ms pre-walk margin, counted from the dequeue instant, so
    # 19.75 s here. A walk 19.6 s after dequeue still succeeds; one 19.9 s
    # after dequeue does not. Without the margin, both walks would succeed.
    with _side("Save", click_config={"screen_read_timeout_ms": 20000}) as side:
        late = side.walk(command_dequeue_monotonic=time.monotonic() - 19.6)
        gone = side.walk(command_dequeue_monotonic=time.monotonic() - 19.9)
    assert late["outcome"] == "ok"
    assert gone["reason"] == "walk_deadline_exceeded"


def test_iw_9_walk_hit_by_a_transient_window_error_is_execution_failed():
    with _side("Save") as side:
        side.root.read_error = OSError("window is rebuilding")
        reply = side.walk(overlay_session_id=2, paint_generation=3)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "transient_com_error")
    assert (reply["overlay_session_id"], reply["paint_generation"]) == (2, 3)
    assert reply["foreground_window"] == NOTEPAD.hwnd


def test_iw_10_walk_hit_by_an_unexpected_error_is_a_status_error():
    with _side("Save") as side:
        side.root.read_error = RuntimeError("bug")
        reply = side.walk(trace_id="t-boom")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "error", "unexpected_error")
    assert reply["trace_id"] == "t-boom"


def test_iw_11_walk_of_a_window_the_os_cannot_resolve_is_execution_failed():
    # The foreground window is gone: UI Automation answers with a COM error, as
    # the OS does for a window that no longer exists. COMError is not an OSError,
    # so only its own entry in the transient error list maps it.
    with _side("Save") as side:
        side.foreground = OTHER.hwnd
        reply = side.walk()
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "transient_com_error")


def test_iw_12_every_walk_gets_a_new_snapshot_id():
    with _side("Save") as side:
        first = side.walk()
        second = side.walk()
    assert first["snapshot_id"] != second["snapshot_id"]


def test_iw_13_settle_walk_reads_until_two_reads_agree():
    with _side("Save", "Open") as side:
        reply = side.walk(settle=True)
        reads = side.root.walks_started
    assert reply["outcome"] == "ok"
    assert _names(reply) == ["Save", "Open"]
    # Measured on the fake root: the event listener resolves the window once,
    # and each read of the list resolves it once. One read of the list gives 2
    # resolutions, the two agreeing reads of today's loop give 3.
    assert reads >= 3


def test_iw_14_settle_walk_that_matches_the_held_list_answers_with_the_held_id():
    with _side("Save", "Open") as side:
        held = side.walk()
        reply = side.walk(settle=True, compare_snapshot_id=held["snapshot_id"])
    assert reply["snapshot_id"] == held["snapshot_id"]
    assert reply["snapshot_summary"]["snapshot_id"] == held["snapshot_id"]
    assert reply["snapshot_summary"] == held["snapshot_summary"]


def test_iw_15_settle_walk_that_differs_from_the_held_list_answers_fresh():
    with _side("Save", "Open") as side:
        held = side.walk()
        side.trees[NOTEPAD.hwnd][:] = button_row("Save", "Close")
        reply = side.walk(settle=True, compare_snapshot_id=held["snapshot_id"])
    assert reply["snapshot_id"] != held["snapshot_id"]
    assert _names(reply) == ["Save", "Close"]
    assert reply["snapshot_summary"]["snapshot_id"] == reply["snapshot_id"]


def test_iw_16_settle_walk_with_an_unknown_held_id_answers_fresh():
    with _side("Save") as side:
        reply = side.walk(settle=True, compare_snapshot_id="walk-gone")
    assert reply["outcome"] == "ok"
    assert reply["snapshot_id"] != "walk-gone"


def test_iw_17_settle_walk_does_not_reuse_a_held_list_from_another_window():
    # Same layout, but the window is a new one at the same handle.
    with _side("Save", "Open") as side:
        held = side.walk()
        side.windows[NOTEPAD.hwnd] = Window(
            NOTEPAD.hwnd, NOTEPAD.pid, NOTEPAD.process_name,
            NOTEPAD.creation_time_ms + 1)
        reply = side.walk(settle=True, compare_snapshot_id=held["snapshot_id"])
    assert reply["snapshot_id"] != held["snapshot_id"]
    assert reply["foreground_window_creation_time"] == (
        NOTEPAD.creation_time_ms + 1)


def test_iw_18_settle_walk_with_no_list_to_read_is_execution_failed():
    with _side("Save") as side:
        side.root.read_error = OSError("gone")
        reply = side.walk(settle=True)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "transient_com_error")


def test_iw_19_settle_walk_answers_with_the_last_read_when_a_later_read_fails():
    # A read that cannot produce a list (here: the walk deadline passes during
    # it) ends the settle, and the answer is the last read that completed. The
    # one real wait below (0.6 s) is what moves the clock past the deadline.
    with _side("Save", "Open",
               click_config={"screen_read_timeout_ms": 1500}) as side:
        # Resolution 1 is the event listener. Read 1 sees Save and Open. Read 2
        # sees Save and Close, so the two reads differ and the settle loop
        # reads a third time. That read would see Save and Exit, but it starts,
        # then the clock passes the deadline. So an answer of Save and Close
        # proves that read 3 did not complete. The deadline is the dequeue time
        # plus the 1500 ms limit minus the 250 ms margin. The dequeue time
        # below leaves 0.5 s of it. (The fake copies the tree when a read
        # starts, so a change at count N shows in read N.)
        def after_walk(count: int) -> None:
            if count == 2:
                side.trees[NOTEPAD.hwnd][:] = button_row("Save", "Close")
            if count == 3:
                side.trees[NOTEPAD.hwnd][:] = button_row("Save", "Exit")
            if count == 4:
                time.sleep(0.6)

        side.root.after_walk = after_walk
        reply = side.walk(settle=True,
                          command_dequeue_monotonic=time.monotonic() - 0.75)
    assert (reply["status"], reply["outcome"]) == ("ok", "ok")
    assert _names(reply) == ["Save", "Close"]


# ---------------------------------------------------------------------------
# pin_snapshot
# ---------------------------------------------------------------------------


def _pin(side, session, gen, snapshot="walk-x", **extra):
    return side.send_one(
        "pin_snapshot", overlay_session_id=session, snapshot_id=snapshot,
        paint_generation=gen, **extra)


def test_iw_20_pin_of_a_stored_snapshot_is_pinned():
    with _side("Save") as side:
        walk = side.walk()
        reply = _pin(side, 4, 1, walk["snapshot_id"])
    assert reply == {
        "status": "ok", "reason": None, "overlay_session_id": 4,
        "snapshot_id": walk["snapshot_id"], "pinned": True,
        "request_id": reply["request_id"], "action": "pin_snapshot",
    }


def test_iw_21_pin_of_an_unknown_snapshot_is_unknown_snapshot():
    with _side("Save") as side:
        reply = _pin(side, 4, 1, "walk-nothing")
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "unknown_snapshot", False)
    assert reply["snapshot_id"] == "walk-nothing"


@pytest.mark.parametrize("params,echo_session,echo_snapshot", [
    ({"overlay_session_id": "4", "snapshot_id": "s", "paint_generation": 1},
     0, "s"),
    ({"overlay_session_id": True, "snapshot_id": "s", "paint_generation": 1},
     0, "s"),
    ({"overlay_session_id": 4, "snapshot_id": "s", "paint_generation": "1"},
     4, "s"),
    ({"overlay_session_id": 4, "snapshot_id": "s", "paint_generation": True},
     4, "s"),
    ({"overlay_session_id": 4, "snapshot_id": 12, "paint_generation": 1},
     4, ""),
])
def test_iw_22_pin_with_malformed_fields_is_invalid_request(
        params, echo_session, echo_snapshot):
    with _side("Save") as side:
        reply = side.send_one("pin_snapshot", **params)
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "error", "invalid_request", False)
    assert reply["overlay_session_id"] == echo_session
    assert reply["snapshot_id"] == echo_snapshot


def test_iw_23_pin_with_the_overlay_off_is_refused():
    with _side("Save", click_config=OFF) as side:
        reply = _pin(side, 4, 1)
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "disabled_by_config", False)


def test_iw_24_pin_from_an_older_session_is_stale_session():
    with _side("Save") as side:
        walk = side.walk()
        assert _pin(side, 5, 1, walk["snapshot_id"])["pinned"] is True
        reply = _pin(side, 4, 9, walk["snapshot_id"])
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "stale_session", False)


def test_iw_25_pin_with_an_older_generation_is_stale_generation():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 5, 3, walk["snapshot_id"])
        reply = _pin(side, 5, 2, walk["snapshot_id"])
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "stale_generation", False)


def test_iw_26_pin_with_the_same_generation_is_accepted_again():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 5, 3, walk["snapshot_id"])
        reply = _pin(side, 5, 3, walk["snapshot_id"])
    assert reply["pinned"] is True


def test_iw_27_pin_from_a_newer_session_resets_the_generation_bar():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 5, 9, walk["snapshot_id"])
        newer = _pin(side, 6, 1, walk["snapshot_id"])
        same_again = _pin(side, 6, 1, walk["snapshot_id"])
        older_gen = _pin(side, 6, 0, walk["snapshot_id"])
    assert newer["pinned"] is True
    assert same_again["pinned"] is True
    assert older_gen["reason"] == "stale_generation"


def test_iw_28_pin_of_a_missing_snapshot_still_raises_the_generation_bar():
    with _side("Save") as side:
        walk = side.walk()
        assert _pin(side, 5, 7, "walk-gone")["reason"] == "unknown_snapshot"
        reply = _pin(side, 5, 6, walk["snapshot_id"])
    assert reply["reason"] == "stale_generation"


def test_iw_29_pin_with_malformed_fields_does_not_move_the_bar():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 5, 3, walk["snapshot_id"])
        side.send_one("pin_snapshot", overlay_session_id="99", snapshot_id="s",
                      paint_generation=99)
        reply = _pin(side, 5, 3, walk["snapshot_id"])
    assert reply["pinned"] is True


def test_iw_30_stale_pin_leaves_the_snapshot_unpinned():
    # The rejected pin does not pin: with the store at capacity 2, the snapshot
    # that was not pinned is the one that is evicted.
    with _side("Save", click_config={"snapshot_store_capacity": 2}) as side:
        held = side.walk()
        _pin(side, 5, 3, "walk-other")
        assert _pin(side, 5, 1, held["snapshot_id"])["reason"] == (
            "stale_generation")
        side.walk()
        side.walk()
        gone = side.send_one("show_numbered_overlay",
                             snapshot_id=held["snapshot_id"])
    assert gone["outcome"] == "snapshot_expired"


# ---------------------------------------------------------------------------
# unpin_snapshot and the pin's effect on the store
# ---------------------------------------------------------------------------


def test_iw_31_unpin_of_a_pinned_snapshot_is_ok_and_not_pinned():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 4, 1, walk["snapshot_id"])
        reply = side.send_one("unpin_snapshot", overlay_session_id=4,
                              snapshot_id=walk["snapshot_id"])
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", None, False)
    assert (reply["overlay_session_id"], reply["snapshot_id"]) == (
        4, walk["snapshot_id"])


def test_iw_32_unpin_of_an_unknown_snapshot_is_unknown_snapshot():
    with _side("Save") as side:
        reply = side.send_one("unpin_snapshot", overlay_session_id=4,
                              snapshot_id="walk-none")
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "unknown_snapshot", False)


@pytest.mark.parametrize("params,echo_session,echo_snapshot", [
    ({"overlay_session_id": "4", "snapshot_id": "s"}, 0, "s"),
    ({"overlay_session_id": False, "snapshot_id": "s"}, 0, "s"),
    ({"overlay_session_id": 4, "snapshot_id": None}, 4, ""),
])
def test_iw_33_unpin_with_malformed_fields_is_invalid_request(
        params, echo_session, echo_snapshot):
    with _side("Save") as side:
        reply = side.send_one("unpin_snapshot", **params)
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "error", "invalid_request", False)
    assert reply["overlay_session_id"] == echo_session
    assert reply["snapshot_id"] == echo_snapshot


def test_iw_34_unpin_with_the_overlay_off_is_refused():
    with _side("Save", click_config=OFF) as side:
        reply = side.send_one("unpin_snapshot", overlay_session_id=4,
                              snapshot_id="s")
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "disabled_by_config", False)


def test_iw_35_unpin_is_not_held_back_by_the_pin_generation_bar():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 9, 9, walk["snapshot_id"])
        reply = side.send_one("unpin_snapshot", overlay_session_id=1,
                              snapshot_id=walk["snapshot_id"])
    assert (reply["status"], reply["reason"]) == ("ok", None)


def test_iw_36_a_pinned_snapshot_survives_store_eviction():
    with _side("Save", click_config={"snapshot_store_capacity": 2}) as side:
        held = side.walk()
        _pin(side, 1, 1, held["snapshot_id"])
        side.walk()
        side.walk()
        side.walk()
        kept = side.send_one("show_numbered_overlay",
                             snapshot_id=held["snapshot_id"])
    assert kept["outcome"] == "ok"


def test_iw_37_an_unpinned_snapshot_is_evicted_when_the_store_is_full():
    with _side("Save", click_config={"snapshot_store_capacity": 2}) as side:
        held = side.walk()
        _pin(side, 1, 1, held["snapshot_id"])
        side.send_one("unpin_snapshot", overlay_session_id=1,
                      snapshot_id=held["snapshot_id"])
        side.walk()
        side.walk()
        side.walk()
        gone = side.send_one("show_numbered_overlay",
                             snapshot_id=held["snapshot_id"])
    assert gone["outcome"] == "snapshot_expired"


# ---------------------------------------------------------------------------
# refresh_overlay_snapshot
# ---------------------------------------------------------------------------


def test_iw_38_refresh_of_a_stored_snapshot_reports_it_found():
    with _side("Save") as side:
        walk = side.walk()
        reply = side.send_one("refresh_overlay_snapshot", overlay_session_id=4,
                              snapshot_id=walk["snapshot_id"])
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", None, True)
    assert (reply["overlay_session_id"], reply["snapshot_id"]) == (
        4, walk["snapshot_id"])


def test_iw_39_refresh_of_an_unknown_snapshot_is_unknown_snapshot():
    with _side("Save") as side:
        reply = side.send_one("refresh_overlay_snapshot", overlay_session_id=4,
                              snapshot_id="walk-none")
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "unknown_snapshot", False)


def test_iw_40_refresh_with_a_non_string_id_is_invalid_request():
    with _side("Save") as side:
        reply = side.send_one("refresh_overlay_snapshot", overlay_session_id=4,
                              snapshot_id=7)
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "error", "invalid_request", False)
    assert reply["snapshot_id"] == ""


def test_iw_41_refresh_with_a_bad_session_id_is_not_rejected():
    # Only the snapshot id is checked. The reply echoes a bad session as 0.
    with _side("Save") as side:
        walk = side.walk()
        reply = side.send_one("refresh_overlay_snapshot",
                              overlay_session_id="x",
                              snapshot_id=walk["snapshot_id"])
    assert (reply["status"], reply["pinned"]) == ("ok", True)
    assert reply["overlay_session_id"] == 0


def test_iw_42_refresh_with_the_overlay_off_is_refused():
    with _side("Save", click_config=OFF) as side:
        reply = side.send_one("refresh_overlay_snapshot", overlay_session_id=4,
                              snapshot_id="s")
    assert (reply["status"], reply["reason"], reply["pinned"]) == (
        "ok", "disabled_by_config", False)


def test_iw_43_refresh_is_not_held_back_by_the_pin_generation_bar():
    with _side("Save") as side:
        walk = side.walk()
        _pin(side, 9, 9, walk["snapshot_id"])
        reply = side.send_one("refresh_overlay_snapshot", overlay_session_id=1,
                              snapshot_id=walk["snapshot_id"])
    assert reply["pinned"] is True


# ---------------------------------------------------------------------------
# show_numbered_overlay
# ---------------------------------------------------------------------------


def _show(side, snapshot_id, **extra):
    extra.setdefault("overlay_session_id", 6)
    extra.setdefault("paint_generation", 3)
    extra.setdefault("trace_id", "t-show")
    return side.send_one("show_numbered_overlay", snapshot_id=snapshot_id,
                         **extra)


def test_iw_44_show_repaints_the_whole_stored_snapshot():
    with _side("Save", "Open", "Cancel") as side:
        walk = side.walk()
        reply = _show(side, walk["snapshot_id"])
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "ok", None)
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert reply["snapshot_summary"]["snapshot_id"] == walk["snapshot_id"]
    assert _names(reply) == ["Save", "Open", "Cancel"]
    assert _numbers(reply) == [1, 2, 3]
    assert (reply["overlay_session_id"], reply["paint_generation"],
            reply["trace_id"]) == (6, 3, "t-show")


def test_iw_45_show_with_a_filter_keeps_only_those_items_renumbered_from_one():
    with _side("Save", "Open", "Cancel") as side:
        walk = side.walk()
        # The filter lists Cancel before Save; the result is in reading order.
        keep = [item_id_for(walk, "Cancel"), item_id_for(walk, "Save")]
        reply = _show(side, walk["snapshot_id"], item_id_filter=keep)
    assert reply["outcome"] == "ok"
    assert _names(reply) == ["Save", "Cancel"]
    assert _numbers(reply) == [1, 2]


def test_iw_46_show_with_an_empty_filter_is_no_targets_with_an_empty_list():
    with _side("Save", "Open") as side:
        walk = side.walk()
        reply = _show(side, walk["snapshot_id"], item_id_filter=[])
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "no_targets", None)
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert reply["snapshot_summary"]["items"] == []


def test_iw_47_show_with_a_filter_that_matches_nothing_is_no_targets():
    with _side("Save") as side:
        walk = side.walk()
        reply = _show(side, walk["snapshot_id"], item_id_filter=["uia-99"])
    assert reply["outcome"] == "no_targets"
    assert reply["snapshot_summary"]["items"] == []


def test_iw_48_show_of_an_unknown_snapshot_is_snapshot_expired():
    with _side("Save") as side:
        reply = _show(side, "walk-none")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "snapshot_expired", "stale_snapshot_id")
    assert reply["snapshot_id"] is None
    assert reply["snapshot_summary"] is None
    assert (reply["overlay_session_id"], reply["paint_generation"],
            reply["trace_id"]) == (6, 3, "t-show")


def test_iw_49_show_after_the_foreground_window_changed_is_snapshot_expired():
    with _side("Save") as side:
        walk = side.walk()
        side.foreground = OTHER.hwnd
        reply = _show(side, walk["snapshot_id"])
    assert reply["outcome"] == "snapshot_expired"


@pytest.mark.parametrize("params", [
    {"snapshot_id": 5},
    {"snapshot_id": "s", "item_id_filter": "uia-1"},
    {"snapshot_id": "s", "item_id_filter": ["uia-1", 2]},
    {"snapshot_id": "s", "overlay_session_id": "6"},
    {"snapshot_id": "s", "overlay_session_id": True},
    {"snapshot_id": "s", "paint_generation": 2.5},
    {"snapshot_id": "s", "trace_id": 9},
])
def test_iw_50_show_with_malformed_fields_is_invalid_request(params):
    with _side("Save") as side:
        reply = side.send_one("show_numbered_overlay", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "error", "invalid_request")
    assert reply["snapshot_id"] is None
    assert reply["snapshot_summary"] is None
    # The echoed scalars are coerced so the reply still parses in Logic.
    assert isinstance(reply["trace_id"], str)
    assert isinstance(reply["overlay_session_id"], int)
    assert not isinstance(reply["overlay_session_id"], bool)
    assert isinstance(reply["paint_generation"], int)


def test_iw_87_show_hit_by_an_unexpected_error_is_a_status_error():
    # No answer the real OS gives is known to reach this path. The test makes the
    # cursor position read answer with a value of the wrong shape, an error
    # class the foreground read does not guard, to model an unexpected exception.
    with _side("Save") as side:
        walk = side.walk()
        side.cursor_pos = None
        reply = _show(side, walk["snapshot_id"])
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "error", "unexpected_error")
    assert reply["snapshot_id"] is None
    assert reply["snapshot_summary"] is None
    assert (reply["overlay_session_id"], reply["paint_generation"],
            reply["trace_id"]) == (6, 3, "t-show")


def test_iw_51_show_with_the_overlay_off_is_refused():
    with _side("Save", click_config=OFF) as side:
        reply = _show(side, "s")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "disabled_by_config")
    assert reply["snapshot_id"] is None


def test_iw_52_show_with_automation_unavailable_says_so():
    with _side("Save", automation_fails=True) as side:
        reply = _show(side, "s")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "execution_failed", "automation_unavailable")


# ---------------------------------------------------------------------------
# click_snapshot_item
# ---------------------------------------------------------------------------


def _click(side, snapshot_id, item_id, **extra):
    extra.setdefault("trace_id", "t-click")
    return side.send_one("click_snapshot_item", snapshot_id=snapshot_id,
                         item_id=item_id, **extra)


def test_iw_53_click_presses_the_named_item_and_reports_ok():
    with _side("Save", "Open", "Cancel") as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Open"))
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "ok", "ok", None)
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert reply["matched_name"] == "Open"
    assert list(reply["matched_names"]) == ["Open"]
    assert reply["snapshot_summary"] is None
    assert reply["trace_id"] == "t-click"
    assert pressed == ["Open"]


@pytest.mark.parametrize("snapshot_id,item_id", [
    ("", "uia-1"), ("walk-x", ""), (None, "uia-1"), ("walk-x", 4),
])
def test_iw_54_click_with_a_missing_or_bad_id_is_invalid_request(
        snapshot_id, item_id):
    with _side("Save") as side:
        reply = side.send_one("click_snapshot_item", snapshot_id=snapshot_id,
                              item_id=item_id, trace_id="t")
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "invalid_request")
    assert reply["snapshot_id"] is None
    assert pressed == []


def test_iw_55_click_with_a_bad_trace_id_still_gets_a_string_trace_id():
    with _side("Save") as side:
        reply = side.send_one("click_snapshot_item", snapshot_id="",
                              item_id="", trace_id=17)
    assert reply["trace_id"] == ""


def test_iw_56_click_with_the_overlay_off_is_refused():
    with _side("Save", click_config=OFF) as side:
        reply = _click(side, "s", "i")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "disabled_by_config")


def test_iw_57_click_with_automation_unavailable_says_so():
    with _side("Save", automation_fails=True) as side:
        reply = _click(side, "s", "i")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "automation_unavailable")


def test_iw_58_click_on_an_unknown_snapshot_is_snapshot_expired():
    with _side("Save") as side:
        reply = _click(side, "walk-none", "uia-1")
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "snapshot_expired")
    assert reply["snapshot_id"] == "walk-none"
    assert pressed == []


def test_iw_59_click_after_the_foreground_window_changed_is_snapshot_expired():
    with _side("Save") as side:
        walk = side.walk()
        side.foreground = OTHER.hwnd
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        pressed = side.invoked_names
    assert reply["reason"] == "snapshot_expired"
    assert pressed == []


def test_iw_60_click_on_an_item_not_in_the_snapshot_is_item_not_found():
    with _side("Save") as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], "uia-99")
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "item_not_found")
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert pressed == []


def test_iw_61_click_on_a_disabled_control_is_refused_with_the_executor_reason():
    elements = [FakeUiaElement("Save", current_enabled=False)]
    with InputSide(trees={NOTEPAD.hwnd: elements}) as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "disabled")
    assert reply["matched_name"] == "Save"
    assert list(reply["matched_names"]) == ["Save"]
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert pressed == []


def test_iw_62_click_on_a_control_that_moved_is_refused_as_bounds_stale():
    elements = [FakeUiaElement("Save", current_rect=(400, 400, 500, 450))]
    with InputSide(trees={NOTEPAD.hwnd: elements}) as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        pressed = side.invoked_names
    assert reply["reason"] == "bounds_stale"
    assert pressed == []


@pytest.mark.parametrize("error", [
    pytest.param(COMError(-2147467259, "Unspecified error",
                          (None, None, None, 0, None)), id="com-error"),
    pytest.param(RuntimeError("press failed"), id="plain-error"),
])
def test_iw_84_click_whose_press_fails_is_refused_as_invoke_com_error(error):
    # An HRESULT that is not on the no-side-effect list, and an error that is
    # not a COM error at all, both fail closed: no mouse click follows.
    elements = [FakeUiaElement("Save", invoke_raises=error)]
    with InputSide(trees={NOTEPAD.hwnd: elements}) as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        mouse, pressed = side.mouse_clicks, side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "invoke_com_error")
    assert reply["matched_name"] == "Save"
    assert reply["snapshot_id"] == walk["snapshot_id"]
    assert pressed == ["Save"]
    assert mouse == []


def test_iw_88_click_hit_by_an_unexpected_error_is_refused_as_invoke_com_error():
    # Same model as IW-87: a cursor position read of the wrong shape.
    with _side("Save") as side:
        walk = side.walk()
        side.cursor_pos = None
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        pressed = side.invoked_names
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "invoke_com_error")
    assert reply["snapshot_id"] is None
    assert reply["trace_id"] == "t-click"
    assert pressed == []


def test_iw_63_click_asks_the_executor_to_press_not_to_use_the_mouse():
    with _side("Save") as side:
        walk = side.walk()
        _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        mouse, pressed = side.mouse_clicks, side.invoked_names
    assert mouse == []
    assert pressed == ["Save"]


def test_iw_64_click_with_a_right_click_gesture_uses_the_mouse_not_invoke():
    with _side("Save") as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"),
                       gesture="right_click")
        mouse, pressed = side.mouse_clicks, side.invoked_names
    assert reply["outcome"] == "ok"
    assert [(b, n) for _x, _y, b, n in mouse] == [("right", 1)]
    assert pressed == []


def test_iw_65_click_with_a_double_click_gesture_clicks_twice():
    with _side("Save") as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"),
                       gesture="double_click")
        mouse = side.mouse_clicks
    assert reply["outcome"] == "ok"
    assert [(b, n) for _x, _y, b, n in mouse] == [("left", 2)]


@pytest.mark.parametrize("gesture", ["", "flying", None, 7])
def test_iw_66_click_with_an_unknown_gesture_uses_invoke(gesture):
    with _side("Save") as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"),
                       gesture=gesture)
        mouse, pressed = side.mouse_clicks, side.invoked_names
    assert reply["outcome"] == "ok"
    assert pressed == ["Save"]
    assert mouse == []


def test_iw_67_click_from_a_list_already_clicked_is_stale_overlay_generation():
    with _side("Save", "Open") as side:
        walk = side.walk()
        save = item_id_for(walk, "Save")
        first = _click(side, walk["snapshot_id"], save,
                       overlay_session_id=3, paint_generation=2)
        again = _click(side, walk["snapshot_id"], item_id_for(walk, "Open"),
                       overlay_session_id=3, paint_generation=2)
        older = _click(side, walk["snapshot_id"], save,
                       overlay_session_id=3, paint_generation=1)
        older_session = _click(side, walk["snapshot_id"], save,
                               overlay_session_id=2, paint_generation=9)
        pressed = side.invoked_names
    assert first["outcome"] == "ok"
    for refused in (again, older, older_session):
        assert (refused["status"], refused["outcome"], refused["reason"]) == (
            "error", "execution_failed", "stale_overlay_generation")
        assert refused["snapshot_id"] == walk["snapshot_id"]
    assert pressed == ["Save"]


def test_iw_68_click_from_a_newer_generation_or_session_is_accepted():
    with _side("Save", "Open", "Cancel") as side:
        walk = side.walk()
        ids = {n: item_id_for(walk, n) for n in ("Save", "Open", "Cancel")}
        _click(side, walk["snapshot_id"], ids["Save"],
               overlay_session_id=3, paint_generation=2)
        newer_gen = _click(side, walk["snapshot_id"], ids["Open"],
                           overlay_session_id=3, paint_generation=3)
        newer_session = _click(side, walk["snapshot_id"], ids["Cancel"],
                               overlay_session_id=4, paint_generation=0)
    assert newer_gen["outcome"] == "ok"
    assert newer_session["outcome"] == "ok"


def test_iw_69_a_refused_click_does_not_use_up_its_list():
    elements = [FakeUiaElement("Save", current_enabled=False)]
    with InputSide(trees={NOTEPAD.hwnd: elements}) as side:
        walk = side.walk()
        save = item_id_for(walk, "Save")
        refused = _click(side, walk["snapshot_id"], save,
                         overlay_session_id=3, paint_generation=2)
        elements[0].CurrentIsEnabled = True
        retry = _click(side, walk["snapshot_id"], save,
                       overlay_session_id=3, paint_generation=2)
    assert refused["reason"] == "disabled"
    assert retry["outcome"] == "ok"


def test_iw_70_click_without_a_list_pair_skips_the_stale_check():
    with _side("Save", "Open") as side:
        walk = side.walk()
        first = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        second = _click(side, walk["snapshot_id"], item_id_for(walk, "Open"))
        pressed = side.invoked_names
    assert first["outcome"] == second["outcome"] == "ok"
    assert pressed == ["Save", "Open"]


def test_iw_71_click_without_a_list_pair_does_not_reset_the_stale_check():
    with _side("Save", "Open") as side:
        walk = side.walk()
        save, opn = item_id_for(walk, "Save"), item_id_for(walk, "Open")
        _click(side, walk["snapshot_id"], save,
               overlay_session_id=3, paint_generation=2)
        _click(side, walk["snapshot_id"], opn)
        refused = _click(side, walk["snapshot_id"], opn,
                         overlay_session_id=3, paint_generation=2)
    assert refused["reason"] == "stale_overlay_generation"


@pytest.mark.parametrize("session,generation", [
    ("3", 2), (3, "2"), (True, 2), (3, False), (3, None), (None, 2),
])
def test_iw_72_click_with_a_bad_list_pair_skips_the_stale_check_and_is_not_recorded(
        session, generation):
    with _side("Save", "Open") as side:
        walk = side.walk()
        save, opn = item_id_for(walk, "Save"), item_id_for(walk, "Open")
        _click(side, walk["snapshot_id"], save,
               overlay_session_id=3, paint_generation=2)
        bad = _click(side, walk["snapshot_id"], opn,
                     overlay_session_id=session, paint_generation=generation)
        # The bad pair was not recorded: the good pair is still the bar.
        refused = _click(side, walk["snapshot_id"], opn,
                         overlay_session_id=3, paint_generation=2)
    assert bad["outcome"] == "ok"
    assert refused["reason"] == "stale_overlay_generation"


def test_iw_73_click_checks_the_list_pair_before_the_overlay_gate_and_the_store():
    with _side("Save") as side:
        walk = side.walk()
        save = item_id_for(walk, "Save")
        _click(side, walk["snapshot_id"], save,
               overlay_session_id=3, paint_generation=2)
        # The snapshot id is unknown, but the stale pair is reported first.
        reply = _click(side, "walk-none", "uia-1",
                       overlay_session_id=3, paint_generation=2)
    assert reply["reason"] == "stale_overlay_generation"
    assert reply["snapshot_id"] == "walk-none"


# ---------------------------------------------------------------------------
# All six: one reply for each request, and a dead reply queue is survived
# ---------------------------------------------------------------------------


class _DeadQueue:
    def put(self, _item):
        raise OSError("queue is closed")


@pytest.mark.parametrize("action,params", [
    ("start_overlay_walk", {"overlay_session_id": 1, "paint_generation": 1}),
    ("show_numbered_overlay", {"snapshot_id": "s"}),
    ("pin_snapshot", {"overlay_session_id": 1, "snapshot_id": "s",
                      "paint_generation": 1}),
    ("unpin_snapshot", {"overlay_session_id": 1, "snapshot_id": "s"}),
    ("refresh_overlay_snapshot", {"overlay_session_id": 1,
                                  "snapshot_id": "s"}),
    ("click_snapshot_item", {"snapshot_id": "s", "item_id": "i"}),
])
def test_iw_74_a_closed_reply_queue_does_not_stop_the_handler(action, params):
    with _side("Save", reply_queue=_DeadQueue()) as side:
        method = getattr(side.handler, action)
        assert method(request_id="r", **params) is None


@pytest.mark.parametrize("action,params", [
    ("start_overlay_walk", {"overlay_session_id": 1, "paint_generation": 1}),
    ("show_numbered_overlay", {"snapshot_id": "s"}),
    ("pin_snapshot", {"overlay_session_id": 1, "snapshot_id": "s",
                      "paint_generation": 1}),
    ("unpin_snapshot", {"overlay_session_id": 1, "snapshot_id": "s"}),
    ("refresh_overlay_snapshot", {"overlay_session_id": 1,
                                  "snapshot_id": "s"}),
    ("click_snapshot_item", {"snapshot_id": "s", "item_id": "i"}),
])
def test_iw_75_each_message_gets_exactly_one_reply_with_its_request_id(
        action, params):
    with _side("Save") as side:
        replies = side.send(action, **params)
    assert len(replies) == 1
    assert replies[0]["action"] == action
    assert replies[0]["request_id"].startswith("req-")


def test_iw_81_a_snapshot_dropped_by_a_foreground_change_stays_dropped():
    # Window A is walked, window B comes to the front and a show finds the
    # snapshot expired. When A is in front again the snapshot is still gone:
    # the mismatch dropped the stored entry, so a click cannot use it.
    with _side("Save") as side:
        walk = side.walk()
        side.foreground = OTHER.hwnd
        shown = _show(side, walk["snapshot_id"])
        side.foreground = NOTEPAD.hwnd
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Save"))
        pressed = side.invoked_names
    assert shown["outcome"] == "snapshot_expired"
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "snapshot_expired")
    assert pressed == []


def test_iw_82_click_on_a_control_with_no_invoke_uses_the_mouse_not_the_default_action():
    # A badge pick prefers the guarded coordinate click over the MSAA default
    # action when the control has no Invoke pattern. The control has a default
    # action, so a press-first order would use it and send no mouse click.
    elements = [FakeUiaElement("Menu", has_invoke=False,
                               default_action="Press")]
    with InputSide(trees={NOTEPAD.hwnd: elements}) as side:
        walk = side.walk()
        reply = _click(side, walk["snapshot_id"], item_id_for(walk, "Menu"))
        mouse, pressed = side.mouse_clicks, side.invoked_names
        default_actions = elements[0].default_actions_done
    assert reply["outcome"] == "ok"
    assert [(b, n) for _x, _y, b, n in mouse] == [("left", 1)]
    assert pressed == []
    assert default_actions == []
