"""Windows thread priority elevation for real-time audio threads.

The STT audio pipeline needs only a few percent of one core, but it must get
that CPU *on time*: when the whole machine is saturated by bulk compute (LLM
inference, parallel agent sessions, builds), a normal-priority consumer thread
can go unscheduled for seconds, the capture queue fills, and audio frames are
dropped in bursts (wh-stt-audio-consumer-behind-realtime). Elevating the
capture and consumer threads lets the OS scheduler protect the audio path
without starving anything else -- the threads sleep most of every 30ms frame.

Levels:
  - "highest":       THREAD_PRIORITY_HIGHEST (2) -- the per-frame consumer loop
  - "time_critical": THREAD_PRIORITY_TIME_CRITICAL (15) -- the capture thread

All functions are safe to call on any platform: on non-Windows they are
no-ops, and OS-level failures are logged and reported via the return value,
never raised.
"""
import ctypes
import logging
import sys
from typing import Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Win32 THREAD_PRIORITY_TIME_CRITICAL. Public so callers can compare a
# measured priority (get_current_thread_priority) against the ceiling.
THREAD_PRIORITY_TIME_CRITICAL = 15

_LEVELS = {
    "highest": 2,        # THREAD_PRIORITY_HIGHEST
    "time_critical": THREAD_PRIORITY_TIME_CRITICAL,
}

_THREAD_PRIORITY_ERROR_RETURN = 0x7FFFFFFF

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE is pointer-sized: c_void_p keeps this correct on 64-bit Python.
    _kernel32.GetCurrentThread.restype = ctypes.c_void_p
    _kernel32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    _kernel32.SetThreadPriority.restype = ctypes.c_int
    _kernel32.GetThreadPriority.argtypes = (ctypes.c_void_p,)
    _kernel32.GetThreadPriority.restype = ctypes.c_int
else:  # pragma: no cover - project targets Windows
    _kernel32 = None


def elevate_current_thread(level: str,
                           log_sink: Optional[Callable] = None) -> bool:
    """Raise the calling thread's scheduling priority.

    Args:
        level: "highest" or "time_critical".
        log_sink: Where the two failure lines below go. None writes
            them here and now, which is right for the two callers on
            ordinary threads -- google_stt_server's consumer loop and
            the WinRT frame-poll loop. A caller on a real-time thread
            passes a sink taking (logger, level, message), so the
            record is created later on a thread that may block.
            crewcut: NO caller passes a sink today. The one that did was
            MicrophoneStream._callback, deleted with the PortAudio
            capture path (wh-portaudio-capture-removal); grep for
            "elevate_current_thread" under services/stt_providers finds
            two callers, winrt_capture.py:592 and google_stt_server/
            main.py:1787, and neither passes this argument. The
            parameter is kept because the next real-time caller needs
            it and the WinRT frame-poll loop may yet become one, and
            because tests/test_capture_log_sink.py keeps two
            mutation-gate catchers alive on it
            (elevation-warning-ignores-the-sink and
            elevation-warning-dropped-instead-of-deferred in
            tests/mutation_gate_capture_frame_loss.py). To remove it,
            delete this parameter, both arms of warn(), that test file
            and those two gate entries. These two arms
            were the only logging calls in this module the deleted
            caller could reach:
            register_current_thread_mmcss returns its reason in
            MmcssRegistration.error instead of logging, the avrt.dll
            warning happens once at import, and the revert's warning
            is reached from the stream-finished callback rather than
            from frame delivery (wh-audio-callback-log.2.5).

    Returns:
        True if the priority was set, False if unavailable or the OS refused.

    Raises:
        ValueError: if level is not a known priority name (a programming
            error, unlike OS failures which are reported via return value).
    """
    if level not in _LEVELS:
        raise ValueError(
            f"unknown priority level {level!r} (expected one of {sorted(_LEVELS)})"
        )

    def warn(message: str) -> None:
        # Formatted here rather than left to the logging call's own
        # %-style arguments, because a sink is handed a finished
        # string. Both arms are failure paths, so the formatting a
        # suppressed level would have saved is not on any hot path.
        if log_sink is None:
            logger.warning(message)
        else:
            log_sink(logger, logging.WARNING, message)

    if _kernel32 is None:
        return False
    try:
        handle = _kernel32.GetCurrentThread()
        ok = bool(_kernel32.SetThreadPriority(handle, _LEVELS[level]))
        if not ok:
            warn(f"[priority] SetThreadPriority({level}) failed "
                 f"(winerror {ctypes.get_last_error()})")
        return ok
    except Exception as e:  # never break audio because elevation failed
        warn(f"[priority] thread priority elevation unavailable: {e}")
        return False


