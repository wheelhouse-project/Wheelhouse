"""Mutation gate for hiding the floating button during the exclusive-speaker
pause (wh-kodi-pause-hide-button).

WHAT THE CHANGE DOES. While an app holds the default speaker in exclusive mode
the Logic process (StateManager) pauses listening at once, and after
EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS it tells the GUI process to hide the
floating button through the snapshot key ``button_hidden_for_exclusive_speaker``.
The button comes back after the pause has stayed ended for the same wait. A
user write that turns FLOATING_BUTTON_VISIBLE on during the pause wins at
once. The GUI (gui.py) hides the button while the key is True and logs one
INFO line for each change.

WHAT IT COVERS (names are the mutation names below):

  sm-*   state_manager.py: the two timers (armed, cancelled, guarded), the
         wait constant, the two timer callbacks, the snapshot key, the setting
         gate, the user-write override and the three paths that call it, and
         the shutdown cancel.
  gui-*  gui.py: the visibility rule, the missing-key default, the stored
         value and the two INFO lines.

This gate reuses the runner set up by mutation_gate_exclusive_speaker.py
(worktree-safe pytest launch, FAILED-line log through MUTATION_GATE_REASONS,
exactly-one-match, compile check, per-run timeout, byte IO, expected-name
validation, green baseline, COLUMNS=1000, short-summary verdicts). Run from
services/wheelhouse with the interpreter of the worktree venv:

    .venv/Scripts/python.exe tests/mutation_gate_exclusive_pause_button.py --check
    .venv/Scripts/python.exe tests/mutation_gate_exclusive_pause_button.py

Set MUTATION_GATE_REASONS to a file path to append the FAILED lines with
their assertion text, so a reader can confirm that the expected assertion
fired and not an unrelated crash upstream of it.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_gate_exclusive_speaker as base  # noqa: E402

runner = base.runner
lines = base.lines
SERVICE = base.SERVICE
STATE_MANAGER = base.STATE_MANAGER
GUI = SERVICE / "gui.py"

LOGIC_TESTS = "tests/test_exclusive_pause_button.py"
GUI_TESTS = "tests/test_floating_button_pause_hide_gui.py"


def sm(name, old, new, expect):
    return dict(name="sm-" + name, service=SERVICE, test_file=LOGIC_TESTS,
                file=STATE_MANAGER, old=old, new=new, expect=expect)


def gui(name, old, new, expect):
    return dict(name="gui-" + name, service=SERVICE, test_file=GUI_TESTS,
                file=GUI, old=old, new=new, expect=expect)


# ------------------------------------------------------------------ state_manager

HIDE_ARM = lines(
    "                self._button_hide_timer = self.loop.call_later(",
    "                    EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS,",
    "                    self._finish_button_hide_change,",
    "                    True,",
    "                )",
)
SHOW_ARM = lines(
    "                self._button_show_timer = self.loop.call_later(",
    "                    EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS,",
    "                    self._finish_button_hide_change,",
    "                    False,",
    "                )",
)
HELD_BRANCH_HEAD = lines(
    "        if held:",
    '            self._cancel_button_timer("_button_show_timer")',
    "            if not self._button_hidden_for_exclusive_speaker:",
)
HIDE_GUARD = lines(
    "            if not self._button_hidden_for_exclusive_speaker:",
    '                self._cancel_button_timer("_button_hide_timer")',
)
ELSE_BRANCH_HEAD = lines(
    "        else:",
    '            self._cancel_button_timer("_button_hide_timer")',
    "            if self._button_hidden_for_exclusive_speaker:",
)
SHOW_GUARD = lines(
    "            if self._button_hidden_for_exclusive_speaker:",
    '                self._cancel_button_timer("_button_show_timer")',
)
FINISH_SET = "        self._button_hidden_for_exclusive_speaker = hidden\n"
FINISH_SEND = lines(
    "        self.send_state_update()",
    "",
    "    def _cancel_button_timer",
).rstrip(chr(10))
SNAPSHOT_KEY = (
    "                'button_hidden_for_exclusive_speaker': "
    "self._button_hidden_for_exclusive_speaker,\n"
)
GATE_CLOSED_RETURN = lines(
    "            return",
    "        new_speech_enabled = self.speech_enabled",
)
OVERRIDE_CANCELS = lines(
    '        self._cancel_button_timer("_button_hide_timer")',
    '        self._cancel_button_timer("_button_show_timer")',
    "        self._button_hidden_for_exclusive_speaker = False",
    "        return True",
)
OVERRIDE_WAITING = lines(
    "            self._button_hidden_for_exclusive_speaker",
    "            or self._button_hide_timer is not None",
    "            or self._button_show_timer is not None",
)
WAS_FALSE_CHECK = lines(
    "        if self.config_service.get('FLOATING_BUTTON_VISIBLE', True):",
    "            return False",
)
SET_VALUE_HEAD = lines(
    "        cleared = self._clear_button_hide_for_user_write({key: value})",
    "        if request_id is not None:",
    "            if cleared:",
    "                self.send_state_update()",
    "            return await self._save_gui_settings({key: value}, request_id)",
    '        logger.debug(f"Updating config: \'{key}\' = {value}")',
    "        self.config_service.set(key, value)",
    "        if cleared:",
    "            self.send_state_update()",
    "        saved = await self.config_service.save()",
)
SET_VALUES_HEAD = lines(
    "        cleared = self._clear_button_hide_for_user_write(values)",
    "        if request_id is not None:",
    "            if cleared:",
    "                self.send_state_update()",
    "            return await self._save_gui_settings(values, request_id)",
)
SET_VALUES_LEGACY = lines(
    "            self.config_service.set(key, value)",
    "        if cleared:",
    "            self.send_state_update()",
    "        saved = await self.config_service.save()",
)
STAGED_SEND_VALUE = lines(
    "            if cleared:",
    "                self.send_state_update()",
    "            return await self._save_gui_settings({key: value}, request_id)",
)
STAGED_SEND_VALUES = lines(
    "            if cleared:",
    "                self.send_state_update()",
    "            return await self._save_gui_settings(values, request_id)",
)
LEGACY_SEND_VALUE = lines(
    "        self.config_service.set(key, value)",
    "        if cleared:",
    "            self.send_state_update()",
    "        saved = await self.config_service.save()",
    "        if not saved:",
    "            logger.error(f\"Config '{key}' changed in memory but was NOT saved to disk\")",
)
SHUTDOWN_HIDE = lines(
    "        # two timers that hide and show the floating button.",
    '        self._cancel_button_timer("_button_hide_timer")',
)
SHUTDOWN_SHOW = lines(
    '        self._cancel_button_timer("_button_show_timer")',
    "",
    "    def set_remote_stt_launcher",
).rstrip(chr(10))

VISIBLE_RULE = lines(
    "            bool(self.button_visible and not self.button_hidden_for_exclusive_speaker)",
)

START_TESTS = [
    "test_a_pause_that_lasts_the_wait_sets_the_key",
    "test_an_end_that_lasts_the_wait_clears_the_key",
]

MUTATIONS = [
    # --- the timers on a pause start
    sm("hide-timer-is-not-armed-on-a-pause-start",
       HIDE_ARM,
       "                self._button_hide_timer = None\n",
       ["test_a_pause_that_lasts_the_wait_sets_the_key"]),
    sm("show-timer-is-not-cancelled-on-a-pause-start",
       HELD_BRANCH_HEAD,
       HELD_BRANCH_HEAD.replace(
           '            self._cancel_button_timer("_button_show_timer")\n',
           "            pass\n"),
       ["test_a_gap_shorter_than_the_wait_keeps_the_key_true"]),
    sm("hide-is-re-armed-when-already-hidden",
       HIDE_GUARD,
       HIDE_GUARD.replace("if not self._button_hidden_for_exclusive_speaker:",
                          "if True:"),
       ["test_a_gap_shorter_than_the_wait_keeps_the_key_true"]),
    # --- the timers on a pause end
    sm("show-timer-is-not-armed-on-a-pause-end",
       SHOW_ARM,
       "                self._button_show_timer = None\n",
       ["test_an_end_that_lasts_the_wait_clears_the_key"]),
    sm("hide-timer-is-not-cancelled-on-a-pause-end",
       ELSE_BRANCH_HEAD,
       ELSE_BRANCH_HEAD.replace(
           '            self._cancel_button_timer("_button_hide_timer")\n',
           "            pass\n"),
       ["test_a_pause_shorter_than_the_wait_never_sets_the_key"]),
    sm("show-is-armed-when-not-hidden",
       SHOW_GUARD,
       SHOW_GUARD.replace("if self._button_hidden_for_exclusive_speaker:",
                          "if True:"),
       ["test_a_pause_shorter_than_the_wait_never_sets_the_key"]),
    sm("pause-start-and-end-are-swapped",
       "        self._schedule_button_hide_change(held)\n",
       "        self._schedule_button_hide_change(not held)\n",
       START_TESTS),
    sm("pause-never-schedules-the-button",
       "        self._schedule_button_hide_change(held)\n",
       "        pass\n",
       START_TESTS),
    # --- the wait
    sm("wait-is-zero",
       "EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS = 1.0\n",
       "EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS = 0.0\n",
       ["test_the_wait_is_one_second"]),
    sm("wait-is-three-seconds",
       "EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS = 1.0\n",
       "EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS = 3.0\n",
       ["test_the_wait_is_one_second"]),
    sm("hide-timer-ignores-the-constant",
       HIDE_ARM,
       HIDE_ARM.replace("EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS", "0.0"),
       ["test_a_pause_that_lasts_the_wait_sets_the_key"]),
    sm("show-timer-ignores-the-constant",
       SHOW_ARM,
       SHOW_ARM.replace("EXCLUSIVE_PAUSE_BUTTON_DELAY_SECONDS", "0.0"),
       ["test_an_end_that_lasts_the_wait_clears_the_key"]),
    # --- the two timer callbacks
    sm("hide-callback-does-not-set-the-attribute",
       FINISH_SET,
       "        self._button_hidden_for_exclusive_speaker = hidden and False\n",
       ["test_a_pause_that_lasts_the_wait_sets_the_key"]),
    sm("show-callback-does-not-clear-the-attribute",
       FINISH_SET,
       "        self._button_hidden_for_exclusive_speaker = (\n"
       "            hidden or self._button_hidden_for_exclusive_speaker\n"
       "        )\n",
       ["test_an_end_that_lasts_the_wait_clears_the_key"]),
    sm("hide-callback-sends-no-snapshot",
       FINISH_SEND,
       FINISH_SEND.replace("        self.send_state_update()\n",
                           "        if not hidden:\n"
                           "            self.send_state_update()\n", 1),
       ["test_a_pause_that_lasts_the_wait_sets_the_key"]),
    sm("show-callback-sends-no-snapshot",
       FINISH_SEND,
       FINISH_SEND.replace("        self.send_state_update()\n",
                           "        if hidden:\n"
                           "            self.send_state_update()\n", 1),
       ["test_an_end_that_lasts_the_wait_clears_the_key"]),
    # --- the snapshot key
    sm("snapshot-omits-the-key",
       SNAPSHOT_KEY,
       "",
       ["test_the_key_is_false_when_nothing_is_paused",
        "test_a_pause_that_lasts_the_wait_sets_the_key"]),
    sm("snapshot-sends-a-constant-false",
       SNAPSHOT_KEY,
       "                'button_hidden_for_exclusive_speaker': False,\n",
       ["test_a_pause_that_lasts_the_wait_sets_the_key",
        "test_the_answer_to_request_initial_state_carries_true_at_once"]),
    sm("snapshot-sends-a-constant-true",
       SNAPSHOT_KEY,
       "                'button_hidden_for_exclusive_speaker': True,\n",
       ["test_the_key_is_false_when_nothing_is_paused"]),
    # --- the setting gate
    sm("closed-gate-return-also-arms-the-timers",
       GATE_CLOSED_RETURN,
       lines("            self._schedule_button_hide_change(held)",
             "            return",
             "        new_speech_enabled = self.speech_enabled"),
       ["test_a_closed_gate_never_hides"]),
    sm("closed-gate-return-is-skipped",
       GATE_CLOSED_RETURN,
       GATE_CLOSED_RETURN.replace("            return\n", "            pass\n", 1),
       ["test_a_closed_gate_never_hides"]),
    # --- the user-write override
    sm("override-is-never-set-by-a-write-of-true-over-false",
       OVERRIDE_CANCELS,
       OVERRIDE_CANCELS.replace("        return True\n", "        return False\n"),
       # Only this test pins the snapshot sent before the save; the attribute
       # is cleared either way, so the later tests still pass.
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("override-drops-the-was-false-check",
       WAS_FALSE_CHECK,
       WAS_FALSE_CHECK.replace(
           "if self.config_service.get('FLOATING_BUTTON_VISIBLE', True):",
           "if False:"),
       # A write of false is already refused by the first check
       # (values.get), so only true over true reaches this one.
       ["test_a_true_over_true_write_does_not_clear_the_key"]),
    sm("override-reads-the-value-after-the-write",
       SET_VALUE_HEAD,
       lines(
           "        if request_id is not None:",
           "            cleared = self._clear_button_hide_for_user_write({key: value})",
           "            if cleared:",
           "                self.send_state_update()",
           "            return await self._save_gui_settings({key: value}, request_id)",
           '        logger.debug(f"Updating config: \'{key}\' = {value}")',
           "        self.config_service.set(key, value)",
           "        cleared = self._clear_button_hide_for_user_write({key: value})",
           "        if cleared:",
           "            self.send_state_update()",
           "        saved = await self.config_service.save()"),
       ["test_the_button_stays_shown_after_the_pause_ends",
        "test_turning_it_on_while_the_hide_is_waiting_cancels_the_hide"]),
    sm("override-does-not-cancel-the-hide-timer",
       OVERRIDE_CANCELS,
       OVERRIDE_CANCELS.replace(
           '        self._cancel_button_timer("_button_hide_timer")\n', "        pass\n"),
       ["test_turning_it_on_while_the_hide_is_waiting_cancels_the_hide"]),
    sm("override-does-not-cancel-the-show-timer",
       OVERRIDE_CANCELS,
       OVERRIDE_CANCELS.replace(
           '        self._cancel_button_timer("_button_show_timer")\n', "        pass\n"),
       ["test_turning_it_on_while_the_show_is_waiting_cancels_the_show"]),
    sm("override-does-not-clear-the-hidden-attribute",
       OVERRIDE_CANCELS,
       OVERRIDE_CANCELS.replace(
           "        self._button_hidden_for_exclusive_speaker = False\n", "        pass\n"),
       ["test_turning_the_button_on_shows_it_at_once",
        "test_the_button_stays_shown_after_the_pause_ends"]),
    sm("override-ignores-a-waiting-hide",
       OVERRIDE_WAITING,
       OVERRIDE_WAITING.replace(
           "            or self._button_hide_timer is not None\n", ""),
       ["test_turning_it_on_while_the_hide_is_waiting_cancels_the_hide"]),
    # No "override-ignores-a-waiting-show" mutation: a show timer is armed only
    # while the key is True and is cancelled before the key goes False, so the
    # key test already covers every case the timer test could add. Removing the
    # timer term changes no behavior (an equivalent mutant, measured: it
    # survived with zero failing tests).
    sm("override-sends-no-snapshot-before-the-staged-save",
       STAGED_SEND_VALUE,
       STAGED_SEND_VALUE.replace("self.send_state_update()", "pass"),
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("override-sends-the-snapshot-after-the-staged-save",
       STAGED_SEND_VALUE,
       lines("            saved_result = await self._save_gui_settings({key: value}, request_id)",
             "            if cleared:",
             "                self.send_state_update()",
             "            return saved_result"),
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("override-sends-no-snapshot-before-the-legacy-save",
       LEGACY_SEND_VALUE,
       LEGACY_SEND_VALUE.replace("            self.send_state_update()", "            pass", 1),
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("override-sends-the-snapshot-after-the-legacy-save",
       LEGACY_SEND_VALUE,
       lines("        self.config_service.set(key, value)",
             "        saved = await self.config_service.save()",
             "        if cleared:",
             "            self.send_state_update()",
             "        if not saved:",
             "            logger.error(f\"Config '{key}' changed in memory but was NOT saved to disk\")"),
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("group-staged-sends-no-snapshot-before-the-save",
       STAGED_SEND_VALUES,
       STAGED_SEND_VALUES.replace("self.send_state_update()", "pass"),
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("group-legacy-sends-no-snapshot-before-the-save",
       SET_VALUES_LEGACY,
       SET_VALUES_LEGACY.replace("            self.send_state_update()", "            pass", 1),
       ["test_turning_the_button_on_shows_it_at_once"]),
    # --- the two writers that call the override
    sm("set-config-values-does-not-call-the-override",
       "        cleared = self._clear_button_hide_for_user_write(values)\n",
       "        cleared = False\n",
       ["test_turning_the_button_on_shows_it_at_once"]),
    sm("set-config-value-does-not-call-the-override",
       "        cleared = self._clear_button_hide_for_user_write({key: value})\n",
       "        cleared = False\n",
       ["test_turning_the_button_on_shows_it_at_once",
        "test_the_button_stays_shown_after_the_pause_ends"]),
    # --- shutdown
    sm("shutdown-does-not-cancel-the-hide-timer",
       SHUTDOWN_HIDE,
       SHUTDOWN_HIDE.replace(
           '        self._cancel_button_timer("_button_hide_timer")\n', "        pass\n"),
       ["test_shutdown_cancels_a_waiting_hide"]),
    sm("shutdown-does-not-cancel-the-show-timer",
       SHUTDOWN_SHOW,
       SHUTDOWN_SHOW.replace(
           '        self._cancel_button_timer("_button_show_timer")\n', "        pass\n"),
       ["test_shutdown_cancels_a_waiting_show"]),

    # ------------------------------------------------------------------------ gui
    gui("set-visible-ignores-the-hidden-value",
        VISIBLE_RULE,
        lines("            bool(self.button_visible)"),
        ["test_the_button_shows_only_when_the_setting_is_on_and_no_pause_hides_it",
         "test_hiding_for_the_pause_cancels_a_press_in_flight"]),
    gui("set-visible-ignores-the-saved-setting",
        VISIBLE_RULE,
        lines("            bool(not self.button_hidden_for_exclusive_speaker)"),
        ["test_the_button_shows_only_when_the_setting_is_on_and_no_pause_hides_it",
         "test_a_setting_that_is_off_keeps_the_button_hidden_after_the_pause"]),
    gui("missing-key-defaults-to-hidden",
        "message.get('button_hidden_for_exclusive_speaker', False)",
        "message.get('button_hidden_for_exclusive_speaker', True)",
        ["test_a_message_without_the_key_means_not_hidden"]),
    gui("the-key-is-not-applied",
        lines("                    self._set_button_hidden_for_exclusive_speaker(",
              "                        bool(message.get('button_hidden_for_exclusive_speaker', False))",
              "                    )"),
        "                    pass\n",
        ["test_a_state_update_with_the_key_hides_a_button_whose_setting_is_on",
         "test_the_first_message_hides_the_button"]),
    gui("the-hidden-value-is-not-stored",
        "        self.button_hidden_for_exclusive_speaker = hidden\n",
        "        pass\n",
        ["test_a_state_update_with_the_key_hides_a_button_whose_setting_is_on",
         "test_the_first_message_hides_the_button"]),
    gui("the-value-starts-hidden",
        "        self.button_hidden_for_exclusive_speaker = False\n        self.show_speech_pulse",
        "        self.button_hidden_for_exclusive_speaker = True\n        self.show_speech_pulse",
        ["test_a_state_that_never_hides_logs_nothing"]),
    gui("hide-line-is-not-logged-at-info",
        lines("            logger.info(",
              '                "floating button hidden while a program has sole control of the speaker"'),
        lines("            logger.debug(",
              '                "floating button hidden while a program has sole control of the speaker"'),
        ["test_one_line_when_the_button_hides_and_none_on_a_repeat",
         "test_the_first_message_logs_the_hide"]),
    gui("come-back-line-is-not-logged-at-info",
        lines("            logger.info(",
              '                "floating button no longer hidden for the exclusive-speaker pause"'),
        lines("            logger.debug(",
              '                "floating button no longer hidden for the exclusive-speaker pause"'),
        ["test_one_line_when_the_button_comes_back_and_none_on_a_repeat"]),
    gui("a-repeat-logs-again",
        lines("        if hidden == self.button_hidden_for_exclusive_speaker:",
              "            return"),
        lines("        if False:", "            return"),
        ["test_one_line_when_the_button_hides_and_none_on_a_repeat",
         "test_one_line_when_the_button_comes_back_and_none_on_a_repeat",
         "test_a_state_that_never_hides_logs_nothing"]),
]
if __name__ == "__main__":
    raise SystemExit(runner.run(MUTATIONS))
