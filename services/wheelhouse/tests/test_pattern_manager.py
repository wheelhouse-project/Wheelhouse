# tests/test_pattern_manager.py
import os
import re
import tomllib
import pytest
from speech.pattern_manager import PatternManager
from speech.phrase_expression import generate_expression


# ---------------------------------------------------------------------------
# Shared fixtures for the split: a shipped system file and a writable user file.
# ---------------------------------------------------------------------------

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
    '\n'
    '[[pattern]]\n'
    "pattern = '''^zoom in$'''\n"
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "+"] }\n'
    ']\n'
    '\n'
    '# =============================================================================\n'
    '# REPLACEMENTS - Punctuation Basic\n'
    '# =============================================================================\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''\\bperiod\\b'''\n"
    'actions = [\n'
    '    { function = "text", params = [". "] }\n'
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


class TestRegexGeneration:
    """Test PatternManager.generate_regex() produces correct patterns."""

    def test_single_word_command(self):
        result = PatternManager.generate_regex("save", "command")
        assert result == r"^save$"

    def test_multi_word_command(self):
        result = PatternManager.generate_regex("save project", "command")
        assert result == r"^save\s+project$"

    def test_single_word_replacement(self):
        result = PatternManager.generate_regex("gpt", "replacement")
        assert result == r"\bgpt\b"

    def test_multi_word_replacement(self):
        result = PatternManager.generate_regex("gee pee tee", "replacement")
        assert result == r"\bgee\s+pee\s+tee\b"

    def test_special_chars_escaped(self):
        result = PatternManager.generate_regex("c++", "replacement")
        assert result == r"\bc\+\+\b"

    def test_regex_compiles(self):
        """Every generated regex must compile without error."""
        cases = [
            ("save", "command"),
            ("save project", "command"),
            ("gpt", "replacement"),
            ("c++", "replacement"),
            ("hello world", "replacement"),
        ]
        for trigger, ptype in cases:
            pattern = PatternManager.generate_regex(trigger, ptype)
            re.compile(pattern, re.IGNORECASE)  # Should not raise


class TestPatternId:
    def test_deterministic(self):
        assert PatternManager.pattern_id("^save$") == PatternManager.pattern_id("^save$")

    def test_different_patterns_different_ids(self):
        assert PatternManager.pattern_id("^save$") != PatternManager.pattern_id("^load$")

    def test_returns_hex_string(self):
        pid = PatternManager.pattern_id("^save$")
        assert len(pid) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in pid)


class TestActionGeneration:
    """Test PatternManager.generate_actions() produces correct TOML actions."""

    def test_hotkey_single(self):
        result = PatternManager.generate_actions("hotkey", {"keys": ["ctrl", "s"]})
        assert result == [{"function": "hk", "params": ["ctrl", "s"]}]

    def test_hotkey_multiple_keys(self):
        result = PatternManager.generate_actions("hotkey", {"keys": ["ctrl", "shift", "n"]})
        assert result == [{"function": "hk", "params": ["ctrl", "shift", "n"]}]

    def test_text_replacement(self):
        result = PatternManager.generate_actions("text", {"output": "GPT"})
        assert result == [{"function": "text", "params": ["GPT"]}]

    def test_run_program(self):
        result = PatternManager.generate_actions("run", {"path": "notepad.exe"})
        assert result == [{"function": "run", "params": ["notepad.exe"]}]

    def test_activate_window(self):
        result = PatternManager.generate_actions("activate", {"target": "brave.exe"})
        assert result == [{"function": "activate", "params": ["brave.exe"]}]

    def test_unknown_action_type_raises(self):
        with pytest.raises(ValueError, match="Unknown action type"):
            PatternManager.generate_actions("unknown", {})


def _write_user(path, *blocks, hotword=None):
    """Helper: write a user_patterns.toml with optional hotword and pattern blocks."""
    parts = []
    if hotword is not None:
        parts.append(f'COMMAND_HOTWORD = "{hotword}"\n')
    parts.extend(blocks)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def _user_block(regex, action, source=True):
    lines = ["[[pattern]]", f"pattern = '''{regex}'''"]
    if source:
        lines.append('source = "pattern_manager"')
    lines.append(f"actions = [{action}]")
    return "\n".join(lines) + "\n"


