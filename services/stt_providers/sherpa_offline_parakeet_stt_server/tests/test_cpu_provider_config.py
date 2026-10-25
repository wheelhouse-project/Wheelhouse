"""Tests for the ONNX Runtime conf file on the CPU path
(wh-parakeet-spinning-upgrade).

ONNX Runtime worker threads wait for work in a loop instead of sleeping,
because session.intra_op.allow_spinning defaults to 1. Inside WheelHouse
that kept 8 to 9 logical processors busy for the whole time a person
spoke. From sherpa-onnx 1.13.5 the provider string "cpu:<conf path>"
gives each "SessionConfig.<key>=<value>" line of the conf file to ONNX
Runtime, so the provider turns the loop off with a shipped conf file.

sherpa-onnx ends the whole process (SHERPA_ONNX_EXIT) when it cannot
open the conf file, so the engine passes the path only when the file
exists, and otherwise falls back to plain "cpu".
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import sherpa_engine
from sherpa_engine import SherpaOfflineEngine

SPINNING_OFF = "SessionConfig.session.intra_op.allow_spinning=0"


def _fake_model_dir(tmp_path: Path) -> Path:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (model_dir / name).write_text("", encoding="utf-8")
    return model_dir


def _provider_passed(model_dir: Path, **kwargs) -> str:
    with patch("sherpa_engine.sherpa_onnx") as sherpa_mock:
        SherpaOfflineEngine(model_path=str(model_dir), **kwargs)
        call = sherpa_mock.OfflineRecognizer.from_transducer.call_args
    assert call is not None, "from_transducer was not called"
    return call.kwargs["provider"]


def _settings(conf_text: str) -> dict[str, str]:
    """Read a conf file the way sherpa-onnx ParseConfigFile reads it:
    trimmed key=value lines, '#' comments and empty lines skipped."""
    settings = {}
    for raw in conf_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
    return settings


class TestShippedConfFile:
    def test_the_conf_file_sits_beside_the_engine(self):
        conf = sherpa_engine.CPU_PROVIDER_CONFIG
        assert conf == Path(sherpa_engine.__file__).resolve().parent / conf.name
        assert conf.is_file()

    def test_the_conf_file_turns_off_spinning(self):
        text = sherpa_engine.CPU_PROVIDER_CONFIG.read_text(encoding="utf-8")
        settings = _settings(text)
        key, value = SPINNING_OFF.split("=", 1)
        assert settings.get(key) == value

    def test_the_conf_file_has_no_debug_line(self):
        # DEBUG=1 makes sherpa-onnx print every setting at each start.
        # It is for the manual check only and must not ship.
        text = sherpa_engine.CPU_PROVIDER_CONFIG.read_text(encoding="utf-8")
        assert "DEBUG" not in _settings(text)


class TestProviderString:
    def test_cpu_path_passes_the_conf_file_by_absolute_path(self, tmp_path):
        provider = _provider_passed(_fake_model_dir(tmp_path))
        name, _, conf_path = provider.partition(":")
        assert name == "cpu"
        # Absolute, so the os.chdir to the model directory for external
        # weights cannot make the path point somewhere else.
        assert Path(conf_path).is_absolute()
        assert Path(conf_path) == sherpa_engine.CPU_PROVIDER_CONFIG

    def test_the_passed_conf_file_turns_off_spinning(self, tmp_path):
        provider = _provider_passed(_fake_model_dir(tmp_path))
        conf_path = Path(provider.partition(":")[2])
        settings = _settings(conf_path.read_text(encoding="utf-8"))
        key, value = SPINNING_OFF.split("=", 1)
        assert settings.get(key) == value

    def test_gpu_path_stays_cuda(self, tmp_path):
        provider = _provider_passed(_fake_model_dir(tmp_path), use_gpu=True)
        assert provider == "cuda"

    def test_a_missing_conf_file_falls_back_to_plain_cpu(
        self, tmp_path, monkeypatch, caplog
    ):
        missing = tmp_path / "gone" / "onnxruntime_cpu.conf"
        monkeypatch.setattr(sherpa_engine, "CPU_PROVIDER_CONFIG", missing)
        with caplog.at_level(logging.WARNING, logger="sherpa_engine"):
            provider = _provider_passed(_fake_model_dir(tmp_path))
        assert provider == "cpu"
        assert str(missing) in caplog.text

    def test_a_directory_in_place_of_the_conf_file_falls_back(
        self, tmp_path, monkeypatch
    ):
        folder = tmp_path / "onnxruntime_cpu.conf"
        folder.mkdir()
        monkeypatch.setattr(sherpa_engine, "CPU_PROVIDER_CONFIG", folder)
        assert _provider_passed(_fake_model_dir(tmp_path)) == "cpu"
