# tests/test_pattern_manager_update.py
"""Tests for update-in-place of user patterns (wh-pattern-editor-update).

PatternManager.update_pattern rewrites the user-file block whose SHA-256 id
matches, preserving the block's position among the ``[[pattern]]`` tables --
delete+append would move it to the end and silently change user-pattern
precedence. The id is recomputed from the new content and returned so the
manager window can re-select the row. A vanished id (the pattern was edited
or deleted outside the window) fails cleanly without touching the file.

The Logic-side pm_update_pattern handler mirrors pm_create_pattern: same
success/error envelope on the GUI queue, and the same live reload/refresh on
success so the edit takes effect without a restart.

Spec: docs/plans/2026-07-09-pattern-manager-editor-design-v1.md section 7.
"""
import os
import tomllib
from unittest.mock import MagicMock

import pytest

from speech.pattern_manager import PatternManager


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


def _write_user(path, *blocks):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(blocks))


def _user_block(regex, action):
    return (
        "[[pattern]]\n"
        f"pattern = '''{regex}'''\n"
        'source = "pattern_manager"\n'
        f"actions = [{action}]\n"
    )


def _update_data(trigger, action_type="hotkey", action_params=None,
                 pattern_type="command", requires_hotword=False):
    """Build the create-shaped data dict update_pattern consumes."""
    return {
        "trigger": trigger,
        "pattern_type": pattern_type,
        "action_type": action_type,
        "action_params": action_params or {"keys": ["ctrl", "u"]},
        "requires_hotword": requires_hotword,
    }


