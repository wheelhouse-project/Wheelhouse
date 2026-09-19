"""A rule that cannot run does not take a built-in's place.

wh-pattern-override-doc-id.3.4. The merge decides which built-in a user
entry replaces before the catalog tries to build anything, so an entry that
named a built-in by ``doc_id`` took its slot and was only then dropped by
the build. The built-in stopped answering, and the Pattern Manager still
showed the broken entry as overriding it.

``speech.pattern_identity`` already refused an entry whose ``pattern`` is
not a string (.2.3). These tests cover the four STRING expressions the
build also refuses, and hold the shared predicate to the build's own
answer so the two cannot drift.

What must not change: an override with a valid expression and no actions
is how a user switches a built-in off (A5). It keeps the slot.
"""

import json
import tomllib

import pytest

from speech.pattern_buildable import can_build_expression, slot_identity
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
    ``availableGeometry``. Two other files in this suite record the same
    pairing -- the module fixture in tests/test_pattern_tester_doc_id.py
    and the class docstring in tests/test_pattern_customize_vs_duplicate.py.
    ``shutdown`` is the module's own hygiene entry point.

    This file sits before the two Qt targets in the mutation gate's
    SELECTION, so without this teardown the gate's own order is one edit
    away from the failure (Boss e7 ruling, 2026-09-06).
    """
    yield
    from speech import safe_regex

    safe_regex.shutdown()


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'
ACTIONS = 'actions = [{ function = "hk", params = ["win", "up"] }]\n'

BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)


def _block(
    expression, *, doc_id=None, actions=ACTIONS, position=None,
    requires_hotword=None,
):
    """One ``[[pattern]]`` table. json.dumps writes the TOML string form."""
    lines = ["[[pattern]]\n"]
    if doc_id is not None:
        lines.append(f'doc_id = "{doc_id}"\n')
    lines.append(f"pattern = {json.dumps(expression)}\n")
    if position is not None:
        lines.append(f'position = "{position}"\n')
    if requires_hotword is not None:
        lines.append(f"requires_hotword = {str(requires_hotword).lower()}\n")
    lines.append(actions)
    return "".join(lines)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _catalog(tmp_path, system_text, user_text):
    return PatternCatalog(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _first_entry(block):
    """The raw dict the catalog and the predicate both work on."""
    return tomllib.loads(block)["pattern"][0]


# Each case is (block-building kwargs, does the build produce a rule?).
# The expected column is asserted against the real build below, so a wrong
# entry here fails rather than teaching the predicate the wrong answer.
CASES = [
    pytest.param({"expression": ""}, False, id="empty-expression"),
    pytest.param({"expression": "["}, False, id="invalid-regex"),
    pytest.param(
        {"expression": "^two words$", "position": "trailing"},
        False,
        id="trailing-more-than-one-word",
    ),
    pytest.param(
        {
            "expression": "^escape$",
            "position": "trailing",
            "requires_hotword": True,
        },
        False,
        id="trailing-requires-hotword",
    ),
    pytest.param({"expression": "^maximize$"}, True, id="ordinary-command"),
    pytest.param({"expression": "teh"}, True, id="replacement-no-anchor"),
    pytest.param(
        {"expression": "^submit$", "position": "trailing"},
        True,
        id="trailing-one-word",
    ),
]


class TestThePredicateMatchesTheBuild:
    """The mirror. ``can_build_expression`` answers before the build runs.

    It reproduces four of the build's rejection rules from the entry
    alone, so it can be wrong only by drifting from them. Each case is put
    to the real ``PatternCatalog`` as well, and the two answers must agree.
    """

    @pytest.mark.parametrize("kwargs,builds", CASES)
    def test_the_predicate_agrees_with_the_catalog(
        self, tmp_path, kwargs, builds,
    ):
        block = _block(**kwargs)
        catalog = _catalog(tmp_path, block, "")
        assert catalog.pattern_count == (1 if builds else 0), (
            "the expected column is wrong for this case: the real build "
            f"produced {catalog.pattern_count} rules"
        )
        assert can_build_expression(_first_entry(block)) is builds

    def test_a_non_table_cannot_build(self):
        """A hand edit can put a list into the pattern array."""
        assert can_build_expression([1, 2, 3]) is False

    def test_a_non_string_expression_cannot_build(self):
        """The .2.3 case, answered here as well rather than only upstream."""
        assert can_build_expression({"pattern": 5}) is False

    def test_a_whitespace_expression_can_build(self):
        """Not every odd expression is unbuildable, and this one is not.

        ``"   "`` compiles and matches whitespace, so the build does
        produce a rule for it. Calling it unbuildable would take a slot
        away from an entry that runs.
        """
        assert can_build_expression({"pattern": "   "}) is True

    def test_actions_are_not_part_of_the_question(self):
        """A5: an actions=[] override still takes its built-in's slot."""
        assert can_build_expression(
            {"doc_id": "window-maximize", "pattern": "^maximize$",
             "actions": []},
        ) is True
        assert slot_identity(
            {"doc_id": "window-maximize", "pattern": "^maximize$",
             "actions": []},
        ) == ("doc", "window-maximize")


