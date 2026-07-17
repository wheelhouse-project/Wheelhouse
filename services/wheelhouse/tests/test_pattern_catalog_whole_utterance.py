# tests/test_pattern_catalog_whole_utterance.py
"""Strict boolean validation of the ``whole_utterance_only`` pattern flag.

``whole_utterance_only`` marks sound-alike punctuation aliases that may fire
only when they match the entire utterance (wh-int8-punctuation-mishears).
The catalog must enable it only for a real TOML boolean ``true``: valid TOML
of the wrong type (a string, an integer) is a hand-edit mistake and must
degrade to disabled with a warning -- in BOTH rebuilt representations (the
routing pattern data and the get_all_patterns listing) so the two can never
disagree (review finding wh-int8-punctuation-mishears.1.2).

The user-override case matters most: a same-trigger user pattern REPLACES
the shipped alias at load, so a user override that carries the flag must
keep whole-utterance safety (review finding wh-int8-punctuation-mishears.1.1).
"""
import logging

import pytest

from speech.pattern_catalog import PatternCatalog


def _system_toml(flag_line: str) -> str:
    return (
        'COMMAND_HOTWORD = "x-ray"\n\n'
        "[[pattern]]\n"
        "pattern = '''^colin$'''\n"
        f"{flag_line}"
        'actions = [{ function = "text", params = [":"] }]\n'
    )


def _build(tmp_path, flag_line: str) -> PatternCatalog:
    path = tmp_path / "patterns.toml"
    path.write_text(_system_toml(flag_line), encoding="utf-8")
    return PatternCatalog(str(path))


def _routing_flag(catalog: PatternCatalog) -> bool:
    matches = catalog.get_matching_patterns("colin")
    assert len(matches) == 1, f"expected one pattern, got {len(matches)}"
    _compiled, _type, data = matches[0]
    return bool(data.get("whole_utterance_only"))


def _listing_flag(catalog: PatternCatalog) -> bool:
    entries = [
        p for p in catalog.get_all_patterns()
        if p["raw_pattern"] == "^colin$"
    ]
    assert len(entries) == 1
    return entries[0]["whole_utterance_only"]


