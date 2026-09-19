"""Re-run the 500-character contract against boundary and removal mutations.

Run with Python from any directory; --check checks anchors and compilation.
Tests always run through scripts/run_tests.py. A fresh bytecode prefix per
sweep and distinct source timestamps prevent stale same-size mutants; each
run has its own JUnit path. Source bytes and timestamps are restored even
after a failed run.
Do not run concurrently with tests or edits in this worktree.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
MANAGER = SERVICE / "speech/pattern_manager.py"
BUDGET = SERVICE / "speech/pattern_expression_budget.py"
DIALOG = SERVICE / "create_pattern_dialog.py"
TESTS = ["tests/test_pattern_manager_update.py", "tests/test_create_pattern_dialog.py"]
IS_WINDOWS = os.name == "nt"
CLEANUP_TIMEOUT = 10
MUTATIONS = [
    ("limit-501", BUDGET, "MAX_EXPRESSION_LENGTH = 500", "MAX_EXPRESSION_LENGTH = 501"),
    ("limit-499", BUDGET, "MAX_EXPRESSION_LENGTH = 500", "MAX_EXPRESSION_LENGTH = 499"),
    ("boundary-inclusive", MANAGER, "if len(expression) > MAX_EXPRESSION_LENGTH:",
     "if len(expression) >= MAX_EXPRESSION_LENGTH:"),
    ("backend-check-removed", MANAGER, "if len(expression) > MAX_EXPRESSION_LENGTH:",
     "if False:"),
    ("editor-logical-limit-removed", DIALOG, "self.setMaxLength(MAX_EXPRESSION_LENGTH)",
     "self.setMaxLength(32767)"),
    ("editor-utf16-capacity-halved", DIALOG, "super().setMaxLength(2 * (length + 1))",
     "super().setMaxLength(length)"),
    ("editor-validator-removed", DIALOG,
     "field._loading_text or within_budget or shortening or restoring_history)",
     "True)"),
    ("editor-overflow-slot-removed", DIALOG, "super().setMaxLength(2 * (length + 1))",
     "super().setMaxLength(2 * length)"),
    ("editor-rejection-feedback-removed", DIALOG, "self.length_rejected = True",
     "self.length_rejected = False"),
    ("loaded-expression-rejected", DIALOG, "self._expression_edit.load_text(text)",
     "self._expression_edit.setText(text)"),
    ("loaded-expression-cannot-shrink", DIALOG,
     "shortening = len(text) < len(field._accepted_text)", "shortening = False"),
    ("loaded-expression-can-grow", DIALOG,
     "shortening = len(text) < len(field._accepted_text)", "shortening = True"),
    ("oversize-compile-guard-removed", DIALOG,
     "if len(expression) > MAX_EXPRESSION_LENGTH:", "if False:"),
    ("rejection-clears-edit-history", DIALOG,
     "self.length_rejected = True",
     "with QSignalBlocker(self):\n            QLineEdit.setText(self, self._accepted_text)\n        self.length_rejected = True"),
    ("undo-legacy-value-rejected", DIALOG,
     "restoring_history = field.isRedoAvailable()", "restoring_history = False"),
    ("native-setter-restoration-removed", DIALOG,
     "text != self._accepted_text and not self._loading_text", "False"),
]


class ProcessCleanupError(RuntimeError):
    """Abort the sweep when a test process tree may still be running."""


def _terminate_test_tree(process):
    errors = []
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False, timeout=CLEANUP_TIMEOUT)
            if result.returncode:
                errors.append("taskkill could not confirm tree termination")
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The entire process group has already exited.
    except Exception as exc:
        errors.append(str(exc))
    try:
        process.communicate(timeout=CLEANUP_TIMEOUT)
    except Exception as exc:
        errors.append(str(exc))
    if errors:
        raise ProcessCleanupError("test tree cleanup failed: " + "; ".join(errors))


def run_tests(directory: Path, index: int):
    report = directory / f"results-{index}.xml"
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(directory / "bytecode")}
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    command = [sys.executable, str(ROOT / "scripts/run_tests.py"), *TESTS,
               "-k", "length_budget", "-p", "no:randomly", f"--junitxml={report}"]
    process = subprocess.Popen(command, cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               start_new_session=not IS_WINDOWS)
    try:
        output, _ = process.communicate(timeout=240)
    except BaseException as failure:
        # Stop only this run's process tree before restoring mutated source.
        try:
            _terminate_test_tree(process)
        except ProcessCleanupError:
            if not isinstance(failure, Exception):
                raise failure
            raise
        raise
    if process.returncode not in (0, 1) or not report.exists():
        raise RuntimeError(f"test runner failed ({process.returncode}): {output[-2000:]}")
    cases = list(ET.parse(report).iter("testcase"))
    if not cases or any(case.find("error") is not None or case.find("skipped") is not None
                        for case in cases):
        raise RuntimeError(f"missing, errored or skipped tests: {output[-2000:]}")
    failures = [case for case in cases if case.find("failure") is not None]
    for case in failures:
        message = case.find("failure").get("message", "")
        if not (message.startswith(("assert ", "AssertionError", "Failed: DID NOT RAISE"))):
            raise RuntimeError(f"non-assertion failure: {case.get('name')}: {message}")
    if (process.returncode == 0) != (not failures):
        raise RuntimeError("JUnit and test runner exit status disagree")
    return {case.get("name") for case in cases}, {case.get("name") for case in failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    originals = {path: path.read_bytes() for _, path, _, _ in MUTATIONS}
    timestamps = {path: (path.stat().st_atime_ns, path.stat().st_mtime_ns)
                  for path in originals}
    prepared = []
    for name, path, old, new in MUTATIONS:
        source = originals[path].decode("utf-8")
        if source.count(old) != 1:
            raise RuntimeError(f"{name}: expected one source anchor, got {source.count(old)}")
        mutant = source.replace(old, new, 1)
        compile(mutant, str(path), "exec")
        prepared.append((name, path, mutant.encode("utf-8")))
    print(f"Checked {len(prepared)} mutations; 0 errors", flush=True)
    if args.check:
        return 0
    survivors = errors = 0
    with tempfile.TemporaryDirectory(prefix="wh-expression-mutations-") as temporary:
        directory = Path(temporary)
        baseline, failures = run_tests(directory, 0)
        if failures:
            raise RuntimeError(f"baseline failed: {sorted(failures)}")
        print(f"Baseline: {len(baseline)} tests green", flush=True)
        for index, (name, path, mutant) in enumerate(prepared, 1):
            try:
                path.write_bytes(mutant)
                accessed, modified = timestamps[path]
                os.utime(path, ns=(accessed, modified + index * 1_000_000_000))
                names, failures = run_tests(directory, index)
                if names != baseline:
                    raise RuntimeError("test selection changed under mutation")
                if failures:
                    print(f"CAUGHT {name}: {', '.join(sorted(failures))}", flush=True)
                else:
                    survivors += 1
                    print(f"SURVIVED {name}", flush=True)
            except ProcessCleanupError:
                raise
            except Exception as exc:
                errors += 1
                print(f"ERROR {name}: {exc}", flush=True)
            finally:
                path.write_bytes(originals[path])
                os.utime(path, ns=timestamps[path])
                if path.read_bytes() != originals[path]:
                    raise RuntimeError(f"restore failed: {path}")
    print(f"Ran {len(prepared)}/{len(MUTATIONS)} mutations; {survivors} survivors; {errors} errors")
    return int(bool(survivors or errors))


if __name__ == "__main__":
    raise SystemExit(main())
