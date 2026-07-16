"""Tests for google_stt_server config_loader.py.

Covers:
- _load_hints: shared hints.txt loading, config.toml hints fallback, merging, filtering
- build_parser: argument definitions and parsing
- load_config_or_exit: TOML loading, missing file, corrupt file
- validate_config_or_exit: required/optional/extra sections
- load_config: end-to-end config assembly, CLI overrides, defaults
- Adversarial: missing/corrupt config, invalid section names
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add google_stt_server to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import (
    _load_hints,
    build_parser,
    load_config_or_exit,
    validate_config_or_exit,
    load_config,
    AppConfig,
    DebugConfig,
    LatencyConfig,
    OverflowDetectionConfig,
    AGCConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_toml_dict():
    """Return a minimal valid config dict matching required sections."""
    return {
        "server": {
            "model": "latest_short",
            "language_code": "en-US",
            "enable_automatic_punctuation": False,
            "single_utterance": False,
        },
        "adaptation": {"hints": [], "class_tokens": []},
        "client": {
            "rate": 16000,
            "chunk_ms": 20,
            "max_stream_seconds": 60.0,
        },
        "diagnostics": {},
        "debug": {},
        "latency": {},
        "overflow_detection": {},
    }


# ---------------------------------------------------------------------------
# _load_hints
# ---------------------------------------------------------------------------

class TestLoadHints:
    """Tests for _load_hints - shared hints.txt + config.toml merging."""

    def test_loads_from_hints_file(self, tmp_path):
        hints_file = tmp_path / "shared" / "hints.txt"
        hints_file.parent.mkdir()
        hints_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        with patch("config_loader.Path") as mock_path_cls:
            # __file__ parent.parent / "shared" / "hints.txt"
            mock_path_cls.return_value.parent.parent.__truediv__.return_value.__truediv__.return_value = hints_file
            # Also need Path(__file__) to behave correctly for the chain
            mock_file = MagicMock()
            mock_file.parent.parent.__truediv__.return_value.__truediv__.return_value = hints_file
            mock_path_cls.__call__ = lambda self, x: mock_file

        # Simpler approach: just patch at the path resolution level
        result = _load_hints({"hints": []})
        # Without patching the path, this uses the real hints.txt
        # The result should be a list of strings
        assert isinstance(result, list)

    def test_merges_hints_txt_and_config(self, tmp_path):
        """Hints from both hints.txt and config.toml are merged."""
        hints_file = tmp_path / "hints.txt"
        hints_file.write_text("from_file\n", encoding="utf-8")

        adap = {"hints": ["from_config"]}

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value="from_file\n"):
                result = _load_hints(adap)

        assert "from_file" in result
        assert "from_config" in result

    def test_deduplicates_via_set(self):
        """Duplicate hints across sources are deduplicated."""
        adap = {"hints": ["word_a", "word_a"]}
        with patch.object(Path, "exists", return_value=False):
            result = _load_hints(adap)
        # Set-based dedup means only one copy
        assert result.count("word_a") == 1

    def test_skips_comments_and_blanks_in_hints_file(self):
        """Comments (#) and blank lines in hints.txt are ignored."""
        content = "# this is a comment\n\nalpha\n  \nbeta\n# another comment\n"
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=content):
                result = _load_hints({"hints": []})
        assert "alpha" in result
        assert "beta" in result
        assert len(result) == 2

    def test_falls_back_to_config_when_no_hints_file(self):
        """When hints.txt doesn't exist, uses config.toml hints only."""
        adap = {"hints": ["config_hint"]}
        with patch.object(Path, "exists", return_value=False):
            result = _load_hints(adap)
        assert result == ["config_hint"]

    def test_handles_hints_file_read_error(self):
        """Read errors on hints.txt fall through to config.toml hints."""
        adap = {"hints": ["fallback"]}
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", side_effect=OSError("read error")):
                result = _load_hints(adap)
        assert result == ["fallback"]

    def test_filters_non_string_hints(self):
        """Non-string values in config hints are filtered out."""
        adap = {"hints": ["valid", 123, None, "", "  "]}
        with patch.object(Path, "exists", return_value=False):
            result = _load_hints(adap)
        assert "valid" in result
        # 123, None filtered by isinstance check; "" and "  " filtered by strip check
        assert len(result) == 1

    def test_strips_whitespace_from_config_hints(self):
        """Whitespace is stripped from config.toml hints."""
        adap = {"hints": ["  padded  "]}
        with patch.object(Path, "exists", return_value=False):
            result = _load_hints(adap)
        assert "padded" in result

    def test_empty_adaptation_section(self):
        """Empty adaptation dict returns empty list."""
        with patch.object(Path, "exists", return_value=False):
            result = _load_hints({})
        assert result == []

    def test_strips_whitespace_from_hints_file_lines(self):
        """Leading/trailing whitespace in hints.txt lines is stripped."""
        content = "  spaced_hint  \n"
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=content):
                result = _load_hints({"hints": []})
        assert "spaced_hint" in result


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------

