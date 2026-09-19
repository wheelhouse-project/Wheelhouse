"""Criterion 5 of wh-voice-access-parity.1.6: alias completeness, item by item.

The bead's SCOPE lists about 34 spoken forms that Voice Access users already
know and that Wheelhouse was to accept as aliases for commands it already had.
Its DONE WHEN adds two conditions beyond "it works": no existing trigger
breaks, and first-match-wins ordering is checked FOR EACH new alias.

This file is the committed form of that proof. A bd comment listing the forms
would be true on the day it was written and unchecked forever after; a test
re-runs.

HOW A FORM IS TIED TO ITS ROW. The runtime pattern data the matcher carries
does NOT include doc_id -- ``pattern_data`` holds actions,
``literal_body_matchers``, ``requires_hotword`` and ``whole_utterance_only``,
and nothing else. doc_id is documentation metadata that only the helpdoc and
action-catalog tooling reads. So a row cannot be named from a MatchResult.
Each form is tied to its row from the data instead: the named row's own regex
must fullmatch the form, that row must come FIRST among every row that
fullmatches it, and the live matcher must then fire on the form and consume
all of it. The three together say which row owns the form and that the row is
really reachable.

WHY ORDER MATTERS FOR SIX OF THEM. Six forms are fullmatched by two rows: the
four "select ..." forms are also fullmatched by ``^select (.+)$``
(select-phrase), and "show numbers" / "show grid" are also fullmatched by
``^show\\s+(.+)$`` (show-app). In every one of those the broad row requires
the hotword and the narrow row does not, and the narrow row is earlier in the
file, so the narrow row wins with or without the hotword. Measured both ways
before this test was written. The ordering assertion is what keeps it that
way.

THE EXCLUSIONS ARE LOAD BEARING (bead SCOPE, and criterion 8). In Voice
Access "delete that" and "scratch that" act on the LAST DICTATED WORDS, not on
the selection. Shipping them as selection aliases would teach a behaviour and
then take it away, so they belong entirely to the last-dictated-words bead.
"select that" is the third exclusion and is a different case: it is not
absent, it reaches ``^select (.+)$`` with the hotword, which is the
spoken-phrase command, not a selection alias.
"""
import re
import sys
import tomllib
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_matcher import PatternMatcher

_PATTERNS_PATH = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"


# (spoken form, doc_id of the row that must own it, hotword needed to fire)
_SCOPED_ALIASES = [
    ("click cancel", "click-element", True),
    ("tap cancel", "click-element", True),
    ("copy that", "copy", False),
    ("cut that", "cut", False),
    ("paste that", "paste", False),
    ("paste here", "paste", False),
    ("undo that", "undo", False),
    ("redo that", "redo", False),
    ("select word", "select-word", False),
    ("select this word", "select-word", False),
    ("select line", "select-line", False),
    ("select this line", "select-line", False),
    ("select paragraph", "select-paragraph", False),
    ("select this paragraph", "select-paragraph", False),
    ("bold text", "bold", False),
    ("bold that", "bold", False),
    ("italics", "italics", False),
    ("italicize that", "italics", False),
    ("underline", "underline", False),
    ("underline that", "underline", False),
    ("capitalize that", "capitalize", False),
    ("cap that", "capitalize-that", False),
    ("uppercase that", "uppercase", False),
    ("all caps that", "uppercase-all-caps-that", False),
    ("lowercase that", "lowercase", False),
    ("no caps that", "lowercase-no-caps-that", False),
    ("apply numbers", "show-numbers", False),
    ("show numbers", "show-numbers", False),
    ("dismiss numbers", "hide-numbers", False),
    ("hide numbers", "hide-numbers", False),
    ("apply grid", "show-grid", False),
    ("show grid", "show-grid", False),
    ("dismiss grid", "hide-grid", False),
    ("hide grid", "hide-grid", False),
    ("open voice access help", "help-online", False),
    ("delete all", "delete-all", False),
    ("escape", "escape", False),
    ("dismiss", "escape", False),
]

# Forms the bead keeps out on purpose. Nothing at all may fire on these.
_EXCLUDED_ENTIRELY = ["delete that", "scratch that"]


