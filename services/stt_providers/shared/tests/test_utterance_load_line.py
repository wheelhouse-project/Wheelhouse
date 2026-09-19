"""Tests for the per-utterance load line a provider with no local engine writes.

wh-stt-load-metrics.4. The Parakeet provider reports each utterance's
capture and inference load from AudioProcessor. The Google provider has no
AudioProcessor and no local recognizer, so it needs the same line built from
what its own loop already holds: the capture counters, the queue depths it
samples every iteration, and the stall tracker it already runs.

The capture-difference rule is the one AudioProcessor already used; it moved
into shared_audio so both providers read one definition of it rather than
two copies that can drift.
"""
import pytest

from shared_audio.diagnostics import (
    CAPTURE_DELTA_KEYS,
    LoopStallTracker,
    UtteranceLoadMetrics,
    capture_deltas,
)


class TestTheCaptureDifferenceRule:
    """What a counter's change across one utterance is, and when there is
    no answer."""

    def test_a_counter_present_at_both_ends_is_the_difference(self):
        deltas = capture_deltas(
            {'overflow_count': 3, 'status_flags': 1, 'drops': 10},
            {'overflow_count': 7, 'status_flags': 5, 'drops': 12})
        assert deltas == {'overflow_count': 4, 'status_flags': 4, 'drops': 2}

    def test_a_counter_the_provider_does_not_keep_is_not_zero(self):
        """The WinRT capture path has no PortAudio status flags and no
        overflow count. This whole measurement exists to decide whether
        capture loss happened, and `overflow=0` is the reading that rules it
        out, so a provider that never counted must not be able to produce it.
        """
        deltas = capture_deltas({'drops': 1}, {'drops': 4})
        assert deltas['overflow_count'] == 'n/a'
        assert deltas['status_flags'] == 'n/a'
        assert deltas['drops'] == 3

    def test_a_value_that_is_not_a_count_is_not_a_difference(self):
        deltas = capture_deltas(
            {'drops': 'many', 'overflow_count': 0, 'status_flags': 0},
            {'drops': 4, 'overflow_count': 0, 'status_flags': 0})
        assert deltas['drops'] == 'n/a'

    def test_a_counter_that_went_backwards_reads_zero(self):
        """A provider restart mid-utterance is the only way this happens.
        A negative count would read as an impossible measurement.
        """
        deltas = capture_deltas(
            {'drops': 40, 'overflow_count': 0, 'status_flags': 0},
            {'drops': 2, 'overflow_count': 0, 'status_flags': 0})
        assert deltas['drops'] == 0

    def test_no_reading_at_either_end_answers_nothing(self):
        for at_open, at_end in (({}, None), (None, {}), (None, None)):
            deltas = capture_deltas(at_open, at_end)
            assert set(deltas) == set(CAPTURE_DELTA_KEYS)
            assert all(v == 'n/a' for v in deltas.values())


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _stats(overflow=0, status_flags=0, drops=0):
    return {'captured': 1, 'qsize': 0, 'max_q': 0,
            'overflow_count': overflow, 'status_flags': status_flags,
            'drops': drops}


class TestTheLineOneUtteranceProduces:
    def _metrics(self, stats_values, tracker=None):
        readings = list(stats_values)

        def read():
            return readings.pop(0) if readings else readings

        return UtteranceLoadMetrics(capture_stats=read,
                                    stall_tracker=tracker)

    def test_the_line_carries_every_field_in_order(self):
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=FakeClock())
        metrics = UtteranceLoadMetrics(
            capture_stats=lambda: _stats(overflow=2, status_flags=1, drops=5),
            stall_tracker=tracker)

        metrics.start()
        metrics.sample(10)
        metrics.sample(20)
        line = metrics.finish(7, 'GOOGLE_FINAL')

        assert line == (
            '[load-diag] utt=7 kind=GOOGLE_FINAL overflow=0 status_flags=0 '
            'drops=0 q_max=20 q_mean=15.0 q_n=2 stalls=0 stall_max_ms=0.0 '
            'engine_calls=n/a engine_ms_total=n/a engine_ms_max=n/a '
            'audio_ms=n/a engine_ratio=n/a')

    def test_the_capture_fields_are_the_change_across_the_utterance(self):
        readings = [_stats(overflow=2, status_flags=1, drops=5),
                    _stats(overflow=9, status_flags=4, drops=11)]
        metrics = UtteranceLoadMetrics(
            capture_stats=lambda: readings.pop(0), stall_tracker=None)

        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'overflow=7' in line
        assert 'status_flags=3' in line
        assert 'drops=6' in line

    def test_a_backend_that_counts_no_overflow_says_so(self):
        """The WinRT reading, end to end."""
        winrt = {'captured': 1, 'drops': 4, 'qsize': 0, 'max_q': 0}
        metrics = UtteranceLoadMetrics(capture_stats=lambda: dict(winrt),
                                       stall_tracker=None)

        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'overflow=n/a' in line
        assert 'status_flags=n/a' in line
        assert 'drops=0' in line

    def test_the_engine_fields_are_never_numbers(self):
        """Google runs the recognizer, so there is no local engine time.
        A 0 here would read as "the recognizer kept up", which nothing
        measured.
        """
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        for field in ('engine_calls', 'engine_ms_total', 'engine_ms_max',
                      'audio_ms', 'engine_ratio'):
            assert f'{field}=n/a' in line

    def test_the_queue_fields_describe_only_this_utterance(self):
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        metrics.start()
        metrics.sample(99)
        metrics.finish(1, 'GOOGLE_FINAL')

        metrics.start()
        metrics.sample(4)
        metrics.sample(8)
        line = metrics.finish(2, 'GOOGLE_FINAL')

        assert 'q_max=8' in line
        assert 'q_mean=6.0' in line
        assert 'q_n=2' in line

    def test_an_utterance_with_no_samples_says_it_took_none(self):
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'q_max=0 q_mean=0.0 q_n=0' in line

    def test_the_stall_fields_come_from_the_utterance_window(self):
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=tracker)

        tracker.record()
        clock.advance(4.0)
        tracker.record()            # a stall before this utterance began

        metrics.start()
        clock.advance(0.03)
        tracker.record()
        clock.advance(2.5)
        tracker.record()            # the only stall inside the utterance
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'stalls=1' in line
        assert 'stall_max_ms=2500.0' in line

    def test_the_periodic_window_still_counts_every_stall(self):
        """The utterance window must not empty the one the periodic
        [overflow-diag] block reports.
        """
        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=tracker)

        tracker.record()
        clock.advance(3.0)
        tracker.record()
        metrics.start()
        clock.advance(0.03)
        tracker.record()
        clock.advance(2.0)
        tracker.record()
        metrics.finish(1, 'GOOGLE_FINAL')

        assert tracker.snapshot_and_reset_window() == {
            'stalls': 2, 'max_gap_ms': 3000.0}

    def test_without_a_tracker_the_stall_fields_say_nothing(self):
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'stalls=n/a' in line
        assert 'stall_max_ms=n/a' in line


