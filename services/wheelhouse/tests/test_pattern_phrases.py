# tests/test_pattern_phrases.py
"""Tests for phrase-list patterns (wh-pattern-editor-phrases).

A user pattern block may carry an optional ``phrases = [...]`` array. When it
does, the matching expression is generated from the phrase list -- each phrase
escaped, joined as alternatives, anchored (``^(?:editor|code\\ editor)$``
style, spec section 6) -- and the ``trigger`` input may be absent. The phrases
array is written into the TOML block so the editor dialog can round-trip it,
through BOTH create_pattern and update_pattern (update regenerates blocks from
create-shaped data, so losing the key on edit is the Stage 1 handoff risk).

The PatternCatalog loader must tolerate the unknown ``phrases`` key so older
WheelHouse versions reading a newer user file degrade gracefully.

Spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 6.
"""
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager
from speech.phrase_expression import (
    generate_expression,
    normalize_phrases,
    validate_phrases,
)


_SERVICE_DIR = Path(__file__).parent.parent

SYSTEM_CONTENT = (
    'COMMAND_HOTWORD = "x-ray"\n'
    '\n'
    '# =============================================================================\n'
    '# COMMANDS - Window Management\n'
    '# =============================================================================\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^save$'''\n"
    'requires_hotword = true\n'
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "s"] }\n'
    ']\n'
)


@pytest.fixture
def system_file(tmp_path):
    f = tmp_path / "patterns.toml"
    f.write_text(SYSTEM_CONTENT, encoding="utf-8")
    return str(f)


@pytest.fixture
def user_file(tmp_path):
    # Path only; the file is created by the test or the code under test.
    return str(tmp_path / "user_patterns.toml")


def _create_data(phrases=None, trigger="", **overrides):
    """Build create-shaped kwargs, defaulting to a hotkey action."""
    data = {
        "trigger": trigger,
        "pattern_type": "command",
        "action_type": "hotkey",
        "action_params": {"keys": ["ctrl", "e"]},
        "requires_hotword": False,
    }
    if phrases is not None:
        data["phrases"] = phrases
    data.update(overrides)
    return data


def _read_user_patterns(user_file):
    with open(user_file, "rb") as fh:
        return tomllib.load(fh)["pattern"]


# ---------------------------------------------------------------------------
# Expression generation (speech/phrase_expression.py)
# ---------------------------------------------------------------------------

class TestGenerateExpression:
    def test_single_phrase_command_anchored(self):
        assert generate_expression(["editor"]) == r"^(?:editor)$"

    def test_multiple_phrases_join_as_alternatives(self):
        # The spec section 6 example, verbatim.
        result = generate_expression(["editor", "code editor", "vs code"])
        assert result == r"^(?:editor|code\ editor|vs\ code)$"

    def test_regex_metacharacters_escaped(self):
        result = generate_expression(["what?", "a.b (x)"])
        assert result == r"^(?:what\?|a\.b\ \(x\))$"
        compiled = re.compile(result, re.IGNORECASE)
        assert compiled.fullmatch("what?")
        assert compiled.fullmatch("a.b (x)")
        # The '?' must be literal, not a quantifier on 't'.
        assert not compiled.fullmatch("wha")

    def test_replacement_anchoring_mirrors_generate_regex(self):
        # PatternManager.generate_regex anchors commands with ^...$ and
        # replacements with \b...\b; the phrase expression mirrors that rule.
        assert generate_expression(["teh"], "replacement") == r"\b(?:teh)\b"
        assert (
            generate_expression(["teh", "hte"], "replacement")
            == r"\b(?:teh|hte)\b"
        )

    def test_expression_matches_each_phrase_case_insensitively(self):
        # The catalog compiles patterns with re.IGNORECASE; the generated
        # expression must fullmatch every phrase under the same flags.
        phrases = ["editor", "code editor", "vs code"]
        compiled = re.compile(generate_expression(phrases), re.IGNORECASE)
        for phrase in phrases:
            assert compiled.fullmatch(phrase)
            assert compiled.fullmatch(phrase.upper())
        assert not compiled.fullmatch("editors")
        assert not compiled.fullmatch("the editor")

    def test_internal_whitespace_normalized_before_escaping(self):
        assert generate_expression(["  code   editor "]) == r"^(?:code\ editor)$"


