"""Mutation gate for the Debug entry's checkmark (finding
wh-audio-suppression-floating-menu.1.1).

Both menus draw the Debug entry's checkmark from one flag, StateManager's
``debug_mode``.  That flag was a literal ``False`` in the constructor, so a
startup whose settings file says ``LOG_LEVEL = "DEBUG"`` drew NO checkmark
while detailed logging was already on, and the first click ran
``toggle_log_level``, which turns DEBUG into INFO and switched detailed
logging off.  The constructor now reads the level main.py already applied.

Nothing in the running program prevents somebody from putting a literal back.
The only thing that does is the guard class

    tests/test_state_manager.py::TestTheDebugFlagMatchesTheRealLoggingLevel

Two mutations, because the two literals fail different halves of that class
and neither one alone proves all three tests earn their place:

    debug-flag-back-to-a-literal-false -> the DEBUG-startup test and the
        published-state test must fail, and the INFO-startup test must stay
        green.  A literal False agrees with an INFO startup by accident, so
        that test cannot catch this mutation and must not be asked to.
    debug-flag-back-to-a-literal-true  -> the INFO-startup test must fail,
        and the other two must stay green.

Each mutation therefore names its own must-stay-green list.  That is what
stops a vacuous pass: a mutation that broke the constructor outright would
fail all three tests, and this gate reports that as no verdict rather than as
a catch.

crewcut: this file repeats the runner of
tests/mutation_gate_removed_menu_items.py, because that one mutates gui.py
with one target file and one test selection baked in, and generalizing it
would rewrite the restore path every earlier verdict on this branch rests on.
Extract a shared runner module when a third gate needs one.

Run it from services/wheelhouse.  Use the interpreter directly: "uv run"
re-syncs the environment first, and a worktree has no .venv of its own.

    .venv/Scripts/python.exe tests/mutation_gate_debug_flag_matches_level.py
    .venv/Scripts/python.exe tests/mutation_gate_debug_flag_matches_level.py --check
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

SERVICE_ROOT = Path(__file__).resolve().parent.parent
TARGET = SERVICE_ROOT / "state_manager.py"
# Only the guard class.  The gate's question is whether these three tests
# catch the two literals, and a wider selection would spend minutes per
# mutation answering a question nobody asked here.
SELECTION = [
    "tests/test_state_manager.py",
    "-k",
    "TestTheDebugFlagMatchesTheRealLoggingLevel",
]

_ASSIGNMENT_ANCHOR = (
    "        self.debug_mode = logging.getLogger().getEffectiveLevel() == logging.DEBUG"
)

_DEBUG_STARTUP = "test_a_debug_startup_reports_debug_mode"
_INFO_STARTUP = "test_an_info_startup_does_not_report_debug_mode"
_PUBLISHED = "test_the_menus_receive_the_same_answer"

MUTATIONS = [
    {
        "name": "debug-flag-back-to-a-literal-false",
        "old": _ASSIGNMENT_ANCHOR,
        "new": "        self.debug_mode = False",
        "expect": [_DEBUG_STARTUP, _PUBLISHED],
        "green": [_INFO_STARTUP],
    },
    {
        "name": "debug-flag-back-to-a-literal-true",
        "old": _ASSIGNMENT_ANCHOR,
        "new": "        self.debug_mode = True",
        "expect": [_INFO_STARTUP],
        "green": [_DEBUG_STARTUP, _PUBLISHED],
    },
]

TIMEOUT_SECONDS = 300


def _file_newline(path: Path) -> bytes:
    raw = path.read_bytes()
    return b"\r\n" if b"\r\n" in raw else b"\n"


def _to_file_endings(text: str, newline: bytes) -> str:
    if newline == b"\r\n":
        return text.replace("\n", "\r\n")
    return text


def _clear_pycache() -> None:
    for cache in SERVICE_ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _collected_test_names() -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"] + SELECTION,
        cwd=SERVICE_ROOT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )
    names = set()
    for line in proc.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split(" ")[0])
    return names


def _run_selection() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:randomly"] + SELECTION,
        cwd=SERVICE_ROOT, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _restore(original: bytes) -> bool:
    """Put the real source back, prove it is back, and say whether it is.

    The verdict travels back to the caller because a gate that cannot
    restore has to stop: every later mutation would judge code nobody
    meant to run, and a run ending "0 survivors" would leave the mutant
    in a tracked file.  A second Ctrl+C landing inside the restore must
    not escape with the mutant still there, and the bytecode caches have
    to go before the interrupt is re-raised: a same-size mutant leaves
    current-looking bytecode behind.
    """
    held: KeyboardInterrupt | None = None
    for attempt in (0, 1):
        try:
            TARGET.write_bytes(original)
            _clear_pycache()
        except KeyboardInterrupt as interrupt:
            if attempt:
                print("ERROR could not restore %s -- the mutant is still in the file"
                      % TARGET)
                raise
            held = interrupt
            continue
        except OSError as exc:
            if attempt:
                print("ERROR could not restore %s (%s) -- the mutant is still in the file"
                      % (TARGET, exc))
                return False
            continue
        try:
            written = TARGET.read_bytes()
        except OSError as exc:
            print("ERROR could not read %s back (%s) -- treating it as unrestored"
                  % (TARGET, exc))
            return False
        if written == original:
            if held is not None:
                raise held
            return True
        if attempt:
            print("ERROR %s still differs from the original after the restore" % TARGET)
            return False
    return False


def _apply_and_run(mutated: str, original: bytes) -> tuple[int | None, str, str, bool]:
    """Write the mutant, run the selection, and always put the source back.

    The write sits INSIDE the try, so a write that opens the file and
    then fails still reaches the restore.  The restore's own verdict comes
    back with the run's, because the caller has to stop when the source is
    not provably back.

    Returns (returncode, output, status, restored); status is "ran",
    "timeout" or "write-error".
    """
    rc: int | None = None
    out = ""
    status = "ran"
    try:
        TARGET.write_bytes(mutated.encode("utf-8"))
        _clear_pycache()
        rc, out = _run_selection()
    except subprocess.TimeoutExpired:
        status = "timeout"
    except OSError as exc:
        status = "write-error"
        out = str(exc)
    finally:
        restored = _restore(original)
    return rc, out, status, restored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="verify each pattern matches exactly once and compiles; run nothing")
    args = parser.parse_args()

    original = TARGET.read_bytes()
    source = original.decode("utf-8")
    newline = _file_newline(TARGET)

    errors: list[str] = []
    prepared = []
    for mutation in MUTATIONS:
        old = _to_file_endings(mutation["old"], newline)
        new = _to_file_endings(mutation["new"], newline)
        count = source.count(old)
        if count == 0:
            errors.append("%s: pattern-not-found" % mutation["name"])
            continue
        if count > 1:
            errors.append("%s: pattern-ambiguous (%d matches)" % (mutation["name"], count))
            continue
        mutated = source.replace(old, new, 1)
        try:
            compile(mutated, str(TARGET), "exec")
        except SyntaxError as exc:
            errors.append("%s: does-not-compile (%s)" % (mutation["name"], exc))
            continue
        prepared.append((mutation, mutated))

    if args.check:
        print("checked %d patterns, %d stale or broken" % (len(MUTATIONS), len(errors)))
        for line in errors:
            print("  ERROR " + line)
        return 1 if errors else 0

    if errors:
        for line in errors:
            print("ERROR " + line)
        return 1

    collected = _collected_test_names()
    for mutation, _ in prepared:
        for name in list(mutation["expect"]) + list(mutation["green"]):
            if name not in collected:
                print("ERROR %s: expected test name not collected: %s"
                      % (mutation["name"], name))
                return 1

    _clear_pycache()
    rc, out = _run_selection()
    if rc != 0:
        print("ERROR baseline is not green; refusing to mutate")
        print(out[-3000:])
        return 1
    print("baseline green")

    survivors = []
    try:
        for mutation, mutated in prepared:
            rc, out, status, restored = _apply_and_run(mutated, original)
            if not restored:
                print("ERROR %s: %s was not restored -- stopping the gate"
                      % (mutation["name"], TARGET))
                return 1
            if status == "write-error":
                print("ERROR %s: could not write the mutant (%s)"
                      % (mutation["name"], out))
                return 1
            if status == "timeout":
                print("ERROR %s: never-terminates" % mutation["name"])
                survivors.append(mutation["name"])
                continue

            if "+++ Timeout +++" in out:
                print("ERROR %s: suite-timeout-abort, no verdict" % mutation["name"])
                survivors.append(mutation["name"])
                continue

            failed = {line.split("::")[-1].split(" ")[0]
                      for line in out.splitlines() if line.startswith("FAILED")}
            collapsed = [n for n in mutation["green"] if n in failed]
            if collapsed:
                print("ERROR %s: the constructor stopped working (%s failed) -- no verdict"
                      % (mutation["name"], ", ".join(collapsed)))
                survivors.append(mutation["name"])
                continue

            missing = [n for n in mutation["expect"] if n not in failed]
            if rc == 0 or missing:
                print("SURVIVED %s (expected failures not seen: %s)"
                      % (mutation["name"], ", ".join(missing) or "none"))
                survivors.append(mutation["name"])
            else:
                print("caught   %s by %s" % (mutation["name"], ", ".join(sorted(failed))))
    except KeyboardInterrupt:
        _restore(original)
        raise

    print("")
    print("ran %d of %d mutations, %d survivors" % (len(prepared), len(MUTATIONS), len(survivors)))
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
