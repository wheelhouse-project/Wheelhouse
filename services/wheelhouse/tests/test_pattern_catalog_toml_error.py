"""Tests for PatternCatalog graceful handling of TOML syntax errors.

Verifies that:
- A malformed TOML file during __init__ results in degraded mode (pattern_count == 0)
  instead of crashing with an exception.
- A malformed TOML file during reload() preserves previous patterns (existing behavior).
"""
import pytest
from speech.pattern_catalog import PatternCatalog


VALID_TOML = (
    'COMMAND_HOTWORD = "x-ray"\n\n'
    "[[pattern]]\n"
    "pattern = '''^save$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
)

MALFORMED_TOML = "this is not valid toml {{{"

MISSING_HOTWORD_TOML = (
    "# no COMMAND_HOTWORD here\n"
    "[[pattern]]\n"
    "pattern = '''^save$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
)


class TestTomlSyntaxErrorOnInit:
    """TOML errors during __init__ should result in degraded mode, not a crash."""

    def test_malformed_toml_does_not_raise(self, tmp_path):
        """PatternCatalog should NOT raise when patterns.toml has syntax errors."""
        f = tmp_path / "patterns.toml"
        f.write_text(MALFORMED_TOML)

        # Should not raise -- degraded mode instead
        catalog = PatternCatalog(str(f))
        assert catalog.pattern_count == 0

    def test_malformed_toml_has_empty_patterns(self, tmp_path):
        """After a TOML error, all pattern structures should be empty."""
        f = tmp_path / "patterns.toml"
        f.write_text(MALFORMED_TOML)

        catalog = PatternCatalog(str(f))
        assert catalog.first_words == {}
        assert catalog.all_patterns == []
        assert catalog.command_hotword is None

    def test_missing_hotword_does_not_raise(self, tmp_path):
        """Missing COMMAND_HOTWORD should degrade gracefully, not crash."""
        f = tmp_path / "patterns.toml"
        f.write_text(MISSING_HOTWORD_TOML)

        catalog = PatternCatalog(str(f))
        assert catalog.pattern_count == 0

    def test_file_not_found_does_not_raise(self, tmp_path):
        """A missing file should degrade gracefully, not crash."""
        missing = tmp_path / "nonexistent.toml"

        catalog = PatternCatalog(str(missing))
        assert catalog.pattern_count == 0

    def test_malformed_toml_logs_error(self, tmp_path, caplog):
        """TOML errors should be logged at ERROR level."""
        f = tmp_path / "patterns.toml"
        f.write_text(MALFORMED_TOML)

        with caplog.at_level("ERROR"):
            PatternCatalog(str(f))

        assert any("TOML syntax error" in msg for msg in caplog.messages)


class TestTomlSyntaxErrorOnReload:
    """TOML errors during reload() should preserve previous patterns."""

    @pytest.fixture
    def catalog_with_patterns(self, tmp_path):
        """Create a catalog loaded from valid TOML."""
        f = tmp_path / "patterns.toml"
        f.write_text(VALID_TOML)
        catalog = PatternCatalog(str(f))
        assert catalog.pattern_count == 1  # sanity check
        return catalog, f

    def test_reload_preserves_patterns_on_toml_error(self, catalog_with_patterns):
        """After a failed reload, old patterns should still be intact."""
        catalog, f = catalog_with_patterns

        f.write_text(MALFORMED_TOML)
        result = catalog.reload()

        assert result is False
        assert catalog.pattern_count == 1
        assert catalog.could_be_pattern_start("save")

    def test_reload_preserves_hotword_on_toml_error(self, catalog_with_patterns):
        """After a failed reload, command_hotword should be preserved."""
        catalog, f = catalog_with_patterns

        f.write_text(MALFORMED_TOML)
        catalog.reload()

        assert catalog.command_hotword == "x-ray"
