r"""Tests for speech/pattern_transform.py.

Finite expansion of group syntax in ``build_literal_prefix_matchers``
(wh-review-pattern-fixes.9): a pre-tail prefix that contains an optional
group ``(?:X )?`` or an alternation group ``(?:A|B)`` must produce
truncation matchers for every concrete word-sequence variant, so the
router's probe can accept a buffer that ends INSIDE the group. Shapes the
expander cannot expand exactly (unbounded repetition, capturing groups,
past-the-cap alternations) keep the prior behavior: whole-prefix matcher
plus whitespace truncations of the raw text.
"""
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from speech import pattern_transform as pattern_transform_module
from speech.safe_regex import RegexTimeout, match_bounded
from speech.pattern_transform import (
    MAX_PREFIX_MATCHERS,
    NUMBER_CAPTURE_BODY,
    NUMBER_CAPTURE_BODY_ATOMIC,
    NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN,
    NUMBER_CAPTURE_BODY_NO_HYPHEN,
    ANY_TEXT,
    _any_separator_sample_matches,
    _assertion_free_superset,
    _balanced_for_compile,
    _detached_body,
    _expand_prefix_variants,
    _matches_separators_only,
    _octal_escape_end,
    _resolve_references,
    build_literal_prefix_matchers,
    extract_full_literal_body,
    extract_literal_prefix,
    greedy_tail_probe_source,
    has_greedy_tail,
    transform_pattern,
)
from speech.number_word_parser import _WORD_ALTERNATION, parse_number_word
from speech.phrase_expression import generate_expression

# What a widened numeric capture holds. The tests below check WHICH group
# the transform widens and what number it records, not the body's own
# text, so they read it from the module rather than repeating it. It was
# r"\w+" until wh-number-words-one-parser widened it to the spoken-number
# grammar; tests/test_spoken_count_words.py checks what the body accepts.
_BODY = NUMBER_CAPTURE_BODY


def _bounded_fullmatch(transformed, text, timeout):
    """``(match or None, ran_away)`` for ``text`` within ``timeout``.

    The runaway comes back as a value rather than as a raised
    ``RegexTimeout`` so that the ASSERTION is what fails. The mutation
    gate reads a raised exception as a crash upstream of the check and
    refuses to count it as a catch, so a test that lets the timeout
    escape can never prove it catches the mutation it names.
    """
    try:
        return match_bounded(
            transformed,
            text,
            flags=re.IGNORECASE,
            timeout=timeout,
            mode="fullmatch",
        ), False
    except RegexTimeout:
        return None, True


def _accepts(matchers, text):
    """Answer the probe's question: does any anchored matcher accept text?"""
    return any(m.match(text) for m in matchers)


# ============================================================================
# Optional-group expansion: (?:the )? (the wh-review-pattern-fixes.9 repro)
# ============================================================================


