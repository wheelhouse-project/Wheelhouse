# tests/test_pattern_legacy_id_written_at_load.py
"""A recovered association is written into the user file, so it lasts.

wh-pattern-override-doc-id.3.1. An override saved before doc_ids existed
carries no name. The merge recovers its built-in from the expression text
when exactly one built-in carries that text, but only in memory: the file
still says nothing, so the next release that rewrites the built-in's
expression breaks the association again and the built-in comes back.

The fix writes the recovered name into the block, together with the key
the editor writes into every block it creates. That is the same pair a
Customize saved by the current editor carries, so a migrated block ends up
in the shape the editor would have written.

Boundaries this file pins:

  Only an UNAMBIGUOUS resolution is written. Two built-ins sharing the
  text means the file does not say which one was meant, and a wrong guess
  hands the person's replacement to a command they never touched (A4).

  Only the resolved blocks change. Comments, blank lines, key order and
  every other block survive byte for byte, because a person may have hand
  written this file.

  One writer. The catalog writes only when the caller asks for it, and the
  only caller that asks is the Logic process load in speech_handler.py.
  Every other loader -- the Pattern Manager window's own PatternManager,
  the try-it preview's simulation -- stays in memory.
"""

import logging
import os

import pytest

from speech.pattern_catalog import PatternCatalog


HOTWORD = 'COMMAND_HOTWORD = "x-ray"\n\n'

BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)
OTHER_BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-restore"\n'
    "pattern = '''^restore$'''\n"
    'actions = [{ function = "hk", params = ["win", "down"] }]\n'
)
# Two built-ins carrying ONE expression: the A4 case the merge refuses to
# resolve, and the migration must refuse to write.
TWIN_A = (
    "[[pattern]]\n"
    'doc_id = "twin-one"\n'
    "pattern = '''^twin$'''\n"
    'actions = [{ function = "hk", params = ["f1"] }]\n'
)
TWIN_B = (
    "[[pattern]]\n"
    'doc_id = "twin-two"\n'
    "pattern = '''^twin$'''\n"
    'actions = [{ function = "hk", params = ["f2"] }]\n'
)

MINE = 'actions = [{ function = "hk", params = ["ctrl", "alt", "m"] }]\n'

