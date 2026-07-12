"""Async IPC interface for WheelHouse UI command execution.

This module provides an async application interface that enables communication
between the main WheelHouse service and the UI input synthesis process via
shared memory and multiprocessing primitives. It handles command serialization,
response demultiplexing, and provides both fire-and-forget and request-response
communication patterns.

Key Classes:
  - WheelHouseApp: Main async interface for UI command execution.

Key Features:
  - Shared memory communication for low-latency IPC
  - Command queuing and response correlation
  - Timeout handling for request-response operations
  - Background task management for async operations

Typical Usage:
  from app import WheelHouseApp
  
  app = WheelHouseApp(
      shm_name="wheelhouse_shm",
      command_ready_event=ready_event,
      ui_ready_event=ui_event,
      response_queue=resp_queue
  )
  
  # Fire-and-forget command
  await app.send_fire_and_forget({"action": "click", "x": 100, "y": 200})
  
  # Request-response command
  result = await app.send_and_await_response({"action": "get_clipboard"})
"""
import asyncio
import logging
import pickle
import struct
import time
import uuid
from typing import Dict, Any, Optional
from multiprocessing import Event, Queue, shared_memory
from queue import Empty
from integrations.websocket_manager import WebSocketManager
from utils.trace_context import get_trace_id, elapsed_ms

logger = logging.getLogger(__name__)
pipeline_logger = logging.getLogger("wheelhouse.pipeline")

