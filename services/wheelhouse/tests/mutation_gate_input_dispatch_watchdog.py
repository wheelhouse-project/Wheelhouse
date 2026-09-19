"""Mutation gate for the Input-process dispatch watchdog
(wh-overlay-slow-uia-stale-badges.11).

WHY THIS GATE EXISTS. Of the three watchdog tests, only the first was
observed failing before the watchdog was written. The other two passed
against a build with no watchdog in it at all, which is exactly the shape
of a test that proves nothing. Both were then strengthened, and a
strengthened test owes the same "watch it fail" step as a test written
after the fix. This gate is that step, and unlike a captured log of a
failing run it can be re-run later to show the tests still catch what they
claim.

WHAT IT COVERS. The first two mutations are the two directions of the stall
limit, which is where the watchdog decides whether a dispatch is news:

  never-report  -- the watchdog stays silent no matter how long a dispatch
                   holds the loop. That is the silent freeze the bead
                   exists to remove.
  always-report -- the watchdog reports every dispatch regardless of how
                   long it took. A watchdog that cries on every command
                   teaches its readers to ignore it, which costs the report
                   all of its value on the one occasion it matters.

That comparison is NOT the whole of the judgement, which is what an earlier
version of this docstring claimed. deepseek round 1 (finding
wh-overlay-slow-uia-stale-badges.11.1.1) enumerated four more decisions the
two mutations above never reach, and every one of them was unpinned by any
test at the time: the repeat interval that keeps a long stall from flooding
the log, the recovery line that says a stall ended, the gate that keeps that
line off ordinary work, and the log record for a dropped expired command.
The mutations below cover those, plus the deadline reader's malformed-value
guards (finding .11.1.2) and the timing guard (finding .11.1.3). Each guard
there turns "malformed" into either "expired" or "the Input process ends",
and the contract forbids both.

Codex round 1 (finding .11.2.1) added the last three. The timing guard as it
stood rejected the wrong SHAPES and let through numbers no platform can wait
on, and that hole is the one place in this feature with no containment at
all: _run_dispatch_watchdog reads the poll interval and waits on it OUTSIDE
the try that wraps poll, so an exception there ends the watchdog thread for
the rest of the run.

One note for whoever extends this gate. The value
test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback feeds in
was changed from "not a number" to "0.01" in the same round, because the
.11.2.1 fix put a second layer under the first: float() raises on
"not a number" whether or not the shape check is there, so
timing-guard-passthrough survived against a test that was still doing its
job. A numeric string is the only input the shape check alone rejects. Any
later fix that adds a layer over an existing guard needs the same look at
the older mutations through it.

Run it from services/wheelhouse:

    uv run python tests/mutation_gate_input_dispatch_watchdog.py
"""

import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE_DIR = Path(__file__).resolve().parents[1]
TARGET = SERVICE_DIR / "input_proc.py"
TEST_FILE = "tests/test_input_proc_stale_command_expiry.py"

# A mutation may name its own target and test file. Most of this feature
# lives in input_proc.py, so those two stay the default and every mutation
# written before codex round 5 keeps its exact behaviour. The launcher
# mutation added for finding .11.2.11 is the one that needs the other pair:
# the structural argument that no Input-only restart path exists is a claim
# about launcher.py, and only tests/test_launcher.py exercises it.
LAUNCHER_TARGET = SERVICE_DIR / "launcher.py"
LAUNCHER_TEST_FILE = "tests/test_launcher.py"

# wh-watchdog-stall-window put half of the feature in the Logic process: the
# watchdog's limit can only follow the awaited window if app.py stamps that
# window on the envelope, and only tests/test_app.py exercises the sender.
APP_TARGET = SERVICE_DIR / "app.py"
APP_TEST_FILE = "tests/test_app.py"

# A legitimate run of this file takes about two seconds. The timeout is far
# above that and far below anything a reader would sit through, so a mutation
# that produces a program which never answers is reported rather than left to
# hang the whole gate.
RUN_TIMEOUT_S = 180

_GUARD = "            if held_s < report_s:\n"

_REPEAT_GUARD = (
    "            if reported_at is not None and now - reported_at < repeat_s:\n"
)

_RECOVERY_GATE = "        if current is None or current[4] is None:\n"

_DROP_LOG = (
    '    logger.error(\n'
    '        "Dropped expired command before dispatch: its delivery deadline "\n'
    '        "passed %.3fs ago while this loop was busy. action=%s trace_id=%s "\n'
    '        "request_id=%s", overdue_s, action, trace_id or "-", request_id or "-",\n'
    '    )\n'
)

_DEADLINE_BOOL_GATE = (
    "        if isinstance(value, bool) or not isinstance(value, (int, float)):\n"
)

_DEADLINE_FINITE_GATE = "        if not math.isfinite(deadline):\n"

_SECONDS_GUARD = (
    "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n"
    "        return fallback\n"
)

# The three parts of _watchdog_seconds that codex round 1 (finding
# wh-overlay-slow-uia-stale-badges.11.2.1) found unpinned. Each is the whole
# of one decision, so each can be mutated on its own.
_SECONDS_CONVERSION_CATCH = "    except Exception:\n        return fallback\n"
_SECONDS_CEILING = (
    "    if not math.isfinite(seconds) or seconds <= 0 or seconds > _MAX_WAIT_S:\n"
)
_SECONDS_RETURN = "    return seconds\n"

# Codex round 2. The watch closed from the process's own cleanup path
# (finding .11.2.4) and the loop's willingness to keep serving after it drops
# an expired command (finding .11.2.6).
_SHUTDOWN_CLOSE = (
    "        if dispatch_watch is not None:\n"
    "            try:\n"
    "                dispatch_watch.end()\n"
)
_EXPIRY_CONTINUE = (
    "                    action, request_id, trace_id, deadline_monotonic,\n"
    "                    command_dequeue_monotonic, response_queue,\n"
    "                )\n"
    "                continue\n"
)