def get_current_thread_priority() -> int | None:
    """Return the calling thread's priority value, or None if unavailable."""
    if _kernel32 is None:
        return None
    try:
        handle = _kernel32.GetCurrentThread()
        value = _kernel32.GetThreadPriority(handle)
        if value == _THREAD_PRIORITY_ERROR_RETURN:
            return None
        return value
    except Exception:
        return None


# Win32 HIGH_PRIORITY_CLASS. The process class matters as much as the thread
# priority: a Task Scheduler launch hands the whole tree Below Normal, which
# Windows propagates to every child, and a Below Normal process class
# undercuts the thread elevation above (wh-process-priority-durable).
HIGH_PRIORITY_CLASS = 0x00000080

# The classes Windows defines, by their documented values. Kept as names
# because a bare 0x4000 in a log line says nothing to the person reading it,
# and the whole reason the class is logged is to make a Below Normal launch
# recognisable at a glance.
PRIORITY_CLASS_NAMES = {
    0x00000040: "IDLE",
    0x00004000: "BELOW_NORMAL",
    0x00000020: "NORMAL",
    0x00008000: "ABOVE_NORMAL",
    HIGH_PRIORITY_CLASS: "HIGH",
    0x00000100: "REALTIME",
}

if _kernel32 is not None:
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    _kernel32.SetPriorityClass.restype = ctypes.c_int
    # A DWORD return: c_uint32 keeps ABOVE_NORMAL (0x8000) and BELOW_NORMAL
    # (0x4000) exact, where a default c_int would still be correct today but
    # would sign-extend any future class with the high bit set.
    _kernel32.GetPriorityClass.argtypes = (ctypes.c_void_p,)
    _kernel32.GetPriorityClass.restype = ctypes.c_uint32


def elevate_current_process() -> bool:
    """Raise the calling process's priority class to High.

    A process may raise its own class up to High without administrator
    rights. Returns True if the class was set, False if unavailable or the
    OS refused -- OS failures are logged, never raised, matching
    elevate_current_thread.
    """
    # crewcut: High for a CPU-bound provider lets active-decode threads
    # preempt every Normal-class application, so on a 2-4 core machine other
    # programs turn sluggish while inference runs (only the balance-set
    # manager's anti-starvation boosts keep them moving). Accepted: speech
    # is the product's primary input and must stay responsive under load,
    # only one provider runs at a time, and any class above Normal starves
    # the same way (Windows scheduling has no cross-class fairness, so
    # ABOVE_NORMAL is not a middle ground) while Normal gives no margin over
    # bulk compute. To remove: add a per-provider class constant here and
    # re-verify the pipeline under a saturated CPU
    # (wh-process-priority-durable.1.3).
    if _kernel32 is None:
        return False
    try:
        handle = _kernel32.GetCurrentProcess()
        ok = bool(_kernel32.SetPriorityClass(handle, HIGH_PRIORITY_CLASS))
        if not ok:
            logger.warning(
                "[priority] SetPriorityClass(HIGH) failed (winerror %d)",
                ctypes.get_last_error(),
            )
        return ok
    except Exception as e:  # never break the provider because elevation failed
        logger.warning("[priority] process priority elevation unavailable: %s", e)
        return False


def get_current_process_priority_class() -> int | None:
    """Return the calling process's priority class, or None if unavailable.

    The companion of get_current_thread_priority, and needed with it: a
    thread priority is not an absolute scheduling position. Windows folds the
    process class into the base priority, so a time-critical thread inside a
    Below Normal process is scheduled below a Normal-class thread at 8. A log
    line carrying only the thread number cannot tell a healthy launch from a
    Task Scheduler one (wh-process-priority-durable), which is why the MMCSS
    line prints both (boss ruling 2026-09-03 13:3x, inline).
    """
    if _kernel32 is None:
        return None
    try:
        value = _kernel32.GetPriorityClass(_kernel32.GetCurrentProcess())
        # GetPriorityClass answers 0 on failure, and 0 is not a class.
        return value or None
    except Exception:
        return None


def describe_priority_class(value: int | None) -> str:
    """Render a priority class for a log line, name first, number after."""
    if value is None:
        return "unavailable"
    return f"{PRIORITY_CLASS_NAMES.get(value, 'unknown')} ({value:#x})"


