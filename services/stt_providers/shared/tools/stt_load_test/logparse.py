"""Turn the provider's log lines back into numbers.

Pure: it is handed text and returns dataclasses. The formats it reads are
the format strings in shared_stt/audio_processor.py (the per-utterance
[load-diag] line and the [vad_utt_stats] line), shared_audio/diagnostics.py
(the periodic window line and the capture-outage event), and
integrations/websocket_manager.py (wheelhouse's own [FINAL] line).

The transcript comes from that last one. [vad_utt_stats] cannot reach
wheelhouse.log at all: audio_processor.py:50-64 leaves it on the package
logger on purpose, and a provider silences that package wholesale, so a
record on it fires no handler and its propagation stops at the silenced
parent. Measured on a live run: 0 of those lines against 117 [load-diag]
lines. [FINAL] is written in wheelhouse's own process, carries the same
utterance number, and does not depend on which provider is running. The
[vad_utt_stats] reader stays for a log captured on the provider side, where
that line does appear.

Two rules run through the whole module.

`n/a` is not zero. A provider that does not count overflows at all writes
`n/a` there, and reading that as 0 would let the report say "no frames were
lost" about a counter nobody kept. Every such field is None here, and every
reader has to decide what to do about not knowing.

Lines are matched by their exact tag at a fixed position, not by searching
for a key anywhere in the text. wheelhouse.log carries every subsystem in
the application, and a command name or a quoted string can contain anything.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from typing import Optional

# The tag as the provider writes it, anchored so a mention of the tag inside
# another component's message cannot be read as a measurement. The app's own
# handler prefixes a timestamp and a level, so the tag is not at the start of
# the line; requiring a space in front of it is what keeps
# "[vad_utt_stats_something_else]" from matching.
_UTTERANCE = re.compile(r'(?:^|\s)\[load-diag\] (utt=.*)$')
_WINDOW = re.compile(r'(?:^|\s)\[load-diag\] (window=.*)$')
_OUTAGE = re.compile(r'(?:^|\s)\[load-diag\] capture-outage (.*)$')
_TRANSCRIPT = re.compile(r'(?:^|\s)\[vad_utt_stats\] (.*)$')
_FINAL = re.compile(r'(?:^|\s)\[FINAL\] UTT-(\d+): (.*)$')

# websocket_manager.py appends this after the closing quote when the provider
# names a reason. Trimming it before the quotes are unwrapped is what keeps a
# transcript ending in those same words from being cut short: inside the
# quotes the line does not end in the bracket this requires.
_FINAL_REASON = re.compile(r'\s+\(final_reason=([^()]*)\)$')

# Which line a transcript was read from. The [FINAL] text is what wheelhouse
# acted on, and [vad_utt_stats] is written before the final is sent, so where
# a log holds both for one utterance the [FINAL] one is the one to keep.
FINAL_SOURCE = 'final'
VAD_SOURCE = 'vad_utt_stats'

# text= holds a repr and is last on the line, so it is read to the end of the
# line. Every other field is a run of non-space characters.
_TEXT_FIELD = re.compile(r'(?:^|\s)text=(.*)$')

NOT_REPORTED = 'n/a'


@dataclass(frozen=True)
class UtteranceMetrics:
    """One [load-diag] utt= line: what this utterance cost.

    Two providers write this line and they measure different things, so
    every field a provider can be unable to measure is Optional and None
    means "not measured here" (wh-stt-load-metrics.4):

    - The five engine fields are None on a Google line. Google runs the
      recognizer on its own machines, so there is no local engine time; a
      0 there would read as "the recognizer kept up", which nothing
      measured.
    - The two stall fields are None on a Parakeet line, and on every line
      written before they existed. The Parakeet loop reports its stalls in
      the periodic window line instead.

    ``stalls`` and ``stall_max_ms`` are last with defaults rather than
    beside q_n where the log writes them, because a dataclass field with a
    default cannot precede one without.
    """
    utt: int
    kind: str
    overflow: Optional[int]
    status_flags: Optional[int]
    drops: Optional[int]
    q_max: int
    q_mean: float
    q_n: int
    engine_calls: Optional[int]
    engine_ms_total: Optional[float]
    engine_ms_max: Optional[float]
    audio_ms: Optional[float]
    engine_ratio: Optional[float]
    stalls: Optional[int] = None
    stall_max_ms: Optional[float] = None


@dataclass(frozen=True)
class Transcript:
    """One transcript line: the text, and which utterance wrote it.

    ``text`` is what the log holds, which under the release default is the
    placeholder from shared_stt/redact.py rather than the words. judge.py
    decides what can be concluded from that.

    ``kind`` is the endpoint kind on a [vad_utt_stats] line and the final
    reason on a [FINAL] line, which is absent more often than not; nothing
    reads it today, and it is kept because it is the only other fact either
    line carries about how the utterance ended.
    """
    utt: int
    kind: Optional[str]
    text: str
    source: str = ''


@dataclass(frozen=True)
class Window:
    """One periodic [load-diag] window= line."""
    window_s: float
    capture_available: bool
    q_now: Optional[int]
    q_max: Optional[int]
    drops: Optional[int]
    overflow: Optional[int]
    status_flags: Optional[int]
    stalls: int
    max_gap_ms: float
    max_frame_gap_ms: Optional[float]


@dataclass(frozen=True)
class Outage:
    """One [load-diag] capture-outage event, however many windows it spans."""
    start_s_ago: float
    end_s_ago: float
    duration_s: float
    windows: int


@dataclass
class ParsedLog:
    """Everything this run's log holds, in the order it was written."""
    utterances: list[UtteranceMetrics] = field(default_factory=list)
    transcripts: list[Transcript] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)


