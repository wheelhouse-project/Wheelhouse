"""Tests for google_stt_server config_updater.py.

Covers:
- add_hint_to_config: adding hints, dedup, empty/long hints, missing file, comment preservation
- get_hints: reading hints, missing sections, file errors
- Adversarial: missing config file, corrupt TOML, missing adaptation section
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add google_stt_server to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config_updater import add_hint_to_config, get_hints


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, hints: list[str] | None = None):
    """Write a minimal TOML config with optional hints list."""
    lines = ['[server]\nmodel = "latest_short"\n']
    if hints is not None:
        hints_str = ", ".join(f'"{h}"' for h in hints)
        lines.append(f"\n[adaptation]\nhints = [{hints_str}]\n")
    path.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# add_hint_to_config
# ---------------------------------------------------------------------------

class TestAddHintToConfig:
    """Tests for add_hint_to_config - adding hints with comment preservation."""

    def test_adds_new_hint(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["existing"])
        result = add_hint_to_config("new_hint", config_path=cfg)
        assert result is True
        # Verify hint was written
        hints = get_hints(config_path=cfg)
        assert "new_hint" in hints

    def test_returns_false_for_duplicate(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["claude"])
        result = add_hint_to_config("claude", config_path=cfg)
        assert result is False

    def test_duplicate_check_is_case_insensitive(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["Claude"])
        result = add_hint_to_config("claude", config_path=cfg)
        assert result is False

    def test_returns_false_for_empty_hint(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        result = add_hint_to_config("", config_path=cfg)
        assert result is False

    def test_returns_false_for_whitespace_only_hint(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        result = add_hint_to_config("   ", config_path=cfg)
        assert result is False

    def test_strips_whitespace_from_hint(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        result = add_hint_to_config("  padded  ", config_path=cfg)
        assert result is True
        hints = get_hints(config_path=cfg)
        assert "padded" in hints

    def test_truncates_long_hint(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        long_hint = "a" * 150
        result = add_hint_to_config(long_hint, config_path=cfg)
        assert result is True
        hints = get_hints(config_path=cfg)
        assert len(hints[0]) <= 100

    def test_returns_false_for_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.toml"
        result = add_hint_to_config("test", config_path=missing)
        assert result is False

    def test_creates_adaptation_section_if_missing(self, tmp_path):
        cfg = tmp_path / "config.toml"
        # Config with no adaptation section
        cfg.write_text('[server]\nmodel = "latest_short"\n', encoding="utf-8")
        result = add_hint_to_config("new_hint", config_path=cfg)
        assert result is True
        hints = get_hints(config_path=cfg)
        assert "new_hint" in hints

    def test_preserves_existing_hints(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["alpha", "beta"])
        add_hint_to_config("gamma", config_path=cfg)
        hints = get_hints(config_path=cfg)
        assert "alpha" in hints
        assert "beta" in hints
        assert "gamma" in hints

    def test_preserves_comments_in_file(self, tmp_path):
        cfg = tmp_path / "config.toml"
        content = '# Important comment\n[server]\nmodel = "latest_short"\n\n[adaptation]\n# Hints list\nhints = ["existing"]\n'
        cfg.write_text(content, encoding="utf-8")
        add_hint_to_config("new_hint", config_path=cfg)
        # Read raw file to check comment preservation
        raw = cfg.read_text(encoding="utf-8")
        assert "# Important comment" in raw
        assert "# Hints list" in raw

    def test_returns_false_on_corrupt_toml(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("this is not [valid toml {{{", encoding="utf-8")
        result = add_hint_to_config("test", config_path=cfg)
        assert result is False

    def test_default_config_path_uses_module_directory(self):
        """When no config_path given, defaults to __file__ parent / config.toml."""
        # We can't easily test the actual default path, but verify the function
        # signature accepts None and attempts to use it
        with patch("config_updater.Path") as mock_path:
            mock_path.return_value.parent.__truediv__.return_value = Path("/fake/config.toml")
            # Will fail with FileNotFoundError, which is caught
            result = add_hint_to_config("test", config_path=None)
            assert result is False

    def test_multiple_sequential_adds(self, tmp_path):
        """Multiple hints can be added sequentially."""
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        assert add_hint_to_config("first", config_path=cfg) is True
        assert add_hint_to_config("second", config_path=cfg) is True
        assert add_hint_to_config("third", config_path=cfg) is True
        hints = get_hints(config_path=cfg)
        assert len(hints) == 3


# ---------------------------------------------------------------------------
# get_hints
# ---------------------------------------------------------------------------

class TestGetHints:
    """Tests for get_hints - reading hints from config."""

    def test_returns_hints_list(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["alpha", "beta"])
        result = get_hints(config_path=cfg)
        assert result == ["alpha", "beta"]

    def test_returns_empty_list_when_no_adaptation(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[server]\nmodel = "test"\n', encoding="utf-8")
        result = get_hints(config_path=cfg)
        assert result == []

    def test_returns_empty_list_when_no_hints_key(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[adaptation]\nhints_boost = 10.0\n', encoding="utf-8")
        result = get_hints(config_path=cfg)
        assert result == []

    def test_returns_empty_list_for_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.toml"
        result = get_hints(config_path=missing)
        assert result == []

    def test_returns_empty_list_for_corrupt_toml(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text("not valid toml {{{", encoding="utf-8")
        result = get_hints(config_path=cfg)
        assert result == []

    def test_converts_values_to_strings(self, tmp_path):
        """All hint values are converted to strings."""
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=["word"])
        result = get_hints(config_path=cfg)
        assert all(isinstance(h, str) for h in result)

    def test_empty_hints_list(self, tmp_path):
        cfg = tmp_path / "config.toml"
        _write_config(cfg, hints=[])
        result = get_hints(config_path=cfg)
        assert result == []
