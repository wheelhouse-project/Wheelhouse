"""A saved override keeps its built-in and its precedence across a rewrite.

wh-pattern-override-doc-id A1. The catalog used to decide "this user entry
replaces that built-in" by comparing normalized pattern text. A release that
rewrote a built-in's regex therefore orphaned every override saved against it:
the two stopped sharing a key, the merge appended the user entry after all the
built-ins instead of replacing one, and the built-in matched first and won.
The user's customisation silently stopped applying.

These tests rewrite a built-in's regex between two loads and assert the
override still wins.
"""

import logging

import pytest

from speech.pattern_catalog import PatternCatalog


from doc_id_catalog_fixtures import (
    BUILTIN_BEFORE, BUILTIN_AFTER, USER_OVERRIDE,
    MAXIMIZE, MAX_PATTERN, RESTORE, RESTORE_PATTERN, block, shipped_entry,
)


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def user_file(tmp_path):
    return _write(tmp_path / "user_patterns.toml", HOTWORD + USER_OVERRIDE)


def _actions_for(catalog, word):
    matches = catalog.get_matching_patterns(word)
    assert len(matches) == 1, (
        f"expected exactly one pattern for {word!r}, got {len(matches)}. "
        "More than one means the override was appended beside the built-in "
        "instead of replacing it, which is the defect this file guards."
    )
    _compiled, _type, data = matches[0]
    return data["actions"]


class TestOverrideSurvivesARewrite:
    def test_the_override_wins_before_the_rewrite(self, tmp_path, user_file):
        """The baseline. Without this, the test below could pass vacuously."""
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        catalog = PatternCatalog(system, user_file)
        assert _actions_for(catalog, "maximize")[0]["params"] == ["ctrl", "alt", "m"]

    def test_the_override_still_wins_after_the_rewrite(self, tmp_path, user_file):
        """The bead's defect, stated as a test.

        The built-in's regex changed and the user's did not. Under the old
        text key the two no longer matched, so the override was appended and
        the built-in won. Under doc_id they still match.
        """
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_AFTER)
        catalog = PatternCatalog(system, user_file)
        assert _actions_for(catalog, "maximize")[0]["params"] == ["ctrl", "alt", "m"]

    def test_the_override_replaces_rather_than_joins_the_builtin(
        self, tmp_path, user_file
    ):
        """One entry, not two.

        Precedence alone is not enough. An appended duplicate that happened to
        sort first would satisfy an actions check while leaving a second copy
        of the command in the catalog, which changes what other patterns see.
        """
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_AFTER)
        catalog = PatternCatalog(system, user_file)
        assert catalog.pattern_count == 1

    def test_the_override_keeps_the_builtin_position_after_the_rewrite(
        self, tmp_path
    ):
        """Order matters for replacement patterns, so the slot must be kept.

        The override must land where the built-in stood, not at the end. A
        later built-in that the user did not touch has to keep matching after
        it, not before it.
        """
        second_builtin = block(RESTORE)
        system = _write(
            tmp_path / "patterns.toml", HOTWORD + BUILTIN_AFTER + second_builtin
        )
        user = _write(tmp_path / "user_patterns.toml", HOTWORD + USER_OVERRIDE)
        catalog = PatternCatalog(system, user)
        raw = [entry["raw_pattern"] for entry in catalog.all_patterns]
        assert raw == [MAX_PATTERN, RESTORE_PATTERN], (
            "the override must sit in the built-in's own slot; "
            f"the merged order is {raw}"
        )


