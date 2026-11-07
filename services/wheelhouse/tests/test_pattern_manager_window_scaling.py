"""Screen-fit sizing tests for the Pattern Manager dialog
(wh-pattern-window-scaling).

The dialog previously always called ``setMinimumSize(800, 500)`` /
``resize(900, 560)`` unconditionally, so on a screen whose available work
area is smaller than that -- a low-resolution laptop, or a normal screen
with a higher accessibility text-scaling factor applied (both shrink the
LOGICAL availableGeometry Qt reports) -- the dialog could open larger than
the screen. ``_clamp_dialog_size`` bounds both the minimum and the initial
size to the current screen's ``availableGeometry`` (already DPI-adjusted by
Qt into logical pixels, so no separate DPI math is needed here).

``_clamp_dialog_size`` is a pure function of ``QSize``/``QRect`` value
types, so most of these tests need no ``QApplication``. The final test
installs a fake screen BEFORE constructing a real dialog, then asserts
the constructed dialog's own sizes -- so it can only pass if ``__init__``
itself performed the clamp. The fake-screen seam is the codebase's
established one (see ``TestScreenIsRecordedWhenTheDragStarts`` in
``test_floating_button_resize_drag.py``), applied to the class rather
than an instance because the instance does not exist yet:
``_apply_screen_bounded_size`` calls ``self.screen()`` from Python, so a
plain Python attribute on the class shadows the inherited Qt binding.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QRect, QSize


def _clamp(preferred, minimum, available):
    from pattern_manager_dialog import _clamp_dialog_size
    return _clamp_dialog_size(
        QSize(*preferred), QSize(*minimum), QRect(*available)
    )


def test_1366x768_screen_uses_the_preferred_size_when_it_fits():
    # A plain 1366x768 laptop panel at 100% scaling, taskbar included:
    # comfortably larger than the preferred 900x560, so nothing should
    # shrink.
    min_size, initial_size = _clamp(
        (900, 560), (800, 500), (0, 0, 1366, 728)
    )
    assert (initial_size.width(), initial_size.height()) == (900, 560)
    assert (min_size.width(), min_size.height()) == (800, 500)


def test_1366x768_screen_at_150_percent_scaling_clamps_the_height():
    # Same 1366x768 panel with a 150% accessibility text-scaling factor
    # applied: Qt reports the SHRUNKEN logical availableGeometry
    # (~910x485), which is shorter than the preferred 560px height.
    available = (0, 0, 910, 485)
    min_size, initial_size = _clamp((900, 560), (800, 500), available)
    assert initial_size.width() <= 910
    assert initial_size.height() <= 485
    assert initial_size.height() < 560  # actually shrunk, not left at 560
    assert min_size.width() <= 910
    assert min_size.height() <= 485


def test_4k_200_percent_screen_does_not_oversize_the_dialog():
    # A 4K screen at 200% Windows scaling reports a 1920x1080 LOGICAL
    # availableGeometry (minus a taskbar); comfortably larger than the
    # preferred size. This guards against a DPI regression where someone
    # multiplies the dialog size by a device-pixel-ratio and ends up
    # requesting something too large for even a roomy high-DPI screen.
    available = (0, 0, 1920, 1040)
    min_size, initial_size = _clamp((900, 560), (800, 500), available)
    assert (initial_size.width(), initial_size.height()) == (900, 560)
    assert initial_size.width() <= 1920
    assert initial_size.height() <= 1040


def test_clamp_never_exceeds_a_screen_smaller_than_both_sizes():
    # Extreme case: available area smaller than even the minimum size.
    # The dialog must still never exceed the screen.
    available = (0, 0, 400, 300)
    min_size, initial_size = _clamp((900, 560), (800, 500), available)
    assert initial_size.width() <= 400
    assert initial_size.height() <= 300
    assert min_size.width() <= 400
    assert min_size.height() <= 300
    # The initial size is never smaller than the (also clamped) minimum.
    assert initial_size.width() >= min_size.width()
    assert initial_size.height() >= min_size.height()


@pytest.mark.usefixtures("qapp")
def test_dialog_construction_uses_the_current_screens_available_geometry(
    monkeypatch,
):
    """Wiring test: ``__init__`` must actually consult ``self.screen()``.

    The fake screen is installed BEFORE construction and this test never
    calls ``_apply_screen_bounded_size`` itself, so the assertions below
    fail unless ``__init__`` ran the clamp on its own.
    """
    from pattern_manager_dialog import (
        _MIN_DIALOG_SIZE,
        _PREFERRED_DIALOG_SIZE,
        PatternManagerDialog,
        _clamp_dialog_size,
    )

    available = QRect(0, 0, 500, 400)

    class _FakeScreen:
        def availableGeometry(self):
            return available

    # ``_apply_screen_bounded_size`` calls ``self.screen()`` from Python,
    # so a Python attribute on the class shadows the inherited Qt binding
    # for any instance built while the patch stands. monkeypatch removes
    # it when the test ends.
    monkeypatch.setattr(
        PatternManagerDialog, "screen", lambda self: _FakeScreen()
    )

    expected_min, expected_initial = _clamp_dialog_size(
        _PREFERRED_DIALOG_SIZE, _MIN_DIALOG_SIZE, available
    )
    # The fake screen is smaller than BOTH the minimum and the preferred
    # size, so an __init__ that skipped the clamp cannot land on these
    # numbers by accident.
    assert expected_min.width() < _MIN_DIALOG_SIZE.width()
    assert expected_min.height() < _MIN_DIALOG_SIZE.height()
    assert expected_initial.width() < _PREFERRED_DIALOG_SIZE.width()
    assert expected_initial.height() < _PREFERRED_DIALOG_SIZE.height()

    dialog = PatternManagerDialog(parent=None)

    assert dialog.minimumSize().width() == expected_min.width()
    assert dialog.minimumSize().height() == expected_min.height()
    assert dialog.size().width() == expected_initial.width()
    assert dialog.size().height() == expected_initial.height()


# ---------------------------------------------------------------------------
# wh-pattern-window-full-height: the first show fills the available height.
#
# These tests do NOT call ``dialog.show()``: a real top-level window would
# flash on screen and could take focus. ``winId()`` creates the native window
# without showing it, and from then on Qt reports the real title-bar height
# in ``frameGeometry()`` (measured on Ikon 2026-10-26: geometry height 560,
# frameGeometry height 588). The tests then call the dialog's own
# ``showEvent`` directly, which is the code path a real ``show()`` runs.
# The frame is measured from the same native window that the assertions
# read, so the tests need no measurement tolerance: the height must be exact.
# ---------------------------------------------------------------------------


def _first_show(monkeypatch, available):
    from PySide6.QtGui import QShowEvent

    from pattern_manager_dialog import PatternManagerDialog

    class _FakeScreen:
        def availableGeometry(self):
            return available

    monkeypatch.setattr(
        PatternManagerDialog, "screen", lambda self: _FakeScreen()
    )
    dialog = PatternManagerDialog(parent=None)
    dialog.winId()
    # Without a title bar the test could not tell a frame-aware fit from one
    # that sizes the client area alone.
    assert dialog.frameGeometry().height() > dialog.geometry().height()
    dialog.showEvent(QShowEvent())
    return dialog


@pytest.mark.usefixtures("qapp")
@pytest.mark.parametrize(
    "available",
    [
        QRect(0, 0, 1920, 1040),
        QRect(100, 40, 1600, 900),
        # A screen to the right of, or to the left of, the place where Qt
        # first put the window: the fit must pull the frame inside it.
        QRect(3000, 0, 1600, 900),
        QRect(-1000, 0, 1000, 900),
    ],
    ids=["taskbar-at-bottom", "offset-screen", "screen-to-the-right",
         "screen-to-the-left"],
)
def test_first_show_fills_the_available_height_with_the_whole_frame(
    monkeypatch, available,
):
    from pattern_manager_dialog import _PREFERRED_DIALOG_SIZE

    dialog = _first_show(monkeypatch, available)

    frame = dialog.frameGeometry()
    assert frame.top() == available.top()
    assert frame.height() == available.height()
    assert frame.left() >= available.left()
    assert frame.right() <= available.right()
    # The default width does not change.
    assert dialog.width() == _PREFERRED_DIALOG_SIZE.width()


@pytest.mark.usefixtures("qapp")
def test_a_later_show_keeps_the_size_the_user_chose(monkeypatch):
    """A restore from minimized sends another show event; the full-height
    fit must not undo a size the user set after the first show."""
    from PySide6.QtGui import QShowEvent

    dialog = _first_show(monkeypatch, QRect(0, 0, 1920, 1040))
    dialog.resize(850, 600)

    dialog.showEvent(QShowEvent())

    assert dialog.size() == QSize(850, 600)


@pytest.mark.usefixtures("qapp")
def test_the_fit_leaves_a_reopen_free_to_center_the_window(monkeypatch):
    """Qt centers a QDialog on each show only while WA_Moved is False.
    Before this change every reopen of the Pattern Manager (closeEvent
    hides it, gui.py shows it again) centered it on the screen under the
    cursor; the fit's move() must not stop that (boss ruling 2026-10-26)."""
    from PySide6.QtCore import Qt

    dialog = _first_show(monkeypatch, QRect(0, 0, 1920, 1040))
    dialog.hide()

    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_Moved)
