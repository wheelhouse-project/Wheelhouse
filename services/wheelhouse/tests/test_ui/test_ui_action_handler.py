"""Tests for UIActionHandler - central coordinator for UI interactions.

Covers:
- Construction and specialist component initialization
- Buffer management (invalidation)
- Utterance lifecycle (start/end, clipboard skip)
- Letter buffering and auto-compression
- intelligent_insert_text dispatch (letter buffer, terminal, router)
- transform_selection (selection detection, transformation, paste-back)
- wrap_or_insert (selection wrap, text wrap, empty delimiters)
- press_key_action / hotkey_action (key dispatch, Flutter, repeat)
- _convert_to_sendkeys_format (modifier/special/single char mapping)
- show_notification (valid/invalid params, import failure)
- Adversarial: missing fields, unexpected kwargs, exception resilience
"""
import logging

import pytest
import time
from unittest.mock import MagicMock, Mock, patch

from ui.context import UIContext
from ui.strategies.base import InsertionResult


def _ok(clipboard_dirty: bool = True) -> InsertionResult:
    """InsertionResult shorthand for ok-paths in handler tests."""
    return InsertionResult(success=True, clipboard_dirty=clipboard_dirty)


def _fail(clipboard_dirty: bool = True) -> InsertionResult:
    """InsertionResult shorthand for failed-paths in handler tests."""
    return InsertionResult(success=False, clipboard_dirty=clipboard_dirty)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    """Build a minimal config dict for UIActionHandler."""
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
    """Build a UIContext for tests."""
    return UIContext(
        focused_control=focused_control,
        is_flutter=is_flutter,
        is_terminal=is_terminal,
        process_name=process_name,
        class_name=class_name,
    )


# Patch paths for the module-under-test
_MOD = "ui.ui_action_handler"


# ---------------------------------------------------------------------------
# Fixture: fully-mocked UIActionHandler
# ---------------------------------------------------------------------------

@pytest.fixture
def handler():
    """Create a UIActionHandler with all specialist components mocked."""
    # Strategy classes are NOT patched because some are still used in
    # isinstance() checks in the production code (SimplePasteStrategy,
    # ClipboardOnlyStrategy). Patching them would replace the class
    # with a MagicMock and break the isinstance branches.
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

        # Expose mocks for assertions
        h._mock_text_perfector_cls = MockTP
        h._mock_clipboard_ops_cls = MockCO
        h._mock_window_mgr_cls = MockWFM
        h._mock_selection_xfm_cls = MockST
        h._mock_utterance_mgr_cls = MockUCM
        h._mock_buffer_mgr_cls = MockSBM
        h._mock_terminal_editor_cls = MockTDE
        h._mock_router_cls = MockRouter

        yield h


# ============================================================================
# Construction and Initialization
# ============================================================================

class TestConstruction:
    """UIActionHandler.__init__ wiring tests."""

    def test_response_queue_stored(self, handler):
        """Response queue should be stored on the instance."""
        assert handler.response_queue is not None

    def test_config_stored(self, handler):
        """Config dict should be stored on the instance."""
        assert isinstance(handler.config, dict)

    def test_specialist_components_created(self, handler):
        """All specialist components should be instantiated."""
        assert handler.text_perfector is not None
        assert handler.clipboard is not None
        assert handler.window_manager is not None
        assert handler.selection_transformer is not None
        assert handler.utterance_manager is not None
        assert handler.buffer_manager is not None
        assert handler.terminal_editor is not None

    def test_strategies_created(self, handler):
        """All wired strategies should be instantiated."""
        assert handler.standard_strategy is not None
        assert handler.flutter_strategy is not None
        assert handler.simple_paste_strategy is not None

    def test_router_created(self, handler):
        """InsertionRouter should be instantiated with strategies."""
        assert handler.router is not None

    def test_letter_buffer_starts_empty(self, handler):
        """Letter buffer should be empty on construction."""
        assert handler._letter_buffer == []

    def test_utterance_timeout_from_config(self):
        """Utterance timeout should be read from config."""
        cfg = _make_config()
        cfg["ui_actions"]["timing"]["utterance_clipboard_timeout_seconds"] = 5.0

        with patch(f"{_MOD}.TextPerfector"), \
             patch(f"{_MOD}.ClipboardOperations"), \
             patch(f"{_MOD}.WindowFocusManager"), \
             patch(f"{_MOD}.SelectionTransformer"), \
             patch(f"{_MOD}.UtteranceClipboardManager") as MockUCM, \
             patch(f"{_MOD}.ShadowBufferManager"), \
             patch(f"{_MOD}.TerminalEditorProxy"), \
             patch(f"{_MOD}.StandardStrategy"), \
             patch(f"{_MOD}.FlutterStrategy"), \
             patch(f"{_MOD}.SimplePasteStrategy"), \
             patch(f"{_MOD}.InsertionRouter"):

            from ui.ui_action_handler import UIActionHandler
            UIActionHandler(response_queue=MagicMock(), config=cfg)
            MockUCM.assert_called_once_with(timeout_seconds=5.0)

    def test_missing_timing_config_uses_default(self):
        """Missing timing config should fall back to 1.0s default."""
        cfg = {}  # No ui_actions key at all

        with patch(f"{_MOD}.TextPerfector"), \
             patch(f"{_MOD}.ClipboardOperations"), \
             patch(f"{_MOD}.WindowFocusManager"), \
             patch(f"{_MOD}.SelectionTransformer"), \
             patch(f"{_MOD}.UtteranceClipboardManager") as MockUCM, \
             patch(f"{_MOD}.ShadowBufferManager"), \
             patch(f"{_MOD}.TerminalEditorProxy"), \
             patch(f"{_MOD}.StandardStrategy"), \
             patch(f"{_MOD}.FlutterStrategy"), \
             patch(f"{_MOD}.SimplePasteStrategy"), \
             patch(f"{_MOD}.InsertionRouter"):

            from ui.ui_action_handler import UIActionHandler
            UIActionHandler(response_queue=MagicMock(), config=cfg)
            MockUCM.assert_called_once_with(timeout_seconds=1.0)


# ============================================================================
# Buffer Management
# ============================================================================

class TestBufferManagement:
    """invalidate_buffer: simplified buffer invalidation."""

    def test_invalidate_always_invalidates_shadow_buffer(self, handler):
        """Shadow buffer should be invalidated regardless of source."""
        handler.invalidate_buffer(source="keyboard:a")
        handler.buffer_manager.invalidate.assert_called_once()

    def test_invalidate_sets_user_interacted_flag(self, handler):
        """User interaction flag should be set on invalidate."""
        handler.invalidate_buffer(source="mouse:left")
        assert handler._user_interacted_during_utterance is True


# ============================================================================
# Utterance Lifecycle
# ============================================================================

