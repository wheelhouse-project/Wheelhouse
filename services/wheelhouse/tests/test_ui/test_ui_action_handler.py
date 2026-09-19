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
from unittest.mock import ANY, MagicMock, Mock, PropertyMock, patch


class _CarriesAControl:
    """Matches a CapturedTarget that names a real control.

    wh-review-pattern-fixes.45: the selection paths must hand the
    control they copied from to verbatim_insert_text. ``ANY`` would
    also match ``None``, which is exactly the regression these
    assertions exist to catch, so the comparison is explicit.
    """

    def __eq__(self, other):
        return getattr(other, "control", None) is not None

    def __repr__(self):
        return "<a CapturedTarget that names a control>"

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
        # wh-review-pattern-fixes.23: the flush now reads the strategy
        # outcome; deliver a real success so this test stays on the
        # delivered path.
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

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
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

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
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

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
        # wh-review-pattern-fixes.23: a delivered flush outcome keeps this
        # test on the intended path (a failed flush now skips the word).
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler._letter_buffer = ["c", "a", "t"]
        handler.intelligent_insert_text("hello", request_id="r2")

        # Buffer should have been flushed
        assert handler._letter_buffer == []
        mock_compress.assert_called_once_with("c a t")
        # The word still goes through the strategy after the flush.
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["cat", "hello"]

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.capture_context")
    def test_non_deferred_single_letter_flushes_buffer_first(
            self, mock_ctx, mock_compress, handler):
        """defer_single_letter=False must flush pending buffered letters
        BEFORE inserting the single letter through the strategy path
        (wh-review-pattern-fixes.16). Ordering matters: the buffered
        letters were dictated earlier and must land first."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.terminal_editor.is_active = False
        handler.clipboard.last_clipboard_write_seq = None

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        handler._letter_buffer = ["a", "b"]
        delivered = handler.intelligent_insert_text(
            "x", request_id=None, defer_single_letter=False)

        assert delivered is True
        assert handler._letter_buffer == []
        mock_compress.assert_called_once_with("a b")
        # Flushed letters insert first, then the non-deferred letter.
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["ab", "x"]

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


    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="Hello World")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_transform_selection_marks_dirty(
        self, mock_ctx, mock_cc, mock_pk, mock_paste, mock_copy, mock_vpk,
        handler
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

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch("pyperclip.copy")
    @patch("pyperclip.paste")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_wrap_or_insert_selection_branch_marks_dirty(
        self, mock_ctx, mock_cc, mock_pk, mock_paste, mock_copy, mock_vpk,
        handler
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

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="Hello World")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_successful_transformation(self, mock_ctx, mock_cc, mock_pk,
                                       mock_paste, mock_copy, mock_vpk,
                                       handler):
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
        # wh-review-pattern-fixes.45: the paste-back carries the
        # control the selection was copied from.
        handler.verbatim_insert_text.assert_called_once_with(
            "hello_world", request_id=None, captured_target=ANY,
        )
        carried = handler.verbatim_insert_text.call_args.kwargs[
            "captured_target"
        ]
        assert carried.control is mock_ctx.return_value.focused_control
        handler.clipboard.verified_paste.assert_not_called()
        handler.buffer_manager.invalidate.assert_called()

        # Response should indicate success
        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response['success'] is True
        assert response['request_id'] == 'r1'

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_no_selection_detected(self, mock_ctx, mock_cc, mock_pk,
                                   mock_vpk, handler):
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

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="some text")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_unknown_transformation_type(self, mock_ctx, mock_cc, mock_pk,
                                         mock_paste, mock_copy, mock_vpk,
                                         handler):
        """Unknown transformation type should return failure."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.clipboard.clipboard_verification_timeout = 0.1

        handler.selection_transformer.apply_transformation.return_value = None

        handler.transform_selection("unknown_transform", request_id="r3")

        # Response should indicate failure
        handler.response_queue.put.assert_called_once()
        response = handler.response_queue.put.call_args[0][0]
        assert response['success'] is False

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="text")
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_buffer_invalidated_after_transform(self, mock_ctx, mock_cc, mock_pk,
                                                 mock_paste, mock_copy,
                                                 mock_vpk, handler):
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

    # -- wh-review-pattern-fixes.25 (sibling of the wrap_or_insert
    # selection branch): transform_selection captures the selection via
    # Ctrl+C and pastes back through verbatim_insert_text, with no
    # letter-buffer flush. A single letter deferred earlier in the same
    # utterance must land BEFORE the selection is copied or mutated. On
    # a failed flush the method must not copy or mutate the selection.

    def _run_transform_with_pending_letters(self, handler, flush_result,
                                            request_id="r-ts25"):
        """Drive transform_selection with a buffered letter pending.

        The strategy mock serves the flush insert. _safe_copy and
        press_keys record into ``events`` so the test can assert the
        order of side effects. The clipboard poll never sees a change
        from the sentinel, so after a delivered flush the method finds
        no selection (the natural re-evaluation). Returns (events,
        mock_strategy)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.clipboard.clipboard_verification_timeout = 0.01
        handler.clipboard.last_clipboard_write_seq = None
        handler._letter_buffer = ["a"]

        events = []
        mock_strategy = MagicMock()

        def _record_insert(text, *args, **kwargs):
            events.append(("insert", text))
            return flush_result

        mock_strategy.insert.side_effect = _record_insert
        handler.router.get_strategy.return_value = mock_strategy

        sentinel_holder = []

        def fake_safe_copy(text):
            events.append(("safe_copy", text))
            sentinel_holder.append(text)
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        # wh-review-pattern-fixes.35: the selection copy rides
        # verified_press_keys now. Record it under the same
        # "press_keys" event kind so the ordering assertions cover the
        # copy regardless of which sender it uses.
        def _record_vpk(*keys):
            events.append(("press_keys", keys))
            return (True, 4, 4)

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys",
                   side_effect=lambda *keys: events.append(
                       ("press_keys", keys))), \
             patch(f"{_MOD}.verified_press_keys", side_effect=_record_vpk), \
             patch(f"{_MOD}.auto_compress_spelled_letters",
                   return_value="a"), \
             patch("pyperclip.paste",
                   side_effect=lambda: (
                       sentinel_holder[-1] if sentinel_holder else "")):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.transform_selection("snake_case", request_id=request_id)

        return events, mock_strategy

    def test_pending_letters_flushed_before_selection_capture(self, handler):
        """A pending deferred letter must flush BEFORE the sentinel write
        and the Ctrl+C copy (wh-review-pattern-fixes.25). The delivered
        letter consumed the selection, so the poll finds no selection
        and the method does not transform."""
        events, _ = self._run_transform_with_pending_letters(
            handler,
            flush_result=InsertionResult(success=True, clipboard_dirty=False),
            request_id="r-ts25-ok",
        )

        # The flush is the first side effect.
        assert events[0] == ("insert", "a")
        kinds = [k for (k, _) in events]
        assert kinds.index("insert") < kinds.index("safe_copy")
        assert kinds.index("safe_copy") < kinds.index("press_keys")
        assert handler._letter_buffer == []
        # Natural re-evaluation: no selection remains, no transform runs.
        handler.selection_transformer.apply_transformation.assert_not_called()
        resp = handler.response_queue.put.call_args[0][0]
        assert resp["success"] is False
        assert resp["request_id"] == "r-ts25-ok"

    def test_failed_flush_fail_closed_no_capture(self, handler):
        """A failed flush must not write the sentinel, must not send
        Ctrl+C, must not transform, and must emit the single failure
        response (wh-review-pattern-fixes.25, fail-closed per the
        finding .23 design)."""
        events, _ = self._run_transform_with_pending_letters(
            handler,
            flush_result=InsertionResult(success=False, clipboard_dirty=False),
            request_id="r-ts25-nd",
        )

        inserted = [t for (k, t) in events if k == "insert"]
        assert inserted == ["a"]  # only the flush attempt
        assert not any(k == "safe_copy" for (k, _) in events)
        assert not any(k == "press_keys" for (k, _) in events)
        handler.selection_transformer.apply_transformation.assert_not_called()
        handler.response_queue.put.assert_called_once()
        resp = handler.response_queue.put.call_args[0][0]
        assert resp["success"] is False
        assert resp["request_id"] == "r-ts25-nd"


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

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.press_keys")
    def test_empty_delimiters_with_cursor_position(self, mock_pk, mock_cc,
                                                    mock_ctx, mock_vpk,
                                                    handler):
        """No text and no selection should insert empty delimiters and move cursor."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager._last_paste_time = 0.0  # old paste
        handler.clipboard.clipboard_verification_timeout = 0.01

        # wh-review-pattern-fixes.31: the probe writes a sentinel via
        # _safe_copy and polls for a change from it. Clipboard stays at
        # the sentinel (no selection) -> Priority 2.
        sentinel_box = {"value": "original"}

        def fake_safe_copy(value):
            sentinel_box["value"] = value
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        with patch("pyperclip.paste",
                   side_effect=lambda: sentinel_box["value"]), \
             patch("pyperclip.copy"), \
             patch("ui.ui_action_handler.time") as mock_time, \
             patch.object(handler, 'intelligent_insert_text') as mock_iit, \
             patch.object(handler, 'press_key_verified',
                          return_value={"success": True}) as mock_pkv:
            mock_time.time.return_value = 100.0
            mock_time.sleep = MagicMock()
            handler.wrap_or_insert("[", "]", text="")
            mock_iit.assert_called_once_with(
                "[]", request_id=None, defer_single_letter=False)
            # wh-review-pattern-fixes.37: the caret move rides the
            # acknowledged press.
            mock_pkv.assert_called_once_with("left", repeat=1)

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.press_keys")
    def test_wrap_existing_selection(self, mock_pk, mock_cc, mock_ctx,
                                     mock_vpk, handler):
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

        # wh-review-pattern-fixes.31: the probe writes a sentinel and
        # polls for a change from it. The first poll already returns
        # the selected text (Ctrl+C landed).
        with patch("pyperclip.paste", side_effect=["selected text"]), \
             patch("pyperclip.copy"), \
             patch("ui.ui_action_handler.time") as mock_time:
            mock_time.time.side_effect = [100.0, 100.0, 100.0, 100.0]
            mock_time.sleep = MagicMock()
            handler.wrap_or_insert("'", "'", text="", request_id="r2")

        # wh-review-pattern-fixes.45: the wrap carries the control the
        # selection was copied from.
        handler.verbatim_insert_text.assert_called_once_with(
            "'selected text'", "r2", captured_target=_CarriesAControl(),
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
            mock_iit.assert_called_once_with(
                "{}", request_id=None, defer_single_letter=False)

    # -- wh-review-pattern-fixes.11: the Priority 2 empty-delimiter branch
    # must gate the caret move and the verified success response on the
    # nested insertion's delivery outcome. A failed or rejected insertion
    # must not move the caret and must not claim insert_verified.

    def _run_empty_delimiter_branch(self, handler, insert_result,
                                    request_id="r-p2"):
        """Drive wrap_or_insert into the Priority 2 branch with a real
        intelligent_insert_text call whose strategy returns
        ``insert_result``. Returns the press_key_verified mock
        (wh-review-pattern-fixes.37: the caret move is acknowledged)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = time.time()  # skip selection check
        handler.clipboard.last_clipboard_write_seq = None

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = insert_result
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch.object(handler, 'press_key_verified',
                          return_value={"success": True}) as mock_pkv:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return mock_pkv

    def test_empty_delimiters_failed_insert_no_caret_move(self, handler):
        """A strategy failure (success=False) must not press left and must
        not emit an insert_verified success (wh-review-pattern-fixes.11)."""
        mock_pkv = self._run_empty_delimiter_branch(
            handler,
            InsertionResult(success=False, clipboard_dirty=False),
            request_id="r-nd",
        )

        mock_pkv.assert_not_called()
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert all(m.get("path") != "insert_verified" for m in msgs)
        # wrap_or_insert owns the response for this path: exactly one
        # Schema A error, same shape as its exception path.
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-nd"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_empty_delimiters_rejected_insert_no_caret_move(self, handler):
        """A pre-send rejection (RejectedInsertionStrategy shape:
        success=True + rejected_reason) delivered no text, so the branch
        must not press left and must not emit insert_verified."""
        mock_pkv = self._run_empty_delimiter_branch(
            handler,
            InsertionResult(
                success=True,
                clipboard_dirty=False,
                rejected_reason="no_text_target",
            ),
            request_id="r-rej",
        )

        mock_pkv.assert_not_called()
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert all(m.get("path") != "insert_verified" for m in msgs)
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-rej"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_empty_delimiters_delivered_insert_moves_caret_once(self, handler):
        """A delivered insertion still presses left exactly once and emits
        the verified wrap_or_insert success (guard for the fix)."""
        mock_pkv = self._run_empty_delimiter_branch(
            handler,
            InsertionResult(success=True, clipboard_dirty=False),
            request_id="r-ok",
        )

        mock_pkv.assert_called_once_with("left", repeat=1)
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        # Nested call runs with request_id=None, so its own emission is
        # suppressed; wrap_or_insert emits the single verified success.
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-ok"
        assert msgs[0]["action"] == "wrap_or_insert"

    # -- wh-review-pattern-fixes.16: when the concatenated fences are a
    # single alphabetic character (reachable from a manually authored
    # pattern, e.g. wrap_or_insert('x', '', '')), the nested insertion
    # must NOT take the letter-buffer branch. Buffering would insert
    # nothing physically, return True, press left immediately, and let
    # the deferred flush land the letter at the shifted caret (possibly
    # coalesced with pending buffered letters). The Priority 2 call
    # passes defer_single_letter=False so the letter routes through a
    # real insertion strategy.

    def _run_single_letter_delimiter_branch(self, handler, insert_result,
                                            request_id="r-sl"):
        """Drive wrap_or_insert('x', '') into the Priority 2 branch with
        a real intelligent_insert_text call whose strategy returns
        ``insert_result``. Records call order in the returned events
        list and returns (events, strategy_mock)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = time.time()  # skip selection check
        handler.clipboard.last_clipboard_write_seq = None

        events = []
        mock_strategy = MagicMock()

        def _record_insert(*args, **kwargs):
            events.append("insert")
            return insert_result

        mock_strategy.insert.side_effect = _record_insert
        handler.router.get_strategy.return_value = mock_strategy

        def _record_left(*a, **k):
            events.append("left")
            return {"success": True}

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch.object(
                 handler, 'press_key_verified', side_effect=_record_left):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("x", "", text="", request_id=request_id)

        return events, mock_strategy

    def test_single_letter_delimiter_not_buffered_left_after_insert(
            self, handler):
        """A single-letter delimiter must skip the letter buffer, go
        through a real insertion strategy, and press left only AFTER
        the insertion call (wh-review-pattern-fixes.16)."""
        events, mock_strategy = self._run_single_letter_delimiter_branch(
            handler,
            InsertionResult(success=True, clipboard_dirty=False),
            request_id="r-sl-ok",
        )

        # Not buffered for deferred delivery.
        assert handler._letter_buffer == []
        # Delivered through the strategy path.
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["x"]
        # Left press strictly after the insertion call, exactly once.
        assert events == ["insert", "left"]
        # wrap_or_insert owns the single verified success response.
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-sl-ok"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_single_letter_delimiter_failed_insert_no_caret_move(
            self, handler):
        """A strategy failure for the single-letter delimiter must not
        press left and must emit the wrap_or_insert error -- the .11
        gating applies unchanged (wh-review-pattern-fixes.16)."""
        events, _ = self._run_single_letter_delimiter_branch(
            handler,
            InsertionResult(success=False, clipboard_dirty=False),
            request_id="r-sl-nd",
        )

        assert "left" not in events
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-sl-nd"
        assert msgs[0]["action"] == "wrap_or_insert"
        assert msgs[0]["message"] == "empty delimiter insertion not delivered"

    # -- wh-review-pattern-fixes.23: the Priority 2 branch flushes any
    # pending buffered letters before the delimiter insertion. A failed
    # flush means letters were lost ahead of the delimiters; pressing
    # left and claiming insert_verified would report the wrong text
    # order AND the wrong caret. The .11 gating must cover the flush
    # outcome, not only the delimiter insert.

    def _run_empty_delimiter_branch_with_flush(self, handler,
                                               flush_result,
                                               delimiter_result,
                                               request_id="r-fl"):
        """Drive wrap_or_insert into Priority 2 with buffered letters
        pending. The first strategy insert is the flush, the second (if
        reached) is the delimiter insertion. Returns the
        press_key_verified mock and the strategy mock."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = time.time()  # skip selection check
        handler.clipboard.last_clipboard_write_seq = None
        handler._letter_buffer = ["a", "b"]

        mock_strategy = MagicMock()
        mock_strategy.insert.side_effect = [flush_result, delimiter_result]
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.auto_compress_spelled_letters",
                   return_value="ab"), \
             patch.object(handler, 'press_key_verified',
                          return_value={"success": True}) as mock_pkv:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return mock_pkv, mock_strategy

    def test_empty_delimiters_failed_flush_no_caret_move(self, handler):
        """A failed letter-buffer flush must not press left and must emit
        the wrap_or_insert error instead of insert_verified, even when
        the delimiter insertion itself would have succeeded
        (wh-review-pattern-fixes.23)."""
        mock_pkv, mock_strategy = self._run_empty_delimiter_branch_with_flush(
            handler,
            flush_result=InsertionResult(success=False, clipboard_dirty=False),
            delimiter_result=InsertionResult(success=True, clipboard_dirty=False),
            request_id="r-fl-nd",
        )

        mock_pkv.assert_not_called()
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert all(m.get("path") != "insert_verified" for m in msgs)
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-fl-nd"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_empty_delimiters_successful_flush_moves_caret(self, handler):
        """Guard: a delivered flush followed by a delivered delimiter
        insertion still presses left once and emits the verified
        wrap_or_insert success."""
        mock_pkv, mock_strategy = self._run_empty_delimiter_branch_with_flush(
            handler,
            flush_result=InsertionResult(success=True, clipboard_dirty=False),
            delimiter_result=InsertionResult(success=True, clipboard_dirty=False),
            request_id="r-fl-ok",
        )

        mock_pkv.assert_called_once_with("left", repeat=1)
        # Flush first, then the delimiters.
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["ab", "()"]
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-fl-ok"
        assert msgs[0]["action"] == "wrap_or_insert"

    # -- wh-review-pattern-fixes.25: the SELECTION branch must flush any
    # pending deferred letters BEFORE the Ctrl+C selection capture. A
    # delivered flush letter replaces the selection, so the correct
    # branch is then the empty-delimiter insertion path, not the wrap.
    # On a failed flush the branch must not copy or mutate the
    # selection, must leave the caret alone, and must emit the single
    # wrap_or_insert failure response (finding .23 design).

    def _run_selection_branch_with_pending_letters(self, handler,
                                                   flush_result,
                                                   delimiter_result=None,
                                                   request_id="r-25"):
        """Drive wrap_or_insert into the selection-check path (stale
        paste time, empty text) with a buffered letter pending.

        pyperclip is wired so that IF the code sends Ctrl+C, the
        clipboard changes and the stale selection would be wrapped.
        Records the order of side effects in ``events``. Returns
        (events, mock_strategy)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = 0.0  # stale -> selection check
        handler.clipboard.clipboard_verification_timeout = 0.01
        handler.clipboard.last_clipboard_write_seq = None
        handler._letter_buffer = ["a"]

        events = []
        results = [flush_result]
        if delimiter_result is not None:
            results.append(delimiter_result)
        mock_strategy = MagicMock()

        def _record_insert(text, *args, **kwargs):
            events.append(("insert", text))
            return results.pop(0)

        mock_strategy.insert.side_effect = _record_insert
        handler.router.get_strategy.return_value = mock_strategy

        clipboard_state = {"value": "original"}

        def fake_press_keys(*keys):
            events.append(("press_keys", keys))
            if keys == ("ctrl", "c"):
                clipboard_state["value"] = "stale selection"

        # wh-review-pattern-fixes.35: the selection copy rides
        # verified_press_keys now. Record it under the same
        # "press_keys" event kind so the no-copy assertions cover it.
        def fake_vpk(*keys):
            fake_press_keys(*keys)
            return (True, 4, 4)

        def _record_left(*a, **k):
            events.append(("left", a))
            return {"success": True}

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys", side_effect=fake_press_keys), \
             patch(f"{_MOD}.verified_press_keys", side_effect=fake_vpk), \
             patch(f"{_MOD}.auto_compress_spelled_letters",
                   return_value="a"), \
             patch("pyperclip.paste",
                   side_effect=lambda: clipboard_state["value"]), \
             patch("pyperclip.copy"), \
             patch.object(
                 handler, 'press_key_verified',
                 side_effect=_record_left):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return events, mock_strategy

    def test_selection_branch_flushes_pending_letters_before_copy(
            self, handler):
        """A pending deferred letter must land BEFORE any selection
        capture. The delivered flush replaces the selection, so the
        branch re-evaluates to the empty-delimiter path: no Ctrl+C, no
        wrap, delimiters inserted after the letter, caret moved once
        (wh-review-pattern-fixes.25)."""
        events, _ = self._run_selection_branch_with_pending_letters(
            handler,
            flush_result=InsertionResult(success=True, clipboard_dirty=False),
            delimiter_result=InsertionResult(
                success=True, clipboard_dirty=False),
            request_id="r-25-ok",
        )

        # The flush is the first side effect; the copy never fires.
        assert events[0] == ("insert", "a")
        assert ("press_keys", ("ctrl", "c")) not in events
        # Letter first, then the empty delimiters -- spoken order.
        inserted = [t for (k, t) in events if k == "insert"]
        assert inserted == ["a", "()"]
        assert handler._letter_buffer == []
        # Empty-delimiter path completes: left press after the insert.
        assert events[-1] == ("left", ("left",))
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-25-ok"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_selection_branch_failed_flush_fail_closed(self, handler):
        """A failed flush must not send Ctrl+C, must not wrap or insert
        anything else, must leave the caret alone, and must emit exactly
        one wrap_or_insert error (wh-review-pattern-fixes.25)."""
        events, _ = self._run_selection_branch_with_pending_letters(
            handler,
            flush_result=InsertionResult(success=False, clipboard_dirty=False),
            request_id="r-25-nd",
        )

        assert ("press_keys", ("ctrl", "c")) not in events
        inserted = [t for (k, t) in events if k == "insert"]
        assert inserted == ["a"]  # only the flush attempt
        assert not any(k == "left" for (k, _) in events)
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-25-nd"
        assert msgs[0]["action"] == "wrap_or_insert"


