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
        on_disconnect_callback: Optional[Callable[[], None]] = None,
        on_reconnect_callback: Optional[Callable[[], None]] = None,
        shutdown_callback: Optional[Callable[[], None]] = None,
        set_interim_results_callback: Optional[Callable[[bool], None]] = None,
        set_log_level_callback: Optional[Callable[[str], None]] = None,
        set_calibration_mode_callback: Optional[Callable[[bool], None]] = None,
        apply_engine_settings_callback: Optional[
            Callable[[dict, Optional[str]], None]
        ] = None,
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
            on_disconnect_callback: Called when connection is lost (after being connected)
            on_reconnect_callback: Called when connection is re-established after disconnect
            shutdown_callback: Called when server sends shutdown command
            set_interim_results_callback: Called when server sends set_interim_results command
            set_log_level_callback: Called when server sends set_log_level command
            set_calibration_mode_callback: Called with the enabled bool when
                the server sends set_calibration_mode (wh-7ou.7.1.2). Also
                called with False whenever an established connection is
                lost -- the safety reset that keeps a crashed Logic process
                from leaving the hallucination filter disabled
            apply_engine_settings_callback: Called with the settings dict
                (every key except "type" and "apply_id") and the command's
                apply_id (None when absent) when the server sends
                apply_engine_settings (wh-7ou.7.1.3). The provider validates
                keys and values, writes its config, and replies via
                send_engine_settings_result, echoing the apply_id so the
                reply can be correlated to the exact apply it answers
                (wh-7ou.7.6.9)
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
        self.on_disconnect_callback = on_disconnect_callback
        self.on_reconnect_callback = on_reconnect_callback
        self.shutdown_callback = shutdown_callback
        self.set_interim_results_callback = set_interim_results_callback
        self.set_log_level_callback = set_log_level_callback
        self.set_calibration_mode_callback = set_calibration_mode_callback
        self.apply_engine_settings_callback = apply_engine_settings_callback
        self.wake_word_activate_callback = wake_word_activate_callback
        self.provider_name = provider_name
        self.emits_eos = emits_eos
        # Whether this provider's wake-word detector loaded, announced in
        # the capabilities frame (wh-audio-suppression-control C3).
        # WheelHouse's sound-pause notice tells the user to say the wake
        # word, and it must not promise one that cannot fire: the
        # openwakeword import is guarded and the detector stays unloaded
        # when the import fails or the model is missing.
        #
        # Not a constructor argument, and read when the frame is sent
        # rather than here: the parakeet and distil servers build this
        # forwarder BEFORE they build their detector, so they cannot know
        # the value yet. They assign this attribute once the detector
        # exists. False is the safe default -- a provider that never sets
        # it declares no wake word.
        self.wake_word_available = False
        # Whether the running engine applies a saved hint
        # (wh-boost-engine-qualification). Declared in the capabilities
        # frame only when a provider sets it: None leaves the key out, so
        # WheelHouse keeps the value unknown and the "boost" command works
        # as it did before the field existed. Set by each provider main
        # before start(), the same way as wake_word_available.
        self.applies_hints: bool | None = None
        self._log = log_func or logger.info
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._queue: asyncio.Queue[dict] | None = None
        # A single log frame whose send failed, held outside the queue so
        # the disconnect clear cannot reach it and the drain loop takes it
        # before anything else (wh-forwarded-log-time-order defect 2). One
        # slot is enough: the send failure breaks the drain loop, so at most
        # one frame per connection can fail, and the next connection sends
        # this one first -- putting it straight back here if it fails again.
        # crewcut: stop() waits on the queue only, so a frame sitting here
        # during the reconnect window is not waited for at shutdown.
        # Removing that means giving stop() a second thing to wait on, and
        # the shutdown drain is already bounded by drain_timeout.
        self._pending_log: dict | None = None
        # Instance-level mirror of the sender loop's connection state,
        # read by stop() from other threads to decide whether queued
        # frames can still deliver (wh-7ou.7.6.10).
        self._ws_connected = False
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

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket to WheelHouse is up right now.

        A producer that must not build a backlog reads this before queueing:
        the outbound queue is unbounded, is created before any connection
        exists, and is cleared only after a connection that had already
        succeeded is lost, so a run of initial connect failures retains
        everything queued behind it (wh-stt-load-metrics.2.1.7).
        """
        return self._ws_connected

    def _run(self):
        """Run the event loop in the background thread."""
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._sender_loop())

    def _reset_calibration_mode_on_disconnect(self):
        """wh-7ou.7.1.2 safety: force calibration mode off on connection loss.

        The mode can only have been enabled by a command over the connection
        that just dropped, and its peer (the Logic process) may have crashed
        mid-session -- so the hallucination filter must never stay bypassed
        past the disconnect. Called from both disconnect paths of
        _sender_loop, only after an established connection is lost (initial
        connection failures cannot have enabled the mode).
        """
        if self.set_calibration_mode_callback:
            try:
                self.set_calibration_mode_callback(False)
            except Exception as cb_err:
                if self.debug:
                    self._log(f"[ws] calibration mode reset error: {cb_err}")

    async def _clear_queue(self):
        """Drop the stale frames after a disconnect, but keep the log frames.

        Called after disconnect to prevent stale transcriptions from flooding
        WheelHouse when the connection is re-established. A transcript from
        before the outage must not arrive as if it were current speech, and
        the same is true of the lifecycle frames that describe utterances
        nobody is waiting for any more.

        A forwarded log record is different, and this is the split
        wh-forwarded-log-time-order defect 2 asked for. It describes a moment
        that has already passed and carries its own source timestamp saying
        when, so arriving late costs nothing -- while the disconnect that
        discarded it is often the very event the reader of wheelhouse.log is
        trying to explain. Keeping them also stops a queued suppression
        report from vanishing on the disconnect that made it worth reading.

        Order among the kept frames is preserved: they go back in the order
        they came out, ahead of anything queued afterwards.
        """
        if self._queue:
            cleared = 0
            kept = []
            while True:
                try:
                    msg_data = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if msg_data.get("type") == "log":
                    kept.append(msg_data)
                else:
                    cleared += 1
            for msg_data in kept:
                self._queue.put_nowait(msg_data)
            if (cleared or kept) and self.debug:
                self._log(
                    f"[ws] Cleared {cleared} stale messages from queue, "
                    f"kept {len(kept)} log messages"
                )

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
                    self._ws_connected = True
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
                    capabilities = {
                        "type": "capabilities",
                        "provider": self.provider_name,
                        "emits_eos": self.emits_eos,
                        "wake_word_available": self.wake_word_available,
                    }
                    if self.applies_hints is not None:
                        capabilities["applies_hints"] = bool(
                            self.applies_hints
                        )
                    try:
                        await ws.send(json.dumps(capabilities))
                        if self.debug:
                            self._log(
                                f"[ws] sent capabilities: provider={self.provider_name} "
                                f"emits_eos={self.emits_eos} "
                                f"wake_word_available={self.wake_word_available} "
                                f"applies_hints={self.applies_hints}"
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

                        if self._pending_log is not None:
                            # A log frame whose send failed on an earlier
                            # connection goes out before anything queued
                            # since (wh-forwarded-log-time-order defect 2).
                            msg_data = self._pending_log
                            self._pending_log = None
                        else:
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
                                # Notification messages have title and message
                                # fields, plus the machine-readable kind that
                                # WheelHouse routes on -- dropping it here
                                # silently disables that routing
                                # (wh-google-creds-file-picker.1.15).
                                # capture_backend is in this list for the
                                # same reason: this payload is rebuilt
                                # key by key, so a field added to
                                # send_notification and not added here
                                # never leaves the process
                                # (wh-capture-winrt-required A4).
                                payload = {
                                    "type": msg_type,
                                    "title": msg_data.get("title", ""),
                                    "message": msg_data.get("message", ""),
                                    "kind": msg_data.get("kind", ""),
                                    "capture_backend": msg_data.get(
                                        "capture_backend", ""),
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
                            elif msg_type == "engine_settings_result":
                                # Reply to apply_engine_settings
                                # (wh-7ou.7.1.3, contract A.4). Exactly the
                                # contract keys -- this is a command
                                # outcome, not a transcript, so it carries
                                # no text/utterance fields. The apply_id
                                # echoes the command's correlation id
                                # (wh-7ou.7.6.9).
                                payload = {
                                    "type": msg_type,
                                    "ok": msg_data.get("ok", False),
                                    "error": msg_data.get("error"),
                                    "apply_id": msg_data.get("apply_id"),
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
                                # Optional confidence measurement block on final
                                # messages (wh-7ou.7.1.1) -- omitted when absent
                                # so non-Whisper providers stay payload-clean.
                                confidence = msg_data.get("confidence")
                                if confidence is not None:
                                    payload["confidence"] = confidence

                            await ws.send(json.dumps(payload))
                            if self.debug:
                                partial_str = " (partial)" if is_partial else ""
                                self._log(f"[ws] UTT-{utt_id}{partial_str}: sent {msg_type} with {len(text)} chars")
                        except Exception as e:
                            if self.debug:
                                self._log(f"[ws] send error: {e}; requeueing message")
                            if msg_data.get("type") == "log":
                                # Hold it outside the queue. The break below
                                # ends this connection, and every path from
                                # there runs _clear_queue -- which is why the
                                # old tail requeue lost the frame rather than
                                # reordering it (wh-forwarded-log-time-order
                                # defect 2). From here it survives the clear
                                # and leads the next connection.
                                self._pending_log = msg_data
                            else:
                                # A transcript or lifecycle frame goes back on
                                # the queue and is dropped by that same clear,
                                # which is the wanted outcome: stale words
                                # must not arrive as current speech.
                                await self._queue.put(msg_data)
                            break

                    listen_task.cancel()

                # If we reach here and stop wasn't requested, we disconnected unexpectedly
                # This handles clean WebSocket closes (no exception thrown)
                if not self._stop_evt.is_set() and was_connected:
                    was_connected = False
                    self._ws_connected = False
                    was_disconnected = True  # Track for reconnect callback
                    self._log("[ws] Wheelhouse connection closed")

                    self._reset_calibration_mode_on_disconnect()

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
                    self._ws_connected = False
                    was_disconnected = True  # Track for reconnect callback
                    self._log("[ws] Wheelhouse connection lost")

                    self._reset_calibration_mode_on_disconnect()

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
        :data_in: JSON command messages {type: 'set_transcription_status'|'add_hint'|'restart_service'}
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

                    elif msg_type == "set_calibration_mode":
                        # wh-7ou.7.1.2 (contract A.2): toggles the engine's
                        # hallucination-suppression bypass for the voice
                        # calibration session.
                        enabled = data.get("enabled", False)
                        self._log(f"[ws] Received command: SET CALIBRATION MODE enabled={enabled}")
                        if self.set_calibration_mode_callback:
                            if self._loop:
                                self._loop.call_soon_threadsafe(
                                    self.set_calibration_mode_callback, bool(enabled)
                                )
                        else:
                            self._log("[ws] Received set_calibration_mode command but no callback registered")

                    elif msg_type == "apply_engine_settings":
                        # wh-7ou.7.1.3 (contract A.3): calibrated single-word
                        # rescue thresholds to write into the provider's
                        # config. Only "type" and "apply_id" are stripped
                        # here -- key and value validation belongs to the
                        # provider handler, so illegal keys must arrive
                        # intact for it to reject. The apply_id is transport
                        # correlation, not a setting: it travels alongside
                        # and is echoed in the reply (wh-7ou.7.6.9). Log
                        # keys only: values in a malformed command could be
                        # arbitrary text.
                        apply_id = data.get("apply_id")
                        settings = {
                            k: v for k, v in data.items()
                            if k not in ("type", "apply_id")
                        }
                        self._log(
                            "[ws] Received command: APPLY ENGINE SETTINGS "
                            f"keys={sorted(settings.keys())}"
                        )
                        if self.apply_engine_settings_callback:
                            if self._loop:
                                self._loop.call_soon_threadsafe(
                                    self.apply_engine_settings_callback,
                                    settings, apply_id,
                                )
                        else:
                            self._log("[ws] Received apply_engine_settings command but no callback registered")

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
        confidence: Optional[dict] = None,
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
            confidence: Optional measurement block from the Whisper engine
                (wh-7ou.7.1.1): {"min_word_probability": float|None,
                "max_no_speech_prob": float|None, "peak_avg_logprob":
                float|None, "word_count": int, "suppressed": bool,
                "rescued": bool}. Attached to every Whisper final for
                diagnostics and voice calibration; omitted from the payload
                when None so other providers send no extra bytes.
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
        if confidence is not None:
            msg_data["confidence"] = confidence

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

    def send_engine_settings_result(
        self, ok: bool, error: Optional[str] = None,
        apply_id: Optional[str] = None,
    ):
        """Send the apply_engine_settings outcome back to WheelHouse.

        Contract A.4 (wh-7ou.7.1.3): {"type": "engine_settings_result",
        "ok": true|false, "error": str|null, "apply_id": str|null}. Sent
        BEFORE the provider's hard restart on success; on a validation or
        write failure it carries the exact error text (shown verbatim
        under the calibration window's "Show details") and the provider
        does not restart.

        Args:
            ok: Whether the settings were validated and written
            error: Exact failure text, or None on success
            apply_id: The correlation id echoed from the
                apply_engine_settings command (wh-7ou.7.6.9) -- WheelHouse
                accepts the reply only when it echoes the id of the apply
                actually in flight, so a delayed reply from an abandoned
                apply cannot be read as the current one's answer
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "engine_settings_result",
            "ok": bool(ok),
            "error": error,
            "apply_id": apply_id,
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

    def send_notification(self, title: str, message: str, kind: str = "",
                          capture_backend: str = ""):
        """Send a notification message to trigger a Windows toast notification.

        Args:
            title: Notification title
            message: Notification message body
            kind: Machine-readable classification ("ready",
                "startup_failed", "error", or "" for a plain notice) so
                WheelHouse can route on it instead of substring-matching
                the message text (wh-google-creds-file-picker.1.5)
            capture_backend: The capture path this provider took, on a
                kind="ready" notice; "" on every other notice, which is
                the only honest answer when the provider is not
                reporting a working microphone. WheelHouse writes it
                into wheelhouse.log at ready
                (wh-capture-winrt-required A4). Always present in the
                frame, empty rather than absent, so WheelHouse reads one
                shape for every notification.
        """
        if not self._loop or not self._queue:
            return

        msg_data = {
            "type": "notification",
            "title": title,
            "message": message,
            "kind": kind,
            "capture_backend": capture_backend,
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
            source: Provider name for prefix (e.g., "Google STT", "Parakeet")
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

    def stop(self, drain_timeout: float = 2.0):
        """Stop the forwarder thread.

        Before signalling the sender loop to exit, give already-queued
        outbound frames a bounded chance to deliver (wh-7ou.7.6.10): the
        apply_engine_settings success reply -- and the notifications
        queued just before a restart -- are enqueued moments before
        shutdown begins, and setting the stop event with frames still
        queued abandons them, stranding the calibration session in
        restart_slow with the typing gate held. The drain only runs when
        a connection is live (undeliverable frames would stall
        standalone shutdown for the full timeout) and never on the
        forwarder's own thread (sleeping there would block the sender
        loop that must do the delivering). Once the queue is empty, the
        thread join below lets an in-flight send finish -- the sender
        loop only re-checks the stop event between frames.
        """
        if (
            drain_timeout > 0
            and self._ws_connected
            and self._queue is not None
            and self._loop is not None
            and self._thread is not None
            and self._thread.is_alive()
            and threading.current_thread() is not self._thread
        ):
            deadline = time.monotonic() + drain_timeout
            # The send_* methods only SCHEDULE queue.put on the loop; a
            # frame queued moments before stop() may not be visible to
            # queue.empty() yet. A sentinel coroutine submitted after
            # them runs after them (the loop runs scheduled calls in
            # FIFO order, and an unbounded put completes in its first
            # step), so once it finishes every prior put has landed.
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0), self._loop
                ).result(timeout=max(0.1, drain_timeout / 2))
            except Exception:
                pass
            while time.monotonic() < deadline and not self._queue.empty():
                time.sleep(0.02)
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


