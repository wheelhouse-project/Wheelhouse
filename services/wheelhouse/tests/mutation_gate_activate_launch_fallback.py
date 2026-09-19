"""Mutation gate for the spoken-name launch fallback (wh-activate-launch-fallback).

David's rulings, items 32 and 43 of QUESTIONS-2026-09-02.md: when
"x-ray activate outlook" matches no open window, look the spoken name up
among the installed programs and start it; when more than one program
matches, start nothing and list the names.

Run it from services/wheelhouse:

    python tests/mutation_gate_activate_launch_fallback.py

and to check only that the patterns still match and still compile,
without running any test:

    python tests/mutation_gate_activate_launch_fallback.py --check

Every test in the two test files was written alongside the fix rather
than against an existing defect, so this gate is what proves they can see
the defect at all. It breaks each of the three outcome branches in turn --
one match, several matches, no match -- and each of the matching rules the
lookup depends on.

Two source files, because the change has two halves: the decision (which
programs the words name) lives in utils/installed_programs.py, and the
outcome (start it, list them, say nothing matched) in input_proc.py.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR
line in the summary, and an expected test that failed for any reason other
than its own assertion. A verdict earned by an unrelated exception is no
verdict: the test never reached the assertion the mutation claims to break.

The gate detects each target file's own line endings and translates the
patterns to them before matching, so a later CRLF conversion of either
file cannot turn the multi-line patterns into a silent batch of
pattern-not-found errors. It never rewrites a file's endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
INPUT_PROC = SERVICE / "input_proc.py"
LOOKUP = SERVICE / "utils" / "installed_programs.py"

TEST_FILES = [
    "tests/test_input_proc_activate_window.py",
    "tests/test_installed_programs.py",
]

# --tb=line is what carries the failure REASON. This project's pytest
# prints its short summary as a bare "FAILED <nodeid>" with no reason
# suffix, so the summary alone cannot tell an assertion failure from a
# crash.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")

# A reason beginning with an exception class name means the test raised
# rather than failed its assertion. AssertionError is the one exception
# name that IS an assertion failure, and pytest.fail prints "Failed:".
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    # ---- input_proc.py: the three outcome branches ----
    {
        "name": "no-match-says-nothing",
        "file": INPUT_PROC,
        "old": """            logger.warning(f"No window found matching: {target}")
            _notice(f"No program matched {target}.")
""",
        "new": """            logger.warning(f"No window found matching: {target}")