class TestWhatMustNotChange:
    def test_an_entry_without_a_doc_id_still_merges_on_its_text(self, tmp_path):
        """Every override saved before doc_ids existed must keep working.

        A user file written by an older build carries no doc_id. It has only
        the pattern text to associate on, so the text key has to stay live.
        """
        # Deliberately remove identity to exercise pre-doc_id file semantics.
        builtin = block({
            k: v for k, v in shipped_entry("save").items() if k != "doc_id"
        })
        legacy_user = (
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "shift", "s"] }]\n'
        )
        system = _write(tmp_path / "patterns.toml", HOTWORD + builtin)
        user = _write(tmp_path / "user_patterns.toml", HOTWORD + legacy_user)
        catalog = PatternCatalog(system, user)
        assert _actions_for(catalog, "save")[0]["params"] == ["ctrl", "shift", "s"]

    def test_a_user_entry_with_a_new_doc_id_is_added_not_merged(self, tmp_path):
        """An id nothing shipped is a new command, not an override."""
        user = _write(
            tmp_path / "user_patterns.toml",
            HOTWORD
            + (
                "[[pattern]]\n"
                'doc_id = "my-own-command"\n'
                "pattern = '''^launch$'''\n"
                'actions = [{ function = "hk", params = ["win"] }]\n'
            ),
        )
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        catalog = PatternCatalog(system, user)
        assert catalog.pattern_count == 2

    def test_a_user_regex_equal_to_a_doc_id_does_not_capture_that_builtin(
        self, tmp_path
    ):
        """The two kinds of identity are separate namespaces.

        A user pattern whose regex text reads "maximize-window" must not
        replace the built-in whose doc_id is "maximize-window". Merging both
        kinds into one key space would let an unrelated command be captured
        silently.
        """
        user = _write(
            tmp_path / "user_patterns.toml",
            HOTWORD
            + (
                "[[pattern]]\n"
                "pattern = '''maximize-window'''\n"
                'actions = [{ function = "text", params = ["typed"] }]\n'
            ),
        )
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        catalog = PatternCatalog(system, user)
        assert catalog.pattern_count == 2, (
            "the user entry must be added beside the built-in, not merged "
            "onto it by a collision between a doc_id and a pattern text"
        )


class TestAnUnbuildableOverrideDoesNotDeleteItsBuiltin:
    """wh-pattern-override-doc-id.2.3.

    A hand edit can leave a user entry carrying a valid doc_id and a pattern
    the catalog cannot build. The identity rule used to award the built-in's
    identity on the doc_id alone, so the merge spent the built-in's slot on
    the entry and the build then dropped it. The command disappeared, the
    pattern-manager window still listed the built-in, and the only record was
    one ERROR line in the log.
    """

    BROKEN_NON_STRING = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        "pattern = 5\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
    )
    BROKEN_MISSING = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
    )

    def test_a_non_string_pattern_leaves_the_builtin_answering(self, tmp_path):
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        user = _write(
            tmp_path / "user_patterns.toml", HOTWORD + self.BROKEN_NON_STRING
        )
        catalog = PatternCatalog(system, user)
        assert catalog.pattern_count == 1, (
            "the broken entry is dropped at build time, so the built-in must "
            "be the one pattern left; a count of 0 means the merge gave the "
            "built-in's slot away before the drop"
        )
        assert _actions_for(catalog, "maximize") == MAXIMIZE["actions"]

    def test_a_missing_pattern_leaves_the_builtin_answering(self, tmp_path):
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        user = _write(
            tmp_path / "user_patterns.toml", HOTWORD + self.BROKEN_MISSING
        )
        catalog = PatternCatalog(system, user)
        assert catalog.pattern_count == 1
        assert _actions_for(catalog, "maximize") == MAXIMIZE["actions"]

    def test_the_same_break_without_a_doc_id_already_kept_the_builtin(
        self, tmp_path
    ):
        """The contrast that names the cause.

        Identical garbage, no doc_id. This passed before the fix too. It is
        here so a later reader can see that the doc_id was the whole
        difference between a dropped entry and a deleted command.
        """
        broken = (
            "[[pattern]]\n"
            "pattern = 5\n"
            'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
        )
        system = _write(tmp_path / "patterns.toml", HOTWORD + BUILTIN_BEFORE)
        user = _write(tmp_path / "user_patterns.toml", HOTWORD + broken)
        catalog = PatternCatalog(system, user)
        assert catalog.pattern_count == 1
        assert _actions_for(catalog, "maximize") == MAXIMIZE["actions"]


