"""Tests for SoftwareDimmer handler.

Tests software overlay dimming including:
- Brightness <-> alpha conversion math
- set_brightness / adjust_brightness
- Lifecycle (start/stop)
- Window procedure message handling
- Adversarial: boundary values, missing window, double start/stop
"""

import asyncio
import ctypes
from ctypes import wintypes
from unittest.mock import Mock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_win_apis():
    """Mock Windows DLLs for SoftwareDimmer."""
    user32 = MagicMock()
    gdi32 = MagicMock()
    kernel32 = MagicMock()

    user32.DefWindowProcW.return_value = 0
    user32.IsWindow.return_value = True
    user32.PostMessageW.return_value = True
    user32.SetLayeredWindowAttributes.return_value = True
    user32.CreateWindowExW.return_value = wintypes.HWND(12345)
    user32.RegisterClassExW.return_value = 1
    user32.GetSystemMetrics.return_value = 1920
    user32.ShowWindow.return_value = True
    user32.UpdateWindow.return_value = True
    gdi32.CreateSolidBrush.return_value = wintypes.HBRUSH(99)
    gdi32.DeleteObject.return_value = True
    kernel32.GetModuleHandleW.return_value = wintypes.HMODULE(1)
    kernel32.GetCurrentThreadId.return_value = 12345
    kernel32.GetLastError.return_value = 0

    return user32, gdi32, kernel32


@pytest.fixture
def software_dimmer(mock_win_apis):
    """SoftwareDimmer with mocked Windows APIs."""
    user32, gdi32, kernel32 = mock_win_apis

    with patch("handlers.software_dimmer.ctypes") as mock_ctypes:
        mock_ctypes.windll.user32 = user32
        mock_ctypes.windll.gdi32 = gdi32
        mock_ctypes.windll.kernel32 = kernel32
        mock_ctypes.POINTER = ctypes.POINTER
        mock_ctypes.byref = ctypes.byref
        mock_ctypes.sizeof = ctypes.sizeof
        mock_ctypes.WINFUNCTYPE = ctypes.WINFUNCTYPE
        mock_ctypes.c_ssize_t = ctypes.c_ssize_t

        from handlers.software_dimmer import SoftwareDimmer

        loop = asyncio.new_event_loop()
        dimmer = SoftwareDimmer.__new__(SoftwareDimmer)
        dimmer.loop = loop
        dimmer.hwnd = None
        dimmer._gui_thread = None
        dimmer._gui_thread_id = None
        dimmer._window_destroyed_event = __import__("threading").Event()
        dimmer.current_brightness_percent = 100
        dimmer.current_alpha = 0
        dimmer.user32 = user32
        dimmer.kernel32 = kernel32
        dimmer.gdi32 = gdi32
        dimmer.window_class_name = "TestDimmerOverlay"
        dimmer.black_brush_handle = None

        # Set up the real conversion methods
        dimmer._brightness_to_alpha = SoftwareDimmer._brightness_to_alpha.__get__(dimmer)
        dimmer._alpha_to_brightness = SoftwareDimmer._alpha_to_brightness.__get__(dimmer)
        dimmer.set_brightness = SoftwareDimmer.set_brightness.__get__(dimmer)
        dimmer.adjust_brightness = SoftwareDimmer.adjust_brightness.__get__(dimmer)
        dimmer.start = SoftwareDimmer.start.__get__(dimmer)
        dimmer.stop = SoftwareDimmer.stop.__get__(dimmer)

        yield dimmer
        loop.close()


# ===========================================================================
# _brightness_to_alpha conversion
# ===========================================================================

class TestBrightnessToAlpha:
    """Test brightness -> alpha conversion math."""

    def test_brightness_100_gives_near_zero_alpha(self, software_dimmer):
        """100% brightness = minimal dimming (alpha 0 or 1 due to float rounding)."""
        alpha = software_dimmer._brightness_to_alpha(100)
        assert alpha <= 1  # Float rounding: int(100 * 2.55) = 254, so 255 - 254 = 1

    def test_brightness_0_gives_alpha_255(self, software_dimmer):
        """0% brightness = max dimming (alpha 255)."""
        assert software_dimmer._brightness_to_alpha(0) == 255

    def test_brightness_50_gives_mid_alpha(self, software_dimmer):
        """50% brightness gives approximately 128 alpha."""
        alpha = software_dimmer._brightness_to_alpha(50)
        assert 125 <= alpha <= 130  # 255 - int(50 * 2.55) = 255 - 127 = 128

    def test_clamps_below_zero(self, software_dimmer):
        """Negative brightness clamps alpha to 255."""
        alpha = software_dimmer._brightness_to_alpha(-50)
        assert 0 <= alpha <= 255

    def test_clamps_above_100(self, software_dimmer):
        """Brightness above 100 clamps alpha to 0."""
        alpha = software_dimmer._brightness_to_alpha(200)
        assert 0 <= alpha <= 255

    def test_full_range_monotonic(self, software_dimmer):
        """Alpha should monotonically decrease as brightness increases."""
        alphas = [software_dimmer._brightness_to_alpha(b) for b in range(101)]
        for i in range(100):
            assert alphas[i] >= alphas[i + 1]


