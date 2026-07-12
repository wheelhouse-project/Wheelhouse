"""Trace ID threading via contextvars.

Provides a per-utterance trace_id that flows through the speech pipeline
via ContextVar, plus a logging Filter that attaches it to every LogRecord.
"""

import logging
import time
from contextvars import ContextVar

current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")
trace_start_time: ContextVar[float] = ContextVar("trace_start_time", default=0.0)


def set_trace(trace_id: str) -> None:
    """Set the current trace_id and reset the elapsed timer."""
    current_trace_id.set(trace_id)
    trace_start_time.set(time.perf_counter())


def get_trace_id() -> str:
    """Read the current trace_id (empty string if unset)."""
    return current_trace_id.get()


def elapsed_ms() -> float:
    """Milliseconds since set_trace was last called."""
    start = trace_start_time.get()
    if start == 0.0:
        return 0.0
    return (time.perf_counter() - start) * 1000.0


class TraceIdFilter(logging.Filter):
    """Logging filter that stamps the producer thread's trace_id onto a record.

    Unconditional overwrite: this filter is the single authority on
    trace_id. It must run on the PRODUCER thread (i.e., be attached to
    a root-level handler whose handle() executes in the emitter's
    thread). Listener-thread handlers MUST NOT carry this filter --
    ContextVars are per-thread and reading current_trace_id from the
    listener thread returns the default empty string, which would
    clobber the producer's value.

    Records synthesised inside the listener (e.g. drop-summary records)
    set record.trace_id manually before dispatching to listener-owned
    handlers, so the listener-side handlers never need this filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id.get()  # type: ignore[attr-defined]
        return True
