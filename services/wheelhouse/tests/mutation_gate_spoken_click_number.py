"""Mutation gate for the spoken "click N" badge click
(wh-number-badge-problems.2).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_spoken_click_number.py
    .venv/Scripts/python.exe tests/mutation_gate_spoken_click_number.py --check

It proves that TestSpokenClickNumberWhilePainted in
tests/test_speech_processor_bare_number.py catches a defect in each guard of
``SpeechProcessor._maybe_hold_spoken_click`` in speech/speech_processor.py:
the finalized command buffer "click 74" (or "tap 74", "click number 74",
"click seventy four") must click badge 74 while the overlay shows badges,
and every other multi-word payload must dictate as before.

  handoff-dropped            _maybe_hold_bare_number no longer hands a
                             multi-word payload to the spoken-click hold
                             (catchers retargeted by .1.5; the entry's
                             own comment says why).
  verb-tuple-widened         "press" joins the click/tap verbs (the prefix
                             check's value).
  verb-check-dropped         The prefix check itself no longer refuses.
  range-check-bypassed       A rest that does not parse (0, 1000, a name, a
                             trailing word, a lone filler) is treated as 74.
  filler-alone-accepted      "click number" with no number is held.
  whole-utterance-dropped    A buffer inside a longer utterance is held.
  overlay-check-dropped      The hold opens with the overlay closed.
  held-digits-only           The hold keeps the number in digits instead of
                             the spoken payload, so every dictation fallback
                             types "74" for a spoken "click 74"
                             (wh-number-badge-problems.1.3).
  consume-uses-spoken-words  The consume runs "click <words as spoken>"
                             instead of "click <N>", so "click number 74"
                             and "click seventy four" no longer execute
                             "click 74".
  punctuation-strip-dropped  _spoken_number_text stops stripping terminal
                             punctuation, so the hold refuses the
                             punctuated forms local STT really sends
                             ("click 74.") (wh-number-badge-problems.1.4).
  consume-skips-punctuation  The consume parses the held text without that
                             strip, so a punctuated hold opens and then
                             dictates instead of clicking.
  bare-hold-skips-strip      The BARE hold parses the raw word, so a lone
                             "74." dictates again (boss ruling 2026-09-02,
                             overridable).
  bare-growth-skips-strip    The growth check parses the raw words, so
                             "seventy" + "four?" stops being one number.
  bare-consume-skips-strip   The consume's bare branch parses the raw held
                             words, so a punctuated bare hold opens and
                             then dictates.

All fourteen must be caught. Pattern-not-found, an ambiguous pattern, a mutation
that does not compile, a per-mutation timeout, a suite-timeout abort, and an
expected catcher that SKIPPED are reported as errors, never as a verdict.
``--check`` verifies every pattern matches exactly once in the current source
and that every mutant compiles, without running any test.

Expected-catcher names keep their pytest parameter id (``name[words5]``), so
a parametrized case is named exactly; the collected-name check reads the same
form from ``--collect-only``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "speech_processor.py"
TEST_FILE = "tests/test_speech_processor_bare_number.py"
CLASS = "TestSpokenClickNumberWhilePainted"
CLASSES = (
    CLASS,
    "TestPunctuatedBareNumberStillClicks",
    "TestASpokenClickWhoseEndMarkerNeverArrives",
)

# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# keeps the FAILED summary lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rfE"]
PER_MUTATION_TIMEOUT_S = 180

CLICKS = "test_click_plus_a_number_clicks_that_badge_at_the_end_marker"
DICTATES = "test_click_plus_anything_else_still_dictates"
CLICKS_PUNCT = "test_terminal_punctuation_on_the_number_still_clicks_the_badge"
BARE_PUNCT = "test_a_punctuated_bare_number_clicks_that_badge"
BARE_CLASS = "TestPunctuatedBareNumberStillClicks"
# The three cases carry explicit ids, because a generated id would hold
# the command text and its space would truncate the name _test_id reads.
BARE_PUNCT_ALL = [
    f"{BARE_PUNCT}[digits-period]",
    f"{BARE_PUNCT}[digits-exclamation]",
    f"{BARE_PUNCT}[words-question]",
]
# The hold guard reads only an utterance's FIRST word, so the two cases
# whose first word carries the punctuation are its catchers; the
# words-question case ("seventy" then "four?") opens its hold either way
# and is caught by the growth check instead.
BARE_PUNCT_FIRST_WORD = [
    f"{BARE_PUNCT}[digits-period]",
    f"{BARE_PUNCT}[digits-exclamation]",
]

_VERB_CHECK = (
    "        if tokens[0].casefold() not in self._SPOKEN_CLICK_VERBS:\n"
    "            return False\n"
)
# wh-overlay-count-homophones.1.5: 97567e82 added aliases=True and
# reflowed this call across three lines, so the old one-line literal
# matched 0 times and both mutations that use it reported an error
# instead of running. Same guard, same catchers.
_PARSE_CHECK = (
    "        number = parse_number_word(\n"
    "            self._spoken_number_text(tokens[1:]), aliases=True,\n"
    "        )\n"
    "        if number is None:\n"
    "            return False\n"
)
_STRIP_LINE = (
    "        stripped = list(tokens)\n"
    "        stripped[-1] = stripped[-1].rstrip(_TRAILING_PUNCT)\n"
    '        return " ".join(token for token in stripped if token)\n'
)
# The consume's own parse; its twelve-space indent keeps it apart from the
# hold's eight-space line above.
# wh-overlay-count-homophones.1.5: 97567e82 added aliases=True and
# reflowed this call too.
_CONSUME_PARSE = (
    "            number = parse_number_word(\n"
    "                self._spoken_number_text(tokens[1:]), aliases=True,\n"
    "            )\n"
)
PUNCTUATED = [
    f"{CLICKS_PUNCT}[words0]",
    f"{CLICKS_PUNCT}[words1]",
    f"{CLICKS_PUNCT}[words2]",
    f"{CLICKS_PUNCT}[words3]",
    f"{CLICKS_PUNCT}[words4]",
    "test_a_punctuated_click_that_does_not_match_keeps_its_punctuation",
]
# wh-overlay-count-homophones.1.5: f4e91316 extracted this comparison
# into _payload_is_the_whole_utterance. The mutation moves to the
# spoken-click hold's CALL of that helper rather than the helper's
# body, so it stays scoped to this hold exactly as before -- the
# helper now serves the multi-word badge check too, and that path is
# tested in tests/test_grid_number_badge_click.py.
_WHOLE_CHECK = (
    "        if not self._payload_is_the_whole_utterance(text):\n"
    "            return False\n"
)
# The overlay check and the hold line are byte-identical in the bare-number
# hold; the spoken-click log line that follows makes this pattern unique.
_HOLD_AND_LOG = (
    "        self._pending_bare_number_words = [text]\n"
    "        pipeline_logger.info(\n"
    '            "BARE-NUMBER candidate held from spoken click text=%r "\n'
)
_OVERLAY_CHECK_AND_HOLD = (
    "        if not self._overlay_accepts_bare_number():\n"
    "            return False\n"
    + _HOLD_AND_LOG
)
# wh-number-badge-problems.1.6: the release deadline for a held spoken
# click. ARMED names the tests that need a sentinel to reach the queue at
# all; RELEASED names the one that needs the sentinel to type the words.
ARMED = [
    "test_a_held_spoken_click_arms_a_release_deadline",
    "test_the_release_deadline_dictates_the_held_spoken_click",
    "test_the_end_marker_still_clicks_and_the_deadline_adds_nothing",
]
RELEASED = ["test_the_release_deadline_dictates_the_held_spoken_click"]
SCOPED_TO_SPOKEN_CLICK = ["test_a_bare_number_hold_arms_no_release_deadline"]
_IS_SPOKEN_CLICK_RETURN = (
    '        tokens = " ".join(held).split()\n'
    "        return (\n"
    "            len(tokens) >= 2\n"
    "            and tokens[0].casefold() in self._SPOKEN_CLICK_VERBS\n"
    "        )\n"
)
FALLBACKS = [
    "test_a_later_word_flushes_the_spoken_click_as_it_was_spoken",
    "test_the_next_utterance_flushes_a_spoken_click_that_lost_its_end_marker",
    "test_the_overlay_closing_between_hold_and_consume_types_the_spoken_click",
    "test_a_click_pattern_that_does_not_match_types_the_spoken_click",
]

MUTATIONS = [
    {
        "name": "handoff-dropped",
        "old": (
            '        if " " in text.strip():\n'
            "            return self._maybe_hold_spoken_click(text)\n"
        ),
        "new": (
            '        if " " in text.strip():\n'
            "            return False\n"
        ),
        # wh-overlay-count-homophones.1.5. The two CLICKS cases this
        # named no longer fail under the mutation, and the behaviour they
        # assert is still correct: f4e91316 added a second badge-click
        # path in the DICTATE branch, so "click 74" still clicks badge 74
        # when the handoff is gone. Measured with the gate skill's
        # masking check -- handoff-dropped alone leaves all five CLICKS
        # cases passing; handoff-dropped plus a disabled .1.3 check fails
        # all five. The shipped order is unchanged, because the hold runs
        # before that check and takes the payload first. The catchers
        # therefore move to two tests only the hold can satisfy: one that
        # the hold opened at all, and one that it kept the words as
        # spoken.
        "expect": [
            "test_a_held_spoken_click_arms_a_release_deadline",
            "test_a_later_word_flushes_the_spoken_click_as_it_was_spoken",
        ],
    },
    {
        "name": "verb-tuple-widened",
        "old": '    _SPOKEN_CLICK_VERBS = ("click", "tap")\n',
        "new": '    _SPOKEN_CLICK_VERBS = ("click", "tap", "press")\n',
        "expect": [f"{DICTATES}[words5]"],
    },
    {
        "name": "verb-check-dropped",
        "old": _VERB_CHECK,
        "new": _VERB_CHECK.replace("return False", "pass"),
        "expect": [f"{DICTATES}[words5]"],
    },
    {
        "name": "range-check-bypassed",
        "old": _PARSE_CHECK,
        "new": _PARSE_CHECK.replace("return False", "number = 74"),
        "expect": [
            f"{DICTATES}[words0]",
            f"{DICTATES}[words1]",
            f"{DICTATES}[words2]",
            f"{DICTATES}[words3]",
            f"{DICTATES}[words4]",
        ],
    },
    {
        "name": "filler-alone-accepted",
        "old": _PARSE_CHECK,
        "new": _PARSE_CHECK.replace(
            "        if number is None:\n",
            "        if number is None and tokens[-1].casefold() not in _NUMBER_FILLERS:\n",
        ),
        "expect": [f"{DICTATES}[words4]"],
    },
    {
        "name": "whole-utterance-dropped",
        "old": _WHOLE_CHECK,
        "new": _WHOLE_CHECK.replace("return False", "pass"),
        "expect": ["test_the_spoken_click_must_be_the_whole_utterance"],
    },
    {
        "name": "overlay-check-dropped",
        "old": _OVERLAY_CHECK_AND_HOLD,
        "new": _OVERLAY_CHECK_AND_HOLD.replace("return False", "pass"),
        "expect": ["test_the_spoken_click_dictates_when_the_overlay_is_closed"],
    },
    {
        "name": "held-digits-only",
        "old": _HOLD_AND_LOG,
        "new": _HOLD_AND_LOG.replace("[text]", "[str(number)]"),
        "expect": FALLBACKS,
    },
    {
        "name": "consume-uses-spoken-words",
        "old": '            return None if number is None else f"click {number}"\n',
        "new": (
            "            return None if number is None else "
            "f\"click {' '.join(tokens[1:])}\"\n"
        ),
        "expect": [f"{CLICKS}[words1]", f"{CLICKS}[words3]", f"{CLICKS}[words4]"],
    },
    {
        "name": "punctuation-strip-dropped",
        "old": _STRIP_LINE,
        "new": _STRIP_LINE.replace(
            "        stripped[-1] = stripped[-1].rstrip(_TRAILING_PUNCT)\n", ""
        ),
        "expect": PUNCTUATED,
    },
    {
        "name": "bare-hold-skips-strip",
        # wh-overlay-count-homophones.1.5: 97567e82 put a comment
        # block between "if (" and the parse, and added aliases=True
        # to it, so the old three-line span matched 0 times. The
        # mutation now names only the line it changes.
        "old": "        bare = self._spoken_number_text([text])\n",
        "new": "        bare = text.strip()\n",
        "expect": BARE_PUNCT_FIRST_WORD,
    },
    {
        "name": "bare-growth-skips-strip",
        # wh-overlay-count-homophones.1.5: 97567e82 added aliases=True.
        "old": (
            "        return parse_number_word(\n"
            "            self._spoken_number_text(held + [text]), aliases=True,\n"
            "        ) is not None\n"
        ),
        "new": (
            "        return parse_number_word(\n"
            '            " ".join(held + [text]), aliases=True,\n'
            "        ) is not None\n"
        ),
        "expect": [f"{BARE_PUNCT}[words-question]"],
    },
    {
        "name": "bare-consume-skips-strip",
        # wh-overlay-count-homophones.1.5: 97567e82 added aliases=True
        # and reflowed this call across three lines.
        "old": (
            "        if parse_number_word(\n"
            "            self._spoken_number_text(held), aliases=True,\n"
            "        ) is None:\n"
        ),
        "new": (
            "        if parse_number_word(\n"
            '            " ".join(held), aliases=True,\n'
            "        ) is None:\n"
        ),
        "expect": BARE_PUNCT_ALL,
    },
    {
        "name": "consume-skips-punctuation",
        "old": _CONSUME_PARSE,
        "new": (
            "            number = parse_number_word(\n"
            '                " ".join(tokens[1:]), aliases=True,\n'
            "            )\n"
        ),
        "expect": PUNCTUATED,
    },
    {
        # wh-number-badge-problems.1.6. `pass` rather than a deletion:
        # the call is not the only statement in its block today, but a
        # later edit could make it one, and an empty block is the
        # does-not-compile false pass the gate skill warns about.
        "name": "hold-arms-no-release-deadline",
        "old": "                self._arm_spoken_click_release()\n",
        "new": "                pass\n",
        "expect": ARMED,
    },
    {
        # The sentinel still reaches the queue, so only the test that
        # reads what the sentinel DOES can catch this one.
        "name": "sentinel-skips-the-spoken-click-release",
        "old": (
            "            if self._is_spoken_click_hold("
            "self._pending_bare_number_words):\n"
        ),
        "new": "            if False:\n",
        "expect": RELEASED,
    },
    {
        # The scope limit codex round 6 asked for: the bare-number hold
        # waits the same way and is a separate, pre-existing question,
        # so arming it here would be a behaviour change out of scope.
        "name": "release-deadline-covers-the-bare-hold-too",
        "old": _IS_SPOKEN_CLICK_RETURN,
        "new": (
            '        tokens = " ".join(held).split()\n'
            "        return len(tokens) >= 1\n"
        ),
        "expect": SCOPED_TO_SPOKEN_CLICK,
    },
]


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
    summary form. A collection error that names a file and not a test
    still matches the summary form, which is what run_defect needs.
    """
    return {
        _test_id(line)
        for line in output.splitlines()
        if line.startswith("ERROR ") or ("::" in line and " ERROR" in line)
    }


