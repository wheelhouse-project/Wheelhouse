"""Tests for AIProvider protocol definition.

Verifies that:
- Classes implementing chat() + is_available() satisfy the protocol
- Classes missing required methods do NOT satisfy the protocol
- The protocol is runtime-checkable via isinstance()
"""

from unittest.mock import AsyncMock

import pytest

from ai.providers.base import AIProvider


class _ValidProvider:
    """Implements both required protocol methods."""

    async def chat(self, messages: list[dict], max_tokens: int = 500) -> str:
        return "response"

    async def is_available(self) -> bool:
        return True


class _MissingChat:
    """Missing chat() -- should NOT satisfy protocol."""

    async def is_available(self) -> bool:
        return True


class _MissingIsAvailable:
    """Missing is_available() -- should NOT satisfy protocol."""

    async def chat(self, messages: list[dict], max_tokens: int = 500) -> str:
        return "response"


class _EmptyClass:
    """No methods at all."""
    pass


class TestAIProviderProtocol:

    def test_valid_provider_satisfies_protocol(self):
        provider = _ValidProvider()
        assert isinstance(provider, AIProvider)

    def test_missing_chat_does_not_satisfy(self):
        obj = _MissingChat()
        assert not isinstance(obj, AIProvider)

    def test_missing_is_available_does_not_satisfy(self):
        obj = _MissingIsAvailable()
        assert not isinstance(obj, AIProvider)

    def test_empty_class_does_not_satisfy(self):
        obj = _EmptyClass()
        assert not isinstance(obj, AIProvider)

    def test_protocol_is_runtime_checkable(self):
        """Verify the protocol has @runtime_checkable decorator."""
        assert hasattr(AIProvider, '__protocol_attrs__') or hasattr(AIProvider, '__abstractmethods__') or True
        # The real check is that isinstance() calls above don't raise TypeError.
        # A non-runtime_checkable Protocol would raise TypeError on isinstance().
        provider = _ValidProvider()
        # This would raise TypeError if not runtime_checkable:
        result = isinstance(provider, AIProvider)
        assert result is True
