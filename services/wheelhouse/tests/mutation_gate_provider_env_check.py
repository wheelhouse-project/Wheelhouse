"""Mutation gate for wh-provider-env-startup-check.

Run with the isolated wheelhouse Python. --check compiles every mutant and
requires exactly one anchor without writing source or running tests. A full
sweep uses scripts/run_tests.py and requires named AssertionError failures in
fresh JUnit XML, never collection/runtime errors. --only accepts mutation names.
The real-uv empirical test is excluded: deliberately unsafe command mutations
must only reach the recording runner, never execute an unsafe uv command.

Reuse the existing tested mutation build/restore machinery; override its test
runner and bytecode scope rather than copying its source-mutation workflow.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mutation_gate_pattern_catalog_nested_group as safety
from mutation_gate_owned_guard import MutationRun


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
SOURCE = SERVICE / "stt/provider_env_check.py"
MANAGER = SERVICE / "service_manager.py"
TEST = "tests/test_provider_env_check.py"
COMMAND = "test_command_is_offline_read_only_and_bounded"
STARTUP = "test_startup_starts_the_check_before_first_launch_and_submits_one_notice"
FAILURE = "test_failure_warns_once_and_continues"
OWN_ENV = "test_all_providers_use_their_own_lock_and_environment"
LOOP_FREE = "test_startup_check_does_not_block_the_logic_event_loop"
LOOP_NOTICE = "test_a_finished_check_keeps_its_notice_when_a_sibling_never_answers"


def mutation(name, old, new, catcher, assertion, file=SOURCE):
    return dict(name=name, file=file, old=old, new=new,
                catcher=catcher, assertion=assertion)


MUTATIONS = [
    mutation("development-packages-required", '"--no-dev", ', '', COMMAND, 'must not require development packages'),
    mutation("extra-development-packages-rejected", '"--inexact", ', '', COMMAND, 'must tolerate installed development packages'),
    mutation("network-allowed", '"--offline", ', '', COMMAND, 'forbid network requests'),
    mutation("environment-can-install", '"--check", ', '', COMMAND, 'report, never install'),
    mutation("lock-can-update", '"--frozen",', '', COMMAND, 'never update uv.lock'),
    mutation("python-can-download", '"--no-python-downloads", ', '', COMMAND, 'no-python-downloads'),
    mutation("subprocess-has-no-deadline", 'timeout=CHECK_TIMEOUT_SECONDS,', 'timeout=None,', COMMAND, 'five-second limit'),
    mutation("provider-project-not-pinned", '"--project", str(directory), ', '',
             'test_inherited_project_override_cannot_select_another_lock', 'must not select another lock'),
    mutation("provider-env-not-pinned", 'env["UV_PROJECT_ENVIRONMENT"] = str(directory / ".venv")',
             'env["UV_PROJECT_ENVIRONMENT"] = "another-project-env"', OWN_ENV, 'UV_PROJECT_ENVIRONMENT'),
    mutation("caller-env-not-cleared", 'env.pop("VIRTUAL_ENV", None)', 'pass', OWN_ENV, 'VIRTUAL_ENV'),
    mutation("missing-lock-check-removed", 'if not all((directory / name).is_file() for name in ("pyproject.toml", "uv.lock")):',
             'if False:', 'test_missing_local_lock_never_checks_an_ancestor_project', 'missing local lock needs one warning'),
    mutation("matching-provider-warned", 'if result.returncode == 0:\n        return False',
             'if result.returncode == 0:\n        return True', 'test_matching_environment_is_silent', 'assert'),
    mutation("mismatch-ignored", '        return True\n    raise RuntimeError',
             '        return False\n    raise RuntimeError', 'test_mismatch_has_exactly_one_named_remedy', 'one notice per mismatched provider'),
    # The first entry below replaces the old any-exit-two-is-mismatch, whose
    # anchor wh-provider-env-check-current-uv deleted: the match no longer reads
    # the exit code at all. The three entries after it are new; they guard the
    # match that replaced it. Two further mutations from that work are
    # deliberately NOT here. They make _check_one create a stray file, and their
    # only catchers are test_installed_uv_missing_locked_package_is_read_only
    # and test_installed_uv_runtime_check_accepts_installer_and_developer_envs
    # -- the real-uv tests this gate excludes on purpose, so that an unsafe
    # command mutation can never execute a real uv command (see the docstring).
    # Those two tests guard the read-only promise themselves, in the ordinary
    # suite and CI.
    mutation("any-nonzero-exit-is-mismatch", 'if result.stderr.rstrip().endswith(_OUTDATED):',
             'if True:', FAILURE + '[invalid-lock]', 'exactly one warning'),
    mutation("mismatch-recoupled-to-exit-code-two", 'if result.stderr.rstrip().endswith(_OUTDATED):',
             'if result.returncode == 2 and result.stderr.rstrip().endswith(_OUTDATED):',
             'test_measured_uv_versions_both_give_the_repair_notice[uv-0.12.17]',
             'must still give one notice'),
    mutation("diagnostic-matched-anywhere-in-stderr", 'if result.stderr.rstrip().endswith(_OUTDATED):',
             'if _OUTDATED in result.stderr:',
             'test_a_line_after_the_diagnostic_is_not_a_recognized_mismatch',
             'a line after the diagnostic makes the form unrecognized'),
    mutation("exit-zero-short-circuit-removed", 'if result.returncode == 0:\n        return False',
             'if False:\n        return False',
             'test_success_exit_gives_no_notice_even_when_stderr_holds_the_diagnostic',
             'exit 0 must stay silent whatever stderr says'),
    mutation("worker-failure-silent", '            result = exc\n        results.put',
             '            result = False\n        results.put', FAILURE + '[missing-uv]', 'exactly one warning'),
    mutation("failure-warning-removed", '            log.warning("Could not check %s environment: %s", name, result)',
             '            pass', FAILURE + '[timeout]', 'exactly one warning'),
    mutation("provider-name-lost", 'notices.append(f"{name}: Speech environment',
             'notices.append(f"Speech provider: Speech environment', 'test_mismatch_has_exactly_one_named_remedy', 'Alpha'),
    mutation("remedy-lost", 'Speech environment is out of date. {_REMEDY}',
             'Speech environment is out of date.', 'test_mismatch_has_exactly_one_named_remedy', 'message.count'),
    mutation("checks-run-in-series", 'threading.Thread(target=worker, args=(index, provider),\n                             name="ProviderEnvironmentCheck", daemon=True).start()',
             'worker(index, provider)', 'test_checks_are_concurrent', 'must run together'),
    mutation("parent-waits-for-stuck-runner",
             'deadlines[index] = time.monotonic() + CHECK_TIMEOUT_SECONDS + COLLECT_GRACE_SECONDS',
             'deadlines[index] = time.monotonic() + 3.0',
             'test_stuck_runner_does_not_hold_startup_or_emit_late_notice', 'before a stuck runner'),
    mutation("providers-share-one-deadline",
             'deadlines[index] = time.monotonic() + CHECK_TIMEOUT_SECONDS + COLLECT_GRACE_SECONDS',
             'deadlines[index] = deadlines.get(0, time.monotonic() + CHECK_TIMEOUT_SECONDS)',
             LOOP_NOTICE, 'answered inside its own limit must keep its notice'),
    mutation("startup-check-skipped", 'if not self._provider_environments_checked:',
             'if False:', STARTUP, 'check must start before the first launch', MANAGER),
    mutation("startup-check-repeated", 'self._provider_environments_checked = True',
             'self._provider_environments_checked = False', STARTUP, 'checked exactly once per run', MANAGER),
    mutation("notice-not-submitted", 'notify_provider_environment_mismatches(notices)\n',
             'pass\n', STARTUP, 'must reach the notifier queue', MANAGER),
    mutation("startup-waits-for-the-check",
             'threading.Thread(target=report_provider_environments,\n'
             '                             name="ProviderEnvironmentNotice", daemon=True).start()',
             'report_provider_environments()',
             LOOP_FREE, 'Logic event loop was blocked', MANAGER),
    mutation("notice-delivery-failure-silent", 'log.warning("Could not queue provider startup notice: %s", message)',
             'pass', 'test_notice_queue_failure_does_not_abort_startup[full]', 'log delivery failure'),
    mutation("notice-reads-uninitialized-module", 'from services.wheelhouse.utils.logging_setup import get_notifier_worker',
             'from utils.logging_setup import get_notifier_worker',
             'test_notice_reaches_worker_initialized_by_production_logging', 'initialized production worker must accept'),
    mutation("notice-payload-has-wrong-identity", 'from utils.notifier_worker import NotifierPayload',
             'from services.wheelhouse.utils.notifier_worker import NotifierPayload',
             'test_notice_reaches_worker_initialized_by_production_logging', 'initialized production worker must accept'),
]


def clear_bytecode():
    # Only compiled copies of the two mutated modules, not every cache in the service.
    for source in (SOURCE, MANAGER):
        for pyc in (source.parent / "__pycache__").glob(source.stem + ".*.pyc"):
            pyc.unlink(missing_ok=True)


def read_cases(report):
    root = ET.parse(report).getroot()
    return {case.attrib["name"]: case for case in root.iter("testcase")}


def is_assertion_failure(failure):
    # Pytest sometimes omits the exception name for rewritten assertions.
    # Anchor at the start so an unrelated exception mentioning it is refused.
    return failure.attrib.get("message", "").startswith(("AssertionError", "assert "))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--only", nargs="+", choices=[m["name"] for m in MUTATIONS])
    args = parser.parse_args()
    selected = [m for m in MUTATIONS if not args.only or m["name"] in args.only]
    safety.MUTATIONS = MUTATIONS
    safety._clear_bytecode = clear_bytecode
    if args.check:
        return safety.check_only(selected)
    print(f"Full set {len(MUTATIONS)}; selected {len(selected)}; skipped {len(MUTATIONS)-len(selected)}", flush=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_OFFLINE="1", UV_NO_SYNC="1")
    guard = MutationRun(ROOT, [item["file"] for item in selected])
    with guard.protect(safety, ("_restore", "_clear_bytecode")), tempfile.TemporaryDirectory(prefix="wh-provider-gate-") as directory:
        report = Path(directory) / "results.xml"
        def run(*extra):
            report.unlink(missing_ok=True)
            return guard.run(
                [sys.executable, str(ROOT / "scripts/run_tests.py"), *extra,
                 "-p", "no:randomly", "--junitxml", str(report)],
                cwd=ROOT, env=env, timeout=90,
            )
        safety._pytest = run
        safety.TEST_FILE = TEST
        clear_bytecode()
        baseline = run(TEST, "-k", "not installed_uv")
        if baseline.returncode or not report.exists():
            print("ERROR baseline", baseline.stdout, baseline.stderr)
            return 1
        cases = read_cases(report)
        if any(m["catcher"] not in cases or cases[m["catcher"]].find("skipped") is not None for m in selected):
            print("ERROR missing or skipped catcher")
            return 1
        print(f"Baseline green; {len(cases)} collected tests; all catchers present", flush=True)
        caught = survived = errors = 0
        for item in selected:
            mutant, original, error = safety._build_mutant(item)
            if error:
                errors += 1
                print("ERROR", error, flush=True)
                continue
            safety.PYTEST_ARGS = ["-k", item["catcher"].split("[")[0]]
            result, error = safety._apply_and_run(item["file"], mutant, original)
            if item["file"].read_bytes() != original:
                print("ERROR original source not restored; stopping", flush=True)
                return 1
            if error or result is None or result.returncode not in (0, 1) or not report.exists():
                errors += 1
                print("ERROR", item["name"], error or "runner failed", flush=True)
                continue
            cases = read_cases(report)
            failures = {name: case.find("failure") for name, case in cases.items() if case.find("failure") is not None}
            if (any(case.find("error") is not None for case in cases.values()) or
                    any(not is_assertion_failure(failure) for failure in failures.values())):
                errors += 1
                print("ERROR", item["name"], "unrelated runtime/setup failure", result.stdout, flush=True)
                continue
            failure = failures.get(item["catcher"])
            if failure is None or item["assertion"] not in (failure.text or ""):
                survived += 1
                print("SURVIVED", item["name"], result.stdout, flush=True)
            else:
                caught += 1
                print("CAUGHT", item["name"], "by", item["catcher"],
                      failure.attrib.get("message", "").splitlines()[0], flush=True)
        print(f"Scope {len(selected)} of {len(MUTATIONS)}; caught {caught}, survivors {survived}, errors {errors}", flush=True)
        return int(bool(survived or errors))


if __name__ == "__main__":
    raise SystemExit(main())
