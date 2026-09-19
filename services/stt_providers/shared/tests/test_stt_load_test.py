"""The one-command STT CPU-load test: parsing, judgement and report.

wh-load-test-script. docs/testing/stt-cpu-load-test-procedure.md describes a
run an operator performs by hand. These tests cover the parts of the script
that replace the operator's reading of the log: turning [load-diag] and
[vad_utt_stats] lines back into numbers, judging each sentence against the
fixed script, and computing the verdict the procedure's template asks for.

Nothing here touches audio hardware. The impure half of the tool -- the
spinners, the saturation reading, the playback, the config flip -- is driven
by the operator and is deliberately not simulated.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

SHARED = Path(__file__).resolve().parents[1]
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from tools.stt_load_test import judge, logparse, record, report, run, script

# Captured before any test runs and before the fixture below replaces it:
# the real busy-loop command, held so the refusal can recognise it without
# this file spelling the busy loop out anywhere a test could copy it from.
REAL_SPINNER_ARGV = list(run.SPINNER_ARGV)
# What a spinner runs during a test: an idle process that ends by itself.
# Long enough for a test to see it alive and stop it, short enough that a
# mutant which loses hold of twenty of them leaves nothing behind.
HARMLESS_SPINNER_ARGV = [sys.executable, '-c', 'import time; time.sleep(30)']


@pytest.fixture(autouse=True)
def no_real_spinners(monkeypatch):
    """No test in this file starts the real busy loop; one that tries fails.

    ``run.main`` starts one ``SPINNER_ARGV`` process per logical core unless
    ``--baseline`` is passed, and ``start_spinners`` starts however many it
    is asked for. Two ``main`` tests in this file used to reach that call
    with nothing standing in for it, so every run of the file put forty
    real busy loops on the machine and trusted the code under test to stop
    them. Under the mutation gate that trust is exactly what is broken: a
    mutant that registers the spinners in a private copy leaves ``main``
    stopping an empty list, and the twenty it started stay at full load.
    Ikon hung five times during full sweeps at those mutations
    (wh-load-test-script.2).

    Two layers. The command is replaced for every test, so a spinner that
    is started runs the harmless command above. And ``Popen.__init__``
    refuses the real command however it arrives -- through
    ``run.SPINNER_ARGV`` after someone removes the replacement, or spelled
    out in a mutant -- before the child exists, so the refusing test leaves
    no process behind. The refusal is ``pytest.fail`` rather than an
    ordinary exception because the code under test catches ``OSError``
    around process creation on purpose, and a refusal it could swallow
    would prove nothing.
    """
    monkeypatch.setattr(run, 'SPINNER_ARGV', list(HARMLESS_SPINNER_ARGV))
    real_init = subprocess.Popen.__init__
    busy_loop = REAL_SPINNER_ARGV[-1]

    def refuse_the_real_spinner(popen, *args, **kwargs):
        argv = args[0] if args else kwargs.get('args')
        if isinstance(argv, (str, bytes, os.PathLike)):
            parts = [argv]
        else:
            parts = list(argv or ())
        if any(busy_loop in str(part) for part in parts):
            pytest.fail(f'a test started the real spinner command {parts!r}; '
                        'spinners in this file come from run.SPINNER_ARGV, '
                        'which the no_real_spinners fixture replaces')
        return real_init(popen, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, '__init__',
                        refuse_the_real_spinner)


# The exact bytes the provider writes, taken from the format strings in
# shared_stt/audio_processor.py and shared_audio/diagnostics.py. The app
# prefixes its own timestamp and level, so every fixture carries one.
PREFIX = ("2026-08-30 14:03:10,123 [INFO] MainProcess - "
          "websocket_manager.py:1008 - trace=none - ")

UTT_LINE = (PREFIX + "[load-diag] utt=3 kind=endpoint overflow=0 "
            "status_flags=2 drops=17 q_max=333 q_mean=41.2 q_n=95 "
            "engine_calls=12 engine_ms_total=8100.0 engine_ms_max=980.0 "
            "audio_ms=6000 engine_ratio=1.35")
VAD_LINE = (PREFIX + "[vad_utt_stats] utt=3 kind=endpoint speech=61 "
            "total=95 ratio=0.642 gate_ms=6100 "
            "text='<redacted: 44 chars, 9 words>'")
WINDOW_LINE = (PREFIX + "[load-diag] window=10s q_now=4 q_max=31 drops=0 "
               "overflow=0 status_flags=0 stalls=0 max_gap_ms=180 "
               "max_frame_gap_ms=240")
UNAVAILABLE_LINE = (PREFIX + "[load-diag] window=10s capture=unavailable "
                    "q_now=n/a q_max=n/a drops=n/a overflow=n/a "
                    "status_flags=n/a stalls=2 max_gap_ms=1400 "
                    "max_frame_gap_ms=8000")
OUTAGE_LINE = (PREFIX + "[load-diag] capture-outage start_s_ago=40 "
               "end_s_ago=10 duration_s=30 windows=3")


def _procedure_text():
    if not script.PROCEDURE_DOC.is_file():
        pytest.skip("development-only CPU-load procedure is absent from this checkout")
    return script.PROCEDURE_DOC.read_text(encoding='utf-8')


class TestTheFixedScript:
    """The six sentences, and the silence that separates them."""

    def test_the_six_sentences_are_the_ones_the_procedure_lists(self):
        """The document and the code must not drift apart. Two runs are
        comparable only when they said the same words, so the sentences are
        read out of the procedure and compared with the tuple the recording
        is built from.
        """
        doc = _procedure_text()
        _, sep, tail = doc.partition('## The fixed script')
        assert sep, "the procedure does not list the fixed script"
        section = tail.split('## The verdict template')[0]
        listed = [line.split('. ', 1)[1].strip()
                  for line in section.splitlines()
                  if line[:1].isdigit() and '. ' in line]
        assert listed == list(script.SENTENCES)

    def test_the_gap_outlasts_the_provider_endpoint_silence(self):
        """Each sentence has to become its own utterance, or the per-sentence
        join on utt=N has nothing to join. The provider ends an utterance
        after endpoint_silence_ms of trailing silence, so the gap in the
        recording is derived from that setting rather than guessed.
        """
        assert script.inter_sentence_gap_s(600) > 0.600
        assert script.inter_sentence_gap_s(800) > 0.800
        assert script.inter_sentence_gap_s(2000) > 2.000

    def test_the_gap_leaves_room_for_a_late_endpoint_decision(self):
        """Under this test's own load the endpoint decision is exactly what
        gets slow, so a gap that merely clears the threshold is not enough.
        """
        assert script.inter_sentence_gap_s(600) * 1000 >= 3 * 600
        assert script.inter_sentence_gap_s(800) * 1000 >= 3 * 800

    def test_the_expected_text_is_what_the_recognizer_would_write(self):
        """The words are spoken, but the log carries the text after the
        provider's own rules have run. sherpa_engine.py applies
        normalize_transcript then apply_itn, so the expected form of sentence
        four is a numeral, and comparing against the spoken words would
        report a correct transcript as wrong.
        """
        spoken = "Seventeen thousand four hundred and thirty two."
        assert script.expected_transcript(spoken) != spoken
        assert any(ch.isdigit() for ch in script.expected_transcript(spoken))


class TestReadingTheProviderLines:
    """Turning the log back into the numbers the operator used to read."""

    def test_the_per_utterance_line_becomes_numbers(self):
        parsed = logparse.parse_log(UTT_LINE)
        assert len(parsed.utterances) == 1
        line = parsed.utterances[0]
        assert line.utt == 3
        assert line.kind == 'endpoint'
        assert line.overflow == 0
        assert line.status_flags == 2
        assert line.drops == 17
        assert line.q_max == 333
        assert line.q_mean == pytest.approx(41.2)
        assert line.q_n == 95
        assert line.engine_calls == 12
        assert line.engine_ms_total == pytest.approx(8100.0)
        assert line.engine_ms_max == pytest.approx(980.0)
        assert line.audio_ms == pytest.approx(6000.0)
        assert line.engine_ratio == pytest.approx(1.35)

    def test_a_counter_the_provider_does_not_report_is_not_zero(self):
        """`n/a` means the provider counts nothing there, which is a
        different fact from counting zero. The procedure says so in as many
        words, and a parser that read it as 0 would let the report claim
        `overflow=0` on the WinRT path, which reports no overflow at all.
        """
        text = UTT_LINE.replace('overflow=0', 'overflow=n/a')
        line = logparse.parse_log(text).utterances[0]
        assert line.overflow is None
        assert line.drops == 17

    def test_the_vad_line_carries_the_text_and_the_utterance_number(self):
        """The per-sentence join lives here: [vad_utt_stats] is the only
        line with both the utterance number and the transcript, so it is
        what ties a sentence to its own [load-diag] numbers.
        """
        line = logparse.parse_log(VAD_LINE).transcripts[0]
        assert line.utt == 3
        assert line.kind == 'endpoint'
        assert line.text == '<redacted: 44 chars, 9 words>'

    def test_a_transcript_containing_spaces_survives_the_parse(self):
        """text= is last on the line and holds a repr, so it is read to the
        end of the line rather than to the next space. Splitting on spaces
        would truncate every real transcript to its first word.
        """
        text = VAD_LINE.replace("'<redacted: 44 chars, 9 words>'",
                                '"The quick brown fox jumps over it"')
        assert (logparse.parse_log(text).transcripts[0].text
                == 'The quick brown fox jumps over it')

    def test_the_window_line_becomes_numbers(self):
        line = logparse.parse_log(WINDOW_LINE).windows[0]
        assert line.capture_available is True
        assert line.window_s == pytest.approx(10.0)
        assert line.q_now == 4
        assert line.q_max == 31
        assert line.drops == 0
        assert line.overflow == 0
        assert line.status_flags == 0
        assert line.stalls == 0
        assert line.max_gap_ms == pytest.approx(180.0)
        assert line.max_frame_gap_ms == pytest.approx(240.0)

    def test_an_unavailable_window_is_marked_rather_than_zeroed(self):
        """The whole point of capture=unavailable is that the capture fields
        say nothing. A window read as six zeros is a clean quiet window,
        which is exactly the reading the marker exists to prevent.
        """
        line = logparse.parse_log(UNAVAILABLE_LINE).windows[0]
        assert line.capture_available is False
        assert line.drops is None
        assert line.overflow is None
        assert line.status_flags is None
        assert line.stalls == 2
        assert line.max_frame_gap_ms == pytest.approx(8000.0)

    def test_the_outage_event_is_read_as_one_event(self):
        line = logparse.parse_log(OUTAGE_LINE).outages[0]
        assert line.start_s_ago == pytest.approx(40.0)
        assert line.end_s_ago == pytest.approx(10.0)
        assert line.duration_s == pytest.approx(30.0)
        assert line.windows == 3

    def test_lines_from_the_rest_of_the_log_are_left_alone(self):
        """wheelhouse.log carries every subsystem. A parser that matched
        loosely would read another component's key=value line as a
        measurement.

        The two noise lines below carry payloads that are well formed, so
        nothing downstream can reject them: only the position of the tag
        tells them apart from a measurement.
        """
        noise = (PREFIX + "Command 'window=10s q_now=0 q_max=0 drops=99 "
                 "overflow=0 status_flags=0 stalls=0 max_gap_ms=0 "
                 "max_frame_gap_ms=0' matched\n"
                 + PREFIX + "[vad_utt_stats_something_else] utt=9 "
                 "kind=endpoint text='not a transcript'\n")
        parsed = logparse.parse_log(noise + WINDOW_LINE)
        assert len(parsed.windows) == 1
        assert parsed.windows[0].drops == 0
        assert parsed.transcripts == []


def _spoken(index: int) -> str:
    return script.SENTENCES[index - 1]


def _said(index: int, text: str) -> logparse.Transcript:
    """A [vad_utt_stats] record for sentence ``index`` reading ``text``."""
    return logparse.Transcript(utt=index, kind='endpoint', text=text)


def _heard(index: int) -> logparse.Transcript:
    """A record whose text is exactly what a clean run would produce."""
    return _said(index, script.expected_transcript(_spoken(index)))


def _redacted(text: str) -> str:
    """The placeholder the release default writes instead of ``text``."""
    return f"<redacted: {len(text)} chars, {len(text.split())} words>"


class TestJudgingEachSentence:
    """What can be concluded about one sentence, at the resolution the
    transcript-privacy default allows."""

    def test_the_placeholder_format_is_the_one_redact_writes(self):
        """The judge reads char and word counts out of the placeholder, so
        it has to be reading the format redact.py actually writes. This test
        builds the string with the shipped function rather than restating
        the format, so the two cannot drift apart.
        """
        import os

        from shared_stt.redact import ENV_VAR, redact_transcript

        previous = os.environ.pop(ENV_VAR, None)
        try:
            written = redact_transcript("one two three")
        finally:
            if previous is not None:
                os.environ[ENV_VAR] = previous
        counts = judge.redacted_counts(written)
        assert counts == (13, 3)

    def test_a_visible_transcript_that_matches_is_correct(self):
        judged = judge.judge_sentences([_heard(i) for i in range(1, 7)])
        assert [r.outcome for r in judged.results] == [judge.CORRECT] * 6
        assert judged.extras == ()

    def test_a_visible_transcript_that_differs_is_wrong(self):
        heard = [_heard(i) for i in range(1, 7)]
        heard[2] = _said(3, 'November December January February March Apple')
        judged = judge.judge_sentences(heard)
        assert judged.results[2].outcome == judge.WRONG
        assert 'Apple' in judged.results[2].detail
        assert [r.outcome for r in judged.results if r.index != 3] == \
            [judge.CORRECT] * 5

    def test_a_redacted_transcript_of_the_right_size_is_not_called_correct(self):
        """Equal counts cannot prove equal words. The outcome says what was
        actually checked, so the report never claims more than it measured.
        """
        heard = [_said(i, _redacted(script.expected_transcript(_spoken(i))))
                 for i in range(1, 7)]
        judged = judge.judge_sentences(heard)
        assert [r.outcome for r in judged.results] == [judge.LENGTH_MATCHES] * 6
        assert judge.CORRECT not in [r.outcome for r in judged.results]

    def test_a_redacted_transcript_of_the_wrong_size_is_wrong(self):
        heard = [_said(i, _redacted(script.expected_transcript(_spoken(i))))
                 for i in range(1, 7)]
        heard[0] = _said(1, _redacted('The quick brown fox'))
        judged = judge.judge_sentences(heard)
        assert judged.results[0].outcome == judge.WRONG
        assert '19 chars' in judged.results[0].detail

    def test_a_redacted_transcript_with_the_wrong_word_count_is_wrong(self):
        """The placeholder carries two numbers and both of them matter. A
        comparison that read only the character count would pass a
        transcript that ran the same letters into a different number of
        words, which is exactly what a lost frame does to a sentence.
        """
        heard = [_said(i, _redacted(script.expected_transcript(_spoken(i))))
                 for i in range(1, 7)]
        want = script.expected_transcript(_spoken(1))
        heard[0] = _said(1, f'<redacted: {len(want)} chars, '
                            f'{len(want.split()) + 1} words>')
        judged = judge.judge_sentences(heard)
        assert judged.results[0].outcome == judge.WRONG

    def test_a_sentence_with_no_utterance_is_missing(self):
        judged = judge.judge_sentences([_heard(1), _heard(2)])
        assert [r.outcome for r in judged.results[2:]] == [judge.MISSING] * 4

    def test_an_extra_utterance_does_not_shift_the_sentences_after_it(self):
        """More utterances than sentences means something else made a sound,
        or the endpointing split one. Pairing by position would then read
        every later sentence against the previous sentence's transcript and
        report five failures for one cough.
        """
        heard = [_heard(i) for i in range(1, 7)]
        heard.insert(2, _said(99, 'a cough'))
        judged = judge.judge_sentences(heard)
        assert [r.outcome for r in judged.results] == [judge.CORRECT] * 6
        assert judged.extras == ('a cough',)


def _utterance(index: int = 1, overflow: Optional[int] = 0,
               drops: Optional[int] = 0, status_flags: Optional[int] = 0,
               ratio: float = 0.7):
    return logparse.UtteranceMetrics(
        utt=index, kind='endpoint', overflow=overflow,
        status_flags=status_flags, drops=drops, q_max=12, q_mean=3.1, q_n=40,
        engine_calls=8, engine_ms_total=4200.0, engine_ms_max=520.0,
        audio_ms=6000.0, engine_ratio=ratio)


def _window(available: bool = True, drops: Optional[int] = 0,
            overflow: Optional[int] = 0, stalls: int = 0):
    return logparse.Window(
        window_s=10.0, capture_available=available, q_now=2, q_max=9,
        drops=drops, overflow=overflow, status_flags=0, stalls=stalls,
        max_gap_ms=120.0, max_frame_gap_ms=200.0)


def _all_correct():
    return judge.judge_sentences([_heard(i) for i in range(1, 7)])


def _one_missing():
    return judge.judge_sentences([_heard(i) for i in range(1, 6)])


class TestTheRunVerdict:
    """The five outcomes the procedure's template asks for, and the two
    honest answers it has no box for."""

    def test_a_run_that_measured_nothing_earns_no_verdict(self):
        """A run whose every capture field is blank cannot earn any of the
        three failure verdicts, because the numbers those verdicts rest on
        were never taken.

        Every window here is unavailable, which is the condition for
        refusing. A run that lost only some of them keeps its verdict and
        prints the gap instead -- TestAPartlyMeasuredRunIsStillJudged.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance()], windows=[_window(available=False)])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert verdict.verdict == judge.CAPTURE_UNAVAILABLE

    def test_overflow_above_zero_is_callback_loss(self):
        parsed = logparse.ParsedLog(utterances=[_utterance(overflow=48)],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.CALLBACK_LOSS
        assert any('overflow' in line for line in verdict.evidence)

    def test_drops_with_a_low_engine_ratio_are_consumer_side_loss(self):
        parsed = logparse.ParsedLog(
            utterances=[_utterance(drops=210, ratio=0.8)],
            windows=[_window(drops=210)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.CONSUMER_LOSS

    def test_drops_with_a_high_engine_ratio_are_inference_lag(self):
        """The procedure says to read drops with engine_ratio beside it: a
        loop that is slow because the recognizer is slow stops draining the
        queue, so the drops are inference lag's second symptom rather than a
        second failure. Reporting two failures here would send the reader
        after a consumer-loop fix that changes nothing.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(drops=210, ratio=2.4)],
            windows=[_window(drops=210)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.INFERENCE_LAG
        assert any('second symptom' in line for line in verdict.evidence)

    def test_overflow_beside_inference_lag_is_more_than_one(self):
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=48, ratio=2.4)],
            windows=[_window(overflow=48)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.MORE_THAN_ONE

    def test_three_clean_numbers_and_a_bad_transcript_are_neither(self):
        parsed = logparse.ParsedLog(utterances=[_utterance(ratio=0.75)],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.NEITHER

    def test_neither_is_not_awarded_without_the_baseline_to_compare_against(self):
        """`neither` takes all three numbers, and one of them is
        engine_ratio near its IDLE value. With no baseline run there is no
        idle value, so the honest answer is that the run does not decide.
        """
        parsed = logparse.ParsedLog(utterances=[_utterance(ratio=0.75)],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=None)
        assert verdict.verdict == judge.UNDETERMINED

    def test_a_counter_the_provider_never_kept_cannot_earn_neither(self):
        """`overflow=n/a` is not `overflow=0`. On the WinRT capture path it
        is the normal reading, and treating it as zero would let the run
        conclude that no frames were lost from a counter nobody kept.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=None, ratio=0.75)],
            windows=[_window(overflow=None)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.UNDETERMINED
        assert any('overflow' in line for line in verdict.evidence)

    def test_a_counter_that_stops_partway_cannot_earn_neither(self):
        """A count covering part of a run is not a clean reading of the whole
        of it. A capture path that fails midway does exactly this: real
        numbers for the windows before it and `n/a` after. Summing what is
        left and calling the total clean would describe seconds nobody
        measured, so a partial count is as disqualifying as no count at all.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=0, ratio=0.75)],
            windows=[_window(overflow=0), _window(overflow=None)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.UNDETERMINED
        assert any('part or all' in line for line in verdict.evidence)

    def test_clean_numbers_and_a_clean_transcript_are_not_a_failure(self):
        """`neither` means the measurement missed a failure that happened.
        A run where nothing went wrong is a different answer, and calling it
        `neither` would send the reader looking for a cause that is not
        there.
        """
        parsed = logparse.ParsedLog(utterances=[_utterance(ratio=0.75)],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert verdict.verdict == judge.NO_FAILURE


def _rendered(**overrides):
    settings = dict(
        machine='Ikon',
        logical_cores=20,
        provider='Parakeet v3 (CPU)',
        busy_percent=100.0,
        baseline_ratio=0.7,
        parsed=logparse.ParsedLog(utterances=[_utterance()],
                                  windows=[_window()]),
        sentences=_all_correct(),
        verdict=judge.RunVerdict(judge.NO_FAILURE, ('nothing moved',)),
        playback_underflows=0,
        rotated=False,
    )
    settings.update(overrides)
    return report.render(**settings)


class TestTheCompletedTemplate:
    """What the operator used to write out by hand at the end of a run."""

    def test_every_field_the_procedure_asks_for_is_filled(self):
        """The template lives in the procedure, and the run has to answer all
        of it -- the document says a blank field is the one that will be
        argued about later. The labels are read out of the document rather
        than restated here, so the report cannot fall behind the template.
        """
        doc = _procedure_text()
        _, sep, tail = doc.partition('## The verdict template')
        assert sep, "the procedure has no verdict template"
        block = tail.split('```')[1]
        labels = [line.split(':', 1)[0] + ':' for line in block.splitlines()
                  if ':' in line]
        assert len(labels) >= 9, labels
        rendered = _rendered()
        for label in labels:
            assert label in rendered, label

    def test_the_verdict_and_its_evidence_are_both_printed(self):
        rendered = _rendered(
            verdict=judge.RunVerdict(judge.CALLBACK_LOSS,
                                     ('overflow total 48',)))
        assert judge.CALLBACK_LOSS in rendered
        assert 'overflow total 48' in rendered

    def test_a_correct_sentence_whose_utterance_lagged_is_reported_as_late(self):
        """The procedure's template has a line for sentences that were
        correct but late, and engine_ratio above 1.0 is exactly what "late
        but correct" means in its own three-row table. The log timestamps
        cannot answer this: forwarded provider lines are stamped when the
        application received them, not when the provider wrote them
        (wh-forwarded-log-time-order), and under this test's load that gap is
        the thing being measured.
        """
        rendered = _rendered(
            parsed=logparse.ParsedLog(utterances=[_utterance(ratio=2.4)],
                                      windows=[_window()]))
        section = rendered.split('Sentences correct but late:')[1]
        assert '1 (sentence 1' in section.splitlines()[0]

    def test_the_late_join_is_on_the_utterance_number_not_the_position(self):
        """Utterance numbers do not start at one -- the provider has been
        running since the application started. Joining on position would
        charge one sentence's lag to another whenever the run is not the
        first thing the provider ever heard.
        """
        heard = [logparse.Transcript(
            utt=11 + i, kind='endpoint',
            text=script.expected_transcript(_spoken(i)))
            for i in range(1, 7)]
        slow = [_utterance(index=11 + i, ratio=2.4 if i == 2 else 0.7)
                for i in range(1, 7)]
        rendered = _rendered(
            parsed=logparse.ParsedLog(utterances=slow, windows=[_window()]),
            sentences=judge.judge_sentences(heard))
        late = rendered.split('Sentences correct but late:')[1].splitlines()[0]
        assert 'sentence 2' in late

    def test_the_report_says_when_the_words_were_never_in_the_log(self):
        """A reader who sees "length matches" without being told why would
        reasonably assume the words were compared. They were not.
        """
        heard = [_said(i, _redacted(script.expected_transcript(_spoken(i))))
                 for i in range(1, 7)]
        rendered = _rendered(sentences=judge.judge_sentences(heard))
        assert 'transcript-privacy' in rendered
        assert 'LOG_TRANSCRIPTS' in rendered

    def test_playback_underflows_are_printed_beside_the_capture_numbers(self):
        """The audio is played out of the speakers on a machine this test
        has deliberately saturated. A glitch in the PLAYBACK sounds exactly
        like a glitch in the capture, and only this number tells them apart.
        """
        assert 'playback underflows' in _rendered().lower()
        assert '7' in _rendered(playback_underflows=7).split(
            'Playback underflows:')[1].splitlines()[0]


class TestTheCachedRecording:
    """When the synthesized WAV may be reused, and when it may not.

    generate_corpus.py already learned this: audio that looks current but was
    built from different text is worse than no audio, because nothing
    downstream can tell.
    """

    def test_a_recording_built_from_the_same_inputs_is_reused(self):
        """The same inputs ask for the same file, so it is reused.

        This looks like the weakest of the three name tests and it is
        what makes the other two mean anything. A digest broken to
        return a fresh value on every call would satisfy both of the
        difference tests below while never reusing a recording at all.
        """
        wanted = record.manifest_for(600, record.DEFAULT_VOICE)
        assert (record.recording_name(dict(wanted))
                == record.recording_name(wanted))

    def test_a_changed_sentence_makes_the_cached_file_stale(self,
                                                            monkeypatch):
        """The manifest is built from the sentences themselves, not from a
        marker saying the sentences were the fixed ones. The two manifests
        below are built the same way and differ only in the words, so they
        can disagree only if the words reach the manifest.
        """
        stored = record.manifest_for(600, record.DEFAULT_VOICE)
        changed = list(script.SENTENCES)
        changed[3] = 'Something else entirely.'
        monkeypatch.setattr(record, 'SENTENCES', tuple(changed))
        wanted = record.manifest_for(600, record.DEFAULT_VOICE)
        assert (record.recording_name(stored)
                != record.recording_name(wanted))

    def test_a_changed_endpoint_setting_makes_the_cached_file_stale(self):
        """The silence between sentences comes from the provider's endpoint
        setting. A file built for a shorter gap would merge two sentences
        into one utterance and break the per-sentence join, while looking
        exactly like a current file on disk.
        """
        assert (record.recording_name(
            record.manifest_for(600, record.DEFAULT_VOICE))
            != record.recording_name(
                record.manifest_for(1200, record.DEFAULT_VOICE)))

    def test_a_missing_recording_is_built_and_a_present_one_is_not(
            self, tmp_path, monkeypatch):
        """Reuse is decided by whether the recording is there.

        Both answers are pinned here because one alone is not a
        guard. A function that always reported a build would satisfy
        the missing half while never reusing anything, and one that
        never reported a build would satisfy the present half while
        rebuilding every run. The three name tests above pin the
        NAME in both directions and say nothing about the flag, and
        nothing else in this file touches the flag at all.
        """
        def build(wav_path, spec):
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(b'RIFF')

        monkeypatch.setattr(record, '_build', build)

        path, built = record.ensure_recording(800.0, cache_dir=tmp_path)
        assert built, 'a recording that is not there has to be built'
        assert path.is_file()

        again, rebuilt = record.ensure_recording(800.0,
                                                cache_dir=tmp_path)
        assert not rebuilt, 'a recording that is there has to be reused'
        assert again == path

    def test_the_name_changes_when_any_part_of_the_manifest_changes(self):
        """The name is what decides reuse, so it has to carry every input.

        The walk takes the keys from the manifest itself rather than a
        list written here, because a list written here rots silently the
        first time the manifest grows -- a new field would then be absent
        from the name, and two recordings that differ only in that field
        would share one file. The pinned key set below is the other half
        of the same guarantee: a walk can never visit a key the manifest
        no longer has, so a REMOVED field would narrow the name without
        failing anything. Pinning the set fails in both directions, which
        makes whoever adds or drops a field come and read this.
        """
        wanted = record.manifest_for(800.0, record.DEFAULT_VOICE)
        assert set(wanted) == {
            'format_version', 'voice', 'sample_rate', 'channels',
            'sample_width', 'lead_in_s', 'gap_s', 'lead_out_s',
            'sentences',
        }, ('the recording name is a digest of this whole manifest; a key '
            'added or removed here changes which recordings share a name')

        name = record.recording_name(wanted)
        for key in wanted:
            altered = dict(wanted)
            # Different from the original whatever its type is, and still
            # JSON, which is the form the digest is taken over.
            altered[key] = ['changed', wanted[key]]
            assert record.recording_name(altered) != name, (
                f'{key} does not reach the recording name, so two '
                f'recordings that differ only in {key} share one file')


_CONFIG = """# Parakeet provider
[engine]
# Parakeet is ~400ms per inference on CPU
re_inference_interval_ms = 600
endpoint_silence_ms = 600

[debug]
# wh-stt-load-metrics. Turn it on for a load test, off afterwards.
log_load_diagnostics = false
"""


class TestFindingTheRunningApplication:
    """Which checkout the run reads and writes.

    The tool measures a WheelHouse that is already running, and that
    application has its own checkout. A copy of this tool in a git worktree
    sits in a different one: it would flip a flag in a config file the
    running provider never reads, then wait for a log line in a file the
    application never writes. The run has to be able to name the checkout it
    is measuring.
    """

    def test_the_paths_default_to_this_files_own_checkout(self):
        paths = run.find_paths()
        assert paths.sherpa_config == (
            paths.repo_root / 'services' / 'stt_providers'
            / 'sherpa_offline_parakeet_stt_server' / 'config.toml')
        assert paths.app_config == (
            paths.repo_root / 'services' / 'wheelhouse' / 'config.toml')
        assert paths.log_file == paths.repo_root / 'wheelhouse.log'
        assert (paths.repo_root / 'services' / 'stt_providers').is_dir()

    def test_a_named_checkout_replaces_every_path(self, tmp_path):
        """Every path moves together. A run that took its config from one
        checkout and its log from another would flip a flag nobody reads and
        then report on somebody else's lines.
        """
        paths = run.find_paths(tmp_path)
        assert paths.repo_root == tmp_path
        for path in (paths.sherpa_config, paths.app_config, paths.log_file):
            assert tmp_path in path.parents


class TestTouchingTheConfigFiles:
    """Turning the diagnostic line on, and putting the file back."""

    def test_flipping_a_flag_leaves_every_other_line_alone(self):
        """These files are mostly comments explaining what each setting is
        for. A round trip through a TOML writer would delete all of it, so
        the edit is one line and the rest of the bytes are untouched.
        """
        flipped = run.set_toml_flag(_CONFIG, 'log_load_diagnostics', True)
        assert 'log_load_diagnostics = true' in flipped
        assert flipped.count('\n') == _CONFIG.count('\n')
        for line in _CONFIG.splitlines():
            if 'log_load_diagnostics' not in line:
                assert line in flipped

    def test_a_flag_that_appears_twice_is_refused(self):
        """Editing the first of two matches would silently change the wrong
        setting and report success -- the same failure a mutation gate has
        to refuse when its anchor is ambiguous.
        """
        with pytest.raises(ValueError):
            run.set_toml_flag(_CONFIG + '\nlog_load_diagnostics = false\n',
                              'log_load_diagnostics', True)

    def test_a_flag_that_is_not_there_is_refused(self):
        with pytest.raises(ValueError):
            run.set_toml_flag(_CONFIG, 'no_such_flag', True)

    def test_the_endpoint_setting_is_read_from_the_provider_config(self, tmp_path):
        config = tmp_path / 'config.toml'
        config.write_text(_CONFIG, encoding='utf-8')
        assert run.read_endpoint_silence_ms(config) == 600.0

    def test_a_config_that_cannot_be_read_falls_back_to_the_coded_default(
            self, tmp_path):
        """The provider itself falls back to 800 when the file names no
        value, so a recording built for anything else would be built for a
        provider that is not running.
        """
        assert run.read_endpoint_silence_ms(tmp_path / 'missing.toml') == 800.0
        broken = tmp_path / 'broken.toml'
        broken.write_text('[engine\n', encoding='utf-8')
        assert run.read_endpoint_silence_ms(broken) == 800.0

    def test_a_setting_that_is_not_a_finite_number_falls_back(self, tmp_path):
        """`true` and `inf` are both valid TOML at this key, and both reach
        the pacing code as numbers unless something rejects them: a bool is
        an int in Python, so `true` reads as a 1 ms threshold and leaves a
        3 ms gap between sentences, far under the margin that makes each
        sentence its own utterance; `inf` reaches time.sleep and raises
        OverflowError. The other persisted numeric reader in this tool,
        load_baseline, already refuses both.
        """
        for text in ('true', 'inf', '-inf', 'nan', '0', '-5'):
            config = tmp_path / 'config.toml'
            config.write_text(
                '[engine]\nendpoint_silence_ms = ' + text + '\n',
                encoding='utf-8')
            assert run.read_endpoint_silence_ms(config) == 800.0, text

    def test_a_restore_does_not_overwrite_someone_elses_edit(self, tmp_path):
        """This machine runs several sessions against one checkout. Putting
        a config file back by writing the whole of the old content over it
        would delete whatever anyone else had written meanwhile, to tidy up
        after a test.
        """
        config = tmp_path / 'config.toml'
        config.write_text(_CONFIG, encoding='utf-8')
        edit = run.FlagEdit(config, 'log_load_diagnostics', True)
        assert edit.apply() is True
        elsewhere = _CONFIG.replace('num_threads', 'x') + '\n# someone else\n'
        config.write_text(elsewhere, encoding='utf-8')
        edit.restore()
        assert config.read_text(encoding='utf-8') == elsewhere

    def test_a_restore_puts_back_exactly_what_was_there(self, tmp_path):
        config = tmp_path / 'config.toml'
        config.write_bytes(_CONFIG.encode('utf-8'))
        edit = run.FlagEdit(config, 'log_load_diagnostics', True)
        edit.apply()
        edit.restore()
        assert config.read_bytes() == _CONFIG.encode('utf-8')


class TestReadingTheRunsOwnSliceOfTheLog:
    """wheelhouse.log is append-only and shared, and it rotates."""

    def test_only_the_lines_written_after_the_mark_are_read(self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('before the run\n', encoding='utf-8')
        mark = run.log_mark(log)
        with open(log, 'a', encoding='utf-8') as handle:
            handle.write('during the run\n')
        text, rotated = run.read_since(log, mark)
        assert text == 'during the run\n'
        assert rotated is False

    def test_a_rotated_log_is_reported_rather_than_read_from_the_mark(
            self, tmp_path):
        """Seeking to a mark taken before a rotation lands in the middle of
        an unrelated line, and the run would report a measurement made from
        whatever bytes happened to line up.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('a' * 500 + '\n', encoding='utf-8')
        mark = run.log_mark(log)
        log.write_text('after rotation\n', encoding='utf-8')
        text, rotated = run.read_since(log, mark)
        assert rotated is True
        assert text == 'after rotation\n'

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def test_the_baseline_ratio_survives_a_round_trip(self, tmp_path):
        run.save_baseline(tmp_path, 0.73, self._provenance(tmp_path))
        assert run.load_baseline(
            tmp_path, self._provenance(tmp_path)) == pytest.approx(0.73)

    def test_a_missing_baseline_reads_as_no_baseline(self, tmp_path):
        assert run.load_baseline(
            tmp_path, self._provenance(tmp_path)) is None


class TestGuidingTheSpeaker:
    """The default run has a person speak the six sentences.

    ENABLE_AUDIO_SUPPRESSION is on in this project's config, so WheelHouse
    stops listening while anything plays through the speakers. Playing the
    recording would therefore be heard by nothing. A person speaks instead,
    and the pacing the recording used to carry moves onto the prompts.
    """

    def test_the_prompts_are_the_six_sentences_in_order(self):
        plan = script.prompt_plan(600)
        assert [p.sentence for p in plan] == list(script.SENTENCES)
        assert [p.index for p in plan] == [1, 2, 3, 4, 5, 6]

    def test_the_first_prompt_waits_for_the_vad_to_hear_the_room(self):
        """The lead-in silence the recording carried is still needed: the
        VAD measures its noise floor before any speech arrives.
        """
        plan = script.prompt_plan(600)
        assert plan[0].silence_before_s == script.LEAD_IN_S

    def test_every_later_prompt_waits_out_the_endpoint_threshold(self):
        """The per-sentence join in judge.py needs each sentence to become
        its own utterance. The provider closes one after
        endpoint_silence_ms of trailing silence, so the script holds the
        next prompt back until that has passed with margin.
        """
        for endpoint_ms in (600, 800, 2000):
            plan = script.prompt_plan(endpoint_ms)
            for prompt in plan[1:]:
                assert prompt.silence_before_s == script.inter_sentence_gap_s(
                    endpoint_ms)
                assert prompt.silence_before_s > endpoint_ms / 1000.0

    def test_the_silence_is_slept_before_the_sentence_is_shown(self):
        """A prompt shown first and paced afterwards would be read aloud
        during the gap it was supposed to create, and two sentences would
        land in one utterance.
        """
        events = []
        run.speak_sentences(
            script.prompt_plan(600), lead_out_s=1.8,
            sleep=lambda s: events.append(('sleep', s)),
            wait=lambda: events.append(('wait', None)),
            out=lambda text: events.append(('out', text)))

        for prompt in script.prompt_plan(600):
            shown = next(i for i, (kind, value) in enumerate(events)
                         if kind == 'out' and prompt.sentence in str(value))
            slept = [value for kind, value in events[:shown]
                     if kind == 'sleep']
            # Counting rather than searching: every gap in this plan after
            # the first is the same number, so "some sleep of 1.8s happened
            # earlier" is satisfied by the PREVIOUS sentence's gap and would
            # pass for five of the six sentences with no pacing at all.
            assert len(slept) == prompt.index, (
                f'sentence {prompt.index} was shown after {len(slept)} '
                'silences')
            assert slept[-1] == prompt.silence_before_s
            waited = [i for i, (kind, _) in enumerate(events)
                      if kind == 'wait' and i > shown]
            assert waited, f'sentence {prompt.index} was not waited for'

    def test_the_last_sentence_is_given_time_to_reach_its_endpoint(self):
        """Without the lead-out the sixth utterance can still be open when
        the run stops reading the log, and the sentence reads as missing.
        """
        events = []
        # 4.2 rather than the 1.8 this plan's own gaps use: with the two the
        # same, dropping the lead-out entirely leaves the sixth sentence's
        # gap as the last sleep, of the same value, and the test passes
        # having proved nothing.
        run.speak_sentences(
            script.prompt_plan(600), lead_out_s=4.2,
            sleep=lambda s: events.append(('sleep', s)),
            wait=lambda: None, out=lambda text: None)
        assert events[-1] == ('sleep', 4.2)
        assert len(events) == len(script.SENTENCES) + 1

    def test_the_recorded_playback_is_off_unless_it_is_asked_for(self):
        """This machine suppresses speech while sound plays through the
        speakers, so the recorded playback cannot be the default. The code
        stays for a future headset loop, behind a switch.
        """
        assert run.build_parser().parse_args([]).playback is False
        assert run.build_parser().parse_args(['--playback']).playback is True


class TestStoppingWhatTheRunStarted:
    """The load and the config flags, put back whatever went wrong."""

    def test_a_spinner_that_fails_to_start_does_not_orphan_the_others(
            self, monkeypatch):
        """Process creation is exactly what this test makes fail: it fills
        the machine on purpose. Spinners held only in a local list are lost
        with the exception, and the run reports it is over while the machine
        stays saturated.

        The failure is injected into ``Popen._execute_child``, the private
        hook the production registration overrides, rather than into
        ``subprocess.Popen`` itself: the real constructor has to run for
        the registration to be under test at all, and faking the hook is
        what keeps five real busy loops off this shared machine. The fake
        creates no child, so ``_child_created`` stays false and
        ``Popen.__del__`` returns without warning about a live process.
        """
        started = []

        def fake_execute_child(popen, *args, **kwargs):
            if len(started) == 2:
                raise OSError('no more processes')
            popen.pid = len(started) + 1
            started.append(popen)

        monkeypatch.setattr(subprocess.Popen, '_execute_child',
                            fake_execute_child)
        held: list = []
        with pytest.raises(OSError):
            run.start_spinners(5, into=held)
        assert held == started
        assert len(held) == 2

    def test_a_started_spinner_is_held_by_the_caller(self, monkeypatch):
        started = []

        def fake_execute_child(popen, *args, **kwargs):
            popen.pid = len(started) + 1
            started.append(popen)

        monkeypatch.setattr(subprocess.Popen, '_execute_child',
                            fake_execute_child)
        held: list = []
        assert run.start_spinners(3, into=held) is held
        assert held == started
        assert len(held) == 3

    def test_an_interrupt_after_the_child_exists_still_holds_it(
            self, monkeypatch):
        """The one moment a spinner can be lost for good.

        Windows has already created the busy loop, and the constructor has
        not yet handed the object back, so the caller's list is still
        empty. ``Popen.__init__``'s own failure cleanup does not kill the
        child and ``Popen.__del__`` does not either (both read on CPython
        3.12.10), so the run finishes, reports, and leaves one core at full
        load until an operator finds it by hand.

        The interrupt is delivered by wrapping ``subprocess.Popen.__init__``
        rather than ``_execute_child``: the child has to be alive and the
        constructor has to be on its way out, which is exactly the window
        the fix has to close. The wrapper keeps the object itself, so this
        test can kill the child it made even in the failing case, where the
        production code hands it nothing.
        """
        created: list = []
        real_init = subprocess.Popen.__init__

        def init_then_interrupt(popen, *args, **kwargs):
            real_init(popen, *args, **kwargs)
            created.append(popen)
            raise KeyboardInterrupt('ctrl-c on the way out of Popen')

        monkeypatch.setattr(subprocess.Popen, '__init__',
                            init_then_interrupt)
        held: list = []
        try:
            with pytest.raises(KeyboardInterrupt):
                run.start_spinners(1, into=held)

            assert len(created) == 1
            assert created[0].poll() is None
            assert held == created

            run.stop_spinners(held, out=lambda text: None)

            assert created[0].poll() is not None
        finally:
            for spinner in created:
                try:
                    spinner.kill()
                except OSError:
                    pass
                try:
                    spinner.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def test_a_restore_that_fails_does_not_skip_the_others(self, tmp_path):
        """The flags are restored in reverse. An exception in the first
        used to skip the rest, so a run that could not put LOG_TRANSCRIPTS
        back also left the provider writing a diagnostic line every ten
        seconds, with a traceback in place of the real failure.
        """
        said = []

        class _Edit:
            def __init__(self, path, key, value, fails):
                self.path = path
                self.key = key
                self.value = value
                self._fails = fails
                self.restored = False
                self.changed = True

            def restore(self):
                if self._fails:
                    raise OSError('the file is gone')
                self.restored = True
                self.changed = False

        first = _Edit(tmp_path / 'a.toml', 'log_load_diagnostics', True,
                      fails=False)
        second = _Edit(tmp_path / 'b.toml', 'LOG_TRANSCRIPTS', True,
                       fails=True)
        run.restore_all([first, second], out=said.append)

        assert first.restored is True
        message = '\n'.join(said)
        assert str(tmp_path / 'b.toml') in message
        assert 'LOG_TRANSCRIPTS' in message
        assert 'false' in message


class TestNoTestStartsTheRealSpinner:
    """The two layers of the ``no_real_spinners`` fixture, each proved on
    its own: a spinner started during a test runs the harmless command,
    and the real command is refused before a child exists.
    """

    def test_a_spinner_started_during_a_test_runs_the_harmless_command(
            self, monkeypatch):
        """``start_spinners`` reads ``SPINNER_ARGV`` when it is called, not
        when the module is imported, so the fixture's replacement is what
        runs. The child creation is faked the way the holding tests fake
        it; ``Popen.__init__`` records ``args`` before it creates anything.
        """
        started = []

        def fake_execute_child(popen, *args, **kwargs):
            popen.pid = len(started) + 1
            started.append(popen)

        monkeypatch.setattr(subprocess.Popen, '_execute_child',
                            fake_execute_child)
        held: list = []
        run.start_spinners(2, into=held)

        assert [list(spinner.args) for spinner in held] == (
            [HARMLESS_SPINNER_ARGV] * 2)

    def test_the_real_spinner_command_is_refused_before_a_child_exists(
            self):
        """The refusal lands inside ``Popen.__init__`` before
        ``_execute_child`` runs, so ``held`` stays empty. Whatever did get
        started is stopped in the ``finally``: with the refusal removed
        this test starts one real busy loop, and must not leave it.
        """
        held: list = []
        try:
            with pytest.raises(pytest.fail.Exception) as refused:
                run._HeldSpinner(held, list(REAL_SPINNER_ARGV),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        finally:
            run.stop_spinners(held, out=lambda text: None)

        assert held == []
        assert 'real spinner command' in str(refused.value)


class TestARotatedRunRecordsNoBaseline:
    """A calibration run that lost part of itself must record nothing.

    ``read_since`` answers a rotation by reading the replacement file from
    its start, so the run holds a fragment. ``worst_ratio`` then searched
    only that fragment, and the worst per-utterance ratio the machine
    produced could be in the file that rotated away. Nothing marks a saved
    baseline as partial -- the provenance names the machine, the provider
    and the endpoint setting, not completeness -- so the fragment became
    this machine's calibration for every later load run, and a calibration
    that is too low turns ordinary work into a reported failure
    (finding wh-load-test-script.1.22).
    """

    def _parsed(self, *ratios):
        return logparse.ParsedLog(
            utterances=[_utterance(index=i, ratio=r)
                        for i, r in enumerate(ratios, start=1)],
            windows=[_window()])

    def test_a_rotated_run_offers_no_ratio_to_save(self):
        parsed = self._parsed(0.61, 0.83, 0.70)

        assert run.baseline_ratio_to_save(parsed, rotated=True) is None

    def test_a_whole_run_still_offers_its_worst_ratio(self):
        """The guard this fix must not weaken: an ordinary calibration run
        still records the worst ratio it measured.
        """
        parsed = self._parsed(0.61, 0.83, 0.70)

        assert run.baseline_ratio_to_save(parsed, rotated=False) == 0.83

    def test_a_run_with_no_ratios_still_offers_none(self):
        parsed = logparse.ParsedLog(utterances=[], windows=[_window()])

        assert run.baseline_ratio_to_save(parsed, rotated=False) is None

    def test_the_refusal_removes_an_earlier_baseline(self, tmp_path):
        """The whole point of answering None rather than a fragment: the
        stale calibration on disk goes too. No baseline is a safe state, a
        wrong one is not.
        """
        provenance = {'checkout': str(tmp_path), 'provider': 'p',
                      'endpoint_silence_ms': 900}
        run.save_baseline(tmp_path, 0.70, provenance)
        parsed = self._parsed(0.61)

        ratio = run.baseline_ratio_to_save(parsed, rotated=True)
        saved = run.save_baseline(tmp_path, ratio, provenance)

        assert saved is False
        assert not run.baseline_file(tmp_path).exists()


class TestARotatedRunSaysSoInTheReport:
    """The rendered block has to carry the rotation itself.

    A load run that lost part of its log keeps its verdict: it measured
    some of its sentences, and the report already names the ones it could
    not measure (ruling of 2026-08-30 on wh-load-test-script.1.22, on the
    same reasoning as the partly measured ruling in .1.5). What it must
    not do is keep that fact out of the block. The rotation notice used to
    print before the report, and the procedure tells the operator to copy
    the block into the verdict template -- so the one thing that says how
    much of the run this verdict rests on was the one thing left behind.
    The same weakness .1.18 fixed for the late-sentence field.
    """

    def _rendered_lines(self, rotated):
        parsed = logparse.ParsedLog(utterances=[_utterance()],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _all_correct(), None)
        return report.render(
            machine='IKON', logical_cores=20, provider='Parakeet v3 (CPU)',
            busy_percent=91.0, baseline_ratio=None, parsed=parsed,
            sentences=_all_correct(), verdict=verdict,
            playback_underflows=None, rotated=rotated).splitlines()

    def test_a_rotated_run_names_the_rotation_in_the_block(self):
        named = [line for line in self._rendered_lines(rotated=True)
                 if 'the log rotated' in line]

        assert len(named) == 1, named

    def test_a_whole_run_names_no_rotation(self):
        """The guard on the other side: an ordinary run must not carry a
        warning about a rotation that did not happen.
        """
        lines = self._rendered_lines(rotated=False)

        assert not [line for line in lines if 'rotated' in line]

    def test_the_rotation_sits_between_the_verdict_and_its_evidence(self):
        """Beside the verdict, above the evidence, and above the two gap
        lines: the log rotating is the widest of the three statements
        about how much of the run this verdict rests on, so it is read
        first.
        """
        lines = self._rendered_lines(rotated=True)
        verdict_at = [i for i, line in enumerate(lines)
                      if line.startswith('VERDICT:')]
        evidence_at = [i for i, line in enumerate(lines)
                       if line.startswith('Evidence for the verdict')]
        rotated_at = [i for i, line in enumerate(lines)
                      if 'the log rotated' in line]

        assert len(rotated_at) == 1, lines
        assert verdict_at[0] < rotated_at[0] < evidence_at[0]

    def test_the_rotation_line_says_what_it_costs_the_reader(self):
        """A bare "the log rotated" leaves the reader to work out what it
        means for the numbers under it. It has to say that the block may
        be reporting on part of the run.
        """
        named = [line for line in self._rendered_lines(rotated=True)
                 if 'the log rotated' in line]

        assert 'part of' in named[0]

    def _rotation_bullet(self):
        """The procedure's whole rotation bullet, as one line.

        The marker is put back in front of the tail, because the phrase
        these tests match is the bullet's own opening. Whitespace is
        collapsed after the bullet is cut out, on the real newlines, so a
        phrase still matches when the document wraps it across two lines.
        """
        marker = '- **`the log rotated'
        doc = _procedure_text()
        _, sep, tail = doc.partition(marker)
        assert sep, 'the procedure does not describe a rotated run'
        return ' '.join((marker + tail).split('\n- ')[0].split())

    def test_the_procedure_names_the_line_the_report_prints(self):
        """Tied to the report's own words rather than restating them, so a
        renamed line breaks this too. The operator has to be able to match
        the printed line to the rule that explains it.
        """
        printed = [line.strip() for line in self._rendered_lines(rotated=True)
                   if 'the log rotated' in line]
        phrase = printed[0].split(', so ')[0]

        assert phrase in self._rotation_bullet()

    def test_the_procedure_keeps_the_verdict_and_refuses_the_baseline(self):
        """The two halves of the ruling, in the one place the operator
        reads. A document that told them to discard a rotated load run
        would undo the verdict half; one that stayed quiet about the
        baseline would leave them expecting a calibration that the run
        deliberately did not record.
        """
        bullet = self._rotation_bullet()

        assert 'keeps its verdict' in bullet
        assert 'records no calibration' in bullet


class TestASecondInterruptStillFinishesTheCleanup:
    """A second Ctrl+C during teardown used to abandon the rest of it.

    ``KeyboardInterrupt`` inherits from ``BaseException``, so the
    ``OSError`` and ``TimeoutExpired`` handlers in these two loops never
    saw it and it left the loop at the item it arrived on. What survived
    was worse than a traceback: spinner processes nobody killed, each
    holding a core at full load until the operator found it by hand, and
    for ``--log-transcripts`` a config file still carrying
    ``LOG_TRANSCRIPTS = true``, which keeps dictated text reaching the log.
    Both loops now finish their work and raise the interrupt afterwards.
    """

    class _Spinner:
        def __init__(self, pid, interrupt_on='', times_out=False):
            self.pid = pid
            self._interrupt_on = interrupt_on
            self._times_out = times_out
            self.killed = False
            self.waited = False

        def kill(self):
            self.killed = True
            if self._interrupt_on == 'kill':
                raise KeyboardInterrupt

        def wait(self, timeout=None):
            self.waited = True
            if self._interrupt_on == 'wait':
                raise KeyboardInterrupt
            if self._times_out:
                raise subprocess.TimeoutExpired(
                    cmd='spinner', timeout=timeout)

    class _Edit:
        def __init__(self, key, interrupts=False):
            self.path = Path('config.toml')
            self.key = key
            self.value = True
            self.changed = True
            self.restored = False
            self._interrupts = interrupts

        def restore(self):
            if self._interrupts:
                raise KeyboardInterrupt
            self.restored = True
            self.changed = False
            return True

    def test_an_interrupt_killing_one_spinner_still_kills_the_rest(self):
        spinners = [self._Spinner(1, interrupt_on='kill'),
                    self._Spinner(2), self._Spinner(3)]

        with pytest.raises(KeyboardInterrupt):
            run.stop_spinners(spinners)

        assert [s.killed for s in spinners] == [True, True, True]

    def test_an_interrupt_waiting_on_one_spinner_still_reaps_the_rest(self):
        """The kills are already issued by this loop, but a child nobody
        waits on stays a zombie process, and the run never reports which
        spinner refused to go.
        """
        spinners = [self._Spinner(1, interrupt_on='wait'),
                    self._Spinner(2), self._Spinner(3)]

        with pytest.raises(KeyboardInterrupt):
            run.stop_spinners(spinners)

        assert [s.waited for s in spinners] == [True, True, True]

    def test_an_interrupt_in_the_timeout_warning_still_reaps_the_rest(self):
        """The timeout warning is a cleanup line like any other, and a
        Ctrl+C landing while it prints used to leave the whole ``try``
        statement. An exception raised inside an ``except`` arm is not
        caught by a sibling arm of the same ``try``, so the
        ``except KeyboardInterrupt`` below the warning never saw it: the
        spinners after the one that timed out were never waited on, and a
        child whose kill did not land kept a core at full load.

        The assertion is the whole waited list, not merely that something
        was raised. The defect raised a KeyboardInterrupt too -- it just
        raised it from the wrong place, three lines too early.
        """
        spinners = [self._Spinner(1, times_out=True),
                    self._Spinner(2), self._Spinner(3)]
        said = []

        def out(message):
            said.append(message)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run.stop_spinners(spinners, out=out)

        assert [s.waited for s in spinners] == [True, True, True]
        # ``_say``'s bound: one retry, then the line is given up so an
        # operator holding Ctrl+C down cannot pin the teardown here.
        assert len(said) == 2

    def test_a_timeout_warning_keeps_an_interrupt_the_kill_loop_stored(self):
        """The warning that prints without incident must hand back the
        interrupt it was given. Passing anything else into ``_say`` -- or
        dropping what it returns -- would erase a Ctrl+C the kill loop
        already caught, and the run would exit as though nothing had
        happened.
        """
        spinners = [self._Spinner(1, interrupt_on='kill'),
                    self._Spinner(2, times_out=True)]
        said = []

        with pytest.raises(KeyboardInterrupt):
            run.stop_spinners(spinners, out=said.append)

        assert [s.waited for s in spinners] == [True, True]
        assert len(said) == 1

    def test_the_timeout_warning_goes_to_stdout_by_default(self, capsys):
        """The call site in ``main`` passes no ``out``, so the default is
        what the operator actually reads. No red for this one: the
        pre-fix code printed the same line. It is here to pin the default
        the injection point introduced.
        """
        spinners = [self._Spinner(4242, times_out=True)]

        run.stop_spinners(spinners)

        assert '4242' in capsys.readouterr().out

    def test_an_ordinary_stop_raises_nothing(self):
        """The guard this fix must not weaken."""
        spinners = [self._Spinner(1), self._Spinner(2)]

        run.stop_spinners(spinners)

        assert [s.killed for s in spinners] == [True, True]

    def test_an_interrupt_restoring_one_flag_still_restores_the_rest(self):
        """Restores run in reverse, so the interrupting edit is the LAST
        one given here and the one that must still be put back is the
        first.
        """
        first = self._Edit('log_load_diagnostics')
        second = self._Edit('LOG_TRANSCRIPTS', interrupts=True)

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([first, second], out=lambda _m: None)

        assert first.restored is True

    def test_the_restart_notice_survives_the_interrupt(self):
        """The interrupt is raised after the whole function, not after the
        loop. The operator whose LOG_TRANSCRIPTS went back still has to be
        told the running application keeps the test value until a restart,
        and that is exactly the case in which they are least likely to look
        at the config file themselves.
        """
        said = []
        first = self._Edit('LOG_TRANSCRIPTS')
        second = self._Edit('log_load_diagnostics', interrupts=True)

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([first, second], out=said.append)

        message = '\n'.join(said)
        assert 'DICTATED TEXT' in message

    def test_an_ordinary_restore_raises_nothing(self):
        """The guard this fix must not weaken."""
        edit = self._Edit('log_load_diagnostics')

        run.restore_all([edit], out=lambda _m: None)

        assert edit.restored is True


class TestCountingACaptureEventOnce:
    """An utterance happens inside a window, and both lines are deltas of
    the same capture counter. Adding the two lists charges every event
    twice, and the doubled number is what an operator pastes into the
    record the next fix decision is made from."""

    def test_an_overflow_counted_by_both_lines_is_one_overflow(self):
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=3, ratio=0.75)],
            windows=[_window(overflow=3)])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert 'overflow total 3' in verdict.evidence

    def test_a_drop_counted_by_both_lines_is_one_drop(self):
        parsed = logparse.ParsedLog(
            utterances=[_utterance(drops=17, ratio=0.75)],
            windows=[_window(drops=17)])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert 'drops total 17' in verdict.evidence

    def test_without_window_lines_the_utterance_deltas_are_the_total(self):
        """The window lines tile the whole run, so they are the total when
        they exist. A slice that caught none of them still has to report a
        number rather than nothing.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(index=1, overflow=2, drops=5, ratio=0.75),
                        _utterance(index=2, overflow=1, drops=0, ratio=0.75)],
            windows=[])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert 'overflow total 3' in verdict.evidence
        assert 'drops total 5' in verdict.evidence

    def test_a_slice_with_no_window_lines_can_still_read_clean(self):
        """A kind of line the slice caught none of is not a counter that
        stopped. Reading an empty list as unmeasured would make every
        window-less run undetermined, however clean the utterances were --
        and `neither` is one of the outcomes this run exists to award.

        The fixture has to be clean for this to prove anything: the partial
        flag is consulted only when no failure fired, so a fixture with an
        overflow in it would reach `callback loss` and never look.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=0, drops=0, ratio=0.75)],
            windows=[])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.NEITHER

    def test_a_window_line_the_slice_missed_cannot_erase_an_overflow(self):
        """The window lines do not always cover the whole run: the slice can
        start mid-window, and the one that carried the overflow is then not
        in it. Preferring the window lines outright would report zero over an
        utterance line saying forty-eight, and turn `callback loss` into
        `neither` -- a worse error than the double count this replaces.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=48, ratio=0.75)],
            windows=[_window(overflow=0)])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert 'overflow total 48' in verdict.evidence
        assert verdict.verdict == judge.CALLBACK_LOSS

    def test_an_utterance_that_counted_nothing_still_blocks_a_clean_reading(
            self):
        """`n/a` is not zero. A provider that stopped counting partway
        cannot earn `neither`, whichever line stopped.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=None, ratio=0.75)],
            windows=[_window(overflow=0)])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.UNDETERMINED
        assert any('part or all' in line for line in verdict.evidence)