# Codex round 3. The lock that orders one dispatch's two log lines against
# each other (finding .11.2.7), and the watchdog's poll call itself, whose
# absence the silence tests could not previously see (finding .11.2.8).
#
# The report-lock pattern reaches into poll()'s first two statements because
# "with self._report_lock:" alone appears twice -- once here and once in
# end() -- and an ambiguous pattern would silently mutate the other method.
_POLL_REPORT_LOCK = (
    "        with self._report_lock:\n"
    "            with self._lock:\n"
    "                current = self._current\n"
    "                if current is None:\n"
)
_WATCHDOG_POLL_CALL = "            watch.poll()\n"

# Codex round 4 (finding .11.2.10). The sibling of watchdog-never-polls that
# the round-3 gate could not see: poll() IS called, and returns before it
# reads _current. The watchdog thread stays alive, every poll completes, and
# no stall or recovery line is written -- which is what all three silence
# tests assert. Only an observation of the dispatch itself separates that
# state from a correct quiet decision.
_POLL_BEFORE_THE_STATE_READ = (
    "        now = time.monotonic()\n"
    "        report_s = _watchdog_seconds(_DISPATCH_STALL_REPORT_S, 6.0)\n"
)

# wh-watchdog-stall-window. The sender-side gate: the whole conditional, so
# one mutation can silence the stamp and another can widen it to every
# action. Both directions matter -- a missing stamp leaves the two long
# handlers on the fixed limit, and a stamp on every action LOWERS the limit
# for actions awaited for less than it.
_APP_WINDOW_STAMP = (
    "        awaited_window = (\n"
    "            effective_timeout\n"
    "            if type(action) is str and action in _AWAITED_WINDOW_ACTIONS\n"
    "            else None\n"
    "        )\n"
)

# The reader, the hand-off into the watch, and the limit arithmetic: the
# three places the stamp can be dropped between the wire and the comparison.
_WINDOW_READER_RETURN = (
    "        return _watchdog_seconds(\n"
    "            command_message.get(\"_awaited_window_s\"), None,\n"
    "        )\n"
)
_WINDOW_HANDED_TO_THE_WATCH = (
    "            dispatch_watch.begin(\n"
    "                action, request_id, trace_id, command_dequeue_monotonic,\n"
    "                awaited_window_s,\n"
    "            )\n"
)
_WINDOW_LIMIT_ARITHMETIC = (
    "                if awaited_window_s is not None:\n"
    "                    report_s = awaited_window_s + _watchdog_seconds(\n"
    "                        _DISPATCH_STALL_WINDOW_MARGIN_S, 1.0,\n"
    "                    )\n"
)

# Review finding .1.1 widened the set, so the MEMBERSHIP needs its own pin
# beside the two gate mutations: a set that still stamps something keeps
# _APP_WINDOW_STAMP's mutations green while quietly dropping one action back
# onto the fixed limit.
_APP_WINDOW_ACTION_SET = (
    "_AWAITED_WINDOW_ACTIONS = frozenset({\n"
    "    \"click_element\",\n"
    "    \"click_snapshot_item\",\n"
    "    \"start_overlay_walk\",\n"
    "})\n"
)

# Review finding .1.2. The reader's protective except runs OUTSIDE the
# per-action try, so losing it ends the Input process over one bad envelope.
# The message line is part of the pattern because the bare "except Exception:"
# plus "logger.warning(" prefix appears more than once in this module -- the
# same ambiguity the deadline reader's mutation had to work around.
_WINDOW_READER_EXCEPT = (
    "    except Exception:\n"
    "        logger.warning(\n"
    "            \"awaited window could not be read; the dispatch will be watched \"\n"
    "            \"against the fixed stall limit\", exc_info=True,\n"
    "        )\n"
    "        return None\n"
)

# Codex round 5 (finding .11.2.11). The launcher's structural claim: a dead
# Input process takes its whole cycle down, and no path rebuilds it on its
# own. Until round 5 every process mock in the restart-cycle tests was dead
# on arrival, so the supervisor always reached its crash branch through the
# LOGIC check and the Input-only state was never created.
#
# The mutation respawns Input and then FALLS THROUGH to the existing crash
# check rather than continuing the supervisor loop. That shape is deliberate.
# The harness's shutdown event answers is_set False forever, so a branch that
# respawns and keeps supervising never ends -- observed once as a
# pytest-timeout abort, which is a gate failure and not a verdict. Falling
# through still produces the extra InputProcess construction the test asserts
# against, and it terminates.
_SUPERVISOR_DEATH_CHECK = (
    "                if not logic_alive or not input_alive or not gui_alive:\n"
)

# Codex round 6 (finding .11.2.13). The sibling of the round-5 mutation, and
# the one the round-5 fix left uncovered. Round 5 loosened the shared helper's
# construction count from "== 6" to ">= 6", on the stated ground that an extra
# construction is a finding each caller names for itself. Two of the four
# callers did not name it: one reads only process_calls[0:3] and [3:6], the
# other only process_calls[1] and [4], so a SEVENTH construction leaves every
# position they read untouched.
#
# The mutation therefore builds its extra process AFTER the supervisor loop
# ends, not inside the crash branch. Placement is the whole point: an extra
# construction inside a cycle shifts the later positions and the two blind
# callers would catch it by accident, proving nothing about the gap. This one
# lands at index 6, past everything they read. It also terminates by
# construction, since the statement runs once after the loop.
#
# Reproduced before the fix: the two name-sequence tests failed and the two
# position-reading tests PASSED (2 failed, 2 passed, 71 deselected). Both now
# assert an exact six.
_LAUNCHER_PID_FILE_CLEANUP = (
    "    if os.path.exists(PID_FILE_PATH):\n"
)

