"""Input process coordination and UI command synthesis.

This module implements the target function for the headless input synthesis
process, which handles all low-level UI interactions including mouse control,
keyboard input, and system integration. It runs in a separate process to
isolate UI operations and provides a shared memory interface for receiving
commands from the main WheelHouse service.

Key Functions:
  - input_process_main: Main entry point for the input synthesis process.
  - Command processing and execution pipeline.
  - UI state monitoring and feedback.

Key Features:
  - Low-level Windows API integration for precise input control
  - Shared memory communication for low-latency command processing
  - Input event listening and monitoring
  - Clipboard integration and text manipulation
  - Window focus and application state management

Process Architecture:
  - Runs as separate process for UI isolation
  - Communicates via shared memory and events
  - Responds to commands from main service
  - Provides feedback via response queue

Typical Usage:
  # Started automatically by launcher
  from input_proc import input_process_main
  
  input_process_main(
      shm_name="wheelhouse_shm",
      command_ready_event=cmd_event,
      input_ready_event=ui_event,
      response_queue=resp_queue,
      shutdown_event=shutdown_event
  )
"""
# input_proc.py: Target function for the headless input synthesis process.
import logging
import multiprocessing
import os
import re
import sys
import time
import threading
from queue import Empty
import pickle
import struct
from multiprocessing import shared_memory, Queue
from pynput import mouse, keyboard
from services.wheelhouse.shared.context_mirror import ContextMirror
import win32gui
import win32process
import psutil

logger = logging.getLogger(__name__)

# Actions whose handlers emit their own Schema A response and therefore must
# skip the generic success/error emission at the end of the main loop
# (wh-lla5d). Adding an action here is a contract: its implementation on
# UIActionHandler must send exactly one Schema A response through
# ResponseHandler for every request_id it receives (success or error).
#
# retract is also handler-owned, but it takes an earlier explicit path in
# the main loop and never reaches the generic emitter, so it does not need
# to be listed here.
_HANDLES_OWN_RESPONSE = frozenset({
    "intelligent_insert_text",
    "wrap_or_insert",
    # wh-ftg63 (wh-9weum Phase 4): the retry handler emits a
    # RetryDictationByTokenResponse-shaped payload (status + retry_outcome
    # + reason). The generic dispatcher's "ok / heuristic_done" emitter
    # would clobber the contract response; list the action here so the
    # generic emitter skips it.
    "retry_dictation_by_token",
    # wh-pkhrp.1.1 (Approach A): the focus-redirect path opens the
    # terminal dictation editor with empty initial text and waits for
    # FOCUS_CONFIRMED before draining buffered words. The handler
    # emits its own Schema A response.
    "open_editor_for_redirect",
    # wh-jfavj (wh-l4h.1 Phase 1.5 stub): the handler emits its own
    # ShowNumberedOverlayResponse Schema A response (status=
    # "not_implemented" in this stub slice; the Phase 1.5 implementation
    # fills the snapshot_summary). The generic emitter would clobber it.
    "show_numbered_overlay",
    # wh-jfavj (wh-l4h.1 Phase 1.5 stub): the handler emits its own
    # ClickElementResponse Schema A response (not_implemented in this stub
    # slice; the real click executor is wh-tab7j). The generic emitter
    # would clobber it.
    "click_snapshot_item",
    # wh-tab7j (wh-l4h.1 Phase 1): the by-name click handler walks the
    # focused window via ElementFinder, runs ClickExecutor on a clear
    # winner, and emits exactly one ClickElementResponse Schema A response.
    # The generic emitter would clobber the executor's outcome.
    "click_element",
    # wh-n29v.37 (wh-l4h.1 Phase 1.5): the standalone numbered-overlay build
    # handler walks the focused window from scratch via ElementFinder,
    # numbers every interactive control 1..K, and emits exactly one
    # StartOverlayWalkResponse Schema A response (echoing the generation
    # fields). It is never-raise (an unexpected error maps to outcome=error).
    # The generic emitter would clobber the walk outcome.
    "start_overlay_walk",
    # wh-n29v.41 (wh-l4h.1 Phase 1.5): the active-overlay pin transport. Each
    # handler drives the multi-snapshot store's pin()/unpin() and emits exactly
    # one PinSnapshotResponse Schema A response (echoing overlay_session_id +
    # snapshot_id). Logic does NOT block the paint on the ack, but the Future
    # must still resolve, so both are never-raise and self-owning; the generic
    # emitter would clobber the ack. NOTE: these are NOT added to the
    # command_dequeue_monotonic injection block above -- that timestamp anchors
    # the UIA-walk deadline (click_element / start_overlay_walk only); pin/unpin
    # are store operations with no walk deadline.
    "pin_snapshot",
    "unpin_snapshot",
    # wh-overlay-snapshot-keepalive: the Input side of the Logic 15s overlay
    # keepalive. Slides the still-visible pinned snapshot's TTL anchor via
    # ElementFinder.refresh_snapshot_ttl and emits its own PinSnapshotResponse
    # (reused as the ack). Never-raise and self-owning like pin/unpin; the
    # generic emitter would clobber the ack. Like pin/unpin it is NOT in the
    # command_dequeue_monotonic injection block (a store touch, no walk
    # deadline).
    "refresh_overlay_snapshot",
})


