"""The lead-word join of the Parakeet provider (wh-first-sound-lost.22).

With the key `lead_word_path` set in the [engine] section of config.toml,
the engine puts a short recorded word ("hello" and its 50 ms tail) in front
of the user's audio on every decode pass, removes that word from the raw
token text, and falls back to a plain decode of the user's audio when the
text does not start with the word. With the key empty or absent, the engine
decodes exactly the audio it receives.

The sherpa recognizer is a fake that records every array it decodes and
answers from a function of that array. `_load_model` is patched to a
no-op, so these tests never load the ONNX model.
"""
from __future__ import annotations

import logging
import tomllib
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import sherpa_engine
from sherpa_engine import SherpaOfflineEngine
from tests.test_leftover_lead_phrase_table import (
    BASE_TOML,
    PROVIDER_CONFIG,
    _run_main_block,
)

SR = 16000
LEAD_SAMPLES = 800


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int16_wave(count: int = LEAD_SAMPLES, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(-20000, 20000, size=count, dtype=np.int16)


def _write_wav(
    path: Path,
    samples: np.ndarray | None = None,
    *,
    rate: int = SR,
    channels: int = 1,
    width: int = 2,
) -> Path:
    if samples is None:
        samples = _int16_wave()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def _expected_lead(samples: np.ndarray) -> np.ndarray:
    return samples.astype(np.float32) / np.float32(32768.0)


class _Stream:
    def __init__(self):
        self.audio = None
        self.result = SimpleNamespace(tokens=[], text="")

    def accept_waveform(self, sample_rate, audio):
        self.audio = np.asarray(audio)


class _Recognizer:
    """Records the array of every decode. `answer(audio)` gives the tokens."""

    def __init__(self, answer):
        self.answer = answer
        self.inputs: list[np.ndarray] = []

    def create_stream(self):
        return _Stream()

    def decode_stream(self, stream):
        self.inputs.append(stream.audio)
        stream.result.tokens = list(self.answer(stream.audio))


def _starts_with(audio: np.ndarray, lead: np.ndarray) -> bool:
    return len(audio) >= len(lead) and np.array_equal(audio[: len(lead)], lead)


@pytest.fixture
def build(monkeypatch):
    """Build an engine with a fake recognizer; return (engine, recognizer)."""
    monkeypatch.setattr(
        SherpaOfflineEngine, "_load_model", lambda *a, **kw: None
    )

    def _build(answer, **engine_kwargs):
        engine = SherpaOfflineEngine(model_path="/nonexistent", **engine_kwargs)
        recognizer = _Recognizer(answer)
        engine._recognizer = recognizer
        return engine, recognizer

    return _build


def _chunks(count: int = 2, n: int = 4000, seed: int = 7):
    rng = np.random.default_rng(seed)
    return [
        (rng.standard_normal(n) * 0.1).astype(np.float32) for _ in range(count)
    ]


def _feed(engine, chunks):
    for chunk in chunks:
        engine.process_audio(chunk.tobytes())


def _engine_records(caplog):
    return [r for r in caplog.records if r.name == "sherpa_engine"]


# ---------------------------------------------------------------------------
# The removal rule
# ---------------------------------------------------------------------------

class TestRemoveLeadWord:
    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("Hello. Delete all.", ("Delete all.", True), id="capital-period"),
            pytest.param("hello, go top", ("go top", True), id="lowercase-comma"),
            pytest.param("HELLO - Go", ("Go", True), id="uppercase-dash"),
            pytest.param("Hello 5 pm", ("5 pm", True), id="digit-after-space"),
            pytest.param("Hello5 pm", ("5 pm", True), id="digit-glued"),
            pytest.param(". Hello. Delete", ("Delete", True), id="leading-punctuation"),
            pytest.param("Hello.", ("", True), id="word-and-period"),
            pytest.param("Hello", ("", True), id="word-alone"),
            pytest.param("Hello. hello there", ("hello there", True), id="second-hello-stays"),
            pytest.param("Hallo. Delete", ("Hallo. Delete", False), id="other-word"),
            pytest.param("Hellopodiatrist", ("Hellopodiatrist", False), id="longer-word"),
            pytest.param("Say hello", ("Say hello", False), id="word-not-first"),
            pytest.param("Hello's here", ("Hello's here", False), id="apostrophe-word"),
            pytest.param("", ("", False), id="empty-text"),
            pytest.param(None, ("", False), id="none-text"),
        ],
    )
    def test_the_rule(self, text, expected):
        assert sherpa_engine.remove_lead_word(text, "hello") == expected


