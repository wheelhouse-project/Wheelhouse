"""Unit tests for speech/number_word_parser.py (wh-n29v Phase 1.5).

Covers bare digits, cardinal words 1..19, tens, hyphenated and
space-separated tens+units compounds, the 1..999 hundreds compounds,
case/whitespace tolerance, and the explicit None paths (ordinals,
out-of-range, empty/None/garbage).
"""

import pytest

from speech.number_word_parser import parse_number_word


class TestBareDigits:
    @pytest.mark.parametrize(
        "text,expected",
        [("1", 1), ("7", 7), ("23", 23), ("99", 99)],
    )
    def test_bare_digits_in_range(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [("100", 100), ("250", 250), ("999", 999)],
    )
    def test_bare_three_digit_in_range(self, text, expected):
        assert parse_number_word(text) == expected


class TestCardinalWords:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("one", 1),
            ("two", 2),
            ("nine", 9),
            ("ten", 10),
            ("eleven", 11),
            ("fifteen", 15),
            ("nineteen", 19),
        ],
    )
    def test_units_and_teens(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("twenty", 20),
            ("thirty", 30),
            ("forty", 40),
            ("fifty", 50),
            ("sixty", 60),
            ("seventy", 70),
            ("eighty", 80),
            ("ninety", 90),
        ],
    )
    def test_tens(self, text, expected):
        assert parse_number_word(text) == expected


class TestCompoundTensUnits:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("twenty-three", 23),
            ("thirty-seven", 37),
            ("forty-two", 42),
            ("ninety-nine", 99),
        ],
    )
    def test_hyphenated(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("twenty three", 23),
            ("thirty seven", 37),
            ("ninety nine", 99),
        ],
    )
    def test_space_separated(self, text, expected):
        assert parse_number_word(text) == expected


class TestHundredsCompounds:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("one hundred", 100),
            ("two hundred", 200),
            ("nine hundred", 900),
            ("one hundred twenty-three", 123),
            ("one hundred and twenty three", 123),
            ("five hundred sixty-seven", 567),
            ("nine hundred ninety-nine", 999),
            ("three hundred five", 305),
            # wh-n29v.8.1: hundreds + 'and' + single unit word.
            ("one hundred and one", 101),
            # wh-n29v.8.2: hundreds + tens-only remainder, with and without 'and'.
            ("one hundred twenty", 120),
            ("one hundred and twenty", 120),
        ],
    )
    def test_hundreds(self, text, expected):
        assert parse_number_word(text) == expected


class TestCaseAndWhitespace:
    def test_case_insensitive_with_surrounding_whitespace(self):
        assert parse_number_word("  Twenty-Three ") == 23

    def test_collapses_repeated_internal_whitespace(self):
        assert parse_number_word("thirty   seven") == 37

    def test_upper_case_word(self):
        assert parse_number_word("NINETY") == 90


class TestOrdinalsRejected:
    @pytest.mark.parametrize(
        "text",
        ["first", "second", "third", "twenty-third", "1st", "2nd", "3rd"],
    )
    def test_ordinals_return_none(self, text):
        assert parse_number_word(text) is None


class TestOutOfRange:
    @pytest.mark.parametrize("text", ["0", "-1", "1000", "12345"])
    def test_out_of_range_digits_return_none(self, text):
        assert parse_number_word(text) is None

    def test_zero_word_returns_none(self):
        assert parse_number_word("zero") is None

    def test_thousand_word_returns_none(self):
        assert parse_number_word("one thousand") is None


class TestUnresolvable:
    @pytest.mark.parametrize("text", [None, "", "   ", "banana", "twenty-twenty"])
    def test_garbage_returns_none(self, text):
        assert parse_number_word(text) is None

    def test_double_tens_returns_none(self):
        assert parse_number_word("twenty thirty") is None

    def test_trailing_unit_word_returns_none(self):
        assert parse_number_word("twenty seven words") is None


