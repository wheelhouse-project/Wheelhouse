"""Tests for removing the Windows-drawn border around the floating button.

Wheelhouse's own paint code never draws a border: ``FloatingButton.paintEvent``
sets ``Qt.PenStyle.NoPen`` and fills a plain circle. The grey ring a user sees
around the button in EVERY colour state is therefore drawn by Windows, not by
Wheelhouse -- Windows 11 gives top-level windows rounded corners and a thin
border by default, and the floating button is a top-level frameless window.

The fix asks Windows to stop doing that for this one window, via
``DwmSetWindowAttribute``:

* ``DWMWA_WINDOW_CORNER_PREFERENCE`` (33) = ``DWMWCP_DONOTROUND`` (1)
* ``DWMWA_BORDER_COLOR`` (34) = ``DWMWA_COLOR_NONE`` (0xFFFFFFFE)

Both attributes need Windows 11 build 22000+. On older builds the call returns
a failure code, which the helper ignores -- the button simply keeps the border
it has today rather than crashing.

The window is re-created by Qt whenever window flags change, and a re-created
window gets Windows' default border back, so the request is re-issued from
``showEvent`` rather than once in ``__init__``.
"""

from __future__ import annotations

import ctypes
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager/QDialog construction out of this file (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")


# The documented Win32 constants this feature depends on. Spelled out here
# rather than imported from gui.py: importing them would make the test agree
# with whatever the source says, including a wrong value.
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE


@pytest.fixture
def button(qapp):
    """A real FloatingButton, so winId() is a genuine window handle."""
    from gui import FloatingButton
    return FloatingButton()


@pytest.fixture
def fake_dwmapi():
    """Stand in for the real dwmapi library so no window is actually altered.

    ``ctypes.windll`` caches loaded libraries as plain attributes, so setting
    one replaces it for the duration of the patch.
    """
    mock = MagicMock()
    mock.DwmSetWindowAttribute.return_value = 0  # S_OK
    with patch.object(ctypes.windll, "dwmapi", mock, create=True):
        yield mock


def _attribute_calls(mock_dwmapi):
    """Map each DwmSetWindowAttribute call to (window_handle, attribute, value).

    The third argument is a pointer built by ``ctypes.byref``; ``._obj`` reads
    back the object it points at, so the test asserts the VALUE Windows would
    actually receive, not merely that some pointer was passed.
    """
    calls = {}
    for call in mock_dwmapi.DwmSetWindowAttribute.call_args_list:
        window_handle, attribute, pointer, size = call.args
        calls[attribute] = (window_handle, pointer._obj.value, size)
    return calls


class TestBorderRemovalRequest:
    """The helper must ask Windows for both changes, on the right window."""

    def test_asks_windows_not_to_round_the_corners(self, button, fake_dwmapi):
        button._apply_borderless_window_style()
        calls = _attribute_calls(fake_dwmapi)

        assert DWMWA_WINDOW_CORNER_PREFERENCE in calls, (
            "never asked Windows to stop rounding the window corners"
        )
        window_handle, value, size = calls[DWMWA_WINDOW_CORNER_PREFERENCE]
        assert value == DWMWCP_DONOTROUND
        assert size == 4

    def test_asks_windows_for_no_border_colour(self, button, fake_dwmapi):
        button._apply_borderless_window_style()
        calls = _attribute_calls(fake_dwmapi)

        assert DWMWA_BORDER_COLOR in calls, (
            "never asked Windows to drop the window border"
        )
        window_handle, value, size = calls[DWMWA_BORDER_COLOR]
        assert value == DWMWA_COLOR_NONE
        assert size == 4

    def test_targets_this_buttons_own_window(self, button, fake_dwmapi):
        """Both requests must name the button's real window.

        Binding the handle is what stops the test passing for the wrong
        reason: a helper that hardcoded 0, or that borrowed some other
        window, would satisfy the value assertions above but change nothing
        on screen.
        """
        expected_handle = int(button.winId())
        button._apply_borderless_window_style()
        calls = _attribute_calls(fake_dwmapi)

        assert calls, "no window attribute was set at all"
        for attribute, (window_handle, _value, _size) in calls.items():
            assert window_handle == expected_handle, (
                f"attribute {attribute} was applied to window {window_handle}, "
                f"not the floating button's own window {expected_handle}"
            )

    def test_survives_windows_versions_without_these_attributes(
        self, button, fake_dwmapi
    ):
        """Windows 10 rejects both attributes. That must not reach the user.

        The button is created during GUI startup, so an exception here would
        take down the whole tray/button surface rather than leaving a cosmetic
        border in place.
        """
        fake_dwmapi.DwmSetWindowAttribute.side_effect = OSError("E_INVALIDARG")
        button._apply_borderless_window_style()  # must not raise


class TestAppliedOnShow:
    """A helper that exists but is never called is the exact failure this
    guards: the border would stay on screen while every test above passed.

    Qt destroys and re-creates the native window when window flags change,
    and the re-created window comes back with Windows' default border, so
    showEvent -- not __init__ -- is the correct place to re-issue it.
    """

    def test_show_event_applies_the_borderless_style(self, button):
        from PySide6.QtGui import QShowEvent

        with patch.object(button, "_apply_borderless_window_style") as applied:
            button.showEvent(QShowEvent())

        applied.assert_called_once()
