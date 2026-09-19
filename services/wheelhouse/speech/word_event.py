"""Word event data structure for speech processing.

This module defines the WordEvent dataclass used to represent individual words
with utterance boundary metadata as they flow through the speech processing pipeline.

WordEvent instances are created in step 1 (STT intake), queued for step 2 (dequeue),
and evaluated in step 3 (truth-table routing). See Speech Processing flow documentation
for the complete pipeline.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WordEvent:
    """Represents a single word with utterance boundary metadata.

    Each word from STT is wrapped in a WordEvent that indicates whether it's
    at the start or end of an utterance. This enables zero-latency passthrough
    decisions and proper pattern matching behavior.

    Attributes:
        word: The transcribed word text (e.g., "hello", "delete", "5")
        start_of_utterance: True if this is the first word of a fresh utterance
        end_of_utterance: True if this is the last word of an utterance
        utterance_id: Optional utterance ID for tracking utterance boundaries
        is_utterance_end_marker: True if this is a special marker signaling utterance end
        is_retraction_marker: True if this is a STT-revision retraction signal
        is_timeout_finalize_marker: True if this is a buffered timeout sentinel
            (wh-oe7u.4). The timeout task enqueues one of these instead of
            calling SpeechProcessor._execute_decision directly so the
            processing loop is the only writer for normal words, retractions,
            utterance_end markers, and timeout finalizations.
        utterance_start_generation: The app's start_utterance count just
            after this utterance's start_utterance command was enqueued
            (wh-spaced-punctuation-names-unresolved.3.1.5). Only the
            first word of an utterance carries it. The processor
            compares it with the count it read at its own last delivery
            to tell text typed BEFORE that command from text typed
            after it; the Input side resets its paste counter on the
            command, so only the earlier text has left the retractable
            span.
        timeout_token: Generation token captured when the sentinel was created.
            The processing loop ignores the sentinel unless this matches the
            processor's current ``timeout_token``; cancelled timeouts bump the
            token so any sentinel they enqueued becomes a no-op.

    Examples:
        # Single-word utterance: "yes"
        WordEvent(word="yes", start_of_utterance=True, end_of_utterance=True)

        # Multi-word utterance: "delete five"
        WordEvent(word="delete", start_of_utterance=True, end_of_utterance=False)
        WordEvent(word="five", start_of_utterance=False, end_of_utterance=True)

        # Mid-utterance word: "I want to delete something"
        WordEvent(word="delete", start_of_utterance=False, end_of_utterance=False)

        # Utterance end marker (no more words for this utterance)
        WordEvent(word="", start_of_utterance=False, end_of_utterance=True,
                 utterance_id=123, is_utterance_end_marker=True)

        # Timeout finalize sentinel (wh-oe7u.4)
        WordEvent.timeout_finalize(token=3)
    """
    word: str
    start_of_utterance: bool
    end_of_utterance: bool
    utterance_id: Optional[int] = None
    is_utterance_end_marker: bool = False
    is_retraction_marker: bool = False
    retraction_full_text: Optional[str] = None
    trace_id: Optional[str] = None
    is_timeout_finalize_marker: bool = False
    timeout_token: int = 0
    # wh-x4fwo Mode 1: lifecycle reset marker for two-phrase fallback finals.
    # Pairs an end_utterance and a start_utterance IPC for the same
    # utterance_id so the WheelHouse SpeechProcessor draws phrase 1 to a
    # close before phrase 2 begins. See wh-2t1f3 design notes.
    is_lifecycle_reset_marker: bool = False
    # wh-spaced-punctuation-names-unresolved.3.1.5: the value of
    # WheelHouseApp.utterance_start_generation just after this
    # utterance's start_utterance command was enqueued. Set only on the
    # FIRST word of an utterance, by the producer that sent that
    # command. ``None`` means no producer stamped it, which the
    # processor treats as "no start seen".
    utterance_start_generation: Optional[int] = None

    @classmethod
    def timeout_finalize(
        cls, token: int, utterance_id: Optional[int] = None,
    ) -> "WordEvent":
        """Construct a typed timeout-finalize sentinel (wh-oe7u.4).

        The sentinel is enqueued by ``_timeout_handler`` and consumed by
        ``process_word_event``. The token must match the processor's
        ``timeout_token`` at consumption time; any earlier value is
        treated as stale and the sentinel is ignored.
        """
        return cls(
            word="",
            start_of_utterance=False,
            end_of_utterance=False,
            utterance_id=utterance_id,
            is_timeout_finalize_marker=True,
            timeout_token=token,
        )