class TestWrapOrInsertSentinelSelectionDetection:
    """wh-review-pattern-fixes.31: the selection probe must use the
    sentinel pattern (as transform_selection and capture_selected_text
    already do), not a comparison against the pre-copy clipboard value.

    With the original-value comparison, a selection whose text equals
    the current clipboard content copies successfully but changes
    nothing; the poll times out, the branch concludes "no selection",
    and Priority 2 inserts empty fences OVER the still-active
    selection, destroying it. The sentinel is unique per call, so a
    successful copy always changes the polled value.
    """

    def _run_selection_probe(self, handler, clipboard_state,
                             safe_copy_ok=True, request_id="r-31"):
        """Drive wrap_or_insert into the selection-check path. The
        simulated clipboard starts at clipboard_state["value"]; a
        Ctrl+C copies clipboard_state["selection"] onto it. Returns
        (pressed_keys, intelligent_insert mock, press_key_verified mock).
        """
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = 0.0  # stale -> check
        handler.clipboard.clipboard_verification_timeout = 0.05
        handler._letter_buffer = []
        handler.verbatim_insert_text = MagicMock(return_value=True)

        def fake_safe_copy(value):
            if not safe_copy_ok:
                return False
            clipboard_state["value"] = value
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        pressed = []

        def fake_press_keys(*keys):
            pressed.append(keys)
            if keys == ("ctrl", "c"):
                clipboard_state["value"] = clipboard_state["selection"]

        # wh-review-pattern-fixes.35: the selection copy rides
        # verified_press_keys now. Record it in the same ``pressed``
        # list so the probe assertions cover it.
        def fake_vpk(*keys):
            fake_press_keys(*keys)
            return (True, 4, 4)

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys", side_effect=fake_press_keys), \
             patch(f"{_MOD}.verified_press_keys", side_effect=fake_vpk), \
             patch("pyperclip.paste",
                   side_effect=lambda: clipboard_state["value"]), \
             patch("pyperclip.copy"), \
             patch.object(handler, 'intelligent_insert_text') as mock_iit, \
             patch.object(handler, 'press_key_verified',
                          return_value={"success": True}) as mock_pkv:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return pressed, mock_iit, mock_pkv

    def test_selection_equal_to_clipboard_still_wraps(self, handler):
        """The reachable data-loss case: selected text == current
        clipboard content. A successful Ctrl+C changes nothing under
        the original-value comparison; with the sentinel the copy is
        detected and the selection is wrapped, not overwritten."""
        clipboard_state = {"value": "same", "selection": "same"}

        pressed, mock_iit, mock_pka = self._run_selection_probe(
            handler, clipboard_state)

        handler.verbatim_insert_text.assert_called_once_with(
            "(same)", "r-31", captured_target=_CarriesAControl())
        # No fall-through to empty-fence insertion over the selection.
        mock_iit.assert_not_called()
        mock_pka.assert_not_called()

    def test_sentinel_write_failure_fails_closed(self, handler):
        """When the sentinel write fails, the probe cannot distinguish
        selection from no-selection. Falling through to Priority 2
        would insert empty fences over a possibly-active selection, so
        the branch must fail closed: no Ctrl+C, no insertion, no caret
        move, one Schema A error."""
        clipboard_state = {"value": "whatever", "selection": "selected"}

        pressed, mock_iit, mock_pka = self._run_selection_probe(
            handler, clipboard_state, safe_copy_ok=False,
            request_id="r-31-nd")

        assert ("ctrl", "c") not in pressed
        handler.verbatim_insert_text.assert_not_called()
        mock_iit.assert_not_called()
        mock_pka.assert_not_called()
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-31-nd"
        assert msgs[0]["action"] == "wrap_or_insert"


# ============================================================================
# intelligent_insert_text - delivery outcome return (wh-review-pattern-fixes.11)
# ============================================================================


class TestIntelligentInsertTextReturn:
    """intelligent_insert_text returns True only when text was delivered.

    A pre-send rejection resolves the caller's Future as a success
    (wh-zndq) but delivers nothing, so it must return False. Strategy
    failures and handled insertion exceptions also return False.
    """

    def _insert(self, handler, insert_result):
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.clipboard.last_clipboard_write_seq = None

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = insert_result
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            return handler.intelligent_insert_text("word", request_id=None)

    def test_delivered_returns_true(self, handler):
        assert self._insert(handler, _ok(clipboard_dirty=False)) is True

    def test_strategy_failure_returns_false(self, handler):
        assert self._insert(handler, _fail(clipboard_dirty=False)) is False

    def test_pre_send_rejection_returns_false(self, handler):
        result = InsertionResult(
            success=True, clipboard_dirty=False,
            rejected_reason="no_text_target",
        )
        assert self._insert(handler, result) is False

    def test_handled_exception_returns_false(self, handler):
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.router.get_strategy.side_effect = RuntimeError("boom")

        with patch(f"{_MOD}.capture_context") as mock_ctx:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            assert handler.intelligent_insert_text(
                "word", request_id=None) is False


