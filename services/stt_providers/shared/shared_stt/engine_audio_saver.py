"""Opt-in saver for the audio that a speech engine receives (wh-first-sound-lost.1).

Purpose: evidence for wh-first-sound-lost. The first sound of an utterance is
sometimes missing from the transcript. A saved WAV file shows whether the
sound was already missing when the engine received the audio, or whether the
engine lost it.

Privacy cost: with the feature on, the provider saves everything the user
says as sound files in one folder, until each file is 2 days old. The files
stay on this computer. The feature is off unless the environment variable
WHEELHOUSE_SAVE_ENGINE_AUDIO is exactly "1". Wheelhouse sets that variable
from the SAVE_ENGINE_AUDIO key of its config.toml each time it starts a
provider.

What a file holds: exactly the samples that the engine received for the decode
that produced the final transcript, in the same order, after every Wheelhouse
processing step. The file is mono, 16-bit PCM, at the engine sample rate.

How it works: ``AudioProcessor`` calls ``EngineAudioSaver.submit`` after it
sent the final transcript. ``submit`` copies the samples and returns at once.
One daemon thread writes the files, and deletes this feature's files that are
older than 2 days: once when the thread starts, and again every hour.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

ENV_VAR = "WHEELHOUSE_SAVE_ENGINE_AUDIO"
LOGGER_NAME = "shared_stt.audio_processor.engine_audio"
MAX_AGE_SECONDS = 2 * 24 * 3600
CLEANUP_INTERVAL_SECONDS = 3600

_FILE_PREFIX = "engine-audio_"
_SAVER_FILE_RE = re.compile(
    r"engine-audio_\d{8}-\d{6}-\d{3}_[a-z0-9_]+_utt\d+\.wav\Z", re.ASCII
)

logger = logging.getLogger(LOGGER_NAME)

_STOP = object()


def default_folder() -> Path:
    """Return the folder for the files. Creates nothing."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise KeyError("LOCALAPPDATA")
    return Path(base) / "WheelHouse" / "engine-audio"


def build_file_name(when: datetime, provider: str, utterance_id: int) -> str:
    """Return the file name: local date and time to the millisecond, provider, utterance."""
    stamp = when.strftime("%Y%m%d-%H%M%S")
    millis = when.microsecond // 1000
    return f"{_FILE_PREFIX}{stamp}-{millis:03d}_{provider}_utt{utterance_id}.wav"


def is_saver_file(name: str) -> bool:
    """True only when *name* is a file name that ``build_file_name`` can build."""
    return _SAVER_FILE_RE.match(name) is not None


def delete_old_files(folder: Path, now: float) -> int:
    """Delete this feature's files in *folder* that are older than MAX_AGE_SECONDS.

    Only regular files directly in *folder* whose name passes
    ``is_saver_file`` can be deleted. It never enters a subfolder. A missing
    folder gives 0. It never raises. Returns the number of files deleted.
    """
    deleted = 0
    try:
        entries = list(os.scandir(folder))
    except Exception:
        return 0
    for entry in entries:
        try:
            if not is_saver_file(entry.name):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime <= MAX_AGE_SECONDS:
                continue
            os.remove(entry.path)
            deleted += 1
        except Exception:
            continue
    return deleted


def samples_to_pcm16(audio) -> bytes:
    """Convert float samples (-1.0 to 1.0) to little-endian 16-bit PCM bytes."""
    samples = np.asarray(audio, dtype=np.float32).ravel().astype(np.float64)
    pcm = np.clip(np.rint(samples * 32768.0), -32768, 32767)
    return pcm.astype("<i2").tobytes()