class TestPhraseValidation:
    def test_valid_list_returns_none(self):
        assert validate_phrases(["editor", "code editor"]) is None

    def test_non_list_rejected(self):
        # A bare string is iterable; without the check it would silently
        # generate a per-character alternation.
        assert validate_phrases("editor") is not None
        assert validate_phrases(None) is not None

    def test_empty_list_rejected(self):
        assert validate_phrases([]) is not None

    def test_non_string_item_rejected(self):
        assert validate_phrases(["editor", 5]) is not None

    def test_blank_phrase_rejected(self):
        assert validate_phrases(["editor", "   "]) is not None
        assert validate_phrases([""]) is not None

    def test_duplicates_rejected_case_and_whitespace_insensitive(self):
        assert validate_phrases(["editor", "Editor"]) is not None
        assert validate_phrases(["code editor", "code   editor"]) is not None

    def test_quote_and_backslash_rejected(self):
        # These cannot round-trip through the basic-string TOML array the
        # manager writes (a backslash would parse as an escape sequence).
        assert validate_phrases(['say "hi"']) is not None
        assert validate_phrases(["a\\b"]) is not None

    def test_generate_expression_raises_on_invalid(self):
        with pytest.raises(ValueError):
            generate_expression([])
        with pytest.raises(ValueError):
            generate_expression(["editor", "editor"])

    def test_normalize_phrases_strips_and_collapses(self):
        assert normalize_phrases(["  code   editor ", "vs\tcode"]) == [
            "code editor", "vs code",
        ]


# ---------------------------------------------------------------------------
# Dependency-freeness (bare subprocess, stdlib only)
# ---------------------------------------------------------------------------

