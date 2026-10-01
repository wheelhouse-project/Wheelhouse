"""Mutation gate for wh-xray-close-app-fails.

The change: "x-ray close <app>" and "close window" send Alt+F4, and some
windows ignore it. ui/close_fallback.py waits up to about 500 ms after the
chord and, when the same window is still open, in front, enabled and has no
owned popup, posts WM_SYSCOMMAND / SC_CLOSE to it. UIActionHandler.
hotkey_action starts that fallback for a single, accepted Alt+F4 only. This
gate breaks each new branch in turn and names the tests that must fail.

Run it from services/wheelhouse with that service's own interpreter, never
through ``uv run`` (that can build a .venv inside a worktree):

    .venv/Scripts/python.exe tests/mutation_gate_xray_close_fallback.py

    --check          verify that every pattern still matches exactly once and
                     every mutant compiles; run no test
    --only=<name>    run one mutation (repeatable); the scope line says so
    --discover       run the mutations WITHOUT expected-test validation and
                     print each failed set and failure reason; used to choose
                     the "expect" lists, never as a verdict

Where the mutations are, by target file:

  ui/close_fallback.py     the shell-window refusal, the closed-window early
                           return, the in-front, enabled and owned-popup
                           checks (and the "popup is the window itself means
                           no popup" comparison), the post itself and its
                           three constants, the wait length, the close chord
                           test, the identity check before the post (removed,
                           a zero marker accepted, any marked window accepted,
                           inverted; wh-xray-close-app-fails.1.2), probe
                           failure reported as a post, and the
                           thread's name, daemon flag and containment
  ui/ui_action_handler.py  the start of the fallback in hotkey_action: the
                           call, the refusal condition, the repeat == 1
                           condition, the chord condition, the root
                           normalization, reading the target before the
                           send, and tagging the root window and passing its
                           marker on

Reported as errors, never as a verdict: pattern-not-found, an ambiguous
pattern, a mutation that does not compile, a per-mutation timeout, a
suite-timeout abort, any pytest return code other than 0 or 1, any ERROR
line in the summary, an expected test that does not exist, and a failing test
that raised an exception instead of failing its assertion (unless the
mutation lists a reason prefix in "raises_ok").

Each target file's own line endings are detected and every pattern is
translated to them before matching; a file is restored with write_bytes and
its bytecode cache entries are deleted after every mutation.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
FALLBACK = SERVICE / "ui" / "close_fallback.py"
HANDLER = SERVICE / "ui" / "ui_action_handler.py"

F_FALLBACK = "tests/test_ui/test_close_fallback.py"

ALL_TEST_FILES = [F_FALLBACK]

# --tb=line carries the failure REASON; this project's pytest prints a bare
# "FAILED <nodeid>" summary with no reason suffix. COLUMNS keeps pytest from
# cutting a long node id.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

MUTATION_TIMEOUT_S = 300


def mut(name, file, old, new, tests, expect, **extra):
    entry = {
        "name": name, "file": file, "old": old, "new": new,
        "tests": tests, "expect": expect,
    }
    entry.update(extra)
    return entry


MUTATIONS = [
    # =====================================================================
    # ui/close_fallback.py
    # =====================================================================
    mut(
        "shell-check-removed",
        FALLBACK,
        "if class_name in SHELL_WINDOW_CLASSES:",
        "if False:",
        [F_FALLBACK],
        ["test_a_shell_window_is_never_closed_and_never_waited_for", "test_every_shell_class_is_refused"],
    ),
    mut(
        "default-ops-enabled-probe-swapped",
        FALLBACK,
        '"IsWindowEnabled": win32gui.IsWindowEnabled,',
        '"IsWindowEnabled": win32gui.IsWindow,',
        [F_FALLBACK],
        ["test_each_name_is_the_matching_win32_call"],
    ),
    mut(
        "foreground-root-not-normalized",
        FALLBACK,
        "return normalize_hwnd_for_foreground_compare(win32gui.GetForegroundWindow())",
        "return win32gui.GetForegroundWindow()",
        [F_FALLBACK],
        ["test_the_foreground_root_normalizes_the_foreground_window"],
    ),
    mut(
        "shell-class-set-mutable",
        FALLBACK,
        "SHELL_WINDOW_CLASSES = frozenset(",
        "SHELL_WINDOW_CLASSES = set(",
        [F_FALLBACK],
        ["test_the_shell_class_set_cannot_be_changed"],
    ),
    mut(
        "closed-early-return-removed",
        FALLBACK,
        'logger.debug("close fallback: hwnd=%s closed after Alt+F4", hwnd)\n            return "closed"\n',
        'logger.debug("close fallback: hwnd=%s closed after Alt+F4", hwnd)\n            pass\n',
        [F_FALLBACK],
        ["test_a_window_that_closes_on_the_third_poll_gets_no_post", "test_a_window_already_gone_gets_no_post"],
    ),
    mut(
        "wait-limit-zero",
        FALLBACK,
        "CLOSE_WAIT_S = 0.5\n",
        "CLOSE_WAIT_S = 0.0\n",
        [F_FALLBACK],
        ["test_a_window_that_closes_on_the_third_poll_gets_no_post", "test_the_wait_is_about_half_a_second_in_steps_of_about_50_ms"],
    ),
    mut(
        "wait-limit-five-seconds",
        FALLBACK,
        "CLOSE_WAIT_S = 0.5\n",
        "CLOSE_WAIT_S = 5.0\n",
        [F_FALLBACK],
        ["test_the_wait_is_about_half_a_second_in_steps_of_about_50_ms"],
    ),
    mut(
        "not-in-front-check-removed",
        FALLBACK,
        'if o["GetForegroundRoot"]() != hwnd:',
        "if False:",
        [F_FALLBACK],
        ["test_a_window_no_longer_in_front_stops_the_post", "test_no_foreground_window_stops_the_post"],
    ),
    mut(
        "not-in-front-check-inverted",
        FALLBACK,
        'if o["GetForegroundRoot"]() != hwnd:',
        'if o["GetForegroundRoot"]() == hwnd:',
        [F_FALLBACK],
        ["test_a_window_that_ignores_alt_f4_gets_sc_close", "test_a_window_no_longer_in_front_stops_the_post"],
    ),
    mut(
        "enabled-check-removed",
        FALLBACK,
        'if not o["IsWindowEnabled"](hwnd):',
        "if False:",
        [F_FALLBACK],
        ["test_a_modal_dialog_stops_the_post"],
    ),
    mut(
        "popup-check-removed",
        FALLBACK,
        "if popup not in (0, None, hwnd):",
        "if False:",
        [F_FALLBACK],
        ["test_a_save_prompt_stops_the_post"],
    ),
    mut(
        "popup-self-comparison-inverted",
        FALLBACK,
        "if popup not in (0, None, hwnd):",
        "if popup not in (0, None):",
        [F_FALLBACK],
        ["test_a_window_that_ignores_alt_f4_gets_sc_close", "test_a_posted_close_is_logged_at_info"],
    ),
    mut(
        "postmessage-removed",
        FALLBACK,
        'o["PostMessage"](hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)',
        "pass",
        [F_FALLBACK],
        ["test_a_window_that_ignores_alt_f4_gets_sc_close"],
    ),
    mut(
        "identity-check-removed",
        FALLBACK,
        'if not marker or o["ReadProvenance"](hwnd) != marker:',
        "if False:",
        [F_FALLBACK],
        ["test_a_new_window_with_the_same_handle_gets_no_post", "test_a_new_window_carrying_another_marker_gets_no_post", "test_a_zero_marker_gets_no_post", "test_the_identity_is_read_for_the_window_after_every_other_probe"],
    ),
    mut(
        "identity-accepts-a-zero-marker",
        FALLBACK,
        'if not marker or o["ReadProvenance"](hwnd) != marker:',
        'if o["ReadProvenance"](hwnd) != marker:',
        [F_FALLBACK],
        ["test_a_zero_marker_gets_no_post"],
    ),
    mut(
        "identity-accepts-any-marked-window",
        FALLBACK,
        'if not marker or o["ReadProvenance"](hwnd) != marker:',
        'if not marker or not o["ReadProvenance"](hwnd):',
        [F_FALLBACK],
        ["test_a_new_window_carrying_another_marker_gets_no_post"],
    ),
    mut(
        "identity-comparison-inverted",
        FALLBACK,
        'if not marker or o["ReadProvenance"](hwnd) != marker:',
        'if not marker or o["ReadProvenance"](hwnd) == marker:',
        [F_FALLBACK],
        ["test_the_matching_marker_posts", "test_a_window_that_ignores_alt_f4_gets_sc_close"],
    ),
    mut(
        "default-ops-provenance-reader-swapped",
        FALLBACK,
        '"ReadProvenance": read_hwnd_provenance,',
        '"ReadProvenance": normalize_hwnd_for_foreground_compare,',
        [F_FALLBACK],
        ["test_each_name_is_the_matching_win32_call"],
    ),
    mut(
        "marker-not-passed-to-the-decision",
        FALLBACK,
        "close_if_still_open(hwnd, marker)",
        "close_if_still_open(hwnd, hwnd)",
        [F_FALLBACK],
        ["test_it_runs_the_decision_on_a_named_daemon_thread"],
    ),
    mut(
        "sc-close-value-changed",
        FALLBACK,
        "SC_CLOSE = 0xF060",
        "SC_CLOSE = 0xF063",
        [F_FALLBACK],
        ["test_a_window_that_ignores_alt_f4_gets_sc_close", "test_the_constants_are_the_windows_values"],
    ),
    mut(
        "wm-syscommand-value-changed",
        FALLBACK,
        "WM_SYSCOMMAND = 0x0112",
        "WM_SYSCOMMAND = 0x0111",
        [F_FALLBACK],
        ["test_a_window_that_ignores_alt_f4_gets_sc_close", "test_the_constants_are_the_windows_values"],
    ),
    mut(
        "enabled-popup-constant-changed",
        FALLBACK,
        "GW_ENABLEDPOPUP = 6",
        "GW_ENABLEDPOPUP = 5",
        [F_FALLBACK],
        ["test_the_popup_probe_asks_for_the_enabled_popup", "test_the_constants_are_the_windows_values"],
    ),
    mut(
        "probe-failure-reported-as-posted",
        FALLBACK,
        'return "probe_failed"',
        'return "posted"',
        [F_FALLBACK],
        ["test_a_probe_that_raises_posts_nothing"],
    ),
    mut(
        "close-chord-any-f4",
        FALLBACK,
        'return sorted(str(k).lower() for k in keys) == ["alt", "f4"]',
        'return "f4" in [str(k).lower() for k in keys]',
        [F_FALLBACK],
        ["test_anything_else_is_not_the_close_chord"],
    ),
    mut(
        "close-chord-order-matters",
        FALLBACK,
        'return sorted(str(k).lower() for k in keys) == ["alt", "f4"]',
        'return [str(k).lower() for k in keys] == ["alt", "f4"]',
        [F_FALLBACK],
        ["test_alt_and_f4_in_either_order"],
    ),
    mut(
        "close-chord-case-sensitive",
        FALLBACK,
        'return sorted(str(k).lower() for k in keys) == ["alt", "f4"]',
        'return sorted(str(k) for k in keys) == ["alt", "f4"]',
        [F_FALLBACK],
        ["test_alt_and_f4_in_either_order"],
    ),
    mut(
        "thread-name-changed",
        FALLBACK,
        'name="close-fallback"',
        'name="other"',
        [F_FALLBACK],
        ["test_it_runs_the_decision_on_a_named_daemon_thread"],
    ),
    mut(
        "thread-not-daemon",
        FALLBACK,
        "daemon=True)",
        "daemon=False)",
        [F_FALLBACK],
        ["test_it_runs_the_decision_on_a_named_daemon_thread"],
    ),
    mut(
        "thread-body-uncontained",
        FALLBACK,
        'except Exception as e:\n            logger.debug("close fallback thread failed',
        'except KeyError as e:\n            logger.debug("close fallback thread failed',
        [F_FALLBACK],
        ["test_an_exception_in_the_decision_stays_in_the_thread"],
    ),
    # =====================================================================
    # ui/ui_action_handler.py: hotkey_action starts the fallback
    # =====================================================================
    mut(
        "handler-start-call-removed",
        HANDLER,
        "                close_fallback.start_close_fallback(\n                    close_target, close_marker\n                )",
        "                pass",
        [F_FALLBACK],
        ["test_alt_f4_starts_the_fallback_for_the_root_of_the_foreground_window", "test_the_target_is_read_before_the_keys_are_sent", "test_the_app_already_in_front_gets_alt_f4_and_the_fallback", "test_the_app_behind_another_window_gets_alt_f4_and_the_fallback"],
    ),
    mut(
        "handler-refusal-condition-removed",
        HANDLER,
        "if close_target and refusal is None:",
        "if close_target:",
        [F_FALLBACK],
        ["test_a_refused_alt_f4_does_not_start_it"],
    ),
    mut(
        "handler-repeat-condition-removed",
        HANDLER,
        "if close_fallback.is_close_chord(keys) and repeat == 1:",
        "if close_fallback.is_close_chord(keys):",
        [F_FALLBACK],
        ["test_a_repeated_alt_f4_does_not_start_it"],
    ),
    mut(
        "handler-chord-condition-removed",
        HANDLER,
        "if close_fallback.is_close_chord(keys) and repeat == 1:",
        "if repeat == 1:",
        [F_FALLBACK],
        ["test_ctrl_a_does_not_start_it"],
    ),
    mut(
        "handler-target-not-root-normalized",
        HANDLER,
        "close_target = normalize_hwnd_for_foreground_compare(\n                    win32gui.GetForegroundWindow()\n                )",
        "close_target = win32gui.GetForegroundWindow()",
        [F_FALLBACK],
        ["test_alt_f4_starts_the_fallback_for_the_root_of_the_foreground_window"],
    ),
    mut(
        "handler-tag-removed",
        HANDLER,
        "close_marker = tag_hwnd_provenance(close_target)",
        "close_marker = 0",
        [F_FALLBACK],
        ["test_alt_f4_starts_the_fallback_for_the_root_of_the_foreground_window", "test_the_root_window_is_tagged_and_the_marker_is_passed_on", "test_the_window_is_tagged_before_the_keys_are_sent"],
    ),
    mut(
        "handler-tags-the-raw-foreground-window",
        HANDLER,
        "close_marker = tag_hwnd_provenance(close_target)",
        "close_marker = tag_hwnd_provenance(win32gui.GetForegroundWindow())",
        [F_FALLBACK],
        ["test_the_root_window_is_tagged_and_the_marker_is_passed_on"],
    ),
    mut(
        "handler-marker-not-passed",
        HANDLER,
        "                    close_target, close_marker\n                )",
        "                    close_target, 0\n                )",
        [F_FALLBACK],
        ["test_the_root_window_is_tagged_and_the_marker_is_passed_on", "test_alt_f4_starts_the_fallback_for_the_root_of_the_foreground_window"],
    ),
    mut(
        "handler-target-read-after-the-send",
        HANDLER,
        "if close_target and refusal is None:\n                close_fallback.start_close_fallback(\n                    close_target, close_marker\n                )",
        "if close_target and refusal is None:\n                close_fallback.start_close_fallback(\n                    normalize_hwnd_for_foreground_compare(win32gui.GetForegroundWindow()), close_marker\n                )",
        [F_FALLBACK],
        ["test_the_target_is_read_before_the_keys_are_sent"],
    ),
]




# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _pytest(*extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *extra],
        cwd=SERVICE,
        capture_output=True,
        text=True,
        timeout=MUTATION_TIMEOUT_S,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "COLUMNS": "1000",
        },
    )


def _clear_bytecode(path):
    """Delete the cached bytecode of ``path`` so a mutant is never skipped."""
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for entry in cache.glob(f"{path.stem}.*.pyc"):
            try:
                entry.unlink()
            except OSError:
                pass


def _write_restoring(path, data):
    """write_bytes with a bounded retry for a transient Windows lock."""
    for attempt in range(10):
        try:
            path.write_bytes(data)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def collect_names():
    """Real test names, so a renamed test cannot read as a survivor."""
    out = _pytest(*ALL_TEST_FILES, "--collect-only", "-q", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def is_assertion_failure(reason):
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_SUMMARY_ERROR = re.compile(r"^ERROR \S+::")


def error_lines(output):
    """Short-summary ERROR lines only (a node id follows the word).

    A captured log record at ERROR level also starts with "ERROR" but is
    followed by spaces and a logger name, and a genuine catch whose test
    logged an error must not read as a gate error.
    """
    return [
        line for line in output.splitlines() if _SUMMARY_ERROR.match(line)
    ]


def _sources():
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        original = path.read_bytes()
        sources[path] = {
            "original": original,
            "text": original.decode("utf-8"),
            "newline": "\r\n" if b"\r\n" in original else "\n",
        }
    return sources


def _translate(sources):
    for m in MUTATIONS:
        newline = sources[m["file"]]["newline"]
        m["old"] = m["old"].replace("\n", newline)
        m["new"] = m["new"].replace("\n", newline)


def _prepare(m, sources):
    """Return (mutated_text, error). Exactly one match, and it must compile."""
    text = sources[m["file"]]["text"]
    count = text.count(m["old"])
    if count != 1:
        return None, f"{m['name']}: pattern matched {count} times, expected 1"
    mutated = text.replace(m["old"], m["new"], 1)
    if m["file"].suffix == ".py":
        try:
            compile(mutated, str(m["file"]), "exec")
        except SyntaxError as exc:
            return None, f"{m['name']}: mutated source does not compile: {exc}"
    return mutated, None


def check_only(sources):
    stale, broken = [], []
    for m in MUTATIONS:
        _mutated, error = _prepare(m, sources)
        if error is None:
            continue
        print("ERROR", error)
        (broken if "does not compile" in error else stale).append(m["name"])
    print()
    print(
        f"checked {len(MUTATIONS)} patterns, "
        f"{len(stale)} stale, {len(broken)} that do not compile"
    )
    return 1 if stale or broken else 0


def main():
    sources = _sources()
    _translate(sources)
    for path, source in sources.items():
        ending = "CRLF" if source["newline"] == "\r\n" else "LF"
        print(f"line endings in {path.name}: {ending}")

    names = [m["name"] for m in MUTATIONS]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        print("ERROR duplicate mutation names:", duplicated)
        return 1

    if "--check" in sys.argv:
        return check_only(sources)

    discover = "--discover" in sys.argv
    only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
    unknown = [n for n in only if n not in set(names)]
    if unknown:
        print("ERROR --only names no mutation:", unknown)
        return 1
    selected = [m for m in MUTATIONS if not only or m["name"] in only]

    errors, caught, survived = [], [], []

    if not discover:
        real_names = collect_names()
        print(f"collected {len(real_names)} test names")
        for m in selected:
            if not m["expect"]:
                errors.append(f"{m['name']}: no expected tests named")
            for name in m["expect"]:
                if name not in real_names:
                    errors.append(
                        f"{m['name']}: expected test {name!r} does not exist"
                    )
        if errors:
            for line in errors:
                print("ERROR", line)
            return 1

    baselines = {}
    for m in selected:
        key = tuple(m["tests"])
        if key in baselines:
            continue
        result = _pytest(*key, *PYTEST_ARGS)
        if result.returncode != 0:
            print(f"ERROR baseline is not green for {list(key)}; refusing")
            print(result.stdout[-2000:])
            return 1
        baselines[key] = True
    print(f"baseline green for {len(baselines)} test selections")

    for m in selected:
        path = m["file"]
        original = sources[path]["original"]
        mutated, error = _prepare(m, sources)
        if error is not None:
            errors.append(error)
            print("ERROR", error)
            continue
        _write_restoring(path, mutated.encode("utf-8"))
        _clear_bytecode(path)
        try:
            result = _pytest(*m["tests"], *PYTEST_ARGS)
        except subprocess.TimeoutExpired:
            errors.append(f"{m['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            try:
                _write_restoring(path, original)
            finally:
                _clear_bytecode(path)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{m['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{m['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        if discover:
            reasons = sorted({r[:110] for r in failure_reasons(result.stdout)})
            print(f"DISCOVER {m['name']} rc={result.returncode}")
            print(f"    failed: {sorted(got)}")
            print(f"    reasons: {reasons}")
            continue
        missing = [n for n in m["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(m["name"])
            print(
                f"SURVIVED {m['name']}; missing {missing}; "
                f"failed tests were {sorted(got)}"
            )
            continue
        allowed = tuple(m.get("raises_ok") or ())
        crashed = [
            r for r in failure_reasons(result.stdout)
            if not is_assertion_failure(r) and not r.startswith(allowed)
        ]
        if crashed:
            errors.append(
                f"{m['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))[:3]}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(m["name"])
        print(f"caught   {m['name']} by {sorted(got)}")

    print()
    skipped = len(MUTATIONS) - len(selected)
    print(
        f"scope: {len(selected)} of {len(MUTATIONS)} mutations, "
        + (f"{skipped} skipped by --only" if skipped else "none skipped")
    )
    if discover:
        print("discover mode: no verdict")
        return 0
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
