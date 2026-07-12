"""Tests for ConfigService - TOML configuration management.

Covers:
- Loading from a TOML file
- Dot-notation access (get)
- Default value when key missing
- set() updates in-memory config (flat and nested)
- save() writes to disk
- Error handling for missing/invalid TOML files
"""

import asyncio
from pathlib import Path

import pytest

from config_service import ConfigService


@pytest.fixture
def config_toml(tmp_path):
    """Create a minimal config.toml for testing."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'SPEECH_ENABLED_ON_STARTUP = true\n'
        'LOG_LEVEL = "INFO"\n'
        '\n'
        '[speech]\n'
        'timeout_ms = 700\n'
        'model = "default"\n'
        '\n'
        '[plugins.bravia]\n'
        'device_name = "Living Room TV"\n'
        'ip = "192.168.1.100"\n'
    )
    return config_file


@pytest.fixture
def config_svc(config_toml):
    """ConfigService loaded from test config file."""
    return ConfigService(config_path=str(config_toml))


# -----------------------------------------------------------------------
# Loading
# -----------------------------------------------------------------------

class TestLoading:

    def test_loads_from_toml_file(self, config_svc):
        assert config_svc.get("LOG_LEVEL") == "INFO"

    def test_loads_nested_values(self, config_svc):
        assert config_svc.get("speech.timeout_ms") == 700

    def test_get_config_returns_full_dict(self, config_svc):
        full = config_svc.get_config()
        assert isinstance(full, dict)
        assert "speech" in full

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ConfigService(config_path=str(tmp_path / "nonexistent.toml"))

    def test_invalid_toml_raises_value_error(self, tmp_path):
        bad_file = tmp_path / "bad.toml"
        bad_file.write_text("this is not [ valid toml {{{{")
        with pytest.raises(ValueError, match="Invalid TOML"):
            ConfigService(config_path=str(bad_file))


# -----------------------------------------------------------------------
# get() with dot notation
# -----------------------------------------------------------------------

class TestGet:

    def test_simple_key(self, config_svc):
        assert config_svc.get("SPEECH_ENABLED_ON_STARTUP") is True

    def test_nested_key_dot_notation(self, config_svc):
        assert config_svc.get("speech.model") == "default"

    def test_deeply_nested_key(self, config_svc):
        assert config_svc.get("plugins.bravia.device_name") == "Living Room TV"

    def test_missing_key_returns_default(self, config_svc):
        assert config_svc.get("nonexistent", "fallback") == "fallback"

    def test_missing_key_returns_none_by_default(self, config_svc):
        assert config_svc.get("nonexistent") is None

    def test_missing_nested_key_returns_default(self, config_svc):
        assert config_svc.get("speech.nonexistent", 42) == 42

    def test_partial_nested_path_returns_default(self, config_svc):
        assert config_svc.get("nonexistent.deep.path", "nope") == "nope"

    def test_intermediate_non_dict_returns_default(self, config_svc):
        # speech.timeout_ms is an int, not a dict - accessing deeper should return default
        assert config_svc.get("speech.timeout_ms.deeper", "default") == "default"


# -----------------------------------------------------------------------
# set()
# -----------------------------------------------------------------------

class TestSet:

    def test_set_simple_key(self, config_svc):
        config_svc.set("NEW_KEY", "new_value")
        assert config_svc.get("NEW_KEY") == "new_value"

    def test_set_overwrites_existing(self, config_svc):
        config_svc.set("LOG_LEVEL", "DEBUG")
        assert config_svc.get("LOG_LEVEL") == "DEBUG"

    def test_set_nested_key(self, config_svc):
        config_svc.set("speech.timeout_ms", 500)
        assert config_svc.get("speech.timeout_ms") == 500

    def test_set_creates_nested_structure(self, config_svc):
        config_svc.set("new_section.subsection.key", "value")
        assert config_svc.get("new_section.subsection.key") == "value"

    def test_set_deeply_nested(self, config_svc):
        config_svc.set("plugins.bravia.ip", "10.0.0.1")
        assert config_svc.get("plugins.bravia.ip") == "10.0.0.1"
        # Ensure other keys in same section are preserved
        assert config_svc.get("plugins.bravia.device_name") == "Living Room TV"


# -----------------------------------------------------------------------
# save()
# -----------------------------------------------------------------------

class TestSave:

    @pytest.mark.asyncio
    async def test_save_persists_to_disk(self, config_svc, config_toml):
        config_svc.set("LOG_LEVEL", "DEBUG")
        await config_svc.save()

        # Reload from same file
        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("LOG_LEVEL") == "DEBUG"

    @pytest.mark.asyncio
    async def test_save_preserves_nested_structure(self, config_svc, config_toml):
        config_svc.set("speech.timeout_ms", 999)
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("speech.timeout_ms") == 999
        assert reloaded.get("speech.model") == "default"

    @pytest.mark.asyncio
    async def test_save_new_keys_persist(self, config_svc, config_toml):
        config_svc.set("brand_new", "value")
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("brand_new") == "value"


# -----------------------------------------------------------------------
# Class variable isolation
# -----------------------------------------------------------------------

class TestDefaultPath:

    def test_default_path_resolves_to_config_toml(self, monkeypatch, tmp_path):
        """When no config_path given, defaults to config.toml next to the module."""
        # Create config.toml in the config_service.py directory
        import config_service as cs_mod
        module_dir = Path(cs_mod.__file__).parent
        config_file = module_dir / "config.toml"

        # Only run if the real config.toml exists (it should in the wheelhouse dir)
        if config_file.exists():
            svc = ConfigService()
            assert svc.config_path == str(config_file)
        else:
            pytest.skip("No config.toml in wheelhouse directory")


class TestSaveErrorHandling:

    @pytest.mark.asyncio
    async def test_save_handles_write_error(self, config_svc):
        """Save logs error but doesn't raise on write failure."""
        # Point to a path that can't be written
        config_svc.config_path = "/nonexistent/dir/config.toml"
        # Should not raise
        await config_svc.save()


