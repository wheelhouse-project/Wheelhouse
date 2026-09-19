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
import inspect
import logging
import math
import multiprocessing
import os
import re
import sys
import time
import threading
from queue import Empty
from typing import TypeGuard
import pickle
import struct
from multiprocessing import shared_memory, Queue
from pynput import mouse, keyboard
import win32gui
import win32process
import psutil

logger = logging.getLogger(__name__)

# The title on every notice this module raises, matching the app_name the
# GUI process already uses for its own toasts.
_NOTICE_TITLE = "Wheelhouse"

# How many program names a "say the full name" notice lists.
_MAX_LISTED_MATCHES = 5

# How many launches may be running at once (wh-launch-off-command-loop,
# boss ruling 2026-09-04, overridable by David). Windows offers no way to
# interrupt a shell call that never returns, so every attempt at a dead
# shortcut leaves one thread running for the life of the process. Without
# this limit a user who repeats the command ten times leaves ten of them.
_MAX_LAUNCHES_IN_FLIGHT = 4

# What the user is told when that limit refuses a launch.
_LAUNCH_BUSY_NOTICE = (
    "Wheelhouse is still starting a program. Try again in a moment."
)

# How long a launch may run before the user is told it is slow
# (wh-launch-off-command-loop, boss ruling 2026-09-04, overridable by
# David). "Starting <name>" is shown only AFTER the shell call returns
# (wh-activate-launch-fallback.1.7 settled that order and it is
# unchanged), so without this a shell call that never returns would say
# nothing at all and the user would hear nothing after asking.
_LAUNCH_SLOW_S = 8.0

# The launches started and not yet finished, as [name, thread] pairs.
# Read and written only through _launches_in_flight and
# _launch_off_command_loop, both of which hold _launch_threads_lock.
_launch_threads = []
_launch_threads_lock = threading.Lock()

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
    # wh-mouse-grid.1.24: the grid channel drain probe replies with the
    # recorded result of the most recent pointer action
    # (last_mouse_action), so Logic can recover a release_failed
    # stuck-button warning whose original reply was discarded after a
    # timeout. The generic emitter's plain heuristic_done reply would
    # drop the record.
    "channel_probe",
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
    # keepalive. Slides the still-retained pinned snapshot's TTL anchor via
    # ElementFinder.refresh_snapshot_ttl (retained, not necessarily visible --
    # wh-overlay-slow-uia-stale-badges.13.6) and emits its own PinSnapshotResponse
    # (reused as the ack). Never-raise and self-owning like pin/unpin; the
    # generic emitter would clobber the ack. Like pin/unpin it is NOT in the
    # command_dequeue_monotonic injection block (a store touch, no walk
    # deadline).
    "refresh_overlay_snapshot",
    # wh-input-mouse-primitives: the mouse-grid pointer actions. Each handler
    # drives one SendInput primitive (click at a point, park the pointer,
    # perform a whole drag) and emits its own MouseActionResponse carrying the
    # outcome + reason tag. The generic emitter's "ok / heuristic_done" reply
    # would clobber that outcome, and its fixed post-action sleep would also
    # be wrong for perform_drag, which already blocks for its own duration.
    # All three are never-raise (an unexpected error maps to
    # execution_failed:unexpected_error).
    "click_point",
    "move_pointer",
    "perform_drag",
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

def _safe_type_name(value: object) -> str:
    """Name a value's class without letting the lookup raise.

    wh-overlay-slow-uia-stale-badges.14.59: a class whose metaclass
    defines __name__ as a raising property makes type(value).__name__
    raise, and the DISPATCH_TIMING error branch passed exactly that
    expression as a logging argument -- arguments are evaluated at the
    call, so it escaped the per-action handler two lines before
    _safe_error_text could contain it.

    wh-overlay-slow-uia-stale-badges.14.61: the same shape reaches the
    malformed-envelope containment code, which names a rejected action
    or params by its type. A class of this shape is globally importable
    and picklable, so it survives the Logic-to-Input round trip. The
    parameter is therefore any object, not only an exception.
    """
    try:
        return type(value).__name__
    except Exception:
        return "<unnamed type>"


def _safe_error_text(e: BaseException) -> str:
    """Render an exception's message without letting the render raise.

    wh-overlay-slow-uia-stale-badges.14.54: every Input error response
    carries the exception's text, and the exception itself can come
    from caller-controlled code -- a poisoned params key raises its OWN
    exception class, and the sender owns that class's __str__ too. A
    raising __str__ then escapes the handler that exists to contain the
    failure, and the escape reaches the reader loop's outer except,
    which stops the only Input consumer. The fall-back names the
    exception's type, and the last resort answers even when the type
    name is a raising property on a custom metaclass.
    """
    try:
        return str(e)
    except Exception:
        return f"<unrenderable {_safe_type_name(e)}>"


def _several_matches_notice(programs) -> str:
    """Word the notice that lists the programs a spoken name could mean.

    crewcut: only the first five names are listed. A Windows toast cuts
    long text off with no sign that anything was removed, so a list of
    twenty names would read as a list of however many happened to fit. To
    remove the limit, show the full list in a window the user can scroll
    rather than in a toast.
    """
    listed = ", ".join(p.name for p in programs[:_MAX_LISTED_MATCHES])
    remaining = len(programs) - _MAX_LISTED_MATCHES
    if remaining > 0:
        listed = f"{listed}, and {remaining} more"
    return f"More than one program matches. Say the full name: {listed}"


def _notice_sender(notify, logger):
    """Wrap a notifier so that a notice which cannot be shown never raises.

    Returns a one-argument function taking the message text. ``notify``
    of None gives a function that does nothing, which is what a caller
    written before the notices existed passes.
    """
    def _notice(message):
        if notify is None:
            return
        try:
            notify(_NOTICE_TITLE, message)
        except Exception as exc:
            logger.warning(f"Notice could not be shown: {exc}")

    return _notice


def _launches_in_flight():
    """Drop the launches that finished and report the ones still running.

    Returns a list of [name, thread] pairs. Called on the command loop
    before a launch starts, and nowhere else.
    """
    with _launch_threads_lock:
        _launch_threads[:] = [
            entry for entry in _launch_threads if entry[1].is_alive()
        ]
        return list(_launch_threads)


def _launch_off_command_loop(name, work, logger, notice, before_start=None):
    """Run one launch on its own thread, so the command loop stays free.

    wh-launch-off-command-loop, David's item 18 of
    QUESTIONS-2026-09-04.md. The Input process runs ONE synchronous
    command loop. os.startfile reaches ShellExecute, which can wait on
    DDE, on a shell extension, or on resolving a shortcut target across
    the network; a Start Menu shortcut with a dead network target is the
    ordinary way in. Called on the loop, such a launch holds every later
    voice command for as long as it takes. _DispatchWatch reports that
    stall and cannot end it, because Windows offers no way to interrupt
    a cross-process call the shell never returns from.

    Returns the thread it started, or None when the in-flight limit
    refused the launch. The caller on the command loop ignores the
    return; the tests use it to wait for the launch they asked for.

    ``before_start`` runs on the command loop AFTER the limit has
    allowed the launch and BEFORE the thread starts. Work that only a
    real launch may do goes there. The .exe caller passes the shadow
    buffer invalidation: a launch the limit refuses never changes which
    window has focus, so it must not cost the next buffer query a full
    UIA re-sync (boss ruling 2026-09-04 on wh-launch-off-command-loop).
    """
    running = _launches_in_flight()
    if len(running) >= _MAX_LAUNCHES_IN_FLIGHT:
        still = ", ".join(entry[0] for entry in running)
        logger.warning(
            f"Not starting {name}: {len(running)} launches have not "
            f"returned yet ({still})"
        )
        notice(_LAUNCH_BUSY_NOTICE)
        return None
    if before_start is not None:
        before_start()

    def _guarded():
        # The launch runs with nothing above it to catch anything, so an
        # unexpected exception here would end only in a bare thread
        # traceback on stderr. Log it where the rest of this process logs.
        try:
            work()
        except Exception as exc:
            logger.error(f"Starting {name} failed: {exc}", exc_info=True)

    thread = threading.Thread(
        target=_guarded, name=f"input-launch-{name}", daemon=True
    )
    thread.start()
    with _launch_threads_lock:
        # Appended AFTER start(), because is_alive() is False until a
        # thread has started and _launches_in_flight would drop it.
        _launch_threads.append([name, thread])
    return thread


