"""Mutation gate for wh-stt-mode-normalize.

The tests in tests/test_stt_mode_normalization.py were written before the
consumers were routed through the helper, and nine of them were seen to
fail. A red-first run proves that once. This proves each test still fails
when the behaviour it claims to pin is broken, and keeps proving it after
later changes.

Run from services/wheelhouse:

    uv run --no-sync python tests/mutation_gate_stt_mode_normalize.py
    uv run --no-sync python tests/mutation_gate_stt_mode_normalize.py --check
    uv run --no-sync python tests/mutation_gate_stt_mode_normalize.py --only warning

Each mutation edits one source file, runs the tests that must fail, and
restores the file byte for byte. A mutation counts as caught only when
every test named in `catchers` appears in that run's failures. Anything
else -- a pattern that matches no times or more than once, a mutation
that does not compile, a run that times out or that pytest's own timeout
aborts, a catcher name that is no longer collected, a red baseline -- is
reported as an ERROR, never as a verdict.

--check applies no mutation: it counts each pattern and compiles each
mutant, which is the cheap way to find a pattern a later fix made stale
and a mutation a later fix stopped from parsing.

This is a separate gate rather than a section of
mutation_gate_remote_stt_robustness.py, although both touch main.py. That
gate covers the settings WRITES and the launch record; this one covers
the mode READ, its tests are a different file, and a full sweep there is
over a hundred and fifty mutations that this work would re-run for
nothing. Every gate in this directory carries its own runner; there is no
shared one to import.

WHAT THIS GATE COVERS NOW. wh-in-process-capture-removal left one speech
engine, so there is no mode to decide and the helper stopped answering:
warn_if_stt_mode_unsupported reads stt.mode and writes one WARNING for a
value the program cannot use. Every mutation below therefore proves the
same kind of thing -- that a settings file naming a mode this build does
not have is still reported to the user, once, with the value quoted -- and
the seven other call sites that used to read the mode are gone rather than
uncovered.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent

CONFIG = SERVICE / "config_service.py"
MAIN = SERVICE / "main.py"

NORM = "tests/test_stt_mode_normalization.py"

RUN_TIMEOUT_S = 300

SOURCES: dict[Path, bytes] = {}


MUTATIONS = [
    # ---- what the helper refuses to accept ------------------------------
    {
        "name": "helper-accepts-any-string",
        "file": CONFIG,
        "old": (
            "    if isinstance(raw, str) and raw == STT_MODE_REMOTE:\n"
        ),
        "new": "    if isinstance(raw, str):\n",
        "selection": [NORM],
        # The return-value catcher went with ruling 6: the helper answers
        # nothing, so widening the accepted set is only visible as a
        # WARNING that never arrives.
        "catchers": [
            "TestTheHelperWarnsOncePerProcess::test_an_unusable_value_is_named_in_one_warning",
        ],
    },
    # ---- what the user is told ------------------------------------------
    {
        "name": "warning-is-suppressed",
        # The whole call becomes `pass`, not nothing: deleting the only
        # statement of a block leaves source that does not parse, and a
        # mutation that cannot compile reads as caught while proving
        # nothing (mutation-gate skill).
        "file": CONFIG,
        "old": """        logger.warning(
            f"The settings file gives {STT_MODE_KEY} as {quoted}, which is "
            f'not a mode this program has. The only mode it accepts is '
            f'"{STT_MODE_REMOTE}", and that is the mode it is running.'
        )
""",
        "new": """        pass