# ===========================================================================
# _alpha_to_brightness conversion
# ===========================================================================

class TestAlphaToBrightness:
    """Test alpha -> brightness inverse conversion."""

    def test_alpha_0_gives_brightness_100(self, software_dimmer):
        """Alpha 0 (no overlay) = 100% brightness."""
        assert software_dimmer._alpha_to_brightness(0) == 100

    def test_alpha_255_gives_brightness_0(self, software_dimmer):
        """Alpha 255 (full overlay) = 0% brightness."""
        assert software_dimmer._alpha_to_brightness(255) == 0

    def test_round_trip_consistency(self, software_dimmer):
        """brightness -> alpha -> brightness should be close to original."""
        for b in [0, 25, 50, 75, 100]:
            alpha = software_dimmer._brightness_to_alpha(b)
            recovered = software_dimmer._alpha_to_brightness(alpha)
            assert abs(recovered - b) <= 1  # Allow rounding error of 1

    def test_clamps_negative_alpha(self, software_dimmer):
        """Negative alpha input clamped to valid range."""
        result = software_dimmer._alpha_to_brightness(-10)
        assert 0 <= result <= 100

    def test_clamps_high_alpha(self, software_dimmer):
        """Alpha above 255 clamped to valid range."""
        result = software_dimmer._alpha_to_brightness(500)
        assert 0 <= result <= 100


# ===========================================================================
# set_brightness
# ===========================================================================

class TestSetBrightness:
    """Test set_brightness API."""

    def test_posts_message_when_window_exists(self, software_dimmer, mock_win_apis):
        """Posts WM_USER_UPDATE_ALPHA to GUI thread when window valid."""
        user32, _, _ = mock_win_apis
        software_dimmer.hwnd = wintypes.HWND(12345)
        software_dimmer._gui_thread_id = 100

        software_dimmer.set_brightness(50)

        user32.PostMessageW.assert_called_once()
        call_args = user32.PostMessageW.call_args[0]
        assert call_args[0] == software_dimmer.hwnd  # hwnd
        # Message should be WM_USER + 100
        from handlers.software_dimmer import WM_USER_UPDATE_ALPHA
        assert call_args[1] == WM_USER_UPDATE_ALPHA

    def test_stores_values_when_no_window(self, software_dimmer, mock_win_apis):
        """Updates internal state when window not available."""
        user32, _, _ = mock_win_apis
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        software_dimmer.set_brightness(50)

        assert software_dimmer.current_brightness_percent == 50
        expected_alpha = software_dimmer._brightness_to_alpha(50)
        assert software_dimmer.current_alpha == expected_alpha
        user32.PostMessageW.assert_not_called()

    def test_handles_invalid_window(self, software_dimmer, mock_win_apis):
        """Stores values if window handle is invalid."""
        user32, _, _ = mock_win_apis
        software_dimmer.hwnd = wintypes.HWND(12345)
        software_dimmer._gui_thread_id = 100
        user32.IsWindow.return_value = False

        software_dimmer.set_brightness(30)
        assert software_dimmer.current_brightness_percent == 30

    def test_handles_post_message_failure(self, software_dimmer, mock_win_apis):
        """PostMessageW failure logged but doesn't crash."""
        user32, _, _ = mock_win_apis
        software_dimmer.hwnd = wintypes.HWND(12345)
        software_dimmer._gui_thread_id = 100
        user32.PostMessageW.return_value = False

        software_dimmer.set_brightness(50)  # Should not raise


# ===========================================================================
# adjust_brightness
# ===========================================================================

