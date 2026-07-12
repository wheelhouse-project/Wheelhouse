"""Tests for ShadowBufferManager - synchronized text buffer for focused UI control.

Covers:
- Initial state (buffer invalid on creation)
- Invalidation (resets cursor_pos, clears buffer)
- get_context() (preceding chars, has_selection flag)
- update_after_insertion() (cursor movement, selection replacement, noop when invalid)
- update_from_clipboard_data() (sets buffer/cursor/selection from external data)
- synchronize() (mock UIA TextPattern, COMError handling, no focused control)
- Adversarial: concurrent access, boundary conditions
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

_MOD = "ui.shadow_buffer"


def _make_buffer():
    """Create a ShadowBufferManager with UIA imports mocked."""
    from ui.shadow_buffer import ShadowBufferManager
    return ShadowBufferManager()


# ===========================================================================
# Initial State
# ===========================================================================

class TestInitialState:
    """ShadowBufferManager initial state after construction."""

    def test_is_invalid_on_creation(self):
        """New buffer should be invalid (cursor_pos == -1)."""
        buf = _make_buffer()
        assert buf.is_valid is False

    def test_empty_buffer_on_creation(self):
        """New buffer should have empty string content."""
        buf = _make_buffer()
        assert buf._buffer == ""

    def test_zero_selection_on_creation(self):
        """New buffer should have no selection."""
        buf = _make_buffer()
        assert buf._selection_len == 0

    def test_cursor_pos_minus_one_on_creation(self):
        """Cursor position should be -1 (invalid sentinel)."""
        buf = _make_buffer()
        assert buf._cursor_pos == -1


# ===========================================================================
# Invalidation
# ===========================================================================

class TestInvalidation:
    """ShadowBufferManager.invalidate() behavior."""

    def test_invalidate_resets_cursor(self):
        """Invalidation should set cursor_pos to -1."""
        buf = _make_buffer()
        # Set up valid state
        buf._cursor_pos = 5
        buf._buffer = "hello"
        buf._selection_len = 0

        buf.invalidate()
        assert buf._cursor_pos == -1

    def test_invalidate_clears_buffer(self):
        """Invalidation should clear the buffer text."""
        buf = _make_buffer()
        buf._cursor_pos = 5
        buf._buffer = "hello"

        buf.invalidate()
        assert buf._buffer == ""

    def test_invalidate_clears_selection(self):
        """Invalidation should clear selection length."""
        buf = _make_buffer()
        buf._cursor_pos = 5
        buf._buffer = "hello world"
        buf._selection_len = 5

        buf.invalidate()
        assert buf._selection_len == 0

    def test_invalidate_makes_buffer_invalid(self):
        """After invalidation, is_valid should be False."""
        buf = _make_buffer()
        buf._cursor_pos = 5
        buf._buffer = "hello"

        buf.invalidate()
        assert buf.is_valid is False

    def test_invalidate_when_already_invalid_is_noop(self):
        """Invalidating an already-invalid buffer should not change state."""
        buf = _make_buffer()
        # Buffer starts invalid, set buffer to something to detect changes
        buf._buffer = "should not change"

        buf.invalidate()
        # Buffer should NOT have been cleared because cursor_pos was already -1
        assert buf._buffer == "should not change"

    def test_double_invalidation_is_safe(self):
        """Calling invalidate twice should not raise."""
        buf = _make_buffer()
        buf._cursor_pos = 3
        buf._buffer = "abc"

        buf.invalidate()
        buf.invalidate()
        assert buf.is_valid is False


# ===========================================================================
# get_context()
# ===========================================================================

class TestGetContext:
    """ShadowBufferManager.get_context() behavior."""

    def test_returns_empty_when_invalid(self):
        """Invalid buffer should return empty context."""
        buf = _make_buffer()
        ctx = buf.get_context()
        assert ctx == {'preceding_chars': '', 'has_selection': False}

    def test_preceding_two_chars(self):
        """Should return up to 2 characters before cursor."""
        buf = _make_buffer()
        buf._buffer = "hello"
        buf._cursor_pos = 5
        buf._selection_len = 0

        ctx = buf.get_context()
        assert ctx['preceding_chars'] == "lo"

    def test_preceding_one_char_at_position_one(self):
        """At position 1, should return only 1 preceding char."""
        buf = _make_buffer()
        buf._buffer = "hello"
        buf._cursor_pos = 1
        buf._selection_len = 0

        ctx = buf.get_context()
        assert ctx['preceding_chars'] == "h"

    def test_preceding_zero_chars_at_position_zero(self):
        """At position 0 (start of buffer), no preceding chars."""
        buf = _make_buffer()
        buf._buffer = "hello"
        buf._cursor_pos = 0
        buf._selection_len = 0

        ctx = buf.get_context()
        assert ctx['preceding_chars'] == ""

    def test_has_selection_false_when_no_selection(self):
        """No selection should set has_selection to False."""
        buf = _make_buffer()
        buf._buffer = "hello"
        buf._cursor_pos = 3
        buf._selection_len = 0

        ctx = buf.get_context()
        assert ctx['has_selection'] is False

    def test_has_selection_true_when_selected(self):
        """Active selection should set has_selection to True."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 6
        buf._selection_len = 5

        ctx = buf.get_context()
        assert ctx['has_selection'] is True

    def test_preceding_chars_mid_buffer(self):
        """Cursor in middle of buffer should get correct 2 chars."""
        buf = _make_buffer()
        buf._buffer = "abcdefgh"
        buf._cursor_pos = 4
        buf._selection_len = 0

        ctx = buf.get_context()
        assert ctx['preceding_chars'] == "cd"


