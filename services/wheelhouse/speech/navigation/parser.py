"""Parse cursor navigation utterances into NavigationCommand sequences."""

from typing import Optional

from ..number_word_parser import parse_number_word
from .models import NavigationCommand

# Every count is sent as hotkey_action repeats, which pause 100 ms apart,
# so this is the same cap as speech/actions.py HOTKEY_REPEAT_CAP: 29
# pauses stay under the 5.0 s wait for the Input process's answer
# (Boss e8 ruling 2026-09-26 12:38, wh-voice-access-parity.1.15.6.1).
# tests/test_navigation_parser.py ties the two values; this module does
# not import speech.actions, to avoid an import cycle.
MAX_COUNT = 30

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
#
# "up" and "down" are here so that a "then" chain can move up or down
# (wh-voice-chain-up-down). A single "go up three" never reaches this
# parser: the whole_utterance_only blocks nav-up-times, nav-up-lines and
# nav-up-paragraphs (and their down twins) sit above cursor-navigate in
# patterns.toml. A chain reaches only cursor-navigate, so an up or down
# part must read the same words those blocks read and press the same keys.
_DIRECTIONS = {"right": "right", "write": "right", "left": "left",
               "up": "up", "down": "down"}

# The unit words of an up or down part: the single forms take "lines" and
# "paragraphs", and "times" after a count, but never "words" or
# "characters". No unit word is one line, as in nav-up-times.
_VERTICAL = {"up", "down"}
_VERTICAL_UNITS = {
    "line": "line", "lines": "line",
    "paragraph": "paragraph", "paragraphs": "paragraph",
}
_TIMES = {"time", "times"}

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

        vertical = direction in _VERTICAL
        # "grab up" and "grab down" are not built: no single form selects
        # up or down (Boss ruling 2026-10-20 20:10, Q2 = 1).
        if vertical and verb == "grab":
            return None

        count = 1
        unit = "line" if vertical else "character"

        # Optional unit: only the last token can be one.
        end = len(tokens)
        if end > pos:
            last = tokens[end - 1]
            if vertical:
                u = _VERTICAL_UNITS.get(last)
                # "times" needs a count before it, as in nav-up-times.
                if u is None and last in _TIMES and end - 1 > pos:
                    u = "line"
            else:
                u = _UNITS.get(last)
            if u is not None:
                unit = u
                end -= 1

        # Optional count: every token between the direction and the unit
        # is one number, so "twenty three" reads as 23, the same as "23"
        # (wh-number-words-command-failures). Tokens that are not one
        # number, such as "three emails", make the segment invalid.
        if end > pos:
            n = NavigationParser._parse_count(" ".join(tokens[pos:end]))
            if n is None:
                return None
            count = n

        return NavigationCommand(verb=verb, kind="relative", direction=direction, count=count, unit=unit)

    @staticmethod
    def _parse_count(text: str) -> Optional[int]:
        """Convert spoken number or digit string to int (1-MAX_COUNT). None if not a number.

        The reading is parse_number_word, the one word-to-integer
        implementation in this service (wh-number-words-one-parser).
        This method used to carry its own one..ten table, so "go right
        fifteen characters" was unparseable and the whole utterance was
        dictated. A larger count still clamps to MAX_COUNT rather than
        being refused, exactly as the table version did. MAX_COUNT was
        50 until wh-voice-access-parity.1.15.6.1 lowered it to 30.

        A digit count whose VALUE is above 999 no longer reads as a
        count. It used to clamp to MAX_COUNT through int(), which accepted
        a digit run of any length: "go right 1000 characters" and "go
        right 1000000 characters" were both counts worth 50. Now the parser
        refuses a value above 999 and the segment falls through to
        dictation.

        The cutoff is the value, NOT the number of characters typed
        (wh-number-words-one-parser.1.4). parse_number_word strips
        leading zeroes before it applies its own three-digit bound, so
        "0001" is still a count worth 1 and "00050" is still 50, exactly
        as int() read them. Only a value above 999 changes, and that
        change is recorded for the user's decision on
        wh-number-words-one-parser.

        The text can be more than one word: the caller passes every token
        between the direction and the unit, so "twenty three" reads as 23
        (wh-number-words-command-failures).
        """
        n = parse_number_word(text, aliases=True)
        if n is None:
            return None
        return min(n, MAX_COUNT)
