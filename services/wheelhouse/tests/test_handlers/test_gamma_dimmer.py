"""Tests for GammaDimmer handler.

Tests gamma ramp brightness control including:
- Brightness scaling math
- Lifecycle (start/stop/atexit)
- Guard checks (not started, already started)
- Adversarial: boundary values, double start/stop
"""

import asyncio
import ctypes
from ctypes import wintypes
from unittest.mock import Mock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixture: GammaDimmer with mocked Windows APIs
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gdi32():
    gdi32 = MagicMock()
    gdi32.CreateDCW.return_value = wintypes.HDC(42)
    gdi32.GetDeviceGammaRamp.return_value = True
    gdi32.SetDeviceGammaRamp.return_value = True
    gdi32.DeleteDC.return_value = True
    return gdi32


@pytest.fixture
def mock_user32():
    return MagicMock()


@pytest.fixture
def mock_kernel32():
    kernel32 = MagicMock()
    kernel32.GetLastError.return_value = 0
    return kernel32


@pytest.fixture
def gamma_dimmer(mock_gdi32, mock_user32, mock_kernel32):
    """Create GammaDimmer with mocked Windows DLLs."""
    with patch("handlers.gamma_dimmer.ctypes") as mock_ctypes:
        mock_ctypes.windll.gdi32 = mock_gdi32
        mock_ctypes.windll.user32 = mock_user32
        mock_ctypes.windll.kernel32 = mock_kernel32
        mock_ctypes.POINTER = ctypes.POINTER
        mock_ctypes.byref = ctypes.byref
        mock_ctypes.sizeof = ctypes.sizeof

        from handlers.gamma_dimmer import GammaDimmer

        loop = asyncio.new_event_loop()
        dimmer = GammaDimmer.__new__(GammaDimmer)
        dimmer.loop = loop
        dimmer.current_brightness_percent = 100
        dimmer.gdi32 = mock_gdi32
        dimmer.user32 = mock_user32
        dimmer.kernel32 = mock_kernel32
        dimmer._hdc = None
        dimmer._original_ramp = None
        dimmer._is_started = False

        yield dimmer
        loop.close()


# ===========================================================================
# Brightness scaling math
# ===========================================================================

