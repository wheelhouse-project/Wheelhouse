"""The Google server wires its per-utterance load line to its own loop.

wh-stt-load-metrics.4. The line itself and its formatting are covered in
services/stt_providers/shared/tests/test_utterance_load_line.py. What is
covered here is only the wiring, which is where this measurement can
silently produce nothing: UtteranceLoadMetrics answers "n/a" for every
capture number when no reader is supplied, so an unwired server would log a
line that looks complete and says nothing.

Three things are wired, and each has its own way of going wrong:

  1. start_new_utterance opens the window. Without it every line reports
     the counters of the whole session so far.
  2. _finalize_utterance reads it and logs the line. Without it the numbers
     are collected and never printed.
  3. The consumer loop hands each iteration's queue depth to both the stall
     tracker and the line. Reading the queue twice would put two different
     depths in the two places that report it, so the reading is taken once
     and shared -- which is what sample_consumer_iteration exists to make
     testable.
  4. That same helper closes each iteration for the segment timer and
     opens the next. The order is the part that can silently go wrong:
     a breakdown read after the reopen describes an iteration that has
     not happened yet (wh-stt-load-metrics.4 G3).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from shared_audio.diagnostics import (
    CaptureLoadReporter, UtteranceLoadMetrics)

import main as google_main


@pytest.fixture
def logged():
    """Every message this module's logger emits.

    caplog cannot be used, and the house fixture in
    tests/test_startup_readiness.py says why: main() sets
    logger.propagate = False beside the WebSocket log handler, and
    caplog's handler sits on the root logger. A caplog assertion here
    passes when this file runs alone and fails once any test in the
    suite has run main() far enough to reach that line, which is how
    the ordering showed itself.
    """
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collect()
    google_main.logger.addHandler(handler)
    previous = google_main.logger.level
    google_main.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        google_main.logger.removeHandler(handler)
        google_main.logger.setLevel(previous)


class TestOneQueueReadingFeedsBothConsumers:
    def test_the_depth_reaches_the_line_and_the_tracker(self):
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=41)
        tracker = MagicMock()
        tracker.record = Mock(return_value=None)
        load = MagicMock()

        google_main.sample_consumer_iteration(mic, tracker, load)

        load.sample.assert_called_once_with(41)
        tracker.record.assert_called_once_with(41)

    def test_the_queue_is_read_once_for_both(self):
        """Two reads would report two different depths for one iteration,
        and the queue moves between them under exactly the load this line
        exists to measure.
        """
        mic = MagicMock()
        mic.get_queue_size = Mock(side_effect=[41, 999])
        tracker = MagicMock()
        tracker.record = Mock(return_value=None)
        load = MagicMock()

        google_main.sample_consumer_iteration(mic, tracker, load)

        assert mic.get_queue_size.call_count == 1

    def test_the_stall_message_is_passed_back(self):
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        tracker = MagicMock()
        tracker.record = Mock(return_value='[stall] consumer loop ...')

        assert google_main.sample_consumer_iteration(
            mic, tracker, None) == ['[stall] consumer loop ...']

    def test_a_quiet_iteration_says_nothing(self):
        """Almost every iteration. A line here would bury the run."""
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        tracker = MagicMock()
        tracker.record = Mock(return_value=None)

        assert google_main.sample_consumer_iteration(
            mic, tracker, None, MagicMock()) == []

    def test_without_a_line_the_tracker_still_runs(self):
        """The line is the new part; the stall tracker has been running on
        this loop since wh-stt-audio-consumer-behind-realtime and must not
        depend on it.
        """
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=7)
        tracker = MagicMock()
        tracker.record = Mock(return_value=None)

        google_main.sample_consumer_iteration(mic, tracker, None)

        tracker.record.assert_called_once_with(7)


def _manager(load_metrics=None):
    cfg = MagicMock()
    cfg.silence_finalize_ms = 800
    cfg.debug.log_lifecycle = False
    cfg.max_no_text_seconds = 30
    return google_main.UtteranceManager(
        cfg, MagicMock(), forwarder=None, load_metrics=load_metrics)


class TestTheUtteranceSeams:
    def test_a_new_utterance_opens_the_window(self):
        load = MagicMock()
        mgr = _manager(load)

        mgr.start_new_utterance()

        load.start.assert_called_once_with()

    def test_finalizing_reads_the_window_and_logs_the_line(self, logged):
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=GOOGLE_FINAL')
        mgr = _manager(load)
        mgr.start_new_utterance()

        mgr._finalize_utterance('GOOGLE_FINAL')

        load.finish.assert_called_once_with(1, 'GOOGLE_FINAL')
        assert '[load-diag] utt=1 kind=GOOGLE_FINAL' in logged

    def test_the_line_is_written_once(self, logged):
        """A duplicate would double every utterance in a parsed run."""
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=GOOGLE_FINAL')
        mgr = _manager(load)
        mgr.start_new_utterance()

        mgr._finalize_utterance('GOOGLE_FINAL')

        assert logged.count('[load-diag] utt=1 kind=GOOGLE_FINAL') == 1

    def test_the_reason_is_the_kind_the_line_reports(self):
        """NO_TEXT_TIMEOUT is the ending the 2026-09-05 runs produced, and
        telling those lines apart from a clean GOOGLE_FINAL is the whole
        point of joining a stall to an utterance.
        """
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] ...')
        mgr = _manager(load)
        mgr.start_new_utterance()

        mgr._finalize_utterance('NO_TEXT_TIMEOUT')

        assert load.finish.call_args[0][1] == 'NO_TEXT_TIMEOUT'

    def test_a_stale_finalization_writes_no_line(self):
        """_finalize_utterance returns early when the utterance is not
        active. A line written there would report an utterance that did not
        just end.
        """
        load = MagicMock()
        mgr = _manager(load)

        mgr._finalize_utterance('GOOGLE_FINAL')

        load.finish.assert_not_called()

    def test_a_server_without_the_line_still_finalizes(self):
        """load_metrics defaults to None: every existing construction of
        UtteranceManager passes two to four arguments.
        """
        mgr = _manager(None)
        mgr.start_new_utterance()

        mgr._finalize_utterance('GOOGLE_FINAL')

        assert mgr.state == google_main.UtteranceState.FINALIZED

    def test_a_line_writer_that_raises_does_not_end_the_utterance(self):
        """This is a diagnostic on the consumer loop's own thread. A
        traceback out of it would take the utterance, and then the loop,
        with it.
        """
        load = MagicMock()
        load.finish = Mock(side_effect=RuntimeError('the reader went away'))
        mgr = _manager(load)
        mgr.start_new_utterance()

        mgr._finalize_utterance('GOOGLE_FINAL')

        assert mgr.state == google_main.UtteranceState.FINALIZED

    def test_a_window_that_cannot_open_does_not_end_the_utterance(self):
        load = MagicMock()
        load.start = Mock(side_effect=RuntimeError('the reader went away'))
        mgr = _manager(load)

        mgr.start_new_utterance()

        assert mgr.state == google_main.UtteranceState.ACTIVE


class TestTheLoopBuildsTheLine:
    """The construction inside main() itself.

    main() is a 700-line function that opens a microphone and a Google
    stream; the existing tests reach it only on its early-return paths
    (--list-devices, mic_check). So these read the source. A source
    assertion is weak evidence of behaviour and strong evidence of
    presence, and presence is the failure this bead is about: an
    unwired reporter still prints a line, with every capture number
    reading n/a, and nothing in the log says it was never connected.
    """

    def _source(self):
        return Path(google_main.__file__).read_text(encoding='utf-8')

    def test_the_loop_builds_one_reporter(self):
        assert self._source().count(
            'utterance_load = UtteranceLoadMetrics(') == 1

    def test_the_reporter_reads_the_provider_capture_counters(self):
        """Without a reader every capture field on every line is n/a.

        Pinned to the UtteranceLoadMetrics construction itself rather
        than to the argument text. Since wh-stt-load-metrics.4 G1a the
        loop builds a second collaborator from the same
        capture_stats=mic.get_stats, so a bare search for that text
        passes with this construction deleted.
        """
        assert ('    utterance_load = UtteranceLoadMetrics(\n'
                '        capture_stats=mic.get_stats, '
                'stall_tracker=stall_tracker)' in self._source())

    def test_the_manager_is_given_the_reporter(self):
        """The two utterance seams live on UtteranceManager. Built
        without this argument the reporter is sampled and never read.
        """
        assert self._source().count('load_metrics=utterance_load') == 1

    def test_the_loop_takes_its_queue_depth_through_the_one_helper(self):
        """stall_tracker.record must have exactly one call site, the
        helper's. A second one would be a second read of the queue.
        """
        source = self._source()
        assert source.count('stall_tracker.record(') == 1
        assert source.count('sample_consumer_iteration(\n') == 1


class TestTheStallSaysWhichCallItWaitedOn:
    """wh-stt-load-metrics.4 G3.

    A [stall] line says the loop lost nine seconds. It cannot say
    whether the loop was blocked inside the send to Google or was not
    running at all, and those two need opposite fixes. The segment
    breakdown that follows it answers that, and it is only true if it
    describes the iteration the gap was measured across.
    """

    def _tracker(self, message):
        tracker = MagicMock()
        tracker.record = Mock(return_value=message)
        return tracker

    def test_the_breakdown_follows_the_stall_message(self):
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        segments = MagicMock()
        segments.report = Mock(return_value='[stall-where] iter=9884.0 ...')

        lines = google_main.sample_consumer_iteration(
            mic, self._tracker('[stall] ...'), None, segments)

        assert lines == ['[stall] ...', '[stall-where] iter=9884.0 ...']

    def test_the_breakdown_describes_the_iteration_that_stalled(self):
        """Read before the reopen, never after. This helper runs at the
        top of an iteration, so the accumulators still hold the previous
        one -- the one the tracker just measured the gap across. Starting
        first would report an iteration that has not run yet, and the
        line would read as a loop that stalled on nothing.
        """
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        order = []
        segments = MagicMock()
        segments.report = Mock(
            side_effect=lambda: order.append('report') or '[stall-where] ...')
        segments.start = Mock(side_effect=lambda: order.append('start'))

        google_main.sample_consumer_iteration(
            mic, self._tracker('[stall] ...'), None, segments)

        assert order == ['report', 'start']

    def test_every_iteration_is_opened_even_when_none_stalled(self):
        """Without this the accumulators would run on from the last
        stall, and the next breakdown would report the sum of every
        iteration since.
        """
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        segments = MagicMock()

        google_main.sample_consumer_iteration(
            mic, self._tracker(None), None, segments)

        segments.start.assert_called_once_with()

    def test_a_quiet_iteration_is_not_asked_for_a_breakdown(self):
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)
        segments = MagicMock()

        google_main.sample_consumer_iteration(
            mic, self._tracker(None), None, segments)

        segments.report.assert_not_called()

    def test_the_stall_message_survives_without_a_segment_timer(self):
        """The stall tracker predates the breakdown and must not lose
        its line to it.
        """
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=3)

        assert google_main.sample_consumer_iteration(
            mic, self._tracker('[stall] ...'), None) == ['[stall] ...']


class TestTheLoopTimesTheCallsItCanWaitOn:
    """Again read from the source: these five calls live inside main().

    Each is a call the loop can sit in for seconds. If one is left
    untimed its cost lands in "unaccounted", which reads as CPU
    starvation -- the one answer that would send the fix somewhere else
    entirely.
    """

    def _source(self):
        return Path(google_main.__file__).read_text(encoding='utf-8')

    def test_the_loop_builds_one_segment_timer(self):
        assert self._source().count('segments = IterationSegments()') == 1

    def test_the_response_drain_is_timed(self):
        source = self._source()
        assert 'responses_started = time.perf_counter()' in source
        assert "segments.add('responses'," in source

    def test_the_microphone_read_is_timed(self):
        assert ("with segments.timing('mic_read'):\n"
                "                audio_frame = mic.read(timeout=0.05)"
                in self._source())

    def test_the_send_to_google_is_timed(self):
        assert ("with segments.timing('send'):\n"
                "                        for chunk in valid_chunks:"
                in self._source())

    def test_the_vad_and_agc_measurements_are_taken_every_iteration(self):
        """They were gated behind cfg.debug.log_overflow_diagnostics. A
        stall that only appears under load is not one anybody reproduces
        with a debug flag turned on afterwards.
        """
        source = self._source()
        assert "segments.add('vad', (t1 - t0) * 1000)" in source
        assert "segments.add('agc', (t2 - t1) * 1000)" in source
        assert ("            t0 = time.perf_counter()\n"
                "            raw_is_speech = vad.is_speech(audio_frame)\n"
                "            t1 = time.perf_counter()" in source)

    def test_the_timer_is_given_to_the_helper(self):
        assert ('sample_consumer_iteration(\n'
                '                    mic, stall_tracker, utterance_load, '
                'segments,\n' in self._source())


class TestTheLineIsWrittenOnTheLoopThread:
    """wh-stt-load-metrics.4.1.1.

    Four of the five finalization triggers run on the consumer loop's
    own thread. The fifth does not: process_google_response arms a
    threading.Timer whose callback calls _finalize_utterance
    ("EOS_FALLBACK") on the timer thread (main.py:874-886), and that
    path is live whenever single_utterance is true.

    Both objects the line reads have no internal locking, and
    LoopStallTracker's docstring states the contract in words: "no
    internal locking; record() and reset() must never run concurrently
    ... Do not add a record()/reset() call from a new context without
    providing the same guarantee." The loop thread writes
    utterance_stalls and _q_sum on every iteration. A finish() on the
    timer thread reads and zeroes both, so a stall counted at the wrong
    instant is lost from the utterance's line and then discarded by the
    next start() -- it appears on no line at all, and the stall that
    coincides with a finalization is exactly the one worth having.

    So the timer thread queues the request and the loop thread writes
    the line. Nothing else about the finalization moves: the final, the
    state transition and the usage CSV still happen where they did.
    """

    def _deferred(self, load):
        """Finalize from a thread that is not the loop's."""
        mgr = _manager(load)
        mgr.start_new_utterance()
        worker = threading.Thread(
            target=mgr._finalize_utterance, args=('EOS_FALLBACK',))
        worker.start()
        worker.join()
        return mgr

    def test_a_finalize_from_another_thread_writes_no_line_there(self):
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1')

        self._deferred(load)

        load.finish.assert_not_called()

    def test_the_loop_writes_the_deferred_line_when_it_drains(self, logged):
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=EOS_FALLBACK')
        mgr = self._deferred(load)

        mgr.emit_pending_load_lines()

        load.finish.assert_called_once_with(1, 'EOS_FALLBACK')
        assert '[load-diag] utt=1 kind=EOS_FALLBACK' in logged

    def test_the_deferred_line_is_written_once(self):
        """A second drain must not repeat an utterance already reported."""
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=EOS_FALLBACK')
        mgr = self._deferred(load)

        mgr.emit_pending_load_lines()
        mgr.emit_pending_load_lines()

        assert load.finish.call_count == 1

    def test_a_new_utterance_drains_before_it_opens_the_window(self):
        """start() zeroes the window finish() has still to read. The
        next utterance can begin in the same iteration the timer
        finalized in, so the drain has to happen here too, not only at
        the top of the loop.
        """
        load = MagicMock()
        order: list[str] = []
        load.finish = Mock(
            side_effect=lambda *a: (order.append('finish')
                                    or '[load-diag] utt=1'))
        load.start = Mock(side_effect=lambda *a: order.append('start'))
        mgr = self._deferred(load)
        order.clear()

        mgr.start_new_utterance()

        assert order == ['finish', 'start']

    def test_a_finalize_on_the_loop_thread_still_writes_it_there(self, logged):
        """The four in-loop triggers keep the behaviour they had: the
        line is written inside the finalize, not one iteration later.
        """
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=GOOGLE_FINAL')
        mgr = _manager(load)
        mgr.start_new_utterance()

        mgr._finalize_utterance('GOOGLE_FINAL')

        load.finish.assert_called_once_with(1, 'GOOGLE_FINAL')
        assert '[load-diag] utt=1 kind=GOOGLE_FINAL' in logged
        load.finish.reset_mock()
        mgr.emit_pending_load_lines()
        load.finish.assert_not_called()

    def test_a_deferred_writer_that_raises_does_not_stop_the_loop(self, logged):
        """Every other reader failure in this feature costs its own
        numbers and nothing else; a drain that raises would instead
        break the iteration that drained it.
        """
        load = MagicMock()
        load.finish = Mock(side_effect=RuntimeError('gone'))
        mgr = self._deferred(load)

        mgr.emit_pending_load_lines()

        assert any('[load-diag] no line for UTT-1' in line for line in logged)

    def test_a_server_without_the_line_drains_nothing(self):
        """load_metrics is None on every existing construction."""
        mgr = _manager(None)
        mgr.start_new_utterance()

        mgr.emit_pending_load_lines()

    def test_the_loop_drains_every_iteration(self):
        """A line queued by the timer thread and never drained is a
        measurement collected and thrown away.
        """
        source = Path(google_main.__file__).read_text(encoding='utf-8')
        assert source.count('utterance_mgr.emit_pending_load_lines()') == 1


