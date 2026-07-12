"""Tests for PatternCatalog.reload() hot-reload functionality."""
import pytest
from speech.pattern_catalog import PatternCatalog


class TestPatternCatalogReload:

    @pytest.fixture
    def patterns_file(self, tmp_path):
        content = (
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
        )
        f = tmp_path / "patterns.toml"
        f.write_text(content)
        return str(f)

    def test_reload_picks_up_new_pattern(self, patterns_file):
        catalog = PatternCatalog(patterns_file)
        assert catalog.pattern_count == 1

        with open(patterns_file, "a") as f:
            f.write(
                "\n[[pattern]]\n"
                "pattern = '''^test$'''\n"
                'actions = [{ function = "hk", params = ["f5"] }]\n'
            )

        catalog.reload()
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("test")

    def test_reload_preserves_hotword(self, patterns_file):
        catalog = PatternCatalog(patterns_file)
        catalog.reload()
        assert catalog.command_hotword == "x-ray"

    def test_reload_rebuilds_first_words(self, patterns_file):
        catalog = PatternCatalog(patterns_file)
        assert not catalog.could_be_pattern_start("test")

        with open(patterns_file, "a") as f:
            f.write(
                "\n[[pattern]]\n"
                "pattern = '''^test$'''\n"
                'actions = [{ function = "hk", params = ["f5"] }]\n'
            )

        catalog.reload()
        assert catalog.could_be_pattern_start("test")

    def test_reload_removes_deleted_pattern(self, patterns_file):
        """If a pattern is removed from the file, reload should drop it."""
        catalog = PatternCatalog(patterns_file)
        assert catalog.could_be_pattern_start("save")

        # Rewrite file without the save pattern
        content = (
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''^other$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n'
        )
        with open(patterns_file, "w") as f:
            f.write(content)

        catalog.reload()
        assert not catalog.could_be_pattern_start("save")
        assert catalog.could_be_pattern_start("other")

    def test_reload_survives_bad_file(self, patterns_file):
        """If the file becomes invalid, reload should keep old data."""
        catalog = PatternCatalog(patterns_file)
        assert catalog.pattern_count == 1

        with open(patterns_file, "w") as f:
            f.write("this is not valid toml {{{")

        catalog.reload()
        # Old data preserved
        assert catalog.pattern_count == 1
        assert catalog.could_be_pattern_start("save")

    def test_reload_updates_all_patterns_list(self, patterns_file):
        catalog = PatternCatalog(patterns_file)
        old_patterns = catalog.get_all_patterns()
        assert len(old_patterns) == 1

        with open(patterns_file, "a") as f:
            f.write(
                "\n[[pattern]]\n"
                "pattern = '''^new cmd$'''\n"
                'actions = [{ function = "hk", params = ["f6"] }]\n'
            )

        catalog.reload()
        new_patterns = catalog.get_all_patterns()
        assert len(new_patterns) == 2
