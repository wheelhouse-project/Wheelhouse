"""Parse a spoken/typed count into Optional[int] (wh-n29v).

Self-contained, pure, stateless helper. It is the ONE word-to-integer
implementation in services/wheelhouse (wh-number-words-one-parser): the
numbered overlay's 'click N', speech/actions.py:words_to_int behind every
command count, and speech/navigation/parser.py's cursor counts all read
numbers through this function. Two tables used to sit beside it, both
stopping at ten, which is why "backspace fifteen" pressed backspace once
and typed the word. Each caller still applies its OWN cap after parsing:
50 for a key repeat, MAX_COUNT 50 for cursor navigation, 999 for a badge.

Two caller differences are keyword options rather than separate tables --
see parse_number_word for what each one admits and why:

  - ``aliases``: the STT homophones "to", "too" and "for".
  - ``zero``: the word "zero" as the value 0.

``NUMBER_PHRASE_PATTERN`` publishes this vocabulary as a regex body, so
the pattern loader can widen the count captures in patterns.toml without
a second copy of the word list. ``NUMBER_PHRASE_PATTERN_ATOMIC`` is the
same body with an atomic multi-word tail, for the pattern shapes that
would otherwise backtrack exponentially; see the comment above the two
definitions for which shapes those are and why the atomic form is not
the default.

Supported range: 1..999. The hundreds compounds ('one hundred twenty-three')
fall out of the same tens+units grammar, so the 1..999 stretch stayed simple;
'1000' / 'one thousand' and anything above 999 return None.

Public API: parse_number_word(text) -> int | None.

Recognized shapes (case-insensitive, surrounding whitespace stripped):
  - bare digit strings: '7', '23', '99', '250'
  - cardinal words 1..19: 'one', 'eleven', 'nineteen'
  - tens: 'twenty' .. 'ninety'
  - tens+units, hyphenated or space-separated: 'twenty-three', 'thirty seven'
  - hundreds: 'one hundred', 'one hundred twenty-three',
    'one hundred and twenty three', 'three hundred five'
  - colloquial hundreds pairing without 'hundred' (wh-click-number-dictation):
    a unit 1..9 followed by a 10..99 remainder -- 'one twelve' (112),
    'three twenty' (320), 'two ninety nine' (299)
  - digit-by-digit reading (wh-click-number-dictation): two or three single
    digit words read left to right, leading digit 1..9, with 'zero'/'oh'
    allowed after the first -- 'one seven' (17), 'one zero five' /
    'one oh five' (105)
  - any of the above after one leading 'number'/'numbers' filler token:
    'number three', 'numbers 75' (users say "click number three" at the
    badges; wh-click-number-dictation)

Returns None for: ordinals ('first', 'twenty-third', '1st'), zero, negatives,
out-of-range values (>999), empty/None/whitespace-only input, and any token or
combination it cannot confidently resolve. None lets the 'click N' routing fall
back to the by-name path on ambiguous input.
"""
import re
from functools import lru_cache
from typing import Optional

# Bare-digit detection is ASCII-only on purpose. str.isdigit() is True for many
# non-ASCII forms -- superscripts (U+00B2 squared), circled digits (U+2461),
# and fullwidth / Arabic-Indic decimals -- where int() either raises ValueError
# (superscripts, circled) or silently yields a value the user never typed
# (fullwidth "5" -> 5). An ASCII [0-9]+ gate makes int() provably safe (no
# ValueError path) and routes every non-ASCII digit token to the word path,
# where it is unrecognized and returns None per the never-raise contract.
_ASCII_DIGITS = re.compile(r"[0-9]+")

# Cardinal units and teens, 1..19. Deliberately excludes "zero" (out of range)
# and the STT homophones (to/too/for) the other helpers carry, because a
# numbered-overlay badge count is never spoken as a homophone.
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}

# Tens multiples, 20..90.
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Single digit words for digit-by-digit reading ('one seven' -> 17). 'zero'
# and its spoken form 'oh' are valid here (and ONLY here) because a middle or
# trailing digit can be zero ('one zero five' / 'one oh five' -> 105); the
# main word grammar above still excludes zero as a standalone value.
_DIGIT_WORDS = {
    "zero": 0, "oh": 0,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}

