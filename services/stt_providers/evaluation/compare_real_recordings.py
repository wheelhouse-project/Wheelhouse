"""Decode real recordings with a sherpa-onnx model and compare models.

Two subcommands:

  decode   Decode a list of wav files with one model, through the same
           SherpaAdapter class and options the benchmark harness uses, and
           write the texts and timings to a JSON file.
  compare  Read one baseline decode file and one or more candidate decode
           files, and report script characters, word error rate, differences
           from the baseline, and latency.

Example (run from this directory, in a venv that has sherpa_onnx and jiwer):

  python compare_real_recordings.py decode --model-path <models>/sherpa-onnx/parakeet-tdt-0.6b-v3 \
      --num-threads 4 --provider cpu --shipped-text --wav-list wavs.txt \
      --form raw --output v3_raw.json
  python compare_real_recordings.py compare --baseline v3_raw.json \
      --candidate v2_raw.json --references refs.json --examples 20 --output report.json

This is test tooling. It imports sherpa_onnx only inside decode, so the
compare logic and the tests run without a model.
"""

import argparse
import json
import sys
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

SAMPLE_RATE = 16000
# Samples dropped from the front of the clip in the last210 form.
LAST210_CLIP_SKIP = 1440

# Cyrillic, Cyrillic Supplement, Greek and Coptic, Greek Extended.
_SCRIPT_RANGES = (
    (0x0400, 0x04FF),
    (0x0500, 0x052F),
    (0x0370, 0x03FF),
    (0x1F00, 0x1FFF),
)


def has_cyrillic_or_greek(text: str) -> bool:
    """True when `text` holds any Cyrillic or Greek character."""
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _SCRIPT_RANGES)


def read_pcm16_mono(path: Path | str) -> np.ndarray:
    """Read a 16 kHz mono 16-bit wav as int16; refuse anything else."""
    with wave.open(str(path), "rb") as wf:
        rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        if rate != SAMPLE_RATE:
            raise ValueError(f"{path}: sample rate {rate}, expected {SAMPLE_RATE}")
        if channels != 1:
            raise ValueError(f"{path}: {channels} channels, expected 1 (mono)")
        if width != 2:
            raise ValueError(f"{path}: sample width {width} bytes, expected 2 (16-bit)")
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16)


