"""Mutation gate for the wake word during the sound pause (wh-playback-disable-fails.1).

WHAT THE CHANGE DOES. While the sound pause is active (the audio monitor
reports sound and the startup decision applies the pause), the wake word
"computer" changes no flag, writes one log line, shows no notice, and tells
the provider again that listening is off with the reason from
_engine_status_reason. The speech toggle still ends the sound pause.

WHAT IT COVERS (names are the mutation names below): the condition that
decides the sound pause (both halves), the early return, the re-sent status
and its reason, the log line, and an over-broad fix that would also stop the
speech toggle.

Every mutation names the tests that must fail, by the bare test name: the
runner removes the parameter list from every collected and every failed name,
so a parametrized test counts as caught when any of its cases fails. The runner requires exactly one
pattern match, compiles each mutant, validates the expected names against real
collection, and requires a green baseline (tests/../stt_providers/shared/tests/
mutation_gate_runner.py).

Run from services/wheelhouse with the interpreter of the worktree venv (pytest
starts as ``<this interpreter> -m pytest``, never through ``uv run``):

    .venv/Scripts/python.exe tests/mutation_gate_wake_word_sound_pause.py --check
    .venv/Scripts/python.exe tests/mutation_gate_wake_word_sound_pause.py

Set MUTATION_GATE_REASONS to a file path to append, for every pytest run, the
short-summary FAILED lines (with the assertion text) under the mutation's name.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
STATE_MANAGER = SERVICE / "state_manager.py"
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

_current = {"name": "(before the first mutation)"}
_original_save_recovery = runner._save_recovery
_original_run_pytest = runner._run_pytest


def _remember_name(path, original, mutated, name):
    _current["name"] = name
    return _original_save_recovery(path, original, mutated, name)


def _log_reasons(service, test_file):
    """Run pytest; when asked, keep the FAILED summary lines with their reasons."""
    proc = _original_run_pytest(service, test_file)
    target = os.environ.get("MUTATION_GATE_REASONS")
    if target:
        out = proc.stdout + proc.stderr
        lines = out.splitlines()
        for index, line in enumerate(lines):
            if "short test summary info" in line:
                lines = lines[index + 1:]
                break
        else:
            lines = []
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"=== {_current['name']} (exit {proc.returncode})\n")
            for line in lines:
                if line.startswith(("FAILED", "ERROR")):
                    handle.write(line + "\n")
    return proc


runner._save_recovery = _remember_name
runner._run_pytest = _log_reasons

TESTS = (
    "tests/test_wake_word_sound_pause.py",
    "tests/test_wake_word_stop_listening.py",
    "tests/test_audio_suppression_control.py",
)

CONDITION = "        if self._speech_suppressed_by_audio and self._audio_suppression_active and not self._ptt_audio_override:\n            # The computer plays sound."
LOG_TEXT = '                f"the computer plays sound (sound pause active)"\n'
RESEND = "                    False, reason=reason\n"
TOGGLE_REASON = "                reason = self._engine_status_reason(False, None)\n"
PTT_REASON = '                    reason = "audio"\n'
EARLY_RETURN = (
    "                self.loop.create_task(self.websocket_manager.broadcast(message))\n"
    "            return\n"
    "        if self._speech_interaction_mode == \"push_to_talk\":\n"
)
ENABLE_HEAD = (
    "        old_speech_enabled = self.speech_enabled\n"
    "        self._set_speech_enabled_explicitly(True)\n"
)

ITEM1 = [
    "test_listening_stays_off",
    "test_every_pause_flag_keeps_its_value",
]
# Item 1 in push-to-talk mode (wh-playback-disable-fails.1.1.3).
ITEM1_PTT = [
    "test_every_pause_flag_keeps_its_value_in_push_to_talk",
    "test_the_engine_is_told_listening_stays_off_in_push_to_talk",
    "test_one_log_line_names_the_sound_in_push_to_talk",
]
ITEM2 = [
    "test_the_wake_word_turns_listening_on_and_clears_every_pause",
    "test_a_wake_word_after_the_sound_stops_turns_listening_on",
]


def sm(name, old, new, expect):
    return dict(name="sm-" + name, service=SERVICE, test_file=TESTS,
                file=STATE_MANAGER, old=old, new=new, expect=expect)


MUTATIONS = [
    # Item 2: the wake word with no sound pause must still turn listening on.
    sm("every-wake-word-is-ignored",
       CONDITION,
       CONDITION.replace(
           "if self._speech_suppressed_by_audio and self._audio_suppression_active and not self._ptt_audio_override:",
           "if True:"),
       ITEM2),
    # Item 2, input level: the startup decision must count, or a stale sound
    # flag blocks the wake word when the sound pause does not apply.
    sm("decision-half-dropped",
       CONDITION,
       CONDITION.replace(" and self._audio_suppression_active", ""),
       ["test_with_the_startup_decision_off_sound_does_not_stop_the_wake_word"]),
    # Item 1, input level: the check must read the sound flag, not another pause.
    sm("reads-the-sonos-flag",
       CONDITION,
       CONDITION.replace("self._speech_suppressed_by_audio and",
                         "self._speech_suppressed_by_sonos and"),
       ITEM1 + ["test_the_wake_word_turns_listening_on_and_clears_every_pause"]),
    # Item 1: without the early return the wake word falls through and enables.
    sm("no-early-return",
       EARLY_RETURN,
       EARLY_RETURN.replace("            return\n", "            pass\n"),
       ITEM1 + ["test_the_engine_is_told_listening_stays_off_for_the_sound",
                "test_no_notice_and_no_gui_state_change",
                "test_every_pause_flag_keeps_its_value_in_push_to_talk"]),
    # Item 1: the provider must be told again that listening is off.
    sm("status-says-on",
       RESEND,
       "                    True, reason=reason\n",
       ["test_the_engine_is_told_listening_stays_off_for_the_sound",
        "test_a_wake_word_during_a_sound_pause_leaves_listening_off"]),
    # Ruling R2: the reason must keep the detector armed ("ptt" disarms it).
    sm("status-reason-disarms",
       TOGGLE_REASON,
       '                reason = "ptt"\n',
       ["test_the_engine_is_told_listening_stays_off_for_the_sound",
        "test_a_wake_word_during_a_sound_pause_leaves_listening_off"]),
    # Item 1 in push-to-talk mode: the sound check must run there too, or the
    # push-to-talk wake word ends the idle pause during the sound pause.
    sm("sound-check-skipped-in-push-to-talk",
       CONDITION,
       CONDITION.replace(
           "and not self._ptt_audio_override:",
           "and not self._ptt_audio_override"
           " and self._speech_interaction_mode != \"push_to_talk\":"),
       ITEM1_PTT),
    # Ruling R2 in push-to-talk mode: the reason must stay "audio", because
    # the push-to-talk reason None disarms the detector.
    sm("push-to-talk-reason-disarms",
       PTT_REASON,
       "                    pass\n",
       ["test_the_engine_is_told_listening_stays_off_in_push_to_talk"]),
    # A push-to-talk hold lifts the sound pause: without the hold term the
    # wake word switches transcription off in the middle of a hold.
    sm("hold-does-not-lift-the-check",
       CONDITION,
       CONDITION.replace(" and not self._ptt_audio_override", ""),
       ["test_a_wake_word_during_a_hold_leaves_the_hold_on"]),
    # Item 4: the log line names the reason.
    sm("log-line-without-the-reason",
       LOG_TEXT,
       '                f"no reason given"\n',
       ["test_one_log_line_names_the_wake_word_and_the_sound"]),
    # Item 3: an over-broad fix in the shared enabling method would also stop
    # the speech toggle during the sound pause.
    sm("toggle-also-blocked-by-sound",
       ENABLE_HEAD,
       "        if self._speech_suppressed_by_audio and self._audio_suppression_active:\n"
       "            return\n" + ENABLE_HEAD,
       ["test_the_toggle_turns_listening_on"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