# ---------------------------------------------------------------------------
# Reading the lead-word file
# ---------------------------------------------------------------------------

class TestLoadLeadWordAudio:
    def test_a_good_file_gives_a_read_only_float32_array(self, tmp_path):
        samples = _int16_wave()
        path = _write_wav(tmp_path / "w.wav", samples)
        audio = sherpa_engine.load_lead_word_audio(str(path), SR)
        assert audio.dtype == np.float32
        assert np.array_equal(audio, _expected_lead(samples))
        assert audio.flags.writeable is False

    def test_the_file_is_used_as_it_is(self, tmp_path):
        samples = np.zeros(LEAD_SAMPLES, dtype=np.int16)
        samples[0] = 32767
        samples[-1] = -32768
        audio = sherpa_engine.load_lead_word_audio(
            str(_write_wav(tmp_path / "w.wav", samples)), SR)
        assert len(audio) == LEAD_SAMPLES
        assert audio[0] == np.float32(32767 / 32768)
        assert audio[-1] == np.float32(-1.0)

    def test_a_one_sample_file_is_good(self, tmp_path):
        audio = sherpa_engine.load_lead_word_audio(
            str(_write_wav(tmp_path / "w.wav", _int16_wave(1))), SR)
        assert len(audio) == 1

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"rate": 44100},
            {"rate": 8000},
            {"channels": 2},
            {"width": 1},
            {"width": 4},
        ],
    )
    def test_a_wrong_format_is_refused(self, tmp_path, kwargs):
        samples = np.zeros(400, dtype={1: np.uint8, 2: np.int16, 4: np.int32}[
            kwargs.get("width", 2)])
        path = _write_wav(tmp_path / "w.wav", samples, **kwargs)
        with pytest.raises(sherpa_engine.LeadWordError):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_an_empty_file_is_refused(self, tmp_path):
        path = _write_wav(tmp_path / "w.wav", np.zeros(0, dtype=np.int16))
        with pytest.raises(sherpa_engine.LeadWordError, match="no samples"):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_zero_byte_file_is_refused(self, tmp_path):
        # wave raises EOFError, not wave.Error, for a file with no header.
        path = tmp_path / "w.wav"
        path.write_bytes(b"")
        with pytest.raises(sherpa_engine.LeadWordError, match="EOFError"):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_corrupt_fmt_chunk_size_is_refused(self, tmp_path):
        # Bytes 16-19 hold the fmt chunk size. A wrong size makes wave's
        # chunk reader raise RuntimeError, not wave.Error. A byte-by-byte
        # scan of a small good file found no other exception type.
        path = _write_wav(tmp_path / "w.wav")
        data = bytearray(path.read_bytes())
        data[16] = 0x7F
        path.write_bytes(bytes(data))
        with pytest.raises(sherpa_engine.LeadWordError, match="RuntimeError"):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_wrong_format_message_names_the_format_it_found(self, tmp_path):
        samples = np.zeros(400, dtype=np.uint8)
        path = _write_wav(
            tmp_path / "w.wav", samples, rate=44100, channels=2, width=1)
        with pytest.raises(sherpa_engine.LeadWordError) as excinfo:
            sherpa_engine.load_lead_word_audio(str(path), SR)
        message = str(excinfo.value)
        assert "44100 Hz" in message
        assert "2 channel" in message
        assert "8-bit" in message
        assert "16000 Hz, 1 channel, 16-bit" in message

    @pytest.mark.parametrize(
        "kwargs, dtype",
        [
            ({"width": 1}, np.uint8),
            ({"width": 4}, np.int32),
            ({"channels": 2}, np.int16),
            ({"rate": 44100}, np.int16),
        ],
        ids=["8-bit", "32-bit", "stereo", "44100-hz"],
    )
    def test_a_wrong_format_is_refused_as_a_format_not_as_cut_off(
        self, tmp_path, kwargs, dtype
    ):
        # For a wrong width or channel count the byte-count check would
        # refuse the file too, with a message that blames a cut-off file.
        # The format check must refuse first.
        path = _write_wav(tmp_path / "w.wav", np.zeros(400, dtype=dtype), **kwargs)
        with pytest.raises(sherpa_engine.LeadWordError, match="its format is"):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_file_that_is_not_a_wav_is_refused(self, tmp_path):
        path = tmp_path / "w.wav"
        path.write_bytes(b"this is not a wave file at all")
        with pytest.raises(sherpa_engine.LeadWordError):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_cut_off_file_is_refused(self, tmp_path):
        path = _write_wav(tmp_path / "w.wav")
        data = path.read_bytes()
        path.write_bytes(data[:-100])
        with pytest.raises(sherpa_engine.LeadWordError, match="cut off"):
            sherpa_engine.load_lead_word_audio(str(path), SR)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(sherpa_engine.LeadWordError, match="none.wav"):
            sherpa_engine.load_lead_word_audio(str(tmp_path / "none.wav"), SR)

    def test_a_folder_is_refused(self, tmp_path):
        with pytest.raises(sherpa_engine.LeadWordError):
            sherpa_engine.load_lead_word_audio(str(tmp_path), SR)

    def test_a_value_that_is_not_text_is_refused(self):
        with pytest.raises(sherpa_engine.LeadWordError):
            sherpa_engine.load_lead_word_audio(5, SR)

    def test_a_path_with_a_null_character_is_refused(self, tmp_path):
        # TOML "\u0000" gives this path. open() raises ValueError for it.
        with pytest.raises(sherpa_engine.LeadWordError):
            sherpa_engine.load_lead_word_audio(str(tmp_path) + "\x00.wav", SR)