# The line wheelhouse itself writes when a provider returns a final
# transcript, taken from integrations/websocket_manager.py:1198. The process
# name and the source file differ from the provider-forwarded lines above
# because this one is written on the wheelhouse side, which is the whole
# reason it survives to the log.
LOGIC_PREFIX = ("2026-08-30 12:40:02,953 [INFO] LogicProcess - "
                "websocket_manager.py:1198 - trace=T-17881079974 - ")
FINAL_LINE = (LOGIC_PREFIX
              + "[FINAL] UTT-95: 'the quick brown fox jumps over the lazy dog'")


class TestReadingTheTranscriptWheelhouseWrites:
    """[FINAL] is the only transcript line that reaches wheelhouse.log.

    [vad_utt_stats] cannot: shared_stt/audio_processor.py:50-64 says the
    line is deliberately left on the package logger, and a provider silences
    that package wholesale (the Parakeet server sets propagate = False on
    "shared_stt" and re-attaches its forwarding handler to the load-metrics
    child alone). Measured on a real run: 0 [vad_utt_stats] lines against
    117 [load-diag] lines. Wheelhouse writes [FINAL] in its own process, so
    it survives, and it carries the same utterance number the [load-diag]
    line carries, which is the join this tool needs.
    """

    def test_the_final_line_is_read_as_a_transcript(self):
        line = logparse.parse_log(FINAL_LINE).transcripts[0]
        assert line.utt == 95
        assert line.text == 'the quick brown fox jumps over the lazy dog'

    def test_a_final_reason_is_kept_out_of_the_transcript(self):
        """websocket_manager.py appends " (final_reason=X)" after the closing
        quote when the provider names one. Reading to the end of the line
        would put that suffix inside the transcript and make every sentence
        carrying it compare as wrong.
        """
        text = FINAL_LINE + ' (final_reason=endpoint)'
        line = logparse.parse_log(text).transcripts[0]
        assert line.text == 'the quick brown fox jumps over the lazy dog'
        assert line.kind == 'endpoint'

    def test_an_apostrophe_in_the_transcript_survives(self):
        """The quotes around the text are written by an f-string, not by %r,
        so an apostrophe inside the transcript is NOT escaped. Reading the
        field as a Python literal fails on exactly the words a person is
        most likely to say.
        """
        text = FINAL_LINE.replace('the quick brown fox jumps over the lazy dog',
                                  "don't stop the recording")
        assert (logparse.parse_log(text).transcripts[0].text
                == "don't stop the recording")

    def test_a_redacted_final_line_reads_as_the_placeholder(self):
        """Both wheelhouse-side lines pass the text through redact_transcript,
        so under the transcript-privacy default the log holds the placeholder.
        judge.py already knows what to do with it; the parser must not.
        """
        text = FINAL_LINE.replace(
            'the quick brown fox jumps over the lazy dog',
            '<redacted: 44 chars, 9 words>')
        assert (logparse.parse_log(text).transcripts[0].text
                == '<redacted: 44 chars, 9 words>')

    def test_one_utterance_is_counted_once_when_both_lines_appear(self):
        """A log captured on the provider side can carry both lines for the
        same utterance. Two records for one utterance would shift every
        later sentence in the alignment by one and report the tail missing.
        """
        both = (VAD_LINE.replace('utt=3', 'utt=95') + '\n' + FINAL_LINE)
        transcripts = logparse.parse_log(both).transcripts
        assert len(transcripts) == 1
        assert transcripts[0].utt == 95

    def test_the_final_line_wins_over_the_vad_line(self):
        """The [FINAL] text is what wheelhouse acted on. [vad_utt_stats] is
        written before the final is sent, so where they differ the [FINAL]
        line is the one the operator is judging.
        """
        both = (VAD_LINE.replace('utt=3', 'utt=95') + '\n' + FINAL_LINE)
        assert (logparse.parse_log(both).transcripts[0].text
                == 'the quick brown fox jumps over the lazy dog')
        reversed_order = (FINAL_LINE + '\n'
                          + VAD_LINE.replace('utt=3', 'utt=95'))
        assert (logparse.parse_log(reversed_order).transcripts[0].text
                == 'the quick brown fox jumps over the lazy dog')

    def test_a_final_line_whose_text_is_not_quoted_is_refused(self):
        """The quotes are what mark where the transcript begins and ends.
        Without them the unwrap would chop a character off each side of
        whatever is there and report the remains as what was heard.
        """
        text = LOGIC_PREFIX + '[FINAL] UTT-95: the quick brown fox'
        assert logparse.parse_log(text).transcripts == []

    def test_a_final_tag_inside_another_message_is_not_a_transcript(self):
        """Same anchoring rule as every other line this module reads: a
        command name or a quoted string in another subsystem's message can
        contain anything, including this tag.
        """
        noise = (LOGIC_PREFIX + "Command '[FINAL] UTT-9: "
                 "'not a transcript'' matched")
        assert logparse.parse_log(noise).transcripts == []

    def test_the_six_sentences_are_judged_from_final_lines_alone(self):
        """The end-to-end shape of the live run: [load-diag] utt= lines for
        the numbers and [FINAL] lines for the words, with nothing else.
        """
        lines = []
        for position, sentence in enumerate(script.SENTENCES):
            utt = 100 + position
            heard = script.expected_transcript(sentence)
            lines.append(UTT_LINE.replace('utt=3', f'utt={utt}'))
            lines.append(LOGIC_PREFIX + f"[FINAL] UTT-{utt}: '{heard}'")
        parsed = logparse.parse_log('\n'.join(lines))
        outcomes = judge.judge_sentences(parsed.transcripts)
        assert [r.outcome for r in outcomes.results] == [judge.CORRECT] * 6
        assert [r.utt for r in outcomes.results] == list(range(100, 106))