# Codex round 7 (finding .11.2.14). The restart-cycle tests read names,
# construction counts and argument positions, and never read the target. A
# process named InputProcess could therefore be built with the LOGIC
# entrypoint and the whole launcher test file stayed green -- observed as
# "75 passed" with no Input command reader started at all.
#
# Two mutations rather than one. The first is the finding's own case and the
# one that matters for this bead. The second moves the wrong target to the
# GUI position, because the new assertion compares a list of (name, target)
# pairs by index and "it works at index 1 so it works at index 2" is the kind
# of by-construction argument a gate exists to stop trusting.
_LAUNCHER_INPUT_CONSTRUCTION = (
    "            input_proc = multiprocessing.Process("
    "target=input_process_main, args=input_args, name=\"InputProcess\")\n"
)
_LAUNCHER_GUI_CONSTRUCTION = (
    "            gui_proc = multiprocessing.Process("
    "target=gui_process_target, args=gui_args, name=\"GuiProcess\")\n"
)

# Codex round 8 (finding .11.2.15). The two round-7 target mutations both
# point a NON-Logic role at the Logic entrypoint, so each one leaves one
# entrypoint used twice and another used not at all. A weakened assertion
# that only checked membership -- each of the three entrypoints appears once
# somewhere -- would still catch both, so the gate could not tell the ordered
# mapping from the membership one. A two-role SWAP keeps every entrypoint
# used exactly once and is caught only by the ordered comparison.
#
# Demonstrated rather than argued: with the test's comparison weakened to
# membership, both round-7 mutations were still caught and this one survived.
_LAUNCHER_LOGIC_AND_INPUT_CONSTRUCTIONS = (
    "            logic_proc = multiprocessing.Process("
    "target=start_logic_process, args=logic_args, name=\"LogicProcess\")\n"
    "            input_proc = multiprocessing.Process("
    "target=input_process_main, args=input_args, name=\"InputProcess\")\n"
)

# Codex round 8 (finding .11.2.16). The harness patched
# launcher.multiprocessing.Queue with return_value=Mock(), so all three
# queues of a cycle -- and all six across two cycles -- were the SAME object
# and no queue relationship could be seen even in principle. The Input
# process could be handed the commands-to-Logic queue where its response
# queue belongs and the whole launcher file stayed green at 76 passed.
#
# Two mutations again, for the same reason as round 7. The first breaks the
# Logic-to-Input response link, which is the one this bead depends on: the
# Input process answers a command on the response queue, and an answer sent
# on the wrong queue is a command that never gets one. The second SWAPS the
# two queues the GUI process receives, so every queue is still used exactly
# once and only an ordered slot check can see it.
_LAUNCHER_INPUT_ARGS = (
    "            input_args = (shm.name, command_ready_event, "
    "input_ready_event, response_queue, shutdown_event)\n"
)
_LAUNCHER_GUI_ARGS = (
    "            gui_args = (shutdown_event, commands_to_logic_queue, "
    "state_to_gui_queue, gui_shm_name)\n"
)
_LAUNCHER_LOGIC_ARGS = (
    "            logic_args = (shm.name, command_ready_event, "
    "input_ready_event, response_queue, SHARED_MEM_SIZE, shutdown_event, "
    "commands_to_logic_queue, state_to_gui_queue, gui_shm_name)\n"
)

