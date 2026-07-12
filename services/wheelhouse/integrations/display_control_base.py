"""Abstract base class for display brightness control integrations.

This module defines the DisplayControl interface that provides a normalized
0-100 brightness range regardless of hardware-specific ranges. All display
control implementations (TVs, monitors, laptops) implement this interface.

DESIGN PRINCIPLES:
- Normalized range: All public methods use 0-100 brightness scale
- Async-first: All methods are async to support network/I2C operations
- Error contracts: None=offline, False=failed, True=success
- Thread safety: Single-threaded async context, no synchronization needed
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple


class DisplayControl(ABC):
    """Abstract base class for all display brightness control integrations.
    
    This interface provides a normalized 0-100 brightness range regardless of
    hardware-specific ranges. Implementations handle conversion between normalized
    values and their native hardware ranges.
    
    All methods are async to support network-based displays (TVs) and local displays
    (laptop backlights) with a consistent interface.
    
    Error Handling:
      - Methods return None on device offline/unreachable
      - Methods return False on operation failure (device online but command failed)
      - Methods should NOT raise exceptions for expected errors (offline, busy, etc.)
      - Log errors internally, return failure indicators
    
    Thread Safety:
      - All methods will be called from the main event loop
      - Blocking operations MUST use asyncio.to_thread()
      - No synchronization needed (single-threaded async context)
    
    Example Implementation:
        >>> class BraviaControl(DisplayControl):
        ...     @property
        ...     def brightness_range(self) -> Tuple[int, int]:
        ...         return (0, 50)  # Bravia hardware range
        ...     
        ...     async def get_brightness(self) -> Optional[int]:
        ...         # Get from TV (0-50), convert to 0-100
        ...         hardware_value = await self._get_from_tv()
        ...         return int((hardware_value / 50) * 100)
        ...     
        ...     async def set_brightness(self, level: int) -> bool:
        ...         # Clamp to 0-100, convert to 0-50
        ...         clamped = max(0, min(100, level))
        ...         hardware_value = int((clamped / 100) * 50)
        ...         return await self._send_to_tv(hardware_value)
    """
    
    @property
    @abstractmethod
    def brightness_range(self) -> Tuple[int, int]:
        """Return hardware brightness range (min, max) in native units.
        
        This is informational only - all public methods use normalized 0-100 range.
        Helps with debugging and validation (e.g., warn if TV responds outside range).
        
        Returns:
            Tuple[int, int]: (min, max) brightness in hardware units
            
        Examples:
            - Bravia TV: (0, 50)
            - Laptop WMI: (0, 100)
            - Samsung TV: (0, 20)
            - LG TV: (0, 100)
        """
        pass
    
    @abstractmethod
    async def get_brightness(self) -> Optional[int]:
        """Get current brightness level (0-100 normalized).
        
        Implementations should:
          1. Query hardware for current brightness in native units
          2. Convert from hardware range to 0-100
          3. Use asyncio.to_thread() for blocking network/I2C calls
          4. Cache last known value to reduce hardware polling
          5. Return None if device offline (don't raise exceptions)
        
        Returns:
            int: Brightness level 0-100, or None if device offline/unreachable
            
        Example:
            >>> bravia = BraviaControl(ip="192.168.1.100", psk="your_psk_here")
            >>> brightness = await bravia.get_brightness()
            >>> print(f"Current brightness: {brightness}%")  # 0-100
        """
        pass
    
    @abstractmethod
    async def set_brightness(self, level: int) -> Optional[bool]:
        """Set absolute brightness level (0-100 normalized).
        
        Implementations should:
          1. Clamp input to 0-100 range
          2. Convert to hardware-specific range
          3. Use asyncio.to_thread() for blocking calls
          4. Return False on command failure (device online but rejected)
          5. Return None if device offline
        
        Args:
            level: Target brightness 0-100 (will be clamped to valid range)
        
        Returns:
            bool: True if successful, False if failed, None if device offline
            
        Example:
            >>> await bravia.set_brightness(50)  # Set to 50% (maps to 25/50 hardware)
            True
            >>> await bravia.set_brightness(150)  # Auto-clamped to 100
            True
        """
        pass
    
    @abstractmethod
    async def adjust_brightness(self, delta: int) -> Optional[bool]:
        """Adjust brightness by delta (0-100 normalized units).
        
        Default implementation (can be overridden for optimization):
          1. Get current brightness
          2. Apply delta and clamp to 0-100
          3. Call set_brightness() with new value
        
        Implementations can override for efficiency (e.g., relative commands).
        
        Args:
            delta: Brightness change (positive=brighter, negative=dimmer)
        
        Returns:
            Optional[bool]: True if successful, False if failed, None if device offline
            
        Note:
            Return True even if already at limit (no-op is success).
            Return False only on actual errors.
            Return None if device offline.
            
        Example:
            >>> await bravia.adjust_brightness(-10)  # Dim by 10%
            True
            >>> # At 0, adjust by -5
            >>> await bravia.adjust_brightness(-5)  # No-op, returns True
            True
        """
        # Default implementation (subclasses can override for optimization)
        current = await self.get_brightness()
        if current is None:
            return None  # Device offline
        
        new_level = max(0, min(100, current + delta))
        result = await self.set_brightness(new_level)
        return result  # Optional[bool]: True/False/None