class TestOptionalGroupExpansion:
    PREFIX = "look (?:the )?widget"

    def test_every_reachable_partial_state_accepted(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in ("look", "look the", "look widget", "look the widget"):
            assert _accepts(matchers, text), (
                f"{text!r} is a reachable spoken opening of "
                f"{self.PREFIX!r} and must keep the buffer listening"
            )

    def test_off_prefix_text_still_rejected(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in ("look sideways", "the", "widget", "the widget"):
            assert not _accepts(matchers, text), (
                f"{text!r} is not an opening of {self.PREFIX!r}"
            )

    def test_matchers_are_deduplicated(self):
        """Variants share truncations (both start with 'look'); each compiled
        pattern string must appear once."""
        matchers = build_literal_prefix_matchers(self.PREFIX)
        patterns = [m.pattern for m in matchers]
        assert len(patterns) == len(set(patterns))


# ============================================================================
# Alternation expansion, including a two-word branch
# ============================================================================


class TestAlternationExpansion:
    PREFIX = "grab (?:the file|a) copy"

    def test_every_branch_partial_state_accepted(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in (
            "grab",
            "grab the",
            "grab the file",
            "grab the file copy",
            "grab a",
            "grab a copy",
        ):
            assert _accepts(matchers, text), (
                f"{text!r} is a reachable spoken opening of "
                f"{self.PREFIX!r} and must keep the buffer listening"
            )

    def test_off_branch_text_still_rejected(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in ("grab copy", "grab banana", "grab the copy"):
            assert not _accepts(matchers, text), (
                f"{text!r} is not an opening of {self.PREFIX!r} -- the "
                f"alternation is not optional"
            )


# ============================================================================
# Nesting: an optional group inside an optional group
# ============================================================================


class TestNestedGroupExpansion:
    PREFIX = "look (?:the (?:big )?)?widget"

    def test_every_nested_partial_state_accepted(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in (
            "look",
            "look the",
            "look the big",
            "look widget",
            "look the widget",
            "look the big widget",
        ):
            assert _accepts(matchers, text), (
                f"{text!r} is a reachable spoken opening of "
                f"{self.PREFIX!r} and must keep the buffer listening"
            )

    def test_off_prefix_text_still_rejected(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        assert not _accepts(matchers, "look big")  # 'big' needs 'the' first


# ============================================================================
# Escapes outside the group survive the expansion compile path
# ============================================================================


class TestEscapedTextAroundGroups:
    PREFIX = r"press \+ (?:the )?key"

    def test_escaped_literal_matches_after_expansion(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        for text in ("press", "press +", "press + the", "press + key",
                     "press + the key"):
            assert _accepts(matchers, text)

    def test_escape_is_not_a_wildcard(self):
        matchers = build_literal_prefix_matchers(self.PREFIX)
        assert not _accepts(matchers, "press x the key")


# ============================================================================
# The bounds: shapes that keep the prior (whitespace-truncation) behavior
# ============================================================================


class TestExpansionBounds:
    def test_unbounded_repetition_keeps_prior_behavior(self):
        """(?:very )+ cannot be expanded exactly. The bound: only the
        whole-prefix matcher survives (each whitespace truncation of the raw
        text is an invalid regex), so a buffer inside the repetition still
        finalizes -- the documented miss, unchanged."""
        matchers = build_literal_prefix_matchers("(?:very )+nice")
        assert _accepts(matchers, "very nice")
        assert _accepts(matchers, "very very nice")
        assert not _accepts(matchers, "very")  # the bound, labeled

    def test_past_the_cap_alternation_keeps_prior_behavior(self):
        """36 concrete variants exceed the 32-expansion cap, so the prefix
        keeps today's raw-text matchers: the full prefix plus the two
        truncations that happen to rejoin valid group text."""
        prefix = "(?:a1|a2|a3|a4|a5|a6) (?:b1|b2|b3|b4|b5|b6) w"
        matchers = build_literal_prefix_matchers(prefix)
        patterns = [m.pattern for m in matchers]
        assert patterns == [
            "^" + prefix + "$",
            "^(?:a1|a2|a3|a4|a5|a6)$",
            "^(?:a1|a2|a3|a4|a5|a6) (?:b1|b2|b3|b4|b5|b6)$",
        ]

    def test_capturing_group_keeps_prior_behavior(self):
        """A capturing group is not expanded (the expander handles only
        (?:...) groups); the raw-text truncations stay as they were."""
        matchers = build_literal_prefix_matchers("(open|start) editor")
        patterns = [m.pattern for m in matchers]
        assert patterns == ["^(open|start) editor$", "^(open|start)$"]

    # -- wh-review-pattern-fixes.13: the cap counts DISTINCT variants --

    def test_duplicated_branches_dedupe_before_the_cap(self):
        """Six two-branch groups whose branches are identical: 2^6 = 64
        syntactic paths, ONE distinct variant. Before the fix the expander
        compared the syntactic count against MAX_PREFIX_EXPANSIONS, aborted,
        and fell back to raw-text matchers -- which wrongly rejected the
        reachable opening 'look a'. Deduplicating in first-seen order before
        the cap check keeps the expansion."""
        prefix = ("look (?:a |a )(?:b |b )(?:c |c )"
                  "(?:d |d )(?:e |e )(?:f |f )widget")
        matchers = build_literal_prefix_matchers(prefix)
        for text in ("look a", "look a b", "look a b c d e f widget"):
            assert _accepts(matchers, text), (
                f"{text!r} is a reachable spoken opening of {prefix!r} and "
                f"must keep the buffer listening"
            )

    def test_optional_group_with_empty_branch_dedupes(self):
        """An optional group whose alternation already contains an empty
        branch -- (?:a |)? -- appends a second empty string; the product
        loop must not emit the duplicate variant."""
        variants = _expand_prefix_variants("look (?:a |)?widget")
        assert variants is not None
        assert len(variants) == len(set(variants))
        assert sorted(variants) == ["look a widget", "look widget"]

    def test_duplicated_optional_empty_branches_expand_past_syntactic_cap(self):
        """Five (?:x |)? groups: 3^5 = 243 syntactic paths but only
        2^5 = 32 distinct variants, exactly at the cap. Dedup-before-cap
        must expand them."""
        prefix = "(?:a |)?(?:b |)?(?:c |)?(?:d |)?(?:e |)?f"
        matchers = build_literal_prefix_matchers(prefix)
        for text in ("a", "a b", "a b c d e f", "f", "b c f"):
            assert _accepts(matchers, text)

    def test_genuinely_distinct_variants_past_the_cap_still_fall_back(self):
        """The cap still binds DISTINCT variants: 36 real combinations
        return None from the expander and the prefix keeps the raw-text
        fallback (the hard cap stays independent of user input)."""
        prefix = "(?:a1|a2|a3|a4|a5|a6) (?:b1|b2|b3|b4|b5|b6) w"
        assert _expand_prefix_variants(prefix) is None

    def test_plain_word_prefix_unchanged(self):
        """The compile path for a prefix with no group syntax must not
        change: same matchers, same order."""
        matchers = build_literal_prefix_matchers("look up")
        assert [m.pattern for m in matchers] == ["^look up$", "^look$"]

    def test_plain_prefix_cap_still_holds(self):
        """A plain prefix still respects MAX_PREFIX_MATCHERS."""
        prefix = " ".join(f"zzqword{i}" for i in range(MAX_PREFIX_MATCHERS + 5))
        matchers = build_literal_prefix_matchers(prefix)
        assert len(matchers) <= MAX_PREFIX_MATCHERS


# ============================================================================
# wh-review-pattern-fixes.18: greediness classification must parse the
# pattern, not substring-scan it
# ============================================================================


class TestGreedyTailClassification:
    r"""has_greedy_tail must see only REAL greedy-tail spans.

    A real span is a '.' that is unescaped (an even number of immediately
    preceding backslashes), outside any character class, outside any
    zero-width or comment group (wh-review-pattern-fixes.22), and
    immediately followed by '*' or '+'. The pattern truncated after the
    END of the LAST real span, with the severed groups closed
    (wh-review-pattern-fixes.21), must also compile de-anchored. The old
    substring test ('.+' in pattern_str) misfired on every shape below
    (wh-review-pattern-fixes.18).
    """

    def test_escaped_dot_is_not_greedy(self):
        assert has_greedy_tail(r"^mark \.+$") is False

    def test_escaped_backslash_before_dot_is_greedy(self):
        r"""\\.+ is a literal backslash followed by a REAL greedy dot."""
        assert has_greedy_tail(r"^mark \\.+$") is True

    def test_character_class_dot_is_not_greedy(self):
        assert has_greedy_tail(r"^mark [.+]$") is False

    def test_mid_pattern_tail_is_greedy(self):
        assert has_greedy_tail(r"^say (.+) twice$") is True

    def test_bare_tail_is_greedy(self):
        assert has_greedy_tail(r"select (.+)") is True

    def test_alternation_branch_tail_stays_greedy(self):
        """The truncation '(mark|say .+)' compiles de-anchored, so the
        pattern stays classified greedy. That is fine: the path-level
        fullmatch probe in can_continue does the per-branch work
        (wh-review-pattern-fixes.18)."""
        assert has_greedy_tail(r"^(mark|say .+)$") is True

    def test_tail_inside_unclosed_lookahead_is_not_greedy(self):
        """The quantified dot sits inside a zero-width lookahead, so it is
        not a span at all (wh-review-pattern-fixes.22). The lookahead
        consumes nothing and 'ok$' bounds the utterance, so the pattern is
        bounded."""
        assert has_greedy_tail(r"^mark (?=ok(?: .+)?)ok$") is False


class TestGreedyTailProbeSource:
    """greedy_tail_probe_source returns the pattern truncated after the END
    of the LAST real span, with one ')' appended per group still open at
    that point (wh-review-pattern-fixes.21), leading '^' stripped, or None
    when no real span exists or the repaired truncation does not compile."""

    def _probe(self, pattern_str):
        from speech.pattern_transform import greedy_tail_probe_source
        return greedy_tail_probe_source(pattern_str)

    def test_mid_pattern_tail_source(self):
        assert self._probe(r"^say (.+) twice$") == r"say (.+)"

    def test_alternation_tail_source_includes_closing_paren(self):
        assert self._probe(r"^(mark|say .+)$") == r"(mark|say .+)"

    def test_no_real_span_returns_none(self):
        assert self._probe(r"^mark \.+$") is None

    def test_zero_width_wrapped_tail_returns_none(self):
        """No real span: the dot sits inside a zero-width lookahead
        (wh-review-pattern-fixes.22)."""
        assert self._probe(r"^mark (?=ok(?: .+)?)ok$") is None


class TestGreedyClassificationConsumers:
    r"""transform_pattern and the two extractors follow the scanner
    (wh-review-pattern-fixes.18): a pattern whose only '.+' text is an
    escaped dot is bounded, so it gets a full literal body (and, via the
    catalog, literal_body_matchers) instead of the greedy branch."""

    def test_transform_pattern_escaped_dot_not_marked_greedy(self):
        _transformed, metadata = transform_pattern(r"^set \.+ mode$")
        assert "is_greedy" not in metadata

    def test_transform_pattern_char_class_not_marked_greedy(self):
        _transformed, metadata = transform_pattern(r"^mark [.+]$")
        assert "is_greedy" not in metadata

    def test_transform_pattern_real_tail_still_marked_greedy(self):
        _transformed, metadata = transform_pattern(r"select (.+)")
        assert metadata.get("is_greedy") is True

    def test_full_literal_body_for_escaped_dot_pattern(self):
        assert extract_full_literal_body(r"^set \.+ mode$") == r"set \.+ mode"

    def test_literal_prefix_empty_for_escaped_dot_pattern(self):
        assert extract_literal_prefix(r"^mark \.+$") == ""


# ============================================================================
# wh-review-pattern-fixes.21: group-wrapped consuming tails
# ============================================================================


class TestGroupWrappedTailClassification:
    r"""A greedy tail wrapped in group syntax must classify greedy with a
    compiling probe source (wh-review-pattern-fixes.21).

    The probe source is the pattern truncated after the LAST real span, with
    one ')' appended for every group still open at that point. The old
    one-character paren extension could not close '(?:', '(?P<tail>', or a
    doubly-nested '((', so ^look up ((.*))$ lost its greedy metadata (the
    truncation did not compile) while the others classified greedy but
    could not build a probe that reaches a partial buffer.
    """

    def test_noncapturing_wrapped_tail_is_greedy(self):
        assert has_greedy_tail(r"^look up (?:.*)$") is True

    def test_noncapturing_wrapped_tail_probe_source(self):
        assert greedy_tail_probe_source(r"^look up (?:.*)$") == r"look up (?:.*)"

    def test_named_group_tail_is_greedy(self):
        assert has_greedy_tail(r"^look up (?P<tail>.+)$") is True

    def test_named_group_tail_probe_source(self):
        assert (
            greedy_tail_probe_source(r"^look up (?P<tail>.+)$")
            == r"look up (?P<tail>.+)"
        )

    def test_double_wrapped_tail_regains_greedy(self):
        """The finding's hardest shape: the old truncation 'look up ((.*'
        did not compile, so the pattern had NO greedy metadata AND no body
        matchers. Closing both open groups restores the metadata."""
        assert has_greedy_tail(r"^look up ((.*))$") is True

    def test_double_wrapped_tail_probe_source(self):
        assert (
            greedy_tail_probe_source(r"^look up ((.*))$") == r"look up ((.*))"
        )

    def test_alternation_group_tail_probe_source(self):
        assert (
            greedy_tail_probe_source(r"^(?:go home|look up .+)$")
            == r"(?:go home|look up .+)"
        )

    def test_mid_pattern_tail_probe_source_unchanged(self):
        """Guard: the appended ')' reproduces the old paren-extension
        result for the plain capturing shape."""
        assert greedy_tail_probe_source(r"^say (.+) twice$") == r"say (.+)"

    def test_transform_pattern_double_wrapped_tail_metadata(self):
        _transformed, metadata = transform_pattern(r"^look up ((.*))$")
        assert metadata.get("is_greedy") is True
        assert metadata.get("literal_prefix") == "look up"


class TestGroupWrappedTailLiteralPrefix:
    """extract_literal_prefix must repair unclosed groups so the prefix is
    speakable text, not a regex fragment (wh-review-pattern-fixes.21).

    The old prefix stopped at the span's paren extension, so group-wrapped
    tails produced fragments like 'look up (?:' that no matcher compiles,
    and can_continue(['look', 'up'], 'command') answered False although
    'look up value' fullmatches.
    """

    def test_noncapturing_wrapped_tail_prefix(self):
        assert extract_literal_prefix(r"^look up (?:.*)$") == "look up"

    def test_named_group_tail_prefix(self):
        assert extract_literal_prefix(r"^look up (?P<tail>.+)$") == "look up"

    def test_double_wrapped_tail_prefix(self):
        assert extract_literal_prefix(r"^look up ((.*))$") == "look up"

    def test_alternation_branch_prefix_keeps_tail_branch(self):
        """The repair keeps only the text after the last top-level '|'
        inside the unclosed group: the literal opening of the tail's own
        branch."""
        assert extract_literal_prefix(r"^(?:go home|look up .+)$") == "look up"

    def test_nested_alternation_prefix(self):
        """Nested case: repair the innermost unclosed group first, then the
        outer one."""
        assert extract_literal_prefix(r"^(?:a (?:b|c .+))$") == "a c"

    def test_capturing_alternation_prefix(self):
        """^(mark|say .+)$ improves from a raw fragment to 'say' -- the
        opening of the greedy branch. Harmless: a buffer held continuable
        by a 'say' truncation can always extend to a full match."""
        assert extract_literal_prefix(r"^(mark|say .+)$") == "say"

    def test_plain_capture_prefix_unchanged(self):
        assert extract_literal_prefix(r"^say (.+) twice$") == "say"


# ============================================================================
# wh-review-pattern-fixes.22: quantified dots inside zero-width or comment
# constructs are not greedy tails
# ============================================================================


class TestZeroWidthDotNotGreedy:
    r"""A quantified dot inside a lookaround or comment group consumes no
    input, so it must not classify the pattern greedy
    (wh-review-pattern-fixes.22).

    ^set (?=.+)mode$ is a bounded two-word command; the old scanner counted
    the lookahead's '.+' as a span, the balanced construct defeated the
    compile gate, and the command was routed onto the greedy timer with a
    garbage prefix 'set (?='.
    """

    def test_lookahead_dot_is_not_greedy(self):
        assert has_greedy_tail(r"^set (?=.+)mode$") is False

    def test_lookahead_dot_probe_source_is_none(self):
        assert greedy_tail_probe_source(r"^set (?=.+)mode$") is None

    def test_lookahead_pattern_not_marked_greedy(self):
        _transformed, metadata = transform_pattern(r"^set (?=.+)mode$")
        assert "is_greedy" not in metadata

    def test_lookahead_pattern_gets_full_literal_body(self):
        assert (
            extract_full_literal_body(r"^set (?=.+)mode$")
            == r"set (?=.+)mode"
        )

    def test_lookahead_pattern_literal_prefix_empty(self):
        assert extract_literal_prefix(r"^set (?=.+)mode$") == ""

    def test_negative_lookahead_dot_is_not_greedy(self):
        assert has_greedy_tail(r"^set (?!bad.*) mode$") is False

    def test_negative_lookahead_pattern_gets_full_literal_body(self):
        assert (
            extract_full_literal_body(r"^set (?!bad.*) mode$")
            == r"set (?!bad.*) mode"
        )

    def test_comment_group_dot_is_not_greedy(self):
        assert has_greedy_tail(r"^set (?# .+)mode$") is False

    def test_comment_group_pattern_gets_full_literal_body(self):
        """The comment is zero-width and is deleted from the body
        (wh-review-pattern-fixes.24), leaving the speakable text."""
        assert (
            extract_full_literal_body(r"^set (?# .+)mode$")
            == "set mode"
        )

    def test_real_tail_after_balanced_lookahead_still_greedy(self):
        """The false first span must not poison the real tail: the (.+) at
        the end is a real span, and the balanced zero-width group stays in
        the prefix text."""
        assert has_greedy_tail(r"^look up (?=.+)thing (.+)$") is True

    def test_real_tail_after_balanced_lookahead_prefix(self):
        assert (
            extract_literal_prefix(r"^look up (?=.+)thing (.+)$")
            == r"look up (?=.+)thing"
        )

    def test_real_tail_after_balanced_lookahead_probe_source(self):
        assert (
            greedy_tail_probe_source(r"^look up (?=.+)thing (.+)$")
            == r"look up (?=.+)thing (.+)"
        )


# ============================================================================
# wh-review-pattern-fixes.24: valid re constructs the repair mislexed
# ============================================================================


CONDITIONAL_TAIL = r"^say (a )?(?(1)foo|look .+)$"


class TestConditionalTailPrefix:
    r"""A conditional group (?(id)yes|no) is NOT ordinary alternation: the
    branch taken depends on whether group ``id`` matched, so the repair's
    keep-the-last-branch rewrite fabricated the prefix 'say (a )?look' for
    ^say (a )?(?(1)foo|look .+)$ -- and that prefix admits the buffer
    'say a look', which NO completion can match (group 1 set forces the
    'foo' branch). The fix: when the pre-tail cut runs through an unclosed
    conditional, extract_literal_prefix returns the conservative empty
    prefix. Greedy CLASSIFICATION stays True -- the '.+' is real and
    consuming -- so Strategy 2's path-level fullmatch probe still governs
    continuation on the reachable branch (wh-review-pattern-fixes.24).
    """

    def test_fixture_pattern_compiles_and_branches(self):
        """Fixture guard: Python's conditional semantics for the shape."""
        compiled = re.compile(CONDITIONAL_TAIL)
        assert compiled.fullmatch("say a foo")
        assert compiled.fullmatch("say look up x")
        assert compiled.fullmatch("say a look x") is None

    def test_classification_stays_greedy(self):
        assert has_greedy_tail(CONDITIONAL_TAIL) is True

    def test_transform_marks_greedy(self):
        _transformed, metadata = transform_pattern(CONDITIONAL_TAIL)
        assert metadata.get("is_greedy") is True

    def test_probe_source_closes_the_conditional(self):
        assert (
            greedy_tail_probe_source(CONDITIONAL_TAIL)
            == r"say (a )?(?(1)foo|look .+)"
        )

    def test_prefix_is_conservatively_empty(self):
        assert extract_literal_prefix(CONDITIONAL_TAIL) == ""

    def test_tail_in_yes_branch_also_empty(self):
        assert extract_literal_prefix(r"^say (a )?(?(1)look .+|foo)$") == ""

    def test_tail_nested_inside_conditional_also_empty(self):
        """The tail's own (?: group sits inside the conditional; the repair
        would still have to cut through the conditional."""
        assert (
            extract_literal_prefix(r"^say (a )?(?(1)x|(?:look .+))$") == ""
        )

    def test_conditional_closed_before_tail_keeps_prefix(self):
        """A conditional that CLOSES before the cut is balanced text the
        repair never touches; the prefix stays exact."""
        assert (
            extract_literal_prefix(r"^(a)?(?(1)b|c) say (.+)$")
            == r"(a)?(?(1)b|c) say"
        )


COMMENT_TAIL = r"^say (?:look(?# note (with pipe |) up .+)$"


class TestCommentGroupLexing:
    r"""Python ends a comment group (?#...) at the first BARE ')' -- no
    nesting, and an escape pair such as ``\)`` is one tokenizer token, so
    it does NOT end the comment (re/_parser.py; verified:
    re.compile(r'(?#a\)b)c') matches 'c', re.compile(r'x(?#ab\)y') raises
    'missing ), unterminated comment'). The repair helpers parsed (?#...)
    as ordinary nested parens, so ^say (?:look(?# note (with pipe |) up .+)$
    produced the garbage prefix 'say look note (with pipe |) up'. Comments
    are zero-width, so deleting them from extracted text is exact
    (wh-review-pattern-fixes.24).
    """

    def test_fixture_pattern_matches_without_its_comment_text(self):
        """Fixture guard: the comment ends at the ')' after 'pipe |', so
        the effective pattern is ^say (?:look up .+)$."""
        assert re.fullmatch(COMMENT_TAIL, "say look up x")
        assert re.fullmatch(COMMENT_TAIL, "say look note x") is None

    def test_classification_stays_greedy(self):
        assert has_greedy_tail(COMMENT_TAIL) is True

    def test_prefix_deletes_the_comment(self):
        assert extract_literal_prefix(COMMENT_TAIL) == "say look up"

    def test_balanced_comment_deleted_from_prefix(self):
        assert extract_literal_prefix(r"^say (?# skip )now (.+)$") == "say now"

    def test_escaped_paren_keeps_comment_open_no_span(self):
        r"""In ^say (?#c\).+)$ the comment swallows everything through the
        final ')': the '.+' is comment text, not a span."""
        assert has_greedy_tail(r"^say (?#c\).+)$") is False

    def test_escaped_paren_comment_prefix_empty(self):
        assert extract_literal_prefix(r"^say (?#c\).+)$") == ""

    def test_escaped_paren_comment_full_body(self):
        r"""The comment ends at the first bare ')', so the effective
        pattern is ^say $ -- re REQUIRES the trailing space and refuses
        'say'. The body keeps that space (wh-review-pattern-fixes.30;
        the old strip returned 'say' and the matchers held a buffer no
        token join could ever complete) and builds no matchers."""
        assert re.compile(r"^say (?#c\).+)$").fullmatch("say") is None
        assert re.compile(r"^say (?#c\).+)$").fullmatch("say ")
        assert extract_full_literal_body(r"^say (?#c\).+)$") == "say "
        assert build_literal_prefix_matchers("say ") == ()

    def test_escaped_backslash_then_paren_ends_comment(self):
        r"""``\\`` is one token (a literal backslash), so the ')' after it
        is bare and ends the comment: ^say (?#c\\).+$ HAS a real tail."""
        assert has_greedy_tail(r"^say (?#c\\).+$") is True
        assert extract_literal_prefix(r"^say (?#c\\).+$") == "say"


INITIAL_BRACKET = r"^do []a.+]+ say hello$"


class TestInitialBracketCharacterClass:
    r"""In Python re, ']' immediately after '[' or '[^' is a LITERAL class
    member; the class ends at the next bare ']' (re/_parser.py: ']' only
    closes a non-empty set). The scanner closed the class of
    ^do []a.+]+ say hello$ at its first ']', saw the class-internal '.+'
    as a span, and extract_full_literal_body returned '' -- so the bounded
    pattern got no literal_body_matchers (wh-review-pattern-fixes.24).
    """

    def test_fixture_pattern_matches(self):
        """Fixture guard: Python's initial-] lexing for the shape."""
        assert re.fullmatch(INITIAL_BRACKET, "do a say hello")

    def test_no_greedy_tail(self):
        assert has_greedy_tail(INITIAL_BRACKET) is False

    def test_no_probe_source(self):
        assert greedy_tail_probe_source(INITIAL_BRACKET) is None

    def test_not_marked_greedy(self):
        _transformed, metadata = transform_pattern(INITIAL_BRACKET)
        assert "is_greedy" not in metadata

    def test_literal_prefix_empty(self):
        assert extract_literal_prefix(INITIAL_BRACKET) == ""

    def test_full_body_extracted(self):
        assert extract_full_literal_body(INITIAL_BRACKET) == "do []a.+]+ say hello"

    def test_negated_initial_bracket_full_body(self):
        assert (
            extract_full_literal_body(r"^do [^]a.+]+ say hello$")
            == r"do [^]a.+]+ say hello"
        )

    def test_escaped_bracket_in_class_still_correct(self):
        r"""Guard: ``\]`` never closes a class; [\]a.+] was already lexed
        correctly and must stay bounded."""
        assert (
            extract_full_literal_body(r"^do [\]a.+]+ say hello$")
            == r"do [\]a.+]+ say hello"
        )

    def test_class_with_paren_before_real_tail_repair_lexes_class(self):
        """A '(' inside an initial-] class is a literal member; the repair
        must not count it as a group opening when it rebuilds the prefix
        before the REAL tail."""
        assert extract_literal_prefix(r"^do [](]+ (?:.+)$") == r"do [](]+"


# ============================================================================
# wh-review-pattern-fixes.27: syntax-aware numeric transform
# ============================================================================


class TestNumericTransformSyntax:
    r"""The numeric transform must be lexer-based, not textual
    (wh-review-pattern-fixes.27).

    The old pass counted every ``(`` not followed by ``?:`` as a capture
    and rewrote ``(\d+)`` / ``(?:\d+)`` shapes with a blind re.sub. That
    recorded wrong validation groups after lookarounds, rewrote character
    class content, skipped named numeric captures, and broadened
    non-capturing numeric groups with no validation metadata. The rebuilt
    transform walks the pattern with the shared lexer helpers and numbers
    groups the way re.compile does.
    """

    def test_lookahead_before_numeric_capture_gets_real_group_number(self):
        """The finding's reproduction: (?= opened a phantom capture, so
        the metadata said g2 while re.compile assigns g1."""
        transformed, meta = transform_pattern(r"^foo (?=bar )bar (\d+)$")
        assert transformed == rf"^foo (?=bar )bar ({_BODY})$"
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1

    def test_class_content_untouched_and_no_metadata(self):
        r"""``[(\d+)]`` holds class members, not a group: the old pass
        rewrote the class AND attached g1 with zero real captures."""
        transformed, meta = transform_pattern(r"^[(\d+)]$")
        assert transformed == r"^[(\d+)]$"
        assert "validation_group" not in meta

    def test_comment_content_untouched(self):
        r"""``(?# note \d+ )`` is comment text; only the real capture
        after it is transformed, and it is capture number 1."""
        transformed, meta = transform_pattern(r"^set (?# note \d+ )(\d+)$")
        assert transformed == rf"^set (?# note \d+ )({_BODY})$"
        assert meta["validation_group"] == "g1"

    def test_escaped_parens_around_digits_untouched(self):
        r"""``\(`` and ``\)`` are literal characters, not a group."""
        transformed, meta = transform_pattern(r"^go \(\d+\) now$")
        assert transformed == r"^go \(\d+\) now$"
        assert "validation_group" not in meta

    def test_named_numeric_capture_transformed_with_real_number(self):
        """A named numeric capture is a real capture: it must be widened
        so number words can match, and validated by its real number."""
        transformed, meta = transform_pattern(r"^set (?P<n>\d+) mode$")
        assert transformed == rf"^set (?P<n>{_BODY}) mode$"
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groupindex["n"] == 1

    def test_noncapturing_numeric_group_left_untransformed(self):
        r"""``(?:\d+)`` has no capture to validate: broadening it would
        admit arbitrary words, so it stays as written."""
        transformed, meta = transform_pattern(r"^set (?:\d+) mode$")
        assert transformed == r"^set (?:\d+) mode$"
        assert "validation_group" not in meta

    def test_plain_numeric_capture_unchanged(self):
        """The shipped-catalog shape keeps its exact prior behavior."""
        transformed, meta = transform_pattern(r"^item (\d+)$")
        assert transformed == rf"^item ({_BODY})$"
        assert meta["validation_group"] == "g1"

    def test_optional_second_capture_keeps_g2(self):
        """The docstring example: two plain captures, metadata g2, and
        the ``?`` quantifier survives."""
        transformed, meta = transform_pattern(
            r"(backspace|back space)\s*(\d+)?$"
        )
        assert transformed == rf"(backspace|back space)\s*({_BODY})?$"
        assert meta["validation_group"] == "g2"

    def test_noncapturing_prefix_keeps_g1(self):
        """The other docstring example: (?: does not consume a number."""
        transformed, meta = transform_pattern(r"(?:prefix )?(\d+) times")
        assert transformed == rf"(?:prefix )?({_BODY}) times"
        assert meta["validation_group"] == "g1"

    def test_mixed_group_kinds_number_correctly(self):
        """Non-capturing, capturing, lookahead, named, capturing: the
        named numeric capture is re.compile's group 2."""
        pattern = r"^(?:go )?(a|b) (?=x )x (?P<n>\d+) (c)$"
        transformed, meta = transform_pattern(pattern)
        assert transformed == rf"^(?:go )?(a|b) (?=x )x (?P<n>{_BODY}) (c)$"
        assert meta["validation_group"] == "g2"
        assert re.compile(transformed).groupindex["n"] == 2

    def test_first_of_two_numeric_captures_recorded(self):
        """Both numeric captures are widened; the metadata names the
        first, with its real number."""
        transformed, meta = transform_pattern(r"^(?=.)go (\d+) of (\d+)$")
        assert transformed == rf"^(?=.)go ({_BODY}) of ({_BODY})$"
        assert meta["validation_group"] == "g1"

    def test_recorded_group_exists_in_compiled_pattern(self):
        """The invariant validate_numeric depends on: the recorded number
        is a group re.compile actually assigns."""
        for pattern in [
            r"^foo (?=bar )bar (\d+)$",
            r"^set (?P<n>\d+) mode$",
            r"^(?:go )?(a|b) (?=x )x (?P<n>\d+) (c)$",
            r"^item (\d+)$",
        ]:
            transformed, meta = transform_pattern(pattern)
            group_num = int(meta["validation_group"][1:])
            assert re.compile(transformed).groups >= group_num


# ============================================================================
# Verbose-mode (?x) comments in the lexical model
# (wh-review-pattern-fixes.29)
# ============================================================================

#: Repro 1 (the bead's pattern, with the REAL newline the comment needs:
#: re's verbose-comment loop ends only at a bare newline character,
#: re/_parser.py:536-540, so the raw-string form with a backslash-n escape
#: does not even compile).
VERBOSE_NUMERIC = "^foo(?x: # (not a capture)\n) (\\d+)$"

#: Repro 2: the '.+' is comment text, the scoped group matches empty, and
#: re fullmatches "look up now".
VERBOSE_COMMENT_BODY = "^look(?x: # .+ comment\n) up now$"


class TestVerboseCommentLexing:
    """While the x flag is active, an unescaped '#' outside a character
    class starts a comment that runs to the next bare newline character.
    The shared lexical walk must skip it exactly as re.compile does:
    parens inside it are not captures, dots inside it are not greedy
    tails, and its text is never literal prefix/body text."""

    def test_re_ground_truth(self):
        """The empirical rules every assertion below keys off."""
        assert re.compile(VERBOSE_NUMERIC).groups == 1
        assert re.compile(VERBOSE_NUMERIC).fullmatch("foo 5")
        assert re.compile(VERBOSE_COMMENT_BODY).fullmatch("look up now")

    def test_commented_paren_not_counted_as_capture(self):
        """Repro 1: validation_group must be the REAL capture number g1,
        not g2 (the old count included the paren inside the comment)."""
        transformed, meta = transform_pattern(VERBOSE_NUMERIC)
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1
        assert transformed == f"^foo(?x: # (not a capture)\n) ({_BODY})$"

    def test_widened_group_still_validates(self):
        """The widened pattern accepts a number word in the real group."""
        transformed, meta = transform_pattern(VERBOSE_NUMERIC)
        match = re.compile(transformed, re.IGNORECASE).fullmatch("foo five")
        assert match is not None
        assert match.group(1) == "five"

    def test_commented_dot_quantifier_is_not_a_span(self):
        """Repro 2: the '.+' is comment text, not a greedy tail."""
        from speech.pattern_transform import _find_greedy_tail_spans
        assert _find_greedy_tail_spans(VERBOSE_COMMENT_BODY) == []
        assert has_greedy_tail(VERBOSE_COMMENT_BODY) is False

    def test_zero_width_verbose_scope_deleted_from_body(self):
        """The scoped group holds only ignored whitespace and a comment,
        so it matches the empty string and is deleted exactly: the body
        is the text the user must speak."""
        assert extract_full_literal_body(VERBOSE_COMMENT_BODY) == (
            "look up now"
        )

    def test_no_bogus_prefix_from_comment_text(self):
        """Repro 2's old prefix was the garbage 'look #'."""
        assert extract_literal_prefix(VERBOSE_COMMENT_BODY) == ""

    def test_zero_width_verbose_scope_deleted_from_prefix(self):
        """A zero-width verbose scope before a real tail is deleted from
        the cut text exactly like a (?#...) comment."""
        pattern = "^say (?x: # c\n)(.+)$"
        assert re.compile(pattern).fullmatch("say hello")
        assert extract_literal_prefix(pattern) == "say"

    def test_live_verbose_scope_is_conservative(self):
        """Literal text lexed inside an active-verbose scope relies on
        whitespace re ignores, so extraction falls back to ''."""
        pattern = "^(?x:look) up now$"
        assert re.compile(pattern).fullmatch("look up now")
        assert extract_full_literal_body(pattern) == ""
        greedy = "^(?x:say #c\n) (.+)$"
        assert re.compile(greedy).fullmatch("say x")
        assert extract_literal_prefix(greedy) == ""

    def test_quantified_zero_width_scope_is_conservative(self):
        """A quantifier applies to the (empty) GROUP, unlike a (?#...)
        comment, so deleting the group would re-attach the quantifier to
        the item before it. Probe: 'a(?x: )?' matches 'a' but not ''."""
        assert re.compile("a(?x: )?").fullmatch("a")
        assert re.compile("a(?x: )?").fullmatch("") is None
        pattern = "^look up(?x: # c\n)? now$"
        assert re.compile(pattern).fullmatch("look up now")
        assert extract_full_literal_body(pattern) == ""

    def test_tail_inside_verbose_scope_keeps_greedy(self):
        """A quantified dot inside a verbose scope (outside any comment)
        is a real greedy tail; the spaced form '. +' quantifies the dot
        because verbose mode ignores the space between them."""
        assert has_greedy_tail("^say (?x:.+)$") is True
        assert re.compile("(?x:a . +)").fullmatch("abcd")
        assert has_greedy_tail("^say (?x:. +)$") is True

    def test_escaped_hash_is_literal(self):
        r"""'\#' never starts a comment, so the capture after it counts."""
        pattern = "(?x:a \\# (\\d+))"
        assert re.compile(pattern).fullmatch("a#5")
        transformed, meta = transform_pattern(pattern)
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1

    def test_class_hash_is_literal(self):
        """'#' inside a character class is a class member, not a comment."""
        pattern = "(?x:[#] (\\d+))"
        assert re.compile(pattern).fullmatch("# 5") is None  # space ignored
        assert re.compile(pattern).fullmatch("#5")
        transformed, meta = transform_pattern(pattern)
        assert meta["validation_group"] == "g1"

    def test_hash_without_verbose_is_literal(self):
        """Outside any verbose scope '#' is an ordinary character and the
        parens after it count normally."""
        transformed, meta = transform_pattern("^a # (\\d+)$")
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1

    def test_global_verbose_flag_group(self):
        """(?x) at the pattern start puts the whole pattern in verbose
        mode: the comment paren is not a capture there either."""
        pattern = "(?x)^del (\\d+) # (fake)\n$"
        assert re.compile(pattern).groups == 1
        transformed, meta = transform_pattern(pattern)
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1

    def test_backslash_n_escape_does_not_end_comment(self):
        r"""The raw-string form of the bead's repro: '\n' is an escape
        PAIR, one token to re's tokenizer, so the comment never ends and
        re.compile refuses the pattern. No metadata may be recorded for
        a pattern whose capture number the walk cannot prove against
        re.compile."""
        pattern = r"^foo(?x: # (not a capture)\n) (\d+)$"
        try:
            re.compile(pattern)
            raise AssertionError("expected re.error")
        except re.error:
            pass
        transformed, meta = transform_pattern(pattern)
        assert "validation_group" not in meta
        assert transformed == pattern

    def test_walk_recompile_disagreement_skips_transform(self):
        r"""The recompile parity gate: the walk counts two captures in
        the unbalanced ``((\d+)`` and would widen the inner one, but
        re.compile refuses the pattern, so the capture number is
        unprovable and the transform must be skipped entirely -- no
        widening, no metadata (wrong metadata made validate_numeric skip
        the check and accept any word)."""
        pattern = r"((\d+)"
        try:
            re.compile(pattern)
            raise AssertionError("expected re.error")
        except re.error:
            pass
        transformed, meta = transform_pattern(pattern)
        assert transformed == pattern
        assert "validation_group" not in meta

    def test_minus_x_scope_restores_normal_lexing(self):
        """(?-x:...) turns verbose off inside an outer verbose scope, so
        a '#' there is literal again."""
        pattern = "(?x:a(?-x: # (\\d+))b)"
        assert re.compile(pattern).groups == 1
        transformed, meta = transform_pattern(pattern)
        assert meta["validation_group"] == "g1"
        assert re.compile(transformed).groups == 1


# ============================================================================
# Exact whitespace separators in literal prefix/body extraction
# (wh-review-pattern-fixes.30)
# ============================================================================


class TestWhitespaceExactSeparators:
    r"""The buffer is always ' '.join(tokens): single interior spaces, never
    leading, trailing, or doubled ones. A generic strip() erased whitespace
    the regex requires, so the matchers accepted buffers that can NEVER
    fullmatch any token join and ordinary dictation was held until the
    command timeout. Exact rule: remove only the ONE separator space (or
    trailing \s+/\s* escape) between the literal opening and the tail;
    refuse to build matchers for text whose remaining whitespace a token
    join cannot supply."""

    def test_double_space_prefix_keeps_second_space(self):
        """One space is the separator before the tail; the second one is
        literal text re requires."""
        assert extract_literal_prefix(r"^look up  (.+)$") == "look up "

    def test_double_space_prefix_builds_no_matchers(self):
        """No token join carries a doubled space, so no buffer may be
        held: refuse every matcher, truncations included."""
        prefix = extract_literal_prefix(r"^look up  (.+)$")
        assert build_literal_prefix_matchers(prefix) == ()
        assert re.compile(r"^look up  (.+)$").fullmatch(
            "look up later") is None

    def test_trailing_space_body_unstripped(self):
        """The body of ^look up now $ ends with a required space."""
        assert extract_full_literal_body(r"^look up now $") == "look up now "

    def test_trailing_space_body_builds_no_matchers(self):
        body = extract_full_literal_body(r"^look up now $")
        assert build_literal_prefix_matchers(body) == ()
        assert re.compile(r"^look up now $").fullmatch("look up now") is None

    def test_leading_space_prefix_unstripped(self):
        assert extract_literal_prefix(r"^ look up (.+)$") == " look up"

    def test_leading_space_prefix_builds_no_matchers(self):
        prefix = extract_literal_prefix(r"^ look up (.+)$")
        assert build_literal_prefix_matchers(prefix) == ()
        assert re.compile(r"^ look up (.+)$").fullmatch(
            "look up later") is None

    def test_single_separator_prefix_unchanged(self):
        """The normal shape: exactly the one separator is removed."""
        assert extract_literal_prefix(r"^look up (.+)$") == "look up"
        matchers = build_literal_prefix_matchers("look up")
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look")
        assert not _accepts(matchers, "look sideways")

    def test_trailing_escape_separator_removed(self):
        r"""The shipped click prefix is 'click\s+': the trailing escape is
        the separator and one space satisfies it, so 'click' stays the
        probe text (wh-click-number-dictation)."""
        matchers = build_literal_prefix_matchers(r"click\s+")
        assert [m.pattern for m in matchers] == ["^click$"]

    def test_interior_escape_becomes_single_space(self):
        matchers = build_literal_prefix_matchers(r"look\s+up")
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look")

    def test_expanded_variant_separator_removed(self):
        """Group expansion exposes the separator inside the group: the
        variants of 'look (?:the )?' end mid-group and both openings stay
        continuable (wh-review-pattern-fixes.9 machinery preserved)."""
        matchers = build_literal_prefix_matchers("look (?:the )?")
        assert _accepts(matchers, "look the")
        assert _accepts(matchers, "look")

    def test_expanded_variant_double_space_refused(self):
        """A doubled space survives expansion and refuses its variants."""
        prefix = extract_literal_prefix(r"^look up  (?:the )?(.+)$")
        assert prefix == "look up  (?:the )?"
        assert build_literal_prefix_matchers(prefix) == ()

    def test_tab_whitespace_refused(self):
        """A literal tab is whitespace no token join can supply."""
        assert build_literal_prefix_matchers("look\tup") == ()

    def test_full_body_with_normal_spacing_unchanged(self):
        body = extract_full_literal_body(r"^push to talk mode$")
        assert body == "push to talk mode"
        matchers = build_literal_prefix_matchers(body)
        assert _accepts(matchers, "push to talk mode")
        assert _accepts(matchers, "push to")


# ============================================================================
# Every safe \s separator spelling builds intermediate truncations
# (wh-review-pattern-fixes.34)
# ============================================================================


class TestEscapedSpaceSeparatorSpellings:
    r"""A ``\s`` spelling that can match exactly ONE space is a word
    separator for the single-space token join, so it must normalize to a
    plain space BEFORE the word split -- otherwise a three-word prefix
    such as ``look\s?up\s?now`` builds no intermediate truncation, the
    probe rejects the buffer ['look', 'up'], and the router finalizes a
    speakable command opening as dictation (wh-review-pattern-fixes.34).

    The safe set: ``\s``, ``\s?``, ``\s+``, ``\s*``, ``\s{1}``, and any
    bounded ``\s{m,n}`` with m <= 1 <= n. A spelling that requires two or
    more whitespace characters (``\s{2}``, ``\s{2,}``) can never match a
    join and must refuse to build, per the round-10 exactness rule
    (wh-review-pattern-fixes.30). ``\s{0}`` matches only the empty
    string, so its words join directly. A ``\s`` inside a character
    class is class content, never a separator.
    """

    # -- the filed repro: \s? in a three-word prefix --

    def test_optional_escape_three_word_prefix_continues(self):
        pattern = r"^look\s?up\s?now (.+)$"
        prefix = extract_literal_prefix(pattern)
        matchers = build_literal_prefix_matchers(prefix)
        assert re.compile(pattern).fullmatch("look up now value")
        assert _accepts(matchers, "look")
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")
        assert not _accepts(matchers, "look sideways")

    def test_bare_escape_three_word_prefix_continues(self):
        prefix = extract_literal_prefix(r"^look\sup\snow (.+)$")
        matchers = build_literal_prefix_matchers(prefix)
        assert _accepts(matchers, "look")
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    def test_bounded_one_escape_three_word_prefix_continues(self):
        prefix = extract_literal_prefix(r"^look\s{1}up\s{1}now (.+)$")
        matchers = build_literal_prefix_matchers(prefix)
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    def test_bounded_range_escape_three_word_prefix_continues(self):
        prefix = extract_literal_prefix(r"^look\s{1,2}up\s{1,2}now (.+)$")
        matchers = build_literal_prefix_matchers(prefix)
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    def test_zero_min_bounded_range_escape_continues(self):
        prefix = extract_literal_prefix(r"^look\s{0,3}up\s{0,3}now (.+)$")
        matchers = build_literal_prefix_matchers(prefix)
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    # -- impossible whitespace refuses, truncations included --

    def test_two_space_bounded_escape_refused(self):
        prefix = extract_literal_prefix(r"^look\s{2}up now (.+)$")
        assert build_literal_prefix_matchers(prefix) == ()

    def test_two_space_open_escape_refused(self):
        prefix = extract_literal_prefix(r"^look\s{2,}up now (.+)$")
        assert build_literal_prefix_matchers(prefix) == ()

    # -- trailing separator spellings are the pre-tail separator --

    def test_trailing_optional_escape_separator_removed(self):
        matchers = build_literal_prefix_matchers(r"look\s?up\s?now\s?")
        assert matchers[0].pattern == "^look up now$"
        assert _accepts(matchers, "look up")

    def test_trailing_bare_escape_separator_removed(self):
        matchers = build_literal_prefix_matchers(r"look\sup\snow\s")
        assert matchers[0].pattern == "^look up now$"
        assert _accepts(matchers, "look up")

    # -- the literal_body_matchers path uses the same builder --

    def test_nongreedy_body_optional_escape_continues(self):
        body = extract_full_literal_body(r"^look\s?up\s?now$")
        matchers = build_literal_prefix_matchers(body)
        assert _accepts(matchers, "look")
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    def test_nongreedy_body_bare_escape_continues(self):
        body = extract_full_literal_body(r"^look\sup\snow$")
        matchers = build_literal_prefix_matchers(body)
        assert _accepts(matchers, "look up")
        assert _accepts(matchers, "look up now")

    # -- bounds of the normalization --

    def test_zero_only_escape_joins_words(self):
        r"""\s{0} matches only the empty string: the words concatenate,
        which is exactly the text re accepts."""
        matchers = build_literal_prefix_matchers(r"look\s{0}up now")
        assert matchers[0].pattern == "^lookup now$"
        assert _accepts(matchers, "lookup")
        assert not _accepts(matchers, "look up")

    def test_class_space_escape_is_not_a_separator(self):
        r"""A \s inside a character class is class content; rewriting it
        would change what the matcher accepts (the shipped right/double
        click patterns carry [\s-]+)."""
        matchers = build_literal_prefix_matchers(r"grab[\s-]all\s+now")
        assert matchers[0].pattern == r"^grab[\s-]all now$"

    @pytest.mark.parametrize("first", ["right", "double"])
    def test_class_separator_keeps_the_grouped_first_word(self, first):
        matchers = build_literal_prefix_matchers(r"(right|double)[\s-]+click")
        assert _accepts(matchers, first)
        assert _accepts(matchers, first + " click")
        assert _accepts(matchers, first + "-click")
        assert not _accepts(matchers, first + "click")

    @pytest.mark.parametrize("separator", [r"[\s-]", r"[ -]+", r"[\x20-]?", r"[^\S\r\n]{1,3}"])
    def test_class_separator_creates_each_complete_word_boundary(self, separator):
        matchers = build_literal_prefix_matchers("turn" + separator + "back" + separator + "now")
        assert _accepts(matchers, "turn")
        assert _accepts(matchers, "turn back")
        assert _accepts(matchers, "turn back now")
        assert not _accepts(matchers, "tur")
        assert not _accepts(matchers, "turn b")

    @pytest.mark.parametrize("separator", [r"[-]", r"[^\s]", r"[\t]", r"[\s-]{2}", r"[\s-]{0}"])
    def test_class_without_a_single_space_does_not_hold_a_word(self, separator):
        matchers = build_literal_prefix_matchers("turn" + separator + "back")
        assert not _accepts(matchers, "turn")

    def test_class_boundaries_keep_prior_separator_regexes(self):
        matchers = build_literal_prefix_matchers(r"turn[\s-]+back[\s-]+now")
        assert _accepts(matchers, "turn-back")
        assert _accepts(matchers, "turn-back now")
        assert _accepts(matchers, "turn back-now")

    @pytest.mark.parametrize("separator", [r"[\s-]+ ", r"[\s-][\s-]", r"[\s-]+[\s-]{2}"])
    def test_adjacent_class_separators_cannot_invent_a_one_space_join(self, separator):
        matchers = build_literal_prefix_matchers("turn" + separator + "back")
        assert not _accepts(matchers, "turn")

    def test_class_separator_matcher_count_keeps_the_existing_bound(self):
        words = [f"token{i}" for i in range(MAX_PREFIX_MATCHERS + 10)]
        matchers = build_literal_prefix_matchers(r"[\s-]+".join(words))
        assert len(matchers) == MAX_PREFIX_MATCHERS
        assert _accepts(matchers, words[0])
        assert _accepts(matchers, "-".join(words))

    def test_trailing_class_separator_keeps_the_word_before_a_tail(self):
        matchers = build_literal_prefix_matchers(extract_literal_prefix(r"^turn[\s-]+(.+)$"))
        assert _accepts(matchers, "turn")

    def test_escaped_backslash_before_s_is_not_a_separator(self):
        r"""\\s+ is a literal backslash then one-or-more 's': not
        whitespace, so it stays exactly as written."""
        matchers = build_literal_prefix_matchers("do\\\\s+it now")
        assert matchers[0].pattern == "^do\\\\s+it now$"

    # -- guards: the shapes that already worked stay byte-identical --

    def test_plain_space_prefix_unchanged(self):
        prefix = extract_literal_prefix(r"^look up now (.+)$")
        matchers = build_literal_prefix_matchers(prefix)
        assert [m.pattern for m in matchers] == [
            "^look up now$", "^look$", "^look up$"]

    def test_shipped_click_prefix_unchanged(self):
        matchers = build_literal_prefix_matchers(r"click\s+")
        assert [m.pattern for m in matchers] == ["^click$"]

    def test_plus_escape_prefix_unchanged(self):
        matchers = build_literal_prefix_matchers(r"look\s+up\s+now")
        assert [m.pattern for m in matchers] == [
            "^look up now$", "^look$", "^look up$"]


# ============================================================================
# A Pattern Manager phrase: an escaped space between its words
# (wh-multiword-phrase-replacement)
# ============================================================================


class TestMultiwordPhraseEscapedSpace:
    r"""An escaped space (a backslash, then a space) between two literal
    words matches exactly one space, which is the join the probes test. It
    is therefore a word separator, and each opening of a phrase that uses
    it needs its own truncation matcher.

    Pattern Manager simple mode is the producer. ``generate_expression``
    passes each phrase through ``re.escape``, which writes every internal
    space as ``\ ``, so the phrase "paste my signature" is saved as
    ``\b(?:paste\ my\ signature)\b``. With no matcher for the opening
    "paste my", the router finalizes that buffer as dictation at word
    two when "paste" follows another word (it also opens the shipped
    command ``^paste(?: that| here)?$``, whole_utterance_only in
    speech/config/patterns.toml), and a pause after "here we" types the
    words instead of holding them for "go"
    (wh-multiword-phrase-replacement, found as
    wh-codex-merge-audit.15.1.2). A two-word phrase is not affected,
    because a one-word buffer of a multi-word pattern always continues.

    Measured at 88e6f6ff: ``_prefix_truncation_offsets`` skips each
    escape pair whole, so for every shape in the first three tests the
    builder returns only the full matcher. The last two tests pin shapes
    that must not gain an opening, or must keep the openings they have:
    an escaped space that cannot give exactly one space, two escaped
    spaces in a row, and an escaped backslash followed by a real space.
    Both pass at 88e6f6ff.
    """

    @pytest.mark.parametrize("pattern_type", ["replacement", "command"])
    def test_multiword_phrase_matchers_hold_each_opening(self, pattern_type):
        """Both anchorings of a saved phrase share one body, and that body
        needs a matcher for each opening: "paste" and "paste my"."""
        expression = generate_expression(["paste my signature"], pattern_type)
        body = extract_full_literal_body(expression)
        # The producer's spelling, pinned so that this test keeps testing
        # the escaped space the Pattern Manager writes.
        assert body == r"(?:paste\ my\ signature)", body
        matchers = build_literal_prefix_matchers(body)
        assert [m.pattern for m in matchers] == [
            r"^paste\ my\ signature$", "^paste$", r"^paste\ my$"]
        for text in ("paste", "paste my", "paste my signature"):
            assert _accepts(matchers, text), text
        for text in ("paste me", "paste my sig", "pastemy"):
            assert not _accepts(matchers, text), text

    def test_multiword_phrase_every_alternative_holds_its_openings(self):
        """A phrase list is saved as one alternation. Each alternative needs
        its own openings, and a buffer that leaves every alternative must
        still be refused."""
        expression = generate_expression(
            ["here we go", "let us go now"], "replacement")
        matchers = build_literal_prefix_matchers(
            extract_full_literal_body(expression))
        patterns = [m.pattern for m in matchers]
        for text in ("here", "here we", "here we go",
                     "let", "let us", "let us go", "let us go now"):
            assert _accepts(matchers, text), (text, patterns)
        for text in ("here they", "here we are", "let go", "let us stay"):
            assert not _accepts(matchers, text), (text, patterns)

    @pytest.mark.parametrize(
        "separator", [r"\ ?", r"\ +", r"\ *", r"\ {1}", r"\ {0,2}", r"\ +?"])
    def test_multiword_phrase_quantified_escaped_space_holds_each_word(
            self, separator):
        """An Advanced-mode pattern can put a quantifier on the escaped
        space. Each spelling here can match exactly one space, so it
        separates words the same way the bare escaped space does."""
        matchers = build_literal_prefix_matchers(
            "turn" + separator + "back" + separator + "now")
        patterns = [m.pattern for m in matchers]
        assert _accepts(matchers, "turn"), patterns
        assert _accepts(matchers, "turn back"), patterns
        assert _accepts(matchers, "turn back now"), patterns
        assert not _accepts(matchers, "tur"), patterns
        assert not _accepts(matchers, "turn b"), patterns

    @pytest.mark.parametrize("separator", [
        pytest.param(r"\ {2}", id="exactly-two"),
        pytest.param(r"\ {0}", id="zero-only"),
        pytest.param(r"\ \ ", id="two-in-a-row"),
        pytest.param(r"\ {2,}", id="two-or-more"),
    ])
    def test_multiword_phrase_escaped_space_without_one_space_holds_nothing(
            self, separator):
        """No single-space join can satisfy these spellings: they need two
        spaces, or no space at all. A matcher for "turn" would hold a
        buffer that the pattern can never complete through a one-space
        join, which is the rule the class separators already follow
        (test_class_without_a_single_space_does_not_hold_a_word)."""
        matchers = build_literal_prefix_matchers("turn" + separator + "back")
        assert [m.pattern for m in matchers] == ["^turn" + separator + "back$"]
        assert not _accepts(matchers, "turn")

    def test_multiword_phrase_escaped_backslash_then_space_stays_a_literal_backslash(
            self):
        r"""``do\\ it now`` is "do", a literal backslash, then a real space.
        The escaped backslash must be read as one token, so the real space
        after it is the separator. Reading the second backslash and the
        space as an escaped space would cut the escaped backslash in half
        and leave a dangling backslash that escapes the matcher's closing
        anchor."""
        matchers = build_literal_prefix_matchers(r"do\\ it now")
        assert [m.pattern for m in matchers] == [
            r"^do\\ it now$", r"^do\\$", r"^do\\ it$"]
        assert _accepts(matchers, "do\\ it")
        assert not _accepts(matchers, "do it")


# ============================================================================
# An inline comment group between a dot and its quantifier
# (wh-review-pattern-fixes.43)
# ============================================================================


COMMENT_QUANTIFIED_TAIL = r"^say .(?# note)+$"


class TestCommentBetweenDotAndQuantifier:
    r"""A ``(?#...)`` comment is zero-width, and re attaches the quantifier
    that follows it to the atom BEFORE it. So the ``+`` in
    ``^say .(?# note)+$`` quantifies the dot: the pattern is a real greedy
    tail that consumes the rest of the utterance.

    The scanner skipped a comment group correctly while it advanced its own
    index, but the look-ahead it runs after a ``.`` skipped only verbose
    whitespace and verbose ``#`` comments before it tested for ``*`` or
    ``+``. The pattern therefore had no span: no probe source, no
    is_greedy metadata, no literal prefix, and
    ``extract_full_literal_body`` handed the catalog the raw regex text
    ``say .+`` as a "literal" body (wh-review-pattern-fixes.43).
    """

    def test_re_ground_truth(self):
        """The empirical rule every assertion below keys off: the '+'
        quantifies the dot across the comment."""
        assert re.fullmatch(COMMENT_QUANTIFIED_TAIL, "say hello")
        assert re.fullmatch(COMMENT_QUANTIFIED_TAIL, "say ") is None

    def test_plus_form_is_greedy(self):
        assert has_greedy_tail(COMMENT_QUANTIFIED_TAIL) is True

    def test_plus_form_probe_source(self):
        assert (
            greedy_tail_probe_source(COMMENT_QUANTIFIED_TAIL)
            == r"say .(?# note)+"
        )

    def test_plus_form_transform_metadata(self):
        _transformed, metadata = transform_pattern(COMMENT_QUANTIFIED_TAIL)
        assert metadata["is_greedy"] is True
        assert metadata["literal_prefix"] == "say"

    def test_plus_form_prefix_drops_the_comment(self):
        assert extract_literal_prefix(COMMENT_QUANTIFIED_TAIL) == "say"

    def test_plus_form_has_no_full_body(self):
        r"""The pattern owns a greedy tail, so the two extractors stay
        disjoint. The old answer ``say .+`` was regex text compiled as a
        body matcher, which accepted every buffer that starts 'say '."""
        assert extract_full_literal_body(COMMENT_QUANTIFIED_TAIL) == ""

    def test_star_form_is_greedy(self):
        pattern = r"^say .(?# note)*$"
        assert re.fullmatch(pattern, "say hello")
        assert has_greedy_tail(pattern) is True
        assert extract_literal_prefix(pattern) == "say"

    def test_escaped_close_paren_in_comment_is_greedy(self):
        r"""``\)`` is one tokenizer token, so the comment runs on to the
        BARE ')' and the '+' after it still quantifies the dot."""
        pattern = r"^say .(?# a\)b)+$"
        assert re.fullmatch(pattern, "say hello")
        assert has_greedy_tail(pattern) is True
        assert extract_literal_prefix(pattern) == "say"

    def test_adjacent_comment_groups_are_greedy(self):
        """Two comments in a row are both zero-width, so the '+' still
        quantifies the dot."""
        pattern = r"^say .(?# a)(?# b)+$"
        assert re.fullmatch(pattern, "say hello")
        assert has_greedy_tail(pattern) is True
        assert extract_literal_prefix(pattern) == "say"

    def test_comment_with_verbose_whitespace_is_greedy(self):
        """Under an active x flag re ignores whitespace as well, so a
        comment group mixed with spaces still leaves the '+' on the dot."""
        pattern = "^say (?x:.(?# c) +)$"
        assert re.fullmatch(pattern, "say hello")
        assert has_greedy_tail(pattern) is True

    def test_optional_quantifier_after_comment_is_not_a_tail(self):
        """'?' is not a greedy tail. The look-ahead tests only '*' and
        '+', and that bound does not change."""
        pattern = r"^say .(?# c)?$"
        assert re.fullmatch(pattern, "say h")
        assert has_greedy_tail(pattern) is False

    def test_literal_space_before_comment_is_not_a_tail(self):
        """Outside verbose mode the space is a real atom, so the '+'
        quantifies the SPACE, not the dot. Probe: the pattern refuses
        'say hello' and accepts 'say x  '."""
        pattern = r"^say . (?# c)+$"
        assert re.fullmatch(pattern, "say hello") is None
        assert re.fullmatch(pattern, "say x  ")
        assert has_greedy_tail(pattern) is False

    def test_dot_inside_a_comment_is_still_not_a_tail(self):
        """The class this fix covers is a dot BEFORE a comment. A dot
        INSIDE one stays comment text (wh-review-pattern-fixes.24)."""
        assert has_greedy_tail(r"^say (?#c\).+)$") is False


# ============================================================================
# Nested empty verbose scopes are provably zero-width
# (wh-review-pattern-fixes.44)
# ============================================================================


NESTED_SCOPE_GREEDY = "^look(?x:(?x: # nested comment\n)) up (.+)$"
NESTED_SCOPE_BOUNDED = "^look(?x:(?x: # nested comment\n)) up now$"


class TestNestedZeroWidthVerboseScopes:
    r"""``_delete_zero_width_verbose_scopes`` deletes an unquantified
    ``(?x:...)`` whose content is only ignored whitespace and comments,
    because such a group matches the empty string exactly. Its scan
    treated every nested ``(`` as live content, so one scope inside
    another lost both extractions (wh-review-pattern-fixes.44).

    The proof is now recursive over exactly the grammar that was already
    proven safe: ignored verbose whitespace, verbose ``#`` comments,
    ``(?#...)`` comments, and nested UNQUANTIFIED scoped groups that turn
    the x flag on and are themselves provably empty. Every other nested
    construct keeps the conservative '' answer -- a capturing or
    non-capturing group, an assertion, a quantified scope, literal text,
    a character class, an escape pair, and any malformed scope.
    """

    def test_re_ground_truth(self):
        """Both nested forms compile and match; the scopes consume
        nothing."""
        assert re.fullmatch(NESTED_SCOPE_GREEDY, "look up value")
        assert re.fullmatch(NESTED_SCOPE_BOUNDED, "look up now")

    def test_nested_scope_greedy_prefix(self):
        assert extract_literal_prefix(NESTED_SCOPE_GREEDY) == "look up"

    def test_nested_scope_bounded_body(self):
        assert extract_full_literal_body(NESTED_SCOPE_BOUNDED) == "look up now"

    def test_single_level_control_unchanged(self):
        """The single-level shape wh-review-pattern-fixes.29 fixed keeps
        its exact answers."""
        assert (
            extract_literal_prefix("^look(?x: # comment\n) up (.+)$")
            == "look up"
        )
        assert (
            extract_full_literal_body("^look(?x: # comment\n) up now$")
            == "look up now"
        )

    def test_nested_comment_group_body(self):
        """A ``(?#...)`` comment inside the nested scope is content the
        proof already accepts."""
        pattern = "^look(?x:(?x:(?# inner))) up now$"
        assert re.fullmatch(pattern, "look up now")
        assert extract_full_literal_body(pattern) == "look up now"

    def test_three_levels_deep_body(self):
        pattern = "^look(?x:(?x:(?x: \t))) up now$"
        assert re.fullmatch(pattern, "look up now")
        assert extract_full_literal_body(pattern) == "look up now"

    def test_nested_scope_greedy_matchers_accept_the_opening(self):
        """End to end: the prefix builds the truncation matchers the
        router's probe reads."""
        prefix = extract_literal_prefix(NESTED_SCOPE_GREEDY)
        matchers = build_literal_prefix_matchers(prefix)
        assert _accepts(matchers, "look")
        assert _accepts(matchers, "look up")
        assert not _accepts(matchers, "look sideways")

    # -- the conservative answers that must NOT change --

    def test_live_nested_scope_stays_conservative(self):
        """Literal text inside the nested scope: no textual extraction is
        safe, so both extractors keep returning ''."""
        bounded = "^look(?x:(?x:up)) now$"
        greedy = "^look(?x:(?x:up)) (.+)$"
        assert re.fullmatch(bounded, "lookup now")
        assert re.fullmatch(greedy, "lookup x")
        assert extract_full_literal_body(bounded) == ""
        assert extract_literal_prefix(greedy) == ""

    def test_quantified_nested_scope_stays_conservative(self):
        """A quantifier on the nested scope is unproven content."""
        pattern = "^look(?x:(?x: )?) up now$"
        assert re.fullmatch(pattern, "look up now")
        assert extract_full_literal_body(pattern) == ""

    def test_nested_negative_lookahead_stays_conservative(self):
        r"""The load-bearing negative control. Under the inherited x flag
        ``(?! )`` is ``(?!)`` -- a negative lookahead on the empty
        pattern, which ALWAYS fails, so the whole pattern matches
        nothing. Deleting it would hand the router a body for a command
        the user can never speak."""
        pattern = "^look(?x:(?! )) up now$"
        assert re.fullmatch(pattern, "look up now") is None
        assert extract_full_literal_body(pattern) == ""

    def test_nested_capturing_group_stays_conservative(self):
        """A capture is a real group node, not a scoped flag group."""
        pattern = "^look(?x:( )) up now$"
        assert re.fullmatch(pattern, "look up now")
        assert extract_full_literal_body(pattern) == ""

    def test_nested_noncapturing_group_stays_conservative(self):
        pattern = "^look(?x:(?: )) up now$"
        assert re.fullmatch(pattern, "look up now")
        assert extract_full_literal_body(pattern) == ""

    def test_nested_x_off_scope_stays_conservative(self):
        r"""``(?-x: )`` turns verbose OFF, so its space is a REQUIRED
        literal the user must speak. Deleting it would drop a character
        re demands."""
        pattern = "^look(?x:(?-x: )) up now$"
        assert re.fullmatch(pattern, "look  up now")
        assert re.fullmatch(pattern, "look up now") is None
        assert extract_full_literal_body(pattern) == ""

    def test_nested_class_stays_conservative(self):
        pattern = "^look(?x:(?x:[ ])) up now$"
        assert re.fullmatch(pattern, "look up now") is None
        assert extract_full_literal_body(pattern) == ""

    def test_unclosed_nested_scope_stays_conservative(self):
        """A malformed scope never compiles, and the walk must not run
        past the end of the text guessing at it."""
        assert extract_full_literal_body("^look(?x:(?x: ) up now$") == ""


class TestTheWidenedBodyCannotRunAwayInsideACallersRepeat:
    """codex finding wh-number-words-one-parser.1.3.

    The widened body carries its own multi-word tail joined by a space.
    When a user writes an Advanced expression that puts the numeric
    capture inside a repeat whose separator is ALSO a space, a run of
    spoken words can be split between the two repeats in exponentially
    many ways, and a non-matching input makes Python try all of them.

    The save-time probe corpus cannot cover this: every probe is a fixed
    string, and any literal prefix the user writes ("^one") keeps a fixed
    probe from ever reaching the word branch. The body itself therefore
    has to be unambiguous, which is why its multi-word tail is atomic.
    """

    # Far above a real utterance and far below the unfixed body's cost.
    # The runtime does NOT bound a match: pattern_matcher.py calls
    # fullmatch and search directly, so the 0.25s in safe_regex belongs to
    # the save-time probe alone and there is no runtime ceiling to sit
    # under. The unfixed body needed over twelve seconds for this input,
    # so this budget is roughly a sixth of it.
    _BUDGET_SECONDS = 2.0

    @staticmethod
    def _runaway_input(token_count: int) -> str:
        """A word run that cannot match, so every partition is tried."""
        return "one " + " ".join(["one"] * token_count) + " !"

    def test_a_numeric_capture_inside_a_repeat_does_not_run_away(self):
        """The exact expression codex reported, at the size it measured."""
        transformed = transform_pattern(r"^one(?: (\d+))+$")
        if isinstance(transformed, tuple):
            transformed = transformed[0]

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_the_runaway_does_not_return_with_more_words(self):
        """Backtracking that is merely slow still fails at a larger size.

        A fix that only moved the cliff would pass the test above and fail
        here, because each extra token multiplied the old body's work.
        """
        transformed = transform_pattern(r"^one(?: (\d+))+$")
        if isinstance(transformed, tuple):
            transformed = transformed[0]

        result = match_bounded(
            transformed,
            self._runaway_input(36),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_a_capture_that_repeats_itself_is_not_the_same_hazard(self):
        """`(\\d+)+` looks like the dangerous shape and is not.

        The widening treats only an ENCLOSING repeat as dangerous, and
        this pins why that is enough. A repeat carrying no separator of
        its own cannot take a second iteration in spoken text, because
        the body can never start with a separator -- so there is no way
        to divide a word run across its iterations. Measured with the
        plain body at 12, 16, 20 and 24 words: 0.000s for `(BODY)+`,
        `(BODY)*` and `(BODY){1,5}`, against 12.363s for `(?: (BODY))+`.

        The assertion leads with the property rather than the timing,
        because the timing follows from it and a timing test alone would
        pass for a shape that was never slow.
        """
        assert re.compile(_BODY).match(" one") is None, (
            "the body must not match a leading separator: if it ever can, "
            "a capture's own quantifier becomes the same hazard as an "
            "enclosing repeat and the widening has to treat it as one"
        )
        assert re.compile(_BODY).match("-one") is None

        transformed, _ = transform_pattern(r"^one (\d+)+$")

        result = match_bounded(
            transformed,
            self._runaway_input(30),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_the_atomic_tail_still_reads_a_real_multi_word_count(self):
        """The guard above must not have been bought by refusing counts.

        An atomic group takes its tokens and never gives one back, so this
        pins that the body still matches the phrase it exists to match.
        """
        transformed = transform_pattern(r"^delete\s*(\d+)?$")
        if isinstance(transformed, tuple):
            transformed = transformed[0]

        match = re.compile(transformed, re.IGNORECASE).match(
            "delete one hundred and twenty three"
        )

        assert match is not None
        assert match.group(1) == "one hundred and twenty three"
class TestACountWordFollowerSurvivesTheWidening:
    """codex finding wh-number-words-one-parser.1.6.

    The .1.3 fix made the body's multi-word tail atomic, which removed the
    runaway but also removed the tail's ability to give a word back. Every
    word in the body vocabulary is then unavailable to a literal that
    follows the capture, and several of those words are ordinary English:
    "to", "too" and "for" (the speech-engine homophones), "and", "oh",
    "one" and "hundred". So `^set volume (\\d+) to mute$`, a pattern a user
    can save in the Advanced editor, stopped matching "set volume fifteen
    to mute" while its digit form kept working.

    The widening is now context-aware: the atomic tail is used only for the
    pattern shapes that can actually run away, and every other pattern
    keeps the plain tail that can give a word back.
    """

    FOLLOWERS = [
        (r"^set volume (\d+) to mute$", "set volume fifteen to mute",
         "fifteen"),
        (r"^set volume (\d+) to mute$", "set volume twenty three to mute",
         "twenty three"),
        (r"^wait (\d+) and go$", "wait five and go", "five"),
        (r"^repeat (\d+) for me$", "repeat three for me", "three"),
        (r"^press (\d+) oh well$", "press seven oh well", "seven"),
        (r"^hold (\d+) hundred percent$", "hold two hundred percent", "two"),
    ]

    def test_a_literal_beginning_with_a_count_word_still_matches(self):
        """Every follower codex named, and the whole class around them."""
        failures = []
        for pattern, utterance, expected in self.FOLLOWERS:
            transformed, metadata = transform_pattern(pattern)
            group = int(metadata["validation_group"].lstrip("g"))
            match = re.compile(transformed, re.IGNORECASE).fullmatch(utterance)
            if match is None or match.group(group) != expected:
                got = None if match is None else match.group(group)
                failures.append(f"{pattern!r} on {utterance!r} gave {got!r}")

        assert not failures, "; ".join(failures)

    def test_the_digit_form_of_the_same_pattern_was_never_broken(self):
        """The control case: digits never needed the tail to give back."""
        transformed, metadata = transform_pattern(r"^set volume (\d+) to mute$")
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "set volume 15 to mute"
        )

        assert match is not None
        assert match.group(group) == "15"

    def test_a_follower_outside_the_count_vocabulary_still_matches(self):
        """A second control: this one passed before the fix and after it.

        It guards against a fix that restores the count-word followers by
        breaking the ordinary ones.
        """
        transformed, metadata = transform_pattern(r"^set volume (\d+) please$")
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "set volume fifteen please"
        )

        assert match is not None
        assert match.group(group) == "fifteen"


class TestManySequentialCapturesDoNotRunAway:
    """The second runaway shape, measured while fixing .1.6.

    The .1.3 finding named one dangerous shape: the capture inside the
    caller's own repeat. A pattern with many sequential captures and no
    enclosing repeat at all is exponential for the same reason -- each
    capture can take one to five words of the run, so the number of ways
    to divide the run multiplies. Measured with the plain tail: seven
    captures took 0.032s, eight 0.158s, nine 0.790s and ten 6.140s.
    """

    _BUDGET_SECONDS = 2.0

    def test_ten_sequential_captures_do_not_run_away(self):
        """Ten captures against a fifty-word run that cannot match."""
        pattern = "^" + " ".join([r"(\d+)"] * 10) + "$"
        transformed, _ = transform_pattern(pattern)

        result = match_bounded(
            transformed,
            " ".join(["one"] * 50) + " !",
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_four_sequential_captures_keep_the_plain_tail(self):
        """The safe side of the same boundary still gives a word back.

        Four captures is 0.000s measured, so it keeps the plain tail and
        a count-word follower after the last capture still matches.
        """
        transformed, _ = transform_pattern(
            r"^go (\d+) (\d+) (\d+) (\d+) to end$"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "go one two three four to end"
        )

        assert match is not None
        assert match.group(4) == "four"


class TestTheAtomicBodyIsNotForcedOnAPatternThatCanMatch:
    """codex finding wh-number-words-one-parser.1.8.

    The .1.6 fix chose one body for the WHOLE pattern: atomic when any
    capture sat inside a repeat, or when the pattern held more than four
    widened captures. Both triggers then imposed the .1.6 regression on
    every capture in the pattern, including shapes that were never slow.

    Measured with the plain tail against a twenty-four word run that
    cannot match: `^one(?: (B) to)+$` is 0.000s and `^(B) one (B) one (B)
    one (B) one (B) one$` is 0.003s, against 15.236s for the `.1.3` shape
    `^one(?: (B))+$`. What makes a shape slow is not the repeat or the
    capture count on its own -- it is two widened bodies competing for the
    same word run with nothing but a separator between them.
    """

    def test_a_repeat_with_a_count_word_literal_after_the_capture(self):
        """The repeat is real, but its literal pins the division."""
        transformed, metadata = transform_pattern(r"^one(?: (\d+) to)+$")
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch("one five to")

        assert match is not None, (
            "the literal ' to' inside the repeat separates one iteration "
            "from the next, so this shape never needed the atomic tail"
        )
        assert match.group(group) == "five"

    def test_five_captures_separated_by_a_count_word(self):
        """Above the four-capture threshold, but not adjacent."""
        pattern = r"^(\d+) one (\d+) one (\d+) one (\d+) one (\d+) one$"
        transformed, _ = transform_pattern(pattern)

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "two one three one four one five one six one"
        )

        assert match is not None, (
            "each ' one ' literal pins the division, so none of these five "
            "captures competes with the next for the same words"
        )
        assert match.group(1) == "two"
        assert match.group(5) == "six"


class TestZeroWidthSyntaxCannotHideARepeatQuantifier:
    """codex finding wh-number-words-one-parser.1.9.

    The enclosure scan read the ONE character after an enclosing group's
    ')' and asked whether it was '*', '+' or '{'. Python's grammar lets
    zero-width syntax sit in between, so a group that really does repeat
    was read as plain and got the exponential body. Both shapes below are
    accepted by the Advanced editor, and the save-time backtracking probe
    does not reach either: all four of its fixed probes begin "a", "word",
    "the" or "1", and each of these patterns is anchored on "one".

    Measured before the fix, by direct fullmatch with no bound: the
    verbose form took 0.474s at twenty words, 1.857s at twenty-two and
    15.050s at twenty-four; the comment form 1.102s, 4.166s and 15.682s.
    The runtime does not bound a match, so that is a hang.
    """

    _BUDGET_SECONDS = 2.0

    @staticmethod
    def _runaway_input(token_count: int) -> str:
        """A word run that cannot match, so the engine tries every split."""
        return "one " + " ".join(["one"] * token_count) + " !"

    def test_a_verbose_scope_hides_the_quantifier(self):
        """In an x scope the space before '+' is ignored, so '+' repeats."""
        transformed, _ = transform_pattern(r"^(?x:one(?:\s(\d+)) +)$")

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_a_comment_group_hides_the_quantifier(self):
        """`(?#...)` sits between the ')' and the '+' in any mode."""
        transformed, _ = transform_pattern(r"^one(?: (\d+))(?# note)+$")

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_an_optional_group_is_still_not_a_repeat(self):
        """'?' after the hidden syntax permits one iteration, not many.

        The skip must not turn every quantifier into a repeat: a pattern
        whose enclosing group is optional keeps the plain tail, so a
        count-word literal after it still matches.
        """
        transformed, metadata = transform_pattern(
            r"^one(?: (\d+))(?# note)? to end$"
        )
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "one five to end"
        )

        assert match is not None
        assert match.group(group) == "five"


class TestABoundedBraceIsNotARunawayRepeat:
    r"""codex finding wh-number-words-one-parser.1.12.

    ``_repeat_quantifier_follows`` read the single character after a
    group's ')' and called every '{' a repeat. A brace bounded at N lets
    the group take at most N iterations, which divides a word run exactly
    as N adjacent captures do, so the boundary is the one the adjacency
    rule already uses: ``_MAX_PLAIN_CAPTURES``. Marking a bounded brace
    dangerous put the atomic tail on a capture that was never slow, which
    is the same regression finding .1.8 reported for the pattern-wide
    rule.

    Measured before the fix, with ``^one(?: (\d+)){q} to end$`` against
    "one fifteen to end": ``{1}``, ``{0,1}``, ``{1,1}``, ``{1,2}``,
    ``{1,3}``, ``{1,4}``, ``{2}`` and ``{2,3}`` ALL took the atomic body
    and refused, while the raw pattern matched "one 15 to end". The first
    three were the finding's own examples; the mutation gate exposed the
    rest, by reporting brace-high-bound-ignored as a survivor that failed
    no test at all.

    Where the runaway really begins was measured, not assumed. Plain-tail
    cost of ``^one(?: (BODY)){1,N}$`` against a twenty-four word run that
    cannot match: N=3 0.000s, N=5 0.001s, N=8 0.100s, N=9 0.303s, N=10
    0.787s, N=12 3.732s, unbounded 15.628s. So the runaway parameters
    below start at twelve; a maximum of three proves nothing and is no
    longer claimed to.

    Which brace spellings Python really treats as quantifiers was also
    measured: ``{}``, ``{foo}`` and ``{1,2,3}`` are literal text and
    repeat nothing, while ``{,}`` and ``{,3}`` DO take a second
    iteration -- ``{,}`` is not literal.

    The literal spellings get no test here, because none can be written.
    The fix keeps answering "divides a run" for them, and even if it did
    not, no input tells the two answers apart: the brace character itself
    sits between the capture and whatever follows, so an atomic tail has
    no count word to swallow and gives the same match as a plain one.
    """

    _BUDGET_SECONDS = 2.0

    @staticmethod
    def _runaway_input(token_count: int) -> str:
        """A word run that cannot match, so the engine tries every split."""
        return "one " + " ".join(["one"] * token_count) + " !"

    # Each brace gets a spoken form its OWN bounds can satisfy. A brace
    # with a minimum above one needs that many iterations before the
    # trailing literal is reached, so feeding every case one count word
    # would fail on the pattern's own arithmetic and prove nothing about
    # which tail was chosen. The expected group is the LAST iteration's
    # capture, because a repeated group keeps only that one.
    @pytest.mark.parametrize(
        "brace, spoken, expected",
        [
            ("{1}", "one fifteen to end", "fifteen"),
            ("{0,1}", "one fifteen to end", "fifteen"),
            ("{1,1}", "one fifteen to end", "fifteen"),
            ("{1,2}", "one fifteen to end", "fifteen"),
            ("{1,3}", "one fifteen to end", "fifteen"),
            ("{1,4}", "one fifteen to end", "fifteen"),
            ("{0,4}", "one fifteen to end", "fifteen"),
            ("{2}", "one fifteen sixteen to end", "sixteen"),
            ("{2,3}", "one fifteen sixteen to end", "sixteen"),
            ("{4}", "one fifteen sixteen seventeen eighteen to end",
             "eighteen"),
        ],
    )
    def test_a_brace_bounded_at_the_safe_maximum_keeps_the_plain_tail(
        self, brace, spoken, expected
    ):
        """A count word still reaches the literal that follows the group."""
        transformed, metadata = transform_pattern(
            r"^one(?: (\d+))" + brace + r" to end$"
        )
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch(spoken)

        assert match is not None, (
            f"{brace} is bounded inside the measured safe maximum, so it "
            "must not force the atomic tail"
        )
        assert match.group(group) == expected

    @pytest.mark.parametrize(
        "brace", ["{1,12}", "{,12}", "{5,}", "{2,}", "{,}"]
    )
    def test_a_brace_that_really_repeats_still_gets_the_atomic_tail(
        self, brace
    ):
        """Every spelling that permits a second iteration stays bounded."""
        transformed, _ = transform_pattern(
            r"^one(?: (\d+))" + brace + r"$"
        )

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"


class TestAlternationInsideARepeatIsJudgedPerBranch:
    r"""codex finding wh-number-words-one-parser.1.13.

    The wrap-around model took the FIRST capture anywhere in the enclosing
    group as the next iteration's entry point. With alternation that is
    the wrong capture: in ``^one(?:foo (\d+)| (\d+))+$`` the second
    branch's capture re-enters its own branch with only a space between
    the iterations, but the model built its wrap from the first branch's
    "foo " prefix and called it safe.

    Measured before the fix by direct fullmatch with no bound: 0.008s at
    sixteen count words, 0.117s at twenty and 1.717s at twenty-four. The
    runtime does not bound a match, so that growth is a hang. The save-time
    backtracking probe does not reach it either, because all four of its
    fixed probes begin "a", "word", "the" or "1" and this pattern is
    anchored on "one".
    """

    _BUDGET_SECONDS = 2.0

    @staticmethod
    def _runaway_input(token_count: int) -> str:
        """A word run that cannot match, so the engine tries every split."""
        return "one " + " ".join(["one"] * token_count) + " !"

    def test_a_later_branch_that_can_follow_itself_is_atomic(self):
        """The second branch's capture re-enters itself through a space."""
        transformed, _ = transform_pattern(r"^one(?:foo (\d+)| (\d+))+$")

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_the_repeat_still_reads_a_count_in_either_branch(self):
        """The fix must not stop the pattern matching what it should."""
        transformed, _ = transform_pattern(r"^one(?:foo (\d+)| (\d+))+$")
        compiled = re.compile(transformed, re.IGNORECASE)

        assert compiled.fullmatch("one fifteen") is not None
        assert compiled.fullmatch("onefoo fifteen") is not None

    def test_a_branch_pinned_by_a_literal_keeps_the_plain_tail(self):
        """Alternation alone is not the hazard; a separator re-entry is.

        Every branch here ends in the literal " to", so no branch can
        follow any other through separators only. The plain tail must
        survive, or a count word before that literal stops matching.
        """
        transformed, _ = transform_pattern(
            r"^one(?:foo (\d+) to| (\d+) to)+end$"
        )
        compiled = re.compile(transformed, re.IGNORECASE)

        assert compiled.fullmatch("one fifteen toend") is not None
class TestVerboseLayoutCannotHideASeparatorOnlyWrap:
    r"""codex finding wh-number-words-one-parser.1.14.

    ``_matches_separators_only`` recompiled a slice of the user's own
    pattern with NO flags. When the pattern is in verbose mode -- set
    globally by ``(?x)`` or locally by ``(?x:...)`` -- the layout
    whitespace and the ``#`` comments that re ignores became literal
    requirements inside that slice. No separator sample could match, the
    wrap read as "a real word stands between the iterations", and a
    capture that really does compete for the same run of words kept the
    plain body.

    Measured before the fix by direct fullmatch with no bound, against
    "one " plus twenty-four count words plus " !":

      (?x)^one (?: \s (\d+) ) +$                7.254s
      (?x)^one(?:\s(\d+) # layout note\n)+$     7.099s
      ^(?x:one(?: \s (\d+) ) +)$                7.614s

    The same shapes without the verbose flag took 0.000s, because they
    already took the atomic body. The runtime matcher calls fullmatch
    directly, with no bound, so each of those three is a hang.

    The adjacency half shares the same helper. Twelve captures whose real
    separator is ``[\s]``, padded by a verbose comment, took 22.033s
    against thirty count words while the same chain written without the
    verbose flag took 0.000s.
    """

    _BUDGET_SECONDS = 2.0

    @staticmethod
    def _runaway_input(token_count: int) -> str:
        """A word run that cannot match, so the engine tries every split."""
        return "one " + " ".join(["one"] * token_count) + " !"

    @pytest.mark.parametrize(
        "raw",
        [
            # Global (?x): the padding spaces around the group's content
            # are ignored, so one iteration re-enters the next through
            # \s alone.
            r"(?x)^one (?: \s (\d+) ) +$",
            # Global (?x) with a comment doing the same job as padding.
            "(?x)^one(?:\\s(\\d+) # layout note\n)+$",
            # Scoped (?x:...): the same, one nesting level down.
            r"^(?x:one(?: \s (\d+) ) +)$",
        ],
    )
    def test_a_verbose_repeat_wrap_takes_the_atomic_body(self, raw):
        """The separator test must read the slice in the pattern's own mode."""
        transformed, _ = transform_pattern(raw)

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "re ignores this pattern's layout, so one iteration follows "
            "the next through separators only and the tail must be atomic"
        )

        result = match_bounded(
            transformed,
            self._runaway_input(24),
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_a_verbose_comment_between_captures_still_counts_as_adjacent(self):
        r"""The adjacency test reads the same slice and needs the same mode.

        Twelve captures separated by ``[\s]`` and a verbose comment. The
        comment is not part of the separator re actually sees, so these
        captures are adjacent and every one but the last needs the atomic
        tail.
        """
        parts = ["(?x)^"]
        for index in range(12):
            parts.append(r"(\d+)")
            if index != 11:
                parts.append("[\\s]# sep\n")
        parts.append("$")

        transformed, _ = transform_pattern("".join(parts))

        assert transformed.count(NUMBER_CAPTURE_BODY_ATOMIC) == 11, (
            "twelve adjacent captures are past the four-capture threshold, "
            "so every one but the last takes the atomic tail"
        )

        result = match_bounded(
            transformed,
            " ".join(["one"] * 30) + " !",
            flags=re.IGNORECASE,
            timeout=self._BUDGET_SECONDS,
            mode="fullmatch",
        )

        assert result is None, "this input is not a count and must not match"

    def test_a_verbose_pattern_still_matches_what_it_should(self):
        """The fix must not stop a verbose pattern reading its own count."""
        transformed, metadata = transform_pattern(
            r"(?x)^one (?: \s (\d+) ) +$"
        )
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch("one fifteen")

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_a_verbose_branch_pinned_by_a_literal_keeps_the_plain_tail(self):
        """Verbose mode alone is not the hazard; a separator re-entry is.

        The literal " to" inside the repeat pins where each iteration
        ends, in verbose mode exactly as in any other, so this shape must
        keep the plain tail whatever the separator test does with the
        layout around it.
        """
        transformed, _ = transform_pattern(r"(?x)^one (?: \s (\d+) \sto ) +$")

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "the literal 'to' separates one iteration from the next, so "
            "this shape never needed the atomic tail"
        )
class TestAGroupBoundaryInTheSliceIsNotASeparator:
    r"""codex finding wh-number-words-one-parser.1.15.

    Both separator rules slice raw pattern text between two points that
    can sit in different groups, so the slice carries a ``)`` with no
    ``(`` or the reverse. ``re.compile`` refuses such a slice and
    ``_matches_separators_only`` answered its conservative True, which
    forces the atomic tail onto a capture whose iterations are really
    pinned by a literal. The atomic tail then eats that literal as a
    number word and cannot give it back.

    Measured at c9428f8c, before the fix:

      ^one(?:(?:\s(\d+)\sto))+$   1 atomic body; refuses "one fifteen to",
                                  while the same transformed pattern with
                                  the plain body matches it.
      ^one(?:\s(\d+)\sto)+$       the unwrapped twin: 0 atomic bodies and
                                  it matches.

    The slice for the first shape is exactly ``\sto)(?:\s``.

    A parenthesis a slice cuts through is group structure and consumes no
    input, so supplying the missing halves cannot change what the slice
    can match. That is what the fix does, and it is why the answer for a
    wrapper whose repeat really is separator-only does not change:
    ``^one(?:(?:\s(\d+)))+$`` still takes the atomic body, and it needs
    to -- forced to the plain body it took 1.825s on a twenty-two word
    nonmatch against 0.001s as it stands.
    """

    _BUDGET_SECONDS = 2.0

    @pytest.mark.parametrize(
        "opening, closing",
        [
            ("(?:", ")"),      # non-capturing
            ("(", ")"),        # capturing, which also renumbers
            ("(?P<w>", ")"),   # named
            ("(?>", ")"),      # atomic
            ("(?i:", ")"),     # scoped flag
        ],
    )
    def test_a_wrapper_does_not_hide_the_literal_that_pins_the_repeat(
        self, opening, closing
    ):
        """The wrapper's parentheses consume no input, so nothing changed."""
        transformed, metadata = transform_pattern(
            r"^one(?:" + opening + r"\s(\d+)\sto" + closing + r")+$"
        )
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "the literal 'to' still separates one iteration from the next, "
            "so wrapping the body must not force the atomic tail"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "one fifteen to"
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_a_wrapper_between_captures_does_not_make_them_adjacent(self):
        """The adjacency slice crosses the same boundary and reads the same."""
        pattern = (
            r"^((\d+) to) ((\d+) to) ((\d+) to) ((\d+) to) ((\d+) to)$"
        )
        transformed, _ = transform_pattern(pattern)

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "each ' to' pins its own capture, so these five are not a run "
            "of adjacent captures however many wrappers stand between them"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "fifteen to sixteen to seventeen to eighteen to nineteen to"
        )

        assert match is not None

    def test_a_wrapper_around_a_separator_only_repeat_stays_atomic(self):
        r"""The fix must not turn a real runaway into a plain tail.

        Nothing but ``\s`` stands between one iteration and the next, so
        this shape needs the atomic tail exactly as its unwrapped form
        does. Forced to the plain body it took 1.825s on the input below,
        against 0.001s as it stands.
        """
        transformed, _ = transform_pattern(r"^one(?:(?:\s(\d+)))+$")

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "a wrapper adds no text between the iterations, so this repeat "
            "is still separator-only and still needs the atomic tail"
        )

        result, ran_away = _bounded_fullmatch(
            transformed,
            "one " + " ".join(["one"] * 24) + " !",
            self._BUDGET_SECONDS,
        )

        assert not ran_away, (
            "the transformed pattern ran past its budget, which is the "
            "runaway the atomic tail exists to prevent"
        )
        assert result is None, "this input is not a count and must not match"

    def test_a_wrapper_around_a_long_capture_chain_still_counts_the_run(self):
        """Adjacency must still SEE a run that a wrapper only decorates.

        Ten captures with nothing but a space between them, each wrapped.
        The wrapper adds no text, so the run is still adjacent and every
        capture but the last needs the atomic tail.
        """
        pattern = "^" + " ".join([r"(?:(\d+))"] * 10) + "$"
        transformed, _ = transform_pattern(pattern)

        assert transformed.count(NUMBER_CAPTURE_BODY_ATOMIC) == 9, (
            "ten adjacent captures are past the four-capture threshold, so "
            "every one but the last takes the atomic tail"
        )


class TestAScopedFlagBoundaryKeepsItsModeAcrossTheSlice:
    r"""codex finding wh-number-words-one-parser.1.16.

    A slice may cut through the closing parenthesis of a SCOPED FLAG
    group. The text after that cut is then governed by the parent
    scope, not by the mode the capture sits in, so a repair that
    supplies a plain ``(?:`` reads the rest of the slice in the wrong x
    mode. In ``(?x)^one(?: (?-x:\s(\d+)) )+$`` the two spaces around
    the scoped group belong to the outer verbose scope and re ignores
    them, so ``\s`` alone separates one iteration from the next; read
    with x off they look like literal text and the repeat looks pinned.

    Measured at fd895cba, before the fix, against twenty-four count
    words and a trailing "!":

      (?x)^one(?: (?-x:\s(\d+)) )+$        0 atomic bodies, 7.423s
      ten captures, each in (?-x:...)      0 atomic bodies, 0.824s

    The fix rebuilds each scope the slice cuts through as an explicit
    ``(?x:`` or ``(?-x:`` opening and compiles in the mode that follows
    the last cut, rather than refusing to repair the slice at all: a
    refusal would answer the conservative True and put the atomic tail
    back on ``(?x)^one(?: (?-x:\s(\d+)\sto) )+$``, whose iterations a
    literal really does pin.
    """

    _BUDGET_SECONDS = 2.0

    _REPEAT = r"(?x)^one(?: (?-x:\s(\d+)) )+$"
    _ADJACENT = r"(?x)^(?-x:(\d+))" + r"  (?-x:\s(\d+))" * 9 + "$"
    _PINNED = r"(?x)^one(?: (?-x:\s(\d+)\sto) )+$"
    #: The mirror image: a plain outer pattern with a verbose scope
    #: around the capture, so the text the slice reads INSIDE the scope
    #: it reopens is layout and a comment rather than literal text.
    _INNER_COMMENT = "^one(?:(?x:\\s(\\d+)  # gap\n)\\s)+$"

    def test_a_scoped_mode_change_does_not_hide_a_separator_only_repeat(self):
        r"""The outer x scope ignores the spaces, so ``\s`` is the whole gap."""
        transformed, _ = transform_pattern(self._REPEAT)

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "the two spaces sit in the outer verbose scope and re ignores "
            "them, so nothing but whitespace separates the iterations and "
            "this repeat needs the atomic tail"
        )

        result, ran_away = _bounded_fullmatch(
            transformed,
            "one " + " ".join(["one"] * 24) + " !",
            self._BUDGET_SECONDS,
        )

        assert not ran_away, (
            "the two spaces are layout the outer verbose scope ignores, so "
            "reading them as literal text leaves the plain body to run away"
        )
        assert result is None, "this input is not a count and must not match"

    def test_a_scoped_mode_change_does_not_hide_a_run_of_captures(self):
        """The adjacency slice crosses the same boundary and reads the same."""
        transformed, _ = transform_pattern(self._ADJACENT)

        assert transformed.count(NUMBER_CAPTURE_BODY_ATOMIC) == 9, (
            "ten captures separated by whitespace the outer verbose scope "
            "ignores are still a run past the four-capture threshold"
        )

    def test_a_literal_inside_the_scoped_group_still_pins_the_repeat(self):
        """Rebuilding the scope, rather than refusing, keeps this one plain."""
        transformed, metadata = transform_pattern(self._PINNED)
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "the literal 'to' separates one iteration from the next however "
            "the x mode changes around it"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "one fifteen to"
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_the_scoped_repeat_still_matches_a_real_count(self):
        """The atomic tail must not cost the shape its own spoken form."""
        transformed, metadata = transform_pattern(self._REPEAT)
        group = int(metadata["validation_group"].lstrip("g"))

        match = re.compile(transformed, re.IGNORECASE).fullmatch("one fifteen")

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_the_reopened_scope_keeps_the_mode_it_had_inside(self):
        r"""The slice's text BEFORE the cut ``)`` is read in the inner mode.

        The outer pattern here is plain and the capture sits in a
        ``(?x:...)`` scope, so the ``  # gap`` following the capture is
        layout and a comment that re ignores, and only whitespace
        separates one iteration from the next. Reopening that scope as a
        plain ``(?:`` would read the comment as literal text and leave
        the plain body on a repeat that needs the atomic tail -- which
        is why supplying the missing half is not enough on its own, and
        the half supplied has to carry the mode.
        """
        transformed, metadata = transform_pattern(self._INNER_COMMENT)
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "re ignores the layout and the comment inside the verbose "
            "scope, so nothing but whitespace stands between the "
            "iterations and this repeat needs the atomic tail"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            "one fifteen "
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_the_balancer_rebuilds_the_scope_the_slice_cuts_through(self):
        """The cut ``)`` comes back as the scoped opening it really closed."""
        built = _balanced_for_compile(r")  (?-x:\s", (True, False))

        assert built == (r"(?-x:)  (?-x:\s)", True), (
            "the slice starts inside a verbose-off scope and continues in "
            "the verbose-on parent, and only an explicit scoped opening "
            "can say so"
        )

    def test_the_balancer_refuses_a_slice_that_closes_past_its_chain(self):
        """No recorded parent means no sound repair, so answer dangerous."""
        assert _balanced_for_compile(r")\s", (True,)) is None


class TestAnUnresolvedReferenceIsNotProofOfASeparator:
    r"""codex finding wh-number-words-one-parser.1.17.

    A slice is a fragment of the user's pattern, so a backreference or a
    conditional inside it can name a group DEFINED OUTSIDE it. re.compile
    refuses such a fragment -- "unknown group name 'sep'", "invalid group
    reference 1" -- and the conservative ``except re.error: return True``
    then called the fragment separator-only. The atomic tail landed on a
    repeat whose iterations that very reference pins to a literal, and
    ate the literal it needed. This is the .1.8 and .1.15 harm reached
    through a fourth door.

    Measured at 72ec1b66, before the fix, all three shapes alike: the raw
    pattern matches its digit form, the transform installs one atomic
    body, the transformed pattern REFUSES the spoken form, and the same
    pattern with the plain body matches it.

    The conservative answer cannot simply be flipped. A backreference to
    a whitespace group is a genuine runaway and needs the atomic tail, so
    the fix resolves the reference instead of guessing: a backreference
    matches exactly the text its group captured, so that group's own body
    is a superset of what the reference can match. No separator sample
    matching the superset proves none can match the reference.
    """

    _BUDGET_SECONDS = 2.0

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^(?P<sep> to)(?: (\d+)(?P=sep))+$",     # named backreference
            r"^( to)(?: (\d+)\1)+$",                  # numeric backreference
            r"^( to)?(?: (\d+)(?(1) to|\sx))+$",      # conditional
        ],
    )
    def test_a_reference_to_a_literal_keeps_the_plain_tail(self, pattern):
        """The reference pins the iteration, so the tail must give it back."""
        transformed, metadata = transform_pattern(pattern)
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "the reference can only ever match ' to', which is not a "
            "separator, so this repeat is pinned and keeps the plain tail"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            " to fifteen to"
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_a_reference_to_whitespace_still_takes_the_atomic_tail(self):
        r"""Resolving the reference must not lose a real runaway.

        ``(?P=sep)`` here can only ever match one whitespace character,
        so nothing but whitespace separates the iterations and this
        repeat needs the atomic tail exactly as an inline ``\s`` would.
        """
        transformed, _ = transform_pattern(
            r"^(?P<sep>\s)(?: (\d+)(?P=sep))+$"
        )

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "the group the reference names matches whitespace and nothing "
            "else, so the separator really is whitespace only"
        )

        result, ran_away = _bounded_fullmatch(
            transformed, " " + " ".join(["one"] * 24) + " !",
            self._BUDGET_SECONDS,
        )

        assert not ran_away, (
            "resolving the reference must leave the atomic tail in place, "
            "which is what stops this input running away"
        )
        assert result is None, "this input is not a count and must not match"

    def test_the_reference_body_keeps_the_mode_it_had_inside(self):
        r"""The body is read in ITS OWN x mode, not the slice's.

        ``(?P<sep>\s  # gap\n)`` sits inside an ``(?x:`` scope, so it
        matches one whitespace character and the repeat around it really
        is separated by whitespace only -- the atomic tail belongs there.
        Spliced into a non-verbose slice without that mode, the same text
        reads as ``\s``, two literal spaces, a '#', and a newline, which
        no separator sample matches, and the repeat would wrongly keep
        the plain tail. Verified both ways before the fix landed: the
        slice answers True with the mode carried and False without it.
        """
        transformed, _ = transform_pattern(
            "^(?x:(?P<sep>\\s  # gap\n))(?: (\\d+)(?P=sep))+$"
        )

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "read in its own verbose mode the group matches one "
            "whitespace character, so this repeat is separated by "
            "whitespace only and needs the atomic tail"
        )

    def test_a_fragment_that_fails_to_compile_for_another_reason_is_dangerous(
        self,
    ):
        """The conservative answer stays for everything else that will not
        compile."""
        assert _matches_separators_only("[a", (False,), {}) is True, (
            "an unterminated character class is not a reference the "
            "resolver can supply, so the conservative answer still stands"
        )

    def test_the_resolver_leaves_text_inside_a_class_and_a_comment_alone(self):
        r"""A reference is syntax only where re reads it as syntax."""
        references = {r"\1": "(?-x: to)"}

        assert _resolve_references(r"[\1]\1", (False,), references) == (
            r"[\1](?-x: to)"
        ), "inside a character class '\\1' is an octal escape, not a reference"

        assert _resolve_references(r"(?#\1)\1", (False,), references) == (
            r"(?#\1)(?-x: to)"
        ), "inside a comment group it is comment text"

    def test_the_resolver_leaves_a_two_digit_reference_alone(self):
        r"""``\10`` is not ``\1`` followed by a zero, so do not split it."""
        assert _resolve_references(r"\10", (False,), {r"\1": "(?-x: to)"}) == (
            r"\10"
        ), (
            "substituting the one-digit reference here would change what "
            "the fragment means, so leave it and keep the conservative "
            "answer the failed compile gives"
        )


