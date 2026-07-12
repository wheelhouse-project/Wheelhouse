"""Unit tests for the voice-element-clicking clear-winner rule (wh-3blym).

Covers every branch of ``ClearWinnerRule.decide`` and the six-part
mouse-cursor tiebreaker from the v5 design doc
(docs/plans/2026-05-21-voice-element-clicking-design-v5.md, sections
"Clear-winner rule (run by ClearWinnerRule.decide)" and "Mouse-cursor
tiebreaker (v4 expanded)").

``decide`` is a pure function over a list of already-scored, already-eligible
ElementMatch objects -- no I/O, no config reads, no COM. The per-monitor DPI
lookup is injected as a ``dpi_resolver`` callable so the physical/logical pixel
conversions are exercised with deterministic values.

The ``_query`` / ``_match`` builders mirror tests/test_confidence_scorer.py;
``_match`` here additionally takes ``score``, ``bounds`` and ``monitor_id`` so
each test can place candidates precisely for the tiebreaker.
"""

import pytest

from ui.clear_winner_rule import Outcome, decide
from ui.element_types import ElementMatch, ElementQuery


def _query(name: str, role: str | None = None) -> ElementQuery:
    return ElementQuery(
        name=name,
        role=role,
        ordinal=None,
        spatial=None,
        raw_utterance=name,
    )


def _match(
    name: str,
    *,
    score: float,
    bounds: tuple[int, int, int, int] = (0, 0, 10, 10),
    monitor_id: int = 0,
    role: str = "Button",
    invoke_supported: bool = True,
    is_enabled: bool = True,
    item_id: str = "m1",
    display_number: int = 1,
) -> ElementMatch:
    return ElementMatch(
        item_id=item_id,
        display_number=display_number,
        name=name,
        role=role,
        bounds=bounds,
        monitor_id=monitor_id,
        score=score,
        is_eligible=True,
        source="uia",
        invoke_supported=invoke_supported,
        is_enabled=is_enabled,
        control_ref=object(),
    )


def _dpi_96(_monitor_id: int) -> float:
    """A flat 96-DPI resolver: physical pixels == logical pixels."""
    return 96.0


# --- min_confidence drop -----------------------------------------------------


def test_below_min_confidence_dropped_to_not_found():
    # A single enabled match below min_confidence is dropped; with no disabled
    # control it becomes not_found.
    m = _match("Cancel", score=0.3, is_enabled=True)
    out = decide([m], (5, 5), 0, _dpi_96)
    assert out.outcome == "not_found"
    assert out.winner is None
    assert out.candidates == ()


