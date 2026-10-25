"""Latency spread, process memory, and the transcribe-cpp entry in the runner.

wh-transcribe-cpp-eval-adapter compares backends by elapsed_ms median and
p95 and by the peak working set of the benchmark process, so the result
JSON must carry all three.
"""

import sys

import pytest

from run_benchmark import UtteranceResult, compute_metrics, create_adapter, process_stats


def _result(elapsed_ms: float) -> UtteranceResult:
    return UtteranceResult(
        id="u", category="c", voice="aria", spoken_text="go", expected_transcription="go",
        actual_transcription="go", exact_match=True, wer=0.0, is_litmus=False,
        elapsed_ms=elapsed_ms,
    )


class TestLatencySpread:

    def test_median_and_p95_of_elapsed_ms(self):
        # 0, 10, ..., 200 ms in shuffled order: the median is 100 and the
        # linear-interpolation p95 falls exactly on the 20th value, 190.
        times = [float(ms) for ms in range(0, 201, 10)]
        times = times[1::2] + times[::2]
        metrics = compute_metrics([_result(ms) for ms in times])
        overall = metrics["overall"]
        assert overall["median_inference_ms"] == 100.0
        assert overall["p95_inference_ms"] == 190.0
        assert overall["avg_inference_ms"] == 100.0

    def test_p95_sees_one_slow_tail(self):
        # 19 fast utterances and one slow one: the median stays fast, and
        # the p95 moves toward the slow tail.
        metrics = compute_metrics([_result(50.0)] * 19 + [_result(5000.0)])
        overall = metrics["overall"]
        assert overall["median_inference_ms"] == 50.0
        assert overall["p95_inference_ms"] == 297.5


class TestProcessStats:

    def test_pid_and_cpu_count(self):
        stats = process_stats()
        assert stats["pid"] > 0
        assert stats["cpu_count"] >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows process counters")
    def test_peak_working_set_and_affinity_on_windows(self):
        ballast = bytearray(64 * 1024 * 1024)  # 64 MB, touched below
        ballast[::4096] = b"x" * len(ballast[::4096])
        stats = process_stats()
        assert stats["peak_working_set_bytes"] >= 64 * 1024 * 1024
        assert stats["peak_working_set_bytes"] >= stats["working_set_bytes"] > 0
        # The default affinity allows at least one logical processor.
        assert stats["affinity_mask"] > 0
        assert stats["affinity_processors"] == bin(stats["affinity_mask"]).count("1")
        del ballast


class TestTranscribeCppEntry:

    def test_requires_model_path(self):
        with pytest.raises(ValueError, match="--model-path"):
            create_adapter("transcribe-cpp", "", "vulkan")

    def test_provider_names_the_backend(self):
        # --provider carries the backend. A value the adapter does not
        # know fails before any model loads.
        with pytest.raises(ValueError, match="cpu, vulkan"):
            create_adapter("transcribe-cpp", "/fake/model.gguf", "cuda")
