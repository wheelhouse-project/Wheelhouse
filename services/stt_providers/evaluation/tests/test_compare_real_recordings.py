"""Tests for compare_real_recordings: decode real wavs, compare models.

Everything runs on fakes and tiny generated wavs; no model file is read.
"""

import json
import wave

import numpy as np
import pytest

import compare_real_recordings as crr
from adapters.base import TranscriptionResult


def _write_wav(path, samples, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype=np.int16).tobytes())
    return path


# -- script detector ----------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("привет", True),            # Cyrillic
        ("open Ԁ", True),       # Cyrillic Supplement
        ("αβγ", True),  # Greek
        ("ἀ", True),            # Greek Extended
        ("café naive resume", False),  # Latin with accents
        ("open tab 2", False),
        ("", False),
    ],
)
def test_has_cyrillic_or_greek(text, expected):
    assert crr.has_cyrillic_or_greek(text) is expected


# -- last210 splice -----------------------------------------------------


def test_last210_splice_length_and_first_samples():
    lead = np.arange(1, 101, dtype=np.int16)
    clip = np.arange(1000, 3000, dtype=np.int16)
    out = crr.build_last210(lead, clip)
    assert out.dtype == np.int16
    assert len(out) == 100 + (2000 - 1440)
    assert list(out[:100]) == list(lead)
    assert out[100] == clip[1440]
    assert out[-1] == clip[-1]


def test_read_pcm16_refuses_wrong_sample_rate(tmp_path):
    path = _write_wav(tmp_path / "a.wav", [0] * 100, rate=8000)
    with pytest.raises(ValueError, match="16000"):
        crr.read_pcm16_mono(path)


def test_read_pcm16_refuses_stereo(tmp_path):
    path = _write_wav(tmp_path / "a.wav", [0] * 100, channels=2)
    with pytest.raises(ValueError, match="channel"):
        crr.read_pcm16_mono(path)