# The STT homophones the older count helpers carried. Kept as a single
# alias step (wh-number-words-one-parser): the speech engine returns "to"
# and "too" for the spoken "two", and "for" for "four", so a count spoken
# into a command lands as one of these words. It is a word-to-WORD map,
# not a second word-to-integer table -- every alias resolves through the
# tables above. Every caller that reads a spoken count now passes
# ``aliases`` True -- the command counts, the cursor navigation counts,
# and, since wh-overlay-count-homophones (David's decision 2026-09-03),
# the numbered overlay and the mouse grid. The switch stays, and the
# default stays False, for a caller that must read the literal word.
_ALIASES = {"to": "two", "too": "two", "for": "four"}

_MIN = 1
_MAX = 999
# Maximum number of significant digits a bare digit string may carry. Anything
# longer than the widest supported value is out of range, and bounding the
# length here keeps int() from ever hitting Python 3.12's integer-string
# conversion limit (which raises ValueError) before the range check runs.
_MAX_DIGITS = len(str(_MAX))

# The longest phrase this parser accepts is five tokens ("one hundred and
# twenty three"), so a phrase is one token plus at most four more. The
# leading filler token is NOT one of the five: parse_number_word drops it
# before it counts anything, so NUMBER_PHRASE_PATTERN carries it as its
# own optional prefix below. Counting the filler against this cap is what
# made the body refuse "number one hundred and twenty three", a phrase
# the parser reads as 123 (wh-number-words-one-parser.1.1).
_MAX_EXTRA_TOKENS = 4

# The filler token users put in front of a count at the badges ("click
# number three"). parse_number_word drops exactly ONE LEADING occurrence
# and the regex body admits exactly one leading occurrence, both from this
# tuple, so the two readings of a filler cannot drift apart.
_FILLER_WORDS = ("number", "numbers")

# Every word any branch above can consume, longest first so the
# alternation reaches "nineteen" before "nine" without backtracking. The
# filler words are deliberately absent: the parser drops one LEADING
# filler and nothing else, so a filler in any other position is not part
# of a phrase and must not match here.
_PHRASE_WORDS = sorted(
    set(_UNITS) | set(_TENS) | set(_DIGIT_WORDS) | set(_ALIASES)
    | {"hundred", "and"},
    key=lambda word: (-len(word), word),
)

_WORD_ALTERNATION = "|".join(re.escape(word) for word in _PHRASE_WORDS)

_FILLER_ALTERNATION = "|".join(
    re.escape(word)
    for word in sorted(_FILLER_WORDS, key=lambda word: (-len(word), word))
)

