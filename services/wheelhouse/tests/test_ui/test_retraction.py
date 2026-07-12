"""Tests for Input Process retraction infrastructure.

Covers:
- ClipboardOperations paste character accumulator
- UIActionHandler retraction state lifecycle (start/end utterance resets)
- UIActionHandler.retract() with various gate conditions
- User interaction flag via invalidate_buffer
- SimplePaste strategy tracking
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from ui.context import UIContext


def _make_config(**overrides):
    cfg = {
        "ui_actions": {
            "timing": {
                "utterance_clipboard_timeout_seconds": 1.0,
            }
        }
    }
    cfg.update(overrides)
    return cfg


def _make_context(*, focused_control=None, is_flutter=False,
                  is_terminal=False, process_name="", class_name=""):
    return UIContext(
        focused_control=focused_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name=class_name,
    )


_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """Create a UIActionHandler with mocked specialist components.

    Defaults set so retract() reaches send_backspaces unless a test
    explicitly enables a gate. The focus checks (editor_focus_lost,
    focus_drifted) need win32gui.GetForegroundWindow patched per-test;
    the fixture sets the remembered/editor HWNDs to match a sentinel
    foreground value tests can opt into.
    """
    with patch(f"{_MOD}.TextPerfector") as MockTP, \
         patch(f"{_MOD}.ClipboardOperations") as MockCO, \
         patch(f"{_MOD}.WindowFocusManager") as MockWFM, \
         patch(f"{_MOD}.SelectionTransformer") as MockST, \
         patch(f"{_MOD}.UtteranceClipboardManager") as MockUCM, \
         patch(f"{_MOD}.ShadowBufferManager") as MockSBM, \
         patch(f"{_MOD}.TerminalEditorProxy") as MockTDE, \
         patch(f"{_MOD}.InsertionRouter") as MockRouter:

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(response_queue=q, config=_make_config())
        h.terminal_editor.is_active = False
        # MagicMock attribute access returns a truthy MagicMock by default.
        # Set explicit falsy defaults for the booleans the new fail-closed
        # gates inspect (wh-20yil, wh-t81d9.1), and a placeholder HWND so
        # the editor_focus_lost gate does not fire on tests that do not
        # care. Tests opting into focus-drift behavior must set
        # _last_target_hwnd / editor_hwnd to whatever they patch
        # win32gui.GetForegroundWindow to return.
        h.clipboard.last_paste_was_optimistic = False
        # wh-pkhrp.1.7: the grapheme-unsafe retract gate fires only on
        # text that contained surrogate pairs or ZWJ joiners. The
        # legacy tests below pretend the dictation was ASCII; pin the
        # flag False so the MagicMock default does not trip the gate.
        h.clipboard.accumulated_has_grapheme_unsafe = False
        # wh-pkhrp.2: retract picks the backspace count based on
        # accumulated_paste_was_qt. The legacy tests pre-date the
        # branch and assume the code-unit counter; pin was_qt False so
        # the MagicMock default does not steer retract into the Qt
        # cluster-counter path.
        h.clipboard.accumulated_paste_was_qt = False
        h.terminal_editor.editor_hwnd = 0xCAFE
        # Skip the non-terminal focus-drift check by default so existing
        # tests that do not patch GetForegroundWindow still hit
        # send_backspaces. Tests that exercise focus_drifted set this to
        # a real HWND value.
        h.window_manager._last_target_hwnd = None
        # wh-32d: also skip the text-target predicate gate by default.
        # Existing retraction tests pre-date the predicate; they assert
        # legacy behavior. Tests that exercise the new gate set
        # text_target_predicate explicitly.
        h.text_target_predicate = None
        yield h


class TestPasteCharacterAccumulator:
    """ClipboardOperations.accumulated_paste_chars tracking."""

    def test_initial_value_is_zero(self):
        from ui.clipboard_operations import ClipboardOperations
        co = ClipboardOperations(_make_config())
        assert co.accumulated_paste_chars == 0

    def test_reset_clears_counter(self):
        from ui.clipboard_operations import ClipboardOperations
        co = ClipboardOperations(_make_config())
        co.accumulated_paste_chars = 42
        co.reset_paste_counter()
        assert co.accumulated_paste_chars == 0


class TestRetractionStateLifecycle:
    """UIActionHandler retraction state across utterance lifecycle."""

    def test_start_utterance_resets_all_flags(self, handler):
        handler._user_interacted_during_utterance = True
        handler._used_simple_paste = True
        handler.clipboard.accumulated_paste_chars = 15

        handler.start_utterance(utterance_id=1)

        assert handler._user_interacted_during_utterance is False
        assert handler._used_simple_paste is False
        handler.clipboard.reset_paste_counter.assert_called()

    def test_end_utterance_resets_all_flags(self, handler):
        handler._user_interacted_during_utterance = True
        handler._used_simple_paste = True

        handler.end_utterance(utterance_id=1)

        assert handler._user_interacted_during_utterance is False
        assert handler._used_simple_paste is False
        handler.clipboard.reset_paste_counter.assert_called()

    def test_invalidate_buffer_sets_interaction_flag(self, handler):
        handler._user_interacted_during_utterance = False
        handler.invalidate_buffer(source="keyboard:a")
        assert handler._user_interacted_during_utterance is True


class TestRetractMethod:
    """UIActionHandler.retract() with various gate conditions."""

    def test_retract_blocked_by_user_interaction(self, handler):
        handler._user_interacted_during_utterance = True
        handler.clipboard.accumulated_paste_chars = 10

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "user_interacted"

    def test_retract_blocked_by_simple_paste(self, handler):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = True
        handler.clipboard.accumulated_paste_chars = 10

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "simple_paste"

    def test_retract_blocked_when_nothing_pasted(self, handler):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 0

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "nothing_to_retract"

    @patch("ui.ui_action_handler.send_backspaces")
    def test_retract_succeeds_sends_backspaces(self, mock_bs, handler):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 15

        result = handler.retract()

        assert result["status"] == "retracted"
        assert result["chars"] == 15
        mock_bs.assert_called_once_with(15)
        handler.clipboard.reset_paste_counter.assert_called()

    @patch("ui.ui_action_handler.send_backspaces")
    def test_retract_resets_counter_after_success(self, mock_bs, handler):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 5

        handler.retract()

        handler.clipboard.reset_paste_counter.assert_called()


class TestRetractClearsPendingLetterBuffer:
    """Retract drops a pending letter buffer that has not yet been pasted
    (wh-j3mgc).

    Background: ``intelligent_insert_text`` defers single letters in
    ``_letter_buffer`` and sends Schema A success without incrementing
    ``accumulated_paste_chars``. If STT then revises (``a`` -> ``hey``),
    retraction sees the counter at zero and returns ``not_retracted``.
    SpeechProcessor takes that as a signal to drop the corrected final, and
    the next ``end_utterance`` flushes the stale ``a``. The user sees the
    wrong text.

    Fix: when retract sees buffered letters but no real paste, it clears
    the buffer and returns ``retracted`` so the corrected final replays.
    """

    @patch("ui.ui_action_handler.send_backspaces")
    def test_retract_with_buffered_letter_clears_buffer_and_reports_retracted(
        self, mock_bs, handler
    ):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 0
        handler._letter_buffer = ["a"]

        result = handler.retract()

        assert result["status"] == "retracted", (
            f"Expected 'retracted' so SpeechProcessor replays the corrected "
            f"final; got {result}. Without this, end_utterance later flushes "
            f"the stale 'a' and the user sees wrong text (wh-j3mgc)."
        )
        assert handler._letter_buffer == [], (
            "Letter buffer was not cleared; end_utterance will still flush "
            "the stale letters."
        )
        # No backspaces fired (nothing was pasted to retract on screen).
        mock_bs.assert_not_called()

    @patch("ui.ui_action_handler.send_backspaces")
    def test_retract_with_buffered_letters_and_paste_chars_takes_paste_path(
        self, mock_bs, handler
    ):
        """If both a paste and letters are pending, retract must back out
        the paste AND drop the letter buffer."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 5
        handler._letter_buffer = ["b", "c"]

        result = handler.retract()

        assert result["status"] == "retracted"
        assert result["chars"] == 5
        mock_bs.assert_called_once_with(5)
        assert handler._letter_buffer == [], (
            "Letter buffer must be cleared on any successful retract so "
            "end_utterance does not flush stale letters."
        )

    def test_retract_no_paste_and_no_letters_still_blocks(self, handler):
        """The pre-existing 'nothing_to_retract' path must still trigger
        when there is genuinely nothing to undo."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 0
        handler._letter_buffer = []

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "nothing_to_retract"


class TestRetractFailsClosedOnUnverifiedState:
    """retract() must fail closed when the rest of the pipeline already
    recorded that the last paste was unverified (wh-20yil). Without this
    gate, retract sends backspaces against state it has no business
    assuming is in sync, and can chew into pre-existing user text.
    """

    @patch("ui.ui_action_handler.send_backspaces")
    def test_retract_fails_closed_on_optimistic_paste(self, mock_bs, handler):
        """clipboard.last_paste_was_optimistic=True means verified_paste
        proceeded under clipboard-lock contention without confirming the
        paste landed. Retract cannot trust accumulated_paste_chars."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 12
        handler.clipboard.last_paste_was_optimistic = True

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "paste_unverified"
        mock_bs.assert_not_called()


