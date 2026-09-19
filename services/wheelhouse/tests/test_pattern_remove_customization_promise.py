# tests/test_pattern_remove_customization_promise.py
"""The listing says how many OTHER user rules claim the same built-in.

wh-pattern-override-doc-id.3.6. The Pattern Manager tells the person that
"Remove customization" restores the built-in. ``delete_pattern`` removes one
saved block, and this branch deliberately keeps every other user rule that
claims the same built-in, so the promise is false whenever another claimant
remains: the person can delete a rule they wanted and still not get the
original command back.

The dialog cannot tell, because ``get_all_patterns_structured`` reports only
``overrides_builtin`` -- a yes or no per row, with no count. These tests pin
the missing fact. ``other_claimants`` is the number of OTHER user rows
claiming the same built-in, absent when there are none, so the dialog can
reserve the restoration promise for the last claimant.

The counted identity is the one the badge already resolves: the row's own
``slot_identity`` when a built-in carries it, otherwise the single legacy
text candidate. An expression the build would reject resolves to nothing and
claims nothing, and an ambiguous legacy row resolves to nothing either, so
neither is counted and neither is counted against.
"""

from speech.pattern_manager import PatternManager


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'

# Two built-ins with distinct doc_ids and distinct expressions, so a test can
# claim one without touching the other.
SYSTEM = (
    "[[pattern]]\n"
    'doc_id = "escape-key"\n'
    "pattern = '''^escape$'''\n"
    'actions = [{ function = "hk", params = ["esc"] }]\n'
    "\n"
    "[[pattern]]\n"
    'doc_id = "undo-last"\n'
    "pattern = '''^undo$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "z"] }]\n'
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _manager(tmp_path, user_text, system_text=SYSTEM):
    return PatternManager(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _user_entries(manager):
    result = manager.get_all_patterns_structured()
    return result["categories"]["User Patterns"]["patterns"]


def _claiming(doc_id, expression, actions='[{ function = "type", params = ["a"] }]'):
    """A user block that claims ``doc_id`` by carrying it."""
    return (
        "[[pattern]]\n"
        f'doc_id = "{doc_id}"\n'
        f"pattern = '''{expression}'''\n"
        f"actions = {actions}\n"
        "\n"
    )


def _legacy(expression, actions='[{ function = "type", params = ["b"] }]'):
    """A user block saved before doc_ids existed, claimed by its text."""
    return (
        "[[pattern]]\n"
        f"pattern = '''{expression}'''\n"
        f"actions = {actions}\n"
        "\n"
    )


class TestTheCountOfOtherClaimants:
    def test_the_only_claimant_reports_no_others(self, tmp_path):
        """The baseline. Removing this row really does restore the built-in.

        Absent, never 0, the same contract doc_id and unresolved_override
        already follow in ``_build_entry``: the dialog reads presence.
        """
        manager = _manager(tmp_path, _claiming("escape-key", "^grow$"))
        entry = _user_entries(manager)[0]
        assert entry["overrides_builtin"] is True
        assert "other_claimants" not in entry

    def test_two_claimants_of_one_builtin_each_report_one_other(
        self, tmp_path,
    ):
        """Codex case A. Deleting either one leaves the built-in replaced."""
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$")
            + _claiming("escape-key", "^expand$"),
        )
        entries = _user_entries(manager)
        assert [e["other_claimants"] for e in entries] == [1, 1]

    def test_three_claimants_each_report_two_others(self, tmp_path):
        """The count is of the others, not of the claimants."""
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$")
            + _claiming("escape-key", "^expand$")
            + _claiming("escape-key", "^widen$"),
        )
        entries = _user_entries(manager)
        assert [e["other_claimants"] for e in entries] == [2, 2, 2]

    def test_a_named_claimant_and_a_legacy_one_count_each_other(
        self, tmp_path,
    ):
        """Codex's mixed case.

        The legacy row carries no doc_id. Exactly one built-in carries its
        expression, so the merge resolves it to that built-in and the badge
        agrees. It suppresses the same built-in the named row does, so each
        has to see the other.
        """
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$") + _legacy("^escape$"),
        )
        entries = _user_entries(manager)
        assert [e["overrides_builtin"] for e in entries] == [True, True]
        assert [e["other_claimants"] for e in entries] == [1, 1]

    def test_a_remaining_no_action_claimant_is_counted(self, tmp_path):
        """Codex's disabled-remaining case, and the one the build hides.

        A block with an empty action list suppresses the built-in and is
        absent from the runtime list entirely. ``slot_identity`` asks only
        whether the EXPRESSION can build, so this row still claims the
        built-in, and deleting the other row leaves the command switched off
        rather than restored.
        """
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$")
            + _claiming("escape-key", "^hush$", actions="[]"),
        )
        entries = _user_entries(manager)
        assert [e["other_claimants"] for e in entries] == [1, 1]


class TestWhatMustNotCount:
    def test_claimants_of_different_builtins_do_not_count_each_other(
        self, tmp_path,
    ):
        """Two customizations, two built-ins, two true promises."""
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$")
            + _claiming("undo-last", "^revert$"),
        )
        entries = _user_entries(manager)
        assert [e["overrides_builtin"] for e in entries] == [True, True]
        assert all("other_claimants" not in e for e in entries)

    def test_an_independent_user_rule_claims_nothing(self, tmp_path):
        """It carries no built-in identity, so it has no others to report."""
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$") + _legacy("^feed the cat$"),
        )
        entries = _user_entries(manager)
        assert [e["overrides_builtin"] for e in entries] == [True, False]
        assert "other_claimants" not in entries[0]
        assert "other_claimants" not in entries[1]

    def test_an_ambiguous_legacy_row_neither_counts_nor_is_counted(
        self, tmp_path,
    ):
        """wh-pattern-override-doc-id A4 again.

        Two built-ins carry the same expression, so the merge refuses to say
        which one this row replaced and leaves both answering. It replaces
        nothing, so it cannot be one of the claimants keeping a built-in
        suppressed.
        """
        twins = (
            "[[pattern]]\n"
            'doc_id = "escape-key"\n'
            "pattern = '''^escape$'''\n"
            'actions = [{ function = "hk", params = ["esc"] }]\n'
            "\n"
            "[[pattern]]\n"
            'doc_id = "escape-twin"\n'
            "pattern = '''^escape$'''\n"
            'actions = [{ function = "hk", params = ["esc"] }]\n'
        )
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$") + _legacy("^escape$"),
            system_text=twins,
        )
        entries = _user_entries(manager)
        assert entries[1]["overrides_builtin"] is False
        assert entries[1].get("unresolved_override") is True
        assert "other_claimants" not in entries[0]
        assert "other_claimants" not in entries[1]

    def test_an_unbuildable_row_claims_nothing(self, tmp_path):
        """wh-pattern-override-doc-id.3.4.

        The build rejects the expression, so the rule never runs and the
        built-in is still answering. ``slot_identity`` returns None for it,
        and a row that claims nothing cannot keep a built-in suppressed.
        """
        manager = _manager(
            tmp_path,
            _claiming("escape-key", "^grow$")
            + _claiming("escape-key", "^(unclosed$"),
        )
        entries = _user_entries(manager)
        assert entries[1]["overrides_builtin"] is False
        assert "other_claimants" not in entries[0]
        assert "other_claimants" not in entries[1]
