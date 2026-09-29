"""The person's own rules run before the built-in rules.

wh-user-rule-precedence, David's answer to QUESTIONS-2026-09-28.md item 12,
option three, and the replacements answer (boss chat, 12:23 2026-09-28).

Before this bead, every user entry that took no built-in's slot was
appended after every shipped entry. A trigger moved onto words another
built-in owns never ran: the built-in answered first and nothing told the
person why. The same held for an added rule, a duplicate, and a user
replacement rule.

Now the merge puts every user entry that holds no built-in slot in front of
every shipped entry, in user-file order (acceptance items 1 and 2). A user
entry that takes a built-in's slot -- an edit that kept its words, a
disabled override, an unambiguous pre-doc_id customization -- keeps that
slot (item 3, boss ruling R1). No mark in the block decides any of this,
so no save writes one (item 5).

Every save of a rule that will stand in front (Add, Duplicate, a moved
edit) copies ``whole_utterance_only`` from the built-in whose words it
takes, and keeps its own ``requires_hotword`` checkbox value. A save that
keeps a slot keeps the stored value. The Try-it preview makes the same
decision (item 4, boss ruling R2).
"""

import itertools
import os
import tomllib

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager
from speech.pattern_matcher import PatternMatcher
from speech.pattern_tester import run_test_draft, run_test_phrase


@pytest.fixture(autouse=True, scope="module")
def _close_the_bounded_regex_pool():
    """Stop the match pool ``run_test_draft`` starts (see the same fixture
    in tests/test_pattern_tester_save_agreement.py for the hazard)."""
    yield
    from speech import safe_regex

    safe_regex.shutdown()


SHIPPED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "speech", "config", "patterns.toml",
)

# The key the marker design wrote. It is no longer written or read.
OLD_MARKER = "before_builtins"

# Two real shipped commands. "show desktop" is the rule the person edits;
# "select all" is owned by a different built-in that sits later in the
# shipped file, and the edit moves the trigger onto its words.
SHOW_DESKTOP_ID = "show-desktop"
SHOW_DESKTOP = "^(?:show (?:the )?)?desktop$"
SELECT_ALL = "^select all$"


def _data(expression, output="CUSTOM"):
    return {
        "expression": expression,
        "pattern_type": "command",
        "action_type": "text",
        "action_params": {"output": output},
    }


def _typed(steps):
    return [s["params"][0] for s in steps if s.get("function") == "text"]


def _runtime_output(catalog, text):
    """What the runtime does for *text*: the first-word index, in order."""
    result = PatternMatcher(catalog).match_complete(text, "command")
    assert result is not None and result.matched, text
    return result.actions


def _blocks(user):
    return tomllib.loads(open(user, encoding="utf-8").read())["pattern"]


class TestTheMovedTriggerWins:
    """Acceptance item 6, a moved edit, against the shipped patterns file."""

    def test_an_edited_builtin_moved_onto_another_builtins_words_answers(
        self, tmp_path,
    ):
        user = str(tmp_path / "user_patterns.toml")
        open(user, "w", encoding="utf-8").close()
        manager = PatternManager(SHIPPED, user)
        catalog = PatternCatalog(SHIPPED, user)
        builtin = _runtime_output(catalog, "select all")
        assert _typed(builtin) == [], "the shipped select-all is a hotkey"

        created = manager.create_pattern(
            **_data(SHOW_DESKTOP), doc_id=SHOW_DESKTOP_ID,
        )
        assert created["success"], created
        assert catalog.reload()

        draft = _data(SELECT_ALL) | {
            "doc_id": SHOW_DESKTOP_ID,
            "exclude_pattern_id": created["pattern_id"],
        }
        preview = run_test_draft(
            draft, "select all", catalog.get_all_patterns(),
            PatternMatcher(catalog), catalog=catalog,
        )
        assert preview["draft_error"] is None, preview

        result = manager.update_pattern(created["pattern_id"], _data(SELECT_ALL))
        assert result["success"], result
        assert catalog.reload()

        assert _typed(_runtime_output(catalog, "select all")) == ["CUSTOM"], (
            "the shipped select-all rule still answers the words the edited "
            "rule moved onto"
        )
        first = catalog.get_matching_patterns("select")[0]
        assert _typed(first[2]["actions"]) == ["CUSTOM"], (
            "the first-word index must list the edited rule first"
        )
        tried = run_test_phrase(
            "select all", catalog.get_all_patterns(), PatternMatcher(catalog),
        )
        assert _typed(tried["match"]["resolved_steps"]) == ["CUSTOM"]
        assert preview["winner"] == "draft", preview
        restarted = PatternCatalog(SHIPPED, user)
        assert _typed(_runtime_output(restarted, "select all")) == ["CUSTOM"]
        # The words the edit left are the built-in's again.
        assert _typed(_runtime_output(restarted, "show desktop")) == []


SYSTEM = '''COMMAND_HOTWORD = "x-ray"
[[pattern]]
doc_id = "first-command"
pattern = '^first$'
actions = [{function = "text", params = ["FIRST"]}]
[[pattern]]
doc_id = "second-command"
pattern = '^second$'
actions = [{function = "text", params = ["SECOND"]}]
[[pattern]]
doc_id = "foo-replacement"
pattern = '\\bfoo\\b'
actions = [{function = "text", params = ["SHIPPED-FOO"]}]
'''


@pytest.fixture
def bench(tmp_path):
    system = tmp_path / "patterns.toml"
    user = tmp_path / "user_patterns.toml"
    system.write_text(SYSTEM, encoding="utf-8")
    user.write_text("", encoding="utf-8")
    return PatternManager(str(system), str(user)), str(system), str(user)


def _outputs(catalog, phrase):
    return [
        entry[2]["actions"][0]["params"][0]
        for entry in catalog.get_matching_patterns(phrase)
    ]


