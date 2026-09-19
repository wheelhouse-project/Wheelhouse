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
import time
from threading import Event
from pathlib import Path
from typing import Optional

import numpy as np

from shared_stt.pre_engine_metrics import PreEngineWork

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

# crewcut: urlopen applies _DOWNLOAD_TIMEOUT_S to each socket operation, not
# to the whole download, so a server that sends a byte every few seconds can
# still take longer; a deadline checked between chunks would bound the total.
_DOWNLOAD_TIMEOUT_S = 15
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
# Every ONNX file is a serialized ModelProto whose first field is ir_version
# (field 1, varint), so its first byte is 0x08. An HTML page is refused.
_ONNX_HEADER = b"\x08"

# Which transcription-disable reasons arm the detector, per wake-word mode.
# WheelHouse names the reason in its set_transcription_status command, and
# the provider's audio loop feeds frames to the detector only while the
# detector is armed.
WAKE_WORD_ARMED_REASONS: dict[str, tuple[str, ...]] = {
    "idle_recovery": ("idle",),
    "push_to_talk": ("idle", "audio", "sonos"),
}


def should_listen_for_wake_word(mode: str, reason: Optional[str]) -> bool:
    """Whether a transcription-disable reason arms the wake word in this mode.

    The three provider mains each carried their own copy of this rule; they
    all call this one function instead (wh-audio-suppression-control C3).

    "audio" armed idle_recovery for one release, so the user could say the
    command that switched audio suppression off while the sound that caused
    the pause kept playing. wh-audio-suppression-auto removed that command
    and the recovery window it was spoken into, so the reason arms nothing
    in this mode any more: a sound pause now ends when the sound stops.

    "push_to_talk" still arms on every reason. Its hold mutes the speakers,
    so the wake word there is not about the sound pause at all.

    Args:
        mode: the [wake_word] mode value, "idle_recovery" or "push_to_talk".
        reason: the reason WheelHouse gave for disabling transcription, or
            None when transcription was re-enabled rather than paused.

    Returns:
        True only when this mode arms the detector for that reason. A mode
        no rule names, and a reason of None, both return False.
    """
    if reason is None:
        return False
    return reason in WAKE_WORD_ARMED_REASONS.get(mode, ())


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
        diagnostic_logger: Optional[logging.Logger] = None,
        log_load_diagnostics: bool = False,
    ):
        self.keyword = keyword
        self.model_dir = Path(model_dir)
        self.sensitivity = sensitivity
        self.is_loaded = False
        self._model = None
        self._downloaded_path: Optional[Path] = None
        self._work = PreEngineWork()
        self._work_started: Optional[float] = None
        # Off by default: the ten-second interval line follows the
        # provider's [debug] flag (wh-audit13-preengine-load-review.1).
        self._log_load_diagnostics = log_load_diagnostics
        self._work_reset_requested = Event()
        self._diagnostic_logger = diagnostic_logger or logging.getLogger(
            'shared_stt.audio_processor.loadmetrics')

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
            # Write to a .part file and rename it only when the body is whole,
            # so a kill during the download never leaves a partial model under
            # the name _resolve_model_path looks for.
            partial = target.with_name(target.name + ".part")
            try:
                import os
                import shutil
                import urllib.request
                logger.info(f"Downloading wake word model: {url}")
                with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as response, \
                        open(partial, "wb") as out:
                    first = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not first.startswith(_ONNX_HEADER):
                        raise ValueError("the response is not an ONNX model")
                    out.write(first)
                    shutil.copyfileobj(response, out, _DOWNLOAD_CHUNK_BYTES)
                    # http.client ends a read without an error when the server
                    # closes early; the bytes still owed under Content-Length
                    # show it. A chunked body raises IncompleteRead instead.
                    if getattr(response, "length", None):
                        raise ValueError("the download ended before the whole body arrived")
                os.replace(partial, target)
                logger.info(
                    f"Wake word model downloaded: {target} "
                    f"({target.stat().st_size} bytes)"
                )
                self._downloaded_path = target
                return target
            except Exception as e:
                logger.warning(f"Failed to download {url}: {e}")
                partial.unlink(missing_ok=True)
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
            # Remove a file this start downloaded, or one that is not ONNX at
            # all, so the next start downloads again. Keep any other file: the
            # failure can come from the resources step, and deleting the
            # shipped model would need the network to get it back.
            try:
                with open(model_path, "rb") as f:
                    is_onnx = f.read(len(_ONNX_HEADER)) == _ONNX_HEADER
                if model_path == self._downloaded_path or not is_onnx:
                    model_path.unlink(missing_ok=True)
                    logger.warning(f"Removed wake word model {model_path}")
            except OSError as unlink_error:
                logger.warning(f"Could not remove {model_path}: {unlink_error}")

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
        # Provider reset callbacks run on the WebSocket control thread.
        # Keep timing counters on this consumer; never hold a lock around
        # inference or log from the reset callback.
        if self._work_reset_requested.is_set():
            self._work_reset_requested.clear()
            self._flush_work('reset')
        detected = False
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16)
            started = time.monotonic()
            if self._work_started is None:
                self._work_started = started
            try:
                predictions = self._model.predict(audio)
            finally:
                self._work.add(PreEngineWork(
                    len(pcm_bytes), wake_word_s=time.monotonic() - started))
            for model_name, confidence in predictions.items():
                if confidence >= self.sensitivity:
                    detected = True
                    logger.info(
                        f"Wake word detected: '{model_name}' "
                        f"(confidence={confidence:.3f})"
                    )
                    return self.keyword
        except Exception as e:
            logger.error(f"Wake word processing error: {e}")
        finally:
            if detected:
                self._flush_work('detected')
            elif (self._work_started is not None
                  and time.monotonic() - self._work_started >= 10.0):
                self._flush_work('interval', log=self._log_load_diagnostics)
        return None

    def _flush_work(self, end: str, log: bool = True) -> None:
        # The window resets either way, so a detection or reset line never
        # reports more than ten seconds of wake-word work
        # (wh-audit13-preengine-load-review.1).
        if log:
            self._work.log(self._diagnostic_logger, 'wake_word', end, 16000)
        self._work = PreEngineWork()
        self._work_started = None

    def reset(self) -> None:
        """Reset the detector's internal prediction buffer."""
        self._work_reset_requested.set()
        if self._model is not None:
            try:
                self._model.reset()
            except Exception as e:
                logger.warning(f"Wake word reset error: {e}")
