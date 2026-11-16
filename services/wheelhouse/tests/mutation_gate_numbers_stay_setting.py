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

Every mutation names the tests that must fail for it to count as caught,
or carries the key "survives" with a reason (wh-full-refactor.3.1): the old
test that caught it is deleted by the overlay refactor, and no boundary test
can produce the order it needs, or the mutant changes nothing a boundary test
can observe. An expected survivor passes only when its whole selection stays
green; a failed test prints "CAUGHT EXPECTED SURVIVOR" and the gate exits 1.
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
CONTROLLER = SERVICE / "numbered_overlay" / "controller.py"
HELPERS = SERVICE / "numbered_overlay" / "helpers.py"
DIALOG = SERVICE / "settings_window_dialog.py"

CONFIG_TESTS = "tests/test_click_config.py"
STATE_TESTS = "tests/test_click_overlay_state.py"
LOGIC_TESTS = "tests/test_logic_overlay_integration.py"
HOOK_TESTS = "tests/test_overlay_focus_hooks.py"
DIALOG_TESTS = "tests/test_settings_window_dialog.py"
HELP_TESTS = "tests/test_settings_help_dialog.py"
# wh-full-refactor.2: the boundary tests that replaced the old tests
# deleted in phase B of the overlay pilot (wh-overlay-refactor-pilot.2).
REWALK_TESTS = "tests/test_overlay_requirements_rewalk.py"
CLICK_TESTS = "tests/test_overlay_requirements_click.py"

CFG_SEL = [CONFIG_TESTS]
STATE_SEL = [STATE_TESTS]
LOGIC_SEL = [LOGIC_TESTS]
HOOK_SEL = [LOGIC_TESTS, HOOK_TESTS]
DIALOG_SEL = [DIALOG_TESTS, HELP_TESTS]

# The selection of every mutation that named a catcher the overlay refactor
# deleted (wh-full-refactor.3.1): the logic file and the two boundary files
# that replace those catchers. An expected survivor must leave all of it
# green.
REPLACED_SEL = LOGIC_SEL + [REWALK_TESTS, CLICK_TESTS]