# MMCSS (the Multimedia Class Scheduler Service). SetThreadPriority above
# raises a thread inside the ordinary scheduler, which still gives the
# scheduler the freedom to leave the thread unscheduled when the machine is
# busy. MMCSS is the separate path the Windows audio engine and audio drivers
# themselves use: a registered thread gets a guaranteed share of each
# scheduling period. Measured on Ikon 2026-09-03 (wh-stt-load-metrics.3): an
# independent WASAPI capture from the same microphone had 0 overflows in 62
# ten-second buckets while the provider's own capture lost up to 60 frames per
# minute over the same minutes, so the provider's callback thread is the part
# that is not being scheduled on time.
#
# The registration is per-thread and must be made ON the thread it protects,
# and released from that same thread, so the caller is the capture thread
# itself rather than any setup code. That thread was the PortAudio callback
# until the PortAudio path was deleted (wh-portaudio-capture-removal); it is
# the WinRT capture thread now.
AVRT_TASK_PRO_AUDIO = "Pro Audio"


class MmcssRegistration(NamedTuple):
    """The result of one AvSetMmThreadCharacteristicsW call.

    Attributes:
        task: The MMCSS task name that was requested.
        handle: The MMCSS handle on success, None on any failure.
        error: A short reason on failure, None on success. It is carried here
            rather than logged, because the only caller runs on a real-time
            audio thread that must not take a logging-handler lock
            (wh-sounddevice-starvation-parity.3.2); that caller hands the text
            to its consumer thread to log.
    """
    task: str
    handle: Optional[int]
    error: Optional[str]


if sys.platform == "win32":
    try:
        _avrt = ctypes.WinDLL("avrt", use_last_error=True)
    except OSError as _avrt_error:  # pragma: no cover - avrt.dll ships in Vista+
        logger.warning("[priority] avrt.dll unavailable: %s", _avrt_error)
        _avrt = None
    else:
        # HANDLE return and LPDWORD argument are both pointer-sized or
        # 32-bit-exact: c_void_p and POINTER(c_uint32) keep this correct on
        # 64-bit Python, where a default c_int return would truncate the
        # handle to its low 32 bits and make the revert pass a bad pointer.
        _avrt.AvSetMmThreadCharacteristicsW.argtypes = (
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
        )
        _avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        _avrt.AvRevertMmThreadCharacteristics.argtypes = (ctypes.c_void_p,)
        _avrt.AvRevertMmThreadCharacteristics.restype = ctypes.c_int
else:  # pragma: no cover - project targets Windows
    _avrt = None


def register_current_thread_mmcss(
    task: str = AVRT_TASK_PRO_AUDIO,
) -> MmcssRegistration:
    r"""Register the calling thread with MMCSS under the named task.

    Must be called from the thread being protected: MMCSS records the
    characteristics against the calling thread, not against a handle passed
    in.

    Args:
        task: An MMCSS task name from the registry key
            HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia
            \SystemProfile\Tasks. "Pro Audio" is the lowest-latency audio
            task; "Audio" is the ordinary one.

    Returns:
        An MmcssRegistration. Never raises: an unavailable avrt.dll, a
        refusal by the OS, and an unexpected ctypes failure all come back as
        a handle of None with a reason in .error, because audio capture must
        continue whether or not the scheduling request succeeded.
    """
    if _avrt is None:
        return MmcssRegistration(task, None, "avrt.dll unavailable")
    try:
        # The task index must be zero on a thread's first registration;
        # AvSetMmThreadCharacteristicsW writes the assigned index back.
        task_index = ctypes.c_uint32(0)
        handle = _avrt.AvSetMmThreadCharacteristicsW(
            task, ctypes.byref(task_index))
        if not handle:
            # ctypes returns None for a NULL c_void_p and 0 is equally a
            # failure, so this must be a falsiness test, not "is not None".
            return MmcssRegistration(
                task, None, f"winerror {ctypes.get_last_error()}")
        return MmcssRegistration(task, handle, None)
    except Exception as e:  # never break audio because scheduling failed
        return MmcssRegistration(task, None, str(e))


def revert_current_thread_mmcss(handle: int) -> bool:
    """Release an MMCSS registration made by this same thread.

    Args:
        handle: The handle from a successful register_current_thread_mmcss.

    Returns:
        True if MMCSS released the registration, False if unavailable or the
        OS refused. Never raises, for the same reason as the registration.
    """
    if _avrt is None:
        return False
    try:
        return bool(_avrt.AvRevertMmThreadCharacteristics(handle))
    except Exception as e:
        logger.warning("[priority] MMCSS revert failed: %s", e)
        return False
