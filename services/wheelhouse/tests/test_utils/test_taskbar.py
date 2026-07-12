"""Tests for taskbar.py - Windows taskbar visibility control.

Tests cover:
- hide_taskbar finds and hides the taskbar window
- show_taskbar finds and shows the taskbar window
- is_taskbar_visible checks taskbar state
- Handles missing taskbar window gracefully
- Handles already-hidden/visible states
"""

from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def mock_user32():
    with patch("utils.taskbar.user32") as m:
        m.FindWindowW = Mock(return_value=12345)  # fake hwnd
        m.IsWindowVisible = Mock(return_value=True)
        m.ShowWindow = Mock()
        yield m


class TestHideTaskbar:
    """Tests for hide_taskbar function."""

    def test_hides_visible_taskbar(self, mock_user32):
        from utils.taskbar import hide_taskbar

        mock_user32.IsWindowVisible.return_value = True
        result = hide_taskbar()

        assert result is True
        mock_user32.ShowWindow.assert_called_once_with(12345, 0)  # SW_HIDE = 0

    def test_already_hidden_returns_true(self, mock_user32):
        from utils.taskbar import hide_taskbar

        mock_user32.IsWindowVisible.return_value = False
        result = hide_taskbar()

        assert result is True
        mock_user32.ShowWindow.assert_not_called()

    def test_no_taskbar_window_returns_false(self, mock_user32):
        from utils.taskbar import hide_taskbar

        mock_user32.FindWindowW.return_value = 0
        result = hide_taskbar()

        assert result is False

    def test_exception_returns_false(self, mock_user32):
        from utils.taskbar import hide_taskbar

        mock_user32.FindWindowW.side_effect = OSError("win32 error")
        result = hide_taskbar()

        assert result is False


class TestShowTaskbar:
    """Tests for show_taskbar function."""

    def test_shows_hidden_taskbar(self, mock_user32):
        from utils.taskbar import show_taskbar

        mock_user32.IsWindowVisible.return_value = False
        result = show_taskbar()

        assert result is True
        mock_user32.ShowWindow.assert_called_once_with(12345, 5)  # SW_SHOW = 5

    def test_already_visible_returns_true(self, mock_user32):
        from utils.taskbar import show_taskbar

        mock_user32.IsWindowVisible.return_value = True
        result = show_taskbar()

        assert result is True
        mock_user32.ShowWindow.assert_not_called()

    def test_no_taskbar_window_returns_false(self, mock_user32):
        from utils.taskbar import show_taskbar

        mock_user32.FindWindowW.return_value = 0
        result = show_taskbar()

        assert result is False

    def test_exception_returns_false(self, mock_user32):
        from utils.taskbar import show_taskbar

        mock_user32.FindWindowW.side_effect = OSError("win32 error")
        result = show_taskbar()

        assert result is False


class TestIsTaskbarVisible:
    """Tests for is_taskbar_visible function."""

    def test_visible_returns_true(self, mock_user32):
        from utils.taskbar import is_taskbar_visible

        mock_user32.IsWindowVisible.return_value = 1
        assert is_taskbar_visible() is True

    def test_hidden_returns_false(self, mock_user32):
        from utils.taskbar import is_taskbar_visible

        mock_user32.IsWindowVisible.return_value = 0
        assert is_taskbar_visible() is False

    def test_no_taskbar_window_returns_false(self, mock_user32):
        from utils.taskbar import is_taskbar_visible

        mock_user32.FindWindowW.return_value = 0
        assert is_taskbar_visible() is False

    def test_exception_returns_false(self, mock_user32):
        from utils.taskbar import is_taskbar_visible

        mock_user32.FindWindowW.side_effect = OSError("win32 error")
        assert is_taskbar_visible() is False