class TestAPartlyMeasuredRunIsStillJudged:
    """A run keeps its verdict while any window carried capture readings.

    The measured live run is the reason: 13 of its 83 window lines read
    capture=unavailable, all inside one five-minute band, while the other 70
    windows and every per-utterance line carried real numbers and every
    sentence was transcribed. Refusing the whole run there would refuse
    exactly the runs this tool exists to measure, and the old advice to fix
    the microphone named a fault that was not present.
    """

    def _windows(self, available: int, unavailable: int) -> str:
        lines = [WINDOW_LINE] * available + [UNAVAILABLE_LINE] * unavailable
        return '\n'.join(lines)

    def test_some_unavailable_windows_do_not_refuse_the_run(self):
        parsed = logparse.parse_log(self._windows(70, 13))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        assert verdict.verdict != judge.CAPTURE_UNAVAILABLE

    def test_the_missing_windows_are_counted_on_the_verdict(self):
        parsed = logparse.parse_log(self._windows(70, 13))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        assert verdict.windows_missing_capture == 13
        assert verdict.windows_total == 83

    def test_a_fully_measured_run_counts_nothing_missing(self):
        parsed = logparse.parse_log(self._windows(5, 0))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        assert verdict.windows_missing_capture == 0

    def test_a_run_with_no_readings_at_all_is_still_refused(self):
        """The condition for refusing is that NOTHING was measured, not that
        something was missed.
        """
        parsed = logparse.parse_log(self._windows(0, 4))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        assert verdict.verdict == judge.CAPTURE_UNAVAILABLE
        assert verdict.windows_missing_capture == 4

    def test_the_refusal_no_longer_blames_the_microphone(self):
        """Every sentence in the live run was transcribed while 13 windows
        read unavailable, so the microphone was working. The evidence names
        what actually happened instead.
        """
        parsed = logparse.parse_log(self._windows(0, 4))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        joined = ' '.join(verdict.evidence)
        assert 'microphone' not in joined
        assert 'capture-stats reader' in joined

    def test_the_missing_count_prints_beside_the_verdict(self):
        """Unmeasured windows weaken the callback-loss against inference-lag
        distinction, so the reader has to see the gap in the same glance as
        the verdict rather than at the end of the evidence list.
        """
        parsed = logparse.parse_log(self._windows(70, 13))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        rendered = report.render(
            machine='IKON', logical_cores=20, provider='Parakeet v3 (CPU)',
            busy_percent=91.0, baseline_ratio=None, parsed=parsed,
            sentences=_all_correct(), verdict=verdict,
            playback_underflows=None, rotated=False).splitlines()
        verdict_at = [i for i, line in enumerate(rendered)
                      if line.startswith('VERDICT:')]
        evidence_at = [i for i, line in enumerate(rendered)
                       if line.startswith('Evidence for the verdict')]
        gap_at = [i for i, line in enumerate(rendered)
                  if 'capture readings missing' in line]
        assert len(gap_at) == 1, rendered
        assert verdict_at[0] < gap_at[0] < evidence_at[0]
        assert '13 of 83' in rendered[gap_at[0]]

    def test_a_fully_measured_run_prints_no_gap_line(self):
        parsed = logparse.parse_log(self._windows(5, 0))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        rendered = report.render(
            machine='IKON', logical_cores=20, provider='Parakeet v3 (CPU)',
            busy_percent=91.0, baseline_ratio=None, parsed=parsed,
            sentences=_all_correct(), verdict=verdict,
            playback_underflows=None, rotated=False)
        assert 'capture readings missing' not in rendered

    def _capture_bullet(self):
        """The procedure's whole `capture=unavailable` bullet, as one line.

        Whitespace is collapsed after the bullet is cut out, so a phrase
        these tests look for still matches when the document wraps it
        across two lines. The cut happens first, on the real newlines.
        """
        doc = _procedure_text()
        _, sep, tail = doc.partition('- **`capture=unavailable`')
        assert sep, 'the procedure does not describe capture=unavailable'
        return ' '.join(tail.split('\n- ')[0].split())

    def test_the_procedure_does_not_stop_the_run_for_one_marked_window(self):
        """The document is the operator's instruction, so it decides what
        happens to a run whatever the tool computes. It used to say that a
        window reading capture=unavailable means stop the test and fix the
        microphone before reading anything else, which is the advice this
        ruling removed: an operator following it discards the loaded runs
        the tool exists to measure and hunts a fault the tool declines to
        claim (finding wh-load-test-script.1.20).
        """
        parsed = logparse.parse_log(self._windows(70, 13))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        assert verdict.verdict != judge.CAPTURE_UNAVAILABLE

        bullet = self._capture_bullet()
        assert 'stop the test' not in bullet
        assert 'fix the microphone' not in bullet

    def test_the_procedure_names_the_line_the_report_prints_for_the_gap(self):
        """Tied to the report's own words rather than restating them, so a
        renamed field breaks this too. The operator has to be able to match
        the printed line to the rule that explains it.
        """
        parsed = logparse.parse_log(self._windows(70, 13))
        verdict = judge.judge_run(parsed, _all_correct(), None)
        rendered = report.render(
            machine='IKON', logical_cores=20, provider='Parakeet v3 (CPU)',
            busy_percent=91.0, baseline_ratio=None, parsed=parsed,
            sentences=_all_correct(), verdict=verdict,
            playback_underflows=None, rotated=False)
        printed = [line.strip() for line in rendered.splitlines()
                   if 'capture readings missing' in line]
        assert len(printed) == 1, rendered
        phrase = printed[0].split(' for ')[0]

        assert phrase in self._capture_bullet()

    def test_the_procedure_states_the_condition_for_refusing_a_run(self):
        """The refusal is all-or-nothing, and the document has to say which:
        an operator who reads "some windows were unavailable" as the refusal
        condition throws away the same runs the old advice did.
        """
        assert judge.CAPTURE_UNAVAILABLE in self._capture_bullet()
        assert 'every window' in self._capture_bullet()


class TestNothingIsDictatedAsPunctuation:
    """No sentence ends with punctuation a reader would speak.

    Wheelhouse is a dictation system. A reader who says the period puts the
    word in the transcript, and the recognizer mishears it as often as not:
    the live run of 2026-08-30 produced 'the quick brown fox jumps over the
    lazy dog kid' and 'check the microphone little period'. Each marked a
    sentence wrong for a reason that had nothing to do with load.
    """

    def test_no_sentence_ends_with_spoken_punctuation(self):
        for sentence in script.SENTENCES:
            assert sentence[-1] not in '.,?!;:', sentence

    def test_the_expected_text_carries_no_punctuation_word(self):
        """The check above is about the script. This one is about what the
        comparison expects, which is what a wrong label rests on.
        """
        for sentence in script.SENTENCES:
            expected = script.expected_transcript(sentence)
            assert not expected.endswith('period'), expected
            assert expected[-1] not in '.,?!;:', expected


class TestACompoundWordDoesNotFailASentence:
    """The one liberty the comparison takes, in both directions.

    The recognizer writes some compounds as one word: the live run produced
    'seashells' and 'seashore' where the fixed script has 'sea shells' and
    'sea shore'. Nothing in the script prevents that and it says nothing
    about load. Two adjacent words on one side may equal one word on the
    other, and nothing further -- a wrong, missing or extra word is the
    failure this test exists to catch and still reads as wrong.
    """

    def test_a_compound_the_recognizer_joined_is_correct(self):
        assert judge._matches('she sells seashells by the seashore',
                              'she sells sea shells by the sea shore')

    def test_a_compound_the_recognizer_split_is_correct(self):
        assert judge._matches('she sells sea shells by the sea shore',
                              'she sells seashells by the seashore')

    def test_the_fifth_sentence_reads_correct_when_joined(self):
        """The live run's own case, end to end through the sentence judge."""
        heard = 'she sells seashells by the seashore on Saturday'
        outcomes = judge.judge_sentences([_said(5, heard)],
                                         [script.SENTENCES[4]])
        assert outcomes.results[0].outcome == judge.CORRECT

    def test_a_substituted_word_is_still_wrong(self):
        assert not judge._matches('she sells sea shells on Sunday',
                                  'she sells sea shells on Saturday')

    def test_a_missing_word_is_still_wrong(self):
        assert not judge._matches('she sells shells on Saturday',
                                  'she sells sea shells on Saturday')

    def test_an_extra_word_is_still_wrong(self):
        """The dictated period is exactly this case. Removing the periods
        from the script is what keeps it from firing; the tolerance must not
        start forgiving it as well.
        """
        assert not judge._matches('she sells sea shells on Saturday period',
                                  'she sells sea shells on Saturday')

    def test_three_words_joined_into_one_is_still_wrong(self):
        """Two adjacent words is the whole of the liberty. A third joined in
        would start forgiving a dropped word.
        """
        assert not judge._matches('she sells seashellsstall',
                                  'she sells sea shells stall')

    def test_the_words_still_have_to_be_in_order(self):
        assert not judge._matches('sells she sea shells',
                                  'she sells sea shells')


