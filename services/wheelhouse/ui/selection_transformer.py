"""Text selection transformations: wrapping and case conversion.

This module provides transformation operations on selected text including:
- Wrapping: quotes, brackets, parentheses, etc.
- Case conversion: snake_case, camelCase, PascalCase, kebab-case, etc.

All transformations are pure string operations with no UI dependencies.
"""
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class SelectionTransformer:
    """Handles text transformation operations on selected text.

    Provides two categories of transformations:
    1. Wrapping: Adding delimiters around text (quotes, brackets, etc.)
    2. Case conversion: Changing text case format (snake_case, camelCase, etc.)

    All methods are pure functions operating on strings.
    """

    def apply_transformation(self, text: str, transformation_type: str) -> Optional[str]:
        """Apply the specified transformation to text.

        Main dispatcher for all transformation types.

        Args:
            text: The text to transform
            transformation_type: Type of transformation to apply

        Returns:
            Transformed text, or None if transformation type is unknown

        Supported transformations:
            Wrapping: quote, single_quote, bracket, parenthesis, angle_bracket, curly_bracket
            Case: uppercase, lowercase, capitalize, title_case,
                  snake_case, camel_case, pascal_case, kebab_case

        Examples:
            >>> t = SelectionTransformer()
            >>> t.apply_transformation("hello", "quote")
            '"hello"'
            >>> t.apply_transformation("hello world", "snake_case")
            'hello_world'
            >>> t.apply_transformation("hello world", "camel_case")
            'helloWorld'
        """
        # Wrapping transformations
        if transformation_type == 'quote':
            return f'"{text}"'
        elif transformation_type == 'single_quote':
            return f"'{text}'"
        elif transformation_type == 'bracket':
            return f'[{text}]'
        elif transformation_type == 'parenthesis':
            return f'({text})'
        elif transformation_type == 'angle_bracket':
            return f'<{text}>'
        elif transformation_type == 'curly_bracket':
            return f'{{{text}}}'

        # Case transformations
        elif transformation_type == 'uppercase':
            return text.upper()
        elif transformation_type == 'lowercase':
            return text.lower()
        elif transformation_type == 'capitalize':
            return text.capitalize()
        elif transformation_type == 'title_case':
            return text.title()
        elif transformation_type == 'snake_case':
            return self._to_snake_case(text)
        elif transformation_type == 'camel_case':
            return self._to_camel_case(text)
        elif transformation_type == 'pascal_case':
            return self._to_pascal_case(text)
        elif transformation_type == 'kebab_case':
            return self._to_kebab_case(text)
        elif transformation_type == 'compress':
            return text.replace(' ', '')

        # Unknown transformation
        logger.error(f"Unknown transformation type: {transformation_type}")
        return None

    # ========================================================================
    # CASE CONVERSION HELPERS
    # ========================================================================

    def _to_snake_case(self, text: str) -> str:
        """Convert space-separated text to snake_case.

        Args:
            text: Space-separated words

        Returns:
            snake_case formatted string

        Example:
            >>> t = SelectionTransformer()
            >>> t._to_snake_case("hello world test")
            'hello_world_test'
        """
        words = text.split()
        return '_'.join(word.lower() for word in words)

    def _to_camel_case(self, text: str) -> str:
        """Convert space-separated text to camelCase.

        First word is lowercased, subsequent words are capitalized,
        all joined without separators.

        Args:
            text: Space-separated words

        Returns:
            camelCase formatted string

        Example:
            >>> t = SelectionTransformer()
            >>> t._to_camel_case("hello world test")
            'helloWorldTest'
        """
        words = text.split()
        if not words:
            return text
        return words[0].lower() + ''.join(word.capitalize() for word in words[1:])

    def _to_pascal_case(self, text: str) -> str:
        """Convert space-separated text to PascalCase.

        All words are capitalized and joined without separators.

        Args:
            text: Space-separated words

        Returns:
            PascalCase formatted string

        Example:
            >>> t = SelectionTransformer()
            >>> t._to_pascal_case("hello world test")
            'HelloWorldTest'
        """
        words = text.split()
        return ''.join(word.capitalize() for word in words)

    def _to_kebab_case(self, text: str) -> str:
        """Convert space-separated text to kebab-case.

        Args:
            text: Space-separated words

        Returns:
            kebab-case formatted string

        Example:
            >>> t = SelectionTransformer()
            >>> t._to_kebab_case("hello world test")
            'hello-world-test'
        """
        words = text.split()
        return '-'.join(word.lower() for word in words)
