"""Tests for the GUI-side text_target_grant_prompt rendering and dedup
(wh-bqv9c).

When the Logic process publishes a ``text_target_grant_prompt`` action
on the state queue, the GUI's queue listener calls
``_show_grant_prompt_toast``. The handler:

  * Validates the payload via ``TextTargetGrantPromptEvent.from_dict``.
  * Skips if the per-tuple identity has already been acted on this
    session (Yes / No clicked) -- per-tuple per-session dedup.
  * Composes title/body strings with the friendly app name and count.
  * Builds (lazily) and shows the GrantPromptToast widget.
  * Records the per-tuple identity so a Yes / No click can attach it
    to the IPC payload sent back to Logic.

Coverage:
  * A well-formed message renders the toast.
  * A malformed message logs and drops without raising.
  * A second event for the same tuple after a Yes click is suppressed.
  * A second event for the same tuple after a No click is suppressed.
  * A second event for the same tuple after a dismiss-without-click
    is NOT suppressed (re-fires per the bead spec).
  * A second event for a different tuple is shown even when the first
    tuple has been acted on.
  * The toast widget is built lazily (not in __init__).
"""

from __future__ import annotations

import logging
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
        mgr = GuiManager(MagicMock(), cmds_q, state_q)
        return mgr


def _payload(**overrides) -> dict:
    base = {
        "action": "text_target_grant_prompt",
        "process_name": "zed.exe",
        "class_name": "zed::Workspace",
        "control_type": "Pane",
        "app_friendly_name": "Zed",
        "count": 3,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Lazy construction
# ---------------------------------------------------------------------------


class TestLazyConstruction:
    def test_widget_is_not_built_at_manager_init(self, manager):
        """The toast widget is created on first use, not during
        GuiManager construction. A Qt widget built before any
        rejection has fired is wasted memory and adds startup time."""

        assert manager._grant_prompt_toast is None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


class TestRender:
    def test_well_formed_message_shows_toast(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload())

        # Widget was built once and show_prompt was called.
        mock_toast_cls.assert_called_once()
        mock_instance.show_prompt.assert_called_once()

    def test_show_prompt_carries_friendly_name_in_title_and_body(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                app_friendly_name="Zed Editor",
                count=4,
            ))

        kwargs = mock_instance.show_prompt.call_args.kwargs
        if not kwargs:
            args = mock_instance.show_prompt.call_args.args
            kwargs = {"title": args[0], "body": args[1]} if len(args) >= 2 else {}
        assert "Zed Editor" in kwargs.get("title", "")
        assert "Zed Editor" in kwargs.get("body", "")
        assert "4" in kwargs.get("body", "")

    def test_malformed_message_drops_without_raise(self, manager, caplog):
        bad = {
            "action": "text_target_grant_prompt",
            # missing fields
        }
        with caplog.at_level(logging.WARNING):
            manager._show_grant_prompt_toast(bad)
        # The toast should not have been built.
        assert manager._grant_prompt_toast is None


