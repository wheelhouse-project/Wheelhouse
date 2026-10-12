"""Mutation gate for the background update check (wh-release-update-check).

services/wheelhouse/update_check.py decides three things the user can see or
that touch the network: whether a request is made at all, whether the notice
for a release is shown, and when the next check runs. This gate breaks each
of those decisions in turn and requires named tests in
tests/test_update_check.py to fail on their own assertion. A mutation that
nothing catches is a finding against the tests, not against the gate.

Since wh-blocked-mic-message.1.2 the shown-notice record is written by the
Logic process when the GUI process reports that the notice is visible, so
this gate also mutates the two halves of that report: the GUI side in
gui.py (_report_update_notice_shown and its two call sites in
_stack_update_notice) and the Logic side in main.py
(_handle_update_notice_shown and its command-map entry). Those mutations
run tests/test_update_notice.py.

Every mutation changes a VALUE a guard reads, or the body a guard protects,
and none changes how a loop advances, so no mutant can hang the suite.

HOW THE RUNNER IS BUILT
-----------------------
The pattern checks, exactly-one-match rule, compile of every mutant, byte-exact
restore, interrupt handling, expected-name validation, green baseline and
scope line all live in the shared runner
(services/stt_providers/shared/tests/mutation_gate_runner.py). This gate
replaces only the layer that starts pytest, as
mutation_gate_itn_percent_split_timing.py does: the shared launcher goes
through scripts/run_tests.py and ``uv run``, and a worktree must not build a
.venv through it. The replaced layer sets PYTHONDONTWRITEBYTECODE=1 and
COLUMNS=1000, reads verdicts only from the short-summary section, treats a
pytest ERROR as no verdict, and prints each failing test's reason.

Usage (from services/wheelhouse):
    uv run python tests/mutation_gate_update_check.py --check
    uv run python tests/mutation_gate_update_check.py
    uv run python tests/mutation_gate_update_check.py the-notice-record
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

WORKTREE = Path(__file__).resolve().parents[3]
WHEELHOUSE = WORKTREE / "services/wheelhouse"

spec = importlib.util.spec_from_file_location(
    "update_check_shared_gate",
    WORKTREE / "services/stt_providers/shared/tests/mutation_gate_runner.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# ---------------------------------------------------------------------------
# The launch layer
# ---------------------------------------------------------------------------

def _main_checkout() -> Path:
    """The checkout that owns the virtual environments."""
    git = WORKTREE / ".git"
    if git.is_dir():
        return WORKTREE
    gitdir = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
    return gitdir.parents[2]


MAIN = _main_checkout()


def _interpreter(service: Path) -> Path:
    """The python.exe that owns a service's dependencies."""
    local = service / ".venv/Scripts/python.exe"
    if local.exists():
        return local
    return MAIN / service.relative_to(WORKTREE) / ".venv/Scripts/python.exe"


RUN_TIMEOUT = 180
_SUMMARY_HEADER = "short test summary info"


def _bare_name(text: str) -> str:
    """A collected or reported test id, without its path and parameter list.

    The name is read as the identifiers after the first "::", so a parameter
    id that holds a space (pytest builds one from a bytes value, for example
    "[this is = = not toml]") cannot cut the name short, and a failure reason
    that holds "::" cannot move it.
    """
    node = text.split("::", 1)[1]
    return re.match(r"\w+(?:::\w+)*", node).group(0).split("::")[-1]


def _summary(output: str) -> str:
    return output.split(_SUMMARY_HEADER, 1)[1] if _SUMMARY_HEADER in output else ""


def failed_names(output: str) -> set[str]:
    """The tests pytest's short summary reports as FAILED, and only those."""
    return {
        _bare_name(line[len("FAILED "):])
        for line in _summary(output).splitlines()
        if line.startswith("FAILED ")
    }


