"""The SpeechProcessor members that BareNumberHolds uses.

BareNumberHolds keeps a back-reference to the SpeechProcessor that built it and
reads and writes these members through it. They stay on SpeechProcessor
because code outside the bare-number area also uses them (the held tail, the
utterance flags, the command timer and dictation). This Protocol lists them,
so the back-reference is explicit and the type checker sees each use.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class BareNumberCommandRunner(Protocol):
    """The members of the text parser that BareNumberHolds uses."""

    last_executed_pattern_type: Optional[str]

    # Any: TextParser.parse_and_execute (speech/command_engine.py) declares no
    # return type.
    # BareNumberHolds reads the result only as a truth value.
    async def parse_and_execute(
        self, text: str, *, authorized_command: bool = ...,
    ) -> Any: ...


class BareNumberHost(Protocol):
    """The members of SpeechProcessor that BareNumberHolds reads or writes."""

    text_parser: BareNumberCommandRunner
    command_timeout_ms: int

    @property
    def words_this_utterance(self) -> list[str]: ...

    @property
    def marker_finalization_active(self) -> bool: ...

    @property
    def current_word_starts_utterance(self) -> bool: ...

    @property
    def command_executed_in_utterance(self) -> bool: ...

    @command_executed_in_utterance.setter
    def command_executed_in_utterance(self, value: bool) -> None: ...

    @property
    def bare_number_deferred_word(self) -> Optional[str]: ...

    @bare_number_deferred_word.setter
    def bare_number_deferred_word(self, word: Optional[str]) -> None: ...

    # A view of the held tail of SpeechProcessor.
    @property
    def pending_bare_number_words(self) -> Optional[list[str]]: ...

    @pending_bare_number_words.setter
    def pending_bare_number_words(self, words: Optional[list[str]]) -> None: ...

    async def send_to_dictation(self, text: str) -> Optional[str]: ...

    def start_timeout(self, duration_ms: int) -> None: ...

    def overlay_state_value(self) -> Optional[str]: ...
