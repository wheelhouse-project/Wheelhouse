"""The uncertain-category rejection notice is disabled (wh-dictation-gate-ux).

David reviewed the dictation-gate user experience on 2026-08-04 and
found it confusing end to end (invisible three-strikes learning, silent
word loss, asymmetric approve/block paths, a No that only stops the
question). Until the redesign under wh-dictation-gate-ux lands, the
"Wheelhouse isn't sure it can type here" notice -- the uncertain
category, the only one with a Try-it-anyway button -- is not shown.

The suppression is a GUI-side switch (``gui.SUPPRESS_UNCERTAIN_REJECTION_NOTICE``)
rather than a change to the Input-process emission gate
(``shared.rejection_category.should_emit_notice``) on purpose: the
event emission, word aggregation, text cache, and Try-it-anyway retry
machinery all stay in place and tested, so the redesign can re-enable
the flow by flipping one constant.

The elevated (administrator boundary) notice is NOT suppressed: it
explains a hard Windows limit the user can act on, and it existed
before the Try-it-anyway flow.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

# These tests construct GuiManager, which builds real Qt widgets;
# without a QApplication Qt aborts the interpreter. Same fixture set
# as test_gui_try_anyway.py (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("qapp", "mock_editor_window")


@pytest.fixture
def manager():
    with patch("gui.FloatingButton"), \
         patch("gui.WorkingDialog"), \
         patch("gui.pystray") as mock_pystray, \
         patch("gui.QTimer"):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager
        mgr = GuiManager(MagicMock(), MagicMock(), MagicMock())
        return mgr


def _uncertain_message() -> dict:
    return {
        "process_name": "zed.exe",
        "class_name": "Zed::Window",
        "control_type": "WindowControl",
        "reason": "default_reject_paste_capable_class",
        "supported_patterns": ["Invoke"],
        "app_friendly_name": "Zed Editor",
        "correlation_token": str(uuid.uuid4()),
    }


def _elevated_message() -> dict:
    return {
        "process_name": "regedit.exe",
        "class_name": "RegEdit_RegEdit",
        "control_type": "WindowControl",
        "reason": "elevated_process_window",
        "supported_patterns": [],
        "app_friendly_name": "Registry Editor",
        "correlation_token": str(uuid.uuid4()),
    }


class TestUncertainNoticeDisabled:
    def test_switch_defaults_to_suppressed(self):
        import gui

        assert gui.SUPPRESS_UNCERTAIN_REJECTION_NOTICE is True

    def test_uncertain_rejection_shows_no_toast(self, manager):
        with patch("plyer.notification"), patch.object(
            manager, "_rejection_suppression"
        ) as mock_suppress, patch(
            "rejection_toast.RejectionToast"
        ) as mock_toast_cls:
            mock_suppress.decide.return_value = MagicMock(
                show=True, lifetime_ms=8000,
            )
            mock_toast_cls.return_value = MagicMock()

            manager._show_rejection_toast(_uncertain_message())

        mock_toast_cls.assert_not_called()
        assert manager._rejection_toast is None

    def test_elevated_rejection_still_shows_toast(self, manager):
        with patch("plyer.notification"), patch.object(
            manager, "_rejection_suppression"
        ) as mock_suppress, patch(
            "rejection_toast.RejectionToast"
        ) as mock_toast_cls:
            mock_suppress.decide.return_value = MagicMock(
                show=True, lifetime_ms=8000,
            )
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_rejection_toast(_elevated_message())

        mock_toast_cls.assert_called_once()
        mock_instance.show_rejection.assert_called_once()

    def test_switch_off_restores_uncertain_toast(self, manager):
        """Flipping the switch back on (the redesign's re-enable path)
        must restore the previous behavior unchanged."""

        with patch("gui.SUPPRESS_UNCERTAIN_REJECTION_NOTICE", False), \
             patch("plyer.notification"), patch.object(
                 manager, "_rejection_suppression"
             ) as mock_suppress, patch(
                 "rejection_toast.RejectionToast"
             ) as mock_toast_cls:
            mock_suppress.decide.return_value = MagicMock(
                show=True, lifetime_ms=8000,
            )
            mock_instance = MagicMock()
            mock_toast_cls.return_value = mock_instance

            manager._show_rejection_toast(_uncertain_message())

        mock_toast_cls.assert_called_once()
        mock_instance.show_rejection.assert_called_once()
