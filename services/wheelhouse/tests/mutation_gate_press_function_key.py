r"""Mutation gate for "f" and a number as one function key.

wh-press-f-number-function-key. press_keys (speech/actions.py) reads the key
word "f" followed by a number N as the one key fN when fN is a key name in
VK_CODE_MAP. _function_key reads the number with words_to_int, so "f 5",
"f five" and "f for" all give f5, and "f 0" and "f 13" name no key.

WHAT IT COVERS (acceptance A5: the new mapping):

  f-branch-removed       press_keys never reads "f" and a number as one key
  any-word-and-a-number  any key word followed by a number becomes fN, so
                         "control 2" loses its ctrl
  no-key-name-check      _function_key returns fN without checking
                         VK_CODE_MAP, so "f 0" becomes the unknown key f0
  digits-only            _function_key reads digits only, so "f five" and
                         "f for" fail
  number-word-reused     press_keys advances past "f" only, so the number
                         is also pressed as its own key

Run from services/wheelhouse with the interpreter the whole worktree uses
(see tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    .venv/Scripts/python.exe tests/mutation_gate_press_function_key.py --check
    .venv/Scripts/python.exe tests/mutation_gate_press_function_key.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
ACTIONS = SERVICE / "speech/actions.py"
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

TESTS = ("tests/test_press_repeat_count.py",)

BRANCH = '            if words[i] == "f" and i + 1 < len(words):\n'
APPEND = "                    normalized_keys.append(function_key)\n"
KEY_CHECK = "    return key if key in VK_CODE_MAP else None\n"
READ_NUMBER = "    number = words_to_int(number_word)\n"

ONE_KEY = "test_f_and_a_number_is_one_function_key"
UNCHANGED = "test_other_sequences_are_unchanged"
STILL_FAILS = "test_f_and_a_number_with_no_function_key_still_fails"
PATTERN = "test_through_the_shipped_pattern"
AS_BEFORE = "test_no_count_presses_once_as_before"


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("f-branch-removed", ACTIONS, BRANCH,
             "            if False:\n",
             [ONE_KEY, PATTERN, AS_BEFORE]),
    mutation("any-word-and-a-number", ACTIONS, BRANCH,
             "            if i + 1 < len(words):\n",
             [UNCHANGED, PATTERN]),
    mutation("no-key-name-check", ACTIONS, KEY_CHECK,
             "    return key\n",
             [UNCHANGED, STILL_FAILS, PATTERN]),
    mutation("digits-only", ACTIONS, READ_NUMBER,
             "    number = int(number_word) if number_word.isdigit() else None\n",
             [ONE_KEY, PATTERN]),
    # Undo one step of the i += 2 that follows, so the number word is read
    # again as a key of its own: "f 5" becomes f5 plus 5.
    mutation("number-word-reused", ACTIONS, APPEND,
             "                    normalized_keys.append(function_key); i -= 1\n",
             [ONE_KEY, PATTERN, AS_BEFORE]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
