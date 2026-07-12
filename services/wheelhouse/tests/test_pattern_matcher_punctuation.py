"""PatternMatcher tests for STT-injected punctuation on the first word.

Slice wh-9f51.1: PatternMatcher.match_complete must route 'backspace,'
(spoken 'backspace comma') to the backspace command, leaving the comma
in the remainder so downstream replacement / dictation can handle it.

These tests pair with tests/test_pattern_catalog_punctuation.py; the
catalog change alone gets us the dict lookup, but match_complete also
runs compiled_pattern.fullmatch() against the original text. For a
command pattern anchored with '^...$', the trailing comma blocks the
fullmatch even though the catalog returns the right candidate. The
matcher therefore needs to strip the trailing punctuation off the
fullmatch input and surface it back through 'remainder'.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.pattern_catalog import PatternCatalog
from speech.pattern_matcher import PatternMatcher


_HEADER = 'COMMAND_HOTWORD = "x-ray"\n\n'


def _write_patterns(tmp_path: Path, body: str) -> str:
    p = tmp_path / "patterns.toml"
    p.write_text(_HEADER + body, encoding="utf-8")
    return str(p)


@pytest.fixture
def production_catalog():
    """The real, on-disk patterns.toml. Used for the 'no regression on
    count-suffix backspace' check -- we want to confirm the production
    'backspace three' pattern still resolves after the fix."""
    return PatternCatalog("speech/config/patterns.toml")


@pytest.fixture
def production_matcher(production_catalog):
    return PatternMatcher(production_catalog)


class TestMatchCompletePunctuation:
    """The text-level fullmatch path must tolerate STT-attached
    punctuation on a command pattern and surface the trailing piece via
    remainder so downstream typing/replacement still happens."""

    def _backspace_only_catalog(self, tmp_path: Path) -> PatternCatalog:
        body = """
