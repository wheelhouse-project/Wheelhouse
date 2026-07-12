"""Sonos speaker system integration plugin.

This plugin provides Sonos speaker control and monitoring for WheelHouse, combining
volume control functionality with playback state monitoring for speech suppression.
It integrates the Sonos speaker system via the SoCo library, enabling voice and
mouse-based volume control while coordinating with speech recognition to prevent
interference during music playback.

Key Features:
  - Volume control via EventBus commands (up, down, set)
  - Playback state monitoring with configurable polling
  - Speech recognition suppression during music playback
  - Automatic device discovery by IP or name
  - Robust error handling for network connectivity issues
  - Line-in source detection (computer audio vs. streaming)

Integration Points:
  - **Subscribes to:** `VolumeAdjustCommand` (from MouseHandler, speech commands)
  - **Publishes:** `SonosStateChangedEvent` (for speech suppression coordination)
  - **Configuration:** `[plugins.sonos]` section in config.toml
  - **Hardware:** Sonos speaker accessible via network

Configuration Example:
  ```toml
  [plugins.sonos]
  enabled = true
  speaker_ip = "192.168.1.100"  # Or speaker name like "Office"
  polling_interval = 2           # Seconds between playback state checks
  ```

Plugin Behavior:
  - **Volume Control:** Responds immediately to VolumeAdjustCommand events
  - **Monitoring:** Polls Sonos every N seconds for playback state
  - **Speech Suppression:** Publishes event when music starts/stops (excluding line-in)
  - **Error Handling:** Network errors don't crash plugin or core system
  - **Graceful Degradation:** If Sonos unreachable, reports unhealthy but continues trying

Typical Flow:
  1. Plugin initializes, loads Sonos IP from config
  2. start() begins monitoring task (background polling)
  3. MouseHandler publishes VolumeAdjustCommand(delta=5)
  4. Plugin receives event, adjusts Sonos volume
  5. Monitoring detects music playing, publishes SonosStateChangedEvent
  6. Speech system receives event, suppresses recognition

For alternative volume control (users without Sonos), see `system_volume_plugin.py`.
"""
import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

import requests.exceptions

from soco import SoCo
import soco.config
import soco.discovery
from soco.exceptions import SoCoException, SoCoUPnPException

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.events import SonosStateChangedEvent, VolumeAdjustCommand
from services.wheelhouse.handlers.volume_router import get_volume_router

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus


logger = logging.getLogger(__name__)


