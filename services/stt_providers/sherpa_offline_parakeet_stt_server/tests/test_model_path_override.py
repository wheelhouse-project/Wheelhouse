"""Tests for the per-machine model-path override mechanism (release plan
section 5 design notes, wh-797.3.6 / wh-797.6.8, implemented under
wh-parakeet-model-delivery).

Precedence for [model].model_path, resolved at config load:
  1. %LOCALAPPDATA%/WheelHouse/stt_model_overrides.toml, section named after
     [provider].name, key model_path  (written by the installer; untracked)
  2. the provider's own tracked config.toml value
  3. the coded default %LOCALAPPDATA%/WheelHouse/models/<v1 model dir>

The shipped public config carries an empty model_path, so on user machines
the override file (or the coded default) supplies the path. Dev machines
keep their tracked-config value and need no override file. A malformed or
unreadable override file must never crash the provider: it logs a warning
and the tracked value stands.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import main as parakeet_main


def _write_override(local_app_data: Path, body: str) -> Path:
    override_dir = local_app_data / "WheelHouse"
    override_dir.mkdir(parents=True, exist_ok=True)
    override_path = override_dir / "stt_model_overrides.toml"
    override_path.write_text(body, encoding="utf-8")
    return override_path


@pytest.fixture
def local_app_data(monkeypatch, tmp_path: Path) -> Path:
    lad = tmp_path / "AppDataLocal"
    lad.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(lad))
    return lad


def _config(model_path: str | None = "C:/tracked/models/parakeet-dir") -> dict:
    cfg: dict = {"provider": {"name": "parakeet_tdt"}}
    if model_path is not None:
        cfg["model"] = {"model_path": model_path}
    else:
        cfg["model"] = {}
    return cfg


class TestOverrideFileWins:
    def test_override_replaces_tracked_config_value(self, local_app_data):
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/override/model-dir"\n',
        )
        resolved = parakeet_main._resolve_model_path(_config())
        assert resolved["model"]["model_path"] == "C:/override/model-dir"

    def test_override_applies_when_config_value_empty(self, local_app_data):
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/override/model-dir"\n',
        )
        resolved = parakeet_main._resolve_model_path(_config(model_path=""))
        assert resolved["model"]["model_path"] == "C:/override/model-dir"

    def test_section_key_comes_from_provider_name(self, local_app_data):
        """The override section is keyed by [provider].name, so a section
        for a different provider must not apply here."""
        _write_override(
            local_app_data,
            '[some_other_provider]\nmodel_path = "C:/other/model"\n',
        )
        cfg = _config()
        resolved = parakeet_main._resolve_model_path(cfg)
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )


class TestTrackedConfigFallback:
    def test_no_override_file_keeps_config_value(self, local_app_data):
        resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )

    def test_override_file_with_empty_value_keeps_config_value(self, local_app_data):
        _write_override(local_app_data, '[parakeet_tdt]\nmodel_path = ""\n')
        resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )


class TestCodedDefault:
    def test_empty_config_and_no_override_uses_coded_default(self, local_app_data):
        resolved = parakeet_main._resolve_model_path(_config(model_path=""))
        expected = str(
            local_app_data
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )
        assert resolved["model"]["model_path"] == expected

    def test_missing_model_section_gets_coded_default(self, local_app_data):
        """The shipped config may omit model_path entirely; resolution must
        create [model].model_path so startup code can rely on it."""
        cfg = {"provider": {"name": "parakeet_tdt"}}
        resolved = parakeet_main._resolve_model_path(cfg)
        expected = str(
            local_app_data
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )
        assert resolved["model"]["model_path"] == expected


class TestFailureHonesty:
    def test_malformed_override_file_keeps_config_value(self, local_app_data, caplog):
        """A half-edited override file must not take the provider down."""
        _write_override(local_app_data, "[parakeet_tdt\nnot toml at all ===")
        with caplog.at_level("WARNING"):
            resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )
        assert any("override" in r.message.lower() for r in caplog.records)

    def test_no_localappdata_keeps_config_value(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )

    def test_no_localappdata_and_no_config_key_sets_empty_string(self, monkeypatch):
        """Invariant (wh-797.14.1): resolution ALWAYS leaves a string at
        [model].model_path, even with no LOCALAPPDATA and no configured
        value - startup code indexes it unconditionally and must get a
        clear model-missing error, not a KeyError."""
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        resolved = parakeet_main._resolve_model_path(
            {"provider": {"name": "parakeet_tdt"}}
        )
        assert resolved["model"]["model_path"] == ""

    def test_non_string_tracked_value_rescued_by_override(self, local_app_data):
        """wh-797.14.2: a hand-corrupted tracked value (unquoted number)
        must be treated as absent so the installer-written override still
        applies - not crash before the override file is even read."""
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/override/model-dir"\n',
        )
        cfg = {"provider": {"name": "parakeet_tdt"}, "model": {"model_path": 3}}
        resolved = parakeet_main._resolve_model_path(cfg)
        assert resolved["model"]["model_path"] == "C:/override/model-dir"

    def test_non_string_tracked_value_no_override_uses_coded_default(
        self, local_app_data
    ):
        cfg = {"provider": {"name": "parakeet_tdt"}, "model": {"model_path": 3}}
        resolved = parakeet_main._resolve_model_path(cfg)
        expected = str(
            local_app_data
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )
        assert resolved["model"]["model_path"] == expected

    def test_utf8_bom_override_file_parses(self, local_app_data):
        """wh-797.14.3: PowerShell 5.1 writes UTF-8 with a byte-order mark
        by default; the installer-written override file must still parse."""
        override_dir = local_app_data / "WheelHouse"
        override_dir.mkdir(parents=True, exist_ok=True)
        body = '[parakeet_tdt]\nmodel_path = "C:/override/bom-dir"\n'
        (override_dir / "stt_model_overrides.toml").write_bytes(
            b"\xef\xbb\xbf" + body.encode("utf-8")
        )
        resolved = parakeet_main._resolve_model_path(_config())
        assert resolved["model"]["model_path"] == "C:/override/bom-dir"

    def test_non_dict_override_section_keeps_config(self, local_app_data, caplog):
        """wh-797.14.4: [parakeet_tdt] as a plain value, not a table."""
        _write_override(local_app_data, 'parakeet_tdt = "not a table"\n')
        with caplog.at_level("WARNING"):
            resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )

    def test_non_string_override_value_keeps_config(self, local_app_data, caplog):
        _write_override(local_app_data, "[parakeet_tdt]\nmodel_path = 3\n")
        with caplog.at_level("WARNING"):
            resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )

    def test_whitespace_only_override_value_keeps_config(self, local_app_data):
        _write_override(local_app_data, '[parakeet_tdt]\nmodel_path = "   "\n')
        resolved = parakeet_main._resolve_model_path(_config())
        assert (
            resolved["model"]["model_path"]
            == "C:/tracked/models/parakeet-dir"
        )

    def test_whitespace_only_tracked_value_uses_coded_default(self, local_app_data):
        resolved = parakeet_main._resolve_model_path(_config(model_path="   "))
        expected = str(
            local_app_data
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )
        assert resolved["model"]["model_path"] == expected


class TestModelSectionPreservation:
    def test_sibling_model_keys_survive_override_resolution(self, local_app_data):
        """wh-797.15.1: resolution must mutate model_path in place, never
        rebuild the model dict - dropping use_gpu/gpu_device_id would
        silently revert a GPU machine to CPU inference."""
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/override/model-dir"\n',
        )
        cfg = {
            "provider": {"name": "parakeet_tdt"},
            "model": {
                "model_path": "C:/tracked/value",
                "use_gpu": True,
                "gpu_device_id": 1,
                "num_threads": 4,
            },
        }
        resolved = parakeet_main._resolve_model_path(cfg)
        assert resolved["model"]["model_path"] == "C:/override/model-dir"
        assert resolved["model"]["use_gpu"] is True
        assert resolved["model"]["gpu_device_id"] == 1
        assert resolved["model"]["num_threads"] == 4

    def test_sibling_model_keys_survive_coded_default(self, local_app_data):
        cfg = {
            "provider": {"name": "parakeet_tdt"},
            "model": {"model_path": "", "use_gpu": True},
        }
        resolved = parakeet_main._resolve_model_path(cfg)
        assert resolved["model"]["use_gpu"] is True
        assert resolved["model"]["model_path"].endswith(
            parakeet_main.DEFAULT_MODEL_DIRNAME
        )


class TestNonAsciiPaths:
    def test_non_ascii_localappdata_finds_override(self, monkeypatch, tmp_path):
        """wh-797.15.2: Windows profile paths legitimately contain
        non-ASCII (C:/Users/Jose-with-accent/...); the override file must
        still be found and parsed."""
        lad = tmp_path / "AppDataLöcal"
        lad.mkdir()
        monkeypatch.setenv("LOCALAPPDATA", str(lad))
        _write_override(
            lad, '[parakeet_tdt]\nmodel_path = "C:/override/model-dir"\n'
        )
        resolved = parakeet_main._resolve_model_path(_config())
        assert resolved["model"]["model_path"] == "C:/override/model-dir"

    def test_non_ascii_override_value_preserved(self, local_app_data):
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/モデル/parakeet"\n',
        )
        resolved = parakeet_main._resolve_model_path(_config())
        assert resolved["model"]["model_path"] == "C:/モデル/parakeet"


class TestLoadConfigIntegration:
    def test_load_config_applies_resolution(self, local_app_data, tmp_path):
        _write_override(
            local_app_data,
            '[parakeet_tdt]\nmodel_path = "C:/override/from-integration"\n',
        )
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[provider]\nname = "parakeet_tdt"\n\n'
            '[model]\nmodel_path = "C:/tracked/value"\n',
            encoding="utf-8",
        )
        config = parakeet_main.load_config(config_path=config_file)
        assert config["model"]["model_path"] == "C:/override/from-integration"

    def test_load_config_missing_file_still_resolves_default(
        self, local_app_data, tmp_path
    ):
        config = parakeet_main.load_config(config_path=tmp_path / "absent.toml")
        expected = str(
            local_app_data
            / "WheelHouse"
            / "models"
            / parakeet_main.DEFAULT_MODEL_DIRNAME
        )
        assert config["model"]["model_path"] == expected
