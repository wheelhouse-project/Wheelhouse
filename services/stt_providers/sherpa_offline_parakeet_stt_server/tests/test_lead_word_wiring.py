"""The Parakeet server wires the training-word controller (wh-first-sound-lost.23).

lead_word_controller is covered in test_lead_word_controller.py. What is
covered here is only the wiring, which is where the feature can silently do
nothing:

  1. The server builds the controller and gives its recorder to
     AudioProcessor (the processor has no setter), with the store on this
     provider's config.toml and the %LOCALAPPDATA% folder, the [client]
     silero_threshold, and the forwarder's microphone name.
  2. The forwarder gets the command callback and the capabilities callable.
  3. A rate other than 16 kHz, or no LOCALAPPDATA, builds no controller:
     lead_word stays out of the capabilities frame and the server still starts.
  4. The audio loop calls end_recording_now() where it skips process_chunk
     because transcription is off (decision M1: a push-to-talk release ends
     the take).
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main
from lead_word_controller import LeadWordController
from lead_word_store import lead_word_folder


def _build(monkeypatch, tmp_path=None, forwarder=None, engine_config=None,
           **kwargs):
    """A ParakeetServer with the model, socket and microphone stubbed.

    Returns (server, processor_class_mock)."""
    if tmp_path is not None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    capture = MagicMock()
    capture.get_stats = Mock(return_value={})
    with patch.object(parakeet_main, "SherpaOfflineEngine"), \
         patch.object(parakeet_main, "WSForwarder",
                      return_value=forwarder or MagicMock()), \
         patch.object(parakeet_main, "get_audio_provider",
                      return_value=capture), \
         patch.object(parakeet_main, "AudioProcessor") as processor_cls:
        server = parakeet_main.ParakeetServer(
            model_config={"model_path": "C:/unused", "use_gpu": False},
            engine_config=engine_config or {},
            **kwargs,
        )
    return server, processor_cls


class TestTheControllerIsBuilt:
    def test_the_processor_gets_the_controllers_recorder(
            self, monkeypatch, tmp_path):
        server, processor_cls = _build(monkeypatch, tmp_path)
        assert isinstance(server.lead_word_controller, LeadWordController)
        assert (processor_cls.call_args.kwargs["chunk_recorder"]
                is server.lead_word_controller.recorder)

    def test_the_forwarder_gets_the_callback_and_the_capabilities(
            self, monkeypatch, tmp_path):
        server, _ = _build(monkeypatch, tmp_path)
        controller = server.lead_word_controller
        assert server.forwarder.lead_word_callback == controller.handle_command
        assert (server.forwarder.lead_word_capabilities
                == controller.capabilities)

    def test_the_store_uses_this_providers_config_and_the_appdata_folder(
            self, monkeypatch, tmp_path):
        server, _ = _build(monkeypatch, tmp_path)
        store = server.lead_word_controller.store
        assert store.config_path == Path(parakeet_main.__file__).parent / "config.toml"
        assert store.folder == lead_word_folder()
        assert store.folder == tmp_path / "WheelHouse" / "lead-word"
        assert not store.folder.exists()  # building the server creates nothing

    def test_the_client_values_reach_the_controller(self, monkeypatch, tmp_path):
        server, _ = _build(
            monkeypatch, tmp_path, vad_threshold=0.37, vad_lead_in_ms=120)
        controller = server.lead_word_controller
        assert controller._silero_threshold == 0.37
        assert controller.recorder.sample_rate == 16000
        assert controller.recorder._lead_in_bytes == 120 * 16 * 2

    def test_the_microphone_name_is_read_from_the_forwarder_at_each_call(
            self, monkeypatch, tmp_path):
        forwarder = MagicMock()
        forwarder.microphone_device = ""
        server, _ = _build(monkeypatch, tmp_path, forwarder=forwarder)
        controller = server.lead_word_controller
        assert controller._get_microphone() == ""
        forwarder.microphone_device = "USB microphone"
        assert controller._get_microphone() == "USB microphone"

    def test_the_engine_noise_advice_key_reaches_the_controller(
            self, monkeypatch, tmp_path):
        # wh-training-word-noisy-reject: [engine] lead_word_noise_advice_db.
        server, _ = _build(
            monkeypatch, tmp_path,
            engine_config={"lead_word_noise_advice_db": 22.5})
        assert server.lead_word_controller.noise_advice_db == 22.5

    def test_without_the_noise_advice_key_the_limit_is_30_db(
            self, monkeypatch, tmp_path):
        server, _ = _build(monkeypatch, tmp_path)
        assert server.lead_word_controller.noise_advice_db == 30.0

    def test_the_tracked_config_holds_the_noise_advice_key_at_30_db(self):
        import tomllib
        config = Path(parakeet_main.__file__).parent / "config.toml"
        engine = tomllib.loads(config.read_text("utf-8"))["engine"]
        assert engine["lead_word_noise_advice_db"] == 30.0

    def test_the_engine_is_the_servers_engine(self, monkeypatch, tmp_path):
        server, _ = _build(monkeypatch, tmp_path)
        assert server.lead_word_controller.engine is server.engine


class TestNoControllerWhenItCannotWork:
    @staticmethod
    def _plain_forwarder():
        return SimpleNamespace(
            lead_word_callback=None, lead_word_capabilities=None,
            microphone_device="")

    def test_another_rate_builds_no_controller(self, monkeypatch, tmp_path):
        forwarder = self._plain_forwarder()
        # A handler on the logger itself: start() of an earlier test can leave
        # propagate off, and caplog sits on the root logger.
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        provider_logger = logging.getLogger("ParakeetTDT")
        provider_logger.addHandler(handler)
        try:
            server, processor_cls = _build(
                monkeypatch, tmp_path, forwarder=forwarder, sample_rate=22050)
        finally:
            provider_logger.removeHandler(handler)
        assert server.lead_word_controller is None
        assert processor_cls.call_args.kwargs["chunk_recorder"] is None
        assert forwarder.lead_word_callback is None
        assert forwarder.lead_word_capabilities is None
        assert any("training word" in r.getMessage().lower() for r in records)

    def test_no_localappdata_builds_no_controller_and_the_server_starts(
            self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        forwarder = self._plain_forwarder()
        server, processor_cls = _build(monkeypatch, forwarder=forwarder)
        assert server.lead_word_controller is None
        assert processor_cls.call_args.kwargs["chunk_recorder"] is None
        assert forwarder.lead_word_capabilities is None


def _run_with_one_chunk(server):
    """Run process_audio_loop for exactly one chunk, then stop it."""
    reads = {"n": 0}

    def read(timeout=0.02):
        reads["n"] += 1
        if reads["n"] > 1:
            server.running = False
            return None
        return b"\x00\x00" * 480

    server.audio_capture.read = Mock(side_effect=read)
    server.running = True
    server.process_audio_loop()


class TestAReleaseEndsTheTake:
    def _server(self, monkeypatch, tmp_path):
        server, _ = _build(monkeypatch, tmp_path)
        server.audio_processor = Mock()
        server._wake_word_detector = None
        return server

    def test_transcription_off_calls_end_recording_now(
            self, monkeypatch, tmp_path):
        server = self._server(monkeypatch, tmp_path)
        server.transcription_enabled.clear()
        _run_with_one_chunk(server)
        server.audio_processor.end_recording_now.assert_called_once_with()
        server.audio_processor.process_chunk.assert_not_called()

    def test_transcription_on_processes_the_chunk_and_does_not_end_the_take(
            self, monkeypatch, tmp_path):
        server = self._server(monkeypatch, tmp_path)
        server.transcription_enabled.set()
        _run_with_one_chunk(server)
        server.audio_processor.process_chunk.assert_called_once()
        server.audio_processor.end_recording_now.assert_not_called()

    def test_a_failing_end_does_not_stop_the_wake_word_branch(
            self, monkeypatch, tmp_path):
        server = self._server(monkeypatch, tmp_path)
        server.transcription_enabled.clear()
        server.audio_processor.end_recording_now.side_effect = RuntimeError("x")
        detector = Mock()
        detector.process = Mock(return_value=None)
        server._wake_word_detector = detector
        server._wake_word_listening = True
        _run_with_one_chunk(server)
        detector.process.assert_called_once()
