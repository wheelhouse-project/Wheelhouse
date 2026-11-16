"""Mutation evidence for the L5 requirement tests (wh-full-refactor.6.1).

Area L5: the mouse grid and pointer action methods. They were methods of
LogicController (main.py); since wh-full-refactor.6.2 they are methods of
MouseGridController (mouse_grid/controller.py), and the mutations of that
code name that file. Each mutation names its row MG-n from the L5 notes of stage A of
place 3, and the boundary tests of tests/test_mouse_grid_requirements_l5.py
that must catch it. Where GridOverlayStateMachine (grid_overlay_state.py)
decides a rule, the mutation is in that module; the test still enters at the
L5 boundary.

Same form and the same shared runner as
tests/mutation_gate_overlay_stack_s3.py (read the docstring of
tests/mutation_gate_overlay_requirements.py for the disciplines the runner
owns). A mutation marked as an expected survivor gives its reason as
survives=; the sweep fails when any test of its selection fails under it,
because the reason is then no longer true.

Run from services/wheelhouse:
  uv run python tests/mutation_gate_mouse_grid_l5.py --check
  uv run python tests/mutation_gate_mouse_grid_l5.py
  uv run python tests/mutation_gate_mouse_grid_l5.py mg-1-focused-monitor-ignored
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

_T = "test_mouse_grid_requirements_l5.py"
_M = "main.py"
_G = "grid_overlay_state.py"
# wh-full-refactor.6.2: the L5 methods moved to MouseGridController.
_MG = "mouse_grid/controller.py"


def sel(*names):
    """The pytest node ids of the named tests."""
    return tuple(f"tests/{_T}::{name}" for name in names)


def add(name, file, old, new, *expect, suite, survives=None):
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


MG_1 = "test_mg_1_show_grid_paints_the_focused_windows_monitor"
MG_2 = "test_mg_2_show_grid_hides_the_numbers_before_it_paints"
MG_3 = "test_mg_3_unknown_focused_monitor_opens_on_the_primary"
MG_4 = "test_mg_4_no_usable_monitor_gives_a_notice_and_no_grid"
MG_5 = "test_mg_5_disabled_grid_gives_one_notice_per_session"
MG_6 = "test_mg_6_by_name_click_and_show_grid_share_the_one_notice"
MG_7 = "test_mg_7_disabled_grid_hide_and_next_screen_do_nothing"
MG_8 = "test_mg_8_hide_grid_sends_clear_grid"
MG_9 = "test_mg_9_next_screen_with_the_grid_closed_does_nothing"
MG_10 = "test_mg_10_next_screen_paints_the_next_monitor"
MG_10B = "test_mg_10b_next_screen_from_a_monitor_that_is_gone_goes_to_the_first"
MG_11 = "test_mg_11_a_restart_repaints_the_mark"
MG_12 = "test_mg_12_a_failed_restart_leaves_the_open_grid_as_it_was"
MG_13 = "test_mg_13_grid_words_with_the_grid_closed_are_typed"
MG_14 = "test_mg_14_a_number_paints_its_cell"
MG_15 = "test_mg_15_a_number_at_the_minimum_cell_does_nothing"
MG_16 = "test_mg_16_mark_pins_the_center_and_starts_the_grid_over"
MG_17 = "test_mg_17_drag_without_a_mark_gives_a_notice"
MG_18 = "test_mg_18_drag_with_a_mark_clears_and_drags"
MG_19 = "test_mg_19_a_closed_grid_forgets_its_mark"
MG_20 = "test_mg_20_click_clears_the_grid_and_clicks_the_center"
MG_21 = "test_mg_21_click_and_a_number_clicks_the_cell_without_painting_it"
MG_22 = "test_mg_22_click_and_a_number_at_the_minimum_cell_clicks_the_center"
MG_23 = "test_mg_23_move_here_clears_the_grid_and_moves_the_pointer"
MG_24 = "test_mg_24_a_bare_click_word_clicks_at_the_pointer"
MG_25 = "test_mg_25_a_bare_click_word_is_typed_when_clicking_is_off"
MG_26 = "test_mg_26_a_bad_grid_key_keeps_the_click_at_the_pointer"
MG_27 = "test_mg_27_an_ok_reply_gives_no_notice"
MG_28 = "test_mg_28_a_failed_reply_gives_the_actions_notice"
MG_29 = "test_mg_29_release_failed_names_the_button"
MG_30 = "test_mg_30_disabled_by_config_reply_gives_that_notice"
MG_31 = "test_mg_31_a_malformed_reply_gives_the_actions_notice"
MG_32 = "test_mg_32_an_error_reply_gives_the_notice_and_the_next_action_probes"
MG_33 = "test_mg_33_no_reply_in_time_sends_a_channel_probe"
MG_33B = "test_mg_33b_a_drag_waits_its_duration_longer"
MG_34 = "test_mg_34_the_probe_reply_decides_the_timeout_notice"
MG_35 = "test_mg_35_an_unanswered_probe_gives_the_failure_notice"
MG_36 = "test_mg_36_the_next_action_on_a_suspect_channel_probes_first"
MG_37 = "test_mg_37_show_grid_on_a_suspect_channel_probes_first"
MG_38 = "test_mg_38_show_grid_waits_for_a_pending_pointer_action"
MG_39 = "test_mg_39_one_pointer_action_at_a_time"

# --- Opening the grid -------------------------------------------------------

# MG-1: the focused window's monitor is used.
add("mg-1-focused-monitor-ignored", _MG,
    "                        focused = _to_rect(target)\n",
    "                        focused = None\n",
    MG_1, suite=sel(MG_1, MG_3))

# MG-2: the numbered overlay is hidden before the grid paints.
add("mg-2-numbers-not-hidden", _MG,
    "                    overlay_batch = await self._host.overlay.handle_overlay_command(\n"
    "                        \"hide\", trace_id,\n"
    "                    )\n",
    "                    overlay_batch = None\n",
    MG_2, suite=sel(MG_2, MG_1))

# MG-3: no focused monitor -> the monitor at the origin, else the first.
add("mg-3-origin-test-skipped", _G,
    "        if monitor.left == 0 and monitor.top == 0:\n",
    "        if False:\n",
    MG_3, suite=sel(MG_3, MG_1))
add("mg-3-last-monitor-instead-of-first", _G,
    "    if monitors:\n"
    "        return monitors[0]\n"
    "    return None\n",
    "    if monitors:\n"
    "        return monitors[-1]\n"
    "    return None\n",
    MG_3, suite=sel(MG_3, MG_1))

# MG-4: no usable monitor -> grid_no_monitor notice, no grid.
add("mg-4-dpi-refusal-ignored", _MG,
    "            if not dpi_ok:\n",
    "            if False:\n",
    MG_4, suite=sel(MG_4, MG_1))
add("mg-4-no-monitor-notice-renamed", _MG,
    "                        reason=\"grid_no_monitor\",\n",
    "                        reason=\"grid_pointer_pending\",\n",
    MG_4, MG_12, suite=sel(MG_4, MG_12))

# MG-5: a disabled grid gives one disabled_by_config notice per session.
add("mg-5-gate-reads-enabled-only", _MG,
    "            if not self._host.click_config.grid_enabled_effective:\n",
    "            if not self._host.click_config.enabled:\n",
    MG_5, suite=sel(MG_5, MG_7, MG_1))
add("mg-5-notice-every-time", _MG,
    "                if command == \"open\" and self._host.take_click_disabled_notice():\n",
    "                if command == \"open\":\n",
    MG_5, MG_6, suite=sel(MG_5, MG_6, MG_7))

# MG-6: the grid and the by-name click share the one-shot flag.
add("mg-6-grid-reads-its-own-flag", _MG,
    "                if command == \"open\" and self._host.take_click_disabled_notice():\n",
    "                if command == \"open\" and not getattr(self, \"grid_notice_shown_x\", False) and not setattr(self, \"grid_notice_shown_x\", True):\n",
    MG_6, suite=sel(MG_6, MG_5))

# MG-7: a disabled grid ignores hide grid and grid next screen.
add("mg-7-notice-for-every-grid-command", _MG,
    "                if command == \"open\" and self._host.take_click_disabled_notice():\n",
    "                if self._host.take_click_disabled_notice():\n",
    MG_7, suite=sel(MG_7, MG_5))

# --- Closing and moving the grid ------------------------------------------

# MG-8: hide grid sends clear_grid, open or closed.
add("mg-8-dismiss-effects-dropped", _MG,
    "                result = machine.apply(GridEvent(GridEventKind.DISMISS))\n"
    "                await self._perform_grid_effects(\n",
    "                result = machine.apply(GridEvent(GridEventKind.DISMISS))\n"
    "                if False: await self._perform_grid_effects(\n",
    MG_8, suite=sel(MG_8))
add("mg-8-closed-dismiss-sends-nothing", _G,
    "        if self.state is GridState.CLOSED:\n"
    "            return GridApplyResult(GridOutcome.ACCEPTED, (self._clear_grid(),))\n",
    "        if self.state is GridState.CLOSED:\n"
    "            return GridApplyResult(GridOutcome.ACCEPTED, ())\n",
    MG_8, suite=sel(MG_8))

# MG-9: grid next screen with the grid closed does nothing. Without the closed
# check, the command reaches the pointer-channel fence. After a pointer action
# timed out, the channel is suspect, so the fence sends channel_probe, and an
# unanswered probe shows the notice grid_pointer_pending. The shipped code
# sends nothing. MG_9B proves that state; MG_9 (a fresh session) stays in the
# selection.
MG_9B = ("test_mg_9_next_screen_with_the_grid_closed_does_not_probe_a_"
         "suspect_channel")
add("mg-9-closed-check-skipped", _MG,
    "            if command == \"next_screen\" and not machine.is_open:\n",
    "            if False:\n",
    MG_9B, suite=sel(MG_9, MG_9B))

# MG-10: next screen paints the next monitor and wraps.
add("mg-10-next-screen-reopens-instead", _MG,
    "                        GridEventKind.NEXT_MONITOR, monitors=monitors,\n",
    "                        GridEventKind.OPEN_GRID, monitors=monitors,\n",
    MG_10, suite=sel(MG_10, MG_10B))
add("mg-10-no-wrap", _G,
    "                return monitors[(index + 1) % len(monitors)]\n",
    "                return monitors[min(index + 1, len(monitors) - 1)]\n",
    MG_10, suite=sel(MG_10, MG_10B))
# MG-10b: a monitor that is gone restarts on the first monitor.
add("mg-10b-gone-monitor-goes-to-the-last", _G,
    "                return monitors[(index + 1) % len(monitors)]\n"
    "    return monitors[0]\n",
    "                return monitors[(index + 1) % len(monitors)]\n"
    "    return monitors[-1]\n",
    MG_10B, suite=sel(MG_10B, MG_10))

# MG-11: a restart repaints the mark.
add("mg-11-open-forgets-the-pin", _G,
    "        effects = [self._close_numbered_overlay(), self._paint_grid()]\n"
    "        if self.mark is not None:\n",
    "        effects = [self._close_numbered_overlay(), self._paint_grid()]\n"
    "        if False:\n",
    MG_11, suite=sel(MG_11))
add("mg-11-next-screen-forgets-the-pin", _G,
    "        effects = [self._paint_grid()]\n"
    "        if self.mark is not None:\n",
    "        effects = [self._paint_grid()]\n"
    "        if False:\n",
    MG_11, suite=sel(MG_11))

# MG-12: a failed restart leaves the open grid as it was.
add("mg-12-failed-reopen-closes", _G,
    "            # in particular, do NOT close a live grid on a failed reopen.\n"
    "            return GridApplyResult(GridOutcome.NO_MONITOR)\n",
    "            # in particular, do NOT close a live grid on a failed reopen.\n"
    "            self._enter_closed()\n"
    "            return GridApplyResult(GridOutcome.NO_MONITOR)\n",
    MG_12, suite=sel(MG_12, MG_4))
add("mg-12-failed-next-screen-closes", _G,
    "        if target is None:\n"
    "            return GridApplyResult(GridOutcome.NO_MONITOR)\n",
    "        if target is None:\n"
    "            self._enter_closed()\n"
    "            return GridApplyResult(GridOutcome.NO_MONITOR)\n",
    MG_12, suite=sel(MG_12, MG_4))

# --- Words with the grid open and closed ------------------------------------

# MG-13: grid-open-only words are typed when the grid is closed.
add("mg-13-closed-words-consumed", _MG,
    "        if not machine.is_open:\n"
    "            return False\n"
    "\n"
    "        if command == \"refine\":\n",
    "        if False:\n"
    "            return False\n"
    "\n"
    "        if command == \"refine\":\n",
    MG_13, suite=sel(MG_13))

# MG-14: a number paints its cell.
add("mg-14-paint-left-from-top", _MG,
    "                        left=rect.left,\n",
    "                        left=rect.top,\n",
    MG_14, suite=sel(MG_14))

# MG-15: a number at the minimum cell is consumed and changes nothing.
add("mg-15-min-cell-number-typed-by-logic", _MG,
    "            if result.outcome is GridOutcome.NO_OP:\n",
    "            if result.outcome is not GridOutcome.ACCEPTED:\n",
    MG_15, suite=sel(MG_15, MG_14))
add("mg-15-min-cell-is-no-op", _G,
    "            return GridApplyResult(GridOutcome.IGNORED_MIN_CELL)\n",
    "            return GridApplyResult(GridOutcome.NO_OP)\n",
    MG_15, suite=sel(MG_15, MG_14))

# MG-16: mark pins the center and starts the grid over.
add("mg-16-pin-axes-swapped", _MG,
    "                        PaintGridPinEvent(x=point.x, y=point.y).to_dict(),\n",
    "                        PaintGridPinEvent(x=point.y, y=point.x).to_dict(),\n",
    MG_16, MG_11, suite=sel(MG_16, MG_11))
add("mg-16-mark-at-the-monitor-center", _G,
    "        self.mark = cell_center(self.rect)\n",
    "        self.mark = cell_center(self.monitor)\n",
    MG_16, MG_18, suite=sel(MG_16, MG_18))

# MG-17: drag without a mark gives the drag_without_mark notice.
add("mg-17-notice-reason-replaced", _MG,
    "                        reason=effect.notice_reason,\n",
    "                        reason=\"grid_click_failed\",\n",
    MG_17, MG_19, suite=sel(MG_17, MG_19))

# MG-18: drag with a mark clears the grid and drags with the config duration.
add("mg-18-duration-fixed", _MG,
    "                            \"duration_ms\": effect.duration_ms,\n",
    "                            \"duration_ms\": 250,\n",
    MG_18, suite=sel(MG_18))
add("mg-18-end-at-the-mark", _G,
    "            end_point=cell_center(self.rect),\n",
    "            end_point=self.mark,\n",
    MG_18, suite=sel(MG_18))

# MG-19: every close forgets the mark.
add("mg-19-close-keeps-the-mark", _G,
    "        self.rect = None\n"
    "        self.mark = None\n",
    "        self.rect = None\n",
    MG_19, suite=sel(MG_19))

# MG-20: click clears the grid and clicks the center with the gesture map.
add("mg-20-triple-is-single", _MG,
    "            return (\"left\", 3)\n",
    "            return (\"left\", 1)\n",
    MG_20, MG_24, suite=sel(MG_20, MG_24))
add("mg-20-double-is-single", _MG,
    "            return (\"left\", 2)\n",
    "            return (\"left\", 1)\n",
    MG_20, MG_24, suite=sel(MG_20, MG_24))
add("mg-20-right-is-left", _MG,
    "            return (\"right\", 1)\n",
    "            return (\"left\", 1)\n",
    MG_20, MG_24, MG_29, suite=sel(MG_20, MG_24, MG_29))
add("mg-20-click-leaves-the-grid-painted", _MG,
    "            result = machine.apply(GridEvent(GridEventKind.ACTION_COMPLETED))\n"
    "            await self._perform_grid_effects(\n",
    "            result = machine.apply(GridEvent(GridEventKind.ACTION_COMPLETED))\n"
    "            if False: await self._perform_grid_effects(\n",
    MG_20, MG_23, suite=sel(MG_20, MG_23))

# MG-21: click N clicks the refined cell center without painting it.
add("mg-21-click-number-ignored", _MG,
    "            if command == \"click\" and number:\n",
    "            if False:\n",
    MG_21, suite=sel(MG_21, MG_22))

# MG-22: click N at the minimum cell still clicks the unchanged center.
add("mg-22-min-cell-click-refused", _MG,
    "                refine_result = machine.apply(\n"
    "                    GridEvent(GridEventKind.REFINE, number=number)\n"
    "                )\n",
    "                refine_result = machine.apply(\n"
    "                    GridEvent(GridEventKind.REFINE, number=number)\n"
    "                )\n"
    "                if refine_result.outcome is GridOutcome.IGNORED_MIN_CELL:\n"
    "                    return True\n",
    MG_22, suite=sel(MG_22, MG_21))

# MG-23: move here clears the grid and moves the pointer to the center.
add("mg-23-move-axes-swapped", _MG,
    "                    {\"x\": center.x, \"y\": center.y},\n",
    "                    {\"x\": center.y, \"y\": center.x},\n",
    MG_23, suite=sel(MG_23))

# --- The click at the pointer -----------------------------------------------

# MG-24: a bare click word with the grid closed clicks at the pointer.
add("mg-24-pointer-button-fixed", _MG,
    "            {\"button\": button, \"click_count\": click_count},\n"
    "            trace_id=trace_id, spoken=spoken,\n"
    "            failure_reason=\"pointer_click_failed\",\n",
    "            {\"button\": \"left\", \"click_count\": click_count},\n"
    "            trace_id=trace_id, spoken=spoken,\n"
    "            failure_reason=\"pointer_click_failed\",\n",
    MG_24, MG_29, suite=sel(MG_24, MG_29))

# MG-25: with clicking off, the click word is typed.
add("mg-25-pointer-click-ignores-the-switch", _MG,
    "        if not self._host.click_config.enabled:\n"
    "            logger.info(\n"
    "                \"pointer click: voice clicking disabled by config; the \"\n",
    "        if False:\n"
    "            logger.info(\n"
    "                \"pointer click: voice clicking disabled by config; the \"\n",
    MG_25, suite=sel(MG_25, MG_24))

# MG-26: a bad grid key does not stop the click at the pointer.
add("mg-26-pointer-click-reads-the-grid-gate", _MG,
    "        if not self._host.click_config.enabled:\n"
    "            logger.info(\n"
    "                \"pointer click: voice clicking disabled by config; the \"\n",
    "        if not self._host.click_config.grid_enabled_effective:\n"
    "            logger.info(\n"
    "                \"pointer click: voice clicking disabled by config; the \"\n",
    MG_26, suite=sel(MG_26, MG_24))

# --- Replies of the Input process -------------------------------------------

# MG-27: an ok reply gives no notice.
add("mg-27-ok-not-recognized", _MG,
    "        if response.outcome == \"ok\":\n",
    "        if response.outcome == \"okay\":\n",
    MG_27, suite=sel(MG_27))

# MG-28: a failed reply gives the notice of its action.
add("mg-28-move-reports-click", _MG,
    "                    failure_reason=\"grid_move_failed\",\n",
    "                    failure_reason=\"grid_click_failed\",\n",
    MG_28, suite=sel(MG_28))
add("mg-28-drag-reports-click", _MG,
    "                        failure_reason=\"grid_drag_failed\",\n",
    "                        failure_reason=\"grid_click_failed\",\n",
    MG_28, MG_31, suite=sel(MG_28, MG_31))
add("mg-28-pointer-reports-click", _MG,
    "            failure_reason=\"pointer_click_failed\",\n",
    "            failure_reason=\"grid_click_failed\",\n",
    MG_28, suite=sel(MG_28))
add("mg-28-failed-reply-gives-no-notice", _MG,
    "            _notice(\"disabled_by_config\")\n"
    "            return\n"
    "        _notice(failure_reason)\n",
    "            _notice(\"disabled_by_config\")\n"
    "            return\n",
    MG_28, suite=sel(MG_28))

# MG-29: release_failed names the button that may still be held.
add("mg-29-release-failed-not-recognized", _MG,
    "        if reason == \"release_failed\":\n",
    "        if reason == \"release_failed_x\":\n",
    MG_29, suite=sel(MG_29))
add("mg-29-reply-button-test-inverted", _MG,
    "            # carry the button identity; no wire schema change needed.\n"
    "            if request.get(\"button\") == \"right\":\n",
    "            # carry the button identity; no wire schema change needed.\n"
    "            if request.get(\"button\") != \"right\":\n",
    MG_29, suite=sel(MG_29))

# MG-30: a disabled_by_config reply gives that notice.
add("mg-30-disabled-reply-generic", _MG,
    "            _notice(\"disabled_by_config\")\n",
    "            _notice(failure_reason)\n",
    MG_30, suite=sel(MG_30))

# MG-31: a malformed reply gives the notice of its action.
add("mg-31-malformed-reply-silent", _MG,
    "                action, trace_id, exc,\n"
    "            )\n"
    "            _notice(failure_reason)\n"
    "            return\n"
    "\n"
    "        if response.outcome == \"ok\":\n",
    "                action, trace_id, exc,\n"
    "            )\n"
    "            return\n"
    "\n"
    "        if response.outcome == \"ok\":\n",
    MG_31, suite=sel(MG_31))

# MG-32: an Input error reply gives the notice and leaves the channel suspect.
add("mg-32-error-reply-leaves-channel-trusted", _MG,
    "                except BaseException:\n"
    "                    self._grid_mouse_channel_suspect = True\n"
    "                    raise\n",
    "                except BaseException:\n"
    "                    raise\n",
    MG_32, suite=sel(MG_32))
add("mg-32-error-reply-silent", _MG,
    "                action, trace_id, exc, exc_info=True,\n"
    "            )\n"
    "            _notice(failure_reason)\n",
    "                action, trace_id, exc, exc_info=True,\n"
    "            )\n",
    MG_32, suite=sel(MG_32))

# --- No reply: the timeout and the channel probe ---------------------------

# MG-33: the wait is response_timeout_ms, plus the duration for a drag.
add("mg-33-timeout-doubled", _MG,
    "        timeout_s = max(timeout_ms / 1000.0, 0.1)\n"
    "        duration_ms = params.get(\"duration_ms\")\n",
    "        timeout_s = max(timeout_ms / 500.0, 0.1)\n"
    "        duration_ms = params.get(\"duration_ms\")\n",
    MG_33, suite=sel(MG_33))
add("mg-33-drag-duration-not-added", _MG,
    "            timeout_s += max(duration_ms, 0) / 1000.0\n",
    "            timeout_s += 0\n",
    MG_33B, suite=sel(MG_33B))
add("mg-33-no-probe-after-timeout", _MG,
    "                    reply = await self._confirm_grid_mouse_channel(\n"
    "                        timeout_s=timeout_s, trace_id=trace_id,\n"
    "                    )\n"
    "                    late = (\n",
    "                    reply = None\n"
    "                    late = (\n",
    MG_33, MG_34, suite=sel(MG_33, MG_34))

# MG-34: the probe's record of the last action decides the notice.
add("mg-34-late-success-not-recognized", _MG,
    "                        if late.get(\"succeeded\") is True:\n",
    "                        if late.get(\"succeeded\") is None:\n",
    MG_34, suite=sel(MG_34))
add("mg-34-trace-not-compared", _MG,
    "                        and late.get(\"trace_id\") == trace_id\n",
    "                        and True\n",
    MG_34, suite=sel(MG_34))
add("mg-34-late-release-not-recognized", _MG,
    "                        if late.get(\"reason\") == \"release_failed\":\n",
    "                        if late.get(\"reason\") == \"release_failed_x\":\n",
    MG_34, suite=sel(MG_34))
add("mg-34-late-button-test-inverted", _MG,
    "                        if late.get(\"reason\") == \"release_failed\":\n"
    "                            if request.get(\"button\") == \"right\":\n",
    "                        if late.get(\"reason\") == \"release_failed\":\n"
    "                            if request.get(\"button\") != \"right\":\n",
    MG_34, suite=sel(MG_34))

# MG-35: a timeout whose probe is not answered gives the failure notice.
add("mg-35-timeout-notice-dropped", _MG,
    "                    _notice(failure_reason)\n"
    "                    return\n"
    "                except BaseException:\n",
    "                    return\n"
    "                except BaseException:\n",
    MG_35, MG_34, suite=sel(MG_35, MG_34))

# MG-36: the next action on a suspect channel probes first.
add("mg-36-suspect-channel-not-probed", _MG,
    "                if self._grid_mouse_channel_suspect:\n"
    "                    if await self._confirm_grid_mouse_channel(\n",
    "                if False:\n"
    "                    if await self._confirm_grid_mouse_channel(\n",
    MG_36, suite=sel(MG_36))
add("mg-36-failed-probe-lets-the-action-go", _MG,
    "                    ) is None:\n"
    "                        logger.warning(\n"
    "                            \"grid: %s withheld, channel unconfirmed after an \"\n",
    "                    ) is None and False:\n"
    "                        logger.warning(\n"
    "                            \"grid: %s withheld, channel unconfirmed after an \"\n",
    MG_36, suite=sel(MG_36))
add("mg-36-answered-probe-keeps-suspicion", _MG,
    "        self._grid_mouse_channel_suspect = False\n"
    "        return reply if isinstance(reply, dict) else {}\n",
    "        return reply if isinstance(reply, dict) else {}\n",
    MG_36, suite=sel(MG_36))

# MG-37: show grid on a suspect channel probes first; unanswered -> notice.
add("mg-37-fence-skips-the-probe", _MG,
    "            if not self._grid_mouse_channel_suspect:\n"
    "                return True\n",
    "            if True:\n"
    "                return True\n",
    MG_37, suite=sel(MG_37))
add("mg-37-pending-notice-renamed", _MG,
    "                        reason=\"grid_pointer_pending\",\n",
    "                        reason=\"grid_no_monitor\",\n",
    MG_37, suite=sel(MG_37))

# MG-38: show grid waits for a pending pointer action.
add("mg-38-fence-waits-for-nothing", _MG,
    "        while pending:\n"
    "            await asyncio.wait(set(pending))\n"
    "        lock = getattr(self, \"_grid_mouse_action_lock\", None)\n"
    "        if lock is None:\n"
    "            lock = asyncio.Lock()\n"
    "            self._grid_mouse_action_lock = lock\n"
    "        async with lock:\n",
    "        while False:\n"
    "            await asyncio.wait(set(pending))\n"
    "        lock = getattr(self, \"_grid_mouse_action_lock\", None)\n"
    "        if lock is None:\n"
    "            lock = asyncio.Lock()\n"
    "            self._grid_mouse_action_lock = lock\n"
    "        if True:\n",
    MG_38, suite=sel(MG_38))
add("mg-38-fence-skips-the-pending-set", _MG,
    "        while pending:\n"
    "            await asyncio.wait(set(pending))\n",
    "        while False:\n"
    "            await asyncio.wait(set(pending))\n",
    suite=sel(MG_38),
    survives="the dispatched task holds the pointer lock before the next "
             "utterance arrives, so the fence's lock alone holds the open; "
             "the pending set covers only a task not yet at the lock, which "
             "needs a loop-turn timing window")

# MG-39: one pointer action at a time.
add("mg-39-no-pointer-lock", _MG,
    "            async with lock:\n"
    "                # wh-mouse-grid.1.18: a timed-out (or failed) send leaves the\n",
    "            if True:\n"
    "                # wh-mouse-grid.1.18: a timed-out (or failed) send leaves the\n",
    MG_39, suite=sel(MG_39))


# --- Defaults of the LogicController delegators (wh-full-refactor.6.3) -----
# Item 6 of that bead: each default value of a parameter of the two
# delegators in main.py exists in one place only, and the delegator keeps it
# (MouseGridController.handle_grid_command and handle_pointer_click have no
# defaults). Only the number default is read by a test that calls the
# delegator without it: the speech action and the numbered overlay pass
# every other parameter, and tests/test_grid_session_fence.py does not read
# what the gesture and spoken defaults change.
_R = "test_grid_speech_routing.py"
_R1 = "test_bare_click_acts_at_current_center"
_R2 = "test_triple_click_with_the_grid_open_clicks_the_center_three_times"

# MG-D1: handle_grid_command(number=) defaults to 0 (no cell chosen).
add("mg-d1-grid-command-number-default-is-a-cell", _M,
    "        self, command: str, trace_id: str, *,\n"
    "        number: int = 0,\n",
    "        self, command: str, trace_id: str, *,\n"
    "        number: int = 5,\n",
    _R1, _R2, suite=(f"tests/{_R}::{_R1}", f"tests/{_R}::{_R2}"))

# MG-D2 to MG-D4: the gesture and spoken defaults of the two delegators.
# Every production caller passes both arguments (speech/actions.py
# _handle_grid_word and _click_at_pointer, numbered_overlay/controller.py
# click_element), so no caller reads these defaults.
_UNREAD = ("every production caller passes this argument (speech/actions.py, "
           "numbered_overlay/controller.py), so the shipped app never reads "
           "the default")
add("mg-d2-grid-command-gesture-default-is-double-click", _M,
    "        number: int = 0,\n"
    "        gesture=None,\n",
    "        number: int = 0,\n"
    "        gesture=\"double_click\",\n",
    suite=(f"tests/{_T}", f"tests/{_R}"), survives=_UNREAD)
add("mg-d3-grid-command-spoken-default-is-text", _M,
    "        gesture=None,\n"
    "        spoken: str = \"\",\n"
    "    ) -> bool:\n"
    "        \"\"\"Apply one grid command;",
    "        gesture=None,\n"
    "        spoken: str = \"typed words\",\n"
    "    ) -> bool:\n"
    "        \"\"\"Apply one grid command;",
    suite=(f"tests/{_T}", f"tests/{_R}"), survives=_UNREAD)
add("mg-d4-pointer-click-spoken-default-is-text", _M,
    "        self, gesture, trace_id: str, *, spoken: str = \"\",\n",
    "        self, gesture, trace_id: str, *, spoken: str = \"typed words\",\n",
    suite=(f"tests/{_T}", f"tests/{_R}"), survives=_UNREAD)

# MG-47: Logic reads the monitors and the focused window inside the
# per-monitor DPI context (wh-full-refactor.6.3, Result 2 of the old test
# test_reads_happen_inside_the_established_context).
MG_47 = "test_mg_47_show_grid_on_a_scaled_desktop_paints_physical_pixels"
add("mg-47-reads-outside-the-dpi-context", _MG,
    "        with monitor_geometry.per_monitor_dpi_context() as dpi_ok:\n",
    "        with __import__(\"contextlib\").nullcontext(True) as dpi_ok:\n",
    MG_47, suite=sel(MG_47))


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
