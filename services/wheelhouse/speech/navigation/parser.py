"""Parse cursor navigation utterances into NavigationCommand sequences."""

from typing import Optional

from ..number_word_parser import parse_number_word
from .models import NavigationCommand

MAX_COUNT = 50

_UNITS = {
    "character": "character", "characters": "character",
    "word": "word", "words": "word",
    "paragraph": "paragraph", "paragraphs": "paragraph",
}

# Spoken direction -> the direction the executor acts on. "write" is here
# because the shipped speech model returns "go write three characters" for
# the spoken words "go right three characters". The staged fragment
# wh-voice-access-parity.1.13 covers the bare utterance with two pattern
# blocks, but a chained utterance never reaches those blocks: the chain
# comes through the cursor-navigate entry into this parser, which knew only
# "right" and returned None, so the whole sentence was typed as text.
# David ruled extend on 2026-08-25 (wh-voice-access-parity.4).
#
# "write" is accepted wherever "right" is accepted, which is wider than the
# two staged blocks: both of those require a unit word, so the bare "go
# write" now moves the caret one character right where it used to be
# dictation. That single utterance is the whole of the widening, and it
# joins "go right", which has always done exactly that. The alternative was
# a parser where "write" works in some shapes and not others.
# "write" is the only navigation homophone the fragment defines; the other
# seven blocks in it are punctuation and delete/copy range commands.
_DIRECTIONS = {"right": "right", "write": "right", "left": "left"}

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
        direction = _DIRECTIONS.get(tokens[pos])
        if direction is None:
            return None
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
        """Convert spoken number or digit string to int (1-50). None if not a number.

        The reading is parse_number_word, the one word-to-integer
        implementation in this service (wh-number-words-one-parser).
        This method used to carry its own one..ten table, so "go right
        fifteen characters" was unparseable and the whole utterance was
        dictated. MAX_COUNT is unchanged: a larger count still clamps to
        50 rather than being refused, exactly as the table version did.

        A digit count whose VALUE is above 999 no longer reads as a
        count. It used to clamp to 50 through int(), which accepted a
        digit run of any length: "go right 1000 characters" and "go right
        1000000 characters" were both counts worth 50. Now the parser
        refuses a value above 999 and the segment falls through to
        dictation.

        The cutoff is the value, NOT the number of characters typed
        (wh-number-words-one-parser.1.4). parse_number_word strips
        leading zeroes before it applies its own three-digit bound, so
        "0001" is still a count worth 1 and "00050" is still 50, exactly
        as int() read them. Only a value above 999 changes, and that
        change is recorded for the user's decision on
        wh-number-words-one-parser.

        crewcut: this reads ONE token, so a multi-word count ("go right
        twenty three characters") is still unparseable here even though
        the parser can read it. The caller walks the tokens one at a time
        and would need to try the longest phrase first; that walk belongs
        with the cursor-navigation work, not with the count fix.
        """
        n = parse_number_word(text, aliases=True)
        if n is None:
            return None
        return min(n, MAX_COUNT)