# ---------------------------------------------------------------------------
# Per-tuple per-session dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_second_event_after_yes_is_suppressed(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload())
            assert mock_instance.show_prompt.call_count == 1

            # Simulate the user clicking Yes -- the manager records the
            # tuple in the dedup set.
            manager._on_grant_prompt_yes_clicked()

            manager._show_grant_prompt_toast(_payload())

        # Second call did NOT show again (still one show).
        assert mock_instance.show_prompt.call_count == 1

    def test_second_event_after_no_is_not_dedup_at_gui(self, manager):
        """wh-vbvgf.12.1 (codex review): No clicks are NOT added to
        the GUI dedup set; Logic owns the per-run suppression
        authority. If a second text_target_grant_prompt message
        somehow reaches the GUI after a No (e.g., Logic restarted and
        cleared its suppression), the GUI must re-show the toast --
        because Logic's decision to forward IS the authority. The GUI
        side has no business overriding."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = False
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload())
            manager._on_grant_prompt_no_clicked()
            manager._show_grant_prompt_toast(_payload())

        # Both messages reached show_prompt. Logic forwarder is the
        # authority; in a real run, Logic would have suppressed the
        # second message at the source.
        assert mock_instance.show_prompt.call_count == 2

    def test_dismiss_without_click_re_fires_on_next_event(self, manager):
        """Per bead spec: the toast appears at most once per tuple per
        session, EXCEPT that a dismiss-without-click resets the dedup
        for that tuple so the next threshold event re-fires the toast.
        """

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload())

            # Simulate a dismiss-without-click (toast emitted ``dismissed``).
            manager._on_grant_prompt_dismissed()

            manager._show_grant_prompt_toast(_payload())

        # Both events showed the toast.
        assert mock_instance.show_prompt.call_count == 2

    def test_different_tuple_shows_after_first_tuple_acted_on(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = True
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))
            # Simulate Yes click: production widget closes itself
            # (wh-vbvgf.7.2). Reflect that on the mock so the manager's
            # visibility check sees the toast as gone.
            manager._on_grant_prompt_yes_clicked()
            mock_instance.isVisible.return_value = False

            manager._show_grant_prompt_toast(_payload(
                process_name="notepad.exe", class_name="Edit",
                control_type="Document",
            ))

        # Second call (different tuple) showed.
        assert mock_instance.show_prompt.call_count == 2

    def test_different_tuple_dropped_while_visible(self, manager):
        """Codex review wh-vbvgf.7.2: a visible toast for tuple A must
        not be replaced by a toast for tuple B. Replacing mid-
        presentation can misattribute a click the user had already
        decided to make for tuple A."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = True
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))
            assert mock_instance.show_prompt.call_count == 1

            # Different tuple while the first toast is visible: drop.
            manager._show_grant_prompt_toast(_payload(
                process_name="notepad.exe", class_name="Edit",
                control_type="Document",
            ))

        # Second call did NOT call show_prompt -- the different-tuple
        # event was dropped while the first toast was visible.
        assert mock_instance.show_prompt.call_count == 1
        # The active tuple was NOT swapped out underneath the visible toast.
        assert manager._active_grant_tuple == ("zed.exe", "A", "Pane")

    def test_same_tuple_re_shows_while_visible(self, manager):
        """A second event for the SAME tuple while the toast is visible
        is allowed -- this can happen when the click counter increments
        again before the user dismisses, and the body wording should
        update with the new count."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = True
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A",
                control_type="Pane", count=3,
            ))
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A",
                control_type="Pane", count=4,
            ))

        # Both calls hit show_prompt with the updated count in the body.
        assert mock_instance.show_prompt.call_count == 2

    def test_different_tuple_shown_after_dismiss(self, manager):
        """Once the first toast is dismissed (no longer visible), a
        different-tuple event is allowed to display."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = True
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))
            # Simulate dismiss; the widget is no longer visible.
            mock_instance.isVisible.return_value = False
            manager._on_grant_prompt_dismissed()

            manager._show_grant_prompt_toast(_payload(
                process_name="notepad.exe", class_name="Edit",
                control_type="Document",
            ))

        assert mock_instance.show_prompt.call_count == 2


# ---------------------------------------------------------------------------
# Active-tuple tracking
# ---------------------------------------------------------------------------


class TestActiveTuple:
    """The GUI manager records the identity tuple of the most recent
    grant prompt so a subsequent Yes / No click can attach it when
    the click is forwarded to Logic."""

    def test_show_records_active_tuple(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe",
                class_name="zed::Workspace",
                control_type="Pane",
            ))

        assert manager._active_grant_tuple == (
            "zed.exe", "zed::Workspace", "Pane",
        )

    def test_active_tuple_unchanged_when_show_prompt_raises(self, manager):
        """Deepseek review wh-vbvgf.8.1: an exception inside show_prompt
        must not leave _active_grant_tuple pointing at a tuple whose
        toast was never displayed. The assignment lives after the
        successful show, so a raise leaves the previous value in
        place."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_instance.isVisible.return_value = False
            mock_instance.show_prompt.side_effect = RuntimeError(
                "simulated Qt failure"
            )
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe",
                class_name="zed::Workspace",
                control_type="Pane",
            ))

        assert manager._active_grant_tuple is None

    def test_second_show_updates_active_tuple(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))
            manager._on_grant_prompt_dismissed()
            manager._show_grant_prompt_toast(_payload(
                process_name="notepad.exe", class_name="Edit",
                control_type="Document",
            ))

        assert manager._active_grant_tuple == (
            "notepad.exe", "Edit", "Document",
        )


# ---------------------------------------------------------------------------
# Signal wiring
# ---------------------------------------------------------------------------


class TestYesClickIPC:
    """wh-8d81z: a Yes click forwards a grant_prompt_yes_clicked action
    to Logic carrying the active tuple identity."""

    def test_yes_click_emits_action_with_tuple(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe",
                class_name="zed::Workspace",
                control_type="Pane",
            ))

        manager._on_grant_prompt_yes_clicked()

        manager.commands_to_logic_queue.put_nowait.assert_called_once_with({
            "action": "grant_prompt_yes_clicked",
            "process_name": "zed.exe",
            "class_name": "zed::Workspace",
            "control_type": "Pane",
        })

    def test_yes_click_with_no_active_tuple_is_noop(self, manager):
        manager._active_grant_tuple = None
        manager._on_grant_prompt_yes_clicked()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_yes_click_marks_acted_on(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))

        manager._on_grant_prompt_yes_clicked()

        assert ("zed.exe", "A", "Pane") in manager._grant_prompt_acted_on

    def test_yes_click_does_not_dedup_when_queue_is_full(self, manager):
        """Codex review wh-vbvgf.9.1: if commands_to_logic_queue.put_nowait
        raises Full, the click is dropped and the GUI dedup set must
        NOT be updated. Otherwise the user gets one shot at granting
        and loses the entire follow-up path until the GUI restarts."""

        from queue import Full

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))

        manager.commands_to_logic_queue.put_nowait.side_effect = (
            Full("simulated backpressure")
        )

        manager._on_grant_prompt_yes_clicked()

        # The tuple is NOT marked as acted-on, so the next threshold
        # event for the same tuple will re-show the toast and give the
        # user another chance to grant.
        assert ("zed.exe", "A", "Pane") not in manager._grant_prompt_acted_on

    def test_yes_click_payload_carries_only_action_and_identity(self, manager):
        """Privacy property: the forwarded payload has exactly the
        four required keys -- action plus the three identity fields.
        No dictation text, no correlation_token, no other content."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload())

        manager._on_grant_prompt_yes_clicked()

        payload = manager.commands_to_logic_queue.put_nowait.call_args.args[0]
        assert set(payload.keys()) == {
            "action", "process_name", "class_name", "control_type",
        }
        forbidden = {
            "text", "dictation", "transcript", "utterance", "content",
            "correlation_token",
        }
        assert set(payload.keys()).isdisjoint(forbidden)