def M(name, target, old, new, selection, expect, survives=None):
    mutation = dict(name=name, service=SERVICE, test_file=tuple(selection),
                    file=target, old=old, new=new, expect=expect)
    # The key is added only when given, as in
    # tests/mutation_gate_overlay_requirements.py: the shared runner treats
    # the presence of "survives" as the declaration.
    if survives is not None:
        mutation["survives"] = survives
    return mutation


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
      HOOK_SEL + [REWALK_TESTS], [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
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
    M("factory-passes-true", HELPERS,
      "        keep_numbers=click_config.overlay_keep_numbers,\n",
      "        keep_numbers=True,\n",
      LOGIC_SEL + [CLICK_TESTS], [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("early-hide-ignores-click-success", CONTROLLER,
      "            click_succeeded\n            and not click_keep_numbers\n",
      "            True\n            and not click_keep_numbers\n",
      LOGIC_SEL + [CLICK_TESTS], [
        "test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints",
        "test_overlay_keep_numbers_false_late_stale_refusal_shows_new_numbers",
        "test_overlay_keep_numbers_false_deferred_refusal_replay_shows_new_numbers",
      ]),
    M("early-hide-reads-the-key-backwards", CONTROLLER,
      "            and not click_keep_numbers\n"
      "            and overlay_dispatch_pair[0] == current_pair[0]\n",
      "            and click_keep_numbers\n"
      "            and overlay_dispatch_pair[0] == current_pair[0]\n",
      REPLACED_SEL, [
        "test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place",
      ]),
    M("early-hide-session-compare-inverted", CONTROLLER,
      "            and overlay_dispatch_pair[0] == current_pair[0]\n"
      "            and not shown_again\n",
      "            and overlay_dispatch_pair[0] != current_pair[0]\n"
      "            and not shown_again\n",
      LOGIC_SEL + [CLICK_TESTS], [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("early-hide-ignores-a-later-show-numbers", CONTROLLER,
      "            and not shown_again\n        ):\n",
      "            and True\n        ):\n",
      LOGIC_SEL, [
        "test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers",
        "test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers",
      ]),
    M("early-hide-applies-show-numbers", CONTROLLER,
      "                OverlayEvent(kind=OverlayEventKind.HIDE_NUMBERS),\n"
      '                source=f"click_complete ({trigger}) gen={current_pair}",\n',
      "                OverlayEvent(kind=OverlayEventKind.SHOW_NUMBERS),\n"
      '                source=f"click_complete ({trigger}) gen={current_pair}",\n',
      LOGIC_SEL + [CLICK_TESTS], [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("shown-again-inverted", CONTROLLER,
      "            and click_show_count\n"
      '            != getattr(self, "_overlay_show_numbers_applied", 0)\n',
      "            and click_show_count\n"
      '            == getattr(self, "_overlay_show_numbers_applied", 0)\n',
      LOGIC_SEL + [CLICK_TESTS], [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("shown-again-without-a-recorded-count", CONTROLLER,
      "            click_show_count is not None\n            and click_show_count\n",
      "            True\n            and click_show_count\n",
      REPLACED_SEL, [],
      survives=(
          "Result 4(c): Needs a click's reply to arrive after 32 newer "
          'badge-click dispatches pruned its record (the prune loop in '
          'numbered_overlay/controller.py, _OVERLAY_CLICK_SHOW_COUNT_KEEP = '
          '32)'
      )),
    M("show-numbers-counter-never-advances", CONTROLLER,
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
    M("show-numbers-counter-counts-hide", CONTROLLER,
      '        if command == "show":\n'
      "            # wh-numbers-stay-setting (decision D5): see\n",
      '        if command == "hide":\n'
      "            # wh-numbers-stay-setting (decision D5): see\n",
      LOGIC_SEL, [
        "test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers",
        "test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers",
      ]),
    M("click-stamps-no-show-count", CONTROLLER,
      _SHOW_COUNT_STAMP,
      "        show_counts[self._overlay_click_dispatch_ordinal] = 0\n",
      REPLACED_SEL, [
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
      ]),
    M("show-counts-never-pruned", CONTROLLER,
      _PRUNE_LOOP,
      _PRUNE_LOOP.replace("                del record[stale]\n",
                          "                pass\n"),
      REPLACED_SEL, [],
      survives=(
          'Result 4(c): memory use; an output change needs a reply for a '
          'click 32 or more dispatches old'
      )),
    M("show-counts-keep-one", HELPERS,
      "_OVERLAY_CLICK_SHOW_COUNT_KEEP = 32\n",
      "_OVERLAY_CLICK_SHOW_COUNT_KEEP = 1\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(c): Needs a timed-out click, a re-said show numbers, a '
          'newer click, then the late reply'
      )),
    M("show-counts-prune-off-by-one", CONTROLLER,
      "                if key <= self._overlay_click_dispatch_ordinal\n"
      "                - _OVERLAY_CLICK_SHOW_COUNT_KEEP\n",
      "                if key < self._overlay_click_dispatch_ordinal\n"
      "                - _OVERLAY_CLICK_SHOW_COUNT_KEEP\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(c): memory use; an output change needs a reply for a '
          'click 32 or more dispatches old'
      )),
    # wh-numbers-stay-setting.1.1: the keep_numbers value each click was sent
    # under, so a save made by that click does not change it.
    M("keep-record-never-pruned", CONTROLLER,
      "        for record in (show_counts, click_keeps):\n",
      "        for record in (show_counts,):\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(c): memory use; an output change needs a reply for a '
          'click 32 or more dispatches old'
      )),
    M("keep-record-never-written", CONTROLLER,
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
    M("reply-reads-the-current-keep-value", CONTROLLER,
      "        ).get(overlay_dispatch_ordinal, machine.keep_numbers)\n",
      "        ).get(-1, machine.keep_numbers)\n",
      LOGIC_SEL, [
        "test_a_save_made_by_the_click_does_not_change_that_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_click_on_to_off",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_off_to_on",
        "test_a_save_made_by_the_click_does_not_change_that_late_click_on_to_off",
      ]),
    M("inner-success-reported-as-refusal", CONTROLLER,
      "                click_succeeded=True,\n"
      "                preclick_snapshot_id=snapshot_id,\n",
      "                click_succeeded=False,\n"
      "                preclick_snapshot_id=snapshot_id,\n",
      LOGIC_SEL + [CLICK_TESTS], [
        "test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click",
        "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins",
        "test_overlay_keep_numbers_false_success_during_a_refresh_closes_at_the_reply_not_after_it_settles",
      ]),
    M("inner-refusal-reported-as-success", CONTROLLER,
      "                click_succeeded=False,\n"
      '                trigger=f"click refused ({response.reason})",\n'
      "                preclick_snapshot_id=snapshot_id,\n",
      "                click_succeeded=True,\n"
      '                trigger=f"click refused ({response.reason})",\n'
      "                preclick_snapshot_id=snapshot_id,\n",
      LOGIC_SEL + [CLICK_TESTS], [
        "test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints",
        "test_overlay_keep_numbers_false_deferred_refusal_replay_shows_new_numbers",
        "test_refresh_before_a_stale_position_refusal_keeps_new_numbers",
      ]),
    M("late-success-reported-as-refusal", HELPERS,
      "                click_succeeded=True,\n"
      "                preclick_snapshot_id=fallback_snapshot_id,\n",
      "                click_succeeded=False,\n"
      "                preclick_snapshot_id=fallback_snapshot_id,\n",
      REPLACED_SEL, [
        "test_overlay_keep_numbers_false_late_success_removes_numbers",
      ]),
    M("late-refusal-reported-as-success", HELPERS,
      "            click_succeeded=False,\n"
      '            trigger=f"click refused ({response.reason})",\n'
      "            preclick_snapshot_id=fallback_snapshot_id,\n",
      "            click_succeeded=True,\n"
      '            trigger=f"click refused ({response.reason})",\n'
      "            preclick_snapshot_id=fallback_snapshot_id,\n",
      REPLACED_SEL, [
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
    M("can-be-dismissed-ignores-the-key", CONTROLLER,
      '        if machine is None or getattr(machine, "keep_numbers", True):\n'
      "            return False\n",
      "        if machine is None:\n            return False\n",
      REPLACED_SEL, [],
      survives=(
          'Item 4 exception 1: changes only a Win32 read and the event '
          'field dismisses_numbers, which the machine reads only in the '
          'painted and refresh_in_flight states and ignores when '
          'keep_numbers is on (_event_dismisses_numbers in '
          'click_overlay_state.py)'
      )),
    M("can-be-dismissed-painted-only", CONTROLLER,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (OverlayState.PAINTED,)\n",
      REPLACED_SEL, [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
      ]),
    M("can-be-dismissed-refresh-in-flight-only", CONTROLLER,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (OverlayState.REFRESH_IN_FLIGHT,)\n",
      LOGIC_SEL + [REWALK_TESTS], [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
      ]),
    M("can-be-dismissed-during-the-first-walk", CONTROLLER,
      _CAN_BE_DISMISSED_STATES,
      "        return machine.state in (\n"
      "            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,\n"
      "            OverlayState.WALK_IN_FLIGHT,\n"
      "        )\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N11: Needs the first walk and paint to finish inside '
          'the 250 ms debounce window after a coalesced foreground event; '
          'otherwise the mutant changes only a Win32 read and the event '
          'field dismisses_numbers, which the machine ignores during the '
          'first walk'
      )),
    M("filter-answers-during-the-first-walk", CONTROLLER,
      _FILTER_STATE_CHECK,
      "        try:\n            facts = _overlay_window_facts(hwnd)\n",
      REPLACED_SEL, [],
      survives=(
          'Item 4 exception 1: changes only a Win32 read and the event '
          'field dismisses_numbers, which the machine reads only in the '
          'painted and refresh_in_flight states and ignores when '
          'keep_numbers is on (_event_dismisses_numbers in '
          'click_overlay_state.py)'
      )),
    M("filter-dismisses-an-unreadable-window", CONTROLLER,
      "            if facts is None:\n                return False\n"
      "            if facts.class_name.lower() in",
      "            if facts is None:\n                return True\n"
      "            if facts.class_name.lower() in",
      LOGIC_SEL + [REWALK_TESTS], [
        "test_rw_6_non_window_change_with_keep_off_refreshes",
      ]),
    M("filter-tooltip-class-removed", HELPERS,
      '_OVERLAY_NON_WINDOW_CHANGE_CLASSES = frozenset({"tooltips_class32"})\n',
      "_OVERLAY_NON_WINDOW_CHANGE_CLASSES = frozenset()\n",
      REPLACED_SEL, [
        "test_rw_6_non_window_change_with_keep_off_refreshes",
      ]),
    M("filter-class-compare-case-sensitive", CONTROLLER,
      "            if facts.class_name.lower() in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:\n",
      "            if facts.class_name in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N10: Needs a tooltip whose class name reads in '
          'another case'
      )),
    M("filter-numbered-window-check-dropped", CONTROLLER,
      '            tracked = getattr(self, "_overlay_tracked_identity", None)\n'
      "            if tracked is not None:\n",
      '            tracked = getattr(self, "_overlay_tracked_identity", None)\n'
      "            if False:\n",
      REPLACED_SEL, [
        "test_rw_6_non_window_change_with_keep_off_refreshes",
      ]),
    M("filter-root-compare-inverted", CONTROLLER,
      "                if facts.root == numbered_root:\n",
      "                if facts.root != numbered_root:\n",
      LOGIC_SEL + [REWALK_TESTS], [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
        "test_rw_6_non_window_change_with_keep_off_refreshes",
      ]),
    M("filter-numbered-root-fallback-zero", CONTROLLER,
      "                    else int(tracked.hwnd)\n",
      "                    else 0\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N10: Needs the numbered window unreadable while a '
          "window inside it is in front; rw_6's child case reads the "
          'numbered window facts, so the fallback never runs'
      )),
    M("filter-wheelhouse-processes-not-excluded", CONTROLLER,
      "            if facts.pid in self._host.wheelhouse_process_ids():\n",
      "            if False:\n",
      REPLACED_SEL, [
        "test_rw_6_non_window_change_with_keep_off_refreshes",
      ]),
    M("filter-never-dismisses", CONTROLLER,
      "                return False\n            return True\n"
      "        except Exception as exc:  # noqa: BLE001 -- never break focus following\n",
      "                return False\n            return False\n"
      "        except Exception as exc:  # noqa: BLE001 -- never break focus following\n",
      LOGIC_SEL + [REWALK_TESTS], [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
      ]),
    M("filter-error-dismisses", CONTROLLER,
      '                "keeping the refresh.", hwnd, exc,\n'
      "            )\n            return False\n",
      '                "keeping the refresh.", hwnd, exc,\n'
      "            )\n            return True\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(c): Needs the process-id read or the window-facts read '
          'to raise; both catch their own exceptions '
          '(_wheelhouse_process_ids in main.py, _overlay_window_facts in '
          'numbered_overlay/helpers.py)'
      )),
    M("foreground-handler-sends-no-decision", CONTROLLER,
      "                dismisses_numbers=self._foreground_change_dismisses_numbers(\n"
      "                    hwnd\n"
      "                ),\n",
      "                dismisses_numbers=False,\n",
      LOGIC_SEL + [REWALK_TESTS], [
        "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
      ]),
]

# --- main.py: the settle-refire flag ---------------------------------------
MUTATIONS += [
    M("refire-flag-set-while-nothing-is-showing", CONTROLLER,
      _FLAG_SET,
      "            if True:\n"
      "                self._overlay_settle_refire_foreground = True\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N11 and case 4(a) RW-11 (NOT_REQUIRED): Needs a '
          'foreground event coalesced while closed or during the first walk '
          'with the paint finishing inside the debounce window'
      )),
    M("refire-flag-never-set", CONTROLLER,
      _FLAG_SET,
      "            if self._overlay_numbers_can_be_dismissed():\n"
      "                self._overlay_settle_refire_foreground = False\n",
      REPLACED_SEL, [
        "test_rw_12_burst_ending_on_another_app_clears_instead_of_walking",
      ]),
    M("refire-flag-starts-set", CONTROLLER,
      "        self._overlay_settle_refire_foreground: bool = False\n",
      "        self._overlay_settle_refire_foreground: bool = True\n",
      REPLACED_SEL + ["tests/test_overlay_journeys.py"], [],
      survives=(
          'Case 4(a) RW-18 (NOT_REQUIRED). Needs a menu or tree change as '
          'the first focus event while painted with another application in '
          'front'
      )),
    M("consume-ignores-the-flag", CONTROLLER,
      _CONSUME_HEAD,
      "        self._overlay_settle_refire_foreground = False\n",
      REPLACED_SEL, [],
      survives=(
          'Result 1 rw_15 (lead decision 15): Needs another application in '
          'front at the refire with no foreground event since the last '
          'decision'
      )),
    M("consume-never-clears-the-flag", CONTROLLER,
      _CONSUME_HEAD,
      '        if not getattr(self, "_overlay_settle_refire_foreground", False):\n'
      "            return False\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N11 and N2d: Needs a later refire or immediate menu '
          'or tree decision with another application in front and no '
          'foreground event since the flag was consumed'
      )),
    M("consume-reads-the-window-while-nothing-is-showing", CONTROLLER,
      _CONSUME_STATE_CHECK,
      "        hwnd = _overlay_current_foreground_hwnd()\n",
      REPLACED_SEL, [],
      survives=(
          'Item 4 exception 1: only an extra GetForegroundWindow read; '
          '_foreground_change_dismisses_numbers starts with the same '
          'can-be-dismissed check and returns False'
      )),
    M("consume-accepts-an-unreadable-foreground", CONTROLLER,
      "        if not hwnd:\n            return False\n"
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      REPLACED_SEL, [],
      survives=(
          'Item 4 exception 1: no output change; _overlay_window_facts(0) '
          'returns None, so _foreground_change_dismisses_numbers(0) returns '
          'False'
      )),
    M("consume-skips-the-exclusions", CONTROLLER,
      "        if not hwnd:\n            return False\n"
      "        return self._foreground_change_dismisses_numbers(hwnd)\n",
      "        if not hwnd:\n            return False\n        return True\n",
      REPLACED_SEL, [
        "test_rw_13_burst_ending_on_numbered_window_refreshes",
      ]),
    M("cancel-keeps-the-flag", CONTROLLER,
      "        self._overlay_settle_refire_foreground = False\n"
      "        handle = self._overlay_settle_handle\n",
      "        handle = self._overlay_settle_handle\n",
      REPLACED_SEL, [],
      survives=(
          'Result 4(b) N2d: Needs another application in front at the '
          'refire with no foreground event since the last decision after '
          'the refire was cancelled'
      )),
    M("refire-never-asks-the-flag", CONTROLLER,
      "        dismisses = self._consume_pending_foreground_dismissal()\n",
      "        dismisses = False\n",
      REPLACED_SEL, [
        "test_rw_12_burst_ending_on_another_app_clears_instead_of_walking",
      ]),
    M("refire-sends-no-decision", CONTROLLER,
      "                dismisses_numbers=dismisses,\n",
      "                dismisses_numbers=False,\n",
      REPLACED_SEL, [
        "test_rw_12_burst_ending_on_another_app_clears_instead_of_walking",
      ]),
    M("menu-event-drops-the-pending-change", CONTROLLER,
      "        if self._consume_pending_foreground_dismissal():\n"
      "            event = dataclasses.replace(event, dismisses_numbers=True)\n"
      "        self._cancel_overlay_settle_refire()\n"
      '        self._apply_overlay_event(event, source=f"menu popup {verb}")\n',
      "        if False:\n"
      "            event = dataclasses.replace(event, dismisses_numbers=True)\n"
      "        self._cancel_overlay_settle_refire()\n"
      '        self._apply_overlay_event(event, source=f"menu popup {verb}")\n',
      REPLACED_SEL, [],
      survives=(
          'Case 4(a): a menu event after a coalesced foreground event, row '
          'RW-18 NOT_REQUIRED'
      )),
    M("pattern-manager-event-drops-the-pending-change", CONTROLLER,
      "        event = decision.event\n"
      "        # As in ``_on_overlay_menu_popup_change``: settle a coalesced\n"
      "        # foreground event into this event before the cancel drops it.\n"
      "        if self._consume_pending_foreground_dismissal():\n",
      "        event = decision.event\n"
      "        # As in ``_on_overlay_menu_popup_change``: settle a coalesced\n"
      "        # foreground event into this event before the cancel drops it.\n"
      "        if False:\n",
      REPLACED_SEL, [],
      survives=(
          'Case 4(a): a Pattern Manager tree event after a coalesced '
          'foreground event, row RW-18 NOT_REQUIRED'
      )),
]

# --- main.py: a saved value reaches the running machine --------------------
_SAVE_ONE = (
    "        self.overlay.apply_saved_overlay_keep_numbers([key], saved)\n"
    "        return saved\n"
)
_SAVE_GROUP = (
    "        self.overlay.apply_saved_overlay_keep_numbers(list(values or {}), saved)\n"
    "        return saved\n"
)

MUTATIONS += [
    M("apply-saved-ignores-a-failed-save", CONTROLLER,
      '        if not saved or "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      '        if "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      LOGIC_SEL, [
        "test_failed_save_leaves_the_machine_unchanged_and_returns_false",
      ]),
    M("apply-saved-ignores-the-key-name", CONTROLLER,
      '        if not saved or "click.overlay_keep_numbers" not in keys:\n'
      "            return\n",
      "        if not saved:\n            return\n",
      LOGIC_SEL, [
        "test_saving_an_unrelated_key_does_not_touch_the_machine",
      ]),
    M("apply-saved-inverts-the-value", CONTROLLER,
      "            machine.keep_numbers = ClickConfig.from_raw(\n"
      '                self._host.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      "            machine.keep_numbers = not ClickConfig.from_raw(\n"
      '                self._host.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      REPLACED_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
        "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
      ]),
    M("apply-saved-reads-an-empty-section", CONTROLLER,
      '                self._host.config_service.get("click", {})\n'
      "            ).overlay_keep_numbers\n",
      "                {}\n            ).overlay_keep_numbers\n",
      REPLACED_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
        "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
      ]),
    M("save-one-never-applies", MAIN,
      _SAVE_ONE,
      "        pass\n        return saved\n",
      REPLACED_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
      ]),
    M("save-one-passes-no-keys", MAIN,
      _SAVE_ONE,
      "        self.overlay.apply_saved_overlay_keep_numbers([], saved)\n"
      "        return saved\n",
      REPLACED_SEL, [
        "test_saved_keep_numbers_value_reaches_the_machine_without_a_restart",
        "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
      ]),
    M("save-one-reports-success-always", MAIN,
      _SAVE_ONE,
      "        self.overlay.apply_saved_overlay_keep_numbers([key], saved)\n"
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
      "        self.overlay.apply_saved_overlay_keep_numbers([], saved)\n"
      "        return saved\n",
      LOGIC_SEL, [
        "test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine",
      ]),
    M("save-group-reports-success-always", MAIN,
      _SAVE_GROUP,
      "        self.overlay.apply_saved_overlay_keep_numbers(list(values or {}), saved)\n"
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
