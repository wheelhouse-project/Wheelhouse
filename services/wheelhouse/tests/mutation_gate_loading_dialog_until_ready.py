"""Mutation gate for the loading dialog that stays until the provider is ready.

Bead wh-loading-dialog-until-ready, acceptance item 8: each new guard test
has a mutation gate result. Committed beside the tests it checks, so it can
be re-run on demand. Run it from the service directory:

    uv run python tests/mutation_gate_loading_dialog_until_ready.py [--check] [--only NAME[,NAME]]

Follows the user-level `mutation-gate` skill:
  - every pattern must match the target file EXACTLY once (ambiguous and
    missing are separate errors, never "survived");
  - every mutant must compile (a SyntaxError is an error, never "caught");
    --check compiles every mutant it can build;
  - bytecode caches are cleared and PYTHONDONTWRITEBYTECODE=1 is set;
  - every expected catcher name is validated against --collect-only of
    that mutation's own selection before the first mutation;
  - the unmutated baseline of every selection must be green first;
  - each mutation runs under its own subprocess timeout, and a pytest-timeout
    abort (`+++ Timeout +++`) is an error, never a verdict;
  - COLUMNS=1000, so the short-summary reason is not cut off;
  - the verdict keys on the exit code first (0 means survived), and failures
    are read ONLY from the "short test summary info" section;
  - each catch prints the reason, and a reason that is not an assertion is
    marked NOT-AN-ASSERTION so a crash cannot pass for a catch;
  - the source is restored with write_bytes, in a finally, and a second
    KeyboardInterrupt cannot escape with the mutant still on disk;
  - patterns are translated to the target file's own line ending;
  - stdout is line-buffered so a killed run still shows its position.

Deliberately NOT here (no deterministic test can tell them apart, or no
caller reads the result):
  - moving `_show_slow_start_text` after `watch.start()`, and moving the
    show out of the signal lock: both only change the outcome inside a
    short timing window between two threads;
  - `_show_slow_start_text` returning False after a send: its one caller
    ignores the return value.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PER_MUTATION_TIMEOUT = 300  # the launcher selection takes about 20 s

LAUNCHER = "stt/remote_stt_launcher.py"
GUI = "gui.py"

SELECTIONS = {
    "launcher": [
        "tests/test_remote_stt_launcher.py",
        "tests/test_launch_generation.py",
    ],
    "gui": ["tests/test_gui.py::TestWorkingDialog"],
}

SHOW_REFUSAL = (
    "            if (\n"
    "                signal.event.is_set()\n"
    "                or signal.reported\n"
    "                or not self.launch_is_current(generation)\n"
    "            ):\n"
    "                return False\n"
)
LIMIT_REFUSAL = (
    "            if (\n"
    "                signal.event.is_set()\n"
    "                or signal.reported\n"
    "                or not self.launch_is_current(generation)\n"
    "            ):\n"
    "                return\n"
    "            self._hide_working(generation)\n"
)
SLOW_TEXT_CALL = "                self._show_slow_start_text(display_name, signal, generation)\n"
LIMIT_NOTICE = (
    "        self._notify(\n"
    "            display_name,\n"
    '            f"{display_name} did not start, so Wheelhouse cannot hear you. "\n'
    '            f"To restart Wheelhouse, choose Restart Wheelhouse in the "\n'
    '            f"Wheelhouse tray menu.",\n'
    "            generation,\n"
    "        )\n"
)

SLOW_START_CATCHERS = [
    "test_the_slow_start_branch_keeps_its_own_launchs_dialog",
    "test_suppresses_failure_notification_when_subprocess_alive_after_timeout",
]
LIMIT_CATCHER = ["test_a_child_that_is_still_alive_is_never_reported"]
GUI_CATCHER = ["test_a_long_text_change_shows_the_whole_text_inside_the_plaque_width"]


# --------------------------------------------------------------------------
# Mutations.  `old` must appear exactly once in the target file.
# --------------------------------------------------------------------------
MUTATIONS = [
    # ---- acceptance item 1: the text change at the limit ------------------
    dict(
        name="slow-start-text-not-shown",
        file=LAUNCHER, sel="launcher",
        proves="at the limit a live, current launch sends the new dialog text",
        old=SLOW_TEXT_CALL,
        new="                pass\n",
        expect=SLOW_START_CATCHERS,
    ),
    dict(
        name="slow-start-hides-at-the-limit-again",
        file=LAUNCHER, sel="launcher",
        proves="at the limit a live provider's dialog is not hidden",
        old=SLOW_TEXT_CALL,
        new=(
            "                if self.launch_is_current(generation):\n"
            "                    self._hide_working(generation)\n"
            + SLOW_TEXT_CALL
        ),
        expect=SLOW_START_CATCHERS + [
            "test_a_late_death_reports_stopped_like_an_in_window_death",
        ],
    ),
    dict(
        name="slow-start-text-wrong-sentence",
        file=LAUNCHER, sel="launcher",
        proves="the text says exactly that WheelHouse cannot hear yet",
        old='                f"Wheelhouse cannot hear you yet.",\n',
        new='                f"Wheelhouse can hear you yet.",\n',
        expect=SLOW_START_CATCHERS,
    ),
    dict(
        name="slow-start-text-owner-dropped",
        file=LAUNCHER, sel="launcher",
        proves="the text change addresses the same owner (launch) as the dialog",
        old=(
            '                f"Wheelhouse cannot hear you yet.",\n'
            "                generation,\n"
        ),
        new=(
            '                f"Wheelhouse cannot hear you yet.",\n'
            "                None,\n"
        ),
        expect=["test_the_slow_start_branch_keeps_its_own_launchs_dialog"],
    ),
    # ---- the three refusals of the text change (items 2, 3, 5) ------------
    dict(
        name="show-ignores-a-set-slot",
        file=LAUNCHER, sel="launcher",
        proves="a ready that already landed refuses the text change",
        old=SHOW_REFUSAL,
        new=SHOW_REFUSAL.replace("signal.event.is_set()", "False"),
        expect=["test_a_ready_that_lands_before_the_text_change_keeps_it_away"],
    ),
    dict(
        name="show-ignores-a-claimed-report",
        file=LAUNCHER, sel="launcher",
        proves="a launch another party reported refuses the text change",
        old=SHOW_REFUSAL,
        new=SHOW_REFUSAL.replace("or signal.reported", "or False"),
        expect=["test_a_reported_launch_refuses_the_text_change"],
    ),
    dict(
        name="show-ignores-a-replaced-launch",
        file=LAUNCHER, sel="launcher",
        proves="a launch that is not current sends no text change",
        old=SHOW_REFUSAL,
        new=SHOW_REFUSAL.replace(
            "or not self.launch_is_current(generation)", "or False"
        ),
        expect=["test_a_replaced_launchs_slow_start_changes_no_dialog"],
    ),
    # ---- boss ruling R1: no dialog without a watch to end it -------------
    dict(
        name="refused-watch-start-keeps-the-dialog",
        file=LAUNCHER, sel="launcher",
        proves="a watch thread that cannot start hides the dialog it kept",
        old=(
            "                    # (wh-loading-dialog-until-ready, boss ruling R1).\n"
            "                    if self.launch_is_current(generation):\n"
            "                        self._hide_working(generation)\n"
        ),
        new=(
            "                    # (wh-loading-dialog-until-ready, boss ruling R1).\n"
            "                    pass\n"
        ),
        expect=["test_a_watch_that_cannot_start_hides_the_dialog"],
    ),
    dict(
        name="no-watch-branch-keeps-the-dialog",
        file=LAUNCHER, sel="launcher",
        proves="a slow start with no slot or no child still hides the dialog",
        old=(
            "                # (wh-loading-dialog-until-ready, boss ruling R1).\n"
            "                self._hide_working(generation)\n"
            "        elif undeclared_failure:\n"
        ),
        new=(
            "                # (wh-loading-dialog-until-ready, boss ruling R1).\n"
            "                pass\n"
            "        elif undeclared_failure:\n"
        ),
        expect=["test_a_slow_start_with_no_watch_still_hides_the_dialog"],
    ),
    # ---- acceptance item 4: the end of the late-death watch ---------------
    dict(
        name="watch-limit-does-not-end-the-dialog",
        file=LAUNCHER, sel="launcher",
        proves="the late-death watch at its limit calls _end_watch_at_limit",
        old=(
            "                self._end_watch_at_limit(display_name, signal, generation)\n"
            "                return\n"
        ),
        new=(
            "                pass\n"
            "                return\n"
        ),
        expect=LIMIT_CATCHER,
    ),
    dict(
        name="limit-ignores-a-set-slot",
        file=LAUNCHER, sel="launcher",
        proves="a ready that lands while the watch polls gets no hide and no notice at the limit",
        old=LIMIT_REFUSAL,
        new=LIMIT_REFUSAL.replace("signal.event.is_set()", "False"),
        # The watch body returns on a set event before it reaches the
        # limit, so only a ready that lands between the poll and the
        # limit check reaches this condition; the catcher calls
        # `_end_watch_at_limit` directly with a set slot.
        expect=["test_a_ready_that_lands_at_the_watch_limit_keeps_the_notice_away"],
    ),
    dict(
        name="limit-ignores-a-claimed-report",
        file=LAUNCHER, sel="launcher",
        proves="a launch another party reported gets no second notice at the limit",
        old=LIMIT_REFUSAL,
        new=LIMIT_REFUSAL.replace("or signal.reported", "or False"),
        expect=["test_the_watch_limit_of_a_reported_launch_is_silent"],
    ),
    dict(
        name="limit-ignores-a-replaced-launch",
        file=LAUNCHER, sel="launcher",
        proves="a launch that is not current changes no dialog at the limit",
        old=LIMIT_REFUSAL,
        new=LIMIT_REFUSAL.replace(
            "or not self.launch_is_current(generation)", "or False"
        ),
        expect=["test_the_watch_limit_of_a_replaced_launch_changes_no_dialog"],
    ),
    dict(
        name="limit-does-not-hide",
        file=LAUNCHER, sel="launcher",
        proves="the watch limit hides the dialog",
        old=(
            "                return\n"
            "            self._hide_working(generation)\n"
            "        self._notify(\n"
        ),
        new=(
            "                return\n"
            "            pass\n"
            "        self._notify(\n"
        ),
        expect=LIMIT_CATCHER,
    ),
    dict(
        name="limit-hides-another-owner",
        file=LAUNCHER, sel="launcher",
        proves="the hide at the watch limit addresses the watch's own launch",
        old=(
            "                return\n"
            "            self._hide_working(generation)\n"
            "        self._notify(\n"
        ),
        new=(
            "                return\n"
            "            self._hide_working(None)\n"
            "        self._notify(\n"
        ),
        expect=LIMIT_CATCHER,
    ),
    dict(
        name="limit-sends-no-notice",
        file=LAUNCHER, sel="launcher",
        proves="the watch limit sends one notice",
        old=LIMIT_NOTICE,
        new="        pass\n",
        expect=LIMIT_CATCHER,
    ),
    dict(
        name="limit-notice-names-another-tray-item",
        file=LAUNCHER, sel="launcher",
        proves="the notice names the tray item with the exact words of the code",
        old='            f"To restart Wheelhouse, choose Restart Wheelhouse in the "\n',
        new='            f"To restart Wheelhouse, choose Restart Speech in the "\n',
        expect=LIMIT_CATCHER,
    ),
    dict(
        name="limit-notice-owner-dropped",
        file=LAUNCHER, sel="launcher",
        proves="the notice carries the watch's own launch",
        old=(
            '            f"Wheelhouse tray menu.",\n'
            "            generation,\n"
        ),
        new=(
            '            f"Wheelhouse tray menu.",\n'
            "            None,\n"
        ),
        expect=LIMIT_CATCHER,
    ),
    # ---- acceptance item 1: the whole text shows, no cut word ------------
    dict(
        name="gui-label-does-not-wrap",
        file=GUI, sel="gui",
        proves="the dialog text wraps inside the plaque width",
        old="        self._message_label.setWordWrap(True)\n",
        new="        self._message_label.setWordWrap(False)\n",
        expect=GUI_CATCHER,
    ),
    dict(
        name="gui-visible-dialog-does-not-grow",
        file=GUI, sel="gui",
        proves="a text change on a visible dialog grows the dialog so every line shows",
        old=(
            "        if self.isVisible():\n"
            "            # A longer text can need more lines than the dialog has. The\n"
            "            # dialog grows in height, and its centre stays where it was\n"
            "            # (wh-loading-dialog-until-ready). The width stays: the text\n"
            "            # wraps inside it.\n"
            "            centre = self.geometry().center()\n"
            "            self._fit_text(self.width())\n"
            "            geometry = self.geometry()\n"
            "            geometry.moveCenter(centre)\n"
            "            self.move(geometry.topLeft())\n"
            "        else:\n"
        ),
        new=(
            "        if self.isVisible():\n"
            "            pass\n"
            "        else:\n"
        ),
        expect=GUI_CATCHER,
    ),
    dict(
        name="gui-grown-dialog-is-not-recentred",
        file=GUI, sel="gui",
        proves="the grown dialog keeps its centre",
        old=(
            "            geometry = self.geometry()\n"
            "            geometry.moveCenter(centre)\n"
            "            self.move(geometry.topLeft())\n"
        ),
        new="            pass\n",
        expect=["test_a_text_change_that_grows_the_dialog_keeps_its_centre"],
    ),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def newline_of(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def to_file_newline(text: str, nl: str) -> str:
    return text.replace("\n", nl) if nl != "\n" else text


def clear_pycache(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        if ".venv" in cache.parts:
            continue
        shutil.rmtree(cache, ignore_errors=True)


def restore(path: Path, original: bytes) -> None:
    """Put the file back. A second Ctrl+C must not escape with the mutant."""
    held = None
    for _attempt in range(6):
        try:
            path.write_bytes(original)
            if path.read_bytes() != original:
                raise OSError("read-back differs")
            if held is not None:
                clear_pycache(SERVICE)
                raise held
            return
        except KeyboardInterrupt as exc:      # second interrupt, inside restore
            if held is not None and exc is held:
                raise
            held = exc
            time.sleep(0.2)
        except OSError:                       # WinError 5: another holder
            time.sleep(0.5)
    print(f"  [ERROR] COULD NOT RESTORE {path} -- the mutant is still on disk")
    if held is not None:
        clear_pycache(SERVICE)
        raise held


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "1000"
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    return env


def pytest_cmd(sel: str, *extra: str) -> list[str]:
    # `-o addopts=` drops the project's --junitxml, so the gate writes no
    # report file into the tree; the ini timeout (30 s) stays in force.
    return [PYTHON, "-m", "pytest", *SELECTIONS[sel], "-p", "no:cacheprovider",
            "-o", "addopts=", *extra]


def collected_test_names(sel: str) -> set[str]:
    proc = subprocess.run(
        pytest_cmd(sel, "--collect-only", "-q"),
        cwd=str(SERVICE), capture_output=True, text=True, timeout=600,
        env=child_env(),
    )
    names = set()
    for line in proc.stdout.splitlines():
        if "::" not in line:
            continue
        tail = line.split("::")[-1].split(" ")[0].strip()
        names.add(tail)
        names.add(tail.split("[")[0])
    return names


SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_END = re.compile(r"^=+ .* =+$")
FAIL_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def run_selection(sel: str) -> tuple[int, str, dict[str, str] | None]:
    """Run one selection.

    Returns (rc, combined output, {test name: reason}). The dict is None
    when the exit code says tests failed but no short-summary section was
    found (an unreadable run, never a verdict).
    """
    proc = subprocess.run(
        pytest_cmd(sel, "-q", "-rfE"),
        cwd=str(SERVICE), capture_output=True, text=True,
        timeout=PER_MUTATION_TIMEOUT, env=child_env(),
    )
    out = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return 0, out, {}
    failed: dict[str, str] = {}
    in_summary = False
    seen_header = False
    for raw in proc.stdout.splitlines():
        line = raw.rstrip()
        if SUMMARY_HEADER.match(line):
            in_summary = seen_header = True
            continue
        if in_summary and SECTION_END.match(line):
            in_summary = False
            continue
        if not in_summary:
            continue
        m = FAIL_LINE.match(line)
        if m:
            nodeid, reason = m.group(2), (m.group(3) or "").strip()
            name = nodeid.split("::")[-1]
            failed[name] = reason
            failed[name.split("[")[0]] = reason
    if not seen_header:
        return proc.returncode, out, None
    return proc.returncode, out, failed


def summary_line(out: str) -> str:
    for line in reversed(out.strip().splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            return line.strip()
    return "(no summary line)"


def is_assertion(reason: str) -> bool:
    return (
        reason.startswith("AssertionError")
        or reason.startswith("assert ")
        or reason.startswith("Failed: DID NOT RAISE")
    )


def target_files() -> list[str]:
    files = sorted({m["file"] for m in MUTATIONS})
    tests = sorted({p.split("::")[0] for s in SELECTIONS.values() for p in s})
    return files + tests


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify patterns, compilation and catcher names; run no tests")
    ap.add_argument("--only", default="", help="comma-separated mutation names")
    args = ap.parse_args()

    wanted = [n for n in args.only.split(",") if n]
    unknown = set(wanted) - {m["name"] for m in MUTATIONS}
    if unknown:
        print(f"[ERROR] unknown mutation name(s): {sorted(unknown)}")
        return 2
    selected = [m for m in MUTATIONS if not wanted or m["name"] in wanted]
    print(f"scope: running {len(selected)} of {len(MUTATIONS)} mutations"
          f"{' (--only)' if wanted else ' (full sweep)'}")

    errors: list[str] = []

    # ---- the target files and the guard tests must match HEAD -------------
    st = subprocess.run(["git", "status", "--porcelain", "--", *target_files()],
                        cwd=str(SERVICE), capture_output=True, text=True)
    if st.stdout.strip():
        print("[ERROR] a target or test file differs from HEAD before the gate:")
        print(st.stdout)
        return 2

    # ---- pattern + compile pre-flight ------------------------------------
    sources: dict[str, bytes] = {}
    for m in selected:
        path = SERVICE / m["file"]
        if m["file"] not in sources:
            sources[m["file"]] = path.read_bytes()
        raw = sources[m["file"]]
        nl = newline_of(raw)
        text = raw.decode("utf-8")
        old = to_file_newline(m["old"], nl)
        count = text.count(old)
        if count == 0:
            errors.append(f"{m['name']}: PATTERN-NOT-FOUND in {m['file']}")
            continue
        if count > 1:
            errors.append(f"{m['name']}: PATTERN-AMBIGUOUS ({count} matches) in {m['file']}")
            continue
        mutated = text.replace(old, to_file_newline(m["new"], nl), 1)
        if mutated == text:
            errors.append(f"{m['name']}: MUTATION-IS-A-NO-OP")
            continue
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{m['name']}: DOES-NOT-COMPILE ({e})")
            continue
        m["_mutated"] = mutated.encode("utf-8")
    stale = sum(1 for e in errors if "PATTERN-" in e or "NO-OP" in e)
    nocompile = sum(1 for e in errors if "DOES-NOT-COMPILE" in e)
    print(f"checked {len(selected)} patterns, {stale} stale or ambiguous, "
          f"{nocompile} that do not compile")

    # ---- expected catcher names must exist -------------------------------
    print("collecting test names ...")
    names_by_sel: dict[str, set[str]] = {}
    for sel in sorted({m["sel"] for m in selected}):
        names_by_sel[sel] = collected_test_names(sel)
        if not names_by_sel[sel]:
            errors.append(f"selection {sel}: COLLECTION PRODUCED NO TEST NAMES")
    for m in selected:
        for want in m["expect"]:
            if want not in names_by_sel.get(m["sel"], set()):
                errors.append(f"{m['name']}: EXPECTED-TEST-MISSING {want}")

    if errors:
        print("\n=== PRE-FLIGHT ERRORS ===")
        for e in errors:
            print("  " + e)
        return 2

    if args.check:
        print("\n--check: patterns match once, mutants compile, catcher names exist.")
        return 0

    # ---- baseline must be green ------------------------------------------
    for sel in sorted({m["sel"] for m in selected}):
        print(f"baseline (unmutated) run of selection {sel} ...")
        clear_pycache(SERVICE)
        try:
            rc, out, failed = run_selection(sel)
        except subprocess.TimeoutExpired:
            print("[ERROR] baseline timed out; refusing to start")
            return 2
        print(f"  baseline {sel}: rc={rc}  {summary_line(out)}")
        if rc != 0:
            print("[ERROR] baseline is not green; refusing to start")
            print(out[-3000:])
            return 2

    # ---- the sweep --------------------------------------------------------
    results = []
    for i, m in enumerate(selected, 1):
        path = SERVICE / m["file"]
        original = sources[m["file"]]
        print(f"\n[{i}/{len(selected)}] {m['name']}  ({m['file']}, selection {m['sel']})")
        print(f"      proves: {m['proves']}")
        verdict, detail = "?", ""
        try:
            clear_pycache(SERVICE)
            path.write_bytes(m["_mutated"])
            clear_pycache(SERVICE)
            try:
                rc, out, failed = run_selection(m["sel"])
            except subprocess.TimeoutExpired:
                verdict, detail = "ERROR", "HANG (per-mutation timeout)"
            else:
                if "+++ Timeout +++" in out:
                    verdict, detail = "ERROR", "suite-timeout-abort"
                elif rc == 0:
                    verdict, detail = "SURVIVED", summary_line(out)
                elif rc != 1:
                    verdict, detail = "ERROR", f"pytest rc={rc} {summary_line(out)}"
                elif failed is None:
                    verdict, detail = "ERROR", f"no short-summary section, rc={rc}"
                else:
                    hits = {n: failed[n] for n in m["expect"] if n in failed}
                    others = sorted(
                        n for n in failed
                        if "[" not in n and n not in m["expect"]
                    )
                    if hits:
                        crash = [n for n, r in hits.items() if not is_assertion(r)]
                        verdict = "caught" if not crash else "ERROR"
                        detail = "; ".join(f"{n}: {r[:160]}" for n, r in hits.items())
                        if crash:
                            detail = f"NOT-AN-ASSERTION in {crash} | " + detail
                        if others:
                            detail += f" | also failed: {', '.join(others)}"
                    elif m["expect"]:
                        verdict = "SURVIVED-BY-EXPECTED"
                        detail = (f"none of {m['expect']} failed; other failures: "
                                  f"{', '.join(others)}")
                    else:
                        verdict = "CAUGHT-BY-UNNAMED"
                        detail = "; ".join(f"{n}: {failed[n][:160]}" for n in others)
        finally:
            restore(path, original)
            clear_pycache(SERVICE)
        print(f"      -> {verdict}: {detail}")
        results.append((m["name"], verdict, detail, m["proves"]))

    # ---- report -----------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"SUMMARY  ({len(selected)} of {len(MUTATIONS)} mutations run"
          f"{', --only' if wanted else ', full sweep'})")
    print("=" * 72)
    caught = [r for r in results if r[1] == "caught"]
    survived = [r for r in results if r[1].startswith("SURVIVED")]
    other = [r for r in results if r not in caught and r not in survived]
    for name, verdict, detail, proves in results:
        print(f"  {verdict:22s} {name}")
        if verdict != "caught":
            print(f"      untested behaviour: {proves}")
            print(f"      {detail}")
    print(f"\ncaught={len(caught)}  survived={len(survived)}  errors={len(other)}")

    st = subprocess.run(["git", "status", "--porcelain", "--", *target_files()],
                        cwd=str(SERVICE), capture_output=True, text=True)
    dirty = st.stdout.strip()
    print(f"target files after the gate: {'CLEAN' if not dirty else 'DIRTY -- ' + dirty}")
    leftovers = list(SERVICE.rglob("*.mutation-gate.*.tmp"))
    if leftovers:
        print(f"[ERROR] leftover temp files: {leftovers}")
    return 0 if not survived and not other and not leftovers and not dirty else 1


if __name__ == "__main__":
    sys.exit(main())