""",
        "expect": ["test_no_match_starts_nothing_and_says_so"],
    },
    {
        # Input-level rather than a revert: the several-matches branch is
        # still structurally present, it just never fires, so the first
        # match is started behind the user's back.
        "name": "several-matches-starts-the-first",
        "file": INPUT_PROC,
        "old": "    if len(programs) > 1:\n",
        "new": "    if len(programs) > 99:\n",
        "expect": [
            "test_several_matches_start_nothing",
            "test_several_matches_keep_buffer_valid",
        ],
    },
    {
        "name": "several-matches-lists-nothing",
        "file": INPUT_PROC,
        "old": "        _notice(_several_matches_notice(programs))\n",
        "new": "        pass\n",
        "expect": [
            "test_several_matches_list_the_names",
            "test_a_long_list_of_names_is_capped",
        ],
    },
    {
        "name": "one-match-never-starts",
        "file": INPUT_PROC,
        "old": "                os.startfile(candidate.launch_target)\n",
        "new": "                pass\n",
        # test_spoken_name_launch_invalidates_buffer was dropped from this
        # list when .1.7 moved the invalidation after the launch: a
        # no-op startfile now reads as a launch that worked, so the buffer
        # is still invalidated and that test still passes.
        "expect": [
            "test_one_match_starts_that_program",
            "test_no_notifier_still_starts_the_program",
        ],
    },
    {
        # Input-level: the launch happens, but from the words the user
        # said instead of the program the lookup found. ShellExecute would
        # then fail on a name Windows cannot resolve, which is the whole
        # defect this branch exists to fix.
        "name": "one-match-starts-the-spoken-words",
        "file": INPUT_PROC,
        "old": "                os.startfile(candidate.launch_target)\n",
        "new": "                os.startfile(target)\n",
        "expect": [
            "test_one_match_starts_that_program",
            "test_no_notifier_still_starts_the_program",
        ],
    },
    {
        "name": "one-match-says-nothing",
        "file": INPUT_PROC,
        "old": '            _notice(f"Starting {program.name}")\n',
        "new": "            pass\n",
        "expect": ["test_one_match_says_what_it_is_starting"],
    },
    {
        # A failed launch used to say nothing at all: the user asked for a
        # program, nothing came, and only the log knew.
        "name": "failed-launch-says-nothing",
        "file": INPUT_PROC,
        "old": '        _notice(f"Could not start {program.name}.")\n',
        "new": "        pass\n",
        "expect": [
            "test_a_launch_failure_says_which_program_would_not_start",
            "test_every_candidate_failing_says_so_once",
        ],
    },
    {
        # The .exe arm of the entry above (wh-exe-launch-notice). The
        # two patterns cannot collide despite sharing an indent and a
        # verb: this one reads `notice(` and names {target}, the one
        # above reads `_notice(` and names {program.name}.
        "name": "failed-exe-launch-says-nothing",
        "file": INPUT_PROC,
        "old": '        notice(f"Could not start {target}.")\n',
        "new": "        pass\n",
        "expect": [
            "test_a_failed_exe_launch_says_which_program_would_not_start",
            "test_a_failed_exe_launch_never_says_it_is_slow",
        ],
    },
    {
        "name": "launch-skips-buffer-invalidation",
        "file": INPUT_PROC,
        "old": """            if buffer_manager is not None:
                buffer_manager.invalidate()
            _notice(f"Starting {program.name}")
""",
        "new": """            _notice(f"Starting {program.name}")
""",
        "expect": [
            "test_spoken_name_launch_invalidates_buffer",
            "test_a_fallback_that_starts_still_invalidates_the_buffer",
        ],
    },
    {
        # The cap the boss asked for: a Windows toast truncates silently,
        # so the notice must do the trimming itself and say how many it
        # left out.
        "name": "listed-names-uncapped",
        "file": INPUT_PROC,
        "old": '    listed = ", ".join(p.name for p in programs[:_MAX_LISTED_MATCHES])\n',
        "new": '    listed = ", ".join(p.name for p in programs)\n',
        "expect": ["test_a_long_list_of_names_is_capped"],
    },
    {
        # Criterion 5: an .exe keeps the direct-launch path and never
        # reaches the lookup. The two neighbouring lines make the pattern
        # unique -- the same assignment appears three times in this file.
        "name": "exe-target-reaches-the-lookup",
        "file": INPUT_PROC,
        "old": """        target = params.get("target") or ""
        is_process = target.lower().endswith(".exe")
""",
        "new": """        target = params.get("target") or ""
        is_process = False
""",
        "expect": [
            "test_an_exe_target_never_reaches_the_lookup",
            "test_exe_target_launches_when_no_window_found",
        ],
    },
    # ---- utils/installed_programs.py: the matching rules ----
    {
        "name": "exact-match-not-preferred",
        "file": LOOKUP,
        "old": "    return _without_duplicates(exact or starts_with)\n",
        "new": "    return _without_duplicates(exact + starts_with)\n",
        "expect": [
            "test_an_exact_match_wins_over_a_longer_name_that_also_starts_with_it"
        ],
    },
    {
        "name": "prefix-becomes-anywhere-in-the-name",
        "file": LOOKUP,
        "old": "        elif name.startswith(wanted):\n",
        "new": "        elif wanted in name:\n",
        "expect": ["test_a_word_in_the_middle_of_a_name_does_not_match"],
    },
    {
        "name": "blank-words-not-guarded",
        "file": LOOKUP,
        "old": """    if not wanted:
        return []
