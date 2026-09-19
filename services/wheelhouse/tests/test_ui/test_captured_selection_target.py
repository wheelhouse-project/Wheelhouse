"""Selection-derived replacements must carry their captured target.

wh-review-pattern-fixes.45: three paths copy a selection out of one
control and then deliver a replacement without carrying that control
forward.

1. ``transform_selection`` copies the selection from control A and then
   calls ``verbatim_insert_text`` with only the transformed text.
2. The selection branch of ``wrap_or_insert`` has the same shape.
3. ``capture_selected_text`` captures from A, the Logic process awaits
   the AI model, and then ``replace_selected_text`` writes the clipboard
   and sends Ctrl+V to whatever window holds the foreground.

``_execute_insert_with_ack`` calls a fresh ``capture_context()`` and
routes against whatever control that returns, so a focus change between
the copy and the paste sends the replacement to the wrong window. The
pre-send focus proof added by wh-review-pattern-fixes.41 does not help,
because these paths supplied no explicit target and therefore landed in
the deliberate no-captured-target fallback.

Every test here asserts on the send functions -- ``verified_press_keys``
and ``type_string`` -- not on a return value alone. A refusal that still
sends a keystroke is the failure this bead exists to stop.
"""
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from ui.context import UIContext
from ui.strategies.base import InsertionMode, InsertionOptions

_HANDLER_MOD = "ui.ui_action_handler"
_CLIP_MOD = "ui.clipboard_operations"
_HWND_MOD = "ui.ui_action_handler"

# verified_press_keys returns (success, accepted_events, expected_events).
_FULL = (True, 6, 6)

# Two windows. A is the control the selection was captured from; B is the
# window the user moved to while the replacement was being composed.
_HWND_A = 1111
_HWND_B = 2222


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config():
    return {
        "ui_actions": {
            "timing": {
                "utterance_clipboard_timeout_seconds": 1.0,
                "clipboard_verification_timeout_ms": 250,
                "clipboard_operation_delay_ms": 50,
                "selection_clear_delay_ms": 20,
                "context_gather_delay_ms": 10,
                "post_paste_delay_ms": 30,
            }
        }
    }


def _fake_normalization():
    """Make the test HWNDs survive GetAncestor(GA_ROOT).

    The handles below are invented, so the real Win32 call rejects them.
    Patch the normalization the handler uses to resolve a captured
    control, so a test HWND normalizes to itself.
    """
    return patch(f"{_HWND_MOD}.normalize_hwnd_for_foreground_compare",
                 side_effect=lambda h: h if h else None)


def _control_for(hwnd):
    """Build a UIA control mock whose top-level window handle is ``hwnd``."""
    control = MagicMock()
    top = MagicMock()
    top.NativeWindowHandle = hwnd
    control.GetTopLevelControl.return_value = top
    control.Exists.return_value = True
    return control


def _context_for(control):
    return UIContext(
        focused_control=control,
        is_flutter=False,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
    )