class TestGetAllPatternsStructured:
    """Reading both files into one categorized structure."""

    def test_system_only_returns_categories(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert "Commands - Window Management" in result["categories"]
        assert "Replacements - Punctuation Basic" in result["categories"]

    def test_system_pattern_not_user_created(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        p = result["categories"]["Commands - Window Management"]["patterns"][0]
        assert p["is_user_created"] is False

    def test_hotword_detected(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert result["hotword"] == "x-ray"

    def test_user_file_patterns_labeled_user(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert "User Patterns" in result["categories"]
        user_patterns = result["categories"]["User Patterns"]["patterns"]
        assert len(user_patterns) == 1
        assert user_patterns[0]["is_user_created"] is True
        assert user_patterns[0]["overrides_builtin"] is False

    def test_user_override_flagged(self, system_file, user_file):
        # Same trigger as the built-in ^save$.
        _write_user(
            user_file,
            _user_block("^save$", '{ function = "hk", params = ["f5"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        user_patterns = result["categories"]["User Patterns"]["patterns"]
        assert user_patterns[0]["overrides_builtin"] is True

    def test_user_hotword_override_reflected(self, system_file, user_file):
        _write_user(user_file, hotword="computer")
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert result["hotword"] == "computer"

    def test_user_hotword_override_is_stripped(self, system_file, user_file):
        # wh-user-patterns-split.8.1: the manager UI must show the normalized
        # hotword, matching what the catalog actually applies to the router.
        _write_user(user_file, hotword="  computer  ")
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert result["hotword"] == "computer"

    def test_non_dict_user_entries_do_not_crash(self, system_file, user_file):
        # Codex finding wh-user-patterns-split.10.1: a hand-edited
        # `pattern = [1, 2, 3]` (a top-level array instead of [[pattern]]
        # tables) must not crash the Pattern Manager; opening it should still
        # list the built-in patterns.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write("pattern = [1, 2, 3]\n")
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()  # must not raise
        assert "Commands - Window Management" in result["categories"]

    def test_multi_word_user_hotword_shows_system_value(self, system_file, user_file):
        # reviewer_0 (bulletproof.3.1): the catalog ignores a multi-word
        # hand-edited hotword and falls back to the system value, so the browse
        # view must show the system value too, not the unusable multi-word one.
        _write_user(user_file, hotword="hey computer")
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert result["hotword"] == "x-ray"

    def test_non_string_pattern_value_does_not_crash(self, system_file, user_file):
        # GLM finding wh-user-patterns-split.11.1: a hand-edited [[pattern]]
        # table with a non-string value (`pattern = 5`) is valid TOML but must
        # not crash the manager on `_trigger_key(5)`. The catalog skips such an
        # entry, so the manager UI skips it too while still listing valid user
        # patterns and the built-ins.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = 5\n"
                'actions = [{ function = "hk", params = ["f9"] }]\n'
                "\n"
                "[[pattern]]\n"
                "pattern = '''^deploy$'''\n"
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()  # must not raise
        assert "Commands - Window Management" in result["categories"]
        user_pats = result["categories"]["User Patterns"]["patterns"]
        assert [p["raw_pattern"] for p in user_pats] == ["^deploy$"]

    def test_position_key_passed_through(self, tmp_path, user_file):
        # The shipped file has one `position = "trailing"` pattern (submit).
        # The explainer classifies by that key, so the entry must carry it
        # (wh-pattern-editor-manager pre-work).
        sysf = tmp_path / "patterns_trailing.toml"
        sysf.write_text(
            "[[pattern]]\n"
            "pattern = '''submit'''\n"
            'position = "trailing"\n'
            'actions = [{ function = "press_keys", params = ["enter"] }]\n',
            encoding="utf-8",
        )
        pm = PatternManager(str(sysf), user_file)
        result = pm.get_all_patterns_structured()
        cats = result["categories"]
        entries = [p for c in cats.values() for p in c["patterns"]]
        assert entries[0]["position"] == "trailing"

    def test_type_key_passed_through(self, system_file, user_file):
        # Advanced-mode saves will store an explicit `type`; the explainer
        # prefers it over the anchor heuristic, so pass it through.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''^deploy$'''\n"
                'type = "command"\n'
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        user_pats = result["categories"]["User Patterns"]["patterns"]
        assert user_pats[0]["type"] == "command"

    def test_garbage_position_and_type_omitted(self, system_file, user_file):
        # Non-string values are hand-edit garbage; omit the keys so the
        # explainer falls back to the anchor heuristic instead of crashing.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''^deploy$'''\n"
                "type = 7\n"
                "position = 5\n"
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        user_pats = result["categories"]["User Patterns"]["patterns"]
        assert "type" not in user_pats[0]
        assert "position" not in user_pats[0]

    def test_whole_utterance_flag_passed_through(self, tmp_path, user_file):
        # The shipped punctuation-alias patterns carry
        # ``whole_utterance_only = true`` (wh-int8-punctuation-mishears).
        # The Customize flow rebuilds a user block from this entry, so the
        # entry must carry the flag or Customize silently strips the
        # whole-utterance safety (review finding
        # wh-int8-punctuation-mishears.1.1).
        sysf = tmp_path / "patterns_alias.toml"
        sysf.write_text(
            "[[pattern]]\n"
            "pattern = '''^colin$'''\n"
            "whole_utterance_only = true\n"
            'actions = [{ function = "text", params = [":"] }]\n',
            encoding="utf-8",
        )
        pm = PatternManager(str(sysf), user_file)
        result = pm.get_all_patterns_structured()
        entries = [
            p for c in result["categories"].values() for p in c["patterns"]
        ]
        assert entries[0]["whole_utterance_only"] is True

    def test_garbage_whole_utterance_flag_omitted(self, system_file, user_file):
        # Non-bool values are hand-edit garbage; omit the key so downstream
        # consumers never see a truthy string (same rule as position/type).
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''^deploy$'''\n"
                'whole_utterance_only = "true"\n'
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        user_pats = result["categories"]["User Patterns"]["patterns"]
        assert "whole_utterance_only" not in user_pats[0]

    def test_whole_utterance_flag_omitted_on_replacement(
        self, system_file, user_file,
    ):
        # The flag is supported only on ^-anchored command patterns; the
        # catalog disables it on replacements with a warning. Carrying it
        # into the UI entry anyway would make the manager keep re-writing
        # a flag the runtime never honors
        # (wh-int8-punctuation-mishears.1.5).
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''deploy'''\n"
                "whole_utterance_only = true\n"
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        user_pats = result["categories"]["User Patterns"]["patterns"]
        assert "whole_utterance_only" not in user_pats[0]


class TestCreatePattern:
    """create_pattern writes only to the user file; the system file is untouched."""

    def test_create_writes_to_user_file(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        assert result["success"] is True
        with open(user_file, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "^deploy$" in text
        assert 'source = "pattern_manager"' in text

    def test_create_does_not_touch_system_file(self, system_file, user_file):
        before = open(system_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        after = open(system_file, "rb").read()
        assert before == after

    def test_create_makes_user_file_when_absent(self, system_file, user_file):
        assert not os.path.exists(user_file)
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        assert os.path.exists(user_file)

    def test_create_preserves_existing_user_patterns(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^existing$", '{ function = "hk", params = ["f9"] }'),
        )
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        pats = [p["pattern"] for p in data["pattern"]]
        assert "^existing$" in pats
        assert "^deploy$" in pats

    def test_create_generates_backup_when_user_file_exists(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^existing$", '{ function = "hk", params = ["f9"] }'),
        )
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        assert os.path.exists(user_file + ".bak")

    def test_create_returns_pattern_id(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        expected_id = PatternManager.pattern_id(
            PatternManager.generate_regex("deploy", "command")
        )
        assert result["pattern_id"] == expected_id

    def test_create_with_hotword_flag(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=True,
        )
        with open(user_file, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "requires_hotword = true" in text

    def test_create_valid_toml(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert any(p["pattern"] == "^deploy$" for p in data["pattern"])

    def test_create_with_whole_utterance_flag(self, system_file, user_file):
        # Customize on a shipped punctuation alias re-creates it as a user
        # block; the flag must land in the written TOML or the override
        # loses whole-utterance safety (wh-int8-punctuation-mishears.1.1).
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="colin",
            pattern_type="command",
            action_type="text",
            action_params={"output": ":"},
            requires_hotword=False,
            whole_utterance_only=True,
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["whole_utterance_only"] is True

    def test_create_without_flag_omits_key(self, system_file, user_file):
        # Ordinary creates must not spray the key across every user block.
        pm = PatternManager(system_file, user_file)
        pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert "whole_utterance_only" not in block

    def test_create_drops_flag_on_replacement_expression(
        self, system_file, user_file,
    ):
        # A Customize of a flagged alias can be edited into an unanchored
        # replacement before saving. The flag is meaningless there (the
        # catalog disables it with a startup warning), so the manager
        # drops it instead of writing a block that violates the schema
        # restriction (wh-int8-punctuation-mishears.1.5).
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="replacement",
            expression="colin",
            actions=[{"function": "text", "params": [":"]}],
            requires_hotword=False,
            whole_utterance_only=True,
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert "whole_utterance_only" not in block


class TestDuplicateTriggerCreate:
    """create_pattern rejects a trigger-key collision with an existing USER
    block (wh-pattern-editor-r0.1). pattern_id is the sha256 of the raw
    expression, so two user blocks sharing an expression collide: the manager
    tree shows two rows with one id, the runtime merge runs the LAST block
    while delete/update target the FIRST, and edits silently stop working.
    Overriding a built-in stays allowed -- that is the Customize flow."""

    def _create(self, pm, **kwargs):
        defaults = dict(
            trigger="deploy", pattern_type="command", action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
        )
        defaults.update(kwargs)
        return pm.create_pattern(**defaults)

    def test_duplicate_simple_create_rejected(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        assert self._create(pm)["success"] is True
        result = self._create(pm, action_params={"keys": ["ctrl", "e"]})
        assert result["success"] is False
        assert "already exists" in result["error"]
        # The colliding trigger is named so the user knows which pattern
        # to edit instead.
        assert "deploy" in result["error"]
        # Only the first block was written.
        with open(user_file, "rb") as fh:
            assert len(tomllib.load(fh)["pattern"]) == 1

    def test_duplicate_raw_create_rejected(self, system_file, user_file):
        # The raw path resolves through the same seam, so an advanced-mode
        # expression equal to an existing user trigger collides too.
        pm = PatternManager(system_file, user_file)
        assert self._create(pm)["success"] is True
        result = pm.create_pattern(
            pattern_type="command",
            expression="^deploy$",
            actions=[{"function": "hk", "params": ["ctrl", "e"]}],
        )
        assert result["success"] is False
        assert "already exists" in result["error"]
        assert "deploy" in result["error"]

    def test_duplicate_check_casefolds(self, system_file, user_file):
        # The collision uses the same trigger key the catalog merge uses
        # (strip+casefold), so a case-only variant still collides.
        pm = PatternManager(system_file, user_file)
        assert self._create(pm)["success"] is True
        result = self._create(pm, trigger="Deploy")
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_overriding_builtin_still_allowed(self, system_file, user_file):
        # "save" matches the built-in ^save$: the Customize flow, working
        # as designed. Only USER-block collisions are rejected.
        pm = PatternManager(system_file, user_file)
        result = self._create(pm, trigger="save")
        assert result["success"] is True


class TestDeletePattern:
    """delete_pattern removes user patterns from the user file only."""

    @pytest.fixture
    def user_with_pattern(self, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        return user_file

    def test_delete_user_pattern(self, system_file, user_with_pattern):
        pm = PatternManager(system_file, user_with_pattern)
        pid = PatternManager.pattern_id("^deploy$")
        result = pm.delete_pattern(pid)
        assert result["success"] is True
        with open(user_with_pattern, "r", encoding="utf-8") as fh:
            text = fh.read()
        assert "^deploy$" not in text

    def test_delete_system_pattern_id_not_found(self, system_file, user_with_pattern):
        # A built-in's ID is not present in the user file.
        pm = PatternManager(system_file, user_with_pattern)
        pid = PatternManager.pattern_id("^save$")
        result = pm.delete_pattern(pid)
        assert result["success"] is False
        assert "error" in result

    def test_delete_nonexistent_pattern(self, system_file, user_with_pattern):
        pm = PatternManager(system_file, user_with_pattern)
        result = pm.delete_pattern("0" * 64)
        assert result["success"] is False
        assert "error" in result

    def test_delete_preserves_other_user_patterns(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
            _user_block("^ship$", '{ function = "hk", params = ["ctrl", "e"] }'),
        )
        pm = PatternManager(system_file, user_file)
        pm.delete_pattern(PatternManager.pattern_id("^deploy$"))
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        pats = [p["pattern"] for p in data["pattern"]]
        assert pats == ["^ship$"]

    def test_delete_does_not_touch_system_file(self, system_file, user_with_pattern):
        before = open(system_file, "rb").read()
        pm = PatternManager(system_file, user_with_pattern)
        pm.delete_pattern(PatternManager.pattern_id("^deploy$"))
        after = open(system_file, "rb").read()
        assert before == after

    def test_delete_matches_by_id_not_substring(self, system_file, user_file):
        # Workflow finding (wrong-block-deletion): delete_pattern found the
        # target correctly by SHA-256 id, but then located the text block to
        # remove by substring-matching the pattern string against every line,
        # including action params. An earlier block whose action text merely
        # quoted the target regex was deleted instead of the block that matched.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = '''\\breceipt\\b'''\n"
                'actions = [{ function = "text", params = ["^total$"] }]\n'
                "\n"
                "[[pattern]]\n"
                "pattern = '''^total$'''\n"
                'actions = [{ function = "hk", params = ["ctrl", "t"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^total$"))
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        remaining = [p["pattern"] for p in data["pattern"]]
        # The "total" command must be gone; the unrelated "receipt" pattern,
        # which only quoted "^total$" in its action text, must survive.
        assert "^total$" not in remaining
        assert "\\breceipt\\b" in remaining

    def test_delete_survives_non_string_pattern_entry(self, system_file, user_file):
        # GLM finding wh-user-patterns-split.11.1: a non-string `pattern = 5`
        # entry ahead of the target must not crash delete on pattern_id(5);
        # the valid target still deletes.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = 5\n"
                'actions = [{ function = "hk", params = ["f9"] }]\n'
                "\n"
                "[[pattern]]\n"
                "pattern = '''^deploy$'''\n"
                'actions = [{ function = "hk", params = ["ctrl", "d"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^deploy$"))
        assert result["success"] is True
        with open(user_file, "r", encoding="utf-8") as fh:
            assert "^deploy$" not in fh.read()


class TestCorruptUserFile:
    """A corrupt user file must fail loudly, not invisibly
    (wh-pattern-editor-r0.7). The structured read carries a
    ``user_file_error`` dict (pinned cross-worker contract: ``path``,
    ``error``, ``backup_path``) so the manager window can show a banner,
    and the read-modify-write methods return a friendly error naming the
    file -- instead of appending to corrupt content and surfacing a parse
    error about a file the user never edited."""

    def _corrupt(self, user_file):
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write("[[pattern]\npattern = broken\n")

    def _good_block(self):
        return _user_block(
            "^deploy$", '{ function = "hk", params = ["ctrl", "d"] }',
        )

    def test_structured_read_reports_user_file_error_without_backup(
        self, system_file, user_file,
    ):
        self._corrupt(user_file)
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        info = result["user_file_error"]
        assert info["path"] == user_file
        assert isinstance(info["error"], str) and info["error"]
        assert "\n" not in info["error"]
        # backup_path is None when no .bak exists on disk.
        assert info["backup_path"] is None
        # The rest of the structure still lists the built-ins.
        assert "Commands - Window Management" in result["categories"]

    def test_structured_read_reports_backup_path_when_bak_exists(
        self, system_file, user_file,
    ):
        self._corrupt(user_file)
        with open(user_file + ".bak", "w", encoding="utf-8") as fh:
            fh.write(self._good_block())
        pm = PatternManager(system_file, user_file)
        result = pm.get_all_patterns_structured()
        assert result["user_file_error"]["backup_path"] == user_file + ".bak"

    def test_structured_read_key_absent_when_file_parses(
        self, system_file, user_file,
    ):
        # Pinned contract: the key is ABSENT (not None) on a healthy read.
        _write_user(user_file, self._good_block())
        pm = PatternManager(system_file, user_file)
        assert "user_file_error" not in pm.get_all_patterns_structured()

    def test_structured_read_key_absent_when_file_missing(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        assert "user_file_error" not in pm.get_all_patterns_structured()

    def test_create_over_corrupt_file_friendly_error(
        self, system_file, user_file,
    ):
        self._corrupt(user_file)
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="deploy", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["ctrl", "d"]},
        )
        assert result["success"] is False
        assert user_file in result["error"]
        assert "could not be read" in result["error"]
        assert open(user_file, "rb").read() == before
        # No .bak was created from the corrupt content.
        assert not os.path.exists(user_file + ".bak")

    def test_create_over_corrupt_file_mentions_backup_and_preserves_it(
        self, system_file, user_file,
    ):
        # The last-good .bak must survive: copying the corrupt file over it
        # before the parse check would destroy the only recovery path.
        self._corrupt(user_file)
        good = self._good_block()
        with open(user_file + ".bak", "w", encoding="utf-8") as fh:
            fh.write(good)
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            trigger="deploy", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["ctrl", "d"]},
        )
        assert result["success"] is False
        assert user_file + ".bak" in result["error"]
        with open(user_file + ".bak", "r", encoding="utf-8") as fh:
            assert fh.read() == good

    def test_delete_over_corrupt_file_friendly_error(
        self, system_file, user_file,
    ):
        self._corrupt(user_file)
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^deploy$"))
        assert result["success"] is False
        assert user_file in result["error"]
        assert "could not be read" in result["error"]
        assert open(user_file, "rb").read() == before
        assert not os.path.exists(user_file + ".bak")


class TestSetHotword:
    """set_hotword writes COMMAND_HOTWORD to the user file."""

    def test_set_hotword_creates_user_file(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.set_hotword("computer")
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert data["COMMAND_HOTWORD"] == "computer"

    def test_set_hotword_replaces_existing(self, system_file, user_file):
        _write_user(user_file, hotword="computer")
        pm = PatternManager(system_file, user_file)
        pm.set_hotword("jarvis")
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert data["COMMAND_HOTWORD"] == "jarvis"

    def test_set_hotword_preserves_user_patterns(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        pm.set_hotword("computer")
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert data["COMMAND_HOTWORD"] == "computer"
        assert any(p["pattern"] == "^deploy$" for p in data["pattern"])

    def test_set_hotword_rejects_empty(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.set_hotword("   ")
        assert result["success"] is False
        assert "error" in result

    def test_set_hotword_does_not_touch_system_file(self, system_file, user_file):
        before = open(system_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        pm.set_hotword("computer")
        after = open(system_file, "rb").read()
        assert before == after

    def test_set_hotword_rejects_multi_word(self, system_file, user_file):
        # reviewer_0 (bulletproof.3.1): a multi-word wake word would be accepted
        # and then never match (the router compares against a single STT token),
        # silently disabling every hotword-gated command. Reject it, and do not
        # write the user file.
        pm = PatternManager(system_file, user_file)
        result = pm.set_hotword("hey computer")
        assert result["success"] is False
        assert "error" in result
        assert not os.path.exists(user_file)

    def test_set_hotword_over_corrupt_file_preserves_backup(
        self, system_file, user_file
    ):
        # Same rule create/update/delete follow (wh-pattern-editor-r0.7):
        # the pre-existing content is parse-checked BEFORE the .bak copy.
        # Copying first would overwrite the last-good backup with the
        # corrupt content -- destroying the exact recovery path the
        # friendly error points at.
        good_backup = 'COMMAND_HOTWORD = "computer"\n'
        with open(user_file + ".bak", "w", encoding="utf-8") as fh:
            fh.write(good_backup)
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write("[[pattern]\nbroken = \n")
        pm = PatternManager(system_file, user_file)
        result = pm.set_hotword("jarvis")
        assert result["success"] is False
        assert user_file in result["error"]
        assert ".bak" in result["error"]
        # The last-good backup is untouched.
        with open(user_file + ".bak", encoding="utf-8") as fh:
            assert fh.read() == good_backup


class TestValidation:
    """validate_pattern under the split: built-in match is an override, not a dup."""

    @pytest.fixture
    def pm(self, system_file, user_file):
        # Seed one user pattern so duplicate-of-user can be tested.
        _write_user(
            user_file,
            _user_block("^mine$", '{ function = "hk", params = ["f8"] }'),
        )
        return PatternManager(system_file, user_file)

    def test_duplicate_user_pattern_rejected(self, pm):
        result = pm.validate_pattern("mine", "command")
        assert result["valid"] is False
        assert "duplicate" in result["error"].lower()

    def test_same_trigger_as_builtin_is_override(self, pm):
        # "save" matches the built-in ^save$ -> allowed override, with a note.
        result = pm.validate_pattern("save", "command")
        assert result["valid"] is True
        assert result.get("warning") is not None
        assert "override" in result["warning"].lower()

    def test_conflict_warning_on_first_word_overlap(self, pm):
        result = pm.validate_pattern("save project", "command")
        assert result["valid"] is True
        assert result.get("warning") is not None

    def test_valid_pattern_no_warning(self, pm):
        result = pm.validate_pattern("open notepad", "command")
        assert result["valid"] is True
        assert result.get("warning") is None

    def test_empty_trigger_rejected(self, pm):
        result = pm.validate_pattern("", "command")
        assert result["valid"] is False

    def test_whitespace_only_trigger_rejected(self, pm):
        result = pm.validate_pattern("   ", "command")
        assert result["valid"] is False

    def test_validate_survives_non_dict_user_entries(self, system_file, user_file):
        # Codex finding wh-user-patterns-split.10.1: the shared reader used by
        # validate/delete must skip non-dict entries too, not raise on
        # `pat.get(...)`.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write("pattern = [1, 2, 3]\n")
        pm = PatternManager(system_file, user_file)
        result = pm.validate_pattern("deploy", "command")  # must not raise
        assert result["valid"] is True

    def test_validate_survives_non_string_pattern_value(self, system_file, user_file):
        # GLM finding wh-user-patterns-split.11.1: _load_pattern_dicts must also
        # skip dict entries whose `pattern` value is non-string, so validate does
        # not raise on `_trigger_key(5)`.
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(
                "[[pattern]]\n"
                "pattern = 5\n"
                'actions = [{ function = "hk", params = ["f9"] }]\n'
            )
        pm = PatternManager(system_file, user_file)
        result = pm.validate_pattern("deploy", "command")  # must not raise
        assert result["valid"] is True


class TestRawExpressionCreate:
    """create_pattern's raw path (wh-pattern-editor-advanced): an advanced
    save carries a raw ``expression`` plus ordered raw ``actions`` instead of
    trigger/phrases + action_type/action_params. The block stores the raw
    expression, an explicit ``type`` key, the steps verbatim, and NO
    ``phrases`` key (absence of phrases is what reopens the pattern in
    advanced mode, spec section 6)."""

    RAW_ACTIONS = [
        {"function": "hk", "params": ["ctrl", "f"]},
        {"function": "type_text", "params": ["g1"]},
    ]

    def test_create_with_expression_and_actions(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^find\s+(.+)$",
            actions=self.RAW_ACTIONS,
            requires_hotword=True,
        )
        assert result["success"] is True, result
        assert result["pattern_id"] == PatternManager.pattern_id(
            r"^find\s+(.+)$"
        )
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["pattern"] == r"^find\s+(.+)$"
        assert block["actions"] == self.RAW_ACTIONS
        assert block["type"] == "command"
        assert block["requires_hotword"] is True
        assert "phrases" not in block

    def test_expression_wins_over_phrases(self, system_file, user_file):
        # The dialog sends exactly one shape, but the contract is pinned:
        # a present expression is the raw path; phrases are ignored and NOT
        # written (a raw block must reopen in advanced mode).
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^deploy$",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
            phrases=["deploy"],
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["pattern"] == "^deploy$"
        assert "phrases" not in block

    def test_int_param_round_trips_as_toml_integer(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^triple undo$",
            actions=[{"function": "hk", "params": ["ctrl", "z", 3]}],
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["actions"][0]["params"] == ["ctrl", "z", 3]

    def test_bad_expression_is_error_envelope(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^(unclosed$",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False
        assert "does not compile" in result["error"]
        assert not os.path.exists(user_file)

    def test_empty_expression_rejected(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="   ",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    def test_command_without_anchor_rejected(self, system_file, user_file):
        # The runtime loader decides the kind from the ^ anchor alone, so a
        # declared type that contradicts the anchoring would make the stored
        # `type` key lie to the explainer. Reject; never rewrite the regex.
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"\bdeploy\b",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False
        assert "^" in result["error"]
        assert not os.path.exists(user_file)

    def test_replacement_with_anchor_rejected(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="replacement",
            expression="^deploy$",
            actions=[{"function": "text", "params": ["Deploy"]}],
        )
        assert result["success"] is False
        assert "^" in result["error"]

    def test_unknown_pattern_type_rejected(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="banana",
            expression="^deploy$",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False

    @pytest.mark.parametrize("bad_actions", [
        [],
        "not a list",
        [{"params": ["ctrl"]}],                      # missing function
        [{"function": "", "params": ["ctrl"]}],      # empty function
        [{"function": "hk", "params": "ctrl"}],      # params not a list
        [{"function": "hk", "params": [{"x": 1}]}],  # non-scalar param
        ["not a table"],
    ])
    def test_invalid_raw_actions_rejected(
        self, system_file, user_file, bad_actions,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^deploy$",
            actions=bad_actions,
        )
        assert result["success"] is False
        assert "error" in result
        assert not os.path.exists(user_file)

    def test_backslash_and_quote_params_survive_round_trip(
        self, system_file, user_file,
    ):
        # _format_toml_value writes basic strings; backslashes and quotes
        # must be escaped or the file fails TOML validation (a Windows run
        # path is the everyday case).
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^edit hosts$",
            actions=[{
                "function": "run",
                "params": ['C:\\Windows\\notepad.exe "hosts"'],
            }],
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["actions"][0]["params"] == [
            'C:\\Windows\\notepad.exe "hosts"',
        ]


class TestGroupRefValidation:
    """A whole-param g<N> reference beyond the expression's capture groups
    is rejected at the shared _resolve_block_content seam
    (wh-pattern-editor-r2.2). At runtime the command engine substitutes
    whole-param g-refs from the match context (g1..g9 pre-seeded as None),
    so an out-of-range reference silently becomes None or stays literal
    text -- a pattern that saves cleanly and lies when spoken. Embedded
    references inside longer text are NOT validated: the engine only
    replaces them when the group matched, and a substring check would
    false-flag literal text that merely contains 'g2'."""

    def test_out_of_range_group_ref_rejected_on_create(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^open (.+)$",
            actions=[{"function": "activate", "params": ["g2"]}],
        )
        assert result["success"] is False
        assert "g2" in result["error"]
        assert "1 capture group" in result["error"]
        assert not os.path.exists(user_file)

    def test_in_range_group_ref_accepted(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^open (.+)$",
            actions=[{"function": "activate", "params": ["g1"]}],
        )
        assert result["success"] is True, result

    def test_two_digit_group_ref_checked(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^open (.+)$",
            actions=[{"function": "activate", "params": ["g10"]}],
        )
        assert result["success"] is False
        assert "g10" in result["error"]

    def test_embedded_reference_not_validated(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^open (.+)$",
            actions=[{"function": "type_text", "params": ["say g2 now"]}],
        )
        assert result["success"] is True, result

    def test_update_path_covered(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        created = pm.create_pattern(
            pattern_type="command",
            expression="^deploy$",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert created["success"] is True
        result = pm.update_pattern(created["pattern_id"], {
            "pattern_type": "command",
            "expression": "^deploy$",
            "actions": [{"function": "activate", "params": ["g1"]}],
        })
        assert result["success"] is False
        assert "g1" in result["error"]
        assert "0 capture group" in result["error"]


class TestRawExpressionUpdate:
    """update_pattern accepts the same raw shape via its data dict."""

    def test_update_to_raw_drops_phrases_and_stores_type(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        created = pm.create_pattern(
            trigger="",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            phrases=["deploy"],
        )
        assert created["success"] is True

        result = pm.update_pattern(created["pattern_id"], {
            "pattern_type": "command",
            "expression": r"^deploy\s+(.+)$",
            "actions": [
                {"function": "hk", "params": ["ctrl", "d"]},
                {"function": "type_text", "params": ["g1"]},
            ],
            "requires_hotword": True,
        })
        assert result["success"] is True, result
        assert result["pattern_id"] == PatternManager.pattern_id(
            r"^deploy\s+(.+)$"
        )
        with open(user_file, "rb") as fh:
            pats = tomllib.load(fh)["pattern"]
        assert len(pats) == 1
        block = pats[0]
        assert block["pattern"] == r"^deploy\s+(.+)$"
        assert block["type"] == "command"
        assert "phrases" not in block
        assert block["actions"] == [
            {"function": "hk", "params": ["ctrl", "d"]},
            {"function": "type_text", "params": ["g1"]},
        ]

    def test_update_raw_validation_parity_leaves_file_untouched(
        self, system_file, user_file,
    ):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(PatternManager.pattern_id("^deploy$"), {
            "pattern_type": "command",
            "expression": "^(unclosed$",
            "actions": [{"function": "hk", "params": ["ctrl", "d"]}],
        })
        assert result["success"] is False
        assert "does not compile" in result["error"]
        assert open(user_file, "rb").read() == before


class TestBacktrackingProbe:
    r"""Save-time catastrophic-backtracking probe (wh-pattern-editor-r0.4).

    Advanced mode accepts any expression that compiles, and Python's re has
    no timeout: a nested-quantifier expression like ^(\w+\s*)+$ would enter
    the live catalog and be matched against EVERY utterance inside the Logic
    asyncio loop. create_pattern probes the resolved expression against a
    small adversarial corpus via safe_regex and rejects the save on timeout.
    Best-effort by design -- a probe corpus cannot catch every pathological
    pattern."""

    PATHOLOGICAL = r"^(\w+\s*)+$"
    REJECT_MESSAGE = (
        "This pattern takes too long to match and could freeze WheelHouse, "
        "so it was not saved. Simplify the expression."
    )

    def test_pathological_expression_rejected_with_pinned_message(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=self.PATHOLOGICAL,
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False
        assert result["error"] == self.REJECT_MESSAGE
        assert not os.path.exists(user_file)

    def test_normal_expression_still_saves(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^find\s+(.+)$",
            actions=[{"function": "hk", "params": ["ctrl", "f"]}],
        )
        assert result["success"] is True, result
        assert os.path.exists(user_file)

    def test_probe_runs_against_transformed_pattern(
        self, system_file, user_file,
    ):
        # The catalog compiles transform_pattern(raw) -- numeric (\d+)
        # groups become (\w+) -- so the probe must test what the runtime
        # will actually run. Raw ^(\d+)+$ fails the probe corpus instantly
        # (digits reject "a..." fast), but the transformed ^(\w+)+$
        # backtracks catastrophically; probing the raw expression would
        # let the save through and the runaway pattern into the live
        # catalog on reload (wh-pattern-editor-r4.1).
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^(\d+)+$",
            actions=[{"function": "hk", "params": ["ctrl", "d"]}],
        )
        assert result["success"] is False
        assert result["error"] == self.REJECT_MESSAGE
        assert not os.path.exists(user_file)

    def test_benign_numeric_expression_still_saves(
        self, system_file, user_file,
    ):
        # The common numeric shape must keep saving: transformed
        # ^volume (\w+)$ is linear on the probe corpus.
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression=r"^volume (\d+)$",
            actions=[{"function": "hk", "params": ["ctrl", "u"]}],
        )
        assert result["success"] is True, result


class TestUnavailableUserFile:
    """Write methods refuse cleanly when the user path resolved to "".

    Codex finding wh-user-patterns-split-bulletproof.5.1: the bulletproof.3.2
    degradation makes SpeechHandler._resolve_user_patterns_file return "" when
    the per-user data dir cannot be created (frozen-build permission/disk
    error). With user_patterns_file == "", the atomic-write path built
    tmp_file = "" + ".tmp" == ".tmp" in the *current working directory*, wrote
    it, then os.replace(".tmp", "") raised -- leaving an orphaned ".tmp" scratch
    file in CWD (dirtying the repo in a source checkout). Every write method
    must refuse up front on an empty path, writing nothing.

    Each test chdir's into an isolated tmp_path and asserts the directory stays
    empty, so a regression that drops a scratch file is caught regardless of its
    name (".tmp", ".bak", ...).
    """

    def test_create_pattern_no_path_fails_and_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        pm = PatternManager("", "")
        result = pm.create_pattern(
            "deploy", "command", "hotkey", {"keys": ["ctrl", "d"]},
        )
        assert result["success"] is False
        assert "error" in result
        assert list(tmp_path.iterdir()) == []

    def test_set_hotword_no_path_fails_and_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        pm = PatternManager("", "")
        result = pm.set_hotword("jarvis")
        assert result["success"] is False
        assert "error" in result
        assert list(tmp_path.iterdir()) == []

    def test_delete_pattern_no_path_fails_and_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        pm = PatternManager("", "")
        result = pm.delete_pattern("de" * 32)  # any 64-hex-ish id
        assert result["success"] is False
        assert "error" in result
        assert list(tmp_path.iterdir()) == []


class TestAwaitsDoneRoundTrip:
    """Steps carrying awaits_done keep it through the save seams
    (wh-pattern-editor-r8.1): the runtime waits for Input-process
    completion only when the key survives the rewrite."""

    def test_validate_raw_actions_keeps_boolean_awaits_done(self):
        steps = PatternManager._validate_raw_actions([
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": True},
        ])
        assert steps[0] == {
            "function": "hk", "params": ["ctrl", "c"], "awaits_done": True,
        }

    def test_validate_raw_actions_drops_non_boolean_awaits_done(self):
        steps = PatternManager._validate_raw_actions([
            {"function": "hk", "params": ["ctrl", "c"], "awaits_done": "yes"},
        ])
        assert "awaits_done" not in steps[0]

    def test_create_writes_awaits_done_and_reload_carries_it(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^grab line$",
            actions=[
                {"function": "hk", "params": ["ctrl", "c"],
                 "awaits_done": True},
                {"function": "hk", "params": ["end"]},
            ],
        )
        assert result["success"] is True, result
        with open(user_file, encoding="utf-8") as fh:
            content = fh.read()
        assert "awaits_done = true" in content
        data = pm.get_all_patterns_structured()
        user_entries = data["categories"]["User Patterns"]["patterns"]
        raw_actions = user_entries[0]["raw_actions"]
        assert raw_actions[0]["awaits_done"] is True
        assert "awaits_done" not in raw_actions[1]


class TestResultKeyRoundTrip:
    """Steps carrying a result key keep it through the save seams
    (wh-pattern-editor-r10.1): the runtime stores the step's return value
    in the execution context under that name (command_engine reads
    step.get("result")), so a save that strips it changes what later
    steps can substitute."""

    def test_validate_raw_actions_keeps_string_result(self):
        steps = PatternManager._validate_raw_actions([
            {"function": "capture_clipboard", "params": [],
             "result": "clip"},
        ])
        assert steps[0] == {
            "function": "capture_clipboard", "params": [],
            "result": "clip",
        }

    def test_validate_raw_actions_drops_non_string_result(self):
        steps = PatternManager._validate_raw_actions([
            {"function": "capture_clipboard", "params": [], "result": 5},
        ])
        assert "result" not in steps[0]

    def test_create_writes_result_and_reload_carries_it(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        result = pm.create_pattern(
            pattern_type="command",
            expression="^stash that$",
            actions=[
                {"function": "capture_clipboard", "params": [],
                 "result": "clip"},
                {"function": "text", "params": ["done"]},
            ],
        )
        assert result["success"] is True, result
        with open(user_file, encoding="utf-8") as fh:
            content = fh.read()
        assert 'result = "clip"' in content
        data = pm.get_all_patterns_structured()
        user_entries = data["categories"]["User Patterns"]["patterns"]
        raw_actions = user_entries[0]["raw_actions"]
        assert raw_actions[0]["result"] == "clip"
        assert "result" not in raw_actions[1]


class TestTriggerDisplayPhrases:
    """Phrase-generated expressions display as the spoken phrases, not as
    raw regex (wh-pattern-editor-r8.2)."""

    def test_single_phrase_command(self):
        expr = generate_expression(["save project"], "command")
        assert PatternManager._trigger_display(expr) == "save project"

    def test_multiple_phrases_show_alternatives(self):
        expr = generate_expression(["editor", "code editor"], "command")
        assert PatternManager._trigger_display(expr) == (
            "editor (or code editor)"
        )

    def test_replacement_phrase(self):
        expr = generate_expression(["jason"], "replacement")
        assert PatternManager._trigger_display(expr) == "jason"

    def test_phrase_with_escaped_punctuation_unescapes(self):
        expr = generate_expression(["c++ mode"], "command")
        assert PatternManager._trigger_display(expr) == "c++ mode"

    def test_non_literal_alternation_falls_back(self):
        # A hand-written nested group is not a phrase list; the display
        # must fall back without crashing or going empty.
        out = PatternManager._trigger_display(r"^(?:sa(ve|il))$")
        assert out != ""

    def test_build_entry_uses_phrases_for_display(
        self, system_file, user_file,
    ):
        pm = PatternManager(system_file, user_file)
        entry = pm._build_entry(
            {
                "pattern": generate_expression(
                    ["editor", "code editor"], "command",
                ),
                "actions": [{"function": "hk", "params": ["ctrl", "e"]}],
                "phrases": ["editor", "code editor"],
            },
            is_user_created=True, overrides_builtin=False,
        )
        assert entry["trigger_display"] == "editor (or code editor)"


USER_TWO_BLOCKS_COMMENTED_HEADER = (
    '[[pattern]]  # my favorite\n'
    "pattern = '''^alpha$'''\n"
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "a"] }\n'
    ']\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^beta$'''\n"
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "b"] }\n'
    ']\n'
)

USER_TWO_BLOCKS_SPACED_HEADER = (
    '[[ pattern ]]\n'
    "pattern = '''^alpha$'''\n"
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "a"] }\n'
    ']\n'
    '\n'
    '[[pattern]]\n'
    "pattern = '''^beta$'''\n"
    'actions = [\n'
    '    { function = "hk", params = ["ctrl", "b"] }\n'
    ']\n'
)


class TestTolerantHeaderWalks:
    """delete/update locate the right block when a hand-edited header
    carries a trailing comment or interior spacing -- both valid TOML
    that tomllib parses (wh-pattern-editor-r8.3)."""

    def _write_user(self, user_file, text):
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_delete_block_with_commented_header(
        self, system_file, user_file,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^alpha$"))
        assert result["success"] is True, result
        with open(user_file, encoding="utf-8") as fh:
            content = fh.read()
        assert "alpha" not in content
        assert "beta" in content

    def test_delete_block_with_spaced_header(self, system_file, user_file):
        self._write_user(user_file, USER_TWO_BLOCKS_SPACED_HEADER)
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^alpha$"))
        assert result["success"] is True, result
        with open(user_file, encoding="utf-8") as fh:
            content = fh.read()
        assert "alpha" not in content
        assert "beta" in content

    def test_update_second_block_after_commented_header(
        self, system_file, user_file,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^beta$"),
            {
                "pattern_type": "command",
                "expression": "^gamma$",
                "actions": [{"function": "hk", "params": ["ctrl", "g"]}],
            },
        )
        assert result["success"] is True, result
        with open(user_file, encoding="utf-8") as fh:
            content = fh.read()
        assert "'''^alpha$'''" in content   # earlier block untouched
        assert "'''^gamma$'''" in content   # target rewritten
        assert "'''^beta$'''" not in content

    def test_header_lookalike_inside_multiline_string_fails_safe(
        self, system_file, user_file,
    ):
        # A regex value spanning lines can contain a line that LOOKS like
        # a header; raw text cannot disambiguate it, so the post-locate
        # check must refuse rather than touch the wrong block.
        lookalike = (
            '[[pattern]]\n'
            "pattern = '''\n"
            '[[pattern]]\n'
            "xyz$'''\n"
            'actions = [\n'
            '    { function = "text", params = ["a"] }\n'
            ']\n'
            '\n'
            '[[pattern]]\n'
            "pattern = '''^beta$'''\n"
            'actions = [\n'
            '    { function = "text", params = ["b"] }\n'
            ']\n'
        )
        self._write_user(user_file, lookalike)
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^beta$"))
        assert result["success"] is False
        assert "Could not locate" in result["error"]
        with open(user_file, encoding="utf-8") as fh:
            assert fh.read() == lookalike  # file untouched


class TestDeleteBackupDeferred:
    """delete_pattern must not touch the .bak until every check has
    passed and a write WILL follow (wh-pattern-editor-r10.2). A refused
    delete that has already overwritten the .bak destroys the last-good
    recovery copy; create/update already defer their backups."""

    def _write_user(self, user_file, text):
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_refused_delete_leaves_existing_bak_untouched(
        self, system_file, user_file,
    ):
        # Valid TOML whose multi-line pattern string contains a
        # header-lookalike line: the post-locate check refuses the delete.
        lookalike = (
            '[[pattern]]\n'
            "pattern = '''\n"
            '[[pattern]]\n'
            "xyz$'''\n"
            'actions = [\n'
            '    { function = "text", params = ["a"] }\n'
            ']\n'
            '\n'
            '[[pattern]]\n'
            "pattern = '''^beta$'''\n"
            'actions = [\n'
            '    { function = "text", params = ["b"] }\n'
            ']\n'
        )
        self._write_user(user_file, lookalike)
        sentinel = "# last-good backup from before the hand edit\n"
        with open(user_file + ".bak", "w", encoding="utf-8") as fh:
            fh.write(sentinel)
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^beta$"))
        assert result["success"] is False
        assert "Could not locate" in result["error"]
        with open(user_file + ".bak", encoding="utf-8") as fh:
            assert fh.read() == sentinel

    def test_successful_delete_backs_up_predelete_content(
        self, system_file, user_file,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        result = pm.delete_pattern(PatternManager.pattern_id("^alpha$"))
        assert result["success"] is True, result
        with open(user_file + ".bak", encoding="utf-8") as fh:
            assert fh.read() == USER_TWO_BLOCKS_COMMENTED_HEADER

    def test_refused_create_leaves_existing_bak_untouched(
        self, system_file, user_file, monkeypatch,
    ):
        # create_pattern must hold the same invariant as the other three
        # writers: the .bak changes only when a write follows
        # (wh-pattern-editor-r11.1). The final whole-file parse cannot
        # fail through the editor today, so simulate the future bug it
        # exists to catch: a block builder emitting broken TOML.
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        sentinel = "# last-good backup from before the hand edit\n"
        with open(user_file + ".bak", "w", encoding="utf-8") as fh:
            fh.write(sentinel)
        pm = PatternManager(system_file, user_file)
        monkeypatch.setattr(
            pm, "_build_block_lines",
            lambda *a, **k: ["not = valid = toml"],
        )
        result = pm.create_pattern(
            pattern_type="command",
            expression="^gamma$",
            actions=[{"function": "text", "params": ["g"]}],
        )
        assert result["success"] is False
        with open(user_file + ".bak", encoding="utf-8") as fh:
            assert fh.read() == sentinel
        with open(user_file, encoding="utf-8") as fh:
            assert fh.read() == USER_TWO_BLOCKS_COMMENTED_HEADER


class TestCollisionCheckSameRead:
    """The duplicate-trigger check must judge the SAME file content the
    save then rewrites. A check that re-reads the file from disk can
    disagree with the content string when a hand edit lands between the
    two reads: an update could then restore a just-deleted duplicate, and
    a create could append one (wh-pattern-editor-r9.1). The stale-disk
    state is simulated by stubbing the disk re-read to return nothing."""

    def _write_user(self, user_file, text):
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_update_judges_collision_from_rewritten_content(
        self, system_file, user_file, monkeypatch,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        # The alpha block "vanishes from disk" after update read its
        # content: a disk re-read sees no collision, but the content
        # being rewritten still contains alpha.
        monkeypatch.setattr(pm, "_load_pattern_dicts", lambda path: [])
        result = pm.update_pattern(
            PatternManager.pattern_id("^beta$"),
            {
                "pattern_type": "command",
                "expression": "^alpha$",
                "actions": [{"function": "hk", "params": ["ctrl", "g"]}],
            },
        )
        assert result["success"] is False
        assert "Duplicate" in result["error"]
        with open(user_file, encoding="utf-8") as fh:
            assert fh.read() == USER_TWO_BLOCKS_COMMENTED_HEADER

    def test_create_judges_collision_from_appended_content(
        self, system_file, user_file, monkeypatch,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        monkeypatch.setattr(pm, "_load_pattern_dicts", lambda path: [])
        result = pm.create_pattern(
            pattern_type="command",
            expression="^alpha$",
            actions=[{"function": "hk", "params": ["ctrl", "x"]}],
        )
        assert result["success"] is False
        assert "Duplicate" in result["error"]
        with open(user_file, encoding="utf-8") as fh:
            assert fh.read() == USER_TWO_BLOCKS_COMMENTED_HEADER

    def test_update_self_exemption_survives_parsed_content_path(
        self, system_file, user_file,
    ):
        self._write_user(user_file, USER_TWO_BLOCKS_COMMENTED_HEADER)
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^beta$"),
            {
                "pattern_type": "command",
                "expression": "^beta$",
                "actions": [{"function": "hk", "params": ["ctrl", "n"]}],
            },
        )
        assert result["success"] is True, result
