"""Tests for TraceIdFilter integration in logging_setup.

Contract (post wh-anai counter-proposal acceptance):

- TraceIdFilter is attached ONLY to producer-side root handlers (the
  QueueHandler and the ErrorNotificationHandler). It runs in the
  emitter's thread, where current_trace_id is meaningful.
- Listener-side handlers (stream, file) DO NOT carry the filter.
  current_trace_id read from the listener thread is always the empty
  default and would clobber the producer's stamp.
- The filter is unconditional: it always overwrites record.trace_id
  with the current ContextVar value. Externally supplied trace_ids
  (e.g., logger.info(..., extra={"trace_id": "stale"})) are replaced.
- Records synthesised inside the listener (drop-summary records) set
  record.trace_id manually, so the formatter never KeyErrors despite
  the absence of a listener-side filter.
"""

import logging

from unittest.mock import patch

from utils import logging_setup
from utils.trace_context import set_trace, current_trace_id, TraceIdFilter


def _call_setup_logging(config):
    """Call setup_logging with file handler and notifier worker disabled."""
    from utils.logging_setup import setup_logging

    logging_setup.shutdown_logging()

    with patch(
        "utils.logging_setup.ConcurrentRotatingFileHandler",
        side_effect=OSError("mocked out"),
    ):
        with patch(
            "utils.logging_setup.ErrorNotificationHandler",
            side_effect=OSError("mocked out"),
        ):
            setup_logging(config)


def _teardown():
    logging_setup.shutdown_logging()


class TestTraceIdInLogFormat:
    """Verify setup_logging wires TraceIdFilter on the producer side only."""

    def teardown_method(self, method):
        _teardown()

    def test_root_handlers_carry_trace_filter(self):
        """Every root handler stamps trace_id on the producer thread."""
        _call_setup_logging({"LOG_LEVEL": "DEBUG"})

        root = logging.getLogger()
        for handler in root.handlers:
            filter_names = [type(f).__name__ for f in handler.filters]
            assert "TraceIdFilter" in filter_names, (
                f"Root handler {handler} missing TraceIdFilter, has: {handler.filters}"
            )

    def test_listener_handlers_do_not_carry_trace_filter(self):
        """Listener-thread handlers must NOT carry TraceIdFilter."""
        _call_setup_logging({"LOG_LEVEL": "DEBUG"})

        listener = logging_setup.get_listener()
        assert listener is not None
        for handler in listener.handlers:
            filter_names = [type(f).__name__ for f in handler.filters]
            assert "TraceIdFilter" not in filter_names, (
                f"Listener handler {handler} must not carry TraceIdFilter "
                f"(would clobber producer trace_id from listener thread)"
            )

    def test_format_includes_trace_id(self):
        """Listener-owned formatters include the trace_id placeholder."""
        _call_setup_logging({"LOG_LEVEL": "DEBUG"})

        listener = logging_setup.get_listener()
        assert listener is not None
        formats = [
            h.formatter._fmt
            for h in listener.handlers
            if h.formatter is not None
        ]
        assert formats, "Listener has no formatted handlers"
        assert all("trace_id" in fmt for fmt in formats), (
            f"Listener formatter missing trace_id placeholder: {formats}"
        )

    def test_trace_id_in_formatted_listener_output(self):
        """A producer-stamped record renders trace_id in listener output."""
        _call_setup_logging({"LOG_LEVEL": "DEBUG"})

        set_trace("T-000042")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        # Apply the producer-side filter (root QueueHandler holds one).
        TraceIdFilter().filter(record)

        listener = logging_setup.get_listener()
        assert listener is not None
        handler = next(
            h for h in listener.handlers if h.formatter is not None
        )
        formatted = handler.formatter.format(record)
        assert "T-000042" in formatted, (
            f"trace_id not found in formatted output: {formatted}"
        )

    def test_filter_overwrites_stale_extra_trace_id(self):
        """A pre-existing trace_id (e.g., from `extra=`) is replaced unconditionally.

        wh-anai.5 regression: the filter must not preserve stale values.
        """
        current_trace_id.set("T-current")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="msg",
            args=(),
            exc_info=None,
        )
        record.trace_id = "T-STALE"  # type: ignore[attr-defined]

        TraceIdFilter().filter(record)

        assert record.trace_id == "T-current", (  # type: ignore[attr-defined]
            "TraceIdFilter must overwrite a stale trace_id with the current "
            f"ContextVar value, not preserve it. Got: {record.trace_id}"  # type: ignore[attr-defined]
        )

    def test_filter_stamps_when_attribute_missing(self):
        """Records without a prior trace_id get the current ContextVar value."""
        current_trace_id.set("T-fresh")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="msg",
            args=(),
            exc_info=None,
        )

        TraceIdFilter().filter(record)

        assert record.trace_id == "T-fresh"  # type: ignore[attr-defined]