def wheelhouse_can_receive_logs(forwarder) -> bool:
    """Whether a log record handed over now would reach WheelHouse.

    A forwarder that cannot answer counts as reachable. Saying the line is
    the safe direction only when the answer is a definite no: these
    handlers exist to make a provider visible off-console, and silence is
    the failure they were written to remove. An older forwarder with no
    accessor at all reads the same way.

    Module level, and used by two callers, because the answer must be the
    same in both places. WebSocketLogHandler asks it to decide whether to
    drop a record. AudioProcessor asks it before WRITING its one no-reader
    report, and a processor that disagreed with the handler it writes
    through would withhold a line the handler would have forwarded
    (wh-capture-load-gaps.2.2).

    This answer decides what is WRITTEN. It does not decide whether a
    one-shot report is spent: two reads of it can disagree, because the
    connection can close between them, so a caller that must not lose its
    report asks for a delivery receipt instead (see
    DELIVERY_RECEIPT_ATTR and wh-capture-load-gaps.2.3).

    Args:
        forwarder: the WSForwarder, or anything standing in for one.

    Returns:
        True when a record should be written, False only on a definite no.
    """
    try:
        return bool(getattr(forwarder, 'is_connected', True))
    except Exception:
        return True


#: The attribute a caller puts on a log record to learn whether a
#: WebSocketLogHandler failed to hand that record to the forwarder. Pass
#: it as ``extra={DELIVERY_RECEIPT_ATTR: receipt}``; the name is long
#: enough not to collide with a LogRecord attribute, which logging refuses
#: outright.
DELIVERY_RECEIPT_ATTR = 'wheelhouse_delivery_receipt'