class TestUpdatePattern:
    """update_pattern rewrites one user block in place."""

    def test_round_trip_create_update_reload(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        created = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
            requires_hotword=False,
        )
        assert created["success"] is True

        result = pm.update_pattern(
            created["pattern_id"],
            _update_data("ship it", action_params={"keys": ["ctrl", "e"]}),
        )
        assert result["success"] is True

        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        pats = data["pattern"]
        assert len(pats) == 1
        assert pats[0]["pattern"] == r"^ship\s+it$"
        assert pats[0]["actions"] == [
            {"function": "hk", "params": ["ctrl", "e"]},
        ]
        # The old content is gone entirely.
        with open(user_file, "r", encoding="utf-8") as fh:
            assert "^deploy$" not in fh.read()

    def test_hand_edited_position_key_survives_update(
        self, system_file, user_file,
    ):
        # A user can hand-edit ``position = "trailing"`` into a block to
        # make a trailing command (wh-2vz); the editor has no field for
        # it. update_pattern rebuilds the block from create-shaped data,
        # so without carrying the key forward the edit would silently turn
        # the trailing command into a regular one
        # (wh-pattern-editor-r3.1).
        _write_user(
            user_file,
            "[[pattern]]\n"
            "pattern = '''submit'''\n"
            'position = "trailing"\n'
            'source = "pattern_manager"\n'
            'actions = [{ function = "press", params = ["enter"] }]\n',
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("submit"),
            {
                "pattern_type": "replacement",
                "expression": "submit",
                "actions": [{"function": "press", "params": ["tab"]}],
            },
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert block["position"] == "trailing"
        assert block["actions"] == [
            {"function": "press", "params": ["tab"]},
        ]

    def test_non_string_position_dropped_on_update(
        self, system_file, user_file,
    ):
        # A hand-edited garbage value (``position = 5``) is meaningless to
        # the runtime (the catalog warns and treats it as leading), so the
        # rewrite drops it instead of re-writing garbage -- same
        # skip-and-degrade rule the manager applies to other hand-edited
        # garbage.
        _write_user(
            user_file,
            "[[pattern]]\n"
            "pattern = '''submit'''\n"
            "position = 5\n"
            'source = "pattern_manager"\n'
            'actions = [{ function = "press", params = ["enter"] }]\n',
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("submit"),
            {
                "pattern_type": "replacement",
                "expression": "submit",
                "actions": [{"function": "press", "params": ["tab"]}],
            },
        )
        assert result["success"] is True, result
        with open(user_file, "rb") as fh:
            block = tomllib.load(fh)["pattern"][0]
        assert "position" not in block

    def test_stale_id_wins_over_duplicate_when_edited_outside(
        self, system_file, user_file,
    ):
        # The pattern was hand-edited outside the window to a same-trigger
        # variant (casefold-equal, different SHA id): the window's id is now
        # stale AND the trigger key is "taken" by the edited block. The
        # honest failure is the stale-id one -- "changed outside this
        # window" names the actual event, while "Duplicate ... edit that
        # pattern instead" points the user at the very pattern they are
        # already editing (wh-pattern-editor-r2.3).
        pm = PatternManager(system_file, user_file)
        created = pm.create_pattern(
            trigger="deploy",
            pattern_type="command",
            action_type="hotkey",
            action_params={"keys": ["ctrl", "d"]},
        )
        assert created["success"] is True

        with open(user_file, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "^deploy$" in content
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write(content.replace("^deploy$", "^DEPLOY$"))
        with open(user_file, "rb") as fh:
            before = fh.read()

        result = pm.update_pattern(
            created["pattern_id"], _update_data("deploy"),
        )
        assert result["success"] is False
        assert "outside this window" in result["error"]
        assert "Duplicate" not in result["error"]
        with open(user_file, "rb") as fh:
            assert fh.read() == before

    def test_position_preserved_updating_middle_block(self, system_file, user_file):
        # The whole point of update-in-place: the middle block stays in the
        # middle. Delete+append would move it to the end.
        _write_user(
            user_file,
            _user_block("^one$", '{ function = "hk", params = ["f1"] }'),
            _user_block("^two$", '{ function = "hk", params = ["f2"] }'),
            _user_block("^three$", '{ function = "hk", params = ["f3"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^two$"),
            _update_data("middle new"),
        )
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert [p["pattern"] for p in data["pattern"]] == [
            "^one$", r"^middle\s+new$", "^three$",
        ]

    def test_returned_id_matches_reread_file(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        old_id = PatternManager.pattern_id("^deploy$")
        result = pm.update_pattern(old_id, _update_data("launch"))
        assert result["success"] is True
        assert result["pattern_id"] != old_id
        # The returned id is recomputed from the new content: hashing the
        # pattern string actually persisted in the file must reproduce it.
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert (
            PatternManager.pattern_id(data["pattern"][0]["pattern"])
            == result["pattern_id"]
        )

    def test_vanished_id_errors_and_file_untouched(self, system_file, user_file):
        # Stale window state: the pattern was edited or deleted outside this
        # window. Fail cleanly, write nothing (no .bak either).
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern("0" * 64, _update_data("launch"))
        assert result["success"] is False
        assert "not found" in result["error"]
        assert open(user_file, "rb").read() == before
        assert not os.path.exists(user_file + ".bak")

    def test_missing_user_file_errors(self, system_file, user_file):
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern("0" * 64, _update_data("launch"))
        assert result["success"] is False
        assert "not found" in result["error"]
        assert not os.path.exists(user_file)

    def test_validation_parity_unknown_action_type(self, system_file, user_file):
        # Whatever create rejects, update rejects: an unknown action type
        # fails both, and update leaves the file byte-identical.
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)

        create_result = pm.create_pattern(
            trigger="bogus", pattern_type="command",
            action_type="teleport", action_params={},
        )
        assert create_result["success"] is False

        update_result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"),
            _update_data("bogus", action_type="teleport", action_params={}),
        )
        assert update_result["success"] is False
        assert open(user_file, "rb").read() == before

    def test_requires_hotword_flag_toggles(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)

        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"),
            _update_data("deploy", requires_hotword=True),
        )
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            assert tomllib.load(fh)["pattern"][0]["requires_hotword"] is True

        # Turning the flag back off removes the key (same as create omitting it).
        result = pm.update_pattern(result["pattern_id"], _update_data("deploy"))
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            assert "requires_hotword" not in tomllib.load(fh)["pattern"][0]

    def test_update_does_not_touch_system_file(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        before = open(system_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        pm.update_pattern(PatternManager.pattern_id("^deploy$"), _update_data("launch"))
        assert open(system_file, "rb").read() == before

    def test_update_creates_backup(self, system_file, user_file):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"), _update_data("launch"),
        )
        assert result["success"] is True
        assert os.path.exists(user_file + ".bak")
        # The backup holds the pre-update content.
        with open(user_file + ".bak", "r", encoding="utf-8") as fh:
            assert "^deploy$" in fh.read()

    def test_update_survives_non_string_pattern_entry(self, system_file, user_file):
        # Mirror of the delete test for wh-user-patterns-split.11.1: a
        # hand-edited `pattern = 5` entry ahead of the target must not crash
        # the id search, and the index-based block walk must stay aligned so
        # the correct block is rewritten.
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
        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"), _update_data("launch"),
        )
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        # The malformed entry is untouched; only the target was rewritten.
        assert data["pattern"][0]["pattern"] == 5
        assert data["pattern"][1]["pattern"] == "^launch$"

    def test_update_matches_by_id_not_substring(self, system_file, user_file):
        # Mirror of the delete wrong-block finding: a block whose action text
        # quotes the target regex must not be rewritten in its place.
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
        result = pm.update_pattern(
            PatternManager.pattern_id("^total$"), _update_data("grand total"),
        )
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            data = tomllib.load(fh)
        assert [p["pattern"] for p in data["pattern"]] == [
            r"\breceipt\b", r"^grand\s+total$",
        ]
        # The receipt pattern's action text is untouched.
        assert data["pattern"][0]["actions"][0]["params"] == ["^total$"]

    def test_update_over_corrupt_file_friendly_error(
        self, system_file, user_file,
    ):
        # wh-pattern-editor-r0.7: a corrupt pre-existing file returns a
        # friendly error naming the file, not a cryptic parse error, and
        # nothing is modified (no .bak either).
        with open(user_file, "w", encoding="utf-8") as fh:
            fh.write("[[pattern]\npattern = broken\n")
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"), _update_data("launch"),
        )
        assert result["success"] is False
        assert user_file in result["error"]
        assert "could not be read" in result["error"]
        assert open(user_file, "rb").read() == before
        assert not os.path.exists(user_file + ".bak")

    def test_no_user_path_fails_and_writes_nothing(self, monkeypatch, tmp_path):
        # Same empty-path guard as create/delete/set_hotword
        # (wh-user-patterns-split-bulletproof.5.1).
        monkeypatch.chdir(tmp_path)
        pm = PatternManager("", "")
        result = pm.update_pattern("de" * 32, _update_data("launch"))
        assert result["success"] is False
        assert "error" in result
        assert list(tmp_path.iterdir()) == []


