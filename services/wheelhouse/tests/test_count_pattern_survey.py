r"""Every shipped count pattern must accept a spoken number, not only digits.

The survey required by wh-number-words-one-parser acceptance criterion 2.
The fix widened the numeric capture in ONE place -- ``_transform_numeric_
captures`` in speech/pattern_transform.py -- so no line of
speech/config/patterns.toml was edited. That is the safest shape of the
change and also the least visible one: nothing in the pattern file records
which commands it altered. This module supplies that record by building it
from the shipped file at test time, so it cannot drift the way a hand-written
list of command names would.

What it checks, for every pattern the shipped file gives a numeric capture:

1. The catalog widened it (``validation_group`` is set on the loaded entry).
2. Widening did not renumber the captures -- the transformed pattern has the
   same number of capture groups as the raw one. The transform records the
   capture number ``re.compile`` assigns, so a capturing group introduced
   inside the widened body would silently point validation at the wrong text.
3. A probe utterance built from the pattern itself matches with the count
   written three ways: as digits ("15"), as one number word ("fifteen"), and
   as two ("twenty three"). The first is the behaviour that already worked;
   the second and third are what David reported broken on 2026-09-02.
4. ``words_to_int`` reads each captured count back to the intended integer,
   which is what ``PatternMatcher.validate_numeric`` asks before it lets the
   match execute.

The probe is generated from the pattern's own parse tree rather than written
by hand: 113 patterns is too many to list utterances for, and a hand-written
utterance would stop covering the pattern the moment someone edits it. The
generator walks ``re``'s parsed form and emits the shortest text each node
accepts, with the numeric capture replaced by the count under test. It is
deliberately small and refuses any regex construct the shipped count patterns
do not use, so an unhandled construct fails loudly instead of being skipped.
"""
import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech.actions import words_to_int
from speech.pattern_catalog import PatternCatalog

# ``re._parser`` is CPython's own regex parser, the module ``re`` itself
# imports. It is private, and this is a test-only use: the alternative is a
# hand-rolled regex reader, which would be a second parser to keep correct.
# ``sre_parse`` is the pre-3.11 name for the same module.
try:  # pragma: no cover - one arm runs per interpreter version
    from re import _parser as _re_parser  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import sre_parse as _re_parser  # type: ignore[no-redef]

SHIPPED_PATTERNS = Path(__file__).parent.parent / "speech" / "config" / "patterns.toml"

#: The count of shipped patterns that carry a numeric capture. Pinned so that
#: adding or removing a count command is a deliberate edit here, with the new
#: command's probe proven by this module in the same commit.
EXPECTED_COUNT_PATTERNS = 113

#: What the count sounds like, and the integer it must become. The last
#: three probe the two ends of the widened body, which is where it can
#: drift from the parser without any shorter probe noticing: the longest
#: phrase the parser reads is six tokens once the leading "number" filler
#: is counted, and the filler may be followed by digits rather than words.
SPOKEN_COUNTS = {
    "15": 15,
    "fifteen": 15,
    "twenty three": 23,
    "twenty-three": 23,
    "number one hundred and twenty three": 123,
    "numbers one hundred and twenty-three": 123,
    "number 75": 75,
}

#: Stands in for the numeric capture while the probe is generated. Wrapped in
#: a non-capturing group at substitution time so a quantifier after the
#: original ``(\d+)`` still applies to the whole slot and not to its last
#: letter.
_SLOT = "zzcountslotzz"


def _sample_class(items):
    """Shortest text a parsed character class accepts."""
    if items and items[0][0].name == "NEGATE":
        return "x"
    opcode, argument = items[0]
    name = opcode.name
    if name == "LITERAL":
        return chr(argument)
    if name == "RANGE":
        return chr(argument[0])
    if name == "CATEGORY":
        by_category = {
            "CATEGORY_DIGIT": "5",
            "CATEGORY_SPACE": " ",
            "CATEGORY_WORD": "a",
        }
        if argument.name in by_category:
            return by_category[argument.name]
    raise AssertionError(f"character class item {name} is not handled: {items!r}")


def _sample(parsed):
    """Shortest non-empty text the parsed pattern accepts.

    "Shortest non-empty" rather than "shortest": an optional element is
    emitted once, so ``\\s*`` becomes one space and ``characters?`` becomes
    "characters". Dropping them would build "delete15" and "delete character",
    which the regex accepts but nobody says.
    """
    out = []
    for opcode, argument in parsed:
        name = opcode.name
        if name == "LITERAL":
            out.append(chr(argument))
        elif name == "NOT_LITERAL":
            out.append("x" if chr(argument) != "x" else "y")
        elif name == "ANY":
            out.append("x")
        elif name in ("AT", "ASSERT", "ASSERT_NOT"):
            continue
        elif name == "IN":
            out.append(_sample_class(argument))
        elif name in ("MAX_REPEAT", "MIN_REPEAT"):
            low, high, body = argument
            out.append(_sample(body) * min(max(1, low), high))
        elif name == "SUBPATTERN":
            out.append(_sample(argument[3]))
        elif name == "BRANCH":
            out.append(_sample(argument[1][0]))
        else:
            raise AssertionError(f"regex opcode {name} is not handled")
    return "".join(out)


