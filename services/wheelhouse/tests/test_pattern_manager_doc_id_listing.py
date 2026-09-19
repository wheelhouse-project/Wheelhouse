# tests/test_pattern_manager_doc_id_listing.py
"""The manager listing agrees with the runtime merge about what overrides what.

wh-pattern-override-doc-id A6. The listing decided "this user pattern
overrides a built-in" with its own copy of the normalized-text key, so it
disagreed with the runtime merge for exactly the case the bead is about: after
a release rewrote a built-in's regex, the runtime lost the association and the
manager also stopped showing the badge. Two copies of a rule are two chances to
disagree; there is now one rule, in ``speech.pattern_identity``.

The listing also has to carry the doc_id out to the dialog, because Customize
reopens a built-in through this data and has to write the id back.
"""

import pytest

from speech.pattern_manager import PatternManager


from doc_id_catalog_fixtures import (
    BUILTIN_BEFORE, BUILTIN_AFTER, USER_OVERRIDE,
    MAX_PATTERN, block, shipped_entry,
)


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _manager(tmp_path, system_text, user_text):
    return PatternManager(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _user_entries(manager):
    result = manager.get_all_patterns_structured()
    return result["categories"]["User Patterns"]["patterns"]


class TestTheOverrideBadgeSurvivesARewrite:
    def test_the_badge_is_shown_before_the_rewrite(self, tmp_path):
        """The baseline, so the test below cannot pass vacuously."""
        manager = _manager(tmp_path, BUILTIN_BEFORE, USER_OVERRIDE)
        assert _user_entries(manager)[0]["overrides_builtin"] is True

    def test_the_badge_is_still_shown_after_the_rewrite(self, tmp_path):
        """The bead's defect on the manager side.

        The built-in's regex changed and the user's did not, so the two
        stopped sharing a text key and the listing reported the user's
        pattern as overriding nothing -- while the runtime had ALSO stopped
        applying it. The user saw a pattern that claimed to be independent
        and did nothing.
        """
        manager = _manager(tmp_path, BUILTIN_AFTER, USER_OVERRIDE)
        assert _user_entries(manager)[0]["overrides_builtin"] is True


class TestWhatMustNotChange:
    def test_a_legacy_entry_without_a_doc_id_is_still_matched_on_text(
        self, tmp_path
    ):
        """Every override saved before doc_ids existed keeps its badge."""
        # Deliberately remove identity to exercise pre-doc_id file semantics.
        builtin = block({
            k: v for k, v in shipped_entry("save").items() if k != "doc_id"
        })
        legacy_user = (
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "shift", "s"] }]\n'
        )
        manager = _manager(tmp_path, builtin, legacy_user)
        assert _user_entries(manager)[0]["overrides_builtin"] is True

    def test_a_user_pattern_with_its_own_doc_id_overrides_nothing(self, tmp_path):
        """An id nothing shipped is a new command, not an override."""
        own = (
            "[[pattern]]\n"
            'doc_id = "my-own-command"\n'
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["win"] }]\n'
        )
        manager = _manager(tmp_path, BUILTIN_BEFORE, own)
        assert _user_entries(manager)[0]["overrides_builtin"] is False

    def test_a_user_regex_equal_to_a_doc_id_does_not_claim_that_builtin(
        self, tmp_path
    ):
        """A doc_id and a pattern text are separate namespaces.

        Without the kind in the identity, a user pattern whose regex text
        happens to read "maximize-window" would be badged as overriding the
        built-in that carries that doc_id.
        """
        lookalike = (
            "[[pattern]]\n"
            "pattern = '''maximize-window'''\n"
            'actions = [{ function = "text", params = ["typed"] }]\n'
        )
        manager = _manager(tmp_path, BUILTIN_BEFORE, lookalike)
        assert _user_entries(manager)[0]["overrides_builtin"] is False

    def test_a_non_string_pattern_is_still_skipped(self, tmp_path):
        """A hand-edited `pattern = 5` must not crash the manager window."""
        broken = "[[pattern]]\npattern = 5\nactions = []\n"
        manager = _manager(tmp_path, BUILTIN_BEFORE, broken + USER_OVERRIDE)
        entries = _user_entries(manager)
        assert len(entries) == 1
        assert entries[0]["raw_pattern"] == MAX_PATTERN


class TestTheListingCarriesTheDocId:
    """Customize reopens a built-in through this data and writes the id back.

    Without the id in the entry the dialog has nothing to persist, so a
    customised built-in would be saved as a new rule that merges on its text
    alone -- the exact association the bead removes.
    """

    def test_a_builtin_entry_carries_its_doc_id(self, tmp_path):
        manager = _manager(tmp_path, BUILTIN_BEFORE, "")
        result = manager.get_all_patterns_structured()
        builtins = [
            entry
            for group in result["categories"].values()
            for entry in group["patterns"]
            if not entry["is_user_created"]
        ]
        assert builtins[0]["doc_id"] == "maximize-window"

    def test_a_user_override_carries_the_doc_id_it_was_saved_with(self, tmp_path):
        manager = _manager(tmp_path, BUILTIN_AFTER, USER_OVERRIDE)
        assert _user_entries(manager)[0]["doc_id"] == "maximize-window"

    def test_an_entry_without_a_doc_id_omits_the_key(self, tmp_path):
        """Absent, not None.

        The dialog decides "does this rule have an identity to keep?" by the
        key's presence, the same contract the phrases and whole_utterance_only
        keys already follow. A None would read as a rule that has one.
        """
        plain = (
            "[[pattern]]\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["win"] }]\n'
        )
        manager = _manager(tmp_path, BUILTIN_BEFORE, plain)
        assert "doc_id" not in _user_entries(manager)[0]

    @pytest.mark.parametrize(
        "value",
        ['"Window Maximize"', '"window--maximize"', "5", "true"],
        ids=["spaces", "doubled-hyphen", "int", "bool"],
    )
    def test_a_malformed_doc_id_is_not_carried(self, tmp_path, value):
        """A hand edit must not hand the dialog an id the merge will not use.

        The merge falls back to the pattern text for a malformed id. Carrying
        the bad value out to the dialog would let a save write it back and
        make the rule look identified when it is not.
        """
        hand_edited = (
            "[[pattern]]\n"
            f"doc_id = {value}\n"
            "pattern = '''^launch$'''\n"
            'actions = [{ function = "hk", params = ["win"] }]\n'
        )
        manager = _manager(tmp_path, BUILTIN_BEFORE, hand_edited)
        assert "doc_id" not in _user_entries(manager)[0]
