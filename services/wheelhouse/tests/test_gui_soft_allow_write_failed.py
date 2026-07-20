"""Tests for the GUI-side soft_allow_write_failed rendering (wh-9dkse).

When LogicController.add_soft_allow fails to persist the soft-allow
file to disk, it emits a ``soft_allow_write_failed`` action on the
GUI state queue. The GUI's queue listener calls
``_show_soft_allow_write_failed_toast``. The handler:

  * Builds the SoftAllowWriteFailedToast widget on first use; the
    instance is reused across events.
  * Composes the fixed-wording title and body.
  * Calls ``show_message`` on the widget.

The toast is identity-agnostic; the event payload's process_name,
class_name, and control_type are not surfaced in the wording. They
exist on the IPC for diagnostic/logging symmetry with the other
soft-allow events but the user-facing message does not need them.

Coverage:
  * The widget is built lazily (not in __init__).
  * A well-formed message renders the toast.
  * The fixed-wording title and body land on the widget.
  * The widget instance is reused across multiple events.
  * A malformed message (missing fields) still renders the toast --
    the wording is fixed, so the payload fields are advisory only;
    failing closed by dropping the toast would silently swallow the
    user's recovery feedback.
  * An exception inside the rendering path is caught and logged at
    WARNING; the GUI does not crash.
  * The queue dispatcher in ``_check_queues_and_events`` routes a
    ``soft_allow_write_failed`` action to the handler. Direct calls
    to the handler do not exercise this branch; without the dispatch
    test, a typo in the action string or a removed elif branch
    would silently drop the user's only feedback that the Yes click
    did not persist (wh-vbvgf.17.1, codex review).
"""

from __future__ import annotations

import logging
from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# wh-pytest-flaky-segfault: these tests construct GuiManager, which
# builds real Qt widgets; without a QApplication Qt aborts the whole
# interpreter (no traceback, output lost). The session-scoped qapp
# fixture guarantees one exists even when this file runs in isolation.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


@pytest.fixture
def manager():
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        cmds_q = MagicMock()
        state_q = MagicMock()
        # The dispatch tests drive _check_queues_and_events, which
        # bails out early if shutdown_event.is_set() is truthy. A
        # bare MagicMock returns a truthy MagicMock for is_set, so
        # set the return value explicitly.
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, cmds_q, state_q)
        return mgr


def _payload(**overrides) -> dict:
    base = {
        "action": "soft_allow_write_failed",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
    }
    base.update(overrides)
    return base


class TestLazyConstruction:
    def test_widget_is_not_built_at_manager_init(self, manager):
        """The toast widget is created on first use, not during
        GuiManager construction. A Qt widget built before any
        write-failure has fired is wasted memory and adds startup time.
        """
        assert manager._soft_allow_write_failed_toast is None