class TestUtteranceLifecycle:
    """start_utterance, end_utterance, skip/clear clipboard restore."""

    def test_start_utterance_delegates(self, handler):
        """start_utterance should delegate to utterance_manager."""
        handler.start_utterance(42)
        handler.utterance_manager.start_utterance.assert_called_once_with(42)

    def test_end_utterance_delegates(self, handler):
        """end_utterance should delegate to utterance_manager."""
        handler._letter_buffer = []  # ensure flush is a no-op
        handler.end_utterance(42)
        handler.utterance_manager.end_utterance.assert_called_once_with(42)

    def test_end_utterance_without_id(self, handler):
        """end_utterance with no ID should pass None."""
        handler._letter_buffer = []
        handler.end_utterance()
        handler.utterance_manager.end_utterance.assert_called_once_with(None)

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="abc")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_end_utterance_flushes_letter_buffer(self, mock_cc, mock_ctx, mock_compress, handler):
        """end_utterance should flush any buffered letters before ending."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        handler._letter_buffer = ["a", "b", "c"]
        handler.end_utterance(1)

        # Buffer should be cleared
        assert handler._letter_buffer == []
        # auto_compress should have been called
        mock_compress.assert_called_once_with("a b c")

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="abc")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_letter_buffer_flush_log_redacts_by_default(
        self, mock_cc, mock_ctx, mock_compress, handler, caplog, monkeypatch
    ):
        """wh-797.17.1: the INFO flush line is the spelled-letters path --
        the exact way a user spells a password. With transcript logging
        off (the release default) it must carry placeholders, never the
        letters or the compressed word."""
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        handler._letter_buffer = ["a", "b", "c"]
        with caplog.at_level(logging.INFO):
            handler.end_utterance(1)

        flush_lines = [
            r.getMessage() for r in caplog.records
            if "[LETTER_BUFFER] Flushing" in r.getMessage()
        ]
        assert len(flush_lines) == 1
        assert "a b c" not in flush_lines[0]
        assert "abc" not in flush_lines[0]
        assert "redacted" in flush_lines[0]

    def test_skip_clipboard_restore_enables(self, handler):
        """skip_clipboard_restore(True) should delegate to utterance_manager."""
        handler.skip_clipboard_restore(enable=True)
        handler.utterance_manager.skip_clipboard_restore.assert_called_once()

    def test_skip_clipboard_restore_false_does_nothing(self, handler):
        """skip_clipboard_restore(False) should NOT call skip_clipboard_restore."""
        handler.skip_clipboard_restore(enable=False)
        handler.utterance_manager.skip_clipboard_restore.assert_not_called()

    def test_clear_skip_clipboard_restore(self, handler):
        """clear_skip_clipboard_restore should delegate to utterance_manager."""
        handler.clear_skip_clipboard_restore()
        handler.utterance_manager.clear_skip_flag.assert_called_once()

    def test_start_utterance_ignores_extra_kwargs(self, handler):
        """start_utterance should accept and ignore extra kwargs."""
        handler.start_utterance(1, foo="bar", baz=42)
        handler.utterance_manager.start_utterance.assert_called_once_with(1)

    def test_end_utterance_ignores_extra_kwargs(self, handler):
        """end_utterance should accept and ignore extra kwargs."""
        handler._letter_buffer = []
        handler.end_utterance(1, foo="bar")
        handler.utterance_manager.end_utterance.assert_called_once_with(1)


# ============================================================================
# Retraction: letter-buffer log redaction
# ============================================================================

class TestRetractLetterBufferRedaction:
    """wh-797.21.1: retract()'s buffered-letters branch is the same
    spelled-letters path as the flush line -- the exact way a user spells
    a password. With transcript logging off (the release default) the
    'Retracting buffered letters' line must not carry the letters."""

    def test_retract_buffered_letters_log_redacts_by_default(
        self, handler, caplog, monkeypatch
    ):
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        # Clear every fail-closed gate ahead of the buffered-letters
        # branch: no user interaction, no SimplePaste, verified paste,
        # no remembered HWND (skips focus verification), zero pasted
        # chars, letters still queued.
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.last_paste_was_optimistic = False
        handler.window_manager._last_target_hwnd = None
        handler.clipboard.accumulated_paste_was_qt = False
        handler.clipboard.accumulated_paste_chars = 0
        # q/j/x do not occur in the static message text or the
        # redaction placeholder, so their absence proves redaction.
        handler._letter_buffer = ["q", "j", "x"]

        with caplog.at_level(logging.INFO):
            result = handler.retract()

        assert result["status"] == "retracted"
        assert result["reason"] == "letter_buffer_cleared"
        retract_lines = [
            r.getMessage() for r in caplog.records
            if "Retracting buffered letters" in r.getMessage()
        ]
        assert len(retract_lines) == 1
        assert "q" not in retract_lines[0]
        assert "j" not in retract_lines[0]
        assert "x" not in retract_lines[0]
        assert "redacted" in retract_lines[0]



# ============================================================================
# Letter Buffering
# ============================================================================

class TestLetterBuffering:
    """_is_single_letter and letter buffer logic."""

    def test_single_letter_lowercase(self, handler):
        """Single lowercase letter should be recognized."""
        assert handler._is_single_letter("a") is True

    def test_single_letter_uppercase(self, handler):
        """Single uppercase letter should be recognized."""
        assert handler._is_single_letter("Z") is True

    def test_digit_not_letter(self, handler):
        """Single digit should NOT be a letter."""
        assert handler._is_single_letter("5") is False

    def test_multi_char_not_letter(self, handler):
        """Multiple characters should NOT be a single letter."""
        assert handler._is_single_letter("ab") is False

    def test_empty_string_not_letter(self, handler):
        """Empty string should NOT be a single letter."""
        assert handler._is_single_letter("") is False

    def test_space_not_letter(self, handler):
        """Space should NOT be a single letter."""
        assert handler._is_single_letter(" ") is False

    def test_punctuation_not_letter(self, handler):
        """Punctuation should NOT be a single letter."""
        assert handler._is_single_letter("!") is False

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="hello")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_flush_applies_compression(self, mock_cc, mock_ctx, mock_compress, handler):
        """_flush_letter_buffer should apply auto_compress_spelled_letters."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        handler._letter_buffer = ["h", "e", "l", "l", "o"]
        handler._flush_letter_buffer()

        mock_compress.assert_called_once_with("h e l l o")
        assert handler._letter_buffer == []

    def test_flush_empty_buffer_does_nothing(self, handler):
        """Flushing empty buffer should be a no-op."""
        handler._letter_buffer = []
        handler._flush_letter_buffer()  # Should not raise


# ============================================================================
# intelligent_insert_text - Dispatch
# ============================================================================

