"""Mutation gate for wh-provider-native-crash-trace.

Stage 1 (services/stt_providers/shared): the supervisor starts main.py
with PYTHONFAULTHANDLER=1 and its stderr in a file, moves a large file
aside, keeps appending when it cannot, still starts the provider when
the file cannot be opened, and records each exit code.

Stage 2 (services/wheelhouse): WheelHouse copies the new lines of that
file into wheelhouse.log through an allow-list filter, from a per-
provider byte offset, and names a native crash in its notices.

Each mutation names its service directory, and LAUNCH starts pytest
with that service's own interpreter (never ``uv run``: in a worktree it
builds a .venv inside the tree, which makes the tree undeletable):

    shared        services/stt_providers/shared/.venv when present, else
                  services/stt_providers/google_stt_server/.venv (its
                  editable wheelhouse-shared install points at this tree)
    wheelhouse    services/wheelhouse/.venv of this tree when present,
                  else the main checkout's; pyproject's pythonpath makes
                  this tree's code the code under test either way

Run it from services/stt_providers/shared with any of those interpreters:

    .venv/Scripts/python.exe tests/mutation_gate_provider_crash_trace.py --check
    .venv/Scripts/python.exe tests/mutation_gate_provider_crash_trace.py

``--check`` answers the two offline questions -- does every pattern match
exactly once, and does every mutant still parse -- without running a test
or writing a file. It is NOT a sweep. Run the gate in full before the
freeze report.

The failure reasons are echoed under each verdict: a catch counts only
when the expected assertion fired, not an unrelated exception upstream
of it (mutation-gate skill).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate_runner as runner  # noqa: E402

SHARED = Path(__file__).resolve().parents[1]
PROVIDERS = SHARED.parent
GOOGLE = PROVIDERS / "google_stt_server"
ROOT = PROVIDERS.parents[1]
WHEELHOUSE = ROOT / "services" / "wheelhouse"

LAUNCHER = SHARED / "shared_stt" / "launcher.py"


def _main_checkout(root: Path) -> Path:
    """The main checkout's root, read from a worktree's .git link file."""
    link = root / ".git"
    if link.is_file():
        text = link.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            return Path(text[len("gitdir:"):].strip()).resolve().parents[2]
    return root


_VENV_PYTHON = Path(".venv") / "Scripts" / "python.exe"


def _first_present(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


INTERPRETERS = {
    SHARED: _first_present(SHARED / _VENV_PYTHON, GOOGLE / _VENV_PYTHON),
    WHEELHOUSE: _first_present(
        WHEELHOUSE / _VENV_PYTHON,
        _main_checkout(ROOT) / "services" / "wheelhouse" / _VENV_PYTHON),
}


def _launch(service, test_file, report, collect):
    """Start pytest with the service's own interpreter, never ``uv run``."""
    python = INTERPRETERS[service]
    if not python.is_file():
        # Raised as OSError so the runner reports an error, never a verdict.
        raise OSError(f"no interpreter for {service.name}: {python}")
    command = [
        str(python), "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        "-p", "no:cacheprovider",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

_plain_run_pytest = runner._run_pytest


def _run_pytest_showing_reasons(service, test_file):
    """The runner's _run_pytest, echoing each failure's reason line."""
    result = _plain_run_pytest(service, test_file)
    out = result.stdout + result.stderr
    summary = out.split("short test summary info", 1)
    if len(summary) == 2:
        for line in summary[1].splitlines():
            if line.startswith(("FAILED", "ERROR")):
                print(f"    reason: {line[:400]}")
    return result


runner._run_pytest = _run_pytest_showing_reasons


def _mutation(name, service, test_file, file, old, new, *expect):
    return {
        "name": name,
        "service": service,
        "test_file": test_file,
        "file": file,
        "old": old,
        "new": new,
        "expect": list(expect),
    }


# --------------------------------------------------------------------------
# Stage 1: shared_stt/launcher.py
# --------------------------------------------------------------------------

LAUNCHER_TESTS = "tests/test_launcher_crash_trace.py"

CRASH_DUMP = "test_the_crash_dump_and_the_exit_code_reach_the_file"
FH_SETTING = "test_the_child_gets_the_faulthandler_setting"
CANNOT_BLOCK = "test_a_child_that_writes_more_than_a_pipe_holds_finishes"
CLEAN_EXIT_LINE = "test_a_clean_exit_is_recorded_with_its_code"
EVERY_ATTEMPT = "test_every_attempt_gets_its_own_line"
PREVIOUS_KEPT = "test_a_previous_run_is_kept"
MOVED_ASIDE = "test_a_large_file_is_moved_aside_at_child_start"
AT_BOUND = "test_a_file_at_the_bound_is_not_moved"
CANNOT_MOVE = "test_a_file_that_cannot_be_moved_is_appended_to"
STILL_STARTS = "test_the_provider_still_starts"
PATH_NAME = "test_the_path_is_in_the_application_data_folder"


def _launcher(name, old, new, *expect):
    return _mutation(name, SHARED, LAUNCHER_TESTS, LAUNCHER, old, new, *expect)


LAUNCHER_MUTATIONS = [
    _launcher(
        "no-faulthandler-variable",
        '    child_env["PYTHONFAULTHANDLER"] = "1"\n',
        '    child_env.pop("PYTHONFAULTHANDLER", None)\n',
        CRASH_DUMP, FH_SETTING,
    ),
    # "0" still enables faulthandler (any non-empty value does), so only
    # the direct reading of the variable can catch this one.
    _launcher(
        "faulthandler-variable-wrong-value",
        '    child_env["PYTHONFAULTHANDLER"] = "1"\n',
        '    child_env["PYTHONFAULTHANDLER"] = "0"\n',
        FH_SETTING,
    ),
    _launcher(
        "child-environment-not-passed",
        "                env=child_env,\n",
        "                env=None,\n",
        CRASH_DUMP, FH_SETTING,
    ),
    _launcher(
        "stderr-not-redirected",
        "                stderr=stderr_log,\n",
        "                stderr=None,\n",
        CRASH_DUMP, FH_SETTING, CANNOT_BLOCK,
    ),
    # The hazard the file avoids: a pipe nobody reads. The child stops in
    # its write, the supervisor waits on it, and the test's 60 s bound
    # fires.
    _launcher(
        "stderr-is-an-unread-pipe",
        "                stderr=stderr_log,\n",
        "                stderr=subprocess.PIPE,\n",
        CANNOT_BLOCK,
    ),
    _launcher(
        "no-exit-line",
        "            _append_exit_line(stderr_log_path, config.app_name, exit_code,\n"
        "                              time.time() - start_time)\n",
        "            pass\n",
        CRASH_DUMP, CLEAN_EXIT_LINE, EVERY_ATTEMPT,
    ),
    _launcher(
        "exit-line-without-hex",
        '            f"(0x{exit_code:08X}) after {uptime:.1f}s\\n")\n',
        '            f"after {uptime:.1f}s\\n")\n',
        CRASH_DUMP, CLEAN_EXIT_LINE, EVERY_ATTEMPT,
    ),
    _launcher(
        "file-truncated-at-child-start",
        '    return open(path, "ab")\n',
        '    return open(path, "wb")\n',
        PREVIOUS_KEPT, CANNOT_MOVE,
    ),
    _launcher(
        "bound-includes-equal",
        "        if os.path.getsize(path) > STDERR_LOG_MAX_BYTES:\n",
        "        if os.path.getsize(path) >= STDERR_LOG_MAX_BYTES:\n",
        AT_BOUND,
    ),
    _launcher(
        "large-file-not-moved",
        '            os.replace(path, path + ".1")\n',
        "            pass\n",
        MOVED_ASIDE,
    ),
    _launcher(
        "refused-rename-not-contained",
        "    except OSError:\n"
        "        # Missing file, or a provider left behind by an earlier\n",
        "    except FileNotFoundError:\n"
        "        # Missing file, or a provider left behind by an earlier\n",
        CANNOT_MOVE,
    ),
    _launcher(
        "unopenable-file-stops-the-provider",
        "            except OSError as e:\n"
        "                # The provider still starts; its stderr stays on the console.\n",
        "            except FileNotFoundError as e:\n"
        "                # The provider still starts; its stderr stays on the console.\n",
        STILL_STARTS,
    ),
    _launcher(
        "path-not-lower-case",
        '    return os.path.join(app_data_path, f"{app_name.lower()}.stderr.log")\n',
        '    return os.path.join(app_data_path, f"{app_name}.stderr.log")\n',
        PATH_NAME,
    ),
]


# --------------------------------------------------------------------------
# Stage 2: services/wheelhouse -- stt/provider_stderr.py (the filter, the
# offset, the native-crash test) and its wiring in
# stt/remote_stt_launcher.py (the launch mark, the copy at every death
# site and at ready, and the native-crash notices).
# --------------------------------------------------------------------------

STDERR_MODULE = WHEELHOUSE / "stt" / "provider_stderr.py"
REMOTE_LAUNCHER = WHEELHOUSE / "stt" / "remote_stt_launcher.py"

STDERR_TESTS = "tests/test_provider_stderr.py"
# One selection for every launcher mutation, so its baseline runs once:
# the launch-generation class for the monitor, the late-death watch and
# the ready report, and the watchdog journey for the watchdog and for
# the launch mark (the only tests that go through start_provider).
WIRING_TESTS = (
    "tests/test_launch_generation.py::"
    "TestAProviderDeathCopiesItsStderrAndNamesANativeCrash",
    "tests/test_provider_watchdog.py",
)

# tests/test_provider_stderr.py
MESSAGE_HIDDEN = "test_a_python_traceback_keeps_its_frames_and_hides_the_message"
DOTTED_TYPE = "test_a_dotted_exception_type_is_kept"
OTHERS_COUNTED = "test_every_other_line_is_counted_not_copied"
LAST_PART = "test_only_the_last_part_of_a_large_addition_is_read"
COPIED_ONCE = "test_each_line_is_copied_once"
MOVED_ASIDE_READ = "test_a_file_moved_aside_is_read_from_the_start"
NEVER_LAUNCHED = "test_a_provider_never_launched_starts_at_the_current_end"
NOT_CRASHES = "test_other_codes_are_not"
CRASHES = "test_error_status_codes_are_native_crashes"
PARTIAL_LINE = "test_a_partial_last_line_waits_for_its_end"
MOVED_LINES_COPIED = "test_lines_written_before_a_move_aside_are_copied"
MOVED_NEW_FILE_LONGER = "test_a_move_is_seen_when_the_new_file_is_already_longer"
MOVED_NO_FILE_YET = "test_a_launch_with_no_file_yet_still_sees_the_move"
MOVED_NO_IDENTITIES = (
    "test_without_file_identities_a_shorter_file_still_reads_the_moved_part")
MOVED_OTHER_FILE = "test_a_moved_file_that_is_not_this_launchs_is_not_read"
MOVED_FIRST_START = "test_a_move_aside_at_the_first_start_copies_no_old_lines"
MOVED_NO_NEW_LINE = (
    "test_the_moved_aside_part_is_read_after_the_move_even_with_no_new_line")
MOVED_LAST_PART = "test_only_the_last_part_of_a_large_moved_aside_addition_is_read"
PASSING_STAT_FAILURE = "test_a_passing_stat_failure_does_not_replay_old_lines"
LOCKED_NEW_FILE = "test_a_locked_new_file_still_logs_the_moved_aside_lines"
MOVED_READ_LATER = "test_a_moved_file_that_cannot_be_read_now_is_read_later"
MOVED_BEFORE_NEW_FILE = (
    "test_a_move_seen_before_the_new_file_exists_copies_the_moved_lines")
MOVED_UNEXAMINED = "test_a_moved_file_that_cannot_be_examined_now_is_read_later"
ONE_LOOK = "test_one_look_gives_the_size_and_the_identity_together"
NEW_FILE_UNEXAMINED = (
    "test_a_new_file_that_cannot_be_examined_returns_no_earlier_exit_code")

# tests/test_launch_generation.py, the class in WIRING_TESTS
DEAD_CHILD_NATIVE = "test_a_native_crash_of_a_dead_child_names_the_code"
DEAD_CHILD_COPY = "test_the_dead_child_copies_its_stderr_into_the_log"
LATE_DEATH_NATIVE = "test_a_native_crash_after_the_monitor_gave_up_names_the_code"
LATE_DEATH_COPY = "test_the_late_death_copies_its_stderr_into_the_log"
DECLARED_COPY = "test_a_declared_failure_copies_its_stderr_into_the_log"
UNDECLARED_COPY = "test_an_undeclared_failure_copies_its_stderr_into_the_log"
READY_COPY = "test_a_ready_report_copies_the_new_stderr_lines"
ORPHAN_DECLARED_COPY = "test_a_failure_after_the_monitor_gave_up_copies_its_stderr"
ORPHAN_UNDECLARED_COPY = (
    "test_an_undeclared_failure_after_the_monitor_gave_up_copies_its_stderr")

# tests/test_provider_watchdog.py
WATCHDOG_RESTARTING = "test_a_native_crash_after_ready_says_the_engine_is_starting_again"
WATCHDOG_RETRY_USED = "test_a_native_crash_after_the_automatic_restart_says_it_stopped"
WATCHDOG_RESTART_FAILED = (
    "test_a_restart_that_cannot_begin_after_a_native_crash_says_it_stopped")
WATCHDOG_COPY = "test_the_watchdog_copies_the_stderr_lines_before_its_restart"
LAUNCH_MARK = "test_only_lines_written_after_the_launch_are_copied"
JOURNEY_READY_COPY = "test_a_ready_report_copies_the_lines_since_the_launch"


def _stderr(name, old, new, *expect):
    return _mutation(name, WHEELHOUSE, STDERR_TESTS, STDERR_MODULE,
                     old, new, *expect)


def _wiring(name, old, new, *expect):
    return _mutation(name, WHEELHOUSE, WIRING_TESTS, REMOTE_LAUNCHER,
                     old, new, *expect)


STDERR_MUTATIONS = [
    _stderr(
        "exception-message-not-redacted",
        '                f"{exception.group(1)}: {redact_transcript(exception.group(2))}"\n',
        '                f"{exception.group(1)}: {exception.group(2)}"\n',
        MESSAGE_HIDDEN, DOTTED_TYPE,
    ),
    # An empty pattern matches every line, so nothing is counted and the
    # exception line is copied whole before the redaction is reached.
    _stderr(
        "allow-list-accepts-everything",
        '    r"^Extension modules: ",\n',
        '    r"",\n',
        OTHERS_COUNTED, MESSAGE_HIDDEN,
    ),
    _stderr(
        "no-16kb-cap",
        "    start = max(offset, size - MAX_COPY_BYTES)\n",
        "    start = offset\n",
        LAST_PART, MOVED_LAST_PART,
    ),
    _stderr(
        "offset-not-advanced",
        "        next_offset = start + end + 1\n",
        "        next_offset = offset\n",
        COPIED_ONCE,
    ),
    _stderr(
        "moved-file-not-read-from-start",
        "                offset = 0\n"
        "                self._offsets[provider_name] = 0\n",
        "                pass\n",
        MOVED_ASIDE_READ,
    ),
    # A provider this WheelHouse never launched would replay an earlier
    # run's crash from byte 0.
    _stderr(
        "unlaunched-provider-read-from-start",
        "            if offset is None:\n"
        "                # Not launched by this WheelHouse: start at the current end.\n"
        "                self._offsets[provider_name] = size or 0\n"
        "                self._file_ids[provider_name] = current_id\n"
        "                return None\n",
        "            if offset is None:\n"
        "                offset = 0\n",
        NEVER_LAUNCHED,
    ),
    # wh-provider-native-crash-trace.2.1: the lines a launch wrote before
    # the supervisor moved its file aside.
    _stderr(
        "moved-part-not-read",
        "                if ours and moved_size > offset:\n",
        "                if False:\n",
        MOVED_LINES_COPIED, MOVED_NO_NEW_LINE, MOVED_LAST_PART,
    ),
    # Read from byte 0, the moved file repeats the earlier run's lines
    # ahead of this launch's. (At a first child start the size check
    # above already stops the read, so MOVED_FIRST_START cannot see this.)
    _stderr(
        "moved-part-read-from-its-start",
        "                        moved, offset, moved_size,\n",
        "                        moved, 0, moved_size,\n",
        MOVED_LINES_COPIED, MOVED_NEW_FILE_LONGER,
    ),
    _stderr(
        "moved-part-read-at-a-first-start",
        "                if ours and moved_size > offset:\n"
        "                    data, skipped, _ = _read_new(\n"
        "                        moved, offset, moved_size,\n",
        "                if ours and moved_size > 0:\n"
        "                    data, skipped, _ = _read_new(\n"
        "                        moved, 0, moved_size,\n",
        MOVED_FIRST_START,
    ),
    _stderr(
        "move-judged-by-size-only",
        "                moved_aside = current_id != known_id\n",
        "                moved_aside = size < offset\n",
        MOVED_NEW_FILE_LONGER, MOVED_NO_FILE_YET,
    ),
    _stderr(
        "no-size-fallback-without-identities",
        "                moved_aside = size < offset\n",
        "                moved_aside = False\n",
        MOVED_NO_IDENTITIES,
    ),
    _stderr(
        "moved-file-identity-not-checked",
        "                    ours = known_id is None or moved_id == known_id\n",
        "                    ours = True\n",
        MOVED_OTHER_FILE,
    ),
    _stderr(
        "mark-does-not-create-the-file",
        '            open(path, "ab").close()\n',
        "            pass\n",
        MOVED_NO_FILE_YET,
    ),
    # The next copy would see the same move again and repeat the .1 lines.
    _stderr(
        "identity-not-updated-after-a-move",
        "                self._offsets[provider_name] = 0\n"
        "            self._file_ids[provider_name] = current_id\n",
        "                self._offsets[provider_name] = 0\n"
        "            pass\n",
        MOVED_NO_NEW_LINE,
    ),
    # wh-provider-native-crash-trace.2.2: a passing failure to examine or
    # read a file neither replays old lines nor loses the .1 part.
    _stderr(
        "passing-stat-failure-treated-as-a-move",
        "                size, current_id = None, None\n"
        "            except OSError:\n"
        "                return None\n",
        "                size, current_id = None, None\n"
        "            except OSError:\n"
        "                size, current_id = 0, None\n",
        PASSING_STAT_FAILURE, NEW_FILE_UNEXAMINED,
    ),
    _stderr(
        "move-before-the-new-file-not-seen",
        "                if path_missing and not ours:\n",
        "                if path_missing:\n",
        MOVED_BEFORE_NEW_FILE,
    ),
    _stderr(
        "new-file-read-failure-not-contained",
        "                except OSError as exc:\n",
        "                except ValueError as exc:\n",
        LOCKED_NEW_FILE,
    ),
    _stderr(
        "position-advanced-after-a-failed-new-file-read",
        "                    logger.debug(f\"Could not read {provider_name} stderr: {exc}\")\n",
        "                    logger.debug(f\"Could not read {provider_name} stderr: {exc}\")\n"
        "                    self._offsets[provider_name] = size\n",
        LOCKED_NEW_FILE,
    ),
    _stderr(
        "failed-moved-read-marks-it-read",
        "                    data, skipped, _ = _read_new(\n"
        "                        moved, offset, moved_size,\n"
        "                        complete_lines_only=False)\n"
        "                    parts.append((data, skipped))\n",
        "                    try:\n"
        "                        data, skipped, _ = _read_new(\n"
        "                            moved, offset, moved_size,\n"
        "                            complete_lines_only=False)\n"
        "                        parts.append((data, skipped))\n"
        "                    except OSError:\n"
        "                        pass\n",
        MOVED_READ_LATER,
    ),
    # wh-provider-native-crash-trace.2.3: a .1 file that cannot be
    # examined now is examined again, and one look gives the size and the
    # identity together.
    _stderr(
        "moved-examine-failure-skips-it",
        "                except OSError:\n"
        "                    return None\n"
        "                else:\n",
        "                except OSError:\n"
        "                    moved_size, moved_id = 0, None\n"
        "                    ours = False\n"
        "                else:\n",
        MOVED_UNEXAMINED,
    ),
    _stderr(
        "size-and-identity-looked-up-apart",
        "                size, current_id = _examine(path)\n",
        "                size = os.stat(path).st_size\n"
        "                try:\n"
        "                    current_id = _file_id(os.stat(path))\n"
        "                except OSError:\n"
        "                    current_id = None\n",
        ONE_LOOK,
    ),
    # wh-provider-native-crash-trace.2.4: the exit code in .1 is an earlier
    # child's when the file the provider writes now could not be read.
    _stderr(
        "earlier-exit-code-returned-after-a-failed-read",
        "        if active_unread:\n"
        "            return None\n",
        "        if False:\n"
        "            return None\n",
        LOCKED_NEW_FILE,
    ),
    _stderr(
        "ctrl-c-counted-as-crash",
        "    return code >= 0xC0000000 and code != _CTRL_C_EXIT\n",
        "    return code >= 0xC0000000\n",
        NOT_CRASHES,
    ),
    _stderr(
        "lowest-error-status-excluded",
        "    return code >= 0xC0000000 and code != _CTRL_C_EXIT\n",
        "    return code > 0xC0000000 and code != _CTRL_C_EXIT\n",
        CRASHES,
    ),
    # The read position moves past a line that has no end yet, so its
    # first half is never copied.
    _stderr(
        "partial-line-not-held",
        '            return b"", 0, offset\n',
        '            return b"", 0, size\n',
        PARTIAL_LINE,
    ),
]


WIRING_MUTATIONS = [
    _wiring(
        "launch-not-marked",
        "            self._stderr_tail.mark_launch(provider_name)\n",
        "            pass\n",
        LAUNCH_MARK, JOURNEY_READY_COPY,
    ),
    _wiring(
        "no-copy-at-declared-failure",
        '                    f"Provider {provider_name} reported a failed startup"\n'
        "                )\n"
        "                self._stderr_tail.copy_new(provider_name)\n",
        '                    f"Provider {provider_name} reported a failed startup"\n'
        "                )\n",
        DECLARED_COPY,
    ),
    _wiring(
        "no-copy-at-undeclared-failure",
        '                f"that already said it failed (wh-launch-generation.2.3)."\n'
        "            )\n"
        "            self._stderr_tail.copy_new(provider_name)\n",
        '                f"that already said it failed (wh-launch-generation.2.3)."\n'
        "            )\n",
        UNDECLARED_COPY,
    ),
    # wh-provider-native-crash-trace.1.1: a failure that arrives after
    # the monitor gave up is reported by the signal itself.
    _wiring(
        "no-copy-at-orphaned-declared-failure",
        "            self._stderr_tail.copy_new(name)\n",
        "            pass\n",
        ORPHAN_DECLARED_COPY,
    ),
    _wiring(
        "no-copy-at-orphaned-undeclared-failure",
        "        self._stderr_tail.copy_new(orphan)\n",
        "        pass\n",
        ORPHAN_UNDECLARED_COPY,
    ),
    _wiring(
        "no-copy-at-dead-child",
        "            exit_code = self._stderr_tail.copy_new(provider_name)\n"
        "            failure_text = ",
        "            exit_code = None\n"
        "            failure_text = ",
        DEAD_CHILD_COPY, DEAD_CHILD_NATIVE,
    ),
    _wiring(
        "no-copy-at-late-death",
        "        exit_code = self._stderr_tail.copy_new(provider_name)\n"
        "        failure_text = ",
        "        exit_code = None\n"
        "        failure_text = ",
        LATE_DEATH_COPY, LATE_DEATH_NATIVE,
    ),
    _wiring(
        "no-copy-at-watchdog-death",
        "        exit_code = self._stderr_tail.copy_new(provider_name)\n"
        "        native_crash = ",
        "        exit_code = None\n"
        "        native_crash = ",
        WATCHDOG_COPY, WATCHDOG_RESTARTING,
    ),
    _wiring(
        "no-copy-at-ready",
        "        if provider_name is not None:\n"
        "            self._stderr_tail.copy_new(provider_name)\n",
        "        if provider_name is not None:\n"
        "            pass\n",
        READY_COPY, JOURNEY_READY_COPY,
    ),
    _wiring(
        "dead-child-native-branch-off",
        '            failure_text = "Failed to start - try restarting Wheelhouse"\n'
        "            if is_native_crash(exit_code):\n",
        '            failure_text = "Failed to start - try restarting Wheelhouse"\n'
        "            if False:\n",
        DEAD_CHILD_NATIVE,
    ),
    _wiring(
        "late-death-native-branch-off",
        '        failure_text = "Failed to start - try restarting Wheelhouse"\n'
        "        if is_native_crash(exit_code):\n",
        '        failure_text = "Failed to start - try restarting Wheelhouse"\n'
        "        if False:\n",
        LATE_DEATH_NATIVE,
    ),
    _wiring(
        "watchdog-native-branch-off",
        "        native_crash = is_native_crash(exit_code)\n",
        "        native_crash = False\n",
        WATCHDOG_RESTARTING, WATCHDOG_RETRY_USED, WATCHDOG_RESTART_FAILED,
    ),
    _wiring(
        "watchdog-restart-notice-says-stopped",
        "                native_crash_message(title, exit_code, restarting=True),\n",
        "                native_crash_message(title, exit_code, restarting=False),\n",
        WATCHDOG_RESTARTING, WATCHDOG_RESTART_FAILED,
    ),
    # After the restart the notice names a launch that has been
    # replaced, and _notify drops it.
    _wiring(
        "watchdog-restart-notice-after-restart",
        "        if native_crash:\n"
        "            # Before the restart: the notice names this launch, and the\n"
        "            # restart replaces it, after which the notice is dropped.\n"
        "            self._notify(\n"
        "                title,\n"
        "                native_crash_message(title, exit_code, restarting=True),\n"
        "                generation, owner=provider_name,\n"
        "            )\n"
        "        record_refused = False\n"
        "\n"
        "        def record_restart(new_generation):\n"
        "            nonlocal record_refused\n"
        "            accepted = on_restarting(new_generation)\n"
        "            record_refused = not accepted\n"
        "            return accepted\n"
        "\n"
        "        started = self.start_provider(\n"
        "            provider_name, _watchdog_generation=generation,\n"
        "            _on_restarting=record_restart,\n"
        "        )\n",
        "        record_refused = False\n"
        "\n"
        "        def record_restart(new_generation):\n"
        "            nonlocal record_refused\n"
        "            accepted = on_restarting(new_generation)\n"
        "            record_refused = not accepted\n"
        "            return accepted\n"
        "\n"
        "        started = self.start_provider(\n"
        "            provider_name, _watchdog_generation=generation,\n"
        "            _on_restarting=record_restart,\n"
        "        )\n"
        "        if native_crash:\n"
        "            self._notify(\n"
        "                title,\n"
        "                native_crash_message(title, exit_code, restarting=True),\n"
        "                generation, owner=provider_name,\n"
        "            )\n",
        WATCHDOG_RESTARTING,
    ),
]


MUTATIONS = LAUNCHER_MUTATIONS + STDERR_MUTATIONS + WIRING_MUTATIONS


if __name__ == "__main__":
    sys.exit(runner.run(MUTATIONS))
