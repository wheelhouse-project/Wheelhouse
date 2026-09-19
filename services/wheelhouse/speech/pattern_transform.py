r"""Shared pattern transformation utilities for speech command patterns.

This module provides centralized pattern transformation logic used by both
PatternCatalog and TextParser to ensure consistent pattern processing
across the speech recognition system.

Key Functions:
  - transform_pattern: Auto-detect and transform special pattern types

Transformations:
  1. Greedy patterns (.+ or .*) → Mark as is_greedy
  2. Numeric patterns (\d+) → Widen to a spoken-number phrase, add validation

This ensures that both the pattern catalog (for buffering decisions) and
text parser (for execution) apply identical transformations.
"""
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .number_word_parser import (
    NUMBER_PHRASE_PATTERN,
    NUMBER_PHRASE_PATTERN_ATOMIC,
    NUMBER_PHRASE_PATTERN_ATOMIC_NO_HYPHEN,
    NUMBER_PHRASE_PATTERN_NO_HYPHEN,
)

# What a widened numeric capture holds. It used to be r"\w+", one word,
# which is why a two-word count ("delete twenty three") matched no
# pattern at all and a one-word count above ten was refused by the
# numeric validation behind it (wh-number-words-one-parser). The body
# comes from number_word_parser, the one word-to-integer implementation
# in this service, so the words the loader admits and the words the
# parser can read cannot drift apart. It contains no capturing group, so
# the capture numbers this module records still match re.compile's.
NUMBER_CAPTURE_BODY = NUMBER_PHRASE_PATTERN

# The same body with an ATOMIC multi-word tail, used only for the pattern
# shapes _pick_number_capture_body calls dangerous. An atomic tail cannot
# give a word back, which is what stops the exponential division of a word
# run and is also why it is not the default (wh-number-words-one-parser.1.3
# and .1.6).
NUMBER_CAPTURE_BODY_ATOMIC = NUMBER_PHRASE_PATTERN_ATOMIC

# The same two bodies with a hyphen-free tail, used only where the raw
# expression consumes a hyphen of its own at the capture's boundary
# (wh-number-words-one-parser.1.34). ``_hyphen_bounded_captures`` names
# those captures and number_word_parser records what the tail costs.
NUMBER_CAPTURE_BODY_NO_HYPHEN = NUMBER_PHRASE_PATTERN_NO_HYPHEN
NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN = NUMBER_PHRASE_PATTERN_ATOMIC_NO_HYPHEN

#: Quantifiers that let a group run more than once, so a word run can be
#: divided across its iterations. ``?`` is deliberately absent: it allows
#: at most one iteration, so it multiplies nothing (measured at 0.000s for
#: a thirty-word run against ``^go(?: (\d+))?$``). ``{`` is present but
#: is not sufficient on its own -- ``_brace_quantifier_divides_a_run``
#: reads how high the brace counts (wh-number-words-one-parser.1.12).
_REPEATING_QUANTIFIERS = ("*", "+", "{")

#: A brace quantifier as Python's parser accepts it: an optional low
#: bound, an optional comma, an optional high bound. A brace holding
#: neither a digit nor a comma is literal text, and so is anything this
#: does not match at all.
_BRACE_QUANTIFIER_RE = re.compile(r"\{(\d*)(,?)(\d*)\}")

#: How many widened captures a pattern may carry and still keep the plain
#: tail. Sequential captures divide a word run the same way an enclosing
#: repeat does, because each one can take one to five words. Measured on a
#: run that cannot match, with the plain tail: four captures 0.000s, seven
#: 0.032s, eight 0.158s, nine 0.790s, ten 6.140s. Four leaves the cost at
#: the floor with a wide margin, and no shipped pattern carries more than
#: one (all 319 entries parsed; the 113 with a numeric capture have
#: exactly one each).
_MAX_PLAIN_CAPTURES = 4


class _WidenedBodyPlaceholder:
    """Stands in for a widened body until the walk knows which one to use.

    The walk cannot pick the body when it reaches a capture: whether an
    enclosing group repeats is decided by the quantifier after that
    group's ``)``, which is still to the right. So the walk emits this
    object, finishes, then joins the parts with the chosen body. It is an
    object rather than a text marker on purpose -- no string can collide
    with it, and a user pattern may contain any text at all.
    """

    __slots__ = ()


_WIDENED_BODY = _WidenedBodyPlaceholder()


def _pick_number_capture_body(
    dangerous: bool, hyphen_bounded: bool = False,
) -> str:
    r"""Choose the plain or the atomic body for ONE widened capture.

    The atomic tail stops the runaway (wh-number-words-one-parser.1.3)
    and it costs the tail its ability to give a word back to a literal
    that follows the capture (wh-number-words-one-parser.1.6). Seven
    words in the body vocabulary are ordinary English -- "to", "too",
    "for", "and", "oh", "one" and "hundred" -- so that cost is real, and
    the choice is made per capture rather than per pattern
    (wh-number-words-one-parser.1.8). ``dangerous`` is
    ``_dangerous_captures``' answer for this capture; all 113 shipped
    patterns with a numeric capture take the plain branch.

    ``hyphen_bounded`` is ``_hyphen_bounded_captures``' answer, and it
    is a SEPARATE axis: the tail's hyphen competes with a hyphen the
    raw expression consumes after the capture, whether or not a word
    run can divide (wh-number-words-one-parser.1.34). All four
    combinations are reachable, so there are four bodies. All 113
    shipped patterns take the plain, hyphen-keeping branch: none of
    them can consume a hyphen anywhere after its numeric capture.
    """
    if dangerous:
        if hyphen_bounded:
            return NUMBER_CAPTURE_BODY_ATOMIC_NO_HYPHEN
        return NUMBER_CAPTURE_BODY_ATOMIC
    if hyphen_bounded:
        return NUMBER_CAPTURE_BODY_NO_HYPHEN
    return NUMBER_CAPTURE_BODY


#: Strings made only of the characters that separate spoken words. Text
#: standing between two widened bodies that can match one of these forces
#: no real word between them, which is what lets the two bodies compete
#: for the same word run.
_SEPARATOR_SAMPLES = ("", " ", "  ", "\t", "-", " - ", " \t ")


