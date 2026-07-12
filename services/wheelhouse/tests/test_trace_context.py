"""Tests for trace_context module -- ContextVar-based trace ID threading."""

import logging

from utils.trace_context import (
    current_trace_id,
    trace_start_time,
    set_trace,
    get_trace_id,
    elapsed_ms,
    TraceIdFilter,
)


class TestSetAndGetTrace:
    """set_trace / get_trace_id round-trip."""

    def test_set_and_get(self):
        set_trace("T-000001")
        assert get_trace_id() == "T-000001"

    def test_default_is_empty_string(self):
        current_trace_id.set("")
        assert get_trace_id() == ""

    def test_overwrite(self):
        set_trace("T-000001")
        set_trace("T-000002")
        assert get_trace_id() == "T-000002"


class TestElapsedMs:
    """elapsed_ms returns non-negative float relative to set_trace call."""

    def test_non_negative(self):
        set_trace("T-000010")
        assert elapsed_ms() >= 0.0

    def test_monotonically_increasing(self):
        set_trace("T-000010")
        first = elapsed_ms()
        second = elapsed_ms()
        assert second >= first

    def test_resets_on_new_trace(self):
        import time

        set_trace("T-000010")
        time.sleep(0.05)
        old_elapsed = elapsed_ms()
        set_trace("T-000011")
        new_elapsed = elapsed_ms()
        # New trace should have a much smaller elapsed than old one
        assert new_elapsed < old_elapsed


class TestTraceIdFilter:
    """TraceIdFilter attaches trace_id to LogRecords."""

    def test_attaches_trace_id(self):
        set_trace("T-000099")
        f = TraceIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        result = f.filter(record)
        assert result is True
        assert record.trace_id == "T-000099"

    def test_empty_default(self):
        current_trace_id.set("")
        f = TraceIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        f.filter(record)
        assert record.trace_id == ""

    def test_works_with_logger(self, caplog):
        """TraceIdFilter integrates with stdlib logging."""
        set_trace("T-000050")
        test_logger = logging.getLogger("trace_filter_test")
        filt = TraceIdFilter()
        test_logger.addFilter(filt)
        try:
            with caplog.at_level(logging.INFO, logger="trace_filter_test"):
                test_logger.info("traced message")
            assert len(caplog.records) == 1
            assert caplog.records[0].trace_id == "T-000050"
        finally:
            test_logger.removeFilter(filt)
