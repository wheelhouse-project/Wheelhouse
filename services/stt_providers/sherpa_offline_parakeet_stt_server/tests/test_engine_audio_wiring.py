"""The Parakeet server wires the engine-audio saver (wh-first-sound-lost.1).

The saver itself is covered in
services/stt_providers/shared/tests/test_engine_audio_saver.py, and the
hand-off from AudioProcessor in test_audio_processor.py beside it. What is
covered here is only the wiring, which is where the feature can silently do
nothing:

  1. The server builds the saver with create_from_environment("parakeet_tdt")
     and gives it to AudioProcessor. "parakeet_tdt" is [provider] name in this
     provider's config.toml and the provider_name its WSForwarder uses.
  2. start() forwards the saver's logger to Wheelhouse. This provider sets
     logging.getLogger("shared_stt").propagate = False with no handler on it,
     so a record from shared_stt.audio_processor.engine_audio is discarded
     unless a handler is attached to that logger itself. Without it the
     "Saved engine audio for UTT-N: <path>" line never reaches
     wheelhouse.log and nobody can tell which file belongs to which
     utterance.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, Mock, patch

import pytest

import main as parakeet_main
from shared_stt.engine_audio_saver import ENV_VAR, LOGGER_NAME, EngineAudioSaver

PROVIDER_NAME = "parakeet_tdt"


def _build_server(**kwargs):
    """A ParakeetServer with the model, socket, and microphone stubbed."""
    capture = MagicMock()
    capture.get_stats = Mock(return_value={})
    with patch.object(parakeet_main, "SherpaOfflineEngine"), \
         patch.object(parakeet_main, "WSForwarder"), \
         patch.object(parakeet_main, "get_audio_provider",
                      return_value=capture):
        return parakeet_main.ParakeetServer(
            model_config={"model_path": "C:/unused", "use_gpu": False},
            engine_config={},
            **kwargs,
        )


class TestTheSaverIsGivenToTheProcessor:
    def _build_with_processor_patched(self, **kwargs):
        capture = MagicMock()
        capture.get_stats = Mock(return_value={})
        with patch.object(parakeet_main, "SherpaOfflineEngine"), \
             patch.object(parakeet_main, "WSForwarder"), \
             patch.object(parakeet_main, "get_audio_provider",
                          return_value=capture), \
             patch.object(parakeet_main, "AudioProcessor") as processor_cls:
            parakeet_main.ParakeetServer(
                model_config={"model_path": "C:/unused", "use_gpu": False},
                engine_config={},
                **kwargs,
            )
        return processor_cls

    def test_the_processor_gets_what_create_from_environment_returned(self):
        saver = object()
        with patch.object(parakeet_main, "create_from_environment",
                          return_value=saver) as create:
            processor_cls = self._build_with_processor_patched()
        assert processor_cls.call_args.kwargs["engine_audio_saver"] is saver
        create.assert_called_once()

    def test_the_saver_is_built_for_this_provider_by_its_config_name(self):
        with patch.object(parakeet_main, "create_from_environment",
                          return_value=None) as create:
            self._build_with_processor_patched()
        assert create.call_args.args[0] == PROVIDER_NAME

    def test_the_saver_is_built_with_the_engine_sample_rate(self):
        """The WAV header takes its rate from this argument, so a saver built
        at the default 16000 would label audio from a 22050 Hz engine with
        the wrong rate and the file would play too fast."""
        with patch.object(parakeet_main, "create_from_environment",
                          return_value=None) as create:
            self._build_with_processor_patched(sample_rate=22050)
        create.assert_called_once()
        assert create.call_args.kwargs["sample_rate"] == 22050

    def test_an_off_feature_passes_none_explicitly(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        processor_cls = self._build_with_processor_patched()
        assert "engine_audio_saver" in processor_cls.call_args.kwargs
        assert processor_cls.call_args.kwargs["engine_audio_saver"] is None

    def test_the_environment_switch_reaches_the_processor(
            self, monkeypatch, tmp_path):
        """No patching of create_from_environment: the real function reads
        the real variable, so a provider that read a different name would
        fail here."""
        monkeypatch.setenv(ENV_VAR, "1")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        processor_cls = self._build_with_processor_patched()
        saver = processor_cls.call_args.kwargs["engine_audio_saver"]
        try:
            assert isinstance(saver, EngineAudioSaver)
        finally:
            if saver is not None:
                saver.stop()


@pytest.fixture
def restore_logging():
    """Undo start()'s edits to the global logger tree."""
    names = ("ParakeetTDT", "shared_stt", "shared_stt.audio_processor",
             LOGGER_NAME, parakeet_main.LOAD_METRICS_LOGGER_NAME,
             parakeet_main.CAPTURE_LOGGER_NAME, "shared_audio.microphone",
             "shared_audio.overflow_monitor", "lead_word_controller",
             "lead_word_recorder", "lead_word_store", "")
    before = {n: (list(logging.getLogger(n).handlers),
                  logging.getLogger(n).propagate,
                  logging.getLogger(n).level) for n in names}
    yield
    for name, (handlers, propagate, level) in before.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def _started(server):
    """Run start() through one loop iteration and its cleanup."""
    def read(timeout=0.02):
        server.running = False
        return None
    server.audio_capture.read = Mock(side_effect=read)
    with patch.object(parakeet_main.signal, "signal"):
        server.start()