class TestEveryFlagGoesBackAfterAnInterrupt:
    """The cleanup has to survive an interrupt arriving around the write.

    Two settings make this load-bearing. log_load_diagnostics left on makes
    the provider write a diagnostic line every ten seconds from then on.
    LOG_TRANSCRIPTS left on keeps writing the words a person dictated to
    disk, after the tool has said it would put the setting back.
    """

    def _edit(self, tmp_path):
        config = tmp_path / 'config.toml'
        config.write_bytes(_CONFIG.encode('utf-8'))
        return config, run.FlagEdit(config, 'log_load_diagnostics', True)

    def test_an_interrupt_straight_after_the_write_still_restores(
            self, tmp_path, monkeypatch):
        """apply() used to set changed = True only after the write returned.
        A KeyboardInterrupt delivered between those two statements left the
        file flipped while changed stayed false, so restore() returned at
        once and the flag was still on when the run ended.
        """
        config, edit = self._edit(tmp_path)
        real_replace = os.replace

        def replace_then_interrupt(src, dst):
            real_replace(src, dst)
            raise KeyboardInterrupt

        monkeypatch.setattr(run.os, 'replace', replace_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            edit.apply()
        assert b'log_load_diagnostics = true' in config.read_bytes()
        monkeypatch.setattr(run.os, 'replace', real_replace)
        run.restore_all([edit])
        assert config.read_bytes() == _CONFIG.encode('utf-8')

    def test_a_write_that_never_landed_is_not_called_someone_elses_edit(
            self, tmp_path, monkeypatch, capsys):
        """The file holds exactly what it held before, so there is nothing
        to put back and nothing to warn about. Reporting a concurrent edit
        here would send the operator looking for a change nobody made.
        """
        config, edit = self._edit(tmp_path)

        def refuse(src, dst):
            raise OSError('disk full')

        monkeypatch.setattr(run.os, 'replace', refuse)
        with pytest.raises(OSError):
            edit.apply()
        run.restore_all([edit])
        assert config.read_bytes() == _CONFIG.encode('utf-8')
        assert 'changed while the test ran' not in capsys.readouterr().out

    def test_a_failed_write_leaves_no_temporary_file_behind(
            self, tmp_path, monkeypatch):
        """A leftover temporary file beside config.toml is litter in the
        directory the application reads.
        """
        config, edit = self._edit(tmp_path)

        def refuse(src, dst):
            raise OSError('disk full')

        monkeypatch.setattr(run.os, 'replace', refuse)
        with pytest.raises(OSError):
            edit.apply()
        assert [p.name for p in tmp_path.iterdir()] == ['config.toml']

    def test_a_concurrent_edit_is_still_left_alone(self, tmp_path):
        """The guard this fix must not weaken."""
        config, edit = self._edit(tmp_path)
        edit.apply()
        elsewhere = (_CONFIG + '\n# someone else\n').encode('utf-8')
        config.write_bytes(elsewhere)
        run.restore_all([edit])
        assert config.read_bytes() == elsewhere


class TestTheMutationGatePutsTheSourceBack:
    """The gate rewrites real source files, so its restore carries weight.

    An interrupt that skips the restore leaves a mutant in the working tree,
    where every later run reads it as the real code.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _target(self, tmp_path, monkeypatch):
        gate = self._gate()
        target = tmp_path / 'source.py'
        target.write_bytes(b'value = 1\n')
        monkeypatch.setattr(gate, '_clear_pycache', lambda service: (None, None))
        # Stub the suite run for every test in this class, including the
        # ones that never reach it. A test that reaches the real
        # _run_pytest spawns `uv run pytest` over this whole file from
        # inside this file's own run, and the gate's 300-second timeout
        # then kills the round with no verdict. That is not theoretical:
        # the-mutant-write-truncates-instead-of-replacing removes the
        # os.replace this class relies on to raise, so the mutant ran
        # straight into a nested full-suite run and timed out.
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda service, test_file, mutant=False:
            subprocess.CompletedProcess([], 0, '', ''))
        return gate, target, b'value = 1\n', b'value = 2\n'

    def test_an_interrupt_while_the_suite_runs_restores_the_source(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)

        def interrupt(service, test_file, mutant=False):
            raise KeyboardInterrupt

        monkeypatch.setattr(gate, '_run_pytest', interrupt)
        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')
        assert target.read_bytes() == original

    def test_an_interrupt_straight_after_the_mutant_write_restores(
            self, tmp_path, monkeypatch):
        """The mutant write used to sit outside the try, so the finally that
        restores never ran when the interrupt arrived between them.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        real_write = gate._atomic_write

        def write_then_interrupt(path, data):
            real_write(path, data)
            raise KeyboardInterrupt

        monkeypatch.setattr(gate, '_atomic_write', write_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')
        assert target.read_bytes() == original

    def test_a_concurrent_edit_during_the_run_is_still_refused(
            self, tmp_path, monkeypatch):
        """The guard this fix must not weaken: a save landing from an editor
        while pytest ran must not be replaced by the pre-run snapshot.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        somebody_else = b'value = 99\n'

        def edit_the_file(service, test_file, mutant=False):
            target.write_bytes(somebody_else)
            return subprocess.CompletedProcess([], 0, '', '')

        monkeypatch.setattr(gate, '_run_pytest', edit_the_file)
        _, _, error = gate._mutate_and_run(target, original, mutant, 'probe')
        assert error and 'refusing to overwrite' in error
        assert target.read_bytes() == somebody_else

    def test_a_failed_mutant_write_leaves_no_temporary_file_behind(
            self, tmp_path, monkeypatch):
        """A half-written source file matches neither the original nor the
        mutant, so the restore reads it as somebody else's edit and refuses
        to touch it. The move either happens or it does not.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)

        def refuse(src, dst):
            raise OSError('disk full')

        monkeypatch.setattr(gate.os, 'replace', refuse)
        with pytest.raises(OSError):
            gate._mutate_and_run(target, original, mutant, 'probe')
        assert target.read_bytes() == original
        assert [p.name for p in tmp_path.iterdir()] == ['source.py']


class TestASentenceWithoutItsOwnNumbersIsNamed:
    """A verdict is only as complete as the sentences it measured.

    judge_run used to accept any single per-utterance line as enough: one
    engine_ratio out of six was enough to reach `neither` while five
    sentences carried no numbers at all. The run keeps its verdict, which is
    the ruling the capture gap already got and for the same reason, and the
    report names the sentences that went unmeasured beside the verdict.
    """

    def _partly(self, measured):
        """Six sentences heard, per-utterance lines only for ``measured``."""
        return logparse.ParsedLog(
            utterances=[_utterance(index=i) for i in measured],
            windows=[_window()])

    def _rendered_lines(self, measured):
        parsed = self._partly(measured)
        verdict = judge.judge_run(parsed, _all_correct(), 0.7)
        return report.render(
            machine='IKON', logical_cores=20, provider='Parakeet v3 (CPU)',
            busy_percent=91.0, baseline_ratio=0.7, parsed=parsed,
            sentences=_all_correct(), verdict=verdict,
            playback_underflows=None, rotated=False).splitlines()

    def test_a_partly_measured_run_keeps_its_verdict(self):
        verdict = judge.judge_run(self._partly([1]), _all_correct(), 0.7)
        assert verdict.verdict == judge.NO_FAILURE

    def test_the_unmeasured_sentences_are_named(self):
        verdict = judge.judge_run(self._partly([1, 3]), _all_correct(), 0.7)
        assert verdict.sentences_missing_metrics == (2, 4, 5, 6)
        assert verdict.sentences_total == 6

    def test_a_fully_measured_run_names_none(self):
        verdict = judge.judge_run(
            self._partly([1, 2, 3, 4, 5, 6]), _all_correct(), 0.7)
        assert verdict.sentences_missing_metrics == ()

    def test_a_run_with_no_per_utterance_line_is_still_undetermined(self):
        """The all-empty path the ruling explicitly kept."""
        parsed = logparse.ParsedLog(utterances=[], windows=[_window()])
        verdict = judge.judge_run(parsed, _all_correct(), 0.7)
        assert verdict.verdict == judge.UNDETERMINED

    def test_the_missing_sentences_print_beside_the_verdict(self):
        """Beside the verdict, not at the end of the evidence: the reader has
        to see how much of the run was measured in the same glance as the
        answer that measurement produced.
        """
        rendered = self._rendered_lines([1, 3])
        verdict_at = [i for i, line in enumerate(rendered)
                      if line.startswith('VERDICT:')]
        evidence_at = [i for i, line in enumerate(rendered)
                       if line.startswith('Evidence for the verdict')]
        gap_at = [i for i, line in enumerate(rendered)
                  if 'per-utterance measurements missing' in line]
        assert len(gap_at) == 1, rendered
        assert verdict_at[0] < gap_at[0] < evidence_at[0]
        assert '4 of 6 sentences: 2, 4, 5, 6' in rendered[gap_at[0]]

    def test_a_fully_measured_run_prints_no_missing_line(self):
        rendered = self._rendered_lines([1, 2, 3, 4, 5, 6])
        assert not [line for line in rendered
                    if 'per-utterance measurements missing' in line]

    def test_the_heading_states_how_many_sentences_were_measured(self):
        """The heading used to read "all six" whatever the real count was."""
        rendered = self._rendered_lines([1, 3])
        heading = [line for line in rendered
                   if line.startswith('Under-load per-utterance lines')]
        assert heading == ['Under-load per-utterance lines: '
                           '2 of 6 sentences measured']
class TestTheRunningApplicationStillHasTheOldValue:
    """Putting the file back is not the same as turning the setting off.

    The launcher reads LOG_TRANSCRIPTS once at startup and exports
    WHEELHOUSE_LOG_TRANSCRIPTS to the Logic, Input, GUI and provider
    children, and the sherpa provider builds its load reporter once from the
    startup configuration. Restoring the bytes therefore changes nothing in
    a WheelHouse that is already running, and saying nothing about that
    leaves the operator believing the logging stopped.
    """

    def _flipped(self, tmp_path, key):
        path = tmp_path / 'config.toml'
        path.write_bytes(f'{key} = false\n'.encode('utf-8'))
        edit = run.FlagEdit(path, key, True)
        assert edit.apply() is True
        return edit

    def test_a_flag_that_went_back_still_needs_a_restart(self, tmp_path):
        edit = self._flipped(tmp_path, 'log_load_diagnostics')
        said = []
        run.restore_all([edit], out=said.append)
        assert edit.path.read_text(encoding='utf-8') == (
            'log_load_diagnostics = false\n')
        told = '\n'.join(said)
        assert 'log_load_diagnostics' in told
        assert 'restart' in told.lower()

    def test_the_dictated_text_keeps_reaching_the_log_until_then(
            self, tmp_path):
        edit = self._flipped(tmp_path, 'LOG_TRANSCRIPTS')
        said = []
        run.restore_all([edit], out=said.append)
        told = '\n'.join(said).lower()
        assert 'dictated text' in told
        assert 'restart' in told

    def test_a_flag_that_was_already_on_says_nothing(self, tmp_path):
        path = tmp_path / 'config.toml'
        path.write_bytes(b'log_load_diagnostics = true\n')
        edit = run.FlagEdit(path, 'log_load_diagnostics', True)
        assert edit.apply() is False
        said = []
        run.restore_all([edit], out=said.append)
        assert said == []

    def test_a_flag_the_run_refused_to_put_back_is_not_announced(
            self, tmp_path):
        """A flag still set is not a flag that went back.

        ``restore`` refuses to overwrite an edit this run did not write, so
        the setting is still on. Announcing it as put back would tell the
        operator the opposite of the truth.
        """
        edit = self._flipped(tmp_path, 'log_load_diagnostics')
        edit.path.write_bytes(
            b'log_load_diagnostics = true\n# theirs\n')
        said = []
        run.restore_all([edit], out=said.append)
        assert not any('restart' in line.lower() for line in said)

    def test_a_failed_restore_still_says_which_value_to_set(self, tmp_path):
        """The existing disclosure must survive beside the new one."""
        edit = self._flipped(tmp_path, 'log_load_diagnostics')
        edit.path.unlink()
        said = []
        run.restore_all([edit], out=said.append)
        assert any('log_load_diagnostics' in line and 'false' in line
                   for line in said)


class TestTheGateRestoreIsAllOrNothing:
    """The restore side of the same defect the mutant write already fixed.

    ``_restore`` opened the live source in truncating write mode, so an
    interrupt or a disk failure part way through recovery left bytes that
    match neither the original nor the mutant -- the one state the next run
    refuses to touch.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _target(self, tmp_path, monkeypatch):
        gate = self._gate()
        target = tmp_path / 'source.py'
        target.write_bytes(b'value = 1\n')
        monkeypatch.setattr(gate, '_clear_pycache', lambda service: (None, None))
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda service, test_file, mutant=False:
            subprocess.CompletedProcess([], 0, '', ''))
        return gate, target, b'value = 1\n', b'value = 2\n'

    def _fail_the_second_move(self, gate, monkeypatch):
        """Let the mutant land, then refuse the move that restores."""
        real = gate.os.replace
        calls = []

        def move(src, dst):
            calls.append(dst)
            if len(calls) == 1:
                return real(src, dst)
            raise OSError('disk full')

        monkeypatch.setattr(gate.os, 'replace', move)

    def test_a_restore_that_cannot_finish_leaves_the_mutant_whole(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._fail_the_second_move(gate, monkeypatch)

        _, _, error = gate._mutate_and_run(target, original, mutant, 'probe')

        assert error and 'MUTANT REMAINS' in error
        assert target.read_bytes() == mutant

    def test_a_failed_restore_leaves_no_temporary_file_behind(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._fail_the_second_move(gate, monkeypatch)

        gate._mutate_and_run(target, original, mutant, 'probe')

        assert [p.name for p in tmp_path.iterdir()] == ['source.py']

    def test_an_ordinary_restore_still_puts_the_original_back(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)

        _, _, error = gate._mutate_and_run(target, original, mutant, 'probe')

        assert error is None
        assert target.read_bytes() == original


class TestASentenceSpokenOutOfOrderIsNotClean:
    """The leftover pass must not rebuild an order that did not happen.

    It hands unmatched sentences the transcripts nothing claimed, in order.
    With every sentence heard but two of them swapped, that reconstructed
    the intended pairing and reported a clean run. Counted over the six
    exact transcripts, 131 of the 719 non-identity orders were reported as
    six correct with no extras.
    """

    def _heard(self, order):
        expected = [judge.expected_transcript(s) for s in judge.SENTENCES]
        return [logparse.Transcript(utt=n + 1, text=expected[i], kind=None)
                for n, i in enumerate(order)]

    def test_a_swapped_pair_is_not_reported_as_six_correct(self):
        outcomes = judge.judge_sentences(self._heard([1, 0, 2, 3, 4, 5]))
        assert not all(r.outcome == judge.CORRECT
                       for r in outcomes.results)

    def test_the_sentence_that_was_not_heard_in_place_reads_missing(self):
        outcomes = judge.judge_sentences(self._heard([1, 0, 2, 3, 4, 5]))
        assert outcomes.results[1].outcome == judge.MISSING

    def test_the_words_nothing_claimed_become_an_extra(self):
        outcomes = judge.judge_sentences(self._heard([1, 0, 2, 3, 4, 5]))
        assert len(outcomes.extras) == 1

    def test_a_reordering_further_apart_is_not_clean_either(self):
        outcomes = judge.judge_sentences(self._heard([0, 1, 5, 3, 4, 2]))
        assert not all(r.outcome == judge.CORRECT
                       for r in outcomes.results)

    def test_words_after_a_later_sentence_are_not_given_to_an_earlier_one(
            self):
        """The upper bound of the leftover pass.

        The spare utterance here arrived after the one that carried the
        third sentence, so calling it the second sentence's words would be
        a false statement about what was heard and in what order.
        """
        spoken = ['alpha', 'bravo', 'charlie']
        heard = ['alpha', 'charlie', 'zulu']
        transcripts = [logparse.Transcript(utt=n + 1, text=text, kind=None)
                       for n, text in enumerate(heard)]
        outcomes = judge.judge_sentences(transcripts, spoken)
        assert outcomes.results[1].outcome == judge.MISSING
        assert len(outcomes.extras) == 1

    def test_a_garbled_sentence_still_shows_what_was_heard(self):
        """The case the leftover pass exists for, which must not change."""
        expected = [judge.expected_transcript(s) for s in judge.SENTENCES]
        heard = list(expected)
        heard[2] = 'the words that actually came out'
        transcripts = [logparse.Transcript(utt=n + 1, text=t, kind=None)
                       for n, t in enumerate(heard)]
        outcomes = judge.judge_sentences(transcripts)
        assert outcomes.results[2].outcome == judge.WRONG
        assert 'actually came out' in outcomes.results[2].detail
        assert outcomes.extras == ()

    def test_every_sentence_in_order_is_still_clean(self):
        outcomes = judge.judge_sentences(self._heard([0, 1, 2, 3, 4, 5]))
        assert all(r.outcome == judge.CORRECT for r in outcomes.results)
        assert outcomes.extras == ()


def _gap_of(endpoint_silence_ms: float) -> float:
    return record.manifest_for(endpoint_silence_ms,
                               record.DEFAULT_VOICE)['gap_s']


def _gap_bytes(endpoint_silence_ms: float) -> bytes:
    """Stand-in audio that says which gap it was built for."""
    return f'gap={_gap_of(endpoint_silence_ms)}'.encode()


def _build_that_records_its_gap(wav_path, spec):
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(f"gap={spec['gap_s']}".encode())


class TestAnInterruptedRebuildKeepsThePreviousRecording:
    """A forced rebuild must not destroy a recording it cannot replace.

    ``--regenerate`` rebuilds a cache whose manifest already matches, and
    the export wrote straight to the final path. An interrupt then left a
    partial WAV beside a manifest that still called the cache current, and
    the next ordinary --playback run reused the partial file.
    """

    class _Track:
        """Stands in for the pydub segment, which owns the export."""

        def __init__(self, data=b'RIFF-new', fail=False):
            self.data = data
            self.fail = fail
            self.exported = []

        def export(self, path, format):  # noqa: A002 - pydub's own name
            self.exported.append(Path(path))
            Path(path).write_bytes(self.data)
            if self.fail:
                raise OSError('disk full')

    def test_a_failed_export_leaves_the_previous_recording_intact(
            self, tmp_path):
        wav = tmp_path / 'six.wav'
        wav.write_bytes(b'RIFF-old')
        with pytest.raises(OSError):
            record._export_atomically(self._Track(fail=True), wav)
        assert wav.read_bytes() == b'RIFF-old'

    def test_a_failed_export_leaves_no_temporary_file_behind(self, tmp_path):
        wav = tmp_path / 'six.wav'
        wav.write_bytes(b'RIFF-old')
        with pytest.raises(OSError):
            record._export_atomically(self._Track(fail=True), wav)
        assert [p.name for p in tmp_path.iterdir()] == ['six.wav']

    def test_a_finished_export_replaces_the_recording(self, tmp_path):
        wav = tmp_path / 'six.wav'
        wav.write_bytes(b'RIFF-old')
        record._export_atomically(self._Track(), wav)
        assert wav.read_bytes() == b'RIFF-new'

    def test_the_export_never_opens_the_recording_itself(self, tmp_path):
        """The property that makes the replacement all-or-nothing."""
        wav = tmp_path / 'six.wav'
        track = self._Track()
        record._export_atomically(track, wav)
        assert track.exported and track.exported[0] != wav

    def test_a_build_that_fails_leaves_the_manifest_alone(
            self, tmp_path, monkeypatch):
        wanted = record.manifest_for(800.0, record.DEFAULT_VOICE)
        wav = tmp_path / record.recording_name(wanted)
        wav.write_bytes(b'RIFF-old')
        manifest = wav.with_suffix('.json')
        manifest.write_text(json.dumps(wanted), encoding='utf-8')

        def fail(wav_path, spec):
            raise OSError('disk full')

        monkeypatch.setattr(record, '_build', fail)
        with pytest.raises(OSError):
            record.ensure_recording(800.0, cache_dir=tmp_path, force=True)
        assert json.loads(manifest.read_text(encoding='utf-8')) == wanted
        assert wav.read_bytes() == b'RIFF-old'

    def test_an_interrupted_publication_never_serves_the_wrong_recording(
            self, tmp_path, monkeypatch):
        """The WAV and the file that describes it are published separately.

        A rebuild for other inputs replaces the WAV first. An interrupt
        before the description is published leaves the new audio behind the
        old description, and a later run for the ORIGINAL inputs then finds
        a description that matches and reuses audio built for something
        else. The gap between sentences is what differs, so the provider
        can join two sentences and the run reads that as a load failure.
        """
        monkeypatch.setattr(record, '_build', _build_that_records_its_gap)
        record.ensure_recording(800.0, cache_dir=tmp_path)

        real_write_text = Path.write_text

        def interrupted(self, *args, **kwargs):
            # The description is the only .json the cache holds. Matching
            # on the suffix rather than on a fixed name is what lets this
            # hook attach whatever the description is called.
            if self.suffix == '.json':
                raise KeyboardInterrupt
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'write_text', interrupted)
        with pytest.raises(KeyboardInterrupt):
            record.ensure_recording(600.0, cache_dir=tmp_path)
        monkeypatch.setattr(Path, 'write_text', real_write_text)

        served, _ = record.ensure_recording(800.0, cache_dir=tmp_path)
        assert served.read_bytes() == _gap_bytes(800.0)

    def test_a_reader_in_the_publication_window_gets_its_own_recording(
            self, tmp_path, monkeypatch):
        """The same hole without an interrupt, reached by a second run.

        This machine runs several sessions against one checkout. A second
        run can look at the cache while the first is between replacing the
        WAV and publishing its description, and nothing in that window says
        the two no longer belong together.
        """
        looked: list[bool] = []
        seen: list[tuple] = []

        def build_then_look(wav_path, spec):
            _build_that_records_its_gap(wav_path, spec)
            if spec['gap_s'] != _gap_of(800.0) and not looked:
                looked.append(True)
                seen.append(record.ensure_recording(800.0,
                                                    cache_dir=tmp_path))

        monkeypatch.setattr(record, '_build', build_then_look)
        record.ensure_recording(800.0, cache_dir=tmp_path)
        record.ensure_recording(600.0, cache_dir=tmp_path)

        assert seen, 'the second run never looked inside the window'
        assert seen[0][0].read_bytes() == _gap_bytes(800.0)


class TestAReplacementLogIsNotAContinuation:
    """Rotation was decided from the size alone.

    WheelHouse rotates at ten megabytes with two backups, so the file the
    run started reading can be renamed away and a new one put in its place.
    Once that replacement grows past the old mark, a size comparison reads
    it as the same file that simply got longer, and the run seeks to a
    numeric position inside a different file -- discarding its beginning
    without saying so.
    """

    def test_a_replacement_that_grew_past_the_mark_is_still_a_rotation(
            self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('a' * 500 + '\n', encoding='utf-8')
        mark = run.log_mark(log)
        log.unlink()
        log.write_text('b' * 900 + '\n', encoding='utf-8')

        text, rotated = run.read_since(log, mark)

        assert rotated is True
        assert text.startswith('b')

    def test_the_same_file_growing_is_not_a_rotation(self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('before the run\n', encoding='utf-8')
        mark = run.log_mark(log)
        with open(log, 'a', encoding='utf-8') as handle:
            handle.write('during the run\n')

        text, rotated = run.read_since(log, mark)

        assert rotated is False
        assert text == 'during the run\n'

    def test_a_log_that_did_not_exist_at_the_mark_is_read_whole(
            self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        mark = run.log_mark(log)
        log.write_text('everything\n', encoding='utf-8')

        text, rotated = run.read_since(log, mark)

        assert text == 'everything\n'
        assert rotated is False
class TestTheGateChecksItsOwnPatterns:
    """A pattern goes stale the moment a fix rewrites the code it names.

    The gate already refuses a selected entry whose pattern does not match
    exactly once. What it could not do was notice staleness in an entry the
    run did not select, and a name-filtered run selects almost nothing. Four
    patterns went stale that way across two rounds of fixes -- the restore
    guard split into two statements, ``offset`` became ``mark.size``, and
    ``gap`` became a multi-line dict -- and every targeted run since kept
    reporting a clean scope.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _entry(self, path, name, old, new='x = 2\n'):
        return {'name': name, 'file': path, 'old': old, 'new': new,
                'expect': ['test_x']}

    @pytest.mark.skipif(
        bool(os.environ.get('STT_LOAD_TEST_MUTANT')),
        reason='a mutant is in the tree, so its own pattern cannot match')
    def test_every_pattern_in_the_real_gate_matches_exactly_once(self):
        _procedure_text()  # The development mutation gate also reads this input.
        gate = self._gate()

        assert gate.check_patterns() == []

    def test_a_pattern_that_matches_nothing_is_reported(self, tmp_path):
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\n')

        problems = gate.check_patterns(
            [self._entry(source, 'gone', 'y = 1\n')])

        assert problems == ['gone: pattern not found']

    def test_a_pattern_that_matches_twice_is_reported(self, tmp_path):
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\nx = 1\n')

        problems = gate.check_patterns(
            [self._entry(source, 'doubled', 'x = 1\n')])

        assert problems == ['doubled: pattern ambiguous (2 matches)']

    def test_a_pattern_that_matches_once_is_not_reported(self, tmp_path):
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\n')

        assert gate.check_patterns(
            [self._entry(source, 'fine', 'x = 1\n')]) == []

    def test_a_crlf_file_is_read_through_the_same_translation(self, tmp_path):
        """The check must translate line endings exactly as the run does.

        A gate that matched LF patterns against a CRLF file would report
        every multi-line pattern as stale and send the reader hunting for a
        rewrite that never happened.
        """
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\r\ny = 2\r\n')

        assert gate.check_patterns(
            [self._entry(source, 'multiline', 'x = 1\ny = 2\n')]) == []


class TestTheGateCanCheckWithoutMutating:
    """``--check`` answers "are the patterns current" in seconds.

    The full sweep takes hours, so the only cheap way to learn a pattern
    went stale used to be to start that sweep. The check touches no file
    and starts no test run.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _refuse_to_run(self, gate, monkeypatch):
        def refuse(*a, **k):
            raise AssertionError('the check must not run pytest')
        monkeypatch.setattr(gate, '_run_pytest', refuse)
        monkeypatch.setattr(gate, '_collected_names', refuse)
        monkeypatch.setattr(gate, '_clear_pycache', lambda service: (None, None))

    def test_the_check_reports_a_stale_pattern_and_fails(
            self, tmp_path, monkeypatch, capsys):
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\n')
        self._refuse_to_run(gate, monkeypatch)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'gone', 'file': source, 'old': 'y = 1\n',
             'new': 'y = 2\n', 'expect': ['test_x']}])
        monkeypatch.setattr(sys, 'argv', ['gate', '--check'])

        assert gate.main() == 1
        out = capsys.readouterr().out
        assert 'gone: pattern not found' in out

    def test_the_check_passes_when_every_pattern_matches(
            self, tmp_path, monkeypatch, capsys):
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\n')
        self._refuse_to_run(gate, monkeypatch)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'fine', 'file': source, 'old': 'x = 1\n',
             'new': 'x = 2\n', 'expect': ['test_x']}])
        monkeypatch.setattr(sys, 'argv', ['gate', '--check'])

        assert gate.main() == 0
        assert 'stale' in capsys.readouterr().out

    def test_the_check_reports_a_mutant_that_does_not_compile(
            self, tmp_path, monkeypatch, capsys):
        """The failure this check was blind to. A pattern can still match
        while the text it produces is not Python, and the full sweep is
        then the only thing that finds out -- hours in, after a run that
        proves nothing. It happened here: the .1.21 fix added a second
        except arm to restore_all's loop, and a mutation written when
        there was one arm left the second with no try above it.
        """
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'try:\n    x = 1\nexcept OSError:\n    x = 2\n')
        self._refuse_to_run(gate, monkeypatch)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'broken', 'file': source, 'old': 'try:\n',
             'new': 'if True:\n', 'expect': ['test_x']}])
        monkeypatch.setattr(sys, 'argv', ['gate', '--check'])

        assert gate.main() == 1
        out = capsys.readouterr().out
        assert 'broken: mutant does not compile' in out

    def test_the_check_does_not_compile_a_document_mutation(
            self, tmp_path, monkeypatch, capsys):
        """The guard on the other side. Prose is not Python, so compiling
        a document mutation would report every one of them as broken.
        """
        gate = self._gate()
        doc = tmp_path / 'procedure.md'
        doc.write_bytes(b'Run the test and read the verdict.\n')
        self._refuse_to_run(gate, monkeypatch)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'prose', 'file': doc, 'old': 'read the verdict',
             'new': 'ignore the verdict', 'expect': ['test_x']}])
        monkeypatch.setattr(sys, 'argv', ['gate', '--check'])

        assert gate.main() == 0
        assert 'does not compile' not in capsys.readouterr().out


class TestANameFilteredRunStillSeesStalenessElsewhere:
    """The gap that let four patterns rot unnoticed.

    Every round since the first ran a name filter, and a filtered run only
    checks the entries it selected. The unselected ones were never counted,
    so each run printed a clean scope while the set decayed behind it.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _run_one(self, gate, tmp_path, monkeypatch):
        source = tmp_path / 'source.py'
        source.write_bytes(b'x = 1\n')
        stale = tmp_path / 'other.py'
        stale.write_bytes(b'z = 9\n')
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'good', 'file': source, 'old': 'x = 1\n',
             'new': 'x = 2\n', 'expect': ['test_x']},
            {'name': 'rotten', 'file': stale, 'old': 'q = 0\n',
             'new': 'q = 1\n', 'expect': ['test_x']}])
        monkeypatch.setattr(gate, '_collected_names',
                            lambda service, tests: {'test_x'})
        monkeypatch.setattr(gate, '_clear_pycache', lambda service: (None, None))
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))
        monkeypatch.setattr(
            gate, '_mutate_and_run',
            lambda path, original, mutated, name: (
                subprocess.CompletedProcess(
                    [], 1, 'FAILED tests/t.py::test_x\n', ''),
                False, None))
        return source, stale

    def test_a_filtered_run_names_the_stale_pattern_it_did_not_select(
            self, tmp_path, monkeypatch, capsys):
        gate = self._gate()
        self._run_one(gate, tmp_path, monkeypatch)
        monkeypatch.setattr(sys, 'argv', ['gate', 'good'])

        gate.main()

        out = capsys.readouterr().out
        assert 'WARNING rotten: pattern not found' in out

    def test_the_selected_mutation_still_reports_its_own_verdict(
            self, tmp_path, monkeypatch, capsys):
        gate = self._gate()
        self._run_one(gate, tmp_path, monkeypatch)
        monkeypatch.setattr(sys, 'argv', ['gate', 'good'])

        assert gate.main() == 0
        assert 'caught good' in capsys.readouterr().out


class TestAMutantRunDoesNotCheckTheMutatedFile:
    """The self-check has to stand down while a mutant is in the tree.

    A mutant replaces the very bytes its own entry names, so the check
    would report that entry stale on every mutation run and put a failure
    with nothing behind it into every mutant's output.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def test_the_mutant_run_carries_the_marker_and_the_baseline_does_not(
            self, monkeypatch):
        gate = self._gate()
        # _run_pytest copies os.environ, and this suite is itself running
        # inside a mutant run whenever the gate exercises it -- so the
        # marker is already in the parent environment and the baseline
        # call inherits it. Without this the test failed under EVERY
        # mutation, which made the two mutations that name it as their
        # only catcher report a catch they had not earned.
        monkeypatch.delenv(gate.MUTANT_ENV, raising=False)
        seen = []
        monkeypatch.setattr(
            gate, '_run_held',
            lambda argv, **k: seen.append(k['env']) or
            subprocess.CompletedProcess(argv, 0, '', ''))

        gate._run_pytest(gate.SHARED, gate.TESTS)
        gate._run_pytest(gate.SHARED, gate.TESTS, mutant=True)

        assert gate.MUTANT_ENV not in seen[0]
        assert seen[1][gate.MUTANT_ENV] == '1'


class TestATimedOutRunDoesNotOutliveItsTimeout:
    """A pytest run the gate gives up on must be gone, all of it.

    The gate ran pytest through ``uv run`` under ``subprocess.run`` with a
    300 s timeout. On timeout ``subprocess.run`` kills the one process it
    started -- uv -- and pytest, with every spinner pytest had started,
    lived on while the gate moved to the next mutation and started
    another pytest beside them. Five timeouts in a row fit the jump from
    28 to 106 python.exe that the freeze monitor recorded as Ikon hung
    (wh-load-test-script.2).

    Now pytest is the direct child, started through the service's own
    interpreter, and held with everything it starts in a Windows job
    object; a timeout ends the job and waits for its active count to
    reach zero before the timeout is reported. The tests here use a
    child that starts a sleeping grandchild, not spinners: the point is
    the tree, and a sleep is the harmless shape of a process that would
    otherwise outlive its parent. Windows only, as the gate is.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    @staticmethod
    def _listed(pid: int) -> bool:
        """Whether ``tasklist`` still shows the process.

        The CSV row quotes the pid, so the match is on the number and not
        on the localised text around it.
        """
        out = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
            capture_output=True, text=True).stdout
        return f'"{pid}"' in out

    @pytest.mark.skipif(sys.platform != 'win32',
                        reason='the gate holds pytest in a Windows job')
    def test_a_timed_out_run_takes_its_grandchildren_with_it(self, tmp_path):
        """The child starts a grandchild that sleeps, writes its pid, and
        sleeps itself past the two-second timeout. The grandchild's own
        stdio is detached so that neither the fixed nor the unfixed gate
        can be kept waiting on a pipe it holds; the unfixed gate then
        returns with the grandchild still listed.
        """
        gate = self._gate()
        pid_file = tmp_path / 'grandchild.pid'
        child = (
            'import pathlib, subprocess, sys, time\n'
            'g = subprocess.Popen('
            '[sys.executable, "-c", "import time; time.sleep(60)"], '
            'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, '
            'stderr=subprocess.DEVNULL)\n'
            f'pathlib.Path({str(pid_file)!r}).write_text(str(g.pid))\n'
            'time.sleep(10)\n')
        grandchild = None
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                gate._run_held([sys.executable, '-c', child],
                               cwd=tmp_path, env=None, timeout=2)
            grandchild = int(pid_file.read_text())

            assert not self._listed(grandchild), (
                f'the grandchild {grandchild} survived the timeout')
        finally:
            if grandchild is None and pid_file.exists():
                grandchild = int(pid_file.read_text())
            if grandchild is not None and self._listed(grandchild):
                subprocess.run(['taskkill', '/F', '/PID', str(grandchild)],
                               capture_output=True)

    def test_pytest_is_started_through_the_venv_interpreter_as_a_direct_child(
            self):
        gate = self._gate()
        interpreter = gate._interpreter(gate.SHARED)

        argv = gate._pytest_argv(gate.SHARED, gate.TESTS, '-q')

        assert interpreter.parent.parent == gate.SHARED / '.venv'
        assert argv == [str(interpreter), '-m', 'pytest', gate.TESTS, '-q']
        assert 'uv' not in argv

    @pytest.mark.skipif(sys.platform != 'win32',
                        reason='the gate holds pytest in a Windows job')
    def test_an_interrupt_on_the_way_out_of_popen_still_ends_the_child(
            self, tmp_path, monkeypatch):
        """The one moment the gate's own pytest can be lost for good.

        Windows has already made the child and ``Popen.__init__`` has
        not handed it back, so a ``_run_held`` that holds the object it
        is given has nothing yet to put in the job. That constructor's
        own failure cleanup closes the pipes and re-raises without
        killing (read on CPython 3.12.10, Lib/subprocess.py lines
        1035-1062), and the job's kill-on-close ends only its members,
        so an unheld pytest outlives the gate that started it -- still
        running the test file, with a mutant possibly still in the
        tracked source it imported.

        The interrupt is delivered by wrapping ``subprocess.Popen.
        __init__`` rather than ``_execute_child``: the child has to be
        alive and the constructor on its way out, which is the window
        the fix has to close. The wrapper keeps the object, so this
        test can reap the child it made even in the failing case,
        where the gate was handed nothing. Same shape as the spinner's
        proof in TestNoTestStartsTheRealSpinner, and the same defect
        one process further out (.2.1.2).
        """
        gate = self._gate()
        created: list = []
        real_init = subprocess.Popen.__init__

        def init_then_interrupt(popen, *args, **kwargs):
            real_init(popen, *args, **kwargs)
            if created:
                # Only the gate's own child is interrupted. The
                # tasklist calls that read the verdict come through
                # this same constructor and must be left alone.
                return
            created.append(popen)
            raise KeyboardInterrupt('ctrl-c on the way out of Popen')

        monkeypatch.setattr(subprocess.Popen, '__init__',
                            init_then_interrupt)
        try:
            with pytest.raises(KeyboardInterrupt):
                gate._run_held(
                    [sys.executable, '-c', 'import time; time.sleep(60)'],
                    cwd=tmp_path, env=None, timeout=30)

            assert len(created) == 1, created
            child = created[0].pid
            deadline = time.monotonic() + 10
            while self._listed(child) and time.monotonic() < deadline:
                time.sleep(0.05)

            assert not self._listed(child), (
                f'the child {child} outlived the interrupted gate')
        finally:
            for made in created:
                try:
                    made.kill()
                except OSError:
                    pass
                try:
                    made.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def test_a_tree_that_did_not_end_stops_the_sweep(
            self, tmp_path, monkeypatch, capsys):
        """The refusal is not a verdict: the mutation is reported as an
        error and nothing after it runs, because its pytest would start
        on the machine the last one may still be saturating.
        """
        gate = self._gate()
        source = tmp_path / 'source.py'
        source.write_bytes(b'a = 1\nb = 2\n')
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'first', 'file': source, 'old': 'a = 1\n',
             'new': 'a = 0\n', 'expect': ['test_x']},
            {'name': 'second', 'file': source, 'old': 'b = 2\n',
             'new': 'b = 0\n', 'expect': ['test_x']}])
        monkeypatch.setattr(gate, '_collected_names',
                            lambda service, tests: {'test_x'})
        monkeypatch.setattr(gate, '_clear_pycache',
                            lambda service: (None, None))
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))
        ran = []

        def still_running(path, original, mutated, name):
            ran.append(name)
            raise gate.ProcessTreeNotHeld('2 process(es) still running')

        monkeypatch.setattr(gate, '_mutate_and_run', still_running)
        monkeypatch.setattr(sys, 'argv', ['gate'])

        code = gate.main()

        out = capsys.readouterr().out
        assert code == 1
        assert ran == ['first'], ran
        assert 'ERROR first: 2 process(es) still running' in out, out
        assert 'aborting remaining mutations' in out, out

    def test_a_restore_refused_while_the_tree_did_not_end_is_reported(
            self, tmp_path, monkeypatch, capsys):
        """The abort still has to say that the source file is not back.

        ``_restore`` refuses to overwrite a file another session saved
        while pytest ran, and returns that refusal instead of restoring.
        Nothing returns from ``_mutate_and_run`` on the abort path, so
        the refusal is heard only if the cleanup reports it on the way
        out. Without that the sweep ends looking orderly while the
        mutant sits in a tracked file -- the outcome ``_restore``'s own
        docstring calls the worst this gate has (.2.1.1).
        """
        gate = self._gate()
        source = tmp_path / 'source.py'
        original = b'a = 1\nb = 2\n'
        saved_by_someone_else = b'a = 1\nb = 3\n'
        source.write_bytes(original)
        monkeypatch.setattr(gate, 'MUTATIONS', [
            {'name': 'first', 'file': source, 'old': 'a = 1\n',
             'new': 'a = 0\n', 'expect': ['test_x']},
            {'name': 'second', 'file': source, 'old': 'b = 2\n',
             'new': 'b = 0\n', 'expect': ['test_x']}])
        monkeypatch.setattr(gate, '_collected_names',
                            lambda service, tests: {'test_x'})
        monkeypatch.setattr(gate, '_clear_pycache',
                            lambda service: (None, None))
        ran = []

        def saved_over_then_the_tree_survives(service, test_file,
                                              mutant=False):
            if not mutant:
                return subprocess.CompletedProcess([], 0, '', '')
            ran.append(test_file)
            source.write_bytes(saved_by_someone_else)
            raise gate.ProcessTreeNotHeld('2 process(es) still running')

        monkeypatch.setattr(gate, '_run_pytest',
                            saved_over_then_the_tree_survives)
        monkeypatch.setattr(sys, 'argv', ['gate'])

        code = gate.main()

        out = capsys.readouterr().out
        assert code == 1
        assert ran == [gate.TESTS], ran
        assert 'refusing to overwrite the concurrent edit' in out, out
        assert 'ERROR first: 2 process(es) still running' in out, out
        assert 'aborting remaining mutations' in out, out
        assert '2 errors' in out, out
        assert source.read_bytes() == saved_by_someone_else

    def test_a_missing_venv_interpreter_is_a_clear_refusal(
            self, tmp_path, monkeypatch, capsys):
        gate = self._gate()
        missing = tmp_path / 'nowhere' / 'python.exe'
        monkeypatch.setattr(gate, '_interpreter', lambda service: missing)

        def refuse(*a, **k):
            raise AssertionError('nothing may run without an interpreter')

        monkeypatch.setattr(gate, '_run_held', refuse)
        monkeypatch.setattr(sys, 'argv', ['gate'])

        assert gate.main() == 1
        out = capsys.readouterr().out
        assert str(missing) in out, out
        assert 'uv sync' in out, out