class TestColloquialHundredsPairing:
    """wh-click-number-dictation: '<unit> <10..99 remainder>' pairing.

    Users read three-digit badge numbers aloud as 'one twelve' (112), the
    colloquial American hundreds form without the word 'hundred'. The
    first token must be a unit 1..9 and the remainder must resolve to
    10..99; a remainder of 1..9 is not a pairing -- that shape belongs to
    digit-by-digit reading (TestDigitByDigitReading).
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("one twelve", 112),
            ("one ten", 110),
            ("one nineteen", 119),
            ("one twenty", 120),
            ("one twenty three", 123),
            ("one twenty-three", 123),
            ("three twenty", 320),
            ("two ninety nine", 299),
            ("nine ninety nine", 999),
            ("number one twelve", 112),
        ],
    )
    def test_pairing_resolves(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "twelve twenty",  # head must be a unit 1..9, not a teen
            "ten twenty",     # head must be a unit 1..9
            "twenty twelve",  # head must be a unit, not a tens multiple
            "one twenty three four",  # trailing token
        ],
    )
    def test_non_pairing_shapes_return_none(self, text):
        assert parse_number_word(text) is None


class TestDigitByDigitReading:
    """wh-click-number-dictation: digit-by-digit badge-number reading.

    Users read a badge number one digit at a time: 'one seven' -> 17,
    'one zero five' -> 105. Every token must be a single digit word
    ('oh' counts as zero), two or three tokens total, and the leading
    digit must be 1..9 because badge numbers never start with zero.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("one seven", 17),
            ("one two", 12),
            ("four two", 42),
            ("one one two", 112),
            ("one two three", 123),
            ("nine nine nine", 999),
            ("one zero five", 105),
            ("one oh five", 105),
            ("seven zero", 70),
            ("one oh", 10),
            ("number one seven", 17),
        ],
    )
    def test_digit_by_digit_resolves(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "zero seven",          # leading zero
            "oh seven",            # leading oh
            "zero zero seven",     # leading zero, three digits
            "one two three four",  # four digits exceeds 999
            "oh",                  # 'oh' alone is not a number
            "one oh oh oh",        # four tokens
        ],
    )
    def test_non_digit_shapes_return_none(self, text):
        assert parse_number_word(text) is None


class TestUnicodeDigits:
    # wh-n29v.6.1: str.isdigit() is True for many non-ASCII forms where int()
    # either raises (superscripts, circled) or silently yields a value the user
    # never typed (fullwidth, Arabic-Indic). The parser must never raise and
    # must return None for these unresolvable forms.
    @pytest.mark.parametrize(
        "text",
        [
            "²",   # superscript two
            "³",   # superscript three
            "⁵",   # superscript five
            "②",   # circled digit two
            "５",   # fullwidth five
            "２３",  # fullwidth "23"
            "٥",   # Arabic-Indic five
            "०",   # Devanagari zero
            "one ２",    # ascii word + fullwidth digit token
        ],
    )
    def test_non_ascii_digits_return_none_without_raising(self, text):
        assert parse_number_word(text) is None


class TestSignedRejected:
    # wh-n29v.6.2: a leading sign is unsupported for digits AND words, and
    # '+'/'-' must be symmetric. Internal hyphens (tens+units) are unaffected.
    @pytest.mark.parametrize(
        "text",
        [
            "-1", "+5", "-99", "+250",
            "-twenty", "+twenty",
            "-one", "+one",
            "-one hundred", "+one hundred",
            "-twenty-three", "+twenty-three",
        ],
    )
    def test_leading_sign_returns_none(self, text):
        assert parse_number_word(text) is None

    def test_internal_hyphen_still_resolves(self):
        # The leading-sign guard must not break the legitimate internal hyphen.
        assert parse_number_word("twenty-three") == 23
        assert parse_number_word("one hundred twenty-three") == 123


