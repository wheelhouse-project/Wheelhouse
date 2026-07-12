"""Tests for WindowMatcher abstraction.

WindowMatcher consolidates duplicated window matching logic from input_proc.py:
- enum_callback (lines 70-114)
- verification loop (lines 202-231)
- handle_activate (lines 274-275)
"""
import pytest
from unittest.mock import MagicMock, patch

from ui.window_matcher import WindowMatcher


class TestWindowMatcher:
    """Tests for the WindowMatcher class."""

    # ========================================================================
    # is_process_target tests
    # ========================================================================

    def test_is_process_target_with_exe_extension(self):
        """Target ending with .exe should return True."""
        assert WindowMatcher.is_process_target("brave.exe") is True

    def test_is_process_target_with_uppercase_exe(self):
        """Case-insensitive .EXE should return True."""
        assert WindowMatcher.is_process_target("Chrome.EXE") is True

    def test_is_process_target_with_title_pattern(self):
        """Title pattern (no .exe) should return False."""
        assert WindowMatcher.is_process_target("Visual Studio Code") is False

    def test_is_process_target_with_regex_pattern(self):
        """Regex pattern should return False."""
        assert WindowMatcher.is_process_target(".*GitHub.*") is False

    # ========================================================================
    # get_process_name tests
    # ========================================================================

    @patch("ui.window_matcher.psutil")
    @patch("ui.window_matcher.win32process")
    def test_get_process_name_returns_lowercase(self, mock_win32process, mock_psutil):
        """Process name should be returned lowercase."""
        mock_win32process.GetWindowThreadProcessId.return_value = (12345, 6789)
        mock_process = MagicMock()
        mock_process.name.return_value = "Brave.EXE"
        mock_psutil.Process.return_value = mock_process

        result = WindowMatcher.get_process_name(12345)
        assert result == "brave.exe"

    @patch("ui.window_matcher.psutil")
    @patch("ui.window_matcher.win32process")
    def test_get_process_name_handles_exception(self, mock_win32process, mock_psutil):
        """Should return None on exception."""
        mock_win32process.GetWindowThreadProcessId.side_effect = Exception("Access denied")

        result = WindowMatcher.get_process_name(12345)
        assert result is None

    @patch("ui.window_matcher.psutil")
    @patch("ui.window_matcher.win32process")
    def test_get_process_name_handles_psutil_exception(self, mock_win32process, mock_psutil):
        """Should return None when psutil.Process fails."""
        mock_win32process.GetWindowThreadProcessId.return_value = (12345, 6789)
        mock_psutil.Process.side_effect = Exception("Process not found")

        result = WindowMatcher.get_process_name(12345)
        assert result is None

    # ========================================================================
    # matches tests - process name matching
    # ========================================================================

    @patch.object(WindowMatcher, "get_process_name")
    def test_matches_by_process_name_exact(self, mock_get_process_name):
        """Exact process name match should return True."""
        mock_get_process_name.return_value = "brave.exe"

        result = WindowMatcher.matches(12345, "brave.exe")
        assert result is True

    @patch.object(WindowMatcher, "get_process_name")
    def test_matches_by_process_name_case_insensitive(self, mock_get_process_name):
        """Process name matching should be case-insensitive."""
        mock_get_process_name.return_value = "brave.exe"

        result = WindowMatcher.matches(12345, "BRAVE.EXE")
        assert result is True

    @patch.object(WindowMatcher, "get_process_name")
    def test_matches_by_process_name_no_match(self, mock_get_process_name):
        """Non-matching process name should return False."""
        mock_get_process_name.return_value = "chrome.exe"

        result = WindowMatcher.matches(12345, "brave.exe")
        assert result is False

    @patch.object(WindowMatcher, "get_process_name")
    def test_matches_by_process_name_none_proc(self, mock_get_process_name):
        """None process name should return False."""
        mock_get_process_name.return_value = None

        result = WindowMatcher.matches(12345, "brave.exe")
        assert result is False

    # ========================================================================
    # matches tests - title pattern matching
    # ========================================================================

    @patch("ui.window_matcher.win32gui")
    def test_matches_by_title_pattern_exact(self, mock_win32gui):
        """Exact title substring match should return True."""
        mock_win32gui.GetWindowText.return_value = "Visual Studio Code"

        result = WindowMatcher.matches(12345, "Visual Studio")
        assert result is True

    @patch("ui.window_matcher.win32gui")
    def test_matches_by_title_pattern_case_insensitive(self, mock_win32gui):
        """Title matching should be case-insensitive."""
        mock_win32gui.GetWindowText.return_value = "Visual Studio Code"

        result = WindowMatcher.matches(12345, "visual studio")
        assert result is True

    @patch("ui.window_matcher.win32gui")
    def test_matches_by_title_regex(self, mock_win32gui):
        """Regex pattern should work for title matching."""
        mock_win32gui.GetWindowText.return_value = "MyApp - GitHub Repository"

        result = WindowMatcher.matches(12345, ".*GitHub.*")
        assert result is True

    @patch("ui.window_matcher.win32gui")
    def test_matches_by_title_no_match(self, mock_win32gui):
        """Non-matching title should return False."""
        mock_win32gui.GetWindowText.return_value = "Notepad"

        result = WindowMatcher.matches(12345, "Visual Studio")
        assert result is False

    @patch("ui.window_matcher.win32gui")
    def test_matches_by_title_empty_title(self, mock_win32gui):
        """Empty window title should return False."""
        mock_win32gui.GetWindowText.return_value = ""

        result = WindowMatcher.matches(12345, "Visual Studio")
        assert result is False

    # ========================================================================
    # is_visible tests
    # ========================================================================

    @patch("ui.window_matcher.win32gui")
    def test_is_visible_returns_true_for_visible_window(self, mock_win32gui):
        """Visible window should return True."""
        mock_win32gui.IsWindowVisible.return_value = True

        result = WindowMatcher.is_visible(12345)
        assert result is True

    @patch("ui.window_matcher.win32gui")
    def test_is_visible_returns_false_for_hidden_window(self, mock_win32gui):
        """Hidden window should return False."""
        mock_win32gui.IsWindowVisible.return_value = False

        result = WindowMatcher.is_visible(12345)
        assert result is False

    # ========================================================================
    # get_window_title tests
    # ========================================================================

    @patch("ui.window_matcher.win32gui")
    def test_get_window_title_returns_title(self, mock_win32gui):
        """Should return window title string."""
        mock_win32gui.GetWindowText.return_value = "Test Window"

        result = WindowMatcher.get_window_title(12345)
        assert result == "Test Window"

    @patch("ui.window_matcher.win32gui")
    def test_get_window_title_handles_exception(self, mock_win32gui):
        """Should return empty string on exception."""
        mock_win32gui.GetWindowText.side_effect = Exception("Window destroyed")

        result = WindowMatcher.get_window_title(12345)
        assert result == ""
