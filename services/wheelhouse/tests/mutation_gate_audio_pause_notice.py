"""Mutation gate for the audio pause notice removal and the stop hold-off (wh-audio-pause-notice-repeats).

Two changes are guarded:

* handlers/audio_monitor.py monitor_audio reports "sound stopped" only after
  the peak has stayed quiet for AUDIO_STOP_HOLDOFF_SECONDS; a start is still
  reported on the first loud poll. Catchers: TestMonitorAudioStopHoldoff in
  tests/test_handlers/test_audio_monitor.py (TestMonitorAudioLoop is in the
  selection so a mutation that breaks start reporting is seen there too).
* Sound that returns inside the hold-off publishes the playing event once
  more, so a sound pause the user toggle cleared starts again
  (wh-sound-pause-returns-after-toggle). Catchers:
  TestMonitorAudioRepublishesPlaying in the same file; two of its tests run
  the loop against a real StateManager, and one mutation removes the
  same-value guard in state_manager.py set_speech_suppressed_by_audio.
* state_manager.py set_speech_suppressed_by_audio no longer sends the
  listening-paused notice. Catcher: TestNoPauseNotice in
  tests/test_audio_suppression_control.py.

Each mutation names its catcher tests and the start of the assertion text
each must fail with, as pytest prints it in the short test summary.

Usage (run from services/wheelhouse with the wheelhouse interpreter):
    python tests/mutation_gate_audio_pause_notice.py --check
    python tests/mutation_gate_audio_pause_notice.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once and that every mutant
compiles, then exits. A sweep also validates the expected catcher names with
--collect-only and requires a green baseline before the first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites handlers/audio_monitor.py and state_manager.py while each mutation
runs.

Equivalent mutation left out on purpose: replacing the `quiet_since = None`
in the hold-off's report branch with `pass`. After a stop is reported the
previous state is False, so the next quiet poll has no change to report and
the first branch resets quiet_since anyway; no behaviour can differ.
"""
# crewcut: the runner below is copied from
# tests/mutation_gate_bravia_error_envelope.py, extended to two target files.
# A shared runner module imported by both gates is the way to remove the
# duplication later.
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
MONITOR = SERVICE / "handlers" / "audio_monitor.py"
STATE = SERVICE / "state_manager.py"
TARGETS = (MONITOR, STATE)
MONITOR_TESTS = "tests/test_handlers/test_audio_monitor.py"
CONTROL_TESTS = "tests/test_audio_suppression_control.py"
SELECTION = [
    f"{MONITOR_TESTS}::TestMonitorAudioLoop",
    f"{MONITOR_TESTS}::TestMonitorAudioStopHoldoff",
    f"{MONITOR_TESTS}::TestMonitorAudioRepublishesPlaying",
    f"{CONTROL_TESTS}::TestNoPauseNotice",
]
RUN_TIMEOUT = 180  # A clean run of the selection takes a few seconds.

DIP = "test_a_one_poll_dip_publishes_no_stop"
PAST = "test_quiet_past_the_hold_off_publishes_one_stop_at_its_end"
RETURNS = "test_sound_returning_inside_the_hold_off_publishes_no_stop"
RESTART = "test_quiet_after_sound_returns_waits_a_whole_new_hold_off"
NO_NOTICE = "test_a_pause_sends_no_notification"
LOOP_START = "test_publishes_event_on_state_change"
REPUBLISH = "test_sound_returning_inside_the_hold_off_publishes_playing_again"
AFTER_RETURN = "test_continuous_sound_after_the_return_publishes_nothing_more"
CONTINUOUS = "test_continuous_sound_publishes_one_event"
PAUSE_ON = "test_repeated_playing_changes_nothing_while_the_pause_is_on"
AFTER_TOGGLE = "test_repeated_playing_restores_the_pause_the_toggle_cleared"