class EngineAudioSaver:
    """Write the audio of each utterance to a WAV file on a background thread."""

    def __init__(
        self,
        provider: str,
        folder,
        *,
        sample_rate: int = 16000,
        clock: Callable[[], float] = time.time,
        local_now: Callable[[], datetime] = datetime.now,
        cleanup_interval: float = CLEANUP_INTERVAL_SECONDS,
        queue_size: int = 16,
    ) -> None:
        self.provider = provider
        self.folder = Path(folder)
        self.sample_rate = sample_rate
        self._clock = clock
        self._local_now = local_now
        self._cleanup_interval = cleanup_interval
        # crewcut: a full queue skips the file. A decode is rare, so the queue
        # fills only when the disk stalls. Raise queue_size to remove the limit.
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._warned = False
        self._warn_lock = threading.Lock()

    # -- warnings ---------------------------------------------------------

    def _warn_once(self, message: str, *args) -> None:
        """Log one WARNING for this saver object. Later failures log nothing."""
        with self._warn_lock:
            if self._warned:
                return
            self._warned = True
        try:
            logger.warning(message, *args)
        except Exception:
            pass

    # -- public -----------------------------------------------------------

    def start(self) -> None:
        """Start the background thread. It deletes old files at once."""
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._run, name=f"engine-audio-saver-{self.provider}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def submit(self, audio, utterance_id: int, final_sent: bool) -> None:
        """Queue one utterance for saving. Returns at once and never raises."""
        try:
            samples = np.array(audio, dtype=np.float32, copy=True)
            if samples.ndim != 1:
                raise ValueError("audio must be a one-dimensional array of samples")
            when = self._local_now()
            self._queue.put_nowait((samples, when, utterance_id, bool(final_sent)))
        except queue.Full:
            self._warn_once(
                "Cannot save engine audio for UTT-%s: the save queue is full. "
                "Later failures are not logged until the provider restarts.",
                utterance_id,
            )
        except Exception as exc:
            self._warn_once(
                "Cannot save engine audio for UTT-%s: %s: %s. "
                "Later failures are not logged until the provider restarts.",
                utterance_id, type(exc).__name__, exc,
            )

    def stop(self, timeout: float = 2.0) -> None:
        """Write what is queued, then stop the thread."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        deadline = time.monotonic() + timeout
        try:
            self._queue.put(_STOP, timeout=max(timeout, 0.0))
        except Exception:
            return
        thread.join(max(deadline - time.monotonic(), 0.0))

    # -- thread -----------------------------------------------------------

    def _cleanup(self) -> None:
        try:
            delete_old_files(self.folder, self._clock())
        except Exception as exc:
            self._warn_once(
                "Cannot delete old engine audio files in %s: %s: %s. "
                "Later failures are not logged until the provider restarts.",
                self.folder, type(exc).__name__, exc,
            )

    def _run(self) -> None:
        self._cleanup()
        next_cleanup = time.monotonic() + self._cleanup_interval
        while True:
            wait = max(next_cleanup - time.monotonic(), 0.0)
            try:
                item = self._queue.get(timeout=wait)
            except queue.Empty:
                self._cleanup()
                next_cleanup = time.monotonic() + self._cleanup_interval
                continue
            if item is _STOP:
                return
            try:
                self._write(*item)
            except Exception as exc:
                self._warn_once(
                    "Cannot save engine audio for UTT-%s: %s: %s. "
                    "Later failures are not logged until the provider restarts.",
                    item[2], type(exc).__name__, exc,
                )
            if time.monotonic() >= next_cleanup:
                self._cleanup()
                next_cleanup = time.monotonic() + self._cleanup_interval

    def _write(
        self, samples: np.ndarray, when: datetime, utterance_id: int,
        final_sent: bool,
    ) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / build_file_name(when, self.provider, utterance_id)
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(samples_to_pcm16(samples))
        except Exception:
            try:
                path.unlink()
            except Exception:
                pass
            raise
        if final_sent:
            logger.info("Saved engine audio for UTT-%s: %s", utterance_id, path)
        else:
            logger.info(
                "Saved engine audio for UTT-%s (no final transcript was sent): %s",
                utterance_id, path,
            )


class _OffCleanup:
    """Delete this feature's old files while the feature is off.

    A user who turns the feature off keeps no saver file past MAX_AGE_SECONDS.
    ``run_once`` deletes at start. When a file of this feature remains, one
    daemon thread repeats the pass every *interval* seconds and ends when no
    such file remains. It logs at most one warning for the object.
    """

    def __init__(self, provider: str, folder: Path, interval: float,
                 clock: Callable[[], float]) -> None:
        self.provider = provider
        self.folder = folder
        self.interval = interval
        self.clock = clock
        self._warned = False
        self._warn_lock = threading.Lock()

    def _warn_once(self, exc: BaseException) -> None:
        with self._warn_lock:
            if self._warned:
                return
            self._warned = True
        try:
            logger.warning(
                "Cannot delete old engine audio files in %s: %s: %s. "
                "Later failures are not logged until the provider restarts.",
                self.folder, type(exc).__name__, exc,
            )
        except Exception:
            pass

    def _pass(self) -> bool:
        """Delete old files once. True when a file of this feature remains."""
        try:
            delete_old_files(self.folder, self.clock())
        except Exception as exc:
            self._warn_once(exc)
        try:
            with os.scandir(self.folder) as entries:
                return any(is_saver_file(entry.name) for entry in entries)
        except Exception:
            return False

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                if not self._pass():
                    return
            except Exception as exc:
                self._warn_once(exc)
                return

    def run_once(self) -> None:
        """Run the start pass. Never raises. Starts the thread when needed."""
        try:
            if not self._pass():
                return
            threading.Thread(
                target=self._loop,
                name=f"engine-audio-cleanup-{self.provider}",
                daemon=True,
            ).start()
        except Exception as exc:
            self._warn_once(exc)


def create_from_environment(
    provider: str,
    sample_rate: int = 16000,
    *,
    cleanup_interval: float = CLEANUP_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.time,
):
    """Return a started saver when the environment turns the feature on, else None.

    The feature is on only when ``WHEELHOUSE_SAVE_ENGINE_AUDIO`` is exactly
    "1". When it is off, this writes no file and creates no folder. It still
    deletes this feature's files that are older than 2 days, and keeps doing
    so every *cleanup_interval* seconds while such a file remains. Without
    this, a user who turns the feature off would keep the files that were
    younger than 2 days at that moment until the feature is on again.
    """
    if os.environ.get(ENV_VAR) != "1":
        try:
            folder = default_folder()
        except KeyError:
            return None
        _OffCleanup(provider, folder, cleanup_interval, clock).run_once()
        return None
    try:
        folder = default_folder()
    except KeyError:
        logger.warning(
            "Cannot save engine audio: the LOCALAPPDATA environment variable "
            "is not set."
        )
        return None
    saver = EngineAudioSaver(provider, folder, sample_rate=sample_rate)
    saver.start()
    return saver
