"""Mutation gate for the overlay_keep_numbers setting (wh-numbers-stay-setting).

Run it from services/wheelhouse with the service's own interpreter, never
while a test suite runs in the same worktree (the gate rewrites the sources
that suite imports):

    .venv/Scripts/python.exe tests/mutation_gate_numbers_stay_setting.py
    .venv/Scripts/python.exe tests/mutation_gate_numbers_stay_setting.py --check
    .venv/Scripts/python.exe tests/mutation_gate_numbers_stay_setting.py can-be-dismissed

The runner is the shared one (stt_providers/shared/tests/
mutation_gate_runner.py, reached through mutation_gate_exclusive_speaker). A
bare word on the command line selects every mutation whose name contains it.
``--check`` verifies that every pattern matches exactly once and that every
Python mutant compiles, without running a test; the expected catcher names are
checked against the collected tests when a sweep starts.

WHAT THE BEAD CHANGED, and therefore what this gate defends. With
``[click] overlay_keep_numbers`` false (the default) a successful click on a
number removes the numbers and a change to another application window removes
them, as Windows Voice Access does. True keeps the old refresh behaviour.

  ui/click_config.py       the key, its default, its validator, its path
                           through ``from_raw`` and the disabled fallback
  click_overlay_state.py   ``keep_numbers`` on the machine and
                           ``dismisses_numbers`` on a FOCUS_CHANGE event: the
                           painted and refresh_in_flight arms close, the two
                           first-walk arms never do
  overlay_focus_hooks.py   ``map_foreground_event`` carries the decision
  main.py                  the early HIDE_NUMBERS after a successful click and
                           its same-session and show-numbers-after-the-click
                           conditions; the window-change filter and its three
                           exclusions; the settle-refire flag that carries a
                           coalesced foreground event; the live apply of a
                           saved value
  settings_window_dialog.py  the check box, its default, its place in the tab
                           order, its label, its lack of a restart note

Every mutation names the tests that must fail for it to count as caught.
Pattern-not-found, an ambiguous pattern, a mutant that does not compile, a
timeout, a suite-timeout abort and a missing expected test name are reported
as errors, never as a verdict; a restore that cannot be proved stops the
sweep outright. Every catcher named below was seen to fail on an assertion
under its mutation (not on a crash) when the table was written.

NOT MUTATED, each with the reason (see NOT_MUTATED at the bottom): comment and
docstring edits (gui.py changed only counts in comments), the ``machine is
None`` guards whose removal raises inside a try that returns the same value,
and two behaviour-preserving mutations found equivalent.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_gate_exclusive_speaker as base  # noqa: E402

runner = base.runner
SERVICE = base.SERVICE

CONFIG = SERVICE / "ui" / "click_config.py"
STATE = SERVICE / "click_overlay_state.py"
HOOKS = SERVICE / "overlay_focus_hooks.py"
MAIN = SERVICE / "main.py"
DIALOG = SERVICE / "settings_window_dialog.py"

CONFIG_TESTS = "tests/test_click_config.py"
STATE_TESTS = "tests/test_click_overlay_state.py"
LOGIC_TESTS = "tests/test_logic_overlay_integration.py"
HOOK_TESTS = "tests/test_overlay_focus_hooks.py"
DIALOG_TESTS = "tests/test_settings_window_dialog.py"
HELP_TESTS = "tests/test_settings_help_dialog.py"

CFG_SEL = [CONFIG_TESTS]
STATE_SEL = [STATE_TESTS]
LOGIC_SEL = [LOGIC_TESTS]
JOURNEY_TESTS = "tests/test_overlay_journeys.py::test_journey_rig_starts_with_no_window_change_pending_and_numbers_removed_by_default"
HOOK_SEL = [LOGIC_TESTS, HOOK_TESTS]
DIALOG_SEL = [DIALOG_TESTS, HELP_TESTS]


def M(name, target, old, new, selection, expect):
    return dict(name=name, service=SERVICE, test_file=tuple(selection),
                file=target, old=old, new=new, expect=expect)


# --- click_config.py --------------------------------------------------------
MUTATIONS = [
    M("config-dataclass-default-true", CONFIG,
      "    overlay_keep_numbers: bool = False\n",
      "    overlay_keep_numbers: bool = True\n",
      CFG_SEL, [
        "test_overlay_keep_numbers_defaults_to_false_on_a_directly_built_config",
      ]),
    M("config-defaults-entry-true", CONFIG,
      '    "overlay_keep_numbers": False,\n',
      '    "overlay_keep_numbers": True,\n',
      CFG_SEL, [
        "test_overlay_keep_numbers_defaults_to_false",
        "test_overlay_keep_numbers_off_on_the_disabled_path",
      ]),
    M("config-validator-accepts-anything", CONFIG,
      '    "overlay_keep_numbers": _is_bool,\n',
      '    "overlay_keep_numbers": (lambda value: True),\n',
      CFG_SEL, [
        "test_overlay_keep_numbers_round_trips",
        "test_overlay_keep_numbers_non_bool_disables_overlay_only",
      ]),
    M("config-from-raw-drops-the-value", CONFIG,
      "            overlay_keep_numbers=overlay.overlay_keep_numbers,\n",
      "            overlay_keep_numbers=False,\n",
      CFG_SEL, ["test_overlay_keep_numbers_round_trips"]),
    M("config-validate-overlay-uses-the-default", CONFIG,
      '        overlay_keep_numbers=coerced["overlay_keep_numbers"],\n',
      '        overlay_keep_numbers=defaults["overlay_keep_numbers"],\n',
      CFG_SEL, ["test_overlay_keep_numbers_round_trips"]),
    M("config-disabled-path-true", CONFIG,
      '        overlay_keep_numbers=_DEFAULTS["overlay_keep_numbers"],\n',
      "        overlay_keep_numbers=True,\n",
      CFG_SEL, [
        "test_overlay_keep_numbers_off_on_the_disabled_path",
      ]),
]

# --- click_overlay_state.py -------------------------------------------------
_STATE_PAINTED_ARM = (
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.REFRESH)\n"
)
_STATE_REFRESH_ARM = (
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh_supersede(BuildReason.SUPERSEDE)\n"
)
_WALK_ARM = (
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Nothing painted yet; just arm the auto-hide flag. Stay\n"
)
_PAINT_ARM = (
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Hide the (about-to-be-) overlay, set the flag, stay\n"
)


def _walk_with_dismissal(old):
    return old.replace(
        "        if kind is OverlayEventKind.FOCUS_CHANGE:\n",
        "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
        "            if self._event_dismisses_numbers(event):\n"
        "                return self._hide_to_closed()\n",
        1,
    )


MUTATIONS += [
    M("machine-keep-numbers-default-false", STATE,
      "    keep_numbers: bool = True\n",
      "    keep_numbers: bool = False\n",
      STATE_SEL, [
        "test_keep_numbers_defaults_to_true_on_a_bare_machine",
      ]),
    M("event-dismisses-default-true", STATE,
      "    dismisses_numbers: bool = False\n",
      "    dismisses_numbers: bool = True\n",
      STATE_SEL + LOGIC_SEL, [
        "test_non_dismissing_focus_change_still_refreshes_in_painted_when_keep_numbers_off",
        "test_dismissing_focus_change_closes_in_refresh_in_flight_when_keep_numbers_off",
        "test_click_complete_in_refresh_in_flight_stays_held_when_keep_numbers_off",
      ]),
    M("dismiss-ignores-the-key", STATE,
      "        return event.dismisses_numbers and not self.keep_numbers\n",
      "        return event.dismisses_numbers\n",
      STATE_SEL, [
        "test_dismissing_focus_change_still_refreshes_in_painted_when_keep_numbers_on",
        "test_dismissing_focus_change_still_supersedes_refresh_when_keep_numbers_on",
      ]),
    M("dismiss-ignores-the-event-flag", STATE,
      "        return event.dismisses_numbers and not self.keep_numbers\n",
      "        return not self.keep_numbers\n",
      STATE_SEL, [
        "test_non_dismissing_focus_change_still_refreshes_in_painted_when_keep_numbers_off",
        "test_dismissing_focus_change_closes_in_refresh_in_flight_when_keep_numbers_off",
        "test_click_complete_in_refresh_in_flight_stays_held_when_keep_numbers_off",
      ]),
    M("dismiss-reads-the-key-backwards", STATE,
      "        return event.dismisses_numbers and not self.keep_numbers\n",
      "        return event.dismisses_numbers and self.keep_numbers\n",
      STATE_SEL, [
        "test_dismissing_focus_change_closes_in_painted_when_keep_numbers_off",
        "test_dismissing_focus_change_still_refreshes_in_painted_when_keep_numbers_on",
        "test_dismissing_focus_change_closes_in_refresh_in_flight_when_keep_numbers_off",
      ]),
    M("painted-focus-change-never-closes", STATE,
      _STATE_PAINTED_ARM,
      "            return self._refresh(BuildReason.REFRESH)\n",
      STATE_SEL, [
        "test_dismissing_focus_change_closes_in_painted_when_keep_numbers_off",
      ]),
    M("refresh-in-flight-focus-change-never-closes", STATE,
      _STATE_REFRESH_ARM,
      "            return self._refresh_supersede(BuildReason.SUPERSEDE)\n",
      STATE_SEL, [
        "test_dismissing_focus_change_closes_in_refresh_in_flight_when_keep_numbers_off",
      ]),
    M("walk-in-flight-focus-change-closes", STATE,
      _WALK_ARM, _walk_with_dismissal(_WALK_ARM),
      STATE_SEL, [
        "test_dismissing_focus_change_restarts_the_first_walk_when_keep_numbers_off",
      ]),
    M("paint-in-flight-focus-change-closes", STATE,
      _PAINT_ARM, _walk_with_dismissal(_PAINT_ARM),
      STATE_SEL, [
        "test_dismissing_focus_change_restarts_the_first_walk_when_keep_numbers_off",
      ]),
]

# --- overlay_focus_hooks.py -------------------------------------------------
MUTATIONS += [
    M("map-foreground-drops-the-decision", HOOKS,
      "        kind=OverlayEventKind.FOCUS_CHANGE, "
      "dismisses_numbers=dismisses_numbers,\n",
      "        kind=OverlayEventKind.FOCUS_CHANGE, dismisses_numbers=False,\n",
      HOOK_SEL, [
        "test_foreground_change_to_another_application_removes_numbers",
        "test_foreground_change_to_a_dialog_the_numbered_window_owns_removes_numbers",
        "test_foreground_change_with_overlay_keep_numbers_written_false_removes_numbers",
      ]),
    M("map-foreground-default-true", HOOKS,
      "    *, hwnd: int, dismisses_numbers: bool = False\n",
      "    *, hwnd: int, dismisses_numbers: bool = True\n",
      HOOK_SEL, [
        "test_map_foreground_event_does_not_dismiss_the_numbers_unless_told_to",
      ]),
]

# --- main.py: the machine factory and the early hide ------------------------
_EARLY_HIDE_EVENT = (
    "                OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),\n"
    '                source=f"click_complete ({trigger}) gen={current_pair}",\n'
    "            )\n"
    "            return\n"
    "        if current_pair != overlay_dispatch_pair:\n"
)
_SHOW_COUNT_STAMP = (
    "        show_counts[self._overlay_click_dispatch_ordinal] = getattr(\n"
    '            self, "_overlay_show_numbers_applied", 0,\n'
    "        )\n"
)
_PRUNE_LOOP = (
    "        for record in (show_counts, click_keeps):\n"
    "            for stale in [\n"
    "                key for key in record\n"
    "                if key <= self._overlay_click_dispatch_ordinal\n"
    "                - _OVERLAY_CLICK_SHOW_COUNT_KEEP\n"
    "            ]:\n"
    "                del record[stale]\n"
)

MUTATIONS += [
    M("factory-passes-true", MAIN,
      "        keep_numbers=click_config.overlay_keep_numbers,\n",
      "        keep_numbers=True,\n",
      LOGIC_SEL, [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("early-hide-ignores-click-success", MAIN,
      "            click_succeeded\n            and not click_keep_numbers\n",
      "            True\n            and not click_keep_numbers\n",
      LOGIC_SEL, [
        "test_overlay_keep_numbers_false_stale_refusal_shows_new_numbers",
        "test_overlay_keep_numbers_false_late_stale_refusal_shows_new_numbers",
        "test_overlay_keep_numbers_false_deferred_refusal_replay_shows_new_numbers",
      ]),
    M("early-hide-reads-the-key-backwards", MAIN,
      "            and not click_keep_numbers\n"
      "            and overlay_dispatch_pair[0] == current_pair[0]\n",
      "            and click_keep_numbers\n"
      "            and overlay_dispatch_pair[0] == current_pair[0]\n",
      LOGIC_SEL, [
        "test_send_snapshot_item_click_late_ok_superseded_pair_feed_noops",
        "test_ok_click_feeds_click_complete_and_painted_refreshes",
        "test_ok_click_for_superseded_generation_does_not_refresh",
      ]),
    M("early-hide-session-compare-inverted", MAIN,
      "            and overlay_dispatch_pair[0] == current_pair[0]\n"
      "            and not shown_again\n",
      "            and overlay_dispatch_pair[0] != current_pair[0]\n"
      "            and not shown_again\n",
      LOGIC_SEL, [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("early-hide-ignores-a-later-show-numbers", MAIN,
      "            and not shown_again\n        ):\n",
      "            and True\n        ):\n",
      LOGIC_SEL, [
        "test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers",
        "test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers",
      ]),
    M("early-hide-applies-show-numbers", MAIN,
      "                OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),\n"
      '                source=f"click_complete ({trigger}) gen={current_pair}",\n',
      "                OverlayEvent(kind=OverlayEventKind.SHOW_NUMBERS),\n"
      '                source=f"click_complete ({trigger}) gen={current_pair}",\n',
      LOGIC_SEL, [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("shown-again-inverted", MAIN,
      "            and click_show_count\n"
      '            != getattr(self, "_overlay_show_numbers_applied", 0)\n',
      "            and click_show_count\n"
      '            == getattr(self, "_overlay_show_numbers_applied", 0)\n',
      LOGIC_SEL, [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("shown-again-without-a-recorded-count", MAIN,
      "            click_show_count is not None\n            and click_show_count\n",
      "            True\n            and click_show_count\n",
      LOGIC_SEL, [
        "test_success_reply_with_no_recorded_show_count_still_removes_the_numbers",
      ]),
    M("show-numbers-counter-never-advances", MAIN,
      "            self._overlay_show_numbers_applied = (\n"
      '                getattr(self, "_overlay_show_numbers_applied", 0) + 1\n'
      "            )\n",
      "            self._overlay_show_numbers_applied = (\n"
      '                getattr(self, "_overlay_show_numbers_applied", 0) + 0\n'
      "            )\n",
      LOGIC_SEL, [
        "test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers",
        "test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers",
      ]),
    M("show-numbers-counter-counts-hide", MAIN,
      '        if command == "show":\n'
      "            # wh-numbers-stay-setting (decision D5): see\n",
      '        if command == "hide":\n'
      "            # wh-numbers-stay-setting (decision D5): see\n",
      LOGIC_SEL, [
        "test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers",
        "test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers",
      ]),
    M("click-stamps-no-show-count", MAIN,
      _SHOW_COUNT_STAMP,
      "        show_counts[self._overlay_click_dispatch_ordinal] = 0\n",
      LOGIC_SEL, [
        "test_a_show_numbers_before_the_click_does_not_cancel_the_success_close",
      ]),
    M("show-counts-never-pruned", MAIN,
      _PRUNE_LOOP,
      _PRUNE_LOOP.replace("                del record[stale]\n",
                          "                pass\n"),
      LOGIC_SEL, [
        "test_click_show_counts_keep_exactly_the_newest_32_dispatches",
      ]),
    M("show-counts-keep-one", MAIN,
      "_OVERLAY_CLICK_SHOW_COUNT_KEEP = 32\n",
      "_OVERLAY_CLICK_SHOW_COUNT_KEEP = 1\n",
      LOGIC_SEL, [
        "test_click_show_counts_keep_exactly_the_newest_32_dispatches",
      ]),
    M("show-counts-prune-off-by-one", MAIN,
      "                if key <= self._overlay_click_dispatch_ordinal\n"
      "                - _OVERLAY_CLICK_SHOW_COUNT_KEEP\n",
      "                if key < self._overlay_click_dispatch_ordinal\n"
      "                - _OVERLAY_CLICK_SHOW_COUNT_KEEP\n",
      LOGIC_SEL, [
        "test_click_show_counts_keep_exactly_the_newest_32_dispatches",
      ]),
    # wh-numbers-stay-setting.1.1: the keep_numbers value each click was sent
    # under, so a save made by that click does not change it.
    M("keep-record-never-pruned", MAIN,
      "        for record in (show_counts, click_keeps):\n",
      "        for record in (show_counts,):\n",
      LOGIC_SEL, [
        "test_click_show_counts_keep_exactly_the_newest_32_dispatches",
      ]),
    M("keep-record-never-written", MAIN,
      "        if machine is not None:\n"
      "            click_keeps[self._overlay_click_dispatch_ordinal] = (\n",
      "        if machine is None:\n"
      "            click_keeps[self._overlay_click_dispatch_ordinal] = (\n",
      LOGIC_SEL, [
        "test_a_save_made_by_the_click_does_not_change_that_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_click_on_to_off",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_on_to_off",
      ]),
    M("reply-reads-the-current-keep-value", MAIN,
      "        ).get(overlay_dispatch_ordinal, machine.keep_numbers)\n",
      "        ).get(-1, machine.keep_numbers)\n",
      LOGIC_SEL, [
        "test_a_save_made_by_the_click_does_not_change_that_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_click_on_to_off",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_on_to_off",
      ]),
    M("inner-success-reported-as-refusal", MAIN,
      "                click_succeeded=True,\n"
      "                preclick_snapshot_id=snapshot_id,\n",
      "                click_succeeded=False,\n"
      "                preclick_snapshot_id=snapshot_id,\n",
      LOGIC_SEL, [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_removes_numbers_after_a_click",
        "test_overlay_keep_numbers_false_success_during_a_refresh_closes_at_the_reply_not_after_it_settles",
      ]),
    M("inner-refusal-reported-as-success", MAIN,
      "                click_succeeded=False,\n"
      '                trigger=f"click refused ({response.reason})",\n'
      "                preclick_snapshot_id=snapshot_id,\n",
      "                click_succeeded=True,\n"
      '                trigger=f"click refused ({response.reason})",\n'
      "                preclick_snapshot_id=snapshot_id,\n",
      LOGIC_SEL, [
        "test_overlay_keep_numbers_false_stale_refusal_shows_new_numbers",
        "test_overlay_keep_numbers_false_deferred_refusal_replay_shows_new_numbers",
        "test_refresh_before_a_stale_position_refusal_keeps_new_numbers",
      ]),
    M("late-success-reported-as-refusal", MAIN,
      "                click_succeeded=True,\n"
      "                preclick_snapshot_id=fallback_snapshot_id,\n",
      "                click_succeeded=False,\n"
      "                preclick_snapshot_id=fallback_snapshot_id,\n",
      LOGIC_SEL, [
        "test_send_snapshot_item_click_late_ok_with_pair_runs_success_bookkeeping",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("late-refusal-reported-as-success", MAIN,
      "            click_succeeded=False,\n"
      '            trigger=f"click refused ({response.reason})",\n'
      "            preclick_snapshot_id=fallback_snapshot_id,\n",
      "            click_succeeded=True,\n"
      '            trigger=f"click refused ({response.reason})",\n'
      "            preclick_snapshot_id=fallback_snapshot_id,\n",
      LOGIC_SEL, [
        "test_send_snapshot_item_click_late_rewalk_reason_feeds_refresh",
        "test_send_snapshot_item_click_late_suppressed_notice_still_feeds_refresh",
        "test_overlay_keep_numbers_false_late_stale_refusal_shows_new_numbers",
      ]),
]

# --- main.py: when a window change may remove numbers ----------------------
_CAN_BE_DISMISSED_STATES = (
    "        return machine.state in (\n"
    "            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,\n"
    "        )\n"
)
_FLAG_SET = (
    "            if self._overlay_numbers_can_be_dismissed():\n"
    "                self._overlay_settle_refire_foreground = True\n"
)
_CONSUME_HEAD = (
    '        if not getattr(self, "_overlay_settle_refire_foreground", False):\n'
    "            return False\n"
    "        self._overlay_settle_refire_foreground = False\n"
)
_CONSUME_STATE_CHECK = (
    "        if not self._overlay_numbers_can_be_dismissed():\n"
    "            return False\n"
    "        hwnd = _overlay_current_foreground_hwnd()\n"
)
_FILTER_STATE_CHECK = (
    "        if not self._overlay_numbers_can_be_dismissed():\n"
    "            return False\n"
    "        try:\n"
    "            facts = _overlay_window_facts(hwnd)\n"
)

MUTATIONS += [
    M("can-be-dismissed-ignores-the-key", MAIN,
      '        if machine is None or getattr(machine, "keep_numbers", True):\n'
      "            return False\n",
      "        if machine is None:\n            return False\n",
      LOGIC_SEL, [
        "test_window_change_filter_reads_nothing_when_keep_numbers_is_true",
      ]),
    M("can-be-dismissed-painted-only", MAIN,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (OverlayState.PAINTED,)\n",
      LOGIC_SEL, [
        "test_window_change_filter_answers_for_a_refresh_in_flight_machine",
      ]),
    M("can-be-dismissed-refresh-in-flight-only", MAIN,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (OverlayState.REFRESH_IN_FLIGHT,)\n",
      LOGIC_SEL, [
        "test_foreground_change_to_another_application_removes_numbers",
        "test_foreground_change_to_a_dialog_the_numbered_window_owns_removes_numbers",
        "test_foreground_change_with_overlay_keep_numbers_written_false_removes_numbers",
      ]),
    M("can-be-dismissed-during-the-first-walk", MAIN,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (\n"
      "            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,\n"
      "            OverlayState.WALK_IN_FLIGHT,\n"
      "        )\n",
      LOGIC_SEL, [
        "test_foreground_change_during_the_first_walk_restarts_the_walk",
        "test_settle_refire_reads_no_window_while_the_first_walk_runs",
        "test_a_foreground_event_coalesced_during_the_first_walk_is_not_judged_after_the_paint",
      ]),
    M("filter-answers-during-the-first-walk", MAIN,
      _FILTER_STATE_CHECK,
      "        try:\n            facts = _overlay_window_facts(hwnd)\n",
      LOGIC_SEL, [
        "test_foreground_change_during_the_first_walk_restarts_the_walk",
      ]),
    M("filter-dismisses-an-unreadable-window", MAIN,
      "            if facts is None:\n                return False\n"
      "            if facts.class_name.lower() in",
      "            if facts is None:\n                return True\n"
      "            if facts.class_name.lower() in",
      LOGIC_SEL, [
        "test_foreground_change_with_unreadable_window_facts_refreshes",
      ]),
    M("filter-tooltip-class-removed", MAIN,
      '_OVERLAY_NON_WINDOW_CHANGE_CLASSES = frozenset({"tooltips_class32"})\n',
      "_OVERLAY_NON_WINDOW_CHANGE_CLASSES = frozenset()\n",
      LOGIC_SEL, [
        "test_foreground_change_to_a_tooltip_refreshes",
        "test_settle_refire_does_not_dismiss_for_a_coalesced_tooltip",
      ]),
    M("filter-class-compare-case-sensitive", MAIN,
      "            if facts.class_name.lower() in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:\n",
      "            if facts.class_name in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:\n",
      LOGIC_SEL, [
        "test_window_change_filter_ignores_the_case_of_the_tooltip_class_name",
      ]),
    M("filter-numbered-window-check-dropped", MAIN,
      '            tracked = getattr(self, "_overlay_tracked_identity", None)\n'
      "            if tracked is not None:\n",
      '            tracked = getattr(self, "_overlay_tracked_identity", None)\n'
      "            if False:\n",
      LOGIC_SEL, [
        "test_foreground_change_back_to_the_numbered_window_refreshes",
        "test_foreground_change_to_a_control_inside_the_numbered_window_refreshes",
        "test_foreground_change_back_to_the_numbered_window_makes_no_process_scan",
      ]),
    M("filter-root-compare-inverted", MAIN,
      "                if facts.root == numbered_root:\n",
      "                if facts.root != numbered_root:\n",
      LOGIC_SEL, [
        "test_foreground_change_to_another_application_removes_numbers",
        "test_foreground_change_to_a_control_inside_the_numbered_window_refreshes",
        "test_foreground_change_to_a_dialog_the_numbered_window_owns_removes_numbers",
      ]),
    M("filter-numbered-root-fallback-zero", MAIN,
      "                    else int(tracked.hwnd)\n",
      "                    else 0\n",
      LOGIC_SEL, [
        "test_window_change_filter_matches_a_control_of_an_unreadable_numbered_window",
      ]),
    M("filter-wheelhouse-processes-not-excluded", MAIN,
      "            if facts.pid in self._wheelhouse_process_ids():\n",
      "            if False:\n",
      LOGIC_SEL, [
        "test_foreground_change_to_a_wheelhouse_window_refreshes",
        "test_foreground_change_back_to_the_numbered_window_makes_no_process_scan",
      ]),
    M("filter-never-dismisses", MAIN,
      "                return False\n            return True\n"
      "        except Exception as exc:  # noqa: BLE001 -- never break focus following\n",
      "                return False\n            return False\n"
      "        except Exception as exc:  # noqa: BLE001 -- never break focus following\n",
      LOGIC_SEL, [
        "test_foreground_change_to_another_application_removes_numbers",
        "test_foreground_change_to_a_dialog_the_numbered_window_owns_removes_numbers",
        "test_foreground_change_with_overlay_keep_numbers_written_false_removes_numbers",
      ]),
    M("filter-error-dismisses", MAIN,
      '                "keeping the refresh.", hwnd, exc,\n'
      "            )\n            return False\n",
      '                "keeping the refresh.", hwnd, exc,\n'
      "            )\n            return True\n",
      LOGIC_SEL, [
        "test_window_change_filter_keeps_the_refresh_when_the_process_list_fails",
      ]),
    M("foreground-handler-sends-no-decision", MAIN,
      "                dismisses_numbers=self._foreground_change_dismisses_numbers(\n"
      "                    hwnd\n"
      "                ),\n",
      "                dismisses_numbers=False,\n",
      LOGIC_SEL, [
        "test_foreground_change_to_another_application_removes_numbers",
        "test_foreground_change_to_a_dialog_the_numbered_window_owns_removes_numbers",
        "test_foreground_change_with_overlay_keep_numbers_written_false_removes_numbers",
      ]),
]

# --- main.py: the settle-refire flag ---------------------------------------
MUTATIONS += [
    M("refire-flag-set-while-nothing-is-showing", MAIN,
      _FLAG_SET,
      "            if True:\n"
      "                self._overlay_settle_refire_foreground = True\n",
      LOGIC_SEL, [
        "test_a_foreground_event_coalesced_during_the_first_walk_is_not_judged_after_the_paint",
        "test_a_foreground_event_coalesced_while_closed_does_not_leak_into_a_later_session",
      ]),
    M("refire-flag-never-set", MAIN,
      _FLAG_SET,
      "            if self._overlay_numbers_can_be_dismissed():\n"
      "                self._overlay_settle_refire_foreground = False\n",
      LOGIC_SEL, [
        "test_settle_refire_dismisses_when_the_burst_ended_on_another_application",
        "test_settle_refire_dismisses_when_a_menu_event_came_before_the_window_change",
        "test_settle_refire_dismisses_when_the_last_coalesced_event_named_the_numbered_window_but_another_is_in_front",
      ]),
    M("refire-flag-starts-set", MAIN,
      "        self._overlay_settle_refire_foreground: bool = False\n",
      "        self._overlay_settle_refire_foreground: bool = True\n",
      [JOURNEY_TESTS], [
        "test_journey_rig_starts_with_no_window_change_pending_and_numbers_removed_by_default",
      ]),
    M("consume-ignores-the-flag", MAIN,
      _CONSUME_HEAD,
      "        self._overlay_settle_refire_foreground = False\n",
      LOGIC_SEL, [
        "test_settle_refire_does_not_dismiss_for_a_coalesced_menu_event_alone",
        "test_settle_refire_clears_the_window_change_flag_after_it_runs",
        "test_cancelling_the_settle_refire_clears_the_window_change_flag",
      ]),
    M("consume-never-clears-the-flag", MAIN,
      _CONSUME_HEAD,
      '        if not getattr(self, "_overlay_settle_refire_foreground", False):\n'
      "            return False\n",
      LOGIC_SEL, [
        "test_settle_refire_reads_no_window_while_the_first_walk_runs",
        "test_settle_refire_clears_the_window_change_flag_after_it_runs",
      ]),
    M("consume-reads-the-window-while-nothing-is-showing", MAIN,
      _CONSUME_STATE_CHECK,
      "        hwnd = _overlay_current_foreground_hwnd()\n",
      LOGIC_SEL, [
        "test_settle_refire_reads_no_window_while_the_first_walk_runs",
      ]),
    M("consume-accepts-an-unreadable-foreground", MAIN,
      "        if not hwnd:\n            return False\n"
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      LOGIC_SEL, [
        "test_settle_refire_refreshes_when_the_foreground_window_reads_as_zero_but_has_facts",
      ]),
    M("consume-skips-the-exclusions", MAIN,
      "        if not hwnd:\n            return False\n"
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      "        if not hwnd:\n            return False\n        return True\n",
      LOGIC_SEL, [
        "test_settle_refire_refreshes_when_the_burst_ended_back_on_the_numbered_window",
        "test_settle_refire_does_not_dismiss_for_a_coalesced_tooltip",
      ]),
    M("cancel-keeps-the-flag", MAIN,
      "        self._overlay_settle_refire_foreground = False\n"
      "        handle = self._overlay_settle_handle\n",
      "        handle = self._overlay_settle_handle\n",
      LOGIC_SEL, [
        "test_cancelling_the_settle_refire_clears_the_window_change_flag",
      ]),
    M("refire-never-asks-the-flag", MAIN,
      "        dismisses = self._consume_pending_foreground_dismissal()\n",
      "        dismisses = False\n",
      LOGIC_SEL, [
        "test_settle_refire_reads_no_window_while_the_first_walk_runs",
        "test_settle_refire_dismisses_when_the_burst_ended_on_another_application",
        "test_settle_refire_dismisses_when_a_menu_event_came_before_the_window_change",
      ]),
    M("refire-sends-no-decision", MAIN,
      "                dismisses_numbers=dismisses,\n",
      "                dismisses_numbers=False,\n",
      LOGIC_SEL, [
        "test_settle_refire_dismisses_when_the_burst_ended_on_another_application",
        "test_settle_refire_dismisses_when_a_menu_event_came_before_the_window_change",
        "test_settle_refire_dismisses_when_the_last_coalesced_event_named_the_numbered_window_but_another_is_in_front",
      ]),
    M("menu-event-drops-the-pending-change", MAIN,
      "        if self._consume_pending_foreground_dismissal():\n"
      "            event = dataclasses.replace(event, dismisses_numbers=True)\n"
      "        self._cancel_overlay_settle_refire()\n"
      '        self._apply_overlay_event(event, source=f"menu popup {verb}")\n',
      "        if False:\n"
      "            event = dataclasses.replace(event, dismisses_numbers=True)\n"
      "        self._cancel_overlay_settle_refire()\n"
      '        self._apply_overlay_event(event, source=f"menu popup {verb}")\n',
      LOGIC_SEL, [
        "test_a_menu_event_that_fires_at_once_keeps_a_pending_window_change",
      ]),
    M("pattern-manager-event-drops-the-pending-change", MAIN,
      "        event = decision.event\n"
      "        # As in ``_on_overlay_menu_popup_change``: settle a coalesced\n"
      "        # foreground event into this event before the cancel drops it.\n"
      "        if self._consume_pending_foreground_dismissal():\n",
      "        event = decision.event\n"
      "        # As in ``_on_overlay_menu_popup_change``: settle a coalesced\n"
      "        # foreground event into this event before the cancel drops it.\n"
      "        if False:\n",
      LOGIC_SEL, [
        "test_a_pattern_manager_event_that_fires_at_once_keeps_a_pending_window_change",
      ]),
]

# --- main.py: a saved value reaches the running machine --------------------
_SAVE_ONE = (
    "        self._apply_saved_overlay_keep_numbers([key], saved)\n"
    "        return saved\n"
)
_SAVE_GROUP = (
    "        self._apply_saved_overlay_keep_numbers(list(values or {}), saved)\n"
    "        return saved\n"
)

MUTATIONS += [
    M("apply-saved-ignores-a-failed-save", MAIN,
      '        if not saved or "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      '        if "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      LOGIC_SEL, [
        "test_failed_save_leaves_the_machine_unchanged_and_returns_false",
      ]),
    M("apply-saved-ignores-the-key-name", MAIN,
      '        if not saved or "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      "        if not saved:\n            return\n",
      LOGIC_SEL, [
        "test_saving_an_unrelated_key_does_not_touch_the_machine",
      ]),
    M("apply-saved-inverts-the-value", MAIN,
      "            machine.keep_numbers = ClickConfig.from_raw(\n"
      '                self.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      "            machine.keep_numbers = not ClickConfig.from_raw(\n"
      '                self.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
        "test_next_click_after_a_save_follows_the_new_value",
      ]),
    M("apply-saved-reads-an-empty-section", MAIN,
      '                self.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      "                {}\n            ).overlay_keep_numbers\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
        "test_next_click_after_a_save_follows_the_new_value",
      ]),
    M("save-one-never-applies", MAIN,
      _SAVE_ONE,
      "        pass\n        return saved\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_next_click_after_a_save_follows_the_new_value",
      ]),
    M("save-one-passes-no-keys", MAIN,
      _SAVE_ONE,
      "        self._apply_saved_overlay_keep_numbers([], saved)\n"
      "        return saved\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_next_click_after_a_save_follows_the_new_value",
      ]),
    M("save-one-reports-success-always", MAIN,
      _SAVE_ONE,
      "        self._apply_saved_overlay_keep_numbers([key], saved)\n"
      "        return True\n",
      LOGIC_SEL, [
        "test_failed_save_leaves_the_machine_unchanged_and_returns_false",
      ]),
    M("save-group-never-applies", MAIN,
      _SAVE_GROUP,
      "        pass\n        return saved\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
      ]),
    M("save-group-passes-no-keys", MAIN,
      _SAVE_GROUP,
      "        self._apply_saved_overlay_keep_numbers([], saved)\n"
      "        return saved\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
      ]),
    M("save-group-reports-success-always", MAIN,
      _SAVE_GROUP,
      "        self._apply_saved_overlay_keep_numbers(list(values or {}), saved)\n"
      "        return True\n",
      LOGIC_SEL, [
        "test_failed_group_save_returns_false_and_leaves_the_machine_unchanged",
      ]),
    M("handler-map-skips-the-live-apply-one", MAIN,
      "self._set_config_value_and_apply(command.get('key')",
      "self.state_manager.set_config_value(command.get('key')",
      LOGIC_SEL, [
        "test_gui_handler_map_routes_both_save_commands_through_the_live_apply",
      ]),
    M("handler-map-skips-the-live-apply-group", MAIN,
      "self._set_config_values_and_apply(command.get('values') or {}",
      "self.state_manager.set_config_values(command.get('values') or {}",
      LOGIC_SEL, [
        "test_gui_handler_map_routes_both_save_commands_through_the_live_apply",
      ]),
]

# --- settings_window_dialog.py ----------------------------------------------
MUTATIONS += [
    M("dialog-key-name-wrong", DIALOG,
      'OVERLAY_KEEP_NUMBERS_KEY = "click.overlay_keep_numbers"\n',
      'OVERLAY_KEEP_NUMBERS_KEY = "click.overlay_keep_number"\n',
      DIALOG_SEL, [
        "test_a_true_keep_numbers_key_shows_the_box_checked",
        "test_opening_asks_logic_for_the_seven_saved_settings",
        "test_the_keep_numbers_box_saves_click_overlay_keep_numbers",
      ]),
    M("dialog-key-not-asked-of-logic", DIALOG,
      "    WAKE_WORD_KEY,\n    OVERLAY_KEEP_NUMBERS_KEY,\n)\n",
      "    WAKE_WORD_KEY,\n)\n",
      DIALOG_SEL, [
        "test_effective_values_come_from_the_config_values_result",
        "test_a_true_keep_numbers_key_shows_the_box_checked",
        "test_opening_asks_logic_for_the_seven_saved_settings",
      ]),
    M("dialog-missing-key-shows-checked", DIALOG,
      "        if key == OVERLAY_KEEP_NUMBERS_KEY:\n"
      "            return value is True\n",
      "        if key == OVERLAY_KEEP_NUMBERS_KEY:\n"
      "            return True if value is None else bool(value)\n",
      DIALOG_SEL, [
        "test_a_missing_keep_numbers_key_shows_the_box_unchecked",
      ]),
    M("dialog-box-not-refreshed-from-the-values", DIALOG,
      "            (OVERLAY_KEEP_NUMBERS_KEY, self._keep_numbers_box),\n",
      "",
      DIALOG_SEL, [
        "test_effective_values_come_from_the_config_values_result",
        "test_a_true_keep_numbers_key_shows_the_box_checked",
        "test_the_saved_settings_stay_disabled_until_the_values_arrive",
      ]),
    M("dialog-box-saves-the-wrong-key", DIALOG,
      "                OVERLAY_KEEP_NUMBERS_KEY, self._keep_numbers_box),\n",
      "                WAKE_WORD_KEY, self._keep_numbers_box),\n",
      DIALOG_SEL, ["test_the_keep_numbers_box_saves_click_overlay_keep_numbers"]),
    M("dialog-label-changed", DIALOG,
      '"Keep numbers on after a click or window change"',
      '"Keep numbers on after a click"',
      DIALOG_SEL, [
        "test_effective_values_come_from_the_config_values_result",
        "test_the_keep_numbers_box_has_the_exact_label_and_accessible_name",
        "test_every_setting_has_window_text_in_real_file",
      ]),
    M("dialog-box-tabs-after-help", DIALOG,
      "        order += [self._wake_box, self._keep_numbers_box,\n"
      "                  self._training_word_btn, self._help_btn,\n"
      "                  self._restart_btn, self._close_btn]\n",
      "        order += [self._wake_box, self._help_btn, self._keep_numbers_box,\n"
      "                  self._training_word_btn,\n"
      "                  self._restart_btn, self._close_btn]\n",
      DIALOG_SEL, [
        "test_the_keep_numbers_box_follows_the_wake_word_box_in_the_tab_order",
        "test_help_button_in_bottom_row_and_tab_order",
      ]),
    M("dialog-box-gets-a-restart-note", DIALOG,
      "                OVERLAY_KEEP_NUMBERS_KEY, self._keep_numbers_box),\n"
      "        )\n\n        # Not a setting: it opens the",
      "                OVERLAY_KEEP_NUMBERS_KEY, self._keep_numbers_box),\n"
      "        )\n"
      "        self._add_restart_note(other_layout, OVERLAY_KEEP_NUMBERS_KEY)\n"
      "\n        # Not a setting: it opens the",
      DIALOG_SEL, [
        "test_the_keep_numbers_box_has_no_restart_note",
        "test_restart_text_sits_beside_exactly_settings_9_to_13",
      ]),
]

# Each entry names code or behaviour this gate deliberately does not break.
NOT_MUTATED = {
    "services/wheelhouse/gui.py":
        "the diff changes only the setting counts inside two comments; "
        "there is no behaviour to break",
    "machine-is-None guards in _apply_saved_overlay_keep_numbers and "
    "_overlay_numbers_can_be_dismissed":
        "removing the guard raises AttributeError, which the surrounding "
        "try/except (or getattr default) turns into the same return value, "
        "so the observable behaviour is identical",
    "facts-is-None branch in _foreground_change_dismisses_numbers":
        "removing it raises AttributeError on facts.class_name; the "
        "enclosing except returns False, the branch's own answer. The "
        "mutation that flips the branch's answer is in the table",
    "early hide that falls through to the refresh instead of returning "
    "(_feed_click_complete_refresh)":
        "EQUIVALENT. The fall-through arms a deferred post-click clear that "
        "_reconcile_overlay_pending_postclick_clear drops at once, because "
        "the machine is already CLOSED after the hide; the visible result and "
        "every recorded effect are the same",
    "dropping the tracked-handle shortcut in _foreground_change_dismisses_"
    "numbers":
        "EQUIVALENT. The GA_ROOT comparison that follows gives the same "
        "answer for the numbered window's own handle; the shortcut only saves "
        "one Win32 read, and the no-process-scan test already pins the "
        "outcome",
    "_record_pending_postclick_refresh":
        "a one-line extraction of an assignment the pre-existing deferral "
        "tests already read back; the bead added no behaviour there",
}


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
