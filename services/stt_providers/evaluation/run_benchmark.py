"""STT Benchmark Harness -- evaluate STT models against the TTS test corpus.

Feeds Phase 1 corpus WAV files into a candidate STT model via an adapter,
compares output against manifest ground truth, and reports accuracy metrics.

Usage:
    cd services/stt_providers/shared
    uv run python ../evaluation/run_benchmark.py \
        --model sherpa-zipformer \
        --model-path "../sherpa_streaming_zipformer_stt_server" \
        --provider cpu

    # Quick litmus check:
    uv run python ../evaluation/run_benchmark.py \
        --model sherpa-zipformer \
        --model-path "../sherpa_streaming_zipformer_stt_server" \
        --category litmus

Reference: docs/design/benchmark_harness_design.md
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import textwrap
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import jiwer
import numpy as np

from adapters.base import ModelAdapter, TranscriptionResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Text Cleanup ────────────────────────────────────────────────────

# Pattern used only for the has_punctuation() reporting flag.
PUNCTUATION_RE = re.compile(r"[^\w\s]")
# BPE word-boundary character used by sherpa-onnx output.
SHERPA_BPE_BOUNDARY = "\u2581"
# Trailing characters treated as cosmetic by the loose match.
TERMINAL_PUNCT = ".?!"
# Cardinal words 0-10 that WheelHouse treats as equivalent to digits in
# command parameters. Source: services/wheelhouse/speech/actions.py:51-54
# (_WORD_TO_INT_MAP). The harness applies the same equivalence so a
# canonical "delete one" matches model output "delete 1", and a canonical
# "tab 2" matches model output "tab two".
_SMALL_NUMBER_WORD_TO_DIGIT = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}
_SMALL_NUMBER_RE = re.compile(
    r"\b(" + "|".join(_SMALL_NUMBER_WORD_TO_DIGIT.keys()) + r")\b",
    re.IGNORECASE,
)


def cleanup_for_display(text: str | None) -> str:
    """Prepare a model output string for display in the results table.

    Replaces the sherpa-onnx BPE word-boundary character, replaces newlines
    and tabs with spaces (so multi-line model output does not break the
    per-utterance table), and strips leading and trailing whitespace.
    Returns an empty string if the adapter passed None.

    Display only. No case folding, no punctuation stripping. The loose
    match comparison is a separate function.
    """
    if not text:
        return ""
    text = text.replace(SHERPA_BPE_BOUNDARY, " ")
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text.strip()


def _has_proper_noun_shape(text: str) -> bool:
    """Return True if any word in text looks like a proper noun.

    A proper-noun-shaped word starts with an uppercase letter and contains
    at least one lowercase letter (Boston, Friday, JavaScript, Microsoft).
    All-cap abbreviations like PM, AM, IBM, USA do not count; neither
    does the standalone pronoun "I". The harness uses this signal to
    decide whether the comparison should be strict (proper nouns must
    match exactly) or case-insensitive (sentence-start and stray
    capitalization are tolerated).

    The convention in vocabulary.py is to lowercase sentence-start words
    in the expected_transcription unless the first word is itself a
    proper noun. Under that convention, any uppercase letter that
    survives in the expected indicates a proper noun the model is
    required to produce in the same case.
    """
    for word in text.split():
        if not word or word == "I":
            continue
        letters = "".join(c for c in word if c.isalpha())
        if len(letters) < 2:
            continue
        if letters[0].isupper() and any(c.islower() for c in letters[1:]):
            return True
    return False


def _normalize_small_numbers(text: str) -> str:
    """Replace cardinal words zero through ten with their digit forms."""
    return _SMALL_NUMBER_RE.sub(
        lambda m: _SMALL_NUMBER_WORD_TO_DIGIT[m.group(0).lower()],
        text,
    )


def _loose_normalize(text: str) -> str:
    """Strip a trailing terminal punctuation character and digit-normalize.

    Mid-string casing and mid-string punctuation stay untouched here.
    The case-sensitivity decision happens later in loose_match().
    """
    text = text.strip()
    if text and text[-1] in TERMINAL_PUNCT:
        text = text[:-1].rstrip()
    return _normalize_small_numbers(text)


def loose_match(expected: str, actual: str) -> bool:
    """Compare expected_transcription against actual model output.

    Comparison rules:
      - A single trailing terminal punctuation character (. ? !) is
        stripped from each side.
      - Cardinal words zero through ten are replaced with their digit
        equivalents on each side, mirroring WheelHouse's word-or-digit
        acceptance for command parameters in that range.
      - If the (normalized) expected contains a proper-noun-shaped word,
        the comparison is strict: mid-string casing must match. So
        "Bill Smith" and "Boston" need their capitals.
      - Otherwise the comparison is case-insensitive. Sentence-start
        capitalization, stray internal capitals like "Port" or "Suite",
        and all-cap abbreviations like PM, AM, IBM, USA, and JSON are
        all tolerated.

    Gating decisions about whether a model is good enough to integrate
    are made by a human reviewer looking at the per-utterance results
    table. The harness does not apply automatic thresholds, does not
    emit a recommend_disposition field, and does not infer pass/fail
    from logical rules over aggregate metrics. That is intentional and
    must stay that way.
    """
    e = _loose_normalize(expected)
    a = _loose_normalize(actual)
    if _has_proper_noun_shape(e):
        return e == a
    return e.lower() == a.lower()


def has_punctuation(text: str) -> bool:
    """Check if raw model output contains punctuation characters."""
    return bool(PUNCTUATION_RE.search(text))


def voice_short_name(voice_full: str) -> str:
    """Convert an Edge-TTS voice ID to its short label.

    "en-US-AriaNeural" -> "aria", "en-US-GuyNeural" -> "guy". Unknown
    formats fall back to a question mark so the per-utterance table
    stays aligned.
    """
    if not voice_full:
        return "?"
    last = voice_full.rsplit("-", 1)[-1]
    short = last.removesuffix("Neural").lower()
    return short or "?"


# ── Data Structures ─────────────────────────────────────────────────


@dataclass
class UtteranceResult:
    """Benchmark result for a single utterance.

    expected_transcription is the verbatim canonical string from the
    manifest. actual_transcription is the model's raw output after the
    cleanup_for_display() pass (BPE boundary, newlines, whitespace).
    exact_match is the loose-match comparison: trailing terminal
    punctuation stripped from each side, only the first character
    lowercased, everything else strict.
    """

    id: str
    category: str
    voice: str
    spoken_text: str
    expected_transcription: str
    actual_transcription: str
    exact_match: bool
    wer: float
    is_litmus: bool
    elapsed_ms: float
    interim_results: list[str] = field(default_factory=list)
    interim_final_match: bool = False
    punctuation_added: bool = False


@dataclass
class CategoryMetrics:
    """Aggregate metrics for one category."""

    count: int = 0
    exact_matches: int = 0
    total_wer: float = 0.0
    total_ms: float = 0.0

    @property
    def exact_match_accuracy(self) -> float:
        return self.exact_matches / self.count if self.count else 0.0

    @property
    def avg_wer(self) -> float:
        return self.total_wer / self.count if self.count else 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


# ── Audio Loading ───────────────────────────────────────────────────


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV file and return (float32 samples, sample_rate)."""
    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_bytes = wf.readframes(n_frames)

    # Convert int16 PCM to float32 [-1.0, 1.0]
    samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sample_rate


