"""Mutation evidence for the interior-run badge row
(wh-overlay-toolbar-badges-cover-icons).

A horizontal run of small controls away from every monitor edge -- a
browser's address-bar row of navigation and extension icons -- gets ONE
badge row just below the row band, in icon order, placed by
``OverlayPaintWindowManager._interior_run_placements_phys`` in
overlay_paint_window.py and substituted, after the per-badge walk, into
the caller's placements by ``_numeral_badge_placements_phys``. The guard tests are the class
TestInteriorToolbarRunRow in tests/test_overlay_paint_window.py. Each
mutation below breaks one behaviour of that pass and names the tests that
must fail for it to count as caught. Part 2 of the bead added the rule that
the left and right edge-cluster columns never claim a member of such a run
(``_small_control_runs``, shared by both passes); its mutations are the
part 2 section, guarded by TestEdgeColumnSkipsToolbarRuns and
TestEdgeClusterColumn. OPTION A added the rule that a top or bottom
edge-cluster row that would cover an icon of such a run is dropped; its
mutations are the last section, guarded by TestEdgeRowSkipsToolbarRunIcons.

Run from services/wheelhouse with the interpreter the whole worktree uses.
In a worktree that is the main checkout's interpreter, so <main> below is
the main checkout's root:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_overlay_interior_row.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_overlay_interior_row.py

A name filter runs a subset: ``... interior_row.py min-run`` runs every
mutation whose name contains "min-run"; the scope line says how many ran.

TWO DEPARTURES FROM tests/mutation_gate_overlay_wide_badge.py, both on
purpose:

  * pytest starts as ``<this interpreter> -m pytest`` in the service
    directory, never through scripts/run_tests.py, whose ``uv run`` builds a
    .venv inside a git worktree and makes the worktree undeletable. The
    launcher is the one in tests/mutation_gate_pattern_manager_tree_changed.py.
  * QT_QPA_PLATFORM is REMOVED from the environment, so pytest runs on the
    native Windows Qt platform. The offscreen platform measures the numeral
    font about 35 percent larger (8 pt, dpr 1.0: a one-digit badge is
    31 x 29 px offscreen and 23 x 25 px native, measured 2026-09-24). The
    guard tests pass on both platforms since 8b66c0db (they derive their
    layouts from the measured badge size, and David's layout asserts the
    edge-cluster outcome at the offscreen size), but the rows they prove
    are the rows the native size produces, and the app itself always runs
    on the native platform.

The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
owns everything else: exactly-one pattern matches, compiled mutants, the
expected-name check against real collection, the green baseline, the
per-run timeout, byte-exact restoration, and the short-summary verdicts.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

PAINT = "tests/test_overlay_paint_window.py"
OPW = "overlay_paint_window.py"

# --- expected catchers (TestInteriorToolbarRunRow) -------------------------
DAVID = "test_david_layout_rows_below_both_runs"
TABS_ABOVE = "test_tab_row_above_rows_below_both_runs"
FALLBACK = "test_small_controls_below_keep_todays_placement"
OVERLAP = "test_area_overlapping_controls_do_not_join_a_run"
# wh-overlay-toolbar-badges-cover-icons.1.1: rows never reroute other badges.
NO_NEW_OVERLAP_GRID = "test_packed_icon_grid_adds_no_badge_overlap"
NO_NEW_OVERLAP_RANDOM = "test_random_packed_layouts_add_no_badge_overlap"
WALKED_BADGE = "test_row_avoids_a_badge_the_walk_placed"
STALE_WALK = "test_accepted_row_frees_its_walk_footprints"
# KNOWN LIMIT 7 (RULING #4): walked badges block the row.
L7A = "test_walked_link_badge_leaves_the_left_run_as_today"
L7B = "test_walked_link_badge_shifts_the_left_row_clear"

MUTATIONS = []


def add(name, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=PAINT,
                          file=SERVICE / OPW, old=old, new=new,
                          expect=list(expect)))


# -- run membership ----------------------------------------------------------

# Runs of two form a row: the overlapping four icons split into two pairs,
# which must keep today's placement.
add("min-run-3-to-2",
    "_INTERIOR_RUN_MIN_COUNT = 3\n",
    "_INTERIOR_RUN_MIN_COUNT = 2\n",
    OVERLAP, DAVID, FALLBACK)
# A run of exactly three no longer forms a row.
add("min-run-3-to-4",
    "_INTERIOR_RUN_MIN_COUNT = 3\n",
    "_INTERIOR_RUN_MIN_COUNT = 4\n",
    "test_a_run_of_exactly_three_gets_a_row")
add("min-run-compare-inclusive",
    "                if len(run) < _INTERIOR_RUN_MIN_COUNT:\n",
    "                if len(run) <= _INTERIOR_RUN_MIN_COUNT:\n",
    "test_a_run_of_exactly_three_gets_a_row")

# Row grouping by vertical centre.
add("row-tolerance-to-zero",
    "_INTERIOR_ROW_CENTRE_TOLERANCE_FACTOR = 0.5\n",
    "_INTERIOR_ROW_CENTRE_TOLERANCE_FACTOR = 0.0\n",
    "test_row_grouping_tolerates_half_a_badge_height")
add("row-tolerance-to-three",
    "_INTERIOR_ROW_CENTRE_TOLERANCE_FACTOR = 0.5\n",
    "_INTERIOR_ROW_CENTRE_TOLERANCE_FACTOR = 3.0\n",
    DAVID, FALLBACK)

# Run continuation along the row.
add("join-gap-to-half",
    "_INTERIOR_RUN_JOIN_GAP_FACTOR = 1.0\n",
    "_INTERIOR_RUN_JOIN_GAP_FACTOR = 0.5\n",
    DAVID, "test_a_gap_under_one_badge_width_keeps_the_run")
add("join-gap-to-three",
    "_INTERIOR_RUN_JOIN_GAP_FACTOR = 1.0\n",
    "_INTERIOR_RUN_JOIN_GAP_FACTOR = 3.0\n",
    "test_a_gap_of_one_badge_width_ends_the_run")
add("join-gap-never-breaks",
    "                    b[1] - runs[-1][-1][3]\n"
    "                    >= _INTERIOR_RUN_JOIN_GAP_FACTOR * sizes[runs[-1][-1][0]][0]\n",
    "                    False\n",
    DAVID, FALLBACK)
add("area-overlap-joins-the-run",
    "                    or any(\n"
    "                        self._rects_overlap_phys(b[1:], m[1:])\n"
    "                        for m in runs[-1]\n"
    "                    )\n",
    "                    or False\n",
    OVERLAP)

# -- where the row goes ------------------------------------------------------

# The row top ignores every other numbered control that shares the band.
add("row-top-ignores-band-neighbours",
    "                    o[4] for o in boxes\n"
    "                    if o[0] not in run_ids\n",
    "                    o[4] for o in boxes\n"
    "                    if False\n",
    "test_row_top_clears_the_band_and_the_gap")
# The row top drops the collision gap below the band.
add("row-top-drops-the-gap",
    "            ) + gap\n"
    "            row_bottom = row_top + badge_h\n",
    "            )\n"
    "            row_bottom = row_top + badge_h\n",
    "test_row_top_clears_the_band_and_the_gap")
# The row is laid over the band instead of below it.
add("row-top-from-band-top",
    "                [band_bottom] + [\n",
    "                [band_top] + [\n",
    DAVID, TABS_ABOVE)

# -- the sideways shift ------------------------------------------------------

add("shift-range-3-to-2",
    "_INTERIOR_RUN_SHIFT_BADGE_WIDTHS = 3\n",
    "_INTERIOR_RUN_SHIFT_BADGE_WIDTHS = 2\n",
    DAVID)
add("shift-range-3-to-6",
    "_INTERIOR_RUN_SHIFT_BADGE_WIDTHS = 3\n",
    "_INTERIOR_RUN_SHIFT_BADGE_WIDTHS = 6\n",
    "test_shift_range_ends_at_three_badge_widths")
add("shift-step-1-to-4",
    "_INTERIOR_RUN_SHIFT_STEP_LOGICAL_PX = 1.0\n",
    "_INTERIOR_RUN_SHIFT_STEP_LOGICAL_PX = 4.0\n",
    "test_blocked_row_takes_the_smallest_clear_shift")
# The step is taken in physical px: at dpr > 1 the range shrinks by dpr.
add("shift-step-ignores-dpr",
    "                dx = shift * _INTERIOR_RUN_SHIFT_STEP_LOGICAL_PX * dpr\n",
    "                dx = shift * _INTERIOR_RUN_SHIFT_STEP_LOGICAL_PX\n",
    DAVID)
add("shift-left-before-right",
    "                shifts.extend((float(d), float(-d)))\n",
    "                shifts.extend((float(-d), float(d)))\n",
    "test_blocked_row_takes_the_smallest_clear_shift")
add("shift-never-zero",
    "            shifts = [0.0]\n",
    "            shifts = []\n",
    "test_unblocked_row_aims_each_badge_at_its_icon")
# No sideways shift at all: a row blocked at the unshifted position falls
# back, although a shift of a few px clears the link's walked badge.
add("no-sideways-shift",
    "            for d in range(1, steps + 1):\n",
    "            for d in range(1, 1):\n",
    L7B)

# -- what blocks the row -----------------------------------------------------

# Since wh-overlay-toolbar-badges-cover-icons.1.1 the row also avoids every
# badge the per-badge walk placed. In layouts (b)-(d) and David's layout the
# small controls' own badges sit in their trailing strips, so that second
# layer now blocks the same positions as the small-control boxes and strips:
# FALLBACK and DAVID no longer fail when only the first layer is removed.
# The shift tests (test_blocked_row_takes_the_smallest_clear_shift,
# test_shift_range_ends_at_three_badge_widths, and the left-corner test)
# place a blocker whose own badge is NOT in the row's way, so only the
# blocker box and strip decide the shift; they are the catchers below.

add("no-small-control-blocks",
    "            if not _is_small(b):\n"
    "                continue\n"
    "            strip = sizes[b[0]][0] + gap\n",
    "            if True:\n"
    "                continue\n"
    "            strip = sizes[b[0]][0] + gap\n",
    DAVID, "test_blocked_row_takes_the_smallest_clear_shift")
add("wide-controls-block-too",
    "            if not _is_small(b):\n"
    "                continue\n"
    "            strip = sizes[b[0]][0] + gap\n",
    "            if False:\n"
    "                continue\n"
    "            strip = sizes[b[0]][0] + gap\n",
    "test_wide_control_below_does_not_block_the_row")
add("right-corner-strip-dropped",
    "                blockers.append((b[0], (b[1], b[2], b[3] + strip, b[4])))\n",
    "                blockers.append((b[0], (b[1], b[2], b[3], b[4])))\n",
    "test_blocked_row_takes_the_smallest_clear_shift",
    "test_shift_range_ends_at_three_badge_widths")
add("left-corner-strip-dropped",
    "                blockers.append((b[0], (b[1] - strip, b[2], b[3], b[4])))\n",
    "                blockers.append((b[0], (b[1], b[2], b[3], b[4])))\n",
    "test_left_corner_keeps_the_blockers_leading_strip_free")
add("strip-side-swapped",
    "            if is_right:\n"
    "                blockers.append(",
    "            if not is_right:\n"
    "                blockers.append(",
    "test_blocked_row_takes_the_smallest_clear_shift",
    "test_left_corner_keeps_the_blockers_leading_strip_free")
add("placed-badges-do-not-block",
    "                ] + [(None, o) for o in placed] + [\n",
    "                ] + [\n",
    "test_adjacent_runs_rows_do_not_overlap")
add("row-may-leave-the-monitor-sideways",
    "                if cand[0][0] < 0.0 or cand[-1][2] > mon_w_phys:\n"
    "                    continue\n",
    "                if False:\n"
    "                    continue\n",
    "test_row_never_shifts_off_the_monitor")
add("row-may-leave-the-monitor-below",
    "            if row_top < 0.0 or row_bottom > mon_h_phys:\n"
    "                return None\n",
    "            if False:\n"
    "                return None\n",
    "test_row_below_the_monitor_bottom_falls_back")
add("blocked-row-placed-anyway",
    "                if any(\n"
    "                    self._rects_overlap_phys(c, o)\n"
    "                    for c in cand for _owner, o in obstacles\n"
    "                ):\n"
    "                    continue\n",
    "                if False:\n"
    "                    continue\n",
    DAVID, FALLBACK)
# No clear position: the row is placed at the unshifted position instead of
# falling back to today's per-badge path.
add("no-fallback-to-todays-path",
    "                return cand, moved\n"
    "            return None\n",
    "                return cand, moved\n"
    "            return [\n"
    "                (p, row_top, p + s, row_bottom)\n"
    "                for p, s in zip(pos, slots)\n"
    "            ], {}\n",
    DAVID, FALLBACK, L7A)

# -- the row's layout ----------------------------------------------------------

add("badges-in-reverse-icon-order",
    "            for b, rect in zip(run, row_rects):\n",
    "            for b, rect in zip(run[::-1], row_rects):\n",
    DAVID, TABS_ABOVE, OVERLAP)
add("no-overflow-split",
    "            excess = (pos[-1] - ideals[-1]) / 2.0\n",
    "            excess = 0.0\n",
    "test_unblocked_row_aims_each_badge_at_its_icon")
add("overflow-all-to-the-left",
    "            excess = (pos[-1] - ideals[-1]) / 2.0\n",
    "            excess = (pos[-1] - ideals[-1]) / 1.0\n",
    "test_unblocked_row_aims_each_badge_at_its_icon")
add("chain-drops-the-gap",
    "                if k and ideal < pos[-1] + slots[k - 1] + gap:\n"
    "                    ideal = pos[-1] + slots[k - 1] + gap\n",
    "                if k and ideal < pos[-1] + slots[k - 1]:\n"
    "                    ideal = pos[-1] + slots[k - 1]\n",
    FALLBACK, "test_unblocked_row_aims_each_badge_at_its_icon")
add("badge-aims-at-icon-left-edge",
    "                (b[1] + b[3]) / 2.0 - s / 2.0 for b, s in zip(run, slots)\n",
    "                b[1] for b, s in zip(run, slots)\n",
    "test_unblocked_row_aims_each_badge_at_its_icon")

# -- seeding and the call site -------------------------------------------------

# The row footprints no longer seed the pass's own placed list, so a later
# run in the same pass may overlap an earlier row.
add("rows-do-not-seed-later-runs",
    "            placed.extend(row_rects)\n",
    "            pass\n",
    "test_adjacent_runs_rows_do_not_overlap")
# The rows seed the per-badge walk again (the first build's structure,
# GLM finding .1.1): the rows are placed before the walk and the walk avoids
# them, so the collision nudges of badges in no run reroute and stack.
add("walk-seeded-by-rows",
    "        placed: \"list[tuple[float, float, float, float]]\" = [\n"
    "            cluster[i][2] for i in sorted(cluster)\n"
    "        ]\n",
    "        if rows_possible:\n"
    "            cluster = {**cluster, **self._interior_run_placements_phys(\n"
    "                badges, dpr, mon_w_phys, mon_h_phys, numeral_metrics,\n"
    "                cluster, {}, corner=corner,\n"
    "            )}\n"
    "        placed: \"list[tuple[float, float, float, float]]\" = [\n"
    "            cluster[i][2] for i in sorted(cluster)\n"
    "        ]\n",
    NO_NEW_OVERLAP_GRID, NO_NEW_OVERLAP_RANDOM)
# The row ignores the walk footprints of badges outside the run: a row may
# land on a badge that keeps its per-badge placement.
add("rows-ignore-walked-badges",
    "                    for i, fp in walked.items() if i not in run_ids\n",
    "                    for i, fp in walked.items() if False\n",
    NO_NEW_OVERLAP_GRID, NO_NEW_OVERLAP_RANDOM, WALKED_BADGE, L7A, L7B)
# The call site hands the pass no walk footprints: the same walk-blind row
# search, entered from the caller.
add("call-site-drops-the-walk-footprints",
    "                cluster, walked, corner=corner,\n",
    "                cluster, {}, corner=corner,\n",
    L7A, L7B)
# The run's own walk footprints block its row: they are replaced by the
# row, so they must not count.
add("run-blocked-by-its-own-walk-badges",
    "                    for i, fp in walked.items() if i not in run_ids\n",
    "                    for i, fp in walked.items() if True\n",
    TABS_ABOVE, "test_unblocked_row_aims_each_badge_at_its_icon")
# An accepted run's members keep their walk footprints as obstacles for
# later runs, although the row replaced them.
add("accepted-members-stay-obstacles",
    "                walked.pop(b[0], None)\n",
    "                pass\n",
    STALE_WALK)
add("call-site-drops-the-rows",
    "                out[i] = placement\n",
    "                pass\n",
    DAVID, TABS_ABOVE, OVERLAP)
add("call-site-ignores-edge-clusters",
    "                badges, dpr, mon_w_phys, mon_h_phys, numeral_metrics,\n"
    "                cluster, walked, corner=corner,\n",
    "                badges, dpr, mon_w_phys, mon_h_phys, numeral_metrics,\n"
    "                {}, walked, corner=corner,\n",
    "test_edge_cluster_run_is_not_claimed_again")

# -- RULING 1A: the row may cover the top strip of controls below the band ----
# When no shift is clear, the first shift that only controls lying wholly
# below the run's band block (and whose top strip, at most
# _INTERIOR_ROW_MAX_COVER_FRACTION of their height, is all the row covers)
# is taken, and the badges it covers are placed again by the per-badge
# placement; a moved badge that still overlaps a badge drops the row.
# Guard tests: TestToolbarRowOverPageControls.
MEAS_ROW = "test_toolbar_rows_below_icons_over_page_controls"
NO_ROOM = "test_displaced_badge_without_room_keeps_todays_placement"
ROOM = "test_displaced_badge_with_room_moves_and_the_row_is_taken"
BAND_BLOCKS = "test_control_overlapping_the_band_still_blocks_the_row"
BELOW_YIELDS = "test_control_wholly_below_the_band_yields_to_the_row"
# wh-overlay-toolbar-badges-cover-icons.2.1: since the detached-move guard,
# a layout reaches the cover cap and the one-attempt rule only through an
# ATTACHED move; these two tests build such layouts. The older catchers
# (DAVID and FALLBACK for the cap, MEAS_ROW for the next shift) reached
# them through moves that land detached, which the guard now refuses.
CAP_ATTACHED = "test_row_over_more_than_the_cap_blocks_an_attached_move"
NEXT_SHIFT = "test_failed_move_drops_the_row_although_a_later_shift_works"

# The wholly-below test excludes a control whose top lies ON the band's
# bottom.
add("1a-wholly-below-strict",
    "                return top >= band_bottom and (\n",
    "                return top > band_bottom and (\n",
    BELOW_YIELDS)
# A control that overlaps the band yields too.
add("1a-wholly-below-dropped",
    "                return top >= band_bottom and (\n",
    "                return True and (\n",
    BAND_BLOCKS)
# No cover cap: the row may cover any part of a control below the band.
add("1a-cover-cap-dropped",
    "                    row_bottom - top\n"
    "                    <= _INTERIOR_ROW_MAX_COVER_FRACTION * (bottom - top)\n",
    "                    True\n",
    CAP_ATTACHED, WALKED_BADGE, L7A, NO_NEW_OVERLAP_RANDOM,
    "test_row_never_shifts_off_the_monitor")
# The cap compares the wrong way: only a control the row covers mostly
# yields.
add("1a-cover-cap-flipped",
    "                    <= _INTERIOR_ROW_MAX_COVER_FRACTION * (bottom - top)\n",
    "                    >= _INTERIOR_ROW_MAX_COVER_FRACTION * (bottom - top)\n",
    MEAS_ROW, ROOM, BELOW_YIELDS)
add("1a-cover-cap-to-whole-control",
    "_INTERIOR_ROW_MAX_COVER_FRACTION = 0.5\n",
    "_INTERIOR_ROW_MAX_COVER_FRACTION = 1.0\n",
    CAP_ATTACHED)
add("1a-cover-cap-to-a-tenth",
    "_INTERIOR_ROW_MAX_COVER_FRACTION = 0.5\n",
    "_INTERIOR_ROW_MAX_COVER_FRACTION = 0.1\n",
    MEAS_ROW)
# Nothing ever yields: 1A never fires.
add("1a-every-obstacle-firm",
    "            firm = [o for owner, o in obstacles if owner is None]\n",
    "            firm = [o for owner, o in obstacles]\n",
    MEAS_ROW, ROOM, BELOW_YIELDS)
# The blocker box of a control below the band never yields.
add("1a-blocker-boxes-never-yield",
    "                    (i if _yields(i) else None, o) for i, o in blockers\n",
    "                    (None, o) for i, o in blockers\n",
    MEAS_ROW)
# The walked badge of a control below the band never yields.
add("1a-walked-badges-never-yield",
    "                    (i if _yields(i) else None, fp)\n",
    "                    (None, fp)\n",
    ROOM, BELOW_YIELDS)
# The row is taken but no covered badge moves: row and badge overlap.
add("1a-no-badge-displaced",
    "                moved = _move_displaced(displaced, cand, run_ids)\n",
    "                moved = _move_displaced([], cand, run_ids)\n",
    MEAS_ROW, ROOM, BELOW_YIELDS)
# Every yielding badge moves, whether the row covers it or not.
add("1a-displaced-ignores-the-row",
    "                    and any(self._rects_overlap_phys(c, fp) for c in cand)\n",
    "",
    MEAS_ROW)
# A moved badge that still overlaps a badge is accepted.
add("1a-move-overlap-accepted",
    "                if any(self._rects_overlap_phys(fp, o) for o in others):\n"
    "                    return None\n",
    "                if False:\n"
    "                    return None\n",
    NO_ROOM)
# A displaced badge is placed again without the row as an obstacle.
add("1a-move-ignores-the-row",
    "            others = row + placed + [\n",
    "            others = placed + [\n",
    NO_ROOM, ROOM)
# ... without the edge badges and the earlier rows as obstacles.
add("1a-move-ignores-placed-badges",
    "            others = row + placed + [\n",
    "            others = row + [\n",
    MEAS_ROW)
# ... without the walk footprints of the other badges as obstacles. The
# measured layout's test caught it until the neighbour-control check
# (wh-overlay-toolbar-badges-cover-icons.2.2); with that check in place it
# passes under this mutant. The shifted Gmail layouts still catch it: a
# moved badge lands on a walked badge (26 on 48, 27 on 119, 26 on 120), a
# pair the walk alone lacks.
add("1a-move-ignores-walked-badges",
    "                if i not in run_ids and i not in displaced\n",
    "                if False\n",
    "test_displaced_page_badge_never_moves_detached")
# A failed move tries the next shift instead of dropping the row. Before
# the detached-move guard, the measured layout caught it (Gmail's badges
# 47 and 48 moved at a later shift); those moves land detached, so now
# only NEXT_SHIFT, whose later shift gives an attached move, catches it.
# (g) cannot catch it: its row has one position only.
add("1a-failed-move-tries-next-shift",
    "                if moved is None:\n"
    "                    return None\n",
    "                if moved is None:\n"
    "                    continue\n",
    NEXT_SHIFT)
# The farthest shift is tried first instead of the nearest.
add("1a-farthest-shift-first",
    "            for cand in cands:\n",
    "            for cand in reversed(cands):\n",
    BELOW_YIELDS)
# A moved badge is not painted at its new place.
add("1a-moved-badge-not-painted",
    "                out[i] = (sizes[i][0], badge_h, fp)\n"
    "                walked[i] = fp\n",
    "                walked[i] = fp\n",
    ROOM, BELOW_YIELDS)
# wh-overlay-toolbar-badges-cover-icons.2.1: a moved badge that lands
# DETACHED from its own control is accepted (the guard removed). With the
# Gmail header cluster shifted, badges 47 and 48 land 18-95 logical px away;
# with the neighbour-control check (.2.2) in place, the shifted layouts'
# test passes under this mutant. The two constructed layouts, whose
# detached move covers no control, catch it: their rows are taken.
DETACHED_DROPS = (
    "test_displaced_badge_whose_only_clear_move_is_detached_drops_row"
)
add("1a-move-detached-accepted",
    "                    _bubble_drawing_state(fp, ctrl_rects_phys[j], dpr)\n"
    "                    == _BUBBLE_STATE_DETACHED\n",
    "                    False\n",
    DETACHED_DROPS,
    NEXT_SHIFT)
# The attachment is judged against the wrong control (the first one): moves
# that stay on their own control are refused and the rows are dropped.
add("1a-move-attached-to-wrong-control",
    "                    _bubble_drawing_state(fp, ctrl_rects_phys[j], dpr)\n",
    "                    _bubble_drawing_state(fp, ctrl_rects_phys[0], dpr)\n",
    MEAS_ROW)
# wh-overlay-toolbar-badges-cover-icons.2.2: a moved badge that overlaps
# ANOTHER numbered control's box is accepted (the check removed). On Codex's
# layout the row displaces #20's badge onto #21.
ONTO_NEIGHBOUR = "test_displaced_badge_never_moves_onto_another_control"
add("1a-move-onto-neighbour-accepted",
    "                if any(\n"
    "                    self._rects_overlap_phys(fp, c)\n"
    "                    for k, c in enumerate(ctrl_rects_phys) if k != j\n"
    "                ):\n",
    "                if False:\n",
    ONTO_NEIGHBOUR)
# The check also counts the badge's OWN control: a move that overlaps its own
# control is refused, so the (g) panel's move fails and the measured Gmail
# rows are dropped.
add("1a-move-own-control-counts-as-neighbour",
    "                    for k, c in enumerate(ctrl_rects_phys) if k != j\n",
    "                    for k, c in enumerate(ctrl_rects_phys)\n",
    MEAS_ROW, ROOM)
# NOT IN THE GATE, because no guard test tells these mutants from the code:
#   * a moved badge's new place is not written back to ``walked`` (drop
#     ``walked[i] = fp``), so it would not block a LATER run's row or
#     move. The measured and google.com-like layouts, (g), (h), and 1,620
#     layouts of the random generator place every badge identically with
#     and without it; it matters only when two runs displace badges near
#     each other.
#   * a displaced badge's own old footprint counts as an obstacle (drop
#     ``and i not in displaced``). In (g) and (h) the per-badge placement
#     steps down or aside from that same corner by a badge size plus the
#     gap, which already clears the old footprint, so nothing changes. In
#     the measured layout it moves badges 22 and 24-28 farther, and in
#     the google.com-like layout badges 19 and 20, with no overlap and no
#     cover the tests state as a defect.

# -- PART 2: the left/right edge columns skip toolbar runs ---------------------
# _edge_cluster_placements_phys must not put a member of a horizontal run of
# small controls (_small_control_runs, the interior-run definition) into a
# left or right column. Guard tests: TestEdgeColumnSkipsToolbarRuns (the
# measured Brave layout in tests/overlay_brave_layout_data.py, plus a
# google.com-like page and a mirrored copy), and TestEdgeClusterColumn for
# the columns and rows that must keep their placement.
EDGE_A = "test_edge_column_claims_neither_back_nor_forward"
EDGE_B = "test_measured_left_group_gets_its_row_below"

# The left column claims Back and Forward again (the measured defect).
add("edge-left-claims-toolbar-runs",
    "                    and b[0] not in in_row_run\n"
    "                    and b[3] <= band\n",
    "                    and b[3] <= band\n",
    EDGE_A, EDGE_B)
# The right column claims the mirrored Back and Forward.
add("edge-right-claims-toolbar-runs",
    "                    and b[0] not in in_row_run\n"
    "                    and b[1] >= mon_w_phys - band\n",
    "                    and b[1] >= mon_w_phys - band\n",
    EDGE_A)
# No run is found for the exclusion at all.
add("edge-exclusion-finds-no-runs",
    "            for run in self._small_control_runs(boxes, badge_sizes, badge_h)\n",
    "            for run in []\n",
    EDGE_A, EDGE_B)
# The runs are found among the edge-band controls only: Back and Forward
# are then a pair, not a run of three, and the left column claims them.
add("edge-runs-see-band-controls-only",
    "            for run in self._small_control_runs(boxes, badge_sizes, badge_h)\n",
    "            for run in self._small_control_runs(\n"
    "                [b for b in boxes\n"
    "                 if b[3] <= band or b[1] >= mon_w_phys - band],\n"
    "                badge_sizes, badge_h)\n",
    EDGE_A)
# The exclusion reaches the bottom edge: a packed tray row at the bottom
# loses its row.
add("edge-exclusion-reaches-bottom-row",
    "                    and b[2] >= mon_h_phys - band\n",
    "                    and b[0] not in in_row_run\n"
    "                    and b[2] >= mon_h_phys - band\n",
    "test_packed_bottom_tray_row_forms_ordered_row")
# The exclusion reaches the top edge: a packed run at the top edge loses
# its row.
add("edge-exclusion-reaches-top-row",
    "                    and b[4] <= band\n",
    "                    and b[0] not in in_row_run\n"
    "                    and b[4] <= band\n",
    "test_edge_cluster_run_is_not_claimed_again")
# A pair counts as a run for the exclusion too (the constant is shared):
# the two-across tray rows of a right-edge column leave the column.
add("min-run-3-to-2-reaches-edge-columns",
    "_INTERIOR_RUN_MIN_COUNT = 3\n",
    "_INTERIOR_RUN_MIN_COUNT = 2\n",
    "test_packed_tray_corner_forms_ordered_column")

# -- OPTION A: a top or bottom edge row never covers a toolbar run's icon -----
# _edge_cluster_placements_phys drops a top or bottom row that would cover
# an icon of a horizontal run of small controls reaching into that edge's
# band; its badges take the per-badge placement. Guard tests:
# TestEdgeRowSkipsToolbarRunIcons (the measured Brave layout with pinned
# tabs, and constructed top and bottom rows at scale 1.0).
OA_J = "test_measured_pinned_tab_badges_leave_the_toolbar_icons_clear"
OA_DROP = "test_row_over_a_run_reaching_into_the_band_falls_back"
OA_TWO = "test_row_over_two_icons_still_forms"
OA_PAST = "test_row_over_a_run_wholly_past_the_band_still_forms"

# The row is never dropped (the measured defect: badge 99 on Back).
add("oa-row-never-dropped",
    "                if any(\n"
    "                    self._rects_overlap_phys(rect, icon)\n",
    "                if False and any(\n"
    "                    self._rects_overlap_phys(rect, icon)\n",
    OA_J, OA_DROP)
# The row is dropped only when EVERY badge covers a toolbar icon.
add("oa-any-becomes-all",
    "                if any(\n"
    "                    self._rects_overlap_phys(rect, icon)\n",
    "                if all(\n"
    "                    self._rects_overlap_phys(rect, icon)\n",
    OA_J, OA_DROP)
# The top edge finds no toolbar icons.
add("oa-top-finds-no-icons",
    "            elif side == \"top\":\n"
    "                toolbar_icons = [\n",
    "            elif False:\n"
    "                toolbar_icons = [\n",
    OA_J, OA_DROP)
# The bottom edge finds no toolbar icons.
add("oa-bottom-finds-no-icons",
    "            if side == \"bottom\":\n"
    "                toolbar_icons = [\n",
    "            if False:\n"
    "                toolbar_icons = [\n",
    OA_DROP)
# Every small control in the band counts, not only a run's members: the
# row over two icons is dropped.
add("oa-top-any-control-counts",
    "                    if b[0] in in_row_run and b[2] < band\n",
    "                    if b[2] < band\n",
    OA_TWO)
add("oa-bottom-any-control-counts",
    "                    if b[0] in in_row_run and b[4] > mon_h_phys - band\n",
    "                    if b[4] > mon_h_phys - band\n",
    OA_TWO)
# A run wholly past the band counts too: the row over page icons drops.
add("oa-top-band-ignored",
    "                    if b[0] in in_row_run and b[2] < band\n",
    "                    if b[0] in in_row_run\n",
    OA_PAST)
add("oa-bottom-band-ignored",
    "                    if b[0] in in_row_run and b[4] > mon_h_phys - band\n",
    "                    if b[0] in in_row_run\n",
    OA_PAST)
# The band boundary: a run starting exactly at the band's end counts.
add("oa-top-band-inclusive",
    "                    if b[0] in in_row_run and b[2] < band\n",
    "                    if b[0] in in_row_run and b[2] <= band\n",
    OA_PAST)
add("oa-bottom-band-inclusive",
    "                    if b[0] in in_row_run and b[4] > mon_h_phys - band\n",
    "                    if b[0] in in_row_run and b[4] >= mon_h_phys - band\n",
    OA_PAST)
# The left and right columns are dropped too when they cover a run icon.
add("oa-columns-avoid-run-icons",
    "            toolbar_icons: \"list[tuple[float, float, float, float]]\" = []\n",
    "            toolbar_icons: \"list[tuple[float, float, float, float]]\" = [\n"
    "                b[1:] for b in boxes if b[0] in in_row_run\n"
    "            ]\n",
    "test_column_over_a_run_icon_still_forms")


def main(argv=None):
    # Native Qt platform for every pytest this gate starts (module docstring).
    # The runner copies os.environ into each launch.
    if os.environ.pop("QT_QPA_PLATFORM", None) is not None:
        print("QT_QPA_PLATFORM removed from the environment: native Qt platform")
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