class TestIntelligentInsertText:
    """intelligent_insert_text dispatch and letter buffering."""

    def test_single_letter_buffered(self, handler):
        """Single letter should be buffered, not immediately inserted."""
        handler.intelligent_insert_text("a", request_id="r1")

        assert handler._letter_buffer == ["a"]
        # Schema A success emitted immediately for single letters so the caller
        # does not block waiting for deferred flush (wh-lla5d).
        handler.response_queue.put.assert_called_once()
        msg = handler.response_queue.put.call_args[0][0]
        assert msg["request_id"] == "r1"
        assert msg["status"] == "ok"
        assert msg["action"] == "intelligent_insert_text"

    def test_single_letter_no_request_id(self, handler):
        """Single letter without request_id should not send ACK."""
        handler.intelligent_insert_text("b")

        assert handler._letter_buffer == ["b"]
        handler.response_queue.put.assert_not_called()

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="cat")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_non_letter_flushes_buffer_first(self, mock_cc, mock_ctx, mock_compress, handler):
        """Non-single-letter word should flush buffered letters first."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.terminal_editor.is_active = False
        handler.router.get_strategy.return_value = MagicMock()

        handler._letter_buffer = ["c", "a", "t"]
        handler.intelligent_insert_text("hello", request_id="r2")

        # Buffer should have been flushed
        assert handler._letter_buffer == []
        mock_compress.assert_called_once_with("c a t")

    @patch(f"{_MOD}.capture_context")
    def test_router_selects_strategy(self, mock_ctx, handler):
        """Should use router to select strategy and call insert."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r4")

        handler.router.get_strategy.assert_called_once_with(ctx, "hello")
        mock_strategy.insert.assert_called_once_with("hello", ctx, "r4", None)

    @patch(f"{_MOD}.capture_context")
    def test_response_sent_for_non_terminal_strategy(self, mock_ctx, handler):
        """Schema A success response should be sent for non-terminal strategy."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("word", request_id="r5")

        handler.response_queue.put.assert_called_once()
        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("type") != "ack"
        assert msg["request_id"] == "r5"
        assert msg["status"] == "ok"
        assert msg["action"] == "intelligent_insert_text"

    # -- wh-lla5d regression tests: Schema A, exactly one response per request --

    def test_single_letter_emits_schema_a_success_once(self, handler):
        """Single-letter path emits exactly one Schema A success (wh-lla5d)."""
        handler.intelligent_insert_text("a", request_id="r-letter")

        assert handler.response_queue.put.call_count == 1
        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("type") != "ack"
        assert msg["request_id"] == "r-letter"
        assert msg["status"] == "ok"
        assert msg["action"] == "intelligent_insert_text"
        assert "path" in msg

    @patch(f"{_MOD}.capture_context")
    def test_standard_strategy_emits_schema_a_success_once(self, mock_ctx, handler):
        """Non-terminal strategy path emits exactly one Schema A success (wh-lla5d)."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r-std")

        assert handler.response_queue.put.call_count == 1
        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("type") != "ack"
        assert msg["request_id"] == "r-std"
        assert msg["status"] == "ok"
        assert msg["action"] == "intelligent_insert_text"

    @patch(f"{_MOD}.capture_context")
    def test_strategy_returns_false_emits_schema_a_error(self, mock_ctx, handler):
        """wh-d43oi: A False return from strategy.insert must produce a
        Schema A error (not a silent success)."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _fail()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("word", request_id="r-false")

        assert handler.response_queue.put.call_count == 1
        msg = handler.response_queue.put.call_args[0][0]
        assert msg["request_id"] == "r-false"
        assert msg.get("error") is True
        assert msg["action"] == "intelligent_insert_text"

    @patch(f"{_MOD}.capture_context")
    def test_strategy_returns_true_emits_insert_verified(self, mock_ctx, handler):
        """wh-d43oi: A True return from strategy.insert must produce a
        Schema A success with path='insert_verified'."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("word", request_id="r-true")

        assert handler.response_queue.put.call_count == 1
        msg = handler.response_queue.put.call_args[0][0]
        assert msg["request_id"] == "r-true"
        assert msg["status"] == "ok"
        assert msg["path"] == "insert_verified"
        assert msg["action"] == "intelligent_insert_text"

    @patch(f"{_MOD}.capture_context")
    def test_exception_emits_schema_a_error(self, mock_ctx, handler):
        """Exception during insert emits one Schema A error response (wh-lla5d)."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        handler.router.get_strategy.side_effect = RuntimeError("strategy failure")

        handler.intelligent_insert_text("word", request_id="r-err")

        assert handler.response_queue.put.call_count == 1
        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("type") != "ack"
        assert msg["request_id"] == "r-err"
        assert msg.get("error") is True
        assert msg["action"] == "intelligent_insert_text"

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_outside_utterance_uses_clipboard_context(self, mock_cc, mock_ctx, handler):
        """Outside utterance, should wrap in clipboard_context."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = False
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("word")

        mock_cc.assert_called_once_with(restore_delay=0.05)

    @patch(f"{_MOD}.capture_context")
    def test_exception_in_insert_logged_not_raised(self, mock_ctx, handler):
        """Exceptions during insert should be logged but not raised."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        handler.router.get_strategy.side_effect = RuntimeError("boom")

        # Should not raise
        handler.intelligent_insert_text("word", request_id="r7")

    def test_window_manager_remembers_target(self, handler):
        """Should remember focused control for focus restoration."""
        with patch(f"{_MOD}.capture_context") as mock_ctx:
            fc = MagicMock()
            ctx = _make_context(focused_control=fc)
            mock_ctx.return_value = ctx
            handler.terminal_editor.is_active = False
            handler.utterance_manager.is_in_utterance.return_value = True
            handler.utterance_manager._clipboard_dirty = False
            handler.utterance_manager._last_paste_time = 0.0

            mock_strategy = MagicMock()
            handler.router.get_strategy.return_value = mock_strategy

            handler.intelligent_insert_text("word")

            handler.window_manager.remember_target.assert_called_once_with(fc)


# ============================================================================
# Clipboard Dirty Forwarding (wh-4z4g9, wh-606yk)
# ============================================================================

class TestClipboardDirtyForwarding:
    """The handler routes InsertionResult.clipboard_dirty to the
    UtteranceClipboardManager so a Unicode-only insert leaves the
    clipboard untouched at end_utterance.
    """

    @patch(f"{_MOD}.capture_context")
    def test_unicode_strategy_does_not_mark_dirty(self, mock_ctx, handler):
        """A strategy that returns clipboard_dirty=False does not call
        mark_clipboard_dirty on the manager."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r-uni")

        handler.utterance_manager.mark_clipboard_dirty.assert_not_called()

    @patch(f"{_MOD}.capture_context")
    def test_clipboard_strategy_marks_dirty(self, mock_ctx, handler):
        """A strategy that returns clipboard_dirty=True calls
        mark_clipboard_dirty on the manager."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r-clip")

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    def test_failure_with_clipboard_dirty_still_marks_dirty(self, mock_ctx, handler):
        """A failed clipboard paste can still leave dictated text on the
        clipboard, so the handler must still mark dirty."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _fail(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r-fail")

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    def test_pre_send_failure_does_not_mark_dirty(self, mock_ctx, handler):
        """A failure before any clipboard write (clipboard_dirty=False)
        leaves the dirty flag alone."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _fail(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello", request_id="r-presend")

        handler.utterance_manager.mark_clipboard_dirty.assert_not_called()

    @patch(f"{_MOD}.capture_context")
    def test_router_receives_insertion_string(self, mock_ctx, handler):
        """wh-606yk: the handler hands insertion_string to the router so
        the Unicode-vs-Standard length check can run."""
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("hello world", request_id="r-rt")

        handler.router.get_strategy.assert_called_once_with(ctx, "hello world")

    def test_raw_insert_text_marks_dirty(self, handler):
        """raw_insert_text writes the clipboard via the strategy router, so
        it must mark dirty when the strategy reports clipboard_dirty=True
        (wh-4z4g9 acceptance, wh-fsov0 routing)."""
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("RAWTEXT")

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once()
        # Strategy received VERBATIM mode.
        opts = mock_strategy.insert.call_args.args[3]
        from ui.strategies.base import InsertionMode
        assert opts.mode is InsertionMode.VERBATIM


    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="Hello World")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_transform_selection_marks_dirty(
        self, mock_ctx, mock_cc, mock_pk, mock_paste, mock_copy, handler
    ):
        """wh-r7al.2: transform_selection writes the system clipboard
        (sentinel and pasted-back transformed text). Even though the
        inner clipboard_context normally restores the saved value, its
        restore step swallows exceptions on failure -- mark dirty so the
        utterance-level restore acts as a safety net.
        """
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.selection_transformer.apply_transformation.return_value = "hello_world"
        handler.clipboard.verified_paste.return_value = True
        handler.clipboard.clipboard_verification_timeout = 0.1

        handler.transform_selection("snake_case", request_id="r-tx")

        handler.utterance_manager.mark_clipboard_dirty.assert_called()

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_wrap_or_insert_selection_branch_marks_dirty(
        self, mock_ctx, mock_cc, mock_pk, mock_paste, mock_copy, handler
    ):
        """wh-r7al.2: the selection-wrap branch of wrap_or_insert writes
        the system clipboard via Ctrl+C and verified_paste. Mark dirty
        so end_utterance can restore the original clipboard if the
        inner clipboard_context's restore step fails.
        """
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.verified_paste.return_value = True
        handler.clipboard.clipboard_verification_timeout = 0.05

        # Force the selection-wrap branch:
        # text="" so text_stripped is empty, _last_paste_time=0 so the
        # 5s recent-paste guard passes, and pyperclip.paste returns
        # different values on consecutive calls so the polling loop
        # detects a "selection changed".
        handler.utterance_manager._last_paste_time = 0.0
        mock_paste.side_effect = ["original_clipboard", "selected_text"]

        handler.wrap_or_insert("(", ")", text="", request_id="r-wrap")

        handler.utterance_manager.mark_clipboard_dirty.assert_called()


class TestRawInsertTextStrategyTracking:
    """wh-bkge.1: raw_insert_text must record _used_simple_paste after
    strategy.insert so retract's simple-paste fail-closed gate applies
    to raw inserts the same way it applies to intelligent inserts.
    """

    def test_simple_paste_strategy_sets_used_simple_flag(self, handler):
        from ui.strategies.specific import SimplePasteStrategy

        mock_strategy = MagicMock(spec=SimplePasteStrategy)
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=None)
            handler.raw_insert_text("text")

        assert handler._used_simple_paste is True

    def test_other_strategy_does_not_set_simple_flag(self, handler):
        # A generic strategy (not Simple, not ClipboardOnly) leaves the
        # simple-paste flag untouched. handler fixture initialises it to
        # False.
        mock_strategy = MagicMock()  # plain mock, no spec
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("text")

        assert handler._used_simple_paste is False


class TestRawInsertTextSlowTargetRegression:
    """wh-qoyk9: regression test for the slow-target 'insert <text>' race
    the parent epic targets.

    Pre-fix raw_insert_text wrapped verified_paste in
    clipboard_context(restore_delay=0.05) which restored the user's
    clipboard 50 ms after Ctrl+V. A slow destination application that
    consumed Ctrl+V after that 50 ms window would paste the user's
    original clipboard contents instead of the dictated text -- the
    exact race captured in T-17773447228 ('insert w' -> github URL)
    and T-17773098055 ('subscriptions' -> wh-oe7u).

    Post-fix: raw_insert_text drops clipboard_context entirely. The
    deferred-restore mechanism in UtteranceClipboardManager schedules
    a PendingRestore at end_utterance (default 300 ms deferral, with
    a Win32 clipboard sequence number ownership check at fire time).
    A slow target that consumes Ctrl+V at any time before the deferred
    fire still sees the dictated text on the clipboard.
    """

    def test_no_synchronous_clipboard_context_wrapper(self, handler):
        """Regression guard: raw_insert_text must NOT enter
        clipboard_context. The pre-fix synchronous 50 ms restore was the
        source of the slow-target leak.
        """
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context") as mock_cc:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("dictated text")

        mock_cc.assert_not_called()

    def test_clipboard_dirty_propagates_write_seq_to_manager(self, handler):
        """Regression guard: the deferred-restore ownership check needs
        the post-write seq captured inside _safe_copy. raw_insert_text
        must read self.clipboard.last_clipboard_write_seq after the
        strategy returns and pass it as write_seq to mark_clipboard_dirty.
        Without this, a manual user copy between the strategy's clipboard
        write and end_utterance could be adopted as the WheelHouse
        baseline and overwritten by the deferred restore.

        Models the production sequence: the handler resets
        last_clipboard_write_seq at entry; the strategy's _safe_copy
        populates it during insertion; the handler reads it after
        strategy.insert returns. The mock strategy's side effect
        captures that population so the assertion sees the production
        value (wh-d94c.1).
        """
        mock_strategy = MagicMock()

        def fake_insert(text, context, request_id, options):
            # Simulate the strategy's _safe_copy populating the seq.
            handler.clipboard.last_clipboard_write_seq = 1234
            return _ok(clipboard_dirty=True)

        mock_strategy.insert.side_effect = fake_insert
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("dictated text")

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once_with(
            write_seq=1234
        )

    def test_unicode_strategy_dirty_false_skips_mark_dirty(self, handler):
        """When the strategy is VerifiedUnicodeStrategy and Unicode
        delivery succeeded without writing the clipboard, clipboard_dirty
        is False and mark_clipboard_dirty is NOT called. The destination
        receives the typed characters via SendInput; the user's clipboard
        is never touched, so no deferred restore is needed.
        """
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("short")

        handler.utterance_manager.mark_clipboard_dirty.assert_not_called()

    def test_simulated_slow_target_sees_dictated_text(self, handler):
        """End-to-end-style slow-target simulation. A fake strategy
        writes the dictated text to a simulated clipboard, sends Ctrl+V,
        and returns clipboard_dirty=True. The simulated destination
        application then reads the clipboard at "consume time" -- after
        raw_insert_text has returned but before any deferred restore
        could fire. The clipboard at that moment must still hold the
        dictated text, not whatever the user had on the clipboard at
        utterance start (which would be the pre-fix race).
        """
        simulated_clipboard = {"value": "ORIGINAL_USER_CLIPBOARD"}
        ctrl_v_sent_at: list[bool] = []

        def fake_insert(text, context, request_id, options):
            # Simulate _safe_copy: write the dictated text to the
            # simulated clipboard and capture the post-write seq.
            simulated_clipboard["value"] = text
            handler.clipboard.last_clipboard_write_seq = 9999
            # Simulate the Ctrl+V keystroke being issued.
            ctrl_v_sent_at.append(True)
            return _ok(clipboard_dirty=True)

        mock_strategy = MagicMock()
        mock_strategy.insert.side_effect = fake_insert
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.raw_insert_text("DICTATED_TEXT")

        # The Ctrl+V keystroke was sent.
        assert ctrl_v_sent_at == [True]
        # At the moment a slow target would consume Ctrl+V (right after
        # raw_insert_text returns), the clipboard still holds the
        # dictated text. Pre-fix, clipboard_context would have restored
        # "ORIGINAL_USER_CLIPBOARD" 50 ms after the keystroke and a slow
        # target reading at e.g. 100 ms would see the original instead.
        assert simulated_clipboard["value"] == "DICTATED_TEXT"
        # mark_clipboard_dirty received the production-style write seq.
        handler.utterance_manager.mark_clipboard_dirty.assert_called_once_with(
            write_seq=9999
        )

    def test_strategy_raises_after_clipboard_write_marks_dirty(self, handler):
        """wh-d94c.3: if the strategy raises AFTER writing the clipboard,
        raw_insert_text must still call mark_clipboard_dirty so the
        deferred-restore mechanism can recover at end_utterance.
        Without this, a post-write exception would leave the dictated
        text on the user's clipboard with no scheduled restore.

        The exception is then propagated; raw_insert_text is NOT in
        _HANDLES_OWN_RESPONSE so the input_proc dispatcher's except
        branch produces a Schema A error response when request_id is
        set.
        """
        boom = RuntimeError("strategy crashed mid-insert")

        def fake_insert(text, context, request_id, options):
            # Simulate a strategy that wrote the clipboard before raising.
            handler.clipboard.last_clipboard_write_seq = 5555
            raise boom

        mock_strategy = MagicMock()
        mock_strategy.insert.side_effect = fake_insert
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            with pytest.raises(RuntimeError, match="strategy crashed mid-insert"):
                handler.raw_insert_text("dictated")

        # Even though the strategy raised, mark_clipboard_dirty fired
        # with the captured seq so the deferred restore can recover.
        handler.utterance_manager.mark_clipboard_dirty.assert_called_once_with(
            write_seq=5555
        )

    def test_strategy_raises_before_clipboard_write_skips_dirty(self, handler):
        """wh-d94c.3 negative case: if the strategy raises BEFORE writing
        the clipboard, raw_insert_text must NOT call mark_clipboard_dirty.
        The user's clipboard is untouched, so no deferred restore is needed.
        """
        def fake_insert(text, context, request_id, options):
            # last_clipboard_write_seq stays None because the strategy
            # raised before reaching _safe_copy.
            raise RuntimeError("pre-write exception")

        mock_strategy = MagicMock()
        mock_strategy.insert.side_effect = fake_insert
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            with pytest.raises(RuntimeError, match="pre-write exception"):
                handler.raw_insert_text("dictated")

        handler.utterance_manager.mark_clipboard_dirty.assert_not_called()


class TestRawInsertTextFailureRaises:
    """wh-fsov0: strategy failure must raise PasteFailedError so the
    input_proc dispatcher's except branch produces a Schema A error
    response (or at least logs the failure for fire-and-forget callers)."""

    def test_failure_raises_paste_failed_error(self, handler):
        from ui.ui_action_handler import PasteFailedError

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _fail(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            with pytest.raises(PasteFailedError):
                handler.raw_insert_text("text")

    def test_success_does_not_raise(self, handler):
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            # Must not raise.
            handler.raw_insert_text("text")


# ============================================================================
# transform_selection
# ============================================================================

class TestTransformSelection:
    """transform_selection flow tests."""

    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="Hello World")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_successful_transformation(self, mock_ctx, mock_cc, mock_pk,
                                       mock_paste, mock_copy, handler):
        """Full flow: copy selection, transform, paste back via verbatim_insert_text.

        wh-iti5: paste-back routes through ``verbatim_insert_text`` instead
        of ``clipboard.verified_paste`` directly so the strategy router
        picks the right delivery (Unicode SendInput or clipboard) and the
        verbatim flag suppresses TextPerfector on the already-transformed
        text.
        """
        mock_ctx.return_value = _make_context(focused_control=MagicMock())

        handler.selection_transformer.apply_transformation.return_value = "hello_world"
        handler.verbatim_insert_text = MagicMock(return_value=True)
        handler.clipboard.clipboard_verification_timeout = 0.1

        handler.transform_selection("snake_case", request_id="r1")

        handler.selection_transformer.apply_transformation.assert_called_once_with(
            "Hello World", "snake_case"
        )
        handler.verbatim_insert_text.assert_called_once_with(
            "hello_world", request_id=None,
        )
        handler.clipboard.verified_paste.assert_not_called()
        handler.buffer_manager.invalidate.assert_called()

        # Response should indicate success
        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response['success'] is True
        assert response['request_id'] == 'r1'

    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_no_selection_detected(self, mock_ctx, mock_cc, mock_pk, handler):
        """When clipboard stays as sentinel, no transformation should occur."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.clipboard_verification_timeout = 0.01  # fast timeout

        # wh-fz7j.4: handler.clipboard is a MagicMock (ClipboardOperations
        # is patched in the fixture), so its _safe_copy does not actually
        # call pyperclip.copy. Wire the mock to forward to the patched
        # pyperclip.copy so the sentinel-captured side effect fires.
        sentinel_captured = []

        def fake_safe_copy(text):
            import pyperclip as _pp
            _pp.copy(text)
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        # Clipboard never changes from sentinel
        with patch("pyperclip.copy", side_effect=lambda t: sentinel_captured.append(t)), \
             patch("pyperclip.paste", side_effect=lambda: sentinel_captured[0] if sentinel_captured else ""):
            handler.transform_selection("snake_case", request_id="r2")

        handler.selection_transformer.apply_transformation.assert_not_called()

    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="some text")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_unknown_transformation_type(self, mock_ctx, mock_cc, mock_pk,
                                         mock_paste, mock_copy, handler):
        """Unknown transformation type should return failure."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.clipboard_verification_timeout = 0.1

        handler.selection_transformer.apply_transformation.return_value = None

        handler.transform_selection("unknown_transform", request_id="r3")

        # Response should indicate failure
        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response['success'] is False

    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="text")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_buffer_invalidated_after_transform(self, mock_ctx, mock_cc, mock_pk,
                                                 mock_paste, mock_copy, handler):
        """Buffer should be invalidated in finally block."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.clipboard_verification_timeout = 0.1
        handler.selection_transformer.apply_transformation.return_value = "TEXT"
        handler.clipboard.verified_paste.return_value = True

        handler.transform_selection("upper_case")

        handler.buffer_manager.invalidate.assert_called()

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_exception_sends_failure_response(self, mock_ctx, mock_cc, mock_pk,
                                               mock_paste, mock_copy, handler):
        """Exception should still send failure response."""
        mock_ctx.side_effect = RuntimeError("context error")

        handler.transform_selection("snake_case", request_id="r4")

        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response['success'] is False


