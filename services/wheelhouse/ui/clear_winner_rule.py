"""Clear-winner rule + mouse-cursor tiebreaker for element clicking (wh-3blym).

A single pure function, ``decide``, split out from the UIA strategy so it can
be unit-tested against plain ElementMatch lists with no COM, no I/O, and no
config reads. It is the third pure-decision slice of the voice-element-clicking
feature (epic wh-l4h.1), sitting after ``ui.confidence_scorer``:

    walker -> confidence_scorer.is_eligible / .score -> ClearWinnerRule.decide

The authoritative spec is the v5 design doc,
docs/plans/2026-05-21-voice-element-clicking-design-v5.md, sections
"Clear-winner rule (run by ClearWinnerRule.decide)" and "Mouse-cursor
tiebreaker (v4 expanded)". confidence_scorer owns "is this match eligible?"
and "how good is it?"; this module owns the empty/single/multi decision and
the tiebreaker only. It receives the already-filtered, already-scored eligible
matches; it never re-runs eligibility or re-scores.

Config-free design (mirrors confidence_scorer): every threshold and the
per-monitor DPI lookup are injected as parameters. ``dpi_resolver`` is a
``Callable[[int], float]`` mapping a monitor_id to that monitor's effective
DPI, so ``GetDpiForMonitor`` is supplied by the caller and this function stays
pure and deterministic in tests.

Three non-obvious design decisions, documented here per the slice contract:

1. **Equal-score sort tiebreak.** Step 2 sorts by score descending with a
   STABLE sort (Python's ``sorted`` is stable). For equal scores the input
   order is preserved, so the first match the walker emitted ranks as "top".
   This is deterministic and matches the walker's own ordering; the
   clear-winner margin between two equal scores is 0, so equal top scores
   always fall through to the tiebreaker rather than being decided by sort
   order. The stable sort therefore only fixes which equal-score match is
   *labelled* top/next for the margin comparison, never the final outcome.

2. **How "an eligible disabled control existed" is detected.** This slice
   receives already-eligible scored matches. A disabled control reaches here
   in ``scored`` with ``is_enabled is False`` (confidence_scorer's eligibility
   table row d keeps a disabled real control with an exact/starts-with name).
   So the disabled signal is simply: any match in the ORIGINAL ``scored`` list
   with ``is_enabled is False``. This is the simplest correct design that keeps
   ``decide`` pure -- no separate parameter, no re-derivation of the name
   signal (eligibility already guaranteed the exact/starts-with name). The
   disabled branch only fires when the kept (confidence-passing) list is empty,
   per spec step 4 (wh-3blym).

3. **Close-set definition.** The tiebreaker (and the ambiguous notice) operate
   on the "close set": the kept matches whose score is within
   ``clear_winner_margin`` of the top score, i.e. ``score >= top - margin``.
   The top match itself is in the close set (distance 0). Matches further than
   the margin below the top are not tiebreaker candidates and are not returned
   in the ambiguous candidate set.

Pixel-unit contract (the arithmetic reviewers probe):

* ElementMatch.bounds is ``(x, y, width, height)`` in PHYSICAL screen pixels;
  WalkSnapshot.cursor_at_walk is also PHYSICAL screen pixels. So distances are
  computed directly in physical pixels with no conversion (step 4).
* ``tiebreaker_influence_logical_px`` and ``tiebreaker_min_separation_logical_px``
  are LOGICAL pixels defined at 96 DPI. The conversion to/from physical uses the
  CURSOR monitor's effective DPI (the user's spatial intuition is anchored to
  where they were looking, per the v5 doc), via ``dpi_resolver(cursor_monitor_id)``:
    - influence radius (logical -> physical):
        influence_physical = influence_logical * dpi / 96.0
    - separation (physical -> logical):
        separation_logical = separation_physical * 96.0 / dpi
"""

from dataclasses import dataclass
from math import hypot, isfinite
from typing import Callable, Literal

from ui.element_types import ElementMatch

# DPI at which the logical-pixel thresholds are defined. Windows' baseline.
_BASE_DPI = 96.0


@dataclass(frozen=True)
class Outcome:
    """Result of ``decide``.

    Outcome is Input-process-local and must NOT cross the Input -> Logic -> GUI
    boundary as-is: ``winner`` and ``candidates`` hold ElementMatch objects that
    carry a live COM ``control_ref`` (exactly like element_types.WalkSnapshot),
    which cannot be pickled across a process boundary. The wh-agd2v coordinator
    must project plain display-safe data (e.g. the matched names) out of this
    Outcome before any IPC send (wh-9f3t.21.2).

    Fields:
        outcome: the decision tag.
        reason: a tag string when ``outcome == "execution_failed"`` -- only
            ``"disabled"`` in this slice (the v5 "Outcome reporting" section
            owns the full reason vocabulary; the executor slice fills the
            click-time reasons). ``None`` otherwise.
        winner: the single winning ElementMatch when ``outcome == "ok"``. Also
            set on ``execution_failed:disabled`` to the matched disabled control
            so the coordinator can name it in the "'<name>' is disabled" notice
            (this is the chosen way to make the matched name reachable -- a
            dedicated field would duplicate what ``winner`` already carries).
            ``None`` for ``not_found`` and ``ambiguous``.
        candidates: the close set returned for the ``ambiguous`` notice so the
            coordinator can build the "Found 'X' and 'Y'" wording. Empty tuple
            for every non-ambiguous outcome.
    """

    outcome: Literal["ok", "not_found", "ambiguous", "execution_failed"]
    reason: str | None
    winner: ElementMatch | None
    candidates: tuple[ElementMatch, ...]