# ── Adapter Factory ─────────────────────────────────────────────────


ADAPTER_REGISTRY = {
    "sherpa-zipformer": "adapters.sherpa_adapter.SherpaAdapter",
    "sherpa-lstm": "adapters.sherpa_adapter.SherpaAdapter",
    "google": "adapters.google_adapter.GoogleSTTAdapter",
    "faster-whisper": "adapters.faster_whisper_adapter.FasterWhisperAdapter",
    "voxtral": "adapters.voxtral_adapter.VoxtralAdapter",
    "parakeet": "adapters.parakeet_adapter.ParakeetAdapter",
    "whisper-cpp": "adapters.whisper_cpp_adapter.WhisperCppAdapter",
}


def create_adapter(
    model_type: str,
    model_path: str,
    provider: str,
    initial_prompt: str = "",
) -> ModelAdapter:
    """Create a model adapter by name."""
    if model_type not in ADAPTER_REGISTRY:
        available = ", ".join(sorted(ADAPTER_REGISTRY))
        raise ValueError(f"Unknown model type '{model_type}'. Available: {available}")

    if model_type == "google":
        from adapters.google_adapter import GoogleSTTAdapter
        return GoogleSTTAdapter()

    if model_type == "faster-whisper":
        from adapters.faster_whisper_adapter import FasterWhisperAdapter
        return FasterWhisperAdapter(
            model_size_or_path=model_path or "large-v3-turbo",
            provider=provider,
        )

    if model_type == "voxtral":
        from adapters.voxtral_adapter import VoxtralAdapter
        if model_path:
            return VoxtralAdapter(model_name=model_path, name=model_path.split("/")[-1])
        return VoxtralAdapter()

    if model_type == "parakeet":
        from adapters.parakeet_adapter import ParakeetAdapter
        return ParakeetAdapter(model_dir=model_path)

    if model_type == "whisper-cpp":
        if not model_path:
            raise ValueError(
                "whisper-cpp requires --model-path pointing to a GGML .bin file"
            )
        from adapters.whisper_cpp_adapter import WhisperCppAdapter
        return WhisperCppAdapter(
            model_path=model_path,
            n_threads=8,
            initial_prompt=initial_prompt,
        )

    from adapters.sherpa_adapter import SherpaAdapter
    return SherpaAdapter(model_path=model_path, provider=provider)


