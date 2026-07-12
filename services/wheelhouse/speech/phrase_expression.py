r"""Phrase-list expression generation for user patterns.

A user pattern block may carry a ``phrases = ["editor", "code editor"]``
array instead of a hand-written trigger. This module turns that list into
the matching expression: each phrase escaped, joined as alternatives,
anchored (``^(?:editor|code\ editor)$``, spec section 6 of
docs/plans/2026-07-09-pattern-manager-editor-design-v1.md,
wh-pattern-editor-phrases).

Anchoring mirrors PatternManager.generate_regex: command patterns get
``^...$``, replacement patterns get ``\b...\b``. Unlike generate_regex
(which splits the trigger into words and joins them with ``\s+``), each
phrase is escaped whole -- ``re.escape`` renders an internal space as
``\ `` -- exactly as the spec example shows; internal whitespace runs are
collapsed to a single space first so a phrase typed with stray spacing
still matches normal STT output.

Dependency-free by contract (stdlib only, no import side effects): the
Logic process (PatternManager) and the GUI process (editor dialog) both
import it.
"""
import re
from typing import List, Optional, Sequence


def normalize_phrases(phrases: Sequence[str]) -> List[str]:
    """Return each phrase stripped, with internal whitespace runs collapsed.

    ``"  code   editor "`` becomes ``"code editor"``. The normalized form is
    what gets escaped into the expression AND what gets stored in the TOML
    ``phrases`` array, so the two always agree. Callers must validate first
    (see validate_phrases); this helper assumes string items.
    """
    return [" ".join(p.split()) for p in phrases]


def validate_phrases(phrases: object) -> Optional[str]:
    """Validate a proposed phrase list; return an error message or None.

    Checks, in order:
      - the value is a list (a bare string is iterable and would otherwise
        silently become a per-character alternation);
      - it is non-empty;
      - every item is a string that is non-empty after stripping;
      - no item contains a double quote or backslash (the manager writes
        the list as a basic-string TOML array, where a backslash parses as
        an escape sequence and a quote breaks the string -- neither can be
        spoken anyway);
      - no duplicates after whitespace collapse + casefold (matching is
        case-insensitive, so "Editor" duplicates "editor").
    """
    if not isinstance(phrases, list):
        return "Phrases must be a list of text phrases"
    if not phrases:
        return "At least one phrase is required"
    seen: set = set()
    for item in phrases:
        if not isinstance(item, str):
            return "Each phrase must be text"
        if not item.strip():
            return "Phrases cannot be empty"
        if '"' in item or "\\" in item:
            return "Phrases cannot contain quotes or backslashes"
        key = " ".join(item.split()).casefold()
        if key in seen:
            return f"Duplicate phrase: '{' '.join(item.split())}'"
        seen.add(key)
    return None


def generate_expression(
    phrases: Sequence[str], pattern_type: str = "command",
) -> str:
    r"""Generate the matching expression for a phrase list.

    Each phrase is normalized (strip + collapse internal whitespace),
    ``re.escape``'d whole, joined with ``|`` inside a non-capturing group,
    and anchored by pattern type -- ``^(?:...)$`` for commands,
    ``\b(?:...)\b`` for replacements -- mirroring
    PatternManager.generate_regex's anchoring rule.

    Args:
        phrases: List of spoken phrases (e.g. ["editor", "code editor"]).
        pattern_type: "command" or "replacement".

    Returns:
        The expression string, ready for re.compile.

    Raises:
        ValueError: If the phrase list fails validate_phrases; the message
            is user-readable and surfaces through the manager's
            {"success": False, "error": ...} envelope.
    """
    error = validate_phrases(phrases)
    if error is not None:
        raise ValueError(error)
    escaped = [re.escape(p) for p in normalize_phrases(list(phrases))]
    body = "(?:" + "|".join(escaped) + ")"
    if pattern_type == "command":
        return f"^{body}$"
    return rf"\b{body}\b"
