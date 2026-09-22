"""Mutation gate for the alternation display (wh-erase-synonym-for-delete.1.1).

Run it from services/wheelhouse with the service's own interpreter:

    uv run python tests/mutation_gate_trigger_display_alternatives.py
    uv run python tests/mutation_gate_trigger_display_alternatives.py --check
    uv run python tests/mutation_gate_trigger_display_alternatives.py --only <name>
    uv run python tests/mutation_gate_trigger_display_alternatives.py --only=<name>

The finding's defect: ``PatternManager._TRIGGER_STRIP_RE`` removes a whole
non-capturing group, so widening the 18 delete commands to
``^(?:delete|erase)...`` deleted the command's verb from its display.
Seventeen rows read "word", "all" or "line", the bare row showed its
expression verbatim, and the Pattern Manager filter -- which reads
``trigger_display`` and nothing else -- found none of them by either verb.
The fix adds ``_literal_alternatives``, ``_strip_display`` and
``_alternation_displays`` to ``speech/pattern_manager.py``, with the class
attributes ``_LITERAL_ALTERNATION_RE``, ``_CLASS_ESCAPE_RE`` and
``_MAX_ALTERNATION_DISPLAYS``, and inserts one alternation step into
``_trigger_display``.

This gate mutates ONE Python source file, ``speech/pattern_manager.py``. It
never touches ``speech/config/patterns.toml`` or any other pattern
expression, so no shipped pattern text is edited by a run. The census tests
in the guard set read the shipped file at run time, which is what lets a
mutation in the display code be measured against every shipped expression
without rewriting one.

  the-alternation-step-is-never-consulted
        ``_trigger_display`` stops asking ``_alternation_displays``, which
        is the bead's defect restored at its source: every widened row
        falls back to the stripping that ate the verb.
  only-the-first-wording-is-displayed
        ``_alternation_displays`` returns its first entry alone, so the row
        names "delete word" and the user who says "erase" cannot find it.
  the-optional-group-is-expanded-again
        The ``? or *`` skip stops skipping, so ``^paste(?: that| here)?$``
        is named as "paste that (or paste here)" and the bare wording
        "paste", which the command also answers to, disappears from the
        window (acceptance criterion D4).
  a-character-class-is-treated-as-a-spoken-word
        ``_literal_alternatives`` loses the ``_CLASS_ESCAPE_RE`` guard, so a
        branch such as ``\\s`` is offered to the user as the spoken word
        "s".
  a-single-branch-group-is-expanded
        The ``len(alternatives) > 1`` guard goes, so a group whose only
        pipe is escaped -- ``(?:a\\|b)`` -- is expanded and the row prints
        regex syntax as a phrase.
  the-combination-cap-no-longer-applies
        ``_MAX_ALTERNATION_DISPLAYS`` stops bounding the product, so an
        expression with four two-branch groups produces one row naming
        sixteen phrases. No shipped pattern reaches the cap, so this entry
        is proven only by
        ``test_too_many_combinations_falls_back_to_the_plain_stripping``,
        added with the gate for that reason.
  the-plain-stripping-fallback-is-broken
        ``_trigger_display`` stops stripping a non-alternation expression,
        so every ordinary command shows its raw regex.
  the-or-format-names-only-the-first-wording
        The alternation result stops going through
        ``_format_phrase_display``, which is the second half of the same
        harm as only-the-first-wording-is-displayed and lives on a
        different line, in the caller rather than the producer.
  an-empty-wording-is-displayed-anyway
        The empty-text refusal inside ``_alternation_displays`` becomes a
        no-op, so an expression whose wordings all strip away leaves the
        Pattern Manager row with an empty label. Nothing shipped produces
        an empty wording, so this entry is proven only by
        ``test_a_wording_that_strips_to_nothing_is_refused``, added with
        the gate.
  a-repeated-wording-is-displayed-twice
        The duplicate check goes, so ``(?:patterns?|patterns)`` -- two
        branches reducing to one spoken phrase -- reads "show patterns (or
        show patterns)". No shipped pattern produces a duplicate wording,
        so this entry is proven only by
        ``test_a_repeated_wording_is_named_once``, added with the gate.

A NOTE ON THE FOUR TESTS ADDED WITH THIS GATE. Four of the ten entries
above changed no measured display against the guard set as it stood:
the combination cap, the duplicate check, the single-branch guard and the
empty-wording refusal are all bounds that no shipped pattern and no
existing fixture reaches. A survivor is a gate failure, not a finding
about the fix, so each of the four gained one test in
``tests/test_trigger_display_alternatives.py`` naming the entry it
catches. The remaining six entries are caught by tests written with the
fix.

A NOTE ON THE PARAMETRIZED CATCHERS THAT ARE NOT NAMED HERE.
``test_each_alternative_is_named`` and
``test_either_spoken_wording_keeps_the_row_visible`` both fail under
several entries below, and neither is listed. pytest builds their ids out
of argument values that contain spaces -- ``test_each_alternative_is_named[
^(?:delete|erase)\\s+word$-delete word (or erase word)]`` -- and the
runner's ``_test_id`` reads only up to the first space, so such a name
could never match the collected set. Every entry is therefore proven by
catchers whose ids carry no space; the parametrized pair still runs in the
selection and still has to fail, it simply is not what the verdict is read
from.

Every entry in ``MUTATIONS`` must be caught. Pattern-not-found, an
ambiguous pattern, a mutation that does not compile, a per-mutation
timeout, a suite-timeout abort, an expected catcher that SKIPPED, an
expected catcher whose name does not exist and a failed restore are
reported as errors, never as a verdict; a failed restore stops the sweep.
``--check`` verifies every pattern matches exactly once in the current
source and that every mutant compiles, without running any test.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
MANAGER = SERVICE / "speech" / "pattern_manager.py"

# The guard set, run for every mutation. Both files are small and the whole
# set takes about two seconds, so narrowing it per mutation would save
# nothing and would hide the half of the harm that only the window shows:
# the display helper's answer is what the Pattern Manager filter matches
# against, and the filter file is what proves a dropped wording makes a
# command unfindable rather than merely mis-labelled.
#
# The display file runs FIRST and the filter file second, deliberately. The
# filter file builds real PatternManagerDialog widgets, so it needs the
# session-scoped qapp fixture; the display file touches no Qt at all and is
# safe ahead of it.
SELECTION = [
    "tests/test_trigger_display_alternatives.py",
    "tests/test_pattern_manager_filter_wordings.py",
]

# -v prints one line per test, which is what names a SKIPPED catcher; -rfE
# keeps the FAILED and ERROR summary lines the readers below parse.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 300

MUTATIONS = [
    {
        # The bead's defect at its source. Every widened row falls back to
        # the stripping that removed the verb, which is exactly the state
        # the finding reported.
        "name": "the-alternation-step-is-never-consulted",
        "file": MANAGER,
        "old": "        wordings = cls._alternation_displays(raw_pattern)\n",
        "new": "        wordings = None\n",
        "expect": [
            "test_no_display_is_a_raw_expression",
            "test_a_group_without_alternatives_keeps_todays_handling",
            "test_a_required_group_still_expands_beside_an_optional_one",
            "test_a_repeated_wording_is_named_once",
            "test_every_shipped_alternative_appears_in_its_display",
            "test_no_alternation_row_displays_a_raw_expression",
            # The user-visible half: the window's filter reads the display
            # and nothing else, so a row named "word" answers to neither
            # verb.
            "test_either_verb_alone_keeps_the_row_visible[delete]",
            "test_either_verb_alone_keeps_the_row_visible[erase]",
        ],
    },
    {
        # Half the harm of the bead: the row is labelled, but only for the
        # wording that happens to come first.
        "name": "only-the-first-wording-is-displayed",
        "file": MANAGER,
        "old": "        return displays or None\n",
        "new": "        return displays[:1] or None\n",
        "expect": [
            "test_a_group_without_alternatives_keeps_todays_handling",
            "test_a_required_group_still_expands_beside_an_optional_one",
            "test_every_shipped_alternative_appears_in_its_display",
            "test_either_verb_alone_keeps_the_row_visible[erase]",
        ],
    },
    {
        # ``continue``, not the whole ``if``: replacing the condition would
        # leave the body as the only statement in the block and the mutant
        # would not compile, which reads as `caught` while proving nothing.
        "name": "the-optional-group-is-expanded-again",
        "file": MANAGER,
        "old": (
            '            if raw_pattern[match.end():match.end() + 1] in ("?", "*"):\n'
            "                continue\n"
        ),
        "new": (
            '            if raw_pattern[match.end():match.end() + 1] in ("?", "*"):\n'
            "                pass\n"
        ),
        "expect": [
            "test_an_optional_group_keeps_todays_handling",
            "test_a_required_group_still_expands_beside_an_optional_one",
        ],
    },
    {
        "name": "a-character-class-is-treated-as-a-spoken-word",
        "file": MANAGER,
        "old": (
            "            if not alternative or cls._CLASS_ESCAPE_RE.search(alternative):\n"
        ),
        "new": "            if not alternative:\n",
        "expect": ["test_a_group_carrying_regex_syntax_falls_back"],
    },
    {
        # ``or None`` keeps the empty-list answer the caller relies on, so
        # the only behaviour this entry changes is the one it names: a
        # group left with a single branch is expanded.
        "name": "a-single-branch-group-is-expanded",
        "file": MANAGER,
        "old": "        return alternatives if len(alternatives) > 1 else None\n",
        "new": "        return alternatives or None\n",
        "expect": ["test_a_group_with_one_branch_keeps_todays_handling"],
    },
    {
        # The condition, not the ``return None`` under it: a mutated value
        # cannot change how the loop above advances, and the body stays a
        # statement so the mutant compiles.
        "name": "the-combination-cap-no-longer-applies",
        "file": MANAGER,
        "old": (
            "        if combinations > cls._MAX_ALTERNATION_DISPLAYS:\n"
            "            return None\n"
        ),
        "new": (
            "        if False:\n"
            "            return None\n"
        ),
        "expect": [
            "test_too_many_combinations_falls_back_to_the_plain_stripping",
        ],
    },
    {
        # An empty string, not a deleted line: ``result`` is read on the
        # next line, so deleting the assignment would raise NameError and
        # the run would read as `caught` on a crash upstream of every
        # assertion.
        "name": "the-plain-stripping-fallback-is-broken",
        "file": MANAGER,
        "old": "        result = cls._strip_display(raw_pattern)\n",
        "new": '        result = ""\n',
        "expect": [
            "test_a_single_wording_keeps_its_plain_display",
            "test_an_optional_group_keeps_todays_handling",
            "test_too_many_combinations_falls_back_to_the_plain_stripping",
            "test_a_group_with_one_branch_keeps_todays_handling",
        ],
    },
    {
        # The ``if wordings is not None:`` line is carried into the pattern
        # so the replacement cannot land on the phrase path's identical
        # ``return cls._format_phrase_display(phrases)`` call above.
        "name": "the-or-format-names-only-the-first-wording",
        "file": MANAGER,
        "old": (
            "        if wordings is not None:\n"
            "            return cls._format_phrase_display(wordings)\n"
        ),
        "new": (
            "        if wordings is not None:\n"
            "            return wordings[0]\n"
        ),
        "expect": [
            "test_a_group_without_alternatives_keeps_todays_handling",
            "test_a_required_group_still_expands_beside_an_optional_one",
            "test_every_shipped_alternative_appears_in_its_display",
            "test_either_verb_alone_keeps_the_row_visible[erase]",
        ],
    },
    {
        # ``pass``, not a deleted line: ``if not text:`` would otherwise be
        # left with an empty body.
        "name": "an-empty-wording-is-displayed-anyway",
        "file": MANAGER,
        "old": (
            '            text = cls._strip_display("".join(rebuilt))\n'
            "            if not text:\n"
            "                return None\n"
        ),
        "new": (
            '            text = cls._strip_display("".join(rebuilt))\n'
            "            if not text:\n"
            "                pass\n"
        ),
        "expect": ["test_a_wording_that_strips_to_nothing_is_refused"],
    },
    {
        "name": "a-repeated-wording-is-displayed-twice",
        "file": MANAGER,
        "old": (
            "            if text not in displays:\n"
            "                displays.append(text)\n"
        ),
        "new": (
            "            if True:\n"
            "                displays.append(text)\n"
        ),
        "expect": ["test_a_repeated_wording_is_named_once"],
    },
]

# Every file a mutation can rewrite. Used to clear exactly the bytecode
# caches that could hold a stale compile of a mutated module.
TARGET_FILES = sorted({mut["file"] for mut in MUTATIONS}, key=str)


def _target(mut):
    return mut["file"]


def _selection(mut):
    return list(mut.get("selection", SELECTION))


def _clear_bytecode():
    """Drop the caches beside the mutated modules only.

    Python decides whether cached bytecode is current from the source's
    (mtime, size). Several mutations here keep the file the same length, so
    a restore fast enough to leave both unchanged would hand the next run
    the other version's bytecode. Only the mutated modules' own cache
    directories are touched -- a glob over the service would walk .venv.
    """
    for path in {p.parent / "__pycache__" for p in TARGET_FILES}:
        shutil.rmtree(path, ignore_errors=True)


def _pytest(*extra):
    """Run pytest with bytecode caching off and a wide reported terminal.

    COLUMNS=1000 matters: pytest cuts the ``- <reason>`` suffix off a
    short-summary line wider than the terminal, and a captured run has no
    terminal, so the default 80 columns truncates node ids that are already
    longer than that. The readers below take the id from the FAILED line,
    which is exactly what the truncation eats.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "COLUMNS": "1000",
        },
    )