def _probe(raw_pattern, spoken):
    """An utterance the raw pattern accepts, with ``spoken`` as its count."""
    with_slot = raw_pattern.replace(r"(\d+)", f"(?:{_SLOT})")
    assert _SLOT in with_slot, f"no numeric capture found in {raw_pattern!r}"
    return _sample(_re_parser.parse(with_slot, re.IGNORECASE)).replace(_SLOT, spoken)


def _count_entries():
    """Every shipped pattern the loader gave a numeric validation group."""
    catalog = PatternCatalog(str(SHIPPED_PATTERNS), user_patterns_file="")
    return [
        entry
        for entry in catalog.get_all_patterns()
        if entry.get("validation_group")
    ]


COUNT_ENTRIES = _count_entries()


class TestTheSurveyCoversEveryShippedCountPattern:
    """The set this change reaches, measured from the shipped file."""

    def test_the_shipped_file_has_the_recorded_number_of_count_patterns(self):
        found = sorted(entry["raw_pattern"] for entry in COUNT_ENTRIES)
        assert len(found) == EXPECTED_COUNT_PATTERNS, (
            f"expected {EXPECTED_COUNT_PATTERNS} count patterns, found "
            f"{len(found)}. Update EXPECTED_COUNT_PATTERNS and confirm the "
            f"new command's probe passes below. Patterns:\n"
            + "\n".join(found)
        )

    def test_every_count_pattern_declares_a_numeric_capture(self):
        without = [
            entry["raw_pattern"]
            for entry in COUNT_ENTRIES
            if r"(\d+)" not in entry["raw_pattern"]
        ]
        assert without == [], (
            "these patterns were given a validation group without a plain "
            f"numeric capture: {without}"
        )


class TestACountCommandWaitsForTheWholeCount:
    r"""A multi-word count needs the command to wait for the sentence.

    ``whole_utterance_only`` is what makes a command wait. Without it a
    command fires as soon as the words spoken so far form it, so a count
    of two words is severed: "undu twenty three" pressed undo twenty
    times and then typed the word "three"
    (wh-number-words-one-parser.1.27).

    Commit 364c1d96 deliberately left "undu", "redu" and "back space"
    unflagged, on the stated ground that they "are not ordinary English
    words and lose nothing by still matching the opening words of a
    longer utterance". Multi-word counts made that ground false for the
    two mishearing aliases, and David reversed it for them on
    2026-09-03. "backspace" keeps firing on its first word, because it
    IS an ordinary word a user may dictate; the help text says so, and
    "delete" is the waiting command offered in its place.
    """

    #: The only shipped count pattern that fires on its first word. Any
    #: other entry reaching this list is a command that will sever a
    #: two-word count, so it belongs here only with the same kind of
    #: decision behind it.
    FIRST_WORD_COUNT_PATTERNS = [r"^back ?space\s*(\d+)?$"]

    def test_only_backspace_fires_before_the_count_is_finished(self):
        firing_early = sorted(
            entry["raw_pattern"]
            for entry in COUNT_ENTRIES
            if not entry.get("whole_utterance_only")
        )
        assert firing_early == self.FIRST_WORD_COUNT_PATTERNS, (
            "a count command that does not wait for the whole utterance "
            "presses its key for the first word of the count and types "
            "the rest into the document. Add whole_utterance_only = true "
            "in speech/config/patterns.toml, or record the decision here "
            f"and in the help text. Found: {firing_early}"
        )


@pytest.mark.parametrize(
    "entry",
    COUNT_ENTRIES,
    ids=[entry["raw_pattern"] for entry in COUNT_ENTRIES],
)
class TestEveryCountPatternAcceptsASpokenNumber:
    """The per-pattern survey."""

    def test_widening_did_not_renumber_the_captures(self, entry):
        raw_groups = re.compile(entry["raw_pattern"], re.IGNORECASE).groups
        assert entry["compiled_pattern"].groups == raw_groups, (
            f"{entry['raw_pattern']!r} changed capture count when widened; "
            "validation_group records the number re.compile assigns, so a "
            "capturing group inside the widened body points validation at "
            "the wrong text"
        )

    def test_the_count_is_captured_and_read_back(self, entry):
        group = int(entry["validation_group"][1:])
        for spoken, expected in SPOKEN_COUNTS.items():
            probe = _probe(entry["raw_pattern"], spoken)
            match = entry["compiled_pattern"].search(probe)
            assert match is not None, (
                f"{entry['raw_pattern']!r} did not match {probe!r}"
            )
            captured = match.group(group)
            assert captured == spoken, (
                f"{entry['raw_pattern']!r} on {probe!r} captured "
                f"{captured!r} in group {group}, expected {spoken!r}"
            )
            assert words_to_int(captured) == expected, (
                f"words_to_int refused {captured!r} from {probe!r}; "
                "PatternMatcher.validate_numeric would drop this match and "
                "the utterance would be dictated"
            )