class TestTheBuiltinKeepsAnsweringUnderABrokenOverride:
    """The defect, stated as a test, once per reachable expression.

    Each user entry names the built-in by its doc_id and carries a valid
    action list, so nothing but the expression is wrong.
    """

    @pytest.mark.parametrize(
        "kwargs,builds",
        [case for case in CASES if case.values[1] is False],
    )
    def test_the_builtin_still_responds(self, tmp_path, kwargs, builds):
        user = _block(doc_id="window-maximize", **kwargs)
        catalog = _catalog(tmp_path, BUILTIN, user)
        matches = catalog.get_matching_patterns("maximize")
        assert len(matches) == 1, (
            "the broken override took the built-in's slot and was then "
            "dropped by the build, so the command has no responder"
        )
        _compiled, _type, data = matches[0]
        assert data["actions"][0]["params"] == ["win", "up"]

    @pytest.mark.parametrize(
        "kwargs,builds",
        [case for case in CASES if case.values[1] is False],
    )
    def test_the_manager_does_not_call_it_an_override(
        self, tmp_path, kwargs, builds,
    ):
        """The badge asks the same question, so it cannot say otherwise."""
        user = _block(doc_id="window-maximize", **kwargs)
        manager = PatternManager(
            _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN),
            _write(tmp_path / "user_patterns.toml", HOTWORD + user),
        )
        entries = manager.get_all_patterns_structured()[
            "categories"
        ]["User Patterns"]["patterns"]
        assert len(entries) == 1
        assert entries[0]["overrides_builtin"] is False, (
            "a rule that cannot run must not be listed as replacing a "
            "built-in that is still answering"
        )


class TestWhatMustNotChange:
    def test_a_working_override_still_replaces_its_builtin(self, tmp_path):
        """The bounding case: an ordinary override is untouched."""
        user = _block(
            "^maximize$",
            doc_id="window-maximize",
            actions='actions = [{ function = "hk", params = ["ctrl", "m"] }]\n',
        )
        catalog = _catalog(tmp_path, BUILTIN, user)
        assert catalog.pattern_count == 1
        _compiled, _type, data = catalog.get_matching_patterns("maximize")[0]
        assert data["actions"][0]["params"] == ["ctrl", "m"]

    def test_a_switched_off_builtin_stays_switched_off(self, tmp_path):
        """A5 through the whole catalog, not only the predicate."""
        user = _block(
            "^maximize$", doc_id="window-maximize", actions="actions = []\n",
        )
        catalog = _catalog(tmp_path, BUILTIN, user)
        assert catalog.pattern_count == 0
        assert catalog.get_matching_patterns("maximize") == []

    def test_the_manager_still_badges_a_working_override(self, tmp_path):
        user = _block(
            "^maximize$",
            doc_id="window-maximize",
            actions='actions = [{ function = "hk", params = ["ctrl", "m"] }]\n',
        )
        manager = PatternManager(
            _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN),
            _write(tmp_path / "user_patterns.toml", HOTWORD + user),
        )
        entries = manager.get_all_patterns_structured()[
            "categories"
        ]["User Patterns"]["patterns"]
        assert entries[0]["overrides_builtin"] is True
