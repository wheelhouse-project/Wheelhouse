"""Catch shipped-catalog drift in the inputs used by the doc_id regressions."""

from pathlib import Path
import tomllib

import pytest

import test_pattern_catalog_doc_id_merge as merge_tests
import test_pattern_manager_doc_id_listing as listing_tests
from doc_id_catalog_fixtures import shipped_entry, rewritten, runtime_entry, block


@pytest.mark.parametrize("consumer", [merge_tests, listing_tests])
def test_builtin_fixture_is_the_complete_shipped_entry(consumer):
    source = Path(__file__).resolve().parents[1] / "speech/config/patterns.toml"
    shipped = tomllib.loads(source.read_text(encoding="utf-8"))["pattern"]
    expected = next(p for p in shipped if p.get("doc_id") == "maximize-window")
    assert tomllib.loads(consumer.BUILTIN_BEFORE)["pattern"] == [expected]


def test_a_catalog_edit_reaches_the_next_fixture_without_losing_metadata(tmp_path):
    """A cached or partial handwritten entry would hide a release edit."""
    source = tmp_path / "catalog.toml"
    entry = shipped_entry("maximize-window")
    source.write_text(block(entry), encoding="utf-8")
    first = shipped_entry("maximize-window", source=source)
    changed = {**entry, "pattern": "^enlarge$", "description": "release edit"}
    source.write_text(block(changed), encoding="utf-8")
    fresh = shipped_entry("maximize-window", source=source)
    assert fresh["pattern"] == "^enlarge$"
    assert fresh["description"] == "release edit"
    assert first["pattern"] == entry["pattern"]
    fresh["actions"][0]["params"].append("test-only")
    assert shipped_entry("maximize-window", source=source) == changed


@pytest.mark.parametrize("entries", [[], ["maximize-window", "maximize-window"]])
def test_missing_or_ambiguous_identity_refuses_a_fixture(tmp_path, entries):
    source = tmp_path / "catalog.toml"
    source.write_text(
        "pattern = []\n" if not entries else "".join(block(shipped_entry(i)) for i in entries),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Expected one shipped"):
        shipped_entry("maximize-window", source=source)


def test_preview_input_preserves_the_shipped_escape_guard(tmp_path):
    """The old preview copy omitted the whole-utterance-only guard."""
    entry = runtime_entry("escape", tmp_path)
    assert entry["whole_utterance_only"] is True
    assert entry["actions"] == [{"function": "press", "params": ["esc"]}]


def test_release_rewrite_changes_only_expression():
    original = shipped_entry("maximize-window")
    changed = rewritten(original)
    assert changed["pattern"] != original["pattern"]
    assert {k: v for k, v in changed.items() if k != "pattern"} == {
        k: v for k, v in original.items() if k != "pattern"
    }


def test_release_rewrite_keeps_command_classification_and_guard(tmp_path):
    original = runtime_entry("maximize-window", tmp_path)
    changed = runtime_entry("maximize-window", tmp_path, rewrite=True)
    assert original["pattern_type"] == changed["pattern_type"] == "command"
    assert original["whole_utterance_only"] is True
    assert changed["whole_utterance_only"] is True
