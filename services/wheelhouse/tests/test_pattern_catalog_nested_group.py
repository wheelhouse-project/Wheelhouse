r"""Pattern-catalog tests for a non-capturing group nested in a capturing one.

wh-grid-click-nested-group, David's item 10 of QUESTIONS-2026-09-04.md,
answer "option one": fix the extractor AND the shipped pattern.

The catalog builds a first-word index, and the router only searches
patterns that index the utterance's first word. The alternation branch of
``_extract_first_words`` finds its alternatives with ``\(([^)]+)\)``,
whose ``[^)]+`` stops at the FIRST close paren. On the shape
``^((?:click|tap)[.!?]?)$`` that captured ``(?:click|tap`` -- the inner
group's opening paren came along -- so the ``?:`` test that strips a
non-capturing prefix never fired, the split on ``|`` produced
``['(?:click', 'tap']``, and the first alternative was dropped.

What the user saw: with the mouse grid open, saying "click" typed the
word instead of clicking, while "tap" -- its alias in the very same
pattern -- worked. That was measured at four layers on
wh-grid-integration.

These tests pin the extractor for the shape, and they pin the shipped
catalog separately. The shape is written out literally here rather than
read from patterns.toml on purpose: the shipped pattern is ALSO being
rewritten to the unnested form, and these two repairs must not be able to
hide each other.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.pattern_catalog import PatternCatalog


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'

# The shape that broke, exactly as it shipped at patterns.toml:2738.
NESTED_SHAPE = r'^((?:click|tap)[.!?]?)$'


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


class TestNestedNonCapturingGroup:
    def test_the_extractor_keeps_every_alternative_of_the_nested_shape(
        self, tmp_path
    ):
        catalog = PatternCatalog(
            _write_patterns(tmp_path, ""), user_patterns_file=""
        )

        assert sorted(catalog._extract_first_words(NESTED_SHAPE)) == [
            "click",
            "tap",
        ]

    def test_a_pattern_of_the_nested_shape_indexes_both_words(self, tmp_path):
        path = _write_patterns(tmp_path, """
[[pattern]]
pattern = '''^((?:click|tap)[.!?]?)$'''
whole_utterance_only = true
actions = [
    { function = "grid_click_command", params = ["g1"] }
]
""")
        catalog = PatternCatalog(path, user_patterns_file="")

        assert "click" in catalog.first_words
        assert "tap" in catalog.first_words
        assert catalog.could_be_pattern_start("click") is True
        assert catalog.could_be_pattern_start("tap") is True

    def test_both_words_reach_the_pattern_through_the_index(self, tmp_path):
        # could_be_pattern_start only says the word is indexed. This asserts
        # the stronger thing the router needs: the index lookup returns the
        # pattern itself for either word.
        path = _write_patterns(tmp_path, """
[[pattern]]
pattern = '''^((?:click|tap)[.!?]?)$'''
whole_utterance_only = true
actions = [
    { function = "grid_click_command", params = ["g1"] }
]
""")
        catalog = PatternCatalog(path, user_patterns_file="")

        for word in ("click", "tap"):
            found = [
                entry[0].pattern
                for entry in catalog.get_matching_patterns(word)
            ]
            assert r'^((?:click|tap)[.!?]?)$' in found, (
                f"{word!r} does not reach the nested pattern; got {found!r}"
            )

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            # The two unnested shapes, which always worked. They are here so
            # a future repair of the nested shape cannot quietly break them.
            (r'^(?:click|tap)\s+(.+)$', ["click", "tap"]),
            (r'^((right|double)[\s-]+click[.!?]?)$', ["double", "right"]),
            (r'^(backspace|back space)$', ["back", "backspace"]),
        ],
        ids=["bare-non-capturing", "capturing", "capturing-multiword"],
    )
    def test_the_shapes_that_already_worked_still_work(
        self, tmp_path, pattern, expected
    ):
        catalog = PatternCatalog(
            _write_patterns(tmp_path, ""), user_patterns_file=""
        )

        assert sorted(catalog._extract_first_words(pattern)) == expected


class TestShippedGridClickPattern:
    """Criterion 2: the shipped pattern must not need the extractor fix.

    These read the real speech/config/patterns.toml.
    """

    def test_the_shipped_grid_pattern_is_not_written_in_the_nested_shape(self):
        toml = (
            Path(__file__).resolve().parent.parent
            / "speech" / "config" / "patterns.toml"
        ).read_text(encoding="utf-8")

        assert NESTED_SHAPE not in toml, (
            "patterns.toml still carries the nested shape "
            f"{NESTED_SHAPE!r}; rewrite it as '^((click|tap)[.!?]?)$' so the "
            "shipped pattern does not depend on the extractor repair"
        )

    def test_a_bare_click_reaches_the_grid_click_pattern(
        self, shipped_catalog
    ):
        found = [
            entry[2].get("actions", [{}])[0].get("function")
            for entry in shipped_catalog.get_matching_patterns("click")
            if entry[0].match("click")
        ]

        assert "grid_click_command" in found, (
            "a bare 'click' does not reach grid_click_command through the "
            f"first-word index; the functions it does reach are {found!r}"
        )

    def test_a_bare_tap_reaches_the_grid_click_pattern(self, shipped_catalog):
        found = [
            entry[2].get("actions", [{}])[0].get("function")
            for entry in shipped_catalog.get_matching_patterns("tap")
            if entry[0].match("tap")
        ]

        assert "grid_click_command" in found, (
            "a bare 'tap' does not reach grid_click_command through the "
            f"first-word index; the functions it does reach are {found!r}"
        )
