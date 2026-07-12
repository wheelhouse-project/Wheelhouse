from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

class ProcessingMode(Enum):
    """Processing state for the speech processor state machine.
    
    States:
        IDLE: Not buffering, processing words immediately (95% of words)
        COMMAND_BUFFERING: Buffering command pattern (1000ms timeout)
        REPLACEMENT_BUFFERING: Buffering replacement pattern (400ms timeout)
        HOTWORD_BUFFERING: Buffering after hotword detected (1000ms timeout)
    """
    IDLE = auto()
    COMMAND_BUFFERING = auto()
    REPLACEMENT_BUFFERING = auto()
    HOTWORD_BUFFERING = auto()

class Action(Enum):
    """Action to take based on routing decision."""
    BUFFER = auto()      # Add word to buffer and continue/start buffering
    EXECUTE = auto()     # Execute command immediately
    DICTATE = auto()     # Send text to dictation
    TRANSITION = auto()  # Change mode without adding to buffer (e.g. hotword)
    IGNORE = auto()      # Do nothing (e.g. utterance end handled elsewhere)

@dataclass
class Decision:
    """Routing decision result."""
    action: Action
    payload: Any = None  # Text to dictate, command to execute, or word to buffer
    target_mode: Optional[ProcessingMode] = None
    timeout_ms: Optional[int] = None
    reason: str = ""
    remainder: Optional[str] = None  # Text AFTER match to process after EXECUTE
    before_remainder: Optional[str] = None  # Text BEFORE match to process FIRST
