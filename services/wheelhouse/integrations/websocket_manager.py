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
from typing import Set, Dict, Any, Callable, Optional
import websockets
from multiprocessing import shared_memory
from speech.word_event import WordEvent
from utils.trace_context import set_trace
from utils.redact import redact_transcript

logger = logging.getLogger(__name__)

# Keys whose values carry user content in broadcast/command payloads.
# Everything else (type, flags, log levels, ids) stays verbatim so the
# payload remains diagnosable with redaction on (wh-797.17.3).
_CONTENT_KEYS = frozenset({"hint", "text", "word", "words", "message", "transcript"})


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
        # The newest connected client -- the one whose transcripts drive
        # the pipeline (older clients stay connected but DISABLED). Only
        # this client's capabilities declaration is honored (wh-nvyh.1.1).
        self._active_stt_client: Optional[Any] = None
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

                    # Handle notification messages
                    if msg_type == "notification":
                        title = data.get("title", "Wheelhouse Notification")
                        notification_message = data.get("message", "")
                        logger.info(f"Received notification: {title} - {redact_transcript(notification_message)}")

                        # Check if this is a "ready" notification from STT provider
                        # Signal the launcher to cancel the startup timeout monitor.
                        # wh-v0q follow-up: substring match is brittle (matches "already",
                        # "not ready", "Ready to retry"). Replace with a structured field
                        # (e.g. kind="ready" in the notification payload from
                        # shared_stt/ws_forwarder.py:send_notification) before touching
                        # tlog->logger wiring (wh-6wp) -- that refactor will reshape how
                        # providers emit startup messages and is a natural moment to
                        # introduce the discriminator.
                        if self.remote_stt_launcher and "ready" in notification_message.lower():
                            self.remote_stt_launcher.signal_provider_ready()
                            # Close the working dialog
                            if self.state_manager and hasattr(self.state_manager, 'state_to_gui_queue'):
                                try:
                                    self.state_manager.state_to_gui_queue.put_nowait({"action": "hide_working"})
                                except Exception:
                                    pass
                            continue  # Working dialog dismissal is sufficient; skip toast

                        # Suppress toast during provider startup -- working dialog is sufficient
                        if self.remote_stt_launcher and self.remote_stt_launcher.is_starting:
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
                        logger.log(level, formatted_msg)
                        continue

                    # Handle "capabilities" messages -- the provider declares
                    # what it can do right after connecting (wh-nvyh). Today
                    # the only consumed capability is emits_eos, which gates
                    # the EOS_NOT_RECEIVED diagnostic warning. The flag is
                    # per-stream: add_client resets it when a new client
                    # becomes the active stream, and the provider's forwarder
                    # re-sends the declaration on every (re)connect.
                    if msg_type == "capabilities":
                        self._apply_capabilities(websocket, data)
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

                            for i, word in enumerate(delta_words):
                                is_first = (i == 0 and previous_count == 0)  # First word of utterance
                                word_event = WordEvent(
                                    word=word,
                                    start_of_utterance=is_first,
                                    end_of_utterance=False,
                                    utterance_id=utterance_id,
                                    trace_id=trace_id,
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

    async def add_client(self, websocket: Any):
        """Registers a new client connection, disabling existing clients.

        Multiple STT providers can stay connected, but only the newest one is
        enabled. When a new provider connects, existing connections are sent
        DISABLE so they stop sending transcripts but remain connected for
        potential re-enabling later.
        """
        # Disable existing clients (but keep them connected)
        if self._clients:
            logger.info(f"New STT client connecting - disabling {len(self._clients)} existing client(s)")
            disable_msg = json.dumps({"type": "set_transcription_status", "enabled": False})
            for existing_client in list(self._clients):
                try:
                    await existing_client.send(disable_msg)
                except Exception as e:
                    logger.warning(f"Error disabling existing client: {e}")

        # The new client becomes the active stream. Reset retraction policy
        # state so the prior active stream's per-utterance and diagnostic
        # state cannot cross-contaminate. (wh-eknz.1, wh-eknz.2)
        self._reset_retraction_policy_state()

        logger.info(f"STT client connected: {websocket.remote_address}")
        self._clients.add(websocket)
        # The newest client is the active stream; only its capabilities
        # declaration may set the per-stream flags (wh-nvyh.1.1).
        self._active_stt_client = websocket

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

            # If the active client left, no client is active until the next
            # add_client -- a lingering DISABLED client must not become able
            # to set per-stream capabilities by default (wh-nvyh.1.1).
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
