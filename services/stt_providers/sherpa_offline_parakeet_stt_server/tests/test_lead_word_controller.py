"""The provider side of the training-word window (wh-first-sound-lost.23).

LeadWordController answers the seven lead_word_* commands with lead_word_result
messages. It owns the recording job (record -> listening -> checking ->
passed or refused), and it calls the store for Keep, go back, turn off and
turn on. The tests use the real store on tmp_path, the real ChunkRecorder
inside a real AudioProcessor (so the message order is the one the audio
thread produces), a fake engine, a fake forwarder, and a VAD that decides by
energy so the synthetic word has exactly one region.

Acceptance items covered: 1 (the controller builds only at 16 kHz), 7 (the
check decode gets the cut alone), 8, 11, 12, 16, 17, 18, 26 (provider half),
and the capabilities field of the contract.
"""
from __future__ import annotations

import json
import logging
import threading
import tomllib
from unittest.mock import Mock

import numpy as np
import pytest

import lead_word_recorder as rec
import lead_word_store as store_mod
from lead_word_controller import LeadWordController, _Job
from lead_word_store import LeadWordStore
from shared_stt.audio_processor import AudioProcessor

RATE = 16000
CH = 512  # samples per chunk: one Silero chunk

CONFIG = (
    "# Provider settings -- keep this comment\n"
    "[engine]\n"
    "lead_word_path = ''  # set by the window\n"
    "\n"
    "[client]\n"
    "silero_threshold = 0.5\n"
)

BUSY = "A recording is already in progress"


class EnergyVad:
    """Speech when a chunk holds a sample above 1000. Stateless."""

    def is_speech(self, pcm):
        return int(np.abs(np.frombuffer(pcm, dtype="<i2")).max()) > 1000

    def reset(self):
        pass


class Voice:
    """Synthetic microphone chunks: low noise, and a 200 Hz tone for speech."""

    def __init__(self, noise_amp: int = 3):
        self.rng = np.random.default_rng(3)
        self.phase = 0
        self.noise_amp = noise_amp

    def silence(self) -> bytes:
        amp = self.noise_amp
        return self.rng.integers(-amp, amp + 1, CH).astype("<i2").tobytes()

    def speech(self) -> bytes:
        t = np.arange(self.phase, self.phase + CH)
        self.phase += CH
        return (8000 * np.sin(2 * np.pi * 200 * t / RATE)).astype("<i2").tobytes()


class FakeForwarder:
    """Records every lead_word_result the controller sends."""

    def __init__(self):
        self.sent = []
        self.microphone_device = "Test microphone"

    def send_lead_word_result(
        self, request_id, stage, reason="", measures=None, key_path="",
        loaded=False, recorded="", microphone="", previous_exists=False,
        current_exists=False, noise_advice=None,
    ):
        message = {
            "request_id": request_id, "stage": stage, "reason": reason,
            "measures": dict(measures or {}), "key_path": key_path,
            "loaded": loaded, "recorded": recorded, "microphone": microphone,
            "previous_exists": previous_exists,
            "current_exists": current_exists,
        }
        # Only a call that passes noise_advice records it.
        if noise_advice is not None:
            message["noise_advice"] = noise_advice
        self.sent.append(message)

    @property
    def stages(self):
        return [m["stage"] for m in self.sent]


class FakeEngine:
    """Holds the lead word the store gives it; decodes with a canned text."""

    def __init__(self):
        self.lead = None
        self.set_calls = []
        self.decode_inputs = []
        self.decode_text = "Hello"
        self.on_decode = None

    def set_lead_word(self, audio):
        self.set_calls.append(audio)
        self.lead = audio

    def has_lead_word(self):
        return self.lead is not None

    def _decode_plain(self, audio):
        self.decode_inputs.append(audio)
        if self.on_decode is not None:
            self.on_decode()
        return self.decode_text


class Rig:
    def __init__(self, tmp_path, silero_threshold=0.5, **controller_kwargs):
        config = tmp_path / "config.toml"
        config.write_bytes(CONFIG.encode("utf-8"))
        self.config = config
        self.folder = tmp_path / "lead-word"
        self.store = LeadWordStore(self.folder, config)
        self.engine = FakeEngine()
        self.forwarder = FakeForwarder()
        self.vad_thresholds = []

        def vad_factory(threshold):
            self.vad_thresholds.append(threshold)
            return EnergyVad()

        self.controller = LeadWordController(
            engine=self.engine, forwarder=self.forwarder, store=self.store,
            silero_threshold=silero_threshold,
            get_microphone=lambda: self.forwarder.microphone_device,
            vad_factory=vad_factory,
            **controller_kwargs,
        )
        self.engine_for_processor = Mock()
        self.engine_for_processor.get_result = Mock(return_value="")
        self.engine_for_processor.is_endpoint = Mock(return_value=False)
        self.engine_for_processor.is_ready = Mock(return_value=True)
        self.engine_for_processor.last_result = ""
        self.processor_forwarder = Mock()
        self.processor = AudioProcessor(
            engine=self.engine_for_processor, forwarder=self.processor_forwarder,
            sample_rate=RATE, vad_lead_in_ms=300,
            chunk_recorder=self.controller.recorder)
        self.processor.vad = EnergyVad()
        self.voice = Voice()

    def command(self, name, request_id="r1"):
        self.controller.handle_command(name, request_id)

    def feed(self, silence_before=0, speech=0, silence_after=0):
        for _ in range(silence_before):
            self.processor.process_chunk(self.voice.silence())
        for _ in range(speech):
            self.processor.process_chunk(self.voice.speech())
        for _ in range(silence_after):
            self.processor.process_chunk(self.voice.silence())

    def say_hello(self):
        """A clean word: 20 quiet chunks, 13 speech chunks (416 ms), 1 s quiet."""
        self.feed(silence_before=20, speech=13, silence_after=33)

    def record_and_pass(self, request_id="rec"):
        self.command("lead_word_record", request_id)
        self.say_hello()
        assert self.forwarder.stages[-1] == "passed", self.forwarder.sent
        return self.forwarder.sent[-1]

    def last(self):
        return self.forwarder.sent[-1]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def folder_names(rig):
    return sorted(p.name for p in rig.folder.iterdir()) if rig.folder.exists() else []


