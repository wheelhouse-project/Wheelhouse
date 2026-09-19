# tests/test_pattern_legacy_override_migration.py
"""An override saved before doc_ids existed still finds its built-in.

wh-pattern-override-doc-id A4. Every shipped pattern carries a doc_id (319 of
319 in speech/config/patterns.toml), so the moment the merge began keying on
doc_id, a user file written before doc_ids existed identified as pattern text
against built-ins that identified as ids -- and matched none of them. Every
such override was appended after the built-in it was written to replace, and
lost. A no-action override, which is how the user switches a built-in off,
lost the same way: the command came back.

The association those files did have is their pattern text. It is recoverable
when exactly one built-in carries that text, and that is the only case this
migrates. Where two built-ins share the text there is no way to tell which one
the user meant, so the entry stays unresolved: it is kept, it is listed, and
the fact that a built-in is still answering is reported rather than passed
over.
"""

import logging

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_manager import PatternManager


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'

# The shipped built-in, carrying the durable name every shipped pattern has.
BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)
# A second built-in, so a count can tell "replaced the right one" from
# "replaced whatever was first".
OTHER_BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-restore"\n'
    "pattern = '''^restore$'''\n"
    'actions = [{ function = "hk", params = ["win", "down"] }]\n'
)
# The user file as it was written before doc_ids existed: the same expression
# and no name at all.
LEGACY_OVERRIDE = (
    "[[pattern]]\n"
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
)
LEGACY_SWITCHED_OFF = (
    "[[pattern]]\n"
    "pattern = '''^maximize$'''\n"
    "actions = []\n"
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _files(tmp_path, system_text, user_text):
    return (
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _catalog(tmp_path, system_text, user_text):
    return PatternCatalog(*_files(tmp_path, system_text, user_text))


def _manager(tmp_path, system_text, user_text):
    return PatternManager(*_files(tmp_path, system_text, user_text))


def _user_entries(manager):
    result = manager.get_all_patterns_structured()
    return result["categories"]["User Patterns"]["patterns"]


class TestOneCandidateMigrates:
    def test_a_legacy_override_still_wins_over_its_builtin(self, tmp_path):
        """The defect, at the runtime.

        The built-in names itself and the override does not, so under the
        identity rule alone the two share nothing and the override is
        appended behind the built-in it was written to replace.
        """
        catalog = _catalog(tmp_path, BUILTIN, LEGACY_OVERRIDE)
        matches = catalog.get_matching_patterns("maximize")
        assert len(matches) == 1
        assert matches[0][2]["actions"][0]["params"] == ["ctrl", "alt", "m"]

    def test_it_replaces_the_builtin_rather_than_joining_it(self, tmp_path):
        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert catalog.pattern_count == 2

    def test_it_takes_the_builtin_position_rather_than_the_end(self, tmp_path):
        """Order decides which of two matching replacements runs first.

        Appending the override moves it behind every built-in, which changes
        the answer for an order-sensitive rule even when the override does
        win.
        """
        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        raw = [p.get("raw_pattern") for p in catalog.all_patterns]
        assert raw == ["^maximize$", "^restore$"]
        assert catalog.all_patterns[0]["is_user"] is True

    def test_a_legacy_switch_off_keeps_the_builtin_off(self, tmp_path):
        """The silent reactivation this criterion names.

        An override with no actions is how the user turns a built-in off.
        Failing to associate it does not merely lose a customization -- it
        turns a command the user deliberately switched off back on.
        """
        catalog = _catalog(tmp_path, BUILTIN, LEGACY_SWITCHED_OFF)
        assert catalog.get_matching_patterns("maximize") == []
        assert catalog.pattern_count == 0

    def test_the_last_of_two_legacy_copies_holds_the_slot(self, tmp_path):
        """Two entries with one text behave as they did before doc_ids.

        The old merge keyed system and user alike on text, so a second copy
        replaced the first in the built-in's slot. Nothing about this change
        should alter that.
        """
        second = (
            "[[pattern]]\n"
            "pattern = '''^maximize$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "alt", "z"] }]\n'
        )
        catalog = _catalog(tmp_path, BUILTIN, LEGACY_OVERRIDE + second)
        assert catalog.pattern_count == 1
        matches = catalog.get_matching_patterns("maximize")
        assert matches[0][2]["actions"][0]["params"] == ["ctrl", "alt", "z"]


class TestNoCandidateStaysIndependent:
    def test_a_legacy_rule_matching_nothing_shipped_is_its_own(self, tmp_path):
        own = (
            "[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["win"] }]\n'
        )
        catalog = _catalog(tmp_path, BUILTIN, own)
        assert catalog.pattern_count == 2

    def test_a_user_rule_with_its_own_doc_id_is_not_migrated_by_text(
        self, tmp_path,
    ):
        """A named rule is answered by its name, never by its text.

        Text is the fallback for entries that have no name. Letting it also
        speak for a named one would let a rule whose expression happens to
        equal a built-in's take that built-in over, which is the ambiguity
        the doc_id exists to remove.
        """
        named = (
            "[[pattern]]\n"
            'doc_id = "my-own-command"\n'
            "pattern = '''^maximize$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
        )
        catalog = _catalog(tmp_path, BUILTIN, named)
        assert catalog.pattern_count == 2


class TestTwoCandidatesStayUnresolved:
    """Two built-ins with one expression: nothing says which was meant."""

    TWINS = (
        "[[pattern]]\n"
        'doc_id = "window-maximize"\n'
        "pattern = '''^maximize$'''\n"
        'actions = [{ function = "hk", params = ["win", "up"] }]\n'
        "[[pattern]]\n"
        'doc_id = "window-zoom"\n'
        "pattern = '''^maximize$'''\n"
        'actions = [{ function = "hk", params = ["win", "z"] }]\n'
    )

    def test_neither_builtin_is_taken_over(self, tmp_path):
        catalog = _catalog(tmp_path, self.TWINS, LEGACY_OVERRIDE)
        assert catalog.pattern_count == 3

    def test_the_override_is_kept_rather_than_dropped(self, tmp_path):
        """Kept and still running, at the end of the order.

        Dropping it would destroy the user's rule outright, which is worse
        than the wrong precedence it has now.
        """
        catalog = _catalog(tmp_path, self.TWINS, LEGACY_OVERRIDE)
        last = catalog.all_patterns[-1]
        assert last["is_user"] is True
        assert last["actions"][0]["params"] == ["ctrl", "alt", "m"]

    def test_the_ambiguity_is_reported_with_both_candidates(
        self, tmp_path, caplog,
    ):
        """Not silent.

        This is the case where a built-in the user may have switched off is
        answering again. The log names the expression and both names, which
        is what a reader needs to decide which one the entry meant.
        """
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            _catalog(tmp_path, self.TWINS, LEGACY_OVERRIDE)
        warnings = [
            r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        assert any(
            "^maximize$" in m
            and "window-maximize" in m
            and "window-zoom" in m
            for m in warnings
        ), warnings


class TestTheManagerAgreesWithTheMerge:
    def test_a_migrated_legacy_override_is_badged(self, tmp_path):
        """The badge answers the same question the merge does.

        Without this the user sees a pattern that claims to be independent
        while it is in fact replacing a built-in, and Remove customization --
        which is gated on the badge -- is not offered for it.
        """
        manager = _manager(tmp_path, BUILTIN, LEGACY_OVERRIDE)
        entry = _user_entries(manager)[0]
        assert entry["overrides_builtin"] is True
        assert "unresolved_override" not in entry

    def test_an_ambiguous_legacy_override_is_listed_as_unresolved(
        self, tmp_path,
    ):
        """Listed, not badged, and marked.

        It is not overriding anything, so the override badge would lie. It
        is also not an ordinary user rule, because a built-in it may have
        been replacing is still answering. The manager says so.
        """
        manager = _manager(tmp_path, TestTwoCandidatesStayUnresolved.TWINS,
                           LEGACY_OVERRIDE)
        entries = _user_entries(manager)
        assert len(entries) == 1
        assert entries[0]["overrides_builtin"] is False
        assert entries[0]["unresolved_override"] is True

    def test_an_independent_user_rule_is_neither(self, tmp_path):
        own = (
            "[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["win"] }]\n'
        )
        manager = _manager(tmp_path, BUILTIN, own)
        entry = _user_entries(manager)[0]
        assert entry["overrides_builtin"] is False
        assert "unresolved_override" not in entry


class TestTheDraftPreviewAgreesWithTheMerge:
    """A5/A6's rule applied to the legacy path: one answer, three readers."""

    def test_a_legacy_draft_lands_in_the_builtin_slot(self, tmp_path):
        from speech.pattern_tester import _build_draft_entry, _simulate_merge

        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, "")
        draft_entry, error = _build_draft_entry({
            "trigger": "maximize",
            "pattern_type": "command",
            "action_type": "hotkey",
            "action_params": {"keys": ["ctrl", "alt", "m"]},
        })
        assert error is None
        assert draft_entry is not None
        simulated = _simulate_merge(catalog.all_patterns, draft_entry, None)
        assert [e.get("raw_pattern") for e in simulated] == [
            "^maximize$", "^restore$",
        ]
        assert simulated[0]["is_user"] is True

    def test_an_ambiguous_legacy_draft_appends(self, tmp_path):
        from speech.pattern_tester import _build_draft_entry, _simulate_merge

        catalog = _catalog(tmp_path, TestTwoCandidatesStayUnresolved.TWINS, "")
        draft_entry, error = _build_draft_entry({
            "trigger": "maximize",
            "pattern_type": "command",
            "action_type": "hotkey",
            "action_params": {"keys": ["ctrl", "alt", "m"]},
        })
        assert error is None
        assert draft_entry is not None
        simulated = _simulate_merge(catalog.all_patterns, draft_entry, None)
        assert len(simulated) == 3
        assert simulated[-1] is draft_entry


@pytest.mark.parametrize(
    "user_text",
    ["[[pattern]]\npattern = 5\nactions = []\n",
     "[[pattern]]\nactions = []\n"],
    ids=["non-string-pattern", "no-pattern-key"],
)
def test_a_malformed_user_entry_does_not_break_the_merge(tmp_path, user_text):
    """A hand edit must not take the whole catalog down."""
    catalog = _catalog(tmp_path, BUILTIN, user_text)
    assert catalog.pattern_count == 1
