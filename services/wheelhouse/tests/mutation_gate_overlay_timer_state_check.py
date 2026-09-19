"""Mutation gate for the two checks a timeout callback now makes
(wh-overlay-timer-cancel-race).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_overlay_timer_state_check.py
    .venv/Scripts/python.exe tests/mutation_gate_overlay_timer_state_check.py --check
    .venv/Scripts/python.exe tests/mutation_gate_overlay_timer_state_check.py \
        --only armed-state-check-dropped,arm-id-check-dropped

``--only`` takes a comma-separated list of mutation names and runs just
those, for the round that adds them; the whole set runs once before the
final commit. It refuses an unknown name rather than running a shorter
sweep than the caller asked for.

David says "show numbers", the build succeeds, and the overlay closes on him
for no reason he can see. Four transitions keep the
(overlay_session_id, paint_generation) pair a timer was armed for, so the
machine's generation gate cannot reject a TIMEOUT carrying that pair. Each of
them emits CANCEL_TIMER, but that effect is dispatched as a task and cannot
run until a later loop turn, while a callback already in the loop's ready
queue runs in this one. Measured on the machine before the fix: the walk
deadline CLOSES a successful build, and the other three families reach an
invalid cell and put the overlay in ERROR.

The fix makes ``_fire_overlay_timeout`` check two things, and each mutation
here removes exactly one of them.

  armed-state-check-dropped     The callback no longer compares the state it
                                was armed for against the machine's current
                                state, so a stale TIMEOUT is fed. All four
                                same-pair families catch it.
  armed-state-read-from-the-bookkeeping
                                The check is present but compares against
                                ``_overlay_armed_timer_state`` instead of the
                                machine. Nothing but an arm or a cancel ever
                                rewrites that field, and neither has run at
                                the racing instant, so the comparison always
                                passes and every family gets through. This is
                                the plausible wrong fix, and the four families
                                are what tell it from the right one.
  none-armed-state-fires        The state comparison is made to tolerate a
                                None armed state, so a timer armed with no
                                state fires on no evidence that the machine
                                is still where it was.
  arm-id-check-dropped          A callback whose timer has been REPLACED acts
                                anyway: it feeds a TIMEOUT for an arm that is
                                gone and clears the live arm's bookkeeping.
  arm-id-never-advances         The id is no longer monotonic, so every arm
                                shares one value and the check above can never
                                separate a superseded callback from the live
                                one.
  bookkeeping-cleared-first     A placement mutation: the three clears MOVE
                                above the id check (they move, they are not
                                copied, so the check cannot pass for the wrong
                                reason). A superseded callback then wipes the
                                live timer's handle and that timer can no
                                longer be cancelled.

All six must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a per-mutation timeout, a suite-timeout abort, a
skipped catcher and a missing expected test name are reported as errors,
never as a verdict. A mutant write that fails is an error too, and a restore
that cannot be proved stops the sweep outright: main.py is the live Logic
source, and a mutant left in it is read as real code by every later run
(wh-overlay-timer-cancel-race.2.1). ``--check`` verifies every pattern
matches exactly once in the current source and that every mutant compiles,
without running a test.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "main.py"

INTEGRATION = "tests/test_logic_overlay_integration.py"
ALL_FILES = [INTEGRATION]

# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# keeps the FAILED summary lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rf"]
PER_MUTATION_TIMEOUT_S = 300

# --- source under mutation, quoted exactly -------------------------------
_ARM_ID_CHECK = (
    '        if arm_id != getattr(self, "_overlay_timer_arm_id", 0):\n'
    "            return\n"
)
_CLEARS = (
    "        self._overlay_timer = None\n"
    "        self._overlay_timer_pair = None\n"
    "        self._overlay_armed_timer_state = None\n"
)
_STATE_CHECK = (
    "        if machine.state is not armed_state:\n"
    "            return\n"
)
_ARM_ID_ADVANCE = (
    '        arm_id = getattr(self, "_overlay_timer_arm_id", 0) + 1\n'
)
# The id check together with the clears it guards, for the placement
# mutation. Longer than _ARM_ID_CHECK on its own, so the two patterns each
# still match exactly once.
_ID_CHECK_THEN_CLEARS = _ARM_ID_CHECK + _CLEARS
# The whole state-checking half, for the wrong-comparison-target mutation.
_STATE_HALF = (
    _CLEARS
    + '        machine = getattr(self, "click_overlay_state", None)\n'
    + "        if machine is None:\n"
    + "            return\n"
    + _STATE_CHECK
)

# --- catcher names -------------------------------------------------------
WALK_TO_PAINT = "test_a_ready_walk_timeout_does_not_close_a_successful_build"
WALK_TO_PAUSED = (
    "test_a_ready_walk_timeout_does_not_error_a_pause_during_the_walk"
)
PAINT_TO_PAINTED = (
    "test_a_ready_paint_timeout_does_not_error_a_completed_paint"
)
REFRESH_TO_PAINTED = (
    "test_a_ready_refresh_timeout_does_not_error_a_resolved_refresh"
)
SUPERSEDED_ARM = (
    "test_a_stale_callback_does_not_clear_a_later_timers_bookkeeping"
)
NO_ARMED_STATE = "test_a_timer_armed_with_no_state_does_not_fire"

FOUR_FAMILIES = [
    WALK_TO_PAINT, WALK_TO_PAUSED, PAINT_TO_PAINTED, REFRESH_TO_PAINTED,
]

MUTATIONS = [
    {
        # ``pass``, not a deleted line: deleting both lines of the block
        # would be fine here, but ``pass`` keeps every mutation in this file
        # to one shape, and a mutant that cannot compile reads as caught
        # while proving nothing.
        "name": "armed-state-check-dropped",
        "old": _STATE_CHECK,
        "new": "        pass\n",
        "selection": ALL_FILES,
        "expect": FOUR_FAMILIES,
    },
    {
        # The plausible wrong fix. ``_overlay_armed_timer_state`` still holds
        # the armed state at the racing instant, because only an arm or a
        # cancel writes it and neither has run, so this comparison passes
        # every time and closes nothing.
        "name": "armed-state-read-from-the-bookkeeping",
        "old": _STATE_HALF,
        "new": (
            "        recorded = self._overlay_armed_timer_state\n"
            + _CLEARS
            + '        machine = getattr(self, "click_overlay_state", None)\n'
            + "        if machine is None:\n"
            + "            return\n"
            + "        if recorded is not armed_state:\n"
            + "            return\n"
        ),
        "selection": ALL_FILES,
        "expect": FOUR_FAMILIES,
    },
    {
        # There is no separate None branch to remove: the state comparison
        # is what refuses a None armed state. So this makes the comparison
        # TOLERATE None instead, which is the plausible wrong reading of the
        # same line and the only way a stateless arm can reach the feed.
        "name": "none-armed-state-fires",
        "old": _STATE_CHECK,
        "new": (
            "        if armed_state is not None and machine.state "
            "is not armed_state:\n"
            "            return\n"
        ),
        "selection": ALL_FILES,
        "expect": [NO_ARMED_STATE],
    },
    {
        "name": "arm-id-check-dropped",
        "old": _ARM_ID_CHECK,
        "new": "        pass\n",
        "selection": ALL_FILES,
        "expect": [SUPERSEDED_ARM],
    },
    {
        # The id generator, not the check that reads it: every arm then
        # carries the same value and no callback can tell it has been
        # replaced.
        "name": "arm-id-never-advances",
        "old": _ARM_ID_ADVANCE,
        "new": '        arm_id = getattr(self, "_overlay_timer_arm_id", 0)\n',
        "selection": ALL_FILES,
        "expect": [SUPERSEDED_ARM],
    },
    {
        # A MOVE, not an added copy: the clears leave their place above the
        # check and land below it. An appended copy would leave the original
        # clears in place and the mutation could read as a survivor of a
        # test that is working correctly.
        "name": "bookkeeping-cleared-first",
        "old": _ID_CHECK_THEN_CLEARS,
        "new": _CLEARS + _ARM_ID_CHECK,
        "selection": ALL_FILES,
        "expect": [SUPERSEDED_ARM],
    },
]


def _clear_bytecode():
    shutil.rmtree(SERVICE / "__pycache__", ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def skipped_names(output):
    """Test names on -v per-test lines that read ``...::name SKIPPED (...)``."""
    names = set()
    for line in output.splitlines():
        if "::" in line and " SKIPPED" in line:
            names.add(line.split("::")[-1].strip().split(" ")[0].split("[")[0])
    return names


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(text: str, newline: str, mut: dict):
    """Return (mutant_text, error). Exactly one match and a compiling result."""
    old = mut["old"].replace("\n", newline)
    new = mut["new"].replace("\n", newline)
    count = text.count(old)
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(old, new, 1)
    try:
        compile(mutated, str(SRC), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only():
    raw = SRC.read_bytes()
    text = raw.decode("utf-8")
    newline = _newline_of(raw)
    stale, non_compiling = [], []
    for mut in MUTATIONS:
        mutant, error = _build_mutant(text, newline, mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    print(
        f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
        f"{len(non_compiling)} that do not compile"
    )
    return 1 if stale or non_compiling else 0


def _restore(path: Path, original: bytes) -> bool:
    """Put the source back, and say whether the original bytes are back.

    Returns True only when the file now holds ``original``. A False is a
    hard condition for the caller, not a warning: a run whose target is
    still mutated cannot yield a verdict, and the mutant is sitting in the
    live Logic source that every later run and every other session sharing
    the checkout will read as real code. The old shape printed the failure
    and returned nothing, so a refused restore on the last mutation let the
    sweep print "caught 6, survived 0, errors 0" and exit 0
    (wh-overlay-timer-cancel-race.2.1; the same repair as
    wh-grid-click-nested-group.2.3).

    A second Ctrl+C landing inside the restore is held, retried once, and
    re-raised only after the bytecode caches are cleared -- a same-size
    mutant otherwise leaves current-looking bytecode for the next run.
    """
    held = None
    restored = False
    for _ in range(2):
        try:
            path.write_bytes(original)
            restored = True
            break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR could not restore {path}: {exc}")
            break
    else:
        print(f"ERROR could not restore {path}: interrupted twice")
    if restored:
        try:
            restored = path.read_bytes() == original
        except OSError as exc:
            print(f"ERROR could not read {path} back: {exc}")
            restored = False
        if not restored:
            print(f"ERROR {path} still differs from the original after the restore")
    _clear_bytecode()
    if held is not None:
        raise held
    return restored


def _apply_and_run(path: Path, mutant: bytes, original: bytes, selection):
    """Write the mutant, run the selection, restore. Return (result, error).

    The write is INSIDE the guarded block on purpose: an interrupt or an
    OSError landing during the write must still reach the restore. A
    restore that fails clears ``result`` as well as setting an error, so
    no caller can read a verdict out of a run whose source file is still
    mutated (wh-overlay-timer-cancel-race.2.1).
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        path.write_bytes(mutant)
        result = _pytest(*selection, *PYTEST_ARGS)
    except subprocess.TimeoutExpired:
        error = "timed out"
    except OSError as exc:
        error = f"could not write the mutant to {path.name}: {exc}"
    finally:
        if not _restore(path, original):
            error = f"the original bytes of {path.name} were not restored"
            result = None
    return result, error