class TestAResolvedReferenceCarriesItsWholeMeaning:
    r"""codex findings .1.18, .1.19 and .1.20, all against 2bee0344.

    The round-9 fix resolves a reference against the body of the group it
    names. Round 10 found three ways that resolution was not yet the whole
    meaning of the reference, and all three reproduce.

    .1.18, an else-less conditional. "(?(N)yes)" matches the EMPTY string
    when group N did not participate, so replacing its opener with "(?:"
    and nothing else gives "(?:yes)", a strict subset. The separator test
    then answers False for a wrap that really is separator-only and the
    plain tail stays where the atomic tail belongs. Measured at 2bee0344
    on "one" + " one" * 20 + " !": 0.171s as shipped, 0.000s with the
    atomic body forced.

    .1.19, a slice that rebinds a numeric reference. The retry ran only
    after the first compile FAILED, so a slice that compiles while binding
    "\1" to a capture inside itself never reached it. re.compile(r"()\1 ")
    succeeds and fullmatches a single space, so the separator test said
    True and installed the atomic tail on a repeat that " to" pins.

    .1.20, a body that holds another reference. The resolver never scanned
    the text it had just inserted, so group 2's body "(\1)" arrived still
    carrying "\1", the compile failed a second time, and the conservative
    True installed the atomic tail again.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^( to)?one(?: (\d+)(?(1) to))+$",
            r"^(?P<sep> to)?one(?: (\d+)(?(sep) to))+$",
        ],
    )
    def test_an_else_less_conditional_still_takes_the_atomic_tail(
        self, pattern
    ):
        """An absent group makes the conditional empty, so only the space
        separates the iterations."""
        transformed, _ = transform_pattern(pattern)

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "group 1 is optional, so when it is absent this conditional "
            "matches nothing at all and the iterations are separated by "
            "whitespace only -- that is the shape the atomic tail exists "
            "for"
        )

    def test_the_resolver_gives_an_else_less_conditional_an_empty_branch(
        self,
    ):
        """The empty alternative is what makes the replacement a superset."""
        references = {"(?(1)": "(?:"}

        assert _resolve_references("(?(1) to)", (False,), references) == (
            "(?: to|)"
        ), "an omitted else branch still matches the empty string"

        assert _resolve_references("(?(1) to|x)", (False,), references) == (
            "(?: to|x)"
        ), "a conditional that has both branches needs nothing added"

    def test_a_slice_that_rebinds_a_reference_keeps_the_plain_tail(self):
        r"""A capture inside the slice renumbers the fragment.

        Compiled on its own, the empty "()" becomes local group 1, so
        "\1" matches what IT captured -- nothing -- and the fragment
        fullmatches a single space. In the whole pattern "\1" can only
        ever be " to", which pins the repeat.
        """
        transformed, metadata = transform_pattern(r"^( to)(?: (\d+)()\1)+$")
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "the reference names the outer group, not the empty one beside "
            "it, so this repeat is pinned and keeps the plain tail"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            " to fifteen to"
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    @pytest.mark.parametrize(
        "pattern",
        [
            r"^( to)(\1)(?: (\d+)\2)+$",
            r"^(?P<a> to)(?P<b>(?P=a))(?: (\d+)(?P=b))+$",
        ],
    )
    def test_a_reference_body_that_holds_another_reference_resolves_through(
        self, pattern
    ):
        r"""Group 2 is a copy of group 1, so it too can only be " to"."""
        transformed, metadata = transform_pattern(pattern)
        group = int(metadata["validation_group"].lstrip("g"))

        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed, (
            "resolving one hop leaves a reference behind; resolving all the "
            "way shows this repeat is pinned by a literal"
        )

        match = re.compile(transformed, re.IGNORECASE).fullmatch(
            " to to fifteen to"
        )

        assert match is not None
        assert match.group(group) == "fifteen"

    def test_a_nested_whitespace_reference_still_takes_the_atomic_tail(self):
        r"""Resolving all the way must not lose a real runaway either.

        Group 2 copies group 1, and group 1 matches one whitespace
        character, so the repeat really is separated by whitespace only.
        """
        transformed, _ = transform_pattern(r"^(\s)(\1)(?: (\d+)\2)+$")

        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, (
            "the group at the end of the chain matches whitespace and "
            "nothing else, so the separator really is whitespace only"
        )
def _carries_an_atomic_body(transformed: str) -> bool:
    r"""Does any widened capture carry the ATOMIC tail, either hyphen form?

    Two bodies carry that tail: the one whose tail joins words with
    ``[\s-]`` and the one that dropped the hyphen because the raw
    expression consumes a hyphen of its own after the capture
    (wh-number-words-one-parser.1.34). The two choices are separate
    axes, so a test that pins the ATOMIC choice has to accept both
    forms or it starts failing for the hyphen axis instead of the one
    it is about. The hyphen axis has its own tests in
    ``TestAHyphenAfterTheCaptureBelongsToTheCaller``.

    Only the three tests whose raw expression carries a BACKREFERENCE
    use this. A reference to a group holding a lookaround or ``\b``
    resolves to the ANY_TEXT superset, which can match a hyphen, so
    those captures answer hyphen-bounded. Every other atomic test still
    pins ``NUMBER_CAPTURE_BODY_ATOMIC`` exactly.
    """
    return (
        NUMBER_CAPTURE_BODY_ATOMIC in transformed
        or NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN in transformed
    )


class TestADetachedBodyMeansWhatItMeantInPlace:
    r"""A reference body is spliced into a slice cut from somewhere else.

    Three ways that move can change what the body means, all found by
    codex round 11 against 0c6fee99 and all reproduced before the fix.
    A body that asks about its surroundings matches NOTHING once
    detached, which is a subset and not the superset the whole argument
    rests on (.1.21). A body is resolved against the map built so far,
    and an ascending walk by capture NUMBER resolves an outer group
    before a nested one it legally references (.1.22). And one body
    spliced twice into one slice defines its named group twice, which
    re refuses (.1.23).
    """

    @pytest.mark.parametrize(
        "raw",
        [
            r"^x(?P<sep>(?<=x)\s)(?:(\d+)(?P=sep))+$",
            r"^x((?<=x)\s)(?:(\d+)\1)+$",
        ],
    )
    def test_a_lookbehind_body_still_takes_the_atomic_tail(self, raw):
        r"""``(?<=x)\s`` detached matches nothing, so no sample matches.

        The separator test then reads the wrap as a real word and keeps
        the plain body, which divides a word run. Measured at 0c6fee99:
        0.172s on a twenty-word nonmatch where the atomic body took
        0.000s.
        """
        transformed, _ = transform_pattern(raw)
        assert _carries_an_atomic_body(transformed)
        result, ran_away = _bounded_fullmatch(
            transformed, "x " + ("one " * 24) + "!", 2.0
        )
        assert not ran_away
        assert result is None

    def test_a_word_boundary_body_still_takes_the_atomic_tail(self):
        r"""``\b`` asks about the character before it, like a lookbehind."""
        transformed, _ = transform_pattern(
            r"^x(?P<sep>\b\s)(?:(\d+)(?P=sep))+$"
        )
        assert _carries_an_atomic_body(transformed)

    def test_a_body_that_names_a_nested_group_keeps_the_plain_tail(self):
        r"""Group 1 references group 2, which is NESTED inside group 1.

        A group takes its number when it OPENS, so the nested group's
        number is the higher one and an ascending walk resolved group 1
        first, leaving its ``\2`` raw. Close order gets it right.
        """
        raw = r"^(( to)\2)(?: (\d+)\1)+$"
        spoken = " to to fifteen to to"
        assert re.fullmatch(raw, " to to 15 to to")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, spoken)

    def test_a_named_body_spliced_twice_keeps_the_plain_tail(self):
        r"""One body used twice in a slice defined ``inner`` twice.

        re refuses the whole slice with "redefinition of group name
        'inner'", and the refusal reached the conservative answer.
        """
        raw = r"^(?P<sep>(?P<inner> to))(?: (\d+)(?P=sep)(?P=sep))+$"
        spoken = " to fifteen to to"
        assert re.fullmatch(raw, " to 15 to to")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, spoken)

    def test_a_detached_body_carries_no_capturing_group(self):
        """Both capture forms come back non-capturing."""
        assert _detached_body(r"(?P<inner> to)", False) == r"(?: to)"
        assert _detached_body(r"( to)", False) == r"(?: to)"
        assert re.compile(_detached_body(r"( to)", False) * 2).groups == 0

    def test_a_context_free_body_comes_back_unchanged(self):
        r"""Nothing is rewritten when nothing needs to be."""
        assert _detached_body(r"\s to", False) == r"\s to"
        assert _detached_body(r"[(]#", True) == r"[(]#"

    @pytest.mark.parametrize(
        "text",
        [
            r"(?<=x)\s",
            r"(?<!x)\s",
            r"(?=x)\s",
            r"(?!x)\s",
            r"\bx",
            r"\Bx",
            r"\Ax",
            r"x\Z",
            r"^x",
            r"x$",
            r"( to)\1",
            r"(?P<a> to)(?P=a)",
        ],
    )
    def test_a_body_that_cannot_be_detached_is_refused(self, text):
        """Every construct that reads its surroundings answers None."""
        assert _detached_body(text, False) is None

    def test_a_class_or_comment_hides_those_constructs(self):
        r"""``^`` in a class and ``\b`` in a comment are ordinary text."""
        assert _detached_body(r"[$^]", False) == r"[$^]"
        assert _detached_body(r"(?#\b)x", False) == r"(?#\b)x"
        assert _detached_body("x  # \\b\n", True) == "x  # \\b\n"

    def test_a_refused_body_makes_every_separator_sample_match(self):
        """ANY_TEXT is what keeps the conservative answer."""
        assert _matches_separators_only(
            "(?P=sep)", (False,), {"(?P=sep)": ANY_TEXT}
        )


class TestASliceMeansWhatItMeantInPlaceToo:
    r"""The .1.21 argument reaches the raw slice, not only a body.

    codex round 12 (.1.24 and .1.25) against 670511f8. A slice is cut
    out of the pattern and compiled on its own, so a lookaround, an
    anchor or a boundary escape inside it asks about text the compiled
    slice no longer has. ``(?<=\w)\s`` matches a space after a word
    character in place and matches nothing standalone, so no separator
    sample matched, the wrap read as a real word, and the plain body
    stayed on a repeat that divides a word run: about 0.18s on a
    twenty-word nonmatch where the atomic body took 0.0005s, with
    ``PatternManager._probe_backtracking`` returning None for every one
    of them.

    .1.25 is the same refusal reached through the round-11 fallback.
    ``ANY_TEXT`` is a superset, and widening reverses direction under a
    negative lookahead: ``(?!(?s:.*))`` can never match, so the probe
    read a working separator as unsafe. The negative lookahead is itself
    a construct the slice cannot evaluate detached, so the one refusal
    answers both.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            r"^x (?:(\d+)(?<=\w)\s)+end$",
            r"^x (?:(\d+)\s(?=\w))+end$",
            r"^x (?:(\d+)\b\s)+end$",
            r"^x (?:(\d+)\s(?!\Z))+end$",
            r"^a(?P<sep>(?<=a)x) (?:(\d+)(?!(?P=sep))\s)+end$",
        ],
    )
    def test_a_context_sensitive_slice_takes_the_atomic_tail(self, raw):
        """Each shape matches its digit form and still ran away."""
        assert re.fullmatch(raw, "x 1 2 end") or re.fullmatch(
            raw, "ax 1 2 end"
        )
        transformed, _ = transform_pattern(raw)
        assert _carries_an_atomic_body(transformed)
        result, ran_away = _bounded_fullmatch(
            transformed, "ax " + ("one " * 24) + "!", 2.0
        )
        assert not ran_away
        assert result is None

    def test_a_slice_with_nothing_to_ask_keeps_the_plain_tail(self):
        """The refusal must not swallow every slice it is asked about."""
        raw = r"^x (?:(\d+) to )+end$"
        assert re.fullmatch(raw, "x 15 to end")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, "x fifteen to end", re.I)

    def test_a_boundary_escape_alone_answers_conservatively(self):
        r"""``\b\s`` carries no lookaround and is still context-sensitive."""
        assert _matches_separators_only(r"\b\s", (False,), {})
        assert not _matches_separators_only(r" to", (False,), {})

    def test_a_widened_reference_under_a_negation_answers_true(self):
        """ANY_TEXT inside ``(?!...)`` matched nothing before the fix."""
        assert _matches_separators_only(
            r"(?!(?P=sep))\s", (False,), {"(?P=sep)": ANY_TEXT}
        )


