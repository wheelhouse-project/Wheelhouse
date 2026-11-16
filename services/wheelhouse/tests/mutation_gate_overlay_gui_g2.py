"""Mutation evidence for the G2 requirement tests (wh-full-refactor.7.1).

Area G2: the 13 methods for the overlay and grid window messages. Stage B of
place 4 (wh-full-refactor.7.2) moved them from GuiManager (gui.py) to
OverlayGuiMessages (overlay_gui/messages.py). Each mutation names its row
OG-n from the G2 notes of stage A of place 4, and the boundary tests of
tests/test_overlay_gui_requirements_g2.py that must catch it. 44 mutations
are in overlay_gui/messages.py. One mutation (og-3-first-lease-shortened) is
in gui.py, because the constant _OVERLAY_LEASE_DEFAULT_MS stays there.

Same form and the same shared runner as tests/mutation_gate_mouse_grid_l5.py
(read the docstring of tests/mutation_gate_overlay_requirements.py for the
disciplines the runner owns). A mutation marked as an expected survivor
gives its reason as survives=; the sweep fails when any test of its
selection fails under it, because the reason is then no longer true.

Run from services/wheelhouse:
  uv run python tests/mutation_gate_overlay_gui_g2.py --check
  uv run python tests/mutation_gate_overlay_gui_g2.py
  uv run python tests/mutation_gate_overlay_gui_g2.py og-1-paint-report-not-sent
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

_T = "test_overlay_gui_requirements_g2.py"
_G = "gui.py"
_M = "overlay_gui/messages.py"
SUITE = (f"tests/{_T}",)


def add(name, file, old, new, *expect, suite=SUITE, survives=None):
    entry = dict(name=name, service=SERVICE, test_file=suite,
                 file=SERVICE / file, old=old, new=new, expect=list(expect))
    if survives is not None:
        entry["survives"] = survives
    MUTATIONS.append(entry)


OG_1 = "test_og_1_accepted_paint_shows_badges_and_reports_painted"
OG_2 = "test_og_2_failed_paint_reports_failed_and_arms_the_lease"
OG_3 = "test_og_3_lease_runs_out_after_90_s_and_removes_the_badges"
OG_3B = "test_og_3b_newer_accepted_paint_restarts_the_lease_at_its_pair"
OG_4 = "test_og_4_stale_paint_reports_nothing_and_keeps_the_lease"
OG_5 = "test_og_5_malformed_paint_shows_and_reports_nothing"
OG_6 = "test_og_6_accepted_clear_removes_badges_reports_cleared_and_ends_the_lease"
OG_7 = "test_og_7_stale_clear_reports_nothing_and_keeps_the_lease"
OG_8 = "test_og_8_incomplete_clear_reports_later_once_and_ends_the_lease"
OG_9 = "test_og_9_late_cleared_report_keeps_the_lease_of_a_newer_paint"
OG_10 = "test_og_10_malformed_clear_removes_nothing_and_keeps_the_lease"
OG_11 = "test_og_11_renew_extends_the_lease_and_expiry_keeps_the_armed_pair"
OG_12 = "test_og_12_renew_for_another_pair_is_ignored"
OG_13 = "test_og_13_renew_with_a_bad_lease_ms_still_renews"
OG_14 = "test_og_14_renew_above_the_timer_limit_still_renews"
OG_15 = "test_og_15_renew_with_no_lease_does_nothing"
OG_16 = "test_og_16_expiry_with_a_surviving_window_reports_later_once"
OG_17 = "test_og_17_expiry_below_an_accepted_clear_reports_nothing"
OG_18 = "test_og_18_reset_removes_badges_ends_the_lease_and_reports_nothing"
OG_19 = "test_og_19_reset_with_a_surviving_window_retries_and_reports_nothing"
OG_20 = "test_og_20_clean_clear_after_an_incomplete_one_reports_both"
OG_20B = "test_og_20b_retry_after_a_clean_reset_leaves_a_later_paint"
OG_21 = "test_og_21_grid_messages_reach_the_grid_and_report_nothing"
OG_21B = "test_og_21b_pin_alone_shows_a_window"
OG_21C = "test_og_21c_clear_grid_with_extra_keys_still_clears"
OG_21D = "test_og_21d_malformed_grid_message_changes_nothing"
OG_22 = "test_og_22_badge_teardown_leaves_the_grid"
OG_22B = "test_og_22b_clear_grid_leaves_the_badges"
OG_23 = "test_og_23_full_queue_drops_the_report_and_the_pump_goes_on"

_EXCEPT = "        except Exception as exc:  # noqa: BLE001 - overlay is non-critical\n"

# --- OG-1 to OG-5: paint_overlay ---------------------------------------------

add("og-1-paint-report-not-sent", _M,
    "            self._emit_overlay_state_changed(result)\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"paint_overlay handling failed: %s\", exc, exc_info=True,\n",
    "            pass\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"paint_overlay handling failed: %s\", exc, exc_info=True,\n",
    OG_1, OG_2)
add("og-2-failed-paint-arms-no-lease", _M,
    "            if result is not None:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the paint presented\n",
    "            if result is not None and result.get(\"state\") == \"painted\":\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the paint presented\n",
    OG_2)
add("og-3-paint-arms-no-lease", _M,
    "                self._arm_overlay_lease(\n"
    "                    (event.overlay_session_id, event.paint_generation),\n"
    "                    _OVERLAY_LEASE_DEFAULT_MS,\n"
    "                )\n",
    "                pass\n",
    OG_2, OG_3, OG_3B)
# LC-30 states the first lease: 90 s without a renew.
add("og-3-first-lease-shortened", _G,
    "_OVERLAY_LEASE_DEFAULT_MS = 90000\n",
    "_OVERLAY_LEASE_DEFAULT_MS = 60000\n",
    OG_3, OG_11)
add("og-3-expiry-destroys-nothing", _M,
    "            result = self._host.overlay_manager.expire_lease(\n"
    "                overlay_session_id=pair[0], paint_generation=pair[1],\n"
    "            )\n",
    "            result = None\n",
    OG_3, OG_3B)
add("og-3-expiry-report-not-sent", _M,
    "            self._arm_teardown_retry_if_pending()\n"
    "            self._emit_overlay_state_changed(result)\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"overlay lease expiry handling failed",
    "            self._arm_teardown_retry_if_pending()\n"
    "            pass\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"overlay lease expiry handling failed",
    OG_3, OG_3B)
add("og-4-stale-paint-rearms-the-lease", _M,
    "            if result is not None:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the paint presented\n",
    "            if True:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the paint presented\n",
    OG_4)
add("og-5-malformed-paint-not-dropped", _M,
    "                PaintOverlayEvent.from_dict, message, log_label=\"paint_overlay\",\n"
    "            )\n"
    "            if event is None:\n"
    "                return  # already logged\n",
    "                PaintOverlayEvent.from_dict, message, log_label=\"paint_overlay\",\n"
    "            )\n"
    "            if event is None:\n"
    "                pass\n",
    OG_5,
    survives="With event None, OverlayGuiMessages.handle_paint_overlay (overlay_gui/messages.py) goes on to "
             "event.summary, which raises AttributeError inside its own try; "
             "the broad except logs it, so nothing is painted or sent and the "
             "pump goes on: the same outputs as the return.")

# --- OG-6 to OG-10: clear_overlay and the teardown retry ---------------------

add("og-6-clear-keeps-the-lease", _M,
    "                self._cancel_overlay_lease()\n"
    "            self._arm_teardown_retry_if_pending()\n",
    "                pass\n"
    "            self._arm_teardown_retry_if_pending()\n",
    OG_6)
add("og-6-clear-report-not-sent", _M,
    "            self._emit_overlay_state_changed(result)\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"clear_overlay handling failed: %s\", exc, exc_info=True,\n",
    "            pass\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"clear_overlay handling failed: %s\", exc, exc_info=True,\n",
    OG_6)
add("og-7-stale-clear-ends-the-lease", _M,
    "            if result is not None:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the badges are gone,\n",
    "            if True:\n"
    "                # wh-overlay-slow-uia-stale-badges.9: the badges are gone,\n",
    OG_7)
add("og-8-clear-arms-no-retry", _M,
    "                self._cancel_overlay_lease()\n"
    "            self._arm_teardown_retry_if_pending()\n",
    "                self._cancel_overlay_lease()\n"
    "            pass\n",
    OG_8, OG_9, OG_20)
add("og-8-retry-not-armed-again", _M,
    "                self._emit_overlay_state_changed(ack)\n"
    "            if self._host.overlay_manager.teardown_pending:\n",
    "                self._emit_overlay_state_changed(ack)\n"
    "            if False:\n",
    OG_8, OG_16, OG_19)
add("og-8-retry-report-not-sent", _M,
    "                self._emit_overlay_state_changed(ack)\n"
    "            if self._host.overlay_manager.teardown_pending:\n",
    "                pass\n"
    "            if self._host.overlay_manager.teardown_pending:\n",
    OG_8, OG_9, OG_16, OG_17, OG_20)
add("og-8-retry-report-keeps-the-lease", _M,
    "                ) == self._overlay_lease_pair:\n"
    "                    self._cancel_overlay_lease()\n",
    "                ) == self._overlay_lease_pair:\n"
    "                    pass\n",
    OG_8)
add("og-9-retry-report-ends-any-lease", _M,
    "                ) == self._overlay_lease_pair:\n"
    "                    self._cancel_overlay_lease()\n",
    "                ) == self._overlay_lease_pair or True:\n"
    "                    self._cancel_overlay_lease()\n",
    OG_9)
add("og-10-malformed-clear-not-dropped", _M,
    "                ClearOverlayEvent.from_dict, message, log_label=\"clear_overlay\",\n"
    "            )\n"
    "            if event is None:\n"
    "                return  # already logged\n",
    "                ClearOverlayEvent.from_dict, message, log_label=\"clear_overlay\",\n"
    "            )\n"
    "            if event is None:\n"
    "                pass\n",
    OG_10,
    survives="With event None, OverlayGuiMessages.handle_clear_overlay (overlay_gui/messages.py) goes on to "
             "event.overlay_session_id, which raises AttributeError inside its "
             "own try; the broad except logs it, so nothing is removed or sent "
             "and the lease runs on: the same outputs as the return.")

# --- OG-11 to OG-15: overlay_lease_renew --------------------------------------

add("og-11-renew-ignored", _M,
    "            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)\n",
    "            pass\n",
    OG_11, OG_13, OG_14)
add("og-11-renew-arms-the-renew-pair", _M,
    "            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)\n",
    "            self._arm_overlay_lease((sid, gen), lease_ms)\n",
    OG_11)
add("og-11-older-generation-rejected", _M,
    "            if sid != armed_sid or (sid, gen) > (armed_sid, armed_gen):\n",
    "            if sid != armed_sid or (sid, gen) != (armed_sid, armed_gen):\n",
    OG_11)
add("og-12-newer-generation-accepted", _M,
    "            if sid != armed_sid or (sid, gen) > (armed_sid, armed_gen):\n",
    "            if sid != armed_sid:\n",
    OG_12)
add("og-12-other-session-accepted", _M,
    "            if sid != armed_sid or (sid, gen) > (armed_sid, armed_gen):\n",
    "            if (sid, gen) > (armed_sid, armed_gen):\n",
    OG_12)
add("og-12-bool-session-accepted", _M,
    "            if isinstance(sid, bool) or not isinstance(sid, int):\n",
    "            if not isinstance(sid, int):\n",
    OG_12)
add("og-12-bool-generation-accepted", _M,
    "            if isinstance(gen, bool) or not isinstance(gen, int):\n",
    "            if not isinstance(gen, int):\n",
    OG_12)
add("og-13-bad-lease-ms-drops-the-renew", _M,
    "                lease_ms = _OVERLAY_LEASE_DEFAULT_MS\n"
    "            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)\n",
    "                return\n"
    "            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)\n",
    OG_13)
add("og-13-zero-lease-ms-accepted", _M,
    "                    or lease_ms <= 0:\n",
    "                    or lease_ms < 0:\n",
    OG_13)
add("og-13-bool-lease-ms-accepted", _M,
    "            if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) \\\n",
    "            if not isinstance(lease_ms, int) \\\n",
    OG_13)
add("og-14-lease-not-clamped", _M,
    "            min(int(lease_ms), _QT_TIMER_MAX_INTERVAL_MS)\n"
    "        )\n"
    "\n"
    "    def _cancel_overlay_lease(self) -> None:\n",
    "            int(lease_ms)\n"
    "        )\n"
    "\n"
    "    def _cancel_overlay_lease(self) -> None:\n",
    OG_14)
# --- OG-6, OG-8, OG-18: the lease ends (body of _cancel_overlay_lease) --------
# Lead decision 97: stage B moves this method, so one mutation must change
# code inside it. With the body a no-operation, the lease pair and the timer
# stay after an accepted clear, a late teardown ack or a reset, and the
# lease expiry later sends an "expired" report that these rows forbid.
add("og-6-cancel-lease-does-nothing", _M,
    "        \"\"\"Stop the badge lease; no badges remain to watch.\"\"\"\n"
    "        self._overlay_lease_pair = None\n"
    "        self._host.overlay_lease_timer.stop()\n",
    "        \"\"\"Stop the badge lease; no badges remain to watch.\"\"\"\n"
    "        pass\n",
    OG_6, OG_8, OG_18)
add("og-15-renew-without-a-lease-goes-on", _M,
    "            if self._overlay_lease_pair is None:\n"
    "                return\n"
    "            sid = message.get(\"overlay_session_id\")\n",
    "            if False:\n"
    "                return\n"
    "            sid = message.get(\"overlay_session_id\")\n",
    OG_15,
    survives="With no lease, OverlayGuiMessages.handle_overlay_lease_renew (overlay_gui/messages.py) unpacks "
             "None into armed_sid, armed_gen, which raises TypeError inside "
             "its own try; the broad except logs it and no lease is armed: "
             "the same outputs as the return.")

# --- OG-16, OG-17: lease expiry ----------------------------------------------

add("og-16-expiry-arms-no-retry", _M,
    "            self._arm_teardown_retry_if_pending()\n"
    "            self._emit_overlay_state_changed(result)\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"overlay lease expiry handling failed",
    "            pass\n"
    "            self._emit_overlay_state_changed(result)\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"overlay lease expiry handling failed",
    OG_16)
add("og-17-empty-report-sent", _M,
    "        if not result:\n"
    "            return\n"
    "        try:\n"
    "            self._host.commands_to_logic_queue.put_nowait(result)\n",
    "        if False:\n"
    "            return\n"
    "        try:\n"
    "            self._host.commands_to_logic_queue.put_nowait(result)\n",
    OG_4, OG_7, OG_8, OG_17)

# --- OG-18, OG-19: reset_overlay ---------------------------------------------

add("og-18-reset-keeps-the-lease", _M,
    "            self._cancel_overlay_lease()\n"
    "            if self._host.overlay_manager is None:\n"
    "                return\n"
    "            self._host.overlay_manager.reset()\n",
    "            pass\n"
    "            if self._host.overlay_manager is None:\n"
    "                return\n"
    "            self._host.overlay_manager.reset()\n",
    OG_18)
add("og-18-reset-not-passed-on", _M,
    "            self._host.overlay_manager.reset()\n",
    "            pass\n",
    OG_18, OG_19, OG_20B)
add("og-19-reset-arms-no-retry", _M,
    "            self._arm_teardown_retry_if_pending()\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"reset_overlay handling failed",
    "            pass\n" + _EXCEPT +
    "            logger.warning(\n"
    "                \"reset_overlay handling failed",
    OG_19)

# --- OG-20: the retry while an older report is parked -----------------------

add("og-20-retry-stopped-while-a-report-is-parked", _M,
    "        elif not self._host.overlay_manager.has_deferred_teardown_ack:\n",
    "        else:\n",
    OG_20)
add("og-20b-retry-never-stopped", _M,
    "        elif not self._host.overlay_manager.has_deferred_teardown_ack:\n",
    "        elif False:\n",
    OG_20B,
    survives="With the stop removed, the retry timer fires after the clean "
             "reset, but OverlayPaintTeardown.retry_teardown (overlay_paint/teardown.py) has no debt "
             "and no parked ack, so it destroys nothing and returns None (P4 "
             "row OT-10); OverlayGuiMessages.on_overlay_teardown_retry (overlay_gui/messages.py) then sends "
             "nothing and does not arm again: no output changes.")

# --- OG-21, OG-22: the mouse grid --------------------------------------------

add("og-21-paint-grid-not-passed-on", _M,
    "            self._host.grid_overlay.paint_grid(event)\n",
    "            pass\n",
    OG_21, OG_21D, OG_22, OG_23)
add("og-21-pin-not-passed-on", _M,
    "            self._host.grid_overlay.paint_grid_pin(event)\n",
    "            pass\n",
    OG_21, OG_21B)
add("og-21-clear-grid-not-passed-on", _M,
    "            self._host.grid_overlay.clear_grid()\n",
    "            pass\n",
    OG_21, OG_21C)
add("og-21d-malformed-grid-not-dropped", _M,
    "                PaintGridEvent.from_dict, message, log_label=\"paint_grid\",\n"
    "            )\n"
    "            if event is None:\n"
    "                return  # already logged\n",
    "                PaintGridEvent.from_dict, message, log_label=\"paint_grid\",\n"
    "            )\n"
    "            if event is None:\n"
    "                pass\n",
    OG_21D)
# Same for the pin: a None pin event would repaint the grid without its pin.
add("og-21d-malformed-pin-not-dropped", _M,
    "                PaintGridPinEvent.from_dict, message,\n"
    "                log_label=\"paint_grid_pin\",\n"
    "            )\n"
    "            if event is None:\n"
    "                return  # already logged\n",
    "                PaintGridPinEvent.from_dict, message,\n"
    "                log_label=\"paint_grid_pin\",\n"
    "            )\n"
    "            if event is None:\n"
    "                pass\n",
    OG_21D)
add("og-22-grid-on-the-badge-manager", _M,
    "            self._host.grid_overlay.paint_grid(event)\n",
    "            self._host.overlay_manager.paint_grid(event)\n",
    OG_22)
add("og-22b-clear-grid-on-the-badge-manager", _M,
    "            self._host.grid_overlay.clear_grid()\n",
    "            self._host.overlay_manager.clear_grid()\n",
    OG_22B, OG_21, OG_21C)

# --- OG-23: a full queue to Logic ---------------------------------------------

add("og-23-full-queue-raises", _M,
    "        except Full:\n"
    "            logger.warning(\n"
    "                \"overlay_state_changed: commands_to_logic_queue Full; \"\n",
    "        except ZeroDivisionError:\n"
    "            logger.warning(\n"
    "                \"overlay_state_changed: commands_to_logic_queue Full; \"\n",
    OG_23,
    survives="Each caller of OverlayGuiMessages._emit_overlay_state_changed (overlay_gui/messages.py) calls it "
             "last inside its own try (paint, clear, expiry; in the retry the "
             "debt is already paid when an ack is sent), so the escaped Full "
             "is logged by that broad except: the report is dropped, the lease "
             "stays armed and the pump goes on, as with the except Full.")


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
