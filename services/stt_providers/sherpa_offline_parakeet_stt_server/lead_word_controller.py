"""The provider side of the 'Your training word' window (wh-first-sound-lost.23).

LeadWordController answers the seven lead_word_* commands of the contract with
lead_word_result messages. It does no signal work and no file work itself:
lead_word_recorder cuts and checks the capture, lead_word_store keeps the
files and the config key, and shared_stt.chunk_recorder.ChunkRecorder keeps
the raw chunks while the audio thread is in recording mode.

Threads:

- The forwarder calls handle_command on its own thread, one command at a
  time. Keep, restore, off, on and status run there.
- lead_word_record only starts a job and asks the recorder for a take. The
  audio thread starts recording mode at the next chunk (on_start), and at the
  end of the capture it runs the cut, the check with its decode, and the
  candidate write (on_finish). The decode blocks the audio loop while it
  runs, because the audio thread is the only thread that may use the
  recognizer between utterances.
- One lock guards the job (self._job). It is held for a few statements and
  for the one small candidate write, never across the check or the decode,
  and status() and capabilities() never take it.
- lead_word_discard clears the job. A check that finishes after that sees a
  job that is no longer its own and drops its result: no candidate on disk and
  no passed or refused message.
"""
from __future__ import annotations

import logging
import math
import threading

import numpy as np

import lead_word_recorder
from lead_word_store import LeadWordStoreError
from shared_stt.chunk_recorder import ChunkRecorder, Recording

logger = logging.getLogger(__name__)

BUSY_REASON = "A recording is already in progress"


class _Job:
    """One lead_word_record request, from the command to its last message."""

    __slots__ = ("request_id",)

    def __init__(self, request_id: str):
        self.request_id = request_id


def _plain(value):
    """A JSON-plain copy of one measure (no numpy scalar types)."""
    if isinstance(value, np.generic):
        return value.item()
    return value


def _advice_limit(value) -> float:
    """The noise advice limit in dB, or NOISE_ADVICE_DB for a bad value."""
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)):
        return float(value)
    logger.warning(
        f"lead_word_noise_advice_db = {value!r} is not a number; using "
        f"{lead_word_recorder.NOISE_ADVICE_DB}")
    return lead_word_recorder.NOISE_ADVICE_DB


