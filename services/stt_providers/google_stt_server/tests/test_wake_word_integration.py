"""Tests for wake word detection integration in Google STT server.

Covers:
- Config loader: wake word config fields loaded from [wake_word] section
- Config validation: [wake_word] accepted as optional section
- Main loop integration: wake word detector processes audio when idle
- Main loop integration: forwarder.send_wake_word_detected called on detection
- Main loop integration: transcription re-enables after wake word detection
- Activate callback: correct mode filtering (idle_recovery, push_to_talk)
"""
import sys
import time
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add google_stt_server to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Config loader tests
# ---------------------------------------------------------------------------

class TestWakeWordConfigValidation:
    """[wake_word] section should be accepted as optional by validate_config_or_exit."""

    def test_wake_word_section_accepted_as_optional(self):
        """Config with [wake_word] section should not exit."""
        from config_loader import validate_config_or_exit

        config = {
            "server": {"model": "latest_short", "language_code": "en-US",
                       "enable_automatic_punctuation": False, "single_utterance": False},
            "adaptation": {"hints": [], "class_tokens": []},
            "client": {"rate": 16000, "chunk_ms": 20, "max_stream_seconds": 60.0},
            "diagnostics": {},
            "debug": {},
            "latency": {},
            "wake_word": {"enabled": True, "keyword": "computer"},
        }
        # Should not raise SystemExit
        validate_config_or_exit(config)


class TestWakeWordConfigLoading:
    """Config loader should read wake word settings into AppConfig."""

    def test_wake_word_fields_on_appconfig(self):
        """AppConfig should have wake word fields with correct defaults."""
        from config_loader import AppConfig, LatencyConfig, DebugConfig, AGCConfig

        cfg = AppConfig(
            latency=LatencyConfig(stability_commit_threshold=0.89),
            debug=DebugConfig(),
            agc=AGCConfig(),
        )
        assert cfg.wake_word_enabled is False
        assert cfg.wake_word_keyword == "computer"
        assert cfg.wake_word_sensitivity == 0.5
        assert cfg.wake_word_mode == "idle_recovery"
        assert cfg.wake_word_model_dir == "data/wake_words"

    def test_load_config_reads_wake_word_section(self):
        """load_config should populate wake word fields from [wake_word] TOML section."""
        from config_loader import load_config

        wake_word_data = {
            "enabled": True,
            "keyword": "jarvis",
            "sensitivity": 0.7,
            "mode": "push_to_talk",
            "model_dir": "/custom/models",
        }

        minimal_data = {
            "server": {"model": "latest_short", "language_code": "en-US",
                       "enable_automatic_punctuation": False, "single_utterance": False},
            "adaptation": {"hints": [], "class_tokens": []},
            "client": {"rate": 16000, "chunk_ms": 20, "max_stream_seconds": 60.0},
            "diagnostics": {},
            "debug": {},
            "latency": {},
            "wake_word": wake_word_data,
        }

        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None, wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit", return_value=minimal_data):
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        args, config = load_config()

        assert config.wake_word_enabled is True
        assert config.wake_word_keyword == "jarvis"
        assert config.wake_word_sensitivity == 0.7
        assert config.wake_word_mode == "push_to_talk"
        assert config.wake_word_model_dir == "/custom/models"

    def test_load_config_defaults_without_wake_word_section(self):
        """When [wake_word] section absent, defaults should be used."""
        from config_loader import load_config

        minimal_data = {
            "server": {"model": "latest_short", "language_code": "en-US",
                       "enable_automatic_punctuation": False, "single_utterance": False},
            "adaptation": {"hints": [], "class_tokens": []},
            "client": {"rate": 16000, "chunk_ms": 20, "max_stream_seconds": 60.0},
            "diagnostics": {},
            "debug": {},
            "latency": {},
        }

        with patch("config_loader.argparse.ArgumentParser.parse_args", return_value=MagicMock(
            list_devices=False, ws_host=None, ws_port=None,
            wake_word_enabled=False, wake_word_keyword=None,
            wake_word_sensitivity=None, wake_word_mode=None, wake_word_model_dir=None,
        )):
            with patch("config_loader.load_config_or_exit", return_value=minimal_data):
                with patch("config_loader.validate_config_or_exit"):
                    with patch("config_loader._load_hints", return_value=[]):
                        args, config = load_config()

        assert config.wake_word_enabled is False
        assert config.wake_word_keyword == "computer"
        assert config.wake_word_sensitivity == 0.5
        assert config.wake_word_mode == "idle_recovery"
        assert config.wake_word_model_dir == "data/wake_words"