""",
        "new": """    if False:
        return []
""",
        "expect": ["test_empty_spoken_words_match_nothing"],
    },
    {
        "name": "matching-becomes-case-sensitive",
        "file": LOOKUP,
        "old": '    wanted = (spoken_name or "").strip().casefold()\n',
        "new": '    wanted = (spoken_name or "").strip()\n',
        "expect": ["test_matching_ignores_case_in_both_directions"],
    },
    {
        "name": "spoken-words-not-stripped",
        "file": LOOKUP,
        "old": '    wanted = (spoken_name or "").strip().casefold()\n',
        "new": '    wanted = (spoken_name or "").casefold()\n',
        "expect": ["test_the_spoken_words_are_stripped_before_matching"],
    },
    {
        "name": "duplicates-not-dropped",
        "file": LOOKUP,
        "old": "    return _without_duplicates(exact or starts_with)\n",
        "new": "    return list(exact or starts_with)\n",
        "expect": ["test_the_same_program_found_twice_is_returned_once"],
    },
    {
        "name": "duplicate-check-becomes-case-sensitive",
        "file": LOOKUP,
        "old": "        target = program.launch_target.casefold()\n",
        "new": "        target = program.launch_target\n",
        "expect": ["test_the_duplicate_check_ignores_the_case_of_the_path"],
    },
    # ---- the four source rules, ruled by David 2026-09-04 ----
    {
        # A comparison that is always false, rather than deleting the
        # guard: the block keeps its shape, so the mutant compiles and
        # every shortcut in Startup is reported again.
        "name": "the-startup-folder-is-scanned-again",
        "file": LOOKUP,
        "old": """            if folders_above and folders_above[0].casefold() == _AUTOSTART_FOLDER:
                continue
""",
        "new": """            if folders_above is None:
                continue
""",
        "expect": [
            "test_the_startup_folder_is_not_a_list_of_programs_to_start",
            "test_the_all_users_startup_folder_is_skipped_too",
        ],
    },
    {
        "name": "a-name-an-earlier-source-claimed-does-not-yield",
        "file": LOOKUP,
        "old": "        if claimed_by.get(name, program.source) != program.source:\n",
        "new": "        if claimed_by.get(name, program.source) is None:\n",
        "expect": [
            "test_an_app_paths_entry_yields_to_a_start_menu_shortcut",
            "test_a_shortcut_in_both_scopes_yields_to_the_user_scope",
            "test_the_notice_never_lists_one_name_twice",
        ],
    },
    {
        "name": "same-source-duplicates-collapse-too",
        "file": LOOKUP,
        "old": "        if claimed_by.get(name, program.source) != program.source:\n",
        "new": "        if name in claimed_by:\n",
        "expect": ["test_one_name_in_two_folders_of_one_scope_stays_ambiguous"],
    },
    {
        # The scope order is the whole of the cross-scope rule: whichever
        # scope is walked first claims the name.
        "name": "the-all-users-scope-is-walked-first",
        "file": LOOKUP,
        "old": """        (shellcon.CSIDL_PROGRAMS, USER_START_MENU),
        (shellcon.CSIDL_COMMON_PROGRAMS, COMMON_START_MENU),
""",
        "new": """        (shellcon.CSIDL_COMMON_PROGRAMS, COMMON_START_MENU),
        (shellcon.CSIDL_PROGRAMS, USER_START_MENU),
""",
        "expect": ["test_each_shortcut_carries_the_scope_it_came_from"],
    },
    {
        "name": "scan-failure-escapes",
        "file": LOOKUP,
        "old": """    try:
        candidates = scan()
    except Exception as exc:
        logger.warning("Installed-program lookup failed for %r: %s", spoken_name, exc)
        return []
""",
        "new": """    candidates = scan()