@pytest.fixture(scope="module")
def rows():
    """Every shipped pattern row, in file order, straight from the TOML."""
    return tomllib.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))["pattern"]


@pytest.fixture(scope="module")
def matcher():
    # user_patterns_file="" keeps the developer's personal
    # data/user_patterns.toml out of the catalog, matching the guard in
    # tests/conftest.py and the fixture in test_router_command_prefix_word_loss.
    return PatternMatcher(
        PatternCatalog(str(_PATTERNS_PATH), user_patterns_file="")
    )


def _rows_that_fullmatch(rows, form):
    """(index, row) for every anchored shipped row whose regex owns `form`."""
    found = []
    for index, row in enumerate(rows):
        source = row["pattern"]
        if not source.startswith("^"):
            continue
        if re.compile(source, re.IGNORECASE).fullmatch(form):
            found.append((index, row))
    return found


class TestEveryScopedAliasReachesItsRow:
    @pytest.mark.parametrize("form,doc_id,needs_hotword", _SCOPED_ALIASES)
    def test_the_named_row_owns_the_form(self, rows, form, doc_id, needs_hotword):
        owners = _rows_that_fullmatch(rows, form)
        assert owners, (
            f"no shipped pattern row fullmatches {form!r}; the bead's SCOPE "
            f"names it as an alias for doc_id {doc_id!r}"
        )
        first_index, first_row = owners[0]
        assert first_row.get("doc_id") == doc_id, (
            f"{form!r} is claimed first by doc_id "
            f"{first_row.get('doc_id')!r} (row {first_index}, "
            f"{first_row['pattern']!r}), not by {doc_id!r}; first-match-wins "
            f"ordering sends it to the wrong command"
        )
        assert bool(first_row.get("requires_hotword", False)) is needs_hotword, (
            f"{form!r} reaches doc_id {doc_id!r} with requires_hotword="
            f"{first_row.get('requires_hotword', False)!r}, expected "
            f"{needs_hotword!r}"
        )

    @pytest.mark.parametrize("form,doc_id,needs_hotword", _SCOPED_ALIASES)
    def test_the_live_matcher_fires_and_consumes_the_whole_form(
        self, matcher, form, doc_id, needs_hotword
    ):
        result = matcher.match_complete(form, hotword_active=needs_hotword)
        assert result is not None and result.matched, (
            f"the shipped matcher does not fire on {form!r} with "
            f"hotword_active={needs_hotword}; the bead's SCOPE names it as an "
            f"alias for doc_id {doc_id!r}"
        )
        assert result.matched_text == form, (
            f"{form!r} matched only {result.matched_text!r}; the rest would be "
            f"typed as dictation"
        )
        assert result.remainder == "" and result.before_remainder == "", (
            f"{form!r} left text outside the match: before="
            f"{result.before_remainder!r} after={result.remainder!r}"
        )

    @pytest.mark.parametrize(
        "form,doc_id",
        [(f, d) for f, d, needs in _SCOPED_ALIASES if not needs],
    )
    def test_a_hotword_free_alias_does_not_need_the_hotword(
        self, matcher, form, doc_id
    ):
        # The bead's DECISION NEEDED asked whether to drop requires_hotword
        # where Voice Access asks for none. The recommendation was to KEEP the
        # hotword wherever Wheelhouse already set it, so this asserts the
        # status quo per form rather than a blanket rule.
        result = matcher.match_complete(form, hotword_active=False)
        assert result is not None and result.matched, (
            f"{form!r} is listed as needing no hotword to reach doc_id "
            f"{doc_id!r}, but the matcher refuses it without one"
        )