def _balanced_for_compile(
    fragment: str, levels: Tuple[bool, ...]
) -> Optional[Tuple[str, bool]]:
    r"""``fragment`` with the group boundaries it cuts through rebuilt.

    Returns the text to compile and the x mode to compile it in, or None
    when the slice cannot be repaired soundly.

    Both separator rules slice raw pattern text between two points that
    can sit in DIFFERENT groups, so a slice routinely carries a ``)``
    with no ``(`` or the reverse. ``^one(?:(?:\s(\d+)\sto))+$`` slices
    to ``\sto)(?:\s``, which ``re.compile`` refuses.

    A parenthesis a slice cuts through is group structure: it consumes
    no input, so supplying its missing half cannot change what the slice
    is able to match. Each unmatched ``(`` therefore gets a ``)`` behind
    the slice.

    An unmatched ``)`` needs more than a generic ``(?:`` in front,
    because the group it closes may be a scoped flag group: the text
    after that ``)`` is then governed by the parent scope, not by the
    mode the slice started in, and one boolean cannot express the
    change (wh-number-words-one-parser.1.16). ``levels`` is the whole
    chain of modes around the slice, outermost first, with
    ``levels[-1]`` the mode at the slice's first character. This
    function steps one level outward per unmatched ``)`` and rebuilds
    that level as an explicit ``(?x:`` or ``(?-x:`` opening, so re reads
    each part of the slice in the mode that really governs it. The
    returned mode is the one in force after the last unmatched ``)``.
    A slice whose closes run past the recorded chain cannot be rebuilt
    and answers None, which the caller reads as the conservative
    dangerous answer.

    Measured before this fix, with the plain body against twenty-four
    count words: ``(?x)^one(?: (?-x:\s(\d+)) )+$`` took 7.423s and the
    ten-capture ``(?-x:...)`` adjacency form took 0.824s, both because
    the two spaces around the scoped group were read as literal text
    when the outer x scope in fact ignores them.

    Reading the slice, rather than counting every parenthesis in it, is
    what keeps an escaped ``\)`` and a ``)`` inside a character class or
    a comment out of the count. The mode is tracked through scoped flag
    groups that open inside the slice as well, so the ``#`` comment
    scanning uses the mode actually in force. In an active verbose scope
    the closers go on their own line, because a ``#`` comment the slice
    ends inside would otherwise swallow them; re ignores that newline.
    """
    outer = list(levels)
    verbose = outer[-1]
    inner: List[bool] = []
    # The scoped openings that replace the ')' characters this slice
    # cuts through, innermost first; they are emitted in reverse so the
    # slice's first ')' closes the innermost of them.
    reopened: List[str] = []
    i = 0
    n = len(fragment)
    while i < n:
        char = fragment[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _scan_class(fragment, i)
            continue
        if fragment.startswith("(?#", i):
            i = _scan_comment(fragment, i)
            continue
        if verbose and char == "#":
            i = _scan_verbose_comment(fragment, i)
            continue
        if fragment.startswith("(?(", i):
            # A conditional group: its "(?(id)" opening carries a ')'
            # that closes nothing, so step over the whole opening. It
            # takes no flags, so the mode inside it is unchanged.
            close = fragment.find(")", i + 3)
            i = (close + 1) if close != -1 else n
            inner.append(verbose)
            continue
        if char == "(":
            open_len = _group_open_len(fragment, i)
            inner.append(verbose)
            verbose = _scoped_verbose_change(fragment, i, open_len, verbose)
            i += open_len
            continue
        if char == ")":
            if inner:
                verbose = inner.pop()
            else:
                if len(outer) < 2:
                    return None
                closed = outer.pop()
                reopened.append("(?x:" if closed else "(?-x:")
                verbose = outer[-1]
            i += 1
            continue
        i += 1
    closers = ")" * len(inner)
    if closers and verbose:
        closers = "\n" + closers
    return "".join(reversed(reopened)) + fragment + closers, outer[-1]


def _resolve_references(
    text: str, levels: Tuple[bool, ...], references: Dict[str, str]
) -> str:
    r"""``text`` with each backreference and conditional replaced.

    A slice is a fragment of the user's pattern, so a ``\1`` or a
    ``(?P=sep)`` inside it can name a group defined OUTSIDE it. re
    refuses such a fragment, and the refusal used to reach the
    conservative dangerous answer, which put the atomic tail on a repeat
    that the reference itself pins to a literal
    (wh-number-words-one-parser.1.17).

    ``references`` maps each reference's own text to a replacement that
    is a SUPERSET of what it can match: a backreference matches exactly
    the text its group captured, so the group's body covers every string
    the reference can be, and ``(?(1)yes|no)`` matches only within
    ``(?:yes|no)``. A superset is sound in the direction this needs --
    if no separator sample matches the superset, none can match the
    reference, so answering "not separator-only" is provably right, and
    a sample that does match keeps the conservative answer.

    A conditional needs more than its opener replaced. ``(?(N)yes)``
    with no else branch matches the EMPTY string when group N did not
    participate, so ``(?:yes)`` is a strict SUBSET of it and would let
    the caller answer False for a wrap that really is separator-only
    (wh-number-words-one-parser.1.18). Whether an else branch exists is
    a property of the text here, not of the group being referenced, so
    this walk tracks each rewritten conditional and closes it as
    ``(?:yes|)`` when its body showed no top-level ``|``.

    The same lexer rules as the rest of this module apply: a ``\1``
    inside a character class is an octal escape and a ``\1`` inside
    either comment form is comment text, so neither is a reference.

    A backslash-digit run is read the way re reads it, before any key
    is consulted. re takes at most TWO digits for a numeric reference,
    and three octal digits are one character, so ``\164`` is the
    letter ``t`` even in a pattern that carries 165 captures, and
    ``\198`` is group 19 followed by a literal ``8``. Searching the
    map for the longest matching key disagreed with both: it replaced
    the octal literals in ``^go(?: (\d+)\040\164\157)+$`` with two
    empty bodies, which left the fragment looking separator-only and
    put the atomic tail on a repeat that the literal " to" pins, so
    the command accepted "go 15 to" and refused "go fifteen to"
    (wh-number-words-one-parser.1.31). ``\10`` whose group is absent
    is still left alone -- the compile then fails again and the
    conservative answer stands.

    ``levels`` is the chain of x modes around the point the text was
    cut, outermost first, with ``levels[-1]`` the mode at its first
    character, the same shape ``_balanced_for_compile`` takes. A slice
    can begin inside a scoped flag group and reach that group's ``)``,
    and the text after that ``)`` is governed by the parent scope. One
    boolean cannot express the change, so each unmatched ``)`` steps
    one level outward (wh-number-words-one-parser.1.37). A caller
    holding balanced text passes a one-element chain.
    """
    if not references:
        return text
    # ``outer`` is consumed by the unmatched ')' characters; ``stack``
    # below carries the groups that open inside the text itself.
    outer = list(levels) or [False]
    verbose = outer[-1]
    # Numeric keys are reached through the backslash-digit branch
    # below, which reads them as re does. Only the named and
    # conditional forms are searched for here, longest first.
    keys = sorted(
        (key for key in references if not key.startswith("\\")),
        key=len,
        reverse=True,
    )
    out: List[str] = []
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
        if verbose and char == "#":
            end = _scan_verbose_comment(text, i)
            out.append(text[i:end])
            i = end
            continue
        if char == "\\" and text[i + 1:i + 2] in _ASCII_DIGITS:
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
        hit = next((k for k in keys if text.startswith(k, i)), None)
        if hit is not None:
            out.append(references[hit])
            if hit.startswith("(?("):
                # The replacement opens a group of its own, so it needs
                # its own level. Without one, the conditional's ')'
                # would pop an enclosing group's mode.
                stack.append(verbose)
                conditional_bar.append(False)
            i += len(hit)
            continue
        if char == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if text.startswith("(?(", i):
            close = text.find(")", i + 3)
            end = (close + 1) if close != -1 else n
            out.append(text[i:end])
            stack.append(verbose)
            conditional_bar.append(None)
            i = end
            continue
        if char == "(":
            open_len = _group_open_len(text, i)
            out.append(text[i:i + open_len])
            stack.append(verbose)
            conditional_bar.append(None)
            verbose = _scoped_verbose_change(text, i, open_len, verbose)
            i += open_len
            continue
        if char == ")":
            if stack:
                verbose = stack.pop()
                if conditional_bar.pop() is False:
                    # No top-level '|' in this rewritten conditional, so
                    # its else branch was omitted. re still matches the
                    # empty string there when the group did not
                    # participate (wh-number-words-one-parser.1.18).
                    out.append("|")
            elif len(outer) > 1:
                # A ')' this text cuts through: step one level outward
                # so the rest reads in the mode that really governs it
                # (wh-number-words-one-parser.1.37).
                outer.pop()
                verbose = outer[-1]
            out.append(char)
            i += 1
            continue
        if char == "|":
            if conditional_bar and conditional_bar[-1] is False:
                conditional_bar[-1] = True
            out.append(char)
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _matches_separators_only(
    fragment: str, levels: Tuple[bool, ...],
    references: Dict[str, str],
) -> bool:
    r"""Can ``fragment`` match a string made only of separators?

    Answered by compiling the fragment and trying it, rather than by
    reading its syntax: a reading has to be right about alternation,
    character classes, ``\w``, lookaround and backreferences, while a
    fullmatch against ``_SEPARATOR_SAMPLES`` is right by construction.

    That answer is only worth having while the compiled fragment still
    means what it meant inside the whole pattern. A fragment carrying a
    lookaround, an anchor or a boundary escape does not: it asks about
    text the fragment no longer has. Such a fragment answers True
    without being compiled at all
    (wh-number-words-one-parser.1.24 and .1.25); ``_detached_body``
    decides which ones those are.

    ``levels`` is the chain of x modes in force around the point the
    fragment was cut, outermost first, and it has to be passed in
    because the fragment is a SLICE of the user's own pattern. Inside an active x scope re ignores layout
    whitespace and ``#`` comments, so compiling the slice without the
    flag turns that ignored layout into literal text that no separator
    sample can match -- the wrap then reads as "a real word stands
    here" and a competing capture keeps the plain body
    (wh-number-words-one-parser.1.14). Measured before this fix, with
    the plain body against twenty-four count words:
    ``(?x)^one (?: \s (\d+) ) +$`` took 7.254s, its comment form
    7.099s, and the ``(?x:...)`` scoped form 7.614s, where the same
    three shapes without the flag took 0.000s.

    A scoped flag change that sits WHOLLY inside the fragment needs
    nothing extra: re reads it from the fragment itself. One that only
    CLOSES inside the fragment is rebuilt by
    ``_balanced_for_compile``, which hands back both the text and the
    mode to read it in (wh-number-words-one-parser.1.16). An earlier
    version of this text claimed such a fragment could not compile and
    that the refusal answered True by itself; that stopped being true
    the moment the balancer began repairing the unbalanced
    parenthesis, and the plain body it then chose cost 7.423s on a
    twenty-four word nonmatch.

    ``references`` supplies a superset for each backreference and
    conditional whose group may sit outside the fragment; see
    ``_reference_replacements``. EVERY fragment goes through it before
    the compile, because a fragment that compiles as written can still
    bind a numeric reference to a capture inside itself rather than the
    one the whole pattern names (wh-number-words-one-parser.1.19). A
    fragment holding no reference text comes back unchanged.

    True is the CONSERVATIVE answer -- it makes the caller use the
    atomic body -- so a fragment that will not compile even with its
    references resolved (the text between two captures can cross a group
    boundary and carry an unbalanced parenthesis that
    ``_balanced_for_compile`` could not repair) answers True rather than
    guessing. Adding
    the x flag can only move the answer toward True, because the flag
    removes required characters from the fragment and the empty string
    is itself one of the samples.
    """
    built = _balanced_for_compile(fragment, levels)
    if built is None:
        return True
    text, verbose = built
    # A reference the fragment cannot resolve is not evidence of
    # anything: the group it names is simply defined outside the slice
    # (wh-number-words-one-parser.1.17). Resolving happens BEFORE the
    # compile rather than after it fails, because a slice that compiles
    # can still bind '\1' to a capture inside itself
    # (wh-number-words-one-parser.1.19). A resolution that will not
    # compile keeps the conservative answer, exactly as an uncompilable
    # raw slice did.
    resolved = _resolve_references(text, (verbose,), references)
    # A slice is compiled OUTSIDE the pattern it was cut from, so a
    # construct that asks about the text AROUND it means something else
    # here: ``(?<=\w)\s`` matches a space after a word character in
    # place and matches nothing at all standalone, so no sample matched
    # and the plain body stayed on a repeat that divides a word run
    # (wh-number-words-one-parser.1.24). ``_detached_body`` names
    # exactly that class -- lookaround of either sign, ``^``, ``$`` and
    # the ``\A \Z \b \B`` escapes -- and a slice carrying one answers
    # True rather than believing a compile that has lost the context.
    #
    # The same refusal covers the ``ANY_TEXT`` fallback under a
    # negation (wh-number-words-one-parser.1.25). ANY_TEXT is a
    # superset, and a superset reverses direction inside ``(?!...)``:
    # ``(?!(?s:.*))`` can never match, so a separator that works read as
    # unsafe. The negation is itself a construct the detached slice
    # cannot evaluate, so the one refusal answers both.
    if _detached_body(resolved, verbose) is None:
        # Refusing is right about a slice that could be nothing but
        # separators, and wrong about one that also carries a
        # mandatory literal: `` to end`` pins where an iteration ends,
        # so the plain body is both correct and fast, while the atomic
        # tail eats the count word "to" and cannot give it back
        # (wh-number-words-one-parser.1.28). Deleting every assertion
        # widens the slice, so a widened slice that matches no
        # separator sample proves the slice itself matches none.
        superset = _assertion_free_superset(resolved, verbose)
        if superset is not None and not _any_separator_sample_matches(
            superset, verbose
        ):
            return False
        return True
    try:
        compiled = re.compile(resolved, re.VERBOSE if verbose else 0)
    except re.error:
        return True
    for sample in _SEPARATOR_SAMPLES:
        try:
            if compiled.fullmatch(sample) is not None:
                return True
        except re.error:
            return True
    return False


def _repeat_quantifier_follows(text: str, i: int, verbose: bool) -> bool:
    r"""Does a REPEATING quantifier apply to the group ending before ``i``?

    Python's grammar allows syntax that consumes no input between a
    group's ``)`` and its quantifier, so reading the single character at
    ``i`` answers wrong for a group that really does repeat
    (wh-number-words-one-parser.1.9). Two forms hide a quantifier:

      - ``(?#...)`` in any mode, as in ``^one(?: (\d+))(?# note)+$``;
      - ignored whitespace and ``#`` comments while the x flag is active
        in the scope holding the quantifier, as in
        ``^(?x:one(?:\s(\d+)) +)$`` -- both are accepted by the Advanced
        editor, and the save-time probe reaches neither.

    ``?`` stays excluded, in every spelling: it permits at most one
    iteration, so it repeats nothing. ``*?``, ``+?``, ``{m,n}?`` and the
    possessive spellings are all included, because their FIRST character
    is the repeating quantifier itself.

    A brace is not enough on its own. A brace bounded at
    ``_MAX_PLAIN_CAPTURES`` or fewer divides a word run no more ways than
    that many adjacent captures do, which is the measured safe side, so
    ``_brace_quantifier_divides_a_run`` reads the bound rather than
    stopping at the ``{`` (wh-number-words-one-parser.1.12).
    """
    n = len(text)
    while i < n:
        if text.startswith("(?#", i):
            i = _scan_comment(text, i)
            continue
        if verbose and text[i] in _VERBOSE_WS:
            i += 1
            continue
        if verbose and text[i] == "#":
            i = _scan_verbose_comment(text, i)
            continue
        if text[i] == "{":
            return _brace_quantifier_divides_a_run(text, i)
        return text[i] in _REPEATING_QUANTIFIERS
    return False


def _brace_quantifier_divides_a_run(text: str, i: int) -> bool:
    r"""Can the brace quantifier at ``text[i]`` divide a run of words?

    ``text[i]`` is ``{``. A brace bounded at N lets the group take at
    most N iterations, which divides a word run exactly as N adjacent
    captures do -- so the answer uses the same measured boundary the
    adjacency rule uses, ``_MAX_PLAIN_CAPTURES``, and not "more than one
    iteration". Plain-tail cost of ``^one(?: (BODY)){1,N}$`` against a
    twenty-four word run that cannot match: N=3 0.000s, N=5 0.001s, N=8
    0.100s, N=9 0.303s, N=10 0.787s, N=12 3.732s, unbounded 15.628s. The
    runaway begins between ten and twelve, so a maximum of four is far
    inside the safe side (wh-number-words-one-parser.1.12).

    Reading the bound rather than stopping at the ``{`` is what stops a
    bounded brace refusing a match it should accept. Measured against
    "one fifteen to end" with the pattern ``^one(?: (\d+)){q} to end$``
    before this rule: ``{1,2}``, ``{1,3}``, ``{1,4}``, ``{2}`` and
    ``{2,3}`` all took the atomic tail and refused, which is the .1.8
    regression class.

    Which spellings Python really treats as quantifiers was measured on
    Python 3.12.10, not read off the syntax, by compiling ``a(?:b){q}``
    and trying it against "ab" and "abb": ``{2}``, ``{1,3}``, ``{2,}``,
    ``{,3}`` and ``{,}`` all take a second iteration -- ``{,}`` is NOT
    literal text, it behaves as ``{0,}`` -- while ``{}``, ``{foo}`` and
    ``{1,2,3}`` are literal text and repeat nothing.

    The literal spellings answer True even so, which is deliberate. The
    truthful answer is False, but a wrong False lets a runaway keep the
    plain tail while a wrong True only refuses one match -- and for a
    literal brace it does not even cost that, because the brace character
    itself stops a count phrase from running past it. Answering True also
    leaves those spellings behaving exactly as they did before this
    function existed. It is the same conservative direction
    ``_matches_separators_only`` takes for a fragment it cannot compile.

    A maximum between five and about eight still answers True while
    measuring 0.001s to 0.100s. That is the same accepted trade-off the
    adjacency rule already makes for five adjacent captures, and holding
    both to one measured constant is worth more than shaving the boundary
    in two places.
    """
    match = _BRACE_QUANTIFIER_RE.match(text, i)
    if match is None:
        return True
    low, comma, high = match.group(1), match.group(2), match.group(3)
    if not low and not comma:
        # "{}": neither a bound nor a comma, so re reads it literally.
        return True
    if not comma:
        # "{m}": exactly m iterations.
        return int(low) > _MAX_PLAIN_CAPTURES
    if not high:
        # "{m,}" and "{,}": unbounded above.
        return True
    return int(high) > _MAX_PLAIN_CAPTURES

def _group_branches(
    content_start: int, close: int, alternations
) -> List[Tuple[int, int]]:
    """The half-open span of each top-level branch of one group.

    ``alternations`` holds the indices of that group's own bare ``|``
    characters, in order. A group without alternation gives back one
    branch covering its whole content, which is what makes the
    per-branch rule reduce exactly to the rule it replaced.
    """
    bounds: List[Tuple[int, int]] = []
    lo = content_start
    for bar in alternations:
        bounds.append((lo, bar))
        lo = bar + 1
    bounds.append((lo, close))
    return bounds


#: What a reference stands for when its group's body cannot be detached
#: from the pattern it was written in. Every string matches it, so every
#: separator sample matches it, and ``_matches_separators_only`` gives
#: the conservative dangerous answer (wh-number-words-one-parser.1.21).
ANY_TEXT = "(?s:.*)"

#: Escapes that ask about the text AROUND the position rather than
#: consuming a character, so a body carrying one means something
#: different once it is detached from its pattern.
_CONTEXT_ESCAPES = frozenset("AZbB")

# Which digits an octal escape may hold. ``8`` and ``9`` are digits
# but never octal, so they leave an escape a backreference.
_OCTAL_DIGITS = frozenset("01234567")

# Which digits re reads after a backslash. ``str.isdigit`` is
# Unicode-aware and answers True for U+0662, U+FF19, U+00B2 and U+2460,
# none of which re reads as a digit: a backslash before any of them is
# an escaped literal. Asking ``str.isdigit`` sent those slices down the
# numeric road and made both detach walks refuse
# (wh-number-words-one-parser.1.33).
_ASCII_DIGITS = frozenset("0123456789")

# What may stand between the braces of a quantifier. ``{2}``,
# ``{1,3}``, ``{2,}``, ``{,3}`` and ``{,}`` all repeat; ``{}``,
# ``{foo}`` and ``{1,2,3}`` are literal text. Measured by compiling
# each on this interpreter, not read off the grammar.
_BRACE_QUANTIFIER = re.compile(r"\d*(?:,\d*)?")


def _detached_body(text: str, verbose: bool) -> Optional[str]:
    r"""``text`` rewritten to mean the same OUTSIDE its own pattern.

    ``_reference_replacements`` splices a group's body into a slice cut
    from somewhere else, so the body is compiled with none of the text
    that stood around it. Two things break under that move, and this
    function is where both are handled.

    A body that ASKS about its surroundings does not survive detaching
    at all. ``(?<=x)\s`` matches a space only after an ``x``; on its own
    it matches nothing, so no separator sample matches it and the caller
    answers "a real word stands here" for a wrap that really is
    separator-only. That is the reverse of the guarantee the whole
    superset argument rests on -- the detached text is a strict SUBSET,
    not a superset -- and the plain body it leaves behind cost 0.172s on
    a twenty-word nonmatch of
    ``^x(?P<sep>(?<=x)\s)(?:(\d+)(?P=sep))+$`` where the atomic body
    cost 0.000s (wh-number-words-one-parser.1.21). There is no sound
    rewrite for such a body, so this returns None and the caller
    substitutes ``ANY_TEXT``, which every separator sample matches and
    which therefore keeps the conservative answer. Lookaround of either
    sign, ``^``, ``$``, and the ``\A \Z \b \B`` escapes are all
    treated this way. A negative lookaround detached can match MORE
    rather than less, but "more" is not reliable either, so it takes the
    same conservative road.

    A body that CAPTURES survives, but its copies collide. The same
    body is spliced once per reference, so ``(?P<inner> to)`` appearing
    twice in one slice is a duplicate group name and re refuses the
    whole slice; the refusal reaches the conservative answer and puts
    the atomic tail on a repeat that a literal pins
    (wh-number-words-one-parser.1.23). A numbered capture does not
    refuse, which is worse -- it silently shifts the numbers a
    reference still inside the slice counts by. Every capturing group
    in the body therefore becomes non-capturing, which leaves the
    language it matches unchanged. That is safe only because the body
    arrives with its own references already resolved, so nothing left
    in it is counting groups.

    A body that still holds a reference of its own is refused for the
    same reason: it names a group this text does not carry, so the
    numbering it counts by is gone. That happens only for a reference re
    itself rejects, since ``_reference_replacements`` resolves in close
    order.

    An OCTAL escape is not such a reference. ``\040`` is a space; it
    names no group and consumes a character like any other escape, so
    ``_octal_escape_end`` tells the two apart and only a real
    backreference is refused. Refusing the octal form put the atomic
    tail on ``^go(?: (\d+)\040to end)+$``, which then accepted
    "go 15 to end" and refused "go fifteen to end"
    (wh-number-words-one-parser.1.30).
    """
    out: List[str] = []
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
        if text.startswith("(?#", i):
            end = _scan_comment(text, i)
            out.append(text[i:end])
            i = end
            continue
        if verbose and char == "#":
            end = _scan_verbose_comment(text, i)
            out.append(text[i:end])
            i = end
            continue
        if char == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                out.append(text[i:end])
                i = end
                continue
            if nxt in _CONTEXT_ESCAPES:
                return None
            out.append(text[i:i + 2])
            i += 2
            continue
        if char in "^$":
            return None
        if text.startswith("(?P=", i) or text.startswith("(?(", i):
            return None
        if char == "(":
            open_len = _group_open_len(text, i)
            opener = text[i:i + open_len]
            if opener[:3] in ("(?=", "(?!") or opener[:3] == "(?<":
                return None
            out.append(
                "(?:" if opener == "(" or opener.startswith("(?P<")
                else opener
            )
            stack.append(verbose)
            verbose = _scoped_verbose_change(text, i, open_len, verbose)
            i += open_len
            continue
        if char == ")":
            if stack:
                verbose = stack.pop()
            out.append(char)
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _octal_escape_end(text: str, i: int) -> Optional[int]:
    r"""Index just past the octal escape at ``text[i]``, else None.

    ``text[i]`` is a backslash and the character after it is a digit.
    An octal escape is a CHARACTER; a backreference names a group. Both
    walks refused every backslash-digit escape, which was right about
    the second and wrong about the first: ``\040`` is a space, and
    refusing it put the atomic tail on
    ``^go(?: (\d+)\040to end)+$`` -- a command savable in the Advanced
    editor -- so it accepted "go 15 to end" and refused
    "go fifteen to end" (wh-number-words-one-parser.1.30).

    Where an escape ends was measured on this interpreter by compiling
    probes, not read off the documentation:

      ``\0407`` fullmatches " 7", so ``\0`` takes at most two further
      octal digits;
      ``\1234`` fullmatches ``chr(0o123) + "4"``, so a leading 1-3
      takes exactly two further octal digits;
      ``(a)\123`` compiles with one group present, so three octal
      digits are ALWAYS a character and never a reference;
      ``(a)\11`` is an invalid group reference, so two digits never
      are;
      ``\400`` and ``\777`` are "octal escape value outside of range
      0-0o377", which is why the leading digit stops at 3.

    None is returned for everything else -- ``\1``, ``\12``, ``\8``, a
    non-ASCII digit -- and the caller keeps its refusal, which is the
    behaviour those escapes had before.

    A mis-split here cannot change either walk's OUTPUT TEXT, only
    which branch it takes: every character an escape consumes is a
    digit, and a digit outside an escape is a literal digit, so the
    same characters reach the output either way.
    """
    n = len(text)
    if text[i + 1:i + 2] == "0":
        end = i + 2
        while end < n and end < i + 4 and text[end] in _OCTAL_DIGITS:
            end += 1
        return end
    head = text[i + 1:i + 4]
    if (len(head) == 3 and all(c in _OCTAL_DIGITS for c in head)
            and head[0] in "123"):
        return i + 4
    return None


def _any_separator_sample_matches(source: str, verbose: bool) -> bool:
    r"""Does any ``_SEPARATOR_SAMPLES`` entry fullmatch ``source``?

    True is the conservative answer for the callers, so a source that
    will not compile, or a sample that raises against it, answers True
    rather than guessing -- the same road ``_matches_separators_only``
    takes for a fragment it cannot compile.
    """
    try:
        widened = re.compile(source, re.VERBOSE if verbose else 0)
    except re.error:
        return True
    for sample in _SEPARATOR_SAMPLES:
        try:
            if widened.fullmatch(sample) is not None:
                return True
        except re.error:
            return True
    return False


def _quantifier_after(text: str, i: int, verbose: bool) -> int:
    r"""Index just past a quantifier at ``i``, or ``i`` if there is none.

    ``_assertion_free_superset`` calls this after deleting a
    lookaround so the quantifier that repeated it goes too.
    Returning ``i`` unchanged is what keeps a brace that only LOOKS
    like a quantifier -- ``{foo}``, ``{}``, ``{1,2,3}``, an unclosed
    ``{`` -- as the literal text re reads it; eating those would
    delete characters the original must match, and the widened text
    would stop being a superset from the other side.

    A quantifier need not sit against the ``)``. ``(?#...)`` in any
    mode, and ignored whitespace or a ``#`` comment while the x flag
    is active, all stand between a group and its quantifier without
    breaking the binding -- ``\s(?=\w)(?# hidden){4}`` compiles and
    repeats. That is the hiding place ``_repeat_quantifier_follows``
    already reads for .1.9, and this walk reads it the same way.

    ``?`` is included here, unlike in ``_repeat_quantifier_follows``.
    That function asks "does this group REPEAT", where ``?`` answers
    no; this one asks "how far does the quantifier reach", where
    every spelling counts. The lazy and possessive suffixes ``?`` and
    ``+`` are taken as part of it.
    """
    n = len(text)
    j = i
    while j < n:
        if text.startswith("(?#", j):
            j = _scan_comment(text, j)
            continue
        if verbose and text[j] in _VERBOSE_WS:
            j += 1
            continue
        if verbose and text[j] == "#":
            j = _scan_verbose_comment(text, j)
            continue
        break
    if j >= n:
        return i
    char = text[j]
    if char in "*+?":
        j += 1
    elif char == "{":
        close = text.find("}", j)
        if close == -1:
            return i
        inner = text[j + 1:close]
        if not inner or _BRACE_QUANTIFIER.fullmatch(inner) is None:
            return i
        j = close + 1
    else:
        return i
    if j < n and text[j] in "?+":
        j += 1
    return j


def _assertion_free_superset(text: str, verbose: bool) -> Optional[str]:
    r"""``text`` with every zero-width assertion deleted.

    ``_detached_body`` answers "this text cannot be read outside its own
    pattern", and ``_matches_separators_only`` then takes the
    conservative road: True, "the wrap is separator-only", which puts
    the atomic tail on the capture. That is the safe answer only while
    the slice really could be nothing but separators, and one slice can
    carry BOTH an assertion and a mandatory literal.
    ``(?<=\w) to end`` pins where each iteration of
    ``^go(?: (\d+)(?<=\w) to end)+$`` ends, so the plain body matches
    "go fifteen to end" and returns in 0.000s on a twenty-word
    nonmatch. The refusal took the atomic tail anyway, that tail ate the
    " to" -- "to" is a count word -- and could not give it back, so a
    command a user can save in the Advanced editor accepted
    "go 15 to end" and refused the spoken form
    (wh-number-words-one-parser.1.28).

    Deleting an assertion can only ADD matches: a zero-width construct
    restricts where the text around it may match and consumes nothing
    itself. What comes back is therefore a superset of what ``text``
    matches, in the direction the caller needs. When no separator sample
    matches even the superset, none matches ``text`` either, so False is
    certain rather than guessed. That is the same superset argument
    ``_reference_replacements`` rests on, used here in a positive
    position, where a superset is sound.

    Lookaround of either sign is dropped WHOLE, contents included, so an
    ``ANY_TEXT`` that reached the slice inside a negation goes with it
    and the .1.25 reversal cannot return through this door. ``^``, ``$``
    and the ``\A \Z \b \B`` escapes are dropped as the single tokens
    they are; a ``^`` or ``$`` inside a character class is class text
    and is copied with the class. A capturing group becomes
    non-capturing for the reason ``_detached_body`` gives.

    A quantifier that repeats a dropped lookaround is dropped with
    it, because a quantified zero-width construct is still
    zero-width. Leaving it behind rebinds it to the text in front,
    which turns the widening into a narrowing (see the ``)`` branch
    below). Only the lookaround needs this: ``^*``, ``\b*``, ``$+``
    and ``\b{2}`` are each ``re.error: nothing to repeat``, so the
    single-token deletions can never be followed by one.

    None means the text holds something this cannot widen soundly: a
    surviving ``(?P=``, ``(?(`` or numeric BACKREFERENCE names a
    group the text does not carry, so the caller keeps its
    conservative answer. An octal escape is a character rather than
    a reference and is kept; ``_octal_escape_end`` draws the line.
    """
    out: List[str] = []
    stack: List[bool] = []
    depth = 0
    drop_at: Optional[int] = None
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "[":
            end = _scan_class(text, i)
            if drop_at is None:
                out.append(text[i:end])
            i = end
            continue
        if text.startswith("(?#", i):
            end = _scan_comment(text, i)
            if drop_at is None:
                out.append(text[i:end])
            i = end
            continue
        if verbose and char == "#":
            end = _scan_verbose_comment(text, i)
            if drop_at is None:
                out.append(text[i:end])
            i = end
            continue
        if char == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
                if end is None:
                    return None
                if drop_at is None:
                    out.append(text[i:end])
                i = end
                continue
            if drop_at is None and nxt not in _CONTEXT_ESCAPES:
                out.append(text[i:i + 2])
            i += 2
            continue
        if char in "^$":
            i += 1
            continue
        if text.startswith("(?P=", i) or text.startswith("(?(", i):
            return None
        if char == "(":
            open_len = _group_open_len(text, i)
            opener = text[i:i + open_len]
            depth += 1
            stack.append(verbose)
            verbose = _scoped_verbose_change(text, i, open_len, verbose)
            if drop_at is None:
                if opener[:3] in ("(?=", "(?!") or opener[:3] == "(?<":
                    drop_at = depth
                else:
                    out.append(
                        "(?:" if opener == "(" or opener.startswith("(?P<")
                        else opener
                    )
            i += open_len
            continue
        if char == ")":
            if stack:
                verbose = stack.pop()
            if drop_at == depth:
                drop_at = None
                depth -= 1
                # The quantifier goes with the lookaround it
                # repeats. Repeating a zero-width construct is
                # still zero-width, so deleting the pair is still a
                # widening -- but leaving the quantifier behind
                # rebinds it to whatever stands in front, and
                # ``\s(?=\w){4}`` then reads as ``\s{4}``, a
                # strict SUBSET (wh-number-words-one-parser.1.29).
                i = _quantifier_after(text, i + 1, verbose)
                continue
            if drop_at is None:
                out.append(char)
            depth -= 1
            i += 1
            continue
        if drop_at is None:
            out.append(char)
        i += 1
    return "".join(out)


def _reference_replacements(
    pattern_str: str,
    capture_opens: Dict[int, int],
    capture_names: Dict[int, str],
    group_content_start: Dict[int, int],
    group_close: Dict[int, int],
    group_inner_verbose: Dict[int, bool],
    base_verbose: bool,
) -> Dict[str, str]:
    r"""Each reference's own text, mapped to a superset of what it matches.

    A backreference matches exactly the text its group captured, so the
    group's body covers every string the reference can be. A conditional
    keeps only ``(?:`` from its opener here; ``_resolve_references``
    supplies the empty else branch that makes THAT a superset. Both are
    supersets, which is what ``_matches_separators_only`` needs to
    answer soundly for a fragment whose reference names a group outside
    it (wh-number-words-one-parser.1.17).

    Each body is a CLOSED superset: a body can itself hold a reference,
    and the resolver does not re-scan text it has just inserted, so a
    body that arrived unresolved would fail the compile and force the
    conservative answer (wh-number-words-one-parser.1.20). Groups are
    walked in the order they CLOSE, and each body is resolved against
    the map built so far, so every body is closed before anything uses
    it. Close order is what makes that work, and capture number is not:
    a group is numbered when it OPENS, so an outer group can legally
    reference a nested one whose number is HIGHER than its own --
    ``(( to)\2)`` is group 1 referencing group 2 -- and an ascending
    walk left that ``\2`` raw (wh-number-words-one-parser.1.22). A
    reference inside a body always names a group that closed before the
    reference, hence before the enclosing group closes, so close order
    is a valid order for it; that is what removes the need for a visited
    guard. A forward reference, which re rejects for the whole pattern
    anyway, stays unresolved, and ``_detached_body`` then refuses the
    body and keeps the conservative answer.

    ``_detached_body`` also decides whether a body survives the move at
    all. It returns None for a body that asks about its surroundings,
    and this maps such a reference to ``ANY_TEXT``
    (wh-number-words-one-parser.1.21); otherwise it hands back the body
    with its capturing groups made non-capturing, so two copies of one
    body in a single slice cannot collide
    (wh-number-words-one-parser.1.23).

    Each body is wrapped in an explicit ``(?x:`` or ``(?-x:`` carrying
    the mode INSIDE the group it came from, because the fragment the
    body is spliced into is compiled in the fragment's mode, not the
    group's.
    """
    references: Dict[str, str] = {}
    # A conditional keeps only ``(?:`` here, and THAT replacement
    # depends on no body at all, so every one of them is seeded before
    # the walk rather than at its own group's turn. re accepts a
    # numeric conditional whose target group has not closed --
    # ``re.compile(r"(?(1)a|b)(c)")`` succeeds -- so a body can name a
    # group that opens after that body closes, and a conditional left
    # unresolved in a body reaches ``_detached_body``, which refuses it
    # and makes the whole reference ANY_TEXT
    # (wh-number-words-one-parser.1.26).
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
        open_index = capture_opens[number]
        start = group_content_start.get(open_index)
        close = group_close.get(open_index)
        if start is None or close is None:
            continue
        inner = group_inner_verbose.get(open_index, base_verbose)
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
            if inner:
                # A '#' comment the body ends inside would otherwise
                # swallow the closer; re ignores the newline.
                body += "\n"
            body += ")"
        references["\\" + str(number)] = body
        name = capture_names.get(number)
        if name:
            references["(?P={0})".format(name)] = body
    return references


def _dangerous_captures(
    pattern_str: str,
    capture_spans: List[Tuple[int, int]],
    capture_levels: List[Tuple[bool, ...]],
    enclosures: List[List[int]],
    group_close: Dict[int, int],
    group_content_start: Dict[int, int],
    group_verbose: Dict[int, bool],
    group_alternations: Dict[int, List[int]],
    base_verbose: bool,
    references: Dict[str, str],
) -> Set[int]:
    r"""Which widened captures need the atomic tail?

    A widened body is slow only when a SECOND widened body can compete
    with it for the same run of words -- that is, when nothing between
    the two forces a real word in between. Measured with the plain tail
    against a twenty-four word run that cannot match:

      - ``^one(?: (B))+$``            15.236s   the repeat re-enters the
                                                body with only a space
                                                between the iterations;
      - ``^one(?: (B) to)+$``          0.000s   the literal " to" pins
                                                where each iteration ends;
      - ``^(B) one (B) one ...$``      0.003s   five captures, each pair
                                                separated by a literal;
      - bare ``(B) (B) ...``           0.000s at four captures, 0.012s at
                                                six, 0.215s at eight and
                                                1.636s at ten.

    So two things make a capture dangerous, and neither is "the pattern
    has a repeat" or "the pattern has many captures" on its own:

      1. it sits in a group that repeats, and the text between one
         iteration's body and the next can match separators only. With
         alternation, "the next" means the first capture of ANY branch,
         and the text after this capture is the rest of ITS OWN branch;
      2. it is not the last of a run of MORE than
         ``_MAX_PLAIN_CAPTURES`` captures that are adjacent to each
         other, adjacency meaning the text between them can match
         separators only.

    Only the EARLIER capture of an adjacent pair needs the atomic tail:
    once its tail cannot give a word back, the division is settled and
    the later capture takes what is left. Measured on the bare chain,
    marking every capture but the last: 0.000s at five and at eight
    captures, for a fifty-word run.

    A run of four or fewer adjacent captures keeps the plain tail. That
    is the measured safe side (0.000s), and it is what lets
    ``^go (\d+) (\d+) (\d+) (\d+) to end$`` still match
    "go one two three four to end".
    """
    dangerous: Set[int] = set()
    total = len(capture_spans)

    # 1. a capture whose own enclosing group repeats over it.
    for index, (_, end) in enumerate(capture_spans):
        for open_index in reversed(enclosures[index]):
            close = group_close.get(open_index)
            if close is None:
                # An unclosed group: re.compile refuses such a pattern
                # and the gate above already returns it unchanged. Answer
                # dangerous rather than guess.
                dangerous.add(index)
                break
            if not _repeat_quantifier_follows(
                pattern_str, close + 1,
                group_verbose.get(open_index, base_verbose),
            ):
                continue
            branches = _group_branches(
                group_content_start.get(open_index, close),
                close,
                group_alternations.get(open_index, ()),
            )
            own = next(
                (b for b in branches if b[0] <= end <= b[1]),
                (group_content_start.get(open_index, close), close),
            )
            # What one iteration puts between this body's end and the
            # next iteration's first body: the rest of THIS branch, then
            # some branch's opening up to that branch's first body. Any
            # branch will do, because the repeat may re-enter through any
            # of them, so the most dangerous one decides.
            tail = pattern_str[end:own[1]]
            hit = False
            for lo, hi in branches:
                first_inside = next(
                    (start for start, _ in capture_spans if lo <= start < hi),
                    None,
                )
                if first_inside is None:
                    continue
                if _matches_separators_only(
                    tail + pattern_str[lo:first_inside],
                    capture_levels[index],
                    references,
                ):
                    dangerous.add(index)
                    hit = True
                    break
            if hit:
                break

    # 2. runs of captures adjacent to each other in sequence.
    adjacent = [
        _matches_separators_only(
            pattern_str[capture_spans[k][1]:capture_spans[k + 1][0]],
            capture_levels[k],
            references,
        )
        for k in range(total - 1)
    ]
    start = 0
    while start < total:
        stop = start
        while stop < total - 1 and adjacent[stop]:
            stop += 1
        if stop - start + 1 > _MAX_PLAIN_CAPTURES:
            dangerous.update(range(start, stop))
        start = stop + 1
    return dangerous


#: The character a widened tail and the raw expression can both want.
_HYPHEN = "-"

#: Regex punctuation that consumes nothing, so it can never take the
#: hyphen. Tested as a standalone token each of these would either raise
#: re.error (``(`` alone) or match nothing (``^``), and the first of
#: those answers True, so they are skipped instead of tested.
_NON_CONSUMING_SYNTAX = "()|*+?{}^$"


def _slice_can_consume_a_hyphen(
    text: str, levels: Tuple[bool, ...]
) -> bool:
    r"""Can any token in ``text`` match a hyphen?

    Each token is asked with re rather than read by eye, because the
    reading has to be right about ranges: ``[a-z]`` holds a hyphen
    character and matches no hyphen, while ``[-a]`` and ``[^a]`` both
    do. ``re.fullmatch(token, "-")`` is right about all three by
    construction, and it is equally right about the spellings a user
    may reach for -- ``(?:-)`` reduces to the same ``-`` token, and
    ``\x2d``, ``\055`` and ``.`` each answer for themselves.

    True is the CONSERVATIVE answer here: it makes the caller drop the
    hyphen from that capture's tail, which costs the hyphenated spoken
    form of a multi-word count and can never change a number. So a
    token that will not compile on its own answers True rather than
    guessing.

    Group OPENINGS are skipped whole, because they consume nothing and
    two of them hold a bare hyphen that is flag syntax rather than
    text: ``(?-x:`` and ``(?-i:``. Reading that hyphen as a token put
    the hyphen-free tail on ``(?x)^one(?: (?-x:\s(\d+)) )+$``, whose
    capture no hyphen follows at all. ``(?#...)`` comment groups are
    skipped whole for the same reason.

    Verbose-mode ``#`` comments ARE skipped, and ``verbose`` says
    whether the x flag is in force where the slice begins. An earlier
    version of this scan carried no verbose state, on the claim that
    reading a comment as ordinary pattern text was the conservative
    direction. That claim was false in one direction and cost a wrong
    number (wh-number-words-one-parser.1.35): comment text can OPEN a
    token that swallows the real delimiter behind it. Measured on
    ``(?x:^(?:(\d+) # [a`` then a newline then ``-#z]``, the ``[``
    inside the comment opened the class ``[a\n-#z]``, which spans the
    newline, matches no hyphen, and so answered False before the scan
    ever reached the real ``-``. The capture kept the hyphen-crossing
    tail and read "two-three-" as 23 where the raw expression reads
    "2-3-" with the capture at "3".

    Scoped ``(?x:`` and ``(?-x:`` changes are followed as well, because
    a slice can begin outside a scope and end inside one.

    ``levels`` is the chain of x modes around the slice, outermost
    first, the same shape ``_balanced_for_compile`` takes
    (wh-number-words-one-parser.1.16). It has to be the whole chain,
    not one boolean: a slice can begin INSIDE a scoped flag group and
    reach that group's ``)``, and the text behind that ``)`` is
    governed by the parent scope. Measured at 882bf54f, when this scan
    started with an empty stack, on ``^one(?x:\s(\d+))#-$``: the slice
    ``)#-$`` stayed verbose, the outer ``#-`` read as a comment, the
    scan never reached the real hyphen, and the capture kept the
    hyphen-crossing tail, so "one two-three#-" read as 23 where the
    raw expression accepts "one 2#-" alone
    (wh-number-words-one-parser.1.37).
    """
    i = 0
    n = len(text)
    # ``outer`` is consumed by the ')' characters the slice cuts
    # through; ``stack`` carries the groups that open inside it.
    outer = list(levels) or [False]
    verbose = outer[-1]
    stack: List[bool] = []
    while i < n:
        char = text[i]
        if verbose and char == "#":
            i = _scan_verbose_comment(text, i)
            continue
        if char == "\\":
            end = None
            nxt = text[i + 1:i + 2]
            if nxt in _ASCII_DIGITS:
                end = _octal_escape_end(text, i)
            if end is None:
                if nxt == "x":
                    end = i + 4
                elif nxt == "u":
                    end = i + 6
                elif nxt == "U":
                    end = i + 10
                elif nxt == "N" and text[i + 2:i + 3] == "{":
                    close = text.find("}", i + 2)
                    end = i + 2 if close == -1 else close + 1
                else:
                    end = i + 2
            token = text[i:end]
            i = end
        elif char == "[":
            end = _scan_class(text, i)
            token = text[i:end]
            i = end
        elif text.startswith("(?#", i):
            i = _scan_comment(text, i)
            continue
        elif char == "(":
            open_len = _group_open_len(text, i)
            stack.append(verbose)
            verbose = _scoped_verbose_change(text, i, open_len, verbose)
            i += open_len
            continue
        elif char == ")":
            if stack:
                verbose = stack.pop()
            elif len(outer) > 1:
                outer.pop()
                verbose = outer[-1]
            else:
                # The closes run past the recorded chain, so no mode is
                # known for the rest of the slice. True is the
                # conservative answer here, the same refusal
                # ``_balanced_for_compile`` makes by returning None.
                return True
            i += 1
            continue
        elif char in _NON_CONSUMING_SYNTAX:
            i += 1
            continue
        else:
            token = char
            i += 1
        try:
            if re.fullmatch(token, _HYPHEN) is not None:
                return True
        except re.error:
            return True
    return False


def _hyphen_bounded_captures(
    pattern_str: str,
    capture_spans: List[Tuple[int, int]],
    capture_levels: List[Tuple[bool, ...]],
    enclosures: List[List[int]],
    group_close: Dict[int, int],
    group_content_start: Dict[int, int],
    group_verbose: Dict[int, bool],
    group_inner_verbose: Dict[int, bool],
    base_verbose: bool,
    references: Dict[str, str],
) -> Set[int]:
    r"""Which widened captures must drop the hyphen from their tail?

    The tail joins two words of ONE spoken count with ``[\s-]``. When
    the raw expression ALSO consumes a hyphen after the capture, both
    readings match the same text and the greedy one wins, so the
    widened form finished a different number of iterations than the raw
    expression and handed the caller a different number
    (wh-number-words-one-parser.1.34). Measured before this answer
    existed: ``^(?:(\d+)-)+$`` read "two-three-" with the capture at
    "two-three", which the parser turns into 23, where the raw
    expression reads "2-3-" with the capture at "3".

    Two slices can put a hyphen after a capture, and both are checked:

      1. everything to the right of the capture, to the end of the
         pattern. This is what catches ``^(\d+)-$``, which needs
         neither a repeat nor a second capture: it accepted
         "two-three-" where the raw expression refuses "2-3-";
      2. the opening of any group whose opening a repeat can RE-ENTER
         after the capture, from that group's content start to the
         capture. ``^(?:-(\d+))+$`` puts its hyphen there rather than
         to the right.

    "Re-enters" is what the second slice needs, and it is narrower than
    "encloses". A group entered once, before the capture, cannot put
    its opening after the capture at all, so a hyphen written there
    competes with nothing. Reading every enclosure alike refused a
    spoken form the raw expression accepts
    (wh-number-words-one-parser.1.36): ``^section(?:[- ]?(\d+))$``
    accepts both "section 23" and "section-23", yet the transform gave
    it the hyphen-free body, so it matched "section twenty" and refused
    "section twenty-three".

    An inner wrapper that carries no quantifier of its own is still
    traversed again whenever an ANCESTOR repeats, and it needs no pass
    of its own to be covered. The ancestor's slice runs from the
    ancestor's content start to the capture, so it SPANS every group
    nested in between, and the scan reads that span in the mode re
    itself uses there. A nested group's slice is therefore always a
    suffix of a repeating ancestor's slice, and asking about it
    separately can only repeat an answer already given. Measured:
    with the ancestor pass patched out, all 720 transform, survey and
    count-word tests still passed, ``^one(?:(?:-(\d+)) )+$`` included,
    whose inner wrapper is exactly that shape.

    The repeat test is the same ``_repeat_quantifier_follows`` call
    ``_dangerous_captures`` makes, read in the mode in force OUTSIDE
    the group, because that is where the quantifier sits.

    The first slice is deliberately the whole rest of the pattern
    rather than the text up to the next capture. A hyphen further right
    cannot be reached without passing the text between, but proving
    that needs the branch and quantifier reasoning this file has had to
    correct repeatedly (.1.24, .1.25, .1.28), and the reward is small:
    all 319 shipped patterns were parsed, 113 carry a numeric capture,
    and NONE of them can consume a hyphen anywhere after it, so the
    wider slice costs the shipped catalog nothing. What it costs a
    user's own pattern is the hyphenated spoken form of a multi-word
    count in a pattern that also writes a hyphen after the capture --
    a refusal, never a wrong number, and the same trade the atomic body
    already makes (.1.6).

    The second slice is NOT narrowed to the text before the FIRST
    capture of the group. A repeat re-enters the whole opening, and a
    group may hold several captures, so the slice runs to the capture
    being asked about.

    Both slices go through ``_resolve_references`` first, the same way
    ``_matches_separators_only`` resolves before it compiles. A
    backreference names a group that may sit outside the slice, so
    ``\1`` cannot compile on its own and would answer the conservative
    True for every pattern that carries one: measured on
    ``^go(\040to end)(?: (\d+)\1)+$``, whose group 1 is " to end" and
    holds no hyphen at all.
    """
    bounded: Set[int] = set()
    for index, (start, end) in enumerate(capture_spans):
        levels = capture_levels[index] if index < len(capture_levels) else ()
        levels = levels or (base_verbose,)
        # The right-hand slice runs to the end of the pattern, so it
        # crosses every enclosing group's ')'. It needs the whole chain
        # (wh-number-words-one-parser.1.37).
        after = _resolve_references(pattern_str[end:], levels, references)
        if _slice_can_consume_a_hyphen(after, levels):
            bounded.add(index)
            continue
        for open_index in enclosures[index]:
            close = group_close.get(open_index)
            if close is None:
                # An unclosed group: re.compile refuses such a pattern
                # and the caller returns it unchanged. Answer bounded
                # rather than guess, the same way _dangerous_captures
                # answers dangerous.
                bounded.add(index)
                break
            if not _repeat_quantifier_follows(
                pattern_str, close + 1,
                group_verbose.get(open_index, base_verbose),
            ):
                continue
            inner = group_inner_verbose.get(open_index, base_verbose)
            # An opening slice runs from the group's own content
            # start to a capture inside that group, so every ')' in it
            # closes a group that also opened in it. It cuts through no
            # ')' and needs no chain beyond its own mode.
            opening = _resolve_references(
                pattern_str[
                    group_content_start.get(open_index, close):start
                ],
                (inner,),
                references,
            )
            if _slice_can_consume_a_hyphen(opening, (inner,)):
                bounded.add(index)
                break
    return bounded


#: Group openings that consume no input. A quantified dot inside one of
#: these cannot swallow buffer words, so it is not a greedy tail
#: (wh-review-pattern-fixes.22).
_ZERO_WIDTH_OPENINGS = ("(?=", "(?!", "(?<=", "(?<!")


def _scan_class(text: str, i: int) -> int:
    r"""Index just past the character class that opens at ``text[i]``.

    ``text[i]`` is ``[``. Python's re lexing (re/_parser.py, wh-review-
    pattern-fixes.24): a ``]`` immediately after ``[`` or ``[^`` is a
    LITERAL class member (``]`` only closes a non-empty set), and an
    escape pair such as ``\]`` never closes; the class ends at the next
    bare ``]``. Returns ``len(text)`` for an unterminated class -- such a
    source does not compile, and the callers' compile gates reject it.
    """
    n = len(text)
    j = i + 1
    if j < n and text[j] == "^":
        j += 1
    if j < n and text[j] == "]":
        j += 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "]":
            return j + 1
        j += 1
    return n


def _scan_comment(text: str, i: int) -> int:
    r"""Index just past the comment group ``(?#...)`` opening at ``text[i]``.

    Python's re tokenizer yields an escape pair as ONE token, so inside a
    comment ``\)`` does NOT end it: the comment ends at the first BARE
    ``)``, with no nesting and no class syntax (re/_parser.py's comment
    loop breaks only on the ``)`` token). Verified empirically on Python
    3.12: ``re.compile(r'(?#a\)b)c')`` matches ``'c'``, and
    ``re.compile(r'x(?#ab\)y')`` raises "missing ), unterminated comment"
    (wh-review-pattern-fixes.24). Returns ``len(text)`` for an
    unterminated comment, which likewise does not compile.
    """
    n = len(text)
    j = i + 3
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == ")":
            return j + 1
        j += 1
    return n


#: The whitespace characters Python's re parser ignores while the x
#: (verbose) flag is active (re/_parser.py WHITESPACE, Python 3.12).
_VERBOSE_WS = " \t\n\r\v\f"

#: A global inline-flag group. Python 3.11+ requires global flags at the
#: very start of the pattern ("global flags not at the start of the
#: expression" otherwise, verified empirically), and the off-flag form
#: ``(?-x)`` without a colon does not exist ("missing :"), so scanning
#: consecutive add-only groups from position 0 is the complete rule.
_GLOBAL_FLAGS_RE = re.compile(r"\(\?[aiLmsux]+\)")


def _global_verbose(pattern_str: str) -> bool:
    """True when a global flag group at the pattern start turns on x.

    ``(?x)^a b$`` puts the WHOLE pattern in verbose mode: unescaped
    whitespace outside character classes is ignored and ``#`` starts a
    comment (wh-review-pattern-fixes.29). Several leading flag groups
    may chain (``(?i)(?x)...``, verified empirically), so every one is
    scanned.
    """
    i = 0
    while True:
        m = _GLOBAL_FLAGS_RE.match(pattern_str, i)
        if m is None:
            return False
        if "x" in m.group(0):
            return True
        i = m.end()


def _scoped_verbose_change(
    text: str, i: int, open_len: int, current: bool
) -> bool:
    """Verbose state INSIDE the group whose opening starts at ``text[i]``.

    Only a scoped flag group ``(?flags-flags:...)`` changes the x flag:
    ``(?x:...)`` turns it on, ``(?-x:...)`` turns it off, and every other
    opening (plain, named, non-capturing, zero-width, atomic, conditional)
    inherits ``current``. ``open_len`` is ``_group_open_len``'s answer, so
    a flag-group opening is exactly ``(?`` + letters + optional ``-`` +
    letters + ``:``.
    """
    opening = text[i:i + open_len]
    if not opening.startswith("(?") or not opening.endswith(":"):
        return current
    if opening.startswith(("(?P<", "(?<")):
        return current
    added, _, removed = opening[2:-1].partition("-")
    if "x" in removed:
        return False
    if "x" in added:
        return True
    return current


def _scan_verbose_comment(text: str, i: int) -> int:
    r"""Index just past the verbose-mode comment starting at ``text[i]``.

    ``text[i]`` is an unescaped ``#`` lexed while the x flag is active.
    Python's parser then consumes TOKENS until one equals a bare newline
    character (re/_parser.py:536-540 on 3.12); the tokenizer folds an
    escape pair into ONE token, so ``\n`` (backslash + n) and even a
    backslash followed by a real newline are comment text that does NOT
    end the comment -- only a bare newline character does. Verified
    empirically (wh-review-pattern-fixes.29): the raw-string form of the
    finding's repro does not even compile, while the real-newline form
    compiles with one capture. Returns ``len(text)`` when no bare newline
    follows -- the rest of the pattern is comment text, exactly as
    re.compile treats it.
    """
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "\n":
            return j + 1
        j += 1
    return n


def _zero_width_verbose_scope_end(
    text: str, start: int, open_len: int
) -> Optional[int]:
    r"""Index just past the ``)`` of a provably empty verbose scope.

    ``text[start]`` opens a scoped flag group that turns the x flag ON
    (``_scoped_verbose_change`` proved it). Returns the index just after
    the group's ``)`` when the group provably matches ONLY the empty
    string, or None when any part of it is unproven.

    The proved grammar is exactly four things, and every one of them is
    zero-width while the x flag is active:

      - whitespace re ignores (``_VERBOSE_WS``),
      - a verbose ``#`` comment (``_scan_verbose_comment``),
      - a comment group ``(?#...)`` (``_scan_comment``), and
      - a NESTED scoped group that itself turns x on and is itself
        provably empty by this same rule (wh-review-pattern-fixes.44).
        The scan treated every nested ``(`` as live content, so
        ``^look(?x:(?x: # c\n)) up now$`` lost its whole body even though
        it fullmatches "look up now".

    EVERYTHING else returns None, including shapes that look harmless:

      - a plain ``(`` or ``(?:`` group, whose deletion would renumber the
        pattern's captures,
      - an assertion. ``(?x:(?! ))`` is ``(?!)`` -- a negative lookahead
        on the empty pattern, which ALWAYS fails, so
        ``^look(?x:(?! )) up now$`` matches NOTHING (verified
        empirically). Deleting it would hand the router the body
        "look up now" for a command no user can speak.
      - a ``(?-x:...)`` scope, whose whitespace is required literal text,
      - literal characters, an escape pair, and a character class,
      - a scope that never closes.

    A quantifier (``?`` ``*`` ``+`` ``{``) after ANY of these ``)``
    characters also returns None. At the OUTERMOST level that rule is
    load-bearing: the group is a real (empty) group node, so deleting
    ``(?x: )`` out of ``a(?x: )?`` would re-attach the ``?`` to the ``a``
    and turn "a required" into "a optional". At a nested level the check
    changes no answer -- a quantifier character there is not in the
    proved grammar, so the walk's final ``return None`` already refuses
    it. Measured: a mutation that deletes the check fails only the
    outermost-level guard (``test_quantified_zero_width_scope_is_
    conservative``), never a nested one.

    The walk carries its own depth counter instead of calling itself, so
    a deeply nested source cannot raise RecursionError here. Measured on
    CPython 3.12 with the default recursion limit of 1000: this walk
    answers ``look up now`` for 5000 nested scopes, while ``re.compile``
    on the same source raises RecursionError from 1000 levels up.
    """
    n = len(text)
    j = start + open_len
    depth = 1
    while depth:
        if j >= n:
            return None
        char = text[j]
        if char in _VERBOSE_WS:
            j += 1
            continue
        if char == "#":
            j = _scan_verbose_comment(text, j)
            continue
        if text.startswith("(?#", j):
            j = _scan_comment(text, j)
            continue
        if char == "(":
            inner_len = _group_open_len(text, j)
            # current=False asks the one question the rule allows: does
            # this opening turn the x flag ON by itself? Every other
            # opening -- '(', '(?:', '(?=', '(?!', '(?<=', '(?<!',
            # '(?P<n>', '(?i:', '(?-x:' -- answers False and is refused.
            if not _scoped_verbose_change(text, j, inner_len, False):
                return None
            j += inner_len
            depth += 1
            continue
        if char == ")":
            j += 1
            depth -= 1
            if j < n and text[j] in "?*+{":
                return None
            continue
        return None
    return j


def _delete_zero_width_verbose_scopes(text: str) -> Optional[str]:
    r"""Delete verbose flag groups that provably match only empty string.

    Returns the rewritten text, or None when some active-verbose scope in
    ``text`` cannot be proven zero-width -- the conservative signal for
    the extractors to fall back to '' (the same precedent as the
    unclosed-conditional fallback): literal text lexed inside an
    active-verbose scope relies on whitespace re ignores, so extracting
    it textually is never safe.

    A scope IS deleted exactly, like a ``(?#...)`` comment, when its
    content consists only of ignored whitespace, verbose ``#`` comments,
    ``(?#...)`` comments, and NESTED scoped groups that turn x on and are
    themselves empty by this same rule (wh-review-pattern-fixes.44): such
    a group matches the empty string at its position, so the remaining
    source matches exactly the strings the original matched. Two
    provisos, both verified empirically (wh-review-pattern-fixes.29):

      - the group is a real (empty) group node, so a quantifier after its
        ``)`` applies to the GROUP -- unlike a ``(?#...)`` comment, whose
        following quantifier re attaches to the item before it. Deleting
        ``(?x: )`` out of ``a(?x: )?`` would turn "a required" into "a
        optional", so a scope followed by ``?`` ``*`` ``+`` or ``{`` is
        not deletable (None).
      - any other content (literal characters, classes, and every group
        that is not a nested empty x-enabling scope -- a capture, a
        ``(?:`` group, an assertion, a ``(?-x:`` scope) makes the scope
        live: None. ``_zero_width_verbose_scope_end`` holds the exact
        list and the reason for each refusal.

    Callers must rule out global verbose (``_global_verbose``) first;
    this walk starts with the x flag off, so a ``#`` outside every scope
    is an ordinary character, and only x-ENABLING scoped groups are
    treated (``(?i:...)`` and friends are ordinary balanced text).
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
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
        if char == "(":
            open_len = _group_open_len(text, i)
            if _scoped_verbose_change(text, i, open_len, False):
                end = _zero_width_verbose_scope_end(text, i, open_len)
                if end is None:
                    # Unclosed scope, a quantified scope, or live content
                    # at any nesting level.
                    return None
                i = end
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _delete_comments(text: str) -> str:
    r"""Delete every comment group ``(?#...)`` from ``text``.

    A comment is zero-width, so the deletion is exact: the remaining
    source matches exactly the strings the original matched (a quantifier
    after a comment already applies to the item before it -- ``a(?#x)+``
    compiles to ``a+``). Escape pairs and character-class interiors are
    copied verbatim, because a ``(?#`` inside a class is literal text,
    not a comment. ``extract_literal_prefix`` and
    ``extract_full_literal_body`` run this over their extracted text so a
    prefix or body never carries comment text into the anchored matchers
    (wh-review-pattern-fixes.24: the repair helpers previously parsed
    ``(?#...)`` as ordinary nested parens and produced garbage prefixes).
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if char == "[":
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            continue
        if text.startswith("(?#", i):
            i = _scan_comment(text, i)
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _find_greedy_tail_spans(pattern_str: str) -> List[Tuple[int, int, int]]:
    r"""Return (start, end, open_groups) for every REAL greedy tail.

    A real greedy tail is a ``.`` immediately followed by ``*`` or ``+``
    where the dot is:

      - NOT escaped (the scanner consumes backslash pairs, so only a dot
        behind an even number of backslashes is reached),
      - NOT inside a character class ``[...]``, and
      - NOT inside a zero-width group (``(?=``, ``(?!``, ``(?<=``,
        ``(?<!``) or a comment group ``(?#``, at any nesting depth. A
        quantified dot there consumes no input, so it cannot make the
        pattern greedy (wh-review-pattern-fixes.22); the old scanner
        counted it, and the balanced construct compiled, so a bounded
        command like ``^set (?=.+)mode$`` was routed onto the greedy
        timer.

    The scanner is a group-structure parser: every unescaped ``(`` pushes
    a frame (consuming or zero-width) and every unescaped ``)`` pops one.
    A comment group and a character class are skipped whole with Python's
    own lexical rules (wh-review-pattern-fixes.24): ``(?#...)`` ends at
    the first BARE ``)`` -- an escape pair ``\)`` is one token and does
    not end it (``_scan_comment``) -- and a ``]`` immediately after ``[``
    or ``[^`` is a literal class member, so the class ends at the next
    bare ``]`` (``_scan_class``). The old scanner closed the class of
    ``^do []a.+]+ say hello$`` at its first ``]`` and saw the
    class-internal ``.+`` as a span. The old textual detection (a plain
    substring test) misfired on ``\.+`` and ``[.+]``
    (wh-review-pattern-fixes.18); this scanner is the single source of
    truth for what counts as a greedy tail.

    A span is the dot plus its quantifier ONLY -- the old one-character
    paren extension is gone (wh-review-pattern-fixes.21):
    ``greedy_tail_probe_source`` now closes open groups itself, and
    ``extract_literal_prefix`` repairs them, so the span no longer needs
    to absorb adjacent parens. ``open_groups`` is the number of group
    frames open at the span's end; the probe appends that many ``)``.
    A lazy quantifier (``.+?``) still yields a span, and the ``?`` is not
    part of it.

    "Immediately followed" means after every ZERO-WIDTH thing re lexes
    between the dot and the quantifier, because the quantifier still
    attaches to the dot across such text. A ``(?#...)`` comment group is
    zero-width in every mode: ``^say .(?# note)+$`` fullmatches
    "say hello" (verified empirically). The look-ahead skipped only
    verbose whitespace and verbose ``#`` comments, so that pattern had no
    span, ``has_greedy_tail`` answered False, and
    ``extract_full_literal_body`` handed the catalog the regex text
    ``say .+`` as a literal body (wh-review-pattern-fixes.43). The rule
    covers one or more comment groups in a row and a comment whose text
    holds an escaped ``\)``. It does NOT cover a literal space before the
    comment outside verbose mode (``^say . (?# c)+$``): the space is a
    real atom there and the ``+`` quantifies IT, so that pattern is
    bounded and stays bounded.

    Verbose mode (wh-review-pattern-fixes.29): the scanner tracks the x
    flag -- global ``(?x)`` at the pattern start, scoped ``(?x:...)`` /
    ``(?-x:...)`` groups per stack frame -- and while it is active an
    unescaped ``#`` outside a character class starts a comment that runs
    to the next bare newline (``_scan_verbose_comment``). A quantified
    dot inside such a comment is comment TEXT, never a span: the old
    scanner saw the ``.+`` in ``^look(?x: # .+ comment\n) up now$`` and
    both extractors mis-fired on the bounded pattern. Verbose mode also
    ignores whitespace and comments BETWEEN a dot and its quantifier
    (``(?x:. +)`` compiles to ``.+``, verified empirically), so the span
    check skips them before looking for ``*`` / ``+``.
    """
    spans: List[Tuple[int, int, int]] = []
    stack: List[Tuple[str, bool]] = []  # ("consuming"|"zero", verbose)
    base_verbose = _global_verbose(pattern_str)
    i = 0
    n = len(pattern_str)
    while i < n:
        verbose = stack[-1][1] if stack else base_verbose
        char = pattern_str[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _scan_class(pattern_str, i)
            continue
        if verbose and char == "#":
            i = _scan_verbose_comment(pattern_str, i)
            continue
        if char == "(":
            if pattern_str.startswith("(?#", i):
                # Zero-width and self-contained: skip it whole, so a
                # quantified dot inside it is never a span and the ')'
                # that ends it never pops a group frame.
                i = _scan_comment(pattern_str, i)
                continue
            open_len = _group_open_len(pattern_str, i)
            inner_verbose = _scoped_verbose_change(
                pattern_str, i, open_len, verbose
            )
            if any(pattern_str.startswith(o, i) for o in _ZERO_WIDTH_OPENINGS):
                stack.append(("zero", inner_verbose))
            else:
                stack.append(("consuming", inner_verbose))
            i += 1
            continue
        if char == ")":
            if stack:
                stack.pop()
            i += 1
            continue
        if char == ".":
            j = i + 1
            # Zero-width text between the dot and its quantifier is
            # transparent to re, so the quantifier still applies to the
            # dot. A comment group is zero-width in EVERY mode (probe:
            # '^say .(?# note)+$' fullmatches 'say hello'); verbose
            # ignored whitespace and '#' comments only while the x flag is
            # active (probe: '(?x:a . +)' fullmatches 'abcd'). The comment
            # group was missing here, so a real greedy tail written as
            # '.(?# c)+' had no span at all (wh-review-pattern-fixes.43).
            while j < n:
                if pattern_str.startswith("(?#", j):
                    j = _scan_comment(pattern_str, j)
                    continue
                if verbose:
                    if pattern_str[j] in _VERBOSE_WS:
                        j += 1
                        continue
                    if pattern_str[j] == "#":
                        j = _scan_verbose_comment(pattern_str, j)
                        continue
                break
            if j < n and pattern_str[j] in "*+":
                if all(kind == "consuming" for kind, _ in stack):
                    spans.append((i, j + 1, len(stack)))
                i = j + 1
                continue
            i += 1
            continue
        i += 1
    return spans


def _group_open_len(text: str, i: int) -> int:
    """Length of the group-opening syntax starting at ``text[i]``.

    The opening runs from the ``(`` through the last character before the
    group's content: 1 for a plain capturing ``(``, 3 for ``(?:``, through
    the ``>`` for ``(?P<name>`` / ``(?<name>``, through the ``:`` for an
    inline-flags group ``(?i:``. Used by the prefix repair to drop an
    unclosed group's opening without leaving fragments like ``?P<tail>``
    behind (wh-review-pattern-fixes.21).
    """
    if not text.startswith("(?", i):
        return 1
    j = i + 2
    if text.startswith("P<", j):
        close = text.find(">", j)
        return (close + 1 - i) if close != -1 else len(text) - i
    if text.startswith("<=", j) or text.startswith("<!", j):
        return 4
    if text.startswith("<", j):
        close = text.find(">", j)
        return (close + 1 - i) if close != -1 else len(text) - i
    if j < len(text) and text[j] in "=!:>#":
        return j + 1 - i
    # Inline flags: (?i: ...). Scan the flag letters; include the ':' when
    # present so the content starts cleanly after it.
    k = j
    while k < len(text) and text[k] in "aiLmsux-":
        k += 1
    if k < len(text) and text[k] == ":":
        return k + 1 - i
    return j - i


def _unclosed_group_positions(text: str) -> List[int]:
    """Indices of every unescaped ``(`` in ``text`` that never closes.

    Innermost last. Skips escape pairs, character-class interiors, and
    comment groups with the same Python lexical rules the span scanner
    uses (``_scan_class`` / ``_scan_comment``,
    wh-review-pattern-fixes.24).
    """
    stack: List[int] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _scan_class(text, i)
            continue
        if text.startswith("(?#", i):
            i = _scan_comment(text, i)
            continue
        if char == "(":
            stack.append(i)
        elif char == ")" and stack:
            stack.pop()
        i += 1
    return stack


def _last_top_level_bar(text: str) -> Optional[int]:
    """Index of the last ``|`` in ``text`` at paren depth 0, or None.

    "Top level" is relative to ``text`` itself: a ``|`` inside a nested
    balanced group, a character class, or a comment group does not count.
    Escape pairs, classes, and comments are skipped with the same Python
    lexical rules the span scanner uses (wh-review-pattern-fixes.24).
    """
    last: Optional[int] = None
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            i = _scan_class(text, i)
            continue
        if text.startswith("(?#", i):
            i = _scan_comment(text, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            last = i
        i += 1
    return last


def _repair_unclosed_groups(text: str) -> str:
    """Rewrite ``text`` so no group is left unclosed.

    ``extract_literal_prefix`` cuts the pattern immediately before a
    greedy-tail dot, so a tail wrapped in group syntax leaves its group
    open in the cut text -- ``look up (?:`` for ``^look up (?:.*)$``
    (wh-review-pattern-fixes.21). Repair innermost outward: for each
    unclosed group, keep only the content after its last top-level ``|``
    (the literal opening of the tail's own alternation branch), or the
    whole content when the group has no top-level ``|``, and splice that
    onto the balanced text before the group opening. The result is exact
    by construction: every buffer the repaired prefix keeps continuable
    can extend to a full match through that branch.
    """
    while True:
        unclosed = _unclosed_group_positions(text)
        if not unclosed:
            return text
        open_idx = unclosed[-1]
        content = text[open_idx + _group_open_len(text, open_idx):]
        bar = _last_top_level_bar(content)
        kept = content if bar is None else content[bar + 1:]
        text = text[:open_idx] + kept


def greedy_tail_probe_source(pattern_str: str) -> Optional[str]:
    r"""Return the truncated pattern body Strategy 2's path probe fullmatches.

    The body is ``pattern_str`` cut immediately after the END of the LAST
    real greedy-tail span, with one ``)`` appended for every group still
    open at that point (wh-review-pattern-fixes.21), a leading ``^``
    stripped, and no trailing ``$``. ``PatternMatcher.can_continue``
    compiles it as ``^<body>$`` and fullmatches the buffer: a match proves
    the trailing buffer words are consumable by the greedy tail on the
    matched path, so ``^(mark|say .+)$`` no longer holds ``mark zzq`` back
    on its bounded ``mark`` branch (wh-review-pattern-fixes.18).

    The appended parens close the groups the cut severed, so a tail
    wrapped in group syntax keeps its greedy classification:
    ``^look up ((.*))$`` yields ``look up ((.*))``, where the old cut
    (``look up ((.*``) did not compile and dropped the pattern's greedy
    metadata entirely. ``^say (.+) twice$`` yields ``say (.+)``, the same
    body the old paren-extension span shape produced.

    Returns None when no real span exists -- which now covers a
    quantified dot inside a zero-width or comment construct, such as
    ``^set (?=.+)mode$`` or ``^mark (?=ok(?: .+)?)ok$``
    (wh-review-pattern-fixes.22): the dot consumes nothing there, so the
    pattern is bounded. The compile gate stays as a safety net for any
    truncation the paren repair cannot make valid.
    """
    spans = _find_greedy_tail_spans(pattern_str)
    if not spans:
        return None
    _start, end, open_groups = spans[-1]
    truncated = pattern_str[:end] + ")" * open_groups
    if truncated.startswith("^"):
        truncated = truncated[1:]
    try:
        re.compile("^" + truncated, re.IGNORECASE)
    except re.error:
        return None
    return truncated


def has_greedy_tail(pattern_str: str) -> bool:
    r"""Return True when ``pattern_str`` has a REAL, reachable greedy tail.

    True only when ``_find_greedy_tail_spans`` finds at least one real span
    (unescaped dot, outside any character class and any zero-width or
    comment group, quantified with ``*`` or ``+``) AND the truncated,
    group-repaired body compiles de-anchored -- i.e.
    ``greedy_tail_probe_source`` returns a body. The old answer was a
    textual scan that also fired on ``\.+`` and ``[.+]``
    (wh-review-pattern-fixes.18) and on lookaround dots like
    ``^set (?=.+)mode$`` (wh-review-pattern-fixes.22), which mis-routed
    bounded patterns onto the greedy catalog branch and the router's
    greedy timer; the group-closing repair also restores greedy
    classification to tails wrapped in group syntax, such as
    ``^look up ((.*))$`` (wh-review-pattern-fixes.21).

    ``PatternMatcher.can_continue`` uses this as the fallback greediness
    test for synthetic catalogs whose pattern data lacks the ``is_greedy``
    flag (wh-review-pattern-fixes.17). ``extract_literal_prefix`` and
    ``extract_full_literal_body`` share the same scanner, but their span
    test skips the compile gate: a pattern whose tail truncation does not
    compile still has a span, so it is excluded from BOTH extractors.
    """
    return greedy_tail_probe_source(pattern_str) is not None


def extract_literal_prefix(pattern_str: str) -> str:
    """Strip anchors / boundaries / greedy tail and return the literal core.

    Example: ``\\bangle brackets(.*)$`` -> ``angle brackets``.
             ``^hey Google.*$``         -> ``hey Google``.

    Returns an empty string when the source has no real greedy-tail span
    (``_find_greedy_tail_spans``; an escaped ``\\.+``, a class ``[.+]``,
    or a lookaround dot like ``(?=.+)`` is not one,
    wh-review-pattern-fixes.18/.22) or when stripping leaves nothing.
    When several spans exist, the prefix stops at the FIRST one. The
    greedy timer and the catalog's greedy-prefix fields key off that
    contract, so it must not change; a NON-greedy anchored pattern's whole
    body is extracted by ``extract_full_literal_body`` instead
    (wh-review-pattern-fixes.12).

    The cut lands immediately before the span's dot, so a tail wrapped in
    group syntax leaves its group open in the cut text.
    ``_repair_unclosed_groups`` then rewrites the text, innermost group
    outward, keeping only the tail branch's own literal opening
    (wh-review-pattern-fixes.21): ``^look up (?:.*)$`` -> ``look up``,
    ``^(?:go home|look up .+)$`` -> ``look up``, ``^(mark|say .+)$`` ->
    ``say``. A balanced zero-width group before the span stays in the
    prefix text (``^look up (?=.+)thing (.+)$`` ->
    ``look up (?=.+)thing``); ``build_literal_prefix_matchers``' raw-text
    fallback still compiles its full matcher and word truncations.

    Two rules from wh-review-pattern-fixes.24 refine the cut text before
    the repair runs:

      - Comment groups ``(?#...)`` are zero-width, so they are DELETED
        from the cut text (``_delete_comments``; the deletion is exact).
        The repair previously parsed them as nested parens:
        ``^say (?:look(?# note (with pipe |) up .+)$`` produced the
        garbage prefix ``say look note (with pipe |) up`` instead of
        ``say look up``.
      - A conditional group ``(?(id)yes|no)`` is NOT ordinary
        alternation: the branch taken depends on whether group ``id``
        matched, so the keep-the-last-branch rewrite can fabricate a
        prefix that admits a buffer no completion can match
        (``say (a )?look`` for ``^say (a )?(?(1)foo|look .+)$``, whose
        buffer ``say a look`` forces the ``foo`` branch). When the cut
        runs through an UNCLOSED conditional -- the tail sits inside one
        -- this function returns the conservative empty prefix instead.
        The pattern keeps its greedy classification (its dot is real and
        consuming) and Strategy 2's path-level fullmatch probe still
        governs continuation; a conditional that closes before the cut
        is balanced text the repair never touches. The exactness rule
        stands: any nonempty prefix is the literal opening of the tail's
        own branch.

    Verbose mode (wh-review-pattern-fixes.29): under an active x flag re
    ignores the whitespace this function would extract as word
    separators, so no text lexed inside an active-verbose scope may ever
    reach the matchers. A global ``(?x)`` returns the conservative empty
    prefix outright. A scoped group that provably matches only the empty
    string -- ignored whitespace, comments, and nested empty x-enabling
    scopes, with no following quantifier (wh-review-pattern-fixes.44) --
    is DELETED from the cut text exactly like a ``(?#...)`` comment
    (``_delete_zero_width_verbose_scopes``); any other verbose scope in
    the cut returns '' (the unclosed-conditional precedent). The span
    scanner itself is verbose-aware, so a ``.+`` inside a verbose comment
    is not a tail and the old garbage prefix ``look #`` for
    ``^look(?x: # .+ comment\n) up now$`` cannot arise.

    Whitespace exactness (wh-review-pattern-fixes.30): exactly ONE
    trailing space -- the intentional separator between the literal
    opening and the tail -- is removed when present; any further
    whitespace is regex text the pattern requires, and leading whitespace
    is never touched. The old blanket ``.rstrip()`` erased a required
    second space (``^look up  (.+)$`` -> prefix ``look up``), so the
    matchers held buffers that no single-space token join could ever
    complete. A prefix that keeps such whitespace builds no matchers
    (``build_literal_prefix_matchers`` refuses it), which finalizes the
    buffer as dictation at once -- the correct outcome for a pattern no
    join can speak.

    Lives here (not in the router) so the prefix is computed ONCE at
    catalog load time and stored as ``literal_prefix`` in the pattern
    metadata (wh-greedy-prefix-precompute); the router's runtime probe
    reads the stored string instead of re-parsing the regex on the hot
    path.
    """
    if _global_verbose(pattern_str):
        return ""
    p = pattern_str
    if p.endswith("$"):
        p = p[:-1]
    if p.startswith("^"):
        p = p[1:]
    if p.startswith(r"\b"):
        p = p[2:]
    spans = _find_greedy_tail_spans(p)
    if not spans:
        return ""
    # A span is never inside a comment or a class, so the cut text holds
    # only COMPLETE comments and classes; deleting the comments here is
    # exact and keeps them out of the repair and the matchers
    # (wh-review-pattern-fixes.24).
    cut = _delete_zero_width_verbose_scopes(p[: spans[0][0]])
    if cut is None:
        # The cut runs through (or contains) a live verbose scope: no
        # textual extraction is safe (wh-review-pattern-fixes.29).
        return ""
    cut = _delete_comments(cut)
    if any(cut.startswith("(?(", pos)
           for pos in _unclosed_group_positions(cut)):
        # The tail sits inside a conditional group: no fabricated prefix
        # (see the docstring; wh-review-pattern-fixes.24).
        return ""
    repaired = _repair_unclosed_groups(cut)
    if repaired.endswith(" "):
        # Exactly one separator space (wh-review-pattern-fixes.30).
        repaired = repaired[:-1]
    return repaired


def extract_full_literal_body(pattern_str: str) -> str:
    r"""Strip anchors and return the whole body of a NON-greedy pattern.

    Companion to ``extract_literal_prefix`` for the pattern shape that
    function deliberately skips (wh-review-pattern-fixes.12): an anchored
    pattern with no greedy tail, whose "literal prefix" is its WHOLE body.
    Example: ``^push to talk mode$`` -> ``push to talk mode``. The result
    feeds ``build_literal_prefix_matchers`` unchanged, so group expansion
    and truncation matchers apply the same way they do for a greedy
    pattern's pre-tail prefix.

    Returns an empty string when the pattern:
      - HAS a real greedy-tail span (``_find_greedy_tail_spans``;
        ``extract_literal_prefix`` owns those, and returning '' here keeps
        the two extractors disjoint, so a pattern can never get both a
        greedy prefix and a full body). An escaped ``\\.+``, a class
        ``[.+]``, or a quantified dot inside a zero-width or comment
        group is NOT a span, so ``^set \\.+ mode$``
        (wh-review-pattern-fixes.18) and ``^set (?=.+)mode$``
        (wh-review-pattern-fixes.22) now yield their bodies and get
        literal_body_matchers -- and stay off the router's greedy
        timer, or
      - is bounded at neither end: the extractable shapes are ``^...$``
        and ``\b...\b``, and a pattern bounded at one end only
        (``\bopen single quote``) is neither. The truncation matchers are
        fullmatch-shaped, which stays correct for the ``\b...\b`` shape
        even though it matches mid-utterance: ``can_continue`` reaches a
        pattern only through the first-word index, so the buffer it tests
        always starts at the name's own first word
        (wh-spaced-punctuation-names-unresolved). Before that bead the
        ``\b`` shape was refused, and since every punctuation NAME in
        patterns.toml is written that way, no name had
        ``literal_body_matchers`` and a buffer holding two words of a
        three-word name could not keep listening for the third, or
      - strips to nothing.

    Comment groups ``(?#...)`` are zero-width, so they are DELETED from
    the returned body (``_delete_comments``, wh-review-pattern-fixes.24):
    ``^set (?# note )mode$`` yields ``set mode``, the text the user must
    actually speak. The deletion is exact, and it keeps comment text out
    of the whitespace word-splits ``build_literal_prefix_matchers`` turns
    into truncation matchers. A VERBOSE scope that provably matches only
    the empty string is deleted the same way
    (``_delete_zero_width_verbose_scopes``, wh-review-pattern-fixes.29):
    ``^look(?x: # .+ comment\n) up now$`` yields ``look up now``, which
    re fullmatches -- the old walk saw the commented ``.+`` as a span and
    returned '', so the pause after "look up" finalized as dictation. The
    proof reaches nested empty scopes too, so
    ``^look(?x:(?x: # c\n)) up now$`` yields the same body
    (wh-review-pattern-fixes.44). A verbose scope that is NOT provably
    zero-width returns the conservative '' instead: its literal text
    relies on whitespace re ignores.

    The body is returned UNSTRIPPED (wh-review-pattern-fixes.30): the
    body is the WHOLE match text, so leading or trailing whitespace in it
    is required by re, and no single-space token join can supply it. The
    old ``.strip()`` turned ``^look up now $`` into the matchable body
    ``look up now``, holding a buffer re refuses;
    ``build_literal_prefix_matchers`` now refuses such a body, so the
    buffer finalizes at once.

    Deliberately does NOT set or read any greedy metadata: a non-greedy
    pattern must never become eligible for the router's greedy timer, so
    the result is stored under its own catalog key
    (``literal_body_matchers``), never under ``literal_prefix``.
    """
    p = pattern_str
    if _find_greedy_tail_spans(p):
        return ""
    anchored = p.startswith("^") and p.endswith("$")
    # A name written between word boundaries is the same "whole body"
    # shape (wh-spaced-punctuation-names-unresolved). No length guard is
    # needed for the degenerate r"\b" and r"\b\b": the strip below takes
    # two characters off each end, so both yield the empty string, which
    # the caller already treats as "no body". Measured, not assumed --
    # an earlier len(p) > 4 here rejected exactly those two inputs and
    # changed no answer, so it was defensive code claiming an effect it
    # did not have.
    bounded = p.startswith(r"\b") and p.endswith(r"\b")
    if not anchored and not bounded:
        return ""
    if _global_verbose(pattern_str):
        # Unreachable while global flags must precede the '^', but kept
        # as an explicit guard: a fully verbose body is never extractable.
        return ""
    if anchored:
        p = p[1:-1]
        if p.startswith(r"\b"):
            p = p[2:]
    else:
        p = p[2:-2]
    stripped_scopes = _delete_zero_width_verbose_scopes(p)
    if stripped_scopes is None:
        return ""
    return _delete_comments(stripped_scopes)


#: Highest number of matchers ``build_literal_prefix_matchers`` will compile
#: per expansion variant of one prefix: the full-variant matcher plus at most
#: ``MAX_PREFIX_MATCHERS - 1`` word truncations. The count must not depend on
#: user input (wh-lru-cache-hot-paths.1.7): a prefix of N words produced N
#: matchers whose source text grows quadratically, and a 1000-word prefix from
#: the advanced Pattern Manager cost 3.310 s and 62.3 MB at catalog load. With
#: this cap the same prefix costs 0.0072 s and 129 KB. 32 is far above any
#: spoken trigger phrase -- the longest prefix in the shipped catalog is two
#: words -- so no real pattern reaches it. The total across variants is
#: bounded by ``MAX_PREFIX_EXPANSIONS * MAX_PREFIX_MATCHERS`` before dedup,
#: still a constant that no user input can grow.
MAX_PREFIX_MATCHERS = 32

#: Highest number of concrete word-sequence variants the group expander in
#: ``build_literal_prefix_matchers`` will produce for one prefix
#: (wh-review-pattern-fixes.9). Past this cap the expansion is abandoned and
#: the prefix keeps the prior raw-text behavior, so the matcher count stays
#: independent of user input. 32 is far above any spoken trigger phrase: the
#: repro pattern ``look (?:the )?widget`` needs 2 variants, and even three
#: chained three-branch alternations need 27.
MAX_PREFIX_EXPANSIONS = 32


def _find_group_end(text: str, start: int) -> Optional[int]:
    """Index of the ``)`` that closes the group opening at ``start``.

    Skips escape pairs; refuses (returns None) a character class, whose
    brackets could hide an unbalanced paren from this scan.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            return None
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level_alternation(text: str) -> Optional[List[str]]:
    """Split ``text`` on ``|`` at paren depth 0, respecting escapes.

    Returns None for a character class or unbalanced parens -- shapes the
    expander refuses.
    """
    branches: List[str] = []
    current: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            if i + 1 >= n:
                return None
            current.append(text[i:i + 2])
            i += 2
            continue
        if char == "[":
            return None
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
        if char == "|" and depth == 0:
            branches.append("".join(current))
            current = []
        else:
            current.append(char)
        i += 1
    if depth != 0:
        return None
    branches.append("".join(current))
    return branches


def _expand_alternation(text: str) -> Optional[List[str]]:
    """Expand each top-level alternation branch of a group's interior.

    Duplicate branch variants are dropped in first-seen order BEFORE the
    count is compared against ``MAX_PREFIX_EXPANSIONS``
    (wh-review-pattern-fixes.13): the cap bounds DISTINCT variants, not
    syntactic paths, so duplicated branches (``(?:a |a )``) cannot abort an
    expansion whose real variant count is small. The cap itself stays a
    constant independent of user input; the seen-set holds at most
    ``MAX_PREFIX_EXPANSIONS + 1`` strings.
    """
    branches = _split_top_level_alternation(text)
    if branches is None:
        return None
    variants: List[str] = []
    seen = set()
    for branch in branches:
        expanded = _expand_sequence(branch)
        if expanded is None:
            return None
        for variant in expanded:
            if variant in seen:
                continue
            seen.add(variant)
            variants.append(variant)
            if len(variants) > MAX_PREFIX_EXPANSIONS:
                return None
    return variants


def _expand_sequence(text: str) -> Optional[List[str]]:
    """Expand one alternation-free sequence into its concrete variants.

    Returns the list of variant strings, or None when ``text`` holds syntax
    the expander cannot expand EXACTLY: any group that is not ``(?:...)``
    (capturing, named, lookaround, inline flags), a group quantified with
    ``+`` ``*`` ``{`` or a lazy/possessive ``?``, a character class, a stray
    ``)`` or top-level ``|``, or a variant count past
    ``MAX_PREFIX_EXPANSIONS``. Escape pairs and quantifiers on single
    characters pass through verbatim -- the variants stay raw regex text.
    """
    variants = [""]
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            if i + 1 >= n:
                return None
            piece = text[i:i + 2]
            variants = [v + piece for v in variants]
            i += 2
        elif char == "(":
            if not text.startswith("(?:", i):
                return None
            end = _find_group_end(text, i)
            if end is None:
                return None
            inner = text[i + 3:end]
            i = end + 1
            optional = False
            if i < n and text[i] == "?":
                optional = True
                i += 1
                if i < n and text[i] in "?+":
                    return None  # lazy / possessive optional
            elif i < n and text[i] in "+*{":
                return None  # unbounded or counted repetition
            branch_variants = _expand_alternation(inner)
            if branch_variants is None:
                return None
            if optional:
                branch_variants = branch_variants + [""]
            # Dedupe the product in first-seen order BEFORE the cap check
            # (wh-review-pattern-fixes.13): an optional group whose
            # alternation already holds an empty branch (``(?:a |)?``)
            # appends a second empty string, and duplicated branches
            # multiply the SYNTACTIC path count past the cap while the
            # distinct variant count stays small. The cap bounds distinct
            # variants and stays independent of user input.
            combined: List[str] = []
            seen = set()
            for variant in variants:
                for branch in branch_variants:
                    candidate = variant + branch
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    combined.append(candidate)
                    if len(combined) > MAX_PREFIX_EXPANSIONS:
                        return None
            variants = combined
        elif char in ")|[":
            return None
        else:
            variants = [v + char for v in variants]
            i += 1
    return variants


def _expand_prefix_variants(normalized: str) -> Optional[List[str]]:
    """Expand group syntax in a normalized prefix into concrete variants.

    Entry point for ``build_literal_prefix_matchers``. Returns None when the
    prefix has no group syntax or cannot be expanded exactly -- the caller
    then keeps the prior raw-text behavior.
    """
    if "(" not in normalized:
        return None
    return _expand_sequence(normalized)


#: Quantifier spellings after a ``\s`` escape. Everything is optional, so
#: a match always succeeds (bare ``\s`` matches empty here); ``mod`` is a
#: lazy ``?`` or possessive ``+`` suffix, which never changes the set of
#: strings a fullmatch probe accepts. An invalid brace form (``{a}``,
#: ``{2``) does not match and stays literal text, exactly as re lexes it.
_SPACE_QUANTIFIER_RE = re.compile(
    r"(?:(?P<sym>[+*?])|\{(?P<lo>\d+)(?:(?P<comma>,)(?P<hi>\d*))?\})?"
    r"(?P<mod>[+?])?"
)


def _space_separator_replacement(match: "re.Match[str]") -> str:
    r"""Replacement text for one ``\s``-plus-quantifier occurrence.

    ``match`` is a ``_SPACE_QUANTIFIER_RE`` match over the quantifier that
    follows the escape. The replacement encodes what the spelling can
    match in a single-space token join:

      - ``' '``  -- the spelling accepts exactly one space (min <= 1 <=
        max): a word separator.
      - ``''``   -- the spelling matches only the empty string
        (``\s{0}``): zero-width, the neighboring words join directly.
      - ``'  '`` -- the spelling requires two or more whitespace
        characters (``\s{2}``, ``\s{2,}``): no token join can supply
        them, and the doubled real space makes the exactness rule refuse
        the variant (wh-review-pattern-fixes.30).
    """
    sym = match.group("sym")
    if sym == "+":
        lo, hi = 1, None
    elif sym == "*":
        lo, hi = 0, None
    elif sym == "?":
        lo, hi = 0, 1
    elif match.group("lo") is not None:
        lo = int(match.group("lo"))
        if match.group("comma") is None:
            hi: Optional[int] = lo
        elif match.group("hi"):
            hi = int(match.group("hi"))
        else:
            hi = None
    else:
        lo, hi = 1, 1
    if hi is not None and hi < lo:
        # re refuses 'min repeat greater than max repeat'; refuse too.
        return "  "
    if hi == 0:
        return ""
    if lo > 1:
        return "  "
    return " "


def _normalize_space_separators(text: str) -> Tuple[str, bool]:
    r"""Rewrite every ``\s`` separator spelling into its join reading.

    Returns ``(normalized, trailing_separator)``. Each ``\s`` escape and
    its quantifier becomes the text ``_space_separator_replacement``
    decides; ``trailing_separator`` is True when the LAST character of
    ``normalized`` is a single separator space produced from an escape at
    the very end of ``text`` -- the caller drops that one space, because
    a trailing escape is the separator before the tail, not literal text.

    The walk uses the scanner's lexical rules, which a blind textual
    substitution cannot honor (wh-review-pattern-fixes.34):

      - A character class is copied whole (``_scan_class``): the ``\s``
        in the shipped ``[\s-]+`` is class content, not a separator.
      - An escape pair is consumed as one token, so the ``\s`` of a
        literal ``\\s`` (escaped backslash, then the letter s) is never
        rewritten.
    """
    out: List[str] = []
    trailing = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            end = _scan_class(text, i)
            out.append(text[i:end])
            i = end
            trailing = False
            continue
        if ch == "\\":
            if text.startswith(r"\s", i):
                quant = _SPACE_QUANTIFIER_RE.match(text, i + 2)
                assert quant is not None  # everything in it is optional
                replacement = _space_separator_replacement(quant)
                out.append(replacement)
                trailing = replacement == " " and quant.end() == n
                i = quant.end()
                continue
            out.append(text[i:i + 2])
            i += 2
            trailing = False
            continue
        out.append(ch)
        i += 1
        trailing = False
    return "".join(out), trailing


def build_literal_prefix_matchers(
    literal_prefix: str,
) -> Tuple["re.Pattern[str]", ...]:
    r"""Compile every regex the literal-opening probes test against.

    Two callers feed this function: the router's greedy-prefix probe passes
    ``extract_literal_prefix`` output (the text before a greedy tail), and
    the matcher's non-greedy continuation probe passes
    ``extract_full_literal_body`` output (a whole anchored body,
    wh-review-pattern-fixes.12). The compile behavior is identical for both.

    Returns, per prefix variant, the full-variant matcher first and then one
    matcher per N-word truncation, in the order the probe must try them,
    deduplicated across variants. An empty tuple means no buffer can match:
    either the prefix normalized to nothing, or no variant compiles.

    A prefix that contains group syntax is first EXPANDED into its concrete
    word-sequence variants (wh-review-pattern-fixes.9), because a whitespace
    truncation of the raw text cannot represent a buffer that ends inside a
    group: ``look (?:the )?widget`` raw yields only ``^look (?:the )?widget$``
    and ``^look$``, so the reachable buffer ``look the`` finalized as
    dictation. What expands: a non-capturing optional group ``(?:X )?`` (two
    variants, with and without X), a non-capturing alternation ``(?:A|B)``
    (one variant per branch; branches may hold several words), and their
    nestings, up to ``MAX_PREFIX_EXPANSIONS`` total variants. What does NOT
    expand -- unbounded or counted repetition (``+`` ``*`` ``{m,n}`` on a
    group), capturing or named groups, lookarounds, inline flags, character
    classes, lazy/possessive quantifiers, top-level alternation, or anything
    past the cap: the whole prefix then keeps the prior behavior, whole-prefix
    matcher plus whitespace truncations of the raw text. The expander never
    expands approximately -- an inexact expansion could hold ordinary
    dictation back, which is worse than a missed truncation.

    Per variant at most ``MAX_PREFIX_MATCHERS`` matchers are built. A variant
    longer than that keeps its full matcher and its first truncations, and
    loses only the deepest partial-buffer probes, so a user who speaks such a
    trigger in full still matches. See the constants for the measurements and
    the total bound.

    ``literal_prefix`` may carry ``\s`` whitespace escapes as word
    separators, because ``extract_literal_prefix`` keeps a trailing ``\s+``
    (the shipped click command's prefix is ``click\s+``) and a source pattern
    may join its literal words with any ``\s`` spelling instead of a real
    space. Every spelling that can match exactly ONE space -- the join the
    probes test -- is a separator (wh-review-pattern-fixes.34): bare
    ``\s``, ``\s?``, ``\s+``, ``\s*``, ``\s{1}``, ``\s{0,}`` / ``\s{1,}``,
    and any bounded ``\s{m,n}`` with m <= 1 <= n, each with an optional
    lazy ``?`` or possessive ``+`` suffix. A TRAILING separator escape is
    the separator before the tail and is dropped whole; interior
    separators become single spaces (wh-click-number-dictation). The
    normalization runs BEFORE group expansion and the word split, so
    truncations exist for every separator spelling; only spellings that
    were normalized until round 10 built them. ``\s{0}`` matches only the
    empty string, so its neighboring words join directly. A spelling that
    REQUIRES two or more whitespace characters (``\s{2}``, ``\s{2,}``)
    becomes a doubled real space, which the exactness rule below refuses
    -- until this change such a spelling stayed inside one split word and
    compiled never-matching probes instead. A ``\s`` inside a character
    class, or preceded by an escaped backslash, is not rewritten by
    ``_normalize_space_separators``. Class separators that accept one
    join space instead supply truncation boundaries in the compile step,
    keeping their original regex (and accepted hyphens) in longer probes.

    Whitespace is otherwise EXACT (wh-review-pattern-fixes.30). The probes
    test single-space token joins, so text that keeps leading, trailing, or
    doubled whitespace can never match one; the old blanket strips rewrote
    that whitespace away and the matchers held buffers whose pattern no
    join can ever complete. The rules now:

      - An EXPANSION variant that ends with a space loses exactly that one
        space: group expansion realizes the branch text up to the tail
        boundary, so the separator that sat inside the group surfaces at
        the variant's end (``look (?:the )?`` -> ``look the `` / ``look ``).
        Same single-separator rule ``extract_literal_prefix`` applies to
        the cut text.
      - Any variant whose remaining whitespace differs from single interior
        spaces is REFUSED -- no matchers at all, chosen over compiling
        never-matching regexes because an absent matcher answers the same
        and costs nothing per probe. Raw (unexpanded) text keeps its
        trailing space if it has one, so an impossible body like
        ``look up now `` is refused rather than silently rewritten.

    Call this once per pattern at catalog load time and store the result
    beside the ``literal_prefix`` string, which is what PatternCatalog does.
    Do NOT cache this function, and do not cache its caller on the router:
    the router's probe takes the user's buffer text as an argument, and a
    process-global cache keyed on speech grows without bound. The design
    history, the two rejected cache placements, and the measurements behind
    the cap are recorded on wh-lru-cache-hot-paths.
    """
    normalized, trailing_separator = _normalize_space_separators(
        literal_prefix)
    if trailing_separator:
        # The trailing escape is the separator between the literal opening
        # and the tail; one join space satisfies it, so 'click' is the
        # probe text for the prefix 'click\s+' (wh-click-number-dictation).
        normalized = normalized[:-1]
    if not normalized.strip():
        return ()
    expanded_variants = _expand_prefix_variants(normalized)
    expanded = expanded_variants is not None
    variants = expanded_variants if expanded else [normalized]
    matchers: List["re.Pattern[str]"] = []
    seen = set()
    for variant in variants:
        if expanded and variant.endswith(" "):
            # One separator space exposed by group expansion
            # (wh-review-pattern-fixes.30; see the docstring).
            variant = variant[:-1]
        if not variant:
            continue
        if variant != " ".join(variant.split()):
            # Leading, trailing, or doubled whitespace survives: re
            # requires whitespace the single-space token join can never
            # supply, so no buffer may be held for this variant
            # (wh-review-pattern-fixes.30).
            continue
        for matcher in _compile_prefix_and_truncations(variant):
            if matcher.pattern not in seen:
                seen.add(matcher.pattern)
                matchers.append(matcher)
    return tuple(matchers)


def _prefix_separator_end(text: str, start: int) -> Optional[int]:
    r"""End of one separator atom, preserving its class and quantifier.

    A separator atom is a literal space, an escaped space (``\ ``, the
    spelling re.escape gives the space inside a simple-mode phrase,
    wh-multiword-phrase-replacement), or a character class that accepts a
    space. The escaped space and the class each keep their quantifier, so
    ``\ {2}`` and ``\ {0}`` reach the run check below and are refused the
    same way as ``[ ]{2}``. The whole class must accept a space; its
    repetition is checked together with adjacent separator atoms before
    adding a truncation. This avoids turning two required separators into a
    single-space word join.

    crewcut: the other escape spellings of a space outside a class
    (``\x20``, ``\040``, ``\u0020``, ``\N{SPACE}``) are not read as
    separators. No WheelHouse code emits them (grep 2026-09-13 under
    services/wheelhouse outside tests found them only in docstrings of this
    module); a hand-written Advanced-mode pattern that uses one keeps its
    full matcher and loses only its truncations. To remove the limit, read
    those spellings here the way the escaped space is read.
    """
    if text[start] == " ":
        return start + 1
    if text.startswith("\\ ", start):
        quant = _SPACE_QUANTIFIER_RE.match(text, start + 2)
        assert quant is not None
        return quant.end()
    if text[start] != "[":
        return None
    end = _scan_class(text, start)
    if re.fullmatch(text[start:end], " ") is None:
        return None
    quant = _SPACE_QUANTIFIER_RE.match(text, end)
    assert quant is not None
    return quant.end()


def _prefix_truncation_offsets(text: str) -> List[int]:
    """Find bounded word ends without splitting class contents or escapes."""
    offsets: List[int] = []
    i = 0
    while i < len(text) and len(offsets) < MAX_PREFIX_MATCHERS - 1:
        end = _prefix_separator_end(text, i)
        if end is not None:
            start = i
            while end < len(text):
                following = _prefix_separator_end(text, end)
                if following is None:
                    break
                end = following
            if start and re.fullmatch(text[start:end], " ") is not None:
                offsets.append(start)
            i = end
        elif text[i] == "[":
            i = _scan_class(text, i)
        elif text[i] == "\\":
            i += 2
        else:
            i += 1
    return offsets


def _compile_prefix_and_truncations(
    normalized: str,
) -> List["re.Pattern[str]"]:
    """Compile one prefix variant: full matcher, then N-word truncations.

    This is the single compile path for plain prefixes, for expansion
    variants, and for the raw-text fallback, so plain-word behavior is
    unchanged by the expansion. An empty list means the variant does not
    compile; a truncation that does not compile (a whitespace split inside
    raw group syntax) is skipped.
    """
    try:
        matchers = [re.compile("^" + normalized + "$", re.IGNORECASE)]
    except re.error:
        return []
    for end in _prefix_truncation_offsets(normalized):
        partial = normalized[:end]
        try:
            matchers.append(re.compile("^" + partial + "$", re.IGNORECASE))
        except re.error:
            continue
    return matchers


def _transform_numeric_captures(
    pattern_str: str,
) -> Tuple[str, Optional[int]]:
    r"""Widen numeric CAPTURES to a number phrase, return the first's number.

    Returns ``(transformed, first_group_num)`` where ``first_group_num``
    is the REAL Python capture number of the first widened group, or None
    when nothing was widened. The walk uses the same lexical rules as the
    greedy-tail scanner (wh-review-pattern-fixes.24/.27): escape pairs
    are consumed whole, a character class is skipped with ``_scan_class``,
    and a comment group is skipped with ``_scan_comment``, so their
    interiors are never rewritten and never counted.

    What is widened: a capturing group whose ENTIRE content is ``\d+`` --
    a plain ``(\d+)`` or a named ``(?P<name>\d+)``. Any quantifier after
    the group's ``)`` is ordinary copied text, so ``(\d+)?`` keeps its
    ``?``.

    WHICH body is substituted depends on the pattern's shape, so the walk
    emits a placeholder and joins the parts once it has seen the whole
    pattern -- an enclosing group's quantifier sits after its ``)`` and is
    therefore still to the right when a capture is reached. See
    ``_pick_number_capture_body`` for the rule and its measurements
    (wh-number-words-one-parser.1.6).

    Group numbering follows re.compile exactly: only a plain ``(`` and a
    named ``(?P<name>`` open a capture, counted left to right by opening
    position. Zero-width groups (``(?=`` ``(?!`` ``(?<=`` ``(?<!``),
    non-capturing ``(?:``, atomic ``(?>``, flag groups, conditionals
    ``(?(id)``, and comments do not count. The old textual pass counted
    every ``(`` not followed by ``?:``, so a lookahead before the capture
    shifted the recorded number past the real group and validate_numeric
    silently skipped the check (wh-review-pattern-fixes.27).

    Deliberately NOT widened, with the reasons:

      - ``(?:\d+)``: a non-capturing group has no capture number, so a
        widened form could never be validated -- it would accept
        arbitrary words, the exact defect this transform exists to
        prevent. It stays as written; a spoken number word will not match
        it, and an author who wants word-number support must use a
        capturing form.
      - A bare ``\d+`` outside any group, and ``\d+`` mixed with other
        content inside a group: same bound, no capture to validate
        (bare), or a content shape the validator cannot check exactly
        (mixed). Both stay as written, which the old pass also left
        untouched.

    Verbose mode (wh-review-pattern-fixes.29): the walk tracks the x
    flag (global ``(?x)``, scoped flag groups) and copies an
    active-verbose ``#`` comment verbatim WITHOUT counting or rewriting
    anything inside it -- the parenthesis in
    ``^foo(?x: # (not a capture)\n) (\d+)$`` is comment text, so the
    widened capture is re.compile's group 1, not the old bogus g2 that
    made validate_numeric skip the check and accept any word.

    Wrong metadata is worse than none, so a widening is gated on
    re.compile agreement: when a group was widened, the walk's total
    capture count must equal ``re.compile(pattern_str).groups``. On any
    disagreement -- including a pattern re refuses to compile, such as
    the finding's raw-string form whose verbose comment never ends --
    the transform is skipped entirely: the pattern is returned unchanged
    with no validation group.
    """
    out: List[Any] = []
    i = 0
    n = len(pattern_str)
    capture_count = 0
    first_group_num: Optional[int] = None
    verbose_stack: List[bool] = []
    base_verbose = _global_verbose(pattern_str)
    # Group-nesting bookkeeping for the body choice. open_stack is pushed
    # and popped in lockstep with verbose_stack, so a widened capture can
    # snapshot exactly which groups enclose it; group_close then records
    # where each of those groups ended, because the quantifier that
    # decides the question sits after the ')'.
    open_stack: List[int] = []
    group_close: Dict[int, int] = {}
    group_content_start: Dict[int, int] = {}
    group_verbose: Dict[int, bool] = {}
    # Where each group's own top-level '|' characters sit, so a capture
    # in one branch is not judged by another branch's text
    # (wh-number-words-one-parser.1.13).
    group_alternations: Dict[int, List[int]] = {}
    # Each capturing group by its re.compile number, so a
    # backreference in a slice can be resolved against the body it
    # names (wh-number-words-one-parser.1.17).
    capture_opens: Dict[int, int] = {}
    capture_names: Dict[int, str] = {}
    group_inner_verbose: Dict[int, bool] = {}
    enclosures: List[List[int]] = []
    capture_spans: List[Tuple[int, int]] = []
    # The whole chain of x modes around each capture, outermost
    # first, in lockstep with capture_spans. Both separator slices
    # below start at a capture's end, so the LAST entry is the mode
    # each of them is read in (wh-number-words-one-parser.1.14), and
    # the entries before it are what a slice steps out into when it
    # cuts through a scoped flag group (.1.16).
    capture_levels: List[Tuple[bool, ...]] = []
    while i < n:
        verbose = verbose_stack[-1] if verbose_stack else base_verbose
        char = pattern_str[i]
        if char == "\\":
            out.append(pattern_str[i:i + 2])
            i += 2
            continue
        if char == "[":
            end = _scan_class(pattern_str, i)
            out.append(pattern_str[i:end])
            i = end
            continue
        if verbose and char == "#":
            end = _scan_verbose_comment(pattern_str, i)
            out.append(pattern_str[i:end])
            i = end
            continue
        if pattern_str.startswith("(?#", i):
            end = _scan_comment(pattern_str, i)
            out.append(pattern_str[i:end])
            i = end
            continue
        if pattern_str.startswith("(?(", i):
            # Conditional group: copy the "(?(id)" opening whole so the
            # id's parentheses are not miscounted as a capture. The
            # yes|no content that follows is scanned normally; push a
            # frame so the group's closing ')' pops it.
            close = pattern_str.find(")", i + 3)
            end = (close + 1) if close != -1 else n
            out.append(pattern_str[i:end])
            verbose_stack.append(verbose)
            open_stack.append(i)
            group_content_start[i] = end
            i = end
            continue
        if char == "(":
            open_len = _group_open_len(pattern_str, i)
            opening = pattern_str[i:i + open_len]
            is_capturing = opening == "(" or opening.startswith("(?P<")
            if is_capturing:
                capture_count += 1
            content_start = i + open_len
            if is_capturing:
                capture_opens[capture_count] = i
                if opening.startswith("(?P<"):
                    capture_names[capture_count] = opening[4:-1]
            if (is_capturing
                    and pattern_str.startswith(r"\d+", content_start)
                    and pattern_str.startswith(")", content_start + 3)):
                # This branch never pushes the group on open_stack,
                # so record its span here: the reference map needs
                # a body for every capturing group, this one too.
                group_content_start[i] = content_start
                group_close[i] = content_start + 3
                group_inner_verbose[i] = verbose
                out.append(opening)
                out.append(_WIDENED_BODY)
                out.append(")")
                capture_spans.append((i, content_start + 4))
                capture_levels.append(
                    tuple([base_verbose] + verbose_stack)
                )
                i = content_start + 4
                enclosures.append(list(open_stack))
                # The capture's OWN quantifier is deliberately NOT read
                # here. '(\d+)+' looks like the same hazard as
                # '(?: (\d+))+' and is not: the body can never start with
                # the separator, so a repeat carrying no separator of its
                # own cannot take a second iteration in spoken text.
                # Measured with the plain body at 12, 16, 20 and 24 words:
                # 0.000s for '(BODY)+', '(BODY)*' and '(BODY){1,5}',
                # against 12.363s for '(?: (BODY))+'.
                # test_a_capture_that_repeats_itself_is_not_the_same_hazard
                # pins the property that makes this true.
                if first_group_num is None:
                    first_group_num = capture_count
                continue
            out.append(opening)
            verbose_stack.append(
                _scoped_verbose_change(pattern_str, i, open_len, verbose)
            )
            group_inner_verbose[i] = verbose_stack[-1]
            open_stack.append(i)
            group_content_start[i] = i + open_len
            i += open_len
            continue
        if char == ")":
            if verbose_stack:
                verbose_stack.pop()
            if open_stack:
                opened = open_stack.pop()
                group_close[opened] = i
                # The quantifier sits OUTSIDE this group, so it is read
                # in the enclosing scope's x mode, not this group's.
                group_verbose[opened] = (
                    verbose_stack[-1] if verbose_stack else base_verbose
                )
            out.append(char)
            i += 1
            continue
        if char == "|" and open_stack:
            # A bare '|' at this depth: every earlier branch of the
            # innermost open group ends here. Escapes, character classes
            # and both comment forms are consumed above, so a '|' that
            # reaches this line is real alternation syntax.
            group_alternations.setdefault(open_stack[-1], []).append(i)
            out.append(char)
            i += 1
            continue
        out.append(char)
        i += 1
    if first_group_num is not None:
        try:
            real_groups = re.compile(pattern_str).groups
        except re.error:
            real_groups = None
        if real_groups != capture_count:
            # The walk cannot prove the capture number: skip the numeric
            # transform entirely (no widening, no metadata).
            return pattern_str, None
    references = _reference_replacements(
        pattern_str, capture_opens, capture_names, group_content_start,
        group_close, group_inner_verbose, base_verbose,
    )
    dangerous = _dangerous_captures(
        pattern_str, capture_spans, capture_levels, enclosures, group_close,
        group_content_start, group_verbose, group_alternations, base_verbose,
        references,
    )
    hyphen_bounded = _hyphen_bounded_captures(
        pattern_str, capture_spans, capture_levels, enclosures, group_close,
        group_content_start, group_verbose, group_inner_verbose,
        base_verbose, references,
    )
    widened_seen = 0
    parts: List[str] = []
    for part in out:
        if part is _WIDENED_BODY:
            parts.append(
                _pick_number_capture_body(
                    widened_seen in dangerous,
                    widened_seen in hyphen_bounded,
                )
            )
            widened_seen += 1
        else:
            parts.append(part)
    return "".join(parts), first_group_num


def transform_pattern(pattern_str: str) -> Tuple[str, Dict[str, Any]]:
    r"""Auto-detect special pattern types and transform if needed.

    Detects and handles:
    1. Greedy patterns (.+ or .*) → Mark as is_greedy
    2. Numeric captures (\d+) → Widen to NUMBER_CAPTURE_BODY, add validation

    Args:
        pattern_str: Original regex pattern string

    Returns:
        Tuple of (transformed_pattern, metadata_dict)
        where metadata_dict may contain:
        - is_greedy: bool (if greedy pattern detected)
        - validation_group: str (if numeric pattern transformed, e.g., 'g1', 'g2')

    Examples. The transformed pattern is described rather than quoted,
    because the widened body is NUMBER_CAPTURE_BODY and is too long to
    read inline:

        transform_pattern(r'(backspace|back space)\s*(\d+)?$') widens the
        second group and returns metadata {'validation_group': 'g2'}.

        transform_pattern(r'(?:prefix )?(\d+) times') widens the only
        capture and returns {'validation_group': 'g1'}; the non-capturing
        group ahead of it is not counted.

        transform_pattern(r'select (.+)') returns the pattern unchanged
        with {'is_greedy': True, 'literal_prefix': 'select'}.

    Notes:
        Shared utility for PatternCatalog and TextParser to ensure consistent
        pattern transformation across the speech recognition system.

        The numeric transform is lexer-based (wh-review-pattern-fixes.27):
        only a real capturing group -- plain ``(\d+)`` or named
        ``(?P<name>\d+)`` -- is widened, ``validation_group`` records the
        capture number re.compile assigns (lookarounds, non-capturing,
        flag, and conditional groups do not count), and character-class /
        comment interiors are never rewritten. A non-capturing ``(?:\d+)``
        and a bare ``\d+`` stay as written: with no capture to validate,
        widening them would let arbitrary words match. See
        ``_transform_numeric_captures`` for the full bounds.
    """
    metadata = {}

    # 1. Detect greedy patterns. Parsed, not substring-scanned: a plain
    # ".+ in pattern_str" test also fired on escaped dots (\.+), character
    # classes ([.+]), and tails inside unclosed group constructs, which
    # mis-routed bounded patterns onto the greedy catalog branch
    # (wh-review-pattern-fixes.18).
    if has_greedy_tail(pattern_str):
        metadata['is_greedy'] = True

    # 2. Widen numeric captures so spoken number words can match, and
    # record the real capture number for validate_numeric.
    transformed, first_group_num = _transform_numeric_captures(pattern_str)
    if first_group_num is not None:
        metadata['validation_group'] = f"g{first_group_num}"

    # 3. Pre-compute the greedy literal prefix from the FINAL transformed
    # pattern -- the same string the catalog compiles -- so the stored
    # prefix always equals what a runtime extraction from the compiled
    # pattern would produce (wh-greedy-prefix-precompute).
    if metadata.get('is_greedy'):
        metadata['literal_prefix'] = extract_literal_prefix(transformed)

    return transformed, metadata