def build_last210(lead: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """The last210 form: lead samples, then the clip without its first 1440 samples."""
    return np.concatenate((lead, clip[LAST210_CLIP_SKIP:]))


def decode_files(
    adapter: Any,
    wav_paths: list[Path | str],
    form: str,
    lead: np.ndarray | None,
) -> list[dict]:
    """Decode every wav. One warm-up decode runs first and is not reported."""
    if form not in ("raw", "last210"):
        raise ValueError(f"unknown form {form!r}")
    if form == "last210" and lead is None:
        raise ValueError("form last210 needs a lead wav")

    def prepare(path: Path | str) -> np.ndarray:
        clip = read_pcm16_mono(path)
        return clip if form == "raw" else build_last210(lead, clip)  # type: ignore[arg-type]

    def run(pcm: np.ndarray):
        adapter.reset()
        return adapter.transcribe(pcm.astype(np.float32) / 32768.0, SAMPLE_RATE)

    if not wav_paths:
        return []
    run(prepare(wav_paths[0]))  # warm-up, left out of the results

    files = []
    for path in wav_paths:
        pcm = prepare(path)
        tr = run(pcm)
        entry = {
            "path": str(path),
            "text": tr.text,
            "inference_ms": tr.elapsed_ms,
            "audio_seconds": len(pcm) / SAMPLE_RATE,
        }
        # With the lead-word join on, the adapter says whether the lead word
        # was missing and the clip was decoded again on its own.
        second = getattr(adapter, "last_second_decode", None)
        if second is not None:
            entry["second_decode"] = second
        files.append(entry)
    return files


def _default_adapter_factory(**kwargs) -> Any:
    from adapters.sherpa_adapter import SherpaAdapter

    return SherpaAdapter(**kwargs)


def _sherpa_version() -> str:
    import sherpa_onnx

    return sherpa_onnx.__version__


def run_decode(
    args: argparse.Namespace,
    adapter_factory: Callable[..., Any] | None = None,
    version: str | None = None,
) -> dict:
    if args.form == "last210" and not args.lead:
        raise SystemExit("--lead is required for --form last210")
    lead_word_wav = getattr(args, "lead_word_wav", None)
    if lead_word_wav and not args.shipped_text:
        raise SystemExit("--lead-word-wav needs --shipped-text")
    lines = Path(args.wav_list).read_text(encoding="utf-8").splitlines()
    wav_paths = [ln.strip() for ln in lines if ln.strip()]
    lead = read_pcm16_mono(args.lead) if args.lead else None
    factory = adapter_factory or _default_adapter_factory
    kwargs: dict[str, Any] = {
        "model_path": args.model_path,
        "provider": args.provider,
        "num_threads": args.num_threads,
        "shipped_text": args.shipped_text,
    }
    if lead_word_wav:
        kwargs["lead_word_wav"] = lead_word_wav
    adapter = factory(**kwargs)
    files = decode_files(adapter, wav_paths, args.form, lead)
    output = {
        "model_path": args.model_path,
        "sherpa_onnx_version": version if version is not None else _sherpa_version(),
        "provider": args.provider,
        "threads": args.num_threads,
        "shipped_text": args.shipped_text,
        "form": args.form,
        "lead": args.lead,
        "lead_word_wav": lead_word_wav,
        "files": files,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return output


# -- compare ------------------------------------------------------------


def _score_text(text: str) -> str:
    """The text the benchmark scores: cleanup, then loose normalization."""
    from run_benchmark import _loose_normalize, cleanup_for_display

    return _loose_normalize(cleanup_for_display(text))


def _wer(files: list[dict], references: dict[str, str]) -> tuple[float | None, int]:
    """Word error rate over the files that have a reference; returns (wer, count).

    A reference that is empty after normalization cannot be scored by jiwer
    and is left out of both the rate and the count.
    """
    import jiwer

    refs, hyps = [], []
    for f in files:
        if f["path"] not in references:
            continue
        ref = _score_text(references[f["path"]])
        if not ref:
            continue
        refs.append(ref)
        hyps.append(_score_text(f["text"]))
    if not refs:
        return None, 0
    return float(jiwer.wer(refs, hyps)), len(refs)


def _model_stats(decoded: dict, references: dict[str, str]) -> dict:
    files = decoded["files"]
    scripts = [{"path": f["path"], "text": f["text"]} for f in files if has_cyrillic_or_greek(f["text"])]
    wer, ref_count = _wer(files, references)
    times = [f["inference_ms"] for f in files]
    audio = sum(f["audio_seconds"] for f in files)
    return {
        "model_path": decoded.get("model_path"),
        "file_count": len(files),
        "script_count": len(scripts),
        "script_outputs": scripts,
        "wer": None if wer is None else round(wer, 4),
        "reference_count": ref_count,
        "mean_inference_ms": round(float(np.mean(times)), 1) if times else None,
        "p95_inference_ms": round(float(np.percentile(times, 95)), 1) if times else None,
        "rtf": round(sum(times) / 1000.0 / audio, 4) if audio > 0 else None,
        "empty_count": sum(1 for f in files if not f["text"].strip()),
        # None when the decode ran without the lead-word join.
        "second_decode_count": (
            sum(1 for f in files if f["second_decode"])
            if files and all("second_decode" in f for f in files) else None
        ),
    }


def compare_models(
    baseline: dict,
    candidates: list[dict],
    references: dict[str, str],
    examples: int,
) -> dict:
    base_text = {f["path"]: f["text"] for f in baseline["files"]}
    report: dict = {
        "baseline": _model_stats(baseline, references),
        "candidates": [],
    }
    for cand in candidates:
        cand_text = {f["path"]: f["text"] for f in cand["files"]}
        common = sorted(set(base_text) & set(cand_text))
        differing = [p for p in common if base_text[p] != cand_text[p]]
        stats = _model_stats(cand, references)
        stats.update({
            "compared_files": len(common),
            "differs_from_baseline": len(differing),
            "examples": [
                {"path": p, "baseline_text": base_text[p], "candidate_text": cand_text[p]}
                for p in differing[:examples]
            ],
            "missing_from_candidate": sorted(set(base_text) - set(cand_text)),
            "missing_from_baseline": sorted(set(cand_text) - set(base_text)),
        })
        report["candidates"].append(stats)
    return report


def run_compare(args: argparse.Namespace) -> dict:
    def load(path: str) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    references = load(args.references) if args.references else {}
    report = compare_models(
        load(args.baseline), [load(p) for p in args.candidate], references, args.examples,
    )
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("decode", help="decode a wav list with one model")
    d.add_argument("--model-path", required=True)
    d.add_argument("--num-threads", type=int, required=True)
    d.add_argument("--provider", default="cpu")
    d.add_argument("--shipped-text", action="store_true")
    d.add_argument("--wav-list", required=True, help="text file, one absolute wav path per line")
    d.add_argument("--form", choices=("raw", "last210"), default="raw")
    d.add_argument("--lead", default=None, help="lead wav (required for --form last210)")
    d.add_argument(
        "--lead-word-wav", default=None,
        help="decode as the shipped server does with lead_word_path set (needs --shipped-text)",
    )
    d.add_argument("--output", required=True)

    c = sub.add_parser("compare", help="compare decode files against a baseline")
    c.add_argument("--baseline", required=True)
    c.add_argument("--candidate", action="append", required=True)
    c.add_argument("--references", default=None, help="JSON mapping wav path to reference text")
    c.add_argument("--examples", type=int, default=20)
    c.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decode":
        run_decode(args)
    else:
        run_compare(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
