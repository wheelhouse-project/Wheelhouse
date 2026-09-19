"""Tests for the distil provider's apply_engine_settings handler
(wh-7ou.7.1.3, spec Sections 5.3 and 8).

The handler validates against the two-key allowed list, writes the values
into its own config.toml [engine] section, replies engine_settings_result
BEFORE restarting, and performs the same flag-file-plus-exit hard restart
the add-hint feature uses. A validation or write failure replies ok=false
and does NOT restart, so the calibration window can offer Apply again.

The distil service has no venv-importable test harness of its own; the
module is loaded here by file path (unique module name, no sys.modules
collision with other providers' main.py).
"""
import importlib.util
import sys
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_DISTIL_MAIN = (
    Path(__file__).parent.parent.parent / "distil_medium_en" / "main.py"
)
_spec = importlib.util.spec_from_file_location(
    "distil_medium_en_main_for_tests", _DISTIL_MAIN
)
distil_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(distil_main)


CONFIG_FIXTURE = """\
# Distil-Whisper Medium.en STT (GPU)

[model]
device = "cuda"

[engine]
# hallucination filter comment that must survive
hallucination_logprob_threshold = -0.55
# single-word rescue comment that must survive
single_word_min_probability = 0.6
single_word_max_no_speech_prob = 0.03

[client]
rate = 16000
"""


class FakeForwarder:
    """Records send calls with a shared order list so the tests can assert
    the reply-before-restart ordering."""

    def __init__(self, order):
        self.order = order
        self.results = []
        self.result_ids = []
        self.notifications = []

    def send_engine_settings_result(self, ok, error=None, apply_id=None):
        self.results.append((ok, error))
        self.result_ids.append(apply_id)
        self.order.append(("result", ok, error))

    def send_notification(self, title, message, kind=""):
        self.notifications.append((title, message))
        self.order.append(("notification", title, message))


def make_server(tmp_path, config_text=CONFIG_FIXTURE, flag_write_ok=True):
    """Build a DistilMediumServer without running __init__ (no model, no
    audio device); set only what the handler under test touches."""
    server = object.__new__(distil_main.DistilMediumServer)
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(config_text.encode("utf-8"))
    server._config_path = config_path

    order = []
    server.forwarder = FakeForwarder(order)
    server._test_order = order

    def fake_write_restart_flag():
        order.append("flag")
        return flag_write_ok

    def fake_stop():
        order.append("stop")

    server._write_restart_flag = fake_write_restart_flag
    server.stop = fake_stop
    return server


def parse_config(server) -> dict:
    with open(server._config_path, "rb") as f:
        return tomllib.load(f)