def _should_emit_keyboard_invalidation(normalized_key: str, is_internal_action: bool) -> bool:
    """Decide whether a keypress should invalidate terminal transaction state."""
    if is_internal_action:
        return False
    return normalized_key not in ['ctrl', 'alt', 'shift', 'win']

def _find_window_by_target(target: str, logger):
    """Find a window by process name (*.exe) or title pattern.
    
    Args:
        target: Either a process name (e.g., 'brave.exe') or title regex pattern
        logger: Logger instance for debug messages
        
    Returns:
        Window handle (hwnd) if found, None otherwise
    """
    import win32gui
    import win32process
    
    is_process = target.lower().endswith(".exe")
    target_hwnd = None
    
    def enum_callback(hwnd, _):
        """
        :flow: Window Activation
        :step: 2
        :description: Window enumeration callback - searches for target window by process name or title.
        :data_in: Window handle (hwnd) from EnumWindows iteration.
        :data_out: Sets nonlocal target_hwnd when match found, returns False to stop enumeration.
        :notes: Callback for win32gui.EnumWindows() in activate_window_by_target(). Iterates all top-level windows
            searching for match by either process name (.exe) or window title pattern. Uses psutil to resolve
            process names from PIDs. Skips invisible windows and windows without titles. Returns False when
            target found to stop enumeration early (performance optimization).
        """
        nonlocal target_hwnd
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            
            title = win32gui.GetWindowText(hwnd)
            if not title:  # Skip windows without titles
                return True
            
            if is_process:
                # Match by process name
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    proc_name = psutil.Process(pid).name().lower()
                    if proc_name == target.lower():
                        target_hwnd = hwnd
                        return False  # Stop enumeration
                except Exception as e:
                    logger.debug(f"Process lookup failed for hwnd {hwnd}: {e}")
            else:
                # Match by title (case-insensitive regex search)
                if re.search(target, title, re.IGNORECASE):
                    target_hwnd = hwnd
                    return False  # Stop enumeration
        except Exception as e:
            logger.debug(f"Error checking window {hwnd}: {e}")
            return True  # Always return True to continue enumeration on error
        
        return True  # Continue enumeration
    
    # Enumerate windows - may fail if a window is destroyed during enumeration
    try:
        win32gui.EnumWindows(enum_callback, None)
    except Exception as e:
        # This is a normal race condition - window closed during enumeration
        logger.debug(f"EnumWindows error (window likely closed during enumeration): {e}")
    
    return target_hwnd

def _activate_window_impl(hwnd: int, logger) -> bool:
    """Activate a window, handling minimized windows and Windows focus restrictions.
    
    Args:
        hwnd: Window handle to activate
        logger: Logger instance for messages
        
    Returns:
        True if activation succeeded, False otherwise
    """
    import win32gui
    import win32process
    import win32con
    
    try:
        # First, check if the window is minimized and restore it
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        
        # Try to force focus using thread attachment to bypass Windows restrictions
        try:
            import win32api
            current_thread = win32api.GetCurrentThreadId()
            target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
            if current_thread != target_thread:
                win32process.AttachThreadInput(current_thread, target_thread, True)
                win32gui.SetForegroundWindow(hwnd)
                win32gui.BringWindowToTop(hwnd)
                win32process.AttachThreadInput(current_thread, target_thread, False)
            else:
                win32gui.SetForegroundWindow(hwnd)
        except:
            # If thread attach fails, try direct method
            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)
        
        logger.info(f"Activated window: hwnd={hwnd}, title={win32gui.GetWindowText(hwnd)}")
        return True
    except Exception as e:
        logger.warning(f"Failed to activate window {hwnd}: {e}")
        return False