# ============================================================================
# Letter-buffer flush delivery outcome (wh-review-pattern-fixes.23)
# ============================================================================


class TestLetterBufferFlushOutcome:
    """The delivery-outcome contract must survive the letter-buffer
    boundary. _flush_letter_buffer returns the flush outcome; a failed
    flush makes intelligent_insert_text report failure instead of
    silently losing the buffered letters, and end_utterance surfaces a
    failed final flush through its logging channel."""

    def _insert_with_buffer(self, handler, insert_results, request_id="r-mix"):
        """Call intelligent_insert_text('word') with ['a','b'] buffered.
        ``insert_results`` feeds the strategy mock in call order (first
        call is the flush). Returns (delivered, strategy_mock)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.clipboard.last_clipboard_write_seq = None
        handler._letter_buffer = ["a", "b"]

        mock_strategy = MagicMock()
        mock_strategy.insert.side_effect = insert_results
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.auto_compress_spelled_letters",
                   return_value="ab"):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            delivered = handler.intelligent_insert_text(
                "word", request_id=request_id)

        return delivered, mock_strategy

    def test_mixed_outcome_failed_flush_returns_false(self, handler):
        """Buffered letters + failed flush must return False even though
        the current word's insertion would have succeeded. The word is
        skipped: delivering it after the lost letters would put
        out-of-order text on screen that no replay can repair."""
        delivered, mock_strategy = self._insert_with_buffer(
            handler,
            [
                InsertionResult(success=False, clipboard_dirty=False),
                InsertionResult(success=True, clipboard_dirty=False),
            ],
            request_id="r-mix-nd",
        )

        assert delivered is False
        # Only the flush reached the strategy; the word was skipped.
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["ab"]
        # The shadow buffer is marked stale: a False strategy result can
        # mean partial delivery (conservative-state model).
        handler.buffer_manager.invalidate.assert_called()
        # Exactly one Schema A error for the request_id.
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-mix-nd"
        assert msgs[0]["action"] == "intelligent_insert_text"

    def test_successful_flush_and_word_returns_true(self, handler):
        """Guard: a delivered flush followed by a delivered word keeps
        the True return and the single verified success response."""
        delivered, mock_strategy = self._insert_with_buffer(
            handler,
            [
                InsertionResult(success=True, clipboard_dirty=False),
                InsertionResult(success=True, clipboard_dirty=False),
            ],
            request_id="r-mix-ok",
        )

        assert delivered is True
        inserted = [c.args[0] for c in mock_strategy.insert.call_args_list]
        assert inserted == ["ab", "word"]
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-mix-ok"

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_end_utterance_failed_final_flush_logs_error(
            self, mock_cc, mock_ctx, mock_compress, handler, caplog):
        """A failed final flush must not vanish: end_utterance has no
        request_id (the generic dispatcher owns its response), so the
        existing failure channel is the log. It must log an ERROR and
        still run the rest of the utterance cleanup."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = InsertionResult(
            success=False, clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy
        handler._letter_buffer = ["a", "b"]

        with caplog.at_level(logging.ERROR):
            handler.end_utterance(1)

        error_lines = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.ERROR
            and "flush" in r.getMessage().lower()
        ]
        assert len(error_lines) == 1
        # Cleanup still runs -- the failed flush must not abort the
        # clipboard-restore path.
        handler.utterance_manager.end_utterance.assert_called_once_with(1)


# ============================================================================
# Letter-buffer flush log level for a refusal (wh-paste-when-unverified.2)
# ============================================================================


class TestLetterBufferRejectionLogLevel:
    """A refused final flush must not raise a Windows notification.

    ErrorNotificationHandler (utils/error_notifier.py) is a logging
    handler at ERROR level that utils/logging_setup.py attaches to the
    root logger, so an ERROR record IS the notification. A pre-send
    refusal is a deliberate no-op, not a fault: end_utterance logs it at
    WARNING. Every other flush failure keeps its ERROR.
    """

    @staticmethod
    def _end_utterance_with_flush_result(handler, result, utterance_id):
        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = result
        handler.router.get_strategy.return_value = mock_strategy
        handler._letter_buffer = ["a", "b"]
        handler.end_utterance(utterance_id)

    @staticmethod
    def _flush_errors(caplog):
        return [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR
            and "flush" in r.getMessage().lower()
        ]

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_rejected_final_flush_logs_warning_not_error(
            self, mock_cc, mock_ctx, mock_compress, handler, caplog):
        """A rejected InsertionResult makes the flush report False
        (success=True but was_rejected), and end_utterance must log that
        at WARNING so no notification fires."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        with caplog.at_level(logging.DEBUG):
            self._end_utterance_with_flush_result(
                handler,
                InsertionResult(
                    success=True, clipboard_dirty=False,
                    rejected_reason="default_reject",
                ),
                1,
            )

        assert self._flush_errors(caplog) == []
        warnings = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING
            and "end_utterance" in r.getMessage()
        ]
        assert len(warnings) == 1
        # Cleanup still runs, exactly as on the ERROR path.
        handler.utterance_manager.end_utterance.assert_called_once_with(1)

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_rejection_does_not_leak_into_a_later_plain_failure(
            self, mock_cc, mock_ctx, mock_compress, handler, caplog):
        """The recorded refusal is per insertion. A refused flush
        followed by a genuinely failed flush must still produce the
        ERROR, and therefore the notification, for the second one."""
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        with caplog.at_level(logging.DEBUG):
            self._end_utterance_with_flush_result(
                handler,
                InsertionResult(
                    success=True, clipboard_dirty=False,
                    rejected_reason="default_reject",
                ),
                1,
            )
        assert self._flush_errors(caplog) == []

        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            self._end_utterance_with_flush_result(
                handler,
                InsertionResult(success=False, clipboard_dirty=False),
                2,
            )
        assert len(self._flush_errors(caplog)) == 1

    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.clipboard_context")
    def test_rejection_does_not_leak_into_a_later_attempt_that_raises(
            self, mock_cc, mock_ctx, mock_compress, handler, caplog):
        """The per-attempt reset, not the recording line, is what clears
        a refusal for an attempt that never reads a result.

        The sibling test above drives its second flush with a plain
        failed result, whose ``was_rejected`` is False, so the recording
        line clears the flag there whether or not the reset ran. The
        exception arm reaches neither: the strategy raises, the handler
        returns False from its ``except`` block, and the recording line
        is never executed. Only the reset at the top of the attempt
        separates that fault from the refusal recorded before it, so
        end_utterance must still log ERROR and raise its notification.
        """
        mock_ctx.return_value = _make_context(focused_control=MagicMock())
        handler.utterance_manager.is_in_utterance.return_value = False

        # First attempt: refused before the send, so the handler records
        # the refusal from the result it reads.
        with caplog.at_level(logging.DEBUG):
            self._end_utterance_with_flush_result(
                handler,
                InsertionResult(
                    success=True, clipboard_dirty=False,
                    rejected_reason="default_reject",
                ),
                1,
            )
        assert self._flush_errors(caplog) == []

        # Second attempt: the strategy raises, so the handler leaves
        # through its exception arm without recording any outcome.
        caplog.clear()
        raising_strategy = MagicMock()
        raising_strategy.insert.side_effect = RuntimeError("strategy exploded")
        handler.router.get_strategy.return_value = raising_strategy
        handler._letter_buffer = ["a", "b"]
        with caplog.at_level(logging.DEBUG):
            handler.end_utterance(2)

        # The handler's own exception log carries no "flush", so this
        # counts only the end_utterance record.
        assert len(self._flush_errors(caplog)) == 1
        refusal_warnings = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING
            and "end_utterance" in r.getMessage()
        ]
        assert refusal_warnings == []
        handler.utterance_manager.end_utterance.assert_called_with(2)


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
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_press_key_calls_the_verified_primitive(
        self, mock_pk, mock_ctx, handler
    ):
        """Should call verified_press_keys for the specified key."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.press_key_action("enter")
        mock_pk.assert_called_once_with("enter", caller_notifies=True)

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_enter_submits_terminal_editor_when_active(self, mock_pk, mock_ctx, handler):
        """Enter key should submit terminal editor when it's active."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = True
        handler.press_key_action("enter", repeat=2)
        handler.terminal_editor.submit.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_press_key_repeats(self, mock_pk, mock_ctx, handler):
        """Repeat parameter should call press_keys multiple times."""
        mock_ctx.return_value = _make_context()
        handler.press_key_action("tab", repeat=3)
        assert mock_pk.call_count == 3

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_cache_invalidating_key_invalidates_buffer(self, mock_pk, mock_ctx, handler):
        """Caret-moving keys such as backspace invalidate the buffer."""
        mock_ctx.return_value = _make_context()
        handler.press_key_action("backspace")
        handler.buffer_manager.invalidate.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_letter_key_invalidates_shadow_buffer(self, mock_pk, mock_ctx, handler):
        """A plain letter key press must invalidate the shadow buffer.

        Review wh-review-pattern-fixes.5: a press pattern that types
        content ("press a", "press 5", "press space", "press .")
        changes the target text and moves the caret. The keyboard
        listener ignores WheelHouse's own synthetic input while an
        internal action runs, so the action itself must invalidate.
        The hotkey_action rationale applies verbatim to a single key:
        the target application decides what the key does, and the cost
        of an unnecessary invalidation is one UIA re-sync on the next
        dictated word.
        """
        from ui.shadow_buffer import ShadowBufferManager

        buffer = ShadowBufferManager()
        typed_so_far = "hello"
        buffer.update_from_clipboard_data(typed_so_far, len(typed_so_far), 0)
        assert buffer.is_valid is True
        handler.buffer_manager = buffer

        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.press_key_action("a")

        assert buffer.is_valid is False

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_press_key_exception_not_raised(self, mock_pk, mock_ctx, handler):
        """Exception should be caught and logged, not raised."""
        mock_ctx.return_value = _make_context()
        mock_pk.side_effect = RuntimeError("key press failed")
        handler.press_key_action("enter")  # Should not raise

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_every_key_invalidates_buffer(self, mock_pk, mock_ctx, handler):
        """Every pressed key triggers invalidation, content keys included.

        Review wh-review-pattern-fixes.5 replaced the old
        CACHE_INVALIDATING_KEYS allowlist with an unconditional
        invalidation. The representative set below covers the former
        allowlist plus the content-typing keys the allowlist missed.
        """
        mock_ctx.return_value = _make_context()
        representative_keys = [
            'backspace', 'enter', 'delete', 'tab', 'left', 'right', 'up',
            'down', 'home', 'end', 'pageup', 'pagedown',
            'a', '5', 'space', '.',
        ]

        for key in representative_keys:
            handler.buffer_manager.invalidate.reset_mock()
            handler.press_key_action(key)
            assert handler.buffer_manager.invalidate.call_count == 1, \
                f"Key '{key}' should invalidate buffer"


# ============================================================================
# hotkey_action
# ============================================================================

class TestHotkeyAction:
    """hotkey_action tests."""

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_hotkey_standard_app(self, mock_pk, mock_ctx, handler):
        """Standard app should use press_keys with SendInput."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.hotkey_action(["ctrl", "c"])
        mock_pk.assert_called_once_with(
            "ctrl", "c", caller_notifies=True
        )

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_enter_hotkey_submits_terminal_editor_when_active(self, mock_pk, mock_ctx, handler):
        """Enter hotkey should submit terminal editor when it's active."""
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = True
        handler.hotkey_action(["enter"], repeat=3)
        handler.terminal_editor.submit.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_hotkey_repeat(self, mock_pk, mock_ctx, handler):
        """Repeat parameter should execute hotkey multiple times."""
        mock_ctx.return_value = _make_context()
        handler.hotkey_action(["ctrl", "z"], repeat=3)
        assert mock_pk.call_count == 3

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
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
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_hotkey_exception_not_raised(self, mock_pk, mock_ctx, handler):
        """Exception should be caught and logged, not raised."""
        mock_ctx.return_value = _make_context()
        mock_pk.side_effect = RuntimeError("hotkey failed")
        handler.hotkey_action(["ctrl", "s"])  # Should not raise

    # ------------------------------------------------------------------
    # wh-space-after-hotkey-linebreak
    # ------------------------------------------------------------------

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_hotkey_invalidates_shadow_buffer(self, mock_pk, mock_ctx, handler):
        """Every hotkey must invalidate the shadow buffer.

        A hotkey is a command, not dictation. It can move the caret or
        change the text in the target (shift+enter, ctrl+v, ctrl+z,
        ctrl+a, ctrl+home). WheelHouse cannot model the result, so the
        remembered copy of the target text is unusable afterwards. The
        keyboard listener cannot cover this: input_proc's
        _should_emit_keyboard_invalidation returns False while an
        internal action runs, which is exactly when these keys are sent.
        """
        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.hotkey_action(["shift", "enter"], repeat=2)
        handler.buffer_manager.invalidate.assert_called_once()

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_no_leading_space_on_word_after_hotkey_line_break(
        self, mock_pk, mock_ctx, handler,
    ):
        """The word after a hotkey line break must not gain a leading space.

        Replays the 2026-08-15 21:17 reproduction from wheelhouse.log:
        the user dictated '...of an inappropriate', said 'new paragraph'
        (patterns.toml sends shift+enter twice through hotkey_action),
        then dictated 'space at'. The insertion arrived as 9 characters
        for an 8-character string, first codepoint 0x0020.

        This test uses a real ShadowBufferManager and a real
        TextPerfector, because the defect is that the stale remembered
        characters 'te' reach the spacing rule.
        """
        from ui.shadow_buffer import ShadowBufferManager
        from ui.text_perfector import TextPerfector

        buffer = ShadowBufferManager()
        typed_so_far = "This is an example of an inappropriate"
        buffer.update_from_clipboard_data(typed_so_far, len(typed_so_far), 0)
        handler.buffer_manager = buffer
        assert buffer.get_context()["preceding_chars"] == "te"

        mock_ctx.return_value = _make_context()
        handler.terminal_editor.is_active = False
        handler.hotkey_action(["shift", "enter"], repeat=2)

        final_string = TextPerfector().perfected_string(
            "space at", **buffer.get_context()
        )
        assert not final_string.startswith(" "), (
            f"word after a hotkey line break gained a leading space: "
            f"{final_string!r}"
        )

        # VerifiedUnicodeStrategy re-syncs only when the buffer is
        # invalid, so an invalid buffer here is what forces a fresh read
        # of the real text instead of the pre-hotkey characters.
        assert buffer.is_valid is False


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

    def test_a_missing_backend_is_still_logged_here(self, handler, caplog):
        """No plyer must leave a log line at this site, not silence.

        wh-notice-length-guard. Before the guard, this method imported
        plyer itself, so a missing plyer raised ImportError and the
        method's own outer handler wrote "Failed to show notification".
        The guard reports a missing backend by returning False instead
        of raising, so that line only survives if this site reads the
        return value. Nothing else here records the loss: the caller
        gets None either way.
        """
        import logging
        import sys

        original_plyer = sys.modules.get("plyer")
        try:
            sys.modules["plyer"] = None
            with caplog.at_level(logging.WARNING):
                handler.show_notification("Title", "Message")
        finally:
            if original_plyer is not None:
                sys.modules["plyer"] = original_plyer
            else:
                sys.modules.pop("plyer", None)

        written = " ".join(r.getMessage() for r in caplog.records)

        assert "notification" in written.lower(), (
            f"a missing plyer left no record here; log held: {written!r}"
        )


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
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
    def test_press_key_with_unexpected_kwargs(self, mock_pk, mock_ctx, handler):
        """press_key_action should warn about unexpected request_id."""
        mock_ctx.return_value = _make_context()
        # Should not raise
        handler.press_key_action("enter", request_id="unexpected")

    @patch(f"{_MOD}.capture_context")
    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 2, 2))
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
            mock_iit.assert_called_once_with(
                "()", request_id=None, defer_single_letter=False)

    @patch(f"{_MOD}.verified_press_keys", return_value=(True, 4, 4))
    @patch(f"{_MOD}.press_keys")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_transform_selection_no_request_id(self, mock_ctx, mock_cc,
                                                mock_pk, mock_vpk, handler):
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

    _TS = f"{_MOD}.type_string_verified"

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_calls_the_verified_primitive(self, mock_ts, handler):
        """type_text sends through type_string_verified, not type_string."""
        handler.type_text("hello")
        mock_ts.assert_called_once_with("hello", caller_notifies=True)

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_empty_string(self, mock_ts, handler):
        """Empty string should still be forwarded to type_string."""
        handler.type_text("")
        mock_ts.assert_called_once_with("", caller_notifies=True)

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_special_characters(self, mock_ts, handler):
        """Special characters should be passed through to type_string."""
        handler.type_text("hello world!\n")
        mock_ts.assert_called_once_with(
            "hello world!\n", caller_notifies=True
        )

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_accepts_extra_kwargs(self, mock_ts, handler):
        """type_text should accept and ignore extra kwargs (like request_id)."""
        handler.type_text("test", request_id="r1", foo="bar")
        mock_ts.assert_called_once_with("test", caller_notifies=True)

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_exception_not_raised(self, mock_ts, handler):
        """Exception in type_string should be caught, not raised."""
        mock_ts.side_effect = RuntimeError("SendInput failed")
        handler.type_text("test")  # Should not raise

    @patch(_TS, return_value=(True, 0, None))
    def test_type_text_invalidates_shadow_buffer(self, mock_ts, handler):
        """type_text must invalidate the shadow buffer.

        Review wh-review-pattern-fixes.5: type_text sends raw keystrokes
        that change the target text and caret. The keyboard listener
        ignores WheelHouse's own synthetic input while an internal
        action runs, so the action itself must invalidate. Without the
        invalidation, the next dictated word perfects against the
        pre-typing context (wrong leading space / capitalization).
        """
        from ui.shadow_buffer import ShadowBufferManager

        buffer = ShadowBufferManager()
        typed_so_far = "hello literal"
        buffer.update_from_clipboard_data(typed_so_far, len(typed_so_far), 0)
        assert buffer.is_valid is True
        handler.buffer_manager = buffer

        handler.type_text("gpu")

        # VerifiedUnicodeStrategy re-syncs only when the buffer is
        # invalid, so an invalid buffer here is what forces a fresh
        # read of the real text instead of the pre-typing characters.
        assert buffer.is_valid is False