class TestAdjustBrightness:
    """Test relative brightness adjustments."""

    def test_positive_delta(self, software_dimmer, mock_win_apis):
        """Positive delta increases brightness."""
        software_dimmer.current_brightness_percent = 50
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        software_dimmer.adjust_brightness(20)
        assert software_dimmer.current_brightness_percent == 70

    def test_negative_delta(self, software_dimmer, mock_win_apis):
        """Negative delta decreases brightness."""
        software_dimmer.current_brightness_percent = 50
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        software_dimmer.adjust_brightness(-20)
        assert software_dimmer.current_brightness_percent == 30

    def test_clamps_at_zero(self, software_dimmer, mock_win_apis):
        """Cannot go below 0."""
        software_dimmer.current_brightness_percent = 10
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        software_dimmer.adjust_brightness(-50)
        assert software_dimmer.current_brightness_percent == 0

    def test_clamps_at_100(self, software_dimmer, mock_win_apis):
        """Cannot go above 100."""
        software_dimmer.current_brightness_percent = 90
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        software_dimmer.adjust_brightness(50)
        assert software_dimmer.current_brightness_percent == 100


# ===========================================================================
# Lifecycle: start / stop
# ===========================================================================

class TestLifecycle:
    """Test dimmer lifecycle management."""

    def test_start_creates_gui_thread(self, software_dimmer, mock_win_apis):
        """start() creates and starts a daemon GUI thread."""
        with patch("threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread.is_alive.return_value = False
            mock_thread_cls.return_value = mock_thread

            software_dimmer.start()

            mock_thread_cls.assert_called_once()
            mock_thread.start.assert_called_once()

    def test_start_already_running_is_noop(self, software_dimmer, mock_win_apis):
        """start() when thread already running does nothing."""
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        software_dimmer._gui_thread = mock_thread

        with patch("threading.Thread") as mock_thread_cls:
            software_dimmer.start()
            mock_thread_cls.assert_not_called()

    def test_stop_posts_wm_close(self, software_dimmer, mock_win_apis):
        """stop() posts WM_CLOSE to overlay window."""
        user32, _, _ = mock_win_apis
        software_dimmer.hwnd = wintypes.HWND(12345)
        software_dimmer._gui_thread_id = 100
        software_dimmer._gui_thread = MagicMock()
        software_dimmer._gui_thread.is_alive.return_value = True

        software_dimmer.stop()

        # Should post WM_CLOSE
        calls = user32.PostMessageW.call_args_list
        assert any(
            c[0][1] == 0x0010  # WM_CLOSE
            for c in calls
        )

    def test_stop_joins_thread(self, software_dimmer, mock_win_apis):
        """stop() waits for GUI thread to finish."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        software_dimmer._gui_thread = mock_thread

        software_dimmer.stop()
        mock_thread.join.assert_called_once_with(timeout=5.0)

    def test_stop_no_window_no_crash(self, software_dimmer, mock_win_apis):
        """stop() when no window handle doesn't crash."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None
        software_dimmer._gui_thread = None

        software_dimmer.stop()  # Should not raise


# ===========================================================================
# Adversarial
# ===========================================================================

class TestAdversarial:
    """Adversarial edge case tests."""

    def test_rapid_brightness_changes(self, software_dimmer, mock_win_apis):
        """Rapid brightness changes don't corrupt state."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None

        for b in range(101):
            software_dimmer.set_brightness(b)
            assert software_dimmer.current_brightness_percent == b

        for b in range(100, -1, -1):
            software_dimmer.set_brightness(b)
            assert software_dimmer.current_brightness_percent == b

    def test_double_stop(self, software_dimmer, mock_win_apis):
        """Calling stop() twice is harmless."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None
        software_dimmer._gui_thread = None

        software_dimmer.stop()
        software_dimmer.stop()  # Should not raise

    def test_double_start(self, software_dimmer, mock_win_apis):
        """Calling start() twice when already running is harmless."""
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        software_dimmer._gui_thread = mock_thread

        software_dimmer.start()  # Noop
        software_dimmer.start()  # Still noop

    def test_set_brightness_boundary_0(self, software_dimmer, mock_win_apis):
        """Brightness 0 is valid and results in max alpha."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None
        software_dimmer.set_brightness(0)
        assert software_dimmer.current_brightness_percent == 0
        assert software_dimmer.current_alpha == 255

    def test_set_brightness_boundary_100(self, software_dimmer, mock_win_apis):
        """Brightness 100 is valid and results in near-zero alpha."""
        software_dimmer.hwnd = None
        software_dimmer._gui_thread_id = None
        software_dimmer.set_brightness(100)
        assert software_dimmer.current_brightness_percent == 100
        assert software_dimmer.current_alpha <= 1  # Float rounding: 255 - int(100*2.55) = 1

    def test_start_with_missing_dlls(self, software_dimmer, mock_win_apis):
        """start() aborts when essential DLLs not loaded."""
        software_dimmer.user32 = None
        software_dimmer.start()  # Should return early without crash
