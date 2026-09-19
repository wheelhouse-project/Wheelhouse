"""Unit tests for the voice-calibration threshold arithmetic (wh-7ou.7.2.1).

Covers spec Section 7 of
docs/superpowers/specs/2026-08-07-voice-calibration-design.md exactly:

- Word-probability floor: lowest observed word probability minus 0.05,
  never below 0.0, rounded to 2 decimals; requires at least 12 valid word
  samples, otherwise keep current.
- No-speech ceiling with noise data (at least 2 noise samples): accepted
  only when the lowest noise value is at least twice the highest word
  no-speech value; the ceiling is their midpoint, rounded to 3 decimals;
  gap absent -> keep current.
- No-speech ceiling with the noise stage skipped (an explicit user
  choice, passed as ``noise_skipped=True``): twice the highest word
  no-speech value, kept within [0.01, 0.10], rounded to 3 decimals.
- A COMPLETED noise stage whose measurements were all unusable is NOT
  a skip: fewer than 2 valid noise values without an explicit skip
  keeps the current ceiling (review finding wh-7ou.7.6.5).

The reference fixture is the 2026-08-07 hand calibration the feature
mechanizes: word probabilities 0.198-0.587, word no-speech 0.007-0.017,
coughs 0.044-0.089; expected floor 0.15, ceiling 0.030 with noise data
and 0.034 without.

compute_thresholds is a pure function -- no I/O, no config reads.
"""

from speech.calibration_math import (
    CalibrationProposal,
    WordSample,
    compute_thresholds,
)

# Shipped defaults for the two settings (whisper_engine.py).
CURRENT_FLOOR = 0.6
CURRENT_CEILING = 0.03


def _words(probs, no_speech=0.01):
    """One WordSample per probability, all sharing one no-speech value."""
    return [
        WordSample(min_word_probability=p, max_no_speech_prob=no_speech)
        for p in probs
    ]


def _fixture_words():
    """Twenty samples spanning the 2026-08-07 hand-calibration ranges."""
    probs = [
        0.198, 0.221, 0.245, 0.260, 0.278,
        0.301, 0.325, 0.340, 0.362, 0.388,
        0.405, 0.430, 0.455, 0.470, 0.492,
        0.515, 0.530, 0.552, 0.570, 0.587,
    ]
    no_speech = [
        0.007, 0.009, 0.011, 0.013, 0.015,
        0.017, 0.008, 0.010, 0.012, 0.014,
        0.016, 0.007, 0.009, 0.011, 0.013,
        0.015, 0.008, 0.010, 0.012, 0.014,
    ]
    return [
        WordSample(min_word_probability=p, max_no_speech_prob=n)
        for p, n in zip(probs, no_speech)
    ]


FIXTURE_NOISE = [0.044, 0.061, 0.089]


def _compute(
    word_samples, noise_samples, noise_skipped=False,
) -> CalibrationProposal:
    return compute_thresholds(
        word_samples=word_samples,
        noise_samples=noise_samples,
        current_min_probability=CURRENT_FLOOR,
        current_max_no_speech_prob=CURRENT_CEILING,
        noise_skipped=noise_skipped,
    )


# ---------------------------------------------------------------------------
# Spec Section 7 reference fixture (the 2026-08-07 hand calibration)
# ---------------------------------------------------------------------------

def test_fixture_floor_matches_hand_calibration():
    result = _compute(_fixture_words(), FIXTURE_NOISE)
    assert result.min_probability.proposed == 0.15


def test_fixture_ceiling_with_noise_matches_hand_calibration():
    result = _compute(_fixture_words(), FIXTURE_NOISE)
    assert result.max_no_speech_prob.proposed == 0.030


