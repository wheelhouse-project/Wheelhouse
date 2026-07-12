"""Multi-stage brightness control orchestration for WheelHouse.

This module provides the BrightnessCoordinator, which acts as the central
orchestrator for multi-stage brightness control. It implements a cascading
system where hardware controls (Sony Bravia TV) are used first, and when
hardware reaches its limits, software dimming (f.lux, overlay dimmer, or
gamma dimming) takes over for extended brightness range.

Architecture:
  - Event-driven: Subscribes to brightness commands and plugin state events
  - Hardware-first: Routes commands to hardware plugins (BraviaPlugin) initially
  - Cascade on overflow: Engages software dimmers when hardware at limits
  - Unwinding: Restores hardware brightness before disengaging software dimmers
  - Configuration-driven: Software dimmer selection via config

:flow: Step 0: Coordinator Initialization
==========================================
**Entry:** ServiceManager instantiates coordinator with dependencies
**Purpose:** Prepare coordinator for brightness orchestration

1. Store references to config_service and event_bus
2. Load configuration:
   - brightness_coordinator.software_dimmer (flux|software_dimmer|gamma_dimmer)
   - brightness_coordinator.unwinding_threshold (default 10)
3. Initialize state tracking:
   - _plugin_states: Cache of hardware plugin brightness states
   - _software_dimmer_level: Current software dimming level (0-100)
   - _is_software_active: Flag indicating software dimming engaged
4. Set initial state to IDLE (hardware only)

**Exit:** Coordinator ready for start()

:flow: Step 0.5: Coordinator Startup
=====================================
**Entry:** ServiceManager calls start() after plugin system initialized
**Purpose:** Subscribe to events and establish orchestration layer

1. Subscribe to BrightnessAdjustCommand from MouseHandler
2. Subscribe to BrightnessStateChanged from hardware plugins (state cache)
3. Subscribe to BrightnessOverflowEvent from hardware plugins (cascade trigger)
4. Log coordinator status (software dimmer configured, thresholds)
5. Set state to ACTIVE

**Exit:** Coordinator receiving events, routing ready

:flow: Step 1: Overall Brightness Command Flow
===============================================
**Entry:** User scrolls mouse wheel in brightness zone
**Purpose:** Orchestrate multi-stage brightness adjustment

1. MouseHandler publishes BrightnessAdjustCommand(delta)
2. Coordinator receives command
3. Check current state:
   - If IDLE (hardware only): Route to hardware plugins
   - If CASCADED (software active): Handle unwinding logic
4. Hardware plugin processes command:
   - If successful: Publishes BrightnessStateChanged
   - If at limit: Publishes BrightnessOverflowEvent
5. Coordinator handles results:
   - State change: Update plugin state cache
   - Overflow: Trigger cascade to software dimmer
6. Software dimmer adjusts (if engaged)

**Exit:** Brightness adjusted via hardware and/or software

:flow: Step 2: Hardware Routing (IDLE State)
=============================================
**Entry:** BrightnessAdjustCommand received while in IDLE state
**Purpose:** Route commands to hardware plugins first

1. Identify target hardware plugin (currently: bravia)
2. Check plugin state cache:
   - If at_max and delta > 0: Hardware can't brighten further
   - If at_min and delta < 0: Hardware can't dim further
   - Otherwise: Hardware can adjust
3. If hardware can adjust:
   - Command already published to EventBus (plugins subscribed)
   - Hardware plugin handles directly
   - Await BrightnessStateChanged or BrightnessOverflowEvent
4. If hardware at limit:
   - Trigger cascade to software dimmer immediately
   - Pass full delta to software dimmer

**Exit:** Command routed to hardware or cascade triggered

:flow: Step 3: Cascade to Software (Overflow Handling)
=======================================================
**Entry:** BrightnessOverflowEvent received from hardware plugin
**Purpose:** Engage software dimmer when hardware reaches limits

1. Validate overflow event:
   - Check reason (at_hardware_limit, device_offline)
   - Validate delta direction matches state (at_min for dimming, at_max for brightening)
2. Check software dimmer availability:
   - Load configured dimmer (flux, software_dimmer, gamma_dimmer)
   - If not available: Log warning, ignore overflow
3. Engage software dimmer:
   - If first overflow: Initialize software dimmer level to 100
   - Apply delta to software dimmer level (clamp 0-100)
   - Call appropriate dimming method based on config
4. Update coordinator state:
   - Set _is_software_active = True
   - Store _software_dimmer_level
   - Transition to CASCADED state
5. Log cascade event (hardware limit → software engaged)

**Exit:** Software dimming active, extended range available

:flow: Step 4: Unwinding Logic (CASCADED State)
================================================
**Entry:** BrightnessAdjustCommand received while in CASCADED state
**Purpose:** Restore hardware brightness before disengaging software dimmer

1. Check command direction:
   - If dimming (delta < 0): Apply to software dimmer only
   - If brightening (delta > 0): Begin unwinding process
2. Unwinding process (brightening):
   - Calculate new software dimmer level (current + delta)
   - If new level >= unwinding_threshold (default 10):
     - Apply brightening to software dimmer only
     - Keep hardware at current state
   - If new level >= 100 (software fully removed):
     - Disable software dimmer
     - Calculate remaining delta for hardware
     - Pass remaining delta to hardware plugin
     - Set _is_software_active = False
     - Transition to IDLE state
3. Partial unwinding optimization:
   - When software_level crosses 50: Start restoring hardware by 10%
   - When software_level reaches 100: Fully disengage software
4. Log unwinding progress (software → hardware transition)

**Exit:** Software dimming reduced, hardware restoration in progress or complete

:flow: Step 5: Plugin State Caching
====================================
**Entry:** BrightnessStateChanged event received from hardware plugin
**Purpose:** Maintain accurate state for routing decisions

1. Extract plugin state from event:
   - source_plugin: Plugin identifier (e.g., "bravia")
   - level: Current brightness (0-100)
   - at_min: Hardware at minimum brightness
   - at_max: Hardware at maximum brightness
2. Update _plugin_states cache:
   - Store state by plugin name
   - Overwrite previous state (always use latest)
3. Check for state transitions:
   - If at_min or at_max: Log hardware limit reached
   - If returned from limit: Log hardware available again
4. Use cached state for routing decisions in Step 2

**Exit:** Plugin state cache updated, routing logic has fresh state

:flow: Step 99: Coordinator Shutdown
=====================================
**Entry:** ServiceManager stopping services
**Purpose:** Clean shutdown, release resources

1. Unsubscribe from all EventBus events:
   - BrightnessAdjustCommand
   - BrightnessStateChanged
   - BrightnessOverflowEvent
2. If software dimmer active:
   - Restore to 100% (remove dimming)
   - Log forced restoration on shutdown
3. Clear state caches:
   - _plugin_states = {}
   - _software_dimmer_level = 0
   - _is_software_active = False
4. Set state to STOPPED

**Exit:** Coordinator stopped, EventBus clean, software dimming disabled
"""
import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Optional
from enum import Enum

