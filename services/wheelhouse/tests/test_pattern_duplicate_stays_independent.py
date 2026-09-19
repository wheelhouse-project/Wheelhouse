"""A rule the person created is never read as a pre-doc_id override.

wh-pattern-override-doc-id.3.3, A2. Duplicate was told apart from Customize
only by leaving the ``doc_id`` out, and to the merge a missing id means
"written before ids existed, so find the built-in by expression text". A
duplicate that kept the built-in's expression therefore took that built-in
over, and the built-in's own action stopped running. The person had pressed
Duplicate precisely to avoid that.

The fix is one key the editor writes into every block it creates:
``origin = "user"``. A block carrying it is the person's own rule and never
enters the legacy text migration, whatever its expression says.

The key is NOT written on an edit of a block that lacks it. A customization
saved before ids existed also lacks the id, and it still needs the legacy
migration to find its built-in (wh-pattern-override-doc-id.3.1). Adding the
key on an ordinary edit would turn such an override into an independent rule
and take the person's customization away.

``source = "pattern_manager"`` is a different key that cannot do this job.
The block writer has written it since 2026-03-16 (a9db2dcd), so a
customization saved before ids existed carries it too. It says which program
wrote the block; it does not say whether the block replaces a built-in.
"""

import json
import tomllib

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager


@pytest.fixture(autouse=True, scope="module")
def _close_the_bounded_regex_pool():
    """Leave no live match worker behind for the next test file.

    The tests below call ``PatternManager.create_pattern`` and
    ``PatternManager.update_pattern``, which reach ``_probe_backtracking``
    (speech/pattern_manager.py:1174 and :1452) and from there
    ``speech.safe_regex.match_bounded``, which starts a spawn-context
    multiprocessing pool (speech/safe_regex.py:71) and keeps it alive for
    the rest of the process. A Qt test file that builds a
    ``PatternManagerDialog`` after that pool exists reads a corrupted
    screen object: ``PatternManagerDialog.__init__`` gets a plain
    ``QObject`` back from ``self.screen()`` and dies on
    ``availableGeometry``. The same fixture guards
    tests/test_pattern_unbuildable_override.py for the same reason.

    This file sits before the Qt targets in the mutation gate's SELECTION,
    so without this teardown the gate's own order is one edit away from
    that failure.
    """
    yield
    from speech import safe_regex

    safe_regex.shutdown()


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'
ESC = [{"function": "hk", "params": ["esc"]}]
MINE = [{"function": "hk", "params": ["ctrl", "alt", "e"]}]