class TestTheLineNeverBreaksTheLoopItWatches:
    """This is a diagnostic. It observes the consumer loop and must never be
    able to stop it -- the same rule CaptureLoadReporter and the stall
    tracker's depth probe already follow.
    """

    def test_a_capture_reader_that_raises_costs_only_the_capture_fields(self):
        def boom():
            raise OSError('the device went away')

        metrics = UtteranceLoadMetrics(capture_stats=boom, stall_tracker=None)
        metrics.start()
        metrics.sample(12)
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'overflow=n/a status_flags=n/a drops=n/a' in line
        assert 'q_max=12' in line

    def test_a_reader_that_answers_with_the_wrong_shape_is_refused(self):
        metrics = UtteranceLoadMetrics(capture_stats=lambda: ['not', 'a dict'],
                                       stall_tracker=None)
        metrics.start()
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'overflow=n/a status_flags=n/a drops=n/a' in line

    def test_no_reader_at_all_still_produces_the_line(self):
        metrics = UtteranceLoadMetrics(capture_stats=None, stall_tracker=None)
        metrics.start()
        metrics.sample(3)
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'overflow=n/a status_flags=n/a drops=n/a' in line
        assert 'q_max=3' in line

    def test_a_finish_with_no_start_still_produces_the_line(self):
        """The loop can meet a finalization for an utterance whose start it
        did not see -- a stale trigger, or the first utterance after a
        restart. A traceback out of a diagnostic would end the consumer loop.
        """
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        line = metrics.finish(1, 'NO_TEXT_TIMEOUT')

        assert line.startswith('[load-diag] utt=1 kind=NO_TEXT_TIMEOUT ')
        assert 'overflow=n/a' in line

    def test_a_sample_that_is_not_a_depth_is_ignored(self):
        metrics = UtteranceLoadMetrics(capture_stats=lambda: _stats(),
                                       stall_tracker=None)
        metrics.start()
        metrics.sample(None)
        metrics.sample(5)
        line = metrics.finish(1, 'GOOGLE_FINAL')

        assert 'q_max=5 q_mean=5.0 q_n=1' in line


class TestTheLineTheParserReads:
    """The line has to survive the round trip, or the load-test tool cannot
    judge a Google run.
    """

    def test_the_tool_reads_back_what_the_provider_wrote(self):
        import sys
        from pathlib import Path
        tools = Path(__file__).resolve().parents[1] / 'tools'
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from stt_load_test import logparse

        clock = FakeClock()
        tracker = LoopStallTracker(stall_threshold_s=1.0, clock=clock)
        metrics = UtteranceLoadMetrics(
            capture_stats=lambda: _stats(drops=0), stall_tracker=tracker)
        metrics.start()
        metrics.sample(327)
        clock.advance(0.01)
        tracker.record()
        clock.advance(2.5)
        tracker.record()
        line = metrics.finish(7, 'GOOGLE_FINAL')

        parsed = logparse.parse_log(
            '2026-09-05 15:00:00,000 [INFO] MainProcess - main.py:1 - '
            'trace=none - ' + line)

        assert len(parsed.utterances) == 1
        read = parsed.utterances[0]
        assert read.utt == 7
        assert read.kind == 'GOOGLE_FINAL'
        assert read.q_max == 327
        assert read.stalls == 1
        assert read.stall_max_ms == pytest.approx(2500.0)
        assert read.engine_ratio is None