@pytest.fixture
def handler():
    """UIActionHandler with the specialist components mocked.

    ClipboardOperations is a mock here; the proof helper it exposes is
    driven per test. The real proof is exercised separately in
    ``TestSimplePasteCarriesCapturedTarget``.
    """
    with patch(f"{_HANDLER_MOD}.TextPerfector"), \
         patch(f"{_HANDLER_MOD}.ClipboardOperations"), \
         patch(f"{_HANDLER_MOD}.WindowFocusManager"), \
         patch(f"{_HANDLER_MOD}.SelectionTransformer"), \
         patch(f"{_HANDLER_MOD}.UtteranceClipboardManager"), \
         patch(f"{_HANDLER_MOD}.ShadowBufferManager"), \
         patch(f"{_HANDLER_MOD}.TerminalEditorProxy"), \
         patch(f"{_HANDLER_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        h = UIActionHandler(response_queue=MagicMock(), config=_make_config())
        h.clipboard.clipboard_verification_timeout = 0.01
        h.clipboard._safe_copy.return_value = True
        h.clipboard.last_clipboard_write_seq = 7
        h._letter_buffer = []
        yield h


# ===========================================================================
# transform_selection
# ===========================================================================

class TestTransformSelectionCarriesTarget:
    """transform_selection must prove control A before the paste-back."""

    def _run(self, handler, *, proof, transformed="TRANSFORMED"):
        control_a = _control_for(_HWND_A)
        control_b = _control_for(_HWND_B)
        handler.clipboard.prove_captured_target_is_foreground.return_value = proof
        handler.selection_transformer.apply_transformation.return_value = (
            transformed
        )
        with _fake_normalization(), \
             patch(f"{_HANDLER_MOD}.capture_context",
                   side_effect=[_context_for(control_a),
                                _context_for(control_b)]), \
             patch(f"{_HANDLER_MOD}.clipboard_context"), \
             patch(f"{_HANDLER_MOD}.pyperclip") as mock_clip, \
             patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL) as mock_vpk, \
             patch(f"{_HANDLER_MOD}.type_string") as mock_type, \
             patch(f"{_HANDLER_MOD}._send_modifier_keyups"):
            mock_clip.paste.return_value = "selected"
            handler.transform_selection("upper", request_id="rid-1")
        return control_a, mock_vpk, mock_type

    def test_refuses_when_the_captured_target_lost_the_foreground(
        self, handler,
    ):
        """Focus moved to B: no strategy runs and no key is sent."""
        control_a, mock_vpk, mock_type = self._run(handler, proof=False)

        # The proof was asked about control A, the capture-time target.
        call_kwargs = (
            handler.clipboard.prove_captured_target_is_foreground.call_args
        )
        assert call_kwargs is not None
        assert control_a in call_kwargs.args or control_a in (
            call_kwargs.kwargs.values()
        )
        # No paste keystroke, no Unicode send, no routing.
        assert call("ctrl", "v") not in mock_vpk.call_args_list
        mock_type.assert_not_called()
        handler.router.get_strategy.assert_not_called()

    def test_refuses_even_when_the_fresh_capture_finds_no_control(
        self, handler,
    ):
        """The SimplePaste case: no focusable control at paste time.

        The router picks SimplePasteStrategy when the fresh capture
        returns no control, and that strategy used to reach the
        deliberate no-captured-target branch of ``verified_paste``. The
        gate refuses before the router is consulted at all.
        """
        control_a = _control_for(_HWND_A)
        empty_context = UIContext(
            focused_control=None, is_flutter=False, is_terminal=False,
            process_name="", class_name="",
        )
        handler.clipboard.prove_captured_target_is_foreground.return_value = (
            False
        )
        handler.selection_transformer.apply_transformation.return_value = "T"
        with _fake_normalization(), \
             patch(f"{_HANDLER_MOD}.capture_context",
                   side_effect=[_context_for(control_a), empty_context]), \
             patch(f"{_HANDLER_MOD}.clipboard_context"), \
             patch(f"{_HANDLER_MOD}.pyperclip") as mock_clip, \
             patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL) as mock_vpk, \
             patch(f"{_HANDLER_MOD}.type_string") as mock_type, \
             patch(f"{_HANDLER_MOD}._send_modifier_keyups"):
            mock_clip.paste.return_value = "selected"
            handler.transform_selection("upper", request_id="rid-3")

        assert call("ctrl", "v") not in mock_vpk.call_args_list
        mock_type.assert_not_called()
        handler.router.get_strategy.assert_not_called()

    def test_delivers_when_the_captured_target_still_holds_focus(
        self, handler,
    ):
        """The proof passes: routing runs, so the target really was carried."""
        _control_a, _mock_vpk, _mock_type = self._run(handler, proof=True)

        handler.router.get_strategy.assert_called_once()
        # The strategy receives the captured target through InsertionOptions.
        insert_call = handler.router.get_strategy.return_value.insert
        insert_call.assert_called_once()
        opts = insert_call.call_args[0][3]
        assert opts.captured_target_hwnd == _HWND_A
        assert opts.mode is InsertionMode.VERBATIM


# ===========================================================================
# wrap_or_insert (selection branch)
# ===========================================================================

class TestWrapOrInsertCarriesTarget:
    """The selection branch has the same shape as transform_selection."""

    def test_refuses_when_the_captured_target_lost_the_foreground(
        self, handler,
    ):
        control_a = _control_for(_HWND_A)
        control_b = _control_for(_HWND_B)
        handler.clipboard.prove_captured_target_is_foreground.return_value = False
        handler.utterance_manager._last_paste_time = 0.0
        with _fake_normalization(), \
             patch(f"{_HANDLER_MOD}.capture_context",
                   side_effect=[_context_for(control_a),
                                _context_for(control_b)]), \
             patch(f"{_HANDLER_MOD}.clipboard_context"), \
             patch(f"{_HANDLER_MOD}.pyperclip") as mock_clip, \
             patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL) as mock_vpk, \
             patch(f"{_HANDLER_MOD}.type_string") as mock_type, \
             patch(f"{_HANDLER_MOD}._send_modifier_keyups"):
            mock_clip.paste.return_value = "selected"
            handler.wrap_or_insert("(", ")", "", request_id="rid-2")

        handler.clipboard.prove_captured_target_is_foreground.assert_called_once()
        assert call("ctrl", "v") not in mock_vpk.call_args_list
        mock_type.assert_not_called()
        handler.router.get_strategy.assert_not_called()


