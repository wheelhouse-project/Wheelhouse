"""AIProvider protocol -- minimal interface for AI backends.

All AI providers (Ollama, OpenAI-compatible, etc.) implement this two-method
protocol. The rest of the system (prompts, service layer, voice commands)
is provider-agnostic.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class AIProvider(Protocol):

    async def chat(self, messages: list[dict], max_tokens: int = 500) -> str:
        """Send a list of chat messages, return the assistant's response text.

        Args:
            messages: List of dicts with 'role' and 'content' keys.
                      Roles: 'system', 'user', 'assistant'
            max_tokens: Maximum tokens in the response.

        Returns:
            The assistant's response text, or empty string on failure.
        """
        ...

    async def is_available(self) -> bool:
        """Check if the provider is ready to accept requests."""
        ...
