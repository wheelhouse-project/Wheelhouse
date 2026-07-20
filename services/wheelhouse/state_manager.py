"""Application state management and GUI synchronization for WheelHouse.

This module manages the dynamic runtime state of the WheelHouse application,
including speech recognition control, feature toggles, and cross-process
communication with the GUI. It serves as the single source of truth for
application state and handles state persistence, change notifications, and
configuration synchronization.

Key Classes:
  - StateManager: Central state coordinator and persistence manager.

Key State Categories:
  - Speech recognition enable/disable state
  - Audio-based speech suppression
  - Feature toggles (window mover, etc.)
  - WebSocket connection status
  - Configuration persistence

Key Features:
  - Real-time GUI state synchronization
  - Debounced configuration persistence
  - Computed properties for complex state logic
  - Event-driven state change notifications
  - Cross-process communication via queues

Typical Usage:
  from state_manager import StateManager
  
  state_manager = StateManager(
                config=config, 
                event_bus=event_bus, 
                loop=loop, 
                state_to_gui_queue=state_to_gui_queue, 
                websocket_manager=app.websocket_manager # Get it from the app instance
            )
  
  # Check computed state
  if state_mgr.speech_enabled:
      process_speech_input()
      
  # Update state
  state_mgr.set_speech_enabled(True)
  state_mgr.update_config("debug_mode", False)
"""
import asyncio
import logging
from multiprocessing import Queue
from typing import Dict, Any, Optional, TYPE_CHECKING

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (
    SonosStateChangedEvent, AudioStateChangedEvent,
    SystemConfigurationErrorEvent, SystemIdleStateChangedEvent,
    WakeWordDetectedEvent, PTTStartedEvent, PTTStoppedEvent,
)
from services.wheelhouse.integrations.websocket_manager import WebSocketManager
from services.wheelhouse.utils.speech_notifier import SpeechNotifier
from services.wheelhouse.config_service import DEFAULT_STT_PROVIDER

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService

logger = logging.getLogger(__name__)


