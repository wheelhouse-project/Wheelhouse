# tests/test_pattern_customize_keeps_identity.py
"""Customize keeps the built-in's name; Duplicate makes a rule of its own.

wh-pattern-override-doc-id A2. The merge now associates a saved override with
its built-in by ``doc_id``, which only works if the id survives the trip out to
the editor and back into the user file. It did not: the block writer had no
doc_id line at all, so every customised built-in was written as a rule
identified by its expression alone -- the very association the bead removes.

The other half is that Duplicate must NOT do this. Duplicate and Customize
open the editor with the same call, so without a deliberate seam a duplicated
rule would inherit the original's identity and the two would fight over the
same built-in, with the file order deciding the winner.
"""

import tomllib

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'

BUILTIN_BEFORE = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)
BUILTIN_AFTER = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize(\\s+window)?$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)


@pytest.fixture
def files(tmp_path):
    system = tmp_path / "patterns.toml"
    user = tmp_path / "user_patterns.toml"
    system.write_text(HOTWORD + BUILTIN_BEFORE, encoding="utf-8")
    return str(system), str(user)


def _manager(files):
    return PatternManager(files[0], files[1])


def _user_blocks(files):
    with open(files[1], "rb") as fh:
        return tomllib.load(fh).get("pattern", [])


class TestTheBlockWriterPersistsTheDocId:
    def test_a_created_block_carries_the_doc_id_it_was_given(self, files):
        manager = _manager(files)
        result = manager.create_pattern(
            trigger="maximize", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["ctrl", "alt", "m"]},
            doc_id="window-maximize",
        )
        assert result["success"] is True
        assert _user_blocks(files)[0]["doc_id"] == "window-maximize"

    def test_a_created_block_without_one_writes_no_doc_id_key(self, files):
        """A rule the user invented has no shipped name to claim."""
        manager = _manager(files)
        result = manager.create_pattern(
            trigger="launch", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["win"]},
        )
        assert result["success"] is True
        assert "doc_id" not in _user_blocks(files)[0]

    @pytest.mark.parametrize(
        "value",
        ["Window Maximize", "window--maximize", "", 5, True],
        ids=["spaces", "doubled-hyphen", "empty", "int", "bool"],
    )
    def test_a_malformed_doc_id_is_refused_rather_than_written(
        self, files, value,
    ):
        """A bad id must not reach the file.

        The merge falls back to the pattern text for a malformed id, so a
        written one would sit in the file looking like an identity while
        nothing keys on it. Refusing the save is the honest answer -- the
        only way one can arrive is a caller bug or a tampered message, not
        anything the editor's own fields can produce.
        """
        manager = _manager(files)
        result = manager.create_pattern(
            trigger="launch", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["win"]},
            doc_id=value,
        )
        assert result["success"] is False
        assert "doc_id" in result["error"]

    def test_a_refused_doc_id_writes_nothing_at_all(self, files):
        """The refusal happens before the file is touched."""
        manager = _manager(files)
        manager.create_pattern(
            trigger="launch", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["win"]},
            doc_id="Window Maximize",
        )
        import os
        assert not os.path.exists(files[1])


class TestTheCustomisedCopySurvivesARewrite:
    def test_a_customised_builtin_still_wins_after_its_regex_is_rewritten(
        self, tmp_path, files,
    ):
        """A2 end to end, which is the whole point of persisting the id.

        Customize the built-in, then ship the release that rewrites the
        built-in's expression, then load the catalog. The user's copy must
        still be the one that runs.
        """
        manager = _manager(files)
        assert manager.create_pattern(
            trigger="maximize", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["ctrl", "alt", "m"]},
            doc_id="window-maximize",
        )["success"] is True

        # The release lands: same doc_id, rewritten expression.
        (tmp_path / "patterns.toml").write_text(
            HOTWORD + BUILTIN_AFTER, encoding="utf-8",
        )
        catalog = PatternCatalog(files[0], files[1])
        assert catalog.pattern_count == 1
        matches = catalog.get_matching_patterns("maximize")
        assert len(matches) == 1
        assert matches[0][2]["actions"][0]["params"] == ["ctrl", "alt", "m"]


class TestAnEditKeepsTheIdentityItHad:
    def test_updating_a_block_carries_its_doc_id_forward(self, files):
        """The id is a property of the rule, not of the edit that changed it.

        update_pattern rebuilds the block from create-shaped data, so any key
        it does not deliberately carry is lost -- which is how position and
        whole_utterance_only came to be carried the same way. Losing the id
        here would orphan the override on the next release instead of on this
        one.
        """
        manager = _manager(files)
        created = manager.create_pattern(
            trigger="maximize", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["ctrl", "alt", "m"]},
            doc_id="window-maximize",
        )
        result = manager.update_pattern(
            created["pattern_id"],
            {
                "trigger": "maximize",
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["ctrl", "alt", "x"]},
            },
        )
        assert result["success"] is True
        blocks = _user_blocks(files)
        assert blocks[0]["doc_id"] == "window-maximize"
        assert blocks[0]["actions"][0]["params"] == ["ctrl", "alt", "x"]

    def test_updating_a_block_that_never_had_one_stays_without_one(self, files):
        """A pre-doc_id user rule is not silently given an identity."""
        manager = _manager(files)
        created = manager.create_pattern(
            trigger="launch", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["win"]},
        )
        result = manager.update_pattern(
            created["pattern_id"],
            {
                "trigger": "launch",
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["win", "e"]},
            },
        )
        assert result["success"] is True
        assert "doc_id" not in _user_blocks(files)[0]

    def test_a_hand_edited_bad_doc_id_is_dropped_by_an_update(self, files):
        """Rewriting a block is the chance to stop carrying garbage.

        The same treatment position and whole_utterance_only already get: a
        wrong-typed value the runtime ignores is dropped rather than written
        back, so the rewritten block means exactly what it says.
        """
        manager = _manager(files)
        created = manager.create_pattern(
            trigger="launch", pattern_type="command",
            action_type="hotkey", action_params={"keys": ["win"]},
        )
        text = open(files[1], encoding="utf-8").read().replace(
            "[[pattern]]\n", '[[pattern]]\ndoc_id = "Window Maximize"\n', 1,
        )
        open(files[1], "w", encoding="utf-8").write(text)
        result = manager.update_pattern(
            created["pattern_id"],
            {
                "trigger": "launch",
                "pattern_type": "command",
                "action_type": "hotkey",
                "action_params": {"keys": ["win", "e"]},
            },
        )
        assert result["success"] is True
        assert "doc_id" not in _user_blocks(files)[0]