# ============================================================================
# wh-review-pattern-fixes.8: every voice-driven focus/text mutation must
# invalidate the shadow buffer before its physical dispatch
# ============================================================================


def _valid_real_buffer():
    """Build a real ShadowBufferManager that reports is_valid True."""
    from ui.shadow_buffer import ShadowBufferManager

    buffer = ShadowBufferManager()
    typed_so_far = "hello"
    buffer.update_from_clipboard_data(typed_so_far, len(typed_so_far), 0)
    assert buffer.is_valid is True
    return buffer


class _FakeClickForeground:
    """Stand-in for the ForegroundContext _capture_click_foreground returns."""

    foreground_window = 1000
    foreground_pid = 4321
    foreground_process_name = "notepad.exe"
    foreground_window_creation_time = 99
    cursor_at_walk = (60, 45)
    cursor_monitor_id = 0


class TestVoiceMutationInvalidatesShadowBuffer:
    """wh-review-pattern-fixes.8: the input listeners suppress invalidation
    while an internal action runs, so each synthetic click/drag/paste path
    must invalidate the shadow buffer itself, BEFORE the dispatch, so a
    partial failure also leaves the buffer invalid. Early validation
    failures that dispatch nothing keep the buffer valid.
    """

    # ------------------------------------------------------------------
    # click_snapshot_item
    # ------------------------------------------------------------------

    def test_click_snapshot_item_success_invalidates_shadow_buffer(self, handler):
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        match = MagicMock()
        match.item_id = "item-1"
        match.name = "Cancel"
        match.role = "Button"
        snapshot = MagicMock()
        snapshot.matches = [match]
        finder = MagicMock()
        finder.get_snapshot.return_value = snapshot
        finder.describe_snapshot_miss.return_value = None
        executor = MagicMock()
        executor.click.return_value = MagicMock(
            outcome="ok", reason=None, matched_name="Cancel",
            clicked_via="invoke",
        )

        with patch.object(handler, "_get_overlay_walk_finder",
                          return_value=finder), \
             patch.object(handler, "_get_click_executor",
                          return_value=executor), \
             patch(f"{_MOD}._capture_click_foreground",
                   return_value=_FakeClickForeground()):
            handler.click_snapshot_item(
                snapshot_id="s1", item_id="item-1",
                request_id="req-1", trace_id="t1",
            )

        executor.click.assert_called_once()
        assert buffer.is_valid is False

    def test_click_snapshot_item_expired_snapshot_keeps_buffer_valid(self, handler):
        """No click dispatch happens on snapshot_expired -- no invalidation."""
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        finder = MagicMock()
        finder.get_snapshot.return_value = None
        finder.describe_snapshot_miss.return_value = "ttl_expired"

        with patch.object(handler, "_get_overlay_walk_finder",
                          return_value=finder), \
             patch(f"{_MOD}._capture_click_foreground",
                   return_value=_FakeClickForeground()):
            handler.click_snapshot_item(
                snapshot_id="s1", item_id="item-1",
                request_id="req-1", trace_id="t1",
            )

        assert buffer.is_valid is True

    # ------------------------------------------------------------------
    # click_element
    # ------------------------------------------------------------------

    def test_click_element_success_invalidates_shadow_buffer(self, handler):
        from ui.element_types import ElementQuery

        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        query = ElementQuery(
            name="cancel", role=None, ordinal=None, spatial=None,
            raw_utterance="click cancel",
        )
        winner = MagicMock()
        winner.name = "Cancel"
        outcome = MagicMock()
        outcome.outcome = "ok"
        outcome.winner = winner
        find_result = MagicMock()
        find_result.outcome = outcome
        find_result.summary = None
        find_result.snapshot.snapshot_id = "s1"
        finder = MagicMock()
        finder.find.return_value = find_result
        executor = MagicMock()
        executor.click.return_value = MagicMock(
            outcome="ok", reason=None, matched_name="Cancel",
            clicked_via="invoke",
        )

        with patch.object(handler, "_get_click_element_finder",
                          return_value=finder), \
             patch.object(handler, "_get_click_executor",
                          return_value=executor), \
             patch(f"{_MOD}._capture_click_foreground",
                   return_value=_FakeClickForeground()):
            handler.click_element(query=query, trace_id="t1",
                                  request_id="req-1")

        executor.click.assert_called_once()
        assert buffer.is_valid is False

    def test_click_element_not_found_keeps_buffer_valid(self, handler):
        """A not_found walk dispatches no click -- no invalidation."""
        from ui.element_types import ElementQuery

        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        query = ElementQuery(
            name="cancel", role=None, ordinal=None, spatial=None,
            raw_utterance="click cancel",
        )
        outcome = MagicMock()
        outcome.outcome = "not_found"
        find_result = MagicMock()
        find_result.outcome = outcome
        find_result.summary = None
        find_result.snapshot.snapshot_id = "s1"
        finder = MagicMock()
        finder.find.return_value = find_result

        with patch.object(handler, "_get_click_element_finder",
                          return_value=finder), \
             patch(f"{_MOD}._capture_click_foreground",
                   return_value=_FakeClickForeground()):
            handler.click_element(query=query, trace_id="t1",
                                  request_id="req-1")

        assert buffer.is_valid is True

    # ------------------------------------------------------------------
    # click_point / perform_drag (mouse-grid SendInput seams)
    # ------------------------------------------------------------------

    def test_click_point_invalidates_shadow_buffer(self, handler):
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        handler._mouse_click_seam = MagicMock(return_value=(True, None))

        handler.click_point(x=10, y=20, request_id="req-1")

        handler._mouse_click_seam.assert_called_once()
        assert buffer.is_valid is False

    def test_click_point_disabled_by_config_keeps_buffer_valid(self, handler):
        """The disabled short-circuit dispatches nothing -- no invalidation."""
        from ui.click_config import ClickConfig

        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        handler._mouse_click_seam = MagicMock(return_value=(True, None))
        handler._click_config = ClickConfig.from_raw({"enabled": False})

        handler.click_point(x=10, y=20, request_id="req-1")

        handler._mouse_click_seam.assert_not_called()
        assert buffer.is_valid is True

    def test_perform_drag_invalidates_shadow_buffer(self, handler):
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        handler._mouse_drag_seam = MagicMock(return_value=(True, None))

        handler.perform_drag(start_x=1, start_y=2, end_x=3, end_y=4,
                             request_id="req-1")

        handler._mouse_drag_seam.assert_called_once()
        assert buffer.is_valid is False

    # ------------------------------------------------------------------
    # retry_dictation_by_token (direct ClipboardOnlyStrategy paste)
    # ------------------------------------------------------------------

    def test_retry_dictation_direct_paste_invalidates_shadow_buffer(self, handler):
        import uuid

        from ui.rejection_text_cache import RejectionTextCache

        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        token = str(uuid.uuid4())
        handler.rejection_text_cache = RejectionTextCache()
        # wh-ensure-focused-same-process-fallback.1.11/.1.12: HIT
        # entries need a full target identity (hwnd + root snapshot +
        # provenance tag) to reach the paste; incomplete entries
        # refuse with token_expired.
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy = MagicMock()
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare",
                 return_value=0x12345,
             ), \
             patch(
                 f"{_MOD}.read_hwnd_provenance",
                 return_value=7,
             ):
            mock_ctx.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy="clipboard_only",
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_called_once()
        assert buffer.is_valid is False

    def test_retry_dictation_unknown_token_keeps_buffer_valid(self, handler):
        """A cache MISS dispatches no paste -- no invalidation."""
        import uuid

        from ui.rejection_text_cache import RejectionTextCache

        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer

        handler.rejection_text_cache = RejectionTextCache()
        handler.clipboard_only_strategy = MagicMock()

        handler.retry_dictation_by_token(
            correlation_token=str(uuid.uuid4()),
            override_strategy="clipboard_only",
            request_id="req-1",
        )

        handler.clipboard_only_strategy.insert.assert_not_called()
        assert buffer.is_valid is True

    # ------------------------------------------------------------------
    # retract: partial backspace delivery
    # ------------------------------------------------------------------

    def _arm_retract(self, handler):
        """Set the gate state so retract() reaches send_backspaces."""
        handler._user_interacted_during_utterance = False
        handler._used_simple_paste = False
        handler.clipboard.last_paste_was_optimistic = False
        handler.clipboard.accumulated_has_grapheme_unsafe = False
        handler.clipboard.accumulated_paste_was_qt = False
        handler.clipboard.accumulated_paste_chars = 11
        handler.clipboard.accumulated_paste_clusters = 11
        handler._letter_buffer = []
        handler.window_manager._last_target_hwnd = None
        handler.text_target_predicate = None

    @patch(f"{_MOD}.send_backspaces", return_value=False)
    def test_partial_retract_invalidates_shadow_buffer(self, mock_bs, handler):
        """send_backspaces reporting partial delivery must leave the buffer
        invalid: an unknown number of backspaces landed, so the remembered
        copy of the target text is unusable.
        """
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        self._arm_retract(handler)

        result = handler.retract()

        assert result["status"] == "not_retracted"
        assert result["reason"] == "partial_send"
        assert buffer.is_valid is False

    @patch(f"{_MOD}.send_backspaces", return_value=True)
    def test_full_retract_still_invalidates_shadow_buffer(self, mock_bs, handler):
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        self._arm_retract(handler)

        result = handler.retract()

        assert result["status"] == "retracted"
        assert buffer.is_valid is False

    def test_retract_gate_refusal_keeps_shadow_buffer_valid(self, handler):
        """A gate refusal before any backspace dispatch keeps the buffer."""
        buffer = _valid_real_buffer()
        handler.buffer_manager = buffer
        self._arm_retract(handler)
        handler._user_interacted_during_utterance = True

        result = handler.retract()

        assert result["reason"] == "user_interacted"
        assert buffer.is_valid is True