class TestBrightnessScaling:
    """Test the gamma ramp brightness calculation in set_brightness."""

    def test_set_brightness_clamps_to_0(self, gamma_dimmer, mock_gdi32):
        """Brightness below 0 clamps to 0."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.set_brightness(-10)
        assert gamma_dimmer.current_brightness_percent == 0

    def test_set_brightness_clamps_to_100(self, gamma_dimmer, mock_gdi32):
        """Brightness above 100 clamps to 100."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.set_brightness(200)
        assert gamma_dimmer.current_brightness_percent == 100

    def test_set_brightness_50_scales_ramp(self, gamma_dimmer, mock_gdi32):
        """50% brightness should scale original values by 0.5."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = 60000  # constant for easy verification
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.set_brightness(50)
        assert gamma_dimmer.current_brightness_percent == 50
        assert mock_gdi32.SetDeviceGammaRamp.called

    def test_set_brightness_0_zeros_ramp(self, gamma_dimmer, mock_gdi32):
        """0% brightness should zero out the ramp."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = 65535
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.set_brightness(0)
        assert gamma_dimmer.current_brightness_percent == 0
        assert mock_gdi32.SetDeviceGammaRamp.called

    def test_set_brightness_100_preserves_original(self, gamma_dimmer, mock_gdi32):
        """100% brightness should result in values matching original."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = 65535
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.set_brightness(100)
        assert gamma_dimmer.current_brightness_percent == 100


# ===========================================================================
# adjust_brightness
# ===========================================================================

class TestAdjustBrightness:
    """Test relative brightness adjustments."""

    def test_adjust_positive_delta(self, gamma_dimmer, mock_gdi32):
        """Positive delta increases brightness."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)
        gamma_dimmer.current_brightness_percent = 50

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.adjust_brightness(10)
        assert gamma_dimmer.current_brightness_percent == 60

    def test_adjust_negative_delta(self, gamma_dimmer, mock_gdi32):
        """Negative delta decreases brightness."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)
        gamma_dimmer.current_brightness_percent = 50

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.adjust_brightness(-10)
        assert gamma_dimmer.current_brightness_percent == 40

    def test_adjust_clamps_at_zero(self, gamma_dimmer, mock_gdi32):
        """Cannot go below 0."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)
        gamma_dimmer.current_brightness_percent = 10

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.adjust_brightness(-50)
        assert gamma_dimmer.current_brightness_percent == 0

    def test_adjust_clamps_at_100(self, gamma_dimmer, mock_gdi32):
        """Cannot go above 100."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)
        gamma_dimmer.current_brightness_percent = 90

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        gamma_dimmer.adjust_brightness(50)
        assert gamma_dimmer.current_brightness_percent == 100


# ===========================================================================
# Lifecycle: start / stop
# ===========================================================================

class TestLifecycle:
    """Test start/stop lifecycle and guard checks."""

    def test_start_creates_dc_and_saves_ramp(self, gamma_dimmer, mock_gdi32):
        """start() should create a display DC and save original ramp."""
        gamma_dimmer.start()

        assert gamma_dimmer._is_started is True
        assert gamma_dimmer._hdc is not None
        mock_gdi32.CreateDCW.assert_called_once_with("DISPLAY", None, None, None)
        assert mock_gdi32.GetDeviceGammaRamp.called

    def test_start_already_started_is_noop(self, gamma_dimmer, mock_gdi32):
        """Calling start() when already started does nothing."""
        gamma_dimmer._is_started = True

        gamma_dimmer.start()
        mock_gdi32.CreateDCW.assert_not_called()

    def test_start_fails_if_create_dc_returns_null(self, gamma_dimmer, mock_gdi32):
        """start() fails gracefully when CreateDCW returns null."""
        mock_gdi32.CreateDCW.return_value = wintypes.HDC(0)

        gamma_dimmer.start()
        assert gamma_dimmer._is_started is False

    def test_start_fails_if_get_gamma_ramp_fails(self, gamma_dimmer, mock_gdi32):
        """start() cleans up if GetDeviceGammaRamp fails."""
        mock_gdi32.GetDeviceGammaRamp.return_value = False

        gamma_dimmer.start()
        assert gamma_dimmer._is_started is False
        assert gamma_dimmer._hdc is None
        mock_gdi32.DeleteDC.assert_called_once()

    def test_stop_restores_original_ramp(self, gamma_dimmer, mock_gdi32):
        """stop() should restore original gamma ramp and release DC."""
        # Simulate started state
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        gamma_dimmer._original_ramp = GAMMA_RAMP()

        gamma_dimmer.stop()

        assert mock_gdi32.SetDeviceGammaRamp.called
        mock_gdi32.DeleteDC.assert_called_once()
        assert gamma_dimmer._is_started is False
        assert gamma_dimmer._hdc is None
        assert gamma_dimmer._original_ramp is None

    def test_stop_not_started_is_noop(self, gamma_dimmer, mock_gdi32):
        """stop() when not started does nothing."""
        gamma_dimmer.stop()
        mock_gdi32.SetDeviceGammaRamp.assert_not_called()
        mock_gdi32.DeleteDC.assert_not_called()

    def test_stop_handles_restore_failure(self, gamma_dimmer, mock_gdi32):
        """stop() logs error but still cleans up if restore fails."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        gamma_dimmer._original_ramp = GAMMA_RAMP()
        mock_gdi32.SetDeviceGammaRamp.return_value = False

        gamma_dimmer.stop()
        # Should still clean up DC
        mock_gdi32.DeleteDC.assert_called_once()
        assert gamma_dimmer._is_started is False


# ===========================================================================
# Atexit cleanup
# ===========================================================================

