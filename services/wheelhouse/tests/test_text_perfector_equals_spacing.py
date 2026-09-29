"""TextPerfector spacing around a dictated equal sign.

wh-voice-access-parity.1.15.5: David dictated "x equals sine y" and
WheelHouse typed "x= y". The punct-equal-sign pattern inserts "=" by
itself, and TextPerfector gives a punctuation-only insertion no leading
space, so the "=" joined the word before it. David chose "x = y"
(option 1, 2026-09-26 09:39): the "=" gets a leading space by the same
rule as a word. The next word already gets its own leading space.

David's answer to Question 7 (option 1, 2026-09-26 11:37) extends the
rule to "+", "<", ">" and "|". "*" and "-" stay joined, and so does
every other punctuation-only insertion.

Boss e8 ruling 11:47: a spaced symbol gets no leading space when the text
before the cursor ends with a spaced symbol, so symbols dictated one after
another build an operator ("<=", "||", ">>", "==") instead of "< =".
Ruling 11:49 extends this to text that ends with one of ! * - % ^ & : ~ ?,
so "!=", "-=", "*=" and ":=" type as they did before the "=" change. "-" is
also a hyphen, so "well-" followed by "=" types "well-="; the bead records
that as accepted.
"""
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(Path(__file__).parent.parent))

from ui.text_perfector import TextPerfector  # noqa: E402


@pytest.fixture
def perfector():
    return TextPerfector()


class TestEqualSignSpacing:
    def test_equal_sign_after_a_word_gets_a_leading_space(self, perfector):
        assert perfector.perfected_string("=", preceding_chars="x") == " ="

    def test_word_after_the_equal_sign_gets_a_leading_space(self, perfector):
        assert perfector.perfected_string("y", preceding_chars="x =") == " y"

    def test_equal_sign_after_a_space_gets_no_second_space(self, perfector):
        assert perfector.perfected_string("=", preceding_chars="x ") == "="

    def test_equal_sign_in_an_empty_field_gets_no_space(self, perfector):
        assert perfector.perfected_string("=", preceding_chars="") == "="

    def test_equal_sign_after_an_opening_bracket_gets_no_space(self, perfector):
        assert perfector.perfected_string("=", preceding_chars="(") == "="

    def test_equal_sign_over_a_selection_gets_no_space(self, perfector):
        assert perfector.perfected_string(
            "=", preceding_chars="x", has_selection=True
        ) == "="


SPACED = [
    pytest.param("+", id="plus"),
    pytest.param("<", id="less"),
    pytest.param(">", id="greater"),
    pytest.param("|", id="bar"),
]


class TestOtherSpacedSymbols:
    """"+", "<", ">" and "|" follow the same rules as "="."""

    @pytest.mark.parametrize("symbol", SPACED)
    def test_spaced_symbol_after_a_word_gets_a_leading_space(self, perfector, symbol):
        assert perfector.perfected_string(symbol, preceding_chars="a") == " " + symbol

    @pytest.mark.parametrize("symbol", SPACED)
    def test_word_after_a_spaced_symbol_gets_a_leading_space(self, perfector, symbol):
        assert perfector.perfected_string("b", preceding_chars="a " + symbol) == " b"

    @pytest.mark.parametrize("symbol", SPACED)
    def test_spaced_symbol_after_a_space_gets_no_second_space(self, perfector, symbol):
        assert perfector.perfected_string(symbol, preceding_chars="a ") == symbol

    @pytest.mark.parametrize("symbol", SPACED)
    def test_spaced_symbol_in_an_empty_field_gets_no_space(self, perfector, symbol):
        assert perfector.perfected_string(symbol, preceding_chars="") == symbol

    @pytest.mark.parametrize("symbol", SPACED)
    def test_spaced_symbol_after_an_opening_bracket_gets_no_space(self, perfector, symbol):
        assert perfector.perfected_string(symbol, preceding_chars="(") == symbol

    @pytest.mark.parametrize("symbol", SPACED)
    def test_spaced_symbol_over_a_selection_gets_no_space(self, perfector, symbol):
        assert perfector.perfected_string(
            symbol, preceding_chars="a", has_selection=True
        ) == symbol


