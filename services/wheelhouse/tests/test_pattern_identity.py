# tests/test_pattern_identity.py
"""The one identity rule every pattern merge site shares (A6).

A saved override used to find its built-in by normalized pattern text, so a
release that rewrote the built-in's regex orphaned the override: the two no
longer shared a key, the user entry was appended after the built-ins, and the
built-in won again. The durable key is ``doc_id``. These tests pin the rule
itself; the merge sites that consume it are tested beside their own code.
"""

import pytest

from speech.pattern_identity import (
    DOC_ID_KEY,
    entry_identity,
    is_valid_doc_id,
)


class TestDocIdShape:
    """A doc_id is a slug: lowercase letters and digits, single hyphens."""

    @pytest.mark.parametrize(
        "value",
        ["window-maximize", "g1", "a", "open-a-new-tab", "x2go-window"],
        ids=["two-words", "letter-digit", "single-letter", "four-words", "digit-inside"],
    )
    def test_a_well_formed_slug_is_accepted(self, value):
        assert is_valid_doc_id(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "Window-Maximize",
            "window_maximize",
            "-window",
            "window-",
            "window--maximize",
            "window maximize",
            "",
        ],
        ids=[
            "uppercase",
            "underscore",
            "leading-hyphen",
            "trailing-hyphen",
            "doubled-hyphen",
            "space",
            "empty",
        ],
    )
    def test_a_malformed_slug_is_rejected(self, value):
        assert is_valid_doc_id(value) is False

    @pytest.mark.parametrize(
        "value", [None, 5, ["window-maximize"], {"doc_id": "x"}],
        ids=["none", "int", "list", "dict"],
    )
    def test_a_non_string_is_rejected(self, value):
        """A hand-edited `doc_id = 5` must not reach the regex as an int."""
        assert is_valid_doc_id(value) is False

    def test_a_slug_that_only_starts_well_is_rejected(self):
        """The match must cover the whole value, not just its start.

        `re.match` would accept "window-maximize!" because the slug matches
        its beginning. That would let a hand-edited id carrying punctuation
        act as a merge key.
        """
        assert is_valid_doc_id("window-maximize!") is False


class TestEntryIdentity:
    """What an entry merges on, and what keeps the two kinds apart."""

    def test_an_entry_with_a_doc_id_identifies_by_it(self):
        identity = entry_identity({DOC_ID_KEY: "window-maximize", "pattern": "^max$"})
        assert identity == ("doc", "window-maximize")

    def test_the_doc_id_survives_a_rewrite_of_the_pattern_text(self):
        """The whole point of the bead: same identity, different regex."""
        before = entry_identity({DOC_ID_KEY: "window-maximize", "pattern": "^max$"})
        after = entry_identity(
            {DOC_ID_KEY: "window-maximize", "pattern": r"^maximi[sz]e\s+window$"}
        )
        assert before == after

    def test_an_entry_without_a_doc_id_identifies_by_normalized_text(self):
        identity = entry_identity({"pattern": "  ^Max$  "})
        assert identity == ("text", "^max$")

    def test_an_entry_with_a_malformed_doc_id_falls_back_to_text(self):
        """A malformed id must not become a key of its own.

        Falling back keeps a hand-edited `doc_id = "Window Maximize"` merging
        exactly as it did before the id existed, instead of silently becoming
        an unmatched entry that loses to its built-in.
        """
        identity = entry_identity({DOC_ID_KEY: "Window Maximize", "pattern": "^max$"})
        assert identity == ("text", "^max$")

    def test_an_entry_with_neither_has_no_identity(self):
        assert entry_identity({"actions": []}) is None

    def test_an_entry_whose_pattern_is_not_a_string_has_no_identity(self):
        """A hand-edited `pattern = 5` must not crash the merge."""
        assert entry_identity({"pattern": 5}) is None

    def test_a_doc_id_never_collides_with_a_pattern_of_the_same_text(self):
        """The two kinds are separate namespaces.

        Without the kind in the identity, a user entry whose regex happened to
        read "window-maximize" would replace the built-in carrying that
        doc_id -- a silent capture of an unrelated command.
        """
        by_id = entry_identity(
            {DOC_ID_KEY: "window-maximize", "pattern": "^maximize$"}
        )
        by_text = entry_identity({"pattern": "window-maximize"})
        assert by_id is not None and by_text is not None
        assert by_id != by_text

    def test_a_non_dict_entry_has_no_identity(self):
        """`pattern = [1, 2, 3]` parses as a list, not a table.

        The catalog already drops such an entry with a warning; the identity
        rule must not raise before it gets the chance.
        """
        assert entry_identity([1, 2, 3]) is None

    def test_a_valid_doc_id_cannot_rescue_a_pattern_that_is_not_a_string(self):
        """The entry cannot build, so it must not claim a built-in's slot.

        The doc branch used to answer before anything looked at the pattern,
        so `doc_id = "window-maximize"` with `pattern = 5` took the built-in's
        place in the merge and was then dropped at build time. The built-in
        was gone and nothing answered the phrase
        (wh-pattern-override-doc-id.2.3).
        """
        assert entry_identity({DOC_ID_KEY: "window-maximize", "pattern": 5}) is None

    def test_a_valid_doc_id_cannot_rescue_a_missing_pattern(self):
        """Same rule for the entry whose pattern line was deleted."""
        assert entry_identity({DOC_ID_KEY: "window-maximize"}) is None