""",
        "expect": ["test_a_failing_scan_matches_nothing_instead_of_raising"],
    },
    {
        # wh-activate-launch-fallback.1.3, registry half. Mutating the
        # loop-continuation keyword rather than deleting a line: the
        # block keeps its shape, so the mutant compiles and reaches the
        # assertion instead of crashing upstream of it.
        "name": "one-bad-registry-entry-drops-the-rest",
        "file": LOOKUP,
        "old": """                        logger.warning("App Paths entry unreadable: %s", exc)
                        continue
""",
        "new": """                        logger.warning("App Paths entry unreadable: %s", exc)
                        break
""",
        "expect": ["test_one_unreadable_entry_does_not_drop_the_ones_after_it"],
    },
    {
        # wh-activate-launch-fallback.1.4. The old check read EVERY folder
        # component above the shortcut, so a vendor group named Startup
        # nested deeper than the one real autostart folder was dropped
        # with everything in it.
        "name": "nested-startup-folders-are-skipped-again",
        "file": LOOKUP,
        "old": "            if folders_above and folders_above[0].casefold() == _AUTOSTART_FOLDER:\n",
        "new": "            if any(part.casefold() == _AUTOSTART_FOLDER for part in folders_above):\n",
        "expect": [
            "test_a_startup_folder_deeper_in_the_tree_is_an_ordinary_group",
        ],
    },
    {
        # wh-activate-launch-fallback.1.5. Re-raising is what the missing
        # try/except amounted to: the failure leaves _app_paths_programs,
        # scan_installed_programs catches per source, and every entry
        # already collected from the earlier root/view pairs goes with it.
        "name": "an-uncountable-key-ends-the-whole-scan",
        "file": LOOKUP,
        "old": """                    logger.warning("App Paths key could not be counted: %s", exc)
                    continue
""",
        "new": """                    logger.warning("App Paths key could not be counted: %s", exc)
                    raise
""",
        "expect": ["test_a_key_that_cannot_be_counted_keeps_the_earlier_entries"],
    },
    {
        # wh-activate-launch-fallback.1.6, the lookup half: the entry that
        # yielded is thrown away instead of kept, so the launch has
        # nothing to fall back to.
        "name": "a-yielding-entry-is-forgotten",
        "file": LOOKUP,
        "old": """            displaced.setdefault(name, []).append(program)
            continue
""",
        "new": """            continue
""",
        "expect": [
            "test_an_entry_that_yields_is_kept_as_a_fallback",
            "test_the_fallbacks_keep_source_order",
        ],
    },
    {
        # The fallbacks belong to the winner alone. Without the check,
        # every entry of an ambiguous same-source pair carries them.
        "name": "every-repeat-carries-the-fallbacks",
        "file": LOOKUP,
        "old": "        if yielded and winner_by_name[name] is program:\n",
        "new": "        if yielded:\n",
        "expect": [
            "test_only_the_winner_of_an_ambiguous_pair_carries_the_fallbacks",
        ],
    },
    {
        # wh-activate-launch-fallback.1.6, the launch half, and the first
        # of the two entries the boss asked for: remove the fallback loop.
        "name": "the-fallback-loop-is-removed",
        "file": INPUT_PROC,
        "old": "        for candidate in (program, *program.fallbacks):\n",
        "new": "        for candidate in (program,):\n",
        "expect": [
            "test_a_stale_winner_falls_back_to_the_next_source",
            "test_the_fallbacks_are_tried_in_the_order_they_are_given",
        ],
    },
    {
        # The boss's second entry: reverse the candidate order, which
        # starts a yielded entry ahead of the winner David ruled for.
        "name": "the-candidate-order-is-reversed",
        "file": INPUT_PROC,
        "old": "        for candidate in (program, *program.fallbacks):\n",
        "new": "        for candidate in (*program.fallbacks, program):\n",
        "expect": [
            "test_a_stale_winner_falls_back_to_the_next_source",
            "test_the_fallbacks_are_tried_in_the_order_they_are_given",
            "test_a_winner_that_starts_never_reaches_its_fallbacks",
        ],
    },
    {
        # wh-activate-launch-fallback.1.7, the ORDERING. This inserts an
        # invalidation above the loop and leaves the one on the success
        # path in place, which looks like the appended-copy mistake the
        # mutation-gate skill warns about and is not: the copy's only
        # behavioural change is on the FAILURE path -- the buffer is lost
        # for a program that never started -- and that is exactly what
        # the expected test measures. The other half of the ordering, the
        # invalidation going missing altogether, is
        # launch-skips-buffer-invalidation above.
        "name": "the-buffer-is-invalidated-before-the-launch-too",
        "file": INPUT_PROC,
        "old": """        for candidate in (program, *program.fallbacks):
            try:
                os.startfile(candidate.launch_target)