class TestANumericConditionalResolvesBeforeItsTargetCloses:
    r"""A conditional's replacement depends on no body, so it cannot wait.

    codex round 12 (.1.26) against 670511f8. Python accepts a numeric
    conditional whose target group has not closed --
    ``re.compile(r"(?(1)a|b)(c)")`` succeeds -- so a body can hold
    ``(?(2)...)`` while group 2 opens after that body closes. The
    ``"(?(N)" -> "(?:"`` entry is a constant, but it was added inside
    the per-group walk, so at group 1's turn the map held no entry for
    group 2, the conditional survived into ``_detached_body``, that
    refused it, and ``\1`` became ANY_TEXT. The atomic tail then refused
    a spoken form the literal pins.

    Close order stays right for the BODY dependencies; only the constant
    was in the wrong place.
    """

    def test_a_conditional_naming_a_later_group_keeps_the_plain_tail(self):
        """Group 1 closes before group 2 opens, and still resolves."""
        raw = r"^((?(2) x| to))()(?: (\d+)\1)+$"
        assert re.fullmatch(raw, " to 15 to")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, " to fifteen to", re.I)

    def test_a_named_conditional_on_its_own_group_keeps_the_plain_tail(
        self,
    ):
        """re accepts a named conditional on the group it sits inside.

        A named conditional cannot name a group defined later --
        ``(?P<a>(?(b)x))(?P<b>)`` is refused with "unknown group name" --
        but it can name its own still-open group, and that entry was
        added at the END of that group's own turn.
        """
        raw = r"^(?P<a>(?(a) x| to))(?: (\d+)(?P=a))+$"
        assert re.fullmatch(raw, " to 15 to")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, " to fifteen to", re.I)

    def test_a_conditional_naming_a_nested_later_group_resolves_too(self):
        """The target opens later AND sits inside another group."""
        raw = r"^((?(3) x| to))((?: to)())(?: (\d+)\1)+$"
        assert re.fullmatch(raw, " to to 15 to")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, " to to fifteen to", re.I)


