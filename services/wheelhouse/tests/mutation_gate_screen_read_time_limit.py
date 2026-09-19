"""Mutation gate for the screen read's own time limit
(wh-overlay-slow-uia-stale-badges.3).

Run it from services/wheelhouse:

    python tests/mutation_gate_screen_read_time_limit.py            # full sweep
    python tests/mutation_gate_screen_read_time_limit.py --check    # patterns only
    python tests/mutation_gate_screen_read_time_limit.py --only a,b  # a subset

The change adds one [click] key, screen_read_timeout_ms (default 10000,
an int in [100, 60000], NOT validated against response_timeout_ms), and
derives four things from it: the Input-side read deadline
(ClickConfig.screen_read_walk_deadline_ms, the key minus the 250 ms
pre-walk margin, floored at 100), the Logic awaiter for every
start_overlay_walk build (AUTO_OPEN keeps response_timeout_ms), the GUI
walking-cue bound (build window plus the pin-ack window, plus the
sentence-end wait for every build but AUTO_OPEN), the overlay state
machine's walk and settle deadlines (key plus the margin, the settle one
also plus the sentence-end wait and never below its class default,
wh-overlay-slow-uia-stale-badges.3.1.1), and the dictation gate's
refusal cap (max(10 s floor, key in seconds + 2 s slack)). The new tests
were written red-first; this gate proves each catcher can see its defect,
and supplies the red for the guards that were green before the change
(the walk_deadline_ms cross-key rule, the cap's upper side, the cap's
floor, and the Mock guard).

Mutations, by target file:

ui/click_config.py:
  read-limit-coupled-to-reply-limit
                          The old coupling comes back: the key is
                          validated below response_timeout_ms, so 8000
                          against 3000 disables clicking. Acceptance 2.
  read-limit-floor-dropped   99 is accepted.
  read-limit-ceiling-raised  60001 is accepted.
  read-limit-default-changed The missing-key default is 3000, not 10000.
  read-deadline-margin-dropped
                          The Input-side bound equals the key instead of
                          the key minus the pre-walk margin.
  read-deadline-floor-dropped
                          A 100 ms key yields a -150 ms Input bound.

ui/ui_action_handler.py:
  read-deadline-uses-click-walk-bound
                          start_overlay_walk anchors the by-name click's
                          walk_deadline_ms (2500) again instead of the
                          read's own bound.

main.py:
  build-awaits-reply-limit-for-reads
                          A start_overlay_walk build is awaited for
                          response_timeout_ms again; a correct 7 s read
                          is discarded at 3 s. Acceptance 3.
  auto-open-awaits-read-limit
                          AUTO_OPEN (a store lookup, no walk) is awaited
                          for the read limit instead of the reply limit.
  cue-bound-walk-only     The walking cue carries the build window alone,
                          not build plus pin ack.
  cue-bound-omits-sentence-wait
                          The walking cue loses the sentence-end wait
                          term while the AUTO_OPEN value keeps its (absent)
                          one, so the GUI fallback can clear the dot during
                          a legitimate mid-sentence walk.
  machine-settle-deadline-not-derived
                          The settle state keeps its 8000 default, so a
                          settle read answering at 9 s is dropped by the
                          state while the awaiter still runs.
  machine-settle-deadline-omits-sentence-wait
                          The settle deadline loses the sentence-end wait
                          term, so a settle read still inside its own
                          bound is dropped whenever the settle build held
                          for an open dictation sentence first.
  machine-walk-deadline-not-derived
                          The walk state keeps its 2500 default.
  machine-deadlines-no-margin
                          The machine deadlines equal the key instead of
                          the key plus the pre-walk margin.

speech/speech_processor.py:
  gate-cap-ignores-read-limit
                          The cap stays at the 10 s floor whatever the
                          key says; a 20 s read stops refusing at 10 s.
  gate-cap-never-expires  The cap is unbounded once a limit is
                          configured. Red proof for the green-before
                          upper-side guard.
  gate-cap-floor-dropped  The cap follows a 3 s key down to 5 s. Red
                          proof for the green-before floor guard.
  gate-cap-mock-guard-dropped
                          The isinstance guard becomes a None check; a
                          MagicMock click_config reaches the max() over a
                          Mock and raises TypeError. The expected catch
                          IS that raise: the guard exists to prevent it.

Not mutated, and why: the ``not isinstance(limit_ms, bool)`` clause has
no observable effect under the floor (True reads as 1 ms, and
max(10.0, 2.001) is the floor either way), so no test can pin it; it
stays for the same reason the ``since`` guard excludes bool. The
comment-only edits in input_proc.py and click_overlay_state.py and the
help text carry no behaviour.

Each mutation runs only the test files that hold its catchers
(``selection``), so a sweep costs about a minute, not the whole suite
per mutation. Every distinct selection is run unmutated first and must
be green. The runner requires each pattern to match exactly once,
compiles every mutant before writing it, clears the target modules'
bytecode caches around every run, sets PYTHONDONTWRITEBYTECODE=1, gives
each pytest run its own timeout, restores the source with write_bytes in
a finally that survives a second Ctrl+C, and reports as ERRORS (never
verdicts): pattern-not-found, pattern-ambiguous, does-not-compile,
timeout, suite-timeout abort, any pytest return code other than 0 or 1,
any ERROR line in the summary, an expected test name that does not
exist, and an expected test that failed for any reason other than its
own assertion (or the mutation's allowed raise).

``--check`` verifies every pattern matches exactly once and every
mutant compiles, without running a test, and prints
"checked N patterns, S stale, C that do not compile".

The gate detects each target file's own line endings and translates the
patterns to them before matching. It never rewrites a file's endings.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
CFG = SERVICE / "ui" / "click_config.py"
UAH = SERVICE / "ui" / "ui_action_handler.py"
MAIN = SERVICE / "main.py"
SP = SERVICE / "speech" / "speech_processor.py"
TARGETS = (CFG, UAH, MAIN, SP)

JOURNEY_7 = (
    "tests/test_overlay_journeys.py::"
    "test_journey_7_a_slow_read_types_nothing_into_the_window_that_took_over"
)
S_CFG = ["tests/test_click_config.py"]
S_CFG_J = S_CFG + [JOURNEY_7]
S_HANDLER = ["tests/test_ui/test_start_overlay_walk_handler.py"]
S_CFG_H = S_CFG + S_HANDLER
S_INT = ["tests/test_logic_overlay_integration.py"]
S_INT_J = S_INT + [JOURNEY_7]
S_GATE = ["tests/test_screen_read_dictation_gate.py"]

PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]
PER_RUN_TIMEOUT_S = 300

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATIONS = [
    # ---- ui/click_config.py -------------------------------------------
    {
        # Acceptance 2: the read limit is not forced below the reply limit.
        "name": "read-limit-coupled-to-reply-limit",
        "src": CFG,
        "old": (
            '                "screen_read_timeout_ms",\n'
            "                _is_int_in_range(100, _SCREEN_READ_TIMEOUT_MAX_MS),\n"
        ),
        "new": (
            '                "screen_read_timeout_ms",\n'
            "                _is_int_in_range(100, response_timeout_ms - 1),\n"
        ),
        "selection": S_CFG_J,
        "expect": [
            "test_screen_read_timeout_above_the_reply_limit_is_accepted",
            "test_journey_7_a_slow_read_types_nothing_into_the_window_that_took_over",
        ],
    },
    {
        "name": "read-limit-floor-dropped",
        "src": CFG,
        "old": "                _is_int_in_range(100, _SCREEN_READ_TIMEOUT_MAX_MS),\n",
        "new": "                _is_int_in_range(0, _SCREEN_READ_TIMEOUT_MAX_MS),\n",
        "selection": S_CFG,
        "expect": ["test_screen_read_timeout_below_floor_disables"],
    },
    {
        "name": "read-limit-ceiling-raised",
        "src": CFG,
        "old": "_SCREEN_READ_TIMEOUT_MAX_MS = 60000\n",
        "new": "_SCREEN_READ_TIMEOUT_MAX_MS = 70000\n",
        "selection": S_CFG,
        "expect": ["test_screen_read_timeout_above_ceiling_disables"],
    },
    {
        "name": "read-limit-default-changed",
        "src": CFG,
        "old": '    "screen_read_timeout_ms": 10000,\n',
        "new": '    "screen_read_timeout_ms": 3000,\n',
        "selection": S_CFG,
        "expect": ["test_screen_read_timeout_default_when_missing"],
    },
    {
        "name": "read-deadline-margin-dropped",
        "src": CFG,
        "old": (
            "        return max(\n"
            "            _WALK_DEADLINE_FLOOR_MS,\n"
            "            self.screen_read_timeout_ms - _WALK_DEADLINE_MARGIN_MS,\n"
            "        )\n"
        ),
        "new": (
            "        return max(\n"
            "            _WALK_DEADLINE_FLOOR_MS,\n"
            "            self.screen_read_timeout_ms,\n"
            "        )\n"
        ),
        "selection": S_CFG_H,
        "expect": [
            "test_screen_read_walk_deadline_subtracts_the_pre_walk_margin",
            "test_screen_read_deadline_uses_its_own_key_not_the_click_walk_bound",
        ],
    },
    {
        "name": "read-deadline-floor-dropped",
        "src": CFG,
        "old": (
            "        return max(\n"
            "            _WALK_DEADLINE_FLOOR_MS,\n"
            "            self.screen_read_timeout_ms - _WALK_DEADLINE_MARGIN_MS,\n"
            "        )\n"
        ),
        "new": (
            "        return self.screen_read_timeout_ms - _WALK_DEADLINE_MARGIN_MS\n"
        ),
        "selection": S_CFG,
        "expect": ["test_screen_read_walk_deadline_floors_at_100"],
    },
    # ---- ui/ui_action_handler.py --------------------------------------
    {
        "name": "read-deadline-uses-click-walk-bound",
        "src": UAH,
        "old": (
            '                "screen_read_walk_deadline_ms",\n'
            "                None,\n"
            "            )\n"
        ),
        "new": (
            '                "walk_deadline_ms",\n'
            "                None,\n"
            "            )\n"
        ),
        "selection": S_HANDLER,
        "expect": [
            "test_screen_read_deadline_uses_its_own_key_not_the_click_walk_bound",
            "test_walk_deadline_anchored_at_command_dequeue",
            "test_walk_deadline_falls_back_to_handler_entry_without_dequeue",
        ],
    },
    # ---- main.py -------------------------------------------------------
    {
        # Acceptance 3: a read finishing after the reply limit is used.
        "name": "build-awaits-reply-limit-for-reads",
        "src": MAIN,
        "old": (
            "        else:\n"
            "            timeout_ms = self.click_config.screen_read_timeout_ms\n"
        ),
        "new": (
            "        else:\n"
            "            timeout_ms = self.click_config.response_timeout_ms\n"
        ),
        "selection": S_INT_J,
        "expect": [
            "test_screen_read_build_awaits_its_own_key_and_auto_open_awaits_the_reply_limit",
            "test_a_read_that_answers_after_the_reply_limit_but_inside_the_read_limit_is_applied",
            "test_journey_7_a_slow_read_types_nothing_into_the_window_that_took_over",
        ],
    },
    {
        "name": "auto-open-awaits-read-limit",
        "src": MAIN,
        "old": (
            "        if effect.build_reason is BuildReason.AUTO_OPEN:\n"
            "            timeout_ms = self.click_config.response_timeout_ms\n"
        ),
        "new": (
            "        if effect.build_reason is BuildReason.AUTO_OPEN:\n"
            "            timeout_ms = self.click_config.screen_read_timeout_ms\n"
        ),
        "selection": S_INT,
        "expect": [
            "test_screen_read_build_awaits_its_own_key_and_auto_open_awaits_the_reply_limit",
        ],
    },
    {
        "name": "cue-bound-walk-only",
        "src": MAIN,
        "old": (
            "        cue_bound_ms = timeout_ms "
            "+ self.click_config.response_timeout_ms\n"
        ),
        "new": "        cue_bound_ms = timeout_ms\n",
        "selection": S_INT,
        "expect": [
            "test_overlay_walk_cue_emitted_active_true_at_walk_start",
            "test_overlay_walk_cue_walk_timeout_covers_walk_plus_pin",
        ],
    },
    {
        # wh-overlay-slow-uia-stale-badges.3.1.1. The AUTO_OPEN arm is
        # untouched, so test_overlay_walk_cue_auto_open_bound_carries_no_
        # sentence_wait stays green here as the control.
        "name": "cue-bound-omits-sentence-wait",
        "src": MAIN,
        "old": (
            "        if effect.build_reason is not BuildReason.AUTO_OPEN:\n"
            "            cue_bound_ms += "
            "int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)\n"
        ),
        "new": (
            "        if effect.build_reason is not BuildReason.AUTO_OPEN:\n"
            "            cue_bound_ms += 0\n"
        ),
        "selection": S_INT,
        "expect": [
            "test_overlay_walk_cue_emitted_active_true_at_walk_start",
            "test_overlay_walk_cue_walk_timeout_covers_walk_plus_pin",
        ],
    },
    {
        "name": "machine-settle-deadline-not-derived",
        "src": MAIN,
        "old": (
            "        settle_deadline_ms=max(\n"
            "            ClickOverlayStateMachine.settle_deadline_ms,\n"
            "            read_bound_ms + wait_ms,\n"
            "        ),\n"
        ),
        "new": (
            "        settle_deadline_ms=ClickOverlayStateMachine.settle_deadline_ms,\n"
        ),
        "selection": S_INT,
        "expect": ["test_overlay_machine_deadlines_follow_the_screen_read_key"],
    },
    {
        # wh-overlay-slow-uia-stale-badges.3.1.1: the derivation keeps the
        # margin but loses the sentence-end wait the settle build spends
        # before its send.
        "name": "machine-settle-deadline-omits-sentence-wait",
        "src": MAIN,
        "old": (
            "        settle_deadline_ms=max(\n"
            "            ClickOverlayStateMachine.settle_deadline_ms,\n"
            "            read_bound_ms + wait_ms,\n"
            "        ),\n"
        ),
        "new": (
            "        settle_deadline_ms=max(\n"
            "            ClickOverlayStateMachine.settle_deadline_ms,\n"
            "            read_bound_ms,\n"
            "        ),\n"
        ),
        "selection": S_INT,
        "expect": ["test_overlay_machine_deadlines_follow_the_screen_read_key"],
    },
    {
        "name": "machine-walk-deadline-not-derived",
        "src": MAIN,
        "old": "        walk_deadline_ms=read_bound_ms,\n",
        "new": "        walk_deadline_ms=ClickOverlayStateMachine.walk_deadline_ms,\n",
        "selection": S_INT,
        "expect": [
            "test_overlay_machine_deadlines_follow_the_screen_read_key",
            "test_a_read_that_answers_after_the_reply_limit_but_inside_the_read_limit_is_applied",
        ],
    },
    {
        "name": "machine-deadlines-no-margin",
        "src": MAIN,
        "old": (
            "    read_bound_ms = click_config.screen_read_timeout_ms "
            "+ PRE_WALK_MARGIN_MS\n"
        ),
        "new": "    read_bound_ms = click_config.screen_read_timeout_ms\n",
        "selection": S_INT,
        "expect": ["test_overlay_machine_deadlines_follow_the_screen_read_key"],
    },
    # ---- speech/speech_processor.py -----------------------------------
    {
        "name": "gate-cap-ignores-read-limit",
        "src": SP,
        "old": (
            "            cap_s = max(\n"
            "                cap_s, limit_ms / 1000.0 + _READ_GATE_REFUSAL_SLACK_S\n"
            "            )\n"
        ),
        "new": "            cap_s = _READ_GATE_MAX_REFUSAL_S\n",
        "selection": S_GATE,
        "expect": ["test_gate_cap_follows_the_configured_screen_read_limit"],
    },
    {
        # Red proof for the green-before upper-side guard.
        "name": "gate-cap-never-expires",
        "src": SP,
        "old": (
            "            cap_s = max(\n"
            "                cap_s, limit_ms / 1000.0 + _READ_GATE_REFUSAL_SLACK_S\n"
            "            )\n"
        ),
        "new": '            cap_s = float("inf")\n',
        "selection": S_GATE,
        "expect": ["test_gate_cap_still_expires_above_the_configured_limit"],
    },
    {
        # Red proof for the green-before floor guard.
        "name": "gate-cap-floor-dropped",
        "src": SP,
        "old": (
            "            cap_s = max(\n"
            "                cap_s, limit_ms / 1000.0 + _READ_GATE_REFUSAL_SLACK_S\n"
            "            )\n"
        ),
        "new": (
            "            cap_s = limit_ms / 1000.0 + _READ_GATE_REFUSAL_SLACK_S\n"
        ),
        "selection": S_GATE,
        "expect": ["test_gate_cap_never_drops_below_the_floor"],
    },
    {
        # The guarded behaviour is "a Mock auto-attribute must not reach
        # the cap arithmetic"; the TypeError is the defect itself, so a
        # raise IS the catch here.
        "name": "gate-cap-mock-guard-dropped",
        "src": SP,
        "old": (
            "        if isinstance(limit_ms, (int, float)) "
            "and not isinstance(limit_ms, bool):\n"
        ),
        "new": "        if limit_ms is not None:\n",
        "selection": S_GATE,
        "expect": ["test_mock_click_config_without_a_read_limit_uses_the_floor"],
        "allow_raise": "TypeError",
    },
]


def _clear_pycache():
    """Drop the target modules' bytecode so a same-size mutant cannot be
    served from a cache compiled from the other version."""
    for target in TARGETS:
        cache = target.parent / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _pytest(*extra):
    _clear_pycache()
    return subprocess.run(
        ["uv", "run", "python", "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_RUN_TIMEOUT_S,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _restore(src, original):
    """Put the original bytes back, surviving a second Ctrl+C.

    A first interrupt reaches the caller's finally; a second one landing
    inside this write would otherwise escape with the mutant still in a
    tracked file. Hold it, retry once, print any failure (an unreported
    mutant is the whole harm), clear bytecode, then re-raise the held
    interrupt.
    """
    held = None
    for _attempt in (1, 2):
        try:
            src.write_bytes(original)
            if src.read_bytes() == original:
                break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR restore of {src.name} failed: {exc}")
    else:
        print(f"ERROR {src.name} may still hold a mutant; restore it by hand")
    _clear_pycache()
    if held is not None:
        raise held


def collect_names(selection):
    """Real test names in a selection, so a renamed test cannot read as a survivor."""
    out = _pytest(*selection, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_acceptable_failure(reason, allow_raise):
    """True for an assertion failure, or the mutation's allowed raise."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    if allow_raise and reason.startswith(allow_raise):
        return True
    if allow_raise and reason.startswith(f"asyncio.{allow_raise}"):
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
    """Every failure reason --tb=line printed, one per failing test."""
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