class TestDependencyFreeness:
    def test_imports_with_every_non_stdlib_module_blocked(self):
        """Import phrase_expression in a subprocess whose meta-path raises on
        any import that is neither stdlib nor the module itself. The GUI
        process imports this module too (spec section 4 style, same guard as
        test_action_catalog.py), so it must never grow a WheelHouse or
        third-party dependency."""
        script = f"""
import sys
import importlib.abc

ALLOWED_LOCAL = {{"speech", "speech.phrase_expression"}}

class _BlockNonStdlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ALLOWED_LOCAL:
            return None
        root = fullname.partition(".")[0]
        if root in sys.stdlib_module_names:
            return None
        raise ModuleNotFoundError(
            "blocked for dependency-freeness test: " + fullname
        )

sys.meta_path.insert(0, _BlockNonStdlib())
sys.path.insert(0, {str(_SERVICE_DIR)!r})

import speech.phrase_expression as pe

assert pe.generate_expression(["editor", "code editor"]) == \\
    r"^(?:editor|code\\ editor)$"
print("IMPORT_OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_SERVICE_DIR),
        )
        assert result.returncode == 0, result.stderr[-3000:]
        assert "IMPORT_OK" in result.stdout


# ---------------------------------------------------------------------------
# create_pattern round-trip
# ---------------------------------------------------------------------------

class TestCreateWithPhrases:
    def test_create_writes_phrases_array_and_generated_regex(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="",  # absent when phrases drive the expression
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "e"]},
            phrases=["editor", "code editor"],
        )
        assert result["success"] is True

        pats = _read_user_patterns(user_file)
        assert len(pats) == 1
        assert pats[0]["phrases"] == ["editor", "code editor"]
        expected_regex = r"^(?:editor|code\ editor)$"
        assert pats[0]["pattern"] == expected_regex
        # The id stays the SHA-256 of the regex, unchanged scheme.
        assert result["pattern_id"] == PatternManager.pattern_id(expected_regex)
        compiled = re.compile(pats[0]["pattern"], re.IGNORECASE)
        assert compiled.fullmatch("editor")
        assert compiled.fullmatch("code editor")

    def test_create_stores_normalized_phrases(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "e"]},
            phrases=["  code   editor "],
        )
        assert result["success"] is True
        pats = _read_user_patterns(user_file)
        assert pats[0]["phrases"] == ["code editor"]

    def test_create_without_phrases_writes_no_phrases_key(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
        )
        assert result["success"] is True
        pats = _read_user_patterns(user_file)
        assert "phrases" not in pats[0]

    def test_create_with_invalid_phrases_fails_before_writing(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "e"]},
            phrases=["editor", "Editor"],  # duplicate after casefold
        )
        assert result["success"] is False
        assert result["error"]
        assert not os.path.exists(user_file)


# ---------------------------------------------------------------------------
# update_pattern round-trip (the Stage 1 handoff risk: update regenerates
# blocks from create-shaped data, so phrases must survive the rewrite)
# ---------------------------------------------------------------------------

class TestUpdateWithPhrases:
    def _create_phrase_pattern(self, pm, phrases):
        result = pm.create_pattern(
            trigger="",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "e"]},
            phrases=phrases,
        )
        assert result["success"] is True
        return result["pattern_id"]

    def test_update_changes_phrases_and_keeps_array(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pid = self._create_phrase_pattern(pm, ["editor", "code editor"])

        # 'trigger' is absent: phrases drive the regenerated expression.
        result = pm.update_pattern(pid, _create_data(
            phrases=["editor", "vs code"],
        ))
        assert result["success"] is True

        pats = _read_user_patterns(user_file)
        assert len(pats) == 1
        assert pats[0]["phrases"] == ["editor", "vs code"]
        assert pats[0]["pattern"] == r"^(?:editor|vs\ code)$"
        assert result["pattern_id"] == PatternManager.pattern_id(
            r"^(?:editor|vs\ code)$"
        )

    def test_update_keeping_same_phrases_still_carries_them(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pid = self._create_phrase_pattern(pm, ["editor", "code editor"])

        result = pm.update_pattern(pid, _create_data(
            phrases=["editor", "code editor"],
            action_params={"keys": ["ctrl", "k"]},
        ))
        assert result["success"] is True
        pats = _read_user_patterns(user_file)
        assert pats[0]["phrases"] == ["editor", "code editor"]
        assert pats[0]["actions"] == [
            {"function": "hk", "params": ["ctrl", "k"]},
        ]

    def test_update_from_trigger_to_phrases(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        created = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
        )
        assert created["success"] is True

        result = pm.update_pattern(created["pattern_id"], _create_data(
            phrases=["deploy", "ship it"],
        ))
        assert result["success"] is True
        pats = _read_user_patterns(user_file)
        assert pats[0]["phrases"] == ["deploy", "ship it"]
        assert pats[0]["pattern"] == r"^(?:deploy|ship\ it)$"

    def test_update_from_phrases_to_trigger_drops_phrases_key(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pid = self._create_phrase_pattern(pm, ["editor", "code editor"])

        result = pm.update_pattern(pid, _create_data(trigger="deploy"))
        assert result["success"] is True
        pats = _read_user_patterns(user_file)
        assert "phrases" not in pats[0]
        assert pats[0]["pattern"] == r"^deploy$"

    def test_update_with_invalid_phrases_leaves_file_untouched(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pid = self._create_phrase_pattern(pm, ["editor"])
        with open(user_file, "r", encoding="utf-8") as fh:
            before = fh.read()

        result = pm.update_pattern(pid, _create_data(phrases=["", "editor"]))
        assert result["success"] is False
        assert result["error"]
        with open(user_file, "r", encoding="utf-8") as fh:
            assert fh.read() == before


# ---------------------------------------------------------------------------
# Manager-window data: get_all_patterns_structured carries phrases
# ---------------------------------------------------------------------------

class TestStructuredDataCarriesPhrases:
    def test_user_entry_includes_phrases_when_present(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "e"]},
            phrases=["editor", "code editor"],
        )
        result = pm.get_all_patterns_structured()
        entries = result["categories"]["User Patterns"]["patterns"]
        assert len(entries) == 1
        assert entries[0]["phrases"] == ["editor", "code editor"]

    def test_entries_without_phrases_omit_the_key(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
        )
        result = pm.get_all_patterns_structured()
        user_entries = result["categories"]["User Patterns"]["patterns"]
        assert "phrases" not in user_entries[0]
        # Built-in (system) entries never have one either.
        for cat, data in result["categories"].items():
            if cat == "User Patterns":
                continue
            for entry in data["patterns"]:
                assert "phrases" not in entry

    def test_hand_edited_garbage_phrases_value_is_omitted(
        self, system_file, user_file,
    ):
        # A hand-edit can write phrases = "editor" (string) or [1, 2]. The
        # manager entry omits the key instead of crashing, so the dialog
        # falls back to advanced mode (spec section 6 degradation rule).
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''^alpha$'''\n"
                'phrases = "alpha"\n'
                'actions = [{ function = "hk", params = ["f1"] }]\n'
                "\n"
                "[[pattern]]\n"
                "pattern = '''^beta$'''\n"
                "phrases = [1, 2]\n"
                'actions = [{ function = "hk", params = ["f2"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        entries = result["categories"]["User Patterns"]["patterns"]
        assert len(entries) == 2
        for entry in entries:
            assert "phrases" not in entry


# ---------------------------------------------------------------------------
# Loader tolerance: PatternCatalog ignores the unknown phrases key
# ---------------------------------------------------------------------------

class TestCatalogToleratesPhrasesKey:
    def test_user_file_with_phrases_key_loads_and_matches(
        self, system_file, user_file,
    ):
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''^(?:editor|code\\ editor)$'''\n"
                'phrases = ["editor", "code editor"]\n'
                'source = "pattern_manager"\n'
                'actions = [\n'
                '    { function = "hk", params = ["ctrl", "e"] }\n'
                ']\n'
            )
        catalog = PatternCatalog(system_file, user_file)
        # Both files load: the system 'save' pattern plus the phrases one.
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("editor")
        assert catalog.could_be_pattern_start("code")
        matches = catalog.get_matching_patterns("editor")
        assert matches
        compiled, ptype, _data = matches[0]
        assert ptype == "command"
        assert compiled.fullmatch("editor")
        assert compiled.fullmatch("code editor")
