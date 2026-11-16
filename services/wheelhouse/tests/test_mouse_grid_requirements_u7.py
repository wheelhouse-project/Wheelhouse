"""Boundary tests for area U7 of the mouse grid (place 3, stage A).

Area U7 is the Input-process half of the mouse grid and of the bare click
words: four actions of ``UIActionHandler`` (click_point, click_at_pointer,
move_pointer, perform_drag), which call ``MousePointerActions`` in
ui/mouse_pointer_actions.py since wh-full-refactor.6.2. Each test sends one message from Logic the way
``input_proc`` does (``getattr(handler, action)(request_id=..., **params)``) and
reads only what leaves the handler: the reply on the response queue, and the
calls on the fake of the SendInput primitives and of the pointer position read.
The ``channel_probe`` message, which Logic sends after a timeout, is covered
too. Rows are MP-1 and up (the notes file of this stage holds them until the
requirements document exists). Each test name carries its row.

The harness is tests/mouse_pointer_input_harness.py, built on
tests/overlay_stack_input_harness.py. No test waits in real time.
"""

from __future__ import annotations

import pytest

from tests.mouse_pointer_input_harness import PointerSide

ON = {"enabled": True}
OFF = {"enabled": False}
BAD_GRID_MIN = {"enabled": True, "grid_min_cell_px": 0}
BAD_GRID_DRAG = {"enabled": True, "drag_duration_ms": 9999}
BAD_ENABLED_TYPE = {"enabled": "yes"}

# Per action: the params Logic sends, and the primitive-call list that records it.
CLICK = ("click_point", {"x": 100, "y": 200}, "click_calls")
MOVE = ("move_pointer", {"x": 100, "y": 200}, "move_calls")
DRAG = ("perform_drag",
        {"start_x": 1, "start_y": 2, "end_x": 30, "end_y": 40}, "drag_calls")
AT_POINTER = ("click_at_pointer", {}, "click_calls")
ALL_FOUR = [
    pytest.param(*CLICK, id="click_point"),
    pytest.param(*AT_POINTER, id="click_at_pointer"),
    pytest.param(*MOVE, id="move_pointer"),
    pytest.param(*DRAG, id="perform_drag"),
]
GRID_THREE = [
    pytest.param(*CLICK, id="click_point"),
    pytest.param(*MOVE, id="move_pointer"),
    pytest.param(*DRAG, id="perform_drag"),
]

_RESULT_FIELD = {"click_calls": "click_result", "move_calls": "move_result",
                 "drag_calls": "drag_result"}


def _set_result(side: PointerSide, calls: str, value) -> None:
    setattr(side, _RESULT_FIELD[calls], value)


# ---------------------------------------------------------------------------
# The reply shape and the primitive result mapping (all four actions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_1_success_replies_ok_with_the_trace_id(action, params, calls):
    with PointerSide(click_config=ON) as side:
        reply = side.send_one(action, trace_id="trace-ok", **params)
    assert reply == {
        "status": "ok", "outcome": "ok", "reason": None,
        "trace_id": "trace-ok", "request_id": "req-1", "action": action,
    }
    assert len(getattr(side, calls)) == 1


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_2_primitive_refusal_reaches_logic_as_execution_failed(
        action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, (False, "cursor_did_not_land"))
        reply = side.send_one(action, trace_id="t-2", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "cursor_did_not_land")
    assert reply["trace_id"] == "t-2"
    assert len(getattr(side, calls)) == 1


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_3_refusal_without_a_reason_is_unexpected_error(
        action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, (False, None))
        reply = side.send_one(action, trace_id="t-3", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "unexpected_error")


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_4_primitive_that_raises_is_sendinput_error(action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, OSError("SendInput failed"))
        reply = side.send_one(action, trace_id="t-4", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "sendinput_error")
    assert reply["trace_id"] == "t-4"
    assert len(getattr(side, calls)) == 1


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_5_malformed_primitive_return_is_unexpected_error(
        action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, None)
        reply = side.send_one(action, trace_id="t-5", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "unexpected_error")
    assert reply["trace_id"] == "t-5"
    assert len(getattr(side, calls)) == 1


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_6_trace_id_that_is_not_a_string_becomes_empty(
        action, params, calls):
    with PointerSide(click_config=ON) as side:
        reply = side.send_one(action, trace_id=12345, **params)
    assert reply["trace_id"] == ""
    assert reply["status"] == "ok"


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_7_release_failed_reaches_logic_unchanged(action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, (False, "release_failed"))
        reply = side.send_one(action, trace_id="t-7", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "release_failed")


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_8_one_reply_per_message_in_every_outcome(action, params, calls):
    outcomes = [(True, None), (False, "invalid_point"), (False, None),
                OSError("boom"), None]
    with PointerSide(click_config=ON) as side:
        for outcome in outcomes:
            _set_result(side, calls, outcome)
            assert len(side.send(action, trace_id="t-8", **params)) == 1
    with PointerSide(click_config=OFF) as side:
        assert len(side.send(action, trace_id="t-8", **params)) == 1


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_9_unknown_message_keys_are_ignored(action, params, calls):
    with PointerSide(click_config=ON) as side:
        reply = side.send_one(action, trace_id="t-9", surprise="x", **params)
    assert reply["status"] == "ok"
    assert len(getattr(side, calls)) == 1


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", [
    pytest.param(OFF, id="enabled-false"),
    pytest.param(BAD_GRID_MIN, id="bad-grid-min-cell"),
    pytest.param(BAD_GRID_DRAG, id="bad-drag-duration"),
    pytest.param(BAD_ENABLED_TYPE, id="bad-enabled-type"),
])
@pytest.mark.parametrize("action, params, calls", GRID_THREE)
def test_mp_10_grid_actions_refuse_when_the_grid_is_off(
        action, params, calls, config):
    with PointerSide(click_config=config) as side:
        reply = side.send_one(action, trace_id="t-10", **params)
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "disabled_by_config")
    assert reply["trace_id"] == "t-10"
    assert side.all_calls == []