class TestTheLoopBuildsTheLoadReporter:
    """wh-stt-load-metrics.4 G1a.

    The Google loop had a bare LoopStallTracker and a [overflow-diag]
    line summarising its window. The Parakeet loop has the shared
    CaptureLoadReporter, whose periodic "[load-diag] window=" line is
    what the load test waits for before it starts a run -- so a Google
    run could not be measured by that tool at all.

    This gives the Google loop the same reporter, gated by the same
    [debug] log_load_diagnostics flag, and retires the [overflow-diag]
    stall summary it replaces. Two things the reporter needs that the
    Parakeet loop supplies by hand come from what this loop already
    measures: the cumulative work total from IterationSegments, and the
    queue reading, which the reporter now publishes so the stall line
    and the per-utterance line still carry one number for one
    iteration.
    """

    def _reporter(self, lines=(), depth=7):
        reporter = MagicMock()
        reporter.record_iteration = Mock(return_value=list(lines))
        reporter.last_queue_depth = depth
        return reporter

    def test_the_reporter_records_the_iteration(self):
        mic = MagicMock()
        reporter = self._reporter()

        google_main.sample_consumer_iteration(
            mic, MagicMock(), MagicMock(), reporter=reporter)

        reporter.record_iteration.assert_called_once_with()

    def test_the_queue_is_not_read_a_second_time(self):
        """The reporter already read it. A second read would give the
        stall line and the utterance line two different depths for one
        iteration."""
        mic = MagicMock()
        mic.get_queue_size = Mock(return_value=99)

        google_main.sample_consumer_iteration(
            mic, MagicMock(), MagicMock(), reporter=self._reporter())

        mic.get_queue_size.assert_not_called()

    def test_the_utterance_line_gets_the_reading_the_reporter_took(self):
        load = MagicMock()

        google_main.sample_consumer_iteration(
            mic=MagicMock(), stall_tracker=MagicMock(), utterance_load=load,
            reporter=self._reporter(depth=44))

        load.sample.assert_called_once_with(44)

    def test_the_tracker_is_not_recorded_twice(self):
        """record_iteration records the stall itself. A second record()
        here would count every iteration twice."""
        tracker = MagicMock()

        google_main.sample_consumer_iteration(
            MagicMock(), tracker, MagicMock(), reporter=self._reporter())

        tracker.record.assert_not_called()

    def test_every_line_the_reporter_returns_is_logged(self):
        window = '[load-diag] window=10.0s q_now=3 q_max=9'
        reporter = self._reporter(lines=[window])

        lines = google_main.sample_consumer_iteration(
            MagicMock(), MagicMock(), MagicMock(), reporter=reporter)

        assert window in lines

    def test_the_breakdown_follows_the_stall_and_nothing_else(self):
        """The reporter can return an outage line and a window summary
        beside the stall. The breakdown describes the iteration the
        stall was measured across, so it belongs after that line alone.
        """
        stall = '[stall] consumer loop made no progress for 9.9s'
        window = '[load-diag] window=10.0s q_now=3 q_max=9'
        segments = MagicMock()
        segments.report = Mock(return_value='[stall-where] iter=9900.0')

        lines = google_main.sample_consumer_iteration(
            MagicMock(), MagicMock(), MagicMock(), segments,
            reporter=self._reporter(lines=[stall, window]))

        assert lines == [stall, '[stall-where] iter=9900.0', window]

    def test_a_reporter_iteration_still_opens_the_next_segment(self):
        segments = MagicMock()

        google_main.sample_consumer_iteration(
            MagicMock(), MagicMock(), MagicMock(), segments,
            reporter=self._reporter())

        segments.start.assert_called_once_with()