class _LaunchName:
    """The name the slow-launch notice uses, which can change mid-launch.

    wh-launch-off-command-loop.2.1, filed by codex. The spoken-name path
    starts its timer before the installed-program lookup, so when the
    timer starts the only name there is is what the user said. Once the
    lookup settles on a program, the notice must name that program
    instead. The timer holds this object and reads it when it fires, so
    one timer and one eight-second bound still cover one spoken command.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


def _slow_launch_timer(name, notice):
    """Build the timer that tells the user a launch is taking too long.

    wh-launch-off-command-loop, boss ruling 2026-09-04. "Starting
    <name>" is shown only after the shell call returns
    (wh-activate-launch-fallback.1.7 settled that order and it is
    unchanged), so a shell call that never returns would otherwise say
    nothing at all and the user would hear nothing after asking. Start
    it before the shell call and cancel it after; it is a daemon, so a
    launch that never comes back cannot hold shutdown.

    One timer covers one spoken command, not one candidate. The notice
    names the program the lookup settled on, so a timer per candidate
    would show the same sentence several times for one thing the user
    said.
    """
    timer = threading.Timer(
        _LAUNCH_SLOW_S,
        lambda: notice(f"{name} is taking a long time to start."),
    )
    timer.daemon = True
    return timer


def _launch_exe_target(target, logger, notice) -> None:
    """Start a bare executable name off the command loop.

    ShellExecute resolves the bare name via System32, PATH, and the App
    Paths registry key. A failure says so, in the same words the
    spoken-name path uses at the end of `_start_named_program`: the user
    asked for a program and it did not come, and failing in silence here
    is the defect class of wh-keyboard-refusal-notice
    (wh-exe-launch-notice, David's item 13).

    The name in the notice is the spoken target, which for this path is
    the executable name itself. The spoken-name path can say something
    better because its lookup settled on an installed program; this one
    has only the words the user said.
    """
    timer = _slow_launch_timer(target, notice)
    timer.start()
    try:
        os.startfile(target)
    except OSError as e:
        logger.warning(f"Could not launch {target}: {e}")
        notice(f"Could not start {target}.")
    finally:
        timer.cancel()


def _start_named_program(target, logger, notify, find_programs,
                         buffer_manager) -> None:
    """Start the installed program a spoken name refers to.

    wh-activate-launch-fallback, David's items 32 and 43. A spoken name
    that is not an executable used to end here doing nothing at all,
    because a title pattern has nothing to execute. Now the name is
    looked up among the installed programs: exactly one match starts,
    several start nothing and are listed so the user can say the full
    name, and none says so instead of failing in silence.
    """
    _notice = _notice_sender(notify, logger)

    # One timer for the whole spoken command, started BEFORE the lookup
    # rather than after it (wh-launch-off-command-loop.2.1, filed by
    # codex). The lookup walks the Start Menu folders and reads the
    # registry on every call, and %APPDATA% is a network path on a
    # roaming profile, so a lookup that blocks is one of the things this
    # whole function was moved off the command loop for. Started after
    # the lookup, the timer measured only the shell call, and a lookup
    # that never returned said nothing at all.
    #
    # The name is held in an object the timer reads when it fires, so
    # eight seconds still cover one spoken command exactly once.
    # Cancelling and restarting the timer once the name is known would
    # hand the shell call its own fresh eight seconds instead.
    name = _LaunchName(target)
    slow = _slow_launch_timer(name, _notice)
    slow.start()
    try:
        if find_programs is None:
            # Imported here, not at module scope, because every
            # project-local import in this file is deferred to the
            # process that uses it.
            from utils.installed_programs import find_installed_programs
            find_programs = find_installed_programs

        programs = find_programs(target)

        if not programs:
            logger.warning(f"No window found matching: {target}")
            _notice(f"No program matched {target}.")
            return

        if len(programs) > 1:
            logger.info(
                f"No window found for {target}; "
                f"{len(programs)} programs match, starting none"
            )
            _notice(_several_matches_notice(programs))
            return

        program = programs[0]
        # The lookup settled on a program, so the notice names that
        # program from here on. Before this point the words the user
        # said are the only name there is.
        name.name = program.name
        logger.info(f"No window found for {target}; starting {program.name}")
        # The winner first, then the entries that yielded to it, in
        # source order (wh-activate-launch-fallback.1.6, ruled by the
        # boss 2026-09-04 and overridable by David). The name was
        # claimed on the existence of a shortcut, and a shortcut whose
        # target was uninstalled is still a shortcut, so the ruled
        # winner can be the one entry of the several that cannot start.
        # Every candidate carries the name the user spoke, so none of
        # them is a different program.
        #
        # OSError only. The shell can put its own dialog on screen for a
        # broken shortcut and still return success; nothing here can see
        # that, so nothing here pretends to.
        for candidate in (program, *program.fallbacks):
            try:
                os.startfile(candidate.launch_target)
            except OSError as e:
                logger.warning(
                    f"Could not start {candidate.launch_target}: {e}"
                )
                continue
            # Cancelled here rather than only in the finally below, so a
            # launch that returned cannot be called slow by a timer that
            # fires between the return and the notice.
            slow.cancel()
            # wh-review-pattern-fixes.8: the started program will take
            # focus, the same reason the activation and .exe branches
            # invalidate. AFTER the launch, not before: an invalidated
            # buffer cannot be restored -- ShadowBufferManager.invalidate
            # clears the text, the cursor and the selection, and only a
            # fresh UIA synchronize brings them back -- so a launch that
            # failed used to cost the user the buffer for a program that
            # never appeared (wh-activate-launch-fallback.1.7).
            #
            # wh-launch-off-command-loop: this now runs on the launch
            # thread, after the command loop cleared is_internal_action,
            # so the listeners are no longer suppressed across it. Only
            # the safe direction is reachable. A listener that
            # re-synchronizes from the OLD window in that gap leaves the
            # buffer invalid, which is what this call wants anyway; a
            # stale buffer cannot survive the launch, because this call
            # is the last thing before the notice.
            if buffer_manager is not None:
                buffer_manager.invalidate()
            _notice(f"Starting {program.name}")
            return
        # The user asked for a program and it did not come. Failing in
        # silence here is the defect class of wh-keyboard-refusal-notice,
        # so say which program would not start -- once, under the name
        # the lookup settled on, however many candidates were behind it.
        _notice(f"Could not start {program.name}.")
    finally:
        slow.cancel()


def _handle_activate_window(params: dict, request_id, response_queue, action: str,
                            is_internal_action: threading.Event,
                            foreground_poll_ms: int, poll_interval_ms: int,
                            action_delay_ms: dict, logger,
                            buffer_manager=None, notify=None,
                            find_programs=None):
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
        buffer_manager: The UIActionHandler's ShadowBufferManager, so the
            activation special case (which bypasses UIActionHandler) can
            invalidate the shadow buffer before it changes foreground
            focus (wh-review-pattern-fixes.8). None skips invalidation
            (legacy callers).

    Returns the launch thread it started, or None when it started none
    (wh-launch-off-command-loop). The command loop ignores this: waiting
    for the launch is the whole thing this change removes. It exists so
    that a test can wait for the launch it asked for.
    """
    _notice = _notice_sender(notify, logger)
    launch_thread = None
    is_internal_action.set()
    try:
        # wh-overlay-slow-uia-stale-badges.14.40: params VALUES are
        # untrusted -- the truth test and .lower() below must run
        # inside this try so a malformed target is answered with the
        # standard error response instead of escaping to the outer
        # Input-loop handler.
        target = params.get("target") or ""
        is_process = target.lower().endswith(".exe")
        logger.info(f"Activating window: target={target} ({'process' if is_process else 'title'})")

        # Find the target window
        target_hwnd = _find_window_by_target(target, logger)
        
        if target_hwnd:
            # wh-review-pattern-fixes.8: activation changes foreground
            # focus, and the listeners suppress invalidation during
            # internal actions; invalidate before the dispatch (see the
            # hotkey_action block comment in ui_action_handler).
            if buffer_manager is not None:
                buffer_manager.invalidate()
            # Activate the window
            _activate_window_impl(target_hwnd, logger)
        elif is_process:
            # Launch fallback (wh-activate-launch-fallback): the app has no
            # window, so start it. os.startfile -> ShellExecute resolves the
            # bare exe name via System32, PATH, and the App Paths registry
            # key. Only .exe targets are launchable; a title regex has
            # nothing to execute.
            #
            # wh-launch-off-command-loop: the shell call runs on its own
            # thread. ShellExecute can wait on DDE, on a shell extension,
            # or on resolving a target across the network, and called
            # here it would hold every later voice command.
            logger.info(f"No window found for {target}; launching it")
            # wh-review-pattern-fixes.8: the launched app will take focus;
            # same invalidation as the activation branch above. It still
            # runs on the command loop, before the launch thread starts,
            # but only once the in-flight limit has allowed the launch
            # (boss ruling 2026-09-04): a refused launch starts nothing,
            # so it must not cost the buffer a re-sync.
            launch_thread = _launch_off_command_loop(
                target,
                lambda: _launch_exe_target(target, logger, _notice),
                logger,
                _notice,
                before_start=(
                    None if buffer_manager is None
                    else buffer_manager.invalidate
                ),
            )
        else:
            # The spoken words are not an executable, so there is nothing
            # to run directly. Look them up among the installed programs
            # (wh-activate-launch-fallback).
            #
            # wh-launch-off-command-loop, boss ruling 2026-09-04: the
            # WHOLE of _start_named_program runs on the launch thread,
            # the lookup included. find_installed_programs walks the
            # Start Menu folders and reads the registry on every lookup
            # with no cache (the crewcut in utils/installed_programs.py),
            # and %APPDATA% is a network path on a roaming profile, so
            # the lookup carries the same exposure as the launch.
            launch_thread = _launch_off_command_loop(
                target,
                lambda: _start_named_program(
                    target, logger, notify, find_programs, buffer_manager
                ),
                logger,
                _notice,
            )

        # Verification polling (if request_id present)
        if request_id:
            _verify_window_activation(target, request_id, response_queue, action,
                                     foreground_poll_ms, poll_interval_ms, 
                                     action_delay_ms.get(action, 500), logger)  # 500ms default for unknown actions
    
    except Exception as e:
        # .14.54: the render is guarded -- see _safe_error_text.
        err = _safe_error_text(e)
        logger.error(f"Error in activate_window: {err}", exc_info=True)
        if request_id:
            response_queue.put({'request_id': request_id, 'error': True,
                              'message': err, 'action': action})
    finally:
        is_internal_action.clear()
    return launch_thread

def _handle_add_soft_allow_tuple(params, request_id, response_queue, ui_handler):
    """Apply a runtime soft-allow grant and acknowledge it when requested.

    wh-9weum Phase 3 (wh-01t75): Logic has already written the disk file;
    this command updates the input-process predicate's in-memory set so the
    next evaluate call sees the new tuple without restart.

    wh-overlay-slow-uia-stale-badges.14.14: when the payload carries a
    request_id, the outcome is reported back on response_queue. Queue
    acceptance in Logic is not delivery, and delivery is not application --
    without this ack, a sender-side drop after acceptance let add_soft_allow
    report SUCCESS (and reset the click counter) while the running session
    never received the grant.
    """
    try:
        # wh-overlay-slow-uia-stale-badges.14.40: params VALUES are
        # untrusted -- the truth tests and the {!r} renders in the
        # missing-field message below must run inside this try so a
        # raising __bool__ or __repr__ is answered with the standard
        # error response instead of escaping to the outer Input-loop
        # handler.
        process_name = params.get("process_name", "")
        class_name = params.get("class_name", "")
        control_type = params.get("control_type", "")
        if not (process_name and class_name and control_type):
            msg = (
                "add_soft_allow_tuple: missing required field "
                f"(process_name={process_name!r} class_name={class_name!r} "
                f"control_type={control_type!r})"
            )
            logger.warning(msg)
            if request_id:
                response_queue.put({
                    'request_id': request_id, 'error': True,
                    'message': msg, 'action': 'add_soft_allow_tuple',
                })
            return
        ui_handler.text_target_predicate.add_soft_allow(
            (process_name, class_name, control_type),
        )
    except Exception as e:
        err = _safe_error_text(e)  # .14.54
        logger.error("add_soft_allow_tuple failed: %s", err, exc_info=True)
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': err, 'action': 'add_soft_allow_tuple',
            })
        return
    logger.info(
        "add_soft_allow_tuple: predicate updated -- "
        "process=%s class=%s control_type=%s",
        process_name, class_name, control_type,
    )
    if request_id:
        response_queue.put({
            'request_id': request_id, 'status': 'ok',
            'action': 'add_soft_allow_tuple',
        })


