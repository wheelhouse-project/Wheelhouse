"""Input-side tests of the pointer handlers that the boundary tests do not cover
(wh-input-mouse-primitives).

The behavior of ``click_point``, ``click_at_pointer``, ``move_pointer`` and
``perform_drag`` (the reply, the seams, the config gates and the last-result
record) is covered by the boundary tests in
``tests/test_mouse_grid_requirements_u7.py`` (rows MP-1 to MP-31 of
``docs/design/mouse-grid-requirements.md``). This file keeps the command-loop
registration checks and the ``channel_probe`` cases without a prior action
(wh-full-refactor.6.3).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# click_at_pointer (wh-voice-access-parity.2.5)
# ---------------------------------------------------------------------------


def test_click_at_pointer_owns_its_response_and_is_wired():
    from input_proc import _HANDLES_OWN_RESPONSE
    from ui import ui_action_handler as mod

    assert "click_at_pointer" in _HANDLES_OWN_RESPONSE
    assert callable(getattr(mod.UIActionHandler, "click_at_pointer"))
    assert callable(mod._win32_pointer_position)


def test_the_pointer_seam_reads_getcursorpos_and_fails_soft():
    from ui import ui_action_handler as mod

    with patch(f"{_MOD}.win32gui.GetCursorPos", return_value=(-3, 7)):
        assert mod._win32_pointer_position() == (-3, 7)
    with patch(
        f"{_MOD}.win32gui.GetCursorPos", side_effect=OSError("denied"),
    ):
        assert mod._win32_pointer_position() is None


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


# ---------------------------------------------------------------------------
# Last-result record and the channel_probe reply (wh-mouse-grid.1.24)
# ---------------------------------------------------------------------------


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