def test_at_min_confidence_kept_single_ok():
    # Exactly at the threshold is kept (>= comparison): single match -> ok.
    m = _match("Cancel", score=0.4)
    out = decide([m], (5, 5), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is m


# --- empty / not_found / disabled -------------------------------------------


def test_empty_input_not_found():
    out = decide([], (5, 5), 0, _dpi_96)
    assert out.outcome == "not_found"
    assert out.winner is None


def test_empty_after_drop_with_disabled_execution_failed():
    # Every kept match is empty after the confidence drop, BUT an eligible
    # disabled control (is_enabled False) existed -> execution_failed:disabled.
    # The disabled control here scores below min_confidence on its own (no
    # enabled bonus, weak name signal) so it is dropped from the kept list, yet
    # its presence flips the empty outcome from not_found to disabled.
    disabled = _match("Delete", score=0.3, is_enabled=False, invoke_supported=False)
    out = decide([disabled], (5, 5), 0, _dpi_96)
    assert out.outcome == "execution_failed"
    assert out.reason == "disabled"
    # The matched name must be reachable for the notice wording.
    assert out.winner is not None
    assert out.winner.name == "Delete"


def test_disabled_above_min_confidence_surfaces_disabled():
    # A disabled control that scores AT/above min_confidence survives the
    # confidence drop and becomes the single kept winner, but decide must NOT
    # return ok for a disabled winner -- it surfaces execution_failed:disabled
    # at walk time rather than leaning on the ClickExecutor's pre-click re-read
    # (wh-9f3t.22.1).
    disabled = _match("Delete", score=0.4, is_enabled=False)
    out = decide([disabled], (5, 5), 0, _dpi_96)
    assert out.outcome == "execution_failed"
    assert out.reason == "disabled"
    assert out.winner is disabled


def test_margin_winner_disabled_surfaces_disabled():
    # The margin-winner branch (top - next >= margin) must also downgrade a
    # disabled winner. top disabled at 0.9, enabled next at 0.7 -> margin 0.2
    # picks top, but top is disabled -> execution_failed:disabled (wh-9f3t.22.1).
    top = _match("Delete", score=0.9, is_enabled=False, item_id="top")
    other = _match("Delete All", score=0.7, is_enabled=True, item_id="other")
    out = decide([top, other], (5, 5), 0, _dpi_96)
    assert out.outcome == "execution_failed"
    assert out.reason == "disabled"
    assert out.winner is top


def test_tiebreaker_disabled_alsoran_does_not_block_enabled_winner():
    # A disabled also-ran in the close set must NOT block an enabled winner.
    # Both on the cursor monitor; enabled "a" is closest (centre 50), disabled
    # "b" is farther (centre 200): separation 150 >= 30 -> ok, enabled a wins.
    a = _match(
        "Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10),
        is_enabled=True, item_id="a",
    )
    b = _match(
        "Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10),
        is_enabled=False, item_id="b",
    )
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is a


def test_tiebreaker_disabled_winner_surfaces_disabled():
    # When the tiebreaker-resolved winner (closest to cursor) is disabled, the
    # outcome must be execution_failed:disabled (wh-9f3t.22.1). disabled "a" is
    # closest (centre 50), enabled "b" farther (centre 200): separation 150 >=
    # 30 -> a wins the tiebreaker, but a is disabled.
    a = _match(
        "Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10),
        is_enabled=False, item_id="a",
    )
    b = _match(
        "Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10),
        is_enabled=True, item_id="b",
    )
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "execution_failed"
    assert out.reason == "disabled"
    assert out.winner is a


def test_empty_after_drop_no_disabled_is_not_found():
    # Below-confidence ENABLED control with no disabled control present:
    # not_found, never disabled.
    m = _match("Cancel", score=0.2, is_enabled=True)
    out = decide([m], (5, 5), 0, _dpi_96)
    assert out.outcome == "not_found"
    assert out.reason is None


# --- single / clear-margin ok -----------------------------------------------


def test_single_match_ok():
    m = _match("Submit", score=0.8)
    out = decide([m], (5, 5), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is m
    assert out.candidates == ()


def test_two_matches_clear_margin_ok():
    # top 0.9, next 0.7 -> margin 0.2 >= 0.15 -> ok top, no tiebreaker.
    top = _match("Save", score=0.9, item_id="top")
    other = _match("Save As", score=0.7, item_id="other")
    out = decide([top, other], (5, 5), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is top


def test_margin_exactly_at_threshold_ok():
    # margin exactly 0.15 -> >= clear_winner_margin -> ok (no tiebreaker).
    top = _match("Save", score=0.75, item_id="top")
    other = _match("Save As", score=0.60, item_id="other")
    out = decide([top, other], (5, 5), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is top


# --- tiebreaker: cross-monitor filter ---------------------------------------


def test_tiebreaker_cross_monitor_drops_below_two_ambiguous():
    # Two close-set candidates but the second is on a different monitor than
    # the cursor; after filtering, fewer than two remain on the cursor monitor
    # -> ambiguous. The close set is returned as candidates.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(0, 0, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=1, bounds=(0, 0, 10, 10), item_id="b")
    out = decide([a, b], (5, 5), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


# --- tiebreaker: influence radius -------------------------------------------


def test_tiebreaker_closest_beyond_influence_radius_ambiguous():
    # Both candidates on the cursor monitor but the closest is farther than the
    # influence radius (400 logical px at 96 DPI) -> abstain -> ambiguous.
    # Cursor at (0,0); candidate centres at (1000,0) and (1100,0): closest is
    # 1000 px > 400 -> ambiguous.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(995, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(1095, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}


# --- tiebreaker: separation below threshold ---------------------------------


def test_tiebreaker_separation_below_threshold_ambiguous():
    # Both within the influence radius, but their distances to the cursor
    # differ by less than tiebreaker_min_separation_logical_px (30) -> ambiguous.
    # Cursor (0,0). Centre a at (100,0) dist 100, centre b at (120,0) dist 120;
    # separation 20 < 30 -> ambiguous.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(95, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(115, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}


def test_tiebreaker_clean_win_ok():
    # Both within influence radius, separation >= 30 -> closest wins.
    # Cursor (0,0). Centre a at (50,0) dist 50, b at (200,0) dist 200;
    # separation 150 >= 30 -> ok, a wins.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is a


def test_tiebreaker_separation_exactly_threshold_ok():
    # Separation exactly 30 logical px -> >= threshold -> closest wins.
    # Cursor (0,0). a centre (50,0) dist 50; b centre (80,0) dist 80; sep 30.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(75, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is a


# --- close-set definition ----------------------------------------------------


def test_close_set_excludes_match_outside_margin():
    # Three matches: top 0.80, second 0.70 (within 0.15 of top), third 0.50
    # (outside the margin). The close set is {top, second}; the third is not a
    # tiebreaker candidate and is not in the returned ambiguous candidates.
    # Put top & second on different monitors so the tiebreaker abstains and we
    # can inspect the close set via candidates.
    top = _match("Close", score=0.80, monitor_id=0, item_id="top")
    second = _match("Close", score=0.70, monitor_id=1, item_id="second")
    third = _match("Closet", score=0.50, monitor_id=0, item_id="third")
    out = decide([top, second, third], (5, 5), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {top, second}
    assert third not in out.candidates


# --- equal-score stable sort -------------------------------------------------


def test_equal_score_stable_sort_first_input_is_top():
    # Two equal-score matches on different monitors so the tiebreaker abstains.
    # margin top-next = 0 < 0.15, so it would run the tiebreaker; the cross-
    # monitor filter then makes it ambiguous, with both in the close set. This
    # exercises the stable-sort tiebreak: input order is preserved for equal
    # scores (first input ranks as "top").
    first = _match("Close", score=0.7, monitor_id=0, item_id="first")
    secnd = _match("Close", score=0.7, monitor_id=1, item_id="secnd")
    out = decide([first, secnd], (5, 5), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {first, secnd}


def test_equal_score_three_way_all_in_close_set():
    # Three equal scores -> all in the close set; cross-monitor split forces
    # ambiguous and confirms all three are returned.
    a = _match("Close", score=0.6, monitor_id=0, item_id="a")
    b = _match("Close", score=0.6, monitor_id=1, item_id="b")
    c = _match("Close", score=0.6, monitor_id=2, item_id="c")
    out = decide([a, b, c], (5, 5), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b, c}


# --- mixed-DPI conversion ----------------------------------------------------


def _dpi_192_on_cursor_monitor(monitor_id: int) -> float:
    # Cursor monitor (id 0) is a 2x HiDPI display (192 DPI); everything else 96.
    return 192.0 if monitor_id == 0 else 96.0


def test_mixed_dpi_influence_radius_uses_cursor_monitor_dpi():
    # On a 192-DPI cursor monitor the influence radius is
    # 400 * 192 / 96 = 800 physical px. A candidate whose closest physical
    # distance is 700 px is INSIDE the radius (would be OUTSIDE at 96 DPI's
    # 400 px), proving the cursor-monitor DPI scales the radius.
    # Cursor (0,0). a centre (700,0) dist 700; b centre (1100,0) dist 1100.
    # Separation physical = 400; logical = 400 * 96 / 192 = 200 >= 30 -> ok, a.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(695, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(1095, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_192_on_cursor_monitor)
    assert out.outcome == "ok"
    assert out.winner is a


def test_mixed_dpi_separation_conversion_uses_cursor_monitor_dpi():
    # On a 192-DPI cursor monitor a physical separation of 40 px converts to
    # 40 * 96 / 192 = 20 logical px, which is BELOW the 30-logical-px threshold
    # -> ambiguous. At 96 DPI the same 40 physical px would be 40 logical >= 30
    # and would win, so this proves the separation conversion uses the cursor
    # monitor DPI. Both well inside the 800 px influence radius.
    # Cursor (0,0). a centre (100,0) dist 100; b centre (140,0) dist 140; sep 40.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(95, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(135, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_192_on_cursor_monitor)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}


# --- dpi_resolver fault -> fail closed (wh-9f3t.21.4 / wh-9f3t.21.1) ---------


def test_dpi_resolver_raises_fails_closed_ambiguous():
    # If dpi_resolver raises, the tiebreaker must fail closed to ambiguous
    # rather than letting the exception escape decide(). Two same-monitor close
    # candidates well within a sane influence radius -- only the DPI fault
    # forces the abstention.
    def _raises(_monitor_id: int) -> float:
        raise RuntimeError("display topology changed mid-decide")

    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _raises)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


def test_dpi_resolver_zero_fails_closed_ambiguous():
    # A returned DPI of 0 would divide-by-zero in both conversions; the guard
    # must fail closed to ambiguous. Same clean two-candidate setup.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, lambda _m: 0.0)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


def test_dpi_resolver_nan_fails_closed_ambiguous():
    # NaN is not a finite value > 0; the `not isfinite(cursor_dpi)` arm of the
    # guard must fail closed to ambiguous rather than propagate NaN through the
    # influence-radius / separation conversions (which would make every
    # comparison False and silently mis-resolve) (wh-9f3t.23.1).
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, lambda _m: float("nan"))
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


def test_dpi_resolver_inf_fails_closed_ambiguous():
    # +Inf is also not finite; the same `not isfinite` arm must fail closed.
    # An infinite DPI would blow the influence radius up to +Inf (every
    # candidate "inside") yet collapse the separation conversion to 0 logical
    # px, so it must be rejected up front (wh-9f3t.23.1).
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, lambda _m: float("inf"))
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


def test_dpi_resolver_none_fails_closed_ambiguous():
    # A resolver returning None (e.g. a monitor lookup that silently misses)
    # hits the `cursor_dpi is None` arm of the guard and fails closed. The
    # annotation is Callable[[int], float]; a None return is a runtime
    # degenerate value the guard defends against (wh-9f3t.23.1).
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, lambda _m: None)  # type: ignore[arg-type,return-value]
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


# --- equal physical distance -> separation 0 (wh-9f3t.23.2) ------------------


def test_equal_physical_distance_separation_zero_ambiguous():
    # Two candidates on the SAME monitor at EXACTLY equal physical distance from
    # the cursor exercise the physical-distance sort (existing equal-score tests
    # split candidates across monitors and never reach it). Cursor (0,0); centre
    # a at (50,0) dist 50; centre b at (-50,0) dist 50. Both within the 400 px
    # influence radius, so the influence check passes; the closest/second
    # distances are equal so separation is 0 < 30 -> ambiguous, both returned
    # (wh-9f3t.23.2).
    a = _match("Close", score=0.8, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(-55, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ambiguous"
    assert set(out.candidates) == {a, b}
    assert out.winner is None


# --- influence-radius boundary (wh-9f3t.21.4) --------------------------------


def test_closest_exactly_at_influence_radius_does_not_abstain():
    # The influence check abstains only when closest_phys > influence_phys, so
    # a closest distance EXACTLY at the radius must NOT abstain -- it proceeds
    # to the separation check and resolves to ok when separation suffices.
    # At 96 DPI influence_phys = 400. Place closest centre at (400,0) dist 400
    # (exactly at radius) and second at (600,0) dist 600: separation 200 >= 30
    # -> ok, closest wins.
    a = _match("Close", score=0.8, monitor_id=0, bounds=(395, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.8, monitor_id=0, bounds=(595, -5, 10, 10), item_id="b")
    out = decide([a, b], (0, 0), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is a


# --- three-candidate clean resolution (wh-9f3t.21.4) -------------------------


def test_three_candidates_same_monitor_tiebreaker_resolves_ok():
    # Three equal-score candidates all on the cursor monitor and all in the
    # close set; the tiebreaker resolves to a winner (closest) because the
    # nearest two are separated by >= 30 logical px. Cursor (0,0): centres at
    # 50, 200, 350; closest 50, second 200, separation 150 >= 30 -> ok, a.
    a = _match("Close", score=0.7, monitor_id=0, bounds=(45, -5, 10, 10), item_id="a")
    b = _match("Close", score=0.7, monitor_id=0, bounds=(195, -5, 10, 10), item_id="b")
    c = _match("Close", score=0.7, monitor_id=0, bounds=(345, -5, 10, 10), item_id="c")
    out = decide([a, b, c], (0, 0), 0, _dpi_96)
    assert out.outcome == "ok"
    assert out.winner is a


# --- Outcome shape -----------------------------------------------------------


def test_outcome_is_frozen():
    out = decide([_match("X", score=0.9)], (0, 0), 0, _dpi_96)
    assert isinstance(out, Outcome)
    with pytest.raises(Exception):
        out.outcome = "not_found"  # type: ignore[misc]