def _test_id(line: str) -> str:
    """The ``name[param]`` part after the last ``::`` on a pytest line."""
    return line.split("::")[-1].strip().split(" ")[0]


def collect_names(selection):
    """Real test ids in the selection, so a rename cannot read as a survivor.

    A name that no longer exists can never appear in the failed set, so a
    genuine catch would be reported as a survivor and the reader would go
    hunting for a test that is sitting there passing.
    """
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
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
    errors rather than fails, and a collection error prints only the summary
    form. Either would otherwise be read as a verdict.

    The summary form is read ONLY from the short-summary section. A captured
    log record at ERROR level opens with the same word and names a logger
    instead of a test, so reading it anywhere would turn a genuine catch
    into an error. Captured output is always printed before the
    short-summary banner, so the split separates the two for good.
    """
    lines = output.splitlines()
    summary_at = next(
        (
            i
            for i, line in enumerate(lines)
            if "short test summary info" in line
        ),
        len(lines),
    )
    named = {
        _test_id(line)
        for line in lines
        if "::" in line and " ERROR" in line
    }
    named |= {
        _test_id(line)
        for line in lines[summary_at:]
        if line.startswith("ERROR ")
    }
    return named


def skipped_names(output):
    """Test ids on -v per-test lines that read ``...::name SKIPPED (...)``."""
    return {
        _test_id(line)
        for line in output.splitlines()
        if "::" in line and " SKIPPED" in line
    }


def run_defect(result):
    """A reason this run cannot yield a verdict, or None.

    The suite-timeout abort is the subtle one: pytest-timeout kills the
    process before the failure summary prints, so a runner that parses
    FAILED lines finds none and reports a survivor for a mutation earlier
    tests in the same run may have genuinely caught.
    """
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


def _newline_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def _build_mutant(mut):
    """Return (mutant_bytes, original_bytes, error) for one mutation.

    The line ending is detected per file: this repository mixes
    conventions -- speech/pattern_manager.py is stored with CRLF -- and an
    LF pattern matches nothing in a CRLF file. The target is Python, so
    every mutant is compiled before it is written: a mutant that does not
    parse reads as `caught` while proving nothing, because the interpreter
    rejects it before a test runs.
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
    try:
        compile(mutated, str(path), "exec")
    except SyntaxError as exc:
        return None, raw, (
            f"{mut['name']}: mutated source does not compile: {exc}"
        )
    return mutated.encode("utf-8"), raw, None


