"""Tests for the system/user patterns split in PatternCatalog.

The catalog loads a shipped system file (required COMMAND_HOTWORD, built-in
patterns) and an optional writable user file. It merges them: a user pattern
whose normalized pattern string matches a built-in replaces the built-in (user
wins); a user pattern with a new trigger is added. The hotword can be
overridden in the user file. A missing or malformed user file loads the system
patterns only.

Design: docs/superpowers/specs/2026-07-08-system-user-patterns-split-design.md
"""
import pytest
from speech.pattern_catalog import PatternCatalog


SYSTEM_TOML = (
    'COMMAND_HOTWORD = "x-ray"\n\n'
    "[[pattern]]\n"
    "pattern = '''^save$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def system_file(tmp_path):
    return _write(tmp_path / "patterns.toml", SYSTEM_TOML)


@pytest.fixture
def user_path(tmp_path):
    # A path for the user file; the file itself may or may not exist per test.
    return tmp_path / "user_patterns.toml"


def _actions_for(catalog, word):
    """Return the actions list bound to the (single) pattern for *word*."""
    matches = catalog.get_matching_patterns(word)
    assert len(matches) == 1, f"expected one pattern for {word!r}, got {len(matches)}"
    _compiled, _type, data = matches[0]
    return data["actions"]


class TestPytestHermeticity:
    def test_default_user_file_resolution_disabled_under_pytest(self, system_file):
        """A catalog built with no explicit user path must not resolve the
        developer's personal data/user_patterns.toml while tests run.

        Several test files construct PatternCatalog(<system file>) with no
        second argument. Outside pytest that default resolves to the real
        per-machine user file; inside pytest an autouse conftest guard must
        force it to "" (no user file), or every such test becomes dependent
        on the developer's personal patterns.
        """
        catalog = PatternCatalog(system_file)
        assert catalog._user_patterns_file == ""


class TestEntryIdentityKeys:
    """all_patterns entries carry raw_pattern and is_user identity keys.

    The try-it messages (wh-pattern-editor-test-messages) identify the
    matched pattern by hashing its ORIGINAL expression so the answer can be
    matched to the manager tree's ids. Numeric patterns are transformed at
    load ((\\d+) becomes (\\w+)), so hashing the compiled string would give
    a different id for those nine shipped patterns; the entry must carry
    the pre-transform string. is_user marks entries from the user file so
    the try-it answer can show the user badge.
    """

    def test_entries_carry_raw_pattern_pre_transform(self, system_file, tmp_path):
        system = _write(
            tmp_path / "sys2.toml",
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''^delete (\\d+)$'''\n"
            'actions = [{ function = "press", params = ["backspace", "g1"] }]\n',
        )
        catalog = PatternCatalog(system, "")
        entries = catalog.get_all_patterns()
        assert len(entries) == 1
        entry = entries[0]
        # The ORIGINAL expression, not the transformed (\w+) compile form.
        assert entry["raw_pattern"] == r"^delete (\d+)$"
        assert entry["compiled_pattern"].pattern != entry["raw_pattern"]

    def test_is_user_flags_user_entries_only(self, system_file, user_path):
        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = '''^editor$'''\n"
            'actions = [{ function = "activate", params = ["code.exe"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        by_raw = {e["raw_pattern"]: e for e in catalog.get_all_patterns()}
        assert by_raw["^save$"]["is_user"] is False
        assert by_raw["^editor$"]["is_user"] is True


class TestUserMerge:
    def test_user_file_adds_new_command(self, system_file, user_path):
        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("save")
        assert catalog.could_be_pattern_start("launch")

    def test_user_overrides_builtin_same_trigger(self, system_file, user_path):
        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        # Replace-in-place: still one 'save' pattern, and it is the user's.
        assert catalog.pattern_count == 1
        assert _actions_for(catalog, "save") == [
            {"function": "hk", "params": ["f5"]}
        ]

    def test_override_preserves_position_in_merged_order(self, tmp_path):
        # Workflow finding (ordering): replacement patterns are order-sensitive,
        # and _merge_entries replaces an overridden built-in in place to keep its
        # position. Lock that behavior: overriding the middle of three patterns
        # must keep it in the middle (not move it to the end), with the user's
        # action. A remove-then-append regression would move bravo last and this
        # test would catch it.
        system = _write(
            tmp_path / "patterns.toml",
            'COMMAND_HOTWORD = "x-ray"\n\n'
            "[[pattern]]\n"
            "pattern = '''\\balpha\\b'''\n"
            'actions = [{ function = "text", params = ["A"] }]\n'
            "\n[[pattern]]\n"
            "pattern = '''\\bbravo\\b'''\n"
            'actions = [{ function = "text", params = ["B"] }]\n'
            "\n[[pattern]]\n"
            "pattern = '''\\bcharlie\\b'''\n"
            'actions = [{ function = "text", params = ["C"] }]\n',
        )
        user = _write(
            tmp_path / "user_patterns.toml",
            "[[pattern]]\n"
            "pattern = '''\\bbravo\\b'''\n"
            'actions = [{ function = "text", params = ["B-user"] }]\n',
        )
        catalog = PatternCatalog(system, str(user))
        assert catalog.pattern_count == 3
        order = [p["compiled_pattern"].pattern for p in catalog.get_all_patterns()]
        i_a, i_b, i_c = (
            order.index(r"\balpha\b"),
            order.index(r"\bbravo\b"),
            order.index(r"\bcharlie\b"),
        )
        assert i_a < i_b < i_c, f"override moved out of position: {order}"
        # The middle pattern is the user's override, still in the middle.
        bravo = catalog.get_all_patterns()[i_b]
        assert bravo["actions"] == [{"function": "text", "params": ["B-user"]}]

    def test_missing_user_file_loads_system_only(self, system_file, user_path):
        # user_path does not exist.
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.pattern_count == 1
        assert catalog.could_be_pattern_start("save")

    def test_malformed_user_file_loads_system_only_with_warning(
        self, system_file, user_path, caplog
    ):
        _write(user_path, "this is not valid toml {{{")
        with caplog.at_level("WARNING"):
            catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.pattern_count == 1
        assert catalog.could_be_pattern_start("save")
        assert any("user" in msg.lower() for msg in caplog.messages)

    def test_bad_user_entry_skipped_rest_loads(self, system_file, user_path):
        _write(
            user_path,
            # First entry has an invalid regex (unclosed group) -> skipped.
            "[[pattern]]\n"
            "pattern = '''^(unclosed$'''\n"
            'actions = [{ function = "hk", params = ["f1"] }]\n'
            "\n[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        # system save + user launch; the bad entry does not count.
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("launch")

    def test_user_entry_error_names_the_user_file(
        self, system_file, user_path, caplog
    ):
        # wh-user-patterns-split.9.1: a bad USER entry must be logged against
        # the user file path, not the shipped system file, so a user
        # debugging a hand-edit is pointed at the right file.
        _write(
            user_path,
            "[[pattern]]\npattern = 5\n"
            'actions = [{ function = "hk", params = ["f1"] }]\n',
        )
        with caplog.at_level("ERROR"):
            PatternCatalog(system_file, str(user_path))
        skip_msgs = [m for m in caplog.messages if "non-string" in m.lower()]
        assert skip_msgs, "expected a non-string skip log"
        assert all(str(user_path) in m for m in skip_msgs)

    def test_non_dict_user_entries_are_not_fatal(self, system_file, user_path):
        # wh-user-patterns-split.9.1: `pattern = [1, 2, 3]` as a top-level
        # array (a hand-edit that skipped the [[pattern]] table syntax) yields
        # non-dict entries. They must be skipped, not crash the merge/build.
        _write(user_path, "pattern = [1, 2, 3]\n")
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.pattern_count == 1  # system 'save' still loads
        assert catalog.could_be_pattern_start("save")

    def test_non_string_user_pattern_is_not_fatal(self, system_file, user_path):
        # wh-user-patterns-split.8.2: valid TOML but 'pattern' is an integer
        # (a hand-edit that forgot the quotes). It must be skipped like any
        # bad entry, NOT raise AttributeError and wipe the whole catalog.
        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = 5\n"
            'actions = [{ function = "hk", params = ["f1"] }]\n'
            "\n[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        # The bad entry is skipped; system 'save' + user 'launch' still load.
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("save")
        assert catalog.could_be_pattern_start("launch")


class TestHotwordOverride:
    def test_user_hotword_overrides_system(self, system_file, user_path):
        _write(user_path, 'COMMAND_HOTWORD = "computer"\n')
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.command_hotword == "computer"
        assert catalog.pattern_count == 1  # system save still present

    def test_empty_user_hotword_falls_back_with_warning(
        self, system_file, user_path, caplog
    ):
        _write(user_path, 'COMMAND_HOTWORD = ""\n')
        with caplog.at_level("WARNING"):
            catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.command_hotword == "x-ray"
        assert any("hotword" in msg.lower() for msg in caplog.messages)

    def test_user_hotword_whitespace_is_stripped(self, system_file, user_path):
        # wh-user-patterns-split.8.1: a hand-edited hotword with a stray space
        # passes the non-empty check but the router matches on an exact
        # lowercased token, so " computer " would never fire. Normalize it.
        _write(user_path, 'COMMAND_HOTWORD = "  computer  "\n')
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.command_hotword == "computer"

    def test_multi_word_user_hotword_falls_back_to_system(
        self, system_file, user_path, caplog
    ):
        # reviewer_0 (bulletproof.3.1): the router matches the hotword against a
        # single STT token by exact equality, so a multi-word value can never
        # fire and would silently disable every hotword-gated command. A
        # hand-edited multi-word hotword must be ignored in favor of the system
        # value, not applied.
        _write(user_path, 'COMMAND_HOTWORD = "hey computer"\n')
        with caplog.at_level("WARNING"):
            catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.command_hotword == "x-ray"
        assert any("hotword" in msg.lower() for msg in caplog.messages)


class TestReloadWithUserFile:
    def test_reload_picks_up_user_changes(self, system_file, user_path):
        _write(user_path, "")  # empty user file to start
        catalog = PatternCatalog(system_file, str(user_path))
        assert catalog.pattern_count == 1

        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        assert catalog.reload() is True
        assert catalog.pattern_count == 2
        assert catalog.could_be_pattern_start("launch")

    def test_delete_override_restores_builtin(self, system_file, user_path):
        _write(
            user_path,
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["f5"] }]\n',
        )
        catalog = PatternCatalog(system_file, str(user_path))
        assert _actions_for(catalog, "save") == [
            {"function": "hk", "params": ["f5"]}
        ]

        # User removes the override -> empty user file -> built-in returns.
        _write(user_path, "")
        assert catalog.reload() is True
        assert catalog.pattern_count == 1
        assert _actions_for(catalog, "save") == [
            {"function": "hk", "params": ["ctrl", "s"]}
        ]
