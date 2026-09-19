r"""The literal body of a word-boundary replacement pattern
(wh-spaced-punctuation-names-unresolved, stage A).

``extract_full_literal_body`` accepted only a fully anchored pattern
(``^...$``). Every punctuation NAME in patterns.toml is written as an
unanchored word-boundary pattern instead -- ``\bopen single quote\b`` --
so the extractor returned '' for all of them, the catalog stored no
``literal_body_matchers``, ``_buffer_opens_literal_prefix`` had no
matcher to test, and ``PatternMatcher.can_continue(['open', 'single'],
'replacement')`` answered False. A buffer that holds the first two words
of a three-word name could not keep listening for the third.

Stage A extends the extractor to the word-boundary shape. It is a
prerequisite, not a user-visible fix: measured 2026-09-05 at be360c26,
the shipped one-utterance path answers through whole-utterance matching
and never asks ``can_continue`` about a replacement, so these tests pin
the matchers rather than a changed pipeline output. The pipeline
regression tests live in tests/test_pipeline_punctuation_names.py.

The truncation matchers are fullmatch-shaped, which stays correct for an
unanchored name: ``can_continue`` only reaches a pattern that the
first-word index returned for ``buffer[0]``, so the buffer under test
always starts at the name's first word.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import tomllib

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_matcher import PatternMatcher
from speech.pattern_transform import (
    build_literal_prefix_matchers,
    extract_full_literal_body,
    extract_literal_prefix,
)

_PATTERNS_TOML = "speech/config/patterns.toml"


@pytest.fixture(scope="module")
def catalog():
    return PatternCatalog(_PATTERNS_TOML)


@pytest.fixture(scope="module")
def matcher(catalog):
    return PatternMatcher(catalog)


def _boundary_multiword_entries():
    """Every shipped ``\\b...\\b`` entry whose literal text has a space."""
    with open(_PATTERNS_TOML, "rb") as handle:
        doc = tomllib.load(handle)
    found = []
    for entry in doc.get("pattern", []):
        text = entry.get("pattern", "")
        if text.startswith(r"\b") and text.endswith(r"\b"):
            inner = text[2:-2]
            if " " in inner:
                found.append((text, inner, entry.get("doc_id")))
    return found


class TestBoundaryAnchoredBodyExtraction:
    """The extractor returns the body of a ``\\b...\\b`` pattern."""

    def test_the_open_single_quote_name_yields_its_words(self):
        assert extract_full_literal_body(r"\bopen single quote\b") == (
            "open single quote"
        )

    def test_the_body_feeds_the_matcher_builder(self):
        matchers = build_literal_prefix_matchers(
            extract_full_literal_body(r"\bopen single quote\b")
        )
        assert matchers is not None
        assert [m.pattern for m in matchers] == [
            "^open single quote$",
            "^open$",
            "^open single$",
        ]

    def test_an_anchored_pattern_is_unchanged(self):
        """The shape the extractor already owned still behaves the same."""
        assert extract_full_literal_body("^push to talk mode$") == (
            "push to talk mode"
        )

    def test_an_anchored_pattern_with_a_leading_boundary_is_unchanged(self):
        assert extract_full_literal_body(r"^\bpush to talk mode$") == (
            "push to talk mode"
        )

    def test_a_pattern_anchored_at_neither_end_is_still_refused(self):
        """Only ``^...$`` and ``\\b...\\b`` are extractable shapes."""
        assert extract_full_literal_body("open single quote") == ""

    def test_the_empty_boundary_pattern_is_refused(self):
        r"""``\b\b`` is bounded at both ends and holds no text.

        Nothing refuses it explicitly. The strip takes two characters
        off each end, so ``\b`` and ``\b\b`` both come out as the
        empty string, and the caller already treats "" as "no body".
        An earlier ``len(p) > 4`` test sat here and rejected exactly
        those two inputs; it changed neither answer, so it was removed.
        This test pins the outcome whichever line produces it.
        """
        assert extract_full_literal_body(r"\b\b") == ""

    def test_a_half_bounded_pattern_is_refused(self):
        """One boundary is not the whole text the user must speak."""
        assert extract_full_literal_body(r"\bopen single quote") == ""
        assert extract_full_literal_body(r"open single quote\b") == ""

    def test_a_greedy_boundary_pattern_stays_with_the_prefix_extractor(self):
        """The two extractors stay disjoint (wh-review-pattern-fixes.12).

        A greedy tail belongs to ``extract_literal_prefix``; the body
        extractor must return '' so no pattern gets both.
        """
        greedy = r"\bclick (.+)\b"
        assert extract_full_literal_body(greedy) == ""


class TestQuantifierInsideTheLiteralText:
    """A3: a quantifier inside the name's text is kept, not refused.

    ``\\bgreater than sign\\b`` ships as ``\\bgreater ?than ?sign\\b`` --
    the spaces are optional, so one spoken token "greaterthansign" matches
    the same pattern. The extractor returns the body UNCHANGED, quantifier
    included, and lets ``build_literal_prefix_matchers`` handle it exactly
    as it already handles a quantifier inside an anchored body. Refusing
    such a pattern instead would leave EIGHTEEN shipped names with no
    matchers for the sake of a rule the builder does not need. That count
    is measured, not estimated: run the real extractor over all 319
    entries in speech/config/patterns.toml and keep the bodies that
    contain " ?". Of the 64 bounded names, 49 are multi-word and 18 carry
    the optional-space quantifier. The "?" inside the "(?:" openers of
    the four grouped names is group syntax, not a quantifier, and does
    not count.
    """

    def test_the_optional_space_survives_into_the_body(self):
        assert extract_full_literal_body(r"\bgreater ?than ?sign\b") == (
            "greater ?than ?sign"
        )

    def test_the_truncation_matcher_accepts_both_spacings(self):
        matchers = build_literal_prefix_matchers(
            extract_full_literal_body(r"\bgreater ?than ?sign\b")
        )
        assert matchers is not None
        two_words = [m for m in matchers if m.pattern == "^greater ?than$"]
        assert two_words, [m.pattern for m in matchers]
        assert two_words[0].match("greater than")
        assert two_words[0].match("greaterthan")

    def test_a_group_inside_the_name_expands(self):
        """``(?:caret|carrot) sign`` must yield both spoken variants."""
        matchers = build_literal_prefix_matchers(
            extract_full_literal_body(r"\b(?:caret|carrot) sign\b")
        )
        assert matchers is not None
        patterns = [m.pattern for m in matchers]
        assert "^caret sign$" in patterns
        assert "^carrot sign$" in patterns


class TestTheCatalogStoresMatchersForEveryName:
    """A1: every multi-word word-boundary name gets body matchers."""

    def test_the_shipped_catalog_has_such_names(self):
        entries = _boundary_multiword_entries()
        assert len(entries) >= 40, len(entries)

    def test_every_one_of_them_has_body_matchers(self, catalog):
        missing = []
        for text, _inner, doc_id in _boundary_multiword_entries():
            first_word = catalog._extract_first_words(text)
            if not first_word:
                # A name whose first word the index itself loses is a
                # different defect, escalated as QUESTIONS-2026-09-04
                # item 37 (punct-ampersand). It is out of scope here.
                continue
            for compiled, _ptype, data in catalog.get_matching_patterns(
                first_word[0]
            ):
                if compiled.pattern != text:
                    continue
                if not (data or {}).get("literal_body_matchers"):
                    missing.append((text, doc_id))
        assert not missing, missing

    def test_no_name_gets_both_a_prefix_and_a_body(self):
        """The two extractors must stay disjoint for these shapes too."""
        both = [
            text
            for text, _inner, _doc_id in _boundary_multiword_entries()
            if extract_full_literal_body(text)
            and extract_literal_prefix(text)
        ]
        assert not both, both


class TestCanContinueHoldsAPartialName:
    """A1: the buffer keeps listening for the last word of a name."""

    def test_open_single_can_continue(self, matcher):
        assert matcher.can_continue(["open", "single"], "replacement") is True

    def test_greater_than_can_continue(self, matcher):
        assert matcher.can_continue(["greater", "than"], "replacement") is True

    def test_a_buffer_that_left_the_name_cannot_continue(self, matcher):
        """Ordinary dictation must not be held back.

        ``open sideways`` starts with a name's first word and then leaves
        it, so no truncation matcher accepts it.
        """
        assert (
            matcher.can_continue(["open", "sideways"], "replacement") is False
        )
