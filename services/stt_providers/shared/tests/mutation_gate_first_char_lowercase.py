"""Mutation gate for the conditional first-character lowercasing.

wh-first-char-lowercase. The guard tests were written after the fix, so
they owe the "watch it fail first" step. This script creates the failure
by breaking what each test protects, one mutation at a time, and reports
a mutation as caught only when a named expected test actually fails.

Run it from the shared service directory:

    cd services/stt_providers/shared
    uv run --no-sync python tests/mutation_gate_first_char_lowercase.py

Every failure mode the mutation-gate skill names is reported as an ERROR
rather than a verdict: a pattern that matches zero times or more than
once, a mutant that does not compile, a mutant that never terminates, and
a run whose suite-level timeout aborted pytest before the failure summary
printed. A run that reports any error proves nothing about that mutation.
"""
import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(line_buffering=True)

HERE = pathlib.Path(__file__).resolve().parent
SHARED = HERE.parent
TRANSCRIPT_RULES = SHARED / "shared_stt" / "transcript_rules.py"
WHISPER_ENGINE = SHARED / "shared_stt" / "whisper_engine.py"

# Every mutation runs against this selection. It is the two files that
# hold the guard rows plus the whisper engine file whose shipped rows the
# condition also moved, so a mutation cannot pass by breaking a row the
# selection does not run.
SELECTION = [
    "tests/test_transcript_rules.py",
    "tests/test_whisper_engine_text_rules.py",
    "tests/test_whisper_engine.py",
]

PER_MUTATION_TIMEOUT = 300  # far above a legitimate run, far below a hang

TR = "transcript_rules.py"
WE = "whisper_engine.py"

# (name, target file, pattern, replacement, expected catching tests)
MUTATIONS = [
    (
        "call-site-transcript-rules-unconditional",
        TRANSCRIPT_RULES,
        "    if text and not has_capital_evidence(text):\n",
        "    if text:\n",
        [
            "test_a_capitalized_second_word_keeps_the_leading_capital",
            "test_an_internal_capital_keeps_the_leading_capital",
            "test_an_all_capitals_first_word_survives",
        ],
    ),
    (
        "call-site-whisper-engine-unconditional",
        WHISPER_ENGINE,
        "        if text and not has_capital_evidence(text):\n",
        "        if text:\n",
        [
            "test_a_capitalized_second_word_keeps_the_leading_capital",
            "test_an_internal_capital_keeps_the_leading_capital",
        ],
    ),
    (
        # Value mutation, not a guard mutation: the slice can never find
        # an uppercase letter, so the internal-capital half is dead while
        # the loop structure is untouched.
        "internal-capital-half-sees-nothing",
        TRANSCRIPT_RULES,
        "    if any(ch.isupper() for ch in words[0][1:]):\n",
        "    if any(ch.isupper() for ch in words[0][1:1]):\n",
        [
            "test_an_internal_capital_keeps_the_leading_capital",
            "test_an_all_capitals_first_word_survives",
        ],
    ),
    (
        # Pattern refreshed after wh-first-char-lowercase.1.1 rewrote this
        # half to exclude the pronoun. The behavior it breaks is the same.
        "second-word-half-disabled",
        TRANSCRIPT_RULES,
        "    return second[:1].isupper() and not _is_capitalized_pronoun(second)\n",
        "    return False\n",
        ["test_a_capitalized_second_word_keeps_the_leading_capital"],
    ),
    (
        # The boundary the design chose: only the SECOND word counts.
        # Widening it to any later word makes 'Call me Friday' keep its C.
        "second-word-half-widened-to-any-later-word",
        TRANSCRIPT_RULES,
        "    return second[:1].isupper() and not _is_capitalized_pronoun(second)\n",
        "    return any(w[:1].isupper() for w in words[1:])\n",
        ["test_a_capital_further_along_is_not_evidence"],
    ),
    (
        # wh-first-char-lowercase.1.1, the mutation the ruling requires by
        # name: delete the pronoun check and the reported bug comes back.
        # One mutation covers both call sites, because whisper_engine.py
        # imports this same helper.
        "pronoun-check-deleted",
        TRANSCRIPT_RULES,
        "    return second[:1].isupper() and not _is_capitalized_pronoun(second)\n",
        "    return second[:1].isupper()\n",
        [
            "test_a_capitalized_pronoun_is_not_evidence",
            "test_the_bare_pronoun_is_not_evidence",
            "test_every_pronoun_contraction_is_excluded",
            "test_a_curly_apostrophe_pronoun_is_not_evidence",
        ],
    ),
    (
        # The exclusion must match the pronoun itself, not every word that
        # begins with the letter. Widening it swallows "Illinois".
        "pronoun-check-widened-to-any-i-word",
        TRANSCRIPT_RULES,
        '    return base == "I"\n',
        '    return base[:1] == "I"\n',
        ["test_a_word_that_merely_starts_with_i_is_still_evidence"],
    ),
    (
        # Value mutation: drop the typographic apostrophe a recognizer may
        # emit, and the curly-apostrophe pronoun stops being recognized.
        # The loop structure is untouched.
        "curly-apostrophe-dropped",
        TRANSCRIPT_RULES,
        "_PRONOUN_APOSTROPHES = \"'\\u2019\"\n",
        "_PRONOUN_APOSTROPHES = \"'\"\n",
        ["test_a_curly_apostrophe_pronoun_is_not_evidence"],
    ),
    (
        # Index 0 must be excluded from the internal-capital scan. If it
        # is not, every capitalized opening counts as its own evidence
        # and nothing is ever lowercased.
        "internal-capital-half-counts-index-zero",
        TRANSCRIPT_RULES,
        "    if any(ch.isupper() for ch in words[0][1:]):\n",
        "    if any(ch.isupper() for ch in words[0][0:]):\n",
        [
            "test_a_lowercase_second_word_still_lowercases_the_first",
            "test_a_single_word_utterance_is_still_lowercased",
            "test_lowercases_first_character",
        ],
    ),
]


