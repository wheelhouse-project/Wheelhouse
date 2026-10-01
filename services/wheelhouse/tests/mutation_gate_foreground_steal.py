"""Mutation gate for utils.foreground.steal_foreground (wh-activate-windows-terminal.3).

WHAT THE CODE DOES. steal_foreground brings a window to the front past the
Windows foreground lock: it attaches this thread's input queue to the thread
of the CURRENT FOREGROUND window, calls SetForegroundWindow, detaches in a
finally, and returns what SetForegroundWindow reported. The voice "activate"
command used to attach to the TARGET window's thread instead; the lock
refuses that, and Wheelhouse logged success while Windows Terminal stayed
behind (David's check at 18:33, 2026-09-29).

WHAT IT COVERS:

  attaches-to-the-target-thread          the thread looked up is the target
                                         window's, not the foreground's
  the-detach-is-dropped                  the finally no longer detaches
  the-detach-is-skipped-on-an-exception  the detach runs only when
                                         SetForegroundWindow returns normally
  success-regardless-of-setforeground    True is returned even when Windows
                                         refused

The catchers are TestStealForeground in tests/test_terminal_editor_window.py
(the function's own tests, through the terminal_editor_window alias) and
TestActivateWindowForegroundLock in tests/test_input_proc_activate_window.py
(the activate path, whose fake answers a different thread for the target
window than for the foreground window).

Run from services/wheelhouse with the interpreter the whole worktree uses.
In a worktree that is the main checkout's interpreter:

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_foreground_steal.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_foreground_steal.py

pytest starts as ``<this interpreter> -m pytest`` in the service directory,
never through scripts/run_tests.py, whose ``uv run`` builds a .venv inside a
git worktree (the launcher is the one in tests/mutation_gate_equals_spacing.py).
The shared runner (services/stt_providers/shared/tests/mutation_gate_runner.py)
owns everything else: exactly-one pattern matches, compiled mutants, the
expected-name check against real collection, the green baseline, the per-run
timeout, byte-exact restoration, and the short-summary verdicts.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
TARGET = SERVICE / "utils/foreground.py"
sys.path.insert(0, str(ROOT / "services/stt_providers/shared/tests"))
import mutation_gate_runner as runner  # noqa: E402


def _launch(service, test_file, report, collect):
    """Start pytest directly, never through ``uv run`` (module docstring)."""
    command = [
        sys.executable, "-m", "pytest",
        *runner._targets(test_file),
        f"--junitxml={report}",
        *(["--collect-only", "-q"] if collect
          else ["-q", "-rf", "-p", "no:randomly"]),
    ]
    return command, service


runner.LAUNCH = _launch

TESTS = (
    "tests/test_terminal_editor_window.py::TestStealForeground",
    "tests/test_input_proc_activate_window.py",
)


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=TARGET,
                old=old, new=new, expect=expect)


DETACH_IN_FINALLY = (
    "        try:\n"
    "            ops[\"BringWindowToTop\"](win_hwnd)\n"
    "            result = bool(ops[\"SetForegroundWindow\"](win_hwnd))\n"
    "        finally:\n"
    "            if attached:\n"
    "                ops[\"AttachThreadInput\"](\n"
    "                    current_thread, fg_thread, False,\n"
    "                )\n"
)

MUTATIONS = [
    # The lock lets the call through only for a thread attached to the
    # foreground thread; the target's thread is what the old code used.
    # TestStealForeground's fake answers one thread for every window, so
    # only the activate test's fake, which answers by window, can see it.
    mutation(
        "attaches-to-the-target-thread",
        "        fg_thread = int(ops[\"GetWindowThreadProcessId\"](fg_hwnd))\n",
        "        fg_thread = int(ops[\"GetWindowThreadProcessId\"](win_hwnd))\n",
        ["test_attaches_to_the_foreground_thread_never_the_target_thread",
         "test_a_refusal_returns_false_warns_and_still_detaches"],
    ),
    # The input queues stay shared, so keyboard and mouse capture stay
    # joined to the other thread.
    mutation(
        "the-detach-is-dropped",
        DETACH_IN_FINALLY,
        "        try:\n"
        "            ops[\"BringWindowToTop\"](win_hwnd)\n"
        "            result = bool(ops[\"SetForegroundWindow\"](win_hwnd))\n"
        "        finally:\n"
        "            pass\n",
        ["test_attaches_and_detaches_when_foreground_is_other_thread",
         "test_detaches_even_when_set_foreground_fails",
         "test_attaches_to_the_foreground_thread_never_the_target_thread",
         "test_a_refusal_returns_false_warns_and_still_detaches"],
    ),
    # The detach leaves the finally: a SetForegroundWindow that raises
    # (pywin32's form of a refusal) skips it. Only the activate test's
    # raising fake reaches this.
    mutation(
        "the-detach-is-skipped-on-an-exception",
        DETACH_IN_FINALLY,
        "        if True:\n"
        "            ops[\"BringWindowToTop\"](win_hwnd)\n"
        "            result = bool(ops[\"SetForegroundWindow\"](win_hwnd))\n"
        "        if True:\n"
        "            if attached:\n"
        "                ops[\"AttachThreadInput\"](\n"
        "                    current_thread, fg_thread, False,\n"
        "                )\n",
        ["test_a_refusal_returns_false_warns_and_still_detaches"],
    ),
    # A refusal reads as success, which is exactly the false "brought its
    # window forward" line David saw.
    mutation(
        "success-regardless-of-setforeground",
        "        return result\n",
        "        return True\n",
        ["test_detaches_even_when_set_foreground_fails",
         "test_a_refusal_returns_false_warns_and_still_detaches"],
    ),
]


if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
