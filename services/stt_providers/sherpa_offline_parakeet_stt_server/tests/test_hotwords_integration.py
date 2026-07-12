"""Integration test: hotword biasing makes Parakeet recognize 'Zwicky'
(wh-mu6l5).

Needs a real spoken fixture at tests/fixtures/zwicky_dictation.wav (16 kHz
mono, a sentence containing the word 'Zwicky') plus the real model on disk.
Skips gracefully when either is missing. Record the fixture with:

    uv run python tests/fixtures/record_zwicky_fixture.py
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import main as parakeet_main

FIXTURE = Path(__file__).parent / "fixtures" / "zwicky_dictation.wav"

# Same resolution chain the service itself uses (override file > tracked
# config > coded LOCALAPPDATA default), so the test finds the model wherever
# this machine actually keeps it (wh-797.6.8).
_resolved = parakeet_main._resolve_model_path({"model": {}})["model"]["model_path"]
MODEL_DIR = Path(_resolved) if _resolved else None

pytestmark = [
    pytest.mark.skipif(
        not FIXTURE.exists(),
        reason="fixture audio missing -- record with tests/fixtures/record_zwicky_fixture.py",
    ),
    pytest.mark.skipif(
        MODEL_DIR is None or not MODEL_DIR.exists(),
        reason="Parakeet model not on this machine",
    ),
]


def _decode(audio: np.ndarray, hotwords_file: str | None) -> str:
    import os

    import sherpa_onnx

    kwargs: dict[str, Any] = dict(
        tokens=str(MODEL_DIR / "tokens.txt"),
        encoder=str(MODEL_DIR / "encoder.onnx"),
        decoder=str(MODEL_DIR / "decoder.onnx"),
        joiner=str(MODEL_DIR / "joiner.onnx"),
        provider="cpu",
        num_threads=4,
        sample_rate=16000,
        feature_dim=128,
        model_type="nemo_transducer",
    )
    if hotwords_file:
        kwargs.update(
            hotwords_file=hotwords_file,
            hotwords_score=2.0,
            modeling_unit="bpe",
            bpe_vocab=str(MODEL_DIR / "tokens.txt"),
            decoding_method="modified_beam_search",
        )
    orig = os.getcwd()
    os.chdir(MODEL_DIR)
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)
    finally:
        os.chdir(orig)
    stream = rec.create_stream()
    stream.accept_waveform(16000, audio)
    rec.decode_stream(stream)
    return stream.result.text


def test_zwicky_recognized_with_hotwords(tmp_path):
    with wave.open(str(FIXTURE), "rb") as w:
        assert w.getframerate() == 16000, "fixture must be 16 kHz"
        audio = (
            np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(
                np.float32
            )
            / 32768.0
        )

    # Baseline control (wh-q33mj.1.5): if plain greedy Parakeet already
    # gets "Zwicky" from this fixture, the biased run proves nothing --
    # and a sherpa upgrade that silently stops honoring the hotwords
    # kwargs would be invisible. Pin the DELTA, not the absolute.
    baseline = _decode(audio, None)
    if "zwicky" in baseline.lower():
        pytest.skip(
            "fixture not discriminating: baseline (no hotwords) already "
            f"transcribes Zwicky ({baseline!r}); re-record a harder "
            "pronunciation"
        )

    hotwords = tmp_path / "hotwords.txt"
    hotwords.write_text("Zwicky\n", encoding="utf-8")

    text = _decode(audio, str(hotwords))
    assert "zwicky" in text.lower(), (
        f"hotword-biased transcript: {text!r}; baseline was {baseline!r}"
    )
