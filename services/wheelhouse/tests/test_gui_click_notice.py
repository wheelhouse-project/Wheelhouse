"""Tests for the GUI-side show_click_notice rendering (wh-click-notice-no-gui-handler).

Logic's ``LogicController._forward_click_notice`` puts a
``show_click_notice`` action carrying a ``ClickNoticeEvent`` payload on
the GUI state queue for every non-ok click outcome (not_found /
ambiguous / execution_failed). The GUI's queue listener
(``_check_queues_and_events``) must route that action to
``_show_click_notice``, which:

  * Builds the ClickNoticeToast widget on first use; the instance is
    reused across events (lazy construction).
  * Reconstructs the ClickNoticeEvent from the message dict (tolerant
    of the extra ``action`` key the wire message carries).
  * Composes the v5 wording via ``compose_click_notice_wording``.
  * Calls ``show_notice`` on the widget.

Regression context: this handler was missing entirely. Logic sent the
notice and the ClickNoticeToast widget existed, but ``gui.py`` had no
``elif action == "show_click_notice"`` branch, so every failed click
was silent ("nothing happens"). Found during the manual release gate
(wh-yx50d). The existing ``test_click_flow.py`` only asserts Logic
SENDS the message; it never checked the GUI CONSUMES it -- which is
why the gap shipped. The TestQueueDispatch case below is the guard.
"""

from __future__ import annotations

import logging
from queue import Empty
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")


@pytest.fixture
def manager(qapp):
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
        "action": "show_click_notice",
        "outcome": "execution_failed",
        "reason": "invoke_com_error",
        "matched_name": "Cancel",
        "matched_names": ["Cancel"],
        "spoken_name": "cancel",
        "app_friendly_name": "",
        "snapshot_id": None,
        "trace_id": "T-test",
    }
    base.update(overrides)
    return base


class TestLazyConstruction:
    def test_widget_is_not_built_at_manager_init(self, manager):
        """The toast widget is created on first use, not during
        GuiManager construction. A Qt widget built before any click
        notice has fired is wasted memory and adds startup time.
        """
        assert manager._click_notice_toast is None


class TestRender:
    def test_well_formed_message_shows_toast(self, manager):
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_click_notice(_payload())

        mock_toast_cls.assert_called_once()
        mock_instance.show_notice.assert_called_once()

    def test_not_found_wording_reaches_widget(self, manager):
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_click_notice(_payload(
                outcome="not_found",
                reason=None,
                matched_name=None,
                matched_names=[],
                spoken_name="cancel",
            ))

        args = mock_instance.show_notice.call_args.args
        text = args[0] if args else mock_instance.show_notice.call_args.kwargs.get("text", "")
        assert "cancel" in text.lower()

    def test_widget_instance_reused_across_events(self, manager):
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_click_notice(_payload())
            manager._show_click_notice(_payload(outcome="not_found", reason=None,
                                                matched_name=None, matched_names=[]))

        mock_toast_cls.assert_called_once()
        assert mock_instance.show_notice.call_count == 2

    def test_malformed_message_is_dropped_without_raising(self, manager, caplog):
        """A payload that fails schema validation (e.g. a bad outcome)
        must be logged and dropped, not raised -- a version-skewed
        sender cannot crash the GUI loop (wh-uf54).
        """
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            with caplog.at_level(logging.WARNING):
                manager._show_click_notice({"action": "show_click_notice"})

        mock_toast_cls.assert_not_called()

    def test_rendered_notice_logs_info_with_wording_and_trace_id(
        self, manager, caplog, monkeypatch
    ):
        """wh-n29v.122: after show_notice is called, ONE INFO line records
        the composed wording and the trace_id -- the GUI-side half of the
        pipeline observability. Without it, a rendered-then-auto-dismissed
        toast is indistinguishable in the logs from one that never painted.

        The wording embeds the spoken click-target name, so the full text
        appears only with transcript logging on (wh-transcript-log-defaults);
        this test enables it to assert the observability contract.
        """
        monkeypatch.setenv("WHEELHOUSE_LOG_TRANSCRIPTS", "1")
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance
            with caplog.at_level(logging.INFO):
                manager._show_click_notice(_payload())

        mock_instance.show_notice.assert_called_once()
        records = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and "click notice rendered" in r.getMessage()
        ]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "T-test" in msg
        shown_text = mock_instance.show_notice.call_args.args[0]
        assert shown_text in msg

    def test_rendered_notice_log_redacts_wording_by_default(
        self, manager, caplog, monkeypatch
    ):
        """wh-transcript-log-defaults: with transcript logging off (the
        release default), the rendered-notice line still proves the notice
        painted (line present, trace_id intact) but hides the wording,
        which embeds the spoken click-target name."""
        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        with patch("click_notice_toast.ClickNoticeToast") as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance
            with caplog.at_level(logging.INFO):
                manager._show_click_notice(_payload())

        records = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and "click notice rendered" in r.getMessage()
        ]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "T-test" in msg
        shown_text = mock_instance.show_notice.call_args.args[0]
        assert shown_text not in msg
        assert "redacted" in msg

    def test_malformed_payload_drop_logs_no_rendered_info(self, manager, caplog):
        """The INFO line must sit AFTER show_notice: the malformed-payload
        drop path emits no 'rendered' line, so the log never claims a toast
        was shown when it was not."""
        with patch("click_notice_toast.ClickNoticeToast"):
            with caplog.at_level(logging.INFO):
                manager._show_click_notice({"action": "show_click_notice"})

        assert not [
            r for r in caplog.records
            if "click notice rendered" in r.getMessage()
        ]


class TestExceptionHandling:
    def test_exception_in_render_is_logged_not_raised(self, manager, caplog):
        """A broken Qt environment must not crash the GUI process. The
        handler logs at WARNING and returns.
        """
        with patch(
            "click_notice_toast.ClickNoticeToast",
            side_effect=RuntimeError("qt environment broken"),
        ):
            with caplog.at_level(logging.WARNING):
                manager._show_click_notice(_payload())

        assert any("click_notice" in r.message.lower() for r in caplog.records)


class TestQueueDispatch:
    """The dispatcher in ``_check_queues_and_events`` is the only
    production entry point for the notice. The direct handler tests
    above skip the dispatch branch; without this test a typo in the
    action string or a missing elif would silently drop every failed
    click's feedback while still passing every direct handler test.
    This is the test that was missing when the bug shipped.
    """

    def test_dispatch_routes_payload_to_handler(self, manager):
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        with patch.object(manager, "_show_click_notice") as mock_handler:
            manager._check_queues_and_events()

        mock_handler.assert_called_once_with(payload)

    def test_dispatch_does_not_invoke_other_toast_handlers(self, manager):
        payload = _payload()
        manager.state_from_logic_queue.get_nowait.side_effect = [payload, Empty()]

        with patch.object(manager, "_show_click_notice") as mock_target, \
             patch.object(manager, "_show_rejection_toast") as mock_rejection:
            manager._check_queues_and_events()

        mock_target.assert_called_once()
        mock_rejection.assert_not_called()