# ── Benchmark Runner ────────────────────────────────────────────────


def run_benchmark(
    adapter: ModelAdapter,
    manifest: dict,
    corpus_dir: Path,
    category_filter: str | None = None,
) -> list[UtteranceResult]:
    """Run the benchmark against all utterances in the manifest."""
    utterances = manifest["utterances"]
    if category_filter:
        utterances = [u for u in utterances if u["category"] == category_filter]

    if not utterances:
        log.warning("No utterances found for category filter: %s", category_filter)
        return []

    total = len(utterances)
    log.info("Running benchmark: %d utterances", total)

    results: list[UtteranceResult] = []

    for i, entry in enumerate(utterances, 1):
        wav_path = corpus_dir / entry["file"]
        if not wav_path.exists():
            log.warning("WAV file missing: %s", wav_path)
            continue

        # Load audio
        samples, sample_rate = read_wav(wav_path)

        # Read the expected_transcription from the manifest. Use a
        # membership check rather than `or` so a deliberately empty
        # canonical form is not silently replaced with the spoken text.
        if "expected_transcription" in entry:
            expected = entry["expected_transcription"]
        else:
            expected = entry["text"]

        # Run inference inside a per-utterance try/except so a single
        # adapter failure does not lose the rest of the run.
        try:
            adapter.reset()
            tr: TranscriptionResult = adapter.transcribe(samples, sample_rate)
            raw_text = tr.text or ""
            elapsed_ms = tr.elapsed_ms
            interim_list = list(tr.interim_results)
            inference_failed = False
        except Exception:
            log.exception("Adapter failed on %s", entry["id"])
            raw_text = ""
            elapsed_ms = 0.0
            interim_list = []
            inference_failed = True

        actual = cleanup_for_display(raw_text)
        exact_match = (not inference_failed) and loose_match(expected, actual)

        # WER stays case-sensitive and runs against the loose-normalized
        # strings so it lines up with the match column.
        norm_expected = _loose_normalize(expected)
        norm_actual = _loose_normalize(actual)
        if norm_expected:
            wer = jiwer.wer(norm_expected, norm_actual) if norm_actual else 1.0
        else:
            wer = 0.0 if not norm_actual else 1.0

        # Interim stability: does the last interim, under the loose match,
        # equal the final actual? Reported only.
        interim_final_match = False
        if interim_list:
            last_interim = cleanup_for_display(interim_list[-1])
            interim_final_match = loose_match(actual, last_interim)

        result = UtteranceResult(
            id=entry["id"],
            category=entry["category"],
            voice=voice_short_name(entry.get("voice", "")),
            spoken_text=entry.get("text", ""),
            expected_transcription=expected,
            actual_transcription=actual,
            exact_match=exact_match,
            wer=wer,
            is_litmus=entry.get("is_litmus", False),
            elapsed_ms=elapsed_ms,
            interim_results=interim_list,
            interim_final_match=interim_final_match,
            punctuation_added=has_punctuation(raw_text),
        )
        results.append(result)

        # Progress indicator
        status = "[+]" if exact_match else "[x]"
        if i % 20 == 0 or not exact_match:
            log.info(
                "  %s [%d/%d] %s: \"%s\" -> \"%s\" (%.0fms)",
                status, i, total, entry["id"],
                expected, actual, elapsed_ms,
            )

    return results


