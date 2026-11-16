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

    def _make(recognizer_text: str = "hello world", tokens=None):
        engine = SherpaOfflineEngine(model_path="/nonexistent")
        stream = MagicMock()
        stream.result.text = recognizer_text
        # The engine builds its text from the tokens, so by default one
        # token carries the whole text.
        stream.result.tokens = [recognizer_text] if tokens is None else tokens
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


class TestRecognizeBuildsTextFromTokens:
    """wh-parakeet-spinning-upgrade: from sherpa-onnx 1.13.4 (commit
    768fd7a99) result.text loses the space before every ASCII punctuation
    character, "$" too, and no option turns that off. result.tokens keeps
    the space, so the engine joins the tokens instead."""

    def test_the_space_before_a_dollar_sign_stays(self, engine_factory):
        engine = engine_factory(
            recognizer_text="It costs$5.",
            tokens=[" It", " cost", "s", " ", "$", "5", "."],
        )
        audio = np.zeros(16000, dtype=np.float32)
        assert engine._recognize(audio) == "It costs $5."


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

    def test_periodic_inference_keeps_a_trailing_tens_word(
        self, engine_factory
    ):
        """wh-itn-lone-number-ten.1.1: the interim call passes
        interim=True. WheelHouse reads agreed interim words as stable
        words, and "backspace twenty" can still grow into "backspace
        twenty three"; the router waits for a count only while the
        trailing tens word is a word."""
        engine = engine_factory(recognizer_text="backspace twenty")
        engine.process_audio(_chunk())
        engine._run_inference()
        engine._run_inference()
        assert engine.get_result() == "backspace twenty"

    def test_final_inference_converts_a_trailing_tens_word(
        self, engine_factory
    ):
        """The final call keeps the default: the utterance is over, so
        the trailing "twenty" can no longer grow."""
        engine = engine_factory(recognizer_text="backspace twenty")
        engine.process_audio(_chunk())
        engine.finalize()
        assert engine.get_result() == "backspace 20"


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


# ---------------------------------------------------------------------------
# wh-first-sound-lost.1: the engine keeps the audio of its final decode
# ---------------------------------------------------------------------------

def _noise_chunks(count: int = 3, n: int = 8000, seed: int = 7):
    """Distinct float32 chunks, so a wrong order or a dropped chunk shows."""
    rng = np.random.default_rng(seed)
    return [
        (rng.standard_normal(n) * 0.1).astype(np.float32) for _ in range(count)
    ]


def _last_waveform(engine) -> np.ndarray:
    """The array the fake recognizer's stream received on its latest call."""
    stream = engine._recognizer.create_stream.return_value
    return stream.accept_waveform.call_args.args[1]


class TestLastFinalAudio:
    """last_final_audio is the float32 array handed to the recognizer for the
    final decode. AudioProcessor passes it to the engine-audio saver."""

    def test_it_starts_as_none(self, engine_factory):
        assert engine_factory().last_final_audio is None

    def test_after_a_final_decode_it_equals_what_the_recognizer_received(
        self, engine_factory
    ):
        engine = engine_factory(recognizer_text="hello there")
        chunks = _noise_chunks()
        for chunk in chunks:
            engine.process_audio(chunk.tobytes())
        engine.finalize()

        received = _last_waveform(engine)
        assert engine.last_final_audio is not None
        assert engine.last_final_audio.dtype == np.float32
        assert np.array_equal(engine.last_final_audio, received)
        # And that array is the whole utterance, in the order it arrived.
        assert np.array_equal(engine.last_final_audio, np.concatenate(chunks))

    def test_it_is_set_when_the_final_decode_gives_no_text(self, engine_factory):
        engine = engine_factory(recognizer_text="")
        chunks = _noise_chunks(2)
        for chunk in chunks:
            engine.process_audio(chunk.tobytes())
        engine.finalize()

        assert engine.get_result() == ""
        assert np.array_equal(engine.last_final_audio, np.concatenate(chunks))

    def test_reset_clears_it(self, engine_factory):
        engine = engine_factory()
        engine.process_audio(_noise_chunks(1)[0].tobytes())
        engine.finalize()
        assert engine.last_final_audio is not None

        engine.reset()

        assert engine.last_final_audio is None

    def test_a_partial_decode_does_not_set_it(self, engine_factory):
        engine = engine_factory()
        engine.process_audio(_noise_chunks(1)[0].tobytes())
        engine._run_inference()
        assert engine._recognizer.create_stream.return_value \
            .accept_waveform.called
        assert engine.last_final_audio is None

    def test_a_periodic_decode_inside_process_audio_does_not_set_it(
        self, engine_factory
    ):
        """process_audio runs _run_inference itself once the interval has
        passed; three seconds of fed audio means several partial decodes and
        no final one."""
        engine = engine_factory()
        for chunk in _noise_chunks(3, n=16000):
            engine.process_audio(chunk.tobytes())
        assert engine.is_endpoint() is False
        assert engine.last_final_audio is None

    def test_the_second_utterance_gets_its_own_audio(self, engine_factory):
        engine = engine_factory(recognizer_text="one")
        first = _noise_chunks(2, seed=1)
        for chunk in first:
            engine.process_audio(chunk.tobytes())
        engine.finalize()
        engine.reset()

        second = _noise_chunks(2, n=4000, seed=2)
        for chunk in second:
            engine.process_audio(chunk.tobytes())
        engine.finalize()

        assert np.array_equal(engine.last_final_audio, np.concatenate(second))
