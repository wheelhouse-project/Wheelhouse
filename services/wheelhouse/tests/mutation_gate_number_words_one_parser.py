r"""Mutation gate for the one number-word parser (wh-number-words-one-parser).

Run it from services/wheelhouse:

    python tests/mutation_gate_number_words_one_parser.py            # full sweep
    python tests/mutation_gate_number_words_one_parser.py --check    # patterns only
    python tests/mutation_gate_number_words_one_parser.py --only=a,b # a subset

David reported 2026-09-02 that "backspace fifteen" pressed Backspace once
and typed the word, and that any count above ten failed. Two word-to-integer
tables stopped at ten (speech/actions.py's _WORD_TO_INT_MAP and
speech/navigation/parser.py's _WORD_TO_INT) and the pattern loader widened a
numeric capture to a single ``(\w+)`` token, so a two-word count matched no
pattern at all. The fix removes both tables, routes every count through
speech/number_word_parser.py, and widens the capture to a body built from
that parser's own vocabulary.

The guard tests were written after the fix, so this gate is what proves each
of them can see its defect. Mutations, by target file:

speech/pattern_transform.py:
  widening-reverted-to-one-word   The capture is a single ``\w+`` again:
                                  the reported defect, all 113 shipped count
                                  patterns at once.

speech/number_word_parser.py:
  phrase-allows-no-extra-tokens   The regex body admits one token, so a
                                  multi-word count matches nothing.
  phrase-drops-the-hyphen-separator
                                  "twenty-three" stops matching. The
                                  input-level mutation: the space-separated
                                  form still works, so a test that only says
                                  "twenty three" passes here.
  phrase-vocabulary-loses-the-tens
                                  The body is built without the tens table,
                                  so "fifteen" still matches and "twenty
                                  three" does not -- the vocabulary really
                                  does come from the parser's tables.
  widening-body-adds-a-capturing-group
                                  One non-capturing group in the body becomes
                                  capturing. The loader records the capture
                                  number re.compile assigns and does NOT
                                  re-check it against the transformed
                                  pattern, so nothing in production catches
                                  this; the survey's group-count test is the
                                  only guard.
  phrase-tail-stops-being-atomic  The multi-word tail loses its atomic
                                  group, so a numeric capture inside a user's
                                  own repeat backtracks exponentially again.
                                  The catchers time out rather than assert,
                                  which IS the defect: the runtime bounds a
                                  match not at all, and these exceed the
                                  tests' own 2s budget.
  body-choice-always-plain        The widening always picks the plain body,
                                  so the dangerous shapes lose the atomic
                                  tail through the chooser rather than the
                                  body definition.
  body-choice-always-atomic       The widening always picks the atomic body,
                                  which is the state 08e2ed2c shipped: every
                                  literal follower that begins with a count
                                  word stops matching.
  quantifier-scan-stops-skipping  The scan for a repeating quantifier stops
                                  skipping (?#...), so a comment between the
                                  ')' and the '+' hides the repeat.
  quantifier-scan-ignores-verbose-space
                                  The scan stops skipping ignored whitespace
                                  in an active verbose scope, which hides the
                                  same repeat a different way.
  wrap-separator-test-always-dangerous
                                  Every enclosing repeat counts as dangerous
                                  again, whatever separates one iteration
                                  from the next.
  adjacency-test-always-true      Every sequential capture pair counts as
                                  adjacent, so captures separated by a real
                                  literal lose the plain tail.
  balancer-returns-the-raw-slice  The balancer hands back the raw slice, so a
                                  slice cutting a group boundary will not
                                  compile.
  balancer-ignores-unmatched-close
                                  An unmatched ')' stops reopening the scope
                                  it closed, so the slice will not compile.
  balancer-ignores-unmatched-open An unmatched '(' stops being closed, which
                                  breaks the same slice from the other end.
  balancer-forgets-the-scope-it-cut
                                  A cut ')' comes back as a plain '(?:', so
                                  the x mode after it is read as the mode
                                  inside the scope that ended.
  balancer-compiles-in-the-slice-start-mode
                                  The slice is compiled in the mode it starts
                                  in rather than the mode after the last cut.
  balancer-never-refuses-a-short-chain
                                  A slice that closes past its recorded chain
                                  is repaired by guesswork instead of
                                  answering dangerous.
  capture-levels-lose-the-outer-scopes
                                  Only the capture's own mode is recorded, so
                                  a slice has no parent scope to step out to.
  reference-resolution-dropped    A slice whose reference names a group
                                  outside it answers the conservative
                                  dangerous straight from the failed compile,
                                  which is the .1.17 defect itself.
  reference-map-not-built         The walk builds no reference map, so the
                                  retry has nothing to substitute and reaches
                                  the same wrong answer from the other end.
  reference-body-ignores-the-group-it-names
                                  Every reference resolves to a fixed literal
                                  instead of the body of the group it names,
                                  so a whitespace-only separator stops being
                                  recognised and the runaway comes back.
  reference-body-mode-not-carried The body is spliced in the slice's x mode
                                  rather than the mode inside the group it
                                  came from, so ignored layout becomes a
                                  literal requirement.
  reference-conditional-not-mapped
                                  A '(?(1)' conditional keeps no replacement,
                                  so a slice holding one still will not
                                  compile.
  reference-named-entry-dropped   A '(?P=name)' backreference keeps no
                                  replacement, so only the numeric form is
                                  resolved.
  resolver-splits-a-two-digit-reference
                                  The resolver substitutes the one-digit key
                                  inside '\10', changing what the slice
                                  means instead of leaving it to fail.
  resolver-drops-the-numeric-branch
                                  The backslash-digit tokenizer goes, so no
                                  numeric reference resolves at all.
  resolver-reads-an-octal-literal-as-a-reference
                                  The octal check goes, so the two-digit head
                                  of '\164' is substituted as a reference.
  resolver-token-takes-a-third-digit
                                  The reference token takes three digits,
                                  which re never reads.
  resolver-drops-an-absent-reference
                                  A reference the map does not carry is
                                  deleted instead of copied.
  resolver-second-digit-is-unicode-aware
                                  The second digit read asks str.isdigit, so
                                  a reference before a Unicode digit takes one
                                  character too many and misses its key.
  detach-digit-is-unicode-aware   The detach walk asks str.isdigit, so a
                                  backslash before a Unicode digit is read as
                                  a numeric escape and refused. The superset
                                  walk still recovers the plain tail, so only
                                  the walk's own test changes.
  superset-digit-is-unicode-aware The superset walk asks str.isdigit and
                                  refuses the same slice.

speech/number_word_parser.py (the phrase boundary):
  phrase-end-goes-from-the-plain-body
                                  The plain body loses its end assertion, so
                                  a literal after the capture takes the rest
                                  of a count word: 'go nineteen' matches
                                  '^go (\d+)teen$' with the capture at
                                  'nine'.
  phrase-end-goes-from-the-atomic-body
                                  The atomic body loses the same assertion.
                                  No shape the transform gives the atomic
                                  body to can reach it, so only the test that
                                  reads the body directly changes.
  phrase-end-covers-the-digit-branch
                                  The assertion moves outside the alternation
                                  and starts refusing digits too, so
                                  'go 1x' stops matching '^go (\d+)x$'.
  phrase-end-forbids-only-a-digit The assertion drops its letters, which is
                                  the half that splits a count word.
  resolver-substitutes-inside-a-class
                                  The resolver stops skipping character
                                  classes, so an octal escape inside one is
                                  rewritten as a backreference.
  resolver-substitutes-inside-a-comment
                                  The resolver stops skipping '(?#' comments,
                                  so reference text written as a comment is
                                  rewritten too.
  conditional-loses-its-empty-branch
                                  A rewritten conditional closes without the
                                  empty alternative, so an else-less
                                  conditional becomes a strict subset of what
                                  it can match.
  conditional-bar-not-recorded    A top-level '|' stops being recorded, so
                                  every conditional gets an empty branch it
                                  did not need.
  conditional-opener-pushes-no-level
                                  The rewritten opener stops taking a level of
                                  its own, so the conditional's ')' pops an
                                  enclosing group's mode.
  separator-test-compiles-before-resolving
                                  The compile is tried on the raw slice first
                                  and the resolver only on failure, which is
                                  the ordering that lets a slice rebind a
                                  numeric reference to a capture inside
                                  itself.
  reference-body-not-resolved-through
                                  A body is stored as written, so a reference
                                  inside it is never resolved.
  reference-bodies-resolved-in-the-wrong-order
                                  Capture numbers are walked from the highest
                                  down, so a body is resolved before the
                                  bodies it depends on.
  detached-body-allows-lookaround
                                  A body carrying a lookaround is spliced as
                                  written, so text that matches nothing once
                                  detached reads as a real word.
  detached-body-allows-anchors    '^' and '$' stop refusing a body, so an
                                  anchor is read against the sample instead of
                                  against the pattern it came from.
  detached-body-allows-context-escapes
                                  The '\\A \\Z \\b \\B' escapes stop refusing a
                                  body, and a leftover numeric reference stops
                                  refusing it too.
  detached-body-allows-a-named-reference
                                  A body still holding '(?P=' or '(?(' is
                                  spliced anyway, so it names a group the
                                  slice does not carry.
  detached-body-keeps-its-captures
                                  Capturing groups stay capturing, so one body
                                  spliced twice defines its name twice.
  detached-body-substitutes-inside-a-class
                                  The scanner stops skipping character
                                  classes, so a '^' inside one refuses the
                                  body.
  reference-bodies-walked-by-capture-number
                                  Groups are walked by capture number again,
                                  so an outer group is resolved before the
                                  nested group it references.
  refused-body-matches-nothing    ANY_TEXT stops matching every string, so a
                                  refused body reaches the opposite of the
                                  conservative answer.
  separator-test-ignores-verbose  The separator slice is compiled with no
                                  flags again, so ignored layout becomes a
                                  literal requirement.
  capture-levels-not-recorded     The walk stops recording each capture's own
                                  x mode, so every slice is read as
                                  non-verbose.
  brace-threshold-drops-to-two    The exact-count branch drops back to "more
                                  than one iteration" instead of the measured
                                  boundary, so {2} forces the atomic tail.
  brace-always-repeats            Every brace counts as a repeat again, so a
                                  bounded group forces the atomic tail.
  brace-never-repeats             No brace counts as a repeat, so a group that
                                  really repeats keeps the plain tail.
  brace-high-bound-ignored        The high bound stops being read, so {1,3}
                                  reads as bounded at one.
  branch-split-ignores-alternation
                                  The group stops being split on its own '|',
                                  so every capture is judged by the first
                                  branch's text.
  alternation-positions-not-recorded
                                  The walk stops recording where each group's
                                  own '|' sits, collapsing the same split from
                                  the other end.
  body-accepts-a-leading-separator
                                  The body gains a leading separator branch,
                                  which is the one change that would make a
                                  capture's own quantifier dangerous. The
                                  widening ignores that quantifier today
                                  precisely because the body cannot start
                                  with a separator.
  body-loses-the-filler-prefix    The optional leading "number" is gone
                                  from the body, so "click number three" and
                                  every filler shape stops matching while a
                                  plain count still works.
  filler-vocabulary-loses-the-plural
                                  The filler tuple keeps "number" and loses
                                  "numbers". Both the body and the parser's
                                  own check read that one tuple, so this
                                  proves the survey covers the plural rather
                                  than only the singular.
  parser-alias-step-dropped       The homophone rewrite never runs.
  parser-zero-option-ignored      The zero= option returns None again.

speech/actions.py:
  words-to-int-loses-the-word-path
                                  words_to_int reads digits only.
  words-to-int-loses-the-homophones
                                  It stops passing aliases=True.
  words-to-int-loses-zero         It stops passing zero=True.
  words-to-int-loses-the-digit-branch
                                  Its own digit branch is skipped and every
                                  digit string goes to the parser, which
                                  refuses anything above three digits. This
                                  is the red proof for the conversion-limit
                                  guard, which was green before this change
                                  (wh-voice-access-parity.2.3.2.6).

speech/navigation/parser.py:
  navigation-loses-the-cap        MAX_COUNT stops clamping.
  navigation-loses-the-homophones It stops passing aliases=True.
  navigation-accepts-zero         It starts passing zero=True, so "go right
                                  zero characters" becomes a command.

speech/pattern_manager.py:
  probe-corpus-loses-the-digit-run
                                  The save-time backtracking probe loses the
                                  digit probe. The three letter probes fail
                                  the widened body on the first character, so
                                  without it a nested numeric quantifier
                                  saves and reaches the live catalog.

Not mutated, and why. The ``crewcut:`` note in _parse_count records that it
still reads one token; that is a stated limit, not behaviour to pin.
speech/config/patterns.toml is unchanged by this fix, so it has nothing to
mutate -- the survey test enumerates the 113 patterns the change reaches
instead. The helpdoc wording carries no behaviour.

Each mutation runs only the test files that hold its catchers
(``selection``), so a sweep costs a few minutes rather than the whole suite
per mutation. Every distinct selection is run unmutated first and must be
green. The runner requires each pattern to match exactly once, compiles every
mutant before writing it, clears the target modules' bytecode caches around
every run, sets PYTHONDONTWRITEBYTECODE=1, gives each pytest run its own
timeout, restores the source with write_bytes in a finally that survives a
second Ctrl+C, and reports as ERRORS (never verdicts): pattern-not-found,
pattern-ambiguous, does-not-compile, timeout, suite-timeout abort, any pytest
return code other than 0 or 1, any ERROR line in the summary, an expected test
name that does not exist, and an expected test that failed for any reason
other than its own assertion.

``--check`` verifies every pattern matches exactly once and every mutant
compiles, without running a test, and prints
"checked N patterns, S stale, C that do not compile".

The gate detects each target file's own line endings and translates the
patterns to them before matching. It never rewrites a file's endings.

Never run this gate and a test suite at the same time in this worktree: the
gate rewrites the target sources underneath any other run.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
PT = SERVICE / "speech" / "pattern_transform.py"
NWP = SERVICE / "speech" / "number_word_parser.py"
ACT = SERVICE / "speech" / "actions.py"
NAV = SERVICE / "speech" / "navigation" / "parser.py"
PM = SERVICE / "speech" / "pattern_manager.py"
TRANSFORM = SERVICE / "speech" / "pattern_transform.py"
TARGETS = (PT, NWP, ACT, NAV, PM)

S_SURVEY = ["tests/test_count_pattern_survey.py"]
S_PIPE = ["tests/test_spoken_count_words.py"]
S_SURVEY_PIPE = S_SURVEY + S_PIPE
S_ACT = ["tests/test_actions.py"]
S_NAV = ["tests/test_navigation_parser.py"]
S_ACT_NAV = S_ACT + S_NAV
S_PM = ["tests/test_pattern_manager.py"]
S_TRANSFORM = ["tests/test_pattern_transform.py"]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]
PER_RUN_TIMEOUT_S = 300

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")



RESOLVER_SKIPS_BLOCK = """\
    stack: List[bool] = []
    # One entry per open group, in step with ``stack``: False or True
    # for a conditional this walk rewrote, saying whether its body has
    # shown a top-level '|' yet, and None for every ordinary group.
    conditional_bar: List[Optional[bool]] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "[":
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("(?#", i):
            end = _scan_comment(text, i)
            out.append(text[i:end])
            i = end
            continue