def run_tests(service: Path, test_file, collect: bool = False):
    # COLUMNS: pytest cuts a short-summary line to the terminal width, and at
    # the 80-column default these ids leave no room for the reason.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")
    command = [
        str(_interpreter(service)), "-m", "pytest",
        *runner._targets(test_file),
        "-p", "no:randomly",
    ]
    command += ["--collect-only", "-q"] if collect else ["-rf", "--tb=short"]
    result = subprocess.run(
        command, cwd=str(service), env=env,
        capture_output=True, text=True, timeout=RUN_TIMEOUT,
    )
    output = result.stdout + result.stderr
    if collect:
        if result.returncode:
            raise RuntimeError("collection failed: " + output[-2000:])
        return result
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"pytest exited {result.returncode}: " + output[-2000:])
    errored = [line for line in _summary(output).splitlines()
               if line.startswith("ERROR ")]
    if errored:
        raise RuntimeError("pytest reported an ERROR, which is no verdict: "
                           + "; ".join(errored[:3]))
    # The reason of each failure is the only thing that tells a mutant that
    # reached the assertion from one that crashed upstream of it.
    for line in _summary(output).splitlines():
        if line.startswith("FAILED "):
            print("  " + line[len("FAILED "):][:600], flush=True)
    return result


def collected_names(service: Path, test_file) -> set[str]:
    result = run_tests(service, test_file, collect=True)
    return {_bare_name(line) for line in result.stdout.splitlines()
            if "::" in line}


def clear_pycache(service: Path) -> None:
    skip = {".venv", ".git", "node_modules", ".pytest_cache", "vendor"}
    for parent, directories, _ in os.walk(service):
        directories[:] = [d for d in directories if d not in skip]
        if "__pycache__" in directories:
            directories.remove("__pycache__")
            shutil.rmtree(Path(parent) / "__pycache__", ignore_errors=True)


runner._run_pytest = run_tests
runner._collected_names = collected_names
runner._failed_names = failed_names
runner._clear_pycache = clear_pycache


# ---------------------------------------------------------------------------
# The mutations
# ---------------------------------------------------------------------------

TARGET = WHEELHOUSE / "update_check.py"
SUITE = "tests/test_update_check.py"
GUI = WHEELHOUSE / "gui.py"
# Not ``MAIN``: that name holds the main checkout directory above, and
# ``_interpreter`` reads it at call time (wh-blocked-mic-message.1.2).
MAIN_PY = WHEELHOUSE / "main.py"
NOTICE_SUITE = "tests/test_update_notice.py"


def mutation(name, old, new, expect):
    return dict(name=name, service=WHEELHOUSE, test_file=SUITE, file=TARGET,
                old=old, new=new, expect=list(expect))


def mutation_in(file, name, old, new, expect):
    """A mutation of gui.py or main.py, guarded by test_update_notice.py."""
    return dict(name=name, service=WHEELHOUSE, test_file=NOTICE_SUITE,
                file=file, old=old, new=new, expect=list(expect))


# The runner hands the notice to notify and writes no record itself
# (wh-blocked-mic-message.1.2).
NOTIFY_CALL = (
    "                outcome = notify(*found)\n"
    "                if inspect.isawaitable(outcome):\n"
    "                    await outcome\n"
)
RECORD_CALL = (
    "                record_notice_shown(state_path, found[0], clock())\n"
)