class TestAPinningLiteralOutranksAContextAssertion:
    r"""A refused slice can still hold a literal that pins the repeat.

    codex round 13 (.1.28) against 60b68224. The .1.24 refusal answers
    True -- "this wrap is separator-only" -- for every slice
    ``_detached_body`` cannot detach, and True puts the atomic tail on
    the capture. That is the safe answer only while the slice really
    could be nothing but separators, and one slice can carry BOTH an
    assertion and a mandatory literal. ``(?<=\w) to end`` pins where
    each iteration of ``^go(?: (\d+)(?<=\w) to end)+$`` ends, so the
    plain body both matches "go fifteen to end" and returns in 0.000s
    on a twenty-word nonmatch. The refusal took the atomic tail anyway,
    that tail ate the " to" -- "to" is a count word -- and could not
    give it back, so a command a user can save in the Advanced editor
    accepted "go 15 to end" and refused the spoken form.

    The fix tests a SUPERSET of the refused slice: every zero-width
    assertion is deleted, which can only add matches. If no separator
    sample matches even the widened slice, none matches the slice
    itself, so False is certain rather than guessed.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            r"^go(?: (\d+)(?<=\w) to end)+$",
            r"^go(?: (\d+) to(?=\W) end)+$",
            r"^go(?: (\d+)\b to end)+$",
            r"^go(?: (\d+)(?!\Z) to end)+$",
            r"^go(?: (\d+)(?<!\s) to end)+$",
        ],
    )
    def test_a_pinned_repeat_keeps_the_plain_tail(self, raw):
        """The digit form matched, so the spoken form has to match too."""
        assert re.fullmatch(raw, "go 15 to end")
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert re.fullmatch(transformed, "go fifteen to end", re.I)
        result, ran_away = _bounded_fullmatch(
            transformed, "go " + ("one " * 20) + "nowhere", 2.0
        )
        assert not ran_away
        assert result is None

    def test_a_separator_only_context_slice_still_takes_the_atomic_tail(self):
        """The .1.24 answer has to survive the .1.28 refinement."""
        for raw in (
            r"^x (?:(\d+)(?<=\w)\s)+end$",
            r"^x (?:(\d+)\s(?=\w))+end$",
            r"^x (?:(\d+)\b\s)+end$",
            r"^x (?:(\d+)\s(?!\Z))+end$",
        ):
            transformed, _ = transform_pattern(raw)
            assert NUMBER_CAPTURE_BODY_ATOMIC in transformed, raw

    def test_a_refused_slice_holding_a_literal_answers_false(self):
        """The answer the transform reads, asked directly."""
        assert not _matches_separators_only(r"(?<=\w) to end ", (False,), {})
        assert _matches_separators_only(r"(?<=\w)\s", (False,), {})

    def test_a_widened_reference_under_a_negation_still_answers_true(self):
        r"""Dropping ``(?!...)`` whole takes its ``ANY_TEXT`` with it."""
        assert _matches_separators_only(
            r"(?!(?P=sep))\s", (False,), {"(?P=sep)": ANY_TEXT}
        )

    def test_a_pinning_literal_under_a_negation_answers_false(self):
        r"""The same negation with a real word after it is not a separator."""
        assert not _matches_separators_only(
            r"(?!(?P=sep)) to end ", (False,), {"(?P=sep)": ANY_TEXT}
        )


class TestTheAssertionFreeSuperset:
    r"""What ``_assertion_free_superset`` deletes, keeps, and refuses.

    codex round 13 (.1.28). The whole soundness argument is that
    deleting a zero-width construct can only ADD matches, so anything
    the walk deletes has to be zero-width and anything it keeps has to
    be preserved exactly.
    """

    @pytest.mark.parametrize(
        "text, expected",
        [
            (r"(?<=\w) to end ", " to end "),
            (r"(?<!\s) to end ", " to end "),
            (r" to(?=\W) end ", " to end "),
            (r" to(?!\Z) end ", " to end "),
            (r"\b\s", r"\s"),
            (r"^ to end$", " to end"),
            (r"\A to\Z", " to"),
            (r"(?!(?s:.*))\s", r"\s"),
            (r"(?=a(?:b|c)d) to ", " to "),
        ],
    )
    def test_every_zero_width_construct_is_deleted(self, text, expected):
        """A lookaround goes whole; an anchor or escape goes alone."""
        assert _assertion_free_superset(text, False) == expected

    @pytest.mark.parametrize(
        "text",
        [
            r"[\^$]",
            r"[^a]",
            r"\\b to",
            r"(?#a $ ^ comment)",
        ],
    )
    def test_text_that_only_looks_like_an_assertion_is_kept(self, text):
        r"""A class member, an escaped backslash and a comment stay put."""
        assert _assertion_free_superset(text, False) == text

    def test_a_capturing_group_becomes_non_capturing(self):
        """Same reason ``_detached_body`` gives: spliced copies collide."""
        assert _assertion_free_superset(r"( to)", False) == "(?: to)"
        assert _assertion_free_superset(r"(?P<a> to)", False) == "(?: to)"

    @pytest.mark.parametrize(
        "text",
        [r"\1 to", r"(?P=sep) to", r"(?(1) to)", r"(?(sep) to)"],
    )
    def test_what_cannot_be_widened_soundly_is_refused(self, text):
        """A leftover reference names a group this text does not carry."""
        assert _assertion_free_superset(text, False) is None

    def test_a_verbose_comment_is_skipped_only_while_x_is_on(self):
        r"""``#`` is comment text under x and a literal without it."""
        assert _assertion_free_superset("a # $ ^\nb", True) == "a # $ ^\nb"
        assert _assertion_free_superset("a # $ ^\nb", False) == "a #  \nb"

    def test_a_source_that_will_not_compile_answers_conservatively(self):
        """``_any_separator_sample_matches`` never guesses."""
        assert _any_separator_sample_matches("(", False)
        assert _any_separator_sample_matches(r"\s", False)
        assert not _any_separator_sample_matches(" to end ", False)


