r"""Catalog tests for an alternation whose branch ends in an optional group.

wh-and-sign-index-defect. The shipped pattern for '&' read

    \b(?:ampersand|and sign(?:ed)?)\b

and it listed three spoken aliases: "ampersand", "and sign" and "and
signed". Only "ampersand" reached the first-word index, so the router
never searched this pattern for an utterance starting with "and" and the
user saw the words typed instead of the mark.

The cause is the optional-prefix branch of
``PatternCatalog._extract_first_words``. Its regex

    \(\?:([^)]+)\s*\)\?(.+)

means "a leading ``(?:...)`` group followed by ``?``", but ``[^)]+``
cannot cross a close paren, so on this pattern it stops inside at
``(?:ed``. The ``\)\?`` then matches the inner group's own ``)?`` and the
branch fires on a pattern that has no optional prefix at all. It reads
the whole alternation as one "prefix" and hands
``ampersand|and sign(?:ed`` to ``_extract_simple_words``, which takes the
first word-like run and returns ``ampersand``. The second and third
aliases are never seen.

This is the same first-close-paren mistake the grid-click repair fixed in
the neighbouring alternation branch (wh-grid-click-nested-group,
c89da4fb), in a different branch of the same function. The crewcut comment
that repair left behind names the remaining limit and the way out: scan
the leading group with balanced parentheses instead of stopping at the
first close paren.

Repairing the extractor made the two "and" aliases reachable, and
measuring that reachability is what settled the shipped entry. This is a
REPLACEMENT pattern, so it matches anywhere in an utterance, not only at
its start: with "and" indexed, "he read and signed the form" typed "he
read & the form" and "please come and sign" typed "please come &".
David answered QUESTIONS-2026-09-05 item 94 with option three -- drop the
two aliases. "ampersand" is the spoken name for "&"; "and sign" and "and
signed" are ordinary English words, and typing them is now the intended
behaviour. wh-and-sign-index-defect.1.1 holds the measurements.

So these tests pin two separate things. The extractor is pinned on the
alternation shape, which is a FIXTURE here rather than a copy of the
shipped text: the defect is real for every nested alternation, so the
shape is worth pinning even though no shipped pattern carries it today.
The shipped entry is pinned separately, at the narrowed text it carries
now, and one of those tests asserts the aliases have NOT come back -- if
they did, the repaired extractor would make them reachable again and the
sentences above would lose their words.

The synthetic shapes are written out literally here rather than read from
patterns.toml, for the reason the grid-click file gives: if a shipped
pattern were ever rewritten, the two would otherwise be able to hide each
other.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.pattern_catalog import PatternCatalog


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'

# The shape the shipped ampersand entry carried until QUESTIONS-2026-09-05
# item 94 removed the two aliases. It is a FIXTURE now, not a copy of the
# shipped text: the extractor defect it exercises is real for every nested
# alternation, so the shape is still worth pinning even though no shipped
# pattern carries it today.
AMPERSAND_SHAPE = r'\b(?:ampersand|and sign(?:ed)?)\b'

# What the shipped entry carries now.
SHIPPED_AMPERSAND = r'\bampersand\b'

# The same shape with every word replaced, so a test that passes here
# cannot be passing because of anything specific to "and" or "sign".
SYNTHETIC_SHAPE = r'(?:alpha|beta gamma(?:ed)?)'


def _write_patterns(tmp_path: Path, body: str) -> str:
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + body, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def shipped_catalog():
    patterns = (
        Path(__file__).resolve().parent.parent
        / "speech" / "config" / "patterns.toml"
    )
    return PatternCatalog(str(patterns))


@pytest.fixture
def empty_catalog(tmp_path):
    return PatternCatalog(_write_patterns(tmp_path, ""), user_patterns_file="")


class TestTheExtractorReadsEveryAlternative:
    """Acceptance A4: the shape, and the shapes it must not disturb."""

    def test_the_ampersand_shape_indexes_both_first_words(
        self, empty_catalog
    ):
        assert sorted(
            empty_catalog._extract_first_words(AMPERSAND_SHAPE)
        ) == ["ampersand", "and"]

    def test_the_synthetic_shape_indexes_both_first_words(
        self, empty_catalog
    ):
        assert sorted(
            empty_catalog._extract_first_words(SYNTHETIC_SHAPE)
        ) == ["alpha", "beta"]

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            # An optional prefix whose REMAINDER carries an optional group.
            # The old regex stopped at that inner group's close paren too,
            # so "downward" -- a word the pattern really can start with --
            # was missing from the index. Measured before the fix:
            # ['down', 'go'].
            (r'(?:go )?down(?:ward)?', ["down", "downward", "go"]),
            # Three alternatives, the last one carrying the optional group,
            # so the scan cannot stop after two. Measured before the fix:
            # ['alpha'].
            (
                r'(?:alpha|beta|gamma delta(?:s)?)',
                ["alpha", "beta", "gamma"],
            ),
        ],
        ids=["optional-prefix-with-optional-tail", "three-alternatives"],
    )
    def test_the_shapes_the_repair_also_fixes(
        self, empty_catalog, pattern, expected
    ):
        assert sorted(empty_catalog._extract_first_words(pattern)) == expected

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            # A shipped pattern with a capital that reaches the new branch:
            # '^Windows? settings$' at patterns.toml:216. Every lookup goes
            # through _normalize_lookup_word, which lowercases, so an index
            # entry that keeps its capital can never be found and the
            # command stops answering. Measured with the .lower() call
            # deleted: ['Window', 'Windows'].
            (r'Windows? settings', ["window", "windows"]),
            # The same inside an alternation, so a pass here cannot come
            # from the optional-letter shape alone.
            (r'(?:Alpha|Beta gamma(?:ed)?)', ["alpha", "beta"]),
        ],
        ids=["shipped-windows-settings", "capitalised-alternation"],
    )
    def test_the_expander_lowercases_what_it_indexes(
        self, empty_catalog, pattern, expected
    ):
        assert sorted(empty_catalog._extract_first_words(pattern)) == expected

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            # The genuine optional prefix. The repaired branch must still
            # recognise it, and this is the shape whose regex is being
            # replaced, so it is the one most at risk.
            (r'(?:go )?down', ["down", "go"]),
            # No group at all: the plain path must be untouched.
            (r'quotes?', ["quote", "quotes"]),
            # The grid-click shape the neighbouring branch repairs. This
            # repair must not disturb it (acceptance A4).
            (r'^((?:click|tap)[.!?]?)$', ["click", "tap"]),
            # Plain capturing alternations, which always worked.
            (r'^(backspace|back space)$', ["back", "backspace"]),
            (r'^((right|double)[\s-]+click[.!?]?)$', ["double", "right"]),
        ],
        ids=[
            "optional-prefix",
            "no-group",
            "grid-click-nested",
            "capturing-multiword",
            "capturing-separator-class",
        ],
    )
    def test_the_shapes_that_already_worked_still_work(
        self, empty_catalog, pattern, expected
    ):
        assert sorted(empty_catalog._extract_first_words(pattern)) == expected


class TestTheShippedAmpersandPattern:
    """Acceptance A1: measured on the real speech/config/patterns.toml."""

    def test_the_shipped_entry_carries_only_the_spoken_name(self):
        # David answered QUESTIONS-2026-09-05 item 94 with option three:
        # drop the two aliases. "ampersand" is the spoken name for "&";
        # "and sign" and "and signed" are ordinary words. If a later
        # change puts them back, this test says so rather than letting
        # the tests below pass for a reason unrelated to the extractor.
        # Read the pattern LINES, not the whole file: the entry carries a
        # comment that quotes the old shape to record why it changed, and
        # a whole-file search would match that comment.
        lines = [
            line.strip()
            for line in (
                Path(__file__).resolve().parent.parent
                / "speech" / "config" / "patterns.toml"
            ).read_text(encoding="utf-8").splitlines()
            if line.startswith("pattern = ")
        ]

        assert f"pattern = '''{SHIPPED_AMPERSAND}'''" in lines, (
            "patterns.toml no longer carries the narrowed shape these "
            f"tests pin, {SHIPPED_AMPERSAND!r}"
        )
        assert f"pattern = '''{AMPERSAND_SHAPE}'''" not in lines, (
            "the two aliases are back in patterns.toml; item 94 removed "
            f"them, so {AMPERSAND_SHAPE!r} must not be a shipped pattern"
        )

    def test_the_spoken_name_reaches_the_ampersand_pattern(
        self, shipped_catalog
    ):
        inserted = [
            action.get("params", [None])[0]
            for entry in shipped_catalog.get_matching_patterns("ampersand")
            for action in entry[2].get("actions", [])
            if action.get("function") == "text"
        ]

        assert "&" in inserted, (
            "'ampersand' does not reach the ampersand pattern through the "
            f"first-word index; the text actions it does reach insert "
            f"{inserted!r}"
        )

    def test_the_word_and_reaches_no_pattern_that_inserts_the_mark(
        self, shipped_catalog
    ):
        # The other half of item 94, and the one that fails silently if
        # the aliases come back: "and" must not carry the user into the
        # ampersand pattern, because "he read and signed the form" would
        # then type "he read & the form".
        inserted = [
            action.get("params", [None])[0]
            for entry in shipped_catalog.get_matching_patterns("and")
            for action in entry[2].get("actions", [])
            if action.get("function") == "text"
        ]

        assert "&" not in inserted, (
            "'and' still reaches a pattern that inserts '&'; item 94 "
            f"removed the aliases that did that. Text actions: {inserted!r}"
        )

    def test_the_spoken_name_is_in_the_first_word_index(
        self, shipped_catalog
    ):
        assert shipped_catalog.could_be_pattern_start("ampersand") is True
