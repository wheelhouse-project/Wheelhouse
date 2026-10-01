"""Mutation gate for activating Microsoft Store programs (wh-activate-windows-terminal).

"x-ray activate terminal" matched nothing, and starting Windows Terminal
while it runs opens a second window. Windows Terminal is a Microsoft Store
program: it has no Start Menu shortcut and no App Paths key, its window
title is the active tab's title, and only the Windows apps folder lists
it. The fix reads that folder as a fourth lookup source and, before it
starts a Store program, looks for a window the program already has open.

Run it from services/wheelhouse:

    python tests/mutation_gate_activate_store_apps.py

and to check only that the patterns still match and still compile,
without running any test:

    python tests/mutation_gate_activate_store_apps.py --check

Every test in the two test files was written alongside the fix rather
than against an existing defect, so this gate is what proves they can see
the defect at all. It breaks, in turn: the running-window check and where
it sits, the four-source order, how a Store launch target is built, the
rules that turn apps-folder records into entries, the window search by
app ID, the process app-ID reader, the package display-name rules, and
the COM release order of the apps-folder reader.

wh-activate-windows-terminal.3 added: the bring-forward result that
_activate_window_impl and the Store branch report (boss rulings R1 and
R3), the poll that brings a just-started Store program's window forward
and its bound (acceptance item 4), and the foreground guard on that
bring-forward (boss ruling R4). The internals of
utils.foreground.steal_foreground are broken by
mutation_gate_foreground_steal.py.

The spoken-name outcome branches (one match, several, none) are broken by
mutation_gate_activate_launch_fallback.py and are not repeated here.

Two source files: the decision (which entries the Store source reports)
lives in utils/installed_programs.py, and the outcome (bring a running
window forward, or start the program) in input_proc.py.

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR
line in the summary, and an expected test that failed for any reason other
than its own assertion (a test outside the expected list may crash on the
mutant without changing the verdict). A verdict earned by an unrelated
exception is no verdict: the test never reached the assertion the
mutation claims to break. A test whose whole point is "this call does not raise" is the one
exception, and names the exception it expects in the mutation's ``raises``
field.

The gate detects each target file's own line endings and translates the
patterns to them before matching, so a CRLF file (installed_programs.py is
one) cannot turn the multi-line patterns into a silent batch of
pattern-not-found errors. It never rewrites a file's endings.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
INPUT_PROC = SERVICE / "input_proc.py"
LOOKUP = SERVICE / "utils" / "installed_programs.py"

TEST_FILES = [
    "tests/test_installed_programs.py",
    "tests/test_input_proc_activate_window.py",
]

# -rfE puts every failed and errored test into the short summary. The
# summary line carries the failure reason after " - ", provided the
# terminal is wide enough: pytest cuts the suffix at the terminal width,
# and a captured run has no terminal, so _pytest sets COLUMNS.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=no"]

# A reason beginning with an exception class name means the test raised
# rather than failed its assertion. AssertionError is the one exception
# name that IS an assertion failure, and pytest.fail prints "Failed:".
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

# ---------------------------------------------------------------------------
# input_proc.py: the block that brings a running Store program forward.
# Kept as one constant because the move mutation deletes it in one place
# and inserts it in another.
# ---------------------------------------------------------------------------
STORE_BLOCK = """        store_candidates = [
            c for c in (program, *program.fallbacks) if c.app_id
        ]
        for store_candidate in store_candidates:
            running_hwnd = _find_window_by_app_id(store_candidate.app_id, logger)
            if running_hwnd:
                slow.cancel()
                if buffer_manager is not None:
                    buffer_manager.invalidate()
                # wh-activate-windows-terminal.3: the log says what really
                # happened. A refusal still starts no second copy, because
                # a second copy of Windows Terminal is a second window.
                if _activate_window_impl(running_hwnd, logger):
                    logger.info(
                        f"{program.name} is already running; "
                        f"brought its window forward instead of starting it"
                    )
                    if reply is not None:
                        reply.verify_running()
                else:
                    logger.warning(
                        f"{program.name} is already running, but Windows "
                        f"refused to bring its window to the front"
                    )
                    if reply is not None:
                        reply.refuse(
                            f"Windows refused to bring {program.name} "
                            f"to the front"
                        )
                return
"""
AFTER_THE_START_LOOP = (
    "        # The user asked for a program and it did not come. Failing in\n"
)

# The same two names an existing test expects, used by several entries.
_RUNNING_TESTS = [
    "test_a_running_store_program_is_brought_forward_not_started",
    "test_the_package_name_of_a_running_store_program_brings_it_forward",
]
_PROCESS_ID_TESTS = [
    "test_a_process_with_that_app_id_matches_ignoring_case",
    "test_a_reader_that_raises_skips_only_that_window",
]

MUTATIONS = [
    # ---- input_proc.py: a running Store program is brought forward ----
    {
        # Criterion 1. The list is empty, so the running-window search
        # never runs and the program is started while it is running.
        "name": "the-running-window-check-never-runs",
        "file": INPUT_PROC,
        "old": """        store_candidates = [
            c for c in (program, *program.fallbacks) if c.app_id
        ]
""",
        "new": """        store_candidates = []
""",
        "expect": [
            *_RUNNING_TESTS,
            "test_bringing_a_running_program_forward_invalidates_the_buffer",
            "test_a_fallback_entry_with_a_running_window_is_brought_forward",
        ],
    },
    {
        # A MOVE, not a copy: the block is deleted above the start loop and
        # inserted below it. A launch that starts returns before it, so the
        # check is reached only when nothing started, and a program that is
        # already running is started a second time.
        "name": "the-running-window-check-moved-after-the-start-loop",
        "file": INPUT_PROC,
        "edits": [
            (STORE_BLOCK, ""),
            (AFTER_THE_START_LOOP, STORE_BLOCK + AFTER_THE_START_LOOP),
        ],
        "expect": [
            *_RUNNING_TESTS,
            "test_a_fallback_entry_with_a_running_window_is_brought_forward",
        ],
    },
    {
        # The window is found but the launch goes ahead anyway.
        "name": "a-running-program-is-brought-forward-and-started-anyway",
        "file": INPUT_PROC,
        # Since wh-activate-windows-terminal.3 the return follows the
        # if/else on the bring-forward result, so the pattern ends there.
        "old": """                        reply.refuse(
                            f"Windows refused to bring {program.name} "
                            f"to the front"
                        )
                return
