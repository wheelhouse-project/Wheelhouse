"""Windows System Volume Control Plugin.

This plugin provides Windows system volume control for WheelHouse, enabling users
without Sonos speakers to use volume control features. It integrates with Windows 
Core Audio APIs via the pycaw library, providing immediate response volume control
through the same EventBus patterns used by other volume plugins.

Key Features:
  - Volume control via EventBus commands (up, down, set)
  - Windows system volume control via Core Audio APIs
  - Fast response time (no network latency)
  - Supports default audio device or specific device selection
  - Configurable volume step size and device selection
  - Robust error handling for device availability

Integration Points:
  - **Subscribes to:** `VolumeAdjustCommand` (from MouseHandler's volume zones;
    there is no shipped voice volume command)
  - **Configuration:** `[plugins.system_volume]` section in config.toml
  - **Hardware:** Any Windows audio device supported by Core Audio

Configuration Example:
  ```toml
  [plugins.system_volume]
  enabled = true
  device_type = "default"        # "default", "communications", or specific device name
  volume_step_db = 3.0          # dB increment per VolumeAdjustCommand delta
  min_volume_db = -65.25        # Minimum volume in dB (Windows default)
  max_volume_db = 0.0           # Maximum volume in dB (Windows default)
  ```

Plugin Behavior:
  - **Volume Control:** Responds immediately to VolumeAdjustCommand events
  - **Device Selection:** Uses Windows default audio device or configured device
  - **Range:** Windows volume range in dB (-65.25 to 0.0 typical)
  - **Error Handling:** Device errors don't crash plugin or core system
  - **Graceful Degradation:** If device unavailable, reports unhealthy but continues trying

Typical Flow:
  1. Plugin initializes, connects to Windows Core Audio
  2. start() subscribes to VolumeAdjustCommand events
  3. MouseHandler publishes VolumeAdjustCommand(delta=5)
  4. Plugin receives event, adjusts Windows system volume
  5. Volume change takes effect immediately (no network latency)

For Sonos speaker volume control, see `sonos_plugin.py`.
Plugins are mutually exclusive - enable only one volume plugin at a time.
"""
import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from comtypes import COMError, CLSCTX_ALL

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.events import VolumeAdjustCommand, PTTStartedEvent, PTTStoppedEvent
from services.wheelhouse.handlers.volume_router import get_volume_router

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.event_bus import EventBus


logger = logging.getLogger(__name__)