class TestNoClickIPC:
    """wh-vdt1t: a No click forwards a grant_prompt_no_clicked action
    to Logic carrying the active tuple identity. The counter is NOT
    reset; the Logic-side handler records suppression only."""

    def test_no_click_emits_action_with_tuple(self, manager):
        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe",
                class_name="zed::Workspace",
                control_type="Pane",
            ))

        manager._on_grant_prompt_no_clicked()

        manager.commands_to_logic_queue.put_nowait.assert_called_once_with({
            "action": "grant_prompt_no_clicked",
            "process_name": "zed.exe",
            "class_name": "zed::Workspace",
            "control_type": "Pane",
        })

    def test_no_click_with_no_active_tuple_is_noop(self, manager):
        manager._active_grant_tuple = None
        manager._on_grant_prompt_no_clicked()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_no_click_does_not_dedup_when_queue_is_full(self, manager):
        """Full queue must NOT update the GUI dedup set; the next
        threshold event re-fires the toast and gives the user another
        chance to choose."""

        from queue import Full

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))

        manager.commands_to_logic_queue.put_nowait.side_effect = (
            Full("simulated backpressure")
        )

        manager._on_grant_prompt_no_clicked()

        assert ("zed.exe", "A", "Pane") not in manager._grant_prompt_acted_on

    def test_no_click_success_does_not_add_to_gui_dedup(self, manager):
        """wh-vbvgf.12.1 (codex review): Logic owns per-run No
        suppression; the GUI must NOT add to its own dedup set on
        No. Otherwise the GUI dedup outlives a Logic restart and
        silently keeps suppressing a tuple Logic has already cleared.
        """

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload(
                process_name="zed.exe", class_name="A", control_type="Pane",
            ))

        manager._on_grant_prompt_no_clicked()

        # The IPC was sent (Logic-side suppression takes effect)...
        manager.commands_to_logic_queue.put_nowait.assert_called_once()
        # ...but the GUI-side dedup set remains empty.
        assert ("zed.exe", "A", "Pane") not in manager._grant_prompt_acted_on

    def test_no_click_payload_carries_only_action_and_identity(self, manager):
        """Privacy property: the forwarded payload has exactly the
        four required keys."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_toast_cls.return_value = MagicMock()
            manager._show_grant_prompt_toast(_payload())

        manager._on_grant_prompt_no_clicked()

        payload = manager.commands_to_logic_queue.put_nowait.call_args.args[0]
        assert set(payload.keys()) == {
            "action", "process_name", "class_name", "control_type",
        }
        forbidden = {
            "text", "dictation", "transcript", "utterance", "content",
            "correlation_token",
        }
        assert set(payload.keys()).isdisjoint(forbidden)


class TestSignalWiring:
    def test_signals_connected_once(self, manager):
        """Calling _show_grant_prompt_toast twice must NOT
        double-connect signals; the toast is reused."""

        with patch(
            "grant_prompt_toast.GrantPromptToast"
        ) as mock_toast_cls:
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_grant_prompt_toast(_payload())
            yes_after_first = (
                mock_instance.yes_clicked.connect.call_count
            )
            no_after_first = (
                mock_instance.no_clicked.connect.call_count
            )
            dismissed_after_first = (
                mock_instance.dismissed.connect.call_count
            )

            manager._on_grant_prompt_dismissed()
            manager._show_grant_prompt_toast(_payload())

            yes_after_second = (
                mock_instance.yes_clicked.connect.call_count
            )
            no_after_second = (
                mock_instance.no_clicked.connect.call_count
            )
            dismissed_after_second = (
                mock_instance.dismissed.connect.call_count
            )

        assert yes_after_first == 1
        assert yes_after_second == 1
        assert no_after_first == 1
        assert no_after_second == 1
        assert dismissed_after_first == 1
        assert dismissed_after_second == 1