# ===========================================================================
# update_after_insertion()
# ===========================================================================

class TestUpdateAfterInsertion:
    """ShadowBufferManager.update_after_insertion() behavior."""

    def test_noop_when_invalid(self):
        """Insertion on invalid buffer should not modify anything."""
        buf = _make_buffer()
        buf.update_after_insertion("hello")
        assert buf._buffer == ""
        assert buf._cursor_pos == -1

    def test_insert_at_cursor(self):
        """Text insertion at cursor should splice into buffer."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 5
        buf._selection_len = 0

        buf.update_after_insertion(" beautiful")
        assert buf._buffer == "hello beautiful world"

    def test_cursor_moves_after_insertion(self):
        """Cursor should advance by length of inserted text."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 5
        buf._selection_len = 0

        buf.update_after_insertion(" beautiful")
        assert buf._cursor_pos == 15  # 5 + len(" beautiful")

    def test_insert_replaces_selection(self):
        """Insertion with active selection should replace selected text."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 6  # start of "world"
        buf._selection_len = 5  # "world" selected

        buf.update_after_insertion("earth")
        assert buf._buffer == "hello earth"

    def test_selection_cleared_after_insertion(self):
        """Selection should be cleared after insertion."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 6
        buf._selection_len = 5

        buf.update_after_insertion("earth")
        assert buf._selection_len == 0

    def test_insert_empty_string(self):
        """Inserting empty string should clear selection but not change buffer text."""
        buf = _make_buffer()
        buf._buffer = "hello world"
        buf._cursor_pos = 6
        buf._selection_len = 5

        buf.update_after_insertion("")
        assert buf._buffer == "hello "
        assert buf._cursor_pos == 6
        assert buf._selection_len == 0

    def test_insert_at_start(self):
        """Inserting at position 0 should prepend text."""
        buf = _make_buffer()
        buf._buffer = "world"
        buf._cursor_pos = 0
        buf._selection_len = 0

        buf.update_after_insertion("hello ")
        assert buf._buffer == "hello world"
        assert buf._cursor_pos == 6

    def test_insert_at_end(self):
        """Inserting at end of buffer should append text."""
        buf = _make_buffer()
        buf._buffer = "hello"
        buf._cursor_pos = 5
        buf._selection_len = 0

        buf.update_after_insertion(" world")
        assert buf._buffer == "hello world"
        assert buf._cursor_pos == 11


# ===========================================================================
# update_from_clipboard_data()
# ===========================================================================

class TestUpdateFromClipboardData:
    """ShadowBufferManager.update_from_clipboard_data() behavior."""

    def test_sets_buffer_from_external_data(self):
        """Should set buffer content from provided full_text."""
        buf = _make_buffer()
        buf.update_from_clipboard_data("external text", cursor_pos=8)
        assert buf._buffer == "external text"

    def test_sets_cursor_position(self):
        """Should set cursor position from provided value."""
        buf = _make_buffer()
        buf.update_from_clipboard_data("hello", cursor_pos=3)
        assert buf._cursor_pos == 3

    def test_sets_selection_length(self):
        """Should set selection length from provided value."""
        buf = _make_buffer()
        buf.update_from_clipboard_data("hello world", cursor_pos=6, selection_len=5)
        assert buf._selection_len == 5

    def test_defaults_selection_to_zero(self):
        """Selection length should default to 0 when not specified."""
        buf = _make_buffer()
        buf.update_from_clipboard_data("hello", cursor_pos=3)
        assert buf._selection_len == 0

    def test_makes_buffer_valid(self):
        """After update_from_clipboard_data, buffer should be valid."""
        buf = _make_buffer()
        assert buf.is_valid is False
        buf.update_from_clipboard_data("text", cursor_pos=2)
        assert buf.is_valid is True

    def test_overwrites_existing_buffer(self):
        """Should completely replace previous buffer state."""
        buf = _make_buffer()
        buf._buffer = "old content"
        buf._cursor_pos = 5
        buf._selection_len = 3

        buf.update_from_clipboard_data("new content", cursor_pos=7, selection_len=0)
        assert buf._buffer == "new content"
        assert buf._cursor_pos == 7
        assert buf._selection_len == 0


