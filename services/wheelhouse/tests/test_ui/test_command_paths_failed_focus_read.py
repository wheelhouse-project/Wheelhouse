"""A failed focused-control read must stop every COMMAND action.

wh-insert-focus-read-stall.1.4 (codex round 3, confirmed against the
code). Before this branch ``capture_context`` let a COM error from
``auto.GetFocusedControl()`` escape, and each command action's own
``except Exception`` arm caught it before any key was sent. The read no
longer raises, so without this guard ``transform_selection``,
``wrap_or_insert``, ``press_key_action`` and ``hotkey_action`` carry on
and send Ctrl+C, a key, or a chord into whatever window then holds the
foreground -- which after a stalled read is often the window the person
just moved to.

Evidence that the four sites really did change behaviour, measured at
6dcc6e4b: ``git rev-parse a1711baf:services/wheelhouse/ui/
ui_action_handler.py`` and the same at 6dcc6e4b both print
e9d22a2bd2a6a86f7c2dba6a0fcf726ae43ec1ba, so the handler file is
byte-identical at the base and at the head. Every difference comes from
``capture_context`` alone.

The dictation INSERTION paths are deliberately not covered here. Pasting
the word into the unchanged window is the whole purpose of
wh-insert-focus-read-stall, so ``_execute_insert_with_ack`` and
``raw_insert_text`` keep the new behaviour. The last test in this file
pins that difference so a later change cannot quietly extend the refusal
to them.

Every caller of ``capture_context`` was enumerated before this file was
written, by two methods that agreed. ``impact-brief.py --symbol
ui.context.capture_context`` (an ast scan of 1145 Python files) and the
language server's ``findReferences`` on the definition both name exactly
ten production call sites, all in ``ui/ui_action_handler.py``: 1073,
1395, 1764, 2005, 2213, 2593, 5163, 5403, 5534 and 5927. Four are the
command paths guarded here. Two (1764, 2593) are the insertion paths
above. The remaining four already return early or report on a control of
None and are unchanged.
"""
import logging

import pytest
from unittest.mock import MagicMock, patch

from ui.context import UIContext


_MOD = "ui.ui_action_handler"


class _ReadStalled(Exception):
    """Stands in for the COM error the focused-control read raises."""


def _failed_read_context():
    """The context capture_context builds after a failed read.

    The identity is non-empty in production -- that is what lets the
    insertion path still paste -- so nothing here depends on the
    identity being absent.
    """
    return UIContext(
        focused_control=None,
        is_flutter=False,
        is_terminal=False,
        process_name="",
        class_name="",
        focus_read_failed=True,
    )


@pytest.fixture
def handler():
    """A UIActionHandler with every specialist component mocked.

    Copied from tests/test_ui/test_ui_action_handler.py. The ``yield``
    sits INSIDE the ``with`` on purpose: a fixture that returned from
    inside the block would release every patch before the test body ran,
    and the body would then reach the real capture_context and the live
    desktop.
    """
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

        from ui.ui_action_handler import UIActionHandler

        h = UIActionHandler(
            response_queue=MagicMock(),
            config={
                "ui_actions": {
                    "timing": {"utterance_clipboard_timeout_seconds": 1.0},
                }
            },
        )
        h.terminal_editor.is_active = False
        yield h


class TestNoCommandSendsKeysAfterAFailedRead:
    """None of the four command actions may send a key after a failed read.

    Each test patches verified_press_keys, which is the single function
    every one of these paths sends through, and asserts it was never
    called. Sending is the harm; the log record is checked separately
    below.
    """

    def test_transform_selection_sends_nothing(self, handler):
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys") as keys:
            handler.transform_selection("uppercase", request_id="r1")

        keys.assert_not_called()

    def test_wrap_or_insert_sends_nothing(self, handler):
        # No captured text and no recent paste, which is the branch that
        # copies the selection with Ctrl+C.
        handler.utterance_manager._last_paste_time = 0.0
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys") as keys:
            handler.wrap_or_insert("(", ")", "", request_id="r2")

        keys.assert_not_called()

    def test_press_key_action_sends_nothing(self, handler):
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys") as keys:
            handler.press_key_action("enter")

        keys.assert_not_called()

    def test_hotkey_action_sends_nothing(self, handler):
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys") as keys:
            handler.hotkey_action(["ctrl", "c"])

        keys.assert_not_called()


class TestTheRefusalMatchesThePreBranchOutcome:
    """The guard must reproduce what the escaping COM error produced.

    The boss's condition on this fix: the user must see what they saw
    before the branch, at the same log level and with the same notice or
    none. Each action swallows the exception in its own except arm, and
    the level differs between them on purpose: transform_selection and
    wrap_or_insert log at ERROR, which ErrorNotificationHandler turns
    into a Windows notice, while press_key_action and hotkey_action log
    at WARNING and show nothing. Re-raising the original error is what
    keeps all four identical.
    """

    def test_no_exception_escapes_any_command(self, handler):
        handler.utterance_manager._last_paste_time = 0.0
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys"):
            handler.transform_selection("uppercase", request_id="r1")
            handler.wrap_or_insert("(", ")", "", request_id="r2")
            handler.press_key_action("enter")
            handler.hotkey_action(["ctrl", "c"])

    def test_the_key_actions_log_at_warning_and_show_no_notice(
        self, handler, caplog,
    ):
        """press_key_action and hotkey_action stay at WARNING.

        An ERROR record here would reach ErrorNotificationHandler and
        show the user a box, which the escaping COM error never did.
        """
        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()), \
             patch(f"{_MOD}.verified_press_keys"), \
             caplog.at_level(logging.DEBUG, logger="ui.ui_action_handler"):
            handler.press_key_action("enter")
            handler.hotkey_action(["ctrl", "c"])

        errors = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name == "ui.ui_action_handler"
        ]
        assert errors == [], (
            "a key command showed the user an error notice that the "
            f"pre-branch COM error never showed: {[r.message for r in errors]}"
        )
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "ui.ui_action_handler"
        ]
        assert warnings, "the refusal was not reported at WARNING at all"

    def test_the_original_read_error_reaches_the_log(self, handler, caplog):
        """The message the user's log shows names the real cause.

        capture_context keeps the exception it caught, and the guard
        re-raises that same object, so each action's own except arm
        formats the original COM error exactly as it did before the
        branch.
        """
        context = _failed_read_context()
        context.read_error = _ReadStalled("the read stalled and failed")

        with patch(f"{_MOD}.capture_context", return_value=context), \
             patch(f"{_MOD}.verified_press_keys"), \
             caplog.at_level(logging.DEBUG, logger="ui.ui_action_handler"):
            handler.press_key_action("enter")

        assert any(
            "the read stalled and failed" in r.getMessage()
            for r in caplog.records
        ), (
            "the original read error did not reach the log: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


class TestTheInsertionPathIsNotRefused:
    """Dictation must still be delivered after a failed read.

    This is the difference the whole bead exists to create, so it gets
    its own test. A later change that extended the command refusal to
    the insertion path would silently restore the dropped word this
    branch removed.
    """

    def test_execute_insert_with_ack_still_routes_the_word(self, handler):
        strategy = MagicMock()
        from ui.strategies.base import InsertionResult
        strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )
        handler.router.get_strategy = MagicMock(return_value=strategy)

        with patch(f"{_MOD}.capture_context",
                   return_value=_failed_read_context()):
            handler._execute_insert_with_ack("hello world", request_id="r9")

        strategy.insert.assert_called()
