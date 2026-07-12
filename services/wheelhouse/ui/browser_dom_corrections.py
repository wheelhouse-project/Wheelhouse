# -----------------------------------------------------------------------------
# Portions of this file are ported from CursorTouch/Operator-Use
# (https://github.com/CursorTouch/Operator-Use), used under the MIT License.
#
# Ported source: operator_use/computer/windows/tree/service.py -- specifically
#   the _dom_correction logic that folds Chromium UI Automation noise
#   (GroupControl->TextControl, ListItem->Hyperlink, Hyperlink->HeadingControl)
#   so a single semantic control survives the walk instead of its scaffolding.
# at pinned commit 67b2d4fd8fe755223fb21ab9bba104f24f4bb04b.
#
# Copyright (c) 2026 CursorTouch
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -----------------------------------------------------------------------------
"""Chromium browser DOM folding corrections for voice element clicking (wh-24e4w).

Chromium-family apps (real browsers plus Electron/CEF apps) expose a UI
Automation tree padded with semantic scaffolding: a ``GroupControl`` that
merely wraps one ``TextControl``, a ``ListItem`` whose only useful child is a
``Hyperlink``, a ``Hyperlink`` whose visible text is a nested
``HeadingControl``. The voice-clicking walker would otherwise emit BOTH the
wrapper and the inner control as separate numbered targets, doubling the
overlay and splitting the clear-winner vote. These three folding rules collapse
each pair to the single control the user actually means to click.

The authoritative spec is the v5 design doc,
docs/plans/2026-05-21-voice-element-clicking-design-v5.md, section
"Browser DOM corrections". The rules mirror Operator-Use's ``_dom_correction``
(pinned commit 67b2d4f) and run only when the foreground process is in the
effective browser-process list.

CONTRACT -- input data shape
============================
``ElementMatch`` (``ui/element_types.py``) is a FLAT frozen record: it carries
``name``, ``role``, ``bounds`` (x, y, w, h) and an opaque ``control_ref``, but
NO parent/child links. ``walk_window``'s ``browser_correction_hook`` is typed
``Callable[[list[ElementMatch]], list[ElementMatch]]`` -- it hands this module a
flat list and expects a (usually shorter) flat list back. The three folding
rules are phrased in parent/child terms, so this module RECOVERS the
ancestor/descendant relationship from geometry PLUS tree order. The walker emits
matches in UIA pre-order (FindAllBuildCache over TreeScope_Subtree yields parents
before children); element A is an ancestor of B when A's bounding rectangle
encloses B's (inclusive of equal edges), and when two different elements share an
IDENTICAL rect the tie is broken by tree order -- the earlier element is the
ancestor. A DIRECT descendant of A is a contained element that is not nested
inside another descendant of A; "only useful descendant" in the fold rules means
exactly one direct descendant. This keeps the module pure and unit-testable
against synthetic ``ElementMatch`` lists with NO comtypes, NO live COM, and NO
real Chromium app -- the design doc explicitly requires that testability. The
module never adds parent links to ``ElementMatch`` and never mutates its inputs;
it returns a new list of survivors with original order preserved.

RESIDUAL RISK (heuristic, not a guarantee)
==========================================
Geometric containment + tree order APPROXIMATES DOM ancestry; it is not a true
parent link. A pathological layout -- overlapping or z-ordered siblings whose
rects happen to nest and whose roles happen to match a fold pattern -- could fold
incorrectly (e.g. an unrelated text drawn inside a group's rect would be read as
the group's child). The correct fix is to carry the actual UIA parent (or a depth
index) on ``ElementMatch`` so ancestry is exact, but ``element_types.py`` is a
forbidden file for this slice, so that is out of scope here. A future slice
should add a parent/depth field to ``ElementMatch`` and switch the helpers below
to use it, retiring the geometry heuristic.

Role detection -- locale invariant for four of five roles (wh-l4h.1.12)
=======================================================================
``ElementMatch`` carries both ``role`` (the localized control-type string the
walker read from ``CachedLocalizedControlType``) and ``control_type_id`` (the
numeric UIA control-type id from ``CachedControlType``). The localized string
differs by Windows display language -- German emits "Gruppe", "Text", "Link",
"Listenelement" -- so matching on it silently fails on non-English Windows and
NOTHING folds. The numeric id is identical across display languages, so four of
the five role predicates (``_is_group``, ``_is_text``, ``_is_hyperlink``,
``_is_listitem``) compare ``control_type_id`` against the stable
UIA_*ControlTypeId values (Group 50026, Text 50020, Hyperlink 50005,
ListItem 50007). Those four are now locale-invariant.

``_is_heading`` is the one exception and CANNOT be made id-based the same way:
Windows UI Automation has NO Heading control-type id. Chromium exposes an ARIA
heading as a Text control (control-type id 50020) and distinguishes it ONLY via
the localized control-type string "heading" (or the UIA Level property, which
this slice does not cache). A numeric id of 50020 cannot tell a heading apart
from ordinary text, so ``_is_heading`` necessarily keeps matching the localized
"heading" string plus the canonical "HeadingControl" name. Rule 3's heading
detection therefore remains locale-DEPENDENT -- an explicit, narrowed residual
limitation. This is far smaller than the prior failure mode (all five rules
dead on non-English Windows): only the inner-heading half of Rule 3 degrades,
and the much more common Group/Text, ListItem/Hyperlink folds are unaffected.
"""

