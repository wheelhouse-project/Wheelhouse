"""WebSocket connection management for Speech-to-Text server communication.

This module manages WebSocket connections between the WheelHouse service and
the Google STT server, handling transcript reception, client connection
lifecycle, and message broadcasting. It provides centralized connection
state management and transcription control for the speech recognition pipeline.

Key Classes:
  - WebSocketManager: Central coordinator for STT WebSocket connections.

Key Features:
  - Multi-client WebSocket connection management
  - Transcript message broadcasting to connected clients
  - Transcription enable/disable state management
  - Connection lifecycle tracking with logging
  - Concurrent message delivery to all clients
  - Error handling for connection failures and disconnections

Message Flow:
  - Receives transcript messages from Google STT server
  - Broadcasts transcripts to all connected WheelHouse clients
  - Manages transcription state across the system
  - Handles client connect/disconnect events

Integration Points:
  - StateManager for transcription control
  - Speech processing pipeline for transcript delivery
  - WebSocket server for STT communication

Typical Usage:
  from integrations.websocket_manager import WebSocketManager
  
  ws_manager = WebSocketManager()
  
  # Client connection management
  ws_manager.add_client(websocket)
  
  # Broadcast transcript to all clients
  await ws_manager.broadcast({
      "type": "transcript",
      "text": "Hello world"
  })
  
  # Control transcription state
  ws_manager.transcription_enabled = False
"""
import asyncio
import json
import logging
import struct
import sys
import uuid
from datetime import datetime
from typing import Set, Dict, Any, Callable, Optional
import websockets
from multiprocessing import shared_memory
from shared.dialog_owner import launch_owner_token
from speech.word_event import WordEvent
from utils.trace_context import set_trace
from utils.redact import redact_transcript

logger = logging.getLogger(__name__)

# Keys whose values carry user content in broadcast/command payloads.
# Everything else (type, flags, log levels, ids) stays verbatim so the
# payload remains diagnosable with redaction on (wh-797.17.3).
_CONTENT_KEYS = frozenset({"hint", "text", "word", "words", "message", "transcript"})

# Per-target bound on a calibration mode-off send (wh-7ou.7.6.14). A
# stalled client's send can block on backpressure indefinitely, and the
# mode-off fan-out consumes its binding set before sending -- nothing
# ever retries a target this call abandons -- so a stall must cost at
# most this long, not forever. Generous next to a healthy send (which
# completes in milliseconds) and far below the 600-second engine-side
# lazy timeout that backstops an undelivered off.
_CAL_MODE_OFF_SEND_TIMEOUT_S = 2.0

# Notification kinds that are exempt from the startup-toast
# suppression: a real failure is the only signal the engine is not
# coming up, so it must reach the user even while a provider is
# starting (wh-google-creds-file-picker.1.5). Both kinds are equally
# wrong to show for a launch the user has already replaced, so the
# currency check and the suppression read this ONE tuple. Writing the
# two lists separately is exactly how the check came to cover only
# "startup_failed" (wh-launch-generation.2.12).
_STARTUP_SUPPRESSION_EXEMPT_KINDS = ("startup_failed", "error")


def _redact_content_fields(payload: dict) -> dict:
    """Copy of payload with only the content-bearing values redacted."""
    return {
        k: redact_transcript(v) if k in _CONTENT_KEYS else v
        for k, v in payload.items()
    }

