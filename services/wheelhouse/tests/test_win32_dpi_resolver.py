"""Unit tests for the _win32_dpi_resolver production seam (wh-click-dpi-permonitor).

_win32_dpi_resolver resolves the real per-monitor effective DPI for an
HMONITOR-as-int by calling shcore's GetDpiForMonitor. It is the fail-soft
seam wired into the ElementFinder's clear-winner cursor-proximity tiebreaker.
The consumer (clear_winner_rule.decide) abstains to an 'ambiguous' outcome if
the resolver raises, returns None, returns a non-finite value, or returns
<= 0, so the resolver MUST never raise and MUST return a positive finite float;
on any failure it returns 96.0 (the 100%-scaling default).

These drive all four outcomes through the real function with ctypes.windll
patched: the function reads ctypes.windll.shcore.GetDpiForMonitor inline.
"""

import ctypes
from unittest.mock import patch

from ui.ui_action_handler import _win32_dpi_resolver


def _make_windll(*, write_dpi=None, hresult=0, raise_exc=None):
    """Build a fake ctypes.windll whose shcore.GetDpiForMonitor behaves as told.

    GetDpiForMonitor(hmonitor, dpiType, dpiX_ptr, dpiY_ptr) -> HRESULT.
    The real function passes ctypes.byref(dpi_x); we reach the underlying
    UINT object via the byref pointer's ._obj and set its .value, mirroring
    the out-parameter contract GetDpiForMonitor honours on the real API.
    """

    class FakeShcore:
        @staticmethod
        def GetDpiForMonitor(hmonitor, dpi_type, dpi_x_ref, dpi_y_ref):
            if raise_exc is not None:
                raise raise_exc
            if write_dpi is not None:
                dpi_x_ref._obj.value = write_dpi
                dpi_y_ref._obj.value = write_dpi
            return hresult

    class FakeWinDll:
        shcore = FakeShcore()

    return FakeWinDll()


def test_happy_path_returns_real_dpi():
    """S_OK with a 144 DPI write -> resolver returns 144.0 (150% scaling)."""
    fake = _make_windll(write_dpi=144, hresult=0)
    with patch.object(ctypes, "windll", fake, create=True):
        result = _win32_dpi_resolver(12345)
    assert result == 144.0


def test_nonzero_hresult_falls_back_to_96():
    """E_INVALIDARG (a non-zero HRESULT) -> resolver returns 96.0."""
    # Write a bogus DPI too, to prove the HRESULT gate is what rejects it.
    fake = _make_windll(write_dpi=144, hresult=0x80070057)
    with patch.object(ctypes, "windll", fake, create=True):
        result = _win32_dpi_resolver(12345)
    assert result == 96.0


def test_degenerate_zero_dpi_falls_back_to_96():
    """HRESULT S_OK but a degenerate 0 DPI value -> resolver returns 96.0."""
    fake = _make_windll(write_dpi=0, hresult=0)
    with patch.object(ctypes, "windll", fake, create=True):
        result = _win32_dpi_resolver(12345)
    assert result == 96.0


def test_getdpiformonitor_raises_falls_back_to_96():
    """GetDpiForMonitor raising (shcore unavailable / bad handle) -> 96.0.

    The resolver must swallow the exception and return 96.0, never propagate.
    """
    fake = _make_windll(raise_exc=OSError("shcore boom"))
    with patch.object(ctypes, "windll", fake, create=True):
        result = _win32_dpi_resolver(0)
    assert result == 96.0