class TestIsolation:

    def test_instances_share_class_config(self):
        """ConfigService uses a class variable _config - verify behavior.

        This documents the current behavior rather than prescribing it.
        Two instances pointing at different files will overwrite each other's
        _config since it's a class variable. Tests use fresh fixtures so this
        doesn't cause issues in practice.
        """
        # Just verify _config exists as a class attribute
        assert hasattr(ConfigService, "_config")


# -----------------------------------------------------------------------
# Adversarial: empty string key
# -----------------------------------------------------------------------

class TestEmptyKey:

    def test_get_empty_string_key_returns_default(self, config_svc):
        """get('') falls through to dict.get('', default) since '' has no dot.
        No config entry has an empty-string key, so default is returned."""
        assert config_svc.get("") is None
        assert config_svc.get("", "fallback") == "fallback"

    def test_set_empty_string_key_stores_value(self, config_svc):
        """set('') stores a value under the empty-string key in the dict.
        This is technically valid Python dict behavior, even if no real
        caller would do it."""
        config_svc.set("", "empty_key_value")
        assert config_svc.get("") == "empty_key_value"

    def test_set_empty_string_key_does_not_corrupt_existing(self, config_svc):
        """Setting an empty-string key should not affect other config values."""
        original_log_level = config_svc.get("LOG_LEVEL")
        config_svc.set("", "empty_key_value")
        assert config_svc.get("LOG_LEVEL") == original_log_level


# -----------------------------------------------------------------------
# Adversarial: concurrent async get/set
# -----------------------------------------------------------------------

class TestSequentialSetAndSave:

    @pytest.mark.asyncio
    async def test_interleaved_set_and_save(self, config_svc, config_toml):
        """Multiple save() calls interleaved with set() should not lose data.
        Since save() runs file I/O in a thread, verify that the final state
        on disk reflects all set() calls made before the last save()."""
        config_svc.set("LOG_LEVEL", "DEBUG")
        await config_svc.save()

        config_svc.set("LOG_LEVEL", "WARNING")
        await config_svc.save()

        reloaded = ConfigService(config_path=str(config_toml))
        assert reloaded.get("LOG_LEVEL") == "WARNING"
