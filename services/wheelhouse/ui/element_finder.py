"""ElementFinder coordinator for voice-driven UI element clicking (wh-agd2v).

This is the Input-process coordinator that wires together the already-built,
already-reviewed pure modules into one ``find()`` path:

    walk_window (ui.uia_walker)
      -> browser_dom_corrections.apply_dom_corrections  (Chromium-family only)
      -> confidence_scorer.is_eligible / .score          (eligible + scored)
      -> clear_winner_rule.decide                         (Outcome)

``ElementFinder.find(query, foreground)`` walks the focused window, applies the
browser DOM corrections when the foreground process is Chromium-family, scores
and filters the matches via the confidence scorer, runs ``decide``, stores the
resulting :class:`~ui.element_types.WalkSnapshot` in the bounded multi-snapshot
store (see below), builds the plain-data
:class:`~ui.element_types.WalkSnapshotSummary`, and returns a small frozen
:class:`FindResult` carrying the decide ``Outcome``, the Input-local
``WalkSnapshot``, and the display-safe ``WalkSnapshotSummary``.

Multi-snapshot store (wh-n29v.33, design v4 "Multi-snapshot store in
ElementFinder")
==================================================================
v1 kept a single ``self._stored`` slot that every new walk overwrote. This
coordinator keeps ``self._stored: dict[str, _StoredSnapshot]`` keyed by
``snapshot_id`` so several snapshots coexist (the numbered-overlay feature
re-reads an earlier walk's snapshot while later walks happen). Three eviction
rules interact:

* **Bounded LRU.** ``snapshot_store_capacity`` (default 4) bounds the store.
  ``find`` inserts the new snapshot as most-recently-used, then evicts the
  least-recently-used UNPINNED entries until the count is at or below capacity.
  ``get_snapshot`` marks a retrieved snapshot most-recently-used. A pinned
  entry is NEVER evicted by LRU; if every non-pinned slot is exhausted and only
  pinned entries remain over capacity, nothing is LRU-evicted (TTL bounds them
  instead). The snapshot a ``find`` just produced is likewise protected from
  its OWN insert's eviction pass, so it is always retrievable and pinnable when
  ``find`` returns -- otherwise, with every pre-existing slot pinned, the fresh
  unpinned entry would be the only LRU candidate and be dropped immediately,
  breaking the find()->pin() contract the overlay handlers depend on (and, at
  capacity=1, a single pin would make the store write-once until that pin's TTL
  elapsed). The store therefore sits at most ONE over capacity (the pins plus
  the fresh entry); the overage clears on the next ``find`` (the entry is then
  an older unpinned one) or when TTL reclaims a pinned slot (reviewer_2 finding
  36.1).
* **TTL.** BOTH ``find`` (before it inserts a new snapshot) and ``get_snapshot``
  (before resolving the requested id) sweep ALL stored snapshots and drop every
  one whose age has reached ``snapshot_ttl_seconds`` (fail-closed boundary:
  ``age >= ttl`` is already expired). TTL evicts PINNED snapshots too -- this is
  the stale-pin cleanup hard boundary if Logic dies (or merely fails to send)
  the unpin. Sweeping on the ``find`` path is what makes the boundary hold under
  find()-only traffic: the overlay numbered-click path (the only ``get_snapshot``
  caller) may never run again after the overlay is dismissed, but plain clicking
  keeps calling ``find``, so a stale pin is reclaimed on the next walk instead of
  leaking its COM keepalive for the life of the Input process (reviewer_0 round-1
  findings 34.1/34.2). The sweep is ACCESS-BASED, not a background timer:
  ElementFinder is a passive object with no event loop, so the boundary is "the
  next find()/get_snapshot once the TTL has elapsed", not a wall-clock-exact
  timer.
* **Pin.** The ``pinned`` flag is set ONLY via :meth:`pin` / :meth:`unpin`
  (never as a side effect of ``find`` or ``get_snapshot``). The pin blocks LRU
  eviction only, never TTL, and pinning does not refresh a snapshot's TTL.
  This slice provides the pin/unpin MECHANISM; the Logic-side state machine
  that decides when to pin/unpin via IPC actions is a separate slice.

Each ``_StoredSnapshot`` owns its own ``walk_results`` list keepalive (the
primary focused-window walk plus every owned-popup subtree walk, wh-n29v.45),
so evicting one entry releases only that entry's COM references; sibling
snapshots keep their ``control_ref`` proxies alive (per-snapshot keepalive
isolation).
``latest_snapshot`` / ``latest_summary`` report the most-recently-ADDED
snapshot (not the most-recently-LRU-accessed one); ``invalidate`` clears the
whole store and every keepalive.

This slice does NOT click anything (the ``ClickExecutor`` is a later slice,
wh-mzpvx) and does NOT read config files (config values arrive as constructor
parameters with the v5 defaults). The authoritative spec is the v5 design doc,
docs/plans/2026-05-21-voice-element-clicking-design-v5.md, sections "Key types"
and the find-flow pseudo-code.

THE LOAD-BEARING BROWSER WIRING MUST (see ``_walk_for``)
=======================================================
For a Chromium-family foreground process the walk MUST run with
``query_has_role=False`` so Text / Group / Heading controls survive the
walker's interactive-control filter into the ``browser_correction_hook``. The
walker applies its interactive filter BEFORE the browser correction hook, and
that filter drops Text / Group / Heading when ``query_has_role=True``. With the
hook wired post-filter in a role-query walk, browser fold rule 1 (Group->Text)
and rule 3 (Hyperlink->Heading) would have NO participants and be inert -- only
rule 2 (ListItem->Hyperlink) could fire. Walking the browser case with
``query_has_role=False`` keeps Text / Group / Heading alive so all three fold
rules are reachable; the confidence scorer's eligibility then drops the
non-matching leftovers. This was found in reviewer_0 of wh-24e4w and confirmed
independently by deepseek (reviewer_2 finding wh-9f3t.20.2); see the two
comments on bead wh-agd2v. Do NOT re-introduce the interactive filter for the
browser path -- that re-breaks rules 1 and 3.

COM-object lifetime (the dangling-pointer hazard, v5 "COM object lifetime")
===========================================================================
The ``Outcome`` and ``WalkSnapshot`` are Input-process-local: their
``winner`` / ``candidates`` / ``matches`` carry live COM ``control_ref``
handles that cannot be pickled across a process boundary. ElementFinder retains
the walker's ``WalkResult`` (and thus its four ``_keepalive_*`` COM references)
for the lifetime of the stored snapshot, so a later click does not hit a
released COM proxy. The plain-data ``WalkSnapshotSummary`` -- display-safe
primitives only -- is what later crosses to Logic/GUI; it is built at walk time
alongside the full snapshot.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from ui.browser_dom_corrections import (
    DEFAULT_BROWSER_PROCESSES,
    _rect_within,
    apply_dom_corrections,
    effective_browser_processes,
)
from ui.clear_winner_rule import Outcome, decide
from ui.confidence_scorer import is_eligible, score
from ui.element_types import (
    ElementMatch,
    ElementQuery,
    WalkSnapshot,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
from ui.uia_walker import (
    UIA_DATAITEM,
    UIA_EDIT,
    UIA_LISTITEM,
    WALK_TRANSIENT_RETRY_ATTEMPTS,
    WalkResult,
    is_interactive_control_type,
    walk_owned_popups,
    walk_window,
)
from ui.window_fallback import (
    FallbackWindow,
    enumerate_top_level_windows,
    order_candidates,
    resolve_monitor_rect,
    resolve_window_rect,
    restrict_to_monitor,
)

logger = logging.getLogger(__name__)

# The stale-window failure class for the fall-back walk (finding 46.1). When a
# candidate window closes between enumeration and its walk, the live-COM calls
# (ElementFromHandle / FindAllBuildCache) raise an OSError or a comtypes
# COMError. Only these are treated as "window closed, skip and continue"; a real
# programming error in score_hook / decide / browser correction must propagate.
try:  # comtypes is present on Windows; absent on headless test hosts.
    from comtypes import COMError as _COMError  # type: ignore[import-not-found]

    _STALE_WINDOW_ERRORS: tuple[type[BaseException], ...] = (OSError, _COMError)
except Exception:  # noqa: BLE001 -- no comtypes -> OSError covers the test fakes
    _STALE_WINDOW_ERRORS = (OSError,)

# v5 design defaults. Mirrored as constructor defaults so this module reads no
# config; the real values arrive from ClickConfig (a separate slice) at the
# call site.
_DEFAULT_SNAPSHOT_TTL_SECONDS = 30
# v4 multi-snapshot store default capacity (design v4 "Multi-snapshot store in
# ElementFinder"). The real value arrives from ClickConfig.snapshot_store_capacity
# at the call site; the production wiring keeps this default today.
_DEFAULT_SNAPSHOT_STORE_CAPACITY = 4
_DEFAULT_MIN_CONFIDENCE = 0.4
_DEFAULT_CLEAR_WINNER_MARGIN = 0.15
_DEFAULT_TIEBREAKER_INFLUENCE_LOGICAL_PX = 400.0
_DEFAULT_TIEBREAKER_MIN_SEPARATION_LOGICAL_PX = 30.0
_DEFAULT_MIN_SUBSTRING_QUERY_LENGTH = 4
_DEFAULT_MIN_SUBSTRING_OVERLAP_RATIO = 0.6
# Classic Win32 #32768 owned-popup walker extension (wh-n29v.45, design line
# 394): the primary focused-window walk uses the FULL per-request budget, but
# the owned-popup walk is performed only when the primary finished within this
# fraction of the budget -- "better to ship by-name results than time out". If
# the primary overran its share, the popup walk is skipped.
_POPUP_PRIMARY_DEADLINE_SHARE = 0.7
# wh-overlay-nested-dupes: the minimum inner/outer area ratio at which a
# contained clickable element counts as filling its container -- the pair is
# then ONE visual target and the container's badge is dropped. Calibrated
# against the real shapes: a Chromium wrapper div and the link inside it share
# 95-100% of their area; a link inside a list ROW covers well under 70%; a
# tab's close button covers a few percent of the tab. 0.90 sits inside the gap
# between "same visual control" and "distinct control inside a bigger one", so
# a tab keeps its badge next to its close button while a wrapper+link pair
# collapses to the link.
_NEAR_IDENTICAL_AREA_RATIO = 0.90
# wh-overlay-nested-dupes round 2 (Gmail left-nav double badges): the minimum
# share of a LATER clickable's area that must overlap an EARLIER clickable for
# the wrapper / same-name drop rules below to treat the pair as nested. An
# overlap share -- intersection area over the LATER element's area -- rather
# than strict containment, because Chromium reports some children sticking out
# of their wrapper (the Gmail search box overhangs its wrapper by 21px, share
# 0.70). 0.5 means "at least half inside": a genuinely contained control
# scores 1.0, adjacent controls with slightly sloppy bounds score near 0.
_WRAPPER_OVERLAP_SHARE = 0.5


def _overlap_share_of_later(
    outer: tuple[int, int, int, int],
    later: tuple[int, int, int, int],
) -> float:
    """Fraction of ``later``'s area that lies inside ``outer`` (0.0..1.0).

    Degenerate rectangles (zero/negative width or height) on either side
    return 0.0 so meaningless geometry can never trigger a drop.
    """
    ox, oy, ow, oh = outer
    lx, ly, lw, lh = later
    if ow <= 0 or oh <= 0 or lw <= 0 or lh <= 0:
        return 0.0
    x1 = max(ox, lx)
    y1 = max(oy, ly)
    x2 = min(ox + ow, lx + lw)
    y2 = min(oy + oh, ly + lh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return ((x2 - x1) * (y2 - y1)) / (lw * lh)


def collapse_near_identical_containers(
    matches: list[ElementMatch],
) -> list[ElementMatch]:
    """Drop container badges that duplicate a later clickable match.

    wh-overlay-nested-dupes: the walker numbers BOTH a container control and
    its interactive descendant (there is no identity / rectangle / ancestry
    de-duplication anywhere in the walk), so one visual control gets two
    numbered badges -- a ListItem and the Hyperlink filling it, a Chromium
    wrapper div exposing Invoke and the real link inside it. This pure pass
    collapses that on the OVERLAY path. An EARLIER match is dropped when a
    LATER match in the same walked window satisfies ANY of:

    1. **Near-identical rectangle** (round 1): the later match lies within
       the earlier's rectangle and covers at least
       ``_NEAR_IDENTICAL_AREA_RATIO`` of its area. Two clickable elements
       occupying (nearly) the same pixels are one visual target.
    2. **Unnamed wrapper** (round 2, Gmail left-nav evidence): the earlier
       match's accessible name is empty/whitespace and the later match lies
       at least ``_WRAPPER_OVERLAP_SHARE`` inside it. An unnamed clickable
       enclosing a real control is a Chromium wrapper div with a delegated
       click handler (a Gmail nav row: unnamed 819x83 Group wrapping the
       160x61 'Starred' link -- 14% coverage, invisible to rule 1). A voice
       user cannot name it, and its click action is normally reachable
       through the enclosed control.
    3. **Same accessible name** (round 2): the earlier and later match share
       the same non-empty name (strip + casefold) and the later lies at
       least ``_WRAPPER_OVERLAP_SHARE`` inside the earlier. Identical name +
       stacked geometry is one visual target twice (Gmail's 'Not starred'
       grid cell wrapping the 'Not starred' button); a voice user cannot
       tell them apart by name either.

    In every rule the inner, more specific element keeps the badge -- the
    same keep-the-inner direction as the browser fold rules.

    Deliberately conservative:

    * A NAMED container keeps its badge over a small differently-named child
      (a tab's close button is a distinct action from the tab; Gmail's
      'Select' button keeps its badge over its unnamed inner checkbox --
      rule 2 looks at the EARLIER element's name only).
    * Same-named controls that do not overlap (a 'Delete' button per row)
      never pair; rules 2 and 3 require the later element at least half
      inside the earlier one.
    * Pairs are only formed WITHIN one walked window
      (``source_window_hwnd``): pre-order ancestry holds within one subtree's
      match list, and a menu item painted OVER a page control must never
      collapse with it.
    * Degenerate rectangles never participate (``_rect_within`` and
      ``_overlap_share_of_later`` both refuse them, and a zero-area earlier
      match is skipped outright), so meaningless geometry cannot invent a
      drop.
    * Containment is judged against the ORIGINAL list, not the survivors, so
      a nested CHAIN (unnamed wrapper > same-named cell > control) collapses
      to the innermost leaf in one pass; drops only ever remove the EARLIER
      element of a pair, so the innermost element of a chain always
      survives.

    Accepted trade-offs (recorded on the bead): an unnamed wrapper whose
    click action has no equivalent enclosed control loses its badge; a grid
    row named with its full row text loses its badge to a same-named cell
    (the differently-named subject link inside keeps one, so the row's
    action stays reachable). The same-named-cell trade-off now applies to
    BROWSER walks only: on a native walk
    ``collapse_passive_cells_in_action_containers`` runs before this pass and
    removes the passive same-named cell first, so a native row named after its
    own cell keeps its badge rather than losing it to a no-action cell
    (reviewer_2 finding).

    Pre-order direction: the walker emits matches in UIA pre-order (ancestors
    before descendants), so only an EARLIER match can be the container of a
    later one -- the same direction rule the browser fold's ``_contains_idx``
    enforces. For coincident rectangles this treats the earlier element as the
    outer one; two z-stacked non-ancestor siblings with (nearly) identical
    rectangles would also pair, which is accepted -- the user cannot visually
    distinguish two fully overlapping controls either, and the click lands on
    the same pixels.

    Complexity: O(n^2) pairwise scan with an early break per dropped outer,
    over the already-filtered clickable set (tens to low hundreds of items) --
    the same accepted bound as ``apply_dom_corrections``' containment scan,
    pure in-memory arithmetic, no COM reads.
    """
    if len(matches) < 2:
        return list(matches)
    dropped: set[int] = set()
    for i, outer in enumerate(matches):
        _, _, outer_w, outer_h = outer.bounds
        outer_area = outer_w * outer_h
        if outer_area <= 0:
            continue
        outer_name = outer.name.strip().casefold()
        for j in range(i + 1, len(matches)):
            inner = matches[j]
            if inner.source_window_hwnd != outer.source_window_hwnd:
                continue
            # Rule 1: near-identical rectangle (strict containment + area
            # ratio, unchanged from round 1).
            if _rect_within(inner.bounds, outer.bounds):
                _, _, inner_w, inner_h = inner.bounds
                if inner_w * inner_h >= _NEAR_IDENTICAL_AREA_RATIO * outer_area:
                    dropped.add(i)
                    break
            # Rules 2 and 3: the later match at least half inside the
            # earlier one, and the earlier is unnamed (wrapper div) or both
            # share one accessible name (same visual target twice).
            share = _overlap_share_of_later(outer.bounds, inner.bounds)
            if share >= _WRAPPER_OVERLAP_SHARE:
                if not outer_name:
                    dropped.add(i)
                    break
                if outer_name == inner.name.strip().casefold():
                    dropped.add(i)
                    break
    if not dropped:
        return list(matches)
    return [m for i, m in enumerate(matches) if i not in dropped]


# wh-overlay-browser-dupes: the minimum intersection area, as a share of the
# SMALLER rectangle of the pair, at which an unnamed invoke-only element
# counts as a duplicate of a named control it overlaps. Share-of-smaller
# rather than containment because the real pair only partially overlaps in
# both directions: on x.com the named engagement button sticks out past its
# unnamed hover circle AND the circle sticks out past the button. Calibrated
# against the live walk (2026-07-05, Brave on x.com): reply pair 0.78, like
# 0.67, views 0.52 (the weakest real duplicate), bookmark / share / avatar
# 1.0; genuinely adjacent controls score near 0. 0.40 sits well under the
# weakest duplicate and far above adjacent-control slop.
_UNNAMED_DUP_OVERLAP_SHARE = 0.40
# reviewer_0 finding 2: overlap share of the smaller rectangle alone has no
# size-disparity bound -- a large unnamed clickable card (delegated click:
# open the item) containing a small named button of a DIFFERENT action
# (Buy) scores share 1.0. The pair is only a duplicate when the two
# rectangles are roughly the same size: every verified live duplicate sits
# in a 1.0-3.2x area-ratio band (views 1.17x, reply 1.77x, bookmark/Grok
# 3.2x), while an icon INSIDE a control is 20x+ (a clear-X in a search box)
# and a card around a button is 100x+. 6.0 splits the bands with margin.
_UNNAMED_DUP_AREA_RATIO_MAX = 6.0


def collapse_unnamed_invoke_duplicates_of_named_controls(
    matches: list[ElementMatch],
) -> list[ElementMatch]:
    """Drop unnamed invoke-only elements that duplicate a named control.

    wh-overlay-browser-dupes (x.com live evidence, 2026-07-05): Chromium
    exposes each tweet engagement action TWICE -- a named Button ("2 Replies.
    Reply") and, AFTER it in tree order, an unnamed Group carrying only
    InvokePattern (the 136x136 circular hover region around the icon). Both
    survive every existing rule, so one visual control got two badges:

    * ``collapse_near_identical_containers`` only ever drops the EARLIER
      match of a pair, and here the unnamed element is the LATER one.
    * Its rule 1 needs full containment, but the pair only PARTIALLY
      overlaps (the button sticks out past the circle's right edge).
    * Its rule 3 needs matching names; the circle has none.

    This pure pass drops a match when ALL of:

    * its accessible name is empty/whitespace (a voice user cannot ask for
      it by name),
    * its control type is NOT interactive -- it earned its badge only via
      ``invoke_supported`` (Chromium stamps Invoke on scaffolding divs with
      delegated click handlers),
    * it overlaps a NAMED, interactive-typed match in the same walked
      window by at least ``_UNNAMED_DUP_OVERLAP_SHARE`` of the SMALLER of
      the two rectangles, in EITHER tree order,
    * the two rectangles are roughly the same size -- neither area exceeds
      the other by more than ``_UNNAMED_DUP_AREA_RATIO_MAX`` (reviewer_0
      finding 2: a large unnamed clickable card containing a small named
      button of a different action is two real targets, not a duplicate),
    * and the named match is not an Edit (reviewer_0 finding 1: clicking a
      text input focuses it -- it does NOT perform the action of an
      unnamed clear-X / show-password glyph drawn inside it, so the
      shared-pixels justification below does not hold for text inputs).

    The named control keeps the badge -- a keep-the-named direction,
    deliberately different from the near-identical pass's keep-the-inner:
    when one element of a stacked pair is nameable and the other is not,
    the nameable one is strictly more useful to a voice user, and clicking
    either lands on the same pixels.

    Deliberately conservative:

    * Interactive control types are NEVER drop candidates, whatever their
      name: a web text input (Edit) inside a clickable wrapper, Gmail's
      unnamed CheckBox inside its named 'Select' button, and a hashtag
      Hyperlink inside a tweet all keep their badges by construction.
    * NAMED non-interactive elements are never drop candidates either --
      named containers stay the near-identical pass's business.
    * The anchor must be NAMED: two stacked unnamed clickables give the
      voice user nothing to prefer, so both keep badges (true full-coverage
      stacks still collapse via the near-identical pass).
    * Pairs form only WITHIN one walked window (``source_window_hwnd``);
      degenerate rectangles never participate on either side.
    * Runs AFTER ``collapse_near_identical_containers``, not before
      (reviewer_1/deepseek finding): that pass's unnamed-wrapper rule fires
      on ANY later element at least half inside the wrapper, so removing an
      unnamed invoke-only element first could disarm the rule's only
      trigger and resurrect a wrapper badge the shipped code collapsed.
      Running second, the near-identical pass's input is unchanged from
      shipped behavior, and this pass only removes badges from its
      survivors. Within this pass, drops are judged against its own input
      list.

    Accepted trade-off (recorded on the bead): a genuinely independent
    unnamed invoke-only control that substantially overlaps a named control
    loses its badge. That shape is indistinguishable from the hover-circle
    duplicate by geometry alone; the named control's click lands on the
    shared pixels, so the action normally stays reachable.

    Complexity: O(n^2) pairwise scan with an early break per dropped match,
    over the already-filtered clickable set -- the same accepted bound as
    the other collapse passes, pure in-memory arithmetic, no COM reads.
    """
    if len(matches) < 2:
        return list(matches)
    dropped: set[int] = set()
    for i, candidate in enumerate(matches):
        if candidate.name.strip():
            continue
        if is_interactive_control_type(
            candidate.control_type_id, query_has_role=True
        ):
            continue
        if not candidate.invoke_supported:
            continue
        _, _, cand_w, cand_h = candidate.bounds
        if cand_w * cand_h <= 0:
            continue
        for anchor in matches:
            if anchor is candidate:
                continue
            if anchor.source_window_hwnd != candidate.source_window_hwnd:
                continue
            if not anchor.name.strip():
                continue
            if not is_interactive_control_type(
                anchor.control_type_id, query_has_role=True
            ):
                continue
            if anchor.control_type_id == UIA_EDIT:
                continue
            _, _, anchor_w, anchor_h = anchor.bounds
            anchor_area = anchor_w * anchor_h
            if anchor_area <= 0:
                continue
            cand_area = cand_w * cand_h
            if (
                max(cand_area, anchor_area)
                > _UNNAMED_DUP_AREA_RATIO_MAX * min(cand_area, anchor_area)
            ):
                continue
            # _overlap_share_of_later(outer, later) is intersection over
            # LATER's area; intersection over the SMALLER area is the max
            # of the two directed shares.
            share_of_smaller = max(
                _overlap_share_of_later(anchor.bounds, candidate.bounds),
                _overlap_share_of_later(candidate.bounds, anchor.bounds),
            )
            if share_of_smaller >= _UNNAMED_DUP_OVERLAP_SHARE:
                dropped.add(i)
                break
    if not dropped:
        return list(matches)
    return [m for i, m in enumerate(matches) if i not in dropped]


# Passive "cell" control types. A Details-view or data-grid row exposes each
# column value as an Edit or DataItem child that has no click action of its own
# -- clicking one just selects the parent row. Numeric UIA_*ControlTypeId
# values, imported from the walker so a comtypes regeneration cannot drift them.
# Deliberately does NOT include CheckBox / RadioButton / Button / Hyperlink /
# TreeItem: those are genuine controls whose nested placement is a real second
# target (a checkbox inside a row, a close button inside a tab).
_PASSIVE_CELL_CONTROL_TYPE_IDS: frozenset[int] = frozenset({UIA_EDIT, UIA_DATAITEM})

# The container must be an actual list/grid ROW for its passive cells to
# collapse into it. A ListItem (File Explorer Details row) or DataItem (grid
# row) holds column cells that are pieces of the row. A clickable control that
# is NOT a row -- a Button, a Hyperlink -- is a single target, and a passive
# child of it (a web <input> inside a <div role="button">) is its own thing,
# not a cell, so it keeps its number. Numeric UIA_*ControlTypeId values.
_ROW_CONTAINER_CONTROL_TYPE_IDS: frozenset[int] = frozenset({UIA_LISTITEM, UIA_DATAITEM})

# A cell counts as "inside" its container when at least this fraction of the
# cell's area overlaps the container. Uses the round-2 overlap-share test, not
# strict containment, because a real Details-view cell can overhang the row's
# rectangle by a few pixels (File Explorer's Size cell sticks 12px past the
# row's right edge). High enough that a merely-adjacent element never pairs.
_CELL_CONTAINMENT_SHARE = 0.90


def collapse_passive_cells_in_action_containers(
    matches: list[ElementMatch],
) -> list[ElementMatch]:
    """Drop the number on a passive cell that is a part of a clickable container.

    wh-overlay-nested-dupes round 3. ``collapse_near_identical_containers``
    keeps the INNER element of a pair -- correct for a Chromium wrapper around a
    real link. A native Windows Details-view row is the opposite shape: the row
    is a clickable ``ListItem`` that carries the real action (Invoke +
    SelectionItem) and fully contains four smaller, differently-named cells
    (Name / Date / Type / Size) that are ``Edit`` controls with NO Invoke --
    pieces of the row, not their own targets. The round-1 area rule (inner far
    smaller than outer) and the round-2 name rules (the cells differ from the
    row's name) both miss them, so each row still shows five numbers.

    This pass removes ``inner``'s number when a DIFFERENT match ``outer`` in the
    same walked window satisfies ALL of:

    * ``outer`` exposes Invoke -- it is the real click target;
    * ``outer``'s control type is a list/grid ROW
      (``_ROW_CONTAINER_CONTROL_TYPE_IDS``: ListItem or DataItem). A clickable
      control that is not a row -- a Button, a Hyperlink -- never collapses a
      passive child: a web ``<input>`` inside a ``<div role="button">`` (a
      Button holding an Edit) is a real input, not a column cell, so it keeps
      its number even when an unrecognized Chromium app is walked as native
      (reviewer_1 finding);
    * ``inner``'s control type is in ``_PASSIVE_CELL_CONTROL_TYPE_IDS`` (a table
      cell type) and ``inner`` does NOT expose Invoke -- it does nothing on its
      own;
    * at least ``_CELL_CONTAINMENT_SHARE`` of ``inner``'s area overlaps
      ``outer`` (the round-2 overlap-share test, not strict containment,
      because a real Details-view cell can overhang the row's rectangle by a
      few pixels -- File Explorer's Size cell sticks 12px past the row's right
      edge);
    * ``inner`` is clearly SMALLER than ``outer`` (area ratio below
      ``_NEAR_IDENTICAL_AREA_RATIO``). A same-size inner (an address-bar Edit
      filling its Group) is left to ``collapse_near_identical_containers`` so
      the two passes never both claim one pair.

    Kept on purpose:

    * A small inner control WITH its own Invoke (a column header's filter
      dropdown, a tab's close button, a row's inline delete button): a distinct
      action, so it keeps its number.
    * A checkbox / radio / other non-cell control inside a row: only the two
      cell types are eligible, so a Toggle-only checkbox is never dropped.
    * A cell inside a container that is NOT itself clickable (a plain List /
      Pane with no Invoke): no container number to duplicate, so the cell keeps
      its own.
    * Tree items: a child ``TreeItem`` is drawn BELOW its parent, not within it,
      so their rectangles do not overlap and both keep numbers.

    Overlay-only, like ``collapse_near_identical_containers``: the by-name
    ``find()`` path never collapses (a spoken name may match a cell's text).
    Pairs form only within one walked window (``source_window_hwnd``).

    Complexity: O(n^2) pairwise scan over the already-filtered clickable set,
    pure in-memory arithmetic, no COM reads -- the same accepted bound as the
    sibling collapse pass.
    """
    if len(matches) < 2:
        return list(matches)
    dropped: set[int] = set()
    for j, inner in enumerate(matches):
        if inner.control_type_id not in _PASSIVE_CELL_CONTROL_TYPE_IDS:
            continue
        if inner.invoke_supported:
            continue
        _, _, inner_w, inner_h = inner.bounds
        inner_area = inner_w * inner_h
        if inner_area <= 0:
            continue
        for k, outer in enumerate(matches):
            if k == j:
                continue
            if not outer.invoke_supported:
                continue
            if outer.control_type_id not in _ROW_CONTAINER_CONTROL_TYPE_IDS:
                continue
            if outer.source_window_hwnd != inner.source_window_hwnd:
                continue
            _, _, outer_w, outer_h = outer.bounds
            outer_area = outer_w * outer_h
            if outer_area <= 0:
                continue
            share = _overlap_share_of_later(outer.bounds, inner.bounds)
            if share < _CELL_CONTAINMENT_SHARE:
                continue
            if inner_area >= _NEAR_IDENTICAL_AREA_RATIO * outer_area:
                continue
            dropped.add(j)
            break
    if not dropped:
        return list(matches)
    return [m for i, m in enumerate(matches) if i not in dropped]


@dataclass(frozen=True)
class ForegroundContext:
    """The focused-window identity + cursor sampled at the moment of a walk.

    The caller (UIActionHandler, in the production path) snapshots the
    foreground window's identity and the cursor position BEFORE the walk and
    hands them in, so the WalkSnapshot records exactly the state the walk ran
    against. ``top_level`` is the HWND (int) or already-resolved UI Automation
    element the walker walks; tests pass a fake element directly.

    ``foreground_process_name`` decides whether the browser DOM-corrections
    hook is wired at all (Chromium-family -> wired with query_has_role=False;
    everything else -> not wired). ``cursor_at_walk`` feeds the clear-winner
    tiebreaker.

    ``cursor_monitor_id`` is retained for the later production-wiring slice's
    contract, but ElementFinder does NOT use the passed value for the
    cross-monitor gate: it RECOMPUTES the cursor's monitor from
    ``cursor_at_walk`` via the injected ``monitor_resolver`` so the cursor and
    each candidate share one monitor-id namespace (reviewer_1 finding 25.1).
    The recomputed value is authoritative and is what lands on the stored
    ``WalkSnapshot.cursor_monitor_id``.
    """

    foreground_window: int
    foreground_pid: int
    foreground_process_name: str
    foreground_window_creation_time: int
    cursor_at_walk: tuple[int, int]
    cursor_monitor_id: int
    top_level: Any = None


@dataclass(frozen=True)
class OverlayWalkResult:
    """What ``ElementFinder.overlay_walk`` returns: outcome + the snapshot.

    Input-process-local, like :class:`FindResult`: ``snapshot`` carries live
    COM ``control_ref`` handles (via ElementMatch) and must NOT cross a process
    boundary as-is; ``summary`` is the plain-data projection that does.

    Unlike ``find``, the overlay walk numbers EVERY interactive control in the
    focused window 1..K and runs no clear-winner decision (there is no spoken
    name to match), so this carries a small overlay-specific ``outcome`` string
    instead of a ``clear_winner_rule.Outcome``:

        * ``"ok"``               -- the walk produced >= 1 interactive control.
        * ``"no_targets"``       -- the walk produced zero interactive controls.
        * ``"execution_failed"`` -- the walk could not complete (e.g. the
          per-request deadline cut it short). ``reason`` carries the tag.

    Fields:
        outcome: one of ``"ok"`` / ``"no_targets"`` / ``"execution_failed"``.
        reason: an open tag for a non-ok outcome (e.g.
            ``walk_deadline_exceeded``), or ``None`` on ``ok`` / ``no_targets``.
        snapshot: the full Input-local :class:`WalkSnapshot`, or ``None`` when
            no snapshot was produced (a walk-time failure before insert).
        summary: the display-safe :class:`WalkSnapshotSummary`, or ``None`` on a
            walk-time failure. On ``ok`` / ``no_targets`` it is populated (the
            ``no_targets`` summary has an empty ``items`` list).
        _walk_results: the FULL list of walker :class:`WalkResult`s this result's
            snapshot was built from -- the primary focused-window walk first,
            then every owned-popup subtree walk -- pinned so a consumer that
            retains this result across a LATER walk keeps every popup subtree's
            COM ``control_ref`` proxies alive, independent of ``self._stored``
            (which only pins the latest walk). Unlike ``FindResult._walk_result``
            (singular: the winner's walk), the overlay numbers the whole set with
            no winner, so it pins the whole set -- the same WalkResult list the
            stored ``_StoredSnapshot`` holds. Excluded from repr and equality.
    """

    outcome: str
    reason: Optional[str]
    snapshot: Optional[WalkSnapshot]
    summary: Optional[WalkSnapshotSummary]
    _walk_results: tuple[WalkResult, ...] = field(
        repr=False, compare=False, default=()
    )


@dataclass(frozen=True)
class FindResult:
    """What ``ElementFinder.find`` returns: the decision + both snapshots.

    Input-process-local. ``outcome`` and ``snapshot`` carry live COM
    ``control_ref`` handles (via ElementMatch) and must NOT cross a process
    boundary as-is; the ``summary`` is the plain-data projection that does.

    COM-keepalive invariant (wh-agd2v reviewer_0 finding 24.2): holding a
    FindResult keeps THIS result's snapshot's COM ``control_ref`` proxies alive
    -- independent of any later ``find()`` call -- because ``_walk_result``
    pins the walker's four ``_keepalive_*`` references. The coordinator's
    ``self._stored`` only pins the LATEST walk; a consumer that retains an
    older FindResult across a subsequent walk would otherwise lose the array
    keepalive and dangle on a later Invoke. ``_walk_result`` closes that gap.
    FindResult therefore remains Input-process-local and must NOT cross IPC.

    Fields:
        outcome: the ``clear_winner_rule.Outcome`` (ok / not_found / ambiguous
            / execution_failed), carrying winner + candidates as ElementMatch.
        snapshot: the full Input-local :class:`WalkSnapshot`.
        summary: the display-safe :class:`WalkSnapshotSummary`; its
            ``snapshot_id`` equals ``snapshot.snapshot_id``.
        _walk_result: the walker's :class:`WalkResult`, pinned so this
            result's snapshot keepalive survives later walks. Excluded from
            repr and equality (it is COM-bearing bookkeeping, not identity).
    """

    outcome: Outcome
    snapshot: WalkSnapshot
    summary: WalkSnapshotSummary
    _walk_result: WalkResult = field(repr=False, compare=False)


@dataclass
class _StoredSnapshot:
    """One stored snapshot plus the WalkResults that keep its COM proxies alive.

    Retaining the ``WalkResult`` list (not just each ``matches`` list) is the v5
    COM keepalive contract: the four ``_keepalive_*`` references on each
    WalkResult hold the IUIAutomation root, the cache request, the element
    array, and the top-level element, so every match's ``control_ref`` survives
    until this stored snapshot is evicted/invalidated. Each entry in the
    multi-snapshot store owns its OWN ``walk_results`` list, so evicting one
    entry releases only that entry's COM references (per-snapshot keepalive
    isolation).

    ``walk_results: list[WalkResult]`` (was a singular ``walk_result`` in
    Phase 1; wh-n29v.45) so the classic Win32 ``#32768`` popup-walker extension
    can pin the PRIMARY focused-window walk AND every owned-popup subtree walk
    TOGETHER. The list always holds the primary walk first; any owned-popup
    walks follow. A snapshot with no popups has a one-element list, exactly the
    Phase 1 keepalive set. Dropping the stored entry drops the whole list, so
    every subtree's keepalive chain is released together and only this
    snapshot's references are released (sibling snapshots keep theirs).

    ``pinned`` is the explicit LRU-immunity flag (wh-n29v.33). It is set only by
    :meth:`ElementFinder.pin` / :meth:`ElementFinder.unpin`, never as a side
    effect of ``find`` or ``get_snapshot``. A pinned entry is skipped by LRU
    eviction but is still subject to TTL eviction (stale-pin cleanup).

    ``ttl_anchor`` is the monotonic timestamp the TTL is measured from
    (wh-overlay-snapshot-keepalive). It starts at the snapshot's walk time
    (``created_at_monotonic``) and is slid forward to "now" by
    :meth:`ElementFinder.refresh_snapshot_ttl` when the Logic-side overlay
    keepalive reports the snapshot is still visible. ``_sweep_ttl`` measures age
    from THIS field, NOT from ``snapshot.created_at_monotonic`` -- the snapshot's
    walk time stays immutable (it is copied into the cross-process summary), so
    the sliding TTL window is tracked here on the mutable store entry instead.
    """

    snapshot: WalkSnapshot
    summary: WalkSnapshotSummary
    walk_results: list[WalkResult] = field(repr=False)
    ttl_anchor: float
    pinned: bool = False


class ElementFinder:
    """Coordinator that composes the find() path and stores recent snapshots.

    Stores up to ``snapshot_store_capacity`` recent :class:`WalkSnapshot`
    objects in a bounded LRU + TTL multi-snapshot store keyed by ``snapshot_id``
    (see the module docstring for the eviction rules and the pin mechanism).

    All thresholds and the DPI / browser-process configuration are injected as
    constructor parameters defaulting to the v5 design defaults, so the
    coordinator reads no config. ``walk_fn``, ``dpi_resolver`` and ``clock`` are
    injectable so composition tests drive fakes with no real display.
    """

    def __init__(
        self,
        *,
        snapshot_ttl_seconds: int = _DEFAULT_SNAPSHOT_TTL_SECONDS,
        snapshot_store_capacity: int = _DEFAULT_SNAPSHOT_STORE_CAPACITY,
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        clear_winner_margin: float = _DEFAULT_CLEAR_WINNER_MARGIN,
        tiebreaker_influence_logical_px: float = _DEFAULT_TIEBREAKER_INFLUENCE_LOGICAL_PX,
        tiebreaker_min_separation_logical_px: float = _DEFAULT_TIEBREAKER_MIN_SEPARATION_LOGICAL_PX,
        min_substring_query_length: int = _DEFAULT_MIN_SUBSTRING_QUERY_LENGTH,
        min_substring_overlap_ratio: float = _DEFAULT_MIN_SUBSTRING_OVERLAP_RATIO,
        browser_processes: Optional[list[str]] = None,
        browser_processes_extend: Optional[list[str]] = None,
        dpi_resolver: Callable[[int], float],
        monitor_resolver: Callable[[tuple[int, int, int, int]], int],
        clock: Callable[[], float] = time.monotonic,
        walk_fn: Callable[..., WalkResult] = walk_window,
        popup_walk_fn: Callable[..., list[WalkResult]] = walk_owned_popups,
        walk_deadline_ms: Optional[float] = None,
        automation: Any = None,
        window_enumerator: Callable[
            [], list[FallbackWindow]
        ] = enumerate_top_level_windows,
        monitor_rect_resolver: Callable[
            [tuple[int, int, int, int]], Optional[tuple[int, int, int, int]]
        ] = resolve_monitor_rect,
        focused_window_rect_resolver: Callable[
            [int], Optional[tuple[int, int, int, int]]
        ] = resolve_window_rect,
        enable_offmonitor_fallback: bool = False,
    ) -> None:
        self._snapshot_ttl_seconds = snapshot_ttl_seconds
        # Bounds the multi-snapshot store; clamped to at least 1 so the store can
        # always hold the snapshot a find() just produced.
        self._snapshot_store_capacity = max(1, snapshot_store_capacity)
        self._min_confidence = min_confidence
        self._clear_winner_margin = clear_winner_margin
        self._tiebreaker_influence_logical_px = tiebreaker_influence_logical_px
        self._tiebreaker_min_separation_logical_px = (
            tiebreaker_min_separation_logical_px
        )
        self._min_substring_query_length = min_substring_query_length
        self._min_substring_overlap_ratio = min_substring_overlap_ratio
        # The effective (lowercased, de-duped) browser-process set, computed
        # once from the starter list + the user extension list.
        starter = (
            list(browser_processes)
            if browser_processes is not None
            else list(DEFAULT_BROWSER_PROCESSES)
        )
        self._browser_processes = set(
            effective_browser_processes(starter, browser_processes_extend)
        )
        self._dpi_resolver = dpi_resolver
        # Resolves a per-match monitor_id from an ElementMatch.bounds
        # (x, y, w, h). v5 defines monitor_id as the monitor the bounds CENTRE
        # falls on, and decide()'s tiebreaker cross-monitor gate depends on it
        # being per-match (reviewer_0 finding 24.1). The production wiring slice
        # injects the real Win32 resolver -- see
        # services/wheelhouse/shared/monitor_geometry.py:_resolve_target_monitor
        # (largest-overlap monitor for a physical rect; note its rect is
        # left/top/right/bottom, so that slice converts x/y/w/h -> l/t/r/b).
        # Kept required + injectable here so headless tests need no real display.
        self._monitor_resolver = monitor_resolver
        self._clock = clock
        self._walk_fn = walk_fn
        # Injected owned-popup subtree walker (wh-n29v.45). Default is the real
        # uia_walker.walk_owned_popups; tests inject a fake. find() calls it with
        # the focused HWND, the PRIMARY walk's automation root + cache_request
        # (so every subtree shares ONE CacheRequest, design line 392), the same
        # score_hook + browser wiring as the primary walk, and the one shared
        # per-request deadline. It returns one WalkResult per owned popup that
        # produced a usable (non-truncated) walk.
        self._popup_walk_fn = popup_walk_fn
        # Bounds the Input-side UIA walk so it cannot keep the command-reader
        # loop blocked past the Logic-side click awaiter ([click]
        # response_timeout_ms). This is the DURATION (ms); find() turns it into
        # ONE absolute per-request deadline -- preferring a caller-supplied
        # dequeue-anchored deadline, else deriving it from this duration -- and
        # passes that single deadline into every walk_window call (focused +
        # each fall-back) so the worst case is one budget, not (1+N) budgets
        # (FINDING 1). None (the default) disables the bound -- the walk
        # processes the whole subtree, preserving the pre-wh-9f3t.54.2
        # behaviour. The production wiring
        # (UIActionHandler._get_click_element_finder) feeds the validated
        # ClickConfig.walk_deadline_ms here AND anchors the absolute deadline at
        # command-dequeue, passing it to find(deadline=...).
        self._walk_deadline_ms = walk_deadline_ms
        self._automation = automation
        # Injected enumerator of visible top-level windows for the v5 restricted
        # fall-back (wh-86qdm). Real-Win32 default; fakes injected in tests so
        # the coordinator needs no real display. enable_offmonitor_fallback is
        # the v5 [click] gate (default False): the call-site wiring that feeds
        # the real ClickConfig value is a separate slice (wh-tab7j), so here it
        # only exists as a defaulted constructor parameter.
        self._window_enumerator = window_enumerator
        # Maps a physical box (the focused window's rect, or a 1x1 cursor box)
        # to that monitor's screen RECTANGLE, so the very-small overlay test and
        # the same-monitor overlap test measure against the focused window's
        # MONITOR rather than the window itself or the cursor box (finding 45.2).
        # Real-Win32 default; injectable fake for tests.
        self._monitor_rect_resolver = monitor_rect_resolver
        # Resolves the focused window's own screen rect (x, y, w, h) from its
        # HWND, independent of the candidate enumeration, so the focused monitor
        # anchors to the real focused window rather than the cursor's monitor
        # (finding 46.2). Returns None on failure -> fail closed. Real-Win32
        # default; injectable fake for tests.
        self._focused_window_rect_resolver = focused_window_rect_resolver
        self._enable_offmonitor_fallback = enable_offmonitor_fallback

        # Monotonic counter for unique snapshot ids within this run, plus a
        # per-instance salt so ids are effectively unique ACROSS runs
        # (wh-pin-snapshot-contract-break-detection.1.3): a bare walk-{n}
        # counter restarts at 1 when the Input process crashes and restarts,
        # re-minting an id the Logic-side pin bookkeeping may still hold --
        # a refresh would then unpin the newly visible snapshot while every
        # id-based audit sees a single legitimate id. The salt is the FULL
        # uuid4 hex (122 random bits), not a truncation: reviewer finding
        # .3.1 -- a 6-hex (24-bit) salt left a ~1-in-16.7M per-restart-pair
        # collision chance, the wrong trade for an id whose only cost is
        # log-line length.
        self._snapshot_salt = uuid.uuid4().hex
        self._snapshot_counter = itertools.count(1)
        # Bounded multi-snapshot store keyed by snapshot_id. Dict insertion order
        # IS the LRU recency order: find() and a get_snapshot() hit move their
        # entry to the END (most-recently-used); LRU eviction scans from the
        # FRONT (least-recently-used). Each entry owns its own WalkResult, so
        # dropping a dict entry releases only that entry's COM keepalive.
        self._stored: dict[str, _StoredSnapshot] = {}
        # The snapshot_id of the most-recently-ADDED snapshot, tracked separately
        # from LRU recency so latest_snapshot()/latest_summary() report the
        # newest snapshot even after a get_snapshot() touches an older one.
        self._latest_id: Optional[str] = None

    # -- public query surface -------------------------------------------------

    def find(
        self,
        query: ElementQuery,
        foreground: ForegroundContext,
        *,
        deadline: Optional[float] = None,
    ) -> FindResult:
        """Walk the focused window for ``query`` and decide the outcome.

        Before inserting, sweeps TTL across the existing store -- dropping every
        expired entry (pinned or not) and its COM keepalive -- so the path that
        GROWS the store is also the path that reclaims stale entries under
        find()-only traffic, not only ``get_snapshot`` (reviewer_0 round-1
        findings 34.1/34.2). Then inserts the resulting WalkSnapshot into the
        bounded multi-snapshot store (as most-recently-used, ``pinned=False``),
        evicts the least-recently-used UNPINNED entries until the store is at or
        below ``snapshot_store_capacity``, records it as the newest snapshot, and
        returns the decide Outcome plus the full snapshot and the plain-data
        summary. Does NOT click. The ``pinned`` flag is never set here -- only
        :meth:`pin` / :meth:`unpin` touch it.

        Per-request walk deadline (wh-9f3t.54.2)
        ========================================
        ``deadline`` is a SINGLE ABSOLUTE monotonic timestamp (the clock
        ``self._clock`` reads). It bounds the TOTAL time spent across the
        focused-window walk AND every fall-back-window walk -- it is computed
        ONCE here and passed unchanged into each ``walk_window`` call, so the
        worst-case block is one deadline, not (1+N) per-window budgets
        (FINDING 1). The production caller (UIActionHandler) anchors this
        deadline at command-dequeue time and passes it in, so the pre-walk
        SharedMemory / foreground-capture / ElementFromHandle latency is
        charged against the budget too and the walk gives up before the Logic
        awaiter's IPC-send-anchored timeout (FINDING 3).

        When ``deadline`` is None and the finder was constructed with a
        ``walk_deadline_ms``, ``find`` anchors the deadline HERE at
        ``self._clock() + walk_deadline_ms/1000`` as a fallback (covers callers
        that have not adopted the dequeue-anchored deadline). When both are
        absent, no bound applies.

        Fail-closed on truncation: any walk the deadline cut short is treated as
        not_found (the matches are a partial prefix and must never reach
        clear-winner), so a deadline can only DOWNGRADE the outcome to
        not_found, never produce a wrong "ok" winner (FINDING 2).
        """
        score_hook = self._make_score_hook(query)

        # Capture the request start so the owned-popup split-deadline (design
        # line 394) can measure the primary walk's 0.7-share checkpoint against
        # it. Read once from the injected clock.
        walk_start = self._clock()

        # Anchor the ONE per-request deadline. Prefer the caller-supplied
        # (dequeue-anchored) absolute timestamp; else derive it from the
        # configured walk_deadline_ms; else None (no bound).
        if deadline is None and self._walk_deadline_ms is not None:
            deadline = walk_start + (self._walk_deadline_ms / 1000.0)

        focused_top_level = (
            foreground.top_level
            if foreground.top_level is not None
            else foreground.foreground_window
        )

        # Cross-monitor-gate namespace consistency (reviewer_1 finding 25.1):
        # each candidate's monitor_id comes from self._monitor_resolver (in the
        # score_hook), so the cursor's monitor_id fed to decide() MUST come from
        # the SAME resolver or the cross-monitor gate (and the DPI key) compares
        # ids from two different namespaces. We therefore RECOMPUTE the cursor
        # monitor here from foreground.cursor_at_walk via the same resolver,
        # passing the cursor as a 1x1 box (cx, cy, 1, 1).
        #
        # Why 1x1, not 0x0 (reviewer_2 finding 26.1): the documented production
        # resolver (shared/monitor_geometry.py:_resolve_target_monitor) assigns
        # a monitor by rectangle OVERLAP AREA, and _overlap_area returns 0 for
        # any box with width<=0 or height<=0. A zero-area box (cx, cy, 0, 0)
        # therefore overlaps EVERY monitor by 0 and the resolver falls through
        # to the primary monitor -- so a cursor on a secondary display would
        # resolve to the primary, breaking the gate and the DPI key. A 1x1 box
        # has overlap 1 with the monitor containing the cursor and 0 with every
        # other monitor, so an overlap-area resolver picks the correct monitor;
        # a point-containment resolver is also correct for a 1x1 box. The
        # _centre shift from (cx, cy) to (cx+0.5, cy+0.5) is half a pixel --
        # invisible to the ~30px tiebreaker separation threshold.
        # ForegroundContext.cursor_monitor_id is kept for the later wiring
        # slice's contract but is NOT used for the gate; the resolver-derived
        # value is authoritative.
        cx, cy = foreground.cursor_at_walk
        cursor_monitor_id = self._monitor_resolver((cx, cy, 1, 1))

        # Focus first (v5 "Window targeting"): walk the focused window.
        outcome, walk_result, scored = self._walk_and_decide(
            focused_top_level,
            query=query,
            process_name=foreground.foreground_process_name,
            cursor_at_walk=foreground.cursor_at_walk,
            cursor_monitor_id=cursor_monitor_id,
            score_hook=score_hook,
            deadline=deadline,
        )

        # The list of WalkResults this snapshot must pin together: the PRIMARY
        # walk first, then every owned-popup subtree walk (wh-n29v.45). The
        # primary walk's WalkResult always leads; popups append.
        walk_results: list[WalkResult] = [walk_result]

        # Owned-popup walk (wh-n29v.45). Run it against the FOCUSED window's
        # owned #32768 / UIA-Menu popups, sharing the PRIMARY walk's automation
        # root + cache_request so every subtree uses ONE CacheRequest (design
        # line 392), BEFORE the fall-back: popups belong to the focused window,
        # and the focused walk's cache_request is the one to share. Skipped when
        # the focused walk was deadline-truncated (no usable cache_request) or
        # when the primary overran its 0.7 share of the budget (design line 394;
        # "better to ship by-name results than time out"). Popup matches are
        # MERGED into the scored set -- focused matches first in reading order,
        # popup matches appended in their own order, focused badge numbers
        # unchanged -- and the combined set is re-decided so a popup menu item
        # can win the by-name click.
        if not walk_result.deadline_truncated and self._popup_share_allows(
            deadline, walk_start
        ):
            # Same browser wiring as the primary walk (_walk_and_decide): for a
            # Chromium-family foreground process query_has_role is False, which
            # keeps Text / Group / Heading wrappers alive through the walker's
            # interactive filter -- so the browser DOM-correction hook MUST run
            # to fold that scaffolding, exactly as the primary does. Passing
            # browser_correction_hook=None here (the old bug, wh-n29v.47.1) left
            # the wrappers unfolded so browser scaffolding reached decide() as
            # scoring candidates. is_browser is computed once and feeds BOTH
            # query_has_role and the hook so the two can never disagree.
            popup_is_browser = self._is_browser_process(
                foreground.foreground_process_name
            )
            popup_results = self._popup_walk_fn(
                foreground.foreground_window,
                automation=self._automation,
                cache_request=walk_result._keepalive_cache_request,
                query_has_role=self._query_has_role(query, popup_is_browser),
                monitor_id=cursor_monitor_id,
                browser_correction_hook=(
                    apply_dom_corrections if popup_is_browser else None
                ),
                score_hook=score_hook,
                deadline=deadline,
                clock=self._clock,
            )
            if popup_results:
                walk_results.extend(popup_results)
                scored, outcome = self._merge_popups_and_decide(
                    focused_scored=scored,
                    popup_results=popup_results,
                    cursor_at_walk=foreground.cursor_at_walk,
                    cursor_monitor_id=cursor_monitor_id,
                )

        # Restricted fall-back (v5): ONLY when the focused-window walk (plus any
        # owned-popup merge) yields no usable match (decide() == not_found).
        # ok / ambiguous / execution_failed all mean a decision was produced --
        # the fall-back must not run, so no enumeration happens in those cases.
        if outcome.outcome == "not_found":
            fb = self._run_fallback(
                query=query,
                foreground=foreground,
                cursor_monitor_id=cursor_monitor_id,
                score_hook=score_hook,
                deadline=deadline,
            )
            if fb is not None:
                fb_outcome, fb_walk_result, fb_scored = fb
                outcome, scored = fb_outcome, fb_scored
                # The fall-back winner comes from a DIFFERENT window; its
                # WalkResult replaces the focused+popup keepalive set because the
                # snapshot's matches are now the fall-back window's matches and a
                # later click Invokes a control from THAT walk. Pin only the
                # fall-back walk so the snapshot's COM chain matches its matches.
                walk_results = [fb_walk_result]

        snapshot_id = f"walk-{self._snapshot_salt}-{next(self._snapshot_counter)}"
        created_at = self._clock()
        snapshot = WalkSnapshot(
            snapshot_id=snapshot_id,
            matches=scored,
            created_at_monotonic=created_at,
            foreground_window=foreground.foreground_window,
            foreground_pid=foreground.foreground_pid,
            foreground_process_name=foreground.foreground_process_name,
            foreground_window_creation_time=foreground.foreground_window_creation_time,
            cursor_at_walk=foreground.cursor_at_walk,
            cursor_monitor_id=cursor_monitor_id,
        )
        summary = self._build_summary(snapshot)

        # Sweep expired entries (including stale PINNED ones) BEFORE inserting
        # the new snapshot. find() is the path that GROWS the store and the one
        # that keeps running after the overlay is dismissed (plain clicking
        # keeps walking), so sweeping here is what makes the TTL boundary hold
        # under find()-only traffic. Without it, _sweep_ttl ran ONLY in
        # get_snapshot, so a pinned snapshot whose unpin was lost leaked its COM
        # keepalive for the life of the Input process whenever the overlay
        # get_snapshot path was never exercised again (reviewer_0 round-1
        # findings 34.1/34.2). The new snapshot is inserted AFTER the sweep, so
        # a find() never sweeps the snapshot it just produced; the sweep, the
        # insert, and the LRU eviction together are the store-maintenance step.
        self._sweep_ttl()

        # Insert into the multi-snapshot store as the most-recently-used entry
        # (appended last in dict order) with pinned=False. Storing the
        # WalkResult LIST keeps THIS entry's full COM keepalive chain (primary
        # walk + every owned-popup subtree walk) reachable for as long as the
        # entry survives. Then evict down to capacity by LRU.
        self._stored[snapshot_id] = _StoredSnapshot(
            snapshot=snapshot, summary=summary, walk_results=walk_results,
            ttl_anchor=created_at,
        )
        self._latest_id = snapshot_id
        # Protect the snapshot just inserted from its own eviction pass: when
        # every pre-existing slot is pinned it would otherwise be the only
        # LRU candidate and be dropped immediately, breaking the find()->pin()
        # contract the overlay handlers rely on (reviewer_2 finding 36.1).
        self._evict_lru(protect_id=snapshot_id)

        # FindResult pins ONE WalkResult for the find-time consumer's keepalive
        # (the Phase 1 contract; the stored snapshot pins the full list). Pick
        # the walk that OWNS the winner so a popup-owned winner's control_ref
        # stays alive even if the consumer drops the store: the winner's
        # source_window_hwnd selects the matching popup walk, else the primary
        # (or fall-back) walk leads ``walk_results``.
        winner_walk_result = self._walk_result_for_winner(outcome, walk_results)

        return FindResult(
            outcome=outcome,
            snapshot=snapshot,
            summary=summary,
            _walk_result=winner_walk_result,
        )

    def overlay_walk(
        self,
        foreground: ForegroundContext,
        *,
        deadline: Optional[float] = None,
    ) -> OverlayWalkResult:
        """Walk the focused window from scratch and number EVERY interactive control.

        This is the standalone numbered-overlay build path (design v4
        "start_overlay_walk"): unlike :meth:`find`, there is NO spoken name to
        match, so it runs no confidence-eligibility filter and no clear-winner
        decision. It keeps every control that survives the walker's interactive
        filter and any browser DOM-fold, numbered 1..K in reading order, and
        projects them to a display-safe :class:`WalkSnapshotSummary`. The walker
        numbers controls 1..N over the walked set, but a browser fold drops
        scaffolding AFTER that numbering, so this method RENUMBERS the post-fold
        survivors 1..K contiguously (reviewer_0 finding 38.1) -- the walker's
        own numbering is contiguous only for the non-browser, no-fold case.

        Store integration is IDENTICAL to ``find`` and reuses the SAME
        multi-snapshot store machinery (``_sweep_ttl`` before insert,
        ``_evict_lru(protect_id=...)`` after, ``_latest_id`` bookkeeping,
        ``_build_summary`` projection). It does NOT touch the wh-n29v.33
        eviction/TTL/pin logic -- it calls those methods, it does not change
        them. The fresh snapshot is inserted ``pinned=False``; the Logic state
        machine pins it later via the ``pin_snapshot`` IPC action.

        Outcome mapping:
          * a walk producing >= 1 interactive control -> ``ok`` with the
            snapshot id + populated summary.
          * a walk producing zero interactive controls -> ``no_targets`` with
            an EMPTY-items summary (the snapshot is still stored so a later
            paint/pin has a snapshot id, and the empty overlay is a valid
            painted-but-no-badges state per design v4).
          * a walk the deadline cut short (``deadline_truncated``) ->
            ``execution_failed`` with ``reason="walk_deadline_exceeded"`` and
            no snapshot stored.

        Only the FOCUSED window is walked: the overlay numbers what the user is
        looking at. The ``find`` restricted window fall-back is deliberately not
        run here -- an empty focused window is a legitimate ``no_targets``
        overlay, not a reason to walk other windows.

        ``deadline`` is the SINGLE per-request absolute monotonic deadline,
        anchored exactly as in ``find``: prefer the caller-supplied
        (dequeue-anchored) timestamp, else derive it from the configured
        ``walk_deadline_ms``, else None (no bound).
        """
        # Capture the request start ONCE and anchor the per-request deadline on
        # the SAME read (parity with find(), reviewer_0 finding wh-n29v.76.2).
        # walk_start anchors BOTH the deadline derivation and the owned-popup
        # 0.7-share checkpoint, so the popup-share budget is the full
        # walk_deadline_ms instead of a sliver less than it. find() reads
        # walk_start at the top and derives the deadline from it (lines 510-516);
        # overlay_walk now does the same -- one clock read, not two.
        walk_start = self._clock()
        if deadline is None and self._walk_deadline_ms is not None:
            deadline = walk_start + (self._walk_deadline_ms / 1000.0)

        focused_top_level = (
            foreground.top_level
            if foreground.top_level is not None
            else foreground.foreground_window
        )

        # Recompute the cursor's monitor from the SAME resolver the per-match
        # monitor_id comes from (namespace consistency, reviewer_1 finding
        # 25.1), so the stored snapshot's cursor_monitor_id is in the same
        # namespace as each item's monitor_id. The overlay does not use the
        # cursor tiebreaker, but the stored snapshot keeps the same shape as a
        # find()-produced one.
        cx, cy = foreground.cursor_at_walk
        cursor_monitor_id = self._monitor_resolver((cx, cy, 1, 1))

        # Walk the focused window keeping ALL interactive controls. The browser
        # wiring still applies per-process: a Chromium-family focused process
        # walks with query_has_role=False so the DOM-folding hook can run; every
        # other process walks with query_has_role=True (interactive-only).
        is_browser = self._is_browser_process(foreground.foreground_process_name)
        # For non-browser the interactive filter (query_has_role=True) is what
        # restricts the numbered set to clickable controls; for browser the
        # load-bearing query_has_role=False keeps Text/Group/Heading alive so
        # the fold rules run, then apply_dom_corrections collapses scaffolding.
        query_has_role = not is_browser
        browser_hook = apply_dom_corrections if is_browser else None
        overlay_score_hook = self._make_overlay_score_hook()

        # walk_start (captured above, with the deadline anchored on it) is the
        # 0.7-share checkpoint reference for the owned-popup split-deadline
        # (design line 394), measured against the clock AFTER the primary walk.
        # Opt the PRIMARY focused-window walk into the transient stale-window
        # retry (wh-overlay-walk-com-retry). A focus change to a Chromium/Brave
        # window can leave its UIA element momentarily virtualized, so the live
        # ElementFromHandle / FindAllBuildCache raises a stale-window error even
        # though the window is still the foreground; a read-only re-walk clears
        # it. The owned-popup walk below deliberately does NOT opt in (it leaves
        # transient_retries at 0): for a popup a raise means the menu closed and
        # must be skipped fast, not retried (reviewer_0 finding
        # wh-overlay-walk-com-retry.1.2).
        walk_result = self._walk_fn(
            focused_top_level,
            automation=self._automation,
            query_has_role=query_has_role,
            monitor_id=cursor_monitor_id,
            browser_correction_hook=browser_hook,
            score_hook=overlay_score_hook,
            deadline=deadline,
            clock=self._clock,
            transient_retries=WALK_TRANSIENT_RETRY_ATTEMPTS,
            # wh-overlay-stale-click-refresh: the overlay must not number a
            # control the user cannot see or click. Off-screen / zero-area
            # controls have no usable badge position and are refused at
            # pre-click verification anyway, so skip them here. Overlay walk
            # only -- the by-name find and owned-popup walks keep the default.
            skip_offscreen_or_zero_area=True,
        )

        if walk_result.deadline_truncated:
            # The walk could not complete; do NOT store a partial snapshot.
            return OverlayWalkResult(
                outcome="execution_failed",
                reason="walk_deadline_exceeded",
                snapshot=None,
                summary=None,
                _walk_results=(walk_result,),
            )

        # The list of WalkResults this snapshot must pin together: the PRIMARY
        # focused-window walk first, then every owned-popup subtree walk
        # (wh-n29v.45 / wh-n29v.75). The primary walk's WalkResult always leads;
        # popups append. Mirrors find()'s keepalive contract so every popup
        # subtree's COM chain stays alive for the life of the stored snapshot.
        walk_results: list[WalkResult] = [walk_result]
        popup_matches: list[ElementMatch] = []

        # Owned-popup walk (wh-n29v.75): fold the focused window's owned #32768 /
        # UIA-Menu popup items into the numbered overlay so "show numbers" can
        # badge a menu item that by-name "click <item>" already targets. Shares
        # the PRIMARY walk's automation root + cache_request so every subtree
        # uses ONE CacheRequest (design line 392), and uses the overlay's own
        # score_hook (keep every match, re-stamp monitor_id). Gated identically
        # to find(): runs only when the primary walk was NOT deadline-truncated
        # (no usable cache_request otherwise) AND the primary finished within its
        # 0.7 share of the budget (design line 394; "better to ship results than
        # time out"). Unlike find() there is NO spoken name, so this does NOT
        # decide()/_merge_popups_and_decide -- the popup matches are merged into
        # the candidate list and the contiguous renumber below runs over the
        # combined set (focused matches first in reading order, popup matches
        # appended).
        if self._popup_share_allows(deadline, walk_start):
            # Same browser wiring as the primary walk: for a Chromium-family
            # foreground query_has_role is False (== not is_browser, the value
            # the primary overlay walk already computed), which keeps Text /
            # Group / Heading wrappers alive through the walker's interactive
            # filter -- so the browser DOM-correction hook MUST run to fold that
            # scaffolding, exactly as the primary does.
            popup_results = self._popup_walk_fn(
                foreground.foreground_window,
                automation=self._automation,
                cache_request=walk_result._keepalive_cache_request,
                query_has_role=query_has_role,
                monitor_id=cursor_monitor_id,
                browser_correction_hook=browser_hook,
                score_hook=overlay_score_hook,
                deadline=deadline,
                clock=self._clock,
            )
            if popup_results:
                walk_results.extend(popup_results)
                for popup_result in popup_results:
                    popup_matches.extend(popup_result.matches)

        # reviewer_1 finding 39.1: a browser overlay walks with
        # query_has_role=False (load-bearing -- the fold rules need Text / Group /
        # Heading alive), so the interactive filter that restricts a non-browser
        # walk to clickable controls does NOT run for a browser. After
        # apply_dom_corrections folds the three scaffolding shapes it knows, any
        # non-interactive wrapper it did NOT match (a Group wrapping a ListItem,
        # leftover Text / Pane) SURVIVES and would otherwise get a numbered badge;
        # "click N" on such a badge resolves to scaffolding the later
        # click_snapshot_item cannot invoke. Drop any post-fold survivor that is
        # neither an interactive control type NOR InvokePattern-capable, so the
        # overlay numbers only genuinely clickable controls -- the same union of
        # signals the find() path treats as clickable (an interactive control
        # type, or any control exposing InvokePattern, e.g. a Chromium
        # <div role="button"> Text). The union (not invoke-only) is required so an
        # interactive control driven by a non-Invoke pattern -- a CheckBox
        # (Toggle), a Slider (RangeValue), a ComboBox (ExpandCollapse) -- keeps
        # its badge. This runs BEFORE the contiguous renumber below so the kept
        # set is still 1..K. It is idempotent for the non-browser path: that walk
        # already kept only interactive control types (query_has_role=True), so
        # every match passes the first arm.
        #
        # The filter runs over the COMBINED candidate set (wh-n29v.75): the
        # focused-window matches FIRST in reading order, then the owned-popup
        # matches appended (design v4 line 395). The same browser-fold-survivor
        # filter applies to popup matches too -- a popup walked with
        # query_has_role=False can leave non-interactive scaffolding the fold
        # did not match.
        combined = list(walk_result.matches) + popup_matches
        clickable = [
            match
            for match in combined
            if is_interactive_control_type(
                match.control_type_id, query_has_role=True
            )
            or match.invoke_supported
        ]

        # wh-overlay-nested-dupes round 3: a native Details-view / data-grid row
        # is a clickable ListItem (Invoke) whose column cells (Edit controls, no
        # click action) are pieces of it, so each row showed a number on the row
        # AND on every cell. Drop the passive cells that sit inside a clickable
        # row container, keeping the row's badge. Overlay-only, same-window
        # contract, before the renumber below.
        #
        # This runs BEFORE the near-identical pass, not after (reviewer_2
        # finding). The near-identical pass keeps the INNER element of a pair and
        # drops the outer when the two share one accessible name. A grid row
        # whose primary cell carries the row's own text (a WPF DataGrid row named
        # after its first column) is exactly that shape: the outer is the
        # clickable row, the inner is a passive no-Invoke cell. If the
        # near-identical pass ran first it would drop the actionable row and keep
        # the passive cell, and the badge would point at a control with no
        # action. Collapsing the passive cells first removes the cell, so the
        # near-identical pass never sees the same-name pair and the row survives.
        # This pass's own "clearly smaller" area guard is what leaves the
        # same-SIZE wrapper shape to the near-identical pass, so order does not
        # hand that shape here.
        #
        # NATIVE-only (reviewer_0 finding 3): a web <input> is also an Edit with
        # no Invoke, and on a page it can sit inside a named clickable container
        # -- dropping its badge would remove exactly the field a hands-free user
        # wants to click to start typing. The native-table shape this pass
        # targets does not occur in a browser (Chromium exposes wrapper divs,
        # which the fold + near-identical rules already handle), so the pass is
        # skipped for a browser walk. is_browser is the same value that set
        # query_has_role and the DOM-correction hook above (line 1056).
        if not is_browser:
            clickable = collapse_passive_cells_in_action_containers(clickable)

        # wh-overlay-nested-dupes: collapse container+inner pairs that occupy
        # (nearly) the same pixels -- a ListItem and the Hyperlink filling it,
        # a Chromium wrapper div and the real link inside it -- so one visual
        # control gets ONE badge. Runs on the combined clickable set but pairs
        # only within one walked window (source_window_hwnd), and runs BEFORE
        # the contiguous renumber below so the kept set is still numbered
        # 1..K. Overlay-only: the by-name find() path never collapses (a
        # spoken name may match the container).
        clickable = collapse_near_identical_containers(clickable)

        # wh-overlay-browser-dupes: BROWSER-only mirror of the native-only
        # cell-collapse pass above. Chromium stamps InvokePattern on unnamed
        # scaffolding (an x.com engagement button's 136x136 hover circle
        # follows the named button in tree order and only partially overlaps
        # it), so the invoke_supported arm of the clickable filter badges the
        # same visual control twice. Drop the unnamed invoke-only duplicate,
        # keeping the named control. The pattern is Chromium-specific
        # (native walks keep only interactive control types, so an unnamed
        # Group never reaches this point), hence the gate. Runs AFTER the
        # near-identical pass, not before (reviewer_1/deepseek finding): the
        # near-identical pass's unnamed-wrapper rule fires on ANY later
        # element at least half inside the wrapper, and removing an unnamed
        # invoke-only element first could disarm that rule's only trigger,
        # resurrecting a wrapper badge the shipped code collapsed. Running
        # second, the near-identical pass's input is unchanged from shipped
        # behavior and this pass only removes badges from its survivors.
        if is_browser:
            clickable = collapse_unnamed_invoke_duplicates_of_named_controls(
                clickable
            )

        # reviewer_0 finding 38.1: the walker numbers controls 1..N over the
        # walked set inside _build_matches_from_array, but for a browser the
        # DOM-fold (browser_correction_hook) runs AFTER that numbering and DROPS
        # scaffolding controls, so the SURVIVORS keep their original, now-gapped
        # display_numbers (e.g. a fold that drops controls #1 and #3 leaves
        # [2, 4, 5]). The post-fold clickable filter above can drop more. The
        # numbered overlay's Logic resolver
        # (click_snapshot_summary_cache.resolve_display_number) matches
        # display_number EXACTLY, so a gap makes "click 1" silently miss on a web
        # page. Renumber the kept survivors 1..K contiguously in reading
        # order and regenerate item_id as "<source>-<n>" (the walker's format) so
        # the stored snapshot.matches and the projected summary agree on BOTH
        # keys -- the resolver keys on display_number, click_snapshot_item keys on
        # item_id. This is idempotent for the already-contiguous non-browser case
        # (no fold runs there, so the walker's 1..K is unchanged).
        matches = [
            replace(match, display_number=i, item_id=f"{match.source}-{i}")
            for i, match in enumerate(clickable, start=1)
        ]
        snapshot_id = f"walk-{self._snapshot_salt}-{next(self._snapshot_counter)}"
        created_at = self._clock()
        snapshot = WalkSnapshot(
            snapshot_id=snapshot_id,
            matches=matches,
            created_at_monotonic=created_at,
            foreground_window=foreground.foreground_window,
            foreground_pid=foreground.foreground_pid,
            foreground_process_name=foreground.foreground_process_name,
            foreground_window_creation_time=foreground.foreground_window_creation_time,
            cursor_at_walk=foreground.cursor_at_walk,
            cursor_monitor_id=cursor_monitor_id,
        )
        summary = self._build_summary(snapshot)

        # SAME store-maintenance sequence as find(): sweep TTL, insert as
        # most-recently-used pinned=False, record as newest, then LRU-evict
        # while protecting the just-inserted snapshot.
        self._sweep_ttl()
        # The overlay walk numbers the FOCUSED window AND its owned popups
        # (wh-n29v.75), so its stored snapshot pins the full WalkResult list --
        # the primary focused-window walk plus every owned-popup subtree walk --
        # exactly like find()'s stored snapshot, so every popup subtree's COM
        # chain stays alive for the life of the entry. With no popup present the
        # list is a single element, the same keepalive set as Phase 1.
        self._stored[snapshot_id] = _StoredSnapshot(
            snapshot=snapshot, summary=summary, walk_results=walk_results,
            ttl_anchor=created_at,
        )
        self._latest_id = snapshot_id
        self._evict_lru(protect_id=snapshot_id)

        outcome = "ok" if matches else "no_targets"
        return OverlayWalkResult(
            outcome=outcome,
            reason=None,
            snapshot=snapshot,
            summary=summary,
            _walk_results=tuple(walk_results),
        )

    def _make_overlay_score_hook(
        self,
    ) -> Callable[[list[ElementMatch]], list[ElementMatch]]:
        """Build the score_hook for the overlay walk: keep EVERY match.

        Unlike ``_make_score_hook`` (which drops ineligible matches against a
        spoken name), this keeps every interactive control the walker found --
        the overlay numbers all of them. It re-stamps ``monitor_id`` from
        ``self._monitor_resolver(match.bounds)`` (the same per-match-monitor
        re-stamp ``find`` does, reviewer_0 finding 24.1, so the stored snapshot
        and the summary agree on monitor ids) and marks the match eligible with
        a zero score; the overlay does not rank, so the score is unused. The
        walker's contiguous ``display_number`` is preserved by ``replace``.
        """

        def _hook(matches: list[ElementMatch]) -> list[ElementMatch]:
            return [
                replace(
                    match,
                    score=0.0,
                    is_eligible=True,
                    monitor_id=self._monitor_resolver(match.bounds),
                )
                for match in matches
            ]

        return _hook

    def latest_snapshot(self) -> Optional[WalkSnapshot]:
        """Return the most-recently-ADDED WalkSnapshot, or None if the store is empty.

        Tracks the newest snapshot by insertion (the last snapshot ``find``
        stored), NOT the most-recently-LRU-accessed one, so a ``get_snapshot``
        that touches an older snapshot does not change what this reports. Does
        NOT apply TTL or foreground checks; ``get_snapshot`` is the gated
        accessor. Returns None once the newest snapshot has been evicted or the
        store has been invalidated.
        """
        stored = self._latest_stored()
        return stored.snapshot if stored is not None else None

    def latest_summary(self) -> Optional[WalkSnapshotSummary]:
        """Return the most-recently-added WalkSnapshotSummary, or None."""
        stored = self._latest_stored()
        return stored.summary if stored is not None else None

    def _latest_stored(self) -> Optional[_StoredSnapshot]:
        """Resolve the most-recently-added stored entry, or None if it is gone."""
        if self._latest_id is None:
            return None
        return self._stored.get(self._latest_id)

    def get_snapshot(
        self,
        snapshot_id: str,
        *,
        current_foreground_window: Optional[int] = None,
        current_foreground_pid: Optional[int] = None,
        current_foreground_process_name: Optional[str] = None,
        current_foreground_window_creation_time: Optional[int] = None,
    ) -> Optional[WalkSnapshot]:
        """Return the requested stored snapshot if still valid, else None.

        Runs in two phases (design v4 "Multi-snapshot store in ElementFinder"):

        1. **Cross-snapshot TTL sweep.** BEFORE resolving the requested id, every
           stored snapshot whose age has reached ``snapshot_ttl_seconds`` is
           dropped (releasing its own WalkResult keepalive). TTL eviction applies
           to PINNED snapshots too -- the pin blocks only LRU eviction, never
           TTL. This is the stale-pin cleanup hard boundary: if the Logic-side
           owner dies before sending unpin, a pinned snapshot is still reclaimed
           after its TTL. The boundary fails closed: ``age >= ttl`` is already
           expired (reviewer_1 finding 25.2). ``find`` runs the SAME sweep before
           each insert, so the boundary holds under find()-only traffic too and
           does not depend on a future ``get_snapshot`` ever being called
           (reviewer_0 round-1 findings 34.1/34.2).
        2. **Resolve the requested id with the per-snapshot identity check.**
           After the sweep, the requested ``snapshot_id`` is looked up. A missing
           id (never stored, already TTL-swept, or LRU-evicted) returns None.
           The full foreground-IDENTITY check then applies to the REQUESTED
           snapshot only:

           * (b) foreground IDENTITY change (window handle, pid, process name, or
             window creation time) -- when any of ``current_foreground_window``,
             ``current_foreground_pid``, ``current_foreground_process_name``, or
             ``current_foreground_window_creation_time`` is supplied and differs
             from the snapshot's corresponding field, ONLY that snapshot is
             dropped and None is returned. Comparing the full identity (not just
             the HWND) is required because Windows reuses HWND values: a recycled
             HWND that lands on a different process would otherwise return a stale
             snapshot as valid (reviewer_0 finding 24.3). Each check is optional
             so window-only callers behave exactly as before.

        On a hit, the snapshot is marked most-recently-used (so an active overlay
        snapshot survives LRU pressure) and returned. Any invalidation drops the
        offending entry AND its WalkResult keepalive, releasing those COM proxies
        while leaving sibling snapshots untouched.

        COM-lifetime caveat for the returned snapshot (reviewer_2 finding
        26.2): the snapshot this method returns carries live COM
        ``control_ref`` handles that are kept alive ONLY by the entry's stored
        ``walk_result`` keepalive. That keepalive is dropped the moment the entry
        is invalidated -- by TTL expiry, a foreground-identity change, an
        LRU eviction on a later ``find()``, or an explicit ``invalidate()`` call.
        A caller that holds a snapshot returned here and then triggers (or merely
        allows) any such invalidation would be left with dangling ``control_ref``
        proxies. ``get_snapshot`` does NOT pin an independent keepalive (the
        ``pinned`` flag blocks LRU eviction in the store; it does not give the
        caller an independent reference). A consumer that needs the snapshot's
        ``control_ref`` handles to remain valid across a possible invalidation
        MUST instead hold the :class:`FindResult` returned by the original
        ``find()`` call -- its private ``_walk_result`` pins the COM chain
        independent of the coordinator's stored state -- or consume the
        snapshot's control_refs before any invalidation can occur (the
        production path consumes immediately, so this is narrow in practice).

        CROSS-SNAPSHOT side effect (reviewer_0 round-1 finding 34.3): the
        phase-(1) TTL sweep runs across ALL stored snapshots, so a
        ``get_snapshot(B)`` call can drop an UNRELATED snapshot A's WalkResult
        keepalive purely as a side effect of being asked about B (and, after the
        34.1 fix, a later ``find()`` does the same before its insert). A consumer
        that obtained snapshot A through an EARLIER ``get_snapshot(A)`` -- and so
        has no :class:`FindResult` to pin A's COM chain -- can therefore have A's
        ``control_ref`` proxies dangled by an unrelated ``get_snapshot``/``find``
        once A has reached its TTL. The "hold the FindResult" mitigation above
        only protects the find()-time consumer; a get_snapshot-time consumer that
        needs A to outlive another snapshot's resolution would need a
        per-snapshot keepalive-for-the-caller mechanism, which this store does
        NOT provide (the ``pinned`` flag blocks LRU eviction only, not TTL, and
        gives the caller no independent reference). The production overlay path
        consumes each resolved snapshot's matches immediately, so this is a
        contract note for future callers, not a live hazard today.
        """
        # (1) Cross-snapshot TTL sweep -- drop every expired entry (pinned or
        # not) and its keepalive before resolving the requested id.
        self._sweep_ttl()

        stored = self._stored.get(snapshot_id)
        if stored is None:
            return None
        snapshot = stored.snapshot
        # (2b) foreground IDENTITY change on the REQUESTED snapshot -- any
        # supplied field that differs from the stored snapshot's corresponding
        # field invalidates ONLY this entry.
        if self._foreground_identity_differs(
            snapshot,
            current_foreground_window=current_foreground_window,
            current_foreground_pid=current_foreground_pid,
            current_foreground_process_name=current_foreground_process_name,
            current_foreground_window_creation_time=(
                current_foreground_window_creation_time
            ),
        ):
            self._drop(snapshot_id)
            return None
        # Hit: mark most-recently-used (move to the end of the recency order).
        self._touch(snapshot_id)
        return snapshot

    def describe_snapshot_miss(
        self,
        snapshot_id: str,
        *,
        current_foreground_window: Optional[int] = None,
        current_foreground_pid: Optional[int] = None,
        current_foreground_process_name: Optional[str] = None,
        current_foreground_window_creation_time: Optional[int] = None,
    ) -> Optional[str]:
        """Name why a matching ``get_snapshot`` would miss, or None if it hits.

        A NON-mutating diagnostic counterpart to :meth:`get_snapshot`: it does
        NOT sweep, drop, or touch any entry. Callers run it BEFORE
        ``get_snapshot`` (which DOES drop the entry on a miss) so a failed
        numbered-overlay "click N" can log WHICH cause fired instead of one
        ambiguous "snapshot_expired" for all three
        (wh-overlay-snapshot-keepalive). The reasons mirror ``get_snapshot``'s
        own miss order:

          * ``"not_found"`` -- never stored, already TTL-swept, or LRU-evicted.
          * ``"ttl_expired"`` -- present but its TTL anchor has reached the TTL,
            so the next ``get_snapshot`` sweep would drop it. Uses the SAME
            fail-closed ``age >= snapshot_ttl_seconds`` boundary and the SAME
            ``ttl_anchor`` as ``_sweep_ttl``.
          * ``"foreground_changed"`` -- present and within TTL, but a supplied
            foreground identity field differs from the snapshot's, so
            ``get_snapshot`` would drop it on the identity check. The user is
            looking at a DIFFERENT window than the one this snapshot was walked
            for.

        Returns None when the snapshot is present, within TTL, and matches every
        supplied foreground field -- i.e. ``get_snapshot`` would return it.
        """
        stored = self._stored.get(snapshot_id)
        if stored is None:
            return "not_found"
        if self._clock() - stored.ttl_anchor >= self._snapshot_ttl_seconds:
            return "ttl_expired"
        if self._foreground_identity_differs(
            stored.snapshot,
            current_foreground_window=current_foreground_window,
            current_foreground_pid=current_foreground_pid,
            current_foreground_process_name=current_foreground_process_name,
            current_foreground_window_creation_time=(
                current_foreground_window_creation_time
            ),
        ):
            return "foreground_changed"
        return None

    @staticmethod
    def _foreground_identity_differs(
        snapshot: WalkSnapshot,
        *,
        current_foreground_window: Optional[int],
        current_foreground_pid: Optional[int],
        current_foreground_process_name: Optional[str],
        current_foreground_window_creation_time: Optional[int],
    ) -> bool:
        """True when any SUPPLIED foreground field differs from the snapshot's.

        The single source of truth for the per-snapshot foreground-identity
        check, shared by :meth:`get_snapshot` (which drops the entry on a
        difference) and :meth:`describe_snapshot_miss` (which only reports it).
        Each field is optional: a None argument is not compared, so window-only
        callers behave exactly as before. Comparing the FULL identity (not just
        the window handle) is required because Windows reuses window-handle
        values -- a recycled handle landing on a different process would
        otherwise pass (reviewer_0 finding 24.3).
        """
        return (
            (
                current_foreground_window is not None
                and current_foreground_window != snapshot.foreground_window
            )
            or (
                current_foreground_pid is not None
                and current_foreground_pid != snapshot.foreground_pid
            )
            or (
                current_foreground_process_name is not None
                and current_foreground_process_name
                != snapshot.foreground_process_name
            )
            or (
                current_foreground_window_creation_time is not None
                and current_foreground_window_creation_time
                != snapshot.foreground_window_creation_time
            )
        )

    def pin(self, snapshot_id: str) -> bool:
        """Mark a stored snapshot pinned (immune to LRU eviction), never TTL.

        Returns True if ``snapshot_id`` was present (and is now pinned), False if
        it is unknown. Pinning does NOT refresh the snapshot's TTL: a pinned
        snapshot is still TTL-evicted once its age reaches ``snapshot_ttl_seconds``
        (stale-pin cleanup, on both the ``find`` and ``get_snapshot`` sweep
        paths). This is the store-side MECHANISM only; the Logic state machine
        decides when to pin/unpin via IPC actions (a separate slice).

        The store imposes no cap on the pinned count and deliberately does NOT
        warn on a multi-pin. The overlay state machine legitimately holds TWO
        pinned snapshots transiently across a refresh paint leg: it pins the new
        snapshot but DEFERS the prior unpin until a successful paint-ack, so a
        FAILED paint can restore the prior (see
        click_overlay_state.py:_refresh_build_ok / _commit_refresh_prior_unpin).
        A store-level "pinned > 1" warning would therefore cry wolf on every
        normal refresh and bury a genuine contract break (reviewer_0 round-2
        finding 34.5). Distinguishing a legitimate refresh-window two-pin from a
        real contract break (a lost unpin or a racing double-pin) needs the
        session/generation context the store does not have, so that detection
        belongs in the Logic state machine / the ``pin_snapshot`` IPC handler --
        not here. Memory safety does not depend on it: pinned entries are
        TTL-bounded (never LRU-bounded) and are reclaimed on the next
        find()/get_snapshot once their age elapses (the 34.1/34.2 fix).
        """
        stored = self._stored.get(snapshot_id)
        if stored is None:
            return False
        stored.pinned = True
        return True

    def unpin(self, snapshot_id: str) -> bool:
        """Clear a stored snapshot's pin so it is again LRU-evictable.

        Returns True if ``snapshot_id`` was present, False if it is unknown.
        """
        stored = self._stored.get(snapshot_id)
        if stored is None:
            return False
        stored.pinned = False
        return True

    def refresh_snapshot_ttl(self, snapshot_id: str) -> bool:
        """Slide a still-visible snapshot's TTL window forward to "now".

        The Input-store COUNTERPART to the Logic-side 15s overlay keepalive
        (main.py ``_fire_overlay_keepalive``). The keepalive re-puts the
        Logic resolver-cache summary so "click N" keeps resolving; this
        re-stamps the Input store's ``ttl_anchor`` so the snapshot the overlay
        still shows does not age out of THIS store while the badges are on
        screen. Without it the two stores expire independently: Logic keeps
        resolving "click N" and dispatching the click, but the Input store has
        already TTL-swept the snapshot, so the click reports ``snapshot_expired``
        on a still-visible overlay (wh-overlay-snapshot-keepalive).

        Deliberately DISTINCT from :meth:`pin`. ``pin`` blocks LRU eviction only
        and does NOT slide TTL (the stale-pin cleanup boundary: a pinned
        snapshot whose Logic owner died still ages out). This is the explicit,
        keepalive-driven TTL slide; once the keepalive stops (the overlay is
        gone or Logic died), the snapshot ages out normally from its LAST
        refresh, so the stale-pin cleanup guarantee is preserved -- the worst
        case is one keepalive interval plus the TTL.

        Returns True when ``snapshot_id`` was present AND still within its TTL
        (the anchor was slid to now). Returns False when the id is unknown OR has
        already reached its TTL (``age >= snapshot_ttl_seconds``): an
        already-expired entry is NOT revived, so the boundary stays fail-closed,
        matching ``_sweep_ttl``. Does NOT sweep siblings or touch LRU recency.
        """
        stored = self._stored.get(snapshot_id)
        if stored is None:
            return False
        now = self._clock()
        if now - stored.ttl_anchor >= self._snapshot_ttl_seconds:
            return False
        stored.ttl_anchor = now
        return True

    def invalidate(self) -> None:
        """Drop the ENTIRE multi-snapshot store and every COM keepalive."""
        self._stored.clear()
        self._latest_id = None

    # -- internals ------------------------------------------------------------

    def _touch(self, snapshot_id: str) -> None:
        """Move ``snapshot_id`` to the most-recently-used end of the store.

        Dict insertion order is the LRU recency order; re-inserting the entry at
        the end marks it most-recently-used without disturbing its identity or
        keepalive. ``_latest_id`` (newest-by-insertion) is intentionally NOT
        changed -- an LRU touch is not an "add".
        """
        stored = self._stored.pop(snapshot_id, None)
        if stored is not None:
            self._stored[snapshot_id] = stored

    def _drop(self, snapshot_id: str) -> None:
        """Remove one entry (releasing only ITS WalkResult keepalive)."""
        self._stored.pop(snapshot_id, None)
        if self._latest_id == snapshot_id:
            self._latest_id = None

    def _sweep_ttl(self) -> None:
        """Drop every stored snapshot whose age has reached the TTL.

        Called from BOTH ``find`` (before each insert) and ``get_snapshot``
        (before resolving the requested id), so the TTL boundary holds whichever
        path runs next and does not depend on a future ``get_snapshot``
        (reviewer_0 round-1 findings 34.1/34.2). Applies to pinned snapshots too
        (the pin blocks only LRU eviction). The boundary fails closed:
        ``age >= snapshot_ttl_seconds`` is expired. Each dropped entry releases
        its own WalkResult keepalive; siblings are untouched.
        """
        now = self._clock()
        expired = [
            sid
            for sid, stored in self._stored.items()
            if now - stored.ttl_anchor >= self._snapshot_ttl_seconds
        ]
        for sid in expired:
            self._drop(sid)

    def _evict_lru(self, protect_id: Optional[str] = None) -> None:
        """Evict least-recently-used UNPINNED entries down to capacity.

        Scans from the front (least-recently-used) of the recency-ordered store
        and drops the first UNPINNED entries until the store size is at or below
        ``snapshot_store_capacity``. A pinned entry is never LRU-evicted; if
        every over-capacity slot is pinned, nothing is evicted (TTL bounds those
        instead). Each eviction releases only that entry's WalkResult keepalive.

        ``protect_id`` (the snapshot ``find`` just inserted) is skipped exactly
        like a pinned entry, so a find() never evicts the snapshot it just
        produced. Without this, when every PRE-EXISTING slot is pinned the fresh
        unpinned entry is the only LRU candidate and gets dropped on its own
        insert pass -- ``get_snapshot``/``pin``/``latest_snapshot`` would then
        all fail for the just-produced snapshot, and at capacity=1 a single pin
        would make the store write-once until that pin's TTL elapsed (reviewer_2
        finding 36.1). Protecting it lets the store sit ONE over capacity (the
        pins plus the fresh entry) until the next find() makes it an older
        unpinned entry again, or TTL reclaims a pinned slot -- the same bounded
        overage the all-pinned case already tolerates.
        """
        if len(self._stored) <= self._snapshot_store_capacity:
            return
        # Iterate front-to-back (oldest recency first); collect the unpinned ids
        # to drop until the store would be back within capacity. The pinned
        # entries and the just-inserted ``protect_id`` are never dropped here.
        to_evict: list[str] = []
        remaining = len(self._stored)
        for sid, stored in self._stored.items():
            if remaining <= self._snapshot_store_capacity:
                break
            if stored.pinned or sid == protect_id:
                continue
            to_evict.append(sid)
            remaining -= 1
        for sid in to_evict:
            self._drop(sid)

    # -- owned-popup orchestration (wh-n29v.45) -------------------------------

    def _popup_share_allows(
        self, deadline: Optional[float], walk_start: float
    ) -> bool:
        """True when the primary walk finished within its 0.7 budget share.

        design line 394: the primary focused-window walk uses the FULL budget,
        but the owned-popup walk runs only when the primary completed within
        ``_POPUP_PRIMARY_DEADLINE_SHARE`` of the budget. If the primary overran
        that share, the popup walk is skipped -- "better to ship by-name results
        than time out". When ``deadline`` is None there is no budget pressure, so
        the popup walk always runs (returns True).

        The checkpoint is ``walk_start + share * (deadline - walk_start)`` -- an
        absolute monotonic timestamp on the same injected clock the deadline
        uses. This is consulted AFTER the primary walk returns, so the clock now
        reflects how long the primary took.
        """
        if deadline is None:
            return True
        budget = deadline - walk_start
        if budget <= 0:
            return False
        checkpoint = walk_start + _POPUP_PRIMARY_DEADLINE_SHARE * budget
        return self._clock() < checkpoint

    def _merge_popups_and_decide(
        self,
        *,
        focused_scored: list[ElementMatch],
        popup_results: list[WalkResult],
        cursor_at_walk: tuple[int, int],
        cursor_monitor_id: int,
    ) -> tuple[list[ElementMatch], Outcome]:
        """Merge owned-popup matches into the focused set and re-decide.

        Merge order (design line 395): the FOCUSED-window matches come first in
        reading order; the popup matches are APPENDED in their own reading order
        (the popup walks are appended in enumeration/z-order; each popup's own
        matches keep their walk order). The focused matches' ``display_number``
        badges are NOT renumbered -- the user's mental model anchors numbers to
        the long-lived focused window. The appended popup matches are renumbered
        to CONTINUE the sequence after the focused maximum (and their ``item_id``
        regenerated to match) so badges do not collide; renumbering only the
        appended popups leaves every focused badge unchanged.

        The combined eligible set is then re-decided so a popup menu item can win
        the by-name click. With no popups present this method is never called, so
        the Phase 1 decide() over the focused set alone is unchanged.
        """
        merged = list(focused_scored)
        next_number = (
            max((m.display_number for m in focused_scored), default=0) + 1
        )
        for popup_result in popup_results:
            for match in popup_result.matches:
                merged.append(
                    replace(
                        match,
                        display_number=next_number,
                        item_id=f"{match.source}-{next_number}",
                    )
                )
                next_number += 1

        outcome = decide(
            merged,
            cursor_at_walk,
            cursor_monitor_id,
            self._dpi_resolver,
            min_confidence=self._min_confidence,
            clear_winner_margin=self._clear_winner_margin,
            tiebreaker_influence_logical_px=self._tiebreaker_influence_logical_px,
            tiebreaker_min_separation_logical_px=(
                self._tiebreaker_min_separation_logical_px
            ),
        )
        return merged, outcome

    @staticmethod
    def _walk_result_for_winner(
        outcome: Outcome, walk_results: list[WalkResult]
    ) -> WalkResult:
        """Pick the WalkResult that owns the decided winner, for FindResult.

        ``walk_results[0]`` is the primary focused-window walk (or the fall-back
        walk when the fall-back won); any owned-popup walks follow. When the
        winner is popup-owned (``source_window_hwnd != 0``) the matching popup
        walk's keepalive must back the returned FindResult so the winner's
        ``control_ref`` stays alive even if the find-time consumer drops the
        store. Falls back to the leading (primary/fall-back) walk for a
        primary-window winner or any non-ok outcome.
        """
        winner = getattr(outcome, "winner", None)
        if winner is not None and getattr(winner, "source_window_hwnd", 0):
            for wr in walk_results:
                if any(
                    m.control_ref is winner.control_ref for m in wr.matches
                ):
                    return wr
        return walk_results[0]

    def _walk_and_decide(
        self,
        top_level: Any,
        *,
        query: ElementQuery,
        process_name: str,
        cursor_at_walk: tuple[int, int],
        cursor_monitor_id: int,
        score_hook: Callable[[list[ElementMatch]], list[ElementMatch]],
        deadline: Optional[float] = None,
    ) -> tuple[Outcome, WalkResult, list[ElementMatch]]:
        """Walk one window and run decide() against the scored matches.

        Shared by the focused-window walk and every fall-back-window walk so the
        load-bearing browser wiring (``query_has_role=False`` + the DOM
        correction hook for a Chromium-family process) is applied PER WINDOW:
        the focused window's process and each fall-back window's process each
        independently drive their own ``query_has_role`` / browser hook. The
        cursor position and resolver-derived cursor monitor are passed through
        so decide()'s tiebreaker and cross-monitor gate are identical to the
        focused-window path.

        ``deadline`` is the SINGLE per-request absolute monotonic deadline,
        passed unchanged from ``find`` so every walk (focused + each fall-back)
        shares one budget (FINDING 1). It is threaded into ``walk_window``
        together with ``self._clock``.

        FAIL-CLOSED ON TRUNCATION (FINDING 2): when the walker reports
        ``walk_result.deadline_truncated`` the matches are an incomplete prefix
        of the subtree (or empty, for the pre-walk skip). Running ``decide``
        over a partial set could return a confident-but-wrong ``ok`` winner, so
        this method does NOT call ``decide`` in that case -- it synthesises a
        ``not_found`` outcome (reason ``walk_deadline_exceeded``) which maps to
        the existing not_found notice path. A deadline can thus only downgrade
        an outcome to not_found, never fabricate a winner.

        Returns the decide ``Outcome`` (or the synthetic not_found on
        truncation), the walker's ``WalkResult`` (its COM keepalive chain), and
        the scored eligible match list (== the WalkResult's matches).
        """
        is_browser = self._is_browser_process(process_name)
        query_has_role = self._query_has_role(query, is_browser)
        browser_hook = apply_dom_corrections if is_browser else None

        walk_result = self._walk_fn(
            top_level,
            automation=self._automation,
            query_has_role=query_has_role,
            monitor_id=cursor_monitor_id,
            browser_correction_hook=browser_hook,
            score_hook=score_hook,
            deadline=deadline,
            clock=self._clock,
        )
        if walk_result.deadline_truncated:
            # Partial/empty walk -- fail closed to not_found WITHOUT decide().
            return (
                Outcome(
                    outcome="not_found",
                    reason="walk_deadline_exceeded",
                    winner=None,
                    candidates=(),
                ),
                walk_result,
                [],
            )
        # The score_hook already filtered to eligible matches with .score set,
        # so walk_result.matches IS the scored eligible list decide() expects.
        scored = walk_result.matches
        outcome = decide(
            scored,
            cursor_at_walk,
            cursor_monitor_id,
            self._dpi_resolver,
            min_confidence=self._min_confidence,
            clear_winner_margin=self._clear_winner_margin,
            tiebreaker_influence_logical_px=self._tiebreaker_influence_logical_px,
            tiebreaker_min_separation_logical_px=(
                self._tiebreaker_min_separation_logical_px
            ),
        )
        return outcome, walk_result, scored

    def _run_fallback(
        self,
        *,
        query: ElementQuery,
        foreground: ForegroundContext,
        cursor_monitor_id: int,
        score_hook: Callable[[list[ElementMatch]], list[ElementMatch]],
        deadline: Optional[float] = None,
    ) -> Optional[tuple[Outcome, WalkResult, list[ElementMatch]]]:
        """Run the v5 restricted window-walk fall-back.

        Enumerate the visible top-level windows, exclude the focused window,
        restrict to the focused window's monitor (unless
        ``enable_offmonitor_fallback`` is set), order/deprioritise by the v5
        overlay heuristics, then walk each remaining window IN ORDER until one
        produces a decided (non-``not_found``) outcome. Returns that window's
        ``(outcome, walk_result, scored)`` so the caller adopts it as the
        result -- the winning walk's ``WalkResult`` is what gets stored/pinned,
        keeping its COM keepalive chain alive for a fall-back winner.

        Returns ``None`` when no fall-back window produces a decided match (the
        caller keeps the focused window's ``not_found``). A window whose walk
        yields ``not_found`` (e.g. an empty overlay with no interactive
        children, v5 signal 4) is skipped and the next candidate is tried.

        Each candidate's walk runs live COM (ElementFromHandle +
        FindAllBuildCache) on an HWND captured in the earlier enumeration pass.
        A window that closes in the gap raises an ``OSError`` / comtypes
        ``COMError``; ONLY that stale-handle class is caught so the candidate is
        skipped (logged at debug) rather than aborting the whole fall-back
        (finding 45.1). A non-stale exception from ``score_hook`` / the browser
        correction / ``decide`` is a real programming error and propagates out
        of ``find()`` -- it is NOT silently turned into ``not_found`` (finding
        46.1).

        Focused-monitor anchor + fail-closed (findings 46.2 / 46.3): the focused
        window's MONITOR rectangle anchors both the same-monitor overlap
        restriction and the very-small overlay test. It is resolved from the
        focused window's OWN rectangle -- the enumerated focused entry's rect
        when present, else ``focused_window_rect_resolver`` on the focused HWND
        -- and then mapped to a monitor rect via ``monitor_rect_resolver``. The
        cursor's monitor is NEVER substituted for the focused monitor. If the
        focused window rect is unavailable, OR the monitor-rect resolver returns
        ``None`` (the box overlaps no monitor), the focused monitor is
        UNRESOLVED: with ``enable_offmonitor_fallback`` False (default) the
        same-monitor fall-back does NOT run (return ``None``, keep the focused
        ``not_found``) rather than walking the wrong monitor. With the
        off-monitor opt-in True the monitor restriction does not apply, so the
        fall-back still walks all candidates and ``order_candidates`` tolerates a
        ``None`` monitor rect (the very-small signal is skipped).
        """
        windows = self._window_enumerator()

        focused_hwnd = foreground.foreground_window
        focused_rect: Optional[tuple[int, int, int, int]] = None
        candidates: list[FallbackWindow] = []
        for window in windows:
            if window.hwnd == focused_hwnd:
                focused_rect = window.rect
                continue
            candidates.append(window)

        if not candidates:
            return None

        # Resolve the focused window's OWN rect (46.2): the enumerated focused
        # entry's rect, else the injected focused-window-rect resolver on the
        # focused HWND. No cursor-box substitution.
        if focused_rect is None:
            focused_rect = self._focused_window_rect_resolver(focused_hwnd)

        # Map the focused window rect to its MONITOR rect; None when the rect is
        # unavailable or overlaps no monitor (46.3 fail-closed guard).
        focused_monitor_rect: Optional[tuple[int, int, int, int]] = None
        if focused_rect is not None:
            focused_monitor_rect = self._monitor_rect_resolver(focused_rect)

        if focused_monitor_rect is None and not self._enable_offmonitor_fallback:
            # Focused monitor unresolved + same-monitor mode: fail closed. We
            # cannot prove a candidate is on the focused monitor, so we do not
            # walk any -- the focused window's not_found stands (46.2 / 46.3).
            logger.debug(
                "window fall-back: focused monitor unresolved (hwnd=%s); "
                "skipping same-monitor fall-back",
                focused_hwnd,
            )
            return None

        restricted = restrict_to_monitor(
            candidates,
            # When off-monitor is enabled restrict_to_monitor ignores the rect;
            # pass a zero rect so the type is satisfied without claiming a
            # monitor the resolver could not confirm.
            focused_monitor_rect=focused_monitor_rect or (0, 0, 0, 0),
            enable_offmonitor_fallback=self._enable_offmonitor_fallback,
        )
        ordered = order_candidates(restricted, monitor_rect=focused_monitor_rect)

        for window in ordered:
            # One shared per-request deadline bounds the WHOLE fall-back loop
            # (FINDING 1): once it has passed, stop enumerating candidates
            # instead of letting each remaining walk hit the pre-walk skip in
            # turn. Keep the focused window's not_found.
            if deadline is not None and self._clock() >= deadline:
                logger.debug(
                    "window fall-back: per-request deadline passed; stopping "
                    "candidate enumeration (keeping focused not_found)"
                )
                return None
            try:
                outcome, walk_result, scored = self._walk_and_decide(
                    window.hwnd,
                    query=query,
                    process_name=window.process_name,
                    cursor_at_walk=foreground.cursor_at_walk,
                    cursor_monitor_id=cursor_monitor_id,
                    score_hook=score_hook,
                    deadline=deadline,
                )
            except _STALE_WINDOW_ERRORS:
                # A window closed between enumeration and walk: the live-COM
                # ElementFromHandle / FindAllBuildCache raised OSError/COMError.
                # Skip this candidate and try the next (46.1). Any OTHER
                # exception type (from score_hook / decide) is NOT caught here
                # and propagates out of find().
                logger.debug(
                    "window fall-back: walk of HWND %s raised a stale-handle "
                    "error; skipping",
                    window.hwnd,
                    exc_info=True,
                )
                continue
            if outcome.outcome != "not_found":
                return outcome, walk_result, scored
        return None

    def _is_browser_process(self, process_name: str) -> bool:
        return (process_name or "").lower() in self._browser_processes

    @staticmethod
    def _query_has_role(query: ElementQuery, is_browser: bool) -> bool:
        """Decide the walker's ``query_has_role`` flag for this walk.

        For a Chromium-family foreground process this MUST be False so the
        walker keeps Text / Group / Heading controls alive into the browser
        correction hook, making all three fold rules reachable (see the
        module docstring's load-bearing wiring MUST and the wh-agd2v bead
        comments). For non-browser processes the standard rule applies: a
        role-bearing query keeps only interactive controls; a role-less query
        keeps everything.
        """
        if is_browser:
            return False
        return query.role is not None

    def _make_score_hook(
        self, query: ElementQuery
    ) -> Callable[[list[ElementMatch]], list[ElementMatch]]:
        """Build the score_hook closure the walker calls after folding.

        For each match: compute eligibility (confidence_scorer.is_eligible)
        with the injected substring thresholds, drop the ineligible ones, then
        return scored eligible copies (confidence_scorer.score) with
        ``is_eligible=True``. ElementMatch is frozen, so the scored copies are
        produced via ``dataclasses.replace``. The returned list is exactly the
        eligible+scored list ``clear_winner_rule.decide`` expects.

        The same ``replace`` also re-stamps ``monitor_id`` from
        ``self._monitor_resolver(match.bounds)`` (reviewer_0 finding 24.1). The
        walker stamps every match with the single ``monitor_id`` it was called
        with; v5 requires the PER-MATCH monitor the bounds centre falls on, and
        decide()'s tiebreaker cross-monitor gate drops candidates whose
        monitor_id != cursor_monitor_id. Re-stamping here -- inside the same
        ``replace`` that sets score/eligibility -- keeps walk_result.matches,
        the snapshot, and decide all consistent and preserves ``control_ref``
        (replace copies it). A disabled-but-eligible match (eligibility row d)
        is still kept and re-stamped, so it still reaches decide and surfaces as
        ``execution_failed:disabled``.
        """

        def _hook(matches: list[ElementMatch]) -> list[ElementMatch]:
            scored: list[ElementMatch] = []
            for match in matches:
                if not is_eligible(
                    query,
                    match,
                    min_substring_query_length=self._min_substring_query_length,
                    min_substring_overlap_ratio=self._min_substring_overlap_ratio,
                ):
                    continue
                scored.append(
                    replace(
                        match,
                        score=score(query, match),
                        is_eligible=True,
                        monitor_id=self._monitor_resolver(match.bounds),
                    )
                )
            return scored

        return _hook

    @staticmethod
    def _build_summary(snapshot: WalkSnapshot) -> WalkSnapshotSummary:
        """Project a WalkSnapshot to its display-safe WalkSnapshotSummary.

        Copies only the display-safe primitives (no ``control_ref``, no
        score/eligibility/enablement internals) so the summary can cross the
        Input -> Logic -> GUI boundary. The summary's ``snapshot_id`` and
        ``created_at_monotonic`` match the full snapshot so the two can be
        correlated.
        """
        items = [
            WalkSnapshotSummaryItem(
                item_id=match.item_id,
                display_number=match.display_number,
                name=match.name,
                role=match.role,
                bounds=match.bounds,
                monitor_id=match.monitor_id,
            )
            for match in snapshot.matches
        ]
        return WalkSnapshotSummary(
            snapshot_id=snapshot.snapshot_id,
            items=items,
            created_at_monotonic=snapshot.created_at_monotonic,
        )

    @staticmethod
    def filter_and_renumber_summary(
        summary: WalkSnapshotSummary,
        item_id_filter: Optional[list[str]] = None,
    ) -> WalkSnapshotSummary:
        """Filter a summary to ``item_id_filter`` and renumber the kept items.

        The numbered-overlay re-paint path (``show_numbered_overlay``, wh-n29v.83)
        re-uses an EXISTING snapshot. ``_build_summary`` copies each match's
        original ``display_number`` verbatim; this helper rebuilds the display
        numbering so the painted badges are CONTIGUOUS from 1:

          * When ``item_id_filter`` is ``None``, every item is kept in the
            summary's existing reading order.
          * When ``item_id_filter`` is supplied, only items whose ``item_id`` is
            in the filter are kept -- the auto-open case that restricts the
            painted set to the ambiguous finalists (design v4 line 408). The
            filter is treated as a membership SET; the kept items follow the
            summary's reading order, NOT the order the ids appear in the filter,
            because the snapshot's order is the authoritative top-to-bottom /
            left-to-right reading order the walker assigned.

        The kept items are then reassigned ``display_number`` 1..K so the
        overlay never paints a gap (e.g. badges 1, 3, 7) when a filter drops the
        controls in between (design v4 lines 416, 543). ``snapshot_id`` and
        ``created_at_monotonic`` are preserved so the rebuilt summary still names
        the same snapshot (the cross-field-rule (c) invariant the
        ``ShowNumberedOverlayResponse`` validator enforces). Pure data
        transformation -- no COM, no store access -- so it is independently
        unit-testable.
        """
        keep = set(item_id_filter) if item_id_filter is not None else None
        renumbered: list[WalkSnapshotSummaryItem] = []
        next_number = 1
        for item in summary.items:
            if keep is not None and item.item_id not in keep:
                continue
            renumbered.append(
                WalkSnapshotSummaryItem(
                    item_id=item.item_id,
                    display_number=next_number,
                    name=item.name,
                    role=item.role,
                    bounds=item.bounds,
                    monitor_id=item.monitor_id,
                )
            )
            next_number += 1
        return WalkSnapshotSummary(
            snapshot_id=summary.snapshot_id,
            items=renumbered,
            created_at_monotonic=summary.created_at_monotonic,
        )


__all__ = [
    "ElementFinder",
    "ForegroundContext",
    "FindResult",
    "OverlayWalkResult",
    "collapse_near_identical_containers",
    "collapse_passive_cells_in_action_containers",
]