def _write(user, text):
    with open(user, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestEveryOwnRuleAnswersFirst:
    """Acceptance items 1, 2 and 6: each kind of slot-less user rule
    answers ahead of a built-in rule with the same words."""

    def test_a_moved_edit(self, bench):
        manager, system, _user = bench
        created = manager.create_pattern(
            **_data("^first$"), doc_id="first-command",
        )
        assert created["success"], created
        result = manager.update_pattern(created["pattern_id"], _data("^second$"))
        assert result["success"], result
        catalog = PatternCatalog(system, bench[2])
        assert _outputs(catalog, "second") == ["CUSTOM", "SECOND"]
        assert _outputs(catalog, "first") == ["FIRST"]

    def test_an_added_rule(self, bench):
        manager, system, user = bench
        result = manager.create_pattern(**_data("^second$"))
        assert result["success"], result
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "second") == ["CUSTOM", "SECOND"]

    def test_a_duplicate(self, bench):
        """Duplicate copies a built-in's words and passes no doc_id. David
        accepted that the duplicate then answers instead of that built-in
        (QUESTIONS-2026-09-28.md item 12, option three)."""
        manager, system, user = bench
        result = manager.create_pattern(**_data("^first$"))
        assert result["success"], result
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "first") == ["CUSTOM", "FIRST"]

    def test_a_rule_saved_before_this_change(self, bench):
        """origin "user", no doc_id, no mark of any kind."""
        _manager, system, user = bench
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^second$'\n"
            'origin = "user"\n'
            'source = "pattern_manager"\n'
            'actions = [{function = "text", params = ["OLD"]}]\n',
        )
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "second") == ["OLD", "SECOND"]

    def test_a_user_replacement_rule(self, bench):
        """David, boss chat 12:23: replacements move in front too."""
        manager, system, user = bench
        result = manager.create_pattern(
            trigger="foo", pattern_type="replacement", action_type="text",
            action_params={"output": "MINE"},
        )
        assert result["success"], result
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "foo") == ["MINE", "SHIPPED-FOO"]
        replacements = [
            _typed(row["actions"]) for row in catalog.get_all_patterns()
            if row["pattern_type"] == "replacement"
        ]
        assert replacements == [["MINE"], ["SHIPPED-FOO"]]

    def test_the_first_own_rule_in_the_file_answers_a_shared_phrase(
        self, bench,
    ):
        _manager, system, user = bench
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^second(?: now)?$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["A"]}]\n'
            "[[pattern]]\n"
            "pattern = '^second$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["B"]}]\n'
            "[[pattern]]\n"
            "pattern = '^(?:the )?second$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["C"]}]\n',
        )
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "second") == ["A", "B", "C", "SECOND"]
        tried = run_test_phrase(
            "second", catalog.get_all_patterns(), PatternMatcher(catalog),
        )
        assert _typed(tried["match"]["resolved_steps"]) == ["A"]


class TestASlotReplacementKeepsItsSlot:
    """Acceptance item 3 (boss ruling R1)."""

    def test_an_edit_that_kept_its_words(self, bench):
        manager, system, user = bench
        manager.create_pattern(**_data("^zebra$"))
        created = manager.create_pattern(
            **_data("^second$", output="MINE"), doc_id="second-command",
        )
        assert created["success"], created
        catalog = PatternCatalog(system, user)
        order = [row["raw_pattern"] for row in catalog.get_all_patterns()]
        assert order == ["^zebra$", "^first$", "^second$", r"\bfoo\b"]
        assert _outputs(catalog, "second") == ["MINE"]

    def test_a_disabled_override(self, bench):
        _manager, system, user = bench
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^zebra$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["Z"]}]\n'
            "[[pattern]]\n"
            "pattern = '^first$'\n"
            'doc_id = "first-command"\n'
            "actions = []\n",
        )
        catalog = PatternCatalog(system, user)
        merged = catalog._merge_entries(
            catalog.get_raw_system_entries(), catalog.get_raw_user_entries(),
        )
        assert [(e["pattern"], len(e["actions"])) for e in merged] == [
            ("^zebra$", 1), ("^first$", 0), ("^second$", 1),
            (r"\bfoo\b", 1),
        ]
        assert _outputs(catalog, "first") == []

    def test_an_unambiguous_customization_saved_before_ids(self, bench):
        """No origin and no doc_id: the merge migrates it onto the one
        built-in that carries its text, and it keeps that slot."""
        _manager, system, user = bench
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^zebra$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["Z"]}]\n'
            "[[pattern]]\n"
            "pattern = '^second$'\n"
            'actions = [{function = "text", params = ["LEGACY"]}]\n',
        )
        catalog = PatternCatalog(system, user)
        order = [
            (row["raw_pattern"], _typed(row["actions"]))
            for row in catalog.get_all_patterns()
        ]
        assert order == [
            ("^zebra$", ["Z"]), ("^first$", ["FIRST"]),
            ("^second$", ["LEGACY"]), (r"\bfoo\b", ["SHIPPED-FOO"]),
        ]


def _rule(expression, output, *, own=True, doc_id=None):
    entry = {
        "pattern": expression,
        "actions": [{"function": "text", "params": [output]}],
    }
    if doc_id is not None:
        entry["doc_id"] = doc_id
    if own:
        entry["origin"] = "user"
    return entry