from __future__ import annotations

from ui.element_types import ElementMatch
from ui.uia_walker import (
    UIA_GROUP,
    UIA_HYPERLINK,
    UIA_LISTITEM,
    UIA_TEXT,
)

# Default Chromium-family starter list (v5 design "Browser DOM corrections").
# Real browsers plus Electron / CEF apps that share Chromium's UIA patterns.
# Stored verbatim (mixed case) as the design ships it; the effective-list
# helper lowercases for comparison so process-name matching is case-folded.
DEFAULT_BROWSER_PROCESSES: tuple[str, ...] = (
    # actual browsers
    "brave.exe",
    "chrome.exe",
    "msedge.exe",
    "vivaldi.exe",
    # Electron / Chromium-Embedded apps that share Chromium's UIA patterns
    "slack.exe",
    "discord.exe",
    "code.exe",
    "ms-teams.exe",
    "Teams.exe",
    "spotify.exe",
    "notion.exe",
    "obsidian.exe",
    "ChatGPT.exe",
)


def effective_browser_processes(
    browser_processes: list[str],
    browser_processes_extend: list[str] | None,
) -> list[str]:
    """Concatenate the starter list and the user extension list.

    Effective list = ``browser_processes`` + ``browser_processes_extend`` (v5
    design). The split lets the starter list grow across WheelHouse releases
    while user additions survive upgrades unchanged. Process names are compared
    case-insensitively elsewhere, so this helper lowercases every entry and
    de-duplicates case-insensitively, keeping first-seen order. ``None`` for the
    extension list is treated as empty.

    This is a pure helper: it takes the two lists as parameters and does NOT
    read config. Config plumbing (the ``[click]`` section, ``ClickConfig``)
    lives in a separate slice.
    """
    extend = browser_processes_extend or []
    seen: set[str] = set()
    out: list[str] = []
    for name in [*browser_processes, *extend]:
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return out


# ---------------------------------------------------------------------------
# Role predicates (wh-l4h.1.12).
#
# Four of the five compare the numeric UIA control-type id carried on
# ElementMatch.control_type_id, which is stable across Windows display languages
# -- so the folding rules fire on non-English Windows where the localized role
# string would not match. _is_heading is the exception: UIA has no Heading
# control-type id (Chromium exposes a heading as a Text control, id 50020), so
# it necessarily keeps matching the localized "heading" string and the canonical
# "HeadingControl" name. See the module docstring for the full rationale.
# ---------------------------------------------------------------------------

def _is_role_string(match: ElementMatch, *names: str) -> bool:
    role = match.role.strip().casefold()
    return role in {n.casefold() for n in names}


