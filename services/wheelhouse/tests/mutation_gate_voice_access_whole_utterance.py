"""Mutation gate for finding wh-voice-access-parity.1.6.2.1.

Codex reported that seven command entries lacked ``whole_utterance_only``, so
a phrase such as "show numbers in the report" executed the command in the
middle of a sentence and dropped the words after it. The seven are
select-word, select-line, select-paragraph, show-numbers, hide-numbers,
show-grid and hide-grid.

The data fix is already in the file: commit c3a54348 (bead
wh-cmd-prefix-word-loss, 2026-08-26) added ``whole_utterance_only = true`` to
153 patterns by a rule, and all seven fall under that rule. What was missing
is evidence naming these seven, in BOTH directions, which the signed
acceptance for wh-voice-access-parity.1.6 requires. The guard tests are in
``TestVoiceAccessAliasEntriesKeepBothDirections`` in
tests/test_router_command_prefix_word_loss.py, and they were written after the
fix, so this gate is what proves they can still see the defect.

Run it from services/wheelhouse:

    python tests/mutation_gate_voice_access_whole_utterance.py

Nine mutations of speech/config/patterns.toml. Seven revert the fix one entry
at a time, and two are input-level checks that the mutation-gate skill asks
for, aimed at the direction a revert cannot reach:

  drop-flag-<doc_id>   Remove ``whole_utterance_only = true`` from one entry.
                       That IS the defect codex reported, restricted to a
                       single command, so a gate that removed all seven at
                       once could not tell which test was doing the work.
                       Seven of these, one per reported entry.
  require-this-word    Narrow ``^select (?:this )?word$`` to
                       ``^select this word$``. The flag stays, so every
                       mid-sentence test still passes; the alias "select word"
                       simply stops working. Only the whole-utterance half can
                       see it, which is what makes it the input-level check
                       for direction two.
  drop-apply-numbers   Narrow ``^(?:show numbers|apply numbers|show numbers here)$``
                       to ``^(?:show numbers|show numbers here)$``. Same shape as above on a different
                       entry, and it removes exactly the older wording that
                       wh-dismiss-alias-restore put back at David's direction.

All nine must be caught, and "caught" means the expected test failed on its
own assertion. A mutant that makes the test raise an exception instead has
proved nothing: the test never reached the assertion the mutation claims to
break. So this gate reads the reason pytest prints after each FAILED name and
refuses to count an exception as a catch (wh-voice-access-parity.2.3.2.4).

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutant that is no longer valid TOML, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR line
in the summary, and an expected test that failed for any reason other than its
own assertion.

The gate detects the target file's own line endings and translates each
pattern to them before matching, so a future CRLF conversion of patterns.toml
cannot turn every multi-line pattern into a silent pattern-not-found batch. It
never rewrites the file's endings.

SCOPE NOTE: the mutations also fail the rule-based sweep tests in
``TestEveryExposedCommandIsWholeUtteranceOnly`` in the same file, which is
expected and harmless -- the gate scores only the named expected tests. The
run covers one test file, not the whole suite, so a mutation's effect on
tests/test_grid_speech_routing.py is outside what this gate measures.

BEAD wh-voice-access-parity.1.15 added eighteen mutations for the rows it
changed. They run against tests/test_voice_access_aliases_1_15.py only (a
mutation's own "files" key; the first nine keep the file above), and their
expected names carry the file ("test_voice_access_aliases_1_15.py::<name>")
because test_voice_access_aliases_1_14.py defines tests of the same names.

  require-that-<row>     Make "that" required again on one of the seven rows
                         that made it optional. The bare form ("cap", "bold")
                         stops matching; the "that" form still works.
  drop-bold-face         Remove the "bold face" alternative (F2).
  drop-select-forward    Remove "forward" / "backward" from the two select-
  drop-select-backward   words rows (F3).
  drop-equals-sine       Narrow the equal-sign row back to "sign" (F5).
  drop-flag-<doc_id>     Remove whole_utterance_only from one of the seven
                         rows whose bare form is new. A bare "cap" without the
                         flag executes at the start of "cap the bottle". The
                         first sweep had all seven survive: every dictation
                         case put the word mid-sentence, where the flag does
                         not act. Six leading-word cases were then added to
                         test_the_bare_word_inside_a_sentence_is_dictation;
                         no-space-that is caught by the joined-words test.

Run ``--check`` first: it confirms every pattern matches exactly once, and
that every mutant is valid TOML whose every ``pattern`` compiles as a
regular expression, without running a test. A mutant regex that does not
compile would fail every catalog load and read as a catch.

This gate starts pytest with the interpreter that runs it (``sys.executable
-m pytest``), not ``uv run``: in a worktree ``uv run`` can sync the .venv.
Run it with the service's interpreter, from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_voice_access_whole_utterance.py --check
    .venv/Scripts/python.exe tests/mutation_gate_voice_access_whole_utterance.py
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "config" / "patterns.toml"
PREFIX_FILE = "tests/test_router_command_prefix_word_loss.py"
ALIASES_1_15 = "tests/test_voice_access_aliases_1_15.py"
# Every file some mutation runs; the name check and the baseline cover all.
TEST_FILES = [PREFIX_FILE, ALIASES_1_15]

# --tb=line is what carries the failure REASON. This project's pytest prints
# its short summary as a bare "FAILED <nodeid>" with no " - reason" suffix, so
# the summary alone cannot tell an assertion failure from a crash. With
# --tb=line each failure also prints one "<path>:<lineno>: <reason>" line.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

# One "<path>:<lineno>: <reason>" line from --tb=line.
_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")

# A reason that begins with an exception class name means the test raised
# rather than failed its assertion. AssertionError is the one exception name
# that IS an assertion failure: pytest prints it when the assert carries a
# message, and prints the assert expression itself when it does not.
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_MID = "test_the_phrase_inside_a_longer_sentence_dictates"
_WHOLE = "test_the_phrase_alone_still_executes"

# The seven entries codex named. Each anchor is the doc_id line plus every
# line between it and the flag, which makes the match unique: a doc_id occurs
# once in the file.
_SEVEN = [
    ("select-word", 'doc_id = "select-word"\n'),
    ("select-line", 'doc_id = "select-line"\n'),
    ("select-paragraph", 'doc_id = "select-paragraph"\n'),
    ("show-numbers", 'doc_id = "show-numbers"\nrequires_hotword = false\n'),
    ("hide-numbers", 'doc_id = "hide-numbers"\nrequires_hotword = false\n'),
    ("show-grid", 'doc_id = "show-grid"\n'),
    ("hide-grid", 'doc_id = "hide-grid"\n'),
]

MUTATIONS = [
    {
        "name": f"drop-flag-{doc_id}",
        "old": head + "whole_utterance_only = true\n",
        "new": head,
        "expect": [_MID],
    }
    for doc_id, head in _SEVEN
]

MUTATIONS += [
    {
        # Input-level, direction two. The flag is untouched, so every
        # mid-sentence assertion still holds; the optional "this" simply
        # becomes required and the bare alias stops matching.
        "name": "require-this-word",
        "old": "pattern = '''^select (?:this )?word$'''\n",
        "new": "pattern = '''^select this word$'''\n",
        "expect": [_WHOLE],
    },
    {
        # Input-level, direction two, on a different entry. Dropping "apply"
        # removes the older Wheelhouse wording that wh-dismiss-alias-restore
        # put back at David's direction, so a person who learned it loses it.
        "name": "drop-apply-numbers",
        "old": "pattern = '''^(?:show numbers|apply numbers|show numbers here)$'''\n",
        "new": "pattern = '''^(?:show numbers|show numbers here)$'''\n",
        "expect": [_WHOLE],
    },
]

for mut in MUTATIONS:
    mut["files"] = [PREFIX_FILE]

# --- wh-voice-access-parity.1.15 ---------------------------------------------
# Catchers in tests/test_voice_access_aliases_1_15.py, qualified by file name
# because test_voice_access_aliases_1_14.py defines the same test names.


def _a15(name):
    return f"test_voice_access_aliases_1_15.py::{name}"


_OWNS = _a15("test_the_named_row_owns_the_form")
_LIVE = _a15("test_the_live_matcher_fires_and_consumes_the_whole_form")
_INDEX = _a15("test_the_live_first_word_index_reaches_the_row")
_RUNS = _a15("test_the_whole_utterance_runs_a_command")
_CTRL_B = _a15("test_bold_forms_press_ctrl_b")
_FLAG = _a15("test_whole_utterance_flag")
_IN_SENTENCE = _a15("test_the_bare_word_inside_a_sentence_is_dictation")
_NO_SPACE_JOINED = _a15("test_no_space_with_words_still_types_them_joined")
_SELECT_WORDS = _a15("test_select_forward_backward_words")
_SINE_ROW = _a15("test_the_equal_sign_row_matches")
_SINE_SENTENCE = _a15("test_equals_sine_inside_a_sentence_types_the_character")
_SINE_ALONE = _a15("test_equal_sine_alone_types_the_character")
_SINE_PAUSE = _a15("test_a_pause_after_equals_still_types_the_character")

# (row name, the pattern line as shipped, the same line with "that" required)
_OPTIONAL_THAT = [
    ("unselect", "^unselect(?: that)?$", "^unselect that$"),
    ("italicize", "^(?:italics|italicize(?: that)?)$",
     "^(?:italics|italicize that)$"),
    ("all-caps", "^all caps(?: that)?$", "^all caps that$"),
    ("no-caps", "^no caps(?: that)?$", "^no caps that$"),
    ("cap", "^cap(?: that)?$", "^cap that$"),
    ("no-space", "^no space(?: that)?$", "^no space that$"),
    ("bold", "^(?:bold|boldface|bold face)(?: text| that)?$",
     "^(?:bold|boldface|bold face) (?:text|that)$"),
]

_NEW = [
    {
        # Input-level, F1: the flag stays and the "that" form still works;
        # only the bare form stops matching.
        "name": f"require-that-{row}",
        "old": f"pattern = '''{shipped}'''\n",
        "new": f"pattern = '''{required}'''\n",
        "expect": [_OWNS, _LIVE, _INDEX, _RUNS]
        + ([_CTRL_B] if row == "bold" else []),
    }
    for row, shipped, required in _OPTIONAL_THAT
]

_NEW += [
    {
        # F2: "bold face" with a space; "bold" and "boldface" stay.
        "name": "drop-bold-face",
        "old": "pattern = '''^(?:bold|boldface|bold face)(?: text| that)?$'''\n",
        "new": "pattern = '''^(?:bold|boldface)(?: text| that)?$'''\n",
        "expect": [_OWNS, _LIVE, _INDEX, _RUNS, _CTRL_B],
    },
    {
        # F3: "select forward [n] words"; "select next" stays.
        "name": "drop-select-forward",
        "old": "pattern = '''^select (?:next|forward|right|write)\\s+(\\d+)?\\s*words?$'''\n",
        "new": "pattern = '''^select (?:next|right|write)\\s+(\\d+)?\\s*words?$'''\n",
        "expect": [_OWNS, _LIVE, _INDEX, _SELECT_WORDS],
    },
    {
        # F3: "select backward [n] words"; "previous" and "last" stay.
        "name": "drop-select-backward",
        "old": "pattern = '''^select (?:previous|last|backward|left)\\s+(\\d+)?\\s*words?$'''\n",
        "new": "pattern = '''^select (?:previous|last|left)\\s+(\\d+)?\\s*words?$'''\n",
        "expect": [_OWNS, _LIVE, _INDEX, _SELECT_WORDS],
    },
    {
        # F5, David's option 1: "equals sine" types "=".
        "name": "drop-equals-sine",
        "old": "pattern = '''\\bequals? (?:sign|sine)\\b'''\n",
        "new": "pattern = '''\\bequals? sign\\b'''\n",
        "expect": [_SINE_ROW, _SINE_SENTENCE, _SINE_ALONE, _SINE_PAUSE],
    },
]

# The seven rows whose bare form is new. Without the flag, a sentence that
# STARTS with the bare word executes the command and loses the rest.
_FLAGGED_1_15 = [
    "unselect-that",
    "italics",
    "uppercase-all-caps-that",
    "lowercase-no-caps-that",
    "capitalize-that",
    "no-space-that",
    "bold",
]
_NEW += [
    {
        "name": f"drop-flag-{doc_id}",
        "old": f'doc_id = "{doc_id}"\nwhole_utterance_only = true\n',
        "new": f'doc_id = "{doc_id}"\n',
        # "no space left" belongs to the no-space row by design, so a
        # sentence led by "no space" cannot be a dictation case. Without the
        # flag, no-space-that runs as a prefix of "no space hello world" and
        # the words are typed with their spaces, which the joined test sees.
        "expect": [_FLAG, _NO_SPACE_JOINED if doc_id == "no-space-that"
                   else _IN_SENTENCE],
    }
    for doc_id in _FLAGGED_1_15
]

for mut in _NEW:
    mut["files"] = [ALIASES_1_15]
MUTATIONS += _NEW


def _pytest(*extra):
    # sys.executable, not "uv run": in a worktree "uv run" can sync the .venv.
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _names_on(nodeid):
    """The bare test name and the file-qualified one, parameters stripped."""
    nodeid = nodeid.strip()
    file_name = nodeid.split("::")[0].replace("\\", "/").rsplit("/", 1)[-1]
    name = nodeid.split("::")[-1].strip().split("[")[0].split(" ")[0]
    return {name, f"{file_name}::{name}"}


def collect_names():
    """Real test names in the files, so a renamed test cannot read as a survivor."""
    out = _pytest(*TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names |= _names_on(line)
    return names


def mutant_problem(text, mut):
    """Why the mutation cannot run as written, or None.

    Checks the pattern matches exactly once, the mutant is valid TOML, and
    every ``pattern`` in the mutant compiles: a regex that does not compile
    fails every catalog load, which reads as a catch and proves nothing.
    """
    count = text.count(mut["old"])
    if count != 1:
        return f"pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    try:
        rows = tomllib.loads(mutated)["pattern"]
    except tomllib.TOMLDecodeError as exc:
        return f"mutated file is not valid TOML: {exc}"
    for row in rows:
        try:
            re.compile(row.get("pattern", ""))
        except re.error as exc:
            return f"mutant pattern {row['pattern']!r} does not compile: {exc}"
    return None


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert statement."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped.

    Each failure is recorded both bare and qualified by its file name, so an
    expected name may use either form.
    """
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names |= _names_on(line[len("FAILED "):].split(" - ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing parameter.

    --tb=line does not say which test each reason belongs to, so the caller
    requires that EVERY reason in the run is an assertion failure. That is the
    stricter reading and it is the right one here: the only failures a
    mutation run should produce are the expected catchers and the rule-based
    sweeps, and both fail on their own asserts.
    """
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [line for line in output.splitlines() if line.startswith("ERROR ")]


def main():
    original = SRC.read_bytes()
    # Match the file's own line endings rather than assuming LF. A repository
    # can mix conventions per file, and an LF pattern finds nothing in a CRLF
    # file -- which reads as a batch of pattern-not-found errors.
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8")
    for mut in MUTATIONS:
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    if "--check" in sys.argv[1:]:
        # Patterns and mutants only; no test runs.
        for mut in MUTATIONS:
            problem = mutant_problem(text, mut)
            if problem:
                errors.append(f"{mut['name']}: {problem}")
                print("ERROR", errors[-1])
            else:
                print(f"ok {mut['name']}")
        print(f"checked {len(MUTATIONS)} patterns, {len(errors)} errors")
        return 1 if errors else 0

    real_names = collect_names()
    print(f"line endings in {SRC.name}: {'CRLF' if newline == chr(13) + chr(10) else 'LF'}")
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        problem = mutant_problem(text, mut)
        if problem:
            errors.append(f"{mut['name']}: {problem}")
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        SRC.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*mut["files"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            SRC.write_bytes(original)
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            SRC.write_bytes(original)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            # 0 means every test passed, 1 means some test failed. Anything
            # else is an aborted run -- interrupted, internal error, usage
            # error, nothing collected -- and is not a verdict either way.
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        # The expected test failed. Insist every failure in the run was an
        # assertion failure: a mutant that makes a test RAISE never reached
        # the assertion the mutation claims to break, so counting it as caught
        # would prove nothing (wh-voice-access-parity.2.3.2.4).
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_assertion_failure(r)]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