class TestTheMergeOrder:
    """Acceptance items 1 and 3, on the merge itself."""

    SYSTEM_ENTRIES = [
        {"doc_id": "first-command", "pattern": "^first$",
         "actions": [{"function": "text", "params": ["FIRST"]}]},
        {"doc_id": "second-command", "pattern": "^second$",
         "actions": [{"function": "text", "params": ["SECOND"]}]},
        {"doc_id": "twin-a", "pattern": "^twin$",
         "actions": [{"function": "text", "params": ["TWIN-A"]}]},
        {"doc_id": "twin-b", "pattern": "^twin$",
         "actions": [{"function": "text", "params": ["TWIN-B"]}]},
    ]

    def _merge(self, bench, user_entries):
        _manager, system, user = bench
        catalog = PatternCatalog(system, user)
        system_entries = [dict(entry) for entry in self.SYSTEM_ENTRIES]
        merged = catalog._merge_entries(system_entries, user_entries)
        return [entry["actions"][0]["params"][0] for entry in merged]

    def test_slot_less_rules_lead_in_file_order_and_slots_stay(self, bench):
        order = self._merge(bench, [
            _rule("^plain one$", "P1"),
            _rule("^second$", "S", doc_id="second-command"),
            _rule("^plain two$", "P2"),
        ])
        assert order == ["P1", "P2", "FIRST", "S", "TWIN-A", "TWIN-B"]

    def test_a_displaced_claimant_sits_at_its_own_file_position(self, bench):
        """A claims first-command, B is an own rule, C claims first-command
        again and takes the slot. A is displaced and moves in front at its
        own file position, ahead of B -- not after B, where it was
        appended."""
        order = self._merge(bench, [
            _rule("^first alt$", "A", doc_id="first-command"),
            _rule("^b$", "B"),
            _rule("^first$", "C", doc_id="first-command"),
        ])
        assert order == ["A", "B", "C", "SECOND", "TWIN-A", "TWIN-B"]

    def test_entries_that_name_no_single_builtin_lead(self, bench):
        """An ambiguous pre-doc_id entry, a doc_id no built-in carries, and
        a pre-doc_id entry whose text no built-in carries."""
        order = self._merge(bench, [
            _rule("^twin$", "AMBIGUOUS", own=False),
            _rule("^named$", "UNKNOWN-ID", doc_id="no-such-builtin"),
            _rule("^legacy$", "LEGACY", own=False),
        ])
        assert order == [
            "AMBIGUOUS", "UNKNOWN-ID", "LEGACY",
            "FIRST", "SECOND", "TWIN-A", "TWIN-B",
        ]

    def test_an_own_rule_with_a_builtins_text_does_not_take_its_slot(
        self, bench,
    ):
        """A duplicate of twin-a's text: is_own_rule keeps the legacy
        migration away from it, so it moves in front and both built-ins
        stay."""
        order = self._merge(bench, [_rule("^first$", "DUP")])
        assert order == ["DUP", "FIRST", "SECOND", "TWIN-A", "TWIN-B"]

    def test_an_old_marker_key_has_no_effect(self, bench):
        """A block that still carries the removed key is placed by its
        identity alone: a slot claim stays in its slot."""
        claim = _rule("^first$", "C", doc_id="first-command")
        claim[OLD_MARKER] = True
        order = self._merge(bench, [_rule("^zebra$", "Z"), claim])
        assert order == ["Z", "C", "SECOND", "TWIN-A", "TWIN-B"]

    def test_the_recovered_report_still_names_user_file_positions(
        self, bench,
    ):
        """The reorder must not disturb the legacy-id report, which is
        keyed by position in the user entries, not in the merged list."""
        _manager, system, user = bench
        catalog = PatternCatalog(system, user)
        recovered = {}
        catalog._merge_entries(
            [dict(entry) for entry in self.SYSTEM_ENTRIES],
            [_rule("^zebra$", "Z"), _rule("^second$", "L", own=False)],
            recovered,
        )
        assert recovered == {1: "second-command"}


class TestNoMarkIsWritten:
    """Acceptance item 5."""

    def test_a_moved_edit_writes_no_marker(self, bench):
        manager, _system, user = bench
        created = manager.create_pattern(
            **_data("^first$"), doc_id="first-command",
        )
        assert created["success"], created
        result = manager.update_pattern(created["pattern_id"], _data("^second$"))
        assert result["success"], result
        assert OLD_MARKER not in _blocks(user)[0]
        assert OLD_MARKER not in open(user, encoding="utf-8").read()

    def test_a_moved_customize_writes_no_marker(self, bench):
        manager, _system, user = bench
        result = manager.create_pattern(
            **_data("^second$"), doc_id="first-command",
        )
        assert result["success"], result
        assert OLD_MARKER not in open(user, encoding="utf-8").read()

    def test_a_stored_marker_is_not_written_again(self, bench):
        manager, system, user = bench
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^second$'\n"
            'origin = "user"\n'
            "before_builtins = true\n"
            'actions = [{function = "text", params = ["OLD"]}]\n',
        )
        result = manager.update_pattern(
            manager.pattern_id("^second$"), _data("^second$"),
        )
        assert result["success"], result
        assert OLD_MARKER not in _blocks(user)[0]
        catalog = PatternCatalog(system, user)
        assert _outputs(catalog, "second") == ["CUSTOM", "SECOND"]


class TestThePreviewAgreesWithTheSave:
    """Try-it places the draft where the save does (acceptance item 4)."""

    @pytest.mark.parametrize("mode,draft,phrase,expected", [
        ("add", _data("^second$"), "second", ["CUSTOM", "SECOND"]),
        ("duplicate", _data("^first$"), "first", ["CUSTOM", "FIRST"]),
        ("customize-moved", _data("^second$") | {"doc_id": "first-command"},
         "second", ["CUSTOM", "SECOND"]),
        ("customize-kept", _data("^second$") | {"doc_id": "second-command"},
         "second", ["CUSTOM"]),
    ])
    def test_a_create(self, bench, mode, draft, phrase, expected):
        manager, system, user = bench
        catalog = PatternCatalog(system, user)
        preview = run_test_draft(
            draft, phrase, catalog.get_all_patterns(),
            PatternMatcher(catalog), catalog=catalog,
        )
        assert preview["draft_error"] is None, preview
        result = manager.create_pattern(**draft)
        assert result["success"], result
        assert catalog.reload()
        assert _outputs(catalog, phrase) == expected
        assert preview["winner"] == "draft", preview

    @pytest.mark.parametrize("start", ["customize", "added", "before-change"])
    def test_an_update(self, bench, start):
        manager, system, user = bench
        if start == "customize":
            created = manager.create_pattern(
                **_data("^first$"), doc_id="first-command",
            )
            assert created["success"], created
            target = created["pattern_id"]
        elif start == "added":
            created = manager.create_pattern(**_data("^zebra$"))
            assert created["success"], created
            target = created["pattern_id"]
        else:
            _write(
                user,
                "[[pattern]]\n"
                "pattern = '^zebra$'\n"
                'origin = "user"\n'
                'actions = [{function = "text", params = ["OLD"]}]\n',
            )
            target = manager.pattern_id("^zebra$")
        catalog = PatternCatalog(system, user)
        draft = _data("^second$") | {"exclude_pattern_id": target}
        preview = run_test_draft(
            draft, "second", catalog.get_all_patterns(),
            PatternMatcher(catalog), catalog=catalog,
        )
        assert preview["draft_error"] is None, preview
        result = manager.update_pattern(target, _data("^second$"))
        assert result["success"], result
        assert catalog.reload()
        assert _outputs(catalog, "second") == ["CUSTOM", "SECOND"]
        assert preview["winner"] == "draft", preview