def run_defect(result):
    """A reason this run cannot yield a verdict, or None.

    wh-number-badge-problems.1.5 (codex round 5): the caught-or-survived
    decision reads FAILED records, so any run that neither passed nor
    simply failed its tests must abort as an error instead. A collection
    error prints no FAILED line and would have read as a survivor; an
    unrelated ERROR beside a real catcher failure would have read as a
    catch. Takes anything with returncode, stdout and stderr.

    The baseline needs no separate call: it already refuses on any
    non-zero exit, which covers every case below except a stderr write
    on an otherwise clean run.
    """
    if "+++ Timeout +++" in result.stdout:
        return "suite-timeout-abort"
    if result.returncode not in (0, 1):
        return f"unexpected pytest exit status {result.returncode}"
    errored = sorted(error_names(result.stdout))
    if errored:
        return f"pytest reported ERROR records: {errored}"
    if result.stderr.strip():
        return f"pytest wrote to stderr: {result.stderr.strip().splitlines()[0]!r}"
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


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    original = SRC.read_bytes()
    text = original.decode("utf-8")
    newline = _newline_of(original)
    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test ids in {' + '.join(CLASSES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
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
    expected_all = {name for mut in MUTATIONS for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(f"ERROR expected catchers skipped in the baseline: {skipped_catchers}")
        print("      a skipped catcher can never fail, so its mutation cannot be proven")
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        mutated, error = _build_mutant(text, newline, mut)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        _clear_bytecode()
        SRC.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(TEST_FILE, *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            SRC.write_bytes(original)
            _clear_bytecode()
        defect = run_defect(result)
        if defect is not None:
            errors.append(f"{mut['name']}: {defect}")
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
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
        else:
            caught.append(mut["name"])
            print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