LEGACY_OVERRIDE = "[[pattern]]\n" "pattern = '''^maximize$'''\n" + MINE
# The same block with no actions at all: the person silenced the command
# rather than replacing it. This is the shape the review reproduced with
# the shipped escape entry.
LEGACY_SUPPRESSION = (
    "[[pattern]]\n" "pattern = '''^maximize$'''\n" "actions = []\n"
)
# The built-in after a release rewrites its expression. The old words still
# work, so a person who never learned the new ones notices nothing -- which
# is why the orphaned override is silent.
REWRITTEN_BUILTIN = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^(?:maximize|make it big)$'''\n"
    'actions = [{ function = "hk", params = ["win", "up"] }]\n'
)
# The same block surrounded by the things a person writes by hand. Nothing
# here may move.
LEGACY_WITH_HAND_WRITING = (
    "# my own notes about this file\n"
    "\n"
    "[[pattern]]\n"
    "# the one I changed\n"
    "pattern = '''^maximize$'''\n"
    "requires_hotword = false\n"
    + MINE
    + "\n"
    "[[pattern]]\n"
    "pattern = '''^my own thing$'''\n"
    'actions = [{ function = "text", params = ["mine"] }]\n'
)

BACKUP_SUFFIX = ".pre-doc-id-migration.bak"


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _files(tmp_path, system_text, user_text):
    return (
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


def _catalog(tmp_path, system_text, user_text, *, migrate=True):
    system_file, user_file = _files(tmp_path, system_text, user_text)
    return PatternCatalog(system_file, user_file, migrate_legacy_ids=migrate)


def _user_text(tmp_path):
    return (tmp_path / "user_patterns.toml").read_text(encoding="utf-8")


def _blocks(tmp_path):
    """The [[pattern]] tables now in the user file."""
    import tomllib

    with open(tmp_path / "user_patterns.toml", "rb") as fh:
        return tomllib.load(fh).get("pattern", [])


def _actions_of(entries):
    """The action lists of matching rules, in merged order.

    ``get_matching_patterns`` returns ``(compiled, pattern_type, data)``
    triples, so the actions live in the third member.
    """
    return [data["actions"] for _compiled, _kind, data in entries]


class TestTheRecoveredNameIsWritten:
    def test_the_doc_id_reaches_the_file(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert _blocks(tmp_path)[0].get("doc_id") == "window-maximize"

    def test_the_block_also_carries_the_editors_own_key(self, tmp_path):
        """The pair a Customize saved by the current editor carries.

        ``create_pattern`` writes both keys for a Customize, so a migrated
        block ends up in the shape the editor would have written. The key
        does not make the block independent: ``is_own_rule`` also requires
        the absence of a doc_id.
        """
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert _blocks(tmp_path)[0].get("origin") == "user"

    def test_the_person_still_gets_their_own_action(self, tmp_path):
        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]

    def test_the_association_survives_a_rewrite_of_the_builtin(
        self, tmp_path,
    ):
        """The whole point of the bead, end to end.

        After the migration the file names the built-in, so a release that
        rewrites the built-in's expression no longer orphans the override.
        The override takes the built-in's place and is the ONLY rule that
        answers the phrase. Without the written name the rewritten built-in
        keeps its own place, the override is appended behind it, and the
        built-in answers first -- which is the defect this bead is about.
        """
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        rewritten = (
            "[[pattern]]\n"
            'doc_id = "window-maximize"\n'
            "pattern = '''^(?:maximize|make it big)$'''\n"
            'actions = [{ function = "hk", params = ["win", "up"] }]\n'
        )
        _write(tmp_path / "patterns.toml", HOTWORD + rewritten + OTHER_BUILTIN)
        after = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert _actions_of(after.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]

    def test_without_the_write_the_rewrite_still_orphans_the_override(
        self, tmp_path,
    ):
        """The same rewrite, over a file the load did not migrate.

        This is what the person has today: the built-in answers first and
        their own action never runs. It is here so the test above cannot
        pass for a reason that has nothing to do with the written name.
        """
        _catalog(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE, migrate=False,
        )
        rewritten = (
            "[[pattern]]\n"
            'doc_id = "window-maximize"\n'
            "pattern = '''^(?:maximize|make it big)$'''\n"
            'actions = [{ function = "hk", params = ["win", "up"] }]\n'
        )
        _write(tmp_path / "patterns.toml", HOTWORD + rewritten + OTHER_BUILTIN)
        after = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert _actions_of(after.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["win", "up"]}],
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]


class TestASuppressingOverrideStaysSuppressing:
    """An override with no actions turns a built-in off, and must stay off.

    This is the case the review reproduced with the shipped ``escape``
    entry: the person silenced a command, and a release that rewrote the
    built-in's expression turned it back on with no message. The empty
    action list also means the entry never reaches the runtime structures
    -- ``_build_structures`` builds an entry only when it has both an
    expression and actions -- so these tests pin that the migration still
    records and writes a block the runtime itself drops.
    """

    def test_the_silenced_block_gets_its_name(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_SUPPRESSION)
        assert _blocks(tmp_path)[0].get("doc_id") == "window-maximize"

    def test_the_command_stays_off_after_a_rewrite(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_SUPPRESSION)
        _write(
            tmp_path / "patterns.toml",
            HOTWORD + REWRITTEN_BUILTIN + OTHER_BUILTIN,
        )
        after = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert after.get_matching_patterns("maximize") == []

    def test_without_the_write_the_rewrite_turns_it_back_on(self, tmp_path):
        """The control: this is what the person has today.

        Without the written name the rewritten built-in keeps its own
        slot, the silenced override no longer covers it, and the command
        the person switched off answers again.
        """
        _catalog(
            tmp_path,
            BUILTIN + OTHER_BUILTIN,
            LEGACY_SUPPRESSION,
            migrate=False,
        )
        _write(
            tmp_path / "patterns.toml",
            HOTWORD + REWRITTEN_BUILTIN + OTHER_BUILTIN,
        )
        after = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert _actions_of(after.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["win", "up"]}],
        ]


class TestTheFileAndTheCatalogAgree:
    """The load parses, merges, builds, and only then writes.

    It does not write first and read the file again. The two answers still
    agree, because the written doc_id makes the next load reach the same
    built-in by name instead of by expression text, and the origin line
    cannot make the block independent while a doc_id is present.
    """

    def test_a_fresh_catalog_over_the_migrated_file_answers_the_same(
        self, tmp_path,
    ):
        migrating = _catalog(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING,
        )
        fresh = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        for phrase in ("maximize", "restore", "my own thing"):
            assert _actions_of(migrating.get_matching_patterns(phrase)) == (
                _actions_of(fresh.get_matching_patterns(phrase))
            ), phrase

    def test_the_pattern_count_is_unchanged_by_the_write(self, tmp_path):
        migrating = _catalog(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING,
        )
        fresh = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert migrating.pattern_count == fresh.pattern_count

    def test_two_copies_of_one_expression_keep_their_order(self, tmp_path):
        """Both resolve to one built-in, and both are written.

        The merge gives the built-in's place to the LAST claimant in file
        order. After the migration both blocks name the built-in, so the
        same rule decides by name and the order is the same.
        """
        user = (
            "[[pattern]]\n"
            "pattern = '''^maximize$'''\n"
            'actions = [{ function = "text", params = ["first"] }]\n'
            "\n"
            "[[pattern]]\n"
            "pattern = '''^maximize$'''\n"
            'actions = [{ function = "text", params = ["second"] }]\n'
        )
        migrating = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, user)
        fresh = PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )
        assert _actions_of(migrating.get_matching_patterns("maximize")) == (
            _actions_of(fresh.get_matching_patterns("maximize"))
        )


class TestEveryOtherByteSurvives:
    def test_only_two_lines_are_added(self, tmp_path):
        before = HOTWORD + LEGACY_WITH_HAND_WRITING
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING)
        after = _user_text(tmp_path)
        added = [
            line for line in after.splitlines()
            if line not in before.splitlines()
        ]
        assert sorted(added) == [
            'doc_id = "window-maximize"', 'origin = "user"',
        ], after

    def test_the_hand_written_comment_stays_with_its_block(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING)
        assert "# the one I changed" in _user_text(tmp_path)
        assert "# my own notes about this file" in _user_text(tmp_path)

    def test_the_other_block_is_untouched(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING)
        second = _blocks(tmp_path)[1]
        assert second["pattern"] == "^my own thing$"
        assert "doc_id" not in second
        assert "origin" not in second

    def test_a_backup_of_the_original_is_kept(self, tmp_path):
        before = HOTWORD + LEGACY_WITH_HAND_WRITING
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_WITH_HAND_WRITING)
        backup = str(tmp_path / "user_patterns.toml") + BACKUP_SUFFIX
        assert os.path.exists(backup)
        with open(backup, encoding="utf-8") as fh:
            assert fh.read() == before

    def test_the_backup_is_not_the_editors_own(self, tmp_path):
        """The editor keeps a .bak of the last good save.

        A migration writing to that name would destroy the copy an edit
        made, so it uses a name of its own.
        """
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert not os.path.exists(
            str(tmp_path / "user_patterns.toml") + ".bak"
        )


class TestAMultiLineExpressionSurvives:
    """A hand-written expression can span lines, and the insert must not
    land inside it.

    This is why the two lines go immediately after the ``[[pattern]]``
    header and not after the block's first key line. A triple-quoted
    string has no fixed length, so the header is the one line in a block
    whose end is certain. An insert one line lower lands INSIDE the
    string: the file still parses, so nothing complains, and the person's
    expression silently gains two lines of TOML.
    """

    MULTI_LINE = (
        "[[pattern]]\n"
        'pattern = """\n'
        "^maximize$\n"
        '"""\n' + MINE
    )

    def test_the_block_gets_its_name(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, self.MULTI_LINE)
        assert _blocks(tmp_path)[0].get("doc_id") == "window-maximize"

    def test_the_expression_is_not_damaged(self, tmp_path):
        """TOML drops the newline that follows the opening ``\"\"\"``, so the
        value is the one line plus its terminator."""
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, self.MULTI_LINE)
        assert _blocks(tmp_path)[0]["pattern"] == "^maximize$\n"


# The decoy expression below contains "[[pattern]]", which the catalog
# compiles as a regex and Python reads as a possible nested set. The
# warning is about the fixture, not about anything under test, and it is
# scoped to this class so a real one elsewhere still shows.
@pytest.mark.filterwarnings("ignore::FutureWarning")
class TestAHeaderInsideAStringIsNotFollowed:
    """A header-looking line inside a string is not one of the headers.

    A hand-written expression may contain a line that reads exactly like a
    ``[[pattern]]`` header. The locator asks the parser rather than the
    text: the file before a real header is a complete document on its own,
    and the file before a line inside a multi-line string is not. So the
    decoy is passed over, the count stays level with the parsed list, and
    the block that needed the name is the block that gets it.

    Before wh-pattern-override-doc-id.3.7 this decoy was counted, the range
    that followed was a fragment of somebody's string, and the write was
    refused -- the file was left alone and the override never named. The
    tests here pin the halves of the corrected outcome: the decoy's own
    text is untouched, and the real block is named.
    """

    DECOY = (
        "[[pattern]]\n"
        'pattern = """\n'
        "[[pattern]]\n"
        "^decoy$\n"
        '"""\n'
        'actions = [{ function = "text", params = ["decoy"] }]\n'
        "\n"
    )

    def _user(self):
        return self.DECOY + LEGACY_OVERRIDE

    def test_the_file_is_left_exactly_as_it_was(self, tmp_path):
        """Apart from the two lines the one migrated block takes."""
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, self._user())
        named = (
            "[[pattern]]\n"
            'doc_id = "window-maximize"\n'
            'origin = "user"\n'
            + LEGACY_OVERRIDE.split("\n", 1)[1]
        )
        assert _user_text(tmp_path) == HOTWORD + self.DECOY + named

    def test_the_decoys_own_expression_is_untouched(self, tmp_path):
        """The person typed that string. The count may not run through it."""
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, self._user())
        assert _blocks(tmp_path)[0]["pattern"] == (
            "[[pattern]]\n^decoy$\n"
        )

    def test_the_person_still_gets_their_own_action(self, tmp_path):
        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, self._user())
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]


class TestWhatIsNeverWritten:
    def test_an_ambiguous_block_is_left_alone(self, tmp_path):
        """A4. Two built-ins carry the text, so the file does not say."""
        user = "[[pattern]]\n" "pattern = '''^twin$'''\n" + MINE
        _catalog(tmp_path, TWIN_A + TWIN_B, user)
        assert "doc_id" not in _blocks(tmp_path)[0]
        assert "origin" not in _blocks(tmp_path)[0]

    def test_a_block_matching_no_builtin_is_left_alone(self, tmp_path):
        user = "[[pattern]]\n" "pattern = '''^nothing shipped$'''\n" + MINE
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, user)
        assert "doc_id" not in _blocks(tmp_path)[0]

    def test_a_block_that_already_names_its_builtin_is_left_alone(
        self, tmp_path,
    ):
        user = (
            "[[pattern]]\n"
            'doc_id = "window-maximize"\n'
            "pattern = '''^maximize$'''\n" + MINE
        )
        before = HOTWORD + user
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, user)
        assert _user_text(tmp_path) == before

    def test_a_rule_the_person_created_is_left_alone(self, tmp_path):
        """.3.3. The key says this block never replaced a built-in."""
        user = (
            "[[pattern]]\n"
            'origin = "user"\n'
            "pattern = '''^maximize$'''\n" + MINE
        )
        before = HOTWORD + user
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, user)
        assert _user_text(tmp_path) == before

    def test_a_catalog_that_did_not_ask_writes_nothing(self, tmp_path):
        """One writer. Every other loader stays in memory."""
        before = HOTWORD + LEGACY_OVERRIDE
        catalog = _catalog(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE, migrate=False,
        )
        assert _user_text(tmp_path) == before
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]

    def test_the_default_is_not_to_write(self, tmp_path):
        """A caller that says nothing gets the memory-only behaviour."""
        system_file, user_file = _files(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE,
        )
        before = HOTWORD + LEGACY_OVERRIDE
        PatternCatalog(system_file, user_file)
        assert _user_text(tmp_path) == before


class TestASecondLoadFindsNothingToDo:
    def test_the_file_does_not_change_again(self, tmp_path):
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        after_first = _user_text(tmp_path)
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, after_first[len(HOTWORD):])
        assert _user_text(tmp_path) == after_first

    def test_the_first_backup_is_not_overwritten(self, tmp_path):
        """A LATER migration must not replace the original copy.

        The person hand-adds another override after the first migration,
        so a second load has something to write. The copy kept must still
        be the file as it was before anything was ever written to it --
        the only copy that can undo the whole migration.
        """
        before = HOTWORD + LEGACY_OVERRIDE
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        migrated = _user_text(tmp_path)
        hand_added = (
            "\n[[pattern]]\n"
            "pattern = '''^restore$'''\n"
            'actions = [{ function = "text", params = ["mine too"] }]\n'
        )
        _write(tmp_path / "user_patterns.toml", migrated + hand_added)
        PatternCatalog(
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
            migrate_legacy_ids=True,
        )
        # The second block really was migrated, so the guard was under load.
        assert _blocks(tmp_path)[1].get("doc_id") == "window-restore"
        backup = str(tmp_path / "user_patterns.toml") + BACKUP_SUFFIX
        with open(backup, encoding="utf-8") as fh:
            assert fh.read() == before


class TestAFailedWriteDoesNotBreakTheLoad:
    def test_the_load_still_gives_the_person_their_rule(
        self, tmp_path, monkeypatch,
    ):
        """The association still holds in memory; only durability waits."""
        import speech.pattern_catalog as module

        def refuse(*args, **kwargs):
            raise OSError("read-only medium")

        monkeypatch.setattr(module.os, "replace", refuse)
        catalog = _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]

    def test_the_failure_is_reported_as_a_warning(
        self, tmp_path, monkeypatch, caplog,
    ):
        import speech.pattern_catalog as module

        def refuse(*args, **kwargs):
            raise OSError("read-only medium")

        monkeypatch.setattr(module.os, "replace", refuse)
        with caplog.at_level(logging.WARNING):
            _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        assert any(
            record.levelno == logging.WARNING
            and "read-only medium" in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_permission_error_is_survived(
        self, tmp_path, monkeypatch, caplog,
    ):
        """PermissionError is the failure the acceptance field names.

        On Windows it is also the ordinary one: another process holding
        the file makes os.replace fail with WinError 5. The load must
        finish, the file must be left as it was, and the next load tries
        again.
        """
        import speech.pattern_catalog as module

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Access is denied")

        _files(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        before = _user_text(tmp_path)
        monkeypatch.setattr(module.os, "replace", refuse)
        with caplog.at_level(logging.WARNING):
            catalog = _catalog(
                tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE,
            )
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]
        assert _user_text(tmp_path) == before
        assert any(
            record.levelno == logging.WARNING
            and "Access is denied" in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_read_only_file_is_survived(self, tmp_path, caplog):
        """The same path with no monkeypatch: the file itself refuses.

        ``os.chmod(path, 0o444)`` sets the Windows read-only attribute,
        and a replace onto a read-only destination is refused there. The
        mode is restored in a finally block so pytest can remove the
        temporary directory afterwards.
        """
        system_file, user_file = _files(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE,
        )
        before = _user_text(tmp_path)
        os.chmod(user_file, 0o444)
        try:
            with caplog.at_level(logging.WARNING):
                catalog = PatternCatalog(
                    system_file, user_file, migrate_legacy_ids=True,
                )
            assert _actions_of(catalog.get_matching_patterns("maximize")) == [
                [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
            ]
            assert _user_text(tmp_path) == before
            assert any(
                record.levelno == logging.WARNING for record in caplog.records
            ), [r.getMessage() for r in caplog.records]
        finally:
            os.chmod(user_file, 0o644)

    def test_a_refused_correspondence_check_leaves_the_file_byte_identical(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Refusing by default means no write at all, not a partial one.

        The whole-file check has no reachable failing input while the
        walk counts correctly, which is recorded on
        wh-pattern-override-doc-id.3.7, so the refusal is forced here.
        What this pins is the caller's answer to a refusal -- write
        nothing, warn, carry on -- and not the check's own judgement.
        TestNothingElseInTheFileMayChange tests that judgement directly.
        """
        import speech.pattern_catalog as module

        monkeypatch.setattr(
            module.PatternCatalog, "_only_the_named_blocks_took_the_keys",
            staticmethod(lambda *args, **kwargs: False),
        )
        _files(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        user_file = tmp_path / "user_patterns.toml"
        before = user_file.read_bytes()
        with caplog.at_level(logging.WARNING):
            catalog = _catalog(
                tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE,
            )
        # Bytes, not text: a changed line ending must fail this too.
        assert user_file.read_bytes() == before
        assert _actions_of(catalog.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]
        assert any(
            record.levelno == logging.WARNING
            and "more than the two names" in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_no_temp_file_is_left_behind(self, tmp_path, monkeypatch):
        import speech.pattern_catalog as module

        def refuse(*args, **kwargs):
            raise OSError("read-only medium")

        monkeypatch.setattr(module.os, "replace", refuse)
        _catalog(tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE)
        leftovers = [
            name for name in os.listdir(tmp_path)
            if name.startswith("user_patterns.toml")
            and not name.endswith((".toml", BACKUP_SUFFIX))
        ]
        assert leftovers == []


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_files_own_line_endings_are_kept(tmp_path, newline):
    """A hand-written file may use either convention.

    Rewriting a CRLF file with LF endings would show every line as changed
    in the person's own version control or editor.
    """
    user = (HOTWORD + LEGACY_OVERRIDE).replace("\n", newline)
    system_file = _write(
        tmp_path / "patterns.toml", HOTWORD + BUILTIN + OTHER_BUILTIN,
    )
    user_file = tmp_path / "user_patterns.toml"
    user_file.write_bytes(user.encode("utf-8"))
    PatternCatalog(system_file, str(user_file), migrate_legacy_ids=True)
    raw = user_file.read_bytes()
    assert b"\r\n" in raw if newline == "\r\n" else b"\r\n" not in raw


class TestTheLogicProcessIsTheOneThatAsks:
    """Constraint 5: exactly one writer, and it is the app's own load.

    The flag is passed at one call site. A test that only checked the
    parameter exists would pass while no shipped code ever set it, and the
    person who never opens the Pattern Manager window -- the whole
    population this bead is about -- would never be migrated.
    """

    def _handler(self, tmp_path, monkeypatch):
        import speech.speech_handler as module

        system_file, user_file = _files(
            tmp_path, BUILTIN + OTHER_BUILTIN, LEGACY_OVERRIDE,
        )
        recorded = {}

        class RecordingCatalog:
            def __init__(self, patterns_file, user_patterns_file=None, **kw):
                recorded["args"] = (patterns_file, user_patterns_file)
                recorded["kwargs"] = kw

        monkeypatch.setattr(module, "PatternCatalog", RecordingCatalog)
        monkeypatch.setattr(
            module, "TextParser", lambda *args, **kwargs: object(),
        )

        class Config:
            def get_config(self):
                return {
                    "STT_PATTERNS_FILE": system_file,
                    "STT_USER_PATTERNS_FILE": user_file,
                }

        module.SpeechHandler(object(), object(), Config())
        return recorded

    def test_the_app_load_asks_for_the_migration(self, tmp_path, monkeypatch):
        recorded = self._handler(tmp_path, monkeypatch)
        assert recorded["kwargs"].get("migrate_legacy_ids") is True

    def test_it_asks_over_the_two_files_the_app_uses(
        self, tmp_path, monkeypatch,
    ):
        recorded = self._handler(tmp_path, monkeypatch)
        assert recorded["args"] == (
            str(tmp_path / "patterns.toml"),
            str(tmp_path / "user_patterns.toml"),
        )


# A rule that TYPES an example of this very file. TOML lets a multi-line
# string hold anything, including lines that look exactly like a
# ``[[pattern]]`` header, and the raw-text walk counts header-looking lines
# (wh-pattern-override-doc-id.3.7). Two of them, because the located range
# ends at the next header-looking line: with one, the range runs past the
# closing delimiter and no longer parses, so the write is refused for the
# wrong reason. Two is the shape that produced a parseable fragment.
TYPES_TWO_HEADERS = (
    "[[pattern]]\n"
    "pattern = '''^pattern example$'''\n"
    'actions = [{ function = "text", params = ["""\n'
    "[[pattern]]\n"
    "pattern = '^maximize$'\n"
    "actions = []\n"
    "\n"
    "[[pattern]]\n"
    "pattern = '^example$'\n"
    "actions = []\n"
    '"""] }]\n'
)


class TestAHeaderInsideAStringDoesNotMisplaceTheWrite:
    """wh-pattern-override-doc-id.3.7, found by the closing review.

    The header count is what names the block, and a header-looking line
    inside a person's text action is counted with the real ones. Every
    block after such a string is then off by one. The block the write
    landed in parsed on its own, held one table, carried the resolved
    expression and no doc_id, so the check meant to catch this accepted
    it: read in isolation, a fragment cut out of a string literal is a
    valid block.
    """

    def test_the_typed_example_is_not_edited(self, tmp_path):
        """The person typed that text. Nothing may rewrite it."""
        _catalog(tmp_path, BUILTIN, TYPES_TWO_HEADERS + LEGACY_OVERRIDE)
        typed = _blocks(tmp_path)[0]["actions"][0]["params"][0]
        assert "doc_id" not in typed, (
            "the migration wrote its two lines into the text this rule "
            "types, so saying the phrase now types a doc_id line"
        )
        assert "origin" not in typed

    def test_the_real_override_is_the_one_named(self, tmp_path):
        """And the block that needed the name still gets it."""
        _catalog(tmp_path, BUILTIN, TYPES_TWO_HEADERS + LEGACY_OVERRIDE)
        blocks = _blocks(tmp_path)
        assert blocks[1].get("doc_id") == "window-maximize", (
            "the block that owns the expression was passed over, so the "
            "name the migration exists to write never reached it"
        )
        assert blocks[1].get("origin") == "user"

    def test_the_command_still_answers_after_a_rewrite(self, tmp_path):
        """The point of the name, end to end."""
        _catalog(tmp_path, BUILTIN, TYPES_TWO_HEADERS + LEGACY_OVERRIDE)
        migrated = _user_text(tmp_path).split(HOTWORD, 1)[-1]
        again = _catalog(tmp_path, REWRITTEN_BUILTIN, migrated)
        assert _actions_of(again.get_matching_patterns("maximize")) == [
            [{"function": "hk", "params": ["ctrl", "alt", "m"]}],
        ]


class TestTheRetainedEntriesAgreeWithTheFile:
    """wh-pattern-override-doc-id.3.8, found by the closing review.

    ``_build_all`` builds, then writes, then returns the entries it read
    BEFORE the write. Everything reading that snapshot -- the try-it
    preview above all -- then believes the block still has no name while
    the file on disk has one. The preview disagreed with the save that
    followed it, which is the .3.5 defect returning for migrated rows.
    """

    def test_a_written_name_reaches_the_retained_entries(self, tmp_path):
        catalog = _catalog(tmp_path, BUILTIN, LEGACY_OVERRIDE)
        entry = catalog.get_raw_user_entries()[0]
        assert entry.get("doc_id") == "window-maximize", (
            "the file now names the built-in and the catalog's own copy "
            "of that entry does not"
        )
        assert entry.get("origin") == "user"

    def test_a_refused_write_leaves_the_entries_alone(self, tmp_path):
        """No write, no claim. The snapshot must not invent an identity."""
        system_file, user_file = _files(tmp_path, BUILTIN, LEGACY_OVERRIDE)
        os.chmod(user_file, 0o444)
        try:
            catalog = PatternCatalog(
                system_file, user_file, migrate_legacy_ids=True,
            )
            entry = catalog.get_raw_user_entries()[0]
            assert entry.get("doc_id") is None
            assert entry.get("origin") is None
        finally:
            os.chmod(user_file, 0o666)

    def test_an_ambiguous_entry_is_not_named_in_the_entries_either(
        self, tmp_path,
    ):
        catalog = _catalog(
            tmp_path,
            TWIN_A + TWIN_B,
            "[[pattern]]\n" "pattern = '''^twin$'''\n" + MINE,
        )
        assert catalog.get_raw_user_entries()[0].get("doc_id") is None


# The block that must be migrated is itself the one holding a
# header-looking line, this time inside the text its action types. The
# END of a block is decided by the same walk as its start, so a block
# ending at the first line that merely LOOKS like a header is cut in the
# middle of that string (wh-pattern-override-doc-id.3.7).
LEGACY_WITH_A_HEADER_IN_ITS_ACTION = (
    "[[pattern]]\n"
    "pattern = '''^maximize$'''\n"
    'actions = [{ function = "text", params = ["""\n'
    "[[pattern]]\n"
    "an example of this file\n"
    '"""] }]\n'
)


class TestTheBlockEndsWhereTheParserSaysItDoes:
    def test_the_block_is_still_named(self, tmp_path):
        _catalog(
            tmp_path, BUILTIN, LEGACY_WITH_A_HEADER_IN_ITS_ACTION,
        )
        block = _blocks(tmp_path)[0]
        assert block.get("doc_id") == "window-maximize", (
            "the block was cut at the header-looking line inside its own "
            "action, so what was located did not parse and the write that "
            "names the built-in was refused"
        )
        assert block.get("origin") == "user"

    def test_the_typed_text_is_untouched(self, tmp_path):
        _catalog(
            tmp_path, BUILTIN, LEGACY_WITH_A_HEADER_IN_ITS_ACTION,
        )
        typed = _blocks(tmp_path)[0]["actions"][0]["params"][0]
        assert typed == "[[pattern]]\nan example of this file\n"


class TestTheLocatedBlockIsTheOneTheMergeResolved:
    """``_block_takes_the_id``, the per-block half of the two checks.

    Reached through the class rather than through a load, because with
    the walk counting correctly there is no user file that makes it
    refuse a block the walk points at. It is kept as the check that a
    later change to the walk cannot quietly write into the wrong place,
    and these tests are what say what it promises.
    """

    def _takes(self, block_text, expression="^maximize$"):
        return PatternCatalog._block_takes_the_id(block_text, expression)

    def test_a_block_carrying_the_expression_is_taken(self):
        assert self._takes(
            "[[pattern]]\npattern = '''^maximize$'''\nactions = []\n"
        )

    def test_a_block_carrying_another_expression_is_not(self):
        assert not self._takes(
            "[[pattern]]\npattern = '''^restore$'''\nactions = []\n"
        )

    def test_a_block_that_does_not_parse_is_not(self):
        assert not self._takes(
            "[[pattern]]\npattern = '''^maximize$\nactions = []\n"
        )

    def test_two_tables_are_not_one_block(self):
        assert not self._takes(
            "[[pattern]]\npattern = '''^maximize$'''\nactions = []\n"
            "[[pattern]]\npattern = '''^restore$'''\nactions = []\n"
        )

    def test_a_block_already_naming_a_builtin_is_not_taken(self):
        assert not self._takes(
            "[[pattern]]\ndoc_id = \"window-restore\"\n"
            "pattern = '''^maximize$'''\nactions = []\n"
        )


