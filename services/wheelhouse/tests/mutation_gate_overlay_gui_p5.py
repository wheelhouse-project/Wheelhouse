"""Mutation evidence for the P5 requirement tests (wh-full-refactor.7.1).

Area P5: the mouse grid drawing methods. Stage B of place 4
(wh-full-refactor.7.2) moved them from OverlayPaintWindowManager
(overlay_paint_window.py) to OverlayGridPainter (overlay_paint/grid.py); the
manager keeps delegators. Each mutation names its row GP-n from the P5 notes
of stage A of place 4, and the boundary tests of
tests/test_overlay_gui_requirements_p5.py that must catch it. 33 mutations
are in overlay_paint/grid.py. Where the window class _OverlayWindow or the
shared constant _OVERLAY_EX_STYLE decides a rule of the grid window, the
mutation is there, in overlay_paint_window.py (2 mutations); the test still
enters through paint_grid, paint_grid_pin and clear_grid.

Same form and the same shared runner as tests/mutation_gate_mouse_grid_l5.py
(read the docstring of tests/mutation_gate_overlay_requirements.py for the
disciplines the runner owns). A mutation marked as an expected survivor
gives its reason as survives=; the sweep fails when any test of its
selection fails under it, because the reason is then no longer true.

Run from services/wheelhouse:
  uv run python tests/mutation_gate_overlay_gui_p5.py --check
  uv run python tests/mutation_gate_overlay_gui_p5.py
  uv run python tests/mutation_gate_overlay_gui_p5.py gp-1-grid-margin-ignores-the-scale
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Run pytest with this interpreter, so a worktree builds no new venv."""
    command = [
        sys.executable, "-m", "pytest", *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_runner_failed_names = runner._failed_names


def _failed_names(output):
    """The runner's failed-name set, cut at the first "[" of a parameter id."""
    return {name.split("[", 1)[0] for name in _runner_failed_names(output)}


runner._failed_names = _failed_names

MUTATIONS = []

_T = "test_overlay_gui_requirements_p5.py"
_P = "overlay_paint_window.py"
_PT = "overlay_paint/teardown.py"
_PG = "overlay_paint/grid.py"
SUITE = (f"tests/{_T}",)


def add(name, file, old, new, *expect, suite=SUITE, survives=None):
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


GP_1 = "test_gp_1_grid_gets_one_window_over_its_rectangle"
GP_2 = "test_gp_2_grid_window_is_click_through_and_never_activated"
GP_3 = "test_gp_3_lines_lie_on_the_cell_boundaries"
GP_4 = "test_gp_4_nine_labels_sit_on_the_cell_centers_in_keypad_order"
GP_4B = "test_gp_4b_labels_sit_on_the_integer_cell_centers_when_the_size_does_not_divide_by_3"
GP_5 = "test_gp_5_label_shadow_follows_the_shadow_setting"
GP_6 = "test_gp_6_pin_marks_its_point"
GP_7 = "test_gp_7_grid_and_pin_share_one_window_and_both_show"
GP_8 = "test_gp_8_pin_on_another_monitor_gets_its_own_window"
GP_9 = "test_gp_9_pin_on_no_monitor_is_dropped"
GP_10 = "test_gp_10_grid_draws_on_the_monitor_its_event_names"
GP_11 = "test_gp_11_grid_whose_monitor_is_gone_draws_nothing"
GP_12 = "test_gp_12_refined_grid_rebuilds_and_a_repeat_reuses_the_window"
GP_13 = "test_gp_13_failed_composite_returns_false_and_removes_the_window"
GP_14 = "test_gp_14_failed_window_creation_returns_false_and_leaves_no_window"
GP_15 = "test_gp_15_clear_grid_removes_the_grid_and_the_pin_and_forgets_both"
GP_16 = "test_gp_16_window_that_survives_clear_grid_is_retried_at_the_next_paint"

# --- GP-1: the grid window covers the rectangle plus the ink margin ----------

add("gp-1-grid-margin-ignores-the-scale", _PG,
    "        return int(math.ceil(_GRID_INK_MARGIN_LOGICAL_PX * scale))\n",
    "        return int(math.ceil(_GRID_INK_MARGIN_LOGICAL_PX))\n",
    GP_1)
add("gp-1-grid-margin-on-one-side-only", _PG,
    "                int(self._grid_event.width) + 2 * margin,\n",
    "                int(self._grid_event.width) + margin,\n",
    GP_1)
add("gp-1-grid-region-not-clamped", _PG,
    "                int(self._grid_event.height) + 2 * margin,\n"
    "            ).intersected(grid_monitor.rect_phys)\n",
    "                int(self._grid_event.height) + 2 * margin,\n"
    "            )\n",
    GP_1)
add("gp-1-composite-at-the-desktop-origin", _PG,
    "                ok = window.composite(dib, geom.left(), geom.top())\n",
    "                ok = window.composite(dib, 0, 0)\n",
    GP_1)

# --- GP-2: the window is click-through and never activated -------------------

add("gp-2-window-not-click-through", _P,
    "    | WS_EX_TOPMOST\n"
    "    | WS_EX_TRANSPARENT\n",
    "    | WS_EX_TOPMOST\n",
    GP_2)
add("gp-2-window-shown-with-activation", _P,
    "        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)\n",
    "        user32.ShowWindow(self.hwnd, 5)\n",
    GP_2)

# --- GP-3: the lines lie on the cell boundaries ------------------------------

add("gp-3-lines-at-equal-thirds", _PG,
    "        xs = (\n"
    "            cells[0].left,\n"
    "            cells[1].left,\n",
    "        xs = (\n"
    "            cells[0].left,\n"
    "            cells[0].left + rect.width() / 3.0,\n",
    GP_3)
add("gp-3-lines-half-a-pixel-right", _PG,
    "            QLineF(x, ys[0], x, ys[3]) for x in xs\n",
    "            QLineF(x + 0.5, ys[0], x + 0.5, ys[3]) for x in xs\n",
    GP_3)
add("gp-3-no-horizontal-lines", _PG,
    "        ] + [\n"
    "            QLineF(xs[0], y, xs[3], y) for y in ys\n"
    "        ]\n",
    "        ]\n",
    GP_3)

# --- GP-4: nine labels on the cell centers, in keypad order -------------------

add("gp-4-labels-in-reverse-order", _PG,
    "            cell = cells[number - 1]\n"
    "            center = _grid_cell_center(cell)\n",
    "            cell = cells[9 - number]\n"
    "            center = _grid_cell_center(cell)\n",
    GP_4)
add("gp-4-label-baseline-on-the-center", _PG,
    "            center_y + metrics.capHeight() / 2.0,\n",
    "            center_y,\n",
    GP_4)
add("gp-4b-labels-at-float-thirds", _PG,
    "                float(center.x),\n"
    "                float(center.y),\n",
    "                grid.left + grid.width * (((number - 1) % 3) + 0.5) / 3,\n"
    "                grid.top + grid.height * (((number - 1) // 3) + 0.5) / 3,\n",
    GP_4B)
add("gp-4-label-size-cap-lowered", _PG,
    "        high = _GRID_LABEL_MAX_LOGICAL_PX * scale\n",
    "        high = _GRID_LABEL_MAX_LOGICAL_PX * scale * 0.8\n",
    survives="the label size (OverlayGridPainter._grid_label_pixel_size and _GRID_LABEL_MAX_LOGICAL_PX in overlay_paint/grid.py) "
             "is style, not a rule: GP-4 measures the drawn size and checks "
             "only the position and the digit")

# --- GP-5: the label shadow follows the shadow setting ------------------------

add("gp-5-grid-label-shadow-never-drawn", _PG,
    "        if self._host.badge_shadow:\n"
    "            shadow = QPainterPath(path)\n"
    "            shadow_offset = _SHADOW_OFFSET_PX * dpr\n"
    "            shadow.translate(shadow_offset, shadow_offset)\n"
    "            painter.fillPath(shadow, _SHADOW_COLOR)\n"
    "        outline = QPen(_OUTLINE_COLOR)\n"
    "        outline.setWidthF(\n"
    "            max(\n"
    "                _GRID_LABEL_OUTLINE_MIN_LOGICAL_PX * dpr,\n",
    "        if False:\n"
    "            shadow = QPainterPath(path)\n"
    "            shadow_offset = _SHADOW_OFFSET_PX * dpr\n"
    "            shadow.translate(shadow_offset, shadow_offset)\n"
    "            painter.fillPath(shadow, _SHADOW_COLOR)\n"
    "        outline = QPen(_OUTLINE_COLOR)\n"
    "        outline.setWidthF(\n"
    "            max(\n"
    "                _GRID_LABEL_OUTLINE_MIN_LOGICAL_PX * dpr,\n",
    GP_5)

# --- GP-6 to GP-9: the pin ------------------------------------------------------

add("gp-6-pin-region-one-pixel-short", _PG,
    "                2 * margin + 1,\n"
    "                2 * margin + 1,\n",
    "                2 * margin,\n"
    "                2 * margin,\n",
    GP_6)
add("gp-6-pin-margin-ignores-the-scale", _PG,
    "            math.ceil((reach + _GRID_LINE_OUTER_LOGICAL_PX) * scale)\n",
    "            math.ceil(reach + _GRID_LINE_OUTER_LOGICAL_PX)\n",
    GP_6)
add("gp-6-pin-without-crosshair", _PG,
    "            painter.drawLines(lines)\n"
    "            painter.drawEllipse(ring)\n",
    "            painter.drawEllipse(ring)\n",
    GP_6)
add("gp-7-pin-region-replaces-the-grid-region", _PG,
    "                    region if existing is None else existing.united(region)\n",
    "                    region\n",
    GP_7)
add("gp-7-pin-not-drawn-on-the-grid-window", _PG,
    "            if pin_monitor is not None and pin_monitor.hmonitor == hmonitor:\n",
    "            if False:\n",
    GP_6, GP_7)
add("gp-8-pin-follows-the-grid-monitor", _PG,
    "        pin_monitor = self._pin_monitor(monitors)\n"
    "\n"
    "        # The drawn region per monitor",
    "        pin_monitor = grid_monitor if self._grid_pin is not None else None\n"
    "\n"
    "        # The drawn region per monitor",
    GP_6, GP_8)
add("gp-8-pin-on-any-monitor", _PG,
    "            if monitor.rect_phys.contains(int(pin.x), int(pin.y)):\n",
    "            if True:\n",
    GP_8)
add("gp-9-pin-off-every-monitor-uses-the-first", _PG,
    "            if monitor.rect_phys.contains(int(pin.x), int(pin.y)):\n"
    "                return monitor\n"
    "        return None\n",
    "            if monitor.rect_phys.contains(int(pin.x), int(pin.y)):\n"
    "                return monitor\n"
    "        return monitors[0] if monitors else None\n",
    GP_9)

# --- GP-10 / GP-11: the monitor of the grid -------------------------------------

add("gp-10-no-overlap-fallback", _PG,
    "        best = None\n"
    "        best_area = 0\n",
    "        return None\n"
    "        best = None\n"
    "        best_area = 0\n",
    GP_10)
add("gp-10-exact-match-skipped", _PG,
    "        for monitor in monitors:\n"
    "            if monitor.rect_phys == wanted:\n"
    "                return monitor\n",
    "        pass\n",
    survives="OverlayGridPainter._grid_monitor (overlay_paint/grid.py): on a desktop whose monitors do not overlap, the "
             "exact rectangle match is also the monitor of largest overlap, "
             "so the overlap loop returns the same monitor (row GP-10b is "
             "NOT_REQUIRED for this reason)")
add("gp-11-gone-monitor-falls-back-to-the-first", _PG,
    "        return best\n"
    "\n"
    "    def _pin_monitor",
    "        return best if best is not None else monitors[0]\n"
    "\n"
    "    def _pin_monitor",
    GP_11)
add("gp-11-window-of-an-absent-monitor-kept", _PG,
    "        for hmonitor in list(self._host.windows.keys()):\n"
    "            if hmonitor not in regions:\n"
    "                window = self._host.windows.pop(hmonitor)\n"
    "                if not window.destroy():\n"
    "                    self._host.windows[hmonitor] = window\n"
    "\n"
    "        if not regions:\n",
    "        if not regions:\n",
    GP_11)

# --- GP-12: a changed rectangle rebuilds the window; the same one reuses it ---

add("gp-12-changed-geometry-reuses-the-window", _PG,
    "            if existing is None or existing.geom_phys != geom:\n"
    "                if existing is not None:\n"
    "                    old = self._host.windows.pop(hmonitor)\n",
    "            if existing is None:\n"
    "                if existing is not None:\n"
    "                    old = self._host.windows.pop(hmonitor)\n",
    GP_12)
add("gp-12-every-render-rebuilds-the-window", _PG,
    "            if existing is None or existing.geom_phys != geom:\n"
    "                if existing is not None:\n"
    "                    old = self._host.windows.pop(hmonitor)\n",
    "            if True:\n"
    "                if existing is not None:\n"
    "                    old = self._host.windows.pop(hmonitor)\n",
    GP_12)

# --- GP-13 / GP-14: a failure leaves no grid window ------------------------------

add("gp-13-failed-composite-keeps-the-window", _PG,
    "                if window.destroy():\n"
    "                    self._host.windows.pop(hmonitor, None)\n"
    "        return all_ok\n"
    "\n"
    "    def _grid_monitor",
    "                pass\n"
    "        return all_ok\n"
    "\n"
    "    def _grid_monitor",
    GP_13)
add("gp-13-failed-composite-reports-true", _PG,
    "                # visible, which would point the user at the wrong cells.\n"
    "                all_ok = False\n",
    "                # visible, which would point the user at the wrong cells.\n"
    "                all_ok = True\n",
    GP_13)
add("gp-14-render-error-keeps-the-windows", _PG,
    "                \"overlay_paint_window: grid paint failed\", exc_info=True\n"
    "            )\n"
    "            self._teardown.destroy_all()\n"
    "            return False\n",
    "                \"overlay_paint_window: grid paint failed\", exc_info=True\n"
    "            )\n"
    "            return False\n",
    GP_14)

# --- GP-15 / GP-16: clear_grid ---------------------------------------------------

_CLEAR_GRID = (
    "        self._grid_event = None\n"
    "        self._grid_pin = None\n"
    "        self._teardown.destroy_all()\n"
)
add("gp-15-clear-grid-destroys-nothing", _PG,
    _CLEAR_GRID,
    "        self._grid_event = None\n"
    "        self._grid_pin = None\n",
    GP_15)
add("gp-15-clear-grid-keeps-the-grid", _PG,
    _CLEAR_GRID,
    "        self._grid_pin = None\n"
    "        self._teardown.destroy_all()\n",
    GP_15)
add("gp-15-clear-grid-keeps-the-pin", _PG,
    _CLEAR_GRID,
    "        self._grid_event = None\n"
    "        self._teardown.destroy_all()\n",
    GP_15)
add("gp-16-grid-render-skips-the-cohort", _PG,
    "        if self._host.pending_destroy:\n"
    "            self._host.pending_destroy = [\n"
    "                window\n"
    "                for window in self._host.pending_destroy\n"
    "                if not window.destroy()\n"
    "            ]\n"
    "\n"
    "        monitors = _enumerate_native_monitors()\n"
    "        grid_monitor = self._grid_monitor(monitors)\n",
    "        monitors = _enumerate_native_monitors()\n"
    "        grid_monitor = self._grid_monitor(monitors)\n",
    GP_16)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