MUTATIONS = [
    {
        # The hold-off comparison is never true, so the first quiet poll
        # reports the stop at once (the behaviour before the fix).
        "name": "hold-off-disabled",
        "file": MONITOR,
        "old": "if now - quiet_since < AUDIO_STOP_HOLDOFF_SECONDS:",
        "new": "if now - quiet_since < 0:",
        "catchers": {
            DIP: "assert [True, False, True] == [True, True]",
            PAST: "assert [(True, 0.0), (False, 1.0)] == [(True, 0.0), (False, 4.0)]",
            RETURNS: "assert [True, False, True] == [True, True]",
            RESTART: "assert [(True, 0.0),... (False, 4.0)] == [(True, 0.0),... (False, 7.0)]",
        },
    },
    {
        # The hold-off never ends, so a stop is never reported.
        "name": "hold-off-never-ends",
        "file": MONITOR,
        "old": "if now - quiet_since < AUDIO_STOP_HOLDOFF_SECONDS:",
        "new": "if now - quiet_since < 10**9:",
        "catchers": {
            PAST: "assert [(True, 0.0)] == [(True, 0.0), (False, 4.0)]",
            RESTART: "assert [(True, 0.0), (True, 3.0)] == [(True, 0.0),... (False, 7.0)]",
        },
    },
    {
        # A loud poll no longer resets quiet_since, so a later quiet stretch
        # is timed from the earlier one and the stop comes early, and every
        # later loud poll repeats the playing event.
        "name": "loud-poll-keeps-stale-quiet-since",
        "file": MONITOR,
        "old": (
            "                        quiet_since = None\n"
            "                    else:\n"
        ),
        "new": (
            "                        pass\n"
            "                    else:\n"
        ),
        "catchers": {
            RESTART: "assert [(True, 0.0),... (False, 4.0)] == [(True, 0.0),... (False, 7.0)]",
            AFTER_RETURN: "assert [(True, 0.0),..., (True, 4.0)] == [(True, 0.0), (True, 2.0)]",
        },
    },
    {
        # A start after quiet is sent through the hold-off too, so a start
        # is no longer reported on the first loud poll.
        "name": "start-waits-out-the-hold-off",
        "file": MONITOR,
        "old": "if is_playing or not report_change:",
        "new": "if not report_change:",
        "catchers": {
            DIP: "assert [] == [True, True]",
            PAST: "assert [] == [(True, 0.0), (False, 4.0)]",
            RETURNS: "assert [] == [True, True]",
            RESTART: "assert [] == [(True, 0.0),... (False, 7.0)]",
            LOOP_START: "AssertionError: Expected 'publish' to have been called.",
        },
    },
    {
        # quiet_since restarts on every quiet poll, so the hold-off never
        # completes and a stop is never reported.
        "name": "quiet-since-restarts-each-poll",
        "file": MONITOR,
        "old": "if quiet_since is None:",
        "new": "if True:",
        "catchers": {
            PAST: "assert [(True, 0.0)] == [(True, 0.0), (False, 4.0)]",
            RESTART: "assert [(True, 0.0), (True, 3.0)] == [(True, 0.0),... (False, 7.0)]",
        },
    },
    {
        # Off by one poll at the boundary: a quiet stretch of exactly the
        # hold-off no longer reports the stop.
        "name": "boundary-less-or-equal",
        "file": MONITOR,
        "old": "if now - quiet_since < AUDIO_STOP_HOLDOFF_SECONDS:",
        "new": "if now - quiet_since <= AUDIO_STOP_HOLDOFF_SECONDS:",
        "catchers": {
            PAST: "assert [(True, 0.0)] == [(True, 0.0), (False, 4.0)]",
            RESTART: "assert [(True, 0.0), (True, 3.0)] == [(True, 0.0),... (False, 7.0)]",
        },
    },
    {
        # A 1 s hold-off lets a 2 s dip end the pause. PAST and RESTART read
        # the constant, so only the fixed dip lengths can see this.
        "name": "hold-off-constant-one-second",
        "file": MONITOR,
        "old": "AUDIO_STOP_HOLDOFF_SECONDS = 3\n",
        "new": "AUDIO_STOP_HOLDOFF_SECONDS = 1\n",
        "catchers": {
            RETURNS: "assert [True, False, True] == [True, True]",
            REPUBLISH: "assert [(True, 0.0),..., (True, 3.0)] == [(True, 0.0), (True, 3.0)]",
        },
    },
    {
        # The repeat is removed: sound that returns inside the hold-off
        # publishes nothing, so a pause the toggle cleared stays off
        # (the behaviour before wh-sound-pause-returns-after-toggle).
        "name": "repeat-removed",
        "file": MONITOR,
        "old": "                            report_change = True\n",
        "new": "                            pass\n",
        "catchers": {
            DIP: "assert [True] == [True, True]",
            RETURNS: "assert [True] == [True, True]",
            RESTART: "assert [(True, 0.0), (False, 7.0)] == [(True, 0.0),... (False, 7.0)]",
            REPUBLISH: "assert [(True, 0.0)] == [(True, 0.0), (True, 3.0)]",
            AFTER_RETURN: "assert [(True, 0.0)] == [(True, 0.0), (True, 2.0)]",
            PAUSE_ON: "assert [True] == [True, True]",
            AFTER_TOGGLE: "assert False is True",
        },
    },
    {
        # Every loud poll repeats the playing event, not only the first
        # loud poll after a quiet reading.
        "name": "repeat-on-every-loud-poll",
        "file": MONITOR,
        "old": "if is_playing and quiet_since is not None:",
        "new": "if is_playing:",
        "catchers": {
            CONTINUOUS: "assert [(True, 0.0),..., (True, 3.0)] == [(True, 0.0)]",
            AFTER_RETURN: "assert [(True, 0.0),..., (True, 4.0)] == [(True, 0.0), (True, 2.0)]",
        },
    },
    {
        # The setter no longer skips a value it already holds, so the
        # repeated playing event broadcasts the status and updates the GUI
        # again while the pause is already on. state_manager.py is not
        # changed by this branch; the mutation proves PAUSE_ON guards it.
        "name": "setter-same-value-guard-removed",
        "file": STATE,
        "old": "        if self._speech_suppressed_by_audio != is_suppressed:\n",
        "new": "        if True:\n",
        "catchers": {
            PAUSE_ON: "assert (0, 1, 1) == (0, 0, 0)",
        },
    },
    {
        # The listening-paused notice comes back at the pause point, with
        # the base code's condition and call form.
        "name": "pause-notice-restored",
        "file": STATE,
        "old": (
            '                self.speech_notifier.notify_suppression_change("System Audio", is_suppressed, details)\n'
        ),
        "new": (
            '                self.speech_notifier.notify_suppression_change("System Audio", is_suppressed, details)\n'
            "\n"
            "            if old_speech_enabled and not new_speech_enabled:\n"
            "                self.speech_notifier._send_notification(\n"
            '                    NOTICE_TITLE, "Listening paused: sound is playing."\n'
            "                )\n"
        ),
        "catchers": {
            NO_NOTICE: "AssertionError: Expected 'mock' to not have been called. Called 1 times.",
        },
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(originals, names):
    """Return (prepared mutants, error lines). Never touches the files."""
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        target = mutation["file"]
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            errors.append(f"ERROR {mutation['name']}: pattern-not-found")
            continue
        if count > 1:
            errors.append(f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)")
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(target), "exec")
        except SyntaxError as exc:
            errors.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, mutant.encode("utf-8")))
    return prepared, errors