class TestTheMarkAndTheReadDescribeOneFile:
    """Two looks at a path can describe two different files.

    ``log_mark`` took the size through ``log_size`` and the identity through
    ``_file_identity``, which is two ``stat`` calls, and ``read_since``
    repeated that split before opening a third handle. A rotation landing
    between any two of them produced a mark or a decision that never
    described a real file: the old file's size beside the replacement's
    identity. Once the replacement grew past that size, both halves looked
    compatible, the run called it a continuation, and it seeked to a byte
    position inside a file whose beginning it then dropped in silence.
    """

    def test_the_mark_is_taken_from_one_look_at_the_file(
            self, tmp_path, monkeypatch):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('x\n', encoding='utf-8')
        real = Path.stat
        looks = []

        def counted(self, **kwargs):
            looks.append(self)
            return real(self, **kwargs)

        monkeypatch.setattr(Path, 'stat', counted)
        run.log_mark(log)

        assert looks == [log]

    def test_a_rotation_while_the_mark_is_taken_cannot_tear_it(
            self, tmp_path, monkeypatch):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('a' * 500 + '\n', encoding='utf-8')
        real = Path.stat
        done = []

        def rotate_after_the_first_look(self, **kwargs):
            info = real(self, **kwargs)
            if self == log and not done:
                done.append(True)
                log.unlink()
                log.write_text('b' * 200 + '\n', encoding='utf-8')
            return info

        monkeypatch.setattr(Path, 'stat', rotate_after_the_first_look)
        mark = run.log_mark(log)
        monkeypatch.setattr(Path, 'stat', real)
        with open(log, 'a', encoding='utf-8') as handle:
            handle.write('c' * 400 + '\n')

        text, rotated = run.read_since(log, mark)

        assert rotated is True
        assert text.startswith('b')

    def test_the_decision_follows_the_file_that_was_opened(
            self, tmp_path, monkeypatch):
        """A separate look at the path can name a file the read never touches.

        The rotation here lands after the decision would have been made and
        before the bytes are read, which is the sequence a stat-then-open
        pair cannot see.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('a' * 500 + '\n', encoding='utf-8')
        mark = run.log_mark(log)
        stale = log.stat()
        log.unlink()
        log.write_text('b' * 900 + '\n', encoding='utf-8')
        monkeypatch.setattr(Path, 'stat', lambda self, **kwargs: stale)

        text, rotated = run.read_since(log, mark)

        assert rotated is True
        assert text.startswith('b')

    def test_a_log_that_is_not_there_is_a_read_it_could_not_make(
            self, tmp_path):
        """This answer CHANGED with finding wh-load-test-script.1.36, and
        the change is deliberate rather than incidental.

        An absent file used to read as the empty slice, on the same
        reasoning ``log_mark`` still uses: a file nobody has created holds
        no history to mistake for this run, and the log is briefly absent
        during an ordinary rotation. That reasoning is about not reading
        the WRONG bytes, and it still holds for the mark.

        It does not hold for the read. ``main`` refuses at its preflight
        unless the log exists, so by the time of the final read the file
        was there at the preflight and there at the mark. Gone afterwards
        means either a rotation in progress -- which
        ``read_since_retrying`` waits out -- or a log that was removed,
        and then the run genuinely cannot say what it measured.
        """
        log = tmp_path / 'missing.log'

        assert run.read_since(log, run.log_mark(log)) is None
class TestNoTwoProcessesShareOneTemporaryFile:
    """Every all-or-nothing write named its temporary file after the target.

    Two copies of this tool on one machine, or a mutation gate run beside
    one, therefore wrote to the same neighbouring path. Each could move the
    other's half-written file over the target, which is the exact damage
    the temporary file exists to prevent. The process id makes the name the
    writer's own.
    """

    def _capture_the_temporary_path(self, monkeypatch):
        seen = []
        real = os.replace

        def capture(src, dst):
            seen.append(Path(src))
            real(src, dst)

        monkeypatch.setattr(os, 'replace', capture)
        return seen

    def test_the_config_write_names_its_temporary_file_for_this_process(
            self, tmp_path, monkeypatch):
        seen = self._capture_the_temporary_path(monkeypatch)
        target = tmp_path / 'config.toml'
        target.write_bytes(b'a = 1\n')

        run._atomic_write(target, b'a = 2\n')

        assert len(seen) == 1
        assert str(os.getpid()) in seen[0].name

    def test_the_recording_export_names_its_temporary_file_for_this_process(
            self, tmp_path, monkeypatch):
        seen = self._capture_the_temporary_path(monkeypatch)
        wav = tmp_path / 'fixed-script.wav'

        class Track:
            def export(self, path, format):
                Path(path).write_bytes(b'RIFF')

        record._export_atomically(Track(), wav)

        assert len(seen) == 1
        assert str(os.getpid()) in seen[0].name

    def test_the_gate_write_names_its_temporary_file_for_this_process(
            self, tmp_path, monkeypatch):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        seen = self._capture_the_temporary_path(monkeypatch)
        target = tmp_path / 'source.py'
        target.write_bytes(b'x = 1\n')

        gate._atomic_write(target, b'x = 2\n')

        assert len(seen) == 1
        assert str(os.getpid()) in seen[0].name

    def test_a_temporary_file_from_another_process_is_left_alone(
            self, tmp_path):
        """Cleanup removes this writer's file, not whatever else is there."""
        target = tmp_path / 'config.toml'
        target.write_bytes(b'a = 1\n')
        other = target.with_name(target.name + '.stt-load-test.999999.tmp')
        other.write_bytes(b'someone else half way through\n')

        run._atomic_write(target, b'a = 2\n')

        assert other.read_bytes() == b'someone else half way through\n'
        assert target.read_bytes() == b'a = 2\n'
class TestAGrowingLogIsNeverCalledAReplacement:
    """The identity has to be built from fields Windows reports stably.

    ``st_ctime_ns`` is not one of them. Measured on this machine,
    ``Path.stat`` and ``os.fstat`` on one unchanged file disagreed on it in
    1867 of 3000 pairs, and ``os.fstat`` disagreed with itself in 581 of
    2000. Carrying it in the identity made an ordinary growing log read as
    a replacement in about a third of reads, so the run threw away the
    slice it had just measured and reported the whole file instead.
    """

    def test_a_growing_log_is_not_a_rotation_however_often_it_is_read(
            self, tmp_path):
        for i in range(200):
            log = tmp_path / f'wheelhouse{i}.log'
            log.write_text('before the run\n', encoding='utf-8')
            mark = run.log_mark(log)
            with open(log, 'a', encoding='utf-8') as handle:
                handle.write('during the run\n')

            text, rotated = run.read_since(log, mark)

            assert rotated is False, f'read {i} reported a rotation'
            assert text == 'during the run\n'

    def test_a_filesystem_that_reports_no_file_index_has_no_identity(
            self, tmp_path):
        """Zero is not an identity: every file would carry the same one."""
        log = tmp_path / 'wheelhouse.log'
        log.write_text('x\n', encoding='utf-8')
        real = log.stat()
        no_index = os.stat_result(
            (real.st_mode, 0, real.st_dev, real.st_nlink, real.st_uid,
             real.st_gid, real.st_size, int(real.st_atime),
             int(real.st_mtime), int(real.st_ctime)))

        assert run._identity_of(no_index) is None

    def test_a_real_file_index_is_an_identity(self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('x\n', encoding='utf-8')

        assert run._identity_of(log.stat()) is not None
class TestABaselineSaysWhatItMeasured:
    """A cached ratio was reused whatever it had been measured against.

    The cache sits beside record.py, so it does not move with --repo-root.
    The documented worktree mode -- run the tool from checkout A against a
    running checkout B -- therefore calibrated on A and judged B, and the
    verdict ladder compares the run's ratio against that number. A baseline
    of 0.70 from A makes a 0.90 run read neither; B's own idle ratio of
    0.40 makes the same run undetermined, which is the true answer.
    """

    def _provenance(self, tmp_path, root='C:/checkout-b', provider='sherpa',
                    silence=800.0):
        return run.baseline_provenance(
            run.Paths(repo_root=Path(root),
                      sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            provider, silence)

    def test_a_baseline_is_read_back_when_it_matches_this_run(
            self, tmp_path):
        wanted = self._provenance(tmp_path)
        run.save_baseline(tmp_path, 0.42, wanted)

        assert run.load_baseline(tmp_path, wanted) == 0.42

    def test_a_baseline_measured_against_another_checkout_is_refused(
            self, tmp_path):
        run.save_baseline(tmp_path, 0.70,
                          self._provenance(tmp_path, root='C:/checkout-a'))
        said = []

        value = run.load_baseline(
            tmp_path, self._provenance(tmp_path, root='C:/checkout-b'),
            out=said.append)

        assert value is None
        assert any('checkout-a' in line and 'checkout-b' in line
                   for line in said)

    def test_a_baseline_measured_with_another_endpoint_setting_is_refused(
            self, tmp_path):
        run.save_baseline(tmp_path, 0.70,
                          self._provenance(tmp_path, silence=800.0))
        said = []

        value = run.load_baseline(
            tmp_path, self._provenance(tmp_path, silence=400.0),
            out=said.append)

        assert value is None
        assert any('endpoint_silence_ms' in line for line in said)

    def test_a_baseline_from_before_provenance_existed_is_refused(
            self, tmp_path):
        """An old file carries a ratio and nothing that says what it is."""
        run.baseline_file(tmp_path).write_text(
            json.dumps({'engine_ratio': 0.70}), encoding='utf-8')
        said = []

        assert run.load_baseline(
            tmp_path, self._provenance(tmp_path), out=said.append) is None
        assert said

    def test_a_missing_baseline_is_not_an_error(self, tmp_path):
        assert run.load_baseline(tmp_path, self._provenance(tmp_path)) is None


class TestACalibrationThatMeasuredNothingSaysSo:
    """``--baseline`` printed success whatever it had measured.

    ``save_baseline`` returned without writing when no per-utterance
    engine_ratio reached the log, while main printed the recorded line with
    None in it and exited 0. Any earlier baseline stayed on disk and the
    next load run read it as this machine's calibration.
    """

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def test_a_run_with_no_ratio_writes_no_baseline(self, tmp_path):
        assert run.save_baseline(
            tmp_path, None, self._provenance(tmp_path)) is False
        assert not run.baseline_file(tmp_path).exists()

    def test_a_run_with_a_ratio_writes_one(self, tmp_path):
        assert run.save_baseline(
            tmp_path, 0.42, self._provenance(tmp_path)) is True
        assert run.baseline_file(tmp_path).exists()

    def test_an_earlier_baseline_does_not_outlive_a_failed_calibration(
            self, tmp_path):
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        run.clear_baseline(tmp_path)

        assert not run.baseline_file(tmp_path).exists()


class TestAFailedLogMarkIsNotAnEmptyLog:
    """``log_mark`` answered a failed ``stat`` with ``LogMark(0, None)``.

    That value was byte-identical to the mark for a genuinely empty log,
    and ``main`` refuses before any mark unless the log file exists. So a
    transient sharing violation -- ordinary on Windows while
    ConcurrentRotatingFileHandler is rotating wheelhouse.log -- put the
    mark at zero, and if the failure cleared before the open that follows,
    the run read the whole pre-run history as its own measurement.
    """

    def _log_with_history(self, tmp_path):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('[load-diag] window=10.0s capture_available=yes '
                       'q_now=2 q_max=9 drops=0 overflow=0 status_flags=0 '
                       'stalls=0 max_gap_ms=120.0 max_frame_gap_ms=200.0\n',
                       encoding='utf-8')
        return log

    def test_a_mark_that_could_not_be_taken_is_no_mark(
            self, tmp_path, monkeypatch):
        log = self._log_with_history(tmp_path)
        real_stat = Path.stat

        def refuse(self, **kwargs):
            # Narrow to this one file: pytest itself stats paths while it
            # reports, and a stub that answers for all of them takes the
            # whole run down with an INTERNALERROR.
            if self == log:
                raise PermissionError('the file is being rotated')
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, 'stat', refuse)

        assert run.log_mark(log) is None

    def test_an_empty_log_that_could_be_read_still_marks_at_zero(
            self, tmp_path):
        """The failure and the ordinary empty file must stay separable."""
        log = tmp_path / 'wheelhouse.log'
        log.write_bytes(b'')

        mark = run.log_mark(log)

        assert mark is not None
        assert mark.size == 0

    def test_a_log_that_is_absent_is_not_a_log_that_was_refused(
            self, tmp_path, monkeypatch):
        """Only a refusal can be hiding bytes. A file nobody has created
        holds no history, and the log is briefly absent during an ordinary
        rotation, where the file that appears next is empty.
        """
        absent = tmp_path / 'never-written.log'
        assert run.log_mark(absent) == run.LogMark(size=0, identity=None)

        log = self._log_with_history(tmp_path)
        real_stat = Path.stat

        def refuse(self, **kwargs):
            if self == log:
                raise PermissionError('the file is being rotated')
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, 'stat', refuse)

        assert run.log_mark(log) is None

    def test_no_mark_reads_nothing_rather_than_the_whole_history(
            self, tmp_path):
        """The structural half of the fix: even a caller that ignores the
        failed mark cannot turn pre-run bytes into this run's data.

        The answer is now None rather than the empty pair, which is the
        same statement made louder -- a caller that ignores it unpacks
        None and fails at once, instead of measuring nothing quietly
        (finding wh-load-test-script.1.36).
        """
        log = self._log_with_history(tmp_path)

        assert run.read_since(log, None) is None

    def test_the_readiness_check_ignores_a_line_written_before_its_mark(
            self, tmp_path, monkeypatch):
        """A window line already in the log used to satisfy the restart
        check the moment the mark failed, so the run announced the flag
        live while the provider had never been restarted.
        """
        log = self._log_with_history(tmp_path)
        real_stat = Path.stat
        refusals = [True]

        def refuse_once(self, **kwargs):
            if refusals and self == log:
                refusals.pop()
                raise PermissionError('the file is being rotated')
            return real_stat(self, **kwargs)

        monkeypatch.setattr(Path, 'stat', refuse_once)
        monkeypatch.setattr(run.time, 'sleep', lambda _s: None)

        assert run.wait_for_window_line(log, 0.05) is False

    def test_the_readiness_check_takes_a_mark_once_the_refusal_clears(
            self, tmp_path, monkeypatch):
        """The retry has to end in a mark. A check that gave up on marking
        would sit reading nothing until it timed out, and report the same
        False whether or not the provider had restarted.
        """
        log = self._log_with_history(tmp_path)
        real_stat = Path.stat
        refusals = [True]

        def refuse_once(self, **kwargs):
            if refusals and self == log:
                refusals.pop()
                raise PermissionError('the file is being rotated')
            return real_stat(self, **kwargs)

        line = ('[load-diag] window=10.0s capture_available=yes q_now=2 '
                'q_max=9 drops=0 overflow=0 status_flags=0 stalls=0 '
                'max_gap_ms=120.0 max_frame_gap_ms=200.0\n')

        def append(_s):
            with open(log, 'a', encoding='utf-8') as handle:
                handle.write(line)

        monkeypatch.setattr(Path, 'stat', refuse_once)
        monkeypatch.setattr(run.time, 'sleep', append)

        assert run.wait_for_window_line(log, 30.0) is True

    def test_the_readiness_check_still_sees_a_line_written_after_the_mark(
            self, tmp_path, monkeypatch):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('nothing yet\n', encoding='utf-8')
        line = ('[load-diag] window=10.0s capture_available=yes q_now=2 '
                'q_max=9 drops=0 overflow=0 status_flags=0 stalls=0 '
                'max_gap_ms=120.0 max_frame_gap_ms=200.0\n')

        def append(_s):
            with open(log, 'a', encoding='utf-8') as handle:
                handle.write(line)

        monkeypatch.setattr(run.time, 'sleep', append)

        assert run.wait_for_window_line(log, 30.0) is True


class TestAFlagThatWasNeverWrittenIsNotAnnounced:
    """``restore_all`` printed the restart notice for a write that failed.

    ``apply`` claims the cleanup before it writes, which is what makes an
    interrupt after the write recoverable. When the write itself raised,
    the file still held the original, ``restore`` recognised that and set
    ``changed`` back to false, and ``restore_all`` read the pair
    (was changed, is not changed) as a completed restoration. It then told
    the operator the flag went back and the running application keeps the
    test value until a restart -- for LOG_TRANSCRIPTS, that dictated text
    keeps reaching the log -- about a setting that never turned on.
    """

    def _edit(self, tmp_path, key='log_load_diagnostics'):
        config = tmp_path / 'config.toml'
        config.write_bytes(f'{key} = false\n'.encode('utf-8'))
        return config, run.FlagEdit(config, key, True)

    def _fail_the_write(self, monkeypatch):
        def refuse(path, data):
            raise PermissionError('the config file is open elsewhere')

        monkeypatch.setattr(run, '_atomic_write', refuse)

    def test_a_write_that_failed_is_not_announced_as_a_restore(
            self, tmp_path, monkeypatch):
        config, edit = self._edit(tmp_path)
        self._fail_the_write(monkeypatch)
        with pytest.raises(PermissionError):
            edit.apply()
        assert config.read_bytes() == b'log_load_diagnostics = false\n'
        said = []

        monkeypatch.undo()
        run.restore_all([edit], out=said.append)

        assert said == []

    def test_the_dictated_text_warning_is_silent_when_nothing_turned_on(
            self, tmp_path, monkeypatch):
        config, edit = self._edit(tmp_path, key='LOG_TRANSCRIPTS')
        self._fail_the_write(monkeypatch)
        with pytest.raises(PermissionError):
            edit.apply()
        said = []

        monkeypatch.undo()
        run.restore_all([edit], out=said.append)

        assert not any('DICTATED TEXT' in line for line in said)

    def test_a_restore_that_wrote_says_it_wrote(self, tmp_path):
        config, edit = self._edit(tmp_path)
        edit.apply()

        assert edit.restore() is True
        assert config.read_bytes() == b'log_load_diagnostics = false\n'

    def test_a_restore_with_nothing_to_put_back_says_it_wrote_nothing(
            self, tmp_path, monkeypatch):
        _config, edit = self._edit(tmp_path)
        self._fail_the_write(monkeypatch)
        with pytest.raises(PermissionError):
            edit.apply()
        monkeypatch.undo()

        assert edit.restore() is False

    def test_a_real_restore_still_carries_both_notices(self, tmp_path):
        """The fix must not silence the notice the .1.7 fix added."""
        _config, edit = self._edit(tmp_path, key='LOG_TRANSCRIPTS')
        edit.apply()
        said = []

        run.restore_all([edit], out=said.append)

        assert any('until you restart it' in line for line in said)
        assert any('DICTATED TEXT' in line for line in said)


class TestAFailedBaselineCacheOperationFailsClosed:
    """A cache operation that raised left the previous calibration usable.

    ``_atomic_write`` replaces the target in one move, so a failed move
    left the older baseline.json in place with its provenance unchanged,
    and the next load run read that ratio as this machine's. The removal
    now happens FIRST: no baseline is a safe state, a stale one is not.
    """

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def test_a_clear_that_removed_the_file_says_so(self, tmp_path):
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        assert run.clear_baseline(tmp_path) is True

    def test_a_clear_of_a_baseline_that_is_not_there_says_so(self, tmp_path):
        assert run.clear_baseline(tmp_path) is True

    def test_a_clear_that_could_not_remove_the_file_says_so(
            self, tmp_path, monkeypatch):
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        target = run.baseline_file(tmp_path)
        real_unlink = Path.unlink

        def refuse(self, missing_ok=False):
            if self == target:
                raise PermissionError('the cache file is open elsewhere')
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, 'unlink', refuse)

        assert run.clear_baseline(tmp_path) is False

    def test_a_write_that_failed_leaves_no_earlier_calibration_behind(
            self, tmp_path, monkeypatch):
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        def refuse(path, data):
            raise PermissionError('the cache file is open elsewhere')

        monkeypatch.setattr(run, '_atomic_write', refuse)
        with pytest.raises(OSError):
            run.save_baseline(tmp_path, 0.42, self._provenance(tmp_path))

        monkeypatch.undo()
        assert not run.baseline_file(tmp_path).exists()
        assert run.load_baseline(tmp_path, self._provenance(tmp_path)) is None

    def test_a_removal_that_failed_refuses_instead_of_reporting_success(
            self, tmp_path, monkeypatch):
        """Nothing inside this tool can delete a file the filesystem
        refuses to delete. What it must not do is return as though the
        cache had been replaced.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        target = run.baseline_file(tmp_path)
        real_unlink = Path.unlink

        def refuse(self, missing_ok=False):
            if self == target:
                raise PermissionError('the cache file is open elsewhere')
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, 'unlink', refuse)

        with pytest.raises(OSError):
            run.save_baseline(tmp_path, 0.42, self._provenance(tmp_path))

    def test_a_run_with_no_ratio_still_removes_the_earlier_calibration(
            self, tmp_path):
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        assert run.save_baseline(
            tmp_path, None, self._provenance(tmp_path)) is False
        assert not run.baseline_file(tmp_path).exists()


class TestAnInterruptRemovingTheOldBaseline:
    """A Ctrl+C at the unlink used to leave the old calibration on disk.

    ``clear_baseline`` caught only ``OSError``, so an interrupt left it and
    ``save_baseline`` before either the old cache was removed or a
    replacement was written, and ``main`` caught only ``OSError`` too. The
    file stayed where it was with its provenance unchanged, so the next
    ordinary load run read it as this machine's calibration -- a number
    the interrupted run never finished measuring, feeding the verdict
    ladder in silence.
    """

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def _interrupt_the_unlink(self, tmp_path, monkeypatch, on):
        """Interrupt the cache file's own unlink on the named attempts.

        ``on`` counts attempts from one; every other path unlinks for real,
        so the temporary directory still cleans itself up.
        """
        target = run.baseline_file(tmp_path)
        real_unlink = Path.unlink
        attempts = []

        def interrupted(self, missing_ok=False):
            if self != target:
                return real_unlink(self, missing_ok=missing_ok)
            attempts.append(1)
            if len(attempts) in on:
                raise KeyboardInterrupt
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, 'unlink', interrupted)
        return attempts

    def test_an_interrupted_removal_is_finished_before_it_is_raised(
            self, tmp_path, monkeypatch):
        """The whole point. The operator pressed Ctrl+C, the run stops --
        and the old baseline is gone, so the next load run measures
        against nothing rather than against a number nobody finished.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))
        self._interrupt_the_unlink(tmp_path, monkeypatch, on={1})

        with pytest.raises(KeyboardInterrupt):
            run.save_baseline(tmp_path, 0.42, self._provenance(tmp_path))

        monkeypatch.undo()
        assert not run.baseline_file(tmp_path).exists()
        assert run.load_baseline(tmp_path, self._provenance(tmp_path)) is None

    def test_a_removal_no_attempt_finished_is_never_left_unsaid(
            self, tmp_path, monkeypatch):
        """The case the tool cannot repair. The file is still there with
        provenance the next load run matches, so the operator is told to
        delete it themselves, in the words main already uses when the
        removal fails.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))
        self._interrupt_the_unlink(tmp_path, monkeypatch, on={1, 2})
        said = []

        with pytest.raises(KeyboardInterrupt):
            run.clear_baseline(tmp_path, out=said.append)

        told = '\n'.join(said)
        assert str(run.baseline_file(tmp_path)) in told
        # The words as well as the constant. Asserting only that the
        # constant appears would let the constant itself be emptied.
        assert run.DELETE_THE_BASELINE_YOURSELF in told
        assert 'Delete that file yourself' in told
        assert 'reuse a calibration this run did not make' in told

    def test_the_removal_is_attempted_twice_and_no_more(
            self, tmp_path, monkeypatch):
        """An operator holding Ctrl+C down must not put the tool in a loop
        it cannot leave.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))
        attempts = self._interrupt_the_unlink(
            tmp_path, monkeypatch, on={1, 2, 3, 4, 5})

        with pytest.raises(KeyboardInterrupt):
            run.clear_baseline(tmp_path, out=lambda _m: None)

        assert len(attempts) == 2

    def test_a_removal_that_finished_says_nothing_about_deleting_it(
            self, tmp_path, monkeypatch):
        """The honest half. Telling an operator to delete a file that is
        already gone is the same defect in the other direction: it sends
        them looking for something that is not there and teaches them to
        ignore the line that matters.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))
        self._interrupt_the_unlink(tmp_path, monkeypatch, on={1})
        said = []

        with pytest.raises(KeyboardInterrupt):
            run.clear_baseline(tmp_path, out=said.append)

        assert said == []

    def test_an_ordinary_removal_raises_nothing(self, tmp_path):
        """The guard this fix must not weaken."""
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        assert run.clear_baseline(tmp_path, out=lambda _m: None) is True


class TestAnInterruptBeforeTheOldBaselineIsRemoved:
    """A Ctrl+C in the directory setup used to leave the old calibration.

    ``save_baseline`` opened with ``cache_dir.mkdir`` and reached
    ``clear_baseline`` only on the next line. An interrupt delivered while
    that setup ran left ``save_baseline`` before any invalidation started,
    so the old baseline stayed on disk with its provenance unchanged and
    the next ordinary load run read it as this machine's calibration.
    ``main`` catches only ``OSError`` at that call site, so it put the
    config back and said nothing about the cache. That is the same silent
    reuse the invalidation exists to prevent, reached one line earlier
    (finding wh-load-test-script.1.29).

    The removal needs no directory: ``unlink(missing_ok=True)`` on a path
    whose parent does not exist raises nothing. So the invalidation goes
    first and the directory is made for the write that needs it.
    """

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def test_an_interrupt_making_the_directory_leaves_no_old_baseline(
            self, tmp_path, monkeypatch):
        """The finding itself. The operator pressed Ctrl+C while the cache
        directory was being set up; the old calibration must still be gone,
        so the next load run measures against nothing rather than against a
        number this run never finished measuring.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        real_mkdir = Path.mkdir
        attempts = []

        def interrupted(self, *args, **kwargs):
            if self != tmp_path:
                return real_mkdir(self, *args, **kwargs)
            attempts.append(1)
            raise KeyboardInterrupt

        monkeypatch.setattr(Path, 'mkdir', interrupted)

        with pytest.raises(KeyboardInterrupt):
            run.save_baseline(tmp_path, 0.42, self._provenance(tmp_path))

        monkeypatch.undo()
        # One interrupt reached the caller, and the directory setup was not
        # retried around it: the operator gets the stop they asked for.
        assert attempts == [1]
        assert not run.baseline_file(tmp_path).exists()
        assert run.load_baseline(tmp_path, self._provenance(tmp_path)) is None

    def test_the_old_baseline_goes_before_the_directory_is_made(
            self, tmp_path, monkeypatch):
        """The ordering the fix rests on, asserted directly. Every
        statement that runs before the invalidation is another window in
        which a Ctrl+C leaves the old calibration behind, so there are to
        be none.
        """
        run.save_baseline(tmp_path, 0.70, self._provenance(tmp_path))

        target = run.baseline_file(tmp_path)
        real_mkdir = Path.mkdir
        real_unlink = Path.unlink
        order = []

        def note_mkdir(self, *args, **kwargs):
            if self == tmp_path:
                order.append('directory')
            return real_mkdir(self, *args, **kwargs)

        def note_unlink(self, missing_ok=False):
            if self == target:
                order.append('invalidation')
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, 'mkdir', note_mkdir)
        monkeypatch.setattr(Path, 'unlink', note_unlink)

        assert run.save_baseline(
            tmp_path, 0.42, self._provenance(tmp_path)) is True

        assert order == ['invalidation', 'directory']

    def test_the_invalidation_needs_no_cache_directory(self, tmp_path):
        """The reason the invalidation may go first. It removes a file,
        and removing a file that is not there is not an error even when
        the directory holding it was never made. The write that follows
        still gets its directory.
        """
        cache = tmp_path / 'not' / 'made' / 'yet'
        assert not cache.exists()

        assert run.clear_baseline(cache) is True
        assert run.save_baseline(
            cache, None, self._provenance(tmp_path)) is False
        assert run.save_baseline(
            cache, 0.42, self._provenance(tmp_path)) is True
        assert run.load_baseline(
            cache, self._provenance(tmp_path)) == 0.42


def _all_redacted():
    """Six transcripts of the right size whose words never reached the log."""
    return judge.judge_sentences(
        [_said(i, _redacted(script.expected_transcript(_spoken(i))))
         for i in range(1, 7)])


class TestARedactedTranscriptIsNeverCalledCorrect:
    """The late list said "correct" about words nobody ever compared.

    ``_late_sentences`` skipped only WRONG and MISSING, so a redacted
    sentence of the expected size landed under "Sentences correct but
    late:". Under the transcript-privacy default that is EVERY sentence,
    so the field an operator reads first contradicted the rule the whole
    tool is built on: equal counts are a length match, never correct.
    """

    def _late(self, sentences):
        return _rendered(
            sentences=sentences,
            parsed=logparse.ParsedLog(
                utterances=[_utterance(index=i, ratio=2.4)
                            for i in range(1, 7)],
                windows=[_window()]))

    def _field(self, rendered, label):
        return rendered.split(label)[1].splitlines()[0]

    def test_a_redacted_late_sentence_is_not_called_correct(self):
        rendered = self._late(_all_redacted())

        assert self._field(
            rendered, 'Sentences correct but late:').strip() == '0'

    def test_a_redacted_late_sentence_is_reported_under_its_own_field(self):
        rendered = self._late(_all_redacted())

        field = self._field(rendered, 'Sentences of the right length but late:')
        assert '6' in field

    def test_a_visible_late_sentence_is_still_called_correct(self):
        rendered = self._late(_all_correct())

        assert '6' in self._field(rendered, 'Sentences correct but late:')
        assert self._field(
            rendered,
            'Sentences of the right length but late:').strip() == '0'

    def test_a_length_match_is_never_counted_in_both_fields(self):
        rendered = self._late(_all_redacted())
        correct = self._field(rendered, 'Sentences correct but late:')

        assert 'sentence' not in correct


class TestANonFiniteCounterReadingIsNoMeasurement:
    """A reading that is not a number must not pass for a measurement.

    ``float()`` accepts "NaN", "Infinity" and "-Infinity", and every
    comparison against NaN is false. The two gates in ``main`` reject only
    ``None`` before comparing, so a NaN reading passed both of them: a
    loaded run treated saturation as confirmed without a measurement, and a
    ``--baseline`` run called the same machine idle and saved a calibration
    from it. The infinities each defeat one of the two gates. Reject the
    whole class here, at the one place the text becomes a number, because
    ``None`` already means "no measurement" to both callers.
    """

    def _reading(self, monkeypatch, text, returncode=0):
        class _Finished:
            def __init__(self):
                self.returncode = returncode
                self.stdout = text
                self.stderr = ''

        monkeypatch.setattr(run.subprocess, 'run',
                            lambda *a, **k: _Finished())
        return run.measure_busy_percent(samples=1)

    def test_a_not_a_number_reading_is_refused(self, monkeypatch):
        assert self._reading(monkeypatch, 'NaN\n') is None

    def test_an_infinite_reading_is_refused(self, monkeypatch):
        assert self._reading(monkeypatch, 'Infinity\n') is None

    def test_a_negatively_infinite_reading_is_refused(self, monkeypatch):
        assert self._reading(monkeypatch, '-Infinity\n') is None

    def test_the_python_spellings_of_the_same_values_are_refused(
            self, monkeypatch):
        """PowerShell writes "NaN" and "Infinity"; float() also takes
        "nan", "inf" and "-inf". The guard asks whether the number is
        finite, so no spelling of the same value can get past it.
        """
        for text in ('nan', 'inf', '-inf', '  NAN  '):
            assert self._reading(monkeypatch, text + '\n') is None

    def test_a_real_reading_is_still_returned(self, monkeypatch):
        """The keep-working half. A guard that refused everything would
        pass every test above and stop the tool from running at all.
        """
        assert self._reading(monkeypatch, '97.5\n') == 97.5

    def test_a_zero_reading_is_still_returned(self, monkeypatch):
        """An idle machine reads near zero, and zero is what a
        ``--baseline`` run wants. A guard written as a truth test rather
        than a finiteness test would throw this reading away.
        """
        assert self._reading(monkeypatch, '0.0\n') == 0.0


