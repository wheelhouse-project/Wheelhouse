"""Mutation gate for the tray Push-to-Talk Mode toggle (wh-tray-ptt-toggle-thread).

WHAT THE CHANGE DOES. The tray menu item "Push-to-Talk Mode" and the
double-click branch of GuiManager._on_tray_left_click ran _toggle_ptt_mode
on pystray's thread, and that method changes Qt widgets. Each one now puts
{"action": "toggle_ptt_mode"} on state_from_logic_queue, the same way as
the tray About item, and _check_queues_and_events calls _toggle_ptt_mode
on the Qt thread.

WHAT IT COVERS:

  tray-item-calls-directly     the tray item calls _toggle_ptt_mode again
  tray-item-wrong-action       the tray item queues an action nobody handles
  tray-item-wrong-queue        the tray item puts the request on the Logic queue
  double-click-calls-directly  the tray double-click calls _toggle_ptt_mode again
  double-click-wrong-action    the double-click queues an action nobody handles
  handler-branch-missing       the queue handler has no toggle_ptt_mode branch
  handler-does-nothing         the branch exists but never changes the mode

Run from services/wheelhouse with the interpreter of the worktree venv
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_tray_ptt_toggle_thread.py --check
    .venv/Scripts/python.exe tests/mutation_gate_tray_ptt_toggle_thread.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
GUI = SERVICE / "gui.py"
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
    "tests/test_gui.py::TestTrayLeftClick",
    "tests/test_gui.py::TestPTTModeMenuItem",
)

TRAY_ITEM = (
    '                    lambda: self.state_from_logic_queue.put('
    '{"action": "toggle_ptt_mode"}),\n'
)
DOUBLE_CLICK = (
    '            self.state_from_logic_queue.put('
    '{"action": "toggle_ptt_mode"})\n'
)
HANDLER_BRANCH = '                elif action == "toggle_ptt_mode":\n'
HANDLER_CALL = (
    "                    # the mode change updates Qt widgets.\n"
    "                    self._toggle_ptt_mode()\n"
)

TRAY_CATCHER = ["test_the_tray_item_changes_nothing_on_the_pystray_thread"]
DOUBLE_CATCHERS = [
    "test_tray_double_click_queues_the_mode_change",
    "test_tray_double_click_switches_mode",
    "test_tray_double_click_from_ptt_to_toggle",
]
HANDLER_CATCHERS = [
    "test_the_gui_thread_changes_the_mode_after_the_queued_request",
    "test_tray_double_click_switches_mode",
    "test_tray_double_click_from_ptt_to_toggle",
]


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=GUI,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("tray-item-calls-directly", TRAY_ITEM,
             "                    self._toggle_ptt_mode,\n",
             TRAY_CATCHER),
    mutation("tray-item-wrong-action", TRAY_ITEM,
             TRAY_ITEM.replace('"toggle_ptt_mode"', '"toggle_ptt"'),
             TRAY_CATCHER),
    mutation("tray-item-wrong-queue", TRAY_ITEM,
             TRAY_ITEM.replace("state_from_logic_queue",
                               "commands_to_logic_queue"),
             TRAY_CATCHER),
    mutation("double-click-calls-directly", DOUBLE_CLICK,
             "            self._toggle_ptt_mode()\n",
             DOUBLE_CATCHERS),
    mutation("double-click-wrong-action", DOUBLE_CLICK,
             DOUBLE_CLICK.replace('"toggle_ptt_mode"', '"toggle_ptt"'),
             DOUBLE_CATCHERS),
    mutation("handler-branch-missing", HANDLER_BRANCH,
             HANDLER_BRANCH.replace('"toggle_ptt_mode"',
                                    '"toggle_ptt_mode_unused"'),
             HANDLER_CATCHERS),
    mutation("handler-does-nothing", HANDLER_CALL,
             HANDLER_CALL.replace("self._toggle_ptt_mode()", "pass"),
             HANDLER_CATCHERS),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
