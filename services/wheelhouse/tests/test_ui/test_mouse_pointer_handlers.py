"""Input-side tests for the three mouse-grid pointer handlers
(wh-input-mouse-primitives).

``click_point``, ``move_pointer`` and ``perform_drag`` are the only places the
Input process touches the mouse for the grid overlay
(``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``). Each is
in ``_HANDLES_OWN_RESPONSE``, so each must emit exactly one
``MouseActionResponse`` per ``request_id`` on every path -- success, a refusal
from the SendInput primitive, the disabled-by-config short-circuit, and an
unexpected internal error -- or the Logic awaiter falls through to its timeout.

The SendInput boundary is injected as a seam on the handler
(``_mouse_click_seam`` / ``_mouse_move_seam`` / ``_mouse_drag_seam``) so these
tests never synthesise real input.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.mouse_action import MouseActionResponse

_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """Build a UIActionHandler with its specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        yield UIActionHandler(
            response_queue=MagicMock(), config={"ui_actions": {}},
        )


def _disable_clicking(handler) -> None:
    from ui.click_config import ClickConfig

    handler._click_config = ClickConfig.from_raw({"enabled": False})


def _invalidate_grid_config(handler) -> None:
    """A bad GRID key with the master switch still on (wh-mouse-grid.1.21).

    grid_min_cell_px=0 fails validation on the overlay track, so enabled
    stays True while grid_enabled_effective goes False. The pointer handlers
    must refuse on this shape too -- gating on `enabled` alone let the grid's
    physical pointer actions run under a config that documents them as
    disabled.
    """
    from ui.click_config import ClickConfig

    cfg = ClickConfig.from_raw({"grid_min_cell_px": 0})
    assert cfg.enabled is True
    handler._click_config = cfg


def _one_response(handler, *, action: str, request_id: str):
    """Assert exactly one response was enqueued, and parse it."""
    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == action
    assert payload["request_id"] == request_id
    return MouseActionResponse.from_dict(payload)


# ---------------------------------------------------------------------------
# click_point
# ---------------------------------------------------------------------------