""",
        "new": """                        reply.refuse(
                            f"Windows refused to bring {program.name} "
                            f"to the front"
                        )
                pass
""",
        "expect": [
            *_RUNNING_TESTS,
            "test_a_fallback_entry_with_a_running_window_is_brought_forward",
        ],
    },
    {
        "name": "the-running-window-is-never-activated",
        "file": INPUT_PROC,
        # Since wh-activate-windows-terminal.3 the call is the condition of
        # the if that picks the log line; the branch taken stays the
        # success branch, so only the call itself is gone.
        "old": "                if _activate_window_impl(running_hwnd, logger):\n",
        "new": "                if True:\n",
        "expect": [
            *_RUNNING_TESTS,
            "test_a_fallback_entry_with_a_running_window_is_brought_forward",
            "test_a_refused_bring_forward_is_logged_as_a_refusal",
            "test_an_accepted_bring_forward_keeps_the_existing_line",
        ],
    },
    {
        "name": "bringing-a-running-program-forward-skips-the-buffer-invalidation",
        "file": INPUT_PROC,
        "old": """                if buffer_manager is not None:
                    buffer_manager.invalidate()
                # wh-activate-windows-terminal.3: the log says what really
""",
        "new": """                # wh-activate-windows-terminal.3: the log says what really
""",
        "expect": [
            "test_bringing_a_running_program_forward_invalidates_the_buffer",
        ],
    },
    {
        # Input-level: the search runs, but for the words the user said
        # instead of the app ID the lookup found (criterion 3).
        "name": "the-window-search-uses-the-spoken-words",
        "file": INPUT_PROC,
        "old": "_find_window_by_app_id(store_candidate.app_id, logger)\n",
        "new": "_find_window_by_app_id(target, logger)\n",
        "expect": [
            *_RUNNING_TESTS,
            "test_a_fallback_entry_with_a_running_window_is_brought_forward",
        ],
    },
    {
        # A desktop program has no app ID, and an empty ID must never be
        # searched for.
        "name": "a-program-with-no-app-id-is-searched-for-anyway",
        "file": INPUT_PROC,
        "old": "            c for c in (program, *program.fallbacks) if c.app_id\n",
        "new": "            c for c in (program, *program.fallbacks)\n",
        "expect": ["test_a_program_with_no_app_id_never_searches_by_app_id"],
    },
    {
        # Only the winner is searched: a Store entry that yielded to a
        # Start Menu shortcut is the one with the app ID.
        "name": "the-fallbacks-are-not-searched-for-a-running-window",
        "file": INPUT_PROC,
        "old": "            c for c in (program, *program.fallbacks) if c.app_id\n",
        "new": "            c for c in (program,) if c.app_id\n",
        "expect": ["test_a_fallback_entry_with_a_running_window_is_brought_forward"],
    },
    # ---- input_proc.py: the bring-forward reports what Windows did ----
    # (wh-activate-windows-terminal.3, boss rulings R1 and R3)
    {
        # _activate_window_impl calls steal_foreground but reports success
        # whatever Windows answered.
        "name": "activate-ignores-the-steal-foreground-result",
        "file": INPUT_PROC,
        "old": "    if not steal_foreground(hwnd):\n",
        "new": "    if not (steal_foreground(hwnd) or True):\n",
        "expect": ["test_a_refusal_returns_false_warns_and_still_detaches"],
    },
    {
        # The Store branch logs "brought its window forward" on a refusal,
        # which is the false line David saw at 18:33.
        "name": "a-refused-running-window-logs-the-success-line",
        "file": INPUT_PROC,
        "old": "                if _activate_window_impl(running_hwnd, logger):\n",
        "new": "                if _activate_window_impl(running_hwnd, logger) or True:\n",
        "expect": ["test_a_refused_bring_forward_is_logged_as_a_refusal"],
    },
    # ---- input_proc.py: a refused found window answers with an error ----
    # (wh-activate-windows-terminal.3.2.1, boss ruling 20:21 option 1)
    {
        # The refusal is ignored, so the verification times out and the
        # awaited rule sends its hotkey to the window in front.
        "name": "a-refused-found-window-is-treated-as-brought-forward",
        "file": INPUT_PROC,
        "old": "            if not _activate_window_impl(target_hwnd, logger):\n",
        "new": "            if not (_activate_window_impl(target_hwnd, logger) or True):\n",
        "expect": ["test_an_awaited_refusal_answers_with_an_error"],
    },
    {
        # The bounded half: a window that came forward is still verified.
        "name": "every-found-window-counts-as-refused",
        "file": INPUT_PROC,
        "old": "            if not _activate_window_impl(target_hwnd, logger):\n",
        "new": "            if _activate_window_impl(target_hwnd, logger) or True:\n",
        "expect": ["test_an_awaited_success_is_still_verified"],
    },
    {
        "name": "the-refusal-answer-carries-no-error",
        "file": INPUT_PROC,
        # wh-safety-word-free-commands: the answer is built by
        # _ActivateReply.refuse now.
        "old": "            'request_id': self.request_id, 'error': True, 'refusal': True,\n",
        "new": "            'request_id': self.request_id, 'error': False, 'refusal': True,\n",
        "expect": ["test_an_awaited_refusal_answers_with_an_error"],
    },
    {
        # The error is sent, then the verification sends its success too.
        "name": "a-refusal-falls-through-to-the-verification",
        "file": INPUT_PROC,
        "old": """                        f"Windows refused to bring {target} to the front"
                    )
                return launch_thread
