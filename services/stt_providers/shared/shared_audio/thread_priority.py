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

logger = logging.getLogger(__name__)

_LEVELS = {
    "highest": 2,        # THREAD_PRIORITY_HIGHEST
    "time_critical": 15,  # THREAD_PRIORITY_TIME_CRITICAL
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


def elevate_current_thread(level: str) -> bool:
    """Raise the calling thread's scheduling priority.

    Args:
        level: "highest" or "time_critical".

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
    if _kernel32 is None:
        return False
    try:
        handle = _kernel32.GetCurrentThread()
        ok = bool(_kernel32.SetThreadPriority(handle, _LEVELS[level]))
        if not ok:
            logger.warning(
                "[priority] SetThreadPriority(%s) failed (winerror %d)",
                level, ctypes.get_last_error(),
            )
        return ok
    except Exception as e:  # never break audio because elevation failed
        logger.warning("[priority] thread priority elevation unavailable: %s", e)
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