class TestRejectBranchesRegression:
    # wh-n29v.6.3: lock in the implemented-but-previously-unasserted reject
    # branches so a future refactor cannot silently start returning a wrong
    # non-None int (which would mis-click a different control).
    @pytest.mark.parametrize(
        "text",
        [
            "hundred",            # bare 'hundred', no head unit
            "hundred five",       # 'hundred' first, no head unit
            "eleven hundred",     # head is a teen, not a 1..9 unit
            "twenty hundred",     # head is a tens word, not a 1..9 unit
            "one hundred two hundred",  # two hundreds components
            "one hundred banana",       # unrecognized remainder
            "one hundred twenty thirty",  # invalid below-hundred remainder
            "one hundred and and five",   # doubled connective
            "2 twenty",           # ascii digit mixed into a word phrase
            "twenty 3",           # word + ascii digit
            "one hundred 5",      # hundreds head + digit remainder
        ],
    )
    def test_invalid_compositions_return_none(self, text):
        assert parse_number_word(text) is None

    def test_hundred_with_and_connective_still_resolves(self):
        # The 'and' connective is valid only immediately after 'hundred'.
        assert parse_number_word("one hundred and five") == 105
        assert parse_number_word("three hundred and twenty-one") == 321


class TestLargeDigitStrings:
    # wh-n29v.7.1: int() on a token longer than Python's integer-string
    # conversion limit raises ValueError before the range check. The parser
    # must never raise; a too-long digit string is out of range -> None.
    def test_huge_digit_string_returns_none_without_raising(self):
        assert parse_number_word("9" * 4301) is None
        assert parse_number_word("1" * 100) is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("007", 7),      # leading zeros stripped, still resolves
            ("0007", 7),     # more leading zeros than the digit bound
            ("0099", 99),
            ("0999", 999),
            ("00", None),    # all zeros -> 0 -> out of range
            ("0", None),
            ("1000", None),  # 4 significant digits -> out of range
            ("12345", None),
        ],
    )
    def test_leading_zero_and_bound(self, text, expected):
        assert parse_number_word(text) == expected


class TestDanglingHundredConnective:
    # wh-n29v.7.2: a 'hundred' followed by a dangling 'and' (no number after)
    # is an incomplete phrase and must return None, not the bare hundreds value.
    @pytest.mark.parametrize("text", ["one hundred and", "three hundred and", "nine hundred and"])
    def test_dangling_and_returns_none(self, text):
        assert parse_number_word(text) is None

    def test_bare_hundred_without_and_still_resolves(self):
        assert parse_number_word("one hundred") == 100
        assert parse_number_word("three hundred") == 300


class TestMalformedHyphens:
    # wh-n29v.7.3: a hyphen is valid only as a single in-word separator. A
    # standalone, repeated, or whitespace-adjacent hyphen is malformed and
    # must return None rather than silently collapsing to a value.
    @pytest.mark.parametrize(
        "text",
        [
            "twenty - three",     # space-before-and-after hyphen
            "twenty- three",      # wh-n29v.8.3: space-after-hyphen variant
            "twenty --three",     # space-before-doubled-hyphen variant
            "twenty--three",      # doubled hyphen
            "one hundred - five",  # space-adjacent hyphen in a hundreds phrase
            "twenty-",            # trailing hyphen
            "- twenty",           # leading hyphen with space (also a sign)
            "twenty - ",          # trailing hyphen with spaces
        ],
    )
    def test_malformed_hyphens_return_none(self, text):
        assert parse_number_word(text) is None

    def test_valid_in_word_hyphen_still_resolves(self):
        assert parse_number_word("twenty-three") == 23
        assert parse_number_word("one hundred twenty-three") == 123


class TestLeadingNumberToken:
    # wh-click-number-dictation: users naturally say "click number three"
    # when badges are on screen, so the captured text arriving here is
    # "number three". A single leading "number"/"numbers" token is dropped
    # before parsing; everything after it must follow the existing grammar.
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("number one", 1),
            ("number seven", 7),
            ("number 75", 75),
            ("numbers seven", 7),
            ("numbers 12", 12),
            ("number twenty-three", 23),
            ("Number 7", 7),
            ("NUMBER ninety", 90),
            ("number one hundred and five", 105),
        ],
    )
    def test_leading_number_token_is_dropped(self, text, expected):
        assert parse_number_word(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "number",             # bare filler, no count -> by-name fallback
            "numbers",
            "number number one",  # only ONE leading token is dropped
            "one number",         # the token is a prefix, never a suffix
            "number first",       # ordinals still rejected after the drop
            "number zero",        # range rules unchanged after the drop
            "number banana",
        ],
    )
    def test_number_token_edge_cases_return_none(self, text):
        assert parse_number_word(text) is None
