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
import math
import pickle
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Any, Optional
from multiprocessing import Event, Queue, shared_memory
from queue import Empty
from integrations.websocket_manager import WebSocketManager
from utils.trace_context import (
    current_trace_id,
    get_trace_id,
    trace_start_time,
)

logger = logging.getLogger(__name__)
pipeline_logger = logging.getLogger("wheelhouse.pipeline")

_DEFAULT_SHARED_MEMORY_BYTES = 64 * 1024

# wh-overlay-slow-uia-stale-badges.5 delivery protection.
# Queue bound: at the consumer's normal ~10 ms cadence, 256 entries is ~2.5 s
# of backlog; the queue only fills when the Input process is wedged for longer
# than the delivery TTL, at which point the payloads are stale anyway.
# Overload policy: fire-and-forget payloads are dropped with an ERROR log
# (the callers hold no handle a failure could be returned through);
# request/response payloads raise IpcDeliveryError to the caller immediately.
_OUTBOUND_QUEUE_MAX = 256
# How long a fire-and-forget payload may wait for delivery before it is
# dropped instead of being typed/clicked into a window that has since changed.
_COMMAND_DELIVERY_TTL_S = 5.0
# Never-stale fire-and-forget actions (wh-overlay-slow-uia-stale-badges.14.1):
# dictated text, utterance lifecycle (end_utterance restores the user's
# clipboard), editor acks, and state updates must reach the Input process
# even when a slow UIA walk wedges the consumer past the short TTL. They get
# a long delivery budget: long enough to outlive any observed wedge (8+ s),
# short enough that a dead Input process still lets the queue drain by expiry
# instead of blocking the sender forever. Pointer, click, and keystroke
# actions stay on the short TTL because acting on a window that has since
# changed is worse than dropping. Every name here must be an action string a
# producer actually sends AND the Input process dispatches (a UIActionHandler
# method or an input_proc inline case) -- a name that fails either half
# silently keeps the short TTL (wh-overlay-slow-uia-stale-badges.14.6).
_DURABLE_COMMAND_ACTIONS = frozenset({
    "intelligent_insert_text",
    "start_utterance",
    "end_utterance",
    "_te_event_ack",
    "add_soft_allow_tuple",
    "set_log_level",
    "terminal_editor_cancelled",
    # skip_clipboard_restore is deliberately NOT here
    # (wh-overlay-slow-uia-stale-badges.14.15): the flag it sets in the Input
    # process is unscoped, so a late delivery makes a later unrelated
    # utterance skip restoring the user's clipboard. Stale-sensitive is the
    # safe class -- losing one breaks the current copy visibly instead.
})
_DURABLE_COMMAND_TTL_S = 120.0

# wh-watchdog-stall-window: the actions whose awaited window is stamped on
# the envelope for the Input process's dispatch watchdog to read.
#
# These are the handlers that hold the Input process's single command loop
# longest, and each is awaited for a window an operator can raise:
# click_element and click_snapshot_item for ``[click] response_timeout_ms``
# (main.py's by-name click path and its numbered-badge path),
# start_overlay_walk for ``[click] screen_read_timeout_ms`` (main.py's
# overlay build path, which sends this action for every build reason except
# AUTO_OPEN -- the post-click settle re-read included). The watchdog's fixed
# limit is shorter than either key's validated ceiling, so without the stamp
# it reports a stall for work still inside the window Logic is waiting out.
#
# click_snapshot_item joined the set on review finding .1.1. The first pass
# took its members from the pair input_proc.py treats as its UIA-walk
# handlers, and the badge click is not one of them -- but it reaches the same
# pre-click verification block through the same shared ClickExecutor, so its
# hold is bounded by ``[click] verification_budget_ms``, which validates to
# 10000 like the window it is awaited for. Missing it left the false stall
# report in place on the numbered-overlay path, which is the report this
# whole change exists to remove.
#
# perform_drag is deliberately absent, and its absence is the one that took
# an argument. It is awaited for ``response_timeout_ms`` PLUS its own
# ``drag_duration_ms`` (main.py's grid path, up to 15 s), and the handler
# blocks for the whole interpolated movement. But a drag is neither a click
# nor a walk, so stamping it changes the mouse-grid path this bead never
# examined, and at the shipped 250 ms duration the stamp would LOWER its
# limit from 6.0 s to 4.25 s. Its false report needs a 5 s drag plus over
# 1 s of pointer-send overhead, which nobody has measured. Recorded here
# rather than changed.
#
# Every other action is deliberately absent. An action whose awaited window
# is SHORTER than the watchdog's fixed limit would have its stall limit
# lowered by a stamp, which is a change this bead did not ask for; the fixed
# limit keeps answering for those. Only an exact str action is looked up,
# the rule the durable-action lookup above documents (.14.35, .14.36).
_AWAITED_WINDOW_ACTIONS = frozenset({
    "click_element",
    "click_snapshot_item",
    "start_overlay_walk",
})


def _safe_type_name(value: Any) -> str:
    """Name a value's class without letting the lookup raise.

    wh-overlay-slow-uia-stale-badges.14.61: the sender names a caller
    object by its type in three places that all promise a specific
    outcome -- send_command's two controlled drops promise a False
    return, and send_request's timeout gate promises ValueError. A class
    whose metaclass defines __name__ as a raising property replaced each
    of those outcomes with its own exception. The Input process has its
    own copy of this helper; app.py does not import input_proc, because
    the two run in separate processes.
    """
    try:
        return type(value).__name__
    except Exception:
        return "<unnamed type>"


def _canonical_action_label(action: Any) -> str:
    """Name an action safely for a log line or an exception message.

    wh-overlay-slow-uia-stale-badges.14.58: the sender rendered the
    caller's own action object into the queue-full IpcDeliveryError and
    the timeout log. An f-string calls format(), so a str subclass with
    a raising __format__ replaced both documented outcomes with its own
    exception. An exact str is kept; anything else is named by its type,
    and a type whose name cannot be read still answers.
    """
    if type(action) is str:
        return action
    return f"<{_safe_type_name(action)}>"


class IpcDeliveryError(Exception):
    """The outbound IPC queue refused a payload; it was never sent."""


