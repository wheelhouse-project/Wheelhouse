"""Mutation evidence for the wide-row badge rule (wh-vscode-menu-badge-misplaced).

Two halves of one change, each guarded by its own tests:

  * the walk-time mark in ui/uia_walker.py -- a browser-walk row whose
    rectangle is not fully inside the nearest preceding Menu element is
    marked ``bounds_outside_menu``;
  * the paint-time wide-row rule in overlay_paint_window.py -- a row that is
    not marked, lies fully on its monitor, and is at least 10 times wider
    than tall gets its numeral in the leading gutter (left for LTR text,
    right for RTL text);

plus the plumbing that carries the mark from the walk to the paint code
(both summary builders, both serializers, and the ``_do_paint`` wrap).

The walker selection lists node ids instead of the whole file because
tests/test_uia_walker.py holds one skipped live-desktop test, and the
JUnit verdict below refuses any run with a skipped test.

Run from services/wheelhouse:

    python tests/mutation_gate_overlay_wide_badge.py --check
    python tests/mutation_gate_overlay_wide_badge.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner

PAINT = "tests/test_overlay_paint_window.py"
FINDER = "tests/test_element_finder.py"
SHOW = "tests/test_show_numbered_overlay.py"
CLICK = "tests/test_click_element.py"
WALKER = tuple(
    f"tests/test_uia_walker.py::{name}" for name in (
        "test_build_matches_marks_row_outside_preceding_menu",
        "test_build_matches_row_inside_preceding_menu_not_marked",
        "test_build_matches_row_with_no_preceding_menu_not_marked",
        "test_build_matches_uses_nearest_preceding_menu",
        "test_build_matches_menu_dropped_by_offscreen_filter_still_counts",
        "test_build_matches_does_not_mark_by_default",
        "test_walk_window_marks_only_for_a_browser_walk",
        # Sanity: the unchanged build and walk behaviour around the mark.
        "test_build_matches_keeps_offscreen_and_zero_area_when_not_opted_in",
    )
)
MUTATIONS = []


def add(name, test_file, file, old, new, *expect):
    MUTATIONS.append(dict(name=name, service=SERVICE, test_file=test_file,
                          file=SERVICE / file, old=old, new=new, expect=list(expect)))


OPW = "overlay_paint_window.py"

# -- paint-time wide-row rule ------------------------------------------------

add("threshold-10-to-1000", PAINT, OPW,
    "_WIDE_ROW_MIN_ASPECT = 10\n",
    "_WIDE_ROW_MIN_ASPECT = 1000\n",
    "test_wide_valid_ltr_row_uses_left_gutter",
    "test_exactly_ten_to_one_applies",
    "test_vscode_like_twelve_to_one_row_applies",
    "test_threshold_constant_is_ten",
    "test_paint_places_wide_row_badge_in_leading_gutter")
add("threshold-ge-to-gt", PAINT, OPW,
    "            >= _WIDE_ROW_MIN_ASPECT * (bottom_phys - top_phys)",
    "            > _WIDE_ROW_MIN_ASPECT * (bottom_phys - top_phys)",
    "test_exactly_ten_to_one_applies")
add("drop-suspect-check", PAINT, OPW,
    "            and not rect.bounds_outside_menu\n",
    "            and True\n",
    "test_suspect_row_keeps_todays_placement",
    "test_paint_keeps_todays_placement_for_marked_row")
add("drop-on-monitor-check", PAINT, OPW,
    "            and left_phys >= 0.0\n"
    "            and top_phys >= 0.0\n"
    "            and right_phys <= mon_w_phys\n"
    "            and bottom_phys <= mon_h_phys\n",
    "            and True\n",
    "test_row_past_right_monitor_edge_keeps_todays_placement",
    "test_row_past_bottom_monitor_edge_keeps_todays_placement",
    "test_row_past_top_monitor_edge_keeps_todays_placement",
    "test_rtl_row_past_left_monitor_edge_keeps_todays_placement")
add("drop-on-monitor-left", PAINT, OPW,
    "            and left_phys >= 0.0\n            and top_phys >= 0.0\n",
    "            and True\n            and top_phys >= 0.0\n",
    "test_rtl_row_past_left_monitor_edge_keeps_todays_placement")
add("drop-on-monitor-top", PAINT, OPW,
    "            and top_phys >= 0.0\n            and right_phys <= mon_w_phys\n",
    "            and True\n            and right_phys <= mon_w_phys\n",
    "test_row_past_top_monitor_edge_keeps_todays_placement")
add("drop-on-monitor-right", PAINT, OPW,
    "            and right_phys <= mon_w_phys\n            and bottom_phys <= mon_h_phys\n",
    "            and True\n            and bottom_phys <= mon_h_phys\n",
    "test_row_past_right_monitor_edge_keeps_todays_placement")
add("drop-on-monitor-bottom", PAINT, OPW,
    "            and bottom_phys <= mon_h_phys\n            and (right_phys - left_phys)\n",
    "            and True\n            and (right_phys - left_phys)\n",
    "test_row_past_bottom_monitor_edge_keeps_todays_placement")
add("drop-wide-rule", PAINT, OPW,
    "            isinstance(rect, _TargetPaintRect)\n            and not rect.bounds_outside_menu\n",
    "            False\n            and not rect.bounds_outside_menu\n",
    "test_wide_valid_ltr_row_uses_left_gutter",
    "test_wide_valid_rtl_hebrew_row_uses_right_gutter",
    "test_paint_places_wide_row_badge_in_leading_gutter")
add("swap-rtl-side", PAINT, OPW,
    "                on_left=not _name_is_right_to_left(rect.target_name)",
    "                on_left=_name_is_right_to_left(rect.target_name)",
    "test_wide_valid_ltr_row_uses_left_gutter",
    "test_wide_valid_rtl_hebrew_row_uses_right_gutter",
    "test_wide_valid_rtl_arabic_row_uses_right_gutter",
    "test_paint_places_wide_row_badge_in_leading_gutter")
add("arabic-not-rtl", PAINT, OPW,
    '        if direction in ("R", "AL"):',
    '        if direction == "R":',
    "test_wide_valid_rtl_arabic_row_uses_right_gutter",
    "test_name_is_right_to_left")
add("no-strong-character-is-rtl", PAINT, OPW,
    "            return False\n    return False\n\n\nclass WNDCLASSEXW",
    "            return False\n    return True\n\n\nclass WNDCLASSEXW",
    "test_name_without_strong_character_is_ltr",
    "test_name_is_right_to_left")
add("ignore-first-strong-ltr", PAINT, OPW,
    '        if direction == "L":\n            return False\n    return False',
    '        if False:\n            return False\n    return False',
    "test_first_strong_character_decides_direction",
    "test_name_is_right_to_left")
add("gutter-ignores-controls", PAINT, OPW,
    "                on_monitor\n                and not _hits_others(gutter)\n                and not _hits_placed(gutter)\n            ):\n                return gutter\n            return None",
    "                on_monitor\n                and True\n                and not _hits_placed(gutter)\n            ):\n                return gutter\n            return None",
    "test_gutter_blocked_by_numbered_control_falls_through")
add("gutter-ignores-placed-badges", PAINT, OPW,
    "                and not _hits_others(gutter)\n                and not _hits_placed(gutter)\n            ):\n                return gutter\n            return None",
    "                and not _hits_others(gutter)\n                and True\n            ):\n                return gutter\n            return None",
    "test_gutter_blocked_by_placed_badge_falls_through")
add("gutter-ignores-monitor", PAINT, OPW,
    "            if (\n                on_monitor\n                and not _hits_others(gutter)",
    "            if (\n                True\n                and not _hits_others(gutter)",
    "test_gutter_off_monitor_falls_through")
add("gutter-ignores-bottom-corner", PAINT, OPW,
    "            g_top = bottom_phys - badge_h_phys if is_bottom else top_phys\n            if mon_h_phys is not None:",
    "            g_top = top_phys\n            if mon_h_phys is not None:",
    "test_ltr_row_uses_left_gutter_under_left_corner_bottom")
add("paint-drops-the-mark", PAINT, OPW,
    '                        getattr(item, "bounds_outside_menu", False) is not False\n',
    '                        False\n',
    "test_paint_keeps_todays_placement_for_marked_row")
add("paint-drops-the-name-wrap", PAINT, OPW,
    "            if isinstance(target_name, str) and isinstance(rect, OverlayPaintRect):",
    "            if False:",
    "test_paint_places_wide_row_badge_in_leading_gutter")

# -- walk-time mark ----------------------------------------------------------

UW = "ui/uia_walker.py"
add("invert-containment", WALKER, UW,
    "        if preceding_menu_bounds is not None and not _bounds_inside(",
    "        if preceding_menu_bounds is not None and _bounds_inside(",
    "test_build_matches_marks_row_outside_preceding_menu",
    "test_build_matches_row_inside_preceding_menu_not_marked",
    "test_build_matches_uses_nearest_preceding_menu",
    "test_walk_window_marks_only_for_a_browser_walk")
add("drop-containment", WALKER, UW,
    "        if preceding_menu_bounds is not None and not _bounds_inside(\n"
    "            match.bounds, preceding_menu_bounds\n"
    "        ):",
    "        if preceding_menu_bounds is not None:",
    "test_build_matches_row_inside_preceding_menu_not_marked",
    "test_build_matches_uses_nearest_preceding_menu")
add("never-mark", WALKER, UW,
    "            match = replace(match, bounds_outside_menu=True)",
    "            pass",
    "test_build_matches_marks_row_outside_preceding_menu",
    "test_build_matches_uses_nearest_preceding_menu",
    "test_build_matches_menu_dropped_by_offscreen_filter_still_counts",
    "test_walk_window_marks_only_for_a_browser_walk")
add("containment-ignores-bottom-edge", WALKER, UW,
    "ix + iw <= ox + ow and iy + ih <= oy + oh",
    "ix + iw <= ox + ow",
    "test_build_matches_marks_row_outside_preceding_menu",
    "test_walk_window_marks_only_for_a_browser_walk")
add("first-menu-not-nearest", WALKER, UW,
    "        if mark_bounds_outside_menu and control_type_id == UIA_MENU:",
    "        if mark_bounds_outside_menu and control_type_id == UIA_MENU and menu_bounds is None:",
    "test_build_matches_uses_nearest_preceding_menu")
add("menu-must-be-on-screen", WALKER, UW,
    "        if mark_bounds_outside_menu and control_type_id == UIA_MENU:",
    "        if mark_bounds_outside_menu and control_type_id == UIA_MENU and not _cached_is_offscreen(element):",
    "test_build_matches_menu_dropped_by_offscreen_filter_still_counts")
add("mark-on-by-default", WALKER, UW,
    "    mark_bounds_outside_menu: bool = False,",
    "    mark_bounds_outside_menu: bool = True,",
    "test_build_matches_does_not_mark_by_default")
add("native-walk-marks", WALKER, UW,
    "        mark_bounds_outside_menu=browser_correction_hook is not None,",
    "        mark_bounds_outside_menu=True,",
    "test_walk_window_marks_only_for_a_browser_walk")
add("browser-walk-does-not-mark", WALKER, UW,
    "        mark_bounds_outside_menu=browser_correction_hook is not None,",
    "        mark_bounds_outside_menu=False,",
    "test_walk_window_marks_only_for_a_browser_walk")

# -- plumbing: summary builders and serializers ------------------------------

EF = "ui/element_finder.py"
add("build-summary-drops-mark", FINDER, EF,
    "                bounds_outside_menu=match.bounds_outside_menu,",
    "                bounds_outside_menu=False,",
    "test_find_browser_summary_carries_bounds_outside_menu",
    "test_overlay_walk_browser_summary_carries_bounds_outside_menu")
add("renumber-drops-mark", FINDER, EF,
    "                    bounds_outside_menu=item.bounds_outside_menu,",
    "                    bounds_outside_menu=False,",
    "test_filter_and_renumber_summary_keeps_bounds_outside_menu")

for label, test_file, file, error_test_names in (
    ("serde", SHOW, "shared/walk_snapshot_serde.py", (
        "test_summary_round_trip_keeps_bounds_outside_menu",
        "test_summary_item_missing_bounds_outside_menu_reads_false",
        "test_summary_item_non_bool_bounds_outside_menu_raises")),
    ("click-element", CLICK, "shared/click_element.py", (
        "test_round_trip_keeps_bounds_outside_menu",
        "test_from_dict_missing_bounds_outside_menu_reads_false",
        "test_from_dict_rejects_non_bool_bounds_outside_menu")),
):
    round_trip, missing_key, non_bool = error_test_names
    add(f"{label}-drops-the-key", test_file, file,
        '                "bounds_outside_menu": item.bounds_outside_menu,\n',
        "",
        round_trip)
    add(f"{label}-missing-key-reads-true", test_file, file,
        '        raw["bounds_outside_menu"] if "bounds_outside_menu" in raw else False',
        '        raw["bounds_outside_menu"] if "bounds_outside_menu" in raw else True',
        missing_key)
    add(f"{label}-accepts-non-bool", test_file, file,
        "    if not isinstance(bounds_outside_menu, bool):",
        "    if False:",
        non_bool)
    add(f"{label}-reads-default", test_file, file,
        "        bounds_outside_menu=bounds_outside_menu,\n    )",
        "        bounds_outside_menu=False,\n    )",
        round_trip)


# The shared runner owns launches, persisted admission, and source restoration.
# This adapter retains the stricter JUnit verdict and aggregate evidence
# (copied from tests/mutation_gate_captured_target_identity.py).
import hashlib
import json
import os
from unittest.mock import patch
import xml.etree.ElementTree as ET


class OwnedGate:
    def __init__(self, evidence_dir):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.reports = []
        self.original = {}

    @property
    def cleanup_confirmed(self):
        try:
            runner._admit_execution()
        except OSError:
            return False
        return True

    def snapshot(self, mutations):
        self.original = {m["file"]: m["file"].read_bytes() for m in mutations}
        for index, (path, data) in enumerate(self.original.items()):
            (self.evidence_dir / f"original-{index}.bin").write_bytes(data)

    def call(self, service, selection, collect=False):
        if service.resolve() != SERVICE.resolve():
            raise ValueError("This gate only owns the WheelHouse service")
        label = f"run-{len(self.reports):02d}"
        report = dict(label=label, collect=collect)
        self.reports.append(report)
        try:
            with patch.dict(os.environ, QT_QPA_PLATFORM="offscreen"):
                result = runner._invoke(service, selection, collect=collect)
        except BaseException as exc:
            report["exception"] = type(exc).__name__
            report["cleanup_confirmed"] = self.cleanup_confirmed
            raise
        report.update(exit=result.returncode, cleanup_confirmed=True)
        (self.evidence_dir / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if not collect:
            paths = [arg.split("=", 1)[1] for arg in result.args if arg.startswith("--junitxml=")]
            if len(paths) != 1:
                raise RuntimeError("Owned runner did not identify one JUnit report")
            xml = Path(paths[0])
            report["junit"] = str(xml)
            parsed = ET.parse(xml).getroot()
            report["failed"] = [tc.attrib["name"] for tc in parsed.iter("testcase") if tc.find("failure") is not None]
            report["errors"] = len(list(parsed.iter("error")))
            report["skipped"] = len(list(parsed.iter("skipped")))
            if report["errors"] or report["skipped"]:
                raise RuntimeError(f"Invalid mutation verdict: {report}")
        return result

    def save_report(self):
        restored = {str(path.relative_to(ROOT)): path.read_bytes() == data
                    for path, data in self.original.items()}
        hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.original}
        for index, path in enumerate(self.original):
            (self.evidence_dir / f"current-{index}.bin").write_bytes(path.read_bytes())
        (self.evidence_dir / "mutations.json").write_text(json.dumps(dict(
            reports=self.reports, cleanup_confirmed=self.cleanup_confirmed,
            restored=restored, hashes=hashes,
        ), indent=2), encoding="utf-8")
        return restored

    def run(self, mutations, argv=None):
        self.snapshot(mutations)
        try:
            with patch.dict(os.environ, QT_QPA_PLATFORM="offscreen"), \
                 patch.object(runner, "_run_pytest", self.call):
                status = runner.run(mutations, argv)
        finally:
            restored = self.save_report()
        if not self.cleanup_confirmed or not all(restored.values()):
            return 1
        return status


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check" in args:
        return runner.run(MUTATIONS, args)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return OwnedGate(ROOT / ".tmp/overlay-wide-badge-mutations" / stamp).run(MUTATIONS, args)


if __name__ == "__main__":
    raise SystemExit(main())