class TestApplySuccess:
    def test_valid_settings_written_and_ok_reply(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings({
            "single_word_min_probability": 0.15,
            "single_word_max_no_speech_prob": 0.034,
        })
        engine = parse_config(server)["engine"]
        assert engine["single_word_min_probability"] == 0.15
        assert engine["single_word_max_no_speech_prob"] == 0.034
        assert server.forwarder.results == [(True, None)]

    def test_reply_goes_out_after_flag_and_before_restart(self, tmp_path):
        """The ok reply is sent only once the restart flag is durably on
        disk, so ok=true always means a restart is actually coming
        (review finding wh-7ou.7.6.7); the reply still precedes stop so
        it goes out before the process exits."""
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        assert server._test_order == ["flag", ("result", True, None), "stop"]

    def test_comments_and_other_keys_survive(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        content = server._config_path.read_bytes().decode("utf-8")
        assert "# hallucination filter comment that must survive" in content
        assert "# single-word rescue comment that must survive" in content
        config = parse_config(server)
        assert config["engine"]["hallucination_logprob_threshold"] == -0.55
        assert config["engine"]["single_word_max_no_speech_prob"] == 0.03
        assert config["client"]["rate"] == 16000

    def test_write_or_keep_absent_key_untouched(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"single_word_max_no_speech_prob": 0.030}
        )
        engine = parse_config(server)["engine"]
        assert engine["single_word_min_probability"] == 0.6
        assert engine["single_word_max_no_speech_prob"] == 0.030

    def test_failed_flag_write_reports_failure_without_exit(self, tmp_path):
        """Flag-write failure: the values are on disk but no restart is
        coming, so reply ok=false with honest error text -- the window
        shows the failure and offers Apply again -- and keep running
        (exit-without-flag reads as a clean shutdown and leaves STT
        dead). Review finding wh-7ou.7.6.7: replying ok=true here left
        the calibration window waiting for a restart that never came."""
        server = make_server(tmp_path, flag_write_ok=False)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        assert len(server.forwarder.results) == 1
        ok, error = server.forwarder.results[0]
        assert ok is False
        assert "saved" in error and "restart" in error
        assert "stop" not in server._test_order


class TestApplyRejection:
    def test_unknown_key_rejected_nothing_written(self, tmp_path):
        server = make_server(tmp_path)
        original = server._config_path.read_bytes()
        server._handle_apply_engine_settings({
            "single_word_min_probability": 0.15,
            "hallucination_logprob_threshold": -0.9,
        })
        assert server._config_path.read_bytes() == original
        assert len(server.forwarder.results) == 1
        ok, error = server.forwarder.results[0]
        assert ok is False
        assert "hallucination_logprob_threshold" in error
        assert "flag" not in server._test_order
        assert "stop" not in server._test_order

    @pytest.mark.parametrize(
        "bad",
        [True, False, "0.5", None, float("nan"), float("inf"), -0.1, 1.5],
    )
    def test_bad_value_rejected_nothing_written(self, tmp_path, bad):
        server = make_server(tmp_path)
        original = server._config_path.read_bytes()
        server._handle_apply_engine_settings(
            {"single_word_min_probability": bad}
        )
        assert server._config_path.read_bytes() == original
        assert len(server.forwarder.results) == 1
        assert server.forwarder.results[0][0] is False
        assert server.forwarder.results[0][1]
        assert "flag" not in server._test_order
        assert "stop" not in server._test_order

    def test_empty_settings_rejected_without_restart(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings({})
        assert len(server.forwarder.results) == 1
        assert server.forwarder.results[0][0] is False
        assert "stop" not in server._test_order


class TestWriteFailure:
    def test_write_failure_replies_error_and_does_not_restart(self, tmp_path):
        """Spec Section 8: config write failure -> engine_settings_result
        failure with the exact error text, no restart, nothing changed."""
        server = make_server(tmp_path)
        # Point at a file that does not exist: the read side of the write
        # helper raises, which is the same handler path as a locked or
        # unwritable file.
        server._config_path = tmp_path / "missing_dir" / "config.toml"
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        assert len(server.forwarder.results) == 1
        ok, error = server.forwarder.results[0]
        assert ok is False
        assert error  # the exact OS error text, verbatim
        assert "flag" not in server._test_order
        assert "stop" not in server._test_order


class TestApplyIdEcho:
    """wh-7ou.7.6.9: every engine_settings_result echoes the apply_id
    that rode in on the command, so WheelHouse can correlate the reply
    to the exact apply it answers -- client identity alone cannot
    distinguish two applies on the same live connection. All four reply
    branches echo: success, validation failure, write failure, and
    flag-write failure."""

    def test_success_reply_echoes_the_apply_id(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}, "op-success",
        )
        assert server.forwarder.results == [(True, None)]
        assert server.forwarder.result_ids == ["op-success"]

    def test_validation_failure_reply_echoes_the_apply_id(self, tmp_path):
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"hallucination_logprob_threshold": -0.9}, "op-rejected",
        )
        assert server.forwarder.results[0][0] is False
        assert server.forwarder.result_ids == ["op-rejected"]

    def test_write_failure_reply_echoes_the_apply_id(self, tmp_path):
        server = make_server(tmp_path)
        server._config_path = tmp_path / "missing_dir" / "config.toml"
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}, "op-write-failed",
        )
        assert server.forwarder.results[0][0] is False
        assert server.forwarder.result_ids == ["op-write-failed"]

    def test_flag_failure_reply_echoes_the_apply_id(self, tmp_path):
        server = make_server(tmp_path, flag_write_ok=False)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}, "op-flag-failed",
        )
        assert server.forwarder.results[0][0] is False
        assert server.forwarder.result_ids == ["op-flag-failed"]

    def test_reply_without_an_id_echoes_none(self, tmp_path):
        """A command with no apply_id still gets a reply; it just
        carries no correlation id."""
        server = make_server(tmp_path)
        server._handle_apply_engine_settings(
            {"single_word_min_probability": 0.15}
        )
        assert server.forwarder.results == [(True, None)]
        assert server.forwarder.result_ids == [None]


class TestForwarderWiring:
    def test_callbacks_wired_into_forwarder(self, monkeypatch):
        """The command only works if main.py registers the callbacks: the
        engine's set_calibration_mode (left unwired by wh-7ou.7.1.2, whose
        report flagged it for this task) and the apply handler."""
        monkeypatch.setattr(distil_main, "WhisperStreamingEngine", Mock())
        monkeypatch.setattr(distil_main, "get_audio_provider", Mock())
        server = distil_main.DistilMediumServer(
            model_config={}, engine_config={}, ws_port=59999
        )
        assert (
            server.forwarder.set_calibration_mode_callback
            is server.engine.set_calibration_mode
        )
        assert (
            server.forwarder.apply_engine_settings_callback
            == server._handle_apply_engine_settings
        )

    def test_config_path_points_at_provider_config(self, monkeypatch):
        """The handler must write the same file load_config reads."""
        monkeypatch.setattr(distil_main, "WhisperStreamingEngine", Mock())
        monkeypatch.setattr(distil_main, "get_audio_provider", Mock())
        server = distil_main.DistilMediumServer(
            model_config={}, engine_config={}, ws_port=59999
        )
        assert server._config_path == _DISTIL_MAIN.parent / "config.toml"
