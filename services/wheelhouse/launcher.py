"""Supervised launcher and process manager for WheelHouse services.

This module serves as the main entry point and supervisor for the WheelHouse
application. It manages process lifecycle, handles crash recovery, maintains
shared resources, and provides graceful shutdown capabilities. The launcher
ensures robust operation by automatically restarting crashed processes while
preventing infinite restart loops.

Key Features:
  - Process supervision with crash detection and recovery
  - Shared memory management for inter-process communication
  - PID file management and stale process cleanup
  - Configurable crash thresholds and restart policies
  - Graceful shutdown with resource cleanup

Key Functions:
  - main: Primary supervisor loop and entry point.
  - cleanup_stale_resources: Removes leftover resources from previous runs.
  - Process lifecycle management with restart counting.

Typical Usage:
  python launcher.py
  
  # Or programmatically:
  from launcher import main
  main()

Process Architecture:
  - Main supervisor process (this module)
  - WheelHouse main service process  
  - Input synthesis UI process
  - Shared memory for IPC between processes
"""

# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()

# launcher.py: Failsafe, supervised entry point for WheelHouse.
import atexit
import logging
import multiprocessing
import os
import psutil
import subprocess
import sys
import threading
import time
from multiprocessing import shared_memory

logger = logging.getLogger(__name__)

# --- Constants ---
APP_NAME = "WheelHouse"
SHARED_MEM_SIZE = 1024 * 64  # 64 KiB buffer
GUI_OVERLAY_SHM_SIZE = 256  # Small buffer for GUI activity state
CRASH_THRESHOLD_S = 15  # If a process exits faster than this, it's a crash.
MAX_CRASHES = 3  # Abort after this many consecutive crashes.
SHUTDOWN_GRACE_PERIOD_S = 5 # How long to wait for processes to exit gracefully

# The console-probe helper (wh-jvrs.1) owns all foreign-console attachment in
# its own isolated process so the Logic process never binds to a foreign
# terminal's console. Its death must NOT take WheelHouse down: a missing helper
# simply degrades the at-a-prompt probe to "not at a prompt" (dictation falls
# back to terminal passthrough). Live ownership is in the Logic process's
# ``ConsoleProbeClient`` (self-spawn + restart on EOF) because the helper's
# stdin/stdout pipes cannot cross the multiprocessing spawn boundary; the
# launcher exposes the supervision seams below (spawn / liveness / restart
# budget) as the single tested contract the client's production spawn reuses.
# This budget bounds helper restarts so a helper crashing in a tight loop
# cannot spin forever; past the budget the probe stays degraded but the app
# keeps running.
MAX_CONSOLE_PROBE_RESTARTS = 5

# --- Global, safe definitions ---
APP_DATA_PATH = ""
PID_FILE_PATH = ""
RESTART_FLAG_PATH = ""

# Console input-mode flags (SetConsoleMode). QuickEdit lets a mouse click in
# the console start a text selection, and Windows freezes EVERY write to the
# console until the selection is dismissed. All WheelHouse processes inherit
# this console for stderr, so one stray click wedges the whole app
# (wh-console-quickedit-freeze, 2026-07-05: GUI main thread Not Responding,
# log frozen, supervisor blocked). ENABLE_EXTENDED_FLAGS must be set in the
# same call or the QuickEdit bit is ignored.
#
# ENABLE_MOUSE_INPUT must be cleared alongside QuickEdit: with QuickEdit off
# and mouse input on, conhost delivers every mouse event -- including the
# scroll wheel -- to the console input buffer, which no WheelHouse process
# ever reads. The wheel stops scrolling the window and unread input records
# accumulate for the whole run (wh-log-console-freeze, 2026-08-08). With
# both bits off, conhost handles the wheel itself and the viewport scrolls.
_ENABLE_MOUSE_INPUT = 0x0010
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3