def _read_envelope(command_message):
    """Extract the fields of an untrusted unpickled envelope safely.

    wh-overlay-slow-uia-stale-badges.14.38: an unpickled value can
    carry subclass behavior that raises from operations the reader
    loop runs OUTSIDE any per-action try -- a dict subclass's .get, a
    poisoned dict key whose __eq__ raises inside the lookup's rich
    comparison, a str subclass request_id whose truth test or hash
    raises later. Any such escape reaches the outer Input-loop handler
    and shuts the process down. This is the single extraction
    chokepoint: it requires an EXACT dict envelope, canonicalizes
    request_id (non-exact-str becomes None -- an untrustworthy id
    cannot be answered) and trace_id (non-exact-str becomes ""), and
    runs every extraction under one protective except. Warning
    messages render type names only, never the untrusted value.

    wh-overlay-slow-uia-stale-badges.14.22 (carried forward from the
    removed _extract_params): only an ABSENT params key defaults to
    {}; every supplied value travels onward for _coerce_params
    validation, falsy or not.

    Returns (action, raw_params, has_params, request_id, trace_id), or
    None when the envelope is malformed and must be dropped.
    """
    try:
        if type(command_message) is not dict:
            logger.warning(
                "command envelope must be a dict, got %s; dropping",
                type(command_message).__name__,
            )
            return None
        action = command_message.get("action")
        request_id = command_message.get("request_id")
        if type(request_id) is not str:
            request_id = None
        trace_id = command_message.get("trace_id", "")
        if type(trace_id) is not str:
            trace_id = ""
        if "params" in command_message:
            return action, command_message["params"], True, request_id, trace_id
        return action, {}, False, request_id, trace_id
    except Exception:
        logger.warning(
            "command envelope extraction failed; dropping", exc_info=True,
        )
        return None


