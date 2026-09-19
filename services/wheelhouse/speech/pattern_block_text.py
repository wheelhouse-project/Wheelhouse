"""Find one ``[[pattern]]`` block in the raw text of a patterns file.

Three callers share this walk: the Pattern Manager's update and delete,
which rewrite one block and leave every other byte alone, and the
load-time legacy-id migration in ``pattern_catalog``, which inserts two
lines into one block and leaves every other byte alone
(wh-pattern-override-doc-id.3.1).

WHY RAW TEXT AND NOT A RE-SERIALIZED FILE. A person hand-edits this file.
It carries their comments, their blank lines, their key order and their
choice of quoting. Parsing the file and writing it back from the parsed
data would lose all of that. So every writer locates the block it means
by counting headers in the text, edits those lines, and copies the rest
through unchanged.

WHY COUNTING AND NOT MATCHING. The block is named by its position in the
parsed ``[[pattern]]`` list, because that is the only name that survives
two blocks carrying one expression. tomllib preserves array-of-tables
order, so the Nth header at TOP LEVEL is the Nth entry in the parsed list.

WHY A HEADER-LOOKING LINE IS NOT ENOUGH. A multi-line TOML string can
hold anything, including a line that reads exactly like a ``[[pattern]]``
header -- a rule that TYPES an example of this file writes one. Counting
those with the real headers puts every later block one place out, and the
misplaced range parses on its own, so a caller checking only the located
text accepts a fragment cut out of somebody's string literal
(wh-pattern-override-doc-id.3.7). So this module decides top level for
itself: the text BEFORE a candidate line is parsed, and a prefix that
ends inside a multi-line string or an unclosed array never parses, while
a prefix that does parse proves the line that follows it stands at top
level.

This module imports nothing from the project. The header expression is
the one thing every writer must agree on, and a second copy of it is what
would let them disagree about which block is the Nth one.
"""

from __future__ import annotations

import re
import tomllib
from typing import List, Optional, Tuple

# A [[pattern]] header as tomllib accepts it: whitespace inside the
# brackets and a trailing comment are both valid TOML that hand-edits
# produce. The raw-text walks must count exactly what tomllib parses,
# or a writer edits the wrong block (wh-pattern-editor-r8.3).
PATTERN_HEADER_RE = re.compile(r"^\s*\[\[\s*pattern\s*\]\]\s*(?:#.*)?$")

# A hand-written banner line the shipped file uses to separate sections.
# A block ends at one of these as well as at the next header, so a writer
# does not swallow the banner that belongs to the section below it.
_BANNER_MARK = "====="


def is_pattern_header(line: str) -> bool:
    """Whether *line* LOOKS like a ``[[pattern]]`` header.

    A line inside a multi-line string can look exactly like one. Only
    ``locate_pattern_block`` decides which of these lines the parser also
    sees as a header; a caller using this alone is counting text.
    """
    return PATTERN_HEADER_RE.match(line) is not None


def _stands_at_top_level(
    lines: List[str], anchor: int, index: int,
) -> bool:
    """Whether line *index* begins at TOML's top level.

    *anchor* is a line already known to begin at top level, and the text
    from there to *index* is parsed on its own. That window is a whole
    document when *index* is at top level, and it ends inside a
    multi-line string or an unclosed array when it is not. So the
    question "is this line inside something?" is asked of the one thing
    that can answer it exactly -- the parser the file is read with --
    rather than of a second, hand-written TOML lexer this module exists
    to avoid having.

    WHY A WINDOW AND NOT THE WHOLE PREFIX. Both answer the same
    question, because the parser's state at *anchor* is top level either
    way, and the window is what keeps the walk linear. Measured on this
    machine against the shipped 120 KB patterns file, 319 blocks: the
    last block costs 1369 ms located from whole prefixes and 10.6 ms
    from windows. The file this actually runs against is the person's
    own, and a 12,469-byte, 599-line 50-rule user file costs about 1.3 ms
    for its last block and about 33 to 34 ms to locate every one of them,
    which is what a migration that names every rule in the file pays.
    Those are five-run medians. Treat every figure here as an observation
    and not a bound: repeated runs of the SAME input in one process have
    moved about 2.4 times, 9 ms to 22 ms, after a long whole-prefix walk
    ran in between.
    """
    try:
        tomllib.loads("".join(lines[anchor:index]))
    except Exception:
        return False
    return True


def locate_pattern_block(
    lines: List[str], target_index: int,
) -> Tuple[Optional[int], Optional[int]]:
    """Return the line range of the *target_index*-th ``[[pattern]]`` block.

    Args:
        lines: The file's lines, as ``splitlines(keepends=True)`` gives
            them. Keeping the line endings is what lets a caller join the
            result back into the original bytes.
        target_index: Zero-based position of the block in the parsed
            ``[[pattern]]`` list.

    Returns:
        ``(start, end)`` as indices into *lines*, where ``start`` is the
        header line and ``end`` is one past the block's last line.
        ``(None, None)`` when the file holds fewer headers than that.

    The range stops at the next header or at a banner comment line, and it
    INCLUDES any blank lines between the block and whichever of those ends
    it. A caller that must not move the following layout trims those blank
    lines itself; a caller that removes the block keeps them, so deleting
    does not leave a growing gap.

    Both ends ignore a header-looking or banner-looking line that the
    parser does not see, so a block whose own text action contains one is
    neither miscounted nor cut in half.
    """
    header_count = -1
    # crewcut: one call walks the file from the start, so a caller that
    # locates N blocks parses every earlier block's windows again. The
    # load-time migration does exactly that -- one call per recovered
    # name -- which makes its cost quadratic in the number of blocks it
    # names. Measured on a 12,469-byte, 599-line 50-rule user file,
    # five-run medians: about 1.3 ms for the last block alone, and about
    # 33 to 34 ms to locate all fifty. Repeated runs of the same input in
    # one process have moved about 2.4 times, so read these as
    # observations and not bounds. To remove the
    # limit, return every block's range from a single walk and let the
    # caller index into the result, instead of taking target_index and
    # starting over.
    # The last line known to begin at top level, and so the place every
    # window starts. The file's first line qualifies; after that, each
    # header this walk accepts does.
    anchor = 0
    i = 0
    while i < len(lines):
        if is_pattern_header(lines[i]) and _stands_at_top_level(
            lines, anchor, i,
        ):
            anchor = i
            header_count += 1
            if header_count == target_index:
                start = i
                j = i + 1
                while j < len(lines):
                    stripped = lines[j].strip()
                    ends_the_block = is_pattern_header(lines[j]) or (
                        stripped.startswith("#") and _BANNER_MARK in stripped
                    )
                    if ends_the_block and _stands_at_top_level(
                        lines, start, j,
                    ):
                        break
                    j += 1
                return start, j
        i += 1
    return None, None
