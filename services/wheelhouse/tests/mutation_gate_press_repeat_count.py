r"""Mutation gate for the repeat count on "press" (wh-voice-access-parity.2.12).

WHAT THE CHANGE DOES. The press-keys row in speech/config/patterns.toml
became ^press\s*(.+?)(?:\s+(\d+)\s+times?)?$ with params ["g1", "g2"], so
"press tab 3 times" passes "3" to press_keys. press_keys
(speech/actions.py) reads it through _clamped_repeat, the reading press()
uses, and clamps it to HOTKEY_REPEAT_CAP (30), because its payload is a
hotkey_action that pauses between repeats (Boss e8 ruling 2026-09-27
19:57). press() keeps its cap of 50.

WHAT IT COVERS (acceptance A5: the count capture, the clamp, and the
default of one):

  count-group-removed        the row loses its "<n> times" group
  times-made-optional        the word "times" is no longer required, so
                             "press control 2" reads 2 as a count
  key-group-greedy           the key group swallows the count
  key-group-off-greedy-buffer the key group is no longer a greedy tail, so
                             a pause longer than the command timer inside
                             the count types the phrase (at word speed the
                             required word "times" still holds it)
  count-not-passed           the row passes only g1
  count-ignored              press_keys always sends repeat 1
  press-keys-cap-50          press_keys clamps at press()'s 50, not 30
  cap-removed                the shared clamp has no upper bound
  zero-not-one               a count of zero is sent as zero
  default-not-one            a zero or unreadable count presses twice
  none-reads-as-two          words_to_int reads a missing count as 2
  default-argument-not-none  press_keys called with no count presses twice

Run from services/wheelhouse with the interpreter the whole worktree uses
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_press_repeat_count.py --check
    .venv/Scripts/python.exe tests/mutation_gate_press_repeat_count.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
ACTIONS = SERVICE / "speech/actions.py"
PATTERNS = SERVICE / "speech/config/patterns.toml"
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
    "tests/test_press_repeat_count.py",
    "tests/e2e/test_e2e_workflows.py::TestMultiStepCommands"
    "::test_press_waits_for_a_two_word_count",
    "tests/e2e/test_e2e_workflows.py::TestMultiStepCommands"
    "::test_press_count_survives_a_pause_longer_than_the_command_timer",
)

PRESS_ROW = r"pattern = '''^press\s*(.+?)(?:\s+(\d+)\s+times?)?$'''" + "\n"
PRESS_PARAMS = '    { function = "press_keys", params = ["g1", "g2"] }\n'
PASS_COUNT = "                \"repeat\": _clamped_repeat(repeat_str, HOTKEY_REPEAT_CAP),\n"
DEFAULT_LINE = "    if repeat_count is None or repeat_count < 1: repeat_count = 1\n"
CAP_LINE = "    if repeat_count > cap: repeat_count = cap\n"
SIGNATURE = "    def press_keys(self, key_sequence: str, repeat_str=None):\n"
NONE_LINE = "    if text is None: return 1\n"

ENGINE_COUNT = "test_count_presses_the_keys_that_many_times"
TWO_WORD = "test_press_waits_for_a_two_word_count"
PAUSE = "test_press_count_survives_a_pause_longer_than_the_command_timer"


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("count-group-removed", PATTERNS, PRESS_ROW,
             r"pattern = '''^press\s*(.+?)$'''" + "\n",
             [ENGINE_COUNT, TWO_WORD]),
    mutation("times-made-optional", PATTERNS, PRESS_ROW,
             PRESS_ROW.replace(r"\s+times?)?$", r"(?:\s+times?)?)?$"),
             ["test_no_count_presses_once_as_before"]),
    mutation("key-group-greedy", PATTERNS, PRESS_ROW,
             PRESS_ROW.replace("(.+?)", "(.+)"),
             [ENGINE_COUNT, TWO_WORD]),
    mutation("key-group-off-greedy-buffer", PATTERNS, PRESS_ROW,
             PRESS_ROW.replace("(.+?)", r"([^\n]+?)"),
             [PAUSE]),
    mutation("count-not-passed", PATTERNS, PRESS_PARAMS,
             PRESS_PARAMS.replace('["g1", "g2"]', '["g1"]'),
             [ENGINE_COUNT, TWO_WORD]),
    mutation("count-ignored", ACTIONS, PASS_COUNT,
             "                \"repeat\": 1,\n",
             ["test_digit_count", "test_word_count",
              "test_count_applies_to_a_combination", ENGINE_COUNT, TWO_WORD]),
    mutation("press-keys-cap-50", ACTIONS, PASS_COUNT,
             PASS_COUNT.replace(", HOTKEY_REPEAT_CAP)", ")"),
             ["test_count_capped_at_the_hotkey_repeat_cap",
              "test_press_keeps_its_own_cap_of_50", ENGINE_COUNT]),
    mutation("cap-removed", ACTIONS, CAP_LINE, "    pass\n",
             ["test_count_capped_at_the_hotkey_repeat_cap",
              "test_press_keeps_its_own_cap_of_50", ENGINE_COUNT]),
    mutation("zero-not-one", ACTIONS, DEFAULT_LINE,
             "    if repeat_count is None: repeat_count = 1\n",
             ["test_zero_count_presses_once"]),
    # words_to_int(None) returns 1 itself, so this line sets the default
    # only for a zero, negative, or unreadable count. The no-count default
    # is guarded by none-reads-as-two below.
    mutation("default-not-one", ACTIONS, DEFAULT_LINE,
             DEFAULT_LINE.replace("repeat_count = 1", "repeat_count = 2"),
             ["test_zero_count_presses_once",
              "test_unreadable_count_presses_once"]),
    mutation("none-reads-as-two", ACTIONS, NONE_LINE,
             NONE_LINE.replace("return 1", "return 2"),
             ["test_no_count_presses_once", "test_none_count_presses_once",
              "test_no_count_presses_once_as_before"]),
    mutation("default-argument-not-none", ACTIONS, SIGNATURE,
             SIGNATURE.replace("repeat_str=None", 'repeat_str="2"'),
             ["test_no_count_presses_once"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