class TestUpdateBacktrackingProbe:
    r"""update_pattern runs the same save-time backtracking probe as
    create_pattern (wh-pattern-editor-r0.4): a pathological edit is rejected
    with the pinned message and the file is untouched."""

    def test_pathological_update_rejected_and_file_untouched(
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
            "expression": r"^(\w+\s*)+$",
            "actions": [{"function": "hk", "params": ["ctrl", "d"]}],
        })
        assert result["success"] is False
        assert result["error"] == (
            "This pattern takes too long to match and could freeze "
            "WheelHouse, so it was not saved. Simplify the expression."
        )
        assert open(user_file, "rb").read() == before
        assert not os.path.exists(user_file + ".bak")


class TestUpdateDuplicateTrigger:
    """update_pattern rejects moving a block onto another user block's
    trigger key (wh-pattern-editor-r0.1), exempting the block being updated
    so an edit can keep its own trigger."""

    def test_update_to_another_blocks_trigger_rejected(
        self, system_file, user_file,
    ):
        _write_user(
            user_file,
            _user_block("^one$", '{ function = "hk", params = ["f1"] }'),
            _user_block("^two$", '{ function = "hk", params = ["f2"] }'),
        )
        before = open(user_file, "rb").read()
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^two$"), _update_data("one"),
        )
        assert result["success"] is False
        assert "already exists" in result["error"]
        # The colliding trigger is named so the user knows which pattern
        # to edit instead.
        assert "one" in result["error"]
        assert open(user_file, "rb").read() == before

    def test_update_keeping_own_trigger_accepted(self, system_file, user_file):
        # Self-exemption: editing only the action while keeping the trigger
        # must not read as a collision with the block's own old content.
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"),
            _update_data("deploy", action_params={"keys": ["ctrl", "e"]}),
        )
        assert result["success"] is True
        with open(user_file, "rb") as fh:
            pats = tomllib.load(fh)["pattern"]
        assert len(pats) == 1
        assert pats[0]["actions"] == [
            {"function": "hk", "params": ["ctrl", "e"]},
        ]

    def test_update_raw_expression_collision_rejected(
        self, system_file, user_file,
    ):
        # The check sits at the shared _resolve_block_content seam, so the
        # advanced raw path is covered by construction.
        _write_user(
            user_file,
            _user_block("^one$", '{ function = "hk", params = ["f1"] }'),
            _user_block("^two$", '{ function = "hk", params = ["f2"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(PatternManager.pattern_id("^two$"), {
            "pattern_type": "command",
            "expression": "^one$",
            "actions": [{"function": "hk", "params": ["f2"]}],
        })
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_update_onto_builtin_trigger_accepted(self, system_file, user_file):
        # Editing a user pattern onto a built-in's trigger is the Customize
        # flow too; only USER-block collisions are rejected.
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        pm = PatternManager(system_file, user_file)
        result = pm.update_pattern(
            PatternManager.pattern_id("^deploy$"), _update_data("save"),
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Logic-side pm_update_pattern handler (style of test_pattern_manager_live_refresh)
# ---------------------------------------------------------------------------

class _FakeTextParser:
    def __init__(self):
        self.patterns = []


class _FakeSpeechHandler:
    """Minimal stand-in exposing exactly what _reload_and_refresh touches."""

    def __init__(self, patterns_file, user_patterns_file):
        from speech.pattern_catalog import PatternCatalog

        self.patterns_file = patterns_file
        self.user_patterns_file = user_patterns_file
        self.pattern_catalog = PatternCatalog(patterns_file, user_patterns_file)
        self.text_parser = _FakeTextParser()
        self.applied_hotwords = []

    def apply_hotword(self, hotword):
        self.applied_hotwords.append(hotword)


class _CapturingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def _make_controller(system_file, user_file):
    from main import LogicController

    controller = MagicMock(spec=LogicController)
    controller._handle_pattern_manager_action = (
        LogicController._handle_pattern_manager_action.__get__(controller)
    )
    handler = _FakeSpeechHandler(system_file, user_file)
    controller.service_manager = MagicMock()
    controller.service_manager.speech_handler = handler
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _CapturingQueue()
    return controller, handler


def _results(controller, action):
    return [
        m for m in controller.state_manager.state_to_gui_queue.items
        if m.get("action") == action
    ]


class TestUpdatePatternHandler:
    """pm_update_pattern mirrors pm_create_pattern: envelope + live refresh."""

    async def test_update_refreshes_live_and_reports_new_id(
        self, system_file, user_file,
    ):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_update_pattern",
            {"data": {
                "pattern_id": PatternManager.pattern_id("^deploy$"),
                "data": _update_data("launch"),
            }},
        )
        # The rewritten pattern is live: the reloaded catalog contains
        # ^launch$ and the text parser was refreshed to the same list.
        patterns = handler.pattern_catalog.get_all_patterns()
        assert any(p["compiled_pattern"].pattern == "^launch$" for p in patterns)
        assert not any(
            p["compiled_pattern"].pattern == "^deploy$" for p in patterns
        )
        assert handler.text_parser.patterns == patterns
        results = _results(controller, "pm_update_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is True
        assert (
            results[0]["data"]["pattern_id"]
            == PatternManager.pattern_id("^launch$")
        )

    async def test_update_vanished_id_reports_error_without_refresh(
        self, system_file, user_file,
    ):
        _write_user(
            user_file,
            _user_block("^deploy$", '{ function = "hk", params = ["ctrl", "d"] }'),
        )
        controller, handler = _make_controller(system_file, user_file)
        await controller._handle_pattern_manager_action(
            "pm_update_pattern",
            {"data": {"pattern_id": "0" * 64, "data": _update_data("launch")}},
        )
        results = _results(controller, "pm_update_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is False
        assert "not found" in results[0]["data"]["error"]
        # No refresh happened: the fake parser still has its initial state.
        assert handler.text_parser.patterns == []
