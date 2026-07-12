"""Tests for TextPattern fast path in ClipboardFallbackStrategy.

Covers:
- UIA fast path: skips clipboard when TextPattern available
- Graceful fallback: uses clipboard when TextPattern unavailable
- Selection handling: passes has_selection from TextPattern to perfector
- Flutter skip: always uses clipboard for Flutter apps
"""
import pytest
from unittest.mock import MagicMock, patch, call

_STRAT_MOD = "ui.strategies.specific"


def _make_strategy():
    """Create a ClipboardFallbackStrategy with mocked dependencies."""
    from ui.strategies.specific import ClipboardFallbackStrategy
    from ui.context import UIContext

    buffer = MagicMock()
    perfector = MagicMock()
    perfector.perfected_string.return_value = " hello"
    clipboard = MagicMock()
    clipboard.verified_paste.return_value = True
    clipboard.gather_context.return_value = {'preceding_chars': 'ab', 'has_selection': False}
    window_mgr = MagicMock()

    strategy = ClipboardFallbackStrategy(buffer, perfector, clipboard, window_mgr)

    ctx = UIContext(
        focused_control=MagicMock(),
        is_flutter=False,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
        process_id=1234,
    )
    return strategy, ctx, clipboard, perfector, buffer


class TestTextPatternFastPath:
    """ClipboardFallbackStrategy should try TextPattern before clipboard."""

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_skips_clipboard_when_text_pattern_succeeds(self, mock_uia):
        """Should not call gather_context when TextPattern provides context."""
        mock_uia.return_value = {'preceding_chars': 'ab', 'has_selection': False}
        strategy, ctx, clipboard, perfector, _ = _make_strategy()
        strategy.insert("hello", ctx)
        clipboard.gather_context.assert_not_called()
        clipboard.clear_selection.assert_not_called()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_falls_back_to_clipboard_when_text_pattern_fails(self, mock_uia):
        """Should use clipboard gather_context when TextPattern returns None."""
        mock_uia.return_value = None
        strategy, ctx, clipboard, perfector, _ = _make_strategy()
        strategy.insert("hello", ctx)
        clipboard.gather_context.assert_called_once()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_passes_text_pattern_context_to_perfector(self, mock_uia):
        """Should pass UIA-gathered context to text perfector."""
        mock_uia.return_value = {'preceding_chars': '.', 'has_selection': False}
        strategy, ctx, clipboard, perfector, _ = _make_strategy()
        strategy.insert("hello", ctx)
        perfector.perfected_string.assert_called_once_with(
            "hello",
            preceding_chars='.',
            has_selection=False,
        )

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_passes_selection_state_from_text_pattern(self, mock_uia):
        """Should pass has_selection=True when TextPattern detects selection."""
        mock_uia.return_value = {'preceding_chars': 'xy', 'has_selection': True}
        strategy, ctx, clipboard, perfector, _ = _make_strategy()
        strategy.insert("hello", ctx)
        perfector.perfected_string.assert_called_once_with(
            "hello",
            preceding_chars='xy',
            has_selection=True,
        )

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_still_pastes_via_clipboard_on_fast_path(self, mock_uia):
        """TextPattern fast path still uses clipboard for the actual paste."""
        mock_uia.return_value = {'preceding_chars': 'ab', 'has_selection': False}
        strategy, ctx, clipboard, perfector, _ = _make_strategy()
        strategy.insert("hello", ctx)
        clipboard.verified_paste.assert_called_once()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_updates_shadow_buffer_on_fast_path_success(self, mock_uia):
        """Should update shadow buffer after successful fast-path insertion."""
        mock_uia.return_value = {'preceding_chars': 'ab', 'has_selection': False}
        strategy, ctx, clipboard, perfector, buffer = _make_strategy()
        strategy.insert("hello", ctx)
        buffer.update_from_clipboard_data.assert_called_once()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_updates_shadow_buffer_with_clipboard_context_on_slow_path(self, mock_uia):
        """Should pass clipboard preceding_chars (not empty) to shadow buffer on slow path."""
        mock_uia.return_value = None  # Force clipboard fallback
        strategy, ctx, clipboard, perfector, buffer = _make_strategy()
        clipboard.gather_context.return_value = {'preceding_chars': 'xy', 'has_selection': False}
        strategy.insert("hello", ctx)
        # Verify the shadow buffer got 'xy' from clipboard, not empty string
        buffer.update_from_clipboard_data.assert_called_once()
        call_args = buffer.update_from_clipboard_data.call_args
        assert call_args[0][0] == 'xy hello'  # reconstructed_buffer = preceding + inserted
        assert call_args[0][1] == len('xy hello')  # cursor_pos

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_skips_fast_path_for_flutter(self, mock_uia):
        """Should skip TextPattern for Flutter apps (UIA unreliable)."""
        from ui.context import UIContext
        strategy, _, clipboard, perfector, _ = _make_strategy()
        flutter_ctx = UIContext(
            focused_control=MagicMock(),
            is_flutter=True,
            is_terminal=False,
            process_name="flutter.exe",
            class_name="FLUTTERVIEW",
            process_id=5678,
        )
        strategy.insert("hello", flutter_ctx)
        mock_uia.assert_not_called()
        clipboard.gather_context.assert_called_once()


