"""The one rule that says which built-in a saved override belongs to.

Every place that decides "is this user entry a replacement for that built-in?"
calls ``entry_identity`` here, and nowhere else defines that rule
(wh-pattern-override-doc-id A6). Three callers share it: the runtime merge in
``pattern_catalog``, the Pattern Manager's listing, and the draft simulation in
``pattern_tester``. A second copy is what let the three disagree.

WHY doc_id AND NOT THE PATTERN TEXT. The association used to be the pattern's
own regex, normalized by strip and casefold. A regex is editable, so a release
that rewrote a built-in's expression broke the association: the saved override
no longer shared a key with the built-in it replaced, the merge appended it
after every built-in instead of replacing one, and the built-in won again. The
user saw their customisation stop working and the Pattern Manager show it
overriding nothing. ``doc_id`` is the durable name of a shipped pattern, so an
override keyed on it survives any rewrite of the expression.

THE STABLE-IDENTITY CONTRACT this depends on, in full:
  1. A shipped pattern's ``doc_id`` never changes. Editing its regex, its
     actions, its phrases or its category is expected; changing its ``doc_id``
     silently orphans every override saved against it.
  2. A retired ``doc_id`` is never reused for a different pattern. Reuse would
     hand an old override to an unrelated command.
  3. Every shipped pattern carries a ``doc_id``, and no two share one.
Points 1 and 2 are promises this code cannot check from a single release; a
test pins them against the shipped file, and the help-document generator's
sidecar has depended on the same contract since wh-helpdoc-pattern-ids.

THIS MODULE IMPORTS NOTHING FROM THE PROJECT, on purpose. It is loaded at
runtime by the app and, by absolute file path, by the release build's
``scripts/release/helpdoc/pattern_ids.py``, which cannot import the ``speech``
package (``scripts/release/`` is a [prune] entry and is absent from a public
release, so the dependency has to run build-tool-imports-runtime). Keeping the
imports to ``re`` and typing is what makes that direction safe.
"""

from __future__ import annotations

import re
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeGuard,
)

# The key a shipped [[pattern]] entry carries its durable name under.
DOC_ID_KEY = "doc_id"

# The key the Pattern Manager writes into every block it CREATES, and the
# one value that counts. It says the block is a rule of the person's own, so
# the legacy text migration below must never hand it a built-in
# (wh-pattern-override-doc-id.3.3).
#
# Not the same thing as ``source = "pattern_manager"``, which the block
# writer has also written since 2026-03-16. That key says which program
# wrote the block; a customization saved before ids existed carries it too,
# so it cannot separate a duplicate from an override.
#
# A hand-edited value other than ORIGIN_OWN does not count. The key is a
# statement the editor made about a block it wrote, and a hand edit must not
# be able to detach an override from the built-in it replaces.
#
# crewcut: this key fixes duplicates saved from now on. A duplicate ALREADY
# saved by a shipped version carries no such key, and its block is byte for
# byte the shape of a customization saved before ids existed, so nothing in
# the file separates the two. Such a duplicate keeps taking its built-in
# over, and the .3.1 migration will write that association into the file and
# make it durable. It is the state the person already has today, not a new
# one. Removing the limit needs a signal the old files do carry -- the
# release the block was written under, or a question put to the person the
# first time the window opens -- and neither exists yet.
ORIGIN_KEY = "origin"
ORIGIN_OWN = "user"

# A doc_id is a slug: lowercase ASCII letters and digits in hyphen-separated
# groups, with no leading, trailing or doubled hyphen. The same shape the
# help-document sidecar joins against, so an id is safe as a TOML table key
# and as a heading anchor.
_DOC_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# The two kinds of identity, kept apart so they cannot collide. Without the
# kind, a user entry whose regex happened to read "window-maximize" would
# replace the built-in whose doc_id is "window-maximize" -- a silent capture
# of an unrelated command.
DOC_KIND = "doc"
TEXT_KIND = "text"

Identity = Tuple[str, str]


def is_valid_doc_id(value: object) -> TypeGuard[str]:
    """Return True only for a well-formed doc_id slug.

    Declared as a TypeGuard, the same shape the help-document sidecar's
    checker uses, so a caller that has tested a value may then treat it as a
    string without a second check.

    ``fullmatch`` and not ``match``: a partial match would accept
    ``"window-maximize!"`` because the slug matches its beginning, letting a
    hand-edited id carrying punctuation act as a merge key. A non-string
    value -- a hand-edited ``doc_id = 5`` -- is rejected before it reaches the
    regex.
    """
    return isinstance(value, str) and _DOC_ID_RE.fullmatch(value) is not None


def normalized_text_key(pattern: str) -> str:
    """Return the legacy association key: the pattern text, strip + casefold.

    Still needed after the move to doc_id, for two jobs. A user entry saved
    before doc_ids existed carries no id, so it merges on this key. And the
    legacy migration matches such an entry to its built-in by this key, which
    is the only association the old file recorded.
    """
    return pattern.strip().casefold()


def identity_from(doc_id: object, pattern: object) -> Optional[Identity]:
    """THE rule, on the two values every shape of entry can supply.

    None when *pattern* is not a string, whatever *doc_id* holds;
    otherwise ``(DOC_KIND, doc_id)`` when *doc_id* is a valid slug;
    otherwise ``(TEXT_KIND, normalized pattern text)``. None means the
    entry can never replace another and is always appended.

    The pattern check comes FIRST because an entry that cannot build must
    not spend a built-in's slot. A hand-edited ``pattern = 5`` under a
    valid ``doc_id`` used to win the built-in's identity here, take its
    place in the merge, and only then be dropped by the build -- so the
    built-in stopped answering and the window still listed it
    (wh-pattern-override-doc-id.2.3).

    A malformed ``doc_id`` falls back to the text key rather than becoming a
    key of its own. Falling back keeps a hand-edited ``doc_id = "Window
    Maximize"`` merging exactly as it did before ids existed, instead of
    turning it into an unmatched entry that quietly loses to its built-in.

    Both parameters are typed ``object`` because both reach here straight
    from a hand-editable TOML file, where ``doc_id = 5`` and ``pattern = 5``
    are valid syntax. Every caller drops such an entry later with a warning;
    this rule must not raise before it gets the chance.
    """
    if not isinstance(pattern, str):
        return None
    if is_valid_doc_id(doc_id):
        return (DOC_KIND, doc_id)
    return (TEXT_KIND, normalized_text_key(pattern))