def spy_run_check(monkeypatch):
    """Wrap run_check. The returned dict gets the samples it was given and
    the result it returned."""
    seen = {}
    real = rec.run_check

    def spy(samples, vad, decode):
        seen["samples"] = samples
        result = real(samples, vad, decode)
        seen["result"] = result
        return result

    monkeypatch.setattr(rec, "run_check", spy)
    return seen


def errors_logged(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# status and capabilities
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_with_no_recording(self, rig):
        rig.command("lead_word_status", "s1")
        assert rig.forwarder.sent == [{
            "request_id": "s1", "stage": "status", "reason": "", "measures": {},
            "key_path": "", "loaded": False, "recorded": "", "microphone": "",
            "previous_exists": False, "current_exists": False,
        }]

    def test_status_reads_the_key_from_config_at_send_time(self, rig):
        rig.config.write_bytes(
            CONFIG.replace("lead_word_path = ''", "lead_word_path = 'D:/x/y.wav'")
            .encode("utf-8"))
        rig.command("lead_word_status")
        assert rig.last()["key_path"] == "D:/x/y.wav"

    def test_status_after_a_keep_reports_the_kept_recording(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep", "k1")
        rig.command("lead_word_status", "s2")
        message = rig.last()
        assert message["loaded"] is True
        assert message["recorded"] != ""
        assert message["microphone"] == "Test microphone"
        assert message["recorded"] == rig.store.status().recorded
        assert message["key_path"] == str(rig.store.wav_path)
        assert message["previous_exists"] is False
        assert message["current_exists"] is True


class TestCapabilities:
    def test_supported_loaded_recorded(self, rig):
        assert rig.controller.capabilities() == {
            "supported": True, "loaded": False, "recorded": ""}
        rig.record_and_pass()
        rig.command("lead_word_keep")
        capabilities = rig.controller.capabilities()
        assert capabilities["supported"] is True
        assert capabilities["loaded"] is True
        assert capabilities["recorded"] == rig.store.status().recorded != ""

    def test_never_raises(self, rig, monkeypatch):
        def boom():
            raise RuntimeError("disk gone")

        monkeypatch.setattr(rig.store, "status", boom)
        rig.engine.lead = np.zeros(1000, dtype=np.float32)
        # The answer after an error says what is known for certain: the
        # feature exists, and nothing is loaded or recorded.
        assert rig.controller.capabilities() == {
            "supported": True, "loaded": False, "recorded": ""}

    def test_a_blocked_check_does_not_block_capabilities(self, rig):
        """A check in progress holds no lock that capabilities() needs."""
        seen = {}

        def other_thread():
            seen["capabilities"] = rig.controller.capabilities()
            rig.command("lead_word_status", "during")

        def during_decode():
            # The forwarder thread answers while the audio thread decodes.
            worker = threading.Thread(target=other_thread, daemon=True)
            worker.start()
            worker.join(5)
            seen["finished"] = not worker.is_alive()

        rig.engine.on_decode = during_decode
        rig.record_and_pass()
        assert seen["finished"] is True
        assert seen["capabilities"]["supported"] is True
        assert [m["stage"] for m in rig.forwarder.sent
                if m["request_id"] == "during"] == ["status"]


# ---------------------------------------------------------------------------
# record: message order, measures, candidate
# ---------------------------------------------------------------------------

def fake_check(monkeypatch, row=None, **measures):
    """run_check returns a result with these measures (and a short cut)."""
    measures.setdefault("decode", "Hello")
    cut = (np.arange(8000) % 100).astype("<i2")
    reason = rec.REASONS.get(row, "") if row else ""
    monkeypatch.setattr(
        rec, "run_check",
        lambda samples, vad, decode: rec.CheckResult(row, reason, dict(measures), cut))


class TestNoiseAdvice:
    """wh-training-word-noisy-reject: the noise refuses nothing; a passed
    recording whose signal-to-noise ratio is under lead_word_noise_advice_db
    carries noise_advice=True, and the window shows the advice."""

    def test_a_noisy_word_through_the_real_check_passes_with_the_advice(
            self, tmp_path):
        # Noise of amp 600 (-39.5 dBFS) against a tone of amp 8000
        # (-15.3 dBFS): about 24 dB, like the Yoga takes.
        rig = Rig(tmp_path)
        rig.voice = Voice(noise_amp=600)
        sent = rig.record_and_pass()
        assert sent["measures"]["snr_db"] < 30
        assert sent["noise_advice"] is True
        assert folder_names(rig) == ["hello.candidate.json", "hello.candidate.wav"]

    def test_a_quiet_room_passes_without_the_advice(self, rig):
        sent = rig.record_and_pass()
        assert sent["measures"]["snr_db"] > 30
        assert sent["noise_advice"] is False

    def test_the_advice_starts_under_the_default_limit_of_30_db(
            self, rig, monkeypatch):
        fake_check(monkeypatch, snr_db=29.99)
        assert rig.record_and_pass("a")["noise_advice"] is True
        fake_check(monkeypatch, snr_db=30.0)
        assert rig.record_and_pass("b")["noise_advice"] is False

    def test_no_snr_measure_gives_no_advice(self, rig, monkeypatch):
        fake_check(monkeypatch)
        assert rig.record_and_pass()["noise_advice"] is False

    def test_the_configured_limit_decides(self, tmp_path, monkeypatch):
        fake_check(monkeypatch, snr_db=25.0)
        (tmp_path / "low").mkdir()
        (tmp_path / "high").mkdir()
        low = Rig(tmp_path / "low", noise_advice_db=20.0)
        high = Rig(tmp_path / "high", noise_advice_db=26.0)
        assert low.record_and_pass()["noise_advice"] is False
        assert high.record_and_pass()["noise_advice"] is True

    def test_a_refusal_carries_no_advice(self, rig, monkeypatch):
        fake_check(monkeypatch, row=rec.ROW_TOO_QUIET, snr_db=5.0)
        rig.command("lead_word_record", "q")
        rig.say_hello()
        assert rig.last()["stage"] == "refused"
        assert "noise_advice" not in rig.last()

    @pytest.mark.parametrize("value", ["30", True, float("nan"), None],
                             ids=["text", "bool", "nan", "none"])
    def test_a_limit_that_is_not_a_number_falls_back_to_30(
            self, tmp_path, value, caplog):
        with caplog.at_level(logging.WARNING):
            rig = Rig(tmp_path, noise_advice_db=value)
        assert rig.controller.noise_advice_db == 30.0
        assert "lead_word_noise_advice_db" in caplog.text

    def test_the_default_limit_is_the_recorder_constant(self, rig):
        assert rig.controller.noise_advice_db == rec.NOISE_ADVICE_DB == 30.0

    def test_an_integer_limit_is_used(self, tmp_path):
        assert Rig(tmp_path, noise_advice_db=25).controller.noise_advice_db == 25.0


class TestRecord:
    def test_the_order_is_listening_checking_passed(self, rig):
        rig.command("lead_word_record", "rec1")
        assert rig.forwarder.sent == []  # recording mode starts at the next chunk
        rig.feed(silence_before=1)
        assert rig.forwarder.stages == ["listening"]
        rig.feed(silence_before=19, speech=13, silence_after=33)
        assert rig.forwarder.stages == ["listening", "checking", "passed"]
        assert {m["request_id"] for m in rig.forwarder.sent} == {"rec1"}

    def test_passed_carries_the_measures_and_the_key_path(self, rig):
        passed = rig.record_and_pass()
        measures = passed["measures"]
        assert set(measures) == {
            "capture_ms", "lead_in_ms", "voiced_ms", "cut_ms", "level_dbfs",
            "noise_dbfs", "snr_db", "clipped", "regions", "decode"}
        assert measures["regions"] == 1
        assert measures["clipped"] == 0
        assert 250 <= measures["voiced_ms"] <= 1000
        assert measures["decode"] == "Hello"
        assert passed["key_path"] == ""
        assert passed["reason"] == ""
        # Plain JSON types only: no numpy scalars.
        assert json.loads(json.dumps(measures)) == measures
        for value in measures.values():
            assert type(value) in (int, float, str)

    def test_a_passed_recording_becomes_the_candidate_with_its_measures(
            self, rig, monkeypatch):
        seen = spy_run_check(monkeypatch)
        passed = rig.record_and_pass()
        assert folder_names(rig) == ["hello.candidate.json", "hello.candidate.wav"]
        saved = np.frombuffer(rig.store.candidate_path.read_bytes()[44:], dtype="<i2")
        assert np.array_equal(saved, seen["result"].cut)  # the cut, not the capture
        facts = json.loads(rig.store.candidate_meta_path.read_text("utf-8"))
        assert facts["measures"] == passed["measures"]
        assert facts["microphone"] == "Test microphone"
        assert facts["decode"] == "Hello"

    def test_the_check_runs_on_the_whole_capture(self, rig, monkeypatch):
        seen = spy_run_check(monkeypatch)
        real = rig.controller._check_and_report

        def spy(job, recording):
            seen["recording"] = recording
            return real(job, recording)

        monkeypatch.setattr(rig.controller, "_check_and_report", spy)
        rig.record_and_pass()
        assert np.array_equal(seen["samples"], seen["recording"].samples)

    def test_measures_are_sent_and_saved_as_plain_numbers(self, rig, monkeypatch):
        """A numpy scalar in a measure must reach the message and the file as
        a plain int or float (the real check returns plain types already)."""
        measures = {
            "a": np.int64(3), "b": np.float32(1.5), "c": np.int32(7),
            "decode": "Hello"}
        cut = (np.arange(8000) % 100).astype("<i2")
        monkeypatch.setattr(
            rec, "run_check",
            lambda samples, vad, decode: rec.CheckResult(None, "", measures, cut))
        rig.command("lead_word_record", "n")
        rig.say_hello()
        sent = rig.last()
        assert sent["stage"] == "passed"
        assert sent["measures"] == {"a": 3, "b": 1.5, "c": 7, "decode": "Hello"}
        assert [type(v) for v in sent["measures"].values()] == [int, float, int, str]
        facts = json.loads(rig.store.candidate_meta_path.read_text("utf-8"))
        assert facts["measures"] == sent["measures"]

    def test_the_candidate_never_changes_hello_wav(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        before = rig.store.wav_path.read_bytes()
        rig.record_and_pass("rec2")
        assert rig.store.wav_path.read_bytes() == before

    def test_the_log_line_holds_the_measures(self, rig, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            rig.record_and_pass()
        lines = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("Training-word recording:")]
        assert len(lines) == 1
        assert "passed" in lines[0] and "voiced_ms=" in lines[0]

    def test_the_check_decode_gets_the_cut_alone_while_the_engine_holds_a_word(
            self, rig):
        rig.engine.lead = np.zeros(1000, dtype=np.float32)  # a lead word is loaded
        rig.record_and_pass()
        assert len(rig.engine.decode_inputs) == 1
        decoded = rig.engine.decode_inputs[0]
        cut = rec.cut_to_float(
            np.frombuffer(
                rig.store.candidate_path.read_bytes()[44:], dtype="<i2"))
        assert decoded.dtype == np.float32
        assert np.array_equal(decoded, cut)  # the cut alone: no join, no pad
        assert rig.engine.set_calls == []   # the check never touches the word

    def test_each_check_makes_a_new_vad_with_the_silero_threshold(self, tmp_path):
        rig = Rig(tmp_path, silero_threshold=0.37)
        rig.record_and_pass("a")
        rig.record_and_pass("b")
        assert rig.vad_thresholds == [0.37, 0.37]

    def test_recording_types_no_text_and_runs_no_command(self, rig):
        rig.record_and_pass()
        rig.engine_for_processor.process_audio.assert_not_called()
        # No vad_start, stable or final message of any name.
        assert rig.processor_forwarder.method_calls == []

    def test_recognition_continues_after_the_take(self, rig):
        rig.record_and_pass()
        rig.feed(speech=2)
        rig.processor_forwarder.send_vad_start.assert_called_once()

    def test_a_record_deletes_an_older_candidate_first(self, rig):
        rig.record_and_pass("one")
        first = rig.store.candidate_path.read_bytes()
        rig.command("lead_word_record", "two")
        assert not rig.store.candidate_path.exists()
        assert not rig.store.candidate_meta_path.exists()
        assert first  # the old take existed


class TestRefused:
    def test_no_speech_in_10_seconds(self, rig):
        rig.command("lead_word_record", "quiet")
        rig.feed(silence_before=320)  # 163840 samples: past 10.0 s
        assert rig.forwarder.stages == ["listening", "checking", "refused"]
        refused = rig.last()
        assert refused["reason"] == "No speech heard"
        assert refused["request_id"] == "quiet"
        assert refused["measures"]["regions"] == 0
        # 313 chunks of 512 samples are the first whole chunks past 10.0 s.
        assert refused["measures"] == {"capture_ms": 10016, "regions": 0}
        assert rig.engine.decode_inputs == []  # run_check never ran
        assert folder_names(rig) == []

    def test_a_refused_row_deletes_the_candidate_and_names_the_reason(self, rig):
        rig.command("lead_word_record")
        rig.feed(silence_before=20, speech=3, silence_after=33)  # 96 ms of tone
        assert rig.forwarder.stages == ["listening", "checking", "refused"]
        # The region is under 200 ms, so Silero finds no region of 200 ms.
        assert rig.last()["reason"] == "No speech heard"
        assert folder_names(rig) == []

    def test_no_speech_logs_its_row(self, rig, caplog):
        with caplog.at_level(logging.INFO):
            rig.command("lead_word_record", "quiet")
            rig.feed(silence_before=320)
        lines = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("Training-word recording:")]
        assert len(lines) == 1
        assert lines[0].startswith(
            f"Training-word recording: refused ({rec.ROW_NO_SPEECH}): No speech heard")

    def test_a_refusal_leaves_no_candidate_even_when_one_appeared_during_the_check(
            self, rig):
        def a_candidate_appears():
            rig.folder.mkdir(parents=True, exist_ok=True)
            rig.store.candidate_path.write_bytes(b"x")
            rig.store.candidate_meta_path.write_bytes(b"{}")

        rig.engine.decode_text = "goodbye"
        rig.engine.on_decode = a_candidate_appears
        rig.command("lead_word_record", "r")
        rig.say_hello()
        assert rig.last()["stage"] == "refused"
        assert folder_names(rig) == []

    def test_a_refusal_does_not_read_the_key(self, rig, monkeypatch):
        # Only a passed recording needs the key path, so a config.toml that
        # cannot be read must not turn a refusal into a failure.
        monkeypatch.setattr(
            rig.store, "status",
            Mock(side_effect=PermissionError("config.toml is locked")))
        rig.engine.decode_text = "goodbye"
        rig.command("lead_word_record", "rec")
        rig.say_hello()
        assert rig.last()["stage"] == "refused"
        assert rig.last()["request_id"] == "rec"

    def test_a_decode_that_is_not_hello_refuses(self, rig):
        rig.engine.decode_text = "goodbye"
        rig.command("lead_word_record")
        rig.say_hello()
        refused = rig.last()
        assert refused["stage"] == "refused"
        assert refused["reason"] == rec.REASON_NOT_HELLO
        assert "decode" in refused["measures"]
        assert folder_names(rig) == []

    def test_refused_logs_only_the_decode_length(self, rig, caplog):
        import logging
        rig.engine.decode_text = "secretwordsaid"
        with caplog.at_level(logging.INFO):
            rig.command("lead_word_record")
            rig.say_hello()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "secretwordsaid" not in text
        assert "decode_chars=14" in text

    def test_a_record_after_a_refusal_works(self, rig):
        rig.engine.decode_text = "goodbye"
        rig.command("lead_word_record", "one")
        rig.say_hello()
        rig.engine.decode_text = "Hello"
        rig.command("lead_word_record", "two")
        rig.say_hello()
        assert rig.last()["stage"] == "passed"
        assert rig.last()["request_id"] == "two"


# ---------------------------------------------------------------------------
# busy
# ---------------------------------------------------------------------------

class TestBusy:
    @pytest.mark.parametrize("command", [
        "lead_word_record", "lead_word_keep", "lead_word_restore",
        "lead_word_off", "lead_word_on"])
    def test_a_command_while_pending_is_refused(self, rig, command):
        rig.command("lead_word_record", "first")
        rig.command(command, "second")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] == BUSY
        assert rig.last()["request_id"] == "second"

    def test_a_refused_record_leaves_the_first_take_running(self, rig):
        rig.command("lead_word_record", "first")
        rig.command("lead_word_record", "second")
        assert rig.last()["reason"] == BUSY
        rig.say_hello()
        assert rig.last()["stage"] == "passed"
        assert rig.last()["request_id"] == "first"

    def test_a_recorder_that_is_already_busy_refuses_the_record_and_frees_the_job(
            self, rig):
        assert rig.controller.recorder.request()   # busy, and no job
        rig.command("lead_word_record", "second")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] == BUSY
        assert rig.last()["request_id"] == "second"
        rig.controller.recorder.cancel()
        rig.command("lead_word_record", "third")  # the failed record left no job
        rig.say_hello()
        assert rig.last()["stage"] == "passed"
        assert rig.last()["request_id"] == "third"

    @pytest.mark.parametrize("command", [
        "lead_word_record", "lead_word_keep", "lead_word_restore",
        "lead_word_off", "lead_word_on"])
    def test_a_command_while_recording_is_refused(self, rig, command):
        rig.command("lead_word_record", "first")
        rig.feed(silence_before=2, speech=3)
        rig.command(command, "second")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] == BUSY

    @pytest.mark.parametrize("command", [
        "lead_word_record", "lead_word_keep", "lead_word_restore",
        "lead_word_off", "lead_word_on"])
    def test_a_command_during_the_check_is_refused(self, rig, command):
        answers = []
        rig.engine.on_decode = lambda: (
            rig.command(command, "second"), answers.append(rig.last()))
        rig.command("lead_word_record", "first")
        rig.say_hello()
        assert answers[0]["stage"] == "failed"
        assert answers[0]["reason"] == BUSY
        # The first take still finished.
        assert rig.last()["stage"] == "passed"

    def test_status_answers_during_a_recording(self, rig):
        rig.command("lead_word_record", "first")
        rig.feed(silence_before=2)
        rig.command("lead_word_status", "s")
        assert rig.last()["stage"] == "status"


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_discard_while_pending_means_it_never_starts(self, rig):
        rig.command("lead_word_record", "first")
        rig.command("lead_word_discard", "d")
        assert rig.last()["stage"] == "status"
        rig.feed(silence_before=3)
        assert rig.forwarder.stages == ["status"]

    def test_a_record_after_a_discard_while_pending_works(self, rig):
        rig.command("lead_word_record", "first")
        rig.command("lead_word_discard", "d")
        rig.command("lead_word_record", "second")
        rig.say_hello()
        assert rig.last()["stage"] == "passed"
        assert rig.last()["request_id"] == "second"

    def test_discard_while_recording_keeps_no_candidate_and_no_result(self, rig):
        rig.command("lead_word_record", "first")
        rig.feed(silence_before=5, speech=4)
        assert rig.forwarder.stages == ["listening"]
        rig.command("lead_word_discard", "d")
        assert rig.forwarder.stages == ["listening", "status"]
        assert rig.last()["request_id"] == "d"
        rig.feed(silence_after=40)
        assert rig.forwarder.stages == ["listening", "status"]
        assert folder_names(rig) == []

    def test_the_next_utterance_after_a_discard_is_typed_as_normal(self, rig):
        rig.command("lead_word_record", "first")
        rig.feed(silence_before=5, speech=4)
        rig.command("lead_word_discard")
        rig.feed(silence_before=2, speech=2)
        rig.processor_forwarder.send_vad_start.assert_called_once()
        rig.engine_for_processor.process_audio.assert_called()

    def test_a_status_error_after_a_pass_answers_failed(self, rig, monkeypatch):
        # The passed message reads the key from config.toml. If that read
        # raises, the window must not wait for the 45 s timeout.
        def broken():
            raise PermissionError("config.toml is locked")
        monkeypatch.setattr(rig.store, "status", broken)
        rig.command("lead_word_record", "rec")
        rig.say_hello()
        assert rig.last()["stage"] == "failed"
        assert rig.last()["request_id"] == "rec"

    def test_discard_during_the_check_keeps_no_candidate_and_no_result(self, rig):
        rig.engine.on_decode = lambda: rig.command("lead_word_discard", "d")
        rig.command("lead_word_record", "first")
        rig.say_hello()
        assert rig.forwarder.stages == ["listening", "checking", "status"]
        assert folder_names(rig) == []
        # The controller is free again.
        rig.engine.on_decode = None
        rig.command("lead_word_record", "second")
        rig.say_hello()
        assert rig.last()["stage"] == "passed"
        assert rig.last()["request_id"] == "second"

    def test_a_take_answers_only_the_request_that_started_it(self, rig, caplog):
        # A discard and a new record can both land between the end of a
        # capture and its finish callback. The old take must not answer
        # the new request.
        rig.command("lead_word_record", "old")
        rig.feed(silence_before=20, speech=13)
        assert rig.forwarder.stages == ["listening"]
        rig.controller._job = _Job("new")
        with caplog.at_level(logging.ERROR):
            rig.feed(silence_after=33)
        assert [m["request_id"] for m in rig.forwarder.sent] == ["old"]
        assert folder_names(rig) == []
        assert errors_logged(caplog) == []

    def test_a_take_whose_job_was_cleared_answers_nothing(self, rig, caplog):
        # A discard that lands between the end of a capture and its finish
        # callback leaves no job at all. The take sends nothing and runs no check.
        rig.command("lead_word_record", "old")
        rig.feed(silence_before=20, speech=13)
        rig.controller._job = None
        with caplog.at_level(logging.ERROR):
            rig.feed(silence_after=33)
        assert rig.forwarder.stages == ["listening"]
        assert rig.engine.decode_inputs == []
        assert folder_names(rig) == []
        assert errors_logged(caplog) == []

    def test_a_check_that_fails_after_it_went_stale_answers_nothing_and_keeps_the_new_job(
            self, rig):
        def a_new_job_then_a_failure():
            rig.controller._job = _Job("new")
            raise RuntimeError("the decoder broke")

        rig.engine.on_decode = a_new_job_then_a_failure
        rig.command("lead_word_record", "old")
        rig.say_hello()
        assert rig.forwarder.stages == ["listening", "checking"]
        rig.command("lead_word_keep", "k")   # the new job is still there
        assert rig.last()["reason"] == BUSY

    def test_discard_with_a_candidate_deletes_it(self, rig):
        rig.record_and_pass()
        assert folder_names(rig)
        rig.command("lead_word_discard", "d")
        assert rig.last()["stage"] == "status"
        assert folder_names(rig) == []

    def test_discard_never_changes_hello_wav(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        before = rig.store.wav_path.read_bytes()
        rig.record_and_pass("two")
        rig.command("lead_word_discard")
        assert rig.store.wav_path.read_bytes() == before

    def test_discard_with_nothing_to_discard_answers_status(self, rig):
        rig.command("lead_word_discard", "d")
        assert rig.forwarder.stages == ["status"]


# ---------------------------------------------------------------------------
# keep
# ---------------------------------------------------------------------------

class TestKeep:
    def test_the_order_record_passed_keep_is_listening_checking_passed_kept(
            self, rig):
        rig.record_and_pass("rec")
        rig.command("lead_word_keep", "keep")
        assert rig.forwarder.stages == ["listening", "checking", "passed", "kept"]
        kept = rig.last()
        assert kept["request_id"] == "keep"
        assert kept["loaded"] is True
        assert kept["recorded"] != ""
        assert kept["microphone"] == "Test microphone"
        assert kept["previous_exists"] is False
        assert "key_path" not in kept or kept["key_path"] == ""

    def test_first_keep_files_key_and_engine(self, rig):
        passed = rig.record_and_pass()
        cut_bytes = rig.store.candidate_path.read_bytes()
        rig.command("lead_word_keep")
        assert folder_names(rig) == ["hello.json", "hello.wav"]
        assert rig.store.wav_path.read_bytes() == cut_bytes
        key = tomllib.loads(rig.config.read_text("utf-8"))["engine"]["lead_word_path"]
        assert key == str(rig.store.wav_path)
        assert len(rig.engine.set_calls) == 1
        assert rig.engine.has_lead_word()
        meta = json.loads(rig.store.meta_path.read_text("utf-8"))
        assert meta["decode"] == "Hello"
        assert meta["measures"] == passed["measures"]
        assert meta["microphone"] == "Test microphone"

    def test_a_replacement_keeps_the_old_recording_as_previous(self, rig):
        rig.record_and_pass("one")
        rig.command("lead_word_keep")
        old = rig.store.wav_path.read_bytes()
        rig.voice = Voice()
        rig.voice.rng = np.random.default_rng(99)
        rig.record_and_pass("two")
        new = rig.store.candidate_path.read_bytes()
        rig.command("lead_word_keep", "k2")
        assert rig.store.previous_path.read_bytes() == old
        assert rig.store.wav_path.read_bytes() == new
        assert rig.last()["previous_exists"] is True
        assert len(rig.engine.set_calls) == 2

    def test_keep_with_no_candidate_fails(self, rig):
        rig.command("lead_word_keep", "k")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] == "No new recording to keep"

    def test_passed_after_a_keep_reports_the_key_path(self, rig):
        rig.record_and_pass("one")
        rig.command("lead_word_keep")
        passed = rig.record_and_pass("two")
        assert passed["key_path"] == str(rig.store.wav_path)

    @pytest.mark.parametrize("step", [
        "first_replace", "second_replace", "meta", "key", "engine"])
    def test_a_failing_keep_step_leaves_the_old_word(
            self, rig, monkeypatch, step):
        rig.record_and_pass("one")
        rig.command("lead_word_keep")
        old_wav = rig.store.wav_path.read_bytes()
        old_config = rig.config.read_bytes()
        engine_calls = len(rig.engine.set_calls)
        old_lead = rig.engine.lead
        rig.voice.rng = np.random.default_rng(5)
        rig.record_and_pass("two")
        candidate = rig.store.candidate_path.read_bytes()

        real_replace = store_mod._replace
        calls = {"n": 0}

        def failing_replace(source, target):
            calls["n"] += 1
            if (step == "first_replace" and calls["n"] == 1) or (
                    step == "second_replace" and calls["n"] == 2):
                raise OSError(f"replace {calls['n']} failed")
            real_replace(source, target)

        monkeypatch.setattr(store_mod, "_replace", failing_replace)
        if step == "meta":
            monkeypatch.setattr(
                LeadWordStore, "_write_json",
                Mock(side_effect=OSError("meta failed")))
        if step == "key":
            # The key already names hello.wav after the first Keep, so make
            # the key look like it names another file to force the write.
            rig.config.write_bytes(
                old_config.replace(
                    str(rig.store.wav_path).encode("utf-8"), b"D:/other.wav"))
            old_config = rig.config.read_bytes()
            monkeypatch.setattr(
                store_mod, "write_lead_word_key",
                Mock(side_effect=OSError("key failed")))
        if step == "engine":
            def refuse(audio):
                raise RuntimeError("engine failed")
            rig.engine.set_lead_word = refuse

        rig.command("lead_word_keep", "k2")
        failed = rig.last()
        assert failed["stage"] == "failed"
        assert failed["request_id"] == "k2"
        assert "failed" in failed["reason"]
        assert rig.store.wav_path.read_bytes() == old_wav
        assert rig.config.read_bytes() == old_config
        assert len(rig.engine.set_calls) == engine_calls
        assert rig.engine.lead is old_lead
        # The candidate is still there: the user can try again.
        assert rig.store.candidate_path.read_bytes() == candidate