# ============================================================================
# wh-review-pattern-fixes.35: verified Ctrl+C in the selection probes
# ============================================================================


class TestTransformSelectionVerifiedCopy:
    """wh-review-pattern-fixes.35: the selection copy must not be
    fire-and-forget. press_keys discards the SendInput count, and an
    undelivered Ctrl+C leaves the sentinel unchanged -- which is
    indistinguishable from a genuine empty selection. A short delivery
    must fail the operation closed (a copy-failure response, never the
    false "No text selected"), release a possibly-held Ctrl, and touch
    nothing. A VERIFIED Ctrl+C with an unchanged sentinel remains a
    genuine empty selection."""

    def _run(self, handler, vpk_result, clipboard_changes=False,
             request_id="r-35-ts"):
        """Drive transform_selection through the non-Flutter copy path.

        ``vpk_result`` is what verified_press_keys reports for the
        Ctrl+C chord. When ``clipboard_changes`` is True a delivered
        copy also changes the polled clipboard value (a selection
        exists). Returns (vpk_calls, press_keys mock, keyups mock)."""
        handler.clipboard.clipboard_verification_timeout = 0.01
        handler._letter_buffer = []
        handler.verbatim_insert_text = MagicMock(return_value=True)
        clipboard_state = {"value": "user clipboard"}

        def fake_safe_copy(text):
            clipboard_state["value"] = text
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        vpk_calls = []

        def fake_vpk(*keys):
            vpk_calls.append(keys)
            if keys == ("ctrl", "c") and vpk_result[0] and clipboard_changes:
                clipboard_state["value"] = "selected text"
            return vpk_result

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys") as mock_pk, \
             patch(f"{_MOD}.verified_press_keys", side_effect=fake_vpk), \
             patch(f"{_MOD}._send_modifier_keyups") as mock_rel, \
             patch("pyperclip.paste",
                   side_effect=lambda: clipboard_state["value"]), \
             patch("pyperclip.copy"):
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.transform_selection("snake_case", request_id=request_id)

        return vpk_calls, mock_pk, mock_rel

    def test_short_ctrl_c_reports_copy_failure_not_no_selection(self, handler):
        vpk_calls, mock_pk, mock_rel = self._run(
            handler, vpk_result=(False, 1, 4))

        assert ("ctrl", "c") in vpk_calls
        # The copy must not ride fire-and-forget press_keys.
        assert not any(c.args == ("ctrl", "c")
                       for c in mock_pk.call_args_list)
        # Fail closed: nothing transformed, nothing pasted.
        handler.selection_transformer.apply_transformation.assert_not_called()
        handler.verbatim_insert_text.assert_not_called()
        # A short chord can leave Ctrl held -- release it.
        mock_rel.assert_called_once_with(("ctrl",))
        resp = handler.response_queue.put.call_args[0][0]
        assert resp["success"] is False
        assert "No text selected" not in resp["message"]

    def test_verified_ctrl_c_unchanged_sentinel_is_no_selection(self, handler):
        """The existing no-selection behavior is preserved when the copy
        verifiably delivered and the sentinel still did not change."""
        vpk_calls, _, mock_rel = self._run(
            handler, vpk_result=(True, 4, 4), clipboard_changes=False)

        assert ("ctrl", "c") in vpk_calls
        mock_rel.assert_not_called()
        handler.selection_transformer.apply_transformation.assert_not_called()
        resp = handler.response_queue.put.call_args[0][0]
        assert resp["success"] is False
        assert "No text selected" in resp["message"]

    def test_verified_ctrl_c_with_selection_still_transforms(self, handler):
        handler.selection_transformer.apply_transformation.return_value = "x"
        self._run(handler, vpk_result=(True, 4, 4), clipboard_changes=True)

        handler.selection_transformer.apply_transformation.assert_called_once_with(
            "selected text", "snake_case"
        )


class TestWrapOrInsertVerifiedCopy:
    """wh-review-pattern-fixes.35: a short Ctrl+C in the selection probe
    must fail the wrap closed with one Schema A error -- never fall
    through to Priority 2, which would insert empty fences OVER the
    still-active selection (data loss). A VERIFIED Ctrl+C with an
    unchanged sentinel keeps the existing Priority 2 fall-through."""

    def _run(self, handler, vpk_result, clipboard_changes=False,
             request_id="r-35-wr"):
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = 0.0  # stale -> check
        handler.clipboard.clipboard_verification_timeout = 0.01
        handler._letter_buffer = []
        handler.verbatim_insert_text = MagicMock(return_value=True)
        clipboard_state = {"value": "user clipboard"}

        def fake_safe_copy(text):
            clipboard_state["value"] = text
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        vpk_calls = []

        def fake_vpk(*keys):
            vpk_calls.append(keys)
            if keys == ("ctrl", "c") and vpk_result[0] and clipboard_changes:
                clipboard_state["value"] = "selected text"
            return vpk_result

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys") as mock_pk, \
             patch(f"{_MOD}.verified_press_keys", side_effect=fake_vpk), \
             patch(f"{_MOD}._send_modifier_keyups") as mock_rel, \
             patch("pyperclip.paste",
                   side_effect=lambda: clipboard_state["value"]), \
             patch("pyperclip.copy"), \
             patch.object(handler, 'intelligent_insert_text') as mock_iit, \
             patch.object(handler, 'press_key_verified',
                          return_value={"success": True}) as mock_pkv:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return vpk_calls, mock_pk, mock_rel, mock_iit, mock_pkv

    def test_short_ctrl_c_fails_closed_no_priority2(self, handler):
        vpk_calls, mock_pk, mock_rel, mock_iit, mock_pka = self._run(
            handler, vpk_result=(False, 1, 4), request_id="r-35-nd")

        assert ("ctrl", "c") in vpk_calls
        assert not any(c.args == ("ctrl", "c")
                       for c in mock_pk.call_args_list)
        # No wrap, no Priority 2 insertion, no caret move.
        handler.verbatim_insert_text.assert_not_called()
        mock_iit.assert_not_called()
        mock_pka.assert_not_called()
        mock_rel.assert_called_once_with(("ctrl",))
        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-35-nd"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_verified_ctrl_c_unchanged_sentinel_falls_to_priority2(
            self, handler):
        vpk_calls, _, mock_rel, mock_iit, mock_pka = self._run(
            handler, vpk_result=(True, 4, 4), clipboard_changes=False)

        assert ("ctrl", "c") in vpk_calls
        mock_rel.assert_not_called()
        mock_iit.assert_called_once_with(
            "()", request_id=None, defer_single_letter=False)

    def test_verified_ctrl_c_with_selection_wraps(self, handler):
        self._run(handler, vpk_result=(True, 4, 4), clipboard_changes=True,
                  request_id="r-35-ok")

        handler.verbatim_insert_text.assert_called_once_with(
            "(selected text)", "r-35-ok", captured_target=_CarriesAControl())


# ============================================================================
# wrap_or_insert Priority 2 caret move (wh-review-pattern-fixes.37)
# ============================================================================


class TestWrapOrInsertVerifiedCaretMove:
    """wh-review-pattern-fixes.37: the Left that positions the caret
    between the freshly inserted empty delimiters must be acknowledged.

    ``press_key_action`` sends through fire-and-forget ``press_keys``,
    discards the SendInput count, catches every exception, and returns
    None. A dropped Left therefore left the delimiters on screen with
    the caret AFTER the closing delimiter while wrap_or_insert reported
    PATH_INSERT_VERIFIED. The next dictated word then landed outside
    the delimiters although the voice feedback said the command
    completed. The press rides ``press_key_verified`` instead, and the
    verified success is emitted only when the press was accepted.
    """

    def _run(self, handler, left_result, request_id="r-37"):
        """Drive wrap_or_insert into Priority 2 with a delivered
        delimiter insertion and the given press_key_verified outcome.
        Returns (press_key_verified mock, press_key_action mock)."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = time.time()
        handler.clipboard.last_clipboard_write_seq = None

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch.object(handler, 'press_key_verified',
                          return_value=left_result) as mock_pkv, \
             patch.object(handler, 'press_key_action') as mock_pka:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id=request_id)

        return mock_pkv, mock_pka

    def test_left_move_rides_the_acknowledged_press(self, handler):
        """The caret move must go through press_key_verified, not the
        fire-and-forget press_key_action."""
        mock_pkv, mock_pka = self._run(
            handler, {"success": True}, request_id="r-37-ok")

        mock_pkv.assert_called_once_with("left", repeat=1)
        mock_pka.assert_not_called()

    def test_acknowledged_left_move_reports_insert_verified(self, handler):
        self._run(handler, {"success": True}, request_id="r-37-ok")

        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0]["status"] == "ok"
        assert msgs[0]["path"] == "insert_verified"
        assert msgs[0]["request_id"] == "r-37-ok"
        assert msgs[0]["action"] == "wrap_or_insert"

    def test_unacknowledged_left_move_does_not_claim_insert_verified(
            self, handler):
        """The delimiters landed but the caret is not between them:
        report that partial outcome, never the verified success."""
        self._run(handler, {"success": False}, request_id="r-37-nd")

        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        assert len(msgs) == 1
        assert msgs[0].get("error") is True
        assert msgs[0]["request_id"] == "r-37-nd"
        assert msgs[0]["action"] == "wrap_or_insert"
        assert all(m.get("path") != "insert_verified" for m in msgs)

    def test_unacknowledged_left_move_names_the_partial_outcome(
            self, handler):
        """The message must say the delimiters are present and the
        caret is not positioned -- not that the insertion failed."""
        self._run(handler, {"success": False}, request_id="r-37-msg")

        msgs = [c.args[0] for c in handler.response_queue.put.call_args_list]
        message = msgs[0]["message"].lower()
        assert "delimiter" in message
        assert "inserted" in message
        assert "caret" in message or "cursor" in message
        # Not the "nothing landed" wording of the .11 gate.
        assert "not delivered" not in message

    def test_unacknowledged_left_move_does_not_roll_back(self, handler):
        """No rollback: a backspace over text the caret may no longer
        sit behind would destroy the user's own content."""
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.utterance_manager._last_paste_time = time.time()
        handler.clipboard.last_clipboard_write_seq = None

        mock_strategy = MagicMock()
        mock_strategy.insert.return_value = _ok(clipboard_dirty=False)
        handler.router.get_strategy.return_value = mock_strategy

        with patch(f"{_MOD}.capture_context") as mock_ctx, \
             patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.press_keys") as mock_pk, \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(False, 1, 2)) as mock_vpk:
            mock_ctx.return_value = _make_context(focused_control=MagicMock())
            handler.wrap_or_insert("(", ")", text="", request_id="r-37-rb")

        # The only key traffic is the single Left attempt.
        assert mock_pk.call_args_list == []
        assert [c.args for c in mock_vpk.call_args_list] == [("left",)]
        # The delimiters were still inserted -- no undo insertion either.
        assert [c.args[0] for c in mock_strategy.insert.call_args_list] == \
            ["()"]


# ============================================================================
# select_phrase (wh-spoken-phrase-select.2)
# ============================================================================