"""

SKIPS_NO_CLASS_BLOCK = """\
    stack: List[bool] = []
    # One entry per open group, in step with ``stack``: False or True
    # for a conditional this walk rewrote, saying whether its body has
    # shown a top-level '|' yet, and None for every ordinary group.
    conditional_bar: List[Optional[bool]] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if text.startswith("(?#", i):
            end = _scan_comment(text, i)
            out.append(text[i:end])
            i = end
            continue
"""

SKIPS_NO_COMMENT_BLOCK = """\
    stack: List[bool] = []
    # One entry per open group, in step with ``stack``: False or True
    # for a conditional this walk rewrote, saying whether its body has
    # shown a top-level '|' yet, and None for every ordinary group.
    conditional_bar: List[Optional[bool]] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "[":
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            continue
"""

BODY_WRAP = """\
        detached = _detached_body(
            _resolve_references(
                # A group's whole body is balanced, so one level is the
                # whole chain it needs.
                pattern_str[start:close], (inner,), references
            ),
            inner,
        )
        if detached is None:
            body = ANY_TEXT
        else:
            body = ("(?x:" if inner else "(?-x:") + detached
"""

BODY_WRAP_FIXED_TEXT = """\
        detached = _detached_body(
            _resolve_references(
                # A group's whole body is balanced, so one level is the
                # whole chain it needs.
                pattern_str[start:close], (inner,), references
            ),
            inner,
        )
        if detached is None:
            body = ANY_TEXT
        else:
            body = ("(?x:" if inner else "(?-x:") + "zzz"
"""

BODY_WRAP_MODE_LOST = """\
        detached = _detached_body(
            _resolve_references(
                # A group's whole body is balanced, so one level is the
                # whole chain it needs.
                pattern_str[start:close], (inner,), references
            ),
            inner,
        )
        if detached is None:
            body = ANY_TEXT
        else:
            body = "(?-x:" + detached
"""

BODY_WRAP_NOT_RESOLVED = """\
        detached = _detached_body(
            pattern_str[start:close],
            inner,
        )
        if detached is None:
            body = ANY_TEXT
        else:
            body = ("(?x:" if inner else "(?-x:") + detached
"""

#: ``_detached_body``'s own scanner head. The two lines before the loop
#: are what make this unique: ``_resolve_references`` carries a
#: ``conditional_bar`` declaration between them.
DETACHED_SKIPS_BLOCK = """\
    stack: List[bool] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "[":
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            continue
"""

DETACHED_SKIPS_NO_CLASS = """\
    stack: List[bool] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if False:
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            continue
"""

DETACHED_CAPTURE_BLOCK = """\
            out.append(
                "(?:" if opener == "(" or opener.startswith("(?P<")
                else opener
            )
"""

BY_CLOSE_BLOCK = """\
    by_close = sorted(
        capture_opens,
        key=lambda num: (
            group_close.get(capture_opens[num], len(pattern_str)), num
        ),
    )
"""

BY_NUMBER_BLOCK = """\
    by_close = sorted(capture_opens)
"""


RESOLVE_FIRST_BLOCK = """\
    try:
        compiled = re.compile(resolved, re.VERBOSE if verbose else 0)
    except re.error:
        return True
"""

COMPILE_FIRST_BLOCK = """\
    try:
        compiled = re.compile(text, re.VERBOSE if verbose else 0)
    except re.error:
        try:
            compiled = re.compile(resolved, re.VERBOSE if verbose else 0)
        except re.error:
            return True
"""

DETACH_CHECK_BLOCK = """\
    if _detached_body(resolved, verbose) is None:
"""

NO_DETACH_CHECK_BLOCK = """\
    if False:
"""

DETACH_BEFORE_RESOLVE_BLOCK = """\
    if _detached_body(text, verbose) is None:
"""

#: The .1.28 refinement: a refused slice is still asked whether a
#: SUPERSET of it can be separators, and a superset that matches no
#: sample proves the slice matches none either.
SUPERSET_CHECK_BLOCK = """\
        superset = _assertion_free_superset(resolved, verbose)
        if superset is not None and not _any_separator_sample_matches(
            superset, verbose
        ):
            return False
        return True
"""

NO_SUPERSET_CHECK_BLOCK = """\
        return True
"""

SUPERSET_COMPILE_FAILS_OPEN = """\
    try:
        widened = re.compile(source, re.VERBOSE if verbose else 0)
    except re.error:
        return False
"""

SUPERSET_COMPILE_FAILS_SHUT = """\
    try:
        widened = re.compile(source, re.VERBOSE if verbose else 0)
    except re.error:
        return True
"""

SUPERSET_DROPS_LOOKAROUND = """\
                if opener[:3] in ("(?=", "(?!") or opener[:3] == "(?<":
                    drop_at = depth
"""

SUPERSET_KEEPS_LOOKAROUND = """\
                if False:
                    drop_at = depth
"""

SUPERSET_ANCHOR_BLOCK = """\
        if char in "^$":
            i += 1
            continue
        if text.startswith("(?P=", i) or text.startswith("(?(", i):
            return None
"""

SUPERSET_KEEPS_ANCHORS = """\
        if char in "":
            i += 1
            continue
        if text.startswith("(?P=", i) or text.startswith("(?(", i):
            return None
"""

SUPERSET_ACCEPTS_A_REFERENCE = """\
        if char in "^$":
            i += 1
            continue
        if False:
            return None
"""

SUPERSET_ESCAPE_BLOCK = """\
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                if drop_at is None:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
"""

SUPERSET_KEEPS_BOUNDARY_ESCAPES = """\
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                if drop_at is None:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in "":
"""

#: A real backreference stops being refused: it is taken as a
#: two-character escape and copied into the widened slice, where it
#: names a group the slice does not carry.
SUPERSET_ACCEPTS_A_NUMERIC_ESCAPE = """\
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    end = i + 2
                if drop_at is None:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
"""

#: The .1.30 revert: an octal escape is refused again, so a slice a
#: literal pins takes the atomic tail and loses the spoken form.
SUPERSET_REFUSES_AN_OCTAL_ESCAPE = """\
            if nxt in _ASCII_DIGITS:
                return None
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
"""

#: The escape a drop is skipping is copied anyway, so a lookaround
#: leaks its own text into the widened slice.
SUPERSET_COPIES_A_DROPPED_ESCAPE = """\
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                if True:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
"""

SUPERSET_DROP_ENDS = """\
            if drop_at == depth:
                drop_at = None
"""

SUPERSET_DROP_NEVER_ENDS = """\
            if False:
                drop_at = None
"""

SUPERSET_CLASS_BLOCK = """\
        if char == "[":
            end = _scan_class(text, i)
            if drop_at is None:
                out.append(text[i:end])
            i = end
            continue
"""

SUPERSET_NO_CLASS_SCAN = """\
        if False:
            end = _scan_class(text, i)
            if drop_at is None:
                out.append(text[i:end])
            i = end
            continue
"""

#: The .1.29 fix: a dropped lookaround takes its quantifier with it.
SUPERSET_QUANTIFIER_GOES = """\
                i = _quantifier_after(text, i + 1, verbose)
"""

SUPERSET_QUANTIFIER_STAYS = """\
                i = i + 1
"""

#: The two lines that open the loop are part of the pattern because
#: the `(?#` skip itself is byte-identical to the one in
#: _resolve_raw_expression's verbose walk.
QUANTIFIER_SKIPS_A_COMMENT = """\
    j = i
    while j < n:
        if text.startswith("(?#", j):
            j = _scan_comment(text, j)
            continue
"""

QUANTIFIER_MISSES_A_COMMENT = """\
    j = i
    while j < n:
        if False:
            j = _scan_comment(text, j)
            continue
"""

QUANTIFIER_SKIPS_VERBOSE_LAYOUT = """\
        if verbose and text[j] in _VERBOSE_WS:
            j += 1
            continue
        if verbose and text[j] == "#":
            j = _scan_verbose_comment(text, j)
            continue
"""

QUANTIFIER_MISSES_VERBOSE_LAYOUT = """\
        if False and text[j] in _VERBOSE_WS:
            j += 1
            continue
        if False and text[j] == "#":
            j = _scan_verbose_comment(text, j)
            continue
"""

QUANTIFIER_CHECKS_THE_BRACE = """\
        if not inner or _BRACE_QUANTIFIER.fullmatch(inner) is None:
            return i
"""

QUANTIFIER_EATS_ANY_BRACE = """\
        if False:
            return i
"""

QUANTIFIER_KEEPS_THE_SUFFIX = """\
    if j < n and text[j] in "?+":
        j += 1
    return j
"""

QUANTIFIER_LOSES_THE_SUFFIX = """\
    if False:
        j += 1
    return j
"""

QUANTIFIER_READS_THE_BRACE_FORM = """\
    elif char == "{":
        close = text.find("}", j)
"""

QUANTIFIER_DROPS_THE_BRACE_FORM = """\
    elif False:
        close = text.find("}", j)
"""

#: An unclosed brace is literal text, so the scan must stop before
#: it. Returning the end of the text instead of ``i`` is the safe
#: spelling of "eat it": returning ``i`` itself from the wrong place
#: would restart the walk at index 0 and never terminate.
QUANTIFIER_STOPS_AT_AN_UNCLOSED_BRACE = """\
        if close == -1:
            return i
"""

QUANTIFIER_EATS_AN_UNCLOSED_BRACE = """\
        if close == -1:
            return n
"""

QUANTIFIER_READS_THE_STAR_FORMS = """\
    if char in "*+?":
        j += 1
"""

QUANTIFIER_DROPS_THE_STAR_FORMS = """\
    if char in "":
        j += 1
"""

#: The .1.30 revert on the detach walk.
DETACH_READS_AN_OCTAL_ESCAPE = """\
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                out.append(text[i:end])
                i = end
                continue
"""

DETACH_REFUSES_AN_OCTAL_ESCAPE = """\
            if nxt in _ASCII_DIGITS:
                return None
"""

OCTAL_LEADING_ZERO_FORM = """\
    if text[i + 1:i + 2] == "0":
"""

OCTAL_NO_LEADING_ZERO_FORM = """\
    if False:
"""

OCTAL_THREE_DIGIT_FORM = """\
            and head[0] in "123"):