# ── Metrics Computation ─────────────────────────────────────────────


def compute_metrics(results: list[UtteranceResult]) -> dict:
    """Compute aggregate metrics from benchmark results."""
    if not results:
        return {}

    # Per-category
    categories: dict[str, CategoryMetrics] = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = CategoryMetrics()
        cat = categories[r.category]
        cat.count += 1
        cat.exact_matches += int(r.exact_match)
        cat.total_wer += r.wer
        cat.total_ms += r.elapsed_ms

    # Overall
    overall = CategoryMetrics()
    for r in results:
        overall.count += 1
        overall.exact_matches += int(r.exact_match)
        overall.total_wer += r.wer
        overall.total_ms += r.elapsed_ms

    # Litmus counts. Reported as data only -- the harness does not derive
    # a litmus_pass boolean or any other automatic disposition from these.
    litmus_results = [r for r in results if r.is_litmus or r.category == "litmus"]
    litmus_correct = sum(1 for r in litmus_results if r.exact_match)
    litmus_total = len(litmus_results)

    # "delete" counts (across all categories). Reported only.
    delete_results = [r for r in results if r.expected_transcription == "delete"]
    delete_correct = sum(1 for r in delete_results if r.exact_match)
    delete_total = len(delete_results)

    # Interim stability
    with_interims = [r for r in results if r.interim_results]
    interim_stable = sum(1 for r in with_interims if r.interim_final_match)
    interim_total = len(with_interims)

    # Punctuation rate
    punct_count = sum(1 for r in results if r.punctuation_added)

    return {
        "overall": {
            "count": overall.count,
            "exact_match_accuracy": round(overall.exact_match_accuracy, 4),
            "wer": round(overall.avg_wer, 4),
            "avg_inference_ms": round(overall.avg_ms, 1),
            "interim_stability": round(
                interim_stable / interim_total, 4
            ) if interim_total else None,
            "interim_count": interim_total,
            "interim_stable_count": interim_stable,
            "punctuation_rate": round(punct_count / overall.count, 4),
            "litmus_correct": litmus_correct,
            "litmus_total": litmus_total,
            "delete_correct": delete_correct,
            "delete_total": delete_total,
        },
        "by_category": {
            cat: {
                "count": m.count,
                "exact_match_accuracy": round(m.exact_match_accuracy, 4),
                "wer": round(m.avg_wer, 4),
                "avg_inference_ms": round(m.avg_ms, 1),
            }
            for cat, m in sorted(categories.items())
        },
    }


# ── Reporting ───────────────────────────────────────────────────────


