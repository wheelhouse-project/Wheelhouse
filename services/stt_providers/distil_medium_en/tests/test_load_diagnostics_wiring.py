"""The distil server passes [debug] log_load_diagnostics to the shared code.

wh-audit13-preengine-load-review.1. AudioProcessor and WakeWordDetector write
a [load-diag] work line every ten seconds of silence or wake listening only
when this setting is on, and both default it to off. The behaviour is tested
in services/stt_providers/shared/tests/test_pre_engine_load.py; covered here
is only the wiring from this provider's config.toml to those constructors,
which is where the setting can silently stop reaching the code.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import main as distil_main
from main import DistilMediumServer


def _build(**kwargs):
    with (
        patch("main.get_audio_provider"),
        patch("main.WhisperStreamingEngine"),
        patch("main.WSForwarder"),
        patch("main.AudioProcessor") as processor_cls,
        patch("shared_stt.wake_word_detector.WakeWordDetector") as detector_cls,
    ):
        DistilMediumServer(model_config={}, engine_config={}, **kwargs)
    return processor_cls, detector_cls


class TestTheSettingReachesBothConstructors:
    def test_the_processor_is_off_by_default(self):
        processor_cls, _ = _build()
        assert processor_cls.call_args.kwargs["log_load_diagnostics"] is False

    def test_the_flag_reaches_the_processor(self):
        processor_cls, _ = _build(log_load_diagnostics=True)
        assert processor_cls.call_args.kwargs["log_load_diagnostics"] is True

    def test_the_flag_reaches_the_wake_word_detector(self):
        _, detector_cls = _build(log_load_diagnostics=True, wake_word_enabled=True)
        assert detector_cls.call_args.kwargs["log_load_diagnostics"] is True


class TestTheSettingIsReadFromTheTrackedConfig:
    """A misspelling on either side leaves the flag permanently false."""

    def test_config_toml_defaults_the_key_to_false(self):
        config_path = Path(distil_main.__file__).parent / "config.toml"
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        assert config["debug"]["log_load_diagnostics"] is False

    def test_the_reader_follows_the_debug_section(self):
        assert distil_main._load_diagnostics_enabled(
            {"debug": {"log_load_diagnostics": True}}) is True
        assert distil_main._load_diagnostics_enabled({"debug": {}}) is False
        assert distil_main._load_diagnostics_enabled({}) is False

    def test_main_hands_the_reader_result_to_the_server(self):
        source = Path(distil_main.__file__).read_text(encoding="utf-8")
        assert "log_load_diagnostics=_load_diagnostics_enabled(config)," in source
