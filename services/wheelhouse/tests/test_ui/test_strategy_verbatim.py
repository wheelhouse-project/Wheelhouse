"""Tests for the InsertionMode.VERBATIM contract across strategies (wh-iti5).

VERBATIM mode lets callers that already composed the final text -- the
selection-wrap branch of ``UIActionHandler.wrap_or_insert`` and the
paste-back step of ``UIActionHandler.transform_selection`` -- bypass
the TextPerfector pass and any prefix-space logic. The strategies must
deliver the supplied text exactly and credit retract accounting by
``len(text)`` so backspaces walk back the actual delivered length.
"""
from unittest.mock import MagicMock, patch

from ui.context import UIContext
from ui.strategies.base import InsertionMode, InsertionOptions
from ui.strategies.specific import (
    ClipboardFallbackStrategy,
    ShadowBufferStrategy,
    SimplePasteStrategy,
)


def _make_context(focused=None) -> UIContext:
    return UIContext(
        focused_control=focused,
        is_flutter=False,
        is_terminal=False,
        process_name="notepad.exe",
        class_name="Edit",
        process_id=1234,
    )


class TestSimplePasteStrategyVerbatim:
    def test_dictation_mode_appends_trailing_space(self):
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = True
        strategy = SimplePasteStrategy(clipboard, MagicMock())

        result = strategy.insert("hello", _make_context())

        assert result.success is True
        # First positional arg to verified_paste is the text actually pasted.
        args = clipboard.verified_paste.call_args.args
        assert args[0] == "hello "  # trailing space preserved in dictation

    def test_verbatim_mode_drops_trailing_space(self):
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = True
        strategy = SimplePasteStrategy(clipboard, MagicMock())

        result = strategy.insert(
            "(hello)",
            _make_context(),
            options=InsertionOptions(mode=InsertionMode.VERBATIM),
        )

        assert result.success is True
        args = clipboard.verified_paste.call_args.args
        # Verbatim must deliver the text exactly -- no decorations.
        assert args[0] == "(hello)"


class TestShadowBufferStrategyVerbatim:
    """Verbatim path skips buffer sync and the perfecter."""

    def test_verbatim_skips_synchronize_and_perfecter(self):
        buffer_manager = MagicMock()
        # Simulate an unsynchronised buffer: dictation mode would refuse,
        # verbatim mode must proceed regardless.
        buffer_manager.is_valid = False
        buffer_manager.synchronize.side_effect = AssertionError(
            "synchronize must not run in verbatim mode"
        )
        text_perfector = MagicMock()
        text_perfector.perfected_string.side_effect = AssertionError(
            "perfecter must not run in verbatim mode"
        )
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = True
        focused = MagicMock()
        focused.GetTopLevelControl.return_value = MagicMock(NativeWindowHandle=4242)

        strategy = ShadowBufferStrategy(
            buffer_manager, text_perfector, clipboard, MagicMock(),
        )
        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: int(h),
        ):
            result = strategy.insert(
                "[wrapped]",
                _make_context(focused=focused),
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is True
        assert result.clipboard_dirty is True
        # Verified_paste received the verbatim string exactly.
        args = clipboard.verified_paste.call_args.args
        assert args[0] == "[wrapped]"
        text_perfector.perfected_string.assert_not_called()


class TestClipboardFallbackStrategyVerbatim:
    """Verbatim path skips both UIA TextPattern and clipboard gather_context."""

    def test_verbatim_skips_textpattern_and_gather(self):
        buffer_manager = MagicMock()
        text_perfector = MagicMock()
        text_perfector.perfected_string.side_effect = AssertionError(
            "perfecter must not run in verbatim mode"
        )
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = True
        clipboard.last_paste_was_sent = False
        focused = MagicMock()
        focused.GetTopLevelControl.return_value = MagicMock(NativeWindowHandle=4242)

        strategy = ClipboardFallbackStrategy(
            buffer_manager, text_perfector, clipboard, MagicMock(),
        )

        with patch(
            "ui.strategies.specific.read_context_via_text_pattern"
        ) as mock_uia, patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: int(h),
        ):
            result = strategy.insert(
                "(hello)",
                _make_context(focused=focused),
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is True
        # Neither composition gather path runs in verbatim mode.
        mock_uia.assert_not_called()
        clipboard.gather_context.assert_not_called()
        text_perfector.perfected_string.assert_not_called()
        # Pasted text matches the verbatim input.
        args = clipboard.verified_paste.call_args.args
        assert args[0] == "(hello)"

    def test_verbatim_clears_stale_last_cleared_selection_at_entry(self):
        """wh-ksde.1: a stale last_cleared_selection from a prior dictation
        slow-path call must not survive a verbatim entry. The verbatim
        path never calls clear_selection, so it owns no selection that
        could legitimately need restoring -- and a stale value left over
        from a prior call could be raw-pasted into the wrong target on
        a later restore decision.
        """
        buffer_manager = MagicMock()
        text_perfector = MagicMock()
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = True
        clipboard.last_paste_was_sent = False
        # Seed stale state from an earlier slow-path call.
        clipboard.last_cleared_selection = "leftover from earlier call"
        focused = MagicMock()
        focused.GetTopLevelControl.return_value = MagicMock(NativeWindowHandle=4242)

        strategy = ClipboardFallbackStrategy(
            buffer_manager, text_perfector, clipboard, MagicMock(),
        )

        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: int(h),
        ):
            strategy.insert(
                "(hello)",
                _make_context(focused=focused),
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert clipboard.last_cleared_selection is None

    def test_verbatim_failure_does_not_restore_stale_selection(self):
        """wh-ksde.1: when verbatim verified_paste returns False before
        the keystroke fires, the strategy must NOT call
        restore_cleared_selection. The verbatim path skips clear_selection
        entirely, so any value in last_cleared_selection belongs to a
        prior call. Restoring it would raw-paste unrelated content into
        the current target.
        """
        buffer_manager = MagicMock()
        text_perfector = MagicMock()
        clipboard = MagicMock()
        clipboard.verified_paste.return_value = False  # pre-send failure
        clipboard.last_paste_was_sent = False
        clipboard.last_cleared_selection = "leftover from earlier call"
        focused = MagicMock()
        focused.GetTopLevelControl.return_value = MagicMock(NativeWindowHandle=4242)

        strategy = ClipboardFallbackStrategy(
            buffer_manager, text_perfector, clipboard, MagicMock(),
        )

        with patch(
            "ui.strategies.specific.normalize_hwnd_for_foreground_compare",
            side_effect=lambda h: int(h),
        ):
            result = strategy.insert(
                "(hello)",
                _make_context(focused=focused),
                options=InsertionOptions(mode=InsertionMode.VERBATIM),
            )

        assert result.success is False
        clipboard.restore_cleared_selection.assert_not_called()
        # Stale value was reset at entry; remains None on the failure path.
        assert clipboard.last_cleared_selection is None