def _restore(path: Path, original: bytes) -> bool:
    """Put the file back, and say whether the original bytes are back.

    A False is a hard condition for the caller, not a warning: a run whose
    target is still mutated cannot yield a verdict, and the mutant sits in a
    TRACKED file that every later run and every other session sharing the
    checkout reads as real code.

    write_bytes, never write_text: on Windows write_text rewrites every line
    ending as CRLF, which silently breaks every multi-line pattern in this
    file on the next run. A second Ctrl+C landing inside the restore is
    held, retried once, and re-raised only AFTER the bytecode caches are
    cleared -- a same-size mutant otherwise leaves current-looking bytecode
    for the next run to import.
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


def _apply_and_run(path: Path, mutant: bytes, original: bytes, selection):
    """Write the mutant, run the suite, restore. Return (result, error).

    The write is INSIDE the guarded block on purpose: an interrupt or an
    OSError landing during the write would otherwise leave the tracked file
    mutated with nothing to put it back. A restore that fails clears
    ``result`` as well as setting an error, so no caller can read a verdict
    out of a run whose source file is still mutated.
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


def check_only(mutations):
    """Verify each selected pattern without running any test.

    This is NOT a sweep. It proves every pattern matches exactly once in the
    current source and that every mutant compiles; it cannot see a masked
    survivor. The compile half matters: a pattern can match exactly once and
    still produce text that does not parse, and a --check that answered only
    "do the patterns match" would report clean while the mutation could
    never run.
    """
    stale, non_compiling = [], []
    for mut in mutations:
        _mutant, _original, error = _build_mutant(mut)
        if error is None:
            continue
        if "does not compile" in error:
            non_compiling.append(error)
        else:
            stale.append(error)
        print("ERROR", error)
    names = collect_names(SELECTION)
    missing = []
    for mut in mutations:
        for name in mut["expect"]:
            if name not in names:
                missing.append(
                    f"{mut['name']}: expected test {name!r} does not exist"
                )
                print("ERROR", missing[-1])
    print(
        f"checked {len(mutations)} of {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(non_compiling)} that do not compile, "
        f"{len(missing)} expected catchers that do not exist"
    )
    return 1 if stale or non_compiling or missing else 0