class SonosPlugin(BasePlugin):
    """Sonos speaker integration plugin for WheelHouse.
    
    Provides volume control and playback monitoring for Sonos speakers. Combines
    the functionality of the legacy `sonos_control.py` and `sonos_monitor.py` modules
    into a single, self-contained plugin that integrates via EventBus.
    
    Lifecycle:
      1. initialize() - Load config, validate Sonos IP/name
      2. start() - Subscribe to volume events, start monitoring task
      3. Running - Respond to volume commands, monitor playback state
      4. stop() - Cancel monitoring, cleanup resources
    
    Volume Control:
      Sonos volume range is 0-100. Commands clamp to this range automatically.
      Volume changes use asyncio.to_thread() for blocking SoCo network calls.
    
    Speech Suppression:
      Monitors playback state and publishes SonosStateChangedEvent when music
      starts/stops. Excludes computer line-in (URI: x-rincon-stream:) to allow
      speech recognition during computer audio playback.
    """
    
    def __init__(self):
        """Initialize plugin state."""
        super().__init__()
        self._config: Optional["ConfigService"] = None
        self._event_bus: Optional["EventBus"] = None
        self._speaker_ip: Optional[str] = None
        self._polling_interval: int = 2
        self._monitor_task: Optional[asyncio.Task] = None
        self._player: Optional[SoCo] = None
        self._previous_suppression_state: Optional[bool] = None
        self._previously_reachable: Optional[bool] = None
        self._last_error: Optional[str] = None
    
    @property
    def name(self) -> str:
        """Return unique plugin identifier."""
        return "sonos"
    
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize Sonos plugin with configuration and event bus.
        
        Gets speaker IP from VolumeRouter (auto-discovered) or falls back to config.
        If no Sonos available, plugin disables itself gracefully (no error).
        
        Args:
            config: ConfigService for reading plugin configuration
            event_bus: EventBus for command/event communication
        """
        self._config = config
        self._event_bus = event_bus

        # Fail fast when the speaker is unreachable: SoCo's default request
        # timeout is 20s per HTTP call, which stalls every 2s poll for 20s
        # when the speaker drops off the network (wh-1f9c). (connect, read)
        # pair: connect fails fast; read still tolerates a slow-but-alive
        # speaker. Module-global, so it applies to every SoCo call in this
        # process.
        connect_timeout = config.get("plugins.sonos.request_connect_timeout", 2.0)
        read_timeout = config.get("plugins.sonos.request_read_timeout", 5.0)
        soco.config.REQUEST_TIMEOUT = (float(connect_timeout), float(read_timeout))

        # Get speaker IP from VolumeRouter (auto-discovered) or config fallback
        volume_router = get_volume_router()
        if volume_router.sonos_ip:
            self._speaker_ip = volume_router.sonos_ip
            self._sonos_available = True
            logger.info(f"Using auto-discovered Sonos: {volume_router.sonos_name} @ {self._speaker_ip}")
        else:
            # Fallback to config for manual override
            self._speaker_ip = config.get("plugins.sonos.speaker_ip")
            if self._speaker_ip:
                self._sonos_available = True
                logger.info(f"Using configured Sonos IP: {self._speaker_ip}")
            else:
                # No Sonos available - plugin will be disabled but not crash
                self._sonos_available = False
                self._speaker_ip = None
                logger.info("SonosPlugin: No Sonos found - plugin disabled")
        
        self._polling_interval = config.get("plugins.sonos.polling_interval", 2)
        
        self._state = PluginState.INITIALIZED
        if self._sonos_available:
            logger.info(f"Sonos plugin initialized for speaker: {self._speaker_ip} (poll interval: {self._polling_interval}s)")
    
    async def start(self) -> None:
        """Start Sonos plugin operation.

        :flow: Speech Suppression by Sonos
        :step: 1
        :description: Subscribes to volume commands and starts Sonos playback monitoring
        :data_in: ConfigService (polling_interval, speaker_ip), EventBus
        :data_out: Active VolumeAdjustCommand subscription and monitoring task
        :notes: Plugin initialization for dual functionality: (1) Volume control via VolumeAdjustCommand subscription (see High-Resolution Mouse Input flow step 5 for implementation), (2) Speech suppression via background monitoring task. Monitoring task (step 2) polls Sonos every N seconds (configurable polling_interval) to detect music playback, publishes SonosStateChangedEvent consumed by StateManager for speech suppression coordination. Sets state to RUNNING on success, FAILED on error with logged details.
        
        Subscribes to volume adjustment events and starts background monitoring task.
        Sets state to RUNNING on success, FAILED on error.
        """
        try:
            self._state = PluginState.STARTING
            
            # Skip all operations if no Sonos available
            if not getattr(self, '_sonos_available', False):
                self._state = PluginState.RUNNING
                logger.info("SonosPlugin: No Sonos available - running in disabled mode")
                return
            
            # Only subscribe to volume commands if VolumeRouter selected Sonos
            volume_router = get_volume_router()
            if volume_router.use_sonos:
                if self._event_bus:
                    self._event_bus.subscribe(VolumeAdjustCommand, self._handle_volume_adjust)
                logger.info("SonosPlugin: Handling volume control (VolumeRouter selected Sonos)")
            else:
                logger.info("SonosPlugin: Volume disabled (VolumeRouter selected System Volume)")
            
            # Always start playback monitoring for speech suppression (when Sonos available)
            self._monitor_task = asyncio.create_task(self._monitor_playback())
            
            self._state = PluginState.RUNNING
            logger.info("Sonos plugin started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start Sonos plugin: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    async def stop(self) -> None:
        """Stop Sonos plugin and clean up resources.
        
        Cancels monitoring task and sets state to STOPPED.
        """
        self._state = PluginState.STOPPING
        
        # Cancel monitoring task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        self._state = PluginState.STOPPED
        logger.info("Sonos plugin stopped")
    
    def get_health_status(self) -> dict:
        """Return Sonos plugin health status.
        
        Returns:
            dict: Health status with connection state and error info
        """
        status = "healthy" if self._state == PluginState.RUNNING else "unhealthy"
        
        # Check if monitoring task is still alive
        monitor_task_alive = self._monitor_task and not self._monitor_task.done()
        
        # If we've successfully connected, status is better
        if self._player is not None and self._state == PluginState.RUNNING and monitor_task_alive:
            status = "healthy"
        elif self._state == PluginState.RUNNING and not monitor_task_alive:
            status = "unhealthy"  # Task died unexpectedly!
            self._last_error = "Monitoring task terminated unexpectedly"
            logger.error("Sonos monitoring task is not running! This should not happen.")
        elif self._state == PluginState.RUNNING and self._last_error:
            status = "degraded"  # Running but having issues
        
        return {
            "status": status,
            "state": self._state.value,
            "speaker_ip": self._speaker_ip,
            "polling_interval": self._polling_interval,
            "connected": self._player is not None,
            "monitor_task_alive": monitor_task_alive,
            "error": self._last_error
        }
    
    async def _get_player(self) -> Optional[SoCo]:
        """Get SoCo player object by IP or name.
        
        Attempts to connect to Sonos speaker via IP address or device name.
        Uses asyncio.to_thread() to avoid blocking the event loop.
        
        Returns:
            SoCo object if connection successful, None otherwise
        """
        try:
            # Run in thread to avoid blocking
            player = await asyncio.to_thread(self._get_player_sync)
            if player:
                self._last_error = None
            return player
        except Exception as e:
            self._last_error = f"Connection error: {e}"
            logger.error(f"Error getting Sonos player: {e}")
            return None
    
    def _get_player_sync(self) -> Optional[SoCo]:
        """Synchronous helper to get SoCo player.
        
        This runs in a thread pool via asyncio.to_thread().
        """
        identifier_str = str(self._speaker_ip)
        
        # Check if it looks like an IP address
        is_ip_like = all(c in "0123456789." for c in identifier_str) and "." in identifier_str
        
        try:
            if is_ip_like:
                # logger.debug(f"Connecting to Sonos by IP: {identifier_str}")  # Too noisy for polling
                player = SoCo(identifier_str)
            else:
                logger.debug(f"Discovering Sonos by name: {identifier_str}")
                player = soco.discovery.by_name(identifier_str)
            
            # Only log connection on first successful connect or errors
            # if player:
            #     logger.debug(f"Connected to Sonos: {player.player_name} ({player.ip_address})")
            return player
            
        except (SoCoException, SoCoUPnPException, ConnectionRefusedError, TypeError) as e:
            logger.error(f"Error connecting to Sonos '{identifier_str}': {e}")
            return None
    
    async def _handle_volume_adjust(self, event: VolumeAdjustCommand) -> None:
        """Handle volume adjustment command from EventBus.
        
        :flow: High-Resolution Mouse Input
        :step: 5
        :description: Sonos Volume Control Implementation - Adjusts Sonos speaker volume via network API
        :consumes_from: High-Resolution Mouse Input
        :data_in: VolumeAdjustCommand event from EventBus step 4B (delta: positive=louder, negative=quieter)
        :data_out: Sonos speaker volume adjusted via SoCo library, networked to physical speaker
        :notes: Complete Sonos volume control implementation consuming VolumeAdjustCommand from mouse handler (step 4B). Retrieves current volume from Sonos speaker via SoCo library, applies delta (clamped 0-100), sets new volume over network. All SoCo network calls wrapped in asyncio.to_thread() to prevent event loop blocking. Volume changes may have network latency (typically <100ms). If speaker unavailable, logs warning and returns gracefully without disrupting other services. This is the primary volume control path for Sonos users - alternative is SystemVolumePlugin for Windows Core Audio. Note: SonosPlugin also handles speech suppression monitoring (separate Speech Suppression by Sonos flow) via independent _monitor_playback() task.
        
        Args:
            event: VolumeAdjustCommand with delta attribute
        """
        delta = event.delta
        
        try:
            player = await self._get_player()
            if not player:
                logger.warning("Cannot adjust volume - Sonos player not available")
                return
            
            # Get current volume and calculate new volume
            current_volume = await asyncio.to_thread(lambda: player.volume)
            new_volume = current_volume + delta
            
            # Clamp to valid range (0-100)
            new_volume = max(0, min(100, new_volume))
            
            if new_volume == current_volume and delta != 0:
                logger.info(f"Sonos volume already at limit ({current_volume})")
                return
            
            # Set new volume
            await asyncio.to_thread(lambda: setattr(player, 'volume', new_volume))
            logger.info(f"Adjusted Sonos volume: {current_volume} → {new_volume} (delta: {delta})")
            
            self._last_error = None
            
        except (SoCoException, SoCoUPnPException) as e:
            self._last_error = f"Volume adjust error: {e}"
            logger.error(f"Error adjusting Sonos volume: {e}")
        except Exception as e:
            self._last_error = f"Unexpected error: {e}"
            logger.error(f"Unexpected error adjusting Sonos volume: {e}", exc_info=True)
    
    def _note_speaker_unreachable(self, detail: str) -> None:
        """Log the speaker becoming unreachable once at WARNING, then keep
        subsequent identical failures at DEBUG so a powered-off speaker does
        not fill the log with an error every poll (wh-1f9c)."""
        if self._previously_reachable is not False:
            logger.warning(
                f"Sonos speaker unreachable: {detail} "
                f"(further failures logged at DEBUG until it recovers)"
            )
        else:
            logger.debug(f"Sonos speaker still unreachable: {detail}")
        self._previously_reachable = False

    def _note_speaker_reachable(self) -> None:
        """Log recovery once at INFO after an unreachable stretch."""
        if self._previously_reachable is False:
            logger.info("Sonos speaker reachable again")
        self._previously_reachable = True

    async def _monitor_playback(self) -> None:
        """Monitor Sonos playback state for speech suppression.

        :flow: Speech Suppression by Sonos
        :step: 2
        :description: Polls Sonos speaker for playback state and filters for music vs local audio
        :data_in: Sonos state via SoCo (transport_info, current_track_info)
        :data_out: SonosStateChangedEvent published every poll (heartbeat pattern)
        :notes: Background monitoring loop polling every N seconds (configurable polling_interval). Checks playback state (PLAYING/STOPPED) and track URI to distinguish music from local audio. Local audio sources (computer line-in, TV HDMI ARC) use URI prefixes x-rincon-stream:, x-sonos-htastream:, x-rincon: and don't suppress speech. Only music/streaming services trigger suppression. Continuous heartbeat publishes every poll (not just on changes) for robust state sync with <2s recovery from desync. StateManager deduplicates, only notifying on actual transitions. SoCo calls wrapped in asyncio.to_thread() to avoid blocking.
        
        Continuously polls Sonos speaker for playback state and publishes
        SonosStateChangedEvent when state changes.
        """
        if not self._speaker_ip:
            logger.warning("Sonos speaker IP not configured, monitoring disabled")
            return
        
        logger.info(f"Starting Sonos playback monitoring (interval: {self._polling_interval}s)")
        
        while self._state == PluginState.RUNNING:
            try:
                player = await self._get_player()
                
                if player:
                    self._player = player
                    
                    # Get playback info (in thread to avoid blocking)
                    track_info = await asyncio.to_thread(player.get_current_track_info)
                    transport_info = await asyncio.to_thread(player.get_current_transport_info)
                    self._note_speaker_reachable()
                    
                    is_playing = transport_info.get('current_transport_state') == 'PLAYING'
                    track_uri = track_info.get('uri', '')
                    
                    # Allow speech for all local audio sources (computer, TV, etc.)
                    # Only suppress for streaming music services
                    is_local_audio = (
                        track_uri.startswith('x-rincon-stream:') or      # Computer line-in
                        track_uri.startswith('x-sonos-htastream:') or    # TV audio (SPDIF/HDMI ARC)
                        track_uri.startswith('x-rincon:')                # Other local Sonos sources
                    )
                    is_suppressed = is_playing and not is_local_audio
                    
                    """:flow: Speech Suppression by Sonos
                    :step: 3
                    :description: Publishes state event every poll for continuous heartbeat synchronization
                    :data_in: Boolean is_suppressed from current poll
                    :data_out: SonosStateChangedEvent published to EventBus
                    :notes: Event publishing with heartbeat pattern - publishes EVERY poll (every 2s) regardless of state changes. Ensures StateManager always reflects ground truth with <2s recovery from desync (user toggles, network glitches). StateManager deduplicates events (step 4), only notifying GUI on actual transitions, so continuous publishing is harmless. Logs only on state changes to reduce noise. If player not found, publishes is_playing=False to clear suppression.
                    """
                    # Always publish on every poll - heartbeat pattern for state synchronization
                    # Log only when state actually changes to reduce log noise
                    if is_suppressed != self._previous_suppression_state:
                        logger.debug(f"Sonos state changed: {'suppressed' if is_suppressed else 'not suppressed'}")
                        self._previous_suppression_state = is_suppressed
                    
                    if self._event_bus:
                        await self._event_bus.publish(SonosStateChangedEvent(is_playing=is_suppressed))
                    self._last_error = None
                else:
                    # Player not found - ensure suppression is off
                    if self._previous_suppression_state is not False:
                        if self._event_bus:
                            await self._event_bus.publish(SonosStateChangedEvent(is_playing=False))
                        self._previous_suppression_state = False
                    self._note_speaker_unreachable(
                        f"player not found, retrying in {self._polling_interval}s"
                    )
                    
            except (
                SoCoException,
                ConnectionError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as e:
                self._last_error = f"Monitoring error: {e}"
                self._note_speaker_unreachable(str(e))
                # Ensure suppression is off on error
                if self._previous_suppression_state is not False:
                    if self._event_bus:
                        await self._event_bus.publish(SonosStateChangedEvent(is_playing=False))
                    self._previous_suppression_state = False
                    
            except asyncio.CancelledError:
                # Task being cancelled - clean up suppression state
                logger.info("Sonos monitoring task cancelled, clearing suppression")
                if self._previous_suppression_state is not False:
                    if self._event_bus:
                        await self._event_bus.publish(SonosStateChangedEvent(is_playing=False))
                    self._previous_suppression_state = False
                raise  # Re-raise to exit properly
                    
            except Exception as e:
                self._last_error = f"Unexpected monitoring error: {e}"
                logger.error(f"Unexpected error in Sonos monitoring: {e}", exc_info=True)
                if self._previous_suppression_state is not False:
                    if self._event_bus:
                        await self._event_bus.publish(SonosStateChangedEvent(is_playing=False))
                    self._previous_suppression_state = False
            
            # Wait before next poll
            try:
                await asyncio.sleep(self._polling_interval)
            except asyncio.CancelledError:
                logger.info("Sonos monitoring task cancelled during sleep")
                raise  # Exit cleanly