# ---------------------------------------------------------------------------
# Main loop integration tests
# ---------------------------------------------------------------------------

# Reuse the fake config infrastructure from test_main.py

@dataclass
class FakeDebugConfig:
    log_lifecycle: bool = False
    log_stream_responses: bool = False
    log_frame_stats: bool = False
    log_overflow_diagnostics: bool = False
    log_load_diagnostics: bool = False


@dataclass
class FakeLatencyConfig:
    stability_commit_threshold: float = 0.9


@dataclass
class FakeAGCConfig:
    enabled: bool = False
    target_speech_rms: float = 0.1
    vad_threshold_rms: float = 0.08
    noise_floor_alpha: float = 0.1
    min_gain: float = 0.1
    max_gain: float = 10.0
    initial_noise_floor: float = 0.01


@dataclass
class FakeAppConfig:
    latency: FakeLatencyConfig = field(default_factory=FakeLatencyConfig)
    debug: FakeDebugConfig = field(default_factory=FakeDebugConfig)
    agc: FakeAGCConfig = field(default_factory=FakeAGCConfig)
    silence_finalize_ms: int = 2000
    max_no_text_seconds: float = 5.0
    forward_ws: bool = True
    ws_host: str = "localhost"
    ws_port: int = 5001
    rate: int = 16000
    chunk_ms: int = 20
    vad_lead_in_ms: int = 300
    max_stream_seconds: float = 60.0
    silero_threshold: float = 0.5
    device_index: int | None = None
    model: str = "latest_short"
    language: str = "en-US"
    auto_punct: bool = False
    single_utterance: bool = False
    phrase_hints: list = field(default_factory=list)
    class_tokens: list = field(default_factory=list)
    hints_boost: float | None = None
    mic_check_seconds: float = 0.0
    mic_check_write: str = ""
    # Wake word fields
    wake_word_enabled: bool = False
    wake_word_keyword: str = "computer"
    wake_word_sensitivity: float = 0.5
    wake_word_mode: str = "idle_recovery"
    wake_word_model_dir: str = "data/wake_words"


def make_config(**overrides) -> FakeAppConfig:
    """Create a FakeAppConfig with optional overrides."""
    cfg = FakeAppConfig()
    for key, val in overrides.items():
        setattr(cfg, key, val)
    return cfg


def make_forwarder() -> MagicMock:
    """Create a mock WSForwarder with expected methods."""
    fwd = MagicMock()
    fwd.send_stable = MagicMock()
    fwd.send_final = MagicMock()
    fwd.send_vad_start = MagicMock()
    fwd.send_notification = MagicMock()
    fwd.send_log = MagicMock()
    fwd.send_wake_word_detected = MagicMock()
    fwd.start = MagicMock()
    fwd.stop = MagicMock()
    return fwd


def real_activate_callback(mode: str, detector_is_loaded: bool = True):
    """Return main()'s own handle_wake_word_activate, plus its detector.

    The callback is nested inside main() and main() hands it to WSForwarder,
    so patching that class captures the real function. The mic read raises
    KeyboardInterrupt on its first call, which stops main() as soon as the
    callback exists; a closure outlives the call that built it, so the
    captured function still reads and writes the same cells the audio loop
    reads.
    """
    args = MagicMock()
    args.list_devices = False
    args.ws_host = None
    args.ws_port = None
    cfg = make_config(wake_word_enabled=True, wake_word_mode=mode)

    mock_detector = MagicMock()
    mock_detector.is_loaded = detector_is_loaded

    mock_mic = MagicMock()
    mock_mic.read.side_effect = KeyboardInterrupt()

    captured = {}

    def capture_ws_init(**kwargs):
        captured.update(kwargs)
        return make_forwarder()

    with ExitStack() as stack:
        stack.enter_context(patch("main.load_config", return_value=(args, cfg)))
        stack.enter_context(patch("main.get_audio_provider", return_value=mock_mic))
        stack.enter_context(patch("main.SileroVAD"))
        stack.enter_context(patch("main.SmartAGC"))
        stack.enter_context(patch("main.UsageMetrics"))
        stack.enter_context(patch("main.WSForwarder", side_effect=capture_ws_init))
        stack.enter_context(
            patch("main.get_startup_banner", return_value="Test v1.0")
        )
        stack.enter_context(patch("main.logger"))
        stack.enter_context(
            patch(
                "shared_stt.wake_word_detector.WakeWordDetector",
                return_value=mock_detector,
            )
        )
        from main import main as google_main
        try:
            google_main()
        except (KeyboardInterrupt, SystemExit):
            pass

    callback = captured.get("wake_word_activate_callback")
    assert callback is not None, (
        "main() did not hand a wake_word_activate_callback to WSForwarder"
    )
    return callback, mock_detector


