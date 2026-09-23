"""Mutation gate for the off-screen floating-button guard tests.

Proves tests/test_floating_button_offscreen_recovery.py and the
TestCorrectOntoAnyScreen part of tests/test_floating_button_geometry.py fail
for the right reason when the protected behaviour breaks
(wh-floating-button-offscreen). Run from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_floating_button_offscreen.py

Each mutation: read bytes, require exactly one pattern match (translated to
the file's own line endings), compile the mutant, write it atomically through
a same-directory temp file, clear __pycache__, run the two test files with
PYTHONDONTWRITEBYTECODE=1 and a timeout, restore bytes in a finally block.
Pattern-not-found, ambiguous patterns, non-compiling mutants, timeouts,
suite-timeout aborts, and collection errors are ERRORS, never verdicts.

Verdicts are read only from pytest's short-summary section, and only when the
exit code is nonzero: a green run prints no summary at all, and that is a
survivor rather than an unreadable run. COLUMNS=1000 is set because pytest
truncates a summary line to the terminal width, and a captured run has no
terminal, so the default 80 would cut node ids this file routinely exceeds.

pytest is started through the service venv rather than ``uv run``: uv
re-synchronises the environment before it runs anything, and a sync inside a
worktree has starved this host before.

Scope note for a reader: this gate covers the behaviour the guard tests
assert. It deliberately carries no mutation for the ``except (AttributeError,
RuntimeError)`` arms in _watch_screen and _watch_the_screen_layout, or for
measuring _distance_to_screen from the centre rather than the corner, because
no test distinguishes those; a gate entry for them would report a survivor
for code nothing claims to cover.
"""
# --check validates patterns and Python syntax without collecting tests,
# recovering pending mutations, or writing target files.
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
GUI = SERVICE / "gui.py"
GEOMETRY = SERVICE / "floating_button_geometry.py"
TEST_FILES = [
    "tests/test_floating_button_offscreen_recovery.py",
    "tests/test_floating_button_geometry.py",
]

_VENV_PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable))

