"""Process priority class elevation for WheelHouse processes.

A Task Scheduler launch runs its process at Below Normal priority (task XML
Priority default 7), and every child process inherits that class -- Windows
propagates Below Normal (and Idle) classes to children, unlike Normal. A
Below Normal speech pipeline goes unscheduled for seconds whenever the
machine is saturated by bulk compute, and thread-level elevation
(shared_audio.thread_priority in the STT providers) cannot compensate for
the process class (wh-process-priority-durable, 2026-08-28).

Each latency-sensitive process therefore raises its own priority class to
High at startup: the launcher/main process, Logic, Input, and GUI. A process
may raise its own class up to High without administrator rights; only
Realtime needs elevation. The by-hand High settings were verified working on
2026-08-28 under a fully saturated CPU.

Elevation failure is logged and never raised -- the application must start
identically on any platform and under any refusal, matching the guarantee
of shared_audio.thread_priority.
"""
import logging
import sys

import psutil

logger = logging.getLogger(__name__)


def elevate_process_priority() -> bool:
    """Raise the calling process's priority class to High.

    Returns:
        True if the class was set, False if unavailable or the OS refused.
        Non-Windows platforms always return False (no HIGH_PRIORITY_CLASS).
    """
    if sys.platform != "win32":
        return False
    try:
        psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)
        logger.info("[priority] Process priority class raised to High")
        return True
    except Exception as e:  # never break startup because elevation failed
        logger.warning(
            "[priority] process priority elevation failed: %s", e
        )
        return False