def print_report(model_name: str, results: list[UtteranceResult], metrics: dict) -> None:
    """Print a human-readable console report."""
    print()
    print(f"=== STT Benchmark Results: {model_name} ===")
    print()

    # Category table
    header = f"{'Category':<20s} {'Count':>5s} {'Exact Match':>12s} {'WER':>8s} {'Avg ms':>8s}"
    print(header)
    print("-" * len(header))

    for cat, m in sorted(metrics["by_category"].items()):
        print(
            f"{cat:<20s} {m['count']:>5d} "
            f"{m['exact_match_accuracy'] * 100:>10.1f}% "
            f"{m['wer']:>8.3f} "
            f"{m['avg_inference_ms']:>8.1f}"
        )

    print("-" * len(header))
    o = metrics["overall"]
    print(
        f"{'OVERALL':<20s} {o['count']:>5d} "
        f"{o['exact_match_accuracy'] * 100:>10.1f}% "
        f"{o['wer']:>8.3f} "
        f"{o['avg_inference_ms']:>8.1f}"
    )
    print()

    # Interim stability. Reported using the raw counts, not the rounded
    # ratio, so the displayed numerator never drifts.
    interim_total = o["interim_count"]
    if interim_total:
        stable = o["interim_stable_count"]
        ratio = stable / interim_total
        print(
            f"Interim stability: {ratio * 100:.1f}% "
            f"({stable}/{interim_total} last interim == final)"
        )

    # Punctuation rate
    print(f"Punctuation added: {o['punctuation_rate'] * 100:.1f}% of utterances")
    print()

    # Delete recognition counts. Reported only -- no PASS/FAIL banner,
    # because gating is the user's decision based on the per-utterance
    # table below.
    if o["delete_total"]:
        print(
            f"DELETE recognition: {o['delete_correct']}/{o['delete_total']} "
            f"correct"
        )

    # Litmus counts. Same reason: counts only, no automatic verdict.
    if o["litmus_total"]:
        print(
            f"Litmus: {o['litmus_correct']}/{o['litmus_total']} correct"
        )

    print()
    _print_per_utterance_table(results)