[[pattern]]
pattern = '''^back ?space\\s*(\\d+)?$'''
actions = [
    { function = "press", params = ["backspace", "g1"] }
]
"""
        return PatternCatalog(_write_patterns(tmp_path, body))

    def test_backspace_with_trailing_comma_matches_command(self, tmp_path):
        catalog = self._backspace_only_catalog(tmp_path)
        matcher = PatternMatcher(catalog)
        result = matcher.match_complete("backspace,", pattern_type="command")
        assert result is not None, (
            "Expected 'backspace,' to match the backspace command after "
            "punctuation normalization; got no match."
        )
        assert result.matched is True
        assert result.pattern_type == "command"

    def test_backspace_with_trailing_comma_leaves_comma_in_remainder(
        self, tmp_path,
    ):
        catalog = self._backspace_only_catalog(tmp_path)
        matcher = PatternMatcher(catalog)
        result = matcher.match_complete("backspace,", pattern_type="command")
        assert result is not None
        # The trailing punctuation has to survive somewhere so downstream
        # replacement/dictation can deliver the comma the user spoke.
        assert "," in result.remainder, (
            f"Expected ',' to appear in remainder so dictation can type "
            f"it; got remainder={result.remainder!r}"
        )

    def test_backspace_with_trailing_period_matches_command(self, tmp_path):
        catalog = self._backspace_only_catalog(tmp_path)
        matcher = PatternMatcher(catalog)
        result = matcher.match_complete("backspace.", pattern_type="command")
        assert result is not None
        assert result.matched is True

    def test_backspace_bare_still_matches(self, tmp_path):
        """No regression: the existing 'backspace' (no punctuation) path
        must keep working."""
        catalog = self._backspace_only_catalog(tmp_path)
        matcher = PatternMatcher(catalog)
        result = matcher.match_complete("backspace", pattern_type="command")
        assert result is not None
        assert result.matched is True

    def test_backspace_count_suffix_still_matches(self, production_matcher):
        """No regression on the production 'backspace three' shape that
        uses the count capture group. The bead asks us to confirm a
        count-suffix backspace pattern keeps working after the change."""
        result = production_matcher.match_complete(
            "backspace 3", pattern_type="command",
        )
        assert result is not None, (
            "Sanity check failed: production catalog should match "
            "'backspace 3' against ^back ?space\\s*(\\d+)?$"
        )
        assert result.matched is True


class TestMidWordPunctuation:
    """wh-midword-punct-severs-count: STT/ITN can attach punctuation to
    an INTERIOR word of a multi-word command ('back space, 3'). The
    first-word normalization and the whole-text trailing rstrip both
    miss it, so the count-suffix fullmatch failed and the command was
    severed from its count. A third retry normalizes every word."""

    def test_interior_comma_keeps_count(self, production_matcher):
        result = production_matcher.match_complete(
            "back space, 3", pattern_type="command",
        )
        assert result is not None, (
            "Expected 'back space, 3' to match the count-suffix backspace "
            "pattern after per-word punctuation normalization; got None."
        )
        assert result.matched is True
        assert result.group(1) == "3"
        assert result.remainder == ""

    def test_count_recovery_across_count_patterns(self, production_matcher):
        # wh-midword-punct-severs-count.2.1: the count-recovery mechanism
        # is generic, but the count-suffix patterns differ in separator
        # shape (\s* vs \s+), capture-group number (g1 vs g2), and
        # alternation. Pin recovery across those shapes so a future
        # refactor of the per-word strip cannot silently break one of
        # them. Each spoken count arrives with STT punctuation attached
        # ("delete, 3"); the count must survive to the capture group.
        cases = [
            # text, count_group, expected_count
            ("delete, 3", 1, "3"),   # \s* separator, count in g1
            ("tab, 3", 2, "3"),      # \s+ separator, count in g2, alternation
            ("indent, 2", 2, "2"),   # alternation, other branch
            ("undo, 4", 1, "4"),     # \s* separator, count in g1
            ("redo, 2", 1, "2"),
        ]
        for text, group_n, expected in cases:
            result = production_matcher.match_complete(
                text, pattern_type="command",
            )
            assert result is not None and result.matched, (
                f"{text!r} should recover its count after punctuation "
                f"normalization; got no match."
            )
            assert result.group(group_n) == expected, (
                f"{text!r}: expected count {expected!r} in group {group_n}, "
                f"got {result.group(group_n)!r}."
            )

    def test_interior_and_trailing_punctuation(self, production_matcher):
        # Trailing punctuation on the LAST word must still fold into
        # remainder (the wh-9f51.1 contract); interior punctuation is
        # discarded as STT noise (the wh-9f51.3 convention).
        result = production_matcher.match_complete(
            "back space, 3,", pattern_type="command",
        )
        assert result is not None
        assert result.matched is True
        assert result.group(1) == "3"
        assert result.remainder == ","

    def test_parameterized_capture_keeps_interior_punctuation(
        self, production_matcher,
    ):
        # The press command captures its argument with (.+); a comma
        # inside the argument is part of what the user wants pressed
        # and must survive. The original-text fullmatch runs FIRST, so
        # per-word normalization must never rewrite this case.
        result = production_matcher.match_complete(
            "press control, c", pattern_type="command",
        )
        assert result is not None
        assert result.matched is True
        assert result.group(1) == "control, c"

    def test_all_punctuation_interior_word_fires_no_command(
        self, production_matcher,
    ):
        # wh-midword-punct-severs-count.1.1: a standalone punctuation
        # token between a command word and a count ("delete, 3 items
        # left" -> ["delete", ",", "3", ...]) must NOT fire a counted
        # command. The count patterns use \s* / \s+ between the word and
        # the number, so a naive rejoin that leaves a double space would
        # let \s* absorb both spaces and fire a spurious irreversible
        # command. Every vulnerable position must return None.
        for text in ("back , space", "back space , 3", "delete , 3", "tab , 3"):
            result = production_matcher.match_complete(
                text, pattern_type="command",
            )
            assert result is None, (
                f"{text!r} contains a standalone punctuation word and must "
                f"not fire a command; got a match instead."
            )