# A regex body that admits everything parse_number_word can read: one
# optional leading filler token, then a digit run or a number phrase of up
# to five words, each part joined by a space or a hyphen. The parser turns
# every hyphen into a space before it splits, so "number-three" really is
# two tokens and the separator class has to accept both. The filler can
# precede EITHER shape -- "numbers 75" is in the docstring above -- which
# is why the prefix sits outside the digit-or-words alternation rather
# than inside the word list (wh-number-words-one-parser.1.1).
#
# The body is self-contained: its own alternations are bracketed, so a
# caller that interpolates it without adding parentheses still gets the
# whole phrase rather than the first branch of a bare alternation.
#
# It exists so the pattern loader can widen the count captures in
# patterns.toml from digits to spoken words WITHOUT a second copy of the
# vocabulary (wh-number-words-one-parser): the words come from the tables
# above, and this body only decides which text is worth handing to the
# parser. The parser stays the only thing that decides what a phrase is
# WORTH -- "one and and two" matches this body and still parses to None,
# and the caller's numeric validation then refuses the match.
#
# Every group inside is non-capturing on purpose. The loader records the
# capture number re.compile assigns to the widened group, so a capturing
# group in this body would shift every later number and send the numeric
# validation to the wrong group.
# Two forms of the same body are published, and the pattern loader picks
# between them per pattern. They differ in one character: whether the
# multi-word tail is an ordinary group or an atomic one.
#
# Why an atomic form is needed at all (wh-number-words-one-parser.1.3).
# The tail joins its words with a space. A caller may put this whole body
# inside a repeat of its own that ALSO joins with a space -- a user can
# write exactly that in the Advanced pattern editor, for example
# `^one(?: (\d+))+$`. A run of spoken words can then be divided between
# the two repeats in exponentially many ways, and an input that cannot
# match makes Python try every one of them: measured at 0.002s for twelve
# words and 12.253s for twenty-four, against 0.000s for the single-token
# capture this body replaced. The runtime matcher calls fullmatch/search
# with no timeout (speech/pattern_matcher.py), so that is a hang, not a
# slow answer. An atomic group takes its tokens and never gives one back,
# which removes the division and with it the runaway. The save-time probe
# in pattern_manager.py cannot cover the case: every probe is a fixed
# string, and any literal prefix the user writes keeps a fixed probe from
# ever reaching this word branch.
#
# Why the atomic form is NOT the default (wh-number-words-one-parser.1.6).
# A tail that never gives a word back cannot leave one for a literal that
# follows the capture, and seven words in _PHRASE_WORDS are ordinary
# English: the homophones "to", "too" and "for", plus "and", "oh", "one"
# and "hundred". With the atomic body everywhere, `^set volume (\d+) to
# mute$` stopped matching "set volume fifteen to mute" while its digit
# form kept working. Shipped patterns were unaffected -- all 319 entries
# were parsed, 113 carry a numeric capture, and none of those 113 has a
# body word as the first literal word after the capture -- but a user's
# own Advanced pattern is saved verbatim and widened the same way.
#
# So pattern_transform chooses: the atomic body only for the shapes that
# can actually run away, and the plain body for everything else. What
# makes a shape dangerous, and the measurements behind the rule, are
# recorded at _pick_number_capture_body in speech/pattern_transform.py.
_PHRASE_TAIL = (
    rf"(?:[\s-](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"
)

# The same tail with the hyphen dropped (wh-number-words-one-parser.1.34).
# The hyphen in the tail above joins two words of ONE spoken count, and
# the raw expression may also want that same character as its own
# separator right after the capture. Both readings then match and the
# greedy one wins, so the widened form completed a different number of
# iterations than the raw expression: measured before this tail existed,
# `^(?:(\d+)-)+$` read "two-three-" with the capture at "two-three",
# which the parser turns into 23, while the raw expression reads "2-3-"
# with the capture at "3". `^(\d+)-$` showed the same class with no
# repeat and no second capture at all: it accepted "two-three-", where
# the raw expression refuses "2-3-".
#
# Dropping the hyphen at that boundary hands the character back to the
# raw expression, which is the reading the raw expression itself takes.
# It costs the hyphenated spoken form of a multi-word count in exactly
# those patterns -- "go twenty-three" still reads as 23 wherever no
# hyphen follows the capture, because pattern_transform picks this tail
# per capture rather than for every pattern.
_PHRASE_TAIL_NO_HYPHEN = (
    rf"(?:[\s](?:{_WORD_ALTERNATION})){{0,{_MAX_EXTRA_TOKENS}}}"
)

# Where a spoken phrase has to END (wh-number-words-one-parser.1.32).
# Eleven count words are strict prefixes of other count words --
# "nineteen" is "nine" then "teen", "seventy" is "seven" then "ty",
# and the full list is eighteen, eighty, forty, fourteen, nineteen,
# ninety, seventeen, seventy, sixteen, sixty and too. The word
# alternation can give the longer word back, so a literal written
# right after the capture could take the rest of a word: measured
# before this assertion, `^go (\d+)teen$` accepted "go nineteen" with
# the capture reading "nine", which the parser turns into 9, while the
# raw expression refuses "go nineteen" outright. The widening is meant
# to ADD the spoken forms of what the raw expression already matched,
# never a form it rejects, and never with a different number.
#
# Only a following ASCII letter or digit is refused. A hyphen is
# deliberately allowed: the tail reads ``[\s-]`` itself, so a hyphen
# after a finished phrase is an ordinary separator rather than a split
# inside a word. Non-ASCII letters are not listed because no count
# word holds one, so none can be split at one -- the same ASCII-only
# reading of the grammar that .1.33 records.
#
# It sits on the spoken-word branch only. The digit branch keeps the
# behaviour of the expression it replaces, so `^go (\d+)x$` still
# matches "go 1x" and `^go (\d+)teen$` still matches "go 9teen".
_PHRASE_END = r"(?![A-Za-z0-9])"