def _selected():
    """The mutations this run covers, honouring --only <name> and --only=<name>.

    Both spellings are accepted, and an empty selection is an ERROR rather
    than a green run of nothing: every mutation here rewrites a tracked
    source file, so the operator has to be able to trust the printed scope.
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
            # Everything up to the next flag, so a later option is not read
            # as a mutation name.
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
    # `--check --only=<name>` checks the one pattern the operator asked
    # about and a --check with an unknown or empty --only exits non-zero.
    selected = _selected()
    if selected is None:
        return 1
    if "--check" in sys.argv[1:]:
        return check_only(selected)

    errors, caught, survived = [], [], []

    # The baseline runs the targets in the order they are DECLARED, which is
    # the order every per-mutation run uses (``_selection`` returns the list
    # unchanged). The display file first and the Qt filter file second, as
    # the comment on SELECTION explains.
    every_selection = []
    for m in selected:
        for target in _selection(m):
            if target not in every_selection:
                every_selection.append(target)
    real_names = collect_names(every_selection)
    print(
        f"collected {len(real_names)} test ids in "
        f"{len(every_selection)} targets"
    )
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
    baseline = _pytest(*every_selection, *PYTEST_ARGS)
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
        result, error = _apply_and_run(
            path, mutant, original, _selection(mut),
        )
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
    print(
        f"caught {len(caught)}, survived {len(survived)}, "
        f"errors {len(errors)}"
    )
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
