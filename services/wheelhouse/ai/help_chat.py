"""HelpChatSession -- multi-turn help conversation state manager.

Holds conversation history and delegates inference to AIService.
Created when AIService starts, destroyed when it stops. History
persists across window show/hide cycles and is only cleared by
explicit reset (New Chat button), model switch, or app restart.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ai.service import AIService

log = logging.getLogger(__name__)


class HelpChatSession:
    """Manages multi-turn help conversation state."""

    def __init__(self, ai_service: AIService):
        self._ai = ai_service
        self._history: list[dict] = []

    async def ask(self, question: str) -> Optional[str]:
        """Send a question, get a response, update history.

        Appends the user question before calling chat_help so the model
        sees it in context. On failure, removes the unanswered question
        so history stays in valid user/assistant pairs.

        Returns the model's response text, or None on failure.
        """
        self._history.append({"role": "user", "content": question})
        response = await self._ai.chat_help(list(self._history))
        if response:
            self._history.append({"role": "assistant", "content": response})
        else:
            self._history.pop()  # Remove unanswered question
        return response

    def reset(self) -> None:
        """Clear conversation history (start fresh)."""
        self._history.clear()
        log.info("Help chat session reset")