MUTATIONS = [
    # --- the arithmetic: floating_button_geometry.correct_onto_any_screen ---
    {
        "name": "the-visible-fraction-becomes-nine-tenths",
        "file": GEOMETRY,
        "old": "MIN_VISIBLE_AREA_FRACTION = 0.5",
        "new": "MIN_VISIBLE_AREA_FRACTION = 0.9",
        "expect": [
            "test_the_fraction_is_a_half",
            "test_a_button_exactly_half_off_an_outer_edge_is_left_alone",
        ],
    },
    {
        # Exactly half visible must be KEPT. A strict comparison moves a
        # button a user deliberately parked on an edge.
        "name": "exactly-half-visible-is-not-enough",
        "file": GEOMETRY,
        "old": "    if visible >= size * size * MIN_VISIBLE_AREA_FRACTION:",
        "new": "    if visible > size * size * MIN_VISIBLE_AREA_FRACTION:",
        "expect": ["test_a_button_exactly_half_off_an_outer_edge_is_left_alone"],
    },
    {
        # The areas are ADDED. Comparing them one screen at a time moves a
        # button the user can see more than half of.
        #
        # The catcher is NOT the plain seam test. A 50 px button fully
        # covered by two monitors has 2500 px of area against a 1250 px
        # threshold, so one screen always holds at least half however it
        # splits, and sum() and max() agree. This gate found that gap: the
        # mutation ran green over the whole suite. Only a button that also
        # hangs off an outer edge separates the two rules.
        "name": "the-areas-are-compared-instead-of-added",
        "file": GEOMETRY,
        "old": "    visible = sum(_overlap_area(left, top, size, screen) for screen in distinct)",
        "new": "    visible = max(_overlap_area(left, top, size, screen) for screen in distinct)",
        "expect": [
            "test_a_button_split_across_a_seam_counts_both_screens_together",
        ],
    },
    {
        # Two screens reporting the same rectangle are one mirrored display.
        "name": "a-mirrored-display-counts-its-pixels-twice",
        "file": GEOMETRY,
        "old": (
            "    distinct = list(dict.fromkeys(\n"
            "        (screen[0], screen[1], screen[2], screen[3]) for screen in screens\n"
            "    ))"
        ),
        "new": (
            "    distinct = [\n"
            "        (screen[0], screen[1], screen[2], screen[3]) for screen in screens\n"
            "    ]"
        ),
        "expect": ["test_a_duplicated_display_does_not_count_the_same_pixels_twice"],
    },
    {
        # An empty screen list must leave the stored position alone. The
        # mutation returns a guess rather than deleting the branch, so the
        # test fails on its assertion instead of on min() over an empty
        # sequence, which would be a verdict earned by an unrelated crash.
        "name": "an-empty-screen-list-invents-the-origin",
        "file": GEOMETRY,
        "old": "    if not screens:\n        return left, top",
        "new": "    if not screens:\n        return 0, 0",
        "expect": ["test_no_screens_at_all_leaves_the_position_untouched"],
    },
    {
        "name": "the-first-screen-wins-instead-of-the-nearest",
        "file": GEOMETRY,
        "old": "    nearest = min(distinct, key=lambda screen: _distance_to_screen(left, top, size, screen))",
        "new": "    nearest = distinct[0]",
        "expect": [
            "test_a_lost_position_returns_to_the_nearest_screen_not_the_first_one",
        ],
    },
    {
        # Without the guard a screen the button misses entirely contributes
        # the product of two negative overlaps, so a lost button reads as
        # fully visible.
        "name": "a-missed-screen-contributes-a-positive-area",
        "file": GEOMETRY,
        "old": "    if width <= 0 or height <= 0:\n        return 0",
        "new": "    if False:\n        return 0",
        "expect": [
            "test_a_button_on_no_screen_at_all_comes_back",
            "test_a_button_of_any_supported_size_ends_up_fully_on_a_screen",
        ],
    },
    # --- the connection: gui.GuiManager._apply_geometry ---
    {
        "name": "the-stored-position-is-applied-uncorrected",
        "file": GUI,
        "old": "        corrected = correct_onto_any_screen(pos[0], pos[1], size, screens)",
        "new": "        corrected = (pos[0], pos[1])",
        "expect": [
            "test_a_position_on_no_screen_is_corrected_before_the_button_moves",
            "test_the_rollback_still_corrects_the_button",
            "test_a_layout_change_re_applies_the_stored_geometry",
        ],
    },
    {
        # The rollback runs BECAUSE the queue refused a write. Saving there
        # starts a loop that never ends.
        "name": "the-rollback-saves-its-correction-too",
        "file": GUI,
        "old": "        if save_correction and list(corrected) != list(pos):",
        "new": "        if list(corrected) != list(pos):",
        "expect": [
            "test_the_rollback_saves_nothing",
            "test_a_refused_write_corrects_without_calling_itself_again",
        ],
    },
    {
        # A save per apply writes config.toml on every state message.
        "name": "every-apply-saves-the-position",
        "file": GUI,
        "old": "        if save_correction and list(corrected) != list(pos):",
        "new": "        if save_correction:",
        "expect": ["test_a_position_needing_no_correction_saves_nothing"],
    },
    {
        # A caller that already holds a screen list holds it for a reason:
        # screenRemoved carries the screen Qt has not dropped yet.
        "name": "a-screen-list-the-caller-supplied-is-ignored",
        "file": GUI,
        "old": "        if screens is None:",
        "new": "        if True:",
        "expect": [
            "test_a_removed_screen_is_left_out_of_the_new_layout",
            "test_a_removed_screen_reaches_the_slot_that_leaves_it_out",
        ],
    },
    # --- the Qt seam: gui._screen_bounds ---
    {
        "name": "the-seam-ignores-the-screen-it-is-told-to-exclude",
        "file": GUI,
        "old": "        if exclude is not None and screen is exclude:",
        "new": "        if False:",
        "expect": [
            "test_it_leaves_out_a_screen_it_is_told_to_exclude",
            "test_a_removed_screen_is_left_out_of_the_new_layout",
        ],
    },
    {
        # geometry is the whole screen; availableGeometry leaves out the
        # taskbar and every docked appbar strip. The button is always-on-top,
        # so it is visible and clickable on those strips and a user may park
        # it there. Measuring it against the usable area moves a button the
        # user can see, and every apply but the rollback saves the move.
        "name": "the-seam-reports-the-usable-area-not-full-geometry",
        "file": GUI,
        "old": "        rect = screen.geometry()",
        "new": "        rect = screen.availableGeometry()",
        "expect": [
            "test_a_position_on_the_taskbar_is_left_where_the_user_put_it",
            "test_it_reports_each_screen_as_x_y_width_height",
            "test_it_leaves_out_a_screen_it_is_told_to_exclude",
        ],
    },
    # --- the resize clamp: gui.FloatingButton._current_screen_bounds ---
    {
        # The same question as the entry above, in the other place that
        # decides where the button may sit. _apply_resize_to corrects an
        # edge-drag against the bounds recorded at the press, and
        # _current_screen_bounds records them. Measuring the usable area here
        # while _apply_geometry measures the whole screen makes the two rules
        # disagree about one button: a resize pulls a button parked on the
        # taskbar up off the strip, and the next apply puts it straight back.
        "name": "the-resize-measures-the-usable-area-not-the-whole-screen",
        "file": GUI,
        "old": "        whole = screen.geometry()",
        "new": "        whole = screen.availableGeometry()",
        "expect": [
            "test_the_bounds_recorded_at_the_press_are_the_whole_screen",
            "test_a_resize_does_not_pull_the_button_off_the_taskbar",
        ],
    },
    # --- the layout signals: gui.GuiManager._watch_the_screen_layout ---
    {
        # The defect this gate exists to keep out: primaryScreenChanged
        # carries a QScreen, and _on_screens_changed reads its first
        # argument as the screen LIST (fixed in e3ef48f3).
        "name": "the-primary-screen-change-goes-to-the-screen-list-slot",
        "file": GUI,
        "old": (
            "            app.primaryScreenChanged.connect("
            "self._on_primary_screen_changed)"
        ),
        "new": "            app.primaryScreenChanged.connect(self._on_screens_changed)",
        "expect": ["test_a_primary_screen_change_corrects_the_stored_position"],
    },
    {
        # The same defect from the other side: the discarding slot stops
        # discarding, so every signal argument reaches the screens parameter.
        "name": "the-layout-slot-passes-its-argument-through",
        "file": GUI,
        "old": (
            "        self._on_screens_changed(signal_name=signal_name)\n"
            "\n"
            "    def _on_screen_added(self, screen):"
        ),
        "new": (
            "        self._on_screens_changed(*args)\n"
            "\n"
            "    def _on_screen_added(self, screen):"
        ),
        "expect": [
            "test_a_primary_screen_change_corrects_the_stored_position",
            "test_a_screen_resize_corrects_the_stored_position",
            "test_a_usable_area_change_corrects_the_stored_position",
        ],
    },
    {
        "name": "a-screen-size-change-is-not-watched",
        "file": GUI,
        "old": (
            "            screen.geometryChanged.connect("
            "self._on_screen_geometry_changed)"
        ),
        "new": "            pass",
        "expect": ["test_a_screen_resize_corrects_the_stored_position"],
    },
    {
        "name": "a-usable-area-change-is-not-watched",
        "file": GUI,
        "old": (
            "            screen.availableGeometryChanged.connect(\n"
            "                self._on_screen_available_geometry_changed)"
        ),
        "new": "            pass",
        "expect": ["test_a_usable_area_change_corrects_the_stored_position"],
    },
    {
        # A stored position is in logical pixels, and a change of scale can
        # redefine them: the same monitor is fewer logical pixels across at
        # 150 per cent than at 125. Whether Windows also fires one of the two
        # geometry signals on a change of scale is not measured, so this
        # connection is guarded on its own rather than left to them.
        "name": "a-display-scale-change-is-not-watched",
        "file": GUI,
        "old": (
            "            screen.logicalDotsPerInchChanged.connect(\n"
            "                self._on_screen_logical_dots_per_inch_changed)"
        ),
        "new": "            pass",
        "expect": [
            "test_a_display_scale_change_corrects_the_stored_position",
            "test_an_added_screen_is_watched_and_the_position_re_checked",
        ],
    },
    {
        "name": "an-arriving-screen-is-not-watched",
        "file": GUI,
        "old": (
            "        self._watch_screen(screen)\n"
            "        self._on_screens_changed(signal_name='QGuiApplication.screenAdded')"
        ),
        "new": (
            "        pass\n"
            "        self._on_screens_changed(signal_name='QGuiApplication.screenAdded')"
        ),
        "expect": ["test_an_added_screen_is_watched_and_the_position_re_checked"],
    },
    {
        "name": "a-removed-screen-is-not-left-out",
        "file": GUI,
        "old": (
            "        self._on_screens_changed(\n"
            "            screens=_screen_bounds(exclude=screen),\n"
            "            signal_name='QGuiApplication.screenRemoved')"
        ),
        "new": (
            "        self._on_screens_changed(\n"
            "            screens=_screen_bounds(),\n"
            "            signal_name='QGuiApplication.screenRemoved')"
        ),
        "expect": [
            "test_a_removed_screen_is_left_out_of_the_new_layout",
            "test_a_removed_screen_reaches_the_slot_that_leaves_it_out",
        ],
    },
    {
        # A correction mid-drag fights the pointer the user is holding. The
        # two-line pattern is what makes it unique: the same condition
        # appears again at the state-message apply, one indent level deeper.
        "name": "a-layout-change-mid-gesture-is-applied-anyway",
        "file": GUI,
        "old": (
            "        if self.button._gesture_running:\n"
            "            # Correcting mid-gesture would fight the pointer the user is"
        ),
        "new": (
            "        if False:\n"
            "            # Correcting mid-gesture would fight the pointer the user is"
        ),
        "expect": ["test_a_layout_change_during_a_gesture_is_held_back"],
    },
    {
        # The whole of wh-floating-button-offscreen.1.2. A gesture's own
        # completion clears _deferred_geometry before gesture_ended fires --
        # mouseReleaseEvent emits moved and _finish_resize emits
        # resize_finished, each one reaching its manager slot first -- so
        # the correction a layout change asked for is gone by the time
        # _on_gesture_ended looks for it. The flag is what survives.
        "name": "a-layout-change-mid-gesture-sets-no-flag",
        "file": GUI,
        "old": "            self._layout_changed_during_gesture = True",
        "new": "            pass",
        "expect": [
            "test_a_layout_change_during_a_drag_corrects_when_the_drag_ends",
            "test_a_layout_change_during_a_resize_corrects_when_the_resize_ends",
            "test_a_write_that_is_never_answered_still_leaves_the_button_on_screen",
            "test_a_later_gesture_with_no_layout_signal_is_left_where_it_lands",
        ],
    },
    {
        # A flag set and never cleared corrects every gesture from the first
        # layout change onward, which takes away a position the user parked
        # on purpose -- past an edge, or over the taskbar. Only a second
        # gesture separates the two rules, so the catcher is the test that
        # drags twice.
        "name": "the-gesture-flag-is-never-cleared",
        "file": GUI,
        "old": (
            "        layout_changed = self._layout_changed_during_gesture\n"
            "        self._layout_changed_during_gesture = False"
        ),
        "new": (
            "        layout_changed = self._layout_changed_during_gesture\n"
            "        pass"
        ),
        "expect": [
            "test_a_later_gesture_with_no_layout_signal_is_left_where_it_lands",
        ],
    },
    {
        "name": "a-new-manager-never-starts-watching",
        "file": GUI,
        "old": "        self._watch_the_screen_layout()",
        "new": "        pass",
        "expect": ["test_a_new_manager_starts_watching_the_screen_layout"],
    },
    {
        # Without the early return the connections run against None and the
        # surrounding except logs an exception at every start.
        "name": "no-qt-application-yet-is-logged-as-a-fault",
        "file": GUI,
        "old": "        app = QGuiApplication.instance()\n        if app is None:",
        "new": "        app = QGuiApplication.instance()\n        if False:",
        "expect": ["test_no_qt_application_yet_is_reported_as_nothing_at_all"],
    },
    # --- forcing the native window back (probe 4, 2026-09-20) ---
    {
        # The whole forcing step disappears, which is the shipped behaviour
        # before this fix: the button is re-applied and the window Windows
        # holds stays where the old scale put it.
        "name": "the-window-is-never-forced-back",
        "file": GUI,
        # Anchored on the line after it: _on_gesture_ended calls the same
        # method one indent deeper since c21899ca, so the bare call matches
        # twice. The gesture path has its own entry below.
        "old": (
            "        self._force_native_geometry()\n"
            "        if schedule_delayed:"
        ),
        "new": (
            "        pass\n"
            "        if schedule_delayed:"
        ),
        "expect": [
            "test_a_disagreeing_native_rectangle_is_forced_back",
            "test_the_comparison_uses_the_screen_ratio_not_the_window_ratio",
            "test_a_size_that_disagrees_alone_is_forced_back",
            "test_a_position_that_disagrees_alone_is_forced_back",
            "test_the_normal_path_writes_two_debug_lines",
            "test_a_forcing_call_that_fails_leaves_the_button_working",
        ],
    },
    {
        # Probe 4 measured this exact defect: Qt sends nothing to Windows
        # when the geometry equals the one it has cached, so the setGeometry
        # on its own is the call that is dropped.
        "name": "the-move-away-is-dropped",
        "file": GUI,
        "old": (
            "                self.button.move(QPoint(pos.x() + 1, pos.y() + 1))\n"
            "                self.button.setGeometry(pos.x(), pos.y(), size, size)"
        ),
        "new": "                self.button.setGeometry(pos.x(), pos.y(), size, size)",
        "expect": ["test_a_disagreeing_native_rectangle_is_forced_back"],
    },
    {
        # The other half: the move away reaches Windows and leaves the
        # window one pixel off with the old size.
        "name": "the-setGeometry-is-dropped",
        "file": GUI,
        "old": (
            "                self.button.move(QPoint(pos.x() + 1, pos.y() + 1))\n"
            "                self.button.setGeometry(pos.x(), pos.y(), size, size)"
        ),
        "new": "                self.button.move(QPoint(pos.x() + 1, pos.y() + 1))",
        "expect": [
            "test_a_disagreeing_native_rectangle_is_forced_back",
            "test_a_forcing_call_that_fails_leaves_the_button_working",
        ],
    },
    {
        # The move carries the position Qt already holds, so Qt drops it and
        # the setGeometry that follows is dropped with it.
        "name": "the-away-move-goes-to-the-same-position",
        "file": GUI,
        "old": "                self.button.move(QPoint(pos.x() + 1, pos.y() + 1))",
        "new": "                self.button.move(QPoint(pos.x(), pos.y()))",
        "expect": ["test_a_disagreeing_native_rectangle_is_forced_back"],
    },
    {
        # The comparison reads the WINDOW's device pixel ratio, which probe 4
        # measured lag behind the screen's: it read 3.0 while the screen read
        # 2.0, so the stale rectangle agrees and nothing is forced.
        "name": "the-comparison-uses-the-window-ratio",
        "file": GUI,
        "old": "            return float(screen.devicePixelRatio()) if screen is not None else None",
        "new": "            return float(handle.devicePixelRatio()) if handle is not None else None",
        # Only the dedicated test can catch this one. The other forcing
        # tests give the window handle a stand-in whose float() is 1.0,
        # so their stale rectangle disagrees at that ratio too and the
        # repair still runs (measured in the 2026-09-20 sweep).
        "expect": ["test_the_comparison_uses_the_screen_ratio_not_the_window_ratio"],
    },
    {
        "name": "nothing-ever-disagrees",
        "file": GUI,
        "old": "        return position_disagrees or size_disagrees",
        "new": "        return False",
        "expect": [
            "test_a_disagreeing_native_rectangle_is_forced_back",
            "test_a_size_that_disagrees_alone_is_forced_back",
            "test_a_position_that_disagrees_alone_is_forced_back",
        ],
    },
    {
        # Criterion B6 from the other side: forcing on every layout signal,
        # including the ones where nothing is wrong.
        "name": "everything-always-disagrees",
        "file": GUI,
        "old": "        return position_disagrees or size_disagrees",
        "new": "        return True",
        "expect": ["test_an_agreeing_native_rectangle_changes_nothing"],
    },
    {
        # Probe 4's return direction was a size-only disagreement: the window
        # was at the right position and 99 physical pixels across where 150
        # was right.
        "name": "the-size-is-not-compared",
        "file": GUI,
        "old": "        return position_disagrees or size_disagrees",
        "new": "        return position_disagrees",
        "expect": ["test_a_size_that_disagrees_alone_is_forced_back"],
    },
    {
        "name": "the-position-is-not-compared",
        "file": GUI,
        "old": "        return position_disagrees or size_disagrees",
        "new": "        return size_disagrees",
        "expect": ["test_a_position_that_disagrees_alone_is_forced_back"],
    },
    {
        # The one-pixel rounding tolerance criterion B1 names becomes wide
        # enough to swallow a whole screen.
        "name": "the-rounding-tolerance-swallows-everything",
        "file": GUI,
        "old": (
            "        position_disagrees = (abs(left - wanted_left) > 1\n"
            "                              or abs(top - wanted_top) > 1)"
        ),
        "new": (
            "        position_disagrees = (abs(left - wanted_left) > 100000\n"
            "                              or abs(top - wanted_top) > 100000)"
        ),
        "expect": ["test_a_position_that_disagrees_alone_is_forced_back"],
    },
    {
        # Criterion B4's delayed re-check disappears. Probe 4 measured why it
        # is needed: the first three signals of a resolution change read the
        # screen at the old ratio and rightly found nothing wrong.
        "name": "no-delayed-re-check-is-scheduled",
        "file": GUI,
        # Anchored on its guard: _on_gesture_ended schedules the same
        # re-check since c21899ca, so the bare call matches twice.
        "old": (
            "        if schedule_delayed:\n"
            "            self._schedule_one_delayed_reapply()"
        ),
        "new": (
            "        if schedule_delayed:\n"
            "            pass"
        ),
        "expect": ["test_three_layout_signals_leave_one_delayed_re_apply"],
    },
    {
        # Every signal of one resolution change builds its own timer, which
        # is the defect the _delayed_reapply_pending flag used to keep out
        # before 51ca5b4d replaced it with one reusable single-shot timer.
        # The behaviour is unchanged: one timer serves every display change.
        "name": "a-new-timer-is-built-for-every-display-change",
        "file": GUI,
        "old": (
            "        if self._delayed_reapply_timer is None:\n"
            "            timer = QTimer(self.button)"
        ),
        "new": (
            "        if True:\n"
            "            timer = QTimer(self.button)"
        ),
        "expect": [
            "test_three_layout_signals_leave_one_delayed_re_apply",
            "test_a_second_display_change_restarts_the_settling_delay",
        ],
    },
    {
        # The successor of the-pending-flag-is-never-cleared. Under the flag,
        # never clearing it meant no later display change was followed by a
        # delayed re-check. Under the reusable timer (51ca5b4d) the same harm
        # is the start() moving inside the build: the timer is started once,
        # ever, so a second display change inherits whatever is left of the
        # first delay instead of getting a whole settling time of its own
        # (wh-floating-button-offscreen.4.2).
        "name": "the-settling-delay-is-started-only-once",
        "file": GUI,
        "old": (
            "            self._delayed_reapply_timer = timer\n"
            "        self._delayed_reapply_timer.start(_FORCED_REAPPLY_DELAY_MS)"
        ),
        "new": (
            "            self._delayed_reapply_timer = timer\n"
            "            self._delayed_reapply_timer.start(_FORCED_REAPPLY_DELAY_MS)"
        ),
        "expect": ["test_a_second_display_change_restarts_the_settling_delay"],
    },
    {
        # The delayed re-check schedules another, which is the repeating
        # chain criterion B3 forbids.
        "name": "the-delayed-re-check-schedules-another",
        "file": GUI,
        "old": "            signal_name='the delayed re-check', schedule_delayed=False)",
        "new": "            signal_name='the delayed re-check', schedule_delayed=True)",
        "expect": ["test_the_delayed_re_apply_schedules_nothing_further"],
    },
    {
        # Criterion B3's first verification method. B3 forbids any timer or
        # periodic tick that reads the screen list looking for a change.
        # This entry and the three after it cover two handlers --
        # _check_queues_and_events, the 100 ms queue poll connected at
        # gui.py:1605-1606, and _check_activity_shm, the 10 ms activity
        # poll connected at gui.py:1627-1628 and started at gui.py:1648
        # whenever _gui_shm_name is set, which launcher.py:906-925 always
        # arranges. In gui.py as this commit leaves it, three other
        # handlers run on repeating timers and no entry here covers them:
        # _reassert_topmost (gui.py:593), which raises the button while it
        # is visible; _animate_dots (gui.py:1190), which advances the dot
        # count and sets the label text; and _pulse_tick (gui.py:686),
        # which advances the pulse phase and repaints the button. At this
        # commit none of those three reads the screen list. Rebuild the
        # list of repeating timers with one command over
        # services/wheelhouse/gui.py and no other file:
        # grep -n -A1 "QTimer(" services/wheelhouse/gui.py .
        # It prints each QTimer( line with the line after it, and a QTimer(
        # line whose next line does not call setSingleShot is repeating,
        # which at this commit leaves five: gui.py:368, 390, 1095, 1605 and
        # 1627. This mutation makes
        # the queue poll run the correction on every tick with no display
        # signal at all, which is the shape B3 exists to forbid. The catching
        # test runs that poll 150 times with an empty queue.
        "name": "every-queue-tick-runs-the-correction",
        "file": GUI,
        "old": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "        except Exception:"
        ),
        "new": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "            self._force_native_geometry()\n"
            "            self._schedule_one_delayed_reapply()\n"
            "        except Exception:"
        ),
        "expect": [
            "test_many_ticks_with_no_display_signal_never_run_the_correction_path",
        ],
    },
    {
        # The literal shape B3's text forbids, which the entry above cannot
        # reach: a periodic tick that READS the screen list looking for a
        # change. The correction runs only when the list differs, so on a
        # steady layout every assertion about the correction stays green and
        # only the read itself is visible. Anchored in the queue tick's try
        # block, the same place as the entry above.
        "name": "the-queue-tick-polls-the-screen-list",
        "file": GUI,
        "old": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "        except Exception:"
        ),
        "new": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "            seen = _screen_bounds()\n"
            "            cached = getattr(self, '_polled_screens', seen)\n"
            "            self._polled_screens = seen\n"
            "            if seen != cached:\n"
            "                self._on_screens_changed()\n"
            "        except Exception:"
        ),
        "expect": [
            "test_many_ticks_with_no_display_signal_never_run_the_correction_path",
        ],
    },
    {
        # A deferred correction issued FROM the tick. The mocked QTimer class
        # records the static-method call on its own .singleShot attribute and
        # leaves the class's .called False, so the constructor assertion
        # cannot see it, and the mocked shot never fires, so the correction
        # functions stay uncalled at assertion time. In production the shot
        # would run the correction 1.5 s later with no display signal at all.
        # The shape is historically real: gui.py scheduled with
        # QTimer.singleShot until 51ca5b4d.
        "name": "the-queue-tick-schedules-a-single-shot",
        "file": GUI,
        "old": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "        except Exception:"
        ),
        "new": (
            "        try:\n"
            "            self._check_settings_timeout()\n"
            "            QTimer.singleShot(\n"
            "                _FORCED_REAPPLY_DELAY_MS,\n"
            "                self._reapply_after_the_layout_settled)\n"
            "        except Exception:"
        ),
        "expect": [
            "test_many_ticks_with_no_display_signal_never_run_the_correction_path",
        ],
    },
    {
        # The same violation on the other driven handler. A correction run
        # from the 10 ms activity poll adds no new timer, so B3's grep half
        # stays green, and until the sibling test existed nothing drove this
        # handler at all. Anchored after the payload is decoded, so the tick
        # has already passed both of the handler's early returns.
        "name": "the-activity-tick-runs-the-correction",
        "file": GUI,
        "old": (
            "            data = bytes(self._gui_shm.buf[4:4+size])\n"
            "            msg = json.loads(data.decode('utf-8'))"
        ),
        "new": (
            "            data = bytes(self._gui_shm.buf[4:4+size])\n"
            "            msg = json.loads(data.decode('utf-8'))\n"
            "            self._force_native_geometry()\n"
            "            self._schedule_one_delayed_reapply()"
        ),
        "expect": [
            "test_many_activity_ticks_with_no_display_signal_run_no_correction",
        ],
    },
    {
        # The successor of a-repeating-timer-replaces-the-single-shot. The
        # QTimer.singleShot call that entry named is gone (51ca5b4d); the
        # same behaviour -- a single shot, never a repeating timer, which is
        # what criterion B3 forbids -- now depends on this one line.
        "name": "the-timer-is-not-made-single-shot",
        "file": GUI,
        "old": (
            "            timer = QTimer(self.button)\n"
            "            timer.setSingleShot(True)"
        ),
        "new": "            timer = QTimer(self.button)",
        "expect": [
            "test_the_correction_path_starts_no_repeating_timer",
            "test_a_second_display_change_restarts_the_settling_delay",
        ],
    },
    {
        # The reusable timer is built and started but carries no re-check, so
        # the delay expires and nothing happens. Under the old singleShot
        # call the callback could not be separated from the scheduling; the
        # reusable timer makes this its own way to lose the re-check.
        "name": "the-delayed-re-check-is-never-connected",
        "file": GUI,
        "old": (
            "            timer.setSingleShot(True)\n"
            "            timer.timeout.connect(self._reapply_after_the_layout_settled)"
        ),
        "new": "            timer.setSingleShot(True)",
        "expect": ["test_three_layout_signals_leave_one_delayed_re_apply"],
    },
    # --- a gesture a display change ran into (c21899ca) ---
    {
        # wh-floating-button-offscreen.4.1. _begin_press sets
        # _gesture_running on any press, so a push-to-talk hold can span a
        # display change. The apply when the hold ends re-sends the geometry
        # Qt already holds, which is the call Qt drops, so without the
        # forcing the window keeps the rectangle the old scale gave it.
        "name": "the-forcing-after-a-gesture-is-dropped",
        "file": GUI,
        "old": (
            "            self._force_native_geometry()\n"
            "            self._schedule_one_delayed_reapply()"
        ),
        "new": (
            "            pass\n"
            "            self._schedule_one_delayed_reapply()"
        ),
        "expect": [
            "test_a_held_button_that_spanned_a_display_change_is_forced_back",
            "test_the_forcing_after_a_gesture_uses_the_geometry_the_apply_settled_on",
        ],
    },
    {
        # Criterion B4 on the gesture path: the released hold owes the same
        # single delayed re-check an unheld display change gets, because the
        # screen's ratio lags the change by longer than the gesture may.
        "name": "the-delayed-re-check-after-a-gesture-is-dropped",
        "file": GUI,
        "old": (
            "            self._force_native_geometry()\n"
            "            self._schedule_one_delayed_reapply()"
        ),
        "new": "            self._force_native_geometry()",
        "expect": [
            "test_a_held_button_that_spanned_a_display_change_gets_one_delayed_re_check",
        ],
    },
    {
        # The other half of criterion B2 on this path: forcing after EVERY
        # gesture, not only one a display change ran into. That takes away a
        # position the user parked on purpose and starts a timer per gesture.
        "name": "every-gesture-forces-the-window-back",
        "file": GUI,
        "old": "        if layout_changed:\n            # _begin_press sets",
        "new": "        if True:\n            # _begin_press sets",
        "expect": [
            "test_a_gesture_that_no_display_change_ran_into_forces_nothing",
        ],
    },
    {
        # c21899ca turned this early return into an elif precisely so the
        # deferred path falls through to the forcing. Putting the return
        # back leaves a held button that a display change ran into with the
        # window rectangle the old scale gave it -- the shipped defect.
        "name": "the-deferred-apply-returns-before-the-forcing",
        "file": GUI,
        "old": (
            "            self._apply_geometry(self._deferred_geometry)\n"
            "        elif layout_changed:"
        ),
        "new": (
            "            self._apply_geometry(self._deferred_geometry)\n"
            "            return\n"
            "        if layout_changed:"
        ),
        "expect": [
            "test_a_held_button_that_spanned_a_display_change_is_forced_back",
            "test_a_held_button_that_spanned_a_display_change_gets_one_delayed_re_check",
        ],
    },
    {
        # Criterion B5's first line stops naming the signal that arrived.
        "name": "the-signal-name-is-not-logged",
        "file": GUI,
        "old": (
            "        logger.debug(\n"
            "            'Floating button: %s arrived; re-checking the stored position',\n"
            "            signal_name)"
        ),
        "new": (
            "        logger.debug(\n"
            "            'Floating button: a signal arrived; re-checking the position')"
        ),
        "expect": ["test_the_normal_path_writes_two_debug_lines"],
    },
    {
        # Criterion B5's second line, the one that carries the rectangle
        # before the re-apply and the rectangle after it.
        "name": "the-rectangle-line-is-not-written",
        "file": GUI,
        "old": (
            "        logger.debug(\n"
            "            'Floating button: window rectangle %s before, %s after, forced=%s',\n"
            "            before, after, forced)"
        ),
        "new": "        pass",
        "expect": ["test_the_normal_path_writes_two_debug_lines"],
    },
    {
        # Criterion B7. The expected failure here IS the escaping OSError:
        # the test's whole claim is that a window that cannot be read leaves
        # the button with the behaviour it has today.
        "name": "a-failed-window-reading-is-not-contained",
        "file": GUI,
        "old": (
            "            return win32gui.GetWindowRect(int(self.button.winId()))\n"
            "        except Exception as exc:"
        ),
        "new": (
            "            return win32gui.GetWindowRect(int(self.button.winId()))\n"
            "        except ValueError as exc:"
        ),
        "expect": ["test_a_native_reading_that_fails_leaves_the_button_working"],
    },
    {
        # Criterion B7, the other half: the forcing call itself raises.
        "name": "a-failed-forcing-call-is-not-contained",
        "file": GUI,
        "old": (
            "                forced = True\n"
            "            except Exception as exc:"
        ),
        "new": (
            "                forced = True\n"
            "            except ValueError as exc:"
        ),
        "expect": ["test_a_forcing_call_that_fails_leaves_the_button_working"],
    },
]