MUTATIONS = [
    {
        "name": "launcher-swaps-the-logic-and-input-entrypoints",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_LOGIC_AND_INPUT_CONSTRUCTIONS,
        "new": (
            "            logic_proc = multiprocessing.Process("
            "target=input_process_main, args=logic_args, "
            "name=\"LogicProcess\")\n"
            "            input_proc = multiprocessing.Process("
            "target=start_logic_process, args=input_args, "
            "name=\"InputProcess\")\n"
        ),
        "expect": [
            "test_each_cycle_wires_every_process_to_its_own_entrypoint",
        ],
    },
    {
        "name": "launcher-input-answers-on-the-commands-queue",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_ARGS,
        "new": (
            "            input_args = (shm.name, command_ready_event, "
            "input_ready_event, commands_to_logic_queue, shutdown_event)\n"
        ),
        "expect": [
            "test_each_cycle_gives_the_processes_the_queues_they_must_share",
        ],
    },
    {
        "name": "launcher-swaps-the-two-queues-the-gui-receives",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_GUI_ARGS,
        "new": (
            "            gui_args = (shutdown_event, state_to_gui_queue, "
            "commands_to_logic_queue, gui_shm_name)\n"
        ),
        "expect": [
            "test_each_cycle_gives_the_processes_the_queues_they_must_share",
        ],
    },
    {
        "name": "launcher-input-process-runs-the-logic-entrypoint",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_CONSTRUCTION,
        "new": (
            "            input_proc = multiprocessing.Process("
            "target=start_logic_process, args=input_args, "
            "name=\"InputProcess\")\n"
        ),
        # Exactly one test, and that is the finding: the name, the argument
        # positions and the construction count are all untouched, so nothing
        # else in the file can see it.
        "expect": [
            "test_each_cycle_wires_every_process_to_its_own_entrypoint",
        ],
    },
    {
        "name": "launcher-gui-process-runs-the-logic-entrypoint",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_GUI_CONSTRUCTION,
        "new": (
            "            gui_proc = multiprocessing.Process("
            "target=start_logic_process, args=gui_args, "
            "name=\"GuiProcess\")\n"
        ),
        "expect": [
            "test_each_cycle_wires_every_process_to_its_own_entrypoint",
        ],
    },
    {
        "name": "launcher-builds-a-process-after-the-last-cycle",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_PID_FILE_CLEANUP,
        "new": (
            "    _extra = multiprocessing.Process("
            "target=input_process_main, args=input_args, name=\"InputProcess\")\n"
            "    if os.path.exists(PID_FILE_PATH):\n"
        ),
        # All four, deliberately. The two name-sequence tests caught this
        # before the round-6 fix; requiring the two position-reading tests
        # here as well is what pins the fix, because those two are the ones
        # that used to pass under it.
        "expect": [
            "test_the_input_process_is_never_rebuilt_on_its_own",
            "test_an_input_only_death_still_takes_the_whole_cycle_down",
            "test_all_three_processes_of_a_cycle_share_one_shutdown_event",
            "test_a_second_cycle_shares_nothing_with_the_first",
        ],
    },
    {
        "name": "launcher-rebuilds-input-on-its-own",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _SUPERVISOR_DEATH_CHECK,
        "new": (
            "                if not input_alive and logic_alive and gui_alive:\n"
            "                    input_proc = multiprocessing.Process("
            "target=input_process_main, args=input_args, name=\"InputProcess\")\n"
            "                    input_proc.start()\n"
            "                if not logic_alive or not input_alive or not gui_alive:\n"
        ),
        # Only the round-5 test catches this, and that is the whole finding:
        # the three older tests in the class drive every process dead, so the
        # mutated branch never runs in them and all three stay green.
        "expect": [
            "test_an_input_only_death_still_takes_the_whole_cycle_down",
        ],
    },
    {
        "name": "launcher-input-opens-the-gui-segment-for-commands",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_ARGS,
        "new": (
            "            input_args = (gui_shm_name, command_ready_event, "
            "input_ready_event, response_queue, shutdown_event)\n"
        ),
        # Two catchers, and the older one is why this mutation is here at
        # all: input_args[0] was the ONE element of the three tuples that
        # already had a reader before round 9, and it was a cross-cycle
        # freshness check rather than a within-cycle link. Both must fail,
        # or the enumeration has lost a row it used to cover.
        "expect": [
            "test_each_cycle_hands_every_process_the_exact_arguments_it_needs",
            "test_a_second_cycle_shares_nothing_with_the_first",
        ],
    },
    {
        "name": "launcher-logic-opens-the-gui-segment-for-commands",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_LOGIC_ARGS,
        "new": (
            "            logic_args = (gui_shm_name, command_ready_event, "
            "input_ready_event, response_queue, SHARED_MEM_SIZE, "
            "shutdown_event, commands_to_logic_queue, state_to_gui_queue, "
            "gui_shm_name)\n"
        ),
        # The Logic half of the same relationship, and before round 9 it
        # had no reader at all: 77 passed with this applied.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-input-waits-on-the-wrong-readiness-event",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_ARGS,
        "new": (
            "            input_args = (shm.name, input_ready_event, "
            "command_ready_event, response_queue, shutdown_event)\n"
        ),
        # The mutation codex asked for in finding .11.2.17, and the one
        # with the worst production consequence: the Logic process signals
        # the real command-ready event while the Input process waits on the
        # real input-ready event, so a spoken command never reaches the
        # Input loop. It preserves every construction count, name, target,
        # queue link, shutdown link and cross-cycle freshness check the
        # class made before round 9, and 77 passed with it applied.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-tells-logic-the-wrong-buffer-size",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_LOGIC_ARGS,
        "new": (
            "            logic_args = (shm.name, command_ready_event, "
            "input_ready_event, response_queue, SHARED_MEM_SIZE // 2, "
            "shutdown_event, commands_to_logic_queue, state_to_gui_queue, "
            "gui_shm_name)\n"
        ),
        # logic_args[4] is the only element in any of the three tuples that
        # is shared with nothing, so no identity link can cover it. Halving
        # it rather than deleting it keeps the tuple length at nine, which
        # is what makes this a test of the value instead of the length.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-gui-and-logic-disagree-about-the-overlay-segment",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_LOGIC_ARGS,
        "new": (
            "            logic_args = (shm.name, command_ready_event, "
            "input_ready_event, response_queue, SHARED_MEM_SIZE, "
            "shutdown_event, commands_to_logic_queue, state_to_gui_queue, "
            "shm.name)\n"
        ),
        # The Logic process would write overlay state into the command
        # segment while the GUI process reads the overlay segment, so the
        # numbered overlay never updates. 77 passed with this applied.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-input-construction-gains-a-lifecycle-keyword",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_CONSTRUCTION,
        "new": (
            "            input_proc = multiprocessing.Process("
            "target=input_process_main, args=input_args, "
            "name=\"InputProcess\", daemon=True)\n"
        ),
        # daemon is a real multiprocessing.Process keyword and it changes
        # process lifecycle semantics. Every other assertion in the class
        # reads a value under a known key, so before the call-shape check
        # existed this left the whole file at 78 passed.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-input-construction-stops-naming-its-arguments",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_CONSTRUCTION,
        "new": (
            "            input_proc = multiprocessing.Process("
            "None, input_process_main, \"InputProcess\", input_args)\n"
        ),
        # The other six tests in the class fail on this too, but they fail
        # with KeyError from whichever assertion reads a key first, which
        # names no cause. Only the enumeration test is listed, because only
        # it fails through an assertion that says what went wrong. A catch
        # by wreckage is not a catch.
        "expect": ["test_each_cycle_hands_every_process_the_exact_arguments_it_needs"],
    },
    {
        "name": "launcher-input-reaches-a-constructor-the-recorder-cannot-see",
        "target": LAUNCHER_TARGET,
        "test_file": LAUNCHER_TEST_FILE,
        "old": _LAUNCHER_INPUT_CONSTRUCTION,
        # A refactor to a DIFFERENT Process constructor is invisible to the
        # harness: patch("launcher.multiprocessing.Process") replaces one
        # module attribute, and multiprocessing.dummy.Process is not it. The
        # construction then never reaches the recorder at all, so no
        # assertion that reads process_calls can see it, however exhaustive
        # that assertion is. What catches it is the COUNT: two cycles of
        # three constructions must record six, and this records four.
        #
        # The import is inside the replacement because the gate applies one
        # replacement per mutation, and it is ALIASED for a reason that cost
        # a measurement to find. Writing it as "import multiprocessing.dummy"
        # binds the name "multiprocessing" as a LOCAL of _run_supervisor, so
        # every earlier use of multiprocessing in that function raises
        # UnboundLocalError: 24 tests then fail with no assertion at all, the
        # gate reports a catch, and nothing is proven. "from multiprocessing
        # import dummy as _mp_dummy" binds only _mp_dummy and leaves the
        # module name alone.
        #
        # This was measured before it was written down, because it had been
        # asserted twice by reasoning and never run (codex round 11 and the
        # orchestrator both claimed it). Scratchpad bypass_check.py also
        # measured the all-three variant and the real-multiprocessing.Process
        # variant; both report "got 0" through the same assertion, and the
        # real variant left no stray processes.
        "new": (
            "            from multiprocessing import dummy as _mp_dummy\n"
            "            input_proc = _mp_dummy.Process("
            "target=input_process_main, args=input_args, name=\"InputProcess\")\n"
        ),
        # Every name here fails through an assertion that NAMES the count.
        # The seven restart-cycle tests fail in the _run_two_cycles helper
        # with "expected two cycles of three processes, got 4", and
        # test_restart_flag_triggers_second_cycle fails with its own
        # "Expected 6 process starts (2 iterations), got 4".
        #
        # test_processes_get_join_with_grace_period also fails under this
        # mutation and is deliberately NOT listed. It fails with "expected
        # call not found: join(timeout=5)", which sends the reader to the
        # shutdown path rather than to the constructor. A catch that names
        # the wrong thing is a catch by wreckage.
        "expect": [
            "test_the_input_process_is_never_rebuilt_on_its_own",
            "test_each_cycle_wires_every_process_to_its_own_entrypoint",
            "test_each_cycle_gives_the_processes_the_queues_they_must_share",
            "test_each_cycle_hands_every_process_the_exact_arguments_it_needs",
            "test_an_input_only_death_still_takes_the_whole_cycle_down",
            "test_all_three_processes_of_a_cycle_share_one_shutdown_event",
            "test_a_second_cycle_shares_nothing_with_the_first",
            "test_restart_flag_triggers_second_cycle",
        ],
    },
    {
        "name": "report-lock-is-not-shared",
        "old": _POLL_REPORT_LOCK,
        # A fresh lock per call excludes nobody, which is the state before
        # the fix: the block structure and the indentation are unchanged, so
        # only the ordering guarantee goes away.
        "new": (
            "        with threading.Lock():\n"
            "            with self._lock:\n"
            "                current = self._current\n"
            "                if current is None:\n"
        ),
        "expect": [
            "test_a_stall_report_cannot_be_overtaken_by_its_own_recovery_line",
        ],
    },
    {
        "name": "poll-returns-before-reading-the-dispatch",
        "old": _POLL_BEFORE_THE_STATE_READ,
        # Unreachable code after a return compiles cleanly, so the rest of
        # the method is left in place rather than deleted: the mutant differs
        # from the original in exactly one decision.
        "new": (
            "        now = time.monotonic()\n"
            "        return\n"
            "        report_s = _watchdog_seconds(_DISPATCH_STALL_REPORT_S, 6.0)\n"
        ),
        "expect": [
            "test_the_watchdog_says_nothing_about_a_dispatch_that_finishes_in_time",
            "test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback",
            "test_a_dispatch_that_was_never_reported_is_not_announced_as_recovered",
        ],
    },
    {
        "name": "watchdog-never-polls",
        "old": _WATCHDOG_POLL_CALL,
        # "pass" rather than a deleted line: the try block would otherwise
        # be empty and the mutant would fail to compile, which the runner
        # reports as an error but which also proves nothing.
        "new": "            pass\n",
        # The three silence tests are the ones this is aimed at. Each
        # asserts an ABSENCE, and a watchdog that never polls produces every
        # one of those absences; before finding .11.2.8 they all passed under
        # this mutation. The positive stall tests fail under it too, which is
        # expected and not what earns the catch. Its sibling above covers the
        # case this one cannot reach: a poll that is called and returns
        # without looking.
        "expect": [
            "test_the_watchdog_says_nothing_about_a_dispatch_that_finishes_in_time",
            "test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback",
            "test_a_dispatch_that_was_never_reported_is_not_announced_as_recovered",
        ],
    },
    {
        "name": "never-report",
        "old": _GUARD,
        "new": "            if True:\n",
        "expect": [
            "test_a_stalled_dispatch_is_reported_while_it_is_still_stalled",
            "test_the_watchdog_does_not_answer_the_request_it_reports",
            "test_a_continuing_stall_is_repeated_but_not_on_every_poll",
            "test_a_stall_that_ends_is_reported_as_recovered",
        ],
    },
    {
        "name": "always-report",
        "old": _GUARD,
        "new": "            if False:\n",
        "expect": [
            "test_the_watchdog_says_nothing_about_a_dispatch_that_finishes_in_time",
            "test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback",
        ],
    },
    # ---- deepseek round 1, finding .11.1.1: the judgement the two mutations
    # above do not reach. poll() decides three things and end() a fourth.
    {
        "name": "no-repeat-suppression",
        "old": _REPEAT_GUARD,
        "new": "            if False:\n",
        "expect": [
            "test_a_continuing_stall_is_repeated_but_not_on_every_poll",
        ],
    },
    {
        "name": "no-recovery-line",
        "old": _RECOVERY_GATE,
        "new": "        if current is None or True:\n",
        "expect": [
            "test_a_stall_that_ends_is_reported_as_recovered",
        ],
    },
    {
        "name": "recovery-line-for-every-dispatch",
        "old": _RECOVERY_GATE,
        "new": "        if current is None:\n",
        "expect": [
            "test_a_dispatch_that_was_never_reported_is_not_announced_as_recovered",
        ],
    },
    {
        "name": "expiry-drop-not-logged",
        "old": _DROP_LOG,
        "new": "    pass\n",
        "expect": [
            "test_the_expired_command_drop_is_written_to_the_log",
        ],
    },
    # ---- deepseek round 1, finding .11.1.2: the deadline reader's malformed
    # cases. Each guard here turns "malformed" into either "expired" or
    # "process ends", both of which the contract forbids.
    {
        "name": "deadline-bool-accepted",
        "old": _DEADLINE_BOOL_GATE,
        "new": "        if not isinstance(value, (int, float)):\n",
        "expect": [
            "test_a_malformed_deadline_reads_as_no_deadline",
            "test_a_command_carrying_a_bool_deadline_is_still_executed",
        ],
    },
    {
        # Keeps the bool rejection, drops the numeric one. A numeric STRING
        # is the case that matters: float("1234.5") succeeds, so the envelope
        # would carry a live deadline built from text the sender never meant
        # as a number. Non-numeric shapes still resolve to None through the
        # protective except, which is why this needs its own mutation rather
        # than riding on deadline-bool-accepted.
        "name": "deadline-numeric-gate-dropped",
        "old": _DEADLINE_BOOL_GATE,
        "new": "        if isinstance(value, bool):\n",
        "expect": [
            "test_a_malformed_deadline_reads_as_no_deadline",
        ],
    },
    {
        "name": "deadline-non-finite-accepted",
        "old": _DEADLINE_FINITE_GATE,
        "new": "        if False:\n",
        "expect": [
            "test_a_malformed_deadline_reads_as_no_deadline",
        ],
    },
    {
        "name": "deadline-reader-reraises",
        # The message line is part of the pattern on purpose: the bare
        # "except Exception:\n        logger.warning(\n" prefix appears twice
        # in this file, and the gate reported it as ambiguous rather than
        # mutating whichever came first.
        "old": (
            "    except Exception:\n"
            "        logger.warning(\n"
            '            "delivery deadline could not be read; the command will be "\n'
        ),
        "new": (
            "    except Exception:\n"
            "        raise\n"
            "        logger.warning(\n"
            '            "delivery deadline could not be read; the command will be "\n'
        ),
        "expect": [
            "test_a_poisoned_key_does_not_escape_the_reader",
        ],
    },
    # ---- deepseek round 1, finding .11.1.3: the timing guard itself.
    {
        "name": "timing-guard-passthrough",
        "old": _SECONDS_GUARD,
        "new": "    if False:\n        return fallback\n",
        "expect": [
            "test_an_unusable_timing_value_falls_back_to_the_shipped_default",
            "test_a_bad_stall_limit_leaves_the_watchdog_reporting_at_the_fallback",
        ],
    },
    # ---- codex round 1, finding .11.2.1. The round-1 guard rejected the
    # wrong SHAPES and let through numbers the platform cannot use. These
    # three matter more than the shape guard above, because
    # _run_dispatch_watchdog reads the poll interval and waits on it OUTSIDE
    # the try that wraps poll: an exception there is not contained anywhere,
    # and it ends the watchdog thread for the rest of the run.
    {
        "name": "seconds-overflow-uncaught",
        "old": _SECONDS_CONVERSION_CATCH,
        "new": "    except (ValueError, TypeError):\n        return fallback\n",
        # 10 ** 400 is an int, so every isinstance check accepts it, and
        # float() on it raises OverflowError rather than returning inf.
        "expect": [
            "test_a_numeric_value_the_platform_cannot_use_falls_back",
            "test_the_returned_interval_is_a_plain_float_in_the_usable_range",
        ],
    },
    # ---- codex round 2, finding .11.2.3. The catch was a list of three
    # named types. isinstance admits subclasses, so float(value) runs the
    # value's own __float__, and nothing constrains what that raises.
    {
        "name": "seconds-conversion-catch-narrowed",
        "old": _SECONDS_CONVERSION_CATCH,
        "new": (
            "    except (OverflowError, ValueError, TypeError):\n"
            "        return fallback\n"
        ),
        "expect": [
            "test_a_value_whose_conversion_raises_falls_back",
            "test_a_poll_interval_that_cannot_be_converted_does_not_end_the_watchdog",
        ],
    },
    # ---- codex round 2, finding .11.2.4. Shutdown can end the loop while a
    # handler is blocked, so the top-of-next-turn end() never runs and a
    # reported stall is never closed in the log.
    {
        "name": "no-close-on-shutdown",
        "old": _SHUTDOWN_CLOSE,
        "new": (
            "        if dispatch_watch is not None:\n"
            "            try:\n"
            "                pass\n"
        ),
        "expect": [
            "test_a_stall_that_shutdown_interrupts_is_still_reported_as_recovered",
        ],
    },
    # ---- codex round 2, finding .11.2.6. Every assertion about a dropped
    # command is an ABSENCE, and a loop that stops serving satisfies all of
    # them. break rather than continue is the cheapest way to stop serving
    # without raising, so a mutant caught here is caught by the fresh-command
    # assertion and not by a crash on the way to it.
    {
        "name": "expiry-drop-ends-the-loop",
        "old": _EXPIRY_CONTINUE,
        "new": (
            "                    action, request_id, trace_id, deadline_monotonic,\n"
            "                    command_dequeue_monotonic, response_queue,\n"
            "                )\n"
            "                break\n"
        ),
        "expect": [
            "test_a_command_that_went_stale_while_the_loop_was_blocked_is_not_executed",
        ],
    },
    {
        "name": "seconds-ceiling-dropped",
        "old": _SECONDS_CEILING,
        "new": "    if not math.isfinite(seconds) or seconds <= 0:\n",
        # 1e308 is finite and positive and still 22 orders of magnitude
        # above what Event.wait accepts.
        "expect": [
            "test_a_numeric_value_the_platform_cannot_use_falls_back",
            "test_the_ceiling_is_the_platforms_own_wait_limit",
            "test_the_returned_interval_is_a_plain_float_in_the_usable_range",
            "test_a_poll_interval_the_platform_rejects_does_not_end_the_watchdog",
        ],
    },
    {
        "name": "seconds-returns-the-callers-object",
        "old": _SECONDS_RETURN,
        "new": "    return value\n",
        # The value is validated and then thrown away: a float subclass
        # goes back out, and whatever it does on comparison becomes the
        # watchdog's problem, in a place with no fallback to take.
        "expect": [
            "test_the_returned_interval_is_a_plain_float_in_the_usable_range",
        ],
    },
    # --- wh-watchdog-stall-window ------------------------------------------
    {
        "name": "sender-never-stamps-the-window",
        "target": APP_TARGET,
        "test_file": APP_TEST_FILE,
        "old": _APP_WINDOW_STAMP,
        "new": "        awaited_window = None\n",
        # With no stamp on the wire the Input watchdog is back on its fixed
        # limit for exactly the two actions that can outlive it.
        "expect": [
            "test_send_request_stamps_the_awaited_window_for_click_and_walk",
        ],
    },
    {
        "name": "sender-stamps-every-action",
        "target": APP_TARGET,
        "test_file": APP_TEST_FILE,
        "old": _APP_WINDOW_STAMP,
        "new": "        awaited_window = effective_timeout\n",
        # The other direction, and the quieter one: an action awaited for
        # less than the fixed limit would have its stall limit LOWERED by a
        # stamp it was never meant to carry.
        "expect": [
            "test_send_request_leaves_other_actions_without_a_window",
        ],
    },
    {
        "name": "reader-never-finds-a-window",
        "old": _WINDOW_READER_RETURN,
        "new": "        return None\n",
        # The reader is the only path from the envelope to the watch, so a
        # reader that always answers None is indistinguishable from a wire
        # that never carried the stamp.
        "expect": [
            "test_a_walk_inside_its_awaited_window_is_not_reported_as_a_stall",
            "test_a_walk_past_its_awaited_window_is_still_reported_as_a_stall",
        ],
    },
    {
        "name": "loop-drops-the-window-it-read",
        "old": _WINDOW_HANDED_TO_THE_WATCH,
        "new": (
            "            dispatch_watch.begin(\n"
            "                action, request_id, trace_id, command_dequeue_monotonic,\n"
            "                None,\n"
            "            )\n"
        ),
        # Read correctly and then thrown away at the hand-off: the reader's
        # own tests stay green under this, which is why the pair below is
        # what catches it.
        "expect": [
            "test_a_walk_inside_its_awaited_window_is_not_reported_as_a_stall",
            "test_a_walk_past_its_awaited_window_is_still_reported_as_a_stall",
        ],
    },
    {
        "name": "poll-ignores-the-window-it-holds",
        "old": _WINDOW_LIMIT_ARITHMETIC,
        # "if False:" rather than a deleted block: the body would otherwise
        # be empty and the mutant would fail to compile, which the runner
        # reports as an error but which proves nothing.
        "new": (
            "                if False:\n"
            "                    report_s = awaited_window_s + _watchdog_seconds(\n"
            "                        _DISPATCH_STALL_WINDOW_MARGIN_S, 1.0,\n"
            "                    )\n"
        ),
        "expect": [
            "test_a_walk_inside_its_awaited_window_is_not_reported_as_a_stall",
            "test_a_walk_past_its_awaited_window_is_still_reported_as_a_stall",
        ],
    },
    {
        "name": "limit-drops-the-margin",
        "old": _WINDOW_LIMIT_ARITHMETIC,
        "new": (
            "                if awaited_window_s is not None:\n"
            "                    report_s = awaited_window_s\n"
        ),
        # The window alone still passes both tests above -- each of them
        # holds the loop far from the margin band. Only the test that holds
        # it PAST the window and INSIDE the margin separates the two.
        "expect": [
            "test_a_walk_is_given_a_margin_beyond_its_awaited_window",
        ],
    },
    {
        "name": "margin-consumed-without-being-judged",
        "old": _WINDOW_LIMIT_ARITHMETIC,
        "new": (
            "                if awaited_window_s is not None:\n"
            "                    report_s = (\n"
            "                        awaited_window_s + _DISPATCH_STALL_WINDOW_MARGIN_S\n"
            "                    )\n"
        ),
        # A margin this module never chose then reaches the addition itself.
        # The raise is swallowed by _run_dispatch_watchdog's wrapper, so the
        # watchdog goes quiet for the rest of the run rather than crashing --
        # the same silent freeze the whole feature exists to remove.
        "expect": [
            "test_an_unusable_margin_leaves_a_stamped_dispatch_still_reported",
        ],
    },
    {
        "name": "sender-forgets-the-badge-click",
        "target": APP_TARGET,
        "test_file": APP_TEST_FILE,
        "old": _APP_WINDOW_ACTION_SET,
        "new": (
            "_AWAITED_WINDOW_ACTIONS = frozenset({\n"
            "    \"click_element\",\n"
            "    \"start_overlay_walk\",\n"
            "})\n"
        ),
        # The state this change shipped in for one round: a stamp exists, so
        # both _APP_WINDOW_STAMP mutations stay caught, but the numbered
        # overlay's click is back on the fixed limit and its false stall
        # report is back with it.
        "expect": [
            "test_send_request_stamps_the_awaited_window_for_click_and_walk",
        ],
    },
    {
        "name": "window-reader-reraises",
        "old": _WINDOW_READER_EXCEPT,
        "new": "    except Exception:\n        raise\n",
        # Nothing in the suite noticed this until finding .1.2 added the
        # poisoned-key test: the two walk tests send well-formed envelopes,
        # so a reader that raises only on hostile input left them green.
        "expect": [
            "test_a_poisoned_window_key_does_not_escape_the_reader",
        ],
    },
]


