"""A config.toml that still holds a [lead_phrase] table (wh-lead-phrase-code-removal).

The lead phrase trial (wh-vad-lead-phrase-trial.1) read a [lead_phrase]
table from this provider's config.toml. The trial failed and its code was
removed, but a user who ran the trial may still keep the table in the
file. The provider must start with that table present and decode exactly
as it does without it.

The test runs main.py's `if __name__ == "__main__":` block with runpy,
because that block is the only code that turns config.toml into a
running server. The engine, the WebSocket forwarder, the audio capture,
and the process priority change are fakes. The config comes from the
TOML text below instead of the provider's own config.toml, and the fake
forwarder's start() raises KeyboardInterrupt, which the block catches as
a normal stop.
"""
from __future__ import annotations

import runpy
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import numpy as np

import main as parakeet_main
import shared_audio.capture
import shared_audio.thread_priority
import shared_stt.ws_forwarder
import sherpa_engine

SR = 16000
MAIN_PATH = Path(parakeet_main.__file__).resolve()
PROVIDER_CONFIG = MAIN_PATH.parent / "config.toml"
REAL_ENGINE = sherpa_engine.SherpaOfflineEngine

BASE_TOML = """
[model]
model_path = "C:/unused"
use_gpu = false

[client]
rate = 16000
chunk_ms = 30
"""


def _write_lead_wav(path: Path) -> Path:
    """A half-second tone standing in for a recorded lead phrase."""
    t = np.arange(int(0.5 * SR)) / SR
    frames = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(frames.tobytes())
    return path


def _leftover_toml(wav_path: Path) -> str:
    """BASE_TOML plus the table the trial's README told users to paste."""
    return BASE_TOML + f"""
[lead_phrase]
enabled = true
wav_path = "{wav_path.as_posix()}"
text = "However,"
compare = true
"""


def _run_main_block(monkeypatch, toml_text: str) -> dict:
    """Run main.py as __main__ with toml_text as its config.toml, and
    return the keyword arguments it passed to SherpaOfflineEngine."""
    import tomllib

    real_load = tomllib.load

    def load(f, *args, **kwargs):
        if Path(f.name).resolve() == PROVIDER_CONFIG:
            return tomllib.loads(toml_text)
        return real_load(f, *args, **kwargs)

    monkeypatch.setattr(tomllib, "load", load)
    monkeypatch.setattr(sys, "argv", [str(MAIN_PATH), "--ws-port", "8765"])
    monkeypatch.setattr(
        shared_audio.thread_priority, "elevate_current_process", lambda: None)

    engine_cls = MagicMock()
    monkeypatch.setattr(sherpa_engine, "SherpaOfflineEngine", engine_cls)

    forwarder_cls = MagicMock()
    forwarder_cls.return_value.start.side_effect = KeyboardInterrupt
    monkeypatch.setattr(shared_stt.ws_forwarder, "WSForwarder", forwarder_cls)

    capture = MagicMock()
    capture.get_stats = Mock(return_value={})
    monkeypatch.setattr(
        shared_audio.capture, "get_audio_provider", lambda *a, **kw: capture)

    assert PROVIDER_CONFIG.exists(), "load_config reads only an existing file"
    runpy.run_path(str(MAIN_PATH), run_name="__main__")

    forwarder_cls.return_value.start.assert_called_once()
    engine_cls.assert_called_once()
    return dict(engine_cls.call_args.kwargs)


def _result(tokens, timestamps):
    """A stream result shaped like sherpa-onnx's OfflineRecognitionResult."""
    return SimpleNamespace(
        text="".join(tokens).strip(),
        tokens=list(tokens),
        timestamps=list(timestamps),
    )


class _Stream:
    def __init__(self):
        self.audio = None
        self.result = None

    def accept_waveform(self, sample_rate, audio):
        self.audio = np.asarray(audio)


class _Recognizer:
    """Answers "undo." for the utterance alone, and "However, wrong." for
    any longer audio, such as the utterance with a lead phrase in front."""

    def __init__(self, utterance_samples: int):
        self._utterance_samples = utterance_samples

    def create_stream(self):
        return _Stream()

    def decode_stream(self, stream):
        if len(stream.audio) == self._utterance_samples:
            stream.result = _result([" und", "o", "."], [0.08, 0.24, 0.48])
        else:
            stream.result = _result(
                [" H", "owe", "ver", ",", " wr", "ong", "."],
                [0.0, 0.16, 0.40, 0.56, 0.88, 1.04, 1.28],
            )


def _decode(monkeypatch, engine_kwargs: dict) -> str:
    """Build the real engine from the captured arguments and decode one
    utterance with a fake recognizer."""
    monkeypatch.setattr(REAL_ENGINE, "_load_model", lambda *a, **kw: None)
    audio = (np.random.default_rng(3).standard_normal(SR) * 0.1).astype(
        np.float32)
    engine = REAL_ENGINE(**engine_kwargs)
    engine._recognizer = _Recognizer(len(audio))
    return engine._recognize(audio)


class TestLeftoverLeadPhraseTable:
    def test_the_provider_starts_and_builds_the_same_engine(
            self, monkeypatch, tmp_path):
        without_table = _run_main_block(monkeypatch, BASE_TOML)
        with_table = _run_main_block(
            monkeypatch, _leftover_toml(_write_lead_wav(tmp_path / "l.wav")))
        assert with_table == without_table

    def test_the_provider_decodes_the_same(self, monkeypatch, tmp_path):
        without_table = _run_main_block(monkeypatch, BASE_TOML)
        with_table = _run_main_block(
            monkeypatch, _leftover_toml(_write_lead_wav(tmp_path / "l.wav")))
        assert _decode(monkeypatch, without_table) == "undo."
        assert _decode(monkeypatch, with_table) == "undo."
