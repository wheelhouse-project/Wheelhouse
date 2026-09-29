"""The (\\d+) rule for numbers in patterns (wh-number-capture-enforce).

Every command that takes a number must capture it with (\\d+). The speech
engine returns "5" or "five" for the same spoken number, and only a group
whose whole body is \\d+ is widened by speech/pattern_transform.py to
accept both. Any other form -- \\d, [0-9], or a list of number words --
accepts only what it lists.

Two checks, both pure (no Qt) so the pattern editor and the tests share
them:

* ``count_capture_error`` (Rule A): a count parameter that reads a capture
  group (``g<N>``) must read a (\\d+) group.
* ``number_form_error`` (Rule B): a command expression must not match a
  number in any form other than (\\d+).

A named group (?P<name>\\d+) counts as (\\d+) in both rules, because the
transform widens it too (boss ruling 2026-09-27).

Both checks allow by default: when the expression does not compile, uses
the verbose flag, or the group walker below disagrees with ``re`` about
the number of capture groups, they return None and the other editor
checks decide.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field

from speech.action_catalog import CATALOG_BY_NAME
from speech.number_word_parser import ALIAS_WORDS, PHRASE_WORDS

# Named exemption from Rule B. The three shipped mouse grid patterns
# (grid-number-prefixed, grid-number-word, grid-number-digit in
# speech/config/patterns.toml) list 1 to 9 on purpose, to limit the match
# to the nine grid cells. Rule B ignores any offending text inside a
# capture group that a step of this function reads as a whole parameter.
# The rule is tied to the action, not to a doc_id list, so it covers
# Customize, Duplicate, and Edit alike; Duplicate drops the doc_id.
GRID_NUMBER_FUNCTION = "grid_number_command"

# The one accepted number capture body.
_NUMBER_BODY = r"\d+"

_GROUP_REF_RE = re.compile(r"g([1-9][0-9]*)")

# A quantifier directly after an atom: *, +, ?, or {m}, {m,}, {m,n}, {,n},
# with an optional lazy or possessive suffix.
_QUANTIFIER_RE = re.compile(r"(?:[*+?]|\{(?:\d+(?:,\d*)?|,\d*)\})[?+]?")

# Inline flags: (?flags) sets them for the whole expression; (?flags:...)
# is a non-capturing group with scoped flags.
_INLINE_FLAGS_RE = re.compile(r"[aiLmsux]*(?:-[imsx]*)?([:)])")

# Number words that make a list a number list. The STT homophones and
# "and" alone do not: (to|too) is a preposition, not a count.
_REAL_NUMBER_WORDS = PHRASE_WORDS - ALIAS_WORDS - {"and"}

# The separators between the words of a spoken number phrase.
_WORD_SPLIT_RE = re.compile(r"[\s-]+")

_DIGITS_MESSAGE = (
    "The part '{part}' does not accept number words such as 'five'. "
    "Capture every number with (\\d+), so it matches both '5' and 'five'."
)
_LIST_MESSAGE = (
    "The part '{part}' accepts only the numbers it lists. Capture every "
    "number with (\\d+), so it matches both '5' and 'five'."
)
_COUNT_MESSAGE = (
    "Step '{function}' reads a number from group g{n}, but that group is "
    "'{group}'. Capture the number with (\\d+) or (\\d+)?, so it matches "
    "both '5' and 'five'."
)


@dataclass
class _Group:
    start: int
    body_start: int
    capturing: bool
    end: int = -1
    alt_splits: list[int] = field(default_factory=list)


@dataclass
class _Class:
    start: int
    end: int
    negated: bool
    has_digit: bool
    accepts_letters: bool


@dataclass
class _Scan:
    groups: list[_Group]
    capturing: list[_Group]
    classes: list[_Class]
    digit_escapes: list[int]


def _accepts_every_letter(class_text: str) -> bool:
    """True when the character class accepts every letter a to z. Such a
    class also matches a one-word number such as "five". The class is
    compiled with re.IGNORECASE, as the runtime compiles every pattern
    (pattern_catalog.py), so [A-Z0-9] counts."""
    # crewcut: a scoped (?-i:...) group turns the runtime's IGNORECASE off,
    # so [A-Z0-9] inside it matches no lowercase word, yet it is accepted
    # here. Pass the scoped flags in to remove this limit.
    try:
        compiled = re.compile(class_text, re.IGNORECASE)
    except re.error:
        return False
    return all(compiled.fullmatch(c) for c in string.ascii_lowercase)


def _skip_to_close(expression: str, i: int) -> int | None:
    """Index just after the next ')' from ``i``, or None."""
    close = expression.find(")", i)
    return None if close < 0 else close + 1


def _scan(expression: str) -> _Scan | None:
    """Walk the expression source once. Returns None when the expression
    does not compile, uses the verbose flag, or the walk does not agree
    with ``re`` about the number of capture groups."""
    try:
        compiled = re.compile(expression)
    except (re.error, TypeError, ValueError, OverflowError):
        return None
    if compiled.flags & re.VERBOSE:
        # crewcut: verbose expressions (whitespace and # comments) are not
        # walked; both checks allow them. Teach the walker the verbose
        # syntax to remove this limit.
        return None

    n = len(expression)
    groups: list[_Group] = []
    classes: list[_Class] = []
    digit_escapes: list[int] = []
    stack: list[_Group] = []
    i = 0
    while i < n:
        c = expression[i]
        if c == "\\":
            if i + 1 < n and expression[i + 1] == "d":
                digit_escapes.append(i)
            i += 2
            continue
        if c == "[":
            j = i + 1
            negated = False
            if j < n and expression[j] == "^":
                negated = True
                j += 1
            if j < n and expression[j] == "]":
                j += 1
            # crewcut: has_digit is a text test (a digit character or \d in
            # the class). A negated class such as [^a-z], or a range such as
            # [!-@], accepts digits without writing one, and it saves.
            # Test the compiled class against each digit 0 to 9 to remove
            # this limit.
            has_digit = False
            while j < n and expression[j] != "]":
                if expression[j] == "\\":
                    if j + 1 < n and expression[j + 1] == "d":
                        has_digit = True
                    j += 2
                    continue
                if expression[j] in "0123456789":
                    has_digit = True
                j += 1
            if j >= n:
                return None
            classes.append(_Class(
                i, j + 1, negated, has_digit,
                _accepts_every_letter(expression[i:j + 1]),
            ))
            i = j + 1
            continue
        if c == "(":
            if expression.startswith("(?", i):
                rest = expression[i + 2:]
                if rest.startswith("P<"):
                    close = expression.find(">", i)
                    if close < 0:
                        return None
                    group = _Group(i, close + 1, capturing=True)
                elif rest.startswith("P=") or rest.startswith("#"):
                    # A backreference or a comment is an atom, not a group.
                    after = _skip_to_close(expression, i)
                    if after is None:
                        return None
                    i = after
                    continue
                elif rest[:1] in (":", "=", "!", ">"):
                    group = _Group(i, i + 3, capturing=False)
                elif rest.startswith("<=") or rest.startswith("<!"):
                    group = _Group(i, i + 4, capturing=False)
                elif rest.startswith("("):
                    # Conditional (?(id)yes|no): the body follows the id.
                    after = _skip_to_close(expression, i + 3)
                    if after is None:
                        return None
                    group = _Group(i, after, capturing=False)
                else:
                    flags = _INLINE_FLAGS_RE.match(rest)
                    if flags is None:
                        return None
                    after = i + 2 + flags.end()
                    if flags.group(1) == ")":
                        i = after
                        continue
                    group = _Group(i, after, capturing=False)
            else:
                group = _Group(i, i + 1, capturing=True)
            stack.append(group)
            i = group.body_start
            continue
        if c == ")":
            if not stack:
                return None
            group = stack.pop()
            group.end = i + 1
            groups.append(group)
            i += 1
            continue
        if c == "|" and stack:
            stack[-1].alt_splits.append(i)
        i += 1
    if stack:
        return None

    groups.sort(key=lambda g: g.start)
    capturing = [g for g in groups if g.capturing]
    if len(capturing) != compiled.groups:
        return None
    return _Scan(groups, capturing, classes, digit_escapes)


def capturing_group_spans(expression: str) -> list[tuple[int, int]] | None:
    """(index of '(', index just after the matching ')') for each capture
    group in group-number order, or None when the walk cannot be trusted
    (see ``_scan``)."""
    scan = _scan(expression)
    if scan is None:
        return None
    return [(g.start, g.end) for g in scan.capturing]


def _quantifier_at(expression: str, index: int) -> str:
    match = _QUANTIFIER_RE.match(expression, index)
    return match.group(0) if match else ""


def _body(expression: str, group: _Group) -> str:
    return expression[group.body_start:group.end - 1]


def _is_number_group(expression: str, group: _Group) -> bool:
    """True for (\\d+) or (?P<name>\\d+) with no following *, +, or {."""
    if not group.capturing or _body(expression, group) != _NUMBER_BODY:
        return False
    return _quantifier_at(expression, group.end)[:1] not in ("*", "+", "{")


def _group_ref(param, group_count: int) -> int | None:
    if not isinstance(param, str):
        return None
    match = _GROUP_REF_RE.fullmatch(param)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number <= group_count else None


def _number_param_indices(function: str, entry: dict, params: list) -> list[int]:
    """Positions in ``params`` that the catalog marks as a number."""
    if function == "hk":
        # The repeat is the last param only when it is an int, a digit
        # string, or a whole g<N> string: the same rule as _split_hk_params
        # in create_pattern_dialog.py, reproduced here to stay free of Qt.
        if not params:
            return []
        last = params[-1]
        if isinstance(last, bool):
            return []
        if isinstance(last, int) or (
            isinstance(last, str)
            and (last.isdigit() or _GROUP_REF_RE.fullmatch(last))
        ):
            return [len(params) - 1]
        return []
    if function == "run_capture":
        # The leading timeout is only an unquoted TOML number; a string in
        # that position is the program path, so a g<N> is never a timeout.
        return []
    catalog_params = entry.get("params", [])
    return [
        index
        for index in range(min(len(params), len(catalog_params)))
        if catalog_params[index].get("kind") == "number"
    ]


def count_capture_error(expression: str, steps) -> str | None:
    """Rule A: the first count parameter that reads a capture group other
    than (\\d+) or (\\d+)?, as the field error, or None."""
    scan = _scan(expression)
    if scan is None:
        return None
    for step in steps:
        function = step.get("function")
        entry = CATALOG_BY_NAME.get(function)
        if entry is None:
            continue
        params = list(step.get("params") or [])
        for index in _number_param_indices(function, entry, params):
            number = _group_ref(params[index], len(scan.capturing))
            if number is None:
                continue
            group = scan.capturing[number - 1]
            if _is_number_group(expression, group):
                continue
            quantifier = _quantifier_at(expression, group.end)
            group_text = expression[group.start:group.end]
            if quantifier[:1] in ("*", "+", "{"):
                group_text += quantifier
            return _COUNT_MESSAGE.format(
                function=function, n=number, group=group_text,
            )
    return None


def _exempt_spans(scan: _Scan, steps) -> list[tuple[int, int]]:
    spans = []
    for step in steps:
        if step.get("function") != GRID_NUMBER_FUNCTION:
            continue
        for param in step.get("params") or []:
            number = _group_ref(param, len(scan.capturing))
            if number is not None:
                group = scan.capturing[number - 1]
                spans.append((group.start, group.end))
    return spans


def _is_number_list(expression: str, group: _Group) -> bool:
    if not group.alt_splits:
        return False
    bounds = [group.body_start, *group.alt_splits, group.end - 1]
    alternatives = [
        expression[bounds[k] + (1 if k else 0):bounds[k + 1]].strip().lower()
        for k in range(len(bounds) - 1)
    ]

    def is_digits(text: str) -> bool:
        return text.isascii() and text.isdigit()

    # An alternative is a number phrase when every word in it is digits or
    # a number word: "one hundred" and "twenty-three" count.
    words = [
        [w for w in _WORD_SPLIT_RE.split(a) if w] for a in alternatives
    ]
    if not all(ws and all(is_digits(w) or w in PHRASE_WORDS for w in ws)
               for ws in words):
        return False
    return any(is_digits(w) or w in _REAL_NUMBER_WORDS
               for ws in words for w in ws)


def number_form_error(expression: str, steps) -> str | None:
    """Rule B: the first part of a command expression that matches a
    number in a form other than (\\d+), as the field error, or None.
    Replacements (no leading '^') are not checked."""
    if not expression.startswith("^"):
        return None
    scan = _scan(expression)
    if scan is None:
        return None
    exempt = _exempt_spans(scan, steps)

    def is_exempt(start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in exempt)

    # \d forms and character classes first.
    findings: list[tuple[int, int]] = []
    number_bodies = set()
    for group in scan.capturing:
        if _body(expression, group) != _NUMBER_BODY:
            continue
        number_bodies.add(group.body_start)
        quantifier = _quantifier_at(expression, group.end)
        if quantifier[:1] in ("*", "+", "{"):
            findings.append((group.start, group.end + len(quantifier)))
    for index in scan.digit_escapes:
        if index in number_bodies:
            continue
        quantifier = _quantifier_at(expression, index + 2)
        findings.append((index, index + 2 + len(quantifier)))
    for cls in scan.classes:
        if cls.has_digit and not cls.negated and not cls.accepts_letters:
            findings.append((cls.start, cls.end))
    for start, end in sorted(findings):
        if not is_exempt(start, end):
            return _DIGITS_MESSAGE.format(part=expression[start:end])

    # Then lists of numbers.
    for group in scan.groups:
        if _is_number_list(expression, group) and not is_exempt(
            group.start, group.end
        ):
            return _LIST_MESSAGE.format(part=expression[group.start:group.end])
    return None
