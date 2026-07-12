"""Wake word detector wrapper around openWakeWord.

Handles model loading (local + download-on-demand), audio frame processing,
and detection threshold comparison. Designed to be instantiated and destroyed
on demand by the STT server's audio processing loop.

Usage:
    detector = WakeWordDetector(keyword="computer", model_dir="data/wake_words")
    if detector.is_loaded:
        result = detector.process(pcm_bytes)  # returns keyword name or None
"""
import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    import openwakeword
except ImportError:
    openwakeword = None  # type: ignore[assignment]
    logger.warning("openwakeword not installed - wake word detection unavailable")

_MODEL_URL_TEMPLATES = [
    "https://github.com/fwartner/home-assistant-wakewords-collection/raw/main/en/{keyword}/{keyword}_v2.onnx",
    "https://github.com/fwartner/home-assistant-wakewords-collection/raw/main/en/{keyword}/{keyword}_v1.onnx",
    "https://github.com/fwartner/home-assistant-wakewords-collection/raw/main/en/{keyword}/{keyword}.onnx",
]

_DOWNLOAD_TIMEOUT_S = 15


class WakeWordDetector:
    """Wraps openWakeWord for wake word detection in STT audio pipeline.

    Attributes:
        is_loaded: True if a model was successfully loaded and detection is available.
    """

    def __init__(
        self,
        keyword: str,
        model_dir: str,
        sensitivity: float = 0.5,
        enabled: bool = True,
    ):
        self.keyword = keyword
        self.model_dir = Path(model_dir)
        self.sensitivity = sensitivity
        self.is_loaded = False
        self._model = None

        if not enabled or openwakeword is None:
            return

        model_path = self._resolve_model_path()
        if model_path:
            self._load_model(model_path)

    def _resolve_model_path(self) -> Optional[Path]:
        """Find a local model file, or attempt to download one."""
        for suffix in ["_v2.onnx", "_v1.onnx", ".onnx"]:
            candidate = self.model_dir / f"{self.keyword}{suffix}"
            if candidate.exists():
                logger.info(f"Wake word model found: {candidate}")
                return candidate
        return self._download_model()

    def _download_model(self) -> Optional[Path]:
        """Try to download a wake word model from known sources."""
        self.model_dir.mkdir(parents=True, exist_ok=True)
        for url_template in _MODEL_URL_TEMPLATES:
            url = url_template.format(keyword=self.keyword)
            filename = url.split("/")[-1]
            target = self.model_dir / filename
            try:
                import urllib.request
                logger.info(f"Downloading wake word model: {url}")
                urllib.request.urlretrieve(url, str(target))
                if target.stat().st_size > 0:
                    logger.info(
                        f"Wake word model downloaded: {target} "
                        f"({target.stat().st_size} bytes)"
                    )
                    return target
                else:
                    target.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to download {url}: {e}")
                target.unlink(missing_ok=True)
                continue
        logger.warning(
            f"Could not find or download wake word model for '{self.keyword}'"
        )
        return None

    def _load_model(self, model_path: Path) -> None:
        """Load an openWakeWord model from disk."""
        try:
            self._ensure_openwakeword_resources()
            self._model = openwakeword.Model(
                wakeword_models=[str(model_path)],
                inference_framework="onnx",
            )
            self.is_loaded = True
            logger.info(
                f"Wake word detector loaded: '{self.keyword}' "
                f"(sensitivity={self.sensitivity})"
            )
        except Exception as e:
            logger.error(f"Failed to load wake word model {model_path}: {e}")
            self._model = None
            self.is_loaded = False

    def _ensure_openwakeword_resources(self) -> None:
        """Ensure openWakeWord feature models are available for ONNX inference.

        Some openwakeword installs are missing bundled resources/models/*.onnx.
        In that case, lazily download required resources once before model load.
        """
        package_file = getattr(openwakeword, "__file__", "")
        if not isinstance(package_file, str) or not package_file:
            return

        resources_dir = Path(package_file).resolve().parent / "resources" / "models"
        required = ("melspectrogram.onnx", "embedding_model.onnx")
        missing = [name for name in required if not (resources_dir / name).exists()]
        if not missing:
            return

        utils = getattr(openwakeword, "utils", None)
        downloader = getattr(utils, "download_models", None) if utils else None
        if not callable(downloader):
            logger.warning(
                "openwakeword resources missing (%s) and download_models unavailable",
                ", ".join(missing),
            )
            return

        logger.info(
            "Downloading missing openwakeword resources: %s",
            ", ".join(missing),
        )
        try:
            # Use a non-matching model name so download_models fetches only core
            # resources (feature + VAD) and skips all optional wakeword packs.
            downloader(
                model_names=["__wheelhouse_noop__"],
                target_directory=str(resources_dir),
            )
        except Exception as e:
            logger.warning("Failed to download openwakeword resources: %s", e)

    def process(self, pcm_bytes: bytes) -> Optional[str]:
        """Process a PCM audio frame and check for wake word detection.

        Args:
            pcm_bytes: Raw PCM audio bytes (16-bit signed, 16kHz mono).

        Returns:
            The keyword name if detected, None otherwise.
        """
        if not self.is_loaded or self._model is None:
            return None
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16)
            predictions = self._model.predict(audio)
            for model_name, confidence in predictions.items():
                if confidence >= self.sensitivity:
                    logger.info(
                        f"Wake word detected: '{model_name}' "
                        f"(confidence={confidence:.3f})"
                    )
                    return self.keyword
        except Exception as e:
            logger.error(f"Wake word processing error: {e}")
        return None

    def reset(self) -> None:
        """Reset the detector's internal prediction buffer."""
        if self._model is not None:
            try:
                self._model.reset()
            except Exception as e:
                logger.warning(f"Wake word reset error: {e}")