# ---------------------------------------------------------------------------
# Acceptance item 4: a rule that will stand in front copies
# whole_utterance_only from the built-in rule(s) whose words it takes, and
# keeps its own requires_hotword. Boss ruling A (07:32 2026-09-28): each
# sample phrase of the new expression is matched against the SHIPPED
# entries in file order, the way the runtime matches them; the first match
# per phrase is the built-in it takes. True when any taken built-in has the
# flag; today's carried value when none matches. Real shipped entries used
# below:
#   save          '^save$'                          whole_utterance_only
#   show-desktop  '^(?:show (?:the )?)?desktop$'    whole_utterance_only
#   close-window  '^close window$'   no flag, requires_hotword = true
#   close-tab     '^close tab$'      no flag, requires_hotword = true
# ---------------------------------------------------------------------------

SAVE_ID = "save"
CLOSE_WINDOW_ID = "close-window"
BUILTINS = {
    SAVE_ID: {"expression": "^save$", "whole_utterance_only": True},
    CLOSE_WINDOW_ID: {
        "expression": "^close window$", "requires_hotword": True,
    },
}
WHOLE = "whole_utterance_only"


def _phrased(phrases, output="CUSTOM", **extra):
    """A simple-mode draft, as the editor sends it."""
    return {
        "phrases": list(phrases),
        "pattern_type": "command",
        "action_type": "text",
        "action_params": {"output": output},
    } | extra


@pytest.fixture
def shipped(tmp_path):
    user = str(tmp_path / "user_patterns.toml")
    open(user, "w", encoding="utf-8").close()
    return PatternManager(SHIPPED, user), user


def _customize(manager, doc_id):
    """Customize a shipped rule without changing its words: the editor's
    create carries the built-in's doc_id and flags forward."""
    builtin = BUILTINS[doc_id]
    created = manager.create_pattern(
        **_data(builtin["expression"]),
        requires_hotword=builtin.get("requires_hotword", False),
        whole_utterance_only=builtin.get(WHOLE, False),
        doc_id=doc_id,
    )
    assert created["success"], created
    assert _only_block(manager.user_patterns_file)["doc_id"] == doc_id
    return created["pattern_id"]


def _only_block(user):
    blocks = _blocks(user)
    assert len(blocks) == 1, blocks
    return blocks[0]


class TestAMovedEditCopiesTheWholeUtteranceFlag:
    """Acceptance item 4, moved edits, against the shipped patterns file."""

    @pytest.mark.parametrize("data", [
        _phrased(["desktop"]),
        _phrased(["save"]),
        # Advanced mode: the alternation display expansions are the
        # sample phrases ("open desktop", "show desktop").
        _data("^(?:open|show) desktop$"),
        # Advanced mode without an alternation: the strip display is the
        # sample phrase, kept because the expression answers it.
        _data("^desktop$"),
        _data("^save$"),
        # The runtime retries a ^ rule with STT punctuation stripped from
        # the end and from interior words, so the built-in answers these
        # too (wh-user-rule-precedence.1.1).
        _phrased(["desktop,"]),
        _phrased(["show, desktop"]),
    ], ids=[
        "phrase-desktop", "phrase-save", "alternation",
        "plain-desktop", "plain-save",
        "trailing-punctuation", "interior-punctuation",
    ])
    def test_an_edit_moved_onto_a_whole_utterance_builtin_gets_the_flag(
        self, shipped, data,
    ):
        """(a) The rule it was edited from (close window) lacks the flag."""
        manager, user = shipped
        target = _customize(manager, CLOSE_WINDOW_ID)
        result = manager.update_pattern(target, data)
        assert result["success"], result
        block = _only_block(user)
        assert "doc_id" not in block
        assert block.get(WHOLE) is True, block

    def test_a_customize_moved_onto_a_whole_utterance_builtin_gets_the_flag(
        self, shipped,
    ):
        """(a) create path: the carried argument is False."""
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["desktop"]), doc_id=CLOSE_WINDOW_ID,
            whole_utterance_only=False,
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True

    def test_an_edit_moved_onto_a_builtin_without_the_flag_drops_it(
        self, shipped,
    ):
        """(b) The rule it was edited from (save) has the flag."""
        manager, user = shipped
        target = _customize(manager, SAVE_ID)
        assert _only_block(user).get(WHOLE) is True
        result = manager.update_pattern(target, _phrased(["close window"]))
        assert result["success"], result
        assert WHOLE not in _only_block(user)

    def test_a_customize_moved_onto_a_builtin_without_the_flag_drops_it(
        self, shipped,
    ):
        """(b) create path: the carried argument is True."""
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["close window"]), doc_id=SAVE_ID,
            whole_utterance_only=True,
        )
        assert result["success"], result
        assert WHOLE not in _only_block(user)

    def test_any_taken_builtin_with_the_flag_sets_it(self, shipped):
        manager, user = shipped
        target = _customize(manager, CLOSE_WINDOW_ID)
        result = manager.update_pattern(
            target, _phrased(["close tab", "desktop"]),
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True

    @pytest.mark.parametrize("origin", [SAVE_ID, CLOSE_WINDOW_ID])
    @pytest.mark.parametrize("data", [
        _phrased(["zebra marmalade"]),
        # No phrases at all: a capture group gives no sample phrase.
        _data("^zebra (.+)$"),
        # The strip display drops the capture group ('item now',
        # 'desktop'); the expression does not answer that text, so it is
        # no sample phrase, even where a built-in answers it (desktop).
        _data(r"^item (\d+) now$"),
        _data(r"^desktop (\d+)$"),
        # The shipped '^fix' (fix-text, no flag) has no '$'. The runtime
        # fullmatches a ^-anchored rule, so it does not answer this phrase
        # and no built-in is taken; a search would find it and drop the
        # carried flag.
        _phrased(["fix zebra marmalade"]),
    ], ids=[
        "unmatched-phrase", "no-phrases", "stripped-capture",
        "stripped-capture-on-builtin-words", "prefix-of-an-open-builtin",
    ])
    def test_words_no_builtin_takes_keep_the_carried_value(
        self, shipped, origin, data,
    ):
        """(c) update path: the carried value is the stored block's."""
        manager, user = shipped
        target = _customize(manager, origin)
        result = manager.update_pattern(target, data)
        assert result["success"], result
        assert (_only_block(user).get(WHOLE) is True) == (origin == SAVE_ID)

    @pytest.mark.parametrize("carried", [True, False])
    def test_a_customize_onto_untaken_words_keeps_the_argument(
        self, shipped, carried,
    ):
        """(c) create path: the carried value is the argument."""
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["zebra marmalade"]), doc_id=SAVE_ID,
            whole_utterance_only=carried,
        )
        assert result["success"], result
        assert (_only_block(user).get(WHOLE) is True) == carried