NUMBER_PHRASE_PATTERN = (
    rf"(?:(?:{_FILLER_ALTERNATION})[\s-])?"
    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL}){_PHRASE_END})"
)

NUMBER_PHRASE_PATTERN_ATOMIC = (
    rf"(?:(?:{_FILLER_ALTERNATION})[\s-])?"
    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?>{_PHRASE_TAIL}){_PHRASE_END})"
)

# The same two bodies with the hyphen-free tail
# (wh-number-words-one-parser.1.34). pattern_transform picks one of the
# four per capture: the atomic pair where a word run can divide, and a
# no-hyphen form where the raw expression consumes a hyphen of its own
# after the capture. The filler prefix keeps its hyphen in every form,
# because it stands BEFORE the phrase and cannot compete with a
# separator that follows it.
NUMBER_PHRASE_PATTERN_NO_HYPHEN = (
    rf"(?:(?:{_FILLER_ALTERNATION})[\s-])?"
    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?:{_PHRASE_TAIL_NO_HYPHEN})"
    rf"{_PHRASE_END})"
)

NUMBER_PHRASE_PATTERN_ATOMIC_NO_HYPHEN = (
    rf"(?:(?:{_FILLER_ALTERNATION})[\s-])?"
    rf"(?:\d+|(?:{_WORD_ALTERNATION})(?>{_PHRASE_TAIL_NO_HYPHEN})"
    rf"{_PHRASE_END})"
)


def _in_range(value: int) -> Optional[int]:
    """Return value if it is within the supported range, else None."""
    if _MIN <= value <= _MAX:
        return value
    return None


def _parse_below_hundred(tokens: list[str]) -> Optional[int]:
    """Parse a 1..99 cardinal from one or two tokens (no hundreds component).

    One token: a unit/teen (1..19) or a tens multiple (20, 30, ...).
    Two tokens: a tens multiple followed by a unit 1..9 ('twenty three' -> 23).
    Returns None for any other shape (e.g. 'three twenty', 'twenty thirty').
    """
    if len(tokens) == 1:
        word = tokens[0]
        if word in _UNITS:
            return _UNITS[word]
        if word in _TENS:
            return _TENS[word]
        return None
    if len(tokens) == 2:
        tens_word, unit_word = tokens
        if tens_word in _TENS and unit_word in _UNITS and 1 <= _UNITS[unit_word] <= 9:
            return _TENS[tens_word] + _UNITS[unit_word]
        return None
    return None