def _handle_terminal_editor_cancelled(params, request_id, response_queue, ui_handler):
    """Apply a GUI-originated editor cancellation and acknowledge it.

    wh-overlay-slow-uia-stale-badges.14.17: the params request_id names
    the editor session the GUI cancelled; the proxy ignores a
    cancellation from a session that is no longer active (empty rid =
    unconditional recovery).

    wh-overlay-slow-uia-stale-badges.14.24: when the IPC envelope
    carries a request_id, the outcome is reported on response_queue --
    queue acceptance in Logic is not delivery, and without this ack a
    post-acceptance drop silently lost the cleanup and left the proxy
    wedged active. Same contract as _handle_add_soft_allow_tuple.
    """
    try:
        ui_handler.terminal_editor_cancelled(params.get("request_id", ""))
    except Exception as e:
        err = _safe_error_text(e)  # .14.54
        logger.error(
            "terminal_editor_cancelled failed: %s", err, exc_info=True,
        )
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': err, 'action': 'terminal_editor_cancelled',
            })
        return
    if request_id:
        response_queue.put({
            'request_id': request_id, 'status': 'ok',
            'action': 'terminal_editor_cancelled',
        })


def _handle_te_event_ack_command(params, request_id, response_queue, ui_handler):
    """Forward a GUI lifecycle ack to the proxy and acknowledge it.

    wh-t81d9.2: the proxy's on_event_ack clears submit-in-progress
    state on submit_complete/submit_failed:* and records the editor
    HWND on show acks. Acks whose session request_id does not name the
    proxy's active session are ignored inside on_event_ack
    (wh-overlay-slow-uia-stale-badges.14.17).

    wh-overlay-slow-uia-stale-badges.14.24: the envelope request_id
    (distinct from the SESSION request_id inside params) is acknowledged
    on response_queue so Logic's bounded retry can observe delivery.
    """
    try:
        # wh-overlay-slow-uia-stale-badges.14.40: params VALUES are
        # untrusted -- the ``or None`` truth test below must run inside
        # this try so a raising __bool__ is answered with the standard
        # error response instead of escaping to the outer Input-loop
        # handler.
        rid = params.get("request_id", "")
        op = params.get("op", "")
        editor_hwnd = params.get("editor_hwnd", 0) or None
        ui_handler.terminal_editor.on_event_ack(rid, op, editor_hwnd)
    except Exception as e:
        err = _safe_error_text(e)  # .14.54
        logger.error("Error in _te_event_ack: %s", err, exc_info=True)
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': err, 'action': '_te_event_ack',
            })
        return
    if request_id:
        response_queue.put({
            'request_id': request_id, 'status': 'ok',
            'action': '_te_event_ack',
        })


def _handle_set_log_level(params, request_id, response_queue):
    """Apply a runtime log-level change without acknowledging success.

    wh-overlay-slow-uia-stale-badges.14.40: this route lived inline in
    the reader loop with getattr(logging, new_level, ...) outside any
    try, so a non-string level VALUE raised TypeError straight into
    the outer Input-loop handler -- _coerce_params proves only the
    params CONTAINER. The route keeps its historical fire-and-forget
    shape: no ack on success, even when the envelope carries a
    request_id. A malformed level is contained here, with the
    standard error response when the sender awaits one.
    """
    try:
        new_level = params.get("level", "INFO")
        logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))
        logger.info("Logging level set to %s", new_level)
    except Exception as e:
        # wh-overlay-slow-uia-stale-badges.14.42: never read params
        # again here -- the failed lookup may have raised from a
        # poisoned key's __eq__, and a second lookup raises the same
        # way and escapes this handler.
        err = _safe_error_text(e)  # .14.54
        logger.warning("set_log_level failed: %s", err)
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': err, 'action': 'set_log_level',
            })


def _coerce_params(params, request_id, response_queue, action):
    """Validate that a command's params is a mapping before dispatch.

    wh-overlay-slow-uia-stale-badges.14.20: the reader loop's
    `command_message.get("params", {}) or {}` replaces only FALSY
    non-mapping values. A truthy non-mapping (a list, a string) used to
    flow to the dispatch sites, where `params.get(...)` raised
    AttributeError inside the reader loop's outer except -- which kills
    the whole Input process. Reject it here instead: log a warning, send
    the standard error response when the sender awaits one, and let the
    reader loop continue with the next command.

    wh-overlay-slow-uia-stale-badges.14.38: only an EXACT dict passes.
    A dict subclass can override get() to raise at the special routes
    outside any per-action try, and the reject message must not render
    the untrusted value -- a raising __repr__ would otherwise blow up
    this reject path itself.

    Returns params unchanged when it is an exact dict, else None.
    """
    if type(params) is dict:
        return params
    # .14.61: a metaclass whose __name__ raises would otherwise escape
    # this reject path and end the reader loop, which is the exact
    # failure the function exists to contain.
    msg = (
        f"{action}: params must be a plain dict, got "
        f"{_safe_type_name(params)}"
    )
    logger.warning(msg)
    if request_id:
        response_queue.put({
            'request_id': request_id, 'error': True,
            'message': msg, 'action': action,
        })
    return None


def _enrich_walk_params(params, command_dequeue_monotonic, action,
                        request_id, response_queue):
    """Add the dequeue timestamp to a UIA-walk command's params.

    wh-overlay-slow-uia-stale-badges.14.48: the reader loop built this
    enriched copy with a dict expansion BEFORE the per-action try. The
    container is an exact dict by then (_coerce_params), but its KEYS
    are still untrusted: a picklable str subclass key whose value and
    hash match "command_dequeue_monotonic" and whose equality raises
    makes the insert raise, and that lands in the loop's outer except,
    which stops the only Input consumer. There is no Input respawn
    path, so voice control does not recover.

    Returns the enriched params, or None when the copy cannot be built
    (warning logged, standard error response when the sender awaits
    one).
    """
    try:
        return {
            **params,
            "command_dequeue_monotonic": command_dequeue_monotonic,
        }
    except Exception as e:
        # .14.54: the render is guarded -- see _safe_error_text.
        msg = (
            f"{action}: params could not carry the dequeue timestamp: "
            f"{_safe_error_text(e)}"
        )
        logger.warning(msg)
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': msg, 'action': action,
            })
        return None