class TestAnOwnRuleCopiesTheWholeUtteranceFlag:
    """Acceptance item 4: Add, Duplicate, and an edit of an own rule stand
    in front, so they copy the flag too (boss ruling R2)."""

    def test_an_add_onto_a_whole_utterance_builtins_words(self, shipped):
        manager, user = shipped
        result = manager.create_pattern(**_phrased(["desktop"]))
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True

    def test_a_duplicate_moved_onto_a_builtin_without_the_flag(self, shipped):
        """Duplicate of save carries the flag and passes no doc_id."""
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["close window"]), whole_utterance_only=True,
        )
        assert result["success"], result
        assert WHOLE not in _only_block(user)

    def test_a_duplicate_that_keeps_its_builtins_words(self, shipped):
        """Duplicate of close window, flag cleared by the editor: the words
        are save's, so the copy takes save's flag."""
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["save"]), whole_utterance_only=False,
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True

    def test_an_add_with_a_builtins_own_expression_copies_the_flag(
        self, shipped,
    ):
        """An Add written with save's exact expression and no doc_id is an
        own rule: create_pattern writes origin = "user", so the save does
        not read the block as a customization of save saved before ids.
        It stands in front and copies save's flag over the cleared box.
        (_phrased(["save"]) builds a different expression, so the test
        above never reaches the legacy text match.)"""
        manager, user = shipped
        result = manager.create_pattern(
            **_data("^save$"), whole_utterance_only=False,
        )
        assert result["success"], result
        block = _only_block(user)
        assert block["pattern"] == "^save$", block
        assert block.get("origin") == "user" and "doc_id" not in block, block
        assert block.get(WHOLE) is True, block

    @pytest.mark.parametrize("carried", [True, False])
    def test_an_add_onto_untaken_words_keeps_the_argument(
        self, shipped, carried,
    ):
        manager, user = shipped
        result = manager.create_pattern(
            **_phrased(["zebra marmalade"]), whole_utterance_only=carried,
        )
        assert result["success"], result
        assert (_only_block(user).get(WHOLE) is True) == carried

    def test_an_edit_of_an_added_rule_moved_onto_a_whole_utterance_builtin(
        self, shipped,
    ):
        manager, user = shipped
        created = manager.create_pattern(**_phrased(["zebra marmalade"]))
        assert created["success"], created
        result = manager.update_pattern(
            created["pattern_id"], _phrased(["desktop"]),
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True

    def test_an_edit_of_an_own_rule_onto_a_builtins_own_expression(
        self, shipped,
    ):
        """origin = "user" keeps an own rule out of the legacy text
        migration, so an own rule written with save's exact expression
        still stands in front, and the save copies save's flag. Without
        the origin the save would read the block as a customization saved
        before ids and keep the stored value."""
        manager, user = shipped
        created = manager.create_pattern(**_phrased(["zebra marmalade"]))
        assert created["success"], created
        result = manager.update_pattern(created["pattern_id"], _data("^save$"))
        assert result["success"], result
        block = _only_block(user)
        assert block["pattern"] == "^save$", block
        assert block.get("origin") == "user" and "doc_id" not in block, block
        assert block.get(WHOLE) is True, block

    def test_a_rule_saved_before_this_change_copies_on_its_next_save(
        self, shipped,
    ):
        """The load does not copy (crewcut in PatternManager); the next
        save does."""
        manager, user = shipped
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^desktop$'\n"
            'origin = "user"\n'
            'actions = [{function = "text", params = ["OLD"]}]\n',
        )
        loaded = PatternCatalog(SHIPPED, user).get_matching_patterns(
            "desktop",
        )[0]
        assert _typed(loaded[2]["actions"]) == ["OLD"]
        assert loaded[2].get(WHOLE) is not True, (
            "the load copies nothing; the stored value stands until a save"
        )
        result = manager.update_pattern(
            manager.pattern_id("^desktop$"), _data("^desktop$"),
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True


class TestASlotKeepingSaveKeepsTheStoredFlag:
    """Acceptance item 4: a save that keeps a built-in's slot copies
    nothing."""

    @pytest.mark.parametrize("origin", [SAVE_ID, CLOSE_WINDOW_ID])
    def test_an_unchanged_trigger_edit(self, shipped, origin):
        manager, user = shipped
        target = _customize(manager, origin)
        expression = BUILTINS[origin]["expression"]
        result = manager.update_pattern(
            target, _data(expression, output="OTHER"),
        )
        assert result["success"], result
        block = _only_block(user)
        assert block["doc_id"] == origin
        assert (block.get(WHOLE) is True) == (origin == SAVE_ID), block

    def test_an_unchanged_trigger_edit_with_the_flag_cleared_by_hand(
        self, shipped,
    ):
        """save's flag is True; the stored block has none, and a slot
        keeping save leaves it that way."""
        manager, user = shipped
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^save$'\n"
            'doc_id = "save"\n'
            'origin = "user"\n'
            'actions = [{function = "text", params = ["MINE"]}]\n',
        )
        result = manager.update_pattern(
            manager.pattern_id("^save$"), _data("^save$", output="OTHER"),
        )
        assert result["success"], result
        assert WHOLE not in _only_block(user)

    def test_a_customize_that_keeps_its_words_keeps_the_argument(
        self, shipped,
    ):
        """Create path: a Customize of save with the whole-utterance box
        cleared keeps save's words, so it keeps save's slot and the save
        writes the cleared box instead of copying save's flag."""
        manager, user = shipped
        result = manager.create_pattern(
            **_data("^save$"), doc_id=SAVE_ID, whole_utterance_only=False,
        )
        assert result["success"], result
        block = _only_block(user)
        assert block["doc_id"] == SAVE_ID, block
        assert WHOLE not in block, block

    def test_a_customization_saved_before_ids(self, shipped):
        """No origin, no doc_id, save's own text: the merge gives it save's
        slot, so the edit keeps the stored (absent) flag."""
        manager, user = shipped
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^save$'\n"
            'actions = [{function = "text", params = ["MINE"]}]\n',
        )
        result = manager.update_pattern(
            manager.pattern_id("^save$"), _data("^save$", output="OTHER"),
        )
        assert result["success"], result
        block = _only_block(user)
        assert "origin" not in block and "doc_id" not in block
        assert WHOLE not in block