def _is_group(match: ElementMatch) -> bool:
    return match.control_type_id == UIA_GROUP


def _is_text(match: ElementMatch) -> bool:
    return match.control_type_id == UIA_TEXT


def _is_hyperlink(match: ElementMatch) -> bool:
    return match.control_type_id == UIA_HYPERLINK


def _is_listitem(match: ElementMatch) -> bool:
    return match.control_type_id == UIA_LISTITEM


def _is_heading(match: ElementMatch) -> bool:
    # UIA has no Heading control-type id; locale-dependent by necessity.
    return _is_role_string(match, "heading", "HeadingControl")


# ---------------------------------------------------------------------------
# Geometry: recover ancestry from bounds + tree order.
#
# The walker hands us matches in UIA pre-order (FindAllBuildCache walks
# TreeScope_Subtree, which yields parents before their children). We exploit
# that order to disambiguate the one case geometry alone cannot decide: when two
# DIFFERENT elements share an identical rect, containment is mutually true, so we
# break the tie by tree order -- the element earlier in the input list (the one
# the pre-order walk emitted first) is the ancestor. Containment is therefore
# expressed in terms of the elements' positions (indices) in the input list, not
# the bare rectangles.
# ---------------------------------------------------------------------------

def _rect_within(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    """True when ``inner`` lies within ``outer`` (inclusive of equal edges).

    Bounds are (x, y, width, height). Degenerate rects (zero/negative width or
    height on either rect) never participate -- a zero-area control has no
    meaningful child geometry. Identical rects ARE within each other here;
    the caller breaks that tie by tree order.
    """
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    if ow <= 0 or oh <= 0 or iw <= 0 or ih <= 0:
        return False
    return (
        ix >= ox
        and iy >= oy
        and ix + iw <= ox + ow
        and iy + ih <= oy + oh
    )


def _contains_idx(
    outer_idx: int,
    inner_idx: int,
    matches: list[ElementMatch],
) -> bool:
    """True when the element at ``outer_idx`` is an ancestor of ``inner_idx``.

    Tree order first (wh-9f3t.19.1): the walker emits matches in strict UIA
    pre-order, in which an ancestor ALWAYS precedes its descendants. So a later
    element (higher index) can never be the ancestor of an earlier one, even
    when it geometrically encloses it -- z-stacking and absolute positioning
    produce exactly that enclose-but-not-ancestor case. Returning False whenever
    ``outer_idx >= inner_idx`` enforces the pre-order direction unconditionally
    (and subsumes the old equal-rect tiebreak: of two coincident rects the
    earlier one is the ancestor). An element never contains itself.

    Geometry second: with the order constraint satisfied, ``inner``'s rect must
    lie within ``outer``'s (inclusive of equal edges, so coincident rects count
    as contained).
    """
    if outer_idx >= inner_idx:
        return False
    return _rect_within(matches[inner_idx].bounds, matches[outer_idx].bounds)


def _direct_descendant_indices(
    parent_idx: int,
    matches: list[ElementMatch],
    dropped: set[int],
) -> list[int]:
    """Indices of the DIRECT descendants of ``matches[parent_idx]``.

    D is a direct descendant of parent P when ``_contains_idx(P, D)`` is true AND
    no OTHER (live) element X has both ``_contains_idx(P, X)`` and
    ``_contains_idx(X, D)`` -- i.e. D is not nested inside another descendant of
    P. Elements whose ``id()`` is in ``dropped`` are treated as already removed:
    they are neither returned as descendants nor counted as the intermediate X
    that would shadow a deeper element. Skipping dropped elements is what lets
    same-rule scaffolding chains converge (wh-9f3t.19.2): once an inner
    GroupControl folds away, its GroupControl parent sees the inner TextControl
    as its OWN sole direct descendant and folds too. Computing this over the
    structural set (rather than a shrinking list) keeps the relationship stable
    while parents are visited child-first.
    """
    contained = [
        i
        for i in range(len(matches))
        if id(matches[i]) not in dropped and _contains_idx(parent_idx, i, matches)
    ]
    direct: list[int] = []
    for d in contained:
        nested = any(
            x != d and _contains_idx(x, d, matches) for x in contained
        )
        if not nested:
            direct.append(d)
    return direct


def _sole_direct_descendant(
    parent_idx: int,
    matches: list[ElementMatch],
    dropped: set[int],
) -> int | None:
    """Return the single live DIRECT-descendant index of the parent, else ``None``.

    Direct descendant is defined in ``_direct_descendant_indices`` (which ignores
    already-dropped elements): a still-live element contained by the parent that
    is not nested inside another live descendant of the parent. "Only useful
    descendant" in the v5 fold rules reads as "exactly one direct descendant".
    Returns ``None`` for zero or two-or-more direct descendants. (No assumption
    is made about the walker's own filtering; the structural test stands alone.)
    """
    kids = _direct_descendant_indices(parent_idx, matches, dropped)
    if len(kids) == 1:
        return kids[0]
    return None


def apply_dom_corrections(matches: list[ElementMatch]) -> list[ElementMatch]:
    """Apply the three Chromium folding rules, returning the surviving matches.

    Pure function. Does not mutate ``matches`` or any ElementMatch. Suitable as
    a ``browser_correction_hook`` for ``walk_window``. The three rules
    (v5 design "Browser DOM corrections"):

    1. ``GroupControl`` whose only useful (direct) descendant is one
       ``TextControl`` with the SAME name -> drop the group, keep the inner text.
    2. ``ListItem`` whose only useful (direct) descendant is a ``Hyperlink`` ->
       drop the list item, keep the hyperlink.
    3. ``Hyperlink`` containing a ``HeadingControl`` -> drop the heading, keep
       the outer hyperlink. (Rule 3 does not require the heading be the sole
       descendant -- a hyperlink may also wrap text/image children; any
       contained heading is folded away in favour of the hyperlink, matching
       Operator-Use's _dom_correction.)

    Survivors are returned in their original input order.

    Parents are visited in REVERSE input order -- post-order, children before
    parents (wh-9f3t.19.2). Combined with ``_sole_direct_descendant`` ignoring
    already-dropped elements, this lets same-rule scaffolding chains collapse in
    one pass: an inner ``GroupControl`` folds first, then its outer
    ``GroupControl`` sees the surviving inner ``TextControl`` as its own sole
    direct descendant and folds too, leaving only the text.

    Complexity: the containment scan is O(n^2) in the interactive-control count
    (each direct-descendant query is itself O(n^2) over the contained set), which
    is acceptable at the realistic tens-to-low-hundreds control counts of a
    single window and is dominated by the UIA walk that produced ``matches``. No
    caching or spatial indexing is added.
    """
    if not matches:
        return list(matches)

    dropped: set[int] = set()  # id() of ElementMatch objects to remove

    for parent_idx in range(len(matches) - 1, -1, -1):
        parent = matches[parent_idx]

        # Rule 1: group -> sole same-name text direct child.
        if _is_group(parent):
            child_idx = _sole_direct_descendant(parent_idx, matches, dropped)
            if child_idx is not None:
                child = matches[child_idx]
                if (
                    _is_text(child)
                    and child.name.strip().casefold() == parent.name.strip().casefold()
                ):
                    dropped.add(id(parent))
                    continue

        # Rule 2: list item -> sole hyperlink direct child.
        if _is_listitem(parent):
            child_idx = _sole_direct_descendant(parent_idx, matches, dropped)
            if child_idx is not None and _is_hyperlink(matches[child_idx]):
                dropped.add(id(parent))
                continue

        # Rule 3: hyperlink containing a heading -> drop the heading(s).
        if _is_hyperlink(parent):
            for kid_idx in range(len(matches)):
                if _contains_idx(parent_idx, kid_idx, matches) and _is_heading(
                    matches[kid_idx]
                ):
                    dropped.add(id(matches[kid_idx]))

    return [m for m in matches if id(m) not in dropped]