class TestSpacedSymbolsBuildOperators:
    """A spaced symbol right after another spaced symbol joins it."""

    @pytest.mark.parametrize("before, symbol", [
        pytest.param("a <", "=", id="less-equal"),
        pytest.param("a >", "=", id="greater-equal"),
        pytest.param("a =", "=", id="equal-equal"),
        pytest.param("a |", "|", id="bar-bar"),
        pytest.param("a >", ">", id="greater-greater"),
        pytest.param("a <", "<", id="less-less"),
        pytest.param("a +", "+", id="plus-plus"),
        pytest.param("a +", "=", id="plus-equal"),
        pytest.param("a =", ">", id="equal-greater"),
    ])
    def test_spaced_symbol_after_a_spaced_symbol_gets_no_space(
        self, perfector, before, symbol
    ):
        assert perfector.perfected_string(symbol, preceding_chars=before) == symbol

    def test_word_after_a_built_operator_gets_a_leading_space(self, perfector):
        assert perfector.perfected_string("b", preceding_chars="a ||") == " b"

    @pytest.mark.parametrize("before", [
        pytest.param("a !", id="bang"),
        pytest.param("a *", id="star"),
        pytest.param("a -", id="minus"),
        pytest.param("a %", id="percent"),
        pytest.param("a ^", id="caret"),
        pytest.param("a &", id="ampersand"),
        pytest.param("a :", id="colon"),
        pytest.param("a ~", id="tilde"),
        pytest.param("a ?", id="question"),
    ])
    def test_equal_sign_after_an_operator_character_gets_no_space(
        self, perfector, before
    ):
        assert perfector.perfected_string("=", preceding_chars=before) == "="

    def test_spaced_symbol_after_an_operator_character_gets_no_space(self, perfector):
        assert perfector.perfected_string(">", preceding_chars="a -") == ">"

    @pytest.mark.parametrize("before", [
        pytest.param("f(x)", id="closing-paren"),
        pytest.param("a[0]", id="closing-bracket"),
        pytest.param('"a"', id="closing-quote"),
        pytest.param("a.", id="period"),
        pytest.param("a,", id="comma"),
    ])
    def test_equal_sign_after_other_punctuation_gets_a_leading_space(
        self, perfector, before
    ):
        assert perfector.perfected_string("=", preceding_chars=before) == " ="


class TestOtherPunctuationUnchanged:
    @pytest.mark.parametrize("symbol", [
        pytest.param(",", id="comma"),
        pytest.param("-", id="minus"),
        pytest.param("*", id="star"),
        pytest.param("==", id="double-equal"),
        pytest.param("!=", id="not-equal"),
    ])
    def test_other_punctuation_after_a_word_gets_no_space(self, perfector, symbol):
        assert perfector.perfected_string(symbol, preceding_chars="x") == symbol


ALL_SPACED = [pytest.param("=", id="equal"), *SPACED]


class TestAPatternOutputWithItsOwnSpace:
    """A personal text pattern can save an output such as " = " that carries
    its own leading space. Before wh-voice-access-parity.1.15.5 that output
    counted as punctuation only and was typed as saved; the spaced-symbol
    rule then added a second space (wh-voice-access-parity.1.15.6.3)."""

    @pytest.mark.parametrize("symbol", ALL_SPACED)
    def test_a_leading_space_in_the_output_is_not_doubled(self, perfector, symbol):
        assert perfector.perfected_string(
            f" {symbol} ", preceding_chars="x"
        ) == f" {symbol} "

    @pytest.mark.parametrize("symbol", ALL_SPACED)
    def test_a_trailing_space_alone_still_gets_the_leading_space(
        self, perfector, symbol
    ):
        assert perfector.perfected_string(
            f"{symbol} ", preceding_chars="x"
        ) == f" {symbol} "


class TestTheHelpNamesTheOperatorCharacters:
    """The notes of the five spaced symbols said "right after another
    symbol", but a period, a comma, a closing bracket or a quote before the
    symbol still gets a space. Each note now lists OPERATOR_CHARACTERS
    (wh-voice-access-parity.1.15.6.4)."""

    @pytest.mark.parametrize("row_id", [
        "punct-equal-sign", "punct-plus-sign", "punct-vertical-bar",
        "punct-less-than-sign", "punct-greater-than-sign",
    ])
    def test_the_note_lists_the_operator_characters(self, row_id):
        import tomllib
        from ui.text_perfector import OPERATOR_CHARACTERS
        path = (Path(__file__).resolve().parents[1] / "knowledge" / "helpdoc"
                / "command_descriptions.toml")
        if not path.is_file():
            pytest.skip("development-only help-document sources are absent from this checkout")
        rows = tomllib.loads(path.read_text(encoding="utf-8"))["command"]
        (row,) = [r for r in rows if row_id in r["ids"]]
        listed = "right after one of = + < > | ! * - % ^ & : ~ ?,"
        assert listed in row["notes"]
        assert set(listed[len("right after one of "):-1].split()) == set(
            OPERATOR_CHARACTERS
        )
        assert "another symbol" not in row["notes"]
