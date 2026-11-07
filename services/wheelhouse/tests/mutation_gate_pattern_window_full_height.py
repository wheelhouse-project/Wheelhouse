r"""Mutation gate for the full-height Pattern Manager window.

wh-pattern-window-full-height. On its first show, PatternManagerDialog
makes its whole frame, title bar included, fill the available height of its
screen (_fit_frame_to_available_height, called from showEvent). A later show
keeps the size the user chose.

WHAT IT COVERS:

  fit-never-called       showEvent does not call the fit
  first-show-only-gone   every show runs the fit, so a restore from
                         minimized undoes the user's size
  title-bar-ignored      the fit gives the client area the whole height, so
                         the frame goes under the taskbar
  frame-top-not-moved    the fit keeps the frame where Qt first put it
  left-edge-unclamped    the fit does not pull the frame right onto the screen
  right-edge-unclamped   the fit does not pull the frame left onto the screen
  moved-flag-kept        the fit leaves WA_Moved set, so a reopen no longer
                         centers the window

Run from services/wheelhouse with the interpreter the whole worktree uses
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_pattern_window_full_height.py --check
    .venv/Scripts/python.exe tests/mutation_gate_pattern_window_full_height.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
DIALOG = SERVICE / "pattern_manager_dialog.py"
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

TESTS = ("tests/test_pattern_manager_window_scaling.py",)

CALL = "            self._fit_frame_to_available_height()\n"
FIRST_ONLY = "        if not self._default_geometry_applied:\n"
EXTRA_H = "        extra_h = frame.height() - self.height()\n"
MOVE = "        self.move(max(x, available.x()), available.y())\n"
RIGHT = (
    "        x = min(frame.x(), available.x() + available.width() - frame_w)\n"
)

FILLS = "test_first_show_fills_the_available_height_with_the_whole_frame"
KEEPS = "test_a_later_show_keeps_the_size_the_user_chose"
RECENTERS = "test_the_fit_leaves_a_reopen_free_to_center_the_window"
MOVED = "        self.setAttribute(Qt.WidgetAttribute.WA_Moved, False)\n"


# The shared runner removes the parameter id from each collected name
# (mutation_gate_runner._collected_names), so a mutation names the test
# function. The comment on each mutation names the case that fails.


def mutation(name, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=DIALOG,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("fit-never-called", CALL, "            pass\n", [FILLS]),
    mutation("first-show-only-gone", FIRST_ONLY, "        if True:\n",
             [KEEPS]),
    mutation("title-bar-ignored", EXTRA_H, "        extra_h = 0\n",
             [FILLS]),
    mutation("frame-top-not-moved", MOVE,
             "        self.move(max(x, available.x()), frame.y())\n",
             [FILLS]),  # taskbar-at-bottom
    mutation("left-edge-unclamped", MOVE,
             "        self.move(x, available.y())\n",
             [FILLS]),  # screen-to-the-right
    mutation("right-edge-unclamped", RIGHT, "        x = frame.x()\n",
             [FILLS]),  # screen-to-the-left
    mutation("moved-flag-kept", MOVED, "        pass\n", [RECENTERS]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