def line_ending(raw: bytes) -> bytes:
    """Return the line ending the file actually uses."""
    return b"\r\n" if raw.count(b"\r\n") else b"\n"


def to_file_endings(pattern: str, ending: bytes) -> str:
    """Translate an LF-written pattern to the target file's endings."""
    if ending == b"\r\n":
        return pattern.replace("\n", "\r\n")
    return pattern


def clear_pycache() -> None:
    for cache in SHARED.rglob("__pycache__"):
        if ".venv" in cache.parts:
            continue
        for item in cache.glob("*.pyc"):
            item.unlink(missing_ok=True)


def run_pytest(selection, extra=()):
    """Run pytest over the selection. Returns (rc, combined output)."""
    clear_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q", "-p",
         "no:cacheprovider", *extra],
        cwd=SHARED,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def collect_test_names():
    """Every test name pytest can actually collect, without parameters."""
    rc, out = run_pytest(SELECTION, extra=("--collect-only",))
    names = set()
    for line in out.splitlines():
        if "::" not in line:
            continue
        leaf = line.strip().split("::")[-1]
        names.add(leaf.split("[")[0])
    return rc, names


def failed_tests(output: str) -> set:
    """Test names from the short failure summary."""
    found = set()
    for line in output.splitlines():
        m = re.match(r"FAILED\s+\S+::(?:\S+::)?([A-Za-z0-9_]+)", line.strip())
        if m:
            found.add(m.group(1))
    return found


