"""Tests for the time/dollar text rules migrated into WhisperStreamingEngine.

wh-ocwbk / wh-e2cgy (Phase 3: retire TextNumerizer, epic wh-251rh). The two
Whisper-output cleanup rules -- time colon formatting anchored on AM/PM, and
the redundant "$N dollars" word -- moved from the Logic process's
TextNumerizer into the engine that produces the quirky output in the first
place. These cases are ported from
services/wheelhouse/tests/test_text_numerizer.py (23 cases) and retargeted
at the pure function ``apply_whisper_text_rules`` plus an end-to-end check
through ``WhisperStreamingEngine._extract_text``.
"""
import pytest
from unittest.mock import Mock, patch

from shared_stt.whisper_engine import (
    WhisperStreamingEngine,
    apply_whisper_text_rules,
)


def make_mock_segment(text: str):
    segment = Mock()
    segment.text = text
    segment.avg_logprob = -0.2
    segment.no_speech_prob = 0.01
    segment.compression_ratio = 0.5
    segment.start = 0.0
    segment.end = 1.0
    return segment


class TestTimeFormatting:
    """Rule 1: digit patterns anchored on AM/PM become H:MM AM/PM."""

    def test_period_separator_am(self):
        assert apply_whisper_text_rules("the meeting is at 9.45 am") == \
            "the meeting is at 9:45 AM"

    def test_period_separator_pm(self):
        assert apply_whisper_text_rules("dinner at 6.30 pm") == "dinner at 6:30 PM"

    def test_space_separator_am(self):
        assert apply_whisper_text_rules("call at 2 30 am") == "call at 2:30 AM"

    def test_space_separator_pm(self):
        assert apply_whisper_text_rules("call at 2 30 pm") == "call at 2:30 PM"

    def test_concatenated_am(self):
        assert apply_whisper_text_rules("945 am") == "9:45 AM"

    def test_concatenated_pm(self):
        assert apply_whisper_text_rules("1230 pm") == "12:30 PM"

    def test_period_no_ampm_untouched(self):
        # No AM/PM anchor: plain decimal must not become a time.
        assert apply_whisper_text_rules("the score was 9.45") == "the score was 9.45"

    def test_am_uppercase_input(self):
        assert apply_whisper_text_rules("9.45 AM") == "9:45 AM"

    def test_pm_mixed_case_input(self):
        assert apply_whisper_text_rules("9.45 Pm") == "9:45 PM"

    def test_time_in_sentence(self):
        assert apply_whisper_text_rules("the meeting is at 9.45 am on Tuesday") == \
            "the meeting is at 9:45 AM on Tuesday"

    def test_period_separator_no_space_before_ampm(self):
        assert apply_whisper_text_rules("9.45am") == "9:45 AM"

    def test_five_digit_number_before_am_untouched(self):
        # \b(\d{1,2})(\d{2})\b cannot match inside a 5-digit run.
        assert apply_whisper_text_rules("12345 am") == "12345 am"


class TestRedundantDollar:
    """Rule 2: "$N dollars" drops the redundant word."""

    def test_whole_amount(self):
        assert apply_whisper_text_rules("$200 dollars") == "$200"

    def test_decimal_amount(self):
        assert apply_whisper_text_rules("$12.50 dollars") == "$12.50"

    def test_no_redundancy_untouched(self):
        assert apply_whisper_text_rules("$200 invested") == "$200 invested"

    def test_dollars_without_sign_untouched(self):
        assert apply_whisper_text_rules("200 dollars") == "200 dollars"

    def test_in_sentence(self):
        assert apply_whisper_text_rules("I paid $200 dollars for it") == \
            "I paid $200 for it"

    def test_comma_formatted_amount(self):
        assert apply_whisper_text_rules("$1,200 dollars") == "$1,200"