# ---------------------------------------------------------------------------
# restore, off, on
# ---------------------------------------------------------------------------

def two_recordings(rig):
    rig.record_and_pass("one")
    rig.command("lead_word_keep")
    first = rig.store.wav_path.read_bytes()
    rig.voice.rng = np.random.default_rng(77)
    rig.record_and_pass("two")
    rig.command("lead_word_keep")
    return first, rig.store.wav_path.read_bytes()


class TestRestoreOffOn:
    def test_restore_swaps_the_files_and_loads_the_word(self, rig):
        first, second = two_recordings(rig)
        calls = len(rig.engine.set_calls)
        rig.command("lead_word_restore", "back")
        message = rig.last()
        assert message["stage"] == "restored"
        assert message["request_id"] == "back"
        assert message["loaded"] is True
        assert message["previous_exists"] is True
        assert rig.store.wav_path.read_bytes() == first
        assert rig.store.previous_path.read_bytes() == second
        assert len(rig.engine.set_calls) == calls + 1
        # A second press goes back to the recording the first press replaced.
        rig.command("lead_word_restore", "forward")
        assert rig.store.wav_path.read_bytes() == second

    def test_restore_of_an_earlier_file_reports_that_hello_wav_exists(
            self, rig, tmp_path):
        """wh-first-sound-lost.23.1.1: the key named another file, the first
        Keep copied it to hello.previous.wav with an empty recorded date, and
        "Go back" makes it hello.wav. The restored stage says that hello.wav
        exists although its recorded date is empty."""
        other = tmp_path / "mine" / "word.wav"
        other.parent.mkdir()
        rec.write_wav(other, np.random.default_rng(9).integers(
            -3000, 3000, size=8000, dtype=np.int16))
        store_mod.write_lead_word_key(rig.config, str(other))
        rig.record_and_pass()
        rig.command("lead_word_keep", "keep")
        assert rig.last()["stage"] == "kept"
        rig.command("lead_word_restore", "back")
        message = rig.last()
        assert message["stage"] == "restored"
        assert message["recorded"] == ""
        assert message["current_exists"] is True
        assert rig.store.wav_path.read_bytes() == other.read_bytes()

    def test_off_and_on_report_that_hello_wav_exists(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        rig.command("lead_word_off", "off")
        assert rig.last()["stage"] == "off"
        assert rig.last()["current_exists"] is True
        rig.command("lead_word_on", "on")
        assert rig.last()["stage"] == "on"
        assert rig.last()["current_exists"] is True

    def test_restore_without_a_previous_recording_fails(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        rig.command("lead_word_restore", "back")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] != ""

    def test_off_empties_the_key_keeps_the_files_and_clears_the_engine(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        rig.command("lead_word_off", "off")
        message = rig.last()
        assert message["stage"] == "off"
        assert message["loaded"] is False
        assert message["recorded"] != ""  # the files are kept
        assert rig.store.wav_path.exists()
        key = tomllib.loads(rig.config.read_text("utf-8"))["engine"]["lead_word_path"]
        assert key == ""
        assert rig.engine.lead is None
        assert rig.engine.has_lead_word() is False

    def test_on_sets_the_key_and_loads_the_word(self, rig):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        rig.command("lead_word_off")
        rig.command("lead_word_on", "on")
        message = rig.last()
        assert message["stage"] == "on"
        assert message["loaded"] is True
        key = tomllib.loads(rig.config.read_text("utf-8"))["engine"]["lead_word_path"]
        assert key == str(rig.store.wav_path)
        assert rig.engine.has_lead_word()

    def test_on_with_no_recording_fails(self, rig):
        rig.command("lead_word_on", "on")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["reason"] != ""
        assert rig.engine.set_calls == []

    def test_a_failed_off_reports_failed_and_keeps_the_word(
            self, rig, monkeypatch):
        rig.record_and_pass()
        rig.command("lead_word_keep")
        monkeypatch.setattr(
            store_mod, "write_lead_word_key",
            Mock(side_effect=OSError("config is locked")))
        rig.command("lead_word_off", "off")
        assert rig.last()["stage"] == "failed"
        assert "config is locked" in rig.last()["reason"]
        assert rig.engine.has_lead_word()


# ---------------------------------------------------------------------------
# failures that must not stop the audio thread or leave the window waiting
# ---------------------------------------------------------------------------

class TestFailures:
    def test_a_failing_check_sends_failed_and_frees_the_job(self, rig):
        def boom():
            raise RuntimeError("the decoder broke")

        rig.engine.on_decode = boom
        rig.command("lead_word_record", "first")
        rig.say_hello()  # does not raise
        failed = rig.last()
        assert rig.forwarder.stages == ["listening", "checking", "failed"]
        assert failed["request_id"] == "first"
        assert "the decoder broke" in failed["reason"]
        assert folder_names(rig) == []
        rig.engine.on_decode = None
        rig.command("lead_word_record", "second")
        rig.say_hello()
        assert rig.last()["stage"] == "passed"

    def test_a_candidate_write_failure_sends_failed(self, rig, monkeypatch):
        monkeypatch.setattr(
            rec, "write_wav", Mock(side_effect=OSError("disk full")))
        rig.command("lead_word_record", "first")
        rig.say_hello()
        assert rig.last()["stage"] == "failed"
        assert "disk full" in rig.last()["reason"]
        assert folder_names(rig) == []

    def test_a_command_that_raises_sends_failed(self, rig, monkeypatch):
        monkeypatch.setattr(
            rig.store, "status", Mock(side_effect=RuntimeError("boom")))
        rig.command("lead_word_status", "s")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["request_id"] == "s"
        assert "boom" in rig.last()["reason"]

    def test_an_unknown_command_sends_failed(self, rig):
        rig.command("lead_word_dance", "x")
        assert rig.last()["stage"] == "failed"
        assert rig.last()["request_id"] == "x"
        assert rig.last()["reason"] == "Unknown training-word command: lead_word_dance"

    def test_a_forwarder_that_raises_never_stops_the_audio_thread(
            self, rig, caplog):
        rig.forwarder.send_lead_word_result = Mock(
            side_effect=RuntimeError("socket gone"))
        with caplog.at_level(logging.ERROR):
            rig.controller.handle_command("lead_word_record", "x")
            rig.say_hello()  # does not raise
        rig.feed(speech=2)
        rig.processor_forwarder.send_vad_start.assert_called_once()
        # The take went on to its end although no message could be sent.
        assert folder_names(rig) == ["hello.candidate.json", "hello.candidate.wav"]
        assert "Cannot send the training-word result 'listening'" in caplog.text
        assert "Cannot send the training-word result 'passed'" in caplog.text


class TestSampleRate:
    def test_only_16_khz_builds(self, rig):
        with pytest.raises(ValueError):
            LeadWordController(
                engine=rig.engine, forwarder=rig.forwarder, store=rig.store,
                silero_threshold=0.5, get_microphone=lambda: "",
                sample_rate=22050)

    def test_the_recorder_gets_the_real_rate_and_a_lead_in_no_longer_than_the_buffer(
            self, rig):
        controller = LeadWordController(
            engine=rig.engine, forwarder=rig.forwarder, store=rig.store,
            silero_threshold=0.5, get_microphone=lambda: "",
            vad_lead_in_ms=100)
        assert controller.recorder.sample_rate == 16000
        assert controller.recorder._lead_in_bytes == 100 * 16 * 2


class TestPushToTalkRelease:
    def test_the_release_ends_the_take_and_the_check_runs(self, rig):
        """Transcription is off from the release on, so no chunk arrives."""
        rig.command("lead_word_record", "ptt")
        rig.feed(silence_before=20, speech=13)
        assert rig.forwarder.stages == ["listening"]
        rig.processor.end_recording_now()
        assert rig.forwarder.stages == ["listening", "checking", "passed"]


# ---------------------------------------------------------------------------
# Test-strength additions of stage G1 (wh-first-sound-lost.23)
# ---------------------------------------------------------------------------

class TestRecorderSettings:
    def test_a_long_processor_buffer_still_gives_a_300_ms_lead_in(self, rig):
        """Acceptance item 4: the raw lead-in is at most 300 ms."""
        controller = LeadWordController(
            engine=rig.engine, forwarder=rig.forwarder, store=rig.store,
            silero_threshold=0.5, get_microphone=lambda: "",
            vad_lead_in_ms=500)
        assert controller.recorder._lead_in_bytes == 300 * 16 * 2

    def test_a_shorter_processor_buffer_limits_the_lead_in(self, rig):
        controller = LeadWordController(
            engine=rig.engine, forwarder=rig.forwarder, store=rig.store,
            silero_threshold=0.5, get_microphone=lambda: "",
            vad_lead_in_ms=250)
        assert controller.recorder._lead_in_bytes == 250 * 16 * 2

    def test_the_capture_ends_1_second_after_the_last_speech_chunk(self, rig):
        """The real timers, through the real controller: 31 chunks of 512
        samples are 15872 samples, 32 are 16384, so the 32nd quiet chunk ends it."""
        rig.command("lead_word_record", "t")
        rig.feed(silence_before=20, speech=13, silence_after=31)
        assert rig.forwarder.stages == ["listening"]
        rig.feed(silence_after=1)
        assert rig.forwarder.stages[1] == "checking"

    def test_the_capture_ends_4_seconds_after_the_gate_opened(self, rig):
        """64000 samples are 125 chunks of 512: the 125th speech chunk ends it."""
        rig.command("lead_word_record", "t")
        rig.feed(speech=124)
        assert rig.forwarder.stages == ["listening"]
        rig.feed(speech=1)
        assert rig.forwarder.stages[1] == "checking"


class TestNoLockOnTheReadPaths:
    """Contract section 7: status and capabilities take no lock, and the
    candidate write is under the lock, so a discard cannot slip past it."""

    def test_status_and_capabilities_answer_while_the_candidate_is_written(
            self, rig, monkeypatch):
        real_save = rig.store.save_candidate
        seen = {}

        def save_while_others_ask(*args, **kwargs):
            # This runs under the controller lock (the audio thread holds it
            # for the candidate write). The forwarder thread asks meanwhile.
            def ask():
                seen["capabilities"] = rig.controller.capabilities()

            def ask_status():
                rig.command("lead_word_status", "during")

            threads = [threading.Thread(target=ask, daemon=True),
                       threading.Thread(target=ask_status, daemon=True)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3)
            seen["finished"] = [not t.is_alive() for t in threads]
            return real_save(*args, **kwargs)

        monkeypatch.setattr(rig.store, "save_candidate", save_while_others_ask)
        rig.record_and_pass()
        assert seen["finished"] == [True, True]
        assert seen["capabilities"]["supported"] is True
        assert [m["stage"] for m in rig.forwarder.sent
                if m["request_id"] == "during"] == ["status"]

    def test_the_key_is_read_without_the_lock(self, rig, monkeypatch):
        real = rig.store.status
        held = []

        def status():
            held.append(rig.controller._lock.locked())
            return real()

        monkeypatch.setattr(rig.store, "status", status)
        rig.record_and_pass()
        assert held and not any(held)

    def test_a_discard_that_arrives_during_the_candidate_write_leaves_no_candidate(
            self, rig, monkeypatch):
        real_save = rig.store.save_candidate
        holder = {}

        def save_while_a_discard_arrives(*args, **kwargs):
            thread = threading.Thread(
                target=lambda: rig.command("lead_word_discard", "d"),
                daemon=True)
            holder["thread"] = thread
            thread.start()
            thread.join(0.5)  # a discard that is not held back finishes here
            return real_save(*args, **kwargs)

        monkeypatch.setattr(rig.store, "save_candidate", save_while_a_discard_arrives)
        rig.command("lead_word_record", "rec")
        rig.say_hello()
        holder["thread"].join(5)
        assert not holder["thread"].is_alive()
        assert folder_names(rig) == []


class TestStartWithoutAJob:
    def test_a_take_that_starts_without_a_job_is_cancelled(self, rig, caplog):
        """A discard can land between the request and the first chunk. Recording
        mode then starts with no job: no 'listening', the take is cancelled at
        once, and the next utterance is typed as normal."""
        assert rig.controller.recorder.request()   # no job: _job stays None
        with caplog.at_level(logging.ERROR):
            rig.feed(silence_before=1)
        assert rig.forwarder.sent == []
        assert errors_logged(caplog) == []   # the start callback did not fail
        rig.feed(speech=2)
        rig.processor_forwarder.send_vad_start.assert_called_once()
        rig.engine_for_processor.process_audio.assert_called()
        assert rig.controller.recorder.busy is False
