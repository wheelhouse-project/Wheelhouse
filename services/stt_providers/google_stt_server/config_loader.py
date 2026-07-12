"""
This module handles the loading and validation of the application's configuration
from a TOML file. It defines the data structures for the configuration,
parses command-line arguments, and ensures that the configuration is valid
before the application starts.
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict
import tomllib


def _load_hints(adap: Dict[str, Any]) -> list[str]:
    """Load hints from shared hints.txt file, merging with any config.toml hints.

    Reads from services/shared/hints.txt (shared across all STT providers).
    Falls back to config.toml hints if shared file doesn't exist.

    Args:
        adap: The [adaptation] section from config.toml

    Returns:
        List of hint phrases
    """
    hints = set()

    # Load from shared hints.txt file
    hints_path = Path(__file__).parent.parent / "shared" / "hints.txt"
    if hints_path.exists():
        try:
            content = hints_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    hints.add(line)
        except Exception:
            pass  # Fall through to config.toml hints

    # Also include any hints from config.toml (for backwards compatibility)
    for hint in adap.get("hints", []):
        if isinstance(hint, str) and hint.strip():
            hints.add(hint.strip())

    return list(hints)


@dataclass
class OverflowDetectionConfig:
    """Config for overflow detection and automatic restart."""
    enabled: bool = True
    overflow_threshold: int = 5
    window_seconds: float = 30.0
    restart_cooldown_seconds: float = 60.0
    max_restart_attempts: int = 3
    stable_reset_seconds: float = 300.0


@dataclass
class DebugConfig:
    """Granular debugging flags."""
    log_lifecycle: bool = False
    log_stream_responses: bool = False
    log_frame_stats: bool = False
    log_overflow_diagnostics: bool = False


@dataclass
class LatencyConfig:
    """Config for latency optimization."""
    stability_commit_threshold: float  # Google sends 0.8999999762 for stable, ~0.01 for unstable


@dataclass
class AGCConfig:
    """Config for automatic gain control."""
    enabled: bool = True
    target_speech_rms: float = 0.1
    vad_threshold_rms: float = 0.08
    noise_floor_alpha: float = 0.1
    min_gain: float = 0.1
    max_gain: float = 10.0
    initial_noise_floor: float = 0.01


@dataclass
class AppConfig:
    """Strongly-typed application configuration."""
    # Required configuration objects
    latency: "LatencyConfig"
    debug: DebugConfig
    overflow_detection: OverflowDetectionConfig
    agc: "AGCConfig"
    
    # Server
    model: str = "latest_short"
    language: str = "en-US"
    auto_punct: bool = False
    single_utterance: bool = False
    
    # Client
    rate: int = 16000
    chunk_ms: int = 20
    silero_threshold: float = 0.5
    max_stream_seconds: float = 60.0
    device_index: int | None = None
    silence_finalize_ms: int = 150
    vad_lead_in_ms: int = 300
    max_no_text_seconds: float = 5.0
    
    # Adaptation
    phrase_hints: list[str] = field(default_factory=list)
    class_tokens: list[str] = field(default_factory=list)
    hints_boost: float | None = None
    
    # Forwarding
    forward_ws: bool = False
    ws_host: str = "localhost"
    ws_port: int = 0
    
    # Diagnostics
    mic_check_seconds: float = 0.0
    mic_check_write: str = ""

    # Wake word detection
    wake_word_enabled: bool = False
    wake_word_keyword: str = "computer"
    wake_word_sensitivity: float = 0.5
    wake_word_mode: str = "idle_recovery"
    wake_word_model_dir: str = "data/wake_words"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Google Cloud STT real-time tuner (config-driven)")
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    p.add_argument("--ws-host", default=None, help="WebSocket host for forwarding (overrides config)")
    p.add_argument("--ws-port", type=int, default=None, help="WebSocket port for forwarding (overrides config)")
    # Wake word args passed by remote_stt_launcher (override config.toml [wake_word])
    p.add_argument("--wake-word-enabled", action="store_true", default=False, help="Enable wake word detection")
    p.add_argument("--wake-word-keyword", default=None, help="Wake word keyword")
    p.add_argument("--wake-word-sensitivity", type=float, default=None, help="Wake word sensitivity")
    p.add_argument("--wake-word-mode", default=None, help="Wake word mode (idle_recovery, push_to_talk)")
    p.add_argument("--wake-word-model-dir", default=None, help="Path to wake word model directory")
    return p


def load_config_or_exit(path: Path) -> Dict[str, Any]:
    if not path.exists():
        print(f"[config][ERROR] Missing config file: {path}")
        sys.exit(2)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"[config][ERROR] Failed to parse TOML: {e}")
        sys.exit(2)


def validate_config_or_exit(c: Dict[str, Any]) -> None:
    required_sections = {"server", "adaptation", "client", "diagnostics", "debug", "latency", "overflow_detection"}
    optional_sections = {"agc", "provider", "forwarding", "wake_word"}  # AGC optional for backward compat, provider for discovery, forwarding deprecated (use CLI args)
    missing = required_sections - set(c.keys())
    extra = set(c.keys()) - required_sections - optional_sections

    if missing or extra:
        if missing:
            print(f"[config][ERROR] Missing sections: {sorted(missing)}")
        if extra:
            print(f"[config][ERROR] Unknown top-level sections: {sorted(extra)}")
        sys.exit(2)

    cli = c["client"]
    # Validation checks removed - vad_commit_window_ms and vad_commit_threshold_ratio are no longer used

def load_config() -> tuple[argparse.Namespace, AppConfig]:
    """Loads all configuration from command line and config.toml."""
    parser = build_parser()
    args = parser.parse_args()

    cfg_path = Path(__file__).resolve().parent / "config.toml"
    data = load_config_or_exit(cfg_path)
    validate_config_or_exit(data)

    srv = data["server"]
    adap = data["adaptation"]
    cli = data["client"]
    fwd = data.get("forwarding", {})  # Optional - ws_host/ws_port can come from CLI args
    diag = data["diagnostics"]
    dbg = data["debug"]
    lat = data.get("latency", {})
    overflow = data.get("overflow_detection", {})

    debug_config = DebugConfig(
        log_lifecycle=dbg.get("log_lifecycle", False),
        log_stream_responses=dbg.get("log_stream_responses", False),
        log_frame_stats=dbg.get("log_frame_stats", False),
    )

    latency_config = LatencyConfig(
        stability_commit_threshold=lat.get("stability_commit_threshold", 0.89),
    )

    overflow_config = OverflowDetectionConfig(
        enabled=overflow.get("enabled", True),
        overflow_threshold=overflow.get("overflow_threshold", 5),
        window_seconds=overflow.get("window_seconds", 30.0),
        restart_cooldown_seconds=overflow.get("restart_cooldown_seconds", 60.0),
        max_restart_attempts=overflow.get("max_restart_attempts", 3),
        stable_reset_seconds=overflow.get("stable_reset_seconds", 300.0),
    )

    agc_data = data.get("agc", {})
    agc_config = AGCConfig(
        enabled=agc_data.get("enabled", True),
        target_speech_rms=agc_data.get("target_speech_rms", 0.1),
        vad_threshold_rms=agc_data.get("vad_threshold_rms", 0.08),
        noise_floor_alpha=agc_data.get("noise_floor_alpha", 0.1),
        min_gain=agc_data.get("min_gain", 0.1),
        max_gain=agc_data.get("max_gain", 10.0),
        initial_noise_floor=agc_data.get("initial_noise_floor", 0.01),
    )

    ww = data.get("wake_word", {})

    app_config = AppConfig(
        latency=latency_config,
        debug=debug_config,
        overflow_detection=overflow_config,
        agc=agc_config,
        model=srv["model"],
        language=srv["language_code"],
        rate=cli["rate"],
        chunk_ms=cli["chunk_ms"],
        silero_threshold=cli.get("silero_threshold", 0.5),
        auto_punct=srv["enable_automatic_punctuation"],
        single_utterance=srv["single_utterance"],
        max_stream_seconds=cli["max_stream_seconds"],
        device_index=cli.get("device_index"),
        silence_finalize_ms=cli.get("silence_finalize_ms", 150),
        vad_lead_in_ms=cli.get("vad_lead_in_ms", 300),
        max_no_text_seconds=cli.get("max_no_text_seconds", 5.0),
        forward_ws=fwd.get("enabled", True),  # Default to True - providers should forward to WheelHouse
        # CLI args override config for ws_host/ws_port (passed by WheelHouse launcher)
        ws_host=args.ws_host if args.ws_host else fwd.get("ws_host", "localhost"),
        ws_port=args.ws_port if args.ws_port is not None else fwd.get("ws_port", 0),
        hints_boost=adap.get("hints_boost"),
        phrase_hints=_load_hints(adap),
        class_tokens=[x for x in adap.get("class_tokens", []) if isinstance(x, str) and x.strip()],
        mic_check_seconds=diag.get("mic_check_seconds", 0),
        mic_check_write=diag.get("mic_check_write", ""),
        # CLI args override config.toml for wake word (passed by WheelHouse launcher)
        wake_word_enabled=args.wake_word_enabled or ww.get("enabled", False),
        wake_word_keyword=args.wake_word_keyword or ww.get("keyword", "computer"),
        wake_word_sensitivity=args.wake_word_sensitivity if args.wake_word_sensitivity is not None else ww.get("sensitivity", 0.5),
        wake_word_mode=args.wake_word_mode or ww.get("mode", "idle_recovery"),
        wake_word_model_dir=args.wake_word_model_dir or ww.get("model_dir", "data/wake_words"),
    )
    return args, app_config