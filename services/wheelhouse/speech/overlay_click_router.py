"""Pure 'click N' routing resolver for the numbered overlay (wh-n29v.17).

Parent epic: ``wh-n29v`` (voice-element-clicking Phase 1.5). The
authoritative spec is the v4 design doc
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``,
the section ``### "Click N" routing (state-machine-driven)``.

This module is the DECISION half of the Logic-side overlay routing layer.
It is a pure function of the current overlay state, the parsed integer (the
captured text already run through
``speech/number_word_parser.py:parse_number_word``), and the overlay state
machine's pin bookkeeping. It performs NO I/O, NO IPC, and NO state
mutation -- it only reads the snapshot-summary cache to resolve a display
number to an item_id and returns a :class:`RoutingDecision`. The integration
layer (``LogicController.forward_click_element``) acts on the decision.

The routing rules (quoted from the v4 design doc) are, after the captured
text resolves to a positive integer N or to ``None``:

  * Non-integer (``parsed_number is None``), ANY state -> ``BY_NAME``.
  * ``CLOSED`` + N -> ``BY_NAME`` (the spoken digit is looked up by name).
  * ``PAINTED`` + N -> resolve N against the CURRENT visible snapshot
    (``pinned_snapshot_id``). FOUND -> ``SNAPSHOT_ITEM``. A miss
    (NOT_FOUND or SNAPSHOT_EXPIRED, or no pinned snapshot) -> ``NOTICE``
    (``no_badge_numbered``). NEVER ``BY_NAME``.
  * ``REFRESH_IN_FLIGHT`` + N -> resolve against the still-VISIBLE previous
    snapshot. That is ``prior_pinned_snapshot_id`` when
    ``prior_pin_deferred`` is True (a refresh build already pinned a new,
    not-yet-painted snapshot and deferred the prior's unpin), ELSE
    ``pinned_snapshot_id`` (the prior build has not returned yet). Same
    FOUND / miss handling as ``PAINTED``. NEVER ``BY_NAME``.
  * ``WALK_IN_FLIGHT`` / ``PAINT_IN_FLIGHT`` / ``PAUSED`` + N -> ``HELD``
    ("queue or drop", up to 200 ms). The real hold timer is owned by the
    integration bead; this resolver only flags the HELD decision.
  * ``ERROR`` + N -> ``NOTICE`` (``numbers_not_showing``). NEVER click,
    NEVER ``BY_NAME``.

The ``reason`` tags (``no_badge_numbered`` / ``numbers_not_showing``) are
open notice tags; wh-g4oma owns the user-facing wording. This module is the
EMIT SITE for the tag, not the wording.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from services.wheelhouse.click_overlay_state import OverlayState
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
    ResolveOutcome,
    resolve_display_number,
)


# Notice reason tags this resolver may emit (open set; wh-g4oma owns wording).
NO_BADGE_NUMBERED = "no_badge_numbered"
NUMBERS_NOT_SHOWING = "numbers_not_showing"
# wh-overlay-fixqueue-review.2: emitted by the integration's renumber guard
# (not by route_click_n itself) when a "click N" lands inside the grace
# window after a PROACTIVE refresh swap and badge N changed identity.
OVERLAY_NUMBERS_CHANGED = "overlay_numbers_changed"


class RoutingKind(enum.Enum):
    """How a 'click N' utterance should be routed.

    BY_NAME: fall through to the existing by-name ``click_element`` flow
      (``forward_click_element`` send_request path) with the spoken text as
      the query. Used for a non-integer in any state and for ``CLOSED`` + N.
    SNAPSHOT_ITEM: resolve a numbered badge to an overlay item; the
      decision carries ``snapshot_id`` and ``item_id`` for the
      ``click_snapshot_item`` dispatch.
    NOTICE: surface a notice (``reason`` carries the tag) and do NOT click;
      used for a numbered-overlay miss (``no_badge_numbered``) and the
      ``ERROR`` reject (``numbers_not_showing``).
    HELD: nothing is reliably visible yet (``walk_in_flight`` /
      ``paint_in_flight`` / ``paused``); hold under the "queue or drop" rule.
    """

    BY_NAME = "by_name"
    SNAPSHOT_ITEM = "snapshot_item"
    NOTICE = "notice"
    HELD = "held"


@dataclass(frozen=True)
class RoutingDecision:
    """The pure routing decision for a 'click N' utterance.

    ``item_id`` is set only for ``SNAPSHOT_ITEM``; ``reason`` is set only for
    ``NOTICE``. ``snapshot_id`` is set for ``SNAPSHOT_ITEM`` (the resolved
    badge's snapshot) AND for a resolvable-but-miss ``NOTICE`` (the VISIBLE
    snapshot the number was resolved against, so the notice never references a
    snapshot the user did not see -- wh-n29v.18.1); it is ``None`` for a
    ``NOTICE`` with no pinned snapshot, for the ``ERROR``-state reject NOTICE,
    for ``BY_NAME``, and for ``HELD``.
    """

    kind: RoutingKind
    snapshot_id: Optional[str] = None
    item_id: Optional[str] = None
    reason: Optional[str] = None


# States that hold a numeric click ("queue or drop", up to 200 ms).
_HELD_STATES = frozenset(
    {
        OverlayState.WALK_IN_FLIGHT,
        OverlayState.PAINT_IN_FLIGHT,
        OverlayState.PAUSED,
    }
)


def route_click_n(
    *,
    state: OverlayState,
    parsed_number: Optional[int],
    cache: ClickSnapshotSummaryCache,
    pinned_snapshot_id: Optional[str],
    prior_pinned_snapshot_id: Optional[str],
    prior_pin_deferred: bool,
    visible_window_is_foreground: Optional[bool] = None,
) -> RoutingDecision:
    """Decide how to route a 'click ...' utterance, purely.

    ``parsed_number`` is the result of running the captured text through
    ``parse_number_word`` -- a positive integer or ``None``. The pin fields
    mirror the overlay state machine's ``pinned_snapshot_id`` /
    ``prior_pinned_snapshot_id`` / ``prior_pin_deferred`` so the caller does
    not couple this resolver to the machine instance.

    ``visible_window_is_foreground`` gates ONLY ``REFRESH_IN_FLIGHT`` and is the
    integration layer's answer to "does the still-visible snapshot belong to the
    window that is currently foreground?" -- ``False`` means a focus-change
    refresh left a stale-window overlay on screen, so HOLD rather than dispatch a
    click Input would reject (trigger B, wh-overlay-snapshot-keepalive). ``None``
    (the default, undeterminable) and ``True`` (same-window content refresh)
    preserve the resolve-against-visible behaviour. It is ignored in every state
    other than ``REFRESH_IN_FLIGHT``.

    Returns a :class:`RoutingDecision`. See the module docstring for the full
    rule set. A resolvable-but-miss NOTICE (``painted`` / ``refresh_in_flight``
    with a number that has no badge) carries ``snapshot_id`` = the VISIBLE
    snapshot the number was resolved against, so the notice never references a
    snapshot the user did not see (wh-n29v.18.1). Never raises on well-typed
    input; an unexpected ``OverlayState`` value falls through to ``BY_NAME``
    (the safe default -- the by-name flow surfaces its own ``not_found``
    notice).
    """

    # Non-integer (a name, a role keyword, unparseable) -> by-name in every
    # state. Also covers the documented "click seven button" case: the parser
    # only ran on a role-less query, so the caller passes parsed_number=None
    # for any query that carried a role keyword.
    if parsed_number is None:
        return RoutingDecision(RoutingKind.BY_NAME)

    if state is OverlayState.CLOSED:
        # The spoken digit is looked up by name; the by-name flow may or may
        # not find a control whose accessible name is the digit.
        return RoutingDecision(RoutingKind.BY_NAME)

    if state is OverlayState.ERROR:
        # Reject: numbers aren't showing. Never click, never by-name.
        return RoutingDecision(
            RoutingKind.NOTICE, reason=NUMBERS_NOT_SHOWING,
        )

    if state in _HELD_STATES:
        # Nothing reliably visible yet (walk/paint) or paused: hold.
        return RoutingDecision(RoutingKind.HELD)

    if state is OverlayState.PAINTED:
        return _resolve_visible(cache, pinned_snapshot_id, parsed_number)

    if state is OverlayState.REFRESH_IN_FLIGHT:
        # Trigger B (wh-overlay-snapshot-keepalive): a refresh caused by focus
        # moving to a DIFFERENT window leaves the still-visible snapshot pinned
        # to the window that is no longer foreground. Resolving N against it and
        # dispatching click_snapshot_item would make Input reject it on a
        # foreground-identity mismatch (surfaced as snapshot_expired on a
        # still-visible overlay). HOLD instead, so the integration's hold timer
        # re-resolves against the freshly-built list once the new window's
        # overlay paints. The flag is None (undeterminable) or True (a
        # same-window content refresh) in every other case, both of which keep
        # the prior resolve-against-visible behaviour.
        if visible_window_is_foreground is False:
            return RoutingDecision(RoutingKind.HELD)
        # The still-VISIBLE snapshot during a refresh is the prior one when a
        # build already pinned a new not-yet-painted snapshot (deferred unpin);
        # otherwise it is the current pin (the prior build has not returned).
        visible = (
            prior_pinned_snapshot_id if prior_pin_deferred
            else pinned_snapshot_id
        )
        return _resolve_visible(cache, visible, parsed_number)

    # Defensive: an unmodelled state. Fall back to by-name rather than drop
    # the click silently.
    return RoutingDecision(RoutingKind.BY_NAME)


def _resolve_visible(
    cache: ClickSnapshotSummaryCache,
    visible_snapshot_id: Optional[str],
    number: int,
) -> RoutingDecision:
    """Resolve ``number`` against the visible snapshot, or a miss notice.

    A miss (no pinned snapshot, an expired/evicted snapshot, or a number
    with no matching badge) returns a ``no_badge_numbered`` NOTICE -- NEVER
    a by-name fall-through, per the v4 routing rules (a number that is not on
    screen must not surface a misleading "found nothing matching 'N'").

    The miss NOTICE carries ``snapshot_id=visible_snapshot_id`` -- the
    snapshot the number was actually resolved against (the VISIBLE one) --
    when a snapshot was pinned, so the notice never references a snapshot the
    user did not see (wh-n29v.18.1; matters in ``refresh_in_flight`` with a
    deferred prior, where the visible snapshot is the prior, not the current
    pin). When ``visible_snapshot_id`` is ``None`` (no pinned snapshot) the
    NOTICE carries ``snapshot_id=None``.
    """

    if visible_snapshot_id is None:
        return RoutingDecision(RoutingKind.NOTICE, reason=NO_BADGE_NUMBERED)
    result = resolve_display_number(cache, visible_snapshot_id, number)
    if result.outcome is ResolveOutcome.FOUND:
        return RoutingDecision(
            RoutingKind.SNAPSHOT_ITEM,
            snapshot_id=visible_snapshot_id,
            item_id=result.item_id,
        )
    # NOT_FOUND and SNAPSHOT_EXPIRED both collapse to the same user surface:
    # the spoken number is not on the visible overlay. Carry the
    # resolved-against (visible) snapshot id so the notice references the
    # snapshot the user actually saw, not a not-yet-painted current pin.
    return RoutingDecision(
        RoutingKind.NOTICE,
        snapshot_id=visible_snapshot_id,
        reason=NO_BADGE_NUMBERED,
    )


def renumber_click_is_safe(
    prior_summary,
    current_summary,
    number: int,
) -> bool:
    """Decide whether "click N" is safe right after a proactive refresh swap.

    wh-overlay-fixqueue-review.2: a timer-driven (proactive) refresh can
    renumber badges between the user reading badge N and their "click N"
    transcript arriving. N then resolves against the NEW snapshot with fresh
    bounds, so the executor's stale-position refusal never fires and the
    click silently lands on the wrong control. This pure check compares the
    identity (accessible name, case-folded and stripped) of the item numbered
    ``number`` in the prior (pre-swap) and current summaries:

      * Prior or current summary unavailable -> True (best-effort guard,
        never a hard gate; the prior may simply have aged out of the cache).
      * No item numbered N in the prior summary -> True (the number is new,
        so the user can only have read it on the NEW overlay).
      * No item numbered N in the current summary -> True (defensive; the
        router already resolved N, so this cannot normally happen).
      * Name (case-folded, stripped), role, AND bounds all equal -> True
        (visually the same badge on an unchanged control: a re-walk of an
        unmoved control reproduces identical physical-pixel bounds, so the
        common idle-page refresh keeps clicking seamlessly).
      * Anything else -> False (badge N changed identity or position across
        the swap; the integration shows the "numbers just changed" notice
        instead).

    wh-overlay-fixqueue-review.3 (codex): the check was originally name-only,
    which waved through same-named controls -- browser pages commonly repeat
    names like "Delete" or "More" per row, so a row insert/remove during the
    grace window could move badge N from one "Delete" to another and the
    wrong row's action would fire. Role and bounds are part of the identity
    now; the cost is one extra safe refusal when the same-named control
    merely moved, which is exactly the case the user should re-check anyway.
    """
    if prior_summary is None or current_summary is None:
        return True

    def _item_at(summary):
        for item in summary.items:
            if item.display_number == number:
                return item
        return None

    prior = _item_at(prior_summary)
    if prior is None:
        return True
    current = _item_at(current_summary)
    if current is None:
        return True
    return (
        prior.name.strip().casefold() == current.name.strip().casefold()
        and prior.role == current.role
        and tuple(prior.bounds) == tuple(current.bounds)
    )
