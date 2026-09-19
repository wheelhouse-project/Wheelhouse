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
import math
import threading
from multiprocessing import Queue
from typing import Dict, Any, Optional, TYPE_CHECKING

from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import (
    SonosStateChangedEvent, AudioStateChangedEvent,
    SystemConfigurationErrorEvent, SystemIdleStateChangedEvent,
    WakeWordDetectedEvent, PTTStartedEvent, PTTStoppedEvent,
    PTTMuteStateEvent,
)
from services.wheelhouse.ai.server_kind import LOCAL, normalize_server_kind
from services.wheelhouse.integrations.websocket_manager import WebSocketManager
from services.wheelhouse.utils.speech_notifier import SpeechNotifier
from services.wheelhouse.config_service import DEFAULT_STT_PROVIDER

if TYPE_CHECKING:
    from services.wheelhouse.config_service import ConfigService

logger = logging.getLogger(__name__)

# How long a push-to-talk hold may ignore audio suppression before the volume
# plugin has confirmed the speakers are silenced. Long enough for the mute,
# which runs in a worker thread and takes two Core Audio calls, and short
# enough that a hold over unmuted speakers cannot transcribe much of what they
# are playing.
DEFAULT_PTT_MUTE_CONFIRM_SECONDS = 0.5

# How long a hold runs before it ends itself, when the release never arrives.
DEFAULT_PTT_SAFETY_TIMEOUT_SECONDS = 30

# How long after a hold over playing sound before the speech engine is told
# the audio monitor's answer again (wh-ptt-release-disables-speech.1.1).
#
# The ending itself cannot tell a mid-hold silence report caused by its own
# mute from the music really stopping, so it tells the engine off and this
# wait sends whatever the monitor has measured since. It has to outlast the
# volume restore and one monitor poll. The restore is ONE Core Audio call,
# SetMasterVolumeLevel, measured at a median of 0.028 ms over thirty runs on
# this machine with the slowest at 0.039 ms. (The count of two above belongs
# to the MUTE, which reads the level and then writes it; an earlier version of
# this comment carried that count down to the restore, where it is wrong.)
# handlers/audio_monitor.py polls every 0.1 seconds after a silence
# measurement, so half a second is five of those poll intervals and about
# twelve thousand times the measured restore.
#
# That margin is large against the work it was measured over, and no wider
# claim follows from it. The measurement bounds one Core Audio call. It is not
# evidence about a loaded machine, and it says nothing about the scheduling
# path, for the reason the paragraph below gives. Read this wait as a
# best-effort fallback that errs in the safe direction, not as a guarantee.
# Half a second is also the whole delay a user can see when the sound really
# did stop during the hold.
#
# crewcut: a fixed wait stands in for asking the monitor to take one fresh
# measurement and publish it unconditionally after the restore completes. That
# needs an event class, a handler in handlers/audio_monitor.py and a publish in
# plugins/system_volume_plugin.py, which is work across three components. The
# work bead is wh-ptt-audio-recheck-handoff.
#
# Two cases the fixed wait does not cover, both reported by codex round 4 as
# wh-ptt-release-disables-speech.1.10 and deferred to that bead. First,
# playback that STARTS after ptt_start took its snapshot: the snapshot is
# False, so the ending sends self.speech_enabled and arms no wait at all,
# and the restore then makes the still-playing speakers audible. Second,
# this wait is not causally ordered after the restore it is meant to
# outlast: ptt_stop schedules it independently of SystemVolumePlugin, which
# restores behind _ptt_audio_lock and an asyncio.to_thread call and reports
# no completion, so a restore delayed past this wait lets the timer read the
# mute's own silence. The measurement above bounds the restore's one Core
# Audio call; it bounds neither the scheduling path nor a loaded machine. Both
# windows are transient and self-correct on the monitor's next change
# publication.
PTT_AUDIO_RECHECK_SECONDS = 0.5

# The range each push-to-talk timer setting has to fall inside. The lower
# bound on the mute wait is what stops it expiring before the two Core Audio
# calls can finish; the upper bound is how long the recogniser may listen over
# speakers that were never muted before the protection stops meaning anything.
#
# One second for that upper bound, measured rather than guessed. On this
# machine the two Core Audio calls the mute makes -- GetMasterVolumeLevel then
# SetMasterVolumeLevel -- took a median of 0.028 ms over thirty runs, with the
# slowest at 0.039 ms. One second is about thirty-five thousand times that, so
# it covers the work with room for a loaded machine and a slow device, while
# still being short enough that a hold over speakers that were never muted
# cannot transcribe a whole sentence of what they are playing. A larger value
# buys nothing for the mute and only extends that exposure, and the setting is
# documented as a confirmation wait, so nothing tells a user that raising it is
# permission to listen over live speakers (wh-ptt-audio-override.1.8).
#
# The safety cutoff cannot end an ordinary hold, so its lower bound is one
# second, and it has to fire within a session for a lost release to recover,
# so its upper bound is five minutes.
MIN_PTT_MUTE_CONFIRM_SECONDS = 0.05
MAX_PTT_MUTE_CONFIRM_SECONDS = 1.0
MIN_PTT_SAFETY_TIMEOUT_SECONDS = 1.0
MAX_PTT_SAFETY_TIMEOUT_SECONDS = 300.0

# Every text below is one Windows notification. utils/notice_text.py caps a
# message at 255 wide characters and a title at 63, because a longer notice
# never appears at all; all of these are far under both.
NOTICE_TITLE = "Wheelhouse"

SPEECH_OFF_NOTICE = "Speech is off. You switched it off."


