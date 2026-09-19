"""Integration tests for hint boosting, against the real installed model.

Everything here runs the PRODUCTION engine -- SherpaOfflineEngine, the
same class the provider builds at startup -- on the real ONNX files and
the real bpe.vocab. That is the difference from tests/test_hotwords.py,
which covers the same behaviour with a mocked sherpa_onnx and can only
prove what the engine PASSES, never what sherpa DOES with it.

An earlier version of this file built its own OfflineRecognizer with its
own keyword arguments, and passed tokens.txt as the bpe_vocab. That copy
of the production wiring is exactly how the defect this bead exists for
stayed invisible: the test's own recognizer had the same wrong argument
as the engine, so it agreed with the bug (wh-parakeet-hotword-vocab.3,
criterion E3).

What has to be on the machine, and what happens when it is not:

- A model directory carrying the INT8 file names the installer delivers
  (encoder.int8.onnx, decoder.int8.onnx, joiner.int8.onnx), tokens.txt,
  and bpe.vocab beside them. `_model_dir` looks in three places, in
  order, and every test here skips with the list when none of them
  qualifies. Nothing here writes to that directory.
- For the transcript test only, a real spoken recording at
  tests/fixtures/zwicky_dictation.wav (16 kHz mono, a sentence holding
  the word "Zwicky"). Record it with
  `uv run python tests/fixtures/record_zwicky_fixture.py`. The other
  tests do not need it and do not skip without it.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np
import pytest

import main as parakeet_main
from sherpa_engine import (
    BPE_VOCAB_FILENAME,
    SherpaOfflineEngine,
    bpe_vocab_rejection_reason,
)

FIXTURE = Path(__file__).parent / "fixtures" / "zwicky_dictation.wav"

#: The file names the installer delivers. The engine prefers these and
#: falls back to full-precision names, so their presence is what proves
#: a load took the int8 branch.
INT8_NAMES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")

#: Names a directory holding an installed int8 model for a machine whose
#: service resolution points somewhere else -- a dev box keeping several
#: models side by side, say. Read only by these tests.
MODEL_DIR_ENV = "WHEELHOUSE_TEST_PARAKEET_MODEL_DIR"


def _candidate_dirs() -> list[Path]:
    """Every directory that might hold an installed int8 model."""
    candidates: list[Path] = []

    from_env = os.environ.get(MODEL_DIR_ENV, "").strip()
    if from_env:
        candidates.append(Path(from_env))

    # The service's own chain: override file > tracked config > coded
    # default. Passing an empty config is what the provider effectively
    # does on a shipped machine, where the tracked model_path is empty.
    resolved = parakeet_main._resolve_model_path({"model": {}})["model"][
        "model_path"
    ]
    if resolved:
        candidates.append(Path(resolved))

    # The installer's own destination, for a machine whose override file
    # was lost. DEFAULT_MODEL_DIRNAME is the archive the installer
    # downloads, so this path is the shipped arrangement.
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )

    return candidates


def _qualifies(path: Path) -> bool:
    """True when this directory can exercise the int8 + bpe.vocab path."""
    needed = [*INT8_NAMES, "tokens.txt", BPE_VOCAB_FILENAME]
    return all((path / name).exists() for name in needed)


def _model_dir() -> Path:
    """The installed int8 model to test against, or skip saying why."""
    candidates = _candidate_dirs()
    for candidate in candidates:
        if _qualifies(candidate):
            return candidate
    looked_in = ", ".join(str(c) for c in candidates) or (
        "(nowhere: no model path resolved and no LOCALAPPDATA)"
    )
    pytest.skip(
        "no installed int8 Parakeet model with a bpe.vocab beside it. "
        f"Needs {', '.join([*INT8_NAMES, 'tokens.txt', BPE_VOCAB_FILENAME])} "
        f"in one directory. Looked in: {looked_in}. Set {MODEL_DIR_ENV} to "
        "point at one."
    )


@pytest.fixture(scope="module")
def model_dir() -> Path:
    return _model_dir()


@pytest.fixture
def hotwords_file(tmp_path: Path) -> str:
    """A hotwords file written here, not read from any shipped config."""
    path = tmp_path / "hotwords.txt"
    path.write_text("Zwicky\n", encoding="utf-8")
    return str(path)


def _transcribe(model_dir: Path, audio: np.ndarray, hotwords: str | None) -> str:
    """The production engine's transcript of one whole recording.

    The engine is a streaming one: it re-infers periodically and
    finalizes on trailing silence. This measures the transcript of the
    WHOLE recording, so the buffer cap is set above the recording's own
    length and the endpoint silence above its duration -- an early
    endpoint would decode a prefix and the comparison would be between
    two different amounts of audio. Streaming and endpoint behaviour
    have their own tests in tests/test_sherpa_engine.py.
    """
    seconds = len(audio) / 16000.0
    engine = SherpaOfflineEngine(
        model_path=str(model_dir),
        hotwords_file=hotwords,
        hotwords_score=2.0,
        max_buffer_duration_s=seconds + 60.0,
        endpoint_silence_ms=int((seconds + 60.0) * 1000),
    )
    try:
        engine.process_audio(audio.astype(np.float32).tobytes())
        engine.finalize()
        return engine.get_result()
    finally:
        engine.cleanup()


def test_the_real_vocabulary_is_accepted_and_the_real_tokens_file_is_not(
    model_dir: Path,
) -> None:
    """The check that stands between the two files, on the real pair.

    tests/test_hotwords.py proves this against constructed files. This
    proves it against the exact bytes the installer delivers, which is
    the pair that actually decided the defect: tokens.txt has two
    columns and passes a shape check, and its second column is an
    integer id, so only the score test separates them.
    """
    tokens = model_dir / "tokens.txt"
    vocabulary = model_dir / BPE_VOCAB_FILENAME

    assert bpe_vocab_rejection_reason(vocabulary, tokens) is None, (
        f"the installed {BPE_VOCAB_FILENAME} was refused: "
        f"{bpe_vocab_rejection_reason(vocabulary, tokens)}"
    )

    reason = bpe_vocab_rejection_reason(tokens, tokens)
    assert reason is not None, (
        "the installed tokens.txt was accepted as a sentencepiece "
        "vocabulary -- that is the defect this bead exists for"
    )


def test_the_production_engine_turns_boosting_on_with_the_installed_vocabulary(
    model_dir: Path, hotwords_file: str
) -> None:
    """The engine builds a real recognizer with hint boosting ACTIVE.

    This is the whole point of the bead. Before bpe.vocab was generated
    and delivered there was no file at that name, so the engine reported
    boosting requested and off, with the missing-file reason. Nothing
    below is mocked: sherpa really loads the int8 trio, really reads
    bpe.vocab, and really builds a modified_beam_search recognizer.
    """
    engine = SherpaOfflineEngine(
        model_path=str(model_dir),
        hotwords_file=hotwords_file,
        hotwords_score=2.0,
    )
    try:
        status = engine.hotwords_status
        assert status.requested, "the engine did not record the request"
        assert status.active, (
            f"boosting was requested and did not come on: {status.detail}"
        )
        assert status.detail == "", (
            f"active boosting carried a reason: {status.detail!r}"
        )
        assert engine._recognizer is not None, "no recognizer was built"
    finally:
        engine.cleanup()


def test_the_engine_loads_the_int8_files_the_installer_delivers(
    model_dir: Path, hotwords_file: str
) -> None:
    """The int8 branch of the loader reaches a working recognizer.

    The engine prefers encoder.int8.onnx and falls back to encoder.onnx,
    so a machine carrying only full-precision files would exercise the
    other branch and prove nothing about what ships. `_model_dir` refuses
    such a directory; this test states that requirement where a reader of
    the failure will see it, and then proves the int8 trio and bpe.vocab
    load TOGETHER -- a combination no mocked test can reach.
    """
    for name in INT8_NAMES:
        assert (model_dir / name).exists(), (
            f"{model_dir} does not carry {name}; the loader would fall "
            "back to the full-precision names and this test would not "
            "be measuring what the installer delivers"
        )

    engine = SherpaOfflineEngine(
        model_path=str(model_dir),
        hotwords_file=hotwords_file,
        hotwords_score=2.0,
    )
    try:
        assert engine._recognizer is not None
        assert engine.hotwords_status.active
    finally:
        engine.cleanup()


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason=(
        "fixture audio missing -- record with "
        "tests/fixtures/record_zwicky_fixture.py"
    ),
)
def test_a_hint_changes_what_the_production_engine_transcribes(
    model_dir: Path, hotwords_file: str
) -> None:
    """A hinted word the plain engine misses comes back with the hint.

    The assertion is on the DELTA, not the absolute (wh-q33mj.1.5): if
    the unhinted engine already transcribes the word, a hinted run that
    also transcribes it proves nothing, and a sherpa upgrade that
    silently stopped honouring the arguments would be invisible. So the
    unhinted run is measured first and the test skips when it is not
    discriminating.
    """
    with wave.open(str(FIXTURE), "rb") as w:
        assert w.getframerate() == 16000, "fixture must be 16 kHz"
        audio = (
            np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(
                np.float32
            )
            / 32768.0
        )

    baseline = _transcribe(model_dir, audio, None)
    if "zwicky" in baseline.lower():
        pytest.skip(
            "fixture not discriminating: the engine without hints already "
            f"transcribes Zwicky ({baseline!r}); re-record a harder "
            "pronunciation"
        )

    hinted = _transcribe(model_dir, audio, hotwords_file)
    assert "zwicky" in hinted.lower(), (
        f"hinted transcript: {hinted!r}; without hints it was {baseline!r}"
    )
