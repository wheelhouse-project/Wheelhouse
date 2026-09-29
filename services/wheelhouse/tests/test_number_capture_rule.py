"""Tests for speech/number_capture_rule.py (wh-number-capture-enforce).

Every command that takes a number must capture it with (\\d+), because
only that group is widened by speech/pattern_transform.py to accept both
digits and number words. Rule A refuses a count parameter that reads a
group of another shape; Rule B refuses a command expression that matches
a number any other way. Named groups (?P<name>\\d+) count as (\\d+): the
transform widens them too (boss ruling 2026-09-27).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from speech.number_capture_rule import (
    GRID_NUMBER_FUNCTION,
    capturing_group_spans,
    count_capture_error,
    number_form_error,
)

PATTERNS_TOML = (
    Path(__file__).resolve().parent.parent / "speech" / "config" / "patterns.toml"
)


def _digits_msg(part: str) -> str:
    return (
        f"The part '{part}' does not accept number words such as 'five'. "
        f"Capture every number with "
        f"(\\d+), so it matches both '5' and 'five'."
    )


def _list_msg(part: str) -> str:
    return (
        f"The part '{part}' accepts only the numbers it lists. Capture every "
        f"number with (\\d+), so it matches both '5' and 'five'."
    )


def _count_msg(function: str, n: int, group: str) -> str:
    return (
        f"Step '{function}' reads a number from group g{n}, but that group "
        f"is '{group}'. Capture the number with (\\d+) or (\\d+)?, so it "
        f"matches both '5' and 'five'."
    )


_NO_STEPS: list = []


# ---------------------------------------------------------------------------
# capturing_group_spans
# ---------------------------------------------------------------------------


class TestCapturingGroupSpans:
    def test_plain_groups_in_order(self):
        expr = r"^(a)(?:b)(c(d))$"
        assert capturing_group_spans(expr) == [(1, 4), (9, 15), (11, 14)]

    def test_named_group_is_capturing_backreference_is_not(self):
        expr = r"^(?P<x>a)(?P=x)$"
        spans = capturing_group_spans(expr)
        assert spans == [(1, 9)]

    def test_escaped_paren_and_class_paren_are_not_groups(self):
        expr = r"^\((a)[(]"
        assert capturing_group_spans(expr) == [(3, 6)]

    def test_bracket_first_in_class_and_escape_in_class(self):
        expr = r"^[]()][\](](b)$"
        assert capturing_group_spans(expr) == [(11, 14)]

    def test_lookarounds_and_inline_flags_are_not_capturing(self):
        expr = r"(?i)^(?=a)(?!b)(?<=c)(?<!d)(e)$"
        spans = capturing_group_spans(expr)
        assert len(spans) == 1
        start, end = spans[0]
        assert expr[start:end] == "(e)"

    def test_does_not_compile_returns_none(self):
        assert capturing_group_spans("^(a") is None

    def test_walk_that_disagrees_with_re_is_not_trusted(self):
        # A scoped verbose group (?x:...) makes '#' start a comment, which
        # the walker does not know: it counts the (1|2) inside the comment
        # as a group while re counts none. Both checks must then allow.
        expr = "^(?x:delete # (1|2)\n)$"
        assert capturing_group_spans(expr) is None
        assert number_form_error(expr, _NO_STEPS) is None


# ---------------------------------------------------------------------------
# Rule B: number_form_error
# ---------------------------------------------------------------------------


class TestRuleBRefusals:
    @pytest.mark.parametrize(
        "expr, part",
        [
            pytest.param(r"^delete \d+$", r"\d+", id="bare-d-plus"),
            pytest.param(r"^delete (\d)$", r"\d", id="group-of-one-d"),
            pytest.param(r"^delete (\d*)$", r"\d*", id="d-star"),
            pytest.param(r"^delete (\d+?)$", r"\d+?", id="d-plus-lazy"),
            pytest.param(r"^delete (?:\d+)$", r"\d+", id="noncapturing-d-plus"),
            pytest.param(r"^delete \d{2}$", r"\d{2}", id="d-count"),
            pytest.param(r"^delete (\d+)+$", r"(\d+)+", id="group-plus"),
            pytest.param(r"^delete (\d+)*$", r"(\d+)*", id="group-star"),
            pytest.param(r"^delete (\d+){2}$", r"(\d+){2}", id="group-count"),
            pytest.param(r"^delete (?P<n>\d+)+$", r"(?P<n>\d+)+", id="named-group-plus"),
            pytest.param(r"^delete ([0-9]+)$", "[0-9]", id="class-0-9"),
            pytest.param(r"^delete ([1-9])$", "[1-9]", id="class-1-9"),
            pytest.param(r"^delete ([\d.]+)$", r"[\d.]", id="class-d-dot"),
            # A class that accepts a digit is refused unless it also accepts
            # every letter a to z (boss ruling 02:13 2026-09-28).
            pytest.param(r"^delete ([0-9.,]+)$", "[0-9.,]", id="class-digits-and-punctuation"),
            pytest.param(r"^color ([0-9a-f]+)$", "[0-9a-f]", id="class-hex-digits"),
        ],
    )
    def test_digit_form_refused(self, expr, part):
        assert number_form_error(expr, _NO_STEPS) == _digits_msg(part)

    @pytest.mark.parametrize(
        "expr, part",
        [
            pytest.param(r"^delete (one|two|three)$", "(one|two|three)", id="words"),
            pytest.param(r"^delete (1|2|3)$", "(1|2|3)", id="digits"),
            pytest.param(r"^delete (one|2)$", "(one|2)", id="word-and-digit"),
            pytest.param(r"^delete (?:one|too)$", "(?:one|too)", id="noncapturing-word-and-homophone"),
            # Multi-word number phrases are number lists too (Codex round 1,
            # wh-number-capture-enforce.2.3).
            pytest.param(r"^delete (one hundred|two hundred)$", "(one hundred|two hundred)", id="multi-word-phrases"),
            pytest.param(r"^delete (twenty-three|forty-two)$", "(twenty-three|forty-two)", id="hyphenated-phrases"),
        ],
    )
    def test_number_list_refused(self, expr, part):
        assert number_form_error(expr, _NO_STEPS) == _list_msg(part)

    def test_digit_form_reported_before_number_list(self):
        expr = r"^(one|two) \d$"
        assert number_form_error(expr, _NO_STEPS) == _digits_msg(r"\d")

    def test_first_digit_form_in_the_expression_is_reported(self):
        # A class ahead of a \d escape is reported first, whatever order
        # the checks collect them in.
        expr = r"^([0-9]) (\d)$"
        assert number_form_error(expr, _NO_STEPS) == _digits_msg("[0-9]")


class TestRuleBAccepted:
    @pytest.mark.parametrize(
        "expr",
        [
            pytest.param(r"^delete (\d+)$", id="d-plus"),
            pytest.param(r"^back ?space\s*(\d+)?$", id="optional-d-plus"),
            pytest.param(r"^delete(?: (\d+))?$", id="optional-noncapturing-wrapper"),
            pytest.param(r"^delete (?P<n>\d+)$", id="named"),
            pytest.param(r"^delete (?P<n>\d+)?$", id="named-optional"),
            pytest.param(r"^delete ([^0-9]+)$", id="negated-class"),
            # [^0-9] also accepts every letter, so only this case needs the
            # negation check (mutation gate case).
            pytest.param(r"^delete ([^0-9a-z]+)$", id="negated-class-without-letters"),
            # A class that also accepts every letter a to z matches a
            # one-word number such as "five", so it saves.
            pytest.param(r"^open ([a-z0-9]+)$", id="class-letters-and-digits"),
            pytest.param(r"^open ([0-9a-z_]+)$", id="class-digits-first-letters"),
            # The runtime compiles every pattern with re.IGNORECASE, so
            # [A-Z0-9] matches "open five" (boss gate 03:16 2026-09-28).
            pytest.param(r"^open ([A-Z0-9]+)$", id="class-capitals-and-digits"),
            pytest.param(r"^type \\d$", id="escaped-backslash-then-d"),
            pytest.param(r"^go (to|too)$", id="homophones-only"),
            # "and" alone is no number either: a list of homophones and
            # "and" names no real number (mutation gate case).
            pytest.param(r"^go (to|and)$", id="homophone-and-and"),
            pytest.param(r"^select (one|all)$", id="list-with-a-non-number-word"),
            # A multi-word alternative is a number phrase only when every
            # word is a number word.
            pytest.param(r"^(one note|one drive)$", id="phrases-with-a-non-number-word"),
            pytest.param(r"^go (to|for) it$", id="homophone-aliases-only"),
            pytest.param(r"^delete (1|2|)$", id="empty-alternative"),
            pytest.param(r"^(\w+) and (\w+)$", id="and-between-words"),
            pytest.param(r"^delete$", id="no-number"),
        ],
    )
    def test_rule_b_accepts(self, expr):
        assert number_form_error(expr, _NO_STEPS) is None

    def test_replacement_expression_is_not_checked(self):
        assert number_form_error(r"\d+ percent", _NO_STEPS) is None

    def test_expression_that_does_not_compile_is_allowed(self):
        assert number_form_error(r"^(\d", _NO_STEPS) is None


class TestGridExemption:
    GRID_EXPR = r"^((one|two|three|four|five|six|seven|eight|nine|too|to|for|1|2|3)[.!?]?)$"

    def test_constant_names_the_grid_function(self):
        assert GRID_NUMBER_FUNCTION == "grid_number_command"

    def test_grid_list_referenced_by_grid_number_command_passes(self):
        steps = [{"function": "grid_number_command", "params": ["g1", "g2"]}]
        assert number_form_error(self.GRID_EXPR, steps) is None

    def test_same_expression_with_another_function_is_refused(self):
        steps = [{"function": "insert_text", "params": ["g1"]}]
        error = number_form_error(self.GRID_EXPR, steps)
        assert error == _list_msg(
            "(one|two|three|four|five|six|seven|eight|nine|too|to|for|1|2|3)"
        )

    def test_offending_part_outside_the_referenced_group_is_refused(self):
        expr = r"^(one|two) (\d)$"
        steps = [{"function": "grid_number_command", "params": ["g1"]}]
        assert number_form_error(expr, steps) == _digits_msg(r"\d")


# ---------------------------------------------------------------------------
# Rule A: count_capture_error
# ---------------------------------------------------------------------------


class TestRuleARefusals:
    def test_hk_repeat_on_word_group(self):
        steps = [{"function": "hk", "params": ["ctrl", "z", "g1"]}]
        assert count_capture_error(r"^undo (\w+)$", steps) == _count_msg(
            "hk", 1, r"(\w+)"
        )

    def test_press_repeat_on_number_list(self):
        steps = [{"function": "press", "params": ["del", "g1"]}]
        assert count_capture_error(r"^delete (one|two)$", steps) == _count_msg(
            "press", 1, "(one|two)"
        )

    def test_scroll_clicks_on_bare_digit_group(self):
        steps = [{"function": "scroll", "params": ["down", "g2"]}]
        expr = r"^scroll (down) (\d)$"
        assert count_capture_error(expr, steps) == _count_msg(
            "scroll", 2, r"(\d)"
        )

    def test_insert_newlines_count_on_word_group(self):
        steps = [{"function": "insert_newlines", "params": ["g1"]}]
        assert count_capture_error(r"^new lines (\w+)$", steps) == _count_msg(
            "insert_newlines", 1, r"(\w+)"
        )

    def test_repeated_group_is_refused(self):
        steps = [{"function": "press", "params": ["del", "g1"]}]
        assert count_capture_error(r"^delete (\d+)+$", steps) == _count_msg(
            "press", 1, r"(\d+)+"
        )

    def test_applies_to_replacements_too(self):
        steps = [{"function": "insert_newlines", "params": ["g1"]}]
        assert count_capture_error(r"(\w+) new lines", steps) == _count_msg(
            "insert_newlines", 1, r"(\w+)"
        )


class TestRuleAAccepted:
    @pytest.mark.parametrize(
        "expr, steps",
        [
            pytest.param(r"^undo\s*(\d+)?$", [{"function": "hk", "params": ["ctrl", "z", "g1"]}], id="hk-optional-d-plus"),
            pytest.param(r"^undo (\d+)$", [{"function": "hk", "params": ["ctrl", "z", "g1"]}], id="hk-d-plus"),
            pytest.param(r"^undo$", [{"function": "hk", "params": ["ctrl", "z", 3]}], id="hk-int-repeat"),
            pytest.param(r"^undo$", [{"function": "hk", "params": ["ctrl", "z", "3"]}], id="hk-digit-string-repeat"),
            pytest.param(r"^delete (?P<n>\d+)$", [{"function": "press", "params": ["del", "g1"]}], id="named"),
            pytest.param(r"^delete (?P<n>\d+)?$", [{"function": "press", "params": ["del", "g1"]}], id="named-optional"),
            pytest.param(r"^scroll down$", [{"function": "scroll", "params": ["down", 5]}], id="int-count"),
            pytest.param(r"^delete (\w+)$", [{"function": "insert_text", "params": ["g1"]}], id="text-param"),
            pytest.param(r"^x (\w+)$", [{"function": "no_such_function", "params": ["g1"]}], id="unknown-function"),
            pytest.param(r"^delete (\w+)$", [{"function": "press", "params": ["del", "g5"]}], id="ref-beyond-group-count"),
            pytest.param(r"^look (\w+)$", [{"function": "run_capture", "params": ["g1"]}], id="run-capture-program"),
        ],
    )
    def test_rule_a_accepts(self, expr, steps):
        assert count_capture_error(expr, steps) is None


# ---------------------------------------------------------------------------
# Every shipped pattern passes both checks (acceptance 4)
# ---------------------------------------------------------------------------


def test_every_shipped_pattern_passes_both_checks():
    with PATTERNS_TOML.open("rb") as handle:
        data = tomllib.load(handle)
    entries = data["pattern"]
    failures = []
    for entry in entries:
        expr = entry["pattern"]
        steps = [
            {"function": action["function"], "params": list(action.get("params", []))}
            for action in entry.get("actions", [])
        ]
        for check in (number_form_error, count_capture_error):
            error = check(expr, steps)
            if error is not None:
                failures.append((entry.get("doc_id"), expr, error))
    assert len(entries) >= 300
    assert failures == []