MUTATIONS = [
    # --- notice_is_due -----------------------------------------------------
    mutation(
        "notice-is-due-final-return-is-always-false",
        "    return not (0 <= now - state.notified_at < NOTICE_DEFER_SECONDS)\n",
        "    return False\n",
        ["test_notice_due_again_for_same_version_after_seven_days",
         "test_notice_due_again_when_the_recorded_time_is_in_the_future"]),
    mutation(
        "notice-is-due-final-return-is-always-true",
        "    return not (0 <= now - state.notified_at < NOTICE_DEFER_SECONDS)\n",
        "    return True\n",
        ["test_notice_not_due_again_for_same_version_inside_seven_days",
         "test_notice_not_due_for_an_older_version_inside_seven_days",
         "test_runner_notice_appears_once_per_version_and_again_after_7_days"]),
    mutation(
        "notice-is-due-ignores-the-version-comparison",
        "    if is_newer_version(latest_version, state.notified_version):\n",
        "    if False:\n",
        ["test_notice_due_at_once_for_a_version_newer_than_the_deferred_one",
         "test_runner_newer_version_shows_at_the_next_check_inside_the_wait"]),
    mutation(
        "notice-is-due-unreadable-record-version-defers",
        "    if parse_version(state.notified_version) is None:\n"
        "        return True\n",
        "    if parse_version(state.notified_version) is None:\n"
        "        return False\n",
        ["test_notice_due_when_recorded_version_is_unreadable"]),
    mutation(
        "notice-is-due-a-future-record-time-holds-the-wait",
        "    return not (0 <= now - state.notified_at < NOTICE_DEFER_SECONDS)\n",
        "    return not (now - state.notified_at < NOTICE_DEFER_SECONDS)\n",
        ["test_notice_due_again_when_the_recorded_time_is_in_the_future"]),
    mutation(
        "the-defer-period-is-three-days",
        "NOTICE_DEFER_SECONDS = 7 * 24 * 60 * 60\n",
        "NOTICE_DEFER_SECONDS = 3 * 24 * 60 * 60\n",
        ["test_schedule_constants_match_the_acceptance_field",
         "test_notice_not_due_again_for_same_version_inside_seven_days"]),
    # --- the runner writes no record (wh-blocked-mic-message.1.2) ----------
    # Both mutants put the old write back, in the old place and after the
    # notify call. The record then names a version whose notice may still
    # wait for a place on the screen.
    mutation(
        "the-runner-records-the-notice-before-notify",
        NOTIFY_CALL,
        RECORD_CALL + NOTIFY_CALL,
        ["test_runner_leaves_the_notice_fields_alone_when_it_calls_notify",
         "test_runner_offers_the_version_again_at_every_check_until_a_report_arrives"]),
    mutation(
        "the-runner-records-the-notice-after-notify",
        NOTIFY_CALL,
        NOTIFY_CALL + RECORD_CALL,
        ["test_runner_leaves_the_notice_fields_alone_when_it_calls_notify",
         "test_runner_offers_the_version_again_at_every_check_until_a_report_arrives"]),
    mutation(
        "record-notice-shown-writes-no-time",
        "        path, replace(load_state(path), notified_version=version, notified_at=now)\n",
        "        path, replace(load_state(path), notified_version=version, notified_at=0.0)\n",
        ["test_runner_notice_appears_once_per_version_and_again_after_7_days"]),
    mutation(
        "record-notice-shown-writes-no-version",
        "        path, replace(load_state(path), notified_version=version, notified_at=now)\n",
        '        path, replace(load_state(path), notified_version="", notified_at=now)\n',
        ["test_runner_notice_appears_once_per_version_and_again_after_7_days",
         "test_runner_newer_version_shows_at_the_next_check_inside_the_wait"]),
    mutation(
        "an-async-notify-is-not-awaited",
        "                if inspect.isawaitable(outcome):\n",
        "                if False:\n",
        ["test_runner_accepts_an_async_callback"]),
    mutation(
        "the-check-runs-on-the-event-loop-thread",
        "            found = await asyncio.to_thread(\n"
        "                check_for_update,\n"
        "                config_service,\n"
        "                state_path=state_path,\n"
        "                get_version=get_version,\n"
        "                fetch=fetch,\n"
        "                clock=clock,\n"
        "            )\n",
        "            found = check_for_update(\n"
        "                config_service,\n"
        "                state_path=state_path,\n"
        "                get_version=get_version,\n"
        "                fetch=fetch,\n"
        "                clock=clock,\n"
        "            )\n",
        ["test_runner_does_not_block_the_event_loop_during_the_request"]),
    # --- the config key -----------------------------------------------------
    mutation(
        "the-config-key-is-never-read",
        "    if not config_service.get(UPDATE_CHECK_SETTING, UPDATE_CHECK_DEFAULT):\n",
        "    if not UPDATE_CHECK_DEFAULT:\n",
        ["test_runner_key_off_makes_no_request_in_any_cycle",
         "test_config_without_the_key_behaves_as_on"]),
    mutation(
        "the-code-default-of-the-config-key-is-false",
        "UPDATE_CHECK_DEFAULT = True\n",
        "UPDATE_CHECK_DEFAULT = False\n",
        ["test_schedule_constants_match_the_acceptance_field",
         "test_config_without_the_key_behaves_as_on"]),
    # --- versions -----------------------------------------------------------
    mutation(
        "the-version-comparison-is-reversed",
        "    return new > old\n",
        "    return new < old\n",
        ["test_is_newer_version_true_when_candidate_is_newer",
         "test_is_newer_version_false_when_older"]),
    mutation(
        "parse-version-keeps-the-leading-v",
        "        text = text[1:]\n",
        "        text = text\n",
        ["test_parse_version_strips_one_leading_v_and_reads_up_to_four_parts",
         "test_is_newer_version_true_when_candidate_is_newer"]),
    mutation(
        "the-version-part-limit-is-five",
        "MAX_VERSION_PARTS = 4\n",
        "MAX_VERSION_PARTS = 5\n",
        ["test_malformed_version_is_never_newer_and_never_parses"]),
    mutation(
        "a-malformed-version-counts-as-newer",
        "    if new is None or old is None:\n"
        "        return False\n",
        "    if new is None or old is None:\n"
        "        return True\n",
        ["test_malformed_version_is_never_newer_and_never_parses",
         "test_check_shows_nothing_for_equal_older_or_malformed_tags"]),
    mutation(
        "the-check-compares-the-running-version-to-the-tag",
        "    if not is_newer_version(tag, running):\n",
        "    if not is_newer_version(running, tag):\n",
        ["test_check_returns_version_and_tag_for_a_newer_release"]),
    mutation(
        "the-notice-version-keeps-the-leading-v",
        '    return (tag[1:] if tag.startswith("v") else tag, tag)\n',
        "    return (tag, tag)\n",
        ["test_check_returns_version_and_tag_for_a_newer_release"]),
    mutation(
        "the-unknown-running-version-is-no-longer-skipped",
        "    if parse_version(running) is None:\n",
        "    if running is None:\n",
        ["test_unknown_running_version_logs_one_line_and_skips"]),
    mutation(
        "the-skipped-unknown-version-logs-nothing",
        '        logger.info("update_check: skipped, running version is %r", running)\n',
        "        pass\n",
        ["test_unknown_running_version_logs_one_line_and_skips"]),
    # --- the schedule -------------------------------------------------------
    mutation(
        "the-runner-checks-every-sixty-seconds",
        "        delay = CHECK_INTERVAL_SECONDS\n",
        "        delay = FIRST_CHECK_DELAY_SECONDS\n",
        ["test_runner_first_check_waits_60_seconds_then_every_24_hours",
         "test_runner_survives_any_exception_from_the_check"]),
    mutation(
        "a-recent-check-does-not-postpone-the-first-check",
        "    until_due = last_check + CHECK_INTERVAL_SECONDS - now\n",
        "    until_due = 0\n",
        ["test_first_check_waits_for_the_24_hour_mark_after_a_recent_check",
         "test_runner_after_a_recent_check_waits_for_the_24_hour_mark"]),
    mutation(
        "the-first-check-wait-has-no-24-hour-cap",
        "    return min(CHECK_INTERVAL_SECONDS, max(FIRST_CHECK_DELAY_SECONDS, until_due))\n",
        "    return max(FIRST_CHECK_DELAY_SECONDS, until_due)\n",
        ["test_first_check_never_waits_longer_than_24_hours_if_clock_moved_back"]),
    mutation(
        "the-first-check-delay-is-skipped-when-never-checked",
        "        return FIRST_CHECK_DELAY_SECONDS  # never checked\n",
        "        return 0.0  # never checked\n",
        ["test_first_check_waits_sixty_seconds_when_never_checked",
         "test_runner_first_check_waits_60_seconds_then_every_24_hours"]),
    # SURVIVOR at f2aa2c6f (gate record, wh-release-update-check item 11).
    # test_first_check_waits_sixty_seconds_when_last_check_is_old passes
    # last_check = 5000.0 - 3 * DAY, which is NEGATIVE, so
    # seconds_until_first_check returns through its `last_check <= 0`
    # ("never checked") branch and never reaches the max(60, until_due)
    # floor. No test gives a positive last_check whose 24-hour mark is less
    # than 60 seconds away or already past. The mutant is real, not
    # equivalent: with last_check = 100000.0 - DAY + 30 and now = 100000.0
    # the shipped code returns 60 and the mutant returns 30. The repair is
    # a test input (a now large enough that now - 3 * DAY is positive), not
    # a change to this entry.
    mutation(
        "the-first-check-delay-has-no-sixty-second-floor",
        "max(FIRST_CHECK_DELAY_SECONDS, until_due)",
        "max(0, until_due)",
        ["test_first_check_waits_sixty_seconds_when_last_check_is_old"]),
    mutation(
        "a-try-is-not-recorded-as-the-last-check",
        "    if not save_state(state_path, replace(load_state(state_path), last_check=now)):\n",
        "    if not save_state(state_path, replace(load_state(state_path), last_check=0.0)):\n",
        ["test_network_failure_shows_nothing_logs_one_line_and_still_counts_as_a_try",
         "test_runner_leaves_the_notice_fields_alone_when_it_calls_notify"]),
    # Fail closed: a refused last_check write must stop the request. The
    # mutant keeps the write and drops the refusal.
    mutation(
        "a-failed-last-check-write-no-longer-stops-the-request",
        "    if not save_state(state_path, replace(load_state(state_path), last_check=now)):\n"
        "        logger.warning(\n"
        '            "update_check: could not record the check time, so no request "\n'
        '            "was made; will retry at the next check"\n'
        "        )\n"
        "        return None\n",
        "    save_state(state_path, replace(load_state(state_path), last_check=now))\n",
        ["test_failed_last_check_write_makes_no_release_request",
         "test_failed_last_check_write_makes_no_request_when_a_directory_holds_the_record_path",
         "test_failed_last_check_write_logs_exactly_one_line",
         "test_runner_waits_the_ordinary_24_hours_after_a_failed_last_check_write"]),
    # --- the request ----------------------------------------------------------
    mutation(
        "the-user-agent-header-is-dropped",
        '        headers={"User-Agent": USER_AGENT_PREFIX + running_version},\n',
        "        headers={},\n",
        ["test_fetch_sends_one_get_with_only_a_wheelhouse_user_agent",
         "test_request_carries_no_config_value_or_identifier"]),
    mutation(
        "the-user-agent-drops-the-version",
        '        headers={"User-Agent": USER_AGENT_PREFIX + running_version},\n',
        '        headers={"User-Agent": USER_AGENT_PREFIX},\n',
        ["test_fetch_sends_one_get_with_only_a_wheelhouse_user_agent"]),
    mutation(
        "the-request-timeout-argument-is-dropped",
        "        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:\n",
        "        with urllib.request.urlopen(request) as response:\n",
        ["test_fetch_sends_one_get_with_only_a_wheelhouse_user_agent"]),
    mutation(
        "the-request-timeout-is-sixty-seconds",
        "REQUEST_TIMEOUT_SECONDS = 15\n",
        "REQUEST_TIMEOUT_SECONDS = 60\n",
        ["test_schedule_constants_match_the_acceptance_field",
         "test_fetch_sends_one_get_with_only_a_wheelhouse_user_agent"]),
    mutation(
        "a-failed-request-logs-nothing",
        '        logger.warning("update_check: no result, will retry at the next check: %s", exc)\n',
        "        pass\n",
        ["test_network_failure_shows_nothing_logs_one_line_and_still_counts_as_a_try"]),
    # --- the record file ---------------------------------------------------
    mutation(
        "save-state-writes-straight-to-the-target",
        "            with tempfile.NamedTemporaryFile(\n"
        '                mode="wb",\n'
        "                delete=False,\n"
        "                dir=path.parent,\n"
        '                prefix=path.name + ".",\n'
        '                suffix=".tmp",\n'
        "            ) as tmp:\n"
        "                temp_path = tmp.name\n"
        "                tmp.write(payload)\n"
        "                tmp.flush()\n"
        "                os.fsync(tmp.fileno())\n"
        "            os.replace(temp_path, path)\n"
        "            return True\n",
        "            path.write_bytes(payload)\n"
        "            return True\n",
        ["test_save_state_failure_returns_false_and_leaves_old_file"]),
    mutation(
        "a-failed-save-leaves-its-temporary-file",
        "                    os.unlink(temp_path)\n",
        "                    pass\n",
        ["test_save_state_failure_returns_false_and_leaves_old_file"]),
    mutation(
        "save-state-does-not-create-the-directory",
        "            path.parent.mkdir(parents=True, exist_ok=True)\n",
        "            pass\n",
        ["test_state_round_trips_through_the_record_file"]),
    mutation(
        "save-state-drops-the-notified-time",
        '                    "notified_at": state.notified_at,\n',
        "",
        ["test_state_round_trips_through_the_record_file"]),
    mutation(
        "load-state-accepts-a-non-text-version",
        '        notified_version=version if isinstance(version, str) else "",\n',
        "        notified_version=version,\n",
        ["test_load_state_unreadable_file_gives_empty_state"]),
    # --- the GUI report (wh-blocked-mic-message.1.2) ------------------------
    mutation_in(
        GUI,
        "gui-the-first-placement-does-not-report",
        "                    first.resume_lifetime()\n"
        "                    self._report_update_notice_shown()\n",
        "                    first.resume_lifetime()\n",
        ["test_the_command_goes_out_with_the_version_when_the_notice_appears",
         "test_update_now_sends_the_command_with_the_tag"]),
    mutation_in(
        GUI,
        "gui-the-later-placement-does-not-report",
        "                    toast.resume_lifetime()\n"
        "                    self._report_update_notice_shown()\n",
        "                    toast.resume_lifetime()\n",
        ["test_a_notice_that_waits_sends_nothing_until_it_appears",
         "test_a_second_message_for_a_waiting_version_replaces_the_waiting_notice"]),
    mutation_in(
        GUI,
        "gui-a-waiting-notice-is-reported",
        "                    toast.pause_lifetime()\n"
        "                    toast.hide()\n",
        "                    toast.pause_lifetime()\n"
        "                    toast.hide()\n"
        "                    self._report_update_notice_shown()\n",
        ["test_a_notice_that_waits_sends_nothing_until_it_appears",
         "test_a_notice_that_never_appears_returns_at_the_next_update_check"]),
    mutation_in(
        GUI,
        "gui-the-report-is-sent-again-at-every-placement",
        '        self._update_notice_unreported_version = ""\n'
        '        self.send_command({"action": "update_notice_shown", "version": version})\n',
        '        self.send_command({"action": "update_notice_shown", "version": version})\n',
        ["test_the_command_goes_out_once_however_often_the_stack_is_placed"]),
    mutation_in(
        GUI,
        "gui-the-report-names-no-version",
        '        self.send_command({"action": "update_notice_shown", "version": version})\n',
        '        self.send_command({"action": "update_notice_shown", "version": ""})\n',
        ["test_the_command_goes_out_with_the_version_when_the_notice_appears"]),
    mutation_in(
        GUI,
        "gui-the-report-action-is-renamed",
        '        self.send_command({"action": "update_notice_shown", "version": version})\n',
        '        self.send_command({"action": "update_notice_shows", "version": version})\n',
        ["test_the_command_goes_out_with_the_version_when_the_notice_appears"]),
    mutation_in(
        GUI,
        "gui-a-new-update-notice-reports-nothing",
        "            self._update_notice_unreported_version = version\n"
        "            self._stack_update_notice()\n",
        "            self._stack_update_notice()\n",
        ["test_the_command_goes_out_with_the_version_when_the_notice_appears",
         "test_the_command_names_the_version_of_the_notice_on_the_screen"]),
    mutation_in(
        GUI,
        "gui-the-version-is-reportable-before-the-text",
        "            self._update_notice_tag = tag\n",
        "            self._update_notice_tag = tag\n"
        "            self._update_notice_unreported_version = version\n",
        ["test_a_notice_whose_text_failed_to_appear_is_not_reported"]),
    mutation_in(
        GUI,
        "gui-the-failure-notice-keeps-a-waiting-version-pending",
        '            self._update_notice_unreported_version = ""\n'
        "            # Hide the buttons before show_prompt, which sizes the toast.\n"
        "            self._update_notice_toast._yes_button.hide()\n",
        "            # Hide the buttons before show_prompt, which sizes the toast.\n"
        "            self._update_notice_toast._yes_button.hide()\n",
        ["test_a_failure_notice_that_replaces_a_waiting_update_notice_sends_nothing"]),
    mutation_in(
        GUI,
        "gui-the-failure-notice-reports-a-version",
        '            self._update_notice_unreported_version = ""\n'
        "            # Hide the buttons before show_prompt, which sizes the toast.\n"
        "            self._update_notice_toast._yes_button.hide()\n",
        '            self._update_notice_unreported_version = "failure"\n'
        "            # Hide the buttons before show_prompt, which sizes the toast.\n"
        "            self._update_notice_toast._yes_button.hide()\n",
        ["test_the_failure_notice_sends_nothing_and_the_update_notice_after_it_sends_one"]),
    # A second message builds a new toast, so the waiting one is left behind
    # instead of being replaced.
    mutation_in(
        GUI,
        "gui-a-second-message-builds-a-second-toast",
        "            self._update_notice_toast.show_prompt(\n"
        "                title=UPDATE_NOTICE_TITLE.format(version=version),\n",
        "            self._update_notice_toast = GrantPromptToast()\n"
        "            self._update_notice_toast.show_prompt(\n"
        "                title=UPDATE_NOTICE_TITLE.format(version=version),\n",
        ["test_a_second_message_for_a_waiting_version_replaces_the_waiting_notice"]),
    # --- the Logic handler (wh-blocked-mic-message.1.2) ----------------------
    mutation_in(
        MAIN_PY,
        "main-the-command-is-not-in-the-command-map",
        '            "update_notice_shown": lambda: self.create_task_with_error_handling(self._handle_update_notice_shown(command.get("version", "")), "UpdateNoticeShown"),\n',
        "",
        ["test_the_update_notice_shown_command_reaches_the_handler"]),
    mutation_in(
        MAIN_PY,
        "main-the-map-entry-passes-no-version",
        'self._handle_update_notice_shown(command.get("version", ""))',
        'self._handle_update_notice_shown("")',
        ["test_the_update_notice_shown_command_reaches_the_handler"]),
    mutation_in(
        MAIN_PY,
        "main-the-handler-writes-no-record",
        "                update_check.record_notice_shown,\n"
        "                update_check.state_file_path(),\n",
        "                lambda *args: True,\n"
        "                update_check.state_file_path(),\n",
        ["test_the_handler_records_the_version_and_the_time"]),
    mutation_in(
        MAIN_PY,
        "main-the-handler-records-another-version",
        "                version,\n"
        "                time.time(),\n",
        '                "0.0.0",\n'
        "                time.time(),\n",
        ["test_the_handler_records_the_version_and_the_time"]),
    mutation_in(
        MAIN_PY,
        "main-the-handler-records-no-time",
        "                version,\n"
        "                time.time(),\n",
        "                version,\n"
        "                0.0,\n",
        ["test_the_handler_records_the_version_and_the_time"]),
    mutation_in(
        MAIN_PY,
        "main-the-handler-accepts-a-command-without-a-version",
        "            if not (isinstance(version, str) and version):\n"
        "                logger.warning(\n"
        '                    "update_notice_shown dropped, version missing: %r", version\n',
        "            if False:\n"
        "                logger.warning(\n"
        '                    "update_notice_shown dropped, version missing: %r", version\n',
        ["test_a_command_without_a_version_records_nothing"]),
    mutation_in(
        MAIN_PY,
        "main-a-failed-record-write-is-not-logged",
        "                logger.warning(\n"
        '                    "Update notice: could not record the shown notice, so the "\n'
        '                    "notice for %s can appear again at the next check.",\n'
        "                    version,\n"
        "                )\n",
        "                pass\n",
        ["test_a_failed_write_logs_one_line_and_does_not_raise"]),
    mutation_in(
        MAIN_PY,
        "main-an-exception-in-the-record-write-escapes",
        "        except Exception as exc:\n"
        '            logger.warning("Update notice record failed: %s", exc)\n',
        "        except ZeroDivisionError as exc:\n"
        '            logger.warning("Update notice record failed: %s", exc)\n',
        ["test_an_exception_in_the_write_is_logged_not_raised"]),
    mutation_in(
        MAIN_PY,
        "main-the-handler-writes-to-another-state-file",
        "                update_check.state_file_path(),\n",
        '                update_check.state_file_path().with_suffix(".bad"),\n',
        ["test_the_handler_records_the_version_and_the_time"]),
]


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