# ===========================================================================
# SimplePasteStrategy fallback (real ClipboardOperations)
# ===========================================================================

class TestSimplePasteCarriesCapturedTarget:
    """The fallback the finding names must receive the captured target.

    SimplePasteStrategy runs when the fresh capture finds no focusable
    control. It used to call ``verified_paste`` with no target at all,
    which is the branch that deliberately keeps sending. With the
    captured target forwarded, ``verified_paste`` runs its own
    wh-review-pattern-fixes.41 proof immediately before Ctrl+V.
    """

    def _make_strategy(self):
        from ui.clipboard_operations import ClipboardOperations
        from ui.strategies.specific import SimplePasteStrategy

        ops = ClipboardOperations(_make_config())
        window_manager = MagicMock()
        window_manager.ensure_focused.return_value = True
        return SimplePasteStrategy(ops, window_manager), window_manager

    def _run(self, *, captured_hwnd, foreground_hwnd):
        strategy, window_manager = self._make_strategy()
        control_a = _control_for(_HWND_A)
        context = UIContext(
            focused_control=None, is_flutter=False, is_terminal=False,
            process_name="", class_name="",
        )
        options = InsertionOptions(
            mode=InsertionMode.VERBATIM,
            captured_target_control=control_a,
            captured_target_hwnd=captured_hwnd,
        )
        with patch(f"{_CLIP_MOD}.normalize_hwnd_for_foreground_compare",
                   side_effect=lambda h: h if h else None), \
             patch(f"{_CLIP_MOD}.win32gui") as mock_win32gui, \
             patch(f"{_CLIP_MOD}.time") as mock_time, \
             patch(f"{_CLIP_MOD}.verified_press_keys",
                   return_value=_FULL) as mock_vpk, \
             patch(f"{_CLIP_MOD}.type_string", create=True) as mock_type, \
             patch(f"{_CLIP_MOD}.pyperclip") as mock_clip:
            mock_time.perf_counter.side_effect = lambda: 0.0
            mock_time.sleep = Mock()
            mock_clip.copy = Mock()
            mock_clip.paste.return_value = "final text"
            mock_win32gui.GetForegroundWindow.return_value = foreground_hwnd
            result = strategy.insert("final text", context, None, options)
        return result, mock_vpk, mock_type, window_manager

    def test_refuses_when_the_captured_target_is_not_foreground(self):
        result, mock_vpk, mock_type, _wm = self._run(
            captured_hwnd=_HWND_A, foreground_hwnd=_HWND_B,
        )

        assert result.success is False
        mock_vpk.assert_not_called()
        mock_type.assert_not_called()

    def test_sends_when_the_captured_target_holds_the_foreground(self):
        """The carried identity must be the RIGHT one, not merely present."""
        result, mock_vpk, _mock_type, _wm = self._run(
            captured_hwnd=_HWND_A, foreground_hwnd=_HWND_A,
        )

        assert result.success is True
        mock_vpk.assert_called_once_with("ctrl", "v")


# ===========================================================================
# The AI path: capture_selected_text -> replace_selected_text
# ===========================================================================