class LeadWordController:
    """Runs the training-word commands for the Parakeet provider."""

    def __init__(
        self,
        engine,
        forwarder,
        store,
        silero_threshold: float,
        get_microphone,
        vad_factory=None,
        sample_rate: int = lead_word_recorder.SAMPLE_RATE,
        vad_lead_in_ms: int = lead_word_recorder.LEAD_IN_MS,
        noise_advice_db=lead_word_recorder.NOISE_ADVICE_DB,
    ):
        """
        Args:
            engine: has set_lead_word(audio_or_None), has_lead_word() and
                _decode_plain(float32 audio) (SherpaOfflineEngine).
            forwarder: has send_lead_word_result(...) (WSForwarder).
            store: a LeadWordStore.
            silero_threshold: the [client] silero_threshold value.
            get_microphone: callable that returns the microphone name now.
            vad_factory: callable(threshold) that returns a new Silero VAD for
                one check. Defaults to lead_word_recorder.new_silero_vad.
            sample_rate: the rate of the audio processor. The cut and the WAV
                writer assume 16 kHz, so any other rate raises ValueError and
                the provider runs without the feature.
            vad_lead_in_ms: the lead-in time of the processor's LeadInBuffer,
                the most raw lead-in the recorder can see.
            noise_advice_db: the [engine] lead_word_noise_advice_db value. A
                passed recording whose snr_db is under it carries
                noise_advice=True. A value that is not a finite number logs
                a warning and gives NOISE_ADVICE_DB.

        Raises:
            ValueError: when sample_rate is not 16000.
        """
        if sample_rate != lead_word_recorder.SAMPLE_RATE:
            raise ValueError(
                f"the training word needs {lead_word_recorder.SAMPLE_RATE} Hz "
                f"audio, not {sample_rate} Hz")
        self.engine = engine
        self.forwarder = forwarder
        self.store = store
        self._silero_threshold = silero_threshold
        self.noise_advice_db = _advice_limit(noise_advice_db)
        self._get_microphone = get_microphone
        self._vad_factory = vad_factory or lead_word_recorder.new_silero_vad
        self._lock = threading.Lock()
        self._job: _Job | None = None
        # The job whose take is being captured, set at its first chunk.
        self._capture_job: _Job | None = None
        self.recorder = ChunkRecorder(
            sample_rate=sample_rate,
            lead_in_ms=min(lead_word_recorder.LEAD_IN_MS, vad_lead_in_ms),
            end_silence_s=lead_word_recorder.CAPTURE_END_SILENCE_S,
            max_s=lead_word_recorder.CAPTURE_MAX_S,
            no_speech_timeout_s=lead_word_recorder.NO_SPEECH_TIMEOUT_S,
            on_start=self._on_start,
            on_finish=self._on_finish,
        )

    # -- sending ------------------------------------------------------------

    def _send(self, request_id: str, stage: str, **fields) -> None:
        """Send one lead_word_result. A send that fails never stops the caller.

        The caller can be the audio thread, which must keep reading chunks.
        """
        try:
            self.forwarder.send_lead_word_result(request_id, stage, **fields)
        except Exception:
            logger.exception(f"Cannot send the training-word result '{stage}'")

    def _send_state(self, request_id: str, stage: str, state) -> None:
        """Send a stage that carries the state fields (status, kept, ...).

        `state` is the StoreStatus of the store. current_exists says that
        hello.wav exists, which recorded alone cannot say: a restored earlier
        file has an empty recorded date.
        """
        fields = dict(
            loaded=self.engine.has_lead_word(),
            recorded=state.recorded,
            microphone=state.microphone,
            previous_exists=state.previous_exists,
            current_exists=state.current_exists,
        )
        if stage == "status":
            fields["key_path"] = state.key_path
        self._send(request_id, stage, **fields)

    def capabilities(self) -> dict:
        """The lead_word field of the capabilities frame. Never raises.

        It takes no lock, so a check or a Keep in progress cannot hold it up.
        """
        try:
            return {
                "supported": True,
                "loaded": bool(self.engine.has_lead_word()),
                "recorded": self.store.status().recorded,
            }
        except Exception:
            logger.exception("Cannot read the training-word state")
            return {"supported": True, "loaded": False, "recorded": ""}

    # -- commands (forwarder thread) ------------------------------------------

    def handle_command(self, command: str, request_id: str) -> None:
        """Run one lead_word_* command and send its result.

        An error in the command is logged and answered with a 'failed' stage,
        so the window never waits for an answer that will not come.
        """
        try:
            self._dispatch(command, request_id)
        except Exception as e:
            logger.exception(f"Training-word command {command} failed")
            self._send(request_id, "failed", reason=f"Training word: {e}")

    def _dispatch(self, command: str, request_id: str) -> None:
        if command == "lead_word_status":
            self._send_state(request_id, "status", self.store.status())
        elif command == "lead_word_record":
            self._record(request_id)
        elif command == "lead_word_discard":
            self._discard(request_id)
        elif command in _STORE_COMMANDS:
            self._store_command(command, request_id)
        else:
            self._send(
                request_id, "failed",
                reason=f"Unknown training-word command: {command}")

    def _record(self, request_id: str) -> None:
        with self._lock:
            if self._job is not None:
                busy = True
            else:
                busy = False
                self._job = _Job(request_id)
                job = self._job
        if busy:
            self._send(request_id, "failed", reason=BUSY_REASON)
            return
        try:
            # An older candidate is dropped before the new take; hello.wav is
            # never touched.
            self.store.discard_candidate()
            if not self.recorder.request():
                raise RuntimeError(BUSY_REASON)
        except Exception as e:
            with self._lock:
                if self._job is job:
                    self._job = None
            logger.exception("Cannot start the training-word recording")
            self._send(request_id, "failed", reason=str(e))

    def _discard(self, request_id: str) -> None:
        # The job is cleared and the candidate deleted under one hold of the
        # lock, so a check that is writing its candidate at this moment
        # either finishes first (and the delete below removes it) or finds
        # no job and writes nothing.
        with self._lock:
            self._job = None
            self.store.discard_candidate()
        self.recorder.cancel()
        self._send_state(request_id, "status", self.store.status())

    def _store_command(self, command: str, request_id: str) -> None:
        if self._job is not None:
            self._send(request_id, "failed", reason=BUSY_REASON)
            return
        action, stage = _STORE_COMMANDS[command]
        try:
            # crewcut: these file actions run on the forwarder's event-loop
            # thread, so the loop waits for them (a few small files and one
            # config edit). Move them to a worker thread if they ever grow.
            state = getattr(self.store, action)(self.engine.set_lead_word)
        except LeadWordStoreError as e:
            self._send(request_id, "failed", reason=str(e))
            return
        self._send_state(request_id, stage, state)

    # -- recording (audio thread) ---------------------------------------------

    def _on_start(self) -> None:
        """Recording mode just started at this chunk."""
        with self._lock:
            job = self._job
            self._capture_job = job
        if job is None:
            # A discard slipped in between the request and the first chunk.
            self.recorder.cancel()
            return
        self._send(job.request_id, "listening")

    def _on_finish(self, recording: Recording) -> None:
        """The capture ended. Check it, then send passed or refused.

        Runs on the audio thread between utterances. Any error here is
        answered with 'failed', because ChunkRecorder.deliver only logs it.
        """
        with self._lock:
            job = self._capture_job
            self._capture_job = None
            if self._job is not job:
                # Discarded while recording. A new record can already be
                # pending; this take must not answer it.
                job = None
        if job is None:
            return
        try:
            self._check_and_report(job, recording)
        except Exception as e:
            logger.exception("The training-word check failed")
            with self._lock:
                current = self._job is job
                if current:
                    self._job = None
            if current:
                self._send(
                    job.request_id, "failed",
                    reason=f"The check could not finish: {e}")

    def _check_and_report(self, job: _Job, recording: Recording) -> None:
        self._send(job.request_id, "checking")
        if recording.samples is None:
            waited_ms = recording.waited_samples * 1000 // self.recorder.sample_rate
            result = lead_word_recorder.CheckResult(
                lead_word_recorder.ROW_NO_SPEECH,
                lead_word_recorder.REASONS[lead_word_recorder.ROW_NO_SPEECH],
                {"capture_ms": waited_ms, "regions": 0},
                None,
            )
        else:
            result = lead_word_recorder.run_check(
                recording.samples,
                self._vad_factory(self._silero_threshold),
                lambda cut: lead_word_recorder.decode_cut(self.engine, cut),
            )
        logger.info(lead_word_recorder.format_log_line(result))
        measures = {k: _plain(v) for k, v in result.measures.items()}
        # Read before the job ends, so an error here still answers 'failed'.
        key_path = self.store.status().key_path if result.passed else ""

        # crewcut: the lock is held across one small file write (the
        # candidate WAV and its measures file) so that a discard cannot slip
        # between the job check and the write. Write to a temp name and
        # rename after the check if this ever needs to be shorter.
        with self._lock:
            if self._job is not job:
                return  # discarded during the check: no file, no message
            if result.passed:
                self.store.save_candidate(
                    result.cut, measures, self._get_microphone())
            else:
                self.store.delete_refused()
            self._job = None
        if result.passed:
            self._send(
                job.request_id, "passed", measures=measures,
                key_path=key_path,
                noise_advice=lead_word_recorder.noise_advice(
                    measures, self.noise_advice_db))
        else:
            self._send(
                job.request_id, "refused", reason=result.reason,
                measures=measures)


# command -> (store method, stage of the answer)
_STORE_COMMANDS = {
    "lead_word_keep": ("keep", "kept"),
    "lead_word_restore": ("restore", "restored"),
    "lead_word_off": ("turn_off", "off"),
    "lead_word_on": ("turn_on", "on"),
}