def entry_identity(entry: Any) -> Optional[Identity]:
    """Return what a RAW ``[[pattern]]`` table merges on, or None.

    The TOML shape: the expression lives under ``pattern``. Used by the
    catalog merge and the manager listing, which both read the files.

    Accepts any object because a hand edit can put a non-table into the
    ``pattern`` array (``pattern = [1, 2, 3]`` parses as a list).
    """
    if not isinstance(entry, dict):
        return None
    return identity_from(entry.get(DOC_ID_KEY), entry.get("pattern"))


def runtime_entry_identity(entry: Any) -> Optional[Identity]:
    """Return what a BUILT runtime entry merges on, or None.

    The in-memory shape the catalog hands the rest of the app: the
    expression lives under ``raw_pattern`` (the pre-transform text), because
    ``pattern`` is not a key there at all. Used by the draft simulation in
    ``pattern_tester``, which works on the live merged list rather than on
    the files.

    Two functions rather than one that tries both key names: an entry shape
    that carried both would be ambiguous, and reading the wrong key would
    silently produce None -- an entry that merges on nothing and is appended,
    which is exactly the bug this module exists to remove.
    """
    if not isinstance(entry, dict):
        return None
    return identity_from(entry.get(DOC_ID_KEY), entry.get("raw_pattern"))


def _text_candidates(
    entries: Iterable[Any], expression_key: str,
) -> Dict[str, List[Identity]]:
    """Map normalized expression text to the identities carrying that text.

    The lookup a pre-doc_id override needs: it recorded no name, so the only
    association its file holds is the expression it was written against.
    Entries whose expression is not a string, and entries that identify as
    nothing, contribute no candidate -- a hand edit must not create one.

    A shipped entry appears under its own text even though it identifies by
    ``doc_id``; that is the whole point, and it is why the caller must try
    the identity match first. An exact identity match is never ambiguous, so
    a system entry that carries no id and matches a legacy override outright
    is settled before this map is consulted.
    """
    index: Dict[str, List[Identity]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        expression = entry.get(expression_key)
        if not isinstance(expression, str):
            continue
        identity = identity_from(entry.get(DOC_ID_KEY), expression)
        if identity is None:
            continue
        index.setdefault(normalized_text_key(expression), []).append(identity)
    return index


def entry_text_candidates(entries: Iterable[Any]) -> Dict[str, List[Identity]]:
    """Build the legacy lookup from RAW ``[[pattern]]`` tables."""
    return _text_candidates(entries, "pattern")


def runtime_text_candidates(
    entries: Iterable[Any],
) -> Dict[str, List[Identity]]:
    """Build the legacy lookup from BUILT runtime entries."""
    return _text_candidates(entries, "raw_pattern")


def is_own_rule(entry: Any) -> bool:
    """True when the Pattern Manager created this block as its own rule.

    Two conditions, and both are needed:

    ``origin`` is exactly ``ORIGIN_OWN``. The editor writes that value into
    every block it creates, so a block carrying it was never a pre-doc_id
    override and must not be handed a built-in by its expression text
    (wh-pattern-override-doc-id.3.3). A hand-written value does not count.

    The block names no built-in at all. A block carrying a ``doc_id`` key is
    claiming one, and a MALFORMED id has always fallen back to the text key
    rather than becoming a key of its own -- see ``identity_from``. Reading
    such a block as an independent rule would turn a hand edit of the id
    into the silent loss of a customization, which is the behaviour that
    fallback exists to prevent. Presence of the key, not its validity, is
    what the test asks: the person wrote an id, however badly.

    Accepts any object because a hand edit can put a non-table into the
    ``pattern`` array.
    """
    if not isinstance(entry, dict):
        return False
    return (
        entry.get(ORIGIN_KEY) == ORIGIN_OWN
        and DOC_ID_KEY not in entry
    )


def legacy_candidates(
    entry: Any,
    identity: Optional[Identity],
    candidates: Mapping[str, List[Identity]],
) -> List[Identity]:
    """Return the shipped identities a pre-doc_id override could belong to.

    Empty for an entry the editor created as its own rule, for one that
    names itself, for one that identifies as nothing, and for one whose text
    nothing shipped carries. Exactly one element is an unambiguous
    association and the only case a caller may migrate. More than one means
    the file does not say which built-in was meant, and the caller must
    leave the entry unresolved rather than guess -- a wrong guess hands a
    user's replacement to a command they never touched.

    An entry that names itself is answered by its name alone. Letting its
    text speak as well would let a user rule whose expression happens to
    equal a built-in's take that built-in over, which is the ambiguity the
    ``doc_id`` exists to remove.

    *entry* is required rather than optional so a caller cannot ask this
    question without supplying the block that answers the first of those
    conditions. Duplicate reached a built-in for exactly as long as the two
    were separable (wh-pattern-override-doc-id.3.3).
    """
    if is_own_rule(entry):
        return []
    if identity is None or identity[0] != TEXT_KIND:
        return []
    return list(candidates.get(identity[1], ()))