class TestTheLoopWiresTheReporter:
    """The construction inside main(), read from the source for the
    reason TestTheLoopBuildsTheLine gives: main() is a 700-line function
    that opens a microphone and a Google stream, and the failure this
    guards is presence."""

    def _source(self):
        return Path(google_main.__file__).read_text(encoding='utf-8')

    def test_the_reporter_is_gated_by_the_flag(self):
        """Off by default: the line costs one log every ten seconds for
        the life of the process."""
        assert 'if cfg.debug.log_load_diagnostics else None' in self._source()

    def test_the_load_reporter_reads_the_provider_capture_counters(self):
        """A distinct name from the UtteranceLoadMetrics test above,
        which pins the same argument on the other construction. Two
        tests of one name cannot be told apart by the mutation gate,
        which matches a failure by its bare test name.
        """
        source = self._source()
        assert source.count('CaptureLoadReporter(') == 1
        assert ('        CaptureLoadReporter(\n'
                '            capture_stats=mic.get_stats,' in source)

    def test_the_reporter_knows_when_capture_is_ready(self):
        """Without it a window with no frames reads as starvation even
        while the microphone is still opening."""
        assert ('capture_ready=lambda: mic.wait_ready(timeout=0.0)'
                in self._source())

    def test_the_reporter_is_given_the_loops_own_work_total(self):
        """Without busy_seconds the [stall] figure is the plain
        wall-clock gap, which counts the loop's own work as starvation.
        """
        assert ('busy_seconds=lambda: segments.work_seconds'
                in self._source())

    def test_one_detector_serves_both_windows(self):
        """Two trackers would apply one threshold twice and report the
        same stall in two places with different numbers."""
        source = self._source()
        assert source.count('LoopStallTracker()') == 1
        assert 'load_reporter.stall_tracker if load_reporter' in source

    def test_the_loop_hands_the_reporter_to_the_one_helper(self):
        assert self._source().count('reporter=load_reporter') == 1

    def test_the_overflow_block_no_longer_summarises_stalls(self):
        """The reporter's window replaces it. Two summaries of one
        tracker would each reset the other's counters, so whichever ran
        second would report a window that had already been emptied."""
        source = self._source()
        assert 'loop stalls' not in source
        assert 'snapshot_and_reset_window' not in source

    def test_the_overflow_block_still_reports_queues_and_timing(self):
        """Those two answer questions the reporter does not."""
        source = self._source()
        assert '[overflow-diag] queues:' in source
        assert '[overflow-diag] timing:' in source