def _clear_bytecode():
    for directory in {target.parent for target in TARGETS}:
        shutil.rmtree(directory / "__pycache__", ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "400"  # keep short-summary messages untruncated
    return env


def _pytest(extra, timeout):
    command = [sys.executable, "-m", "pytest", *SELECTION, "-p", "no:randomly",
               "-p", "no:cacheprovider", "--junitxml=NUL" if os.name == "nt" else "--junitxml=/dev/null",
               *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _collected_names():
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split("[")[0].strip())
    return result.returncode, names


def _summary(output: str):
    """Map test name -> (kind, message) from pytest's short summary only."""
    records, inside = {}, False
    for line in output.splitlines():
        if SUMMARY_HEADER.match(line):
            inside = True
            continue
        if inside and SECTION_RULE.match(line):
            break
        if inside:
            match = SUMMARY_LINE.match(line)
            if match:
                test_id = match.group(2)
                name = test_id.split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if result.returncode == 1 and not records:
        raise RuntimeError(f"pytest exit 1 with no short-summary records: {output[-1500:]}")
    return records


def _write_with_retry(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    last = None
    try:
        for _ in range(10):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(target: Path, original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(target, original)
            os.utime(target, ns=stat)
            if target.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {target}: {exc}", flush=True)
    restored = False
    try:
        restored = target.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(f"RESTORE FAILED: {target} may still hold a mutant; check git diff", flush=True)
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


def _judge(mutation, records):
    unexpected = [f"{name}: {message}" for name, (kind, message) in records.items()
                  if kind == "ERROR" or not (message.startswith("assert") or message.startswith("AssertionError"))]
    if unexpected:
        return "ERROR", "non-assertion failure: " + "; ".join(unexpected)
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not records[name][1].startswith(marker):
            wrong.append(f"{name}: {records[name][1]} (expected {marker!r})")
        else:
            fired.append(f"{name}: {records[name][1]}")
    if wrong:
        return "ERROR", "catcher failed at an unexpected assertion: " + "; ".join(wrong)
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append("other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others))
    if not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME", help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    known = {m["name"] for m in MUTATIONS}
    unknown = [n for n in args.only if n not in known]
    if unknown:
        say(f"ERROR unknown mutation names: {', '.join(unknown)}")
        return 1

    originals = {target: target.read_bytes() for target in TARGETS}
    stats = {target: (target.stat().st_atime_ns, target.stat().st_mtime_ns) for target in TARGETS}
    prepared, errors = _prepare(originals, set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, {broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted({name for mutation, _ in prepared for name in mutation["catchers"]} - collected)
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): {', '.join(missing) or 'none collected'}")
        return 1

    _clear_bytecode()
    try:
        baseline = _run_selection()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        say(f"ERROR baseline: {exc}")
        return 1
    if baseline:
        say(f"ERROR baseline not green: {sorted(baseline)}")
        return 1
    say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    survivors = 0
    error_count = len(errors)
    for index, (mutation, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        target = mutation["file"]
        stat = stats[target]
        try:
            _write_with_retry(target, mutant)
            os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection()
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(target, originals[target], stat)
        if verdict == "SURVIVED":
            survivors += 1
        elif verdict == "ERROR":
            error_count += 1
        say(f"{verdict} {name}: {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{len(prepared) - survivors - (error_count - len(errors))} caught, "
        f"{survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
