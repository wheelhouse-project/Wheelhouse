"""Mutation gate for the nested non-capturing group in the first-word index
(wh-grid-click-nested-group).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_nested_group.py
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_nested_group.py --check
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_nested_group.py --only <name>
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_nested_group.py --only=<name>

It proves that tests/test_pattern_catalog_nested_group.py catches a defect in
each half of David's "option one": the extractor repair in
``PatternCatalog._extract_first_words`` (speech/pattern_catalog.py) AND the
rewritten shipped pattern (speech/config/patterns.toml). Both halves are here
on purpose. Option one exists so that neither repair can hide a regression in
the other, and a gate that mutated only the Python file could not show that.

  the-nested-prefix-is-not-stripped
        The elif that strips a non-capturing prefix one group deep is
        disabled, which restores the shipped defect exactly: the
        first-close-paren stop in ``\\(([^)]+)\\)`` leaves '(?:click|tap',
        the '?:' test above does not fire on '(?:', and the first
        alternative is dropped from the first-word index.
  the-nested-strip-leaves-the-colon
        The strip takes two characters instead of three, leaving
        ':click|tap', so the first alternative is indexed wrong rather
        than lost. An off-by-one that a "does it crash" test would miss.
  the-nested-strip-eats-the-first-letter
        The strip takes four characters, leaving 'lick|tap'. The other
        direction of the same off-by-one.
  the-bare-non-capturing-prefix-is-not-stripped
        The ORIGINAL '?:' strip beside the new elif stops working. It is
        here because the new branch sits directly against it: a later
        edit that merges or reorders the two must not silently drop the
        shape that always worked.
  the-shipped-pattern-returns-to-the-nested-shape
        patterns.toml goes back to '^((?:click|tap)[.!?]?)$'. With the
        extractor repaired this pattern still WORKS, which is the whole
        point of criterion 2: only a test that reads the toml can catch
        it, and this mutation proves such a test exists. Without it, the
        two halves of the fix would mask each other.

All five must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a per-mutation timeout, a suite-timeout abort, and an
expected catcher that SKIPPED are reported as errors, never as a verdict.
``--check`` verifies every pattern matches exactly once in the current source
and that every Python mutant compiles, without running any test. The toml
mutation is skipped by the compile check, because it is not Python.

Expected-catcher names keep their pytest parameter id
(``name[bare-non-capturing]``), so a parametrized case is named exactly; the
collected-name check reads the same form from ``--collect-only``. The three
parametrized cases carry explicit hyphenated ids for that reason -- a
generated id would hold a regex containing a space, and the name reader
truncates at the first space.
"""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "pattern_catalog.py"
TOML = SERVICE / "speech" / "config" / "patterns.toml"
TEST_FILE = "tests/test_pattern_catalog_nested_group.py"
CLASSES = ("TestNestedNonCapturingGroup", "TestShippedGridClickPattern")

# -v prints one line per test, which is what names a SKIPPED catcher; -rfE
# keeps the FAILED and ERROR summary lines the readers below parse.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 180

EXTRACTOR = "test_the_extractor_keeps_every_alternative_of_the_nested_shape"
INDEXES = "test_a_pattern_of_the_nested_shape_indexes_both_words"
REACHES = "test_both_words_reach_the_pattern_through_the_index"
UNNESTED = "test_the_shapes_that_already_worked_still_work"
NOT_NESTED = "test_the_shipped_grid_pattern_is_not_written_in_the_nested_shape"

# The nested-shape catchers. All three drive the same extractor branch from
# a different height: the function directly, the index it builds, and the
# index lookup the router actually performs.
NESTED_ALL = [EXTRACTOR, INDEXES, REACHES]

# The new elif, with the comment block that explains it left out of the
# match so a later wording change cannot make the pattern stale.
_NESTED_ELIF = (
    "            elif content.startswith('(?:'):\n"
    "                content = content[3:]\n"
)
_BARE_IF = (
    "            if content.startswith('?:'):\n"
    "                content = content[2:]\n"
)

MUTATIONS = [
    {
        # Criterion 4: this is the mutation that restores the
        # first-close-paren stop's consequence.
        "name": "the-nested-prefix-is-not-stripped",
        "old": _NESTED_ELIF,
        "new": (
            "            elif False:\n"
            "                content = content[3:]\n"
        ),
        "expect": NESTED_ALL,
    },
    {
        "name": "the-nested-strip-leaves-the-colon",
        "old": _NESTED_ELIF,
        "new": (
            "            elif content.startswith('(?:'):\n"
            "                content = content[2:]\n"
        ),
        "expect": NESTED_ALL,
    },
    {
        "name": "the-nested-strip-eats-the-first-letter",
        "old": _NESTED_ELIF,
        "new": (
            "            elif content.startswith('(?:'):\n"
            "                content = content[4:]\n"
        ),
        "expect": NESTED_ALL,
    },
    {
        "name": "the-bare-non-capturing-prefix-is-not-stripped",
        "old": _BARE_IF,
        "new": (
            "            if False:\n"
            "                content = content[2:]\n"
        ),
        "expect": [f"{UNNESTED}[bare-non-capturing]"],
    },
    {
        "name": "the-shipped-pattern-returns-to-the-nested-shape",
        "file": TOML,
        "old": "pattern = '''^((click|tap)[.!?]?)$'''\ndoc_id = \"grid-click\"\n",
        "new": "pattern = '''^((?:click|tap)[.!?]?)$'''\ndoc_id = \"grid-click\"\n",
        "expect": [NOT_NESTED],
    },
]


