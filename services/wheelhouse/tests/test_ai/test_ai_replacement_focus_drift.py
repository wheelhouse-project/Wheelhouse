"""The awaited AI replacement must not land in a window the user moved to.

wh-review-pattern-fixes.45: this is the worst of the three
selection-to-replacement paths, because the model request takes real
time. ``capture_selected_text`` runs in the Input process,
``_run_ai_text_transform`` awaits the model in the Logic process, and
``replace_selected_text`` sends the Ctrl+V. A user can select text in
window A, ask for a correction, switch to window B while the model
runs, and -- before this fix -- have B's selection replaced while the
flow spoke "Done".

The tests below run the whole lifecycle: the real Input-process handler
methods on both ends, an awaited model in between, and a foreground that
changes DURING the await. They assert on the send function, not on a
return value.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_ai.test_silent_actions import notifications

from ai.providers.openai_compat import ChatResult, ChatStatus

_MOD = "ui.ui_action_handler"

_FULL = (True, 4, 4)
_HWND_A = 1111


def _make_config():
    return {
        "ui_actions": {
            "timing": {
                "utterance_clipboard_timeout_seconds": 1.0,
                "clipboard_verification_timeout_ms": 250,
            }
        }
    }


def _control_for(hwnd):
    control = MagicMock()
    top = MagicMock()
    top.NativeWindowHandle = hwnd
    control.GetTopLevelControl.return_value = top
    return control


def _make_handler():
    """A real UIActionHandler with its specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        handler = UIActionHandler(
            response_queue=MagicMock(), config=_make_config(),
        )
    handler.clipboard.clipboard_verification_timeout = 0.01
    handler.clipboard._safe_copy.return_value = True
    handler.clipboard.last_clipboard_write_seq = 7
    handler._letter_buffer = []
    handler._poll_clipboard = MagicMock(return_value="original text")
    return handler


def _make_ai(corrected="corrected text"):
    ai = MagicMock()
    ai.cancel_requested = False
    ai.is_ready = MagicMock(return_value=True)
    ai.recheck_ready = AsyncMock(return_value=True)
    lock = asyncio.Lock()
    ai._processing_lock = lock
    ai.is_processing = MagicMock(side_effect=lambda: lock.locked())
    ai.fix_text = AsyncMock(
        return_value=ChatResult(status=ChatStatus.OK, text=corrected)
    )
    return ai


def _make_actions(ai):
    from speech.actions import ActionFunctions

    speech_handler = MagicMock()
    service_manager = MagicMock()
    service_manager.ai_service = ai
    logic_controller = MagicMock()
    logic_controller.service_manager = service_manager
    speech_handler.logic_controller = logic_controller
    return ActionFunctions(speech_handler)


def _wire_ipc(actions, handler, sent_keys):
    """Route the Logic-side requests to the real Input-side handler.

    ``params`` is what actually crosses the process boundary, so this
    also proves the carried identity survives that trip: only plain
    values go through.
    """
    async def send_request(action, params=None):
        params = params or {}
        for value in params.values():
            assert isinstance(value, (str, int, type(None))), (
                f"{action} params must be plain values, got {value!r}"
            )
        with patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.time"), \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.type_string") as mock_type, \
             patch(f"{_MOD}.verified_press_keys",
                   side_effect=lambda *keys: (
                       sent_keys.append(keys) or _FULL
                   )):
            result = getattr(handler, action)(**params)
            mock_type.assert_not_called()
        return result

    actions.speech_handler.app.send_request = AsyncMock(
        side_effect=send_request,
    )


@pytest.mark.asyncio
async def test_replacement_is_refused_when_the_user_switched_windows():
    """Control A is captured; window B is foreground when the model answers."""
    handler = _make_handler()
    control_a = _control_for(_HWND_A)
    sent_keys = []

    ai = _make_ai()
    actions = _make_actions(ai)
    _wire_ipc(actions, handler, sent_keys)

    # The capture proves nothing; it only records the target. The proof
    # runs at replacement time, and by then the user has moved to B.
    handler.clipboard.prove_captured_target_is_foreground.return_value = False

    with patch(f"{_MOD}.capture_context",
               return_value=MagicMock(focused_control=control_a)), \
         patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
               side_effect=lambda h: h if h else None):
        await actions.fix_text_ai()

    # Ctrl+C for the capture is expected. Ctrl+V is not.
    assert ("ctrl", "c") in sent_keys
    assert ("ctrl", "v") not in sent_keys
    # Nothing was written to the clipboard for the replacement either;
    # only the capture sentinel was.
    for call_args in handler.clipboard._safe_copy.call_args_list:
        assert call_args.args[0].startswith("__SENTINEL__")
    # The user is told plainly that nothing changed.
    displayed = " ".join(
        str(c) for c in notifications(actions)
    ).lower()
    assert "window changed" in displayed
    assert "unchanged" in displayed
    # The working dialog precedes the model; a refused paste must not show Done.
    brief = [str(c) for c in notifications(actions)]
    assert "Done." not in brief


@pytest.mark.asyncio
async def test_replacement_is_sent_when_the_captured_target_kept_focus():
    """The carried identity must be the RIGHT one, not merely present."""
    handler = _make_handler()
    control_a = _control_for(_HWND_A)
    sent_keys = []

    ai = _make_ai()
    actions = _make_actions(ai)
    _wire_ipc(actions, handler, sent_keys)

    handler.clipboard.prove_captured_target_is_foreground.return_value = True

    with patch(f"{_MOD}.capture_context",
               return_value=MagicMock(focused_control=control_a)), \
         patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
               side_effect=lambda h: h if h else None):
        await actions.fix_text_ai()

    assert ("ctrl", "v") in sent_keys
    # The proof was asked about the window the capture recorded.
    proof_call = handler.clipboard.prove_captured_target_is_foreground.call_args
    assert _HWND_A in proof_call.args
    assert "Done." in notifications(actions)