def new_delivery_receipt() -> dict:
    """A receipt to hand a record, and to read after the log call returns.

    Why a receipt rather than a second look at the forwarder. A caller
    that must spend a one-shot report has to know what happened to THIS
    record, and no observation of the connection can tell it that: the
    forwarder's own thread writes _ws_connected on a connection that
    closes, so the caller's read and the handler's read can disagree
    inside one logging call, and the report is then spent on a record the
    handler dropped (wh-capture-load-gaps.2.3). The handler already knows
    which records it failed to hand over, so it reports that instead.

    It records the FAILURE, not the hand-over, and that direction is the
    whole design. A caller that waited for a positive hand-over would wait
    forever wherever no WebSocketLogHandler is attached at all -- a plain
    console setup, or the provider's own logging before start() -- and
    would repeat its one-shot line every window, which is the flood
    wh-capture-load-gaps.1.1 removed. Recording the failure spends the
    report unless some handler says it could not deliver, so the two cases
    that must differ do.

    Reading the receipt after the log call is sound because logging
    dispatches handlers synchronously on the calling thread: Logger.info
    calls handle, callHandlers and emit before it returns. A QueueHandler
    would move emit to another thread and break that; no STT provider
    installs one (grep -rn "QueueHandler" services/ --include=*.py matches
    only under services/wheelhouse/, the WheelHouse app process).

    Returns:
        The receipt. Read receipt['dropped'] after the log call.
    """
    return {'dropped': False}