class TestANegativeCounterReadingIsNoMeasurement:
    """A machine cannot be less than nothing busy, and -5% is not idle.

    A negative reading is finite, so the isfinite guard lets it through,
    and the two gates then disagree about it: the loaded run refuses it
    (-5 < 90 is true) while a --baseline run accepts it as idle (-5 > 30
    is false) and can save a calibration from a machine whose load was
    never measured. That is the worst outcome this tool has, because a
    bad calibration is kept and silently reused by later runs.

    The upper end is deliberately left open (wh-load-test-script.1.23,
    boss ruling of 2026-08-30, option (a)). Windows processor counters
    can read slightly over 100 on a healthy saturated machine from timing
    skew, so refusing above 100 would risk a false refusal in exactly the
    case this tool exists for, while accepting a high reading only makes
    the loaded run conclude "saturated" -- which is close to true whenever
    a real machine reads high.
    """

    def _reading(self, monkeypatch, text):
        class _Finished:
            returncode = 0
            stdout = text
            stderr = ''

        monkeypatch.setattr(run.subprocess, 'run',
                            lambda *a, **k: _Finished())
        return run.measure_busy_percent(samples=1)

    def test_a_negative_reading_is_refused(self, monkeypatch):
        assert self._reading(monkeypatch, '-5.0\n') is None

    def test_a_barely_negative_reading_is_refused(self, monkeypatch):
        """The guard asks the sign, not the size. A reading that is
        negative by a hair is still not a measurement.
        """
        assert self._reading(monkeypatch, '-0.0001\n') is None

    def test_an_idle_reading_of_zero_is_accepted(self, monkeypatch):
        """The boundary, and the reading a --baseline run wants most.
        A guard written as <= 0 would refuse every genuinely idle
        machine and no baseline could ever be taken.
        """
        assert self._reading(monkeypatch, '0.0\n') == 0.0

    def test_a_reading_above_one_hundred_is_still_accepted(self, monkeypatch):
        """The recorded trade-off, pinned so a later change cannot close
        the upper end without reading the reasoning first.
        """
        assert self._reading(monkeypatch, '150.0\n') == 150.0


class TestASecondInterruptStillPutsTheGateSourceBack:
    """Ctrl+C twice must not leave a mutant in a real source file.

    The first interrupt lands while pytest runs and reaches the cleanup.
    The second lands inside the restore, which caught only OSError, so it
    escaped and the file kept the mutant. Every later run -- and every
    other session sharing this checkout -- then reads the mutant as the
    real code, which is the worst outcome this gate has.

    This is the same defect wh-load-test-script.1.21 fixed for the run
    tool's flag restore, in the one source-mutation cleanup path that
    still lacked the protection (finding wh-load-test-script.1.24).
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _target(self, tmp_path, monkeypatch, cleared=None):
        """A throwaway source file, with pytest and the cache stubbed.

        The interrupt comes from the stubbed ``_run_pytest``, which is
        where a real Ctrl+C arrives: the operator presses it while the
        suite is running.
        """
        gate = self._gate()
        target = tmp_path / 'source.py'
        target.write_bytes(b'value = 1\n')

        def clear(service):
            if cleared is not None:
                cleared.append(service)
            # The real one returns (error, deferred interrupt) since
            # wh-load-test-script.1.26; a clean walk reports neither.
            return None, None

        monkeypatch.setattr(gate, '_clear_pycache', clear)

        def interrupted(service, test_file, mutant=False):
            raise KeyboardInterrupt('first Ctrl+C, while the suite ran')

        monkeypatch.setattr(gate, '_run_pytest', interrupted)
        return gate, target, b'value = 1\n', b'value = 2\n'

    def _interrupt_moves(self, gate, monkeypatch, which):
        """Raise KeyboardInterrupt on the named os.replace calls.

        Call 1 is the mutant write. Call 2 onwards are restore attempts.
        """
        real = gate.os.replace
        calls = []

        def move(src, dst):
            calls.append(dst)
            if len(calls) in which:
                raise KeyboardInterrupt('Ctrl+C again, during the restore')
            return real(src, dst)

        monkeypatch.setattr(gate.os, 'replace', move)
        return calls

    def test_a_second_interrupt_during_the_restore_still_restores(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original

    def test_the_second_interrupt_still_reaches_the_operator(
            self, tmp_path, monkeypatch):
        """Restoring the file must not swallow the Ctrl+C. An operator who
        interrupts a run and is not stopped presses it again, which is how
        this defect was reached in the first place.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

    def test_a_second_interrupt_does_not_skip_the_bytecode_clearing(
            self, tmp_path, monkeypatch):
        """The restore and the cache are one cleanup, not two.

        Python decides whether cached bytecode is current from the source
        file's timestamp and size, and a mutation can keep both. Letting
        the interrupt out before _clear_pycache would put the source back
        and leave the mutant's bytecode beside it, so the next run could
        execute the mutation the file no longer contains.
        """
        cleared = []
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, cleared=cleared)
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert cleared == [gate.SHARED]

    def test_an_interrupt_on_every_attempt_says_the_mutant_may_remain(
            self, tmp_path, monkeypatch, capsys):
        """The retry is bounded at two. An operator holding Ctrl+C down
        must not put the gate in a loop it cannot leave, so the second
        failure reports instead of trying again.

        The report has to be PRINTED. Raising the interrupt skips the
        sweep loop that normally prints restore errors, so the returned
        error string never reaches anybody. A mutant left in a real source
        file that nobody is told about is the failure this whole class
        exists to prevent.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2, 3, 4, 5})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == mutant
        said = capsys.readouterr().out
        assert 'MUTANT MAY REMAIN' in said
        assert str(target) in said

    def test_a_restore_that_worked_says_nothing_about_a_mutant(
            self, tmp_path, monkeypatch, capsys):
        """The other direction. A run that put the file back must not
        print a warning an operator would go and act on.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert 'MUTANT' not in capsys.readouterr().out

    def test_one_interrupt_alone_still_restores_and_still_raises(
            self, tmp_path, monkeypatch):
        """The keep-working guard. Nothing interrupts the restore here,
        so the file goes back and the original Ctrl+C still stops the run.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original

    def test_an_interrupted_restore_leaves_no_temporary_file_behind(
            self, tmp_path, monkeypatch):
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert [p.name for p in tmp_path.iterdir()] == ['source.py']

    def test_an_interrupt_during_the_restore_alone_still_stops_the_run(
            self, tmp_path, monkeypatch):
        """The one interrupt arrives during the cleanup, not the suite.

        An operator pressing Ctrl+C in the gap between one mutation's
        pytest finishing and the next starting lands here. No exception is
        in flight, so nothing carries the interrupt out of the cleanup on
        its own: re-raising it is the only thing that stops the sweep.
        Without that, the run swallows the Ctrl+C and keeps going for
        hours.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda service, test_file, mutant=False:
            subprocess.CompletedProcess([], 0, '', ''))
        self._interrupt_moves(gate, monkeypatch, {2})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original

    def test_a_second_interrupt_reading_the_file_still_restores(
            self, tmp_path, monkeypatch):
        """The restore's other seam. It reads the file before deciding
        what to do, and an interrupt there leaves it having decided
        nothing -- with the mutant still on disk.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        real_read = Path.read_bytes
        reads = []

        def read(self):
            reads.append(self)
            if len(reads) == 1 and str(self) == str(target):
                raise KeyboardInterrupt('Ctrl+C again, reading the file')
            return real_read(self)

        monkeypatch.setattr(Path, 'read_bytes', read)

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        monkeypatch.undo()
        assert target.read_bytes() == original


def _utt_line(ratio: str, drops: int = 0) -> str:
    """A complete [load-diag] utterance line carrying ``ratio``.

    Nothing about the line is truncated or reordered: the whole point of
    the class below is a line the parser can read end to end, whose
    engine_ratio is still not a measurement.
    """
    return (PREFIX + '[load-diag] utt=3 kind=endpoint overflow=0 '
            f'status_flags=0 drops={drops} q_max=333 q_mean=41.2 q_n=95 '
            'engine_calls=12 engine_ms_total=8100.0 engine_ms_max=980.0 '
            f'audio_ms=6000 engine_ratio={ratio}')


class TestANonFiniteEngineRatioIsNoMeasurement:
    """A ratio that is not a number must not reach the verdict ladder.

    ``float()`` accepts "nan", "inf" and "-inf", so every one of those
    spellings passed the parser's own ``(KeyError, ValueError)`` guard,
    whose comment promises that a half-read measurement is worse than a
    missing one. The value then reached ``judge_run`` unchanged, where
    every comparison against NaN is false: ``worst_ratio > 1.0`` does not
    fire, ``worst_ratio > baseline * 1.5`` does not fire, and the ladder
    falls through to ``neither`` with evidence claiming the NaN ratio is
    within 1.5 times the idle reading. Positive infinity makes a later
    finite ratio look inside the margin once it has been saved as a
    baseline; negative infinity drives the threshold the other way.

    The ingress is fixed, not the ladder. ``None`` -- no per-utterance
    line carried an engine_ratio -- already means "unmeasured" all the way
    up, and the ladder already answers ``undetermined`` for it (finding
    wh-load-test-script.1.25).
    """

    def test_a_not_a_number_ratio_is_not_read_as_a_measurement(self):
        assert logparse.parse_log(_utt_line('nan')).utterances == []

    def test_an_infinite_ratio_is_not_read_as_a_measurement(self):
        assert logparse.parse_log(_utt_line('inf')).utterances == []

    def test_a_negatively_infinite_ratio_is_not_read_as_a_measurement(self):
        assert logparse.parse_log(_utt_line('-inf')).utterances == []

    def test_every_spelling_of_those_values_is_refused(self):
        """``float()`` takes several spellings of each. The guard asks
        whether the number is finite, so no spelling gets past it.
        """
        for text in ('NaN', 'NAN', 'Infinity', '-Infinity', 'INF', '+inf'):
            assert logparse.parse_log(_utt_line(text)).utterances == [], text

    def test_a_finite_ratio_is_still_read(self):
        """The keep-working half. A guard that refused every ratio would
        pass every test above and leave the tool measuring nothing.
        """
        parsed = logparse.parse_log(_utt_line('1.35'))
        assert [u.engine_ratio for u in parsed.utterances] == [1.35]

    def test_a_zero_ratio_is_still_read(self):
        """Zero is a real reading -- an utterance whose engine time was
        below the timer's resolution. A guard written as a truth test
        rather than a finiteness test would throw it away.
        """
        parsed = logparse.parse_log(_utt_line('0.0'))
        assert [u.engine_ratio for u in parsed.utterances] == [0.0]

    def test_a_not_a_number_ratio_cannot_earn_a_neither_verdict(self):
        """The whole reason the parser has to refuse it. A NaN ratio,
        clean counters, a bad transcript and a finite baseline used to
        return ``neither``.
        """
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('nan')]))
        verdict = judge.judge_run(parsed, _one_missing(), 0.40)
        assert verdict.verdict == judge.UNDETERMINED

    def test_an_infinite_ratio_cannot_earn_a_confident_verdict(self):
        """Positive infinity clears 1.0, so the same run reported
        ``inference lag`` -- a named failure with a cause and a remedy,
        read off a value that is not a measurement.
        """
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('inf')]))
        verdict = judge.judge_run(parsed, _one_missing(), 0.40)
        assert verdict.verdict == judge.UNDETERMINED

    def test_a_negatively_infinite_ratio_cannot_earn_a_neither_verdict(self):
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('-inf')]))
        verdict = judge.judge_run(parsed, _one_missing(), 0.40)
        assert verdict.verdict == judge.UNDETERMINED

    def test_the_evidence_never_calls_a_non_finite_ratio_idle(self):
        """The evidence line is what an operator pastes into the record.
        Saying a NaN ratio is within 1.5 times the idle reading is the
        confident-and-wrong sentence this finding is about.
        """
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('nan')]))
        verdict = judge.judge_run(parsed, _one_missing(), 0.40)
        joined = ' '.join(verdict.evidence)
        assert 'nan' not in joined.lower()
        assert 'idle reading' not in joined

    def test_a_finite_ratio_still_earns_its_neither_verdict(self):
        """The keep-working half at the verdict level: the same shape of
        run with a real ratio must still reach ``neither``.
        """
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('0.45')]))
        verdict = judge.judge_run(parsed, _one_missing(), 0.40)
        assert verdict.verdict == judge.NEITHER


class TestANonFiniteBaselineIsNeverWrittenOrReused:
    """The baseline cache is the other ingress that changes a verdict.

    ``json.dumps`` writes NaN and Infinity by default and ``json.loads``
    reads them back, so one unusable log line became a persisted
    calibration that every later run with matching provenance reused. The
    reader also accepted ``True``, because ``bool`` is a subclass of
    ``int`` and ``float(True)`` is 1.0 -- a calibration nobody measured.

    No baseline is a safe, handled state here: the run says it has none
    and the operator calibrates (ruling of 2026-08-30 on
    wh-load-test-script.1.17). A poisoned one is not.
    """

    def _provenance(self, tmp_path):
        return run.baseline_provenance(
            run.Paths(repo_root=tmp_path, sherpa_config=tmp_path / 's.toml',
                      app_config=tmp_path / 'a.toml',
                      log_file=tmp_path / 'w.log'),
            'sherpa', 800.0)

    def _on_disk(self, tmp_path, ratio_text):
        """A baseline file holding ``ratio_text`` verbatim.

        Written as text rather than through ``save_baseline``, because
        the writer refuses these values and this is what an already
        poisoned cache looks like on disk.
        """
        provenance = self._provenance(tmp_path)
        body = json.dumps(provenance)
        run.baseline_file(tmp_path).write_text(
            '{"engine_ratio": ' + ratio_text + ', ' + body[1:],
            encoding='utf-8')
        return provenance

    def test_a_not_a_number_ratio_is_not_persisted(self, tmp_path):
        assert run.save_baseline(
            tmp_path, float('nan'), self._provenance(tmp_path)) is False
        assert not run.baseline_file(tmp_path).exists()

    def test_an_infinite_ratio_is_not_persisted(self, tmp_path):
        assert run.save_baseline(
            tmp_path, float('inf'), self._provenance(tmp_path)) is False
        assert not run.baseline_file(tmp_path).exists()

    def test_a_negatively_infinite_ratio_is_not_persisted(self, tmp_path):
        assert run.save_baseline(
            tmp_path, float('-inf'), self._provenance(tmp_path)) is False
        assert not run.baseline_file(tmp_path).exists()

    def test_an_earlier_baseline_does_not_outlive_a_refused_one(
            self, tmp_path):
        """The refusal follows the same rule as every other one here: the
        old file is removed FIRST, so a run that refuses to write leaves
        no calibration behind for the next run to read as this machine's.
        """
        provenance = self._provenance(tmp_path)
        run.save_baseline(tmp_path, 0.42, provenance)

        assert run.save_baseline(
            tmp_path, float('nan'), provenance) is False
        assert not run.baseline_file(tmp_path).exists()

    def test_a_not_a_number_baseline_on_disk_is_ignored(self, tmp_path):
        provenance = self._on_disk(tmp_path, 'NaN')
        said = []

        assert run.load_baseline(
            tmp_path, provenance, out=said.append) is None
        assert said

    def test_an_infinite_baseline_on_disk_is_ignored(self, tmp_path):
        provenance = self._on_disk(tmp_path, 'Infinity')
        assert run.load_baseline(tmp_path, provenance,
                                 out=lambda _: None) is None

    def test_a_negatively_infinite_baseline_on_disk_is_ignored(
            self, tmp_path):
        provenance = self._on_disk(tmp_path, '-Infinity')
        assert run.load_baseline(tmp_path, provenance,
                                 out=lambda _: None) is None

    def test_a_true_baseline_is_not_a_ratio(self, tmp_path):
        """``isinstance(True, (int, float))`` is True and ``float(True)``
        is 1.0, so a JSON ``true`` used to become a calibration of 1.0.
        """
        provenance = self._on_disk(tmp_path, 'true')
        assert run.load_baseline(tmp_path, provenance,
                                 out=lambda _: None) is None

    def test_a_false_baseline_is_not_a_ratio(self, tmp_path):
        """The other spelling, which reads as the most idle machine
        possible and would make every later run look raised.
        """
        provenance = self._on_disk(tmp_path, 'false')
        assert run.load_baseline(tmp_path, provenance,
                                 out=lambda _: None) is None

    def test_a_poisoned_baseline_cannot_earn_a_neither_verdict(
            self, tmp_path):
        """End to end: the run that reads the poisoned file must not
        reach ``neither`` on it.
        """
        provenance = self._on_disk(tmp_path, 'NaN')
        baseline = run.load_baseline(tmp_path, provenance,
                                     out=lambda _: None)
        parsed = logparse.parse_log(
            '\n'.join([WINDOW_LINE, _utt_line('0.45')]))

        verdict = judge.judge_run(parsed, _one_missing(), baseline)

        assert verdict.verdict == judge.UNDETERMINED

    def test_an_ordinary_baseline_is_still_written_and_read(self, tmp_path):
        """The keep-working half. A guard that refused every ratio would
        pass every test above and no calibration could ever be taken.
        """
        provenance = self._provenance(tmp_path)
        assert run.save_baseline(tmp_path, 0.42, provenance) is True
        assert run.load_baseline(tmp_path, provenance) == 0.42

    def test_a_zero_baseline_is_still_written_and_read(self, tmp_path):
        """An idle machine can read zero, and a guard written as a truth
        test rather than a finiteness test would throw it away.
        """
        provenance = self._provenance(tmp_path)
        assert run.save_baseline(tmp_path, 0.0, provenance) is True
        assert run.load_baseline(tmp_path, provenance) == 0.0

    def test_an_integer_baseline_is_still_read(self, tmp_path):
        """JSON writes 1 for a whole number, and the reader has always
        accepted an int. Rejecting bool must not reject that.
        """
        provenance = self._on_disk(tmp_path, '1')
        assert run.load_baseline(tmp_path, provenance) == 1.0


class TestAnInterruptClearingTheCacheStillFinishesTheCleanup:
    """Ctrl+C during the bytecode clearing must not hide the mutant warning.

    ``_mutate_and_run`` restores the source, clears SHARED/__pycache__ and
    only then prints a pending restore error and re-raises. That ordering
    is right, but the clearing itself was unprotected: ``Path.rglob`` and
    ``shutil.rmtree`` both let a KeyboardInterrupt straight out of the
    finally. The interrupt then replaced the pending return, so neither
    the sweep loop nor the local ``if restore_error`` branch ran, and an
    operator whose source file may still hold a mutant got a traceback and
    no warning.

    The same interrupt could also stop the clearing part way through,
    which is the state the ordering exists to prevent: a source mutation
    can keep the timestamp and the size Python uses to decide a cached
    .pyc is current, so a mutant .pyc left beside a restored source is
    executed by the next process (finding wh-load-test-script.1.26).
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _target(self, tmp_path, monkeypatch, suite_interrupts=True):
        """A throwaway checkout: one source file and one __pycache__.

        SHARED is pointed at it so the real ``_clear_pycache`` walks this
        tree and nothing else. The cache directory is what the walk has
        to remove.
        """
        gate = self._gate()
        monkeypatch.setattr(gate, 'SHARED', tmp_path)
        target = tmp_path / 'source.py'
        target.write_bytes(b'value = 1\n')
        cache = tmp_path / 'pkg' / '__pycache__'
        cache.mkdir(parents=True)
        (cache / 'source.cpython-313.pyc').write_bytes(b'\x00')

        if suite_interrupts:
            def run_pytest(service, test_file, mutant=False):
                raise KeyboardInterrupt('first Ctrl+C, while the suite ran')
        else:
            def run_pytest(service, test_file, mutant=False):
                return subprocess.CompletedProcess([], 0, '', '')

        monkeypatch.setattr(gate, '_run_pytest', run_pytest)
        return gate, target, b'value = 1\n', b'value = 2\n'

    def _interrupt_removals(self, gate, monkeypatch, which):
        """Raise KeyboardInterrupt on the named ``shutil.rmtree`` calls."""
        real = gate.shutil.rmtree
        calls = []

        def rmtree(path, *args, **kwargs):
            calls.append(path)
            if len(calls) in which:
                raise KeyboardInterrupt('Ctrl+C again, clearing the cache')
            return real(path, *args, **kwargs)

        monkeypatch.setattr(gate.shutil, 'rmtree', rmtree)
        return calls

    def _interrupt_moves(self, gate, monkeypatch, which):
        """Raise KeyboardInterrupt on the named ``os.replace`` calls.

        Call 1 is the mutant write. Call 2 onwards are restore attempts.
        """
        real = gate.os.replace
        calls = []

        def move(src, dst):
            calls.append(dst)
            if len(calls) in which:
                raise KeyboardInterrupt('Ctrl+C again, during the restore')
            return real(src, dst)

        monkeypatch.setattr(gate.os, 'replace', move)
        return calls

    def _caches(self, tmp_path):
        return [p for p in tmp_path.rglob('__pycache__')]

    def test_a_pending_restore_warning_survives_an_interrupt_in_the_walk(
            self, tmp_path, monkeypatch, capsys):
        """The defect itself. The restore could not prove it put the file
        back, and the interrupt that arrived during the cache walk carried
        that warning away with it.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2, 3, 4, 5})
        self._interrupt_removals(gate, monkeypatch, {1})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        said = capsys.readouterr().out
        assert 'MUTANT MAY REMAIN' in said
        assert str(target) in said

    def test_the_bytecode_is_still_cleared_when_the_walk_is_interrupted(
            self, tmp_path, monkeypatch):
        """The companion case: the restore worked. An interrupt part way
        through the walk still must not leave the mutant's bytecode beside
        the restored source, because a mutation can keep the timestamp and
        size Python checks before reusing a .pyc.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_removals(gate, monkeypatch, {1})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original
        assert self._caches(tmp_path) == []

    def test_an_interrupt_in_the_walk_alone_still_stops_the_sweep(
            self, tmp_path, monkeypatch):
        """The one interrupt arrives during the clearing, not the suite.

        Nothing else is in flight, so holding it and never re-raising it
        would swallow the operator's Ctrl+C and keep the sweep running for
        hours.
        """
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, suite_interrupts=False)
        self._interrupt_removals(gate, monkeypatch, {1})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert self._caches(tmp_path) == []

    def test_an_interrupt_on_every_pass_is_reported_and_bounded(
            self, tmp_path, monkeypatch, capsys):
        """An operator holding Ctrl+C down must not put the gate in a loop
        it cannot leave, so the walk gets two passes and the second failure
        reports instead of trying again.

        The report has to be PRINTED for the same reason the restore's is:
        raising skips the sweep loop that would otherwise print it.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        calls = self._interrupt_removals(gate, monkeypatch, {1, 2, 3, 4, 5})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert len(calls) == 2
        said = capsys.readouterr().out
        assert 'MUTANT .pyc MAY REMAIN' in said
        assert 'probe' in said

    def test_an_interrupt_in_the_walk_alone_on_every_pass_is_reported(
            self, tmp_path, monkeypatch, capsys):
        """The same held-down Ctrl+C, with nothing else in flight.

        The suite finished normally, so the walk's own held interrupt is
        the only thing that can carry the warning out. A finally that
        drops it prints nothing, raises nothing, and returns as if the
        run were clean. The test above cannot see that: its suite
        interrupt is in flight, and the print guard's ``failure`` arm
        prints the warning on its own.
        """
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, suite_interrupts=False)
        calls = self._interrupt_removals(gate, monkeypatch, {1, 2, 3, 4, 5})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert len(calls) == 2
        said = capsys.readouterr().out
        assert 'MUTANT .pyc MAY REMAIN' in said
        assert 'probe' in said

    def test_both_halves_of_a_failed_cleanup_reach_the_operator(
            self, tmp_path, monkeypatch, capsys):
        """A mutant left in a source file and a mutant .pyc left beside it
        are separate repairs, so a run that could do neither has to say so
        twice.
        """
        gate, target, original, mutant = self._target(tmp_path, monkeypatch)
        self._interrupt_moves(gate, monkeypatch, {2, 3, 4, 5})
        self._interrupt_removals(gate, monkeypatch, {1, 2, 3, 4, 5})

        with pytest.raises(KeyboardInterrupt):
            gate._mutate_and_run(target, original, mutant, 'probe')

        said = capsys.readouterr().out
        assert 'MUTANT MAY REMAIN' in said
        assert 'MUTANT .pyc MAY REMAIN' in said

    def test_a_cleanup_that_worked_says_nothing_and_clears_the_cache(
            self, tmp_path, monkeypatch, capsys):
        """The keep-working half. Nothing is interrupted, so the run ends
        normally, the cache is gone and no warning an operator would act on
        is printed.
        """
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, suite_interrupts=False)

        _, timed_out, restore_error = gate._mutate_and_run(
            target, original, mutant, 'probe')

        assert restore_error is None
        assert timed_out is False
        assert target.read_bytes() == original
        assert self._caches(tmp_path) == []
        assert 'MUTANT' not in capsys.readouterr().out

    def test_a_clean_walk_reports_nothing_and_holds_nothing(self, tmp_path):
        """The clearing's own contract, on the ordinary path."""
        gate = self._gate()
        cache = tmp_path / 'pkg' / '__pycache__'
        cache.mkdir(parents=True)

        assert gate._clear_pycache(tmp_path) == (None, None)
        assert list(tmp_path.rglob('__pycache__')) == []

    def test_the_walk_leaves_the_virtual_environment_alone(self, tmp_path):
        """The venv holds thousands of cached files this gate never
        mutates, and deleting them would rebuild the environment on every
        pass. The guard that skips them has to survive the retry.
        """
        gate = self._gate()
        inside = tmp_path / '.venv' / 'Lib' / '__pycache__'
        inside.mkdir(parents=True)

        assert gate._clear_pycache(tmp_path) == (None, None)
        assert inside.exists()

    def test_a_pre_run_clearing_stops_the_sweep_and_says_why(
            self, tmp_path, monkeypatch, capsys):
        """The two calls that happen before any restore result exists. No
        warning is pending there, so the interrupt ends the sweep -- but
        only after the clearing has finished or reported that it could not.
        """
        gate = self._gate()
        (tmp_path / 'pkg' / '__pycache__').mkdir(parents=True)
        self._interrupt_removals(gate, monkeypatch, {1, 2, 3})

        with pytest.raises(KeyboardInterrupt):
            gate._clear_pycache_or_stop(tmp_path)

        assert 'MUTANT .pyc MAY REMAIN' in capsys.readouterr().out

    def test_a_pre_run_clearing_that_worked_stops_nothing(
            self, tmp_path, capsys):
        """The keep-working half of the same call: an ordinary clearing
        must not end the sweep.
        """
        gate = self._gate()
        (tmp_path / 'pkg' / '__pycache__').mkdir(parents=True)

        assert gate._clear_pycache_or_stop(tmp_path) is None
        assert capsys.readouterr().out == ''