def main() -> int:
    print("=" * 72)
    print("Mutation gate: conditional first-character lowercasing")
    print("wh-first-char-lowercase")
    print("=" * 72)
    print(f"Mutations: {len(MUTATIONS)} (full set, no subset skipping)")
    print(f"Selection: {' '.join(SELECTION)}")
    print()

    errors = []
    survivors = []
    caught = []

    # --- Precondition 1: every expected test name must exist. ---
    rc, available = collect_test_names()
    if not available:
        print("[ERROR] could not collect any test names; aborting")
        return 2
    missing = set()
    for name, _f, _p, _r, expected in MUTATIONS:
        for test in expected:
            if test not in available:
                missing.add((name, test))
    if missing:
        print("[ERROR] expected test names that do not exist:")
        for mutation, test in sorted(missing):
            print(f"        {mutation}: {test}")
        print("        Refusing to start: a name that cannot be collected")
        print("        can never appear in the failed set, so a genuine")
        print("        catch would be reported as a survivor.")
        return 2
    print(f"[ok] all expected test names collectable ({len(available)} names)")

    # --- Precondition 2: the baseline must be green. ---
    rc, out = run_pytest(SELECTION)
    if rc != 0:
        print("[ERROR] baseline is NOT green; aborting")
        print(out[-2500:])
        return 2
    tail = [ln for ln in out.splitlines() if "passed" in ln]
    print(f"[ok] baseline green: {tail[-1].strip() if tail else 'passed'}")
    print()

    originals = {
        TRANSCRIPT_RULES: TRANSCRIPT_RULES.read_bytes(),
        WHISPER_ENGINE: WHISPER_ENGINE.read_bytes(),
    }

    try:
        for name, path, pattern, replacement, expected in MUTATIONS:
            print(f"--- {name} ({path.name})")
            raw = originals[path]
            ending = line_ending(raw)
            src = raw.decode("utf-8")
            pat = to_file_endings(pattern, ending)
            rep = to_file_endings(replacement, ending)

            hits = src.count(pat)
            if hits != 1:
                kind = "pattern-not-found" if hits == 0 else "pattern-ambiguous"
                print(f"    [ERROR] {kind}: matched {hits} times, need exactly 1")
                errors.append((name, kind))
                continue

            mutated = src.replace(pat, rep, 1)
            try:
                compile(mutated, str(path), "exec")
            except SyntaxError as exc:
                print(f"    [ERROR] does-not-compile: {exc}")
                errors.append((name, "does-not-compile"))
                continue

            path.write_bytes(mutated.encode("utf-8"))
            try:
                rc, out = run_pytest(SELECTION)
            except subprocess.TimeoutExpired:
                path.write_bytes(raw)
                print("    [ERROR] never-terminates: run exceeded "
                      f"{PER_MUTATION_TIMEOUT}s")
                errors.append((name, "never-terminates"))
                continue
            finally:
                path.write_bytes(raw)

            if "+++ Timeout +++" in out:
                print("    [ERROR] suite-timeout-abort: pytest was killed "
                      "before the failure summary printed")
                errors.append((name, "suite-timeout-abort"))
                continue

            failures = failed_tests(out)
            hit = sorted(set(expected) & failures)
            if not hit:
                print(f"    [SURVIVED] no expected test failed "
                      f"(rc={rc}, {len(failures)} other failures)")
                survivors.append(name)
                continue

            # A verdict earned by an unrelated crash is no verdict.
            for marker in ("AttributeError", "TypeError", "NameError"):
                if marker in out and "AssertionError" not in out:
                    print(f"    [ERROR] false-catch: mutant raised {marker} "
                          "with no assertion failure")
                    errors.append((name, f"false-catch-{marker}"))
                    break
            else:
                print(f"    [caught] by {', '.join(hit)}")
                caught.append(name)
    finally:
        for path, raw in originals.items():
            path.write_bytes(raw)
        clear_pycache()

    print()
    print("=" * 72)
    print(f"caught:    {len(caught)}/{len(MUTATIONS)}")
    print(f"survivors: {len(survivors)}  {survivors if survivors else ''}")
    print(f"errors:    {len(errors)}  {errors if errors else ''}")
    print("=" * 72)

    # Confirm the restore really restored, byte for byte.
    for path, raw in originals.items():
        if path.read_bytes() != raw:
            print(f"[ERROR] {path.name} was NOT restored byte for byte")
            return 2
    print("[ok] both source files restored byte for byte")

    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
