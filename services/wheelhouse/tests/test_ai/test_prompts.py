"""Tests for AI prompt templates.

Verifies prompt constants exist, are well-formed strings, and that
the help template supports knowledge_base placeholder substitution.
"""

from ai.prompts import TEXT_CORRECTION_SYSTEM, HELP_SYSTEM_TEMPLATE


class TestTextCorrectionPrompt:

    def test_is_string(self):
        assert isinstance(TEXT_CORRECTION_SYSTEM, str)

    def test_is_nonempty(self):
        assert len(TEXT_CORRECTION_SYSTEM) > 50

    def test_mentions_formatting(self):
        """The correction prompt should mention its core purpose."""
        assert "formatting" in TEXT_CORRECTION_SYSTEM.lower() or \
               "capitalize" in TEXT_CORRECTION_SYSTEM.lower()

    def test_mentions_preservation(self):
        """The prompt should instruct the model NOT to rephrase."""
        assert "MUST NOT" in TEXT_CORRECTION_SYSTEM or \
               "must not" in TEXT_CORRECTION_SYSTEM.lower()


class TestHelpSystemTemplate:

    def test_is_string(self):
        assert isinstance(HELP_SYSTEM_TEMPLATE, str)

    def test_has_knowledge_base_placeholder(self):
        assert "{knowledge_base}" in HELP_SYSTEM_TEMPLATE

    def test_format_with_knowledge_base(self):
        """Verify .format() works with the placeholder."""
        result = HELP_SYSTEM_TEMPLATE.format(knowledge_base="test content")
        assert "test content" in result
        assert "{knowledge_base}" not in result

    def test_mentions_wheelhouse(self):
        assert "WheelHouse" in HELP_SYSTEM_TEMPLATE
