"""Tests for UIActionHandler capture/replace clipboard methods.

Tests capture_selected_text() and replace_selected_text() -- the Input Process
side of the AI "fix this" flow. These methods use the sentinel clipboard pattern
proven in transform_selection() to capture and replace text via clipboard IPC.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch, call

import pytest


# Patch paths
_MOD = "ui.ui_action_handler"


def _make_config(**overrides):
    """Build a minimal config dict for UIActionHandler."""
    cfg = {
        "ui_actions": {
            "timing": {
                "utterance_clipboard_timeout_seconds": 1.0,
                "clipboard_verification_timeout_ms": 250,
            }
        }
    }
    cfg.update(overrides)
    return cfg


@contextmanager
def _noop_clipboard_context(**kwargs):
    """No-op replacement for clipboard_context in tests."""
    yield


@pytest.fixture
def handler():
    """Create a UIActionHandler with specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations") as MockCO, \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"), \
         patch(f"{_MOD}.clipboard_context", side_effect=_noop_clipboard_context):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(response_queue=q, config=_make_config())
        # Set clipboard verification timeout on the mock
        h.clipboard.clipboard_verification_timeout = 0.01  # fast for tests
        yield h


# =========================================================================
# capture_selected_text
# =========================================================================

class TestCaptureSelectedText:
    """Tests for UIActionHandler.capture_selected_text()."""

    def test_returns_selected_text(self, handler):
        """When text is selected, Ctrl+C copies it and it's returned."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time:
            # Sentinel first, then selected text appears after Ctrl+C
            mock_clip.paste.side_effect = ["selected text"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            assert result["text"] == "selected text"

    def test_ctrl_c_sent(self, handler):
        """Ctrl+C is sent to copy the current selection."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.time") as mock_time:
            mock_clip.paste.side_effect = ["captured"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            handler.capture_selected_text()

            # Should have called press_keys with ctrl+c
            mock_keys.assert_any_call('ctrl', 'c')

    def test_no_selection_selects_all(self, handler):
        """When no text is selected (sentinel unchanged), Ctrl+A then Ctrl+C."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.time") as mock_time:
            sentinel = None  # Will be set by copy()

            def track_copy(text):
                nonlocal sentinel
                sentinel = text

            # wh-fz7j.4: capture_selected_text now routes its sentinel write
            # through self.clipboard._safe_copy. Forward the side effect so
            # the test's sentinel tracking and mock_clip.paste polling still work.
            handler.clipboard._safe_copy.side_effect = lambda t: (track_copy(t) or True)
            mock_clip.copy.side_effect = track_copy

            # First poll: sentinel unchanged (no selection)
            # After timeout, Ctrl+A+C, then poll returns text
            call_count = [0]

            def paste_side_effect():
                call_count[0] += 1
                if call_count[0] <= 3:
                    return sentinel  # Still sentinel (no selection)
                return "all text"  # After Ctrl+A, text appears

            mock_clip.paste.side_effect = paste_side_effect
            # Time progression: enough for first poll to timeout, then second succeeds
            mock_time.time.side_effect = [
                1000.0,  # sentinel creation
                1000.0, 1000.1, 1001.0,  # first poll: start, check, timeout
                1001.0, 1001.0, 1001.0,  # second poll: start, check, found
            ]
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            # Should have sent Ctrl+A before second Ctrl+C attempt
            assert call('ctrl', 'a') in mock_keys.call_args_list
            assert result["text"] == "all text"

    def test_no_text_anywhere_returns_empty(self, handler):
        """When even select-all yields nothing, return empty text."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time:
            sentinel = None

            def track_copy(text):
                nonlocal sentinel
                sentinel = text

            mock_clip.copy.side_effect = track_copy
            # Always returns sentinel -- no text in application at all
            mock_clip.paste.side_effect = lambda: sentinel
            # Time: both polls timeout
            time_values = [1000.0]  # sentinel creation
            time_values.extend([1000.0 + i * 0.5 for i in range(20)])  # all timeout
            mock_time.time.side_effect = time_values
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            assert result["text"] == ""

    def test_uses_clipboard_context(self, handler):
        """Clipboard save/restore via clipboard_context."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time, \
             patch(f"{_MOD}.clipboard_context") as mock_ctx:
            mock_ctx.return_value.__enter__ = Mock(return_value=None)
            mock_ctx.return_value.__exit__ = Mock(return_value=False)
            mock_clip.paste.side_effect = ["text"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            handler.capture_selected_text()

            mock_ctx.assert_called_once()


# =========================================================================
# replace_selected_text
# =========================================================================

class TestReplaceSelectedText:
    """Tests for UIActionHandler.replace_selected_text()."""

    def test_sets_clipboard_and_pastes(self, handler):
        """Text is placed on clipboard and Ctrl+V is sent."""
        with patch(f"{_MOD}.pyperclip") as _mock_clip, \
             patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()

            # wh-fz7j.4: replace_selected_text now routes the clipboard
            # write through self.clipboard._safe_copy. Configure the mock
            # so we can assert on it.
            handler.clipboard._safe_copy.return_value = True

            result = handler.replace_selected_text(text="corrected text")

            handler.clipboard._safe_copy.assert_called_with("corrected text")
            mock_keys.assert_any_call('ctrl', 'v')
            assert result["success"] is True

    def test_uses_clipboard_context(self, handler):
        """Clipboard is saved and restored around the paste."""
        with patch(f"{_MOD}.pyperclip"), \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time, \
             patch(f"{_MOD}.clipboard_context") as mock_ctx:
            mock_ctx.return_value.__enter__ = Mock(return_value=None)
            mock_ctx.return_value.__exit__ = Mock(return_value=False)
            mock_time.sleep = Mock()

            handler.replace_selected_text(text="new text")

            mock_ctx.assert_called_once()

    def test_returns_success(self, handler):
        """Returns success dict for IPC response."""
        with patch(f"{_MOD}.pyperclip"), \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()

            result = handler.replace_selected_text(text="hello")

            assert isinstance(result, dict)
            assert result["success"] is True

    def test_invalidates_buffer(self, handler):
        """Buffer is invalidated since text content changed."""
        with patch(f"{_MOD}.pyperclip"), \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()

            handler.replace_selected_text(text="replaced")

            handler.buffer_manager.invalidate.assert_called()
