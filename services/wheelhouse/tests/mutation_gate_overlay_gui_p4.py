"""Mutation evidence for the P4 requirement tests (wh-full-refactor.7.1).

Area P4: the clear, lease expiry and teardown methods. Stage B of place 4
(wh-full-refactor.7.2) moved them from OverlayPaintWindowManager
(overlay_paint_window.py) to OverlayPaintTeardown (overlay_paint/teardown.py);
the manager keeps delegators. Each mutation names its row OT-n from the P4
notes of stage A of place 4, and the boundary tests of
tests/test_overlay_gui_requirements_p4.py that must catch it. 26 mutations
are in overlay_paint/teardown.py. Where GenerationGate decides a rule for a
teardown, the mutation is in that class, which stays in
overlay_paint_window.py (7 mutations); the test still enters through the
public methods of the manager.

Same form and the same shared runner as tests/mutation_gate_mouse_grid_l5.py
(read the docstring of tests/mutation_gate_overlay_requirements.py for the
disciplines the runner owns). A mutation marked as an expected survivor
gives its reason as survives=; the sweep fails when any test of its
selection fails under it, because the reason is then no longer true.

Run from services/wheelhouse:
  uv run python tests/mutation_gate_overlay_gui_p4.py --check
  uv run python tests/mutation_gate_overlay_gui_p4.py
  uv run python tests/mutation_gate_overlay_gui_p4.py ot-1-clear-returns-no-ack
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

_T = "test_overlay_gui_requirements_p4.py"
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


OT_1 = "test_ot_1_accepted_teardown_destroys_every_window_and_acks"
OT_2 = "test_ot_2_teardown_with_no_window_still_acks"
OT_3 = "test_ot_3_stale_teardown_returns_none_and_destroys_nothing"
OT_3B = "test_ot_3b_teardown_older_than_an_accepted_clear_is_stale"
OT_4 = "test_ot_4_late_paint_at_or_below_the_teardown_pair_shows_nothing"
OT_4B = "test_ot_4b_paint_newer_than_the_teardown_pair_shows"
OT_5 = "test_ot_5_teardown_with_a_surviving_window_withholds_the_ack"
OT_6 = "test_ot_6_retry_while_destroy_still_fails_retries_only_the_survivor"
OT_7 = "test_ot_7_retry_that_destroys_the_survivor_releases_the_ack_once"
OT_8 = "test_ot_8_newest_incomplete_teardown_owns_the_parked_ack"
OT_9 = "test_ot_9_retry_never_destroys_the_window_of_a_later_paint"
OT_9B = "test_ot_9b_paint_destroys_the_survivor_and_the_retry_releases_the_ack"
OT_10 = "test_ot_10_retry_without_debt_or_parked_ack_does_nothing"
OT_11 = "test_ot_11_later_clean_clear_pays_the_debt_and_the_old_ack_stays_parked"
OT_12 = "test_ot_12_reset_destroys_every_window_and_returns_nothing"
OT_13 = "test_ot_13_reset_starts_a_fresh_generation_gate"
OT_14 = "test_ot_14_reset_drops_the_parked_ack_but_keeps_the_debt"
OT_14B = "test_ot_14b_clean_reset_pays_the_debt"
OT_14C = "test_ot_14c_reset_with_a_new_survivor_records_the_debt"
OT_15 = "test_ot_15_clear_all_destroys_every_window_and_keeps_the_gate"
OT_16 = "test_ot_16_clear_all_records_no_debt_for_a_survivor"
OT_17 = "test_ot_17_teardown_destroys_only_the_windows_of_its_own_manager"

# Unique anchors in clear() and expire_lease().
_CLEAR_ACK = (
    "        ack = OverlayStateChangedEvent(\n"
    "            state=\"cleared\",\n"
    "            overlay_session_id=overlay_session_id,\n"
    "            paint_generation=paint_generation,\n"
    "            monitor_ids=(),\n"
    "            snapshot_id=None,\n"
    "        ).to_dict()\n"
)
_EXPIRE_ACK = _CLEAR_ACK.replace("\"cleared\"", "\"expired\"")

# --- OT-1 / OT-2: an accepted teardown destroys and acks ----------------------

add("ot-1-clear-returns-no-ack", _PT,
    "            return None\n"
    "        return ack\n"
    "\n"
    "    def expire_lease(\n",
    "            return None\n"
    "        return None\n"
    "\n"
    "    def expire_lease(\n",
    OT_1, OT_2)
add("ot-1-expire-acks-as-cleared", _PT,
    "            state=\"expired\",\n",
    "            state=\"cleared\",\n",
    OT_1, OT_2)
add("ot-1-clear-destroys-nothing", _PT,
    _CLEAR_ACK + "        if not self.destroy_all():\n",
    _CLEAR_ACK + "        if not True:\n",
    OT_1)
add("ot-1-expire-destroys-nothing", _PT,
    _EXPIRE_ACK + "        if not self.destroy_all():\n",
    _EXPIRE_ACK + "        if not True:\n",
    OT_1)
# The gate refuses a clear at the pair of the mark (same-pair case).
add("ot-1-same-pair-clear-is-stale", _P,
    "        pair = (overlay_session_id, paint_generation)\n"
    "        if pair < self._mark:\n",
    "        pair = (overlay_session_id, paint_generation)\n"
    "        if pair <= self._mark:\n",
    OT_1)

# --- OT-3: a stale teardown does nothing --------------------------------------

add("ot-3-clear-ignores-the-gate", _PT,
    "        if not self._host.gate.accept_clear(overlay_session_id, paint_generation):\n"
    "            logger.debug(\n"
    "                \"overlay_paint_window: stale clear (%s, %s) ignored\",\n",
    "        if False:\n"
    "            logger.debug(\n"
    "                \"overlay_paint_window: stale clear (%s, %s) ignored\",\n",
    OT_3, OT_3B)
add("ot-3-expire-ignores-the-gate", _PT,
    "        if not self._host.gate.accept_clear(overlay_session_id, paint_generation):\n"
    "            logger.debug(\n"
    "                \"overlay_paint_window: stale lease expiry (%s, %s) ignored\",\n",
    "        if False:\n"
    "            logger.debug(\n"
    "                \"overlay_paint_window: stale lease expiry (%s, %s) ignored\",\n",
    OT_3, OT_3B)

# --- OT-4: a teardown blocks a late paint at or below its pair --------------

add("ot-4-clear-records-no-block", _P,
    "        if pair > self._cleared_at:\n"
    "            self._cleared_at = pair\n",
    "        if False:\n"
    "            self._cleared_at = pair\n",
    OT_4)
add("ot-4-same-pair-paint-passes-the-block", _P,
    "        if pair < self._mark or pair <= self._cleared_at:\n",
    "        if pair < self._mark or pair < self._cleared_at:\n",
    OT_4)
add("ot-4b-block-refuses-every-paint", _P,
    "        if pair < self._mark or pair <= self._cleared_at:\n",
    "        if True:\n",
    OT_4B)

# --- OT-5 to OT-8: a survivor withholds the ack until a retry ----------------

add("ot-5-clear-parks-nothing", _PT,
    "            self._teardown_pending = True\n"
    "            self._deferred_teardown_ack = ack\n"
    "            logger.warning(\n"
    "                \"overlay_paint_window: clear (%s, %s) left surviving badge \"\n",
    "            pass\n"
    "            logger.warning(\n"
    "                \"overlay_paint_window: clear (%s, %s) left surviving badge \"\n",
    OT_5)
add("ot-5-clear-acks-despite-a-survivor", _PT,
    "                \"windows; ack deferred until a retry destroys them\",\n"
    "                overlay_session_id,\n"
    "                paint_generation,\n"
    "            )\n"
    "            return None\n",
    "                \"windows; ack deferred until a retry destroys them\",\n"
    "                overlay_session_id,\n"
    "                paint_generation,\n"
    "            )\n"
    "            return ack\n",
    OT_5)
add("ot-5-expire-parks-nothing", _PT,
    "            self._teardown_pending = True\n"
    "            self._deferred_teardown_ack = ack\n"
    "            logger.warning(\n"
    "                \"overlay_paint_window: lease expiry (%s, %s) left surviving \"\n",
    "            pass\n"
    "            logger.warning(\n"
    "                \"overlay_paint_window: lease expiry (%s, %s) left surviving \"\n",
    OT_5)
add("ot-5-survivor-not-kept-in-the-cohort", _PT,
    "            if not window.destroy():\n"
    "                self._host.pending_destroy.append(window)\n"
    "        self._host.windows = {}\n",
    "            window.destroy()\n"
    "        self._host.windows = {}\n",
    OT_5, OT_6)
add("ot-5-pending-property-reads-false", _PT,
    "        return self._teardown_pending\n",
    "        return False\n",
    OT_5, OT_6)
add("ot-5-parked-ack-property-inverted", _PT,
    "        return self._deferred_teardown_ack is not None\n",
    "        return self._deferred_teardown_ack is None\n",
    OT_5, OT_6)
add("ot-6-retry-ignores-the-debt", _PT,
    "        if self._teardown_pending and not self._destroy_pending():\n",
    "        if False and not self._destroy_pending():\n",
    OT_6, OT_7)
add("ot-7-retry-releases-the-ack-twice", _PT,
    "            ack = self._deferred_teardown_ack\n"
    "            self._deferred_teardown_ack = None\n"
    "            return ack\n",
    "            ack = self._deferred_teardown_ack\n"
    "            return ack\n",
    OT_7)
add("ot-7-clean-retry-keeps-the-debt", _PT,
    "        ]\n"
    "        clean = not self._host.pending_destroy\n"
    "        if clean:\n"
    "            self._teardown_pending = False\n"
    "        return clean\n",
    "        ]\n"
    "        clean = not self._host.pending_destroy\n"
    "        return clean\n",
    OT_7)
add("ot-8-teardown-skips-the-cohort", _PT,
    "        # Retry cohort members first; keep the ones that still fail.\n"
    "        self._host.pending_destroy = [\n"
    "            window for window in self._host.pending_destroy if not window.destroy()\n"
    "        ]\n",
    "        pass\n",
    OT_8, OT_11)

# --- OT-9 / OT-10: a retry sweeps only the debt cohort -----------------------

add("ot-9-retry-sweeps-the-live-windows", _PT,
    "        if self._teardown_pending and not self._destroy_pending():\n",
    "        if self._teardown_pending and not self.destroy_all():\n",
    OT_9)
_PAINT_PRUNE = (
    "        if self._pending_destroy:\n"
    "            self._pending_destroy = [\n"
    "                window\n"
    "                for window in self._pending_destroy\n"
    "                if not window.destroy()\n"
    "            ]\n"
    "\n"
    "        # Enumerate the topology EXACTLY ONCE per render"
)
add("ot-9b-paint-pays-the-debt", _P,
    _PAINT_PRUNE,
    "        if self._pending_destroy:\n"
    "            self._destroy_pending()\n"
    "\n"
    "        # Enumerate the topology EXACTLY ONCE per render",
    OT_9B)
add("ot-9b-paint-skips-the-cohort", _P,
    _PAINT_PRUNE,
    "        if False:\n"
    "            pass\n"
    "\n"
    "        # Enumerate the topology EXACTLY ONCE per render",
    OT_9B)
add("ot-10-retry-without-debt-sweeps-the-live-windows", _PT,
    "        if self._teardown_pending and not self._destroy_pending():\n",
    "        if not self.destroy_all():\n",
    OT_10, OT_9)

# --- OT-11: a clean sweep pays the debt ----------------------------------------

add("ot-11-clean-sweep-keeps-the-debt", _PT,
    "        clean = not self._host.pending_destroy\n"
    "        if clean:\n"
    "            self._teardown_pending = False\n"
    "        return clean\n"
    "\n"
    "    def _destroy_pending",
    "        clean = not self._host.pending_destroy\n"
    "        return clean\n"
    "\n"
    "    def _destroy_pending",
    OT_11, OT_14B)

# --- OT-12 to OT-14: reset ---------------------------------------------------------

add("ot-12-reset-destroys-nothing", _PT,
    "        self._host.gate = GenerationGate()\n"
    "        if not self.destroy_all():\n",
    "        self._host.gate = GenerationGate()\n"
    "        if not True:\n",
    OT_12)
add("ot-13-reset-keeps-the-old-gate", _PT,
    "        self._host.gate = GenerationGate()\n"
    "        if not self.destroy_all():\n",
    "        if not self.destroy_all():\n",
    OT_13)
add("ot-14-reset-keeps-the-parked-ack", _PT,
    "            self._teardown_pending = True\n"
    "        self._deferred_teardown_ack = None\n",
    "            self._teardown_pending = True\n",
    OT_14)
add("ot-14-reset-records-no-debt", _PT,
    "            self._teardown_pending = True\n"
    "        self._deferred_teardown_ack = None\n",
    "            pass\n"
    "        self._deferred_teardown_ack = None\n",
    OT_14C)

# --- OT-15 / OT-16: clear_all ------------------------------------------------------

add("ot-15-clear-all-destroys-nothing", _PT,
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        self.destroy_all()\n",
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        pass\n",
    OT_15)
add("ot-15-clear-all-resets-the-gate", _PT,
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        self.destroy_all()\n",
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        self._host.gate = GenerationGate()\n"
    "        self.destroy_all()\n",
    OT_15)
add("ot-16-clear-all-records-debt", _PT,
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        self.destroy_all()\n",
    "        Used on GUI teardown / fixture cleanup.\n"
    "        \"\"\"\n"
    "        if not self.destroy_all():\n"
    "            self._teardown_pending = True\n",
    OT_16)

# --- OT-17: each manager owns its own windows -----------------------------------

add("ot-17-managers-share-one-window-table", _P,
    "        self._windows: dict[int, _OverlayWindow] = {}\n",
    "        self._windows: dict[int, _OverlayWindow] = globals().setdefault(\n"
    "            \"_mutant_shared_windows\", {})\n",
    OT_17)


def main(argv=None):
    return runner.run(MUTATIONS, list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
