"""Tests for the transcribe.cpp benchmark adapter.

The model and the device list are mocked, so these tests need neither
the transcribe_cpp package nor its native library.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from adapters.base import ModelAdapter, TranscriptionResult
from adapters.transcribe_cpp_adapter import TranscribeCppAdapter

GPU = SimpleNamespace(
    name="Vulkan0", description="NVIDIA GeForce RTX 3060", kind="vulkan",
    memory_total=12_696_158_208,
)
CPU = SimpleNamespace(
    name="CPU", description="12th Gen Intel(R) Core(TM) i7-12700F", kind="cpu",
    memory_total=34_163_920_896,
)


def _model_on(device, text="hello world"):
    """A mock transcribe_cpp.Model whose model.device is *device*."""
    model = MagicMock()
    model.arch = "parakeet"
    model.variant = "tdt-0.6b-v3"
    model.device = device
    model.session.return_value.run.return_value = SimpleNamespace(text=text)
    return model


@pytest.fixture
def devices():
    with patch("adapters.transcribe_cpp_adapter._list_devices", return_value=[GPU, CPU]):
        yield


# -- Backend check (the wh-kft failure: a silent fallback corrupts latency) --


@pytest.mark.usefixtures("devices")
class TestBackendRefusal:
    """The adapter must refuse a model that runs on another backend."""

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_vulkan_request_on_cpu_device_refused(self, mock_load):
        mock_load.return_value = _model_on(CPU)
        with pytest.raises(RuntimeError) as excinfo:
            TranscribeCppAdapter(model_path="/fake/model.gguf", backend="vulkan")
        message = str(excinfo.value)
        assert "vulkan" in message and "cpu" in message

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_cpu_request_on_vulkan_device_refused(self, mock_load):
        mock_load.return_value = _model_on(GPU)
        with pytest.raises(RuntimeError) as excinfo:
            TranscribeCppAdapter(model_path="/fake/model.gguf", backend="cpu")
        message = str(excinfo.value)
        assert "vulkan" in message and "cpu" in message

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_refusal_opens_no_session(self, mock_load):
        model = _model_on(CPU)
        mock_load.return_value = model
        with pytest.raises(RuntimeError):
            TranscribeCppAdapter(model_path="/fake/model.gguf", backend="vulkan")
        model.session.assert_not_called()

    @pytest.mark.parametrize("backend, device", [("vulkan", GPU), ("cpu", CPU)])
    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_matching_backend_accepted(self, mock_load, backend, device):
        mock_load.return_value = _model_on(device)
        adapter = TranscribeCppAdapter(model_path="/fake/model.gguf", backend=backend)
        mock_load.assert_called_once_with("/fake/model.gguf", backend)
        assert adapter.name.endswith(f"-{backend}_t4")

    def test_unknown_backend_rejected_before_load(self):
        with patch("adapters.transcribe_cpp_adapter._load_model") as mock_load:
            with pytest.raises(ValueError, match="cpu, vulkan"):
                TranscribeCppAdapter(model_path="/fake/model.gguf", backend="cuda")
            mock_load.assert_not_called()

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_reported_devices_logged(self, mock_load, caplog):
        mock_load.return_value = _model_on(GPU)
        with caplog.at_level(logging.INFO, logger="adapters.transcribe_cpp_adapter"):
            TranscribeCppAdapter(model_path="/fake/model.gguf", backend="vulkan")
        text = caplog.text
        assert "NVIDIA GeForce RTX 3060" in text
        assert "12th Gen Intel(R) Core(TM) i7-12700F" in text


# -- Protocol and transcription --


@pytest.mark.usefixtures("devices")
class TestTranscription:

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_is_model_adapter(self, mock_load):
        mock_load.return_value = _model_on(GPU)
        adapter = TranscribeCppAdapter(model_path="/models/parakeet-tdt-0.6b-v3-F32.gguf")
        assert isinstance(adapter, ModelAdapter)
        assert adapter.name == "transcribe-cpp-parakeet-tdt-0.6b-v3-F32-vulkan_t4"

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_threads_passed_to_session(self, mock_load):
        model = _model_on(CPU)
        mock_load.return_value = model
        TranscribeCppAdapter(model_path="/fake/model.gguf", backend="cpu", n_threads=2)
        model.session.assert_called_once_with(n_threads=2)

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_returns_stripped_text(self, mock_load):
        model = _model_on(GPU, text=" Go to top. ")
        mock_load.return_value = model
        adapter = TranscribeCppAdapter(model_path="/fake/model.gguf")
        samples = np.array([0.0, 0.5, -0.5], dtype=np.float32)
        result = adapter.transcribe(samples, 16000)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Go to top."
        assert result.interim_results == []
        assert result.elapsed_ms >= 0
        last_call = model.session.return_value.run.call_args
        np.testing.assert_array_equal(last_call.args[0], samples)

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_warm_up_run_before_first_utterance(self, mock_load):
        model = _model_on(GPU)
        mock_load.return_value = model
        TranscribeCppAdapter(model_path="/fake/model.gguf")
        run = model.session.return_value.run
        run.assert_called_once()
        assert len(run.call_args.args[0]) == 16000

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_other_sample_rate_rejected(self, mock_load):
        mock_load.return_value = _model_on(GPU)
        adapter = TranscribeCppAdapter(model_path="/fake/model.gguf")
        with pytest.raises(ValueError, match="16000"):
            adapter.transcribe(np.zeros(8000, dtype=np.float32), 8000)

    @patch("adapters.transcribe_cpp_adapter._load_model")
    def test_reset_is_noop(self, mock_load):
        mock_load.return_value = _model_on(GPU)
        TranscribeCppAdapter(model_path="/fake/model.gguf").reset()