class SystemVolumePlugin(BasePlugin):
    """Windows system volume control plugin for WheelHouse.
    
    Provides system volume control via Windows Core Audio APIs for users without
    Sonos speakers or other network audio devices. Uses the pycaw library to
    interface with Windows volume controls, providing immediate response without
    network latency.
    
    Lifecycle:
      1. initialize() - Load config, validate audio device access
      2. start() - Subscribe to volume events, connect to audio device
      3. Running - Respond to volume commands via Core Audio APIs
      4. stop() - Cleanup audio device references
    
    Volume Control:
      Windows volume is controlled in dB scale (typically -65.25 to 0.0 dB).
      VolumeAdjustCommand delta is multiplied by volume_step_db to get dB change.
      Volume changes use asyncio.to_thread() for blocking COM operations.
    
    Device Selection:
      Supports default audio device, communications device, or specific device by name.
      Falls back to default device if specified device is not available.
    """
    
    def __init__(self):
        """Initialize plugin state."""
        super().__init__()
        self._config: Optional["ConfigService"] = None
        self._event_bus: Optional["EventBus"] = None
        self._device_type: str = "default"
        self._volume_step_db: float = 3.0
        self._min_volume_db: float = -65.25
        self._max_volume_db: float = 0.0
        self._volume_interface: Optional[IAudioEndpointVolume] = None
        self._device_name: Optional[str] = None
        self._last_error: Optional[str] = None
        self._pre_ptt_volume_db: Optional[float] = None

    @property
    def name(self) -> str:
        """Return unique plugin identifier."""
        return "system_volume"
    
    async def initialize(self, config: "ConfigService", event_bus: "EventBus") -> None:
        """Initialize System Volume plugin with configuration and event bus.
        
        Loads volume control configuration including device selection, step size,
        and volume range. Validates that Windows Core Audio is accessible but
        does NOT connect to audio device yet (that happens in start()).
        
        Args:
            config: ConfigService for reading plugin configuration
            event_bus: EventBus for command/event communication
        
        Raises:
            ValueError: If configuration is invalid
            ImportError: If pycaw library is not available
        """
        self._config = config
        self._event_bus = event_bus
        
        # Load configuration with defaults
        self._device_type = config.get("plugins.system_volume.device_type", "default")
        self._volume_step_db = config.get("plugins.system_volume.volume_step_db", 3.0)
        self._min_volume_db = config.get("plugins.system_volume.min_volume_db", -65.25)
        self._max_volume_db = config.get("plugins.system_volume.max_volume_db", 0.0)
        
        # Validate configuration
        if self._volume_step_db <= 0:
            raise ValueError("volume_step_db must be positive")
        
        if self._min_volume_db >= self._max_volume_db:
            raise ValueError("min_volume_db must be less than max_volume_db")
        
        if self._device_type not in ["default", "communications"]:
            # Assume it's a specific device name
            logger.info(f"Using specific audio device: {self._device_type}")
        
        # Test that we can access AudioUtilities (validates pycaw is working)
        try:
            await asyncio.to_thread(AudioUtilities.GetSpeakers)
        except Exception as e:
            raise ImportError(f"Cannot access Windows Core Audio APIs: {e}")
        
        self._state = PluginState.INITIALIZED
        logger.info(f"System Volume plugin initialized (device: {self._device_type}, step: {self._volume_step_db}dB)")
    
    async def start(self) -> None:
        """Start System Volume plugin operation.

        :flow: System Volume Control
        :step: 1
        :description: Subscribes to volume commands and connects to Windows Core Audio
        :data_in: ConfigService and EventBus references
        :data_out: Active VolumeAdjustCommand subscription and Core Audio interface
        :notes: Gets appropriate audio device (default, communications, or named) and retrieves IAudioEndpointVolume interface for volume control. When volume adjustment events received, plugin converts delta to dB scale and applies change via SetMasterVolumeLevel(). All COM operations wrapped in asyncio.to_thread() to prevent event loop blocking. Sets state to RUNNING on success, FAILED on error.

        """
        try:
            self._state = PluginState.STARTING
            
            # Only subscribe to volume commands if VolumeRouter selected system volume
            volume_router = get_volume_router()
            if volume_router.use_system_volume:
                if self._event_bus:
                    self._event_bus.subscribe(VolumeAdjustCommand, self._handle_volume_adjust)
                logger.info("SystemVolumePlugin: Handling volume control (VolumeRouter selected System Volume)")
            else:
                # Still connect to audio device for potential future use, but don't handle volume
                logger.info("SystemVolumePlugin: Volume disabled (VolumeRouter selected Sonos)")

            # Always subscribe to PTT events for audio muting
            if self._event_bus:
                self._event_bus.subscribe(PTTStartedEvent, self._handle_ptt_started)
                self._event_bus.subscribe(PTTStoppedEvent, self._handle_ptt_stopped)

            # Connect to audio device
            await self._connect_audio_device()
            
            self._state = PluginState.RUNNING
            logger.info(f"System Volume plugin started successfully (device: {self._device_name})")
            
        except Exception as e:
            logger.error(f"Failed to start System Volume plugin: {e}", exc_info=True)
            self._state = PluginState.FAILED
            self._last_error = str(e)
    
    async def stop(self) -> None:
        """Stop System Volume plugin and clean up resources.
        
        Releases audio device interface and sets state to STOPPED.
        """
        self._state = PluginState.STOPPING
        
        # Release audio device interface
        self._volume_interface = None
        self._device_name = None
        
        self._state = PluginState.STOPPED
        logger.info("System Volume plugin stopped")
    
    def get_health_status(self) -> dict:
        """Return System Volume plugin health status.
        
        Returns:
            dict: Health status with audio device state and error info
        """
        status = "healthy" if self._state == PluginState.RUNNING and self._volume_interface else "unhealthy"
        
        # More detailed status based on interface availability
        if self._state == PluginState.RUNNING:
            if self._volume_interface is not None:
                status = "healthy"
            elif self._last_error:
                status = "unhealthy"
            else:
                status = "degraded"  # Running but no interface
        
        return {
            "status": status,
            "state": self._state.value,
            "device_type": self._device_type,
            "device_name": self._device_name,
            "volume_step_db": self._volume_step_db,
            "connected": self._volume_interface is not None,
            "error": self._last_error
        }
    
    async def _connect_audio_device(self) -> None:
        """Connect to Windows Core Audio device.
        
        Gets the appropriate audio endpoint based on device_type configuration
        and retrieves the IAudioEndpointVolume interface for volume control.
        
        Raises:
            Exception: If audio device cannot be accessed
        """
        try:
            # Get audio device based on configuration
            device = await asyncio.to_thread(self._get_audio_device)
            
            if device is None:
                raise RuntimeError(f"Cannot find audio device: {self._device_type}")
            
            # Get volume interface
            self._volume_interface = await asyncio.to_thread(
                lambda: device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
            )
            
            # Get device name for status reporting (simplified approach)
            self._device_name = await asyncio.to_thread(
                lambda: "Windows Default Audio Device"  # Simplified - device name detection is complex
            )
            
            self._last_error = None
            logger.info(f"Connected to audio device: {self._device_name}")
            
        except Exception as e:
            self._last_error = f"Audio device connection error: {e}"
            logger.error(f"Error connecting to audio device: {e}")
            raise
    
    def _get_audio_device(self):
        """Get audio device based on configuration.
        
        This runs in a thread pool via asyncio.to_thread().
        
        Returns:
            Audio device endpoint or None if not found
        """
        try:
            if self._device_type == "default":
                return AudioUtilities.GetSpeakers()
            elif self._device_type == "communications":
                # Get communications device (different from default)
                devices = AudioUtilities.GetAllDevices()
                for device in devices:
                    # Look for communications device role
                    # This is a simplified approach - in practice might need more specific role checking
                    if "communications" in device.FriendlyName.lower():
                        return device
                # Fall back to default if no communications device found
                logger.warning("Communications device not found, falling back to default")
                return AudioUtilities.GetSpeakers()
            else:
                # Specific device by name
                devices = AudioUtilities.GetAllDevices()
                for device in devices:
                    if device.FriendlyName.lower() == self._device_type.lower():
                        return device
                # Fall back to default if named device not found
                logger.warning(f"Device '{self._device_type}' not found, falling back to default")
                return AudioUtilities.GetSpeakers()
                
        except Exception as e:
            logger.error(f"Error getting audio device: {e}")
            return None
    
    async def _handle_volume_adjust(self, event: VolumeAdjustCommand) -> None:
        """Handle volume adjustment command from EventBus.

        :flow: System Volume Control
        :step: 2
        :description: Receives volume command and adjusts Windows system volume
        :data_in: VolumeAdjustCommand (delta: positive=louder, negative=quieter)
        :data_out: Adjusted Windows system volume via Core Audio API
        :notes: Converts integer delta to dB scale by multiplying by volume_step_db config value. Gets current Windows volume via GetMasterVolumeLevel(), applies dB delta (clamped to min/max range), sets new volume via SetMasterVolumeLevel(). All Core Audio COM operations wrapped in asyncio.to_thread() to avoid blocking event loop. Volume changes take effect immediately with no network latency.
        """
        
        """Handle VolumeAdjustCommand to change system volume.
        Args:
            event: VolumeAdjustCommand with delta attribute
        """
        if not self._volume_interface:
            logger.warning("Cannot adjust volume - audio device not connected")
            return
        
        delta = event.delta
        delta_db = delta * self._volume_step_db
        
        try:
            # Get current volume level (in dB)
            current_db = await asyncio.to_thread(self._volume_interface.GetMasterVolumeLevel)
            new_db = current_db + delta_db
            
            # Clamp to valid range
            new_db = max(self._min_volume_db, min(self._max_volume_db, new_db))
            
            if abs(new_db - current_db) < 0.1:  # Essentially no change
                logger.debug(f"System volume already at limit ({current_db:.1f}dB)")
                return
            
            # Set new volume level
            await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, new_db, None)
            logger.info(f"Adjusted system volume: {current_db:.1f}dB → {new_db:.1f}dB (delta: {delta_db:+.1f}dB)")
            
            self._last_error = None
            
        except (COMError, AttributeError) as e:
            self._last_error = f"Volume adjust error: {e}"
            logger.error(f"Error adjusting system volume: {e}")
            
            # Try to reconnect audio device on COM error
            if isinstance(e, COMError):
                logger.info("Attempting to reconnect audio device after COM error")
                try:
                    await self._connect_audio_device()
                except Exception as reconnect_error:
                    logger.error(f"Failed to reconnect audio device: {reconnect_error}")
                    
        except Exception as e:
            self._last_error = f"Unexpected error: {e}"
            logger.error(f"Unexpected error adjusting system volume: {e}", exc_info=True)

    async def _handle_ptt_started(self, event: PTTStartedEvent) -> None:
        """Mute system audio when push-to-talk begins."""
        if not self._volume_interface:
            logger.warning("Cannot mute for PTT -- audio device not connected")
            return

        try:
            current_db = await asyncio.to_thread(self._volume_interface.GetMasterVolumeLevel)
            self._pre_ptt_volume_db = current_db
            await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, self._min_volume_db, None)
            logger.info(f"[PTT] System audio muted for push-to-talk (was {current_db:.1f}dB)")
        except Exception as e:
            logger.error(f"[PTT] Failed to mute system audio: {e}")

    async def _handle_ptt_stopped(self, event: PTTStoppedEvent) -> None:
        """Restore system audio when push-to-talk ends."""
        if self._pre_ptt_volume_db is None:
            return  # Nothing to restore

        if not self._volume_interface:
            logger.warning("Cannot restore volume after PTT -- audio device not connected")
            self._pre_ptt_volume_db = None
            return

        try:
            restore_db = self._pre_ptt_volume_db
            self._pre_ptt_volume_db = None
            await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, restore_db, None)
            logger.info(f"[PTT] System audio restored to {restore_db:.1f}dB")
        except Exception as e:
            logger.error(f"[PTT] Failed to restore system audio: {e}")