class _FakeClock:
    """A monotonic clock the test advances by hand.

    The stall this bead is about is a second and more of a consumer loop
    not running. Sleeping for it would make the suite that long and
    still not pin the gap to a number.
    """

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestTheLoadLineIsQueuedBeforeTheUtteranceCanReopen:
    """wh-stt-load-metrics.4.1.3.

    The EOS fallback timer finalizes on its own thread. Between the
    state transition to FINALIZED and the end of the method it calls
    usage_metrics.log_utterance, which writes a CSV row synchronously
    under a lock (usage_metrics.py). While that write waits -- or while
    the timer thread is descheduled under the load this line exists to
    measure -- the consumer loop can see stream_should_close, close the
    stream and open the next utterance. That reopen increments
    current_utterance_id, drains the queue, and zeroes the load window.

    With the append after the CSV write, the resumed timer queues the
    old EOS under the NEW id, against a window the reopen has already
    zeroed: the utterance that ended gets no line at all, and the new
    one gets a line built from its own first counters. Queuing before
    the state transition is what makes the request exist before
    anything can act on it, and the id is copied into a local so a
    reopen cannot relabel it.

    The append cannot move any earlier than that. forwarder.send_final
    sits above the transition and can raise out of the WebSocket, and a
    raise there leaves the utterance active, so a line queued before it
    would report an utterance that had not ended.
    """

    def _off_thread_manager(self, load, usage=None, forwarder=None):
        """A manager whose loop thread is not this thread.

        The module-level _manager() helper passes no usage_metrics, and
        the CSV writer is the collaborator this reentrancy runs
        through. The foreign thread id is the EOS fallback case: the
        same-thread drain at the end of _finalize_utterance does not
        fire, so what is asserted is the queued request itself.
        """
        cfg = MagicMock()
        cfg.silence_finalize_ms = 800
        cfg.debug.log_lifecycle = False
        cfg.max_no_text_seconds = 30
        mgr = google_main.UtteranceManager(
            cfg, MagicMock(), forwarder=forwarder,
            usage_metrics=usage, load_metrics=load)
        mgr._load_metrics_thread = threading.get_ident() + 1
        return mgr

    def _reopen_inside_the_usage_write(self, load):
        """Finalize UTT-1 while the consumer opens UTT-2 mid-write."""
        usage = MagicMock()
        mgr = self._off_thread_manager(load, usage=usage)
        mgr.start_new_utterance()
        usage.log_utterance = Mock(
            side_effect=lambda **kwargs: mgr.start_new_utterance())

        mgr._finalize_utterance('EOS_FALLBACK')

        # Anything still queued is the loop's to write on its next
        # iteration. Writing it here keeps the assertions about which
        # utterance the line names and which window it read, rather
        # than about when the drain happened.
        mgr.emit_pending_load_lines()
        return mgr

    def test_a_reopen_during_the_usage_write_keeps_the_ended_utterances_id(
            self):
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=EOS_FALLBACK')

        self._reopen_inside_the_usage_write(load)

        load.finish.assert_called_once_with(1, 'EOS_FALLBACK')

    def test_a_reopen_during_the_usage_write_reads_the_ended_window(self):
        """The id alone is not enough. finish() reads whatever counters
        are live when it runs, so it has to run before the reopen's
        start() zeroes them: the queued request must be there for the
        reopen's own drain to find.
        """
        load = MagicMock()
        order: list[str] = []
        load.finish = Mock(
            side_effect=lambda *a: (order.append('finish')
                                    or '[load-diag] utt=1'))
        load.start = Mock(side_effect=lambda *a: order.append('start'))

        self._reopen_inside_the_usage_write(load)

        assert order == ['start', 'finish', 'start']

    def test_a_reopen_at_the_state_transition_keeps_the_ended_utterances_id(
            self):
        """The upper bound on the move, one statement tighter than the
        CSV write above.

        closed_utterances.add runs immediately after
        state = FINALIZED, so a reopen there is the earliest moment the
        consumer loop can act on the transition. The request has to
        already exist by then. This test fails for any position below
        the transition, including positions the usage_metrics test
        above cannot tell apart.
        """
        load = MagicMock()
        load.finish = Mock(return_value='[load-diag] utt=1 kind=EOS_FALLBACK')
        mgr = self._off_thread_manager(load)
        mgr.start_new_utterance()

        class ReopeningSet(set):
            def add(self, value):
                super().add(value)
                mgr.start_new_utterance()

        mgr.closed_utterances = ReopeningSet(mgr.closed_utterances)

        mgr._finalize_utterance('EOS_FALLBACK')
        mgr.emit_pending_load_lines()

        load.finish.assert_called_once_with(1, 'EOS_FALLBACK')

    def test_a_final_that_raises_queues_no_line(self):
        """The lower bound on the move. send_final is above the state
        transition and can raise out of the WebSocket; the utterance is
        then still ACTIVE, and a line queued before that call would
        report an utterance that had not ended and let the next start()
        zero its window.
        """
        load = MagicMock()
        forwarder = MagicMock()
        forwarder.send_final = Mock(side_effect=RuntimeError('socket gone'))
        mgr = self._off_thread_manager(load, forwarder=forwarder)
        mgr.start_new_utterance()

        with pytest.raises(RuntimeError):
            mgr._finalize_utterance('EOS_FALLBACK')

        assert list(mgr._pending_load_lines) == []
        assert mgr.state == google_main.UtteranceState.ACTIVE