@pytest.mark.parametrize("action, params, calls", GRID_THREE)
def test_mp_11_grid_actions_run_when_only_an_overlay_key_is_off(
        action, params, calls):
    config = {"enabled": True, "overlay_enabled": False}
    with PointerSide(click_config=config) as side:
        reply = side.send_one(action, trace_id="t-11", **params)
    assert reply["status"] == "ok"
    assert len(getattr(side, calls)) == 1


@pytest.mark.parametrize("config", [
    pytest.param(OFF, id="enabled-false"),
    pytest.param(BAD_ENABLED_TYPE, id="bad-enabled-type"),
])
def test_mp_12_click_at_pointer_refuses_when_voice_clicking_is_off(config):
    with PointerSide(click_config=config) as side:
        reply = side.send_one("click_at_pointer", trace_id="t-12")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "disabled_by_config")
    assert side.all_calls == []


@pytest.mark.parametrize("config", [
    pytest.param(BAD_GRID_MIN, id="bad-grid-min-cell"),
    pytest.param(BAD_GRID_DRAG, id="bad-drag-duration"),
])
def test_mp_13_click_at_pointer_still_clicks_with_a_bad_grid_key(config):
    with PointerSide(click_config=config) as side:
        reply = side.send_one("click_at_pointer", trace_id="t-13")
    assert reply["status"] == "ok"
    assert side.click_calls == [(60, 45, "left", 1)]


def test_mp_14_the_config_default_allows_every_action():
    with PointerSide() as side:
        for action, params, calls in (CLICK, AT_POINTER, MOVE, DRAG):
            reply = side.send_one(action, trace_id="t", **params)
            assert reply["status"] == "ok"


# ---------------------------------------------------------------------------
# What reaches the primitive
# ---------------------------------------------------------------------------


def test_mp_15_click_point_passes_its_arguments_unconverted():
    with PointerSide(click_config=ON) as side:
        side.send_one("click_point", x=-5, y="7", button="right",
                      click_count=2, trace_id="t-15")
    assert side.click_calls == [(-5, "7", "right", 2)]


def test_mp_16_click_point_defaults_to_one_left_click():
    with PointerSide(click_config=ON) as side:
        side.send_one("click_point", x=3, y=4, trace_id="t-16")
    assert side.click_calls == [(3, 4, "left", 1)]


def test_mp_17_click_at_pointer_clicks_where_the_pointer_is():
    with PointerSide(click_config=ON) as side:
        side.pointer = (-1200, 340)
        side.send_one("click_at_pointer", button="right", click_count=3,
                      trace_id="t-17")
    assert side.click_calls == [(-1200, 340, "right", 3)]


@pytest.mark.parametrize("pointer", [
    pytest.param(OSError("secure desktop"), id="read-raises"),
    pytest.param("not-a-point", id="read-is-malformed"),
])
def test_mp_18_unreadable_pointer_sends_no_click(pointer):
    with PointerSide(click_config=ON) as side:
        side.pointer = pointer
        reply = side.send_one("click_at_pointer", trace_id="t-18")
    assert (reply["status"], reply["outcome"], reply["reason"]) == (
        "error", "execution_failed", "pointer_unavailable")
    assert reply["trace_id"] == "t-18"
    assert side.all_calls == []