class TestAutoRestoreClearedSelection:
    """ClipboardFallbackStrategy must auto-restore a cleared selection
    only on a clean pre-send verified_paste failure (wh-t81d9.5).

    The slow path calls clear_selection -> gather_context ->
    verified_paste. If clear_selection deleted a real selection and
    verified_paste then returns False BEFORE the Ctrl+V keystroke
    fired, the deleted text is recoverable: gather_context's arrow-key
    sequence is balanced so the caret is back where it was, the target
    field is unchanged, and the saved selection can be raw-pasted to
    put the user's text back. After Ctrl+V fires, the target is
    unknown and the restore would compound corruption.
    """

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_pre_send_failure_triggers_restore(self, mock_uia):
        """verified_paste returns False, last_paste_was_sent is False
        -> restore fires."""
        mock_uia.return_value = None  # Force slow path
        strategy, ctx, clipboard, _, _ = _make_strategy()
        clipboard.verified_paste.return_value = False
        clipboard.last_paste_was_sent = False

        strategy.insert("hello", ctx)

        clipboard.restore_cleared_selection.assert_called_once()
        kwargs = clipboard.restore_cleared_selection.call_args.kwargs
        # Restore must use the same target plumbing as the original
        # paste, otherwise focus drift could land the saved selection
        # in the wrong window.
        assert kwargs["target_control"] is ctx.focused_control

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_post_send_failure_does_not_restore(self, mock_uia):
        """verified_paste returns False, last_paste_was_sent is True
        -> restore must NOT fire (Ctrl+V already landed somewhere; a
        raw paste of the saved selection would compound corruption)."""
        mock_uia.return_value = None
        strategy, ctx, clipboard, _, _ = _make_strategy()
        clipboard.verified_paste.return_value = False
        clipboard.last_paste_was_sent = True

        strategy.insert("hello", ctx)

        clipboard.restore_cleared_selection.assert_not_called()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_success_clears_last_cleared_selection_slot(self, mock_uia):
        """A successful paste must clear last_cleared_selection so a
        later unrelated failure does not restore stale text."""
        mock_uia.return_value = None
        strategy, ctx, clipboard, _, _ = _make_strategy()
        clipboard.verified_paste.return_value = True
        clipboard.last_cleared_selection = "from a prior call"

        strategy.insert("hello", ctx)

        assert clipboard.last_cleared_selection is None
        clipboard.restore_cleared_selection.assert_not_called()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_exception_branch_does_not_restore(self, mock_uia):
        """If gather_context throws after clear_selection succeeded,
        the caret is in an indeterminate position. Auto-restore would
        land the saved selection at the wrong location. Skip it."""
        mock_uia.return_value = None
        strategy, ctx, clipboard, _, _ = _make_strategy()
        # Force gather_context to raise so the except branch fires
        clipboard.gather_context.side_effect = RuntimeError("partial nav")

        result = strategy.insert("hello", ctx)

        assert result.success is False
        clipboard.restore_cleared_selection.assert_not_called()

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_flutter_pre_send_failure_passes_flutter_control(self, mock_uia):
        """On Flutter slow-path failure the restore must receive the
        focused_control as flutter_control so _raw_paste fires
        SendKeys('{Ctrl}v') instead of the SendInput keystroke that
        Flutter fields can silently drop."""
        from ui.context import UIContext

        mock_uia.return_value = None
        strategy, _, clipboard, _, _ = _make_strategy()
        clipboard.verified_paste.return_value = False
        clipboard.last_paste_was_sent = False

        flutter_focus = MagicMock()
        flutter_ctx = UIContext(
            focused_control=flutter_focus,
            is_flutter=True,
            is_terminal=False,
            process_name="flutter.exe",
            class_name="FLUTTERVIEW",
            process_id=5678,
        )

        strategy.insert("hello", flutter_ctx)

        clipboard.restore_cleared_selection.assert_called_once()
        kwargs = clipboard.restore_cleared_selection.call_args.kwargs
        assert kwargs["flutter_control"] is flutter_focus

    @patch(f"{_STRAT_MOD}.read_context_via_text_pattern")
    def test_non_flutter_pre_send_failure_omits_flutter_control(self, mock_uia):
        """Non-Flutter targets must NOT receive a flutter_control --
        passing one would route the restore through SendKeys, which is
        not what verified_paste used."""
        mock_uia.return_value = None
        strategy, ctx, clipboard, _, _ = _make_strategy()
        clipboard.verified_paste.return_value = False
        clipboard.last_paste_was_sent = False

        strategy.insert("hello", ctx)

        kwargs = clipboard.restore_cleared_selection.call_args.kwargs
        assert kwargs["flutter_control"] is None