""",
        "selection": [NORM],
        "catchers": [
            "TestTheHelperWarnsOncePerProcess::test_an_unusable_value_is_named_in_one_warning",
        ],
    },
    {
        "name": "warning-omits-the-raw-value",
        "file": CONFIG,
        "old": (
            "            f\"The settings file gives {STT_MODE_KEY} as "
            "{quoted}, which is \"\n"
        ),
        "new": (
            "            f\"The settings file gives {STT_MODE_KEY} as "
            "a value which is \"\n"
        ),
        "selection": [NORM],
        "catchers": [
            "TestTheHelperWarnsOncePerProcess::test_an_unusable_value_is_named_in_one_warning",
        ],
    },
    {
        "name": "warning-repeats-on-every-read",
        "file": CONFIG,
        "old": "    if quoted not in _WARNED_STT_MODES:\n",
        "new": "    if True:\n",
        "selection": [NORM],
        "catchers": [
            "TestTheHelperWarnsOncePerProcess::test_repeated_reads_warn_only_once",
        ],
    },
    {
        "name": "a-missing-key-is-reported",
        "file": CONFIG,
        "old": """    if raw is _STT_MODE_ABSENT:
        return
""",
        "new": """    if False:
        return
""",
        "selection": [NORM],
        "catchers": [
            "TestTheHelperWarnsOncePerProcess::test_a_missing_key_is_silent",
        ],
    },
    # ---- the consumers --------------------------------------------------
    {
        # wh-in-process-capture-removal repaired this entry twice. It used
        # to mutate a two-line pair -- an stt_mode local and the
        # start_websocket flag built from it -- and both lines went with the
        # in-process engine. Then ruling 6 renamed the helper to
        # warn_if_stt_mode_unsupported and took away its return value.
        # Startup still reads the key through the helper, so a raw read in
        # its place is still a real mistake to guard against: it is the one
        # way the WARNING stops being written, which is why the catcher
        # below is the warning test rather than the remote-path test.
        "name": "startup-bypasses-the-helper",
        "file": MAIN,
        "old": "            warn_if_stt_mode_unsupported(self.config_service)\n",
        "new": '            self.config_service.get("stt.mode", "remote")\n',
        "selection": [NORM],
        "catchers": [
            "TestStartupStartsTheRemotePath::test_the_startup_names_the_unusable_mode_in_one_warning",
        ],
    },
]


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _clear_pycache():
    for cache in SERVICE.rglob("__pycache__"):
        for item in cache.glob("*.pyc"):
            try:
                item.unlink()
            except OSError:
                pass


def _run(selection):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "-rf", "-p", "no:randomly"],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=RUN_TIMEOUT_S,
    )


def _failed_names(output):
    names = set()
    for line in output.splitlines():
        if not line.startswith("FAILED "):
            continue
        item = line[len("FAILED "):].split(" ")[0]
        names.add(re.sub(r"\[.*\]$", "", item))
    return names


def _matches(name, failed):
    return any(item.endswith(name) or name in item for item in failed)


def _collect_names(selection):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "--collect-only"],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=RUN_TIMEOUT_S,
    )
    names = set()
    for line in result.stdout.splitlines():
        if "::" not in line:
            continue
        names.add(re.sub(r"\[.*\]$", "", line.strip()))
    return names


def _translate(pattern, newline):
    return pattern.replace("\n", newline) if newline != "\n" else pattern


def _selected(mutations):
    """The mutations named by --only, or all of them."""
    wanted = [
        sys.argv[i + 1]
        for i, item in enumerate(sys.argv)
        if item == "--only" and i + 1 < len(sys.argv)
    ]
    if not wanted:
        return list(mutations), True
    chosen = [m for m in mutations if any(w in m["name"] for w in wanted)]
    return chosen, False


def check() -> int:
    """Count every pattern and compile every mutant; apply nothing.

    A pattern that matches no times or more than once, and a mutant that
    does not parse, are both reported here. The second is the one worth
    the trouble: an unparseable mutant reads as `caught` in a real run,
    because the interpreter rejects it before a test runs.
    """
    stale = 0
    broken = 0
    for mutation in MUTATIONS:
        path = mutation["file"]
        text = path.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        old = _translate(mutation["old"], newline)
        new = _translate(mutation["new"], newline)
        count = text.count(old)
        if count != 1:
            stale += 1
            print(
                f"STALE {mutation['name']}: pattern matched {count} times "
                f"in {path.name}"
            )
            continue
        try:
            compile(text.replace(old, new, 1), str(path), "exec")
        except SyntaxError as e:
            broken += 1
            print(f"BROKEN {mutation['name']}: mutant does not compile ({e})")
    print(
        f"\nchecked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{broken} that do not compile"
    )
    return 0 if not stale and not broken else 1


def main() -> int:
    if "--check" in sys.argv:
        return check()

    _clear_pycache()

    # A run may cover a subset: a mutation already seen to fail for the
    # right reason, whose code and catchers have not changed since, proves
    # nothing on a re-run. Name the ones to run with --only <substring>,
    # repeatable. The scope is printed either way, so a partial run cannot
    # read as a full sweep.
    mutations, is_full = _selected(MUTATIONS)
    if not mutations:
        print("ERROR --only matched no mutation")
        return 1

    # Read once, before anything is written, so the final check compares
    # against the state the gate started from rather than against
    # whatever the last restore happened to write.
    SOURCES.update(
        {path: path.read_bytes() for path in {m["file"] for m in mutations}}
    )

    every_selection = sorted({item for m in mutations for item in m["selection"]})
    if is_full:
        print(f"gate: FULL SWEEP, {len(MUTATIONS)} mutations")
    else:
        print(
            f"gate: PARTIAL RUN, {len(mutations)} of {len(MUTATIONS)} "
            f"mutations; {len(MUTATIONS) - len(mutations)} skipped as "
            "already proven and unchanged"
        )
        for m in mutations:
            print(f"      running: {m['name']}")
    print(f"gate: {len(every_selection)} selections")

    print("baseline: running the unmutated suite")
    baseline = _run(every_selection)
    if baseline.returncode != 0:
        print("ERROR baseline is red; refusing to start")
        print(baseline.stdout[-4000:])
        return 1
    print("baseline: green")

    collected = _collect_names(every_selection)
    missing = [
        (m["name"], c)
        for m in mutations
        for c in m["catchers"]
        if not _matches(c, collected)
    ]
    if missing:
        for name, catcher in missing:
            print(f"ERROR {name}: expected catcher not collected: {catcher}")
        return 1
    print(f"catchers: all {sum(len(m['catchers']) for m in mutations)} names collected")

    caught = []
    survived = []
    errors = []

    for mutation in mutations:
        name = mutation["name"]
        path = mutation["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        old = _translate(mutation["old"], newline)
        new = _translate(mutation["new"], newline)

        count = text.count(old)
        if count != 1:
            errors.append(f"{name}: pattern matched {count} times, expected 1")
            print(f"ERROR {name}: pattern matched {count} times")
            continue

        mutated = text.replace(old, new, 1)
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as e:
            errors.append(f"{name}: mutated source does not compile ({e})")
            print(f"ERROR {name}: mutated source does not compile")
            continue

        path.write_bytes(mutated.encode("utf-8"))
        _clear_pycache()
        try:
            result = _run(mutation["selection"])
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: the mutated run did not finish")
            print(f"ERROR {name}: the mutated run did not finish")
            continue
        finally:
            path.write_bytes(original)
            _clear_pycache()

        if "+++ Timeout +++" in output:
            errors.append(f"{name}: pytest's own timeout aborted the run")
            print(f"ERROR {name}: pytest's own timeout aborted the run")
            continue

        failed = _failed_names(output)
        uncaught = [c for c in mutation["catchers"] if not _matches(c, failed)]
        if uncaught:
            survived.append((name, uncaught))
            print(f"SURVIVED {name}: still passing -> {', '.join(uncaught)}")
        else:
            caught.append(name)
            print(f"caught   {name}")

    unrestored = [
        str(path.relative_to(SERVICE))
        for path, content in SOURCES.items()
        if path.read_bytes() != content
    ]
    for path in unrestored:
        print(f"ERROR {path} was not restored byte for byte")
    print(
        f"\n{len(mutations)} mutations run: {len(caught)} caught, "
        f"{len(survived)} survived, {len(errors)} errors; "
        f"{len(SOURCES) - len(unrestored)} of {len(SOURCES)} sources restored"
    )
    return 0 if (not survived and not errors and not unrestored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