# ===========================================================================
# synchronize()
# ===========================================================================

class TestSynchronize:
    """ShadowBufferManager.synchronize() with mocked UIA."""

    @patch(f"{_MOD}.auto")
    def test_no_focused_control_returns_false(self, mock_auto):
        """If no control is focused, synchronize should return False."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)
        mock_auto.GetFocusedControl.return_value = None

        buf = _make_buffer()
        result = buf.synchronize()
        assert result is False

    @patch(f"{_MOD}.auto")
    def test_no_text_pattern_returns_false(self, mock_auto):
        """If control doesn't support TextPattern, should return False."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)

        focused = MagicMock()
        focused.Name = "TestControl"
        focused.GetPattern.return_value = None
        mock_auto.GetFocusedControl.return_value = focused

        buf = _make_buffer()
        result = buf.synchronize()
        assert result is False

    @patch(f"{_MOD}.auto")
    def test_successful_sync_sets_buffer(self, mock_auto):
        """Successful synchronize should populate buffer, cursor, selection."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)
        mock_auto.TextPatternRangeEndpoint.End = "End"
        mock_auto.TextPatternRangeEndpoint.Start = "Start"

        # Set up focused control with TextPattern
        focused = MagicMock()
        text_pattern = MagicMock()
        focused.GetPattern.return_value = text_pattern

        # DocumentRange returns full text
        doc_range = MagicMock()
        doc_range.GetText.return_value = "hello world"
        text_pattern.DocumentRange = doc_range

        # Selection range: "world" is selected (cursor at 6, selection of 5)
        sel_range = MagicMock()
        sel_range.GetText.return_value = "world"
        text_pattern.GetSelection.return_value = [sel_range]

        # Clone for cursor position calculation
        cursor_range = MagicMock()
        cursor_range.GetText.return_value = "hello "  # 6 chars before selection
        doc_range.Clone.return_value = cursor_range

        mock_auto.GetFocusedControl.return_value = focused

        buf = _make_buffer()
        result = buf.synchronize()

        assert result is True
        assert buf._buffer == "hello world"
        assert buf._cursor_pos == 6
        assert buf._selection_len == 5
        assert buf.is_valid is True

    @patch(f"{_MOD}.auto")
    def test_com_error_on_get_selection_returns_false(self, mock_auto):
        """COMError during GetSelection should return False.

        The inner try/except catches _ctypes.COMError on GetSelection.
        We import the real _ctypes.COMError to raise it, which the production
        code catches.
        """
        import _ctypes as real_ctypes

        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)

        focused = MagicMock()
        text_pattern = MagicMock()
        focused.GetPattern.return_value = text_pattern
        text_pattern.DocumentRange = MagicMock()

        # GetSelection raises real COMError so the except clause catches it
        text_pattern.GetSelection.side_effect = real_ctypes.COMError(-2147418113, "operation failed", None)

        mock_auto.GetFocusedControl.return_value = focused

        buf = _make_buffer()
        result = buf.synchronize()
        assert result is False

    @patch(f"{_MOD}.auto")
    def test_empty_selection_ranges_returns_false(self, mock_auto):
        """Empty selection ranges list should return False."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)

        focused = MagicMock()
        text_pattern = MagicMock()
        focused.GetPattern.return_value = text_pattern
        text_pattern.DocumentRange = MagicMock()
        text_pattern.GetSelection.return_value = []  # Empty list

        mock_auto.GetFocusedControl.return_value = focused

        buf = _make_buffer()
        result = buf.synchronize()
        assert result is False

    @patch(f"{_MOD}.auto")
    def test_general_exception_invalidates_and_returns_false(self, mock_auto):
        """General exception during sync should invalidate buffer and return False."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)
        mock_auto.GetFocusedControl.side_effect = RuntimeError("unexpected failure")

        buf = _make_buffer()
        # Pre-populate buffer to verify invalidation
        buf._cursor_pos = 5
        buf._buffer = "test"

        result = buf.synchronize()
        assert result is False
        assert buf.is_valid is False
        assert buf._buffer == ""

    @patch(f"{_MOD}.auto")
    def test_sync_with_no_selection_text(self, mock_auto):
        """Sync with cursor but no selected text (0-length selection)."""
        mock_auto.UIAutomationInitializerInThread.return_value.__enter__ = MagicMock()
        mock_auto.UIAutomationInitializerInThread.return_value.__exit__ = MagicMock(return_value=False)
        mock_auto.TextPatternRangeEndpoint.End = "End"
        mock_auto.TextPatternRangeEndpoint.Start = "Start"

        focused = MagicMock()
        text_pattern = MagicMock()
        focused.GetPattern.return_value = text_pattern

        doc_range = MagicMock()
        doc_range.GetText.return_value = "hello"
        text_pattern.DocumentRange = doc_range

        sel_range = MagicMock()
        sel_range.GetText.return_value = ""  # No selection
        text_pattern.GetSelection.return_value = [sel_range]

        cursor_range = MagicMock()
        cursor_range.GetText.return_value = "hel"  # Cursor at position 3
        doc_range.Clone.return_value = cursor_range

        mock_auto.GetFocusedControl.return_value = focused

        buf = _make_buffer()
        result = buf.synchronize()

        assert result is True
        assert buf._buffer == "hello"
        assert buf._cursor_pos == 3
        assert buf._selection_len == 0


# ===========================================================================
# Adversarial / Edge Cases
# ===========================================================================

class TestAdversarial:
    """Adversarial scenarios and edge cases."""

    def test_get_context_then_insert_then_get_context(self):
        """Context should reflect state after insertion."""
        buf = _make_buffer()
        buf._buffer = "ab"
        buf._cursor_pos = 2
        buf._selection_len = 0

        ctx1 = buf.get_context()
        assert ctx1['preceding_chars'] == "ab"

        buf.update_after_insertion("cd")

        ctx2 = buf.get_context()
        assert ctx2['preceding_chars'] == "cd"
        assert buf._buffer == "abcd"

    def test_replace_entire_selection(self):
        """Replacing entire buffer content via selection."""
        buf = _make_buffer()
        buf._buffer = "old text"
        buf._cursor_pos = 0
        buf._selection_len = 8  # Entire buffer selected

        buf.update_after_insertion("new")
        assert buf._buffer == "new"
        assert buf._cursor_pos == 3
        assert buf._selection_len == 0

    def test_update_from_clipboard_then_invalidate(self):
        """Clipboard update then invalidation should reset everything."""
        buf = _make_buffer()
        buf.update_from_clipboard_data("content", cursor_pos=4)
        assert buf.is_valid is True

        buf.invalidate()
        assert buf.is_valid is False
        assert buf._buffer == ""

    def test_successive_insertions(self):
        """Multiple insertions should accumulate correctly."""
        buf = _make_buffer()
        buf._buffer = ""
        buf._cursor_pos = 0
        buf._selection_len = 0

        buf.update_after_insertion("hello")
        assert buf._buffer == "hello"
        assert buf._cursor_pos == 5

        buf.update_after_insertion(" ")
        assert buf._buffer == "hello "
        assert buf._cursor_pos == 6

        buf.update_after_insertion("world")
        assert buf._buffer == "hello world"
        assert buf._cursor_pos == 11

    def test_unicode_text_handling(self):
        """Unicode content should work correctly."""
        buf = _make_buffer()
        buf._buffer = "cafe"
        buf._cursor_pos = 4
        buf._selection_len = 0

        buf.update_after_insertion(" latte")
        assert buf._buffer == "cafe latte"
        ctx = buf.get_context()
        assert ctx['preceding_chars'] == "te"

    def test_multiline_text(self):
        """Newlines in buffer should be handled as regular characters."""
        buf = _make_buffer()
        buf._buffer = "line1\nline2"
        buf._cursor_pos = 6  # Start of "line2"
        buf._selection_len = 0

        ctx = buf.get_context()
        # buf[max(0, 6-2):6] = buf[4:6] = "1\n"
        assert ctx['preceding_chars'] == "1\n"

    def test_insert_multiline_text(self):
        """Inserting text with newlines should work."""
        buf = _make_buffer()
        buf._buffer = "start end"
        buf._cursor_pos = 6
        buf._selection_len = 0

        buf.update_after_insertion("line1\nline2\n")
        assert buf._buffer == "start line1\nline2\nend"
        assert buf._cursor_pos == 18  # 6 + len("line1\nline2\n")