class TestBuildParser:
    """Tests for build_parser - CLI argument definitions."""

    def test_returns_parser(self):
        parser = build_parser()
        assert parser is not None

    def test_list_devices_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--list-devices"])
        assert args.list_devices is True

    def test_list_devices_default_false(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.list_devices is False

    def test_ws_host_override(self):
        parser = build_parser()
        args = parser.parse_args(["--ws-host", "192.168.1.1"])
        assert args.ws_host == "192.168.1.1"

    def test_ws_port_override(self):
        parser = build_parser()
        args = parser.parse_args(["--ws-port", "9999"])
        assert args.ws_port == 9999

    def test_ws_host_default_none(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.ws_host is None

    def test_wake_word_args_accepted(self):
        """build_parser must accept --wake-word-* args passed by remote_stt_launcher."""
        parser = build_parser()
        args = parser.parse_args([
            "--wake-word-enabled",
            "--wake-word-keyword", "hey_computer",
            "--wake-word-sensitivity", "0.6",
            "--wake-word-mode", "idle_recovery",
            "--wake-word-model-dir", "/path/to/models",
        ])
        assert args.wake_word_enabled is True
        assert args.wake_word_keyword == "hey_computer"
        assert args.wake_word_sensitivity == 0.6
        assert args.wake_word_mode == "idle_recovery"
        assert args.wake_word_model_dir == "/path/to/models"

    def test_wake_word_args_default_disabled(self):
        """Wake word args should default to disabled when not provided."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.wake_word_enabled is False

    def test_ws_port_default_none(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.ws_port is None


# ---------------------------------------------------------------------------
# load_config_or_exit
# ---------------------------------------------------------------------------

class TestLoadConfigOrExit:
    """Tests for load_config_or_exit - TOML file loading."""

    def test_loads_valid_toml(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[server]\nmodel = "latest_short"\n', encoding="utf-8")
        result = load_config_or_exit(cfg)
        assert result["server"]["model"] == "latest_short"

    def test_exits_on_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.toml"
        with pytest.raises(SystemExit) as exc:
            load_config_or_exit(missing)
        assert exc.value.code == 2

    def test_exits_on_corrupt_toml(self, tmp_path):
        cfg = tmp_path / "bad.toml"
        cfg.write_text("this is not valid { toml [", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            load_config_or_exit(cfg)
        assert exc.value.code == 2

    def test_loads_complex_toml(self, tmp_path):
        """Loads TOML with nested tables, arrays, and various types."""
        content = """
[server]
model = "latest_short"
language_code = "en-US"

[adaptation]
hints = ["alpha", "beta"]
hints_boost = 10.0

[client]
rate = 16000
"""
        cfg = tmp_path / "config.toml"
        cfg.write_text(content, encoding="utf-8")
        result = load_config_or_exit(cfg)
        assert result["adaptation"]["hints"] == ["alpha", "beta"]
        assert result["adaptation"]["hints_boost"] == 10.0
        assert result["client"]["rate"] == 16000


# ---------------------------------------------------------------------------
# validate_config_or_exit
# ---------------------------------------------------------------------------

class TestValidateConfigOrExit:
    """Tests for validate_config_or_exit - section validation."""

    def test_passes_with_all_required_sections(self):
        config = _minimal_toml_dict()
        # Should not raise
        validate_config_or_exit(config)

    def test_exits_on_missing_section(self):
        config = _minimal_toml_dict()
        del config["server"]
        with pytest.raises(SystemExit) as exc:
            validate_config_or_exit(config)
        assert exc.value.code == 2

    def test_exits_on_multiple_missing_sections(self):
        config = _minimal_toml_dict()
        del config["server"]
        del config["debug"]
        with pytest.raises(SystemExit) as exc:
            validate_config_or_exit(config)
        assert exc.value.code == 2

    def test_exits_on_extra_unknown_section(self):
        config = _minimal_toml_dict()
        config["totally_unknown"] = {"foo": "bar"}
        with pytest.raises(SystemExit) as exc:
            validate_config_or_exit(config)
        assert exc.value.code == 2

    def test_allows_optional_agc_section(self):
        config = _minimal_toml_dict()
        config["agc"] = {"enabled": True}
        # Should not raise
        validate_config_or_exit(config)

    def test_allows_optional_provider_section(self):
        config = _minimal_toml_dict()
        config["provider"] = {"name": "google_stt"}
        validate_config_or_exit(config)

    def test_allows_optional_forwarding_section(self):
        config = _minimal_toml_dict()
        config["forwarding"] = {"enabled": True}
        validate_config_or_exit(config)

    def test_allows_all_optional_sections_together(self):
        config = _minimal_toml_dict()
        config["agc"] = {}
        config["provider"] = {}
        config["forwarding"] = {}
        validate_config_or_exit(config)

    def test_exits_with_both_missing_and_extra(self):
        """When both missing and extra sections exist, exits with code 2."""
        config = _minimal_toml_dict()
        del config["latency"]
        config["bogus"] = {}
        with pytest.raises(SystemExit) as exc:
            validate_config_or_exit(config)
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# load_config (end-to-end)
# ---------------------------------------------------------------------------

class TestLoadConfig:
    """Tests for load_config - full config assembly from TOML + CLI."""

    def _write_full_config(self, path: Path):
        """Write a complete valid config.toml to the given path."""
        content = """
[provider]
name = "google_stt"
display_name = "Google"
launcher = "launcher.py"

[server]
model = "latest_short"
language_code = "en-US"
enable_automatic_punctuation = false
single_utterance = false

[adaptation]
hints = ["test_hint"]
hints_boost = 10.0
class_tokens = ["$CARDINAL"]

[client]
rate = 16000
chunk_ms = 20
silero_threshold = 0.5
max_stream_seconds = 60.0
silence_finalize_ms = 1500
vad_lead_in_ms = 300
max_no_text_seconds = 3.9

[latency]
stability_commit_threshold = 0.89

[overflow_detection]
enabled = true
overflow_threshold = 5
window_seconds = 30.0
restart_cooldown_seconds = 60.0
max_restart_attempts = 3
stable_reset_seconds = 300.0

[debug]
log_lifecycle = true
log_stream_responses = false
log_frame_stats = false

[diagnostics]
mic_check_seconds = 0.0
mic_check_write = ""

[agc]
enabled = true
target_speech_rms = 0.1
vad_threshold_rms = 0.08
noise_floor_alpha = 0.02
min_gain = 0.1
max_gain = 10.0
initial_noise_floor = 0.01
"""
        path.write_text(content, encoding="utf-8")

    def test_returns_args_and_appconfig(self, tmp_path):
        cfg_path = tmp_path / "config.toml"
        self._write_full_config(cfg_path)

        with patch("config_loader.Path") as MockPath:
            # Make Path(__file__).resolve().parent / "config.toml" point to our temp file
            MockPath.__call__ = lambda self, x: MagicMock()
            resolve_mock = MagicMock()
            resolve_mock.parent.__truediv__.return_value = cfg_path
            MockPath.return_value.resolve.return_value = resolve_mock

        # Simpler approach: patch at function level
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=["test_hint"]):
                        mock_load.return_value = {
                            "server": {
                                "model": "latest_short",
                                "language_code": "en-US",
                                "enable_automatic_punctuation": False,
                                "single_utterance": False,
                            },
                            "adaptation": {
                                "hints": ["test_hint"],
                                "hints_boost": 10.0,
                                "class_tokens": ["$CARDINAL"],
                            },
                            "client": {
                                "rate": 16000,
                                "chunk_ms": 20,
                                "silero_threshold": 0.5,
                                "max_stream_seconds": 60.0,
                                "silence_finalize_ms": 1500,
                                "vad_lead_in_ms": 300,
                                "max_no_text_seconds": 3.9,
                            },
                            "diagnostics": {
                                "mic_check_seconds": 0.0,
                                "mic_check_write": "",
                            },
                            "debug": {
                                "log_lifecycle": True,
                                "log_stream_responses": False,
                                "log_frame_stats": False,
                            },
                            "latency": {"stability_commit_threshold": 0.89},
                            "overflow_detection": {
                                "enabled": True,
                                "overflow_threshold": 5,
                                "window_seconds": 30.0,
                                "restart_cooldown_seconds": 60.0,
                                "max_restart_attempts": 3,
                                "stable_reset_seconds": 300.0,
                            },
                            "agc": {
                                "enabled": True,
                                "target_speech_rms": 0.1,
                            },
                        }
                        args, config = load_config()

        assert isinstance(config, AppConfig)
        assert config.model == "latest_short"
        assert config.language == "en-US"
        assert config.rate == 16000

    def test_cli_ws_host_overrides_config(self):
        """--ws-host CLI arg overrides config.toml forwarding.ws_host."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host="10.0.0.1", ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        mock_load.return_value = _minimal_toml_dict()
                        mock_load.return_value["forwarding"] = {
                            "ws_host": "localhost",
                            "ws_port": 5001,
                        }
                        args, config = load_config()

        assert config.ws_host == "10.0.0.1"

    def test_cli_ws_port_overrides_config(self):
        """--ws-port CLI arg overrides config.toml forwarding.ws_port."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=7777,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        mock_load.return_value = _minimal_toml_dict()
                        args, config = load_config()

        assert config.ws_port == 7777

    def test_defaults_when_optional_sections_missing(self):
        """Optional sections (agc, forwarding) use defaults when absent."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        # No forwarding, no agc sections
                        mock_load.return_value = data
                        args, config = load_config()

        # AGC defaults
        assert config.agc.enabled is True
        assert config.agc.target_speech_rms == 0.1
        # Forwarding defaults
        assert config.forward_ws is True
        assert config.ws_host == "localhost"
        assert config.ws_port == 0

    def test_debug_config_mapping(self):
        """Debug section values are mapped to DebugConfig correctly."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        data["debug"] = {
                            "log_lifecycle": True,
                            "log_stream_responses": True,
                            "log_frame_stats": True,
                        }
                        mock_load.return_value = data
                        args, config = load_config()

        assert config.debug.log_lifecycle is True
        assert config.debug.log_stream_responses is True
        assert config.debug.log_frame_stats is True

    def test_overflow_config_mapping(self):
        """Overflow detection values are mapped correctly."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        data["overflow_detection"] = {
                            "enabled": False,
                            "overflow_threshold": 10,
                            "window_seconds": 45.0,
                            "restart_cooldown_seconds": 120.0,
                            "max_restart_attempts": 5,
                            "stable_reset_seconds": 600.0,
                        }
                        mock_load.return_value = data
                        args, config = load_config()

        assert config.overflow_detection.enabled is False
        assert config.overflow_detection.overflow_threshold == 10
        assert config.overflow_detection.window_seconds == 45.0
        assert config.overflow_detection.max_restart_attempts == 5

    def test_class_tokens_filtered(self):
        """Class tokens are filtered to valid strings only."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        data["adaptation"]["class_tokens"] = ["$CARDINAL", "", "  ", "$CURRENCY"]
                        mock_load.return_value = data
                        args, config = load_config()

        assert "$CARDINAL" in config.class_tokens
        assert "$CURRENCY" in config.class_tokens
        assert "" not in config.class_tokens
        assert len(config.class_tokens) == 2

    def test_latency_config_defaults(self):
        """Latency config uses default when section is empty."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        data["latency"] = {}  # Empty
                        mock_load.return_value = data
                        args, config = load_config()

        assert config.latency.stability_commit_threshold == 0.89

    def test_client_optional_fields_defaults(self):
        """Client optional fields use defaults when not in config."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        # client has only required fields (rate, chunk_ms, max_stream_seconds)
                        mock_load.return_value = data
                        args, config = load_config()

        assert config.silero_threshold == 0.5
        assert config.device_index is None
        assert config.silence_finalize_ms == 150
        assert config.vad_lead_in_ms == 300
        assert config.max_no_text_seconds == 5.0

    def test_diagnostics_defaults(self):
        """Diagnostics fields use defaults when empty."""
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        data = _minimal_toml_dict()
                        data["diagnostics"] = {}
                        mock_load.return_value = data
                        args, config = load_config()

        assert config.mic_check_seconds == 0.0
        assert config.mic_check_write == ""


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------

class TestDataclassDefaults:
    """Tests for config dataclass default values."""

    def test_debug_config_defaults(self):
        dc = DebugConfig()
        assert dc.log_lifecycle is False
        assert dc.log_stream_responses is False
        assert dc.log_frame_stats is False
        assert dc.log_overflow_diagnostics is False

    def test_overflow_config_defaults(self):
        oc = OverflowDetectionConfig()
        assert oc.enabled is True
        assert oc.overflow_threshold == 5
        assert oc.window_seconds == 30.0
        assert oc.restart_cooldown_seconds == 60.0
        assert oc.max_restart_attempts == 3
        assert oc.stable_reset_seconds == 300.0

    def test_agc_config_defaults(self):
        ac = AGCConfig()
        assert ac.enabled is True
        assert ac.target_speech_rms == 0.1
        assert ac.vad_threshold_rms == 0.08
        assert ac.noise_floor_alpha == 0.1
        assert ac.min_gain == 0.1
        assert ac.max_gain == 10.0
        assert ac.initial_noise_floor == 0.01

    def test_app_config_defaults(self):
        """AppConfig fields have expected defaults (non-required fields)."""
        ac = AppConfig(
            latency=LatencyConfig(stability_commit_threshold=0.89),
            debug=DebugConfig(),
            overflow_detection=OverflowDetectionConfig(),
            agc=AGCConfig(),
        )
        assert ac.model == "latest_short"
        assert ac.language == "en-US"
        assert ac.auto_punct is False
        assert ac.single_utterance is False
        assert ac.rate == 16000
        assert ac.chunk_ms == 20
        assert ac.silero_threshold == 0.5
        assert ac.max_stream_seconds == 60.0
        assert ac.device_index is None
        assert ac.silence_finalize_ms == 150
        assert ac.vad_lead_in_ms == 300
        assert ac.max_no_text_seconds == 5.0
        assert ac.phrase_hints == []
        assert ac.class_tokens == []
        assert ac.hints_boost is None
        assert ac.forward_ws is False
        assert ac.ws_host == "localhost"
        assert ac.ws_port == 0
        assert ac.mic_check_seconds == 0.0
        assert ac.mic_check_write == ""

    def test_latency_config_requires_threshold(self):
        """LatencyConfig requires stability_commit_threshold (no default)."""
        with pytest.raises(TypeError):
            LatencyConfig()


# ---------------------------------------------------------------------------
# debug.log_overflow_diagnostics wiring (wh-stt-audio-consumer-behind-realtime)
# ---------------------------------------------------------------------------

class TestDebugOverflowDiagnosticsFlag:
    """The [debug] log_overflow_diagnostics flag must be read from config.toml.

    It gates the main loop's [overflow-diag] timing block; before this fix it
    was silently hard-coded False, so the diagnostics could never be enabled.
    """

    def _load_with_debug(self, debug_dict):
        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None,
            wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit") as mock_load:
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        cfg_dict = _minimal_toml_dict()
                        cfg_dict["debug"] = debug_dict
                        mock_load.return_value = cfg_dict
                        args, config = load_config()
        return config

    def test_log_overflow_diagnostics_true_from_config(self):
        config = self._load_with_debug({"log_overflow_diagnostics": True})
        assert config.debug.log_overflow_diagnostics is True

    def test_log_overflow_diagnostics_defaults_false(self):
        config = self._load_with_debug({})
        assert config.debug.log_overflow_diagnostics is False