def listening_state(callback) -> bool:
    """Read the callback's own wake_word_listening cell.

    The callback writes that variable with `nonlocal`, so it is a closure
    cell of main() rather than an attribute of an object. That cell is the
    value main()'s transcription-disabled branch reads before it feeds a
    frame to the detector, so reading it asserts the real listening state.
    """
    names = callback.__code__.co_freevars
    return callback.__closure__[names.index("wake_word_listening")].cell_contents


class TestWakeWordActivateCallback:
    """Test main()'s real handle_wake_word_activate callback.

    The callback is what WSForwarder calls when transcription status changes:
    - reason=None -> transcription enabled (stop listening for wake word)
    - reason="idle" -> transcription disabled after an idle pause
    - reason="manual"/"startup"/"audio"/"sonos"/"ptt" -> transcription
      disabled for the other reasons

    Which reasons arm the detector is one shared rule,
    shared_stt.wake_word_detector.should_listen_for_wake_word; the shared
    suite covers the rule itself and these tests cover this provider's use
    of it. Every test here drives the production closure: the four that
    predate wh-audio-suppression-control defined a copy of the rule inside
    the test body and asserted against the copy, so they exercised no
    provider code at all and could not see the C3 change.
    """

    def test_idle_reason_activates_in_idle_recovery_mode(self):
        """In idle_recovery mode, reason='idle' should start wake word listening."""
        callback, detector = real_activate_callback("idle_recovery")

        callback("idle")
        assert listening_state(callback) is True
        detector.reset.assert_called_once()

    @pytest.mark.parametrize("reason", ["manual", "startup", "audio", "sonos"])
    def test_every_pause_reason_activates_in_idle_recovery(self, reason):
        """wh-wake-word-stop-listening R2: the wake word ends any pause.

        idle_recovery arms on every disable reason WheelHouse sends except
        "ptt", so the wake word turns listening on whatever switched it off.
        """
        callback, detector = real_activate_callback("idle_recovery")

        callback(reason)
        assert listening_state(callback) is True
        detector.reset.assert_called_once()

    def test_ptt_reason_does_not_activate_in_idle_recovery(self):
        """A push-to-talk release is not a pause the wake word ends."""
        callback, detector = real_activate_callback("idle_recovery")

        callback("ptt")
        assert listening_state(callback) is False
        detector.reset.assert_not_called()

    def test_manual_reason_does_not_activate_in_push_to_talk(self):
        """push_to_talk keeps its own list, which names no "manual"."""
        callback, detector = real_activate_callback("push_to_talk")

        callback("manual")
        assert listening_state(callback) is False
        detector.reset.assert_not_called()

    def test_audio_reason_activates_in_push_to_talk(self):
        """In push_to_talk mode, reason='audio' should start listening."""
        callback, detector = real_activate_callback("push_to_talk")

        callback("audio")
        assert listening_state(callback) is True
        detector.reset.assert_called_once()

    def test_sonos_reason_activates_in_push_to_talk(self):
        """push_to_talk arms on every disable reason, unchanged."""
        callback, detector = real_activate_callback("push_to_talk")

        callback("sonos")
        assert listening_state(callback) is True
        detector.reset.assert_called_once()

    def test_none_reason_deactivates_listening(self):
        """reason=None (transcription enabled) should stop wake word listening."""
        callback, _detector = real_activate_callback("idle_recovery")

        callback("idle")
        assert listening_state(callback) is True
        callback(None)
        assert listening_state(callback) is False

    def test_detector_not_loaded_does_not_activate(self):
        """A provider whose detector never loaded does not start listening.

        The loaded check sits in main() ahead of the shared rule, so an idle
        pause on a machine without openWakeWord leaves the listening state
        False and resets nothing.
        """
        callback, detector = real_activate_callback(
            "idle_recovery", detector_is_loaded=False
        )

        callback("idle")
        assert listening_state(callback) is False
        detector.reset.assert_not_called()


