"""WebSocket forwarder for sending transcripts to WheelHouse service.

This module manages the WebSocket connection between STT servers and
the WheelHouse service. It handles transcript forwarding with automatic reconnection,
backoff strategies, and thread-safe queuing to ensure reliable delivery of
speech transcription results.

Key Classes:
  - WSForwarder: Background WebSocket client with reconnection capabilities.
  - WebSocketLogHandler: Python logging.Handler that forwards logs via WebSocket.

Key Methods:
  - start: Starts the WebSocket forwarding thread.
  - stop: Gracefully stops the forwarder.
  - send_stable: Sends stable (partial) transcript.
  - send_final: Sends final transcript.
  - send_log: Sends log message for centralized logging.

Typical Usage:
  from shared.stt import WSForwarder, WebSocketLogHandler

  forwarder = WSForwarder(host="localhost", port=<port>,
                         transcription_enabled_event=event)
  forwarder.start()

  # Set up log forwarding
  log_handler = WebSocketLogHandler(forwarder, source="My Provider")
  logging.getLogger().addHandler(log_handler)

  # Send transcripts
  forwarder.send_stable("Hello", utterance_id=0)
  forwarder.send_final("Hello world", utterance_id=0)

  forwarder.stop()
"""
import asyncio
import importlib
import json
import logging
import time
import threading
from typing import Callable, Optional

from shared_stt.redact import redact_transcript


def generate_trace_id() -> str:
    """Generate a trace ID from the current time (decisecond precision).

    Format: T-{11_digit_timestamp} where timestamp is Unix epoch in deciseconds.
    Example: T-17720345601
    """
    return f"T-{int(time.time() * 10)}"

logger = logging.getLogger(__name__)