class TestTheDetachCheckReadsTheResolvedSlice:
    r"""Resolve first, then judge -- the .1.19 and .1.22 ordering.

    codex round 13 follow-up. After the .1.28 superset check went into
    the refusal branch, reading the RAW slice instead of the resolved
    one stopped changing any ANSWER: a differential over the 61
    fragments the transform asks about across 76 raw patterns from this
    file found 18 where the branch taken differs and 0 where the answer
    differs. That is sound -- resolution only ever inserts
    assertion-free text, either a body ``_detached_body`` already
    detached or ``ANY_TEXT``, so a raw slice that is detachable stays
    detachable, and inside the branch the superset check reproduces the
    compile test.

    The ordering is still the design, so it is pinned here directly:
    a slice whose only obstacle is a reference that resolves clean must
    never reach the refusal branch at all.
    """

    def test_a_resolvable_reference_never_reaches_the_refusal(
        self, monkeypatch
    ):
        r"""``\1`` resolves to a literal, so nothing is undetachable."""
        seen = []
        real = pattern_transform_module._assertion_free_superset

        def spy(text, verbose):
            seen.append(text)
            return real(text, verbose)

        monkeypatch.setattr(
            pattern_transform_module, "_assertion_free_superset", spy
        )
        assert not _matches_separators_only(
            r"\1 to ", (False,), {"\\1": "(?: x)"}
        )
        assert seen == []

    def test_an_unresolvable_slice_does_reach_the_refusal(
        self, monkeypatch
    ):
        r"""The spy proves the branch exists, so the test above can fail."""
        seen = []
        real = pattern_transform_module._assertion_free_superset

        def spy(text, verbose):
            seen.append(text)
            return real(text, verbose)

        monkeypatch.setattr(
            pattern_transform_module, "_assertion_free_superset", spy
        )
        assert not _matches_separators_only(r"(?<=\w) to ", (False,), {})
        assert seen == [r"(?<=\w) to "]


# ============================================================================
# A dropped lookaround takes its quantifier with it (.1.29)
# ============================================================================


