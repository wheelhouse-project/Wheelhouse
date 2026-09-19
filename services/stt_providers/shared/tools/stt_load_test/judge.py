"""Judge the run: each sentence, then the whole.

Pure: it is handed parsed log records and returns verdicts. Every rule here
comes from docs/testing/stt-cpu-load-test-procedure.md, which is the reading
guide an operator followed by hand before this script existed.

Two limits are stated rather than hidden, because a verdict that claims more
than it measured is worse than no verdict.

The transcript-privacy default (LOG_TRANSCRIPTS in
services/wheelhouse/config.toml, exported by launcher.py as
WHEELHOUSE_LOG_TRANSCRIPTS) leaves the log holding a placeholder with only
the character and word counts. A sentence judged from that is reported as
LENGTH_MATCHES, never CORRECT.

`n/a` is not zero. A provider that keeps no overflow counter -- the WinRT
capture path is one -- cannot support the `neither` verdict, because
`neither` rests on all three numbers reading clean.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from . import logparse
from .script import SENTENCES, expected_transcript

# Per-sentence outcomes.
CORRECT = 'correct'
LENGTH_MATCHES = 'length matches'
WRONG = 'wrong'
MISSING = 'missing'

# Run verdicts. The first five are the boxes the procedure's template offers.
CALLBACK_LOSS = 'callback loss'
CONSUMER_LOSS = 'consumer-side loss'
INFERENCE_LAG = 'inference lag'
MORE_THAN_ONE = 'more than one'
NEITHER = 'neither'
# The two the template has no box for. NO_FAILURE is a run in which nothing
# went wrong, which `neither` would misreport as an unexplained failure.
# UNDETERMINED is a run that cannot support any verdict, because a number a
# verdict rests on was never measured.
NO_FAILURE = 'no failure observed'
UNDETERMINED = 'undetermined'
CAPTURE_UNAVAILABLE = 'capture unavailable'

# How far above the idle reading engine_ratio may sit and still count as
# "near its idle value". The procedure gives no number, so this one is the
# script's own and is stated in the report beside the verdict it produced.
NEITHER_RATIO_MARGIN = 1.5

# The placeholder shared_stt/redact.py writes when transcript logging is off.
# A test builds the string with that shipped function and reads it back
# through here, so this pattern cannot drift away from what it parses.
_REDACTED = re.compile(r'\A<redacted: (\d+) chars, (\d+) words>\Z')


@dataclass(frozen=True)
class SentenceResult:
    """What was concluded about one sentence, and from what.

    ``utt`` is the utterance number the provider gave this sentence, which
    is what ties it to its own [load-diag] utt= line. None when no utterance
    carried the sentence at all.
    """
    index: int
    spoken: str
    expected: str
    outcome: str
    detail: str
    utt: Optional[int] = None


@dataclass(frozen=True)
class SentenceOutcomes:
    """The six sentences, plus anything else that became an utterance."""
    results: tuple[SentenceResult, ...]
    extras: tuple[str, ...]

    @property
    def any_bad(self) -> bool:
        return any(r.outcome in (WRONG, MISSING) for r in self.results)


@dataclass(frozen=True)
class RunVerdict:
    """The verdict, and the specific fields and values behind it.

    ``windows_missing_capture`` and ``windows_total`` carry the size of the
    measurement gap. They are counts rather than a sentence because the
    report decides the wording, and because the reader needs the proportion:
    unmeasured windows weaken the callback-loss against inference-lag
    distinction, so how much of the run went unmeasured has to be visible in
    the same glance as the verdict it weakens.

    ``sentences_missing_metrics`` is the second half of the same statement,
    at the resolution the procedure cares about. Each of the six sentences
    is deliberately its own utterance, so a late or missing [load-diag] utt=
    line hides one sentence's engine_ratio while the others carry numbers.
    The numbers are the sentence positions the report prints elsewhere, so
    a reader can match this line to the per-sentence block below it.
    """
    verdict: str
    evidence: tuple[str, ...]
    windows_missing_capture: int = 0
    windows_total: int = 0
    sentences_missing_metrics: tuple[int, ...] = ()
    sentences_total: int = 0


def redacted_counts(text: str) -> Optional[tuple[int, int]]:
    """(characters, words) when ``text`` is a redaction placeholder.

    None means the log holds the transcript itself, which happens only when
    the operator has turned transcript logging on.
    """
    match = _REDACTED.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _matches(heard: str, expected: str) -> bool:
    """True when ``heard`` is what a clean run would have written.

    Under the privacy default this compares the two counts the placeholder
    carries, which is the most the log allows. Equal counts do not prove
    equal words -- the caller reports that as LENGTH_MATCHES rather than
    CORRECT.
    """
    counts = redacted_counts(heard)
    if counts is not None:
        return counts == (len(expected), len(expected.split()))
    if heard.strip().casefold() == expected.strip().casefold():
        return True
    return _same_words(heard.strip().casefold().split(),
                       expected.strip().casefold().split())


def _same_words(heard: Sequence[str], expected: Sequence[str]) -> bool:
    """True when the two differ only in where words are joined.

    The recognizer writes some compounds as one word: the live run of
    2026-08-30 produced "seashells" and "seashore" where the fixed script has
    "sea shells" and "sea shore". Nothing in the script can prevent that, and
    it says nothing about load, which is the only thing this test measures.

    Exactly one liberty is taken, in both directions: two adjacent words on
    one side may equal one word on the other. There is no edit distance and
    no substitution, so a wrong word, a missing word, or an extra word --
    the failures this test exists to catch -- still reads as wrong.

    The table asks which pairs of positions are reachable from the start
    under those three moves. Walking greedily from the left would commit to
    a join that a later word contradicts.
    """
    reachable = [[False] * (len(expected) + 1) for _ in range(len(heard) + 1)]
    reachable[0][0] = True
    for j in range(len(heard) + 1):
        for i in range(len(expected) + 1):
            if not reachable[j][i]:
                continue
            if j < len(heard) and i < len(expected):
                if heard[j] == expected[i]:
                    reachable[j + 1][i + 1] = True
                if (i + 1 < len(expected)
                        and heard[j] == expected[i] + expected[i + 1]):
                    reachable[j + 1][i + 2] = True
                if (j + 1 < len(heard)
                        and heard[j] + heard[j + 1] == expected[i]):
                    reachable[j + 2][i + 1] = True
    return reachable[len(heard)][len(expected)]


def _align(transcripts: Sequence[logparse.Transcript],
           expected: Sequence[str]) -> tuple[list[Optional[int]], list[int]]:
    """Pair each sentence with the utterance that carries it.

    Pairing by position looks simpler and is wrong: one extra utterance --
    a cough, or an endpoint that split a sentence in two -- would shift
    every later sentence onto the previous sentence's transcript and report
    one interruption as five failures.

    So the sentences are matched in order against the first utterance ahead
    that carries them, which steps over anything else that made a sound.
    Whatever is left is then handed to whichever sentences found no match,
    in order, because a transcript that matches nothing is still the best
    evidence available about the sentence that has none.

    Returns the transcript index for each sentence (None when it has none)
    and the indices of the transcripts nothing claimed.
    """
    paired: list[Optional[int]] = [None] * len(expected)
    used: set[int] = set()
    cursor = 0
    for position, text in enumerate(expected):
        for index in range(cursor, len(transcripts)):
            if index in used:
                continue
            if _matches(transcripts[index].text, text):
                paired[position] = index
                used.add(index)
                cursor = index + 1
                break

    spare = [i for i in range(len(transcripts)) if i not in used]
    for position in range(len(expected)):
        if paired[position] is not None:
            continue
        # Only from between the neighbours. Handing a sentence any leftover
        # transcript in chronological order used to rebuild an order that
        # never happened: with sentence 2 spoken before sentence 1, the
        # forward pass paired sentence 1 with the second utterance and this
        # pass then gave sentence 2 the first one, so six sentences read
        # correct and the run was clean. Counted over the six exact
        # transcripts, 131 of the 719 non-identity orders were reported that
        # way. Keeping the pairing increasing leaves the garbled-sentence
        # case this pass exists for untouched -- the leftover utterance
        # still sits between its neighbours -- and makes the reordered case
        # read as one missing sentence and one extra, which is the truth.
        low = max((paired[p] for p in range(position)
                   if paired[p] is not None), default=-1)
        high = min((paired[p] for p in range(position + 1, len(expected))
                    if paired[p] is not None), default=len(transcripts))
        index = next((i for i in spare if low < i < high), None)
        if index is None:
            continue
        spare.remove(index)
        paired[position] = index
        used.add(index)
    return paired, spare


def _describe(heard: str, expected: str) -> str:
    """Why a sentence was judged wrong, in the terms the log allows."""
    counts = redacted_counts(heard)
    if counts is None:
        return f'heard {heard!r}, expected {expected!r}'
    return (f'heard {counts[0]} chars, {counts[1]} words; expected '
            f'{len(expected)} chars, {len(expected.split())} words')


def judge_sentences(
        transcripts: Sequence[logparse.Transcript],
        sentences: Sequence[str] = SENTENCES) -> SentenceOutcomes:
    """Judge every sentence in the fixed script against what was heard."""
    expected = [expected_transcript(s) for s in sentences]
    paired, spare = _align(transcripts, expected)

    results = []
    for position, sentence in enumerate(sentences):
        want = expected[position]
        index = paired[position]
        if index is None:
            outcome, detail = MISSING, 'no utterance carried this sentence'
        else:
            heard = transcripts[index].text
            if not _matches(heard, want):
                outcome, detail = WRONG, _describe(heard, want)
            elif redacted_counts(heard) is None:
                outcome, detail = CORRECT, 'the transcript matches'
            else:
                outcome, detail = LENGTH_MATCHES, (
                    'the transcript is the right length; its words are not '
                    'in the log under the transcript-privacy default')
        results.append(SentenceResult(
            index=position + 1, spoken=sentence, expected=want,
            outcome=outcome, detail=detail,
            utt=None if index is None else transcripts[index].utt))
    return SentenceOutcomes(results=tuple(results),
                            extras=tuple(transcripts[i].text for i in spare))


def _total(values: Sequence[Optional[int]]) -> tuple[Optional[int], bool]:
    """The sum of what was measured, and whether anything was not.

    A counter reading `n/a` contributes nothing to the sum and sets the flag:
    the run then knows it is looking at a partial count, which is what stops
    it from concluding that nothing happened.
    """
    measured = [v for v in values if v is not None]
    unmeasured = len(measured) != len(values)
    if not measured:
        return None, True
    return sum(measured), unmeasured


def _capture_total(utterance_values: Sequence[Optional[int]],
                   window_values: Sequence[Optional[int]]
                   ) -> tuple[Optional[int], bool]:
    """One capture counter's total for the run, counting each event once.

    The two lines report deltas of the SAME counter over overlapping spans:
    an utterance happens inside a window, so adding the two lists charges
    every overflow and every drop twice, and the operator pastes a number
    about twice the real count into the record.

    Each list on its own is a lower bound on the events in this run. The
    utterance lines miss whatever happened between utterances; the window
    lines miss whatever fell outside the windows the log slice caught, which
    is why preferring them outright is wrong -- a slice that starts mid-window
    would report zero over an utterance line saying forty-eight. The larger of
    the two sums is therefore the closest lower bound available, and it cannot
    count an event twice.

    The partial flag reads both lists. `n/a` anywhere means some of this run
    was not counted, and a run that was not fully counted cannot claim a clean
    reading, whichever line stopped counting.
    """
    def side(values: Sequence[Optional[int]]) -> tuple[Optional[int], bool]:
        # A list with no lines in it was not a counter that stopped: the run
        # simply caught none of that kind of line. _total reports an empty
        # list as unmeasured, which would make every slice without a window
        # line undetermined.
        return (None, False) if not values else _total(values)

    from_utterances, utterances_partial = side(utterance_values)
    from_windows, windows_partial = side(window_values)
    measured = [v for v in (from_utterances, from_windows) if v is not None]
    if not measured:
        return None, True
    return max(measured), utterances_partial or windows_partial


def judge_run(parsed: logparse.ParsedLog, sentences: SentenceOutcomes,
              baseline_ratio: Optional[float]) -> RunVerdict:
    """The run's verdict, and the fields and values that produced it."""
    evidence: list[str] = []

    unavailable = [w for w in parsed.windows if not w.capture_available]
    # Which of the six sentences got their own numbers. Each sentence is
    # deliberately its own utterance, so one late [load-diag] utt= line
    # hides one sentence's engine_ratio while the rest carry numbers. A
    # sentence no utterance carried at all has no utt to join on, and is
    # unmeasured for the same reason. Ruling of 2026-08-30, following the
    # capture-gap ruling: a run that measured some of its sentences keeps
    # its verdict, and the report names the ones it could not measure.
    measured = {u.utt for u in parsed.utterances}
    gap: dict = dict(
        windows_missing_capture=len(unavailable),
        windows_total=len(parsed.windows),
        sentences_missing_metrics=tuple(
            r.index for r in sentences.results
            if r.utt is None or r.utt not in measured),
        sentences_total=len(sentences.results))
    # Refused only when NOTHING was measured. A run that lost some windows is
    # still the run this tool exists to measure: on the live run of
    # 2026-08-30, 13 of 83 windows read unavailable inside one five-minute
    # band while the other 70 and every per-utterance line carried real
    # numbers and every sentence was transcribed. Refusing that would refuse
    # the loaded runs and keep only the idle ones. The gap prints beside the
    # verdict instead, so the reader knows how much of it went uncounted.
    if unavailable and len(unavailable) == len(parsed.windows):
        evidence.append(
            f'every one of the {len(unavailable)} window line(s) read '
            'capture=unavailable, so no capture number in this run was taken')
        for outage in parsed.outages:
            evidence.append(
                f'capture-outage: {outage.duration_s:.0f}s across '
                f'{outage.windows} window(s)')
        # Not "fix the microphone": on the live run every sentence was
        # transcribed while windows read unavailable, so that advice named a
        # fault that was not there. _read_capture_stats returns nothing when
        # no reader is wired, when the reader raises, or when it returns
        # something other than a dict, and it reports which at debug level on
        # a logger the provider silences.
        evidence.append(
            "the provider's capture-stats reader returned nothing for those "
            'windows; its reason is logged at debug level and does not reach '
            'this log')
        return RunVerdict(CAPTURE_UNAVAILABLE, tuple(evidence), **gap)

    overflow, overflow_partial = _capture_total(
        [u.overflow for u in parsed.utterances],
        [w.overflow for w in parsed.windows])
    drops, drops_partial = _capture_total(
        [u.drops for u in parsed.utterances],
        [w.drops for w in parsed.windows])
    # None means the provider never measured a ratio -- every Google line,
    # and any Parakeet line whose engine field was corrupt. Dropping those
    # here is what makes worst_ratio None for a whole Google run, which the
    # ladder below already answers with UNDETERMINED rather than with a
    # clean reading (wh-stt-load-metrics.4).
    ratios = [u.engine_ratio for u in parsed.utterances
              if u.engine_ratio is not None]
    worst_ratio = max(ratios) if ratios else None

    evidence.append(f'overflow total {"n/a" if overflow is None else overflow}')
    evidence.append(f'drops total {"n/a" if drops is None else drops}')
    evidence.append(
        'worst engine_ratio '
        f'{"n/a" if worst_ratio is None else format(worst_ratio, ".2f")}')

    callback_loss = overflow is not None and overflow > 0
    inference_lag = worst_ratio is not None and worst_ratio > 1.0
    dropped = drops is not None and drops > 0
    # The procedure reads drops with engine_ratio beside it: the provider
    # drains the queue and runs the recognizer in one loop, so inference slow
    # enough to lag also stops the drain. Counting that as a second failure
    # would send the reader after a consumer-loop fix that changes nothing.
    consumer_loss = dropped and not inference_lag
    if dropped and inference_lag:
        evidence.append(
            'drops are read here as inference lag showing its second symptom, '
            'not as a separate consumer-side failure, because engine_ratio is '
            'above 1.0')

    named = [name for name, fired in (
        (CALLBACK_LOSS, callback_loss),
        (CONSUMER_LOSS, consumer_loss),
        (INFERENCE_LAG, inference_lag)) if fired]
    if len(named) > 1:
        evidence.append('more than one failure fired: ' + ', '.join(named))
        return RunVerdict(MORE_THAN_ONE, tuple(evidence), **gap)
    if named:
        return RunVerdict(named[0], tuple(evidence), **gap)

    # Nothing fired. What that means depends on whether the numbers were
    # good enough to mean anything.
    if overflow is None or overflow_partial:
        evidence.append(
            'overflow was not counted for part or all of this run, so a '
            'clean overflow reading cannot be claimed')
        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)
    if drops is None or drops_partial:
        evidence.append(
            'drops was not counted for part or all of this run, so a clean '
            'drops reading cannot be claimed')
        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)
    if worst_ratio is None:
        evidence.append('no per-utterance line carried an engine_ratio')
        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)
    if not sentences.any_bad:
        evidence.append('every sentence came back at the expected length or '
                        'better, and no counter moved')
        return RunVerdict(NO_FAILURE, tuple(evidence), **gap)
    if baseline_ratio is None:
        evidence.append(
            'the transcript was bad and no counter moved, but `neither` also '
            'needs engine_ratio near its IDLE value and no baseline run was '
            'given; run with --baseline first')
        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)
    if worst_ratio > baseline_ratio * NEITHER_RATIO_MARGIN:
        evidence.append(
            f'engine_ratio {worst_ratio:.2f} is more than '
            f'{NEITHER_RATIO_MARGIN} times the idle reading '
            f'{baseline_ratio:.2f} without passing 1.0, so it is raised but '
            'not lagging')
        return RunVerdict(UNDETERMINED, tuple(evidence), **gap)
    evidence.append(
        f'engine_ratio {worst_ratio:.2f} is within {NEITHER_RATIO_MARGIN} '
        f'times the idle reading {baseline_ratio:.2f}, overflow and drops '
        'are 0, and the transcript is still bad')
    return RunVerdict(NEITHER, tuple(evidence), **gap)