""",
        "new": """        if buffer_manager is not None:
            buffer_manager.invalidate()
        for candidate in (program, *program.fallbacks):
            try:
                os.startfile(candidate.launch_target)
""",
        "expect": ["test_a_launch_that_failed_keeps_the_buffer_valid"],
    },
    # ---- input_proc.py: the launch runs off the command loop ----
    # wh-launch-off-command-loop, David's item 18 of
    # QUESTIONS-2026-09-04.md. Each of these puts a launch back on the
    # single Input command loop, or removes one of the two bounds the
    # boss ruled for on 2026-09-04.
    {
        # The spoken-name path back on the loop. This is the call site
        # the finding wh-activate-launch-fallback.1.8 reported.
        "name": "the-spoken-name-launch-runs-on-the-command-loop",
        "file": INPUT_PROC,
        "old": """            launch_thread = _launch_off_command_loop(
                target,
                lambda: _start_named_program(
                    target, logger, notify, find_programs, buffer_manager
                ),
                logger,
                _notice,
            )
""",
        "new": """            _start_named_program(
                target, logger, notify, find_programs, buffer_manager
            )
""",
        "expect": [
            "test_a_spoken_name_launch_that_blocks_does_not_hold_the_handler",
            "test_a_later_command_runs_while_a_spoken_name_launch_is_blocked",
            "test_the_installed_program_scan_also_runs_off_the_loop",
        ],
    },
    {
        # The .exe path back on the loop. It is the older and the more
        # common of the two, and criterion 5 of the launch-fallback
        # branch had frozen it.
        "name": "the-exe-launch-runs-on-the-command-loop",
        "file": INPUT_PROC,
        "old": """            launch_thread = _launch_off_command_loop(
                target,
                lambda: _launch_exe_target(target, logger, _notice),
                logger,
                _notice,
                before_start=(
                    None if buffer_manager is None
                    else buffer_manager.invalidate
                ),
            )
""",
        "new": """            if buffer_manager is not None:
                buffer_manager.invalidate()
            _launch_exe_target(target, logger, _notice)
""",
        "expect": [
            "test_an_exe_launch_that_blocks_does_not_hold_the_handler",
            "test_a_later_command_runs_while_an_exe_launch_is_blocked",
        ],
        # With the .exe launch back on the command loop, each of these
        # two tests fills the limit with four launches that now block
        # the loop in turn. Four ten-second waits pass the suite's
        # thirty-second per-test timeout, pytest kills the run before
        # the failure summary prints, and the mutation reports no
        # verdict at all. Neither test catches this mutation.
        "deselect": [
            "test_more_launches_than_the_limit_start_nothing_and_say_so",
            "test_a_refused_launch_keeps_the_buffer_valid",
        ],
    },
    {
        # The limit checked after the buffer invalidation instead of
        # before it. Boss ruling 2026-09-04: a launch the limit refuses
        # starts nothing and changes no focus, so it must not cost the
        # next buffer query a full UIA re-sync. This MOVES the call
        # rather than adding a copy, so the success path still runs it
        # exactly once and only the refusal path changes.
        "name": "the-limit-is-checked-after-the-buffer-invalidation",
        "file": INPUT_PROC,
        "old": """    running = _launches_in_flight()
    if len(running) >= _MAX_LAUNCHES_IN_FLIGHT:
        still = ", ".join(entry[0] for entry in running)
        logger.warning(
            f"Not starting {name}: {len(running)} launches have not "
            f"returned yet ({still})"
        )
        notice(_LAUNCH_BUSY_NOTICE)
        return None
    if before_start is not None:
        before_start()
