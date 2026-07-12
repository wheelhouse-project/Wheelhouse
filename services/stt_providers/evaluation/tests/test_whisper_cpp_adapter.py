"""Tests for the whisper.cpp benchmark adapter.

Validates that WhisperCppAdapter conforms to the ModelAdapter protocol,
correctly passes audio to the whisper.cpp engine via pywhispercpp, and
properly extracts transcription text.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from adapters.base import ModelAdapter, TranscriptionResult
from adapters.whisper_cpp_adapter import WhisperCppAdapter


# -- Protocol Conformance --


class TestProtocolConformance:
    """WhisperCppAdapter must satisfy the ModelAdapter protocol."""

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_is_model_adapter(self, mock_load):
        """Adapter instance must pass isinstance check against ModelAdapter."""
        mock_load.return_value = MagicMock()
        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        assert isinstance(adapter, ModelAdapter)

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_has_name(self, mock_load):
        """Adapter must expose a name attribute."""
        mock_load.return_value = MagicMock()
        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        assert isinstance(adapter.name, str)
        assert len(adapter.name) > 0

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_custom_name(self, mock_load):
        """Adapter should accept a custom name."""
        mock_load.return_value = MagicMock()
        adapter = WhisperCppAdapter(model_path="/fake/model.bin", name="my-whisper")
        assert adapter.name == "my-whisper"

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_default_name_from_model_path(self, mock_load):
        """Default name should be derived from the model filename."""
        mock_load.return_value = MagicMock()
        adapter = WhisperCppAdapter(model_path="/models/ggml-small.en.bin")
        assert adapter.name == "whisper-cpp-small.en"


# -- Transcription --


class TestTranscription:
    """Adapter must return proper TranscriptionResult."""

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_returns_transcription_result(self, mock_load):
        """transcribe() must return a TranscriptionResult."""
        mock_model = MagicMock()
        mock_seg = MagicMock()
        mock_seg.text = " hello world"
        mock_model.transcribe.return_value = [mock_seg]
        mock_load.return_value = mock_model

        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert isinstance(result, TranscriptionResult)
        assert result.elapsed_ms > 0
        assert result.interim_results == []

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_text_extracted_and_stripped(self, mock_load):
        """Segment text should be stripped and joined."""
        mock_model = MagicMock()
        seg1 = MagicMock()
        seg1.text = " hello"
        seg2 = MagicMock()
        seg2.text = " world"
        mock_model.transcribe.return_value = [seg1, seg2]
        mock_load.return_value = mock_model

        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == "hello world"

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_empty_segments(self, mock_load):
        """No segments should return empty string."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = []
        mock_load.return_value = mock_model

        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == ""

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_numpy_array_passed_directly(self, mock_load):
        """Float32 numpy array should be passed to model.transcribe() as-is."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = []
        mock_load.return_value = mock_model

        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        adapter.transcribe(samples, 16000)

        mock_model.transcribe.assert_called_once()
        call_args = mock_model.transcribe.call_args
        np.testing.assert_array_equal(call_args[0][0], samples)


# -- Configuration --


class TestConfiguration:
    """Adapter should pass configuration to whisper.cpp model."""

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_n_threads_passed(self, mock_load):
        """n_threads should be forwarded to model constructor."""
        mock_load.return_value = MagicMock()
        WhisperCppAdapter(model_path="/fake/model.bin", n_threads=4)
        mock_load.assert_called_once_with("/fake/model.bin", n_threads=4)

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_default_threads_is_eight(self, mock_load):
        """Default thread count should be 8 (P-cores on i7-12700F)."""
        mock_load.return_value = MagicMock()
        WhisperCppAdapter(model_path="/fake/model.bin")
        mock_load.assert_called_once_with("/fake/model.bin", n_threads=8)

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_language_set_to_english(self, mock_load):
        """Transcribe should force English language."""
        mock_model = MagicMock()
        mock_model.transcribe.return_value = []
        mock_load.return_value = mock_model

        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        mock_model.transcribe.assert_called_once()
        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs.kwargs.get("language") == "en" or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] == "en"
        )


# -- Reset --


class TestReset:
    """Reset should be a safe no-op."""

    @patch("adapters.whisper_cpp_adapter._load_model")
    def test_reset_is_noop(self, mock_load):
        """reset() should not raise."""
        mock_load.return_value = MagicMock()
        adapter = WhisperCppAdapter(model_path="/fake/model.bin")
        adapter.reset()


# -- Vulkan fallback warning (wh-kft) --


class TestVulkanFallbackWarning:
    """A silent CPU-only pywhispercpp build corrupts benchmark latency
    numbers -- the champion decisions rest on them. Adapter init must
    say so loudly."""

    @patch("adapters.whisper_cpp_adapter._load_model")
    @patch(
        "adapters.whisper_cpp_adapter._vulkan_wheel_installed",
        return_value=False,
    )
    def test_cpu_only_build_warns(self, mock_vk, mock_load, caplog):
        import logging

        mock_load.return_value = MagicMock()
        with caplog.at_level(logging.WARNING):
            WhisperCppAdapter(model_path="/fake/model.bin")
        assert any(
            "ggml-vulkan" in r.message and "CPU-only" in r.message
            for r in caplog.records
        )

    @patch("adapters.whisper_cpp_adapter._load_model")
    @patch(
        "adapters.whisper_cpp_adapter._vulkan_wheel_installed",
        return_value=True,
    )
    def test_vulkan_build_no_warning(self, mock_vk, mock_load, caplog):
        import logging

        mock_load.return_value = MagicMock()
        with caplog.at_level(logging.WARNING):
            WhisperCppAdapter(model_path="/fake/model.bin")
        assert not any("CPU-only" in r.message for r in caplog.records)

    def test_this_venv_has_the_vulkan_wheel(self):
        # Canary for the Vulkan Wheel Protection rule in CLAUDE.md: if a
        # dependency operation ever swaps the vendored Vulkan wheel for
        # the CPU-only PyPI build, this fails before any benchmark lies.
        pytest.importorskip("pywhispercpp")
        from adapters.whisper_cpp_adapter import _vulkan_wheel_installed

        assert _vulkan_wheel_installed() is True