class TestRender:
    def test_well_formed_message_shows_toast(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_soft_allow_write_failed_toast(_payload())

        mock_toast_cls.assert_called_once()
        mock_instance.show_message.assert_called_once()

    def test_show_message_carries_fixed_wording(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_soft_allow_write_failed_toast(_payload())

        kwargs = mock_instance.show_message.call_args.kwargs
        if not kwargs:
            args = mock_instance.show_message.call_args.args
            kwargs = {"title": args[0], "body": args[1]} if len(args) >= 2 else {}
        assert kwargs.get("title") == "Wheelhouse couldn't save your choice"
        assert kwargs.get("body") == (
            "Try saying the words again later, then click Yes again."
        )

    def test_widget_instance_reused_across_events(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_soft_allow_write_failed_toast(_payload())
            manager._show_soft_allow_write_failed_toast(_payload(
                process_name="notepad.exe",
                class_name="Edit",
                control_type="Document",
            ))

        # The widget class was instantiated exactly once; the second
        # event reused the cached instance.
        mock_toast_cls.assert_called_once()
        assert mock_instance.show_message.call_count == 2

    def test_malformed_message_still_renders(self, manager):
        """The toast wording is fixed, so the event's identity fields
        do not affect the user-visible string. A payload missing
        process_name / class_name / control_type still triggers the
        toast: the alternative is silently swallowing the user's
        recovery feedback, which is worse than ignoring the missing
        diagnostic fields.
        """
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_soft_allow_write_failed_toast({
                "action": "soft_allow_write_failed",
            })

        mock_instance.show_message.assert_called_once()


class TestGrantPromptDedupClear:
    """wh-vbvgf.18.1 (deepseek): the disk-write-fails event must
    clear the tuple from `_grant_prompt_acted_on` so the next
    threshold event for the same tuple can re-fire the grant
    prompt within the same GUI session.

    Without this, the trace from the deepseek finding holds:
    user clicks Yes -> GUI adds tuple to dedup -> Logic disk
    write fails -> Logic does NOT reset the counter (DISK_FAILED
    spec) -> user re-attempts -> counter crosses threshold ->
    Logic forwards text_target_grant_prompt -> GUI dedup blocks
    it. The disk-write-fails toast says "click Yes again" but
    the system makes that impossible until the GUI restarts.
    """

    def test_handler_discards_tuple_from_acted_on_set(self, manager):
        # Pre-populate the dedup set as if the user had already
        # clicked Yes on the grant prompt.
        tuple_key = ("zed.exe", "zed::Workspace", "Pane")
        manager._grant_prompt_acted_on.add(tuple_key)

        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ):
            manager._show_soft_allow_write_failed_toast(_payload(
                process_name=tuple_key[0],
                class_name=tuple_key[1],
                control_type=tuple_key[2],
            ))

        assert tuple_key not in manager._grant_prompt_acted_on

    def test_handler_does_not_raise_on_unknown_tuple(self, manager):
        # If the tuple is not in the set (e.g. the user dismissed
        # the grant prompt without clicking Yes, but the disk
        # write somehow still failed), the discard is a noop.
        tuple_key = ("zed.exe", "zed::Workspace", "Pane")
        assert tuple_key not in manager._grant_prompt_acted_on

        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ):
            manager._show_soft_allow_write_failed_toast(_payload(
                process_name=tuple_key[0],
                class_name=tuple_key[1],
                control_type=tuple_key[2],
            ))

        assert tuple_key not in manager._grant_prompt_acted_on

    def test_malformed_payload_does_not_clear_arbitrary_tuples(self, manager):
        # A payload missing identity fields must not clear any
        # entry from the dedup set. The wording is fixed and the
        # toast still renders, but the dedup discard requires a
        # fully-formed tuple so it cannot accidentally re-arm a
        # different tuple's prompt.
        unrelated_tuple = ("notepad.exe", "Edit", "Document")
        manager._grant_prompt_acted_on.add(unrelated_tuple)

        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ):
            manager._show_soft_allow_write_failed_toast({
                "action": "soft_allow_write_failed",
            })

        assert unrelated_tuple in manager._grant_prompt_acted_on


class TestExceptionHandling:
    def test_exception_in_render_is_logged_not_raised(self, manager, caplog):
        """A broken Qt environment must not crash the GUI process.
        The handler logs at WARNING and returns.
        """
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast",
            side_effect=RuntimeError("qt environment broken"),
        ):
            with caplog.at_level(logging.WARNING):
                manager._show_soft_allow_write_failed_toast(_payload())

        assert any(
            "soft_allow_write_failed" in r.message.lower()
            or "write" in r.message.lower()
            for r in caplog.records
        )


class TestQueueDispatch:
    """The dispatcher in ``_check_queues_and_events`` is the only
    production entry point for the new feedback path. The direct
    handler tests above exercise behaviour but skip the dispatch
    branch entirely; a typo in the action string or an accidentally
    removed elif would silently drop disk-write-failure feedback
    while still passing every direct handler test.

    These tests drive ``_check_queues_and_events`` with a real
    payload on ``state_from_logic_queue`` and assert the handler
    receives it.
    """

    def test_dispatch_routes_payload_to_handler(self, manager):
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [
            payload, Empty(),
        ]

        with patch.object(
            manager, "_show_soft_allow_write_failed_toast"
        ) as mock_handler:
            manager._check_queues_and_events()

        mock_handler.assert_called_once_with(payload)

    def test_dispatch_does_not_invoke_other_toast_handlers(self, manager):
        """A regression that misroutes the action to the rejection
        toast or grant prompt handler would corrupt the user-facing
        message. Assert only the soft_allow_write_failed handler
        sees the payload.
        """
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [
            payload, Empty(),
        ]

        with patch.object(
            manager, "_show_soft_allow_write_failed_toast"
        ) as mock_target, patch.object(
            manager, "_show_rejection_toast"
        ) as mock_rejection, patch.object(
            manager, "_show_grant_prompt_toast"
        ) as mock_grant:
            manager._check_queues_and_events()

        mock_target.assert_called_once()
        mock_rejection.assert_not_called()
        mock_grant.assert_not_called()