def _load_texts():
    originals = {path: path.read_bytes() for path in TARGETS}
    texts = {}
    for path, raw in originals.items():
        newline = "\r\n" if b"\r\n" in raw else "\n"
        texts[path] = (raw.decode("utf-8"), newline)
        print(
            f"line endings in {path.name}: "
            f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
        )
    for mut in MUTATIONS:
        newline = texts[mut["src"]][1]
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)
    return originals, texts


def _build_mutant(mut, texts):
    """Return (mutated_text, error) -- error names a stale, ambiguous, or
    non-compiling pattern, and is reported, never counted as a verdict."""
    src = mut["src"]
    text = texts[src][0]
    count = text.count(mut["old"])
    if count != 1:
        return None, f"{mut['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(mut["old"], mut["new"], 1)
    try:
        compile(mutated, str(src), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only():
    _originals, texts = _load_texts()
    stale = 0
    bad = 0
    for mut in MUTATIONS:
        _mutated, err = _build_mutant(mut, texts)
        if err is None:
            continue
        print("ERROR", err)
        if "does not compile" in err:
            bad += 1
        else:
            stale += 1
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{bad} that do not compile"
    )
    return 1 if stale or bad else 0


def main(argv):
    if "--check" in argv:
        return check_only()
    only = None
    for arg in argv:
        if arg.startswith("--only="):
            only = set(arg[len("--only="):].split(","))
    chosen = [m for m in MUTATIONS if only is None or m["name"] in only]
    if only is not None:
        unknown = only - {m["name"] for m in MUTATIONS}
        if unknown:
            print(f"ERROR unknown mutation names in --only: {sorted(unknown)}")
            return 1

    originals, texts = _load_texts()
    errors, caught, survived = [], [], []

    selections = []
    for mut in chosen:
        if mut["selection"] not in selections:
            selections.append(mut["selection"])
    names_by_selection = {}
    for selection in selections:
        names = collect_names(selection)
        names_by_selection[tuple(selection)] = names
        print(f"collected {len(names)} test names in {', '.join(selection)}")
    for mut in chosen:
        real_names = names_by_selection[tuple(mut["selection"])]
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(
                    f"{mut['name']}: expected test {name!r} does not exist "
                    f"in its selection"
                )
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    for selection in selections:
        baseline = _pytest(*selection, *PYTEST_ARGS)
        if baseline.returncode != 0:
            print(
                f"ERROR baseline is not green for {', '.join(selection)}; "
                f"refusing to start"
            )
            print(baseline.stdout[-2000:])
            return 1
        print(f"baseline green: {', '.join(selection)}")

    for mut in chosen:
        src = mut["src"]
        mutated, err = _build_mutant(mut, texts)
        if err is not None or mutated is None:
            errors.append(err or f"{mut['name']}: no mutant built")
            print("ERROR", errors[-1])
            continue
        src.write_bytes(mutated.encode("utf-8"))
        try:
            result = _pytest(*mut["selection"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            _restore(src, originals[src])
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; "
                f"no verdict"
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
        allow = mut.get("allow_raise")
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_acceptable_failure(r, allow)]
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
    skipped = len(MUTATIONS) - len(chosen)
    print(
        f"scope: ran {len(chosen)} of {len(MUTATIONS)} mutations"
        + (f", skipped {skipped} by --only" if skipped else ", none skipped")
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
