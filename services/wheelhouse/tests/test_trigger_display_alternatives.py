"""The Pattern Manager names every spoken wording of an alternation.

Finding wh-erase-synonym-for-delete.1.1, Boss e8 ruling OPTION 1, acceptance
criteria D1 to D3.

Widening the 18 delete commands to ^(?:delete|erase) moved them into a defect
that already held the go/move and click/tap families: PatternManager's
_TRIGGER_STRIP_RE removes a whole non-capturing group, so the display lost the
command's verb. Seventeen rows showed as "word", "all", "line" and the like,
and the bare row showed its expression verbatim. The Pattern Manager filter
reads trigger_display and nothing else, so typing "delete" or "erase" matched
none of them.

These tests read the shipped pattern file at run time rather than carrying a
copy of the wordings, so a command added later with an unnamed alternative
fails here.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_PATTERNS_FILE = _SERVICE_ROOT / "speech" / "config" / "patterns.toml"

if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from speech.pattern_manager import PatternManager  # noqa: E402

# The count measured at d6edafb5, the commit before this fix. The check below
# exists so an empty selection cannot make the census pass silently.
_ALTERNATION_PATTERN_COUNT = 18

# A non-capturing group that holds two or more alternatives and no nested
# parentheses. This is the shape the display code must name in full.
_ALTERNATION_RE = re.compile(r"\(\?:([^()|]*(?:\|[^()|]*)+)\)")


def _expanded_groups(expression: str) -> list[str]:
    """The alternation groups the display code expands, in order.

    A group followed by ? or * is optional, so the command also answers to
    the wording without it and the display leaves that group to the plain
    stripping (acceptance criterion D4). Such a group is therefore not part
    of what this census measures.
    """
    groups = []
    for match in _ALTERNATION_RE.finditer(expression):
        if expression[match.end():match.end() + 1] in ("?", "*"):
            continue
        groups.append(match.group(1))
    return groups


def _shipped_expressions() -> list[str]:
    """Every pattern expression in the shipped pattern file."""
    with _PATTERNS_FILE.open("rb") as handle:
        data = tomllib.load(handle)
    return [
        entry["pattern"]
        for entry in data.get("pattern", [])
        if isinstance(entry.get("pattern"), str)
    ]


class TestAlternationDisplay:
    """D1: every spoken alternative is named, in the established format."""

    @pytest.mark.parametrize(
        "expression, expected",
        [
            (r"^(?:delete|erase)\s+word$", "delete word (or erase word)"),
            (r"^(?:delete|erase)\s*(\d+)?$", "delete (or erase)"),
            (
                r"^(?:go|move)\s+up\s+(\d+)?\s*lines?$",
                "go up lines (or move up lines)",
            ),
            (r"^(?:click|tap)\s+(.+)$", "click (or tap)"),
        ],
    )
    def test_each_alternative_is_named(self, expression, expected):
        assert PatternManager._trigger_display(expression) == expected

    def test_no_display_is_a_raw_expression(self):
        # The bare delete row used to fall through to the raw expression,
        # because stripping the group left nothing behind.
        for expression in (
            r"^(?:delete|erase)\s*(\d+)?$",
            r"^(?:click|tap)\s+(.+)$",
        ):
            display = PatternManager._trigger_display(expression)
            assert display != expression
            assert "(?:" not in display
            assert "\\s" not in display

    def test_a_single_wording_keeps_its_plain_display(self):
        # Nothing about the one-wording case changes.
        assert PatternManager._trigger_display(r"^save$") == "save"
        assert (
            PatternManager._trigger_display(r"^cut\s+next\s+(\d+)?\s*words?$")
            == "cut next words"
        )

    def test_a_group_without_alternatives_keeps_todays_handling(self):
        # (?:this )? carries no alternative, so the stripper still removes it.
        assert (
            PatternManager._trigger_display(r"^(?:delete|erase)\s+(?:this )?word$")
            == "delete word (or erase word)"
        )

    def test_an_optional_group_keeps_todays_handling(self):
        # ^paste(?: that| here)?$ also accepts the bare word "paste", so
        # naming only the two longer wordings would hide a spoken form and
        # tell the user that "paste" alone does nothing. A group followed by
        # '?' therefore stays with the stripping (acceptance criterion D4).
        assert (
            PatternManager._trigger_display(r"^paste(?: that| here)?$")
            == "paste"
        )

    def test_a_required_group_still_expands_beside_an_optional_one(self):
        # Only the optional group steps aside; the required one is still
        # named in full.
        assert (
            PatternManager._trigger_display(
                r"^(?:delete|erase)(?: this| that)?\s+word$"
            )
            == "delete word (or erase word)"
        )

    def test_a_group_carrying_regex_syntax_falls_back(self):
        # \s is a character class, not a spoken word, so the group is not
        # expanded and the old stripping decides the display.
        display = PatternManager._trigger_display(r"^(?:\s|x)\s*word$")
        assert "(or" not in display

    def test_too_many_combinations_falls_back_to_the_plain_stripping(self):
        # Mutation gate entry the-combination-cap-no-longer-applies.
        # Four two-branch groups make sixteen wordings, past
        # _MAX_ALTERNATION_DISPLAYS, and one row cannot carry sixteen
        # phrases legibly. The cap hands such an expression back to the
        # plain stripping. No shipped pattern reaches the cap, so without
        # this test removing the cap changes no measured display.
        assert (
            PatternManager._trigger_display(
                r"^(?:delete|erase)\s+(?:this|that)\s+(?:long|short)"
                r"\s+(?:red|blue)\s+word$"
            )
            == "word"
        )

    def test_a_repeated_wording_is_named_once(self):
        # Mutation gate entry a-repeated-wording-is-displayed-twice.
        # Two branches can reduce to the same spoken words -- "patterns?"
        # and "patterns" both strip to "patterns" -- and the row must not
        # read "show patterns (or show patterns)". No shipped pattern
        # produces a duplicate wording, so without this test removing the
        # duplicate check changes no measured display.
        assert (
            PatternManager._trigger_display(r"^show\s+(?:patterns?|patterns)$")
            == "show patterns"
        )

    def test_a_group_with_one_branch_keeps_todays_handling(self):
        # Mutation gate entry a-single-branch-group-is-expanded. The pipe
        # here is escaped, so the group holds the literal text "a|b" and
        # offers no choice of wording. Expanding it would print regex
        # syntax on the row as though it were a spoken phrase.
        assert PatternManager._trigger_display(r"^(?:a\|b)\s+word$") == "word"

    def test_a_wording_that_strips_to_nothing_is_refused(self):
        # Mutation gate entry an-empty-wording-is-displayed-anyway. Every
        # wording of this expression strips away entirely, so naming the
        # wordings would put an empty label on the row. The refusal sends
        # the expression to the stripping below, which also yields nothing
        # and therefore shows the expression itself.
        expression = r"^(?:\?|\*)\s*$"
        display = PatternManager._trigger_display(expression)
        assert display, "the Pattern Manager row would carry an empty label"
        assert display == expression


class TestShippedPatternCensus:
    """D3: every literal alternative of every shipped pattern is displayed."""

    def test_every_shipped_alternative_appears_in_its_display(self):
        expressions = _shipped_expressions()
        assert expressions, "the shipped pattern file yielded no expressions"

        checked = 0
        missing: list[str] = []
        for expression in expressions:
            groups = _expanded_groups(expression)
            if not groups:
                continue
            checked += 1
            display = PatternManager._trigger_display(expression)
            for group in groups:
                for alternative in group.split("|"):
                    if not alternative or "\\" in alternative:
                        continue
                    # An alternative may carry a quantifier, as "patterns?"
                    # does in (?:patterns?|pattern manager). The display
                    # names its spoken form, so the census compares the same
                    # stripping the display applies.
                    spoken = PatternManager._strip_display(alternative)
                    if spoken and spoken not in display:
                        missing.append(
                            f"{expression!r} displays as {display!r}, "
                            f"which never says {spoken!r}"
                        )

        assert checked >= _ALTERNATION_PATTERN_COUNT, (
            f"only {checked} shipped patterns carry an alternation group; "
            f"at least {_ALTERNATION_PATTERN_COUNT} were expected, so this "
            "census is no longer measuring what it claims"
        )
        assert not missing, "\n".join(missing)

    def test_no_alternation_row_displays_a_raw_expression(self):
        """Only the ruling's class is covered here.

        Six shipped rows still display their expression verbatim, all of
        them through a CAPTURING group: ^(tab|indent)\\s+(\\d+)$,
        ^(shift tab|outdent)$, ^(mark[.!?]?)$, ^(drag[.!?]?)$ and
        ^(move here[.!?]?)$, plus the literal word "submit", whose display
        equals its expression because the expression is the spoken word.
        Every one of them displayed that way before this branch, and
        acceptance criterion D4 leaves a capturing group with today's
        handling, so this test measures the non-capturing class alone.
        """
        raw: list[str] = []
        checked = 0
        for expression in _shipped_expressions():
            if not _expanded_groups(expression):
                continue
            checked += 1
            display = PatternManager._trigger_display(expression)
            if "(?:" in display or "\\s" in display or display == expression:
                raw.append(f"{expression!r} displays as {display!r}")
        assert checked >= _ALTERNATION_PATTERN_COUNT, (
            f"only {checked} shipped patterns carry an alternation group"
        )
        assert not raw, "\n".join(raw)