class WebSocketManager:
    """
    Manages active WebSocket connections and broadcasts messages.
    This class centralizes connection handling and state for the STT server.
    """
    def __init__(self, loop: asyncio.AbstractEventLoop, text_handler=None):
        self._clients: Set[Any] = set()
        self.transcription_enabled = True  # Default to enabled
        self._suppression_reason: Optional[str] = None  # Reason for transcription being disabled
        self.interim_results_enabled = True  # Default to enabled (send partial results)
        self.loop = loop
        self._server_task: Optional[asyncio.Task] = None
        self._server: Optional[Any] = None
        self.port: int = 0
        self.text_handler = text_handler
        # For backward compatibility, assume text_handler might be just the method
        # or the full speech_handler object with on_utterance_end
        self.speech_handler = text_handler if hasattr(text_handler, 'on_utterance_end') else None
        
        # Word queue for new architecture
        self.word_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        
        # Track current utterance ID for boundary detection
        self.current_utterance_id: Optional[int] = None

        # Reference to state_manager for notifications (set after initialization)
        self.state_manager: Optional[Any] = None

        # Reference to remote_stt_launcher for startup monitoring (set after initialization)
        self.remote_stt_launcher: Optional[Any] = None

        # Reference to app for utterance lifecycle UI commands (set after initialization)
        self._app: Optional[Any] = None

        # Current log level to send to newly connected providers
        self._current_log_level = "INFO"

        # Overlay mode state tracking for delta extraction
        self._processed_word_count: int = 0
        self._last_stable_utterance_id: Optional[int] = None
        self._sent_stable_text: str = ""  # Actual text sent for current utterance (for revision detection)

        # wh-x4fwo three-mode retraction policy state.
        # Per-utterance flags use Optional[int] equality with utterance_id so
        # they auto-expire across utterances without an explicit reset.
        self._stable_disagreement_for_utterance_id: Optional[int] = None
        self._eos_received_for_utterance_id: Optional[int] = None
        # Stream-level diagnostic state for the EOS_NOT_RECEIVED warning.
        # Reset on remove_client so a downgraded STT provider self-heals on
        # the next stream.
        self._eos_observed_in_stream: bool = False
        self._utterances_with_final_in_stream: int = 0
        self._eos_warning_emitted: bool = False
        # Per-stream capability declared by the provider's capabilities
        # message on connect (wh-nvyh). Defaults to False (silent gate)
        # for providers that never declare.
        self._provider_emits_eos: bool = False
        # Whether the active provider's engine applies a saved hint: True,
        # False, or None for unknown -- no frame yet, a provider build
        # without the field, or a stream boundary
        # (wh-boost-engine-qualification). Pushed to the matcher path
        # through speech_handler.apply_hint_engine.
        self.provider_applies_hints: Optional[bool] = None
        # The newest connected client -- the one whose transcripts drive
        # the pipeline (older clients stay connected but DISABLED). Only
        # this client's capabilities declaration is honored (wh-nvyh.1.1).
        self._active_stt_client: Optional[Any] = None
        # The launch generation each STT connection belongs to, stamped
        # when the connection became the active stream. A provider
        # connects as part of the launch that spawned it, so this is the
        # identity of the sender of anything that arrives on it. The
        # provider being replaced keeps an active connection until the
        # replacement connects, so its startup_failed would otherwise
        # end the replacement's own startup monitor and blank the
        # display for an engine that is starting normally
        # (wh-launch-generation).
        self._stt_client_generations: dict[Any, Optional[int]] = {}
        # Connections whose stamp is still the connect-time guess. A
        # provider connects seconds after its own spawn, so the newest
        # launch is only a guess until the connection says which
        # provider it is (wh-launch-generation.1.2).
        self._stt_client_provisional: "set[Any]" = set()
        # The client the last apply_engine_settings was sent to, and
        # that command's correlation id. The reply is accepted only from
        # that client (even if a newer connect has promoted another
        # client in the meantime, wh-7ou.7.6.6 round 4) AND only when it
        # echoes this id -- client identity alone cannot distinguish two
        # applies on the same live connection, so a delayed reply from
        # an abandoned apply must not be read as the current one's
        # answer (wh-7ou.7.6.9). Both cleared when the reply is
        # consumed or the send fails.
        self._engine_settings_reply_client: Optional[Any] = None
        self._engine_settings_reply_id: Optional[str] = None
        # Every client that received set_calibration_mode enabled=true
        # since the last off -- the providers whose suppression bypass is
        # on. A session-ending mode-off targets these clients, not "the
        # active client": on a provider-switch end, add_client has
        # already promoted the NEW client before the controller ends the
        # session, and the off must follow the bypass (wh-7ou.7.6.11). A
        # set, not the most recent client: a same-provider reconnect
        # mid-capture enables the bypass on the new client while the old
        # one is still live, and the off must reach both
        # (wh-7ou.7.6.12). Consumed when an off is sent; a disconnecting
        # client is discarded (its engine clears the bypass itself on
        # disconnect).
        self._calibration_mode_clients: set = set()
        # Threshold for the one-shot EOS_NOT_RECEIVED warning. Three is
        # enough to skip a single anomalous startup utterance.
        self._eos_missing_warning_threshold: int = 3

        # Shared memory for GUI activity state updates
        self._gui_shm: Optional[shared_memory.SharedMemory] = None

        # Idle watchdog: clears 'hearing' state when no final arrives within N
        # seconds. Protects against STT providers that suppress an utterance
        # internally (hallucination filter, crash, network glitch) and never
        # emit a final WebSocket message. Without this, the GUI floating button
        # would pulse orange/red indefinitely. 6.0 s comfortably exceeds the
        # longest real utterance observed in logs (~5 s).
        self._idle_watchdog_seconds: float = 6.0
        self._idle_watchdog_handle: Optional[asyncio.TimerHandle] = None

        # wh-prewarm-detector-vad-start: optional sync callback fired on each
        # vad_start so the focus-redirect path can pre-warm the prompt
        # detector for the foreground HWND. Wired by speech_handler at init
        # to FocusRedirectPath.prewarm; remains None in headless tests and
        # before init completes.
        self._vad_start_callback: Optional[Callable[[], None]] = None

    async def start(self, host: str, port: int) -> int:
        """Starts the WebSocket server.

        Args:
            host: Bind address (e.g., "127.0.0.1", or an all-interfaces
                bind for LAN use).
            port: Port to bind. Use 0 to let the OS assign a free port.

        Returns:
            The actual port the server is listening on.

        :flow: WebSocket Communication
        :step: 1
        :description: Initializes and starts the WebSocket server for STT communication
        :data_in: host (str), port (int)
        :data_out: Active WebSocket server listening for connections
        :notes: Creates asyncio server task. Sets up connection handler.
        """
        if self._server_task and not self._server_task.done():
            logger.warning("WebSocket server is already running.")
            return self.port

        logger.info(f"Starting WebSocket server on {host}:{port}...")
        try:
            self._server = await websockets.serve(self.handle_connection, host, port)
            # Extract the actual bound port (critical when port=0)
            self.port = self._server.sockets[0].getsockname()[1]
            logger.info(f"WebSocket server started on port {self.port}")
            return self.port
        except Exception as e:
            logger.critical(f"Failed to start WebSocket server: {e}", exc_info=True)
            raise

    async def stop(self):
        """Stops the WebSocket server.

        :flow: WebSocket Communication
        :step: 4
        :description: Gracefully shuts down WebSocket server and closes all connections
        :data_in: None
        :data_out: Closed server and connections
        :notes: Cancels server task, waits for closure.
        """
        # Cancel the idle watchdog before teardown so a stray callback cannot
        # fire against shared memory the launcher may have already unmapped.
        self._cancel_idle_watchdog()

        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
                logger.info("WebSocket server stopped.")
            except asyncio.TimeoutError:
                logger.warning("WebSocket server did not close gracefully within the timeout.")
        
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            logger.info("WebSocket server task stopped.")

    def set_app(self, app):
        """
        Sets the app reference for sending utterance lifecycle UI commands.

        This allows the WebSocketManager to send start_utterance and end_utterance
        commands to the UI process for clipboard management.

        Args:
            app: WheelHouseApp instance
        """
        self._app = app
        logger.debug("WebSocketManager: app reference set for utterance lifecycle commands")

    def set_gui_shm(self, shm_name: str):
        """Connect to GUI shared memory for activity updates.

        Args:
            shm_name: Name of the shared memory segment created by launcher
        """
        try:
            self._gui_shm = shared_memory.SharedMemory(name=shm_name)
            logger.info(f"WebSocketManager: Connected to GUI shared memory: {shm_name}")
        except Exception as e:
            logger.error(f"WebSocketManager: Failed to connect to GUI shared memory: {e}")

    def set_vad_start_callback(
        self, callback: Optional[Callable[[], None]],
    ) -> None:
        """Register a sync callback to fire on every vad_start message.

        wh-prewarm-detector-vad-start: speech_handler binds this to
        ``FocusRedirectPath.prewarm`` so the policy's prompt-detector
        cache fills in the background while Silero VAD is still
        committing to a new utterance. The callback runs synchronously
        on the websocket handler's task -- it must return immediately
        and never raise (the handler wraps the call in try/except so
        a broken callback cannot skip the activity-state write or the
        idle watchdog arm).
        """
        self._vad_start_callback = callback
    
    def _arm_idle_watchdog(self, utterance_id: int) -> None:
        """Schedule a callback that clears stuck 'hearing' state after timeout.

        Cancels any previously pending watchdog first. Called on vad_start and
        on each stable delta so a long real utterance does not trip the timer.
        """
        self._cancel_idle_watchdog()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop (unit tests without async context)
        self._idle_watchdog_handle = loop.call_later(
            self._idle_watchdog_seconds,
            self._fire_idle_watchdog,
            utterance_id,
        )

    def _cancel_idle_watchdog(self) -> None:
        """Cancel any pending idle watchdog. Safe to call when none is armed."""
        if self._idle_watchdog_handle is not None:
            self._idle_watchdog_handle.cancel()
            self._idle_watchdog_handle = None

    def _fire_idle_watchdog(self, utterance_id: int) -> None:
        """Watchdog expired without a matching final - clear 'hearing' state."""
        self._idle_watchdog_handle = None
        logger.warning(
            f"[IDLE_WATCHDOG] UTT-{utterance_id}: no final within "
            f"{self._idle_watchdog_seconds}s - clearing stuck 'hearing' state"
        )
        self._write_activity_state('idle', utterance_id)

    def _write_activity_state(self, state: str, utterance_id: int):
        """Write activity state to GUI shared memory.

        States: 'idle', 'hearing', 'settling', 'confirmed'. 'settling' marks
        the provisional window: typed live text that the final could still
        retract. It is set on the first stable delta and cleared by the
        'confirmed' write at the final (wh-dictation-retraction-indicator.1).

        Args:
            state: Activity state string
            utterance_id: Current utterance identifier
        """
        if not self._gui_shm:
            return
        try:
            # Simple protocol: 4-byte size header + JSON payload
            data = json.dumps({'state': state, 'utterance_id': utterance_id}).encode('utf-8')
            size = len(data)
            struct.pack_into('>I', self._gui_shm.buf, 0, size)
            self._gui_shm.buf[4:4+size] = data
        except Exception as e:
            logger.error(f"Failed to write activity state to GUI shm: {e}")

    def _extract_delta(self, new_text: str, utterance_id: int) -> str:
        """
        Extract new text (delta) from stable/final using prefix matching.

        Compares new_text against what we've already sent. If new_text
        starts with our sent text, extract the suffix. If not, a revision
        occurred - log warning, notify user, and return empty string.

        Args:
            new_text: The complete text received (stable or final)
            utterance_id: Current utterance identifier

        Returns:
            The new text suffix that hasn't been sent yet, or empty string
            if revision detected.
        """
        # Reset if this is a new utterance
        if utterance_id != self._last_stable_utterance_id:
            self._sent_stable_text = ""
            self._processed_word_count = 0
            self._last_stable_utterance_id = utterance_id

        # Use word-level comparison to detect revisions.
        # Character-level prefix matching misses revisions where a word is
        # extended (e.g., "comm" -> "comma") because "comma".startswith("comm")
        # is True, causing "a" to be sent as a separate word instead of
        # detecting the revision.
        sent_words = self._sent_stable_text.split() if self._sent_stable_text else []
        new_words = new_text.split() if new_text else []

        if len(new_words) >= len(sent_words) and new_words[:len(sent_words)] == sent_words:
            # Normal case: words match, extract new words as delta
            delta_words = new_words[len(sent_words):]
            delta = " ".join(delta_words)
            if delta:
                self._sent_stable_text = new_text
                self._processed_word_count = len(new_words)
            return delta
        else:
            # Revision detected: STT changed earlier words
            logger.warning(
                f"[REVISION] UTT-{utterance_id}: "
                f"sent='{redact_transcript(self._sent_stable_text)}', "
                f"received='{redact_transcript(new_text)}'"
            )

            return None  # Signal retraction needed (distinct from "" = no new words)

    async def _handle_mode3_retract(
        self,
        utterance_id: int,
        text: str,
        trace_id: str,
        label: str,
    ) -> None:
        """Queue a retraction marker followed by an end marker (Mode 3 path).

        Used by both the explicit stable-disagreement branch (label="MODE3")
        and the conservative ambiguous default (label="AMBIGUOUS_NO_EOS").
        Resets per-utterance delta tracking so the next utterance starts
        fresh.
        """
        logger.info(
            f"[RETRACTION:{label}] UTT-{utterance_id}: queuing retraction marker "
            f"for '{redact_transcript(text)}'"
        )
        self._notify_revision(utterance_id)
        retraction_marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=utterance_id,
            is_retraction_marker=True,
            retraction_full_text=text,
            trace_id=trace_id,
        )
        await self.word_queue.put(retraction_marker)

        end_marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=utterance_id,
            is_utterance_end_marker=True,
            trace_id=trace_id,
        )
        await self.word_queue.put(end_marker)

        self._processed_word_count = 0
        self._last_stable_utterance_id = None
        self._sent_stable_text = ""

    async def _handle_mode1_fresh_content(
        self,
        utterance_id: int,
        text: str,
        trace_id: str,
        final_reason: Optional[str],
    ) -> None:
        """Queue a lifecycle reset and treat the final text as fresh content.

        Mode 1 fires when the STT server-side fallback finalization
        (GOOGLE_SILENCE_2S, EOS_FALLBACK, NO_TEXT_TIMEOUT) returns text that
        does not extend the prior stable. The disagreement is interpreted as
        a SECOND phrase that the server merged into one utterance, so the
        decision tree closes phrase 1, opens a fresh utterance scope, and
        appends phrase 2's words.

        The lifecycle reset marker is queued AHEAD of the new words so the
        SpeechProcessor finishes draining phrase 1 before it sees phrase 2's
        opening word (wh-58vf.5 ordering hazard resolution).
        """
        logger.info(
            f"[MODE1_FRESH_CONTENT] UTT-{utterance_id}: appending "
            f"'{redact_transcript(text)}' as new (final_reason={final_reason!r})"
        )

        reset_marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=utterance_id,
            is_lifecycle_reset_marker=True,
            trace_id=trace_id,
        )
        await self.word_queue.put(reset_marker)

        # Reset delta tracking BEFORE queueing phrase 2 so subsequent stables
        # for any future utterance compute correctly.
        self._processed_word_count = 0
        self._sent_stable_text = ""

        words = text.split()
        for i, word in enumerate(words):
            word_event = WordEvent(
                word=word,
                start_of_utterance=(i == 0),
                end_of_utterance=False,
                utterance_id=utterance_id,
                trace_id=trace_id,
            )
            await self.word_queue.put(word_event)

        end_marker = WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=utterance_id,
            is_utterance_end_marker=True,
            trace_id=trace_id,
        )
        await self.word_queue.put(end_marker)

        self._last_stable_utterance_id = None

    def _notify_revision(self, utterance_id: int) -> None:
        """Send toast notification about transcription revision (if enabled)."""
        # Check if notifications are enabled via config
        if self.state_manager and hasattr(self.state_manager, 'config_service'):
            config = self.state_manager.config_service
            if hasattr(config, 'get'):
                notify_enabled = config.get('speech.notify_on_revision', True)
                if not notify_enabled:
                    return

        # Send toast notification
        if self.state_manager and hasattr(self.state_manager, 'speech_notifier'):
            self.state_manager.speech_notifier._send_notification(
                "Transcription Problem",
                "Transcription problem detected. Please check the output."
            )

    def _apply_capabilities(self, websocket: Any, data: dict) -> None:
        """Apply a provider's capabilities declaration to the active stream.

        Gated on the declaring client being the ACTIVE one (reviewer_0
        finding wh-nvyh.1.1): during overlapping connects -- an orphaned
        provider reconnecting from its backoff loop at the same moment the
        launcher's new provider connects -- a stale client's declaration
        must not overwrite the new active stream's flag. add_client records
        the newest client as active; remove_client clears the record when
        the active client leaves.
        """
        # The provider field is read from EVERY connection, active or
        # not: it is the only thing on the wire that says which launch a
        # connection belongs to, and the connection that needs
        # correcting is exactly the non-active one from an earlier
        # launch (wh-launch-generation.1.2). Every other field stays
        # gated below.
        self._rebind_launch_stamp(websocket, data.get("provider"))
        if websocket is not self._active_stt_client:
            logger.debug(
                "[CAPABILITIES] ignored declaration from non-active client %s",
                getattr(websocket, "remote_address", None),
            )
            return
        declared_emits_eos = bool(data.get("emits_eos", False))
        declared_provider = data.get("provider") or "unknown"
        self._provider_emits_eos = declared_emits_eos
        logger.info(
            f"[CAPABILITIES] provider={declared_provider} "
            f"emits_eos={declared_emits_eos}"
        )

        # wh-audio-suppression-control: whether this provider loaded a wake
        # word detector. One of the sound-pause notices tells the user to
        # say the wake word to get a command through, and that sentence
        # must never appear on a machine whose detector never loaded.
        # Gated on key PRESENCE, not on a default: a provider from before
        # the field existed declares nothing, and the last real answer must
        # stand rather than being overwritten with a guess. Inside the
        # active-client gate above with every other field, so an orphaned
        # provider reconnecting from its backoff loop cannot answer for the
        # live stream.
        if (
            "wake_word_available" in data
            and self.state_manager
            and hasattr(self.state_manager, "set_wake_word_available")
        ):
            declared_wake_word = bool(data.get("wake_word_available"))
            logger.info(
                f"[CAPABILITIES] provider={declared_provider} "
                f"wake_word_available={declared_wake_word}"
            )
            self.state_manager.set_wake_word_available(declared_wake_word)

        # wh-boost-engine-qualification: whether the running engine applies
        # a saved hint. Same key-presence rule as wake_word_available: a
        # provider build from before the field declares nothing, and the
        # value then stays unknown (None), which keeps the "boost" command
        # working. Only a declared False makes the matcher refuse a pattern
        # whose actions save a hint, so the word is typed as dictation.
        if "applies_hints" in data:
            declared_applies_hints = bool(data.get("applies_hints"))
            logger.info(
                f"[CAPABILITIES] provider={declared_provider} "
                f"applies_hints={declared_applies_hints}"
            )
            self._set_provider_applies_hints(declared_applies_hints)

    def _set_provider_applies_hints(self, value: Optional[bool]) -> None:
        """Store the tri-state hint capability and push it to the matcher.

        The push goes through SpeechHandler.apply_hint_engine, which keeps
        the value for a speech processor that does not exist yet.
        """
        self.provider_applies_hints = value
        apply = getattr(self.speech_handler, "apply_hint_engine", None)
        if apply is not None:
            apply(value)

    def _rebind_launch_stamp(self, websocket: Any, provider_name: Any) -> None:
        """Replace a connection's provisional stamp with its own launch.

        The launcher knows the latest launch of each provider by name,
        so naming the provider is enough to move the stamp off the
        newest launch and onto the one that spawned this connection.

        crewcut: two launches of the SAME provider still collapse -- a
        late connection from an earlier launch of a provider is bound to
        that provider's newest launch, because the name is all the
        capabilities message carries. Removing this needs the provider
        to echo its own launch id on the wire, which changes what every
        provider must send and is a decision for the user, not this
        change (wh-launch-generation.1.2). It also makes the
        capabilities message load-bearing. A provider that never sends
        one keeps its provisional stamp, so its startup failure is
        dropped as a signal and recorded against that stamp instead
        (RemoteSTTLauncher.record_undeclared_startup_failure). Its
        ready is now dropped as well, for the same reason and with no
        record, since a launch whose provider never reported ready is
        ended by that launch's own monitor at its deadline
        (wh-ready-connection-stamp.2); the dialog dismiss that ready
        used to carry is dropped with it, because it named the same
        guessed launch (wh-ready-connection-stamp.2.1.1). The
        launch that was starting at the time then reports the stop when
        its own monitor gives up. That attribution is by timing rather
        than identity, so a failure from an undeclared connection that
        really belonged to an OLDER launch ends the current launch's
        startup early: the provider is reported stopped while it may
        still have been warming up. The wire echo removes this too
        (wh-launch-generation.2.3).
        """
        if not provider_name or websocket not in self._stt_client_generations:
            return
        launcher = self.remote_stt_launcher
        if launcher is None:
            return
        try:
            generation = launcher.launch_generation(provider_name)
        except Exception:
            logger.exception("Launch generation lookup failed")
            return
        if generation is None:
            # This launcher never started that provider, so there is no
            # launch to bind to and the stamp stays provisional.
            return
        previous = self._stt_client_generations.get(websocket)
        self._stt_client_generations[websocket] = generation
        self._stt_client_provisional.discard(websocket)
        if previous != generation:
            logger.info(
                f"[CAPABILITIES] provider={provider_name} connection rebound "
                f"from launch {previous} to launch {generation}"
            )

    def _provider_should_emit_eos(self) -> bool:
        """Return True if the active STT provider declared that it emits eos.

        Used to gate the wh-x4fwo EOS_NOT_RECEIVED diagnostic warning so it
        only fires for providers that ARE expected to send eos. The value
        comes from the provider's own capabilities message on connect
        (wh-nvyh) -- there is no hardcoded provider-name set to keep in
        sync. A provider that never sends capabilities (older server)
        defaults to False, the safe silent-gate default.
        """
        return self._provider_emits_eos

    def _current_launch_generation(self) -> Optional[int]:
        """The launch a connection registering right now belongs to.

        A provider connects as part of the launch that spawned it, and
        start_provider runs on the event loop under the switch lock, so
        no other launch can have started between that spawn and this
        connection. Every failure degrades to None, which restores the
        earlier behaviour of attributing a notification to whichever
        launch is current (wh-launch-generation).
        """
        launcher = self.remote_stt_launcher
        if launcher is None:
            return None
        try:
            return launcher.current_launch_generation()
        except Exception:
            logger.exception("Launch generation lookup failed")
            return None

    def _sending_launch_is_current(self, websocket: Any) -> bool:
        """True unless a later launch has replaced this frame's sender.

        The working dialog and the failure toast are shared by every
        provider, and a `startup_failed` frame can arrive from a launch
        the user has already replaced: the provider being replaced keeps
        the active connection until the replacement connects, so its
        failure passes the non-active-client gate. Acting on the shared
        display then blanks the REPLACEMENT'S loading dialog and
        announces an error for a provider that is starting normally.
        The startup monitor asks this same question before touching the
        same display (wh-launch-generation.2.7); this is the second path
        to it (wh-launch-generation.2.8).

        A connection with no stamp, no launcher to ask, or a launcher
        that raises answers True, which is the behaviour every one of
        those cases had before the comparison existed.
        """
        launcher = self.remote_stt_launcher
        if launcher is None:
            return True
        try:
            return bool(
                launcher.launch_is_current(
                    self._stt_client_generations.get(websocket)
                )
            )
        except Exception:
            logger.exception("Launch currency check failed")
            return True

    def _calibration_controller(self) -> Optional[Any]:
        """Return the voice-calibration session controller, or None.

        wh-7ou.7.2.3: the controller lives on the LogicController
        (_get_calibration_controller), reached through the speech_handler
        reference main.py wires in after startup. Every failure in the
        lookup degrades to None -- gate inert, transcripts flow -- because
        silently blocking all dictation would be a worse failure for this
        accessibility-first system than a missed calibration measurement.
        """
        logic = getattr(self.speech_handler, "logic_controller", None)
        getter = getattr(logic, "_get_calibration_controller", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            logger.exception("Calibration controller lookup failed")
            return None

    def _calibration_session_active(self) -> bool:
        """True from the intro screen through the apply wait.

        The stable/interim side of the typing gate (spec Section 6.2):
        while a session is active -- not merely while a capture stage
        runs -- provisional transcript text must not reach the speech
        pipeline (wh-7ou.7.6.3: the session promises measurement-only
        speech from start to finish, and click commands fire from
        finals, so dropping stables costs nothing). Finals go through
        _divert_final_to_calibration instead, so the controller itself
        makes the consumption decision.
        """
        controller = self._calibration_controller()
        return controller is not None and bool(controller.session_active)

    def _divert_final_to_calibration(
        self, text: str, confidence: Optional[dict],
    ) -> bool:
        """Offer a final transcript to the calibration session.

        Returns True when the controller consumed it -- a measurement in
        the word/noise stages, swallowed on any other active screen
        (wh-7ou.7.6.3); the caller must then forward nothing into the
        speech pipeline. Returns False -- the final flows normally --
        when no session is active, for voice-click utterances ("click
        <target>", the allowlisted channel that keeps every screen's
        buttons hands-free, spec Section 3.7), and on any hand-off
        failure (fail-open: dictation must survive a broken calibration
        session).
        """
        controller = self._calibration_controller()
        if controller is None:
            return False
        try:
            consumed = controller.handle_final(text, confidence)
        except Exception:
            logger.exception(
                "Calibration final hand-off failed; forwarding final normally"
            )
            return False
        return bool(consumed)

    def _log_forwarded(
        self, level: int, message: str, source_iso: str
    ) -> None:
        """Log a provider-forwarded record at its source time.

        The provider stamps each forwarded record with the time it was
        written (shared_stt/ws_forwarder.py sends it as a naive local
        isoformat string). After a disconnect the provider's queue
        drains late on reconnect, so stamping on arrival dates every
        drained line by the whole reconnect delay -- and the
        load-diagnostic reading guide subtracts start_s_ago/end_s_ago
        from the line's own timestamp
        (wh-forwarded-log-time-order defect 1). Only created and msecs
        feed %(asctime)s; relativeCreated keeps the arrival value
        because no formatter reads it.
        """
        if not logger.isEnabledFor(level):
            return
        record = logger.makeRecord(
            logger.name,
            level,
            __file__,
            sys._getframe().f_lineno,
            message,
            (),
            None,
        )
        try:
            created = datetime.fromisoformat(source_iso).timestamp()
        except (TypeError, ValueError):
            # Missing or unparseable source time: keep the arrival
            # stamp rather than drop the record.
            pass
        else:
            record.created = created
            record.msecs = (created - int(created)) * 1000
        logger.handle(record)

    async def handle_connection(self, websocket: Any):
        """Handles a new client connection.

        :flow: WebSocket Communication
        :step: 2
        :description: Manages individual client connection lifecycle and message processing
        :data_in: websocket connection
        :data_out: Processed messages (WordEvents) or Notifications
        :notes: Registers client, sends initial status, processes incoming messages loop.
        """
        await self.add_client(websocket)
        try:
            # Send initial status messages to the newly connected client
            await websocket.send(json.dumps(self.get_current_status_message()))
            await websocket.send(json.dumps({
                "type": "set_interim_results",
                "enabled": self.interim_results_enabled
            }))
            # Send current log level to newly connected provider
            await websocket.send(json.dumps({
                "type": "set_log_level",
                "level": self._current_log_level
            }))
            async for message in websocket:
                """:flow: Speech Processing
                :step: 1
                :description: Intake bridge for STT WebSocket frames—normalizes `utterance_id`,
                emits `WordEvent` objects, and forwards health notifications into shared state.
                :produces_for: Speech Processing
                :notes: First step of speech processing pipeline. Receives JSON frames from STT
                WebSocket, splits text into words, annotates utterance boundaries (start/end flags),
                and enqueues WordEvent objects. Phases: (1) Message arrival - validates STT frames
                and handles health notifications, (2) WordEvent handoff - splits delta text and
                annotates boundaries, (3) Command bridge - maintains utterance continuity for
                downstream truth-table processing (step 2). Non-speech notifications bypass the
                queue and trigger Windows toast notifications directly.
                :data_in: JSON string `{type: "delta"|"utterance_end"|"notification", text: "...", utterance_id: N, title: "...", message: "..."}` from the STT server
                :data_out: `WordEvent` objects enqueued to `word_queue`, or Windows toast notifications dispatched via `speech_notifier`
                """
                # Removed: logger.debug(f"Received message from client: {message}") - fired on every word
                
                try:
                    # All messages from STT server are JSON
                    data = json.loads(message)
                    msg_type = data.get("type", "delta")
                    text = data.get("text", "")
                    utterance_id = data.get("utterance_id", 0)
                    trace_id = data.get("trace_id", "")
                    set_trace(trace_id)

                    # Sender binding while a calibration session is
                    # active (wh-7ou.7.6.6): a transcript or lifecycle
                    # frame from a client that is not the active stream
                    # -- a superseded or orphaned provider delivering
                    # queued frames -- must not become a measurement,
                    # type, touch the shared watchdog or activity state,
                    # steer the retraction policy, or publish a wake
                    # event. Dropped here, before ANY side effect. With
                    # no session, stale frames keep their pre-existing
                    # behavior. notification / capabilities /
                    # engine_settings_result keep their own
                    # unconditional active-client gates below.
                    if (
                        msg_type in (
                            "vad_start", "eos", "stable",
                            "final", "wake_word_detected",
                        )
                        and self._calibration_session_active()
                        and websocket is not self._active_stt_client
                    ):
                        logger.info(
                            f"[CALIBRATION] dropping {msg_type} from "
                            "non-active STT client "
                            f"{getattr(websocket, 'remote_address', None)}"
                        )
                        continue

                    # Handle notification messages
                    if msg_type == "notification":
                        title = data.get("title", "Wheelhouse Notification")
                        notification_message = data.get("message", "")
                        # Structured classification set by the provider
                        # (shared_stt/ws_forwarder.py:send_notification):
                        # "ready", "startup_failed", "error", or "" for a
                        # plain notice (wh-google-creds-file-picker.1.5).
                        # All three shipped providers send it; the ready
                        # branch below records where each one does.
                        kind = data.get("kind", "")

                        # Which capture path the provider's factory
                        # actually built, in the provider's own words
                        # (shared_audio/capture/factory.py, constant
                        # CAPTURE_BACKEND_NAME). Only a ready carries a
                        # value: a provider whose capture never opened
                        # has no true answer, and one whose credentials
                        # failed never asked. Absent for a provider
                        # built before this field existed, which is why
                        # the log line below distinguishes "did not
                        # report" from a named path rather than
                        # supplying a default (wh-capture-winrt-required
                        # A4).
                        capture_backend = data.get("capture_backend", "")

                        # Same gate as _apply_capabilities (wh-nvyh.1.1):
                        # add_client keeps older connections open (merely
                        # DISABLED), so an orphaned provider from a previous
                        # generation can still deliver queued notification
                        # frames while a new provider starts. Its
                        # "startup_failed"/"ready" describes the OLD
                        # generation and must not end or complete the NEW
                        # startup, and its notices misreport the system
                        # state (wh-google-creds-file-picker.1.16).
                        if (
                            self._active_stt_client is not None
                            and websocket is not self._active_stt_client
                        ):
                            logger.info(
                                "Ignoring notification from non-active STT "
                                f"client {getattr(websocket, 'remote_address', None)}: "
                                f"{title} - {redact_transcript(notification_message)}"
                            )
                            continue

                        logger.info(f"Received notification: {title} - {redact_transcript(notification_message)}")

                        # A failed startup: end the launcher's starting
                        # state, close the working dialog, and fall
                        # through to the toast -- the message says what
                        # is wrong, and suppressing it left the user
                        # with a dead engine and no signal
                        # (wh-google-creds-file-picker.1.5).
                        if kind == "startup_failed":
                            if self.remote_stt_launcher:
                                if websocket in self._stt_client_provisional:
                                    # This connection never said which
                                    # provider it is, so its stamp is
                                    # still the connect-time guess.
                                    # Attributing the failure to that
                                    # guess is the defect this change
                                    # removes, and guessing by name
                                    # would put name identity back. The
                                    # owning launch's own monitor still
                                    # reports it when it times out; the
                                    # provisional stamp is logged so the
                                    # two can be matched up in the log
                                    # (wh-launch-generation.1.2).
                                    logger.warning(
                                        "Dropping a startup failure from a "
                                        "connection that never declared its "
                                        "provider (provisional launch stamp "
                                        f"{self._stt_client_generations.get(websocket)})"
                                    )
                                    # Dropped as a SIGNAL, kept as
                                    # evidence. The launch that was
                                    # starting when this connection
                                    # arrived reads it instead of
                                    # treating its own provider's
                                    # silence as a slow cold start
                                    # (wh-launch-generation.2.3).
                                    self.remote_stt_launcher.record_undeclared_startup_failure(
                                        self._stt_client_generations.get(websocket)
                                    )
                                else:
                                    # Stamped with the launch that owns
                                    # THIS connection, not the current
                                    # one: the gate above passes a frame
                                    # from the provider being replaced
                                    # while its connection is still the
                                    # active one (wh-launch-generation).
                                    self.remote_stt_launcher.signal_provider_startup_failed(
                                        self._stt_client_generations.get(websocket)
                                    )
                            # Only the launch that owns the display may
                            # close it (wh-launch-generation.2.8). The
                            # dismiss also names that launch, so a
                            # replacement landing between this answer
                            # and the GUI reading the message is caught
                            # there (wh-launch-addressed-notices).
                            if (
                                self._sending_launch_is_current(websocket)
                                and self.state_manager
                                and hasattr(self.state_manager, 'state_to_gui_queue')
                            ):
                                try:
                                    self.state_manager.state_to_gui_queue.put_nowait({
                                        "action": "hide_working",
                                        "owner": launch_owner_token(
                                            self._stt_client_generations.get(websocket)
                                        ),
                                    })
                                except Exception:
                                    pass
                        # Check if this is a "ready" notification from STT provider
                        # Signal the launcher to cancel the startup timeout monitor.
                        # kind="ready" is the whole test. google_stt_server sends
                        # it, and it once passed only by matching "Ready." in its
                        # own message, so the structured path worked by accident;
                        # naming it made that a contract (wh-launch-generation.2.13).
                        # The substring fallback beside it is GONE
                        # (wh-ready-connection-stamp.2.2.1). It accepted any
                        # kind-less notice whose text contained "ready", which also
                        # matches "already", "not ready" and "Ready to retry", so
                        # every provider's "Hint '<word>' already exists" notice
                        # completed the launch, closed the working dialog, and never
                        # reached the user -- and so did a failed hint save whose
                        # word contained "ready". The condition the wh-v0q follow-up
                        # named for its removal is now met: all three shipped
                        # providers send kind="ready"
                        # (google_stt_server/main.py:198 and 215,
                        # distil_medium_en/main.py:535,
                        # sherpa_offline_parakeet_stt_server/main.py:596). A
                        # provider that sends no kind now completes no launch by
                        # any route: the structured branch is the only one left.
                        # If this launcher never started it, its connection also
                        # stays provisional and its ready is dropped above
                        # (344c6782).
                        elif self.remote_stt_launcher and kind == "ready":
                            # Stamped with the launch that owns THIS
                            # connection, not the current one -- the
                            # same stamp the startup_failed branch
                            # above reads, and for the same reason: the
                            # gate at the top of this block passes a
                            # frame from the provider being replaced
                            # while its connection is still the active
                            # one. Unstamped, this ready completed
                            # whichever launch happened to be starting
                            # (wh-ready-connection-stamp).
                            #
                            # A connection that never declared its
                            # provider keeps its connect-time stamp, so
                            # its ready names whichever launch was
                            # starting when it arrived rather than the
                            # launch it belongs to. The signal is
                            # dropped, the same shape the startup_failed
                            # branch above uses: a provider left running
                            # by a previous run of WheelHouse reconnects
                            # into a launch this launcher never started,
                            # becomes the active client, and its ready
                            # would end the CURRENT launch's starting
                            # state -- for a launch whose own provider
                            # may never have reported ready
                            # (wh-ready-connection-stamp.2).
                            #
                            # The cost this comment used to name for
                            # dropping it -- a healthy launch left to
                            # reach the slow-start branch, which never
                            # ended the starting state, so the
                            # suppression below swallowed every later
                            # notice from that provider for the rest of
                            # the session -- is paid by that branch now
                            # calling _end_starting_state for its own
                            # launch. Dropping the ready is only safe
                            # together with that call
                            # (RemoteSTTLauncher._monitor_startup_body).
                            #
                            # The dialog dismiss is dropped with the
                            # signal. It travels with the same stamp,
                            # and the GUI applies a dismiss that names
                            # the dialog's owner or names no launch at
                            # all, so a dismiss addressed with the guess
                            # closed the CURRENT launch's loading display
                            # on a ready this branch had just declared
                            # un-attributable, while that launch's own
                            # provider was still warming up. That
                            # launch's monitor hides the dialog at its
                            # deadline (wh-ready-connection-stamp.2.1.1).
                            if websocket in self._stt_client_provisional:
                                logger.info(
                                    "Dropping a ready from a connection that "
                                    "never declared its provider (provisional "
                                    "launch stamp "
                                    f"{self._stt_client_generations.get(websocket)})"
                                )
                                continue
                            # The one line in wheelhouse.log that answers
                            # "which capture path did this run use?".
                            # Before it, nothing on either side of the
                            # socket said: on 2026-09-05 a provider
                            # captured through PortAudio for hours and
                            # the log was silent, because a working
                            # capture and a wrong capture look the same
                            # from here. It is written on the accepted
                            # ready only -- a dropped ready belongs to a
                            # launch this launcher does not own, and a
                            # line about it would name a capture path
                            # nothing in this session is using
                            # (wh-capture-winrt-required A4).
                            logger.info(
                                "STT provider ready: %s -- capture backend "
                                "%s (launch %s)",
                                title,
                                capture_backend or "not reported",
                                self._stt_client_generations.get(websocket),
                            )
                            self.remote_stt_launcher.signal_provider_ready(
                                self._stt_client_generations.get(websocket)
                            )
                            # Close the working dialog, naming the
                            # launch that owns this connection. A ready
                            # from a launch the user has already
                            # replaced must not close the replacement's
                            # dialog, and the GUI is where that can be
                            # decided against the order the two messages
                            # were sent in (wh-launch-addressed-notices).
                            if self.state_manager and hasattr(self.state_manager, 'state_to_gui_queue'):
                                try:
                                    self.state_manager.state_to_gui_queue.put_nowait({
                                        "action": "hide_working",
                                        "owner": launch_owner_token(
                                            self._stt_client_generations.get(websocket)
                                        ),
                                    })
                                except Exception:
                                    pass
                            continue  # Working dialog dismissal is sufficient; skip toast

                        # A failure from a launch the user has already
                        # replaced is not news: the replacement is
                        # starting, and the toast names the provider
                        # being replaced. The exemption below would
                        # otherwise carry it straight past the startup
                        # suppression, which is exactly what makes this
                        # a second path to the shared feedback the
                        # monitor already guards
                        # (wh-launch-generation.2.7, .2.8). Every exempt
                        # kind is checked, not only the startup one
                        # (wh-launch-generation.2.12).
                        if kind in _STARTUP_SUPPRESSION_EXEMPT_KINDS and not (
                            self._sending_launch_is_current(websocket)
                        ):
                            logger.debug(
                                "Suppressing a failure toast from a replaced "
                                f"launch (stamp "
                                f"{self._stt_client_generations.get(websocket)})"
                            )
                            continue

                        # Suppress toast during provider startup -- working dialog is
                        # sufficient. Failure notices are exempt: they are the only
                        # signal the engine is not coming up
                        # (wh-google-creds-file-picker.1.5).
                        if (
                            kind not in _STARTUP_SUPPRESSION_EXEMPT_KINDS
                            and self.remote_stt_launcher
                            and self.remote_stt_launcher.is_starting
                        ):
                            logger.debug(f"Suppressing toast during startup: {redact_transcript(notification_message)}")
                            continue

                        # Send Windows toast notification via state_manager's speech_notifier
                        if self.state_manager and hasattr(self.state_manager, 'speech_notifier'):
                            self.state_manager.speech_notifier._send_notification(title, notification_message)
                            logger.debug(f"Notification sent successfully")
                        else:
                            logger.warning("state_manager not available - notification not sent")
                        continue

                    # Handle forwarded log messages from STT providers
                    if msg_type == "log":
                        log_level = data.get("level", "INFO").upper()
                        log_message = data.get("message", "")
                        log_source = data.get("source", "STT")
                        log_timestamp = data.get("timestamp", "")

                        formatted_msg = f"[{log_source}] {log_message}"

                        # Map level string to logging level and log with appropriate level
                        level_map = {
                            "DEBUG": logging.DEBUG,
                            "INFO": logging.INFO,
                            "WARNING": logging.WARNING,
                            "ERROR": logging.ERROR,
                            "CRITICAL": logging.CRITICAL
                        }
                        level = level_map.get(log_level, logging.INFO)
                        self._log_forwarded(level, formatted_msg, log_timestamp)
                        continue

                    # Handle "capabilities" messages -- the provider declares
                    # what it can do right after connecting (wh-nvyh). The
                    # consumed capabilities are emits_eos, which gates the
                    # EOS_NOT_RECEIVED diagnostic warning, wake_word_available,
                    # and applies_hints (see _apply_capabilities). Each is
                    # per-stream: add_client resets it when a new client
                    # becomes the active stream, and the provider's forwarder
                    # re-sends the declaration on every (re)connect.
                    if msg_type == "capabilities":
                        self._apply_capabilities(websocket, data)
                        continue

                    # The provider's answer to apply_engine_settings
                    # (wh-7ou.7.2.3, calibration message contract). Routed
                    # to the calibration controller, which ignores it
                    # outside the applying stage. While an apply is in
                    # flight the reply belongs to the client the apply was
                    # SENT to, active or not (wh-7ou.7.6.6 round 4: an
                    # orphan connect can promote a new client between the
                    # send and the reply, and dropping the reply as
                    # non-active strands the session in the applying
                    # state). With no apply in flight, the active-client
                    # gate applies as for notifications: a superseded
                    # provider's late frames describe an old generation,
                    # not the save the user is waiting on.
                    if msg_type == "engine_settings_result":
                        expected = self._engine_settings_reply_client
                        if expected is not None:
                            if websocket is not expected:
                                logger.info(
                                    "[CALIBRATION] ignoring engine_settings_result "
                                    "from a client the apply was not sent to "
                                    f"{getattr(websocket, 'remote_address', None)}"
                                )
                                continue
                            if data.get("apply_id") != self._engine_settings_reply_id:
                                # A delayed reply from an EARLIER apply on
                                # the same connection -- a cancelled
                                # session's apply, or a provider write
                                # that hung and answered late. Client
                                # identity cannot tell two applies apart;
                                # only the echoed id can (wh-7ou.7.6.9).
                                # Keep waiting for the current apply's
                                # reply.
                                logger.info(
                                    "[CALIBRATION] ignoring engine_settings_result "
                                    "echoing a different apply than the one "
                                    "in flight"
                                )
                                continue
                            self._engine_settings_reply_client = None
                            self._engine_settings_reply_id = None
                        elif (
                            self._active_stt_client is not None
                            and websocket is not self._active_stt_client
                        ):
                            logger.info(
                                "[CALIBRATION] ignoring engine_settings_result "
                                "from non-active STT client "
                                f"{getattr(websocket, 'remote_address', None)}"
                            )
                            continue
                        ok = data.get("ok") is True
                        controller = self._calibration_controller()
                        if controller is None:
                            logger.warning(
                                "engine_settings_result received but no "
                                "calibration controller is available; dropped"
                            )
                            continue
                        controller.on_engine_settings_result(ok, data.get("error"))
                        continue


                    # ================================================================
                    # OVERLAY MODE MESSAGE TYPES (from STT overlay_mode=true)
                    # ================================================================
                    
                    # Handle "vad_start" messages - VAD committed to new speech
                    # This triggers instant GUI pulse (~150ms from speech start)
                    if msg_type == "vad_start":
                        logger.debug(f"[VAD_START] UTT-{utterance_id}: Speech detected")
                        self._write_activity_state('hearing', utterance_id)
                        self._arm_idle_watchdog(utterance_id)
                        # wh-prewarm-detector-vad-start: kick off the
                        # focus-redirect policy's prompt detector for the
                        # current foreground HWND so the cache is warm by
                        # the time the first dictated word arrives. The
                        # callback is sync and fire-and-forget; we wrap
                        # in try/except so a broken callback cannot break
                        # the GUI hearing pulse or the idle watchdog.
                        if self._vad_start_callback is not None:
                            try:
                                self._vad_start_callback()
                            except Exception:
                                logger.exception(
                                    "vad_start_callback raised; "
                                    "continuing -- pre-warm will be "
                                    "skipped for this utterance",
                                )
                        continue

                    # Handle "eos" messages - END_OF_SINGLE_UTTERANCE lifecycle event.
                    # The Google STT provider sends this when Google's streaming
                    # API decides the speaker stopped. Other providers do not
                    # emit it. Recording presence per-utterance and per-stream
                    # feeds the three-mode retraction policy decision tree
                    # below (wh-x4fwo).
                    if msg_type == "eos":
                        logger.debug(f"[EOS] UTT-{utterance_id}: end-of-single-utterance received")
                        self._eos_received_for_utterance_id = utterance_id
                        self._eos_observed_in_stream = True
                        continue

                    # Handle "stable" messages - extract deltas and queue words immediately
                    if msg_type == "stable":
                        logger.info(f"[STABLE] UTT-{utterance_id}: '{redact_transcript(text)}'")
                        # Proof of ongoing speech -- slide the watchdog forward
                        # so long utterances with pauses do not trip it.
                        self._arm_idle_watchdog(utterance_id)

                        # wh-7ou.7.2.3 typing gate (spec Section 6.2): while
                        # a calibration session is ACTIVE -- any screen, not
                        # only the capture stages (wh-7ou.7.6.3) --
                        # provisional text must not type or claim 'settling'.
                        # Drop it here; the final carries the measurement or
                        # the allowlisted click command to the controller.
                        # Status messages (vad_start, wake word) keep
                        # flowing normally.
                        if self._calibration_session_active():
                            logger.debug(
                                f"[CALIBRATION] UTT-{utterance_id}: stable "
                                "diverted during calibration session"
                            )
                            continue

                        # Extract delta (new words since last stable)
                        delta = self._extract_delta(text, utterance_id)

                        # Stable revisions: notify but don't retract (wait for final).
                        # Record the disagreement so the final-handler decision
                        # tree (wh-x4fwo Mode 3) can prefer it over EOS evidence.
                        # Set the flag BEFORE notifying so a notification failure
                        # does not lose the state (wh-76yv.1 resolution).
                        if delta is None:
                            self._stable_disagreement_for_utterance_id = utterance_id
                            self._notify_revision(utterance_id)
                            continue

                        if delta:
                            # Send start_utterance on first delta of new utterance
                            if self.current_utterance_id != utterance_id:
                                self.current_utterance_id = utterance_id
                                logger.debug(f"Utterance {utterance_id} started (from stable)")
                                if self._app:
                                    await self._app.send_command({
                                        'action': 'start_utterance',
                                        'params': {'utterance_id': utterance_id}
                                    })
                                # First provisional word of this utterance is now
                                # being typed and could still be retracted by the
                                # final. Signal 'settling' so the GUI can show a
                                # working indicator. Cleared by the 'confirmed'
                                # write in the final handler below.
                                # (wh-dictation-retraction-indicator.1)
                                self._write_activity_state('settling', utterance_id)

                            delta_words = delta.split()
                            # Calculate if this is the start of the utterance
                            # We just added len(delta_words) to the count in _extract_delta
                            previous_count = self._processed_word_count - len(delta_words)
                            # wh-spaced-punctuation-names-unresolved
                            # .3.1.5: read the count the send above
                            # bumped. Nothing between that send and
                            # this line awaits, so the number names
                            # exactly that start_utterance.
                            start_generation = getattr(
                                self._app, 'utterance_start_generation', None,
                            ) if self._app else None

                            for i, word in enumerate(delta_words):
                                is_first = (i == 0 and previous_count == 0)  # First word of utterance
                                word_event = WordEvent(
                                    word=word,
                                    start_of_utterance=is_first,
                                    end_of_utterance=False,
                                    utterance_id=utterance_id,
                                    trace_id=trace_id,
                                    utterance_start_generation=(
                                        start_generation if is_first else None
                                    ),
                                )
                                await self.word_queue.put(word_event)
                            logger.debug(f"Queued {len(delta_words)} words from stable delta")
                        continue
                    
                    # Handle "final" messages - complete transcript, signals utterance end
                    if msg_type == "final":
                        final_reason = data.get("final_reason")
                        logger.info(
                            f"[FINAL] UTT-{utterance_id}: '{redact_transcript(text)}'"
                            + (f" (final_reason={final_reason})" if final_reason else "")
                        )

                        # wh-7ou.7.6.13: a bypassed final from a client
                        # that is NOT the active stream must not touch
                        # shared pipeline state at all. The session-
                        # scoped sender gate at the top of the loop
                        # stops running the moment the session ends,
                        # and utterance ids are per-provider counters,
                        # so this stale final's id can collide with the
                        # ACTIVE stream's live utterance -- the verdict
                        # cleanup below would close the wrong stream's
                        # utterance and reset its delta state, and the
                        # 'confirmed' flash / watchdog cancel would
                        # misreport a stream that produced nothing.
                        # Dropped before every side effect; only the
                        # bypassed shape (suppressed=true WITH text) is
                        # touched -- clean non-active finals keep their
                        # pre-existing flow.
                        _stale_conf = data.get("confidence")
                        if (
                            text
                            and isinstance(_stale_conf, dict)
                            and _stale_conf.get("suppressed") is True
                            and websocket is not self._active_stt_client
                        ):
                            logger.info(
                                f"[CALIBRATION] UTT-{utterance_id}: "
                                "dropping bypassed final from non-active "
                                "STT client "
                                f"{getattr(websocket, 'remote_address', None)}"
                            )
                            continue

                        # Stream-level diagnostic: warn once if a Google STT
                        # provider produces several utterances with finals and
                        # never an eos. Indicates a downgraded or older STT
                        # server that does not speak the new protocol; the
                        # decision tree below will conservatively default to
                        # retract+replay for ambiguous cases (wh-a2j2y). Gated
                        # to providers whose capabilities message declared
                        # emits_eos=true (wh-nvyh) so local providers (which
                        # do not emit eos by design) do not produce a
                        # misleading warning every stream.
                        self._utterances_with_final_in_stream += 1
                        if (
                            self._utterances_with_final_in_stream >= self._eos_missing_warning_threshold
                            and not self._eos_observed_in_stream
                            and not self._eos_warning_emitted
                            and self._provider_should_emit_eos()
                        ):
                            logger.warning(
                                "[EOS_NOT_RECEIVED] STT stream has produced "
                                f"{self._utterances_with_final_in_stream} utterances with finals "
                                "but no eos messages. Decision tree will conservatively default to "
                                "retract+replay for ambiguous cases. Verify STT provider is updated."
                            )
                            self._eos_warning_emitted = True

                        # Signal GUI to show confirmed state (green flash)
                        self._write_activity_state('confirmed', utterance_id)
                        self._cancel_idle_watchdog()

                        # wh-7ou.7.2.3 typing gate (spec Section 6.2): every
                        # final is offered to the calibration controller
                        # first, which consumes it -- a measurement in the
                        # word/noise stages, swallowed on any other active
                        # screen -- unless it is an allowlisted voice-click
                        # utterance, which flows normally so the window's
                        # buttons stay hands-free on every screen (spec
                        # Section 3.7, wh-7ou.7.6.3). The 'confirmed' flash
                        # and watchdog cancel above already ran: status
                        # stays live. Stale-client finals never get here
                        # during a session -- the sender-binding gate at
                        # the top of the loop dropped them before any
                        # side effect (wh-7ou.7.6.6).
                        if self._divert_final_to_calibration(text, data.get("confidence")):
                            if utterance_id == self.current_utterance_id:
                                # Capture began mid-utterance: stables had
                                # already queued words downstream, so close
                                # the open utterance with an end marker
                                # (never the diverted text) and reset delta
                                # tracking, mirroring the normal final path.
                                end_marker = WordEvent(
                                    word="",
                                    start_of_utterance=False,
                                    end_of_utterance=True,
                                    utterance_id=utterance_id,
                                    is_utterance_end_marker=True,
                                    trace_id=trace_id,
                                )
                                await self.word_queue.put(end_marker)
                                self._processed_word_count = 0
                                self._last_stable_utterance_id = None
                                self._sent_stable_text = ""
                            continue

                        # wh-7ou.7.6.11 verdict gate: a final carrying the
                        # provider filter's own suppressed=true verdict
                        # WITH text present can only be a calibration-
                        # bypass final -- outside the bypass the provider
                        # empties the transcript before sending. Reaching
                        # here means no session consumed it (the session
                        # just ended and mode-off is still in flight, or a
                        # stale provider never received mode-off), so
                        # apply the verdict at this boundary: the text
                        # must not type or execute. Runs AFTER the divert
                        # above -- during capture these finals ARE the
                        # noise measurements. Same open-utterance closure
                        # as the diverted path.
                        _final_conf = data.get("confidence")
                        if (
                            text
                            and isinstance(_final_conf, dict)
                            and _final_conf.get("suppressed") is True
                        ):
                            logger.info(
                                f"[CALIBRATION] UTT-{utterance_id}: dropping "
                                "bypassed final carrying a suppressed verdict "
                                "with no session to consume it"
                            )
                            if utterance_id == self.current_utterance_id:
                                end_marker = WordEvent(
                                    word="",
                                    start_of_utterance=False,
                                    end_of_utterance=True,
                                    utterance_id=utterance_id,
                                    is_utterance_end_marker=True,
                                    trace_id=trace_id,
                                )
                                await self.word_queue.put(end_marker)
                                self._processed_word_count = 0
                                self._last_stable_utterance_id = None
                                self._sent_stable_text = ""
                            continue

                        # Extract any remaining delta (words not sent via stables)
                        delta = self._extract_delta(text, utterance_id) if text else ""

                        # Three-mode disagreement handling (wh-x4fwo). Decision
                        # tree synthesized from wh-76yv adversarial review:
                        #   stable_disagreement -> Mode 3 (retract + replay; current behavior)
                        #   eos_received        -> Mode 2 (trust stable, drop final, log)
                        #   fallback final_reason -> Mode 1 (treat as fresh content)
                        #   else                -> conservative Mode 3 default
                        if delta is None:
                            stable_disagreement = (
                                self._stable_disagreement_for_utterance_id == utterance_id
                            )
                            eos_received = (
                                self._eos_received_for_utterance_id == utterance_id
                            )

                            if stable_disagreement:
                                await self._handle_mode3_retract(
                                    utterance_id, text, trace_id, label="MODE3"
                                )
                                continue
                            elif eos_received:
                                logger.warning(
                                    f"[POST_EOS_FINAL_DROPPED] UTT-{utterance_id}: "
                                    f"keeping stable='{redact_transcript(self._sent_stable_text)}', "
                                    f"dropping final='{redact_transcript(text)}'"
                                )
                                end_marker = WordEvent(
                                    word="",
                                    start_of_utterance=False,
                                    end_of_utterance=True,
                                    utterance_id=utterance_id,
                                    is_utterance_end_marker=True,
                                    trace_id=trace_id,
                                )
                                await self.word_queue.put(end_marker)
                                self._processed_word_count = 0
                                self._last_stable_utterance_id = None
                                self._sent_stable_text = ""
                                continue
                            elif final_reason in (
                                "GOOGLE_SILENCE_2S", "EOS_FALLBACK", "NO_TEXT_TIMEOUT",
                            ):
                                await self._handle_mode1_fresh_content(
                                    utterance_id, text, trace_id, final_reason
                                )
                                continue
                            else:
                                logger.warning(
                                    f"[AMBIGUOUS_NO_EOS] UTT-{utterance_id}: "
                                    f"final_reason={final_reason!r}, defaulting to retract+replay"
                                )
                                await self._handle_mode3_retract(
                                    utterance_id, text, trace_id, label="AMBIGUOUS_NO_EOS"
                                )
                                continue

                        # Detect if this is a new utterance (no stables received)
                        is_new_utterance = (utterance_id != self.current_utterance_id)
                        if is_new_utterance:
                            self.current_utterance_id = utterance_id
                            logger.debug(f"Utterance {utterance_id} started (from final)")
                            if self._app:
                                await self._app.send_command({
                                    'action': 'start_utterance',
                                    'params': {'utterance_id': utterance_id}
                                })

                        # Queue any remaining words from the delta
                        if delta:
                            delta_words = delta.split()
                            # wh-spaced-punctuation-names-unresolved
                            # .3.1.5: same read as the stable path
                            # above, for the start_utterance this
                            # branch may have just sent.
                            start_generation = getattr(
                                self._app, 'utterance_start_generation', None,
                            ) if self._app else None
                            for i, word in enumerate(delta_words):
                                # First word of a new utterance needs start_of_utterance=True
                                # so speech processor treats it as a potential command
                                is_first = (i == 0 and is_new_utterance)
                                word_event = WordEvent(
                                    word=word,
                                    start_of_utterance=is_first,
                                    end_of_utterance=False,
                                    utterance_id=utterance_id,
                                    trace_id=trace_id,
                                    utterance_start_generation=(
                                        start_generation if is_first else None
                                    ),
                                )
                                await self.word_queue.put(word_event)
                            logger.debug(f"Queued {len(delta_words)} remaining words from final")

                        # Always queue utterance end marker
                        end_marker = WordEvent(
                            word="",
                            start_of_utterance=False,
                            end_of_utterance=True,
                            utterance_id=utterance_id,
                            is_utterance_end_marker=True,
                            trace_id=trace_id,
                        )
                        await self.word_queue.put(end_marker)

                        # Reset tracking state for next utterance
                        self._processed_word_count = 0
                        self._last_stable_utterance_id = None
                        self._sent_stable_text = ""
                        continue
                    
                    # Handle wake word detection from STT provider
                    if msg_type == "wake_word_detected":
                        keyword = data.get("keyword", "")
                        logger.info(f"[WAKE_WORD] Detected keyword: '{keyword}'")
                        if self.state_manager and hasattr(self.state_manager, 'event_bus'):
                            from services.wheelhouse.events import WakeWordDetectedEvent
                            await self.state_manager.event_bus.publish(
                                WakeWordDetectedEvent(keyword=keyword)
                            )
                        continue

                    # Unexpected message type - STT server should only send
                    # vad_start, stable, final, notification, or wake_word_detected messages
                    logger.warning(f"Unexpected message type '{msg_type}' from STT: {redact_transcript(text) if text else '(empty)'}")

                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from STT server: {e}. Message: {redact_transcript(message)}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}", exc_info=True)

        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client {websocket.remote_address} disconnected.")
        except Exception as e:
            logger.error(f"Error in WebSocket connection handler for {websocket.remote_address}: {e}", exc_info=True)
        finally:
            self.remove_client(websocket)

    def _reset_retraction_policy_state(self) -> None:
        """Reset all wh-x4fwo three-mode retraction policy state.

        Called on active-stream lifecycle boundaries -- when add_client
        promotes a new client to active, and when remove_client takes the
        last client out. Both per-utterance flags AND stream-level
        diagnostic counters get cleared together because the per-utterance
        Optional[int] equality only auto-expires across utterances WITHIN
        a stream. Across a stream boundary, utterance IDs can repeat
        (the STT provider process restarts at utterance_id=1), so a stale
        per-utterance flag from the prior stream would otherwise match a
        new-stream utterance ID and steer the decision tree wrong.
        Reported as wh-eknz.1 and wh-eknz.2 in the implementation review.
        """
        self._stable_disagreement_for_utterance_id = None
        self._eos_received_for_utterance_id = None
        self._eos_observed_in_stream = False
        self._utterances_with_final_in_stream = 0
        self._eos_warning_emitted = False
        # The capability declaration is also per-stream (wh-nvyh): the next
        # active client must re-declare, and until it does the gate stays
        # at the safe silent default.
        self._provider_emits_eos = False
        # The wake word declaration is per-stream for the same reason, but
        # StateManager owns the value, so clear it there. A provider that
        # disconnects and never returns would otherwise leave True behind,
        # and the sound-pause notice would name a wake word that no running
        # process can hear; a provider built without openWakeWord can also
        # replace one that had it (wh-audio-suppression-control).
        if self.state_manager and hasattr(
            self.state_manager, "set_wake_word_available"
        ):
            self.state_manager.set_wake_word_available(False)
        # Whether the engine applies hints is per-stream too, but the
        # boundary resets it to None (unknown), not False: during an engine
        # switch the gap before the new provider's frame must not refuse
        # the "boost" command (wh-boost-engine-qualification).
        self._set_provider_applies_hints(None)

    async def add_client(self, websocket: Any):
        """Registers a new client connection, disabling existing clients.

        Multiple STT providers can stay connected, but only the newest one is
        enabled. When a new provider connects, existing connections are sent
        DISABLE so they stop sending transcripts but remain connected for
        potential re-enabling later.
        """
        # Register and promote BEFORE any await
        # (wh-google-creds-file-picker.1.19): the DISABLE sends below can
        # suspend this coroutine, and an older add_client resuming after a
        # newer one had already promoted itself used to overwrite the
        # newer connection -- promotion then followed send-completion
        # order, not arrival order. With no await between registration and
        # promotion, the last add_client to START is the one that stays
        # active.
        existing_clients = [c for c in self._clients if c is not websocket]

        # The new client becomes the active stream. Reset retraction policy
        # state so the prior active stream's per-utterance and diagnostic
        # state cannot cross-contaminate. (wh-eknz.1, wh-eknz.2)
        self._reset_retraction_policy_state()

        logger.info(f"STT client connected: {websocket.remote_address}")
        self._clients.add(websocket)
        # The newest client is the active stream; only its capabilities
        # declaration may set the per-stream flags (wh-nvyh.1.1).
        self._active_stt_client = websocket
        # Stamp the connection before any await, so a notification
        # arriving on it can be attributed to the launch that sent it
        # rather than to whichever launch is current when it arrives
        # (wh-launch-generation). The stamp is PROVISIONAL: nothing the
        # client has sent yet says which provider this is, and a slow
        # provider from an earlier launch can connect while a later
        # launch is starting. _apply_capabilities corrects it from the
        # provider's own declaration (wh-launch-generation.1.2).
        self._stt_client_generations[websocket] = self._current_launch_generation()
        self._stt_client_provisional.add(websocket)

        # A provider (re)connect matters to a live calibration session
        # (wh-7ou.7.2.3): mid-capture it means the provider reset
        # calibration mode on disconnect and must be told to turn it
        # back on (spec Section 5.2); post-apply it means the settings
        # restart completed. The controller ignores the call in every
        # other state. Guarded so a controller failure can never break
        # client registration.
        controller = self._calibration_controller()
        if controller is not None:
            try:
                controller.on_provider_connected()
            except Exception:
                logger.exception(
                    "Calibration provider-connect handling failed"
                )

        # Disable existing clients (but keep them connected)
        if existing_clients:
            logger.info(f"New STT client connecting - disabling {len(existing_clients)} existing client(s)")
            disable_msg = json.dumps({"type": "set_transcription_status", "enabled": False})
            for existing_client in existing_clients:
                try:
                    await existing_client.send(disable_msg)
                except Exception as e:
                    logger.warning(f"Error disabling existing client: {e}")

    def remove_client(self, websocket: Any):
        """Unregisters a client connection and cleans up in-progress utterances.

        Resets wh-x4fwo retraction policy state ONLY when the removed client
        was the last one. If other clients remain, the active stream is still
        operating and its state must not be wiped by an old disabled client's
        disconnect (wh-eknz.2 case 2).
        """
        if websocket in self._clients:
            logger.info(f"STT client disconnected: {websocket.remote_address}")
            self._clients.remove(websocket)

            # Its engine clears the suppression bypass itself on
            # disconnect; a dangling binding would misdirect a later
            # session's mode-off (wh-7ou.7.6.11).
            self._calibration_mode_clients.discard(websocket)

            # If the active client left, no client is active until the next
            # add_client -- a lingering DISABLED client must not become able
            # to set per-stream capabilities by default (wh-nvyh.1.1).
            # The launch stamp leaves with the connection it identified;
            # nothing can arrive on a closed connection, and the map
            # would otherwise grow for the life of the process
            # (wh-launch-generation).
            self._stt_client_generations.pop(websocket, None)
            self._stt_client_provisional.discard(websocket)

            if websocket is self._active_stt_client:
                self._active_stt_client = None
                # The departed stream's declared capability and diagnostic
                # counters must leave with it, even when disabled clients
                # remain connected (codex finding wh-nvyh.3.1). Otherwise a
                # disabled client's late finals could hit the
                # EOS_NOT_RECEIVED gate armed by a provider that is gone.
                # The per-stream state all belongs to the departed active
                # stream, so resetting it here cannot wipe a live stream's
                # state -- there is no active stream until the next
                # add_client, and add_client resets again anyway.
                self._reset_retraction_policy_state()

            # Only when the removed client was the LAST one do we touch the
            # active stream's state. add_client DISABLES older clients but keeps
            # them connected, so an old disabled client can disconnect later
            # while the active client is mid-utterance. In that case the active
            # stream is still alive: its retraction-policy state, in-progress
            # utterance, working badge, and idle watchdog must NOT be cleaned up
            # by the disabled client's disconnect (wh-eknz.2 case 2, extended to
            # the utterance/badge/watchdog for wh-dictation-retraction-indicator.10.1).
            if not self._clients:
                # No active stream remains.
                self._reset_retraction_policy_state()

                # Clean up any in-progress utterance to prevent clipboard timeout
                if self.current_utterance_id is not None:
                    logger.warning(f"STT disconnected with UTT-{self.current_utterance_id} in progress - queuing cleanup end marker")
                    # Queue end marker synchronously using put_nowait
                    try:
                        end_marker = WordEvent(
                            word="",
                            start_of_utterance=False,
                            end_of_utterance=True,
                            utterance_id=self.current_utterance_id,
                            is_utterance_end_marker=True
                        )
                        self.word_queue.put_nowait(end_marker)
                        logger.debug(f"Queued cleanup end marker for UTT-{self.current_utterance_id}")
                    except Exception as e:
                        logger.error(f"Failed to queue cleanup end marker: {e}")

                    # Clear the working/busy indicator immediately, but ONLY when
                    # this utterance had no final yet. The final cancels the idle
                    # watchdog (and already wrote 'confirmed'), so an armed watchdog
                    # here means provisional text is still on screen with no final
                    # coming -- write 'idle' to clear the badge now instead of
                    # waiting out the long last-resort fallback. If the final already
                    # arrived (watchdog cancelled), leave 'confirmed' intact and do
                    # not flash a spurious 'idle' (wh-dictation-retraction-indicator.9.1).
                    if self._idle_watchdog_handle is not None:
                        self._write_activity_state('idle', self.current_utterance_id)

                    # Reset state
                    self.current_utterance_id = None
                    self._last_stable_utterance_id = None

                # Cancel any pending idle watchdog -- the last client is gone.
                self._cancel_idle_watchdog()

    async def broadcast(self, message: Dict[str, Any]):
        """
        Sends a JSON message to all connected clients.

        :flow: WebSocket Communication
        :step: 3
        :description: Broadcasts status updates or messages to all connected STT clients
        :data_in: message (dict)
        :data_out: JSON message sent to all websockets
        :notes: Used for synchronization (e.g., transcription enabled/disabled status).
        """
        if not self._clients:
            return

        logger.debug(f"Broadcasting message to {len(self._clients)} clients: {_redact_content_fields(message)}")
        message_json = json.dumps(message)
        tasks = [client.send(message_json) for client in self._clients]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result, client in zip(results, self._clients):
            if isinstance(result, Exception):
                logger.warning(f"Failed to send message to client {client.remote_address}: {result}")

    def set_transcription_status(self, enabled: bool, reason: str = None):
        """
        Sets the transcription status and returns a message for broadcasting.

        Args:
            enabled: Whether transcription should be enabled.
            reason: Optional reason for the status change (e.g., "idle", "wake_word").
        """
        self.transcription_enabled = enabled
        # Track suppression reason so reconnecting providers get the current state
        if enabled:
            self._suppression_reason = None
        elif reason is not None:
            self._suppression_reason = reason
        logger.info(f"Transcription status set to: {'ENABLED' if enabled else 'DISABLED'}"
                     + (f" (reason={reason})" if reason else ""))
        msg = {
            "type": "set_transcription_status",
            "enabled": self.transcription_enabled
        }
        if reason is not None:
            msg["reason"] = reason
        return msg

    def get_current_status_message(self) -> Dict[str, Any]:
        """
        Returns the current transcription status as a message dictionary.
        Includes suppression reason when transcription is disabled, so
        reconnecting STT providers can activate wake word listening.
        """
        msg: Dict[str, Any] = {
            "type": "set_transcription_status",
            "enabled": self.transcription_enabled
        }
        if not self.transcription_enabled and self._suppression_reason is not None:
            msg["reason"] = self._suppression_reason
        return msg

    def set_log_level(self, level: str):
        """Update the stored log level for sending to newly connected providers."""
        self._current_log_level = level

    async def send_command_to_stt(self, command_type: str, **params):
        """Send a command to all connected STT clients.
        
        This method broadcasts commands to the STT server(s) for operations like:
        - Adding hints to the STT configuration
        - Restarting the STT service
        - Other control operations
        
        Args:
            command_type: Type of command (e.g., "add_hint", "restart_service")
            **params: Additional parameters for the command
        
        Example:
            await ws_manager.send_command_to_stt("add_hint", hint="antigravity")
            await ws_manager.send_command_to_stt("restart_service")
        """
        message = {"type": command_type, **params}
        logger.info(f"Sending command to STT clients: {command_type} with params: {_redact_content_fields(params)}")
        await self.broadcast(message)

    async def send_command_to_active_stt(self, command_type: str, **params):
        """Send a command to the ACTIVE STT client only (wh-7ou.7.6.6).

        add_client keeps superseded providers connected in DISABLED
        state, so send_command_to_stt's broadcast would also reach them.
        Calibration commands (set_calibration_mode /
        apply_engine_settings) concern exactly one provider generation
        -- a stale whisper provider honoring them would act on a session
        that is not its own -- so they target the active stream. Returns
        True when the command was sent, False when there was no active
        client or the send failed; the apply path turns False into an
        ok=false settings result so the window never waits on a command
        that never left this process (wh-7ou.7.6.7).
        """
        client = self._active_stt_client
        if command_type == "set_calibration_mode" and not params.get("enabled"):
            # Mode-off must reach every provider whose suppression
            # bypass is actually on. On a provider-switch session end,
            # add_client has already promoted the NEW client by the time
            # the controller ends the session, so "active" would aim the
            # off at a provider that never had mode on and leave the old
            # one bypassing its filter until disconnect or the 10-minute
            # lazy timeout (wh-7ou.7.6.11); a same-provider reconnect
            # mid-capture leaves TWO live enabled clients, so the off
            # fans out to all of them (wh-7ou.7.6.12). The bindings are
            # consumed before any send and whether or not the sends
            # succeed -- on failure the provider's own disconnect reset
            # and lazy timeout are the backstops, same as before the
            # binding existed. When no bound client is still connected,
            # fall through to the active client (an idempotent no-op
            # there).
            bound = {c for c in self._calibration_mode_clients
                     if c in self._clients}
            self._calibration_mode_clients.clear()
            if bound:
                message_json = json.dumps({"type": command_type, **params})
                logger.info(
                    f"Sending command to {len(bound)} calibration-enabled "
                    f"STT client(s): {command_type} "
                    f"with params: {_redact_content_fields(params)}"
                )

                # Concurrent and bounded per target (wh-7ou.7.6.14): a
                # serial unbounded loop let one stalled client's
                # backpressured send starve every later target -- with
                # the set already consumed, the healthy active provider
                # then stayed in calibration mode until its 600-second
                # engine timeout. The cancel a timeout fires may break
                # the stalled client's connection state; acceptable,
                # its engine clears the bypass itself on disconnect.
                async def _send_off(target):
                    try:
                        await asyncio.wait_for(
                            target.send(message_json),
                            timeout=_CAL_MODE_OFF_SEND_TIMEOUT_S,
                        )
                        return True
                    except Exception as e:
                        logger.warning(
                            f"Failed to send {command_type} to STT client "
                            f"{getattr(target, 'remote_address', None)}: {e}"
                        )
                        return False

                results = await asyncio.gather(
                    *(_send_off(target) for target in bound)
                )
                return all(results)
        if client is None:
            logger.warning(
                f"No active STT client; dropping command: {command_type} "
                f"with params: {_redact_content_fields(params)}"
            )
            return False
        message = {"type": command_type, **params}
        if command_type == "set_calibration_mode" and params.get("enabled"):
            # Remember who is getting the bypass turned on, so the off
            # can follow it after another client is promoted
            # (wh-7ou.7.6.11). Accumulated, never overwritten: an
            # earlier enabled client stays bound until an off consumes
            # the set or it disconnects (wh-7ou.7.6.12). Recorded
            # before the send for the same reason as the apply binding
            # below; deliberately left in place when a send fails -- a
            # failed 300-second REFRESH send means the bypass is still
            # on from the earlier successful send, and a failed first
            # send costs only a harmless idempotent off later.
            self._calibration_mode_clients.add(client)
        if command_type == "apply_engine_settings":
            # Bind the reply to this client AND this operation BEFORE
            # the send completes: the provider can answer while this
            # coroutine is still suspended in send(), and a connect
            # promoting another client in that window must not orphan
            # the reply (wh-7ou.7.6.6 round 4). The id rides the
            # command and comes back in the reply, so a delayed reply
            # from an earlier apply on the same connection cannot be
            # read as this one's answer (wh-7ou.7.6.9).
            self._engine_settings_reply_client = client
            self._engine_settings_reply_id = uuid.uuid4().hex
            message["apply_id"] = self._engine_settings_reply_id
        message_json = json.dumps(message)
        logger.info(
            f"Sending command to active STT client: {command_type} "
            f"with params: {_redact_content_fields(params)}"
        )
        try:
            await client.send(message_json)
        except Exception as e:
            if command_type == "apply_engine_settings":
                self._engine_settings_reply_client = None
                self._engine_settings_reply_id = None
            logger.warning(
                f"Failed to send {command_type} to active STT client "
                f"{getattr(client, 'remote_address', None)}: {e}"
            )
            return False
        return True
