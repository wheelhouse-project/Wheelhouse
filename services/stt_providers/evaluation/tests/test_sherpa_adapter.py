"""Tests for the sherpa-onnx benchmark adapter's language option.

Nemotron 3.5 ASR is a multilingual streaming model. sherpa-onnx selects its
language per stream through OnlineStream.set_option("language", ...); with
no option set, the model detects the language itself. The benchmark needs
to force a language so that its numbers match an English-only deployment.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import run_benchmark
from adapters.sherpa_adapter import SherpaAdapter

ONLINE_DIR = "/fake/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-1120ms-int8-2026-06-11"
OFFLINE_DIR = "/fake/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"


@pytest.fixture
def fake_recognizer():
    """Replace the sherpa-onnx recognizers and the model-file lookup."""
    recognizer = MagicMock()
    recognizer.is_ready.return_value = False
    recognizer.get_result.return_value = "hello"
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch(
            "adapters.sherpa_adapter.sherpa_onnx.OnlineRecognizer.from_transducer",
            return_value=recognizer,
        ),
        patch.object(SherpaAdapter, "_load_offline_recognizer", return_value=MagicMock()),
    ):
        yield recognizer


def _transcribe(adapter: SherpaAdapter) -> None:
    adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)


def test_language_is_set_on_each_online_stream(fake_recognizer):
    adapter = SherpaAdapter(model_path=ONLINE_DIR, language="en-US")
    _transcribe(adapter)
    stream = fake_recognizer.create_stream.return_value
    stream.set_option.assert_called_once_with("language", "en-US")


def test_no_language_leaves_the_stream_option_unset(fake_recognizer):
    adapter = SherpaAdapter(model_path=ONLINE_DIR)
    _transcribe(adapter)
    stream = fake_recognizer.create_stream.return_value
    stream.set_option.assert_not_called()


def test_language_is_part_of_the_default_name(fake_recognizer):
    """Result files are named after the adapter, so runs must not collide."""
    adapter = SherpaAdapter(model_path=ONLINE_DIR, language="en-US")
    assert adapter.name.endswith("_en-US")


def test_language_on_an_offline_model_is_refused(fake_recognizer):
    with pytest.raises(ValueError, match="language"):
        SherpaAdapter(model_path=OFFLINE_DIR, language="en-US")


def test_create_adapter_passes_the_language(fake_recognizer):
    adapter = run_benchmark.create_adapter(
        "sherpa-streaming", ONLINE_DIR, "cpu", language="en-US",
    )
    assert adapter.language == "en-US"


def test_create_adapter_refuses_language_for_other_adapters():
    with pytest.raises(ValueError, match="language"):
        run_benchmark.create_adapter("google", "", "cpu", language="en-US")


# The thread count. sherpa-onnx gives an online recognizer 2 threads and an
# offline recognizer 1 thread when the caller passes none, while the shipped
# Parakeet provider passes 4, so the benchmark must be able to set it.


@pytest.fixture
def recognizer_factories():
    """Replace both from_transducer factories; the offline load changes directory."""
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch("adapters.sherpa_adapter.sherpa_onnx.OnlineRecognizer.from_transducer") as online,
        patch("adapters.sherpa_adapter.sherpa_onnx.OfflineRecognizer.from_transducer") as offline,
        patch("adapters.sherpa_adapter.os.chdir"),
    ):
        yield online, offline


def test_num_threads_reaches_the_online_recognizer(recognizer_factories):
    online, _ = recognizer_factories
    SherpaAdapter(model_path=ONLINE_DIR, num_threads=4)
    assert online.call_args.kwargs["num_threads"] == 4


def test_num_threads_reaches_the_offline_recognizer(recognizer_factories):
    _, offline = recognizer_factories
    SherpaAdapter(model_path=OFFLINE_DIR, num_threads=4)
    assert offline.call_args.kwargs["num_threads"] == 4


def test_no_num_threads_keeps_the_sherpa_defaults(recognizer_factories):
    """Earlier results used the defaults; an unset option must not change them."""
    online, offline = recognizer_factories
    SherpaAdapter(model_path=ONLINE_DIR)
    SherpaAdapter(model_path=OFFLINE_DIR)
    assert "num_threads" not in online.call_args.kwargs
    assert "num_threads" not in offline.call_args.kwargs


def test_num_threads_is_part_of_the_default_name(recognizer_factories):
    """Result files are named after the adapter, so runs must not collide."""
    adapter = SherpaAdapter(model_path=OFFLINE_DIR, num_threads=4)
    assert adapter.name.endswith("_t4")


def test_num_threads_below_one_is_refused(recognizer_factories):
    with pytest.raises(ValueError, match="num_threads"):
        SherpaAdapter(model_path=OFFLINE_DIR, num_threads=0)


def test_create_adapter_passes_num_threads(recognizer_factories):
    _, offline = recognizer_factories
    run_benchmark.create_adapter("sherpa-streaming", OFFLINE_DIR, "cpu", num_threads=4)
    assert offline.call_args.kwargs["num_threads"] == 4


def test_create_adapter_refuses_num_threads_for_other_adapters():
    with pytest.raises(ValueError, match="num-threads"):
        run_benchmark.create_adapter("google", "", "cpu", num_threads=4)


# The shipped text path. The shipped Parakeet server builds its transcript from
# result.tokens (result.text loses the space before ASCII punctuation from
# sherpa-onnx 1.13.4), then runs normalize_transcript and apply_itn. The
# benchmark must be able to score that exact text.

SHIPPED_TOKENS = [" Open", " tab", " two", " p", ".", "m", "."]


def _offline_adapter(tokens=SHIPPED_TOKENS, **kwargs) -> SherpaAdapter:
    """An offline adapter whose recognizer returns tokens that differ from text."""
    recognizer = MagicMock()
    recognizer.create_stream.return_value.result = SimpleNamespace(
        tokens=tokens,
        text="Wrong text from result.text",
    )
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch.object(SherpaAdapter, "_load_offline_recognizer", return_value=recognizer),
    ):
        return SherpaAdapter(model_path=OFFLINE_DIR, **kwargs)


def test_shipped_text_matches_the_server_text_path():
    from shared_stt.transcript_rules import apply_itn, normalize_transcript

    adapter = _offline_adapter(shipped_text=True)
    out = adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    server_text = apply_itn(normalize_transcript("".join(SHIPPED_TOKENS).strip()))
    assert out.text == server_text
    assert out.text == "open tab 2 PM"


def test_shipped_text_collapses_spaces_like_the_server():
    # The server splits the final text into words and joins them with one
    # space (sherpa_engine.py _run_final_inference and get_result). Both
    # tokens.txt files hold a lone "▁" token, so "word ▁, next" leaves two
    # spaces once normalize_transcript removes the comma.
    adapter = _offline_adapter(tokens=[" word", " ", ",", " next"], shipped_text=True)
    out = adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    assert out.text == "word next"


def test_shipped_text_off_keeps_result_text():
    adapter = _offline_adapter()
    out = adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    assert out.text == "Wrong text from result.text"


def test_shipped_text_is_refused_on_an_online_model(fake_recognizer):
    with pytest.raises(ValueError, match="shipped_text"):
        SherpaAdapter(model_path=ONLINE_DIR, shipped_text=True)


def test_create_adapter_passes_shipped_text(recognizer_factories):
    adapter = run_benchmark.create_adapter(
        "sherpa-streaming", OFFLINE_DIR, "cpu", shipped_text=True,
    )
    assert adapter.shipped_text is True


def test_create_adapter_shipped_text_defaults_off(recognizer_factories):
    adapter = run_benchmark.create_adapter("sherpa-streaming", OFFLINE_DIR, "cpu")
    assert adapter.shipped_text is False


def test_create_adapter_refuses_shipped_text_for_other_adapters():
    with pytest.raises(ValueError, match="shipped-text"):
        run_benchmark.create_adapter("google", "", "cpu", shipped_text=True)


def test_shipped_text_flag_is_parsed():
    with patch.object(sys, "argv", ["run_benchmark.py", "--model", "sherpa-streaming", "--shipped-text"]):
        assert run_benchmark.parse_args().shipped_text is True
    with patch.object(sys, "argv", ["run_benchmark.py", "--model", "sherpa-streaming"]):
        assert run_benchmark.parse_args().shipped_text is False


# The lead-word join (wh-parakeet-listening-check.1). With lead_word_path set,
# the shipped Parakeet server decodes the lead WAV directly before the user's
# audio, removes the lead word from the raw text, and decodes the audio alone
# when the lead word is not found (sherpa_engine.py _recognize). Then
# normalize_transcript and apply_itn run, and the words join with one space.

LEAD_SAMPLES = [1000, -2000, 3000]
CLIP = np.array([0.25, -0.5, 0.125, 0.75, -0.25], dtype=np.float32)


def _write_lead(path, samples=LEAD_SAMPLES, rate=16000):
    import wave

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return path


def _lead_float():
    return np.asarray(LEAD_SAMPLES, dtype=np.float32) / np.float32(32768.0)


class _Stream:
    def __init__(self):
        self.samples = None
        self.result = None

    def accept_waveform(self, rate, samples):
        assert rate == 16000
        self.samples = np.array(samples, copy=True)


class _Recognizer:
    """Answers with `joined` tokens for audio longer than the clip, else `plain`."""

    def __init__(self, joined, plain):
        self.joined, self.plain = joined, plain
        self.streams: list[_Stream] = []

    def create_stream(self):
        s = _Stream()
        self.streams.append(s)
        return s

    def decode_stream(self, stream):
        tokens = self.joined if len(stream.samples) > len(CLIP) else self.plain
        stream.result = SimpleNamespace(tokens=tokens, text="Wrong text from result.text")


def _lead_adapter(tmp_path, joined, plain, **kwargs):
    recognizer = _Recognizer(joined, plain)
    lead = _write_lead(tmp_path / "hello.wav")
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch.object(SherpaAdapter, "_load_offline_recognizer", return_value=recognizer),
    ):
        adapter = SherpaAdapter(
            model_path=OFFLINE_DIR, shipped_text=True, lead_word_wav=str(lead), **kwargs,
        )
    return adapter, recognizer


def test_lead_word_join_puts_the_lead_directly_before_the_clip(tmp_path):
    adapter, rec = _lead_adapter(tmp_path, joined=[" hello", " open", " tab"], plain=[" wrong"])
    out = adapter.transcribe(CLIP, 16000)
    assert len(rec.streams) == 1
    # No gap and no cut: the lead samples, then every clip sample.
    np.testing.assert_array_equal(rec.streams[0].samples, np.concatenate((_lead_float(), CLIP)))
    assert out.text == "open tab"
    assert adapter.last_second_decode is False


def test_lead_word_is_removed_from_the_raw_text_before_normalization(tmp_path):
    import sherpa_engine
    from shared_stt.transcript_rules import apply_itn, normalize_transcript

    joined = [" Hello", ",", " Open", " tab", " two", " p", ".", "m", "."]
    adapter, _ = _lead_adapter(tmp_path, joined=joined, plain=[" wrong"])
    out = adapter.transcribe(CLIP, 16000)
    raw, found = sherpa_engine.remove_lead_word("".join(joined).strip(), sherpa_engine.LEAD_WORD)
    assert found
    assert out.text == " ".join(apply_itn(normalize_transcript(raw)).split())
    assert out.text == "open tab 2 PM"


def test_second_decode_of_the_clip_alone_when_the_lead_word_is_not_found(tmp_path):
    adapter, rec = _lead_adapter(tmp_path, joined=[" yellow", " open"], plain=[" Open", " tab", " three"])
    out = adapter.transcribe(CLIP, 16000)
    assert len(rec.streams) == 2
    np.testing.assert_array_equal(rec.streams[1].samples, CLIP)
    assert out.text == "open tab three"
    assert adapter.last_second_decode is True


def test_second_decode_flag_is_none_without_the_lead_word():
    adapter = _offline_adapter(shipped_text=True)
    adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    assert adapter.last_second_decode is None


def test_lead_word_wav_needs_shipped_text(tmp_path):
    lead = _write_lead(tmp_path / "hello.wav")
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch.object(SherpaAdapter, "_load_offline_recognizer", return_value=MagicMock()),
        pytest.raises(ValueError, match="lead_word_wav"),
    ):
        SherpaAdapter(model_path=OFFLINE_DIR, lead_word_wav=str(lead))


def test_lead_word_wav_is_read_by_the_server_loader(tmp_path):
    import sherpa_engine

    lead = _write_lead(tmp_path / "hello8k.wav", rate=8000)
    with (
        patch.object(SherpaAdapter, "_find_model_file", return_value="encoder.onnx"),
        patch.object(SherpaAdapter, "_load_offline_recognizer", return_value=MagicMock()),
        pytest.raises(sherpa_engine.LeadWordError),
    ):
        SherpaAdapter(model_path=OFFLINE_DIR, shipped_text=True, lead_word_wav=str(lead))


def test_lead_word_join_refuses_another_sample_rate(tmp_path):
    adapter, _ = _lead_adapter(tmp_path, joined=[" hello"], plain=[" x"])
    with pytest.raises(ValueError, match="16000"):
        adapter.transcribe(CLIP, 8000)


@pytest.mark.parametrize(
    "joined, plain",
    [
        (" hello open tab", " wrong"),
        (" Hello, Open tab.", " wrong"),
        (" yellow open", " open tab"),
        ("", " open tab"),
        (" hello", " open"),
        (" open hello tab", " plain text"),
    ],
)
def test_recognize_with_lead_matches_the_server_recognize(joined, plain):
    """The copied join logic gives the same raw text as the server's own _recognize."""
    import sherpa_engine
    from adapters.sherpa_adapter import recognize_with_lead

    lead = _lead_float()

    def decode_plain(audio):
        return (joined if len(audio) > len(CLIP) else plain).strip()

    server = object.__new__(sherpa_engine.SherpaOfflineEngine)
    server._utterance_lead = sherpa_engine._NOT_COPIED
    server._lead_audio = lead
    server._decode_plain = decode_plain
    expected = sherpa_engine.SherpaOfflineEngine._recognize(server, CLIP)

    text, _second = recognize_with_lead(decode_plain, CLIP, lead)
    assert text == expected
