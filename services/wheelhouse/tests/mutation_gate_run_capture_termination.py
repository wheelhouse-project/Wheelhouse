"""Prove real-child termination and deterministic timeout-argument coverage.

Run from the repo root with Python; --check validates patterns without writes.
Only run in an isolated worktree, without concurrent tests in that worktree.
Production timeout defaults and the other termination branches are out of scope.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from mutation_gate_owned_guard import CleanupUnconfirmedError, MutationRun

SERVICE = Path(__file__).resolve().parents[1]
REPO = SERVICE.parents[1]
TARGET = SERVICE / "speech/actions.py"
TEST = "tests/test_run_capture_action.py::TestRunCaptureUnit::"

# Reuse only the existing publication/restoration/entry-interrupt utilities. This gate
# never calls the shared runner's separate subprocess execution path.
_spec = importlib.util.spec_from_file_location(
    "_run_capture_cleanup", REPO / "services/stt_providers/shared/tests/mutation_gate_runner.py")
_cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cleanup)
_restore = _cleanup._restore
_call_cleanup = _cleanup._call_cleanup
_atomic_write_bytes = _cleanup._atomic_write_bytes

MUTATIONS = []
for branch, test in (
    ("asyncio.TimeoutError", "test_timeout_terminates_the_started_process"),
    ("BaseException", "test_unexpected_collector_error_terminates_the_started_process"),
):
    old = (
        f"        except {branch}:\n"
        "            await _terminate_run_capture_process(\n"
        "                process, drain_streams=(process.stdout, process.stderr)\n"
        "            )\n"
    )
    for name, replacement, assertion in (
        ("no-termination", "pass", "is still running"),
        ("natural-exit", "await process.wait()", "exited naturally"),
    ):
        MUTATIONS.append((f"{branch}-{name}", old,
                          f"        except {branch}:\n            {replacement}\n",
                          test, assertion))
MUTATIONS.append((
    "leading-float-deadline-changed",
    "                timeout=timeout_s,\n            )\n        except asyncio.TimeoutError:",
    "                timeout=timeout_s + 1,\n            )\n        except asyncio.TimeoutError:",
    "test_leading_float_parameter_is_consumed_as_timeout",
    "leading float must set the capture deadline",
))


def run_tests(selection, report, guard):
    # An empty, per-run cache prefix prevents reading old bytecode without
    # deleting caches shared with other work. -B also prevents writing it.
    # Retain reports/cache/log roots on uncertainty; cleanup must not race a
    # descendant still using them. Every invocation has a distinct report.
    cache = report.with_suffix(".bytecode")
    cache.mkdir()
    report.unlink(missing_ok=True)
    result = guard.run(
            [sys.executable, str(REPO / "scripts/run_tests.py"), *selection,
             "-p", "no:randomly", f"--junitxml={report}"],
            cwd=REPO, timeout=180,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONPYCACHEPREFIX": str(cache)},
        )
    output = result.stdout + result.stderr
    if result.returncode not in (0, 1) or "+++ Timeout +++" in output:
        raise RuntimeError(f"test runner aborted: {output}")
    cases = ET.parse(report).findall(".//testcase")
    if not cases or any(c.find("error") is not None or c.find("skipped") is not None
                        for c in cases):
        raise RuntimeError(f"missing, skipped, or errored test cases: {output}")
    return result.returncode, cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    marker = REPO / ".tmp/run-capture-cleanup-unconfirmed.json"
    if not args.check and marker.exists():
        raise CleanupUnconfirmedError(f"Prior cleanup is unconfirmed; inspect {marker}")
    original = TARGET.read_bytes()
    source = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in source else "\n"
    prepared = []
    for name, old, new, test, assertion in MUTATIONS:
        old, new = (s.replace("\n", newline) for s in (old, new))
        if source.count(old) != 1:
            raise RuntimeError(f"{name}: pattern matches {source.count(old)} times")
        mutated = source.replace(old, new, 1)
        compile(mutated, str(TARGET), "exec")
        prepared.append((name, mutated.encode("utf-8"), test, assertion))
    print(f"checked {len(prepared)} patterns, 0 stale, 0 that do not compile", flush=True)
    if args.check:
        return 0
    caught = survivors = errors = 0
    guard = MutationRun(REPO, [TARGET])
    directory = guard.evidence_dir
    with guard.protect(sys.modules[__name__], ("_restore",)):
        selection = sorted({TEST + m[2] for m in prepared})
        try:
            rc, cases = run_tests(selection, directory / "baseline.xml", guard)
        except CleanupUnconfirmedError:
            _record_uncertainty(marker, guard)
            raise
        expected_names = {m[2] for m in prepared}
        if (rc or len(cases) != len(expected_names)
                or {c.get("name") for c in cases} != expected_names
                or any(c.find("failure") is not None for c in cases)):
            raise RuntimeError("baseline is not green; refusing to mutate")
        print(f"baseline: {len(cases)} passed", flush=True)
        for index, (name, mutant, test, assertion) in enumerate(prepared):
            try:
                # Atomic publication preserves the whole-byte states _restore
                # recognizes, even when writing the temporary mutant fails.
                _atomic_write_bytes(TARGET, mutant)
                rc, cases = run_tests([TEST + test], directory / f"{index}.xml", guard)
                if len(cases) != 1 or cases[0].get("name") != test:
                    raise RuntimeError("expected catcher was not collected")
                failure = cases[0].find("failure")
                if rc == 0 and failure is None:
                    survivors += 1
                    print(f"SURVIVED {name}", flush=True)
                elif (rc == 1 and failure is not None
                      and failure.get("message", "").startswith("AssertionError:")
                      and assertion in failure.get("message", "")):
                    caught += 1
                    print(f"caught {name}: {failure.get('message')}", flush=True)
                else:
                    raise RuntimeError("catcher failed outside the expected assertion")
            except CleanupUnconfirmedError:
                _record_uncertainty(marker, guard)
                raise
            except (OSError, RuntimeError, ET.ParseError, subprocess.TimeoutExpired) as exc:
                errors += 1
                print(f"ERROR {name}: {exc}", flush=True)
            finally:
                # protect() refuses entry when process exit is unconfirmed;
                # the shared dispatcher retries an interrupted cleanup entry.
                error, deferred = _call_cleanup(_restore, TARGET, original, mutant, name)
                if error:
                    print("ERROR", error, flush=True)
                    raise RuntimeError(error)
                if deferred is not None:
                    raise deferred
    print(f"{len(prepared)} mutations: {caught} caught, {survivors} survivors, {errors} errors")
    return int(bool(survivors or errors))


def _record_uncertainty(marker, guard):
    try:
        marker.write_text(json.dumps({"cleanup_confirmed": False,
                                     "evidence_dir": str(guard.evidence_dir)}), encoding="utf-8")
    except OSError as exc:
        # The in-memory guard remains set even if this extra marker cannot be
        # written; its original/mutant backups were retained before this call.
        print(f"ERROR uncertainty marker write failed: {exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
