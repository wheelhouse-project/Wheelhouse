"""Tests for the faster-whisper benchmark adapter.

Validates that FasterWhisperAdapter conforms to the ModelAdapter protocol,
correctly passes float32 audio to the CTranslate2 Whisper model, and
properly concatenates segment outputs.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from adapters.base import ModelAdapter, TranscriptionResult
from adapters.faster_whisper_adapter import FasterWhisperAdapter


# ── Protocol Conformance ───────────────────────────────────────────


class TestProtocolConformance:
    """FasterWhisperAdapter must satisfy the ModelAdapter protocol."""

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_is_model_adapter(self, mock_model_cls):
        """Adapter instance must pass isinstance check against ModelAdapter."""
        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        assert isinstance(adapter, ModelAdapter)

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_has_name(self, mock_model_cls):
        """Adapter must expose a name attribute."""
        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        assert isinstance(adapter.name, str)
        assert len(adapter.name) > 0

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_custom_name(self, mock_model_cls):
        """Adapter should accept a custom name."""
        adapter = FasterWhisperAdapter(
            model_size_or_path="tiny", name="my-whisper"
        )
        assert adapter.name == "my-whisper"

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_default_name_includes_model_size(self, mock_model_cls):
        """Default name should reflect the model size."""
        adapter = FasterWhisperAdapter(model_size_or_path="large-v3-turbo")
        assert "large-v3-turbo" in adapter.name


# ── Transcription ─────────────────────────────────────────────────


class TestTranscription:
    """Adapter must return proper TranscriptionResult from Whisper segments."""

    def _make_mock_segment(self, text: str) -> MagicMock:
        """Build a mock faster-whisper Segment with given text."""
        segment = MagicMock()
        segment.text = text
        return segment

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_single_segment(self, mock_model_cls):
        """Should extract text from a single transcription segment."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (
            iter([self._make_mock_segment(" hello world")]),
            mock_info,
        )

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "hello world"
        assert result.elapsed_ms > 0
        assert result.interim_results == []

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_multiple_segments_concatenated(self, mock_model_cls):
        """Multiple segments should be joined into a single string."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (
            iter([
                self._make_mock_segment(" hello"),
                self._make_mock_segment(" world"),
            ]),
            mock_info,
        )

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == "hello world"

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_empty_segments(self, mock_model_cls):
        """No segments should return empty string."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (iter([]), mock_info)

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == ""

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_segments_with_leading_trailing_whitespace(self, mock_model_cls):
        """Segment text with extra whitespace should be cleaned up."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (
            iter([self._make_mock_segment("  hello world  ")]),
            mock_info,
        )

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        # Final text should be stripped
        assert result.text == "hello world"


# ── Audio Input ───────────────────────────────────────────────────


class TestAudioInput:
    """Adapter passes float32 samples directly to Whisper (no PCM conversion)."""

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_float32_passed_directly(self, mock_model_cls):
        """Float32 numpy array should be passed to model.transcribe() as-is."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (iter([]), mock_info)

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        adapter.transcribe(samples, 16000)

        # Verify transcribe was called with the original samples
        mock_model.transcribe.assert_called_once()
        call_args = mock_model.transcribe.call_args
        np.testing.assert_array_equal(call_args[0][0], samples)

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_language_set_to_english(self, mock_model_cls):
        """Transcribe should force English language for this benchmark."""
        mock_model = mock_model_cls.return_value
        mock_info = MagicMock()
        mock_model.transcribe.return_value = (iter([]), mock_info)

        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)

        call_kwargs = mock_model.transcribe.call_args
        assert call_kwargs.kwargs.get("language") == "en"


# ── Configuration ─────────────────────────────────────────────────


class TestConfiguration:
    """Adapter should map provider to correct device/compute_type settings."""

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_cuda_provider_uses_float16(self, mock_model_cls):
        """provider='cuda' should create model with device='cuda', compute_type='float16'."""
        FasterWhisperAdapter(model_size_or_path="large-v3-turbo", provider="cuda")

        mock_model_cls.assert_called_once_with(
            "large-v3-turbo", device="cuda", compute_type="float16"
        )

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_cpu_provider_uses_int8(self, mock_model_cls):
        """provider='cpu' should create model with device='cpu', compute_type='int8'."""
        FasterWhisperAdapter(model_size_or_path="small.en", provider="cpu")

        mock_model_cls.assert_called_once_with(
            "small.en", device="cpu", compute_type="int8"
        )

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_default_provider_is_cpu(self, mock_model_cls):
        """Default provider should be 'cpu' for safety."""
        FasterWhisperAdapter(model_size_or_path="tiny")

        mock_model_cls.assert_called_once_with(
            "tiny", device="cpu", compute_type="int8"
        )


# ── Reset ──────────────────────────────────────────────────────────


class TestReset:
    """Reset should be a safe no-op."""

    @patch("adapters.faster_whisper_adapter.WhisperModel")
    def test_reset_is_noop(self, mock_model_cls):
        """reset() should not raise and should not affect subsequent calls."""
        adapter = FasterWhisperAdapter(model_size_or_path="tiny")
        adapter.reset()  # Should not raise