def _verify_window_activation(target: str, request_id, response_queue, action: str,
                               foreground_poll_ms: int, poll_interval_ms: int,
                               action_delay_ms: int, logger) -> None:
    """:flow: UI Action Response
    :step: 1a
    :description: activate_window response generation with foreground polling
    :data_in: target window identifier, request_id from step 7a of UI Action Execution
    :data_out: Response dict enqueued to response_queue (step 2)
    :execution_context: input_proc (GUI process)
    :execution_mode: sync
    :notes: Branch 1a - alternative response generation path for activate_window (step 7a). Parallel
    to step 1 (general actions). Polls GetForegroundWindow for up to foreground_poll_ms to verify
    target window became active. Supports two target types: (1) Process name (*.exe) - compares
    psutil.Process.name() via GetWindowThreadProcessId, (2) Title pattern - regex search on
    GetWindowText result. On success, enqueues response with path='foreground_done'. If polling
    times out, falls back to heuristic delay (action_delay_ms) and enqueues path='heuristic_done'.
    Converges with step 1 at step 2 (response_queue.put) for consumption by demuxer (step 3).
    """
    """
    Poll to verify a window matching target became the foreground window.

    Args:
        target: Process name (*.exe) or title pattern to match
        request_id: Request ID to include in response
        response_queue: Queue to send verification response
        action: Action name for response
        foreground_poll_ms: How long to poll for foreground change
        poll_interval_ms: Delay between poll attempts
        action_delay_ms: Fallback delay if verification fails
        logger: Logger instance
    """
    import win32gui
    import win32process
    import time
    
    is_process = target.lower().endswith(".exe")
    done = False
    start = time.time()
    
    while (time.time() - start) * 1000 < foreground_poll_ms:
        try:
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd) or ""
            
            if is_process:
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    proc_name = psutil.Process(pid).name().lower()
                    if proc_name == target.lower():
                        response_queue.put({'request_id': request_id, 'status': 'ok',
                                          'path': 'foreground_done', 'action': action})
                        done = True
                        break
                except Exception as e:
                    logger.debug(f"Verification process lookup failed: {e}")
            else:
                if re.search(target, title, re.IGNORECASE):
                    response_queue.put({'request_id': request_id, 'status': 'ok',
                                      'path': 'foreground_done', 'action': action})
                    done = True
                    break
        except Exception as e:
            logger.debug(f"Verification loop iteration failed: {e}")
        time.sleep(poll_interval_ms / 1000.0)
    
    if not done:
        # Fallback to heuristic timing
        time.sleep(action_delay_ms / 1000.0)
        response_queue.put({'request_id': request_id, 'status': 'ok', 
                          'path': 'heuristic_done', 'action': action})

def _handle_activate_window(params: dict, request_id, response_queue, action: str,
                            is_internal_action: threading.Event,
                            foreground_poll_ms: int, poll_interval_ms: int,
                            action_delay_ms: dict, logger) -> None:
    """:flow: UI Action Execution
    :step: 7a
    :description: Direct window activation without UIActionHandler
    :data_in: params with window target, optional request_id
    :data_out: Window activation + optional response on response_queue
    :execution_context: input_proc (GUI process)
    :execution_mode: sync
    :notes: Branch 7a from step 7 conditional. Special case bypassing UIActionHandler overhead.
    Uses win32gui.FindWindow to locate target window by title/class, win32gui.SetForegroundWindow
    to activate. If request_id present, polls to verify activation succeeded (checks
    GetForegroundWindow matches target) before sending response. Polling uses foreground_poll_ms
    window with poll_interval_ms checks. Fallback to heuristic delay if verification times out.
    Isolated from UIActionHandler to keep window management independently testable. Response sent
    via response_queue for consumption by _response_demuxer (UI Action Response flow).
    """
    
    """
    Handle the activate_window action from start to finish.
    This orchestrates: find window → activate (or launch a .exe target
    that has no window) → verify → respond.

    Args:
        params: Action parameters containing 'target'
        request_id: Request ID for response (None if no response needed)
        response_queue: Queue to send responses
        action: Action name
        is_internal_action: Event to set during action execution
        foreground_poll_ms: Polling timeout for verification
        poll_interval_ms: Delay between polls
        action_delay_ms: Dict of action-specific delays
        logger: Logger instance
    """
    target = params.get("target") or ""
    is_process = target.lower().endswith(".exe")
    logger.info(f"Activating window: target={target} ({'process' if is_process else 'title'})")
    
    is_internal_action.set()
    try:
        # Find the target window
        target_hwnd = _find_window_by_target(target, logger)
        
        if target_hwnd:
            # Activate the window
            _activate_window_impl(target_hwnd, logger)
        elif is_process:
            # Launch fallback (wh-activate-launch-fallback): the app has no
            # window, so start it. os.startfile -> ShellExecute resolves the
            # bare exe name via System32, PATH, and the App Paths registry
            # key, and returns without waiting, so the command loop is not
            # blocked. Only .exe targets are launchable; a title regex has
            # nothing to execute.
            logger.info(f"No window found for {target}; launching it")
            try:
                os.startfile(target)
            except OSError as e:
                logger.warning(f"Could not launch {target}: {e}")
        else:
            logger.warning(f"No window found matching: {target}")
        
        # Verification polling (if request_id present)
        if request_id:
            _verify_window_activation(target, request_id, response_queue, action,
                                     foreground_poll_ms, poll_interval_ms, 
                                     action_delay_ms.get(action, 500), logger)  # 500ms default for unknown actions
    
    except Exception as e:
        logger.error(f"Error in activate_window: {e}", exc_info=True)
        if request_id:
            response_queue.put({'request_id': request_id, 'error': True, 
                              'message': str(e), 'action': action})
    finally:
        is_internal_action.clear()