class TestAnIdNamingNoBuiltinCopiesTheFlag:
    """Acceptance items 1 and 4: a doc_id whose built-in no longer ships
    holds no slot, so its save stands in front and copies the flag."""

    def test_a_doc_id_naming_no_builtin_copies_the_flag(self, shipped):
        manager, user = shipped
        _write(
            user,
            "[[pattern]]\n"
            "pattern = '^desktop$'\n"
            'doc_id = "retired-builtin"\n'
            'actions = [{function = "text", params = ["OLD"]}]\n',
        )
        result = manager.update_pattern(
            manager.pattern_id("^desktop$"), _data("^desktop$"),
        )
        assert result["success"], result
        block = _only_block(user)
        assert block["doc_id"] == "retired-builtin", block
        assert block.get(WHOLE) is True, block
        first = PatternCatalog(SHIPPED, user).get_matching_patterns(
            "desktop",
        )[0]
        assert _typed(first[2]["actions"]) == ["CUSTOM"]


class TestTheHotwordCheckboxIsKept:
    """requires_hotword is the checkbox value, never copied."""

    def test_a_checked_box_stays_on_words_whose_builtin_has_none(
        self, shipped,
    ):
        manager, user = shipped
        target = _customize(manager, CLOSE_WINDOW_ID)
        result = manager.update_pattern(
            target, _phrased(["desktop"], requires_hotword=True),
        )
        assert result["success"], result
        block = _only_block(user)
        assert block.get("requires_hotword") is True, block
        assert block.get(WHOLE) is True, block

    def test_a_clear_box_stays_on_words_whose_builtin_needs_the_hotword(
        self, shipped,
    ):
        manager, user = shipped
        target = _customize(manager, SAVE_ID)
        result = manager.update_pattern(
            target, _phrased(["close window"], requires_hotword=False),
        )
        assert result["success"], result
        block = _only_block(user)
        assert "requires_hotword" not in block, block
        assert WHOLE not in block, block

    def test_an_add_keeps_a_clear_box(self, shipped):
        manager, user = shipped
        result = manager.create_pattern(**_phrased(["close window"]))
        assert result["success"], result
        assert "requires_hotword" not in _only_block(user)


def _user_row(rows, expression):
    found = [
        row for row in rows
        if row.get("is_user") and row.get("raw_pattern") == expression
    ]
    assert len(found) == 1, (expression, found)
    return found[0]


class TestThePreviewAgreesOnTheFlag:
    """Try-it builds the draft row with the flag the save writes."""

    @pytest.mark.parametrize("mode", ["create", "update"])
    @pytest.mark.parametrize("origin,phrases,expected", [
        (CLOSE_WINDOW_ID, ["desktop"], True),
        (SAVE_ID, ["close window"], False),
        (SAVE_ID, ["zebra marmalade"], True),
        (CLOSE_WINDOW_ID, ["zebra marmalade"], False),
    ], ids=["copies-true", "copies-false", "keeps-true", "keeps-false"])
    def test_the_preview_row_carries_the_saved_flag(
        self, shipped, monkeypatch, mode, origin, phrases, expected,
    ):
        self._check(
            shipped, monkeypatch, mode, origin, _phrased(phrases),
            " ".join(phrases), expected,
        )

    @pytest.mark.parametrize("mode", ["create", "update"])
    def test_a_plain_advanced_expression_previews_the_saved_flag(
        self, shipped, monkeypatch, mode,
    ):
        """Advanced mode '^desktop$' takes show-desktop's flag."""
        self._check(
            shipped, monkeypatch, mode, CLOSE_WINDOW_ID, _data("^desktop$"),
            "desktop", True,
        )

    @pytest.mark.parametrize("mode", ["add", "update-own"])
    @pytest.mark.parametrize("phrases,carried,expected", [
        (["desktop"], False, True),
        (["close window"], True, False),
    ], ids=["copies-true", "copies-false"])
    def test_an_own_rule_previews_the_saved_flag(
        self, shipped, monkeypatch, mode, phrases, carried, expected,
    ):
        """Add (and Duplicate, the same create) and an edit of an own rule
        stand in front, so the preview copies the flag as the save does."""
        self._check(
            shipped, monkeypatch, mode, None, _phrased(phrases),
            " ".join(phrases), expected, carried=carried,
        )

    @staticmethod
    def _check(
        shipped, monkeypatch, mode, origin, draft, spoken, expected,
        carried=None,
    ):
        manager, user = shipped
        target = None
        if origin is not None:
            builtin = BUILTINS[origin]
            # The editor's draft carries the entry's doc_id and its flag.
            draft = draft | {"doc_id": origin}
            if builtin.get(WHOLE):
                draft[WHOLE] = True
            if mode == "update":
                target = _customize(manager, origin)
        elif mode == "update-own":
            _write(
                user,
                "[[pattern]]\n"
                "pattern = '^zebra$'\n"
                'origin = "user"\n'
                + ("whole_utterance_only = true\n" if carried else "")
                + 'actions = [{function = "text", params = ["OLD"]}]\n',
            )
            target = manager.pattern_id("^zebra$")
        elif carried:
            draft = draft | {WHOLE: True}
        if target is not None:
            draft = draft | {"exclude_pattern_id": target}
        catalog = PatternCatalog(SHIPPED, user)
        built = []
        real_build = catalog.build_from_user_entries

        def spy(entries):
            rows = real_build(entries)
            built.append(rows)
            return rows

        monkeypatch.setattr(catalog, "build_from_user_entries", spy)
        preview = run_test_draft(
            draft, spoken, catalog.get_all_patterns(),
            PatternMatcher(catalog), catalog=catalog,
        )
        assert preview["draft_error"] is None, preview
        assert len(built) == 1

        if target is None:
            result = manager.create_pattern(**draft)
        else:
            result = manager.update_pattern(target, draft)
        assert result["success"], result
        expression = _only_block(user)["pattern"]
        saved = _user_row(
            PatternCatalog(SHIPPED, user).get_all_patterns(), expression,
        )
        previewed = _user_row(built[0], expression)
        assert saved["whole_utterance_only"] is expected, saved
        assert previewed["whole_utterance_only"] is expected, previewed


