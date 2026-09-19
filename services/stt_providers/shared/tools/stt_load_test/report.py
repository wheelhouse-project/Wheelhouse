"""Fill in the procedure's verdict template.

Pure: handed the parsed log, the judgement and the run's own observations,
it returns the text the operator used to write out by hand. The field names
are the ones under "The verdict template" in
docs/testing/stt-cpu-load-test-procedure.md, and a test reads them out of
that document so this file cannot fall behind it.

The document says a blank field is the one that will be argued about later,
so every field is answered -- including the ones whose honest answer is that
the number was never measured.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from . import judge, logparse

_WIDTH = 43

# engine_ratio above 1.0 is what the procedure's own table calls "words come
# out late but correct". The log timestamps cannot answer the same question:
# a forwarded provider line is stamped when the application received it
# rather than when the provider wrote it (wh-forwarded-log-time-order), and
# under this test's load that delay is part of what is being measured.
_LATE_RATIO = 1.0


def _field(label: str, value: str) -> str:
    """``label`` at a fixed column, with ``value`` starting there.

    Padding is all the column does, so a label already at or past it used
    to receive none and hand its value to the reader with no gap between
    them. Every fixed label in this report is short enough for that never
    to show; the one label built at run time -- "Under-load per-utterance
    lines: N of M sentences measured" -- is not, and the render that most
    needs reading is the one with no per-utterance evidence, where the
    heading and the sentence explaining the absence arrived as a single
    word (finding wh-load-test-script.1.37).
    """
    if len(label) >= _WIDTH:
        return f'{label} {value}'
    return f'{label:<{_WIDTH}}{value}'


def _utterance_line(metrics: logparse.UtteranceMetrics) -> str:
    def shown(value):
        return logparse.NOT_REPORTED if value is None else value

    def figure(value, spec):
        """A number in its usual format, or n/a for one nobody measured.

        Every engine field is None on a Google line, and a format spec
        applied to None raises TypeError, so this used to be the point at
        which the report could not print the run it was asked to summarise
        (wh-stt-load-metrics.4).
        """
        return (logparse.NOT_REPORTED if value is None
                else format(value, spec))

    return (f'  utt={metrics.utt} kind={metrics.kind} '
            f'overflow={shown(metrics.overflow)} '
            f'status_flags={shown(metrics.status_flags)} '
            f'drops={shown(metrics.drops)} q_max={metrics.q_max} '
            f'q_mean={metrics.q_mean:.1f} '
            f'stalls={shown(metrics.stalls)} '
            f'stall_max_ms={figure(metrics.stall_max_ms, ".1f")} '
            f'engine_calls={shown(metrics.engine_calls)} '
            f'engine_ms_total={figure(metrics.engine_ms_total, ".1f")} '
            f'engine_ms_max={figure(metrics.engine_ms_max, ".1f")} '
            f'audio_ms={figure(metrics.audio_ms, ".0f")} '
            f'engine_ratio={figure(metrics.engine_ratio, ".2f")}')


def _window_line(window: logparse.Window) -> str:
    def shown(value):
        return logparse.NOT_REPORTED if value is None else value

    marker = '' if window.capture_available else 'capture=unavailable '
    gap = (logparse.NOT_REPORTED if window.max_frame_gap_ms is None
           else f'{window.max_frame_gap_ms:.0f}')
    return (f'  window={window.window_s:.0f}s {marker}'
            f'q_now={shown(window.q_now)} q_max={shown(window.q_max)} '
            f'drops={shown(window.drops)} overflow={shown(window.overflow)} '
            f'status_flags={shown(window.status_flags)} '
            f'stalls={window.stalls} max_gap_ms={window.max_gap_ms:.0f} '
            f'max_frame_gap_ms={gap}')


def _block(label: str, lines: Sequence[str], empty: str) -> list[str]:
    if not lines:
        return [_field(label, empty)]
    return [label] + list(lines)


@dataclass(frozen=True)
class _Lateness:
    """One outcome's lateness: what was timed, and what was not.

    ``late`` holds the sentences measured slow, ``unmeasured`` the eligible
    sentences whose utterance reported no engine_ratio at all, and
    ``eligible`` how many sentences the outcome covered in the first place.

    The last two are here because ``late`` alone cannot tell three
    different runs apart: one where nothing came back slow, one where the
    outcome held no sentences, and one where nothing was ever timed. All
    three used to print the same 0 (wh-stt-load-metrics.4.1.5).
    """
    late: tuple[judge.SentenceResult, ...]
    unmeasured: tuple[judge.SentenceResult, ...]
    eligible: int


def _late_sentences(sentences: judge.SentenceOutcomes,
                    parsed: logparse.ParsedLog,
                    outcomes) -> _Lateness:
    """The sentences of one outcome that came back slow, and the untimed.

    A sentence counts as late when the utterance that carried it spent
    longer inside the recognizer than the audio it was given lasted. The
    join is on utt=N, which is the only thing tying a sentence to its own
    numbers.

    ``outcomes`` is what the caller is willing to name. This used to skip
    only WRONG and MISSING, which put LENGTH_MATCHES under a field headed
    "correct". Under the transcript-privacy default that is every sentence
    in the run, so the first field an operator reads contradicted the rule
    the tool is built on: equal counts are a length match, never correct.

    A sentence whose utterance carried no engine_ratio is neither late nor
    on time: nothing timed it. Google runs the recognizer on its own
    machines and writes n/a in all five engine fields, so on a Google run
    that is every sentence in the run. Dropping those silently is what let
    the report answer a question it had never asked; they are returned
    beside the late ones so the caller can say so.
    """
    ratios = {m.utt: m.engine_ratio for m in parsed.utterances}
    late = []
    unmeasured = []
    eligible = 0
    for result in sentences.results:
        if result.outcome not in outcomes:
            continue
        eligible += 1
        ratio = ratios.get(result.utt)
        if ratio is None:
            unmeasured.append(result)
        elif ratio > _LATE_RATIO:
            late.append(result)
    return _Lateness(late=tuple(late), unmeasured=tuple(unmeasured),
                     eligible=eligible)


def render(*, machine: str, logical_cores: int, provider: str,
           busy_percent: Optional[float], baseline_ratio: Optional[float],
           parsed: logparse.ParsedLog, sentences: judge.SentenceOutcomes,
           verdict: judge.RunVerdict,
           playback_underflows: Optional[int], rotated: bool) -> str:
    """The completed template, ready to paste into a bd comment.

    ``rotated`` has no default on purpose. It is the one field here that
    no caller can work out from the parsed log -- a rotation is a
    property of the read, not of the text it returned -- so a default
    would let a caller that never learned the answer report a whole run.
    Required, the interpreter asks the question for us.
    """
    bad = [r for r in sentences.results
           if r.outcome in (judge.WRONG, judge.MISSING)]
    late = _late_sentences(sentences, parsed, (judge.CORRECT,))
    late_length = _late_sentences(sentences, parsed, (judge.LENGTH_MATCHES,))

    def late_field(lateness: _Lateness, note: str = '') -> str:
        """One aggregate late count, or the honest answer in its place.

        Both late fields are written here rather than at their two call
        sites, so neither can be left saying 0 while the other is fixed.

        A count of 0 means one of two very different things, and the reader
        cannot tell them apart from the number: no sentence came back slow,
        or no sentence was ever timed. Google measures no engine time at
        all, so on a Google run it is always the second, and 0 there reads
        as "the recognizer kept up" -- the reading that rules inference lag
        out, which is one of the two failures this test exists to tell
        apart (wh-stt-load-metrics.4.1.5).

        An outcome holding no sentences at all is the one case where 0 is
        the whole truth, and it keeps it.
        """
        unmeasured = len(lateness.unmeasured)
        timed = lateness.eligible - unmeasured
        if lateness.eligible and not timed:
            return (f'not measured -- none of the {lateness.eligible} '
                    'sentences carried an engine_ratio, so how late they '
                    'were is unknown')
        if not lateness.late:
            counted = '0'
        else:
            counted = (f'{len(lateness.late)} (sentence '
                       + ', '.join(str(r.index) for r in lateness.late)
                       + note + ')')
        if not unmeasured:
            return counted
        return (f'{counted}; measured for {timed} of the '
                f'{lateness.eligible} sentences -- the other {unmeasured} '
                'carried no engine_ratio')

    lines = [
        'STT under CPU load -- completed verdict template',
        'Generated by tools/stt_load_test. The reading guide for every field '
        'below is',
        'docs/testing/stt-cpu-load-test-procedure.md.',
        '',
        _field('Machine, logical cores:', f'{machine}, {logical_cores}'),
        _field('Provider and mode:', provider),
        _field('Measured busy percentage under load:',
               'not measured (baseline run)' if busy_percent is None
               else f'{busy_percent:.1f}%'),
        _field('Playback underflows:',
               'not measured' if playback_underflows is None
               else str(playback_underflows)),
        _field('Baseline (idle) per-utterance line:',
               'no baseline run recorded' if baseline_ratio is None
               else f'engine_ratio {baseline_ratio:.2f}'),
    ]

    # The heading used to read "all six" whatever the real count was, which
    # is a false statement in a report whose whole purpose is to be read
    # later by somebody who was not there.
    measured = {m.utt for m in parsed.utterances}
    counted = sum(1 for r in sentences.results
                  if r.utt is not None and r.utt in measured)
    lines += _block(f'Under-load per-utterance lines: {counted} of '
                    f'{len(sentences.results)} sentences measured',
                    [_utterance_line(m) for m in parsed.utterances],
                    'no per-utterance line was written')
    lines += _block('Under-load window lines:',
                    [_window_line(w) for w in parsed.windows],
                    'none (log_load_diagnostics was off, or the run was '
                    'shorter than one window)')

    lines.append(_field(
        'Sentences wrong or missing:',
        'none' if not bad
        else ', '.join(f'{r.index} ({r.outcome})' for r in bad)))
    lines.append(_field(
        'Sentences correct but late:',
        late_field(late)))
    lines.append(_field(
        'Sentences of the right length but late:',
        late_field(late_length, '; their words are not in the log')))
    lines.append(f'VERDICT: {verdict.verdict}')
    # The widest of the three statements about how much of this run the
    # verdict rests on, so it is read first. The run prints this before the
    # report as well, but the procedure tells the operator to copy the
    # block into the verdict template -- so anything outside the block is
    # lost to every later reader (finding wh-load-test-script.1.22, and the
    # same weakness .1.18 fixed for the late-sentence field).
    if rotated:
        lines.append(
            '  the log rotated during this run, so the numbers above rest '
            'on part of it and may include lines from before the playback '
            'started')
    # Beside the verdict, above the evidence: unmeasured windows weaken the
    # callback-loss against inference-lag distinction the evidence below
    # draws, so the reader has to see the size of the gap before reading it.
    if verdict.windows_missing_capture:
        lines.append(
            f'  capture readings missing for '
            f'{verdict.windows_missing_capture} of {verdict.windows_total} '
            f'windows')
    # The same statement at the sentence resolution. Named, not counted: the
    # reader matches these numbers against the per-sentence block below to
    # see which answers rest on numbers and which do not.
    if verdict.sentences_missing_metrics:
        lines.append(
            f'  per-utterance measurements missing for '
            f'{len(verdict.sentences_missing_metrics)} of '
            f'{verdict.sentences_total} sentences: '
            + ', '.join(str(i) for i in verdict.sentences_missing_metrics))
    lines.append('Evidence for the verdict:')
    lines += [f'  - {line}' for line in verdict.evidence]

    lines.append('')
    lines.append('Per sentence:')
    for result in sentences.results:
        lines.append(f'  {result.index}. {result.outcome} -- {result.detail}')
        lines.append(f'     said: {result.spoken}')
    if sentences.extras:
        lines.append('  Utterances that carried none of the six sentences:')
        lines += [f'     {text}' for text in sentences.extras]

    if any(r.outcome == judge.LENGTH_MATCHES for r in sentences.results):
        lines.append('')
        lines.append(
            '  "length matches" is not "correct". Under the '
            'transcript-privacy default the')
        lines.append(
            '  log holds only a character and word count, so a sentence of '
            'the right length')
        lines.append(
            '  with the wrong words reads the same as a correct one. Set '
            'LOG_TRANSCRIPTS')
        lines.append(
            '  = true in services/wheelhouse/config.toml, or pass '
            '--log-transcripts, to')
        lines.append(
            '  compare the words themselves -- both of which write dictated '
            'text to disk.')

    return '\n'.join(lines) + '\n'