def note_record_dropped(record) -> None:
    """Stamp the receipt on this record, if its writer left one.

    Called on every path where a WebSocketLogHandler ends without handing
    the record to the forwarder: an unreachable WheelHouse, a rendering or
    send failure, and a rate limit that suppressed it.

    Args:
        record: the LogRecord this handler did not hand over.
    """
    receipt = getattr(record, DELIVERY_RECEIPT_ATTR, None)
    if isinstance(receipt, dict):
        receipt['dropped'] = True


class WebSocketLogHandler(logging.Handler):
    """Python logging.Handler that forwards logs to WheelHouse via WebSocket.

    This handler can be added to any logger to forward log messages to WheelHouse
    for centralized logging. The logs appear in wheelhouse.log with a provider
    prefix (e.g., "[Google STT] message").

    A record written while WheelHouse is unreachable is DROPPED rather than
    queued, and counted per logger; the totals go out as one line on the
    first record forwarded after the connection returns, so an outage is
    bounded but never silent. RateLimitedWebSocketLogHandler introduced that
    policy for the capture path (wh-stt-load-metrics.2.1.7) and it lives here
    because the reason was never specific to that path: WSForwarder's
    outbound queue is unbounded, is created before any connection exists, and
    is cleared only after a connection that had already succeeded is lost. A
    provider that starts while WheelHouse is down would otherwise keep every
    record it wrote during the outage and send the whole backlog ahead of its
    first live transcript, because transcripts use the same queue
    (wh-forwarded-log-time-order criterion 4).

    While the connection is up, records go straight to that queue, which
    delivers them in order.

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

    #: How the disconnect report names itself and the records it counts. The
    #: capture subclass overrides both, so a reader of wheelhouse.log can
    #: tell a capture-path report from a provider-logger one.
    DROP_REPORT_TAG = 'log-forward'
    DROP_REPORT_SUBJECT = 'forwarded log records'

    def __init__(self, forwarder: WSForwarder, source: str, level: int = logging.DEBUG):
        """Initialize the WebSocket log handler.

        Args:
            forwarder: WSForwarder instance to send logs through
            source: Provider name for log prefix (e.g., "Google STT", "Parakeet")
            level: Minimum log level to forward (default: DEBUG forwards all)
        """
        super().__init__(level)
        self.forwarder = forwarder
        self.source = source
        # logger name -> records dropped because WheelHouse was unreachable.
        # Read and written only inside emit(), which
        # logging.Handler.handle() calls while holding this handler's lock,
        # so no separate lock is needed. CUMULATIVE and never cleared
        # (wh-stt-load-metrics.2.1.8); the reason is in
        # _forward_disconnected_drops.
        self._disconnected_drops = {}
        # The drop total the last report ATTEMPTED to carry. Attempted, not
        # delivered: send_log gives no delivery answer.
        self._reported_disconnected_total = 0

    def emit(self, record: logging.LogRecord):
        """Forward a log record to WheelHouse, unless it would build a backlog.

        Args:
            record: The log record to forward
        """
        if self._drop_while_unreachable(record):
            return
        self._report_disconnected_drops_if_due(record)
        self._send_record(record)

    def _drop_while_unreachable(self, record: logging.LogRecord) -> bool:
        """Count and refuse a record that could only sit in the queue.

        Returns:
            True when the record was dropped and the caller must stop.
        """
        if not self._wheelhouse_is_reachable():
            self._disconnected_drops[record.name] = (
                self._disconnected_drops.get(record.name, 0) + 1)
            note_record_dropped(record)
            self._on_disconnected_drop()
            return True
        return False

    def _on_disconnected_drop(self) -> None:
        """Bookkeeping a subclass needs the moment a record is dropped.

        Nothing to do here. RateLimitedWebSocketLogHandler overrides it.
        """

    def _report_disconnected_drops_if_due(self, record: logging.LogRecord):
        """Send the outage report when the totals have moved past the mark."""
        if sum(self._disconnected_drops.values()) != (
                self._reported_disconnected_total):
            self._forward_disconnected_drops(record)

    def _wheelhouse_is_reachable(self) -> bool:
        """Whether queueing a record now would reach WheelHouse.

        A forwarder that cannot answer counts as reachable. Dropping is the
        safe direction only when the answer is a definite no: these handlers
        exist to make a provider visible off-console, and silence is the
        failure they were written to remove. An older forwarder with no
        accessor at all reads the same way.
        """
        return wheelhouse_can_receive_logs(self.forwarder)

    def _forward_disconnected_drops(self, record: logging.LogRecord):
        """Report what the outages have cost so far, as one line.

        The counts are cumulative and are deliberately NOT cleared here
        (wh-stt-load-metrics.2.1.8). WSForwarder.send_log returns no
        acceptance and no delivery answer: it swallows every exception from
        run_coroutine_threadsafe and returns None. A report can therefore
        still be lost after it is queued, so clearing the counts on the way
        out would make that outage permanently silent while later outages
        went on counting, which is the failure this reporting exists to
        remove. A cumulative total means the next report carries every
        earlier outage as well.

        Exposing an acceptance result from send_log would not help: the loss
        happens AFTER the queue accepts the message, so an acceptance answer
        would always say yes on every path that reaches here.

        Since wh-forwarded-log-time-order defect 2 the main loss path is
        closed -- _clear_queue keeps log frames, and a log frame whose send
        failed waits in the forwarder's pending slot -- but process exit
        before DELIVERY still loses the report, so the counts stay
        cumulative. Two routes reach that exit, not one: stop() drains the
        queue only, and only while the connection is live, and it never
        waits on the pending slot at all (the crewcut: at _pending_log). A
        frame in that slot is therefore lost even when the queue is empty.

        Args:
            record: supplies the timestamp and the trace id only.
        """
        counts = dict(self._disconnected_drops)
        total = sum(counts.values())
        self._reported_disconnected_total = total
        detail = ', '.join(
            f"{name} {count}" for name, count in sorted(counts.items()))
        try:
            from datetime import datetime
            self.forwarder.send_log(
                level='WARNING',
                message=(
                    f"[{self.DROP_REPORT_TAG}] {total} "
                    f"{self.DROP_REPORT_SUBJECT} have been "
                    f"dropped while the wheelhouse connection was down, "
                    f"counted since this provider started ({detail})"),
                source=self.source,
                timestamp=datetime.fromtimestamp(
                    record.created).isoformat(),
                trace_id=getattr(self.forwarder, '_current_trace_id', '') or '',
            )
        except Exception:
            pass

    def _send_record(self, record: logging.LogRecord) -> bool:
        """Hand one record to the forwarder and say whether it went.

        emit() answers nothing, and it answers nothing on failure too: a
        record whose args cannot be rendered is caught below and reported
        through handleError, after which emit returns exactly as it does on
        success. A subclass that spends a bounded budget needs to tell those
        two apart, because a slot spent on a record that reached nobody is a
        capture warning wheelhouse.log never gets
        (wh-stt-load-metrics.2.1.14).

        Args:
            record: The log record to forward.

        Returns:
            True when the record was handed to the forwarder, False when
            rendering or the send raised and handleError was called.
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
            return True
        except Exception:
            # Don't let logging errors crash the application
            note_record_dropped(record)
            self.handleError(record)
            return False