class TestTheMergeKeepsEveryUserEntry:
    """wh-pattern-override-doc-id.2.1.

    Two user entries can claim one built-in. The merge used to write the
    second one over the first, so a rule the user wrote stopped running and
    nothing said so. Before this branch the key was the pattern text, so two
    entries with different triggers had different keys and both survived; the
    doc_id key is what made them collide.

    The rule: the LAST claimant in user-file order holds the built-in's slot,
    and every earlier claimant is appended after all system entries. Last
    wins because it is the newer customisation and, in the sequence that
    reaches this, it is the one carrying the built-in's own trigger.
    """

    SECOND_BUILTIN = block(RESTORE)
    # The user's first customisation, its trigger since edited to something
    # of their own. It still carries the built-in's doc_id.
    FIRST_CLAIM = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        "pattern = '''^grow$'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
    )
    # Customize pressed a second time on the same built-in.
    SECOND_CLAIM = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        f"pattern = '''{MAX_PATTERN}'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "n"] }]\n'
    )
    # A pre-doc_id override of the same built-in: no id, the built-in's own
    # expression. It reaches the same slot down the legacy path.
    LEGACY_CLAIM = (
        "[[pattern]]\n"
        f"pattern = '''{MAX_PATTERN}'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "p"] }]\n'
    )

    def _catalog(self, tmp_path, user_body):
        system = _write(
            tmp_path / "patterns.toml",
            HOTWORD + BUILTIN_BEFORE + self.SECOND_BUILTIN,
        )
        user = _write(tmp_path / "user_patterns.toml", HOTWORD + user_body)
        return PatternCatalog(system, user)

    def test_both_claims_on_one_builtin_survive(self, tmp_path):
        """The defect, stated as a test. The first claim used to vanish."""
        catalog = self._catalog(
            tmp_path, self.FIRST_CLAIM + self.SECOND_CLAIM
        )
        assert catalog.pattern_count == 3, (
            "two user entries and the untouched second built-in; a count of "
            "2 means the merge wrote one user entry over the other"
        )
        assert _actions_for(catalog, "grow")[0]["params"] == ["ctrl", "alt", "m"]
        assert _actions_for(catalog, "maximize")[0]["params"] == [
            "ctrl", "alt", "n",
        ]

    def test_the_last_claim_holds_the_builtin_slot(self, tmp_path):
        """Order matters for replacement patterns, so say which one wins."""
        catalog = self._catalog(
            tmp_path, self.FIRST_CLAIM + self.SECOND_CLAIM
        )
        raw = [entry["raw_pattern"] for entry in catalog.all_patterns]
        assert raw == [MAX_PATTERN, RESTORE_PATTERN, "^grow$"], (
            "the last claimant takes the built-in's slot and the earlier one "
            f"is appended after every built-in; the merged order is {raw}"
        )

    def test_a_legacy_claim_does_not_evict_the_named_one(self, tmp_path):
        """The same loss down the other path into the slot.

        candidates is built from the SYSTEM entries only, so the built-in's
        own text still matches after a named user entry has taken its slot.
        The legacy entry then reached that slot and overwrote it.
        """
        catalog = self._catalog(
            tmp_path, self.FIRST_CLAIM + self.LEGACY_CLAIM
        )
        assert catalog.pattern_count == 3
        assert _actions_for(catalog, "grow")[0]["params"] == ["ctrl", "alt", "m"]
        assert _actions_for(catalog, "maximize")[0]["params"] == [
            "ctrl", "alt", "p",
        ]

    def test_the_displaced_entry_is_reported(self, tmp_path, caplog):
        """A rule that moved out of its built-in's slot is not moved silently."""
        with caplog.at_level(logging.WARNING, logger="speech.pattern_catalog"):
            self._catalog(tmp_path, self.FIRST_CLAIM + self.SECOND_CLAIM)
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        joined = "\n".join(warnings)
        assert "^grow$" in joined and MAX_PATTERN in joined, (
            "the warning must name both triggers so a reader can tell which "
            f"rule moved; the warnings were {warnings}"
        )
        assert "maximize-window" in joined

    def test_one_claim_alone_still_replaces_in_place(self, tmp_path):
        """The bounding test: the ordinary single override must not move.

        A fix that appended every user entry would satisfy the tests above
        and lose the built-in's position, which is the whole point of the
        replace-in-place merge. This passes before AND after.
        """
        catalog = self._catalog(tmp_path, self.SECOND_CLAIM)
        raw = [entry["raw_pattern"] for entry in catalog.all_patterns]
        assert raw == [MAX_PATTERN, RESTORE_PATTERN]
        assert catalog.pattern_count == 2

    def test_a_second_copy_of_one_trigger_supersedes_the_first(self, tmp_path):
        """The one case that keeps a single rule, and why.

        Appending a rule whose trigger equals the rule that replaced it adds
        one that can never match, and puts two rules on one phrase where the
        catalog has only ever held one. Two copies of one expression
        collapsed before doc_ids existed;
        tests/test_pattern_legacy_override_migration.py pins the same rule
        down the legacy path.
        """
        same_trigger_again = (
            "[[pattern]]\n"
            'doc_id = "maximize-window"\n'
            f"pattern = '''{MAX_PATTERN}'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "alt", "q"] }]\n'
        )
        catalog = self._catalog(
            tmp_path, self.SECOND_CLAIM + same_trigger_again
        )
        assert catalog.pattern_count == 2, (
            "the second copy supersedes the first outright, so only it and "
            "the untouched second built-in remain"
        )
        assert _actions_for(catalog, "maximize")[0]["params"] == [
            "ctrl", "alt", "q",
        ]