def parse_number_word(
    text: Optional[str],
    *,
    aliases: bool = False,
    zero: bool = False,
) -> Optional[int]:
    """Parse a spoken/typed count into an int in 1..999, or None.

    See the module docstring for the full set of recognized shapes and the
    None-returning cases. Pure and stateless; no logging side effects.

    Args:
        text: the spoken or typed count.
        aliases: accept the STT homophones "to", "too" and "for" as their
            spoken numbers. The speech engine really does return those
            words for "two" and "four". Every caller that reads a spoken
            count sets this: the command counts, the cursor navigation
            counts, and -- since wh-overlay-count-homophones, David's
            decision 2026-09-03 -- the numbered overlay and the mouse
            grid. The overlay left it False until then, on the reasoning
            that a badge count is never spoken as a homophone; that was
            wrong in practice, because a spoken "four" returned as "for"
            reached the by-name search and clicked nothing. The accepted
            cost is David's: a bare "to", "too" or "for" spoken while
            badges or the grid show now clicks 2 or 4 instead of typing.
        zero: return 0 for the word "zero" instead of None. Zero is out of
            the badge range this parser was written for, but it is a real
            answer for a command count -- "scroll down zero" scrolls
            nothing -- so the command callers set this.
    """
    if text is None:
        return None

    # Lowercase, strip ends, normalize a single hyphen between tens and units
    # to a space, then collapse repeated internal whitespace.
    normalized = str(text).lower().strip()
    if not normalized:
        return None
    # A leading sign ('-1', '+5', '-twenty', '+one hundred') is unsupported:
    # the spec returns None for negative / signed input. Reject ANY leading
    # '+' or '-' before the hyphen-to-space step below, which would otherwise
    # strip a leading minus and parse '-twenty' as 'twenty' (20). Rejecting on
    # the sign character (not only signed digits) also keeps '+' and '-'
    # symmetric -- both return None. Internal hyphens ('twenty-three') are
    # untouched because only the first character is tested here.
    if normalized[0] in "+-":
        return None
    # A hyphen is valid ONLY as a single in-word separator joining two
    # characters ("twenty-three"). Reject a hyphen at the start or end of the
    # string, a doubled hyphen, or a hyphen adjacent to whitespace
    # ("twenty - three", "twenty--three", "one hundred - five", "twenty-").
    # Those are malformed, operator-like forms the parser cannot confidently
    # resolve, so they return None and the caller falls back to by-name. This
    # check runs before the hyphen-to-space step below, which would otherwise
    # silently accept them.
    if "-" in normalized and re.search(r"(?:^|[\s-])-|-(?:$|[\s-])", normalized):
        return None
    normalized = normalized.replace("-", " ")
    tokens = normalized.split()
    if not tokens:
        return None

    # wh-click-number-dictation: with badges on screen the natural phrasing is
    # "click number three", so the text arriving here is "number three". Drop
    # exactly one leading "number"/"numbers" filler token and parse the rest.
    # A bare "number"/"numbers" (or a doubled filler) leaves an unparseable
    # remainder and still returns None, keeping the by-name fallback.
    if tokens[0] in _FILLER_WORDS:
        tokens = tokens[1:]
        if not tokens:
            return None

    # The single alias step (wh-number-words-one-parser). It rewrites the
    # homophone to the word it stands for, so every branch below reads one
    # vocabulary and no branch needs a homophone of its own.
    if aliases:
        tokens = [_ALIASES.get(token, token) for token in tokens]

    # "zero" is the one value outside 1..999 a command count may mean.
    if zero and tokens == ["zero"]:
        return 0

    # Bare digit string (whole input is digits). ASCII-only [0-9]+ so int()
    # cannot raise on a non-ASCII digit; '1st' is not all-digits and falls
    # through to the word path (-> None). Strip leading zeros so "007" -> "7",
    # then bound the digit count: a string longer than the widest supported
    # value is out of range, and the bound stops int() from hitting Python's
    # integer-string conversion limit (which raises) on a huge token.
    if len(tokens) == 1 and _ASCII_DIGITS.fullmatch(tokens[0]):
        stripped = tokens[0].lstrip("0") or "0"
        if len(stripped) > _MAX_DIGITS:
            return None
        return _in_range(int(stripped))

    # Any digit token mixed into a word phrase, or a lone non-ASCII digit token
    # (str.isdigit() True but not matched above), is unsupported -> None. This
    # is the branch that catches fullwidth/Arabic-Indic/superscript digits
    # without ever calling int() on them.
    if any(tok.isdigit() for tok in tokens):
        return None

    # Hundreds compound: <unit> hundred [and] <below-hundred>.
    if "hundred" in tokens:
        idx = tokens.index("hundred")
        # Exactly one hundreds component, and it must be the second token.
        if tokens.count("hundred") != 1 or idx != 1:
            return None
        head = tokens[0]
        if head not in _UNITS or not (1 <= _UNITS[head] <= 9):
            return None
        hundreds = _UNITS[head] * 100
        remainder_tokens = tokens[idx + 1:]
        # Allow an optional connective 'and' immediately after 'hundred'.
        consumed_and = bool(remainder_tokens) and remainder_tokens[0] == "and"
        if consumed_and:
            remainder_tokens = remainder_tokens[1:]
        if not remainder_tokens:
            # Bare hundreds ("one hundred") is valid; a dangling connective
            # ("one hundred and") is an incomplete phrase -> None, so it does
            # not mis-resolve to the bare hundreds badge number.
            if consumed_and:
                return None
            return _in_range(hundreds)
        below = _parse_below_hundred(remainder_tokens)
        if below is None:
            return None
        return _in_range(hundreds + below)

    # Digit-by-digit reading (wh-click-number-dictation): every token is a
    # single digit word, read left to right ('one seven' -> 17, 'one zero
    # five' / 'one oh five' -> 105). Two or three digits, matching the 1..999
    # badge range; a leading zero is rejected because badge numbers never
    # start with zero. This branch must run BEFORE the colloquial pairing
    # branch below, whose remainder guard returns None for unit-unit shapes.
    if 2 <= len(tokens) <= 3 and all(tok in _DIGIT_WORDS for tok in tokens):
        if _DIGIT_WORDS[tokens[0]] == 0:
            return None
        value = 0
        for tok in tokens:
            value = value * 10 + _DIGIT_WORDS[tok]
        return _in_range(value)

    # Colloquial hundreds pairing without the word 'hundred': users read a
    # three-digit badge number aloud as 'one twelve' (112) or 'three twenty'
    # (320) (wh-click-number-dictation). The head must be a unit 1..9 and the
    # remainder must resolve to 10..99. A remainder of 1..9 never pairs --
    # a unit followed by units is digit-by-digit reading, claimed by the
    # branch above before this one runs.
    if 2 <= len(tokens) <= 3 and tokens[0] in _UNITS:
        head_value = _UNITS[tokens[0]]
        remainder = _parse_below_hundred(tokens[1:])
        if 1 <= head_value <= 9 and remainder is not None and remainder >= 10:
            return _in_range(head_value * 100 + remainder)
        return None

    # No hundreds component: a 1..99 cardinal.
    below = _parse_below_hundred(tokens)
    if below is None:
        return None
    return _in_range(below)