class TestSelectPhrase:
    """The handler captures the focused control and hands the words to
    ui/uia_phrase_select.py. The search itself has its own tests in
    tests/test_ui/test_uia_phrase_select.py; these cover the wrapper.
    """

    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_passes_the_focused_control_and_the_phrase(
        self, mock_read, mock_select, handler
    ):
        control = MagicMock()
        mock_read.return_value = control
        mock_select.return_value = ("selected", "brown fox")

        handler.select_phrase("brown fox")

        mock_select.assert_called_once_with(control, "brown fox")

    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_returns_the_outcome(self, mock_read, mock_select, handler):
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("not_found", None)

        assert handler.select_phrase("purple cow") == "not_found"

    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_no_focused_control_reports_no_control(
        self, mock_read, mock_select, handler
    ):
        mock_read.return_value = None
        mock_select.return_value = ("no_control", None)

        assert handler.select_phrase("brown fox") == "no_control"

    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_the_log_line_does_not_carry_the_phrase(
        self, mock_read, mock_select, handler, caplog
    ):
        """The words a person speaks are theirs. wh-797.17 keeps spoken
        text out of the log by default, and this path is no different."""
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("selected", "hunter2")

        with caplog.at_level(logging.DEBUG):
            handler.select_phrase("my bank password is hunter2")

        assert "hunter2" not in caplog.text

    @patch(f"{_MOD}.read_focused_control")
    def test_a_focus_read_that_raises_reports_failed(self, mock_read, handler):
        mock_read.side_effect = OSError("the application is busy")

        assert handler.select_phrase("brown fox") == "failed"

    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.capture_context")
    def test_does_not_capture_the_whole_context(
        self, mock_ctx, mock_select, handler
    ):
        """wh-spoken-phrase-select.7.1. capture_context reads the class
        name, the process id and the top level window, and this method
        discards every one of them. Those reads cost time on the voice
        command path, and capture_context logs an ERROR when one of them
        raises."""
        mock_select.return_value = ("selected", "brown fox")

        handler.select_phrase("brown fox")

        assert not mock_ctx.called, (
            "select_phrase read the whole context and threw it away"
        )

    @patch(f"{_MOD}.select_phrase_in_control")
    def test_a_control_whose_metadata_raises_stays_quiet(
        self, mock_select, handler, caplog
    ):
        """wh-spoken-phrase-select.7.1. Every ERROR record on the root
        logger shows the user a notification box, and capture_context
        logs an ERROR when the class name or the process id raises. A
        select that succeeds must not show the user an error."""
        control = MagicMock()
        type(control).ClassName = PropertyMock(side_effect=OSError("stale"))
        type(control).ProcessId = PropertyMock(side_effect=OSError("stale"))
        mock_select.return_value = ("selected", None)

        with patch("uiautomation.GetFocusedControl", return_value=control):
            with caplog.at_level(logging.DEBUG):
                outcome = handler.select_phrase("brown fox")

        assert outcome == "selected"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], f"select_phrase logged an error: {errors}"




class TestSelectPhraseTellsTheUser:
    """wh-spoken-phrase-select.3. Five of the six outcomes leave the
    screen unchanged. The handler shows a Windows notification for each
    of them, which a screen reader announces. The wording lives in
    ui/phrase_select_notice.py and has its own tests in
    tests/test_ui/test_phrase_select_notice.py.
    """

    @patch(f"{_MOD}.notify_outcome")
    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_a_select_that_worked_shows_nothing(
        self, mock_read, mock_select, mock_notify, handler
    ):
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("selected", "brown fox")

        handler.select_phrase("brown fox")

        outcome = mock_notify.call_args[0][0]
        assert outcome == "selected"

    @patch(f"{_MOD}.notify_outcome")
    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_words_that_are_absent_reach_the_notifier(
        self, mock_read, mock_select, mock_notify, handler
    ):
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("not_found", None)

        handler.select_phrase("purple cow")

        outcome, phrase = mock_notify.call_args[0][0:2]
        assert outcome == "not_found"
        assert phrase == "purple cow"

    @patch(f"{_MOD}.notify_outcome")
    @patch(f"{_MOD}.read_focused_control")
    def test_a_failure_before_the_search_reaches_the_notifier(
        self, mock_read, mock_notify, handler
    ):
        """The early return had no report at all before this change."""
        mock_read.side_effect = OSError("no focus")

        assert handler.select_phrase("brown fox") == "failed"

        outcome, phrase = mock_notify.call_args[0][0:2]
        assert outcome == "failed"
        assert phrase == "brown fox"

    @patch(f"{_MOD}.get_notifier_worker")
    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_the_handler_passes_the_process_notifier_worker(
        self, mock_read, mock_select, mock_worker, handler
    ):
        """The worker comes from utils.logging_setup, which input_proc
        populates when it calls setup_logging."""
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("not_found", None)
        worker = MagicMock()
        mock_worker.return_value = worker

        handler.select_phrase("purple cow")

        assert worker.submit.called

    @patch(f"{_MOD}.notify_outcome")
    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_a_notifier_that_raises_does_not_change_the_outcome(
        self, mock_read, mock_select, mock_notify, handler
    ):
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("selected", "brown fox")
        mock_notify.side_effect = RuntimeError("notifier is gone")

        assert handler.select_phrase("brown fox") == "selected"

    @patch(f"{_MOD}.notify_outcome")
    @patch(f"{_MOD}.select_phrase_in_control")
    @patch(f"{_MOD}.read_focused_control")
    def test_a_notifier_that_raises_never_logs_the_phrase(
        self, mock_read, mock_select, mock_notify, handler, caplog
    ):
        mock_read.return_value = MagicMock()
        mock_select.return_value = ("not_found", None)
        mock_notify.side_effect = RuntimeError("notifier is gone")

        with caplog.at_level(logging.DEBUG):
            handler.select_phrase("my bank password is hunter2")

        assert "hunter2" not in caplog.text


# ============================================================================
# wh-keyboard-refusal-notice: a failed key press, hotkey or typing says so
# ============================================================================

class TestTheKeyboardActionsWriteTheirOwnNotice:
    """wh-keyboard-refusal-notice, criteria 1 to 3.

    Until this change ``press_key_action``, ``hotkey_action`` and
    ``type_text`` called the fire-and-forget primitives ``press_keys`` and
    ``type_string``, which return None. The callers learned nothing, so the
    only user-visible signal was the generic ``[ERROR]`` box that
    ``ErrorNotificationHandler`` shows for every ERROR record.

    That box is not proof the user was told. ``ErrorNotificationHandler.emit``
    keys on (logger name, level, message) and returns without submitting
    anything when the same key repeats inside ``rate_limit_seconds``, which
    ``utils/logging_setup.py`` sets to 10. Two identical failed hotkeys inside
    ten seconds produced ONE box. The same measurement closed
    wh-wheel-refusal-notice.1.7, and the wheel handler answered it the same
    way these three do now: the caller writes the notice, and the primitive
    records the refusal below ERROR so the user gets exactly one box.
    """

    def test_an_unknown_key_name_shows_one_notice_naming_the_key(
        self, handler
    ):
        from ui.ui_action_handler import PRESS_KEY_UNKNOWN_MESSAGE

        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)

            handler.press_key_action("shhift")

        assert notice.call_count == 1
        assert notice.call_args.args[1] == PRESS_KEY_UNKNOWN_MESSAGE.format(
            keys="shhift"
        )

    def test_a_short_key_send_shows_one_notice_naming_the_key(self, handler):
        from ui.ui_action_handler import PRESS_KEY_REFUSED_MESSAGE

        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 1, 2)

            handler.press_key_action("tab")

        assert notice.call_count == 1
        assert notice.call_args.args[1] == PRESS_KEY_REFUSED_MESSAGE.format(
            keys="tab"
        )

    def test_a_short_key_press_releases_the_key(self, handler):
        """wh-keyboard-refusal-notice.1.1: the notice is not the whole job.

        ``(False, 1, 2)`` for "tab" is the down accepted and the up
        dropped, so the key stays physically held and Windows
        auto-repeats it into the focused application while the notice
        says the press did not work. Same shape as press_key_verified.
        """
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups") as release, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (False, 1, 2)

            handler.press_key_action("tab")

        release.assert_called_once_with(("tab",))

    def test_an_unrecognised_key_name_releases_nothing(self, handler):
        """``(False, 0, 0)`` sent nothing, so there is nothing to release.

        Releasing here would log the primitive's own "unmapped key;
        skipping" WARNING and release a key that was never pressed.
        """
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups") as release, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)

            handler.press_key_action("shhift")

        release.assert_not_called()

    def test_a_successful_key_press_shows_no_notice(self, handler):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (True, 2, 2)

            handler.press_key_action("tab")

        assert notice.call_count == 0

    def test_press_key_action_asks_the_primitive_to_stay_below_error(
        self, handler
    ):
        """The flag is what keeps the generic box from doubling the notice."""
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (True, 2, 2)

            handler.press_key_action("tab")

        assert press.call_args.kwargs.get("caller_notifies") is True

    def test_an_unknown_key_inside_a_chord_is_named_in_the_notice(
        self, handler
    ):
        """Ruling B: the handler reads VK_CODE_MAP and names the bad key.

        ``verified_press_keys`` returns ``(False, 0, 0)`` for an unmapped key
        and never says WHICH key was wrong, so a chord notice that only
        repeated the chord would leave the user guessing.
        """
        from ui.ui_action_handler import HOTKEY_UNKNOWN_MESSAGE

        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)

            handler.hotkey_action(["ctrl", "shhift", "s"])

        assert notice.call_count == 1
        assert notice.call_args.args[1] == HOTKEY_UNKNOWN_MESSAGE.format(
            chord="ctrl + shhift + s", keys="shhift"
        )

    def test_a_short_hotkey_send_shows_one_notice_naming_the_chord(
        self, handler
    ):
        from ui.ui_action_handler import HOTKEY_REFUSED_MESSAGE

        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 3, 6)

            handler.hotkey_action(["ctrl", "c"])

        assert notice.call_count == 1
        assert notice.call_args.args[1] == HOTKEY_REFUSED_MESSAGE.format(
            chord="ctrl + c"
        )

    def test_a_short_hotkey_releases_the_chord(self, handler):
        """A short chord leaves Ctrl held and every later key runs with it.

        ``(False, 1, 4)`` for ctrl+c is the ctrl-down accepted and the
        rest dropped. Until the release the modifier is held in the
        Windows keyboard state, so the next dictated words execute as
        shortcuts.
        """
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups") as release, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (False, 1, 4)

            handler.hotkey_action(["ctrl", "c"])

        release.assert_called_once_with(("ctrl", "c"))

    def test_an_unrecognised_hotkey_key_releases_nothing(self, handler):
        """``(False, 0, 0)`` sent nothing, so there is nothing to release."""
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}._send_modifier_keyups") as release, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)

            handler.hotkey_action(["ctrl", "shhift", "s"])

        release.assert_not_called()

    def test_hotkey_action_asks_the_primitive_to_stay_below_error(
        self, handler
    ):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice"):
            ctx.return_value = _make_context()
            press.return_value = (True, 6, 6)

            handler.hotkey_action(["ctrl", "c"])

        assert press.call_args.kwargs.get("caller_notifies") is True

    def test_a_successful_hotkey_shows_no_notice(self, handler):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (True, 6, 6)

            handler.hotkey_action(["ctrl", "c"])

        assert notice.call_count == 0

    def test_stopped_typing_shows_one_notice_saying_where_it_stopped(
        self, handler
    ):
        """Criterion 2: the notice says typing stopped and where.

        The typed characters never reach the notice. The counts are the
        report; the text is the user's own dictated content.
        """
        from ui.ui_action_handler import TYPING_REFUSED_MESSAGE

        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice") as notice:
            typing.return_value = (False, 4, "partial: 8/22 events")

            handler.type_text("my bank password")

        assert notice.call_count == 1
        assert notice.call_args.args[1] == TYPING_REFUSED_MESSAGE.format(
            sent=4, total=16
        )
        assert "my bank password" not in str(notice.call_args)

    def test_type_text_asks_the_primitive_to_stay_below_error(self, handler):
        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice"):
            typing.return_value = (True, 5, None)

            handler.type_text("hello")

        assert typing.call_args.kwargs.get("caller_notifies") is True

    def test_successful_typing_shows_no_notice(self, handler):
        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice") as notice:
            typing.return_value = (True, 5, None)

            handler.type_text("hello")

        assert notice.call_count == 0

    def test_two_identical_failures_inside_ten_seconds_show_two_notices(
        self, handler
    ):
        """Criterion 3, the whole reason this bead exists.

        ``ErrorNotificationHandler`` drops the second of two identical ERROR
        records inside ten seconds. ``send_notice`` does not go through that
        handler at all, so the second failure still reports.
        """
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)

            handler.hotkey_action(["ctrl", "shhift", "s"])
            handler.hotkey_action(["ctrl", "shhift", "s"])

        assert notice.call_count == 2

    def test_a_notice_that_itself_fails_never_logs_an_error(
        self, handler, caplog
    ):
        """An ERROR here would pop the very box the notice replaces."""
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)
            notice.side_effect = [RuntimeError("the notifier is gone"), None]

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.press_key_action("shhift")

        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]
        # Exactly one attempt. The refusal is decided inside the try and
        # acted on after it, so a notice that raises cannot land the
        # handler in the except branch and be tried a second time.
        assert notice.call_count == 1

    def test_the_typed_text_never_reaches_the_log(self, handler, caplog):
        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice"):
            typing.return_value = (False, 2, "partial: 4/30 events")

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.type_text("my bank password is hunter2")

        assert "hunter2" not in caplog.text

    def test_the_notices_say_the_exact_words_the_user_hears(self):
        """Spelled out, because every other test reads the constant.

        A test that compares ``MESSAGE.format(...)`` against the same
        constant passes whatever the constant says, so the wording is only
        pinned where the words appear literally.
        """
        from ui.ui_action_handler import (
            HOTKEY_REFUSED_MESSAGE,
            HOTKEY_UNKNOWN_MESSAGE,
            PRESS_KEY_REFUSED_MESSAGE,
            PRESS_KEY_UNKNOWN_MESSAGE,
            TYPING_REFUSED_MESSAGE,
        )

        assert PRESS_KEY_UNKNOWN_MESSAGE.format(keys="shhift") == (
            "Cannot press shhift: Wheelhouse does not know that key name"
        )
        assert PRESS_KEY_REFUSED_MESSAGE.format(keys="tab") == (
            "Pressing tab did not work"
        )
        assert HOTKEY_UNKNOWN_MESSAGE.format(
            chord="ctrl + shhift", keys="shhift"
        ) == (
            "Cannot run the ctrl + shhift shortcut: Wheelhouse does not "
            "know shhift"
        )
        assert HOTKEY_REFUSED_MESSAGE.format(chord="ctrl + c") == (
            "The ctrl + c shortcut did not work"
        )
        assert TYPING_REFUSED_MESSAGE.format(sent=4, total=16) == (
            "Typing stopped after 4 of 16 characters"
        )

    def test_a_raising_key_press_shows_one_notice_and_no_error(
        self, handler, caplog
    ):
        """The except arm reports too, and reports the same way.

        A primitive that raises is the case the old code logged at ERROR
        and left to ``ErrorNotificationHandler``, which is the box the
        rate limiter can drop.
        """
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.side_effect = RuntimeError("the desktop is gone")

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.press_key_action("tab")

        assert notice.call_count == 1
        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]

    def test_a_raising_hotkey_shows_one_notice_and_no_error(
        self, handler, caplog
    ):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.side_effect = RuntimeError("the desktop is gone")

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.hotkey_action(["ctrl", "c"])

        assert notice.call_count == 1
        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]

    def test_raising_typing_shows_one_notice_and_no_error(
        self, handler, caplog
    ):
        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice") as notice:
            typing.side_effect = RuntimeError("the desktop is gone")

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.type_text("hello")

        assert notice.call_count == 1
        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]

    def test_each_notice_names_the_action_that_sent_it(self, handler):
        """The source is what the log line names, so it must be distinct."""
        sources = []
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()
            press.return_value = (False, 0, 0)
            typing.return_value = (False, 0, "win32 error 5")

            handler.press_key_action("shhift")
            sources.append(notice.call_args.kwargs["source"])
            handler.hotkey_action(["ctrl", "shhift"])
            sources.append(notice.call_args.kwargs["source"])
            handler.type_text("hello")
            sources.append(notice.call_args.kwargs["source"])

        assert sources == ["press key", "hotkey", "type text"]

    def test_a_chord_that_is_not_a_list_still_gets_its_notice(
        self, handler, caplog
    ):
        """wh-keyboard-refusal-notice.1.2: the except arm must not raise.

        ``input_proc`` validates only the container before
        ``method_to_call(**params)``, so nothing between Logic and the
        handler rejects a ``keys`` value that is not a list. No producer
        in this repository emits one today (grep for ``hotkey_action``
        under ``services/wheelhouse`` excluding ``.venv`` and tests:
        every one builds a list of strings), so this test pins the
        handler's own contract rather than an observed input.
        The first statement inside the try iterates ``keys`` and raises;
        the except arm iterated it a SECOND time to word the chord, which
        raised again and carried the exception out of the method. The
        Input loop then logged its own ERROR, whose box
        ``ErrorNotificationHandler`` rate-limits on a repeat -- the exact
        box this bead exists to replace.

        ``_send_modifier_keyups`` is patched for the reason two tests
        above it are: an unpatched name here reaches the real SendInput.
        The message is read through its constant, not spelled out, because
        the wording is another test's subject and this one is about the
        chord text a value that cannot be iterated produces.
        """
        from ui.ui_action_handler import HOTKEY_REFUSED_MESSAGE

        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.verified_press_keys"), \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.hotkey_action(None)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == HOTKEY_REFUSED_MESSAGE.format(
            chord="None"
        )
        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]

    def test_text_that_has_no_length_still_gets_its_notice(
        self, handler, caplog
    ):
        """The same shape for the typing handler, with ``text=1``.

        ``if not text`` is False for 1, so the primitive runs and raises
        while it iterates an int; the except arm then evaluated
        ``len(text)`` to word the notice and raised again. Zero is the
        honest total for a value with no length: nothing was typed.
        """
        from ui.ui_action_handler import TYPING_REFUSED_MESSAGE

        with patch(f"{_MOD}.type_string_verified") as typing, \
             patch(f"{_MOD}.send_notice") as notice:
            typing.side_effect = TypeError("'int' object is not iterable")

            with caplog.at_level(logging.DEBUG, logger=_MOD):
                handler.type_text(1)

        assert notice.call_count == 1
        assert notice.call_args.args[1] == TYPING_REFUSED_MESSAGE.format(
            sent=0, total=0
        )
        assert not [
            r for r in caplog.records
            if r.name == _MOD and r.levelname == "ERROR"
        ]


