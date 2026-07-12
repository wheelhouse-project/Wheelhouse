"""Tests for WakeWordDetector wrapper."""
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestWakeWordDetectorInit:
    """Test detector initialization and model loading."""

    def test_init_with_valid_local_model(self, tmp_path):
        """Detector loads a model file from the local model directory."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake model data")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )
            assert detector.is_loaded
            mock_oww.Model.assert_called_once()

    def test_init_with_missing_model_not_loaded(self, tmp_path):
        """Detector gracefully handles missing model file."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="nonexistent",
            model_dir=str(tmp_path),
            sensitivity=0.5,
        )
        assert not detector.is_loaded

    def test_init_downloads_missing_openwakeword_resources(self, tmp_path):
        """Missing openwakeword ONNX resources trigger lazy download before load."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake model data")

        oww_pkg = tmp_path / "openwakeword_pkg"
        resources_dir = oww_pkg / "resources" / "models"
        resources_dir.mkdir(parents=True)
        fake_init = oww_pkg / "__init__.py"
        fake_init.write_text("# fake openwakeword package marker\n", encoding="utf-8")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_oww.__file__ = str(fake_init)
            mock_oww.utils.download_models = MagicMock()
            mock_oww.Model.return_value = MagicMock()

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            assert detector.is_loaded
            mock_oww.utils.download_models.assert_called_once_with(
                model_names=["__wheelhouse_noop__"],
                target_directory=str(resources_dir),
            )

    def test_init_with_disabled_flag(self, tmp_path):
        """Detector skips loading when enabled=False."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="test_word",
            model_dir=str(tmp_path),
            sensitivity=0.5,
            enabled=False,
        )
        assert not detector.is_loaded


class TestWakeWordDetectorProcess:
    """Test audio frame processing and detection."""

    def test_process_returns_none_when_no_detection(self, tmp_path):
        """Processing audio with no wake word returns None."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.1}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            frame = b"\x00" * 2560
            result = detector.process(frame)
            assert result is None

    def test_process_returns_keyword_on_detection(self, tmp_path):
        """Processing audio with wake word returns the keyword name."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.8}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )

            frame = b"\x00" * 2560
            result = detector.process(frame)
            assert result == "test_word"

    def test_process_when_not_loaded_returns_none(self, tmp_path):
        """Processing when model failed to load returns None safely."""
        from shared_stt.wake_word_detector import WakeWordDetector

        detector = WakeWordDetector(
            keyword="missing",
            model_dir=str(tmp_path),
            sensitivity=0.5,
        )
        frame = b"\x00" * 2560
        result = detector.process(frame)
        assert result is None


class TestWakeWordDetectorReset:
    """Test detector reset between activations."""

    def test_reset_clears_internal_state(self, tmp_path):
        """Reset clears openWakeWord's prediction buffer."""
        model_file = tmp_path / "test_word_v2.onnx"
        model_file.write_bytes(b"fake")

        from shared_stt.wake_word_detector import WakeWordDetector

        with patch("shared_stt.wake_word_detector.openwakeword") as mock_oww:
            mock_model = MagicMock()
            mock_model.predict.return_value = {"test_word": 0.0}
            mock_oww.Model.return_value = mock_model

            detector = WakeWordDetector(
                keyword="test_word",
                model_dir=str(tmp_path),
                sensitivity=0.5,
            )
            detector.reset()
            mock_model.reset.assert_called_once()
