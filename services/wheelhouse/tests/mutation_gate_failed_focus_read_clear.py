"""Mutation gate for the failed focus-read clear
(wh-overlay-failed-focus-read-keeps-numbers).

Run it from services/wheelhouse with the service's own interpreter:

    .venv/Scripts/python.exe tests/mutation_gate_failed_focus_read_clear.py
    .venv/Scripts/python.exe tests/mutation_gate_failed_focus_read_clear.py --check
    .venv/Scripts/python.exe tests/mutation_gate_failed_focus_read_clear.py \
        --only build-failed-arm-falls-back,timeout-arm-falls-back

``--only`` takes a comma-separated list of mutation names and runs just
those; the whole set runs once before the final commit. It refuses an
unknown name rather than running a shorter sweep than the caller asked for.

On 2026-09-24 at 18:10:29 a desktop read after a focus change got no reply,
and Brave's numbers stayed on the desktop until "hide numbers". A failed
build response or a timeout in ``refresh_in_flight`` fell back to the numbers
already on screen, although they belonged to a window no longer in front.

The fix has two halves, and the mutations break each piece of them:

  main.py ``_overlay_mark_visible_window_left`` marks such a failure with
  ``OverlayEvent.visible_window_left`` when
  ``_overlay_refresh_visible_window_is_foreground`` answers False, and
  ``_apply_overlay_event`` calls it before ``machine.apply``.

  click_overlay_state.py ``_visible_numbers_left_behind`` reads that mark
  (except on the microphone auto-hide leg), and the failed-build and TIMEOUT
  arms of ``_on_refresh_in_flight`` then close through
  ``_refresh_window_left_to_closed``: cancel the timer, clear, fire the
  generic standalone failure notice, unpin every pinned snapshot.

  build-failed-arm-falls-back   The failed-build arm ignores the mark.
  timeout-arm-falls-back        The TIMEOUT arm ignores the mark.
  auto-hide-leg-closes          The auto-hide exception is dropped, so a
                                failure while paused by the microphone
                                closes instead of staying paused.
  unknown-foreground-clears     The foreground answer is read for truth, so
                                an unknown foreground (None) marks the event
                                just as a False does. Only the
                                foreground-unknown cases can tell it apart.
  unknown-foreground-clears-window-left-keeps
                                ``is not False`` becomes ``is not None``: an
                                unknown foreground marks, a window-left one
                                does not.
  mark-never-called             ``_apply_overlay_event`` no longer calls the
                                marking helper.
  pair-check-dropped            The pair-equality check in the helper is
                                disabled. EXPECTED TO SURVIVE as an
                                equivalent mutant: ``apply`` rejects a
                                mismatched pair as STALE_GENERATION before
                                any state handler reads the mark, so the only
                                difference is one extra foreground read and
                                one INFO log line. It is kept here so a later
                                change that makes the mark matter for a stale
                                pair shows up as a catch.
  timeout-half-of-failed-check-dropped
                                A TIMEOUT is no longer a failure to the
                                helper, so it is never marked.
  window-left-close-fires-no-notice
                                ``_refresh_window_left_to_closed`` no longer
                                fires the standalone failure notice.
  timeout-arm-closes-through-error-to-closed
                                The TIMEOUT arm closes through
                                ``_error_to_closed(emit_standalone_notice=
                                True)``, which fires an auto-open session's
                                stale ambiguous-click notice instead.

Review finding .1.1 added a third piece: ``_restart_walk`` out of
``paint_in_flight`` clears the paint it abandons, stamped with the painted
generation, before the generation bump and before the new build. Without it,
a failed read after the restart left the gen-0 paint on screen.

  restart-emits-no-clear        The restart clear is removed.
  restart-bumps-before-clear-stamp
                                The generation bump moves ahead of the clear,
                                so the clear carries the new walk's pair.
  restart-clears-after-build    The clear moves after the build.
  restart-clears-without-pin    The pin gate is dropped, so a walk_in_flight
                                restart with nothing painted clears too.

Pattern-not-found, an ambiguous pattern, a mutation that does not compile, a
per-mutation timeout, a suite-timeout abort, a skipped catcher and a missing
expected test name are reported as errors, never as a verdict. A mutant write
that fails is an error too, and a restore that cannot be proved stops the
sweep outright: both targets are live source files, and a mutant left in one
is read as real code by every later run. ``--check`` verifies every pattern
matches exactly once in the current source and that every mutant compiles,
without running a test.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
STATE_SRC = SERVICE / "click_overlay_state.py"
MAIN_SRC = SERVICE / "main.py"

STATE_TESTS = "tests/test_click_overlay_state.py"
INTEGRATION = "tests/test_logic_overlay_integration.py"
ALL_FILES = [STATE_TESTS, INTEGRATION]

# -v prints one line per test, which is what names a SKIPPED catcher; -rf
# prints the short-summary FAILED lines failed_names() reads.
PYTEST_ARGS = ["-p", "no:randomly", "-v", "-rf"]
PER_MUTATION_TIMEOUT_S = 300

# --- source under mutation, quoted exactly -------------------------------
_BUILD_FAILED_ARM = (
    "            if not event.build_ok:\n"
    "                if self._visible_numbers_left_behind(event):\n"
    "                    return self._refresh_window_left_to_closed()\n"
)
_TIMEOUT_ARM = (
    "        if kind is OverlayEventKind.TIMEOUT:\n"
    "            if self._visible_numbers_left_behind(event):\n"
    "                return self._refresh_window_left_to_closed()\n"
)
_LEFT_BEHIND_RETURN = (
    "        return event.visible_window_left and not self.auto_hide_in_flight\n"
)
_WINDOW_LEFT_EFFECTS = (
    "            self._dispatch_clear(),\n"
    "            self._fire_standalone_failure_notice(),\n"
    "        ]\n"
)
_FOREGROUND_CHECK = (
    "        if self._overlay_refresh_visible_window_is_foreground(machine) "
    "is not False:\n"
)
_MARK_CALL = (
    "        event = self._overlay_mark_visible_window_left(machine, event)\n"
)
_PAIR_CHECK = (
    "        if (event.overlay_session_id, event.paint_generation) != (\n"
    "            machine.overlay_session_id, machine.paint_generation\n"
    "        ):\n"
)
_FAILED_CHECK_HEAD = (
    "        failed = event.kind is OverlayEventKind.TIMEOUT or (\n"
)
# The whole body of ``_restart_walk`` (.1.1). Quoted whole so every mutant
# below MOVES a line inside it rather than adding a copy, and so the match is
# unique: ``_error_to_closed`` also calls ``_clear_if_visible``.
_RESTART_HEAD = (
    "        effects: list[Effect] = [self._cancel_timer()]\n"
)
_RESTART_CLEAR = (
    "        clear = self._clear_if_visible()\n"
    "        if clear is not None:\n"
    "            effects.append(clear)\n"
)
_RESTART_UNPIN = (
    "        unpin = self._unpin_current()\n"
    "        if unpin is not None:\n"
    "            effects.append(unpin)\n"
    "        self.pinned_snapshot_id = None\n"
)
_RESTART_BUMP = "        self._bump_generation()\n"
_RESTART_STATE = "        self.state = OverlayState.WALK_IN_FLIGHT\n"
_RESTART_BUILD = "        effects.append(self._dispatch_build(reason))\n"
_RESTART_TAIL = (
    "        effects.append(self._arm_timer(OverlayState.WALK_IN_FLIGHT))\n"
)
_RESTART_BODY = (
    _RESTART_HEAD + _RESTART_CLEAR + _RESTART_UNPIN + _RESTART_BUMP
    + _RESTART_STATE + _RESTART_BUILD + _RESTART_TAIL
)

# --- catcher names -------------------------------------------------------
STATE_BUILD_FAILED = (
    "test_refresh_build_failed_after_window_left_clears_and_closes"
)
STATE_TIMEOUT = "test_refresh_timeout_after_window_left_clears_and_closes"
STATE_TIMEOUT_AFTER_BUILD_OK = (
    "test_refresh_timeout_after_build_ok_and_window_left_unpins_both"
)
STATE_AUTO_OPEN = (
    "test_refresh_failure_after_window_left_in_auto_open_session_skips_"
    "stale_notice"
)
STATE_AUTO_HIDDEN = (
    "test_refresh_failure_after_window_left_while_auto_hidden_stays_paused"
)
_INTEG = "test_refresh_failure_clears_numbers_only_when_visible_window_left"
INTEG_LEFT_BUILD = f"{_INTEG}[window-left-build_failed]"
INTEG_LEFT_TIMEOUT = f"{_INTEG}[window-left-timeout]"
INTEG_UNKNOWN_BUILD = f"{_INTEG}[foreground-unknown-build_failed]"
INTEG_UNKNOWN_TIMEOUT = f"{_INTEG}[foreground-unknown-timeout]"
_STATE_RESTART = "test_paint_restart_clears_abandoned_paint_before_build"
STATE_RESTART_SHOW = f"{_STATE_RESTART}[show-numbers]"
STATE_RESTART_FOCUS = f"{_STATE_RESTART}[focus-change]"
_STATE_FAILED_READ = (
    "test_failed_read_after_focus_change_in_paint_leaves_no_paint"
)
STATE_FAILED_READ_BUILD = f"{_STATE_FAILED_READ}[build_failed]"
STATE_FAILED_READ_TIMEOUT = f"{_STATE_FAILED_READ}[timeout]"
_INTEG_RESTART = (
    "test_failed_read_after_restart_in_paint_in_flight_leaves_no_numbers"
)
INTEG_RESTART_ALL = [
    f"{_INTEG_RESTART}[{kind}-{ending}]"
    for kind in ("focus-change", "show-numbers")
    for ending in ("build_failed", "timeout")
]
INTEG_NEW_WALK_PAINT = (
    "test_new_walk_paint_after_restart_clear_passes_the_gui_gate"
)

MUTATIONS = [
    {
        "name": "build-failed-arm-falls-back",
        "file": STATE_SRC,
        "old": _BUILD_FAILED_ARM,
        "new": "            if not event.build_ok:\n",
        "selection": ALL_FILES,
        "expect": [STATE_BUILD_FAILED, INTEG_LEFT_BUILD],
    },
    {
        "name": "timeout-arm-falls-back",
        "file": STATE_SRC,
        "old": _TIMEOUT_ARM,
        "new": "        if kind is OverlayEventKind.TIMEOUT:\n",
        "selection": ALL_FILES,
        "expect": [
            STATE_TIMEOUT, STATE_TIMEOUT_AFTER_BUILD_OK, STATE_AUTO_OPEN,
            INTEG_LEFT_TIMEOUT,
        ],
    },
    {
        "name": "auto-hide-leg-closes",
        "file": STATE_SRC,
        "old": _LEFT_BEHIND_RETURN,
        "new": "        return event.visible_window_left\n",
        "selection": ALL_FILES,
        "expect": [STATE_AUTO_HIDDEN],
    },
    {
        # Differs from the fix only for a None answer, so only the
        # foreground-unknown cases can catch it.
        "name": "unknown-foreground-clears",
        "file": MAIN_SRC,
        "old": _FOREGROUND_CHECK,
        "new": (
            "        if self._overlay_refresh_visible_window_is_foreground("
            "machine):\n"
        ),
        "selection": [INTEGRATION],
        "expect": [INTEG_UNKNOWN_BUILD, INTEG_UNKNOWN_TIMEOUT],
    },
    {
        "name": "unknown-foreground-clears-window-left-keeps",
        "file": MAIN_SRC,
        "old": _FOREGROUND_CHECK,
        "new": (
            "        if self._overlay_refresh_visible_window_is_foreground(machine) "
            "is not None:\n"
        ),
        "selection": [INTEGRATION],
        "expect": [
            INTEG_LEFT_BUILD, INTEG_LEFT_TIMEOUT,
            INTEG_UNKNOWN_BUILD, INTEG_UNKNOWN_TIMEOUT,
        ],
    },
    {
        # ``pass``, not a deleted line, so every mutation keeps one shape.
        "name": "mark-never-called",
        "file": MAIN_SRC,
        "old": _MARK_CALL,
        "new": "        pass\n",
        "selection": [INTEGRATION],
        "expect": [INTEG_LEFT_BUILD, INTEG_LEFT_TIMEOUT],
    },
    {
        # Expected to survive: see the module docstring. No test is named,
        # so a green run reads as a survivor and a red run as a catch.
        "name": "pair-check-dropped",
        "file": MAIN_SRC,
        "old": _PAIR_CHECK,
        "new": "        if False:\n",
        "selection": [INTEGRATION],
        "expect": [],
    },
    {
        "name": "timeout-half-of-failed-check-dropped",
        "file": MAIN_SRC,
        "old": _FAILED_CHECK_HEAD,
        "new": "        failed = (\n",
        "selection": [INTEGRATION],
        "expect": [INTEG_LEFT_TIMEOUT],
    },
    {
        "name": "window-left-close-fires-no-notice",
        "file": STATE_SRC,
        "old": _WINDOW_LEFT_EFFECTS,
        "new": (
            "            self._dispatch_clear(),\n"
            "        ]\n"
        ),
        "selection": ALL_FILES,
        "expect": [
            STATE_BUILD_FAILED, STATE_TIMEOUT, STATE_TIMEOUT_AFTER_BUILD_OK,
            STATE_AUTO_OPEN,
        ],
    },
    {
        # The TIMEOUT arm, because the auto-open test times out.
        "name": "timeout-arm-closes-through-error-to-closed",
        "file": STATE_SRC,
        "old": _TIMEOUT_ARM,
        "new": (
            "        if kind is OverlayEventKind.TIMEOUT:\n"
            "            if self._visible_numbers_left_behind(event):\n"
            "                return self._error_to_closed("
            "emit_standalone_notice=True)\n"
        ),
        "selection": ALL_FILES,
        "expect": [STATE_AUTO_OPEN],
    },
    {
        "name": "restart-emits-no-clear",
        "file": STATE_SRC,
        "old": _RESTART_BODY,
        "new": (
            _RESTART_HEAD + _RESTART_UNPIN + _RESTART_BUMP + _RESTART_STATE
            + _RESTART_BUILD + _RESTART_TAIL
        ),
        "selection": ALL_FILES,
        "expect": [
            STATE_RESTART_SHOW, STATE_RESTART_FOCUS,
            STATE_FAILED_READ_BUILD, STATE_FAILED_READ_TIMEOUT,
            *INTEG_RESTART_ALL, INTEG_NEW_WALK_PAINT,
        ],
    },
    {
        # The bump MOVES ahead of the clear, so the clear carries the new
        # walk's pair: the GUI drops it and would refuse the new paint.
        "name": "restart-bumps-before-clear-stamp",
        "file": STATE_SRC,
        "old": _RESTART_BODY,
        "new": (
            _RESTART_HEAD + _RESTART_BUMP + _RESTART_CLEAR + _RESTART_UNPIN
            + _RESTART_STATE + _RESTART_BUILD + _RESTART_TAIL
        ),
        "selection": ALL_FILES,
        "expect": [
            STATE_RESTART_SHOW, STATE_RESTART_FOCUS,
            STATE_FAILED_READ_BUILD, STATE_FAILED_READ_TIMEOUT,
            *INTEG_RESTART_ALL, INTEG_NEW_WALK_PAINT,
        ],
    },
    {
        # The clear keeps its old-pair stamp but is appended after the build,
        # which would turn the walk cue off again at the GUI.
        "name": "restart-clears-after-build",
        "file": STATE_SRC,
        "old": _RESTART_BODY,
        "new": (
            _RESTART_HEAD + "        clear = self._clear_if_visible()\n"
            + _RESTART_UNPIN + _RESTART_BUMP + _RESTART_STATE + _RESTART_BUILD
            + "        if clear is not None:\n"
            + "            effects.append(clear)\n"
            + _RESTART_TAIL
        ),
        "selection": [STATE_TESTS],
        "expect": [
            STATE_RESTART_SHOW, STATE_RESTART_FOCUS,
            STATE_FAILED_READ_BUILD, STATE_FAILED_READ_TIMEOUT,
        ],
    },
    {
        # The pin gate is dropped: a walk_in_flight restart clears too.
        "name": "restart-clears-without-pin",
        "file": STATE_SRC,
        "old": _RESTART_BODY,
        "new": (
            _RESTART_HEAD + "        effects.append(self._dispatch_clear())\n"
            + _RESTART_UNPIN + _RESTART_BUMP + _RESTART_STATE + _RESTART_BUILD
            + _RESTART_TAIL
        ),
        "selection": [STATE_TESTS],
        "expect": [
            "test_walk_restart_without_pin_emits_no_clear[show-numbers]",
            "test_walk_restart_without_pin_emits_no_clear[focus-change]",
        ],
    },
]


def _clear_bytecode():
    shutil.rmtree(SERVICE / "__pycache__", ignore_errors=True)
    shutil.rmtree(SERVICE / "tests" / "__pycache__", ignore_errors=True)


def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=PER_MUTATION_TIMEOUT_S,
        # COLUMNS keeps pytest from cutting the " - <reason>" suffix off a
        # short-summary line wider than 80 columns.
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COLUMNS": "1000"},
    )


def _node_name(node_part: str) -> str:
    """``name[param-id]`` from the part after the last ``::``.

    The parameter list stays: different cases catch different mutations.
    Every id in the selection is hyphenated, with no space.
    """
    return node_part.split(" - ", 1)[0].strip().split(" ")[0]


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(_node_name(line.split("::", 1)[1].split("::")[-1]))
    return names


def _short_summary(output):
    marker = "short test summary info"
    idx = output.find(marker)
    if idx < 0:
        return None
    return output[idx:]


def failed_lines(output):
    """``{name: full FAILED line}`` from the short-summary section only."""
    section = _short_summary(output)
    found = {}
    if section is None:
        return found
    for line in section.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            node = line[len("FAILED "):].split(" - ", 1)[0]
            found[_node_name(node.split("::")[-1])] = line
    return found


def skipped_names(output):
    """Test names on -v per-test lines that read ``...::name SKIPPED (...)``."""
    names = set()
    for line in output.splitlines():
        if "::" in line and " SKIPPED" in line:
            names.add(line.split("::")[-1].strip().split(" ")[0])
    return names


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
        compile(mutated, str(mut["file"]), "exec")
    except SyntaxError as exc:
        return None, f"{mut['name']}: mutated source does not compile: {exc}"
    return mutated, None


def _originals():
    out = {}
    for mut in MUTATIONS:
        path = mut["file"]
        if path not in out:
            out[path] = path.read_bytes()
    return out


def check_only():
    originals = _originals()
    stale, non_compiling = [], []
    for mut in MUTATIONS:
        raw = originals[mut["file"]]
        _mutant, error = _build_mutant(
            raw.decode("utf-8"), _newline_of(raw), mut
        )
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


def _write_with_retry(path: Path, data: bytes) -> None:
    """Write ``data``; retry a bounded number of times on a held file.

    On Windows the language server, an antivirus scan, or the previous
    pytest process can hold the target for a moment ([WinError 5]).
    """
    for attempt in range(5):
        try:
            path.write_bytes(data)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5)


def _restore(path: Path, original: bytes) -> bool:
    """Put the source back, and say whether the original bytes are back.

    A False is a hard condition for the caller: the mutant is sitting in a
    live source file. A second Ctrl+C landing inside the restore is held,
    retried once, and re-raised only after the bytecode caches are cleared.
    """
    held = None
    restored = False
    for _ in range(2):
        try:
            _write_with_retry(path, original)
            restored = True
            break
        except KeyboardInterrupt as exc:
            held = exc
        except OSError as exc:
            print(f"ERROR could not restore {path}: {exc}")
            break
    else:
        print(f"ERROR could not restore {path}: interrupted twice")
    if restored:
        try:
            restored = path.read_bytes() == original
        except KeyboardInterrupt as exc:
            held = exc
            restored = False
            print(f"ERROR interrupted while reading {path} back")
        except OSError as exc:
            print(f"ERROR could not read {path} back: {exc}")
            restored = False
        if not restored:
            print(f"ERROR {path} still differs from the original after the restore")
    _clear_bytecode()
    if held is not None:
        raise held
    return restored


def _apply_and_run(path: Path, mutant: bytes, original: bytes, selection):
    """Write the mutant, run the selection, restore. Return (result, error).

    The write is INSIDE the guarded block: an interrupt or an OSError during
    the write must still reach the restore. A failed restore clears
    ``result`` so no verdict is read from a run whose source is still
    mutated.
    """
    result = None
    error = None
    _clear_bytecode()
    try:
        _write_with_retry(path, mutant)
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


def _selected(argv):
    """The mutations to run, honouring ``--only a,b``. Unknown name -> None."""
    wanted = None
    for i, arg in enumerate(argv):
        if arg == "--only" and i + 1 < len(argv):
            wanted = [n for n in argv[i + 1].split(",") if n]
        elif arg.startswith("--only="):
            wanted = [n for n in arg.split("=", 1)[1].split(",") if n]
    if wanted is None:
        return list(MUTATIONS)
    known = {mut["name"]: mut for mut in MUTATIONS}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        print(f"ERROR --only names no such mutation: {unknown}")
        print(f"      known names: {sorted(known)}")
        return None
    return [known[name] for name in wanted]


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    mutations = _selected(sys.argv[1:])
    if mutations is None:
        return 1

    originals = _originals()
    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(f"collected {len(real_names)} test names in {ALL_FILES}")
    for mut in mutations:
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
    baseline = _pytest(*ALL_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-3000:])
        return 1
    expected_all = {name for mut in mutations for name in mut["expect"]}
    skipped_catchers = sorted(skipped_names(baseline.stdout) & expected_all)
    if skipped_catchers:
        print(f"ERROR expected catchers skipped in the baseline: {skipped_catchers}")
        return 1
    print("baseline green")

    for mut in mutations:
        path = mut["file"]
        original = originals[path]
        mutated, error = _build_mutant(
            original.decode("utf-8"), _newline_of(original), mut
        )
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        result, error = _apply_and_run(
            path, mutated.encode("utf-8"), original, mut["selection"]
        )
        if error is not None:
            errors.append(f"{mut['name']}: {error}")
            print("ERROR", errors[-1])
            if "not restored" in error:
                print(f"ERROR stopping the sweep; restore {path.name} by hand")
                break
            continue
        assert result is not None
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        skipped = [n for n in mut["expect"] if n in skipped_names(result.stdout)]
        if skipped:
            errors.append(f"{mut['name']}: expected catcher skipped: {skipped}")
            print("ERROR", errors[-1])
            continue
        # Exit code first: 0 means no test failed, and a green run prints no
        # short-summary section at all.
        if result.returncode == 0:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; no test failed")
            continue
        got = failed_lines(result.stdout)
        if not got:
            errors.append(
                f"{mut['name']}: exit {result.returncode} with no FAILED "
                "line in the short summary"
            )
            print("ERROR", errors[-1])
            print(result.stdout[-3000:])
            continue
        missing = [n for n in mut["expect"] if n not in got]
        if missing:
            survived.append(mut["name"])
            print(
                f"SURVIVED {mut['name']}; missing {missing}; "
                f"failed {sorted(got)}"
            )
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")
        # Print every FAILED line with its reason, so a reader can confirm
        # the expected assertion fired rather than an unrelated crash.
        for name in sorted(got):
            print(f"         {got[name]}")

    print()
    names = ", ".join(mut["name"] for mut in mutations)
    print(
        f"scope: ran {len(mutations)} of {len(MUTATIONS)} mutations: {names}"
    )
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