class TestAnInterruptWhileACleanupWarningPrints:
    """The half of the deferred-interrupt promise that was not kept.

    ``restore_all`` holds a second Ctrl+C across ``edit.restore()``, but
    every ``out`` call sat outside a try. An interrupt raised while a
    cleanup warning was printing left ``restore_all`` at once: the
    remaining flags were never attempted, which for ``LOG_TRANSCRIPTS``
    keeps dictated text reaching the log, and an interrupt in the closing
    notices suppressed the restart report the function says has to survive
    an interrupt, along with the re-raise of an interrupt already stored.
    """

    class _Console:
        """An ``out`` interrupted BEFORE it writes. Calls count from one.

        Nothing is recorded on an interrupted call, which is the worst
        case of a real one: the operator saw none of that line, so only a
        second attempt can give it to them.
        """

        def __init__(self, *interrupt_on, always=False):
            self.said = []
            self.calls = 0
            self._interrupt_on = set(interrupt_on)
            self._always = always

        def __call__(self, message):
            self.calls += 1
            if self._always or self.calls in self._interrupt_on:
                raise KeyboardInterrupt('the console')
            self.said.append(message)

    class _Edit:
        def __init__(self, key, raises=None):
            self.path = Path('config.toml')
            self.key = key
            self.value = True
            self.changed = True
            self.restored = False
            self._raises = raises

        def restore(self):
            if self._raises is not None:
                raise self._raises
            self.restored = True
            self.changed = False
            return True

    def test_an_interrupt_printing_a_failure_warning_restores_the_rest(self):
        """Restores run in reverse, so the flag that fails is the LAST one
        given here and the one that must still be put back is the first.
        """
        console = self._Console(1)
        first = self._Edit('log_load_diagnostics')
        second = self._Edit('LOG_TRANSCRIPTS', raises=OSError('it is gone'))

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([first, second], out=console)

        assert first.restored is True
        assert any('LOG_TRANSCRIPTS' in line for line in console.said)

    def test_an_interrupt_printing_the_deferred_warning_restores_the_rest(
            self):
        """The other arm of the same loop. The first Ctrl+C arrives in the
        restore itself and the second while its warning prints.
        """
        console = self._Console(1)
        first = self._Edit('log_load_diagnostics')
        second = self._Edit('LOG_TRANSCRIPTS', raises=KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([first, second], out=console)

        assert first.restored is True

    def test_an_interrupt_printing_the_restart_notice_keeps_the_rest(self):
        """The flags are already back, so nothing here is recoverable by a
        later run: this is the only time the operator is told the running
        application keeps the test value, and that dictated text keeps
        reaching the log until they restart it.
        """
        console = self._Console(1)
        edit = self._Edit('LOG_TRANSCRIPTS')

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([edit], out=console)

        told = '\n'.join(console.said)
        assert 'restart' in told.lower()
        assert 'DICTATED TEXT' in told

    def test_an_interrupt_printing_the_dictated_text_line_still_stops(self):
        """The last line of the cleanup is still inside the deferral. An
        interrupt caught there and then dropped would end the run with a
        zero exit, telling the operator the test finished normally.
        """
        console = self._Console(2)
        edit = self._Edit('LOG_TRANSCRIPTS')

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([edit], out=console)

        assert any('DICTATED TEXT' in line for line in console.said)

    def test_the_interrupt_the_caller_gets_is_the_one_that_was_stored(self):
        """One Ctrl+C reaches the caller, and it is the first one. An
        interrupt from ``out`` used to escape in place of the stored one,
        so the run ended on an interrupt raised after the cleanup rather
        than on the one that stopped it.
        """
        console = self._Console(always=True)
        edit = self._Edit('LOG_TRANSCRIPTS',
                          raises=KeyboardInterrupt('the restore'))

        with pytest.raises(KeyboardInterrupt) as caught:
            run.restore_all([edit], out=console)

        assert str(caught.value) == 'the restore'

    def test_a_console_that_never_finishes_a_line_still_ends(self):
        """An operator holding Ctrl+C down must not put the cleanup in a
        loop it cannot leave. Two flags go back, so three notices are due
        -- two restart lines and the dictated-text line -- and each is
        attempted twice and then given up.
        """
        console = self._Console(always=True)
        first = self._Edit('log_load_diagnostics')
        second = self._Edit('LOG_TRANSCRIPTS')

        with pytest.raises(KeyboardInterrupt):
            run.restore_all([first, second], out=console)

        assert [first.restored, second.restored] == [True, True]
        assert console.calls == 6


class TestAnInterruptEnteringTheBaselineSave:
    """A Ctrl+C on the way INTO the save used to leave main silent.

    ``main`` catches only ``OSError`` around ``save_baseline``, and CPython
    delivers a pending signal at a bytecode boundary -- so the interrupt
    can land on the call into ``save_baseline``, or on the call into
    ``clear_baseline`` before that function's first statement runs. No
    unlink has happened either way. The interrupt leaves ``main`` through
    its outer ``finally``, the config flags go back, and nothing at all is
    said about the cache: an earlier baseline.json whose provenance still
    matches stays on disk, and the next ordinary load run reads it as this
    machine's calibration -- a number the interrupted run never finished
    measuring (finding wh-load-test-script.1.30).

    Saying it whenever an interrupt arrives is the same defect pointing the
    other way. The interrupt can also land after the new baseline was
    written and before ``main`` stores the return value, and then the file
    on disk is this run's own calibration; sending the operator to delete
    that is the false instruction
    ``test_a_removal_that_finished_says_nothing_about_deleting_it``
    forbids. So three outcomes are separated, not two: no file at all, a
    file this run wrote, and a file that predates this run.

    These are the first tests in this file that drive ``main``. The absence
    of any was why the window was left open at wh-load-test-script.1.29:
    a line printed there could not be guarded at all.
    """

    class _Edit:
        """A ``FlagEdit`` that touches no file and records its restore."""

        def __init__(self, path, key, value):
            self.path = path
            self.key = key
            self.value = value
            self.changed = False
            self.restored = False

        def apply(self):
            return False

        def restore(self):
            self.restored = True
            return False

    def _provenance(self, tmp_path):
        """What main's own run will compute, from the same inputs."""
        return run.baseline_provenance(
            run.find_paths(tmp_path), 'Parakeet v3 (CPU)', 800.0)

    def _argv(self, tmp_path):
        return ['--baseline', '--playback', '--repo-root', str(tmp_path)]

    def _mocked_run(self, tmp_path, monkeypatch, *, cache, utterances=(),
                    rotated=False):
        """Everything a --baseline run needs EXCEPT the cache itself.

        The log file, the config edits, the wait for the provider's line,
        the processor counter, the playback and the report are stood in
        for. The cache directory, the provenance,
        ``baseline_ratio_to_save`` and the save are the real ones, because
        they are what this class is about: a stand-in that stopped short of
        the save would prove nothing about the boundary the interrupt lands
        on.

        Returns the list the stand-in ``FlagEdit`` records itself in, so a
        test can prove the config restoration still ran.
        """
        (tmp_path / 'wheelhouse.log').write_text('', encoding='utf-8')
        edits = []

        def make_edit(path, key, value):
            edit = self._Edit(path, key, value)
            edits.append(edit)
            return edit

        monkeypatch.setattr(run, 'FlagEdit', make_edit)
        monkeypatch.setattr(run, 'read_endpoint_silence_ms',
                            lambda _path: 800.0)
        monkeypatch.setattr(run.record, 'CACHE_DIR', cache)
        monkeypatch.setattr(run.record, 'ensure_recording',
                            lambda *a, **k: (tmp_path / 'six.wav', False))
        monkeypatch.setattr(run, 'wait_for_window_line',
                            lambda _path, _timeout: True)
        monkeypatch.setattr(run, 'log_mark', lambda _path: 'the mark')
        monkeypatch.setattr(run, 'measure_busy_percent', lambda: 1.0)
        monkeypatch.setattr(run, 'play_through_speakers',
                            lambda _wav, _device: (12.0, 0))
        monkeypatch.setattr(run.time, 'sleep', lambda _seconds: None)
        monkeypatch.setattr(run, 'read_since',
                            lambda _path, _mark: ('', rotated))
        monkeypatch.setattr(
            run.logparse, 'parse_log',
            lambda _text: logparse.ParsedLog(utterances=list(utterances)))
        monkeypatch.setattr(run.report, 'render', lambda **_fields: '')
        return edits

    @staticmethod
    def _never_started(message):
        """A call the interrupt reached before its first statement ran."""
        def stop(*_args, **_kwargs):
            raise KeyboardInterrupt(message)
        return stop

    @pytest.mark.parametrize('utterances,rotated', [
        ([_utterance(ratio=0.42)], False),
        ([], False),
        ([_utterance(ratio=0.42)], True),
    ], ids=['a-measured-ratio', 'no-measured-ratio', 'a-rotated-log'])
    def test_an_interrupt_entering_the_save_names_the_stale_baseline(
            self, tmp_path, monkeypatch, capsys, utterances, rotated):
        """The finding itself, on all three paths that reach the save: a
        finite per-utterance ratio, a run that measured none, and a rotated
        log that ``baseline_ratio_to_save`` turns into none. They converge
        on one call, so they share one window.
        """
        cache = tmp_path / 'cache'
        provenance = self._provenance(tmp_path)
        run.save_baseline(cache, 0.70, provenance)
        edits = self._mocked_run(tmp_path, monkeypatch, cache=cache,
                                 utterances=utterances, rotated=rotated)
        monkeypatch.setattr(run, 'save_baseline',
                            self._never_started('entering the save'))

        with pytest.raises(KeyboardInterrupt) as caught:
            run.main(self._argv(tmp_path))

        # The one interrupt the operator asked for reaches the caller, not
        # a second one raised by the guard on the way out.
        assert str(caught.value) == 'entering the save'
        told = capsys.readouterr().out
        assert str(run.baseline_file(cache)) in told
        # The words as well as the constant, so emptying the constant is
        # caught rather than satisfied.
        assert run.DELETE_THE_BASELINE_YOURSELF in told
        assert 'Delete that file yourself' in told
        # The harm the operator is being told about: the old calibration is
        # still there, still matching, still what the next load run reads.
        assert run.load_baseline(cache, provenance) == 0.70
        # And the config still went back.
        assert [edit.restored for edit in edits] == [True]

    def test_an_interrupt_entering_the_invalidation_names_it_too(
            self, tmp_path, monkeypatch, capsys):
        """The second boundary. ``save_baseline`` is the real one here, so
        the interrupt lands on the call it opens with -- past nothing,
        because the invalidation is its first executable statement and that
        statement had not begun.
        """
        cache = tmp_path / 'cache'
        provenance = self._provenance(tmp_path)
        run.save_baseline(cache, 0.70, provenance)
        self._mocked_run(tmp_path, monkeypatch, cache=cache,
                         utterances=[_utterance(ratio=0.42)])
        monkeypatch.setattr(run, 'clear_baseline',
                            self._never_started('entering the invalidation'))

        with pytest.raises(KeyboardInterrupt) as caught:
            run.main(self._argv(tmp_path))

        assert str(caught.value) == 'entering the invalidation'
        told = capsys.readouterr().out
        assert run.DELETE_THE_BASELINE_YOURSELF in told
        assert 'Delete that file yourself' in told
        assert run.load_baseline(cache, provenance) == 0.70

    def test_the_baseline_this_run_wrote_is_never_called_stale(
            self, tmp_path, monkeypatch, capsys):
        """The trap. The interrupt lands after ``_atomic_write`` finished
        and before ``main`` stores what the save returned, so the file on
        disk is THIS run's calibration. An existence check alone would send
        the operator to delete a baseline that is valid and current.
        """
        cache = tmp_path / 'cache'
        provenance = self._provenance(tmp_path)
        run.save_baseline(cache, 0.70, provenance)
        self._mocked_run(tmp_path, monkeypatch, cache=cache,
                         utterances=[_utterance(ratio=0.42)])
        real_write = run._atomic_write

        def write_then_stop(path, data):
            real_write(path, data)
            raise KeyboardInterrupt('after the write')

        monkeypatch.setattr(run, '_atomic_write', write_then_stop)

        with pytest.raises(KeyboardInterrupt):
            run.main(self._argv(tmp_path))

        told = capsys.readouterr().out
        # The state the trap needs, asserted rather than assumed: the file
        # on disk is the one this run wrote, not the 0.70 it replaced.
        assert run.load_baseline(cache, provenance) == 0.42
        assert run.DELETE_THE_BASELINE_YOURSELF not in told
        assert 'Delete that file yourself' not in told

    def test_a_baseline_that_is_not_there_is_never_called_stale(
            self, tmp_path, monkeypatch, capsys):
        """The other half of the honest answer. Telling an operator to
        delete a file that was never there sends them looking for nothing
        and teaches them to skim the line that matters.
        """
        cache = tmp_path / 'cache'
        self._mocked_run(tmp_path, monkeypatch, cache=cache,
                         utterances=[_utterance(ratio=0.42)])
        monkeypatch.setattr(run, 'save_baseline',
                            self._never_started('entering the save'))

        with pytest.raises(KeyboardInterrupt):
            run.main(self._argv(tmp_path))

        told = capsys.readouterr().out
        assert not run.baseline_file(cache).exists()
        assert run.DELETE_THE_BASELINE_YOURSELF not in told
        assert 'Delete that file yourself' not in told

    def test_the_operator_is_told_to_delete_it_once(
            self, tmp_path, monkeypatch, capsys):
        """``clear_baseline`` already says it when neither unlink attempt
        finished. A second copy from ``main`` sends the operator after one
        file twice, which is how a line stops being read.
        """
        cache = tmp_path / 'cache'
        provenance = self._provenance(tmp_path)
        run.save_baseline(cache, 0.70, provenance)
        self._mocked_run(tmp_path, monkeypatch, cache=cache,
                         utterances=[_utterance(ratio=0.42)])
        target = run.baseline_file(cache)
        real_unlink = Path.unlink

        def interrupted(self, missing_ok=False):
            if self != target:
                return real_unlink(self, missing_ok=missing_ok)
            raise KeyboardInterrupt('at the unlink')

        monkeypatch.setattr(Path, 'unlink', interrupted)

        with pytest.raises(KeyboardInterrupt):
            run.main(self._argv(tmp_path))

        monkeypatch.undo()
        told = capsys.readouterr().out
        assert told.count(run.DELETE_THE_BASELINE_YOURSELF) == 1
        assert run.load_baseline(cache, provenance) == 0.70

    def test_a_second_interrupt_printing_the_line_does_not_carry_it_away(
            self, tmp_path, monkeypatch):
        """The line is the only thing between the operator and a
        calibration nobody measured, so it is said through ``_say``: an
        interrupt landing while it prints is held and the line gets another
        attempt, rather than taking the only warning away with it.
        """
        cache = tmp_path / 'cache'
        run.save_baseline(cache, 0.70, self._provenance(tmp_path))
        self._mocked_run(tmp_path, monkeypatch, cache=cache,
                         utterances=[_utterance(ratio=0.42)])
        said = []
        attempts = []

        def console(*parts):
            message = ' '.join(str(part) for part in parts)
            if run.DELETE_THE_BASELINE_YOURSELF in message:
                attempts.append(1)
                if len(attempts) == 1:
                    raise KeyboardInterrupt('the console')
            said.append(message)

        monkeypatch.setattr('builtins.print', console)
        monkeypatch.setattr(run, 'save_baseline',
                            self._never_started('entering the save'))

        with pytest.raises(KeyboardInterrupt) as caught:
            run.main(self._argv(tmp_path))

        # The stored interrupt is the one that reaches the caller, not the
        # console's.
        assert str(caught.value) == 'entering the save'
        assert attempts == [1, 1]
        assert any(run.DELETE_THE_BASELINE_YOURSELF in line for line in said)

    def test_an_uninterrupted_baseline_run_says_nothing_about_deleting(
            self, tmp_path, monkeypatch, capsys):
        """The run that finishes, which is also what proves the stand-ins
        above really reach the save rather than stopping short of it: the
        new calibration is on disk, the exit is zero, and nothing is said
        about deleting anything.
        """
        cache = tmp_path / 'cache'
        provenance = self._provenance(tmp_path)
        run.save_baseline(cache, 0.70, provenance)
        edits = self._mocked_run(tmp_path, monkeypatch, cache=cache,
                                 utterances=[_utterance(ratio=0.42)])

        assert run.main(self._argv(tmp_path)) == 0

        told = capsys.readouterr().out
        assert run.DELETE_THE_BASELINE_YOURSELF not in told
        assert 'Delete that file yourself' not in told
        assert run.load_baseline(cache, provenance) == 0.42
        assert [edit.restored for edit in edits] == [True]


def _never_entered():
    """A body nothing calls.

    Its code object is the decoy a stand-in wears as ``__code__`` so that
    no traceback frame can ever belong to it, which is what the caller
    sees when an interrupt reaches a callee before the callee has a frame
    at all -- a C function, or the interpreter checking for the signal in
    the caller's own frame.
    """


@contextlib.contextmanager
def _interrupt_entering(code, message,
                        error: type[BaseException] = KeyboardInterrupt):
    """Stop the first entry into ``code`` before its body runs.

    A real Ctrl+C at a call boundary is delivered at the callee's FIRST
    instruction: the frame exists, it is still sitting on the callee's own
    ``def`` line, and no statement of the body has run. Measured on this
    interpreter with a second thread calling ``_thread.interrupt_main``
    while the main thread span on a guarded call: 93 of 93 interrupts the
    caller's ``try`` caught had exactly that shape, and none was raised in
    the caller's own frame.

    A stand-in function that raises from its own body cannot reproduce it,
    because its frame has already moved to one of its statements -- that
    is the shape of a callee that RAN. So the injection goes through
    ``sys.settrace``, whose ``call`` event fires at the same first
    instruction and leaves the same frame behind. Both closures and plain
    functions are used below on purpose: a closure's frame begins past
    instruction 0, so an offset test would read one stopped at its door as
    one that had run.

    One entry only. CPython turns tracing off as soon as a trace function
    raises, so the retry runs untraced whatever this does; the list keeps
    that explicit instead of depending on it, and a test that needs the
    door shut on every attempt uses ``_AtTheDoor`` below.
    """
    entries = []

    def tracer(frame, event, _arg):
        if event == 'call' and frame.f_code is code and not entries:
            entries.append(frame.f_code.co_name)
            raise error(message)
        return None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        yield entries
    finally:
        sys.settrace(previous)


class _AtTheDoor:
    """A callee the interrupt reaches at its door the first ``doors`` times.

    ``__code__`` names ``_never_entered``, whose frame never appears in any
    traceback, so the guard reads every call as one whose body did not
    start -- the other shape of the same event, and the only one a test
    can repeat, since a raising trace function disarms itself.
    """

    def __init__(self, real, doors=1, message='at the door'):
        self._real = real
        self._doors = doors
        self._message = message
        self.calls = 0
        self.__code__ = _never_entered.__code__

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self._doors:
            raise KeyboardInterrupt(self._message)
        return self._real(*args, **kwargs)


@pytest.fixture(params=['tool', 'gate', 'runner'])
def guarding(request):
    """Every copy of the cleanup-call guard, one test body for each.

    The gate cannot import the tool: it is standard-library-only by
    design, ``run.py`` reaches ``shared_stt`` through ``script.py`` and
    needs the environment, and the gate's whole job is rewriting the very
    file it would be importing -- a gate whose mutant recovery ran mutated
    code could not recover anything. The shared mutation-gate runner is a
    copy for the same reason, and it gained the guard when a Ctrl+C at the
    restore call's door was found to leave a whole mutant in a tracked
    source file (wh-stt-load-metrics.3.2.5). So the guard exists three
    times, and this fixture makes every test below run against all three.

    The parameter names carry no spaces on purpose: the gate reads failed
    test names out of pytest's output with ``line.split()[0]``, so a space
    inside the bracketed id would cut the name in half and every mutation
    these tests catch would read as a survivor.
    """
    if request.param == 'tool':
        return run
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    if request.param == 'runner':
        import mutation_gate_runner as shared_runner
        return shared_runner
    import mutation_gate_stt_load_test as gate
    return gate


class TestTheCleanupGuardTellsASkippedCallFromAFinishedOne:
    """The distinction the whole guard turns on.

    ``stop_spinners`` and ``restore_all`` END by re-raising an interrupt
    they held, so an escaping KeyboardInterrupt is the ORDINARY outcome of
    a call that did all of its work. A guard that caught one and simply
    called again would put every flag back twice, print every warning
    twice, and kill and wait every spinner twice. The retry has to happen
    only when the body never ran (finding wh-load-test-script.1.33).
    """

    def test_a_call_stopped_at_its_door_is_made_again(self, guarding):
        """A closure, deliberately. A closure's frame starts past
        instruction 0, so a guard that read the offset instead of the line
        would call this one a body that had already run.

        The interrupt is turned into a plain failure rather than allowed
        out: a KeyboardInterrupt reaching pytest aborts the whole session,
        which prints no FAILED line, and a mutation this test catches
        would then read as a survivor.
        """
        marks = []

        def callee():
            marks.append('body')
            return 'the answer'

        with _interrupt_entering(callee.__code__, 'at the door'):
            try:
                result, deferred = guarding._call_cleanup(callee)
            except KeyboardInterrupt as stop:
                pytest.fail(f'the call was never made again: {stop}')

        assert marks == ['body']
        assert result == 'the answer'
        assert str(deferred) == 'at the door'

    def test_a_call_that_ran_and_raised_is_not_made_again(self, guarding):
        """The trap. Its own held interrupt coming back out is what these
        helpers do when they finish, not a sign that they were skipped.
        """
        marks = []

        def callee():
            marks.append('body')
            raise KeyboardInterrupt('after the whole body')

        with pytest.raises(KeyboardInterrupt) as caught:
            guarding._call_cleanup(callee)

        assert marks == ['body']
        assert str(caught.value) == 'after the whole body'

    def test_a_call_stopped_at_its_door_every_time_is_bounded_at_two(
            self, guarding):
        """An operator holding Ctrl+C down must not be put in a loop they
        cannot leave, so the second failure gives the call up and raises
        the interrupt it held.
        """
        marks = []
        door = _AtTheDoor(lambda: marks.append('body'), doors=4)

        with pytest.raises(KeyboardInterrupt) as caught:
            guarding._call_cleanup(door)

        assert door.calls == 2
        assert marks == []
        assert str(caught.value) == 'at the door'

    def test_an_oserror_at_the_door_is_not_deferred(self, guarding):
        """Only a KeyboardInterrupt is held. Anything else propagates on
        the first attempt, exactly as it did before this guard existed.
        """
        marks = []

        def callee():
            marks.append('body')

        with _interrupt_entering(callee.__code__, 'no such file',
                                 error=OSError):
            with pytest.raises(OSError):
                guarding._call_cleanup(callee)

        assert marks == []

    def test_a_callee_with_no_code_object_is_never_called_twice(
            self, guarding):
        """Nothing to read means nothing to prove, and calling something
        twice that may already have finished is the failure this guard
        exists to avoid.
        """
        calls = []

        class _Callable:
            def __call__(self):
                calls.append('call')
                raise KeyboardInterrupt('from something with no frame')

        with pytest.raises(KeyboardInterrupt):
            guarding._call_cleanup(_Callable())

        assert calls == ['call']

    def test_an_uninterrupted_call_holds_nothing_and_runs_once(
            self, guarding):
        """The guard this fix must not weaken."""
        marks = []

        def callee(word):
            marks.append(word)
            return len(marks)

        assert guarding._call_cleanup(callee, 'body') == (1, None)
        assert marks == ['body']


class TestAnInterruptEnteringMainsCleanupStillCleansUp:
    """A Ctrl+C at the door of a cleanup call used to skip the whole call.

    ``main`` stops the spinners from its inner ``finally`` and puts the
    config flags back from its outer one. Both helpers defer an interrupt
    that arrives INSIDE them, and neither can cover its own call boundary:
    the interpreter checks for a pending signal at the callee's first
    instruction, before any statement of the body has run, and the entire
    function is then skipped.

    What that left behind was a machine still saturated by spinner
    processes nobody killed, each holding a core at full load, and -- on a
    --log-transcripts run -- LOG_TRANSCRIPTS still true in the config file
    with none of the three warnings said, so dictated text kept reaching
    the log and nobody was told (finding wh-load-test-script.1.33).
    """

    class _Spinner:
        def __init__(self, pid, interrupt_on=''):
            self.pid = pid
            self._interrupt_on = interrupt_on
            self.kills = 0
            self.waits = 0

        def kill(self):
            self.kills += 1
            if self._interrupt_on == 'kill':
                raise KeyboardInterrupt('inside the stop')

        def wait(self, timeout=None):
            self.waits += 1

    class _Edit:
        def __init__(self, path, key, value, interrupts=False):
            self.path = path
            self.key = key
            self.value = value
            self.changed = True
            self.restores = 0
            self._interrupts = interrupts

        def apply(self):
            return False

        def restore(self):
            self.restores += 1
            if self._interrupts:
                raise KeyboardInterrupt('inside the restore')
            self.changed = False
            return True

    def _argv(self, tmp_path):
        return ['--playback', '--repo-root', str(tmp_path)]

    def _mocked_run(self, tmp_path, monkeypatch, *, spinners=(),
                    edit_interrupts=False):
        """A --playback run with the machine, the log and the report
        stood in for. The two cleanup calls are the real ones: this class
        is about the way out of a run, not about what the run measures.
        """
        (tmp_path / 'wheelhouse.log').write_text('', encoding='utf-8')
        edits = []

        def make_edit(path, key, value):
            edit = self._Edit(path, key, value, interrupts=edit_interrupts)
            edits.append(edit)
            return edit

        monkeypatch.setattr(run, 'FlagEdit', make_edit)
        monkeypatch.setattr(run, 'read_endpoint_silence_ms',
                            lambda _path: 800.0)
        monkeypatch.setattr(run.record, 'CACHE_DIR', tmp_path / 'cache')
        monkeypatch.setattr(run.record, 'ensure_recording',
                            lambda *a, **k: (tmp_path / 'six.wav', False))
        monkeypatch.setattr(run, 'wait_for_window_line',
                            lambda _path, _timeout: True)
        monkeypatch.setattr(run, 'log_mark', lambda _path: 'the mark')
        monkeypatch.setattr(run, 'measure_busy_percent', lambda: 95.0)
        monkeypatch.setattr(run, 'play_through_speakers',
                            lambda _wav, _device: (12.0, 0))
        monkeypatch.setattr(run, 'start_spinners',
                            lambda _count, into: into.extend(spinners))
        monkeypatch.setattr(run.time, 'sleep', lambda _seconds: None)
        monkeypatch.setattr(run, 'read_since',
                            lambda _path, _mark: ('', False))
        monkeypatch.setattr(run.logparse, 'parse_log',
                            lambda _text: logparse.ParsedLog(utterances=[]))
        monkeypatch.setattr(run.report, 'render', lambda **_fields: '')
        return edits

    def test_an_interrupt_entering_the_spinner_stop_still_stops_them(
            self, tmp_path, monkeypatch, capsys):
        """The spinners are the reason this window matters at all: the
        run has just deliberately saturated the machine, and a stop that
        never starts leaves every core held at full load by a process the
        operator has to find by hand.
        """
        spinners = [self._Spinner(1), self._Spinner(2), self._Spinner(3)]
        self._mocked_run(tmp_path, monkeypatch, spinners=spinners)

        with _interrupt_entering(run.stop_spinners.__code__,
                                 'entering the stop'):
            with pytest.raises(KeyboardInterrupt) as caught:
                run.main(self._argv(tmp_path))

        assert [s.kills for s in spinners] == [1, 1, 1]
        assert [s.waits for s in spinners] == [1, 1, 1]
        assert str(caught.value) == 'entering the stop'

    def test_an_interrupt_inside_the_spinner_stop_stops_them_once(
            self, tmp_path, monkeypatch, capsys):
        """The other half of the distinction, and the one a careless guard
        breaks. ``stop_spinners`` catching a Ctrl+C in its kill loop and
        re-raising it at the end is the function doing its job. Calling it
        again would kill and wait every spinner a second time.

        No red for this one against the pre-fix code, which had no retry
        to get wrong. The mutation that turns the guard into a plain
        catch-and-retry is what this test is here for.
        """
        spinners = [self._Spinner(1, interrupt_on='kill'),
                    self._Spinner(2), self._Spinner(3)]
        self._mocked_run(tmp_path, monkeypatch, spinners=spinners)

        with pytest.raises(KeyboardInterrupt) as caught:
            run.main(self._argv(tmp_path))

        assert [s.kills for s in spinners] == [1, 1, 1]
        assert [s.waits for s in spinners] == [1, 1, 1]
        assert str(caught.value) == 'inside the stop'

    def test_an_interrupt_entering_the_flag_restore_still_restores(
            self, tmp_path, monkeypatch, capsys):
        """The most serious of the four. The only thing that puts
        LOG_TRANSCRIPTS back is this call, and skipping it leaves the file
        changed AND says none of the three warnings, so dictated text
        keeps reaching the log with nobody told.
        """
        edits = self._mocked_run(tmp_path, monkeypatch)

        with _interrupt_entering(run.restore_all.__code__,
                                 'entering the restore'):
            with pytest.raises(KeyboardInterrupt) as caught:
                run.main(self._argv(tmp_path))

        assert [edit.restores for edit in edits] == [1]
        assert [edit.changed for edit in edits] == [False]
        assert str(caught.value) == 'entering the restore'

    def test_an_interrupt_inside_the_flag_restore_restores_once(
            self, tmp_path, monkeypatch, capsys):
        """The trap again, on the second helper. ``restore_all`` holds an
        interrupt from ``edit.restore()``, says which flag was left, and
        raises at the end; a second pass would attempt every restore and
        print every warning twice.

        No red against the pre-fix code, for the same reason as the
        spinner twin above.
        """
        edits = self._mocked_run(tmp_path, monkeypatch,
                                 edit_interrupts=True)

        with pytest.raises(KeyboardInterrupt) as caught:
            run.main(self._argv(tmp_path))

        assert [edit.restores for edit in edits] == [1]
        assert str(caught.value) == 'inside the restore'

    def test_an_uninterrupted_run_stops_and_restores_once_each(
            self, tmp_path, monkeypatch, capsys):
        """The guard this fix must not weaken: no interrupt, no retry,
        one stop and one restore, and a zero exit.
        """
        spinners = [self._Spinner(1), self._Spinner(2)]
        edits = self._mocked_run(tmp_path, monkeypatch, spinners=spinners)

        assert run.main(self._argv(tmp_path)) == 0

        assert [s.kills for s in spinners] == [1, 1]
        assert [edit.restores for edit in edits] == [1]


class TestAnInterruptEnteringTheGateCleanupStillCleansUp:
    """The same door, on the two calls that recover the working tree.

    ``_mutate_and_run`` puts the source back and clears the bytecode from
    one ``finally``. A Ctrl+C at the door of either call skipped it: the
    first leaves the mutant sitting in a real source file for every later
    run and every other session in the checkout to read as the real code,
    and the second leaves a mutant .pyc beside a restored source, which
    Python will reuse because a mutation can keep the timestamp and the
    size it checks (finding wh-load-test-script.1.33).

    These two helpers are the OTHER shape. They return their interrupt
    inside an ``(error, interrupt)`` tuple rather than raising it, so one
    escaping means the body did not finish -- the trap the run tool's two
    helpers set does not exist here. The guard does not need to know that:
    it asks only where the interrupt was raised, never what the callee
    would have returned.
    """

    def _gate(self):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import mutation_gate_stt_load_test as gate
        return gate

    def _target(self, tmp_path, monkeypatch, cleared):
        """A throwaway source file, with pytest and the cache stubbed.

        The first interrupt comes from the stubbed ``_run_pytest``, which
        is where a real one arrives: the operator presses Ctrl+C while the
        suite is running.
        """
        gate = self._gate()
        target = tmp_path / 'source.py'
        target.write_bytes(b'value = 1\n')

        def clear(service):
            cleared.append(service)
            return None, None

        monkeypatch.setattr(gate, '_clear_pycache', clear)

        def interrupted(service, test_file, mutant=False):
            raise KeyboardInterrupt('first Ctrl+C, while the suite ran')

        monkeypatch.setattr(gate, '_run_pytest', interrupted)
        return gate, target, b'value = 1\n', b'value = 2\n'

    def test_an_interrupt_entering_the_restore_still_puts_the_source_back(
            self, tmp_path, monkeypatch):
        cleared = []
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, cleared)

        with _interrupt_entering(gate._restore.__code__,
                                 'entering the restore'):
            with pytest.raises(KeyboardInterrupt) as caught:
                gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original
        assert cleared == [gate.SHARED]
        assert str(caught.value) == 'entering the restore'

    def test_an_interrupt_entering_the_cache_clearing_still_clears_it(
            self, tmp_path, monkeypatch):
        """The source is already back by this point, which is what makes
        the cached bytecode dangerous rather than merely stale: the next
        process reads a restored file and runs the mutant's .pyc.
        """
        cleared = []
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, cleared)

        with _interrupt_entering(gate._clear_pycache.__code__,
                                 'entering the clearing'):
            with pytest.raises(KeyboardInterrupt) as caught:
                gate._mutate_and_run(target, original, mutant, 'probe')

        assert target.read_bytes() == original
        assert cleared == [gate.SHARED]
        assert str(caught.value) == 'entering the clearing'

    def test_an_interrupt_leaving_the_restore_does_not_restore_twice(
            self, tmp_path, monkeypatch):
        """A restore that did its work and then let an interrupt out on
        one of its own unguarded bytecodes must not be run again. What
        happens after it is what happened before this guard existed: the
        interrupt leaves the finally and the clearing is skipped.
        """
        cleared = []
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, cleared)
        real_restore = gate._restore
        attempts = []

        def restore_then_stop(path, original_bytes, mutated, name):
            attempts.append(name)
            real_restore(path, original_bytes, mutated, name)
            raise KeyboardInterrupt('leaving the restore')

        monkeypatch.setattr(gate, '_restore', restore_then_stop)

        with pytest.raises(KeyboardInterrupt) as caught:
            gate._mutate_and_run(target, original, mutant, 'probe')

        assert attempts == ['probe']
        assert target.read_bytes() == original
        assert str(caught.value) == 'leaving the restore'

    def test_an_uninterrupted_cleanup_restores_and_clears_once_each(
            self, tmp_path, monkeypatch):
        """The guard this fix must not weaken."""
        cleared = []
        gate, target, original, mutant = self._target(
            tmp_path, monkeypatch, cleared)
        monkeypatch.setattr(
            gate, '_run_pytest',
            lambda service, test_file, mutant=False: 'the run')

        run_result, timed_out, restore_error = gate._mutate_and_run(
            target, original, mutant, 'probe')

        assert run_result == 'the run'
        assert timed_out is False
        assert restore_error is None
        assert target.read_bytes() == original
        assert cleared == [gate.SHARED]