def _fields(body: str) -> dict[str, str]:
    """The key=value pairs in ``body``, as strings, in one flat mapping."""
    pairs = {}
    for token in body.split():
        key, sep, value = token.partition('=')
        if sep:
            pairs[key] = value
    return pairs


def _optional_int(raw: Optional[str]) -> Optional[int]:
    """The integer, or None for a counter this provider does not keep."""
    if raw is None or raw == NOT_REPORTED:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _optional_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or raw == NOT_REPORTED:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _seconds(raw: Optional[str]) -> Optional[float]:
    """window=10s -- the value carries its unit."""
    if raw is None:
        return None
    return _optional_float(raw.removesuffix('s'))


def _read_utterance(body: str) -> Optional[UtteranceMetrics]:
    values = _fields(body)
    try:
        metrics = UtteranceMetrics(
            utt=int(values['utt']),
            kind=values['kind'],
            overflow=_optional_int(values.get('overflow')),
            status_flags=_optional_int(values.get('status_flags')),
            drops=_optional_int(values.get('drops')),
            q_max=int(values['q_max']),
            q_mean=float(values['q_mean']),
            q_n=int(values['q_n']),
            # Optional for the reason in the class docstring: a Google line
            # writes n/a in all five. That also means a corrupt value in one
            # of them now costs that field rather than the whole line, which
            # is the treatment the capture fields above already get. The
            # fields whose absence really does mean a truncated line -- utt,
            # kind and the three queue fields -- are still read strictly.
            engine_calls=_optional_int(values.get('engine_calls')),
            engine_ms_total=_optional_float(values.get('engine_ms_total')),
            engine_ms_max=_optional_float(values.get('engine_ms_max')),
            audio_ms=_optional_float(values.get('audio_ms')),
            engine_ratio=_optional_float(values.get('engine_ratio')),
            stalls=_optional_int(values.get('stalls')),
            stall_max_ms=_optional_float(values.get('stall_max_ms')),
        )
    except (KeyError, ValueError):
        # A line the provider truncated, or a format that has moved on. A
        # half-read measurement is worse than a missing one: the report would
        # present invented numbers as measured.
        return None
    if metrics.engine_ratio is not None and not math.isfinite(
            metrics.engine_ratio):
        # The same rule, for a line that is complete but carries a value
        # that is not a measurement. ``float()`` accepts "nan", "inf" and
        # "-inf", so those spellings reached judge_run, where every
        # comparison against NaN is false: the ladder fell through to
        # `neither` and its evidence called the NaN ratio within 1.5 times
        # the idle reading. engine_ratio is the one parsed float that
        # decides the verdict AND crosses into the saved baseline, so it is
        # the one refused here (finding wh-load-test-script.1.25).
        # None is a different case and is kept: it says the provider never
        # measured a ratio, which judge_run reads as "not measured" rather
        # than as a number (wh-stt-load-metrics.4).
        return None
    return metrics


