"""Tests for the Google Cloud STT benchmark adapter.

Validates that GoogleSTTAdapter conforms to the ModelAdapter protocol,
correctly converts audio formats, and properly wraps the synchronous
Google Cloud STT recognize() API.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from adapters.base import ModelAdapter, TranscriptionResult
from adapters.google_adapter import GoogleSTTAdapter


# ── Protocol Conformance ───────────────────────────────────────────


class TestProtocolConformance:
    """GoogleSTTAdapter must satisfy the ModelAdapter protocol."""

    def test_is_model_adapter(self):
        """Adapter instance must pass isinstance check against ModelAdapter."""
        adapter = GoogleSTTAdapter(client=MagicMock())
        assert isinstance(adapter, ModelAdapter)

    def test_has_name(self):
        """Adapter must expose a name attribute."""
        adapter = GoogleSTTAdapter(client=MagicMock())
        assert isinstance(adapter.name, str)
        assert len(adapter.name) > 0

    def test_custom_name(self):
        """Adapter should accept a custom name."""
        adapter = GoogleSTTAdapter(client=MagicMock(), name="my-google-model")
        assert adapter.name == "my-google-model"


# ── Audio Conversion ──────────────────────────────────────────────


class TestAudioConversion:
    """Adapter must convert float32 samples to int16 PCM bytes for Google."""

    def test_float32_to_int16_conversion(self):
        """Float32 [-1.0, 1.0] samples should become int16 PCM bytes."""
        mock_client = MagicMock()
        # Set up mock to return empty results
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)

        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        adapter.transcribe(samples, 16000)

        # Verify recognize was called
        mock_client.recognize.assert_called_once()
        call_kwargs = mock_client.recognize.call_args

        # The audio content should be int16 PCM bytes (2 bytes per sample)
        audio = call_kwargs.kwargs["audio"]
        assert len(audio.content) == len(samples) * 2  # int16 = 2 bytes each

    def test_sample_rate_passed_to_config(self):
        """The sample rate from transcribe() should be used in the config."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        samples = np.zeros(1600, dtype=np.float32)
        adapter.transcribe(samples, 16000)

        call_kwargs = mock_client.recognize.call_args
        config = call_kwargs.kwargs["config"]
        assert config.sample_rate_hertz == 16000


# ── Transcription ─────────────────────────────────────────────────


class TestTranscription:
    """Adapter must return proper TranscriptionResult from Google API responses."""

    def _make_mock_response(self, transcript: str, confidence: float = 0.95):
        """Build a mock Google recognize() response."""
        mock_alt = MagicMock()
        mock_alt.transcript = transcript
        mock_alt.confidence = confidence

        mock_result = MagicMock()
        mock_result.alternatives = [mock_alt]

        mock_response = MagicMock()
        mock_response.results = [mock_result]
        return mock_response

    def test_single_result(self):
        """Should extract transcript from a single result."""
        mock_client = MagicMock()
        mock_client.recognize.return_value = self._make_mock_response("hello world")

        adapter = GoogleSTTAdapter(client=mock_client)
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert isinstance(result, TranscriptionResult)
        assert result.text == "hello world"
        assert result.elapsed_ms > 0
        assert result.interim_results == []

    def test_multiple_results_concatenated(self):
        """Multiple result segments should be joined with spaces."""
        mock_alt1 = MagicMock()
        mock_alt1.transcript = "hello"
        mock_result1 = MagicMock()
        mock_result1.alternatives = [mock_alt1]

        mock_alt2 = MagicMock()
        mock_alt2.transcript = " world"
        mock_result2 = MagicMock()
        mock_result2.alternatives = [mock_alt2]

        mock_response = MagicMock()
        mock_response.results = [mock_result1, mock_result2]

        mock_client = MagicMock()
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == "hello world"

    def test_empty_results(self):
        """Empty results should return empty string."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == ""

    def test_no_alternatives(self):
        """Result with empty alternatives should produce empty text."""
        mock_result = MagicMock()
        mock_result.alternatives = []

        mock_response = MagicMock()
        mock_response.results = [mock_result]

        mock_client = MagicMock()
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        result = adapter.transcribe(np.zeros(16000, dtype=np.float32), 16000)

        assert result.text == ""


# ── Reset ──────────────────────────────────────────────────────────


class TestReset:
    """Reset should be a safe no-op."""

    def test_reset_is_noop(self):
        """reset() should not raise and should not affect subsequent calls."""
        mock_client = MagicMock()
        adapter = GoogleSTTAdapter(client=mock_client)
        adapter.reset()  # Should not raise


# ── Configuration ──────────────────────────────────────────────────


class TestConfiguration:
    """Adapter should use correct Google STT settings."""

    def test_default_model_is_latest_short(self):
        """Default model should be 'latest_short' (optimized for short utterances)."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)

        call_kwargs = mock_client.recognize.call_args
        config = call_kwargs.kwargs["config"]
        assert config.model == "latest_short"

    def test_auto_punctuation_mirrors_production(self):
        """The adapter must mirror the production flag, whatever its value.

        Hardcoding the expected value here made the test stale the moment
        production flipped the flag (it has been false in the committed
        google_stt_server config since the early cleanup commits). The
        contract is "benchmark matches production", so read production.
        """
        from adapters.google_adapter import _load_production_config

        expected = _load_production_config()["enable_automatic_punctuation"]
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)

        call_kwargs = mock_client.recognize.call_args
        config = call_kwargs.kwargs["config"]
        assert config.enable_automatic_punctuation is expected

    def test_language_code_en_us(self):
        """Language should be en-US to match production."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.results = []
        mock_client.recognize.return_value = mock_response

        adapter = GoogleSTTAdapter(client=mock_client)
        adapter.transcribe(np.zeros(1600, dtype=np.float32), 16000)

        call_kwargs = mock_client.recognize.call_args
        config = call_kwargs.kwargs["config"]
        assert config.language_code == "en-US"
