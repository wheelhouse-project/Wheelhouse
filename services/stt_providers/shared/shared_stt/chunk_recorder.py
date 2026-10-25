"""Opt-in recording mode for AudioProcessor.process_chunk (wh-first-sound-lost.23).

A provider that wants to capture a short stretch of raw microphone audio, for
example one spoken word, attaches a ChunkRecorder to its AudioProcessor.
While no recording is requested the recorder is idle and process_chunk
behaves exactly as it does without one.

The recorder is a small state machine that the audio thread drives:

- Another thread calls request() or cancel(). Those two only set flags.
- The audio thread calls feed() once per chunk, after the VAD and the AGC
  have run. feed() starts a requested recording at the next chunk where no
  utterance is in progress, keeps the raw chunks, and decides when the
  capture ends.

Every time limit is counted in audio samples, never in wall time. A capture
that the machine delivers faster or slower than real time ends at the same
audio position.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# What feed() tells the audio thread to do with the chunk.
FEED_SKIP = "skip"          # not recording: process the chunk as usual
FEED_CONSUMED = "consumed"  # recording: the chunk was taken, do nothing more
FEED_FINISHED = "finished"  # the capture just ended: reset, then deliver()
FEED_ABORTED = "aborted"    # a cancel ended it: reset if needed, process as usual

# Why a capture ended.
END_SILENCE = "silence"      # end_silence_s after the last speech chunk
END_MAX = "max"              # max_s after the gate opened
END_NO_SPEECH = "no_speech"  # the gate never opened within no_speech_timeout_s


@dataclass(frozen=True)
class Recording:
    """The result of one capture.

    `samples` holds the raw int16 audio (the lead-in, then every chunk from
    the gate opening to the end), or None when the gate never opened.
    `waited_samples` is the audio that passed from the start of recording
    mode until the end, at the sample rate of the recorder.
    """

    samples: Optional[np.ndarray]
    end: str
    waited_samples: int


class ChunkRecorder:
    """Records raw chunks while the audio thread runs in recording mode."""

    def __init__(
        self,
        sample_rate: int,
        lead_in_ms: int,
        end_silence_s: float,
        max_s: float,
        no_speech_timeout_s: float,
        on_start: Optional[Callable[[], None]] = None,
        on_finish: Optional[Callable[[Recording], None]] = None,
    ):
        self.sample_rate = sample_rate
        self._lead_in_bytes = lead_in_ms * sample_rate // 1000 * 2
        self._end_silence_samples = int(end_silence_s * sample_rate)
        self._max_samples = int(max_s * sample_rate)
        self._no_speech_samples = int(no_speech_timeout_s * sample_rate)
        self._on_start = on_start
        self._on_finish = on_finish
        # The flags below are set by request() and cancel() on another thread
        # and read by feed() on the audio thread. The lock makes each change
        # of state one step; the audio thread holds it for a few statements
        # per chunk and never while it calls a callback.
        self._lock = threading.Lock()
        self._pending = False   # requested, not started yet
        self._active = False    # recording mode is on
        self._cancel = False    # a cancel arrived while active
        self._outcome: Optional[Recording] = None
        self._start_capture()

    def _start_capture(self) -> None:
        self._gate = False
        self._chunks: list[bytes] = []
        self._waited = 0
        self._since_open = 0
        self._trailing = 0

    # -- called from any thread -------------------------------------------

    def request(self) -> bool:
        """Ask for one recording. False when one is pending or running."""
        with self._lock:
            if self._pending or self._active:
                return False
            self._pending = True
            return True

    def cancel(self) -> None:
        """Drop a pending request, or end a running recording at its next chunk.

        A cancelled recording delivers no result.
        """
        with self._lock:
            self._pending = False
            if self._active:
                self._cancel = True

    @property
    def busy(self) -> bool:
        """True from request() until the capture ends or is cancelled."""
        return self._pending or self._active

    # -- called from the audio thread --------------------------------------

    @property
    def gate_opened(self) -> bool:
        """True when the last capture saw speech (valid after FEED_ABORTED)."""
        return self._gate

    def feed(self, pcm_bytes: bytes, is_speech: bool, gate_open: bool,
             lead_in_buffer) -> str:
        """Take one raw chunk, or tell the audio thread to process it as usual.

        `gate_open` is the processor's VAD gate: an utterance is in progress,
        so a pending recording waits. `lead_in_buffer` is the processor's
        LeadInBuffer of raw chunks.
        """
        if not (self._pending or self._active):
            return FEED_SKIP
        started = False
        with self._lock:
            if self._active and self._cancel:
                self._active = False
                self._cancel = False
                self._chunks = []
                return FEED_ABORTED
            if not self._active:
                if not self._pending or gate_open:
                    return FEED_SKIP
                self._pending = False
                self._active = True
                self._cancel = False
                self._start_capture()
                started = True
        if started and self._on_start is not None:
            try:
                self._on_start()
            except Exception:
                logger.exception("Recording start callback failed")
        with self._lock:
            return self._take(pcm_bytes, is_speech, lead_in_buffer)

    def end_now(self) -> str:
        """End a running capture now, for a caller that gets no more chunks.

        The audio thread calls this where its loop skips process_chunk
        because transcription is off, for example at a push-to-talk release.
        The end-silence timer counts only audio that arrives, so without
        this call a capture would stay frozen until the next hold.

        A capture whose gate opened ends as END_SILENCE (the check runs). A
        capture whose gate never opened ends as END_NO_SPEECH. A request that
        is only pending stays pending: the user may click Record first and
        hold the button after. A cancelled capture ends without a result.

        Returns FEED_FINISHED (a result waits for deliver()), FEED_ABORTED
        (cancelled; gate_opened says whether it took the lead-in buffer), or
        FEED_SKIP (nothing was running).
        """
        if not self._active:  # cheap: the audio loop calls this for every skipped chunk
            return FEED_SKIP
        with self._lock:
            if not self._active:
                return FEED_SKIP
            if self._cancel:
                self._active = False
                self._cancel = False
                self._chunks = []
                return FEED_ABORTED
            return self._end(END_SILENCE if self._gate else END_NO_SPEECH)

    def _take(self, pcm_bytes: bytes, is_speech: bool, lead_in_buffer) -> str:
        n = len(pcm_bytes) // 2
        self._waited += n
        if not self._gate:
            if not is_speech:
                lead_in_buffer.add(pcm_bytes)
                if self._waited >= self._no_speech_samples:
                    return self._end(END_NO_SPEECH)
                return FEED_CONSUMED
            lead = lead_in_buffer.get_lead_in()
            lead_in_buffer.clear()
            if len(lead) > self._lead_in_bytes:
                lead = lead[len(lead) - self._lead_in_bytes:]
            self._gate = True
            self._chunks = [lead, pcm_bytes] if lead else [pcm_bytes]
            self._since_open = n
            self._trailing = 0
        else:
            self._chunks.append(pcm_bytes)
            self._since_open += n
            self._trailing = 0 if is_speech else self._trailing + n
        if self._trailing >= self._end_silence_samples:
            return self._end(END_SILENCE)
        if self._since_open >= self._max_samples:
            return self._end(END_MAX)
        return FEED_CONSUMED

    def _end(self, end: str) -> str:
        samples = None
        if end != END_NO_SPEECH:
            samples = np.frombuffer(b"".join(self._chunks), dtype="<i2").copy()
        self._outcome = Recording(samples, end, self._waited)
        self._chunks = []
        self._active = False
        return FEED_FINISHED

    def deliver(self) -> None:
        """Hand the finished capture to on_finish.

        The audio thread calls this after FEED_FINISHED and after it reset the
        utterance state, so the callback (which may decode) runs between
        utterances. An exception in the callback is logged and never stops
        the audio loop.
        """
        outcome, self._outcome = self._outcome, None
        if outcome is None or self._on_finish is None:
            return
        try:
            self._on_finish(outcome)
        except Exception:
            logger.exception("Recording finish callback failed")