class TestOneBoxThroughTheRealPrimitive:
    """Boss e7 ruling A: one box for a refusal, and no ERROR record.

    The class above patches the primitive, so it proves what the handler
    does with an answer it is handed. These three run the REAL primitive,
    which is the only way to show that ``caller_notifies=True`` actually
    travels and actually lowers the record. Without them, a mutation that
    stops passing the flag through :func:`_build_press_keys_events`, or
    that ignores it inside the primitive, leaves every test above green.

    None of the three sends a real keystroke. An unmapped key name is
    refused before ``SendInput`` is reached, and the typing test replaces
    ``user32`` outright.
    """

    def _errors(self, caplog):
        return [
            f"{r.name}:{r.getMessage()}"
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name in (_MOD, "utils.win_input_sender")
        ]

    def test_a_refused_key_press_shows_one_box_and_logs_no_error(
        self, handler, caplog
    ):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()

            with caplog.at_level(logging.DEBUG):
                handler.press_key_action("shhift")

        assert notice.call_count == 1
        assert self._errors(caplog) == []

    def test_a_refused_hotkey_shows_one_box_and_logs_no_error(
        self, handler, caplog
    ):
        with patch(f"{_MOD}.capture_context") as ctx, \
             patch(f"{_MOD}.send_notice") as notice:
            ctx.return_value = _make_context()

            with caplog.at_level(logging.DEBUG):
                handler.hotkey_action(["ctrl", "shhift", "s"])

        assert notice.call_count == 1
        assert self._errors(caplog) == []

    def test_refused_typing_shows_one_box_and_logs_no_error(
        self, handler, caplog
    ):
        with patch("utils.win_input_sender.user32") as user32, \
             patch("utils.win_input_sender.kernel32") as kernel32, \
             patch(f"{_MOD}.send_notice") as notice:
            user32.SendInput.return_value = 0
            kernel32.GetLastError.return_value = 5

            with caplog.at_level(logging.DEBUG):
                handler.type_text("hi")

        assert notice.call_count == 1
        assert self._errors(caplog) == []


# ============================================================================
# Empty captured identity: the reject drops silently
# (wh-paste-when-unverified.2)
# ============================================================================

class TestEmptyIdentityRejectIsSilent:
    """A reject whose captured identity is EMPTY reaches no paste.

    ui/context.py calls capture_target_identity on every capture and it
    returns the all-zero ``TargetIdentity()`` -- never None -- both when
    there is no focused control and when reading the control raises.
    ``TargetIdentity.is_current()`` fails on its first line for that
    record, with no Windows call, so ClipboardOnlyStrategy's paste would
    be refused before the send EVERY time, and verified_paste logs the
    refusal at ERROR. An ERROR record IS a Windows notification
    (ErrorNotificationHandler, utils/error_notifier.py, sits on the root
    logger), so pasting such a reject shows the user a failure where the
    old behaviour showed nothing.

    On the letter-buffer path a second notification followed, because
    ClipboardOnlyStrategy sets no rejected_reason: the handler's
    ``_last_insert_was_rejected`` stayed False and end_utterance took its
    ERROR arm instead of the WARNING arm.

    These tests wire the REAL router and a REAL ClipboardOperations so
    the refusal and its ERROR come from the shipped code rather than
    from a copy of it here.
    """

    # Both reasons arrive carrying the same empty record: ui/text_target.py
    # answers no_focused_control for a None control and stale_com for one
    # whose reads raise, and capture_target_identity returns
    # TargetIdentity() for both.
    REASONS = ["no_focused_control", "stale_com"]

    @staticmethod
    def _wire_real_routing(handler, reason):
        """Replace the fixture's mock router with the real decision.

        The handler fixture patches InsertionRouter, so handler.router
        makes no routing decision at all. These tests are about that
        decision, so the real router is wired here between the two
        strategies it chooses from.

        ClipboardOnlyStrategy is given a REAL ClipboardOperations. Its
        identity refusal is the first thing verified_paste does, ahead
        of every Windows call and every clipboard write, so the test
        reaches the real ERROR record without touching the machine's
        clipboard. verified_paste is wrapped rather than replaced so the
        call is recorded AND the shipped refusal still runs.

        Returns that ClipboardOperations.
        """
        from ui.clipboard_operations import ClipboardOperations
        from ui.router import InsertionRouter
        from ui.strategies.specific import (
            ClipboardOnlyStrategy, RejectedInsertionStrategy,
        )
        from ui.text_target import TextTargetPredicate, TextTargetVerdict

        clipboard = ClipboardOperations(_make_config())
        clipboard.verified_paste = Mock(wraps=clipboard.verified_paste)
        predicate = MagicMock(spec=TextTargetPredicate)
        predicate.evaluate.return_value = TextTargetVerdict(
            verdict=False, reason=reason,
            control_type="ListItemControl", class_name="UIItem",
            process_name="chrome.exe",
        )
        handler.router = InsertionRouter(
            standard_strategy=MagicMock(name="StandardStrategy"),
            flutter_strategy=MagicMock(name="FlutterStrategy"),
            simple_paste_strategy=MagicMock(name="SimplePasteStrategy"),
            rejected_strategy=RejectedInsertionStrategy(),
            text_target_predicate=predicate,
            verified_unicode_strategy=MagicMock(name="VerifiedUnicodeStrategy"),
            verified_unicode_max_chars=50,
            clipboard_only_strategy=ClipboardOnlyStrategy(
                clipboard,
                MagicMock(name="WindowFocusManager"),
                text_perfector=None,
            ),
        )
        return clipboard

    @staticmethod
    def _context(reason):
        """The context ui/context.py really produces for these rejects."""
        from ui.target_identity import TargetIdentity
        return UIContext(
            focused_control=(
                None if reason == "no_focused_control" else MagicMock()
            ),
            is_flutter=False,
            is_terminal=False,
            process_name="chrome.exe",
            class_name="Chrome_WidgetWin_1",
            target_identity=TargetIdentity(),
        )

    @staticmethod
    def _errors(caplog):
        """Every record at ERROR or above. Each one IS a notification."""
        return [
            f"{r.name}: {r.getMessage()}" for r in caplog.records
            if r.levelno >= logging.ERROR
        ]

    # --- direct insert path ----------------------------------------------

    @pytest.mark.parametrize("reason", REASONS)
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_direct_insert_never_reaches_verified_paste(
            self, mock_ctx, mock_cc, handler, caplog, reason):
        """The pre-send proof would refuse this paste on every attempt,
        and the refusal is an ERROR, so the words must never be routed
        into it."""
        clipboard = self._wire_real_routing(handler, reason)
        mock_ctx.return_value = self._context(reason)
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.terminal_editor.is_active = False

        with caplog.at_level(logging.DEBUG):
            handler.intelligent_insert_text(
                "hello world", request_id="r-empty-identity",
            )

        clipboard.verified_paste.assert_not_called()
        assert self._errors(caplog) == []

    # --- letter-buffer flush path -----------------------------------------

    @pytest.mark.parametrize("reason", REASONS)
    @patch(f"{_MOD}.auto_compress_spelled_letters", return_value="ab")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_letter_buffer_flush_takes_the_warning_arm(
            self, mock_ctx, mock_cc, mock_compress, handler, caplog, reason):
        """end_utterance must report the undelivered letters at WARNING.
        The ERROR arm would raise a second Windows notification for the
        same dictation."""
        clipboard = self._wire_real_routing(handler, reason)
        mock_ctx.return_value = self._context(reason)
        handler.utterance_manager.is_in_utterance.return_value = False
        handler.terminal_editor.is_active = False
        handler._letter_buffer = ["a", "b"]

        with caplog.at_level(logging.DEBUG):
            handler.end_utterance(1)

        warnings = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING
            and "end_utterance" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert self._errors(caplog) == []
        clipboard.verified_paste.assert_not_called()

    # --- raw_insert_text ---------------------------------------------------

    @patch(f"{_MOD}.capture_context")
    def test_raw_insert_text_does_not_raise_on_this_reject(
            self, mock_ctx, handler):
        """raw_insert_text raises PasteFailedError on success=False.
        RejectedInsertionStrategy returns success=True, so the reject
        stays the silent no-op it was before wh-paste-when-unverified.2.
        The exception is captured rather than allowed out so a failure
        is reported as this test's own assertion, which is what the
        mutation gate can read as a catch."""
        clipboard = self._wire_real_routing(handler, "no_focused_control")
        mock_ctx.return_value = self._context("no_focused_control")
        handler.clipboard.last_clipboard_write_seq = None

        raised = None
        try:
            handler.raw_insert_text("hello world")
        except Exception as exc:
            raised = exc

        assert raised is None, f"raw_insert_text raised {raised!r}"
        clipboard.verified_paste.assert_not_called()


# ============================================================================
# A rejected insert delivered nothing, so the utterance must not retract
# (wh-paste-when-unverified.3.4)
# ============================================================================

class _TopLevelWindow:
    """The one attribute WindowFocusManager.remember_target reads."""

    def __init__(self, hwnd):
        self.NativeWindowHandle = hwnd