def _print_per_utterance_table(results: list[UtteranceResult]) -> None:
    """Print only the failing utterances, fitted to the terminal width.

    Columns are Voice, Spoken, Expected, Actual. Voice is fixed at five
    characters; the three text columns split the remaining width evenly
    and any cell content longer than its column wraps onto additional
    lines within the cell rather than widening the table. The Match
    column is omitted because every printed row is a failure by
    definition.

    Successful rows are not printed; the JSON output still carries every
    row for programmatic cross-referencing. This trades full inspection
    in the terminal for a readable view focused on the rows that need
    attention.
    """
    failures = [r for r in results if not r.exact_match]
    if not failures:
        print("Per-utterance results: all rows passed.")
        print()
        return

    term_w = shutil.get_terminal_size(fallback=(120, 24)).columns
    voice_w = 5
    sep = "  "
    text_columns = 3
    remaining = term_w - voice_w - len(sep) * text_columns
    text_w = max(15, remaining // text_columns)

    headers = ("Voice", "Spoken", "Expected", "Actual")
    widths = (voice_w, text_w, text_w, text_w)
    table_w = sum(widths) + len(sep) * text_columns

    print(f"Per-utterance failures ({len(failures)}/{len(results)}):")
    print()
    header_line = sep.join(h.ljust(w) for h, w in zip(headers, widths))
    print(header_line)
    print("-" * table_w)

    def wrap_cell(text: str, width: int) -> list[str]:
        if not text:
            return [""]
        wrapped = textwrap.wrap(
            text, width=width, break_long_words=True, break_on_hyphens=False,
        )
        return wrapped or [""]

    for r in failures:
        cells = [
            wrap_cell(r.voice, voice_w),
            wrap_cell(r.spoken_text, text_w),
            wrap_cell(r.expected_transcription, text_w),
            wrap_cell(r.actual_transcription, text_w),
        ]
        max_lines = max(len(c) for c in cells)
        for c in cells:
            c.extend([""] * (max_lines - len(c)))
        for i in range(max_lines):
            line = sep.join(
                cells[col][i].ljust(widths[col]) for col in range(4)
            )
            print(line.rstrip())
    print()


# ── Results Output ──────────────────────────────────────────────────


def write_results(
    model_name: str,
    results: list[UtteranceResult],
    metrics: dict,
    output_dir: Path,
) -> Path:
    """Write detailed results to a JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"{model_name}_{timestamp}.json"
    output_path = output_dir / filename

    failures = [r for r in results if not r.exact_match]

    output = {
        "model": model_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "corpus_utterances": len(results),
        **metrics,
        "failures": [asdict(r) for r in failures],
        "utterances": [asdict(r) for r in results],
    }

    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    log.info("Results written: %s", output_path)
    return output_path


# ── CLI ─────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="STT Benchmark Harness -- evaluate models against TTS corpus",
    )
    parser.add_argument(
        "--model", required=True,
        help="Adapter name: sherpa-zipformer, sherpa-lstm, google, faster-whisper",
    )
    parser.add_argument(
        "--model-path", default="",
        help="Path to model weights directory (not needed for google)",
    )
    parser.add_argument(
        "--provider", default="cpu",
        help="Inference provider: cpu (default), cuda",
    )
    parser.add_argument(
        "--corpus", default=None,
        help="Path to corpus directory (default: evaluation/corpus)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to results directory (default: evaluation/results)",
    )
    parser.add_argument(
        "--category", default=None,
        help="Run only one category (e.g., litmus for quick check)",
    )
    parser.add_argument(
        "--initial-prompt", default="",
        help="Initial prompt text (whisper-cpp only). When set, the "
             "whisper-cpp adapter runs with production Vulkan Small decoder "
             "knobs plus this prompt, producing a calibrated production-parity "
             "baseline. Without this flag the adapter runs bare whisper.cpp.",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point."""
    args = parse_args()

    eval_dir = Path(__file__).parent
    corpus_dir = Path(args.corpus) if args.corpus else eval_dir / "corpus"
    output_dir = Path(args.output) if args.output else eval_dir / "results"

    # Load manifest
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.exists():
        log.error("Manifest not found: %s", manifest_path)
        log.error("Run generate_corpus.py first to create the test corpus.")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    log.info(
        "Loaded manifest: %d utterances from %s",
        manifest["utterance_count"], manifest_path,
    )

    # Create adapter
    adapter = create_adapter(
        args.model,
        args.model_path,
        args.provider,
        initial_prompt=args.initial_prompt,
    )
    # wh-as9: park the adapter on a module global so it survives main()
    # returning. After a full-corpus CUDA run, the ctranslate2 model
    # DESTRUCTOR itself crashes the process (0xC0000409 fail-fast) when
    # the adapter's refcount drops on return -- before __main__'s
    # teardown-skipping exit can run. Keeping the reference alive means
    # no destructor ever runs; TerminateProcess in __main__ then ends the
    # process without native teardown.
    globals()["_ADAPTER_KEEPALIVE"] = adapter
    log.info("Model: %s", adapter.name)

    # Run benchmark
    results = run_benchmark(adapter, manifest, corpus_dir, args.category)
    if not results:
        log.error("No results produced.")
        return 1

    # Compute metrics
    metrics = compute_metrics(results)

    # Report
    print_report(adapter.name, results, metrics)

    # Write results
    results_path = write_results(adapter.name, results, metrics, output_dir)
    print(f"Results saved: {results_path}")

    return 0


if __name__ == "__main__":
    rc = main()
    # wh-as9: exit without native library teardown. ctranslate2 4.7.1 +
    # CUDA on Windows has a known teardown race at process exit (upstream
    # SYSTRAN/faster-whisper#71): after a full-corpus CUDA run the process
    # dies AFTER "Results written" (0xFFFFFFFF on the April-2026 driver,
    # 0xC0000409 fail-fast on driver 591.86) -- the JSON is valid but the
    # returncode is noise, and callers (benchmark-stt-candidates.py) had
    # to invent ok_despite_crash to tell that apart from a real failure.
    # os._exit is NOT sufficient: its ExitProcess still runs
    # DLL_PROCESS_DETACH, which is where the CUDA teardown crashes.
    # TerminateProcess on the current process skips DLL detach entirely.
    # All results are already flushed to disk by this point; short/litmus
    # runs exit clean either way (reproduced 2026-07-05).
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), rc & 0xFF)
    os._exit(rc)