def _selected(argv):
    """The mutations to run, honouring ``--only a,b``. Unknown name -> None.

    A silently shorter sweep than the caller asked for is the failure this
    guards against: a typo in a name would otherwise read as a clean run of
    the mutations that did match.
    """
    wanted = None
    for i, arg in enumerate(argv):
        if arg == "--only" and i + 1 < len(argv):
            wanted = [n for n in argv[i + 1].split(",") if n]
        elif arg.startswith("--only="):
            wanted = [n for n in arg.split("=", 1)[1].split(",") if n]
    if wanted is None:
        return list(MUTATIONS)
    known = {mut["name"]: mut for mut in MUTATIONS}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        print(f"ERROR --only names no such mutation: {unknown}")
        print(f"      known names: {sorted(known)}")
        return None
    return [known[name] for name in wanted]


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    mutations = _selected(sys.argv[1:])
    if mutations is None:
        return 1

    original = SRC.read_bytes()
    text = original.decode("utf-8")
    newline = _newline_of(original)
    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {ALL_FILES}")
    for mut in mutations:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    _clear_bytecode()
    baseline = _pytest(*ALL_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-3000:])
        return 1
    expected_all = {name for mut in mutations for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(f"ERROR expected catchers skipped in the baseline: {skipped_catchers}")
        print(
            "      a skipped catcher can never fail, so its mutation cannot "
            "be proven"
        )
        return 1
    print("baseline green")

    for mut in mutations:
        mutated, error = _build_mutant(text, newline, mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        result, error = _apply_and_run(
            SRC, mutated.encode("utf-8"), original, mut["selection"]
        )
        if error is not None:
            errors.append(f"{mut['name']}: {error}")
            print("ERROR", errors[-1])
            if "not restored" in error:
                # The live Logic source still holds the mutant. Every later
                # mutation would run against it, and so would any other
                # session sharing the checkout, so the sweep stops here
                # instead of piling verdicts on top of a corrupt tree.
                print("ERROR stopping the sweep; restore main.py by hand")
                break
            continue
        assert result is not None
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        skipped = [n for n in mut["expect"] if n in skipped_names(result.stdout)]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
            print("ERROR", errors[-1])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed {sorted(got)}"
            )
        else:
            caught.append(mut["name"])
            print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    names = ", ".join(mut["name"] for mut in mutations)
    print(f"scope: {len(mutations)} mutations, none skipped: {names}")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
