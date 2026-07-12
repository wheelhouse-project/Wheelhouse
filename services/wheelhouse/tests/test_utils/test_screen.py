"""Tests for screen.py - Screen dimension utilities.

Tests cover:
- get_screen_size returns dimensions from win32api.GetSystemMetrics
  (wh-drop-pyautogui: pyautogui is gone -- its MouseInfo dependency is GPL)
- Fallback to 1920x1080 on error or degenerate metrics
"""

from unittest.mock import patch

import pytest


class TestGetScreenSize:
    """Tests for the get_screen_size function."""

    def test_returns_system_metrics_dimensions(self):
        from utils.screen import get_screen_size

        with patch("utils.screen.win32api") as mock_api:
            mock_api.GetSystemMetrics.side_effect = lambda i: {0: 2560, 1: 1440}[i]
            width, height = get_screen_size()

        assert width == 2560
        assert height == 1440

    def test_fallback_on_error(self):
        from utils.screen import get_screen_size

        with patch("utils.screen.win32api") as mock_api:
            mock_api.GetSystemMetrics.side_effect = RuntimeError("no display")
            width, height = get_screen_size()

        assert width == 1920
        assert height == 1080

    def test_fallback_on_zero_metrics(self):
        """GetSystemMetrics reports failure by returning 0, not raising."""
        from utils.screen import get_screen_size

        with patch("utils.screen.win32api") as mock_api:
            mock_api.GetSystemMetrics.return_value = 0
            width, height = get_screen_size()

        assert width == 1920
        assert height == 1080

    def test_returns_tuple(self):
        from utils.screen import get_screen_size

        with patch("utils.screen.win32api") as mock_api:
            mock_api.GetSystemMetrics.side_effect = lambda i: {0: 1920, 1: 1080}[i]
            result = get_screen_size()

        assert isinstance(result, tuple)
        assert len(result) == 2
