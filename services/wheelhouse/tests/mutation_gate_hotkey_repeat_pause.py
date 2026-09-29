"""Mutation gate for the pause between hotkey repeats (wh-voice-access-parity.1.15.4).

WHAT THE FIX DOES. UIActionHandler.hotkey_action (ui/ui_action_handler.py)
sent the repeats of one hotkey back to back, and VS Code selected one word for
"select forward 3 words". It now sleeps HOTKEY_REPEAT_PAUSE_S (0.1 s) before
every repeat after the first, in the SendInput loop and in the Flutter
SendKeys loop. Probe 2 (2026-09-26 11:42) passed 5 of 5 only at 100 ms and
above. speech/actions.py HOTKEY_REPEAT_CAP (30, was 50) keeps the total pause
at 2.9 s, under the 5.0 s wait for the Input process's answer (Boss e8 ruling
2026-09-26 11:49).

WHAT IT COVERS:

  pause-removed                  the SendInput loop does not pause
  pause-under-100-ms             the pause is 50 ms
  pause-before-the-first-press   the SendInput loop pauses before every press
  flutter-pause-removed          the SendKeys loop does not pause
  cap-back-at-50                 the cap is 50 again
  cap-check-removed              a count above the cap is sent unchanged
  catalog-states-50              the "hk" repeat text in action_catalog.py
                                 says 50 again
  navigation-cap-back-at-50      speech/navigation/parser.py MAX_COUNT is 50
                                 again, so "go right fifty words" sends 50
                                 repeats (wh-voice-access-parity.1.15.6.1)
  navigation-clamp-removed       a cursor count above MAX_COUNT is sent
                                 unchanged
  help-states-50                 the help document's Navigation section says
                                 "Counts above 50 move 50" again
                                 (wh-voice-access-parity.1.15.6.2)

Run from services/wheelhouse with the interpreter the whole worktree uses (the
main checkout's interpreter in a worktree; see
tests/mutation_gate_equals_spacing.py for why pytest starts as
``<this interpreter> -m pytest``):

    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_hotkey_repeat_pause.py --check
    <main>/services/wheelhouse/.venv/Scripts/python.exe \\
        tests/mutation_gate_hotkey_repeat_pause.py
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
HANDLER = SERVICE / "ui/ui_action_handler.py"
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

NAV_PARSER = SERVICE / "speech/navigation/parser.py"
CATALOG = SERVICE / "speech/action_catalog.py"
HELP_SECTION = SERVICE / "knowledge/helpdoc/sections/070-voice-commands.md"

TESTS = (
    "tests/test_ui/test_ui_action_handler.py::TestHotkeyAction",
    "tests/test_actions.py::TestHotkey",
    "tests/test_navigation_parser.py",
)

PAUSE_VALUE = "HOTKEY_REPEAT_PAUSE_S = 0.1\n"
SENDINPUT_PAUSE = (
    "                    if i:\n"
    "                        time.sleep(HOTKEY_REPEAT_PAUSE_S)\n"
    "                    ok, accepted, expected = verified_press_keys(\n"
)
FLUTTER_PAUSE = (
    "                        time.sleep(HOTKEY_REPEAT_PAUSE_S)\n"
    "                    focused_control.SendKeys(sendkeys_str)\n"
)
CAP_VALUE = "HOTKEY_REPEAT_CAP = 30\n"
CAP_CHECK = "                    if repeat_count > HOTKEY_REPEAT_CAP:\n"


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=file,
                old=old, new=new, expect=expect)


MUTATIONS = [
    mutation("pause-removed", HANDLER, SENDINPUT_PAUSE,
             SENDINPUT_PAUSE.replace("time.sleep(HOTKEY_REPEAT_PAUSE_S)", "pass"),
             ["test_repeated_hotkey_pauses_at_least_100_ms_between_presses",
              "test_refused_hotkey_stops_without_a_further_pause"]),
    mutation("pause-under-100-ms", HANDLER, PAUSE_VALUE,
             "HOTKEY_REPEAT_PAUSE_S = 0.05\n",
             ["test_repeated_hotkey_pauses_at_least_100_ms_between_presses",
              "test_repeated_flutter_hotkey_pauses_between_presses"]),
    mutation("pause-before-the-first-press", HANDLER, SENDINPUT_PAUSE,
             SENDINPUT_PAUSE.replace("if i:", "if True:"),
             ["test_repeated_hotkey_pauses_at_least_100_ms_between_presses",
              "test_single_hotkey_does_not_pause"]),
    mutation("flutter-pause-removed", HANDLER, FLUTTER_PAUSE,
             FLUTTER_PAUSE.replace("time.sleep(HOTKEY_REPEAT_PAUSE_S)", "pass"),
             ["test_repeated_flutter_hotkey_pauses_between_presses"]),
    mutation("cap-back-at-50", ACTIONS, CAP_VALUE,
             "HOTKEY_REPEAT_CAP = 50\n",
             ["test_repeat_capped_at_30"]),
    mutation("cap-check-removed", ACTIONS, CAP_CHECK,
             "                    if False:\n",
             ["test_repeat_capped_at_30"]),
    mutation("catalog-states-50", CATALOG,
             "combination (capped at 30); often",
             "combination (capped at 50); often",
             ["test_action_catalog_states_the_hotkey_repeat_cap"]),
    mutation("navigation-cap-back-at-50", NAV_PARSER,
             "MAX_COUNT = 30\n", "MAX_COUNT = 50\n",
             ["test_max_count_is_the_hotkey_repeat_cap",
              "test_a_large_count_sends_at_most_30_repeats",
              "test_a_count_above_the_maximum_still_clamps",
              "test_leading_zeroes_do_not_make_a_count_too_long"]),
    mutation("help-states-50", HELP_SECTION,
             "Counts above 30 move 30.", "Counts above 50 move 50.",
             ["test_the_help_document_states_max_count"]),
    mutation("navigation-clamp-removed", NAV_PARSER,
             "        return min(n, MAX_COUNT)\n", "        return n\n",
             ["test_a_large_count_sends_at_most_30_repeats",
              "test_a_count_above_the_maximum_still_clamps",
              "test_leading_zeroes_do_not_make_a_count_too_long"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