class TestALabelWiderThanItsColumnStillSeparates:
    """A heading and its answer must not run together.

    ``_field`` pads a label out to a fixed column and puts the value at
    that column. Padding is all it does, so a label already at or past the
    column gets no padding at all and the value starts at the character
    after the label's last one. The only label in the report that is built
    at run time -- "Under-load per-utterance lines: N of M sentences
    measured" -- is 55 characters against a 43-character column, so the
    one render that has no per-utterance evidence to show is exactly the
    render that fuses its heading to the sentence explaining the absence
    (finding wh-load-test-script.1.37).
    """

    def test_an_over_long_label_keeps_its_value_a_separate_word(self):
        line = report._field('x' * (report._WIDTH + 12) + ':', 'the value')
        assert line.endswith(' the value'), line

    def test_a_label_inside_the_column_still_lands_on_the_column(self):
        """The guard this fix must not weaken: every other field in the
        report reads as a column, and a fix that separated by one space
        everywhere would take that away.
        """
        line = report._field('short:', 'the value')
        assert line == 'short:'.ljust(report._WIDTH) + 'the value'

    def test_a_run_with_no_per_utterance_lines_says_so_as_its_own_words(self):
        rendered = _rendered(
            parsed=logparse.ParsedLog(utterances=[], windows=[_window()]))
        line = next(l for l in rendered.splitlines()
                    if l.startswith('Under-load per-utterance lines:'))
        assert 'measuredno per-utterance' not in line, line
        assert line.endswith(' no per-utterance line was written'), line


class TestTheProcedureDoesNotPromisePlaybackByDefault:
    """Only ``--playback`` plays the script; the default run needs a person.

    The pre-run bullet said, without qualification, that the script plays
    audio and reads the log. The parser gives ``--playback`` the
    ``store_true`` default, so ``main`` leaves ``wav_path`` as None and
    calls ``speak_sentences`` with ``wait=lambda: input()``. An operator
    who reads that bullet and runs the documented default command waits
    for audio that never arrives, and the run sits at its first prompt
    with no test speech at all (finding wh-load-test-script.1.38).
    """

    def _running_bullet(self):
        """The whole 'WheelHouse has to be running' bullet, as one line."""
        doc = _procedure_text()
        _, sep, tail = doc.partition('- **WheelHouse has to be running')
        assert sep, 'the procedure does not say WheelHouse has to be running'
        return ' '.join(tail.split('\n- ')[0].split())

    def test_the_default_really_does_not_play_anything(self):
        """The document is checked against the code, not against itself."""
        parser = run.build_parser()
        assert parser.parse_args([]).playback is False

    def test_the_bullet_never_promises_playback_without_the_flag(self):
        bullet = self._running_bullet()
        assert 'The script plays audio' not in bullet, bullet
        assert '--playback' in bullet, bullet

    def test_the_bullet_says_the_default_run_needs_a_person_to_speak(self):
        assert 'speak' in self._running_bullet()

    def test_the_bullet_still_refuses_to_hand_the_recognizer_a_file(self):
        """The guard this fix must not weaken. The reason the run goes
        through the microphone at all is that a file skips capture, which
        is the half of the question this procedure exists to ask.
        """
        assert 'skip capture' in self._running_bullet()


class TestAnUnreadableFinalLogIsNotAnEmptyRun:
    """``read_since`` answered a refused read with the empty slice.

    After a successful mark, an ``OSError`` from the final ``open`` came
    back as ``('', False)`` -- the same pair a readable log with nothing
    new in it produces. ``main`` parsed that, judged it, printed a
    completed template and returned 0, so a Windows sharing violation or
    the momentary absence of wheelhouse.log during rotation was presented
    as a finished run that happened to measure nothing.

    Under ``--baseline`` the same empty slice reached ``save_baseline``
    with no ratio, and that call REMOVES the operator's existing
    calibration. A run with no evidence about its own log has said nothing
    about the machine, so it may not throw away the last run that did
    (finding wh-load-test-script.1.36).

    ``read_since`` now answers None when it could not read and a pair when
    it could. A caller that ignores the difference unpacks None and fails
    loudly rather than measuring nothing quietly.

    These tests make the REAL ``open`` refuse rather than standing
    ``read_since`` in with one that returns None. A stand-in returning
    None makes the unfixed code raise TypeError, and a test that fails on
    a value the shipped code never produces has proved nothing about it.
    The refusal is narrowed to run.py's own module namespace, so pytest's
    own reads are untouched.
    """

    class _Edit:
        """A ``FlagEdit`` that touches no file."""

        def __init__(self, path, key, value):
            self.path = path
            self.key = key
            self.value = value
            self.changed = False
            self.restored = False

        def apply(self):
            return False

        def restore(self):
            self.restored = True
            return False

    def _argv(self, tmp_path, *extra):
        return ['--playback', '--repo-root', str(tmp_path), *extra]

    def _refuse_open(self, monkeypatch, log, times=None):
        """Refuse to open ``log``, the way a sharing violation does.

        ``times`` None refuses for good; an integer refuses that many
        attempts and then lets the read through, which is what an
        ordinary rotation looks like. Returns the list holding the
        remaining count, so a test can prove the retries were spent.
        """
        real_open = open
        left = [times]

        def maybe_refuse(file, *args, **kwargs):
            if Path(file) == log and (left[0] is None or left[0] > 0):
                if left[0] is not None:
                    left[0] -= 1
                raise PermissionError('the file is being rotated')
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(run, 'open', maybe_refuse, raising=False)
        return left

    def _mocked_run(self, tmp_path, monkeypatch, *, cache,
                    busy_percent=99.0):
        """Everything a run needs except the reading of its own log.

        ``log_mark`` is deliberately NOT stood in for: these tests run the
        real ``read_since``, which reads ``mark.size`` and
        ``mark.identity``, and a stand-in mark would fail on the attribute
        rather than on the refusal.

        ``busy_percent`` defaults above SATURATION_FLOOR_PERCENT for a
        load run, which refuses below it. A ``--baseline`` run refuses
        ABOVE BASELINE_CEILING_PERCENT instead, so those tests pass an
        idle number. Leaving the default in place made one of them pass on
        the busy-machine refusal, several steps before the read this class
        is about.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('', encoding='utf-8')
        monkeypatch.setattr(run, 'FlagEdit',
                            lambda path, key, value: self._Edit(
                                path, key, value))
        monkeypatch.setattr(run, 'read_endpoint_silence_ms',
                            lambda _path: 800.0)
        monkeypatch.setattr(run.record, 'CACHE_DIR', cache)
        monkeypatch.setattr(run.record, 'ensure_recording',
                            lambda *a, **k: (tmp_path / 'six.wav', False))
        monkeypatch.setattr(run, 'wait_for_window_line',
                            lambda _path, _timeout: True)
        monkeypatch.setattr(run, 'measure_busy_percent',
                            lambda: busy_percent)
        monkeypatch.setattr(run, 'play_through_speakers',
                            lambda _wav, _device: (12.0, 0))
        monkeypatch.setattr(run.time, 'sleep', lambda _seconds: None)
        monkeypatch.setattr(run.report, 'render',
                            lambda **_fields: 'THE COMPLETED TEMPLATE')
        # The load itself is stood in for. These tests are about the read
        # of the log, and without this the two load runs below each
        # started one real busy loop per logical core and relied on
        # ``main`` to stop them -- the behaviour the mutation gate breaks
        # on purpose (wh-load-test-script.2).
        monkeypatch.setattr(run, 'start_spinners',
                            lambda _count, into: None)
        return log

    def test_a_read_it_could_not_make_is_told_apart_from_an_empty_slice(
            self, tmp_path, monkeypatch):
        """The whole finding in two calls. Both used to answer
        ``('', False)``, and no caller could tell which had happened.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('', encoding='utf-8')
        mark = run.log_mark(log)
        assert mark is not None

        assert run.read_since(log, mark) == ('', False)

        self._refuse_open(monkeypatch, log)
        assert run.read_since(log, mark) is None

    def test_a_load_run_that_cannot_read_its_log_refuses_rather_than_judging(
            self, tmp_path, monkeypatch, capsys):
        log = self._mocked_run(tmp_path, monkeypatch,
                               cache=tmp_path / 'cache')
        self._refuse_open(monkeypatch, log)

        code = run.main(self._argv(tmp_path))

        told = capsys.readouterr().out
        assert code == 1, told
        assert 'THE COMPLETED TEMPLATE' not in told, told
        assert str(log) in told, told
        assert '[x]' in told, told

    def test_a_baseline_run_that_cannot_read_its_log_keeps_the_calibration(
            self, tmp_path, monkeypatch, capsys):
        """The consequence that costs the operator something real."""
        cache = tmp_path / 'cache'
        provenance = run.baseline_provenance(
            run.find_paths(tmp_path), 'Parakeet v3 (CPU)', 800.0)
        run.save_baseline(cache, 0.70, provenance)
        before = run.baseline_file(cache).read_bytes()

        log = self._mocked_run(tmp_path, monkeypatch, cache=cache,
                               busy_percent=1.0)
        self._refuse_open(monkeypatch, log)

        code = run.main(self._argv(tmp_path, '--baseline'))

        told = capsys.readouterr().out
        assert code == 1, told
        assert run.baseline_file(cache).read_bytes() == before, told

    def test_a_refused_read_is_retried_before_the_run_is_refused(
            self, tmp_path, monkeypatch, capsys):
        """A sharing violation while ConcurrentRotatingFileHandler rotates
        wheelhouse.log is ordinary and brief. Refusing on the first one
        throws away a run that has already been measured, and the machine
        was saturated for minutes to produce it.
        """
        log = self._mocked_run(tmp_path, monkeypatch,
                               cache=tmp_path / 'cache')
        left = self._refuse_open(monkeypatch, log, times=2)

        code = run.main(self._argv(tmp_path))

        told = capsys.readouterr().out
        assert code == 0, told
        assert 'THE COMPLETED TEMPLATE' in told, told
        assert left[0] == 0, 'the read was never retried past the refusals'

    def test_a_failure_after_the_open_is_a_refused_read_not_a_traceback(
            self, tmp_path, monkeypatch):
        """``os.fstat``, ``seek`` and ``read`` all run after the open has
        already succeeded, and each can fail on its own. Leaving them
        outside the guard turns a rotation landing mid-read into a
        traceback on the operator's screen. This one fails BEFORE the fix
        by raising rather than by asserting, which is the defect itself:
        the finding is that the error escapes.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('a line\n', encoding='utf-8')
        mark = run.log_mark(log)
        assert mark is not None

        def refuse(_fileno):
            raise PermissionError('the file is being rotated')

        monkeypatch.setattr(run.os, 'fstat', refuse)

        assert run.read_since(log, mark) is None

    def test_the_readiness_poll_survives_a_read_it_could_not_make(
            self, tmp_path, monkeypatch):
        """The caller-side contract. ``wait_for_window_line`` used to
        unpack the result unconditionally, so the new None would reach it
        as a TypeError. Nothing about the poll's ANSWER changes -- an
        empty slice and a refusal both leave it waiting -- so this is what
        there is to guard: a later edit that unpacks again fails here.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('', encoding='utf-8')
        monkeypatch.setattr(run, 'read_since', lambda _path, _mark: None)
        monkeypatch.setattr(run.time, 'sleep', lambda _seconds: None)

        assert run.wait_for_window_line(log, 0.05) is False

    def test_a_refusal_does_not_end_the_readiness_poll_early(
            self, tmp_path, monkeypatch):
        log = tmp_path / 'wheelhouse.log'
        log.write_text('', encoding='utf-8')
        line = ('[load-diag] window=10.0s capture_available=yes q_now=2 '
                'q_max=9 drops=0 overflow=0 status_flags=0 stalls=0 '
                'max_gap_ms=120.0 max_frame_gap_ms=200.0\n')
        answers = [None, None, (line, False)]
        monkeypatch.setattr(run, 'read_since',
                            lambda _path, _mark: answers.pop(0))
        monkeypatch.setattr(run.time, 'sleep', lambda _seconds: None)

        assert run.wait_for_window_line(log, 30.0) is True
        assert answers == []

    def test_the_preflight_says_so_when_the_log_cannot_be_stated(
            self, tmp_path, monkeypatch, capsys):
        """``Path.is_file`` re-raises every OSError its own ignore list
        does not cover. Measured on this interpreter, that list holds
        ENOENT, ENOTDIR, EBADF and ELOOP, and does NOT hold EACCES, EPERM
        or a Windows sharing violation. So the refusal the tool is most
        likely to meet came out as a traceback instead of the clear
        diagnostic sitting on the very next line.
        """
        log = tmp_path / 'wheelhouse.log'
        log.write_text('', encoding='utf-8')
        real_is_file = Path.is_file

        def refuse(self, *args, **kwargs):
            if self == log:
                raise PermissionError('the file is being rotated')
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'is_file', refuse)

        code = run.main(self._argv(tmp_path))

        told = capsys.readouterr().out
        assert code == 1, told
        assert '[x]' in told, told
        assert str(log) in told, told


# The Google provider's per-utterance line: the Parakeet field order with
# stalls= and stall_max_ms= inserted after q_n, and the five engine fields
# reading n/a because Google runs the recognizer and there is no local
# engine time to report (wh-stt-load-metrics.4).
GOOGLE_UTT_LINE = (PREFIX + "[load-diag] utt=7 kind=GOOGLE_FINAL "
                   "overflow=0 status_flags=0 drops=4 "
                   "q_max=327 q_mean=88.4 q_n=210 "
                   "stalls=2 stall_max_ms=9900.0 "
                   "engine_calls=n/a engine_ms_total=n/a engine_ms_max=n/a "
                   "audio_ms=n/a engine_ratio=n/a")


class TestOneParserReadsBothProviders:
    """wh-stt-load-metrics.4. The Google line carries no engine timings and
    two stall fields the Parakeet line does not have. Before this change the
    parser required all five engine fields as numbers, so one `n/a` dropped
    the whole line and took every capture and queue number on it.
    """

    def test_the_google_line_keeps_its_numbers_when_the_engine_fields_are_na(
            self):
        parsed = logparse.parse_log(GOOGLE_UTT_LINE)
        assert len(parsed.utterances) == 1
        line = parsed.utterances[0]
        assert line.utt == 7
        assert line.kind == 'GOOGLE_FINAL'
        assert line.drops == 4
        assert line.q_max == 327
        assert line.q_mean == pytest.approx(88.4)
        assert line.q_n == 210

    def test_an_engine_field_the_provider_never_measured_is_not_a_number(self):
        """The same rule the capture fields already follow: `n/a` means the
        provider measured nothing there, and a 0 would read as "the
        recognizer kept up", which nothing measured.
        """
        line = logparse.parse_log(GOOGLE_UTT_LINE).utterances[0]
        assert line.engine_calls is None
        assert line.engine_ms_total is None
        assert line.engine_ms_max is None
        assert line.audio_ms is None
        assert line.engine_ratio is None

    def test_the_stall_fields_are_read(self):
        line = logparse.parse_log(GOOGLE_UTT_LINE).utterances[0]
        assert line.stalls == 2
        assert line.stall_max_ms == pytest.approx(9900.0)

    def test_a_parakeet_line_without_the_stall_fields_still_parses(self):
        """The two fields are new, and every line written before this change
        lacks them. A parser that required them would refuse every existing
        log.
        """
        line = logparse.parse_log(UTT_LINE).utterances[0]
        assert line.stalls is None
        assert line.stall_max_ms is None
        assert line.engine_ratio == pytest.approx(1.35)
        assert line.engine_calls == 12

    def test_a_ratio_that_is_not_a_measurement_is_still_refused(self):
        """`float()` accepts "nan", and every comparison against NaN is
        false, so a NaN ratio used to reach the verdict ladder and fall
        through to `neither` (wh-load-test-script.1.25). Making the field
        optional must not lose that refusal.
        """
        text = UTT_LINE.replace('engine_ratio=1.35', 'engine_ratio=nan')
        assert logparse.parse_log(text).utterances == []
        text = UTT_LINE.replace('engine_ratio=1.35', 'engine_ratio=inf')
        assert logparse.parse_log(text).utterances == []

    def test_a_line_missing_a_field_the_provider_always_writes_is_refused(
            self):
        """utt, kind and the three queue fields are written by every
        provider, so their absence means a truncated line rather than an
        unmeasured quantity. A half-read measurement is worse than a missing
        one.
        """
        text = UTT_LINE.replace(' q_max=333', '')
        assert logparse.parse_log(text).utterances == []


class TestAVerdictWithNoEngineTiming:
    """A Google run carries no engine_ratio at all, so the inference-lag
    half of the ladder has nothing to read. It must say so rather than read
    a missing measurement as a good one.
    """

    def test_utterances_without_a_ratio_do_not_earn_a_clean_reading(self):
        parsed = logparse.ParsedLog(utterances=[_utterance(ratio=None)],
                                    windows=[_window()])
        verdict = judge.judge_run(parsed, _all_correct(), baseline_ratio=0.7)
        assert verdict.verdict == judge.UNDETERMINED
        assert any('engine_ratio' in line for line in verdict.evidence)

    def test_a_capture_failure_is_still_named_without_engine_timing(self):
        """The overflow half of the ladder does not need engine_ratio, so a
        Google run that lost frames still earns its verdict.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(overflow=48, ratio=None)],
            windows=[_window()])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.CALLBACK_LOSS

    def test_a_run_that_measured_some_ratios_uses_the_ones_it_has(self):
        """One late line hides one utterance's engine_ratio; the rest still
        decide the verdict (the 2026-08-30 partly-measured ruling).
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(index=1, ratio=None),
                        _utterance(index=2, ratio=2.4)],
            windows=[_window()])
        verdict = judge.judge_run(parsed, _one_missing(), baseline_ratio=0.7)
        assert verdict.verdict == judge.INFERENCE_LAG
        assert any('2.40' in line for line in verdict.evidence)


class TestReportingARunWithNoEngineTiming:
    """The consequence of making the engine fields optional: everything that
    formats or compares them meets None on a Google run (wh-stt-load-metrics.4).
    """

    def test_the_per_utterance_report_line_survives_a_google_utterance(self):
        """`f'{None:.1f}'` raises TypeError, so the report used to be unable
        to print the run it was asked to summarise.
        """
        line = report._utterance_line(
            logparse.UtteranceMetrics(
                utt=7, kind='GOOGLE_FINAL', overflow=0, status_flags=0,
                drops=4, q_max=327, q_mean=88.4, q_n=210,
                engine_calls=None, engine_ms_total=None, engine_ms_max=None,
                audio_ms=None, engine_ratio=None,
                stalls=2, stall_max_ms=9900.0))
        assert 'engine_calls=n/a' in line
        assert 'engine_ms_total=n/a' in line
        assert 'engine_ms_max=n/a' in line
        assert 'audio_ms=n/a' in line
        assert 'engine_ratio=n/a' in line

    def test_the_report_line_carries_the_stall_numbers(self):
        """They are the whole point of the Google line; a report that
        dropped them would send the reader back to the raw log.
        """
        line = report._utterance_line(
            logparse.UtteranceMetrics(
                utt=7, kind='GOOGLE_FINAL', overflow=0, status_flags=0,
                drops=4, q_max=327, q_mean=88.4, q_n=210,
                engine_calls=None, engine_ms_total=None, engine_ms_max=None,
                audio_ms=None, engine_ratio=None,
                stalls=2, stall_max_ms=9900.0))
        assert 'stalls=2' in line
        assert 'stall_max_ms=9900.0' in line

    def test_a_parakeet_utterance_still_prints_its_engine_numbers(self):
        line = report._utterance_line(_utterance(ratio=1.35))
        assert 'engine_ratio=1.35' in line
        assert 'engine_ms_total=4200.0' in line
        assert 'stalls=n/a' in line

    def test_the_saved_baseline_ignores_an_unmeasured_ratio(self):
        """`max` over a list holding None raises, so a calibration run that
        met one unmeasured utterance used to end in a traceback.
        """
        parsed = logparse.ParsedLog(
            utterances=[_utterance(index=1, ratio=None),
                        _utterance(index=2, ratio=0.83)],
            windows=[_window()])
        assert run.worst_ratio(parsed) == 0.83

    def test_a_run_that_measured_no_ratio_at_all_offers_none(self):
        parsed = logparse.ParsedLog(
            utterances=[_utterance(index=1, ratio=None)], windows=[_window()])
        assert run.worst_ratio(parsed) is None


def _google_utterance_line(utt: int) -> str:
    """One [load-diag] line as the Google provider really writes it.

    Built with the shipped UtteranceLoadMetrics rather than spelled out
    here, so the engine fields these tests rest on are the ones the
    provider writes: all five read n/a, because Google runs the recognizer
    on its own machines and there is no local engine time to report.
    """
    from shared_audio.diagnostics import UtteranceLoadMetrics

    metrics = UtteranceLoadMetrics(
        capture_stats=lambda: {'overflow_count': 0, 'status_flags': 0,
                               'drops': 0},
        stall_tracker=None)
    metrics.start()
    metrics.sample(4)
    return PREFIX + metrics.finish(utt, 'GOOGLE_FINAL')


def _google_run_log(*, redacted: bool = False, timed: tuple = (),
                    timed_ratio: str = '1.35') -> str:
    """The log slice a Google run leaves behind: six utterances, six finals.

    ``timed`` names the sentence positions whose utterance carries a real
    engine_ratio. Those get the Parakeet line, which is how a mixed slice
    reads -- a provider switch mid-run, or a Google line the log lost. Every
    other position gets Google's own line, which times no engine at all.

    ``redacted`` writes the transcripts through the shipped
    redact_transcript with transcript logging off, which is the release
    default and the reason those sentences come back LENGTH_MATCHES.
    """
    from shared_stt.redact import ENV_VAR, redact_transcript

    heard = [script.expected_transcript(s) for s in script.SENTENCES]
    if redacted:
        previous = os.environ.pop(ENV_VAR, None)
        try:
            heard = [redact_transcript(text) for text in heard]
        finally:
            if previous is not None:
                os.environ[ENV_VAR] = previous

    lines = [WINDOW_LINE]
    for position, text in enumerate(heard, start=1):
        utt = 200 + position
        if position in timed:
            lines.append(UTT_LINE.replace('utt=3', f'utt={utt}').replace(
                'engine_ratio=1.35', f'engine_ratio={timed_ratio}'))
        else:
            lines.append(_google_utterance_line(utt))
        lines.append(LOGIC_PREFIX + f"[FINAL] UTT-{utt}: '{text}'")
    return '\n'.join(lines)


def _google_report(**overrides) -> str:
    """The whole report a Google run produces, from its log text up.

    Nothing here is a stand-in: the lines come from the shipped metric
    writer, the parse and both judgements are the real ones, and the report
    is the one the operator pastes into the verdict template.
    """
    parsed = logparse.parse_log(_google_run_log(**overrides))
    sentences = judge.judge_sentences(parsed.transcripts)
    verdict = judge.judge_run(parsed, sentences, baseline_ratio=None)
    return report.render(
        machine='Ikon', logical_cores=20, provider='Google (cloud)',
        busy_percent=100.0, baseline_ratio=None, parsed=parsed,
        sentences=sentences, verdict=verdict, playback_underflows=None,
        rotated=False)


class TestARunThatTimedNothingReportsNoLateCount:
    """wh-stt-load-metrics.4.1.5. Google times no engine work at all, so
    every engine_ratio in its log reads n/a.

    Both aggregate late fields counted only the sentences whose ratio was
    above 1.0 and printed 0 when that list came out empty. An ordinary
    completed Google report therefore answered "Sentences correct but late:
    0" about a question nothing had asked, and 0 there is the reading that
    rules inference lag out -- one of the two failures this whole load test
    exists to tell apart. The undetermined verdict and the n/a on each
    per-utterance line do not make the aggregate true; the aggregate is
    what the operator pastes into the record.
    """

    def _value(self, rendered, label):
        return rendered.split(label)[1].splitlines()[0].strip()

    def test_the_google_round_trip_really_leaves_every_ratio_unmeasured(self):
        """The fixture, proved rather than assumed: six correct sentences
        and not one engine_ratio between them.
        """
        parsed = logparse.parse_log(_google_run_log())
        sentences = judge.judge_sentences(parsed.transcripts)

        assert len(parsed.utterances) == 6
        assert [u.engine_ratio for u in parsed.utterances] == [None] * 6
        assert [r.outcome for r in sentences.results] == [judge.CORRECT] * 6

    def test_a_google_run_never_says_zero_sentences_were_correct_but_late(
            self):
        value = self._value(_google_report(), 'Sentences correct but late:')

        assert value != '0'
        assert 'not measured' in value
        assert '6' in value

    def test_the_privacy_default_google_round_trip_gives_length_matches(self):
        """The other half of the fixture: under the release default the log
        holds counts, not words, so the same run answers in the second
        aggregate field instead of the first.
        """
        parsed = logparse.parse_log(_google_run_log(redacted=True))
        sentences = judge.judge_sentences(parsed.transcripts)

        assert [u.engine_ratio for u in parsed.utterances] == [None] * 6
        assert [r.outcome
                for r in sentences.results] == [judge.LENGTH_MATCHES] * 6

    def test_a_google_run_never_says_zero_of_the_right_length_were_late(self):
        value = self._value(_google_report(redacted=True),
                            'Sentences of the right length but late:')

        assert value != '0'
        assert 'not measured' in value
        assert '6' in value

    def test_a_partly_timed_run_says_how_many_sentences_it_could_not_time(
            self):
        """A count taken over two of six sentences is not wrong, but a
        reader who takes it for all six is. So the field says what it
        covers.
        """
        value = self._value(_google_report(timed=(1, 2)),
                            'Sentences correct but late:')

        assert value.startswith('2 (sentence 1, 2)')
        assert '4' in value
        assert 'no engine_ratio' in value

    def test_an_outcome_with_no_sentences_in_it_still_reads_zero(self):
        """Nothing went unmeasured here because nothing was eligible: every
        sentence came back visible, so the length-match field has no
        sentences to say anything about and 0 is the whole truth.
        """
        assert self._value(
            _google_report(),
            'Sentences of the right length but late:') == '0'

    def test_a_fully_timed_run_with_nothing_slow_still_reads_zero(self):
        """The measured zero the field was always meant to carry, which the
        fix must not turn into a hedge.
        """
        value = self._value(
            _google_report(timed=(1, 2, 3, 4, 5, 6), timed_ratio='0.70'),
            'Sentences correct but late:')

        assert value == '0'