def _run_pytest(test_file=TEST_FILE):
    env = dict(os.environ)
    # Python decides whether cached bytecode is current from the source
    # file's (mtime, size). Both mutations here keep the file a different
    # length, but the cache is disabled anyway rather than relied on.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q", "-rf", "-p",
         "no:cacheprovider"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True,
        timeout=RUN_TIMEOUT_S,
    )


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            rest = line.split(" ", 1)[1]
            if "::" in rest:
                # rsplit for the same reason the collector uses it: a
                # class-based test reports as path::Class::method.
                names.add(rest.split(" ")[0].rsplit("::", 1)[1].split("[")[0])
    return names


def main():
    targets = {mutation.get("target", TARGET) for mutation in MUTATIONS}
    originals = {}
    for target in targets:
        raw = target.read_bytes()
        if b"\r\n" in raw:
            # The patterns above are written with LF. A file stored with
            # CRLF would make every multi-line pattern miss in a batch,
            # which reads as a survivor. Refuse rather than rewrite the
            # file's endings. Checked per target: a repository can mix
            # conventions per file, so one clean target proves nothing
            # about the next.
            print(f"ERROR: {target.name} uses CRLF; patterns here are LF")
            return 1
        originals[target] = raw
    sources = {t: raw.decode("utf-8") for t, raw in originals.items()}

    errors, survivors, caught = [], [], []

    # Every expected test name must exist before the first mutation. A name
    # that no longer exists can never appear in the failed set, so a genuine
    # catch would be reported as a survivor.
    existing = {}
    for test_file in {m.get("test_file", TEST_FILE) for m in MUTATIONS}:
        collect = subprocess.run(
            [sys.executable, "-m", "pytest", test_file, "--collect-only", "-q",
             "-p", "no:cacheprovider"],
            cwd=SERVICE_DIR, capture_output=True, text=True,
            timeout=RUN_TIMEOUT_S,
        )
        existing[test_file] = {
            # rsplit, not split: a class-based test collects as
            # path::Class::method, and taking the FIRST segment would keep
            # the class name and never match an expect entry. Every launcher
            # test is class-based (codex round 5, finding .11.2.11).
            line.rsplit("::", 1)[1].split("[")[0].strip()
            for line in collect.stdout.splitlines() if "::" in line
        }
    for mutation in MUTATIONS:
        known = existing[mutation.get("test_file", TEST_FILE)]
        for name in mutation["expect"]:
            if name not in known:
                errors.append(f"{mutation['name']}: no such test {name}")
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # A suite that is already red reports every mutation as caught for a
    # reason unrelated to the mutation. Every test file this run uses needs
    # its own baseline.
    for test_file in sorted(existing):
        baseline = _run_pytest(test_file)
        if baseline.returncode != 0:
            print(f"ERROR baseline is not green in {test_file}; refusing to start")
            print(baseline.stdout[-2000:])
            return 1
    print(f"baselines green; running {len(MUTATIONS)} mutations")

    try:
        for mutation in MUTATIONS:
            name = mutation["name"]
            target = mutation.get("target", TARGET)
            test_file = mutation.get("test_file", TEST_FILE)
            source = sources[target]
            original = originals[target]
            count = source.count(mutation["old"])
            if count != 1:
                errors.append(f"{name}: pattern matched {count} times, need 1")
                print(f"ERROR {name}: pattern matched {count} times")
                continue

            mutated = source.replace(mutation["old"], mutation["new"], 1)
            try:
                compile(mutated, str(target), "exec")
            except SyntaxError as exc:
                errors.append(f"{name}: mutant does not compile: {exc}")
                print(f"ERROR {name}: mutant does not compile")
                continue

            target.write_bytes(mutated.encode("utf-8"))
            try:
                result = _run_pytest(test_file)
            except subprocess.TimeoutExpired:
                target.write_bytes(original)
                errors.append(f"{name}: mutant never terminated")
                print(f"ERROR {name}: mutant never terminated")
                continue
            finally:
                target.write_bytes(original)

            combined = result.stdout + result.stderr
            if "+++ Timeout +++" in combined:
                errors.append(f"{name}: suite-timeout abort, no verdict")
                print(f"ERROR {name}: suite-timeout abort")
                continue

            failed = _failed_names(combined)
            missing = [n for n in mutation["expect"] if n not in failed]
            if missing:
                survivors.append(f"{name}: expected failures missing: {missing}")
                print(f"SURVIVED {name}: {missing} did not fail")
            else:
                caught.append(name)
                print(f"caught   {name}: {sorted(failed)}")
    finally:
        for target, raw in originals.items():
            target.write_bytes(raw)

    print(
        f"\nscope: all {len(MUTATIONS)} mutations for this feature, none skipped."
        f"\ncaught {len(caught)}, survivors {len(survivors)}, errors {len(errors)}"
    )
    for line in survivors + errors:
        print(f"  {line}")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