class IpcSerializationError(IpcDeliveryError, ValueError):
    """The payload failed the pre-enqueue size check; it was never sent.

    wh-overlay-slow-uia-stale-badges.7.1.10: both send helpers serialize
    and size-check the payload BEFORE any enqueue, so an oversize
    rejection is a proven never-sent failure -- exactly what the
    sentence-restore sites treat IpcDeliveryError as. It also subclasses
    ValueError so the documented oversize contract (callers and tests
    that catch ValueError on "exceeds shared memory capacity") survives
    unchanged.
    """


class IpcEventError(Exception):
    """command_ready_event could not be read; the Input process is likely dead."""


# Late-response registry bounds (wh-overlay-slow-uia-stale-badges.6). When a
# send_request caller supplies on_late_response, the request_id is registered
# at future-creation time (wh-overlay-slow-uia-stale-badges.20.1) so the
# demuxer can hand the caller an answer that arrives AFTER the wait timed out
# -- including one landing in the gap before the finally-pop -- instead of
# discarding it. Deliberately module constants, not config: the grace runs
# from the TIMEOUT instant with the full 15 s
# (wh-overlay-slow-uia-stale-badges.20.6) -- each entry's expiry is the send
# instant + the request's effective timeout + this grace -- which covers the
# observed several-seconds-late refusal (7810 ms verification against a
# 3000 ms awaiter) with margin even under a long configured timeout, and the
# capacity bounds memory for a caller that times out repeatedly.
_LATE_RESPONSE_GRACE_S = 15.0
_LATE_RESPONSE_CAPACITY = 32