class TestAThirdClaimantAfterALegacyDisplacement:
    """wh-pattern-override-doc-id.3.2.

    The .2.1 fix moves a displaced claimant to the end of the merged list.
    It left two pieces of bookkeeping undone. The appended row was never
    marked as claimed, and the built-in's own identity was repointed at
    that appended row. A third claimant of the same built-in then followed
    the repointed identity, found what looked like a free built-in slot,
    and wrote over the moved rule. The user's saved rule stopped running,
    and the newest customisation never reached the built-in's position.

    The sequence is three ordinary saves, no hand edit and no race:
    Customize with an edited trigger, then Add or Duplicate using the
    built-in's own phrase, then Customize again with another trigger.

    The rule these pin: the built-in's slot belongs to the built-in's
    identity for the whole merge, and every appended user row is owned by
    the user entry sitting in it.
    """

    SECOND_BUILTIN = block(RESTORE)
    # 1. Customize, then edit the trigger. Carries the built-in's doc_id.
    NAMED_FIRST = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        "pattern = '''^grow$'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'
    )
    # 2. Add or Duplicate, using the built-in's own phrase. No doc_id, so
    # it reaches the same slot down the legacy path.
    LEGACY_MIDDLE = (
        "[[pattern]]\n"
        f"pattern = '''{MAX_PATTERN}'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "p"] }]\n'
    )
    # 3. Customize again, another trigger. This is the newest rule, so it
    # is the one that must hold the built-in's place in the order.
    NAMED_LAST = (
        "[[pattern]]\n"
        'doc_id = "maximize-window"\n'
        "pattern = '''^expand$'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "n"] }]\n'
    )
    # 4. A second copy of the legacy expression. It must find the moved
    # copy and supersede it, not add a second rule on one phrase.
    LEGACY_AGAIN = (
        "[[pattern]]\n"
        f"pattern = '''{MAX_PATTERN}'''\n"
        'actions = [{ function = "hk", params = ["ctrl", "alt", "q"] }]\n'
    )

    def _catalog(self, tmp_path, user_body):
        system = _write(
            tmp_path / "patterns.toml",
            HOTWORD + BUILTIN_BEFORE + self.SECOND_BUILTIN,
        )
        user = _write(tmp_path / "user_patterns.toml", HOTWORD + user_body)
        return PatternCatalog(system, user)

    def test_all_three_claimants_still_run(self, tmp_path):
        """The loss, stated as a test. The first claim used to disappear."""
        catalog = self._catalog(
            tmp_path,
            self.NAMED_FIRST + self.LEGACY_MIDDLE + self.NAMED_LAST,
        )
        assert catalog.pattern_count == 4, (
            "three user rules and the untouched second built-in; a count of "
            "3 means the third claimant wrote over the rule the legacy one "
            "displaced"
        )
        assert _actions_for(catalog, "grow")[0]["params"] == [
            "ctrl", "alt", "m",
        ]
        assert _actions_for(catalog, "maximize")[0]["params"] == [
            "ctrl", "alt", "p",
        ]
        assert _actions_for(catalog, "expand")[0]["params"] == [
            "ctrl", "alt", "n",
        ]

    def test_the_last_claimant_still_takes_the_builtin_slot(self, tmp_path):
        """A legacy claimant in the middle must not move the slot itself.

        The built-in's identity names the built-in's position for the whole
        merge. Repointing it at a displaced row sent the newest rule to the
        end of the command order, behind every built-in.
        """
        catalog = self._catalog(
            tmp_path,
            self.NAMED_FIRST + self.LEGACY_MIDDLE + self.NAMED_LAST,
        )
        raw = [entry["raw_pattern"] for entry in catalog.all_patterns]
        assert raw == ["^expand$", RESTORE_PATTERN, "^grow$", MAX_PATTERN], (
            "the newest claimant holds the built-in's slot and the two it "
            "displaced follow every built-in, in the order they moved; the "
            f"merged order is {raw}"
        )

    def test_a_repeated_legacy_expression_still_supersedes_its_own_copy(
        self, tmp_path,
    ):
        """The displaced-row lookup stays, and this is what it is for.

        A second copy of one expression has to find the copy that moved and
        replace it. Anchoring the built-in's identity must not take that
        away, or two rules end up on one phrase -- which the .2.1 rule
        exists to prevent.
        """
        catalog = self._catalog(
            tmp_path,
            self.NAMED_FIRST
            + self.LEGACY_MIDDLE
            + self.NAMED_LAST
            + self.LEGACY_AGAIN,
        )
        assert catalog.pattern_count == 4, (
            "the repeated expression supersedes the copy that moved rather "
            "than joining it"
        )
        assert _actions_for(catalog, "maximize")[0]["params"] == [
            "ctrl", "alt", "q",
        ]
        assert _actions_for(catalog, "grow")[0]["params"] == [
            "ctrl", "alt", "m",
        ]
        assert _actions_for(catalog, "expand")[0]["params"] == [
            "ctrl", "alt", "n",
        ]

    def test_a_disabled_claimant_is_kept_through_the_same_sequence(
        self, tmp_path,
    ):
        """An actions=[] rule builds no runtime row, so assert on the merge.

        A disabled override suppresses its built-in by holding the slot
        (A5). It contributes nothing to ``all_patterns``, so losing one is
        invisible from the runtime list -- which is why this reads the
        merged raw entries directly.
        """
        catalog = self._catalog(tmp_path, self.NAMED_LAST)
        system = [MAXIMIZE]
        user = [
            {"doc_id": "maximize-window", "pattern": "^grow$", "actions": []},
            {
                "pattern": MAX_PATTERN,
                "actions": [
                    {"function": "hk", "params": ["ctrl", "alt", "p"]},
                ],
            },
            {
                "doc_id": "maximize-window",
                "pattern": "^expand$",
                "actions": [
                    {"function": "hk", "params": ["ctrl", "alt", "n"]},
                ],
            },
        ]
        merged = catalog._merge_entries(system, user)
        expressions = [entry["pattern"] for entry in merged]
        assert expressions == ["^expand$", "^grow$", MAX_PATTERN], (
            "the disabled rule is displaced, not dropped; the merged "
            f"expressions were {expressions}"
        )
