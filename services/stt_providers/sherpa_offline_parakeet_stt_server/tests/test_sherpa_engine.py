"""Tests for SherpaOfflineEngine's use of the shared transcript rules.

The engine used to carry its own `_normalize_text` copy of the time /
phone-number / am-pm-uppercase rules, and this file pinned that copy row
by row. wh-shared-itn-parakeet deleted the copy: both inference paths now
call shared_stt.transcript_rules.normalize_transcript and apply_itn, and
the rules themselves are pinned by the table-driven tests in
services/stt_providers/shared/tests/test_transcript_rules.py.

What is left here is INTEGRATION coverage -- rows that exercise the
engine calling the shared functions in the fixed order, including the
double-conversion guard that matters only for this provider, because
Parakeet emits digits natively.

The sherpa recognizer is mocked; these tests never load the real ONNX
model. `_load_model` is patched to a no-op so the filesystem /
model-file checks don't fire during construction.
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


def _chunk() -> bytes:
    """One second of silent float32 audio, as process_audio wants it.

    Silence is enough: the recognizer is a MagicMock whose result text is
    fixed by engine_factory, so the samples only have to reach the buffer.
    """
    return np.zeros(16000, dtype=np.float32).tobytes()


class TestRecognizeStripsLeadingAndTrailingWhitespace:
    def test_recognize_strips_whitespace(self, engine_factory):
        engine = engine_factory(recognizer_text="  hello  ")
        audio = np.zeros(16000, dtype=np.float32)
        assert engine._recognize(audio) == "hello"


# ---------------------------------------------------------------------------
# Integration: the engine calls the shared transcript rules
# (wh-shared-itn-parakeet). These rows exercise the two inference call
# sites, not the rules themselves -- the rules have their own
# table-driven tests in services/stt_providers/shared/tests/.
# ---------------------------------------------------------------------------

class TestNormalizeApplied:
    """normalize_transcript reaches the engine's confirmed words.

    One recognizer string in, one confirmed-word string out, through
    each of the two call sites: _run_final_inference (via finalize) and
    _run_inference (the periodic path, which needs two agreeing decodes
    before LocalAgreement-2 confirms anything).
    """

    def test_final_inference_normalizes(self, engine_factory):
        engine = engine_factory(recognizer_text="It is 8.17 p.m.")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "it is 8:17 PM"

    def test_periodic_inference_normalizes(self, engine_factory):
        engine = engine_factory(recognizer_text="X, Ray Boost")
        engine.process_audio(_chunk())
        engine._run_inference()
        engine._run_inference()
        # wh-first-char-lowercase changed this row from "x-ray Boost".
        # "Boost" is capitalized, which the condition reads as evidence
        # that the leading X is not merely positional, so the X
        # survives. The rejoin this row exists to prove still ran: the
        # hyphen is present. The hotword is unaffected, because
        # speech.router._word_matches_hotword lowercases both sides.
        assert engine.get_result() == "X-ray Boost"


class TestItnApplied:
    """apply_itn reaches the engine's confirmed words.

    Same shape as TestNormalizeApplied. The stage order is fixed:
    normalize_transcript first, apply_itn second. Normalization strips
    punctuation and collapses "p.m." to "pm", which is the shape the ITN
    clock rule reads.
    """

    def test_cardinal_phrase_converted(self, engine_factory):
        engine = engine_factory(recognizer_text="set the volume to fifty nine")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "set the volume to 59"

    def test_money_phrase_converted(self, engine_factory):
        engine = engine_factory(
            recognizer_text="fifty nine dollars and seventeen cents"
        )
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "$59.17"

    def test_clock_time_with_period_word_converted(self, engine_factory):
        """The uppercase "PM" here can only come from apply_itn: the
        _AMPM_UPPERCASE rule inside normalize_transcript runs first and
        needs an HH:MM shape, so it leaves the lowercase "pm" alone."""
        engine = engine_factory(recognizer_text="eleven fifteen pm")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "11:15 PM"

    def test_periodic_inference_converts(self, engine_factory):
        engine = engine_factory(recognizer_text="set the volume to fifty nine")
        engine.process_audio(_chunk())
        engine._run_inference()
        engine._run_inference()
        assert engine.get_result() == "set the volume to 59"

    def test_lone_number_word_stays_a_word(self, engine_factory):
        """Command safety: "delete two" must never become "delete 2",
        which would be a different, destructive command."""
        engine = engine_factory(recognizer_text="delete two")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "delete two"

        bare = engine_factory(recognizer_text="two")
        bare.process_audio(_chunk())
        bare.finalize()
        assert bare.get_result() == "two"


class TestItnDoesNotDoubleConvert:
    """Parakeet emits digits natively, unlike word-emitting streaming
    models, so apply_itn runs on text that may ALREADY hold numerals.
    Every row here feeds a recognizer string that contains digits and
    asserts the engine's output is exactly the normalized form -- no
    second conversion pass on top of the first (wh-shared-itn-parakeet,
    acceptance item 2).
    """

    def test_bare_numeral_passes_through(self, engine_factory):
        engine = engine_factory(recognizer_text="set brightness to 25")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "set brightness to 25"

    def test_numeral_beside_unit_anchor_passes_through(self, engine_factory):
        """A digit next to "dollars" is the shape the money rule reads
        in its spoken form. Already-written digits must not be swept
        into "$25"."""
        engine = engine_factory(recognizer_text="I have 25 dollars")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "I have 25 dollars"

    def test_normalizer_produced_clock_time_passes_through(
        self, engine_factory
    ):
        """The hazard specific to this phase: normalize_transcript turns
        "8.17 p.m." into "8:17 PM" and apply_itn then sees a clock shape
        it also knows how to build. It must leave it alone."""
        engine = engine_factory(recognizer_text="It is 8.17 p.m.")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "it is 8:17 PM"

    def test_normalizer_produced_phone_number_passes_through(
        self, engine_factory
    ):
        engine = engine_factory(recognizer_text="call me at 7035551234")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "call me at 703-555-1234"

    def test_decimal_and_digit_run_pass_through(self, engine_factory):
        engine = engine_factory(recognizer_text="pi is about 3.14")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "pi is about 3.14"

    def test_mixed_digits_and_words_pass_through(self, engine_factory):
        engine = engine_factory(
            recognizer_text="set it to 25 percent and call 7035551234"
        )
        engine.process_audio(_chunk())
        engine.finalize()
        assert (
            engine.get_result()
            == "set it to 25 percent and call 703-555-1234"
        )
