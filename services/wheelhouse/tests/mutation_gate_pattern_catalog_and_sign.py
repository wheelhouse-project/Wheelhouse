r"""Mutation gate for the alternation-expanding first-word index
(wh-and-sign-index-defect).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_and_sign.py
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_and_sign.py --check
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_and_sign.py --only <name>
    .venv/Scripts/python.exe tests/mutation_gate_pattern_catalog_and_sign.py --only=<name>

It is the sibling of tests/mutation_gate_pattern_catalog_nested_group.py,
which guards the same function against the grid-click shape. This one guards
the repair that reads a pattern's alternatives through balanced parentheses:
``PatternCatalog._extract_first_words`` now expands the pattern with
``pattern_transform._expand_sequence`` before either of the two older regex
branches runs. It also carries two mutations against the shipped ampersand
entry in speech/config/patterns.toml, so a catalog-level test that never
reaches an insertion cannot pass for the wrong reason.

  the-expander-is-not-consulted
        ``_expand_sequence`` is never called, so the function falls back to
        the two regex branches and the original defect returns exactly: the
        optional-prefix branch fires on ``\b(?:ampersand|and sign(?:ed)?)\b``
        and indexes only "ampersand".
  the-expander-reads-only-the-first-variant
        The loop over the expanded variants stops after one, so the second
        and third alternatives are dropped from the index. The extractor
        still runs and still returns a list, so nothing crashes; only the
        alternatives beyond the first go missing.
  the-expander-keeps-the-case-it-was-given
        The expanded word is indexed with its capital. Every lookup goes
        through ``_normalize_lookup_word``, which lowercases, so such an
        entry can never be found again.
  the-shipped-pattern-inserts-the-wrong-text
        The punct-ampersand action inserts "and" instead of "&". Only a
        test that reads the EMITTED ACTION can catch this; a test that
        checked index membership alone would stay green. That is the whole
        reason acceptance A2 asks for the pipeline level as well as the
        catalog level.
  the-shipped-pattern-takes-the-aliases-back
        patterns.toml carries ``\b(?:ampersand|and sign(?:ed)?)\b`` again.
        This is the regression David's answer to QUESTIONS-2026-09-05 item
        94 removed, and with the extractor repaired it is worse than it was
        before: the aliases now reach the index, so "he read and signed the
        form" loses a word. The mutation exists because the decision to
        drop the aliases is pinned by tests, and a pin nobody has seen fail
        is not a pin.

All five must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a toml mutant that does not parse, a per-mutation
timeout, a suite-timeout abort, and an expected catcher that SKIPPED are
reported as errors, never as a verdict. ``--check`` verifies every pattern
matches exactly once in the current source and that every Python mutant
compiles and every toml mutant parses, without running any test. ``--check``
is NOT a sweep and cannot see a masked survivor.

The selection covers five classes across two files, so the run includes the
two ordinary-dictation tests that must NOT change. They are deliberately
absent from every ``expect`` list: they are the negative control for the
whole change, they pass with and without the repair, and naming them as a
catcher would turn that into a false verdict. The four-sentence case in the
same class is NOT one of those two -- it IS a catcher, for the aliases-back
mutation, because those four sentences measurably lost a word while the
aliases were indexed.

Expected-catcher names keep their pytest parameter id
(``name[and-sign]``), so a parametrized case is named exactly; the
collected-name check reads the same form from ``--collect-only``. Every
parametrized case in both test files carries an explicit hyphenated id for
that reason -- a generated id would hold a regex containing a space, and the
name reader truncates at the first space.
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

CATALOG_TESTS = "tests/test_pattern_catalog_and_sign.py"
PIPELINE_TESTS = "tests/test_pipeline_and_sign.py"

# Class-level targets rather than whole files: the same form the sibling
# gate uses, so a test added to either file outside these classes cannot
# quietly join the selection.
TEST_TARGETS = (
    f"{CATALOG_TESTS}::TestTheExtractorReadsEveryAlternative",
    f"{CATALOG_TESTS}::TestTheShippedAmpersandPattern",
    f"{PIPELINE_TESTS}::TestTheSpokenNameInsertsTheMark",
    f"{PIPELINE_TESTS}::TestTheRemovedAliasesTypeTheirWords",
    f"{PIPELINE_TESTS}::TestOrdinaryDictationKeepsItsWords",
)

# -v prints one line per test, which is what names a SKIPPED catcher; -rfE
# keeps the FAILED and ERROR summary lines the readers below parse.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 180

SHAPE_INDEX = "test_the_ampersand_shape_indexes_both_first_words"
SYNTHETIC_INDEX = "test_the_synthetic_shape_indexes_both_first_words"
ALSO_FIXED = "test_the_shapes_the_repair_also_fixes"
LOWERCASES = "test_the_expander_lowercases_what_it_indexes"
ONLY_NAME = "test_the_shipped_entry_carries_only_the_spoken_name"
REACHES = "test_the_spoken_name_reaches_the_ampersand_pattern"
AND_REACHES_NOTHING = "test_the_word_and_reaches_no_pattern_that_inserts_the_mark"
INSERTS = "test_the_spoken_name_inserts_exactly_the_mark"
MID_SENTENCE = "test_the_spoken_name_still_inserts_the_mark_mid_sentence"
TYPES_ITS_WORDS = "test_the_removed_alias_types_its_words"
OLD_ALIAS_SENTENCE = "test_a_sentence_containing_the_old_alias_types_every_word"

# The catchers for the extractor repair itself. They are all at the
# extractor level now: item 94 narrowed the shipped entry to a pattern with
# no alternation, so the shipped index no longer depends on this repair and
# a catalog- or pipeline-level test cannot speak for it. The shape those two
# tests use is a fixture, which is exactly why it still can.
SHAPE_CATCHERS = [
    SHAPE_INDEX,
    SYNTHETIC_INDEX,
]

# The expander call and its guard, with the long comment block above them
# left out of the match so a later wording change cannot make the pattern
# stale.
_EXPANDER_CALL = (
    "        expanded = _expand_sequence(cleaned)\n"
    "        if expanded is not None:\n"
)
_VARIANT_LOOP = "            for variant in expanded:\n"

# The whole shipped entry, pattern line included. Including the pattern is
# deliberate: a rewrite of that line should make these mutations report
# themselves stale rather than mutate a pattern the tests no longer
# describe. The comment block above the entry in patterns.toml is left out,
# so a later wording change there cannot make them stale for no reason.
_SHIPPED_PATTERN_LINE = "pattern = '''\\bampersand\\b'''\n"
_ALIAS_PATTERN_LINE = (
    "pattern = '''\\b(?:ampersand|and sign(?:ed)?)\\b'''\n"
)
_AMPERSAND_TOML = (
    _SHIPPED_PATTERN_LINE
    + 'doc_id = "punct-ampersand"\n'
    + "actions = [\n"
    + '    { function = "text", params = ["&"] }\n'
    + "]\n"
)

MUTATIONS = [
    {
        "name": "the-expander-is-not-consulted",
        "old": _EXPANDER_CALL,
        "new": (
            "        expanded = None\n"
            "        if expanded is not None:\n"
        ),
        "expect": SHAPE_CATCHERS + [
            f"{ALSO_FIXED}[optional-prefix-with-optional-tail]",
            f"{ALSO_FIXED}[three-alternatives]",
        ],
    },
    {
        "name": "the-expander-reads-only-the-first-variant",
        "old": _VARIANT_LOOP,
        "new": "            for variant in expanded[:1]:\n",
        "expect": SHAPE_CATCHERS,
    },
    {
        # Reported as a coverage gap by the reviewer_0 round, not as a
        # finding, and measured before this entry was written: with the
        # call deleted every class in TEST_TARGETS stayed green
        # while '^Windows? settings$' indexed ['Window', 'Windows']. Every
        # lookup goes through _normalize_lookup_word, which lowercases, so
        # those entries can never be found and the shipped command stops
        # answering. The catchers are the two cases added for it.
        "name": "the-expander-keeps-the-case-it-was-given",
        "old": "                    lowered = word.lower()\n",
        "new": "                    lowered = word\n",
        "expect": [
            f"{LOWERCASES}[shipped-windows-settings]",
            f"{LOWERCASES}[capitalised-alternation]",
        ],
    },
    {
        "name": "the-shipped-pattern-inserts-the-wrong-text",
        "file": TOML,
        "old": _AMPERSAND_TOML,
        "new": _AMPERSAND_TOML.replace('["&"]', '["and"]'),
        "expect": [
            INSERTS,
            MID_SENTENCE,
            REACHES,
        ],
    },
    {
        # The pin for David's answer to QUESTIONS-2026-09-05 item 94. The
        # aliases come back, and with the extractor repaired they reach the
        # first-word index, which is the state measured on this branch:
        # "he read and signed the form" typed "he read & the form". A
        # decision recorded only in prose is not protected; this says the
        # tests that record it can actually fail.
        "name": "the-shipped-pattern-takes-the-aliases-back",
        "file": TOML,
        "old": _SHIPPED_PATTERN_LINE,
        "new": _ALIAS_PATTERN_LINE,
        "expect": [
            ONLY_NAME,
            AND_REACHES_NOTHING,
            f"{TYPES_ITS_WORDS}[and-sign]",
            f"{TYPES_ITS_WORDS}[and-signed]",
            f"{OLD_ALIAS_SENTENCE}[checked-and-signed]",
            f"{OLD_ALIAS_SENTENCE}[come-and-sign]",
            f"{OLD_ALIAS_SENTENCE}[read-and-signed]",
            f"{OLD_ALIAS_SENTENCE}[contract-and-sign]",
        ],
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
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            # The pipeline harness builds Qt objects. Without this the run
            # depends on whether a display is attached, which would make
            # the gate's verdict depend on how it was started.
            "QT_QPA_PLATFORM": "offscreen",
        },
    )


def _test_id(line: str) -> str:
    """The ``name[param]`` part after the last ``::`` on a pytest line."""
    return line.split("::")[-1].strip().split(" ")[0]


def collect_names():
    """Real test ids in TEST_TARGETS, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_TARGETS, "--collect-only", "-q", "-p", "no:randomly")
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
        # with the behaviour it pins, and the mutation is scored as caught.
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
    checkout will read as real code.

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

    The write is INSIDE the guarded block on purpose. When it sits outside,
    an interrupt or an OSError landing during the write leaves the tracked
    file mutated with nothing to put it back. A restore that fails clears
    ``result`` as well as setting an error, so no caller can read a
    verdict out of a run whose source file is still mutated.
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        path.write_bytes(mutant)
        result = _pytest(*TEST_TARGETS, *PYTEST_ARGS)
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
    runs three opens two more windows in which a concurrent test run reads
    a half-mutated file.
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
    # The selection is resolved BEFORE the --check branch, so
    # `--check --only=<name>` validates the one pattern the operator asked
    # about and a --check with an unknown or empty --only exits non-zero.
    selected = _selected()
    if selected is None:
        return 1
    if "--check" in sys.argv[1:]:
        return check_only(selected)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test ids in {len(TEST_TARGETS)} classes")
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
    baseline = _pytest(*TEST_TARGETS, *PYTEST_ARGS)
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
