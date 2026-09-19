"""Tests for the summary logic of scripts/probe_settle_events.py.

Review findings wh-overlay-slow-uia-stale-badges.10.1.7 (the pre-click
baseline must not count as a settled pair), .10.1.8 (the summary must
report the option 3 result, with the UI Automation event veto applied to
each matching pair's observation window), and .10.1.9 (a reader thread
still alive after its join timeout is a failed run, not a success).

The probe is a developer measurement tool; these tests pin only the parts
whose defects would mislead a future measurement run.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any

PROBE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "probe_settle_events.py"
)
_spec = importlib.util.spec_from_file_location("probe_settle_events", PROBE_PATH)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def read_record(
    index: int,
    start_ms: float,
    end_ms: float,
    matches_previous: bool,
) -> dict[str, Any]:
    return {
        "index": index,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "count": 10,
        "added_since_previous": 0,
        "removed_since_previous": 0,
        "matches_previous": matches_previous,
    }


def uia_event(t_ms: float, name: str = "StructureChanged") -> dict[str, Any]:
    return {"t_ms": t_ms, "source": "uia", "event": name}


def win_event(t_ms: float) -> dict[str, Any]:
    return {"t_ms": t_ms, "source": "winevent", "event": "EVENT_OBJECT_REORDER"}


class TestBaselineIsNotAPair:
    def test_first_post_click_read_matching_baseline_does_not_settle(self) -> None:
        # Run-2 shape (probe-settle-20260827-095814.json): read 0 matched
        # the pre-click baseline and the old summary reported
        # settled_at_ms=234 from a single post-click read.
        reads = [
            read_record(0, 0.0, 234.0, True),
            read_record(1, 240.0, 470.0, False),
            read_record(2, 480.0, 700.0, True),
        ]
        summary = probe.summarize(reads, [], 1500, True)
        assert summary["settled_at_ms"] == 700.0

    def test_no_change_run_settles_on_the_first_post_click_pair(self) -> None:
        reads = [
            read_record(0, 0.0, 234.0, True),
            read_record(1, 240.0, 470.0, True),
        ]
        summary = probe.summarize(reads, [], 1500, True)
        assert summary["settled_at_ms"] == 470.0
        assert summary["option3_settled_at_ms"] == 470.0


class TestOption3Summary:
    def test_events_in_the_observation_window_void_the_pair(self) -> None:
        # Run-3 shape (probe-settle-20260827-100746.json): the pair over
        # reads 1-2 matched at 813 ms while StructureChanged events landed
        # inside its 328-813 ms observation window; the tree settled later.
        reads = [
            read_record(0, 0.0, 328.0, False),
            read_record(1, 328.0, 600.0, False),
            read_record(2, 600.0, 813.0, True),
            read_record(3, 850.0, 1080.0, False),
            read_record(4, 1100.0, 1300.0, True),
        ]
        events = [uia_event(400.0), uia_event(500.0)]
        summary = probe.summarize(reads, events, 1500, True)
        assert summary["settled_at_ms"] == 813.0
        assert summary["option3_settled_at_ms"] == 1300.0
        assert summary["option3_voided_pairs"] == 1
        assert summary["option3_settled_within_max"] is True

    def test_windows_events_and_property_changes_do_not_void(self) -> None:
        # The detector subscribes to the four UI Automation events only;
        # Windows events and the opt-in PropertyChanged stream are not
        # part of the option 3 veto.
        reads = [
            read_record(0, 0.0, 200.0, False),
            read_record(1, 200.0, 400.0, False),
            read_record(2, 400.0, 600.0, True),
        ]
        events = [win_event(450.0), uia_event(500.0, name="PropertyChanged")]
        summary = probe.summarize(reads, events, 1500, True)
        assert summary["option3_settled_at_ms"] == 600.0
        assert summary["option3_voided_pairs"] == 0

    def test_never_quiet_run_reports_no_option3_settle(self) -> None:
        reads = [
            read_record(0, 0.0, 200.0, False),
            read_record(1, 200.0, 400.0, True),
        ]
        events = [uia_event(300.0)]
        summary = probe.summarize(reads, events, 1500, True)
        assert summary["option3_settled_at_ms"] is None
        assert summary["option3_settled_within_max"] is False
        assert summary["option3_voided_pairs"] == 1


class TestOption3Availability:
    # Review finding wh-overlay-slow-uia-stale-badges.10.1.11: a run with
    # no registered veto subscription measured only the plain two-read
    # fallback and must not print an option 3 conclusion.
    MATCHING = [
        read_record(0, 0.0, 200.0, False),
        read_record(1, 200.0, 400.0, False),
        read_record(2, 400.0, 600.0, True),
    ]

    def test_no_registered_veto_source_reports_option3_unavailable(self) -> None:
        summary = probe.summarize(self.MATCHING, [], 1500, False)
        assert summary["option3_available"] is False
        assert summary["option3_settled_at_ms"] is None
        assert summary["option3_settled_within_max"] is False

    def test_registered_veto_sources_report_option3_available(self) -> None:
        summary = probe.summarize(self.MATCHING, [], 1500, True)
        assert summary["option3_available"] is True
        assert summary["option3_settled_at_ms"] == 600.0


class TestOption3Truncation:
    # Review finding wh-overlay-slow-uia-stale-badges.10.1.12: once the
    # recorder cap drops events, the record is incomplete from the first
    # dropped time on; a quiet-looking window past that point proves
    # nothing.
    MATCHING = [
        read_record(0, 0.0, 200.0, False),
        read_record(1, 200.0, 400.0, True),
    ]

    def test_window_reaching_past_first_drop_cannot_settle(self) -> None:
        summary = probe.summarize(
            self.MATCHING, [], 1500, True, dropped_from_ms=300.0,
        )
        assert summary["option3_settled_at_ms"] is None
        assert summary["option3_truncated"] is True

    def test_drop_after_the_settling_window_does_not_truncate(self) -> None:
        summary = probe.summarize(
            self.MATCHING, [], 1500, True, dropped_from_ms=10000.0,
        )
        assert summary["option3_settled_at_ms"] == 400.0
        assert summary["option3_truncated"] is False


class TestRecorderDropTracking:
    def test_first_dropped_time_is_recorded(self) -> None:
        recorder = probe.Recorder()
        for _ in range(probe.MAX_RECORDED_EVENTS):
            recorder.add("winevent", "X", {})
        assert recorder.first_dropped_t_ms is None
        recorder.add("uia", "StructureChanged", {})
        assert recorder.first_dropped_t_ms == probe.BEFORE_CLICK_MS
        assert recorder.dropped == 1

    def test_first_dropped_time_is_the_minimum_dropped_offset(self) -> None:
        # Review finding wh-overlay-slow-uia-stale-badges.10.1.14: a
        # callback stamps its offset before taking the lock, so a
        # later-stamped callback can reach the full store first. The
        # truncation boundary must be the earliest dropped stamp, not the
        # first one through the lock.
        recorder = probe.Recorder()
        stamps = iter(
            [1.0] * probe.MAX_RECORDED_EVENTS + [302.0, 300.0]
        )
        recorder.offset_ms = lambda: next(stamps)  # type: ignore[method-assign]
        for _ in range(probe.MAX_RECORDED_EVENTS + 2):
            recorder.add("uia", "StructureChanged", {})
        assert recorder.first_dropped_t_ms == 300.0


class TestRecorderSnapshot:
    # Review finding wh-overlay-slow-uia-stale-badges.10.1.15: main must
    # consume one consistent snapshot taken under the recorder's lock,
    # and a callback must stamp its offset under that same lock so a
    # descheduled callback cannot hold an earlier stamp than the counted
    # truncation boundary.
    def test_offset_is_stamped_under_the_lock(self) -> None:
        recorder = probe.Recorder()
        held: list[bool] = []
        original = recorder.offset_ms

        def spying_offset() -> float:
            held.append(recorder._lock.locked())
            return original()

        recorder.offset_ms = spying_offset  # type: ignore[method-assign]
        recorder.add("uia", "StructureChanged", {})
        assert held == [True]

    def test_close_returns_a_snapshot_and_ignores_later_events(self) -> None:
        recorder = probe.Recorder()
        recorder.add("uia", "StructureChanged", {})
        events, dropped, dropped_from = recorder.close()
        assert len(events) == 1
        assert dropped == 0
        assert dropped_from is None
        recorder.add("uia", "StructureChanged", {})
        assert len(events) == 1
        assert len(recorder.events) == 1

    def test_close_snapshot_is_not_the_live_list(self) -> None:
        recorder = probe.Recorder()
        events, _, _ = recorder.close()
        recorder.events.append({"t_ms": 1.0})
        assert events == []


class TestReaderErrorSignaling:
    # Review finding wh-overlay-slow-uia-stale-badges.10.1.13.
    def test_baseline_exception_signals_baseline_done(self, monkeypatch) -> None:
        def boom() -> Any:
            raise OSError("window gone")

        monkeypatch.setattr(probe, "create_automation", boom)
        reads: list[dict[str, Any]] = []
        baseline: dict[str, Any] = {}
        start_event = threading.Event()
        stop_event = threading.Event()
        baseline_done = threading.Event()
        reader = threading.Thread(
            target=probe.read_loop,
            args=(
                0, probe.Recorder(), start_event, stop_event, reads,
                baseline, baseline_done,
            ),
            daemon=True,
        )
        reader.start()
        reader.join(timeout=10.0)
        assert not reader.is_alive()
        assert baseline_done.is_set()
        assert any("error" in r for r in reads)

    def test_failed_read_finds_the_error_record(self) -> None:
        assert probe.failed_read(
            [{"index": 0}, {"error": "OSError('gone')"}]
        ) is not None
        assert probe.failed_read([{"index": 0}]) is None


class TestReaderCompleted:
    def test_alive_reader_is_not_completed(self) -> None:
        release = threading.Event()
        reader = threading.Thread(target=release.wait, daemon=True)
        reader.start()
        try:
            assert probe.reader_completed(reader, 0.05) is False
        finally:
            release.set()

    def test_finished_reader_is_completed(self) -> None:
        release = threading.Event()
        reader = threading.Thread(target=release.wait, daemon=True)
        reader.start()
        release.set()
        assert probe.reader_completed(reader, 5.0) is True
