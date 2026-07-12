"""Tests for the GUI-side declined_write_failed rendering (wh-27gvv).

When LogicController.add_declined fails to persist the declined file
to disk, it emits a ``declined_write_failed`` action on the GUI state
queue. The GUI's queue listener calls
``_show_declined_write_failed_toast``. The handler:

  * Builds (or reuses) the SoftAllowWriteFailedToast widget.
  * Composes the fixed wording for the No-choice failure case.
  * Calls ``show_message`` on the widget.
  * Does NOT touch ``_grant_prompt_acted_on``. The No-click handler
    never adds to that set (wh-vbvgf.12.1), so the disk-failure
    handler has nothing to clear. The Logic-side forwarder
    publishes a fresh approval prompt on the next verified-retry
    threshold because ``add_declined`` did not update the in-memory
    suppression set on disk failure; the GUI then renders that
    prompt normally. ``TestNoGrantPromptDedupTouched`` asserts this
    contract: a pre-seeded entry remains in the set after the
    handler runs.

The toast widget is shared with the soft_allow_write_failed path
(both are "WheelHouse couldn't save your choice" notices), but the
wording in the body differs because the user just clicked No, not
Yes.
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
        shutdown = MagicMock()
        shutdown.is_set.return_value = False
        mgr = GuiManager(shutdown, cmds_q, state_q)
        return mgr


def _payload(**overrides) -> dict:
    base = {
        "action": "declined_write_failed",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
    }
    base.update(overrides)
    return base


class TestRender:
    def test_well_formed_message_shows_toast(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_declined_write_failed_toast(_payload())

        mock_toast_cls.assert_called_once()
        mock_instance.show_message.assert_called_once()

    def test_show_message_carries_no_specific_wording(self, manager):
        """The body must mention "click No again" rather than the
        Yes-path wording. The user just clicked No; telling them to
        click Yes would confuse them."""
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_declined_write_failed_toast(_payload())

        kwargs = mock_instance.show_message.call_args.kwargs
        if not kwargs:
            args = mock_instance.show_message.call_args.args
            kwargs = (
                {"title": args[0], "body": args[1]}
                if len(args) >= 2 else {}
            )
        assert kwargs.get("title") == "WheelHouse couldn't save your choice"
        body = kwargs.get("body") or ""
        # The body must steer the user to re-attempt the No click.
        assert "No" in body, (
            f"declined body should reference clicking No again; got: {body!r}"
        )
        # And must NOT incorrectly steer them to click Yes.
        assert "Yes" not in body, (
            f"declined body should not reference Yes; got: {body!r}"
        )

    def test_widget_instance_reused_across_events(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_declined_write_failed_toast(_payload())
            manager._show_declined_write_failed_toast(_payload(
                process_name="notepad.exe",
                class_name="Edit",
                control_type="Document",
            ))

        mock_toast_cls.assert_called_once()
        assert mock_instance.show_message.call_count == 2

    def test_malformed_message_still_renders(self, manager):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_declined_write_failed_toast({
                "action": "declined_write_failed",
            })

        mock_instance.show_message.assert_called_once()


class TestNoGrantPromptDedupTouched:
    """The declined-write-failed handler does NOT clear
    ``_grant_prompt_acted_on``. The No-click handler in the GUI
    deliberately does not populate that dedup set (wh-vbvgf.12.1):
    Logic owns the authoritative per-run suppression, and the GUI
    process outlives a Logic restart, so a GUI-side dedup add would
    silently keep suppressing prompts that Logic has cleared.

    Because the No-click never adds, the disk-failure handler has
    nothing to clear. A future maintainer copy-pasting the Yes-path
    discard into this handler would imply the No-click adds to the
    dedup -- the very thing wh-vbvgf.12.1 prohibits.
    """

    def test_handler_does_not_touch_acted_on_set(self, manager):
        # Pre-seed the set with the declined tuple (defensively --
        # this should never happen in production because the No
        # handler never adds, but if it ever did we must not touch
        # the entry).
        tuple_key = ("zed.exe", "zed::Workspace", "Pane")
        manager._grant_prompt_acted_on.add(tuple_key)

        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast"
        ):
            manager._show_declined_write_failed_toast(_payload(
                process_name=tuple_key[0],
                class_name=tuple_key[1],
                control_type=tuple_key[2],
            ))

        # The entry is untouched. A regression that copies the
        # Yes-path discard into the declined handler fails here.
        assert tuple_key in manager._grant_prompt_acted_on


class TestExceptionHandling:
    def test_exception_in_render_is_logged_not_raised(self, manager, caplog):
        with patch(
            "soft_allow_write_failed_toast.SoftAllowWriteFailedToast",
            side_effect=RuntimeError("qt environment broken"),
        ):
            with caplog.at_level(logging.WARNING):
                manager._show_declined_write_failed_toast(_payload())

        assert any(
            "declined" in r.message.lower() or "write" in r.message.lower()
            for r in caplog.records
        )


class TestQueueDispatch:
    """The dispatcher in ``_check_queues_and_events`` routes the
    declined_write_failed action to the handler. A typo in the action
    string or a missing elif would silently drop the user's only
    feedback that the No click did not persist.
    """

    def test_dispatch_routes_payload_to_handler(self, manager):
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [
            payload, Empty(),
        ]

        with patch.object(
            manager, "_show_declined_write_failed_toast"
        ) as mock_handler:
            manager._check_queues_and_events()

        mock_handler.assert_called_once_with(payload)

    def test_dispatch_does_not_invoke_other_toast_handlers(self, manager):
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [
            payload, Empty(),
        ]

        with patch.object(
            manager, "_show_declined_write_failed_toast"
        ) as mock_target, patch.object(
            manager, "_show_rejection_toast"
        ) as mock_rejection, patch.object(
            manager, "_show_grant_prompt_toast"
        ) as mock_grant, patch.object(
            manager, "_show_soft_allow_write_failed_toast"
        ) as mock_soft_allow:
            manager._check_queues_and_events()

        mock_target.assert_called_once()
        mock_rejection.assert_not_called()
        mock_grant.assert_not_called()
        mock_soft_allow.assert_not_called()
