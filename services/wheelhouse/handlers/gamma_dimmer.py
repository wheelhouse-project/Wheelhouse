"""Native Gamma Ramp display dimming using Windows GDI32 API.

This module implements hardware-level display dimming by manipulating the GPU's
Look-Up Table (LUT) via SetDeviceGammaRamp. Unlike SoftwareDimmer's overlay
approach, this dims the entire display output including the mouse cursor.

Key Classes:
  - GammaDimmer: Main dimming controller using gamma ramp manipulation.

Key Features:
  - Dims mouse cursor along with screen content
  - Multi-monitor support via per-DC gamma control
  - Saves original gamma ramp for graceful restoration
  - Thread-safe brightness adjustment
  - Automatic cleanup on shutdown (atexit handler)

Technical Implementation:
  - GetDeviceGammaRamp: Saves original 256-entry LUT per channel
  - SetDeviceGammaRamp: Applies scaled LUT for brightness control
  - CreateDC("DISPLAY"): Gets device context for all monitors

Typical Usage:
  from handlers.gamma_dimmer import GammaDimmer

  dimmer = GammaDimmer(loop)
  dimmer.start()

  # Set brightness level (0 = max dim, 100 = no dim)
  dimmer.set_brightness(50)  # 50% brightness

  # Cleanup (restores original gamma)
  dimmer.stop()
"""
import ctypes
from ctypes import wintypes
import logging
import asyncio
import atexit
from typing import Optional, Any

logger = logging.getLogger(__name__)

# Gamma ramp structure: 256 WORD values per channel (R, G, B)
# Total size: 256 * 2 bytes * 3 channels = 1536 bytes
GAMMA_RAMP = (wintypes.WORD * 256) * 3


class GammaDimmer:
    """Native gamma ramp dimmer using SetDeviceGammaRamp.

    This dimmer manipulates the GPU's Look-Up Table to dim the display,
    which affects all output including the mouse cursor. It matches the
    SoftwareDimmer API for drop-in compatibility with BrightnessCoordinator.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, initial_brightness: int = 100):
        """Initialize gamma dimmer.

        Args:
            loop: Event loop for async operations
            initial_brightness: Initial brightness 0-100 (100 = no dimming)
        """
        self.loop = loop
        self.current_brightness_percent = initial_brightness

        # Windows API handles
        self.gdi32 = ctypes.windll.gdi32
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

        self._setup_ctypes_prototypes()

        # Device context for display
        self._hdc: Optional[wintypes.HDC] = None

        # Original gamma ramp for restoration
        self._original_ramp: Optional[GAMMA_RAMP] = None
        self._is_started = False

        # Register cleanup handler for unexpected exit
        atexit.register(self._atexit_cleanup)

        logger.info(
            f"GammaDimmer initialized. Initial brightness: {initial_brightness}%"
        )

    def _setup_ctypes_prototypes(self) -> None:
        """Configure ctypes function prototypes for Windows API calls."""
        # GetDeviceGammaRamp
        self.gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.POINTER(GAMMA_RAMP)]
        self.gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL

        # SetDeviceGammaRamp
        self.gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.POINTER(GAMMA_RAMP)]
        self.gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

        # CreateDC
        self.gdi32.CreateDCW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p
        ]
        self.gdi32.CreateDCW.restype = wintypes.HDC

        # DeleteDC
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.DeleteDC.restype = wintypes.BOOL

    def start(self) -> None:
        """Start gamma dimmer and save original gamma ramp.

        Creates device context for display and saves original gamma ramp
        for later restoration.
        """
        if self._is_started:
            logger.warning("GammaDimmer already started")
            return

        # Get device context for primary display
        # "DISPLAY" targets all monitors as a virtual display
        self._hdc = self.gdi32.CreateDCW("DISPLAY", None, None, None)
        if not self._hdc:
            logger.error("GammaDimmer: Failed to create display DC")
            return

        # Save original gamma ramp
        self._original_ramp = GAMMA_RAMP()
        if not self.gdi32.GetDeviceGammaRamp(self._hdc, ctypes.byref(self._original_ramp)):
            logger.error("GammaDimmer: Failed to get original gamma ramp")
            self.gdi32.DeleteDC(self._hdc)
            self._hdc = None
            return

        self._is_started = True
        logger.info(f"GammaDimmer started (HDC: {self._hdc})")

        # Apply initial brightness if not 100%
        if self.current_brightness_percent != 100:
            self.set_brightness(self.current_brightness_percent)

    def stop(self) -> None:
        """Stop gamma dimmer and restore original gamma ramp."""
        if not self._is_started:
            logger.debug("GammaDimmer not started, nothing to stop")
            return

        logger.info("GammaDimmer stopping, restoring original gamma...")

        # Restore original gamma ramp
        if self._hdc and self._original_ramp:
            if not self.gdi32.SetDeviceGammaRamp(self._hdc, ctypes.byref(self._original_ramp)):
                logger.error("GammaDimmer: Failed to restore original gamma ramp")
            else:
                logger.info("GammaDimmer: Original gamma ramp restored")

        # Release device context
        if self._hdc:
            self.gdi32.DeleteDC(self._hdc)
            self._hdc = None

        self._original_ramp = None
        self._is_started = False
        logger.info("GammaDimmer stopped")

    def _atexit_cleanup(self) -> None:
        """Cleanup handler for unexpected process exit."""
        if self._is_started:
            logger.warning("GammaDimmer: atexit cleanup triggered")
            self.stop()

    def set_brightness(self, brightness_percent: int) -> bool:
        """Set display brightness via gamma ramp scaling.

        Args:
            brightness_percent: Brightness level 0-100 (0 = max dim, 100 = no dim)

        Returns:
            True if brightness was applied (or stored when not started),
            False if the SetDeviceGammaRamp API call failed.
        """
        brightness_percent = max(0, min(100, brightness_percent))
        self.current_brightness_percent = brightness_percent

        if not self._is_started or not self._hdc or not self._original_ramp:
            logger.warning(
                f"GammaDimmer: set_brightness({brightness_percent}%) called but not started"
            )
            return True

        # Calculate scaling factor (0.0 to 1.0)
        scale = brightness_percent / 100.0

        # Create new gamma ramp by scaling original values
        new_ramp = GAMMA_RAMP()
        for channel in range(3):  # R, G, B
            for i in range(256):
                # Scale original value and clamp to valid range
                original = self._original_ramp[channel][i]
                scaled = int(original * scale)
                new_ramp[channel][i] = min(65535, max(0, scaled))

        # Apply new gamma ramp
        if not self.gdi32.SetDeviceGammaRamp(self._hdc, ctypes.byref(new_ramp)):
            error_code = self.kernel32.GetLastError()
            logger.warning(
                f"GammaDimmer: Failed to set gamma ramp at {brightness_percent}% "
                f"(error={error_code})"
            )
            return False

        logger.debug(f"GammaDimmer: Brightness set to {brightness_percent}%")
        return True

    def adjust_brightness(self, delta_percent: int) -> None:
        """Adjust brightness by relative delta.

        Args:
            delta_percent: Change in brightness (-100 to +100)
        """
        new_brightness = max(0, min(100, self.current_brightness_percent + delta_percent))
        logger.info(
            f"GammaDimmer: adjust_brightness delta={delta_percent}%, "
            f"{self.current_brightness_percent}% -> {new_brightness}%"
        )
        self.set_brightness(new_brightness)