def test_click_point_calls_the_seam_and_acks_ok(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_click_seam = seam

    handler.click_point(
        x=640, y=480, button="right", click_count=2,
        trace_id="trace-1", request_id="req-1",
    )

    seam.assert_called_once_with(640, 480, "right", 2)
    response = _one_response(handler, action="click_point", request_id="req-1")
    assert response.status == "ok"
    assert response.outcome == "ok"
    assert response.reason is None
    assert response.trace_id == "trace-1"


def test_click_point_defaults_to_a_single_left_click(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_click_seam = seam

    handler.click_point(x=10, y=20, request_id="req-2")

    seam.assert_called_once_with(10, 20, "left", 1)


def test_click_point_reports_the_seam_refusal(handler):
    handler._mouse_click_seam = MagicMock(
        return_value=(False, "cursor_did_not_land")
    )

    handler.click_point(x=1, y=2, trace_id="t", request_id="req-3")

    response = _one_response(handler, action="click_point", request_id="req-3")
    assert response.status == "error"
    assert response.outcome == "execution_failed"
    assert response.reason == "cursor_did_not_land"


def test_click_point_short_circuits_when_clicking_is_disabled(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_click_seam = seam
    _disable_clicking(handler)

    handler.click_point(x=1, y=2, request_id="req-4")

    seam.assert_not_called()
    response = _one_response(handler, action="click_point", request_id="req-4")
    assert response.outcome == "execution_failed"
    assert response.reason == "disabled_by_config"


def test_click_point_maps_an_unexpected_error_to_one_response(handler):
    handler._mouse_click_seam = MagicMock(side_effect=RuntimeError("boom"))

    handler.click_point(x=1, y=2, request_id="req-5")

    response = _one_response(handler, action="click_point", request_id="req-5")
    assert response.status == "error"
    assert response.outcome == "execution_failed"
    assert response.reason == "unexpected_error"


def test_click_point_survives_a_dead_response_queue(handler):
    handler._mouse_click_seam = MagicMock(return_value=(True, None))
    handler.response_queue.put.side_effect = OSError("queue closed")

    handler.click_point(x=1, y=2, request_id="req-6")

    assert handler.response_queue.put.call_count == 1


def test_click_point_without_a_request_id_omits_the_key(handler):
    handler._mouse_click_seam = MagicMock(return_value=(True, None))

    handler.click_point(x=1, y=2)

    payload = handler.response_queue.put.call_args[0][0]
    assert "request_id" not in payload


# ---------------------------------------------------------------------------
# move_pointer
# ---------------------------------------------------------------------------


def test_move_pointer_calls_the_seam_and_acks_ok(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_move_seam = seam

    handler.move_pointer(x=300, y=400, trace_id="t-move", request_id="req-7")

    seam.assert_called_once_with(300, 400)
    response = _one_response(
        handler, action="move_pointer", request_id="req-7",
    )
    assert response.status == "ok"
    assert response.outcome == "ok"
    assert response.trace_id == "t-move"


def test_move_pointer_reports_the_seam_refusal(handler):
    handler._mouse_move_seam = MagicMock(return_value=(False, "invalid_point"))

    handler.move_pointer(x=None, y=400, request_id="req-8")

    response = _one_response(
        handler, action="move_pointer", request_id="req-8",
    )
    assert response.outcome == "execution_failed"
    assert response.reason == "invalid_point"


def test_move_pointer_short_circuits_when_clicking_is_disabled(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_move_seam = seam
    _disable_clicking(handler)

    handler.move_pointer(x=1, y=2, request_id="req-9")

    seam.assert_not_called()
    response = _one_response(
        handler, action="move_pointer", request_id="req-9",
    )
    assert response.reason == "disabled_by_config"


def test_move_pointer_maps_an_unexpected_error_to_one_response(handler):
    handler._mouse_move_seam = MagicMock(side_effect=RuntimeError("boom"))

    handler.move_pointer(x=1, y=2, request_id="req-10")

    response = _one_response(
        handler, action="move_pointer", request_id="req-10",
    )
    assert response.reason == "unexpected_error"


# ---------------------------------------------------------------------------
# perform_drag
# ---------------------------------------------------------------------------


def test_perform_drag_calls_the_seam_and_acks_ok(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_drag_seam = seam

    handler.perform_drag(
        start_x=10, start_y=20, end_x=300, end_y=400, duration_ms=180,
        trace_id="t-drag", request_id="req-11",
    )

    seam.assert_called_once_with(10, 20, 300, 400, 180)
    response = _one_response(
        handler, action="perform_drag", request_id="req-11",
    )
    assert response.status == "ok"
    assert response.outcome == "ok"
    assert response.trace_id == "t-drag"


def test_perform_drag_defaults_the_duration(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_drag_seam = seam

    handler.perform_drag(
        start_x=0, start_y=0, end_x=1, end_y=1, request_id="req-12",
    )

    assert seam.call_args[0][4] == 250


def test_perform_drag_surfaces_a_failed_release(handler):
    # release_failed is the one outcome that means a button may still be held.
    # It must reach Logic verbatim so the notice can say so.
    handler._mouse_drag_seam = MagicMock(
        return_value=(False, "release_failed")
    )

    handler.perform_drag(
        start_x=0, start_y=0, end_x=1, end_y=1, request_id="req-13",
    )

    response = _one_response(
        handler, action="perform_drag", request_id="req-13",
    )
    assert response.status == "error"
    assert response.outcome == "execution_failed"
    assert response.reason == "release_failed"


def test_perform_drag_short_circuits_when_clicking_is_disabled(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_drag_seam = seam
    _disable_clicking(handler)

    handler.perform_drag(
        start_x=0, start_y=0, end_x=1, end_y=1, request_id="req-14",
    )

    seam.assert_not_called()
    response = _one_response(
        handler, action="perform_drag", request_id="req-14",
    )
    assert response.reason == "disabled_by_config"


def test_perform_drag_maps_an_unexpected_error_to_one_response(handler):
    handler._mouse_drag_seam = MagicMock(side_effect=RuntimeError("boom"))

    handler.perform_drag(
        start_x=0, start_y=0, end_x=1, end_y=1, request_id="req-15",
    )

    response = _one_response(
        handler, action="perform_drag", request_id="req-15",
    )
    assert response.reason == "unexpected_error"


# ---------------------------------------------------------------------------
# Command-loop registration
# ---------------------------------------------------------------------------


def test_the_three_actions_own_their_responses():
    # Without this the generic emitter in the input_proc command loop would
    # put a second, synthetic "heuristic_done" reply on the queue for the same
    # request_id (wh-lla5d).
    from input_proc import _HANDLES_OWN_RESPONSE

    assert {"click_point", "move_pointer", "perform_drag"} <= (
        _HANDLES_OWN_RESPONSE
    )


def test_the_handlers_exist_under_their_action_names(handler):
    # input_proc dispatches with getattr(ui_handler, action), so the method
    # names ARE the command names Logic sends.
    for action in ("click_point", "move_pointer", "perform_drag"):
        assert callable(getattr(handler, action))


def test_the_production_seams_are_wired_by_default(handler):
    from ui import ui_action_handler as mod

    assert handler._mouse_click_seam is mod._win32_click_point
    assert handler._mouse_move_seam is mod._win32_move_pointer
    assert handler._mouse_drag_seam is mod._win32_drag_pointer


# ---------------------------------------------------------------------------
# Invalid-grid-config refusal (wh-mouse-grid.1.21)
# ---------------------------------------------------------------------------


def test_click_point_short_circuits_when_the_grid_config_is_invalid(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_click_seam = seam
    _invalidate_grid_config(handler)

    handler.click_point(x=1, y=2, request_id="req-g1")

    seam.assert_not_called()
    response = _one_response(
        handler, action="click_point", request_id="req-g1",
    )
    assert response.outcome == "execution_failed"
    assert response.reason == "disabled_by_config"


def test_move_pointer_short_circuits_when_the_grid_config_is_invalid(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_move_seam = seam
    _invalidate_grid_config(handler)

    handler.move_pointer(x=1, y=2, request_id="req-g2")

    seam.assert_not_called()
    response = _one_response(
        handler, action="move_pointer", request_id="req-g2",
    )
    assert response.outcome == "execution_failed"
    assert response.reason == "disabled_by_config"


def test_perform_drag_short_circuits_when_the_grid_config_is_invalid(handler):
    seam = MagicMock(return_value=(True, None))
    handler._mouse_drag_seam = seam
    _invalidate_grid_config(handler)

    handler.perform_drag(
        start_x=1, start_y=2, end_x=3, end_y=4, request_id="req-g3",
    )

    seam.assert_not_called()
    response = _one_response(
        handler, action="perform_drag", request_id="req-g3",
    )
    assert response.outcome == "execution_failed"
    assert response.reason == "disabled_by_config"


# ---------------------------------------------------------------------------
# Last-result record and the channel_probe reply (wh-mouse-grid.1.24)
# ---------------------------------------------------------------------------


def test_pointer_handlers_record_the_last_result(handler):
    handler._mouse_click_seam = MagicMock(
        return_value=(False, "release_failed")
    )

    handler.click_point(
        x=1, y=2, button="right", trace_id="t-rec", request_id="req-r1",
    )

    assert handler._last_mouse_action_result == {
        "action": "click_point",
        "succeeded": False,
        "reason": "release_failed",
        "trace_id": "t-rec",
    }


def test_the_record_survives_a_dead_response_queue(handler):
    # The record exists precisely for the case where Logic never received
    # the reply; a failed enqueue must not skip it.
    handler._mouse_drag_seam = MagicMock(return_value=(False, "release_failed"))
    handler.response_queue.put.side_effect = OSError("queue closed")

    handler.perform_drag(
        start_x=1, start_y=2, end_x=3, end_y=4,
        trace_id="t-dead", request_id="req-r2",
    )

    assert handler._last_mouse_action_result["reason"] == "release_failed"
    assert handler._last_mouse_action_result["trace_id"] == "t-dead"


def test_channel_probe_reply_carries_the_last_result(handler):
    handler._mouse_click_seam = MagicMock(
        return_value=(False, "release_failed")
    )
    handler.click_point(
        x=1, y=2, button="right", trace_id="t-late", request_id="req-a",
    )
    handler.response_queue.put.reset_mock()

    handler.channel_probe(trace_id="t-probe", request_id="req-p")

    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["action"] == "channel_probe"
    assert payload["request_id"] == "req-p"
    assert payload["status"] == "ok"
    assert payload["last_mouse_action"] == {
        "action": "click_point",
        "succeeded": False,
        "reason": "release_failed",
        "trace_id": "t-late",
    }


def test_channel_probe_reply_without_a_prior_action_carries_none(handler):
    handler.channel_probe(trace_id="t-probe", request_id="req-p2")

    assert handler.response_queue.put.call_count == 1
    payload = handler.response_queue.put.call_args[0][0]
    assert payload["status"] == "ok"
    assert payload["last_mouse_action"] is None


def test_channel_probe_survives_a_dead_response_queue(handler):
    handler.response_queue.put.side_effect = OSError("queue closed")

    handler.channel_probe(trace_id="t-probe", request_id="req-p3")

    assert handler.response_queue.put.call_count == 1


def test_channel_probe_owns_its_response():
    from input_proc import _HANDLES_OWN_RESPONSE

    assert "channel_probe" in _HANDLES_OWN_RESPONSE
