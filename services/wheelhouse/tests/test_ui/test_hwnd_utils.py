"""Tests for ui.hwnd_utils.normalize_hwnd_for_foreground_compare (wh-oe7u.3).

The helper exists so insertion and retraction compare HWNDs through the
same root-normalization, eliminating Chromium/Electron false mismatches
where UIA exposes a renderer child HWND while GetForegroundWindow returns
the top-level frame.

Contract under test:
- 0/None input -> None (fail-closed)
- GetAncestor exception -> None (fail-closed; do not silently pass)
- GetAncestor returns 0 -> None (fail-closed)
- Valid HWND -> the GetAncestor(GA_ROOT) result
"""
from unittest.mock import patch

from ui.hwnd_utils import GA_ROOT, normalize_hwnd_for_foreground_compare


_MOD = "ui.hwnd_utils"


class TestNormalizeHwndForForegroundCompare:
    def test_zero_returns_none(self):
        """A zero HWND has no root; the helper must NOT call GetAncestor
        and must return None so callers fail closed."""
        assert normalize_hwnd_for_foreground_compare(0) is None

    def test_none_returns_none(self):
        assert normalize_hwnd_for_foreground_compare(None) is None

    @patch(f"{_MOD}.win32gui")
    def test_returns_root_for_child_hwnd(self, mock_win32gui):
        """Chromium-shaped case: child HWND captured by UIA, root HWND is
        the actual top-level window. The helper returns the root."""
        mock_win32gui.GetAncestor.return_value = 0xAAAA
        result = normalize_hwnd_for_foreground_compare(0xBBBB)
        assert result == 0xAAAA
        mock_win32gui.GetAncestor.assert_called_once_with(0xBBBB, GA_ROOT)

    @patch(f"{_MOD}.win32gui")
    def test_top_level_hwnd_returns_itself(self, mock_win32gui):
        """If the input is already the top-level HWND, GetAncestor(GA_ROOT)
        returns the same value -- normalized comparison is identity for
        non-child windows."""
        mock_win32gui.GetAncestor.return_value = 0xCAFE
        result = normalize_hwnd_for_foreground_compare(0xCAFE)
        assert result == 0xCAFE

    @patch(f"{_MOD}.win32gui")
    def test_get_ancestor_exception_returns_none(self, mock_win32gui):
        """GetAncestor raised (e.g. invalid HWND, dead window). Helper
        must return None so callers fail closed -- silently passing
        through the unnormalized hwnd would defeat the point."""
        mock_win32gui.GetAncestor.side_effect = OSError("invalid window handle")
        result = normalize_hwnd_for_foreground_compare(0xDEAD)
        assert result is None

    @patch(f"{_MOD}.win32gui")
    def test_get_ancestor_returns_zero_returns_none(self, mock_win32gui):
        """GetAncestor returned 0 (no ancestor / window destroyed): treat
        as failure, not as a valid 0 root."""
        mock_win32gui.GetAncestor.return_value = 0
        result = normalize_hwnd_for_foreground_compare(0xBEEF)
        assert result is None