class TestRetractFocusVerification:
    """Per-strategy focus verification: backspaces follow foreground HWND
    so retract must refuse when focus has drifted between paste and
    retract (wh-t81d9.1).
    """

    @patch("ui.ui_action_handler.win32gui")
    @patch("ui.ui_action_handler.send_backspaces", return_value=True)
    def test_non_terminal_focus_drift_blocks_retract(
        self, mock_bs, mock_win32gui, handler
    ):
        """Non-terminal: remembered HWND no longer foreground -> focus_drifted."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 9
        handler.window_manager._last_target_hwnd = 0xAAA1
        mock_win32gui.GetForegroundWindow.return_value = 0xBBB2

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "focus_drifted"
        mock_bs.assert_not_called()

    @patch(
        "ui.ui_action_handler.normalize_hwnd_for_foreground_compare",
        side_effect=lambda h: h if h else None,
    )
    @patch("ui.ui_action_handler.win32gui")
    @patch("ui.ui_action_handler.send_backspaces", return_value=True)
    def test_non_terminal_focus_match_proceeds_to_send_backspaces(
        self, mock_bs, mock_win32gui, _mock_norm, handler
    ):
        """Non-terminal: remembered HWND == foreground -> proceed.

        Identity-stub normalize so the test's HWND values are not
        passed to the real Win32 GetAncestor (wh-oe7u.3).
        """
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 9
        handler.window_manager._last_target_hwnd = 0xAAA1
        mock_win32gui.GetForegroundWindow.return_value = 0xAAA1

        result = handler.retract()

        assert result["status"] == "retracted"
        assert result["chars"] == 9
        mock_bs.assert_called_once_with(9)

class TestRetractPartialSendInput:
    """retract() must refuse to claim success when SendInput reports
    partial delivery -- there is no way to know how much of the editor's
    content was actually deleted (wh-t81d9.1).
    """

    @patch("ui.ui_action_handler.send_backspaces", return_value=False)
    def test_partial_send_returns_not_retracted_with_chars_sent(
        self, mock_bs, handler
    ):
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 11

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "partial_send"
        assert result["chars_sent"] == 11
        # Counter is NOT reset on partial send -- the editor state is
        # unknown; do not pretend the paste accounting is back to zero.
        handler.clipboard.reset_paste_counter.assert_not_called()


class TestRetractHwndNormalization:
    """wh-oe7u.3: retract focus check normalizes both HWNDs through the
    same helper so Chromium/Electron renderer-child captures compare
    equal to top-level frame foregrounds. Mirrors the verified_paste
    contract; insertion and retraction must agree on what 'same target'
    means or accounting drifts.
    """

    @patch("ui.ui_action_handler.win32gui")
    @patch("ui.ui_action_handler.send_backspaces", return_value=True)
    def test_chromium_child_remembered_with_root_foreground_succeeds(
        self, mock_bs, mock_win32gui, handler
    ):
        """Non-terminal Chromium shape: remembered HWND is a renderer
        child, foreground is the top-level Chrome window. Both
        normalize to the same root -> retract proceeds."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 5
        child = 0xC11D
        root = 0xC007
        handler.window_manager._last_target_hwnd = child
        mock_win32gui.GetForegroundWindow.return_value = root

        with patch(
            "ui.ui_action_handler.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: root if h in (child, root) else None,
        ):
            result = handler.retract()

        assert result["status"] == "retracted"
        assert result["chars"] == 5
        mock_bs.assert_called_once_with(5)

    @patch("ui.ui_action_handler.win32gui")
    @patch("ui.ui_action_handler.send_backspaces", return_value=True)
    def test_remembered_hwnd_normalize_failure_blocks_with_focus_drifted(
        self, mock_bs, mock_win32gui, handler
    ):
        """Fail-closed: if the remembered HWND cannot be normalized,
        return focus_drifted (do not silently pass)."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.accumulated_paste_chars = 4
        handler.window_manager._last_target_hwnd = 0xDEAD
        mock_win32gui.GetForegroundWindow.return_value = 0xCAFE

        with patch(
            "ui.ui_action_handler.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: None if h == 0xDEAD else h,
        ):
            result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "focus_drifted"
        mock_bs.assert_not_called()

class TestRetractTextTargetGate:
    """wh-32d / wh-fc1x round-1 design: pre-retract text-target check.

    The HWND focus-drift check above proves the foreground window has
    not changed, but two controls inside the same top-level window can
    have very different roles. Sending Backspace to a non-text element
    can trigger page navigation, close menus, or deselect items rather
    than delete from the original text input. The shared
    TextTargetPredicate gates this case.

    The gate is skipped on Flutter (FlutterStrategy widgets often do
    not expose UIA TextPattern but are legitimate retraction targets).
    """

    def _accept_predicate(self):
        from ui.text_target import TextTargetPredicate, TextTargetVerdict
        p = MagicMock(spec=TextTargetPredicate)
        p.evaluate.return_value = TextTargetVerdict(
            verdict=True, reason="text_pattern_available",
            supported_patterns=("TextPattern",),
        )
        return p

    def _reject_predicate(self, reason="default_reject"):
        from ui.text_target import TextTargetPredicate, TextTargetVerdict
        p = MagicMock(spec=TextTargetPredicate)
        p.evaluate.return_value = TextTargetVerdict(
            verdict=False, reason=reason,
            control_type="ListItemControl", class_name="UIItem",
            process_name="explorer.exe",
        )
        return p

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_predicate_rejects_blocks_retract(self, mock_capture, mock_bs, handler):
        # Predicate says the current focus is not a text target; retract
        # must block before send_backspaces.
        handler.text_target_predicate = self._reject_predicate()
        handler.clipboard.accumulated_paste_chars = 5
        mock_capture.return_value = UIContext(
            focused_control=MagicMock(),
            is_flutter=False, is_terminal=False,
            process_name="explorer.exe", class_name="UIItem",
        )

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "text_target_rejected"
        mock_bs.assert_not_called()

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_predicate_accepts_proceeds_to_send_backspaces(self, mock_capture, mock_bs, handler):
        handler.text_target_predicate = self._accept_predicate()
        handler.clipboard.accumulated_paste_chars = 5
        mock_capture.return_value = UIContext(
            focused_control=MagicMock(),
            is_flutter=False, is_terminal=False,
            process_name="brave.exe", class_name="textarea medium",
        )

        result = handler.retract()

        assert result["status"] == "retracted"
        assert result["chars"] == 5
        mock_bs.assert_called_once_with(5)

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_flutter_context_skips_predicate_gate(self, mock_capture, mock_bs, handler):
        # is_flutter=True context proceeds without consulting the
        # predicate. Flutter widgets often do not expose TextPattern but
        # FlutterStrategy backspaces are still valid.
        predicate = self._reject_predicate()
        handler.text_target_predicate = predicate
        handler.clipboard.accumulated_paste_chars = 7
        mock_capture.return_value = UIContext(
            focused_control=MagicMock(),
            is_flutter=True, is_terminal=False,
            process_name="flutter_app.exe", class_name="FLUTTERVIEW",
        )

        result = handler.retract()

        assert result["status"] == "retracted"
        predicate.evaluate.assert_not_called()
        mock_bs.assert_called_once_with(7)

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_predicate_evaluate_called_with_empty_class_name(self, mock_capture, mock_bs, handler):
        # The retract gate passes class_name="" to the predicate so a
        # freshly captured control with empty ClassName cannot inherit a
        # captured-context class (parallel to wh-ix1z.15 in the slow
        # path).
        predicate = self._accept_predicate()
        handler.text_target_predicate = predicate
        handler.clipboard.accumulated_paste_chars = 3
        mock_capture.return_value = UIContext(
            focused_control=MagicMock(),
            is_flutter=False, is_terminal=False,
            process_name="brave.exe",
            class_name="textarea medium",  # captured-context class
        )

        handler.retract()

        kwargs = predicate.evaluate.call_args.kwargs
        assert kwargs["class_name"] == ""
        assert kwargs["process_name"] == "brave.exe"

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_no_predicate_wired_skips_gate_legacy_path(self, mock_capture, mock_bs, handler):
        # Legacy back-compat: when text_target_predicate is None, the
        # gate is skipped entirely and capture_context is not called.
        # Existing test fixtures rely on this.
        handler.text_target_predicate = None
        handler.clipboard.accumulated_paste_chars = 2

        result = handler.retract()

        assert result["status"] == "retracted"
        mock_capture.assert_not_called()
        mock_bs.assert_called_once_with(2)

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    @patch(f"{_MOD}.capture_context")
    def test_buffered_letter_clear_runs_before_predicate_gate(
        self, mock_capture, mock_bs, handler,
    ):
        """wh-ix1z.18: the buffered-letters early-return path must NOT
        be blocked by the text-target gate.

        Setup: char_count=0, _letter_buffer non-empty, predicate would
        reject if asked. The wh-j3mgc behavior expects a
        retracted/letter_buffer_cleared response; the gate must run
        AFTER that branch so a stale letter buffer is still cleared
        when current focus is on a non-text element.
        """
        handler.text_target_predicate = self._reject_predicate()
        handler.clipboard.accumulated_paste_chars = 0
        handler._letter_buffer = ["a"]
        # capture_context would only be called if the gate runs; the
        # buffered-letters branch should return before that.
        result = handler.retract()

        assert result["status"] == "retracted"
        assert result["reason"] == "letter_buffer_cleared"
        assert result["chars"] == 0
        assert handler._letter_buffer == []
        # send_backspaces is never called on the buffered-letter path.
        mock_bs.assert_not_called()
        # The predicate gate did not run -- buffered-letter cleanup
        # short-circuited first.
        handler.text_target_predicate.evaluate.assert_not_called()
        mock_capture.assert_not_called()
