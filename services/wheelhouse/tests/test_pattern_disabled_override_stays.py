"""A built-in the user switched off stays off across a release.

wh-pattern-override-doc-id A5. A user turns a built-in off by saving an
override with no actions: the merge puts that entry in the built-in's slot and
the catalog then drops it, because an entry with no actions builds nothing. The
command stops responding, which is what the user asked for.

Keyed on pattern text, a release that rewrote the built-in's expression undid
that silently. The empty override no longer matched, so it was appended instead
of replacing, the built-in stayed in the merged list with its actions intact,
and a command the user had deliberately switched off started firing again --
with no message, no badge change, and nothing in the manager to explain it. A
command that fires when the user has told it not to is worse than one that does
not fire: it is an action taken against their wish, and for a hotkey or a run
step, an action with consequences.

Keyed on doc_id the override still lands in the built-in's slot, so the switch
stays where the user put it. Only removing the override restores the built-in,
which is what the Remove Customization button does.
"""

from speech.pattern_catalog import PatternCatalog


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
SWITCHED_OFF = (
    "[[pattern]]\n"
    'doc_id = "window-maximize"\n'
    "pattern = '''^maximize$'''\n"
    "actions = []\n"
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def _catalog(tmp_path, system_text, user_text):
    return PatternCatalog(
        _write(tmp_path / "patterns.toml", HOTWORD + system_text),
        _write(tmp_path / "user_patterns.toml", HOTWORD + user_text),
    )


class TestASwitchedOffBuiltinStaysOff:
    def test_the_builtin_does_not_respond_before_the_rewrite(self, tmp_path):
        """The baseline, so the test below cannot pass vacuously."""
        catalog = _catalog(tmp_path, BUILTIN_BEFORE, SWITCHED_OFF)
        assert catalog.get_matching_patterns("maximize") == []

    def test_the_builtin_does_not_come_back_after_the_rewrite(self, tmp_path):
        """A5. The release must not undo the user's decision."""
        catalog = _catalog(tmp_path, BUILTIN_AFTER, SWITCHED_OFF)
        assert catalog.get_matching_patterns("maximize") == [], (
            "a built-in the user switched off fired again after a release "
            "rewrote its expression"
        )

    def test_the_rewritten_expression_does_not_respond_either(self, tmp_path):
        """The rewrite widened the built-in; the switch covers the new form.

        The rewritten rule also accepts a trailing word the original never
        did, so "maximize window" reaches it and nothing else. The override
        sits in that entry's slot, so the whole rule is off -- not merely
        the words the user happened to type.
        """
        catalog = _catalog(tmp_path, BUILTIN_AFTER, SWITCHED_OFF)
        assert catalog.get_matching_patterns("maximize window") == []

    def test_the_switched_off_entry_holds_the_slot_and_builds_nothing(
        self, tmp_path,
    ):
        """One rule off, the rest of the file untouched.

        The count is what separates "the override replaced the built-in and
        then built nothing" from "the merge dropped both", which would take
        an unrelated command down with it.
        """
        second = (
            "[[pattern]]\n"
            'doc_id = "window-restore"\n'
            "pattern = '''^restore$'''\n"
            'actions = [{ function = "hk", params = ["win", "down"] }]\n'
        )
        catalog = _catalog(tmp_path, BUILTIN_AFTER + second, SWITCHED_OFF)
        assert catalog.pattern_count == 1
        assert len(catalog.get_matching_patterns("restore")) == 1


class TestRemovingTheOverrideIsWhatRestoresIt:
    def test_the_builtin_returns_once_the_user_file_is_empty(self, tmp_path):
        """The explicit removal path, which Remove Customization performs.

        Without this, "stays off" could be read as "can never be switched
        back on", and the guard above would pass on a catalog that had
        simply lost the pattern.
        """
        catalog = _catalog(tmp_path, BUILTIN_AFTER, "")
        assert len(catalog.get_matching_patterns("maximize")) == 1


class TestAnActionlessLegacyOverrideStillWorks:
    def test_an_entry_without_a_doc_id_switches_its_builtin_off_by_text(
        self, tmp_path,
    ):
        """Every switch saved before doc_ids existed keeps working.

        It has only the pattern text to associate on, so the text key has to
        stay live -- and a user who switched a command off years ago must not
        find it firing after an upgrade.
        """
        builtin = (
            "[[pattern]]\n"
            "pattern = '''^save$'''\n"
            'actions = [{ function = "hk", params = ["ctrl", "s"] }]\n'
        )
        legacy_off = "[[pattern]]\npattern = '''^save$'''\nactions = []\n"
        catalog = _catalog(tmp_path, builtin, legacy_off)
        assert catalog.get_matching_patterns("save") == []