"""

OCTAL_VALUE_ABOVE_THE_RANGE = """\
            and head[0] in "1234567"):
"""

OCTAL_ZERO_RUN = """\
        while end < n and end < i + 4 and text[end] in _OCTAL_DIGITS:
"""

OCTAL_ZERO_RUN_TOO_LONG = """\
        while end < n and end < i + 5 and text[end] in _OCTAL_DIGITS:
"""

OCTAL_ZERO_RUN_TOO_SHORT = """\
        while end < n and end < i + 3 and text[end] in _OCTAL_DIGITS:
"""

OCTAL_DIGIT_SET = """\
_OCTAL_DIGITS = frozenset("01234567")
"""

OCTAL_DIGIT_SET_TOO_WIDE = """\
_OCTAL_DIGITS = frozenset("0123456789")
"""


SEED_BEFORE_WALK = """\
    for number in capture_opens:
        references["(?({0})".format(number)] = "(?:"
        name = capture_names.get(number)
        if name:
            references["(?({0})".format(name)] = "(?:"
    by_close = sorted(
        capture_opens,
        key=lambda num: (
            group_close.get(capture_opens[num], len(pattern_str)), num
        ),
    )
    for number in by_close:
"""

SEED_INSIDE_WALK = """\
    by_close = sorted(
        capture_opens,
        key=lambda num: (
            group_close.get(capture_opens[num], len(pattern_str)), num
        ),
    )
    for number in by_close:
        references["(?({0})".format(number)] = "(?:"
        name = capture_names.get(number)
        if name:
            references["(?({0})".format(name)] = "(?:"
"""


# wh-number-words-one-parser.1.31: the resolver reads a backslash-digit
# run the way re reads it, before it consults any key in the map.
RESOLVE_NUMERIC_BRANCH = r"""        if char == "\\" and text[i + 1:i + 2] in _ASCII_DIGITS:
            end = _octal_escape_end(text, i)
            if end is not None:
                # An octal literal names no group, whatever number its
                # digits spell.
                out.append(text[i:end])
                i = end
                continue
            end = i + 3 if text[i + 2:i + 3] in _ASCII_DIGITS else i + 2
            token = text[i:end]
            # A reference the map does not carry is copied unchanged,
            # exactly as an unresolvable one always was.
            out.append(references.get(token, token))
            i = end
            continue
"""

RESOLVE_NUMERIC_BRANCH_GONE = r"""        if char == "\\" and text[i + 1:i + 2] in _ASCII_DIGITS:
            out.append(text[i:i + 2])
            i += 2
            continue
"""

# The opening line comes with it: the same call, indented deeper,
# stands in both of the other two walks, and a bare slice of it is a
# substring of theirs.
RESOLVE_OCTAL_CHECK = r"""        if char == "\\" and text[i + 1:i + 2] in _ASCII_DIGITS:
            end = _octal_escape_end(text, i)
"""

RESOLVE_OCTAL_CHECK_OFF = r"""        if char == "\\" and text[i + 1:i + 2] in _ASCII_DIGITS:
            end = None
"""

RESOLVE_TOKEN_END = r"""            end = i + 3 if text[i + 2:i + 3] in _ASCII_DIGITS else i + 2
"""

RESOLVE_TOKEN_END_THREE = r"""            end = i + 4 if text[i + 2:i + 3] in _ASCII_DIGITS else i + 2
"""

RESOLVE_TOKEN_END_ONE = r"""            end = i + 2
"""

RESOLVE_ABSENT_KEY = r"""            out.append(references.get(token, token))
"""

RESOLVE_ABSENT_KEY_DROPPED = r"""            out.append(references.get(token, ""))
"""


# wh-number-words-one-parser.1.33: the three reads that answer
# differently once ``str.isdigit`` is asked instead of _ASCII_DIGITS.
# The first read in _resolve_references carries no mutation: reverting
# it alone changes no output, because the branch it opens copies a
# non-ASCII digit escape verbatim and resumes where the default road
# would have, which is measured in the commit that added this.
RESOLVE_TOKEN_END_UNICODE = r"""            end = i + 3 if text[i + 2:i + 3].isdigit() else i + 2
"""

DETACH_DIGIT_IS_UNICODE_AWARE = """\
            if nxt.isdigit():
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                out.append(text[i:end])
                i = end
                continue
