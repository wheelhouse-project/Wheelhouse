"""Which call the consumer loop was waiting on when it stalled.

wh-stt-load-metrics.4, criterion G3: the diagnosis has to name the call,
with evidence from timing around it rather than from reading the code.

LoopStallTracker already says a stall happened and how long it lasted. It
cannot say where the time went, and the two candidate answers need
opposite fixes: a loop blocked inside one call (the send to Google
back-pressuring, a microphone read waiting on a device) is a queue or a
transport problem, while a loop whose every call was fast and which still
lost nine seconds was not running at all -- it was descheduled by whatever
else had the CPU. IterationSegments tells those apart by measuring the
parts and comparing their sum against the whole.

The unaccounted figure is the load-bearing one. It is what is left of the
iteration after every timed segment, so it is the descheduling answer, and
it competes with the segments to be named the worst.
"""
from __future__ import annotations

import re

import pytest

from shared_audio.diagnostics import IterationSegments


class _Clock:
    """A perf_counter stand-in, in seconds, that only moves when told."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now += ms / 1000.0


@pytest.fixture
def clock():
    return _Clock()


def _fields(line):
    return dict(re.findall(r'(\w+)=([^\s]+)', line))


class TestTheLineNamesTheSlowestPart:
    def test_a_blocking_send_is_named(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()
        with seg.timing('send'):
            clock.advance_ms(9870)
        clock.advance_ms(4)

        assert _fields(seg.report())['worst'] == 'send'

    def test_a_blocking_microphone_read_is_named(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()
        with seg.timing('mic_read'):
            clock.advance_ms(2000)
        with seg.timing('send'):
            clock.advance_ms(3)

        assert _fields(seg.report())['worst'] == 'mic_read'

    def test_a_loop_that_was_not_running_names_no_call(self, clock):
        """Every timed call was fast and nine seconds went missing. That
        is CPU starvation, and naming a call for it would send the fix to
        the wrong place.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        with seg.timing('mic_read'):
            clock.advance_ms(1)
        with seg.timing('vad'):
            clock.advance_ms(2)
        clock.advance_ms(9000)

        fields = _fields(seg.report())
        assert fields['worst'] == 'unaccounted'
        assert float(fields['unaccounted']) == pytest.approx(9000.0, abs=1.0)

    def test_the_whole_iteration_is_reported_beside_the_parts(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()
        with seg.timing('agc'):
            clock.advance_ms(500)
        clock.advance_ms(100)

        fields = _fields(seg.report())
        assert float(fields['iter']) == pytest.approx(600.0, abs=1.0)

    def test_every_segment_appears_even_when_it_did_nothing(self, clock):
        """A missing field would read as a segment that was not measured.
        A zero reads as a call that returned at once, which is the truth.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        clock.advance_ms(50)

        fields = _fields(seg.report())
        for name in IterationSegments.NAMES:
            assert fields[name] == '0.0', name

    def test_the_line_carries_its_own_tag(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()

        assert seg.report().startswith('[stall-where] ')


class TestOneIterationAtATime:
    def test_a_segment_entered_twice_adds_up(self, clock):
        """The send loop runs once per chunk, and a lead-in burst sends
        several in one iteration. Reporting only the last would hide the
        cost of the burst.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        for _ in range(3):
            with seg.timing('send'):
                clock.advance_ms(10)

        assert float(_fields(seg.report())['send']) == pytest.approx(30.0)

    def test_starting_again_forgets_the_previous_iteration(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()
        with seg.timing('send'):
            clock.advance_ms(9000)
        seg.start()
        with seg.timing('vad'):
            clock.advance_ms(2)

        fields = _fields(seg.report())
        assert fields['send'] == '0.0'
        assert fields['worst'] == 'vad'

    def test_a_report_before_any_start_answers_rather_than_raising(self):
        """The first stall can be reported before the first full
        iteration. A traceback out of a diagnostic would take the loop.
        """
        assert IterationSegments().report().startswith('[stall-where] ')

    def test_an_unknown_segment_name_is_ignored(self, clock):
        """A caller typo must not add a field the parser has never seen,
        must not raise inside the loop, and must not reach the figures.

        The last of those is the one that hides: report() prints only
        the fixed NAMES, so a stored typo never appears as a field --
        but it would still count toward the total, shrink unaccounted,
        and be named the worst. A five-second segment nobody can see.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        clock.advance_ms(50)
        seg.add('snd', 5000.0)

        line = seg.report()
        fields = _fields(line)
        assert 'snd=' not in line
        assert fields['worst'] == 'unaccounted'
        assert float(fields['unaccounted']) == pytest.approx(50.0, abs=1.0)


class TestTheTimerNeverStopsTheLoopItWatches:
    def test_a_raising_body_still_records_its_time(self, clock):
        """The send to Google raises when the stream dies, and that is
        exactly the iteration whose timing matters most.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        with pytest.raises(RuntimeError):
            with seg.timing('send'):
                clock.advance_ms(4000)
                raise RuntimeError('the stream died')

        assert float(_fields(seg.report())['send']) == pytest.approx(4000.0)

    def test_a_raising_body_propagates(self, clock):
        """Timing must not swallow the loop's own error handling."""
        seg = IterationSegments(clock=clock)
        seg.start()
        with pytest.raises(RuntimeError):
            with seg.timing('send'):
                raise RuntimeError('the stream died')

    def test_a_negative_or_absent_measurement_is_dropped(self, clock):
        """time.perf_counter is monotonic, so a negative figure means the
        caller passed something wrong; a NaN would print and poison the
        comparison that names the worst.
        """
        seg = IterationSegments(clock=clock)
        seg.start()
        seg.add('send', -5.0)
        seg.add('vad', float('nan'))

        fields = _fields(seg.report())
        assert fields['send'] == '0.0'
        assert fields['vad'] == '0.0'


class TestTheWorkTotalTheStallFigureSubtracts:
    """wh-stt-load-metrics.4 G1a.

    LoopStallTracker.record computes gap = (now - last) - busy_s, so the
    busy figure decides what counts as a stall. Two candidate figures
    exist and only one of them can be right.

    The wall time of an iteration minus the capture wait is the WRONG
    one: it includes this class's unaccounted figure, and unaccounted is
    the descheduling itself. Passing it would subtract the starvation
    from the gap and drive every stall to zero -- the [stall] line would
    go quiet exactly when the machine was worst.

    So the work total is the sum of the MEASURED spans, which is the
    same shape the Parakeet loop accumulates by hand
    (sherpa_offline_parakeet_stt_server/main.py:498-511). The capture
    read is left out for the reason that loop leaves it out: a starved
    loop sits in that wait, so those seconds are the stall signal and
    counting them as work erases it.

    It is cumulative for the life of the loop. CaptureLoadReporter
    differences it across iterations (diagnostics.py:651-680), and a
    figure that reset each iteration would charge the whole of it to
    every gap.
    """

    def test_a_new_timer_has_done_no_work(self):
        assert IterationSegments().work_seconds == 0.0

    def test_every_segment_but_the_capture_wait_is_work(self):
        seg = IterationSegments()
        seg.start()

        seg.add('responses', 100.0)
        seg.add('vad', 20.0)
        seg.add('agc', 5.0)
        seg.add('send', 75.0)

        assert seg.work_seconds == pytest.approx(0.2)

    def test_the_capture_wait_is_not_work(self):
        """The whole reason this figure exists: a loop starved of CPU
        waits in mic_read, and charging that wait to the loop's own work
        would subtract the stall from itself."""
        seg = IterationSegments()
        seg.start()

        seg.add('mic_read', 9000.0)

        assert seg.work_seconds == 0.0

    def test_a_timed_block_feeds_the_work_total(self, clock):
        seg = IterationSegments(clock=clock)
        seg.start()

        with seg.timing('send'):
            clock.advance_ms(400)

        assert seg.work_seconds == pytest.approx(0.4)

    def test_a_timed_block_that_raises_still_counts_its_work(self, clock):
        """The send raises when the stream dies, and that iteration's
        seconds were still spent."""
        seg = IterationSegments(clock=clock)
        seg.start()

        with pytest.raises(RuntimeError):
            with seg.timing('send'):
                clock.advance_ms(300)
                raise RuntimeError('stream died')

        assert seg.work_seconds == pytest.approx(0.3)

    def test_a_timed_capture_read_is_still_not_work(self, clock):
        """timing() records through add(), so the exclusion has to hold
        for the context manager the loop actually uses on mic_read."""
        seg = IterationSegments(clock=clock)
        seg.start()

        with seg.timing('mic_read'):
            clock.advance_ms(2000)

        assert seg.work_seconds == 0.0

    def test_opening_an_iteration_does_not_clear_the_work_total(self):
        """The reader is cumulative for the loop's life; the reporter
        differences it. A total cleared by start() would charge one
        iteration's work to every gap."""
        seg = IterationSegments()
        seg.start()
        seg.add('vad', 50.0)

        seg.start()
        seg.add('vad', 30.0)

        assert seg.work_seconds == pytest.approx(0.08)

    def test_a_measurement_the_timer_rejects_is_not_work(self):
        """add() drops an unknown name, a negative and a NaN. A total
        that took them anyway would subtract time the loop never spent
        and hide a real stall."""
        seg = IterationSegments()
        seg.start()

        seg.add('not_a_segment', 500.0)
        seg.add('vad', -10.0)
        seg.add('agc', float('nan'))
        seg.add('send', float('inf'))

        assert seg.work_seconds == 0.0

    def test_the_work_total_survives_a_report(self):
        """report() reads the iteration; it must not spend the total."""
        seg = IterationSegments()
        seg.start()
        seg.add('vad', 40.0)

        seg.report()

        assert seg.work_seconds == pytest.approx(0.04)
