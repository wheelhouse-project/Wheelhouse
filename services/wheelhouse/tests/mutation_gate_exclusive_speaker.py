"""Mutation gate for the exclusive-speaker pause (wh-kodi-playback-echo-stutter).

WHAT THE CHANGE DOES. While an app (Kodi with WASAPI exclusive output) holds
the default speaker in exclusive mode, WheelHouse pauses all listening.
handlers/exclusive_speaker_monitor.py detects the hold (method A: a shared-mode
Initialize that answers AUDCLNT_E_DEVICE_IN_USE; method B: a held stream whose
session disconnects with reason 5) and publishes
SpeakerExclusiveStateChangedEvent. StateManager sets or clears the pause.
service_manager.py builds, starts and stops the monitor. LogicController.main
hands StateManager the setting gate from ENABLE_AUDIO_SUPPRESSION.

WHAT IT COVERS (names are the mutation names below):

  sm-*    state_manager.py: the reason that is sent while the pause holds, the
          wake-word guard, the setting gate, push-to-talk getting no exception,
          the release restoring listening, the user's switch left alone, the
          engine and button announcements, the subscription and the handler.
  em-*    handlers/exclusive_speaker_monitor.py: both detection methods, the
          generation check on notices, the default-speaker change and removal,
          publish-on-change only, warn-once, the closed-loop guard, the daemon
          thread, a stop() that does not wait, the poll interval, and the
          source rules (never exclusive mode, never started, never written to).
  svm-*   service_manager.py: build, start, stop and contained stop failure.
  main-*  main.py: the gate value, and the startup order: the gate, the detector
          and its bounded first-answer wait come before the STT provider starts
          (wh-kodi-playback-echo-stutter.1.1); the sound-pause decision and
          start_services come after.

Every mutation names the tests that must fail. The runner requires exactly one
pattern match, compiles each mutant, validates the expected names against real
collection, and requires a green baseline (tests/../stt_providers/shared/tests/
mutation_gate_runner.py).

Run from services/wheelhouse with the interpreter of the worktree venv (pytest
starts as ``<this interpreter> -m pytest``, never through ``uv run``):

    .venv/Scripts/python.exe tests/mutation_gate_exclusive_speaker.py --check
    .venv/Scripts/python.exe tests/mutation_gate_exclusive_speaker.py

Set MUTATION_GATE_REASONS to a file path to append, for every pytest run, the
short-summary FAILED lines (with the assertion text) under the mutation's name.
That is how a reader confirms that the expected assertion fired and not an
unrelated crash upstream of it.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services/wheelhouse"
STATE_MANAGER = SERVICE / "state_manager.py"
MONITOR = SERVICE / "handlers/exclusive_speaker_monitor.py"
SERVICE_MANAGER = SERVICE / "service_manager.py"
MAIN = SERVICE / "main.py"
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
    "tests/test_exclusive_speaker_monitor.py",
    "tests/test_exclusive_speaker_pause.py",
    "tests/test_audio_suppression_startup.py",
    "tests/test_state_manager.py",
)


def lines(*rows):
    """Source rows joined with newlines, ending in a newline."""
    return "\n".join(rows) + "\n"


def mutation(name, file, old, new, expect):
    return dict(name=name, service=SERVICE, test_file=TESTS, file=file,
                old=old, new=new, expect=expect)


def sm(name, old, new, expect):
    return mutation("sm-" + name, STATE_MANAGER, old, new, expect)


def em(name, old, new, expect):
    return mutation("em-" + name, MONITOR, old, new, expect)


def svm(name, old, new, expect):
    return mutation("svm-" + name, SERVICE_MANAGER, old, new, expect)


def main(name, old, new, expect):
    return mutation("main-" + name, MAIN, old, new, expect)


# main.py blocks the order mutations move. Each is a verbatim block of
# LogicController.main. _span copies the code from one block to another, so a
# mutation can move a block past the code between them without a copy of that
# code here; --check fails when a block changes.
_GATE_CALL = lines("            self.state_manager.apply_exclusive_speaker_pause_gate(",
                   "                exclusive_speaker_pause_on",
                   "            )")
_MONITOR_BLOCK = lines(
    "            exclusive_speaker_monitor = self.service_manager.exclusive_speaker_monitor",
    "            if exclusive_speaker_monitor:",
    "                exclusive_speaker_monitor.start()",
    "                if exclusive_speaker_pause_on:",
    "                    await exclusive_speaker_monitor.wait_first_answer()")
_WAIT_BLOCK = lines("                if exclusive_speaker_pause_on:",
                    "                    await exclusive_speaker_monitor.wait_first_answer()")
_PROVIDER_START = "                self.service_manager.start_remote_stt()\n"
_SERVICES_START = "            service_tasks = self.service_manager.start_services()\n"


def _span(start, end):
    """The text of main.py from the start of `start` to the end of `end`."""
    text = MAIN.read_bytes().decode("utf-8").replace("\r\n", "\n")
    first = text.index(start)
    return text[first:text.index(end, first) + len(end)]


def _indent(block, spaces=4):
    return "".join(" " * spaces + row if row.strip() else row
                   for row in block.splitlines(True))


# ----------------------------------------------------------------- state_manager

SEND_REASON = [
    "test_the_send_names_no_armed_reason",
]
SEND_START = SEND_REASON + [
    "test_another_pause_start_tells_the_engine_the_exclusive_reason",
]
WAKE_ENGINE = ["test_a_wake_word_during_the_pause_turns_the_engine_off_again"]
GATE_OFF = [
    "test_the_pause_does_not_apply_when_the_gate_is_off",
]
PAUSE_HOLDS = [
    "test_the_event_pauses_speech_and_the_release_restores_it",
    "test_the_start_tells_the_engine_and_the_button",
]

HELPER_TAIL = lines(
    '            return "exclusive_speaker"',
    "        return reason",
)
SPEECH_ENABLED_GUARD = lines(
    "        if self._exclusive_speaker_pause_active():",
    "            return False",
)
STATUS_REASON_GUARD = lines(
    "        if self._exclusive_speaker_pause_active():",
    '            return "exclusive_speaker"',
    "        if not self._speech_enabled:",
)
FLAG_ASSIGNMENT = "        self._speech_suppressed_by_exclusive_speaker = held\n"
END_REASON_TAIL = lines(
    "                    reason=self._engine_status_reason(self.speech_enabled, None),",
    "                )",
    "            self.loop.create_task(self.websocket_manager.broadcast(message))",
    "",
    "        self.send_state_update()",
    "",
    "    async def toggle_button_visibility",
).rstrip(chr(10))
BROADCAST_TAIL = lines(
    "            self.loop.create_task(self.websocket_manager.broadcast(message))",
    "",
    "        self.send_state_update()",
    "",
    "    async def toggle_button_visibility",
).rstrip(chr(10))
UPDATE_TAIL = lines(
    "        self.send_state_update()",
    "",
    "    async def toggle_button_visibility",
).rstrip(chr(10))
WAKE_GUARD_HEAD = lines(
    "        if self._exclusive_speaker_pause_active():",
    "            # An app holds the speaker in exclusive mode. The wake word must",
)
WAKE_SEND_HEAD = lines(
    "            # wake word, so tell it again that listening is off.",
    "            if self.websocket_manager:",
)
GATE_TERM = lines(
    "            self._speech_suppressed_by_exclusive_speaker",
    "            and self._exclusive_speaker_pause_gate",
)
GATE_TERM_CLOSE = lines(
    "            and self._exclusive_speaker_pause_gate",
    "        )",
)
REFUSAL_HEAD = (
    "        if self._exclusive_speaker_pause_active():\n"
    '            return "Another app has taken sole control'
)
REPEAT_GUARD = lines(
    "        if self._speech_suppressed_by_exclusive_speaker == held:",
    "            return",
)
GATE_OFF_RETURN = lines(
    "            return",
    "        new_speech_enabled = self.speech_enabled",
)

MUTATIONS = [
    # --- the reason sent while the pause holds
    sm("reason-helper-returns-the-given-reason",
       HELPER_TAIL,
       lines("            return reason", "        return reason"),
       SEND_START),
    sm("call-site-idle-sends-the-raw-reason",
       "                        self.speech_enabled, reason=self._reason_during_exclusive_pause"
       '("idle")\n',
       '                        self.speech_enabled, reason="idle"\n',
       SEND_START),
    sm("call-site-button-sends-the-raw-reason",
       "                self.speech_enabled, reason=self._reason_during_exclusive_pause"
       '("manual")\n',
       '                self.speech_enabled, reason="manual"\n',
       ["test_stop_listening_during_the_pause_tells_the_engine_the_exclusive_reason"]),
    sm("call-site-mode-switch-sends-the-raw-reason",
       "                    False, reason=self._reason_during_exclusive_pause"
       '("manual")\n',
       '                    False, reason="manual"\n',
       SEND_REASON),
    sm("call-site-sound-start-sends-the-raw-reason",
       "                        self.speech_enabled, reason=self._reason_during_exclusive_pause"
       '("audio")\n',
       '                        self.speech_enabled, reason="audio"\n',
       SEND_START),
    sm("call-site-sonos-start-sends-the-raw-reason",
       "                        self.speech_enabled, reason=self._reason_during_exclusive_pause"
       '("sonos")\n',
       '                        self.speech_enabled, reason="sonos"\n',
       SEND_START),
    sm("status-reason-is-an-armed-reason",
       STATUS_REASON_GUARD,
       STATUS_REASON_GUARD.replace('return "exclusive_speaker"', 'return "manual"'),
       ["test_the_reason_never_arms_the_wake_word",
        "test_another_pause_ending_keeps_the_exclusive_reason"]),
    sm("status-reason-clause-is-skipped",
       STATUS_REASON_GUARD,
       STATUS_REASON_GUARD.replace(
           "_pause_active():", "_pause_active() and False:"),
       ["test_the_reason_never_arms_the_wake_word",
        "test_another_pause_ending_keeps_the_exclusive_reason"]),
    # --- the wake-word guard
    sm("wake-guard-is-skipped",
       WAKE_GUARD_HEAD,
       WAKE_GUARD_HEAD.replace("_pause_active():", "_pause_active() and False:"),
       ["test_a_wake_word_in_toggle_mode_does_not_end_the_pause"] + WAKE_ENGINE),
    sm("wake-guard-sends-another-reason",
       '                    False, reason="exclusive_speaker"\n',
       '                    False, reason="manual"\n',
       WAKE_ENGINE),
    sm("wake-guard-sends-nothing",
       WAKE_SEND_HEAD,
       WAKE_SEND_HEAD.replace("if self.websocket_manager:",
                              "if self.websocket_manager and False:"),
       WAKE_ENGINE),
    # --- the setting gate
    sm("gate-is-ignored",
       GATE_TERM,
       GATE_TERM.replace("and self._exclusive_speaker_pause_gate", "and True"),
       GATE_OFF + [
           "test_a_wake_word_with_the_gate_off_works_as_before",
           "test_the_gate_off_pause_does_not_block_push_to_talk",
       ]),
    sm("gate-starts-closed",
       "        self._exclusive_speaker_pause_gate = True\n",
       "        self._exclusive_speaker_pause_gate = False\n",
       ["test_the_pause_is_on_when_nothing_sets_the_gate"]),
    sm("gate-setter-ignores-its-argument",
       "        self._exclusive_speaker_pause_gate = bool(enabled)\n",
       "        self._exclusive_speaker_pause_gate = True\n",
       GATE_OFF),
    sm("gate-follows-the-startup-decision",
       GATE_TERM_CLOSE,
       GATE_TERM_CLOSE.replace("self._exclusive_speaker_pause_gate",
                               "self._audio_suppression_active"),
       ["test_the_pause_applies_whatever_the_startup_decision_is"]),
    sm("gate-off-still-announces",
       GATE_OFF_RETURN,
       GATE_OFF_RETURN.replace("            return\n", "            pass\n", 1),
       GATE_OFF),
    # --- speech_enabled and push-to-talk
    sm("speech-enabled-ignores-the-pause",
       SPEECH_ENABLED_GUARD,
       SPEECH_ENABLED_GUARD.replace("return False", "pass"),
       PAUSE_HOLDS),
    sm("push-to-talk-gets-an-exception",
       SPEECH_ENABLED_GUARD,
       SPEECH_ENABLED_GUARD.replace(
           "_pause_active():", "_pause_active() and not self._ptt_active:"),
       ["test_push_to_talk_gets_no_exception"]),
    sm("push-to-talk-audio-override-lifts-the-pause",
       SPEECH_ENABLED_GUARD,
       SPEECH_ENABLED_GUARD.replace(
           "_pause_active():",
           "_pause_active() and not self._ptt_audio_override:"),
       ["test_the_push_to_talk_audio_override_does_not_lift_it"]),
    sm("push-to-talk-refusal-is-skipped",
       REFUSAL_HEAD,
       REFUSAL_HEAD.replace("_pause_active():", "_pause_active() and False:"),
       ["test_push_to_talk_gets_no_exception"]),
    sm("push-to-talk-refusal-does-not-name-the-speaker",
       'of the speaker. Listening is paused until it lets go."',
       'of the output. Listening is paused until it lets go."',
       ["test_push_to_talk_gets_no_exception"]),
    # --- the release, the user's switch, the repeat guard
    sm("release-does-not-restore-listening",
       FLAG_ASSIGNMENT,
       "        self._speech_suppressed_by_exclusive_speaker = True\n",
       ["test_the_event_pauses_speech_and_the_release_restores_it",
        "test_the_end_tells_the_engine_and_the_button"]),
    sm("pause-changes-the-user-switch",
       FLAG_ASSIGNMENT,
       FLAG_ASSIGNMENT + "        self._speech_enabled = not held\n",
       ["test_the_pause_does_not_change_the_user_switch"]),
    sm("release-clears-the-sound-pause",
       FLAG_ASSIGNMENT,
       FLAG_ASSIGNMENT
       + "        if not held:\n"
       + "            self._speech_suppressed_by_audio = False\n",
       ["test_the_sound_pause_still_pauses_after_the_exclusive_pause_ends"]),
    sm("release-clears-the-sonos-pause",
       FLAG_ASSIGNMENT,
       FLAG_ASSIGNMENT
       + "        if not held:\n"
       + "            self._speech_suppressed_by_sonos = False\n",
       ["test_the_sonos_pause_still_pauses_after_the_exclusive_pause_ends"]),
    sm("release-clears-the-idle-pause",
       FLAG_ASSIGNMENT,
       FLAG_ASSIGNMENT
       + "        if not held:\n"
       + "            self._speech_suppressed_by_idle = False\n",
       ["test_the_idle_pause_still_pauses_after_the_exclusive_pause_ends"]),
    sm("sound-pause-end-clears-the-exclusive-pause",
       "            self._speech_suppressed_by_audio = is_suppressed\n",
       "            self._speech_suppressed_by_audio = is_suppressed\n"
       "            if not is_suppressed:\n"
       "                self._speech_suppressed_by_exclusive_speaker = False\n",
       ["test_the_exclusive_pause_still_pauses_after_another_pause_ends"]),
    sm("repeat-event-is-not-ignored",
       REPEAT_GUARD,
       REPEAT_GUARD.replace("return", "pass"),
       ["test_a_repeat_event_tells_nobody",
        "test_a_repeat_event_logs_nothing",
        "test_a_release_with_no_hold_logs_nothing"]),
    # --- what the engine and the button hear
    sm("start-sends-another-reason",
       '                    self.speech_enabled, reason="exclusive_speaker"\n',
       '                    self.speech_enabled, reason="manual"\n',
       ["test_the_start_tells_the_engine_and_the_button"]),
    sm("end-sends-the-exclusive-reason",
       END_REASON_TAIL,
       END_REASON_TAIL.replace(
           "reason=self._engine_status_reason(self.speech_enabled, None),",
           'reason="exclusive_speaker",'),
       ["test_the_end_tells_the_engine_and_the_button"]),
    sm("start-and-end-send-nothing-to-the-engine",
       BROADCAST_TAIL,
       BROADCAST_TAIL.replace(
           "            self.loop.create_task(self.websocket_manager.broadcast(message))\n",
           "            pass\n", 1),
       ["test_the_start_tells_the_engine_and_the_button",
        "test_the_end_tells_the_engine_and_the_button"]),
    sm("start-and-end-do-not-update-the-button",
       UPDATE_TAIL,
       UPDATE_TAIL.replace("        self.send_state_update()\n", "        pass\n", 1),
       ["test_the_start_tells_the_engine_and_the_button",
        "test_the_end_tells_the_engine_and_the_button"]),
    # --- the subscription and the handler
    sm("subscribes-the-wrong-handler",
       lines("            SpeakerExclusiveStateChangedEvent,",
             "            self._handle_exclusive_speaker_state_changed,"),
       lines("            SpeakerExclusiveStateChangedEvent,",
             "            self._handle_sonos_state_changed,"),
       ["test_the_manager_subscribes_to_the_event"]),
    sm("handler-inverts-held",
       "            event.held, event.endpoint_name, event.method\n",
       "            not event.held, event.endpoint_name, event.method\n",
       ["test_the_event_pauses_speech_and_the_release_restores_it"]),
    sm("handler-swaps-endpoint-and-method",
       "            event.held, event.endpoint_name, event.method\n",
       "            event.held, event.method, event.endpoint_name\n",
       ["test_one_info_line_at_the_start_and_one_at_the_end"]),

    # ------------------------------------------------ exclusive_speaker_monitor
    # --- method A
    em("method-a-compares-another-hresult",
       "        if hresult == E_DEVICE_IN_USE:\n",
       "        if hresult == E_NOTFOUND:\n",
       ["test_in_use_answer_starts_held", "test_s_ok_after_held_ends_it",
        "test_publishes_only_on_a_change_of_state"]),
    em("method-a-in-use-publishes-free",
       "            self._set_held(True, self._endpoint_name, METHOD_SHARED_CHECK)\n",
       "            self._set_held(False, self._endpoint_name, METHOD_SHARED_CHECK)\n",
       ["test_in_use_answer_starts_held", "test_s_ok_after_held_ends_it"]),
    em("method-a-free-publishes-held",
       "            self._set_held(False, self._endpoint_name, METHOD_SHARED_CHECK)\n",
       "            self._set_held(True, self._endpoint_name, METHOD_SHARED_CHECK)\n",
       ["test_free_answer_from_the_start_publishes_nothing",
        "test_s_ok_after_held_ends_it"]),
    em("method-a-free-needs-another-answer",
       "        elif hresult == 0:\n",
       "        elif hresult == 1:\n",
       ["test_s_ok_after_held_ends_it",
        "test_end_after_reason_5_comes_from_the_shared_check"]),
    em("method-a-checks-another-endpoint",
       "            hresult = self._backend.check_in_use(self._endpoint_id)\n",
       '            hresult = self._backend.check_in_use("{wrong}")\n',
       ["test_checks_the_default_endpoint",
        "test_checks_the_new_endpoint_after_a_change"]),
    # --- method B
    em("method-b-ignores-reason-5",
       "            if reason == DISCONNECT_REASON_EXCLUSIVE_OVERRIDE:\n",
       "            if reason == DISCONNECT_REASON_EXCLUSIVE_OVERRIDE + 100:\n",
       ["test_reason_5_starts_held_without_waiting_for_the_next_check",
        "test_reason_5_through_the_thread_publishes_before_the_next_check",
        "test_end_after_reason_5_comes_from_the_shared_check"]),
    em("method-b-accepts-reason-2",
       "            if reason == DISCONNECT_REASON_EXCLUSIVE_OVERRIDE:\n",
       "            if reason in (DISCONNECT_REASON_EXCLUSIVE_OVERRIDE, 2):\n",
       ["test_other_disconnect_reason_changes_nothing_and_warns_once",
        "test_two_disconnect_reasons_warn_once_each",
        "test_callback_from_a_replaced_stream_is_ignored"]),
    em("method-b-other-reason-ends-a-pause",
       lines("            else:",
             "                self._warn_once(",
             '                    f"disconnect:{reason}",'),
       lines("            else:",
             "                self._set_held(False, self._endpoint_name, METHOD_SESSION_DISCONNECT)",
             "                self._warn_once(",
             '                    f"disconnect:{reason}",'),
       ["test_other_disconnect_reason_does_not_end_a_pause"]),
    em("method-b-accepts-a-notice-from-a-replaced-stream",
       "            if self._stream is None or generation != self._stream_gen:\n",
       "            if self._stream is None:\n",
       ["test_callback_from_a_replaced_stream_is_ignored"]),
    em("method-b-dead-stream-is-kept",
       "            self._release_stream()  # the session is over; a new stream follows\n",
       "            pass  # the session is over; a new stream follows\n",
       ["test_dead_stream_is_replaced_at_the_next_check",
        "test_stream_closed_after_session_death_is_not_closed_twice_on_a_change"]),
    em("method-b-callback-parses-the-reason",
       "        self._notices.put((generation, reason))\n        self._wake.set()\n",
       "        self._notices.put((generation, int(reason)))\n        self._wake.set()\n",
       ["test_callback_only_queues_so_a_bad_reason_cannot_raise_on_the_com_thread"]),
    em("method-b-callback-does-not-wake-the-thread",
       "        self._notices.put((generation, reason))\n        self._wake.set()\n",
       "        self._notices.put((generation, reason))\n        pass\n",
       ["test_reason_5_through_the_thread_publishes_before_the_next_check"]),
    em("method-b-stream-opens-on-another-endpoint",
       "                self._endpoint_id, lambda reason, g=generation: self._notify(g, reason)\n",
       '                "{wrong}", lambda reason, g=generation: self._notify(g, reason)\n',
       ["test_held_stream_is_opened_on_the_default_endpoint_and_never_closed_while_free"]),
    em("method-b-stream-is-opened-every-check",
       lines("        if self._stream is not None:", "            return"),
       lines("        if False:", "            return"),
       ["test_held_stream_is_opened_on_the_default_endpoint_and_never_closed_while_free"]),
    em("method-b-in-use-refusal-warns",
       "            if exc.hresult == E_DEVICE_IN_USE:\n",
       "            if exc.hresult == E_NOTFOUND:\n",
       ["test_held_stream_refused_with_in_use_is_expected_and_silent"]),
    # --- the default speaker changes or goes away
    em("follow-does-not-end-a-pause",
       "        if changed and self._held:\n",
       "        if False:\n",
       ["test_a_pause_the_old_endpoint_caused_ends_on_the_change",
        "test_new_endpoint_that_is_also_held_pauses_again"]),
    em("follow-keeps-the-old-stream",
       lines("        if changed:", "            self._release_stream()"),
       lines("        if changed:", "            pass"),
       ["test_follows_the_new_default_and_closes_the_old_stream"]),
    em("follow-sees-a-change-when-there-is-none",
       "        changed = self._endpoint_id is not None and endpoint_id != self._endpoint_id\n",
       "        changed = self._endpoint_id is not None and endpoint_id == self._endpoint_id\n",
       ["test_held_stream_is_opened_on_the_default_endpoint_and_never_closed_while_free",
        "test_follows_the_new_default_and_closes_the_old_stream"]),
    em("follow-never-adopts-the-new-endpoint",
       lines("        self._endpoint_id = endpoint_id", "        self._endpoint_name = name"),
       lines("        pass", "        self._endpoint_name = name"),
       ["test_checks_the_default_endpoint",
        "test_checks_the_new_endpoint_after_a_change",
        "test_held_stream_is_opened_on_the_default_endpoint_and_never_closed_while_free"]),
    em("drop-does-not-end-a-pause",
       "        if self._held:\n",
       "        if False:\n",
       ["test_no_render_endpoint_ends_a_pause_the_old_speaker_caused"]),
    em("no-endpoint-does-not-drop",
       "            self._drop_endpoint()\n",
       "            pass\n",
       ["test_no_render_endpoint_ends_a_pause_the_old_speaker_caused"]),
    # --- publish on change only
    em("publishes-a-repeat-state",
       "        if held == self._held or self._stop.is_set():\n",
       "        if self._stop.is_set():\n",
       ["test_publishes_only_on_a_change_of_state",
        "test_free_answer_from_the_start_publishes_nothing",
        "test_reason_5_while_already_held_by_method_a_publishes_nothing_more"]),
    em("publishes-after-stop",
       "        if held == self._held or self._stop.is_set():\n",
       "        if held == self._held:\n",
       ["test_nothing_is_published_after_stop"]),
    em("closed-loop-error-is-not-contained",
       lines("            self._loop.call_soon_threadsafe(self._publish_on_loop, event)",
             "        except RuntimeError:"),
       lines("            self._loop.call_soon_threadsafe(self._publish_on_loop, event)",
             "        except KeyError:"),
       ["test_closed_loop_does_not_raise_from_the_thread"]),
    # --- the first answer (wh-kodi-playback-echo-stutter.1.1)
    em("first-answer-closed-loop-error-is-not-contained",
       lines("            self._loop.call_soon_threadsafe(self._first_answer.set)",
             "        except RuntimeError:"),
       lines("            self._loop.call_soon_threadsafe(self._first_answer.set)",
             "        except KeyError:"),
       ["test_closed_loop_through_the_thread_leaves_no_thread_exception"]),
    em("first-answer-is-never-signalled",
       "            self._loop.call_soon_threadsafe(self._first_answer.set)\n",
       "            pass\n",
       ["test_the_answer_is_ready_only_after_a_subscriber_got_the_held_event",
        "test_the_answer_is_ready_after_the_first_poll_when_the_speaker_is_free",
        "test_the_answer_is_ready_after_a_backend_start_failure"]),
    em("first-answer-is-ready-before-the-poll",
       lines("                    self._guarded(self._poll_once)",
             "                    self._signal_first_answer()"),
       lines("                    self._signal_first_answer()",
             "                    self._guarded(self._poll_once)"),
       ["test_the_answer_is_ready_only_after_a_subscriber_got_the_held_event"]),
    em("first-answer-is-ready-before-the-event-is-delivered",
       "                await asyncio.wait(pending)\n",
       "                pass\n",
       ["test_the_answer_is_ready_only_after_a_subscriber_got_the_held_event"]),
    em("first-answer-is-not-signalled-on-a-failed-start",
       lines("            # Nothing will ever answer; startup must not wait for it.",
             "            self._signal_first_answer()"),
       lines("            # Nothing will ever answer; startup must not wait for it.",
             "            pass"),
       ["test_the_answer_is_ready_after_a_backend_start_failure"]),
    em("first-answer-timeout-has-no-warning",
       lines("            logger.warning(",
             '                "[EXCLUSIVE SPEAKER] The exclusive-mode detector gave no answer "'),
       lines("            logger.info(",
             '                "[EXCLUSIVE SPEAKER] The exclusive-mode detector gave no answer "'),
       ["test_a_backend_that_never_answers_times_out_with_one_warning"]),
    em("first-answer-timeout-is-longer-than-two-seconds",
       "FIRST_ANSWER_TIMEOUT_SECONDS = 2.0\n",
       "FIRST_ANSWER_TIMEOUT_SECONDS = 30.0\n",
       ["test_the_wait_is_bounded_by_two_seconds"]),
    em("first-answer-timeout-argument-is-ignored",
       "            await asyncio.wait_for(_ready(), timeout)\n",
       "            await asyncio.wait_for(_ready(), timeout * 100)\n",
       ["test_a_backend_that_never_answers_times_out_with_one_warning"]),
    em("first-answer-wait-blocks-the-loop",
       "            await asyncio.wait_for(_ready(), timeout)\n",
       "            time.sleep(timeout)\n            raise asyncio.TimeoutError\n",
       ["test_a_backend_that_never_answers_times_out_with_one_warning"]),
    # --- warn once
    em("warns-more-than-once",
       lines("        if kind in self._warned:", "            return"),
       lines("        if kind in self._warned:", "            pass"),
       ["test_other_hresult_changes_nothing_and_warns_once",
        "test_com_exception_changes_nothing_and_warns_once",
        "test_no_render_endpoint_changes_nothing_and_warns_once",
        "test_endpoint_query_failure_changes_nothing_and_warns_once",
        "test_other_disconnect_reason_changes_nothing_and_warns_once",
        "test_held_stream_refused_for_another_reason_warns_once",
        "test_held_stream_com_exception_warns_once"]),
    em("warn-is-not-recorded",
       "        self._warned.add(kind)\n",
       "        pass\n",
       ["test_other_hresult_changes_nothing_and_warns_once",
        "test_com_exception_changes_nothing_and_warns_once",
        "test_no_render_endpoint_changes_nothing_and_warns_once"]),
    em("one-warning-for-every-kind",
       "        if kind in self._warned:\n",
       "        if self._warned:\n",
       ["test_two_kinds_of_hresult_warn_once_each",
        "test_two_disconnect_reasons_warn_once_each"]),
    em("start-failure-is-not-final",
       lines("            return",
             '        logger.info("[EXCLUSIVE SPEAKER] Exclusive-mode detector started")'),
       lines("            pass",
             '        logger.info("[EXCLUSIVE SPEAKER] Exclusive-mode detector started")'),
       ["test_backend_that_fails_to_start_changes_nothing_and_warns_once"]),
    # --- threads
    em("thread-is-not-a-daemon",
       "name=THREAD_NAME, daemon=True)",
       "name=THREAD_NAME, daemon=False)",
       ["test_work_runs_on_one_named_daemon_thread",
        "test_thread_is_a_daemon_thread"]),
    em("stop-waits-for-the-thread",
       lines("        self._stop.set()", "        self._wake.set()"),
       lines("        self._stop.set()", "        self._wake.set()",
             "        if self._thread is not None:",
             "            self._thread.join(timeout=2.0)"),
       ["test_stop_returns_at_once_while_the_backend_blocks"]),
    em("stop-does-not-wake-the-thread",
       lines("        self._stop.set()", "        self._wake.set()"),
       lines("        self._stop.set()", "        pass"),
       ["test_stop_wakes_a_thread_that_is_waiting_between_checks"]),
    em("start-twice-runs-two-threads",
       lines("        if self._thread is not None and self._thread.is_alive():",
             "            return"),
       lines("        if False:", "            return"),
       ["test_start_twice_runs_one_thread"]),
    em("backend-is-never-shut-down",
       "                self._guarded(self._backend.shutdown)\n",
       "                pass\n",
       ["test_every_backend_call_runs_off_the_calling_thread_on_one_thread",
        "test_stop_wakes_a_thread_that_is_waiting_between_checks"]),
    em("backend-is-never-initialized",
       "            self._backend.initialize()\n",
       "            pass\n",
       ["test_every_backend_call_runs_off_the_calling_thread_on_one_thread",
        "test_start_twice_runs_one_thread"]),
    # --- when method A runs (boss ruling 00:20)
    em("method-a-runs-every-poll-while-free-and-ready",
       "        if self._held or self._stream is None or now >= self._next_backup_check:\n",
       "        if True:\n",
       ["test_while_free_with_a_ready_stream_a_poll_skips_method_a_until_the_backup_time",
        "test_the_backup_check_catches_a_start_that_method_b_missed",
        "test_thread_polls_the_endpoint_once_per_interval_while_method_a_is_idle"]),
    em("method-a-skipped-while-held",
       "        if self._held or self._stream is None or now >= self._next_backup_check:\n",
       "        if self._stream is None or now >= self._next_backup_check:\n",
       ["test_while_held_every_poll_runs_method_a_and_s_ok_ends_the_pause"]),
    # 5.0 plus one poll interval allows a start up to 5.5 s late.
    em("backup-time-allows-a-start-later-than-five-seconds",
       "BACKUP_CHECK_SECONDS = 4.5\n",
       "BACKUP_CHECK_SECONDS = 5.0\n",
       ["test_backup_constants_are_at_most_five_seconds",
        "test_the_backup_check_catches_a_start_that_method_b_missed"]),
    em("missed-start-keeps-the-old-stream",
       "            # report the next one; release it so a fresh one opens when free.\n"
       "            self._release_stream()\n",
       "            # report the next one; release it so a fresh one opens when free.\n"
       "            pass\n",
       ["test_after_a_start_that_method_b_missed_a_fresh_stream_is_held"]),
    # --- the poll interval
    em("poll-interval-above-half-a-second",
       "POLL_INTERVAL_SECONDS = 0.5\n",
       "POLL_INTERVAL_SECONDS = 1.5\n",
       ["test_default_interval_is_at_most_one_second"]),
    em("poll-interval-is-ignored",
       "                    next_poll = time.monotonic() + self.poll_interval\n",
       "                    next_poll = time.monotonic() + self.poll_interval * 0.001\n",
       ["test_thread_does_not_check_faster_than_the_interval"]),
    # --- source rules
    em("initialize-in-exclusive-mode",
       "    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)\n",
       "    client.Initialize(AUDCLNT_SHAREMODE_EXCLUSIVE, 0, _BUFFER_DURATION, 0, buffer, None)\n",
       ["test_no_exclusive_share_mode_anywhere",
        "test_every_initialize_is_shared_mode"]),
    em("client-is-started",
       "    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)\n",
       "    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)\n"
       "    client.Start()\n",
       ["test_audio_client_is_never_started"]),
    em("buffer-is-written",
       "    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)\n",
       "    client.Initialize(AUDCLNT_SHAREMODE_SHARED, 0, _BUFFER_DURATION, 0, buffer, None)\n"
       "    client.GetBuffer(0)\n",
       ["test_nothing_is_ever_written_to_the_speaker"]),

    # ----------------------------------------------------------- service_manager
    svm("monitor-is-not-built",
        "        self.exclusive_speaker_monitor = ExclusiveSpeakerMonitor(self.loop, self.event_bus)\n",
        "        self.exclusive_speaker_monitor = None\n",
        ["test_initialize_services_builds_the_monitor"]),
    svm("monitor-is-not-started",
        "            self.exclusive_speaker_monitor.start()\n",
        "            pass\n",
        ["test_start_services_starts_it_without_a_task_to_wait_on",
         "test_start_services_starts_it_whatever_the_suppression_setting_says"]),
    svm("monitor-start-waits-on-a-task",
        "            self.exclusive_speaker_monitor.start()\n",
        "            tasks.append(self.exclusive_speaker_monitor.start())\n",
        ["test_start_services_starts_it_without_a_task_to_wait_on"]),
    svm("monitor-start-needs-the-setting",
        lines("        if self.exclusive_speaker_monitor:",
              "            # No task is appended: the detector runs on a daemon thread, and"),
        lines('        if self.exclusive_speaker_monitor and self.config_service.get("ENABLE_AUDIO_SUPPRESSION", True):',
              "            # No task is appended: the detector runs on a daemon thread, and"),
        ["test_start_services_starts_it_whatever_the_suppression_setting_says"]),
    svm("monitor-is-not-stopped",
        "                self.exclusive_speaker_monitor.stop()\n",
        "                pass\n",
        ["test_shutdown_services_stops_it"]),
    svm("monitor-stop-failure-is-not-contained",
        lines("            except Exception as e:",
              '                log.error(f"Error stopping ExclusiveSpeakerMonitor: {e}", exc_info=True)'),
        lines("            except KeyError as e:",
              '                log.error(f"Error stopping ExclusiveSpeakerMonitor: {e}", exc_info=True)'),
        ["test_shutdown_continues_when_the_monitor_stop_raises"]),

    # ------------------------------------------------------------------- main.py
    main("gate-is-closed-unless-the-setting-is-true",
         "                is not False\n",
         "                is True\n",
         ["test_no_setting_gives_an_open_gate_even_when_the_decision_is_off",
          "test_auto_gives_an_open_gate"]),
    main("gate-follows-the-startup-decision",
         _GATE_CALL,
         lines("            self.state_manager.apply_exclusive_speaker_pause_gate(",
               "                await decide_audio_suppression(",
               "                    self.config_service.get(",
               "                        AUDIO_SUPPRESSION_SETTING, AUDIO_SUPPRESSION_DEFAULT",
               "                    )",
               "                )",
               "            )"),
         ["test_no_setting_gives_an_open_gate_even_when_the_decision_is_off",
          "test_auto_gives_an_open_gate"]),
    main("gate-is-never-set",
         _GATE_CALL,
         "            pass\n",
         ["test_no_setting_gives_an_open_gate_even_when_the_decision_is_off",
          "test_auto_gives_an_open_gate",
          "test_true_gives_an_open_gate",
          "test_false_gives_a_closed_gate",
          "test_the_gate_is_set_before_services_start"]),
    main("gate-is-set-after-services-start",
         _span(_GATE_CALL, _SERVICES_START),
         _span(_GATE_CALL, _SERVICES_START).replace(_GATE_CALL, "", 1)
         + _GATE_CALL,
         ["test_the_gate_is_set_before_services_start"]),
    # --- the order before the STT provider starts (wh-kodi-playback-echo-stutter.1.1)
    main("gate-is-set-after-the-provider-starts",
         _span(_GATE_CALL, _PROVIDER_START),
         _span(_GATE_CALL, _PROVIDER_START).replace(_GATE_CALL, "", 1)
         + _indent(_GATE_CALL),
         # Only the order test sees it: the gate's stored default is open, so
         # the held-speaker test reports the pause with the gate set late too.
         ["test_an_open_gate_orders_gate_start_wait_provider_decision_services"]),
    main("detector-is-not-started-before-the-provider",
         "                exclusive_speaker_monitor.start()\n",
         "                pass\n",
         ["test_an_app_holding_the_speaker_is_known_when_the_provider_starts",
          "test_a_closed_gate_starts_the_detector_and_does_not_wait",
          "test_an_open_gate_orders_gate_start_wait_provider_decision_services"]),
    main("detector-start-needs-the-gate",
         "            if exclusive_speaker_monitor:\n",
         "            if exclusive_speaker_monitor and exclusive_speaker_pause_on:\n",
         ["test_a_closed_gate_starts_the_detector_and_does_not_wait"]),
    main("detector-is-started-after-the-provider",
         _span(_MONITOR_BLOCK, _PROVIDER_START),
         _span(_MONITOR_BLOCK, _PROVIDER_START).replace(_MONITOR_BLOCK, "", 1)
         + _indent(_MONITOR_BLOCK),
         ["test_an_app_holding_the_speaker_is_known_when_the_provider_starts",
          "test_an_open_gate_orders_gate_start_wait_provider_decision_services",
          "test_the_detector_still_starts_when_there_is_no_provider_launcher"]),
    main("detector-wait-is-removed",
         _WAIT_BLOCK,
         "                pass\n",
         ["test_an_app_holding_the_speaker_is_known_when_the_provider_starts",
          "test_an_open_gate_orders_gate_start_wait_provider_decision_services",
          "test_the_detector_still_starts_when_there_is_no_provider_launcher"]),
    main("detector-wait-is-after-the-provider-starts",
         _span(_WAIT_BLOCK, _PROVIDER_START),
         _span(_WAIT_BLOCK, _PROVIDER_START).replace(_WAIT_BLOCK, "                pass\n", 1)
         + _WAIT_BLOCK,
         ["test_an_app_holding_the_speaker_is_known_when_the_provider_starts",
          "test_an_open_gate_orders_gate_start_wait_provider_decision_services"]),
    main("detector-waits-although-the-gate-is-off",
         _WAIT_BLOCK,
         lines("                if True:",
               "                    await exclusive_speaker_monitor.wait_first_answer()"),
         ["test_a_closed_gate_starts_the_detector_and_does_not_wait"]),
]

if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
