"""Windows laptop internal panel brightness control plugin.

This plugin integrates Windows laptops' internal displays into the WheelHouse
brightness control system using the plugin architecture. It subscribes to
brightness adjustment commands from the EventBus and controls the laptop display
via the InternalPanelControl integration.

Key Classes:
  - InternalPanelPlugin: Plugin implementation for laptop internal panel brightness control.

Key Features:
  - Event-driven brightness control via WMI
  - Publishes state changes for coordinator awareness
  - Publishes overflow events when hardware limits reached
  - Graceful degradation when WMI APIs unavailable (desktop machines)
  - Configuration validation and health reporting

Event Integration:
  Subscribes to:
    - HardwareBrightnessCommand: Hardware brightness change requests from coordinator
  
  Publishes:
    - BrightnessStateChanged: Current brightness and limit state
    - BrightnessOverflowEvent: When hardware can't adjust further

Configuration:
  ```toml
  [plugins.internal_panel]
  enabled = true
  # WMI settings are auto-detected, no manual configuration needed
  ```

Typical Usage:
  # Plugin is auto-discovered and initialized by PluginRegistry
  # No direct instantiation needed
  
  # Configuration in config.toml:
  [plugins.internal_panel]
  enabled = true

Hardware Requirements:
  - Windows laptop with internal display
  - WMI brightness APIs available (WmiMonitorBrightness/WmiMonitorBrightnessMethods)
  - Gracefully degrades on desktop machines or older laptops without WMI support
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.integrations.internal_panel_control import InternalPanelControl
from services.wheelhouse.events import (
    HardwareBrightnessCommand,
    BrightnessStateChanged,
    BrightnessOverflowEvent
)

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)


class InternalPanelPlugin(BasePlugin):
    """:flow: Brightness Control Plugin System
    :step: 4
    :description: InternalPanelPlugin subscribes to HardwareBrightnessCommand events from BrightnessCoordinator and controls Windows laptop internal display brightness via WMI. When hardware limits are reached (0% or 100%), publishes BrightnessOverflowEvent to signal cascade to software dimming. Publishes BrightnessStateChanged after each adjustment to maintain coordinator state awareness.
    :data_in: HardwareBrightnessCommand (delta: int)
    :data_out: BrightnessStateChanged (level, at_min, at_max, source_plugin), BrightnessOverflowEvent (delta, source_plugin, reason)
    """
    
    """Plugin implementation for Windows laptop internal panel brightness control.
    
    This plugin manages laptop internal displays as a hardware brightness source in the
    multi-stage brightness control system. It handles:
      - Brightness adjustments within hardware range (0-100 WMI range)
      - State reporting for coordinator decision-making
      - Overflow signaling when limits reached
      - Graceful degradation when WMI APIs unavailable (desktop machines)
    
    Architecture Pattern:
      This plugin demonstrates the standard WheelHouse plugin pattern:
      1. Event subscription in start() method
      2. Hardware control via integration layer (InternalPanelControl)
      3. State publishing for coordinator awareness
      4. Error isolation (plugin failures don't crash core)
      5. Health monitoring and configuration validation
    
    WMI Integration:
      Uses WmiMonitorBrightness and WmiMonitorBrightnessMethods for hardware control.
      These APIs are only available on laptops with internal displays that support
      WMI brightness control. Desktop machines gracefully degrade.
    
    Error Handling:
      - Plugin disables itself if WMI APIs unavailable
      - Individual brightness commands fail gracefully
      - Health status reflects WMI availability
      - Logs informational messages for unavailable hardware
    """
    
    def __init__(self):
        """Initialize the internal panel plugin.
        
        Sets up instance variables but does not connect to hardware.
        Call initialize() and start() to activate the plugin.
        """
        super().__init__()
        self._config: Optional["ConfigService"] = None
        self._event_bus: Optional["EventBus"] = None
        self._display_control: Optional[InternalPanelControl] = None
        self._current_brightness: Optional[int] = None
        self._last_error: Optional[str] = None
        self._is_hardware_available = False
        
    @property
    def name(self) -> str:
        """Plugin name for registry and logging."""
        return "internal_panel"
    
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize internal panel plugin with configuration and event bus.
        
        This method validates configuration, creates the display control instance,
        and checks hardware availability. It does not start event subscriptions
        (that happens in start()).
        
        Args:
            config: Configuration service for plugin settings
            event_bus: Event bus for pub-sub communication
        """
        try:
            self._config = config
            self._event_bus = event_bus
            
            # Check if plugin is enabled in configuration
            enabled = config.get("plugins.internal_panel.enabled", True)
            if not enabled:
                logger.info("Internal panel plugin disabled in configuration")
                self._state = PluginState.INITIALIZED
                return
            
            # Create and initialize the display control
            self._display_control = InternalPanelControl()
            hardware_available = await self._display_control.initialize()
            
            if not hardware_available:
                logger.info("Internal panel brightness control not available (normal on desktop machines)")
                self._is_hardware_available = False
                self._state = PluginState.INITIALIZED
                return
            
            # Test initial brightness reading
            self._current_brightness = await self._display_control.get_brightness()
            if self._current_brightness is None:
                logger.warning("Internal panel detected but brightness not readable")
                self._is_hardware_available = False
                self._state = PluginState.INITIALIZED
                return
            
            logger.info(f"Internal panel plugin initialized successfully (current: {self._current_brightness}%)")
            self._is_hardware_available = True
            self._state = PluginState.INITIALIZED
            
        except Exception as e:
            logger.error(f"Failed to initialize internal panel plugin: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    async def start(self) -> None:
        """Start the internal panel plugin and subscribe to events.
        
        Subscribes to HardwareBrightnessCommand events and publishes initial
        state if hardware is available.
        """
        try:
            self._state = PluginState.STARTING
            
            # Only subscribe to events if hardware is available
            if self._is_hardware_available and self._event_bus:
                self._event_bus.subscribe(HardwareBrightnessCommand, self._handle_brightness_command)
                logger.debug("Subscribed to HardwareBrightnessCommand events")
                
                # Publish initial state
                await self._publish_brightness_state()
            
            self._state = PluginState.RUNNING
            status = "with hardware" if self._is_hardware_available else "without hardware (graceful degradation)"
            logger.info(f"Internal panel plugin started successfully {status}")
            
        except Exception as e:
            logger.error(f"Failed to start internal panel plugin: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    async def stop(self) -> None:
        """Stop the internal panel plugin and clean up resources."""
        try:
            self._state = PluginState.STOPPING
            
            # No background tasks to cancel for this plugin
            # WMI connections are cleaned up automatically
            
            self._state = PluginState.STOPPED
            logger.info("Internal panel plugin stopped successfully")
            
        except Exception as e:
            logger.error(f"Failed to stop internal panel plugin: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    def get_health_status(self) -> dict:
        """Return plugin health status and diagnostic information.
        
        Returns:
            dict: Health status with plugin state, hardware availability, and errors
        """
        if not self._is_hardware_available:
            status = "healthy"  # Not having hardware is not an error condition
            message = "Hardware not available (normal on desktop machines)"
        elif self._state == PluginState.RUNNING:
            status = "healthy"
            message = f"Operating normally (brightness: {self._current_brightness}%)"
        else:
            status = "unhealthy"
            message = self._last_error or "Unknown error"
        
        return {
            "status": status,
            "state": self._state.value,
            "hardware_available": self._is_hardware_available,
            "current_brightness": self._current_brightness,
            "brightness_range": (0, 100) if self._display_control else None,
            "message": message,
            "last_error": self._last_error
        }
    
    async def _handle_brightness_command(self, event: HardwareBrightnessCommand) -> None:
        """Handle hardware brightness adjustment command.
        
        :flow: Brightness Control Plugin System
        :step: 5
        :description: Process brightness adjustment request by calling InternalPanelControl, check for hardware limits, and publish state change or overflow events.
        :data_in: HardwareBrightnessCommand (delta: int)
        :data_out: BrightnessStateChanged or BrightnessOverflowEvent
        """
        
        """This method processes brightness adjustment commands from the BrightnessCoordinator:
        1. Gets current brightness from hardware
        2. Calculates new brightness with delta
        3. Checks if adjustment would exceed hardware limits
        4. Either adjusts hardware or publishes overflow event
        5. Publishes state change event after successful adjustment
        
        Args:
            event: Hardware brightness command with delta adjustment
        """
        if not self._is_hardware_available or not self._display_control:
            logger.debug("Ignoring brightness command - hardware not available")
            return
        
        try:
            # Use write-through cache: trust our own last-set value
            # WMI query returns stale values immediately after set on some Lenovo laptops
            if self._current_brightness is not None:
                current = self._current_brightness
            else:
                # Only query WMI if we have no cached value (startup)
                current = await self._display_control.get_brightness()
                if current is None:
                    logger.warning("Cannot read current brightness - device may be offline")
                    await self._publish_overflow_event(event.delta, "device_offline")
                    return
                self._current_brightness = current
            
            logger.debug(f"Brightness command: current={current}, delta={event.delta}")
            
            # Calculate target brightness
            target = current + event.delta
            
            # Check for hardware limits (will overflow)
            if target < 0:
                # At minimum, cascade remaining adjustment to software
                overflow_delta = target  # Negative value = remaining dimming needed
                await self._display_control.set_brightness(0)
                self._current_brightness = 0
                await self._publish_brightness_state()
                await self._publish_overflow_event(overflow_delta, "at_hardware_limit")
                logger.debug(f"Hardware at minimum (0%), overflow cascade: {overflow_delta}")
                return
            
            if target > 100:
                # At maximum, cascade remaining adjustment to software
                overflow_delta = target - 100  # Positive value = remaining brightening needed
                await self._display_control.set_brightness(100)
                self._current_brightness = 100
                await self._publish_brightness_state()
                await self._publish_overflow_event(overflow_delta, "at_hardware_limit")
                logger.debug(f"Hardware at maximum (100%), overflow cascade: {overflow_delta}")
                return
            
            # Within limits, adjust normally
            success = await self._display_control.set_brightness(target)
            if success:
                self._current_brightness = target
                await self._publish_brightness_state()
                logger.debug(f"Adjusted internal panel brightness to {target}%")
            else:
                logger.warning(f"Failed to set internal panel brightness to {target}%")
                # Don't publish overflow on failure - this is a hardware error
                
        except Exception as e:
            logger.error(f"Error handling brightness command: {e}", exc_info=True)
            self._last_error = str(e)
    
    async def _publish_brightness_state(self) -> None:
        """Publish current brightness state for coordinator awareness.
        
        Publishes BrightnessStateChanged event with current level and limit flags.
        This enables the BrightnessCoordinator to make fast decisions during
        overflow cascade scenarios.
        """
        if not self._event_bus or self._current_brightness is None:
            return
        
        try:
            await self._event_bus.publish(BrightnessStateChanged(
                level=self._current_brightness,
                at_min=(self._current_brightness == 0),
                at_max=(self._current_brightness == 100),
                source_plugin=self.name,
                timestamp=time.time()
            ))
            logger.debug(f"Published brightness state: {self._current_brightness}%")
            
        except Exception as e:
            logger.error(f"Failed to publish brightness state: {e}")
    
    async def _publish_overflow_event(self, delta: int, reason: str) -> None:
        """Publish brightness overflow event for cascade to software dimming.
        
        Publishes BrightnessOverflowEvent when hardware cannot handle the full
        adjustment, signaling the BrightnessCoordinator to cascade to software
        dimming methods (f.lux, overlay, etc.).
        
        Args:
            delta: Remaining adjustment that couldn't be applied by hardware
            reason: Why overflow occurred ("at_hardware_limit", "device_offline")
        """
        if not self._event_bus:
            return
        
        try:
            await self._event_bus.publish(BrightnessOverflowEvent(
                delta=delta,
                source_plugin=self.name,
                reason=reason,
                timestamp=time.time()
            ))
            logger.debug(f"Published overflow event: delta={delta}, reason={reason}")
            
        except Exception as e:
            logger.error(f"Failed to publish overflow event: {e}")