def _target(mut):
    return mut.get("file", SRC)


def _clear_bytecode():
    for cache in SERVICE.glob("speech/__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _test_id(line: str) -> str:
    """The ``name[param]`` part after the last ``::`` on a pytest line."""
    return line.split("::")[-1].strip().split(" ")[0]


def collect_names():
    """Real test ids in CLASSES, so a renamed test cannot read as a survivor."""
    selected = [f"{TEST_FILE}::{name}" for name in CLASSES]
    out = _pytest(*selected, "--collect-only", "-q", "-p", "no:randomly")
    return {_test_id(line) for line in out.stdout.splitlines() if "::" in line}


def failed_names(output):
    return {
        _test_id(line)
        for line in output.splitlines()
        if line.startswith("FAILED ")
    }


def error_names(output):
    """Test ids on ERROR records: the -v per-test lines and the -rE summary.

    pytest reports an ERROR, not a FAILED, when a fixture raises or a test
    errors rather than fails, and a collection error prints only the
    summary form. Either would otherwise be read as a verdict.
    """
    return {
        _test_id(line)
        for line in output.splitlines()
        if line.startswith("ERROR ") or ("::" in line and " ERROR" in line)
    }


def run_defect(result):
    """A reason this run cannot yield a verdict, or None."""
    if "+++ Timeout +++" in result.stdout:
        return "suite-timeout-abort"
    if result.returncode not in (0, 1):
        return f"unexpected pytest exit status {result.returncode}"
    errored = sorted(error_names(result.stdout))
    if errored:
        return f"pytest reported ERROR records: {errored}"
    if result.stderr.strip():
        first = result.stderr.strip().splitlines()[0]
        return f"pytest wrote to stderr: {first!r}"
    return None


def skipped_names(output):
    """Test ids on -v per-test lines that read ``...::name SKIPPED (...)``."""
    return {
        _test_id(line)
        for line in output.splitlines()
        if "::" in line and " SKIPPED" in line
    }


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(mut):
    """Return (mutant_bytes, original_bytes, error) for one mutation.

    Reads the target itself, so a mutation against patterns.toml is built
    from the toml and a mutation against pattern_catalog.py from the
    Python file. The line ending is detected per file: this repository
    mixes conventions, and an LF pattern matches nothing in a CRLF file.
    """
    path = _target(mut)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    newline = _newline_of(raw)
    old = mut["old"].replace("\n", newline)
    new = mut["new"].replace("\n", newline)
    count = text.count(old)
    if count != 1:
        return None, raw, (
            f"{mut['name']}: pattern matched {count} times in {path.name}, "
            "expected 1"
        )
    mutated = text.replace(old, new, 1)
    if path.suffix == ".py":
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as exc:
            return None, raw, (
                f"{mut['name']}: mutated source does not compile: {exc}"
            )
    elif path.suffix == ".toml":
        # A malformed toml mutant is the toml half of the does-not-compile
        # false pass. Written to disk it makes the catalog fail to LOAD, so
        # the expected catcher fails for a reason that has nothing to do
        # with the behaviour it pins, and the mutation is scored as caught
        # (wh-grid-click-nested-group.2.2).
        try:
            tomllib.loads(mutated)
        except tomllib.TOMLDecodeError as exc:
            return None, raw, (
                f"{mut['name']}: mutated toml does not parse: {exc}"
            )
    return mutated.encode("utf-8"), raw, None


def _restore(path: Path, original: bytes) -> bool:
    """Put the file back, and say whether the original bytes are back.

    Returns True only when the file now holds ``original``. A False is a
    hard condition for the caller, not a warning: a run whose target is
    still mutated cannot yield a verdict, and the mutant is sitting in a
    TRACKED file that every later run and every other session sharing the
    checkout will read as real code (wh-grid-click-nested-group.2.3).

    write_bytes, never write_text: on Windows write_text rewrites every
    line ending as CRLF, which silently breaks every multi-line pattern
    in this file on the next run. A second Ctrl+C landing inside the
    restore is held, retried once, and re-raised only after the bytecode
    caches are cleared -- a same-size mutant otherwise leaves
    current-looking bytecode for the next run to import.
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
    _clear_bytecode()
    if held is not None:
        raise held
    return restored


def _apply_and_run(path: Path, mutant: bytes, original: bytes):
    """Write the mutant, run the suite, restore. Return (result, error).

    The write is INSIDE the guarded block on purpose. When it sat outside,
    an interrupt or an OSError landing during the write left the tracked
    file mutated with nothing to put it back. A restore that fails clears
    ``result`` as well as setting an error, so no caller can read a
    verdict out of a run whose source file is still mutated
    (wh-grid-click-nested-group.2.3).
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        path.write_bytes(mutant)
        result = _pytest(TEST_FILE, *PYTEST_ARGS)
    except subprocess.TimeoutExpired:
        error = "timed out"
    except OSError as exc:
        error = f"could not write the mutant to {path.name}: {exc}"
    finally:
        if not _restore(path, original):
            error = f"the original bytes of {path.name} were not restored"
            result = None
    return result, error


def check_only(mutations=None):
    """Verify each selected pattern without running any test.

    This is NOT a sweep. It proves every pattern matches exactly once in
    the current source and that every mutant it can build is valid; it
    cannot see a masked survivor.
    """
    if mutations is None:
        mutations = MUTATIONS
    stale, non_compiling, non_parsing = [], [], []
    for mut in mutations:
        _mutant, _original, error = _build_mutant(mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        elif "does not parse" in error:
            non_parsing.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    print(
        f"checked {len(mutations)} of {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(non_compiling)} that do not compile, "
        f"{len(non_parsing)} that do not parse"
    )
    return 1 if stale or non_compiling or non_parsing else 0


def _selected():
    """The mutations this run covers, honouring --only <name> and --only=<name>.

    Both spellings are accepted, and an empty selection is an ERROR rather
    than a green run of nothing. Every mutation here rewrites
    pattern_catalog.py or patterns.toml on disk, so the operator has to be
    able to trust the printed scope: a command that says one mutation and
    runs five opens four more windows in which a concurrent test run reads
    a half-mutated file (wh-grid-click-nested-group.1.2).
    """
    argv = sys.argv[1:]
    if not any(a == "--only" or a.startswith("--only=") for a in argv):
        return MUTATIONS
    wanted = set()
    for i, arg in enumerate(argv):
        if arg.startswith("--only="):
            value = arg[len("--only="):]
            if value:
                wanted.add(value)
        elif arg == "--only":
            # Everything up to the next flag. Stopping at a flag keeps a
            # later option from being read as a mutation name.
            for follower in argv[i + 1:]:
                if follower.startswith("--"):
                    break
                wanted.add(follower)
    if not wanted:
        print(
            "ERROR --only needs a mutation name: "
            "--only <name> or --only=<name>"
        )
        return None
    unknown = wanted - {m["name"] for m in MUTATIONS}
    if unknown:
        print(f"ERROR --only names no such mutation: {sorted(unknown)}")
        return None
    return [m for m in MUTATIONS if m["name"] in wanted]


def main():
    # The selection is resolved BEFORE the --check branch. When --check
    # returned first, `--check --only=<name>` validated all five patterns
    # while the operator had asked about one, and a --check with an unknown
    # or empty --only exited 0 (wh-grid-click-nested-group.2.4).
    selected = _selected()
    if selected is None:
        return 1
    if "--check" in sys.argv[1:]:
        return check_only(selected)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test ids in {' + '.join(CLASSES)}")
    for mut in selected:
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
    baseline = _pytest(TEST_FILE, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    expected_all = {name for mut in selected for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(
            f"ERROR expected catchers skipped in the baseline: "
            f"{skipped_catchers}"
        )
        print(
            "      a skipped catcher can never fail, so its mutation cannot "
            "be proven"
        )
        return 1
    print("baseline green")

    for mut in selected:
        mutant, original, error = _build_mutant(mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        assert mutant is not None
        path = _target(mut)
        result, error = _apply_and_run(path, mutant, original)
        if error is not None:
            errors.append(f"{mut['name']}: {error}")
            print("ERROR", errors[-1])
            if "not restored" in error:
                # A tracked source file still holds the mutant. Every later
                # mutation would run against it, and so would any other
                # session sharing the checkout, so the sweep stops here
                # instead of piling verdicts on top of a corrupt tree.
                print("ERROR stopping the sweep; restore the file by hand")
                break
            continue
        assert result is not None
        defect = run_defect(result)
        if defect is not None:
            errors.append(f"{mut['name']}: {defect}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        skipped = [
            n for n in mut["expect"] if n in skipped_names(result.stdout)
        ]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
            print("ERROR", errors[-1])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
        else:
            caught.append(mut["name"])
            print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    # Reached, not len(selected): a restore failure breaks the sweep, and
    # "none skipped" would then be a false line in the run's own summary.
    reached = len(caught) + len(survived) + len(errors)
    print(
        f"scope: {reached} of {len(selected)} selected mutations run, "
        f"{len(MUTATIONS)} in the full set"
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
