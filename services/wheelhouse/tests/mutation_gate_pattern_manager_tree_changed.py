"""Mutation evidence for the Pattern Manager tree-change re-walk
(wh-overlay-rewalk-after-filter).

Typing in the Pattern Manager's filter box hides rows, and the window
announces nothing a Logic-side hook can see, so the GUI process sends a
``pattern_manager_tree_changed`` event itself. This gate breaks each part of
that path -- the pure decision in ``decide_tree_change``, the dialog's emit
sites and their sequence counter, the GUI forwarder's Full guard, and the
Logic handler's validation, config gate, ordering and handler-map entry --
and requires the named tests to fail for each.

EXPECTED SURVIVORS (wh-full-refactor.3.1). Seven mutations carry the key
"survives" with a reason: the old tests that caught them are deleted by the
overlay refactor, and the requirement rows that cover the behaviour are
NOT_REQUIRED, or the mutant changes nothing a boundary test can observe. An
expected survivor passes only when its whole selection stays
green; a failed test prints "CAUGHT EXPECTED SURVIVOR" and the gate exits 1.

Run from services/wheelhouse, with the interpreter the whole worktree uses
(``sys.executable`` becomes the pytest launcher, see LAUNCH below). In a
worktree that is the main checkout's interpreter, so <main> below is the
main checkout's root:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \
        tests/mutation_gate_pattern_manager_tree_changed.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \
        tests/mutation_gate_pattern_manager_tree_changed.py

WHY THIS GATE REPLACES ``runner.LAUNCH``. Every other gate starts pytest
through ``scripts/run_tests.py``, which runs ``uv run pytest``. In a git
worktree ``uv`` builds a ``.venv`` inside the tree, and that .venv is what
makes a worktree undeletable (long paths plus processes holding the
directory). This gate therefore launches ``<python> -m pytest`` in the
service directory instead. Nothing else about the run changes: the same
Windows Job ownership, the same timeout, the same pre-launch marker, the
same output parsing.

THREE MUTATIONS THAT ARE NOT HERE, and why. They are recorded rather than
faked, because a mutation nobody can catch is neither a survivor to paper
over nor a test to invent.

  - ``tracked_hwnd == 0`` removed on its own, and ``event_hwnd == 0``
    removed on its own, are both EQUIVALENT mutants. The guard reads
    ``tracked_hwnd == 0 or event_hwnd == 0 or tracked_hwnd != event_hwnd``.
    With one zero test gone: if the two handles differ, the inequality
    still drops the event; if they are equal they are both zero, and the
    remaining zero test still drops it. Neither removal changes the
    function's answer for any input, so no test can fail and none is
    missing. The mutation ``window-check-drops-both-zero-guards`` below
    removes BOTH, which IS observable ((0, 0) is then accepted), and the
    zero-handle test catches it.
  - Bumping ``self._tree_seq`` after the event is built IS included
    (``sequence-bumped-after-the-event``); it is catchable and it does not
    raise. The schema's ``sequence >= 1`` rule lives in ``from_dict``, not
    in the dataclass constructor, so ``PatternManagerTreeChangedEvent(
    hwnd=..., sequence=0)`` is built without complaint and an event
    carrying sequence 0 is really emitted. Nothing is swallowed by
    ``_emit_tree_changed``'s try/except.

``self.winId()`` in place of ``self.window().winId()`` IS included. The
handle-equality assertions cannot catch it -- the dialog is built with
``parent=None`` in these tests, so it is its own top-level window and the
two calls return the same handle -- but the test that makes ``window()``
raise does catch it, because the mutant never calls ``window()`` at all and
emits an event where the test requires none.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (see the module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

HOOKS = "tests/test_overlay_focus_hooks.py"
DIALOG = "tests/test_pattern_manager_dialog.py"
SCHEMA = "tests/test_pattern_manager_tree_changed.py"
HANDLER = "tests/test_logic_pattern_manager_tree_changed_handler.py"
# wh-overlay-rewalk-after-filter.1.2. Its four tests live in their own module
# rather than at the end of DIALOG: with them there, the eight-file overlay
# selection died under -q with a 0xc0000374 heap corruption, 5 runs of 5, in
# code this branch does not touch. See that module's docstring.
EXPANDS = "tests/test_pattern_manager_tree_expand_collapse.py"

# Per-mutation selections. Each is the expected catchers' own file plus a
# small sanity set, never the whole suite.
PURE = (HOOKS,)                 # decide_tree_change and its Logic callback
UI = (DIALOG, SCHEMA)           # the dialog's emits and the GUI forwarder
LOGIC = (HANDLER, HOOKS)        # the Logic handler and the callback's ordering
EXPAND = (EXPANDS,)             # expand / collapse of a category
# wh-full-refactor.2: phase B of the overlay pilot (wh-overlay-refactor-pilot.2)
# deleted the old decide_tree_change, callback and handler-map tests; their
# boundary tests live here, so the selections that name them add this file.
REWALK = "tests/test_overlay_requirements_rewalk.py"
PURE_RW = PURE + (REWALK,)
LOGIC_RW = LOGIC + (REWALK,)

# --- expected catchers, by the name pytest reports -------------------------

# decide_tree_change (tests/test_overlay_focus_hooks.py)
# The boundary test that replaced the two old "accepts" tests (painted with
# matching handles, and refresh in flight); tests/test_overlay_requirements_rewalk.py.
RW_ACCEPT = "test_rw_19_tree_change_for_the_pinned_window_refreshes"
# One parametrized test names the expected decision for all EIGHT OverlayState
# members (contract C2 as amended). The runner strips the parameter list, so
# this one name catches a wrong decision for any state.
EVERY_STATE = "test_decide_tree_change_decides_every_overlay_state"
STATE_TABLE_COMPLETE = "test_the_state_table_covers_every_overlay_state"
NO_CLOSED_LIE = "test_no_drop_reason_claims_the_overlay_is_closed_unless_it_is"
SUPERSEDES = (
    "test_a_tree_change_in_flight_supersedes_the_build_and_paints_post_filter"
)
DROP_OTHER_WINDOW = (
    "test_decide_tree_change_drops_other_window_on_a_different_handle"
)
DROP_ZERO_HANDLE = "test_decide_tree_change_drops_other_window_on_a_zero_handle"
# The boundary test that replaced the old drop-stale, accept-next and
# handle-restart tests; tests/test_overlay_requirements_rewalk.py.
RW_SEQUENCE = "test_rw_23_old_sequence_dropped_and_new_handle_restarts_it"
ORDER_STATE_FIRST = "test_decide_tree_change_checks_state_before_the_window"
ORDER_WINDOW_FIRST = "test_decide_tree_change_checks_the_window_before_the_sequence"

# the Logic callback (same file)
# The boundary test that replaced the old burst test;
# tests/test_overlay_requirements_rewalk.py.
CB_BURST = "test_rw_24_typing_burst_inside_debounce_yields_one_walk"

# the dialog (tests/test_pattern_manager_dialog.py)
UI_FILTER_ONE = "test_filter_change_emits_one_tree_changed_event"
UI_SECOND_FILTER = "test_a_second_filter_change_increments_the_sequence"
UI_CLEARING = "test_clearing_the_filter_also_emits"
UI_NOT_VISIBLE = "test_no_event_is_emitted_while_the_dialog_is_not_visible"
UI_POPULATE = "test_populate_emits_a_tree_changed_event"
UI_HANDLE_RAISES = (
    "test_a_failed_window_handle_read_does_not_break_the_filter_box"
)

# expand / collapse (tests/test_pattern_manager_tree_expand_collapse.py)
X_COLLAPSE = "test_collapsing_a_category_emits_one_tree_changed_event"
X_RE_EXPAND = "test_re_expanding_a_category_emits_a_further_tree_changed_event"
X_NO_EXPAND_ALL = "test_populate_sends_no_event_for_its_own_expand_all"
X_NOT_VISIBLE = "test_no_expand_collapse_event_while_the_dialog_is_not_visible"

# the GUI forwarder (same file)
GUI_FORWARDS = "test_gui_forwards_the_tree_changed_dict_to_the_logic_queue"
GUI_FULL = "test_gui_logs_and_drops_when_the_logic_queue_is_full"

# the Logic handler (tests/test_logic_pattern_manager_tree_changed_handler.py)
H_MALFORMED = (
    "test_malformed_payload_dropped_without_raising_or_touching_the_machine"
)
H_WRONG_ACTION = "test_wrong_action_payload_dropped_without_raising"
H_ZERO_HWND = "test_zero_hwnd_payload_dropped_without_raising"
# The boundary test that replaced the old handler-map test;
# tests/test_overlay_requirements_rewalk.py.
H_MAP = "test_rw_19_tree_change_for_the_pinned_window_refreshes"

MUTATIONS = []


def add(name, file, tests, old, new, *expect, survives=None):
    mutation = dict(name=name, service=SERVICE, test_file=tests,
                    file=SERVICE / file, old=old, new=new,
                    expect=list(expect))
    # The key is added only when given, as in
    # tests/mutation_gate_overlay_requirements.py: the shared runner treats
    # the presence of "survives" as the declaration.
    if survives is not None:
        mutation["survives"] = survives
    MUTATIONS.append(mutation)


HOOKS_SRC = "overlay_focus_hooks.py"
DIALOG_SRC = "pattern_manager_dialog.py"
GUI_SRC = "gui.py"
MAIN_SRC = "main.py"
CONTROLLER_SRC = "numbered_overlay/controller.py"

STATE_CHECK = "    if state not in TREE_CHANGE_ACCEPTED_STATES:\n"
# The whole state branch, for the order mutation below.
STATE_BRANCH = (
    STATE_CHECK
    + "        # A state in neither set is a state nobody decided about. Drop it --\n"
    "        # the safe direction -- under a reason built from its own name, so the\n"
    "        # log line still names the state truthfully and this path can never\n"
    "        # raise on a GUI event. The eight-member table test in\n"
    "        # tests/test_overlay_focus_hooks.py is what makes that omission loud.\n"
    "        reason = TREE_CHANGE_DROP_BY_STATE.get(\n"
    "            state, f\"overlay_state_{state.value}\"\n"
    "        )\n"
    "        return TreeChangeDecision(None, reason, None)\n"
)
ACCEPTED_SET = (
    "TREE_CHANGE_ACCEPTED_STATES = frozenset(\n"
    "    {\n"
    "        OverlayState.PAINTED,\n"
    "        OverlayState.REFRESH_IN_FLIGHT,\n"
    "        OverlayState.WALK_IN_FLIGHT,\n"
    "        OverlayState.PAINT_IN_FLIGHT,\n"
    "    }\n"
    ")\n"
)
WINDOW_CHECK = (
    "    if tracked_hwnd == 0 or event_hwnd == 0 or tracked_hwnd != event_hwnd:\n"
)
STALE_CHECK = (
    "    if event_hwnd == last_hwnd and event_sequence <= last_sequence:\n"
)
ACCEPT = (
    "    return TreeChangeDecision(\n"
    "        OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),\n"
    "        None,\n"
    "        event_sequence,\n"
    "    )\n"
)

# --- the overlay-state check ------------------------------------------------

add("state-check-removed", HOOKS_SRC, PURE,
    STATE_CHECK,
    "    if False:\n",
    EVERY_STATE, ORDER_STATE_FIRST)
# Finer granularity: only CLOSED let through. The three other dropped states
# stay dropped, so this proves the parametrised test tests CLOSED too.
add("state-check-also-accepts-closed", HOOKS_SRC, PURE,
    ACCEPTED_SET,
    ACCEPTED_SET.replace("    }\n",
                         "        OverlayState.CLOSED,\n    }\n"),
    EVERY_STATE, ORDER_STATE_FIRST)

# --- contract C2 as amended (wh-overlay-rewalk-after-filter.1.1) ------------
# The two in-flight build states must stay in the accepted set: their
# FOCUS_CHANGE arms restart the build with BuildReason.SUPERSEDE, so dropping
# the event there paints the PRE-filter walk over the post-filter rows.

add("accepted-set-drops-walk-in-flight", HOOKS_SRC, PURE,
    ACCEPTED_SET,
    ACCEPTED_SET.replace("        OverlayState.WALK_IN_FLIGHT,\n", ""),
    EVERY_STATE, SUPERSEDES)
add("accepted-set-drops-paint-in-flight", HOOKS_SRC, PURE,
    ACCEPTED_SET,
    ACCEPTED_SET.replace("        OverlayState.PAINT_IN_FLIGHT,\n", ""),
    EVERY_STATE, SUPERSEDES)
# A log line must never say "closed" about an open session: the retired
# single reason is what this mutation puts back, on one dropped state.
add("drop-reason-renamed-back-to-overlay-closed", HOOKS_SRC, PURE,
    'TREE_CHANGE_DROP_STATE_PAUSED = "overlay_state_paused"\n',
    'TREE_CHANGE_DROP_STATE_PAUSED = "overlay_closed"\n',
    EVERY_STATE, NO_CLOSED_LIE)
# NO MUTATION for removing one entry from TREE_CHANGE_DROP_BY_STATE: that is
# an equivalent mutant. The fallback builds "overlay_state_" + state.value,
# which is byte for byte what each constant holds, so a missing entry produces
# the same string. The explicit table is there to pin the spelling where a
# reader looks for it, and the fallback is there so an undecided state can
# never raise on a GUI event; they agree on purpose.

# --- the window check -------------------------------------------------------

# Removing EITHER zero test alone is an equivalent mutant (module docstring).
# Removing both is not: (0, 0) is then accepted.
add("window-check-drops-both-zero-guards", HOOKS_SRC, PURE,
    WINDOW_CHECK,
    "    if tracked_hwnd != event_hwnd:\n",
    DROP_ZERO_HANDLE)
add("window-check-inverted", HOOKS_SRC, PURE_RW,
    WINDOW_CHECK,
    "    if tracked_hwnd == 0 or event_hwnd == 0 or tracked_hwnd == event_hwnd:\n",
    RW_ACCEPT)
add("window-check-removed", HOOKS_SRC, PURE,
    WINDOW_CHECK,
    "    if False:\n",
    DROP_OTHER_WINDOW, DROP_ZERO_HANDLE, ORDER_WINDOW_FIRST)

# --- the stale-sequence check ----------------------------------------------

add("stale-check-lets-a-repeat-through", HOOKS_SRC, PURE_RW,
    STALE_CHECK,
    "    if event_hwnd == last_hwnd and event_sequence < last_sequence:\n",
    RW_SEQUENCE)
# The sequence-restart behaviour: without the handle conjunct a reopened
# dialog's first event is judged against the OLD window's counter.
add("stale-check-ignores-the-handle", HOOKS_SRC, PURE_RW,
    STALE_CHECK,
    "    if event_sequence <= last_sequence:\n",
    RW_SEQUENCE)
add("stale-check-removed", HOOKS_SRC, PURE_RW,
    STALE_CHECK,
    "    if False:\n",
    RW_SEQUENCE)

# --- what an accepted decision carries -------------------------------------

# wh-full-refactor.2: the three old catchers were deleted in phase B. The
# boundary tests RW_ACCEPT and RW_SEQUENCE stay green under this mutation
# (sweep 2026-11-10); the parametrized EVERY_STATE test fails on its
# ``accepted_sequence == 1`` assertion for each accepting state.
add("accepted-sequence-is-the-old-one", HOOKS_SRC, PURE,
    ACCEPT,
    ACCEPT.replace("        event_sequence,\n", "        last_sequence,\n"),
    EVERY_STATE)
add("accepted-event-is-the-wrong-kind", HOOKS_SRC, PURE_RW,
    ACCEPT,
    ACCEPT.replace("OverlayEventKind.FOCUS_CHANGE",
                   "OverlayEventKind.FOCUSED_HWND_DESTROYED"),
    RW_ACCEPT)

# --- the order the three reasons are tested in ------------------------------
# Only the order test can catch this one: with the handles equal (every other
# drop test) the window check passes and the state check still answers first.

add("drop-reason-order-swapped", HOOKS_SRC, PURE,
    STATE_BRANCH
    + WINDOW_CHECK
    + "        return TreeChangeDecision(None, TREE_CHANGE_DROP_OTHER_WINDOW, None)\n",
    WINDOW_CHECK
    + "        return TreeChangeDecision(None, TREE_CHANGE_DROP_OTHER_WINDOW, None)\n"
    + STATE_BRANCH,
    ORDER_STATE_FIRST)

# --- the dialog's own emits -------------------------------------------------

add("visibility-check-removed", DIALOG_SRC, UI + EXPAND,
    "            if not self.isVisible():\n"
    "                return\n"
    "            self._tree_seq += 1\n",
    "            self._tree_seq += 1\n",
    UI_NOT_VISIBLE, X_NOT_VISIBLE)
# wh-overlay-rewalk-after-filter.1.2 added the suppression check directly
# above the visibility check, so this mutation removes the newer one and
# leaves the older intact -- the two are tested apart.
add("suppression-check-removed", DIALOG_SRC, UI + EXPAND,
    "            if self._suppress_tree_changed:\n"
    "                return\n",
    "",
    X_NO_EXPAND_ALL, UI_POPULATE)
add("sequence-never-bumped", DIALOG_SRC, UI,
    "            self._tree_seq += 1\n",
    "            pass\n",
    UI_FILTER_ONE, UI_SECOND_FILTER, UI_POPULATE)
add("sequence-bumped-after-the-event", DIALOG_SRC, UI,
    "            self._tree_seq += 1\n"
    "            # window() is the TOP-LEVEL window -- the handle Logic compares\n"
    "            # against the window the overlay was painted over.\n"
    "            hwnd = int(self.window().winId())\n"
    "            event = PatternManagerTreeChangedEvent(\n"
    "                hwnd=hwnd, sequence=self._tree_seq\n"
    "            )\n",
    "            # window() is the TOP-LEVEL window -- the handle Logic compares\n"
    "            # against the window the overlay was painted over.\n"
    "            hwnd = int(self.window().winId())\n"
    "            event = PatternManagerTreeChangedEvent(\n"
    "                hwnd=hwnd, sequence=self._tree_seq\n"
    "            )\n"
    "            self._tree_seq += 1\n",
    UI_FILTER_ONE, UI_SECOND_FILTER, UI_POPULATE)
add("non-top-level-window-handle", DIALOG_SRC, UI,
    "            hwnd = int(self.window().winId())\n",
    "            hwnd = int(self.winId())\n",
    UI_HANDLE_RAISES)
add("filter-tail-emit-removed", DIALOG_SRC, UI,
    "            self._tree_empty_label.setVisible(show_empty)\n"
    "\n"
    "        # The rows a walk would find just changed; tell Logic so the numbered\n"
    "        # overlay re-walks (wh-overlay-rewalk-after-filter).\n"
    '        self._emit_tree_changed("filter")\n',
    "            self._tree_empty_label.setVisible(show_empty)\n",
    UI_FILTER_ONE, UI_SECOND_FILTER, UI_CLEARING)
add("populate-tail-emit-removed", DIALOG_SRC, UI,
    '        self._emit_tree_changed("populate")\n',
    "        pass\n",
    UI_POPULATE)

# --- the GUI forwarder ------------------------------------------------------

add("forwarder-full-guard-removed", GUI_SRC, UI,
    "        try:\n"
    "            self.commands_to_logic_queue.put_nowait(command)\n"
    "        except Full:\n"
    "            logger.warning(\n"
    '                "pattern_manager_tree_changed: commands_to_logic_queue Full; "\n'
    '                "dropping the overlay re-walk request",\n'
    "            )\n",
    "        self.commands_to_logic_queue.put_nowait(command)\n",
    GUI_FULL)
# NOT a survivor any more. This comment used to say the connect line was
# untested because nothing constructs a GuiManager; the wiring test named
# below was then added for exactly that line and catches this mutation. The
# old wording survived the fix and is corrected here -- GLM 5.3 round 1
# reported it as a note on wh-overlay-rewalk-after-filter.1. The forwarder's
# own tests still bind the sender onto a bare holder, which is why the
# wiring test is the only catcher.
add("tree-changed-wired-to-the-unguarded-sender", GUI_SRC, UI,
    "            self._pm_dialog.tree_changed.connect(\n"
    "                self._send_pattern_manager_tree_changed\n"
    "            )\n",
    "            self._pm_dialog.tree_changed.connect(self._send_pm_command)\n",
    # The forwarder tests bind the sender onto a bare holder, so only the
    # wiring test reaches the connect line this mutation rewrites.
    "test_open_pattern_manager_wires_tree_changed_to_the_guarded_sender")

# --- the Logic handler and the callback's ordering --------------------------

DECIDE_CALL = (
    "        from services.wheelhouse.click_overlay_state import OverlayState\n"
    "\n"
    "        state = self.click_overlay_state.state\n"
    "        pending = self._overlay_pending_build_identity\n"
    "        if pending is not None and state in (\n"
    "            OverlayState.WALK_IN_FLIGHT,\n"
    "            OverlayState.PAINT_IN_FLIGHT,\n"
    "            OverlayState.REFRESH_IN_FLIGHT,\n"
    "        ):\n"
    "            tracked = pending\n"
    "        else:\n"
    "            tracked = self._overlay_tracked_identity\n"
    "        decision = decide_tree_change(\n"
    "            state=state,\n"
    "            tracked_hwnd=tracked.hwnd if tracked is not None else 0,\n"
    "            event_hwnd=hwnd,\n"
    "            event_sequence=sequence,\n"
    "            last_hwnd=self._pm_tree_change_last_hwnd,\n"
    "            last_sequence=self._pm_tree_change_last_sequence,\n"
    "        )\n"
    "        if decision.event is None:\n"
    "            _overlay_focus_logger.debug(\n"
    '                "overlay: pattern manager tree change hwnd=%s seq=%s dropped "\n'
    '                "(%s).",\n'
    "                hwnd,\n"
    "                sequence,\n"
    "                decision.drop_reason,\n"
    "            )\n"
    "            return\n"
    "\n"
    "        self._pm_tree_change_last_hwnd = hwnd\n"
    "        self._pm_tree_change_last_sequence = sequence\n"
    "\n"
)
DEBOUNCE = (
    "        now_ms = self._clock() * 1000.0\n"
    "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n"
    "            _overlay_focus_logger.debug(\n"
    '                "overlay: pattern manager tree change hwnd=%s seq=%s "\n'
    '                "coalesced by debounce.",\n'
    "                hwnd,\n"
    "                sequence,\n"
    "            )\n"
    '            self._arm_overlay_settle_refire("pattern manager tree change")\n'
    "            return\n"
    "        event = decision.event\n"
    "        # As in ``_on_overlay_menu_popup_change``: settle a coalesced\n"
    "        # foreground event into this event before the cancel drops it.\n"
    "        if self._consume_pending_foreground_dismissal():\n"
    "            event = dataclasses.replace(event, dismisses_numbers=True)\n"
    "        self._cancel_overlay_settle_refire()\n"
)

add("accepted-sequence-recorded-after-the-debounce", CONTROLLER_SRC, LOGIC_RW,
    "        self._pm_tree_change_last_hwnd = hwnd\n"
    "        self._pm_tree_change_last_sequence = sequence\n"
    "\n"
    + DEBOUNCE,
    DEBOUNCE.replace(
        "        self._cancel_overlay_settle_refire()\n",
        "        self._pm_tree_change_last_hwnd = hwnd\n"
        "        self._pm_tree_change_last_sequence = sequence\n"
        "        self._cancel_overlay_settle_refire()\n"),
    CB_BURST)
add("debounce-runs-before-the-decision", CONTROLLER_SRC,
    LOGIC_RW,
    DECIDE_CALL
    + "        now_ms = self._clock() * 1000.0\n"
    + "        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):\n",
    "        now_ms = self._clock() * 1000.0\n"
    "        fired = self._overlay_focus_debouncer.should_fire(now_ms=now_ms)\n"
    + DECIDE_CALL
    + "        if not fired:\n",
    survives=(
        "Result 4 timing detail, case 4(a) RW-11 (NOT_REQUIRED). The mutant "
        "only lets a dropped closed-state tree change move the debounce clock. "
        "Needs a tree change while closed, then within "
        "overlay_focus_debounce_ms show numbers, a paint and a focus event"
    ))
add("malformed-payload-guard-removed", MAIN_SRC, LOGIC,
    "        if ev is None:\n"
    "            return  # already logged\n"
    "\n"
    "        self.overlay.on_pattern_manager_tree_change(ev.hwnd, ev.sequence)\n",
    "        self.overlay.on_pattern_manager_tree_change(\n"
    '            getattr(ev, "hwnd", 0), getattr(ev, "sequence", 0)\n'
    "        )\n",
    H_MALFORMED, H_WRONG_ACTION, H_ZERO_HWND)
add("config-gate-removed", CONTROLLER_SRC,
    LOGIC_RW,
    "        if not (\n"
    "            self._host.click_config.enabled\n"
    "            and self._host.click_config.overlay_enabled_effective\n"
    "        ):\n"
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import decide_tree_change\n",
    "        if False:\n"
    "            return\n"
    "        from services.wheelhouse.overlay_focus_hooks import decide_tree_change\n",
    survives=(
        "Item 4 exception 1: with the overlay disabled the machine stays "
        "closed, so decide_tree_change drops the same event before the "
        "debounce; the mutant changes only a debug log line"
    ))
add("handler-map-entry-removed", MAIN_SRC, LOGIC_RW,
    '            "pattern_manager_tree_changed": lambda: (\n'
    "                self._handle_pattern_manager_tree_changed(command)\n"
    "            ),\n",
    "",
    H_MAP)

# --- expand / collapse of a category (wh-overlay-rewalk-after-filter.1.2) ---
#
# Contract C1 lists expand/collapse beside the filter change. The two
# connects below are the whole trigger, and the suppression flag is what
# keeps populate()'s own expandAll() from putting one event per category on
# the wire instead of the single event populate's tail sends.

add("expand-connect-removed", DIALOG_SRC, EXPAND,
    "        self._tree.itemExpanded.connect(self._on_tree_item_expanded)\n",
    "",
    X_RE_EXPAND)
add("collapse-connect-removed", DIALOG_SRC, EXPAND,
    "        self._tree.itemCollapsed.connect(self._on_tree_item_collapsed)\n",
    "",
    X_COLLAPSE, X_RE_EXPAND)
# The flag never set: populate's expandAll() then emits once per category.
# Six events for a two-category populate (two expands plus the tail, times
# the two dialogs an ordering pass builds), which is why the filter tests in
# the dialog file catch it as well.
add("expand-all-suppression-never-set", DIALOG_SRC, EXPAND + (DIALOG,),
    "        self._suppress_tree_changed = True\n"
    "        try:\n"
    "            self._tree.expandAll()\n",
    "        self._suppress_tree_changed = False\n"
    "        try:\n"
    "            self._tree.expandAll()\n",
    X_NO_EXPAND_ALL, X_COLLAPSE, X_RE_EXPAND, UI_POPULATE, UI_FILTER_ONE)


# --- the window an in-flight build is for (wh-overlay-rewalk-after-filter.1.3)
#
# ``_overlay_tracked_identity`` names the latest PIN, and the pin runs only
# after a build response comes back, so a tree change arriving DURING a build
# had no truthful window to be judged against: None on the first session of a
# run, and the window that lost the focus on a cross-window refresh. Both were
# dropped as ``other_window`` and the pre-change build painted stale badges --
# the failure this whole bead exists to remove, reached by the ordinary
# sequence "show numbers, then type in the filter box".
#
# ``_overlay_pending_build_identity`` is sampled at the single build funnel and
# read only while a build is in flight. These five mutations break each part:
# the sample, its two ends, the read, and the state gate that keeps the read
# from outliving its build.

INTEGRATION = "tests/test_logic_overlay_integration.py"
BUILD = (INTEGRATION, HOOKS)    # the dispatch/pin lifecycle and the callback

# Case 4(a) of the stage inventory: the rows RW-20 and RW-22 that cover the
# window an in-flight build is for are NOT_REQUIRED (lead ruling).
_PENDING_WINDOW_ROWS = "Case 4(a): rows RW-20 and RW-22 are NOT_REQUIRED"

CB_FIRST_AUTO = "test_tree_change_accepted_during_a_first_session_auto_open"
CB_CROSS_WINDOW = "test_tree_change_in_a_cross_window_refresh_follows_the_build"

add("dispatch-does-not-record-the-build-window", CONTROLLER_SRC,
    BUILD + (REWALK,),
    "        self._overlay_pending_build_identity = (\n"
    "            self._capture_overlay_foreground_identity()\n"
    "        )\n",
    "",
    survives=(
        f"{_PENDING_WINDOW_ROWS}: the build dispatch records the pending "
        "build window, on a walk and on auto-open. Needs a Pattern Manager "
        "list change while a build is in flight"
    ))
add("pin-does-not-consume-the-build-window", CONTROLLER_SRC,
    BUILD + (REWALK,),
    "            # the tree-change callback prefer it over the pin.\n"
    "            self._overlay_pending_build_identity = None\n",
    "            # the tree-change callback prefer it over the pin.\n",
    survives=(
        f"{_PENDING_WINDOW_ROWS}: the pin consumes the pending build window"
    ))
add("closed-does-not-clear-the-build-window", CONTROLLER_SRC,
    BUILD + (REWALK,),
    "            # And the window a build in flight was being made for: the session\n"
    "            # it belonged to is over (wh-overlay-rewalk-after-filter.1.3).\n"
    "            self._overlay_pending_build_identity = None\n",
    "            # And the window a build in flight was being made for: the session\n"
    "            # it belonged to is over (wh-overlay-rewalk-after-filter.1.3).\n",
    survives=(
        f"{_PENDING_WINDOW_ROWS}: entry to closed clears the pending build "
        "window"
    ))
# The read falls back to the pre-fix source: the pinned identity, whatever the
# machine is doing. This is the defect itself, restored.
add("tree-change-ignores-the-pending-build-window", CONTROLLER_SRC,
    PURE_RW,
    "        from services.wheelhouse.click_overlay_state import OverlayState\n"
    "\n"
    "        state = self.click_overlay_state.state\n"
    "        pending = self._overlay_pending_build_identity\n"
    "        if pending is not None and state in (\n"
    "            OverlayState.WALK_IN_FLIGHT,\n"
    "            OverlayState.PAINT_IN_FLIGHT,\n"
    "            OverlayState.REFRESH_IN_FLIGHT,\n"
    "        ):\n"
    "            tracked = pending\n"
    "        else:\n"
    "            tracked = self._overlay_tracked_identity\n",
    "        state = self.click_overlay_state.state\n"
    "        tracked = self._overlay_tracked_identity\n",
    CB_FIRST_AUTO, CB_CROSS_WINDOW)
# And the opposite error: the pending target read in every accepted state. A
# refresh that falls back to its prior pin returns to painted with no new pin,
# so the pending target then names a window whose badges never reached the
# screen.
add("tree-change-prefers-the-build-window-in-every-state", CONTROLLER_SRC,
    PURE_RW,
    "        if pending is not None and state in (\n"
    "            OverlayState.WALK_IN_FLIGHT,\n"
    "            OverlayState.PAINT_IN_FLIGHT,\n"
    "            OverlayState.REFRESH_IN_FLIGHT,\n"
    "        ):\n",
    "        if pending is not None:\n",
    survives=(
        "Case 4(a) RW-20/RW-22 (NOT_REQUIRED; lead ruling). Needs a tree "
        "change while painted with a build window pending"
    ))


# wh-overlay-rewalk-after-filter.1.4: the build's window is also sampled when a
# batch holding a build is handed over, before its task is scheduled. The
# catcher queues a tree change ahead of the AUTO_OPEN effect task; without the
# handover sample the event finds no target and the pre-filter snapshot paints.
Q_AHEAD = "test_tree_change_queued_ahead_of_the_auto_open_build_is_not_dropped"
HANDOFF_SAMPLE = (
    "        if any(e.kind is EffectKind.DISPATCH_BUILD for e in effects):\n"
    "            self._overlay_pending_build_identity = (\n"
    "                self._capture_overlay_foreground_identity()\n"
    "            )\n"
)
add("handoff-does-not-record-the-build-window", CONTROLLER_SRC, BUILD,
    HANDOFF_SAMPLE,
    "",
    Q_AHEAD)
# The same sample deferred by one loop turn. It then runs behind every callback
# already ready on the Logic loop -- exactly the ordering the finding replayed.
# (Moving it after create_task_with_error_handling instead is an equivalent
# mutant: the default task factory runs nothing until this function returns.)
add("handoff-sample-deferred-one-loop-turn", CONTROLLER_SRC, BUILD,
    HANDOFF_SAMPLE,
    "        if any(e.kind is EffectKind.DISPATCH_BUILD for e in effects):\n"
    "            def _late_sample():\n"
    "                self._overlay_pending_build_identity = (\n"
    "                    self._capture_overlay_foreground_identity()\n"
    "                )\n"
    "            self._host.loop.call_soon(_late_sample)\n",
    Q_AHEAD)


# --- the Pattern Manager comes to the front (wh-voice-access-parity.1.15 F4) --
#
# Every trigger ("show commands", "what can I say", the tray items) goes
# through GuiManager._open_pattern_manager. It clears only the minimized bit
# of the window state, then show / raise_ / activateWindow, then asks the
# terminal editor's _steal_foreground for the Windows foreground, inside a
# try/except so a failure there cannot reach the Qt slot or queue handler.

F4_RESTORE = "test_open_pattern_manager_restores_a_minimized_dialog"
F4_MAXIMIZED = "test_open_pattern_manager_keeps_a_maximized_dialog_maximized"
F4_NORMAL = "test_open_pattern_manager_leaves_a_normal_dialog_state_alone"
F4_FOREGROUND = "test_open_pattern_manager_takes_the_foreground_with_the_dialog_hwnd"
F4_FAILURE = "test_open_pattern_manager_survives_a_foreground_failure"

F4_RESTORE_BRANCH = (
    "        if state & Qt.WindowState.WindowMinimized:\n"
    "            self._pm_dialog.setWindowState(\n"
    "                (state & ~Qt.WindowState.WindowMinimized)\n"
    "                | Qt.WindowState.WindowActive\n"
    "            )\n"
)
F4_STEAL_CALL = "            _steal_foreground(int(self._pm_dialog.winId()))\n"
F4_STEAL_GUARD = (
    "        try:\n"
    "            from terminal_editor_window import _steal_foreground\n"
    "\n"
    + F4_STEAL_CALL
    + "        except Exception as exc:\n"
    '            logger.debug("Pattern Manager foreground request failed: %s", exc)\n'
)

add("pm-open-minimized-restore-dropped", GUI_SRC, UI,
    F4_RESTORE_BRANCH,
    "        if state & Qt.WindowState.WindowMinimized:\n"
    "            pass\n",
    F4_RESTORE, F4_MAXIMIZED)
# showNormal's effect: the maximized bit is lost with the minimized one.
add("pm-open-restore-loses-maximized", GUI_SRC, UI,
    F4_RESTORE_BRANCH,
    "        if state & Qt.WindowState.WindowMinimized:\n"
    "            self._pm_dialog.setWindowState(\n"
    "                Qt.WindowState.WindowActive\n"
    "            )\n",
    F4_MAXIMIZED)
# Input-level: the restore applied whatever the state is.
add("pm-open-restore-runs-for-every-state", GUI_SRC, UI,
    F4_RESTORE_BRANCH,
    F4_RESTORE_BRANCH.replace(
        "        if state & Qt.WindowState.WindowMinimized:\n",
        "        if True:\n"),
    F4_NORMAL)
add("pm-open-foreground-call-dropped", GUI_SRC, UI,
    F4_STEAL_CALL,
    "            pass\n",
    F4_FOREGROUND, F4_FAILURE)
add("pm-open-foreground-gets-a-zero-handle", GUI_SRC, UI,
    F4_STEAL_CALL,
    "            _steal_foreground(0)\n",
    F4_FOREGROUND)
# The catcher's contract is "does not raise", so under this mutant it fails
# on the OSError its stand-in raises -- that exception IS the defect here,
# not an unrelated crash upstream of the assertion.
add("pm-open-foreground-guard-removed", GUI_SRC, UI,
    F4_STEAL_GUARD,
    "        from terminal_editor_window import _steal_foreground\n"
    "\n"
    "        _steal_foreground(int(self._pm_dialog.winId()))\n",
    F4_FAILURE)


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
