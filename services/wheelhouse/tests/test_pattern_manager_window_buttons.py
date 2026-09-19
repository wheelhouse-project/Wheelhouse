"""Window-chrome tests for the Pattern Manager dialog (wh-pattern-window-buttons).

The dialog previously had no minimize/maximize title-bar buttons, which also
blocks wh-pattern-window-voice (voice minimize/maximize needs the buttons/
styles to exist first). Covers acceptance criteria 1 and 3 of
wh-pattern-window-buttons: the window flags carry both hints, and the dialog
stays resizable. Criterion 2 (buttons work on the live window) is a manual
check per the bead and is not automated here.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

# wh-pytest-flaky-segfault: constructing the dialog builds real Qt widgets;
# without a QApplication Qt aborts the whole interpreter. The session-scoped
# qapp fixture guarantees one exists even when this file runs alone.
pytestmark = pytest.mark.usefixtures("qapp")


def _make_dialog():
    from pattern_manager_dialog import PatternManagerDialog
    return PatternManagerDialog(parent=None)


def test_window_flags_include_minimize_hint():
    dialog = _make_dialog()
    assert dialog.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint


def test_window_flags_include_maximize_hint():
    dialog = _make_dialog()
    assert dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint


def test_dialog_is_resizable():
    # A fixed-size dialog reports an unchanged maximumSize equal to its
    # minimumSize; a resizable one has room above the minimum.
    dialog = _make_dialog()
    assert not (dialog.windowFlags() & Qt.WindowType.MSWindowsFixedSizeDialogHint)
    assert dialog.maximumSize().width() > dialog.minimumSize().width()
    assert dialog.maximumSize().height() > dialog.minimumSize().height()