class TestNothingElseInTheFileMayChange:
    """``_only_the_named_blocks_took_the_keys``, the whole-file check.

    The last thing asked before the file is installed, and the only one
    that reads the whole document. Like the check above it has no user
    file that reaches it while the walk counts correctly; it is what
    makes a later mistake in the walk harmless instead of silent.
    """

    ORIGINAL = (
        'COMMAND_HOTWORD = "x-ray"\n\n'
        "[[pattern]]\npattern = '''^maximize$'''\nactions = []\n\n"
        "[[pattern]]\npattern = '''^restore$'''\nactions = []\n"
    )
    NAMED = (
        'COMMAND_HOTWORD = "x-ray"\n\n'
        "[[pattern]]\ndoc_id = \"window-maximize\"\norigin = \"user\"\n"
        "pattern = '''^maximize$'''\nactions = []\n\n"
        "[[pattern]]\npattern = '''^restore$'''\nactions = []\n"
    )

    def _agrees(self, new_text, positions=None):
        return PatternCatalog._only_the_named_blocks_took_the_keys(
            self.ORIGINAL, new_text,
            {0} if positions is None else positions,
        )

    def test_the_two_names_on_the_named_block_agree(self):
        assert self._agrees(self.NAMED)

    def test_a_block_that_was_not_named_may_not_change(self):
        assert not self._agrees(
            self.NAMED.replace("'''^restore$'''", "'''^restored$'''")
        )

    def test_a_named_block_may_change_nothing_else(self):
        assert not self._agrees(
            self.NAMED.replace("'''^maximize$'''", "'''^maximise$'''")
        )

    def test_a_block_may_not_appear(self):
        assert not self._agrees(
            self.NAMED + "\n[[pattern]]\npattern = '''^x$'''\nactions = []\n"
        )

    def test_the_hotword_may_not_change(self):
        assert not self._agrees(self.NAMED.replace("x-ray", "yankee"))

    def test_a_block_nobody_named_may_not_take_the_keys(self):
        assert not self._agrees(self.NAMED, positions=set())

    def test_the_name_written_must_be_a_doc_id(self):
        assert not self._agrees(
            self.NAMED.replace('doc_id = "window-maximize"', 'doc_id = ""')
        )

    def test_the_other_key_must_say_the_person_owns_it(self):
        assert not self._agrees(
            self.NAMED.replace('origin = "user"', 'origin = "system"')
        )