""",
        "new": """    if before_start is not None:
        before_start()
    running = _launches_in_flight()
    if len(running) >= _MAX_LAUNCHES_IN_FLIGHT:
        still = ", ".join(entry[0] for entry in running)
        logger.warning(
            f"Not starting {name}: {len(running)} launches have not "
            f"returned yet ({still})"
        )
        notice(_LAUNCH_BUSY_NOTICE)
        return None
""",
        "expect": ["test_a_refused_launch_keeps_the_buffer_valid"],
    },
    {
        # The .exe launch stops invalidating the buffer at all. The
        # launched app takes focus, so the shadow buffer's contents no
        # longer describe the window in front of the user. The
        # spoken-name path has its own pair of mutations for this
        # (launch-skips-buffer-invalidation and the one after it).
        "name": "the-exe-launch-skips-the-buffer-invalidation",
        "file": INPUT_PROC,
        "old": """                before_start=(
                    None if buffer_manager is None
                    else buffer_manager.invalidate
                ),
""",
        "new": "                before_start=None,\n",
        "expect": ["test_launch_fallback_invalidates_buffer"],
    },
    {
        # The limit removed. Windows offers no way to interrupt a shell
        # call that never returns, so without the limit a user who
        # repeats the command leaves one stuck thread per attempt.
        "name": "the-in-flight-limit-is-removed",
        "file": INPUT_PROC,
        "old": "    if len(running) >= _MAX_LAUNCHES_IN_FLIGHT:\n",
        "new": "    if False:\n",
        "expect": [
            "test_more_launches_than_the_limit_start_nothing_and_say_so",
        ],
    },
    {
        # The limit counts every launch ever started rather than the
        # ones still running, so one finished launch would hold a slot
        # for the life of the process.
        "name": "the-limit-counts-launches-that-finished-too",
        "file": INPUT_PROC,
        "old": """        _launch_threads[:] = [
            entry for entry in _launch_threads if entry[1].is_alive()
        ]
""",
        "new": "        pass\n",
        "expect": ["test_a_launch_that_finished_frees_a_slot"],
    },
    {
        # The slow-launch notice never started on the .exe path.
        # "Starting <name>" is shown only after the shell call returns,
        # so without this timer a launch that never returns says nothing
        # at all and the user hears nothing after asking.
        "name": "the-exe-slow-launch-notice-never-starts",
        "file": INPUT_PROC,
        "old": """    timer = _slow_launch_timer(target, notice)
    timer.start()
""",
        "new": "    timer = _slow_launch_timer(target, notice)\n",
        "expect": [
            "test_a_blocked_exe_launch_says_it_is_taking_a_long_time",
        ],
    },
    {
        # The same timer on the spoken-name path.
        "name": "the-spoken-name-slow-notice-never-starts",
        "file": INPUT_PROC,
        "old": """    slow = _slow_launch_timer(name, _notice)
    slow.start()
""",
        "new": "    slow = _slow_launch_timer(name, _notice)\n",
        "expect": [
            "test_a_blocked_lookup_says_the_spoken_words_are_slow",
            "test_a_blocked_spoken_name_launch_names_the_program",
        ],
    },
    {
        # The timer is left running once the shell call has returned, so
        # the notice call that follows it -- which reaches a Windows
        # toast and can take longer than the limit -- can be overtaken by
        # the timer and a launch that worked is called slow.
        "name": "the-slow-notice-is-not-cancelled-when-the-launch-returns",
        "file": INPUT_PROC,
        "old": """            slow.cancel()
            # wh-review-pattern-fixes.8: the started program will take
