"""Tests for the sherpa-onnx benchmark adapter's language option.

Nemotron 3.5 ASR is a multilingual streaming model. sherpa-onnx selects its
language per stream through OnlineStream.set_option("language", ...); with
no option set, the model detects the language itself. The benchmark needs
to force a language so that its numbers match an English-only deployment.
"""

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