class TestNoFalsePositives:
    def test_plain_text(self):
        assert apply_whisper_text_rules("hello world") == "hello world"

    def test_concat_digits_before_amps_untouched(self):
        # wh-251rh.1.2: (am|pm) must be word-bounded or "230 amps"
        # becomes "2:30 AMps".
        assert apply_whisper_text_rules("the circuit draws 230 amps") == \
            "the circuit draws 230 amps"

    def test_dotted_digits_before_amps_untouched(self):
        assert apply_whisper_text_rules("the meter reads 0.75 amps") == \
            "the meter reads 0.75 amps"

    def test_space_digits_before_amperes_untouched(self):
        assert apply_whisper_text_rules("it draws 2 30 amperes") == \
            "it draws 2 30 amperes"

    def test_long_dotted_number_before_am_untouched(self):
        # wh-251rh.3 (codex): without a LEADING \b, "123.45 am" partially
        # rewrites as "1" + "23:45 AM".
        assert apply_whisper_text_rules("flight 123.45 am departure") == \
            "flight 123.45 am departure"

    def test_long_space_number_before_am_untouched(self):
        # Same hole in the space form: "1234 45 am" partially rewrote as
        # "12" + "34:45 AM".
        assert apply_whisper_text_rules("serial 1234 45 am") == \
            "serial 1234 45 am"

    def test_invalid_minutes_dotted_untouched(self):
        # wh-251rh.3.1 (codex): only values that read as a real 12-hour
        # clock may rewrite. 75 is not a minute.
        assert apply_whisper_text_rules("the meter shows 0.75 am today") == \
            "the meter shows 0.75 am today"

    def test_invalid_hour_dotted_untouched(self):
        assert apply_whisper_text_rules("13.99 pm value") == "13.99 pm value"

    def test_radio_frequency_concat_untouched(self):
        # "980 am" is an AM radio frequency: 80 is not a minute.
        assert apply_whisper_text_rules("tune to 980 am") == "tune to 980 am"

    def test_invalid_minutes_space_untouched(self):
        assert apply_whisper_text_rules("room 7 85 pm wing") == \
            "room 7 85 pm wing"

    def test_empty_string(self):
        assert apply_whisper_text_rules("") == ""

    def test_numbers_without_patterns(self):
        assert apply_whisper_text_rules("I have 42 apples") == "I have 42 apples"

    def test_currency_correct(self):
        assert apply_whisper_text_rules("I paid $50 for it") == "I paid $50 for it"


class TestCombined:
    def test_time_and_currency(self):
        assert apply_whisper_text_rules("at 9.45 am I paid $200 dollars") == \
            "at 9:45 AM I paid $200"


@patch("shared_stt.whisper_engine.WhisperModel")
class TestExtractTextAppliesRules:
    """The rules run inside _extract_text AFTER the existing normalization
    (spelled-word collapse, punctuation removal, first-char lowercase, I
    capitalization), so the inserted colon survives and the output equals
    what the Logic-side TextNumerizer used to produce on the engine's
    final text.

    WhisperModel is mocked (wh-251rh.1.1): the engine's __init__ loads the
    real model, and these tests only exercise the text path. Without the
    patch they hard-fail on non-CUDA machines and download multi-GB
    weights elsewhere, violating the mock-everything contract documented
    in test_whisper_engine.py.
    """

    def test_time_rule_applies_after_normalization(self, mock_model_class):
        engine = WhisperStreamingEngine()
        segments = [make_mock_segment("The meeting is at 9.45 am.")]
        # Punctuation removal keeps the digit-adjacent period in 9.45 and
        # drops the trailing one; lowercase-first-char then the time rule.
        assert engine._extract_text(segments) == "the meeting is at 9:45 AM"

    def test_dollar_rule_applies_after_comma_stripping(self, mock_model_class):
        engine = WhisperStreamingEngine()
        segments = [make_mock_segment("I paid $1,200 dollars.")]
        # The existing normalization strips the comma first, then the
        # dollar rule drops the redundant word.
        assert engine._extract_text(segments) == "I paid $1200"

    def test_plain_text_unchanged(self, mock_model_class):
        engine = WhisperStreamingEngine()
        segments = [make_mock_segment("Hello world.")]
        assert engine._extract_text(segments) == "hello world"