class TestAQuantifiedLookaroundGoesWithItsQuantifier:
    r"""Deleting a lookaround must delete the quantifier that repeats it.

    ``_assertion_free_superset`` deletes a lookaround whole and then
    resumes copying, so ``\s(?=\w){4}`` came back as ``\s{4}``: the
    quantifier rebound to the ``\s`` in front of it and the widened
    text demanded FOUR separators where the original demanded one. That
    is a strict SUBSET, and a subset matching no separator sample
    proves nothing about the original -- the superset argument runs
    backwards (wh-number-words-one-parser.1.29).

    The plain body it let through cost 5.631s on
    ``^x (?:(\d+)\s(?=\w){4})+end$`` against a twenty-four word
    nonmatch, where the atomic body cost 0.0005s. The save-time probe
    returns None for that expression and the runtime matcher is
    unbounded, so nothing else stood between it and a hang.

    Deleting the pair is still a widening: a lookaround repeated any
    number of times consumes nothing, so removing it can only add
    matches.

    Only the lookaround needs this. ``^``, ``$`` and the ``\A \Z \b
    \B`` escapes cannot carry a quantifier at all -- ``^*``, ``\b*``,
    ``$+`` and ``\b{2}`` are each ``re.error: nothing to repeat``.
    """

    @pytest.mark.parametrize("tail", [
        r"\s(?=\w){4}",
        r"\s(?!\Z){4}",
        r"\s(?<=\s){4}",
        r"\s(?<!\S){4}",
    ])
    def test_every_lookaround_sign_takes_its_quantifier(self, tail):
        """All four spellings leaked the same ``{4}`` before the fix."""
        assert _assertion_free_superset(tail, False) == r"\s"

    @pytest.mark.parametrize("tail", [
        r"\s(?=\w)?",
        r"\s(?=\w)*",
        r"\s(?=\w)+",
        r"\s(?=\w){0}",
        r"\s(?=\w){2,4}",
        r"\s(?=\w){2,}",
        r"\s(?=\w){,4}",
        r"\s(?=\w){,}",
        r"\s(?=\w){4}?",
        r"\s(?=\w)*?",
        r"\s(?=\w){4}+",
        r"\s(?=\w)*+",
        r"\s(?=\w)(?# hidden){4}",
    ])
    def test_every_quantifier_spelling_goes_too(self, tail):
        r"""Every spelling re accepts here, including ``{,}`` and ``(?#``.

        The list was built by compiling each one on this interpreter,
        not read off the grammar: ``{,}`` is a quantifier and behaves as
        ``{0,}``, the possessive ``{4}+`` and ``*+`` are accepted on
        3.11 and later, and a ``(?#...)`` comment sits between a group
        and its quantifier without breaking the binding -- the same
        hiding place ``_repeat_quantifier_follows`` handles for .1.9.
        """
        assert _assertion_free_superset(tail, False) == r"\s"

    @pytest.mark.parametrize("tail,expected", [
        (r"\s(?=\w){foo}", r"\s{foo}"),
        (r"\s(?=\w){}", r"\s{}"),
        (r"\s(?=\w){1,2,3}", r"\s{1,2,3}"),
        (r"\s(?=\w){2,4", r"\s{2,4"),
        (r"\s(?=\w)x", r"\sx"),
        (r"\s(?=\w) ", r"\s "),
        (r"\s(?=\w)", r"\s"),
    ])
    def test_a_brace_that_is_not_a_quantifier_stays(self, tail, expected):
        r"""``{foo}``, ``{}`` and ``{1,2,3}`` are literal text to re.

        Eating them would delete characters the original must match, so
        the widened text would stop being a superset in the other
        direction. An unclosed ``{`` is literal for the same reason.
        """
        assert _assertion_free_superset(tail, False) == expected

    def test_a_quantifier_hidden_by_verbose_layout_goes_too(self):
        r"""Ignored whitespace and a ``#`` comment hide a quantifier."""
        assert _assertion_free_superset(
            "\\s(?=\\w) # note\n {4}", True
        ) == r"\s"

    def test_the_same_layout_is_literal_while_x_is_off(self):
        r"""Without x the space is text, so nothing may be consumed."""
        assert _assertion_free_superset(r"\s(?=\w) {4}", False) == r"\s {4}"

    def test_a_nested_group_inside_the_lookaround_goes_with_it(self):
        r"""The close that clears the drop is the lookaround's own."""
        assert _assertion_free_superset(r"\s(?=(a)(b)){4}", False) == r"\s"

    def test_a_quantifier_after_an_ordinary_group_is_untouched(self):
        r"""Only a DROPPED close consumes what follows it."""
        assert _assertion_free_superset(r"(?:ab){4}", False) == r"(?:ab){4}"

    def test_the_repeat_the_leaked_quantifier_hung_now_answers(self):
        r"""End to end: the atomic tail is chosen and the scan returns.

        ``PatternManager._probe_backtracking`` returns None for this
        raw expression, so the Advanced editor saves it and the
        unbounded runtime matcher runs it as written.
        """
        raw = r"^x (?:(\d+)\s(?=\w){4})+end$"
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY_ATOMIC in transformed
        match, ran_away = _bounded_fullmatch(
            transformed, "x " + ("one " * 24) + "!", 1.0
        )
        assert not ran_away
        assert match is None

    def test_the_widened_slice_still_answers_false_when_it_should(self):
        r"""A pinning literal under a quantified lookaround stays plain.

        The fix must not turn every quantified lookaround into a
        refusal: ``(?=\w){2} to end`` widens to `` to end``, which
        matches no separator sample, so the plain tail is still the
        right answer and the .1.28 class stays fixed.
        """
        assert not _matches_separators_only(r"(?=\w){2} to end ", (False,), {})


# ============================================================================
# An octal escape is a character, not a group reference (.1.30)
# ============================================================================


class TestAnOctalEscapeIsACharacterNotAReference:
    r"""``\040`` is a space, and refusing it costs a spoken form.

    Both walks refused every backslash-digit escape outright, on the
    reasoning that a surviving numeric reference names a group the
    detached text does not carry. That reasoning is right about a
    backreference and wrong about an octal literal, which names no
    group and consumes a character like any other escape.

    The cost is the .1.8 regression class returning:
    ``^go(?: (\d+)\040to end)+$`` is savable in the Advanced editor,
    and the refusal put the atomic tail on it. The atomic tail eats the
    " to" -- "to" is a count word -- and cannot give it back, so the
    command accepted "go 15 to end" and refused "go fifteen to end"
    (wh-number-words-one-parser.1.30).

    Where the escape ends was measured by compiling probes, not read
    off the documentation: ``\0`` takes up to two further octal digits,
    a leading 1-3 takes exactly two further octal digits, and every
    other backslash-digit escape is a group reference or an error and
    is still refused. ``(a)\123`` compiles even with one group present,
    so three octal digits are always a character.
    """

    @pytest.mark.parametrize("text,end", [
        (r"\0", 2),
        (r"\00", 3),
        (r"\000", 4),
        (r"\0000", 4),
        (r"\040", 4),
        (r"\0407", 4),
        (r"\08", 2),
        (r"\018", 3),
        (r"\123", 4),
        (r"\1234", 4),
        (r"\377", 4),
        (r"\200", 4),
    ])
    def test_the_scan_stops_where_the_escape_stops(self, text, end):
        assert _octal_escape_end(text, 0) == end

    @pytest.mark.parametrize("text", [
        r"\1", r"\7", r"\12", r"\99", r"\400", r"\777", r"\8", r"\9",
        r"\1x", r"\12x",
    ])
    def test_what_is_not_an_octal_literal_is_refused(self, text):
        """A backreference, or an escape re itself rejects."""
        assert _octal_escape_end(text, 0) is None

    @pytest.mark.parametrize("text,sample", [
        (r"\0407", " 7"),
        (r"\0000", "\x00" + "0"),
        (r"\1234", chr(0o123) + "4"),
    ])
    def test_the_scan_agrees_with_re_about_where_the_escape_ends(
        self, text, sample
    ):
        """re is the authority on the split, so ask re, not the grammar."""
        end = _octal_escape_end(text, 0)
        assert re.fullmatch(text, sample) is not None
        assert re.fullmatch(text[:end], sample[:1]) is not None
        assert text[end:] == sample[1:]

    def test_the_detach_walk_keeps_an_octal_escape(self):
        assert _detached_body(r"\040to end ", False) == r"\040to end "

    def test_the_superset_walk_keeps_an_octal_escape(self):
        assert _assertion_free_superset(
            r"\040(?<=\w)to end ", False
        ) == r"\040to end "

    def test_a_pinned_octal_space_answers_false(self):
        assert not _matches_separators_only(r"\040to end ", (False,), {})

    def test_a_separator_only_octal_still_answers_true(self):
        r"""``\040`` alone IS a separator, so the atomic tail is right."""
        assert _matches_separators_only(r"\040", (False,), {})

    def test_the_repeat_a_pinned_octal_space_holds_takes_the_plain_tail(self):
        raw = r"^go(?: (\d+)\040to end)+$"
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY in transformed
        assert re.fullmatch(transformed, "go fifteen to end", re.IGNORECASE)
        assert re.fullmatch(transformed, "go 15 to end", re.IGNORECASE)

    @pytest.mark.parametrize("fragment", [r"\1 ", r"\12 ", r"\8 "])
    def test_a_real_reference_is_still_refused_by_both_walks(self, fragment):
        assert _detached_body(fragment, False) is None
        assert _assertion_free_superset(fragment, False) is None
        assert _matches_separators_only(fragment, (False,), {})

    @pytest.mark.parametrize("raw", [
        r"^go(?P<sep>\040to end)(?: (\d+)(?P=sep))+$",
        r"^go(\040to end)(?: (\d+)\1)+$",
    ])
    def test_a_reference_to_an_octal_body_splices_that_body(self, raw):
        r"""``_detached_body``'s VALUE is used, not only its refusal.

        ``_reference_replacements`` splices a group's body in place of
        every reference to it, and substitutes ``ANY_TEXT`` when the
        body cannot be detached. ``ANY_TEXT`` matches every separator
        sample, so refusing an octal body puts the atomic tail on the
        repeat: the digit form still matches and the spoken form stops,
        which is the shape of the reported bug.

        This is the ONLY observable difference the detach walk's octal
        handling makes. Its other use -- the gate in
        ``_matches_separators_only`` -- is covered a second time by the
        .1.28 superset check, which reads the same octal escape through
        ``_octal_escape_end`` and reaches the same answer. Reverting the
        detach walk alone therefore leaves that path green, which is how
        the mutation first read as a survivor.
        """
        transformed, _ = transform_pattern(raw)
        assert NUMBER_CAPTURE_BODY in transformed
        assert re.fullmatch(transformed, "go to end 15 to end", re.IGNORECASE)
        assert re.fullmatch(
            transformed, "go to end fifteen to end", re.IGNORECASE
        )

    def test_an_octal_escape_inside_a_dropped_lookaround_goes_with_it(self):
        r"""The drop must not start copying an escape it is skipping."""
        assert _assertion_free_superset(
            r"a(?=\040b)c", False
        ) == r"ac"


class TestANumericEscapeIsReadTheWayReReadsIt:
    r"""A backslash-digit run is tokenized before any key is consulted.

    wh-number-words-one-parser.1.31: ``_resolve_references`` replaced
    the LONGEST matching key in the reference map, and the map carries
    a ``\N`` key for every capture in the pattern. re never reads more
    than two digits for a numeric reference, and it reads three octal
    digits as one character, so in a pattern with 165 captures the
    letter ``t`` (``\164``) was replaced by group 164's body.
    """

    @pytest.mark.parametrize(
        "text, references, expected",
        [
            # Three octal digits are one character, never a reference.
            (r"\123", {"\\123": "(?-x:Z)"}, r"\123"),
            (r"\164\157", {"\\164": "(?-x:Z)", "\\157": "(?-x:Y)"},
             r"\164\157"),
            (r"\040", {"\\40": "(?-x:Z)", "\\4": "(?-x:W)"}, r"\040"),
            (r"\0", {"\\0": "(?-x:Z)"}, r"\0"),
            # The two-digit head of an octal literal is not a reference
            # either, so a map entry for it must not be substituted.
            (r"\164", {"\\16": "(?-x:Y)"}, r"\164"),
            (r"\123x", {"\\12": "(?-x:Y)"}, r"\123x"),
            # A numeric reference is one or two digits and no more.
            (r"\198", {"\\198": "(?-x:Z)", "\\19": "(?-x:Y)"}, "(?-x:Y)8"),
            (r"\19", {"\\19": "(?-x:Y)"}, "(?-x:Y)"),
            (r"\89", {"\\89": "(?-x:Y)"}, "(?-x:Y)"),
            (r"\9", {"\\9": "(?-x:Y)"}, "(?-x:Y)"),
            (r"\1x", {"\\1": "(?-x:Y)"}, "(?-x:Y)x"),
            # A reference the map does not carry is copied unchanged.
            (r"\10", {"\\1": "(?-x:Y)"}, r"\10"),
            (r"\7", {"\\1": "(?-x:Y)"}, r"\7"),
        ],
    )
    def test_the_resolver_reads_the_token_re_reads(
        self, text, references, expected
    ):
        assert _resolve_references(text, (False,), references) == expected

    def test_re_refuses_an_out_of_range_octal_run(self):
        r"""``\401`` cannot reach this walk from a pattern that compiled.

        The walk reads it as group 40 followed by a literal 1, which
        differs from re only for text re refuses outright.
        """
        with pytest.raises(re.error):
            re.compile(r"\401")

    def test_an_octal_literal_beside_its_number_keeps_the_plain_tail(self):
        r"""The reported case: 165 captures and a ``\164`` literal."""
        raw = "^" + "()" * 164 + r"go(?: (\d+)\040\164\157)+$"
        assert re.compile(raw).groups == 165
        assert re.fullmatch(raw, "go 15 to")
        transformed, metadata = transform_pattern(raw)
        assert metadata["validation_group"] == "g165"
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert NUMBER_CAPTURE_BODY in transformed
        assert re.fullmatch(transformed, "go 15 to", re.IGNORECASE)
        assert re.fullmatch(transformed, "go fifteen to", re.IGNORECASE)

    def test_the_octal_literals_survive_the_resolve_walk(self):
        r"""The same slice, read directly, keeps its " to"."""
        references = {"\\164": "(?-x:)", "\\157": "(?-x:)"}
        assert _resolve_references(
            r" (\d+)\040\164\157", (False,), references
        ) == r" (\d+)\040\164\157"
        assert _matches_separators_only(
            r"\040\164\157", (False,), references
        ) is False

    def test_a_named_reference_still_resolves(self):
        r"""Dropping numeric keys from the length search kept the rest."""
        references = {"(?P=sep)": "(?-x: to)", "\\1": "(?-x: to)"}
        assert _resolve_references(
            "(?P=sep)", (False,), references
        ) == "(?-x: to)"
        assert _resolve_references("\\1", (False,), references) == "(?-x: to)"

    def test_a_numeric_escape_inside_a_class_is_left_alone(self):
        r"""Inside a class ``\1`` is an octal escape, not a reference."""
        assert _resolve_references(
            r"[\1]", (False,), {"\\1": "(?-x:Z)"}
        ) == r"[\1]"

    def test_a_numeric_escape_inside_a_comment_is_left_alone(self):
        assert _resolve_references(
            r"(?#\164)", (False,), {"\\164": "(?-x:Z)", "\\16": "(?-x:Y)"}
        ) == r"(?#\164)"


class TestANonAsciiDigitIsNotADigitToRe:
    r"""``str.isdigit`` is Unicode-aware; re's grammar is not.

    U+0662 (Arabic-Indic two), U+FF19 (fullwidth nine), U+00B2
    (superscript two) and U+2460 (circled one) all answer True to
    ``str.isdigit``, and re reads a backslash before any of them as an
    escaped literal -- never as an octal escape and never as a
    reference. Asking ``str.isdigit`` sent those slices down the
    numeric road, where ``_octal_escape_end`` refused them and both
    detach walks returned None, so the count took the atomic tail and
    could not give back a following count word
    (wh-number-words-one-parser.1.33).

    The characters are written as escapes here on purpose: this
    project's console is CP1252 and cannot print them.
    """

    DIGITS = ("\u0662", "\uff19", "\u00b2", "\u2460")

    @pytest.mark.parametrize("char", DIGITS)
    def test_re_reads_the_escape_as_the_character_itself(self, char):
        r"""The mismatch the fix is built on, measured on re itself."""
        assert char.isdigit() is True
        assert re.fullmatch("\\" + char, char)

    @pytest.mark.parametrize("char", DIGITS)
    def test_both_walks_keep_the_slice(self, char):
        fragment = "\\" + char + " to"
        assert _detached_body(fragment, False) == fragment
        assert _assertion_free_superset(fragment, False) == fragment

    @pytest.mark.parametrize("char", DIGITS)
    def test_the_slice_is_not_separators_only(self, char):
        fragment = "\\" + char + " to"
        assert _matches_separators_only(fragment, (False,), {}) is False

    @pytest.mark.parametrize("char", DIGITS)
    def test_the_reported_pattern_keeps_the_plain_tail(self, char):
        raw = "^go(?: (\\d+)(?:\\" + char + ")? to)+$"
        assert re.fullmatch(raw, "go 15 to")
        transformed, metadata = transform_pattern(raw)
        assert metadata["validation_group"] == "g1"
        assert NUMBER_CAPTURE_BODY_ATOMIC not in transformed
        assert NUMBER_CAPTURE_BODY in transformed
        assert re.fullmatch(transformed, "go 15 to", re.IGNORECASE)
        assert re.fullmatch(transformed, "go fifteen to", re.IGNORECASE)

    @pytest.mark.parametrize("char", DIGITS)
    def test_a_reference_before_one_still_resolves(self, char):
        r"""re stops the reference at ``\1``; so does the resolver now.

        This is the second of the two reads, the one that decides how
        many characters the token takes. A Unicode digit made it take
        one too many, so the token missed its key in the map and the
        reference was copied instead of resolved.
        """
        assert re.fullmatch("(Y)\\1" + char, "YY" + char)
        assert _resolve_references(
            "\\1" + char, (False,), {"\\1": "(?-x:Y)"}
        ) == "(?-x:Y)" + char

    def test_an_ascii_digit_after_a_reference_is_still_taken(self):
        r"""The ASCII road is unchanged: ``\12`` is one two-digit key."""
        assert _resolve_references(
            r"\12", (False,), {"\\12": "(?-x:Y)", "\\1": "(?-x:Z)"}
        ) == "(?-x:Y)"


class TestASpokenPhraseEndsAtATokenBoundary:
    r"""A literal right after the capture cannot take part of a word.

    Eleven count words are strict prefixes of other count words, and
    the word alternation can give the longer one back. Measured before
    the boundary was added, ``^go (\d+)teen$`` accepted "go nineteen"
    with the capture reading "nine", which the parser turns into 9 --
    while the raw expression refuses "go nineteen" outright. The
    widening adds the spoken forms of what the raw expression already
    matched; it must not add a form the raw expression rejects, and it
    must not report a different number
    (wh-number-words-one-parser.1.32).
    """

    PREFIX_PAIRS = [
        ("eighteen", "eight", "een"),
        ("eighty", "eight", "y"),
        ("forty", "for", "ty"),
        ("fourteen", "four", "teen"),
        ("nineteen", "nine", "teen"),
        ("ninety", "nine", "ty"),
        ("seventeen", "seven", "teen"),
        ("seventy", "seven", "ty"),
        ("sixteen", "six", "teen"),
        ("sixty", "six", "ty"),
        ("too", "to", "o"),
    ]

    def test_the_pair_list_is_every_strict_prefix_in_the_vocabulary(self):
        r"""A word added to the tables later cannot slip past this class."""
        words = set(_WORD_ALTERNATION.split("|"))
        found = {
            (whole, head, whole[len(head):])
            for whole in words
            for head in words
            if head != whole and whole.startswith(head)
        }
        assert found == set(self.PREFIX_PAIRS)

    @pytest.mark.parametrize("whole, head, suffix", PREFIX_PAIRS)
    def test_a_literal_suffix_cannot_split_the_word(
        self, whole, head, suffix
    ):
        assert whole == head + suffix
        raw = "^go (\\d+)" + re.escape(suffix) + "$"
        spoken = "go " + whole
        assert re.fullmatch(raw, spoken, re.IGNORECASE) is None
        transformed, _ = transform_pattern(raw)
        assert re.fullmatch(transformed, spoken, re.IGNORECASE) is None

    @pytest.mark.parametrize("whole, head, suffix", PREFIX_PAIRS)
    def test_the_digit_form_the_raw_expression_takes_still_matches(
        self, whole, head, suffix
    ):
        r"""The boundary sits on the word branch, so digits are untouched."""
        raw = "^go (\\d+)" + re.escape(suffix) + "$"
        digits = "go 9" + suffix
        assert re.fullmatch(raw, digits)
        transformed, _ = transform_pattern(raw)
        matched = re.fullmatch(transformed, digits, re.IGNORECASE)
        assert matched
        assert matched.group(1) == "9"

    def test_a_letter_after_the_capture_keeps_the_digit_branch(self):
        raw = r"^go (\d+)x$"
        transformed, _ = transform_pattern(raw)
        assert re.fullmatch(raw, "go 1x")
        assert re.fullmatch(transformed, "go 1x", re.IGNORECASE)
        assert re.fullmatch(transformed, "go onex", re.IGNORECASE) is None

    def test_a_phrase_that_ends_the_pattern_still_matches(self):
        transformed, _ = transform_pattern(r"^go (\d+)$")
        matched = re.fullmatch(transformed, "go nineteen", re.IGNORECASE)
        assert matched
        assert parse_number_word(matched.group(1)) == 19

    def test_a_hyphenated_phrase_still_matches(self):
        r"""A hyphen is a separator the tail reads, not a word split."""
        transformed, _ = transform_pattern(r"^go (\d+)$")
        matched = re.fullmatch(transformed, "go twenty-three", re.IGNORECASE)
        assert matched
        assert parse_number_word(matched.group(1)) == 23

    def test_a_count_word_follower_still_matches(self):
        r"""The .1.6 case: a following count word is still given back."""
        transformed, _ = transform_pattern(r"^set volume (\d+) to mute$")
        matched = re.fullmatch(
            transformed, "set volume fifteen to mute", re.IGNORECASE
        )
        assert matched
        assert parse_number_word(matched.group(1)) == 15

    @pytest.mark.parametrize("atomic", [False, True], ids=["plain", "atomic"])
    def test_both_bodies_carry_the_boundary(self, atomic):
        r"""The atomic form is defensive here.

        A capture whose next character is a letter is never dangerous
        -- the fragment after it is not separators-only, so
        ``_pick_number_capture_body`` takes the plain body -- but the
        two forms have to stay in step, so the boundary is measured on
        each of them directly.
        """
        body = NUMBER_CAPTURE_BODY_ATOMIC if atomic else NUMBER_CAPTURE_BODY
        assert re.fullmatch("(" + body + ")teen", "nineteen") is None
        assert re.fullmatch("(" + body + ")teen", "9teen")