""",
        "new": "            # wh-review-pattern-fixes.8: the started program will take\n",
        "expect": [
            "test_a_slow_notice_cannot_make_a_finished_launch_look_slow",
        ],
    },
    {
        # The timer left running when no candidate started at all. That
        # path never reaches the cancel above, so this one is the only
        # thing stopping "Could not start X." from being followed by
        # "X is taking a long time to start."
        "name": "the-slow-notice-is-not-cancelled-when-nothing-starts",
        "file": INPUT_PROC,
        "old": """    finally:
        slow.cancel()
""",
        "new": """    finally:
        pass
""",
        "expect": [
            "test_a_launch_where_nothing_starts_never_says_it_is_slow",
        ],
    },
    {
        # The .exe path's own cancel, which the gate had no mutation for
        # until deepseek filed wh-launch-off-command-loop.1.1. It is the
        # only thing stopping every finished .exe launch -- the one that
        # worked and the one that raised alike -- from being called slow
        # eight seconds after it already returned.
        "name": "the-exe-slow-notice-is-not-cancelled-when-the-launch-ends",
        "file": INPUT_PROC,
        "old": """    finally:
        timer.cancel()
""",
        "new": """    finally:
        pass
""",
        "expect": [
            "test_a_finished_exe_launch_never_says_it_is_slow",
            "test_a_failed_exe_launch_never_says_it_is_slow",
        ],
    },
    {
        # The worker body runs uncontained. The same finding: an
        # unexpected exception on the launch thread then ends in a bare
        # thread traceback on stderr, and the only record of a failed
        # launch never reaches the process log. The spoken-name lookup
        # runs entirely on that thread now, so a lookup that raises is
        # the ordinary way in.
        "name": "the-launch-worker-runs-uncontained",
        "file": INPUT_PROC,
        "old": """    thread = threading.Thread(
        target=_guarded, name=f"input-launch-{name}", daemon=True
    )
""",
        "new": """    thread = threading.Thread(
        target=work, name=f"input-launch-{name}", daemon=True
    )
""",
        "expect": [
            "test_a_worker_that_raises_is_logged_and_ends_only_its_thread",
        ],
    },
    {
        # The start moves back below the lookup, which is where it was
        # before codex filed wh-launch-off-command-loop.2.1. It is a
        # move, not a copy: a copy would leave the early start in place
        # and prove nothing about where the start belongs. The timer is
        # still BUILT above, so the finally below always has something
        # to cancel and no unrelated test dies on an unbound name.
        "name": "the-spoken-name-timer-starts-only-after-the-lookup",
        "file": INPUT_PROC,
        "old": """    slow.start()
    try:
        if find_programs is None:
            # Imported here, not at module scope, because every
            # project-local import in this file is deferred to the
            # process that uses it.
            from utils.installed_programs import find_installed_programs
            find_programs = find_installed_programs

        programs = find_programs(target)

        if not programs:
            logger.warning(f"No window found matching: {target}")
            _notice(f"No program matched {target}.")
            return

        if len(programs) > 1:
            logger.info(
                f"No window found for {target}; "
                f"{len(programs)} programs match, starting none"
            )
            _notice(_several_matches_notice(programs))
            return

        program = programs[0]
        # The lookup settled on a program, so the notice names that
        # program from here on. Before this point the words the user
        # said are the only name there is.
        name.name = program.name
""",
        "new": """    try:
        if find_programs is None:
            # Imported here, not at module scope, because every
            # project-local import in this file is deferred to the
            # process that uses it.
            from utils.installed_programs import find_installed_programs
            find_programs = find_installed_programs

        programs = find_programs(target)

        if not programs:
            logger.warning(f"No window found matching: {target}")
            _notice(f"No program matched {target}.")
            return

        if len(programs) > 1:
            logger.info(
                f"No window found for {target}; "
                f"{len(programs)} programs match, starting none"
            )
            _notice(_several_matches_notice(programs))
            return

        program = programs[0]
        # The lookup settled on a program, so the notice names that
        # program from here on. Before this point the words the user
        # said are the only name there is.
        name.name = program.name
        slow.start()
