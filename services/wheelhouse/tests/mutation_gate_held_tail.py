"""Mutation gate for the single held tail (Stage 2 of
wh-whole-utterance-command-matching).

Run it from services/wheelhouse:

    python tests/mutation_gate_held_tail.py

Stage 2 replaces the three hold-slot attributes with one kind-tagged
slot, ``SpeechProcessor._held_tail``, behind compatibility properties,
one top-of-loop advance (``_advance_held_tail``), one unified flush
(``_flush_held_tail_as_dictation``), and one end-of-utterance consume
(``_consume_held_tail_at_utterance_end``). The refactor's risk is the
new dispatch layer: a wrong kind check, a dropped guard, or a flipped
dispatch would preserve the structure tests while changing behaviour.
So the catchers here are drawn from BOTH the new structure tests
(tests/test_held_tail.py) and the absorbed behaviour suites
(tests/test_speech_processor_trailing_command.py,
tests/test_speech_processor_bare_number.py).

Nine mutations of speech/speech_processor.py:

  clear-ignores-kind      A None-assignment clears the tail whatever
                          kind it holds. Every kind flush method ends
                          by assigning None to its own name, so a
                          kind-blind clear lets one kind's flush drop
                          another kind's held words.
  getter-ignores-kind     Reading any old attribute name returns the
                          held words whatever the kind. Pins the claim
                          the pre-implementation state already
                          satisfied by accident (recorded on the bead:
                          test_cross_kind_read_is_none passed before
                          the implementation because the old
                          independent attributes were None).
  eviction-log-dropped    Cross-kind arming overwrites silently. The
                          loud eviction is the tripwire for a future
                          event type that forgets the unified flush.
  advance-trailing-noop   The advance dispatch stops flushing a held
                          trailing word on a following event; the word
                          then wrongly fires as a command at the end
                          marker instead of dictating.
  advance-consumes-event  The trailing advance claims the arriving
                          event after flushing, swallowing the
                          following word.
  start-guard-dropped     The bare-number extend check stops requiring
                          a non-opening word at its NEW location inside
                          _advance_held_tail; two utterances fuse into
                          one wrong click (deepseek round 1,
                          wh-click-number-dictation.1.1 -- the guard
                          this refactor moved).
  end-marker-advances     The top-of-loop guard stops excluding the
                          utterance-end marker, so the tail flushes as
                          dictation instead of consuming (no Enter
                          press, no click).
  consume-dispatch-flip   The end-of-utterance consume dispatches the
                          trailing kind to its dictation flush instead
                          of firing the action.
  unified-flush-flip      The unified flush dispatches the prefix kind
                          to the trailing flush (a cross-kind no-op),
                          leaving the held words unsent.

All nine must be caught, and "caught" means the expected test failed on
its own assertion. Reported as errors, never as a verdict:
pattern-not-found, an ambiguous pattern, a mutation that does not
compile, a per-mutation timeout, a suite-timeout abort, any pytest
return code other than 0 or 1, any ERROR line in the summary, and an
expected test that failed for any reason other than its own assertion.

The gate detects the target file's own line endings and translates each
pattern to them before matching. It never rewrites the file's endings.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "speech" / "speech_processor.py"

HELD_TAIL = "tests/test_held_tail.py"
TRAILING = "tests/test_speech_processor_trailing_command.py"
BARE = "tests/test_speech_processor_bare_number.py"
ALL_FILES = [HELD_TAIL, TRAILING, BARE]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    {
        "name": "clear-ignores-kind",
        "files": [HELD_TAIL],
        "old": """        if words is None:
            if tail is not None and tail.kind is kind:
                self._held_tail = None
            return
""",
        "new": """        if words is None:
            if tail is not None:
                self._held_tail = None
            return
""",
        "expect": ["test_cross_kind_none_set_preserves_tail"],
    },
    {
        "name": "getter-ignores-kind",
        "files": [HELD_TAIL],
        "old": """        tail = self._held_tail
        if tail is not None and tail.kind is kind:
            return tail.words
        return None
""",
        "new": """        tail = self._held_tail
        if tail is not None:
            return tail.words
        return None
""",
        "expect": ["test_cross_kind_read_is_none"],
    },
    {
        # Pattern refreshed after the .2.1 redaction fix changed the
        # logged argument.
        "name": "eviction-log-dropped",
        "files": [HELD_TAIL],
        "old": """        if tail is not None and tail.kind is not kind:
            pipeline_logger.error(
                "held tail collision: arming %s while %s holds %s -- "
                "evicting the held words. No live arming sequence does "
                "this; a new event type is missing its "
                "_flush_held_tail_as_dictation call.",
                kind.name,
                tail.kind.name,
                redact_transcript(" ".join(tail.words)),
            )