class TestTheFinalIterationsStallReachesTheUtteranceLine:
    """wh-stt-load-metrics.4.1.4.

    sample_consumer_iteration measures the iteration that has just
    ended, and through that iteration the utterance being finalized was
    still the active one. So the sample has to be taken before the
    queued EOS line is written, not after it.

    With the drain first, a stall spanning the iteration an EOS
    finalization landed in is read out of the utterance window before
    the detector has recorded it. The line reports stalls=0; the sample
    then puts the stall into a window whose line is already written;
    the next start() discards it. The stall that coincides with the end
    of an utterance -- the one this correlation exists for -- reaches no
    per-utterance line at all.
    """

    def _loop_top_order(self):
        """Which of main()'s two loop-top statements runs first.

        The loop is inside main(), a 700-line function that opens a
        microphone and a Google stream, so the behavioural test below
        cannot run it. It drives the same objects in the order the
        source declares, which is what makes it answer for the
        production order rather than for an order chosen here.
        """
        source = Path(google_main.__file__).read_text(encoding='utf-8')
        drain = source.index('utterance_mgr.emit_pending_load_lines()')
        sample = source.index('for stall_line in sample_consumer_iteration(')
        return ['sample', 'drain'] if sample < drain else ['drain', 'sample']

    def test_the_loop_samples_the_iteration_before_it_drains(self):
        assert self._loop_top_order() == ['sample', 'drain']

    def _loop(self):
        """The loop's collaborators, real, on a hand-advanced clock.

        The real CaptureLoadReporter, the one LoopStallTracker it owns,
        and a real UtteranceLoadMetrics, wired as main() wires them:
        one detector, and the reporter's own queue reading shared with
        the per-utterance line. Mocks here would answer whatever the
        assertion asked of them and pass on either order.
        """
        clock = _FakeClock()
        stats = {'qsize': 0, 'overflow_count': 0, 'status_flags': 0,
                 'drops': 0, 'frames_captured': 0}
        reporter = CaptureLoadReporter(
            capture_stats=lambda: dict(stats), clock=clock,
            busy_seconds=lambda: 0.0, capture_ready=lambda: True)
        utterance_load = UtteranceLoadMetrics(
            capture_stats=lambda: dict(stats),
            stall_tracker=reporter.stall_tracker)
        mgr = _manager(utterance_load)

        def iterate():
            return google_main.sample_consumer_iteration(
                MagicMock(), reporter.stall_tracker, utterance_load, None,
                reporter=reporter)

        return clock, stats, mgr, iterate

    def _finalize_off_thread(self, mgr, reason='EOS_FALLBACK'):
        """The EOS fallback timer's thread, which only queues."""
        worker = threading.Thread(
            target=mgr._finalize_utterance, args=(reason,))
        worker.start()
        worker.join()

    def _run_the_loop_top(self, mgr, iterate):
        """The two loop-top statements, in the order main() has them."""
        lines = []
        for step in self._loop_top_order():
            if step == 'sample':
                lines.extend(iterate())
            else:
                mgr.emit_pending_load_lines()
        return lines

    def _load_line(self, logged, utt):
        matches = [line for line in logged
                   if line.startswith(f'[load-diag] utt={utt} ')]
        assert len(matches) == 1, f'lines for utt={utt}: {matches}'
        return matches[0]

    def test_a_stall_on_the_finalizing_iteration_reaches_that_line(
            self, logged):
        clock, stats, mgr, iterate = self._loop()
        mgr.start_new_utterance()
        iterate()

        # The consumer does not run for 1.1s. The EOS fallback timer
        # fires inside that gap and queues UTT-1's line.
        clock.advance(1.1)
        stats['qsize'] = 42
        self._finalize_off_thread(mgr)

        lines = self._run_the_loop_top(mgr, iterate)

        assert any(line.startswith(google_main.STALL_PREFIX)
                   for line in lines), lines
        assert 'stalls=1 stall_max_ms=1100.0' in self._load_line(logged, 1)

    def test_the_next_utterance_does_not_inherit_the_finalizing_stall(
            self, logged):
        """The same stall on two lines would be as wrong as on none:
        UTT-2 would read as having stalled before a word was said.
        """
        clock, stats, mgr, iterate = self._loop()
        mgr.start_new_utterance()
        iterate()
        clock.advance(1.1)
        self._finalize_off_thread(mgr)
        self._run_the_loop_top(mgr, iterate)

        mgr.start_new_utterance()
        clock.advance(0.05)
        iterate()
        mgr._finalize_utterance('GOOGLE_FINAL')

        assert 'stalls=0 stall_max_ms=50.0' in self._load_line(logged, 2)