def _reject_unbindable_self_owning(method, action, request_id, params,
                                   response_queue) -> bool:
    """Answer a self-owning call whose arguments cannot bind.

    wh-overlay-slow-uia-stale-badges.14.45: an exact dict passes
    _coerce_params yet can still fail Python's argument binding at the
    dispatch call -- a non-string key raises at ** expansion, a params
    key can duplicate the envelope-injected request_id, and a required
    argument can be missing. The dispatch except suppresses the
    standard error response for _HANDLES_OWN_RESPONSE actions (the
    handler owns its response), so the awaited Future timed out. Probe
    the binding first: a failure here is answered with exactly one
    standard error response and no call -- there is no double-ack risk
    because the handler never started.

    Returns True when the call cannot bind (error answered, skip the
    call); False when binding succeeds or the method cannot be
    introspected (keep the existing call behavior).
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    try:
        sig.bind(request_id=request_id, **params)
        return False
    except Exception as e:
        # .14.47: not only TypeError -- a poisoned params key whose
        # value equals a declared parameter name (or request_id) makes
        # the binding raise the stored key's own exception. Letting it
        # escape reaches the dispatch except, which suppresses the
        # response for these actions, and the request times out.
        err = _safe_error_text(e)  # .14.54
        logger.warning(
            "%s: request rejected before the handler started: %s",
            action, err,
        )
        if request_id:
            response_queue.put({
                'request_id': request_id, 'error': True,
                'message': err, 'action': action,
            })
        return True


def _validate_action(action, request_id, response_queue) -> TypeGuard[str]:
    """Validate that a command's action is a string before any dispatch.

    wh-overlay-slow-uia-stale-badges.14.33: the reader loop calls
    hasattr(ui_handler, action) outside the action-execution try block,
    and hasattr raises TypeError for a None or non-string attribute
    name -- the outer handler then shuts the Input process down over
    one malformed envelope. String-ness is the only check here: an
    unknown string action keeps the existing warning-and-continue and
    unanswered-request-timeout behavior at the hasattr branch. Same
    boundary pattern as _coerce_params: log a warning, send the
    standard error response when the sender awaits one, and let the
    reader loop continue with the next command.

    wh-overlay-slow-uia-stale-badges.14.38: only an EXACT str passes.
    A str subclass passes isinstance, but its broken hash raises from
    hasattr(ui_handler, action) (the handler has an instance __dict__)
    and from the frozenset membership sites, and a raising __eq__
    propagates from the special-route comparisons -- all outside the
    per-action try. The reject message renders the type name only,
    never the untrusted value: a raising __repr__ or __str__ would
    otherwise blow up this reject path itself.

    Returns True when action is an exact str, else False.
    """
    if type(action) is str:
        return True
    # .14.61: same containment reason as _coerce_params -- the type name
    # is read through the safe helper so a raising metaclass cannot end
    # the reader loop from inside the reject path.
    type_name = _safe_type_name(action)
    msg = (
        f"command action must be a plain string, got "
        f"{type_name}"
    )
    logger.warning(msg)
    if request_id:
        response_queue.put({
            'request_id': request_id, 'error': True,
            'message': msg, 'action': type_name,
        })
    return False


# wh-overlay-slow-uia-stale-badges.11: the dispatch watchdog's timings.
# Read on every poll rather than captured once, so a test can shorten
# them without restarting the loop.
#
# _DISPATCH_STALL_REPORT_S is the limit for a command that arrives with no
# awaited window on it. It is longer than the 5.0s response timeout app.py
# applies to the ordinary command round trip (the InputProcessManager
# ``response_timeout_s`` default). A dispatch that merely runs long is not
# news; the watchdog exists for the case where the Logic side has already
# given up and the loop is STILL held, which is the state no existing log
# records.
#
# wh-watchdog-stall-window: that default is NOT the bound for the two
# handlers most likely to hold this loop, so those two no longer answer to
# the fixed limit. app.py stamps the window it really awaits on the envelope
# of ``click_element`` (awaited for ``[click] response_timeout_ms``, an int
# validated in [100, 10000]) and of ``start_overlay_walk`` (awaited for
# ``[click] screen_read_timeout_ms``, shipped 10000 and validated in
# [100, 60000]; the post-click settle re-read sends that same action). A
# stamped command is reported only once it has held the loop for its own
# window plus _DISPATCH_STALL_WINDOW_MARGIN_S, so raising either config key
# -- the documented remedy for a slow provider -- moves the stall limit with
# it, and a healthy slow read is no longer reported as a stall it was never
# inside. Reported by deepseek round 1 as
# wh-overlay-slow-uia-stale-badges.11.1.4.
#
# The margin is slack, not a second window. The window is charged from this
# process's DEQUEUE instant, which is later than the moment the Logic side
# started waiting, so a dispatch that reaches window + margin has certainly
# outlived the awaiter that is still nominally holding it.
_DISPATCH_STALL_REPORT_S = 6.0
_DISPATCH_STALL_WINDOW_MARGIN_S = 1.0
_DISPATCH_STALL_REPEAT_S = 10.0
_WATCHDOG_POLL_S = 0.25


# The longest interval ``threading.Event.wait`` accepts. Above it the wait
# raises OverflowError instead of waiting, which in _run_dispatch_watchdog
# would end the watchdog thread. Read from threading rather than written out,
# because the limit belongs to the platform, not to this module.
_MAX_WAIT_S = threading.TIMEOUT_MAX


def _watchdog_seconds(value, fallback):
    """Return ``value`` as a plain float of seconds, or ``fallback``.

    All three watchdog timings are module globals read at use, so any of them
    can hold a value this module never chose. The failure directions are not
    symmetrical, and none of them is acceptable:

    * A non-numeric limit raises inside ``poll`` on every call. The wrapper in
      ``_run_dispatch_watchdog`` catches it, so the thread survives and the
      watchdog reports NOTHING for the rest of the run -- the same silent
      freeze this bead exists to remove, produced by the watchdog itself.
    * NaN, zero or a negative limit fails every ``<`` comparison, so ordinary
      work is reported as a stall on every poll. A watchdog that cries on
      every command teaches its readers to ignore it.
    * A number the platform cannot wait on is the worst of the three, because
      it is not contained anywhere. ``_run_dispatch_watchdog`` reads the poll
      interval and waits on it OUTSIDE the try that wraps ``poll``, so
      ``Event.wait`` raising there ends the watchdog thread for the rest of
      the run. Two ordinary-looking numbers do it: ``10 ** 400`` is an int too
      large to convert to a float, so ``math.isfinite`` raises on it, and any
      value above ``threading.TIMEOUT_MAX`` (4294967.0 on this platform)
      makes ``wait`` raise ``OverflowError``.

    Bools are rejected before the numeric check because ``True`` would
    otherwise read as a 1.0-second limit.

    The return is always a plain ``float``, never the caller's own object.
    A ``float`` subclass passes every ``isinstance`` check and can still
    raise from the comparison that consumes it, which would move the failure
    somewhere with no fallback to take; converting here keeps the whole
    problem inside the one function equipped to answer it.

    That conversion catches ``Exception`` rather than a list of expected
    types. ``isinstance`` admits subclasses, so ``float(value)`` runs the
    value's own ``__float__``, and nothing constrains what that may raise --
    a named list of three types left every other one to escape the single
    function whose whole purpose is to choose the fallback. ``Exception`` and
    not ``BaseException``, so KeyboardInterrupt and SystemExit still travel.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        seconds = float(value)
    except Exception:
        return fallback
    if not math.isfinite(seconds) or seconds <= 0 or seconds > _MAX_WAIT_S:
        return fallback
    return seconds