@dataclass
class _LateResponseEntry:
    """One registered late-response callback (wh-overlay-slow-uia-stale-badges.6).

    ``armed`` (wh-overlay-slow-uia-stale-badges.20.7) is False at
    registration and set True only by send_request's TimeoutError branch:
    it records that the awaiter actually exited through a timeout, the only
    terminal outcome that leaves a late answer for the callback to catch.
    """

    callback: Callable[[Dict[str, Any]], None]
    expiry: float
    armed: bool = False


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
        # request_id -> (trace_id, perf_counter anchor) for the demux
        # task's IPC_COMPLETE record (wh-overlay-slow-uia-stale-badges
        # .14.23): the demux task runs in its own context, so the
        # ContextVar-based trace id and elapsed_ms() are empty there.
        self._request_trace_meta = {}
        self.demuxer_task = None

        # Late-response callbacks (wh-overlay-slow-uia-stale-badges.6):
        # request_id -> _LateResponseEntry(callback, monotonic expiry,
        # armed). Registered at future-creation time, so entries also cover
        # IN-FLIGHT requests until send_request pops them on completion, on
        # a non-timeout failure, or on cancellation
        # (wh-overlay-slow-uia-stale-badges.20.1, .20.7). An entry's expiry
        # is the send instant + the effective timeout + the grace, so the
        # grace runs from the timeout instant (.20.6). ARMED is set only by
        # send_request's TimeoutError branch: the demuxer's unknown-id
        # consult fires ONLY armed entries (an unarmed hit is a duplicate
        # answer after an on-time one -- warn and leave the entry for the
        # caller's own pop), while its done-future consult fires regardless
        # of arming, because that branch IS the timeout-to-finally race
        # window where the entry cannot be armed yet. Accepted residual
        # (.20.7): a response arriving between a CALLER cancellation and
        # the caller's resume can still fire via the done-future branch;
        # the Logic-side staleness gates and the callback exception guard
        # bound the harm. Insertion-ordered dict; bounded by
        # _LATE_RESPONSE_CAPACITY (evict oldest -- capacity 32 stays ample:
        # only the two click awaiters register, and voice clicks are
        # serialized) and pruned of expired entries whenever it is touched.
        self._late_response_callbacks: Dict[str, _LateResponseEntry] = {}

        # Callback for unsolicited events from Input Process (no request_id)
        self._event_handler = None

        # Serialize outbound writes to SHM to prevent overwrite/loss.
        # Bounded (wh-overlay-slow-uia-stale-badges.5): see _OUTBOUND_QUEUE_MAX.
        self._outbound_q = asyncio.Queue(maxsize=_OUTBOUND_QUEUE_MAX)
        self._sender_task = None

        # wh-spaced-punctuation-names-unresolved.3.1.5. Counts the
        # start_utterance commands this app has enqueued. Two producers
        # send that command -- WebSocketManager
        # (integrations/websocket_manager.py:1295 and :1541) and the
        # SpeechProcessor's own lifecycle-reset branch
        # (speech/speech_processor.py:1451) -- and the Input side
        # resets its paste counter on every one of them
        # (ui/ui_action_handler.py:1126). The processor cannot see the
        # command, so this counter is how it learns whether its own
        # text went out before or after a given start_utterance: the
        # number is bumped immediately before the enqueue below, and
        # read immediately before the processor's own enqueue, and
        # neither send awaits anything in between. WebSocketManager
        # copies the new value onto the first WordEvent of the
        # utterance, so the processor can compare the two.
        self.utterance_start_generation = 0

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

    async def start(self, host, port, text_handler):
        """:flow: Application Lifecycle
        :step: 3
        :consumes_from: Application Lifecycle
        :description: Starts IPC background tasks and the WebSocket manager
        :data_in: Host, port, text handler callback
        :data_out: Running background tasks (demuxer, sender, websocket)
        :notes: Initializes the WebSocketManager for speech server communication.
                A start_websocket flag used to make this optional, for the
                in-process STT mode that wh-in-process-capture-removal deleted;
                the words always arrive over the WebSocket now.
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
        
        self.ws_port = await self.websocket_manager.start(host, port)
        logger.info(f"WebSocket server started on port {self.ws_port}")

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
                        trace_meta = self._request_trace_meta.pop(request_id, None)
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
                                # IPC_COMPLETE carries the requester's trace
                                # (wh-overlay-slow-uia-stale-badges.14.23):
                                # this task's own context has no trace id and
                                # a zero elapsed_ms(), so the record uses the
                                # (trace_id, anchor) captured by send_request
                                # -- the same arithmetic as IPC_SENT (.14.11).
                                trace, anchor = trace_meta or ("", 0.0)
                                complete_elapsed_ms = (
                                    (time.perf_counter() - anchor) * 1000.0
                                    if isinstance(anchor, float) and anchor > 0.0
                                    else 0.0
                                )
                                token = current_trace_id.set(trace)
                                try:
                                    pipeline_logger.info(
                                        "IPC_COMPLETE action=%s request_id=%s elapsed_ms=%.1f",
                                        response.get("action", ""), request_id,
                                        complete_elapsed_ms,
                                    )
                                finally:
                                    current_trace_id.reset(token)
                                future.set_result(response)
                        else:
                            # The future is DONE but was still registered:
                            # the awaiter's wait_for timed out (cancelling
                            # the future) and its finally has not popped it
                            # yet (wh-overlay-slow-uia-stale-badges.20.1).
                            # Without this fallthrough the answer landing in
                            # that gap would be consumed here and lost --
                            # the registry entry would expire unanswered.
                            # Consult the registry like the unknown-id
                            # branch below; a miss logs a warning (that
                            # drop used to be completely silent). This
                            # consult fires even an UNARMED entry
                            # (wh-overlay-slow-uia-stale-badges.20.7): in
                            # this race window the wait has timed out but
                            # the TimeoutError branch has not armed the
                            # entry yet.
                            if not self._dispatch_late_response(
                                request_id, response, fire_unarmed=True,
                            ):
                                logger.warning(
                                    "Demuxer got a response for request_id "
                                    "%s whose future is already done (the "
                                    "awaiter gave up); no late-response "
                                    "registration -- response dropped",
                                    request_id,
                                )
                    else:
                        if request_id:
                            # A timed-out request may have registered a
                            # late-response callback
                            # (wh-overlay-slow-uia-stale-badges.6): consult
                            # the registry BEFORE the unknown-id warning so
                            # the true answer reaches the caller instead of
                            # being discarded. A miss (never registered,
                            # expired, or evicted) keeps today's warning.
                            # Only an ARMED entry may fire here
                            # (wh-overlay-slow-uia-stale-badges.20.7): an
                            # unarmed hit is a duplicate answer after an
                            # on-time one.
                            if not self._dispatch_late_response(
                                request_id, response, fire_unarmed=False,
                            ):
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
        self._request_trace_meta.clear()
        # Shutdown clears the WHOLE late-response registry
        # (wh-overlay-slow-uia-stale-badges.20.7): with the demuxer gone no
        # response can be delivered any more, and no late callback may fire
        # after shutdown -- including for entries whose awaiters the loop
        # above just cancelled.
        self._late_response_callbacks.clear()
        logger.debug("Demuxer loop finished.")

    # -- late-response registry (wh-overlay-slow-uia-stale-badges.6) ---------

    def _dispatch_late_response(
        self, request_id: str, response: Dict[str, Any], *,
        fire_unarmed: bool,
    ) -> bool:
        """Consult the registry; on a hit, handle the response and return True.

        The shared consult for the demuxer's two lossy cells
        (wh-overlay-slow-uia-stale-badges.20.1): an unknown request_id, and a
        known request_id whose future is already done (the awaiter gave up).
        A miss returns False; each caller logs its own warning.

        ``fire_unarmed`` (wh-overlay-slow-uia-stale-badges.20.7): the
        done-future caller passes True -- that cell IS the timeout-to-finally
        race window, where the wait already timed out but the TimeoutError
        branch has not armed the entry yet, so arming cannot be required
        there. The unknown-id caller passes False: an UNARMED hit there
        means the caller was answered on time and this is a duplicate
        answer -- log the duplicate warning, leave the entry in place for
        the caller's own pop, fire nothing, and return True (handled).
        """
        self._prune_late_responses()
        entry = self._late_response_callbacks.get(request_id)
        if entry is None:
            return False
        if not entry.armed and not fire_unarmed:
            logger.warning(
                "Demuxer got a duplicate response for request_id %s, which "
                "was already answered on time (registration not armed); "
                "duplicate dropped", request_id,
            )
            return True
        del self._late_response_callbacks[request_id]
        logger.info(
            "Demuxer got a late response for timed-out "
            "request_id %s within the grace window; "
            "scheduling its callback", request_id,
        )
        asyncio.get_running_loop().call_soon(
            self._run_late_response_callback,
            entry.callback, response, request_id,
        )
        return True

    def _register_late_response(
        self, request_id: str,
        callback: Callable[[Dict[str, Any]], None],
        lifetime_s: float,
    ) -> None:
        """Register a callback for an answer the awaiter may not live to see.

        Called from send_request at FUTURE-CREATION time when the caller
        supplied ``on_late_response`` (wh-overlay-slow-uia-stale-badges.20.1),
        so the entry also covers the in-flight window; send_request pops it
        again on normal completion, on a non-timeout failure, and on
        cancellation (.20.7). The entry lives for ``lifetime_s`` seconds
        from registration -- send_request passes its effective timeout plus
        ``_LATE_RESPONSE_GRACE_S``, so the grace runs from the TIMEOUT
        instant (.20.6). The entry is created UNARMED; only send_request's
        TimeoutError branch arms it (.20.7). The registry holds at most
        ``_LATE_RESPONSE_CAPACITY`` entries, evicting the OLDEST on
        overflow. Expired entries are pruned on every touch of the registry.
        """
        now = time.monotonic()
        self._prune_late_responses(now)
        while len(self._late_response_callbacks) >= _LATE_RESPONSE_CAPACITY:
            evicted_id = next(iter(self._late_response_callbacks))
            del self._late_response_callbacks[evicted_id]
            logger.warning(
                "Late-response registry full; evicted the oldest entry "
                "(request_id %s)", evicted_id,
            )
        self._late_response_callbacks[request_id] = _LateResponseEntry(
            callback=callback, expiry=now + lifetime_s,
        )

    def _pop_late_response(
        self, request_id: str
    ) -> Optional[_LateResponseEntry]:
        """Remove and return the live entry for ``request_id``, or None.

        Prunes expired entries first (every touch prunes), so an answer
        arriving after the grace window reads as a miss and gets the
        unknown-id warning.
        """
        self._prune_late_responses()
        return self._late_response_callbacks.pop(request_id, None)

    def _prune_late_responses(self, now: Optional[float] = None) -> None:
        """Drop every registry entry whose grace window has passed."""
        if not self._late_response_callbacks:
            return
        if now is None:
            now = time.monotonic()
        expired = [
            rid
            for rid, entry in self._late_response_callbacks.items()
            if entry.expiry <= now
        ]
        for rid in expired:
            del self._late_response_callbacks[rid]
            logger.info(
                "Late-response entry for request_id %s expired unanswered; "
                "dropped", rid,
            )

    def _run_late_response_callback(
        self,
        callback: Callable[[Dict[str, Any]], None],
        response: Dict[str, Any],
        request_id: str,
    ) -> None:
        """Invoke a late-response callback; a raise never breaks the demuxer."""
        try:
            callback(response)
        except Exception:  # noqa: BLE001 -- caller code must not kill the loop
            logger.error(
                "Late-response callback failed for request_id %s",
                request_id, exc_info=True,
            )


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
        # Any failure propagates WITHOUT a log record here
        # (wh-overlay-slow-uia-stale-badges.14.39): the sole production
        # caller logs the single correlated ERROR (action, trace_id,
        # request_id) and the IPC_DROPPED record. A second helper-level
        # ERROR made the notifier show two Windows popups per failure.
        #
        # Prefer the bytes frozen at acceptance
        # (wh-overlay-slow-uia-stale-badges.14.32): re-serializing here
        # would execute reducers over caller-reachable state, so a
        # mutation after acceptance could change the delivered bytes.
        # Both enqueue helpers attach _frame; entries without it exist
        # only in direct unit tests of the sender's drop paths.
        data = payload.get("_frame")
        if not isinstance(data, bytes):
            data = self._serialize_final_payload(payload)
        size = len(data)

        size_bytes = struct.pack('>I', size)
        self.shm.buf[:4] = size_bytes
        self.shm.buf[4:4 + size] = data

    def _serialize_final_payload(self, payload: Dict[str, Any]) -> bytes:
        """Serialize and size-check an IPC payload after all envelope fields exist."""
        data = pickle.dumps(payload)
        size = len(data)
        shm_size = getattr(self.shm, "size", None)
        # Production SharedMemory exposes an integer size. The configured
        # size is an equivalent safe fallback for minimal IPC test doubles.
        if not isinstance(shm_size, int) or isinstance(shm_size, bool):
            shm_size = getattr(self, "shm_bytes", _DEFAULT_SHARED_MEMORY_BYTES)
        if size > shm_size - 4:
            raise IpcSerializationError(
                "final UI payload size "
                f"({size}b) exceeds shared memory capacity ({shm_size - 4}b)."
            )
        return data

    async def _await_event_state(self, desired_set: bool, timeout_s: float = 1.0, poll_s: float = 0.003) -> bool:
        """Polls command_ready_event until it matches desired_set or timeout.

        Raises IpcEventError when the event object itself cannot be read
        (wh-overlay-slow-uia-stale-badges.14.2): a broken event is a distinct
        failure from a timeout, and collapsing the two made _send_one blame a
        deadline for drops the deadline did not cause.
        """
        start = asyncio.get_running_loop().time()
        while True:
            try:
                is_set = self.command_ready_event.is_set()
            except Exception as e:
                raise IpcEventError(f"command_ready_event is unreadable: {e}") from e
            if is_set == desired_set:
                return True
            if asyncio.get_running_loop().time() - start > timeout_s:
                return False
            await asyncio.sleep(poll_s)

    def _log_ipc_dropped(self, payload: Dict[str, Any], reason: str) -> None:
        """Record a payload the IPC layer dropped, so pipeline metrics count drops."""
        pipeline_logger.info(
            "IPC_DROPPED reason=%s action=%s trace_id=%s request_id=%s",
            reason, payload.get("action", ""),
            payload.get("trace_id") or "-", payload.get("request_id") or "-",
        )

    async def _send_one(self, payload: Dict[str, Any]) -> None:
        """:flow: UI Action Execution
        :step: 4
        :description: Coordinates shared memory write with event signaling
        :data_in: Dictionary payload to send
        :data_out: Shared memory written + command_ready_event signaled, or the payload dropped
        :execution_context: main process (logic) - background task
        :execution_mode: async
        :notes: Critical IPC coordination layer (wh-overlay-slow-uia-stale-badges.5).
        (1) Waits for the consumer to clear command_ready_event, bounded by the payload's
        delivery deadline (_delivery_deadline_monotonic, stamped at enqueue; a payload
        without one gets _COMMAND_DELIVERY_TTL_S from now). A payload whose deadline
        passes before the frame is free is DROPPED with an ERROR log -- the frame is
        never overwritten while it holds an unread command, because that destroys the
        unread command with no trace. (2) Calls _frame_and_write() to pickle and write
        the payload (step 5). (3) Sets command_ready_event to signal the GUI process.
        (4) Observes the consumer clearing the event for best-effort pickup confirmation.
        The sender still makes forward progress when the GUI process stalls: each
        stalled payload expires at its own deadline and the loop moves on.
        """
        # Delivery-time records must carry the payload's trace_id as the
        # structured LogRecord attribute (wh-overlay-slow-uia-stale-badges.14.13):
        # this sender task inherited an empty ContextVar context at start(),
        # and TraceIdFilter reads current_trace_id unconditionally, so without
        # this every record below formats trace= empty and escapes structured
        # per-utterance filtering. Set only the id -- set_trace would also
        # reset trace_start_time, and the elapsed value is deliberately
        # computed from the payload's own anchor instead.
        token = current_trace_id.set(payload.get("trace_id") or "")
        try:
            await self._send_one_in_trace_context(payload)
        finally:
            current_trace_id.reset(token)

    async def _send_one_in_trace_context(self, payload: Dict[str, Any]) -> None:
        """The body of _send_one; runs with current_trace_id set to the payload's id."""
        if payload.get("_delivery_abandoned"):
            # The caller cancelled while the payload sat queued
            # (wh-overlay-slow-uia-stale-badges.14.7). INFO, not ERROR:
            # abandonment is the caller's deliberate choice, and every ERROR
            # record raises a Windows notification popup.
            logger.info(
                "Skipping cancelled IPC payload before send: action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "cancelled")
            return
        deadline = payload.get("_delivery_deadline_monotonic")
        if deadline is None:
            deadline = time.monotonic() + _COMMAND_DELIVERY_TTL_S
        elif (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            # Validate before any arithmetic (wh-overlay-slow-uia-stale-badges.14.12):
            # a string here used to raise out to the sender loop's generic
            # catch, and NaN/inf passed every comparison below, wedging the
            # single sender forever while the event stayed set.
            logger.error(
                "Dropped IPC payload: invalid delivery deadline %r. "
                "action=%s trace_id=%s request_id=%s",
                deadline, payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "invalid_deadline")
            return
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            logger.error(
                "Dropped expired IPC payload before send (deadline passed while queued): "
                "action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "expired")
            return
        # Never overwrite an unread command: wait for the consumer to clear the
        # previous signal, up to this payload's own deadline.
        try:
            cleared = await self._await_event_state(desired_set=False, timeout_s=remaining_s)
        except IpcEventError as e:
            logger.error(
                "Dropped IPC payload: command_ready_event is unreadable (%s); "
                "the Input process is likely dead. action=%s trace_id=%s request_id=%s",
                e, payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "event_error")
            return
        if not cleared:
            logger.error(
                "Dropped IPC payload: Input process did not read the previous command "
                "before this payload's deadline; refusing to overwrite it. "
                "action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "unread_frame")
            return
        # Re-check the deadline after the wait (wh-overlay-slow-uia-stale-badges.14.3):
        # _await_event_state accepts a state match before its elapsed check, and
        # event-loop scheduling can delay the observing poll, so the frame can
        # come free only after this payload's deadline. Never deliver late.
        if time.monotonic() >= deadline:
            logger.error(
                "Dropped expired IPC payload after wait (frame came free past the "
                "deadline): action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "expired")
            return
        if payload.get("_delivery_abandoned"):
            # The cancellation can land while this coroutine waits on the
            # frame (wh-overlay-slow-uia-stale-badges.14.7): the overlay
            # abort path cancels its build request exactly then. Re-check
            # before the write so the abandoned build is never delivered.
            logger.info(
                "Skipping cancelled IPC payload after wait: action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "cancelled")
            return
        try:
            self._frame_and_write(payload)
        except Exception as e:
            # A write failure is a terminal outcome for this payload
            # (wh-overlay-slow-uia-stale-badges.14.8): without a classified
            # drop here it escaped to the sender loop's generic catch, so the
            # payload stayed counted as IPC_SENT with no attributable record.
            logger.error(
                "Dropped IPC payload: shared-memory write failed (%s). "
                "action=%s trace_id=%s request_id=%s",
                e, payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
                exc_info=True,
            )
            self._log_ipc_dropped(payload, "write_error")
            return
        # The deadline can pass during framing (wh-overlay-slow-uia-stale-badges.14.16):
        # serialization and the shared-memory copy run between the post-wait
        # check and the signal, and a scheduling stall there delivered a stale
        # payload and counted it IPC_SENT. The frame is written but the event
        # is still clear, so leaving it unannounced is safe -- the next
        # payload's pre-write wait sees a free frame and overwrites it.
        # (_delivery_abandoned cannot change here: there is no await point
        # between the post-wait abandonment check and the signal.)
        if time.monotonic() >= deadline:
            logger.error(
                "Dropped expired IPC payload after framing (deadline passed "
                "before signal): action=%s trace_id=%s request_id=%s",
                payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
            )
            self._log_ipc_dropped(payload, "expired")
            return
        try:
            self.command_ready_event.set()
        except Exception as e:
            # The frame was written but the Input process was never told
            # (wh-overlay-slow-uia-stale-badges.14.5): the payload is
            # undelivered, and a later send may overwrite the unannounced
            # frame. Record it as a drop so it is not counted as sent.
            logger.error(
                "Failed to set command_ready_event: %s -- payload written but never "
                "signaled. action=%s trace_id=%s request_id=%s",
                e, payload.get("action"),
                payload.get("trace_id") or "-", payload.get("request_id") or "-",
                exc_info=True,
            )
            self._log_ipc_dropped(payload, "event_set_failed")
            return
        # IPC_SENT is recorded only here, after the frame is written AND
        # signaled (wh-overlay-slow-uia-stale-badges.14.11): recording it at
        # enqueue produced both IPC_SENT and IPC_DROPPED for one payload
        # whenever the sender later dropped it. The elapsed value comes from
        # the perf_counter anchor stamped at enqueue -- elapsed_ms() reads a
        # ContextVar that is empty in this sender task.
        anchor = payload.get("_pipeline_elapsed_anchor")
        sent_elapsed_ms = (
            (time.perf_counter() - anchor) * 1000.0
            if isinstance(anchor, float) and anchor > 0.0
            else 0.0
        )
        pipeline_logger.info(
            "IPC_SENT action=%s trace_id=%s request_id=%s elapsed_ms=%.1f",
            payload.get("action", ""),
            payload.get("trace_id") or "-", payload.get("request_id") or "-",
            sent_elapsed_ms,
        )
        # Observe consumer clear to confirm pickup (best-effort): the payload is
        # already delivered, so an unreadable event here is not a drop.
        try:
            cleared = await self._await_event_state(desired_set=False, timeout_s=1.0)
        except IpcEventError as e:
            logger.debug("Pickup confirmation unavailable (%s); continuing.", e)
            return
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

    async def send_command(self, payload: Dict[str, Any] | str, params: Optional[Dict[str, Any]] = None) -> bool:
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
        sender task (step 3) will consume from this queue. Returns True when the payload was
        accepted into the queue and False when it was dropped
        (wh-overlay-slow-uia-stale-badges.14.10) -- acceptance, not delivery: the sender can
        still drop an accepted payload later (deadline, unread frame). Callers with
        retry/outcome semantics (add_soft_allow) must check the return; fire-and-forget
        callers may ignore it.
        """
        if isinstance(payload, str):
            # Only an OMITTED params (None) defaults to {} (.14.25): a
            # supplied falsy non-mapping must travel unchanged so the
            # input-side _extract_params gate (.14.22) rejects it,
            # instead of being silently rewritten into an accepted {}.
            payload = {
                "action": payload,
                "params": params if params is not None else {},
            }

        # The stamping block runs caller-mapping operations (lookups and
        # inserts on the supplied dict), and an exact dict can still
        # carry a hash-colliding stored key whose __eq__ raises from the
        # probe (wh-overlay-slow-uia-stale-badges.14.43). Contain those
        # here with a controlled drop; the serializer call below stays
        # OUTSIDE this protection because its oversize ValueError is a
        # documented contract the command engine depends on. The except
        # must not touch the payload again -- render the type name only.
        try:
            trace_id = get_trace_id()
            payload["trace_id"] = trace_id
            # .14.56: drop the caller's response identity BEFORE the
            # frame is frozen. .14.49 removed it from the queued entry
            # only, so the delivered bytes still carried it, and
            # input_proc canonicalizes a request identity from those
            # bytes -- a fire-and-forget command then answered, and an
            # id that collides with a live request settled that
            # caller's future with the wrong action's response.
            payload.pop("request_id", None)
            # Delivery deadline (wh-overlay-slow-uia-stale-badges.5): a
            # fire-and-forget payload that cannot be delivered within its TTL is
            # dropped rather than typed/clicked into a window that has since
            # changed. Never-stale actions (_DURABLE_COMMAND_ACTIONS) get the long
            # budget so dictated words and lifecycle state survive a wedged
            # consumer (wh-overlay-slow-uia-stale-badges.14.1). Only an exact
            # str action is looked up (.14.35, .14.36): an unhashable action
            # raised TypeError here, and a str subclass passes isinstance but
            # its broken hash can raise anything from the membership test --
            # before the reader-side validator (.14.33) could reject the
            # envelope. Non-exact-str actions take the stale-sensitive TTL.
            ttl = (
                _DURABLE_COMMAND_TTL_S
                if type(payload.get("action")) is str
                and payload.get("action") in _DURABLE_COMMAND_ACTIONS
                else _COMMAND_DELIVERY_TTL_S
            )
            deadline = time.monotonic() + ttl
            # .14.58: the drop paths render the queued entry's action,
            # so it is canonicalized here to a value that cannot raise
            # while a diagnostic is built. The FRAME still carries the
            # caller's own action object; the input-side validator
            # (.14.33) is what judges it.
            raw_action = payload.get("action")
            safe_action = _canonical_action_label(raw_action)
            payload["_delivery_deadline_monotonic"] = deadline
            # Anchor for the delivery-time IPC_SENT elapsed value
            # (wh-overlay-slow-uia-stale-badges.14.11): the sender task cannot
            # call elapsed_ms() -- its trace ContextVar is empty there.
            anchor = trace_start_time.get() or 0.0
            payload["_pipeline_elapsed_anchor"] = anchor
        except Exception:
            logger.warning(
                "send_command: dropping command whose envelope cannot be "
                "stamped (payload of type %s)",
                # .14.61: a logging argument is evaluated at the call, so
                # the type name is read through the safe helper or this
                # handler raises instead of returning False.
                _safe_type_name(payload), exc_info=True,
            )
            return False
        # Validate after the envelope fields are attached and before enqueuing.
        # The sender can then retain its resilience behavior without silently
        # losing an oversized fire-and-forget pattern step.
        data = self._serialize_final_payload(payload)
        # The queued entry carries the validated bytes as _frame, and
        # delivery writes those bytes (wh-overlay-slow-uia-stale-badges
        # .14.12, .14.18, .14.30, .14.32): any snapshot built by copying
        # or re-serializing caller-reachable objects can be subverted
        # (__deepcopy__ returning self, __reduce__ resolving to live
        # state), so the delivered representation is frozen HERE, at
        # acceptance. The top-level dict copy detaches the sender's
        # metadata keys (deadline, anchor) from the caller's dict;
        # nested aliases are harmless because nothing reads params after
        # this point. send_request keeps its shared envelope on purpose:
        # the timeout path marks the SAME queued object
        # _delivery_abandoned.
        #
        # .14.43: the _frame insert and the metadata restamps below
        # probe the copied table, so a poisoned stored key can raise
        # here too -- same controlled drop.
        # .14.44: the serializer above ran reducers over
        # caller-reachable state, which can rewrite the payload's
        # stamped metadata in place AFTER the frame froze the honest
        # values -- so the queued copy re-takes the scheduling metadata
        # from the sender's own locals, never from the live caller
        # envelope.
        try:
            queued = dict(payload)
            queued["_frame"] = data
            queued["trace_id"] = trace_id
            queued["_delivery_deadline_monotonic"] = deadline
            queued["_pipeline_elapsed_anchor"] = anchor
            # .14.46: the cancellation mark belongs to send_request's
            # shared envelope; a send_command entry has no cancellation
            # path, so a mark that arrived via the post-serialization
            # copy (a reducer, or the caller) must not drop the
            # accepted frame as cancelled.
            queued.pop("_delivery_abandoned", None)
            # .14.49: every sender diagnostic renders
            # payload.get("request_id") or "-". A raw fire-and-forget
            # payload has no request-response contract, so the queued
            # entry carries no caller request id at all -- a poisoned
            # key or a raising-truthiness value would otherwise raise
            # inside the milestone and leave an accepted frame with no
            # IPC_SENT and no classified drop.
            queued.pop("request_id", None)
            # .14.58: canonical action for every drop diagnostic.
            queued["action"] = safe_action
        except Exception:
            logger.warning(
                "send_command: dropping command whose queued entry cannot "
                "be built (payload of type %s)",
                # .14.61: same safe lookup as the stamping handler above.
                _safe_type_name(payload), exc_info=True,
            )
            return False

        # wh-spaced-punctuation-names-unresolved.3.1.5: bump the
        # generation immediately before the enqueue, so the number the
        # processor reads before its own enqueue tells the two sends
        # apart in the order the Input process will see them. The bump
        # sits inside send_command rather than at each caller because
        # all three producers of this command come through here, and
        # nothing between here and put_nowait awaits.
        if queued.get("action") == "start_utterance":
            self.utterance_start_generation += 1

        try:
            # Enqueue for serialized send by background sender. Overload policy
            # for fire-and-forget: drop the NEW payload with an ERROR log. The
            # callers hold no handle a failure could be returned through, and a
            # full queue means the Input process has been wedged longer than the
            # delivery TTL, so the queued payloads are already dying of old age.
            self._outbound_q.put_nowait(queued)
        except asyncio.QueueFull:
            # .14.58: read the SANITIZED entry, never the live payload.
            # A reducer that ran during serialization can leave a
            # raising-truthiness trace_id behind, and the eager `or "-"`
            # then turned this documented False return into a leaked
            # exception.
            logger.error(
                "Outbound IPC queue full (%d); dropped fire-and-forget command "
                "action=%s trace_id=%s",
                _OUTBOUND_QUEUE_MAX, queued.get("action"),
                queued.get("trace_id") or "-",
            )
            self._log_ipc_dropped(queued, "queue_full")
            return False
        except Exception as e:
            logger.error(f"Failed to enqueue command: {e}", exc_info=True)
            return False

        # No IPC_SENT here: the payload is only accepted, not delivered.
        # _send_one records IPC_SENT after the frame is written and signaled
        # (wh-overlay-slow-uia-stale-badges.14.11).
        return True
            
    async def send_request(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
        on_late_response: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
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

        ``on_late_response`` (wh-overlay-slow-uia-stale-badges.6): optional
        sync callback taking the response dict. When provided, the
        request_id is registered in the bounded late-response registry AT
        FUTURE-CREATION time -- before the enqueue -- so the registration
        spans the whole in-flight window: an answer landing in the gap
        between the wait timeout and the ``finally`` that pops the future
        still reaches the callback through the demuxer's done-future
        fallthrough (wh-overlay-slow-uia-stale-badges.20.1). The
        registration is popped on normal completion, on a non-timeout
        failure, and on CANCELLATION (.20.7 -- a cancelled caller did not
        time out, so no late correction may fire for it); a TIMEOUT leaves
        it registered and ARMS it. The grace runs from the TIMEOUT instant
        with the full ``_LATE_RESPONSE_GRACE_S`` seconds
        (wh-overlay-slow-uia-stale-badges.20.6), implemented as an entry
        lifetime of the effective timeout plus the grace, measured from
        the send instant; after it the entry expires and the unknown-id
        warning returns. The callback runs on the event loop; keep it
        non-blocking.
        """
        request_id = str(uuid.uuid4())
        effective_timeout = timeout_s if timeout_s is not None else self.response_timeout_s
        # Validate before the deadline arithmetic, the future registration,
        # and the enqueue (wh-overlay-slow-uia-stale-badges.14.34): inf and
        # NaN pass max() unchanged, the sender then drops the payload as
        # invalid_deadline, and asyncio.wait_for with a non-finite timeout
        # never completes -- the caller waited forever with its response
        # future and trace metadata stranded. Finite negative timeouts keep
        # their immediate-timeout semantics.
        # .14.51: math.isfinite raises OverflowError for an integer too
        # large to convert to float (10 ** 400), so the huge-integer
        # case has to answer with the same ValueError as every other
        # invalid timeout shape instead of leaking that exception.
        # .14.53: isinstance accepts SUBCLASSES, so a finite float
        # subclass whose comparison raises passed every check here, was
        # registered and enqueued, and only then raised from
        # asyncio.wait_for's own `timeout <= 0` test -- which left a
        # deliverable payload with no caller to receive its response.
        # Only an exact int or float is accepted now, the same rule the
        # durable-action lookup below applies to the action (.14.35).
        # `type(x) is int` also rejects bool, which is a subclass.
        try:
            timeout_is_valid = (
                type(effective_timeout) is int
                or type(effective_timeout) is float
            ) and math.isfinite(effective_timeout)
        except OverflowError:
            timeout_is_valid = False
        if not timeout_is_valid:
            # .14.53: the value is rendered only when its type is
            # exactly int or float. repr of an arbitrary object raises
            # whatever the object wants, and that exception would
            # replace the documented ValueError.
            if type(effective_timeout) is int or type(effective_timeout) is float:
                detail = repr(effective_timeout)
            else:
                # .14.61: the reject path promises ValueError, so the type
                # name is read through the safe helper.
                detail = f"a {_safe_type_name(effective_timeout)}"
            raise ValueError(
                f"send_request timeout must be a finite real number, got {detail}"
            )
        # Delivery deadline = the caller's own timeout
        # (wh-overlay-slow-uia-stale-badges.5): the payload expires from the
        # send queue at the same moment the caller stops waiting, so a
        # timed-out request (for example a click) is never delivered late into
        # a window that has since changed. Durable actions are the exception
        # (wh-overlay-slow-uia-stale-badges.14.4): the normal dictation path is
        # a request (intelligent_insert_text), and its text must still be
        # delivered after a wedged consumer recovers even if the caller has
        # stopped waiting -- FIFO ordering keeps it ahead of the durable
        # end_utterance queued behind it, so the clipboard restore cannot
        # overtake the insertion. Only the delivery budget grows; the caller
        # still waits effective_timeout.
        delivery_ttl = (
            max(effective_timeout, _DURABLE_COMMAND_TTL_S)
            # Only an exact str action is looked up (.14.35, .14.36): an
            # unhashable action raised TypeError here, and a str subclass
            # passes isinstance but its broken hash can raise anything
            # from the membership test -- before the reader-side
            # validator (.14.33) could answer with the standard error.
            if type(action) is str and action in _DURABLE_COMMAND_ACTIONS
            else effective_timeout
        )
        # .14.50: the sender's control metadata is inserted BEFORE
        # params, and each value is kept in a local. Pickling the live
        # envelope runs reducers over caller-controlled values, and a
        # reducer can reach this envelope (gc.get_referrers from its own
        # params dict) and rewrite a control in place. Insertion order
        # decides what the frame records: with the controls written
        # first, pickle has already encoded the honest values by the
        # time any reducer runs.
        trace_id = get_trace_id()
        deadline = time.monotonic() + delivery_ttl
        anchor = trace_start_time.get() or 0.0
        # Awaited window (wh-watchdog-stall-window): the number of seconds
        # THIS caller waits, which is what the Input process's dispatch
        # watchdog needs and the only place that holds it. It is not the
        # delivery deadline above: that is a TTL for getting the command
        # delivered, it is an absolute instant rather than a duration, and
        # for a durable action it is stretched to _DURABLE_COMMAND_TTL_S
        # while the caller still waits only effective_timeout. None for
        # every other action leaves the watchdog's fixed limit in charge.
        # Kept in a local and written into the literal BEFORE params, the
        # rule .14.50 documents for every control on this envelope.
        awaited_window = (
            effective_timeout
            if type(action) is str and action in _AWAITED_WINDOW_ACTIONS
            else None
        )
        payload = {
            "action": action,
            "request_id": request_id,
            "trace_id": trace_id,
            "_delivery_deadline_monotonic": deadline,
            "_awaited_window_s": awaited_window,
            # Anchor for the delivery-time IPC_SENT elapsed value (.14.11).
            "_pipeline_elapsed_anchor": anchor,
            # Only an omitted params (None) defaults to {} (.14.25); a
            # supplied falsy non-mapping travels to the input-side gate.
            "params": params if params is not None else {},
        }
        # The delivered representation is frozen as the validated bytes
        # (.14.28, .14.30, .14.32): "params": params aliases the caller's
        # mutable graph, and copy- or loads-based snapshots can be
        # subverted by __deepcopy__ or __reduce__ overrides, so delivery
        # writes the _frame bytes captured here and never re-serializes
        # the envelope.
        frame = self._serialize_final_payload(payload)
        # .14.57: the QUEUE no longer receives the live envelope. Every
        # post-freeze write to that envelope (the .14.50 restamps and
        # the .14.52 pop) probed a table a reducer may have re-keyed
        # during pickling, so a poisoned control key raised out of the
        # sender before the future was even registered; and the action,
        # which was never restamped, could name something the frozen
        # bytes do not carry. This entry is built from sender locals
        # only. Caller params are deliberately absent: delivery writes
        # _frame, and nothing downstream reads params -- so no
        # caller-reachable object remains where a diagnostic or a
        # control write could touch it.
        queued = {
            "action": _canonical_action_label(action),
            "request_id": request_id,
            "trace_id": trace_id,
            "_delivery_deadline_monotonic": deadline,
            # Anchor for the delivery-time IPC_SENT elapsed value (.14.11).
            "_pipeline_elapsed_anchor": anchor,
            "_frame": frame,
        }

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.response_futures[request_id] = future
        # Captured here, in the requester's context, so the demux task can
        # stamp IPC_COMPLETE with this request's trace id and an elapsed
        # value from the same anchor as IPC_SENT (.14.23).
        self._request_trace_meta[request_id] = (trace_id, anchor)
        if on_late_response is not None:
            # Register BEFORE the enqueue (wh-overlay-slow-uia-stale-badges
            # .20.1): a response can land between the wait timeout and the
            # finally-pop below, where the demuxer pops the DONE future; the
            # registration must already exist for its fallthrough to find.
            # The lifetime spans the whole awaiter period PLUS the grace,
            # so the grace runs from the timeout instant (.20.6).
            self._register_late_response(
                request_id, on_late_response,
                effective_timeout + _LATE_RESPONSE_GRACE_S,
            )

        try:
            # Enqueue for serialized send; demuxer resolves the future.
            # Overload policy for request/response: fail fast and visibly.
            try:
                self._outbound_q.put_nowait(queued)
            except asyncio.QueueFull:
                # .14.58: both the drop record and the exception message
                # name the canonicalized action. An f-string calls
                # format(), so rendering the caller's own action object
                # replaced this documented IpcDeliveryError with
                # whatever that object chose to raise.
                self._log_ipc_dropped(queued, "queue_full")
                raise IpcDeliveryError(
                    f"outbound IPC queue full ({_OUTBOUND_QUEUE_MAX}); "
                    f"request '{queued['action']}' was not sent"
                )
            # No IPC_SENT here: the payload is only accepted, not delivered.
            # _send_one records IPC_SENT after the frame is written and
            # signaled (wh-overlay-slow-uia-stale-badges.14.11).
            result = await asyncio.wait_for(future, timeout=effective_timeout)
            if on_late_response is not None:
                # Answered on time -- the registration has no answer left to
                # catch; drop it so a stray duplicate cannot fire the callback.
                self._pop_late_response(request_id)
            return result
        except asyncio.CancelledError:
            # Explicit caller cancellation abandons the queued payload
            # (wh-overlay-slow-uia-stale-badges.14.7): the overlay abort path
            # cancels its build request while the payload waits behind a busy
            # frame, and without this mark the sender still delivered the
            # stale build once the frame came free. A TIMEOUT does not
            # abandon: durable requests must out-survive their caller's
            # timeout by design (wh-overlay-slow-uia-stale-badges.14.4).
            # .14.57: the mark goes on the canonical queued entry, which
            # IS the object the queue holds. Every key in it is
            # sender-owned, so this write cannot raise from a poisoned
            # key the way a write to the live envelope could.
            queued["_delivery_abandoned"] = True
            # wh-overlay-slow-uia-stale-badges.20.7: the caller was
            # CANCELLED, not timed out -- no late correction may fire for
            # it. CancelledError is a BaseException on this runtime, so the
            # Exception clause below cannot pop it; it needs its own.
            if on_late_response is not None:
                self._pop_late_response(request_id)
            raise
        except asyncio.TimeoutError:
            # Deliberately LEAVE the registration in place: the request may
            # still complete in the Input process, and the demuxer hands the
            # late answer to the callback within the grace window
            # (wh-overlay-slow-uia-stale-badges.6). ARM it
            # (wh-overlay-slow-uia-stale-badges.20.7): the timeout is the
            # one terminal outcome that licenses the demuxer's unknown-id
            # consult to fire this entry.
            if on_late_response is not None:
                entry = self._late_response_callbacks.get(request_id)
                if entry is not None:
                    entry.armed = True
            logger.error(
                f"Request '{queued['action']}' (id: {request_id}) "
                f"timed out after {effective_timeout}s."
            )
            raise
        except Exception as e:
            if on_late_response is not None:
                # A non-timeout failure: the request never reached the wire
                # (or the demuxer surfaced an error response), so no late
                # answer can exist -- remove the registration.
                self._pop_late_response(request_id)
            # Carries both ids so the operational ERROR record is
            # correlatable (wh-overlay-slow-uia-stale-badges.14.9); the
            # queue-full IpcDeliveryError lands here too.
            logger.error(
                "send_request failed for action '%s' (request_id=%s trace_id=%s): %s",
                # .14.58: sender locals only -- reading the live
                # envelope here re-exposed the value class this finding
                # closes, on the path the IpcDeliveryError travels.
                queued["action"], request_id, trace_id or "-", e,
                exc_info=True,
            )
            raise
        finally:
            # Ensure future is removed to prevent memory leaks
            self.response_futures.pop(request_id, None)
            self._request_trace_meta.pop(request_id, None)

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
