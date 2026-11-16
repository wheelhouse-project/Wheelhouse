"""Mutation evidence for the overlay requirement tests (wh-overlay-refactor-pilot.1).

Acceptance item 5 of the bead: for each requirement that has a test in
tests/test_overlay_requirements_{lifecycle,click,rewalk,autoopen}.py, a
mutation of the production code that implements the requirement makes that
requirement's tests fail. Each mutation name starts with the lowercase
requirement ID from docs/design/overlay-requirements.md.

The shared runner in services/stt_providers/shared/tests/mutation_gate_runner.py
owns the disciplines of the mutation-gate skill: the exactly-one-match and
line-start checks, the compile check of each mutant, the check that every
expected test name is collected, the green baseline per selection, the
per-run timeout with process ownership, the "+++ Timeout +++" check, the
pytest result-line check, bytecode clearing with PYTHONDONTWRITEBYTECODE=1,
COLUMNS=1000, and byte-exact restoration in a finally. This file only declares
the table and narrows each mutation's test selection to its expected catchers
plus one or two sanity tests (a whole requirement file takes 65 to 80 s).

A mutation marked EXPECTED SURVIVOR breaks a rule that the boundary cannot
observe on its own. Its add() call gives the reason as survives=, and the
comment above it gives the full reason. The runner reports it as an expected
survivor, and the sweep fails when any test of its selection fails under it,
because the reason is then no longer true. It keeps the requirement's test
in its expect list, so the name check still covers that test.

Run from services/wheelhouse with the worktree's interpreter:
  .venv/Scripts/python.exe tests/mutation_gate_overlay_requirements.py --check
  .venv/Scripts/python.exe tests/mutation_gate_overlay_requirements.py
  .venv/Scripts/python.exe tests/mutation_gate_overlay_requirements.py lc-13 ck-15
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
    """The runner's failed-name set, with a parameter id that holds a space.

    pytest prints a failed parametrized test as
    "FAILED tests/x.py::test_lc_1_...[apply numbers] - AssertionError". The
    runner takes the first whitespace-separated word, which here ends inside
    the brackets ("test_lc_1_...[apply"), and its "\\[.*\\]$" strip then
    leaves the unclosed bracket in place, so the bare test name is never in
    the set and a caught mutation reads as SURVIVED. Cutting at the first
    "[" is safe because a Python function name cannot contain one.
    """
    return {name.split("[", 1)[0] for name in _runner_failed_names(output)}


runner._failed_names = _failed_names

MUTATIONS = []


def sel(module, *names):
    """The pytest node ids of the named tests in one requirement test file."""
    return tuple(f"tests/{module}::{name}" for name in names)


def add(name, file, old, new, *expect, suite, survives=None):
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


# ===========================================================================
# LC: lifecycle (tests/test_overlay_requirements_lifecycle.py)
# ===========================================================================
_LC = "test_overlay_requirements_lifecycle.py"

# LC-1: "show numbers" while closed sends one start_overlay_walk.
LC_1 = sel(_LC, "test_lc_1_show_numbers_while_closed_sends_one_walk",
           "test_lc_3_hide_numbers_while_closed_sends_nothing")
add("lc-1-closed-show-dispatches-no-walk", "click_overlay_state.py",
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            return ApplyResult(\n"
    "                OverlayOutcome.ACCEPTED,\n"
    "                (\n"
    "                    self._dispatch_build(BuildReason.SHOW_NUMBERS),\n"
    "                    self._arm_timer(OverlayState.WALK_IN_FLIGHT),\n",
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            return ApplyResult(\n"
    "                OverlayOutcome.ACCEPTED,\n"
    "                (\n"
    "                    self._arm_timer(OverlayState.WALK_IN_FLIGHT),\n",
    "test_lc_1_show_numbers_while_closed_sends_one_walk",
    "test_lc_3_hide_numbers_while_closed_sends_nothing",
    suite=LC_1)

# LC-2: "hide numbers" clears, unpins, and closes at once.
LC_2 = sel(_LC, "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once",
           "test_lc_3_hide_numbers_while_closed_sends_nothing")
add("lc-2-painted-hide-ignored", "click_overlay_state.py",
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.CLICK_N:\n"
    "            # The integration dispatches click_snapshot_item; no state\n",
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n"
    "        if kind is OverlayEventKind.CLICK_N:\n"
    "            # The integration dispatches click_snapshot_item; no state\n",
    "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once",
    suite=LC_2)
add("lc-2-hide-does-not-unpin", "click_overlay_state.py",
    "        case an in-flight state was hidden.\n"
    "        \"\"\"\n"
    "\n"
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n",
    "        case an in-flight state was hidden.\n"
    "        \"\"\"\n"
    "\n"
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        self._enter_closed()\n",
    "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once",
    suite=LC_2)
add("lc-2-hide-waits-for-the-cleared-ack", "click_overlay_state.py",
    "        case an in-flight state was hidden.\n"
    "        \"\"\"\n"
    "\n"
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n",
    "        case an in-flight state was hidden.\n"
    "        \"\"\"\n"
    "\n"
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        effects.extend(self._unpin_all_pinned())\n",
    "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once",
    suite=LC_2)

# LC-3: "hide numbers" while closed sends nothing.
LC_3 = sel(_LC, "test_lc_3_hide_numbers_while_closed_sends_nothing",
           "test_lc_1_show_numbers_while_closed_sends_one_walk")
add("lc-3-closed-hide-sends-a-clear", "click_overlay_state.py",
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n"
    "        if kind is OverlayEventKind.CLICK_N:\n"
    "            # By-name: routed to click_element by the integration. No state\n",
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.CLICK_N:\n"
    "            # By-name: routed to click_element by the integration. No state\n",
    "test_lc_3_hide_numbers_while_closed_sends_nothing",
    suite=LC_3)

# LC-4: with clicking or the overlay off, show and hide send nothing.
LC_4 = sel(_LC, "test_lc_4_show_and_hide_do_nothing_while_overlay_off",
           "test_lc_1_show_numbers_while_closed_sends_one_walk")
add("lc-4-overlay-gate-drops-the-overlay-term", "numbered_overlay/controller.py",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "test_lc_4_show_and_hide_do_nothing_while_overlay_off",
    suite=LC_4)
add("lc-4-overlay-gate-drops-the-click-term", "numbered_overlay/controller.py",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "        if not (\n"
    "            self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "test_lc_4_show_and_hide_do_nothing_while_overlay_off",
    suite=LC_4)

# LC-7: "show numbers" while painted refreshes one generation higher with no
# clear before the new paint.
LC_7 = sel(_LC, "test_lc_7_show_numbers_while_painted_refreshes_without_clearing",
           "test_lc_8_show_during_refresh_drops_the_older_walk_reply",
           "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once")
add("lc-7-painted-show-restarts-with-a-clear", "click_overlay_state.py",
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            return self._refresh(BuildReason.REFRESH)\n"
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.REFRESH)\n"
    "        if kind is OverlayEventKind.CLICK_COMPLETE:\n",
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            return self._restart_walk(BuildReason.SHOW_NUMBERS)\n"
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.REFRESH)\n"
    "        if kind is OverlayEventKind.CLICK_COMPLETE:\n",
    "test_lc_7_show_numbers_while_painted_refreshes_without_clearing",
    suite=LC_7)
add("lc-7-refresh-keeps-the-generation", "click_overlay_state.py",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        self._bump_generation()\n"
    "        self.state = OverlayState.REFRESH_IN_FLIGHT\n",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        self.state = OverlayState.REFRESH_IN_FLIGHT\n",
    "test_lc_7_show_numbers_while_painted_refreshes_without_clearing",
    "test_lc_8_show_during_refresh_drops_the_older_walk_reply",
    suite=LC_7)

# LC-8: "show numbers" during a refresh supersedes it one generation higher.
LC_8 = sel(_LC, "test_lc_8_show_during_refresh_drops_the_older_walk_reply",
           "test_lc_7_show_numbers_while_painted_refreshes_without_clearing")
add("lc-8-supersede-keeps-the-generation", "click_overlay_state.py",
    "            self._prior_pin_deferred = False\n"
    "        self._bump_generation()\n"
    "        # state stays REFRESH_IN_FLIGHT\n",
    "            self._prior_pin_deferred = False\n"
    "        # state stays REFRESH_IN_FLIGHT\n",
    "test_lc_8_show_during_refresh_drops_the_older_walk_reply",
    suite=LC_8)

# LC-9: "show numbers" during the first walk or paint restarts it: clear only
# if a paint was sent, unpin the old snapshot, generation raised. (The clear
# half is also broken by the rw-2 restart mutations below.)
LC_9 = sel(_LC, "test_lc_9_show_during_first_walk_restarts_without_a_clear",
           "test_lc_9_show_during_first_paint_clears_unpins_and_restarts",
           "test_lc_1_show_numbers_while_closed_sends_one_walk")
add("lc-9-restart-does-not-unpin", "click_overlay_state.py",
    "        unpin = self._unpin_current()\n"
    "        if unpin is not None:\n"
    "            effects.append(unpin)\n"
    "        self.pinned_snapshot_id = None\n"
    "        self._bump_generation()\n"
    "        self.state = OverlayState.WALK_IN_FLIGHT\n",
    "        self.pinned_snapshot_id = None\n"
    "        self._bump_generation()\n"
    "        self.state = OverlayState.WALK_IN_FLIGHT\n",
    "test_lc_9_show_during_first_paint_clears_unpins_and_restarts",
    suite=LC_9)
add("lc-9-restart-keeps-the-generation", "click_overlay_state.py",
    "        self.pinned_snapshot_id = None\n"
    "        self._bump_generation()\n"
    "        self.state = OverlayState.WALK_IN_FLIGHT\n",
    "        self.pinned_snapshot_id = None\n"
    "        self.state = OverlayState.WALK_IN_FLIGHT\n",
    "test_lc_9_show_during_first_walk_restarts_without_a_clear",
    "test_lc_9_show_during_first_paint_clears_unpins_and_restarts",
    suite=LC_9)

# LC-10: "show numbers" in the error state starts a new session.
LC_10 = sel(_LC, "test_lc_10_show_numbers_in_error_starts_a_new_session",
            "test_lc_37_hide_numbers_in_error_closes_the_overlay")
add("lc-10-error-show-keeps-the-old-session", "click_overlay_state.py",
    "            self._enter_closed()  # clears stale per-session state + pin\n"
    "            self._start_session()\n",
    "            self._enter_closed()  # clears stale per-session state + pin\n",
    "test_lc_10_show_numbers_in_error_starts_a_new_session",
    suite=LC_10)

# LC-11: an ok walk reply pins and paints at one pair; not painted yet.
LC_11 = sel(_LC, "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair",
            "test_lc_16_painted_ack_makes_click_n_resolve_to_the_list")
add("lc-11-ok-walk-reply-not-pinned", "click_overlay_state.py",
    "                    self._cancel_timer(),\n"
    "                    self._pin(event.snapshot_id),\n"
    "                    self._dispatch_paint(event.snapshot_id),\n"
    "                    self._arm_timer(OverlayState.PAINT_IN_FLIGHT),\n",
    "                    self._cancel_timer(),\n"
    "                    self._dispatch_paint(event.snapshot_id),\n"
    "                    self._arm_timer(OverlayState.PAINT_IN_FLIGHT),\n",
    "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair",
    suite=LC_11)
add("lc-11-ok-walk-reply-treated-as-painted", "click_overlay_state.py",
    "            # Normal: -> paint_in_flight, pin + dispatch paint.\n"
    "            self.pinned_snapshot_id = event.snapshot_id\n"
    "            self.state = OverlayState.PAINT_IN_FLIGHT\n",
    "            # Normal: -> paint_in_flight, pin + dispatch paint.\n"
    "            self.pinned_snapshot_id = event.snapshot_id\n"
    "            self.state = OverlayState.PAINTED\n",
    "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair",
    suite=LC_11)

# LC-13: a walk reply for a passed generation, or after hide, is neither
# pinned nor painted.
# EXPECTED SURVIVOR: the rule is enforced in two places. The commit fence in
# main.py (_machine_still_awaits_this_build) drops a reply whose pair or state
# the machine has left, and behind it the machine drops the same reply: the
# generation gate in apply() rejects a passed generation, and the closed
# state's late-completion cell takes a reply that arrives after hide. Breaking
# the fence alone leaves the gate, so no boundary output changes; breaking the
# gate alone is caught by LC-18 (lc-18-generation-gate-ignores-the-generation).
# Not a boundary limit but two layers: neither single mutation can break the
# observable rule, so this one is kept as a record of the redundancy.
LC_13 = sel(_LC, "test_lc_13_late_walk_reply_is_neither_pinned_nor_painted",
            "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair")
add("lc-13-commit-fence-ignores-the-pair", "numbered_overlay/controller.py",
    "            return machine.state in (\n"
    "                OverlayState.WALK_IN_FLIGHT, OverlayState.REFRESH_IN_FLIGHT,\n"
    "                OverlayState.POST_CLICK_SETTLING,\n"
    "            ) and (\n"
    "                machine.overlay_session_id, machine.paint_generation\n"
    "            ) == (sid, gen)\n",
    "            return True\n",
    "test_lc_13_late_walk_reply_is_neither_pinned_nor_painted",
    survives=("Needs two layers broken at once: the main.py commit fence and the machine's generation gate both drop the reply"),
    suite=LC_13)

# LC-14: a failed first walk shows numbers_not_drawn and closes.
LC_14 = sel(_LC, "test_lc_14_failed_first_walk_shows_notice_and_closes",
            "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair")
add("lc-14-failed-walk-reply-without-notice", "click_overlay_state.py",
    "                # (standalone \"show numbers\"), error -> closed (wh-n29v.16.1).\n"
    "                return self._error_to_closed(emit_standalone_notice=True)\n",
    "                # (standalone \"show numbers\"), error -> closed (wh-n29v.16.1).\n"
    "                return self._error_to_closed(emit_standalone_notice=False)\n",
    "test_lc_14_failed_first_walk_shows_notice_and_closes",
    suite=LC_14)
# EXPECTED SURVIVOR (reason 3): the walk_in_flight TIMEOUT cell cannot be
# reached from the boundary. An unanswered start_overlay_walk fails first at
# its own reply wait (numbered_overlay/controller.py NumberedOverlayController._overlay_dispatch_build, "no reply within %dms; feeding
# build_ok=False"), and the failed BUILD_RESPONSE cell shows the same notice
# and closes, so the [timeout] param passes whatever the TIMEOUT cell does.
add("lc-14-walk-timeout-without-notice", "click_overlay_state.py",
    "            # \"show numbers\"), unpin (wh-n29v.16.1, v4 line 278).\n"
    "            return self._error_to_closed(emit_standalone_notice=True)\n",
    "            # \"show numbers\"), unpin (wh-n29v.16.1, v4 line 278).\n"
    "            return self._error_to_closed(emit_standalone_notice=False)\n",
    "test_lc_14_failed_first_walk_shows_notice_and_closes",
    survives=('Needs the walk_in_flight TIMEOUT cell reached: the walk reply wait fails first with the same notice and close'),
    suite=LC_14)

# LC-16: a painted acknowledgement makes "click N" resolve to the list.
LC_16 = sel(_LC, "test_lc_16_painted_ack_makes_click_n_resolve_to_the_list",
            "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair")
add("lc-16-painted-ack-leaves-paint-in-flight", "click_overlay_state.py",
    "                # -> painted.\n"
    "                self.state = OverlayState.PAINTED\n",
    "                # -> painted.\n"
    "                pass\n",
    "test_lc_16_painted_ack_makes_click_n_resolve_to_the_list",
    suite=LC_16)

# LC-17: a failed or missing first paint acknowledgement clears and closes,
# with no generic notice.
LC_17 = sel(_LC, "test_lc_17_failed_or_missing_paint_ack_clears_and_closes",
            "test_lc_16_painted_ack_makes_click_n_resolve_to_the_list")
add("lc-17-failed-paint-ack-ignored", "click_overlay_state.py",
    "                # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "                return self._error_to_closed(emit_standalone_notice=False)\n"
    "            # CLEARED ack here is unexpected (no hide happened): bookkeeping.\n",
    "                # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "                return ApplyResult(OverlayOutcome.NO_OP)\n"
    "            # CLEARED ack here is unexpected (no hide happened): bookkeeping.\n",
    "test_lc_17_failed_or_missing_paint_ack_clears_and_closes",
    suite=LC_17)
add("lc-17-paint-timeout-fires-the-generic-notice", "click_overlay_state.py",
    "            # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "            return self._error_to_closed(emit_standalone_notice=False)\n"
    "        # build_response / click_complete / auto_open / focused_hwnd_destroyed\n",
    "            # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "            return self._error_to_closed(emit_standalone_notice=True)\n"
    "        # build_response / click_complete / auto_open / focused_hwnd_destroyed\n",
    "test_lc_17_failed_or_missing_paint_ack_clears_and_closes",
    suite=LC_17)

# LC-18: an acknowledgement at another pair is ignored.
LC_18 = sel(_LC, "test_lc_18_acknowledgement_at_another_pair_is_ignored",
            "test_lc_16_painted_ack_makes_click_n_resolve_to_the_list")
add("lc-18-generation-gate-ignores-the-generation", "click_overlay_state.py",
    "                event.overlay_session_id != self.overlay_session_id\n"
    "                or event.paint_generation != self.paint_generation\n",
    "                event.overlay_session_id != self.overlay_session_id\n",
    "test_lc_18_acknowledgement_at_another_pair_is_ignored",
    suite=LC_18)
add("lc-18-generation-gate-ignores-the-session", "click_overlay_state.py",
    "                event.overlay_session_id != self.overlay_session_id\n"
    "                or event.paint_generation != self.paint_generation\n",
    "                event.paint_generation != self.paint_generation\n",
    "test_lc_18_acknowledgement_at_another_pair_is_ignored",
    suite=LC_18)

# LC-19: a cleared acknowledgement at the current pair changes nothing.
LC_19 = sel(_LC, "test_lc_19_cleared_acknowledgement_changes_nothing",
            "test_lc_28_expired_lease_report_clears_and_closes")
add("lc-19-cleared-ack-closes-like-expired", "click_overlay_state.py",
    "            if event.paint_state is PaintAckState.EXPIRED:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the GUI's badge lease\n",
    "            if event.paint_state in (\n"
    "                PaintAckState.EXPIRED, PaintAckState.CLEARED,\n"
    "            ):\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the GUI's badge lease\n",
    "test_lc_19_cleared_acknowledgement_changes_nothing",
    suite=LC_19)

# LC-20: a refresh pins the new snapshot with the paint and unpins the old one
# only after the acknowledgement.
LC_20 = sel(_LC, "test_lc_20_refresh_unpins_the_old_snapshot_after_the_new_paint",
            "test_lc_7_show_numbers_while_painted_refreshes_without_clearing")
add("lc-20-old-snapshot-never-unpinned", "click_overlay_state.py",
    "                effects.extend(self._commit_refresh_prior_unpin())\n"
    "                self.state = OverlayState.PAINTED\n",
    "                self.state = OverlayState.PAINTED\n",
    "test_lc_20_refresh_unpins_the_old_snapshot_after_the_new_paint",
    suite=LC_20)
add("lc-20-old-snapshot-unpinned-before-the-paint", "click_overlay_state.py",
    "        self.pinned_snapshot_id = snapshot_id\n"
    "        effects.append(self._pin(snapshot_id))\n"
    "        if self.auto_hide_in_flight:\n",
    "        self.pinned_snapshot_id = snapshot_id\n"
    "        effects.append(self._pin(snapshot_id))\n"
    "        effects.extend(self._commit_refresh_prior_unpin())\n"
    "        if self.auto_hide_in_flight:\n",
    "test_lc_20_refresh_unpins_the_old_snapshot_after_the_new_paint",
    suite=LC_20)

# LC-21: a failed refresh on the same foreground window keeps the numbers.
LC_21_22 = sel(_LC,
               "test_lc_21_failed_refresh_on_same_window_keeps_the_numbers",
               "test_lc_22_failed_refresh_after_window_left_clears_and_closes")
add("lc-21-every-failed-refresh-closes", "click_overlay_state.py",
    "        return event.visible_window_left and not self.auto_hide_in_flight\n",
    "        return not self.auto_hide_in_flight\n",
    "test_lc_21_failed_refresh_on_same_window_keeps_the_numbers",
    suite=LC_21_22)
add("lc-21-same-window-read-as-left", "numbered_overlay/controller.py",
    "        if self._overlay_refresh_visible_window_is_foreground(machine) is not False:\n"
    "            return event\n",
    "        if self._overlay_refresh_visible_window_is_foreground(machine) is not True:\n"
    "            return event\n",
    "test_lc_21_failed_refresh_on_same_window_keeps_the_numbers",
    "test_lc_22_failed_refresh_after_window_left_clears_and_closes",
    suite=LC_21_22)

# LC-22: a failed refresh after the numbered window left clears and closes.
add("lc-22-failed-refresh-always-falls-back", "click_overlay_state.py",
    "        return event.visible_window_left and not self.auto_hide_in_flight\n",
    "        return False\n",
    "test_lc_22_failed_refresh_after_window_left_clears_and_closes",
    suite=LC_21_22)
add("lc-22-window-left-never-marked", "numbered_overlay/controller.py",
    "        return dataclasses.replace(event, visible_window_left=True)\n",
    "        return event\n",
    "test_lc_22_failed_refresh_after_window_left_clears_and_closes",
    suite=LC_21_22)

# LC-26: a clear with no acknowledgement in 5000 ms raises one error report.
LC_26 = sel(_LC, "test_lc_26_unacknowledged_clear_raises_an_error_notification",
            "test_lc_2_hide_numbers_clears_unpins_and_closes_at_once")
add("lc-26-cleared-ack-does-not-resolve-the-clear", "numbered_overlay/controller.py",
    "            if state not in (\"cleared\", \"expired\"):\n"
    "                return\n"
    "        elif (sid, gen) < pending:\n",
    "            if state not in (\"expired\",):\n"
    "                return\n"
    "        elif (sid, gen) < pending:\n",
    "test_lc_26_unacknowledged_clear_raises_an_error_notification",
    suite=LC_26)
add("lc-26-clear-deadline-reports-nothing", "numbered_overlay/controller.py",
    "        if pending != (sid, gen):\n"
    "            return  # resolved or superseded since arming; nothing to report\n",
    "        if True:\n"
    "            return  # resolved or superseded since arming; nothing to report\n",
    "test_lc_26_unacknowledged_clear_raises_an_error_notification",
    suite=LC_26)

# LC-27 and LC-30: a painted overlay renews a bounded badge lease (at least
# 60000 ms, at most four missed ticks) every keepalive tick and re-puts the
# snapshot.
LC_27_30 = sel(_LC,
               "test_lc_27_painted_overlay_renews_its_lease_every_interval",
               "test_lc_30_logic_grants_only_a_bounded_badge_lease",
               "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair")
add("lc-27-no-lease-renew", "numbered_overlay/controller.py",
    "        if machine.state is OverlayState.PAINTED:\n"
    "            interval_s = getattr(self, \"_overlay_keepalive_interval_s\", 15.0)\n",
    "        if False:\n"
    "            interval_s = getattr(self, \"_overlay_keepalive_interval_s\", 15.0)\n",
    "test_lc_27_painted_overlay_renews_its_lease_every_interval",
    "test_lc_30_logic_grants_only_a_bounded_badge_lease",
    "test_lc_11_ok_walk_reply_pins_and_paints_at_the_same_pair",
    suite=LC_27_30)
add("lc-27-lease-covers-one-tick", "numbered_overlay/controller.py",
    "            lease_ms = max(\n"
    "                _OVERLAY_LEASE_FLOOR_MS,\n"
    "                int(interval_s * 1000) * _OVERLAY_LEASE_INTERVAL_TICKS,\n",
    "            lease_ms = max(\n"
    "                0,\n"
    "                int(interval_s * 1000),\n",
    "test_lc_27_painted_overlay_renews_its_lease_every_interval",
    "test_lc_30_logic_grants_only_a_bounded_badge_lease",
    suite=LC_27_30)
add("lc-27-snapshot-not-re-put", "numbered_overlay/controller.py",
    "        pinned = machine.pinned_snapshot_id\n"
    "        if pinned:\n"
    "            try:\n"
    "                self._host.create_task_with_error_handling(\n"
    "                    self._overlay_send_refresh(\n",
    "        pinned = machine.pinned_snapshot_id\n"
    "        if False:\n"
    "            try:\n"
    "                self._host.create_task_with_error_handling(\n"
    "                    self._overlay_send_refresh(\n",
    "test_lc_27_painted_overlay_renews_its_lease_every_interval",
    suite=LC_27_30)
add("lc-30-lease-of-forty-ticks", "numbered_overlay/helpers.py",
    "_OVERLAY_LEASE_INTERVAL_TICKS = 4\n",
    "_OVERLAY_LEASE_INTERVAL_TICKS = 40\n",
    "test_lc_30_logic_grants_only_a_bounded_badge_lease",
    suite=LC_27_30)

# LC-28: an expired-lease report clears, unpins, closes, and logs an error.
LC_28 = sel(_LC, "test_lc_28_expired_lease_report_clears_and_closes",
            "test_lc_19_cleared_acknowledgement_changes_nothing")
add("lc-28-expired-report-keeps-painted", "click_overlay_state.py",
    "            if event.paint_state is PaintAckState.EXPIRED:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the GUI's badge lease\n",
    "            if False:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the GUI's badge lease\n",
    "test_lc_28_expired_lease_report_clears_and_closes",
    suite=LC_28)
add("lc-28-expired-report-logged-as-warning", "numbered_overlay/controller.py",
    "                        logger.error(\n"
    "                            \"overlay: the display process expired the badge \"\n"
    "                            \"lease and removed the numbered badges while \"\n",
    "                        logger.warning(\n"
    "                            \"overlay: the display process expired the badge \"\n"
    "                            \"lease and removed the numbered badges while \"\n",
    "test_lc_28_expired_lease_report_clears_and_closes",
    suite=LC_28)

# LC-29: a starting Logic sends reset_overlay to the GUI first.
LC_29 = sel(_LC, "test_lc_29_logic_start_sends_reset_overlay_first",
            "test_lc_1_show_numbers_while_closed_sends_one_walk")
add("lc-29-startup-reset-sends-nothing", "numbered_overlay/controller.py",
    "        self._put_overlay_gui_action(\n"
    "            {\"action\": \"reset_overlay\"}, trace_id=\"logic-startup\",\n"
    "        )\n",
    "        pass\n",
    "test_lc_29_logic_start_sends_reset_overlay_first",
    "test_lc_4_show_and_hide_do_nothing_while_overlay_off",
    suite=LC_29 + sel(_LC, "test_lc_4_show_and_hide_do_nothing_while_overlay_off"))
# EXPECTED SURVIVOR (reason 3): the call site in LogicController.main() is not
# run by any test. main() also starts the STT provider, the WebSocket server
# and the provider launcher, so the harness calls
# controller.overlay.send_overlay_startup_reset() itself in main()'s place
# (tests/overlay_boundary_harness.py, function overlay_boundary). Removing
# the call from main() therefore changes no boundary output in the harness.
add("lc-29-no-startup-reset", "main.py",
    "            self.overlay.send_overlay_startup_reset()\n",
    "            pass\n",
    "test_lc_29_logic_start_sends_reset_overlay_first",
    survives=("Needs LogicController.main() run: the harness calls the startup reset in main()'s place"),
    suite=LC_29)

# LC-37: "hide numbers" in the error state closes the overlay.
LC_37 = sel(_LC, "test_lc_37_hide_numbers_in_error_closes_the_overlay",
            "test_lc_10_show_numbers_in_error_starts_a_new_session")
add("lc-37-error-hide-stays-in-error", "click_overlay_state.py",
    "        if kind in (\n"
    "            OverlayEventKind.HIDE_NUMBERS,\n"
    "            OverlayEventKind.MIC_PAUSE,\n"
    "            OverlayEventKind.MIC_RESUME,\n"
    "        ):\n",
    "        if kind in (\n"
    "            OverlayEventKind.MIC_PAUSE,\n"
    "            OverlayEventKind.MIC_RESUME,\n"
    "        ):\n",
    "test_lc_37_hide_numbers_in_error_closes_the_overlay",
    suite=LC_37)


# ===========================================================================
# CK: click N (tests/test_overlay_requirements_click.py)
# ===========================================================================
# Mutations for tests/test_overlay_requirements_click.py (CK requirements).
# Generated from the worktree sources; every `old` counted byte-exact (count == 1).


CK_1 = sel('test_overlay_requirements_click.py',
    'test_ck_1_click_n_while_closed_is_a_by_name_click',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-1: 'click N' while closed is a by-name click. Mutant: closed routes N through the badge resolver (no pin -> no_badge_numbered notice). Caught by: test_ck_1 at :237 (next_frame click_element).
add('ck-1-closed-resolves-against-badges', 'speech/overlay_click_router.py',
    '    if state is OverlayState.CLOSED:\n'
    '        # The spoken digit is looked up by name; the by-name flow may or may\n'
    '        # not find a control whose accessible name is the digit.\n'
    '        return RoutingDecision(RoutingKind.BY_NAME)\n',
    '    if state is OverlayState.CLOSED:\n'
    '        # The spoken digit is looked up by name; the by-name flow may or may\n'
    '        # not find a control whose accessible name is the digit.\n'
    '        return _resolve_visible(cache, pinned_snapshot_id, parsed_number)\n',
    'test_ck_1_click_n_while_closed_is_a_by_name_click',
    suite=CK_1)

CK_2 = sel('test_overlay_requirements_click.py',
    'test_ck_2_click_text_is_by_name_in_every_state',
    'test_ck_1_click_n_while_closed_is_a_by_name_click',
)
# CK-2: text, or a number with a role word, is by-name in every state. Mutant: 'seven button' is parsed as 7 despite the role. Caught by: test_ck_2[painted-seven button] at :269 (next_frame click_element).
add('ck-2-number-parsed-despite-role', 'numbered_overlay/controller.py',
    '        parsed_number = (\n'
    '            parse_number_word(spoken, aliases=True) if role is None else None\n'
    '        )\n',
    '        parsed_number = (\n'
    '            parse_number_word(spoken, aliases=True)\n'
    '        )\n',
    'test_ck_2_click_text_is_by_name_in_every_state',
    suite=CK_2)
# CK-2 (b). Mutant: in the error state a text click falls to the numbers_not_showing reject. Caught by: test_ck_2[error-*] at :269.
add('ck-2-error-state-rejects-text', 'speech/overlay_click_router.py',
    '    if parsed_number is None:\n'
    '        return RoutingDecision(RoutingKind.BY_NAME)\n',
    '    if parsed_number is None and state is not OverlayState.ERROR:\n'
    '        return RoutingDecision(RoutingKind.BY_NAME)\n',
    'test_ck_2_click_text_is_by_name_in_every_state',
    suite=CK_2)

CK_3 = sel('test_overlay_requirements_click.py',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
    'test_ck_1_click_n_while_closed_is_a_by_name_click',
)
# CK-3: painted + N sends click_snapshot_item for badge N; the default gesture is omitted. Mutant: the default Invoke gesture goes into the payload. Caught by: test_ck_3[click two] at :300.
add('ck-3-default-gesture-sent', 'numbered_overlay/controller.py',
    '        if gesture is not None and gesture != DEFAULT_GESTURE:\n',
    '        if gesture is not None:\n',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
    suite=CK_3)
# CK-3 (b). Mutant: the badge resolver picks badge N+1. Caught by: test_ck_3 at :298 (item_id).
add('ck-3-resolve-off-by-one', 'click_snapshot_summary_cache.py',
    '    for item in result.summary.items:\n'
    '        if item.display_number == display_number:\n',
    '    for item in result.summary.items:\n'
    '        if item.display_number == display_number + 1:\n',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
    suite=CK_3)

CK_4 = sel('test_overlay_requirements_click.py',
    'test_ck_4_click_n_while_painted_without_badge_n_shows_notice',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-4: painted + N with no badge N gives a no_badge_numbered notice, never by-name. Mutant: the miss falls through to by-name. Caught by: test_ck_4 at :322 (next_gui show_click_notice).
add('ck-4-miss-falls-to-by-name', 'speech/overlay_click_router.py',
    '    return RoutingDecision(\n'
    '        RoutingKind.NOTICE,\n'
    '        snapshot_id=visible_snapshot_id,\n'
    '        reason=NO_BADGE_NUMBERED,\n'
    '    )\n',
    '    return RoutingDecision(RoutingKind.BY_NAME)\n',
    'test_ck_4_click_n_while_painted_without_badge_n_shows_notice',
    suite=CK_4)
# CK-4 (b). Mutant: the miss notice carries no snapshot id. Caught by: test_ck_4 at :324.
add('ck-4-miss-notice-drops-snapshot-id', 'speech/overlay_click_router.py',
    '    return RoutingDecision(\n'
    '        RoutingKind.NOTICE,\n'
    '        snapshot_id=visible_snapshot_id,\n'
    '        reason=NO_BADGE_NUMBERED,\n'
    '    )\n',
    '    return RoutingDecision(\n'
    '        RoutingKind.NOTICE,\n'
    '        snapshot_id=None,\n'
    '        reason=NO_BADGE_NUMBERED,\n'
    '    )\n',
    'test_ck_4_click_n_while_painted_without_badge_n_shows_notice',
    suite=CK_4)

CK_5 = sel('test_overlay_requirements_click.py',
    'test_ck_5_click_n_during_refresh_uses_the_visible_snapshot',
    'test_ck_4_click_n_while_painted_without_badge_n_shows_notice',
)
# CK-5: during a refresh, N resolves against the VISIBLE snapshot. Mutant: it resolves against the not-yet-painted current pin. Caught by: test_ck_5[new_list_not_painted] at :360 (badge 5 is found and clicked; no notice).
add('ck-5-refresh-resolves-against-current-pin', 'speech/overlay_click_router.py',
    '        visible = (\n'
    '            prior_pinned_snapshot_id if prior_pin_deferred\n'
    '            else pinned_snapshot_id\n'
    '        )\n',
    '        visible = pinned_snapshot_id\n',
    'test_ck_5_click_n_during_refresh_uses_the_visible_snapshot',
    suite=CK_5)
# CK-5 (b). Mutant: refresh_in_flight holds the click instead of resolving it. Caught by: test_ck_5 at :360 or :361 (no notice in the wait, or numbers_not_showing after the hold).
add('ck-5-refresh-holds', 'speech/overlay_click_router.py',
    '        OverlayState.WALK_IN_FLIGHT,\n'
    '        OverlayState.PAINT_IN_FLIGHT,\n'
    '        OverlayState.PAUSED,\n',
    '        OverlayState.WALK_IN_FLIGHT,\n'
    '        OverlayState.PAINT_IN_FLIGHT,\n'
    '        OverlayState.PAUSED,\n'
    '        OverlayState.REFRESH_IN_FLIGHT,\n',
    'test_ck_5_click_n_during_refresh_uses_the_visible_snapshot',
    suite=CK_5)

CK_7 = sel('test_overlay_requirements_click.py',
    'test_ck_7_held_click_n_resolves_once_the_overlay_paints',
    'test_ck_8_held_click_n_that_cannot_resolve_shows_notice',
)
# CK-7: a held 'click N' clicks once the overlay paints. Mutant: the SNAPSHOT_ITEM arm of the hold resolve does not dispatch and falls to the numbers_not_showing notice. Caught by: test_ck_7 at :398 (next_frame click_snapshot_item).
add('ck-7-held-click-never-dispatched', 'numbered_overlay/controller.py',
    '            self._dispatch_snapshot_item_click(\n'
    '                snapshot_id=decision.snapshot_id,\n'
    '                item_id=decision.item_id,\n'
    '                trace_id=trace_id,\n'
    '                gesture=gesture,\n'
    '            )\n'
    '            return\n',
    "",
    'test_ck_7_held_click_n_resolves_once_the_overlay_paints',
    suite=CK_7)

CK_8 = sel('test_overlay_requirements_click.py',
    'test_ck_8_held_click_n_that_cannot_resolve_shows_notice',
    'test_ck_7_held_click_n_resolves_once_the_overlay_paints',
)
# CK-8: a held 'click N' still unresolvable after 200 ms gives numbers_not_showing. Mutant: wrong reason on the final fallback notice. Caught by: test_ck_8[still_in_flight] at :425.
add('ck-8-unresolved-hold-wrong-reason', 'numbered_overlay/controller.py',
    '            number, decision.kind.value, machine.state.value, trace_id,\n'
    '        )\n'
    '        self.forward_click_notice(\n'
    '            outcome="execution_failed", reason="numbers_not_showing",\n'
    '            matched_name=None, matched_names=(), spoken_name=spoken,\n'
    '            snapshot_id=None, trace_id=trace_id,\n'
    '        )\n',
    '            number, decision.kind.value, machine.state.value, trace_id,\n'
    '        )\n'
    '        self.forward_click_notice(\n'
    '            outcome="execution_failed", reason="no_badge_numbered",\n'
    '            matched_name=None, matched_names=(), spoken_name=spoken,\n'
    '            snapshot_id=None, trace_id=trace_id,\n'
    '        )\n',
    'test_ck_8_held_click_n_that_cannot_resolve_shows_notice',
    suite=CK_8)
# CK-8 (b). Mutant: the final fallback notice is dropped. Caught by: test_ck_8[still_in_flight] at :423 (next_gui show_click_notice).
add('ck-8-unresolved-hold-silent-drop', 'numbered_overlay/controller.py',
    '            number, decision.kind.value, machine.state.value, trace_id,\n'
    '        )\n'
    '        self.forward_click_notice(\n'
    '            outcome="execution_failed", reason="numbers_not_showing",\n'
    '            matched_name=None, matched_names=(), spoken_name=spoken,\n'
    '            snapshot_id=None, trace_id=trace_id,\n'
    '        )\n',
    '            number, decision.kind.value, machine.state.value, trace_id,\n'
    '        )\n',
    'test_ck_8_held_click_n_that_cannot_resolve_shows_notice',
    suite=CK_8)

CK_12 = sel('test_overlay_requirements_click.py',
    'test_ck_12_click_n_in_error_state_shows_notice',
    'test_ck_13_click_n_while_settling_shows_numbers_updating',
)
# CK-12: 'click N' in error gives numbers_not_showing with no snapshot id. Mutant: error resolves N against the pin (no_badge_numbered). Caught by: test_ck_12 at :445.
add('ck-12-error-state-resolves-badges', 'speech/overlay_click_router.py',
    '        return RoutingDecision(\n'
    '            RoutingKind.NOTICE, reason=NUMBERS_NOT_SHOWING,\n'
    '        )\n',
    '        return _resolve_visible(cache, pinned_snapshot_id, parsed_number)\n',
    'test_ck_12_click_n_in_error_state_shows_notice',
    suite=CK_12)

CK_13 = sel('test_overlay_requirements_click.py',
    'test_ck_13_click_n_while_settling_shows_numbers_updating',
    'test_ck_12_click_n_in_error_state_shows_notice',
)
# CK-13: 'click N' while settling gives numbers_updating. Mutant: reason numbers_not_showing. Caught by: test_ck_13 at :481.
add('ck-13-settling-wrong-reason', 'speech/overlay_click_router.py',
    '        return RoutingDecision(RoutingKind.NOTICE, reason=NUMBERS_UPDATING)\n',
    '        return RoutingDecision(RoutingKind.NOTICE, reason=NUMBERS_NOT_SHOWING)\n',
    'test_ck_13_click_n_while_settling_shows_numbers_updating',
    suite=CK_13)

CK_14 = sel('test_overlay_requirements_click.py',
    'test_ck_14_settle_clears_at_once_then_reads_and_repaints',
    'test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins',
)
# CK-14: settle on: clear_overlay at once (at the painted pair), then a re-read. Mutant: the generation is bumped before the clear, so the clear carries a pair the GUI never painted. Caught by: test_ck_14 at :502.
add('ck-14-clear-stamped-after-bump', 'click_overlay_state.py',
    '        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n'
    '        self._bump_generation()\n'
    '        self.state = OverlayState.POST_CLICK_SETTLING\n',
    '        self._bump_generation()\n'
    '        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n'
    '        self.state = OverlayState.POST_CLICK_SETTLING\n',
    'test_ck_14_settle_clears_at_once_then_reads_and_repaints',
    suite=CK_14)
# CK-14 (b). Mutant: settle entry sends no clear. Caught by: test_ck_14 at :501 (next_gui clear_overlay).
add('ck-14-no-clear-on-settle', 'click_overlay_state.py',
    '        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n'
    '        self._bump_generation()\n'
    '        self.state = OverlayState.POST_CLICK_SETTLING\n',
    '        effects: list[Effect] = [self._cancel_timer()]\n'
    '        self._bump_generation()\n'
    '        self.state = OverlayState.POST_CLICK_SETTLING\n',
    'test_ck_14_settle_clears_at_once_then_reads_and_repaints',
    suite=CK_14)
# CK-14 (c). Mutant: settle entry dispatches no re-read build. Caught by: test_ck_14 at :503 (next_frame start_overlay_walk).
add('ck-14-no-settle-read', 'click_overlay_state.py',
    '        # that was on screen when the user clicked.\n'
    '        effects.append(\n'
    '            self._dispatch_build(BuildReason.SETTLE, self.pinned_snapshot_id)\n'
    '        )\n',
    '        # that was on screen when the user clicked.\n',
    'test_ck_14_settle_clears_at_once_then_reads_and_repaints',
    suite=CK_14)

CK_15 = sel('test_overlay_requirements_click.py',
    'test_ck_15_settling_without_repaint_closes_at_the_deadline',
    'test_ck_13_click_n_while_settling_shows_numbers_updating',
)
# CK-15: settling with no repaint closes (unpin, no notice) at the deadline. Mutant: the settle TIMEOUT arm does nothing.
# EXPECTED SURVIVOR (reason 3): the unanswered settle read fails at its own 10 s reply limit (main.py send_request bound) before the 15.25 s settle deadline, and the build-failure arm closes with the same outputs, so the TIMEOUT arm is never reached from the boundary
add('ck-15-settle-timeout-ignored', 'click_overlay_state.py',
    '        if kind is OverlayEventKind.TIMEOUT:\n'
    '            # Nothing ended the gap. Give up cleanly: unpin, close, and let\n'
    '            # the user say "show numbers". The badges are already gone, so\n'
    '            # the only cost is the spoken command.\n'
    '            return self._hide_to_closed()\n',
    '        if kind is OverlayEventKind.TIMEOUT:\n'
    '            return ApplyResult(OverlayOutcome.NO_OP)\n',
    'test_ck_15_settling_without_repaint_closes_at_the_deadline',
    survives=('Needs the settle deadline reached: the settle read fails first at its 10 s reply limit with the same outputs'),
    suite=CK_15)
# CK-15 (b). Mutant: a failed settle read closes through the error path and fires the 'numbers could not be drawn' notice. Caught by: test_ck_15 at :553 (_notices == []). This exercises the read-failure close, which is the close the boundary sees.
add('ck-15-settle-failure-closes-with-notice', 'click_overlay_state.py',
    '                return self._hide_to_closed()\n'
    '            if event.snapshot_id == self.pinned_snapshot_id:\n',
    '                return self._error_to_closed(emit_standalone_notice=True)\n'
    '            if event.snapshot_id == self.pinned_snapshot_id:\n',
    'test_ck_15_settling_without_repaint_closes_at_the_deadline',
    suite=CK_15)

CK_17 = sel('test_overlay_requirements_click.py',
    'test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-17: an ok click with keep-numbers off clears and unpins. Mutant: the keep-numbers test is inverted, so the click re-walks instead. Caught by: test_ck_17 at :571 (next_gui clear_overlay).
add('ck-17-keep-numbers-test-inverted', 'numbered_overlay/controller.py',
    '            click_succeeded\n'
    '            and not click_keep_numbers\n'
    '            and overlay_dispatch_pair[0] == current_pair[0]\n',
    '            click_succeeded\n'
    '            and click_keep_numbers\n'
    '            and overlay_dispatch_pair[0] == current_pair[0]\n',
    'test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins',
    suite=CK_17)
# CK-17 (b). Mutant: the same-session test is inverted. Caught by: test_ck_17 at :571.
add('ck-17-session-compare-inverted', 'numbered_overlay/controller.py',
    '            click_succeeded\n'
    '            and not click_keep_numbers\n'
    '            and overlay_dispatch_pair[0] == current_pair[0]\n',
    '            click_succeeded\n'
    '            and not click_keep_numbers\n'
    '            and overlay_dispatch_pair[0] != current_pair[0]\n',
    'test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins',
    suite=CK_17)

CK_18 = sel('test_overlay_requirements_click.py',
    'test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place',
    'test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins',
)
# CK-18: an ok click with keep-numbers on and settle off re-walks in place. Mutant: the hide branch ignores keep-numbers. Caught by: test_ck_18 at :595 (next_frame start_overlay_walk).
add('ck-18-hide-ignores-keep-numbers', 'numbered_overlay/controller.py',
    '            click_succeeded\n'
    '            and not click_keep_numbers\n'
    '            and overlay_dispatch_pair[0] == current_pair[0]\n',
    '            click_succeeded\n'
    '            and overlay_dispatch_pair[0] == current_pair[0]\n',
    'test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place',
    suite=CK_18)
# CK-18 (b). Mutant: CLICK_COMPLETE settles when the flag is off, clearing the old badges first. Caught by: test_ck_18 at :606.
add('ck-18-settle-flag-inverted', 'click_overlay_state.py',
    '            if self.settle_after_click:\n'
    '                return self._enter_post_click_settling()\n',
    '            if not self.settle_after_click:\n'
    '                return self._enter_post_click_settling()\n',
    'test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place',
    suite=CK_18)

CK_19 = sel('test_overlay_requirements_click.py',
    'test_ck_19_ok_click_shows_no_notice_and_cancels_the_held_click',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
)
# CK-19: an ok reply cancels a held 'click N' at the same pair with numbers_updating. Mutant: the on-time ok path does not cancel the hold. Caught by: test_ck_19 at :633 or :634 (no numbers_updating notice; the hold later fires numbers_not_showing).
add('ck-19-held-click-not-cancelled', 'numbered_overlay/controller.py',
    '            # cannot race the machine transition.\n'
    '            self._cancel_held_click_after_success(\n'
    '                overlay_dispatch_pair=overlay_dispatch_pair,\n'
    '                trace_id=trace_id,\n'
    '            )\n'
    '            self._feed_click_complete_refresh(\n',
    '            # cannot race the machine transition.\n'
    '            self._feed_click_complete_refresh(\n',
    'test_ck_19_ok_click_shows_no_notice_and_cancels_the_held_click',
    suite=CK_19)
# CK-19 (b). Mutant: the cancelled hold's notice has the wrong reason. Caught by: test_ck_19 at :634.
add('ck-19-cancel-wrong-reason', 'numbered_overlay/controller.py',
    '        )\n'
    '        self.forward_click_notice(\n'
    '            outcome="execution_failed", reason="numbers_updating",\n',
    '        )\n'
    '        self.forward_click_notice(\n'
    '            outcome="execution_failed", reason="numbers_not_showing",\n',
    'test_ck_19_ok_click_shows_no_notice_and_cancels_the_held_click',
    suite=CK_19)

CK_20 = sel('test_overlay_requirements_click.py',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
    'test_ck_24_verification_timeout_refusal_starts_no_rewalk',
)
# CK-20: a refused reply becomes a notice; the snapshot id falls back to the dispatched one. Mutant: no fallback. Caught by: test_ck_20 at :666.
add('ck-20-no-snapshot-fallback', 'numbered_overlay/helpers.py',
    '        matched_names=response.matched_names,\n'
    '        spoken_name=spoken_name,\n'
    '        snapshot_id=response.snapshot_id or fallback_snapshot_id,\n',
    '        matched_names=response.matched_names,\n'
    '        spoken_name=spoken_name,\n'
    '        snapshot_id=response.snapshot_id,\n',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
    suite=CK_20)
# CK-20 (b). Mutant: matched names dropped from the notice. Caught by: test_ck_20 at :664.
add('ck-20-matched-names-dropped', 'numbered_overlay/helpers.py',
    '        matched_names=response.matched_names,\n'
    '        spoken_name=spoken_name,\n'
    '        snapshot_id=response.snapshot_id or fallback_snapshot_id,\n',
    '        matched_names=(),\n'
    '        spoken_name=spoken_name,\n'
    '        snapshot_id=response.snapshot_id or fallback_snapshot_id,\n',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
    suite=CK_20)

CK_21_22 = sel('test_overlay_requirements_click.py',
    'test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints',
    'test_ck_24_verification_timeout_refusal_starts_no_rewalk',
)
# CK-21: a bounds_invalid or bounds_stale refusal re-walks and repaints. Mutant: bounds_invalid leaves the re-walk set. Caught by: test_ck_21_ck_22[CK-21-bounds_invalid] at :700 (next_frame start_overlay_walk).
add('ck-21-bounds-invalid-no-rewalk', 'numbered_overlay/helpers.py',
    '    {\n'
    '        "bounds_invalid",\n',
    '    {\n',
    'test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints',
    suite=CK_21_22)
# CK-21 (b). Mutant: bounds_stale leaves the re-walk set. Caught by: test_ck_21_ck_22[CK-21-bounds_stale] at :700.
add('ck-21-bounds-stale-no-rewalk', 'numbered_overlay/helpers.py',
    '        "bounds_invalid",\n'
    '        "bounds_stale",\n'
    '        "target_moved_offscreen",\n',
    '        "bounds_invalid",\n'
    '        "target_moved_offscreen",\n',
    'test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints',
    suite=CK_21_22)
# CK-22: a stale_overlay_generation refusal re-walks. Mutant: it leaves the re-walk set. Caught by: test_ck_21_ck_22[CK-22] at :700.
add('ck-22-stale-generation-no-rewalk', 'numbered_overlay/helpers.py',
    '        "stale_overlay_generation",\n'
    '    }\n',
    '    }\n',
    'test_ck_21_ck_22_stale_position_refusal_rewalks_and_repaints',
    suite=CK_21_22)

CK_24 = sel('test_overlay_requirements_click.py',
    'test_ck_24_verification_timeout_refusal_starts_no_rewalk',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
)
# CK-24: a verification_timeout refusal starts no re-walk. Mutant: verification_timeout joins the re-walk set. Caught by: test_ck_24 at :731.
add('ck-24-verification-timeout-rewalks', 'numbered_overlay/helpers.py',
    '        "stale_overlay_generation",\n'
    '    }\n',
    '        "stale_overlay_generation",\n'
    '        "verification_timeout",\n'
    '    }\n',
    'test_ck_24_verification_timeout_refusal_starts_no_rewalk',
    suite=CK_24)

CK_25 = sel('test_overlay_requirements_click.py',
    'test_ck_25_unanswered_badge_click_shows_timeout_notice',
    'test_ck_20_refused_badge_click_shows_the_reply_as_a_notice',
)
# CK-25: no reply in time gives an execution_failed:timeout notice. Mutant: the timeout notice loses the snapshot id. Caught by: test_ck_25 at :752.
add('ck-25-timeout-notice-no-snapshot', 'numbered_overlay/controller.py',
    '            self.forward_click_notice(\n'
    '                outcome="execution_failed", reason="timeout",\n'
    '                matched_name=None, matched_names=(), spoken_name="",\n'
    '                snapshot_id=snapshot_id, trace_id=trace_id,\n'
    '            )\n'
    '            return\n',
    '            self.forward_click_notice(\n'
    '                outcome="execution_failed", reason="timeout",\n'
    '                matched_name=None, matched_names=(), spoken_name="",\n'
    '                snapshot_id=None, trace_id=trace_id,\n'
    '            )\n'
    '            return\n',
    'test_ck_25_unanswered_badge_click_shows_timeout_notice',
    suite=CK_25)
# CK-25 (b). Mutant: the timeout shows no notice. Caught by: test_ck_25 at :749 (next_gui show_click_notice).
add('ck-25-timeout-silent', 'numbered_overlay/controller.py',
    '            self.forward_click_notice(\n'
    '                outcome="execution_failed", reason="timeout",\n'
    '                matched_name=None, matched_names=(), spoken_name="",\n'
    '                snapshot_id=snapshot_id, trace_id=trace_id,\n'
    '            )\n'
    '            return\n',
    '            return\n',
    'test_ck_25_unanswered_badge_click_shows_timeout_notice',
    suite=CK_25)

CK_27 = sel('test_overlay_requirements_click.py',
    'test_ck_27_late_refusal_corrects_the_timeout_notice',
    'test_ck_25_unanswered_badge_click_shows_timeout_notice',
)
# CK-27: a late refusal corrects the timeout notice when no newer notice exists. Mutant: the staleness gate is inverted, so the click's own correction is suppressed. Caught by: test_ck_27 at :775 (next_gui show_click_notice).
add('ck-27-staleness-gate-inverted', 'numbered_overlay/helpers.py',
    '    if trace_id != last_attempted and trace_id != last_forwarded:\n',
    '    if trace_id == last_attempted or trace_id == last_forwarded:\n',
    'test_ck_27_late_refusal_corrects_the_timeout_notice',
    suite=CK_27)
# CK-27 (b). Mutant: the late callback of a badge click passes no fallback snapshot. Caught by: test_ck_27 at :778.
add('ck-27-late-fallback-snapshot-dropped', 'numbered_overlay/controller.py',
    '                action="click_snapshot_item",\n'
    '                spoken_name="",\n'
    '                fallback_snapshot_id=snapshot_id,\n',
    '                action="click_snapshot_item",\n'
    '                spoken_name="",\n'
    '                fallback_snapshot_id=None,\n',
    'test_ck_27_late_refusal_corrects_the_timeout_notice',
    suite=CK_27)

CK_29 = sel('test_overlay_requirements_click.py',
    'test_ck_29_late_ok_reply_shows_no_notice',
    'test_ck_27_late_refusal_corrects_the_timeout_notice',
)
# CK-29: a late ok reply shows no notice. Mutant: the late ok branch does not return, so the reply is forwarded as a corrected notice (outcome ok; the ClickNoticeEvent constructor does not check outcome). Caught by: test_ck_29 at :796.
add('ck-29-late-ok-falls-through', 'numbered_overlay/helpers.py',
    '        return\n'
    '    # Badge path (wh-overlay-slow-uia-stale-badges.20.3): mirror the on-time\n',
    '    # Badge path (wh-overlay-slow-uia-stale-badges.20.3): mirror the on-time\n',
    'test_ck_29_late_ok_reply_shows_no_notice',
    suite=CK_29)

CK_31 = sel('test_overlay_requirements_click.py',
    'test_ck_31_second_badge_click_while_first_in_flight_is_refused',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-31: a badge click while one is in flight is refused with overlay_click_in_flight. Mutant: the guard refuses only the same item. Caught by: test_ck_31 at :812 (next_gui show_click_notice).
add('ck-31-in-flight-guard-same-item-only', 'numbered_overlay/controller.py',
    '        in_flight = getattr(self, "_overlay_click_in_flight", None)\n'
    '        if in_flight is not None:\n',
    '        in_flight = getattr(self, "_overlay_click_in_flight", None)\n'
    '        if in_flight is not None and in_flight[1] == item_id:\n',
    'test_ck_31_second_badge_click_while_first_in_flight_is_refused',
    suite=CK_31)
# CK-31 (b). Mutant: the in-flight marker is never set. Caught by: test_ck_31 at :812.
add('ck-31-in-flight-marker-not-set', 'numbered_overlay/controller.py',
    '        self._overlay_click_in_flight = (snapshot_id, item_id, trace_id)\n'
    '        try:\n',
    '        try:\n',
    'test_ck_31_second_badge_click_while_first_in_flight_is_refused',
    suite=CK_31)

CK_32 = sel('test_overlay_requirements_click.py',
    'test_ck_32_badge_click_carries_the_visible_pair',
    'test_ck_32_badge_click_omits_an_unknown_pair',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-32: the click carries the visible pair; both fields are omitted when unknown. Mutant: the machine's current pair is sent instead of the visible painted pair. Caught by: test_ck_32_badge_click_omits_an_unknown_pair at :855.
add('ck-32-resolved-pair-is-machine-pair', 'numbered_overlay/controller.py',
    '        overlay_resolved_pair = getattr(\n'
    '            self, "_overlay_visible_painted_pair", None\n'
    '        )\n',
    '        overlay_resolved_pair = overlay_dispatch_pair\n',
    'test_ck_32_badge_click_omits_an_unknown_pair',
    suite=CK_32)
# CK-32 (b). Mutant: the recorded visible pair is one generation ahead of the paint. Caught by: test_ck_32_badge_click_carries_the_visible_pair at :832.
add('ck-32-visible-pair-off-by-one', 'numbered_overlay/controller.py',
    '            self._overlay_visible_painted_pair = (\n'
    '                machine.overlay_session_id, machine.paint_generation,\n'
    '            )\n',
    '            self._overlay_visible_painted_pair = (\n'
    '                machine.overlay_session_id, machine.paint_generation + 1,\n'
    '            )\n',
    'test_ck_32_badge_click_carries_the_visible_pair',
    suite=CK_32)

CK_34 = sel('test_overlay_requirements_click.py',
    'test_ck_34_click_n_after_a_renumbering_repaint_is_refused',
    'test_ck_35_click_n_after_a_repaint_that_kept_badge_n_clicks',
    'test_ck_35_click_n_new_after_a_repaint_clicks',
)
# CK-34: 'click N' after a repaint that renumbered badge N is refused with overlay_numbers_changed. Mutant: the 3 s grace test is inverted, so a fresh swap is pruned. Caught by: test_ck_34 at :891 (next_gui show_click_notice).
add('ck-34-grace-window-inverted', 'numbered_overlay/controller.py',
    '        live = [\n'
    '            entry for entry in swaps\n'
    '            if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n',
    '        live = [\n'
    '            entry for entry in swaps\n'
    '            if now - entry[1] > _OVERLAY_RENUMBER_GRACE_SECONDS\n',
    'test_ck_34_click_n_after_a_renumbering_repaint_is_refused',
    suite=CK_34)
# CK-34 (b). Mutant: the renumber guard ignores the accessible name. Caught by: test_ck_34 at :891.
add('ck-34-name-not-compared', 'speech/overlay_click_router.py',
    '        prior.name.strip().casefold() == current.name.strip().casefold()\n'
    '        and prior.role == current.role\n',
    '        prior.role == current.role\n',
    'test_ck_34_click_n_after_a_renumbering_repaint_is_refused',
    suite=CK_34)

CK_35 = sel('test_overlay_requirements_click.py',
    'test_ck_35_click_n_after_a_repaint_that_kept_badge_n_clicks',
    'test_ck_35_click_n_new_after_a_repaint_clicks',
    'test_ck_34_click_n_after_a_renumbering_repaint_is_refused',
)
# CK-35: after a repaint, a number that kept its badge, or is new, clicks. Mutant: a number absent from the prior list is refused. Caught by: test_ck_35_click_n_new_after_a_repaint_clicks at :932.
add('ck-35-new-number-refused', 'speech/overlay_click_router.py',
    '    prior = _item_at(prior_summary)\n'
    '    if prior is None:\n'
    '        return True\n',
    '    prior = _item_at(prior_summary)\n'
    '    if prior is None:\n'
    '        return False\n',
    'test_ck_35_click_n_new_after_a_repaint_clicks',
    suite=CK_35)
# CK-35 (b). Mutant: badge identity is compared by item_id (different in every snapshot), so an unchanged badge is refused. Caught by: test_ck_35_click_n_after_a_repaint_that_kept_badge_n_clicks at :909.
add('ck-35-identity-by-item-id', 'speech/overlay_click_router.py',
    '        prior.name.strip().casefold() == current.name.strip().casefold()\n',
    '        prior.item_id == current.item_id\n',
    'test_ck_35_click_n_after_a_repaint_that_kept_badge_n_clicks',
    suite=CK_35)

CK_37 = sel('test_overlay_requirements_click.py',
    'test_ck_37_gui_badge_click_sends_click_snapshot_item',
    'test_ck_3_click_n_while_painted_sends_click_snapshot_item',
)
# CK-37: a GUI badge click sends click_snapshot_item for that badge. Mutant: the GUI display number is treated as 0-based. Caught by: test_ck_37 at :957.
add('ck-37-gui-display-number-off-by-one', 'numbered_overlay/controller.py',
    '            result = resolve_display_number(\n'
    '                self.click_snapshot_summary_cache,\n'
    '                ev.snapshot_id,\n'
    '                ev.display_number,\n',
    '            result = resolve_display_number(\n'
    '                self.click_snapshot_summary_cache,\n'
    '                ev.snapshot_id,\n'
    '                ev.display_number - 1,\n',
    'test_ck_37_gui_badge_click_sends_click_snapshot_item',
    suite=CK_37)

CK_41 = sel('test_overlay_requirements_click.py',
    'test_ck_41_click_disabled_sends_no_click_and_types_or_notices',
    'test_ck_42_by_name_miss_without_safety_word_types_the_words',
)
# CK-41: clicking disabled sends no click; without the safety word the words are typed. Mutant: the gate checks only an invalid key, so enabled=false clicks anyway. Caught by: test_ck_41_click_disabled_sends_no_click_and_types_or_notices at :990 (nothing typed).
add('ck-41-gate-on-invalid-key-only', 'numbered_overlay/controller.py',
    '        if not self._host.click_config.enabled:\n'
    '            if defer_failure_notice:\n',
    '        if self._host.click_config.invalid_key is not None:\n'
    '            if defer_failure_notice:\n',
    'test_ck_41_click_disabled_sends_no_click_and_types_or_notices',
    suite=CK_41)
# CK-41 (b). Mutant: the disabled branch shows the notice itself instead of returning it to the caller. Caught by: test_ck_41_click_disabled_sends_no_click_and_types_or_notices at :990.
add('ck-41-deferral-inverted', 'numbered_overlay/controller.py',
    '        if not self._host.click_config.enabled:\n'
    '            if defer_failure_notice:\n',
    '        if not self._host.click_config.enabled:\n'
    '            if not defer_failure_notice:\n',
    'test_ck_41_click_disabled_sends_no_click_and_types_or_notices',
    suite=CK_41)
# CK-41 (c), once per session on the deferred path (dev fix 9efe63ba, wh-click-off-notice-once). Mutant: the deferred notice ignores the once-per-session flag. Caught by: test_ck_41_disabled_notice_shows_once_per_session (two notices).
add('ck-41-deferred-notice-every-time', 'speech/actions.py',
    '                if not lc.take_click_disabled_notice():\n',
    '                if False:\n',
    'test_ck_41_disabled_notice_shows_once_per_session',
    'test_ao_9_safety_word_click_disabled_notice_shows_once',
    suite=sel('test_overlay_requirements_click.py',
              'test_ck_41_disabled_notice_shows_once_per_session',
              'test_ck_41_click_disabled_sends_no_click_and_types_or_notices')
    + sel('test_overlay_requirements_autoopen.py',
          'test_ao_9_safety_word_click_disabled_notice_shows_once'))

CK_42 = sel('test_overlay_requirements_click.py',
    'test_ck_42_by_name_miss_without_safety_word_types_the_words',
    'test_ck_43_by_name_miss_with_safety_word_shows_the_notice',
)
# CK-42: a by-name miss without the safety word types the words and shows no notice. Mutant: not_found is no longer deferred to the caller. Caught by: test_ck_42[not_found-None] at :1052.
add('ck-42-not-found-not-deferred', 'numbered_overlay/controller.py',
    '        if defer_failure_notice and (\n'
    '            response.outcome == "not_found"\n'
    '            or (\n'
    '                response.outcome == "execution_failed"\n'
    '                and response.reason == "walk_deadline_exceeded"\n'
    '            )\n'
    '        ):\n',
    '        if defer_failure_notice and (\n'
    '            (\n'
    '                response.outcome == "execution_failed"\n'
    '                and response.reason == "walk_deadline_exceeded"\n'
    '            )\n'
    '        ):\n',
    'test_ck_42_by_name_miss_without_safety_word_types_the_words',
    suite=CK_42)
# CK-42 (b). Mutant: the speech processor shows the notice without the safety word and types nothing. Caught by: test_ck_42 at :1052.
add('ck-42-notice-without-safety-word', 'speech/speech_processor.py',
    '            if hotword_authorized and isinstance(notice, str) and notice:\n',
    '            if isinstance(notice, str) and notice:\n',
    'test_ck_42_by_name_miss_without_safety_word_types_the_words',
    suite=CK_42)

CK_43 = sel('test_overlay_requirements_click.py',
    'test_ck_43_by_name_miss_with_safety_word_shows_the_notice',
    'test_ck_42_by_name_miss_without_safety_word_types_the_words',
)
# CK-43: a by-name miss with the safety word shows the notice and types nothing. Mutant: the deferred notice's show() never forwards it. Caught by: test_ck_43 at :1076 (next_gui show_click_notice).
add('ck-43-deferred-notice-not-shown', 'speech/actions.py',
    '            lc.forward_click_notice(**kwargs)\n'
    '\n'
    '        return StepFailed(text, show=show)\n',
    '            return None\n'
    '\n'
    '        return StepFailed(text, show=show)\n',
    'test_ck_43_by_name_miss_with_safety_word_shows_the_notice',
    suite=CK_43)

CK_47 = sel('test_overlay_requirements_click.py',
    'test_ck_47_number_words_and_homophones_route_as_numbers',
    'test_ck_47_ordinals_and_out_of_range_numbers_are_by_name',
)
# CK-47: number words, homophones and digits route as numbers; ordinals and out-of-range numbers are by-name. Mutant: homophone aliases off. Caught by: test_ck_47_number_words_and_homophones_route_as_numbers[to/too/for] at :1106.
add('ck-47-no-homophone-aliases', 'numbered_overlay/controller.py',
    '        parsed_number = (\n'
    '            parse_number_word(spoken, aliases=True) if role is None else None\n'
    '        )\n',
    '        parsed_number = (\n'
    '            parse_number_word(spoken, aliases=False) if role is None else None\n'
    '        )\n',
    'test_ck_47_number_words_and_homophones_route_as_numbers',
    suite=CK_47)
# CK-47 (b). Mutant: a digit string skips the 1..999 range check. Caught by: test_ck_47_ordinals_and_out_of_range_numbers_are_by_name[1000] at :1125.
add('ck-47-digit-range-unchecked', 'speech/number_word_parser.py',
    '        if len(stripped) > _MAX_DIGITS:\n'
    '            return None\n'
    '        return _in_range(int(stripped))\n',
    '        return int(stripped)\n',
    'test_ck_47_ordinals_and_out_of_range_numbers_are_by_name',
    suite=CK_47)

CK_48 = sel('test_overlay_requirements_click.py',
    'test_ck_48_bare_number_while_grid_open_belongs_to_the_grid',
    'test_ck_47_number_words_and_homophones_route_as_numbers',
)
# CK-48: a bare number while the grid is open belongs to the grid. Mutant: the grid claims only numbers WITH a role word. Caught by: test_ck_48 at :1151 (next_frame click_point).
add('ck-48-grid-claim-role-inverted', 'numbered_overlay/controller.py',
    '            and grid_machine.is_open\n'
    '            and role is None\n'
    '            and parsed_number is not None\n'
    '            and 1 <= parsed_number <= GRID_CELL_COUNT\n',
    '            and grid_machine.is_open\n'
    '            and role is not None\n'
    '            and parsed_number is not None\n'
    '            and 1 <= parsed_number <= GRID_CELL_COUNT\n',
    'test_ck_48_bare_number_while_grid_open_belongs_to_the_grid',
    suite=CK_48)


# ===========================================================================
# RW: re-walk (tests/test_overlay_requirements_rewalk.py)
# ===========================================================================
# Draft mutations for tests/test_overlay_requirements_rewalk.py (prefix RW).
# Uses the gate helpers sel(...) and add(...). Test line numbers refer to
# services/wheelhouse/tests/test_overlay_requirements_rewalk.py at 954c6b50.
_T = "test_overlay_requirements_rewalk.py"

# ---------------------------------------------------------------------------
# RW-1
# ---------------------------------------------------------------------------
RW_1 = sel(_T,
    "test_rw_1_foreground_change_while_painted_refreshes",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear")
# RW-1: painted + foreground (not a dismissal) -> REFRESH, old badges stay.
# Mutant: painted FOCUS_CHANGE restarts (clear + unpin) instead of refreshing.
# Caught by: test_rw_1 line 281 (_clears_before(...) == [] -- a clear_overlay
# for snap-1 precedes paint2).
add("rw-1-painted-focus-restarts-instead-of-refresh", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.REFRESH)\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n",
    "test_rw_1_foreground_change_while_painted_refreshes",
    suite=RW_1)

# ---------------------------------------------------------------------------
# RW-2
# ---------------------------------------------------------------------------
RW_2 = sel(_T,
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    "test_rw_2_foreground_during_first_paint_clears_unpins_and_restarts",
    "test_rw_1_foreground_change_while_painted_refreshes")
# RW-2: restart clears only if a paint was dispatched. Mutant: _restart_walk
# never clears. Caught by: test_rw_2_..._first_paint line 330
# (next_gui("clear_overlay") raises AssertionError: no clear sent).
add("rw-2-restart-drops-clear-of-dispatched-paint", "click_overlay_state.py",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        clear = self._clear_if_visible()\n"
    "        if clear is not None:\n"
    "            effects.append(clear)\n"
    "        unpin = self._unpin_current()\n",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        unpin = self._unpin_current()\n",
    "test_rw_2_foreground_during_first_paint_clears_unpins_and_restarts",
    suite=RW_2)
# Mutant: _restart_walk clears unconditionally (drops the "a paint was
# dispatched" gate). Caught by: test_rw_2_..._first_walk line 309
# ("clear_overlay" not in gui).
add("rw-2-restart-clears-without-dispatched-paint", "click_overlay_state.py",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        clear = self._clear_if_visible()\n"
    "        if clear is not None:\n"
    "            effects.append(clear)\n"
    "        unpin = self._unpin_current()\n",
    "        effects: list[Effect] = [self._cancel_timer()]\n"
    "        clear = self._dispatch_clear()\n"
    "        if clear is not None:\n"
    "            effects.append(clear)\n"
    "        unpin = self._unpin_current()\n",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    suite=RW_2)
# Mutant: walk_in_flight ignores FOCUS_CHANGE (no supersede walk). Caught by:
# test_rw_2_..._first_walk line 304 (next_frame("start_overlay_walk",
# timeout_s=2.0) raises AssertionError).
add("rw-2-walk-in-flight-focus-change-ignored", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Nothing painted yet; just arm the auto-hide flag. Stay\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Nothing painted yet; just arm the auto-hide flag. Stay\n",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    suite=RW_2)

# ---------------------------------------------------------------------------
# RW-3
# ---------------------------------------------------------------------------
RW_3 = sel(_T,
    "test_rw_3_trigger_during_refresh_supersedes_and_keeps_old_badges",
    "test_rw_1_foreground_change_while_painted_refreshes")
# RW-3: refresh_in_flight + trigger -> supersede, old badges stay pinned.
# Mutant: refresh_in_flight FOCUS_CHANGE restarts (clears and unpins snap-1).
# Caught by: test_rw_3 line 371 (_clears_before(...) == []), all four params.
add("rw-3-refresh-in-flight-focus-restarts", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh_supersede(BuildReason.SUPERSEDE)\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n",
    "test_rw_3_trigger_during_refresh_supersedes_and_keeps_old_badges",
    suite=RW_3)

# ---------------------------------------------------------------------------
# RW-4
# ---------------------------------------------------------------------------
RW_4 = sel(_T,
    "test_rw_4_trigger_in_idle_states_sends_nothing",
    "test_rw_21_tree_change_in_idle_states_is_dropped")
# RW-4: closed + trigger -> nothing. Mutant: closed FOCUS_CHANGE starts a
# session walk. Caught by: test_rw_4 [closed-*] line 410 (frames == []).
# test_rw_21 stays green: decide_tree_change drops the tree event in closed.
add("rw-4-closed-focus-change-starts-walk", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            # record-only: there is no overlay to follow.\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            # record-only: there is no overlay to follow.\n"
    "            self._start_session()\n"
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            return ApplyResult(\n"
    "                OverlayOutcome.ACCEPTED,\n"
    "                (\n"
    "                    self._dispatch_build(BuildReason.SUPERSEDE),\n"
    "                    self._arm_timer(OverlayState.WALK_IN_FLIGHT),\n"
    "                ),\n"
    "            )\n",
    "test_rw_4_trigger_in_idle_states_sends_nothing",
    suite=RW_4)
# Mutant: post_click_settling FOCUS_CHANGE refreshes (cancels the settle
# read). Caught by: test_rw_4 [post_click_settling-*] line 410 (frames == [];
# a start_overlay_walk goes out after the abort wakes the parked settle send).
add("rw-4-post-click-settling-focus-change-refreshes", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            # A click that opens a dialog CHANGES the foreground window, and\n"
    "            # that is the common case for \"click N\". Cancelling here would\n"
    "            # make it behave like Option A, which David rejected. The machine\n"
    "            # stays; the integration picks the window to read and revalidates\n"
    "            # its identity before it paints (child .2).\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._refresh(BuildReason.REFRESH)\n",
    "test_rw_4_trigger_in_idle_states_sends_nothing",
    suite=RW_4)
# Mutant: error FOCUS_CHANGE restarts a walk. Caught by: test_rw_4 [error-*]
# line 410 (frames == []).
add("rw-4-error-focus-change-restarts-walk", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n"
    "        if kind in _GENERATION_BEARING:\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind in _GENERATION_BEARING:\n",
    "test_rw_4_trigger_in_idle_states_sends_nothing",
    suite=RW_4)

# ---------------------------------------------------------------------------
# RW-5
# ---------------------------------------------------------------------------
RW_5 = sel(_T,
    "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
    "test_rw_6_non_window_change_with_keep_off_refreshes")
# RW-5: keep off + other top-level window -> clear + unpin, no walk.
# Mutant: the window-change check never reports a change of window.
# Caught by: test_rw_5 line 442 (next_gui("clear_overlay") raises
# AssertionError; a refresh walk goes out instead), every param.
add("rw-5-window-change-never-dismisses", "numbered_overlay/controller.py",
    "            if facts.pid in self._host.wheelhouse_process_ids():\n"
    "                return False\n"
    "            return True\n",
    "            if facts.pid in self._host.wheelhouse_process_ids():\n"
    "                return False\n"
    "            return False\n",
    "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
    suite=RW_5)
# Mutant: a window of the numbered process counts as the same window (breaks
# ruling R-B, the owned dialog). Caught by: test_rw_5 [*-owned-dialog]
# line 442 (next_gui("clear_overlay") raises AssertionError).
add("rw-5-owned-dialog-counted-as-same-window", "numbered_overlay/controller.py",
    "                if facts.root == numbered_root:\n"
    "                    return False\n",
    "                if facts.root == numbered_root or facts.pid == tracked.pid:\n"
    "                    return False\n",
    "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
    suite=RW_5)
# Mutant: numbers count as dismissable only in painted, not in
# refresh_in_flight. Caught by: test_rw_5 [refresh_in_flight-*] line 442
# (next_gui("clear_overlay") raises AssertionError; supersede walk instead).
add("rw-5-refresh-in-flight-not-dismissable", "numbered_overlay/controller.py",
    "        return machine.state in (\n"
    "            OverlayState.PAINTED, OverlayState.REFRESH_IN_FLIGHT,\n"
    "        )\n",
    "        return machine.state in (\n"
    "            OverlayState.PAINTED,\n"
    "        )\n",
    "test_rw_5_change_of_window_with_keep_off_clears_and_unpins",
    suite=RW_5)

# ---------------------------------------------------------------------------
# RW-6
# ---------------------------------------------------------------------------
RW_6 = sel(_T,
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    "test_rw_5_change_of_window_with_keep_off_clears_and_unpins")
# RW-6: tooltip / numbered / child / WheelHouse / unreadable -> refresh.
# Each mutant makes one of them a dismissal: clear + unpin + closed, no walk.
# Caught by: test_rw_6 line 481 (next_frame("start_overlay_walk") raises
# AssertionError) for the named param.
# Mutant: tooltip class not exempt. Param: tooltip.
add("rw-6-tooltip-counts-as-window-change", "numbered_overlay/controller.py",
    "            if facts.class_name.lower() in _OVERLAY_NON_WINDOW_CHANGE_CLASSES:\n"
    "                return False\n",
    "            if facts.class_name.lower() in _OVERLAY_NON_WINDOW_CHANGE_CLASSES and False:\n"
    "                return False\n",
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    suite=RW_6)
# Mutant: tracked-window comparison inverted, so the numbered window and a
# child inside it are not recognised. Params: numbered, child.
add("rw-6-numbered-window-not-recognised", "numbered_overlay/controller.py",
    "            tracked = getattr(self, \"_overlay_tracked_identity\", None)\n"
    "            if tracked is not None:\n"
    "                if int(hwnd) == int(tracked.hwnd):\n",
    "            tracked = getattr(self, \"_overlay_tracked_identity\", None)\n"
    "            if tracked is None:\n"
    "                if int(hwnd) == int(tracked.hwnd):\n",
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    suite=RW_6)
# Mutant: a window inside the numbered window (same GA_ROOT) is not exempt.
# Param: child (numbered still exempt through the exact-handle check).
add("rw-6-child-of-numbered-window-dismisses", "numbered_overlay/controller.py",
    "                if facts.root == numbered_root:\n"
    "                    return False\n",
    "                if facts.root == numbered_root and False:\n"
    "                    return False\n",
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    suite=RW_6)
# Mutant: WheelHouse process windows not exempt. Param: wheelhouse.
add("rw-6-wheelhouse-window-dismisses", "numbered_overlay/controller.py",
    "            if facts.pid in self._host.wheelhouse_process_ids():\n"
    "                return False\n"
    "            return True\n",
    "            return True\n",
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    suite=RW_6)
# Mutant: an unreadable window is treated as a change of window.
# Param: unreadable.
add("rw-6-unreadable-window-dismisses", "numbered_overlay/controller.py",
    "            facts = _overlay_window_facts(hwnd)\n"
    "            if facts is None:\n"
    "                return False\n",
    "            facts = _overlay_window_facts(hwnd)\n"
    "            if facts is None:\n"
    "                return True\n",
    "test_rw_6_non_window_change_with_keep_off_refreshes",
    suite=RW_6)

# ---------------------------------------------------------------------------
# RW-7
# ---------------------------------------------------------------------------
RW_7 = sel(_T,
    "test_rw_7_other_window_during_first_build_restarts_not_dismisses",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    "test_rw_2_foreground_during_first_paint_clears_unpins_and_restarts")
# RW-7: a foreground event during the first walk or paint restarts, never
# dismisses. Mutant: walk_in_flight FOCUS_CHANGE closes the session.
# Caught by: test_rw_7 [walk_in_flight] line 509 and test_rw_2_..._first_walk
# line 304 (next_frame("start_overlay_walk", timeout_s=2.0) raises).
add("rw-7-walk-in-flight-focus-change-dismisses", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Nothing painted yet; just arm the auto-hide flag. Stay\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Nothing painted yet; just arm the auto-hide flag. Stay\n",
    "test_rw_7_other_window_during_first_build_restarts_not_dismisses",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    suite=RW_7)
# Mutant: paint_in_flight FOCUS_CHANGE closes the session. Caught by:
# test_rw_7 [paint_in_flight] line 509 and test_rw_2_..._first_paint line 334
# (next_frame("start_overlay_walk") raises; clear and unpin do go out).
add("rw-7-paint-in-flight-focus-change-dismisses", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._restart_walk(BuildReason.SUPERSEDE)\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Hide the (about-to-be-) overlay, set the flag, stay\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.HIDE_NUMBERS:\n"
    "            return self._hide_to_closed()\n"
    "        if kind is OverlayEventKind.MIC_PAUSE:\n"
    "            # Hide the (about-to-be-) overlay, set the flag, stay\n",
    "test_rw_7_other_window_during_first_build_restarts_not_dismisses",
    "test_rw_2_foreground_during_first_paint_clears_unpins_and_restarts",
    suite=RW_7)

# ---------------------------------------------------------------------------
# RW-8
# ---------------------------------------------------------------------------
RW_8 = sel(_T,
    "test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk",
    "test_rw_1_foreground_change_while_painted_refreshes")
# RW-8: events inside the debounce window send nothing now. Mutant: the
# debounce comparison is inverted, so an event inside the window fires.
# Caught by: test_rw_8 line 545 (frames == []).
add("rw-8-debounce-comparison-inverted", "overlay_focus_hooks.py",
    "        if now_ms - self._last_fired_ms >= self.debounce_ms:\n",
    "        if now_ms - self._last_fired_ms <= self.debounce_ms:\n",
    "test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk",
    suite=RW_8)
# Mutant: a coalesced foreground event arms no trailing re-walk. Caught by:
# test_rw_8 line 547 (next_frame("start_overlay_walk", timeout_s=3.0) raises).
add("rw-8-coalesced-foreground-arms-no-trailing-walk", "numbered_overlay/controller.py",
    "            if self._overlay_numbers_can_be_dismissed():\n"
    "                self._overlay_settle_refire_foreground = True\n"
    "            self._arm_overlay_settle_refire(\"foreground change\")\n"
    "            return\n",
    "            if self._overlay_numbers_can_be_dismissed():\n"
    "                self._overlay_settle_refire_foreground = True\n"
    "            return\n",
    "test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk",
    suite=RW_8)

# ---------------------------------------------------------------------------
# RW-12 and RW-13 (one shared selection)
# ---------------------------------------------------------------------------
RW_12_13 = sel(_T,
    "test_rw_12_burst_ending_on_another_app_clears_instead_of_walking",
    "test_rw_13_burst_ending_on_numbered_window_refreshes")
# RW-12: a coalesced burst ending on another app clears. Mutant: the coalesced
# foreground event is not remembered, so the trailing re-walk walks.
# Caught by: test_rw_12 line 577 (next_gui("clear_overlay", timeout_s=3.0)
# raises AssertionError), both params. test_rw_13 stays green.
add("rw-12-coalesced-foreground-not-remembered", "numbered_overlay/controller.py",
    "            if self._overlay_numbers_can_be_dismissed():\n"
    "                self._overlay_settle_refire_foreground = True\n"
    "            self._arm_overlay_settle_refire(\"foreground change\")\n",
    "            self._arm_overlay_settle_refire(\"foreground change\")\n",
    "test_rw_12_burst_ending_on_another_app_clears_instead_of_walking",
    suite=RW_12_13)
# RW-13: a burst ending back on the numbered window refreshes. Mutant: the
# refire dismisses whenever a foreground event was coalesced, without reading
# the window in front. Caught by: test_rw_13 line 603
# (next_frame("start_overlay_walk", timeout_s=3.0) raises; a clear goes out).
# test_rw_12 stays green.
add("rw-13-refire-ignores-window-in-front", "numbered_overlay/controller.py",
    "        if not hwnd:\n"
    "            return False\n"
    "        return self._foreground_change_dismisses_numbers(hwnd)\n",
    "        if not hwnd:\n"
    "            return False\n"
    "        return True\n",
    "test_rw_13_burst_ending_on_numbered_window_refreshes",
    suite=RW_12_13)

# ---------------------------------------------------------------------------
# RW-15
# ---------------------------------------------------------------------------
RW_15 = sel(_T,
    "test_rw_15_menu_popup_while_painted_refreshes",
    "test_rw_1_foreground_change_while_painted_refreshes")
# RW-15: menu pop-up start or end while painted -> refresh walk.
# Mutant: only the menu-end event maps. Caught by: test_rw_15 [start] line 631
# (next_frame("start_overlay_walk") raises).
add("rw-15-menu-start-not-mapped", "overlay_focus_hooks.py",
    "    if event_id in (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND):\n"
    "        return OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE)\n",
    "    if event_id == EVENT_SYSTEM_MENUPOPUPEND:\n"
    "        return OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE)\n",
    "test_rw_15_menu_popup_while_painted_refreshes",
    suite=RW_15)
# Mutant: menu events dismiss the numbers (keep_numbers is off by default).
# Caught by: test_rw_15 line 631 (next_frame("start_overlay_walk") raises;
# clear + unpin go out instead), both params.
add("rw-15-menu-event-dismisses-numbers", "overlay_focus_hooks.py",
    "    if event_id in (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND):\n"
    "        return OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE)\n",
    "    if event_id in (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND):\n"
    "        return OverlayEvent(\n"
    "            kind=OverlayEventKind.FOCUS_CHANGE, dismisses_numbers=True,\n"
    "        )\n",
    "test_rw_15_menu_popup_while_painted_refreshes",
    suite=RW_15)

# ---------------------------------------------------------------------------
# RW-16
# ---------------------------------------------------------------------------
RW_16 = sel(_T,
    "test_rw_16_menu_close_inside_debounce_is_not_lost",
    "test_rw_15_menu_popup_while_painted_refreshes")
# RW-16: a menu event inside the shared debounce window arms the trailing
# re-walk. Mutant: a coalesced menu event arms nothing. Caught by: test_rw_16
# line 664 (next_frame("start_overlay_walk", timeout_s=3.0) raises).
add("rw-16-coalesced-menu-arms-no-trailing-walk", "numbered_overlay/controller.py",
    "            self._arm_overlay_settle_refire(f\"menu popup {verb}\")\n"
    "            return\n",
    "            return\n",
    "test_rw_16_menu_close_inside_debounce_is_not_lost",
    suite=RW_16)
# Mutant: menu events skip the shared debounce clock. Caught by: test_rw_16
# line 662 (frames == []; the menu start fires a supersede walk at once).
add("rw-16-menu-skips-shared-debounce", "numbered_overlay/controller.py",
    "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: menu pop-up event %#06x coalesced by debounce.\",\n",
    "        if False:\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: menu pop-up event %#06x coalesced by debounce.\",\n",
    "test_rw_16_menu_close_inside_debounce_is_not_lost",
    suite=RW_16)

# ---------------------------------------------------------------------------
# RW-19
# ---------------------------------------------------------------------------
RW_19 = sel(_T,
    "test_rw_19_tree_change_for_the_pinned_window_refreshes",
    "test_rw_21_tree_change_in_idle_states_is_dropped")
# RW-19: tree change for the pinned window while painted or refresh_in_flight
# -> refresh walk. Mutant: painted dropped from the accepted states.
# Caught by: test_rw_19 [painted] line 697 (next_frame("start_overlay_walk",
# timeout_s=2.0) raises).
add("rw-19-painted-not-accepted-for-tree-change", "overlay_focus_hooks.py",
    "        OverlayState.PAINTED,\n"
    "        OverlayState.REFRESH_IN_FLIGHT,\n"
    "        OverlayState.WALK_IN_FLIGHT,\n",
    "        OverlayState.REFRESH_IN_FLIGHT,\n"
    "        OverlayState.WALK_IN_FLIGHT,\n",
    "test_rw_19_tree_change_for_the_pinned_window_refreshes",
    suite=RW_19)
# Mutant: the handle comparison is inverted (a matching handle is dropped as
# other_window). Caught by: test_rw_19 line 697, both params.
add("rw-19-handle-comparison-inverted", "overlay_focus_hooks.py",
    "    if tracked_hwnd == 0 or event_hwnd == 0 or tracked_hwnd != event_hwnd:\n",
    "    if tracked_hwnd == 0 or event_hwnd == 0 or tracked_hwnd == event_hwnd:\n",
    "test_rw_19_tree_change_for_the_pinned_window_refreshes",
    suite=RW_19)

# ---------------------------------------------------------------------------
# RW-21
# ---------------------------------------------------------------------------
RW_21 = sel(_T,
    "test_rw_21_tree_change_in_idle_states_is_dropped",
    "test_rw_19_tree_change_for_the_pinned_window_refreshes")
# RW-21: tree change in closed / post_click_settling / error is dropped.
# Mutant: decide_tree_change skips its state check.
# EXPECTED SURVIVOR (reason 3): the rule is enforced twice. An event that
# passes decide_tree_change becomes FOCUS_CHANGE, and the machine's
# FOCUS_CHANGE arm is a NO_OP in exactly these states (closed, error,
# post_click_settling; decide_tree_change's docstring states that rule), and
# in closed and error no pin exists so the handle check drops it as
# other_window anyway. No output differs at the boundary. Breaking the
# machine arm instead is the RW-4 mutants, which rw_21 cannot see because
# this state check drops the event first.
add("rw-21-tree-change-state-check-skipped", "overlay_focus_hooks.py",
    "    if state not in TREE_CHANGE_ACCEPTED_STATES:\n",
    "    if False:\n",
    "test_rw_21_tree_change_in_idle_states_is_dropped",
    survives=("Needs two layers broken at once: the machine's FOCUS_CHANGE arm is a no-op in the same states"),
    suite=RW_21)

# ---------------------------------------------------------------------------
# RW-23
# ---------------------------------------------------------------------------
RW_23 = sel(_T,
    "test_rw_23_old_sequence_dropped_and_new_handle_restarts_it",
    "test_rw_19_tree_change_for_the_pinned_window_refreshes")
# RW-23: same handle and sequence at or below the last applied -> dropped.
# Mutant: only a strictly lower sequence is dropped (the repeat of 5 walks).
# Caught by: test_rw_23 line 760 (frames == []).
add("rw-23-equal-sequence-accepted", "overlay_focus_hooks.py",
    "    if event_hwnd == last_hwnd and event_sequence <= last_sequence:\n",
    "    if event_hwnd == last_hwnd and event_sequence < last_sequence:\n",
    "test_rw_23_old_sequence_dropped_and_new_handle_restarts_it",
    suite=RW_23)
# Mutant: the sequence check ignores the handle, so a new handle's sequence 1
# is stale against the old window's 6 (last_* is not reset on closed).
# Caught by: test_rw_23 line 776 (next_frame("start_overlay_walk",
# timeout_s=2.0) raises).
add("rw-23-new-handle-does-not-restart-sequence", "overlay_focus_hooks.py",
    "    if event_hwnd == last_hwnd and event_sequence <= last_sequence:\n",
    "    if event_sequence <= last_sequence:\n",
    "test_rw_23_old_sequence_dropped_and_new_handle_restarts_it",
    suite=RW_23)

# ---------------------------------------------------------------------------
# RW-24
# ---------------------------------------------------------------------------
RW_24 = sel(_T,
    "test_rw_24_typing_burst_inside_debounce_yields_one_walk",
    "test_rw_23_old_sequence_dropped_and_new_handle_restarts_it")
# RW-24: an accepted tree change inside the debounce window records its
# sequence, sends nothing now, and the trailing re-walk fires once.
# Mutant: the sequence is recorded only when the event fires, not when it is
# coalesced. Caught by: test_rw_24 line 812 (frames == []; the repeat of
# sequence 3 is accepted against last 1 and walks).
add("rw-24-coalesced-sequence-not-recorded", "numbered_overlay/controller.py",
    "        self._pm_tree_change_last_hwnd = hwnd\n"
    "        self._pm_tree_change_last_sequence = sequence\n"
    "\n"
    "        now_ms = self._clock() * 1000.0\n"
    "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: pattern manager tree change hwnd=%s seq=%s \"\n"
    "                \"coalesced by debounce.\",\n"
    "                hwnd,\n"
    "                sequence,\n"
    "            )\n"
    "            self._arm_overlay_settle_refire(\"pattern manager tree change\")\n"
    "            return\n",
    "        now_ms = self._clock() * 1000.0\n"
    "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: pattern manager tree change hwnd=%s seq=%s \"\n"
    "                \"coalesced by debounce.\",\n"
    "                hwnd,\n"
    "                sequence,\n"
    "            )\n"
    "            self._arm_overlay_settle_refire(\"pattern manager tree change\")\n"
    "            return\n"
    "        self._pm_tree_change_last_hwnd = hwnd\n"
    "        self._pm_tree_change_last_sequence = sequence\n",
    "test_rw_24_typing_burst_inside_debounce_yields_one_walk",
    suite=RW_24)
# Mutant: a coalesced tree change arms no trailing re-walk. Caught by:
# test_rw_24 line 802 (next_frame("start_overlay_walk", timeout_s=3.0) raises).
add("rw-24-coalesced-tree-change-arms-no-trailing-walk", "numbered_overlay/controller.py",
    "            self._arm_overlay_settle_refire(\"pattern manager tree change\")\n"
    "            return\n",
    "            return\n",
    "test_rw_24_typing_burst_inside_debounce_yields_one_walk",
    suite=RW_24)

# ---------------------------------------------------------------------------
# RW-25 and RW-26 (one shared selection)
# ---------------------------------------------------------------------------
RW_25_26 = sel(_T,
    "test_rw_25_stale_paint_over_browser_refreshes_at_keepalive_tick",
    "test_rw_26_keepalive_tick_without_conditions_starts_no_walk")
# RW-25: a stale paint over a browser refreshes at the keepalive tick.
# Mutant: the keepalive tick never calls the browser refresh. Caught by:
# test_rw_25 line 851 (next_frame("start_overlay_walk") raises).
# test_rw_26 stays green.
add("rw-25-keepalive-tick-skips-browser-refresh", "numbered_overlay/controller.py",
    "        self._arm_overlay_keepalive_timer()\n"
    "        # wh-n29v.121: LAST, so the re-arm above is already in place -- if this\n"
    "        # feeds a refresh, the apply's keepalive reconciler cancels the timer\n"
    "        # (state leaves PAINTED) and re-arms it when the refresh completes.\n"
    "        self._maybe_overlay_browser_refresh(machine)\n",
    "        self._arm_overlay_keepalive_timer()\n"
    "        # wh-n29v.121: LAST, so the re-arm above is already in place -- if this\n"
    "        # feeds a refresh, the apply's keepalive reconciler cancels the timer\n"
    "        # (state leaves PAINTED) and re-arms it when the refresh completes.\n",
    "test_rw_25_stale_paint_over_browser_refreshes_at_keepalive_tick",
    suite=RW_25_26)
# RW-26: a tick without the conditions starts no walk.
# Mutant: the browser-process test is inverted. Caught by: test_rw_26
# [not_browser] line 894 (frames == []) and test_rw_25 line 851
# (next_frame("start_overlay_walk") raises: chrome.exe is now skipped).
add("rw-26-browser-process-test-inverted", "numbered_overlay/controller.py",
    "        if tracked.process_name.lower() not in browsers:\n"
    "            return\n",
    "        if tracked.process_name.lower() in browsers:\n"
    "            return\n",
    "test_rw_26_keepalive_tick_without_conditions_starts_no_walk",
    "test_rw_25_stale_paint_over_browser_refreshes_at_keepalive_tick",
    suite=RW_25_26)
# Mutant: overlay_browser_refresh_seconds = 0 no longer turns the refresh
# off. Caught by: test_rw_26 [refresh_off] line 894 (frames == []).
add("rw-26-zero-refresh-window-not-off", "numbered_overlay/controller.py",
    "        window_s = getattr(self, \"_overlay_browser_refresh_seconds\", 0.0)\n"
    "        if window_s <= 0:\n"
    "            return\n",
    "        window_s = getattr(self, \"_overlay_browser_refresh_seconds\", 0.0)\n"
    "        if window_s < 0:\n"
    "            return\n",
    "test_rw_26_keepalive_tick_without_conditions_starts_no_walk",
    suite=RW_25_26)
# Mutant: the paint-age check is removed. Caught by: test_rw_26
# [paint_too_young] line 894 (frames == []).
add("rw-26-paint-age-not-checked", "numbered_overlay/controller.py",
    "        if self._overlay_now_monotonic() - last_paint < window_s * backoff:\n"
    "            return\n",
    "        if False:\n"
    "            return\n",
    "test_rw_26_keepalive_tick_without_conditions_starts_no_walk",
    suite=RW_25_26)

# ---------------------------------------------------------------------------
# RW-32
# ---------------------------------------------------------------------------
RW_32 = sel(_T,
    "test_rw_32_stale_replies_ignored_and_superseded_send_is_woken",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    "test_rw_15_menu_popup_while_painted_refreshes")
# RW-32: a trigger that supersedes a build wakes the parked send. Mutant: the
# focus-hook apply path does not wake the parked build, so the superseding
# walk waits behind _overlay_effect_lock for walk1's 10 s read limit.
# Caught by: test_rw_32 line 923 and test_rw_2_..._first_walk line 304
# (next_frame("start_overlay_walk", timeout_s=2.0) raises). test_rw_15 stays
# green (nothing is parked when its menu event arrives).
add("rw-32-supersede-does-not-wake-parked-build", "numbered_overlay/controller.py",
    "        # below, so the pair it compares against is the one this apply\n"
    "        # committed and the parked send is still the pre-transition one.\n"
    "        self._abort_inflight_overlay_build()\n",
    "        # below, so the pair it compares against is the one this apply\n"
    "        # committed and the parked send is still the pre-transition one.\n",
    "test_rw_32_stale_replies_ignored_and_superseded_send_is_woken",
    "test_rw_2_foreground_during_first_walk_supersedes_without_clear",
    suite=RW_32)
# RW-32: an event stamped with an old paint generation is ignored. Mutant: the
# machine's generation gate compares only the session id. The old-pair
# "expired" ack (sid, gen+1) then reaches _on_painted and closes the overlay.
# Caught by: test_rw_32 line 949 (frames == []; an unpin_snapshot goes out)
# -- line 950 (gui == []) would fail too on the clear_overlay.
add("rw-32-generation-gate-ignores-paint-generation", "click_overlay_state.py",
    "        if event.kind in _GENERATION_BEARING:\n"
    "            if (\n"
    "                event.overlay_session_id != self.overlay_session_id\n"
    "                or event.paint_generation != self.paint_generation\n"
    "            ):\n"
    "                return ApplyResult(OverlayOutcome.STALE_GENERATION)\n",
    "        if event.kind in _GENERATION_BEARING:\n"
    "            if (\n"
    "                event.overlay_session_id != self.overlay_session_id\n"
    "            ):\n"
    "                return ApplyResult(OverlayOutcome.STALE_GENERATION)\n",
    "test_rw_32_stale_replies_ignored_and_superseded_send_is_woken",
    suite=RW_32)


# ===========================================================================
# AO: auto-open (tests/test_overlay_requirements_autoopen.py)
# ===========================================================================
# Draft mutation-table entries for tests/test_overlay_requirements_autoopen.py
# (requirement prefix AO). Uses the gate file's helpers sel() and add().
# Every `old` was measured to occur exactly once in its target file (LF bytes).
# Line numbers below are for commit 954c6b50.

_AO = "test_overlay_requirements_autoopen.py"

# ---------------------------------------------------------------------------
# AO-1
# ---------------------------------------------------------------------------
AO_1 = sel(_AO,
           "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice",
           "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice")
# AO-1: an ambiguous click with the overlay closed auto-opens and sends no
# notice. Mutant: _perform_auto_open_ambiguous reports failure after it
# dispatched, so the caller also sends the plain notice. Caught by:
# test_ao_1 at test line 315 ("show_click_notice" not in gui).
add("ao-1-auto-open-reports-failure-so-notice-also-sent", "numbered_overlay/controller.py",
    "                \"(gen=%s, filter=%d id(s), trace_id=%s)\",\n"
    "                pair, len(item_ids), trace_id,\n"
    "            )\n"
    "            return True\n",
    "                \"(gen=%s, filter=%d id(s), trace_id=%s)\",\n"
    "                pair, len(item_ids), trace_id,\n"
    "            )\n"
    "            return False\n",
    "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice",
    suite=AO_1)
# AO-1: Mutant: the gate tests the wrong outcome, so an ambiguous click never
# auto-opens. Caught by: test_ao_1 at test line 240 (auto_open ->
# harness next_frame("show_numbered_overlay") raises AssertionError
# "Logic sent no 'show_numbered_overlay'", harness line 701, after 5 s).
add("ao-1-gate-tests-wrong-outcome", "numbered_overlay/controller.py",
    "            response.outcome == \"ambiguous\"\n"
    "            and self._host.click_config.enabled\n",
    "            response.outcome == \"not_found\"\n"
    "            and self._host.click_config.enabled\n",
    "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice",
    suite=AO_1)

# ---------------------------------------------------------------------------
# AO-2
# ---------------------------------------------------------------------------
AO_2 = sel(_AO,
           "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter",
           "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice")
# AO-2: show_numbered_overlay carries the click snapshot id, the finalist
# filter as a list, and a new session/generation. Mutant: the filter is not
# sent. Caught by: test_ao_2 at test line 338 (item_id_filter == [...]).
add("ao-2-filter-not-sent", "numbered_overlay/controller.py",
    "                \"item_id_filter\": item_id_filter,\n",
    "                \"item_id_filter\": None,\n",
    "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter",
    suite=AO_2)
# AO-2: Mutant: the reuse snapshot id is read from the machine's pin (None
# before the first pin; the wh-n29v.96.1 defect) instead of the effect.
# Caught by: test_ao_2 at test line 337 (snapshot_id == CLICK_SNAP).
add("ao-2-snapshot-id-from-pin-not-effect", "numbered_overlay/controller.py",
    "                \"snapshot_id\": effect.snapshot_id or \"\",\n"
    "                \"item_id_filter\": item_id_filter,\n",
    "                \"snapshot_id\": self.click_overlay_state.pinned_snapshot_id or \"\",\n"
    "                \"item_id_filter\": item_id_filter,\n",
    "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter",
    suite=AO_2)
# AO-2: Mutant: the filter is stashed as a tuple, so the request carries a
# tuple (the frame is pickled, which keeps the type). Caught by: test_ao_2 at
# test line 338 (a tuple never equals the expected list).
add("ao-2-filter-sent-as-tuple", "numbered_overlay/controller.py",
    "            self._overlay_auto_open_filter = (pair, list(item_ids))\n",
    "            self._overlay_auto_open_filter = (pair, tuple(item_ids))\n",
    "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter",
    suite=AO_2)
# AO-2: Mutant: AUTO_OPEN does not start a new session, so the request reuses
# the previous session id. Caught by: test_ao_2 at test line 344
# (overlay_session_id != earlier session id).
add("ao-2-auto-open-reuses-old-session-id", "click_overlay_state.py",
    "            self._start_session()\n"
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            self.pending_ambiguous_notice = event.notice\n",
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            self.pending_ambiguous_notice = event.notice\n",
    "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter",
    suite=AO_2)

# ---------------------------------------------------------------------------
# AO-3
# ---------------------------------------------------------------------------
AO_3 = sel(_AO,
           "test_ao_3_pin_and_paint_wait_for_the_build_reply",
           "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter")
# AO-3: no pin or paint before the build reply; after an ok reply, pin then a
# 1..K paint. Mutant: the walk_in_flight ok reply paints without pinning.
# Caught by: test_ao_3 at test line 371 (pins == [CLICK_SNAP]).
add("ao-3-ok-reply-paints-without-pin", "click_overlay_state.py",
    "            # Normal: -> paint_in_flight, pin + dispatch paint.\n"
    "            self.pinned_snapshot_id = event.snapshot_id\n"
    "            self.state = OverlayState.PAINT_IN_FLIGHT\n"
    "            return ApplyResult(\n"
    "                OverlayOutcome.ACCEPTED,\n"
    "                (\n"
    "                    self._cancel_timer(),\n"
    "                    self._pin(event.snapshot_id),\n",
    "            # Normal: -> paint_in_flight, pin + dispatch paint.\n"
    "            self.pinned_snapshot_id = event.snapshot_id\n"
    "            self.state = OverlayState.PAINT_IN_FLIGHT\n"
    "            return ApplyResult(\n"
    "                OverlayOutcome.ACCEPTED,\n"
    "                (\n"
    "                    self._cancel_timer(),\n",
    "test_ao_3_pin_and_paint_wait_for_the_build_reply",
    suite=AO_3)
# AO-3: Mutant: AUTO_OPEN pins the reuse snapshot at once, before the build
# reply. Caught by: test_ao_3 at test line 364 ("pin_snapshot" not in frames).
add("ao-3-auto-open-pins-before-reply", "click_overlay_state.py",
    "                    self._dispatch_build(\n"
    "                        BuildReason.AUTO_OPEN, snapshot_id=event.snapshot_id\n"
    "                    ),\n",
    "                    self._pin(event.snapshot_id),\n"
    "                    self._dispatch_build(\n"
    "                        BuildReason.AUTO_OPEN, snapshot_id=event.snapshot_id\n"
    "                    ),\n",
    "test_ao_3_pin_and_paint_wait_for_the_build_reply",
    suite=AO_3)

# ---------------------------------------------------------------------------
# AO-4
# ---------------------------------------------------------------------------
AO_4 = sel(_AO,
           "test_ao_4_auto_open_closes_the_mouse_grid_first",
           "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice")
# AO-4: an auto-open with the mouse grid open closes the grid first. Mutant:
# the auto-open path no longer closes the grid. Caught by: test_ao_4 at test
# line 398 ("clear_grid" in later).
add("ao-4-auto-open-leaves-grid-open", "numbered_overlay/controller.py",
    "            # handle_overlay_command 'show' branch.\n"
    "            self._close_grid_for_numbered_overlay(trace_id)\n",
    "            # handle_overlay_command 'show' branch.\n",
    "test_ao_4_auto_open_closes_the_mouse_grid_first",
    suite=AO_4)

# ---------------------------------------------------------------------------
# AO-6
# ---------------------------------------------------------------------------
AO_6 = sel(_AO,
           "test_ao_6_overlay_off_gives_the_plain_ambiguous_notice",
           "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice")
# AO-6: with the overlay off (or an overlay key invalid), an ambiguous click
# gets the plain notice. Mutant: the auto-open gate drops the
# overlay_enabled_effective term, so it auto-opens and suppresses the notice.
# Caught by: test_ao_6 (both params) at test line 415 (helper
# next_gui("show_click_notice") raises AssertionError, harness line 746).
add("ao-6-auto-open-ignores-overlay-off", "numbered_overlay/controller.py",
    "            and self._host.click_config.overlay_enabled_effective\n"
    "            and self._host.click_config.overlay_auto_open_on_ambiguous\n",
    "            and self._host.click_config.overlay_auto_open_on_ambiguous\n",
    "test_ao_6_overlay_off_gives_the_plain_ambiguous_notice",
    suite=AO_6)

# ---------------------------------------------------------------------------
# AO-7
# ---------------------------------------------------------------------------
AO_7 = sel(_AO,
           "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice",
           "test_ao_6_overlay_off_gives_the_plain_ambiguous_notice")
# AO-7: overlay_auto_open_on_ambiguous false gives the plain notice. Mutant:
# the gate drops the auto-open setting. Caught by: test_ao_7 at test line 415
# (helper next_gui("show_click_notice") AssertionError, harness line 746).
add("ao-7-auto-open-ignores-setting", "numbered_overlay/controller.py",
    "            and self._host.click_config.overlay_auto_open_on_ambiguous\n"
    "            and response.snapshot_id\n",
    "            and response.snapshot_id\n",
    "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice",
    suite=AO_7)

# ---------------------------------------------------------------------------
# AO-8
# ---------------------------------------------------------------------------
AO_8 = sel(_AO,
           "test_ao_8_unusable_click_reply_gives_the_plain_notice",
           "test_ao_8_malformed_finalist_ids_give_malformed_response_notice",
           "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice")
# AO-8: no snapshot_id, or no finalist ids, gives the plain notice. Mutant:
# the gate drops the snapshot_id term. Caught by: test_ao_8_unusable
# [no-snapshot-id] at test line 415 (helper next_gui AssertionError).
add("ao-8-auto-open-without-snapshot-id", "numbered_overlay/controller.py",
    "            and response.snapshot_id\n"
    "            and response.ambiguous_item_ids\n",
    "            and response.ambiguous_item_ids\n",
    "test_ao_8_unusable_click_reply_gives_the_plain_notice",
    suite=AO_8)
# AO-8: Mutant: the gate drops the ambiguous_item_ids term. Caught by:
# test_ao_8_unusable[ids-empty] at test line 415 (an empty filter auto-opens
# and suppresses the notice). [ids-missing] still passes: list(None) raises
# inside the guarded apply, which resets and sends the plain notice.
add("ao-8-auto-open-without-finalist-ids", "numbered_overlay/controller.py",
    "            and response.ambiguous_item_ids\n"
    "            and getattr(\n",
    "            and getattr(\n",
    "test_ao_8_unusable_click_reply_gives_the_plain_notice",
    suite=AO_8)
# AO-8 (note): a malformed ambiguous_item_ids makes the response malformed.
# Mutant: the schema degrades a non-list value to "absent" instead of raising.
# Caught by: test_ao_8_malformed at test line 508 (outcome ==
# "execution_failed"; the mutant gives the plain "ambiguous" notice).
add("ao-8-malformed-ids-read-as-absent", "shared/click_element.py",
    "    if type(raw) is not list and type(raw) is not tuple:\n"
    "        raise ClickElementResponseSchemaError(\n"
    "            \"field 'ambiguous_item_ids' must be a builtin list or tuple of \"\n"
    "            f\"str, got {type(raw).__name__}\"\n"
    "        )\n",
    "    if type(raw) is not list and type(raw) is not tuple:\n"
    "        return None\n",
    "test_ao_8_malformed_finalist_ids_give_malformed_response_notice",
    suite=AO_8)

# ---------------------------------------------------------------------------
# AO-9
# ---------------------------------------------------------------------------
AO_9 = sel(_AO,
           "test_ao_9_click_disabled_sends_no_input_request",
           "test_ao_9_gesture_click_disabled_notice_shows_once",
           "test_ao_28_click_off_turns_off_the_whole_feature")
# AO-9: with [click] enabled false, no Input request and one disabled notice
# per session. Mutant: the once-per-session flag is not consulted on the
# shown path. Caught by: test_ao_9_gesture at test line 560 (three notices);
# test_ao_28[click-disabled] at test line 1017 (two notices).
add("ao-9-disabled-notice-every-time", "numbered_overlay/controller.py",
    "            if self._host.take_click_disabled_notice():\n",
    "            if self._host.take_click_disabled_notice() or True:\n",
    "test_ao_9_gesture_click_disabled_notice_shows_once",
    "test_ao_28_click_off_turns_off_the_whole_feature",
    suite=AO_9)
# AO-9: Mutant: a deferred-notice caller ('click save', 'x-ray click save')
# falls through the disabled short-circuit and sends click_element to Input.
# Caught by: test_ao_9_click_disabled_sends_no_input_request at test line 535
# (frames == []); test_ao_28[click-disabled] at test line 1009 (the scripted
# ambiguous reply gives a notice, so nothing is typed).
add("ao-9-deferred-click-reaches-input-when-disabled", "numbered_overlay/controller.py",
    "        if not self._host.click_config.enabled:\n"
    "            if defer_failure_notice:\n",
    "        if not self._host.click_config.enabled and not defer_failure_notice:\n"
    "            if defer_failure_notice:\n",
    "test_ao_9_click_disabled_sends_no_input_request",
    "test_ao_28_click_off_turns_off_the_whole_feature",
    suite=AO_9)

# ---------------------------------------------------------------------------
# AO-10
# ---------------------------------------------------------------------------
AO_10 = sel(_AO,
            "test_ao_10_ambiguous_click_does_not_take_over_an_open_overlay",
            "test_ao_1_ambiguous_click_opens_overlay_instead_of_notice")
# AO-10: an ambiguous click while the overlay is open gets the notice and
# leaves the overlay alone. Mutant: the gate drops the CLOSED-state term, so
# AUTO_OPEN is applied to the open machine, which enters error (the plain
# notice still fires because the post-apply guard sees no walk_in_flight).
# Caught by: test_ao_10[painted] at test line 628 ('click two' gets the
# numbers_not_showing notice, so next_frame("click_snapshot_item") raises
# AssertionError, harness line 701); test_ao_10[walk_in_flight] at test line
# 635 (the error state drops the walk reply; no paint_overlay, harness 746).
add("ao-10-auto-open-takes-over-open-overlay", "numbered_overlay/controller.py",
    "            and response.ambiguous_item_ids\n"
    "            and getattr(\n"
    "                getattr(self, \"click_overlay_state\", None), \"state\", None,\n"
    "            ) is OverlayState.CLOSED\n",
    "            and response.ambiguous_item_ids\n",
    "test_ao_10_ambiguous_click_does_not_take_over_an_open_overlay",
    suite=AO_10)

# ---------------------------------------------------------------------------
# AO-11
# ---------------------------------------------------------------------------
AO_11 = sel(_AO,
            "test_ao_11_cached_list_is_only_the_renumbered_finalists",
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist",
            "test_ao_3_pin_and_paint_wait_for_the_build_reply",
            "test_ao_13_click_n_above_k_shows_no_badge_notice")
# AO-11: after the auto-open the cache holds only the K renumbered finalists.
# Mutant: the show_numbered_overlay reply's filtered summary is not cached,
# so the click walk's unfiltered five-item list stays. Caught by: test_ao_3 at
# test line 375 (display numbers [1..5] != [1, 2]); test_ao_12[1] at test
# line 689 (badge 1 is control 1, not finalist 2); test_ao_11 at test line 660
# ('click three' clicks control 3, so no no_badge notice: next_gui
# AssertionError, harness line 746).
add("ao-11-filtered-summary-not-cached", "numbered_overlay/controller.py",
    "        if build_ok and snapshot_id and summary is not None:\n"
    "            try:\n"
    "                self.click_snapshot_summary_cache.put(snapshot_id, summary)\n",
    "        if (\n"
    "            build_ok and snapshot_id and summary is not None\n"
    "            and action != \"show_numbered_overlay\"\n"
    "        ):\n"
    "            try:\n"
    "                self.click_snapshot_summary_cache.put(snapshot_id, summary)\n",
    "test_ao_11_cached_list_is_only_the_renumbered_finalists",
    "test_ao_12_click_n_on_auto_open_clicks_that_finalist",
    "test_ao_3_pin_and_paint_wait_for_the_build_reply",
    suite=AO_11)

# ---------------------------------------------------------------------------
# AO-12
# ---------------------------------------------------------------------------
AO_12 = sel(_AO,
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist",
            "test_ao_13_click_n_above_k_shows_no_badge_notice")
# AO-12: 'click N' (N in 1..K) clicks the finalist that carried badge N.
# Mutant: the resolver matches N against the 0-based list position, not the
# display number (off by one). Caught by: test_ao_12[1] at test line 689
# ('click one' resolves to finalist 4, not finalist 2).
add("ao-12-resolve-by-position-off-by-one", "click_snapshot_summary_cache.py",
    "    for item in result.summary.items:\n"
    "        if item.display_number == display_number:\n"
    "            return ResolveResult(ResolveOutcome.FOUND, item.item_id)\n",
    "    for index, item in enumerate(result.summary.items):\n"
    "        if index == display_number:\n"
    "            return ResolveResult(ResolveOutcome.FOUND, item.item_id)\n",
    "test_ao_12_click_n_on_auto_open_clicks_that_finalist",
    suite=AO_12)

# ---------------------------------------------------------------------------
# AO-13
# ---------------------------------------------------------------------------
AO_13 = sel(_AO,
            "test_ao_13_click_n_above_k_shows_no_badge_notice",
            "test_ao_11_cached_list_is_only_the_renumbered_finalists",
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist")
# AO-13: 'click N' with N > K shows no_badge_numbered and sends no click.
# Mutant: a numbered miss falls through to the by-name search. Caught by:
# test_ao_13 at test line 706 (no notice; next_gui AssertionError, harness
# line 746; a click_element goes out instead); test_ao_11 at test line 660.
add("ao-13-numbered-miss-falls-through-to-by-name",
    "speech/overlay_click_router.py",
    "    return RoutingDecision(\n"
    "        RoutingKind.NOTICE,\n"
    "        snapshot_id=visible_snapshot_id,\n"
    "        reason=NO_BADGE_NUMBERED,\n"
    "    )\n",
    "    return RoutingDecision(RoutingKind.BY_NAME)\n",
    "test_ao_13_click_n_above_k_shows_no_badge_notice",
    "test_ao_11_cached_list_is_only_the_renumbered_finalists",
    suite=AO_13)

# ---------------------------------------------------------------------------
# AO-15
# ---------------------------------------------------------------------------
AO_15 = sel(_AO,
            "test_ao_15_hide_numbers_closes_auto_open_without_notice",
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist")
# AO-15: 'hide numbers' on an auto-opened overlay clears and unpins with no
# ambiguous notice. Mutant: the hide path fires the held ambiguous notice.
# Caught by: test_ao_15 at test line 734 (_ambiguous_notices(...) == []).
add("ao-15-hide-fires-held-notice", "click_overlay_state.py",
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n",
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        if self.pending_ambiguous_notice is not None:\n"
    "            effects.append(self._fire_notice(self.pending_ambiguous_notice))\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n",
    "test_ao_15_hide_numbers_closes_auto_open_without_notice",
    suite=AO_15)
# AO-15: Mutant: the hide path clears the screen but does not unpin. Caught
# by: test_ao_15 at test line 731 (next_frame("unpin_snapshot") raises
# AssertionError, harness line 701).
add("ao-15-hide-does-not-unpin", "click_overlay_state.py",
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n",
    "        effects: list[Effect] = [self._cancel_timer(), self._dispatch_clear()]\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n",
    "test_ao_15_hide_numbers_closes_auto_open_without_notice",
    suite=AO_15)

# ---------------------------------------------------------------------------
# AO-18
# ---------------------------------------------------------------------------
AO_18 = sel(_AO,
            "test_ao_18_failed_build_reply_fires_the_suppressed_notice",
            "test_ao_19_no_build_reply_fires_the_suppressed_notice",
            "test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice",
            "test_ao_22_painted_auto_open_never_fires_the_held_notice")
# AO-18: a failed show_numbered_overlay reply fires the held ambiguous notice
# and closes. Mutant: the in-flight failure recovery ignores the held notice
# (generic numbers_not_drawn notice, or none in the paint phase). Caught by:
# test_ao_18 at test line 775 (outcome == "ambiguous"); test_ao_19 at test
# line 802; test_ao_20_ao_21 at test line 832 (no notice at all: next_gui
# AssertionError, harness line 746).
add("ao-18-failure-recovery-ignores-held-notice", "click_overlay_state.py",
    "        if self.pending_ambiguous_notice is not None:\n"
    "            effects.append(self._fire_notice(self.pending_ambiguous_notice))\n"
    "        elif emit_standalone_notice:\n",
    "        if emit_standalone_notice:\n",
    "test_ao_18_failed_build_reply_fires_the_suppressed_notice",
    "test_ao_19_no_build_reply_fires_the_suppressed_notice",
    "test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice",
    suite=AO_18)
# AO-18: Mutant: the echoed-pair check compares only the session id, so an ok
# reply whose echoed generation differs is used. Caught by:
# test_ao_18[echoed-generation-differs]: the reply paints; with no ack the
# 1000 ms paint deadline fires the notice, then test line 779 fails
# ("paint_overlay" not in gui).
add("ao-18-echo-check-ignores-generation", "numbered_overlay/controller.py",
    "        if (echoed_sid, echoed_gen) != (sid, gen):\n",
    "        if echoed_sid != sid:\n",
    "test_ao_18_failed_build_reply_fires_the_suppressed_notice",
    suite=AO_18)

# ---------------------------------------------------------------------------
# AO-19
# ---------------------------------------------------------------------------
AO_19 = sel(_AO,
            "test_ao_19_no_build_reply_fires_the_suppressed_notice",
            "test_ao_18_failed_build_reply_fires_the_suppressed_notice",
            "test_ao_22_painted_auto_open_never_fires_the_held_notice")
# AO-19: no build reply by the deadline fires the held notice and the overlay
# returns to closed. Mutant: the in-flight failure recovery does not enter
# closed (the machine stays walk_in_flight). Caught by: test_ao_19 at test
# line 805 (the 10.25 s machine TIMEOUT and the 3 s reply-timeout feed both
# reach _error_to_closed, so two ambiguous notices) or, failing that, test
# line 264 via assert_closed_by_new_session (re-said 'show numbers' restarts
# the walk in the SAME session); test_ao_18 at test line 264 (same reason).
add("ao-19-failure-recovery-does-not-close", "click_overlay_state.py",
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n"
    "\n"
    "    def _restart_walk(",
    "        effects.extend(self._unpin_all_pinned())\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n"
    "\n"
    "    def _restart_walk(",
    "test_ao_19_no_build_reply_fires_the_suppressed_notice",
    "test_ao_18_failed_build_reply_fires_the_suppressed_notice",
    suite=AO_19)
# EXPECTED SURVIVOR (reason 3): the machine's walk_in_flight TIMEOUT cell is
# masked at the boundary. The test advances 21 s, which also expires the
# show_numbered_overlay reply wait (response_timeout_ms 3000,
# numbered_overlay/controller.py NumberedOverlayController._overlay_dispatch_build); that path feeds a failed BUILD_RESPONSE, which fires the
# same held notice and closes the overlay, so ignoring the TIMEOUT changes no
# boundary output.
add("ao-19-walk-timeout-ignored", "click_overlay_state.py",
    "            # walk_in_flight timeout -> error -> closed: fire the pending\n"
    "            # notice (auto-open) or the generic standalone notice (standalone\n"
    "            # \"show numbers\"), unpin (wh-n29v.16.1, v4 line 278).\n"
    "            return self._error_to_closed(emit_standalone_notice=True)\n",
    "            # walk_in_flight timeout -> error -> closed: fire the pending\n"
    "            # notice (auto-open) or the generic standalone notice (standalone\n"
    "            # \"show numbers\"), unpin (wh-n29v.16.1, v4 line 278).\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n",
    "test_ao_19_no_build_reply_fires_the_suppressed_notice",
    survives=('Needs the walk_in_flight TIMEOUT cell reached: the reply wait fails first with the same held notice and close'),
    suite=AO_19)

# ---------------------------------------------------------------------------
# AO-20 and AO-21
# ---------------------------------------------------------------------------
AO_20_21 = sel(_AO,
               "test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice",
               "test_ao_22_painted_auto_open_never_fires_the_held_notice")
# AO-20: a failed paint ack on an auto-open fires the held notice, clears,
# unpins, closes. Mutant: a failed paint ack closes like 'hide numbers' (no
# notice). Caught by: test_ao_20_ao_21[failed] at test line 832 (next_gui
# AssertionError, harness line 746).
add("ao-20-failed-paint-ack-closes-silently", "click_overlay_state.py",
    "            if event.paint_state is PaintAckState.FAILED:\n"
    "                # paint failed -> error -> closed; fire the pending notice only\n"
    "                # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "                return self._error_to_closed(emit_standalone_notice=False)\n",
    "            if event.paint_state is PaintAckState.FAILED:\n"
    "                # paint failed -> error -> closed; fire the pending notice only\n"
    "                # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "                return self._hide_to_closed()\n",
    "test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice",
    suite=AO_20_21)
# AO-21: no paint ack within 1000 ms fires the held notice. Mutant: the paint
# timeout closes like 'hide numbers' (no notice). Caught by:
# test_ao_20_ao_21[no_ack] at test line 832 (next_gui AssertionError).
add("ao-21-paint-timeout-closes-silently", "click_overlay_state.py",
    "        if kind is OverlayEventKind.TIMEOUT:\n"
    "            # paint timeout -> error -> closed; fire the pending notice only\n"
    "            # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "            return self._error_to_closed(emit_standalone_notice=False)\n",
    "        if kind is OverlayEventKind.TIMEOUT:\n"
    "            # paint timeout -> error -> closed; fire the pending notice only\n"
    "            # (v4 line 279 fires no standalone notice for the paint phase).\n"
    "            return self._hide_to_closed()\n",
    "test_ao_20_ao_21_failed_or_missing_paint_ack_fires_the_notice",
    suite=AO_20_21)

# ---------------------------------------------------------------------------
# AO-22
# ---------------------------------------------------------------------------
AO_22 = sel(_AO,
            "test_ao_22_painted_auto_open_never_fires_the_held_notice",
            "test_ao_15_hide_numbers_closes_auto_open_without_notice")
# AO-22: once an auto-open paints, the held notice is never sent; a later
# refresh failure fires only numbers_not_drawn. Mutant: the failed refresh
# whose window left closes through _error_to_closed, which fires the held
# ambiguous notice (the docstring's warned-against change). Caught by:
# test_ao_22 at test line 868 (reason == "numbers_not_drawn").
add("ao-22-window-left-refresh-fires-held-notice", "click_overlay_state.py",
    "        effects: list[Effect] = [\n"
    "            self._cancel_timer(),\n"
    "            self._dispatch_clear(),\n"
    "            self._fire_standalone_failure_notice(),\n"
    "        ]\n"
    "        effects.extend(self._unpin_all_pinned())\n"
    "        self._enter_closed()\n"
    "        return ApplyResult(OverlayOutcome.ACCEPTED, tuple(effects))\n",
    "        return self._error_to_closed(emit_standalone_notice=True)\n",
    "test_ao_22_painted_auto_open_never_fires_the_held_notice",
    suite=AO_22)

# ---------------------------------------------------------------------------
# AO-26
# ---------------------------------------------------------------------------
AO_26 = sel(_AO,
            "test_ao_26_click_number_with_overlay_off_clicks_by_name",
            "test_ao_27_bad_overlay_key_turns_off_only_the_overlay",
            "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice")
# AO-26: with the overlay off, 'click 7' is a by-name click for '7' (state
# closed). Mutant: show/hide numbers handling ignores the overlay-off gate, so
# 'show numbers' opens a walk and 'click 7' is held. Caught by: test_ao_26
# (both params) at test line 901 (next_frame("click_element") AssertionError)
# or line 905 (_actions(frames) == ["click_element"]); test_ao_27 at test
# line 957 (frames == []).
add("ao-26-show-numbers-ignores-overlay-off", "numbered_overlay/controller.py",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "test_ao_26_click_number_with_overlay_off_clicks_by_name",
    "test_ao_27_bad_overlay_key_turns_off_only_the_overlay",
    suite=AO_26)
# AO-26: Mutant: a number spoken while the overlay is closed gets the
# no_badge notice instead of a by-name search. Caught by: test_ao_26 at test
# line 901 (next_frame("click_element") AssertionError, harness line 701).
add("ao-26-closed-number-not-searched-by-name",
    "speech/overlay_click_router.py",
    "    if state is OverlayState.CLOSED:\n"
    "        # The spoken digit is looked up by name; the by-name flow may or may\n"
    "        # not find a control whose accessible name is the digit.\n"
    "        return RoutingDecision(RoutingKind.BY_NAME)\n",
    "    if state is OverlayState.CLOSED:\n"
    "        # The spoken digit is looked up by name; the by-name flow may or may\n"
    "        # not find a control whose accessible name is the digit.\n"
    "        return RoutingDecision(RoutingKind.NOTICE, reason=NO_BADGE_NUMBERED)\n",
    "test_ao_26_click_number_with_overlay_off_clicks_by_name",
    suite=AO_26)

# ---------------------------------------------------------------------------
# AO-27
# ---------------------------------------------------------------------------
AO_27 = sel(_AO,
            "test_ao_27_bad_overlay_key_turns_off_only_the_overlay",
            "test_ao_26_click_number_with_overlay_off_clicks_by_name",
            "test_ao_28_click_off_turns_off_the_whole_feature")
# AO-27: a bad overlay-track key turns off only the overlay. Mutant: a bad
# overlay value silently takes its default and is not recorded, so the
# overlay stays on. Caught by: test_ao_27 (every param) at test line 957
# (frames == []: 'show numbers' walks); test_ao_26[bad-overlay-key] at test
# line 901/905.
add("ao-27-bad-overlay-key-not-recorded", "ui/click_config.py",
    "            coerced[key] = defaults[key]\n"
    "            overlay_invalid_key.append(source_key)\n",
    "            coerced[key] = defaults[key]\n",
    "test_ao_27_bad_overlay_key_turns_off_only_the_overlay",
    "test_ao_26_click_number_with_overlay_off_clicks_by_name",
    suite=AO_27)
# AO-27: Mutant: overlay_enabled_effective ignores overlay_invalid_key.
# Caught by: the same assertions as above.
add("ao-27-effective-flag-ignores-invalid-keys", "ui/click_config.py",
    "        return self.overlay_enabled and self.overlay_invalid_key == ()\n",
    "        return self.overlay_enabled\n",
    "test_ao_27_bad_overlay_key_turns_off_only_the_overlay",
    "test_ao_26_click_number_with_overlay_off_clicks_by_name",
    suite=AO_27)

# ---------------------------------------------------------------------------
# AO-28
# ---------------------------------------------------------------------------
AO_28 = sel(_AO,
            "test_ao_28_click_off_turns_off_the_whole_feature",
            "test_ao_7_auto_open_off_gives_the_plain_ambiguous_notice")
# AO-28: an invalid Phase 1 key or enabled=false turns off the whole feature.
# Mutant: show/hide numbers handling gates on overlay_enabled_effective only;
# a valid enabled=false leaves that True. Caught by: test_ao_28[click-disabled]
# at test line 1001 (frames == []: 'show numbers' walks).
add("ao-28-show-numbers-ignores-click-disabled", "numbered_overlay/controller.py",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "        if not (\n"
    "            self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n",
    "test_ao_28_click_off_turns_off_the_whole_feature",
    suite=AO_28)
# AO-28: Mutant: the focus hooks start when only the overlay is on. Caught by:
# test_ao_28[click-disabled] at test line 992 (no focus hook registered).
add("ao-28-focus-hooks-ignore-click-disabled", "numbered_overlay/controller.py",
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.info(\n"
    "                \"overlay: focus hooks not started (numbered overlay not \"\n",
    "        if not (\n"
    "            self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            logger.info(\n"
    "                \"overlay: focus hooks not started (numbered overlay not \"\n",
    "test_ao_28_click_off_turns_off_the_whole_feature",
    suite=AO_28)
# EXPECTED SURVIVOR (reason 3): the auto-open gate's `enabled` term is dead
# code. forward_click_element returns at numbered_overlay/controller.py NumberedOverlayController.forward_click_element when enabled is false,
# before any click_element is sent, so no ambiguous reply ever reaches the
# gate with clicking disabled.
add("ao-28-auto-open-gate-drops-enabled-term", "numbered_overlay/controller.py",
    "            response.outcome == \"ambiguous\"\n"
    "            and self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n",
    "            response.outcome == \"ambiguous\"\n"
    "            and self._host.click_config.overlay_enabled_effective\n",
    "test_ao_28_click_off_turns_off_the_whole_feature",
    survives=('Needs an ambiguous reply with clicking disabled: forward_click_element returns before any click_element'),
    suite=AO_28)


# ===========================================================================
# Round 2: LC-5, LC-6, LC-12, LC-15, LC-23, LC-25 (lifecycle tests of commit
# b5dc7f5d) and CK-6, CK-9, CK-10, CK-11 (click tests of commit 524e3948).
# ===========================================================================

# ---------------------------------------------------------------------------
# LC-5
# ---------------------------------------------------------------------------
LC_5 = sel(_LC, "test_lc_5_show_numbers_refused_while_grid_pointer_pending",
           "test_lc_6_show_numbers_closes_the_mouse_grid_first")
# LC-5: 'show numbers' with the grid pointer channel unconfirmed is refused
# with a grid_pointer_pending notice. Mutant: the channel probe still goes
# out, but its answer is ignored and the show proceeds. Caught by: test_lc_5
# at the next_gui("show_click_notice") loop (no grid_pointer_pending notice).
add("lc-5-show-ignores-the-unconfirmed-channel", "numbered_overlay/controller.py",
    "            if not await self._host.grid_pointer_channel_quiescent(\n"
    "                trace_id=trace_id,\n"
    "            ):\n"
    "                logger.warning(\n"
    "                    \"overlay: show withheld, grid pointer channel \"\n",
    "            if not (await self._host.grid_pointer_channel_quiescent(\n"
    "                trace_id=trace_id,\n"
    "            ) or True):\n"
    "                logger.warning(\n"
    "                    \"overlay: show withheld, grid pointer channel \"\n",
    "test_lc_5_show_numbers_refused_while_grid_pointer_pending",
    suite=LC_5)
# LC-5: Mutant: the notice is shown, but the show then goes on to walk.
# Caught by: test_lc_5 at "start_overlay_walk" not in _actions(frames).
add("lc-5-show-walks-after-the-notice", "numbered_overlay/controller.py",
    "                    snapshot_id=None,\n"
    "                    trace_id=trace_id,\n"
    "                )\n"
    "                return None\n"
    "            kind = OverlayEventKind.SHOW_NUMBERS\n",
    "                    snapshot_id=None,\n"
    "                    trace_id=trace_id,\n"
    "                )\n"
    "            kind = OverlayEventKind.SHOW_NUMBERS\n",
    "test_lc_5_show_numbers_refused_while_grid_pointer_pending",
    suite=LC_5)

# ---------------------------------------------------------------------------
# LC-6
# ---------------------------------------------------------------------------
LC_6 = sel(_LC, "test_lc_6_show_numbers_closes_the_mouse_grid_first",
           "test_lc_5_show_numbers_refused_while_grid_pointer_pending")
# LC-6: 'show numbers' closes the mouse grid before the overlay walk. Mutant:
# the show branch no longer closes the grid. Caught by: test_lc_6 at
# "clear_grid" in actions.
add("lc-6-show-leaves-the-grid-open", "numbered_overlay/controller.py",
    "            # consumer. Never raises; a closed grid is a no-op.\n"
    "            self._close_grid_for_numbered_overlay(trace_id)\n"
    "        elif command == \"hide\":\n",
    "            # consumer. Never raises; a closed grid is a no-op.\n"
    "            pass\n"
    "        elif command == \"hide\":\n",
    "test_lc_6_show_numbers_closes_the_mouse_grid_first",
    suite=LC_6)
# LC-6: Mutant: the grid machine closes, but the clear_grid message is never
# sent to the GUI, so the grid stays on screen. Caught by: test_lc_6 at
# "clear_grid" in actions.
add("lc-6-grid-close-not-sent-to-the-gui", "numbered_overlay/controller.py",
    "                self._host.put_grid_gui_event(\n"
    "                    ClearGridEvent().to_dict(), trace_id,\n"
    "                )\n",
    "                pass\n",
    "test_lc_6_show_numbers_closes_the_mouse_grid_first",
    suite=LC_6)

# ---------------------------------------------------------------------------
# LC-12 (three cases: wrong session id, wrong generation, unreadable body)
# ---------------------------------------------------------------------------
LC_12 = sel(_LC, "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk",
            "test_lc_15_no_targets_walk_reply_is_a_failed_walk")
# LC-12 wrong session id: the echo check compares only the generation.
# Caught by: test_lc_12[wrong-session-id] at the next_gui("show_click_notice")
# (the skewed reply paints, so no numbers_not_drawn notice).
add("lc-12-echo-check-ignores-the-session", "numbered_overlay/controller.py",
    "        if (echoed_sid, echoed_gen) != (sid, gen):\n"
    "            logger.error(\n"
    "                \"overlay: %s response echoed generation (%s,%s) != request \"\n",
    "        if echoed_gen != gen:\n"
    "            logger.error(\n"
    "                \"overlay: %s response echoed generation (%s,%s) != request \"\n",
    "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk",
    suite=LC_12)
# LC-12 wrong generation: the echo check compares only the session. Caught
# by: test_lc_12[wrong-generation], same assertion.
add("lc-12-echo-check-ignores-the-generation", "numbered_overlay/controller.py",
    "        if (echoed_sid, echoed_gen) != (sid, gen):\n"
    "            logger.error(\n"
    "                \"overlay: %s response echoed generation (%s,%s) != request \"\n",
    "        if echoed_sid != sid:\n"
    "            logger.error(\n"
    "                \"overlay: %s response echoed generation (%s,%s) != request \"\n",
    "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk",
    suite=LC_12)
# LC-12 wrong pair (both cases): the design's "drop the stale response"
# wording -- a skewed echo is dropped instead of failing the walk. Caught by:
# test_lc_12[wrong-session-id] and [wrong-generation] at the
# next_gui("show_click_notice") (no numbers_not_drawn notice).
add("lc-12-skewed-echo-dropped-not-failed", "numbered_overlay/controller.py",
    "                action, echoed_sid, echoed_gen, sid, gen, trace_id,\n"
    "            )\n"
    "            _feed(build_ok=False, snapshot_id=None)\n"
    "            return\n",
    "                action, echoed_sid, echoed_gen, sid, gen, trace_id,\n"
    "            )\n"
    "            return\n",
    "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk",
    suite=LC_12)
# LC-12 unreadable body: the malformed reply is logged and dropped instead of
# failing the walk. Caught by: test_lc_12[unreadable-body] and
# [unreadable-other] at the next_gui("show_click_notice").
add("lc-12-unreadable-reply-dropped-not-failed", "numbered_overlay/controller.py",
    "            # failure so the machine recovers rather than stalling.\n"
    "            _feed(build_ok=False, snapshot_id=None)\n"
    "            return\n",
    "            # failure so the machine recovers rather than stalling.\n"
    "            return\n",
    "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk",
    suite=LC_12)

# ---------------------------------------------------------------------------
# LC-15
# ---------------------------------------------------------------------------
LC_15 = sel(_LC, "test_lc_15_no_targets_walk_reply_is_a_failed_walk",
            "test_lc_12_unmatched_or_unreadable_walk_reply_is_a_failed_walk")
# LC-15: a no_targets walk reply is a failed walk. Mutant: the design
# wording -- an empty window is a successful build (build_ok True). Caught by:
# test_lc_15 (no numbers_not_drawn notice, or a pin/paint of the empty list).
add("lc-15-no-targets-counts-as-a-built-list", "numbered_overlay/controller.py",
    "        return (\n"
    "            resp.outcome == \"ok\", resp.snapshot_id, resp.snapshot_summary,\n"
    "            resp.overlay_session_id, resp.paint_generation,\n"
    "            # wh-overlay-slow-uia-stale-badges.2.2.1: the window Input read.\n",
    "        return (\n"
    "            resp.outcome in (\"ok\", \"no_targets\"), resp.snapshot_id,\n"
    "            resp.snapshot_summary,\n"
    "            resp.overlay_session_id, resp.paint_generation,\n"
    "            # wh-overlay-slow-uia-stale-badges.2.2.1: the window Input read.\n",
    "test_lc_15_no_targets_walk_reply_is_a_failed_walk",
    suite=LC_15)

# ---------------------------------------------------------------------------
# LC-23
# ---------------------------------------------------------------------------
LC_23 = sel(_LC, "test_lc_23_failed_refresh_paint_clears_unpins_both_and_closes",
            "test_lc_15_no_targets_walk_reply_is_a_failed_walk")
# LC-23: a refresh paint acked failed (no mic pause) clears, unpins both and
# closes. Mutant: the auto-hide fall-back runs without a mic pause too (the
# behaviour before wh-overlay-slow-uia-stale-badges.18.7). Caught by:
# test_lc_23 at next_gui("clear_overlay") or the unpin of snap-2.
add("lc-23-failed-refresh-paint-falls-back", "click_overlay_state.py",
    "                # for the paint phase).\n"
    "                return self._error_to_closed(emit_standalone_notice=False)\n",
    "                # for the paint phase).\n"
    "                return self._refresh_paint_failed()\n",
    "test_lc_23_failed_refresh_paint_clears_unpins_both_and_closes",
    suite=LC_23)
# LC-23: Mutant: the close fires the generic standalone notice. Caught by:
# test_lc_23 at "show_click_notice" not in _actions(b.gui_since(mark)).
add("lc-23-failed-refresh-paint-fires-a-notice", "click_overlay_state.py",
    "                # for the paint phase).\n"
    "                return self._error_to_closed(emit_standalone_notice=False)\n",
    "                # for the paint phase).\n"
    "                return self._error_to_closed(emit_standalone_notice=True)\n",
    "test_lc_23_failed_refresh_paint_clears_unpins_both_and_closes",
    suite=LC_23)

# ---------------------------------------------------------------------------
# LC-25 (no retry, no state change, no notice; one check per reply kind)
# ---------------------------------------------------------------------------
LC_25 = sel(_LC, "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
            "test_lc_23_failed_refresh_paint_clears_unpins_both_and_closes")
# LC-25 no reply: Mutant: the design's re-dispatch -- a pin or unpin with no
# reply is sent once more. Caught by: test_lc_25[*-no_reply] at the
# "no retry" assertion (frames_since(mark) holds a second request).
add("lc-25-no-reply-retried", "numbered_overlay/controller.py",
    "                \"overlay: %s no reply within %dms (trace_id=%s)\",\n"
    "                action, timeout_ms, trace_id,\n"
    "            )\n"
    "            return\n",
    "                \"overlay: %s no reply within %dms (trace_id=%s)\",\n"
    "                action, timeout_ms, trace_id,\n"
    "            )\n"
    "            try:\n"
    "                await self._host.app.send_request(\n"
    "                    action, params=params, timeout_s=timeout_s,\n"
    "                )\n"
    "            except Exception:  # noqa: BLE001\n"
    "                pass\n"
    "            return\n",
    "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
    suite=LC_25)
# LC-25 malformed reply: Mutant: a malformed reply is re-dispatched. Caught
# by: test_lc_25[*-malformed] at the "no retry" assertion.
add("lc-25-malformed-reply-retried", "numbered_overlay/controller.py",
    "                \"overlay: malformed %s response (trace_id=%s): %s\",\n"
    "                action, trace_id, exc,\n"
    "            )\n"
    "            return\n",
    "                \"overlay: malformed %s response (trace_id=%s): %s\",\n"
    "                action, trace_id, exc,\n"
    "            )\n"
    "            try:\n"
    "                await self._host.app.send_request(\n"
    "                    action, params=params, timeout_s=timeout_s,\n"
    "                )\n"
    "            except Exception:  # noqa: BLE001\n"
    "                pass\n"
    "            return\n",
    "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
    suite=LC_25)
# LC-25 status error: Mutant: an error ack is re-dispatched. Caught by:
# test_lc_25[*-status_error] at the "no retry" assertion.
add("lc-25-error-reply-retried", "numbered_overlay/controller.py",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n"
    "            try:\n"
    "                await self._host.app.send_request(\n"
    "                    action, params=params, timeout_s=timeout_s,\n"
    "                )\n"
    "            except Exception:  # noqa: BLE001\n"
    "                pass\n",
    "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
    suite=LC_25)
# LC-25 status error: Mutant: an error ack shows a click notice. Caught by:
# test_lc_25[*-status_error] at "show_click_notice" not in the GUI messages.
add("lc-25-error-reply-shows-a-notice", "numbered_overlay/controller.py",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n"
    "            self.forward_click_notice(\n"
    "                outcome=\"execution_failed\", reason=\"numbers_not_drawn\",\n"
    "                matched_name=None, matched_names=(), spoken_name=\"\",\n"
    "                snapshot_id=None, trace_id=trace_id,\n"
    "            )\n",
    "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
    suite=LC_25)
# LC-25 status error: Mutant: a failed pin closes the overlay (a state
# change). Caught by: test_lc_25[pin_snapshot-status_error] at
# next_frame("click_snapshot_item") ('click two' is a by-name click).
add("lc-25-failed-pin-closes-the-overlay", "numbered_overlay/controller.py",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n",
    "                action, resp.status, resp.reason, resp.pinned,\n"
    "                resp.snapshot_id, trace_id,\n"
    "            )\n"
    "            if action == \"pin_snapshot\":\n"
    "                from services.wheelhouse.click_overlay_state import (\n"
    "                    OverlayEvent as _MutEvent,\n"
    "                    OverlayEventKind as _MutKind,\n"
    "                )\n"
    "                self._apply_overlay_event(\n"
    "                    _MutEvent(_MutKind.HIDE_NUMBERS), source=\"pin failed\",\n"
    "                )\n",
    "test_lc_25_bad_pin_reply_is_logged_and_nothing_else_happens",
    suite=LC_25)

# ---------------------------------------------------------------------------
# CK-6
# ---------------------------------------------------------------------------
_CK = "test_overlay_requirements_click.py"
CK_6 = sel(_CK, "test_ck_6_click_n_during_refresh_for_another_window_is_held",
           "test_ck_5_click_n_during_refresh_uses_the_visible_snapshot")
# CK-6: 'click N' during a refresh for another window is held. Mutant: the
# router's trigger-B hold is removed, so the click resolves against the left
# window's visible list. Caught by: test_ck_6[unanswered] at
# next_gui("show_click_notice"); test_ck_6[painted_during_hold] at
# click.params["snapshot_id"] == "snap-2".
add("ck-6-router-ignores-the-left-window", "speech/overlay_click_router.py",
    "        if visible_window_is_foreground is False:\n"
    "            return RoutingDecision(RoutingKind.HELD)\n",
    "        pass\n",
    "test_ck_6_click_n_during_refresh_for_another_window_is_held",
    suite=CK_6)
# CK-6: Mutant: the immediate route (forward_click_element) does not tell the
# router whether the visible window is in front. Caught by: the same
# assertions as above.
add("ck-6-immediate-route-omits-the-foreground-answer", "numbered_overlay/controller.py",
    "                prior_pin_deferred=machine.prior_pin_deferred,\n"
    "                visible_window_is_foreground=(\n"
    "                    self._overlay_refresh_visible_window_is_foreground(machine)\n"
    "                    if machine.state is OverlayState.REFRESH_IN_FLIGHT\n"
    "                    else None\n"
    "                ),\n"
    "            )\n"
    "\n"
    "        if decision.kind is RoutingKind.SNAPSHOT_ITEM:\n",
    "                prior_pin_deferred=machine.prior_pin_deferred,\n"
    "                visible_window_is_foreground=None,\n"
    "            )\n"
    "\n"
    "        if decision.kind is RoutingKind.SNAPSHOT_ITEM:\n",
    "test_ck_6_click_n_during_refresh_for_another_window_is_held",
    suite=CK_6)

# ---------------------------------------------------------------------------
# CK-9
# ---------------------------------------------------------------------------
CK_9 = sel(_CK,
           "test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason",
           "test_ck_11_second_held_click_n_replaces_the_first")
# CK-9: a held click the router answers with a notice shows that reason.
# Mutant: the hold's NOTICE branch is skipped, so every notice decision falls
# through to numbers_not_showing. Caught by: test_ck_9 at
# notice["reason"] == "no_badge_numbered".
add("ck-9-held-notice-becomes-numbers-not-showing", "numbered_overlay/controller.py",
    "        if decision.kind is RoutingKind.NOTICE:\n"
    "            logger.info(\n"
    "                \"overlay: held click N=%s -> notice reason=%r after hold \"\n",
    "        if decision.kind is None:\n"
    "            logger.info(\n"
    "                \"overlay: held click N=%s -> notice reason=%r after hold \"\n",
    "test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason",
    suite=CK_9)
# CK-9: Mutant: the held miss notice drops the painted snapshot id. Caught by:
# test_ck_9 at notice["snapshot_id"] == "snap-1".
add("ck-9-held-notice-drops-the-snapshot", "numbered_overlay/controller.py",
    "                snapshot_id=decision.snapshot_id, trace_id=trace_id,\n"
    "            )\n"
    "            return\n"
    "        # Still HELD (overlay never painted within the hold), or BY_NAME (the\n",
    "                snapshot_id=None, trace_id=trace_id,\n"
    "            )\n"
    "            return\n"
    "        # Still HELD (overlay never painted within the hold), or BY_NAME (the\n",
    "test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason",
    suite=CK_9)

# ---------------------------------------------------------------------------
# CK-10
# ---------------------------------------------------------------------------
CK_10 = sel(_CK, "test_ck_10_hold_across_a_new_paint_generation_clicks_nothing",
            "test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason")
# CK-10: a held click whose paint generation changed clicks nothing. Mutant:
# the hold's gate compares only the session id. Caught by: test_ck_10 at
# next_gui("show_click_notice") (a click_snapshot_item for snap-2 instead).
add("ck-10-hold-gate-ignores-the-generation", "numbered_overlay/controller.py",
    "        if current_pair != armed_pair:\n"
    "            # The overlay was superseded / a new session started during the hold.\n",
    "        if current_pair[0] != armed_pair[0]:\n"
    "            # The overlay was superseded / a new session started during the hold.\n",
    "test_ck_10_hold_across_a_new_paint_generation_clicks_nothing",
    suite=CK_10)
# CK-10: Mutant: the pair is read when the hold fires, not when it is armed,
# so the gate always passes. Caught by: the same assertion.
add("ck-10-pair-read-when-the-hold-fires", "numbered_overlay/controller.py",
    "                number=number, spoken=spoken, trace_id=trace_id,\n"
    "                armed_pair=armed_pair, gesture=gesture,\n",
    "                number=number, spoken=spoken, trace_id=trace_id,\n"
    "                armed_pair=(machine.overlay_session_id,\n"
    "                            machine.paint_generation), gesture=gesture,\n",
    "test_ck_10_hold_across_a_new_paint_generation_clicks_nothing",
    suite=CK_10)

# ---------------------------------------------------------------------------
# CK-11
# ---------------------------------------------------------------------------
CK_11 = sel(_CK, "test_ck_11_second_held_click_n_replaces_the_first",
            "test_ck_9_held_click_n_answered_by_a_router_notice_shows_that_reason")
# CK-11: a second held click cancels the first hold. Mutant: the first hold
# is not cancelled, so both fire. Caught by: test_ck_11[still_in_flight] at
# len(_notices(b, mark)) == 1 or the first notice's spoken_name;
# test_ck_11[painted] at the one-click list (item 1 is clicked too).
add("ck-11-first-hold-not-cancelled", "numbered_overlay/controller.py",
    "        existing = getattr(self, \"_overlay_hold_timer\", None)\n"
    "        if existing is not None:\n"
    "            existing.cancel()\n",
    "        existing = getattr(self, \"_overlay_hold_timer\", None)\n"
    "        if existing is not None:\n"
    "            pass\n",
    "test_ck_11_second_held_click_n_replaces_the_first",
    suite=CK_11)


# ===========================================================================
# Round 3 (CK-16, CK-23, CK-40, CK-44, CK-45, CK-46, CK-49; AO-16, AO-17,
# AO-23). David confirmed these on 2026-11-04.
# ===========================================================================

# Inserted into forward_click_element by the CK-44 and CK-45 mutants: the
# plausible wrong change is to hand a failure notice back to the caller as a
# deferred "nothing was clicked" result, as not_found is, so that without
# the safety word the words are typed and no notice shows.
def _deferred_return(indent, reason, snapshot_id="None"):
    pad = " " * indent
    return (
        f"{pad}if defer_failure_notice:\n"
        f"{pad}    return dict(\n"
        f"{pad}        outcome=\"execution_failed\", reason={reason},\n"
        f"{pad}        matched_name=None, matched_names=(),\n"
        f"{pad}        spoken_name=spoken, snapshot_id={snapshot_id},\n"
        f"{pad}        trace_id=trace_id,\n"
        f"{pad}    )\n"
    )


# ---------------------------------------------------------------------------
# CK-16
# ---------------------------------------------------------------------------
CK_16 = sel(_CK, "test_ck_16_show_numbers_while_settling_walks_fresh_at_once",
            "test_ck_13_click_n_while_settling_shows_numbers_updating")
_CK_16_ARM = (
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            # The user got impatient and asked out loud. Read now instead of\n"
    "            # waiting for the application to settle.\n"
)
# CK-16: 'show numbers' while settling unpins and walks fresh at once.
# Mutant: the request is ignored, so the machine waits for the settle read.
# Caught by: test_ck_16 at the unpin_snapshot next_frame (1 s limit).
add("ck-16-settling-show-numbers-ignored", "click_overlay_state.py",
    _CK_16_ARM,
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            return ApplyResult(OverlayOutcome.NO_OP)\n"
    "            # The user got impatient and asked out loud. Read now instead of\n"
    "            # waiting for the application to settle.\n",
    "test_ck_16_show_numbers_while_settling_walks_fresh_at_once",
    suite=CK_16)
# CK-16 (b). Mutant: the old snapshot is not unpinned before the fresh walk.
# Caught by: test_ck_16 at the unpin_snapshot next_frame.
add("ck-16-settling-show-numbers-keeps-pin", "click_overlay_state.py",
    "            effects: list[Effect] = [self._cancel_timer()]\n"
    "            unpin = self._unpin_current()\n"
    "            if unpin is not None:\n"
    "                effects.append(unpin)\n"
    "            self.pinned_snapshot_id = None\n"
    "            self._bump_generation()\n"
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            effects.append(self._dispatch_build(BuildReason.SHOW_NUMBERS))\n",
    "            effects: list[Effect] = [self._cancel_timer()]\n"
    "            self.pinned_snapshot_id = None\n"
    "            self._bump_generation()\n"
    "            self.state = OverlayState.WALK_IN_FLIGHT\n"
    "            effects.append(self._dispatch_build(BuildReason.SHOW_NUMBERS))\n",
    "test_ck_16_show_numbers_while_settling_walks_fresh_at_once",
    suite=CK_16)

# ---------------------------------------------------------------------------
# CK-23
# ---------------------------------------------------------------------------
CK_23 = sel(_CK,
            "test_ck_23_moved_or_closed_target_refusal_rewalks_and_repaints",
            "test_ck_24_verification_timeout_refusal_starts_no_rewalk")
# CK-23: a target_moved_offscreen, popup_closed or taskbar_closed refusal
# re-walks and repaints. Mutant: each reason in turn leaves the re-walk set.
# Caught by: test_ck_23[<reason>] at the start_overlay_walk next_frame.
add("ck-23-moved-offscreen-no-rewalk", "numbered_overlay/helpers.py",
    "        \"bounds_stale\",\n"
    "        \"target_moved_offscreen\",\n",
    "        \"bounds_stale\",\n",
    "test_ck_23_moved_or_closed_target_refusal_rewalks_and_repaints",
    suite=CK_23)
add("ck-23-popup-closed-no-rewalk", "numbered_overlay/helpers.py",
    "        \"target_moved_offscreen\",\n"
    "        \"popup_closed\",\n",
    "        \"target_moved_offscreen\",\n",
    "test_ck_23_moved_or_closed_target_refusal_rewalks_and_repaints",
    suite=CK_23)
add("ck-23-taskbar-closed-no-rewalk", "numbered_overlay/helpers.py",
    "        \"popup_closed\",\n"
    "        \"taskbar_closed\",\n",
    "        \"popup_closed\",\n",
    "test_ck_23_moved_or_closed_target_refusal_rewalks_and_repaints",
    suite=CK_23)

# ---------------------------------------------------------------------------
# CK-40
# ---------------------------------------------------------------------------
CK_40 = sel(_CK,
            "test_ck_40_gui_badge_click_resolves_in_any_state",
            "test_ck_40_gui_badge_click_skips_the_renumber_guard",
            "test_ck_40_gui_badge_click_is_refused_while_a_click_is_in_flight",
            "test_ck_37_gui_badge_click_sends_click_snapshot_item")
_CK_40_FOUND = (
    "            if result.outcome is ResolveOutcome.FOUND:\n"
    "                # wh-n29v.95: dispatch the real overlay-item click. The resolver\n"
)
# CK-40: a GUI badge click resolves in any state; the state router does not
# apply. Mutant: the click is dispatched only while painted. Caught by:
# test_ck_40_gui_badge_click_resolves_in_any_state[*] at the
# click_snapshot_item next_frame (1 s limit); CK-37 (painted) stays green.
add("ck-40-gui-click-only-while-painted", "numbered_overlay/controller.py",
    _CK_40_FOUND,
    "            if (result.outcome is ResolveOutcome.FOUND\n"
    "                    and self.click_overlay_state.state.value == \"painted\"):\n"
    "                # wh-n29v.95: dispatch the real overlay-item click. The resolver\n",
    "test_ck_40_gui_badge_click_resolves_in_any_state",
    suite=CK_40)
# CK-40 (b). Mutant: the GUI number is resolved against the machine's pin,
# not the snapshot the GUI named. Caught by: test_ck_40[refresh_in_flight]
# at the item_id assertion (snap-2's item, not snap-1's).
add("ck-40-gui-click-resolves-against-pin", "numbered_overlay/controller.py",
    "            result = resolve_display_number(\n"
    "                self.click_snapshot_summary_cache,\n"
    "                ev.snapshot_id,\n"
    "                ev.display_number,\n",
    "            result = resolve_display_number(\n"
    "                self.click_snapshot_summary_cache,\n"
    "                (self.click_overlay_state.pinned_snapshot_id\n"
    "                 or ev.snapshot_id),\n"
    "                ev.display_number,\n",
    "test_ck_40_gui_badge_click_resolves_in_any_state",
    suite=CK_40)
# CK-40 (c). Mutant: the renumber guard is applied to a GUI click too.
# Caught by: test_ck_40_gui_badge_click_skips_the_renumber_guard at the
# click_snapshot_item next_frame.
add("ck-40-gui-click-renumber-guarded", "numbered_overlay/controller.py",
    _CK_40_FOUND,
    "            if (result.outcome is ResolveOutcome.FOUND\n"
    "                    and self._overlay_renumber_click_safe(\n"
    "                        ev.display_number, ev.snapshot_id)):\n"
    "                # wh-n29v.95: dispatch the real overlay-item click. The resolver\n",
    "test_ck_40_gui_badge_click_skips_the_renumber_guard",
    suite=CK_40)
# CK-40 (d). Mutant: a GUI click bypasses the in-flight guard (the marker is
# dropped before the GUI dispatch). Caught by:
# test_ck_40_gui_badge_click_is_refused_while_a_click_is_in_flight at the
# show_click_notice next_gui.
add("ck-40-gui-click-skips-in-flight-guard", "numbered_overlay/controller.py",
    "                self._dispatch_snapshot_item_click(\n"
    "                    snapshot_id=ev.snapshot_id,\n",
    "                self._overlay_click_in_flight = None\n"
    "                self._dispatch_snapshot_item_click(\n"
    "                    snapshot_id=ev.snapshot_id,\n",
    "test_ck_40_gui_badge_click_is_refused_while_a_click_is_in_flight",
    suite=CK_40)

# ---------------------------------------------------------------------------
# CK-44
# ---------------------------------------------------------------------------
CK_44 = sel(_CK,
            "test_ck_44_failed_by_name_click_shows_notice_and_types_nothing",
            "test_ck_42_by_name_miss_without_safety_word_types_the_words")
# CK-44: a timed-out, unsent or malformed by-name click shows its notice and
# types nothing, with or without the safety word. Mutant: the timeout is
# deferred to the caller like not_found. Caught by:
# test_ck_44[timeout-plain] at _typed == [] (the words are typed).
add("ck-44-timeout-deferred", "numbered_overlay/controller.py",
    "                \"click_element: no reply within %dms; emitting \"\n"
    "                \"execution_failed:timeout (trace_id=%s)\",\n"
    "                timeout_ms, trace_id,\n"
    "            )\n",
    "                \"click_element: no reply within %dms; emitting \"\n"
    "                \"execution_failed:timeout (trace_id=%s)\",\n"
    "                timeout_ms, trace_id,\n"
    "            )\n"
    + _deferred_return(12, "\"timeout\""),
    "test_ck_44_failed_by_name_click_shows_notice_and_types_nothing",
    suite=CK_44)
# CK-44 (b). Mutant: a send failure is deferred. Caught by:
# test_ck_44[send_request_failed-plain] at the show_click_notice next_gui
# (the typed words fail to send too, so the notice check is what fires).
add("ck-44-send-failure-deferred", "numbered_overlay/controller.py",
    "                \"click_element: send_request failed (trace_id=%s): %s\",\n"
    "                trace_id, exc, exc_info=True,\n"
    "            )\n",
    "                \"click_element: send_request failed (trace_id=%s): %s\",\n"
    "                trace_id, exc, exc_info=True,\n"
    "            )\n"
    + _deferred_return(12, "\"send_request_failed\""),
    "test_ck_44_failed_by_name_click_shows_notice_and_types_nothing",
    suite=CK_44)
# CK-44 (c). Mutant: a malformed reply is deferred. Caught by:
# test_ck_44[malformed_response-plain] at _typed == [].
add("ck-44-malformed-deferred", "numbered_overlay/controller.py",
    "                \"click_element: malformed response (trace_id=%s): %s; \"\n"
    "                \"payload_shape=%s\", trace_id, exc, payload_shape,\n"
    "            )\n",
    "                \"click_element: malformed response (trace_id=%s): %s; \"\n"
    "                \"payload_shape=%s\", trace_id, exc, payload_shape,\n"
    "            )\n"
    + _deferred_return(12, "\"malformed_response\""),
    "test_ck_44_failed_by_name_click_shows_notice_and_types_nothing",
    suite=CK_44)

# ---------------------------------------------------------------------------
# CK-45
# ---------------------------------------------------------------------------
CK_45 = sel(_CK, "test_ck_45_click_n_notice_types_nothing",
            "test_ck_4_click_n_while_painted_without_badge_n_shows_notice")
# CK-45: 'click N' answered by a router notice types nothing, with or
# without the safety word. Mutant: the router notice is deferred to the
# caller. Caught by: test_ck_45[no_badge_numbered|numbers_not_showing|
# numbers_updating-plain] at the show_click_notice next_gui.
add("ck-45-router-notice-deferred", "numbered_overlay/controller.py",
    "                parsed_number, decision.reason, overlay_state_label, trace_id,\n"
    "            )\n",
    "                parsed_number, decision.reason, overlay_state_label, trace_id,\n"
    "            )\n"
    + _deferred_return(12, "decision.reason", "decision.snapshot_id"),
    "test_ck_45_click_n_notice_types_nothing",
    suite=CK_45)
# CK-45 (b). Mutant: the renumber notice is deferred. Caught by:
# test_ck_45[overlay_numbers_changed-plain] at the show_click_notice next_gui.
add("ck-45-renumber-notice-deferred", "numbered_overlay/controller.py",
    "                    \"identity changed across a repaint swap \"\n"
    "                    \"(state=%s, trace_id=%s)\",\n"
    "                    parsed_number, overlay_state_label, trace_id,\n"
    "                )\n",
    "                    \"identity changed across a repaint swap \"\n"
    "                    \"(state=%s, trace_id=%s)\",\n"
    "                    parsed_number, overlay_state_label, trace_id,\n"
    "                )\n"
    + _deferred_return(16, "OVERLAY_NUMBERS_CHANGED", "decision.snapshot_id"),
    "test_ck_45_click_n_notice_types_nothing",
    suite=CK_45)

# ---------------------------------------------------------------------------
# CK-46
# ---------------------------------------------------------------------------
CK_46 = sel(_CK,
            "test_ck_46_unusable_target_without_safety_word_is_typed",
            "test_ck_46_unusable_target_with_safety_word_shows_notice",
            "test_ck_1_click_n_while_closed_is_a_by_name_click")
# CK-46: an empty or article-only target is typed without the safety word
# and gives the 'Say the name or number...' notice with it. Mutant: the
# unusable target is swallowed (no StepFailed). Caught by: test_ck_46 with
# and without the safety word (next_gui show_notification; _next_typed).
add("ck-46-unusable-target-swallowed", "speech/actions.py",
    "            raise StepFailed(\"Say the name or number of what to click.\")\n",
    "            return None\n",
    "test_ck_46_unusable_target_without_safety_word_is_typed",
    "test_ck_46_unusable_target_with_safety_word_shows_notice",
    suite=CK_46)
# CK-46 (b). Mutant: a lone "the" is kept as the name. Caught by:
# test_ck_46[the] in both tests (a click_element goes out instead).
add("ck-46-lone-article-kept", "speech/click_parser.py",
    "        if tokens[0] == \"the\":\n"
    "            tokens = tokens[1:]\n",
    "        if tokens[0] == \"the\" and len(tokens) > 1:\n"
    "            tokens = tokens[1:]\n",
    "test_ck_46_unusable_target_without_safety_word_is_typed",
    "test_ck_46_unusable_target_with_safety_word_shows_notice",
    suite=CK_46)

# ---------------------------------------------------------------------------
# CK-49
# ---------------------------------------------------------------------------
CK_49 = sel(_CK, "test_ck_49_badge_click_skips_the_first_use_browser_hint",
            "test_ck_3_click_n_while_painted_sends_click_snapshot_item")
# CK-49: a 'click N' routed to a badge click shows no first-use hint.
# Mutant: the hint check also runs before the badge click. Caught by:
# test_ck_49 at the gui_since(click_first_use_hint) == [] assertion.
add("ck-49-badge-click-shows-hint", "numbered_overlay/controller.py",
    "            self._dispatch_snapshot_item_click(\n"
    "                snapshot_id=decision.snapshot_id,\n"
    "                item_id=decision.item_id,\n"
    "                trace_id=trace_id,\n"
    "                gesture=getattr(query, \"gesture\", None),\n"
    "            )\n",
    "            await self._maybe_show_first_use_hint(trace_id)\n"
    "            self._dispatch_snapshot_item_click(\n"
    "                snapshot_id=decision.snapshot_id,\n"
    "                item_id=decision.item_id,\n"
    "                trace_id=trace_id,\n"
    "                gesture=getattr(query, \"gesture\", None),\n"
    "            )\n",
    "test_ck_49_badge_click_skips_the_first_use_browser_hint",
    suite=CK_49)

# ---------------------------------------------------------------------------
# AO-16
# ---------------------------------------------------------------------------
AO_16 = sel(_AO,
            "test_ao_16_show_numbers_after_auto_open_walks_the_whole_window",
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist")
# AO-16: 'show numbers' after a painted auto-open is a full refresh walk.
# Mutant: the painted SHOW_NUMBERS refresh reuses the AUTO_OPEN build, so it
# repaints the old snapshot with show_numbered_overlay. Caught by: test_ao_16
# at the start_overlay_walk next_frame.
add("ao-16-painted-refresh-reuses-auto-open", "click_overlay_state.py",
    "    def _on_painted(self, event: OverlayEvent) -> ApplyResult:\n"
    "        kind = event.kind\n"
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            return self._refresh(BuildReason.REFRESH)\n",
    "    def _on_painted(self, event: OverlayEvent) -> ApplyResult:\n"
    "        kind = event.kind\n"
    "        if kind is OverlayEventKind.SHOW_NUMBERS:\n"
    "            return self._refresh(BuildReason.AUTO_OPEN)\n",
    "test_ao_16_show_numbers_after_auto_open_walks_the_whole_window",
    suite=AO_16)

# ---------------------------------------------------------------------------
# AO-17
# ---------------------------------------------------------------------------
AO_17 = sel(_AO,
            "test_ao_17_focus_rewalk_after_auto_open_paints_the_whole_window",
            "test_ao_12_click_n_on_auto_open_clicks_that_finalist")
# AO-17: a focus or menu re-walk after an auto-open is unfiltered. Mutant:
# the painted FOCUS_CHANGE refresh reuses the AUTO_OPEN build. Caught by:
# test_ao_17[*] at the start_overlay_walk next_frame.
add("ao-17-painted-focus-refresh-reuses-auto-open", "click_overlay_state.py",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.REFRESH)\n"
    "        if kind is OverlayEventKind.CLICK_COMPLETE:\n"
    "            # wh-overlay-slow-uia-stale-badges.1. The flag is OFF by default,\n",
    "        if kind is OverlayEventKind.FOCUS_CHANGE:\n"
    "            if self._event_dismisses_numbers(event):\n"
    "                return self._hide_to_closed()\n"
    "            return self._refresh(BuildReason.AUTO_OPEN)\n"
    "        if kind is OverlayEventKind.CLICK_COMPLETE:\n"
    "            # wh-overlay-slow-uia-stale-badges.1. The flag is OFF by default,\n",
    "test_ao_17_focus_rewalk_after_auto_open_paints_the_whole_window",
    suite=AO_17)

# ---------------------------------------------------------------------------
# AO-23
# ---------------------------------------------------------------------------
AO_23 = sel(_AO,
            "test_ao_23_closed_auto_open_leaves_no_filter_for_later_builds",
            "test_ao_2_auto_open_request_reuses_click_snapshot_with_filter")
# AO-23: after an auto-open closes, no later build carries its filter.
# EXPECTED SURVIVOR (reason 3, unobservable at the boundary). The stash the
# reconcile clears can never reach a request: (1) only an AUTO_OPEN build
# reads it, and _overlay_dispatch_build pops it with no await between the
# apply that writes it and the take, before the pre-send staleness check;
# (2) the take returns it only for the exact (overlay_session_id,
# paint_generation) pair, and every later session has a new session id;
# (3) every later auto-open writes its own stash first. So the reconcile is
# defence in depth: removing it changes no frame or GUI message.
# Mutant: the reconcile helper never clears the stash.
add("ao-23-reconcile-keeps-stash", "numbered_overlay/controller.py",
    "        if machine is not None and machine.state is OverlayState.CLOSED:\n"
    "            self._overlay_auto_open_filter = None\n",
    "        if machine is not None and machine.state is OverlayState.CLOSED:\n"
    "            pass\n",
    "test_ao_23_closed_auto_open_leaves_no_filter_for_later_builds",
    survives=('Needs a stash that outlives its build: no reachable sequence lets a later take read it'),
    suite=AO_23)
# AO-23 (b). EXPECTED SURVIVOR, same reason. Mutant: the take ignores the
# pair key, so a stash for another build would be used. No reachable
# sequence leaves a stash from an earlier build at the next take.
add("ao-23-take-ignores-pair", "numbered_overlay/controller.py",
    "        pair, item_ids = stash\n"
    "        if pair != (sid, gen):\n"
    "            return None\n",
    "        pair, item_ids = stash\n",
    "test_ao_23_closed_auto_open_leaves_no_filter_for_later_builds",
    survives=('Needs a stash that outlives its build: no reachable sequence lets a later take read it'),
    suite=AO_23)


# ===========================================================================
# Round 4: AO-30 and RW-14 (David confirmed both on 2026-11-06, "keep today's
# behavior"; tests of commit ec9ba37f; the AO-30 tests were strengthened in
# the commit that adds this block, so a stalled utterance fails at its
# frames assertion)
# ===========================================================================

# The two-key config gate as main.py writes it, and the three mutants of it:
# the gate removed, and the gate narrowed to one key.
_GATE = (
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
)
_GATE_REMOVED = "        if False:\n"
_GATE_CLICK_ONLY = (
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "        ):\n"
)
_GATE_OVERLAY_ONLY = (
    "        if not (\n"
    "            self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
)
# The first lines of the show/hide numbers gate's own body, which make the
# pattern match only that gate (main.py handle_overlay_command).
_SHOW_HIDE_BODY = (
    "            logger.debug(\n"
    "                \"overlay: %r ignored, numbered overlay not active \"\n"
)

# ---------------------------------------------------------------------------
# AO-30
# ---------------------------------------------------------------------------
_AO_30_SILENT = "test_ao_30_show_or_hide_numbers_with_overlay_off_is_a_silent_command"
_AO_30_CONTROL = "test_ao_30_control_overlay_on_show_and_hide_reach_the_boundary"
_AO_30_GRID = "test_ao_30_show_numbers_with_overlay_off_leaves_the_grid_open"
AO_30 = sel(_AO, _AO_30_SILENT, _AO_30_CONTROL, _AO_30_GRID)
# AO-30: show/hide numbers with the overlay or clicking off is a silent
# command. Mutant: the config gate is removed, so 'show numbers' runs the
# machine. Caught by: test_ao_30_show_or_hide[*-show numbers] and
# test_ao_30_..._leaves_the_grid_open, both at `frames == []` (a
# start_overlay_walk frame; the grid test's GUI log also holds a clear_grid,
# asserted next). The hide params pass under this mutant: a hide while closed
# sends nothing either way.
add("ao-30-show-hide-gate-removed", "numbered_overlay/controller.py",
    _GATE + _SHOW_HIDE_BODY,
    _GATE_REMOVED + _SHOW_HIDE_BODY,
    _AO_30_SILENT, _AO_30_GRID,
    suite=AO_30)
# AO-30: Mutant: the gate checks click.enabled only, so overlay_enabled=false
# no longer stops it. Caught by: test_ao_30_show_or_hide[overlay-disabled-
# show numbers] and the grid test.
add("ao-30-show-hide-gate-click-key-only", "numbered_overlay/controller.py",
    _GATE + _SHOW_HIDE_BODY,
    _GATE_CLICK_ONLY + _SHOW_HIDE_BODY,
    _AO_30_SILENT, _AO_30_GRID,
    suite=AO_30)
# AO-30: Mutant: the gate checks overlay_enabled_effective only; a valid
# enabled=false leaves that True. Caught by: test_ao_30_show_or_hide[click-
# disabled-show numbers] (a start_overlay_walk frame).
add("ao-30-show-hide-gate-overlay-key-only", "numbered_overlay/controller.py",
    _GATE + _SHOW_HIDE_BODY,
    _GATE_OVERLAY_ONLY + _SHOW_HIDE_BODY,
    _AO_30_SILENT,
    suite=AO_30)
# AO-30: Mutant: the gate shows the disabled_by_config notice, as 'show grid'
# does with the grid off. Caught by: test_ao_30_show_or_hide (all four params:
# a show_click_notice message) and the grid test.
add("ao-30-show-hide-gate-shows-a-notice", "numbered_overlay/controller.py",
    "                self._host.click_config.overlay_invalid_key, trace_id,\n"
    "            )\n"
    "            return\n"
    "\n"
    "        if command == \"show\":\n",
    "                self._host.click_config.overlay_invalid_key, trace_id,\n"
    "            )\n"
    "            self.forward_click_notice(\n"
    "                outcome=\"execution_failed\",\n"
    "                reason=\"disabled_by_config\",\n"
    "                matched_name=None,\n"
    "                matched_names=(),\n"
    "                spoken_name=\"\",\n"
    "                snapshot_id=None,\n"
    "                trace_id=trace_id,\n"
    "            )\n"
    "            return\n"
    "\n"
    "        if command == \"show\":\n",
    _AO_30_SILENT, _AO_30_GRID,
    suite=AO_30)
# AO-30: the utterance is a command, not dictation. Mutant: the show-numbers
# pattern requires the safety word, so a plain 'show numbers' falls through
# to the next rule that matches it, the show-app rule, which sends Input an
# activate_window request. Caught by: test_ao_30_show_or_hide[*-show numbers]
# and the grid test at `frames == []` (['activate_window']), and the control
# test at `frames == ["start_overlay_walk"]`. Before _say_bounded these tests
# waited on that unanswered request and failed on a TimeoutError instead.
add("ao-30-show-numbers-needs-the-safety-word", "speech/config/patterns.toml",
    "doc_id = \"show-numbers\"\n"
    "requires_hotword = false\n",
    "doc_id = \"show-numbers\"\n"
    "requires_hotword = true\n",
    _AO_30_SILENT, _AO_30_CONTROL, _AO_30_GRID,
    suite=AO_30)
# AO-30: Mutant: the same for 'hide numbers'; no other rule matches it, so
# it is typed as dictation. Caught by: test_ao_30_show_or_hide[*-hide
# numbers] and the control test at `typed == []` (['hide numbers']).
add("ao-30-hide-numbers-needs-the-safety-word", "speech/config/patterns.toml",
    "doc_id = \"hide-numbers\"\n"
    "requires_hotword = false\n",
    "doc_id = \"hide-numbers\"\n"
    "requires_hotword = true\n",
    _AO_30_SILENT, _AO_30_CONTROL,
    suite=AO_30)

# ---------------------------------------------------------------------------
# RW-14
# ---------------------------------------------------------------------------
_RW_14_OFF = "test_rw_14_overlay_off_registers_no_hook_and_drops_tree_changes"
_RW_14_CONTROL = "test_rw_14_control_overlay_on_each_trigger_walks"
RW_14 = sel(_T, _RW_14_OFF, _RW_14_CONTROL)
_HOOK_START_BODY = (
    "            logger.info(\n"
    "                \"overlay: focus hooks not started (numbered overlay not \"\n"
)
# RW-14: with clicking or the overlay off, no re-walk event sends anything.
# Foreground, menu and destroy events reach Logic only through the Win32
# focus hook, so the observable half of the rule is that the hook is never
# registered. Mutant: the hook-start gate is removed. Caught by:
# test_rw_14_overlay_off (both params: FakeFocusHooks.instances == []).
add("rw-14-focus-hooks-start-gate-removed", "numbered_overlay/controller.py",
    _GATE + _HOOK_START_BODY,
    _GATE_REMOVED + _HOOK_START_BODY,
    _RW_14_OFF,
    suite=RW_14)
# RW-14: Mutant: the hook-start gate checks click.enabled only. Caught by:
# test_rw_14_overlay_off[overlay-disabled].
add("rw-14-focus-hooks-start-gate-click-key-only", "numbered_overlay/controller.py",
    _GATE + _HOOK_START_BODY,
    _GATE_CLICK_ONLY + _HOOK_START_BODY,
    _RW_14_OFF,
    suite=RW_14)
# RW-14: Mutant: the hook-start gate checks overlay_enabled_effective only.
# Caught by: test_rw_14_overlay_off[click-disabled].
add("rw-14-focus-hooks-start-gate-overlay-key-only", "numbered_overlay/controller.py",
    _GATE + _HOOK_START_BODY,
    _GATE_OVERLAY_ONLY + _HOOK_START_BODY,
    _RW_14_OFF,
    suite=RW_14)
# EXPECTED SURVIVORS (reason 3) for the five callback gates the RW-14 row
# cites. Each is defence in depth behind two other gates:
#   * the foreground, menu pop-up and destroy callbacks are called only by
#     the Win32 focus hook, and _start_overlay_focus_hooks registers no hook
#     in either config, so Windows never calls them;
#   * the settle refire is armed only from a coalesced foreground or menu
#     event, and never while the machine is closed;
#   * the tree-change callback is reachable (the GUI process sends the
#     message on its own queue), but the machine stays closed in either
#     config: the show/hide gate (handle_overlay_command), the click gate
#     (forward_click_element returns before any click_element when enabled
#     is false) and the auto-open gate (overlay_enabled_effective) keep it
#     there, and decide_tree_change drops every event while closed (RW-21).
# Each mutant removes the gate. A gate narrowed to one key changes the code
# path only in a config where the removed gate also does, so it cannot be
# seen where the removal is not seen; those narrowings are not listed.
add("rw-14-foreground-gate-removed", "numbered_overlay/controller.py",
    _GATE +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import map_foreground_event\n",
    _GATE_REMOVED +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import map_foreground_event\n",
    _RW_14_OFF,
    survives=('Needs the Win32 focus hook registered: no config registers it, so Windows never calls this callback'),
    suite=RW_14)
add("rw-14-menu-popup-gate-removed", "numbered_overlay/controller.py",
    _GATE +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import (\n"
    "            EVENT_SYSTEM_MENUPOPUPEND,\n",
    _GATE_REMOVED +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import (\n"
    "            EVENT_SYSTEM_MENUPOPUPEND,\n",
    _RW_14_OFF,
    survives=('Needs the Win32 focus hook registered: no config registers it, so Windows never calls this callback'),
    suite=RW_14)
add("rw-14-tree-change-gate-removed", "numbered_overlay/controller.py",
    _GATE +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import decide_tree_change\n",
    _GATE_REMOVED +
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import decide_tree_change\n",
    _RW_14_OFF,
    survives=('Needs an open overlay with the overlay or clicking off: three other gates keep the machine closed'),
    suite=RW_14)
add("rw-14-settle-refire-gate-removed", "numbered_overlay/controller.py",
    "        self._overlay_settle_handle = None\n" + _GATE +
    "            return\n",
    "        self._overlay_settle_handle = None\n" + _GATE_REMOVED +
    "            return\n",
    _RW_14_OFF,
    survives=('Needs a coalesced foreground or menu event: the focus hook is not registered in either config'),
    suite=RW_14)
add("rw-14-destroy-gate-removed", "numbered_overlay/controller.py",
    _GATE +
    "            return\n"
    "        # wh-n29v.24.2: drop a destroy callback that arrives after the transient\n",
    _GATE_REMOVED +
    "            return\n"
    "        # wh-n29v.24.2: drop a destroy callback that arrives after the transient\n",
    _RW_14_OFF,
    survives=('Needs the Win32 focus hook registered: no config registers it, so Windows never calls this callback'),
    suite=RW_14)


# ===========================================================================
# Stage C: the walking cue and the read limit (LC-40..LC-43, AO-33, AO-34)
# ===========================================================================
_CTL = "numbered_overlay/controller.py"
_CUE_LC = sel(_LC, "test_lc_40_walk_cue_stays_on_until_the_paint",
              "test_lc_41_failed_walk_turns_the_walk_cue_off",
              "test_lc_42_walk_cue_carries_the_whole_walk_bound",
              "test_lc_43_a_walk_waits_for_the_screen_read_limit")
_CUE_AO = sel(_AO, "test_ao_33_auto_open_walk_cue_carries_two_reply_limits",
              "test_ao_34_auto_open_waits_only_the_click_reply_limit")

# LC-40: a good build sends no overlay_walk_cue with active false.
add("lc-40-good-build-turns-the-cue-off", _CTL,
    "            if not build_ok:\n"
    "                self._put_overlay_walk_cue(False, trace_id=trace_id)\n",
    "            if True:\n"
    "                self._put_overlay_walk_cue(False, trace_id=trace_id)\n",
    "test_lc_40_walk_cue_stays_on_until_the_paint",
    suite=_CUE_LC)
# LC-41: a failed or timed-out walk turns the cue off.
add("lc-41-failed-walk-keeps-the-cue", _CTL,
    "            if not build_ok:\n"
    "                self._put_overlay_walk_cue(False, trace_id=trace_id)\n",
    "            if False:\n"
    "                self._put_overlay_walk_cue(False, trace_id=trace_id)\n",
    "test_lc_41_failed_walk_turns_the_walk_cue_off",
    suite=_CUE_LC)
# LC-42: the walk cue bound carries the dictation sentence wait.
add("lc-42-walk-cue-bound-drops-the-sentence-wait", _CTL,
    "            cue_bound_ms += int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)\n",
    "            cue_bound_ms += 0\n",
    "test_lc_42_walk_cue_carries_the_whole_walk_bound",
    suite=_CUE_LC)
# LC-43: a walk waits screen_read_timeout_ms. Mutant: it waits the click
# reply limit, so a reply at 5 s is discarded (and the cue bound shrinks).
add("lc-43-walk-waits-the-click-reply-limit", _CTL,
    "        else:\n"
    "            timeout_ms = self._host.click_config.screen_read_timeout_ms\n"
    "        timeout_s = max(timeout_ms / 1000.0, 0.1)\n",
    "        else:\n"
    "            timeout_ms = self._host.click_config.response_timeout_ms\n"
    "        timeout_s = max(timeout_ms / 1000.0, 0.1)\n",
    "test_lc_42_walk_cue_carries_the_whole_walk_bound",
    "test_lc_43_a_walk_waits_for_the_screen_read_limit",
    suite=_CUE_LC)
# AO-33: the auto-open cue bound has no sentence wait.
add("ao-33-auto-open-cue-bound-adds-the-sentence-wait", _CTL,
    "        if effect.build_reason is not BuildReason.AUTO_OPEN:\n"
    "            cue_bound_ms += int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)\n",
    "        if True:\n"
    "            cue_bound_ms += int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)\n",
    "test_ao_33_auto_open_walk_cue_carries_two_reply_limits",
    suite=_CUE_AO)
# AO-34: an auto-open waits response_timeout_ms. Mutant: it waits the screen
# read limit, so no notice comes 3.5 s after the request (and the cue bound
# grows).
add("ao-34-auto-open-waits-the-screen-read-limit", _CTL,
    "        if effect.build_reason is BuildReason.AUTO_OPEN:\n"
    "            timeout_ms = self._host.click_config.response_timeout_ms\n",
    "        if effect.build_reason is BuildReason.AUTO_OPEN:\n"
    "            timeout_ms = self._host.click_config.screen_read_timeout_ms\n",
    "test_ao_33_auto_open_walk_cue_carries_two_reply_limits",
    "test_ao_34_auto_open_waits_only_the_click_reply_limit",
    suite=_CUE_AO)


# CK-50: the settle walk carries settle true and the clicked snapshot id; a
# show-numbers walk carries neither field.
_CK_50 = sel(_CK, "test_ck_50_settle_walk_names_the_clicked_snapshot",
             "test_ck_16_show_numbers_while_settling_walks_fresh_at_once")
add("ck-50-settle-walk-compares-nothing", _CTL,
    '                params["compare_snapshot_id"] = effect.snapshot_id or ""\n',
    '                params["compare_snapshot_id"] = ""\n',
    "test_ck_50_settle_walk_names_the_clicked_snapshot",
    suite=_CK_50)
add("ck-50-every-walk-is-a-settle-walk", _CTL,
    "            if effect.build_reason is BuildReason.SETTLE:\n"
    "                # wh-overlay-slow-uia-stale-badges.2: the post-click re-read.\n",
    "            if True:\n"
    "                # wh-overlay-slow-uia-stale-badges.2: the post-click re-read.\n",
    "test_ck_50_settle_walk_names_the_clicked_snapshot",
    "test_ck_16_show_numbers_while_settling_walks_fresh_at_once",
    suite=_CK_50)
add("ck-50-settle-walk-drops-the-settle-flag", _CTL,
    '                params["settle"] = True\n',
    '                params["settle"] = False\n',
    "test_ck_50_settle_walk_names_the_clicked_snapshot",
    "test_ck_16_show_numbers_while_settling_walks_fresh_at_once",
    suite=_CK_50)


# CK-29 (bookkeeping half): a late ok reply gets the on-time post-click step.
_CK_29B = sel(_CK, "test_ck_29_late_ok_reply_clears_like_an_on_time_ok",
              "test_ck_29_late_ok_reply_shows_no_notice")
add("ck-29-late-ok-is-read-as-a-refusal", "numbered_overlay/helpers.py",
    "                click_succeeded=True,\n"
    "                preclick_snapshot_id=fallback_snapshot_id,\n",
    "                click_succeeded=False,\n"
    "                preclick_snapshot_id=fallback_snapshot_id,\n",
    "test_ck_29_late_ok_reply_clears_like_an_on_time_ok",
    suite=_CK_29B)


# CK-24 (other reasons): disabled, COM and item_not_found refusals start no
# re-walk. Each mutant adds one reason to the re-walk list.
_CK_24B = sel(_CK, "test_ck_24_other_refusals_start_no_rewalk",
              "test_ck_24_verification_timeout_refusal_starts_no_rewalk")
add("ck-24-disabled-rewalks", "numbered_overlay/helpers.py",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n"
    "        \"disabled\",\n",
    "test_ck_24_other_refusals_start_no_rewalk",
    suite=_CK_24B)
add("ck-24-invoke-com-error-rewalks", "numbered_overlay/helpers.py",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n"
    "        \"invoke_com_error\",\n",
    "test_ck_24_other_refusals_start_no_rewalk",
    suite=_CK_24B)
add("ck-24-item-not-found-rewalks", "numbered_overlay/helpers.py",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n",
    "        \"bounds_invalid\",\n"
    "        \"bounds_stale\",\n"
    "        \"item_not_found\",\n",
    "test_ck_24_other_refusals_start_no_rewalk",
    suite=_CK_24B)


# CK-51 and LC-44: one fault raises one Windows error notification
# (wh-duplicate-error-popup). Each mutant logs Logic's own timeout line at
# ERROR, which raises a second notification.
_CK_51 = sel(_CK, "test_ck_51_unanswered_badge_click_raises_one_error_notification",
             "test_ck_25_unanswered_badge_click_shows_timeout_notice")
add("ck-51-click-timeout-logged-at-error", _CTL,
    "            logger.warning(\n"
    "                \"click_snapshot_item: no reply within %dms; \"\n",
    "            logger.error(\n"
    "                \"click_snapshot_item: no reply within %dms; \"\n",
    "test_ck_51_unanswered_badge_click_raises_one_error_notification",
    suite=_CK_51)
_LC_44 = sel(_LC, "test_lc_44_unanswered_walk_raises_one_error_notification",
             "test_lc_41_failed_walk_turns_the_walk_cue_off")
add("lc-44-walk-timeout-logged-at-error", _CTL,
    "            logger.warning(\n"
    "                \"overlay: %s no reply within %dms; feeding build_ok=False \"\n",
    "            logger.error(\n"
    "                \"overlay: %s no reply within %dms; feeding build_ok=False \"\n",
    "test_lc_44_unanswered_walk_raises_one_error_notification",
    suite=_LC_44)


# CK-52: with keep-numbers off, an ok reply in the click's session clears at
# once even when a refresh started between the click and the reply. Mutant:
# the early hide compares the whole pair, so the refresh's bump drops it.
_CK_52 = sel(_CK, "test_ck_52_ok_click_with_keep_numbers_off_clears_after_a_refresh_started",
             "test_ck_17_ok_click_with_keep_numbers_off_clears_and_unpins")
add("ck-52-early-hide-compares-the-whole-pair", _CTL,
    "            and overlay_dispatch_pair[0] == current_pair[0]\n",
    "            and overlay_dispatch_pair == current_pair\n",
    "test_ck_52_ok_click_with_keep_numbers_off_clears_after_a_refresh_started",
    suite=_CK_52)


# CK-53: keep-numbers on, an ok reply during a refresh re-walks once that
# refresh ends. Mutant: the pending post-click refresh is never recorded.
_CK_53 = sel(_CK, "test_ck_53_ok_click_during_a_refresh_rewalks_once_the_refresh_ends",
             "test_ck_18_ok_click_with_keep_numbers_on_refreshes_in_place")
add("ck-53-pending-postclick-refresh-not-recorded", _CTL,
    "            # (or drops it if the overlay is superseded / torn down first).\n"
    "            self._record_pending_postclick_refresh(current_pair)\n",
    "            # (or drops it if the overlay is superseded / torn down first).\n"
    "            pass\n",
    "test_ck_53_ok_click_during_a_refresh_rewalks_once_the_refresh_ends",
    suite=_CK_53)


# CK-54: a reply or a timeout ends the in-flight refusal. Mutant: the
# in-flight marker is never cleared, so every later badge click is refused.
_CK_54 = sel(_CK, "test_ck_54_next_badge_click_is_sent_after_the_first_is_answered",
             "test_ck_31_second_badge_click_while_first_in_flight_is_refused")
add("ck-54-in-flight-marker-never-cleared", _CTL,
    "        finally:\n"
    "            self._overlay_click_in_flight = None\n",
    "        finally:\n"
    "            pass\n",
    "test_ck_54_next_badge_click_is_sent_after_the_first_is_answered",
    suite=_CK_54)


# CK-55: a badge click and a lease renew carry the pair of the paint on
# screen across a failed, an in-flight and a painted refresh.
_CK_55 = sel(_CK, "test_ck_55_badge_click_and_lease_renew_carry_the_list_on_screen",
             "test_ck_32_badge_click_carries_the_visible_pair")
# Mutant: a failed refresh's fall-back records the machine's bumped pair.
add("ck-55-fall-back-records-the-machine-pair", _CTL,
    "                and machine.pinned_snapshot_id == recorded_pin\n",
    "                and False\n",
    "test_ck_55_badge_click_and_lease_renew_carry_the_list_on_screen",
    suite=_CK_55)
# Mutant: a refresh in flight forgets the pair of the list on screen.
add("ck-55-refresh-in-flight-forgets-the-pair", _CTL,
    "        elif machine.state in (\n"
    "            OverlayState.REFRESH_IN_FLIGHT,\n"
    "            OverlayState.POST_CLICK_SETTLING,\n",
    "        elif machine.state in (\n"
    "            OverlayState.POST_CLICK_SETTLING,\n",
    "test_ck_55_badge_click_and_lease_renew_carry_the_list_on_screen",
    suite=_CK_55)
# Mutant: the lease renew always carries the machine's pair.
add("ck-55-renew-carries-the-machine-pair", _CTL,
    "                visible if visible is not None\n",
    "                None if True\n",
    "test_ck_55_badge_click_and_lease_renew_carry_the_list_on_screen",
    suite=_CK_55)


# CK-56: 'click N' more than 3 s after a renumbering repaint clicks. Mutant:
# the click check never lets a swap expire.
_CK_56 = sel(_CK, "test_ck_56_click_n_after_the_renumber_window_clicks",
             "test_ck_34_click_n_after_a_renumbering_repaint_is_refused")
add("ck-56-renumber-swap-never-expires", _CTL,
    "        live = [\n"
    "            entry for entry in swaps\n"
    "            if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n",
    "        live = [\n"
    "            entry for entry in swaps\n"
    "            if now - entry[1] <= float(\"inf\")\n",
    "test_ck_56_click_n_after_the_renumber_window_clicks",
    suite=_CK_56)


# CK-57: every swap in a chain of repaints is checked in its own 3 s window
# (wh-overlay-slow-uia-stale-badges.4, findings .4.2.1). The two mutants are
# the ones tests/mutation_gate_renumber_guard_every_repaint.py names.
_CK_57 = sel(_CK, "test_ck_57_renumbering_anywhere_in_a_repaint_chain_is_refused",
             "test_ck_34_click_n_after_a_renumbering_repaint_is_refused")
# Mutant: the check compares badge N against the first prior alone.
add("ck-57-checks-only-the-first-prior", _CTL,
    "        for prior_id, _swap_t in live:\n",
    "        for prior_id, _swap_t in live[:1]:\n",
    "test_ck_57_renumbering_anywhere_in_a_repaint_chain_is_refused",
    suite=_CK_57)
# Mutant: every swap expires on the oldest swap's time.
add("ck-57-expiry-uses-the-oldest-swap", _CTL,
    "        live = [\n"
    "            entry for entry in swaps\n"
    "            if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n",
    "        live = [\n"
    "            entry for entry in swaps\n"
    "            if now - swaps[0][1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n",
    "test_ck_57_renumbering_anywhere_in_a_repaint_chain_is_refused",
    suite=_CK_57)


# LC-45: a failed refresh while the foreground cannot be read keeps the
# numbers (wh-overlay-failed-focus-read-keeps-numbers). Mutant: the unknown
# answer is read as "the window left" (the mutation
# tests/mutation_gate_failed_focus_read_clear.py names
# unknown-foreground-clears).
_LC_45 = sel(_LC, "test_lc_45_failed_refresh_with_no_readable_foreground_keeps_the_numbers",
             "test_lc_21_failed_refresh_on_same_window_keeps_the_numbers",
             "test_lc_22_failed_refresh_after_window_left_clears_and_closes")
add("lc-45-unknown-foreground-clears", _CTL,
    "        if self._overlay_refresh_visible_window_is_foreground(machine) "
    "is not False:\n",
    "        if self._overlay_refresh_visible_window_is_foreground(machine):\n",
    "test_lc_45_failed_refresh_with_no_readable_foreground_keeps_the_numbers",
    suite=_LC_45)


# LC-46: the unacknowledged-clear log line advises 'show numbers'. Mutant:
# the advice names 'hide numbers', which does nothing in the closed state.
_LC_46 = sel(_LC, "test_lc_46_unacknowledged_clear_log_advises_show_numbers",
             "test_lc_26_unacknowledged_clear_raises_an_error_notification")
add("lc-46-clear-fault-advises-hide-numbers", _CTL,
    "expires; say 'show numbers' to paint again. (trace_id=%s)",
    "expires; say 'hide numbers' to remove them. (trace_id=%s)",
    "test_lc_46_unacknowledged_clear_log_advises_show_numbers",
    suite=_LC_46)


# CK-58: settle on, a focus refresh between a badge click and its ok reply;
# a refresh that fails leaves the clicked list on screen, and Logic clears it
# (wh-overlay-slow-uia-stale-badges.15). Mutant: the reply at a bumped
# generation is dropped and no deferred clear is armed (the bug .15 states).
_CK_58 = sel(_CK, "test_ck_58_settle_click_reply_after_a_focus_refresh_clears_the_clicked_list",
             "test_ck_14_settle_clears_at_once_then_reads_and_repaints")
add("ck-58-bumped-reply-arms-no-deferred-clear", _CTL,
    "            # because the early hide above removes the numbers first.\n"
    "            self._maybe_defer_postclick_clear(\n",
    "            # because the early hide above removes the numbers first.\n"
    "            (lambda **_kw: None)(\n",
    "test_ck_58_settle_click_reply_after_a_focus_refresh_clears_the_clicked_list",
    suite=_CK_58)


# LC-47: one clear fault per pair raises one error notification
# (wh-overlay-slow-uia-stale-badges.9). The expired report is the ERROR; the
# unacknowledged echo clear at the same pair is a WARNING.
_LC_47 = sel(_LC, "test_lc_47_unacknowledged_echo_clear_after_an_expired_lease_is_a_warning",
             "test_lc_26_unacknowledged_clear_raises_an_error_notification")
# Mutant: every clear fault is reported at ERROR (no per-pair downgrade).
add("lc-47-every-clear-fault-is-an-error", _CTL,
    "        reported = getattr(self, \"_overlay_clear_fault_reported_pair\", None)\n"
    "        if reported == (sid, gen):\n",
    "        reported = getattr(self, \"_overlay_clear_fault_reported_pair\", None)\n"
    "        if False:\n",
    "test_lc_47_unacknowledged_echo_clear_after_an_expired_lease_is_a_warning",
    suite=_LC_47)
# Mutant: the expired report does not mark its pair as reported.
add("lc-47-expired-report-marks-no-pair", _CTL,
    "                        # downgrades to WARNING instead of a second popup.\n"
    "                        self._overlay_clear_fault_reported_pair = machine_pair\n",
    "                        # downgrades to WARNING instead of a second popup.\n"
    "                        pass\n",
    "test_lc_47_unacknowledged_echo_clear_after_an_expired_lease_is_a_warning",
    suite=_LC_47)


# RW-40: a Pattern Manager tree change shares the foreground debouncer
# (wh-overlay-rewalk-after-filter). Mutant: the tree change consults its own
# new debouncer, so it fires at once inside the foreground event's window.
_RW_40 = sel(_T, "test_rw_40_tree_change_after_a_foreground_event_shares_its_debounce",
             "test_rw_24_typing_burst_inside_debounce_yields_one_walk")
add("rw-37-tree-change-owns-a-debouncer", _CTL,
    "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: pattern manager tree change hwnd=%s seq=%s \"\n",
    "        if not self._build_overlay_focus_debouncer().should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    "                \"overlay: pattern manager tree change hwnd=%s seq=%s \"\n",
    "test_rw_40_tree_change_after_a_foreground_event_shares_its_debounce",
    suite=_RW_40)


# CK-59: the renumber guard after a focus or settle repaint
# (wh-overlay-slow-uia-stale-badges.4). The mutants are those of
# mutation_gate_renumber_guard_every_repaint.py, caught here at the boundary.
_CK_59 = sel(_CK, "test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked",
             "test_ck_34_click_n_after_a_renumbering_repaint_is_refused")
_CK_59_ENTRY_PIN_STATES = (
    "            in (OverlayState.REFRESH_IN_FLIGHT, "
    "OverlayState.POST_CLICK_SETTLING)\n"
)
# Mutant: a refresh records no entry pin, so a focus repaint arms nothing.
add("ck-59-refresh-entry-site-dropped", _CTL,
    _CK_59_ENTRY_PIN_STATES,
    "            in (OverlayState.POST_CLICK_SETTLING,)\n",
    "test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked",
    suite=_CK_59)
# Mutant: the post-click settle re-read records no entry pin.
add("ck-59-settle-entry-site-dropped", _CTL,
    _CK_59_ENTRY_PIN_STATES,
    "            in (OverlayState.REFRESH_IN_FLIGHT,)\n",
    "test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked",
    suite=_CK_59)
# Mutant: the pin comparison is inverted; a restore arms and a swap does not.
add("ck-59-arm-on-restore", _CTL,
    "            if entry_pin is not None and "
    "machine.pinned_snapshot_id != entry_pin:\n",
    "            if entry_pin is not None and "
    "machine.pinned_snapshot_id == entry_pin:\n",
    "test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked",
    suite=_CK_59)


# CK-60: post_click_settling keeps the clicked snapshot alive with no lease
# renew. Mutant: the keepalive state list drops POST_CLICK_SETTLING, so entry
# to the settling state cancels the keepalive timer.
_CK_60 = sel(_CK, "test_ck_60_settling_keeps_the_snapshot_alive_without_a_lease_renew",
             "test_ck_14_settle_clears_at_once_then_reads_and_repaints")
add("ck-60-settling-not-a-keepalive-state", "numbered_overlay/helpers.py",
    "        OverlayState.PAUSED,\n"
    "        OverlayState.POST_CLICK_SETTLING,\n"
    "    )\n",
    "        OverlayState.PAUSED,\n"
    "    )\n",
    "test_ck_60_settling_keeps_the_snapshot_alive_without_a_lease_renew",
    suite=_CK_60)


# CK-62: a saved overlay_keep_numbers applies at the next click
# (wh-numbers-stay-setting). Mutants: the single-key save is not handed to
# the running machine, and the running machine reads the saved key never.
_CK_62 = sel(_CK, "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
             "test_ck_14_settle_clears_at_once_then_reads_and_repaints")
add("ck-62-single-save-not-applied", "main.py",
    "        self.overlay.apply_saved_overlay_keep_numbers([key], saved)\n",
    "        pass\n",
    "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
    suite=_CK_62)
add("ck-62-saved-key-never-matches", _CTL,
    "        if not saved or \"click.overlay_keep_numbers\" not in keys:\n",
    "        if not saved or \"click.overlay_keep_numbers\" in keys:\n",
    "test_ck_62_saved_keep_numbers_applies_at_the_next_click",
    suite=_CK_62)


# Part B, rule D-T1 (wh-n29v.66): Logic builds the focus debouncer from the
# validated overlay_focus_debounce_ms. Mutant: the configured value is
# ignored and the 250 ms default is used; test_rw_8 configures 800 ms.
_RW_8_DT1 = sel(_T,
    "test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk",
    "test_rw_1_foreground_change_while_painted_refreshes")
add("rw-8-debouncer-ignores-the-configured-value", _CTL,
    "            debounce_ms=self._host.click_config.overlay_focus_debounce_ms\n",
    "            debounce_ms=250\n",
    "test_rw_8_events_inside_debounce_send_nothing_then_one_trailing_walk",
    suite=_RW_8_DT1)


# CK-61: an ok badge-click reply after 'hide numbers' is a teardown no-op
# (wh-n29v.95 part 6).
# EXPECTED SURVIVOR (reason 3): Needs two guards removed at once. The rule
# is enforced twice: controller.py _feed_click_complete_refresh feeds no
# CLICK_COMPLETE to a closed machine (keep-on: the PAINTED check; keep-off:
# the early hide applies HIDE_NUMBERS), and the closed state also takes a
# CLICK_COMPLETE as a teardown no-op. The runner applies one change to one
# file per mutation, so no single mutation breaks the rule.
# Manual proof (lead decision 12), from services/wheelhouse:
#   .venv/Scripts/python.exe tests/manual_proof_ck_61.py 0,1,2
# Measured 2026-11-10 20:08: 0,1,2 fails keep_on and keep_off; 0,2 fails
# keep_on; 1,2 fails keep_off; 0, 1 or 2 alone passes both. Each failure is
# "Logic sent no 'click_element' to Input within 5.0s" (the machine went to
# error, so 'click three' gave a notice and no by-name search). The script
# restores each file; git diff --stat -- numbered_overlay/
# click_overlay_state.py printed nothing after each run.
_CK_61 = sel(_CK, "test_ck_61_ok_click_reply_after_hide_numbers_sends_nothing",
             "test_ck_31_second_badge_click_while_first_in_flight_is_refused")
add("ck-61-closed-click-complete-is-invalid", "click_overlay_state.py",
    "            OverlayEventKind.TIMEOUT,\n"
    "            OverlayEventKind.CLICK_COMPLETE,\n"
    "        ):\n"
    "            # A build_response / paint_ack / timeout / click_complete landing in\n",
    "            OverlayEventKind.TIMEOUT,\n"
    "        ):\n"
    "            # A build_response / paint_ack / timeout / click_complete landing in\n",
    "test_ck_61_ok_click_reply_after_hide_numbers_sends_nothing",
    survives=('Needs two guards removed at once: tests/manual_proof_ck_61.py removes both and the test fails'),
    suite=_CK_61)


# LC-48: a timer callback already due when a same-pair transition commits
# feeds no TIMEOUT (wh-overlay-timer-cancel-race). The paint deadline comes
# due in the loop turn after the painted ack is read, behind the ack's
# handler step (OverlayBoundary.send_gui_as_time_passes). Mutants: the
# callback no longer compares its armed state with the machine's state, and
# the plausible wrong fix that compares it with the timer bookkeeping.
_LC_48 = sel(_LC,
    "test_lc_48_paint_deadline_due_as_the_painted_ack_commits_closes_nothing",
    "test_lc_17_failed_or_missing_paint_ack_clears_and_closes")
add("lc-48-armed-state-check-dropped", _CTL,
    "        if machine.state is not armed_state:\n"
    "            return\n",
    "        pass\n",
    "test_lc_48_paint_deadline_due_as_the_painted_ack_commits_closes_nothing",
    suite=_LC_48)
add("lc-48-armed-state-read-from-the-bookkeeping", _CTL,
    "        self._overlay_timer = None\n"
    "        self._overlay_timer_pair = None\n"
    "        self._overlay_armed_timer_state = None\n"
    "        machine = getattr(self, \"click_overlay_state\", None)\n"
    "        if machine is None:\n"
    "            return\n"
    "        if machine.state is not armed_state:\n"
    "            return\n",
    "        recorded = self._overlay_armed_timer_state\n"
    "        self._overlay_timer = None\n"
    "        self._overlay_timer_pair = None\n"
    "        self._overlay_armed_timer_state = None\n"
    "        machine = getattr(self, \"click_overlay_state\", None)\n"
    "        if machine is None:\n"
    "            return\n"
    "        if recorded is not armed_state:\n"
    "            return\n",
    "test_lc_48_paint_deadline_due_as_the_painted_ack_commits_closes_nothing",
    suite=_LC_48)


# LC-49: effect batches leave Logic in the order the machine scheduled them
# (wh-n29v.95). The harness holds the pin_snapshot reply; 'hide numbers' said
# in that wait must send nothing until the pin reply arrives. Mutant: each
# batch takes its own lock, so the hide batch no longer waits.
_LC_49 = sel(_LC,
    "test_lc_49_hide_while_the_pin_is_unanswered_clears_after_the_paint",
    "test_lc_17_failed_or_missing_paint_ack_clears_and_closes")
add("lc-49-batches-are-not-serialized", _CTL,
    "        lock = getattr(self, \"_overlay_effect_lock\", None)\n",
    "        lock = None\n",
    "test_lc_49_hide_while_the_pin_is_unanswered_clears_after_the_paint",
    suite=_LC_49)


# LC-50: a refresh paint held behind its pin_snapshot reply past the
# refresh deadline is not sent (wh-overlay-slow-uia-stale-badges.19).
# Needs screen_read_timeout_ms 1000, so the refresh deadline ends inside
# the pin reply limit. Mutant: the paint check compares only the pair, so
# the paint for the abandoned list goes out.
_LC_50 = sel(_LC,
    "test_lc_50_refresh_paint_held_past_the_refresh_deadline_is_not_sent",
    "test_lc_49_hide_while_the_pin_is_unanswered_clears_after_the_paint",
    "test_lc_17_failed_or_missing_paint_ack_clears_and_closes")
add("lc-50-paint-check-ignores-the-pin", _CTL,
    "        if pair != (effect.overlay_session_id, effect.paint_generation) or (\n"
    "            machine.pinned_snapshot_id != effect.snapshot_id\n"
    "        ):\n",
    "        if pair != (effect.overlay_session_id, effect.paint_generation):\n",
    "test_lc_50_refresh_paint_held_past_the_refresh_deadline_is_not_sent",
    suite=_LC_50)


# ===========================================================================
# RW-37 .. RW-39: the mouse refresh (wh-overlay-refresh-after-mouse)
# ===========================================================================
_CTRL = "numbered_overlay/controller.py"
_INTEG = "test_logic_overlay_integration.py"

# ---------------------------------------------------------------------------
# RW-37
# ---------------------------------------------------------------------------
RW_37 = sel(_T,
    "test_rw_37_click_and_wheel_burst_refreshes_once_after_quiet_time",
    "test_rw_37_input_during_first_build_refreshes_after_the_paint")
# Mutant: the hook entry never posts to the Logic loop. Caught by: test_rw_37
# burst (next_frame("start_overlay_walk") raises AssertionError).
add("rw-37-hook-entry-posts-nothing", _CTRL,
    "        if self._overlay_pointer_input_wanted:\n"
    "            self._host.loop.call_soon_threadsafe(\n",
    "        if False:\n"
    "            self._host.loop.call_soon_threadsafe(\n",
    "test_rw_37_click_and_wheel_burst_refreshes_once_after_quiet_time",
    "test_rw_37_input_during_first_build_refreshes_after_the_paint",
    suite=RW_37)
# Mutant: a new event does not restart the quiet time; the first event's
# timer stands. Caught by: test_rw_37 burst (frames == [] after the second
# burst, while the first burst's quiet time has passed).
add("rw-37-quiet-time-not-restarted", _CTRL,
    "        self._cancel_overlay_pointer_refresh()\n"
    "        self._overlay_pointer_refresh_handle = self._host.loop.call_later(\n",
    "        if self._overlay_pointer_refresh_handle is not None:\n"
    "            return\n"
    "        self._overlay_pointer_refresh_handle = self._host.loop.call_later(\n",
    "test_rw_37_click_and_wheel_burst_refreshes_once_after_quiet_time",
    suite=RW_37)
# Mutant: no quiet time; every event refreshes at once. Caught by: test_rw_37
# burst (frames == [] inside the quiet time).
add("rw-37-no-quiet-time", _CTRL,
    "            _OVERLAY_POINTER_SETTLE_S, self._on_overlay_pointer_settled,\n",
    "            0.0, self._on_overlay_pointer_settled,\n",
    "test_rw_37_click_and_wheel_burst_refreshes_once_after_quiet_time",
    suite=RW_37)
# Mutant: input during the first walk or paint is dropped instead of waiting
# for the paint. Caught by: test_rw_37 first build (next_frame raises).
add("rw-37-first-build-input-dropped", _CTRL,
    "            OverlayState.WALK_IN_FLIGHT, OverlayState.PAINT_IN_FLIGHT,\n"
    "        ):\n"
    "            self._arm_overlay_pointer_refresh()\n"
    "            return\n",
    "            OverlayState.WALK_IN_FLIGHT, OverlayState.PAINT_IN_FLIGHT,\n"
    "        ):\n"
    "            return\n",
    "test_rw_37_input_during_first_build_refreshes_after_the_paint",
    suite=RW_37)

# ---------------------------------------------------------------------------
# RW-38
# ---------------------------------------------------------------------------
RW_38 = sel(_T,
    "test_rw_38_mouse_input_in_idle_states_sends_nothing",
    "test_rw_38_hide_cancels_a_pending_mouse_refresh") + sel(_INTEG,
    "test_mouse_refresh_settled_while_numbers_hidden_does_nothing",
    "test_mouse_refresh_settled_while_numbers_show_applies_one_focus_change")
# Mutant: the timer body refreshes in every state outside the first build.
# The machine's own FOCUS_CHANGE arms in closed, settling and error are
# no-ops (RW-4), so the boundary sees nothing; caught by the unit test
# test_mouse_refresh_settled_while_numbers_hidden_does_nothing
# (no settle log line and no overlay output after the quiet time).
add("rw-38-settled-refreshes-in-every-state", _CTRL,
    "        if not numbers_show:\n"
    "            return\n"
    "        logger.debug(\n",
    "        logger.debug(\n",
    "test_mouse_refresh_settled_while_numbers_hidden_does_nothing",
    suite=RW_38)
# Mutant: an automatic-hide refresh counts as numbers showing. Caught by:
# test_mouse_refresh_settled_while_numbers_hidden_does_nothing
# [auto-hide-refresh].
add("rw-38-auto-hide-refresh-counts-as-showing", _CTRL,
    "            and not machine.auto_hide_in_flight\n",
    "            and True\n",
    "test_mouse_refresh_settled_while_numbers_hidden_does_nothing",
    suite=RW_38)
# Mutant: closing the overlay does not cancel a pending mouse refresh. Caught
# by: test_rw_38_hide_cancels_a_pending_mouse_refresh (one start_overlay_walk
# expected after the new show; the old timer adds a second).
add("rw-38-close-keeps-pending-refresh", _CTRL,
    "        if state is OverlayState.CLOSED:\n"
    "            self._cancel_overlay_pointer_refresh()\n",
    "        if state is OverlayState.CLOSED:\n"
    "            pass\n",
    "test_rw_38_hide_cancels_a_pending_mouse_refresh",
    suite=RW_38)

# ---------------------------------------------------------------------------
# RW-39
# ---------------------------------------------------------------------------
RW_39 = sel(_T, "test_rw_39_number_click_starts_no_second_refresh") + sel(
    _INTEG, "test_mouse_input_while_a_number_click_is_in_flight")
# Mutant: an injected click while a number click is in flight counts. The
# boundary cannot see it on its own (the number click's end cancels the
# timer); caught by test_mouse_input_while_a_number_click_is_in_flight
# [injected-click] (no start_overlay_walk while the click waits).
add("rw-39-in-flight-injected-click-counts", _CTRL,
    "            getattr(self, \"_overlay_click_in_flight\", None) is not None\n"
    "            or getattr(self, \"_overlay_number_click_quiet_handle\", None)\n",
    "            False\n"
    "            or getattr(self, \"_overlay_number_click_quiet_handle\", None)\n",
    "test_mouse_input_while_a_number_click_is_in_flight",
    suite=RW_39)
# Mutant: no quiet window after the number click ends. Caught by: test_rw_39
# [after] (one start_overlay_walk expected; the late injected click adds a
# second).
add("rw-39-no-quiet-window-after-number-click", _CTRL,
    "            or getattr(self, \"_overlay_number_click_quiet_handle\", None)\n"
    "            is not None\n",
    "            or False\n",
    "test_rw_39_number_click_starts_no_second_refresh",
    suite=RW_39)
# Mutant: the number click's end leaves a pending mouse refresh. Caught by:
# test_rw_39 [pending] (one start_overlay_walk expected).
add("rw-39-number-click-keeps-pending-refresh", _CTRL,
    "        try:\n"
    "            self._cancel_overlay_pointer_refresh()\n"
    "            quiet = getattr(self, \"_overlay_number_click_quiet_handle\", None)\n",
    "        try:\n"
    "            quiet = getattr(self, \"_overlay_number_click_quiet_handle\", None)\n",
    "test_rw_39_number_click_starts_no_second_refresh",
    suite=RW_39)

# ---------------------------------------------------------------------------
# The hook-thread exception guard (acceptance item 5)
# ---------------------------------------------------------------------------
HOOK_GUARD = (
    "tests/test_handlers/test_mouse_handler.py::TestPointerInputForOverlay",
)
# Mutant: an exception from the listener leaves the pynput callback (which
# stops the listener). Caught by: test_an_exception_does_not_leave_the_callback
# (escaped == []).
add("hook-callback-exception-escapes", "handlers/mouse_handler.py",
    "        except Exception:  # noqa: BLE001 -- nothing may leave the hook\n"
    "            pass\n",
    "        except ZeroDivisionError:  # mutant: other exceptions escape\n"
    "            pass\n",
    "test_an_exception_does_not_leave_the_callback",
    suite=HOOK_GUARD)

# Mutant: the shipped app never gives the mouse handler the overlay's entry,
# so no click or wheel step reaches the overlay. The boundary harness makes
# this connection itself, so only the lifecycle test sees the mutant.
# Caught by: test_mouse_clicks_and_wheel_steps_reach_the_numbered_overlay
# (set_pointer_input_listener called 0 times).
add("service-manager-listener-not-connected", "service_manager.py",
    "        self.mouse_handler.set_pointer_input_listener(\n"
    "            self.overlay_pointer_input_listener()\n"
    "        )\n",
    "        pass  # mutant: the overlay gets no mouse input\n",
    "test_mouse_clicks_and_wheel_steps_reach_the_numbered_overlay",
    suite=("tests/test_service_manager_lifecycle.py::TestInitializeServices",))

# wh-overlay-refresh-after-mouse.1.1: overlay.shutdown() stops the mouse
# refresh. Mutant 1: shutdown() leaves the two mouse-refresh timers armed.
# Mutant 2: the hook entry still posts after shutdown. Caught by:
# test_shutdown_stops_the_mouse_refresh (a timer is not cancelled after
# shutdown; the hook entry posts after shutdown).
SHUTDOWN_GUARD = (
    "tests/test_logic_overlay_integration.py::test_shutdown_stops_the_mouse_refresh",
)
add("shutdown-keeps-the-mouse-refresh-timers", "numbered_overlay/controller.py",
    "            \"_overlay_pointer_refresh_handle\",\n"
    "            \"_overlay_number_click_quiet_handle\",\n"
    "        ):\n",
    "        ):\n",
    "test_shutdown_stops_the_mouse_refresh",
    suite=SHUTDOWN_GUARD)
add("shutdown-keeps-the-hook-entry-open", "numbered_overlay/controller.py",
    "        self._overlay_pointer_input_wanted = False\n"
    "        if getattr(self, \"_overlay_pending_clear\", None) is not None:\n",
    "        if getattr(self, \"_overlay_pending_clear\", None) is not None:\n",
    "test_shutdown_stops_the_mouse_refresh",
    suite=SHUTDOWN_GUARD)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
