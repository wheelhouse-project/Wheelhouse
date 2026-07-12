"""Tests for the GUI-side try_anyway_clicked wiring (wh-iycks).

When a rejection toast is rendered, the GUI manager records the
rejection's correlation_token. When the user clicks "Try it anyway",
the toast emits a Qt ``try_anyway_clicked`` signal; the GUI manager
reads the stored correlation_token and forwards a
``try_anyway_clicked`` action onto commands_to_logic_queue via
``send_command``.

Coverage:
  * Rendering a rejection toast captures the correlation_token.
  * Click on the captured toast posts the canonical action+token to
    commands_to_logic_queue.
  * No correlation_token captured -> click is a noop (defensive).
  * The forwarded payload carries only ``action`` and
    ``correlation_token`` (privacy property).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

# wh-pytest-flaky-segfault: these tests construct GuiManager, which
# builds real Qt widgets; without a QApplication Qt aborts the whole
# interpreter (no traceback, output lost). The session-scoped qapp
# fixture guarantees one exists even when this file runs in isolation.
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


def _new_token() -> str:
    return str(uuid.uuid4())


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


# ---------------------------------------------------------------------------
# Token capture and click forwarding
# ---------------------------------------------------------------------------


class TestCorrelationTokenCapture:
    def test_show_rejection_captures_correlation_token(self, manager):
        token = _new_token()

        with patch("gui.notification") as _notify:
            # Bypass actual toast widget construction; we only need
            # to observe that the manager recorded the token from
            # the message.
            with patch.object(
                manager, "_rejection_suppression"
            ) as mock_suppress, patch(
                "rejection_toast.RejectionToast"
            ) as mock_toast_cls:
                mock_suppress.decide.return_value = MagicMock(
                    show=True, lifetime_ms=8000,
                )
                mock_toast_cls.return_value = MagicMock()

                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": ["Invoke"],
                    "app_friendly_name": "Zed Editor",
                    "correlation_token": token,
                })

        assert manager._last_rejection_token == token


class TestClickForwarding:
    def test_click_emits_try_anyway_event_with_token(self, manager):
        token = _new_token()
        manager._last_rejection_token = token

        manager._on_try_anyway_clicked()

        manager.commands_to_logic_queue.put_nowait.assert_called_once_with({
            "action": "try_anyway_clicked",
            "correlation_token": token,
        })

    def test_click_with_no_token_is_noop(self, manager):
        # Defensive: if a click somehow lands before show_rejection set
        # the token, the manager logs and drops -- no IPC.
        manager._last_rejection_token = None
        manager._on_try_anyway_clicked()
        manager.commands_to_logic_queue.put_nowait.assert_not_called()

    def test_click_payload_carries_only_action_and_token(self, manager):
        """Privacy property: the forwarded payload has exactly two keys."""

        token = _new_token()
        manager._last_rejection_token = token

        manager._on_try_anyway_clicked()

        payload = manager.commands_to_logic_queue.put_nowait.call_args.args[0]
        assert set(payload.keys()) == {"action", "correlation_token"}
        forbidden = {"text", "dictation", "transcript", "utterance", "content"}
        assert set(payload.keys()).isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# Foreground-grant before IPC (wh-override-paste-focus-drift round 2)
# ---------------------------------------------------------------------------


class TestSetForegroundGrant:
    """Verify the GUI grants Input the right to SetForegroundWindow.

    The Input process is two IPC hops away from the user's click and
    has no recent user-input attribution, so Windows refuses its
    SetForegroundWindow(target_hwnd) call. The GUI process IS the
    foreground process at click time (it just received the click)
    and so is allowed to call AllowSetForegroundWindow, which grants
    another process a one-shot right to set the foreground. The
    grant must happen BEFORE the IPC is queued so it is in place by
    the time Input's retry handler calls SetForegroundWindow.
    """

    def test_click_grants_set_foreground_before_send_command(self, manager):
        token = _new_token()
        manager._last_rejection_token = token

        call_order: list[str] = []

        def _grant_side_effect() -> None:
            call_order.append("grant")

        def _send_side_effect(_payload) -> None:
            call_order.append("send")

        manager.commands_to_logic_queue.put_nowait.side_effect = (
            _send_side_effect
        )
        with patch(
            "gui._grant_foreground_to_any_process",
            side_effect=_grant_side_effect,
        ) as mock_grant:
            manager._on_try_anyway_clicked()

        mock_grant.assert_called_once_with()
        assert call_order == ["grant", "send"], (
            "AllowSetForegroundWindow grant must precede the IPC send "
            "so the Input process has the right by the time it calls "
            "SetForegroundWindow."
        )

    def test_click_with_no_token_does_not_grant(self, manager):
        """No token means no IPC will go out; granting foreground in that
        case would consume the one-shot grant for a no-op and could
        let an unrelated process steal foreground."""

        manager._last_rejection_token = None
        with patch("gui._grant_foreground_to_any_process") as mock_grant:
            manager._on_try_anyway_clicked()
        mock_grant.assert_not_called()


# ---------------------------------------------------------------------------
# Wiring -- the toast's signal must connect to the manager's slot
# ---------------------------------------------------------------------------


class TestSignalWiring:
    def test_show_rejection_connects_signal_once(self, manager):
        """Calling _show_rejection_toast twice must NOT double-connect.

        Without explicit deduplication, every render would attach a new
        slot, and a click would fire N events. The manager must connect
        the signal only on first construction (the toast is reused
        across renders) or otherwise ensure idempotency.
        """

        with patch("gui.notification"):
            with patch.object(
                manager, "_rejection_suppression"
            ) as mock_suppress, patch(
                "rejection_toast.RejectionToast"
            ) as mock_toast_cls:
                mock_suppress.decide.return_value = MagicMock(
                    show=True, lifetime_ms=8000,
                )
                mock_instance = MagicMock()
                mock_toast_cls.return_value = mock_instance

                token1 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": token1,
                })
                connect_calls_after_first = (
                    mock_instance.try_anyway_clicked.connect.call_count
                )

                token2 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": token2,
                })
                connect_calls_after_second = (
                    mock_instance.try_anyway_clicked.connect.call_count
                )

        # Connect was called exactly once on first render and not again
        # on the second. The manager keeps a single toast instance and
        # connects the signal once (when the instance is constructed).
        assert connect_calls_after_first == 1
        assert connect_calls_after_second == 1

    def test_second_show_updates_token(self, manager):
        """The recorded token must follow the most recent rejection."""

        with patch("gui.notification"):
            with patch.object(
                manager, "_rejection_suppression"
            ) as mock_suppress, patch(
                "rejection_toast.RejectionToast"
            ) as mock_toast_cls:
                mock_suppress.decide.return_value = MagicMock(
                    show=True, lifetime_ms=8000,
                )
                mock_toast_cls.return_value = MagicMock()

                t1 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": t1,
                })
                t2 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": t2,
                })
        assert manager._last_rejection_token == t2

    def test_suppressed_repeat_updates_token(self, manager):
        """A same-key rejection suppressed by the cooldown still updates
        the active token (wh-vbvgf.3.1).

        The previous toast is still visible; without this update, the
        click on it would retry the older dictation. Updating the token
        on every rejection (shown or suppressed) keeps the visible
        button bound to the most recent dictation rejected for the
        same target.
        """

        with patch("gui.notification"):
            with patch.object(
                manager, "_rejection_suppression"
            ) as mock_suppress, patch(
                "rejection_toast.RejectionToast"
            ) as mock_toast_cls:
                mock_toast_cls.return_value = MagicMock()

                # First rejection -- shown.
                mock_suppress.decide.return_value = MagicMock(
                    show=True, lifetime_ms=8000,
                )
                t1 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": t1,
                })
                assert manager._last_rejection_token == t1

                # Second same-key rejection -- suppressed by cooldown.
                mock_suppress.decide.return_value = MagicMock(
                    show=False, lifetime_ms=4000,
                )
                t2 = _new_token()
                manager._show_rejection_toast({
                    "process_name": "zed.exe",
                    "class_name": "Zed::Window",
                    "control_type": "WindowControl",
                    "reason": "default_reject_paste_capable_class",
                    "supported_patterns": [],
                    "app_friendly_name": "Zed",
                    "correlation_token": t2,
                })

        assert manager._last_rejection_token == t2
