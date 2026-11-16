"""Mutation gate for the renumber guard that now arms on EVERY repaint
(wh-overlay-slow-uia-stale-badges.4).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_renumber_guard_every_repaint.py
    .venv/Scripts/python.exe tests/mutation_gate_renumber_guard_every_repaint.py --check
    .venv/Scripts/python.exe tests/mutation_gate_renumber_guard_every_repaint.py \
        --only checks-only-the-first-prior,expiry-uses-the-oldest-swap

``--only`` takes a comma-separated list of mutation names and runs just
those, for the round that adds them; the whole set runs once before the
final commit. It refuses an unknown name rather than running a shorter
sweep than the caller asked for.

David reads badge sixteen, says "click sixteen", and the numbers repaint
while he speaks. Badge numbers are positional (uia_walker.py increments
display_number in walk order), so one control added or removed anywhere
renumbers everything after it, and his words then resolve against a list he
never saw. The guard that catches this was armed only by the browser timer's
proactive refresh; it is now armed on the PAINTED edge of every repaint that
puts a different snapshot on screen.

  arm-dropped                   The PAINTED edge records no swap at all.
  refresh-entry-site-dropped    A refresh (focus, menu, re-said "show
                                numbers", proactive tick) records no entry
                                pin, so nothing arms after it.
  settle-entry-site-dropped     The post-click settle re-read records no
                                entry pin.
  show-entry-site-dropped       handle_overlay_command records no entry pin,
                                so a re-said "show numbers" -- the one
                                repaint that never reaches
                                _apply_overlay_event -- arms nothing
                                (wh-overlay-slow-uia-stale-badges.4.1.1).
  keeps-only-the-newest-prior   The arming replaces the list instead of
                                appending, so a second swap inside the grace
                                window drops the prior the user actually read
                                (wh-overlay-slow-uia-stale-badges.4.1.2).
  checks-only-the-first-prior   The check compares badge N against the first
                                prior alone, so a list that was only on
                                screen in the MIDDLE of a chain of repaints
                                is never compared
                                (wh-overlay-slow-uia-stale-badges.4.2.1).
  expiry-uses-the-oldest-swap   Every entry expires on the OLDEST entry's
                                timestamp, so a swap that just happened
                                loses its protection as soon as the first
                                swap of the chain ages out
                                (wh-overlay-slow-uia-stale-badges.4.2.1).
  arm-on-restore                The pin comparison is inverted: a restore
                                arms and a swap does not. This is the
                                same-snapshot exclusion, from both sides.
  proactive-gate-restored       The old behaviour: arming reads
                                _overlay_refresh_started_proactive again, so
                                only the browser timer's refresh arms.
  restore-disarms               The old failed-refresh leg is back: a restore
                                assigns None to the guard slot.
  closed-reset-keeps-entry-pin  A session close leaves a stranded entry pin
                                behind, so the first paint of the NEXT
                                session arms against a list from the closed
                                one.
  renumber-refuses-everything   _overlay_renumber_click_safe refuses every
                                number. A guard that refuses everything
                                satisfies every refusal test on its own; this
                                is what makes the pair discriminate.

Nine must be caught. Three are declared expected survivors with the key
"survives" and a reason (wh-full-refactor.3.1): keeps-only-the-newest-prior,
restore-disarms and closed-reset-keeps-entry-pin. The old tests that caught
them need an order inside the 3 s grace window that no boundary test
produces, and the stage deleted them. An expected
survivor passes only when its whole selection stays green; any failed test
prints "CAUGHT EXPECTED SURVIVOR" and the gate exits 1. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a per-mutation timeout, a suite-timeout abort, a
skipped catcher and a missing expected test name are reported as errors,
never as a verdict. ``--check`` verifies every pattern matches exactly once
in the current source and that every mutant compiles, without running a test,
and refuses an expected survivor whose reason is blank.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "numbered_overlay" / "controller.py"

INTEGRATION = "tests/test_logic_overlay_integration.py"
JOURNEYS = "tests/test_overlay_journeys.py"
# wh-overlay-slow-uia-stale-badges.4.2.1: the multi-prior decision is proven
# by routing tests that drive _overlay_renumber_click_safe directly, so this
# file is part of the collected-name set as well as a selection.
ROUTING = "tests/test_voice_overlay_routing.py"
ALL_FILES = [INTEGRATION, JOURNEYS, ROUTING]
# wh-full-refactor.2: phase B of the overlay pilot (wh-overlay-refactor-pilot.2)
# replaced the old re-said "show numbers" test with a boundary test in this
# file. It is collected and run in the baseline beside ALL_FILES, and only
# the mutation that names it selects it.
CLICK = "tests/test_overlay_requirements_click.py"
COLLECTED = ALL_FILES + [CLICK]

# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# keeps the FAILED summary lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rf"]
PER_MUTATION_TIMEOUT_S = 300

# --- source under mutation, quoted exactly -------------------------------
_ARM_IF = (
    "            if entry_pin is not None and "
    "machine.pinned_snapshot_id != entry_pin:\n"
)
_ARM_BODY = (
    "                now = self._overlay_now_monotonic()\n"
    "                armed = list(\n"
    '                    getattr(self, "_overlay_repaint_swaps", None) or ()\n'
    "                )\n"
    "                self._overlay_repaint_swaps = [\n"
    "                    entry for entry in armed\n"
    "                    if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n"
    "                ] + [(entry_pin, now)]\n"
)
# The per-entry expiry inside the CHECK, at its own indentation (the arming
# above has the same test at four more spaces, so the two never collide).
_LIVE_FILTER = (
    "            entry for entry in swaps\n"
    "            if now - entry[1] <= _OVERLAY_RENUMBER_GRACE_SECONDS\n"
)
_CHECK_LOOP = "        for prior_id, _swap_t in live:\n"
# The comment block between _ARM_IF and _ARM_BODY is left out of both
# patterns on purpose: quoting it would make every mutation here stale the
# next time a sentence in it is reworded.
_SHOW_ENTRY_CALL = (
    "        # the session still clears it.\n"
    "        self._reconcile_overlay_repaint_entry_pin(prev_state)\n"
)
_ENTRY_PIN_STATES = (
    "            in (OverlayState.REFRESH_IN_FLIGHT, "
    "OverlayState.POST_CLICK_SETTLING)\n"
)
_CLOSED_RESET = (
    "            self._overlay_repaint_swaps = []\n"
    "            self._overlay_repaint_entry_pin = None\n"
)
_SAFE_CALL = (
    "            if not renumber_click_is_safe(prior, current, "
    "parsed_number):\n"
)

# --- catcher names -------------------------------------------------------
JOURNEY_REFUSES = "test_journey_2_a_repaint_mid_sentence_refuses_the_spoken_number"
JOURNEY_UNCHANGED = (
    "test_journey_2_an_unchanged_badge_stays_clickable_across_that_repaint"
)
SHOW_ARMS = "test_ck_34_click_n_after_a_renumbering_repaint_is_refused"
# wh-full-refactor.3.1: the boundary tests that replace the deleted catchers.
# CK-57 is the same code change as checks-only-the-first-prior and
# expiry-uses-the-oldest-swap; CK-59 is the same code change as
# refresh-entry-site-dropped, settle-entry-site-dropped and arm-on-restore.
CHAIN_REFUSED = "test_ck_57_renumbering_anywhere_in_a_repaint_chain_is_refused"
FOCUS_OR_SETTLE_CHECKED = (
    "test_ck_59_click_n_after_a_focus_or_settle_repaint_is_checked"
)

MUTATIONS = [
    {
        "name": "arm-dropped",
        "old": _ARM_BODY,
        # ``pass``, not a deleted line: deleting the only statement in the
        # block leaves an empty body, and a mutant that cannot compile reads
        # as caught while proving nothing.
        "new": "                pass\n",
        "selection": ALL_FILES + [CLICK],
        "expect": [JOURNEY_REFUSES, FOCUS_OR_SETTLE_CHECKED],
    },
    {
        "name": "refresh-entry-site-dropped",
        "old": _ENTRY_PIN_STATES,
        "new": "            in (OverlayState.POST_CLICK_SETTLING,)\n",
        "selection": ALL_FILES + [CLICK],
        "expect": [JOURNEY_REFUSES, FOCUS_OR_SETTLE_CHECKED],
    },
    {
        "name": "settle-entry-site-dropped",
        "old": _ENTRY_PIN_STATES,
        "new": "            in (OverlayState.REFRESH_IN_FLIGHT,)\n",
        "selection": [INTEGRATION, CLICK],
        "expect": [FOCUS_OR_SETTLE_CHECKED],
    },
    {
        # wh-overlay-slow-uia-stale-badges.4.1.1. The other apply path.
        # handle_overlay_command moves the machine itself and never calls
        # _apply_overlay_event, so the shared helper has to be called from
        # here too or a re-said "show numbers" records nothing.
        "name": "show-entry-site-dropped",
        "old": _SHOW_ENTRY_CALL,
        # Deleting the call is safe here: it is one statement among many in
        # this function, not the only statement in a block.
        "new": "        # the session still clears it.\n",
        "selection": [INTEGRATION, CLICK],
        "expect": [SHOW_ARMS],
    },
    {
        # wh-overlay-slow-uia-stale-badges.4.1.2. Replace instead of append,
        # which is what the code did before that finding: the second swap
        # inside the grace window then drops the prior the user read.
        "name": "keeps-only-the-newest-prior",
        "old": _ARM_BODY,
        "new": (
            "                now = self._overlay_now_monotonic()\n"
            "                self._overlay_repaint_swaps = "
            "[(entry_pin, now)]\n"
        ),
        "selection": [INTEGRATION, ROUTING, CLICK],
        "expect": [],
        "survives": (
            "Needs two repaint swaps and a click N inside one 3 s grace window"
        ),
    },
    {
        # wh-overlay-slow-uia-stale-badges.4.2.1. Keep the whole chain but
        # compare badge N against the first prior only, so a list that was
        # on screen only in the MIDDLE of the chain is never compared.
        "name": "checks-only-the-first-prior",
        "old": _CHECK_LOOP,
        "new": "        for prior_id, _swap_t in live[:1]:\n",
        "selection": [ROUTING, CLICK],
        "expect": [CHAIN_REFUSED],
    },
    {
        # wh-overlay-slow-uia-stale-badges.4.2.1. Expire every entry on the
        # OLDEST entry's timestamp, so the swap that just happened loses its
        # protection as soon as the first swap of the chain ages out.
        "name": "expiry-uses-the-oldest-swap",
        "old": _LIVE_FILTER,
        "new": (
            "            entry for entry in swaps\n"
            "            if now - swaps[0][1] <= "
            "_OVERLAY_RENUMBER_GRACE_SECONDS\n"
        ),
        "selection": [ROUTING, CLICK],
        "expect": [CHAIN_REFUSED],
    },
    {
        "name": "arm-on-restore",
        "old": _ARM_IF,
        "new": (
            "            if entry_pin is not None and "
            "machine.pinned_snapshot_id == entry_pin:\n"
        ),
        "selection": ALL_FILES + [CLICK],
        # Both sides of the same-snapshot exclusion: the restores now arm,
        # and the swaps stop arming.
        "expect": [JOURNEY_REFUSES, FOCUS_OR_SETTLE_CHECKED],
    },
    {
        "name": "proactive-gate-restored",
        "old": _ARM_IF,
        "new": (
            "            if (\n"
            "                entry_pin is not None\n"
            "                and machine.pinned_snapshot_id != entry_pin\n"
            "                and getattr(\n"
            '                    self, "_overlay_refresh_started_proactive",'
            " False,\n"
            "                )\n"
            "            ):\n"
        ),
        "selection": ALL_FILES + [CLICK],
        # The proactive tests stay green under this mutant on purpose: it IS
        # the old behaviour, and the whole bead is that the old behaviour
        # protected browser windows and nothing else.
        "expect": [JOURNEY_REFUSES, FOCUS_OR_SETTLE_CHECKED],
    },
    {
        "name": "restore-disarms",
        "old": _ARM_BODY,
        "new": _ARM_BODY + (
            "            else:\n"
            "                self._overlay_repaint_swaps = []\n"
        ),
        "selection": [INTEGRATION, CLICK],
        "expect": [],
        "survives": (
            "Needs a repaint swap, then a failed refresh, then click N, all "
            "inside the 3 s grace window"
        ),
    },
    {
        "name": "closed-reset-keeps-entry-pin",
        "old": _CLOSED_RESET,
        "new": "            self._overlay_repaint_swaps = []\n",
        "selection": [INTEGRATION, CLICK],
        "expect": [],
        "survives": (
            "Needs hide numbers while a refresh walk is outstanding, then "
            "show numbers and a click N inside 3 s of the new session's "
            "first paint"
        ),
    },
    {
        "name": "renumber-refuses-everything",
        "old": _SAFE_CALL,
        "new": "            if True:\n",
        "selection": [JOURNEYS],
        "expect": [JOURNEY_UNCHANGED],
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
    out = _pytest(*COLLECTED, "--collect-only", "-q", "-p", "no:randomly")
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


def _declaration_problems(mutations):
    """Name each mutation whose expected-survivor declaration has no reason.

    The same rule as the shared runner (stt_providers/shared/tests/
    mutation_gate_runner.py): a declaration without a reason hides a
    survivor and says nothing about why, so it is refused before any file
    is mutated.
    """
    problems = []
    for mut in mutations:
        if "survives" not in mut:
            continue
        reason = mut["survives"]
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{mut['name']}: an expected survivor needs a reason")
    return problems


def _declared(mutations):
    return sum(1 for mut in mutations if "survives" in mut)


def check_only():
    raw = SRC.read_bytes()
    text = raw.decode("utf-8")
    newline = _newline_of(raw)
    stale, non_compiling = [], []
    invalid = _declaration_problems(MUTATIONS)
    for problem in invalid:
        print("ERROR", problem)
    for mut in MUTATIONS:
        mutant, error = _build_mutant(text, newline, mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    line = (
        f"checked {len(MUTATIONS)} patterns, {len(stale)} stale, "
        f"{len(non_compiling)} that do not compile"
    )
    # The line keeps its old form when no mutation declares a survivor.
    if _declared(MUTATIONS):
        line += f", {_declared(MUTATIONS)} expected survivors"
    print(line)
    return 1 if stale or non_compiling or invalid else 0


def _restore(original: bytes):
    """Put the source back, holding a second Ctrl+C until the cache is clear.

    A mutant left in a tracked file is the whole harm this gate can do: the
    next run, and every other session sharing the checkout, would read it as
    real code. A same-size mutant also leaves current-looking bytecode, so
    the cache clear has to happen before any held interrupt is re-raised.
    """
    held = None
    for _ in range(2):
        try:
            SRC.write_bytes(original)
            break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR restore failed: {exc}")
            break
    _clear_bytecode()
    if held is not None:
        raise held


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
    invalid = _declaration_problems(mutations)
    if invalid:
        for problem in invalid:
            print("ERROR", problem)
        return 1

    original = SRC.read_bytes()
    text = original.decode("utf-8")
    newline = _newline_of(original)
    errors, caught, survived = [], [], []
    expected, expected_caught = [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {COLLECTED}")
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
    baseline = _pytest(*COLLECTED, *PYTEST_ARGS)
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
        _clear_bytecode()
        SRC.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*mut["selection"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            _restore(original)
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
        if "survives" in mut:
            # An expected survivor must leave its whole selection green. A
            # failed test, named or not, catches the mutation, so the
            # reason is no longer true and the declaration must go.
            if result.returncode == 0:
                expected.append(mut["name"])
                print(f"expected survivor {mut['name']}: {mut['survives']}")
            elif got:
                expected_caught.append(mut["name"])
                print(
                    f"CAUGHT EXPECTED SURVIVOR {mut['name']} by {sorted(got)}"
                )
            else:
                errors.append(
                    f"{mut['name']}: exit {result.returncode} with no FAILED line"
                )
                print("ERROR", errors[-1])
            continue
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
    line = f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}"
    # The line keeps its old form when no selected mutation declares a
    # survivor, so a run of an older table prints what it always printed.
    if _declared(mutations):
        line += (
            f", {len(expected)} expected survivors, "
            f"{len(expected_caught)} expected survivors caught"
        )
    print(line)
    return 1 if survived or errors or expected_caught else 0


if __name__ == "__main__":
    sys.exit(main())
