"""The Google config.toml must declare the [debug] key its own code reads.

wh-codex-merge-audit.13.1.1. main.py:1441 passes
cfg.debug.log_load_diagnostics into WakeWordDetector, and
config_loader.py:209 reads that value with a default of False, so the line in
the tracked config.toml is the only thing an operator can change to turn the
ten-second [load-diag] work_scope=wake_word line -- and the periodic
[load-diag] window= line the CaptureLoadReporter writes -- on. With no such
line in the file the setting stays at the dataclass default for the life of
every Google run, and nothing else reports the gap: the flag's own tests in
tests/test_config_loader.py (TestDebugLoadDiagnosticsFlag) build the [debug]
table in memory and never read the shipped file.

What is read here is therefore the TRACKED file on disk rather than a
fixture, which is the only thing that can catch the key being absent from it.
The sherpa and distil suites pin the same binding for their own providers, in
tests/test_load_diagnostics_wiring.py.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"


def _tracked_config() -> dict:
    """The shipped config.toml, parsed the way the service parses it."""
    with open(CONFIG_PATH, "rb") as stream:
        return tomllib.load(stream)


class TestTheTrackedConfigDeclaresTheLoadDiagnosticsKey:
    """A key no config file declares is a setting no operator can change."""

    def test_the_tracked_config_declares_the_key(self):
        config = _tracked_config()
        assert "log_load_diagnostics" in config["debug"], (
            "google_stt_server/config.toml declares no [debug] "
            "log_load_diagnostics key, so an operator has no line to edit "
            "and the ten-second [load-diag] work_scope=wake_word line "
            "cannot be turned on")
        assert isinstance(config["debug"]["log_load_diagnostics"], bool), (
            "the key must parse as a TOML boolean, because "
            "config_loader.py hands the parsed value straight to "
            "WakeWordDetector")

    def test_the_key_ships_off(self):
        """The line costs one log every ten seconds for the life of the
        process, so the shipped file must leave it off and a run that wants
        the measurement must say so."""
        assert _tracked_config()["debug"]["log_load_diagnostics"] is False