# The real shipped escape row's shape: an id, the two-phrase expression the
# phrase builder generates, and one action.
SHIPPED = (
    "[[pattern]]\n"
    'doc_id = "escape"\n'
    "pattern = '''^(?:escape|dismiss)$'''\n"
    'actions = [{ function = "hk", params = ["esc"] }]\n'
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _actions_toml(actions):
    """Render an action list as TOML inline tables.

    TOML wants bare keys inside an inline table, so json.dumps cannot write
    the whole list; it writes each value.
    """
    steps = ", ".join(
        "{{ function = {}, params = {} }}".format(
            json.dumps(step["function"]), json.dumps(step["params"]),
        )
        for step in actions
    )
    return f"actions = [{steps}]\n"


def _block(expression, *, doc_id=None, origin=None, actions=ESC):
    """One ``[[pattern]]`` table. json.dumps writes the TOML string form."""
    lines = ["[[pattern]]\n"]
    if doc_id is not None:
        lines.append(f"doc_id = {json.dumps(doc_id)}\n")
    if origin is not None:
        lines.append(f"origin = {json.dumps(origin)}\n")
    lines.append(f"pattern = {json.dumps(expression)}\n")
    lines.append(_actions_toml(actions))
    return "".join(lines)


def _catalog(tmp_path, system_text, user_text):
    return PatternCatalog(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _manager(tmp_path, user_text="", system_text=SHIPPED):
    return PatternManager(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _saved_blocks(manager):
    """The ``[[pattern]]`` tables now in the user file."""
    with open(manager.user_patterns_file, "rb") as fh:
        return tomllib.load(fh).get("pattern", [])


def _actions_of(entries):
    """The action lists of matching rules, in merged order.

    ``get_matching_patterns`` returns ``(compiled, pattern_type, data)``
    triples, so the actions live in the third member.
    """
    return [data["actions"] for _compiled, _kind, data in entries]


class TestTheEditorMarksItsOwnRules:
    """Every block ``create_pattern`` writes says who wrote it."""

    def test_a_new_rule_carries_the_key(self, tmp_path):
        manager = _manager(tmp_path)
        result = manager.create_pattern(
            expression="^screenshot$", actions=MINE,
        )
        assert result["success"] is True, result
        assert _saved_blocks(manager)[0].get("origin") == "user"

    def test_a_duplicate_carries_the_key(self, tmp_path):
        """The case this bead is about: no doc_id, the built-in's words."""
        manager = _manager(tmp_path)
        result = manager.create_pattern(
            expression="^(?:escape|dismiss)$", actions=MINE,
        )
        assert result["success"] is True, result
        block = _saved_blocks(manager)[0]
        assert block.get("origin") == "user"
        assert "doc_id" not in block

    def test_a_customize_carries_both_keys(self, tmp_path):
        manager = _manager(tmp_path)
        result = manager.create_pattern(
            expression="^(?:escape|dismiss)$", actions=MINE, doc_id="escape",
        )
        assert result["success"] is True, result
        block = _saved_blocks(manager)[0]
        assert block.get("origin") == "user"
        assert block.get("doc_id") == "escape"

    def test_an_edit_keeps_the_key_the_block_already_had(self, tmp_path):
        manager = _manager(tmp_path, _block("^screenshot$", origin="user"))
        pid = PatternManager.pattern_id("^screenshot$")
        result = manager.update_pattern(
            pid,
            {
                "expression": "^grab screen$",
                "actions": MINE,
                "pattern_type": "command",
            },
        )
        assert result["success"] is True, result
        assert _saved_blocks(manager)[0].get("origin") == "user"

    def test_an_edit_does_not_add_the_key_to_a_pre_doc_id_block(
        self, tmp_path,
    ):
        """An old customization must stay eligible for migration (.3.1).

        Adding the key here would tell the merge this block never replaced
        anything, and the person's customization would stop replacing the
        built-in the moment they edited it.
        """
        manager = _manager(tmp_path, _block("^(?:escape|dismiss)$"))
        pid = PatternManager.pattern_id("^(?:escape|dismiss)$")
        result = manager.update_pattern(
            pid,
            {
                "expression": "^(?:escape|dismiss)$",
                "actions": MINE,
                "pattern_type": "command",
            },
        )
        assert result["success"] is True, result
        assert "origin" not in _saved_blocks(manager)[0]


class TestADuplicateNoLongerTakesTheBuiltIn:
    def test_the_builtin_still_runs_its_own_action(self, tmp_path):
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block("^(?:escape|dismiss)$", origin="user", actions=MINE),
        )
        found = catalog.get_matching_patterns("escape")
        assert ESC in _actions_of(found), (
            "the built-in's own action stopped running after the person "
            "pressed Duplicate"
        )

    def test_both_rules_are_present(self, tmp_path):
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block("^(?:escape|dismiss)$", origin="user", actions=MINE),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [
            ESC, MINE,
        ]

    def test_the_listing_does_not_call_it_an_override(self, tmp_path):
        manager = _manager(
            tmp_path,
            _block("^(?:escape|dismiss)$", origin="user", actions=MINE),
        )
        rows = manager.get_all_patterns_structured()["categories"][
            "User Patterns"
        ]["patterns"]
        assert [row["overrides_builtin"] for row in rows] == [False]

    def test_the_listing_does_not_badge_it_unresolved(self, tmp_path):
        manager = _manager(
            tmp_path,
            _block("^(?:escape|dismiss)$", origin="user", actions=MINE),
        )
        rows = manager.get_all_patterns_structured()["categories"][
            "User Patterns"
        ]["patterns"]
        assert rows[0].get("unresolved_override", False) is False


class TestTheWholeDuplicateSequence:
    def test_saving_a_duplicate_leaves_the_builtin_running(self, tmp_path):
        """The reviewer's own sequence, through the real save and load.

        Duplicate of the shipped escape row, action changed and nothing
        else, saved by ``create_pattern``, then read by a real
        ``PatternCatalog`` over the same two files.
        """
        manager = _manager(tmp_path)
        result = manager.create_pattern(
            expression="^(?:escape|dismiss)$", actions=MINE,
        )
        assert result["success"] is True, result
        catalog = PatternCatalog(
            manager.patterns_file, manager.user_patterns_file,
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [
            ESC, MINE,
        ]


class TestWhatMustNotChange:
    def test_a_pre_doc_id_override_still_claims_its_builtin(self, tmp_path):
        """A1. The population this whole bead exists for."""
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block("^(?:escape|dismiss)$", actions=MINE),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [MINE]

    def test_a_customize_still_claims_its_builtin(self, tmp_path):
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block(
                "^(?:escape|dismiss)$", doc_id="escape", origin="user",
                actions=MINE,
            ),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [MINE]

    def test_a_hand_written_origin_value_is_ignored(self, tmp_path):
        """Only the exact string the editor writes counts.

        A hand-edited value is not a claim the editor made, so it must not
        be able to detach an override from its built-in.
        """
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block("^(?:escape|dismiss)$", origin="wheelhouse", actions=MINE),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [MINE]

    def test_a_block_naming_a_builtin_badly_still_falls_back_to_text(
        self, tmp_path,
    ):
        """A malformed id keeps merging exactly as it did before ids.

        ``identity_from`` records that decision: a hand-edited
        ``doc_id = "Escape Key"`` falls back to the text key rather than
        becoming an unmatched entry that quietly loses to its built-in.
        A block that names a built-in, however badly, is not an
        independent rule, so the key does not detach it.
        """
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block(
                "^(?:escape|dismiss)$", doc_id="Escape Key", origin="user",
                actions=MINE,
            ),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [MINE]

    def test_a_switched_off_duplicate_switches_nothing_off(self, tmp_path):
        """A5 belongs to overrides, not to the person's own rules.

        An empty action list on an independent rule builds nothing and
        leaves the built-in alone, rather than switching it off.
        """
        catalog = _catalog(
            tmp_path, SHIPPED,
            _block("^(?:escape|dismiss)$", origin="user", actions=[]),
        )
        assert _actions_of(catalog.get_matching_patterns("escape")) == [ESC]
