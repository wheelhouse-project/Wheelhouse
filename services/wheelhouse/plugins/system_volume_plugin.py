"""Windows System Volume Control Plugin.

This plugin provides Windows system volume control for WheelHouse, enabling users
without Sonos speakers to use volume control features. It integrates with Windows 
Core Audio APIs via the pycaw library, providing immediate response volume control
through the same EventBus patterns used by other volume plugins.

Key Features:
  - Volume control via EventBus commands (up, down, set)
  - Windows system volume control via Core Audio APIs
  - Fast response time (no network latency)
  - Supports the Windows default audio device or the communications device
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
  device_type = "default"        # "default" or "communications"
  volume_step_db = 3.0          # dB increment per VolumeAdjustCommand delta
  min_volume_db = -65.25        # Minimum volume in dB (Windows default)
  max_volume_db = 0.0           # Maximum volume in dB (Windows default)
  ```

Plugin Behavior:
  - **Volume Control:** Responds immediately to VolumeAdjustCommand events
  - **Device Selection:** Uses the Windows default audio device, or the
    communications device when device_type says so
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

from pycaw.pycaw import (
    AudioUtilities,
    EDataFlow,
    ERole,
    IAudioEndpointVolume,
)
from comtypes import COMError, CLSCTX_ALL

from services.wheelhouse.plugins.base import BasePlugin, PluginState
from services.wheelhouse.events import (
    VolumeAdjustCommand, PTTStartedEvent, PTTStoppedEvent, PTTMuteStateEvent,
    SystemConfigurationErrorEvent,
)
from services.wheelhouse.handlers.volume_router import get_volume_router
from services.wheelhouse.utils import ptt_volume_record

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
      device_type takes one of two values: "default" or "communications".
      A device name is reported and ignored, and the Windows default device
      is used instead; names have never worked (wh-named-device-doc-removal).
      The communications device is the endpoint Windows itself records for
      the communications role, asked for by role rather than found by name;
      it falls back to the default device on a machine that has none
      (wh-named-device-doc-removal.1.3).
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
        self._audio_device_id: Optional[str] = None
        self._device_name: Optional[str] = None
        self._last_error: Optional[str] = None
        self._pre_ptt_volume_db: Optional[float] = None
        # Which configured-device refusals the user has already been told
        # about. Cleared by the next successful mute, so a fault that comes
        # back is reported again (wh-codex-merge-audit.6.1.4).
        self._ptt_endpoint_errors_reported: set[str] = set()
        # Turning the speakers down and putting them back up are two Core
        # Audio calls each, run in worker threads, and StateManager only
        # schedules the events that start them. Without this lock a release
        # can pass a press: a stop that runs before the mute recorded the old
        # level finds nothing to restore and leaves the speakers silent, and a
        # slow restore for one hold can finish after the next hold has muted,
        # turning the speakers back up while that hold is still listening to
        # them (wh-ptt-audio-override.1.4). asyncio wakes waiters in the order
        # they arrived, so this also keeps one hold's restore ahead of the
        # next hold's mute.
        self._ptt_audio_lock = asyncio.Lock()

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
            # A device name has never worked: see the comment above
            # _get_audio_device. David chose on 2026-08-27 to drop the
            # setting rather than fix it, so a name is reported and the
            # default device is used, which leaves the user with working
            # volume control (wh-named-device-doc-removal).
            ignored_device_type = self._device_type
            logger.warning(
                "Audio device names are not supported; "
                f"device_type='{ignored_device_type}' will be ignored and the "
                "Windows default device used. Set device_type to 'default' "
                "or 'communications' to silence this message."
            )
            self._device_type = "default"
            # The log line above reaches a file this audience does not
            # open, so on its own it leaves volume control silently
            # driving a different device from the configured one. This
            # event is the project's channel for a configuration value
            # rejected during initialization: StateManager subscribes to
            # it in its constructor, which runs before the plugin
            # registry starts, and bridges it to a Windows notice.
            # AudioMonitor publishes the same event from its own
            # initialize for the same class of problem
            # (wh-named-device-doc-removal.1.2).
            await event_bus.publish(SystemConfigurationErrorEvent(
                service_name="SystemVolumePlugin",
                error_message=(
                    f"Audio device name device_type='{ignored_device_type}' "
                    "is not supported and was ignored. Volume control is "
                    "using the Windows default device."
                ),
                user_action=(
                    "Set plugins.system_volume.device_type in config.toml "
                    "to 'default' or 'communications'."
                ),
            ))
        
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
        :notes: Gets the audio device device_type asks for (default or communications) and retrieves IAudioEndpointVolume interface for volume control. When volume adjustment events received, plugin converts delta to dB scale and applies change via SetMasterVolumeLevel(). All COM operations wrapped in asyncio.to_thread() to prevent event loop blocking. Sets state to RUNNING on success, FAILED on error.

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
        self._audio_device_id = None
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
        self._audio_device_id = None
        try:
            # Get audio device based on configuration
            device = await asyncio.to_thread(self._get_audio_device)
            
            if device is None:
                raise RuntimeError(f"Cannot find audio device: {self._device_type}")
            
            # Get volume interface
            self._volume_interface = await asyncio.to_thread(
                lambda: device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
            )

            # Bind identity to THIS cached volume interface, not a later
            # role lookup that may select a different device. Identity is
            # required for PTT's audio override, but not for volume control.
            try:
                device_id = await asyncio.to_thread(device.GetId)
                self._audio_device_id = device_id if isinstance(device_id, str) and device_id else None
            except Exception as identity_error:
                logger.warning("Cannot verify PTT audio endpoint identity: %s", identity_error)
            
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
        # Every branch returns a raw IMMDevice, which is what
        # _connect_audio_device's device.Activate() needs. The
        # communications branch used to return an
        # AudioUtilities.GetAllDevices() item instead -- a pycaw AudioDevice
        # wrapper carrying only FriendlyName and EndpointVolume -- so
        # Activate raised AttributeError and the plugin ended in
        # PluginState.FAILED for any user with "communications" in some
        # device's FriendlyName. David ruled on 2026-08-27 to fix it rather
        # than drop the setting (wh-named-device-doc-removal.1.3).
        #
        # The FriendlyName scan was the wrong question as well as the wrong
        # return type. Windows records which endpoint serves communications
        # as a ROLE on the endpoint, so it is asked for by role, the same
        # call AudioUtilities.GetSpeakers() makes with ERole.eMultimedia. A
        # device merely named "Communications Headset" that Windows has not
        # been told to use for communications is a different device, and a
        # user whose real communications endpoint is called something else
        # never reached this branch at all.
        #
        # A device by NAME is a separate matter and stays unsupported:
        # initialize rewrites a name to "default" and reports it, because a
        # name has no role to ask Windows for (wh-named-device-doc-removal).
        try:
            if self._device_type == "communications":
                enumerator = AudioUtilities.GetDeviceEnumerator()
                try:
                    return enumerator.GetDefaultAudioEndpoint(
                        EDataFlow.eRender.value, ERole.eCommunications.value
                    )
                except Exception as e:
                    # A machine with no communications endpoint raises rather
                    # than returning nothing, so the fallback the old scan had
                    # moves here. Volume control keeps working on the default
                    # device instead of the plugin failing.
                    logger.warning(
                        "Communications device not available (%s), "
                        "falling back to the default device", e
                    )
                    return AudioUtilities.GetSpeakers()
            # "default", and anything else: initialize rewrites an
            # unrecognised device_type to "default", so the second case is
            # only reached if the attribute was set directly.
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

        This is the other writer of SetMasterVolumeLevel, so it takes the same
        lock the push-to-talk mute and restore take. Without it a wheel turn
        could raise the very endpoint the mute lowered while a hold was still
        listening to it (wh-ptt-audio-override.1.7).

        Args:
            event: VolumeAdjustCommand with delta attribute
        """
        if not self._volume_interface:
            logger.warning("Cannot adjust volume - audio device not connected")
            return

        delta = event.delta
        delta_db = delta * self._volume_step_db

        async with self._ptt_audio_lock:
            if self._pre_ptt_volume_db is not None:
                # A push-to-talk hold has the speakers turned down, and the
                # hold ignores audio suppression only while they stay down.
                # The change therefore must not reach the device now. It goes
                # to the level the release will restore, which keeps the
                # user's wheel turn instead of throwing it away
                # (wh-ptt-audio-override.1.7).
                held_db = self._pre_ptt_volume_db
                new_db = max(self._min_volume_db, min(self._max_volume_db, held_db + delta_db))

                if abs(new_db - held_db) < 0.1:  # Essentially no change
                    logger.debug(f"System volume already at limit ({held_db:.1f}dB)")
                    return

                self._pre_ptt_volume_db = new_db
                # The copy on disk follows, so a restore after a lost process
                # puts back the level the release would have put back
                # (wh-ptt-mute-orphaned-on-process-loss).
                await self._write_ptt_volume_record(new_db)
                logger.info(
                    f"[PTT] Volume change held until the hold ends: "
                    f"{held_db:.1f}dB → {new_db:.1f}dB (delta: {delta_db:+.1f}dB)"
                )
                return

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
        """Mute system audio when push-to-talk begins.

        Publishes the outcome, because StateManager lets the hold ignore audio
        suppression only while the speakers are known to be silenced
        (wh-ptt-audio-override). Every path out of this method reports. The
        report goes out after the lock is released, so an event bus subscriber
        cannot hold up the next hold's mute.
        """
        muted, reason = await self._mute_for_ptt()
        await self._publish_ptt_mute_state(muted, reason, event.hold_id)
        await self._report_ptt_endpoint_configuration_error(muted, reason)

    async def _mute_for_ptt(self) -> tuple[bool, str]:
        """Turn the speakers down for a hold, and say whether it worked."""
        async with self._ptt_audio_lock:
            if not self._volume_interface:
                logger.warning("Cannot mute for PTT -- audio device not connected")
                return False, "no_device"

            try:
                endpoint_confirmed = True
                metered_id = await self._metered_endpoint_id()
                if metered_id is None or metered_id != self._audio_device_id:
                    # Windows moves the default render endpoint under a running
                    # plugin whenever a headset, a Bluetooth device or an HDMI
                    # display arrives, and nothing else here follows it:
                    # start() and the volume COMError branch are the only other
                    # callers of _connect_audio_device, and a moved default
                    # raises no COMError. Re-resolving once on the press is
                    # what stops a refusal lasting the whole session
                    # (wh-codex-merge-audit.6.1.5).
                    # crewcut: this costs one Activate and one GetId per
                    # refused hold. Registering an IMMNotificationClient and
                    # reconnecting from OnDefaultDeviceChanged would move that
                    # cost off the press.
                    #
                    # A re-resolve that fails, or that resolves an endpoint
                    # whose identity it cannot read, keeps the identity on the
                    # interface it did not replace, so the next press still
                    # has it (wh-codex-merge-audit.6.2.1). It has not told
                    # this press WHICH endpoint that interface controls now,
                    # so the press writes no volume level and reports the
                    # failed re-resolve rather than a mismatch a device_type =
                    # "default" user cannot correct. An interface the
                    # re-resolve replaced before it failed never inherits the
                    # previous identity (wh-codex-merge-audit.6.2.3).
                    previous_id = self._audio_device_id
                    previous_interface = self._volume_interface
                    try:
                        await self._connect_audio_device()
                    except Exception as reconnect_error:
                        logger.warning("[PTT] Cannot re-resolve the audio endpoint: %s", reconnect_error)
                    if self._audio_device_id is None:
                        endpoint_confirmed = False
                        if previous_id and self._volume_interface is previous_interface:
                            self._audio_device_id = previous_id
                    metered_id = await self._metered_endpoint_id()

                if metered_id is None or not self._audio_device_id or not endpoint_confirmed:
                    logger.warning("Cannot mute for PTT -- audio endpoint identity is unverified")
                    self._last_error = ("Push-to-talk could not re-resolve the audio endpoint"
                                        if metered_id and self._audio_device_id
                                        else "Push-to-talk audio endpoint identity is unverified")
                    return False, "endpoint_unverified"
                # AudioMonitor meters the multimedia default, independently
                # of the plugin's configured volume-control role. Muting a
                # different endpoint cannot authorize listening over it. It
                # is still the endpoint device_type asked for, and silencing
                # it for the hold is what that setting is for, so the mute
                # below runs either way and only the override is withheld
                # (wh-codex-merge-audit.6.1.6).
                covers_metered_endpoint = metered_id == self._audio_device_id
                if not covers_metered_endpoint:
                    logger.warning("PTT override withheld -- volume device differs from monitored speakers")
                    self._last_error = "Push-to-talk audio endpoint differs from the monitored speakers"
                current_db = await asyncio.to_thread(self._volume_interface.GetMasterVolumeLevel)
                # The copy on disk is written BEFORE the device write, the
                # opposite of the saved level below. A process lost between
                # the two writes must not leave the speakers down with no
                # record. When the device write then raises, the record stays
                # on disk, and the launcher's restore writes the pre-hold
                # level only if the endpoint is at the lowered level at that
                # time (wh-ptt-mute-orphaned-on-process-loss).
                await self._write_ptt_volume_record(current_db)
                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, self._min_volume_db, None)
                # The saved level is the whole record that a hold has the
                # speakers down: _handle_volume_adjust reads it to decide
                # whether a wheel turn belongs on the device now or waits for
                # the release, and _handle_ptt_stopped reads it to decide
                # whether there is anything to restore. So it is written only
                # after the device has actually been turned down. A read that
                # worked followed by a write that raised leaves the speakers
                # loud, and a saved level would then claim a mute that never
                # happened (wh-ptt-audio-override.1.9).
                self._pre_ptt_volume_db = current_db
                logger.info(f"[PTT] System audio muted for push-to-talk (was {current_db:.1f}dB)")
                if not covers_metered_endpoint:
                    return False, "endpoint_mismatch"
                self._last_error = None
                return True, "muted"
            except Exception as e:
                logger.error(f"[PTT] Failed to mute system audio: {e}")
                return False, "error"

    async def _report_ptt_endpoint_configuration_error(self, muted: bool, reason: str) -> None:
        """Tell the user when the configured audio device costs them a hold.

        The refusal reaches only wheelhouse.log otherwise, and the hold's
        speech goes off with nothing naming the setting behind it. initialize
        already treats a configuration value that costs the user a capability
        as this event's case, and StateManager bridges it to a Windows notice.
        One notice per distinct reason, cleared by the next successful mute,
        is what keeps a repeated press from repeating it
        (wh-codex-merge-audit.6.1.4).
        """
        if muted:
            self._ptt_endpoint_errors_reported.clear()
            return
        if reason != "endpoint_mismatch":
            # endpoint_unverified is Windows reporting no readable render
            # endpoint -- the transient device condition
            # handlers/audio_monitor.py reports as "Audio device unavailable"
            # -- not a setting the user can correct, and under the shipped
            # device_type = "default" every clause below is a no-op for it. It
            # keeps _last_error and the health surface, and loses only the
            # notice (wh-codex-merge-audit.6.2.2).
            return
        if reason in self._ptt_endpoint_errors_reported or not self._event_bus:
            return
        self._ptt_endpoint_errors_reported.add(reason)
        await self._event_bus.publish(SystemConfigurationErrorEvent(
            service_name="SystemVolumePlugin",
            error_message=(
                f"Push-to-talk cannot confirm the '{self._device_type}' audio "
                f"device is the one Windows plays through ({reason}), so "
                "speech stays off for the whole hold while sound is playing."
            ),
            user_action=(
                f"Make the '{self._device_type}' playback device the one "
                "Windows plays through"
                + ("." if self._device_type == "default" else
                   ", or set plugins.system_volume.device_type in config.toml "
                   "to 'default'.")
            ),
        ))

    async def _metered_endpoint_id(self) -> Optional[str]:
        """Identify the endpoint AudioMonitor meters, or None if it cannot be read.

        An unreadable identity is reported as None rather than raised, because
        the caller answers it with a re-resolution and another attempt on the
        next press, not with the failure path (wh-codex-merge-audit.6.1.5).
        """
        try:
            return await asyncio.to_thread(lambda: AudioUtilities.GetSpeakers().GetId())
        except Exception as e:
            logger.warning("Cannot read the monitored speakers' identity: %s", e)
            return None

    async def _publish_ptt_mute_state(self, muted: bool, reason: str, hold_id: int) -> None:
        """Tell StateManager whether the speakers were actually silenced.

        The hold number comes straight from the PTTStartedEvent that caused
        this mute. StateManager ignores a report for any hold other than the
        one running, which is how a report that arrives late, after the user
        released the button and pressed it again, stops being acted on as if it
        answered the new hold (wh-ptt-audio-override.1.1).
        """
        if not self._event_bus:
            return
        await self._event_bus.publish(
            PTTMuteStateEvent(muted=muted, reason=reason, hold_id=hold_id)
        )

    async def _handle_ptt_stopped(self, event: PTTStoppedEvent) -> None:
        """Restore system audio when push-to-talk ends.

        The lock is what makes the check below trustworthy. Without it a
        release could reach this method while the mute was still reading the
        old level, find nothing recorded, and return, leaving the mute that
        followed with nothing to undo it.
        """
        async with self._ptt_audio_lock:
            if self._pre_ptt_volume_db is None:
                return  # Nothing to restore

            if not self._volume_interface:
                logger.warning("Cannot restore volume after PTT -- audio device not connected")
                # The record on disk stays: the speakers can still be at the
                # lowered level, and the launcher's restore can put the level
                # back when WheelHouse stops or starts
                # (wh-ptt-mute-orphaned-on-process-loss).
                self._pre_ptt_volume_db = None
                return

            try:
                restore_db = self._pre_ptt_volume_db
                self._pre_ptt_volume_db = None
                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, restore_db, None)
                logger.info(f"[PTT] System audio restored to {restore_db:.1f}dB")
            except Exception as e:
                # The record on disk stays, for the same reason as above.
                logger.error(f"[PTT] Failed to restore system audio: {e}")
            else:
                await self._delete_ptt_volume_record()

    async def _write_ptt_volume_record(self, pre_hold_db: float) -> None:
        """Keep the hold on disk, so the launcher can put the level back if this process is lost.

        Call only with _ptt_audio_lock held. The record names the endpoint of
        the cached volume interface, the level to put back, and the level the
        mute writes. A record that cannot be written is logged, and the caller
        goes on unchanged: only the restore after a lost process depends on it
        (wh-ptt-mute-orphaned-on-process-loss).
        """
        try:
            written = await asyncio.to_thread(
                ptt_volume_record.write_record,
                self._audio_device_id,
                pre_hold_db,
                self._min_volume_db,
            )
        except Exception as e:
            logger.warning("[PTT] Writing the push-to-talk volume record raised: %s", e)
            written = False
        if not written:
            logger.warning(
                "[PTT] The push-to-talk volume record was not written. If this process "
                "is lost during the hold, the speakers stay at the lowered level."
            )

    async def _delete_ptt_volume_record(self) -> None:
        """Remove the hold's record after the level is back. Call only with _ptt_audio_lock held.

        A record that cannot be deleted is logged. The launcher's restore
        writes the pre-hold level only if the endpoint is at the lowered level
        at that time; otherwise it deletes the record without a write.
        """
        try:
            deleted = await asyncio.to_thread(ptt_volume_record.delete_record)
        except Exception as e:
            logger.warning("[PTT] Deleting the push-to-talk volume record raised: %s", e)
            deleted = False
        if not deleted:
            logger.warning("[PTT] The push-to-talk volume record was not deleted after the restore")