def _poll_and_update_context(context_mirror, last_hwnd):
    """
    :flow: Context Mirroring
    :step: 1
    :description: Polls foreground window state and updates shared memory if changed.
    :data_in: Current foreground window handle (via GetForegroundWindow).
    :data_out: Writes JSON context to shared memory if window changed.
    :execution_context: Input Process
    :notes: Lightweight polling (50ms interval) using fast Win32 APIs.
            Avoids heavy UIA queries to prevent input lag.
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd != last_hwnd:
            last_hwnd = hwnd
            title = win32gui.GetWindowText(hwnd)
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid > 0:
                    proc = psutil.Process(pid)
                    app_name = proc.name()
                else:
                    app_name = ""
            except Exception as e:
                logger.debug(f"Context mirror process lookup failed: {e}")
                app_name = ""

            context_mirror.write_context({
                "app_name": app_name,
                "window_title": title,
                "timestamp": time.time()
            })
    except Exception as e:
        logger.debug(f"Context mirror update failed: {e}")
    return last_hwnd

def input_process_main(shm_name: str, command_ready_event: multiprocessing.Event, input_ready_event: multiprocessing.Event, response_queue: Queue, shutdown_event: multiprocessing.Event):
    """
    :flow: UI Action Execution
    :step: 1
    :description: Main loop coordinating all low-level Windows UI automation.
    :data_in: Action payloads via shared memory from Logic process.
    :data_out: Completion responses via response_queue.
    :consumes_from: Command and Dictation Routing
    :execution_context: Input Process (separate from Logic)
    :notes: This process handles all low-level UI interactions including window management,
    keyboard/mouse input, and clipboard operations. Runs in separate process for isolation.
    Architecture: Receives commands via shared memory, executes Windows SendInput API calls,
    monitors user input to detect interaction (invalidates pattern buffer), polls for
    action completion using empirically tuned timeouts, returns status via response_queue.
    
    Args:
        shm_name: Name of the shared memory segment for IPC
        command_ready_event: Event signaled when a command is ready
        input_ready_event: Event to signal when this process is ready
        response_queue: Queue for sending action completion responses
        shutdown_event: Event to signal process shutdown
    """
    from services.wheelhouse.config_service import ConfigService
    from utils.logging_setup import setup_logging
    from utils.trace_context import set_trace
    from ui.ui_actions import UIActionHandler
    from ui.clipboard import get_text_safe

    shm = None
    mouse_listener = None
    keyboard_listener = None
    _fault_file = None
    try:
        # ============================================================================
        # INITIALIZATION
        # ============================================================================
        config_service = ConfigService()
        config = config_service.get_config()
        setup_logging(config)

        # Enable faulthandler to capture native crashes (access violations, segfaults)
        # that bypass Python's exception handling.  Writes traceback to a file so
        # we can diagnose silent process deaths after the fact.
        import faulthandler
        import tempfile, os
        _fault_path = os.path.join(tempfile.gettempdir(), "wheelhouse_input_crash.log")
        _fault_file = open(_fault_path, "a")
        faulthandler.enable(file=_fault_file)
        logger.info("Input process started. Faulthandler writing to %s", _fault_path)
        shm = shared_memory.SharedMemory(name=shm_name)
        ui_handler = UIActionHandler(response_queue, config)
        is_internal_action = threading.Event()
        
        # Initialize Context Mirror in writer mode
        context_mirror = ContextMirror()
        context_mirror.init_writer()
        last_context_check = 0
        CONTEXT_CHECK_INTERVAL = 0.05  # 50ms polling for context updates
        last_hwnd = 0

        # --- Heuristics and Timings ---
        # These timing values are empirically determined to handle asynchronous Windows UI behavior.
        # They represent the balance between responsiveness and reliability in verifying actions.
        
        # ACTION_DELAY_MS: Fallback delays (ms) when verification polling times out.
        # These represent typical worst-case completion times for each action type.
        # Used as last resort when we can't verify completion through polling.
        ACTION_DELAY_MS = {
            "hotkey_action": 550,           # Hotkey processing + app response time
            "press_key_action": 400,        # Key press propagation through UI
            "type_text": 400,               # Text insertion completion
            "intelligent_insert_text": 50,  # Text insertion is synchronous, minimal delay for UI update
            "activate_window": 650,         # Window activation + focus settling
            "transform_selection": 800,     # Text transformation with clipboard operations
            "skip_clipboard_restore": 0,    # Just sets a flag, no UI operation
            "clear_skip_clipboard_restore": 0,  # Just clears a flag, no UI operation
            "start_utterance": 0,           # Just saves clipboard state
            "end_utterance": 0,             # Just restores clipboard state
            "retract": 0,                   # Backspaces are synchronous SendInput
        }
        
        # CLIPBOARD_TIMEOUT_MS: How long to poll for clipboard changes after Ctrl+C (1200ms).
        # Accounts for slow applications that take time to populate clipboard after copy command.
        CLIPBOARD_TIMEOUT_MS = 1200
        
        # FOREGROUND_POLL_MS: How long to poll for window activation (900ms).
        # Windows activation can be delayed by focus restrictions and window animations.
        FOREGROUND_POLL_MS = 900
        
        # POLL_INTERVAL_MS: Delay between polling attempts (20ms).
        # Optimized for low-latency dictation response. Fast polling minimizes detection
        # delay for rapid action completion (clipboard changes, window activation).
        POLL_INTERVAL_MS = 20

        # ============================================================================
        # INPUT MONITORING
        # ============================================================================
        def _normalize_key(key) -> str:
            if isinstance(key, keyboard.Key):
                key_name = str(key).replace('Key.', '')
                if 'ctrl' in key_name: return 'ctrl'
                if 'alt' in key_name: return 'alt'
                if 'shift' in key_name: return 'shift'
                if 'cmd' in key_name: return 'win'
                return key_name
            if hasattr(key, 'char') and key.char is not None:
                result = key.char.lower()
                if not result.isprintable():
                    logger.debug("Non-printable KeyCode: char=%r, vk=%s, type=%s",
                                 key.char, getattr(key, 'vk', '?'), type(key).__name__)
                return result
            fallback = str(key).lower()
            logger.debug("KeyCode fallback: str=%r, vk=%s, type=%s",
                         fallback, getattr(key, 'vk', '?'), type(key).__name__)
            return fallback

        def on_user_click(x, y, button, pressed):
            """
            :flow: Multi-Word Pattern Buffer
            :step: 5
            :description: Detects user mouse clicks to invalidate buffering state.
            :data_in: Mouse click events from pynput listener.
            :data_out: Buffer invalidation signal to UIActionHandler.
            :produces_for: Multi-Word Pattern Catalog
            :notes: When user manually clicks while pattern buffer is active, this indicates
            intentional interaction with UI that should abort buffered dictation state.
            Filters out internal clicks (from automated actions) using is_internal_action flag.
            Uses WindowFromPoint to check the actual click target -- clicks on overlay
            windows (virtual keyboards, always-on-top tools) that aren't the foreground
            app are forwarded as mouse:left:other so terminal escalation can ignore them.
            """
            if pressed and button == mouse.Button.left and not is_internal_action.is_set():
                # Check if the click target window matches the foreground window.
                # Virtual keyboards and other overlays sit on top but are different
                # windows/processes.  Only clicks on the actual foreground app
                # should signal "mouse:left" for terminal escalation purposes.
                try:
                    import ctypes
                    point = ctypes.wintypes.POINT(int(x), int(y))
                    clicked_hwnd = ctypes.windll.user32.WindowFromPoint(point)
                    fg_hwnd = win32gui.GetForegroundWindow()
                    # Walk up to top-level for both (child windows share the top-level)
                    clicked_root = win32gui.GetAncestor(clicked_hwnd, 2) if clicked_hwnd else 0  # GA_ROOT = 2
                    fg_root = win32gui.GetAncestor(fg_hwnd, 2) if fg_hwnd else 0
                    if clicked_root and fg_root and clicked_root != fg_root:
                        ui_handler.invalidate_buffer(source="mouse:left:other")
                        return
                except Exception:
                    pass  # Fall through to normal behavior on any error
                ui_handler.invalidate_buffer(source="mouse:left")

        def on_user_press(key):
            """
            :flow: Multi-Word Pattern Buffer
            :step: 6
            :description: Detects user keypresses to invalidate buffering state.
            :data_in: Keyboard events from pynput listener.
            :data_out: Buffer invalidation signal to UIActionHandler.
            :produces_for: Multi-Word Pattern Catalog
            :notes: When user manually types while pattern buffer is active, this indicates
            they're manually correcting or typing, which should abort buffered dictation.
            Ignores modifier keys (ctrl/alt/shift/win) and internal actions to avoid
            false invalidation from automated hotkey execution.
            """
            normalized_key = _normalize_key(key)
            if _should_emit_keyboard_invalidation(normalized_key, is_internal_action.is_set()):
                ui_handler.invalidate_buffer(source=f"keyboard:{normalized_key}")

        mouse_listener = mouse.Listener(on_click=on_user_click)
        keyboard_listener = keyboard.Listener(on_press=on_user_press)
        mouse_listener.start()
        keyboard_listener.start()
        logger.debug("Input process listeners started.")
        
        input_ready_event.set()

        # ============================================================================
        # MAIN COMMAND LOOP
        # ============================================================================
        while not shutdown_event.is_set():
            now = time.time()

            # --- Context Mirroring (Polling) ---
            # Check for foreground window changes periodically
            if now - last_context_check > CONTEXT_CHECK_INTERVAL:
                last_context_check = now
                last_hwnd = _poll_and_update_context(context_mirror, last_hwnd)

            # Wait for command with 10ms timeout for low-latency dictation response.
            # Rapid polling is essential for real-time text dictation where Google STT
            # sends individual word deltas that must be processed immediately to avoid
            # queue buildup and missing text. 10ms provides <20ms average latency.
            signaled = command_ready_event.wait(timeout=0.01)
            if not signaled:
                continue
            
            """:flow: UI Action Execution
            :step: 6
            :description: GUI process polls event, reads and unpickles command from shared memory
            :data_in: command_ready_event signal + framed binary data in shm.buf
            :data_out: Deserialized command_message dictionary
            :execution_context: input_proc (GUI process)
            :execution_mode: sync
            :notes: IPC receiver side. Main loop polls command_ready_event with 10ms timeout (avoids
            blocking, <20ms average latency). On signal: (1) Reads 4-byte size header to determine
            message length, (2) Reads msg_len bytes of pickled data, (3) Creates independent bytearray
            copy to prevent BufferError on shm.close(), (4) Clears event to signal sender that data
            has been safely copied, (5) Unpickles into command_message dict. Event is cleared AFTER
            data copy to prevent race condition where sender could overwrite buffer mid-read.
            Extracts action, params, request_id for dispatch (step 7).
            Shutdown signal: command_message=None causes clean exit.
            """
            try:
                # Shared memory protocol: [4-byte size prefix][pickled message data]
                # Read the 4-byte big-endian unsigned int that specifies message length
                size_bytes = bytes(shm.buf[:4])
                msg_len = struct.unpack('>I', size_bytes)[0]
                # Create independent copies to prevent BufferError on shm.close()
                msg_data = bytearray(msg_len)
                msg_data[:] = shm.buf[4:4 + msg_len]  # Skip the 4-byte size prefix
                # Clear event AFTER data copy to signal sender it's safe to write next command.
                # This prevents race condition where sender overwrites buffer while we're reading.
                command_ready_event.clear()
                command_message = pickle.loads(msg_data)
                # wh-9f3t.73.1: anchor the per-request walk deadline HERE, the
                # earliest point the Input command-reader loop owns this
                # message -- immediately after deserialization and BEFORE the
                # INPUT_RECEIVED logging + dispatch lookup (a path the
                # input_proc comments document as having stalled ~1.0s). The
                # Logic awaiter's clock started at send_request, so charging the
                # walk budget from this dequeue instant (rather than from
                # UIActionHandler.click_element entry) folds the pre-handler
                # reader time into the budget and keeps the walk from blowing
                # past the awaiter before it even starts.
                command_dequeue_monotonic = time.monotonic()
            except Exception as e:
                logger.error("Input proc read from SHM failed: %s", e)
                command_ready_event.clear()  # Still need to clear so sender can proceed
                continue

            if command_message is None:
                logger.info("Input process received shutdown signal via SHM. Exiting.")
                break

            """:flow: UI Action Execution
            :step: 7
            :description: Dispatches command to appropriate handler based on action type
            :data_in: Deserialized command_message with action, params, request_id
            :data_out: Routed to handler function
            :branches_to: Step 7a (activate_window), Step 7b (UIActionHandler)
            :execution_context: input_proc (GUI process)
            :execution_mode: conditional
            :condition: If action == "activate_window" → 7a, else → 7b
            :notes: Critical dispatch point routing to different handlers. activate_window handled
            directly via _handle_activate_window() because it only needs win32gui operations, avoiding
            UIActionHandler overhead. All other actions go through UIActionHandler which provides full
            UI automation framework (UIA, terminal detection, shadow buffer, clipboard management).
            Validates action exists via hasattr(ui_handler, action), logs warning if unknown.
            """
            action = command_message.get("action")
            params = command_message.get("params", {}) or {}
            request_id = command_message.get("request_id")
            trace_id = command_message.get("trace_id", "")
            set_trace(trace_id)

            # wh-9f3t.73.1 (+ reviewer_0 wh-n29v.38.2): thread the dequeue-
            # anchored monotonic timestamp to the two UIA-walk handlers
            # (click_element and start_overlay_walk) via their own params dict, so
            # each walk deadline is charged from the earliest reader instant.
            # Scoped to these two actions so NO other handler dispatched through
            # method_to_call(**params) (the wh-lla5d-sensitive site) receives an
            # unexpected kwarg. Both handlers accept it as an explicit optional
            # parameter; it never reaches any other handler's signature.
            if action in ("click_element", "start_overlay_walk"):
                params = {
                    **params,
                    "command_dequeue_monotonic": command_dequeue_monotonic,
                }

            # --- Special Case: Window Activation ---
            # activate_window is handled directly here because it only requires win32gui
            # operations and doesn't need the UIActionHandler's UI automation framework.
            # This keeps the window management logic isolated and independently testable.
            if action == "activate_window":
                _handle_activate_window(params, request_id, response_queue, action,
                                       is_internal_action, FOREGROUND_POLL_MS,
                                       POLL_INTERVAL_MS, ACTION_DELAY_MS, logger)
                continue

            # --- Special Case: Retraction ---
            # retract is handled directly here to return retraction status
            # in the response (not just ok/error like generic actions).
            if action == "retract":
                is_internal_action.set()
                try:
                    result = ui_handler.retract()
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            **result,
                            'action': action
                        })
                except Exception as e:
                    logger.error(f"Error in retract: {e}", exc_info=True)
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            'status': 'not_retracted',
                            'reason': f'error: {e}',
                            'action': action
                        })
                finally:
                    is_internal_action.clear()
                continue

            # --- Special Case: AI Clipboard Operations ---
            # capture/replace need custom response data (text or success),
            # not the generic ok/heuristic_done from the default handler.
            if action in ("capture_selected_text", "replace_selected_text"):
                is_internal_action.set()
                try:
                    method = getattr(ui_handler, action)
                    result = method(**params)
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            **result,
                            'action': action,
                        })
                except Exception as e:
                    logger.error(f"Error in {action}: {e}", exc_info=True)
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            'error': True,
                            'message': str(e),
                            'action': action,
                        })
                finally:
                    is_internal_action.clear()
                continue

            # --- Special Case: Log Level ---
            if action == "terminal_editor_cancelled":
                ui_handler.terminal_editor_cancelled()
                continue

            if action == "_te_event_ack":
                # wh-t81d9.2: GUI confirmed a previously enqueued show
                # te_event was applied (or a submit lifecycle ack arrived).
                # The proxy's on_event_ack clears submit-in-progress state
                # on submit_complete/submit_failed:* and records the
                # editor HWND on show acks. Late acks for unknown
                # request_ids are logged at debug level inside
                # on_event_ack and ignored.
                rid = params.get("request_id", "")
                op = params.get("op", "")
                editor_hwnd = params.get("editor_hwnd", 0) or None
                try:
                    ui_handler.terminal_editor.on_event_ack(rid, op, editor_hwnd)
                except Exception as e:
                    logger.error("Error in _te_event_ack: %s", e, exc_info=True)
                continue

            if action == "set_log_level":
                new_level = params.get("level", "INFO")
                logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))
                logger.info("Logging level set to %s", new_level)
                continue

            # wh-9weum Phase 3 (wh-01t75): runtime soft-allow update.
            # Logic has already written the disk file; this command just
            # updates the input-process predicate's in-memory set so the
            # next evaluate call sees the new tuple without restart.
            if action == "add_soft_allow_tuple":
                process_name = params.get("process_name", "")
                class_name = params.get("class_name", "")
                control_type = params.get("control_type", "")
                if not (process_name and class_name and control_type):
                    logger.warning(
                        "add_soft_allow_tuple: missing required field "
                        "(process_name=%r class_name=%r control_type=%r)",
                        process_name, class_name, control_type,
                    )
                    continue
                try:
                    ui_handler.text_target_predicate.add_soft_allow(
                        (process_name, class_name, control_type),
                    )
                    logger.info(
                        "add_soft_allow_tuple: predicate updated -- "
                        "process=%s class=%s control_type=%s",
                        process_name, class_name, control_type,
                    )
                except Exception as e:
                    logger.error(
                        "add_soft_allow_tuple failed: %s", e, exc_info=True,
                    )
                continue

            # wh-mvgvt: instrument the suspected stall site. UTT-573/4/5/577
            # showed a consistent 1.0s gap between INPUT_RECEIVED and the
            # inner Starting utterance log. Capture three monotonic markers
            # so a future session reveals whether logger.info itself blocks
            # (handler contention) or some other step is slow.
            t_dispatch_log_before = time.perf_counter()
            if trace_id:
                logger.info("INPUT_RECEIVED action=%s", action)
            t_dispatch_log_after = time.perf_counter()

            if not hasattr(ui_handler, action):
                logger.warning("Unknown action: %s", action)
                continue

            # --- Action Execution and Verification ---
            is_internal_action.set()
            try:
                method_to_call = getattr(ui_handler, action)
                pre_clip_ok, pre_clip_txt = False, ""
                is_ctrl_c = action == "hotkey_action" and isinstance(params.get("keys"), list) and {"ctrl","c"}.issubset({str(k).lower() for k in params["keys"]})

                if request_id and is_ctrl_c:
                    pre_clip_ok, pre_clip_txt = get_text_safe()

                # Actions that emit their own Schema A response need the
                # request_id from the IPC envelope. The envelope's request_id
                # lives outside the params dict, so it must be injected
                # explicitly. Other actions get the response from the generic
                # emitter below and never look at request_id (wh-lla5d).
                if action in _HANDLES_OWN_RESPONSE:
                    method_to_call(request_id=request_id, **params)
                else:
                    method_to_call(**params)
                t_dispatch_done = time.perf_counter()
                if trace_id:
                    logger.info(
                        "DISPATCH_TIMING action=%s status=ok "
                        "log_block_ms=%.1f dispatch_ms=%.1f total_ms=%.1f",
                        action,
                        (t_dispatch_log_after - t_dispatch_log_before) * 1000.0,
                        (t_dispatch_done - t_dispatch_log_after) * 1000.0,
                        (t_dispatch_done - t_dispatch_log_before) * 1000.0,
                    )

                """:flow: UI Action Response
                :step: 1
                :description: General action response generation after execution completes
                :data_in: request_id (from step 2b of UI Action Execution), action completion status
                :data_out: Response dict prepared for enqueue (step 2)
                :branches_to: Step 1a (activate_window path)
                :execution_context: input_proc (GUI process)
                :execution_mode: conditional
                :condition: If request_id present → generate response, else no response needed
                :notes: Response generation for general actions (step 7b path). Parallel to step 1a
                (activate_window path). Checks if original request (send_request step 2b) included
                request_id. If yes, generates response with verification: (1) Clipboard polling for Ctrl+C -
                waits CLIPBOARD_TIMEOUT_MS for clipboard change, (2) Heuristic delay - uses ACTION_DELAY_MS
                timing, (3) Error path - generates error response on exception. Fire-and-forget commands
                (send_command step 2a) have no request_id and skip this flow. All paths converge at step 2
                (response_queue.put).
                """
                if request_id and action not in _HANDLES_OWN_RESPONSE:
                    """:flow: UI Action Response
                    :step: 2
                    :description: Enqueues response dict to multiprocessing Queue for IPC
                    :data_in: Response dict with request_id, status/error, path, action, optional message
                    :data_out: Response in multiprocessing.Queue for consumption by main process
                    :execution_context: input_proc (GUI process)
                    :execution_mode: sync
                    :notes: Convergence point from steps 1 and 1a. Critical IPC boundary returning response
                    from GUI process to logic process. Puts response dict onto response_queue with three
                    possible outcomes: (1) Success with path='clipboard_done' (clipboard verification),
                    (2) Success with path='heuristic_done' (timing fallback), path='foreground_done'
                    (window activation), (3) Error with error=True and message. All responses include
                    request_id for demuxing in step 3. Queue is thread-safe multiprocessing.Queue established
                    during process initialization. After enqueue, GUI process continues to next command while
                    response travels through demuxer (step 3) to original awaiter.

                    Actions listed in _HANDLES_OWN_RESPONSE (wh-lla5d) skip this
                    block entirely; their handlers emit the Schema A response
                    themselves so each request_id yields exactly one response.
                    """
                    done = False
                    if is_ctrl_c:
                        start = time.time()
                        while (time.time() - start) * 1000 < CLIPBOARD_TIMEOUT_MS:
                            ok, now_txt = get_text_safe()
                            if ok and ((pre_clip_ok and now_txt != pre_clip_txt) or (not pre_clip_ok and now_txt)):
                                response_queue.put({'request_id': request_id, 'status': 'ok', 'path': 'clipboard_done', 'action': action})
                                done = True
                                break
                            time.sleep(POLL_INTERVAL_MS / 1000.0)

                    if not done:
                        # Use action-specific delay, or 500ms default for unknown actions
                        ms = ACTION_DELAY_MS.get(action, 500)
                        time.sleep(ms / 1000.0)
                        response_queue.put({'request_id': request_id, 'status': 'ok', 'path': 'heuristic_done', 'action': action})

            except Exception as e:
                # wh-zcx9.4: capture the timing marker BEFORE any logging
                # in this branch so the perf_counter values are not
                # contaminated by the error log (which includes exc_info
                # traceback formatting and may itself be slow). A traced
                # action that blocked for ~1s and then raised will produce
                # a DISPATCH_TIMING status=error line that still answers
                # the same log_block_ms vs dispatch_ms question.
                t_dispatch_failed = time.perf_counter()
                if trace_id:
                    logger.info(
                        "DISPATCH_TIMING action=%s status=error "
                        "log_block_ms=%.1f dispatch_ms=%.1f total_ms=%.1f "
                        "exception=%s",
                        action,
                        (t_dispatch_log_after - t_dispatch_log_before) * 1000.0,
                        (t_dispatch_failed - t_dispatch_log_after) * 1000.0,
                        (t_dispatch_failed - t_dispatch_log_before) * 1000.0,
                        type(e).__name__,
                    )
                err = str(e)
                logger.error("Error executing input action '%s': %s", action, e, exc_info=True)
                if request_id and action not in _HANDLES_OWN_RESPONSE:
                    response_queue.put({'request_id': request_id, 'error': True, 'message': err, 'action': action})
            finally:
                is_internal_action.clear()

    # ============================================================================
    # CLEANUP
    # ============================================================================
    except KeyboardInterrupt:
        logger.info("Input process KeyboardInterrupt.")
    except Exception as e:
        logger.error("Critical error in Input loop: %s", e, exc_info=True)
    finally:
        logger.info("Stopping Input process...")
        if mouse_listener:
            mouse_listener.stop()
            mouse_listener.join()
        if keyboard_listener:
            keyboard_listener.stop()
            keyboard_listener.join()
        if shm:
            shm.close()
        if _fault_file:
            _fault_file.close()
        logger.info("Input process finished.")
