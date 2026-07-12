"""Parse a spoken/typed count into Optional[int] for the 'click N' overlay (wh-n29v).

Self-contained, pure, stateless helper. It exists separately from the existing
number helpers (speech/actions.py:words_to_int caps at ten and includes STT
homophones; speech/navigation/parser.py:_WORD_TO_INT caps at ten with
MAX_COUNT=50) so those callers are untouched. The numbered overlay needs counts
up to ~99 for a browser tab strip or the Outlook ribbon.

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

Returns None for: ordinals ('first', 'twenty-third', '1st'), zero, negatives,
out-of-range values (>999), empty/None/whitespace-only input, and any token or
combination it cannot confidently resolve. None lets the 'click N' routing fall
back to the by-name path on ambiguous input.
"""
import re
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

_MIN = 1
_MAX = 999
# Maximum number of significant digits a bare digit string may carry. Anything
# longer than the widest supported value is out of range, and bounding the
# length here keeps int() from ever hitting Python 3.12's integer-string
# conversion limit (which raises ValueError) before the range check runs.
_MAX_DIGITS = len(str(_MAX))


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


def parse_number_word(text: Optional[str]) -> Optional[int]:
    """Parse a spoken/typed count into an int in 1..999, or None.

    See the module docstring for the full set of recognized shapes and the
    None-returning cases. Pure and stateless; no logging side effects.
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

    # No hundreds component: a 1..99 cardinal.
    below = _parse_below_hundred(tokens)
    if below is None:
        return None
    return _in_range(below)