# ---------------------------------------------------------------------------
# Join off: the decode input is the audio the engine received
# ---------------------------------------------------------------------------

class TestJoinOff:
    @pytest.mark.parametrize(
        "kwargs", [{}, {"lead_word_path": ""}], ids=["absent", "empty"]
    )
    def test_the_final_decode_input_is_unchanged(self, build, kwargs, caplog):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        engine, recognizer = build(lambda audio: [" Hello. Delete"], **kwargs)
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        assert len(recognizer.inputs) == 1
        assert np.array_equal(recognizer.inputs[0], np.concatenate(chunks))
        assert _engine_records(caplog) == []

    @pytest.mark.parametrize(
        "kwargs", [{}, {"lead_word_path": ""}], ids=["absent", "empty"]
    )
    def test_the_partial_decode_input_is_unchanged(self, build, kwargs):
        engine, recognizer = build(lambda audio: [" word"], **kwargs)
        chunks = _chunks(1)
        _feed(engine, chunks)
        engine._run_inference()
        assert len(recognizer.inputs) == 1
        assert np.array_equal(recognizer.inputs[0], chunks[0])

    def test_the_text_is_not_changed_when_the_join_is_off(self, build):
        engine, _ = build(
            lambda audio: [" Hello", ". Delete"], lead_word_path="")
        assert engine._recognize(_chunks(1)[0]) == "Hello. Delete"


# ---------------------------------------------------------------------------
# Join on
# ---------------------------------------------------------------------------

@pytest.fixture
def lead_file(tmp_path):
    samples = _int16_wave()
    return _write_wav(tmp_path / "lead.wav", samples), _expected_lead(samples)