""",
        "expect": [
            "test_a_blocked_lookup_says_the_spoken_words_are_slow",
        ],
    },
]


def _pytest(*extra):
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _deselect(mut):
    """Drop the tests a mutation would make run until the suite timeout.

    A mutation can make a test that is NOT one of its catchers take far
    longer than pytest-timeout allows. pytest then kills the whole run
    before the failure summary prints, and the aborted run reports no
    verdict for a mutation its catchers may well have caught. Naming
    those tests here removes them from that one mutation's run and
    leaves every other test in it. main() refuses to start when a
    deselected name does not exist, or when it is also an expected
    catcher.
    """
    names = mut.get("deselect", ())
    if not names:
        return []
    return ["-k", " and ".join(f"not {name}" for name in names)]


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


def error_lines(output):
    return [line for line in output.splitlines() if line.startswith("ERROR ")]


def _sources():
    """Each target file's original bytes, text, and own line ending."""
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        original = path.read_bytes()
        newline = "\r\n" if b"\r\n" in original else "\n"
        sources[path] = {
            "original": original,
            "text": original.decode("utf-8"),
            "newline": newline,
        }
    return sources


def _translate(sources):
    """Rewrite every pattern into its target file's line ending."""
    for mut in MUTATIONS:
        newline = sources[mut["file"]]["newline"]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)


def _prepare(mut, sources):
    """Return (mutated_text, error). Exactly one match, and it must compile."""
    text = sources[mut["file"]]["text"]
    count = text.count(mut["old"])
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    try:
        compile(mutated, str(mut["file"]), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only(sources):
    """Report stale and non-compiling patterns without running a test.

    A --check that only counts matches can report clean while a mutation
    can never run, and every round that quotes that clean line spreads the
    false assurance.
    """
    stale, broken = [], []
    for mut in MUTATIONS:
        mutated, error = _prepare(mut, sources)
        if error is None:
            continue
        print("ERROR", error)
        if "does not compile" in error:
            broken.append(mut["name"])
        else:
            stale.append(mut["name"])
    print()
    print(
        f"checked {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(broken)} that do not compile"
    )
    return 1 if stale or broken else 0


def main():
    sources = _sources()
    _translate(sources)

    for path, source in sources.items():
        ending = "CRLF" if source["newline"] == "\r\n" else "LF"
        print(f"line endings in {path.name}: {ending}")

    if "--check" in sys.argv:
        return check_only(sources)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")

    # --only=<name> runs one mutation. The scope rule for a review round
    # is the mutations added or affected this round; the whole set runs
    # once before the final commit. A name that matches nothing is an
    # error, never a quiet run of zero mutations.
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    unknown = [n for n in only if n not in {m["name"] for m in MUTATIONS}]
    if unknown:
        print("ERROR --only names no mutation:", unknown)
        return 1
    selected = [m for m in MUTATIONS if not only or m["name"] in only]

    for mut in selected:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
        for name in mut.get("deselect", ()):
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: deselected test {name!r} does not exist"
                )
            if name in mut["expect"]:
                errors.append(
                    f"{mut['name']}: {name!r} is both expected and "
                    f"deselected, so the mutation could never be caught"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in selected:
        path = mut["file"]
        original = sources[path]["original"]
        mutated, error = _prepare(mut, sources)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        path.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*TEST_FILES, *_deselect(mut), *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            # write_bytes, never write_text: on Windows write_text would
            # translate every newline and silently rewrite the file's
            # endings, breaking every multi-line pattern afterwards.
            path.write_bytes(original)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        crashed = [
            r for r in failure_reasons(result.stdout)
            if not is_assertion_failure(r)
        ]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    skipped = len(MUTATIONS) - len(selected)
    print(
        f"scope: {len(selected)} of {len(MUTATIONS)} mutations, "
        + (f"{skipped} skipped by --only" if skipped else "none skipped")
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
