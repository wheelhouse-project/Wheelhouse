"""Pattern-catalog unit tests for trailing-position commands (wh-2vz).

A trailing-position pattern carries ``position = "trailing"`` in patterns.toml
and is reserved for single-word commands that fire after the dictated
prefix completes (v1: "submit" -> press Enter).

These tests pin the loader contract:

- A trailing pattern is indexed in a separate ``trailing_commands`` map
  keyed by the lowercased literal word.
- Each value carries the compiled regex and the action list so the
  SpeechProcessor can execute it without re-parsing.
- A trailing pattern does NOT appear in ``first_words`` or
  ``all_patterns`` -- it must not affect any existing routing or
  remainder logic.
- ``position`` defaults to "leading", so the absence of the field
  preserves today's loader behaviour for every other pattern.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from speech.pattern_catalog import PatternCatalog


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _write_patterns(tmp_path: Path, body: str) -> str:
    """Helper: write a patterns.toml under tmp_path and return its path."""
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + body, encoding="utf-8")
    return str(p)


def test_trailing_pattern_is_indexed_in_trailing_commands_map(tmp_path):
    """``position = "trailing"`` enters the trailing_commands map."""
    body = """
[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert "submit" in catalog.trailing_commands
    entry = catalog.trailing_commands["submit"]
    assert entry["actions"] == [{"function": "press_keys", "params": ["enter"]}]
    # The compiled regex must match the literal word, case-insensitive.
    assert entry["compiled_pattern"].fullmatch("submit") is not None
    assert entry["compiled_pattern"].fullmatch("SUBMIT") is not None


def test_trailing_pattern_does_not_pollute_first_words_or_all_patterns(tmp_path):
    """Trailing patterns must not show up in the leading-pattern indexes."""
    body = """
[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert "submit" not in catalog.first_words
    # The trailing entry should NOT contribute to all_patterns (the list
    # TextParser walks for regular pattern matching). It lives only in
    # trailing_commands.
    assert all(
        entry.get("position", "leading") != "trailing"
        for entry in catalog.all_patterns
    )
    assert catalog.get_pattern_type("submit").name == "NONE"


def test_position_defaults_to_leading_when_absent(tmp_path):
    """Leading patterns without an explicit position field keep working."""
    body = """
[[pattern]]
pattern = '''^zoom in$'''
actions = [
    { function = "hk", params = ["ctrl", "+"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert catalog.trailing_commands == {}
    assert "zoom" in catalog.first_words


def test_explicit_position_leading_is_treated_like_default(tmp_path):
    """``position = "leading"`` is equivalent to omitting the field."""
    body = """
[[pattern]]
pattern = '''^zoom in$'''
position = "leading"
actions = [
    { function = "hk", params = ["ctrl", "+"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert catalog.trailing_commands == {}
    assert "zoom" in catalog.first_words


def test_trailing_pattern_with_multi_word_pattern_string_is_rejected(tmp_path):
    """v1 only supports single-word trailing patterns. Multi-word entries
    are skipped at load time -- the rest of the file must still load."""
    body = """
[[pattern]]
pattern = '''please submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]

[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    # The multi-word entry is dropped; the valid single-word entry loads.
    assert list(catalog.trailing_commands.keys()) == ["submit"]


def test_trailing_with_requires_hotword_is_rejected(tmp_path, caplog):
    """A trailing entry with requires_hotword=true is incoherent.

    The hotword has to PRECEDE the command but the trailing position
    puts the command word LAST. Reject at load time so the
    contradictory configuration is visible.
    """
    body = """
[[pattern]]
pattern = '''submit'''
position = "trailing"
requires_hotword = true
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    import logging
    caplog.set_level(logging.ERROR)
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert catalog.trailing_commands == {}
    assert any(
        "requires_hotword=true" in rec.message
        and "Trailing-position" in rec.message
        for rec in caplog.records
    )


def test_word_registered_as_both_leading_and_trailing_logs_warning(
    tmp_path, caplog,
):
    """A word in both registries silently breaks; log a load-time warning."""
    body = """
[[pattern]]
pattern = '''^submit$'''
actions = [
    { function = "press_keys", params = ["enter"] }
]

[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    import logging
    caplog.set_level(logging.WARNING)
    PatternCatalog(_write_patterns(tmp_path, body))

    assert any(
        "BOTH leading and trailing" in rec.message and "submit" in rec.message
        for rec in caplog.records
    )


def test_get_trailing_command_returns_entry_case_insensitively(tmp_path):
    """The lookup helper is case-insensitive to match how STT delivers words."""
    body = """
[[pattern]]
pattern = '''submit'''
position = "trailing"
actions = [
    { function = "press_keys", params = ["enter"] }
]
"""
    catalog = PatternCatalog(_write_patterns(tmp_path, body))

    assert catalog.get_trailing_command("submit") is not None
    assert catalog.get_trailing_command("Submit") is not None
    assert catalog.get_trailing_command("SUBMIT") is not None
    assert catalog.get_trailing_command("send") is None
