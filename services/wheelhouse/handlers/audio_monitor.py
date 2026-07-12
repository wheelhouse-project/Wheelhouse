"""System audio monitoring with spatial sound management.

This module monitors Windows system audio levels and manages spatial sound
settings based on audio playback activity. It integrates with the state
management system to provide audio-aware speech recognition control and
automatic spatial sound toggling for an enhanced audio experience.

Key Classes:
  - AudioMonitor: Monitors system audio levels and manages spatial sound.

Key Features:
  - Real-time system audio level monitoring using WASAPI
  - Automatic spatial sound enable/disable based on audio activity
  - Speech recognition suppression during audio playback
  - Integration with Windows DolbyAccess spatial sound system
  - Audio threshold-based activity detection
  - COM interface management for audio monitoring

Key Integrations:
  - StateManager for speech suppression control
  - Windows audio endpoints via pycaw
  - DolbyAccess.exe for spatial sound control

Typical Usage:
  from handlers.audio_monitor import AudioMonitor
  
  audio_monitor = AudioMonitor(
      loop=event_loop,
      config=config,
      state_manager=state_mgr
  )
  
  # Start monitoring task
  task = asyncio.create_task(audio_monitor.start_monitoring())
"""
"""
Audio monitoring and spatial sound management
"""

import asyncio
import logging
import os
import shutil
import subprocess
from typing import Optional, Dict, Any, TYPE_CHECKING

import pythoncom
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import AudioStateChangedEvent, SystemConfigurationErrorEvent

if TYPE_CHECKING:
    from ..config_service import ConfigService

logger = logging.getLogger(__name__)