def _seconds_setting(value, default: float, name: str, lowest: float, highest: float) -> float:
    """Return value as a usable number of seconds, or default.

    ConfigService returns whatever the settings file held, so a user who
    quotes the number gets a string, and asyncio's call_later raises TypeError
    on it. ptt_start switches the hold on before it creates its timers, so a
    raise there used to leave a hold running with no wait to withdraw the
    audio override and no safety cutoff to end it (wh-ptt-audio-override.1.2).

    A value can also be a real number and still be useless as a duration. TOML
    writes inf and nan as ordinary floats, and neither compares as less than
    or equal to zero (wh-ptt-audio-override.1.3). A finite 1e308 is accepted
    by asyncio but never expires during any real run, a value below the event
    loop's clock resolution expires before the mute can finish, and a whole
    number too large for a float makes math.isfinite itself raise
    OverflowError (wh-ptt-audio-override.1.5). Every one of those leaves a
    hold with a timer that cannot do its job, so the value must fall inside
    the range the caller documents.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            f"[PTT] {name} must be a number, got {value!r}; using {default} instead"
        )
        return float(default)
    # math.isfinite raises OverflowError on an int too large for a float, and
    # a Python int is always finite, so only a float needs this check.
    if isinstance(value, float) and not math.isfinite(value):
        logger.warning(
            f"[PTT] {name} must be a finite number, got {value!r}; "
            f"using {default} instead"
        )
        return float(default)
    # Comparing an arbitrarily large int against a float is exact in Python
    # and cannot overflow, so this check is safe for any accepted type.
    if value < lowest or value > highest:
        logger.warning(
            f"[PTT] {name} must be between {lowest} and {highest} seconds, "
            f"got {value!r}; using {default} instead"
        )
        return float(default)
    return float(value)


class StateManager:
    """Manages the application's dynamic state and GUI communication."""

    def __init__(self, config_service: "ConfigService", event_bus: EventBus, loop: asyncio.AbstractEventLoop, state_to_gui_queue: Queue, websocket_manager: Optional[WebSocketManager]):
        self.config_service = config_service
        self.event_bus = event_bus
        self.loop = loop
        self.state_to_gui_queue = state_to_gui_queue
        # Straight to the backing field: the property setter below reads
        # state this constructor has not computed yet. main.py passes None
        # here and attaches the real manager through the property.
        self._websocket_manager = websocket_manager

        # State variables
        self._speech_enabled = self.config_service.get("SPEECH_ENABLED_ON_STARTUP", False)
        self._speech_suppressed_by_audio = False
        self._speech_suppressed_by_sonos = False
        self._speech_suppressed_by_idle = False
        # Whether sound this computer plays pauses listening this session.
        # LogicController.main replaces this with the startup decision before
        # any service runs (apply_audio_suppression_decision). True until
        # then, so a report that somehow arrives first is treated the way
        # WheelHouse treated every report before wh-audio-suppression-auto.
        self._audio_suppression_active = True
        self._speech_enabled_before_idle = None  # Track state before idle
        self.interim_results_enabled = True  # Whether STT sends partial results
        # The level main.py already applied decides this, not a literal.
        # setup_logging (main.py:11635) sets the root level from the settings
        # file before this constructor runs (main.py:11661), so a literal
        # False published "not DEBUG" even when LOG_LEVEL = "DEBUG" had put
        # the root logger at DEBUG. Both menus draw the Debug entry's
        # checkmark from this flag, so the entry stood unchecked while
        # detailed logging was already on, and the first click ran
        # toggle_log_level, which turns DEBUG into INFO (main.py:2226) and
        # switched detailed logging off. This is the same comparison
        # main.py:2242 uses after every toggle.
        # Finding wh-audio-suppression-floating-menu.1.1.
        self.debug_mode = logging.getLogger().getEffectiveLevel() == logging.DEBUG

        self.stt_websocket_connection: Optional[Any] = None
        self._remote_stt_launcher = None  # Reference to RemoteSTTLauncher for provider info
        # The remote provider actually started this run. Outranks config in
        # _get_current_stt_provider: a malformed stt section can make the
        # config unrepairable while a fallback engine runs.
        self._running_remote_stt_provider: str | None = None
        # Which LAUNCH of that provider the record is about. A name
        # cannot tell a launch that failed from a later launch of the
        # same provider, so a late report from the earlier one used to
        # clear or overwrite the record of the live one
        # (wh-launch-generation). None while the launcher cannot say.
        self._running_remote_stt_generation: int | None = None
        # True once a remote provider is known to have failed or died with
        # nothing in its place. Needed as well as the record above, because
        # an empty record falls back to the config value, which still names
        # the provider the user chose (wh-remote-stt-robustness).
        self._remote_stt_confirmed_stopped: bool = False
        # The launcher's startup-monitor thread reports a stopped provider
        # while the event loop records a replacement. Both setters read
        # and write the two fields above, so they take this lock and
        # cannot interleave (wh-remote-stt-robustness.1.2).
        # _get_current_stt_provider deliberately does NOT take it: it runs
        # from many call sites and from the three-second periodic updater,
        # and a reader that lands between the two writes gets one stale
        # answer that the next update corrects, not the lasting misreport
        # the pair exists to prevent.
        self._remote_stt_record_lock = threading.Lock()
        self._ai_service = None  # Reference to AIService for model discovery
        
        # Speech state notification system
        self.speech_notifier = SpeechNotifier(enabled=False)
        self._toggle_counter = 0

        # Push-to-talk state
        self._speech_interaction_mode = self.config_service.get("speech.interaction_mode", "toggle")
        if self._speech_interaction_mode == "push_to_talk":
            self._speech_enabled = False  # PTT starts idle, regardless of speech-on-start.
        self._ptt_active = False
        self._ptt_request_id = None
        self._ptt_safety_handle: Optional[asyncio.TimerHandle] = None
        # A hold mutes the speakers, so the sound the audio monitor last saw is
        # on its way out. This override lets the hold ignore audio suppression
        # without writing _speech_suppressed_by_audio, which the monitor owns.
        self._ptt_audio_override = False
        self._ptt_mute_confirm_handle: Optional[asyncio.TimerHandle] = None
        # Whether the connected provider reported a loaded wake-word
        # detector. False until a capabilities frame says otherwise, because
        # a notice must never promise a wake word that cannot fire.
        self._wake_word_available = False
        # What the hold started over, and whether the plugin confirmed a mute
        # of the endpoint the audio monitor meters. A mid-hold silence report
        # may be that mute rather than the music ending, so the release must
        # distrust it (wh-ptt-release-disables-speech.1.1).
        self._audio_playing_at_ptt_start = False
        self._ptt_mute_confirmed = False
        self._ptt_audio_recheck_handle: Optional[asyncio.TimerHandle] = None
        # Every hold gets the next number. The mute runs in a worker thread, so
        # its report can arrive after the user released the button and pressed
        # it again; the number is how a late report is told apart from the
        # running hold's own report (wh-ptt-audio-override.1.1).
        self._ptt_hold_id = 0

        # Subscribe to events
        self.event_bus.subscribe(SonosStateChangedEvent, self._handle_sonos_state_changed)
        self.event_bus.subscribe(AudioStateChangedEvent, self._handle_audio_state_changed)
        self.event_bus.subscribe(SystemConfigurationErrorEvent, self._handle_system_config_error)
        self.event_bus.subscribe(SystemIdleStateChangedEvent, self._handle_idle_state_changed)
        self.event_bus.subscribe(WakeWordDetectedEvent, self._handle_wake_word_detected)
        self.event_bus.subscribe(PTTMuteStateEvent, self._handle_ptt_mute_state)

    @property
    def websocket_manager(self) -> Optional[WebSocketManager]:
        """The manager the STT engine reads its transcription status from."""
        return self._websocket_manager

    @websocket_manager.setter
    def websocket_manager(self, manager: Optional[WebSocketManager]) -> None:
        self._websocket_manager = manager
        self._sync_engine_transcription_status()

    def _sync_engine_transcription_status(self) -> None:
        """Tell a newly attached manager the state the button already shows.

        The manager starts on a stored True default and only
        set_transcription_status changes it; every other call site of that
        setter sits in an event or timer handler. On a quiet startup none of
        them fires, so a provider connecting afterwards was told enabled True
        while the user interface showed idle, and the engine transcribed
        (wh-audit2-ptt-mode-review.1). Written against speech_enabled, not the
        push_to_talk mode, so the reported state follows the real one.
        """
        if self._websocket_manager is None:
            return
        enabled = self.speech_enabled
        message = self._websocket_manager.set_transcription_status(
            enabled, reason=None if enabled else "startup")
        # A provider that connected before the manager was attached holds the
        # stale value and would not otherwise be corrected.
        self.loop.create_task(self._websocket_manager.broadcast(message))

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
        """Re-enable transcription after a wake word ends the idle pause.

        The idle pause is cleared outright, because the wake word proves the
        user is there. A pause caused by sound playing is not ended here at
        all: the sound is still playing, the audio monitor owns that answer,
        and wh-audio-suppression-auto removed the bounded recovery window
        that used to override it, together with the one command that window
        existed to carry.
        """
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

    def set_wake_word_available(self, available: bool) -> None:
        """Record whether the connected provider has a wake-word detector.

        Only the provider knows whether its detector really loaded, so this
        is told to StateManager by the capabilities handler. Nothing reads
        the flag since the listening-paused notice was removed
        (wh-audio-pause-notice-repeats); it chose that notice's wording.
        """
        self._wake_word_available = bool(available)

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

    def apply_audio_suppression_decision(self, active: bool) -> None:
        """Record whether sound this computer plays pauses listening.

        LogicController.main calls this once, before the services start, so
        the decision is in place before the audio monitor reports anything.
        The stored default is True, which is the behaviour WheelHouse shipped
        before wh-audio-suppression-auto: sound pauses listening.

        This writes nothing to the settings file and publishes no state. It is
        not a user switch; the user's part is the ENABLE_AUDIO_SUPPRESSION
        value that decide_audio_suppression reads at startup.
        """
        self._audio_suppression_active = bool(active)

    @property
    def speech_enabled(self) -> bool:
        """Determines if speech processing should be active.

        :flow: Speech Suppression by Audio
        :step: 3
        :description: Computed property combining user toggle and audio suppression states
        :data_in: _speech_enabled and _speech_suppressed_by_audio flags
        :data_out: Boolean indicating final speech processing state
        :notes: Final checkpoint for audio-based suppression. Returns True only if user enabled speech AND not suppressed by system audio. The audio flag counts only while the startup decision is on (apply_audio_suppression_decision, set once from ENABLE_AUDIO_SUPPRESSION and what Windows reports about the microphone).

        :flow: Speech Suppression by Sonos
        :step: 5
        :description: Computed property combining all suppression states (audio + Sonos + idle)
        :data_in: _speech_enabled, _speech_suppressed_by_audio, _speech_suppressed_by_sonos, _speech_suppressed_by_idle flags
        :data_out: Boolean indicating final authoritative speech state
        :notes: Final checkpoint combining all suppression logic. Returns True only if user enabled AND not suppressed by system audio AND not suppressed by Sonos AND not suppressed by idle. Respects the ENABLE_SONOS_SUPPRESSION and ENABLE_IDLE_SUPPRESSION config flags; the audio flag counts only while the startup decision is on (apply_audio_suppression_decision). One override sits beside the audio flag and switches listening back on without writing it: a push-to-talk hold, which mutes the speakers. Read by WheelHouseApp.handle_transcribed_text in main.py, which drops an arriving transcript while this is False, and throughout this module to fill the engine's transcription status and the 'speech_enabled' key the GUI reads; SpeechProcessor does not consult it (grep for 'speech_enabled' and 'state_manager' in services/wheelhouse/speech/speech_processor.py found no hits).

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
            self._audio_suppression_active and
            not self._ptt_audio_override
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
            self._check_remote_stt_health()
        except Exception as e:
            logger.warning(f"Could not check speech provider health: {e}")
        try:
            state = {
                'action': 'state_update',
                'speech_enabled': self.speech_enabled,
                'button_visible': self.config_service.get('FLOATING_BUTTON_VISIBLE', True),
                'FLOATING_BUTTON_SIZE': self.config_service.get('FLOATING_BUTTON_SIZE', 50),
                'FLOATING_BUTTON_POS': self.config_service.get('FLOATING_BUTTON_POS', [100, 100]),
                'SHOW_SPEECH_PULSE': self.config_service.get('SHOW_SPEECH_PULSE', True),
                'settings_persisted': {
                    key: self.config_service.get_persisted(key, default)
                    for key, default in (
                        ('FLOATING_BUTTON_SIZE', 50),
                        ('FLOATING_BUTTON_POS', [100, 100]),
                        ('FLOATING_BUTTON_VISIBLE', True),
                        ('SHOW_SPEECH_PULSE', True),
                    )
                },
                'stt_provider': self._get_current_stt_provider(),
                'stt_providers_available': self._get_available_stt_providers(),
                'stt_provider_display_names': self._get_provider_display_names(),
                'interim_results_enabled': self.interim_results_enabled,
                'debug_mode': self.debug_mode,
                'speech_interaction_mode': self._speech_interaction_mode,
                'ptt_active': self._ptt_active,
                'ptt_request_id': self._ptt_request_id,
                'ptt_refusal_reason': self._ptt_refusal_reason(),
                'ai_provider': self._get_current_ai_provider(),
                'ai_providers_available': self._get_available_ai_providers(),
                'ai_provider_display_names': self._get_ai_provider_display_names(),
            }
            self.state_to_gui_queue.put_nowait(state)
        except Exception as e:
            logger.error(f"Failed to send state update to GUI: {e}")

    def _set_speech_enabled_explicitly(self, value: bool):
        """Write the speech setting on behalf of an explicit user decision.

        ``ptt_stop`` puts back the value that was there before the hold. That
        is right for a hold on its own, but a hold is not a modal state: the
        shipped voice pattern "push to talk mode" reaches
        ``set_speech_interaction_mode`` while the button is still held, and
        nothing in ``services/wheelhouse/speech/`` knows a hold is running.
        Without this, the release would put back the pre-hold value and
        silently undo the switch the user just spoke
        (wh-ptt-release-disables-speech.1.2).

        So every explicit decision made during a hold also moves the saved
        value forward, and the release then restores the newest decision
        rather than a stale one.
        """
        self._speech_enabled = value
        if self._ptt_active:
            self._speech_before_ptt = value

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
            if self._speech_interaction_mode == "push_to_talk":
                self.set_speech_interaction_mode("toggle")
            self._set_speech_enabled_explicitly(True)
            self._speech_suppressed_by_audio = False
            self._speech_suppressed_by_sonos = False
            self._speech_suppressed_by_idle = False

            self.speech_notifier.notify_debug(f"Toggle #{self._toggle_counter}: Enabling speech (was disabled)")
            logger.info(f"[USER TOGGLE] Speech ENABLED by user. Cleared all suppression. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos}, idle_suppressed={self._speech_suppressed_by_idle}")
            
        else:
            # Speech is currently enabled - disable it
            self._set_speech_enabled_explicitly(False)

            self.speech_notifier.notify_debug(f"Toggle #{self._toggle_counter}: Disabling speech (was enabled)")
            logger.info(f"[USER TOGGLE] Speech DISABLED by user. State: user_enabled={self._speech_enabled}, audio_suppressed={self._speech_suppressed_by_audio}, sonos_suppressed={self._speech_suppressed_by_sonos}, idle_suppressed={self._speech_suppressed_by_idle}")

        # Send appropriate notification
        new_speech_enabled = self.speech_enabled
        if new_speech_enabled and not old_speech_enabled:
            self.speech_notifier.notify_speech_enabled("User toggle", "All suppression cleared")
        elif not new_speech_enabled and old_speech_enabled:
            self.speech_notifier.notify_speech_disabled("User toggle")
            # The user's own speech-off. A hands-free user who switched
            # listening off by voice or by the button gets no other sign that
            # the microphone shut, and the same silence follows an automatic
            # pause, so the notice says which of the two this was.
            self.speech_notifier._send_notification(NOTICE_TITLE, SPEECH_OFF_NOTICE)
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

    def _ptt_refusal_reason(self) -> str:
        """Describe the existing suppression decision; never change that decision."""
        if not self._ptt_active or self.speech_enabled:
            return ""
        if self._speech_suppressed_by_sonos and self.config_service.get("ENABLE_SONOS_SUPPRESSION", True):
            return "Sonos is playing. Pause Sonos before using push to talk."
        if (self._speech_suppressed_by_audio and not self._ptt_audio_override
                and self._audio_suppression_active):
            return "System audio is suppressing listening. Pause it before using push to talk."
        if self._speech_suppressed_by_idle and self.config_service.get("ENABLE_IDLE_SUPPRESSION", True):
            return "Listening is paused because the computer is idle."
        return "Speech is disabled."

    def ptt_start(self, source: str = "floating_button", request_id: str | None = None):
        """Activate push-to-talk: enable speech, clear idle, mute audio, start safety timeout."""
        if self._ptt_active:
            # A restarted GUI needs an acknowledgement for its new request.
            # Preserve the existing hold, safety deadline, and mute lifecycle.
            if request_id is not None:
                self._ptt_request_id = request_id
            self.send_state_update()
            return

        # Read and check both timer settings before anything is switched on. A
        # bad value used to raise inside call_later, after the hold was already
        # live, which left it with no wait and no safety cutoff
        # (wh-ptt-audio-override.1.2).
        confirm_seconds = _seconds_setting(
            self.config_service.get(
                "speech.ptt_mute_confirm_seconds", DEFAULT_PTT_MUTE_CONFIRM_SECONDS
            ),
            DEFAULT_PTT_MUTE_CONFIRM_SECONDS,
            "speech.ptt_mute_confirm_seconds",
            MIN_PTT_MUTE_CONFIRM_SECONDS,
            MAX_PTT_MUTE_CONFIRM_SECONDS,
        )
        timeout_seconds = _seconds_setting(
            self.config_service.get(
                "speech.ptt_safety_timeout_seconds", DEFAULT_PTT_SAFETY_TIMEOUT_SECONDS
            ),
            DEFAULT_PTT_SAFETY_TIMEOUT_SECONDS,
            "speech.ptt_safety_timeout_seconds",
            MIN_PTT_SAFETY_TIMEOUT_SECONDS,
            MAX_PTT_SAFETY_TIMEOUT_SECONDS,
        )

        self._speech_before_ptt = self._speech_enabled  # Save for drag cancel restore
        self._ptt_active = True
        self._ptt_request_id = request_id
        self._ptt_hold_id += 1
        self._speech_enabled = True
        # Holding the button proves the user is at the machine, so the idle
        # pause ends.
        self._speech_suppressed_by_idle = False

        # The hold mutes this computer's speakers, so audio suppression should
        # not silence the user for sound that is about to stop. The setting
        # itself is NOT written here: the audio monitor owns it, reports only
        # when its answer changes, and polls every 10 seconds while sound
        # plays, so a hold shorter than one poll would leave a cleared setting
        # with nothing to put it back (wh-button-drag-resize.1.26, reverted in
        # b73030c8). The override below is separate state that this method
        # creates and every ending destroys, so the observed setting stays
        # exactly what the monitor last measured.
        #
        # The override is applied at once, because waiting for the mute would
        # add delay at the one moment the user wants none. It is withdrawn
        # again unless the volume plugin reports the speakers really were
        # silenced, which covers a failed mute, an absent audio device, and a
        # plugin that is not loaded at all and answers nothing.
        #
        # Sonos suppression is deliberately untouched: muting this computer
        # cannot silence a speaker in another room.
        self._ptt_audio_override = True
        if self._ptt_mute_confirm_handle:
            self._ptt_mute_confirm_handle.cancel()
        self._ptt_mute_confirm_handle = self.loop.call_later(
            confirm_seconds, self._ptt_mute_unconfirmed
        )

        # What the monitor last measured, kept for the ending to read. From
        # here on the monitor may be metering speakers this hold turned down,
        # so its answer stops being evidence about the music
        # (wh-ptt-release-disables-speech.1.1). The mute is not confirmed yet;
        # ptt_confirm_mute records that.
        self._audio_playing_at_ptt_start = self._speech_suppressed_by_audio
        self._ptt_mute_confirmed = False
        # A previous ending's re-check would land inside this hold and tell
        # the engine an answer this hold has already replaced.
        if self._ptt_audio_recheck_handle:
            self._ptt_audio_recheck_handle.cancel()
            self._ptt_audio_recheck_handle = None

        logger.info(f"[PTT] Push-to-talk started (source={source})")

        # Publish event for audio muting
        self.loop.create_task(
            self.event_bus.publish(
                PTTStartedEvent(source=source, hold_id=self._ptt_hold_id)
            )
        )

        # Tell the speech engine exactly what send_state_update is about to show
        # the user. Sending a flat True here would start the engine listening
        # while the button shows speech off, whenever Sonos is still playing.
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(
                self.speech_enabled, reason="ptt"
            )
            self.loop.create_task(self.websocket_manager.broadcast(message))

        # Start safety timeout
        if self._ptt_safety_handle:
            self._ptt_safety_handle.cancel()
        self._ptt_safety_handle = self.loop.call_later(
            timeout_seconds, self._ptt_safety_timeout
        )

        self.send_state_update()

    def ptt_stop(self, reason: str = "released"):
        """End push-to-talk: put the speech setting back, restore audio.

        Every ending restores the setting saved at ptt_start: the release,
        both cancellations, and the safety cutoff. The one-line summary said
        "disable speech" until wh-ptt-release-disables-speech.1.9; that
        described the contract 5719a1fa removed.
        """
        if not self._ptt_active:
            # The hold already ended -- almost always the safety timeout ending
            # one the user never released. The interface still believes it has
            # a hold, and on a cancellation it puts speech back to what it was
            # before the hold, which would show the microphone open while it is
            # shut. Send the real state instead of returning silently. Nothing
            # else changes here: no event is published and speech is not
            # touched, because there is no hold to end.
            self.send_state_update()
            return

        # Cancel safety timeout
        if self._ptt_safety_handle:
            self._ptt_safety_handle.cancel()
            self._ptt_safety_handle = None

        # The hold is over, so the reason to ignore audio suppression is over
        # with it. Every ending passes through here, including the safety
        # cutoff and both cancellations.
        if self._ptt_mute_confirm_handle:
            self._ptt_mute_confirm_handle.cancel()
            self._ptt_mute_confirm_handle = None
        self._ptt_audio_override = False

        self._ptt_active = False
        # A hold borrows the microphone for its own length. It does not decide
        # whether speech is on afterwards, so every ending puts back exactly
        # the value that was there before the hold and lets audio suppression,
        # Sonos suppression and the idle pause decide the rest.
        #
        # This covers the ordinary release, the two cancellations -- a drag,
        # or a press whose release was taken by the context menu or by the
        # button being hidden -- and the safety cutoff.
        #
        # wh-ptt-release-disables-speech: the ordinary release used to force
        # this to False. A hold that began while speech was on but
        # audio-suppressed therefore ended with the user's own setting
        # overwritten, and when the audio monitor lifted suppression ten
        # seconds later there was no enabled setting left to restore. The user
        # had to click the button to get listening back (David, 2026-08-27;
        # wheelhouse.log.3, 11:48:48 to 11:49:18, with no [USER TOGGLE] line
        # in between).
        #
        # The safety cutoff was deliberately excluded from that restore and
        # kept forcing speech off, on the reading that a hold the user never
        # released carried no decision to honour. David ruled on 2026-08-27
        # that a lost release is not a decision to switch speech off either,
        # so the cutoff now restores like every other ending. What still makes
        # it safe is that the restore cannot leave a hold running: _ptt_active
        # is already False above, the audio override is already withdrawn, and
        # the restored value is the user's own setting, which every
        # suppression source still applies to.
        #
        # In push-to-talk mode speech is off between holds, so the restored
        # value is False there and that mode is unchanged.
        self._speech_enabled = getattr(self, '_speech_before_ptt', False)

        logger.info(f"[PTT] Push-to-talk stopped (reason={reason})")

        # Publish event for audio restore
        self.loop.create_task(self.event_bus.publish(PTTStoppedEvent(reason=reason)))

        # Tell the speech engine what the ending really leaves behind. Every
        # ending puts speech back to what it was, which can be on, and sending
        # a flat False would leave the button showing an open microphone that
        # cannot hear anything. Reading the property rather than a raw value
        # keeps every suppression that is still recorded in force.
        #
        # The property is not evidence that the speakers are silent, though.
        # SystemVolumePlugin confirms a mute only when its cached volume
        # interface belongs to the multimedia endpoint AudioMonitor meters.
        # This includes a communications role that names the same device,
        # and a fallback to the default. A distinct or unverifiable endpoint
        # refuses PTT muting and withdraws the override through the existing
        # failure response (wh-ptt-comms-endpoint-mismatch).
        # A monitor poll inside a confirmed hold can report silence caused by
        # the mute rather than by the music stopping; the monitor publishes
        # only changed answers (wh-ptt-release-disables-speech.1.1).
        #
        # Nothing here can tell that report apart from the music really
        # ending, so a hold that began over playing sound with the mute
        # confirmed stops trusting it and tells the engine off. That is the
        # safe half of the answer and it costs the release no time. The other
        # half is _ptt_audio_recheck below: if the sound really did stop, the
        # monitor is already at silence and will never publish again, so the
        # re-check is what tells the engine to listen.
        #
        # send_state_update still shows the property. Only what the engine is
        # told changes here, so for that half second the button can show
        # listening while the engine is off. That is the safe direction, and
        # the button is corrected by whichever of the two answers arrives.
        audio_answer_is_the_holds_own_mute = (
            self._audio_playing_at_ptt_start
            and self._ptt_mute_confirmed
            and self._audio_suppression_active
        )
        told_the_engine = (
            False if audio_answer_is_the_holds_own_mute else self.speech_enabled
        )
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(
                told_the_engine, reason="ptt"
            )
            self.loop.create_task(self.websocket_manager.broadcast(message))

        if self._ptt_audio_recheck_handle:
            self._ptt_audio_recheck_handle.cancel()
            self._ptt_audio_recheck_handle = None
        if audio_answer_is_the_holds_own_mute:
            self._ptt_audio_recheck_handle = self.loop.call_later(
                PTT_AUDIO_RECHECK_SECONDS, self._ptt_audio_recheck
            )

        self.send_state_update()

    async def _handle_ptt_mute_state(self, event: PTTMuteStateEvent):
        """Handles the PTTMuteStateEvent published by SystemVolumePlugin."""
        self.ptt_confirm_mute(event.muted, event.hold_id, reason=event.reason)

    def ptt_confirm_mute(self, muted: bool, hold_id: int, reason: str = ""):
        """Record whether the speakers really were silenced for one hold.

        A confirmed mute lets that hold keep ignoring audio suppression. A
        failed mute withdraws that at once, so the microphone stops feeding
        the recogniser with whatever the speakers are still playing.

        This can only withdraw the override, never grant it. An answer that
        arrives after the wait expired, or after the hold ended, therefore
        cannot start the recogniser listening again.

        :param hold_id: The hold this report answers. The mute runs in a worker
            thread, so a report can arrive after the user released the button
            and pressed it again. Acting on that report would either cancel the
            new hold's wait, removing the only thing that bounds how long it
            listens over unmuted speakers, or withdraw the new hold's override
            for its whole length. Reports for any other hold are ignored
            (wh-ptt-audio-override.1.1).
        """
        if not self._ptt_active or hold_id != self._ptt_hold_id:
            return

        if self._ptt_mute_confirm_handle:
            self._ptt_mute_confirm_handle.cancel()
            self._ptt_mute_confirm_handle = None

        if muted:
            # The plugin verified its volume interface against the monitored
            # multimedia endpoint before muting. Its acknowledgement covers
            # that endpoint, so the ending must distrust mid-hold silence.
            self._ptt_mute_confirmed = True
            logger.debug("[PTT] Mute confirmed; the hold keeps listening over audio")
            return

        logger.warning(
            f"[PTT] The speakers were not muted (reason={reason or 'unknown'}); "
            "audio suppression applies to this hold"
        )
        self._withdraw_ptt_audio_override()

    def _ptt_mute_unconfirmed(self):
        """No answer arrived about the mute, so stop trusting it.

        The usual cause is the volume plugin not being loaded, in which case
        nothing publishes an answer at all.
        """
        self._ptt_mute_confirm_handle = None
        if not self._ptt_audio_override:
            return
        logger.warning(
            "[PTT] No answer about the mute; audio suppression applies to this hold"
        )
        self._withdraw_ptt_audio_override()

    def _withdraw_ptt_audio_override(self):
        """Stop ignoring audio suppression, and tell the engine and the user."""
        if not self._ptt_audio_override:
            return
        self._ptt_audio_override = False

        # The speech engine was told the microphone was open. Correct that
        # before the user speaks into a hold that cannot hear them.
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(
                self.speech_enabled, reason="ptt"
            )
            self.loop.create_task(self.websocket_manager.broadcast(message))

        self.send_state_update()

    def _ptt_audio_recheck(self):
        """Send the engine the computed speech state again after a fixed delay.

        ptt_stop tells the engine off when the hold's own mute may be what
        cleared audio suppression. That is right when the music resumes, and
        wrong when the music genuinely stopped during the hold: the monitor is
        then already at silence, publishes only on a change, and would never
        say anything again, so the engine would stay switched off with no
        setting left to bring it back -- the very defect
        wh-ptt-release-disables-speech reported.

        The wait AIMS to outlast the volume restore and one monitor poll, so
        that in the ordinary case the property rests on a measurement of the
        restored speakers. It does not guarantee that, and an earlier version
        of this docstring stated it as fact. Nothing orders this callback
        after the restore: ptt_stop schedules it with loop.call_later, while
        SystemVolumePlugin._handle_ptt_stopped restores behind _ptt_audio_lock
        in a worker thread and reports no completion to StateManager. A
        restore delayed past this wait lets the callback read the silence the
        hold's own mute produced. wh-ptt-audio-recheck-handoff carries the
        causal fix: one fresh measurement published unconditionally after the
        restore completes. Only the engine is told; the setting and
        _speech_suppressed_by_audio are untouched.
        """
        self._ptt_audio_recheck_handle = None
        if self._ptt_active:
            # A new hold started and has already told the engine its own
            # answer. This one is stale.
            return
        if self.websocket_manager:
            message = self.websocket_manager.set_transcription_status(
                self.speech_enabled, reason="ptt"
            )
            self.loop.create_task(self.websocket_manager.broadcast(message))

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
            self._set_speech_enabled_explicitly(False)
            logger.info("[MODE] Speech disabled on interaction mode change")
            self.speech_notifier.notify_speech_disabled("Mode switch")
            if self.websocket_manager:
                message = self.websocket_manager.set_transcription_status(False, reason="manual")
                self.loop.create_task(self.websocket_manager.broadcast(message))

        # Persist to config. Nothing waits for this write, so a failure would
        # otherwise pass without a trace: report it here or the next start
        # silently comes back in the previous mode.
        self.config_service.set("speech.interaction_mode", mode)

        async def save_and_report():
            if not await self.config_service.save():
                logger.error(
                    f"[MODE] Interaction mode {mode} was NOT saved; the next "
                    "start will use the previous mode"
                )

        self.loop.create_task(save_and_report())

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

    async def set_config_value(self, key: str, value: Any, request_id: str | None = None) -> bool:
        """Updates a configuration value and saves it.

        With a request ID, stage the write and acknowledge its disk outcome;
        failure leaves the confirmed live value intact. Legacy callers without
        an ID retain their in-memory-first behavior and receive the saved bool.
        """
        if request_id is not None:
            return await self._save_gui_settings({key: value}, request_id)
        logger.debug(f"Updating config: '{key}' = {value}")
        self.config_service.set(key, value)
        saved = await self.config_service.save()
        if not saved:
            logger.error(f"Config '{key}' changed in memory but was NOT saved to disk")
        self.send_state_update()
        return saved

    async def set_config_values(self, values: dict[str, Any], request_id: str | None = None) -> bool:
        """Updates several configuration values and saves them together.

        For settings that only make sense as a group. The floating button's
        size and position are the case this exists for: a resize changes both
        at once, because the button grows around its centre and so its stored
        corner moves with its diameter. Sending them as two separate changes
        would let one survive without the other, which puts a differently
        sized button at a corner belonging to the old size on the next start.

        A request ID selects the staged, acknowledged path; legacy callers
        receive only the saved bool, as in set_config_value.
        """
        if request_id is not None:
            return await self._save_gui_settings(values, request_id)
        if not values:
            return True
        logger.debug(f"Updating config group: {values}")
        for key, value in values.items():
            self.config_service.set(key, value)
        saved = await self.config_service.save()
        if not saved:
            logger.error(
                f"Config group {sorted(values)} changed in memory but was NOT saved to disk"
            )
        self.send_state_update()
        return saved

    async def _save_gui_settings(self, values: dict[str, Any], request_id: str) -> bool:
        """Serialize GUI groups and acknowledge the staged persistence result.

        The read reconciliation uses this same lock so it cannot report the
        tentative memory of a write whose disk outcome is still unknown.
        """
        if not hasattr(self, '_gui_settings_lock'):
            self._gui_settings_lock = asyncio.Lock()
        async with self._gui_settings_lock:
            saved = False
            try:
                saved = bool(await self.config_service.save(values=values)) if values else True
            except Exception:
                logger.exception('GUI settings save failed')
            # No await between save completion and this detached snapshot:
            # the request ID identifies this write, never a newer live edit.
            result = {key: self.config_service.get_persisted(key) for key in values}
            self._send_settings_result({
                'action': 'config_write_result', 'request_id': request_id,
                'saved': saved, 'values': result,
            })
            self.send_state_update()
            return saved

    def _send_settings_result(self, message: dict) -> None:
        try:
            self.state_to_gui_queue.put_nowait(message)
        except Exception:
            # The GUI's bounded timeout will request a read, never another write.
            logger.exception('Could not deliver settings result to GUI')

    async def get_config_values(self, keys: list[str], request_id: str) -> None:
        if not hasattr(self, '_gui_settings_lock'):
            self._gui_settings_lock = asyncio.Lock()
        async with self._gui_settings_lock:
            self._send_settings_result({
                'action': 'config_values_result', 'request_id': request_id,
                'values': {key: self.config_service.get_persisted(key) for key in keys},
            })

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

    def set_remote_stt_launcher(self, launcher) -> None:
        """Set reference to RemoteSTTLauncher for provider discovery."""
        self._remote_stt_launcher = launcher

    def _launch_generation(self, provider: str) -> int | None:
        """Ask the launcher which launch of `provider` is the latest.

        Every caller that records a running provider does so right after
        a successful start_provider on the same thread, so the latest
        stamp for that name is the launch being recorded. None when
        there is no launcher, or when it never started that provider --
        an unstamped record is compared by name alone, exactly as before
        (wh-launch-generation).
        """
        launcher = self._remote_stt_launcher
        if launcher is None:
            return None
        try:
            return launcher.launch_generation(provider)
        except Exception as e:
            logger.warning(f"Could not read the launch generation: {e}")
            return None

    def set_running_remote_stt_provider(self, provider: str) -> None:
        """Record the remote provider that actually started this run."""
        generation = self._launch_generation(provider)
        with self._remote_stt_record_lock:
            self._running_remote_stt_provider = provider
            self._running_remote_stt_generation = generation
            self._remote_stt_confirmed_stopped = False

    def _check_remote_stt_health(self) -> None:
        """Run the owned-process watchdog on Logic's existing state-update loop."""
        launcher = self._remote_stt_launcher
        if launcher is None:
            return
        with self._remote_stt_record_lock:
            provider = self._running_remote_stt_provider
            generation = self._running_remote_stt_generation
        if provider is None or generation is None:
            return

        def record_restart(new_generation):
            # Called under the launcher's stamp lock: record operations only.
            # No caller holds this record lock while acquiring that stamp lock.
            with self._remote_stt_record_lock:
                if (
                    self._running_remote_stt_provider != provider
                    or self._running_remote_stt_generation != generation
                ):
                    return False
                self._running_remote_stt_generation = new_generation
                return True

        launcher.check_provider_health(provider, generation, record_restart)

    def replace_stopped_remote_stt_provider(
        self, stopped: str, survivor: str, generation: int | None = None
    ) -> bool:
        """Name `survivor` only while `stopped` is still the record.

        The reconciliation in main.py scans providers and waits for an
        exit outside this lock, so the survivor it chose can be stale by
        the time it is written: a switch that lands during the wait
        records a newer engine, and discover_providers keeps unsorted
        directory order, so the engine the switch replaced can be the
        first match. Writing it back would leave the tray naming an
        engine that is not the active one, and nothing would correct it
        -- a ready signal only returns from its monitor
        (wh-remote-stt-robustness.2.7).

        The name alone is not enough. The switch away from `stopped` and
        back to it can complete during that wait rather than after it,
        so at this moment the record names `stopped` again -- a
        DIFFERENT launch of it, already transcribing. The name matches,
        this write SUCCEEDS, and the stale survivor lands on top of the
        live launch. That ordering goes through the success path, which
        is why a further name comparison could not close it; only the
        launch generation can (wh-launch-generation, from the ruling on
        wh-remote-stt-robustness.2.8).

        Args:
            stopped: The provider whose failure started the
                reconciliation. The write happens only while the record
                still names it.
            survivor: The provider found to be running instead.
            generation: The launch of `stopped` whose failure started
                the reconciliation. The write happens only while the
                record is still about that same launch. None means the
                caller cannot say, and the comparison falls back to the
                name alone.

        Returns:
            True when the record was replaced. False when a newer record
            won, in which case the caller must leave it alone.
        """
        survivor_generation = self._launch_generation(survivor)
        with self._remote_stt_record_lock:
            if self._running_remote_stt_provider != stopped:
                return False
            if not self._same_launch(generation):
                return False
            self._running_remote_stt_provider = survivor
            self._running_remote_stt_generation = survivor_generation
            self._remote_stt_confirmed_stopped = False
            return True

    def _same_launch(self, generation: int | None) -> bool:
        """True when `generation` is the launch the record is about.

        Caller must hold `_remote_stt_record_lock`. An unstamped record
        or an unstamped report cannot be told apart from the recorded
        launch, so both answer True and the comparison falls back to the
        name the caller already checked (wh-launch-generation).
        """
        recorded = self._running_remote_stt_generation
        if generation is None or recorded is None:
            return True
        return recorded == generation

    def set_remote_stt_stopped(
        self, provider: str | None = None, generation: int | None = None
    ) -> None:
        """Record that no remote speech engine is running.

        The startup monitor, the autostart path, a switch whose
        replacement fails to start, and a credentials restart that fails
        after a confirmed stop all end with nothing running. Without
        this the tray keeps a check mark on the engine that stopped:
        clearing the record alone is not enough, because
        _get_current_stt_provider then reads stt.last_provider, which
        still names the same provider (wh-remote-stt-robustness).

        Args:
            provider: The provider that stopped, when the caller knows
                it. A signal naming a provider other than the recorded
                running one is ignored -- a late monitor thread from an
                earlier engine must not blank the display for the engine
                that replaced it. Omit it when the caller means "nothing
                is running", whatever was recorded.
            generation: The launch that stopped, when the caller knows
                it. The name alone cannot stop a report from an earlier
                launch of the SAME provider from clearing the record of
                a restart of it, which is what the reconciliation's
                fall-through used to do (wh-launch-generation). None
                falls back to the name comparison alone.
        """
        # The post-ready owned-process watchdog also reaches this after
        # its single automatic retry fails. A websocket disconnect alone
        # never clears the record or triggers that recovery.
        # The lock covers the comparison as well as the two writes. A
        # monitor thread that compared before a replacement was recorded
        # and wrote afterwards would blank an engine that is running,
        # and nothing would correct it (wh-remote-stt-robustness.1.2).
        with self._remote_stt_record_lock:
            if (
                provider is not None
                and self._running_remote_stt_provider is not None
                and (
                    self._running_remote_stt_provider != provider
                    or not self._same_launch(generation)
                )
            ):
                return
            self._running_remote_stt_provider = None
            self._running_remote_stt_generation = None
            self._remote_stt_confirmed_stopped = True

    def set_ai_service(self, ai_service) -> None:
        """Set AIService reference for model discovery state."""
        self._ai_service = ai_service

    def _get_current_stt_provider(self) -> str | None:
        """Get the name of the remote provider the tray should show.

        The runtime record of the engine that actually started, and the
        stored choice only when no start has been recorded yet.
        wh-in-process-capture-removal deleted the STTManager arm this
        method used to fall through to.
        """
        if self._running_remote_stt_provider:
            return self._running_remote_stt_provider
        if self._remote_stt_confirmed_stopped:
            # A start failed or the engine died. Falling through to
            # the config here would name the provider the user chose
            # and show it as running (wh-remote-stt-robustness).
            return None
        # Return actual remote provider name from config
        return self.config_service.get("stt.last_provider", DEFAULT_STT_PROVIDER)

    def _get_available_stt_providers(self) -> list[str]:
        """Get list of available STT providers.

        Uses RemoteSTTLauncher to discover them. Before ServiceManager
        hands the launcher over there is nothing to discover, and there
        is no second place to look: the only provider this method ever
        produced from config was the cloud provider removed after
        release 1.0.7.
        """
        # Use the RemoteSTTLauncher cached providers
        if self._remote_stt_launcher:
            return [p["name"] for p in self._remote_stt_launcher.get_providers()]

        return []

    def _get_provider_display_names(self) -> dict[str, str]:
        """Get display name mapping for available providers.

        Returns a dict mapping provider name to display name.
        """
        if self._remote_stt_launcher:
            providers = self._remote_stt_launcher.get_providers()
            return {p["name"]: p["display_name"] for p in providers}

        # Before the launcher is handed over: the hardcoded name this
        # method has always answered with. wh-in-process-capture-removal
        # left the answer alone; only the mode test above it is gone.
        return {
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

        # Read through the same normalizer AIService uses, so the menu and the
        # inference path cannot disagree about what an unreadable value means.
        # They used to: this site asked "is it local?" and the provider-building
        # site asked "is it cloud?", so "remote" showed the cloud menu while the
        # provider was built as a local one (wh-ai-kind-validation). The
        # complaint is discarded here on purpose -- this method runs on every
        # tray-menu rebuild, and AIService says it once at startup.
        kind, _ = normalize_server_kind(
            self.config_service.get("ai.server.kind", None))
        if kind == LOCAL:
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