class TestAHyphenAfterTheCaptureBelongsToTheCaller:
    r"""codex finding wh-number-words-one-parser.1.34.

    The widened tail joins two words of ONE spoken count with
    ``[\s-]``. When the raw expression ALSO consumes a hyphen after the
    capture, both readings match the same text, the greedy one wins,
    and the widened form finishes a different number of iterations than
    the raw expression. Measured at 121c4f2e, before the fix:

      ``^(?:(\d+)-)+$``   read "two-three-" with the capture at
                          "two-three", which the parser turns into 23,
                          where the raw expression reads "2-3-" with
                          the capture at "3";
      ``^(\d+)-$``        accepted "two-three-", which the raw
                          expression refuses as "2-3-". This shape needs
                          neither a repeat nor a second capture, so the
                          class is wider than the finding described.

    ``_hyphen_bounded_captures`` names the captures a hyphen follows and
    ``_pick_number_capture_body`` gives those the hyphen-free tail. The
    axis is separate from the atomic one, so there are four bodies.
    """

    def _bodies(self, transformed):
        """How many captures took each of the four bodies.

        Counted directly, with no subtraction:
        ``test_the_four_bodies_are_distinct`` proves no body is a
        substring of another, so no count can absorb another's hits.
        """
        return {
            "plain": transformed.count(NUMBER_CAPTURE_BODY),
            "atomic": transformed.count(NUMBER_CAPTURE_BODY_ATOMIC),
            "no_hyphen": transformed.count(NUMBER_CAPTURE_BODY_NO_HYPHEN),
            "atomic_no_hyphen": transformed.count(
                NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN
            ),
        }

    def test_the_four_bodies_are_distinct(self):
        """No body is a substring of another, so counting them is sound."""
        bodies = [
            NUMBER_CAPTURE_BODY,
            NUMBER_CAPTURE_BODY_ATOMIC,
            NUMBER_CAPTURE_BODY_NO_HYPHEN,
            NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN,
        ]
        assert len(set(bodies)) == 4
        for one in bodies:
            for other in bodies:
                if one is other:
                    continue
                assert one not in other, (one[:40], other[:40])

    def test_a_repeated_hyphen_group_keeps_the_raw_iteration_count(self):
        r"""``^(?:(\d+)-)+$`` must read "two-three-" the way it reads "2-3-"."""
        raw = r"^(?:(\d+)-)+$"
        assert re.fullmatch(raw, "2-3-").groups() == ("3",)

        transformed, metadata = transform_pattern(raw)
        assert metadata == {"validation_group": "g1"}
        match = re.fullmatch(transformed, "two-three-", re.IGNORECASE)

        assert match is not None, "the spoken form must still match"
        assert match.groups() == ("three",), (
            "the capture must hold the LAST iteration's word, as the raw "
            "expression does, not the whole hyphenated run"
        )
        assert parse_number_word(match.group(1)) == 3

    def test_a_trailing_hyphen_needs_no_repeat_and_no_second_capture(self):
        r"""``^(\d+)-$`` is the smallest shape in this class."""
        raw = r"^(\d+)-$"
        assert re.fullmatch(raw, "2-").groups() == ("2",)
        assert re.fullmatch(raw, "2-3-") is None

        transformed, _ = transform_pattern(raw)

        kept = re.fullmatch(transformed, "two-", re.IGNORECASE)
        assert kept is not None and kept.groups() == ("two",)
        assert re.fullmatch(transformed, "two-three-", re.IGNORECASE) is None, (
            "the raw expression refuses '2-3-', so the widened form must "
            "refuse its spoken shape too"
        )

    def test_the_digit_form_the_raw_expression_takes_still_matches(self):
        """Widening only ever ADDS spoken forms; the digits are untouched."""
        transformed, _ = transform_pattern(r"^(\d+)-$")
        match = re.fullmatch(transformed, "2-", re.IGNORECASE)
        assert match is not None and match.groups() == ("2",)

    def test_hyphen_separated_captures_each_take_one_word(self):
        r"""Five captures: every one reads its own word, as the raw does."""
        raw = r"^(\d+)-(\d+)-(\d+)-(\d+)-(\d+)$"
        assert re.fullmatch(raw, "2-3-4-5-6").groups() == (
            "2", "3", "4", "5", "6",
        )

        transformed, _ = transform_pattern(raw)
        match = re.fullmatch(
            transformed, "two-three-four-five-six", re.IGNORECASE
        )

        assert match is not None, (
            "before the fix the four atomic tails ate their hyphens and "
            "could not give them back, so this spoken form was refused"
        )
        assert match.groups() == ("two", "three", "four", "five", "six")

    def test_a_hyphen_before_the_capture_in_a_repeat_also_counts(self):
        r"""A repeat re-enters its own opening, so ``^(?:-(\d+))+$`` counts."""
        raw = r"^(?:-(\d+))+$"
        assert re.fullmatch(raw, "-2-3").groups() == ("3",)

        transformed, _ = transform_pattern(raw)
        match = re.fullmatch(transformed, "-two-three", re.IGNORECASE)

        assert match is not None
        assert match.groups() == ("three",), (
            "the hyphen the next iteration wants sits in the group's "
            "OPENING, to the left of the capture, not to its right"
        )

    def test_a_hyphen_before_a_capture_that_never_repeats_is_not_counted(self):
        r"""``^menu-bar (\d+)$`` must keep the ordinary hyphenated count."""
        transformed, _ = transform_pattern(r"^menu-bar (\d+)$")
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }

        match = re.fullmatch(
            transformed, "menu-bar twenty-three", re.IGNORECASE
        )
        assert match is not None
        assert parse_number_word(match.group(1)) == 23

    def test_a_capture_with_no_hyphen_after_it_keeps_the_hyphen_tail(self):
        """The commonest spoken form of a two-word count still reads."""
        transformed, _ = transform_pattern(r"^go (\d+)$")
        assert self._bodies(transformed)["plain"] == 1

        for spoken in ("go twenty-three", "go twenty three"):
            match = re.fullmatch(transformed, spoken, re.IGNORECASE)
            assert match is not None, spoken
            assert parse_number_word(match.group(1)) == 23, spoken

    def test_the_choice_is_made_per_capture(self):
        r"""In ``^(\d+)-(\d+)$`` only the FIRST capture has a hyphen after it."""
        transformed, _ = transform_pattern(r"^(\d+)-(\d+)$")
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 1, "atomic_no_hyphen": 0,
        }

        match = re.fullmatch(
            transformed, "twenty-three-four", re.IGNORECASE
        )
        assert match is not None
        assert match.groups() == ("twenty", "three-four"), (
            "capture 1 gave the hyphen to the caller; capture 2 has "
            "nothing after it and kept its own hyphen tail"
        )

    def test_the_atomic_axis_and_the_hyphen_axis_are_independent(self):
        r"""Both answers at once, and each one alone, are all reachable.

        ``^one(?:-(\d+))+$`` is dangerous AND hyphen-bounded: one
        iteration reaches the next across a bare "-", which is a
        separator sample, and that same "-" sits in the group opening a
        repeat re-enters.

        ``^one(?: (\d+)-)+$`` is hyphen-bounded and NOT dangerous: the
        text between two iterations is "- ", which no separator sample
        matches, so a literal pins where each iteration ends.
        """
        both, _ = transform_pattern(r"^one(?:-(\d+))+$")
        assert self._bodies(both) == {
            "plain": 0, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 1,
        }, self._bodies(both)

        hyphen_only, _ = transform_pattern(r"^one(?: (\d+)-)+$")
        assert self._bodies(hyphen_only) == {
            "plain": 0, "atomic": 0, "no_hyphen": 1, "atomic_no_hyphen": 0,
        }, self._bodies(hyphen_only)

    @pytest.mark.parametrize(
        "token",
        ["-", "(?:-)", "[-]", "[-a]", "[^a]", ".", r"\x2d", r"\055"],
    )
    def test_every_spelling_of_a_hyphen_reaches_the_same_answer(self, token):
        """The token is asked with re, so each spelling answers for itself."""
        transformed, _ = transform_pattern(r"^go (\d+)" + token + "$")
        assert self._bodies(transformed)["no_hyphen"] == 1, token

    @pytest.mark.parametrize(
        "token", ["x", "[a-z]", r"\s", r"\d", r"\w", " to end", "$"],
    )
    def test_a_token_that_cannot_be_a_hyphen_keeps_the_tail(self, token):
        """A hyphen character inside a RANGE is not a hyphen the tail loses."""
        transformed, _ = transform_pattern(r"^go (\d+)" + token + "$")
        assert self._bodies(transformed)["plain"] == 1, token

    def test_a_flag_group_hyphen_is_syntax_and_not_a_token(self):
        r"""``(?-x:`` holds a bare hyphen that consumes nothing."""
        transformed, _ = transform_pattern(r"(?x)^one(?: (?-x:\s(\d+)) )+$")
        counts = self._bodies(transformed)
        assert counts["atomic"] == 1, counts
        assert counts["atomic_no_hyphen"] == 0, counts

    def test_a_reference_is_resolved_before_the_scan(self):
        r"""``\1`` cannot compile alone, and its group holds " to end".

        Unresolved, the reference answers the conservative True and this
        capture loses its hyphen tail for nothing. Resolved, the slice
        reads "\040to end", which holds no hyphen, so the plain body
        stays -- the same body ``TestAnOctalEscapeIsACharacterNotARefer\
        ence`` already pins for this expression.
        """
        transformed, _ = transform_pattern(r"^go(\040to end)(?: (\d+)\1)+$")
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

    def test_the_right_hand_slice_runs_past_the_next_capture(self):
        r"""``^(\d+) (\d+)-$`` puts its hyphen beyond the second capture.

        The slice is deliberately the whole rest of the pattern rather
        than the text up to the next capture. Narrowing it would need
        the branch and quantifier reasoning this file has had to
        correct four times, and the wider read costs the shipped
        catalog nothing: all 113 shipped numeric captures still take
        the plain body.
        """
        transformed, _ = transform_pattern(r"^(\d+) (\d+)-$")
        assert self._bodies(transformed) == {
            "plain": 0, "atomic": 0, "no_hyphen": 2, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

    def test_a_comment_group_is_skipped_whole(self):
        """``(?#...)`` consumes nothing, so a hyphen inside it is not one."""
        transformed, _ = transform_pattern(r"^go (\d+)(?#a-b) end$")
        assert self._bodies(transformed)["plain"] == 1

    def test_the_shipped_catalog_keeps_the_hyphen_on_every_capture(self):
        """All 113 shipped numeric captures take the plain body."""
        catalog = (
            Path(__file__).resolve().parent.parent
            / "speech" / "config" / "patterns.toml"
        )
        data = tomllib.loads(catalog.read_text(encoding="utf-8"))
        entries = []
        for value in data.values():
            if isinstance(value, list):
                entries.extend(v for v in value if isinstance(v, dict))
            elif isinstance(value, dict):
                entries.append(value)

        totals = {
            "plain": 0, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }
        for entry in entries:
            raw = entry.get("pattern")
            if not isinstance(raw, str):
                continue
            transformed, metadata = transform_pattern(raw)
            if not isinstance(metadata, dict):
                continue
            if "validation_group" not in metadata:
                continue
            for key, value in self._bodies(transformed).items():
                totals[key] += value

        assert len(entries) == 320, len(entries)
        assert totals == {
            "plain": 113, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, totals


class TestAVerboseCommentCannotHideADelimiter:
    r"""codex finding wh-number-words-one-parser.1.35.

    ``_slice_can_consume_a_hyphen`` carried no verbose state at all, on
    the claim that reading an ignored ``#`` comment as ordinary pattern
    text was the conservative direction. The claim was false in one
    direction: comment text can OPEN a token that swallows the real
    delimiter behind it. Measured at c7b8255f, before the fix, on the
    three-line expression whose first line ends ``(\d+) # [a``:

    the ``[`` inside the comment opened a class spanning the newline,
    ``[a\n-#z]``, which matches no hyphen, so the scan answered False
    before it ever reached the real ``-``. The capture kept the
    hyphen-crossing tail and read "two-three-" as 23, where the raw
    expression reads "2-3-" with the capture at "3". That is the .1.8
    class of wrong number, not a refusal.

    The scan now carries the verbose mode in force where each slice
    begins, skips an active comment with ``_scan_verbose_comment``, and
    follows scoped ``(?x:`` and ``(?-x:`` changes.
    """

    def _bodies(self, transformed):
        return {
            "plain": transformed.count(NUMBER_CAPTURE_BODY),
            "atomic": transformed.count(NUMBER_CAPTURE_BODY_ATOMIC),
            "no_hyphen": transformed.count(NUMBER_CAPTURE_BODY_NO_HYPHEN),
            "atomic_no_hyphen": transformed.count(
                NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN
            ),
        }

    def _drops_the_hyphen(self, transformed):
        """Either hyphen-free body counts. A repeat turns the atomic
        axis on at the same time, and the axes are independent."""
        bodies = self._bodies(transformed)
        return bodies["no_hyphen"] + bodies["atomic_no_hyphen"]

    #: The right-hand slice. The comment's '[' opens a phantom class
    #: that eats the newline and the real delimiter behind it.
    HIDDEN_ON_THE_RIGHT = "(?x:^(?:(\\d+) # [a\n-#z]\n)+$)"

    #: The re-entered opening, same shape, hyphen before the capture.
    HIDDEN_IN_THE_OPENING = "(?x:^(?: # [a\n-#z]\n(\\d+))+$)"

    def test_the_phantom_class_really_does_refuse_a_hyphen(self):
        """The premise, measured rather than argued."""
        assert re.fullmatch("[a\n-#z]", "-") is None

    def test_a_comment_on_the_right_cannot_hide_the_delimiter(self):
        raw = self.HIDDEN_ON_THE_RIGHT
        assert re.fullmatch(raw, "2-3-").groups() == ("3",)

        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )

        match = re.fullmatch(transformed, "two-three-", re.IGNORECASE)
        assert match is not None, "the spoken form must still match"
        assert match.groups() == ("three",), (
            "the capture must hold the last iteration's word, as the raw "
            "expression does, not the whole hyphenated run"
        )
        assert parse_number_word(match.group(1)) == 3

    def test_a_comment_in_the_opening_cannot_hide_the_delimiter(self):
        raw = self.HIDDEN_IN_THE_OPENING
        assert re.fullmatch(raw, "-2-3").groups() == ("3",)

        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )

        match = re.fullmatch(transformed, "-two-three", re.IGNORECASE)
        assert match is not None
        assert match.groups() == ("three",)
        assert parse_number_word(match.group(1)) == 3

    def test_a_hyphen_inside_a_comment_is_not_a_delimiter(self):
        """The other direction. re skips the comment, so the tail keeps
        its hyphen. Reading the comment as pattern text refused a spoken
        form the raw expression accepts."""
        raw = "(?x:^go[ ](\\d+) # a-b\n$)"
        assert re.fullmatch(raw, "go 23").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

        match = re.fullmatch(transformed, "go twenty-three", re.IGNORECASE)
        assert match is not None
        assert parse_number_word(match.group(1)) == 23

    def test_a_globally_verbose_comment_is_skipped_too(self):
        r"""The mode can come from the pattern's own ``(?x)`` rather
        than from a scoped group, and the answer must be the same."""
        raw = "(?x)^go[ ](\\d+)#[ ]a-b\n$"
        assert re.fullmatch(raw, "go 23").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

    def test_a_comment_is_only_a_comment_in_verbose_mode(self):
        r"""Outside verbose mode a ``#`` is an ordinary literal, and the
        hyphen written after it is a real delimiter."""
        raw = r"^go (\d+)#-$"
        assert re.fullmatch(raw, "go 23#-").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed)["no_hyphen"] == 1, self._bodies(
            transformed
        )


    def test_a_scoped_mode_change_inside_the_slice_is_followed(self):
        r"""A slice can begin in verbose mode and then enter a scope
        that turns the x flag off, where a ``#`` is an ordinary literal
        and the hyphen written after it is a real delimiter."""
        raw = "(?x:^go[ ](\\d+)(?-x:[ ]#-)$)"
        assert re.fullmatch(raw, "go 23 #-").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed)["no_hyphen"] == 1, self._bodies(
            transformed
        )

    def test_the_mode_is_restored_when_a_scope_closes(self):
        r"""And the scope ends at its own ``)``: the ``#`` written after
        it is a comment again, so the hyphen inside that comment is not
        a delimiter."""
        raw = "(?x:^go[ ](\\d+)(?-x:z)#[ ]a-b\n$)"
        assert re.fullmatch(raw, "go 23z").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)


class TestOnlyAReEnteredOpeningCountsAsAHyphen:
    r"""codex finding wh-number-words-one-parser.1.36.

    ``_hyphen_bounded_captures`` read the opening of EVERY enclosing
    group, though its own reason for reading an opening is that a
    repeat re-enters it. A group that never repeats is entered once,
    before the capture, so a hyphen in its opening can never compete
    with the widened tail. Measured at c7b8255f, before the fix:
    ``^section(?:[- ]?(\d+))$`` took the hyphen-free body, so it matched
    "section twenty" and refused "section twenty-three", while the raw
    expression accepts both "section 23" and "section-23".

    The narrowing must not go further than that. An inner wrapper that
    carries no quantifier of its own, but sits inside an ancestor that
    DOES repeat, is traversed again every time the ancestor re-enters,
    so its opening still counts -- and it counts without a pass of its
    own, because the repeating ancestor's slice runs from that
    ancestor's content start to the capture and so already spans it.
    """

    def _bodies(self, transformed):
        return {
            "plain": transformed.count(NUMBER_CAPTURE_BODY),
            "atomic": transformed.count(NUMBER_CAPTURE_BODY_ATOMIC),
            "no_hyphen": transformed.count(NUMBER_CAPTURE_BODY_NO_HYPHEN),
            "atomic_no_hyphen": transformed.count(
                NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN
            ),
        }

    def test_a_group_that_never_repeats_keeps_the_hyphen(self):
        raw = r"^section(?:[- ]?(\d+))$"
        assert re.fullmatch(raw, "section 23").groups() == ("23",)
        assert re.fullmatch(raw, "section-23").groups() == ("23",)

        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

        match = re.fullmatch(
            transformed, "section twenty-three", re.IGNORECASE
        )
        assert match is not None, (
            "the raw expression accepts 'section-23', so its spoken "
            "hyphenated form must match too"
        )
        assert parse_number_word(match.group(1)) == 23

    def test_an_optional_group_still_never_repeats(self):
        r"""``?`` is not a repeat: the group is entered at most once."""
        raw = r"^section(?:[- ]?(\d+))?$"
        transformed, _ = transform_pattern(raw)
        assert self._bodies(transformed) == {
            "plain": 1, "atomic": 0, "no_hyphen": 0, "atomic_no_hyphen": 0,
        }, self._bodies(transformed)

    def _drops_the_hyphen(self, transformed):
        """Either hyphen-free body counts. The atomic axis is separate
        and a repeat can turn it on at the same time."""
        bodies = self._bodies(transformed)
        return bodies["no_hyphen"] + bodies["atomic_no_hyphen"]

    def test_a_wrapper_inside_a_repeat_still_counts(self):
        r"""The narrowing stops here. The inner group carries no
        quantifier of its own, but the outer one repeats, and the outer
        group's own opening slice reaches the capture, so it spans the
        inner opening's hyphen."""
        raw = r"^one(?:(?:-(\d+)) )+$"
        assert re.fullmatch(raw, "one-2 -3 ").groups() == ("3",)

        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )

    def test_the_group_that_repeats_over_the_capture_still_counts(self):
        r"""The original .1.34 opening shape is unchanged."""
        raw = r"^(?:-(\d+))+$"
        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )


class TestTheModeOutsideASliceIsRestored:
    r"""codex finding wh-number-words-one-parser.1.37.

    The .1.35 repair gave ``_slice_can_consume_a_hyphen`` a mode and a
    stack, but the stack started EMPTY. A right-hand slice that begins
    inside a scoped flag group and then reaches that group's closing
    ``)`` had nothing to pop, so the scan kept the inner mode for the
    text after the ``)`` -- text the outer scope really governs.

    Measured at 882bf54f, before this fix, on ``^one(?x:\s(\d+))#-$``:
    the slice ``)#-$`` stayed verbose, the outer ``#-`` read as a
    comment, and the scan never saw the real hyphen. The capture kept
    the hyphen-crossing tail and read "one two-three#-" as 23, where
    the raw expression accepts "one 2#-" and nothing else. That is the
    .1.8 class of wrong number, not a refusal.

    The opposite polarity fails the same way: a ``(?-x:`` capture
    inside an outer ``(?x)`` scope left the scan falsely non-verbose,
    so an outer comment was read as pattern text and hid the delimiter
    behind it.

    ``levels`` -- the whole chain of modes around the slice, outermost
    first -- is now passed to the scan, exactly as
    ``_balanced_for_compile`` already took it for the separator axis
    (wh-number-words-one-parser.1.16). Each unmatched ``)`` steps one
    level outward.
    """

    def _bodies(self, transformed):
        return {
            "plain": transformed.count(NUMBER_CAPTURE_BODY),
            "atomic": transformed.count(NUMBER_CAPTURE_BODY_ATOMIC),
            "no_hyphen": transformed.count(NUMBER_CAPTURE_BODY_NO_HYPHEN),
            "atomic_no_hyphen": transformed.count(
                NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN
            ),
        }

    def _drops_the_hyphen(self, transformed):
        bodies = self._bodies(transformed)
        return bodies["no_hyphen"] + bodies["atomic_no_hyphen"]

    def test_a_closed_verbose_scope_leaves_the_outer_hyphen_visible(self):
        r"""``(?x:`` around the capture, a plain ``#-`` outside it."""
        raw = "^one(?x:\\s(\\d+))#-$"
        assert re.fullmatch(raw, "one 2#-").groups() == ("2",)
        assert re.fullmatch(raw, "one two-three#-") is None

        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )
        assert re.fullmatch(
            transformed, "one two-three#-", re.IGNORECASE
        ) is None

    def test_a_closed_non_verbose_scope_restores_the_outer_comment(self):
        r"""``(?-x:`` around the capture inside an outer ``(?x)`` scope.

        The outer comment ``# [a`` opens a class that spans the newline
        and swallows the real hyphen, the .1.35 shape, reachable only
        once the mode is restored at the ``)``.
        """
        raw = "(?x)^one(?-x:\\s(\\d+)) # [a\n-#z]"
        assert re.fullmatch(raw, "one 2-").groups() == ("2",)
        assert re.fullmatch(raw, "one two-three-") is None

        transformed, _ = transform_pattern(raw)
        assert self._drops_the_hyphen(transformed) == 1, self._bodies(
            transformed
        )
        assert re.fullmatch(
            transformed, "one two-three-", re.IGNORECASE
        ) is None
