"""Whether a raw ``[[pattern]]`` entry can take a built-in's slot.

wh-pattern-override-doc-id.3.4. The merge decides which built-in a user
entry replaces BEFORE the catalog tries to build any of them. An entry
that names a built-in by ``doc_id`` therefore took that built-in's place
and was only then dropped by the build, so the built-in stopped answering
and nothing in the window said why. ``speech.pattern_identity`` already
refuses an entry whose ``pattern`` is not a string (.2.3); this module
extends the same refusal to the strings the build rejects.

WHAT IT DOES NOT LOOK AT. Actions. An override with a valid expression and
``actions = []`` is how a user switches a built-in off, so it must keep
the slot and suppress its built-in (wh-pattern-override-doc-id A5). The
question here is only whether the EXPRESSION could produce a rule.

THE FOUR REJECTIONS IT MIRRORS, each one a state
``PatternCatalog._build_structures`` refuses:
  1. An empty ``pattern`` string, refused by the ``if pattern_str and
     actions_list`` guard that opens the build loop.
  2. An expression that ``re.compile`` rejects after ``transform_pattern``.
  3. ``position = "trailing"`` with an expression that is not one literal
     word, refused by ``PatternCatalog._build_trailing_entry``.
  4. ``position = "trailing"`` together with ``requires_hotword``, refused
     because a hotword must precede the command while a trailing command
     must be the last word.
Mirroring is what makes this module able to answer before the build runs,
and mirroring is also its risk: an edit to the build's rules that is not
copied here would let a rejected entry take a slot again.
``tests/test_pattern_buildable.py`` holds each rejection to the build's
own answer over the same entry, so the two cannot drift unnoticed.

crewcut: one build-time rejection is deliberately NOT mirrored. A second
``position = "trailing"`` entry carrying a word another entry already
registered is skipped as a duplicate, so it builds nothing while this
module still calls it buildable. A per-entry question cannot see the rest
of the list. Removing the limit means giving the merge the whole merged
list to answer against, which is a larger change than the defect needs.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .pattern_identity import Identity, entry_identity
from .pattern_transform import transform_pattern

# The v1 trailing-command contract: one literal word, letters then
# letters or digits. The same expression ``_build_trailing_entry`` uses.
_TRAILING_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def can_build_expression(entry: Any) -> bool:
    """True when this raw entry's expression could produce a rule.

    Accepts any object because a hand edit can put a non-table into the
    ``pattern`` array, and answers False for one rather than raising: the
    merge asks this question about every entry it reads from a file the
    user can edit.
    """
    if not isinstance(entry, dict):
        return False
    expression = entry.get("pattern")
    # An empty string, and only an empty string: a whitespace-only
    # expression compiles and matches whitespace, so the build does
    # produce a rule for it and this must not claim otherwise.
    if not isinstance(expression, str) or not expression:
        return False
    if entry.get("position") == "trailing":
        if entry.get("requires_hotword", False):
            return False
        candidate = expression.strip()
        if not candidate:
            return False
        if candidate.startswith("^"):
            candidate = candidate[1:]
        if candidate.endswith("$"):
            candidate = candidate[:-1]
        # The word is escaped before compiling, so a word that passes
        # this check always compiles.
        return _TRAILING_WORD_RE.fullmatch(candidate) is not None
    try:
        transformed, _metadata = transform_pattern(expression)
        re.compile(transformed, re.IGNORECASE)
    except re.error:
        return False
    return True


def slot_identity(entry: Any) -> Optional[Identity]:
    """The identity this entry may take a built-in's slot under, or None.

    ``speech.pattern_identity.entry_identity`` answers what the entry
    identifies AS; this answers whether it may spend that identity on a
    built-in's place in the command order. The merge and the Pattern
    Manager's badge both ask this one, so a rule that cannot run is never
    listed as overriding a built-in that is still answering
    (wh-pattern-override-doc-id A6).
    """
    if not can_build_expression(entry):
        return None
    return entry_identity(entry)