class WheelHouseApp:
    """
    Provides async helpers for sending fire-and-forget UI commands (ACK semantics)
    and request/response commands that resolve when the UI reports DONE.
    """
    def __init__(self, shm_name, command_ready_event, ui_ready_event, response_queue, shm_bytes=1024 * 64, response_timeout_s=5.0):
        self.shm_name = shm_name
        self.command_ready_event = command_ready_event
        self.ui_ready_event = ui_ready_event
        self.response_queue = response_queue
        self.shm_bytes = shm_bytes
        self.response_timeout_s = response_timeout_s

        self.shm = shared_memory.SharedMemory(name=self.shm_name)
        self.websocket_manager = None # To be initialized in start
        self.ws_port: int = 0

        # For demuxing responses to the correct awaiter
        self.response_futures = {}
        self.demuxer_task = None

        # Callback for unsolicited events from Input Process (no request_id)
        self._event_handler = None

        # Serialize outbound writes to SHM to prevent overwrite/loss
        self._outbound_q = asyncio.Queue()
        self._sender_task = None

    def register_event_handler(self, handler):
        """Register a callback for unsolicited events from the Input Process.

        Events are messages on the response_queue that have a ``type`` field
        but no ``request_id``.  The handler receives the full message dict.
        """
        self._event_handler = handler

    def get_screen_dimensions(self):
        """Get screen dimensions for WebSocket manager.
        
        Returns:
            Tuple[int, int]: Screen width and height in pixels (default 1920x1080)
        """
        # This is a placeholder. In a real scenario, you might use a library
        # like `screeninfo` or platform-specific APIs to get the actual screen size.
        # For now, we'll assume a common default or get it from a config.
        # Let's simulate getting it from a config or a fixed value.
        return 1920, 1080 # Example: Full HD resolution

    async def start(self, host, port, text_handler, start_websocket: bool = True):
        """:flow: Application Lifecycle
        :step: 3
        :consumes_from: Application Lifecycle
        :description: Starts IPC background tasks and optionally WebSocket manager
        :data_in: Host, port, text handler callback, and start_websocket flag
        :data_out: Running background tasks (demuxer, sender, optionally websocket)
        :notes: Initializes the WebSocketManager for speech server communication if start_websocket is True. 
                When start_websocket is False (in_process STT mode), WebSocket connection is skipped.
                Always starts the response demuxer and outbound sender for IPC.
        """
        """Starts the background tasks for the application."""
        # text_handler can be either a method or a speech_handler object
        # If it's an object with process_transcription, wrap it
        if hasattr(text_handler, 'process_transcription'):
            self.websocket_manager = WebSocketManager(asyncio.get_running_loop(), text_handler=text_handler.process_transcription)
            self.websocket_manager.speech_handler = text_handler  # Pass full object for utterance_end
            
            # NOTE: SpeechProcessor initialization moved to main.py after service initialization
            # to avoid chicken-and-egg problem with initialization order
        else:
            self.websocket_manager = WebSocketManager(asyncio.get_running_loop(), text_handler=text_handler)
        self._start_demuxer()
        self._start_sender()
        
        # Only connect to remote STT server if start_websocket is True
        if start_websocket:
            self.ws_port = await self.websocket_manager.start(host, port)
            logger.info(f"WebSocket server started on port {self.ws_port}")
        else:
            self.ws_port = 0
            logger.info("WebSocket to remote STT skipped (in_process mode)")

    async def stop(self):
        """Stops the background tasks."""
        tasks_to_cancel = []
        if self.demuxer_task:
            self.demuxer_task.cancel()
            tasks_to_cancel.append(self.demuxer_task)
        if self._sender_task:
            self._sender_task.cancel()
            tasks_to_cancel.append(self._sender_task)
        
        if self.websocket_manager:
            tasks_to_cancel.append(asyncio.create_task(self.websocket_manager.stop()))

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            
        logger.info("WheelHouseApp background tasks stopped.")


    def _start_demuxer(self) -> None:
        if self.demuxer_task is None or self.demuxer_task.done():
            self.demuxer_task = asyncio.create_task(self._demux_loop())
            logger.info("Response demuxer task started.")

    def _start_sender(self) -> None:
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender_loop())
            logger.info("Outbound sender task started.")

    async def _demux_loop(self) -> None:
        """:flow: UI Action Response
        :step: 3
        :description: Background task polls response_queue and demuxes by request_id
        :data_in: Response dicts from multiprocessing.Queue
        :data_out: Calls to Future.set_result() or Future.set_exception()
        :execution_context: main process (logic) - background task
        :execution_mode: background
        :notes: Long-running background task bridging blocking multiprocessing.Queue into asyncio.
        Polls response_queue.get_nowait() with 20ms sleep to avoid busy-wait. On response: (1) Extracts
        request_id from response dict (step 4), (2) Looks up Future in response_futures dict created
        by send_request (step 2b of UI Action Execution), (3) Validates Future not already done,
        (4) Resolves Future with response data or exception (step 5). If request_id not found in dict,
        logs warning (either timed out or fire-and-forget from step 2a which has no Future). Started
        during app initialization and runs until cancellation during shutdown.
        """
        logger.debug("Demuxer loop running.")
        while True:
            try:
                try:
                    """:flow: UI Action Response
                    :step: 4
                    :description: Extracts request_id and looks up awaiting Future
                    :data_in: Response dict with request_id
                    :data_out: Future reference from response_futures dict
                    :execution_context: main process (logic) - background task
                    :execution_mode: sync
                    :notes: Request ID matching for response demultiplexing. Extracts request_id from
                    response (UUID generated in send_request step 2b), uses as key to look up Future in
                    response_futures dict. If found, pops Future from dict (each request_id used once).
                    If not found, logs warning - indicates either: (1) request timed out and Future
                    already removed by send_request's finally block, or (2) fire-and-forget command from
                    send_command (step 2a) which never creates Future. This demuxing allows multiple
                    concurrent requests to be tracked independently.
                    """
                    response = self.response_queue.get_nowait()

                    # Unsolicited event from the input process. Messages
                    # with a 'type' field are events; their request_id,
                    # if present, is an internal correlation token for
                    # the editor ack protocol (wh-zhn). Dispatch by type
                    # before checking response_futures so a te_event:show
                    # carrying a request_id does not get dropped as an
                    # unknown response (regression from wh-t81d9.2).
                    if response.get("type") and self._event_handler:
                        try:
                            self._event_handler(response)
                        except Exception as e:
                            logger.error("Error in event handler: %s", e, exc_info=True)
                        continue

                    request_id = response.get('request_id')

                    if request_id and request_id in self.response_futures:
                        future = self.response_futures.pop(request_id)
                        """:flow: UI Action Response
                        :step: 5
                        :description: Resolves Future with response data or exception
                        :data_in: Response dict and Future reference
                        :data_out: Future.set_result() or Future.set_exception() call
                        :execution_context: main process (logic) - background task
                        :execution_mode: sync
                        :notes: Final step resolving request-response pattern. Checks Future not already
                        done (could be cancelled). If response has 'error' field, calls Future.set_exception
                        with RuntimeError wrapping message. Otherwise calls Future.set_result with full
                        response dict containing {request_id, status, path, action} for success paths or
                        {request_id, error, message, action} for error path. This immediately unblocks the
                        original send_request caller (step 2b) awaiting this Future, completing the
                        round-trip: command_engine → send_request → IPC → input_proc → response_queue →
                        demuxer → Future resolution → send_request return value.
                        """
                        if not future.done():
                            if response.get('error'):
                                future.set_exception(RuntimeError(response.get('message', 'UI process error')))
                            else:
                                pipeline_logger.info(
                                    "IPC_COMPLETE action=%s request_id=%s elapsed_ms=%.1f",
                                    response.get("action", ""), request_id, elapsed_ms(),
                                )
                                future.set_result(response)
                    else:
                        if request_id:
                            logger.warning(f"Demuxer got response for unknown/timed-out request_id: {request_id}")

                except Empty:
                    await asyncio.sleep(0.02) # Don't busy-wait
                except Exception as e:
                    logger.error(f"Error in demuxer loop: {e}", exc_info=True)
                    await asyncio.sleep(1) # Avoid spamming logs on persistent error

            except asyncio.CancelledError:
                logger.info("Demuxer task cancelled.")
                break

        # Clean up any pending futures on cancellation
        for future in self.response_futures.values():
            if not future.done():
                future.set_exception(asyncio.CancelledError("Application is shutting down."))
        logger.debug("Demuxer loop finished.")


    def _frame_and_write(self, payload: Dict[str, Any]) -> None:
        """:flow: UI Action Execution
        :step: 5
        :description: Pickles payload and writes to shared memory with size header
        :data_in: Dictionary payload for UI action
        :data_out: Framed binary data written to shm.buf
        :execution_context: main process (logic) - background task
        :execution_mode: sync
        :notes: Low-level IPC transport. Pickles payload dictionary into bytes, creates 4-byte
        big-endian size header with struct.pack('>I', size), writes [size_header][pickled_data] to
        shared memory buffer. Validates payload size doesn't exceed shm.size - 4 bytes (raises
        ValueError if too large). This framing protocol allows receiver (step 6) to know exactly how
        many bytes to read. Called by _send_one() (step 4) after event coordination.
        """
        try:
            data = pickle.dumps(payload)
            size = len(data)
            if size > self.shm.size - 4:
                raise ValueError(f"Payload size ({size}b) exceeds shared memory capacity ({self.shm.size - 4}b).")
            
            size_bytes = struct.pack('>I', size)
            self.shm.buf[:4] = size_bytes
            self.shm.buf[4:4 + size] = data
        except Exception as e:
            logger.error(f"Failed to write to shared memory: {e}", exc_info=True)
            raise

    async def _await_event_state(self, desired_set: bool, timeout_s: float = 1.0, poll_s: float = 0.003) -> bool:
        """Polls command_ready_event until it matches desired_set or timeout."""
        start = asyncio.get_running_loop().time()
        while True:
            try:
                is_set = self.command_ready_event.is_set()
            except Exception:
                return False
            if is_set == desired_set:
                return True
            if asyncio.get_running_loop().time() - start > timeout_s:
                return False
            await asyncio.sleep(poll_s)

    async def _send_one(self, payload: Dict[str, Any]) -> None:
        """:flow: UI Action Execution
        :step: 4
        :description: Coordinates shared memory write with event signaling
        :data_in: Dictionary payload to send
        :data_out: Shared memory written + command_ready_event signaled
        :execution_context: main process (logic) - background task
        :execution_mode: async
        :notes: Critical IPC coordination layer. (1) Waits for previous command_ready_event to
        be cleared by consumer (prevents clobbering if GUI process slow). (2) Calls _frame_and_write()
        to pickle and write payload (step 5). (3) Sets command_ready_event to signal GUI process
        that new data ready. (4) Observes consumer clearing event for best-effort confirmation of
        pickup. Uses 1s timeouts on waits - if timeout, logs warning and continues (doesn't block).
        This ensures sender makes forward progress even if GUI process stalls.
        """
        # Ensure previous signal cleared (avoids back-to-back clobber)
        await self._await_event_state(desired_set=False, timeout_s=1.0)
        self._frame_and_write(payload)
        try:
            self.command_ready_event.set()
        except Exception as e:
            logger.error(f"Failed to set command_ready_event: {e}", exc_info=True)
            return
        # Observe consumer clear to confirm pickup (best-effort)
        cleared = await self._await_event_state(desired_set=False, timeout_s=1.0)
        if not cleared:
            logger.debug("Sender did not observe consumer clearing event within timeout; continuing.")

    async def _sender_loop(self) -> None:
        """:flow: UI Action Execution
        :step: 3
        :description: Background task serializes IPC sends from outbound queue
        :data_in: Payload dictionaries from _outbound_q
        :data_out: Calls to _send_one() for each payload
        :execution_context: main process (logic) - background task
        :execution_mode: background
        :notes: Long-running background task continuously consuming from _outbound_q populated by
        both send_command (2a) and send_request (2b). Provides serialization to prevent IPC race
        conditions - ensures commands sent one at a time via _send_one(). Calls await asyncio.sleep(0)
        after each send to yield control and prevent CPU hogging. Runs until app shutdown when task
        is cancelled. Error handling ensures one failed send doesn't crash the sender loop.
        """
        logger.debug("Sender loop running.")
        try:
            while True:
                payload = await self._outbound_q.get()
                try:
                    await self._send_one(payload)
                except Exception as e:
                    logger.error(f"Error sending payload: {e}", exc_info=True)
                finally:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.info("Sender task cancelled.")
        finally:
            logger.debug("Sender loop finished.")

    async def send_command(self, payload: Dict[str, Any] | str, params: Optional[Dict[str, Any]] = None) -> None:
        """:flow: UI Action Execution
        :step: 2a
        :description: Fire-and-forget: enqueues command without response tracking
        :data_in: Dictionary payload with action and params
        :data_out: Payload in _outbound_q
        :execution_context: main process (logic)
        :execution_mode: async
        :notes: Branch 2a from step 1 conditional. Simply enqueues payload to _outbound_q and
        returns immediately. No Future created, no response tracking, no timeout. Used for
        actions that don't need completion confirmation (e.g., type_text, press). Background
        sender task (step 3) will consume from this queue.
        """
        if isinstance(payload, str):
            payload = {"action": payload, "params": params or {}}

        payload["trace_id"] = get_trace_id()

        pipeline_logger.info(
            "IPC_SENT action=%s elapsed_ms=%.1f",
            payload.get("action", ""), elapsed_ms(),
        )

        try:
            # Enqueue for serialized send by background sender
            await self._outbound_q.put(payload)
        except Exception as e:
            logger.error(f"Failed to enqueue command: {e}", exc_info=True)
            
    async def send_request(self, action: str, params: Optional[Dict[str, Any]] = None, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """:flow: UI Action Execution
        :step: 2b
        :description: Request-response: enqueues command and creates Future for response
        :data_in: action string, params dict, optional timeout
        :data_out: Payload in _outbound_q + Future in response_futures dict
        :response_flow: UI Action Response
        :execution_context: main process (logic)
        :execution_mode: async
        :notes: Branch 2b from step 1 conditional. Generates unique request_id via uuid4, creates
        asyncio Future for response tracking, stores in self.response_futures[request_id]. Enqueues
        payload with request_id to _outbound_q. Calling code awaits the Future, which will be
        resolved by _response_demuxer background task when GUI process sends completion via
        response_queue (see UI Action Response flow). Times out after effective_timeout (default
        5s). On timeout or error, Future removed to prevent memory leaks.
        """
        request_id = str(uuid.uuid4())
        payload = {"action": action, "params": params or {}, "request_id": request_id, "trace_id": get_trace_id()}

        pipeline_logger.info(
            "IPC_SENT action=%s elapsed_ms=%.1f", action, elapsed_ms(),
        )

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.response_futures[request_id] = future
        effective_timeout = timeout_s if timeout_s is not None else self.response_timeout_s

        try:
            # Enqueue for serialized send; demuxer resolves the future
            await self._outbound_q.put(payload)
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            logger.error(f"Request '{action}' (id: {request_id}) timed out after {effective_timeout}s.")
            raise
        except Exception as e:
            logger.error(f"send_request failed for action '{action}': {e}", exc_info=True)
            raise
        finally:
            # Ensure future is removed to prevent memory leaks
            self.response_futures.pop(request_id, None)

    async def shutdown(self) -> None:
        """:flow: Application Lifecycle
        :step: 8
        :consumes_from: Application Lifecycle
        :description: Stops IPC background tasks
        :data_in: None
        :data_out: Cancelled background tasks
        :notes: Final step of the shutdown sequence. Cancels the demuxer and sender tasks to stop IPC processing. Ensures clean termination of the communication layer after all services have stopped.
        """
        """Cancels the demuxer task and ensures it stops."""
        if self.demuxer_task and not self.demuxer_task.done():
            self.demuxer_task.cancel()
            try:
                await self.demuxer_task
            except asyncio.CancelledError:
                pass
        if self._sender_task and not self._sender_task.done():
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("WheelHouseApp shutdown complete.")