class TestTheRuntimeCarriesTheCopiedFlag:
    """The saved rule answers first and its pattern data has the flag."""

    def test_the_rule_on_desktop_words_is_whole_utterance_only(
        self, shipped,
    ):
        manager, user = shipped
        target = _customize(manager, CLOSE_WINDOW_ID)
        result = manager.update_pattern(target, _phrased(["desktop"]))
        assert result["success"], result
        catalog = PatternCatalog(SHIPPED, user)
        first = catalog.get_matching_patterns("desktop")[0]
        assert _typed(first[2]["actions"]) == ["CUSTOM"]
        assert first[2].get(WHOLE) is True, first[2]
        assert _typed(_runtime_output(catalog, "desktop")) == ["CUSTOM"]

    def test_the_rule_on_close_window_words_is_not(self, shipped):
        manager, user = shipped
        target = _customize(manager, SAVE_ID)
        result = manager.update_pattern(target, _phrased(["close window"]))
        assert result["success"], result
        catalog = PatternCatalog(SHIPPED, user)
        first = catalog.get_matching_patterns("close")[0]
        assert _typed(first[2]["actions"]) == ["CUSTOM"]
        assert WHOLE not in first[2], first[2]

    def test_an_added_rule_on_desktop_words_answers_first_with_the_flag(
        self, shipped,
    ):
        manager, user = shipped
        result = manager.create_pattern(**_phrased(["desktop"]))
        assert result["success"], result
        catalog = PatternCatalog(SHIPPED, user)
        first = catalog.get_matching_patterns("desktop")[0]
        assert _typed(first[2]["actions"]) == ["CUSTOM"]
        assert first[2].get(WHOLE) is True, first[2]


# ---------------------------------------------------------------------------
# wh-user-rule-precedence.2.1: an advanced expression that is not
# phrase-shaped and has no alternation display gave only its strip display
# as a sample phrase. '^show(?: desktop)?$' gave only 'show', which no
# built-in answers, so the save kept the carried value although the rule
# also answers 'show desktop', whose shipped show-desktop rule is
# whole-utterance-only. The sample phrases now also include the phrases
# enumerated from the expression itself.
# ---------------------------------------------------------------------------

OPTIONAL_DESKTOP = "^show(?: desktop)?$"


class TestAnOptionalGroupTakesTheBuiltinsFlag:
    """Create, a moved edit, and Try-it each find show-desktop's flag."""

    def test_an_add_of_an_optional_group(self, shipped):
        manager, user = shipped
        result = manager.create_pattern(
            **_data(OPTIONAL_DESKTOP), whole_utterance_only=False,
        )
        assert result["success"], result
        block = _only_block(user)
        assert block["pattern"] == OPTIONAL_DESKTOP, block
        assert block.get(WHOLE) is True, block

    def test_a_moved_edit_to_an_optional_group(self, shipped):
        manager, user = shipped
        target = _customize(manager, CLOSE_WINDOW_ID)
        result = manager.update_pattern(target, _data(OPTIONAL_DESKTOP))
        assert result["success"], result
        block = _only_block(user)
        assert "doc_id" not in block, block
        assert block.get(WHOLE) is True, block

    @pytest.mark.parametrize("mode", ["create", "update"])
    def test_the_preview_of_an_optional_group(self, shipped, monkeypatch, mode):
        TestThePreviewAgreesOnTheFlag._check(
            shipped, monkeypatch, mode, CLOSE_WINDOW_ID,
            _data(OPTIONAL_DESKTOP), "show desktop", True,
        )

    def test_the_display_still_samples_what_the_enumeration_cannot_read(
        self, shipped,
    ):
        """A lookahead makes the enumeration give nothing; the strip
        display 'desktop' is still sampled, and show-desktop answers it."""
        manager, user = shipped
        result = manager.create_pattern(
            **_data("^(?!zebra)desktop$"), whole_utterance_only=False,
        )
        assert result["success"], result
        assert PatternManager._enumerated_phrases("^(?!zebra)desktop$") == []
        assert _only_block(user).get(WHOLE) is True

    @pytest.mark.parametrize("carried", [True, False])
    def test_enumerated_phrases_no_builtin_answers_keep_the_carried_value(
        self, shipped, carried,
    ):
        """'zebra' and 'zebra marmalade' answer no built-in."""
        manager, user = shipped
        result = manager.create_pattern(
            **_data("^zebra(?: marmalade)?$"), whole_utterance_only=carried,
        )
        assert result["success"], result
        assert (_only_block(user).get(WHOLE) is True) == carried


# wh-user-rule-precedence.2.2: a finite repeat range was expanded only at
# its minimum and the minimum plus one, so '^select al{0,2}$' never sampled
# 'select all', and the save missed the shipped select-all rule's flag.
FINITE_RANGE_SELECT_ALL = "^select al{0,2}$"