def _publish_durable(tmp, path):
    """Replace path with tmp so the rename itself is on disk on return.

    os.replace maps to MoveFileExW with MOVEFILE_REPLACE_EXISTING and no
    write-through, so a power loss after it returns can lose the rename even
    though the temp file's bytes were fsynced; a journal lost that way makes
    the next invocation's recovery silently skip a still-mutated target.
    MOVEFILE_WRITE_THROUGH makes the call wait until the move is on disk.
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.MoveFileExW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ]
        kernel32.MoveFileExW.restype = ctypes.c_int
        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        if not kernel32.MoveFileExW(
            str(tmp), str(path),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError()
    else:
        os.replace(tmp, path)


def _write_atomic(path, data):
    """Write data to path so a partial write can never land in path.

    A same-directory temporary file takes the write; a durable rename swaps
    it in only after a complete, flushed write. Always bytes, never text: on
    Windows Path.write_text turns every \\n into \\r\\n, which silently
    rewrites the whole file's line endings and breaks every multi-line
    pattern on the next run.
    """
    tmp = path.with_name(path.name + ".gate-tmp")
    try:
        with open(tmp, "wb") as f:
            written = f.write(data)
            if written != len(data):
                raise OSError(f"short write: {written} of {len(data)} bytes")
            f.flush()
            os.fsync(f.fileno())
        _publish_durable(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _journal_path(path):
    return path.with_name(path.name + ".gate-journal")


def _write_journal(path, original, mutated):
    """Persist the original and mutant bytes before the mutant lands.

    An abrupt termination while the mutant is on disk bypasses the
    finally-restore, and the original bytes die with the process. The journal
    survives it, so _recover_pending on the next invocation can undo the
    mutant instead of leaving it in a tracked source file. Format: decimal
    length of the original, a newline, the original bytes, the mutant bytes.
    """
    payload = str(len(original)).encode("ascii") + b"\n" + original + mutated
    _write_atomic(_journal_path(path), payload)


def _clear_journal(path):
    try:
        _journal_path(path).unlink()
    except FileNotFoundError:
        pass


def _recover_pending(paths):
    """Undo any mutant a dead run left behind. Returns error strings."""
    errors = []
    for path in sorted(set(paths)):
        journal = _journal_path(path)
        if not journal.exists():
            continue
        try:
            payload = journal.read_bytes()
            header, rest = payload.split(b"\n", 1)
            length = int(header)
            original, mutated = rest[:length], rest[length:]
            current = path.read_bytes()
        except (OSError, ValueError) as e:
            errors.append(f"{path}: unreadable recovery journal {journal}: {e}")
            continue
        if current == mutated and current != original:
            try:
                _write_atomic(path, original)
            except OSError as e:
                errors.append(
                    f"{path}: recovery restore failed; the MUTANT REMAINS: {e}"
                )
                continue
            _clear_journal(path)
            print(f"recovered {path} from an interrupted prior run")
        elif current == original:
            _clear_journal(path)
            print(f"dropped stale journal for {path} (target already original)")
        else:
            errors.append(
                f"{path}: recovery journal exists but the target matches "
                "neither the recorded original nor the recorded mutant; "
                "reconcile the file by hand"
            )
    return errors


def _restore_source(path, original, mutated, name):
    """Put the pre-run bytes back. Returns an error string, or None.

    Never overwrite bytes this run did not write: a save landing from another
    session while pytest ran would otherwise be replaced by the stale pre-run
    snapshot and lost. A failed restore is reported explicitly, because a
    mutant silently left in a tracked source file is the worst outcome this
    script can produce.

    A KeyboardInterrupt landing inside the restore is held, the restore is
    retried once, and the interrupt is re-raised only after the caller has
    cleared the bytecode caches -- a same-size mutant leaves current-looking
    bytecode behind, so re-raising earlier would hand the next run the
    mutant's compiled form.
    """
    for attempt in (1, 2):
        try:
            current = path.read_bytes()
            if current == original:
                return None
            if current != mutated:
                return (f"{name}: {path} changed while pytest ran (bytes "
                        "differ from the written mutant); refusing to "
                        "overwrite the concurrent edit with the stale "
                        "pre-run snapshot -- reconcile the file by hand")
            _write_atomic(path, original)
            return None
        except KeyboardInterrupt:
            if attempt == 2:
                _RESTORE_INTERRUPTED.append(name)
                return (f"{name}: interrupted twice during restore; "
                        f"the MUTANT MAY REMAIN in {path}")
            continue
        except OSError as e:
            if attempt == 2:
                return (f"{name}: restore write failed; the MUTANT REMAINS "
                        f"in {path}: {e}")
            continue
    return None


_RESTORE_INTERRUPTED = []


def _pytest_env():
    # COLUMNS=1000 keeps pytest from truncating a short-summary line to the
    # 80 columns a captured run reports; the node ids here are longer.
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")


def _run_pytest():
    return subprocess.run(
        [PYTHON, "-m", "pytest", *TEST_FILES, "-rf", "-q", "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=300,
    )


def _clear_pycache():
    for d in SERVICE.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def _short_summary(out):
    """Return the lines below pytest's short-summary header, or None."""
    lines = out.splitlines()
    start = None
    for index, line in enumerate(lines):
        if "short test summary info" in line:
            start = index + 1
    return None if start is None else lines[start:]