class TestTheExcludedFormsStayOut:
    @pytest.mark.parametrize("form", _EXCLUDED_ENTIRELY)
    def test_nothing_fires_on_it(self, matcher, form):
        for hotword_active in (False, True):
            result = matcher.match_complete(form, hotword_active=hotword_active)
            assert not (result and result.matched), (
                f"{form!r} fires a command (hotword_active={hotword_active}); "
                f"the bead excludes it because in Voice Access it acts on the "
                f"last dictated words, not on the selection"
            )

    def test_select_that_is_the_spoken_phrase_command_not_a_selection_alias(
        self, rows, matcher
    ):
        owners = _rows_that_fullmatch(rows, "select that")
        assert [row.get("doc_id") for _, row in owners] == ["select-phrase"], (
            "'select that' is owned by rows "
            f"{[row.get('doc_id') for _, row in owners]}; it must reach only "
            "select-phrase, never a selection alias"
        )
        assert not matcher.match_complete("select that", hotword_active=False), (
            "'select that' fires without the hotword; select-phrase is "
            "hotword-gated and nothing else may claim the words"
        )


class TestTheTableStillCoversTheScope:
    def test_the_table_is_not_empty_and_names_every_scope_group(self):
        # Without this an edit that empties or guts the table would make every
        # sweep above pass by judging nothing.
        assert len(_SCOPED_ALIASES) >= 38
        forms = {form for form, _, _ in _SCOPED_ALIASES}
        for expected in (
            "tap cancel", "copy that", "paste here", "undo that",
            "select this word", "bold that", "all caps that",
            "show numbers", "hide grid", "open voice access help",
            "delete all", "dismiss",
        ):
            assert expected in forms, f"the scope form {expected!r} is missing"

    def test_only_the_click_and_tap_forms_need_the_hotword(self):
        # Criterion 8 forbids ADDING requires_hotword to any entry. This pins
        # the split as measured, so a change either way is visible.
        needing = [form for form, _, needs in _SCOPED_ALIASES if needs]
        assert needing == ["click cancel", "tap cancel"], (
            f"the hotword-gated aliases are now {needing}; only the click and "
            f"tap target-selection forms were gated (wh-voice-access-parity."
            f"1.6.1.1, David 2026-08-17)"
        )


# ---------------------------------------------------------------------------
# "dismiss" was the one SCOPE item still unshipped when this file was written
# ---------------------------------------------------------------------------

class TestDismissIsAnAliasForEscapeInBothDirections:
    """The alias is only safe because the row keeps whole_utterance_only.

    "dismiss" is an ordinary English word, so the alias is worth nothing if
    the word can only ever press Escape, and is a word-loss bug if it presses
    Escape and then types the rest of the sentence. Both directions are
    asserted per phrase, the standard criterion 1 set for the seven
    whole-utterance entries.
    """

    @pytest.fixture(scope="class")
    def router(self):
        from speech.router import SpeechRouter

        return SpeechRouter(
            PatternCatalog(str(_PATTERNS_PATH), user_patterns_file=""),
            hotword="x-ray",
        )

    @pytest.mark.parametrize(
        "phrase",
        [
            "dismiss the meeting invite",
            "dismiss that thought for now",
            "dismiss my earlier objection",
        ],
    )
    def test_the_word_inside_a_longer_sentence_dictates(self, router, phrase):
        from speech.domain import Action

        decision = router._resolve_finalization(phrase.split(), hotword_active=False)
        assert decision.action is Action.DICTATE, (
            f"{phrase!r} resolved to {decision.action}; the leading "
            f'"dismiss" pressed Escape and the rest of the sentence was lost'
        )
        assert decision.payload == phrase

    @pytest.mark.parametrize("phrase", ["dismiss", "escape"])
    def test_the_word_alone_still_presses_escape(self, router, phrase):
        from speech.domain import Action

        decision = router._resolve_finalization(phrase.split(), hotword_active=False)
        assert decision.action is Action.EXECUTE, (
            f"{phrase!r} spoken alone resolved to {decision.action}; the "
            f"alias is useless if the whole utterance does not fire"
        )
        assert decision.payload == phrase

    def test_the_two_dismiss_toggles_are_untouched(self, matcher):
        # Widening ^escape$ must not shadow the anchored two-word rows that
        # already used the word (wh-dismiss-alias-restore, 2026-08-20).
        for phrase in ("dismiss numbers", "dismiss grid"):
            result = matcher.match_complete(phrase, hotword_active=False)
            assert result and result.matched and result.matched_text == phrase, (
                f"{phrase!r} no longer fires its own row after the escape "
                f"trigger was widened"
            )
