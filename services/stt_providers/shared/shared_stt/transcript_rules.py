"""Shared transcript normalization and stability rules for sherpa engines.

Canonical extraction of the parakeet engine's `_normalize_text` (see
services/stt_providers/sherpa_offline_parakeet_stt_server/sherpa_engine.py)
so other sherpa providers do not add a third hand-maintained copy.

crewcut: the parakeet engine and shared_stt/whisper_engine.py still carry
their own inline copies of these rules; migrating them to this module is
the way to remove the duplication once their test suites are re-pointed.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Time-reformat rule: transducer models emit dotted-period AM/PM forms
# like "It is 8.17 p.m."; after the punctuation pass collapses "p.m." to
# "pm", this rule reshapes "8.17 pm" into "8:17 PM". The trailing \b
# keeps words that merely start with am/pm out of the rule ("0.75 amps"
# must not become "0:75 AMps" -- wh-251rh.1.2). The leading \b keeps
# longer digit runs from partially rewriting ("123.45 am" -- wh-251rh.3).
_TIME_PERIOD = re.compile(
    r'\b(\d{1,2})\.(\d{2})\s*(am|pm)\b',
    re.IGNORECASE,
)

# Canonicalize "am" / "pm" next to an HH:MM time to uppercase AM / PM.
_AMPM_UPPERCASE = re.compile(r'(\d{1,2}:\d{2}\s+)(am|pm)\b', re.IGNORECASE)

# Phone-number hyphenation. A 10-digit block surrounded by word
# boundaries becomes NNN-NNN-NNNN. \b on both ends prevents matching
# 11-digit country-code blocks or 9-digit ZIP+4 strings. Known
# false-positive surface: a genuinely non-phone 10-digit number in
# dictation will be hyphenated.
_HYPHENATE_PHONE = re.compile(r'\b(\d{3})(\d{3})(\d{4})\b')

# wh-parakeet-xray-hotword: a deliberate pause between the syllables of
# "x-ray" makes transducer models emit two words (measured raw form:
# "X, Ray Boost"), which breaks the Logic-side wake-word match -- the
# wake word must arrive as ONE word. Rejoin the single letter x + the
# word "ray", keeping the x's case. Runs after the punctuation pass.
_XRAY_JOIN = re.compile(r'\b([Xx])\s+[Rr]ay\b')


def _time_replace(match: re.Match) -> str:
    # wh-251rh.3.1: only values that read as a real 12-hour clock time
    # may rewrite ("0.75 am" / "13.99 pm" are measurements, not times).
    hour = match.group(1)
    minutes = match.group(2)
    if not (1 <= int(hour) <= 12 and 0 <= int(minutes) <= 59):
        return match.group(0)
    ampm = match.group(3).upper()
    return f"{hour}:{minutes} {ampm}"


_PRONOUN_APOSTROPHES = "'\u2019"


def _is_capitalized_pronoun(word: str) -> bool:
    r"""Report whether the word is the pronoun "I" or a contraction of it.

    English capitalizes "I" by grammar, not because it names anything, and
    the recognizers capitalize it on nearly every utterance -- measured at
    32 of 36 standalone occurrences in the recorded parakeet runs. So the
    capital says nothing about the word before it, and counting it as
    evidence kept a positional capital on ordinary dictation: "So I'm
    going" stayed capitalized where the rule used to produce "so I'm going"
    (wh-first-char-lowercase.1.1).

    Excluding it costs no wanted capital. The closing rule of
    normalize_transcript rewrites \bi\b to "I", and an apostrophe is a word
    boundary, so a lowercased "i'm" is restored to "I'm" after this
    condition has already run. Only the pronoun itself is excluded:
    "Illinois" is a real proper noun and still counts as evidence.
    """
    base = word
    for index, char in enumerate(word):
        if char in _PRONOUN_APOSTROPHES:
            base = word[:index]
            break
    return base == "I"


def has_capital_evidence(text: str) -> bool:
    """Report whether the text shows a capital that is not merely positional.

    A recognizer that treats each utterance as a sentence capitalizes the
    first character every time, so that character on its own says nothing
    about whether the word is a name. Measured over the recorded parakeet
    evaluation runs, 2,041 of 2,041 uppercase openings had a lowercase
    ground truth: "delete" came back as "Delete.". Two shapes elsewhere in
    the text do carry the information:

    - an uppercase letter after index 0 of the first word, as in
      "McDonald" or "NASA";
    - a capitalized second word, as in "Bill Smith".

    Callers use this to decide whether to leave the first character as the
    recognizer wrote it (wh-first-char-lowercase). Only the second word
    counts; a capital further along belongs to its own word. The pronoun
    "I" and its contractions do not count, because English capitalizes
    them by grammar -- see _is_capitalized_pronoun.
    """
    # crewcut: the spelled-out-letter rewrite runs before this condition at
    # both call sites and lowercases its whole match, so "N-A-S-A launched
    # it" arrives here as "nasa launched it" and the interior capitals this
    # function reads are already gone. The word form "NASA launched it"
    # survives; the spelled form cannot (wh-first-char-lowercase.1.2). Not
    # a regression: both forms produced "nasa" before this change. To
    # remove the limit, either decide the evidence before that rewrite, or
    # make the rewrite preserve an all-uppercase spelled sequence. Both
    # change a normalization outside the first-character rule, so neither
    # was done here.
    words = text.split()
    if not words:
        return False
    if any(ch.isupper() for ch in words[0][1:]):
        return True
    if len(words) < 2:
        return False
    second = words[1]
    return second[:1].isupper() and not _is_capitalized_pronoun(second)


def normalize_transcript(text: str) -> str:
    """Normalize transducer transcription output for WheelHouse.

    - Convert spelled-out letter sequences (V-O-X -> vox)
    - Remove punctuation (except periods between digits and colons in times)
    - Rejoin a split "x ray" / "X, Ray" into "x-ray"
    - Dotted-period time form: '8.17 p.m.' -> '8:17 PM'
    - AM/PM uppercase beside an HH:MM time
    - Phone-number hyphenation: '7035551234' -> '703-555-1234'
    - Lowercase the first character, unless the utterance carries
      capital-letter evidence (see has_capital_evidence)
    - Always capitalize pronoun 'I'
    """
    if not text:
        return ""

    # Convert spelled-out words (3+ letters).
    text = re.sub(
        r'\b([A-Za-z](?:-[A-Za-z]){2,})\b',
        lambda m: m.group(0).replace('-', '').lower(),
        text,
    )

    # Remove punctuation FIRST so the time rules see bare "am"/"pm"
    # instead of the dotted forms "a.m." / "p.m.". Keeps periods between
    # digits (preserves "8.17" decimal time form) and colons between
    # digits (preserves HH:MM that a time rule already produced).
    text = re.sub(r'(?<!\d)\.|\.(?!\d)|(?<!\d):|:(?!\d)|[,!?;]', '', text)

    # Rejoin a split "x ray" into "x-ray" (wh-parakeet-xray-hotword).
    text = _XRAY_JOIN.sub(lambda m: m.group(1) + '-ray', text)

    # Time-reformat rule: operates on text with dotted AM/PM already
    # collapsed to bare am/pm by the punctuation pass.
    text = _TIME_PERIOD.sub(_time_replace, text)

    # Uppercase am/pm when anchored to an HH:MM time.
    text = _AMPM_UPPERCASE.sub(
        lambda m: m.group(1) + m.group(2).upper(), text
    )

    # Hyphenate 10-digit blocks as phone numbers.
    text = _HYPHENATE_PHONE.sub(r'\1-\2-\3', text)

    # Lowercase the first character unless the utterance carries a
    # capital that is not merely positional (wh-first-char-lowercase).
    if text and not has_capital_evidence(text):
        text = text[0].lower() + text[1:]

    # Always capitalize 'I' and contractions
    text = re.sub(r'\bi\b', 'I', text)

    return text


# --------------------------------------------------------------------
# Inverse text normalization (ITN), wh-shared-itn-rules-module.
#
# Word-emitting recognizers hand Wheelhouse "fifty nine dollars and
# seventeen cents" where the user wants "$59.17" (observed live on the
# retired streaming provider, David, 2026-08-25). These rules are the deterministic replacement for the
# NeMo/pynini ITN removed in 984c7ba: pure functions, no new
# dependencies. They are safe to run on every interim hypothesis BEFORE
# the stability layer confirms words, so a confirmed word does not have
# to be rewritten at finalization.
#
# Command safety (David's direction, 2026-08-25): a phrase converts only
# when it carries two or more number words that read as ONE number, or a
# number word beside a unit anchor (dollar/cent, AM/PM, "point"). A lone
# number word stays a word, because Wheelhouse matches spoken commands
# against word forms -- "backspace one" must never become "backspace 1".
# The accepted cost is that a dictated lone "seven" types "seven".
#
# Refusal policy (David's ruling, 2026-08-25): once ANY rule refuses a
# span, the refusal absorbs every adjacent word of the number-ish
# vocabulary until punctuation or any other word ends it -- see
# _absorbed_refusal_end, which is where every refusal endpoint in
# _match_number_phrase and _try_date_ordinal comes from. Rounds 2 to 5
# of the review each reported one shape of the same defect, a refusal
# reaching only as far as the shape that refused, and patching shape
# pairs never converged.
# The order this module sorts outcomes by: a wrong conversion is worst,
# a missed conversion second, and words are always safe.
#
# Phase 4 (wh-shared-itn-addresses-dates) added spec rules 6 and 7 and
# the hyphenated number word the crewcut here used to defer. Its review
# then found rule 7 blind to a hyphenated ordinal (_hyphenated_ordinal)
# and half-converting a date range (_date_range_end); the range is a
# refusal, so it obeys the ranking above -- absorption included, which
# is the third finding of that review. What is still missing is rule
# 8, the bounded true-casing that capitalizes the street name of a
# matched address and the month of a matched date;
# wh-shared-itn-truecasing carries it, which is why rule 7 leaves a
# date as "january 3rd" rather than "January 3rd".
# --------------------------------------------------------------------

_ITN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_ITN_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_ITN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_ITN_SCALES = {
    "hundred": 100, "thousand": 1000,
    "million": 1000000, "billion": 1000000000,
}

# "oh" is a spoken zero, but it is far more often the interjection, so
# it is NOT a number word: it joins a phrase only after a digit or a
# teen ("nine oh five", "twelve oh five"), never at the start ("oh
# five minutes").
#
# The LETTER "o" is the same spoken zero (wh-itn-oh-letter-alias). The
# provider emits it where the speaker said the sound, so "seven point o
# seven" is the same utterance as "seven point oh seven" and has to
# read as 7.07. The alias is LOOKUP-ONLY: nothing rewrites "o" to "oh",
# so a phrase that stays words keeps the word the speaker said and
# "o brother" is never "oh brother". Both spellings carry the same
# interjection risk, so both obey the after-a-digit rule above -- which
# is also what keeps "nine o clock" words, because "clock" is not a
# number word and no rule can read what the run leaves behind.
_ITN_OH_WORDS = {"oh", "o"}
_ITN_DIGITS = dict(_ITN_UNITS)
for _oh_word in _ITN_OH_WORDS:
    _ITN_DIGITS[_oh_word] = 0
del _oh_word

_ITN_NUMBER_WORDS = (
    set(_ITN_UNITS) | set(_ITN_TEENS) | set(_ITN_TENS) | set(_ITN_SCALES)
)

# The longest number phrase this module can read is about twenty words
# ("nine hundred ninety nine billion ..."), and a spelled-out card
# number is sixteen, so this cap is far above any real phrase.
_ITN_MAX_RUN_WORDS = 64

# Thousands separators (wh-itn-thousands-commas). David's request from
# live testing, 2026-08-25: "$1000000" must type as "$1,000,000" and
# "15000" as "15,000". Grouping starts at FIVE digits, the threshold he
# ratified, so a scale-built year stays clean -- "two thousand five" is
# 2005 and never 2,005. Lowering this constant is a behavior change that
# needs his word, not a tidy-up.
_ITN_GROUP_MIN = 10000

_DOLLAR_WORDS = {"dollar", "dollars"}
_CENT_WORDS = {"cent", "cents"}
_PERIOD_WORDS = {"am", "pm"}

# Rule 6. A paired house number converts only in front of one of these
# six suffixes. The list is the spec's own and is closed: every word
# added to it converts one more ambiguous phrase.
_STREET_SUFFIXES = {"street", "avenue", "road", "drive", "lane", "court"}

# How far past the number the suffix may sit. The street name stands
# between them ("twelve thirty four main street"), so the reach is one
# word: that covers the spec's example and refuses a long street name,
# where a paired reading is far less certain (boss ruling, 2026-08-25).
# Widen it on measured evidence, never on taste.
_STREET_SUFFIX_REACH = 1

# How far the suffix may sit for the FORMATTING decision only: an
# already-converted cardinal in front of a suffix is written bare. A
# directional prefix plus a two-word street name is the widest common
# address shape ("north maple grove road"), so three words. Widening
# this reach can only ever remove separators, never change a value, so
# it may exceed rule 6's licensing reach without touching that ruling
# (finding wh-itn-thousands-commas.1.1).
_BARE_ADDRESS_REACH = 3

# Rule 7. The month name is the unit anchor an ordinal needs -- the role
# "dollars" plays for money -- so "january third" is one ordinal word
# beside an anchor and a bare "third" stays a word.
#
# crewcut: THREE month names are deliberately absent -- "may", "march"
# and "august". Each is also an ordinary English word (the modal verb,
# the verb, the adjective), so as anchors they converted ordinary
# dictation wrongly: "you may first check the log" became "you may 1st
# check the log", "we march third in the parade" became "we march 3rd
# in the parade", and "the august first edition" became "the august 1st
# edition". This module ranks a wrong conversion as the worst class and
# a missed conversion second, so the three names come out and the other
# nine keep converting; the cost is that "march third" as a real date
# stays words. That deviates from the spec, whose rule-7 example IS
# "march third", and David ratified the exclusion anyway on 2026-08-25.
# Re-enabling any of the three therefore needs a NEW David ruling. What
# would earn one: a rule that reads the whole clause, or the
# capitalization signal phase 5 (rule 8) introduces -- either can tell
# the month from the verb, which this module cannot.
_ITN_MONTHS = {
    "january", "february", "april", "june",
    "july", "september", "october", "november", "december",
}

_ITN_ORDINAL_UNITS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
}
_ITN_ORDINAL_TEENS = {
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
}
_ITN_ORDINAL_TENS = {"twentieth": 20, "thirtieth": 30}

# The letters an ordinal takes, by its last digit. 11th, 12th and 13th
# are the exception this lookup cannot express.
_ITN_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}

# No month has more than 31 days, so a larger ordinal is not a date.
# WHICH months are shorter is calendar knowledge this module does not
# carry: "february thirtieth" writes the day the speaker said, and
# checking it against a real calendar is not this stage's job.
_ITN_MAX_DAY = 31

# The article of "and a hundred" is not a number word, so it ends the
# run _collect_run is building. The connector refusal steps over it to
# see the number behind it (wh-shared-itn-review.13).
_ITN_ARTICLES = {"a", "an"}

# Leading and trailing punctuation around a token. The middle group is
# lazy, so the trailing group takes everything after the last word
# character: "cents." splits into ("", "cents", ".").
_ITN_TOKEN_PARTS = re.compile(r'\A([^\w]*)(.*?)([^\w]*)\Z')


class _Token(NamedTuple):
    lead: str
    core: str
    trail: str
    low: str


def _split_token(token: str) -> _Token:
    match = _ITN_TOKEN_PARTS.match(token)
    lead, core, trail = match.group(1), match.group(2), match.group(3)
    return _Token(lead, core, trail, core.lower())


def _hyphen_pieces(token: _Token) -> list[_Token] | None:
    """`token` split at its hyphens, or None when it is not a hyphenated
    number word.

    A recognizer can hand this module "twenty-five" as ONE token, which
    no rule could read (the phase-1 crewcut assigned it here). Only a
    token whose every hyphen-separated piece is a number word splits, so
    "x-ray", "well-known", "twenty-five-year-old" and the hyphenated
    phone number normalize_transcript produces are left whole and can
    convert nothing.

    The pieces carry the original token's leading and trailing
    punctuation on the ends, and _tokenize puts the hyphen back as the
    separator between them, so a split token no rule converts is rebuilt
    character for character.
    """
    if "-" not in token.core:
        return None
    parts = token.core.split("-")
    if not all(part.lower() in _ITN_NUMBER_WORDS for part in parts):
        return None
    last = len(parts) - 1
    return [
        _Token(
            token.lead if position == 0 else "",
            part,
            token.trail if position == last else "",
            part.lower(),
        )
        for position, part in enumerate(parts)
    ]


def _tokenize(text: str) -> tuple[list[str], list[str], list[_Token]]:
    """The tokens of `text`, the separator that follows each, and the
    record for each.

    A hyphenated number word becomes several records joined by a "-"
    separator, which is what lets the rules read "twenty-five" as the
    run they already read for "twenty five". Every other token is one
    record, exactly as before. The separator is re-emitted with the
    token it follows, so text no rule converts is returned unchanged.
    """
    parts = re.split(r'(\s+)', text)
    spoken = parts[0::2]
    gaps = parts[1::2]
    gaps.append("")

    words: list[str] = []
    separators: list[str] = []
    records: list[_Token] = []
    for token, gap in zip(spoken, gaps):
        record = _split_token(token)
        pieces = _hyphen_pieces(record)
        if pieces is None:
            words.append(token)
            separators.append(gap)
            records.append(record)
            continue
        last = len(pieces) - 1
        for position, piece in enumerate(pieces):
            words.append(piece.lead + piece.core + piece.trail)
            separators.append("-" if position < last else gap)
            records.append(piece)
    return words, separators, records


def _adjacent(records: list[_Token], index: int) -> bool:
    """True when token `index` follows token `index - 1` with no
    punctuation between them."""
    return (
        0 < index < len(records)
        and records[index - 1].trail == ""
        and records[index].lead == ""
    )


def _within_length_cap(words: list[str]) -> bool:
    """False for a run too long to read as one number.

    apply_itn runs on every interim hypothesis, so a pathological run
    must leave the text alone rather than raise: Python refuses an int
    or str conversion past 4300 digits, and 5000 spoken digit words
    reach that limit through the spelled-out-amount fallback in
    _single_value (wh-shared-itn-review.7).
    """
    return len(words) <= _ITN_MAX_RUN_WORDS


def _parse_cardinal(words: list[str]) -> list[int] | None:
    """Read number words as one or more integers, left to right.

    "fifty nine" is one number (59); "four ten" is two (4 and 10). The
    caller uses that difference: only a run that reads as ONE number is
    unambiguous enough to convert without a unit anchor.

    A scale sequence that is not English refuses the whole run instead
    of inventing a value: "one thousand million" is not 1001000 and
    "one hundred hundred" is not 10000 (wh-shared-itn-review.7).

    Group presence is parser state (`has_group`), never the numeric
    value of `current`: an explicit "zero" is a coefficient the speaker
    said, and reading it as an absent one turned "zero million" into
    1000000 (wh-shared-itn-review.11).
    """
    if any(word in _ITN_OH_WORDS for word in words):
        return None
    if not _within_length_cap(words):
        return None
    # A spoken zero does not scale. "zero hundred" is not 100 and not
    # 0 either -- nobody says it, so the run stays words
    # (wh-shared-itn-review.11).
    if "zero" in words and any(word in _ITN_SCALES for word in words):
        return None

    numbers: list[int] = []
    total = 0
    current = 0
    started = False
    has_unit = False
    has_ten = False
    has_hundred = False
    has_group = False
    last_scale = 0

    def flush() -> None:
        nonlocal total, current, started, has_unit, has_ten
        nonlocal has_hundred, has_group, last_scale
        if started:
            numbers.append(total + current)
        total = 0
        current = 0
        started = False
        has_unit = False
        has_ten = False
        has_hundred = False
        has_group = False
        last_scale = 0

    for word in words:
        if word in _ITN_TENS:
            if has_ten or has_unit:
                flush()
            current += _ITN_TENS[word]
            has_ten = True
            has_group = True
        elif word in _ITN_TEENS:
            if has_ten or has_unit:
                flush()
            current += _ITN_TEENS[word]
            has_unit = True
            has_group = True
        elif word in _ITN_UNITS:
            if has_unit:
                flush()
            current += _ITN_UNITS[word]
            has_unit = True
            has_group = True
        elif word in _ITN_SCALES:
            scale = _ITN_SCALES[word]
            # A scale needs the group in front of it once the number
            # has started: "one million thousand" is not 1001000 and
            # "one thousand hundred" is not 1100. Only a LEADING scale
            # keeps the implicit one, because "a hundred" / "a
            # thousand" is ordinary speech and its article is not a
            # number word (wh-shared-itn-review.11).
            if started and not has_group:
                return None
            if scale == 100:
                # One hundred per group: "one hundred thousand two
                # hundred" is a number, "one hundred hundred" is not.
                if has_hundred:
                    return None
                current = (current if has_group else 1) * 100
                has_hundred = True
                has_group = True
            else:
                # The scales of one number descend: "one million two
                # hundred thousand" is a number, "one thousand million"
                # and "one million million" are not.
                if last_scale and scale >= last_scale:
                    return None
                last_scale = scale
                total += (current if has_group else 1) * scale
                current = 0
                has_hundred = False
                has_group = False
            has_unit = False
            has_ten = False
        else:
            return None
        started = True

    flush()
    return numbers


def _value_words(run: list[str]) -> list[str] | None:
    """The run's number words with "and" removed, or None when an "and"
    stands where the grammar has no group for it.

    "one hundred and five" is English; "one hundred and thousand" is
    not, and dropping that "and" reads it as 100000
    (wh-shared-itn-review.11). _collect_run keeps such an "and" inside
    the run on purpose: refusing the value here leaves the WHOLE phrase
    as words, where stopping the run at "hundred" would convert its
    head and type "100 and thousand".
    """
    for index, word in enumerate(run[:-1]):
        if word == "and" and run[index + 1] in _ITN_SCALES:
            return None
    return [word for word in run if word != "and"]


def _single_value(run: list[str]) -> int | None:
    """The one integer a run stands for, or None when it stands for
    several. Used by the anchored rules, which need a single amount."""
    words = _value_words(run)
    if words is None:
        return None
    if not words or not _within_length_cap(words):
        return None
    values = _parse_cardinal(words)
    if values is not None and len(values) == 1:
        return values[0]
    # "five five five dollars" is a spelled-out amount, not three
    # numbers.
    if len(words) >= 2 and all(word in _ITN_DIGITS for word in words):
        return int("".join(str(_ITN_DIGITS[word]) for word in words))
    return None


def _scale_built(run: list[str]) -> bool:
    """True when the run carries a scale word.

    A group with no scale word tops out at 99, so every value
    _parse_cardinal reads at or above _ITN_GROUP_MIN is scale-built and
    this answers True for all of them. What it actually separates is the
    OTHER reading _single_value carries: the spelled-out amount, where
    the speaker said the digits one at a time. "nine oh two one oh
    dollars" is those five digits, and a digit-by-digit run is never
    grouped -- the bead's second acceptance criterion -- so it types
    "$90210" while "fifteen thousand dollars" types "$15,000". One
    amount with two written forms is the accepted cost of keeping every
    digit-by-digit run out of the grouping, which is what makes "nine oh
    two one oh" a ZIP code.
    """
    return any(word in _ITN_SCALES for word in run)


def _write_number(run: list[str], value: int) -> str:
    """`value` written for typing: comma grouped once it reaches
    _ITN_GROUP_MIN, bare below it (wh-itn-thousands-commas).

    Python's format spec does the grouping, so this module carries no
    digit-walking code of its own.

    Only a QUANTITY comes through here -- a cardinal, the dollars or
    cents of an amount, the whole half of a decimal. Every identifier
    this module writes is formatted by its own rule and stays bare: the
    spelled-out digit sequence of rule 5, the paired house number of
    rule 6, the hour and minutes of a clock time, and the day of a date.
    The last three cannot reach the threshold anyway -- the hour caps at
    12, the day at 31, and both chunks of a paired house number at 99,
    so 9999 is the largest that rule can write -- and so does the
    fractional half of a decimal, which is a string of digits rather
    than a value.
    """
    if value < _ITN_GROUP_MIN or not _scale_built(run):
        return str(value)
    return f"{value:,}"


def _collect_run(records: list[_Token], start: int) -> tuple[list[str], int]:
    """The number-word phrase beginning at `start`, and the index after
    it. Punctuation ends the phrase, so "fifty, nine" is not "59"."""
    run: list[str] = []
    index = start
    while index < len(records):
        token = records[index]
        if index > start and (token.lead or records[index - 1].trail):
            break
        low = token.low
        if low in _ITN_NUMBER_WORDS:
            run.append(low)
        elif (
            low in _ITN_OH_WORDS
            and run
            and (run[-1] in _ITN_DIGITS or run[-1] in _ITN_TEENS)
        ):
            # A teen is an hour ("twelve oh five"), and admitting it
            # can only enable the clock rule: _parse_cardinal refuses
            # any run holding "oh", and _try_digit_sequence refuses
            # any run holding a teen (wh-shared-itn-review.1).
            run.append(low)
        elif (
            low == "and"
            and run
            and run[-1] in _ITN_SCALES
            and _adjacent(records, index + 1)
            and records[index + 1].low in _ITN_NUMBER_WORDS
        ):
            run.append(low)
        else:
            break
        index += 1
    return run, index


def _cents_after(
    records: list[_Token], start: int
) -> tuple[int, int] | None:
    """A "(and) <number> cents" tail, as (value, index after it)."""
    index = start
    if not _adjacent(records, index):
        return None
    if records[index].low == "and":
        index += 1
        if not _adjacent(records, index):
            return None
    run, end = _collect_run(records, index)
    if not run or not _adjacent(records, end):
        return None
    if records[end].low not in _CENT_WORDS:
        return None
    value = _single_value(run)
    if value is None or not 0 <= value <= 99:
        return None
    return value, end + 1


def _implied_cents_follows(records: list[_Token], start: int) -> bool:
    """True when a bare number run follows the dollar anchor.

    "one dollar fifty" is the implied-cents form, which this module
    does not read; converting the dollars alone would type "$1 fifty",
    so _try_money refuses and the whole phrase stays words
    (wh-shared-itn-review.3). Keeping a longer tail whole is no longer
    this shape's own problem -- _absorbed_refusal_end carries EVERY
    refusal through its adjacent number words, so this reports the shape
    and nothing else. Between wh-shared-itn-review.6 and the absorption
    ruling it returned the index after the tail, because back then the
    money refusal was the only thing that knew how far to reach; a
    two-word tail converted alone as "five dollars 59" and a second pass
    wrote "$5 59".

    A run closed by "cent(s)" is NOT this shape -- that is the explicit
    grammar _cents_after already read and refused on value alone ("five
    dollars and one hundred cents").
    """
    index = start
    if not _adjacent(records, index):
        return False
    if records[index].low == "and":
        index += 1
        if not _adjacent(records, index):
            return False
    run, end = _collect_run(records, index)
    if not run:
        return False
    if _adjacent(records, end) and records[end].low in _CENT_WORDS:
        return False
    return True


def _try_money(
    records: list[_Token], run: list[str], anchor: int
) -> tuple[str, int] | None:
    word = records[anchor].low
    if word in _DOLLAR_WORDS:
        value = _single_value(run)
        if value is None:
            return None
        cents = _cents_after(records, anchor + 1)
        if cents is None:
            if _implied_cents_follows(records, anchor + 1):
                return None
            return f"${_write_number(run, value)}", anchor + 1
        amount, end = cents
        # The cents are a two-digit field, so only the dollars can reach
        # the grouping threshold: "$15,000.20" (wh-itn-thousands-commas).
        return f"${_write_number(run, value)}.{amount:02d}", end
    if word in _CENT_WORDS:
        value = _single_value(run)
        if value is None:
            return None
        return f"{_write_number(run, value)} {records[anchor].core}", anchor + 1
    return None


def _period_word(record: _Token) -> str | None:
    """The am/pm period an anchor token stands for, or None.

    "p.m." reaches here with its inner period intact, because the
    punctuation split only strips leading and trailing punctuation."""
    period = record.low.replace(".", "")
    return period if period in _PERIOD_WORDS else None


def _clock_minutes(words: list[str]) -> str | None:
    """The minutes half of a clock time: "" for none, None to refuse."""
    if not words:
        return ""
    if (
        len(words) == 2
        and words[0] in _ITN_OH_WORDS
        and words[1] in _ITN_UNITS
    ):
        return f"{_ITN_UNITS[words[1]]:02d}"
    if len(words) == 1:
        value = _ITN_TEENS.get(words[0], _ITN_TENS.get(words[0]))
        if value is None or value > 59:
            return None
        return f"{value:02d}"
    if len(words) == 2 and words[0] in _ITN_TENS and words[1] in _ITN_UNITS:
        value = _ITN_TENS[words[0]] + _ITN_UNITS[words[1]]
        if value > 59:
            return None
        return f"{value:02d}"
    return None


def _try_clock_time(
    records: list[_Token], run: list[str], anchor: int
) -> tuple[str, int] | None:
    # An AM/PM anchor is what makes "four ten" a time; without it the
    # phrase could be a house number or two separate numbers, so it
    # stays words (spec rule 3).
    period = _period_word(records[anchor])
    if period is None:
        return None
    if "and" in run:
        return None
    hour = _ITN_UNITS.get(run[0], _ITN_TEENS.get(run[0]))
    if hour is None or not 1 <= hour <= 12:
        return None
    minutes = _clock_minutes(run[1:])
    if minutes is None:
        return None
    if minutes == "":
        return f"{hour} {period.upper()}", anchor + 1
    return f"{hour}:{minutes} {period.upper()}", anchor + 1


def _fraction_after(
    records: list[_Token], anchor: int
) -> tuple[str | None, int]:
    """The fractional tail standing directly against the "point" at
    `anchor`: its written digits, and the index after the WHOLE tail.

    One scanner serves both the decimal grammar and the refusal span, so
    the two cannot disagree about where the tail ends
    (wh-shared-itn-review.12).

    The digits are None when the tail carries a number word the grammar
    cannot read, and the index still covers that word and everything the
    tail continues with. Reading only the digit prefix took "five" as
    the fraction of "three point five hundred dollars", stopped in front
    of "hundred", and left it to convert on its own as "3.5 $100".

    "oh" is a digit anywhere in a fraction, leading included: the
    grammar has always read "three point oh five" as 3.05, so a refusal
    span that stopped in front of a leading "oh" let the words behind it
    convert as "... point oh $5".

    An index equal to `anchor + 1` means nothing numeric follows at all,
    which is what keeps the ordinary word out of every decimal rule
    ("the three point plan" -- wh-shared-itn-review.8).
    """
    digits: list[str] = []
    readable = True
    index = anchor + 1
    while _adjacent(records, index):
        low = records[index].low
        if low in _ITN_DIGITS:
            digits.append(str(_ITN_DIGITS[low]))
        elif low in _ITN_NUMBER_WORDS:
            # A number word continues the spoken fraction but is not a
            # spoken digit, so the whole attempted decimal is
            # unreadable. Collection carries on: the caller has to
            # refuse through the rest of the tail, not stop here.
            readable = False
        else:
            break
        index += 1
    if not readable or not digits:
        return None, index
    return "".join(digits), index


def _try_decimal(
    records: list[_Token], run: list[str], anchor: int
) -> tuple[str, int] | None:
    if records[anchor].low != "point":
        return None
    whole = _single_value(run)
    if whole is None:
        return None
    # The whole half is a value and groups; the fractional half is the
    # string of digits _fraction_after collects and never does, so
    # "fifteen thousand point five" is "15,000.5"
    # (wh-itn-thousands-commas). The two lines below stay adjacent
    # because the gate's M11 pattern spells them together.
    digits, end = _fraction_after(records, anchor)
    if digits is None:
        return None
    return f"{_write_number(run, whole)}.{digits}", end


def _refused_decimal_follows(records: list[_Token], anchor: int) -> bool:
    """True when the "point" at `anchor` carries a fractional tail the
    decimal grammar could not read.

    Only an adjacent number run makes the phrase an attempted decimal,
    which is what keeps "the three point plan" -- where "point" is the
    ordinary word -- out of the refusal (wh-shared-itn-review.8). The
    tail comes from _fraction_after, the same scanner the decimal
    grammar reads, so the refusal and the grammar cannot disagree about
    what stands after the "point" (wh-shared-itn-review.12); an index
    equal to `anchor + 1` means nothing numeric follows at all. The one
    caller reaches this with an anchor already adjacent to the run in
    front of it, which is the adjacency _fraction_after re-reads.
    """
    _, end = _fraction_after(records, anchor)
    return end != anchor + 1


def _number_ish(record: _Token) -> bool:
    """True for a token that can continue a spoken number phrase.

    This is a vocabulary, not a grammar: it holds every word the rules
    of this module read -- number words, the spoken zero "oh", the
    decimal "point", and the dollar, cent and AM/PM anchors -- without
    asking whether they read as one number. Only _absorbed_refusal_end
    uses it, so a word entering this set can widen a refusal and can
    never convert anything.
    """
    low = record.low
    if low in _ITN_NUMBER_WORDS or low in _ITN_OH_WORDS:
        return True
    if low == "point":
        return True
    if low in _DOLLAR_WORDS or low in _CENT_WORDS:
        return True
    return _period_word(record) is not None


def _absorbed_refusal_end(records: list[_Token], end: int) -> int:
    """`end` carried past every adjacent number-ish word.

    Rounds 2 to 5 of the review each reported one shape of a single
    defect: a refusal endpoint computed for the shape that refused stops
    short when a DIFFERENT number-ish shape stands against it, and the
    scanner converts the continuation. It was an implied-cents tail
    (wh-shared-itn-review.6), a fractional tail (.8), a chained "point"
    (.10), an article-bearing connector (.13), that connector's own
    decimal and cents continuations (.14), and a refused decimal in
    front of a connector (.15). Every one of those endpoints now comes
    from here instead, so no pair of shapes can leave a gap between
    them (David's ruling, 2026-08-25).

    Only a REFUSAL absorbs. A phrase that converted keeps the endpoint
    its rule returned, and a token that started no phrase at all is
    still stepped over one token at a time, which is what keeps "oh five
    pm" reading as "oh 5 PM".

    Punctuation ends the span, because _adjacent is what the loop steps
    on: "five dollars, twenty one point twenty five" is two phrases
    judged separately. The accepted cost is that a refused phrase
    suppresses an adjacent unpunctuated amount that might have been
    separate speech -- "five five five pm and twenty dollars" stays
    words entirely -- and unpunctuated adjacency to a refused phrase is
    exactly where separateness cannot be trusted.

    The loop walks forward once and the scanner resumes at the index it
    returns, so the whole scan stays linear in the token count.
    """
    index = end
    while _adjacent(records, index):
        if records[index].low == "and":
            # The article of "and a hundred" is not a number word, so
            # the vocabulary alone would end the span in front of the
            # number behind it (wh-shared-itn-review.13, .14).
            index += 1
            if (
                _adjacent(records, index)
                and records[index].low in _ITN_ARTICLES
            ):
                index += 1
        elif _number_ish(records[index]):
            index += 1
        else:
            break
    return index


def _anchor_refuses(records: list[_Token], anchor: int) -> bool:
    """True when an anchored rule claimed this anchor and refused it.

    An anchor whose rule refused keeps the WHOLE phrase as words. The
    unanchored rules must not convert the run the refusal was about, and
    the scanner must not resume inside the tail either -- otherwise the
    refusal means nothing:

    - "five five five pm" is a time this module cannot read, not the
      number 555 (wh-shared-itn-review.2);
    - "five dollars fifty nine" is an implied-cents amount it cannot
      read, not "$5 59" (wh-shared-itn-review.6);
    - "twenty one point twenty five" is a decimal it cannot read, not
      "21 point 25" (wh-shared-itn-review.8).

    An AM/PM, dollar or cent anchor is always a claim: the caller
    reaches this only after _try_money and _try_clock_time have already
    declined the anchor they read, and a phrase they cannot read is one
    amount the speaker said, not two. "point" is the one anchor that is
    also an ordinary word, so it claims nothing without a fractional
    tail behind it.

    Only the claim is decided here. How far the refusal reaches is
    _absorbed_refusal_end's answer for every refusal alike
    (wh-shared-itn-review.10, .14, .15).
    """
    if _period_word(records[anchor]) is not None:
        return True
    word = records[anchor].low
    if word in _DOLLAR_WORDS or word in _CENT_WORDS:
        return True
    if word == "point":
        return _refused_decimal_follows(records, anchor)
    return False


def _connector_refuses(records: list[_Token], end: int) -> bool:
    """True when an "and" _collect_run did not admit stands in front of
    a number, which is the shape that splits one amount in two.

    _collect_run admits "and" only after a scale and in front of a
    number word, so the article of ordinary speech ends the run: "one
    thousand and a hundred dollars" converted its head as a cardinal and
    its tail against its own anchor, and typed "1000 and a $100"
    (wh-shared-itn-review.13). One amount the speaker said once must not
    become two written quantities.

    The shape is narrow on purpose -- the "and", at most one article,
    then an adjacent number-word run -- because an "and" followed by
    anything else is ordinary speech whose head still converts: "fifty
    nine and then some" is "59 and then some" and "twenty one and a
    half" is "21 and a half". The accepted cost of the shape is that
    "twenty one and five" stays words, which is the safe direction for a
    phrase this module cannot read as one number.

    A second connector needs no loop here: once the shape refuses,
    _absorbed_refusal_end carries the span through every further "and",
    article and continuation (wh-shared-itn-review.14).
    """
    if not (_adjacent(records, end) and records[end].low == "and"):
        return False
    start = end + 1
    if not _adjacent(records, start):
        return False
    if records[start].low in _ITN_ARTICLES:
        start += 1
        if not _adjacent(records, start):
            return False
    run, _ = _collect_run(records, start)
    return bool(run)


def _try_digit_sequence(run: list[str]) -> str | None:
    # Three or more digit words read as a spelled-out sequence: a ZIP
    # code, a phone number, a room number (spec rule 5). Two is left
    # alone, because "one two" is more often two spoken numbers.
    if len(run) < 3 or any(word not in _ITN_DIGITS for word in run):
        return None
    return "".join(str(_ITN_DIGITS[word]) for word in run)


def _try_cardinal(
    records: list[_Token], run: list[str], end: int
) -> str | None:
    """The written form of a run that reads as ONE cardinal, or None.

    The records and the endpoint are read for one thing only: a cardinal
    standing in front of a street suffix is an address number, and an
    address number is written bare (wh-itn-thousands-commas). Nothing
    else about the position changes what this rule converts.
    """
    words = _value_words(run)
    if words is None:
        return None
    if len(words) < 2:
        return None
    values = _parse_cardinal(words)
    if values is None or len(values) != 1:
        return None
    if _street_suffix_near(records, end, _BARE_ADDRESS_REACH):
        # "15000 main street", never "15,000 main street" -- the bead's
        # second acceptance criterion keeps a house number bare, and
        # this is the only path one can reach the threshold on: rule 6's
        # paired form caps both of its chunks at 99. The bare write
        # covers a suffix up to _BARE_ADDRESS_REACH words out; a suffix
        # past that reach is not read as an address, and the quantity
        # keeps its separators.
        #
        # crewcut: the six suffix words are ordinary nouns, so this also
        # takes the separators off a quantity that merely stands within
        # reach of one -- "twenty thousand road miles" types "20000 road
        # miles", and the widened reach extends that to "twenty thousand
        # winding mountain road miles". That is a MISSED grouping, which
        # this module ranks below a wrong conversion, and the way to
        # remove it is the clause-level reading rule 8 needs anyway.
        return str(values[0])
    return _write_number(run, values[0])


def _street_suffix_near(
    records: list[_Token], end: int, reach: int = _STREET_SUFFIX_REACH
) -> bool:
    """True when a street suffix stands within `reach` of the run ending
    at `end`.

    The suffix is the licence rule 6 needs, because the paired reading
    is ambiguous on its own -- "twelve thirty four" is also 12:34. The
    default reach is _STREET_SUFFIX_REACH words, which the street name
    uses up; _try_cardinal passes the wider _BARE_ADDRESS_REACH for its
    formatting-only decision.

    A word between the number and the suffix has to be an ordinary word.
    A number-ish word there belongs to a spoken number this module could
    not read ("twelve thirty oh street"), and an "and" there is the
    connector shape, so neither may license an address.

    Punctuation ends the reach the way it ends a run and detaches a unit
    anchor: "twelve thirty four, main street" is two phrases.
    """
    index = end
    for _ in range(reach + 1):
        if not _adjacent(records, index):
            return False
        if records[index].low in _STREET_SUFFIXES:
            return True
        if _number_ish(records[index]) or records[index].low == "and":
            return False
        index += 1
    return False


def _try_house_number(
    records: list[_Token], run: list[str], end: int
) -> str | None:
    """The digits of a PAIRED house number, or None (spec rule 6).

    The paired reading is the only thing this rule adds: a house number
    that reads as ONE cardinal already converts through _try_cardinal,
    with no suffix anywhere ("four hundred main street" is "400 main
    street"). Two chunks spoken as one written number are what stayed
    words -- "twelve thirty four" as 1234, "one twenty three" as 123.

    That is why the rule runs at the fall-through of
    _match_number_phrase, after every other rule has declined (boss
    ruling, 2026-08-25): from there it can only convert a phrase that is
    words today, so it cannot change a conversion that already works.

    The trailing chunk must read as two digits, because that is what
    makes the concatenation the only reading: "twelve thirty four" is
    1234, while "twelve four" is 124 or 1204 and stays words. The
    leading chunk is capped at two digits as well, which keeps a longer
    ambiguous run out ("five hundred twelve thirty four").

    The rule reports the digits alone. The street name and its suffix
    are ordinary words that the caller leaves where they stand, and
    capitalizing them is rule 8's work in phase 5.
    """
    if not _street_suffix_near(records, end):
        return None
    words = _value_words(run)
    if words is None:
        return None
    values = _parse_cardinal(words)
    if values is None or len(values) != 2:
        return None
    # The guard above is what makes these the whole run: a run reading
    # as ONE number is a cardinal every earlier rule already had its
    # chance at, and three chunks are not a house number anyone says.
    leading, trailing = values[0], values[-1]
    if not 1 <= leading <= 99:
        return None
    if not 10 <= trailing <= 99:
        return None
    return f"{leading}{trailing}"


def _ordinal_suffix(day: int) -> str:
    """The two letters a written ordinal ends with."""
    if day % 100 in (11, 12, 13):
        return "th"
    return _ITN_ORDINAL_SUFFIXES.get(day % 10, "th")


def _hyphenated_ordinal(low: str) -> int | None:
    """The day a ONE-token hyphenated ordinal stands for, or None.

    A recognizer that hands this module "twenty-five" as one token hands
    it "twenty-fifth" the same way -- both are tens-plus-unit compounds
    -- and phase 4 read only the first of them: _hyphen_pieces admits
    number words alone, an ordinal word is a separate vocabulary, so the
    compound reached _read_ordinal whole and no table held it
    (wh-itn-phase4-review.1).

    Reading the compound here instead of widening that split is what
    keeps it inside rule 7, the only rule that may read an ordinal at
    all: the caller supplies the month anchor and the day cap, so a
    hyphenated ordinal converts exactly where its two-token form
    converts, and a bare "twenty-fifth" stays a word.

    The head must be a tens word and the tail a unit ordinal, which is
    the pair _read_ordinal already reads across two tokens. Everything
    else is None: "one-third" is an ordinary fraction and
    "twenty-five-year-old" an adjective, and neither is a day. A token
    with no hyphen partitions to an empty tail, which no ordinal table
    holds, so the lookups below refuse it without a separate check.
    """
    head, _, tail = low.partition("-")
    tens = _ITN_TENS.get(head)
    unit = _ITN_ORDINAL_UNITS.get(tail)
    if tens is None or unit is None:
        return None
    return tens + unit


def _read_ordinal(records: list[_Token], start: int) -> tuple[int, int] | None:
    """The day an ordinal phrase at `start` stands for, and the index
    after it.

    "third", "twelfth" and "twentieth" are one token. "twenty third" is
    two, and only the second of them is an ordinal: the tens word in
    front is the ordinary number word _ITN_TENS already holds, so the
    unit behind it is what makes the phrase an ordinal at all.
    "twenty-fifth" is that same pair inside ONE token, which
    _hyphenated_ordinal reads.
    """
    low = records[start].low
    for table in (_ITN_ORDINAL_UNITS, _ITN_ORDINAL_TEENS, _ITN_ORDINAL_TENS):
        if low in table:
            return table[low], start + 1
    compound = _hyphenated_ordinal(low)
    if compound is not None:
        return compound, start + 1
    tens = _ITN_TENS.get(low)
    if tens is None or not _adjacent(records, start + 1):
        return None
    unit = _ITN_ORDINAL_UNITS.get(records[start + 1].low)
    if unit is None:
        return None
    return tens + unit, start + 2


def _date_range_end(records: list[_Token], end: int) -> int | None:
    """The index after the SECOND day of a date range whose first day
    ends at `end`, or None when no range stands there.

    One hop only. _date_chain_end is what walks the rest, and the caller
    reads its answer rather than this one -- a list of days is a chain of
    these pairs, not a single pair (wh-itn-phase4-review.4).

    The caller refuses the range, and a refusal needs the end of what it
    refused: the whole span reaches _absorbed_refusal_end from here, so
    the number phrase touching the range is absorbed with it
    (wh-itn-phase4-review.3). Answering `True` left the caller with the
    FIRST day's endpoint, which is not the span it was refusing.

    "january third and fourth" is one month anchor over two days. Rule 7
    anchors only the ordinal directly behind the month, so the first day
    converted and the second stayed a word: "january 3rd and fourth"
    (wh-itn-phase4-review.2). David ruled on 2026-08-25 -- option 1 of
    the two he was offered -- that the whole span stays words, which is
    the order this module already sorts outcomes by: a mixed output is a
    half conversion, and words are always safe.

    The shape is narrow, the way _connector_refuses is narrow, and it
    asks the same two questions that one opens with -- deliberately, in
    its own words, because the two functions read different grammars and
    only one of them may end a date. The "and" must stand against the
    ordinal with no punctuation between them, and an ordinal must stand
    against the "and". An "and" in front of anything else is ordinary
    speech whose date still converts ("january third and we left"), and
    a second month reads its own date ("january first and february
    second").
    """
    if not _adjacent(records, end) or records[end].low != "and":
        return None
    if not _adjacent(records, end + 1):
        return None
    read = _read_ordinal(records, end + 1)
    return None if read is None else read[1]


def _date_chain_end(records: list[_Token], end: int) -> int:
    """`end` carried past EVERY adjacent "and <ordinal>" pair.

    A spoken list of days is not two days. "january third and fourth and
    fifth" is one month anchor over three, and each further day arrives
    as another _date_range_end pair, so the endpoint of the whole list is
    that pair read until it stops. Reading it once left the third day
    outside the refused span, the scanner resumed on it, and the number
    phrase behind it converted -- "january third and fourth and fifth $5"
    -- which is the half conversion review.3 closed for the two-day form
    and review.4 reported again for three (wh-itn-phase4-review.4).

    The walk owns no boundary of its own: every hop is _date_range_end's
    own shape, so punctuation, an ordinary word and a second month end
    the chain exactly where they end a single pair. This is also why the
    fix is not ordinal words in _number_ish -- that vocabulary is read by
    _absorbed_refusal_end for EVERY refusal in the module, and a bare
    ordinal would then widen spans that have nothing to do with a date.

    Returns `end` itself when nothing continues, which is what makes
    `chain_end != end` the caller's test for "a range stands here": a
    chain that walked past the first day IS the range, and no separate
    question needs asking.
    """
    index = end
    while True:
        hop = _date_range_end(records, index)
        if hop is None:
            return index
        index = hop


def _try_date_ordinal(
    records: list[_Token], start: int
) -> tuple[str | None, int] | None:
    """The written ordinal of a date beginning at `start`, and the index
    after it (spec rule 7).

    Three answers, not two, and the third is the point of the pair this
    returns. `None` means no date started here at all, and the caller
    hands the tokens to the number rules. A pair whose first item is a
    STRING is the converted day. A pair whose first item is None is a
    REFUSAL -- this rule read a date and declined it -- and the index
    beside it is how far the refusal reaches, exactly as
    _match_number_phrase reports its own (wh-itn-phase4-review.3).

    A month name standing directly in front is the unit anchor, the same
    role "dollars" plays for money: it is what lets a single spoken word
    convert without breaking the command-safety rule, so a bare "third"
    and a "press third" stay words. Punctuation detaches the anchor the
    way it detaches every other one ("january, third"). Three month
    names are not in _ITN_MONTHS at all -- see the crewcut beside it.

    Only the ordinal is rewritten. The month keeps its own token and its
    own spacing, and phase 5 (rule 8) is what capitalizes it -- this
    stage writes "january 3rd".

    The two refusals are a day outside 1-31 and a date RANGE: a day it
    could convert, standing in front of "and" and a second day it has no
    anchor for, is one span this module cannot write, so the span stays
    words entirely. Each one ends at _absorbed_refusal_end, because a
    refused span absorbs the number phrase touching it under the
    module-wide policy, and rule 7's refusals are refusals like any
    other. Returning a bare None here instead made the caller rescan the
    words behind the refused span, and "january third and fourth five
    dollars" typed "... $5" beside a date the module had just declined
    to write.

    BOTH refusals start from _date_chain_end, one line above the day cap
    so neither can skip it. The cap used to answer before the range was
    read at all, and the range read one day past the first, so a list of
    three days -- or of two with a rejected day at its head -- ended its
    refusal inside itself and the tail converted (wh-itn-phase4-review.4).
    The chain endpoint is the whole list either way, and the day-cap exit
    needs it just as much: the ordinal it rejected can carry a list
    behind it. A day the cap accepts converts only when the chain went
    nowhere, which is the `chain_end != end` test.
    """
    if not _adjacent(records, start):
        return None
    if records[start - 1].low not in _ITN_MONTHS:
        return None
    read = _read_ordinal(records, start)
    if read is None:
        return None
    day, end = read
    chain_end = _date_chain_end(records, end)
    if not 1 <= day <= _ITN_MAX_DAY:
        return None, _absorbed_refusal_end(records, chain_end)
    if chain_end != end:
        return None, _absorbed_refusal_end(records, chain_end)
    return f"{day}{_ordinal_suffix(day)}", end


def _match_number_phrase(
    records: list[_Token], start: int
) -> tuple[str | None, int]:
    """The written form for the phrase at `start`, and the index after
    the tokens it consumed.

    A refusal still reports the end of the whole phrase. Rescanning one
    token deeper would convert a piece of an ambiguous phrase -- the
    tail of "twelve thirty four" reads as 34 on its own -- and the point
    of the refusal is that the phrase as a whole is ambiguous. Every
    refusal below therefore ends at _absorbed_refusal_end, which is the
    one answer for how far a refused span reaches -- the fall-through at
    the bottom included, because a run no rule could read is as ambiguous
    as one a rule read and declined (wh-shared-itn-review.16).

    The empty run below is the one exit that is NOT a refusal: no phrase
    started at this token, so the scanner steps over it singly. That is
    what keeps "oh five pm" reading as "oh 5 PM".
    """
    run, end = _collect_run(records, start)
    if not run:
        return None, start + 1
    # The whole over-long run is refused here, not inside a rule:
    # refusing it deeper would let the scanner resume inside the run and
    # convert a 64-word piece of it (wh-shared-itn-review.7).
    if not _within_length_cap(run):
        return None, _absorbed_refusal_end(records, end)
    anchor = end if _adjacent(records, end) else None
    if anchor is not None:
        for rule in (_try_money, _try_clock_time, _try_decimal):
            anchored = rule(records, run, anchor)
            if anchored is not None:
                return anchored
        # An anchored rule that claimed this anchor and then refused
        # keeps the whole phrase as words -- see _anchor_refuses.
        if _anchor_refuses(records, anchor):
            return None, _absorbed_refusal_end(records, end)
    # The connector is judged only after the anchored rules, because
    # they read their own conjunctions first: "one thousand and one
    # dollars" is $1001 and "five dollars and seventeen cents" is
    # $5.17. What is left is an "and" no rule claimed, which is the one
    # that splits an amount in two (wh-shared-itn-review.13).
    if _connector_refuses(records, end):
        return None, _absorbed_refusal_end(records, end)
    digits = _try_digit_sequence(run)
    if digits is not None:
        return digits, end
    cardinal = _try_cardinal(records, run, end)
    if cardinal is not None:
        return cardinal, end
    # Rule 6 is last on purpose (boss ruling, 2026-08-25). Every rule
    # above has already declined this run, so the paired house number
    # can only convert a phrase that would otherwise stay words, and it
    # cannot reorder or shadow a conversion that already works.
    house = _try_house_number(records, run, end)
    if house is not None:
        return house, end
    # Every rule declined the run, so the phrase is ambiguous and this
    # is a refusal like the ones above -- it absorbs what stands against
    # it. Ending at the run instead let the scanner resume on the words
    # behind it: "twelve thirty" is a run _collect_run will not admit an
    # "oh" into, so the ambiguous head was skipped, the "oh" was skipped,
    # and "five pm" converted alone as "twelve thirty oh 5 PM"
    # (wh-shared-itn-review.16).
    return None, _absorbed_refusal_end(records, end)


def apply_itn(text: str) -> str:
    """Rewrite spoken number phrases as the written forms users expect.

    - Cardinals: 'fifty nine' -> '59', 'eight thousand' -> '8000'
    - Thousands separators from five digits up: 'fifteen thousand' ->
      '15,000', 'one million dollars' -> '$1,000,000'
    - Money: 'fifty nine dollars and seventeen cents' -> '$59.17'
    - Clock times, AM/PM anchored: 'eleven fifteen PM' -> '11:15 PM'
    - Decimals: 'three point five' -> '3.5'
    - Digit sequences, 'oh' as zero: 'nine oh two one oh' -> '90210'
    - House numbers, street-suffix anchored: 'twelve thirty four main
      street' -> '1234 main street'
    - Dates, month anchored: 'january third' -> 'january 3rd'

    A lone number word never converts (see the command-safety note
    above). Every other character, including whitespace, is returned
    unchanged.
    """
    if not text:
        return ""

    words, separators, records = _tokenize(text)

    pieces: list[str] = []
    index = 0
    while index < len(records):
        # Rule 7 is read before the number rules because an ordinal is
        # not a number word: "march twenty third" reaches the scanner as
        # the tens word "twenty" plus a word no rule reads, and only the
        # month anchor makes the two of them one day. Only an outright
        # None reaches the number rules: a date the rule read and refused
        # answers with a pair of its own, and the refusal branch below
        # emits its span as words (wh-itn-phase4-review.3).
        dated = _try_date_ordinal(records, index)
        if dated is not None:
            replacement, end = dated
        else:
            replacement, end = _match_number_phrase(records, index)
        if replacement is None:
            for skipped in range(index, end):
                pieces.append(words[skipped] + separators[skipped])
            index = end
            continue
        pieces.append(
            records[index].lead
            + replacement
            + records[end - 1].trail
            + separators[end - 1]
        )
        index = end
    return "".join(pieces)


def agreement_prefix_len(prev_words: list[str], current_words: list[str]) -> int:
    """Length of the shared word prefix between two decoder outputs.

    LocalAgreement-2 confirms the words two consecutive decodes agree on;
    this is the agreement measurement, engines keep the promotion policy.
    """
    lcp_len = 0
    for i in range(min(len(prev_words), len(current_words))):
        if prev_words[i] == current_words[i]:
            lcp_len = i + 1
        else:
            break
    return lcp_len