def _centre(match: ElementMatch) -> tuple[float, float]:
    """Physical-pixel centre of a match's bounding rectangle.

    bounds is (x, y, width, height); centre = (x + width/2, y + height/2).
    """
    x, y, width, height = match.bounds
    return (x + width / 2.0, y + height / 2.0)


def _ok_or_disabled(
    match: ElementMatch,
    candidates: tuple[ElementMatch, ...] = (),
) -> Outcome:
    """Wrap a determined winner, downgrading a disabled winner to disabled.

    decide() never returns ``outcome="ok"`` for a disabled winner: the v5
    eligibility table and the ``execution_failed:disabled`` reason both define
    a disabled control (IsEnabled false at walk OR click time) as a disabled
    outcome, so decide surfaces it at walk time rather than leaning on the
    ClickExecutor's later pre-click IsEnabled re-read. Only the match that would
    actually be clicked (the winner) triggers this -- a disabled also-ran that
    is not the winner does not block an enabled winner (wh-9f3t.22.1).

    This is the single ok-producing helper called by all three winner sites
    (single-kept, margin-winner, tiebreaker-resolved).
    """
    if not match.is_enabled:
        return Outcome(
            outcome="execution_failed",
            reason="disabled",
            winner=match,
            candidates=(),
        )
    return Outcome(
        outcome="ok",
        reason=None,
        winner=match,
        candidates=candidates,
    )


def decide(
    scored: list[ElementMatch],
    cursor_at_walk: tuple[int, int],
    cursor_monitor_id: int,
    dpi_resolver: Callable[[int], float],
    *,
    min_confidence: float = 0.4,
    clear_winner_margin: float = 0.15,
    tiebreaker_influence_logical_px: float = 400.0,
    tiebreaker_min_separation_logical_px: float = 30.0,
) -> Outcome:
    """Apply the v5 clear-winner rule + mouse-cursor tiebreaker.

    ``scored`` is the list of eligible matches the confidence scorer produced,
    each with ``.score`` set and ``.is_eligible`` True. ``cursor_at_walk`` is
    the snapshot cursor position (physical screen pixels); ``cursor_monitor_id``
    is the monitor that contained it at walk time. ``dpi_resolver`` maps a
    monitor_id to that monitor's effective DPI.

    Returns an :class:`Outcome`. See the module docstring for the close-set,
    disabled-detection, and pixel-unit contracts.
    """
    # --- Clear-winner step 1: drop matches below min_confidence. -------------
    kept = [m for m in scored if m.score >= min_confidence]

    # Disabled detection (wh-3blym): a disabled control reaches this slice in
    # the ORIGINAL scored list with is_enabled False (confidence_scorer row d
    # already proved its name is exact/starts-with). We look at the original
    # list, not the kept list, because a disabled control with no enabled bonus
    # may score below min_confidence and be dropped from kept -- its presence
    # still flips an empty result from not_found to disabled (spec step 4).
    disabled_present = next((m for m in scored if not m.is_enabled), None)

    # --- Clear-winner step 2: stable sort by score, highest first. -----------
    # sorted() is stable, so equal scores keep their input order (decision 1).
    ranked = sorted(kept, key=lambda m: m.score, reverse=True)

    # --- Clear-winner steps 3 & 4: empty kept list. --------------------------
    if not ranked:
        if disabled_present is not None:
            # Step 4: surface the matched disabled control's name via winner.
            return Outcome(
                outcome="execution_failed",
                reason="disabled",
                winner=disabled_present,
                candidates=(),
            )
        # Step 3: nothing kept and nothing disabled -> not found.
        return Outcome(
            outcome="not_found",
            reason=None,
            winner=None,
            candidates=(),
        )

    # --- Clear-winner step 5: exactly one kept match. ------------------------
    if len(ranked) == 1:
        return _ok_or_disabled(ranked[0])

    # --- Clear-winner step 6: two or more kept. ------------------------------
    top = ranked[0]
    nxt = ranked[1]
    # Exact float equality at the margin boundary is safe ONLY because
    # confidence_scorer's score weights are clean 0.1-step values (0.5, 0.4,
    # 0.3, 0.1), so score differences and the 0.15 margin are exactly
    # representable enough for this >= to behave. If a future scoring weight
    # introduces a non-0.1-step value, revisit whether math.isclose is needed
    # here. Conscious accept, do not "fix" the comparison (wh-9f3t.21.3).
    if top.score - nxt.score >= clear_winner_margin:
        # Clear winner by margin; no tiebreaker.
        return _ok_or_disabled(top)

    # Margin below threshold -> run the tiebreaker on the close set.
    # Close set: kept matches within clear_winner_margin of the top score
    # (top itself included, distance 0). See module docstring decision 3.
    close_set = tuple(m for m in ranked if top.score - m.score <= clear_winner_margin)
    return _tiebreaker(
        close_set=close_set,
        cursor_at_walk=cursor_at_walk,
        cursor_monitor_id=cursor_monitor_id,
        dpi_resolver=dpi_resolver,
        tiebreaker_influence_logical_px=tiebreaker_influence_logical_px,
        tiebreaker_min_separation_logical_px=tiebreaker_min_separation_logical_px,
    )


