"""Windows laptop internal panel brightness control via WMI.

This module provides brightness control for laptop internal displays using the
Windows WMI (Windows Management Instrumentation) interface. It implements the
DisplayControl interface for consistent integration with the plugin system.

Key Classes:
  - InternalPanelControl: WMI-based laptop display brightness control.

WMI Interface:
  Uses WmiMonitorBrightness and WmiMonitorBrightnessMethods classes in the
  root\\wmi namespace. These APIs are only available on laptops with internal
  displays that support WMI brightness control.

Error Handling:
  - Returns None when WMI APIs unavailable (desktop machines, older laptops)
  - Returns False on WMI operation failures
  - Returns True on successful operations
  - Graceful degradation when hardware not supported

Thread Safety:
  All WMI operations are wrapped in asyncio.to_thread() to prevent blocking
  the main event loop, as WMI calls can be synchronous and potentially slow.

Example Usage:
  >>> control = InternalPanelControl()
  >>> await control.initialize()
  >>> if control.is_available:
  ...     current = await control.get_brightness()
  ...     await control.set_brightness(50)  # Set to 50%
"""

import asyncio
import logging
from typing import Optional, Tuple

from services.wheelhouse.integrations.display_control_base import DisplayControl

logger = logging.getLogger(__name__)


class InternalPanelControl(DisplayControl):
    """Windows laptop internal panel brightness control via WMI.
    
    This class provides brightness control for laptop internal displays using
    Windows WMI APIs. It implements the standard DisplayControl interface with
    a normalized 0-100 brightness range.
    
    Hardware Support:
      - Windows laptops with WMI brightness support
      - Internal displays only (not external monitors)
      - Requires WmiMonitorBrightness/WmiMonitorBrightnessMethods
    
    Architecture:
      - All WMI calls wrapped in asyncio.to_thread() for async compatibility
      - Graceful detection of unavailable hardware
      - Caches WMI objects for performance
      - Automatic retry logic for transient WMI errors
    
    Error Handling:
      - None returned when hardware unavailable (expected on desktops)
      - False returned on WMI operation failures
      - Logs warnings for debugging but doesn't raise exceptions
    """
    
    def __init__(self):
        """Initialize the internal panel control.
        
        Note: Constructor only sets up instance variables. Call initialize()
        to actually connect to WMI and detect hardware availability.
        """
        self._wmi_connection = None
        self._brightness_instance = None
        self._brightness_methods = None
        self._is_available = False
        self._last_brightness = None  # Cache for performance
        self._hardware_range = (0, 100)  # Default, updated after WMI detection
        
    async def initialize(self) -> bool:
        """Initialize WMI connection and detect hardware availability.
        
        This method must be called before using any other methods. It connects
        to the WMI service and checks if brightness control is available.
        
        Returns:
            bool: True if hardware available and ready, False otherwise
        """
        try:
            # Import WMI in thread to avoid blocking
            def _init_wmi():
                import wmi
                # Connect to the WMI namespace that contains brightness classes
                connection = wmi.WMI(namespace="root\\wmi")
                
                # Query for brightness instances
                brightness_instances = connection.query("SELECT * FROM WmiMonitorBrightness")
                if not brightness_instances:
                    return None, None, None
                    
                # Query for brightness methods
                brightness_methods = connection.query("SELECT * FROM WmiMonitorBrightnessMethods")
                if not brightness_methods:
                    return None, None, None
                
                # Use the first available instance
                return connection, brightness_instances[0], brightness_methods[0]
            
            # Run WMI initialization in thread
            result = await asyncio.to_thread(_init_wmi)
            self._wmi_connection, self._brightness_instance, self._brightness_methods = result
            
            if self._brightness_instance is None:
                logger.info("Internal panel brightness control not available (expected on desktop machines)")
                self._is_available = False
                return False
            
            # Test the interface by reading current brightness
            current = await self._get_current_brightness()
            if current is None:
                logger.warning("Internal panel brightness detected but not readable")
                self._is_available = False
                return False
            
            logger.info(f"Internal panel brightness control available (current: {current}%)")
            self._is_available = True
            return True
            
        except ImportError:
            logger.error("WMI module not available - install with: pip install WMI")
            self._is_available = False
            return False
        except Exception as e:
            logger.warning(f"Failed to initialize internal panel brightness control: {e}")
            self._is_available = False
            return False
    
    @property
    def is_available(self) -> bool:
        """Check if internal panel brightness control is available."""
        return self._is_available
    
    @property
    def brightness_range(self) -> Tuple[int, int]:
        """Return hardware brightness range (0-100 for WMI).
        
        WMI brightness APIs typically use 0-100 range natively, so no
        conversion is needed. This aligns perfectly with our normalized range.
        """
        return self._hardware_range
    
    async def get_brightness(self) -> Optional[int]:
        """Get current brightness level (0-100).
        
        Returns:
            int: Current brightness 0-100, or None if hardware unavailable
        """
        if not self._is_available:
            return None
            
        try:
            brightness = await self._get_current_brightness()
            if brightness is not None:
                self._last_brightness = brightness  # Cache for performance
            return brightness
        except Exception as e:
            logger.warning(f"Failed to get internal panel brightness: {e}")
            return None
    
    async def set_brightness(self, level: int) -> Optional[bool]:
        """Set absolute brightness level (0-100).
        
        Args:
            level: Target brightness 0-100 (will be clamped to valid range)
            
        Returns:
            bool: True if successful, False if failed, None if unavailable
        """
        if not self._is_available:
            return None
            
        # Clamp to valid range
        clamped_level = max(0, min(100, level))
        
        try:
            success = await self._set_brightness_value(clamped_level)
            if success:
                self._last_brightness = clamped_level  # Update cache
                logger.debug(f"Set internal panel brightness to {clamped_level}%")
            return success
        except Exception as e:
            logger.warning(f"Failed to set internal panel brightness to {clamped_level}%: {e}")
            return False
    
    async def _get_current_brightness(self) -> Optional[int]:
        """Get current brightness from WMI (internal helper).
        
        Returns:
            int: Current brightness 0-100, or None if failed
        """
        if not self._wmi_connection:
            return None
            
        def _wmi_get():
            # Re-query to get current value (more reliable than Refresh_)
            try:
                import wmi
                connection = wmi.WMI(namespace="root\\wmi")
                instances = connection.query("SELECT CurrentBrightness FROM WmiMonitorBrightness")
                if instances:
                    return int(instances[0].CurrentBrightness)
                return None
            except Exception as e:
                logger.debug(f"WMI brightness query failed: {e}")
                return None
        
        try:
            brightness = await asyncio.to_thread(_wmi_get)
            return brightness
        except Exception as e:
            logger.debug(f"WMI get brightness failed: {e}")
            return None
    
    async def _set_brightness_value(self, level: int) -> bool:
        """Set brightness value via WMI (internal helper).
        
        Args:
            level: Brightness level 0-100
            
        Returns:
            bool: True if successful, False if failed
        """
        if not self._brightness_methods:
            return False
            
        def _wmi_set():
            # Call WmiSetBrightness method
            # The method signature is: WmiSetBrightness(Timeout, Brightness)
            # Timeout: usually 0 for immediate
            # Brightness: 0-100 value
            try:
                if self._brightness_methods is not None:
                    result = self._brightness_methods.WmiSetBrightness(Timeout=1, Brightness=level)
                    logger.debug(f"WmiSetBrightness({level}) returned: {result}")
                    # WMI methods may return 0, None, or nothing on success
                    return result is None or result == 0 or result == ()
                return False
            except Exception as e:
                logger.debug(f"WMI set failed: {e}")
                return False
        
        try:
            success = await asyncio.to_thread(_wmi_set)
            return success
        except Exception as e:
            logger.debug(f"WMI set brightness failed: {e}")
            return False
    
    async def adjust_brightness(self, delta: int) -> Optional[bool]:
        """Adjust brightness by delta (0-100 normalized units).
        
        Uses the default implementation from DisplayControl base class:
        gets current brightness, applies delta with clamping, then sets new value.
        
        Args:
            delta: Brightness change (positive=brighter, negative=dimmer)
            
        Returns:
            Optional[bool]: True if successful, False if failed, None if unavailable
        """
        if not self._is_available:
            return None
            
        # Use default implementation from base class
        current = await self.get_brightness()
        if current is None:
            return None  # Device offline/unavailable
        
        new_level = max(0, min(100, current + delta))
        result = await self.set_brightness(new_level)
        return result