class RateLimitedWebSocketLogHandler(WebSocketLogHandler):
    """Forward a noisy logger tree to WheelHouse without flooding it.

    The capture path (the shared_audio tree) writes to the provider console
    and nowhere else: the provider attaches its own WebSocketLogHandler to its
    own logger, so the overflow evidence a load investigation needs is
    invisible off-console (wh-stt-load-metrics.2). Forwarding that tree with
    the plain handler is not safe, because a microphone dropping frames writes
    several records a second and would flood both wheelhouse.log and the
    WebSocket queue.

    Two decisions worth knowing before changing this class.

    The limiting is here, in emit(), and NOT in a logging.Filter. One
    LogRecord object is shared by every handler the emit walk visits, so a
    filter that annotated or dropped at the record level would also change
    what the provider prints on its own console. This handler drops only its
    own output; the console keeps every record.

    The budget is PER LOGGER, keyed on record.name, not one budget for the
    whole tree. A storm on shared_audio.microphone would otherwise consume a
    shared budget and suppress OverflowMonitor's rate-limited summary, which
    is the one record the investigation actually needs. The key set is bounded
    in practice: these are module __name__ values under shared_audio, a fixed
    set of eight.

    A window that suppressed records forwards one extra line naming the count
    and the logger, so a storm is bounded but never silent.

    Records written while WheelHouse is unreachable are dropped rather than
    queued, and counted per logger. The window bounds the RATE; it does not
    bound the BACKLOG (wh-stt-load-metrics.2.1.7). The forwarder's outbound
    queue is unbounded, is created before any connection exists, and is
    cleared only after a connection that had already succeeded is lost -- so
    a run of initial connect failures keeps every record this handler queued
    during the outage, and sends the whole backlog ahead of the first live
    transcript, because transcripts use the same queue. The bound belongs on
    a handler rather than on that queue: a handler is what adds a producer,
    while the queue's clear policy is shared with transcripts and with every
    other provider. That bound now sits on WebSocketLogHandler, which every
    producer on the queue inherits (wh-forwarded-log-time-order criterion 4);
    this class only adds what a shared budget needs on top of it, which is
    the watermark reset in _on_disconnected_drop. The counts go out as one
    line on the first record forwarded after the connection returns, so an
    outage is bounded but never silent either.
    """

    #: This tree's reports name the capture path, so one wheelhouse.log can
    #: carry both these and a provider's own without the reader guessing.
    DROP_REPORT_TAG = 'capture-log'
    DROP_REPORT_SUBJECT = 'capture-path records'

    #: Records forwarded per logger per window before suppression starts.
    DEFAULT_MAX_RECORDS_PER_WINDOW = 5
    #: Length of that window. Matches OverflowConfig.window_seconds, and
    #: leaves room for OverflowMonitor's summary, which is emitted at most
    #: once per log_summary_interval_seconds (10s) and so cannot fill this.
    DEFAULT_WINDOW_SECONDS = 30.0

    def __init__(self, forwarder: WSForwarder, source: str,
                 max_records_per_window: int = DEFAULT_MAX_RECORDS_PER_WINDOW,
                 window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 level: int = logging.DEBUG,
                 clock: Callable[[], float] = time.monotonic):
        """Initialize the rate-limited WebSocket log handler.

        Args:
            forwarder: WSForwarder instance to send logs through.
            source: Provider name for the log prefix.
            max_records_per_window: Records forwarded per logger per window.
            window_seconds: Length of the window.
            level: Minimum log level to forward.
            clock: Seconds source. Monotonic on purpose: time.time() can jump
                backward on an NTP correction, which would suppress the
                capture path for an unbounded period while frames keep
                dropping -- the exact signal this handler exists to preserve.
                OverflowMonitor's own summary interval is monotonic for the
                same reason.
        """
        super().__init__(forwarder, source, level)
        self._max_records_per_window = max_records_per_window
        self._window_seconds = window_seconds
        self._clock = clock
        # logger name -> {'start', 'forwarded', 'suppressed'}. Read and
        # written only inside emit(), which logging.Handler.handle() calls
        # while holding this handler's lock, so no separate lock is needed.
        self._windows = {}
        # The per-logger disconnect counting lives on the base class, which
        # bounds the same backlog for every producer on the queue
        # (wh-forwarded-log-time-order criterion 4). Its key set is bounded
        # here by the same fixed set of module names under shared_audio
        # (wh-stt-load-metrics.2.1.7), and the same lock argument as
        # _windows covers it.
        # logger name -> records the rate limit did not forward, cumulative
        # for the same reason, and the per-logger total the last suppression
        # report attempted to carry.
        self._suppressed_totals = {}
        self._reported_suppressed = {}

    def emit(self, record: logging.LogRecord):
        """Forward the record unless it would build a backlog or fill a window.

        The disconnected test comes first and does NOT consume the window
        budget: an outage must not spend the budget the recovery needs.
        """
        if self._drop_while_unreachable(record):
            return

        self._report_disconnected_drops_if_due(record)

        now = self._clock()
        window = self._windows.get(record.name)
        if window is None or now - window['start'] >= self._window_seconds:
            suppressed = 0 if window is None else window['suppressed']
            window = {'start': now, 'forwarded': 0, 'suppressed': 0}
            self._windows[record.name] = window
            if suppressed:
                self._suppressed_totals[record.name] = (
                    self._suppressed_totals.get(record.name, 0) + suppressed)
            # Every logger with an outstanding total, not just this one. A
            # capture path whose report was lost and which then stops writing
            # never opens another window, so only a path that keeps working
            # can carry its count (wh-stt-load-metrics.2.1.9). Since
            # wh-forwarded-log-time-order defect 2 a disconnect no longer
            # discards a queued report; the loss that remains is process exit
            # before delivery -- before the queue drains, or with the frame
            # still in the pending slot, which stop() never waits on.
            for name in sorted(self._suppressed_totals):
                running_total = self._suppressed_totals[name]
                if running_total != self._reported_suppressed.get(name, 0):
                    self._forward_suppression_count(record, name,
                                                    running_total)
        if window['forwarded'] >= self._max_records_per_window:
            # crewcut: the count reaches the log only when the next record on
            # this logger opens a new window, so a storm that stops entirely
            # leaves its final count unreported. Removing that means a timer
            # or a flush from the consumer path; both add a thread or a call
            # site to the capture path, which this bead does not change.
            window['suppressed'] += 1
            note_record_dropped(record)
            return
        # Only a record the handler actually handed to the forwarder spends a
        # slot. A record whose args cannot be rendered reaches nobody, so
        # counting it would let a few broken call sites silence the capture
        # warnings this handler exists to deliver (wh-stt-load-metrics.2.1.14).
        if self._send_record(record):
            window['forwarded'] += 1

    def _on_disconnected_drop(self) -> None:
        """Take back every suppression watermark when the connection is down.

        A disconnect used to be when _clear_queue threw away whatever the
        queue still held, which could include a suppression report whose
        watermark was already written. Taking the watermarks back makes the
        next window report those totals again (wh-stt-load-metrics.2.1.9).
        Repeating a count already in the log costs one duplicate line;
        keeping the watermark costs the count outright, and the message says
        the total is counted since the provider started, so a repeat still
        reads true. The drop watermark needs no such reset: every observed
        outage drops a record, which pushes the drop total past it.

        crewcut: since wh-forwarded-log-time-order defect 2, _clear_queue
        KEEPS log frames, so a queued suppression report usually survives
        the disconnect and this reset then takes back a watermark for a
        report that was in fact delivered -- producing one duplicate count
        line. Accepted by the boss on 2026-08-31 as the cheap direction, the
        same trade-off wh-stt-load-metrics.2.1.9 already accepted in the
        other direction. Removing it needs a delivery answer from
        WSForwarder.send_log, which today swallows every exception from
        run_coroutine_threadsafe and returns None; that is a change to the
        shared sender path transcripts also use. No test asserts the
        duplicate line, so removing the limit later breaks nothing.
        """
        self._reported_suppressed.clear()

    def _forward_suppression_count(self, record: logging.LogRecord,
                                   logger_name: str, suppressed: int):
        """Report one logger's running not-forwarded total, as its own line.

        Cumulative for the same reason as _forward_disconnected_drops
        (wh-stt-load-metrics.2.1.8). This send goes through the same queue,
        which since wh-forwarded-log-time-order defect 2 keeps log frames
        across a disconnect instead of discarding them -- but process exit
        before delivery still loses the report, so cumulative it stays. That
        covers exit before the queue drains and exit with the frame in the
        pending slot, which stop() never waits on.

        The watermark written here records an ATTEMPT, never a delivery,
        because send_log answers neither. On its own that would make a lost
        report permanent: the running total would still equal the watermark,
        so the cumulative counter would go on matching and this logger would
        never report again (wh-stt-load-metrics.2.1.9). Two things prevent
        it. _on_disconnected_drop clears every suppression watermark as soon
        as the handler sees the connection down; and any logger opening a
        window reports every outstanding total, so a capture path that falls
        silent after its report was lost is still reported by one that keeps
        working.

        crewcut: a report lost during an outage that no capture record ever
        observes -- the connection drops and returns between two records --
        keeps its watermark, and that count then waits for the logger's next
        suppressed window. Removing that needs a delivery answer from
        WSForwarder.send_log, which is a change to the shared sender path
        transcripts also use.

        Args:
            record: supplies the timestamp and the trace id only.
            logger_name: whose count this is. Not always record's own
                logger, because any logger's rollover reports the others.
            suppressed: that logger's running total since the provider
                started.
        """
        self._reported_suppressed[logger_name] = suppressed
        try:
            from datetime import datetime
            self.forwarder.send_log(
                level='WARNING',
                message=(
                    f"[capture-log] {suppressed} {logger_name} records have "
                    f"not been forwarded (cap "
                    f"{self._max_records_per_window} per "
                    f"{self._window_seconds:.0f}s window), counted since "
                    f"this provider started; the provider console has all "
                    f"of them"
                ),
                source=self.source,
                timestamp=datetime.fromtimestamp(record.created).isoformat(),
                trace_id=self.forwarder._current_trace_id,
            )
        except Exception:
            # Same contract as emit(): a logging failure must not reach the
            # caller, which here is whatever thread wrote a capture record.
            self.handleError(record)
