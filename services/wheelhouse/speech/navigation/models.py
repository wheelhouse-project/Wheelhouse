"""Data models for cursor navigation commands."""

from dataclasses import dataclass


@dataclass
class NavigationCommand:
    """A single parsed navigation command.

    Attributes:
        verb: "go" (move cursor) or "grab" (select text).
        kind: "relative" (direction + count + unit) or "landmark" (absolute position).
        direction: "right" or "left". Only used when kind="relative".
        count: Number of units to move. Default 1. Only used when kind="relative".
        unit: "character", "word", or "paragraph". Only used when kind="relative".
        landmark: Target position name. Only used when kind="landmark".
            Values: "home", "end", "top", "bottom",
            "start_of_word", "end_of_word", "start_of_paragraph", "end_of_paragraph".
    """

    verb: str
    kind: str
    direction: str = ""
    count: int = 1
    unit: str = "character"
    landmark: str = ""
