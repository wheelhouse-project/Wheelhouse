"""Sony Bravia TV brightness control plugin.

This plugin integrates Sony Bravia TVs into the WheelHouse brightness control
system using the plugin architecture. It subscribes to brightness adjustment
commands from the EventBus and controls the TV via the BraviaControl integration.

Key Classes:
  - BraviaPlugin: Plugin implementation for Bravia TV brightness control.

Key Features:
  - SSDP auto-discovery of Bravia TVs (no IP configuration needed)
  - Event-driven brightness control via BraviaControl
  - Publishes state changes for coordinator awareness
  - Publishes overflow events when hardware limits reached
  - Graceful degradation when TV offline

Event Integration:
  Subscribes to:
    - HardwareBrightnessCommand: Hardware brightness change requests from coordinator
  
  Publishes:
    - BrightnessStateChanged: Current brightness and limit state
    - BrightnessOverflowEvent: When hardware can't adjust further

Configuration:
  ```toml
  [plugins.bravia]
  enabled = true
  psk = "your_psk_here"  # Pre-Shared Key configured on TV
  ```
  
  Note: IP address is auto-discovered via SSDP - no need to configure.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.integrations.bravia_control import (
    BraviaControl,
    discover_bravia_ssdp,
    validate_bravia_api
)
from services.wheelhouse.integrations.display_discovery import (
    discover_displays,
    find_sony_displays
)
from services.wheelhouse.events import (
    HardwareBrightnessCommand,
    BrightnessStateChanged,
    BrightnessOverflowEvent
)

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus

logger = logging.getLogger(__name__)


class BraviaPlugin(BasePlugin):
    """Sony Bravia hardware brightness plugin with EventBus integration."""
    
    def __init__(self):
        """Initialize BraviaPlugin with uninitialized state.
        
        Actual initialization occurs in initialize() method after config is available.
        """
        super().__init__()
        self._config: "ConfigService" = None  # type: ignore
        self._event_bus: "EventBus" = None  # type: ignore
        self._bravia_control: BraviaControl = None  # type: ignore
        self._last_known_brightness: int | None = None
        self._last_health_check: float | None = None
        self._last_error: str | None = None
    
    @property
    def name(self) -> str:
        """Return unique plugin identifier.
        
        Returns:
            str: "bravia" (used in config: [plugins.bravia])
        """
        return "bravia"
    
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """:flow: Brightness Control Plugin System
        :step: 1
        :description: Verifies Sony display connected via EDID, then resolves TV IP
        :data_in: ConfigService and EventBus references
        :data_out: Initialized BraviaControl instance
        :notes: First checks EDID for Sony display (proves physical connection). Then resolves TV IP: uses ip_address from config if set (deterministic, supports multi-TV setups), otherwise falls back to SSDP discovery. PSK required from config for authentication.
        """
        self._config = config
        self._event_bus = event_bus
        
        # Load config
        plugins_config = config.get("plugins", {})
        bravia_config = plugins_config.get("bravia", {})
        psk = bravia_config.get("psk", "")
        configured_ip = bravia_config.get("ip_address", "")

        # Validate PSK is configured
        if not psk or psk == "your_psk_here":
            raise ValueError("plugins.bravia.psk must be configured (not placeholder)")

        # Phase 1: Verify Sony display is physically connected via EDID
        logger.info("BraviaPlugin: Checking for Sony display via EDID...")
        displays = await discover_displays()
        sony_displays = find_sony_displays(displays)

        if not sony_displays:
            # No Sony display connected - normal on machines without TV
            logger.info("BraviaPlugin: No Sony display connected, plugin inactive")
            self._state = PluginState.INITIALIZED
            return

        logger.info(f"BraviaPlugin: Found Sony display via EDID: {sony_displays[0].model}")

        # Phase 2: Resolve TV IP - use configured IP if present, otherwise SSDP
        if configured_ip:
            logger.info(f"BraviaPlugin: Using configured IP {configured_ip} (skipping SSDP)")
            if not await validate_bravia_api(configured_ip, psk):
                raise ValueError(f"Bravia TV at {configured_ip} rejected PSK - check configuration")
            tv_ip = configured_ip
        else:
            logger.info("BraviaPlugin: No IP configured, searching via SSDP...")
            tv_ip = await discover_bravia_ssdp()
            if not tv_ip:
                raise ValueError("Sony display detected but no Bravia TV found on network via SSDP")
            if not await validate_bravia_api(tv_ip, psk):
                raise ValueError(f"Bravia TV at {tv_ip} rejected PSK - check configuration")

        # Create BraviaControl instance
        self._bravia_control = BraviaControl(ip_address=tv_ip, psk=psk)
        self._state = PluginState.INITIALIZED
        source = "configured IP" if configured_ip else "SSDP"
        logger.info(f"BraviaPlugin: Initialized for TV at {tv_ip} ({source} + EDID verified)")
    
    async def start(self) -> None:
        """:flow: Brightness Control Plugin System
        :step: 2
        :description: Subscribes to brightness commands and performs initial TV connectivity check
        :data_in: Plugin state and EventBus hooks
        :data_out: Active subscription and optional BrightnessStateChanged event
        :notes: Subscribes to HardwareBrightnessCommand events and queries current TV brightness. If TV is reachable, publishes initial state to coordinator. Error Handling: Exceptions caught and state set to FAILED, errors logged but not raised (plugin fails independently), TV offline is not a failure (plugin remains in RUNNING state).

        """
        try:
            self._state = PluginState.STARTING
            
            # Skip if no hardware available (no Sony display connected)
            if self._bravia_control is None:
                self._state = PluginState.RUNNING
                logger.info(f"{self.name}: Started (inactive - no Sony display)")
                return
            
            # Subscribe to brightness adjustment commands
            self._event_bus.subscribe(HardwareBrightnessCommand, self._handle_brightness_command)
            logger.debug(f"{self.name}: Subscribed to HardwareBrightnessCommand")
            
            # Perform initial connectivity check and publish initial state
            try:
                current_brightness = await self._bravia_control.get_brightness()
                if current_brightness is not None:
                    await self._publish_state_change(current_brightness)
                    logger.info(f"{self.name}: TV connected, initial brightness: {current_brightness}%")
                else:
                    logger.warning(f"{self.name}: TV offline or unreachable during startup")
            except Exception as e:
                logger.warning(f"{self.name}: Initial connectivity check failed: {e}")
                # Not fatal - plugin can still handle commands when TV comes online
            
            self._state = PluginState.RUNNING
            logger.info(f"{self.name}: Started successfully")
            
        except Exception as e:
            logger.error(f"{self.name}: Failed to start: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    async def stop(self) -> None:
        """:flow: Brightness Control Plugin System
        :step: 6
        :description: Stops plugin and releases EventBus subscriptions
        :data_in: Plugin state
        :data_out: PluginState.STOPPED with released subscriptions
        :notes: Unsubscribes from events and cleans up. BraviaControl doesn't maintain persistent connections, so no connection cleanup needed.

        """
        self._state = PluginState.STOPPING
        
        try:
            # Unsubscribe from events if EventBus supports it
            # (Current EventBus implementation may not have unsubscribe)
            logger.debug(f"{self.name}: Stopping")
        except Exception as e:
            logger.error(f"{self.name}: Error during stop: {e}")
        finally:
            self._state = PluginState.STOPPED
            logger.info(f"{self.name}: Stopped")
    
    def get_health_status(self) -> dict:
        """Return current health and status information.
        
        Health status reflects:
          - Plugin state (RUNNING, FAILED, etc.)
          - TV connectivity (based on last known brightness)
          - Last error if any
        
        Status values:
          - "healthy": Plugin running and TV responsive
          - "degraded": Plugin running but TV offline/unreachable
          - "unhealthy": Plugin in FAILED state
        
        Returns:
            dict: Health status with state, connectivity, and error info
        """
        status = "unhealthy"
        if self._state == PluginState.RUNNING:
            if self._last_known_brightness is not None:
                status = "healthy"
            else:
                status = "degraded"  # Running but TV not responding
        
        return {
            "status": status,
            "state": self._state.value,
            "last_check": self._last_health_check,
            "details": {
                "tv_connected": self._last_known_brightness is not None,
                "last_brightness": self._last_known_brightness,
                "error": self._last_error
            }
        }
    
    async def _handle_brightness_command(self, event: HardwareBrightnessCommand) -> None:
        """:flow: Brightness Control Plugin System
        :step: 3
        :description: Applies brightness delta to hardware, handling limits and offline scenarios
        :data_in: HardwareBrightnessCommand with delta
        :data_out: BrightnessStateChanged or BrightnessOverflowEvent
        :notes: Flow: (1) Query current brightness (2) Calculate target (clamped 0-100) (3) Detect if at hardware limit (4) Apply change via BraviaControl (5) Publish state or overflow event. Overflow Detection: delta<0 and brightness==0 (can't dim), delta>0 and brightness==100 (can't brighten), TV offline (reason="device_offline"). Bravia hardware range is 0-50, so minimum delta is ±2 on 0-100 scale.
        """
        """
        Args:
            event: HardwareBrightnessCommand with delta value
        """
        try:
            delta = event.delta
            logger.debug(f"{self.name}: Received hardware brightness command: delta={delta}")
            
            # Bravia hardware range is 0-50, so minimum meaningful delta on 0-100 scale is 2
            # (1 unit on 0-50 scale = 2% on 0-100 scale)
            if delta != 0:
                if abs(delta) < 2:
                    # Scale up to minimum step: preserve sign, enforce minimum magnitude
                    delta = 2 if delta > 0 else -2
                    logger.debug(f"{self.name}: Enforcing minimum delta=±2 for Bravia hardware (0-50 range)")
            
            # Get current brightness
            current_brightness = await self._bravia_control.get_brightness()
            
            # Check if TV is offline
            if current_brightness is None:
                logger.warning(f"{self.name}: TV offline, publishing overflow event")
                await self._publish_overflow(delta, "device_offline")
                self._last_known_brightness = None
                return
            
            # Check if at hardware limit (overflow condition)
            at_min = current_brightness == 0
            at_max = current_brightness == 100
            
            if (delta < 0 and at_min) or (delta > 0 and at_max):
                logger.debug(f"{self.name}: At hardware limit (current={current_brightness}%, delta={delta}), publishing overflow")
                await self._publish_overflow(delta, "at_hardware_limit")
                await self._publish_state_change(current_brightness)  # Still publish state for awareness
                return
            
            # Apply brightness adjustment
            result = await self._bravia_control.adjust_brightness(delta)
            
            if result is None:
                # TV went offline during adjustment
                logger.warning(f"{self.name}: TV went offline during adjustment")
                await self._publish_overflow(delta, "device_offline")
                self._last_known_brightness = None
                return
            
            if result is False:
                # Adjustment failed but TV is online
                logger.error(f"{self.name}: Brightness adjustment failed (command rejected)")
                self._last_error = "Brightness adjustment command rejected by TV"
                return
            
            # Adjustment successful - get new brightness and publish state
            new_brightness = await self._bravia_control.get_brightness()
            if new_brightness is not None:
                await self._publish_state_change(new_brightness)
                logger.debug(f"{self.name}: Brightness adjusted to {new_brightness}%")
            
        except Exception as e:
            logger.error(f"{self.name}: Error handling brightness command: {e}", exc_info=True)
            self._last_error = str(e)
    
    async def _publish_state_change(self, brightness: int) -> None:
        """:flow: Brightness Control Plugin System
        :step: 4
        :description: Publishes current brightness state to coordinator
        :data_in: Normalized brightness (0-100)
        :data_out: BrightnessStateChanged event on EventBus
        :notes: Informs BrightnessCoordinator of current TV brightness state, enabling it to make informed decisions about overflow cascade routing. Includes at_min and at_max flags.
        """
        
        """
        Args:
            brightness: Current brightness level (0-100 normalized)
        """
        event = BrightnessStateChanged(
            level=brightness,
            at_min=(brightness == 0),
            at_max=(brightness == 100),
            source_plugin=self.name,
            timestamp=time.time()
        )
        await self._event_bus.publish(event)
        self._last_known_brightness = brightness
        self._last_health_check = time.time()
        logger.debug(f"{self.name}: Published state change: level={brightness}%, at_min={event.at_min}, at_max={event.at_max}")
    
    async def _publish_overflow(self, delta: int, reason: str) -> None:
        """:flow: Brightness Control Plugin System
        :step: 5
        :description: Publishes overflow event when hardware cannot satisfy brightness delta
        :data_in: Remaining delta and reason string
        :data_out: BrightnessOverflowEvent on EventBus
        :notes: Signals to BrightnessCoordinator that TV cannot fulfill adjustment and dimming/brightening should cascade to software methods (f.lux, overlay dimmer, etc.). Reasons: "at_hardware_limit" or "device_offline".
        """
        
        """
        Args:
            delta: Remaining brightness adjustment that couldn't be applied
            reason: Why overflow occurred ("at_hardware_limit", "device_offline")
        """
        event = BrightnessOverflowEvent(
            delta=delta,
            source_plugin=self.name,
            reason=reason,
            timestamp=time.time()
        )
        await self._event_bus.publish(event)
        self._last_health_check = time.time()
        logger.debug(f"{self.name}: Published overflow event: delta={delta}, reason={reason}")
