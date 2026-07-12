"""Tests for the per-call latency histogram around the text-target check.

The check runs once per dictated word. Adding a wall-clock timer around
every call lets production sessions report real per-call costs on shutdown
via the existing log stream, replacing the mock-only microbenchmark in
test_text_target_latency.py for the wh-mm39e (performance check on the
accept path) comparison.

The histogram is a module-level singleton at
ui.text_target._check_latency_histogram. record() collects samples;
snapshot() returns per-reason summaries with percentiles; reset()
clears every bucket for test isolation. An atexit handler at
ui.text_target._log_check_latency_summary emits one INFO log line per
reason at process shutdown.

These tests exercise the histogram directly and through evaluate so
the shutdown log line cannot regress without a test failure.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import uiautomation as auto

from ui import text_target
from ui.text_target import (
    TextTargetPredicate,
    _check_latency_histogram,
    _log_check_latency_summary,
    _percentile,
)


@pytest.fixture(autouse=True)
def reset_histogram():
    """Clear the module-level histogram before and after every test.

    Other suites (the latency-baseline harness, the soft-allow tests)
    drive evaluate calls that populate the histogram. Resetting on each
    test entry keeps assertions independent of test execution order;
    resetting on exit keeps a long pytest run from accumulating
    irrelevant samples.
    """
    _check_latency_histogram.reset()
    yield
    _check_latency_histogram.reset()


def _ctrl(*, control_type=auto.ControlType.EditControl,
          control_type_name="EditControl",
          class_name="Edit",
          has_text_pattern=True,
          is_focusable=True,
          is_enabled=True):
    """Build a mock UIA control matching the latency harness fixtures."""
    ctrl = MagicMock()
    ctrl.ControlType = int(control_type)
    ctrl.ControlTypeName = control_type_name
    ctrl.ClassName = class_name
    ctrl.IsKeyboardFocusable = is_focusable
    ctrl.IsEnabled = is_enabled

    def get_pattern(pid):
        if pid == auto.PatternId.TextPattern and has_text_pattern:
            return MagicMock(name="TextPattern")
        return None

    ctrl.GetPattern.side_effect = get_pattern
    return ctrl


class TestHistogramRecord:
    def test_first_record_creates_bucket(self):
        _check_latency_histogram.record("text_pattern_available", 42.5)
        snap = _check_latency_histogram.snapshot()
        assert "text_pattern_available" in snap
        bucket = snap["text_pattern_available"]
        assert bucket["count"] == 1
        assert bucket["mean_us"] == pytest.approx(42.5)
        assert bucket["min_us"] == pytest.approx(42.5)
        assert bucket["max_us"] == pytest.approx(42.5)

    def test_multiple_records_aggregate(self):
        for sample_us in (10.0, 20.0, 30.0):
            _check_latency_histogram.record("text_pattern_available", sample_us)
        bucket = _check_latency_histogram.snapshot()["text_pattern_available"]
        assert bucket["count"] == 3
        assert bucket["mean_us"] == pytest.approx(20.0)
        assert bucket["min_us"] == pytest.approx(10.0)
        assert bucket["max_us"] == pytest.approx(30.0)

    def test_distinct_reasons_get_distinct_buckets(self):
        _check_latency_histogram.record("text_pattern_available", 50.0)
        _check_latency_histogram.record("denylist_control_type", 2.0)
        snap = _check_latency_histogram.snapshot()
        assert set(snap.keys()) == {
            "text_pattern_available", "denylist_control_type",
        }
        assert snap["text_pattern_available"]["count"] == 1
        assert snap["denylist_control_type"]["count"] == 1

    def test_min_and_max_track_extremes(self):
        # Order should not matter for min/max.
        for sample_us in (50.0, 10.0, 100.0, 30.0):
            _check_latency_histogram.record("edit_control", sample_us)
        bucket = _check_latency_histogram.snapshot()["edit_control"]
        assert bucket["min_us"] == pytest.approx(10.0)
        assert bucket["max_us"] == pytest.approx(100.0)

    def test_reservoir_does_not_grow_unbounded(self):
        # Beyond _RESERVOIR_SIZE samples, the reservoir stays bounded.
        # Use a value larger than the constant to confirm the cap.
        big = text_target._RESERVOIR_SIZE * 4
        for i in range(big):
            _check_latency_histogram.record("text_pattern_available", float(i))
        bucket = _check_latency_histogram.snapshot()["text_pattern_available"]
        # The reservoir is hidden inside the bucket; we cannot access it
        # directly after snapshot, but the bucket's count must equal
        # every recorded sample (running total, NOT reservoir size).
        assert bucket["count"] == big
        # min and max are running -- the smallest and largest of every
        # sample we sent, regardless of the reservoir contents.
        assert bucket["min_us"] == pytest.approx(0.0)
        assert bucket["max_us"] == pytest.approx(big - 1)

    def test_reset_clears_all_buckets(self):
        _check_latency_histogram.record("text_pattern_available", 50.0)
        _check_latency_histogram.record("denylist_control_type", 2.0)
        _check_latency_histogram.reset()
        assert _check_latency_histogram.snapshot() == {}


class TestPercentileHelper:
    def test_empty_returns_zero(self):
        assert _percentile([], 50) == 0.0
        assert _percentile([], 99) == 0.0

    def test_single_sample_returns_that_sample(self):
        assert _percentile([7.5], 50) == pytest.approx(7.5)
        assert _percentile([7.5], 99) == pytest.approx(7.5)

    def test_known_percentiles_on_uniform_sequence(self):
        samples = list(range(1, 101))  # 1..100 inclusive
        # With 100 sorted samples and linear interpolation:
        # p50 at index (50/100) * 99 = 49.5 -> halfway between 50 and 51
        assert _percentile(samples, 50) == pytest.approx(50.5)
        # p95 at index 0.95 * 99 = 94.05 -> ~95.05
        assert _percentile(samples, 95) == pytest.approx(95.05)
        # p99 at index 0.99 * 99 = 98.01 -> ~99.01
        assert _percentile(samples, 99) == pytest.approx(99.01)

    def test_q_below_zero_raises_value_error(self):
        # deepseek review wh-hl3s.2.1: the docstring says q must be
        # between 0 and 100, but the implementation did not enforce
        # it. A negative q used to produce a negative idx and Python's
        # negative-index list lookup wrapped to the end of the list,
        # silently returning the wrong sample. Now it raises.
        with pytest.raises(ValueError, match="q must be between 0 and 100"):
            _percentile([1.0, 2.0, 3.0], -10.0)

    def test_q_above_one_hundred_raises_value_error(self):
        # deepseek review wh-hl3s.2.1: q > 100 used to raise a cryptic
        # IndexError from the out-of-bounds list lookup. Now it raises
        # a ValueError with a clear message.
        with pytest.raises(ValueError, match="q must be between 0 and 100"):
            _percentile([1.0, 2.0, 3.0], 200.0)

    def test_q_at_zero_and_one_hundred_are_accepted(self):
        # The guard's range is inclusive on both ends so q=0 and q=100
        # remain valid percentile requests.
        samples = list(range(1, 101))  # 1..100 inclusive
        assert _percentile(samples, 0.0) == pytest.approx(1.0)
        assert _percentile(samples, 100.0) == pytest.approx(100.0)

    def test_snapshot_includes_percentile_fields(self):
        for sample_us in (10.0, 20.0, 30.0, 40.0, 50.0):
            _check_latency_histogram.record("text_pattern_available", sample_us)
        bucket = _check_latency_histogram.snapshot()["text_pattern_available"]
        assert "p50_us" in bucket
        assert "p95_us" in bucket
        assert "p99_us" in bucket
        # Median of 10..50 is 30.
        assert bucket["p50_us"] == pytest.approx(30.0)


class TestEvaluateInstrumentation:
    def test_evaluate_records_into_histogram(self):
        # An evaluate call with a TextPattern-bearing control should
        # show up under the text_pattern_available reason.
        predicate = TextTargetPredicate()
        ctrl = _ctrl(has_text_pattern=True)
        v = predicate.evaluate(
            ctrl, class_name="Edit", process_name="notepad.exe",
        )
        assert v.reason == "text_pattern_available"
        snap = _check_latency_histogram.snapshot()
        assert "text_pattern_available" in snap
        assert snap["text_pattern_available"]["count"] == 1

    def test_evaluate_keys_buckets_by_reason(self):
        predicate = TextTargetPredicate()
        # Path 1: TextPattern accept.
        predicate.evaluate(
            _ctrl(has_text_pattern=True),
            class_name="Edit", process_name="notepad.exe",
        )
        # Path 2: Denylist hit.
        predicate.evaluate(
            _ctrl(
                control_type=auto.ControlType.MenuItemControl,
                control_type_name="MenuItemControl",
                class_name="MenuItem",
                has_text_pattern=False,
            ),
            class_name="MenuItem", process_name="notepad.exe",
        )
        # Path 3: No focused control.
        predicate.evaluate(
            None, class_name="", process_name="notepad.exe",
        )
        snap = _check_latency_histogram.snapshot()
        assert set(snap.keys()) >= {
            "text_pattern_available",
            "denylist_control_type",
            "no_focused_control",
        }

    def test_evaluate_records_exception_path(self, monkeypatch):
        # Local reviewer wh-hl3s finding 1: if _evaluate_impl raises,
        # the wrapper must still record into the histogram under the
        # synthetic "exception" reason. The pathological cases (UIA
        # reads raising past the inner except blocks, or a future
        # refactor that lets an exception escape) are exactly what we
        # want telemetry on. The exception itself must still propagate
        # so the caller sees the real failure -- the wrapper does not
        # swallow it.
        predicate = TextTargetPredicate()

        def boom(_self, _focused_control, *, class_name="", process_name=""):
            raise RuntimeError("simulated impl failure")

        monkeypatch.setattr(
            TextTargetPredicate, "_evaluate_impl", boom, raising=True,
        )

        with pytest.raises(RuntimeError, match="simulated impl failure"):
            predicate.evaluate(_ctrl(), class_name="x", process_name="y")

        snap = _check_latency_histogram.snapshot()
        assert "exception" in snap
        assert snap["exception"]["count"] == 1

    def test_evaluate_returns_same_verdict_with_or_without_timing(self):
        # Smoke check: the timing wrapper must not affect the answer
        # the predicate returns. Compare the wrapped evaluate to a
        # direct _evaluate_impl call against the same fixture.
        predicate = TextTargetPredicate()
        ctrl = _ctrl(has_text_pattern=True)
        wrapped = predicate.evaluate(
            ctrl, class_name="Edit", process_name="notepad.exe",
        )
        impl = predicate._evaluate_impl(
            ctrl, class_name="Edit", process_name="notepad.exe",
        )
        assert wrapped.reason == impl.reason
        assert wrapped.verdict == impl.verdict
        assert wrapped.control_type == impl.control_type
        assert wrapped.class_name == impl.class_name


class TestShutdownLog:
    def test_log_summary_swallows_errors_so_atexit_cannot_raise(
        self, monkeypatch, caplog,
    ):
        # Local reviewer wh-hl3s finding 2: the atexit handler must
        # not raise. A teardown-time failure (the queue-based log
        # listener already stopped, the log file handle already
        # closed) would otherwise show up on stderr from Python 3.10+
        # and look like a process crash to the launcher's child-exit
        # logs. The handler wraps its body in a broad except clause;
        # this test forces snapshot() to raise and confirms the
        # handler returns without propagating.
        def boom(self):
            raise RuntimeError("simulated teardown race")

        monkeypatch.setattr(
            type(_check_latency_histogram),
            "snapshot",
            boom,
            raising=True,
        )

        with caplog.at_level(logging.INFO, logger="ui.text_target"):
            _log_check_latency_summary()
        # Did not raise; that is the contract. No info-level latency
        # line is emitted because the snapshot fetch failed before
        # any per-reason iteration could run.
        info_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.INFO
            and "text-target check latency" in r.message
        ]
        assert info_messages == []

    def test_log_summary_emits_header_with_iso_timestamp(self, caplog):
        # Local reviewer wh-hl3s finding 6: the per-summary header
        # gives a log reader a way to attribute a summary block to a
        # specific process run. Confirm the header line appears once
        # per call and carries an ISO 8601 timestamp.
        import re
        _check_latency_histogram.record("text_pattern_available", 50.0)
        with caplog.at_level(logging.INFO, logger="ui.text_target"):
            _log_check_latency_summary()
        header_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.INFO
            and "summary at" in r.message
        ]
        assert len(header_messages) == 1
        # ISO 8601 with seconds resolution: 2026-05-14T07:00:00
        assert re.search(
            r"summary at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            header_messages[0],
        )

    def test_log_summary_emits_one_info_line_per_reason(self, caplog):
        # Seed the histogram with two reasons.
        for _ in range(5):
            _check_latency_histogram.record("text_pattern_available", 50.0)
        _check_latency_histogram.record("denylist_control_type", 2.0)

        with caplog.at_level(logging.INFO, logger="ui.text_target"):
            _log_check_latency_summary()

        # Filter to per-reason lines only -- the header "summary at"
        # line is tested separately in test_log_summary_emits_header.
        reason_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.INFO
            and r.message.startswith("text-target check latency: reason=")
        ]
        # One line per reason.
        assert len(reason_messages) == 2
        # Each line names the reason and the count.
        joined = " ".join(reason_messages)
        assert "reason=text_pattern_available" in joined
        assert "reason=denylist_control_type" in joined
        assert "count=5" in joined
        assert "count=1" in joined

    def test_log_summary_is_silent_on_empty_histogram(self, caplog):
        # No records, no log lines. A short-lived process (unit test,
        # CLI tool) must not flood the log with empty rows.
        with caplog.at_level(logging.INFO, logger="ui.text_target"):
            _log_check_latency_summary()
        info_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.INFO
            and "text-target check latency" in r.message
        ]
        assert info_messages == []

    def test_log_summary_skips_buckets_with_zero_count(
        self, caplog, monkeypatch,
    ):
        # codex review wh-hl3s.1.2: the earlier version of this test
        # recorded one real sample and never created a count=0 bucket,
        # so the stats["count"] == 0 guard could have been removed
        # without test failure. This version monkeypatches snapshot()
        # to return a zero-count bucket alongside a real one and asserts
        # only the real bucket emits a per-reason line.
        def fake_snapshot(_self):
            return {
                "text_pattern_available": {
                    "count": 5,
                    "mean_us": 50.0,
                    "min_us": 40.0,
                    "max_us": 60.0,
                    "p50_us": 50.0,
                    "p95_us": 58.0,
                    "p99_us": 60.0,
                },
                "zero_count_reason": {
                    "count": 0,
                    "mean_us": 0.0,
                    "min_us": 0.0,
                    "max_us": 0.0,
                    "p50_us": 0.0,
                    "p95_us": 0.0,
                    "p99_us": 0.0,
                },
            }

        monkeypatch.setattr(
            type(_check_latency_histogram),
            "snapshot",
            fake_snapshot,
            raising=True,
        )
        with caplog.at_level(logging.INFO, logger="ui.text_target"):
            _log_check_latency_summary()
        # Filter to per-reason lines only; the "summary at" header is
        # tested separately.
        reason_messages = [
            r.message for r in caplog.records
            if r.levelno == logging.INFO
            and r.message.startswith("text-target check latency: reason=")
        ]
        # The bucket with a real sample is logged; the count=0 bucket is
        # skipped, not silently logged as an all-zero row.
        assert len(reason_messages) == 1
        assert "reason=text_pattern_available" in reason_messages[0]
        joined = " ".join(reason_messages)
        assert "reason=zero_count_reason" not in joined


class TestEvaluateRecordFailure:
    """codex review wh-hl3s.1.1: telemetry record() failure must not
    change evaluate's return value or mask the _evaluate_impl exception.

    The fix wraps the record() call inside evaluate's finally block in
    its own try/except so a telemetry-side failure cannot affect the
    on-the-wire dictation path.
    """

    def test_record_failure_does_not_change_verdict(self, monkeypatch):
        # A successful evaluate must return its real verdict even when
        # the histogram record() call raises. The earlier code path
        # would have re-raised the record exception out of the finally
        # block and the caller would have seen no verdict at all.
        predicate = TextTargetPredicate()
        ctrl = _ctrl(has_text_pattern=True)

        def boom_record(self, reason, elapsed_us):
            raise RuntimeError("simulated record failure")

        monkeypatch.setattr(
            type(_check_latency_histogram),
            "record",
            boom_record,
            raising=True,
        )

        v = predicate.evaluate(
            ctrl, class_name="Edit", process_name="notepad.exe",
        )
        # evaluate must still return the normal verdict, not raise.
        assert v.reason == "text_pattern_available"
        assert v.verdict is True

    def test_record_failure_does_not_mask_impl_exception(self, monkeypatch):
        # When _evaluate_impl itself raises AND record() also raises,
        # the caller must see the _evaluate_impl exception (the real
        # bug), not the telemetry-side exception. The earlier code
        # path would have replaced the impl exception with the record
        # exception in the finally block.
        predicate = TextTargetPredicate()

        def boom_impl(_self, _focused_control, *, class_name="", process_name=""):
            raise RuntimeError("simulated impl failure")

        def boom_record(self, reason, elapsed_us):
            raise RuntimeError("simulated record failure")

        monkeypatch.setattr(
            TextTargetPredicate, "_evaluate_impl", boom_impl, raising=True,
        )
        monkeypatch.setattr(
            type(_check_latency_histogram),
            "record",
            boom_record,
            raising=True,
        )

        with pytest.raises(RuntimeError, match="simulated impl failure"):
            predicate.evaluate(_ctrl(), class_name="x", process_name="y")


class TestHistogramConcurrency:
    """codex review wh-hl3s.1.3: the histogram's lock is declared safe
    for cross-thread record/snapshot. The earlier test suite exercised
    only single-threaded calls; this class verifies the lock contract
    holds under contention.
    """

    def test_concurrent_record_and_snapshot_are_thread_safe(self):
        import threading as _th

        n_threads = 8
        per_thread = 200
        expected = n_threads * per_thread
        errors: list[BaseException] = []
        stop_snapshot = _th.Event()

        def writer():
            try:
                for i in range(per_thread):
                    _check_latency_histogram.record(
                        "text_pattern_available", float(i),
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                while not stop_snapshot.is_set():
                    _check_latency_histogram.snapshot()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        writers = [_th.Thread(target=writer) for _ in range(n_threads)]
        snap_thread = _th.Thread(target=reader)
        snap_thread.start()
        for t in writers:
            t.start()
        for t in writers:
            t.join(timeout=10.0)
            assert not t.is_alive(), "writer thread did not finish"
        stop_snapshot.set()
        snap_thread.join(timeout=5.0)
        assert not snap_thread.is_alive(), "reader thread did not finish"

        assert errors == []
        bucket = _check_latency_histogram.snapshot()["text_pattern_available"]
        # The integer count must equal the total number of record() calls
        # across every writer thread. A lost-update race on count would
        # show up as bucket["count"] < expected.
        assert bucket["count"] == expected
        # Running min and max stay bounded to the sample range each
        # writer thread used.
        assert bucket["min_us"] >= 0.0
        assert bucket["max_us"] <= float(per_thread - 1)