class TestAtexitCleanup:
    """Test atexit handler for unexpected exit."""

    def test_atexit_calls_stop_when_started(self, gamma_dimmer, mock_gdi32):
        """atexit cleanup calls stop() when dimmer is running."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        gamma_dimmer._original_ramp = GAMMA_RAMP()

        gamma_dimmer._atexit_cleanup()
        assert gamma_dimmer._is_started is False

    def test_atexit_noop_when_not_started(self, gamma_dimmer, mock_gdi32):
        """atexit cleanup does nothing when dimmer not started."""
        gamma_dimmer._atexit_cleanup()
        mock_gdi32.SetDeviceGammaRamp.assert_not_called()


# ===========================================================================
# Guards: set_brightness without start
# ===========================================================================

class TestGuards:
    """Test guard conditions for set_brightness."""

    def test_set_brightness_without_start_stores_value(self, gamma_dimmer, mock_gdi32):
        """set_brightness when not started updates internal value but doesn't call API."""
        gamma_dimmer.set_brightness(50)
        assert gamma_dimmer.current_brightness_percent == 50
        mock_gdi32.SetDeviceGammaRamp.assert_not_called()

    def test_set_brightness_api_failure_logs(self, gamma_dimmer, mock_gdi32):
        """SetDeviceGammaRamp failure is logged but doesn't crash."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        mock_gdi32.SetDeviceGammaRamp.return_value = False
        gamma_dimmer.set_brightness(50)  # Should not raise


# ===========================================================================
# Adversarial: start with initial brightness != 100
# ===========================================================================

class TestAdversarial:
    """Adversarial tests for boundary and unusual conditions."""

    def test_start_with_initial_brightness_below_100(self, gamma_dimmer, mock_gdi32):
        """start() applies initial brightness if != 100."""
        gamma_dimmer.current_brightness_percent = 50
        gamma_dimmer.start()

        assert gamma_dimmer._is_started is True
        # SetDeviceGammaRamp should be called to apply initial brightness
        assert mock_gdi32.SetDeviceGammaRamp.called

    def test_rapid_brightness_cycles(self, gamma_dimmer, mock_gdi32):
        """Rapid brightness changes don't corrupt state."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

        for brightness in [100, 0, 50, 75, 25, 100, 0]:
            gamma_dimmer.set_brightness(brightness)
            assert gamma_dimmer.current_brightness_percent == brightness

    def test_double_stop(self, gamma_dimmer, mock_gdi32):
        """Calling stop() twice doesn't crash."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)

        from handlers.gamma_dimmer import GAMMA_RAMP
        gamma_dimmer._original_ramp = GAMMA_RAMP()

        gamma_dimmer.stop()
        gamma_dimmer.stop()  # Should be noop
        assert gamma_dimmer._is_started is False

    def test_double_start(self, gamma_dimmer, mock_gdi32):
        """Calling start() twice is harmless."""
        gamma_dimmer.start()
        assert gamma_dimmer._is_started is True

        gamma_dimmer.start()  # Second call - noop
        # CreateDCW should only be called once
        mock_gdi32.CreateDCW.assert_called_once()


# ===========================================================================
# Return value from set_brightness
# ===========================================================================

class TestSetBrightnessReturnValue:
    """set_brightness must return bool so callers can detect API failure."""

    def _make_started(self, gamma_dimmer, mock_gdi32):
        """Helper to set dimmer into started state with a linear ramp."""
        gamma_dimmer._is_started = True
        gamma_dimmer._hdc = wintypes.HDC(42)
        from handlers.gamma_dimmer import GAMMA_RAMP
        ramp = GAMMA_RAMP()
        for ch in range(3):
            for i in range(256):
                ramp[ch][i] = i * 256
        gamma_dimmer._original_ramp = ramp

    def test_returns_true_on_success(self, gamma_dimmer, mock_gdi32):
        """set_brightness returns True when SetDeviceGammaRamp succeeds."""
        self._make_started(gamma_dimmer, mock_gdi32)
        mock_gdi32.SetDeviceGammaRamp.return_value = True

        result = gamma_dimmer.set_brightness(50)
        assert result is True

    def test_returns_false_on_api_failure(self, gamma_dimmer, mock_gdi32):
        """set_brightness returns False when SetDeviceGammaRamp fails."""
        self._make_started(gamma_dimmer, mock_gdi32)
        mock_gdi32.SetDeviceGammaRamp.return_value = False

        result = gamma_dimmer.set_brightness(50)
        assert result is False

    def test_returns_true_when_not_started(self, gamma_dimmer, mock_gdi32):
        """set_brightness returns True when not started (value stored for later)."""
        result = gamma_dimmer.set_brightness(50)
        assert result is True
