"""Pure threshold arithmetic for the voice-calibration session (wh-7ou.7.2.1).

Implements Section 7 of
docs/superpowers/specs/2026-08-07-voice-calibration-design.md exactly.
The CalibrationController collects the samples and applies the result;
this module only does the arithmetic -- no I/O, no config reads.

The two settings judged here are the single-word rescue thresholds in the
Distil-Whisper provider's ``[engine]`` config section:

- ``single_word_min_probability`` (the word-probability floor)
- ``single_word_max_no_speech_prob`` (the no-speech ceiling)

Each setting is decided on its own data (per-setting write-or-keep,
design decision 4): a setting without clean supporting data keeps its
current value, expressed here as ``proposed=None``.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

# Spec Section 7 constants. Each rule gets its own name even where the
# numbers coincide, so the rules stay independently editable.
_FLOOR_MARGIN = 0.05
_FLOOR_MIN_WORD_SAMPLES = 12
_CEILING_MIN_NOISE_SAMPLES = 2
_NOISE_GAP_RATIO = 2.0
_SKIP_SAFETY_FACTOR = 2.0
_CEILING_SKIP_LOW = 0.01
_CEILING_SKIP_HIGH = 0.10


@dataclass(frozen=True)
class WordSample:
    """Measurement numbers from one accepted word capture.

    Both values come from the final's ``confidence`` block:
    ``min_word_probability`` is the lowest per-word decode probability in
    the capture; ``max_no_speech_prob`` the highest segment no-speech
    probability.
    """
    min_word_probability: Optional[float]
    max_no_speech_prob: Optional[float]


@dataclass(frozen=True)
class ThresholdDecision:
    """Write-or-keep outcome for one setting.

    ``proposed`` is the value to write, or None to keep ``current``.
    """
    current: float
    proposed: Optional[float]

    @property
    def keep(self) -> bool:
        return self.proposed is None


@dataclass(frozen=True)
class MeasuredRanges:
    """Raw extremes of the valid samples, for the Show-details view.

    A field is None when no valid sample of that kind was measured
    (for example the noise fields when the noise stage was skipped).
    """
    word_probability_min: Optional[float] = None
    word_probability_max: Optional[float] = None
    word_no_speech_min: Optional[float] = None
    word_no_speech_max: Optional[float] = None
    noise_no_speech_min: Optional[float] = None
    noise_no_speech_max: Optional[float] = None


@dataclass(frozen=True)
class CalibrationProposal:
    """Both per-setting decisions plus the measurements behind them."""
    min_probability: ThresholdDecision
    max_no_speech_prob: ThresholdDecision
    measured: MeasuredRanges


def _is_valid_probability(value) -> bool:
    """A usable measurement: a real number in [0, 1].

    bool is excluded explicitly (it is an int subclass, so True would
    otherwise read as probability 1.0). NaN and infinities fail the
    range comparison and need no separate check.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0.0 <= value <= 1.0


def compute_thresholds(
    word_samples: Sequence[WordSample],
    noise_samples: Sequence[float],
    current_min_probability: float,
    current_max_no_speech_prob: float,
    *,
    noise_skipped: bool = False,
) -> CalibrationProposal:
    """Apply the spec Section 7 rules to one session's measurements.

    ``word_samples`` are the accepted word captures; ``noise_samples``
    are the highest no-speech probabilities of the noise captures.
    ``noise_skipped`` records the user's explicit Skip choice -- the
    only case where the doubled-word skip formula may run. A COMPLETED
    noise stage whose values all turn out unusable is not a skip: it
    keeps the current ceiling, because the skip formula's justification
    (the user chose to give no noise evidence) does not hold there
    (review finding wh-7ou.7.6.5). Malformed values (None, non-finite,
    outside [0, 1], bool) are dropped before any rule runs; a word
    sample must be valid on both of its numbers to count.
    """
    valid_words = [
        s for s in word_samples
        if _is_valid_probability(s.min_word_probability)
        and _is_valid_probability(s.max_no_speech_prob)
    ]
    valid_noise = [v for v in noise_samples if _is_valid_probability(v)]

    return CalibrationProposal(
        min_probability=_propose_floor(valid_words, current_min_probability),
        max_no_speech_prob=_propose_ceiling(
            valid_words, valid_noise, current_max_no_speech_prob,
            noise_skipped,
        ),
        measured=_measured_ranges(valid_words, valid_noise),
    )


def _propose_floor(
    valid_words: list[WordSample], current: float
) -> ThresholdDecision:
    """Word-probability floor: lowest observed minus the safety margin."""
    if len(valid_words) < _FLOOR_MIN_WORD_SAMPLES:
        return ThresholdDecision(current=current, proposed=None)
    lowest = min(s.min_word_probability for s in valid_words)
    floor_value = max(0.0, lowest - _FLOOR_MARGIN)
    return ThresholdDecision(current=current, proposed=round(floor_value, 2))


def _propose_ceiling(
    valid_words: list[WordSample],
    valid_noise: list[float],
    current: float,
    noise_skipped: bool,
) -> ThresholdDecision:
    """No-speech ceiling: gap midpoint with noise data, doubled on skip."""
    if not valid_words:
        return ThresholdDecision(current=current, proposed=None)
    highest_word = max(s.max_no_speech_prob for s in valid_words)

    if noise_skipped:
        # The user's explicit Skip choice. The controller passes no
        # noise data on this path; if stray values ever arrive with the
        # flag, the explicit choice wins and they are ignored.
        doubled = _SKIP_SAFETY_FACTOR * highest_word
        clamped = min(_CEILING_SKIP_HIGH, max(_CEILING_SKIP_LOW, doubled))
        return ThresholdDecision(current=current, proposed=round(clamped, 3))

    if len(valid_noise) >= _CEILING_MIN_NOISE_SAMPLES:
        lowest_noise = min(valid_noise)
        if lowest_noise >= _NOISE_GAP_RATIO * highest_word:
            midpoint = (highest_word + lowest_noise) / 2.0
            return ThresholdDecision(
                current=current, proposed=round(midpoint, 3)
            )
        # Words and noise too similar to tell apart (no clean gap).
        return ThresholdDecision(current=current, proposed=None)

    # A completed noise stage that yielded fewer than 2 usable values
    # (all unusable, or exactly one -- not enough for the gap test).
    # Writing from ambiguous data is worse than keeping the current
    # value, and the skip formula is reserved for the explicit Skip
    # (review finding wh-7ou.7.6.5). Keep current.
    return ThresholdDecision(current=current, proposed=None)


def _measured_ranges(
    valid_words: list[WordSample], valid_noise: list[float]
) -> MeasuredRanges:
    word_probs = [s.min_word_probability for s in valid_words]
    word_no_speech = [s.max_no_speech_prob for s in valid_words]
    return MeasuredRanges(
        word_probability_min=min(word_probs) if word_probs else None,
        word_probability_max=max(word_probs) if word_probs else None,
        word_no_speech_min=min(word_no_speech) if word_no_speech else None,
        word_no_speech_max=max(word_no_speech) if word_no_speech else None,
        noise_no_speech_min=min(valid_noise) if valid_noise else None,
        noise_no_speech_max=max(valid_noise) if valid_noise else None,
    )