def _tiebreaker(
    *,
    close_set: tuple[ElementMatch, ...],
    cursor_at_walk: tuple[int, int],
    cursor_monitor_id: int,
    dpi_resolver: Callable[[int], float],
    tiebreaker_influence_logical_px: float,
    tiebreaker_min_separation_logical_px: float,
) -> Outcome:
    """The six-part mouse-cursor tiebreaker (v5 "Mouse-cursor tiebreaker").

    On any abstention the close set is returned as ``candidates`` so the
    coordinator can build the "Found 'X' and 'Y'" ambiguous notice.
    """
    cursor_x, cursor_y = cursor_at_walk

    # Step 1: the cursor's monitor is the supplied cursor_monitor_id.
    # Step 2: drop candidates not on the cursor's monitor (cross-monitor
    # distance is meaningless -- the cursor was on a different display).
    on_cursor_monitor = [m for m in close_set if m.monitor_id == cursor_monitor_id]

    # Step 3: fewer than two left on the cursor monitor -> abstain.
    if len(on_cursor_monitor) < 2:
        return Outcome(
            outcome="ambiguous",
            reason=None,
            winner=None,
            candidates=close_set,
        )

    # Step 4: Euclidean distance from each candidate's bounding-rectangle centre
    # to the cursor, in PHYSICAL pixels (bounds and cursor are both physical).
    def _phys_distance(match: ElementMatch) -> float:
        cx, cy = _centre(match)
        return hypot(cx - cursor_x, cy - cursor_y)

    # Stable sort by physical distance, closest first. sorted() is stable, so
    # equal distances keep their close-set order (deterministic).
    by_distance = sorted(on_cursor_monitor, key=_phys_distance)
    closest = by_distance[0]
    second = by_distance[1]
    closest_phys = _phys_distance(closest)
    second_phys = _phys_distance(second)

    # The cursor monitor's effective DPI drives both conversions (the user's
    # spatial intuition is anchored to where they were looking, per v5).
    # The display topology can change between walk and decide, so the injected
    # GetDpiForMonitor lookup can raise or return a degenerate value (0 or
    # negative would divide-by-zero / collapse the conversions). Fail closed to
    # the same ambiguous abstention every other tiebreaker bail-out uses, rather
    # than letting the exception escape decide() or silently corrupting the
    # distance math (wh-9f3t.21.1).
    try:
        cursor_dpi = dpi_resolver(cursor_monitor_id)
    except Exception:
        cursor_dpi = None
    if cursor_dpi is None or not isfinite(cursor_dpi) or cursor_dpi <= 0:
        return Outcome(
            outcome="ambiguous",
            reason=None,
            winner=None,
            candidates=close_set,
        )

    # Step 5: influence radius. The logical threshold (at 96 DPI) is scaled UP
    # to physical pixels for the cursor monitor: physical = logical * dpi / 96.
    # If the closest candidate is farther than that, the cursor is too far from
    # any candidate to be a meaningful pointer -> abstain.
    influence_phys = tiebreaker_influence_logical_px * cursor_dpi / _BASE_DPI
    if closest_phys > influence_phys:
        return Outcome(
            outcome="ambiguous",
            reason=None,
            winner=None,
            candidates=close_set,
        )

    # Step 6: convert the physical separation (second - closest) back to logical
    # pixels using the cursor monitor's DPI: logical = physical * 96 / dpi. If
    # the logical separation meets the threshold, the closest candidate wins;
    # otherwise the two are too close together to disambiguate -> ambiguous.
    separation_phys = second_phys - closest_phys
    separation_logical = separation_phys * _BASE_DPI / cursor_dpi
    if separation_logical >= tiebreaker_min_separation_logical_px:
        # The closest candidate is the resolved winner; downgrade to
        # execution_failed:disabled if it happens to be disabled (wh-9f3t.22.1).
        return _ok_or_disabled(closest)

    return Outcome(
        outcome="ambiguous",
        reason=None,
        winner=None,
        candidates=close_set,
    )
