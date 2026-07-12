"""Unit tests for SelectionTransformer - case conversion and wrapping.

These tests verify the pure logic functions for text transformations.
No UI dependencies.
"""
import pytest
from services.wheelhouse.ui.selection_transformer import SelectionTransformer


class TestSelectionTransformer:
    """Test suite for SelectionTransformer class."""

    @pytest.fixture
    def transformer(self):
        """Create a SelectionTransformer instance for testing."""
        return SelectionTransformer()

    # ========================================================================
    # WRAPPING TESTS
    # ========================================================================

    def test_quote_wrapping(self, transformer):
        """Double quotes should wrap text."""
        result = transformer.apply_transformation("hello", "quote")
        assert result == '"hello"'

    def test_single_quote_wrapping(self, transformer):
        """Single quotes should wrap text."""
        result = transformer.apply_transformation("hello", "single_quote")
        assert result == "'hello'"

    def test_bracket_wrapping(self, transformer):
        """Square brackets should wrap text."""
        result = transformer.apply_transformation("hello", "bracket")
        assert result == "[hello]"

    def test_parenthesis_wrapping(self, transformer):
        """Parentheses should wrap text."""
        result = transformer.apply_transformation("hello", "parenthesis")
        assert result == "(hello)"

    def test_angle_bracket_wrapping(self, transformer):
        """Angle brackets should wrap text."""
        result = transformer.apply_transformation("hello", "angle_bracket")
        assert result == "<hello>"

    def test_curly_bracket_wrapping(self, transformer):
        """Curly braces should wrap text."""
        result = transformer.apply_transformation("hello", "curly_bracket")
        assert result == "{hello}"

    # ========================================================================
    # SIMPLE CASE TESTS
    # ========================================================================

    def test_uppercase(self, transformer):
        """Uppercase should convert all characters."""
        result = transformer.apply_transformation("hello world", "uppercase")
        assert result == "HELLO WORLD"

    def test_lowercase(self, transformer):
        """Lowercase should convert all characters."""
        result = transformer.apply_transformation("HELLO WORLD", "lowercase")
        assert result == "hello world"

    def test_capitalize(self, transformer):
        """Capitalize should capitalize first letter only."""
        result = transformer.apply_transformation("hello world", "capitalize")
        assert result == "Hello world"

    def test_title_case(self, transformer):
        """Title case should capitalize each word."""
        result = transformer.apply_transformation("hello world test", "title_case")
        assert result == "Hello World Test"

    # ========================================================================
    # PROGRAMMING CASE TESTS
    # ========================================================================

    def test_snake_case_two_words(self, transformer):
        """Snake case should join with underscores."""
        result = transformer.apply_transformation("hello world", "snake_case")
        assert result == "hello_world"

    def test_snake_case_three_words(self, transformer):
        """Snake case should handle multiple words."""
        result = transformer.apply_transformation("hello world test", "snake_case")
        assert result == "hello_world_test"

    def test_camel_case_two_words(self, transformer):
        """Camel case should lowercase first, capitalize rest."""
        result = transformer.apply_transformation("hello world", "camel_case")
        assert result == "helloWorld"

    def test_camel_case_three_words(self, transformer):
        """Camel case should handle multiple words."""
        result = transformer.apply_transformation("hello world test", "camel_case")
        assert result == "helloWorldTest"

    def test_camel_case_single_word(self, transformer):
        """Camel case with single word should just lowercase it."""
        result = transformer.apply_transformation("Hello", "camel_case")
        assert result == "hello"

    def test_pascal_case_two_words(self, transformer):
        """Pascal case should capitalize all words."""
        result = transformer.apply_transformation("hello world", "pascal_case")
        assert result == "HelloWorld"

    def test_pascal_case_three_words(self, transformer):
        """Pascal case should handle multiple words."""
        result = transformer.apply_transformation("hello world test", "pascal_case")
        assert result == "HelloWorldTest"

    def test_kebab_case_two_words(self, transformer):
        """Kebab case should join with hyphens."""
        result = transformer.apply_transformation("hello world", "kebab_case")
        assert result == "hello-world"

    def test_kebab_case_three_words(self, transformer):
        """Kebab case should handle multiple words."""
        result = transformer.apply_transformation("hello world test", "kebab_case")
        assert result == "hello-world-test"

    # ========================================================================
    # EDGE CASES
    # ========================================================================

    def test_empty_string(self, transformer):
        """Empty string should remain empty."""
        result = transformer.apply_transformation("", "snake_case")
        assert result == ""

        result = transformer.apply_transformation("", "camel_case")
        assert result == ""

    def test_unknown_transformation(self, transformer):
        """Unknown transformation should return None."""
        result = transformer.apply_transformation("hello", "unknown_type")
        assert result is None

    def test_mixed_case_input(self, transformer):
        """Transformations should handle mixed case input."""
        result = transformer.apply_transformation("HeLLo WoRLd", "snake_case")
        assert result == "hello_world"

        result = transformer.apply_transformation("HeLLo WoRLd", "camel_case")
        assert result == "helloWorld"

    # ========================================================================
    # REAL-WORLD USE CASES
    # ========================================================================

    def test_variable_naming_python(self, transformer):
        """Convert phrase to Python variable name."""
        result = transformer.apply_transformation("user login count", "snake_case")
        assert result == "user_login_count"

    def test_variable_naming_javascript(self, transformer):
        """Convert phrase to JavaScript variable name."""
        result = transformer.apply_transformation("user login count", "camel_case")
        assert result == "userLoginCount"

    def test_class_naming(self, transformer):
        """Convert phrase to class name."""
        result = transformer.apply_transformation("user account manager", "pascal_case")
        assert result == "UserAccountManager"

    def test_css_class_naming(self, transformer):
        """Convert phrase to CSS class name."""
        result = transformer.apply_transformation("button primary large", "kebab_case")
        assert result == "button-primary-large"

    def test_quote_code_snippet(self, transformer):
        """Quickly quote a code snippet."""
        result = transformer.apply_transformation("import sys", "quote")
        assert result == '"import sys"'
