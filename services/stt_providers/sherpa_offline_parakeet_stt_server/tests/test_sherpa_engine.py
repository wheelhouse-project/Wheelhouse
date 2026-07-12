"""Tests for SherpaOfflineEngine numeric fallback rules.

Covers `_normalize_text` -- time / phone-number / am-pm-uppercase
rules. There is deliberately NO redundant-dollar rule here: Parakeet
does not emit "$200 dollars" forms (see the 2026-04-19 Parakeet ITN
design doc), so that rule lives only in the whisper engine.
The sherpa recognizer is mocked; these tests
never load the real ONNX model. `_load_model` is patched to a no-op at
the class level so the filesystem / model-file checks don't fire during
construction.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from sherpa_engine import SherpaOfflineEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine_factory(monkeypatch):
    """Return a callable that builds an engine with a fake recognizer.

    Patches `_load_model` to a no-op so we can construct without the real
    ONNX model directory.
    """
    monkeypatch.setattr(
        SherpaOfflineEngine, "_load_model", lambda *a, **kw: None
    )

    def _make(recognizer_text: str = "hello world"):
        engine = SherpaOfflineEngine(model_path="/nonexistent")
        stream = MagicMock()
        stream.result.text = recognizer_text
        recognizer = MagicMock()
        recognizer.create_stream.return_value = stream
        engine._recognizer = recognizer
        return engine

    return _make


# ---------------------------------------------------------------------------
# _normalize_text: dotted-period time, AM/PM uppercase, phone hyphenation
# ---------------------------------------------------------------------------

class TestNormalizeTextTimeRules:
    def test_period_form_becomes_colon(self):
        assert (
            SherpaOfflineEngine._normalize_text("call at 9.45 am")
            == "call at 9:45 AM"
        )

    def test_pm_variants_case_insensitive(self):
        assert (
            SherpaOfflineEngine._normalize_text("see you at 6.30 Pm")
            == "see you at 6:30 PM"
        )

    def test_idempotent_on_uppercase_colon_output(self):
        # Text already in canonical '6:30 PM' form must not be double-transformed.
        assert (
            SherpaOfflineEngine._normalize_text("remind me at 6:30 PM")
            == "remind me at 6:30 PM"
        )

    def test_uppercases_lowercase_ampm_beside_colon_time(self):
        # Lowercase am/pm next to HH:MM must be canonicalized to AM/PM.
        assert (
            SherpaOfflineEngine._normalize_text("remind me at 6:30 pm")
            == "remind me at 6:30 PM"
        )

    def test_dotted_digits_before_amps_untouched(self):
        # wh-251rh.1.2: (am|pm) must be word-bounded or "0.75 amps"
        # becomes "0:75 AMps". Same hole as the whisper engine's copy.
        assert (
            SherpaOfflineEngine._normalize_text("the meter reads 0.75 amps")
            == "the meter reads 0.75 amps"
        )

    def test_invalid_minutes_dotted_untouched(self):
        # wh-251rh.3.1 (codex): only values that read as a real 12-hour
        # clock may rewrite. 75 is not a minute; 13 is not a 12-hour hour.
        assert (
            SherpaOfflineEngine._normalize_text("the log shows 0.75 am today")
            == "the log shows 0.75 am today"
        )
        assert (
            SherpaOfflineEngine._normalize_text("value 13.99 pm recorded")
            == "value 13.99 pm recorded"
        )

    def test_long_dotted_number_before_am_untouched(self):
        # wh-251rh.3 (codex): without a LEADING \b, "123.45 am" partially
        # rewrites as "1" + "23:45 AM". Same hole as the whisper engine.
        assert (
            SherpaOfflineEngine._normalize_text("part 123.45 am reading")
            == "part 123.45 am reading"
        )

    def test_parakeet_dotted_ampm_period_time_form(self):
        # Regression guard for the TTS-audio diagnostic finding:
        # Parakeet itself emits strings like "It is 8.17 p.m." where
        # the AM/PM marker has internal dots. The punctuation strip runs
        # before the time rule so the dotted AM/PM has collapsed to bare
        # "am"/"pm" by the time _TIME_PERIOD fires.
        assert (
            SherpaOfflineEngine._normalize_text("It is 8.17 p.m.")
            == "it is 8:17 PM"
        )
        assert (
            SherpaOfflineEngine._normalize_text("It is 9.45 a.m.")
            == "it is 9:45 AM"
        )
        assert (
            SherpaOfflineEngine._normalize_text("Call me at 9.45 p.m.")
            == "call me at 9:45 PM"
        )

    def test_hyphenates_ten_digit_phone_number(self):
        # Some Parakeet voices emit "7035551234" (flat); others emit
        # "703-555-1234" (hyphenated). The hyphenated form is canonical.
        assert (
            SherpaOfflineEngine._normalize_text("call me at 7035551234")
            == "call me at 703-555-1234"
        )
        assert (
            SherpaOfflineEngine._normalize_text("my number is 2025559876")
            == "my number is 202-555-9876"
        )

    def test_hyphenate_phone_preserves_already_hyphenated(self):
        assert (
            SherpaOfflineEngine._normalize_text("call 703-555-1234 now")
            == "call 703-555-1234 now"
        )

    def test_hyphenate_phone_leaves_short_digit_runs_alone(self):
        # 7-digit, 9-digit, 11-digit runs must not become phone shapes.
        assert (
            SherpaOfflineEngine._normalize_text("the code is 5551234")
            == "the code is 5551234"
        )
        assert (
            SherpaOfflineEngine._normalize_text("the id is 12345678901")
            == "the id is 12345678901"
        )

    def test_preserves_two_digit_hours(self):
        assert (
            SherpaOfflineEngine._normalize_text("lunch at 10:45 AM")
            == "lunch at 10:45 AM"
        )
        assert (
            SherpaOfflineEngine._normalize_text("noon meeting 12:00 PM")
            == "noon meeting 12:00 PM"
        )

    def test_plain_decimal_without_ampm_left_alone(self):
        # "3.14" without an AM/PM anchor is a plain decimal and must not
        # be misinterpreted as a time.
        assert (
            SherpaOfflineEngine._normalize_text("pi is about 3.14")
            == "pi is about 3.14"
        )


# ---------------------------------------------------------------------------
# Punctuation-regex colon negative-case coverage
# ---------------------------------------------------------------------------

class TestNormalizeTextColonHandling:
    def test_non_digit_colons_still_stripped(self):
        # Prior behavior: colons outside of HH:MM are stripped as punctuation.
        # The tightened regex must not start preserving those.
        assert (
            SherpaOfflineEngine._normalize_text("subject: dinner plans")
            == "subject dinner plans"
        )

    def test_digit_flanked_colons_preserved_in_non_time_contexts(self):
        # Ratios, scores, IPv4 port suffixes -- anywhere colons sit
        # between digits -- must survive the punctuation pass, even when
        # not emitted by the time-reformat rules.
        assert (
            SherpaOfflineEngine._normalize_text("the score was 3:2 tonight")
            == "the score was 3:2 tonight"
        )


class TestRecognizeStripsLeadingAndTrailingWhitespace:
    def test_recognize_strips_whitespace(self, engine_factory):
        engine = engine_factory(recognizer_text="  hello  ")
        audio = np.zeros(16000, dtype=np.float32)
        assert engine._recognize(audio) == "hello"