class StateManager:
    """Manages the application's dynamic state and GUI communication."""

    def __init__(self, config_service: "ConfigService", event_bus: EventBus, loop: asyncio.AbstractEventLoop, state_to_gui_queue: Queue, websocket_manager: Optional[WebSocketManager]):
        self.config_service = config_service
        self.event_bus = event_bus
        self.loop = loop
        self.state_to_gui_queue = state_to_gui_queue
        self.websocket_manager = websocket_manager

        # State variables
        self._speech_enabled = self.config_service.get("SPEECH_ENABLED_ON_STARTUP", False)
        self._speech_suppressed_by_audio = False
        self._speech_suppressed_by_sonos = False
        self._speech_suppressed_by_idle = False
        self._speech_enabled_before_idle = None  # Track state before idle
        self.interim_results_enabled = True  # Whether STT sends partial results
        self.debug_mode = False  # Whether log level is DEBUG

        self.stt_websocket_connection: Optional[Any] = None
        self._stt_manager = None  # Reference to STTManager for state queries
        self._remote_stt_launcher = None  # Reference to RemoteSTTLauncher for provider info
        self._ai_service = None  # Reference to AIService for model discovery
        
        # Speech state notification system
        self.speech_notifier = SpeechNotifier(enabled=False)
        self._toggle_counter = 0

        # Push-to-talk state
        self._speech_interaction_mode = self.config_service.get("speech.interaction_mode", "toggle")
        self._ptt_active = False
        self._ptt_safety_handle: Optional[asyncio.TimerHandle] = None

        # Subscribe to events
        self.event_bus.subscribe(SonosStateChangedEvent, self._handle_sonos_state_changed)
        self.event_bus.subscribe(AudioStateChangedEvent, self._handle_audio_state_changed)
        self.event_bus.subscribe(SystemConfigurationErrorEvent, self._handle_system_config_error)
        self.event_bus.subscribe(SystemIdleStateChangedEvent, self._handle_idle_state_changed)
        self.event_bus.subscribe(WakeWordDetectedEvent, self._handle_wake_word_detected)

    async def _handle_sonos_state_changed(self, event: SonosStateChangedEvent):
        """Handles the SonosStateChangedEvent."""
        self._set_speech_suppressed_by_sonos(event.is_playing)

    async def _handle_audio_state_changed(self, event: AudioStateChangedEvent):
        """:flow: Speech Suppression by Audio
        :step: 3
        :description: EventBus subscriber receiving audio state changes from AudioMonitor
        :data_in: AudioStateChangedEvent with is_playing boolean
        :data_out: Call to set_speech_suppressed_by_audio()
        :notes: EventBus subscription handler connecting AudioMonitor (step 2) to suppression logic (step 4). Receives audio playback state changes and forwards to set_speech_suppressed_by_audio() which updates internal suppression flag and broadcasts speech state changes. This indirection enables event-driven architecture - AudioMonitor doesn't need direct StateManager reference.
        """
        self.set_speech_suppressed_by_audio(event.is_playing)

    async def _handle_idle_state_changed(self, event: SystemIdleStateChangedEvent):
        """:flow: Speech Suppression by Idle
        :step: 3
        :description: EventBus subscriber handling idle state transitions and managing speech suppression
        :data_in: SystemIdleStateChangedEvent with is_idle boolean and idle_duration_seconds
        :data_out: Updated _speech_suppressed_by_idle flag, WebSocket broadcast, GUI state update
        :notes: Manages speech suppression lifecycle for idle detection. On IDLE transition: saves current speech_enabled state for restoration, sets _speech_suppressed_by_idle=True, broadcasts to WebSocket clients and GUI. On ACTIVE transition: clears _speech_suppressed_by_idle=False, restores previous state via computed property (step 4). Only sends notifications on actual state changes (deduplicates heartbeats). Follows same pattern as audio/Sonos suppression for consistency. Respects ENABLE_IDLE_SUPPRESSION config flag.
        """
        if event.is_idle:
            # System became idle
            if not self._speech_suppressed_by_idle:
                # Save current speech state
                self._speech_enabled_before_idle = self.speech_enabled

                old_speech_enabled = self.speech_enabled
                self._speech_suppressed_by_idle = True
                new_speech_enabled = self.speech_enabled

                logger.info(
                    f"[IDLE MONITOR] Speech suppressed by idle state "
                    f"(idle for {event.idle_duration_seconds:.1f}s). "
                    f"Saved state: {self._speech_enabled_before_idle}"
                )

                # Send notification only if state actually changed
                if old_speech_enabled != new_speech_enabled:
                    self.speech_notifier.notify_suppression_change(
                        "System Idle",
                        True,
                        f"Saved state: {self._speech_enabled_before_idle}"
                    )

                # Broadcast update
                if self.websocket_manager:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled, reason="idle")
                    self.loop.create_task(self.websocket_manager.broadcast(message))

                self.send_state_update()

        else:
            # System became active
            if self._speech_suppressed_by_idle:
                old_speech_enabled = self.speech_enabled
                self._speech_suppressed_by_idle = False
                new_speech_enabled = self.speech_enabled

                logger.info(
                    f"[IDLE MONITOR] Speech un-suppressed by activity resume. "
                    f"Restored state: {new_speech_enabled}"
                )

                # Send notification only if state actually changed
                if old_speech_enabled != new_speech_enabled:
                    self.speech_notifier.notify_suppression_change(
                        "System Idle",
                        False,
                        f"Restored state: {new_speech_enabled}"
                    )

                # Clear saved state
                self._speech_enabled_before_idle = None

                # Broadcast update
                if self.websocket_manager:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled)
                    self.loop.create_task(self.websocket_manager.broadcast(message))

                self.send_state_update()

    async def _handle_wake_word_detected(self, event: WakeWordDetectedEvent):
        """Clear idle suppression to re-enable transcription after wake word."""
        if self._speech_suppressed_by_idle:
            old_speech_enabled = self.speech_enabled
            self._speech_suppressed_by_idle = False
            self._speech_enabled_before_idle = None
            new_speech_enabled = self.speech_enabled

            logger.info(
                f"[WAKE WORD] Wake word '{event.keyword}' cleared idle suppression. "
                f"Speech restored: {new_speech_enabled}"
            )

            if old_speech_enabled != new_speech_enabled:
                self.speech_notifier.notify_suppression_change(
                    "Wake Word",
                    False,
                    f"Keyword: {event.keyword}"
                )

            if self.websocket_manager:
                message = self.websocket_manager.set_transcription_status(self.speech_enabled)
                self.loop.create_task(self.websocket_manager.broadcast(message))

            self.send_state_update()
        else:
            logger.info(f"Wake word '{event.keyword}' detected but not idle-suppressed - ignoring")

    async def _handle_system_config_error(self, event: SystemConfigurationErrorEvent):
        """
        Handles SystemConfigurationErrorEvent by sending user notification via IPC.
        
        :flow: System Configuration Error Notification
        :step: 1
        :description: EventBus subscriber that receives *SystemConfigurationErrorEvent* from various services (e.g., AudioMonitor fail-fast validation failures) and bridges them to the GUI process via IPC queue. Transforms EventBus events into IPC notification messages that trigger Windows toast notifications to inform users of configuration issues. This creates the architectural bridge between intra-process EventBus communication and cross-process IPC communication.
        
        Single-step flow: This is an intentional coordination point bridging EventBus to IPC.
        
        :data_in: *SystemConfigurationErrorEvent* from EventBus containing service name, error message, and user action guidance.
        :data_out: IPC notification message sent to GUI process via `state_to_gui_queue` for Windows toast display.
        """
        try:
            notification = {
                'action': 'show_notification',
                'title': f'Wheelhouse: {event.service_name} Configuration Error',
                'message': f'{event.error_message}\n\n{event.user_action}',
                'timeout': 10
            }
            self.state_to_gui_queue.put_nowait(notification)
            logger.warning(f"Configuration error in {event.service_name}: {event.error_message}")
        except Exception as e:
            logger.error(f"Failed to send configuration error notification: {e}")

    @property
    def speech_enabled(self) -> bool:
        """Determines if speech processing should be active.

        :flow: Speech Suppression by Audio
        :step: 3
        :description: Computed property combining user toggle and audio suppression states
        :data_in: _speech_enabled and _speech_suppressed_by_audio flags
        :data_out: Boolean indicating final speech processing state
        :notes: Final checkpoint for audio-based suppression. Returns True only if user enabled speech AND not suppressed by system audio. Respects ENABLE_AUDIO_SUPPRESSION config flag for feature toggle.

        :flow: Speech Suppression by Sonos
        :step: 5
        :description: Computed property combining all suppression states (audio + Sonos + idle)
        :data_in: _speech_enabled, _speech_suppressed_by_audio, _speech_suppressed_by_sonos, _speech_suppressed_by_idle flags
        :data_out: Boolean indicating final authoritative speech state
        :notes: Final checkpoint combining all suppression logic. Returns True only if user enabled AND not suppressed by system audio AND not suppressed by Sonos AND not suppressed by idle. Respects ENABLE_AUDIO_SUPPRESSION, ENABLE_SONOS_SUPPRESSION, and ENABLE_IDLE_SUPPRESSION config flags. This property consulted by SpeechProcessor to gate transcription processing.

        :flow: Speech Suppression by Idle
        :step: 4
        :description: Computed property combining all suppression states including idle
        :data_in: _speech_enabled, _speech_suppressed_by_audio, _speech_suppressed_by_sonos, _speech_suppressed_by_idle flags
        :data_out: Boolean indicating final authoritative speech state
        :notes: Final checkpoint for idle suppression flow. Integrates with existing audio/Sonos suppression via computed property pattern. Returns True only if all conditions met: user enabled speech AND not suppressed by audio AND not suppressed by Sonos AND not suppressed by idle. State restored automatically when _speech_suppressed_by_idle cleared in step 3. Respects ENABLE_IDLE_SUPPRESSION config flag.
        """
        # Check if automatic suppression features are enabled
        audio_suppression_active = (
            self._speech_suppressed_by_audio and
            self.config_service.get("ENABLE_AUDIO_SUPPRESSION", True)
        )
        sonos_suppression_active = (
            self._speech_suppressed_by_sonos and
            self.config_service.get("ENABLE_SONOS_SUPPRESSION", True)
        )
        idle_suppression_active = (
            self._speech_suppressed_by_idle and
            self.config_service.get("ENABLE_IDLE_SUPPRESSION", True)
        )

        return (
            self._speech_enabled and
            not audio_suppression_active and
            not sonos_suppression_active and
            not idle_suppression_active
        )

    def send_state_update(self):
        """:flow: GUI State Synchronization
        :step: 5
        :produces_for: GUI State Synchronization
        :description: Packages all UI state variables and sends to GUI process
        :data_in: Current StateManager state variables
        :data_out: State dictionary sent to state_to_gui_queue
        :notes: Broadcasts complete UI state to GUI process via IPC. Packages state into dictionary with keys: action='state_update', speech_enabled, button_visible, FLOATING_BUTTON_SIZE, FLOATING_BUTTON_POS. Uses Queue.put_nowait() to avoid blocking. This is the outbound half of Logic→GUI IPC direction. Queue is consumed by gui.py's _check_queues_and_events() in GUI process (step 6). Called after any state change (toggle, audio suppression, config updates).
        """
        try:
            state = {
                'action': 'state_update',
                'speech_enabled': self.speech_enabled,
                'button_visible': self.config_service.get('FLOATING_BUTTON_VISIBLE', True),
                'FLOATING_BUTTON_SIZE': self.config_service.get('FLOATING_BUTTON_SIZE', 50),
                'FLOATING_BUTTON_POS': self.config_service.get('FLOATING_BUTTON_POS', [100, 100]),
                'SHOW_SPEECH_PULSE': self.config_service.get('SHOW_SPEECH_PULSE', True),
                'stt_mode': self._get_current_stt_mode(),
                'stt_provider': self._get_current_stt_provider(),
                'stt_providers_available': self._get_available_stt_providers(),
                'stt_provider_display_names': self._get_provider_display_names(),
                'interim_results_enabled': self.interim_results_enabled,
                'debug_mode': self.debug_mode,
                'speech_interaction_mode': self._speech_interaction_mode,
                'ptt_active': self._ptt_active,
                'ai_provider': self._get_current_ai_provider(),
                'ai_providers_available': self._get_available_ai_providers(),
                'ai_provider_display_names': self._get_ai_provider_display_names(),
            }
            self.state_to_gui_queue.put_nowait(state)
        except Exception as e:
            logger.error(f"Failed to send state update to GUI: {e}")

    def toggle_speech_enabled_state(self):
        """:flow: GUI State Synchronization
        :step: 5
        :description: Toggles speech state with intuitive behavior and clears suppression
        :data_in: Current speech_enabled computed property value
        :data_out: Updated _speech_enabled flag and cleared suppression flags
        :notes: Handler for toggle_speech_enabled_state action from step 4. Implements intuitive toggle: if speech currently OFF (for any reason), enables it and clears ALL suppression flags (audio and Sonos). If speech currently ON, disables it. Broadcasts state via send_state_update() and WebSocket to STT clients. Sends speech_notifier notifications for enabled/disabled transitions. Maintains toggle counter for debugging.
        """
        self._toggle_counter += 1
        old_speech_enabled = self.speech_enabled
        
        # Intuitive toggle: if speech is currently OFF, turn it ON. If ON, turn it OFF.
        if not old_speech_enabled:
            # Speech is currently disabled - enable it and clear suppression
            self._speech_enabled = True
            self._speech_suppressed_by_audio = False
            self._speech_suppressed_by_sonos = False
            self._speech_suppressed_by_idle = False

            self.speech_notifier.notify_debug(f"Toggle #{self._toggle_counter}: Enabling speech (was disabled)")
            logger.info(f"[USER TOGGLE] Speech ENABLED by user. Cleared all suppression. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos}, idle_suppressed={self._speech_suppressed_by_idle}")
            
        else:
            # Speech is currently enabled - disable it
            self._speech_enabled = False

            self.speech_notifier.notify_debug(f"Toggle #{self._toggle_counter}: Disabling speech (was enabled)")
            logger.info(f"[USER TOGGLE] Speech DISABLED by user. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos}, idle_suppressed={self._speech_suppressed_by_idle}")

        # Send appropriate notification
        new_speech_enabled = self.speech_enabled
        if new_speech_enabled and not old_speech_enabled:
            self.speech_notifier.notify_speech_enabled("User toggle", "All suppression cleared")
        elif not new_speech_enabled and old_speech_enabled:
            self.speech_notifier.notify_speech_disabled("User toggle")
        else:
            # Unexpected state - should not happen with new logic
            self.speech_notifier.notify_debug(f"Unexpected toggle result: {old_speech_enabled} -> {new_speech_enabled}")

        # Update the central status and get the message to broadcast
        if self.websocket_manager:
            if not self.speech_enabled:
                message = self.websocket_manager.set_transcription_status(self.speech_enabled, reason="manual")
            else:
                message = self.websocket_manager.set_transcription_status(self.speech_enabled)

            # Broadcast the new status to all STT clients
            self.loop.create_task(self.websocket_manager.broadcast(message))

        self.send_state_update()

    def ptt_start(self, source: str = "floating_button"):
        """Activate push-to-talk: enable speech, clear idle, mute audio, start safety timeout."""
        if self._ptt_active:
            return  # Already active

        self._speech_before_ptt = self._speech_enabled  # Save for drag cancel restore
        self._ptt_active = True
        self._speech_enabled = True
        self._speech_suppressed_by_idle = False

        logger.info(f"[PTT] Push-to-talk started (source={source})")

        # Publish event for audio muting
        self.loop.create_task(self.event_bus.publish(PTTStartedEvent(source=source)))

        # Broadcast to STT providers
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(True, reason="ptt")
            self.loop.create_task(self.websocket_manager.broadcast(message))

        # Start safety timeout
        timeout_seconds = self.config_service.get("speech.ptt_safety_timeout_seconds", 30)
        if self._ptt_safety_handle:
            self._ptt_safety_handle.cancel()
        self._ptt_safety_handle = self.loop.call_later(
            timeout_seconds, self._ptt_safety_timeout
        )

        self.send_state_update()

    def ptt_stop(self, reason: str = "released"):
        """Deactivate push-to-talk: disable speech, restore audio."""
        if not self._ptt_active:
            return  # Not active, nothing to do

        # Cancel safety timeout
        if self._ptt_safety_handle:
            self._ptt_safety_handle.cancel()
            self._ptt_safety_handle = None

        self._ptt_active = False
        if reason == "drag_cancel":
            # Drag interrupted hold -- restore pre-PTT speech state
            self._speech_enabled = getattr(self, '_speech_before_ptt', False)
        else:
            self._speech_enabled = False

        logger.info(f"[PTT] Push-to-talk stopped (reason={reason})")

        # Publish event for audio restore
        self.loop.create_task(self.event_bus.publish(PTTStoppedEvent(reason=reason)))

        # Broadcast to STT providers
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(False, reason="ptt")
            self.loop.create_task(self.websocket_manager.broadcast(message))

        self.send_state_update()

    def _ptt_safety_timeout(self):
        """Safety timeout: auto-stop PTT if ptt_stop was never received."""
        logger.warning("[PTT] Safety timeout expired -- auto-stopping PTT")
        self.ptt_stop(reason="safety_timeout")

    def set_speech_interaction_mode(self, mode: str):
        """Switch between 'toggle' and 'push_to_talk' interaction modes.

        Disables speech on mode switch for clean state transition:
        - PTT mode: speech should only be active while holding the button
        - Toggle mode: user can single-click to re-enable
        """
        if mode not in ("toggle", "push_to_talk"):
            logger.warning(f"Invalid interaction mode: {mode!r}, ignoring")
            return

        old_mode = self._speech_interaction_mode
        self._speech_interaction_mode = mode

        # Disable speech on mode switch for immediate visual feedback
        if self._speech_enabled:
            self._speech_enabled = False
            logger.info("[MODE] Speech disabled on interaction mode change")
            self.speech_notifier.notify_speech_disabled("Mode switch")
            if self.websocket_manager:
                message = self.websocket_manager.set_transcription_status(False, reason="manual")
                self.loop.create_task(self.websocket_manager.broadcast(message))

        # Persist to config
        self.config_service.set("speech.interaction_mode", mode)
        self.loop.create_task(self.config_service.save())

        logger.info(f"[MODE] Speech interaction mode changed: {old_mode} -> {mode}")
        self.send_state_update()

    def set_speech_suppressed_by_audio(self, is_suppressed: bool):
        """:flow: Speech Suppression by Audio
        :step: 4
        :description: Updates audio suppression flag and broadcasts state changes
        :data_in: Boolean is_suppressed (True when system audio playing)
        :data_out: WebSocket broadcast and GUI state update via send_state_update()
        :notes: Suppression state updater called from step 3. Updates _speech_suppressed_by_audio flag, checks if overall speech_enabled changed (via property), sends notifications only on actual transitions. Broadcasts to two channels: (1) WebSocket to STT clients via websocket_manager, (2) IPC to GUI via send_state_update(). Notifications include details (user toggle + Sonos state) for debugging. Only processes on state change to avoid spam.
        """
        if self._speech_suppressed_by_audio != is_suppressed:
            old_speech_enabled = self.speech_enabled
            self._speech_suppressed_by_audio = is_suppressed
            new_speech_enabled = self.speech_enabled
            
            logger.info(f"[AUDIO MONITOR] Speech {'suppressed' if is_suppressed else 'un-suppressed'} by system audio. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos} -> final={new_speech_enabled}")
            
            # Send notification about audio suppression change
            if old_speech_enabled != new_speech_enabled:
                details = f"Speech enabled: {self._speech_enabled}, Sonos suppressed: {self._speech_suppressed_by_sonos}"
                self.speech_notifier.notify_suppression_change("System Audio", is_suppressed, details)
            
            if self.websocket_manager:
                # Broadcast the change to STT clients
                if is_suppressed:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled, reason="audio")
                else:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled)
                self.loop.create_task(self.websocket_manager.broadcast(message))

            # Update the GUI
            self.send_state_update()

    def _set_speech_suppressed_by_sonos(self, is_suppressed: bool):
        """:flow: Speech Suppression by Sonos
        :step: 4
        :description: Updates Sonos suppression flag and broadcasts state changes
        :data_in: Boolean is_suppressed (True when Sonos playing music, not local audio)
        :data_out: WebSocket broadcast and GUI state update via send_state_update()
        :notes: Suppression state updater called from SonosPlugin via EventBus (step 3 heartbeat). Updates _speech_suppressed_by_sonos flag, checks if overall speech_enabled changed (via property in step 5), sends notifications only on actual transitions. Broadcasts to two channels: (1) WebSocket to STT clients, (2) IPC to GUI. Deduplicates heartbeat events - only processes on state change to avoid spam. Notifications include details (user toggle + audio state) for debugging.
        """
        if self._speech_suppressed_by_sonos != is_suppressed:
            old_speech_enabled = self.speech_enabled
            self._speech_suppressed_by_sonos = is_suppressed
            new_speech_enabled = self.speech_enabled
            
            logger.info(f"[SONOS PLUGIN] Speech {'suppressed' if is_suppressed else 'un-suppressed'} by Sonos playback. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos} -> final={new_speech_enabled}")
            
            # Send notification about Sonos suppression change
            if old_speech_enabled != new_speech_enabled:
                details = f"Speech enabled: {self._speech_enabled}, Audio suppressed: {self._speech_suppressed_by_audio}"
                self.speech_notifier.notify_suppression_change("Sonos Playback", is_suppressed, details)
            
            if self.websocket_manager:
                # Broadcast the change to STT clients
                if is_suppressed:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled, reason="sonos")
                else:
                    message = self.websocket_manager.set_transcription_status(self.speech_enabled)
                self.loop.create_task(self.websocket_manager.broadcast(message))

            # Update the GUI
            self.send_state_update()

    async def toggle_button_visibility(self):
        """Toggles the visibility of the floating button."""
        is_visible = self.config_service.get('FLOATING_BUTTON_VISIBLE', True)
        await self.set_config_value('FLOATING_BUTTON_VISIBLE', not is_visible)

    async def set_config_value(self, key: str, value: Any):
        """Updates a configuration value and saves it."""
        logger.debug(f"Updating config: '{key}' = {value}")
        self.config_service.set(key, value)
        await self.config_service.save()
        self.send_state_update()

    def register_stt_connection(self, connection: Any):
        """Registers the active STT WebSocket connection."""
        self.stt_websocket_connection = connection

    def unregister_stt_connection(self):
        """Unregisters the STT WebSocket connection."""
        self.stt_websocket_connection = None

    async def cancel_pending_saves(self):
        """Cancels any pending configuration saves, typically during shutdown."""
        # This is now handled by the ConfigService, but we keep the method for API compatibility
        pass

    def set_stt_manager(self, stt_manager) -> None:
        """Set reference to STTManager for state queries."""
        self._stt_manager = stt_manager

    def set_remote_stt_launcher(self, launcher) -> None:
        """Set reference to RemoteSTTLauncher for provider discovery."""
        self._remote_stt_launcher = launcher

    def set_ai_service(self, ai_service) -> None:
        """Set AIService reference for model discovery state."""
        self._ai_service = ai_service

    def _get_current_stt_mode(self) -> str:
        """Get current STT mode: 'remote' or 'in_process'."""
        return self.config_service.get("stt.mode", "remote")

    def _get_current_stt_provider(self) -> str | None:
        """Get current STT provider name.

        In remote mode, returns the actual provider from stt.last_provider config.
        For zipformer, expands to zipformer_cpu or zipformer_gpu based on config.
        In in_process mode, returns the provider from STTManager or config.
        """
        mode = self._get_current_stt_mode()
        if mode == "remote":
            # Return actual remote provider name from config
            provider = self.config_service.get("stt.last_provider", DEFAULT_STT_PROVIDER)
            # Expand zipformer to CPU/GPU variant
            if provider == "zipformer":
                return self._get_zipformer_variant()
            return provider
        if self._stt_manager:
            return self._stt_manager.get_current_provider()
        # Fallback to config
        return self.config_service.get("stt.provider", "google")

    def _get_zipformer_variant(self) -> str:
        """Determine current Zipformer variant (CPU or GPU) from its config.

        Reads the Zipformer config.toml to check the use_gpu setting.
        Returns "zipformer_cpu" or "zipformer_gpu".
        """
        if not self._remote_stt_launcher:
            return "zipformer_cpu"  # Default to CPU if no launcher

        provider_info = self._remote_stt_launcher.get_provider_by_name("zipformer")
        if not provider_info:
            return "zipformer_cpu"

        config_path = provider_info["service_dir"] / "config.toml"
        if not config_path.exists():
            return "zipformer_cpu"

        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib

            with open(config_path, "rb") as f:
                config = tomllib.load(f)

            use_gpu = config.get("model", {}).get("use_gpu", False)
            return "zipformer_gpu" if use_gpu else "zipformer_cpu"
        except Exception:
            return "zipformer_cpu"

    def _get_available_stt_providers(self) -> list[str]:
        """Get list of available STT providers.

        In remote mode, uses RemoteSTTLauncher to discover providers.
        Expands "zipformer" into "zipformer_cpu" and "zipformer_gpu" variants.
        In in_process mode, checks for configured cloud providers.
        """
        mode = self._get_current_stt_mode()

        # Remote mode: use RemoteSTTLauncher cached providers
        if mode == "remote" and self._remote_stt_launcher:
            providers = self._remote_stt_launcher.get_providers()
            result = []
            for p in providers:
                if p["name"] == "zipformer":
                    # Expand zipformer into CPU and GPU variants
                    result.append("zipformer_cpu")
                    result.append("zipformer_gpu")
                else:
                    result.append(p["name"])
            return result

        # In-process mode: check configured cloud providers
        providers = []

        # Cloud providers (if credentials configured)
        azure_key = self.config_service.get("stt.azure.subscription_key", "")
        if azure_key:
            providers.append("azure")

        return providers

    def _get_provider_display_names(self) -> dict[str, str]:
        """Get display name mapping for available providers.

        Returns a dict mapping provider name to display name.
        Expands "zipformer" into CPU and GPU variant display names.
        """
        mode = self._get_current_stt_mode()

        if mode == "remote" and self._remote_stt_launcher:
            providers = self._remote_stt_launcher.get_providers()
            result = {}
            for p in providers:
                if p["name"] == "zipformer":
                    # Expand zipformer into CPU and GPU variants
                    result["zipformer_cpu"] = "Zipformer CPU"
                    result["zipformer_gpu"] = "Zipformer GPU"
                else:
                    result[p["name"]] = p["display_name"]
            return result

        # In-process mode: use hardcoded display names
        return {
            "azure": "Azure Speech",
            "google": "Google Cloud",
        }

    # -- AI Model state --

    def _get_current_ai_provider(self) -> str | None:
        """Get the currently-selected AI model id (thin-client coordinator).

        Reads the cached selected model from AIService (set by set_model)
        rather than the legacy ai.provider / ai.active_model config keys
        (design 5.3). Falls back to the configured [ai.server].model when the
        service is not yet wired so the menu still shows a sensible check.
        """
        if self._ai_service is not None:
            cached = getattr(self._ai_service, "_model_name", "")
            if cached:
                return cached
        return self.config_service.get("ai.server.model", "") or None

    def _get_available_ai_providers(self) -> list[str]:
        """Build the AI Model menu list with the explicit three-way branch
        (decision 29, spec 5.3/5.4).

        (a) [ai] enabled is false (master kill switch) -> the
            __ai_disabled__ sentinel so the GUI renders a non-selectable
            "AI disabled" placeholder, matching AIService._ai_off() behaviour
            (finding wh-ay6h.10.8).
        (b) [ai.server] unconfigured (no base_url) -> a single
            __ai_unconfigured__ sentinel so the menu can render a non-
            selectable 'AI not configured' placeholder.
        (c) [ai.server] configured but ai.server.enabled is false -> the
            __ai_disabled__ sentinel so the GUI renders a non-selectable
            "AI disabled" placeholder.
        (d) [ai.server] configured and enabled -> the kind-aware live list:
              local: the live model list from the most recent refresh, with the
                     configured model always included (so the current selection
                     is selectable even before the first refresh lands).
              cloud: the configured model only (a cloud endpoint has no useful
                     live list).
        """
        if not self.config_service.get("ai.enabled", True):
            return ["__ai_disabled__"]

        base_url = self.config_service.get("ai.server.base_url", "")
        if not base_url:
            return ["__ai_unconfigured__"]

        configured_model = self.config_service.get("ai.server.model", "")

        if not self.config_service.get("ai.server.enabled", True):
            # Configured but disabled: return the __ai_disabled__ sentinel so
            # the GUI renders a non-selectable placeholder.  Returning the real
            # model name here caused it to appear as a fully-enabled, clickable
            # menu item (finding wh-ay6h.6.7).
            return ["__ai_disabled__"]

        kind = self.config_service.get("ai.server.kind", "local")
        if kind == "local":
            live: list[str] = []
            if self._ai_service is not None and hasattr(self._ai_service, "cached_models"):
                live = list(self._ai_service.cached_models())
            # Always include the configured model so the current selection is
            # selectable even before the first live refresh.
            if configured_model and configured_model not in live:
                live.append(configured_model)
            return live
        # cloud: configured model only.
        return [configured_model] if configured_model else []

    def _get_ai_provider_display_names(self) -> dict[str, str]:
        """Get display name mapping for AI providers.

        The thin-client redesign (design 5.2) replaced the eager-load model
        registry (AIService.available_models / get_model_by_id) with a plain
        string cache (AIService.cached_models). Plain model IDs have no
        separate display_name attribute; the GUI fallback in
        _get_ai_provider_display_name already converts unknown IDs to a
        Title-cased string, so no per-model override is needed here.
        The sentinel keys are mapped explicitly so gui.py has them if it
        reaches the dict before its own hardcoded sentinel branches
        (finding wh-ay6h.10.3).
        """
        return {
            "openai": "Google Flash",
            "__ai_unconfigured__": "AI not configured",
            "__ai_disabled__": "AI disabled",
        }