class TestCaptureReturnsTheTargetIdentity:
    """capture_selected_text must report the target it captured from."""

    def test_capture_returns_a_token_and_the_target_hwnd(self, handler):
        control_a = _control_for(_HWND_A)
        handler._poll_clipboard = Mock(return_value="selected")
        with _fake_normalization(), \
             patch(f"{_HANDLER_MOD}.capture_context",
                   return_value=_context_for(control_a)), \
             patch(f"{_HANDLER_MOD}.clipboard_context"), \
             patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL), \
             patch(f"{_HANDLER_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["text"] == "selected"
        assert result["target_hwnd"] == _HWND_A
        assert isinstance(result["capture_token"], str)
        assert result["capture_token"]

    def test_each_capture_issues_a_fresh_token(self, handler):
        control_a = _control_for(_HWND_A)
        handler._poll_clipboard = Mock(return_value="selected")
        tokens = []
        for _ in range(2):
            with _fake_normalization(), \
                 patch(f"{_HANDLER_MOD}.capture_context",
                       return_value=_context_for(control_a)), \
                 patch(f"{_HANDLER_MOD}.clipboard_context"), \
                 patch(f"{_HANDLER_MOD}.verified_press_keys",
                       return_value=_FULL), \
                 patch(f"{_HANDLER_MOD}.time"):
                tokens.append(
                    handler.capture_selected_text()["capture_token"]
                )

        assert tokens[0] != tokens[1]


class TestReplaceSelectedTextCarriesTarget:
    """replace_selected_text must prove the captured target first."""

    def _capture(self, handler, control):
        handler._poll_clipboard = Mock(return_value="selected")
        with _fake_normalization(), \
             patch(f"{_HANDLER_MOD}.capture_context",
                   return_value=_context_for(control)), \
             patch(f"{_HANDLER_MOD}.clipboard_context"), \
             patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL), \
             patch(f"{_HANDLER_MOD}.time"):
            return handler.capture_selected_text()

    def _replace(self, handler, **kwargs):
        handler.clipboard._safe_copy.reset_mock()
        with patch(f"{_HANDLER_MOD}.verified_press_keys",
                   return_value=_FULL) as mock_vpk, \
             patch(f"{_HANDLER_MOD}.type_string") as mock_type, \
             patch(f"{_HANDLER_MOD}._send_modifier_keyups"), \
             patch(f"{_HANDLER_MOD}.time"):
            result = handler.replace_selected_text(**kwargs)
        return result, mock_vpk, mock_type

    def test_refuses_when_the_foreground_drifted_during_the_ai_request(
        self, handler,
    ):
        control_a = _control_for(_HWND_A)
        captured = self._capture(handler, control_a)
        handler.clipboard.prove_captured_target_is_foreground.return_value = (
            False
        )

        result, mock_vpk, mock_type = self._replace(
            handler,
            text="corrected",
            target_hwnd=captured["target_hwnd"],
            capture_token=captured["capture_token"],
        )

        assert result["success"] is False
        assert result["focus_drift"] is True
        mock_vpk.assert_not_called()
        mock_type.assert_not_called()
        # Nothing was written to the clipboard either.
        handler.clipboard._safe_copy.assert_not_called()

    def test_refuses_when_the_token_is_unknown(self, handler):
        """A token this process never issued cannot be resolved."""
        result, mock_vpk, mock_type = self._replace(
            handler,
            text="corrected",
            target_hwnd=_HWND_A,
            capture_token="never-issued",
        )

        assert result["success"] is False
        assert result["focus_drift"] is True
        mock_vpk.assert_not_called()
        mock_type.assert_not_called()
        handler.clipboard.prove_captured_target_is_foreground.assert_not_called()

    def test_refuses_when_the_reported_hwnd_does_not_match_the_capture(
        self, handler,
    ):
        """A WRONG identity must fail, not only a missing one."""
        control_a = _control_for(_HWND_A)
        captured = self._capture(handler, control_a)

        result, mock_vpk, mock_type = self._replace(
            handler,
            text="corrected",
            target_hwnd=_HWND_B,
            capture_token=captured["capture_token"],
        )

        assert result["success"] is False
        assert result["focus_drift"] is True
        mock_vpk.assert_not_called()
        mock_type.assert_not_called()
        handler.clipboard.prove_captured_target_is_foreground.assert_not_called()

    def test_refuses_when_no_captured_target_was_supplied(self, handler):
        """The only caller always has a captured target, so absence fails."""
        result, mock_vpk, mock_type = self._replace(
            handler, text="corrected",
        )

        assert result["success"] is False
        assert result["focus_drift"] is True
        mock_vpk.assert_not_called()
        mock_type.assert_not_called()

    def test_sends_when_the_captured_target_still_holds_focus(self, handler):
        """The carried identity must be the RIGHT one, not merely present."""
        control_a = _control_for(_HWND_A)
        captured = self._capture(handler, control_a)
        handler.clipboard.prove_captured_target_is_foreground.return_value = (
            True
        )

        result, mock_vpk, _mock_type = self._replace(
            handler,
            text="corrected",
            target_hwnd=captured["target_hwnd"],
            capture_token=captured["capture_token"],
        )

        assert result["success"] is True
        mock_vpk.assert_called_once_with("ctrl", "v")
        proof_call = (
            handler.clipboard.prove_captured_target_is_foreground.call_args
        )
        assert _HWND_A in proof_call.args or _HWND_A in (
            proof_call.kwargs.values()
        )
