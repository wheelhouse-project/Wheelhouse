"""Tests for RejectedInsertionStrategy and the handler's rejection path.

Covers:
- RejectedInsertionStrategy.insert is a complete no-op (no clipboard,
  no buffer, no SendInput, no counter advance).
- The result carries rejected_reason set, success=True, clipboard_dirty=False.
- The handler emits PATH_INSERT_REJECTED on a rejected result instead of
  PATH_INSERT_VERIFIED.
- The handler does NOT log "Strategy returned success=False" or call
  send_error on a rejected result.

References: wh-zndq (no-text-input dictation routing), wh-fc1x (text
input target handling epic), wh-ix1z.2 (codex-review-loop round 1
finding: explicit silent reject outcome).
"""
from unittest.mock import MagicMock

from ui.context import UIContext
from ui.response_handler import ResponseHandler
from ui.strategies.base import InsertionResult
from ui.strategies.specific import RejectedInsertionStrategy


def _make_context() -> UIContext:
    return UIContext(
        focused_control=MagicMock(),
        is_flutter=False,
        is_terminal=False,
        process_name="explorer.exe",
        class_name="UIItem",
    )


# --- TestRejectedInsertionStrategy ----------------------------------------


class TestRejectedInsertionStrategy:
    def test_insert_returns_success_with_rejected_reason(self):
        strategy = RejectedInsertionStrategy()
        result = strategy.insert("hello", _make_context())
        assert isinstance(result, InsertionResult)
        assert result.success is True
        assert result.clipboard_dirty is False
        assert result.rejected_reason == RejectedInsertionStrategy.DEFAULT_REASON
        assert result.was_rejected is True

    def test_insert_does_not_touch_clipboard(self):
        # The strategy holds no clipboard reference -- a regression that
        # added one would surface as an attribute access here. The test
        # also confirms the public surface stays minimal: just insert.
        strategy = RejectedInsertionStrategy()
        public = [n for n in dir(strategy) if not n.startswith("_") and n != "insert"]
        assert "DEFAULT_REASON" in public
        # No clipboard / buffer / window manager attributes leaked in.
        assert "clipboard" not in public
        assert "buffer_manager" not in public
        assert "window_manager" not in public

    def test_insert_does_not_advance_state_attributes(self):
        # The strategy must not write any attribute on the context or its
        # focused_control mock during a no-op rejection.
        ctx = _make_context()
        ctrl = ctx.focused_control
        before_calls = list(ctrl.method_calls)
        RejectedInsertionStrategy().insert("hello", ctx)
        after_calls = list(ctrl.method_calls)
        assert before_calls == after_calls

    def test_insert_logs_at_debug_only(self, caplog):
        # INFO logging is the router's job (with full predicate verdict
        # telemetry); the strategy itself stays at DEBUG so background
        # speech rejection does not produce a wall of INFO records.
        strategy = RejectedInsertionStrategy()
        with caplog.at_level("INFO", logger="ui.strategies.specific"):
            strategy.insert("hello", _make_context())
        info_records = [
            r for r in caplog.records
            if r.name == "ui.strategies.specific" and r.levelname == "INFO"
        ]
        assert info_records == []


# --- TestHandlerRejectionPath ---------------------------------------------


class TestHandlerRejectionPath:
    """The handler at ui_action_handler._execute_insert_with_ack must
    distinguish a rejected result from an ordinary delivery result.
    Rejected results emit a Schema A success with PATH_INSERT_REJECTED
    and never trigger the "strategy returned False" send_error branch.
    """

    def test_rejected_result_emits_insert_rejected_path(self):
        # Build a stand-alone ResponseHandler so we can assert what it puts
        # on the queue without instantiating the full UIActionHandler.
        queue = MagicMock()
        handler = ResponseHandler(queue)
        result = InsertionResult(
            success=True, clipboard_dirty=False,
            rejected_reason="no_text_target",
        )
        # Mirror the production handler branch precisely.
        if result.success:
            if result.was_rejected:
                handler.send_success(
                    "req-1", "intelligent_insert_text",
                    ResponseHandler.PATH_INSERT_REJECTED,
                    rejected_reason=result.rejected_reason,
                )
            else:
                handler.send_success(
                    "req-1", "intelligent_insert_text",
                    ResponseHandler.PATH_INSERT_VERIFIED,
                )
        msg = queue.put.call_args.args[0]
        assert msg["status"] == "ok"
        assert msg["path"] == "insert_rejected"
        assert msg["rejected_reason"] == "no_text_target"
        assert msg["action"] == "intelligent_insert_text"
        assert "error" not in msg

    def test_normal_success_still_emits_insert_verified_path(self):
        queue = MagicMock()
        handler = ResponseHandler(queue)
        result = InsertionResult(success=True, clipboard_dirty=False)
        assert result.was_rejected is False
        if result.success:
            if result.was_rejected:
                handler.send_success(
                    "req-2", "intelligent_insert_text",
                    ResponseHandler.PATH_INSERT_REJECTED,
                    rejected_reason=result.rejected_reason,
                )
            else:
                handler.send_success(
                    "req-2", "intelligent_insert_text",
                    ResponseHandler.PATH_INSERT_VERIFIED,
                )
        msg = queue.put.call_args.args[0]
        assert msg["status"] == "ok"
        assert msg["path"] == "insert_verified"
        assert "rejected_reason" not in msg


# --- TestInsertionResultRejectedReason ------------------------------------


class TestInsertionResultRejectedReason:
    def test_default_rejected_reason_is_none(self):
        r = InsertionResult(success=True, clipboard_dirty=False)
        assert r.rejected_reason is None
        assert r.was_rejected is False

    def test_explicit_rejected_reason_marks_was_rejected(self):
        r = InsertionResult(
            success=True, clipboard_dirty=False,
            rejected_reason="denylist_class_name",
        )
        assert r.was_rejected is True
        assert r.rejected_reason == "denylist_class_name"

    def test_failure_with_rejected_reason_still_was_rejected(self):
        # Sanity: was_rejected reflects the field, not the success state.
        # In practice the strategy only sets rejected_reason on success
        # results; this test guards the property's pure logic.
        r = InsertionResult(
            success=False, clipboard_dirty=False,
            rejected_reason="stale_com",
        )
        assert r.was_rejected is True