@lru_cache(maxsize=512)
def _some_word_extends(text: str, aliases: bool, zero: bool) -> bool:
    """Cached core of :func:`number_phrase_can_extend`; see its docstring."""
    for word in _PHRASE_WORDS:
        if parse_number_word(f"{text} {word}", aliases=aliases, zero=zero) is not None:
            return True
    return False


def number_phrase_can_extend(
    text: Optional[str],
    *,
    aliases: bool = False,
    zero: bool = False,
) -> bool:
    """True if some further number word could still extend ``text``.

    A spoken count arrives one word at a time, so a phrase that already
    parses is not necessarily finished: "twenty" is 20 and "twenty
    three" is 23. A caller that executes as soon as the count parses
    cuts the number short, which is exactly what made "backspace twenty
    three" press backspace twenty times and type "three"
    (wh-whole-utterance-command-matching.4). This answers the question
    that caller actually needs: could the next word make this a
    different, larger number?

    The answer is measured against this module's own grammar rather
    than a second list of rules, by asking parse_number_word about
    ``text`` plus each word the grammar admits. That keeps the one
    word-to-integer implementation (wh-number-words-one-parser) as the
    single authority, so a later change to the vocabulary or the
    hundreds handling moves this answer with it.

    Only word forms can extend a phrase, so the digit spellings need no
    special case: parse_number_word("twenty 3") and ("3 twenty") are
    both None, while ("twenty three") is 23. A count already spoken as
    digits therefore answers False and its caller can act at once.

    Cost, because this runs on the speech path: a cache miss parses
    ``text`` once per word in the grammar (about thirty short parses,
    all pure string work), and the result is cached. It is asked once
    per word while a count is buffering, not per utterance.

    Args:
        text: the count captured so far, spoken or typed.
        aliases: accept the STT homophones, as parse_number_word does.
        zero: accept the word "zero", as parse_number_word does.
            Pass both exactly as the caller's own count validation
            passes them, or this answer and that validation disagree.
            speech/actions.py:words_to_int uses aliases=True, zero=True.

    Returns:
        True if at least one word of the grammar extends ``text`` into
        another number this parser accepts; False for an empty or
        unparsable ``text``, and for a phrase nothing can extend.
    """
    if not text:
        return False
    cleaned = str(text).lower().strip()
    if not cleaned:
        return False
    return _some_word_extends(cleaned, aliases, zero)
