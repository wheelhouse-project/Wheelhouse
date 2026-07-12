"""Tests for timing.py - Performance timing utilities.

Tests cover:
- log_perf_time step duration calculation
- log_perf_time total elapsed calculation
- Log message formatting
"""

import time
from unittest.mock import patch

import pytest


class TestLogPerfTime:
    """Tests for the log_perf_time function."""

    def test_calculates_step_duration(self):
        from utils.timing import log_perf_time

        # Simulate: step started 100ms ago, total started 500ms ago
        now = time.perf_counter()
        step_start = now - 0.1    # 100ms ago
        initial_time = now - 0.5  # 500ms ago

        with patch("utils.timing.time.perf_counter", return_value=now):
            with patch("utils.timing.logger") as mock_logger:
                log_perf_time("test step", step_start, initial_time)

        log_msg = mock_logger.info.call_args[0][0]
        assert "test step" in log_msg
        assert "PERF:" in log_msg
        assert "Step:" in log_msg
        assert "Total:" in log_msg

    def test_step_and_total_values_correct(self):
        from utils.timing import log_perf_time

        # Fixed timestamps for deterministic test
        end_time = 10.0
        step_start = 9.9       # 100ms step
        initial_time = 9.5     # 500ms total

        with patch("utils.timing.time.perf_counter", return_value=end_time):
            with patch("utils.timing.logger") as mock_logger:
                log_perf_time("op", step_start, initial_time)

        log_msg = mock_logger.info.call_args[0][0]
        # Step should be ~100ms, total ~500ms
        assert "100.00" in log_msg
        assert "500.00" in log_msg

    def test_message_padded_to_40_chars(self):
        from utils.timing import log_perf_time

        with patch("utils.timing.time.perf_counter", return_value=1.0):
            with patch("utils.timing.logger") as mock_logger:
                log_perf_time("short", 0.999, 0.5)

        log_msg = mock_logger.info.call_args[0][0]
        # Format uses {message:<40} so should pad
        assert "PERF: short" in log_msg