class WSForwarder:
    """Background WebSocket sender with simple reconnect/backoff."""

    def __init__(
        self,
        host: str,
        port: int,
        transcription_enabled_event: threading.Event,
        add_hint_callback: Optional[Callable[[str], None]] = None,
        restart_callback: Optional[Callable[[], None]] = None,
        hard_restart_callback: Optional[Callable[[], None]] = None,
        on_disconnect_callback: Optional[Callable[[], None]] = None,
        on_reconnect_callback: Optional[Callable[[], None]] = None,
        shutdown_callback: Optional[Callable[[], None]] = None,
        set_interim_results_callback: Optional[Callable[[bool], None]] = None,
        set_log_level_callback: Optional[Callable[[str], None]] = None,
        wake_word_activate_callback: Optional[Callable] = None,
        debug: bool = False,
        log_func: Optional[Callable[[str], None]] = None,
        provider_name: str = "",
        emits_eos: bool = False,
    ):
        """Initialize the WebSocket forwarder.

        Args:
            host: WebSocket server host
            port: WebSocket server port
            transcription_enabled_event: Event to signal transcription enable/disable
            add_hint_callback: Called when server sends add_hint command
            restart_callback: Called when server sends restart_service command
            hard_restart_callback: Called when server sends hard_restart_service command
            on_disconnect_callback: Called when connection is lost (after being connected)
            on_reconnect_callback: Called when connection is re-established after disconnect
            shutdown_callback: Called when server sends shutdown command
            set_interim_results_callback: Called when server sends set_interim_results command
            set_log_level_callback: Called when server sends set_log_level command
            wake_word_activate_callback: Called with reason when transcription status changes
                (reason string when disabled with reason, None when enabled or disabled without reason)
            debug: Enable debug logging
            log_func: Optional custom logging function (defaults to logger.info)
            provider_name: Provider identity announced in the capabilities
                frame on every (re)connect (wh-nvyh)
            emits_eos: Whether this provider emits the eos lifecycle event;
                announced in the capabilities frame and consumed by
                WheelHouse's EOS_NOT_RECEIVED warning gate
        """
        self.uri = f"ws://{host}:{port}"
        self.debug = debug
        self.transcription_enabled_event = transcription_enabled_event
        self.add_hint_callback = add_hint_callback
        self.restart_callback = restart_callback
        self.hard_restart_callback = hard_restart_callback
        self.on_disconnect_callback = on_disconnect_callback
        self.on_reconnect_callback = on_reconnect_callback
        self.shutdown_callback = shutdown_callback
        self.set_interim_results_callback = set_interim_results_callback
        self.set_log_level_callback = set_log_level_callback
        self.wake_word_activate_callback = wake_word_activate_callback
        self.provider_name = provider_name
        self.emits_eos = emits_eos
        self._log = log_func or logger.info
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._queue: asyncio.Queue[dict] | None = None
        self._current_trace_id: str = ""

    def start(self):
        """Start the WebSocket forwarder thread."""
        if self._thread and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._queue = asyncio.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self.debug:
            self._log(f"[ws] forwarder started -> {self.uri}")

    def _run(self):
        """Run the event loop in the background thread."""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._sender_loop())

    async def _clear_queue(self):
        """Clear all pending messages from the queue.

        Called after disconnect to prevent stale transcriptions from flooding
        WheelHouse when the connection is re-established.
        """
        if self._queue:
            cleared = 0
            while True:
                try:
                    self._queue.get_nowait()
                    cleared += 1
                except asyncio.QueueEmpty:
                    break
            if cleared and self.debug:
                self._log(f"[ws] Cleared {cleared} stale messages from queue")

    async def _sender_loop(self):
        """Main sender loop with reconnection logic.

        :flow: STT Transcription
        :step: 8
        :produces_for: Speech Processing
        :description: Maintains the client connection to WheelHouse. Pulls queued JSON payloads
        (delta transcripts, utterance boundaries, restart notifications) and streams them to the
        WheelHouse `WebSocketManager`, which immediately converts them into `WordEvent` objects for
        the `SpeechProcessor`. Handles connection failures with exponential backoff so real-time
        dictation survives transient network hiccups.
        :data_in: JSON-ready dicts from the forwarder's queue `{type, text, utterance_id, is_partial, ...}`
        :data_out: Serialized JSON frames delivered to WheelHouse `websocket_manager`
        """
        assert self._queue is not None
        backoff = 0.5
        was_connected = False  # Track if we've successfully connected
        was_disconnected = False  # Track if we've disconnected (for reconnect callback)

        while not self._stop_evt.is_set():
            try:
                ws_mod = importlib.import_module('websockets')
                async with ws_mod.connect(self.uri, ping_interval=None) as ws:  # type: ignore[attr-defined]
                    if self.debug:
                        self._log("[ws] connected")
                    backoff = 0.5

                    # Fire reconnect callback if this is a reconnection (not first connect)
                    if was_disconnected and self.on_reconnect_callback:
                        try:
                            if self._loop:
                                self._loop.call_soon_threadsafe(self.on_reconnect_callback)
                        except Exception as cb_err:
                            if self.debug:
                                self._log(f"[ws] reconnect callback error: {cb_err}")

                    was_connected = True  # Mark as connected
                    was_disconnected = False  # Reset disconnect flag

                    # Create a task to listen for incoming messages
                    listen_task = asyncio.create_task(self._listen_for_commands(ws))

                    # Wait briefly to allow the server's initial status message to be processed
                    # This prevents the first transcript word from being sent before the listener
                    # task has consumed the status message, which could cause message ordering issues
                    await asyncio.sleep(0.05)  # 50ms delay to ensure listener is ready

                    # Declare capabilities on every (re)connect (wh-nvyh).
                    # WheelHouse resets its per-stream emits_eos flag when a
                    # new client becomes the active stream, so this must be
                    # per-connection, not once per process. Sent directly
                    # (not via the queue): the queue is cleared on disconnect
                    # and a queued one-shot would not repeat on reconnect.
                    # A send failure is caught rather than raised so the
                    # listen task is still cancelled deterministically below
                    # (reviewer_0 finding wh-nvyh.1.2) -- the connection then
                    # follows the normal close/reconnect path.
                    capabilities_sent = True
                    try:
                        await ws.send(json.dumps({
                            "type": "capabilities",
                            "provider": self.provider_name,
                            "emits_eos": self.emits_eos,
                        }))
                        if self.debug:
                            self._log(
                                f"[ws] sent capabilities: provider={self.provider_name} "
                                f"emits_eos={self.emits_eos}"
                            )
                    except Exception as cap_err:
                        capabilities_sent = False
                        if self.debug:
                            self._log(
                                f"[ws] capabilities send error: {cap_err}; "
                                "reconnecting"
                            )

                    while capabilities_sent and not self._stop_evt.is_set():
                        # Check if connection was closed by the server
                        # websockets 15.x uses state attribute instead of closed
                        ws_state = importlib.import_module('websockets').State
                        if ws.state != ws_state.OPEN:
                            break

                        try:
                            msg_data = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue

                        text = msg_data.get("text", "")
                        utt_id = msg_data.get("utterance_id", 0)
                        is_partial = msg_data.get("is_partial", False)
                        msg_type = msg_data.get("type", "delta")

                        try:
                            trace_id = msg_data.get("trace_id", "")

                            # Build payload based on message type
                            if msg_type == "notification":
                                # Notification messages have title and message fields
                                payload = {
                                    "type": msg_type,
                                    "title": msg_data.get("title", ""),
                                    "message": msg_data.get("message", ""),
                                    "utterance_id": utt_id,
                                    "is_partial": is_partial
                                }
                            elif msg_type == "log":
                                # Log messages for centralized logging
                                payload = {
                                    "type": msg_type,
                                    "level": msg_data.get("level", "INFO"),
                                    "message": msg_data.get("message", ""),
                                    "source": msg_data.get("source", "STT"),
                                    "timestamp": msg_data.get("timestamp", ""),
                                    "utterance_id": utt_id,
                                    "is_partial": is_partial,
                                    "trace_id": trace_id,
                                }
                            elif msg_type == "wake_word_detected":
                                # Wake word detection event
                                payload = {
                                    "type": msg_type,
                                    "keyword": msg_data.get("keyword", ""),
                                    "utterance_id": utt_id,
                                    "is_partial": is_partial
                                }
                            elif msg_type == "eos":
                                # End-of-single-utterance signal from Google STT.
                                # No text or is_partial -- this is a lifecycle event
                                # consumed by WheelHouse's three-mode retraction policy
                                # (wh-x4fwo). Kept small for non-Google providers that
                                # never emit it.
                                payload = {
                                    "type": msg_type,
                                    "utterance_id": utt_id,
                                    "trace_id": trace_id,
                                }
                            else:
                                # Standard transcript messages (stable, final, vad_start)
                                payload = {
                                    "type": msg_type,
                                    "text": text,
                                    "utterance_id": utt_id,
                                    "is_partial": is_partial,
                                    "trace_id": trace_id,
                                }
                                # Optional final_reason on final messages -- omitted
                                # when None so non-Google providers stay payload-clean.
                                final_reason = msg_data.get("final_reason")
                                if final_reason is not None:
                                    payload["final_reason"] = final_reason

                            await ws.send(json.dumps(payload))
                            if self.debug:
                                partial_str = " (partial)" if is_partial else ""
                                self._log(f"[ws] UTT-{utt_id}{partial_str}: sent {msg_type} with {len(text)} chars")
                        except Exception as e:
                            if self.debug:
                                self._log(f"[ws] send error: {e}; requeueing message")
                            # Re-queue the message so it's not lost on reconnection
                            await self._queue.put(msg_data)
                            break

                    listen_task.cancel()

                # If we reach here and stop wasn't requested, we disconnected unexpectedly
                # This handles clean WebSocket closes (no exception thrown)
                if not self._stop_evt.is_set() and was_connected:
                    was_connected = False
                    was_disconnected = True  # Track for reconnect callback
                    self._log("[ws] Wheelhouse connection closed")

                    if self.on_disconnect_callback:
                        try:
                            self.on_disconnect_callback()
                        except Exception as cb_err:
                            if self.debug:
                                self._log(f"[ws] disconnect callback error: {cb_err}")

                    await self._clear_queue()
                    await asyncio.sleep(backoff)
                    backoff = min(5.0, backoff * 2)

            except Exception as e:
                # Only invoke callback if we were previously connected (not initial failure)
                if was_connected:
                    was_connected = False  # Reset for next connection cycle
                    was_disconnected = True  # Track for reconnect callback
                    self._log("[ws] Wheelhouse connection lost")

                    if self.on_disconnect_callback:
                        try:
                            self.on_disconnect_callback()
                        except Exception as cb_err:
                            if self.debug:
                                self._log(f"[ws] disconnect callback error: {cb_err}")

                    # Clear stale messages only after disconnect (not initial failure)
                    await self._clear_queue()

                if self.debug:
                    self._log(f"[ws] connect error: {e}; retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(5.0, backoff * 2)

    async def _listen_for_commands(self, ws):
        """Listen for control commands from WheelHouse.

        :flow: STT Command Handling
        :step: 1
        :consumes_from: WheelHouse Logic Process
        :description: Listens for control commands from WheelHouse over the WebSocket connection.
            Handles transcription enable/disable, hint additions, and restart requests.
            Hard restart triggers process exit for launcher to restart fresh.
        :data_in: JSON command messages {type: 'set_transcription_status'|'add_hint'|'restart_service'|'hard_restart_service'}
        :data_out: Callback invocations to STT main process for state changes
        """
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                    msg_type = data.get("type")

                    if msg_type == "set_transcription_status":
                        enabled = data.get("enabled", False)
                        reason = data.get("reason")
                        if enabled:
                            self.transcription_enabled_event.set()
                            self._log("[ws] Received command: ENABLE transcription")
                            if self.wake_word_activate_callback:
                                if self._loop:
                                    self._loop.call_soon_threadsafe(
                                        self.wake_word_activate_callback, None
                                    )
                        else:
                            self.transcription_enabled_event.clear()
                            self._log(f"[ws] Received command: DISABLE transcription"
                                      + (f" (reason={reason})" if reason else ""))
                            if self.wake_word_activate_callback:
                                if self._loop:
                                    self._loop.call_soon_threadsafe(
                                        self.wake_word_activate_callback, reason
                                    )

                    elif msg_type == "add_hint":
                        hint = data.get("hint", "")
                        if hint and self.add_hint_callback:
                            self._log(
                                f"[ws] Received command: ADD HINT "
                                f"'{redact_transcript(hint)}'"
                            )
                            # Call callback in a thread-safe way
                            if self._loop:
                                self._loop.call_soon_threadsafe(self.add_hint_callback, hint)
                        else:
                            self._log("[ws] Received add_hint command but no callback registered")

                    elif msg_type == "restart_service":
                        self._log("[ws] Received command: RESTART SERVICE")
                        if self.restart_callback:
                            # Call callback in a thread-safe way
                            if self._loop:
                                self._loop.call_soon_threadsafe(self.restart_callback)
                        else:
                            self._log("[ws] Received restart_service command but no callback registered")

                    elif msg_type == "hard_restart_service":
                        self._log("[ws] Received command: HARD RESTART SERVICE")
                        if self.hard_restart_callback:
                            # Call callback in a thread-safe way
                            if self._loop:
                                self._loop.call_soon_threadsafe(self.hard_restart_callback)
                        else:
                            self._log("[ws] Received hard_restart_service command but no callback registered")

                    elif msg_type == "shutdown":
                        self._log("[ws] Received command: SHUTDOWN")
                        if self.shutdown_callback:
                            # Call callback in a thread-safe way
                            if self._loop:
                                self._loop.call_soon_threadsafe(self.shutdown_callback)
                        else:
                            self._log("[ws] Received shutdown command but no callback registered")

                    elif msg_type == "set_interim_results":
                        enabled = data.get("enabled", False)
                        self._log(f"[ws] Received command: SET INTERIM RESULTS enabled={enabled}")
                        if self.set_interim_results_callback:
                            # Call callback in a thread-safe way with the enabled parameter
                            if self._loop:
                                self._loop.call_soon_threadsafe(
                                    self.set_interim_results_callback, enabled
                                )
                        else:
                            self._log("[ws] Received set_interim_results command but no callback registered")

                    elif msg_type == "set_log_level":
                        level = data.get("level", "INFO")
                        self._log(f"[ws] Received command: SET LOG LEVEL level={level}")
                        if self.set_log_level_callback:
                            if self._loop:
                                self._loop.call_soon_threadsafe(
                                    self.set_log_level_callback, level
                                )
                        else:
                            self._log("[ws] Received set_log_level command but no callback registered")

                except Exception as e:
                    self._log(f"[ws] Error processing command: {e}")
        except asyncio.CancelledError:
            pass  # Expected on disconnect
        except Exception as e:
            if self.debug:
                self._log(f"[ws] Listener error: {e}")

    def send_stable(self, text: str, utterance_id: int = 0, trace_id: str = ""):
        """Send full stable text for overlay mode (replaces previous display).

        In overlay mode, this sends the complete stable transcript so far,
        allowing the UI to replace (not append) the displayed text.

        Args:
            text: Full stable transcript text
            utterance_id: Current utterance identifier
            trace_id: Trace ID for observability
        """
        if not self._loop or not self._queue or not text:
            return

        msg_data = {
            "type": "stable",
            "text": text,
            "utterance_id": utterance_id,
            "is_partial": True,
            "trace_id": trace_id,
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_final(
        self,
        text: str,
        utterance_id: int = 0,
        trace_id: str = "",
        final_reason: Optional[str] = None,
    ):
        """Send final transcript - signals utterance end with complete text.

        This replaces the separate utterance_end signal. The final message
        carries both the complete transcript and the end-of-utterance signal.

        Args:
            text: Complete final transcript
            utterance_id: Utterance identifier
            trace_id: Trace ID for observability
            final_reason: Optional finalization-source tag for the WheelHouse
                three-mode retraction policy. One of "GOOGLE_FINAL",
                "GOOGLE_SILENCE_2S", "EOS_FALLBACK", "NO_TEXT_TIMEOUT". Omitted
                from the payload when None so non-Google providers send no
                extra bytes.
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "final",
            "text": text,
            "utterance_id": utterance_id,
            "is_partial": False,
            "trace_id": trace_id,
        }
        if final_reason is not None:
            msg_data["final_reason"] = final_reason

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_eos(self, utterance_id: int, trace_id: str = ""):
        """Send end-of-single-utterance lifecycle event.

        Google's streaming API emits END_OF_SINGLE_UTTERANCE when it decides
        the speaker stopped talking. WheelHouse's three-mode retraction policy
        (wh-x4fwo) uses the presence or absence of this signal to choose
        between trusting the stable transcript and trusting Google's final.

        Synchronous like send_stable, send_final, and send_vad_start.
        Non-Google providers do not emit this.

        Args:
            utterance_id: Current utterance identifier
            trace_id: Trace ID for observability
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "eos",
            "utterance_id": utterance_id,
            "trace_id": trace_id,
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_vad_start(self, utterance_id: int, trace_id: str = ""):
        """Send VAD speech start signal for instant visual feedback.

        This is sent when VAD commits to new speech (before any Google results).
        Allows the GUI to show immediate visual feedback within ~150ms of speech start.

        Args:
            utterance_id: Current utterance identifier
            trace_id: Trace ID for observability (stored as _current_trace_id)
        """
        if not self._loop or not self._queue:
            return

        self._current_trace_id = trace_id

        msg_data = {
            "type": "vad_start",
            "text": "",
            "utterance_id": utterance_id,
            "is_partial": False,
            "trace_id": trace_id,
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_wake_word_detected(self, keyword: str):
        """Send wake word detection event to WheelHouse.

        Args:
            keyword: The wake word or phrase that was detected.
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "wake_word_detected",
            "keyword": keyword,
            "utterance_id": 0,
            "is_partial": False
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_notification(self, title: str, message: str):
        """Send a notification message to trigger a Windows toast notification.

        Args:
            title: Notification title
            message: Notification message body
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "notification",
            "title": title,
            "message": message,
            "utterance_id": 0,
            "is_partial": False
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def send_log(self, level: str, message: str, source: str, timestamp: Optional[str] = None, trace_id: str = ""):
        """Send a log message to WheelHouse for centralized logging.

        Log messages are forwarded via WebSocket to WheelHouse, where they appear
        in the main wheelhouse.log with a provider prefix (e.g., "[Google STT]").

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message text
            source: Provider name for prefix (e.g., "Google STT", "Zipformer")
            timestamp: ISO format timestamp. Auto-generated if not provided.
            trace_id: Trace ID for observability
        """
        if not self._loop or not self._queue:
            return

        from datetime import datetime
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        msg_data = {
            "type": "log",
            "level": level,
            "message": message,
            "source": source,
            "timestamp": timestamp,
            "utterance_id": 0,
            "is_partial": False,
            "trace_id": trace_id,
        }

        try:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg_data), self._loop)
        except Exception:
            pass

    def stop(self):
        """Stop the forwarder thread."""
        self._stop_evt.set()
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self.debug:
            self._log("[ws] forwarder stopped")


class WebSocketLogHandler(logging.Handler):
    """Python logging.Handler that forwards logs to WheelHouse via WebSocket.

    This handler can be added to any logger to forward log messages to WheelHouse
    for centralized logging. The logs appear in wheelhouse.log with a provider
    prefix (e.g., "[Google STT] message").

    The handler buffers logs automatically via WSForwarder's queue - logs are
    queued before WebSocket connects and sent once connected.

    Example:
        forwarder = WSForwarder(...)
        forwarder.start()

        # Add to root logger for all messages
        handler = WebSocketLogHandler(forwarder, source="Google STT")
        logging.getLogger().addHandler(handler)

        # Or add to specific logger
        logger = logging.getLogger("my_module")
        logger.addHandler(handler)
    """

    def __init__(self, forwarder: WSForwarder, source: str, level: int = logging.DEBUG):
        """Initialize the WebSocket log handler.

        Args:
            forwarder: WSForwarder instance to send logs through
            source: Provider name for log prefix (e.g., "Google STT", "Zipformer")
            level: Minimum log level to forward (default: DEBUG forwards all)
        """
        super().__init__(level)
        self.forwarder = forwarder
        self.source = source

    def emit(self, record: logging.LogRecord):
        """Forward a log record to WheelHouse via WebSocket.

        Args:
            record: The log record to forward
        """
        try:
            # Format the message using the handler's formatter or default
            msg = self.format(record)

            # Get level name
            level_name = record.levelname

            # Get timestamp from the record (preserves original log time)
            from datetime import datetime
            timestamp = datetime.fromtimestamp(record.created).isoformat()

            # Send via forwarder (include current trace_id if available)
            self.forwarder.send_log(
                level=level_name,
                message=msg,
                source=self.source,
                timestamp=timestamp,
                trace_id=self.forwarder._current_trace_id,
            )
        except Exception:
            # Don't let logging errors crash the application
            self.handleError(record)