def test_fixture_ceiling_without_noise_matches_hand_calibration():
    result = _compute(_fixture_words(), [], noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.034


def test_fixture_current_values_echoed():
    result = _compute(_fixture_words(), FIXTURE_NOISE)
    assert result.min_probability.current == CURRENT_FLOOR
    assert result.max_no_speech_prob.current == CURRENT_CEILING


def test_fixture_measured_ranges_report_extremes():
    result = _compute(_fixture_words(), FIXTURE_NOISE)
    measured = result.measured
    assert measured.word_probability_min == 0.198
    assert measured.word_probability_max == 0.587
    assert measured.word_no_speech_min == 0.007
    assert measured.word_no_speech_max == 0.017
    assert measured.noise_no_speech_min == 0.044
    assert measured.noise_no_speech_max == 0.089


# ---------------------------------------------------------------------------
# Word-probability floor
# ---------------------------------------------------------------------------

def test_floor_requires_twelve_valid_word_samples():
    samples = _words([0.30] * 10 + [0.25])
    assert len(samples) == 11
    result = _compute(samples, [])
    assert result.min_probability.proposed is None
    assert result.min_probability.keep


def test_floor_proposes_at_exactly_twelve_samples():
    samples = _words([0.30] * 11 + [0.25])
    assert len(samples) == 12
    result = _compute(samples, [])
    assert result.min_probability.proposed == 0.20
    assert not result.min_probability.keep


def test_floor_never_below_zero():
    result = _compute(_words([0.03] * 12), [])
    assert result.min_probability.proposed == 0.0


def test_floor_rounds_to_two_decimals():
    # 0.213 - 0.05 = 0.163 -> nearest hundredth 0.16.
    result = _compute(_words([0.30] * 11 + [0.213]), [])
    assert result.min_probability.proposed == 0.16


# ---------------------------------------------------------------------------
# No-speech ceiling, with noise data
# ---------------------------------------------------------------------------

def test_ceiling_noise_gap_accepted_at_exactly_twice():
    # Lowest noise 0.04 is exactly twice the highest word value 0.02
    # ("at least twice" admits equality); midpoint (0.02 + 0.04) / 2.
    result = _compute(_words([0.4] * 12, no_speech=0.02), [0.04, 0.05])
    assert result.max_no_speech_prob.proposed == 0.03


def test_ceiling_noise_gap_absent_keeps_current():
    # 0.038 / 0.02 = 1.9x -- below the required 2x gap.
    result = _compute(_words([0.4] * 12, no_speech=0.02), [0.038, 0.05])
    assert result.max_no_speech_prob.proposed is None
    assert result.max_no_speech_prob.keep


def test_ceiling_midpoint_rounds_to_three_decimals():
    # Midpoint (0.016 + 0.038) / 2 = 0.027; two decimals would give 0.03.
    result = _compute(_words([0.4] * 12, no_speech=0.016), [0.038, 0.05])
    assert result.max_no_speech_prob.proposed == 0.027


def test_ceiling_requires_two_noise_samples():
    # One noise sample fits neither spec branch (noise path needs at
    # least 2; the skip formula is for a skipped stage): keep current.
    result = _compute(_words([0.4] * 12, no_speech=0.017), [0.05])
    assert result.max_no_speech_prob.proposed is None


def test_ceiling_proposes_with_exactly_two_noise_samples():
    result = _compute(_words([0.4] * 12, no_speech=0.02), [0.04, 0.06])
    assert result.max_no_speech_prob.proposed == 0.03


# ---------------------------------------------------------------------------
# No-speech ceiling, noise stage skipped
# ---------------------------------------------------------------------------

def test_ceiling_skipped_doubles_highest_word_value():
    result = _compute(_words([0.4] * 12, no_speech=0.017), [],
                      noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.034


def test_ceiling_skipped_clamps_low():
    # 2 * 0.004 = 0.008 -> clamped up to 0.01.
    result = _compute(_words([0.4] * 12, no_speech=0.004), [],
                      noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.01


def test_ceiling_skipped_clamps_high():
    # 2 * 0.06 = 0.12 -> clamped down to 0.10.
    result = _compute(_words([0.4] * 12, no_speech=0.06), [],
                      noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.10


def test_ceiling_skipped_rounds_to_three_decimals():
    # 2 * 0.0171 = 0.0342 -> 0.034.
    result = _compute(_words([0.4] * 12, no_speech=0.0171), [],
                      noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.034


def test_ceiling_does_not_require_twelve_word_samples():
    # Only the floor carries the 12-sample minimum (spec Section 7).
    result = _compute(_words([0.4] * 3, no_speech=0.017), [],
                      noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.034
    assert result.min_probability.proposed is None


# ---------------------------------------------------------------------------
# No-speech ceiling, completed noise stage with unusable measurements
# (review finding wh-7ou.7.6.5: not a skip -- the skip formula's
# justification, an explicit user choice to give no noise evidence,
# does not hold, so the ceiling keeps its current value)
# ---------------------------------------------------------------------------

def test_completed_noise_all_none_keeps_ceiling():
    result = _compute(_words([0.4] * 12, no_speech=0.017),
                      [None, None, None])
    assert result.max_no_speech_prob.proposed is None
    assert result.max_no_speech_prob.keep
    # The floor is decided on its own data and is still written.
    assert result.min_probability.proposed == 0.35


def test_completed_noise_all_malformed_keeps_ceiling():
    # Every malformed shape the provider can produce: null, bool,
    # non-finite, out of range, non-numeric.
    result = _compute(
        _words([0.4] * 12, no_speech=0.017),
        [None, True, float("nan"), 1.5, "loud"],
    )
    assert result.max_no_speech_prob.proposed is None


def test_skip_flag_with_stray_noise_data_still_uses_skip_formula():
    # The controller never passes noise data on a skip; if a caller
    # ever does, the explicit skip wins and the data is ignored.
    result = _compute(_words([0.4] * 12, no_speech=0.017),
                      [0.05, 0.06], noise_skipped=True)
    assert result.max_no_speech_prob.proposed == 0.034


# ---------------------------------------------------------------------------
# No word samples at all
# ---------------------------------------------------------------------------

def test_no_word_samples_keeps_both():
    result = _compute([], [0.04, 0.06])
    assert result.min_probability.proposed is None
    assert result.max_no_speech_prob.proposed is None
    assert result.measured.word_probability_min is None
    assert result.measured.word_probability_max is None
    assert result.measured.word_no_speech_min is None
    assert result.measured.word_no_speech_max is None
    # Noise was still measured and belongs in the details view.
    assert result.measured.noise_no_speech_min == 0.04
    assert result.measured.noise_no_speech_max == 0.06


def test_measured_noise_ranges_none_when_skipped():
    result = _compute(_fixture_words(), [], noise_skipped=True)
    assert result.measured.noise_no_speech_min is None
    assert result.measured.noise_no_speech_max is None


# ---------------------------------------------------------------------------
# Malformed sample values never count and never poison the arithmetic
# ---------------------------------------------------------------------------

def test_out_of_range_word_sample_does_not_count():
    samples = _words([0.30] * 11 + [1.5])
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_negative_word_sample_does_not_count():
    samples = _words([0.30] * 11 + [-0.2])
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_non_finite_word_sample_does_not_count():
    samples = _words([0.30] * 11 + [float("nan")])
    result = _compute(samples, [])
    assert result.min_probability.proposed is None
    samples = _words([0.30] * 11 + [float("inf")])
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_bool_word_sample_does_not_count():
    # bool is an int subclass; True must not read as probability 1.0.
    samples = _words([0.30] * 11 + [True])
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_none_word_sample_does_not_count():
    # The provider's confidence block carries nulls when word-level data
    # is missing; a None must be dropped, not crash the arithmetic.
    samples = _words([0.30] * 11) + [
        WordSample(min_word_probability=None, max_no_speech_prob=0.01),
    ]
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_invalid_no_speech_side_invalidates_the_word_sample():
    samples = _words([0.30] * 11) + [
        WordSample(min_word_probability=0.25, max_no_speech_prob=float("nan")),
    ]
    result = _compute(samples, [])
    assert result.min_probability.proposed is None


def test_invalid_noise_values_are_ignored():
    # Two valid noise values plus a NaN: still the with-noise path.
    result = _compute(
        _words([0.4] * 12, no_speech=0.02), [0.04, 0.06, float("nan")]
    )
    assert result.max_no_speech_prob.proposed == 0.03


def test_single_valid_noise_value_after_filtering_keeps_current():
    # One valid value plus garbage is still just one noise sample.
    result = _compute(
        _words([0.4] * 12, no_speech=0.017), [0.05, float("inf")]
    )
    assert result.max_no_speech_prob.proposed is None


def test_invalid_samples_do_not_reach_measured_ranges():
    samples = _words([0.30] * 12) + [
        WordSample(min_word_probability=1.5, max_no_speech_prob=0.9),
    ]
    result = _compute(samples, [0.04, 0.06, 2.0])
    assert result.measured.word_probability_max == 0.30
    assert result.measured.noise_no_speech_max == 0.06