def test_read_pcm16_refuses_wrong_sample_width(tmp_path):
    with wave.open(str(tmp_path / "a.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(16000)
        wf.writeframes(b"\x00" * 100)
    with pytest.raises(ValueError, match="sample width"):
        crr.read_pcm16_mono(tmp_path / "a.wav")


def test_read_pcm16_returns_int16(tmp_path):
    path = _write_wav(tmp_path / "a.wav", [1, -2, 3])
    out = crr.read_pcm16_mono(path)
    assert out.dtype == np.int16
    assert list(out) == [1, -2, 3]


# -- decode -------------------------------------------------------------


class FakeAdapter:
    """Records every transcribe() input; answers with the sample count."""

    def __init__(self):
        self.seen: list[np.ndarray] = []

    def reset(self):
        pass

    def transcribe(self, samples, sample_rate):
        assert sample_rate == 16000
        assert samples.dtype == np.float32
        self.seen.append(samples)
        return TranscriptionResult(
            text=f"n{len(samples)}", elapsed_ms=float(len(self.seen)), interim_results=[],
        )


def test_decode_files_warms_up_once_and_leaves_it_out(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    b = _write_wav(tmp_path / "b.wav", [1] * 3200)
    adapter = FakeAdapter()
    files = crr.decode_files(adapter, [a, b], form="raw", lead=None)
    assert [f["path"] for f in files] == [str(a), str(b)]
    # Three transcribe calls: one warm-up plus two results.
    assert len(adapter.seen) == 3
    # The warm-up's elapsed_ms (1.0) is not in the results.
    assert [f["inference_ms"] for f in files] == [2.0, 3.0]
    assert [f["audio_seconds"] for f in files] == [0.1, 0.2]
    assert [f["text"] for f in files] == ["n1600", "n3200"]


def test_decode_files_last210_decodes_the_splice(tmp_path):
    lead = np.full(100, 7, dtype=np.int16)
    a = _write_wav(tmp_path / "a.wav", np.arange(3000))
    adapter = FakeAdapter()
    files = crr.decode_files(adapter, [a], form="last210", lead=lead)
    assert files[0]["text"] == f"n{100 + 3000 - 1440}"
    assert files[0]["audio_seconds"] == (100 + 3000 - 1440) / 16000


def test_decode_files_last210_needs_a_lead(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [0] * 2000)
    with pytest.raises(ValueError, match="lead"):
        crr.decode_files(FakeAdapter(), [a], form="last210", lead=None)


def test_run_decode_writes_json(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    wav_list = tmp_path / "list.txt"
    wav_list.write_text(f"{a}\n\n", encoding="utf-8")
    out = tmp_path / "out.json"
    built = {}

    def factory(**kwargs):
        built.update(kwargs)
        return FakeAdapter()

    args = crr.build_parser().parse_args([
        "decode", "--model-path", "/m/v3", "--num-threads", "4",
        "--provider", "cpu", "--shipped-text", "--wav-list", str(wav_list),
        "--form", "raw", "--output", str(out),
    ])
    crr.run_decode(args, adapter_factory=factory, version="9.9")
    assert built == {
        "model_path": "/m/v3", "provider": "cpu", "num_threads": 4, "shipped_text": True,
    }
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["model_path"] == "/m/v3"
    assert data["sherpa_onnx_version"] == "9.9"
    assert data["provider"] == "cpu"
    assert data["threads"] == 4
    assert data["form"] == "raw"
    assert [f["path"] for f in data["files"]] == [str(a)]


def test_last210_without_lead_is_a_parser_level_error(tmp_path):
    wav_list = tmp_path / "list.txt"
    wav_list.write_text("x.wav\n", encoding="utf-8")
    args = crr.build_parser().parse_args([
        "decode", "--model-path", "/m", "--num-threads", "1", "--provider", "cpu",
        "--wav-list", str(wav_list), "--form", "last210", "--output", "o.json",
    ])
    with pytest.raises(SystemExit):
        crr.run_decode(args, adapter_factory=lambda **k: FakeAdapter(), version="x")


class FakeLeadAdapter(FakeAdapter):
    """A FakeAdapter that reports a second decode on every other call."""

    def __init__(self):
        super().__init__()
        self.last_second_decode = None

    def transcribe(self, samples, sample_rate):
        out = super().transcribe(samples, sample_rate)
        self.last_second_decode = len(self.seen) % 2 == 0
        return out


def test_decode_files_records_the_second_decode_flag(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    b = _write_wav(tmp_path / "b.wav", [1] * 1600)
    files = crr.decode_files(FakeLeadAdapter(), [a, b], form="raw", lead=None)
    # Call 1 is the warm-up; calls 2 and 3 give True and False.
    assert [f["second_decode"] for f in files] == [True, False]


def test_decode_files_leaves_the_flag_out_without_the_lead_word(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    files = crr.decode_files(FakeAdapter(), [a], form="raw", lead=None)
    assert "second_decode" not in files[0]


def test_run_decode_passes_the_lead_word_wav(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    wav_list = tmp_path / "list.txt"
    wav_list.write_text(f"{a}\n", encoding="utf-8")
    out = tmp_path / "out.json"
    built = {}

    def factory(**kwargs):
        built.update(kwargs)
        return FakeLeadAdapter()

    args = crr.build_parser().parse_args([
        "decode", "--model-path", "/m/v3", "--num-threads", "4", "--provider", "cpu",
        "--shipped-text", "--wav-list", str(wav_list), "--form", "raw",
        "--lead-word-wav", "C:/lead/hello.wav", "--output", str(out),
    ])
    crr.run_decode(args, adapter_factory=factory, version="9.9")
    assert built["lead_word_wav"] == "C:/lead/hello.wav"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["lead_word_wav"] == "C:/lead/hello.wav"
    assert "second_decode" in data["files"][0]


def test_run_decode_without_the_lead_word_wav_records_none(tmp_path):
    a = _write_wav(tmp_path / "a.wav", [1] * 1600)
    wav_list = tmp_path / "list.txt"
    wav_list.write_text(f"{a}\n", encoding="utf-8")
    out = tmp_path / "out.json"
    args = crr.build_parser().parse_args([
        "decode", "--model-path", "/m", "--num-threads", "1", "--provider", "cpu",
        "--shipped-text", "--wav-list", str(wav_list), "--output", str(out),
    ])
    crr.run_decode(args, adapter_factory=lambda **k: FakeAdapter(), version="x")
    assert json.loads(out.read_text(encoding="utf-8"))["lead_word_wav"] is None


def test_lead_word_wav_needs_shipped_text(tmp_path):
    wav_list = tmp_path / "list.txt"
    wav_list.write_text("x.wav\n", encoding="utf-8")
    args = crr.build_parser().parse_args([
        "decode", "--model-path", "/m", "--num-threads", "1", "--provider", "cpu",
        "--wav-list", str(wav_list), "--lead-word-wav", "C:/lead/hello.wav", "--output", "o.json",
    ])
    with pytest.raises(SystemExit):
        crr.run_decode(args, adapter_factory=lambda **k: FakeAdapter(), version="x")


# -- compare ------------------------------------------------------------


def _decode(model, rows):
    return {
        "model_path": model,
        "files": [
            {"path": p, "text": t, "inference_ms": ms, "audio_seconds": s}
            for p, t, ms, s in rows
        ],
    }


def test_compare_counts_differences_and_limits_examples_in_sorted_order():
    rows_v3 = [(f"/w/{n}.wav", "same", 100.0, 1.0) for n in "dcba"]
    rows_v2 = [
        ("/w/d.wav", "other d", 100.0, 1.0),
        ("/w/c.wav", "same", 100.0, 1.0),
        ("/w/b.wav", "other b", 100.0, 1.0),
        ("/w/a.wav", "other a", 100.0, 1.0),
    ]
    report = crr.compare_models(
        _decode("v3", rows_v3), [_decode("v2", rows_v2)], references={}, examples=2,
    )
    cand = report["candidates"][0]
    assert cand["differs_from_baseline"] == 3
    assert [e["path"] for e in cand["examples"]] == ["/w/a.wav", "/w/b.wav"]
    assert cand["examples"][0] == {
        "path": "/w/a.wav", "baseline_text": "same", "candidate_text": "other a",
    }


def test_compare_reports_missing_files_on_both_sides():
    v3 = _decode("v3", [("/w/a.wav", "x", 1.0, 1.0), ("/w/only_base.wav", "x", 1.0, 1.0)])
    v2 = _decode("v2", [("/w/a.wav", "x", 1.0, 1.0), ("/w/only_cand.wav", "y", 1.0, 1.0)])
    cand = crr.compare_models(v3, [v2], references={}, examples=20)["candidates"][0]
    assert cand["compared_files"] == 1
    assert cand["differs_from_baseline"] == 0
    assert cand["missing_from_candidate"] == ["/w/only_base.wav"]
    assert cand["missing_from_baseline"] == ["/w/only_cand.wav"]


def test_compare_script_counts_for_each_model_including_baseline():
    v3 = _decode("v3", [("/w/a.wav", "open tab", 1.0, 1.0), ("/w/b.wav", "привет", 1.0, 1.0)])
    v2 = _decode("v2", [("/w/a.wav", "α open", 1.0, 1.0), ("/w/b.wav", "café", 1.0, 1.0)])
    report = crr.compare_models(v3, [v2], references={}, examples=20)
    assert report["baseline"]["script_count"] == 1
    assert report["baseline"]["script_outputs"] == [
        {"path": "/w/b.wav", "text": "привет"},
    ]
    assert report["candidates"][0]["script_count"] == 1
    assert report["candidates"][0]["script_outputs"][0]["path"] == "/w/a.wav"


def test_compare_wer_only_over_referenced_files():
    v3 = _decode("v3", [
        ("/w/a.wav", "open tab", 1.0, 1.0),
        ("/w/b.wav", "totally wrong text", 1.0, 1.0),
    ])
    refs = {"/w/a.wav": "open tab"}
    report = crr.compare_models(v3, [], references=refs, examples=20)
    assert report["baseline"]["reference_count"] == 1
    assert report["baseline"]["wer"] == 0.0
    # A wrong reference moves the WER: one of two words wrong.
    refs = {"/w/a.wav": "open window"}
    report = crr.compare_models(v3, [], references=refs, examples=20)
    assert report["baseline"]["wer"] == 0.5


def test_compare_difference_is_an_exact_string_compare():
    v3 = _decode("v3", [("/w/a.wav", "Open tab", 1.0, 1.0)])
    v2 = _decode("v2", [("/w/a.wav", "open tab", 1.0, 1.0)])
    cand = crr.compare_models(v3, [v2], references={}, examples=20)["candidates"][0]
    assert cand["differs_from_baseline"] == 1


def test_compare_wer_skips_a_reference_that_is_empty_after_normalization():
    v3 = _decode("v3", [("/w/a.wav", "open tab", 1.0, 1.0), ("/w/b.wav", "x", 1.0, 1.0)])
    refs = {"/w/a.wav": "open tab", "/w/b.wav": "."}
    base = crr.compare_models(v3, [], references=refs, examples=20)["baseline"]
    assert base["reference_count"] == 1
    assert base["wer"] == 0.0


def test_compare_wer_is_none_without_references():
    v3 = _decode("v3", [("/w/a.wav", "x", 1.0, 1.0)])
    report = crr.compare_models(v3, [], references={}, examples=20)
    assert report["baseline"]["wer"] is None
    assert report["baseline"]["reference_count"] == 0


def test_compare_latency_and_rtf():
    rows = [(f"/w/{i}.wav", "x", float(ms), 1.0) for i, ms in enumerate(range(0, 201, 10))]
    report = crr.compare_models(_decode("v3", rows), [], references={}, examples=20)
    base = report["baseline"]
    assert base["mean_inference_ms"] == 100.0
    assert base["p95_inference_ms"] == 190.0
    # 2100 ms of inference over 21 s of audio.
    assert base["rtf"] == 0.1
    assert base["file_count"] == 21


def test_compare_cli_end_to_end(tmp_path):
    v3 = tmp_path / "v3.json"
    v2 = tmp_path / "v2.json"
    v3.write_text(json.dumps(_decode("v3", [("/w/a.wav", "x", 1.0, 1.0)])), encoding="utf-8")
    v2.write_text(json.dumps(_decode("v2", [("/w/a.wav", "y", 1.0, 1.0)])), encoding="utf-8")
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps({"/w/a.wav": "x"}), encoding="utf-8")
    out = tmp_path / "report.json"
    args = crr.build_parser().parse_args([
        "compare", "--baseline", str(v3), "--candidate", str(v2),
        "--references", str(refs), "--examples", "5", "--output", str(out),
    ])
    crr.run_compare(args)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["candidates"][0]["differs_from_baseline"] == 1
    assert report["baseline"]["wer"] == 0.0
    assert report["candidates"][0]["wer"] == 1.0


def test_compare_counts_empty_outputs_for_each_model():
    v3 = _decode("v3", [("/w/a.wav", "", 1.0, 1.0), ("/w/b.wav", "  ", 1.0, 1.0), ("/w/c.wav", "x", 1.0, 1.0)])
    v2 = _decode("v2", [("/w/a.wav", "y", 1.0, 1.0), ("/w/b.wav", "", 1.0, 1.0), ("/w/c.wav", "x", 1.0, 1.0)])
    report = crr.compare_models(v3, [v2], {}, examples=0)
    assert report["baseline"]["empty_count"] == 2
    assert report["candidates"][0]["empty_count"] == 1


def test_compare_counts_second_decodes_or_none_without_the_flag():
    v3 = _decode("v3", [("/w/a.wav", "x", 1.0, 1.0), ("/w/b.wav", "x", 1.0, 1.0)])
    v3["files"][0]["second_decode"] = True
    v3["files"][1]["second_decode"] = False
    v2 = _decode("v2", [("/w/a.wav", "x", 1.0, 1.0), ("/w/b.wav", "x", 1.0, 1.0)])
    report = crr.compare_models(v3, [v2], {}, examples=0)
    assert report["baseline"]["second_decode_count"] == 1
    assert report["candidates"][0]["second_decode_count"] is None
