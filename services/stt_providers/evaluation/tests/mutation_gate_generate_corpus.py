"""Mutation gate for the corpus generator's category-to-directory mapping.

Committed next to the tests it checks so it can be re-run on demand. It proves
that tests/test_generate_corpus.py actually detects the defect it was written
for, rather than passing for some unrelated reason.

Two mutations, per the "watch it fail first" rule:

  revert-the-fix     Puts back the eight-entry identity dict in category_dir
                     and the parallel hand-maintained list in
                     ensure_output_dirs -- the exact code that raised
                     KeyError: 'va_punctuation_symbols' partway through the
                     2026-08-24 run.
  drop-new-categories
                     Keeps the derivation but changes the INPUT it derives
                     from, filtering the va_* categories out of the set
                     ensure_output_dirs walks. This is the input-level
                     mutation: the code still compiles, still creates
                     directories, and a test that merely asserted "some
                     directories exist" would stay green.

Usage, from services/stt_providers/shared:
    uv run python ../evaluation/tests/mutation_gate_generate_corpus.py
"""

import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

TESTS_DIR = Path(__file__).resolve().parent
EVALUATION_DIR = TESTS_DIR.parent
TARGET = EVALUATION_DIR / "generate_corpus.py"
TEST_FILE = TESTS_DIR / "test_generate_corpus.py"

CURRENT_CATEGORY_DIR_TAIL = '''    return category


def ensure_output_dirs(vocabulary: list[Utterance]) -> None:'''

OLD_CATEGORY_DIR_TAIL = '''    return {
        "single_word": "single_word",
        "multi_word": "multi_word",
        "parameterized": "parameterized",
        "punctuation": "punctuation",
        "dictation": "dictation",
        "discontinuous": "discontinuous",
        "itn": "itn",
        "litmus": "litmus",
    }[category]


def ensure_output_dirs(vocabulary: list[Utterance]) -> None:'''

CURRENT_LOOP = (
    "    for category in sorted({u.category for u in vocabulary}):"
)
OLD_LOOP = '''    for category in [
        "single_word",
        "multi_word",
        "parameterized",
        "punctuation",
        "dictation",
        "discontinuous",
        "itn",
        "litmus",
    ]:'''
FILTERED_LOOP = (
    "    for category in sorted("
    '{u.category for u in vocabulary if not u.category.startswith("va_")}):'
)

MUTATIONS = [
    {
        "name": "revert-the-fix",
        "edits": [
            (CURRENT_CATEGORY_DIR_TAIL, OLD_CATEGORY_DIR_TAIL),
            (CURRENT_LOOP, OLD_LOOP),
        ],
        "expected_catchers": [
            "test_every_vocabulary_category_maps_to_a_directory",
            "test_ensure_output_dirs_creates_one_directory_per_category",
        ],
    },
    {
        "name": "drop-new-categories",
        "edits": [(CURRENT_LOOP, FILTERED_LOOP)],
        "expected_catchers": [
            "test_ensure_output_dirs_creates_one_directory_per_category",
        ],
    },
]

PYTEST = [sys.executable, "-m", "pytest", str(TEST_FILE), "-q", "-rf", "-p", "no:cacheprovider"]
ENV_NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}


def run_pytest() -> tuple[int, str]:
    import os

    env = dict(os.environ, **ENV_NO_BYTECODE)
    try:
        proc = subprocess.run(
            PYTEST, capture_output=True, text=True, timeout=180, env=env,
            cwd=str(EVALUATION_DIR),
        )
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    out = proc.stdout + proc.stderr
    if "+++ Timeout +++" in out:
        return -2, out
    return proc.returncode, out


def collect_test_names() -> set[str]:
    import os

    env = dict(os.environ, **ENV_NO_BYTECODE)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST_FILE), "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=180, env=env,
        cwd=str(EVALUATION_DIR),
    )
    names = set()
    for line in proc.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split("[")[0].strip())
    return names


def main() -> int:
    original = TARGET.read_bytes()
    errors: list[str] = []
    survivors: list[str] = []
    caught: list[str] = []

    print(f"target: {TARGET}")
    print(f"tests:  {TEST_FILE}")
    print(f"scope:  {len(MUTATIONS)} mutations, full set (this gate has no others)")
    print()

    # Every expected catcher must exist, or a real catch reads as a survivor.
    real_names = collect_test_names()
    for mutation in MUTATIONS:
        for name in mutation["expected_catchers"]:
            if name not in real_names:
                print(f"ERROR: expected catcher {name!r} does not exist")
                return 2

    rc, out = run_pytest()
    if rc != 0:
        print("ERROR: baseline is not green; refusing to mutate")
        print(out[-2000:])
        return 2
    print("baseline: green")
    print()

    try:
        for mutation in MUTATIONS:
            name = mutation["name"]
            source = original.decode("utf-8")
            ok = True
            for old, new in mutation["edits"]:
                count = source.count(old)
                if count != 1:
                    errors.append(f"{name}: pattern matched {count} times, expected exactly 1")
                    ok = False
                    break
                source = source.replace(old, new, 1)
            if not ok:
                continue

            try:
                compile(source, str(TARGET), "exec")
            except SyntaxError as exc:
                errors.append(f"{name}: mutated source does not compile: {exc}")
                continue

            TARGET.write_bytes(source.encode("utf-8"))
            rc, out = run_pytest()
            TARGET.write_bytes(original)

            if rc == -1:
                errors.append(f"{name}: run timed out")
                continue
            if rc == -2:
                errors.append(f"{name}: suite-timeout-abort, no verdict")
                continue

            failed = {
                line.split("::")[-1].split("[")[0].strip()
                for line in out.splitlines()
                if line.startswith("FAILED")
            }
            hit = [c for c in mutation["expected_catchers"] if c in failed]
            if hit:
                caught.append(f"{name}: caught by {', '.join(hit)}")
            else:
                survivors.append(f"{name}: SURVIVED; failures were {sorted(failed) or 'none'}")
    finally:
        TARGET.write_bytes(original)

    for line in caught:
        print("caught   ", line)
    for line in survivors:
        print("SURVIVED ", line)
    for line in errors:
        print("ERROR    ", line)
    print()
    print(f"{len(caught)} caught, {len(survivors)} survived, {len(errors)} errors")
    return 0 if not survivors and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