""",
        "new": """        if tail is not None and tail.kind is not kind:
            pass
""",
        "expect": ["test_arming_second_kind_evicts_and_logs"],
    },
    {
        # deepseek finding wh-whole-utterance-command-matching.2.1: the
        # tripwire is the one site whose job is to fire when held words
        # meet an unexpected arming sequence, so it must not put user
        # speech in the pipeline log.
        "name": "eviction-log-unredacted",
        "files": [HELD_TAIL],
        "old": """                kind.name,
                tail.kind.name,
                redact_transcript(" ".join(tail.words)),
""",
        "new": """                kind.name,
                tail.kind.name,
                " ".join(tail.words),
""",
        "expect": ["test_eviction_log_redacts_the_held_words"],
    },
    {
        "name": "advance-trailing-noop",
        "files": [TRAILING],
        "old": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
            return False
""",
        "new": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            return False
""",
        "expect": ["test_submit_followed_by_more_words_is_dictated_verbatim"],
    },
    {
        "name": "advance-consumes-event",
        "files": [TRAILING],
        "old": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
            return False
""",
        "new": """        if tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
            return True
""",
        "expect": ["test_submit_followed_by_more_words_is_dictated_verbatim"],
    },
    {
        "name": "start-guard-dropped",
        "files": [BARE],
        "old": """        if (
            not word_event.start_of_utterance
            and self._bare_number_extends(word_event.word)
        ):
""",
        "new": """        if (
            self._bare_number_extends(word_event.word)
        ):
""",
        "expect": ["test_a_new_utterance_word_never_joins_the_previous_number"],
    },
    {
        "name": "end-marker-advances",
        "files": [TRAILING, BARE],
        # 7a34abb5 added a fourth condition, the lifecycle-reset
        # exclusion, so the three-condition anchor stopped matching and
        # this mutation reported pattern-not-found in every run since
        # 2026-08-30. The mutation is unchanged: it still removes only
        # the utterance-end exclusion.
        "old": """        if (
            self._held_tail is not None
            and not word_event.is_utterance_end_marker
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_lifecycle_reset_marker
        ):
            if await self._advance_held_tail(word_event):
                return
""",
        "new": """        if (
            self._held_tail is not None
            and not word_event.is_timeout_finalize_marker
            and not word_event.is_lifecycle_reset_marker
        ):
            if await self._advance_held_tail(word_event):
                return
""",
        "expect": [
            "test_lone_submit_fires_action_with_no_dictation",
            "test_bare_digit_final_executes_click",
        ],
    },
    {
        "name": "consume-dispatch-flip",
        "files": [TRAILING],
        "old": """        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._consume_pending_trailing_word_at_utterance_end()
""",
        "new": """        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
""",
        "expect": ["test_lone_submit_fires_action_with_no_dictation"],
    },
    {
        "name": "unified-flush-flip",
        "files": [HELD_TAIL],
        "old": """        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            await self._flush_pending_replacement_prefix_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
        else:
            await self._flush_pending_bare_number_as_dictation()
""",
        "new": """        if tail.kind is _HeldTailKind.REPLACEMENT_PREFIX:
            await self._flush_pending_trailing_word_as_dictation()
        elif tail.kind is _HeldTailKind.TRAILING_COMMAND:
            await self._flush_pending_trailing_word_as_dictation()
        else:
            await self._flush_pending_bare_number_as_dictation()
""",
        "expect": ["test_unified_flush_prefix_joins_words"],
    },
]


def _pytest(*extra):
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def collect_names():
    """Real test names in the files, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert statement."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped."""
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing parameter."""
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_ERROR_SUMMARY = re.compile(r"^ERROR\s+\S+\.py(::\S+)?(\s|$)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed."""
    return [
        line for line in output.splitlines() if _ERROR_SUMMARY.match(line)
    ]


def main():
    original = SRC.read_bytes()
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8")
    for mut in MUTATIONS:
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"line endings in {SRC.name}: {'CRLF' if newline == chr(13) + chr(10) else 'LF'}")
    print(f"collected {len(real_names)} test names in {', '.join(ALL_FILES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*ALL_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")

    for mut in MUTATIONS:
        count = text.count(mut["old"])
        if count != 1:
            errors.append(f"{mut['name']}: pattern matched {count} times, expected 1")
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        try:
            compile(mutated, str(SRC), "exec")
        except SyntaxError as exc:
            errors.append(f"{mut['name']}: mutated source does not compile: {exc}")
            print("ERROR", errors[-1])
            continue
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