"""

SUPERSET_DIGIT_IS_UNICODE_AWARE = """\
            if nxt.isdigit():
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                if drop_at is None:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
"""


# wh-number-words-one-parser.1.32: the spoken-word branch of each body
# ends at a token boundary, so a literal written right after the
# capture cannot take the rest of a count word.
PHRASE_END_PLAIN = r"""    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"
"""

PHRASE_END_PLAIN_GONE = r"""    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}))"
"""

PHRASE_END_PLAIN_OVER_THE_DIGITS = r"""    rf"(?:(?:\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL})){_PHRASE_END})"
"""

PHRASE_END_ATOMIC = r"""    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?>{_PHRASE_TAIL}){_PHRASE_END})"
"""

PHRASE_END_ATOMIC_GONE = r"""    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?>{_PHRASE_TAIL}))"
"""

PHRASE_END_CLASS = r"""_PHRASE_END = r"(?![A-Za-z0-9])"
"""

PHRASE_END_DIGITS_ONLY = r"""_PHRASE_END = r"(?![0-9])"
"""


TAIL_NO_HYPHEN = r"""_PHRASE_TAIL_NO_HYPHEN = (
    rf"(?:[\s](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"
)
"""

TAIL_NO_HYPHEN_WITH_ONE = r"""_PHRASE_TAIL_NO_HYPHEN = (
    rf"(?:[\s-](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"
)
"""


MUTATIONS = [
    {
        # The refused slice goes straight to the conservative answer
        # again, so a mandatory literal that pins the repeat is read as
        # a separator and the atomic tail eats the count word "to"
        # (wh-number-words-one-parser.1.28).
        "name": "superset-check-removed",
        "src": TRANSFORM,
        "old": SUPERSET_CHECK_BLOCK,
        "new": NO_SUPERSET_CHECK_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_pinned_repeat_keeps_the_plain_tail",
            "test_a_refused_slice_holding_a_literal_answers_false",
            "test_a_pinning_literal_under_a_negation_answers_false",
        ],
    },
    {
        # A lookaround is emitted instead of dropped, so the "superset"
        # is the slice itself: it means nothing detached, matches no
        # sample, and the answer flips to False for a wrap that really
        # is separator-only. That is the .1.24 and .1.25 harm reached
        # from the other side -- anti-conservative rather than
        # over-conservative.
        "name": "superset-keeps-the-lookaround",
        "src": TRANSFORM,
        "old": SUPERSET_DROPS_LOOKAROUND,
        "new": SUPERSET_KEEPS_LOOKAROUND,
        "selection": S_TRANSFORM,
        "expect": [
            "test_every_zero_width_construct_is_deleted",
            "test_a_separator_only_context_slice_still_takes_the_atomic_tail",
            "test_a_widened_reference_under_a_negation_still_answers_true",
            "test_a_context_sensitive_slice_takes_the_atomic_tail",
        ],
    },
    {
        # The drop never ends, so everything after a lookaround is
        # deleted too. The empty string is one of the samples, so the
        # widened slice always matches and the .1.28 answer is lost.
        "name": "superset-drop-never-ends",
        "src": TRANSFORM,
        "old": SUPERSET_DROP_ENDS,
        "new": SUPERSET_DROP_NEVER_ENDS,
        "selection": S_TRANSFORM,
        "expect": [
            "test_every_zero_width_construct_is_deleted",
            "test_a_pinned_repeat_keeps_the_plain_tail",
            "test_a_refused_slice_holding_a_literal_answers_false",
        ],
    },
    {
        # '^' and '$' survive into the widened slice, so an anchored
        # wrap can never fullmatch a sample and the pinning literal is
        # not what decided the answer.
        "name": "superset-keeps-the-anchors",
        "src": TRANSFORM,
        "old": SUPERSET_ANCHOR_BLOCK,
        "new": SUPERSET_KEEPS_ANCHORS,
        "selection": S_TRANSFORM,
        "expect": ["test_every_zero_width_construct_is_deleted"],
    },
    {
        # The boundary escapes survive, which is the same harm for
        # '\b', '\B', '\A' and '\Z'.
        "name": "superset-keeps-the-boundary-escapes",
        "src": TRANSFORM,
        "old": SUPERSET_ESCAPE_BLOCK,
        "new": SUPERSET_KEEPS_BOUNDARY_ESCAPES,
        "selection": S_TRANSFORM,
        # Not test_a_pinned_repeat_keeps_the_plain_tail: its '\\b' shape
        # keeps its answer under this mutation, because ' to end ' still
        # matches no sample whether the '\\b' survives or not. The harm
        # is on the separator-only side, where a surviving '\\b' stops the
        # widened slice matching a sample it should match.
        "expect": [
            "test_every_zero_width_construct_is_deleted",
            "test_a_boundary_escape_alone_answers_conservatively",
            "test_a_context_sensitive_slice_takes_the_atomic_tail",
            "test_a_separator_only_context_slice_still_takes_the_atomic_tail",
        ],
    },
    {
        # A '(?P=' or '(?(' that survived resolution is walked instead
        # of refused, so the widened text counts by a numbering it does
        # not carry.
        "name": "superset-accepts-a-leftover-reference",
        "src": TRANSFORM,
        "old": SUPERSET_ANCHOR_BLOCK,
        "new": SUPERSET_ACCEPTS_A_REFERENCE,
        "selection": S_TRANSFORM,
        "expect": ["test_what_cannot_be_widened_soundly_is_refused"],
    },
    {
        # A numeric escape is walked instead of refused, so '\1' can
        # bind to whichever group the widened text happens to have.
        "name": "superset-accepts-a-numeric-escape",
        "src": TRANSFORM,
        "old": SUPERSET_ESCAPE_BLOCK,
        "new": SUPERSET_ACCEPTS_A_NUMERIC_ESCAPE,
        "selection": S_TRANSFORM,
        "expect": ["test_what_cannot_be_widened_soundly_is_refused"],
    },
    {
        # The class is walked character by character, so a '^' or '$'
        # inside it is deleted as though it were an anchor.
        "name": "superset-loses-the-class-scan",
        "src": TRANSFORM,
        "old": SUPERSET_CLASS_BLOCK,
        "new": SUPERSET_NO_CLASS_SCAN,
        "selection": S_TRANSFORM,
        "expect": ["test_text_that_only_looks_like_an_assertion_is_kept"],
    },
    {
        # A widened slice that will not compile answers False, so the
        # caller reads "no sample matched" and hands back the plain body
        # for a wrap it never managed to test.
        "name": "superset-compile-failure-answers-false",
        "src": TRANSFORM,
        "old": SUPERSET_COMPILE_FAILS_SHUT,
        "new": SUPERSET_COMPILE_FAILS_OPEN,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_source_that_will_not_compile_answers_conservatively",
        ],
    },
    {
        # The .1.29 revert: the lookaround goes, the quantifier stays,
        # and the quantifier rebinds to whatever stands in front of it.
        # `\s(?=\w){4}` widens to `\s{4}`, a strict SUBSET, so a slice
        # that really is separator-only reads as one that is not and the
        # plain body goes on a repeat that hangs for 5.631s.
        "name": "superset-keeps-the-quantifier",
        "src": TRANSFORM,
        "old": SUPERSET_QUANTIFIER_GOES,
        "new": SUPERSET_QUANTIFIER_STAYS,
        "selection": S_TRANSFORM,
        "expect": [
            "test_every_lookaround_sign_takes_its_quantifier",
            "test_every_quantifier_spelling_goes_too",
            "test_a_quantifier_hidden_by_verbose_layout_goes_too",
            "test_a_nested_group_inside_the_lookaround_goes_with_it",
            "test_the_repeat_the_leaked_quantifier_hung_now_answers",
            "test_the_widened_slice_still_answers_false_when_it_should",
        ],
    },
    {
        # A `(?#...)` comment between the `)` and the quantifier hides
        # it, so the scan stops before reaching it.
        "name": "quantifier-scan-misses-a-hidden-comment",
        "src": TRANSFORM,
        "old": QUANTIFIER_SKIPS_A_COMMENT,
        "new": QUANTIFIER_MISSES_A_COMMENT,
        "selection": S_TRANSFORM,
        "expect": ["test_every_quantifier_spelling_goes_too"],
    },
    {
        # Ignored whitespace and a `#` comment hide a quantifier the
        # same way while the x flag is on.
        "name": "quantifier-scan-misses-verbose-layout",
        "src": TRANSFORM,
        "old": QUANTIFIER_SKIPS_VERBOSE_LAYOUT,
        "new": QUANTIFIER_MISSES_VERBOSE_LAYOUT,
        "selection": S_TRANSFORM,
        "expect": ["test_a_quantifier_hidden_by_verbose_layout_goes_too"],
    },
    {
        # Every brace counts as a quantifier, so the literal `{foo}`,
        # `{}` and `{1,2,3}` are eaten and the widened text stops being
        # a superset from the other side.
        "name": "quantifier-scan-eats-a-literal-brace",
        "src": TRANSFORM,
        "old": QUANTIFIER_CHECKS_THE_BRACE,
        "new": QUANTIFIER_EATS_ANY_BRACE,
        "selection": S_TRANSFORM,
        "expect": ["test_a_brace_that_is_not_a_quantifier_stays"],
    },
    {
        # The lazy and possessive suffixes stay behind, so `{4}?` leaves
        # a `?` that rebinds to the text in front.
        "name": "quantifier-scan-loses-the-lazy-suffix",
        "src": TRANSFORM,
        "old": QUANTIFIER_KEEPS_THE_SUFFIX,
        "new": QUANTIFIER_LOSES_THE_SUFFIX,
        "selection": S_TRANSFORM,
        "expect": ["test_every_quantifier_spelling_goes_too"],
    },
    {
        # The brace form stops being read at all, so only `*`, `+` and
        # `?` are taken and `{4}` leaks.
        "name": "quantifier-scan-drops-the-brace-form",
        "src": TRANSFORM,
        "old": QUANTIFIER_READS_THE_BRACE_FORM,
        "new": QUANTIFIER_DROPS_THE_BRACE_FORM,
        "selection": S_TRANSFORM,
        "expect": [
            "test_every_lookaround_sign_takes_its_quantifier",
            "test_every_quantifier_spelling_goes_too",
        ],
    },
    {
        # An unclosed `{` is literal text, so eating to the end of the
        # slice deletes characters the original must match.
        "name": "quantifier-scan-eats-an-unclosed-brace",
        "src": TRANSFORM,
        "old": QUANTIFIER_STOPS_AT_AN_UNCLOSED_BRACE,
        "new": QUANTIFIER_EATS_AN_UNCLOSED_BRACE,
        "selection": S_TRANSFORM,
        "expect": ["test_a_brace_that_is_not_a_quantifier_stays"],
    },
    {
        # `*`, `+` and `?` stop being taken, so only the brace form is.
        "name": "quantifier-scan-drops-the-star-forms",
        "src": TRANSFORM,
        "old": QUANTIFIER_READS_THE_STAR_FORMS,
        "new": QUANTIFIER_DROPS_THE_STAR_FORMS,
        "selection": S_TRANSFORM,
        "expect": ["test_every_quantifier_spelling_goes_too"],
    },
    {
        # The .1.30 revert on the detach walk: `\040` is refused as
        # though it named a group, so a slice a literal pins takes the
        # atomic tail and the spoken form stops matching.
        # The two catchers that read through _matches_separators_only
        # were dropped from this list after the first run reported a
        # survivor: the .1.28 superset check reads the same octal escape
        # and reaches the same answer, so reverting the detach walk
        # alone leaves that path green. What is left is the walk's own
        # output plus the one caller that uses its VALUE.
        "name": "detach-refuses-an-octal-escape",
        "src": TRANSFORM,
        "old": DETACH_READS_AN_OCTAL_ESCAPE,
        "new": DETACH_REFUSES_AN_OCTAL_ESCAPE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_detach_walk_keeps_an_octal_escape",
            "test_a_reference_to_an_octal_body_splices_that_body",
        ],
    },
    {
        # The same revert on the superset walk, which is the second door
        # a slice carrying both an assertion and an octal escape uses.
        "name": "superset-refuses-an-octal-escape",
        "src": TRANSFORM,
        "old": SUPERSET_ESCAPE_BLOCK,
        "new": SUPERSET_REFUSES_AN_OCTAL_ESCAPE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_superset_walk_keeps_an_octal_escape",
            "test_an_octal_escape_inside_a_dropped_lookaround_goes_with_it",
        ],
    },
    {
        # An octal escape inside a dropped lookaround is copied out of
        # it, so the lookaround's own text reaches the widened slice.
        "name": "superset-copies-a-dropped-octal-escape",
        "src": TRANSFORM,
        "old": SUPERSET_ESCAPE_BLOCK,
        "new": SUPERSET_COPIES_A_DROPPED_ESCAPE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_an_octal_escape_inside_a_dropped_lookaround_goes_with_it",
        ],
    },
    {
        # The leading-zero form stops being recognised, so `\040` falls
        # through to the three-digit rule, whose first digit must be 1-3.
        "name": "octal-scan-drops-the-leading-zero-form",
        "src": TRANSFORM,
        "old": OCTAL_LEADING_ZERO_FORM,
        "new": OCTAL_NO_LEADING_ZERO_FORM,
        "selection": S_TRANSFORM,
        "expect": ["test_the_scan_stops_where_the_escape_stops"],
    },
    {
        # The leading digit stops stopping at 3, so `\400` and `\777`
        # read as characters when re calls them out of range.
        "name": "octal-scan-allows-a-value-above-the-range",
        "src": TRANSFORM,
        "old": OCTAL_THREE_DIGIT_FORM,
        "new": OCTAL_VALUE_ABOVE_THE_RANGE,
        "selection": S_TRANSFORM,
        "expect": ["test_what_is_not_an_octal_literal_is_refused"],
    },
    {
        # `\0` takes a fourth digit, so `\0000` swallows the literal
        # that follows it.
        "name": "octal-scan-takes-a-fourth-digit",
        "src": TRANSFORM,
        "old": OCTAL_ZERO_RUN,
        "new": OCTAL_ZERO_RUN_TOO_LONG,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_scan_stops_where_the_escape_stops",
            "test_the_scan_agrees_with_re_about_where_the_escape_ends",
        ],
    },
    {
        # `\0` stops after one further digit, so `\040` is cut short.
        "name": "octal-scan-stops-a-digit-early",
        "src": TRANSFORM,
        "old": OCTAL_ZERO_RUN,
        "new": OCTAL_ZERO_RUN_TOO_SHORT,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_scan_stops_where_the_escape_stops",
            "test_the_scan_agrees_with_re_about_where_the_escape_ends",
        ],
    },
    {
        # 8 and 9 join the octal digits, so `\08` reads as a three
        # character escape when re reads two.
        "name": "octal-scan-accepts-a-non-octal-digit",
        "src": TRANSFORM,
        "old": OCTAL_DIGIT_SET,
        "new": OCTAL_DIGIT_SET_TOO_WIDE,
        "selection": S_TRANSFORM,
        "expect": ["test_the_scan_stops_where_the_escape_stops"],
    },
    # ---- speech/pattern_transform.py ----------------------------------
    {
        "name": "widening-reverted-to-one-word",
        "src": PT,
        "old": "NUMBER_CAPTURE_BODY = NUMBER_PHRASE_PATTERN\n",
        "new": 'NUMBER_CAPTURE_BODY = r"\\w+"\n',
        "selection": S_SURVEY_PIPE,
        # Only a MULTI-WORD count can see this layer, which is the
        # sharpest statement of what the defect actually was. A single
        # \w+ token already matched "fifteen" before this change; what
        # refused it was words_to_int's ten-word table, so
        # test_backspace_fifteen and test_tab_eleven stay GREEN here and
        # are caught by words-to-int-loses-the-word-path instead. The
        # widened body is what a count spanning a space needs.
        "expect": [
            "test_the_count_is_captured_and_read_back",
            "test_delete_twenty_three_presses_delete_twenty_three_times",
        ],
    },
    # ---- speech/number_word_parser.py ---------------------------------
    {
        "name": "phrase-allows-no-extra-tokens",
        "src": NWP,
        "old": "_MAX_EXTRA_TOKENS = 4\n",
        "new": "_MAX_EXTRA_TOKENS = 0\n",
        "selection": S_SURVEY_PIPE,
        "expect": [
            "test_the_count_is_captured_and_read_back",
            "test_delete_twenty_three_presses_delete_twenty_three_times",
        ],
    },
    {
        "name": "phrase-drops-the-hyphen-separator",
        "src": NWP,
        "old": '    rf"(?:[\\s-](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"\n',
        "new": '    rf"(?:[\\s](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"\n',
        "selection": S_SURVEY,
        "expect": ["test_the_count_is_captured_and_read_back"],
    },
    {
        "name": "phrase-vocabulary-loses-the-tens",
        "src": NWP,
        "old": "    set(_UNITS) | set(_TENS) | set(_DIGIT_WORDS) | set(_ALIASES)\n",
        "new": "    set(_UNITS) | set(_DIGIT_WORDS) | set(_ALIASES)\n",
        "selection": S_SURVEY_PIPE,
        "expect": [
            "test_the_count_is_captured_and_read_back",
            "test_delete_twenty_three_presses_delete_twenty_three_times",
        ],
    },
    {
        "name": "widening-body-adds-a-capturing-group",
        "src": NWP,
        "old": '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "new": '    rf"(?:\\d+|({_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "selection": S_SURVEY,
        "expect": ["test_widening_did_not_renumber_the_captures"],
    },
    {
        "name": "body-loses-the-filler-prefix",
        "src": NWP,
        "old": '    rf"(?:(?:{_FILLER_ALTERNATION})[\\s-])?"\n'
                 '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "new": '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "selection": S_SURVEY_PIPE,
        "expect": [
            "test_the_count_is_captured_and_read_back",
            "test_a_filler_before_a_digit_count_presses_that_many_times",
            "test_a_filler_before_the_longest_phrase_is_read_and_clamped",
        ],
    },
    {
        "name": "phrase-tail-stops-being-atomic",
        "src": NWP,
        "old": '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?>{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "new": '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_numeric_capture_inside_a_repeat_does_not_run_away",
            "test_the_runaway_does_not_return_with_more_words",
            "test_ten_sequential_captures_do_not_run_away",
        ],
    },
    {
        # The choice always answers "plain". Identical in effect to
        # reverting the atomic group, reached through the chooser instead
        # of the body, so a fix that keeps two bodies but stops using the
        # atomic one is caught too.
        "name": "body-choice-always-plain",
        "src": TRANSFORM,
        "old": "    if dangerous:\n"
               "        if hyphen_bounded:\n",
        "new": "    if False:\n"
               "        if hyphen_bounded:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_numeric_capture_inside_a_repeat_does_not_run_away",
            "test_the_runaway_does_not_return_with_more_words",
            "test_ten_sequential_captures_do_not_run_away",
        ],
    },
    {
        # The choice always answers "atomic": the state 08e2ed2c shipped,
        # which broke every literal follower beginning with a count word.
        "name": "body-choice-always-atomic",
        "src": TRANSFORM,
        "old": "    return NUMBER_CAPTURE_BODY\n",
        "new": "    return NUMBER_CAPTURE_BODY_ATOMIC\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_literal_beginning_with_a_count_word_still_matches",
            "test_four_sequential_captures_keep_the_plain_tail",
        ],
    },
    {
        # The repeat test reads the single character after the group's
        # ')' again, so zero-width syntax hides a real quantifier and the
        # dangerous shape takes the plain body (wh-number-words-one-
        # parser.1.9).
        "name": "quantifier-scan-stops-skipping",
        "src": TRANSFORM,
        "old": "    n = len(text)\n"
               "    while i < n:\n"
               "        if text.startswith(\"(?#\", i):\n",
        "new": "    n = len(text)\n"
               "    while i < n:\n"
               "        if False:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_comment_group_hides_the_quantifier",
        ],
    },
    {
        # The verbose skip goes, so ignored whitespace between the ')'
        # and its '+' hides the repeat.
        "name": "quantifier-scan-ignores-verbose-space",
        "src": TRANSFORM,
        "old": "        if verbose and text[i] in _VERBOSE_WS:\n"
               "            i += 1\n"
               "            continue\n",
        "new": "        if False:\n"
               "            i += 1\n"
               "            continue\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_verbose_scope_hides_the_quantifier",
        ],
    },
    {
        # Every enclosing repeat counts as dangerous again, whatever
        # separates one iteration from the next, so a repeat carrying a
        # literal loses its ability to give a word back
        # (wh-number-words-one-parser.1.8).
        "name": "wrap-separator-test-always-dangerous",
        "src": TRANSFORM,
        "old": "                if _matches_separators_only(\n"
               "                    tail + pattern_str[lo:first_inside],\n"
               "                    capture_levels[index],\n"
               "                    references,\n"
               "                ):\n",
        "new": "                if True or _matches_separators_only(\n"
               "                    tail + pattern_str[lo:first_inside],\n"
               "                    capture_levels[index],\n"
               "                    references,\n"
               "                ):\n",
        "selection": S_TRANSFORM,
        # test_an_optional_group_is_still_not_a_repeat is deliberately
        # NOT an expected catcher: '?' makes _repeat_quantifier_follows
        # answer False, so that pattern never reaches the mutated line.
        # The gate reported this mutation as a survivor until the name
        # came out of the list.
        "expect": [
            "test_a_repeat_with_a_count_word_literal_after_the_capture",
        ],
    },
    {
        # Adjacency stops being measured: every sequential pair counts as
        # adjacent, so five captures separated by a literal count word
        # lose the plain tail.
        "name": "adjacency-test-always-true",
        "src": TRANSFORM,
        "old": "    adjacent = [\n"
               "        _matches_separators_only(\n",
        "new": "    adjacent = [\n"
               "        True or _matches_separators_only(\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_five_captures_separated_by_a_count_word",
        ],
    },
    {
        # The balancer hands back the raw slice, so a slice that cuts
        # through a group boundary will not compile and the conservative
        # True forces the atomic tail onto a literal-pinned repeat
        # (wh-number-words-one-parser.1.15).
        "name": "balancer-returns-the-raw-slice",
        "src": TRANSFORM,
        "old": "    return \"\".join(reversed(reopened)) + fragment + closers, outer[-1]\n",
        "new": "    return fragment, outer[-1]\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_wrapper_does_not_hide_the_literal_that_pins_the_repeat",
            "test_a_wrapper_between_captures_does_not_make_them_adjacent",
            "test_the_balancer_rebuilds_the_scope_the_slice_cuts_through",
        ],
    },
    {
        # An unmatched ')' stops reopening the scope it closed, so the
        # slice keeps the unbalanced parenthesis and will not compile.
        "name": "balancer-ignores-unmatched-close",
        "src": TRANSFORM,
        "old": "                reopened.append(\"(?x:\" if closed else \"(?-x:\")\n",
        "new": "                closed = closed\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_wrapper_does_not_hide_the_literal_that_pins_the_repeat",
            "test_a_wrapper_between_captures_does_not_make_them_adjacent",
            "test_the_balancer_rebuilds_the_scope_the_slice_cuts_through",
        ],
    },
    {
        # An unmatched '(' stops being closed, which breaks the same
        # slice from the other end.
        "name": "balancer-ignores-unmatched-open",
        "src": TRANSFORM,
        "old": "    closers = \")\" * len(inner)\n",
        "new": "    closers = \"\"\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_wrapper_does_not_hide_the_literal_that_pins_the_repeat",
            "test_a_wrapper_between_captures_does_not_make_them_adjacent",
        ],
    },
    {
        # The separator slice is compiled with no flags again, so layout
        # whitespace and '#' comments that re ignores become literal
        # requirements and a competing capture keeps the plain body
        # (wh-number-words-one-parser.1.14).
        "name": "separator-test-ignores-verbose",
        "src": TRANSFORM,
        "old": (
            "        compiled = re.compile("
            "resolved, re.VERBOSE if verbose else 0)\n"
        ),
        "new": "        compiled = re.compile(resolved)\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_verbose_repeat_wrap_takes_the_atomic_body",
            "test_a_verbose_comment_between_captures_still_counts_as_adjacent",
        ],
    },
    {
        # The walk stops recording each capture's own x mode, so every
        # slice is read as non-verbose and the same gap returns from the
        # other end.
        "name": "capture-levels-not-recorded",
        "src": TRANSFORM,
        "old": "                    tuple([base_verbose] + verbose_stack)\n",
        "new": "                    (False,)\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_verbose_repeat_wrap_takes_the_atomic_body",
            "test_a_verbose_comment_between_captures_still_counts_as_adjacent",
        ],
    },
    {
        # A cut ')' comes back as a plain '(?:', so the text after it is
        # read in the mode INSIDE the scope that ended rather than the
        # parent's mode. That is the round-7 repair's own defect
        # (wh-number-words-one-parser.1.16).
        "name": "balancer-forgets-the-scope-it-cut",
        "src": TRANSFORM,
        "old": "                reopened.append(\"(?x:\" if closed else \"(?-x:\")\n",
        "new": "                reopened.append(\"(?:\")\n",
        "selection": S_TRANSFORM,
        # The two _REPEAT-shaped tests are deliberately NOT expected
        # catchers. Their slice carries no text before the cut ')', so
        # the mode the reopened group holds cannot reach anything and
        # the compile flag alone still gives the right verdict. The gate
        # reported this mutation as a survivor until the test below,
        # whose slice DOES carry text inside that scope, was written.
        "expect": [
            "test_the_reopened_scope_keeps_the_mode_it_had_inside",
            "test_the_balancer_rebuilds_the_scope_the_slice_cuts_through",
        ],
    },
    {
        # The slice is compiled in the mode it STARTS in rather than the
        # mode in force after the last cut ')', which is the same wrong
        # answer reached from the flag argument instead of the text.
        "name": "balancer-compiles-in-the-slice-start-mode",
        "src": TRANSFORM,
        "old": "    return \"\".join(reversed(reopened)) + fragment + closers, outer[-1]\n",
        "new": "    return \"\".join(reversed(reopened)) + fragment + closers, levels[-1]\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_scoped_mode_change_does_not_hide_a_separator_only_repeat",
            "test_a_scoped_mode_change_does_not_hide_a_run_of_captures",
            "test_the_balancer_rebuilds_the_scope_the_slice_cuts_through",
        ],
    },
    {
        # A slice whose closes run past the recorded chain is repaired by
        # repeating the outermost mode instead of answering dangerous, so
        # the balancer guesses at a scope it was never told about.
        "name": "balancer-never-refuses-a-short-chain",
        "src": TRANSFORM,
        "old": "                if len(outer) < 2:\n"
               "                    return None\n",
        "new": "                if len(outer) < 2:\n"
               "                    outer.append(outer[-1])\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_balancer_refuses_a_slice_that_closes_past_its_chain",
        ],
    },
    {
        # Only the capture's own mode is recorded, so a slice that cuts
        # through a scope has no parent to step out to and every such
        # slice answers the conservative dangerous -- which puts the
        # atomic tail back on a literal-pinned repeat.
        "name": "capture-levels-lose-the-outer-scopes",
        "src": TRANSFORM,
        "old": "                    tuple([base_verbose] + verbose_stack)\n",
        "new": "                    (verbose_stack[-1] if verbose_stack "
               "else base_verbose,)\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_literal_inside_the_scoped_group_still_pins_the_repeat",
            "test_a_wrapper_does_not_hide_the_literal_that_pins_the_repeat",
        ],
    },
    {
        # The retry is gone, so a slice whose backreference names a group
        # defined OUTSIDE it answers the conservative dangerous straight
        # from the failed compile -- the .1.17 defect itself, which puts
        # the atomic tail on a repeat the reference pins to a literal.
        "name": "reference-resolution-dropped",
        "src": TRANSFORM,
        "old": (
            "    resolved = _resolve_references"
            "(text, (verbose,), references)\n"
        ),
        "new": "    resolved = text\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_to_a_literal_keeps_the_plain_tail",
        ],
    },
    {
        # The walk builds an empty map, so the retry substitutes nothing
        # and the compile fails a second time. Same wrong answer, reached
        # from the map end rather than the retry end.
        "name": "reference-map-not-built",
        "src": TRANSFORM,
        "old": "    for number in by_close:\n",
        "new": "    for number in []:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_to_a_literal_keeps_the_plain_tail",
        ],
    },
    {
        # Every reference resolves to a fixed literal instead of the body
        # of the group it names. The literal case still answers right by
        # luck; the whitespace cases stop being recognised as separators
        # and the runaway the atomic tail exists to prevent comes back.
        "name": "reference-body-ignores-the-group-it-names",
        "src": TRANSFORM,
        "old": BODY_WRAP,
        "new": BODY_WRAP_FIXED_TEXT,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_to_whitespace_still_takes_the_atomic_tail",
            "test_the_reference_body_keeps_the_mode_it_had_inside",
        ],
    },
    {
        # The body is spliced in the slice's own x mode rather than the
        # mode inside the group it came from, so layout the group ignores
        # becomes a literal requirement and a whitespace-only separator
        # reads as a literal run.
        "name": "reference-body-mode-not-carried",
        "src": TRANSFORM,
        "old": BODY_WRAP,
        "new": BODY_WRAP_MODE_LOST,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_reference_body_keeps_the_mode_it_had_inside",
        ],
    },
    {
        # A '(?(1)' conditional keeps no replacement, so a slice holding
        # one still will not compile and still answers dangerous.
        "name": "reference-conditional-not-mapped",
        "src": TRANSFORM,
        "old": '        references["(?({0})".format(number)] = "(?:"\n',
        "new": "        pass\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_to_a_literal_keeps_the_plain_tail",
        ],
    },
    {
        # Only the numeric form of a backreference is resolved, so
        # '(?P=sep)' still refuses to compile.
        "name": "reference-named-entry-dropped",
        "src": TRANSFORM,
        "old": '            references["(?P={0})".format(name)] = body\n',
        "new": "            pass\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_to_a_literal_keeps_the_plain_tail",
        ],
    },
    {
        # The resolver substitutes the one-digit key inside a two-digit
        # reference, which
        # changes what the slice means rather than leaving it to fail and
        # keep the conservative answer.
        "name": "resolver-splits-a-two-digit-reference",
        "src": TRANSFORM,
        "old": RESOLVE_TOKEN_END,
        "new": RESOLVE_TOKEN_END_ONE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_resolver_leaves_a_two_digit_reference_alone",
            "test_the_resolver_reads_the_token_re_reads",
        ],
    },
    {
        # The resolver stops skipping character classes, so the octal
        # escape inside a character class is rewritten as a reference.
        "name": "resolver-substitutes-inside-a-class",
        "src": TRANSFORM,
        "old": RESOLVER_SKIPS_BLOCK,
        "new": SKIPS_NO_CLASS_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_resolver_leaves_text_inside_a_class_and_a_comment_alone",
        ],
    },
    {
        # The resolver stops skipping '(?#' comments, so reference text
        # written as a comment is rewritten too.
        "name": "resolver-substitutes-inside-a-comment",
        "src": TRANSFORM,
        "old": RESOLVER_SKIPS_BLOCK,
        "new": SKIPS_NO_COMMENT_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_resolver_leaves_text_inside_a_class_and_a_comment_alone",
        ],
    },
    {
        # A rewritten conditional closes without the empty alternative,
        # so "(?(1) to)" becomes "(?: to)" -- a strict SUBSET, because
        # the real conditional matches the empty string when its group
        # did not participate (wh-number-words-one-parser.1.18).
        "name": "conditional-loses-its-empty-branch",
        "src": TRANSFORM,
        "old": "                if conditional_bar.pop() is False:\n"
               "                    # No top-level '|' in this rewritten "
               "conditional, so\n"
               "                    # its else branch was omitted. re still "
               "matches the\n"
               "                    # empty string there when the group did "
               "not\n"
               "                    # participate "
               "(wh-number-words-one-parser.1.18).\n"
               '                    out.append("|")\n',
        "new": "                if conditional_bar.pop() is False:\n"
               "                    pass\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_an_else_less_conditional_still_takes_the_atomic_tail",
            "test_the_resolver_gives_an_else_less_conditional_an_empty_branch",
        ],
    },
    {
        # A top-level '|' stops being recorded, so a conditional that
        # HAS an else branch is given an empty one as well. That is a
        # superset, so it cannot cause a runaway -- it makes the
        # separator test answer the conservative dangerous for a repeat
        # a literal pins, which is the .1.8 harm.
        "name": "conditional-bar-not-recorded",
        "src": TRANSFORM,
        "old": "            if conditional_bar and conditional_bar[-1] "
               "is False:\n",
        "new": "            if False:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_resolver_gives_an_else_less_conditional_an_empty_branch",
            "test_a_reference_to_a_literal_keeps_the_plain_tail",
        ],
    },
    {
        # The rewritten opener stops taking a level of its own, so the
        # conditional's ')' pops an enclosing group's entry. It shares
        # its catchers with conditional-loses-its-empty-branch by
        # construction: without the pushed entry the pop reads None
        # instead of False and the '|' is not emitted either. That is
        # the point -- the push is what makes the close know it is
        # closing a conditional.
        "name": "conditional-opener-pushes-no-level",
        "src": TRANSFORM,
        "old": '            if hit.startswith("(?("):\n',
        "new": "            if False:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_an_else_less_conditional_still_takes_the_atomic_tail",
            "test_the_resolver_gives_an_else_less_conditional_an_empty_branch",
        ],
    },
    {
        # The raw slice is compiled first and the resolver runs only on
        # failure, which is exactly the 2bee0344 ordering. A slice that
        # compiles while binding '\1' to a capture inside itself then
        # never reaches the resolver
        # (wh-number-words-one-parser.1.19).
        "name": "separator-test-compiles-before-resolving",
        "src": TRANSFORM,
        "old": RESOLVE_FIRST_BLOCK,
        "new": COMPILE_FIRST_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_slice_that_rebinds_a_reference_keeps_the_plain_tail",
        ],
    },
    {
        # A body is stored as written, so a reference inside it survives
        # into the slice, the compile fails a second time, and the
        # conservative answer puts the atomic tail on a repeat a literal
        # pins (wh-number-words-one-parser.1.20).
        "name": "reference-body-not-resolved-through",
        "src": TRANSFORM,
        "old": BODY_WRAP,
        "new": BODY_WRAP_NOT_RESOLVED,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_body_that_holds_another_reference_resolves_"
            "through",
        ],
    },
    {
        # Capture numbers are walked from the highest down, so a body is
        # resolved before the bodies it depends on. The sort is
        # deliberately not redundant with dict insertion order for this
        # reason: capture_opens happens to be built in ascending order
        # today, so this mutation proves the ORDER is load-bearing even
        # though removing the sorted() call alone would not change it.
        "name": "reference-bodies-resolved-in-the-wrong-order",
        "src": TRANSFORM,
        "old": "    for number in by_close:\n",
        "new": "    for number in reversed(by_close):\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_reference_body_that_holds_another_reference_resolves_"
            "through",
        ],
    },
    {
        # A body carrying a lookaround is spliced as written. Detached,
        # "(?<=x)\\s" matches NOTHING, so no separator sample matches and
        # the caller reads the wrap as a real word
        # (wh-number-words-one-parser.1.21).
        "name": "detached-body-allows-lookaround",
        "src": TRANSFORM,
        "old": '            if opener[:3] in ("(?=", "(?!") or '
               'opener[:3] == "(?<":\n'
               "                return None\n",
        "new": "            if False:\n"
               "                return None\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_lookbehind_body_still_takes_the_atomic_tail",
            "test_a_body_that_cannot_be_detached_is_refused",
        ],
    },
    {
        # '^' and '$' stop refusing a body. No behaviour test names this
        # one: an anchored body is refused by the unit test only. That
        # is deliberate -- an anchor inside a capture that a repeat then
        # references is not a shape this branch found a spoken command
        # for, and a mutation whose only catcher is a unit test still
        # proves the line is load-bearing.
        "name": "detached-body-allows-anchors",
        "src": TRANSFORM,
        "old": '        if char in "^$":\n            return None\n',
        "new": "        if False:\n            return None\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_body_that_cannot_be_detached_is_refused",
        ],
    },
    {
        # The escape check covers two things at once: the context
        # escapes and a leftover numeric reference. Both reach the same
        # line, so both catchers belong here.
        "name": "detached-body-allows-context-escapes",
        "src": TRANSFORM,
        "old": "            if nxt in _CONTEXT_ESCAPES:\n"
               "                return None\n",
        "new": "            if False:\n"
               "                return None\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_word_boundary_body_still_takes_the_atomic_tail",
            "test_a_body_that_cannot_be_detached_is_refused",
        ],
    },
    {
        # A body still holding a named reference or a conditional is
        # spliced anyway. It names a group the slice does not carry.
        "name": "detached-body-allows-a-named-reference",
        "src": TRANSFORM,
        "old": '        if char in "^$":\n            return None\n'
               '        if text.startswith("(?P=", i) or '
               'text.startswith("(?(", i):\n'
               "            return None\n",
        "new": '        if char in "^$":\n            return None\n'
               "        if False:\n"
               "            return None\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_body_that_cannot_be_detached_is_refused",
        ],
    },
    {
        # Capturing groups stay capturing, so one body spliced twice
        # defines its name twice and re refuses the slice
        # (wh-number-words-one-parser.1.23).
        "name": "detached-body-keeps-its-captures",
        "src": TRANSFORM,
        "old": DETACHED_CAPTURE_BLOCK,
        "new": "            out.append(opener)\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_named_body_spliced_twice_keeps_the_plain_tail",
            "test_a_detached_body_carries_no_capturing_group",
        ],
    },
    {
        # The scanner stops skipping character classes, so a '^' written
        # inside one refuses a body that is perfectly detachable.
        "name": "detached-body-substitutes-inside-a-class",
        "src": TRANSFORM,
        "old": DETACHED_SKIPS_BLOCK,
        "new": DETACHED_SKIPS_NO_CLASS,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_class_or_comment_hides_those_constructs",
        ],
    },
    {
        # The slice is compiled with no check that it still means what
        # it meant in place, so a lookaround, an anchor or a boundary
        # escape inside it is evaluated against a string that no longer
        # has the surrounding text -- the .1.24 defect, and with it the
        # .1.25 ANY_TEXT-under-a-negation defect.
        "name": "slice-detach-check-removed",
        "src": TRANSFORM,
        "old": DETACH_CHECK_BLOCK,
        "new": NO_DETACH_CHECK_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_context_sensitive_slice_takes_the_atomic_tail",
            "test_a_boundary_escape_alone_answers_conservatively",
            "test_a_widened_reference_under_a_negation_answers_true",
        ],
    },
    {
        # The check reads the RAW slice instead of the resolved one, so
        # every reference still in it -- a '\\1' naming a group defined
        # outside the slice -- reads as a construct that cannot be
        # detached, and the conservative answer returns to the repeats
        # that .1.17 and .1.22 freed.
        "name": "slice-detach-check-before-resolution",
        "src": TRANSFORM,
        "old": DETACH_CHECK_BLOCK,
        "new": DETACH_BEFORE_RESOLVE_BLOCK,
        "selection": S_TRANSFORM,
        # The .1.28 superset check MASKED this mutation's old catchers.
        # Reading the raw slice sends more fragments into the refusal
        # branch, and the superset check inside that branch now answers
        # the same as the compile would have: a differential over the 61
        # fragments the transform asks about across 76 raw patterns
        # found 18 where the branch differs and 0 where the answer does.
        # The ordering is still the design, so it is pinned by the
        # branch a resolvable reference must not take.
        "expect": [
            "test_a_resolvable_reference_never_reaches_the_refusal",
        ],
    },
    {
        # The conditional entries go back inside the per-group walk, so
        # a body naming a group that opens later still finds no entry,
        # the conditional survives into _detached_body, and the whole
        # reference becomes ANY_TEXT (wh-number-words-one-parser.1.26).
        "name": "conditional-seeded-inside-the-walk",
        "src": TRANSFORM,
        "old": SEED_BEFORE_WALK,
        "new": SEED_INSIDE_WALK,
        "selection": S_TRANSFORM,
        # The entries go back at the TOP of the walk, so a conditional
        # on its OWN group still resolves; only a target that opens
        # later can catch this. conditional-seeding-skips-names covers
        # the self-naming case from the other end.
        "expect": [
            "test_a_conditional_naming_a_later_group_keeps_the_plain_tail",
            "test_a_conditional_naming_a_nested_later_group_resolves_too",
        ],
    },
    {
        # Only the numeric spelling is seeded, so a named conditional on
        # a group that has not finished its own turn stays raw.
        "name": "conditional-seeding-skips-names",
        "src": TRANSFORM,
        "old": (
            '            references["(?({0})".format(name)] = "(?:"\n'
            "    by_close = sorted(\n"
        ),
        "new": "            pass\n    by_close = sorted(\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_named_conditional_on_its_own_group_keeps_the_plain_tail",
        ],
    },
    {
        # Groups are walked by capture NUMBER again. A group is numbered
        # when it opens, so an outer group can legally reference a
        # nested one whose number is higher, and the ascending walk
        # leaves that reference raw
        # (wh-number-words-one-parser.1.22). This is a different
        # mutation from reference-bodies-resolved-in-the-wrong-order,
        # which reverses whatever order is chosen; this one picks the
        # WRONG ordering key while keeping it ascending.
        "name": "reference-bodies-walked-by-capture-number",
        "src": TRANSFORM,
        "old": BY_CLOSE_BLOCK,
        "new": BY_NUMBER_BLOCK,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_body_that_names_a_nested_group_keeps_the_plain_tail",
        ],
    },
    {
        # ANY_TEXT stops matching every string. A refused body then
        # matches no separator sample at all, which is the OPPOSITE of
        # the conservative answer it exists to give.
        "name": "refused-body-matches-nothing",
        "src": TRANSFORM,
        "old": 'ANY_TEXT = "(?s:.*)"\n',
        # An empty class complement, not "(?!)": the round-12 refusal
        # answers True for any fragment carrying a lookaround before it
        # compiles anything, so the negative-lookahead spelling of this
        # mutant was masked and every catcher stayed green.
        "new": 'ANY_TEXT = "[^\\\\s\\\\S]"\n',
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_lookbehind_body_still_takes_the_atomic_tail",
            "test_a_word_boundary_body_still_takes_the_atomic_tail",
            "test_a_refused_body_makes_every_separator_sample_match",
        ],
    },
    {
        # The exact-count branch stops using the measured boundary and
        # drops back to "more than one iteration", so {2} and {4} force
        # the atomic tail again (wh-number-words-one-parser.1.12).
        "name": "brace-threshold-drops-to-two",
        "src": TRANSFORM,
        "old": "        return int(low) > _MAX_PLAIN_CAPTURES\n",
        "new": "        return int(low) >= 2\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_brace_bounded_at_the_safe_maximum_keeps_the_plain_tail",
        ],
    },
    {
        # Every brace counts as dividing a word run again, so a bounded
        # group forces the atomic tail and refuses a count word that the
        # following literal needs back
        # (wh-number-words-one-parser.1.12).
        "name": "brace-always-repeats",
        "src": TRANSFORM,
        "old": "    match = _BRACE_QUANTIFIER_RE.match(text, i)\n"
               "    if match is None:\n"
               "        return True\n",
        "new": "    match = _BRACE_QUANTIFIER_RE.match(text, i)\n"
               "    if True:\n"
               "        return True\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_brace_bounded_at_the_safe_maximum_keeps_the_plain_tail",
        ],
    },
    {
        # No brace counts as a repeat, so a group that really does repeat
        # keeps the plain tail and runs away.
        "name": "brace-never-repeats",
        "src": TRANSFORM,
        "old": "        if text[i] == \"{\":\n"
               "            return _brace_quantifier_divides_a_run(text, i)\n",
        "new": "        if text[i] == \"{\":\n"
               "            return False\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_brace_that_really_repeats_still_gets_the_atomic_tail",
        ],
    },
    {
        # The high bound stops being read, so {1,12} and {,12} read as
        # bounded low and keep the plain tail. The runaway begins between
        # a maximum of ten and twelve, measured, so the catchers use
        # twelve; a maximum of three is 0.000s and catches nothing, which
        # is how this mutation first showed up as a survivor that failed
        # no test at all.
        "name": "brace-high-bound-ignored",
        "src": TRANSFORM,
        "old": "    return int(high) > _MAX_PLAIN_CAPTURES\n",
        "new": "    return False\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_brace_that_really_repeats_still_gets_the_atomic_tail",
        ],
    },
    {
        # The group stops being split on its own '|', so every capture is
        # judged by the first branch's text and a later branch that can
        # follow itself through a space keeps the plain tail
        # (wh-number-words-one-parser.1.13).
        "name": "branch-split-ignores-alternation",
        "src": TRANSFORM,
        "old": "    lo = content_start\n"
               "    for bar in alternations:\n",
        "new": "    lo = content_start\n"
               "    for bar in ():\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_later_branch_that_can_follow_itself_is_atomic",
        ],
    },
    {
        # The walk stops recording where each group's own '|' sits, which
        # empties every branch list and collapses the split the same way
        # from the other end.
        "name": "alternation-positions-not-recorded",
        "src": TRANSFORM,
        "old": "        if char == \"|\" and open_stack:\n",
        "new": "        if False:\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_later_branch_that_can_follow_itself_is_atomic",
        ],
    },
    {
        # The body gains a leading separator branch. That is the one
        # change that would make a capture's own quantifier dangerous,
        # which is why the widening can ignore that quantifier today.
        "name": "body-accepts-a-leading-separator",
        "src": NWP,
        "old": '    rf"(?:(?:{_FILLER_ALTERNATION})[\\s-])?"\n'
               '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "new": '    rf"[\\s-]?(?:(?:{_FILLER_ALTERNATION})[\\s-])?"\n'
               '    rf"(?:\\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"\n',
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_capture_that_repeats_itself_is_not_the_same_hazard",
        ],
    },
    {
        "name": "filler-vocabulary-loses-the-plural",
        "src": NWP,
        "old": '_FILLER_WORDS = ("number", "numbers")\n',
        "new": '_FILLER_WORDS = ("number",)\n',
        "selection": S_SURVEY,
        "expect": ["test_the_count_is_captured_and_read_back"],
    },
    {
        "name": "parser-alias-step-dropped",
        "src": NWP,
        "old": "    if aliases:\n"
               "        tokens = [_ALIASES.get(token, token) for token in tokens]\n",
        "new": "    if False:\n"
               "        tokens = [_ALIASES.get(token, token) for token in tokens]\n",
        "selection": S_ACT_NAV,
        "expect": [
            "test_homophones",
            "test_the_speech_homophones_are_still_counts",
        ],
    },
    {
        "name": "parser-zero-option-ignored",
        "src": NWP,
        "old": '    if zero and tokens == ["zero"]:\n        return 0\n',
        "new": "    if False:\n        return 0\n",
        "selection": S_ACT,
        "expect": ["test_zero"],
    },
    {
        "name": "alias-map-loses-for",
        "src": NWP,
        "old": '_ALIASES = {"to": "two", "too": "two", "for": "four"}\n',
        "new": '_ALIASES = {"to": "two", "too": "two"}\n',
        "selection": S_ACT_NAV,
        "expect": [
            "test_homophones",
            "test_the_speech_homophones_are_still_counts",
        ],
    },
    # ---- speech/actions.py --------------------------------------------
    {
        "name": "words-to-int-loses-the-word-path",
        "src": ACT,
        "old": "    return parse_number_word(text, aliases=True, zero=True)\n",
        "new": "    return None\n",
        "selection": S_ACT + S_PIPE,
        "expect": [
            "test_word_number",
            "test_a_word_count_above_ten",
            "test_backspace_fifteen_presses_backspace_fifteen_times",
        ],
    },
    {
        "name": "words-to-int-loses-the-homophones",
        "src": ACT,
        "old": "    return parse_number_word(text, aliases=True, zero=True)\n",
        "new": "    return parse_number_word(text, aliases=False, zero=True)\n",
        "selection": S_ACT,
        "expect": ["test_homophones"],
    },
    {
        "name": "words-to-int-loses-zero",
        "src": ACT,
        "old": "    return parse_number_word(text, aliases=True, zero=True)\n",
        "new": "    return parse_number_word(text, aliases=True, zero=False)\n",
        "selection": S_ACT,
        "expect": ["test_zero"],
    },
    {
        "name": "words-to-int-loses-the-digit-branch",
        "src": ACT,
        "old": "    if text.isdigit():\n",
        "new": "    if False:\n",
        "selection": S_ACT,
        "expect": ["test_a_digit_string_at_the_conversion_limit_still_converts"],
    },
    # ---- speech/navigation/parser.py ----------------------------------
    {
        "name": "navigation-loses-the-cap",
        "src": NAV,
        "old": "        return min(n, MAX_COUNT)\n",
        "new": "        return n\n",
        "selection": S_NAV,
        "expect": ["test_a_count_above_the_maximum_still_clamps"],
    },
    {
        "name": "navigation-loses-the-homophones",
        "src": NAV,
        "old": "        n = parse_number_word(text, aliases=True)\n",
        "new": "        n = parse_number_word(text, aliases=False)\n",
        "selection": S_NAV,
        "expect": ["test_the_speech_homophones_are_still_counts"],
    },
    {
        "name": "navigation-accepts-zero",
        "src": NAV,
        "old": "        n = parse_number_word(text, aliases=True)\n",
        "new": "        n = parse_number_word(text, aliases=True, zero=True)\n",
        "selection": S_NAV,
        "expect": ["test_zero_is_not_a_cursor_count"],
    },
    # ---- speech/pattern_manager.py ------------------------------------
    {
        "name": "probe-corpus-loses-the-digit-run",
        "src": PM,
        "old": '        "1" * 30 + "!",\n',
        "new": "",
        "selection": S_PM,
        "expect": ["test_probe_runs_against_transformed_pattern"],
    },
    {
        # The whole tokenizer goes: a backslash-digit run is copied raw
        # and no numeric reference resolves at all.
        "name": "resolver-drops-the-numeric-branch",
        "src": TRANSFORM,
        "old": RESOLVE_NUMERIC_BRANCH,
        "new": RESOLVE_NUMERIC_BRANCH_GONE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_resolver_reads_the_token_re_reads",
            "test_a_named_reference_still_resolves",
        ],
    },
    {
        # The octal check goes, so the two-digit head of an octal
        # literal is substituted as though it named a group.
        "name": "resolver-reads-an-octal-literal-as-a-reference",
        "src": TRANSFORM,
        "old": RESOLVE_OCTAL_CHECK,
        "new": RESOLVE_OCTAL_CHECK_OFF,
        "selection": S_TRANSFORM,
        "expect": ["test_the_resolver_reads_the_token_re_reads"],
    },
    {
        # The reference token takes a third digit, which re never does.
        "name": "resolver-token-takes-a-third-digit",
        "src": TRANSFORM,
        "old": RESOLVE_TOKEN_END,
        "new": RESOLVE_TOKEN_END_THREE,
        "selection": S_TRANSFORM,
        "expect": ["test_the_resolver_reads_the_token_re_reads"],
    },
    {
        # A reference the map does not carry is deleted instead of
        # copied, which silently removes text the pattern must match.
        "name": "resolver-drops-an-absent-reference",
        "src": TRANSFORM,
        "old": RESOLVE_ABSENT_KEY,
        "new": RESOLVE_ABSENT_KEY_DROPPED,
        "selection": S_TRANSFORM,
        "expect": ["test_the_resolver_reads_the_token_re_reads"],
    },
    {
        # The second digit read goes back to str.isdigit, so a
        # reference standing before a Unicode digit takes a third
        # character and no longer matches its key in the map.
        "name": "resolver-second-digit-is-unicode-aware",
        "src": TRANSFORM,
        "old": RESOLVE_TOKEN_END,
        "new": RESOLVE_TOKEN_END_UNICODE,
        "selection": S_TRANSFORM,
        "expect": ["test_a_reference_before_one_still_resolves"],
    },
    {
        # The detach walk goes back to str.isdigit, so a backslash
        # before a Unicode digit is read as a numeric escape,
        # _octal_escape_end refuses it, and the walk returns None.
        # Only the test that reads the walk directly changes: measured
        # with this mutation alone, _matches_separators_only still
        # answers False, because the refusal falls through to
        # _assertion_free_superset -- the .1.28 recovery road -- and
        # that walk still keeps the slice. Both walks have to be
        # reverted for the count to lose its plain tail, which is why
        # the superset mutation below carries the same catcher.
        "name": "detach-digit-is-unicode-aware",
        "src": TRANSFORM,
        "old": DETACH_READS_AN_OCTAL_ESCAPE,
        "new": DETACH_DIGIT_IS_UNICODE_AWARE,
        "selection": S_TRANSFORM,
        "expect": ["test_both_walks_keep_the_slice"],
    },
    {
        # The superset walk goes back to str.isdigit and refuses the
        # same slice, which is the other half of the same refusal.
        "name": "superset-digit-is-unicode-aware",
        "src": TRANSFORM,
        "old": SUPERSET_ESCAPE_BLOCK,
        "new": SUPERSET_DIGIT_IS_UNICODE_AWARE,
        "selection": S_TRANSFORM,
        "expect": ["test_both_walks_keep_the_slice"],
    },
    {
        # The plain body loses its end assertion, so the word
        # alternation gives "nineteen" back as "nine" and leaves
        # "teen" for a literal the raw expression never matched.
        "name": "phrase-end-goes-from-the-plain-body",
        "src": NWP,
        "old": PHRASE_END_PLAIN,
        "new": PHRASE_END_PLAIN_GONE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_literal_suffix_cannot_split_the_word",
            "test_a_letter_after_the_capture_keeps_the_digit_branch",
            "test_both_bodies_carry_the_boundary",
        ],
    },
    {
        # The atomic body loses the same assertion. No shape the
        # transform hands the atomic body to can reach it -- a
        # capture followed by a letter is never separators-only --
        # so only the test that reads the body directly changes.
        "name": "phrase-end-goes-from-the-atomic-body",
        "src": NWP,
        "old": PHRASE_END_ATOMIC,
        "new": PHRASE_END_ATOMIC_GONE,
        "selection": S_TRANSFORM,
        "expect": ["test_both_bodies_carry_the_boundary"],
    },
    {
        # The assertion moves outside the alternation, so it starts
        # refusing the digit branch too and the widened body stops
        # matching text the raw expression matches.
        "name": "phrase-end-covers-the-digit-branch",
        "src": NWP,
        "old": PHRASE_END_PLAIN,
        "new": PHRASE_END_PLAIN_OVER_THE_DIGITS,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_digit_form_the_raw_expression_takes_still_matches",
            "test_a_letter_after_the_capture_keeps_the_digit_branch",
        ],
    },
    {
        # The assertion keeps its digits and drops its letters, which
        # is the half that splits a count word.
        "name": "phrase-end-forbids-only-a-digit",
        "src": NWP,
        "old": PHRASE_END_CLASS,
        "new": PHRASE_END_DIGITS_ONLY,
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_literal_suffix_cannot_split_the_word",
            "test_a_letter_after_the_capture_keeps_the_digit_branch",
            "test_both_bodies_carry_the_boundary",
        ],
    },
    {
        # The hyphen axis stops answering, so every capture takes a tail
        # that can cross a hyphen the raw expression wanted for itself
        # (wh-number-words-one-parser.1.34).
        "name": "hyphen-axis-never-answers",
        "src": TRANSFORM,
        "old": "    return bounded\n",
        "new": "    return set()\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_repeated_hyphen_group_keeps_the_raw_iteration_count",
            "test_a_trailing_hyphen_needs_no_repeat_and_no_second_capture",
            "test_the_choice_is_made_per_capture",
        ],
    },
    {
        # Only the group opening is read, so a hyphen standing plainly
        # to the RIGHT of the capture stops counting. ^(\d+)-$ needs no
        # repeat and no second capture, which is the half of the class
        # the finding did not describe.
        "name": "hyphen-scan-skips-the-right-slice",
        "src": TRANSFORM,
        "old": "        if _slice_can_consume_a_hyphen(after, levels):\n",
        "new": "        if False and "
               "_slice_can_consume_a_hyphen(after, levels):\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_trailing_hyphen_needs_no_repeat_and_no_second_capture",
            "test_the_choice_is_made_per_capture",
            "test_the_right_hand_slice_runs_past_the_next_capture",
        ],
    },
    {
        # Only the right-hand slice is read, so a repeat that puts its
        # hyphen in its own opening stops counting: ^(?:-(\d+))+$ has
        # nothing to the right of the capture at all.
        "name": "hyphen-scan-skips-the-group-opening",
        "src": TRANSFORM,
        "old": "        for open_index in enclosures[index]:\n",
        "new": "        for open_index in ():\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_hyphen_before_the_capture_in_a_repeat_also_counts",
            "test_the_atomic_axis_and_the_hyphen_axis_are_independent",
        ],
    },
    {
        # A character class is read character by character instead of
        # being asked as one token, so the range hyphen of [a-z] reads
        # as a hyphen the raw expression wants.
        "name": "hyphen-scan-reads-a-class-as-characters",
        "src": TRANSFORM,
        "old": '        elif char == "[":\n'
               "            end = _scan_class(text, i)\n"
               "            token = text[i:end]\n"
               "            i = end\n",
        "new": '        elif char == "[" and False:\n'
               "            end = _scan_class(text, i)\n"
               "            token = text[i:end]\n"
               "            i = end\n",
        "selection": S_TRANSFORM,
        "expect": ["test_a_token_that_cannot_be_a_hyphen_keeps_the_tail"],
    },
    {
        # A group OPENING is read as tokens instead of skipped whole,
        # so the inline-flag hyphen of (?-x: reads as pattern text.
        "name": "hyphen-scan-reads-a-group-opening-as-tokens",
        "src": TRANSFORM,
        "old": '        elif char == "(":\n'
               "            open_len = _group_open_len(text, i)\n"
               "            stack.append(verbose)\n"
               "            verbose = _scoped_verbose_change("
               "text, i, open_len, verbose)\n"
               "            i += open_len\n"
               "            continue\n",
        "new": '        elif char == "(" and False:\n'
               "            open_len = _group_open_len(text, i)\n"
               "            stack.append(verbose)\n"
               "            verbose = _scoped_verbose_change("
               "text, i, open_len, verbose)\n"
               "            i += open_len\n"
               "            continue\n",
        "selection": S_TRANSFORM,
        "expect": ["test_a_flag_group_hyphen_is_syntax_and_not_a_token"],
    },
    {
        # A (?#...) comment group is read as tokens, so prose inside it
        # decides the tail. _group_open_len answers 3 for "(?#", which
        # leaves the comment body exposed.
        "name": "hyphen-scan-reads-a-comment-group-as-tokens",
        "src": TRANSFORM,
        "old": '        elif text.startswith("(?#", i):\n'
               "            i = _scan_comment(text, i)\n"
               "            continue\n",
        "new": '        elif text.startswith("(?#", i) and False:\n'
               "            i = _scan_comment(text, i)\n"
               "            continue\n",
        "selection": S_TRANSFORM,
        "expect": ["test_a_comment_group_is_skipped_whole"],
    },
    {
        # The right-hand slice is scanned before its backreferences are
        # resolved. A lone \1 cannot compile, the scan answers the
        # conservative True, and every pattern carrying a reference
        # loses its hyphen tail.
        "name": "hyphen-scan-skips-reference-resolution",
        "src": TRANSFORM,
        "old": "        after = _resolve_references("
               "pattern_str[end:], levels, references)\n",
        "new": "        after = pattern_str[end:]\n",
        "selection": S_TRANSFORM,
        "expect": ["test_a_reference_is_resolved_before_the_scan"],
    },
    {
        # The right-hand slice stops at the next capture instead of
        # running to the end of the pattern. That narrowing is the one
        # the docstring rejects: it needs branch and quantifier
        # reasoning to be right, and ^(\d+) (\d+)-$ shows it is not.
        "name": "hyphen-scan-stops-at-the-next-capture",
        "src": TRANSFORM,
        "old": "        after = _resolve_references("
               "pattern_str[end:], levels, references)\n"
               "        if _slice_can_consume_a_hyphen(after, levels):\n",
        "new": "        stop = (capture_spans[index + 1][0]\n"
               "                if index + 1 < len(capture_spans)\n"
               "                else len(pattern_str))\n"
               "        after = _resolve_references("
               "pattern_str[end:stop], levels, references)\n"
               "        if _slice_can_consume_a_hyphen(after, levels):\n",
        "selection": S_TRANSFORM,
        "expect": ["test_the_right_hand_slice_runs_past_the_next_capture"],
    },
    {
        # The verbose-mode comment skip is removed, which is the state
        # wh-number-words-one-parser.1.35 measured: the '[' inside a
        # comment opens a class spanning the newline, that class
        # matches no hyphen, and the real delimiter behind it is never
        # reached. The scan then answers False and the capture keeps a
        # hyphen-crossing tail.
        "name": "hyphen-scan-reads-a-verbose-comment-as-tokens",
        "src": TRANSFORM,
        "old": '        if verbose and char == "#":\n'
               "            i = _scan_verbose_comment(text, i)\n"
               "            continue\n"
               '        if char == "\\\\":\n',
        "new": '        if False and char == "#":\n'
               "            i = _scan_verbose_comment(text, i)\n"
               "            continue\n"
               '        if char == "\\\\":\n',
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_comment_on_the_right_cannot_hide_the_delimiter",
            "test_a_comment_in_the_opening_cannot_hide_the_delimiter",
        ],
    },
    {
        # The RIGHT-hand slice is read in the pattern's outermost mode
        # instead of the mode in force at the capture, so a capture
        # inside (?x: ... ) has the comment after it read as ordinary
        # pattern text. Same wrong number as above, reached from the
        # caller's side. The opening slice takes its mode from
        # group_inner_verbose instead, so the ..._in_the_opening_...
        # test is NOT an expected catcher here.
        "name": "hyphen-scan-reads-the-slice-in-the-base-mode",
        "src": TRANSFORM,
        "old": "        levels = levels or (base_verbose,)\n",
        "new": "        levels = (base_verbose,)\n",
        "selection": S_TRANSFORM,
        # test_a_comment_on_the_right_cannot_hide_the_delimiter was an
        # expected catcher until the .1.37 fix and can no longer see
        # this mutation. Its pattern's right-hand slice carries two
        # unmatched ')' characters, so a one-element chain runs off the
        # end of ``outer`` and the scan returns the conservative True
        # -- the right answer, reached by the wrong road, so the test
        # passes under the mutation. This is the masking the
        # mutation-gate skill names: a later layer covers the same
        # input. The three below fail on the mode itself.
        "expect": [
            "test_a_hyphen_inside_a_comment_is_not_a_delimiter",
            "test_the_mode_is_restored_when_a_scope_closes",
            "test_the_shipped_catalog_keeps_the_hyphen_on_every_capture",
        ],
    },
    {
        # A scoped (?-x: inside the slice stops turning verbose mode
        # off, so a '#' that re reads as a literal is read as a comment
        # and the real hyphen written after it disappears.
        "name": "hyphen-scan-ignores-a-scoped-mode-change",
        "src": TRANSFORM,
        "old": '        elif char == "(":\n'
               "            open_len = _group_open_len(text, i)\n"
               "            stack.append(verbose)\n"
               "            verbose = _scoped_verbose_change("
               "text, i, open_len, verbose)\n",
        "new": '        elif char == "(":\n'
               "            open_len = _group_open_len(text, i)\n"
               "            stack.append(verbose)\n"
               "            verbose = verbose\n",
        "selection": S_TRANSFORM,
        "expect": ["test_a_scoped_mode_change_inside_the_slice_is_followed"],
    },
    {
        # The mode is never restored at the scope's own ')', so a
        # comment written after a (?-x:...) group stays ordinary text
        # and the hyphen inside that comment reads as a delimiter.
        "name": "hyphen-scan-never-restores-the-outer-mode",
        "src": TRANSFORM,
        "old": '        elif char == ")":\n'
               "            if stack:\n"
               "                verbose = stack.pop()\n"
               "            elif len(outer) > 1:\n",
        "new": '        elif char == ")":\n'
               "            if stack and False:\n"
               "                verbose = stack.pop()\n"
               "            elif len(outer) > 1:\n",
        "selection": S_TRANSFORM,
        "expect": ["test_the_mode_is_restored_when_a_scope_closes"],
    },
    {
        # The ')' characters the slice CUTS THROUGH step one level out
        # of ``outer`` but no longer carry the mode with them, so the
        # rest of the slice keeps the inner scope's mode. That is the
        # state wh-number-words-one-parser.1.37 measured: the slice
        # ')#-$' of ^one(?x:\s(\d+))#-$ stayed verbose, the outer '#-'
        # read as a comment, and "one two-three#-" returned 23 where
        # the raw expression accepts "one 2#-" alone.
        "name": "hyphen-scan-keeps-the-inner-mode-past-an-outer-close",
        "src": TRANSFORM,
        "old": "            elif len(outer) > 1:\n"
               "                outer.pop()\n"
               "                verbose = outer[-1]\n",
        "new": "            elif len(outer) > 1:\n"
               "                outer.pop()\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_closed_verbose_scope_leaves_the_outer_hyphen_visible",
            "test_a_closed_non_verbose_scope_restores_the_outer_comment",
        ],
    },
    {
        # The repeat test is dropped, so the opening of EVERY enclosing
        # group is read again. That is the state
        # wh-number-words-one-parser.1.36 measured: a group entered once
        # is entered before the capture, so a hyphen in its opening
        # competes with nothing, yet ^section(?:[- ]?(\d+))$ lost its
        # hyphenated spoken form to it.
        "name": "hyphen-opening-read-for-every-enclosure",
        "src": TRANSFORM,
        "old": "            if not _repeat_quantifier_follows(\n"
               "                pattern_str, close + 1,\n"
               "                group_verbose.get(open_index, base_verbose),\n"
               "            ):\n"
               "                continue\n"
               "            inner = group_inner_verbose.get("
               "open_index, base_verbose)\n",
        "new": "            if False:\n"
               "                continue\n"
               "            inner = group_inner_verbose.get("
               "open_index, base_verbose)\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_a_group_that_never_repeats_keeps_the_hyphen",
            "test_an_optional_group_still_never_repeats",
        ],
    },
    {
        # The hyphen-free tail gets its hyphen back, so the fourth body
        # stops differing from the first and the axis has nothing to
        # choose between.
        "name": "hyphen-tail-keeps-its-hyphen",
        "src": NWP,
        "old": TAIL_NO_HYPHEN,
        "new": TAIL_NO_HYPHEN_WITH_ONE,
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_four_bodies_are_distinct",
            "test_a_repeated_hyphen_group_keeps_the_raw_iteration_count",
        ],
    },
    {
        # The two axes stop being independent: a capture that is both
        # dangerous and hyphen-bounded takes the atomic body that still
        # carries the hyphen.
        "name": "hyphen-axis-ignored-when-dangerous",
        "src": TRANSFORM,
        "old": "        if hyphen_bounded:\n"
               "            return NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN\n"
               "        return NUMBER_CAPTURE_BODY_ATOMIC\n",
        "new": "        if hyphen_bounded:\n"
               "            return NUMBER_CAPTURE_BODY_ATOMIC\n"
               "        return NUMBER_CAPTURE_BODY_ATOMIC\n",
        "selection": S_TRANSFORM,
        "expect": [
            "test_the_atomic_axis_and_the_hyphen_axis_are_independent",
        ],
    },
]


def _clear_pycache():
    """Drop the target modules' bytecode so a same-size mutant cannot be
    served from a cache compiled from the other version."""
    for target in TARGETS:
        cache = target.parent / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    _clear_pycache()
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_RUN_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _restore(src, original):
    """Put the original bytes back, surviving a second Ctrl+C.

    A first interrupt reaches the caller's finally; a second one landing
    inside this write would otherwise escape with the mutant still in a
    tracked file. Hold it, retry once, print any failure (an unreported
    mutant is the whole harm), clear bytecode, then re-raise the held
    interrupt.
    """
    held = None
    for _attempt in (1, 2):
        try:
            src.write_bytes(original)
            if src.read_bytes() == original:
                break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR restore of {src.name} failed: {exc}")
    else:
        print(f"ERROR {src.name} may still hold a mutant; restore it by hand")
    _clear_pycache()
    if held is not None:
        raise held


def collect_names(selection):
    """Real test names in a selection, so a renamed test cannot read as a survivor."""
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_acceptable_failure(reason):
    """True for an assertion failure, false for a crash upstream of it."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped."""
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing test."""
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [line for line in output.splitlines() if _ERROR_SUMMARY.match(line)]


def _load_texts():
    originals = {path: path.read_bytes() for path in TARGETS}
    texts = {}
    for path, raw in originals.items():
        newline = "\r\n" if b"\r\n" in raw else "\n"
        texts[path] = (raw.decode("utf-8"), newline)
        print(
            f"line endings in {path.name}: "
            f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    for mut in MUTATIONS:
        newline = texts[mut["src"]][1]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)
    return originals, texts


def _build_mutant(mut, texts):
    """Return (mutated_text, error) -- error names a stale, ambiguous, or
    non-compiling pattern, and is reported, never counted as a verdict."""
    src = mut["src"]
    text = texts[src][0]
    count = text.count(mut["old"])
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    try:
        compile(mutated, str(src), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only():
    _originals, texts = _load_texts()
    stale = 0
    bad = 0
    for mut in MUTATIONS:
        _mutated, err = _build_mutant(mut, texts)
        if err is None:
            continue
        print("ERROR", err)
        if "does not compile" in err:
            bad += 1
        else:
            stale += 1
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{bad} that do not compile"
    )
    return 1 if stale or bad else 0


def main(argv):
    if "--check" in argv:
        return check_only()
    only = None
    for arg in argv:
        if arg.startswith("--only="):
            only = set(arg[len("--only="):].split(","))
    chosen = [m for m in MUTATIONS if only is None or m["name"] in only]
    if only is not None:
        unknown = only - {m["name"] for m in MUTATIONS}
        if unknown:
            print(f"ERROR unknown mutation names in --only: {sorted(unknown)}")
            return 1

    originals, texts = _load_texts()
    errors, caught, survived = [], [], []

    selections = []
    for mut in chosen:
        if mut["selection"] not in selections:
            selections.append(mut["selection"])
    names_by_selection = {}
    for selection in selections:
        names = collect_names(selection)
        names_by_selection[tuple(selection)] = names
        print(f"collected {len(names)} test names in {', '.join(selection)}")
    for mut in chosen:
        real_names = names_by_selection[tuple(mut["selection"])]
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist "
                    f"in its selection"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    for selection in selections:
        baseline = _pytest(*selection, *PYTEST_ARGS)
        if baseline.returncode != 0:
            print(
                f"ERROR baseline is not green for {', '.join(selection)}; "
                f"refusing to start"
            )
            print(baseline.stdout[-2000:])
            return 1
        print(f"baseline green: {', '.join(selection)}")

    for mut in chosen:
        src = mut["src"]
        mutated, err = _build_mutant(mut, texts)
        if err is not None or mutated is None:
            errors.append(err or f"{mut['name']}: no mutant built")
            print("ERROR", errors[-1])
            continue
        src.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*mut["selection"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            _restore(src, originals[src])
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_acceptable_failure(r)]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    skipped = len(MUTATIONS) - len(chosen)
    print(
        f"scope: ran {len(chosen)} of {len(MUTATIONS)} mutations"
        + (f", skipped {skipped} by --only" if skipped else ", none skipped")
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