class _DispatchWatch:
    """Reports a dispatch that has held the single command loop too long.

    wh-overlay-slow-uia-stale-badges.11. The Input process runs ONE
    synchronous command loop. A handler waiting on a provider that never
    answers holds that loop, and the freeze leaves no record at all: the
    process has not crashed, is not shutting down, and logs nothing, so
    nothing in the log distinguishes "held inside one handler" from
    "idle". This watch is what makes that state visible.

    It REPORTS ONLY, on purpose, for two separate reasons.

    It does not answer the stalled request. The wh-lla5d contract is
    exactly one response per request_id; a watchdog answer followed by
    the handler's own eventual response would be two, and the demuxer
    settles the caller's future on whichever arrives first. The Logic
    process already times out on its own and already handles a late
    answer.

    It also cannot end the stall. Windows offers no way to interrupt a
    cross-process call that a provider never returns from, so the
    handler holds the loop until it returns either way. This makes the
    freeze visible; it does not shorten it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Orders the two log lines of ONE dispatch against each other, and
        # nothing else. poll() decides a stall under _lock and end() closes
        # it under _lock, but both write their line after releasing _lock, so
        # without this the recovery line can be written first and the log's
        # last word about an action that already returned says the loop is
        # still wedged (codex round 3, finding .11.2.7).
        #
        # It is held ACROSS the logger call on purpose -- that is the whole
        # point -- and taken OUTSIDE _lock on both paths, so the acquisition
        # order is the same in both methods and neither can wait on the
        # other. The command loop calls end() and can therefore wait here for
        # one emission. In the Input process that emission is a put_nowait on
        # the bounded queue in utils/queue_logging.py, which drops rather
        # than blocks when full and does no formatting on the calling thread,
        # so the wait is a queue put and never disk I/O.
        self._report_lock = threading.Lock()
        # [action, request_id, trace_id, started_monotonic, reported_at,
        # awaited_window_s] or None when no dispatch is in flight.
        self._current = None

    def begin(
        self, action, request_id, trace_id, started_monotonic,
        awaited_window_s=None,
    ):
        """Register the dispatch that is about to run.

        ``awaited_window_s`` is the Logic-side awaited window this command
        was stamped with (wh-watchdog-stall-window), already validated by
        _read_awaited_window, or None for a command that carries no stamp.
        It defaults to None so a direct unit test of this class -- and any
        caller written before the stamp existed -- still registers a
        dispatch that the fixed limit answers for.
        """
        with self._lock:
            self._current = [
                action, request_id, trace_id, started_monotonic, None,
                awaited_window_s,
            ]

    def end(self):
        """Close the current dispatch, reporting recovery if it was stalled."""
        with self._report_lock:
            with self._lock:
                current = self._current
                self._current = None
            if current is None or current[4] is None:
                # Either nothing was in flight, or it finished before the
                # stall limit. A watchdog that also announced its quiet
                # successes would bury the one line that matters.
                return
            (
                action, request_id, trace_id, started, _reported_at,
                _awaited_window_s,
            ) = current
            logger.error(
                "Input command loop recovered: %s finally returned after "
                "holding the single command loop for %.1fs. trace_id=%s "
                "request_id=%s",
                action, time.monotonic() - started, trace_id or "-",
                request_id or "-",
            )

    def poll(self):
        """Report the in-flight dispatch if it has passed the stall limit.

        The two limits are read from the module globals on every call
        rather than captured once, so a test can shorten them without
        restarting the loop. Nothing in production changes them; both go
        through _watchdog_seconds anyway, because a global read at use can
        hold a value this module never chose.
        """
        now = time.monotonic()
        report_s = _watchdog_seconds(_DISPATCH_STALL_REPORT_S, 6.0)
        repeat_s = _watchdog_seconds(_DISPATCH_STALL_REPEAT_S, 10.0)
        with self._report_lock:
            with self._lock:
                current = self._current
                if current is None:
                    return
                (
                    action, request_id, trace_id, started, reported_at,
                    awaited_window_s,
                ) = current
                # wh-watchdog-stall-window: a stamped command answers to its
                # own awaited window instead of the fixed limit. Read here,
                # under the lock that already holds the dispatch, and from
                # the same module global as the other timings so a test can
                # shorten it without restarting the loop.
                if awaited_window_s is not None:
                    report_s = awaited_window_s + _watchdog_seconds(
                        _DISPATCH_STALL_WINDOW_MARGIN_S, 1.0,
                    )
                held_s = now - started
                if held_s < report_s:
                    return
                if reported_at is not None and now - reported_at < repeat_s:
                    return
                current[4] = now
            logger.error(
                "Input command loop stalled inside a handler: %s has held "
                "the single command loop for %.1fs, past its %.1fs limit, so "
                "every command behind it is waiting. The loop cannot "
                "interrupt the call; this report exists so the freeze is not "
                "silent. trace_id=%s request_id=%s",
                action, held_s, report_s, trace_id or "-", request_id or "-",
            )


def _run_dispatch_watchdog(watch, shutdown_event):
    """Poll one _DispatchWatch until shutdown.

    Every poll is wrapped: a watchdog that raised would take its own
    thread down and leave the loop unwatched for the rest of the run,
    which is the opposite of what it is here for.
    """
    while not shutdown_event.is_set():
        try:
            watch.poll()
        except Exception:
            logger.warning(
                "dispatch watchdog poll failed; the loop is unaffected",
                exc_info=True,
            )
        poll_s = _watchdog_seconds(_WATCHDOG_POLL_S, 0.25)
        if shutdown_event.wait(timeout=poll_s):
            break


def _read_delivery_deadline(command_message):
    """Read the delivery deadline out of an untrusted envelope.

    wh-overlay-slow-uia-stale-badges.11. The Logic process stamps
    ``_delivery_deadline_monotonic`` on the payload BEFORE the frame is
    frozen (app.py stamps it, then serializes that same payload, and
    the sender writes those frozen bytes), so the value is already in
    the bytes this process receives. Nothing here changes the wire
    format; this only reads a field that was already arriving.

    Every lookup runs under one protective except, for the reason
    _read_envelope documents (.14.38): this code runs OUTSIDE the
    per-action try, so a poisoned key or a subclass whose comparison
    raises would otherwise end the Input process over one bad envelope.

    Returns the deadline as a float, or None when the envelope carries
    no usable deadline. None means "do not judge this command's age",
    never "expired": a missing or malformed value must not turn into a
    silent refusal of work the sender expects to run.
    """
    try:
        if type(command_message) is not dict:
            return None
        value = command_message.get("_delivery_deadline_monotonic")
        # bool is a subclass of int, and True would otherwise read as a
        # deadline of 1.0 -- long past, so every command carrying it
        # would expire. The Logic side rejects the same shapes before
        # sending (app.py's invalid-deadline drop).
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        deadline = float(value)
        if not math.isfinite(deadline):
            return None
        return deadline
    except Exception:
        logger.warning(
            "delivery deadline could not be read; the command will be "
            "dispatched without an age check", exc_info=True,
        )
        return None


def _read_awaited_window(command_message):
    """Read the Logic-side awaited window out of an untrusted envelope.

    wh-watchdog-stall-window. app.py stamps ``_awaited_window_s`` on the
    payload of the two handlers that hold this loop longest -- the click and
    the overlay walk -- before the frame is frozen, so the value is already
    in the bytes this process receives. Nothing here changes the wire format.

    The whole lookup runs under one protective except for the reason
    _read_envelope documents (.14.38): this runs OUTSIDE the per-action try,
    so a poisoned key or a subclass whose comparison raises would otherwise
    end the Input process over one bad envelope.

    The numeric judgement is _watchdog_seconds', not a second copy of it:
    the value ends up in the same comparison as the other watchdog timings,
    so it has to reject the same shapes -- bool, non-numeric, NaN, zero or
    negative, and anything above the platform wait limit -- and be returned
    as a plain float rather than as the caller's own object.

    Returns the window in seconds, or None when the envelope carries no
    usable one. None means "judge this dispatch by the fixed limit", which
    is what every unstamped command has always been judged by; it never
    means "report immediately".
    """
    try:
        if type(command_message) is not dict:
            return None
        return _watchdog_seconds(
            command_message.get("_awaited_window_s"), None,
        )
    except Exception:
        logger.warning(
            "awaited window could not be read; the dispatch will be watched "
            "against the fixed stall limit", exc_info=True,
        )
        return None


def _command_expired(now_monotonic, deadline_monotonic) -> bool:
    """True when this command's delivery deadline passed before it was read.

    ``now_monotonic`` is the loop's own dequeue instant, not a fresh
    reading: that is the moment this process took ownership of the
    command, and it is the same anchor the walk handlers already charge
    their budget from.

    The comparison is valid across the two processes. time.monotonic()
    resolves to GetTickCount64() here, which counts system uptime and is
    therefore shared by every process on the machine, verified by
    measurement rather than assumed.
    """
    if deadline_monotonic is None:
        return False
    return now_monotonic >= deadline_monotonic


# wh-voice-access-parity.2.3.3: the actions an expired command still runs.
# Only the continuous-scroll stop is here, and adding anything else needs the
# same three properties: the act is idempotent, it is still correct however
# late it arrives, and NOT doing it leaves the machine acting on its own. A
# stop has all three -- the wheel is turning, the user has already spent their
# stop word, and stopping a scroll that already stopped changes nothing.
_STOPS_EVEN_WHEN_EXPIRED = frozenset({"stop_continuous_scroll"})


def _run_expired_stop(action, ui_handler) -> None:
    """Carry out an expired stop command before the drop is reported.

    The containment drops a command whose delivery deadline passed while this
    loop was blocked, because it would otherwise act on a screen that has
    changed since (wh-overlay-slow-uia-stale-badges.11). A stop is the one
    command where the drop causes the damage instead of preventing it: the
    wheel keeps turning and the user's stop word is already spent.

    Wrapped in its own except for the reason the loop's other pre-dispatch
    helpers are: this runs OUTSIDE the per-action try, so a raising handler
    would end the Input process over one dropped command. The expiry report
    still follows either way, so the drop is never silent.
    """
    if action not in _STOPS_EVEN_WHEN_EXPIRED:
        return
    try:
        getattr(ui_handler, action)()
        logger.info(
            "Expired command %s was carried out anyway before the drop was "
            "reported: stopping is safe however late it arrives, and "
            "dropping it would leave the scroll running.", action,
        )
    except Exception:
        logger.error(
            "The expired %s command could not be carried out; the scroll may "
            "still be running", action, exc_info=True,
        )


def _report_expired_command(
    action, request_id, trace_id, deadline_monotonic,
    now_monotonic, response_queue,
) -> None:
    """Record an expired command and answer whoever is waiting for it.

    The bead's acceptance requires the drop to be logged and reported,
    not silent. Before this existed the loop ran the stale command and
    answered ``status: ok``, so the Logic process was told a command it
    had already given up on had succeeded.
    """
    overdue_s = now_monotonic - deadline_monotonic
    logger.error(
        "Dropped expired command before dispatch: its delivery deadline "
        "passed %.3fs ago while this loop was busy. action=%s trace_id=%s "
        "request_id=%s", overdue_s, action, trace_id or "-", request_id or "-",
    )
    if request_id:
        response_queue.put({
            'request_id': request_id, 'error': True,
            'message': (
                f"command expired before dispatch: its delivery deadline "
                f"passed {overdue_s:.3f}s earlier"
            ),
            'action': action,
        })


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
    from utils.process_priority import elevate_process_priority

    from utils.trace_context import set_trace
    from ui.ui_actions import UIActionHandler
    from ui.clipboard import get_text_safe

    shm = None
    mouse_listener = None
    keyboard_listener = None
    _fault_file = None
    # wh-overlay-slow-uia-stale-badges.11, codex round 2 finding .11.2.4:
    # named out here so the cleanup below can close a dispatch the loop was
    # never able to close itself.
    dispatch_watch = None
    try:
        # ============================================================================
        # INITIALIZATION
        # ============================================================================
        config_service = ConfigService()
        config = config_service.get_config()
        setup_logging(config)

        # High process class keeps input handling scheduled under a
        # saturated CPU; the Below Normal class a Task Scheduler launch
        # hands down starves it (wh-process-priority-durable). After
        # setup_logging so a refusal's warning reaches the process log
        # instead of bare stderr (wh-process-priority-durable.1.4).
        elevate_process_priority()

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
        # wh-voice-access-parity.2.3.3.2.10: the shutdown signal goes to the
        # handler because the continuous scroll runs on its own timer thread,
        # which outlives the command that started it. This loop's own reading
        # of shutdown_event cannot stop that thread: a blocked handler holds
        # the loop away from the condition below, and the cleanup finally
        # never calls stop_continuous_scroll. The launcher waits
        # SHUTDOWN_GRACE_PERIOD_S before terminating this process, so without
        # the handoff the wheel keeps turning for those seconds.
        ui_handler = UIActionHandler(
            response_queue, config, shutdown_event=shutdown_event,
        )
        is_internal_action = threading.Event()

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
            "scroll_wheel": 150,            # One SendInput batch + the app's repaint
            # Both continuous-scroll handlers start or stop a thread and
            # return; there is no UI change to wait for, and the default
            # 500ms would hold this loop for half a second after each one
            # (wh-voice-access-parity.2.3.3).
            "start_continuous_scroll": 0,
            "stop_continuous_scroll": 0,
            "channel_probe": 0,             # No-op drain probe; reply immediately (wh-mouse-grid.1.18)
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

        # wh-overlay-slow-uia-stale-badges.11: the dispatch watch runs on its
        # own thread because the thread it watches is, by definition, the one
        # that cannot report on itself -- a handler holding the command loop
        # is exactly the state in which no loop code runs.
        dispatch_watch = _DispatchWatch()
        threading.Thread(
            target=_run_dispatch_watchdog,
            args=(dispatch_watch, shutdown_event),
            name="input-dispatch-watchdog",
            daemon=True,
        ).start()

        # ============================================================================
        # MAIN COMMAND LOOP
        # ============================================================================
        while not shutdown_event.is_set():
            # wh-overlay-slow-uia-stale-badges.11: close the previous
            # dispatch here, at the top of the turn, rather than after each
            # handler. Every dispatch branch below ends in `continue`, so
            # this single site covers all of them -- including the special
            # cases and the error paths -- where per-branch calls would have
            # to be added to each and would be forgotten by the next one.
            dispatch_watch.end()

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
            # wh-overlay-slow-uia-stale-badges.14.38: every field of the
            # untrusted envelope is extracted through one protective
            # chokepoint -- a poisoned dict key or subclass .get would
            # otherwise raise here, outside the per-action try, killing
            # the Input process. request_id and trace_id come back
            # canonicalized (exact str or None/"").
            extracted = _read_envelope(command_message)
            if extracted is None:
                continue
            action, raw_params, has_params, request_id, trace_id = extracted
            # wh-overlay-slow-uia-stale-badges.11: the delivery deadline
            # travels in the same untrusted envelope, so it is read
            # through its own protective chokepoint for the reason
            # _read_envelope documents (.14.38): an unpickled value can
            # raise from a lookup that runs outside the per-action try
            # and take the whole Input process down with it. It is a
            # separate helper rather than a sixth element of
            # _read_envelope's return, because that exact 5-tuple is an
            # established contract several tests assert against.
            deadline_monotonic = _read_delivery_deadline(command_message)
            # wh-watchdog-stall-window: read beside the delivery deadline,
            # from the same envelope and under the same containment, so a
            # malformed stamp costs the dispatch its tailored limit and
            # nothing else.
            awaited_window_s = _read_awaited_window(command_message)
            set_trace(trace_id)

            # wh-overlay-slow-uia-stale-badges.14.33 + .14.38: a missing,
            # non-string, or subclassed-string action would raise at the
            # hasattr branch, the frozenset membership sites, or the
            # special-route comparisons below, outside the
            # action-execution try, killing the Input process. Reject it
            # here and move on.
            if not _validate_action(action, request_id, response_queue):
                continue

            # wh-overlay-slow-uia-stale-badges.11: expire a command that
            # went stale while this loop was blocked. The Logic process
            # already refuses to SEND a payload whose deadline passed
            # (app.py checks it before the send, after the wait for a
            # free frame, and after framing), but every one of those
            # checks stops at the shared-memory write. The damage lands
            # on the NEXT turn of this loop: a hung handler holds the
            # single thread, Logic gives up and writes its next command
            # into the frame this loop already freed at dequeue, and
            # when the provider finally answers, the loop dispatches
            # that command against a screen that has since changed.
            # Checked BEFORE _coerce_params: an expired command is not
            # going to run, so its params never need coercing.
            if _command_expired(command_dequeue_monotonic, deadline_monotonic):
                # wh-voice-access-parity.2.3.3: a stop still stops. Every
                # other expired command is dropped unrun, including a
                # continuous-scroll START -- a scroll the Logic process
                # already gave up on must not begin late.
                _run_expired_stop(action, ui_handler)
                _report_expired_command(
                    action, request_id, trace_id, deadline_monotonic,
                    command_dequeue_monotonic, response_queue,
                )
                continue

            # wh-overlay-slow-uia-stale-badges.14.20 + .14.22 + .14.38: a
            # non-dict (or subclassed-dict) params would raise at the
            # dispatch sites below and land in the loop's outer except,
            # killing the Input process. Only an absent params key
            # defaults to {}; every supplied value is validated, falsy
            # or not. Reject and move to the next command.
            params = (
                _coerce_params(raw_params, request_id, response_queue, action)
                if has_params else {}
            )
            if params is None:
                continue

            # wh-9f3t.73.1 (+ reviewer_0 wh-n29v.38.2): thread the dequeue-
            # anchored monotonic timestamp to the two UIA-walk handlers
            # (click_element and start_overlay_walk) via their own params dict, so
            # each walk deadline is charged from the earliest reader instant.
            # Scoped to these two actions so NO other handler dispatched through
            # method_to_call(**params) (the wh-lla5d-sensitive site) receives an
            # unexpected kwarg. Both handlers accept it as an explicit optional
            # parameter; it never reaches any other handler's signature.
            # .14.48: the expansion runs before the per-action try, so
            # it is contained in its own helper.
            if action in ("click_element", "start_overlay_walk"):
                params = _enrich_walk_params(
                    params, command_dequeue_monotonic, action,
                    request_id, response_queue,
                )
                if params is None:
                    continue

            # wh-overlay-slow-uia-stale-badges.11: from here to the top of
            # the next turn, this loop is inside a handler and cannot report
            # on itself. Marked before the special-case chain rather than at
            # each dispatch site, so activate_window, retract, the clipboard
            # actions and the generic route are all covered by one call.
            dispatch_watch.begin(
                action, request_id, trace_id, command_dequeue_monotonic,
                awaited_window_s,
            )

            # --- Special Case: Window Activation ---
            # activate_window is handled directly here because it only requires win32gui
            # operations and doesn't need the UIActionHandler's UI automation framework.
            # This keeps the window management logic isolated and independently testable.
            if action == "activate_window":
                _handle_activate_window(params, request_id, response_queue, action,
                                       is_internal_action, FOREGROUND_POLL_MS,
                                       POLL_INTERVAL_MS, ACTION_DELAY_MS, logger,
                                       buffer_manager=ui_handler.buffer_manager,
                                       notify=ui_handler.show_notification)
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
                    err = _safe_error_text(e)  # .14.54
                    logger.error(f"Error in retract: {err}", exc_info=True)
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            'status': 'not_retracted',
                            'reason': f'error: {err}',
                            'action': action
                        })
                finally:
                    is_internal_action.clear()
                continue

            # --- Special Case: AI Clipboard Operations ---
            # capture/replace need custom response data (text or success),
            # not the generic ok/heuristic_done from the default handler.
            # press_key_verified (wh-review-pattern-fixes.32) rides the
            # same shape: its {"success": bool} delivery report is the
            # whole point, and the generic emitter would replace it with
            # an unconditional heuristic_done.
            if action in (
                "capture_selected_text",
                "replace_selected_text",
                "press_key_verified",
            ):
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
                    err = _safe_error_text(e)  # .14.54
                    logger.error(f"Error in {action}: {err}", exc_info=True)
                    if request_id:
                        response_queue.put({
                            'request_id': request_id,
                            'error': True,
                            'message': err,
                            'action': action,
                        })
                finally:
                    is_internal_action.clear()
                continue

            # wh-overlay-slow-uia-stale-badges.14.24: both editor control
            # commands now arrive as acknowledged requests from Logic's
            # bounded retry, so their handlers report the outcome on
            # response_queue when the envelope carries a request_id.
            if action == "terminal_editor_cancelled":
                _handle_terminal_editor_cancelled(
                    params, request_id, response_queue, ui_handler,
                )
                continue

            if action == "_te_event_ack":
                _handle_te_event_ack_command(
                    params, request_id, response_queue, ui_handler,
                )
                continue

            # wh-overlay-slow-uia-stale-badges.14.40: the level VALUE is
            # untrusted, so the getattr runs inside the handler's own
            # protective try, not inline in this loop.
            if action == "set_log_level":
                _handle_set_log_level(params, request_id, response_queue)
                continue

            # wh-9weum Phase 3 (wh-01t75): runtime soft-allow update, with
            # an ack when sent as a request (wh-overlay-slow-uia-stale-badges.14.14).
            if action == "add_soft_allow_tuple":
                _handle_add_soft_allow_tuple(
                    params, request_id, response_queue, ui_handler,
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
                # wh-overlay-slow-uia-stale-badges.14.45: a binding
                # failure raises BEFORE the self-owning handler starts,
                # and the except below suppresses the standard error
                # for these actions -- the awaited Future would time
                # out. The guard answers it instead and skips the call.
                if action in _HANDLES_OWN_RESPONSE:
                    if not _reject_unbindable_self_owning(
                        method_to_call, action, request_id, params,
                        response_queue,
                    ):
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
                        _safe_type_name(e),  # .14.59
                    )
                err = _safe_error_text(e)  # .14.54
                logger.error("Error executing input action '%s': %s", action, err, exc_info=True)
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
        # The loop closes each dispatch at the top of its NEXT turn, which is
        # the placement that covers every dispatch branch with one call. A
        # shutdown that arrives while a handler is blocked ends the loop
        # before that turn happens, so the closing line for a stall that was
        # already reported would never be written and the operator would be
        # left with a stall record that reads as though the provider never
        # came back. end() is idempotent, so this is a no-op on every
        # ordinary exit. Wrapped because a failure to write one log line must
        # not stop the listeners and the shared memory from being released
        # (codex round 2, finding .11.2.4).
        if dispatch_watch is not None:
            try:
                dispatch_watch.end()
            except Exception:
                logger.warning(
                    "the in-flight dispatch could not be closed during "
                    "shutdown", exc_info=True,
                )
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