class TestWholeUtteranceFlagValidation:
    def test_boolean_true_enables_both_representations(self, tmp_path):
        catalog = _build(tmp_path, "whole_utterance_only = true\n")
        assert _routing_flag(catalog) is True
        assert _listing_flag(catalog) is True

    def test_absent_disables(self, tmp_path):
        catalog = _build(tmp_path, "")
        assert _routing_flag(catalog) is False
        assert _listing_flag(catalog) is False

    def test_boolean_false_disables(self, tmp_path):
        catalog = _build(tmp_path, "whole_utterance_only = false\n")
        assert _routing_flag(catalog) is False
        assert _listing_flag(catalog) is False

    def test_string_false_disables(self, tmp_path):
        # The string "false" is truthy in Python; truthiness would silently
        # ENABLE the flag for a value that reads as disabled.
        catalog = _build(tmp_path, 'whole_utterance_only = "false"\n')
        assert _routing_flag(catalog) is False
        assert _listing_flag(catalog) is False

    def test_string_true_disables(self, tmp_path):
        # A quoted "true" is a hand-edit mistake, not a boolean; strict
        # validation refuses it the same as any other string.
        catalog = _build(tmp_path, 'whole_utterance_only = "true"\n')
        assert _routing_flag(catalog) is False
        assert _listing_flag(catalog) is False

    def test_integer_disables(self, tmp_path):
        catalog = _build(tmp_path, "whole_utterance_only = 1\n")
        assert _routing_flag(catalog) is False
        assert _listing_flag(catalog) is False

    def test_wrong_type_logs_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            _build(tmp_path, 'whole_utterance_only = "true"\n')
        assert any(
            "whole_utterance_only" in record.getMessage()
            for record in caplog.records
        ), "expected a warning naming the malformed flag"

    def test_boolean_true_logs_nothing(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            _build(tmp_path, "whole_utterance_only = true\n")
        assert not any(
            "whole_utterance_only" in record.getMessage()
            for record in caplog.records
        )


def _build_replacement(tmp_path, flag_line: str) -> PatternCatalog:
    """A catalog whose one pattern is UNANCHORED (a replacement)."""
    path = tmp_path / "patterns.toml"
    path.write_text(
        'COMMAND_HOTWORD = "x-ray"\n\n'
        "[[pattern]]\n"
        "pattern = '''colin'''\n"
        f"{flag_line}"
        'actions = [{ function = "text", params = [":"] }]\n',
        encoding="utf-8",
    )
    return PatternCatalog(str(path))


class TestFlagRestrictedToCommands:
    """The flag is honored only on anchored command patterns.

    The router's whole-utterance gates exist only on the command paths;
    a replacement executes without consulting the flag (the immediate
    single-word replacement execute and the replacement finalization).
    Accepting the flag on a replacement would therefore promise safety
    the runtime does not deliver, so the catalog disables it with a
    warning instead (review finding wh-int8-punctuation-mishears.1.4).
    """

    def test_flag_on_replacement_disabled_in_routing_data(self, tmp_path):
        catalog = _build_replacement(
            tmp_path, "whole_utterance_only = true\n",
        )
        matches = catalog.get_matching_patterns("colin")
        assert len(matches) == 1
        _compiled, ptype, data = matches[0]
        assert ptype == "replacement"
        assert data.get("whole_utterance_only", False) is False

    def test_flag_on_replacement_disabled_in_listing(self, tmp_path):
        catalog = _build_replacement(
            tmp_path, "whole_utterance_only = true\n",
        )
        entries = [
            p for p in catalog.get_all_patterns()
            if p["raw_pattern"] == "colin"
        ]
        assert len(entries) == 1
        assert entries[0]["whole_utterance_only"] is False

    def test_flag_on_replacement_logs_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            _build_replacement(tmp_path, "whole_utterance_only = true\n")
        assert any(
            "whole_utterance_only" in record.getMessage()
            for record in caplog.records
        ), "expected a warning naming the unsupported flag placement"

    def test_unflagged_replacement_logs_nothing(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            _build_replacement(tmp_path, "")
        assert not any(
            "whole_utterance_only" in record.getMessage()
            for record in caplog.records
        )


class TestUserOverrideKeepsFlag:
    """A same-trigger user override carrying the flag keeps it at load.

    This is the load half of the Customize round-trip (review finding
    wh-int8-punctuation-mishears.1.1): the Pattern Manager writes the flag
    into the user block, and the catalog merge must honor it on the
    override that replaces the shipped alias.
    """

    def test_user_override_with_flag_is_whole_utterance_only(self, tmp_path):
        system = tmp_path / "patterns.toml"
        system.write_text(
            _system_toml("whole_utterance_only = true\n"), encoding="utf-8",
        )
        user = tmp_path / "user_patterns.toml"
        user.write_text(
            "[[pattern]]\n"
            "pattern = '''^colin$'''\n"
            "whole_utterance_only = true\n"
            'source = "pattern_manager"\n'
            'actions = [{ function = "text", params = ["::"] }]\n',
            encoding="utf-8",
        )
        catalog = PatternCatalog(str(system), str(user))
        matches = catalog.get_matching_patterns("colin")
        assert len(matches) == 1
        _compiled, _type, data = matches[0]
        # The user override's actions won (":" became "::")...
        assert data["actions"] == [{"function": "text", "params": ["::"]}]
        # ...and the safety flag survived the override.
        assert data.get("whole_utterance_only") is True

    def test_user_override_without_flag_loses_it_by_design(self, tmp_path):
        # The merge is replace-not-merge: an override that omits the flag
        # turns it off. That is the documented escape for a user who WANTS
        # "colin" back as plain dictation -- not an accident. This test
        # pins the semantics so a future "merge the flag from the shipped
        # entry" change is a deliberate decision, not drift.
        system = tmp_path / "patterns.toml"
        system.write_text(
            _system_toml("whole_utterance_only = true\n"), encoding="utf-8",
        )
        user = tmp_path / "user_patterns.toml"
        user.write_text(
            "[[pattern]]\n"
            "pattern = '''^colin$'''\n"
            'source = "pattern_manager"\n'
            'actions = [{ function = "text", params = ["::"] }]\n',
            encoding="utf-8",
        )
        catalog = PatternCatalog(str(system), str(user))
        matches = catalog.get_matching_patterns("colin")
        assert len(matches) == 1
        _compiled, _type, data = matches[0]
        assert data.get("whole_utterance_only", False) is False
