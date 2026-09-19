"""Comparator rows for the benchmark harness's loose match.

The harness normalizes BOTH the manifest's expected_transcription and the
model's output with the same function before comparing them, so a model
that writes "8000" where the canonical says "eight thousand" is not
scored down for a formatting choice WheelHouse itself makes downstream.

Two normalizers run, in this order:

1. shared_stt.transcript_rules.apply_itn -- the shipped inverse text
   normalizer. Above-ten cardinals, money, AM/PM clock times, decimals
   and spoken digit sequences. It refuses a lone number word, which is a
   command-safety rule of the module and is NOT relaxed for the harness.
2. _normalize_small_numbers -- the harness's own zero-through-ten word to
   digit table, which predates the module and stays. It is what converts
   the lone small numbers apply_itn declines to touch, so "delete one"
   keeps matching "delete 1".

The order is load-bearing: apply_itn reads number WORDS, so
"eight thousand" must reach it before _normalize_small_numbers turns it
into "8 thousand", a form apply_itn leaves alone.
"""

import pytest

from run_benchmark import _loose_normalize, loose_match


class TestAboveTenNumbersMatch:
    """The class wh-x1rn measured: models that emit digits were scored down."""

    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("eight thousand", "8000"),
            ("one hundred", "100"),
            ("fifty nine", "59"),
            ("call me at nine oh two one oh", "call me at 90210"),
        ],
    )
    def test_spoken_cardinal_matches_written_digits(self, expected, actual):
        assert loose_match(expected, actual)


class TestMoneyTimeAndDecimalMatch:
    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("five dollars", "$5"),
            ("fifty nine dollars and seventeen cents", "$59.17"),
            ("eleven fifteen PM", "11:15 PM"),
            ("three point five", "3.5"),
        ],
    )
    def test_spoken_form_matches_written_form(self, expected, actual):
        assert loose_match(expected, actual)


class TestAlreadyWrittenFormsAreNotConvertedTwice:
    """A digits-in string must survive normalization unchanged.

    If apply_itn ran after _normalize_small_numbers, or ran over its own
    output, an already-written string could drift. Both sides here are
    identical, so a mismatch means the normalizer is not idempotent.
    """

    @pytest.mark.parametrize(
        "text",
        ["8000", "100", "$59.17", "11:15 PM", "3.5", "90210", "tab 2"],
    )
    def test_written_form_matches_itself(self, text):
        assert loose_match(text, text)

    @pytest.mark.parametrize(
        "text",
        ["8000", "$59.17", "11:15 PM", "3.5", "90210"],
    )
    def test_normalizing_twice_changes_nothing(self, text):
        once = _loose_normalize(text)
        assert _loose_normalize(once) == once


class TestSmallNumberHandlingIsUnchanged:
    """apply_itn declines a lone number word; the harness table still converts it."""

    @pytest.mark.parametrize(
        "expected,actual",
        [
            ("five", "5"),
            ("ten", "10"),
            ("zero", "0"),
            ("delete one", "delete 1"),
            ("tab two", "tab 2"),
            ("tab 2", "tab two"),
        ],
    )
    def test_lone_small_number_still_matches_its_digit(self, expected, actual):
        assert loose_match(expected, actual)


class TestModuleRefusalsGetTheSameTreatmentOnBothSides:
    """Phrases the module refuses stay refused, by design.

    'twenty three fifty' and 'four ten' are command-safety refusals in
    shared_stt.transcript_rules. The harness does not add a rule for
    them. Because the identical normalizer runs on the reference and on
    the model output, a model that spells them out is scored exactly as
    a model that writes them out -- neither is punished.
    """

    @pytest.mark.parametrize(
        "text", ["twenty three fifty", "four ten", "set volume to fifty"]
    )
    def test_refused_phrase_still_matches_itself(self, text):
        assert loose_match(text, text)

    @pytest.mark.parametrize(
        "expected,actual",
        [("twenty three fifty", "2350"), ("four ten", "4:10")],
    )
    def test_refused_phrase_does_not_match_its_written_form(
        self, expected, actual
    ):
        assert not loose_match(expected, actual)


class TestProperNounBranchIsUnchanged:
    """The case-sensitivity decision in loose_match() must not move."""

    def test_proper_noun_expected_requires_matching_case(self):
        assert not loose_match("call Bill Smith", "call bill smith")

    def test_proper_noun_expected_matches_when_case_agrees(self):
        assert loose_match("call Bill Smith", "call Bill Smith.")

    def test_without_proper_noun_comparison_stays_case_insensitive(self):
        assert loose_match("open the file", "Open The File")

    def test_all_cap_abbreviation_is_not_a_proper_noun(self):
        assert loose_match("send the JSON to the server", "send the json to the server")

    def test_number_conversion_does_not_introduce_a_proper_noun(self):
        """A converted phrase must not flip the branch to case-sensitive."""
        assert loose_match("eight thousand files", "8000 FILES")