# ============================================================================
# wrap_or_insert
# ============================================================================

class TestWrapOrInsert:
    """wrap_or_insert logic - wrap selection, insert wrapped text, empty delimiters."""

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_insert_wrapped_captured_text(self, mock_ctx, mock_cc,
                                          mock_paste, mock_copy, handler):
        """With captured text, should insert wrapped text via intelligent_insert."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = time.time()  # recent paste

        with patch.object(handler, 'intelligent_insert_text') as mock_iit:
            handler.wrap_or_insert("(", ")", text="hello", request_id="r1")
            mock_iit.assert_called_once_with("(hello)", "r1")

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.press_keys")
    def test_empty_delimiters_with_cursor_position(self, mock_pk, mock_cc,
                                                    mock_ctx, handler):
        """No text and no selection should insert empty delimiters and move cursor."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = 0.0  # old paste
        handler.clipboard.clipboard_verification_timeout = 0.01

        # Clipboard doesn't change (no selection)
        with patch("pyperclip.paste", return_value="original"), \
             patch("pyperclip.copy"), \
             patch("ui.ui_action_handler.time") as mock_time, \
             patch.object(handler, 'intelligent_insert_text') as mock_iit, \
             patch.object(handler, 'press_key_action') as mock_pka:
            mock_time.time.return_value = 100.0
            mock_time.sleep = MagicMock()
            handler.wrap_or_insert("[", "]", text="")
            mock_iit.assert_called_once_with("[]", request_id=None)
            mock_pka.assert_called_once_with("left", repeat=1)

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.press_keys")
    def test_wrap_existing_selection(self, mock_pk, mock_cc, mock_ctx, handler):
        """When text is selected, the wrap routes through verbatim_insert_text.

        wh-iti5: the selection-wrap branch hands the wrapped text to
        verbatim_insert_text so the strategy router picks the right
        delivery (Unicode SendInput or clipboard). The previous direct
        verified_paste call raced the clipboard restore in Qt event-loop
        apps.
        """
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = 0.0
        handler.clipboard.clipboard_verification_timeout = 0.1
        handler.verbatim_insert_text = MagicMock(return_value=True)

        # Simulate clipboard change: first paste() returns original,
        # second paste() returns the selected text (after Ctrl+C)
        with patch("pyperclip.paste", side_effect=["original", "selected text"]), \
             patch("pyperclip.copy"), \
             patch("ui.ui_action_handler.time") as mock_time:
            mock_time.time.side_effect = [100.0, 100.0, 100.0, 100.0]
            mock_time.sleep = MagicMock()
            handler.wrap_or_insert("'", "'", text="", request_id="r2")

        handler.verbatim_insert_text.assert_called_once_with(
            "'selected text'", "r2",
        )
        handler.clipboard.verified_paste.assert_not_called()

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_exception_sends_error_response(self, mock_ctx, mock_cc,
                                             mock_paste, mock_copy, handler):
        """Exception should emit a Schema A error (wh-d43oi)."""
        mock_ctx.side_effect = RuntimeError("context error")

        handler.wrap_or_insert("(", ")", text="", request_id="r3")

        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response.get("error") is True
        assert response["request_id"] == "r3"
        assert response["action"] == "wrap_or_insert"

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_recent_paste_skips_selection_check(self, mock_ctx, mock_cc,
                                                 mock_paste, mock_copy, handler):
        """Recent paste activity should skip selection check."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = time.time()  # very recent

        with patch.object(handler, 'intelligent_insert_text') as mock_iit:
            # No text, but recent paste -> should go to empty delimiters
            handler.wrap_or_insert("{", "}", text="")
            mock_iit.assert_called_once_with("{}", request_id=None)


# ============================================================================
# verbatim_insert_text (wh-iti5)
# ============================================================================


class TestVerbatimInsertText:
    """verbatim_insert_text routes through the strategy router with
    InsertionOptions(mode=VERBATIM) so already-composed text lands
    exactly without TextPerfector mangling.
    """

    @patch(f"{_MOD}.capture_context")
    def test_routes_through_strategy_with_verbatim_options(
        self, mock_ctx, handler,
    ):
        from ui.strategies.base import InsertionMode

        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        result = handler.verbatim_insert_text("(hello)", request_id="r-v1")

        assert result is True
        handler.router.get_strategy.assert_called_once_with(ctx, "(hello)")
        mock_strategy.insert.assert_called_once()
        # Last positional arg is the options object.
        call_args = mock_strategy.insert.call_args.args
        opts = call_args[3]
        assert opts is not None
        assert opts.mode is InsertionMode.VERBATIM

    @patch(f"{_MOD}.capture_context")
    def test_emits_schema_a_response_when_request_id_supplied(
        self, mock_ctx, handler,
    ):
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler.verbatim_insert_text("text", request_id="r-v2")

        # Schema A success on the response queue.
        handler.response_queue.put.assert_called_once()
        msg = handler.response_queue.put.call_args[0][0]
        assert msg["request_id"] == "r-v2"
        assert msg["status"] == "ok"
        assert msg["action"] == "verbatim_insert_text"

    @patch(f"{_MOD}.capture_context")
    def test_no_response_when_request_id_none(self, mock_ctx, handler):
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler.verbatim_insert_text("text", request_id=None)

        # ResponseHandler.send_success and send_error tolerate request_id
        # None and should not put anything on the queue. The transform_selection
        # caller passes None because it owns its own legacy-format response.
        handler.response_queue.put.assert_not_called()

    @patch(f"{_MOD}.capture_context")
    def test_strategy_failure_returns_false_and_emits_error(
        self, mock_ctx, handler,
    ):
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _fail(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        result = handler.verbatim_insert_text("text", request_id="r-err")

        assert result is False
        handler.response_queue.put.assert_called_once()
        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("error") is True
        assert msg["request_id"] == "r-err"
        assert msg["action"] == "verbatim_insert_text"

    @patch(f"{_MOD}.capture_context")
    def test_clipboard_dirty_propagates_to_utterance_manager(
        self, mock_ctx, handler,
    ):
        ctx = _make_context(focused_control=MagicMock())
        mock_ctx.return_value = ctx
        handler.terminal_editor.is_active = False

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=True)
        handler.router.get_strategy.return_value = mock_strategy

        handler.verbatim_insert_text("text", request_id="r-dirty")

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once()


# ============================================================================
# press_key_action
# ============================================================================

class TestPressKeyAction:
    """press_key_action tests."""

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_press_key_calls_press_keys(self, mock_pk, mock_ctx, handler):
        """Should call press_keys for the specified key."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.press_key_action("enter")
        mock_pk.assert_called_once_with("enter")

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_enter_submits_terminal_editor_when_active(self, mock_pk, mock_ctx, handler):
        """Enter key should submit terminal editor when it's active."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = True
        handler.press_key_action("enter", repeat=2)
        handler.terminal_editor.submit.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_press_key_repeats(self, mock_pk, mock_ctx, handler):
        """Repeat parameter should call press_keys multiple times."""
        mock_ctx.return_value = _make_context()
        handler.press_key_action("tab", repeat=3)
        assert mock_pk.call_count == 3

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_cache_invalidating_key_invalidates_buffer(self, mock_pk, mock_ctx, handler):
        """Cache-invalidating keys should invalidate the buffer."""
        mock_ctx.return_value = _make_context()
        handler.press_key_action("backspace")
        handler.buffer_manager.invalidate.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_non_invalidating_key_skips_buffer(self, mock_pk, mock_ctx, handler):
        """Non-cache-invalidating keys should NOT invalidate buffer."""
        mock_ctx.return_value = _make_context()
        handler.press_key_action("a")
        handler.buffer_manager.invalidate.assert_not_called()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_press_key_exception_not_raised(self, mock_pk, mock_ctx, handler):
        """Exception should be caught and logged, not raised."""
        mock_ctx.return_value = _make_context()
        mock_pk.side_effect = RuntimeError("key press failed")
        handler.press_key_action("enter")  # Should not raise

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_all_cache_invalidating_keys(self, mock_pk, mock_ctx, handler):
        """All defined cache-invalidating keys should trigger invalidation."""
        from ui.ui_action_handler import CACHE_INVALIDATING_KEYS
        mock_ctx.return_value = _make_context()

        for key in CACHE_INVALIDATING_KEYS:
            handler.buffer_manager.invalidate.reset_mock()
            handler.press_key_action(key)
            handler.buffer_manager.invalidate.assert_called_once(), \
                f"Key '{key}' should invalidate buffer"


# ============================================================================
# hotkey_action
# ============================================================================

class TestHotkeyAction:
    """hotkey_action tests."""

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_hotkey_standard_app(self, mock_pk, mock_ctx, handler):
        """Standard app should use press_keys with SendInput."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.hotkey_action(["ctrl", "c"])
        mock_pk.assert_called_once_with("ctrl", "c")

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_enter_hotkey_submits_terminal_editor_when_active(self, mock_pk, mock_ctx, handler):
        """Enter hotkey should submit terminal editor when it's active."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = True
        handler.hotkey_action(["enter"], repeat=3)
        handler.terminal_editor.submit.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_hotkey_repeat(self, mock_pk, mock_ctx, handler):
        """Repeat parameter should execute hotkey multiple times."""
        mock_ctx.return_value = _make_context()
        handler.hotkey_action(["ctrl", "z"], repeat=3)
        assert mock_pk.call_count == 3

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_hotkey_flutter_uses_sendkeys(self, mock_pk, mock_ctx, handler):
        """Flutter app should use SendKeys on focused control."""
        fc = MagicMock()
        fc.Exists.return_value = True
        mock_ctx.return_value = _make_context(
            focused_control=fc, is_flutter=True
        )

        with patch.object(handler, '_convert_to_sendkeys_format', return_value='{Ctrl}c'):
            handler.hotkey_action(["ctrl", "c"])

        fc.SendKeys.assert_called_once_with('{Ctrl}c')
        mock_pk.assert_not_called()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_hotkey_exception_not_raised(self, mock_pk, mock_ctx, handler):
        """Exception should be caught and logged, not raised."""
        mock_ctx.return_value = _make_context()
        mock_pk.side_effect = RuntimeError("hotkey failed")
        handler.hotkey_action(["ctrl", "s"])  # Should not raise


# ============================================================================
# _convert_to_sendkeys_format
# ============================================================================

class TestConvertToSendkeysFormat:
    """_convert_to_sendkeys_format pure conversion tests."""

    def test_modifier_keys(self, handler):
        """Modifier keys should be wrapped in braces."""
        assert handler._convert_to_sendkeys_format(["ctrl"]) == "{Ctrl}"
        assert handler._convert_to_sendkeys_format(["shift"]) == "{Shift}"
        assert handler._convert_to_sendkeys_format(["alt"]) == "{Alt}"
        assert handler._convert_to_sendkeys_format(["win"]) == "{Win}"

    def test_special_keys(self, handler):
        """Special keys should be mapped and wrapped in braces."""
        assert handler._convert_to_sendkeys_format(["left"]) == "{Left}"
        assert handler._convert_to_sendkeys_format(["enter"]) == "{Enter}"
        assert handler._convert_to_sendkeys_format(["backspace"]) == "{BS}"
        assert handler._convert_to_sendkeys_format(["tab"]) == "{Tab}"
        assert handler._convert_to_sendkeys_format(["escape"]) == "{Esc}"

    def test_single_char_key(self, handler):
        """Single character keys should be lowercase without braces."""
        assert handler._convert_to_sendkeys_format(["a"]) == "a"
        assert handler._convert_to_sendkeys_format(["c"]) == "c"

    def test_modifier_plus_char(self, handler):
        """Modifier + char combination should be formatted correctly."""
        result = handler._convert_to_sendkeys_format(["ctrl", "c"])
        assert result == "{Ctrl}c"

    def test_multiple_modifiers_plus_special(self, handler):
        """Multiple modifiers + special key should all be formatted."""
        result = handler._convert_to_sendkeys_format(["ctrl", "shift", "left"])
        assert result == "{Ctrl}{Shift}{Left}"

    def test_empty_keys_list(self, handler):
        """Empty keys list should return empty string."""
        assert handler._convert_to_sendkeys_format([]) == ""

    def test_space_key(self, handler):
        """Space key should be mapped without braces."""
        result = handler._convert_to_sendkeys_format(["space"])
        assert result == " "

    def test_case_insensitive(self, handler):
        """Key matching should be case-insensitive."""
        assert handler._convert_to_sendkeys_format(["CTRL"]) == "{Ctrl}"
        assert handler._convert_to_sendkeys_format(["Shift"]) == "{Shift}"
        assert handler._convert_to_sendkeys_format(["LEFT"]) == "{Left}"


# ============================================================================
# show_notification
# ============================================================================

class TestShowNotification:
    """show_notification tests."""

    def test_valid_notification(self, handler):
        """Valid parameters should dispatch notification."""
        import sys
        mock_notification = MagicMock()
        mock_notification.notify = MagicMock()
        mock_plyer = MagicMock()
        mock_plyer.notification = mock_notification

        original_plyer = sys.modules.get("plyer")
        try:
            sys.modules["plyer"] = mock_plyer
            handler.show_notification("Test", "Message", timeout=3)

            mock_notification.notify.assert_called_once_with(
                title="Test",
                message="Message",
                app_name="Wheelhouse",
                timeout=3
            )
        finally:
            if original_plyer is not None:
                sys.modules["plyer"] = original_plyer
            else:
                sys.modules.pop("plyer", None)

    def test_invalid_title_type(self, handler):
        """Non-string title should be rejected (logged, not raised)."""
        handler.show_notification(123, "Message", timeout=5)  # Should not raise

    def test_invalid_message_type(self, handler):
        """Non-string message should be rejected."""
        handler.show_notification("Title", 456, timeout=5)  # Should not raise

    def test_invalid_timeout_type(self, handler):
        """Non-int timeout should be rejected."""
        handler.show_notification("Title", "Message", timeout="five")  # Should not raise

    def test_plyer_import_failure(self, handler):
        """If plyer is not installed, should catch ImportError."""
        import sys
        original_plyer = sys.modules.get("plyer")
        try:
            # Force ImportError by setting module to None
            sys.modules["plyer"] = None
            handler.show_notification("Title", "Message")  # Should not raise
        finally:
            if original_plyer is not None:
                sys.modules["plyer"] = original_plyer
            else:
                sys.modules.pop("plyer", None)


# ============================================================================
# Adversarial Tests
# ============================================================================

class TestAdversarial:
    """Edge cases and adversarial inputs."""

    def test_intelligent_insert_empty_string(self, handler):
        """Empty string should NOT be buffered as a letter."""
        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.terminal_editor.is_active = False
            handler.utterance_manager.is_in_utterance.return_value = True
            handler.utterance_manager._clipboard_dirty = False
            handler.utterance_manager._last_paste_time = 0.0

            mock_strategy = MagicMock()
            handler.router.get_strategy.return_value = mock_strategy

            handler.intelligent_insert_text("")
            assert handler._letter_buffer == []

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_press_key_with_unexpected_kwargs(self, mock_pk, mock_ctx, handler):
        """press_key_action should warn about unexpected request_id."""
        mock_ctx.return_value = _make_context()
        # Should not raise
        handler.press_key_action("enter", request_id="unexpected")

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.press_keys")
    def test_hotkey_with_unexpected_kwargs(self, mock_pk, mock_ctx, handler):
        """hotkey_action should warn about unexpected request_id."""
        mock_ctx.return_value = _make_context()
        # Should not raise
        handler.hotkey_action(["ctrl", "c"], request_id="unexpected")

    @patch(f"{_MOD}.capture_context")
    def test_insert_with_none_focused_control(self, mock_ctx, handler):
        """None focused_control should still work (router handles it)."""
        mock_ctx.return_value = _make_context(focused_control=None)
        handler.terminal_editor.is_active = False
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._clipboard_dirty = False
        handler.utterance_manager._last_paste_time = 0.0

        mock_strategy = MagicMock()
        handler.router.get_strategy.return_value = mock_strategy

        handler.intelligent_insert_text("word")
        # window_manager.remember_target should NOT be called with None
        handler.window_manager.remember_target.assert_not_called()

    def test_consecutive_single_letters_accumulate(self, handler):
        """Multiple consecutive single letters should accumulate in buffer."""
        handler.intelligent_insert_text("a")
        handler.intelligent_insert_text("b")
        handler.intelligent_insert_text("c")
        assert handler._letter_buffer == ["a", "b", "c"]

    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_wrap_or_insert_whitespace_only_text(self, mock_ctx, mock_cc,
                                                  mock_paste, mock_copy, handler):
        """Whitespace-only text should be treated as empty text."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = time.time()

        with patch.object(handler, 'intelligent_insert_text') as mock_iit:
            handler.wrap_or_insert("(", ")", text="   ")
            # Whitespace stripped -> empty -> should insert empty delimiters
            mock_iit.assert_called_once_with("()", request_id=None)

    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_transform_selection_no_request_id(self, mock_ctx, mock_cc,
                                                mock_pk, handler):
        """transform_selection without request_id should not send response."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.clipboard_verification_timeout = 0.01

        # Clipboard doesn't change (no selection)
        sentinel_captured = []
        with patch("pyperclip.copy", side_effect=lambda t: sentinel_captured.append(t)), \
             patch("pyperclip.paste", side_effect=lambda: sentinel_captured[0] if sentinel_captured else ""):
            handler.transform_selection("snake_case")

        handler.response_queue.put.assert_not_called()

    def test_skip_clipboard_restore_default_enables(self, handler):
        """skip_clipboard_restore with no args should enable (default True)."""
        handler.skip_clipboard_restore()
        handler.utterance_manager.skip_clipboard_restore.assert_called_once()


# ============================================================================
# type_text - Raw keystroke typing via SendInput
# ============================================================================

class TestTypeText:
    """type_text: raw character-by-character typing via type_string (SendInput).

    Unlike intelligent_insert_text (clipboard paste with spacing/context logic),
    type_text sends raw keystrokes. Used by patterns like 'find <text>' where
    text must be typed into a dialog, not pasted.
    """

    _TS = f"{_MOD}.type_string"

    @patch(_TS)
    def test_type_text_calls_type_string(self, mock_ts, handler):
        """type_text should call type_string with the text."""
        handler.type_text("hello")
        mock_ts.assert_called_once_with("hello")

    @patch(_TS)
    def test_type_text_empty_string(self, mock_ts, handler):
        """Empty string should still be forwarded to type_string."""
        handler.type_text("")
        mock_ts.assert_called_once_with("")

    @patch(_TS)
    def test_type_text_special_characters(self, mock_ts, handler):
        """Special characters should be passed through to type_string."""
        handler.type_text("hello world!\n")
        mock_ts.assert_called_once_with("hello world!\n")

    @patch(_TS)
    def test_type_text_accepts_extra_kwargs(self, mock_ts, handler):
        """type_text should accept and ignore extra kwargs (like request_id)."""
        handler.type_text("test", request_id="r1", foo="bar")
        mock_ts.assert_called_once_with("test")

    @patch(_TS)
    def test_type_text_exception_not_raised(self, mock_ts, handler):
        """Exception in type_string should be caught, not raised."""
        mock_ts.side_effect = RuntimeError("SendInput failed")
        handler.type_text("test")  # Should not raise