def _test_name(line):
    return re.sub(r"\[.*\]$", "", line.split("::")[-1].split()[0])


def check_only() -> int:
    """Validate every pattern and Python mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = mutation["file"]
        data = path.read_bytes()
        old = _translate(mutation["old"], data)
        new = _translate(mutation["new"], data)
        count = data.count(old)
        if count == 0:
            stale += 1
            print(f"STALE {mutation['name']}: pattern not found in {path}")
            continue
        if count != 1:
            ambiguous += 1
            print(f"AMBIGUOUS {mutation['name']}: {count} matches in {path}")
            continue
        if path.suffix == ".py":
            try:
                compile(data.replace(old, new, 1).decode("utf-8"), str(path), "exec")
            except SyntaxError as exc:
                broken += 1
                print(f"BROKEN {mutation['name']}: mutant does not compile: {exc}")
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{ambiguous} ambiguous, {broken} that do not compile"
    )
    print("--check is NOT a sweep: it cannot see a survivor or a masked catcher")
    return 1 if stale or ambiguous or broken else 0


def main() -> int:
    argv = sys.argv[1:]
    if "--check" in argv:
        return check_only()
    only = None
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))

    errors = []
    survivors = []

    recovery_errors = _recover_pending(m["file"] for m in MUTATIONS)
    if recovery_errors:
        for e in recovery_errors:
            print(f"ERROR {e}")
        return 1

    selected = [m for m in MUTATIONS if only is None or m["name"] in only]
    if only is not None:
        unknown = only - {m["name"] for m in MUTATIONS}
        if unknown:
            print(f"ERROR --only names no such mutation: {sorted(unknown)}")
            return 1

    # Every expected test name must exist before the first mutation, or a
    # genuine catch is reported as a survivor.
    collect = subprocess.run(
        [PYTHON, "-m", "pytest", *TEST_FILES, "--collect-only", "-q"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=300,
    )
    real_names = {
        _test_name(line) for line in collect.stdout.splitlines() if "::" in line
    }
    for m in selected:
        for name in m["expect"]:
            if name not in real_names:
                errors.append(f"{m['name']}: expected test {name} does not exist")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    base = _run_pytest()
    if base.returncode != 0:
        print("ERROR baseline run is not green; refusing to mutate")
        print(base.stdout[-2000:])
        return 1
    print("baseline green")

    for m in selected:
        path = m["file"]
        data = path.read_bytes()
        old = _translate(m["old"], data)
        new = _translate(m["new"], data)
        count = data.count(old)
        if count == 0:
            errors.append(f"{m['name']}: pattern not found")
            print(f"ERROR {m['name']}: pattern not found in {path}")
            continue
        if count > 1:
            errors.append(f"{m['name']}: pattern ambiguous ({count} matches)")
            print(f"ERROR {m['name']}: pattern ambiguous ({count} matches)")
            continue
        mutated = data.replace(old, new, 1)
        try:
            compile(mutated.decode("utf-8"), str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{m['name']}: mutant does not compile: {e}")
            print(f"ERROR {m['name']}: mutant does not compile: {e}")
            continue
        _clear_pycache()
        try:
            _write_journal(path, data, mutated)
        except OSError as e:
            errors.append(f"{m['name']}: journal write failed: {e}")
            print(f"ERROR {m['name']}: journal write failed; target unchanged: {e}")
            continue
        try:
            _write_atomic(path, mutated)
        except OSError as e:
            errors.append(f"{m['name']}: mutant write failed: {e}")
            print(f"ERROR {m['name']}: mutant write failed; target unchanged: {e}")
            _clear_journal(path)
            continue
        timed_out = False
        try:
            run = _run_pytest()
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error = _restore_source(path, data, mutated, m["name"])
            _clear_pycache()
        if restore_error:
            errors.append(restore_error)
            print(f"ERROR {restore_error}")
            print("aborting the remaining mutations: the source no longer "
                  "matches what this run snapshotted; the recovery journal "
                  "is kept for the next invocation")
            break
        if _RESTORE_INTERRUPTED:
            raise KeyboardInterrupt
        _clear_journal(path)
        if timed_out:
            errors.append(f"{m['name']}: pytest timed out")
            print(f"ERROR {m['name']}: pytest timed out")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        if run.returncode == 0:
            survivors.append(f"{m['name']}: the run was green")
            print(f"SURVIVED {m['name']}: the whole run was green")
            continue
        summary = _short_summary(out)
        if summary is None:
            errors.append(f"{m['name']}: nonzero exit with no short summary")
            print(f"ERROR {m['name']}: exit {run.returncode} and no short "
                  "summary section to read a verdict from")
            continue
        collection_errors = [line for line in summary if line.startswith("ERROR ")]
        if collection_errors:
            errors.append(f"{m['name']}: collection or fixture error")
            print(f"ERROR {m['name']}: the mutant broke collection or a "
                  f"fixture, so no test reached its assertion: "
                  f"{collection_errors[:3]}")
            continue
        failed = {
            _test_name(line) for line in summary if line.startswith("FAILED ")
        }
        missing = [t for t in m["expect"] if t not in failed]
        if missing:
            survivors.append(
                f"{m['name']}: expected {missing}, failed={sorted(failed)}"
            )
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
        else:
            print(f"caught {m['name']} by {sorted(failed & set(m['expect']))}")

    scope = ("full set for this gate" if only is None
             else f"subset named by --only, {len(MUTATIONS) - len(selected)} skipped")
    print(
        f"scope: ran {len(selected)} of {len(MUTATIONS)} mutations ({scope}), "
        f"{len(survivors)} survivors, {len(errors)} errors"
    )
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