def _forwarded(server):
    return [c.kwargs.get("message", "")
            for c in server.forwarder.send_log.call_args_list]


def _emit_at_info(name, message):
    """Log one INFO record with the level check taken out of the question
    (under pytest the root logger sits at WARNING)."""
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.info(message)


class TestTheEngineAudioLoggerIsForwarded:
    SAVED_LINE = (
        "Saved engine audio for UTT-12: "
        r"C:\Users\x\AppData\Local\WheelHouse\engine-audio"
        r"\engine-audio_20261019-200507-045_parakeet_tdt_utt12.wav"
    )

    def test_the_handler_is_attached_to_the_engine_audio_logger(
            self, monkeypatch, restore_logging):
        monkeypatch.delenv(ENV_VAR, raising=False)
        server = _build_server()
        _started(server)
        assert server._ws_log_handler in logging.getLogger(
            LOGGER_NAME).handlers

    def test_a_saved_line_reaches_the_forwarder(
            self, monkeypatch, restore_logging):
        """The attachment is the point, so assert that the record arrives
        and not only that a handler sits in a list. The environment switch is
        OFF here: the logger is forwarded whether or not the feature is on,
        because the setting can change between provider starts."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        server = _build_server()
        _started(server)
        _emit_at_info(LOGGER_NAME, self.SAVED_LINE)
        sent = [m for m in _forwarded(server) if self.SAVED_LINE in m]
        assert len(sent) == 1, "the line must arrive once, not zero or twice"

    def test_a_warning_from_the_saver_reaches_the_forwarder(
            self, monkeypatch, restore_logging):
        monkeypatch.delenv(ENV_VAR, raising=False)
        server = _build_server()
        _started(server)
        logging.getLogger(LOGGER_NAME).setLevel(logging.INFO)
        logging.getLogger(LOGGER_NAME).warning(
            "Cannot save engine audio: disk full")
        assert any("Cannot save engine audio: disk full" in m
                   for m in _forwarded(server))

    def test_the_ordinary_processor_logger_stays_silent(
            self, monkeypatch, restore_logging):
        """Narrow forwarding: [vad_utt_stats] and every other shared_stt
        record keep the visibility they have today."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        server = _build_server()
        _started(server)
        _emit_at_info("shared_stt.audio_processor", "[vad_utt_stats] utt=7")
        assert not any("vad_utt_stats" in m for m in _forwarded(server))


class TestTheTrainingWordLoggersAreForwarded:
    """Acceptance items 8 and 13 put the check measures and the old key
    value in a log line. The root logger writes only to stdout, which no
    log file captures, so these loggers need the WebSocket handler too."""

    @pytest.mark.parametrize("name", [
        "lead_word_controller", "lead_word_recorder", "lead_word_store"])
    def test_a_training_word_line_reaches_the_forwarder(
            self, name, monkeypatch, restore_logging):
        monkeypatch.delenv(ENV_VAR, raising=False)
        server = _build_server()
        _started(server)
        _emit_at_info(name, "Training word check: passed")
        assert "Training word check: passed" in _forwarded(server)