from utils.win_input_sender import press_keys

from services.wheelhouse.events import (
    BrightnessAdjustCommand,
    HardwareBrightnessCommand,
    BrightnessStateChanged,
    BrightnessOverflowEvent
)

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus
    from services.wheelhouse.handlers.software_dimmer import SoftwareDimmer

logger = logging.getLogger(__name__)


class CoordinatorState(Enum):
    """Brightness coordinator operational states."""
    IDLE = "idle"           # Hardware only, no software dimming
    CASCADED = "cascaded"   # Software dimmer engaged
    STOPPED = "stopped"     # Coordinator not running


class BrightnessCoordinator:
    """
    Orchestrates multi-stage brightness control across hardware and software dimmers.
    
    This coordinator implements a cascading brightness system where hardware controls
    (Sony Bravia TV) are used first, and when hardware reaches its limits, software
    dimming (f.lux, overlay dimmer, or gamma dimming) takes over for extended range.
    
    The coordinator is event-driven, subscribing to brightness commands from input
    handlers (MouseHandler) and state events from hardware plugins (BraviaPlugin).
    It maintains a state cache of hardware plugin brightness levels and manages the
    cascade logic to provide smooth brightness control across the full range.
    """
    
    def __init__(
        self,
        config_service: "ConfigService",
        event_bus: "EventBus",
        software_dimmer: Optional["SoftwareDimmer"] = None
    ):
        """
        Initialize the brightness coordinator.
        
        Args:
            config_service: Configuration service for loading settings
            event_bus: EventBus for subscribing to brightness events
            software_dimmer: Optional software dimmer for extended range
        """
        self._config_service: "ConfigService" = config_service  # type: ignore
        self._event_bus: "EventBus" = event_bus  # type: ignore
        self._software_dimmer: Optional["SoftwareDimmer"] = software_dimmer
        
        # Load configuration
        config = self._config_service.get_config()
        coordinator_config = config.get("brightness_coordinator", {})
        
        self._software_dimmer_type: str = coordinator_config.get("software_dimmer", "flux")
        self._unwinding_threshold: int = coordinator_config.get("unwinding_threshold", 10)
        
        # Load f.lux configuration settings
        self._flux_transition_percent: int = coordinator_config.get("flux_transition_percent", 100)
        self._flux_dim_hotkey: list = coordinator_config.get("flux_dim_hotkey", ["alt", "pagedown"])
        self._flux_brighten_hotkey: list = coordinator_config.get("flux_brighten_hotkey", ["alt", "pageup"])
        
        # State tracking
        self._plugin_states: Dict[str, Dict] = {}  # {plugin_name: {level, at_min, at_max}}
        self._software_dimmer_level: int = 100  # 100 = no dimming, 0 = max dimming
        self._is_software_active: bool = False
        self._state: CoordinatorState = CoordinatorState.STOPPED
        
        logger.info(
            f"BrightnessCoordinator initialized (software_dimmer={self._software_dimmer_type}, "
            f"unwinding_threshold={self._unwinding_threshold})"
        )
    
    def start(self) -> None:
        """
        Start the brightness coordinator and subscribe to events.
        
        This method subscribes to brightness-related events from the EventBus
        and sets the coordinator state to IDLE (hardware-only mode).
        """
        logger.info("Starting BrightnessCoordinator...")
        
        # Initialize software dimmer to 100% (no dimming) to establish known state
        # This ensures coordinator's internal tracking matches actual f.lux level
        if self._software_dimmer:
            logger.debug("Initializing software dimmer to 100% (no dimming)")
            self._software_dimmer.set_brightness(100)
            self._software_dimmer_level = 100
            self._is_software_active = False
        
        # Subscribe to brightness events
        self._event_bus.subscribe(BrightnessAdjustCommand, self._handle_brightness_command)
        self._event_bus.subscribe(BrightnessStateChanged, self._handle_state_change)
        self._event_bus.subscribe(BrightnessOverflowEvent, self._handle_overflow)
        
        self._state = CoordinatorState.IDLE
        logger.info(
            f"BrightnessCoordinator started (state={self._state.value}, "
            f"software_dimmer={self._software_dimmer_type})"
        )
    
    def stop(self) -> None:
        """
        Stop the brightness coordinator and clean up resources.
        
        This method restores software dimming to 100% if active and sets the
        coordinator state to STOPPED. EventBus subscriptions remain active
        (EventBus has no unsubscribe method).
        """
        logger.info("Stopping BrightnessCoordinator...")
        
        # Note: EventBus has no unsubscribe method - subscriptions remain active
        # This is acceptable since coordinator won't process events when STOPPED
        
        # Restore software dimming if active
        if self._is_software_active and self._software_dimmer:
            logger.info("Restoring software dimming on shutdown...")
            self._software_dimmer.set_brightness(100)  # Remove dimming (100% brightness)
            self._is_software_active = False
            self._software_dimmer_level = 100
        
        # Clear state
        self._plugin_states.clear()
        self._state = CoordinatorState.STOPPED
        logger.info("BrightnessCoordinator stopped")
    
    async def _handle_brightness_command(self, event: BrightnessAdjustCommand) -> None:
        """
        Handle brightness adjustment commands from input handlers.
        
        :flow: Brightness Control Abstraction
        :step: 1
        :description: Central entry point for brightness commands, routing based on coordinator state
        :data_in: BrightnessAdjustCommand(delta)
        :data_out: HardwareBrightnessCommand OR Software Dimmer Adjustment
        :notes: Routes commands based on state: IDLE -> Hardware, CASCADED -> Unwinding/Software.

        This is the main entry point for brightness commands. The coordinator
        routes commands based on current state:
        - IDLE: Route to hardware plugins via HardwareBrightnessCommand
        - CASCADED: Handle unwinding logic for software dimmer
        
        Args:
            event: BrightnessAdjustCommand event with delta value
        """
        delta = event.delta
        
        if self._state == CoordinatorState.IDLE:
            # Hardware-only mode: publish HardwareBrightnessCommand for plugins
            await self._route_to_hardware(delta)
            
        elif self._state == CoordinatorState.CASCADED:
            # Software dimmer engaged: handle unwinding logic
            await self._handle_unwinding(delta)
    
    async def _route_to_hardware(self, delta: int) -> None:
        """
        Route brightness command to hardware plugins.
        
        :flow: Brightness Control Abstraction
        :step: 2
        :description: Publishes command for hardware plugins (e.g., Bravia) to consume
        :data_in: delta (int)
        :data_out: HardwareBrightnessCommand
        :notes: Hardware plugins subscribe to this event. Coordinator gates access.

        Publishes HardwareBrightnessCommand which hardware plugins subscribe to.
        This provides a clear separation: coordinator gates hardware access.
        
        Args:
            delta: Brightness adjustment delta
        """
        event = HardwareBrightnessCommand(delta=delta)
        await self._event_bus.publish(event)
        logger.debug(f"Routed to hardware: delta={delta:+d}")
    
    async def _handle_unwinding(self, delta: int) -> None:
        """
        Handle unwinding logic when software dimmer is active.
        
        :flow: Brightness Control Abstraction
        :step: 4
        :description: Restores software dimmer to 100% before returning control to hardware
        :data_in: delta (int)
        :data_out: Software Dimmer Adjustment OR HardwareBrightnessCommand
        :notes: Ensures smooth transition back to hardware control. Transitions CASCADED -> IDLE.

        Unwinding process:
        1. Dimming (delta < 0): Apply to software dimmer only
        2. Brightening (delta > 0): Restore software dimmer, then hardware
        3. When software reaches 100%: Disengage software, pass delta to hardware
        
        Args:
            delta: Brightness adjustment delta (-100 to 100)
        """
        if delta < 0:
            # Dimming: apply to software dimmer only (hardware stays at minimum)
            await self._adjust_software_dimmer(delta)
            
        else:  # delta > 0, brightening
            # Calculate new software dimmer level
            new_level = min(100, self._software_dimmer_level + delta)
            
            if new_level < 100:
                # Software dimmer not fully restored yet - adjust software only
                await self._adjust_software_dimmer(delta)
                
            else:
                # Software dimmer fully restored (100%) - transition back to hardware
                
                # Disengage software dimmer (only touch overlay if not using f.lux)
                if self._software_dimmer_type in ("software_dimmer", "overlay", "gamma_dimmer") and self._software_dimmer:
                    self._software_dimmer.set_brightness(100)  # Remove dimming (100% brightness)
                    logger.info("Software dimming (overlay) fully restored, returning to hardware")
                elif self._software_dimmer_type == "flux":
                    logger.info("Software dimming (f.lux) fully restored, returning to hardware")
                
                self._is_software_active = False
                self._software_dimmer_level = 100
                self._state = CoordinatorState.IDLE
                
                # Now route the current brightening command to hardware
                # (Hardware was at minimum, now it can start brightening)
                await self._route_to_hardware(delta)
                logger.debug(f"Transitioned to IDLE, routed delta={delta:+d} to hardware")
    
    async def _adjust_software_dimmer(self, delta: int) -> None:
        """
        Adjust software dimmer level.
        
        Args:
            delta: Brightness adjustment delta (-100 to 100)
        """
        # Calculate new level (clamped 0-100)
        old_level = self._software_dimmer_level
        new_level = max(0, min(100, self._software_dimmer_level + delta))
        
        # Apply adjustment based on configured dimmer type
        if self._software_dimmer_type == "flux":
            # f.lux hotkey integration using configured hotkeys, sent through
            # the app's own SendInput path. (Formerly pyautogui.hotkey;
            # pyautogui's win32 install dependency MouseInfo is GPLv3 and
            # cannot ship in the Apache-2.0 release. press_keys has no
            # mouse-corner fail-safe, so no FAILSAFE juggling is needed.)
            # Calculate presses needed based on configured transition percent
            # flux_transition_percent represents the % change per keypress (e.g., 2 means 2% per press)
            # For a 1% change with 2% per press: 1 // 2 = 0, so we need at least 1 press
            level_change = abs(new_level - old_level)
            presses = max(1, level_change // self._flux_transition_percent)

            # Use configured hotkeys (dim for decrease, brighten for increase)
            hotkey = self._flux_dim_hotkey if delta < 0 else self._flux_brighten_hotkey

            for _ in range(presses):
                await asyncio.to_thread(press_keys, *hotkey)
                await asyncio.sleep(0.05)  # Small delay between presses

            self._software_dimmer_level = new_level
            logger.debug(
                f"f.lux adjusted: {old_level} → {new_level} "
                f"(delta={delta:+d}, {presses} keypresses using {hotkey})"
            )
        
        elif self._software_dimmer_type in ("software_dimmer", "overlay", "gamma_dimmer"):
            # SoftwareDimmer overlay integration
            if not self._software_dimmer:
                logger.warning("Software dimmer overlay not available, ignoring adjustment")
                return

            if self._software_dimmer.set_brightness(new_level):
                self._software_dimmer_level = new_level
                logger.debug(
                    f"Software dimmer adjusted: {old_level} → {new_level} "
                    f"(delta={delta:+d})"
                )
            else:
                logger.warning(
                    f"Software dimmer failed to apply brightness {new_level}%, "
                    f"keeping level at {old_level}%"
                )
        
        else:
            logger.warning(
                f"Unknown software dimmer type '{self._software_dimmer_type}', "
                f"no adjustment applied"
            )
    
    async def _handle_state_change(self, event: BrightnessStateChanged) -> None:
        """
        Handle brightness state changes from hardware plugins.
        
        This method caches plugin state for routing decisions and logs
        important state transitions (hardware reaching limits).
        
        Args:
            event: BrightnessStateChanged event from hardware plugin
        """
        plugin_name = event.source_plugin
        
        # Update plugin state cache
        self._plugin_states[plugin_name] = {
            "level": event.level,
            "at_min": event.at_min,
            "at_max": event.at_max,
            "timestamp": event.timestamp
        }
        
        # Log important state transitions
        if event.at_min:
            logger.debug(f"Hardware plugin '{plugin_name}' at minimum brightness")
        elif event.at_max:
            logger.debug(f"Hardware plugin '{plugin_name}' at maximum brightness")
    
    async def _handle_overflow(self, event: BrightnessOverflowEvent) -> None:
        """
        Handle overflow events from hardware plugins at brightness limits.
        
        :flow: Brightness Control Abstraction
        :step: 3
        :description: Engages software dimming when hardware reaches physical limits
        :data_in: BrightnessOverflowEvent
        :data_out: Software Dimmer Adjustment
        :notes: Transitions state IDLE -> CASCADED. Extends brightness range virtually.

        When hardware reaches its limits, this method engages software dimming
        to provide extended brightness range. The coordinator transitions from
        IDLE to CASCADED state.
        
        Args:
            event: BrightnessOverflowEvent from hardware plugin
        """
        plugin_name = event.source_plugin
        delta = event.delta
        reason = event.reason
        
        logger.info(
            f"Brightness overflow from '{plugin_name}': delta={delta:+d}, reason={reason}"
        )
        
        # Check if software dimmer available (f.lux doesn't need the overlay object)
        if self._software_dimmer_type not in ("flux", "software_dimmer", "overlay", "gamma_dimmer"):
            logger.warning(
                f"Unknown software dimmer type '{self._software_dimmer_type}', cannot cascade overflow"
            )
            return
        
        if self._software_dimmer_type in ("software_dimmer", "overlay", "gamma_dimmer") and not self._software_dimmer:
            logger.warning(
                f"Software dimmer overlay not available, cannot cascade overflow (delta={delta})"
            )
            return
        
        # Validate overflow direction
        plugin_state = self._plugin_states.get(plugin_name, {})
        at_min = plugin_state.get("at_min", False)
        at_max = plugin_state.get("at_max", False)
        
        if delta < 0 and not at_min:
            logger.warning(f"Overflow event for dimming but plugin not at minimum")
            return
        if delta > 0 and not at_max:
            logger.warning(f"Overflow event for brightening but plugin not at maximum")
            return
        
        # Only cascade for DIMMING overflow (trying to go below 0%)
        # Brightening past 100% is a no-op - hardware is already at max
        if delta > 0:
            logger.debug(f"Ignoring brightening overflow (hardware at max, nothing to do)")
            return
        
        # Engage software dimmer on first overflow
        if not self._is_software_active:
            logger.info("Engaging software dimmer for extended brightness range")
            self._is_software_active = True
            self._software_dimmer_level = 100  # Start at no dimming
            self._state = CoordinatorState.CASCADED
        
        # Apply overflow delta to software dimmer
        await self._adjust_software_dimmer(delta)
        
        logger.info(
            f"Cascaded to software dimmer: level={self._software_dimmer_level}% "
            f"(state={self._state.value})"
        )
    
    def get_status(self) -> Dict:
        """
        Get current coordinator status for monitoring/debugging.
        
        Returns:
            Dictionary with coordinator state and statistics
        """
        return {
            "state": self._state.value,
            "software_dimmer_active": self._is_software_active,
            "software_dimmer_level": self._software_dimmer_level,
            "software_dimmer_type": self._software_dimmer_type,
            "unwinding_threshold": self._unwinding_threshold,
            "hardware_plugins": list(self._plugin_states.keys()),
            "plugin_states": self._plugin_states.copy()
        }