def disable_console_quick_edit(*, _kernel32=None):
    """Turn off QuickEdit and mouse input on the attached console, if any.

    Opens ``CONIN$`` directly instead of ``GetStdHandle(STD_INPUT_HANDLE)``
    because a redirected stdin would make GetStdHandle return a pipe handle
    on which GetConsoleMode fails (same lesson as ui/console_probe_helper.py).
    Deliberate selection stays available via the console system menu
    (Edit > Mark); only the accidental click-drag path is removed. Mouse
    input is cleared too so conhost keeps handling the scroll wheel itself
    (wh-log-console-freeze); no WheelHouse process reads console input.

    Returns True when the mode was changed, False otherwise. Never raises:
    a headless launch (no console) or any API failure just leaves the mode
    as it is. ``_kernel32`` is a test seam.
    """
    import ctypes

    try:
        if _kernel32 is None:
            if sys.platform != "win32":
                return False
            _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        if not _kernel32.GetConsoleWindow():
            return False  # headless: no console attached

        invalid_handle = ctypes.c_void_p(-1).value
        handle = _kernel32.CreateFileW(
            "CONIN$",
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if not handle or handle == invalid_handle:
            return False

        try:
            mode = ctypes.c_ulong(0)
            if not _kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            new_mode = (mode.value | _ENABLE_EXTENDED_FLAGS) & ~(
                _ENABLE_QUICK_EDIT_MODE | _ENABLE_MOUSE_INPUT
            )
            if not _kernel32.SetConsoleMode(handle, new_mode):
                return False
            return True
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        return False

# Seconds to wait for the previous instance to exit after terminate() before
# escalating to kill(). The launcher tears its children down and unlinks
# shared memory on the way out, so it needs more than an instant.
_STOP_PREVIOUS_WAIT_S = 5.0


def write_instance_record(path, *, pid, create_time):
    """Record this launcher's PID and creation time at ``path``.

    The creation time is what makes the record safe to act on later.
    Windows reuses PIDs, so a bare PID can point at an unrelated process
    once the recorded one exits. ``stop_previous_instance`` kills a
    process tree, so acting on a recycled PID would kill whatever the
    user happens to be running under that number.
    """
    with open(path, 'w') as f:
        f.write("%d %.6f" % (int(pid), float(create_time)))


def read_instance_record(path):
    """Return ``(pid, create_time_text)`` from ``path``, or None.

    Returns None when the file is absent, unreadable, or not in the
    two-field format. A bare PID with no creation time (the format older
    builds wrote) also reads as None, so such a file can never authorise
    a kill.
    """
    try:
        with open(path, 'r') as f:
            parts = f.read().strip().split()
        if len(parts) != 2:
            return None
        pid = int(parts[0])
        float(parts[1])          # validate, but keep the text for comparison
        return (pid, parts[1])
    except (IOError, OSError, ValueError):
        return None


def _owned_descendants(owner, descendants):
    """Return the descendants that belong to WheelHouse itself.

    Being a descendant of the launcher does NOT make a process ours. The
    ``run`` voice action starts an arbitrary user program through a shell
    (``speech/actions.py`` ``run_program``), so a document the user opened
    by voice is a grandchild of the launcher. Terminating the whole
    recursive tree would close that program and lose unsaved work.

    The test is the executable: Logic, Input and GUI are started with
    ``multiprocessing``, so they run the same interpreter as the launcher
    that recorded them. A ``run`` target reached through the shell does
    not. Any process whose executable cannot be read is left alone, so an
    unreadable answer never authorises a kill.

    This is a bound on the harm, not a complete ownership model. A
    provider or helper that the Logic process started runs a different
    executable and is left behind (wh-launcher-single-instance.2.1).
    """
    try:
        owner_exe = os.path.normcase(owner.exe())
    except Exception as exc:
        logger.warning(
            "Could not read the executable of the previous launcher (%s), "
            "so its descendants cannot be identified. Stopping the "
            "launcher alone.", exc,
        )
        return []

    owned = []
    for proc in descendants:
        try:
            is_ours = os.path.normcase(proc.exe()) == owner_exe
        except Exception:
            is_ours = False
        if is_ours:
            owned.append(proc)
        else:
            logger.info(
                "Leaving PID %s alone: it is not a WheelHouse process.",
                getattr(proc, "pid", "?"),
            )
    return owned


def stop_previous_instance(path, *, _psutil=None):
    """Stop the launcher recorded at ``path`` and its children.

    Returns True when a previous instance was stopped, False otherwise.
    Never raises: any failure leaves the caller free to start normally.
    ``_psutil`` is a test seam.

    Two details are load-bearing:

    * The recorded creation time must match the live process before
      anything is killed. A mismatch means the PID was reused, so the
      record is stale and the live process belongs to somebody else.
    * The parent is killed BEFORE its children. The old launcher
      supervises its children and restarts them when they die, so killing
      a child first can make the old supervisor respawn it. The child
      list is read before the parent dies, because psutil finds children
      through the parent.
    """
    ps = _psutil or psutil
    record = read_instance_record(path)
    if record is None:
        # Absent, unreadable, or the legacy bare-PID format. Nothing can be
        # identified from such a file, so remove it instead of leaving it
        # for a later start to misread.
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    old_pid, recorded_create_time = record

    if old_pid == os.getpid():
        logger.warning(
            "Instance record names this process (%s); ignoring it.", old_pid
        )
        return False

    stopped = False
    try:
        try:
            proc = ps.Process(old_pid)
            live_create_time = "%.6f" % float(proc.create_time())
        except ps.NoSuchProcess:
            logger.info(
                "Instance record names PID %s, which is gone. Clean start.",
                old_pid,
            )
            proc = None
            live_create_time = None
        except Exception as exc:
            # Windows can refuse access to the process object. Without the
            # creation time the record cannot be confirmed, and an
            # unconfirmed record must never authorise a kill.
            logger.error(
                "Could not identify the process recorded at PID %s (%s). "
                "Leaving it alone.", old_pid, exc,
            )
            proc = None
            live_create_time = None

        if proc is not None:
            if live_create_time != recorded_create_time:
                logger.info(
                    "PID %s is now a different process (created %s, record "
                    "says %s). Leaving it alone.",
                    old_pid, live_create_time, recorded_create_time,
                )
            else:
                logger.warning(
                    "A previous WheelHouse launcher (PID %s) is still "
                    "running. Stopping it before this one starts.", old_pid
                )
                try:
                    descendants = proc.children(recursive=True)
                except Exception as exc:
                    # Without the list the children outlive their parent,
                    # but stopping the parent is still worth doing.
                    logger.warning(
                        "Could not list the children of PID %s (%s). "
                        "Stopping the parent alone.", old_pid, exc,
                    )
                    descendants = []
                children = _owned_descendants(proc, descendants)

                # Parent first, then the children, each guarded on its own.
                # One process this launcher may not terminate -- the old
                # copy runs elevated and this one does not, or it exits
                # between two calls -- must not cost the rest of the stop.
                for target in [proc] + children:
                    try:
                        target.terminate()
                    except Exception as exc:
                        logger.warning(
                            "Could not terminate PID %s: %s",
                            getattr(target, "pid", "?"), exc,
                        )
                    else:
                        if target is proc:
                            stopped = True

                try:
                    gone, alive = ps.wait_procs(
                        [proc] + children, timeout=_STOP_PREVIOUS_WAIT_S
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not wait for the previous instance to "
                        "exit: %s", exc,
                    )
                    alive = []
                for p in alive:
                    logger.warning(
                        "PID %s did not exit after terminate; killing it.",
                        getattr(p, "pid", "?"),
                    )
                    try:
                        p.kill()
                    except Exception:
                        pass
                if stopped:
                    logger.info("Previous instance stopped.")
                else:
                    logger.error(
                        "Could not stop the previous launcher (PID %s). "
                        "Two copies may now be running.", old_pid,
                    )
    except Exception as exc:
        logger.error(
            "Could not stop the previous instance (PID %s): %s",
            old_pid, exc, exc_info=True,
        )

    try:
        os.remove(path)
    except OSError:
        pass
    return stopped


def cleanup_stale_resources():
    """Idempotent cleanup of resources from a previous, potentially crashed run.

    Stops a previous launcher that is still running (wh-launcher-single-
    instance). Before this, the function read the recorded PID and then
    did nothing with it, so two full WheelHouse instances could run at
    once. The only protection was outside the application, in the
    scheduled task's StopExisting policy.
    """
    logger.info("Performing failsafe cleanup of stale resources...")
    stop_previous_instance(PID_FILE_PATH)


def restore_orphaned_ptt_volume():
    """Put back a speaker level that a lost push-to-talk hold left lowered.

    A Logic process that is lost during a hold never runs its release, so
    the speakers stay at the lowered level. The Logic process writes a
    record on disk before it lowers the level, and this restore reads that
    record (wh-ptt-mute-orphaned-on-process-loss). Call it only when no
    Logic process runs: a restore during a live hold raises the level while
    the user still holds the key.

    The restore runs in a daemon thread, and the launcher waits for it at
    most SHUTDOWN_GRACE_PERIOD_S, as the bounded stop in
    utils/notifier_worker.py waits for its worker. A Core Audio call that
    does not return costs the launcher that time and no more. At the limit
    the launcher sets the restore's cancel flag, logs a warning that names
    the limit, and goes on. The restore checks the flag immediately before
    its write and immediately before its record delete.

    A failure is logged and never stops the launcher. restore_orphaned_volume
    logs each result except "no record", so this function and the thread
    log only an exception that comes out of it.

    The restore module is imported here, on the launcher thread, before the
    thread starts. When comtypes and pycaw are not loaded yet, the restore
    thread loads them before its first Core Audio call, so a restore thread
    that waits in a Core Audio call holds no import lock that the launcher
    needs for "from main import start_logic_process".
    """
    cancel = threading.Event()
    try:
        from services.wheelhouse.utils.ptt_volume_record import restore_orphaned_volume

        worker = threading.Thread(
            target=_run_ptt_volume_restore,
            args=(restore_orphaned_volume, cancel),
            name="PttVolumeRestore",
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        logger.error(
            "Could not restore the push-to-talk speaker level: %s", exc, exc_info=True,
        )
        return
    worker.join(SHUTDOWN_GRACE_PERIOD_S)
    if worker.is_alive():
        # crewcut: the cancel flag stops only the steps that have not started.
        # A device write, or a record delete, that the restore thread started
        # before the flag was set can still land after the launcher goes on,
        # for example during a hold of the next Logic process. No way to
        # remove this is known: Python cannot stop a thread inside a foreign
        # call. Running the restore in a child process that the launcher ends
        # at the limit would stop more steps, but not a write that the audio
        # service is already applying.
        cancel.set()
        logger.warning(
            "The push-to-talk speaker restore did not finish within %s s. The "
            "launcher goes on without it, and the restore stops before its next "
            "write or record delete.",
            SHUTDOWN_GRACE_PERIOD_S,
        )


def _run_ptt_volume_restore(restore, cancel):
    """Run the restore on its thread. An exception is logged and not raised."""
    try:
        restore(cancel=cancel)
    except Exception as exc:
        logger.error(
            "Could not restore the push-to-talk speaker level: %s", exc, exc_info=True,
        )


def _console_probe_helper_command():
    """Return the argv that launches the console-probe helper subprocess.

    Runs ``ui/console_probe_helper.py`` directly with the current interpreter.
    Pure: builds a path relative to this file and returns a list -- no spawn, no
    I/O -- so it is trivially unit-testable (wh-jvrs.1).
    """
    helper_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "ui",
        "console_probe_helper.py",
    )
    return [sys.executable, helper_path]


def _spawn_console_probe_helper(popen=subprocess.Popen):
    """Spawn the helper with stdin/stdout pipes and stderr discarded.

    ``stderr=subprocess.DEVNULL`` is the OS-level guarantee that the helper can
    never leak text into a foreign terminal even if some library inside it
    writes to stderr. ``popen`` is injectable so a unit test can assert the
    spawn arguments without launching a real process.

    Returns the spawned process object, or None if the spawn failed (the probe
    then degrades to "not at a prompt" and the app keeps running).
    """
    try:
        return popen(
            _console_probe_helper_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # pragma: no cover - defensive; spawn rarely fails
        logger.error(f"Failed to spawn console-probe helper: {e}")
        return None


def _console_probe_helper_alive(proc):
    """Return True iff ``proc`` is a live helper (``poll()`` is None).

    Pure predicate over the process object; None (never spawned / torn down)
    reports not-alive. Unit-testable without a real process.
    """
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:  # pragma: no cover - defensive
        return False


def _should_restart_console_probe_helper(restart_count):
    """Return True iff the helper may be restarted at ``restart_count`` so far.

    Bounds helper restarts to ``MAX_CONSOLE_PROBE_RESTARTS`` so a helper that
    crashes in a tight loop cannot spin forever. Past the budget the probe stays
    degraded (returns False) while WheelHouse keeps running.
    """
    return restart_count < MAX_CONSOLE_PROBE_RESTARTS


# Token that triggers the one-shot screen-reader-flag clear and immediate exit.
CLEAR_SCREEN_READER_FLAG_ARG = "--clear-screen-reader-flag"

# Token that triggers the one-shot first-use-hint record reset and exit (wh-r3xy1).
RESET_FIRST_USE_HINTS_ARG = "--reset-first-use-hints"


def _clear_screen_reader_flag_intent(argv):
    """Return True iff the one-shot clear-flag token is present in ``argv``.

    Pure: no side effects, no imports beyond membership test. ``argv`` is the
    full argument vector (including argv[0]); the function only checks for the
    presence of the flag token anywhere after it. Trivially unit-testable.
    """

    return CLEAR_SCREEN_READER_FLAG_ARG in (argv or [])


def _reset_first_use_hints_intent(argv):
    """Return True iff the one-shot reset-first-use-hints token is in ``argv``.

    Pure: no side effects, no imports beyond a membership test. ``argv`` is the
    full argument vector (including argv[0]); the function only checks for the
    presence of the token anywhere after it. Trivially unit-testable (wh-r3xy1).
    """

    return RESET_FIRST_USE_HINTS_ARG in (argv or [])


def main(argv=None, *, clear_setter=None, delete_hints_fn=None):
    """Entry point. Handles the one-shot CLI shortcuts, else supervises.

    When ``argv`` carries ``--clear-screen-reader-flag`` the program clears the
    Windows screen-reader flag (uiParam=0) via
    ``utils.screen_reader_flag.clear_screen_reader_flag``, prints a one-line
    confirmation, and exits BEFORE any process spawn or shared-memory setup --
    exit 0 when the clear succeeded, exit 1 when the best-effort clear failed
    (so a recovery script can tell the flag may still be set). This is a
    recovery shortcut for a machine left with the flag set after a WheelHouse
    crash.

    When ``argv`` carries ``--reset-first-use-hints`` the program deletes the
    first-use-hint record file
    (``services/wheelhouse/data/click_first_use_hint_shown.toml``) via
    ``click_first_use_hint.delete_hint_record`` under that module's lock,
    prints a one-line confirmation, and exits BEFORE any process spawn -- exit
    0 on success (including an already-absent file), exit 1 on a delete error.
    This lets a user who dismissed the screen-reader-flag discovery hint see it
    again (wh-r3xy1). The module lock is process-local, so this reset is meant
    to run while WheelHouse is NOT running; if run live, a concurrent Logic
    first-use-hint write could re-create the record -- harmless, just re-run
    once WheelHouse is stopped (wh-9f3t.61.1).

    ``argv`` defaults to ``sys.argv``. ``clear_setter`` is forwarded to
    ``clear_screen_reader_flag`` so unit tests can inject a fake and avoid the
    real Win32 call; production passes None and the real syscall runs.
    ``delete_hints_fn`` is forwarded to the reset path so unit tests can inject
    a fake deleter; production passes None and the real delete runs.
    """

    if argv is None:
        argv = sys.argv

    # --- One-shot recovery shortcut (must short-circuit before any spawn) ---
    if _reset_first_use_hints_intent(argv):
        # The bare `from click_first_use_hint ...` import below needs this
        # module's own directory (services/wheelhouse) on sys.path -- same
        # robustness reason as the clear-flag branch below.
        service_dir = os.path.dirname(os.path.abspath(__file__))
        if service_dir not in sys.path:
            sys.path.insert(0, service_dir)
        if delete_hints_fn is not None:
            ok = delete_hints_fn()
        else:
            from click_first_use_hint import (
                default_hint_path,
                delete_hint_record,
            )
            ok = delete_hint_record(default_hint_path())
        if ok:
            print(
                "Reset the first-use discovery hints. Run this while Wheelhouse "
                "is not running; if Wheelhouse is live, a concurrent write may "
                "re-create the record -- re-run once it is stopped."
            )
            raise SystemExit(0)
        print("Failed to reset the first-use discovery hints (see log).")
        raise SystemExit(1)

    if _clear_screen_reader_flag_intent(argv):
        # The bare `from utils...` import below needs this module's own
        # directory (services/wheelhouse) on sys.path. When run as
        # `python launcher.py` Python adds the script dir automatically, but
        # under `python -m`, runpy, or a frozen build it may not be present,
        # so insert it explicitly to make the import robust.
        service_dir = os.path.dirname(os.path.abspath(__file__))
        if service_dir not in sys.path:
            sys.path.insert(0, service_dir)
        from utils.screen_reader_flag import clear_screen_reader_flag
        ok = clear_screen_reader_flag(setter=clear_setter)
        if ok:
            print("Cleared the Windows screen-reader flag.")
            raise SystemExit(0)
        # Best-effort clear FAILED: report a non-zero exit so a recovery
        # script (or the user) can observe that the flag may still be set.
        print("Failed to clear the Windows screen-reader flag (best-effort; see log).")
        raise SystemExit(1)

    _run_supervisor()


# Live handles from the most recent _configure_launcher_logging call so a
# repeat call can tear the previous split down instead of stacking a second
# queue handler on the root logger (wh-launcher-test-log-leak). Production
# calls this once per process; pytest runs launcher.main() -- and therefore
# this function -- dozens of times in one process, and with k accumulated
# handlers every record was written to the log file k times.
_launcher_logging_state: dict = {
    "listener": None,
    "queue_handler": None,
    "watchdog": None,
}


def _teardown_launcher_logging():
    """Stop and release the current launcher logging split, if any.

    Removes the queue handler from the root logger, stops the watchdog
    and listener, closes the listener's handlers -- the rotating file
    handler otherwise keeps wheelhouse.log and its lock file open for
    the rest of the process, one leaked pair per reconfigure -- and
    clears _launcher_logging_state. Safe to call when nothing is
    configured. Used by _configure_launcher_logging on reconfigure and
    by tests that exercise the real configure function
    (wh-launcher-test-log-leak, wh-log-crash-fixes.1.2).
    """
    root_logger = logging.getLogger()
    if _launcher_logging_state["queue_handler"] is not None:
        root_logger.removeHandler(_launcher_logging_state["queue_handler"])
        _launcher_logging_state["queue_handler"] = None
    if _launcher_logging_state["watchdog"] is not None:
        try:
            _launcher_logging_state["watchdog"].stop()
        except Exception:
            pass
        _launcher_logging_state["watchdog"] = None
    listener = _launcher_logging_state["listener"]
    if listener is not None:
        try:
            listener.stop(5.0)
        except Exception:
            pass
        for handler in getattr(listener, "handlers", ()):
            try:
                handler.close()
            except Exception:
                pass
        _launcher_logging_state["listener"] = None


def _configure_launcher_logging(project_root):
    """Queue/listener logging for the launcher (wh-console-write-resilience).

    The supervisor thread must never block on a frozen console: the
    crash-recovery path logged synchronously to stderr (logger.error at
    child death), so a conhost hang / mark mode wedged the restart loop
    at the log call, before it could respawn the dead child. Same split
    the service processes use via setup_logging: the supervisor enqueues
    records on a bounded non-blocking queue; a listener thread writes the
    FILE handler first (durable even when the console is frozen), then
    stderr. A listener watchdog reports a stalled drain to the same
    side-channel file setup_logging uses, not stderr.

    Calling it again replaces the previous configuration: the old queue
    handler is removed from the root logger and the old watchdog and
    listener are stopped (wh-launcher-test-log-leak).

    Returns the started listener (callers stop it on shutdown;
    _run_supervisor registers a bounded atexit stop).
    """
    from services.wheelhouse.utils.queue_logging import (
        WheelHouseQueueListener,
        _DroppingQueueHandler,
        _ListenerWatchdog,
        make_log_queue,
    )

    log_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] Launcher - %(filename)s:%(lineno)d - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Tear down the previous call's split before installing a new one.
    _teardown_launcher_logging()

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(log_formatter)

    log_file = os.path.join(project_root, "wheelhouse.log")
    file_handler = None
    try:
        from concurrent_log_handler import ConcurrentRotatingFileHandler
        file_handler = ConcurrentRotatingFileHandler(
            log_file, mode='a',
            maxBytes=10 * 1024 * 1024, backupCount=2, encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(log_formatter)
    except Exception as e:
        file_handler = None
        print(f"Launcher failed to create log file handler: {e}", file=sys.stderr)

    # File first: every record is durably on disk before the risky
    # console write (same ordering rationale as logging_setup.py).
    listener_handlers = []
    if file_handler is not None:
        listener_handlers.append(file_handler)
    listener_handlers.append(console_handler)

    log_queue = make_log_queue()
    queue_handler = _DroppingQueueHandler(log_queue)
    queue_handler.setLevel(logging.NOTSET)
    listener = WheelHouseQueueListener(
        log_queue, *listener_handlers, drop_handler_ref=queue_handler
    )
    listener.start()

    watchdog = _ListenerWatchdog(
        queue_handler, listener,
        stall_log_path=os.path.join(project_root, "wheelhouse-watchdog.log"),
    )
    watchdog.start()

    root_logger.addHandler(queue_handler)
    _launcher_logging_state["listener"] = listener
    _launcher_logging_state["queue_handler"] = queue_handler
    _launcher_logging_state["watchdog"] = watchdog
    return listener


def _read_transcript_logging_flag(config_path) -> bool:
    """Read the single LOG_TRANSCRIPTS switch from config.toml.

    Never raises (wh-transcript-log-defaults): a missing file, malformed
    TOML, or non-bool value all mean False -- the privacy-safe release
    default. The launcher exports the result as WHEELHOUSE_LOG_TRANSCRIPTS
    ("1"/"0") before spawning children, so Logic, Input, GUI, and every STT
    provider process (via RemoteSTTLauncher's environment copy) see the
    same value. utils/redact.py and the providers' shared_stt/redact.py
    consume it.
    """
    try:
        import tomllib

        with open(config_path, "rb") as f:
            value = tomllib.load(f).get("LOG_TRANSCRIPTS")
        return value is True
    except Exception:
        return False


def _run_supervisor():
    """The main launcher and supervisor loop."""
    # --- Configure Python Path ---
    # Add the project's root directory (one level up from 'services') to the Python path.
    # This allows for clean, absolute imports from the project root (e.g., 'from services.wheelhouse...').
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # --- Configure logging IMMEDIATELY ---
    # Launcher can't use setup_logging() (needs config dict + heavier imports);
    # _configure_launcher_logging gives it the same queue/listener split with
    # the same format and file (wh-console-write-resilience).
    _listener = _configure_launcher_logging(project_root)
    atexit.register(_listener.stop, 5.0)

    # --- Transcript-logging switch (wh-transcript-log-defaults) ---
    # Exported before any child spawn so all processes inherit it; see
    # _read_transcript_logging_flag.
    _config_toml = os.path.join(os.path.dirname(__file__), "config.toml")
    os.environ["WHEELHOUSE_LOG_TRANSCRIPTS"] = (
        "1" if _read_transcript_logging_flag(_config_toml) else "0"
    )
    if os.environ["WHEELHOUSE_LOG_TRANSCRIPTS"] == "1":
        logger.info(
            "Transcript logging ENABLED (LOG_TRANSCRIPTS = true): dictated "
            "text will appear in logs."
        )

    # --- Raise this process's priority class before spawning children ---
    # A Task Scheduler launch starts the tree at Below Normal (task XML
    # Priority default 7), which Windows propagates to every child and which
    # starves the speech pipeline whenever the machine is saturated
    # (wh-process-priority-durable). Children still elevate themselves: High
    # does not propagate to children, only Below Normal and Idle do.
    from services.wheelhouse.utils.process_priority import (
        elevate_process_priority,
    )
    elevate_process_priority()

    # --- Harden the console before anything writes to it ---
    # Children inherit this console for stderr; QuickEdit stays disabled for
    # all of them because the mode lives on the console, not the process.
    if disable_console_quick_edit():
        logger.info("Console QuickEdit mode disabled (click-freeze hardening).")
    else:
        logger.info("Console QuickEdit mode unchanged (no console or API failure).")

    # --- Initialize paths ---
    from services.wheelhouse.utils.system import get_app_data_path
    global APP_DATA_PATH, PID_FILE_PATH, RESTART_FLAG_PATH
    
    APP_DATA_PATH = get_app_data_path()
    PID_FILE_PATH = os.path.join(APP_DATA_PATH, f"{APP_NAME.lower()}.pid")
    RESTART_FLAG_PATH = os.path.join(APP_DATA_PATH, f"{APP_NAME.lower()}.restart")
    
    cleanup_stale_resources()

    # Claim the instance record for THIS launcher. cleanup_stale_resources
    # has just stopped any previous launcher and removed the old record.
    # The record holds the launcher's own PID, not the Logic process PID:
    # the launcher is the process whose lifetime matches the application,
    # and the one a second start has to stop (wh-launcher-single-instance).
    try:
        write_instance_record(
            PID_FILE_PATH,
            pid=os.getpid(),
            create_time=psutil.Process(os.getpid()).create_time(),
        )
    except Exception as exc:
        # Best effort: losing the record costs second-copy protection on
        # the NEXT start, which is not a reason to refuse to start now.
        logger.error("Could not write the instance record: %s", exc)

    # No child of this launcher has started yet. When the instance record
    # named a live launcher, cleanup_stale_resources has just stopped it and
    # its children. A hold that a previous run lost is restored here, before
    # a new Logic process can start a hold of its own
    # (wh-ptt-mute-orphaned-on-process-loss). The restore comes after this
    # launcher's instance record is written, so a later start during the
    # restore finds this launcher and stops it, and no second WheelHouse
    # starts.
    # crewcut: a Logic process of a previous run that cleanup_stale_resources
    # did not stop (its launcher was already gone, or the stop failed) can
    # still be in a hold, and this restore then raises the level during that
    # hold. To remove the limit, write the Logic process PID and creation
    # time into the push-to-talk volume record, and skip the restore while
    # that process runs.
    restore_orphaned_ptt_volume()

    crash_count = 0
    should_restart = True

    while should_restart and crash_count < MAX_CRASHES:
        start_time = time.time()
        
        # Using a unique name for the SHM block to avoid collisions from stale runs
        shm_name = f"{APP_NAME}_SHM_{os.getpid()}_{time.time()}"
        shm = shared_memory.SharedMemory(name=shm_name, create=True, size=SHARED_MEM_SIZE)
        
        # Create a small shared memory segment for GUI activity state updates
        gui_shm_name = f"{APP_NAME}_GUI_SHM_{os.getpid()}_{time.time()}"
        gui_shm = shared_memory.SharedMemory(name=gui_shm_name, create=True, size=GUI_OVERLAY_SHM_SIZE)
        
        response_queue = multiprocessing.Queue()
        commands_to_logic_queue = multiprocessing.Queue()
        state_to_gui_queue = multiprocessing.Queue()
        command_ready_event = multiprocessing.Event()
        input_ready_event = multiprocessing.Event()
        shutdown_event = multiprocessing.Event()
        
        logic_proc, input_proc, gui_proc = None, None, None

        try:
            from main import start_logic_process
            from input_proc import input_process_main
            from gui import gui_process_target

            logic_args = (shm.name, command_ready_event, input_ready_event, response_queue, SHARED_MEM_SIZE, shutdown_event, commands_to_logic_queue, state_to_gui_queue, gui_shm_name)
            input_args = (shm.name, command_ready_event, input_ready_event, response_queue, shutdown_event)
            gui_args = (shutdown_event, commands_to_logic_queue, state_to_gui_queue, gui_shm_name)

            logic_proc = multiprocessing.Process(target=start_logic_process, args=logic_args, name="LogicProcess")
            input_proc = multiprocessing.Process(target=input_process_main, args=input_args, name="InputProcess")
            gui_proc = multiprocessing.Process(target=gui_process_target, args=gui_args, name="GuiProcess")

            logger.info("Starting Logic, Input, and GUI processes...")
            logic_proc.start()
            input_proc.start()
            gui_proc.start()

            # NOTE (wh-jvrs.1): the console-probe helper is NOT spawned here.
            # The helper's stdin/stdout pipes cannot cross the multiprocessing
            # spawn boundary into the Logic process, so a launcher-spawned
            # helper would be an orphan with no client talking to it. Instead
            # the Logic process owns the helper through its own
            # ``ConsoleProbeClient`` (self-spawn on first use, restart on EOF
            # under the ``MAX_CONSOLE_PROBE_RESTARTS`` budget) -- the alternative
            # the dispatch authorises. The client reuses the launcher's
            # ``_console_probe_helper_command`` / ``_spawn_console_probe_helper``
            # for the single spawn path and ``_should_restart_console_probe_helper``
            # for the respawn budget; ``_console_probe_helper_alive`` is the
            # parallel liveness predicate exercised by the launcher tests (the
            # client checks ``proc.poll()`` directly inline). The launcher does
            # not run a redundant idle helper of its own.

            # The instance record is written once at startup and holds the
            # LAUNCHER's PID. It deliberately is not rewritten here: the
            # Logic process PID used to be written to this same file, which
            # overwrote the record and left nothing able to identify the
            # running instance (wh-launcher-single-instance). Nothing read
            # the Logic PID.

            # Wait until a shutdown is signaled or a process crashes.
            while not shutdown_event.is_set():
                logic_alive = logic_proc.is_alive()
                input_alive = input_proc.is_alive()
                gui_alive = gui_proc.is_alive()

                if not logic_alive or not input_alive or not gui_alive:
                    # Log which process(es) died and their exit codes
                    dead = []
                    if not logic_alive:
                        dead.append(f"Logic(exit={logic_proc.exitcode})")
                    if not input_alive:
                        dead.append(f"Input(exit={input_proc.exitcode})")
                    if not gui_alive:
                        dead.append(f"GUI(exit={gui_proc.exitcode})")
                    logger.error(f"Process(es) terminated unexpectedly: {', '.join(dead)}")
                    shutdown_event.set() # Trigger a full shutdown/restart cycle
                    break
                time.sleep(0.5)
            
            logger.info("Launcher loop exited. Reason: Shutdown signaled or process terminated.")

        except Exception as e:
            logger.critical(f"Launcher failed to start processes: {e}", exc_info=True)
            shutdown_event.set()
        
        finally:
            logger.info("Launcher entering cleanup phase...")
            if not shutdown_event.is_set():
                shutdown_event.set()

            procs_to_join = [p for p in [logic_proc, input_proc, gui_proc] if p and p.is_alive()]
            for p in procs_to_join:
                p.join(timeout=SHUTDOWN_GRACE_PERIOD_S)

            for p in [p for p in procs_to_join if p.is_alive()]:
                logger.warning(f"Process {p.name} ({p.pid}) did not exit gracefully. Terminating.")
                p.terminate()
                # terminate() only asks Windows to end the process. The join
                # waits until it has ended, so the restore below does not run
                # while the Logic process can still hold the speakers down.
                p.join(timeout=SHUTDOWN_GRACE_PERIOD_S)

            # A hold that this cycle's Logic process lost is restored here,
            # after every child has exited and before the restart decision
            # (wh-ptt-mute-orphaned-on-process-loss).
            still_running = [
                p for p in [logic_proc, input_proc, gui_proc] if p and p.is_alive()
            ]
            if still_running:
                logger.warning(
                    "Push-to-talk speaker restore skipped because %s is still running.",
                    ", ".join(f"{p.name} ({p.pid})" for p in still_running),
                )
            else:
                restore_orphaned_ptt_volume()

            shm.close()
            shm.unlink()
            gui_shm.close()
            gui_shm.unlink()
            logger.info("Shared memory unlinked by launcher.")

            uptime = time.time() - start_time
            if uptime < CRASH_THRESHOLD_S and not os.path.exists(RESTART_FLAG_PATH):
                crash_count += 1
                logger.error(f"Application crashed after {uptime:.2f}s. Crash count: {crash_count}/{MAX_CRASHES}")
            else:
                crash_count = 0

            if os.path.exists(RESTART_FLAG_PATH):
                logger.info("Restart flag found.")
                try:
                    os.remove(RESTART_FLAG_PATH)
                    should_restart = True
                except OSError as e:
                    logger.error(f"Failed to remove restart flag: {e}. Aborting.")
                    should_restart = False
            else:
                should_restart = False
        
    # The instance record is removed once the supervisor loop ends, NOT at
    # the end of each iteration. A restart iteration keeps the same launcher
    # process alive, so clearing the record between iterations would leave
    # the restarted instance unidentifiable to a second start
    # (wh-launcher-single-instance).
    if os.path.exists(PID_FILE_PATH):
        try:
            os.remove(PID_FILE_PATH)
        except OSError:
            pass

    if crash_count >= MAX_CRASHES:
        logger.critical("Application crashed too many times. Aborting restart.")

    logger.info("Launcher exiting.")

def _bootstrap(argv=None):
    """Process-entry bootstrap; testable so the no-multiprocessing guarantee is asserted.

    The one-shot ``--clear-screen-reader-flag`` and ``--reset-first-use-hints``
    shortcuts must short-circuit BEFORE any multiprocessing setup
    (``freeze_support`` / ``set_start_method``) so the recovery paths never
    touch the multiprocessing machinery. On either shortcut path this calls
    ``main(argv)`` directly and lets it perform the action and exit; on the
    normal path it configures multiprocessing first, then delegates to
    ``main(argv)``.

    ``argv`` defaults to ``sys.argv``. Extracted from the ``__main__`` block so
    a unit test can assert that ``set_start_method`` is NOT called on the clear
    path and IS called once on the normal path.
    """

    if argv is None:
        argv = sys.argv

    if _clear_screen_reader_flag_intent(argv) or _reset_first_use_hints_intent(argv):
        main(argv)
        return

    multiprocessing.freeze_support()
    multiprocessing.set_start_method('spawn', force=True)
    main(argv)


if __name__ == "__main__":
    _bootstrap()