def test_mp_19_move_pointer_moves_to_the_point_and_presses_nothing():
    with PointerSide(click_config=ON) as side:
        side.send_one("move_pointer", x=-40, y=900, trace_id="t-19")
    assert side.move_calls == [(-40, 900)]
    assert side.click_calls == [] and side.drag_calls == []


def test_mp_20_perform_drag_passes_the_points_and_the_duration():
    with PointerSide(click_config=ON) as side:
        side.send_one("perform_drag", start_x=1, start_y=2, end_x=30,
                      end_y=40, duration_ms=600, trace_id="t-20")
    assert side.drag_calls == [(1, 2, 30, 40, 600)]


def test_mp_21_perform_drag_uses_250_ms_when_logic_omits_the_duration():
    with PointerSide(click_config=ON) as side:
        side.send_one("perform_drag", start_x=1, start_y=2, end_x=30,
                      end_y=40, trace_id="t-21")
    assert side.drag_calls == [(1, 2, 30, 40, 250)]


# ---------------------------------------------------------------------------
# channel_probe and the record of the last pointer action
# ---------------------------------------------------------------------------


def test_mp_22_probe_before_any_action_reports_no_record():
    with PointerSide(click_config=ON) as side:
        reply = side.send_one("channel_probe", trace_id="probe-1")
    assert reply == {
        "status": "ok", "trace_id": "probe-1", "last_mouse_action": None,
        "request_id": "req-1", "action": "channel_probe",
    }
    assert side.all_calls == []


def test_mp_23_probe_trace_id_that_is_not_a_string_becomes_empty():
    with PointerSide(click_config=ON) as side:
        reply = side.send_one("channel_probe", trace_id=7)
    assert reply["trace_id"] == ""


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_24_probe_reports_the_last_action_and_its_failure(
        action, params, calls):
    with PointerSide(click_config=ON) as side:
        _set_result(side, calls, (False, "release_failed"))
        side.send_one(action, trace_id="act-1", **params)
        reply = side.send_one("channel_probe", trace_id="probe-2")
    assert reply["last_mouse_action"] == {
        "action": action, "succeeded": False, "reason": "release_failed",
        "trace_id": "act-1",
    }


def test_mp_25_probe_reports_success_as_succeeded_true_and_no_reason():
    with PointerSide(click_config=ON) as side:
        side.send_one("click_point", x=1, y=2, trace_id="act-2")
        reply = side.send_one("channel_probe", trace_id="probe-3")
    assert reply["last_mouse_action"] == {
        "action": "click_point", "succeeded": True, "reason": None,
        "trace_id": "act-2",
    }


def test_mp_26_probe_reports_only_the_most_recent_action():
    with PointerSide(click_config=ON) as side:
        side.send_one("click_point", x=1, y=2, trace_id="first")
        side.send_one("move_pointer", x=3, y=4, trace_id="second")
        reply = side.send_one("channel_probe", trace_id="probe-4")
    assert reply["last_mouse_action"]["action"] == "move_pointer"
    assert reply["last_mouse_action"]["trace_id"] == "second"


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_27_a_refusal_by_config_is_also_recorded(action, params, calls):
    with PointerSide(click_config=OFF) as side:
        side.send_one(action, trace_id="act-3", **params)
        reply = side.send_one("channel_probe", trace_id="probe-5")
    assert reply["last_mouse_action"] == {
        "action": action, "succeeded": False, "reason": "disabled_by_config",
        "trace_id": "act-3",
    }


def test_mp_28_a_trace_id_that_is_not_a_string_is_recorded_as_empty():
    with PointerSide(click_config=ON) as side:
        side.send_one("click_point", x=1, y=2, trace_id=99)
        reply = side.send_one("channel_probe", trace_id="probe-6")
    assert reply["last_mouse_action"]["trace_id"] == ""


@pytest.mark.parametrize("action, params, calls", ALL_FOUR)
def test_mp_29_the_record_survives_a_reply_that_could_not_be_queued(
        action, params, calls):
    with PointerSide(click_config=ON, fail_first_put=True) as side:
        _set_result(side, calls, (False, "release_failed"))
        lost = side.send(action, trace_id="act-4", **params)
        reply = side.send_one("channel_probe", trace_id="probe-7")
    assert lost == []
    assert reply["last_mouse_action"] == {
        "action": action, "succeeded": False, "reason": "release_failed",
        "trace_id": "act-4",
    }


def test_mp_30_a_probe_reply_that_could_not_be_queued_is_dropped_quietly():
    with PointerSide(click_config=ON, fail_first_put=True) as side:
        assert side.send("channel_probe", trace_id="probe-8") == []
        reply = side.send_one("channel_probe", trace_id="probe-9")
    assert reply["trace_id"] == "probe-9"