class AudioMonitor:
    """Monitors audio playback and adjusts spatial sound settings."""

    def __init__(self, loop: asyncio.AbstractEventLoop, config_service: "ConfigService", event_bus: EventBus):
        self.loop = loop
        self.config_service = config_service
        self.event_bus = event_bus
        self.audio_meter: Optional[IAudioMeterInformation] = None
        self.was_audio_playing = False
        self._previous_audio_state: Optional[bool] = None
        self._first_run = True
        
        # Spatial sound feature control - fail-fast validation with graceful degradation
        self.spatial_sound_enabled = False
        self.spatial_sound_exec = None
        self.sony_tv_name = None
        self.spatial_sound_format = None
        
        self._audio_suspended = False

        self._validate_and_configure_spatial_sound()

    def start(self) -> asyncio.Task:
        """Creates and returns the monitoring task."""
        return self.loop.create_task(self.monitor_audio())

    def _validate_and_configure_spatial_sound(self) -> None:
        """
        Validate spatial sound configuration with fail-fast error reporting.
        
        :flow: Speech Suppression by Audio
        :step: 1.5
        :description: Validates spatial sound configuration with fail-fast error reporting
        :data_in: Config values (SPATIAL_SOUND_EXEC, device_name, format)
        :data_out: spatial_sound_enabled flag and optional SystemConfigurationErrorEvent
        :notes: Initialization validation during AudioMonitor setup. Checks executable paths for spatial sound feature, gracefully degrades by disabling feature when misconfigured. Publishes SystemConfigurationErrorEvent via EventBus on validation failures - StateManager bridges this to Windows toast notifications for user awareness. Prevents silent failures, enables self-healing architecture. Spatial sound is optional feature - audio monitoring works independently.
        """
        # Get configuration values - fail fast for critical settings
        configured_exec = self.config_service.get("SPATIAL_SOUND_EXEC", "").strip()
        
        configured_tv_name = self.config_service.get("plugins.bravia.device_name")
        if not configured_tv_name:
            raise ValueError("plugins.bravia.device_name not configured in config.toml")
        configured_tv_name = configured_tv_name.strip()
        
        configured_format = self.config_service.get("SPATIAL_SOUND_FORMAT", "").strip()
        
        # Check if features are disabled (empty strings)
        if not configured_exec or not configured_tv_name or not configured_format:
            self.spatial_sound_enabled = False
            logger.info("Spatial sound features disabled (empty configuration values)")
            return
        
        # Features are enabled - validate configuration
        errors = []
        
        # Validate executable path
        if not os.path.exists(configured_exec):
            errors.append(f"Spatial sound executable not found: {configured_exec}")
        else:
            self.spatial_sound_exec = configured_exec
            
        # Store other validated config (TV name and format are harder to validate at startup)
        self.sony_tv_name = configured_tv_name
        self.spatial_sound_format = configured_format
        
        # If there are errors, disable features and send notification
        if errors:
            self.spatial_sound_enabled = False
            error_message = "; ".join(errors)
            user_action = "Check SPATIAL_SOUND_EXEC path in config.toml or set to empty string to disable"
            
            # Send configuration error event
            asyncio.create_task(self.event_bus.publish(SystemConfigurationErrorEvent(
                service_name="AudioMonitor",
                error_message=error_message,
                user_action=user_action
            )))
        else:
            self.spatial_sound_enabled = True
            logger.info(f"Spatial sound features enabled with executable: {self.spatial_sound_exec}")

    def is_audio_playing(self) -> bool:
        """Check if audio is currently playing."""
        try:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            audio_meter = interface.QueryInterface(IAudioMeterInformation)
            peak = audio_meter.GetPeakValue()

            if self._audio_suspended:
                logger.info("Audio COM connection recovered")
                self._audio_suspended = False

            return peak > 0.05  # Increased from 0.01 to 0.05 to filter out VS Code signals
        except Exception as e:
            if not self._audio_suspended:
                logger.warning(f"Audio device unavailable, suspending monitor: {e}")
                self._audio_suspended = True
            return False

    async def monitor_audio(self) -> None:
        """:flow: Speech Suppression by Audio
        :step: 1
        :description: Periodically checks Windows audio playback state and publishes changes
        :data_in: Windows Core Audio API state (via is_audio_playing())
        :data_out: AudioStateChangedEvent published to EventBus
        :notes: Main monitoring loop running in separate thread. Polls Windows audio state every 1s using pycaw library to detect system audio playback. On state change (started/stopped playing), publishes AudioStateChangedEvent to EventBus for decoupled speech suppression. Also manages spatial sound settings for Bravia TV (optional feature). Audio monitoring works independently of spatial sound configuration.
        """
        # Note: Audio monitoring for speech suppression always works regardless of spatial sound configuration

        pythoncom.CoInitialize()
        try:
            test_interval = 1.0
            while True:
                try:
                    is_playing = self.is_audio_playing()

                    # COM device unavailable (idle/sleep) -- wait 60s before retrying
                    if self._audio_suspended:
                        await asyncio.sleep(60.0)
                        continue

                    # On the first successful poll, just set the initial state and don't trigger any change.
                    if self._first_run:
                        self._previous_audio_state = is_playing
                        self._first_run = False
                        logger.debug(f"AudioMonitor initial state: {'playing' if is_playing else 'not playing'}.")
                        await asyncio.sleep(test_interval)
                        continue

                    # Publish event if audio state has changed
                    if is_playing != self._previous_audio_state:
                        """:flow: Speech Suppression by Audio
                        :step: 2
                        :produces_for: Speech Suppression by Audio
                        :description: Publishes audio state change event when playback starts/stops
                        :data_in: Boolean is_playing state change
                        :data_out: AudioStateChangedEvent published to EventBus
                        :notes: Event publishing handoff from monitor to decoupled eventing system. Only publishes on state transitions (not every poll). Event consumed by StateManager._handle_audio_state_changed() in step 3. This decoupling allows multiple subscribers to react to audio state without AudioMonitor knowing about them.
                        """
                        logger.debug(f"Audio state changed to {'playing' if is_playing else 'not playing'}. Publishing event.")
                        await self.event_bus.publish(AudioStateChangedEvent(is_playing=is_playing))
                        self._previous_audio_state = is_playing

                    # Manage spatial sound settings
                    if is_playing:
                        if not self.was_audio_playing:
                            self.was_audio_playing = True
                            #logger.info("New Audio detected. Adjusting spatial sound.")
                            #await self.start_atmos()  # Will silently skip if disabled
                            test_interval = 10.0
                    else:
                        if self.was_audio_playing:
                            self.was_audio_playing = False
                            test_interval = 0.1
                    await asyncio.sleep(test_interval)
                except asyncio.CancelledError:
                    logger.info("Audio monitor task cancelled.")
                    raise 
                except Exception as e:
                    logger.error(f"Error in monitor_audio loop: {e}")
                    await asyncio.sleep(test_interval)
        except asyncio.CancelledError:
            pass # Expected on shutdown
        finally:
            pythoncom.CoUninitialize()
            logger.info("Audio monitor task finished, COM uninitialized.")

    async def start_atmos(self) -> None:
        """:flow: Atmos Activation by Mouse Hover
        :step: 4
        :description: Validates configuration and initiates spatial sound change
        :data_in: Call from MouseHandler
        :data_out: Calls set_spatial_sound
        :notes: Entry point into AudioMonitor for this flow. Checks if spatial sound is enabled and configured (TV name, format). If valid, proceeds to execution (step 5). Silently returns if disabled, preventing errors on unsupported systems.
        """
        """Sets spatial sound to Dolby Atmos using validated configuration."""
        if not self.spatial_sound_enabled or not self.sony_tv_name or not self.spatial_sound_format:
            return  # Silent skip - features disabled or configuration invalid
            
        await self.set_spatial_sound(self.sony_tv_name, self.spatial_sound_format)

    async def set_spatial_sound(
        self, device_name: str, spatial_sound_format: str
    ) -> None:
        """:flow: Atmos Activation by Mouse Hover
        :step: 5
        :description: Executes external command to set Windows spatial sound
        :data_in: device_name, spatial_sound_format
        :data_out: Subprocess execution of helper executable
        :notes: The final actuation step. Uses `asyncio.create_subprocess_exec` to call the external helper tool (configured in SPATIAL_SOUND_EXEC). Changes Windows audio settings to enable Dolby Atmos for the specified device.
        """
        """Sets the spatial sound format using an external executable."""
        if not self.spatial_sound_enabled or not self.spatial_sound_exec:
            return  # Silent skip - features disabled or configuration invalid

        command = [
            self.spatial_sound_exec,
            "/SetSpatial",
            device_name,
            spatial_sound_format,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                logger.info("Spatial sound set to Atmos successfully.")
            else:
                logger.error(f"Failed to set spatial sound: {stderr.decode().strip()}")
        except Exception as e:
            logger.error(f"Exception in set_spatial_sound: {e}")