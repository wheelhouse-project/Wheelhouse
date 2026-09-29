"""Mutation gate for the "Create an advanced command" goal (wh-advanced-command-choice).

WHAT THE CHANGE DOES. The last choice in the Add Pattern list "What do you
want to happen?" (create_pattern_dialog.py _GOAL_TEMPLATES) was "Start from
scratch" and opened the simple editor. It is now "Create an advanced
command": its template carries ``"advanced": True``, and
_apply_goal_template ticks the Advanced check box and puts focus in the
"Regular expression:" field. The other five choices still open the simple
editor with Advanced unticked (David's answer to QUESTIONS-2026-09-28.md
item 11).

WHAT IT COVERS:

  title-reverted            the choice reads "Start from scratch" again
  flag-dropped              the template no longer asks for Advanced
  tick-removed              _apply_goal_template never ticks Advanced
  every-goal-ticks          every choice ticks Advanced
  focus-left-on-phrase-row  focus falls to the hidden simple-pane phrase row

Run from services/wheelhouse with the interpreter the whole worktree uses (the
main checkout's interpreter in a worktree; see
tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_advanced_command_choice.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_advanced_command_choice.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
DIALOG = SERVICE / "create_pattern_dialog.py"
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

TESTS = ("tests/test_create_pattern_dialog.py::TestGoalPage",)

APPLY_ADVANCED = (
    "        if template.get(\"advanced\"):\n"
    "            # The toggle's slot switches the mode and syncs the panes.\n"
    "            self._advanced_toggle.setChecked(True)\n"
    "            self._expression_edit.setFocus()\n"
    "            return\n"
)
# The runner matches names with the parameter id cut off.
OTHER_GOALS = ["test_other_goals_leave_advanced_unticked"]


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=DIALOG,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("title-reverted",
             '        "title": "Create an advanced command",\n',
             '        "title": "Start from scratch",\n',
             ["test_last_goal_is_create_an_advanced_command"]),
    mutation("flag-dropped",
             '        "advanced": True,\n',
             '        "advanced": False,\n',
             ["test_advanced_goal_opens_editor_with_advanced_ticked"]),
    mutation("tick-removed", APPLY_ADVANCED,
             APPLY_ADVANCED.replace(
                 "self._advanced_toggle.setChecked(True)", "pass"),
             ["test_advanced_goal_opens_editor_with_advanced_ticked"]),
    mutation("every-goal-ticks", APPLY_ADVANCED,
             APPLY_ADVANCED.replace(
                 'if template.get("advanced"):', "if True:"),
             OTHER_GOALS),
    mutation("focus-left-on-phrase-row", APPLY_ADVANCED,
             APPLY_ADVANCED.replace(
                 "            self._expression_edit.setFocus()\n"
                 "            return\n",
                 "            self._focus_first_empty_field()\n"
                 "            return\n"),
             ["test_advanced_goal_opens_editor_with_advanced_ticked"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