class TestAFiniteRangeTakesTheBuiltinsFlag:
    """Every count of a finite range is sampled, greedy and lazy."""

    @pytest.mark.parametrize("expression", [
        FINITE_RANGE_SELECT_ALL, "^select al{0,2}?$",
    ], ids=["greedy", "lazy"])
    def test_an_add_of_a_finite_range(self, shipped, expression):
        manager, user = shipped
        result = manager.create_pattern(
            **_data(expression), whole_utterance_only=False,
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True, _only_block(user)

    @pytest.mark.parametrize("mode", ["create", "update"])
    def test_the_preview_of_a_finite_range(self, shipped, monkeypatch, mode):
        TestThePreviewAgreesOnTheFlag._check(
            shipped, monkeypatch, mode, CLOSE_WINDOW_ID,
            _data(FINITE_RANGE_SELECT_ALL), "select all", True,
        )


# wh-user-rule-precedence.2.3: the last count of '(?: desktop){0,32}' fits
# the length guard alone, but not once 'show' is joined in front of it, and
# that one long phrase made the whole enumeration give nothing, so the
# save missed the shipped show-desktop rule's flag.
LONG_RANGE_SHOW_DESKTOP = "^show(?: desktop){0,32}$"


class TestALongRangeKeepsItsShortPhrases:
    """A phrase too long after joining is dropped; the short ones stay."""

    @pytest.mark.parametrize("expression", [
        LONG_RANGE_SHOW_DESKTOP, "^show(?: desktop){0,32}?$",
    ], ids=["greedy", "lazy"])
    def test_an_add_of_a_long_range(self, shipped, expression):
        manager, user = shipped
        result = manager.create_pattern(
            **_data(expression), whole_utterance_only=False,
        )
        assert result["success"], result
        assert _only_block(user).get(WHOLE) is True, _only_block(user)

    @pytest.mark.parametrize("mode", ["create", "update"])
    def test_the_preview_of_a_long_range(self, shipped, monkeypatch, mode):
        TestThePreviewAgreesOnTheFlag._check(
            shipped, monkeypatch, mode, CLOSE_WINDOW_ID,
            _data(LONG_RANGE_SHOW_DESKTOP), "show desktop", True,
        )


class TestTheExpressionEnumeration:
    """PatternManager._enumerated_phrases on its own."""

    @pytest.mark.parametrize("expression,expected", [
        # An optional group gives the phrase without it and with it.
        (OPTIONAL_DESKTOP, ["show", "show desktop"]),
        # A nested alternation inside an optional group.
        (
            "^(?:open |show (?:the |my ))?desktop$",
            ["desktop", "open desktop", "show the desktop", "show my desktop"],
        ),
        # A digit capture: one representative member, repeated once and
        # twice.
        (r"^item (\d+) now$", ["item 2 now", "item 22 now"]),
        # One representative member per class and category; runs of
        # whitespace collapse, so both counts of \s+ give one phrase.
        (r"^[x-z]\s+\w$", ["x a"]),
        (r"^go .$", ["go a"]),
        # A fixed count is expanded once, not also at count + 1.
        ("^(?:ab ){2}end$", ["ab ab end"]),
        # A finite range is expanded at every count, greedy or lazy.
        (FINITE_RANGE_SELECT_ALL, ["select a", "select al", "select all"]),
        ("^select al{0,2}?$", ["select a", "select al", "select all"]),
        # A count whose phrase passes the length guard ends the range; the
        # counts before it are kept.
        ("^(?:" + "b" * 100 + "){1,5}$", ["b" * 100, "b" * 200]),
        # A phrase that passes the length guard only once the text before
        # or after the repeat is joined to it is dropped; the shorter
        # phrases are kept.
        (
            LONG_RANGE_SHOW_DESKTOP,
            ["show" + " desktop" * count for count in range(32)],
        ),
        (
            "^show(?: desktop){0,32}?$",
            ["show" + " desktop" * count for count in range(32)],
        ),
        (
            "^(?:show ){0,52}desktop$",
            ["show " * count + "desktop" for count in range(50)],
        ),
    ], ids=[
        "optional-group", "nested-alternation", "digit-capture",
        "class-and-category", "any", "fixed-count", "finite-range",
        "lazy-finite-range", "range-past-the-length-guard",
        "too-long-after-a-prefix", "lazy-too-long-after-a-prefix",
        "too-long-before-a-suffix",
    ])
    def test_the_enumerated_phrases(self, expression, expected):
        assert PatternManager._enumerated_phrases(expression) == expected

    def test_the_enumeration_cap(self):
        """128 combinations: the first 64, in product order."""
        expression = "^" + " ".join(["(?:ab|cd)"] * 7) + "$"
        expected = [
            " ".join(words)
            for words in itertools.product(["ab", "cd"], repeat=7)
        ][:64]
        assert PatternManager._enumerated_phrases(expression) == expected

    @pytest.mark.parametrize("expression", [
        r"^(\w+) \1$",      # GROUPREF
        "^(?=x)x$",         # ASSERT
        "^(?!x)y$",         # ASSERT_NOT
        "^go [^ab]$",       # a negated class
        "^go [^a]$",        # NOT_LITERAL
        r"^go \D$",         # a category with no representative member
        "^(unclosed$",      # a parse error
    ], ids=[
        "groupref", "assert", "assert-not", "negated-class", "not-literal",
        "non-digit-category", "parse-error",
    ])
    def test_an_unsupported_construct_gives_nothing(self, expression):
        assert PatternManager._enumerated_phrases(expression) == []

    @pytest.mark.parametrize("expression", [
        "^x" + "b" * 256 + "$",
        "^(?:" + "b" * 200 + "){2,3}$",
    ], ids=["one-phrase-too-long", "minimum-count-too-long"])
    def test_no_phrase_within_the_length_guard_gives_nothing(self, expression):
        assert PatternManager._enumerated_phrases(expression) == []

    def test_a_product_with_no_phrase_within_the_length_guard_raises(self):
        """The raise is what ends a range at its first count too long to
        speak; without it, '^b{200,100000}$' would walk every count."""
        with pytest.raises(PatternManager._NotEnumerable):
            PatternManager._enumerate_product(["b" * 200], ["b" * 100])

    def test_a_product_keeps_the_phrases_within_the_length_guard(self):
        assert PatternManager._enumerate_product(
            ["a", "b" * 200], ["c", "d" * 100],
        ) == ["ac", "a" + "d" * 100, "b" * 200 + "c"]
