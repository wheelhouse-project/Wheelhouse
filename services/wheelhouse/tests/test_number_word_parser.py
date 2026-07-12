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

    def test_units_only_compound_returns_none(self):
        # "three twenty" is not a valid tens+units composition.
        assert parse_number_word("three twenty") is None

    def test_double_tens_returns_none(self):
        assert parse_number_word("twenty thirty") is None

    def test_trailing_unit_word_returns_none(self):
        assert parse_number_word("twenty seven words") is None


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
