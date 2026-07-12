"""Parse cursor navigation utterances into NavigationCommand sequences."""

from typing import Optional

from .models import NavigationCommand

MAX_COUNT = 50

_WORD_TO_INT = {
    "one": 1, "two": 2, "to": 2, "too": 2,
    "three": 3, "four": 4, "for": 4,
    "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}

_UNITS = {
    "character": "character", "characters": "character",
    "word": "word", "words": "word",
    "paragraph": "paragraph", "paragraphs": "paragraph",
}

_SIMPLE_LANDMARKS = {"home", "end", "top", "bottom"}
_COMPOUND_PREFIXES = {"start", "beginning", "end"}
_COMPOUND_UNITS = {"word", "paragraph"}


class NavigationParser:
    """Stateless parser: utterance string -> list of NavigationCommand."""

    @staticmethod
    def parse(utterance: str) -> Optional[list]:
        """Parse a full utterance (may contain 'then' chains) into commands.

        Returns a list of NavigationCommand on success, or None if any segment
        is unparseable (caller should fall through to dictation).
        """
        segments = utterance.split(" then ")
        commands = []
        for segment in segments:
            cmd = NavigationParser._parse_segment(segment.strip())
            if cmd is None:
                return None
            commands.append(cmd)
        return commands if commands else None

    @staticmethod
    def _parse_segment(segment: str) -> Optional[NavigationCommand]:
        tokens = segment.lower().split()
        if not tokens:
            return None

        verb = tokens[0]
        if verb not in ("go", "grab"):
            return None

        rest = tokens[1:]
        if not rest:
            return None

        if verb == "go":
            return NavigationParser._parse_go(rest)
        return NavigationParser._parse_grab(rest)

    @staticmethod
    def _parse_go(tokens: list) -> Optional[NavigationCommand]:
        # Optional "to" after "go": "go to end" == "go end" (wh-ed4).
        # Strip before dispatch so both landmark and relative see uniform tokens.
        # Relative never starts with "to" (direction is required first), so
        # stripping here cannot mis-route a relative utterance.
        if len(tokens) > 1 and tokens[0] == "to":
            tokens = tokens[1:]
        # Try landmark first (landmarks can start with "end" which overlaps direction)
        cmd = NavigationParser._try_landmark(tokens, "go")
        if cmd:
            return cmd
        return NavigationParser._try_relative(tokens, "go")

    @staticmethod
    def _parse_grab(tokens: list) -> Optional[NavigationCommand]:
        # "grab to <landmark>"
        if tokens[0] == "to":
            if len(tokens) < 2:
                return None
            return NavigationParser._try_landmark(tokens[1:], "grab")
        # "grab <relative>"
        return NavigationParser._try_relative(tokens, "grab")

    @staticmethod
    def _try_landmark(tokens: list, verb: str) -> Optional[NavigationCommand]:
        """Try to parse tokens as a landmark. Returns None if not a landmark."""
        # Three-word compound: "start/beginning/end of word/paragraph"
        if len(tokens) == 3 and tokens[1] == "of":
            prefix, unit = tokens[0], tokens[2]
            if unit in _COMPOUND_UNITS:
                if prefix in ("start", "beginning"):
                    return NavigationCommand(verb=verb, kind="landmark", landmark=f"start_of_{unit}")
                if prefix == "end":
                    return NavigationCommand(verb=verb, kind="landmark", landmark=f"end_of_{unit}")
            return None  # "X of Y" with invalid X or Y

        # Single-word landmark
        if len(tokens) == 1 and tokens[0] in _SIMPLE_LANDMARKS:
            return NavigationCommand(verb=verb, kind="landmark", landmark=tokens[0])

        return None

    @staticmethod
    def _try_relative(tokens: list, verb: str) -> Optional[NavigationCommand]:
        """Try to parse tokens as a relative movement."""
        pos = 0

        # Direction is required
        if tokens[pos] not in ("right", "left"):
            return None
        direction = tokens[pos]
        pos += 1

        count = 1
        unit = "character"

        # Optional count
        if pos < len(tokens):
            n = NavigationParser._parse_count(tokens[pos])
            if n is not None:
                count = n
                pos += 1

        # Optional unit
        if pos < len(tokens):
            u = _UNITS.get(tokens[pos])
            if u is not None:
                unit = u
                pos += 1

        # Trailing tokens = invalid
        if pos < len(tokens):
            return None

        return NavigationCommand(verb=verb, kind="relative", direction=direction, count=count, unit=unit)

    @staticmethod
    def _parse_count(text: str) -> Optional[int]:
        """Convert spoken number or digit string to int (1-50). None if not a number."""
        n = _WORD_TO_INT.get(text)
        if n is not None:
            return min(n, MAX_COUNT) if n > 0 else None
        try:
            n = int(text)
            return min(n, MAX_COUNT) if n > 0 else None
        except ValueError:
            return None
