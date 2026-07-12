"""Tests for input_proc._HANDLES_OWN_RESPONSE (wh-lla5d).

Actions listed in ``_HANDLES_OWN_RESPONSE`` must have their handler emit
exactly one Schema A response. The generic response emitter in the main
loop must skip those actions so a single request_id never produces two
responses.
"""
import inspect
from unittest.mock import MagicMock

from input_proc import _HANDLES_OWN_RESPONSE
from ui.ui_action_handler import UIActionHandler


def test_intelligent_insert_text_owns_its_response():
    # intelligent_insert_text now owns its own Schema A response emission,
    # so the generic emitter in the main loop must skip it.
    assert "intelligent_insert_text" in _HANDLES_OWN_RESPONSE


def test_wrap_or_insert_owns_its_response():
    # wrap_or_insert emits exactly one Schema A response end-to-end for
    # every branch (selection-wrap, text-stripped, empty-delimiters, error).
    # wh-d43oi added it to the self-owning set so the generic emitter does
    # not double-emit.
    assert "wrap_or_insert" in _HANDLES_OWN_RESPONSE


def test_start_overlay_walk_owns_its_response():
    # wh-n29v.37: the standalone numbered-overlay build handler walks the
    # focused window from scratch and emits exactly one
    # StartOverlayWalkResponse. It must be in the self-owning set so the
    # generic emitter does not double-emit (the wh-lla5d defence).
    assert "start_overlay_walk" in _HANDLES_OWN_RESPONSE


def test_pin_snapshot_owns_its_response():
    # wh-n29v.41: the active-overlay pin handler drives ElementFinder.pin and
    # emits exactly one PinSnapshotResponse. It must be in the self-owning set
    # so the generic emitter does not double-emit (the wh-lla5d defence). Logic
    # does not block the paint on the ack, but the Future must still resolve.
    assert "pin_snapshot" in _HANDLES_OWN_RESPONSE


def test_unpin_snapshot_owns_its_response():
    # wh-n29v.41: the clear-by-identity unpin handler drives
    # ElementFinder.unpin and emits exactly one PinSnapshotResponse. Same
    # wh-lla5d defence as pin_snapshot.
    assert "unpin_snapshot" in _HANDLES_OWN_RESPONSE


def test_pin_unpin_not_in_dequeue_anchor_injection_block():
    # wh-n29v.41: the command_dequeue_monotonic injection in input_proc.py is
    # scoped to the UIA-walk handlers (click_element, start_overlay_walk) whose
    # walk deadline must anchor at the reader's dequeue instant. pin_snapshot /
    # unpin_snapshot are store operations with NO walk deadline, so they must
    # NOT be added to that injection block (an unexpected command_dequeue_
    # monotonic kwarg would reach a handler that does not name it). Assert the
    # source still scopes the injection to exactly the two walk actions.
    import inspect

    import input_proc

    src = inspect.getsource(input_proc.input_process_main)
    assert 'if action in ("click_element", "start_overlay_walk"):' in src
    # The pin/unpin actions are not part of the dequeue-anchor branch.
    anchor_idx = src.index(
        'if action in ("click_element", "start_overlay_walk"):'
    )
    anchor_block = src[anchor_idx:anchor_idx + 300]
    assert "pin_snapshot" not in anchor_block
    assert "unpin_snapshot" not in anchor_block


def test_handles_own_response_is_frozen():
    # Frozen so downstream code cannot mutate the contract by accident.
    assert isinstance(_HANDLES_OWN_RESPONSE, frozenset)


def test_handler_methods_accept_request_id():
    """Every handler whose action is in _HANDLES_OWN_RESPONSE must accept
    a request_id keyword argument. The IPC envelope's request_id lives
    outside the params dict, so input_proc.py injects it explicitly. If
    the handler signature stops accepting request_id, the call raises
    TypeError on every dictation word and the speech pipeline times out
    on every request (regression caught 2026-04-24).
    """
    for action in _HANDLES_OWN_RESPONSE:
        method = getattr(UIActionHandler, action)
        sig = inspect.signature(method)
        assert "request_id" in sig.parameters, (
            f"UIActionHandler.{action} must accept a request_id keyword "
            f"argument because the action is in _HANDLES_OWN_RESPONSE"
        )


def test_handler_does_not_no_op_with_real_request_id():
    """End-to-end check: UIActionHandler.intelligent_insert_text with a
    real request_id must put exactly one Schema A response on the queue.

    Catches the 2026-04-24 regression where input_proc.py called
    method_to_call(**params) without injecting request_id, so the handler
    saw request_id=None, ResponseHandler.send_success no-oped, and the
    demuxer timed out on every word.
    """
    from unittest.mock import patch

    _MOD = "ui.ui_action_handler"
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"), \
         patch(f"{_MOD}.capture_context"):
        q = MagicMock()
        h = UIActionHandler(response_queue=q, config={"ui_actions": {"timing": {}}})
        h.terminal_editor.is_active = False
        h.utterance_manager.is_in_utterance.return_value = True
        h.utterance_manager._clipboard_dirty = False
        h.utterance_manager._last_paste_time = 0.0

        from ui.strategies.base import InsertionResult
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = InsertionResult(success=True, clipboard_dirty=True)
        h.router.get_strategy.return_value = mock_strategy

        h.intelligent_insert_text("hello", request_id="real-uuid-from-ipc")

        assert q.put.call_count == 1
        msg = q.put.call_args[0][0]
        assert msg["request_id"] == "real-uuid-from-ipc"
        assert msg["status"] == "ok"