class TestWakeWordMainLoopIntegration:
    """Test wake word processing in the main loop's transcription-disabled path.

    When transcription is disabled and wake word listening is active,
    the main loop should:
    1. Read audio frames
    2. Feed them to the wake word detector
    3. On detection: call forwarder.send_wake_word_detected()
    4. On detection: re-enable transcription and reset VAD
    """

    @patch("main.load_config")
    @patch("main.get_audio_provider")
    @patch("main.SileroVAD")
    @patch("main.SmartAGC")
    @patch("main.UsageMetrics")
    @patch("main.WSForwarder")
    @patch("main.get_startup_banner", return_value="Test v1.0")
    @patch("main.logger")
    def test_wake_word_detection_sends_event_and_reenables(
        self, mock_logger, mock_banner, mock_ws_class,
        mock_metrics, mock_agc, mock_vad,
        mock_audio, mock_load_config
    ):
        """When wake word detected while idle, should send event and re-enable transcription."""
        args = MagicMock()
        args.list_devices = False
        args.ws_host = None
        args.ws_port = None
        cfg = make_config(wake_word_enabled=True, wake_word_keyword="computer")
        mock_load_config.return_value = (args, cfg)

        mock_mic = MagicMock()
        mock_audio.return_value = mock_mic

        mock_ws_instance = make_forwarder()
        captured_callbacks = {}
        def capture_ws_init(**kwargs):
            captured_callbacks.update(kwargs)
            return mock_ws_instance
        mock_ws_class.side_effect = capture_ws_init

        # Mock the WakeWordDetector
        mock_detector = MagicMock()
        mock_detector.is_loaded = True
        mock_detector.process.return_value = "computer"  # Detected!

        iteration = 0
        transcription_event = None

        def mock_read(timeout=None):
            nonlocal iteration, transcription_event
            iteration += 1

            if iteration == 1:
                # First iteration: simulate WheelHouse sending disable with reason=idle
                # The wake_word_activate_callback should have been called by WSForwarder
                # We simulate this by calling it directly
                if "wake_word_activate_callback" in captured_callbacks and captured_callbacks["wake_word_activate_callback"]:
                    captured_callbacks["wake_word_activate_callback"]("idle")
                # Also need to clear the transcription event
                if "transcription_enabled_event" in captured_callbacks:
                    captured_callbacks["transcription_enabled_event"].clear()
                return b"\x00" * 640  # Audio frame

            if iteration == 2:
                # Second iteration: transcription should be disabled, wake word should process
                return b"\x00" * 640

            # Exit after detection
            raise KeyboardInterrupt()

        mock_mic.read.side_effect = mock_read

        with patch("shared_stt.wake_word_detector.WakeWordDetector", return_value=mock_detector) as mock_detector_class:
            from main import main
            try:
                main()
            except (KeyboardInterrupt, SystemExit):
                pass

        # Verify WSForwarder was given the wake_word_activate_callback
        assert "wake_word_activate_callback" in captured_callbacks
        assert captured_callbacks["wake_word_activate_callback"] is not None

    @patch("main.load_config")
    @patch("main.get_audio_provider")
    @patch("main.SileroVAD")
    @patch("main.SmartAGC")
    @patch("main.UsageMetrics")
    @patch("main.WSForwarder")
    @patch("main.get_startup_banner", return_value="Test v1.0")
    @patch("main.logger")
    def test_wake_word_not_created_when_disabled(
        self, mock_logger, mock_banner, mock_ws_class,
        mock_metrics, mock_agc, mock_vad,
        mock_audio, mock_load_config
    ):
        """When wake_word_enabled=False, no WakeWordDetector should be created."""
        args = MagicMock()
        args.list_devices = False
        args.ws_host = None
        args.ws_port = None
        cfg = make_config(wake_word_enabled=False)
        mock_load_config.return_value = (args, cfg)

        mock_mic = MagicMock()
        read_count = 0
        def mock_read(timeout=None):
            nonlocal read_count
            read_count += 1
            if read_count > 1:
                raise KeyboardInterrupt()
            return None
        mock_mic.read.side_effect = mock_read
        mock_audio.return_value = mock_mic

        mock_ws_instance = make_forwarder()
        mock_ws_class.return_value = mock_ws_instance

        with patch("shared_stt.wake_word_detector.WakeWordDetector") as mock_detector_class:
            from main import main
            try:
                main()
            except (KeyboardInterrupt, SystemExit):
                pass

            # WakeWordDetector should NOT have been instantiated
            mock_detector_class.assert_not_called()