class TestJoinOn:
    def test_the_final_pass_puts_the_word_in_front_with_no_gap(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", ". Delete", " all", "."],
            lead_word_path=str(path),
        )
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        user = np.concatenate(chunks)
        assert len(recognizer.inputs) == 1
        joined = recognizer.inputs[0]
        assert joined.dtype == np.float32
        assert len(joined) == len(lead) + len(user)
        assert np.array_equal(joined[: len(lead)], lead)
        assert np.array_equal(joined[len(lead):], user)

    def test_the_word_is_removed_from_the_final_text(self, build, lead_file):
        path, _ = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", ". Delete", " all", "."],
            lead_word_path=str(path),
        )
        _feed(engine, _chunks())
        engine.finalize()
        assert engine.get_result() == "delete all"
        assert len(recognizer.inputs) == 1

    def test_the_partial_pass_joins_and_removes_the_word(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", ". Delete", " all"],
            lead_word_path=str(path),
        )
        chunks = _chunks(1)
        _feed(engine, chunks)
        engine._run_inference()
        assert len(recognizer.inputs) == 1
        assert np.array_equal(
            recognizer.inputs[0], np.concatenate([lead, chunks[0]]))
        assert engine._prev_words == ["delete", "all"]

    def test_every_periodic_pass_inside_process_audio_joins(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", ". Delete", " all"],
            lead_word_path=str(path),
        )
        _feed(engine, _chunks(3, n=16000))
        assert engine.is_endpoint() is False
        assert len(recognizer.inputs) >= 2
        assert all(_starts_with(inp, lead) for inp in recognizer.inputs)
        assert engine.get_result() == "delete all"

    def test_the_removal_runs_before_normalization(
        self, build, lead_file, monkeypatch
    ):
        path, _ = lead_file
        seen = []

        def record(text):
            seen.append(text)
            return text

        monkeypatch.setattr(sherpa_engine, "normalize_transcript", record)
        engine, _ = build(
            lambda audio: [" Hello", ". Delete", " all", "."],
            lead_word_path=str(path),
        )
        _feed(engine, _chunks())
        engine.finalize()
        assert seen == ["Delete all."]

    def test_the_lead_array_is_the_same_object_on_every_pass(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, _ = build(lambda audio: [" Hello", " x"], lead_word_path=str(path))
        first = engine._lead_audio
        _feed(engine, _chunks(1))
        engine._run_inference()
        engine.finalize()
        engine.reset()
        assert engine._lead_audio is first
        assert np.array_equal(first, lead)

    def test_no_word_found_decodes_the_users_audio_again_alone(
        self, build, lead_file
    ):
        path, lead = lead_file

        def answer(audio):
            if _starts_with(audio, lead):
                return [" Hallo", ". Delete"]
            return [" plain", " text"]

        engine, recognizer = build(answer, lead_word_path=str(path))
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        user = np.concatenate(chunks)
        assert len(recognizer.inputs) == 2
        assert np.array_equal(recognizer.inputs[0], np.concatenate([lead, user]))
        assert np.array_equal(recognizer.inputs[1], user)
        assert engine.get_result() == "plain text"

    def test_no_word_found_on_a_partial_pass_uses_the_second_decode(
        self, build, lead_file
    ):
        path, lead = lead_file

        def answer(audio):
            if _starts_with(audio, lead):
                return [" Hellopodiatrist"]
            return [" go", " top"]

        engine, recognizer = build(answer, lead_word_path=str(path))
        chunks = _chunks(1)
        _feed(engine, chunks)
        engine._run_inference()
        assert len(recognizer.inputs) == 2
        assert np.array_equal(recognizer.inputs[1], chunks[0])
        assert engine._prev_words == ["go", "top"]

    def test_a_found_word_with_nothing_after_it_gives_empty_text_and_one_decode(
        self, build, lead_file
    ):
        path, _ = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", "."], lead_word_path=str(path))
        assert engine._recognize(_chunks(1)[0]) == ""
        assert len(recognizer.inputs) == 1

    def test_the_second_decode_is_a_method_a_later_feature_can_call(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(lambda audio: [" a", " b"],
                                   lead_word_path=str(path))
        user = _chunks(1)[0]
        assert engine._decode_plain(user) == "a b"
        assert len(recognizer.inputs) == 1
        assert np.array_equal(recognizer.inputs[0], user)

    def test_the_saver_gets_the_users_audio_without_the_word(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", ". Delete"], lead_word_path=str(path))
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        user = np.concatenate(chunks)
        assert np.array_equal(engine.last_final_audio, user)
        assert len(recognizer.inputs[0]) == len(lead) + len(user)

    def test_the_saver_gets_the_users_audio_after_a_fallback_decode(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, _ = build(
            lambda audio: [" Hallo"] if _starts_with(audio, lead) else [" x"],
            lead_word_path=str(path),
        )
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        assert np.array_equal(engine.last_final_audio, np.concatenate(chunks))


# ---------------------------------------------------------------------------
# Set the word of a running engine (wh-first-sound-lost.23, item 15)
# ---------------------------------------------------------------------------

def _new_word(value: float = 0.25, count: int = 400) -> np.ndarray:
    word = np.full(count, value, dtype=np.float32)
    word.flags.writeable = False
    return word


class TestHasLeadWord:
    """The public read-only answer to "does the engine hold a lead word"
    (wh-first-sound-lost.23): the training-word controller uses it instead of
    reading the private attribute."""

    def test_follows_set_lead_word(self, build, lead_file):
        path, _lead = lead_file
        engine, _recognizer = build(lambda audio: [" Hello"])
        assert engine.has_lead_word() is False
        engine.set_lead_word(_new_word())
        assert engine.has_lead_word() is True
        engine.set_lead_word(None)
        assert engine.has_lead_word() is False

    def test_true_after_a_start_time_load(self, build, lead_file):
        path, _lead = lead_file
        engine, _recognizer = build(
            lambda audio: [" Hello"], lead_word_path=str(path))
        assert engine.has_lead_word() is True


class TestSetLeadWord:
    def test_a_load_during_an_utterance_changes_no_pass_of_it(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", " go"], lead_word_path=str(path))
        _feed(engine, _chunks(1))
        engine._run_inference()
        engine.set_lead_word(_new_word())
        _feed(engine, _chunks(1, seed=9))
        engine._run_inference()
        engine.finalize()
        assert len(recognizer.inputs) == 3
        assert all(_starts_with(inp, lead) for inp in recognizer.inputs)

    def test_the_next_utterance_uses_the_new_word(self, build, lead_file):
        path, lead = lead_file
        new = _new_word()
        engine, recognizer = build(
            lambda audio: [" Hello", " go"], lead_word_path=str(path))
        _feed(engine, _chunks(1))
        engine._run_inference()
        engine.set_lead_word(new)
        engine.finalize()
        engine.reset()
        _feed(engine, _chunks(1))
        engine._run_inference()
        assert _starts_with(recognizer.inputs[-1], new)
        assert not _starts_with(recognizer.inputs[-1], lead)

    def test_a_load_before_the_first_pass_applies_to_that_utterance(
        self, build, lead_file
    ):
        path, _ = lead_file
        new = _new_word()
        engine, recognizer = build(
            lambda audio: [" Hello", " go"], lead_word_path=str(path))
        _feed(engine, _chunks(1))
        engine.set_lead_word(new)
        engine._run_inference()
        assert _starts_with(recognizer.inputs[0], new)

    def test_a_word_set_while_the_join_was_off_waits_for_the_next_utterance(
        self, build
    ):
        engine, recognizer = build(lambda audio: [" Hello", " go"])
        chunks = _chunks(1)
        _feed(engine, chunks)
        engine._run_inference()
        engine.set_lead_word(_new_word())
        engine._run_inference()
        assert all(len(inp) == len(chunks[0]) for inp in recognizer.inputs)
        engine.reset()
        _feed(engine, chunks)
        engine._run_inference()
        assert len(recognizer.inputs[-1]) == len(chunks[0]) + 400

    def test_none_ends_the_join_from_the_next_utterance_on(
        self, build, lead_file
    ):
        path, lead = lead_file
        engine, recognizer = build(
            lambda audio: [" Hello", " go"], lead_word_path=str(path))
        chunks = _chunks(1)
        _feed(engine, chunks)
        engine._run_inference()
        engine.set_lead_word(None)
        engine._run_inference()
        assert _starts_with(recognizer.inputs[1], lead)
        engine.reset()
        _feed(engine, chunks)
        engine._run_inference()
        assert np.array_equal(recognizer.inputs[-1], chunks[0])

    def test_the_word_is_set_by_one_attribute_assignment(self, build):
        engine, _ = build(lambda audio: [" go"])
        new = _new_word()
        engine.set_lead_word(new)
        assert engine._lead_audio is new

    def test_the_start_time_load_goes_through_the_method(
        self, build, lead_file, monkeypatch
    ):
        path, lead = lead_file
        calls = []
        original = SherpaOfflineEngine.set_lead_word

        def spy(self, audio):
            calls.append(audio)
            original(self, audio)

        monkeypatch.setattr(SherpaOfflineEngine, "set_lead_word", spy)
        engine, _ = build(lambda audio: [" go"], lead_word_path=str(path))
        assert len(calls) == 1
        assert np.array_equal(calls[0], lead)
        assert engine._lead_audio is calls[0]


# ---------------------------------------------------------------------------
# A file the provider cannot use: join off, one log line, no stop
# ---------------------------------------------------------------------------

class TestUnusableFile:
    def test_a_missing_file_keeps_the_join_off_with_one_log_line(
        self, build, tmp_path, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        missing = tmp_path / "none.wav"
        engine, recognizer = build(
            lambda audio: [" Hello", ". x"], lead_word_path=str(missing))
        records = _engine_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert str(missing) in records[0].getMessage()
        assert "cannot read it" in records[0].getMessage()
        assert engine._lead_audio is None
        chunks = _chunks()
        _feed(engine, chunks)
        engine.finalize()
        assert len(recognizer.inputs) == 1
        assert np.array_equal(recognizer.inputs[0], np.concatenate(chunks))
        assert len(_engine_records(caplog)) == 1

    @pytest.mark.parametrize(
        "kwargs", [{"rate": 44100}, {"channels": 2}, {"width": 1}]
    )
    def test_a_wrong_format_keeps_the_join_off_with_one_log_line(
        self, build, tmp_path, caplog, kwargs
    ):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        width = kwargs.get("width", 2)
        samples = np.zeros(400, dtype=np.uint8 if width == 1 else np.int16)
        path = _write_wav(tmp_path / "w.wav", samples, **kwargs)
        engine, _ = build(lambda audio: [" x"], lead_word_path=str(path))
        records = _engine_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert engine._lead_audio is None

    def test_a_file_that_is_not_a_wav_keeps_the_join_off(
        self, build, tmp_path, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        path = tmp_path / "w.wav"
        path.write_bytes(b"not a wave file")
        engine, _ = build(lambda audio: [" x"], lead_word_path=str(path))
        assert len(_engine_records(caplog)) == 1
        assert engine._lead_audio is None

    def test_a_value_that_is_not_text_keeps_the_join_off(self, build, caplog):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        engine, _ = build(lambda audio: [" x"], lead_word_path=5)
        assert len(_engine_records(caplog)) == 1
        assert engine._lead_audio is None

    @pytest.mark.parametrize("value", [False, 0])
    def test_a_false_value_that_is_not_text_still_warns(
        self, build, caplog, value
    ):
        # A TOML `false` or `0` is not the empty string, so it is not the
        # normal "off". The user gets the one warning, not silence.
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        engine, _ = build(lambda audio: [" x"], lead_word_path=value)
        records = _engine_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert engine._lead_audio is None

    def test_a_good_file_turns_the_join_on_with_no_warning(
        self, build, lead_file, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        path, lead = lead_file
        engine, _ = build(lambda audio: [" x"], lead_word_path=str(path))
        assert np.array_equal(engine._lead_audio, lead)
        assert all(r.levelno < logging.WARNING for r in _engine_records(caplog))

    def test_a_good_file_writes_one_info_line_with_the_size_and_path(
        self, build, lead_file, caplog
    ):
        caplog.set_level(logging.DEBUG, logger="sherpa_engine")
        path, _ = lead_file
        build(lambda audio: [" x"], lead_word_path=str(path))
        records = _engine_records(caplog)
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        message = records[0].getMessage()
        assert "Lead-word join is on" in message
        assert f"{LEAD_SAMPLES} samples" in message
        assert str(path) in message

    def test_the_file_is_checked_against_the_engines_sample_rate(
        self, build, tmp_path
    ):
        path = _write_wav(tmp_path / "w8.wav", rate=8000)
        engine, _ = build(lambda audio: [" x"], sample_rate=8000,
                          lead_word_path=str(path))
        assert engine._lead_audio is not None
        assert len(engine._lead_audio) == LEAD_SAMPLES


# ---------------------------------------------------------------------------
# The key in config.toml
# ---------------------------------------------------------------------------

class TestConfigKey:
    def test_the_tracked_config_declares_the_key_empty(self):
        with open(PROVIDER_CONFIG, "rb") as f:
            config = tomllib.load(f)
        assert config["engine"]["lead_word_path"] == ""

    def test_main_passes_the_key_to_the_engine(self, monkeypatch):
        toml_text = BASE_TOML + "\n[engine]\nlead_word_path = 'C:\\x\\hello.wav'\n"
        kwargs = _run_main_block(monkeypatch, toml_text)
        assert kwargs["lead_word_path"] == "C:\\x\\hello.wav"

    def test_main_passes_empty_when_the_key_is_absent(self, monkeypatch):
        kwargs = _run_main_block(monkeypatch, BASE_TOML)
        assert kwargs["lead_word_path"] == ""