""",
        "new": """                        f"Windows refused to bring {target} to the front"
                    )
                pass
""",
        "expect": ["test_an_awaited_refusal_answers_with_an_error"],
    },
    {
        # A plain activate (no request id) answers too, which no caller
        # reads. wh-safety-word-free-commands: the answer object is only
        # built for an awaited activate, so the mutation builds it always.
        "name": "a-plain-refusal-answers-as-well",
        "file": INPUT_PROC,
        "old": """        if request_id:
            reply = _ActivateReply(
""",
        "new": """        if True:
            reply = _ActivateReply(
""",
        "expect": ["test_a_plain_refusal_sends_nothing"],
    },
    # ---- input_proc.py: a started Store program is brought forward ----
    # (acceptance item 4 and boss ruling R4)
    {
        "name": "a-started-store-program-is-never-brought-forward",
        "file": INPUT_PROC,
        "old": """            if candidate.app_id:
                _bring_started_store_window_forward(
""",
        "new": """            if False:
                _bring_started_store_window_forward(
""",
        "expect": [
            "test_the_new_window_is_brought_forward_when_it_appears",
            "test_a_window_that_never_appears_stops_at_the_bound",
            "test_a_fallback_that_started_is_the_one_polled",
            "test_an_unchanged_foreground_brings_the_new_window_forward",
        ],
    },
    {
        "name": "the-poll-stops-after-the-first-miss",
        "file": INPUT_PROC,
        "old": "        if _store_poll_clock() >= deadline:\n",
        "new": "        if True:\n",
        "expect": [
            "test_the_new_window_is_brought_forward_when_it_appears",
            "test_a_window_that_never_appears_stops_at_the_bound",
            "test_an_unchanged_foreground_brings_the_new_window_forward",
        ],
    },
    {
        # The bound is multiplied rather than the deadline check removed,
        # so the mutant cannot hang: every test that reaches the poll
        # either sets a fake clock that moves on each sleep (5 s becomes
        # 5000 fake seconds, 5000 quick loops) or runs under the autouse
        # bound of 0.0, which stays 0.0.
        "name": "the-poll-runs-past-its-bound",
        "file": INPUT_PROC,
        "old": "    deadline = _store_poll_clock() + _STORE_WINDOW_WAIT_S\n",
        "new": "    deadline = _store_poll_clock() + _STORE_WINDOW_WAIT_S * 1000\n",
        "expect": ["test_a_window_that_never_appears_stops_at_the_bound"],
    },
    {
        # Boss ruling R4 removed: the new window is always brought forward,
        # so it takes the focus from a window the user chose meanwhile.
        "name": "the-r4-foreground-guard-is-removed",
        "file": INPUT_PROC,
        "old": """            foreground_now = _foreground_window()
            if foreground_now == hwnd:
                logger.info(
                    f"Started {name}; its window came to the front by itself"
                )
            elif foreground_now != foreground_at_start:
                logger.info(
                    f"Started {name}, but did not bring its window to the "
                    f"front: the user moved to another window while it "
                    f"started"
                )
            else:
                _activate_window_impl(hwnd, logger)
""",
        "new": """            _activate_window_impl(hwnd, logger)
""",
        "expect": [
            "test_the_user_moved_to_another_window_so_it_is_not_brought_forward",
            "test_windows_already_brought_the_new_window_forward",
        ],
    },
    {
        "name": "a-changed-foreground-is-ignored",
        "file": INPUT_PROC,
        "old": "            elif foreground_now != foreground_at_start:\n",
        "new": "            elif False:\n",
        "expect": [
            "test_the_user_moved_to_another_window_so_it_is_not_brought_forward",
        ],
    },
    {
        # The new window already in front is treated as "the user moved".
        "name": "the-new-window-in-front-counts-as-a-changed-foreground",
        "file": INPUT_PROC,
        "old": "            if foreground_now == hwnd:\n",
        "new": "            if foreground_now == hwnd and False:\n",
        "expect": ["test_windows_already_brought_the_new_window_forward"],
    },
    {
        # The reverse: a window the user moved to is treated as the new
        # window having come forward by itself.
        "name": "a-changed-foreground-counts-as-the-new-window-in-front",
        "file": INPUT_PROC,
        "old": "            if foreground_now == hwnd:\n",
        "new": "            if foreground_now != foreground_at_start:\n",
        "expect": [
            "test_the_user_moved_to_another_window_so_it_is_not_brought_forward",
        ],
    },
    {
        # Input-level, and a MOVE: the window at the start is read after
        # os.startfile instead of before it, when the started program may
        # already hold the foreground.
        "name": "the-foreground-at-the-start-is-read-after-the-start",
        "file": INPUT_PROC,
        "edits": [
            ("""            foreground_at_start = (
                _foreground_window() if candidate.app_id else None
            )
""", ""),
            ("""                os.startfile(candidate.launch_target)
""", """                os.startfile(candidate.launch_target)
                foreground_at_start = (
                    _foreground_window() if candidate.app_id else None
                )
"""),
        ],
        "expect": [
            "test_the_user_moved_to_another_window_so_it_is_not_brought_forward",
            "test_windows_already_brought_the_new_window_forward",
            "test_an_unchanged_foreground_brings_the_new_window_forward",
        ],
    },
    # ---- input_proc.py: _find_window_by_app_id ----
    {
        # Compares the raw ID with the spoken-for ID, so the case the
        # process or window reports has to match exactly.
        "name": "the-app-id-comparison-is-case-sensitive",
        "file": INPUT_PROC,
        "old": "                if candidate and candidate.casefold() == wanted:\n",
        "new": "                if candidate and candidate == app_id:\n",
        "expect": [
            "test_a_process_with_that_app_id_matches_ignoring_case",
            "test_a_window_property_with_that_app_id_matches",
        ],
    },
    {
        "name": "the-wanted-app-id-is-not-casefolded",
        "file": INPUT_PROC,
        "old": '    wanted = (app_id or "").casefold()\n',
        "new": '    wanted = (app_id or "")\n',
        "expect": [
            "test_a_process_with_that_app_id_matches_ignoring_case",
            "test_a_window_property_with_that_app_id_matches",
        ],
    },
    {
        "name": "the-process-app-id-reader-is-dropped",
        "file": INPUT_PROC,
        "old": "            for read, key in ((_process_app_id, pid), (_window_app_id, hwnd)):\n",
        "new": "            for read, key in ((_window_app_id, hwnd),):\n",
        "expect": _PROCESS_ID_TESTS,
    },
    {
        "name": "the-window-property-reader-is-dropped",
        "file": INPUT_PROC,
        "old": "            for read, key in ((_process_app_id, pid), (_window_app_id, hwnd)):\n",
        "new": "            for read, key in ((_process_app_id, pid),):\n",
        "expect": ["test_a_window_property_with_that_app_id_matches"],
    },
    {
        "name": "an-invisible-window-is-searched",
        "file": INPUT_PROC,
        "old": "            if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):\n",
        "new": "            if not win32gui.GetWindowText(hwnd):\n",
        "expect": ["test_invisible_and_untitled_windows_are_skipped"],
    },
    {
        "name": "an-untitled-window-is-searched",
        "file": INPUT_PROC,
        "old": "            if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):\n",
        "new": "            if not win32gui.IsWindowVisible(hwnd):\n",
        "expect": ["test_invisible_and_untitled_windows_are_skipped"],
    },
    {
        # The whole test is that the call does not raise, so the escaped
        # exception IS the test's failure.
        "name": "an-enumeration-failure-escapes",
        "file": INPUT_PROC,
        "old": """    except Exception as e:
        # A window destroyed during the walk; also what a callback that
""",
        "new": """    except KeyError as e:
        # A window destroyed during the walk; also what a callback that
""",
        "expect": ["test_an_enumeration_failure_is_swallowed"],
        "raises": {"test_an_enumeration_failure_is_swallowed": "OSError"},
    },
    # ---- input_proc.py: _process_app_id ----
    {
        # A nonzero GetApplicationUserModelId return (15703 for a desktop
        # process) treated as success: the condition can no longer fire on
        # a positive error code.
        "name": "a-nonzero-app-model-return-is-treated-as-success",
        "file": INPUT_PROC,
        "old": "        if kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), buffer) != 0:\n",
        "new": "        if kernel32.GetApplicationUserModelId(handle, ctypes.byref(length), buffer) < 0:\n",
        "expect": ["test_a_desktop_process_reports_none_and_is_closed"],
    },
    {
        "name": "a-process-that-cannot-be-opened-is-queried-anyway",
        "file": INPUT_PROC,
        "old": """    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(256)
""",
        "new": """    if False:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(256)
""",
        "expect": ["test_a_process_that_cannot_be_opened_reports_none"],
    },
    {
        "name": "the-process-handle-is-never-closed",
        "file": INPUT_PROC,
        "old": "        kernel32.CloseHandle(handle)\n",
        "new": "        pass\n",
        "expect": [
            "test_a_packaged_process_reports_its_app_id",
            "test_a_desktop_process_reports_none_and_is_closed",
        ],
    },
    {
        "name": "the-process-is-opened-with-a-different-access-mask",
        "file": INPUT_PROC,
        "old": "    handle = kernel32.OpenProcess(0x1000, False, pid)\n",
        "new": "    handle = kernel32.OpenProcess(0x0400, False, pid)\n",
        "expect": ["test_a_packaged_process_reports_its_app_id"],
    },
    {
        "name": "an-empty-app-id-is-returned-as-it-is",
        "file": INPUT_PROC,
        "old": "        return buffer.value or None\n",
        "new": "        return buffer.value\n",
        "expect": ["test_a_packaged_process_with_an_empty_app_id_reports_none"],
    },
    # ---- utils/installed_programs.py: the four sources ----
    {
        "name": "the-store-source-is-dropped-from-the-scan",
        "file": LOOKUP,
        "old": "    for source in (_start_menu_programs, _app_paths_programs, _store_programs):\n",
        "new": "    for source in (_start_menu_programs, _app_paths_programs):\n",
        "expect": ["test_the_store_source_runs_after_the_other_three"],
    },
    {
        "name": "the-store-source-runs-before-app-paths",
        "file": LOOKUP,
        "old": "    for source in (_start_menu_programs, _app_paths_programs, _store_programs):\n",
        "new": "    for source in (_start_menu_programs, _store_programs, _app_paths_programs):\n",
        "expect": ["test_the_store_source_runs_after_the_other_three"],
    },
    {
        "name": "a-failing-source-in-the-scan-escapes",
        "file": LOOKUP,
        "old": """            programs.extend(source())
        except Exception as exc:
""",
        "new": """            programs.extend(source())
        except KeyError as exc:
""",
        "expect": ["test_a_failing_store_source_keeps_the_others"],
        "raises": {"test_a_failing_store_source_keeps_the_others": "OSError"},
    },
    {
        "name": "a-failing-store-reader-escapes-the-store-source",
        "file": LOOKUP,
        "old": """        return _store_programs_from_records(_read_store_app_records())
    except Exception as exc:
""",
        "new": """        return _store_programs_from_records(_read_store_app_records())
    except KeyError as exc:
""",
        "expect": ["test_the_store_source_never_raises_when_the_reader_fails"],
    },
    # ---- utils/installed_programs.py: records into entries ----
    {
        # Criterion 3: the launch target is built from the app ID the
        # folder returned, never from a name.
        "name": "the-store-launch-target-is-built-from-the-tile-name",
        "file": LOOKUP,
        "old": "        target = _STORE_TARGET_PREFIX + app_id\n",
        "new": "        target = _STORE_TARGET_PREFIX + tile_name\n",
        "expect": [
            "test_the_launch_target_is_built_from_the_app_id_alone",
            "test_terminal_finds_exactly_the_store_program",
            "test_windows_terminal_finds_exactly_the_store_program",
        ],
    },
    {
        # An app ID with no "!" is a desktop program the first two
        # sources already report.
        "name": "an-id-without-an-exclamation-mark-is-a-store-app",
        "file": LOOKUP,
        "old": r'''        if not bang or not tile_name or set(family) & set("\\/:"):
''',
        "new": r'''        if not tile_name or set(family) & set("\\/:"):
''',
        "expect": ["test_only_ids_with_an_exclamation_mark_are_store_apps"],
    },
    {
        "name": "the-path-guard-before-the-exclamation-mark-is-removed",
        "file": LOOKUP,
        "old": r'''        if not bang or not tile_name or set(family) & set("\\/:"):
''',
        "new": r'''        if not bang or not tile_name:
''',
        "expect": [
            "test_a_path_that_happens_to_hold_an_exclamation_mark_is_not_a_store_app"
        ],
    },
    {
        "name": "a-record-with-no-tile-name-is-listed",
        "file": LOOKUP,
        "old": r'''        if not bang or not tile_name or set(family) & set("\\/:"):
''',
        "new": r'''        if not bang or set(family) & set("\\/:"):
''',
        "expect": ["test_a_record_with_no_tile_name_is_not_listed"],
    },
    {
        "name": "a-backslash-before-the-exclamation-mark-is-allowed",
        "file": LOOKUP,
        "old": r'''set(family) & set("\\/:")''',
        "new": r'''set(family) & set("/:")''',
        "expect": ["test_a_package_family_holding_a_path_character_is_not_a_store_app"],
    },
    {
        "name": "a-slash-before-the-exclamation-mark-is-allowed",
        "file": LOOKUP,
        "old": r'''set(family) & set("\\/:")''',
        "new": r'''set(family) & set("\\:")''',
        "expect": ["test_a_package_family_holding_a_path_character_is_not_a_store_app"],
    },
    {
        "name": "a-colon-before-the-exclamation-mark-is-allowed",
        "file": LOOKUP,
        "old": r'''set(family) & set("\\/:")''',
        "new": r'''set(family) & set("\\/")''',
        "expect": ["test_a_package_family_holding_a_path_character_is_not_a_store_app"],
    },
    {
        "name": "the-store-entry-carries-no-app-id",
        "file": LOOKUP,
        "old": """                source=STORE_APPS,
                app_id=app_id,
            )
        )
        if (
""",
        "new": """                source=STORE_APPS,
            )
        )
        if (
""",
        "expect": ["test_an_entry_carries_its_source_and_app_id"],
    },
    {
        "name": "the-store-entry-carries-the-wrong-source",
        "file": LOOKUP,
        "old": """                source=STORE_APPS,
                app_id=app_id,
            )
        )
        if (
""",
        "new": """                source=APP_PATHS,
                app_id=app_id,
            )
        )
        if (
""",
        "expect": ["test_an_entry_carries_its_source_and_app_id"],
    },
    {
        # More than one app in the package: one package name would claim
        # every one of them.
        "name": "a-multi-app-package-gets-a-package-name-entry",
        "file": LOOKUP,
        "old": "            package_app_count == 1\n",
        "new": "            package_app_count >= 1\n",
        "expect": ["test_a_two_app_package_adds_no_package_name_entry"],
    },
    {
        "name": "a-one-app-package-gets-no-package-name-entry",
        "file": LOOKUP,
        "old": "            package_app_count == 1\n",
        "new": "            package_app_count == 2\n",
        "expect": [
            "test_a_one_app_package_adds_an_entry_under_the_package_name",
            "test_windows_terminal_finds_exactly_the_store_program",
        ],
    },
    {
        # The None-safe form of "the missing-name guard is gone": None
        # becomes an empty string, which differs from the tile name, so an
        # entry named None is added. Deleting the guard outright would
        # raise AttributeError on None before the assertion, which proves
        # nothing.
        "name": "a-missing-package-name-adds-an-entry",
        "file": LOOKUP,
        "old": """            and package_name
            and package_name.casefold() != tile_name.casefold()
""",
        "new": """            and (package_name or "").casefold() != tile_name.casefold()
""",
        "expect": ["test_a_missing_package_name_adds_nothing"],
    },
    {
        "name": "a-package-name-equal-to-the-tile-name-adds-an-entry-case-sensitively",
        "file": LOOKUP,
        "old": "            and package_name.casefold() != tile_name.casefold()\n",
        "new": "            and package_name != tile_name\n",
        "expect": ["test_a_package_name_equal_to_the_tile_name_adds_nothing"],
    },
    {
        "name": "a-package-name-equal-to-the-tile-name-adds-an-entry",
        "file": LOOKUP,
        "old": "            and package_name.casefold() != tile_name.casefold()\n",
        "new": "            and True\n",
        "expect": ["test_a_package_name_equal_to_the_tile_name_adds_nothing"],
    },
    {
        # Input-level: the package-name entry starts a name, not the app ID.
        "name": "the-package-name-entry-is-built-from-the-package-name",
        "file": LOOKUP,
        "old": """                    name=package_name,
                    launch_target=target,
""",
        "new": """                    name=package_name,
                    launch_target=_STORE_TARGET_PREFIX + package_name,
""",
        "expect": [
            "test_a_one_app_package_adds_an_entry_under_the_package_name",
            "test_windows_terminal_finds_exactly_the_store_program",
        ],
    },
    {
        "name": "the-package-name-entry-carries-no-app-id",
        "file": LOOKUP,
        "old": """                    name=package_name,
                    launch_target=target,
                    source=STORE_APPS,
                    app_id=app_id,
""",
        "new": """                    name=package_name,
                    launch_target=target,
                    source=STORE_APPS,
""",
        "expect": ["test_a_one_app_package_adds_an_entry_under_the_package_name"],
    },
    # ---- utils/installed_programs.py: _resolve_package_display_name ----
    {
        "name": "plain-text-is-not-returned-as-it-is",
        "file": LOOKUP,
        "old": """        return raw_name
    package_name = package_full_name.split""",
        "new": """        return None
    package_name = package_full_name.split""",
        "expect": ["test_plain_text_is_returned_without_loading_the_dll"],
    },
    {
        # None-safe: None still returns None, "" now returns "".
        "name": "an-empty-raw-name-is-returned-as-it-is",
        "file": LOOKUP,
        "old": """    if not raw_name:
        return None
""",
        "new": """    if not raw_name:
        return raw_name
""",
        "expect": ["test_an_empty_raw_name_resolves_to_nothing"],
    },
    {
        "name": "a-full-resource-reference-is-rebuilt",
        "file": LOOKUP,
        "old": """    if raw_name.lower().startswith("ms-resource://"):
        resource = raw_name
""",
        "new": """    if False:
        resource = raw_name
""",
        "expect": ["test_a_full_reference_is_passed_through_unchanged"],
    },
    {
        "name": "a-reference-with-a-path-is-put-under-resources",
        "file": LOOKUP,
        "old": '        if "/" in rest:\n',
        "new": "        if False:\n",
        "expect": ["test_a_reference_with_a_path_uses_that_path_under_the_package"],
    },
    {
        "name": "a-short-reference-is-not-put-under-resources",
        "file": LOOKUP,
        "old": 'resource = f"ms-resource://{package_name}/Resources/{rest}"\n',
        "new": 'resource = f"ms-resource://{package_name}/{rest}"\n',
        "expect": ["test_a_short_reference_names_the_resources_file_of_the_package"],
    },
    {
        "name": "the-package-name-is-the-full-name",
        "file": LOOKUP,
        "old": '    package_name = package_full_name.split("_", 1)[0]\n',
        "new": "    package_name = package_full_name\n",
        "expect": [
            "test_a_short_reference_names_the_resources_file_of_the_package",
            "test_a_reference_with_a_path_uses_that_path_under_the_package",
        ],
    },
    {
        "name": "the-loader-source-is-not-wrapped-in-the-indirect-string-form",
        "file": LOOKUP,
        "old": '    source = f"@{{{package_full_name}?{resource}}}"\n',
        "new": '    source = f"{{{package_full_name}?{resource}}}"\n',
        "expect": [
            "test_a_short_reference_names_the_resources_file_of_the_package",
            "test_a_full_reference_is_passed_through_unchanged",
        ],
    },
    {
        "name": "a-failed-load-is-treated-as-success",
        "file": LOOKUP,
        "old": """    if load(source, buffer, len(buffer), None) != 0:
        return None
""",
        "new": """    load(source, buffer, len(buffer), None)
""",
        "expect": ["test_a_failed_load_resolves_to_nothing"],
    },
    {
        "name": "an-output-that-is-still-a-reference-is-used",
        "file": LOOKUP,
        "old": """    if not resolved or resolved.lower().startswith("ms-resource"):
""",
        "new": """    if not resolved:
""",
        "expect": ["test_an_output_that_is_still_a_reference_resolves_to_nothing"],
    },
    {
        "name": "an-empty-loader-output-is-used",
        "file": LOOKUP,
        "old": """    if not resolved or resolved.lower().startswith("ms-resource"):
""",
        "new": """    if resolved.lower().startswith("ms-resource"):
""",
        "expect": ["test_an_empty_output_resolves_to_nothing"],
    },
    {
        "name": "a-leading-slash-is-kept-in-a-reference-with-a-path",
        "file": LOOKUP,
        "old": '        rest = raw_name[len("ms-resource:"):].lstrip("/")\n',
        "new": '        rest = raw_name[len("ms-resource:"):]\n',
        "expect": ["test_a_leading_slash_is_dropped_from_a_reference_with_a_path"],
    },
    # ---- utils/installed_programs.py: the apps-folder reader ----
    {
        # D1. A MOVE: CoUninitialize now runs before the enumeration, so
        # every COM object is released after COM is gone.
        "name": "couninitialize-runs-before-the-enumeration",
        "file": LOOKUP,
        "edits": [
            (
                """    if initialised:
        pythoncom.CoUninitialize()
    return records
""",
                "    return records\n",
            ),
            (
                """    try:
        records = _enumerate_store_apps(shell, shellcon, propsys)
""",
                """    if initialised:
        pythoncom.CoUninitialize()
    try:
        records = _enumerate_store_apps(shell, shellcon, propsys)
""",
            ),
        ],
        "expect": [
            "test_every_object_is_released_before_couninitialize",
            "test_the_failure_path_releases_first_and_returns_an_empty_list",
        ],
    },
    {
        # D1. The reader's own frame holds a COM object when
        # CoUninitialize runs, which is what the shell calls living in
        # this function used to do.
        "name": "the-reader-frame-holds-a-com-object-at-couninitialize",
        "file": LOOKUP,
        "old": """        records = _enumerate_store_apps(shell, shellcon, propsys)
    except Exception as exc:
""",
        "new": """        held = shell.SHGetDesktopFolder()
        records = _enumerate_store_apps(shell, shellcon, propsys)
    except Exception as exc:
""",
        "expect": [
            "test_every_object_is_released_before_couninitialize",
            "test_the_failure_path_releases_first_and_returns_an_empty_list",
        ],
    },
    {
        # D1, failure path: the exception (and with it the traceback that
        # references the helper's frame and its COM objects) is still
        # referenced when CoUninitialize runs.
        "name": "the-failure-path-keeps-the-exception-past-couninitialize",
        "file": LOOKUP,
        "old": """        records = []
    if initialised:
""",
        "new": """        records = []
        kept = exc
    if initialised:
""",
        "expect": ["test_the_failure_path_releases_first_and_returns_an_empty_list"],
    },
    {
        # D4-adjacent: the log record holds the exception object, and with
        # it the traceback, for as long as a log handler keeps the record.
        "name": "the-failure-log-holds-the-exception-object",
        "file": LOOKUP,
        "old": """        logger.warning("Microsoft Store lookup unavailable: %s", str(exc))
        records = []
""",
        "new": """        logger.warning("Microsoft Store lookup unavailable: %s", exc)
        records = []
""",
        "expect": ["test_the_failure_path_releases_first_and_returns_an_empty_list"],
    },
    {
        # D4: one item whose name cannot be read must not end the read.
        "name": "one-unreadable-item-ends-the-whole-read",
        "file": LOOKUP,
        "old": """        except Exception as exc:
            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
""",
        "new": """        except KeyError as exc:
            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
""",
        "expect": [
            "test_one_item_whose_name_cannot_be_read_does_not_end_the_read",
        ],
    },
    {
        "name": "one-unreadable-item-drops-the-items-after-it",
        "file": LOOKUP,
        "old": """            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
            continue
""",
        "new": """            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
            break
""",
        "expect": [
            "test_one_item_whose_name_cannot_be_read_does_not_end_the_read",
        ],
    },
    {
        # Review finding wh-activate-windows-terminal.1.1: the queue handler
        # keeps a record unformatted until its listener thread writes it, so
        # an exception in the record's arguments keeps the COM objects in its
        # traceback alive past CoUninitialize.
        "name": "the-unreadable-item-log-holds-the-exception-object",
        "file": LOOKUP,
        "old": """            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
""",
        "new": """            logger.debug("Apps-folder item unreadable, skipped: %s", exc)
""",
        "expect": ["test_an_unreadable_item_is_logged_as_text_only"],
    },
    {
        "name": "the-unreadable-package-name-log-holds-the-exception-object",
        "file": LOOKUP,
        "old": """                logger.debug("Package name of %s unreadable: %s", app_id, str(exc))
""",
        "new": """                logger.debug("Package name of %s unreadable: %s", app_id, exc)
""",
        "expect": ["test_an_unreadable_package_name_is_logged_as_text_only"],
    },
    {
        "name": "the-store-lookup-warning-holds-the-exception-object",
        "file": LOOKUP,
        "old": """        logger.warning("Microsoft Store lookup unavailable: %s", str(exc))
        return []
""",
        "new": """        logger.warning("Microsoft Store lookup unavailable: %s", exc)
        return []
""",
        "expect": ["test_a_failed_store_lookup_is_logged_as_text_only"],
    },
    {
        "name": "an-app-id-the-folder-returns-twice-is-listed-twice",
        "file": LOOKUP,
        "old": "        tiles.setdefault(app_id.casefold(), (app_id, tile_name))\n",
        "new": "        tiles.setdefault((app_id, len(tiles)), (app_id, tile_name))\n",
        "expect": ["test_a_repeated_app_id_is_read_once"],
    },
    {
        "name": "an-app-id-repeated-in-another-case-is-listed-twice",
        "file": LOOKUP,
        "old": "        tiles.setdefault(app_id.casefold(), (app_id, tile_name))\n",
        "new": "        tiles.setdefault(app_id, (app_id, tile_name))\n",
        "expect": ["test_a_repeated_app_id_is_read_once"],
    },
    {
        "name": "the-package-name-of-a-multi-app-package-is-read",
        "file": LOOKUP,
        "old": "        if count == 1:\n",
        "new": "        if True:\n",
        "expect": ["test_a_two_app_package_is_counted_and_its_name_is_not_read"],
    },
    {
        "name": "the-apps-of-a-package-are-not-counted-together",
        "file": LOOKUP,
        "old": "        per_package[family] = per_package.get(family, 0) + 1\n",
        "new": "        per_package[family] = 1\n",
        "expect": ["test_a_two_app_package_is_counted_and_its_name_is_not_read"],
    },
    {
        "name": "an-unreadable-package-name-ends-the-whole-read",
        "file": LOOKUP,
        "old": """            except Exception as exc:
                logger.debug("Package name of %s unreadable: %s", app_id, str(exc))
""",
        "new": """            except KeyError as exc:
                logger.debug("Package name of %s unreadable: %s", app_id, str(exc))
""",
        "expect": ["test_a_package_name_that_cannot_be_read_leaves_the_record_in"],
    },
    {
        # COM already initialised in another mode on this thread: the
        # reader goes on, and does not uninitialise what it never
        # initialised.
        "name": "a-com-initialisation-failure-escapes",
        "file": LOOKUP,
        "old": "    except pythoncom.com_error:\n",
        "new": "    except KeyError:\n",
        "expect": ["test_a_thread_whose_com_was_already_initialised_still_reads"],
    },
    {
        "name": "couninitialize-runs-without-a-successful-coinitialize",
        "file": LOOKUP,
        "old": """    if initialised:
        pythoncom.CoUninitialize()
    return records
""",
        "new": """    if True:
        pythoncom.CoUninitialize()
    return records
""",
        "expect": ["test_a_thread_whose_com_was_already_initialised_still_reads"],
    },
]


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            # pytest cuts a short-summary line to the terminal width, and a
            # captured run has no terminal: the reason after " - " would
            # be lost for a long node id.
            "COLUMNS": "1000",
        },
    )


def _clear_caches():
    """Delete cached bytecode for the two target modules.

    A mutation that leaves the file the same size and restores it fast
    enough can leave (mtime, size) unchanged, and the next run would then
    execute bytecode compiled from the other version.
    """
    for directory in (SERVICE / "__pycache__", SERVICE / "utils" / "__pycache__"):
        if not directory.is_dir():
            continue
        for stem in ("input_proc", "installed_programs"):
            for cached in directory.glob(stem + ".*.pyc"):
                try:
                    cached.unlink()
                except OSError:
                    pass


def _deselect(mut):
    """Drop the tests a mutation would make run until the suite timeout."""
    names = mut.get("deselect", ())
    if not names:
        return []
    return ["-k", " and ".join(f"not {name}" for name in names)]


def _node_name(node):
    """The bare test name of a node id, without its parameter list."""
    return node.split("::")[-1].strip().split("[")[0].split(" ")[0]


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(_node_name(line))
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def summary_lines(output):
    """The lines of pytest's short-summary section, and nothing else.

    A line that starts with ERROR or FAILED can also be a captured log
    record, so verdicts are read only from this section.
    """
    lines = []
    inside = False
    for line in output.splitlines():
        if line.startswith("=") and "short test summary info" in line:
            inside = True
            continue
        if inside and line.startswith("="):
            break
        if inside:
            lines.append(line)
    return lines


def failures(output):
    """{test name: [reason, ...]} from the short summary."""
    found = {}
    for line in summary_lines(output):
        if not line.startswith("FAILED "):
            continue
        head, _sep, reason = line[len("FAILED "):].partition(" - ")
        found.setdefault(_node_name(head), []).append(reason.strip())
    return found


def error_lines(output):
    return [line for line in summary_lines(output) if line.startswith("ERROR ")]


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


def _edits(mut):
    """A mutation's (old, new) pairs: one pair, or its own list."""
    if "edits" in mut:
        return mut["edits"]
    return [(mut["old"], mut["new"])]


def _translate(sources):
    """Rewrite every pattern into its target file's line ending."""
    for mut in MUTATIONS:
        newline = sources[mut["file"]]["newline"]
        mut["edits"] = [
            (old.replace("\n", newline), new.replace("\n", newline))
            for old, new in _edits(mut)
        ]


def _prepare(mut, sources):
    """Return (mutated_text, error). Exactly one match each, and it must compile."""
    text = sources[mut["file"]]["text"]
    for old, _new in mut["edits"]:
        count = text.count(old)
        if count != 1:
            return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text
    for old, new in mut["edits"]:
        mutated = mutated.replace(old, new, 1)
    if mutated == text:
        return None, f"{mut['name']}: the mutation changes nothing"
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


def _write(path, data):
    """Replace ``path`` with ``data`` through a temporary file, retrying.

    On Windows os.replace can fail with a PermissionError while another
    process (a language server, a virus scan, the previous pytest) holds
    the file for a moment.
    """
    tmp = path.with_name(path.name + f".mutation-gate.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    last = None
    try:
        for _ in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.25)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(path, original):
    """Put the original bytes back, prove it, and clear the bytecode caches.

    A restore that cannot be proven is printed loudly: an unreported
    mutant in a tracked file is the whole harm. Never raises for an OSError.
    """
    held = None
    for _ in range(2):
        try:
            _write(path, original)
            if path.read_bytes() == original:
                break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print("ERROR restore of", path, "failed:", exc)
    else:
        print("ERROR restore of", path, "could not be proven; check git diff")
    _clear_caches()
    if held is not None:
        raise held


def main():
    sources = _sources()
    _translate(sources)

    for path, source in sources.items():
        ending = "CRLF" if source["newline"] == "\r\n" else "LF"
        print(f"line endings in {path.name}: {ending}")

    if "--check" in sys.argv:
        return check_only(sources)

    errors, caught, survived = [], [], []

    _clear_caches()
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

    names = [m["name"] for m in MUTATIONS]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"mutation name {name!r} is used more than once")
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
        for name in mut.get("raises", {}):
            if name not in mut["expect"]:
                errors.append(f"{mut['name']}: raises names {name!r}, not expected")
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
        _clear_caches()
        try:
            _write(path, mutated.encode("utf-8"))
            result = _pytest(*TEST_FILES, *_deselect(mut), *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        except OSError as exc:
            errors.append(f"{mut['name']}: could not write the mutant: {exc}")
            print("ERROR", errors[-1])
            continue
        finally:
            # Written as bytes, never as text: on Windows write_text would
            # translate every newline and silently rewrite the file's
            # endings, breaking every multi-line pattern afterwards.
            _restore(path, original)
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
        # Exit code first: 0 means no test failed, so the mutation survived,
        # and a green run prints no short summary to read.
        if result.returncode == 0:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; no test failed")
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failures(result.stdout)
        if not got:
            errors.append(
                f"{mut['name']}: pytest failed but the summary names no test; "
                f"no verdict"
            )
            print("ERROR", errors[-1])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if missing:
            survived.append(mut["name"])
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed tests were {sorted(got)}"
            )
            continue
        raises = mut.get("raises", {})
        # Only the tests the mutation is expected to fail owe an assertion.
        # A test outside that list can crash on the mutant (a None name
        # reaching code that expects a string, say) without saying anything
        # about whether the expected tests reached their assertions.
        crashed = {}
        for test, reasons in got.items():
            if test not in mut["expect"]:
                continue
            for reason in reasons:
                if is_assertion_failure(reason):
                    continue
                if test in raises and reason.startswith(raises[test]):
                    continue
                crashed.setdefault(test, []).append(reason)
        # A test that must not raise (the raises field) is the only one
        # where an exception is the proof; make sure it did raise.
        for test, exc_name in raises.items():
            if not any(r.startswith(exc_name) for r in got.get(test, [])):
                crashed.setdefault(test, []).append(
                    f"expected {exc_name} from this test, got {got.get(test)}"
                )
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {crashed}"
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
