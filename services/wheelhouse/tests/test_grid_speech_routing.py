"""Logic-side speech routing for the mouse grid (wh-grid-speech-routing).

Layers under test:

  1. ``LogicController.forward_click_element`` grid claim: while the grid
     is open, a bare 1..9 number query (word or digit, no role) is
     consumed by the grid -- refine + act per the query gesture -- and
     NEVER reaches the by-name IPC or the numbered-overlay router. With
     the grid closed the existing routing (numbered overlay, then
     by-name) is untouched.
  2. ``LogicController.handle_grid_command`` -- the effect-performing
     integration for open / dismiss / next_screen / refine / mark /
     drag / move_here / click, including the [click] config gate, the
     NO_MONITOR degrade, and the True/False consumed contract that
     drives the actions-layer dictation fallback.
  3. ``LogicController._send_mouse_action`` failure notices, including
     the distinct release_failed wording tag.
  4. Mutual exclusion: opening the numbered overlay closes the grid.
  5. Gesture threading: a gesture query routed to the numbered overlay
     carries the gesture into the ``click_snapshot_item`` request.
  6. Pattern-catalog ordering for the new grid/gesture patterns.
  7. Notice wording for the new grid reason tags.

Effects are asserted as DATA (GUI queue payload dicts, captured
send_request calls), mirroring tests/test_voice_overlay_routing.py.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional
from unittest import mock
from unittest.mock import MagicMock

import pytest

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    OverlayState,
)
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.grid_overlay_state import (
    GridEvent,
    GridEventKind,
    GridOverlayStateMachine,
    GridRect,
    cell_center,
    cell_rect,
)
from services.wheelhouse.ui.element_types import (
    ClickGesture,
    ElementQuery,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)


MON = GridRect(0, 0, 1920, 1080)
MON2 = GridRect(1920, 0, 1920, 1080)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=f"control {n}",
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n in display_numbers
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


def _cache_with(*summaries: WalkSnapshotSummary) -> ClickSnapshotSummaryCache:
    cache = ClickSnapshotSummaryCache()
    for s in summaries:
        cache.put(s.snapshot_id, s)
    return cache


def _query(
    name: str,
    role: Optional[str] = None,
    gesture: ClickGesture = ClickGesture.INVOKE,
) -> ElementQuery:
    return ElementQuery(name, role, None, None, name, gesture=gesture)


def _probe_reply(params, *, last_mouse_action=None) -> dict:
    """The reply ``UIActionHandler.channel_probe`` actually emits.

    Since wh-mouse-grid.1.24 the probe is handler-owned (it is in
    ``input_proc._HANDLES_OWN_RESPONSE``), so the generic
    ``path=heuristic_done`` response can never be produced for it; every
    scripted ``send_request`` in this file that answers a ``channel_probe``
    must return THIS envelope (wh-mouse-grid.1.26). Asserts the probe
    request carried a nonempty ``trace_id``, mirroring the real request
    built by ``_confirm_grid_mouse_channel``, and echoes it back the way
    the real handler does.
    """
    assert isinstance(params, dict) and params.get("trace_id"), (
        f"channel_probe request must carry a nonempty trace_id, got {params!r}"
    )
    return {
        "status": "ok",
        "action": "channel_probe",
        "trace_id": params["trace_id"],
        "last_mouse_action": last_mouse_action,
        "request_id": "req-probe",
    }


def _make_controller(
    *,
    grid_open: bool = False,
    grid_monitor: GridRect = MON,
    enabled: bool = True,
    overlay_state: OverlayState = OverlayState.CLOSED,
    pinned_snapshot_id: Optional[str] = None,
    cache_summaries=(),
    monitors: tuple = (MON, MON2),
    focused: Optional[GridRect] = MON,
):
    """Build a MagicMock(spec=LogicController) wired for grid routing.

    The grid-facing methods are bound from the real class; the state
    machines are real; app.send_request and the GUI queue are captured.
    """
    from main import LogicController
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    for name in (
        "forward_click_element",
        "handle_grid_command",
        "_perform_grid_effects",
        "_forward_click_notice",
        "_close_grid_for_numbered_overlay",
        "_send_mouse_action",
        "_confirm_grid_mouse_channel",
        "_dispatch_mouse_action",
        "handle_overlay_command",
        "_hold_click_n",
        "_grid_monitor_context_off_loop",
        "_grid_gesture_buttons",
        "_put_grid_gui_event",
    ):
        setattr(c, name, getattr(LogicController, name).__get__(c))

    c.click_config = ClickConfig.from_raw(
        {"enabled": enabled, "response_timeout_ms": 3000}
    )
    c._click_disabled_notice_shown = False

    grid = GridOverlayStateMachine()
    if grid_open:
        result = grid.apply(
            GridEvent(
                GridEventKind.OPEN_GRID,
                monitors=(grid_monitor,),
                focused_monitor=grid_monitor,
            )
        )
        assert grid.is_open, result
    c.grid_overlay_state = grid

    machine = ClickOverlayStateMachine()
    machine.state = overlay_state
    machine.pinned_snapshot_id = pinned_snapshot_id
    c.click_overlay_state = machine
    c._overlay_focus_debouncer = FocusChangeDebouncer()
    c.click_snapshot_summary_cache = _cache_with(*cache_summaries)

    gui_events: list = []
    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue.put_nowait = gui_events.append
    c._gui_events = gui_events

    captured: dict = {"calls": []}

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        captured["calls"].append((action, params, timeout_s))
        captured["action"] = action
        captured["params"] = params
        override = getattr(c, "_mouse_response", None)
        if action in ("click_point", "move_pointer", "perform_drag"):
            if isinstance(override, BaseException):
                raise override
            if override is not None:
                return override
            from shared.mouse_action import MouseActionResponse
            return MouseActionResponse(
                status="ok", outcome="ok", reason=None, trace_id="t",
            ).to_dict()
        if action == "channel_probe":
            return _probe_reply(params)
        from shared.click_element import ClickElementResponse
        return ClickElementResponse(
            status="ok", outcome="not_found", reason=None,
            matched_names=(), snapshot_id=None, snapshot_summary=None,
            matched_name=None, trace_id="trace",
        ).to_dict()

    c.app = MagicMock()
    c.app.send_request = _send_request
    c._captured = captured

    async def _hint(_trace):
        return None

    c._maybe_show_first_use_hint = _hint

    c._grid_monitor_context = lambda: (monitors, focused)

    c._tasks = []

    def _create_task(coro, name=None):
        task = asyncio.get_event_loop().create_task(coro)
        c._tasks.append(task)
        return task

    c.create_task_with_error_handling = _create_task
    return c


def _run(c, coro):
    """Run ``coro`` then drain the background tasks the call scheduled."""

    async def _inner():
        result = await coro
        while c._tasks:
            pending, c._tasks = c._tasks, []
            await asyncio.gather(*pending)
        return result

    return asyncio.run(_inner())


def _notices(c) -> list:
    return [e for e in c._gui_events if e.get("action") == "show_click_notice"]


def _gui(c, action: str) -> list:
    return [e for e in c._gui_events if e.get("action") == action]


def _mouse_calls(c) -> list:
    return [
        (a, p) for (a, p, _t) in c._captured["calls"]
        if a in ("click_point", "move_pointer", "perform_drag")
    ]


# ---------------------------------------------------------------------------
# 1. forward_click_element: the grid claim.
# ---------------------------------------------------------------------------


def test_grid_open_bare_number_is_claimed_refine_then_click():
    c = _make_controller(grid_open=True)
    _run(c, c.forward_click_element(_query("3"), "tr"))
    # No by-name IPC, no click_element call.
    assert c._captured.get("action") != "click_element"
    # Refined to cell 3, then clicked at its center, then the grid closed.
    center = cell_center(cell_rect(MON, 3))
    calls = _mouse_calls(c)
    assert len(calls) == 1
    action, params = calls[0]
    assert action == "click_point"
    assert params["x"] == center.x and params["y"] == center.y
    assert params["button"] == "left" and params["click_count"] == 1
    assert not c.grid_overlay_state.is_open
    assert _gui(c, "clear_grid"), "grid close must clear the paint"


def test_grid_open_number_word_is_claimed():
    c = _make_controller(grid_open=True)
    _run(c, c.forward_click_element(_query("three"), "tr"))
    assert c._captured.get("action") != "click_element"
    assert len(_mouse_calls(c)) == 1
    assert not c.grid_overlay_state.is_open


def test_grid_open_right_click_number_uses_right_button():
    c = _make_controller(grid_open=True)
    _run(
        c,
        c.forward_click_element(
            _query("3", gesture=ClickGesture.RIGHT_CLICK), "tr",
        ),
    )
    ((action, params),) = _mouse_calls(c)
    assert action == "click_point"
    assert params["button"] == "right" and params["click_count"] == 1


def test_grid_open_double_click_number_uses_click_count_two():
    c = _make_controller(grid_open=True)
    _run(
        c,
        c.forward_click_element(
            _query("3", gesture=ClickGesture.DOUBLE_CLICK), "tr",
        ),
    )
    ((action, params),) = _mouse_calls(c)
    assert action == "click_point"
    assert params["button"] == "left" and params["click_count"] == 2


def test_grid_open_non_number_goes_by_name():
    c = _make_controller(grid_open=True)
    _run(c, c.forward_click_element(_query("cancel"), "tr"))
    assert c._captured.get("action") == "click_element"
    assert c.grid_overlay_state.is_open


def test_grid_open_role_query_goes_by_name():
    # "click seven button" -> name="seven", role="Button": by-name, not grid.
    c = _make_controller(grid_open=True)
    _run(c, c.forward_click_element(_query("7", role="Button"), "tr"))
    assert c._captured.get("action") == "click_element"
    assert c.grid_overlay_state.is_open


def test_grid_open_out_of_range_number_goes_by_name():
    # 12 is a valid overlay number but not a grid cell; grid must not claim.
    c = _make_controller(grid_open=True)
    _run(c, c.forward_click_element(_query("12"), "tr"))
    assert c._captured.get("action") == "click_element"
    assert c.grid_overlay_state.is_open


def test_grid_closed_number_goes_by_name():
    c = _make_controller(grid_open=False)
    _run(c, c.forward_click_element(_query("7"), "tr"))
    assert c._captured.get("action") == "click_element"


def test_grid_closed_overlay_painted_number_routes_snapshot_item():
    c = _make_controller(
        grid_open=False,
        overlay_state=OverlayState.PAINTED,
        pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1, 2, 3),),
    )
    c._dispatch_snapshot_item_click = MagicMock()
    _run(c, c.forward_click_element(_query("2"), "tr"))
    assert c._captured.get("action") != "click_element"
    c._dispatch_snapshot_item_click.assert_called_once()


def test_grid_open_takes_precedence_over_overlay_routing():
    # Mutual exclusion should keep both from being open, but if they ever
    # are, the grid claim runs first (same precedence slot, grid checked
    # before route_click_n).
    c = _make_controller(
        grid_open=True,
        overlay_state=OverlayState.PAINTED,
        pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1, 2, 3),),
    )
    c._dispatch_snapshot_item_click = MagicMock()
    _run(c, c.forward_click_element(_query("2"), "tr"))
    c._dispatch_snapshot_item_click.assert_not_called()
    assert len(_mouse_calls(c)) == 1


def test_no_grid_machine_attr_degrades_to_existing_routing():
    c = _make_controller(grid_open=False)
    del c.grid_overlay_state
    _run(c, c.forward_click_element(_query("7"), "tr"))
    assert c._captured.get("action") == "click_element"


# ---------------------------------------------------------------------------
# 2. handle_grid_command.
# ---------------------------------------------------------------------------


def test_open_paints_grid_on_focused_monitor_and_closes_overlay():
    c = _make_controller()
    hidden = []

    async def _overlay(command, trace_id):
        hidden.append(command)

    c.handle_overlay_command = _overlay
    consumed = _run(c, c.handle_grid_command("open", "tr"))
    assert consumed is True
    assert c.grid_overlay_state.is_open
    paints = _gui(c, "paint_grid")
    assert len(paints) == 1
    assert paints[0]["monitor_left"] == MON.left
    assert paints[0]["monitor_width"] == MON.width
    assert paints[0]["left"] == MON.left and paints[0]["width"] == MON.width
    assert hidden == ["hide"], "open must close the numbered overlay"


def test_open_with_no_monitors_fires_notice_not_crash():
    c = _make_controller(monitors=(), focused=None)
    consumed = _run(c, c.handle_grid_command("open", "tr"))
    assert consumed is True
    assert not c.grid_overlay_state.is_open
    (notice,) = _notices(c)
    assert notice["reason"] == "grid_no_monitor"


def test_open_with_no_focused_monitor_falls_back_to_primary():
    c = _make_controller(monitors=(MON, MON2), focused=None)

    async def _overlay(command, trace_id):
        return None

    c.handle_overlay_command = _overlay
    _run(c, c.handle_grid_command("open", "tr"))
    assert c.grid_overlay_state.is_open
    (paint,) = _gui(c, "paint_grid")
    assert paint["monitor_left"] == MON.left


def test_open_disabled_by_config_shows_one_shot_notice():
    c = _make_controller(enabled=False)
    assert _run(c, c.handle_grid_command("open", "tr")) is True
    assert not c.grid_overlay_state.is_open
    (notice,) = _notices(c)
    assert notice["reason"] == "disabled_by_config"
    # Second attempt: consumed, but no second notice.
    assert _run(c, c.handle_grid_command("open", "tr2")) is True
    assert len(_notices(c)) == 1


def test_grid_only_words_not_consumed_when_closed():
    c = _make_controller(grid_open=False)
    for command in ("mark", "drag", "move_here", "click", "refine"):
        assert _run(
            c, c.handle_grid_command(command, "tr", number=5),
        ) is False, command
    assert c._gui_events == []
    assert _mouse_calls(c) == []


def test_grid_only_words_not_consumed_when_disabled():
    c = _make_controller(enabled=False)
    assert _run(c, c.handle_grid_command("click", "tr")) is False
    assert _notices(c) == []


def test_refine_repaints_smaller_rect():
    c = _make_controller(grid_open=True)
    consumed = _run(c, c.handle_grid_command("refine", "tr", number=3))
    assert consumed is True
    assert c.grid_overlay_state.is_open
    (paint,) = _gui(c, "paint_grid")
    expected = cell_rect(MON, 3)
    assert paint["left"] == expected.left
    assert paint["top"] == expected.top
    assert paint["width"] == expected.width
    assert paint["height"] == expected.height


def test_refine_below_min_cell_is_consumed_silently():
    tiny = GridRect(0, 0, 60, 60)
    c = _make_controller(grid_open=True, grid_monitor=tiny)
    # First refine: 60x60 -> 20x20 cell, allowed (the current rect clears
    # the floor). Second refine: current rect 20x20 < grid_min_cell_px 24
    # -> IGNORED_MIN_CELL, still consumed, silent.
    _run(c, c.handle_grid_command("refine", "tr", number=1))
    paints_before = len(_gui(c, "paint_grid"))
    consumed = _run(c, c.handle_grid_command("refine", "tr", number=1))
    assert consumed is True, "IGNORED_MIN_CELL is still consumed by the grid"
    assert c.grid_overlay_state.is_open
    assert len(_gui(c, "paint_grid")) == paints_before
    assert _notices(c) == []


def test_mark_paints_pin_and_keeps_grid_open():
    c = _make_controller(grid_open=True)
    consumed = _run(c, c.handle_grid_command("mark", "tr"))
    assert consumed is True
    assert c.grid_overlay_state.is_open
    center = cell_center(MON)
    (pin,) = _gui(c, "paint_grid_pin")
    assert pin["x"] == center.x and pin["y"] == center.y


def test_drag_without_mark_fires_notice_no_input():
    c = _make_controller(grid_open=True)
    consumed = _run(c, c.handle_grid_command("drag", "tr"))
    assert consumed is True
    assert c.grid_overlay_state.is_open
    (notice,) = _notices(c)
    assert notice["reason"] == "drag_without_mark"
    assert _mouse_calls(c) == []


def test_mark_refine_drag_sends_perform_drag_and_closes():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_grid_command("mark", "tr"))
    start = cell_center(MON)
    _run(c, c.handle_grid_command("refine", "tr", number=3))
    end = cell_center(cell_rect(MON, 3))
    consumed = _run(c, c.handle_grid_command("drag", "tr"))
    assert consumed is True
    assert not c.grid_overlay_state.is_open
    ((action, params),) = _mouse_calls(c)
    assert action == "perform_drag"
    assert params["start_x"] == start.x and params["start_y"] == start.y
    assert params["end_x"] == end.x and params["end_y"] == end.y
    assert params["duration_ms"] == 250
    assert _gui(c, "clear_grid")


def test_move_here_moves_pointer_and_closes():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_grid_command("refine", "tr", number=7))
    center = cell_center(cell_rect(MON, 7))
    consumed = _run(c, c.handle_grid_command("move_here", "tr"))
    assert consumed is True
    assert not c.grid_overlay_state.is_open
    ((action, params),) = _mouse_calls(c)
    assert action == "move_pointer"
    assert params["x"] == center.x and params["y"] == center.y


def test_bare_click_acts_at_current_center():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_grid_command("refine", "tr", number=9))
    center = cell_center(cell_rect(MON, 9))
    consumed = _run(
        c,
        c.handle_grid_command(
            "click", "tr", gesture=ClickGesture.RIGHT_CLICK,
        ),
    )
    assert consumed is True
    assert not c.grid_overlay_state.is_open
    ((action, params),) = _mouse_calls(c)
    assert action == "click_point"
    assert params["x"] == center.x and params["y"] == center.y
    assert params["button"] == "right"


def test_dismiss_clears_and_closes():
    c = _make_controller(grid_open=True)
    consumed = _run(c, c.handle_grid_command("dismiss", "tr"))
    assert consumed is True
    assert not c.grid_overlay_state.is_open
    assert _gui(c, "clear_grid")


def test_dismiss_when_closed_is_consumed_and_defensively_clears():
    # wh-mouse-grid.1.3: the spoken dismiss is the recovery path for a
    # paint that outlived the state machine, so it must reach the GUI as
    # a clear_grid even when Logic already believes the grid is closed.
    c = _make_controller(grid_open=False)
    assert _run(c, c.handle_grid_command("dismiss", "tr")) is True
    assert _gui(c, "clear_grid")


def test_next_screen_repaints_on_next_monitor():
    c = _make_controller(grid_open=True)
    consumed = _run(c, c.handle_grid_command("next_screen", "tr"))
    assert consumed is True
    assert c.grid_overlay_state.is_open
    (paint,) = _gui(c, "paint_grid")
    assert paint["monitor_left"] == MON2.left
    assert paint["left"] == MON2.left and paint["width"] == MON2.width


def test_next_screen_when_closed_is_consumed_noop():
    c = _make_controller(grid_open=False)
    assert _run(c, c.handle_grid_command("next_screen", "tr")) is True
    assert c._gui_events == []


# ---------------------------------------------------------------------------
# 3. Mouse-action failure notices.
# ---------------------------------------------------------------------------


def _failed_mouse_response(reason: str) -> dict:
    # status="error" matches the ONLY real producer,
    # UIActionHandler._emit_mouse_action_response, which always pairs
    # status="error" with outcome="execution_failed"
    # (wh-mouse-grid.1.14: a fixture claiming status="ok" for a failure
    # exercised a wire shape no Input process ever sends).
    from shared.mouse_action import MouseActionResponse

    return MouseActionResponse(
        status="error", outcome="execution_failed", reason=reason,
        trace_id="t",
    ).to_dict()


def test_click_failure_fires_grid_click_failed_notice():
    c = _make_controller(grid_open=True)
    c._mouse_response = _failed_mouse_response("cursor_did_not_land")
    _run(c, c.handle_grid_command("click", "tr"))
    (notice,) = _notices(c)
    assert notice["reason"] == "grid_click_failed"


def test_drag_release_failed_gets_distinct_notice():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_grid_command("mark", "tr"))
    c._mouse_response = _failed_mouse_response("release_failed")
    _run(c, c.handle_grid_command("drag", "tr"))
    reasons = [n["reason"] for n in _notices(c)]
    assert "grid_button_release_failed" in reasons


def test_click_release_failed_gets_the_same_distinct_notice():
    # wh-mouse-grid.1.5: click_point can now report release_failed too (a
    # partial click batch whose compensating release was refused), so the
    # held-button notice is shared by every grid mouse action, not drags
    # alone.
    c = _make_controller(grid_open=True)
    c._mouse_response = _failed_mouse_response("release_failed")
    _run(c, c.handle_grid_command("click", "tr"))
    reasons = [n["reason"] for n in _notices(c)]
    assert "grid_button_release_failed" in reasons


def test_drag_failure_fires_grid_drag_failed_notice():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_grid_command("mark", "tr"))
    c._mouse_response = _failed_mouse_response("sendinput_error")
    _run(c, c.handle_grid_command("drag", "tr"))
    reasons = [n["reason"] for n in _notices(c)]
    assert "grid_drag_failed" in reasons


def test_move_timeout_fires_grid_move_failed_notice():
    c = _make_controller(grid_open=True)
    c._mouse_response = asyncio.TimeoutError()
    _run(c, c.handle_grid_command("move_here", "tr"))
    (notice,) = _notices(c)
    assert notice["reason"] == "grid_move_failed"


def test_malformed_mouse_response_fires_failure_notice():
    c = _make_controller(grid_open=True)
    c._mouse_response = {"nonsense": True}
    _run(c, c.handle_grid_command("click", "tr"))
    (notice,) = _notices(c)
    assert notice["reason"] == "grid_click_failed"


# ---------------------------------------------------------------------------
# 4. Mutual exclusion: numbered overlay opening closes the grid.
# ---------------------------------------------------------------------------


def test_show_numbers_closes_open_grid():
    c = _make_controller(grid_open=True)
    _run(c, c.handle_overlay_command("show", "tr"))
    assert not c.grid_overlay_state.is_open
    assert _gui(c, "clear_grid")


def test_auto_open_ambiguous_closes_open_grid():
    from main import LogicController

    c = _make_controller(grid_open=True)
    c._perform_auto_open_ambiguous = LogicController._perform_auto_open_ambiguous.__get__(c)
    response = MagicMock()
    response.snapshot_id = "snap"
    response.ambiguous_item_ids = ("a", "b")
    response.outcome = "ambiguous"
    response.reason = None
    response.matched_name = None
    response.matched_names = ("A", "B")
    response.trace_id = "tr"
    c._perform_auto_open_ambiguous(response, "spoken", "tr")
    assert not c.grid_overlay_state.is_open


def test_close_grid_helper_noop_when_closed():
    c = _make_controller(grid_open=False)
    c._close_grid_for_numbered_overlay("tr")
    assert c._gui_events == []


# ---------------------------------------------------------------------------
# 5. Gesture threading to the numbered overlay click.
# ---------------------------------------------------------------------------


def test_overlay_pick_threads_gesture_to_dispatch():
    c = _make_controller(
        overlay_state=OverlayState.PAINTED,
        pinned_snapshot_id="snap",
        cache_summaries=(_summary("snap", 1, 2, 3),),
    )
    c._dispatch_snapshot_item_click = MagicMock()
    _run(
        c,
        c.forward_click_element(
            _query("2", gesture=ClickGesture.RIGHT_CLICK), "tr",
        ),
    )
    _, kwargs = c._dispatch_snapshot_item_click.call_args
    # == not "is": the module is importable both as ui.element_types
    # and services.wheelhouse.ui.element_types, giving two enum classes;
    # ClickGesture subclasses str so equality compares the wire value.
    assert kwargs.get("gesture") == ClickGesture.RIGHT_CLICK


def test_snapshot_item_request_carries_gesture_payload():
    from main import LogicController

    c = _make_controller()
    c._dispatch_snapshot_item_click = (
        LogicController._dispatch_snapshot_item_click.__get__(c)
    )
    c._send_snapshot_item_click = (
        LogicController._send_snapshot_item_click.__get__(c)
    )
    # wh-overlay-slow-uia-stale-badges.8: the send body lives in the inner
    # method; without binding it the spec'd mock swallows the send.
    c._send_snapshot_item_click_inner = (
        LogicController._send_snapshot_item_click_inner.__get__(c)
    )
    c._cancel_held_click_after_success = MagicMock()
    c._feed_click_complete_refresh = MagicMock()

    async def _go():
        c._dispatch_snapshot_item_click(
            snapshot_id="snap", item_id="snap-item-2", trace_id="tr",
            gesture=ClickGesture.DOUBLE_CLICK,
        )

    _run(c, _go())
    action, params, _timeout = c._captured["calls"][-1]
    assert action == "click_snapshot_item"
    assert params["gesture"] == "double_click"


def test_snapshot_item_request_omits_default_gesture():
    from main import LogicController

    c = _make_controller()
    c._dispatch_snapshot_item_click = (
        LogicController._dispatch_snapshot_item_click.__get__(c)
    )
    c._send_snapshot_item_click = (
        LogicController._send_snapshot_item_click.__get__(c)
    )
    # wh-overlay-slow-uia-stale-badges.8: see the sibling test above.
    c._send_snapshot_item_click_inner = (
        LogicController._send_snapshot_item_click_inner.__get__(c)
    )
    c._cancel_held_click_after_success = MagicMock()
    c._feed_click_complete_refresh = MagicMock()

    async def _go():
        c._dispatch_snapshot_item_click(
            snapshot_id="snap", item_id="snap-item-2", trace_id="tr",
        )

    _run(c, _go())
    action, params, _timeout = c._captured["calls"][-1]
    assert action == "click_snapshot_item"
    assert "gesture" not in params


# ---------------------------------------------------------------------------
# 6. Actions layer: dictation fallback for grid-open-only words.
# ---------------------------------------------------------------------------


def _make_actions(lc, *, with_processor: bool = True,
                  badge_click: bool = False):
    """Build ActionFunctions with a recording app and speech processor.

    wh-mouse-grid.1.27: the closed-grid dictation fallback must ride the
    speech processor's normal dictation path (_send_to_dictation) and
    flag the parse as a dictation fallback on the text parser -- so the
    fixture models both seams. ``with_processor=False`` models partial
    wiring (no speech processor), where the raw insert payload via
    app.send_command is the only remaining path.

    ``badge_click`` is what the processor's bare-number click path
    answers (wh-overlay-count-homophones.1.1): True stands for badges on
    screen, where a bare number clicks its badge and nothing types;
    False stands for badges not showing, where the words type as before.
    ``processor.badge_clicks`` records every word offered to that path,
    so a test can prove which commands offer one and which never do.

    Returns (functions, sent, dictated, processor): ``sent`` records
    app.send_command payloads; ``dictated`` records _send_to_dictation
    text; ``processor.text_parser.dictation_fallback_this_parse`` is the
    flag the fallback must set.
    """
    from speech.actions import ActionFunctions

    speech_handler = MagicMock()
    speech_handler.logic_controller = lc
    sent = []

    async def _send_command(action):
        sent.append(action)

    speech_handler.app.send_command = _send_command

    dictated: list = []

    class _FakeParser:
        def __init__(self):
            self.dictation_fallback_this_parse = False

    class _FakeProcessor:
        def __init__(self):
            self.text_parser = _FakeParser()
            self.badge_clicks: list = []

        async def _send_to_dictation(self, text):
            dictated.append(text)

        async def try_bare_number_badge_click(self, spoken):
            self.badge_clicks.append(spoken)
            return badge_click

    processor = _FakeProcessor() if with_processor else None
    speech_handler.speech_processor = processor
    functions = ActionFunctions(speech_handler)
    return functions, sent, dictated, processor


class _FakeLc:
    def __init__(self, consumed: bool):
        self.consumed = consumed
        self.calls = []

    async def handle_grid_command(self, command, trace_id, **kwargs):
        self.calls.append((command, kwargs))
        return self.consumed


def test_bare_number_grid_closed_dictates_the_words():
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_number_command("number five", "five"))
    assert lc.calls and lc.calls[0][0] == "refine"
    # wh-mouse-grid.1.27: the fallback rides the speech processor's
    # normal dictation path, never the raw insert payload, and marks
    # the parse as a dictation fallback so the match is not recorded
    # as an executed command (which would block retraction).
    assert dictated == ["number five"]
    assert sent == []
    assert processor.text_parser.dictation_fallback_this_parse is True


def test_unparseable_number_word_dictates_via_the_fallback():
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_number_command("ten", "ten"))
    assert lc.calls == []
    assert dictated == ["ten"]
    assert sent == []
    assert processor.text_parser.dictation_fallback_this_parse is True


def test_bare_number_grid_open_does_not_dictate():
    lc = _FakeLc(consumed=True)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_number_command("five", "five"))
    assert sent == []
    assert dictated == []
    assert processor.text_parser.dictation_fallback_this_parse is False
    assert lc.calls[0][1].get("number") == 5


@pytest.mark.parametrize(("spoken", "cell"), [
    pytest.param("to", 2, id="to-is-2"),
    pytest.param("too", 2, id="too-is-2"),
    pytest.param("for", 4, id="for-is-4"),
])
def test_a_homophone_grid_number_refines_that_cell(spoken, cell):
    """wh-overlay-count-homophones: the action, not the pattern.

    The two word-form grid patterns now capture "to", "too" and "for",
    so grid_number_command receives them. Without the alias the parse
    returns None and the word dictates instead of refining, which is the
    half that widening the patterns alone cannot fix.
    """
    lc = _FakeLc(consumed=True)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_number_command(spoken, spoken))
    assert dictated == []
    assert sent == []
    assert lc.calls and lc.calls[0][0] == "refine"
    assert lc.calls[0][1].get("number") == cell


@pytest.mark.parametrize("spoken", ["five", "5", "number five", "for"])
def test_a_grid_number_clicks_its_badge_when_badges_are_showing(spoken):
    """wh-overlay-count-homophones.1.1: the grid refuses, the overlay acts.

    The grid is asked first and does not consume (grid closed). Badges
    ARE showing, so the number is a click, not text: nothing dictates,
    and the spoken words go to the speech processor's bare-number click
    path -- the same path a "click N" the STT dropped the verb from
    takes. This is David's ruling of 2026-08-27 recorded on
    wh-click-number-dictation.1.2.
    """
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(
        lc, badge_click=True,
    )
    asyncio.run(functions.grid_number_command(spoken, spoken.split()[-1]))
    assert lc.calls and lc.calls[0][0] == "refine"
    assert processor.badge_clicks == [spoken]
    assert dictated == []
    assert sent == []


@pytest.mark.parametrize(("call", "spoken"), [
    pytest.param("mark", "mark", id="mark"),
    pytest.param("drag", "drag", id="drag"),
    pytest.param("move_here", "move here", id="move-here"),
])
def test_a_grid_word_that_is_not_a_number_never_offers_a_badge_click(
    call, spoken,
):
    """Only the number commands consult the numbered overlay.

    "mark", "drag" and "move here" name no badge, so with the grid
    closed they must type exactly as before, without asking the overlay
    anything. Badges are showing here on purpose: the fixture would
    click if it were asked.
    """
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(
        lc, badge_click=True,
    )
    asyncio.run(functions.grid_action_command(call, spoken))
    assert processor.badge_clicks == []
    assert dictated == [spoken]
    assert sent == []


@pytest.mark.parametrize("spoken", ["to", "too", "for"])
def test_a_homophone_with_no_grid_and_no_badges_still_types(spoken):
    """The fallback the change must not disturb.

    With no grid open AND no badges showing, the utterance dictates
    verbatim, which is what keeps "for" usable in ordinary speech. The
    fixture's badge_click is False here, standing for badges not
    showing. This catches an over-broad fix, not the bug.
    """
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_number_command(spoken, spoken))
    assert dictated == [spoken]
    assert sent == []
    # The overlay was consulted and said no; that is the only reason the
    # words typed.
    assert processor.badge_clicks == [spoken]


def test_bare_click_grid_closed_dictates():
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_click_command("click"))
    assert dictated == ["click"]
    assert sent == []
    assert processor.text_parser.dictation_fallback_this_parse is True


def test_bare_right_click_parses_gesture():
    lc = _FakeLc(consumed=True)
    functions, sent, dictated, _processor = _make_actions(lc)
    asyncio.run(functions.grid_click_command("right click"))
    command, kwargs = lc.calls[0]
    assert command == "click"
    # == not "is": the module is importable both as ui.element_types
    # and services.wheelhouse.ui.element_types, giving two enum classes;
    # ClickGesture subclasses str so equality compares the wire value.
    assert kwargs.get("gesture") == ClickGesture.RIGHT_CLICK
    assert sent == []
    assert dictated == []


def test_mark_grid_closed_dictates():
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, processor = _make_actions(lc)
    asyncio.run(functions.grid_action_command("mark", "mark."))
    assert dictated == ["mark."]
    assert sent == []
    assert processor.text_parser.dictation_fallback_this_parse is True


def test_no_logic_controller_dictates_via_the_fallback():
    functions, sent, dictated, processor = _make_actions(None)
    asyncio.run(functions.grid_action_command("mark", "mark"))
    assert dictated == ["mark"]
    assert sent == []
    assert processor.text_parser.dictation_fallback_this_parse is True


def test_fallback_without_a_processor_still_types_the_words():
    # Partial wiring (no speech processor): the raw insert payload via
    # app.send_command is the only remaining path; the words must not
    # be silently dropped.
    lc = _FakeLc(consumed=False)
    functions, sent, dictated, _processor = _make_actions(
        lc, with_processor=False,
    )
    asyncio.run(functions.grid_action_command("mark", "mark."))
    assert dictated == []
    assert len(sent) == 1
    assert sent[0]["params"]["insertion_string"] == "mark."


def test_show_grid_never_dictates():
    lc = _FakeLc(consumed=True)
    functions, sent, dictated, _processor = _make_actions(lc)
    asyncio.run(functions.grid_show_command())
    assert lc.calls[0][0] == "open"
    assert sent == []
    assert dictated == []


def test_gesture_click_command_routes_parse_command():
    class _ClickLc:
        def __init__(self):
            self.queries = []

        async def forward_click_element(self, query, trace_id):
            self.queries.append(query)

    lc = _ClickLc()
    functions, sent, _dictated, _processor = _make_actions(lc)
    asyncio.run(functions.click_element_command("right click cancel"))
    (query,) = lc.queries
    assert query.name == "cancel"
    assert query.gesture == ClickGesture.RIGHT_CLICK
    assert sent == []


def test_gesture_click_number_carries_gesture():
    class _ClickLc:
        def __init__(self):
            self.queries = []

        async def forward_click_element(self, query, trace_id):
            self.queries.append(query)

    lc = _ClickLc()
    functions, sent, _dictated, _processor = _make_actions(lc)
    asyncio.run(functions.click_element_command("double click 5"))
    (query,) = lc.queries
    assert query.name == "5"
    assert query.gesture == ClickGesture.DOUBLE_CLICK


# ---------------------------------------------------------------------------
# 7. Pattern catalog: ordering and hotword freedom.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog():
    from speech.pattern_catalog import PatternCatalog

    patterns = (
        Path(__file__).resolve().parent.parent
        / "speech" / "config" / "patterns.toml"
    )
    return PatternCatalog(str(patterns))


def _first_match(catalog, utterance: str):
    for entry in catalog.get_all_patterns():
        if entry["compiled_pattern"].match(utterance):
            return entry
    return None


def _first_function(entry) -> str:
    actions = entry.get("actions") or []
    if not actions:
        return ""
    return actions[0].get("function", "")


@pytest.mark.parametrize(
    "utterance,function",
    [
        ("right click cancel", "click_element_command"),
        ("double click 5", "click_element_command"),
        ("right-click save", "click_element_command"),
        ("show grid", "grid_show_command"),
        # "apply grid" and "dismiss grid" are aliases on the same two
        # entries, restored 2026-08-20 (wh-dismiss-alias-restore).
        ("apply grid", "grid_show_command"),
        ("hide grid", "grid_dismiss_command"),
        # "dismiss grid" is an alias on the same entry, restored 2026-08-20
        # (wh-dismiss-alias-restore).
        ("dismiss grid", "grid_dismiss_command"),
        ("grid next screen", "grid_next_screen_command"),
        ("click", "grid_click_command"),
        ("right click", "grid_click_command"),
        ("double click", "grid_click_command"),
        ("mark", "grid_action_command"),
        ("drag", "grid_action_command"),
        ("move here", "grid_action_command"),
        ("five", "grid_number_command"),
        ("number five", "grid_number_command"),
        ("5", "grid_number_command"),
        ("number 5", "grid_number_command"),
    ],
)
def test_grid_patterns_first_match(catalog, utterance, function):
    entry = _first_match(catalog, utterance)
    assert entry is not None, utterance
    assert _first_function(entry) == function
    assert entry["requires_hotword"] is False


# Explicit ids, hyphens and no spaces, so the mutation gate can name a
# single case: a generated id such as "number to" truncates at the space.
@pytest.mark.parametrize("utterance", [
    pytest.param("to", id="to"),
    pytest.param("too", id="too"),
    pytest.param("for", id="for"),
    pytest.param("number to", id="number-to"),
    pytest.param("number too", id="number-too"),
    pytest.param("number for", id="number-for"),
])
def test_the_grid_number_patterns_accept_the_stt_homophones(
    catalog, utterance,
):
    """wh-overlay-count-homophones: the shipped grid patterns, not a call.

    The engine returns "to"/"too" for the spoken "two" and "for" for
    "four". The two word-form grid patterns listed only one..nine, so a
    spoken grid number heard as its homophone matched no grid pattern at
    all and never reached grid_number_command. David's decision
    2026-09-03 puts the grid with the overlay. The digit pattern needs
    nothing: a digit is never a homophone.
    """
    entry = _first_match(catalog, utterance)
    assert entry is not None, utterance
    assert _first_function(entry) == "grid_number_command"
    assert entry["requires_hotword"] is False
    assert entry["whole_utterance_only"] is True


def test_click_to_talk_mode_still_wins_over_click_element(catalog):
    entry = _first_match(catalog, "click to talk mode")
    assert entry is not None
    assert _first_function(entry) not in (
        "click_element", "click_element_command", "grid_click_command",
    )


def test_plain_click_element_pattern_unchanged(catalog):
    entry = _first_match(catalog, "click cancel")
    assert _first_function(entry) == "click_element"


def test_bare_word_patterns_are_whole_utterance_only(catalog):
    for utterance in ("click", "mark", "drag", "move here", "five"):
        entry = _first_match(catalog, utterance)
        assert entry["whole_utterance_only"] is True, utterance


def test_explicit_grid_commands_are_whole_utterance_only(catalog):
    """Explicit grid commands fire as a WHOLE utterance and nothing less.

    This test asserted the opposite until 2026-08-26. wh-grid-speech-routing
    flagged only the bare grid words (click, mark, drag, numbers), so that
    they stayed dictatable, and this test pinned the contrast: the explicit
    two-word commands were left unflagged. No recorded reason required the
    split -- the bead's design notes cover the bare words only.

    wh-cmd-prefix-word-loss ruled that word count never protected an
    utterance. A leading command phrase of ANY length executes and the rest
    of the utterance dictates, so an unflagged "show grid" swallowed the
    tail of "show grid lines on the chart". Two words are not rare enough
    to be safe.

    What the grid section of patterns.toml actually promises -- "always
    consumed, grid open or closed" -- is unchanged and is asserted below:
    the flag governs only whether a command may fire on a PREFIX.
    """
    for utterance in (
        "show grid", "apply grid", "hide grid", "dismiss grid",
        "grid next screen",
    ):
        entry = _first_match(catalog, utterance)
        assert entry.get("whole_utterance_only") is True, utterance


def test_explicit_grid_commands_still_execute_whole(catalog):
    """The flag must not cost the grid commands their execution."""
    from speech.domain import Action
    from speech.router import SpeechRouter

    router = SpeechRouter(catalog, hotword="x-ray")
    for utterance in (
        "show grid", "apply grid", "hide grid", "dismiss grid",
        "grid next screen",
    ):
        decision = router._resolve_finalization(
            utterance.split(), hotword_active=False
        )
        assert decision.action is Action.EXECUTE, utterance
        assert decision.payload == utterance

    # ...and the leak the flag closes: the command name starts a sentence.
    decision = router._resolve_finalization(
        "show grid lines on the chart".split(), hotword_active=False
    )
    assert decision.action is Action.DICTATE
    assert decision.payload == "show grid lines on the chart"


def test_ten_is_not_a_grid_number(catalog):
    entry = _first_match(catalog, "ten")
    assert entry is None or _first_function(entry) != "grid_number_command"


# ---------------------------------------------------------------------------
# 8. Notice wording for the new grid reason tags.
# ---------------------------------------------------------------------------


def _wording(reason: str, matched_name: Optional[str] = None) -> str:
    from click_notice_toast_wording import compose_click_notice_wording
    from shared.click_notice import ClickNoticeEvent

    event = ClickNoticeEvent(
        outcome="execution_failed",
        reason=reason,
        matched_name=matched_name,
        matched_names=(),
        spoken_name="",
        app_friendly_name="",
        snapshot_id=None,
        trace_id="tr",
    )
    return compose_click_notice_wording(event)


def test_wording_drag_without_mark_mentions_mark():
    text = _wording("drag_without_mark")
    assert "mark" in text.lower()


def test_wording_grid_no_monitor():
    assert "monitor" in _wording("grid_no_monitor").lower()


def test_wording_grid_failures_are_distinct():
    click = _wording("grid_click_failed")
    move = _wording("grid_move_failed")
    drag = _wording("grid_drag_failed")
    release = _wording("grid_button_release_failed")
    assert len({click, move, drag, release}) == 4


def _quoted_recovery_command(text: str) -> str:
    # The toast tells the user to say a specific command in single quotes
    # ("say 'show grid' and ..."). Extract it so the tests below can verify
    # it is a command the pattern catalog actually registers -- a toast that
    # prescribes an unregistered utterance strands a hands-free user
    # (wh-mouse-grid.1.12).
    match = re.search(r"say '([^']+)'", text)
    assert match is not None, f"no quoted spoken command in wording: {text!r}"
    return match.group(1)


def test_wording_release_failed_warns_button_may_be_held(catalog):
    text = _wording("grid_button_release_failed").lower()
    assert "button" in text
    # The recovery must be hands-free-safe: a spoken step, not only the
    # physical mouse button (wh-mouse-grid.1.5) -- and the spoken step must
    # be a registered command (wh-mouse-grid.1.12).
    command = _quoted_recovery_command(text)
    assert _first_match(catalog, command) is not None, (
        f"recovery command {command!r} matches no registered pattern"
    )


def test_wording_button_release_failed_on_the_by_name_path(catalog):
    # The by-name / numbered-badge gesture path surfaces the same held-button
    # state under its own executor tag; it needs the same voice recovery.
    text = _wording("button_release_failed").lower()
    assert "button" in text
    command = _quoted_recovery_command(text)
    assert _first_match(catalog, command) is not None, (
        f"recovery command {command!r} matches no registered pattern"
    )


def test_wording_right_button_release_names_the_right_button(catalog):
    # wh-mouse-grid.1.19: a failed RIGHTUP leaves the RIGHT button held; a
    # plain grid click sends LEFTDOWN+LEFTUP and does NOT release it. The
    # right-button tags must prescribe a right click on the cell.
    for reason in (
        "grid_right_button_release_failed", "right_button_release_failed",
    ):
        text = _wording(reason).lower()
        assert "right" in text, reason
        assert "button" in text, reason
        assert "right click a cell" in text, reason
        command = _quoted_recovery_command(text)
        assert _first_match(catalog, command) is not None, (
            f"recovery command {command!r} matches no registered pattern"
        )


def test_wording_left_release_tags_do_not_prescribe_a_right_click():
    # The left-button variants keep prescribing a plain click; telling the
    # user to right click would open a context menu instead of releasing.
    for reason in ("grid_button_release_failed", "button_release_failed"):
        text = _wording(reason).lower()
        assert "right" not in text, reason


def test_wording_left_and_right_release_wordings_are_distinct():
    assert _wording("grid_button_release_failed") != _wording(
        "grid_right_button_release_failed"
    )
    assert _wording("button_release_failed") != _wording(
        "right_button_release_failed"
    )


def test_right_click_release_failed_names_the_right_button():
    # wh-mouse-grid.1.19 grid path: Logic knows the button it requested, so a
    # right click whose release failed must surface the right-button tag.
    from services.wheelhouse.ui.element_types import ClickGesture

    c = _make_controller(grid_open=True)
    c._mouse_response = _failed_mouse_response("release_failed")
    _run(c, c.handle_grid_command(
        "click", "tr", gesture=ClickGesture.RIGHT_CLICK,
    ))
    reasons = [n["reason"] for n in _notices(c)]
    assert "grid_right_button_release_failed" in reasons
    assert "grid_button_release_failed" not in reasons


def test_wording_gesture_not_eligible_names_control():
    text = _wording("gesture_not_eligible", matched_name="Save")
    assert "Save" in text


def test_wording_gesture_sendinput_failed_names_control():
    text = _wording("gesture_sendinput_failed", matched_name="Save")
    assert "Save" in text


# ---------------------------------------------------------------------------
# 9. Logic-side per-monitor DPI awareness for the monitor seam
#    (wh-mouse-grid.1.16). The Logic process has no Qt and no uiautomation
#    import, so a plain python.exe Logic process is DPI-UNAWARE and its
#    User32 monitor / window rectangles are virtualized (a 3840x2160
#    monitor at 300% reads as 1280x720 -- verified empirically on Ikon).
#    The seam must establish a per-monitor thread context for its reads
#    and refuse (degrade to NO_MONITOR) when it cannot.
# ---------------------------------------------------------------------------


class TestPerMonitorDpiContext:
    """The shared context manager that guards physical-pixel reads."""

    def _user32(self):
        from services.wheelhouse.shared import monitor_geometry

        user32 = MagicMock()
        patcher = mock.patch.object(monitor_geometry, "ctypes")
        mocked = patcher.start()
        mocked.windll.user32 = user32
        mocked.c_void_p = lambda v=None: v
        return user32, patcher

    def test_yields_true_and_restores_the_previous_context(self):
        from services.wheelhouse.shared.monitor_geometry import (
            per_monitor_dpi_context,
        )

        user32, patcher = self._user32()
        try:
            user32.SetThreadDpiAwarenessContext.return_value = 1234
            with per_monitor_dpi_context() as ok:
                assert ok is True
            # Restored with the context the switch returned.
            assert user32.SetThreadDpiAwarenessContext.call_args_list[-1][0] \
                == (1234,)
        finally:
            patcher.stop()

    def test_yields_false_when_switch_fails_on_an_unaware_thread(self):
        from services.wheelhouse.shared.monitor_geometry import (
            per_monitor_dpi_context,
        )

        user32, patcher = self._user32()
        try:
            user32.SetThreadDpiAwarenessContext.return_value = None
            user32.GetThreadDpiAwarenessContext.return_value = 77
            user32.GetAwarenessFromDpiAwarenessContext.return_value = 0
            with per_monitor_dpi_context() as ok:
                assert ok is False
        finally:
            patcher.stop()

    def test_yields_true_when_switch_fails_but_thread_already_aware(self):
        from services.wheelhouse.shared.monitor_geometry import (
            per_monitor_dpi_context,
        )

        user32, patcher = self._user32()
        try:
            user32.SetThreadDpiAwarenessContext.return_value = None
            user32.GetThreadDpiAwarenessContext.return_value = 77
            user32.GetAwarenessFromDpiAwarenessContext.return_value = 2
            with per_monitor_dpi_context() as ok:
                assert ok is True
        finally:
            patcher.stop()

    def test_yields_false_when_the_api_is_missing(self):
        from services.wheelhouse.shared.monitor_geometry import (
            per_monitor_dpi_context,
        )

        user32, patcher = self._user32()
        try:
            # Pre-1703 Windows: no SetThreadDpiAwarenessContext export.
            del user32.SetThreadDpiAwarenessContext
            with per_monitor_dpi_context() as ok:
                assert ok is False
        finally:
            patcher.stop()


class TestGridMonitorContextDpi:
    """_grid_monitor_context reads only inside the context, refuses without."""

    def _controller(self):
        from main import LogicController

        c = MagicMock(spec=LogicController)
        c._grid_monitor_context = (
            LogicController._grid_monitor_context.__get__(c)
        )
        return c

    def test_refuses_when_no_reliable_physical_coordinates(self):
        from contextlib import contextmanager

        from services.wheelhouse.shared import monitor_geometry

        c = self._controller()

        @contextmanager
        def _no_dpi():
            yield False

        enum = MagicMock()
        with mock.patch.object(
            monitor_geometry, "per_monitor_dpi_context", _no_dpi
        ), mock.patch.object(
            monitor_geometry, "_enumerate_native_monitors", enum
        ):
            monitors, focused = c._grid_monitor_context()

        # Fail-safe refusal: no monitors (degrades to NO_MONITOR -> notice)
        # and no virtualized rectangles ever read.
        assert monitors == () and focused is None
        enum.assert_not_called()

    def test_reads_happen_inside_the_established_context(self):
        from contextlib import contextmanager

        from services.wheelhouse.shared import monitor_geometry

        c = self._controller()
        order: list = []

        @contextmanager
        def _dpi_ok():
            order.append("enter")
            yield True
            order.append("exit")

        def _enum():
            order.append("enumerate")
            return []

        with mock.patch.object(
            monitor_geometry, "per_monitor_dpi_context", _dpi_ok
        ), mock.patch.object(
            monitor_geometry, "_enumerate_native_monitors", _enum
        ):
            monitors, focused = c._grid_monitor_context()

        assert order == ["enter", "enumerate", "exit"]
        assert monitors == () and focused is None


# ---------------------------------------------------------------------------
# 10. Grid mouse IPC serialization (wh-mouse-grid.1.15). perform_drag
#     blocks the synchronous Input command loop for its whole interpolated
#     duration (up to 5s). A second grid pointer action sent during that
#     window could time out at Logic (its budget starts at enqueue), be
#     reported failed, and still execute later -- or overwrite an unread
#     shared-memory payload and be silently dropped. Admission is
#     serialized: a grid pointer action is not SENT until the previous
#     one's reply has arrived, so each send_request's timeout starts at
#     its own admission and at most one grid payload is ever outstanding.
# ---------------------------------------------------------------------------


class TestGridMouseActionSerialization:
    def test_second_action_is_not_sent_while_a_drag_is_in_flight(self):
        c = _make_controller()
        calls: list = []

        async def _scenario():
            gate = asyncio.Event()

            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                calls.append(action)
                if action == "perform_drag":
                    await gate.wait()
                from shared.mouse_action import MouseActionResponse
                return MouseActionResponse(
                    status="ok", outcome="ok", reason=None, trace_id="t",
                ).to_dict()

            c.app.send_request = _send_request
            loop = asyncio.get_event_loop()
            drag = loop.create_task(
                c._send_mouse_action(
                    "perform_drag",
                    {"from_x": 0, "from_y": 0, "x": 9, "y": 9,
                     "duration_ms": 5000},
                    trace_id="t1", spoken="drag",
                    failure_reason="grid_drag_failed",
                )
            )
            # Let the drag reach its (held) send.
            for _ in range(5):
                await asyncio.sleep(0)
            click = loop.create_task(
                c._send_mouse_action(
                    "click_point",
                    {"x": 5, "y": 5, "button": "left", "click_count": 1},
                    trace_id="t2", spoken="click",
                    failure_reason="grid_click_failed",
                )
            )
            for _ in range(5):
                await asyncio.sleep(0)
            # The click has NOT been sent while the drag is in flight --
            # its timeout budget has not started and its payload cannot
            # overwrite the drag's.
            assert calls == ["perform_drag"]
            gate.set()
            await asyncio.gather(drag, click)

        asyncio.run(_scenario())
        assert calls == ["perform_drag", "click_point"]
        assert _notices(c) == []

    def test_lock_releases_after_a_timeout_and_the_next_action_sends(self):
        c = _make_controller()
        calls: list = []

        async def _scenario():
            async def _send_request(action, params=None, timeout_s=None,
                                    on_late_response=None):
                calls.append(action)
                if action == "perform_drag":
                    raise asyncio.TimeoutError()
                if action == "channel_probe":
                    return _probe_reply(params)
                from shared.mouse_action import MouseActionResponse
                return MouseActionResponse(
                    status="ok", outcome="ok", reason=None, trace_id="t",
                ).to_dict()

            c.app.send_request = _send_request
            await c._send_mouse_action(
                "perform_drag",
                {"from_x": 0, "from_y": 0, "x": 9, "y": 9,
                 "duration_ms": 100},
                trace_id="t1", spoken="drag",
                failure_reason="grid_drag_failed",
            )
            await c._send_mouse_action(
                "click_point",
                {"x": 5, "y": 5, "button": "left", "click_count": 1},
                trace_id="t2", spoken="click",
                failure_reason="grid_click_failed",
            )

        asyncio.run(_scenario())
        # Both were attempted (the timeout released the serialization
        # lock). The timeout marked the channel suspect, so the click was
        # admitted only after a channel_probe reply confirmed Input had
        # drained past the timed-out payload (wh-mouse-grid.1.18). Only
        # the drag noticed.
        assert calls == ["perform_drag", "channel_probe", "click_point"]
        notices = _notices(c)
        assert len(notices) == 1
        assert notices[0]["reason"] == "grid_drag_failed"


# ---------------------------------------------------------------------------
# 11. A timed-out grid pointer request may still execute later
#     (wh-mouse-grid.1.18): after a timeout the channel is suspect, and the
#     next action is admitted only once a probe round trip proves Input has
#     drained everything queued before it.
# ---------------------------------------------------------------------------


class TestGridMouseChannelProbe:
    def _drag_then_click(self, c, calls):
        async def _scenario():
            await c._send_mouse_action(
                "perform_drag",
                {"from_x": 0, "from_y": 0, "x": 9, "y": 9,
                 "duration_ms": 100},
                trace_id="t1", spoken="drag",
                failure_reason="grid_drag_failed",
            )
            await c._send_mouse_action(
                "click_point",
                {"x": 5, "y": 5, "button": "left", "click_count": 1},
                trace_id="t2", spoken="click",
                failure_reason="grid_click_failed",
            )

        asyncio.run(_scenario())

    def test_action_withheld_and_noticed_when_the_probe_gets_no_reply(self):
        c = _make_controller()
        calls: list = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            calls.append(action)
            # The drag times out, and so does the probe: Input is still
            # blocked (e.g. inside the interpolated drag).
            raise asyncio.TimeoutError()

        c.app.send_request = _send_request
        self._drag_then_click(c, calls)
        # Two probes: the immediate post-timeout recovery probe
        # (wh-mouse-grid.1.24) times out, so the channel stays suspect; the
        # click's admission probe then also times out and the click itself
        # is never sent.
        assert calls == ["perform_drag", "channel_probe", "channel_probe"]
        notices = _notices(c)
        assert [n["reason"] for n in notices] == [
            "grid_drag_failed", "grid_click_failed",
        ]

    def test_probe_runs_once_then_the_channel_is_trusted_again(self):
        c = _make_controller()
        calls: list = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            calls.append(action)
            if action == "perform_drag" and calls.count("perform_drag") == 1:
                raise asyncio.TimeoutError()
            if action == "channel_probe":
                return _probe_reply(params)
            from shared.mouse_action import MouseActionResponse
            return MouseActionResponse(
                status="ok", outcome="ok", reason=None, trace_id="t",
            ).to_dict()

        c.app.send_request = _send_request

        async def _scenario():
            await c._send_mouse_action(
                "perform_drag",
                {"from_x": 0, "from_y": 0, "x": 9, "y": 9,
                 "duration_ms": 100},
                trace_id="t1", spoken="drag",
                failure_reason="grid_drag_failed",
            )
            for n, trace in ((1, "t2"), (2, "t3")):
                await c._send_mouse_action(
                    "click_point",
                    {"x": n, "y": n, "button": "left", "click_count": 1},
                    trace_id=trace, spoken="click",
                    failure_reason="grid_click_failed",
                )

        asyncio.run(_scenario())
        # Exactly one probe: the immediate post-timeout recovery probe
        # (wh-mouse-grid.1.24) confirms the channel, so BOTH clicks are
        # admitted without another probe.
        assert calls == [
            "perform_drag", "channel_probe", "click_point", "click_point",
        ]

    def test_non_timeout_send_failure_also_marks_the_channel_suspect(self):
        c = _make_controller()
        calls: list = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            calls.append(action)
            if action == "perform_drag":
                raise RuntimeError("queue broken")
            if action == "channel_probe":
                return _probe_reply(params)
            from shared.mouse_action import MouseActionResponse
            return MouseActionResponse(
                status="ok", outcome="ok", reason=None, trace_id="t",
            ).to_dict()

        c.app.send_request = _send_request
        self._drag_then_click(c, calls)
        assert calls == ["perform_drag", "channel_probe", "click_point"]


# ---------------------------------------------------------------------------
# 12. Grid paint must not overtake the numbered-overlay clear
#     (wh-mouse-grid.1.17): 'show grid' closes the numbered overlay via a
#     SCHEDULED dispatch batch behind _overlay_effect_lock; paint_grid may
#     ship only after that clear batch has gone out.
# ---------------------------------------------------------------------------


class TestGridPaintWaitsForOverlayClearDispatch:
    def test_paint_grid_not_queued_until_the_overlay_clear_batch_ships(self):
        from main import LogicController

        c = _make_controller(overlay_state=OverlayState.PAINTED)
        # Bind the REAL overlay effect performers: the bug lives in the
        # scheduling seam between them, so faking handle_overlay_command
        # (as the open test does) cannot see it.
        for name in ("_perform_overlay_effects", "_dispatch_overlay_effects"):
            setattr(c, name, getattr(LogicController, name).__get__(c))

        order: list = []
        gui_events = c._gui_events

        def _put(event):
            order.append(event.get("action"))
            gui_events.append(event)

        c.state_manager.state_to_gui_queue.put_nowait = _put

        async def _clear_one(effect, trace_id):
            order.append("clear_overlay")

        c._overlay_dispatch_clear_one = _clear_one

        async def _scenario():
            # Simulate an in-flight overlay refresh batch holding the
            # effect lock when 'show grid' arrives.
            lock = asyncio.Lock()
            c._overlay_effect_lock = lock
            await lock.acquire()
            open_task = asyncio.get_event_loop().create_task(
                c.handle_grid_command("open", "tr")
            )
            for _ in range(20):
                await asyncio.sleep(0)
            assert not _gui(c, "paint_grid"), (
                "paint_grid shipped while the numbered-overlay clear batch "
                "was still waiting on _overlay_effect_lock"
            )
            lock.release()
            await open_task
            while c._tasks:
                pending, c._tasks = c._tasks, []
                await asyncio.gather(*pending)

        asyncio.run(_scenario())
        assert "clear_overlay" in order and "paint_grid" in order
        assert order.index("clear_overlay") < order.index("paint_grid")


# ---------------------------------------------------------------------------
# 13. Grid open must not wait out a stale overlay build's deadline
#     (wh-mouse-grid.1.20): a numbered-overlay refresh build holds
#     _overlay_effect_lock across its Input round trip (up to
#     response_timeout_ms). 'show grid' supersedes that build; the hide must
#     wake it so the clear/paint sequence ships promptly instead of waiting
#     for the stale request to return or time out.
# ---------------------------------------------------------------------------


class TestGridOpenNotBlockedByStaleOverlayBuild:
    def test_grid_open_ships_promptly_while_a_real_build_holds_the_lock(self):
        from main import LogicController
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        c = _make_controller(overlay_state=OverlayState.PAINTED)
        for name in (
            "_perform_overlay_effects",
            "_dispatch_overlay_effects",
            "_overlay_dispatch_build",
            "_abort_inflight_overlay_build",
        ):
            setattr(c, name, getattr(LogicController, name).__get__(c))

        order: list = []
        gui_events = c._gui_events

        def _put(event):
            order.append(event.get("action"))
            gui_events.append(event)

        c.state_manager.state_to_gui_queue.put_nowait = _put

        async def _clear_one(effect, trace_id):
            order.append("clear_overlay")

        c._overlay_dispatch_clear_one = _clear_one

        calls: list = []

        async def _scenario():
            c.loop = asyncio.get_running_loop()
            gate = asyncio.Event()

            async def _send(action, params=None, timeout_s=None):
                calls.append(action)
                if action == "start_overlay_walk":
                    # A slow Input walk: never answers until the gate opens.
                    await gate.wait()
                    return {}
                from shared.mouse_action import MouseActionResponse
                return MouseActionResponse(
                    status="ok", outcome="ok", reason=None, trace_id="t",
                ).to_dict()

            c.app.send_request = _send
            # A REAL refresh build batch: machine painted -> refresh in
            # flight, prior badges deliberately left visible, DISPATCH_BUILD
            # acquires the effect lock and blocks awaiting Input.
            result = c.click_overlay_state.apply(
                OverlayEvent(OverlayEventKind.SHOW_NUMBERS)
            )
            loop = asyncio.get_event_loop()
            build = loop.create_task(
                c._dispatch_overlay_effects(
                    tuple(result.effects), trace_id="tr0",
                )
            )
            for _ in range(10):
                await asyncio.sleep(0)
            assert "start_overlay_walk" in calls, (
                "fixture failed to put a real build on the effect lock"
            )
            open_task = loop.create_task(c.handle_grid_command("open", "tr"))
            # Only cooperative yields -- the gate is never opened before the
            # assertion, so a grid open that waits for the stale build's
            # response (or its timeout) cannot pass this.
            for _ in range(50):
                await asyncio.sleep(0)
            assert _gui(c, "paint_grid"), (
                "grid open stalled behind the stale overlay build's deadline"
            )
            assert "clear_overlay" in order
            assert order.index("clear_overlay") < order.index("paint_grid")
            gate.set()
            await open_task
            await build
            while c._tasks:
                pending, c._tasks = c._tasks, []
                await asyncio.gather(*pending)

        asyncio.run(_scenario())

    def test_hide_before_the_build_batch_starts_skips_the_stale_send(self):
        # The abort event only wakes a build already awaiting Input. A build
        # batch still QUEUED behind the effect lock when the hide commits
        # must notice the teardown at start (machine CLOSED) and skip its
        # send entirely -- otherwise it holds the lock for the full
        # response_timeout_ms on a session that no longer exists.
        from main import LogicController
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        c = _make_controller(overlay_state=OverlayState.PAINTED)
        for name in (
            "_perform_overlay_effects",
            "_dispatch_overlay_effects",
            "_overlay_dispatch_build",
            "_abort_inflight_overlay_build",
        ):
            setattr(c, name, getattr(LogicController, name).__get__(c))

        order: list = []

        async def _clear_one(effect, trace_id):
            order.append("clear_overlay")

        c._overlay_dispatch_clear_one = _clear_one
        calls: list = []

        async def _scenario():
            c.loop = asyncio.get_running_loop()
            gate = asyncio.Event()

            async def _send(action, params=None, timeout_s=None):
                calls.append(action)
                if action == "start_overlay_walk":
                    await gate.wait()
                return {}

            c.app.send_request = _send
            lock = asyncio.Lock()
            c._overlay_effect_lock = lock
            await lock.acquire()
            result = c.click_overlay_state.apply(
                OverlayEvent(OverlayEventKind.SHOW_NUMBERS)
            )
            build = asyncio.get_event_loop().create_task(
                c._dispatch_overlay_effects(
                    tuple(result.effects), trace_id="tr0",
                )
            )
            for _ in range(5):
                await asyncio.sleep(0)
            # The hide commits while the build batch is still queued.
            await c.handle_overlay_command("hide", "tr-hide")
            lock.release()
            for _ in range(50):
                await asyncio.sleep(0)
            assert "start_overlay_walk" not in calls, (
                "a stale build sent its walk request after the session "
                "was already hidden"
            )
            assert "clear_overlay" in order
            gate.set()
            await build
            while c._tasks:
                pending, c._tasks = c._tasks, []
                await asyncio.gather(*pending)

        asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# 14. Invalid grid config refuses the grid (wh-mouse-grid.1.21) and the
#     awaiter rejects an impossible success reply (wh-mouse-grid.1.22).
# ---------------------------------------------------------------------------


def _config_with_invalid_grid_key():
    from ui.click_config import ClickConfig

    cfg = ClickConfig.from_raw(
        {"grid_min_cell_px": 0, "response_timeout_ms": 3000}
    )
    assert cfg.enabled is True
    return cfg


class TestInvalidGridConfigRefusesTheGrid:
    def test_open_refused_when_a_grid_key_is_invalid(self):
        # grid_min_cell_px=0 fails validation on the overlay track: enabled
        # stays True (by-name click keeps working) but the grid must refuse
        # to open rather than silently running on the restored default.
        c = _make_controller()
        c.click_config = _config_with_invalid_grid_key()

        assert _run(c, c.handle_grid_command("open", "tr")) is True
        assert not c.grid_overlay_state.is_open
        assert _gui(c, "paint_grid") == []
        (notice,) = _notices(c)
        assert notice["reason"] == "disabled_by_config"
        # One-shot: the second attempt is consumed without a second notice.
        assert _run(c, c.handle_grid_command("open", "tr2")) is True
        assert len(_notices(c)) == 1

    def test_open_allowed_when_the_overlay_is_opted_out(self):
        # overlay_enabled=false is a valid numbered-overlay opt-out. The gate
        # must NOT be overlay_enabled_effective wholesale: the grid stays
        # governed by the master switch plus the GRID keys alone.
        from ui.click_config import ClickConfig

        c = _make_controller()
        c.click_config = ClickConfig.from_raw(
            {"overlay_enabled": False, "response_timeout_ms": 3000}
        )

        assert _run(c, c.handle_grid_command("open", "tr")) is True
        assert c.grid_overlay_state.is_open
        assert len(_gui(c, "paint_grid")) == 1

    def test_grid_only_words_not_consumed_when_grid_config_invalid(self):
        c = _make_controller()
        c.click_config = _config_with_invalid_grid_key()

        assert _run(c, c.handle_grid_command("click", "tr")) is False
        assert _notices(c) == []


class TestImpossibleSuccessReplyRejected:
    def test_error_status_with_ok_outcome_is_not_treated_as_success(self):
        # wh-mouse-grid.1.22: the Input handler can only emit status=ok with
        # outcome=ok, or status=error with outcome=execution_failed and a
        # reason. A corrupted hybrid (status=error, outcome=ok) used to parse
        # and, because the awaiter decides success from outcome alone, was
        # silently treated as a completed click -- with reason=release_failed
        # discarded and no stuck-button notice. The schema now rejects it and
        # the awaiter rides the malformed-reply path.
        c = _make_controller(grid_open=True)
        c._mouse_response = {
            "status": "error",
            "outcome": "ok",
            "reason": "release_failed",
            "trace_id": "t",
        }

        _run(c, c.handle_grid_command("click", "tr"))

        (notice,) = _notices(c)
        assert notice["reason"] == "grid_click_failed"


# ---------------------------------------------------------------------------
# 15. Late-reply recovery through the post-timeout probe (wh-mouse-grid.1.24).
#     A timed-out pointer action's reply is discarded by the app demuxer, so
#     the probe reply carries Input's record of the last emitted result;
#     Logic correlates it by trace_id and recovers the stuck-button warning
#     (or suppresses a false failure notice when the action landed late).
# ---------------------------------------------------------------------------


class TestLateReplyRecovery:
    def _controller_with_probe_reply(self, last_mouse_action):
        c = _make_controller(grid_open=True)
        calls: list = []

        async def _send_request(action, params=None, timeout_s=None,
                                on_late_response=None):
            calls.append(action)
            if action == "channel_probe":
                return _probe_reply(
                    params, last_mouse_action=last_mouse_action,
                )
            raise asyncio.TimeoutError()

        c.app.send_request = _send_request
        c._probe_calls = calls
        return c

    def _timed_out_click(self, c, *, button: str, trace_id: str):
        async def _scenario():
            await c._send_mouse_action(
                "click_point",
                {"x": 5, "y": 5, "button": button, "click_count": 1},
                trace_id=trace_id, spoken="click",
                failure_reason="grid_click_failed",
            )

        asyncio.run(_scenario())

    def test_late_right_release_failed_recovers_the_right_warning(self):
        c = self._controller_with_probe_reply({
            "action": "click_point",
            "succeeded": False,
            "reason": "release_failed",
            "trace_id": "t-late",
        })

        self._timed_out_click(c, button="right", trace_id="t-late")

        reasons = [n["reason"] for n in _notices(c)]
        assert reasons == ["grid_right_button_release_failed"]

    def test_late_left_release_failed_recovers_the_left_warning(self):
        c = self._controller_with_probe_reply({
            "action": "click_point",
            "succeeded": False,
            "reason": "release_failed",
            "trace_id": "t-late",
        })

        self._timed_out_click(c, button="left", trace_id="t-late")

        reasons = [n["reason"] for n in _notices(c)]
        assert reasons == ["grid_button_release_failed"]

    def test_late_result_for_a_different_request_is_ignored(self):
        # The record may describe an OLDER action; only a trace_id match may
        # change this request's notice.
        c = self._controller_with_probe_reply({
            "action": "click_point",
            "succeeded": False,
            "reason": "release_failed",
            "trace_id": "t-some-earlier-request",
        })

        self._timed_out_click(c, button="right", trace_id="t-late")

        reasons = [n["reason"] for n in _notices(c)]
        assert reasons == ["grid_click_failed"]

    def test_late_success_suppresses_the_false_failure_notice(self):
        # The reply proves the click executed (late); telling the user it
        # failed would be wrong, and there is no button to recover.
        c = self._controller_with_probe_reply({
            "action": "click_point",
            "succeeded": True,
            "reason": None,
            "trace_id": "t-late",
        })

        self._timed_out_click(c, button="left", trace_id="t-late")

        assert _notices(c) == []

    def test_probe_reply_without_a_record_keeps_the_generic_notice(self):
        c = self._controller_with_probe_reply(None)

        self._timed_out_click(c, button="left", trace_id="t-late")

        reasons = [n["reason"] for n in _notices(c)]
        assert reasons == ["grid_click_failed"]

    def test_late_failure_with_another_reason_keeps_the_generic_notice(self):
        c = self._controller_with_probe_reply({
            "action": "click_point",
            "succeeded": False,
            "reason": "sendinput_error",
            "trace_id": "t-late",
        })

        self._timed_out_click(c, button="right", trace_id="t-late")

        reasons = [n["reason"] for n in _notices(c)]
        assert reasons == ["grid_click_failed"]