class _StaleThenLiveControl:
    """A focused control whose PROPERTY reads raise until ``stale`` clears.

    This stands in for the UIA boundary and for nothing else.
    ui/text_target.py:699-730 reads ControlType, ControlTypeName,
    ClassName, IsKeyboardFocusable and IsEnabled inside ONE try block
    and answers ``stale_com`` for an OSError out of any of them, so
    raising from the first read is what makes the REAL predicate return
    that reason. Once ``stale`` is cleared the same reads answer
    EditControl + IsEnabled, which the real predicate accepts on its
    ui/text_target.py:833-844 branch.

    ``GetTopLevelControl`` answers in both states. That is the shape the
    finding describes: the handle lookup works, so
    ui_action_handler.py:1767-1768 moves the remembered window to this
    control's window, while a transient property read still rejects.
    """

    def __init__(self, hwnd, *, stale=False, class_name="Edit"):
        self._hwnd = hwnd
        self.stale = stale
        self._class_name = class_name

    def _read(self, value):
        if self.stale:
            raise OSError("UIA property read failed on an initialising window")
        return value

    @property
    def ControlType(self):
        import uiautomation as auto
        return self._read(int(auto.ControlType.EditControl))

    @property
    def ControlTypeName(self):
        return self._read("EditControl")

    @property
    def ClassName(self):
        return self._read(self._class_name)

    @property
    def IsKeyboardFocusable(self):
        return self._read(True)

    @property
    def IsEnabled(self):
        return self._read(True)

    def GetPattern(self, pattern_id):
        """No TextPattern. The accept then comes from the EditControl
        branch, which needs one stand-in fewer than a pattern object."""
        return None

    def GetTopLevelControl(self):
        return _TopLevelWindow(self._hwnd)


class _CreditingStrategy:
    """Stands in for the delivering strategy on the window-A insert.

    VerifiedUnicodeStrategy.insert credits what it delivered through
    ``ClipboardOperations.credit_paste_chars``
    (ui/strategies/specific.py:1257), and that counter is the whole of
    what the retract path reads for its backspace count. This stub makes
    the same call against the REAL ClipboardOperations and returns a real
    InsertionResult, so window A ends up with the accounting a delivered
    insert leaves, without a clipboard write or a SendInput event.
    """

    def __init__(self, clipboard):
        self.clipboard = clipboard
        self.texts = []

    def insert(self, text, context, request_id=None, options=None):
        self.texts.append(text)
        self.clipboard.credit_paste_chars(
            text, target_class_name=getattr(context, "class_name", "") or "",
        )
        return InsertionResult(success=True, clipboard_dirty=False)


class TestRejectedInsertLeavesTheUtteranceUnretractable:
    """A rejected insert must set the retraction gate in both callers.

    THE HARM (wh-paste-when-unverified.3.4). ui_action_handler.py:1767
    moves the remembered window to the newly focused control BEFORE the
    routing decision below it. When routing then rejects, nothing is
    delivered, but the credited character count from the PREVIOUS window
    survives and the remembered window now names the NEW one. The
    retraction's own focus check at :1291-1318 compares the remembered
    window against the foreground, so it passes, and :1426 sends
    backspaces into a window this program never wrote to -- deleting the
    user's own text there.

    The gate that stops it is ``_used_simple_paste``, read at :1272.
    Before this bead it was set for SimplePasteStrategy and
    ClipboardOnlyStrategy only, so a RejectedInsertionStrategy result
    left retraction open.

    These tests wire the REAL InsertionRouter, the REAL
    TextTargetPredicate the handler already built, the REAL
    ClipboardOperations and the REAL WindowFocusManager, so the routing
    decision, the credited counter and the remembered window all come
    from the shipped code. Only the Win32 and UIA boundary and
    ``send_backspaces`` are stood in for.
    """

    HWND_A = 0x00A0A0
    HWND_B = 0x00B0B0

    @staticmethod
    def _live_identity(hwnd):
        """A captured identity that is NOT the empty record.

        Window A's insert must not take the empty-identity branch;
        equality against a fresh TargetIdentity() is the router's whole
        test, so any populated record keeps window A off that branch.
        """
        from ui.target_identity import TargetIdentity
        return TargetIdentity(
            hwnd=hwnd, root=hwnd, process_id=4242, tag=7,
            root_tag=7, foreground_root=hwnd, foreground_tag=7,
        )

    @classmethod
    def _wire(cls, handler):
        """Replace the fixture's mocks with the real decision-makers.

        Returns (clipboard, delivery_strategy).
        """
        from ui.clipboard_operations import ClipboardOperations
        from ui.router import InsertionRouter
        from ui.strategies.specific import (
            ClipboardOnlyStrategy, RejectedInsertionStrategy,
        )
        from ui.window_focus_manager import WindowFocusManager

        clipboard = ClipboardOperations(_make_config())
        handler.clipboard = clipboard
        handler.window_manager = WindowFocusManager()
        delivery = _CreditingStrategy(clipboard)
        # handler.text_target_predicate is the real predicate the
        # fixture's __init__ already built (ui_action_handler.py:803).
        # Production hands the router that same instance, and the
        # retract gate at :1394 reads it, so both decisions in these
        # tests come from one predicate as they do in the app.
        handler.router = InsertionRouter(
            standard_strategy=delivery,
            flutter_strategy=MagicMock(name="FlutterStrategy"),
            simple_paste_strategy=MagicMock(name="SimplePasteStrategy"),
            rejected_strategy=RejectedInsertionStrategy(),
            text_target_predicate=handler.text_target_predicate,
            verified_unicode_strategy=delivery,
            verified_unicode_max_chars=50,
            clipboard_only_strategy=ClipboardOnlyStrategy(
                clipboard, handler.window_manager, text_perfector=None,
            ),
        )
        return clipboard, delivery

    @staticmethod
    def _context(control, identity):
        """The context ui/context.py produces for these captures.

        capture_target_identity returns the all-zero TargetIdentity() --
        never None -- when the control's reads fail
        (ui/target_identity.py:62-83), so the empty record is what the
        rejected capture carries. It is built here rather than captured
        because the real capture tags window properties through Win32,
        which a test must not do against a handle it invented.
        """
        return UIContext(
            focused_control=control,
            is_flutter=False,
            is_terminal=False,
            process_name="notepad.exe",
            class_name="Edit",
            target_identity=identity,
        )

    # --- _execute_insert_with_ack (intelligent / verbatim / letter flush) ---

    @patch(f"{_MOD}.send_backspaces")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare")
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_stale_com_reject_blocks_the_retraction(
            self, mock_ctx, mock_cc, mock_win32, mock_norm, mock_bs,
            handler):
        """The reachable case. Five characters are credited in window A;
        window B then takes focus with a focused control whose reads
        raise, so the predicate answers stale_com and the captured
        identity is the empty record; the router drops that insert
        silently. B's reads recover before the corrected final arrives.
        Without the gate, retract sends five backspaces into B, deleting
        five characters this program never wrote there."""
        from ui.target_identity import TargetIdentity

        clipboard, delivery = self._wire(handler)
        mock_norm.side_effect = lambda hwnd: hwnd or None
        mock_win32.GetForegroundWindow.return_value = self.HWND_B
        mock_bs.return_value = True

        control_a = _StaleThenLiveControl(self.HWND_A)
        control_b = _StaleThenLiveControl(self.HWND_B, stale=True)
        current = {
            "context": self._context(
                control_a, self._live_identity(self.HWND_A),
            ),
        }
        mock_ctx.side_effect = lambda: current["context"]
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.terminal_editor.is_active = False

        # 1. Five characters are delivered into window A and credited.
        handler.intelligent_insert_text("hello", request_id="r-window-a")
        assert delivery.texts == ["hello"]
        assert clipboard.accumulated_paste_chars == 5
        assert handler.window_manager._last_target_hwnd == self.HWND_A
        assert handler._used_simple_paste is False

        # 2. Window B takes focus while its property reads raise. The
        #    REAL predicate is what answers stale_com here.
        assert handler.text_target_predicate.evaluate(
            control_b, class_name="Edit", process_name="notepad.exe",
        ).reason == "stale_com"
        current["context"] = self._context(control_b, TargetIdentity())
        handler.intelligent_insert_text("hello", request_id="r-window-b")

        # The remembered window has moved to B although nothing landed
        # there -- this is the precondition the harm needs, so it is
        # asserted rather than assumed.
        assert handler.window_manager._last_target_hwnd == self.HWND_B
        assert delivery.texts == ["hello"]
        assert clipboard.accumulated_paste_chars == 5

        # 3. B's UIA reads recover, then the corrected final retracts.
        control_b.stale = False
        result = handler.retract()

        # One assertion carrying both facts: a rejected insert must send
        # no key, and the failure text must name what retract decided,
        # because "no backspace" alone would not say which gate stopped
        # it (or that none did).
        assert mock_bs.call_args_list == [], (
            "backspaces were sent into the window the rejected insert "
            f"never wrote to; retract returned {result!r}"
        )
        assert result == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    # --- raw_insert_text ----------------------------------------------------

    @patch(f"{_MOD}.send_backspaces")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare")
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.capture_context")
    def test_stale_com_reject_blocks_the_retraction_from_raw_insert(
            self, mock_ctx, mock_win32, mock_norm, mock_bs, handler):
        """raw_insert_text is the second get_strategy caller. It
        remembers the newly focused window before it routes, exactly as
        _execute_insert_with_ack does, so it carries the same harm and
        needs the same gate."""
        from ui.target_identity import TargetIdentity

        clipboard, delivery = self._wire(handler)
        mock_norm.side_effect = lambda hwnd: hwnd or None
        mock_win32.GetForegroundWindow.return_value = self.HWND_B
        mock_bs.return_value = True

        control_a = _StaleThenLiveControl(self.HWND_A)
        control_b = _StaleThenLiveControl(self.HWND_B, stale=True)
        current = {
            "context": self._context(
                control_a, self._live_identity(self.HWND_A),
            ),
        }
        mock_ctx.side_effect = lambda: current["context"]

        handler.raw_insert_text("hello")
        assert clipboard.accumulated_paste_chars == 5
        assert handler.window_manager._last_target_hwnd == self.HWND_A
        assert handler._used_simple_paste is False

        current["context"] = self._context(control_b, TargetIdentity())
        handler.raw_insert_text("hello")

        assert handler.window_manager._last_target_hwnd == self.HWND_B
        assert delivery.texts == ["hello"]
        assert clipboard.accumulated_paste_chars == 5

        control_b.stale = False
        result = handler.retract()

        # One assertion carrying both facts: a rejected insert must send
        # no key, and the failure text must name what retract decided,
        # because "no backspace" alone would not say which gate stopped
        # it (or that none did).
        assert mock_bs.call_args_list == [], (
            "backspaces were sent into the window the rejected insert "
            f"never wrote to; retract returned {result!r}"
        )
        assert result == {
            "status": "not_retracted", "reason": "simple_paste",
        }

    # --- the case the harm does NOT reach -----------------------------------

    @patch(f"{_MOD}.send_backspaces")
    @patch(f"{_MOD}.normalize_hwnd_for_foreground_compare")
    @patch(f"{_MOD}.win32gui")
    @patch(f"{_MOD}.clipboard_context")
    @patch(f"{_MOD}.capture_context")
    def test_no_focused_control_reject_keeps_the_older_protection(
            self, mock_ctx, mock_cc, mock_win32, mock_norm, mock_bs,
            handler):
        """PASSES BOTH BEFORE AND AFTER THE FIX, on purpose.

        The other reject reason that carries the empty identity is
        no_focused_control, and the harm never reached it:
        ui_action_handler.py:1767 reads ``if context.focused_control:``
        before calling remember_target, so with no control the
        remembered window stays on window A and the focus check at
        :1291-1318 refuses the backspaces by itself.

        Before the fix this test blocks at that focus check with reason
        ``focus_drifted``. After the fix the earlier
        ``_used_simple_paste`` gate at :1272 blocks it first with reason
        ``simple_paste``. Both are refusals that send no key, so the
        assertions below are written on what must hold either way -- the
        remembered window never moved, no backspace was sent, and the
        retraction did not happen. A test pinning one reason token would
        have to change with the fix and would therefore prove nothing
        about it."""
        from ui.target_identity import TargetIdentity

        clipboard, delivery = self._wire(handler)
        mock_norm.side_effect = lambda hwnd: hwnd or None
        mock_win32.GetForegroundWindow.return_value = self.HWND_B
        mock_bs.return_value = True

        control_a = _StaleThenLiveControl(self.HWND_A)
        current = {
            "context": self._context(
                control_a, self._live_identity(self.HWND_A),
            ),
        }
        mock_ctx.side_effect = lambda: current["context"]
        handler.utterance_manager.is_in_utterance.return_value = True
        handler.terminal_editor.is_active = False

        handler.intelligent_insert_text("hello", request_id="r-window-a")
        assert clipboard.accumulated_paste_chars == 5
        assert handler.window_manager._last_target_hwnd == self.HWND_A

        # No focused control at all. The REAL predicate answers
        # no_focused_control for None (ui/text_target.py:691-695).
        assert handler.text_target_predicate.evaluate(
            None, class_name="", process_name="notepad.exe",
        ).reason == "no_focused_control"
        current["context"] = self._context(None, TargetIdentity())
        handler.intelligent_insert_text("hello", request_id="r-no-control")

        # The protection: remember_target was never called, so the
        # remembered window is still A while the foreground is B.
        assert handler.window_manager._last_target_hwnd == self.HWND_A
        assert delivery.texts == ["hello"]
        assert clipboard.accumulated_paste_chars == 5

        result = handler.retract()

        mock_bs.assert_not_called()
        assert result["status"] == "not_retracted"
        assert result["reason"] in ("focus_drifted", "simple_paste")