def _read_window(body: str) -> Optional[Window]:
    values = _fields(body)
    window_s = _seconds(values.get('window'))
    if window_s is None:
        return None
    stalls = _optional_int(values.get('stalls'))
    max_gap_ms = _optional_float(values.get('max_gap_ms'))
    if stalls is None or max_gap_ms is None:
        return None
    return Window(
        window_s=window_s,
        capture_available=values.get('capture') != 'unavailable',
        q_now=_optional_int(values.get('q_now')),
        q_max=_optional_int(values.get('q_max')),
        drops=_optional_int(values.get('drops')),
        overflow=_optional_int(values.get('overflow')),
        status_flags=_optional_int(values.get('status_flags')),
        stalls=stalls,
        max_gap_ms=max_gap_ms,
        max_frame_gap_ms=_optional_float(values.get('max_frame_gap_ms')),
    )


def _read_outage(body: str) -> Optional[Outage]:
    values = _fields(body)
    try:
        return Outage(
            start_s_ago=float(values['start_s_ago']),
            end_s_ago=float(values['end_s_ago']),
            duration_s=float(values['duration_s']),
            windows=int(values['windows']),
        )
    except (KeyError, ValueError):
        return None


def _read_transcript(body: str) -> Optional[Transcript]:
    values = _fields(body)
    if 'utt' not in values or 'kind' not in values:
        return None
    match = _TEXT_FIELD.search(body)
    if match is None:
        return None
    try:
        text = ast.literal_eval(match.group(1).strip())
    except (ValueError, SyntaxError):
        # The field is a repr the provider wrote with %r. Anything else is a
        # line this parser does not understand, and guessing at the quoting
        # would put a mangled transcript in front of the operator.
        return None
    if not isinstance(text, str):
        return None
    try:
        return Transcript(utt=int(values['utt']), kind=values['kind'],
                          text=text, source=VAD_SOURCE)
    except ValueError:
        return None


def _read_final(number: str, body: str) -> Optional[Transcript]:
    """One [FINAL] UTT-n line: the utterance number and the transcript."""
    kind = None
    match = _FINAL_REASON.search(body)
    if match is not None:
        kind = match.group(1)
        body = body[:match.start()]
    body = body.strip()
    # The quotes around the text are written by an f-string, not by %r, so
    # what is inside them is not escaped and must not be read as a Python
    # literal: an apostrophe in an ordinary sentence would fail that parse,
    # and the words a person is most likely to say are the ones that carry
    # one. Unwrapping the quotes by hand is the only reading that survives.
    if len(body) < 2 or not (body.startswith("'") and body.endswith("'")):
        return None
    try:
        return Transcript(utt=int(number), kind=kind, text=body[1:-1],
                          source=FINAL_SOURCE)
    except ValueError:
        return None


def _keep(into: dict, record: Transcript) -> None:
    """Keep one transcript per utterance, preferring the [FINAL] line.

    Two records for one utterance would shift every later sentence in
    judge.py's alignment by one and report the tail of the script missing.
    Replacing in place rather than appending again keeps the order the log
    was written in, which is the order that alignment reads.
    """
    existing = into.get(record.utt)
    if existing is None or (record.source == FINAL_SOURCE
                            and existing.source != FINAL_SOURCE):
        into[record.utt] = record


def parse_log(text: str) -> ParsedLog:
    """Every measurement line in ``text``, in the order it was written."""
    parsed = ParsedLog()
    transcripts: dict[int, Transcript] = {}
    for line in text.splitlines():
        match = _UTTERANCE.search(line)
        if match is not None:
            record = _read_utterance(match.group(1))
            if record is not None:
                parsed.utterances.append(record)
            continue
        match = _WINDOW.search(line)
        if match is not None:
            window = _read_window(match.group(1))
            if window is not None:
                parsed.windows.append(window)
            continue
        match = _OUTAGE.search(line)
        if match is not None:
            outage = _read_outage(match.group(1))
            if outage is not None:
                parsed.outages.append(outage)
            continue
        match = _FINAL.search(line)
        if match is not None:
            record = _read_final(match.group(1), match.group(2))
            if record is not None:
                _keep(transcripts, record)
            continue
        match = _TRANSCRIPT.search(line)
        if match is not None:
            transcript = _read_transcript(match.group(1))
            if transcript is not None:
                _keep(transcripts, transcript)
    parsed.transcripts.extend(transcripts.values())
    return parsed
