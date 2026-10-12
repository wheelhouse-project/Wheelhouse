"""Mutation gate for the settings window and its voice command
(wh-voice-settings-window).

Proves the guard tests added by commits 994e74c1 (stage A: INTERIM_RESULTS
saved and read, LOG_TRANSCRIPTS read in each start cycle) and 3326662c
(stage B: the settings window, the "settings" voice command, the "Settings"
item on both right-click menus) fail for the right reason when the protected
behaviour breaks. Run from services/wheelhouse with the service venv:

    .venv/Scripts/python.exe tests/mutation_gate_voice_settings_window.py
    .venv/Scripts/python.exe tests/mutation_gate_voice_settings_window.py --check
    .venv/Scripts/python.exe tests/mutation_gate_voice_settings_window.py \\
        --only name-one,name-two

Runner structure copied from tests/mutation_gate_log_rotation.py (recovery
journal, durable restore, compile check, per-mutation timeout, suite-timeout
detection, expected-name validation, a reason substring per expected catcher)
and tests/mutation_gate_floating_button_offscreen.py (--only). Additions:

- A mutation may carry "edits": a list of (old, new) pairs inside one file,
  each pair needing exactly one match. The launcher "move" mutation uses it:
  the call is DELETED from the loop and INSERTED before the loop, never
  copied (a copy would leave the original in place and read as a survivor).
- Expected catcher names match by trailing node-id components, so a name
  that two test files share (test_missing_key_keeps_interim_results_on) can
  be told apart by a leading file or class component.
- A TOML mutant is checked with tomllib instead of compile().

Pattern-not-found, ambiguous patterns, non-compiling mutants, timeouts,
suite-timeout aborts, collection or fixture errors, and wrong-reason
failures are ERRORS, never verdicts. A green run is a survivor; a nonzero
exit is read only from pytest's short-summary section, with COLUMNS=1000 so
the reason suffix is not cut.

pytest is started through the service venv rather than ``uv run``: uv
re-synchronises the environment before it runs anything.
"""
# --check validates patterns and syntax without collecting tests,
# recovering pending mutations, or writing target files.
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parents[1]

DIALOG = SERVICE / "settings_window_dialog.py"
HELP_DIALOG = SERVICE / "settings_help_dialog.py"
GUI = SERVICE / "gui.py"
ACTIONS = SERVICE / "speech" / "actions.py"
CATALOG = SERVICE / "speech" / "action_catalog.py"
PATTERNS = SERVICE / "speech" / "config" / "patterns.toml"
STATE_MANAGER = SERVICE / "state_manager.py"
WEBSOCKET_MANAGER = SERVICE / "integrations" / "websocket_manager.py"
MAIN = SERVICE / "main.py"
LAUNCHER = SERVICE / "launcher.py"

_VENV_PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable))

DIALOG_TESTS = ["tests/test_settings_window_dialog.py"]
GUI_TESTS = DIALOG_TESTS + ["tests/test_gui.py::TestSettingsEntryOnBothMenus"]
HELP_TESTS = ["tests/test_settings_help_dialog.py"]
VOICE_TESTS = ["tests/test_settings_window_voice.py"]
STATE_TESTS = [
    "tests/test_state_manager.py::TestInterimResultsReadAtStart",
    "tests/test_toggle_interim_results.py",
]
WEBSOCKET_TESTS = [
    "tests/test_websocket_manager.py::TestInterimResultsReadAtStart",
]
READ_TESTS = STATE_TESTS + WEBSOCKET_TESTS
LAUNCHER_TESTS = ["tests/test_launcher.py::TestTranscriptFlagReadEachCycle"]

ARITHMETIC = "test_next_timeout_value_arithmetic"
RESTART_ROWS = "test_restart_text_sits_beside_exactly_settings_9_to_13"
NON_BUTTON = "test_only_buttons_check_boxes_and_radio_buttons_take_input"
WARNING_ROWS = "test_the_wake_word_and_speech_warning_appears_beside_those_two_only"

# "expect" maps each expected catcher to a substring its failure reason must
# contain. "selection" overrides the default. A mutation has either
# "old"/"new" or "edits".
MUTATIONS = [
    # ------------------------------------------------------------------ #
    #  settings_window_dialog.py
    # ------------------------------------------------------------------ #
    {
        "name": "speech-enabled-toggle-also-saves",
        "file": DIALOG,
        "old": (
            '            menu_layout, "Speech Enabled",\n'
            "            lambda checked=False: self.speech_toggle_requested.emit(),\n"
        ),
        "new": (
            '            menu_layout, "Speech Enabled",\n'
            "            lambda checked=False: (\n"
            "                self.speech_toggle_requested.emit(),\n"
            '                self.setting_changed.emit("SPEECH_ENABLED", checked)),\n'
        ),
        "selection": DIALOG_TESTS,
        "expect": {"test_speech_enabled_sends_the_menu_command_and_never_saves": "abled_state'}]"},
    },
    {
        "name": "debug-toggle-also-saves",
        "file": DIALOG,
        "old": (
            '            menu_layout, "Debug",\n'
            "            lambda checked=False: self.debug_toggle_requested.emit(),\n"
        ),
        "new": (
            '            menu_layout, "Debug",\n'
            "            lambda checked=False: (\n"
            "                self.debug_toggle_requested.emit(),\n"
            '                self.setting_changed.emit("DEBUG", checked)),\n'
        ),
        "selection": DIALOG_TESTS,
        "expect": {"test_debug_sends_the_menu_command_and_never_saves": "e_log_level'}]"},
    },
    {
        "name": "restart-text-missing-beside-log-transcripts",
        "file": DIALOG,
        "old": "        self._add_restart_note(layout, LOG_TRANSCRIPTS_KEY)\n",
        "new": "        pass\n",
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        "name": "restart-text-missing-beside-replacement-timeout",
        "file": DIALOG,
        "old": "        self._add_restart_note(layout, key)\n",
        "new": (
            "        if key != REPLACEMENT_TIMEOUT_KEY:\n"
            "            self._add_restart_note(layout, key)\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        "name": "restart-text-missing-beside-command-timeout",
        "file": DIALOG,
        "old": "        self._add_restart_note(layout, key)\n",
        "new": (
            "        if key != COMMAND_TIMEOUT_KEY:\n"
            "            self._add_restart_note(layout, key)\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        "name": "restart-text-missing-beside-audio-suppression",
        "file": DIALOG,
        "old": "        self._add_restart_note(layout, AUDIO_SUPPRESSION_KEY)\n",
        "new": "        pass\n",
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        "name": "restart-text-missing-beside-wake-word",
        "file": DIALOG,
        "old": (
            "        self._add_restart_note(layout, WAKE_WORD_KEY)\n"
            "        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)\n"
        ),
        "new": (
            "        pass\n"
            "        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        # Two edits: the updates row keeps its layout, and a note is added to
        # it after _restart_notes exists (it is created further down).
        "name": "restart-text-added-beside-check-for-updates",
        "file": DIALOG,
        "edits": [
            (
                "        self._updates_box, _ = self._check_box(\n",
                "        self._updates_box, _upd_layout = self._check_box(\n",
            ),
            (
                "        self._add_restart_note(layout, LOG_TRANSCRIPTS_KEY)\n",
                "        self._add_restart_note(layout, LOG_TRANSCRIPTS_KEY)\n"
                "        self._add_restart_note(_upd_layout, CHECK_FOR_UPDATES_KEY)\n",
            ),
        ],
        "selection": DIALOG_TESTS,
        "expect": {RESTART_ROWS: "AssertionError: ['If the wake word"},
    },
    {
        "name": "timeout-step-becomes-200",
        "file": DIALOG,
        "old": "TIMEOUT_STEP_MS = 100\n",
        "new": "TIMEOUT_STEP_MS = 200\n",
        "selection": DIALOG_TESTS,
        "expect": {
            ARITHMETIC: "assert 400 == 300",
            # Strengthened with stored 1000 and 1200: the stored 700 gave 800
            # and 600 under a step of 200 as well, so this test missed it.
            "test_a_timeout_step_inside_the_range_moves_by_100": "T_MS', 1100))]",
        },
    },
    {
        "name": "timeout-minimum-becomes-100",
        "file": DIALOG,
        "old": "TIMEOUT_MIN_MS = 200\n",
        "new": "TIMEOUT_MIN_MS = 100\n",
        "selection": DIALOG_TESTS,
        "expect": {
            ARITHMETIC: "assert 100 == 200",
            "test_a_press_at_a_limit_writes_nothing": "UT_MS', 100))] == []",
        },
    },
    {
        "name": "timeout-maximum-becomes-4000",
        "file": DIALOG,
        "old": "TIMEOUT_MAX_MS = 3000\n",
        "new": "TIMEOUT_MAX_MS = 4000\n",
        "selection": DIALOG_TESTS,
        "expect": {
            ARITHMETIC: "assert 3400 == 3000",
            "test_a_press_at_a_limit_writes_nothing": "T_MS', 3100))] == []",
            "test_the_first_press_moves_an_out_of_range_value_into_the_range": "AssertionError: assert [('setting_ch",
        },
    },
    {
        "name": "below-range-press-does-not-snap-to-minimum",
        "file": DIALOG,
        "old": (
            "    if current < TIMEOUT_MIN_MS:\n"
            "        return TIMEOUT_MIN_MS\n"
        ),
        "new": (
            "    if current < TIMEOUT_MIN_MS:\n"
            "        return None\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {ARITHMETIC: "assert None == 200"},
    },
    {
        "name": "above-range-press-does-not-snap-to-maximum",
        "file": DIALOG,
        "old": (
            "    if current > TIMEOUT_MAX_MS:\n"
            "        return TIMEOUT_MAX_MS\n"
        ),
        "new": (
            "    if current > TIMEOUT_MAX_MS:\n"
            "        return None\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {
            ARITHMETIC: "assert None == 3000",
            "test_the_first_press_moves_an_out_of_range_value_into_the_range": "AssertionError: assert [('setting_ch",
        },
    },
    {
        # 750 + Increase gives 700 (down), not 800.
        "name": "increase-from-a-non-multiple-goes-down",
        "file": DIALOG,
        "old": "        target = (current // TIMEOUT_STEP_MS + 1) * TIMEOUT_STEP_MS\n",
        "new": "        target = (current // TIMEOUT_STEP_MS) * TIMEOUT_STEP_MS\n",
        "selection": DIALOG_TESTS,
        "expect": {ARITHMETIC: "assert 700 == 800"},
    },
    {
        # 750 + Decrease gives 600 (two steps down), not 700.
        "name": "decrease-from-a-non-multiple-goes-too-far-down",
        "file": DIALOG,
        "old": "        ceiling = -((-current) // TIMEOUT_STEP_MS)\n",
        "new": "        ceiling = current // TIMEOUT_STEP_MS\n",
        "selection": DIALOG_TESTS,
        "expect": {ARITHMETIC: "assert 600 == 700"},
    },
    {
        # Clamping an out-of-range value as it loads writes without a press.
        "name": "a-value-is-written-on-load-without-a-press",
        "file": DIALOG,
        "old": '            parts["value"].setText(_format_ms(value))\n',
        "new": (
            '            parts["value"].setText(_format_ms(value))\n'
            "            if _is_number(value) and not (\n"
            "                    TIMEOUT_MIN_MS <= value <= TIMEOUT_MAX_MS):\n"
            "                self.setting_changed.emit(\n"
            "                    key, min(max(value, TIMEOUT_MIN_MS), TIMEOUT_MAX_MS))\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {
            "test_a_timeout_shows_the_value_in_use_and_writes_nothing_before_a_press": "T_MS', 3000))] == []",
        },
    },
    {
        # A tool button is a button, but not a check box, radio button or
        # push button, which is what the voice number overlay is tested on.
        "name": "a-decrease-button-is-a-tool-button",
        "file": DIALOG,
        "old": '        decrease = QPushButton("Decrease")\n',
        "new": (
            "        from PySide6.QtWidgets import QToolButton as _TB\n"
            "        decrease = _TB()\n"
            '        decrease.setText("Decrease")\n'
        ),
        "selection": DIALOG_TESTS,
        "expect": {NON_BUTTON: "assert False"},
    },
    {
        "name": "a-timeout-value-is-a-text-field",
        "file": DIALOG,
        "old": '        value = QLabel("")\n',
        "new": (
            "        from PySide6.QtWidgets import QLineEdit as _LE\n"
            '        value = _LE("")\n'
        ),
        "selection": DIALOG_TESTS,
        "expect": {NON_BUTTON: "AssertionError: QLineEdit"},
    },
    {
        # No other test looks the Close button up by name.
        "name": "a-control-loses-its-accessible-name",
        "file": DIALOG,
        "old": '        self._close_btn.setAccessibleName("Close")\n',
        "new": "        pass\n",
        "selection": DIALOG_TESTS,
        "expect": {NON_BUTTON: "AssertionError: Close"},
    },
    {
        "name": "restart-button-is-not-connected",
        "file": DIALOG,
        "old": (
            "        self._restart_btn.clicked.connect(\n"
            "            lambda checked=False: self.restart_requested.emit()\n"
            "        )\n"
        ),
        "new": "        pass\n",
        "selection": DIALOG_TESTS,
        "expect": {"test_the_restart_button_emits_restart_requested": "assert [] == [('restart_requested', ())]"},
    },
    {
        "name": "warning-text-missing-beside-speech-enabled",
        "file": DIALOG,
        "old": (
            "        # R4). A static text: no confirmation step.\n"
            "        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)\n"
        ),
        "new": (
            "        # R4). A static text: no confirmation step.\n"
            "        pass\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {WARNING_ROWS: "assert 1 == 2"},
    },
    {
        "name": "warning-text-missing-beside-wake-word",
        "file": DIALOG,
        "old": (
            "        self._add_restart_note(layout, WAKE_WORD_KEY)\n"
            "        layout.addWidget(self._note(WAKE_WORD_SPEECH_WARNING), 1)\n"
        ),
        "new": (
            "        self._add_restart_note(layout, WAKE_WORD_KEY)\n"
            "        pass\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {WARNING_ROWS: "assert 1 == 2"},
    },
    {
        "name": "wake-word-missing-key-default-becomes-true",
        "file": DIALOG,
        "old": "WAKE_WORD_ENABLED_DEFAULT = False\n",
        "new": "WAKE_WORD_ENABLED_DEFAULT = True\n",
        "selection": DIALOG_TESTS,
        "expect": {"test_a_missing_wake_word_key_shows_the_code_default": "assert True is False"},
    },
    {
        "name": "check-for-updates-missing-key-default-becomes-false",
        "file": DIALOG,
        "old": "            return UPDATE_CHECK_DEFAULT if value is None else bool(value)\n",
        "new": "            return False if value is None else bool(value)\n",
        "selection": DIALOG_TESTS,
        "expect": {
            "test_missing_update_and_transcript_keys_show_their_code_defaults": "assert False is True",
        },
    },
    {
        "name": "choosing-the-selected-audio-choice-writes",
        "file": DIALOG,
        "old": (
            "        if choice == _audio_choice(self._values.get(AUDIO_SUPPRESSION_KEY)):\n"
            "            return\n"
        ),
        "new": (
            "        if False:\n"
            "            return\n"
        ),
        "selection": DIALOG_TESTS,
        "expect": {"test_clicking_the_selected_audio_choice_writes_nothing": "ON', 'auto'))] == []"},
    },
    {
        "name": "saved-settings-are-enabled-before-the-values-arrive",
        "file": DIALOG,
        "old": "            box.setEnabled(self._loaded)\n",
        "new": "            box.setEnabled(True)\n",
        "selection": DIALOG_TESTS,
        "expect": {
            "test_the_saved_settings_stay_disabled_until_the_values_arrive": "assert not True",
        },
    },
    # ------------------------------------------------------------------ #
    #  gui.py
    # ------------------------------------------------------------------ #
    {
        "name": "open-settings-window-dispatch-does-nothing",
        "file": GUI,
        "old": (
            "                    self._open_settings_window()\n"
            '                elif action == "open_help_explainer":\n'
        ),
        "new": (
            "                    pass\n"
            '                elif action == "open_help_explainer":\n'
        ),
        "selection": GUI_TESTS,
        "expect": {"test_the_gui_dispatch_opens_the_window_and_reuses_it": "'_sw_dialog'"},
    },
    {
        "name": "tray-settings-item-sends-the-wrong-action",
        "file": GUI,
        "old": (
            '                    lambda: self.state_from_logic_queue.put({"action": "open_settings_window"}),\n'
        ),
        "new": (
            '                    lambda: self.state_from_logic_queue.put({"action": "open_pattern_manager"}),\n'
        ),
        "selection": GUI_TESTS,
        "expect": {"test_the_tray_item_asks_the_qt_thread_to_open_the_window": "expected call not found"},
    },
    {
        "name": "qt-settings-item-connects-to-the-wrong-slot",
        "file": GUI,
        "old": "            settings_action.triggered.connect(self._open_settings_window)\n",
        "new": "            settings_action.triggered.connect(self._open_pattern_manager)\n",
        "selection": GUI_TESTS,
        "expect": {"test_the_window_item_opens_the_window": "Called 0 times"},
    },
    {
        # Returning True for every write result also keeps the four existing
        # keys' results from the shared handler.
        "name": "write-result-is-swallowed-before-the-shared-handler",
        "file": GUI,
        "old": (
            "            dialog.show_save_result(saved)\n"
            "        return False\n"
        ),
        "new": (
            "            dialog.show_save_result(saved)\n"
            "        return True\n"
        ),
        "selection": GUI_TESTS,
        "expect": {
            "test_the_floating_button_write_tracking_still_works": "assert True is False",
            "test_a_failed_save_of_a_window_key_still_shows_the_gui_failure_notice": "in ''",
        },
    },
    {
        "name": "window-never-shows-a-write-result",
        "file": GUI,
        "old": "        if keys:\n",
        "new": "        if False:\n",
        "selection": GUI_TESTS,
        "expect": {
            "test_a_write_result_shows_saved_and_keeps_the_new_value": "assert '' == 'Saved'",
            "test_a_failed_write_shows_not_saved_and_restores_the_stored_value": "assert '' == 'Not saved'",
        },
    },
    {
        "name": "read-reply-for-another-request-reaches-the-window",
        "file": GUI,
        "old": (
            "            if request_id != getattr(self, '_sw_read_request_id', None):\n"
            "                return False\n"
        ),
        "new": (
            "            if False:\n"
            "                return False\n"
        ),
        "selection": GUI_TESTS,
        "expect": {
            "test_a_read_reply_for_another_request_does_not_touch_the_window": "assert True is False",
        },
    },
    {
        "name": "ptt-toggle-refreshes-the-window-again",
        "file": GUI,
        "old": (
            "        self.update_tray_menu()\n"
            "        # The settings window is not refreshed here:"
        ),
        "new": (
            "        self.update_tray_menu()\n"
            "        self._refresh_settings_window()\n"
            "        # The settings window is not refreshed here:"
        ),
        "selection": GUI_TESTS,
        "expect": {"test_the_push_to_talk_toggle_never_touches_the_window": "to not have been called. Called 1 times"},
    },
    {
        "name": "update-ui-state-no-longer-refreshes-the-window",
        "file": GUI,
        "old": (
            "        self.update_tray_menu()\n"
            "        self._refresh_settings_window()\n"
        ),
        "new": "        self.update_tray_menu()\n",
        "selection": GUI_TESTS,
        "expect": {
            "test_a_state_update_while_the_window_is_open_refreshes_without_commands": "assert False",
            "test_the_state_update_after_a_push_to_talk_change_refreshes_the_window": "assert False",
        },
    },
    {
        "name": "gui-restart-signal-is-not-connected",
        "file": GUI,
        "old": "            dialog.restart_requested.connect(self.request_restart)\n",
        "new": "            pass\n",
        "selection": GUI_TESTS,
        "expect": {"test_the_restart_button_calls_request_restart": "Called 0 times"},
    },
    {
        "name": "interim-results-item-connects-to-the-wrong-slot",
        "file": GUI,
        "old": "            dialog.interim_toggle_requested.connect(self.toggle_interim_results)\n",
        "new": "            dialog.interim_toggle_requested.connect(self._toggle_ptt_mode)\n",
        "selection": GUI_TESTS,
        "expect": {"test_interim_results_calls_the_toggle_path": "Called 0 times"},
    },
    {
        "name": "opening-does-not-ask-for-all-six-saved-settings",
        "file": GUI,
        "old": "            'keys': list(CONFIG_KEYS),\n",
        "new": "            'keys': list(CONFIG_KEYS)[:3],\n",
        "selection": GUI_TESTS,
        "expect": {"test_opening_asks_logic_for_the_six_saved_settings": "AssertionError: assert ['CHECK_FOR_U"},
    },
    {
        "name": "opening-does-not-restore-a-minimized-window",
        "file": GUI,
        "old": (
            "            'keys': list(CONFIG_KEYS),\n"
            "        })\n"
            "        state = dialog.windowState()\n"
            "        if state & Qt.WindowState.WindowMinimized:\n"
        ),
        "new": (
            "            'keys': list(CONFIG_KEYS),\n"
            "        })\n"
            "        state = dialog.windowState()\n"
            "        if False:\n"
        ),
        "selection": GUI_TESTS,
        "expect": {"test_opening_restores_a_minimized_window": "AssertionError: [('show',)"},
    },
    {
        "name": "opening-does-not-take-the-foreground",
        "file": GUI,
        "old": "            _steal_foreground(int(dialog.winId()))\n",
        "new": "            pass\n",
        "selection": GUI_TESTS,
        "expect": {"test_opening_takes_the_foreground_with_the_window_handle": "Called 0 times"},
    },
    {
        "name": "a-saved-value-is-sent-under-the-wrong-key",
        "file": GUI,
        "old": "        self.send_command({'action': 'set_config_value', 'key': key, 'value': value})\n",
        "new": "        self.send_command({'action': 'set_config_value', 'key': key + '_X', 'value': value})\n",
        "selection": GUI_TESTS,
        "expect": {
            "test_a_check_box_setting_sends_set_config_value_with_a_request_id": "'CHECK_FOR_UPDATES_X' == 'CHECK_FOR_UPDATES'",
            "test_timeout_and_audio_settings_send_set_config_value": "ON_X', False)]",
        },
    },
    # ------------------------------------------------------------------ #
    #  The voice command: actions, catalog, patterns
    # ------------------------------------------------------------------ #
    {
        "name": "the-action-sends-nothing",
        "file": ACTIONS,
        "old": '        self._send_gui_action({"action": "open_settings_window"})\n',
        "new": "        pass\n",
        "selection": VOICE_TESTS,
        "expect": {"test_open_settings_window_action_sends_the_gui_open_message": "Called 0 times"},
    },
    {
        "name": "the-action-is-not-registered",
        "file": ACTIONS,
        "old": '        self._functions["open_settings_window"] = self.open_settings_window\n',
        "new": "        pass\n",
        "selection": VOICE_TESTS,
        "expect": {"test_open_settings_window_action_sends_the_gui_open_message": "assert 'open_settings_window' in"},
    },
    {
        "name": "the-catalog-entry-is-missing",
        "file": CATALOG,
        "old": '        "name": "open_settings_window",\n',
        "new": '        "name": "open_settings_window_missing",\n',
        "selection": VOICE_TESTS,
        "expect": {"test_the_action_catalog_describes_open_settings_window": "assert 0 == 1"},
    },
    {
        "name": "the-pattern-accepts-only-settings",
        "file": PATTERNS,
        "old": "pattern = '''^settings?$'''\n",
        "new": "pattern = '''^settings$'''\n",
        "selection": VOICE_TESTS,
        "expect": {
            "test_settings_and_setting_resolve_as_a_whole_utterance": "assert None",
            "test_settings_forms_reach_the_settings_row_first": "assert None == 'settings-window'",
        },
    },
    {
        "name": "the-pattern-loses-whole-utterance-only",
        "file": PATTERNS,
        "old": (
            'doc_id = "settings-window"\n'
            "whole_utterance_only = true\n"
        ),
        "new": (
            'doc_id = "settings-window"\n'
            "whole_utterance_only = false\n"
        ),
        "selection": VOICE_TESTS,
        "expect": {"test_settings_and_setting_resolve_as_a_whole_utterance": "assert False is True"},
    },
    # ------------------------------------------------------------------ #
    #  Stage A: INTERIM_RESULTS
    # ------------------------------------------------------------------ #
    {
        "name": "state-manager-ignores-the-interim-results-key",
        "file": STATE_MANAGER,
        "old": (
            "        self.interim_results_enabled = "
            "read_interim_results_setting(self.config_service)\n"
        ),
        "new": "        self.interim_results_enabled = True\n",
        "selection": STATE_TESTS,
        "expect": {
            "test_state_manager.py::test_false_in_config_starts_with_interim_results_off": "assert True is False",
        },
    },
    {
        "name": "websocket-manager-ignores-the-interim-results-key",
        "file": WEBSOCKET_MANAGER,
        "old": "            self.interim_results_enabled = read_interim_results_setting(config)\n",
        "new": "            self.interim_results_enabled = True\n",
        "selection": WEBSOCKET_TESTS,
        "expect": {
            "test_websocket_manager.py::test_false_in_config_starts_with_interim_results_off": "assert True is False",
            "test_a_new_client_is_told_the_value_read_from_config": "'enabled': True, 'type': 'set_interim_results'",
        },
    },
    {
        "name": "interim-results-missing-key-default-becomes-false",
        "file": WEBSOCKET_MANAGER,
        "old": '        value = config_service.get("INTERIM_RESULTS", True)\n',
        "new": '        value = config_service.get("INTERIM_RESULTS", False)\n',
        "selection": READ_TESTS,
        "expect": {
            "test_state_manager.py::test_missing_key_keeps_interim_results_on": "assert False is True",
            "test_websocket_manager.py::test_missing_key_keeps_interim_results_on": "assert False is True",
        },
    },
    {
        "name": "interim-results-non-bool-value-turns-it-off",
        "file": WEBSOCKET_MANAGER,
        "old": (
            '        f"INTERIM_RESULTS must be true or false, got {value!r}; using true instead"\n'
            "    )\n"
            "    return True\n"
        ),
        "new": (
            '        f"INTERIM_RESULTS must be true or false, got {value!r}; using true instead"\n'
            "    )\n"
            "    return False\n"
        ),
        "selection": READ_TESTS,
        "expect": {
            "test_state_manager.py::test_non_bool_value_keeps_interim_results_on_and_warns": "assert False is True",
            "test_websocket_manager.py::test_non_bool_value_keeps_interim_results_on_and_warns": "assert False is True",
        },
    },
    {
        "name": "the-interim-results-toggle-does-not-save",
        "file": MAIN,
        "old": "            await self.state_manager.set_config_value('INTERIM_RESULTS', enabled)\n",
        "new": "            pass\n",
        "selection": ["tests/test_toggle_interim_results.py"],
        "expect": {
            "test_the_new_value_is_saved_with_the_toggle": "Awaited 0 times",
            "test_toggling_back_on_saves_true": "Awaited 0 times",
            "test_a_real_state_manager_stores_the_value_and_saves_to_disk": "assert None is False",
        },
    },
    # ------------------------------------------------------------------ #
    #  Stage A: LOG_TRANSCRIPTS read in each start cycle
    # ------------------------------------------------------------------ #
    {
        # MOVE: the call leaves the loop and runs once before it.
        "name": "transcript-export-moves-back-before-the-loop",
        "file": LAUNCHER,
        "edits": [
            (
                "        _export_transcript_logging_flag(_config_toml)\n",
                "",
            ),
            (
                '    _config_toml = os.path.join(os.path.dirname(__file__), "config.toml")\n',
                '    _config_toml = os.path.join(os.path.dirname(__file__), "config.toml")\n'
                "    _export_transcript_logging_flag(_config_toml)\n",
            ),
        ],
        "selection": LAUNCHER_TESTS,
        "expect": {
            "test_a_switch_turned_off_between_cycles_reaches_the_second_cycle": "['1', '1', '1', '1', '1', '1']",
            "test_a_switch_turned_on_between_cycles_reaches_the_second_cycle": "['0', '0', '0', '0', '0', '0']",
            "test_the_enabled_line_is_logged_for_every_cycle_that_has_it_on": "Transcript logging ENABLED (LOG_TRANSCRIPTS = true)",
        },
    },
    {
        "name": "the-enabled-log-line-is-removed",
        "file": LAUNCHER,
        "old": (
            "        logger.info(\n"
            '            "Transcript logging ENABLED (LOG_TRANSCRIPTS = true): dictated "\n'
            '            "text will appear in logs."\n'
            "        )\n"
        ),
        "new": "        pass\n",
        "selection": LAUNCHER_TESTS,
        "expect": {
            "test_the_enabled_line_is_logged_only_for_a_cycle_that_has_it_on": "AssertionError: ['[priority]",
            "test_the_enabled_line_is_logged_for_every_cycle_that_has_it_on": "AssertionError: ['[priority]",
        },
    },
    {
        "name": "the-exported-value-is-always-on",
        "file": LAUNCHER,
        "old": '    os.environ["WHEELHOUSE_LOG_TRANSCRIPTS"] = "1" if enabled else "0"\n',
        "new": '    os.environ["WHEELHOUSE_LOG_TRANSCRIPTS"] = "1"\n',
        "selection": LAUNCHER_TESTS,
        "expect": {
            "test_a_switch_turned_off_between_cycles_reaches_the_second_cycle": "['1', '1', '1', '1', '1', '1']",
        },
    },
    # ------------------------------------------------------------------ #
    #  settings_help_dialog.py (wh-settings-window-help)
    # ------------------------------------------------------------------ #
    {
        "name": "help-missing-file-message-drops-the-path",
        "file": HELP_DIALOG,
        "old": '            f"The settings help file is missing: {path}"\n',
        "new": '            "The settings help file is missing"\n',
        "selection": HELP_TESTS,
        "expect": {"test_load_missing_file_raises_naming_problem_and_path": "AssertionError: assert 'C:"},
    },
    {
        "name": "help-missing-file-reported-as-unreadable",
        "file": HELP_DIALOG,
        "old": "    except FileNotFoundError:\n",
        "new": "    except ZeroDivisionError:\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_missing_file_raises_naming_problem_and_path": "assert 'help file is missing: ' in"},
    },
    {
        "name": "help-read-error-escapes-as-os-error",
        "file": HELP_DIALOG,
        "old": "    except OSError as exc:\n        raise SettingsHelpError(\n            f\"The settings help file cannot be read",
        "new": "    except ZeroDivisionError as exc:\n        raise SettingsHelpError(\n            f\"The settings help file cannot be read",
        "selection": HELP_TESTS,
        "expect": {"test_load_unreadable_path_raises": "PermissionError: [Errno 13]"},
    },
    {
        "name": "help-read-error-message-drops-the-path",
        "file": HELP_DIALOG,
        "old": '            f"The settings help file cannot be read: {path} ({exc})"\n',
        "new": '            f"The settings help file cannot be read: ({exc})"\n',
        "selection": HELP_TESTS,
        "expect": {"test_load_unreadable_path_raises": "The settings help file cannot be read: ([Errno 13]"},
    },
    {
        "name": "help-non-utf8-error-escapes",
        "file": HELP_DIALOG,
        "old": "    except (UnicodeDecodeError, ValueError) as exc:\n",
        "new": "    except json.JSONDecodeError as exc:\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_non_utf8_raises": "UnicodeDecodeError: 'utf-8' codec"},
    },
    {
        "name": "help-bad-json-error-escapes",
        "file": HELP_DIALOG,
        "old": "    except (UnicodeDecodeError, ValueError) as exc:\n",
        "new": "    except UnicodeDecodeError as exc:\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_invalid_json_raises": "json.decoder.JSONDecodeError: Expecting property name"},
    },
    {
        "name": "help-bad-json-message-drops-the-path",
        "file": HELP_DIALOG,
        "old": '            f"The settings help file is not valid UTF-8 JSON: {path} ({exc})"\n',
        "new": '            f"The settings help file is not valid UTF-8 JSON: ({exc})"\n',
        "selection": HELP_TESTS,
        "expect": {
            "test_load_invalid_json_raises": "is not valid UTF-8 JSON: (Expecting property name",
            "test_load_non_utf8_raises": "is not valid UTF-8 JSON: ('utf-8' codec",
        },
    },
    {
        "name": "help-top-level-list-not-refused",
        "file": HELP_DIALOG,
        "old": "    entries = data.get(\"settings\") if isinstance(data, dict) else None\n",
        "new": "    entries = data.get(\"settings\")\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_wrong_shape_raises": "AttributeError: 'list' object has no attribute 'get'"},
    },
    {
        "name": "help-empty-settings-list-accepted",
        "file": HELP_DIALOG,
        "old": "    if not isinstance(entries, list) or not entries:\n",
        "new": "    if not isinstance(entries, list):\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_wrong_shape_raises": "DID NOT RAISE"},
    },
    {
        "name": "help-blank-label-accepted",
        "file": HELP_DIALOG,
        "old": "        if not (isinstance(label, str) and label.strip()\n",
        "new": "        if not (isinstance(label, str)\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_wrong_shape_raises": "DID NOT RAISE"},
    },
    {
        "name": "help-blank-text-accepted",
        "file": HELP_DIALOG,
        "old": "                and isinstance(text, str) and text.strip()):\n",
        "new": "                and isinstance(text, str)):\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_wrong_shape_raises": "DID NOT RAISE"},
    },
    {
        "name": "help-non-dict-entry-escapes-as-attribute-error",
        "file": HELP_DIALOG,
        "old": "        label = item.get(\"label\") if isinstance(item, dict) else None\n",
        "new": "        label = item.get(\"label\")\n",
        "selection": HELP_TESTS,
        "expect": {"test_load_wrong_shape_raises": "AttributeError: 'str' object has no attribute 'get'"},
    },
    {
        "name": "help-load-returns-entries-reversed",
        "file": HELP_DIALOG,
        "old": "        pairs.append((label, text))\n",
        "new": "        pairs.insert(0, (label, text))\n",
        "selection": HELP_TESTS,
        "expect": {
            "test_load_good_file_returns_pairs_in_order": "assert [('A', 'ta'), ('B', 'tb')] == [('B', 'tb'), ('A', 'ta')]",
            "test_help_window_labels_equal_settings_window_order": "AssertionError: assert ['Wake word',",
        },
    },
    {
        "name": "help-html-label-not-escaped",
        "file": HELP_DIALOG,
        "old": "<p><b>{html.escape(label)}</b>",
        "new": "<p><b>{label}</b>",
        "selection": HELP_TESTS,
        "expect": {"test_html_escapes_and_entry_starts_with_label": "AssertionError: assert ('<i>' not in"},
    },
    {
        "name": "help-html-text-not-escaped",
        "file": HELP_DIALOG,
        "old": "<br/>{html.escape(text)}</p>",
        "new": "<br/>{text}</p>",
        "selection": HELP_TESTS,
        "expect": {"test_html_escapes_and_entry_starts_with_label": "AssertionError: assert ('<i>' not in"},
    },
    {
        "name": "help-html-entries-in-reverse-order",
        "file": HELP_DIALOG,
        "old": "    for label, text in pairs:\n        parts.append(",
        "new": "    for label, text in reversed(list(pairs)):\n        parts.append(",
        "selection": HELP_TESTS,
        "expect": {
            "test_html_escapes_and_entry_starts_with_label": " < 39",
            "test_rendered_entries_start_with_label": "ValueError: substring not found",
        },
    },
    {
        "name": "help-entry-labels-in-reverse-order",
        "file": HELP_DIALOG,
        "old": "        self._labels = [label for label, _ in pairs]\n",
        "new": "        self._labels = [label for label, _ in reversed(list(pairs))]\n",
        "selection": HELP_TESTS,
        "expect": {"test_help_window_labels_equal_settings_window_order": "AssertionError: assert ['Wake word',"},
    },
    {
        "name": "help-close-button-does-not-close",
        "file": HELP_DIALOG,
        "old": "        close_btn.clicked.connect(lambda checked=False: self.close())\n",
        "new": "        close_btn.clicked.connect(lambda checked=False: None)\n",
        "selection": HELP_TESTS,
        "expect": {"test_close_button_closes_help_window": "assert not True"},
    },
    {
        "name": "help-close-button-accessible-name-changed",
        "file": HELP_DIALOG,
        "old": '        close_btn.setAccessibleName("Close")\n',
        "new": '        close_btn.setAccessibleName("Done")\n',
        "selection": HELP_TESTS,
        "expect": {"test_close_button_closes_help_window": "assert (0 == 1)"},
    },
    # ------------------------------------------------------------------ #
    #  settings_window_dialog.py: the Help button and the help window
    # ------------------------------------------------------------------ #
    {
        "name": "help-button-accessible-name-changed",
        "file": DIALOG,
        "old": '        self._help_btn.setAccessibleName("Help")\n',
        "new": '        self._help_btn.setAccessibleName("Assist")\n',
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_button_in_bottom_row_and_tab_order": "assert 0 == 1"},
    },
    {
        "name": "help-button-moved-out-of-the-bottom-row",
        "file": DIALOG,
        "old": (
            "        layout.addWidget(self._help_btn)\n"
            "        self._restart_btn = QPushButton(\"Restart WheelHouse\")\n"
        ),
        "new": (
            "        main.addWidget(self._help_btn)\n"
            "        self._restart_btn = QPushButton(\"Restart WheelHouse\")\n"
        ),
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_button_in_bottom_row_and_tab_order": "settings_window_dialog.SettingsWindowDialog("},
    },
    {
        "name": "help-button-missing-from-tab-order",
        "file": DIALOG,
        "old": (
            "        order += [self._wake_box, self._help_btn, self._restart_btn,\n"
            "                  self._close_btn]\n"
        ),
        "new": (
            "        order += [self._wake_box, self._restart_btn,\n"
            "                  self._close_btn]\n"
        ),
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_button_in_bottom_row_and_tab_order": "assert <PySide6.QtWidgets.QPushButton("},
    },
    {
        "name": "help-button-after-restart-in-tab-order",
        "file": DIALOG,
        "old": (
            "        order += [self._wake_box, self._help_btn, self._restart_btn,\n"
            "                  self._close_btn]\n"
        ),
        "new": (
            "        order += [self._wake_box, self._restart_btn, self._help_btn,\n"
            "                  self._close_btn]\n"
        ),
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_button_in_bottom_row_and_tab_order": "assert 16 == (14 + 1)"},
    },
    {
        "name": "setting-label-not-recorded-for-ai-model",
        "file": DIALOG,
        "old": '        self._setting_labels.append("AI Model")\n',
        "new": "        pass\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {
            "test_setting_labels_has_13_entries_each_visible_in_window": "assert 12 == 13",
            "test_every_setting_control_in_window_has_a_recorded_label": "assert 12 == ((8 + 2) + 3)",
        },
    },
    {
        "name": "setting-label-not-recorded-for-check-boxes",
        "file": DIALOG,
        "old": "        self._setting_labels.append(text)\n        row, layout = self._row(parent_layout)\n        box = QCheckBox(text)\n",
        "new": "        row, layout = self._row(parent_layout)\n        box = QCheckBox(text)\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {
            "test_setting_labels_has_13_entries_each_visible_in_window": "assert 5 == 13",
            "test_every_setting_control_in_window_has_a_recorded_label": "assert 5 == ((8 + 2) + 3)",
        },
    },
    {
        "name": "setting-label-not-recorded-for-timeouts",
        "file": DIALOG,
        "old": "        self._setting_labels.append(title)\n",
        "new": "        pass\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {
            "test_setting_labels_has_13_entries_each_visible_in_window": "assert 11 == 13",
            "test_every_setting_control_in_window_has_a_recorded_label": "assert 11 == ((8 + 2) + 3)",
        },
    },
    {
        "name": "setting-labels-ai-model-recorded-before-stt-provider",
        "file": DIALOG,
        "edits": [
            (
                '        self._setting_labels.append("STT Provider")\n',
                '        self._setting_labels.append("AI Model")\n'
                '        self._setting_labels.append("STT Provider")\n',
            ),
            (
                '        self._setting_labels.append("AI Model")\n'
                "        ai_layout = ",
                "        ai_layout = ",
            ),
        ],
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_window_labels_equal_settings_window_order": "AssertionError: assert ['Speech Enab"},
    },
    {
        "name": "help-error-path-still-opens-the-window",
        "file": DIALOG,
        "old": "            self._show_help_error(str(exc))\n            return\n",
        "new": "            self._show_help_error(str(exc))\n            pairs = []\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_click_with_missing_file_warns_and_window_keeps_working": "assert [<settings_he"},
    },
    {
        "name": "help-error-is-not-shown",
        "file": DIALOG,
        "old": "            self._show_help_error(str(exc))\n            return\n",
        "new": "            return\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_click_with_missing_file_warns_and_window_keeps_working": "assert 0 == 1"},
    },
    {
        "name": "help-error-message-is-not-the-load-error",
        "file": DIALOG,
        "old": "            self._show_help_error(str(exc))\n",
        "new": "            self._show_help_error(\"The help text cannot be shown\")\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_click_with_missing_file_warns_and_window_keeps_working": "in 'The help text cannot be shown'"},
    },
    {
        "name": "help-window-created-on-every-click",
        "file": DIALOG,
        "old": "        if dlg is None:\n            dlg = SettingsHelpDialog(pairs, parent=self)\n",
        "new": "        if True:\n            dlg = SettingsHelpDialog(pairs, parent=self)\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_second_click_does_not_open_a_second_window": "assert 2 == 1"},
    },
    {
        "name": "help-window-not-remembered",
        "file": DIALOG,
        "old": "            self._help_window = dlg\n",
        "new": "            pass\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_second_click_does_not_open_a_second_window": "assert 2 == 1"},
    },
    {
        "name": "help-window-not-shown",
        "file": DIALOG,
        "old": "        dlg.show()\n        dlg.raise_()\n",
        "new": "        dlg.raise_()\n",
        "selection": HELP_TESTS + DIALOG_TESTS,
        "expect": {"test_help_button_opens_visible_help_window": "assert False"},
    },
]


# ---------------------------------------------------------------------- #
#  Runner (structure from tests/mutation_gate_log_rotation.py)
# ---------------------------------------------------------------------- #


def _publish_durable(tmp, path):
    """Replace path with tmp so the rename itself is on disk on return.

    MOVEFILE_WRITE_THROUGH makes the call wait until the move is on disk, so
    a journal lost to a power failure cannot hide a still-mutated target.
    Non-Windows falls back to os.replace.
    """
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.MoveFileExW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ]
        kernel32.MoveFileExW.restype = ctypes.c_int
        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        if not kernel32.MoveFileExW(
            str(tmp), str(path),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError()
    else:
        os.replace(tmp, path)


def _write_atomic(path, data):
    """Write data (bytes) to path through a same-directory temp file.

    A partial write can never land in path. Always bytes, never text: on
    Windows write_text would translate every newline.
    """
    tmp = path.with_name(path.name + ".gate-tmp")
    try:
        with open(tmp, "wb") as f:
            written = f.write(data)
            if written != len(data):
                raise OSError(f"short write: {written} of {len(data)} bytes")
            f.flush()
            os.fsync(f.fileno())
        last = None
        for _ in range(5):
            try:
                _publish_durable(tmp, path)
                last = None
                break
            except OSError as e:  # another process may hold the target
                last = e
                time.sleep(0.3)
        if last is not None:
            raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _journal_path(path):
    return path.with_name(path.name + ".gate-journal")


def _write_journal(path, original, mutated):
    """Persist the original and mutant bytes before the mutant lands."""
    payload = str(len(original)).encode("ascii") + b"\n" + original + mutated
    _write_atomic(_journal_path(path), payload)


def _clear_journal(path):
    try:
        _journal_path(path).unlink()
    except FileNotFoundError:
        pass


def _recover_pending(paths):
    """Undo any mutant a dead run left behind. Returns error strings."""
    errors = []
    for path in sorted(set(paths)):
        journal = _journal_path(path)
        if not journal.exists():
            continue
        try:
            payload = journal.read_bytes()
            header, rest = payload.split(b"\n", 1)
            length = int(header)
            original, mutated = rest[:length], rest[length:]
            current = path.read_bytes()
        except (OSError, ValueError) as e:
            errors.append(f"{path}: unreadable recovery journal {journal}: {e}")
            continue
        if current == mutated and current != original:
            try:
                _write_atomic(path, original)
            except OSError as e:
                errors.append(
                    f"{path}: recovery restore failed; the MUTANT REMAINS: {e}"
                )
                continue
            _clear_journal(path)
            print(f"recovered {path} from an interrupted prior run")
        elif current == original:
            _clear_journal(path)
            print(f"dropped stale journal for {path} (target already original)")
        else:
            errors.append(
                f"{path}: recovery journal exists but the target matches "
                "neither the recorded original nor the recorded mutant; "
                "reconcile the file by hand"
            )
    return errors


def _restore_source(path, original, mutated, name):
    """Put the pre-run bytes back. Returns an error string, or None.

    Never overwrites bytes this run did not write.
    """
    try:
        current = path.read_bytes()
    except OSError as e:
        return (f"{name}: cannot read {path} before restore; "
                f"the MUTANT MAY REMAIN in source: {e}")
    if current == original:
        return None
    if current != mutated:
        return (f"{name}: {path} changed while pytest ran (bytes differ "
                "from the written mutant); refusing to overwrite the "
                "concurrent edit with the stale pre-run snapshot -- "
                "reconcile the file by hand")
    # crewcut: a check-to-replace window remains -- a save landing between
    # the equality read above and the replace inside _write_atomic is
    # overwritten by the original snapshot. To remove: mutate a disposable
    # copy of the source tree instead of live source. Accepted because the
    # window needs a writer inside this session-owned worktree during a
    # gate run, which the worktree rules forbid.
    try:
        _write_atomic(path, original)
    except OSError as e:
        return f"{name}: restore write failed; the MUTANT REMAINS in {path}: {e}"
    return None


def _restore_holding_interrupt(path, original, mutated, name):
    """Restore, surviving a Ctrl+C that lands inside the restore itself.

    Returns (error string or None, held KeyboardInterrupt or None). One
    interrupt during the restore is held and the restore retried once; a
    second interrupt during the retry is held too and the failure printed.
    The caller clears bytecode caches before re-raising the held interrupt.
    """
    held = None
    try:
        return _restore_source(path, original, mutated, name), None
    except KeyboardInterrupt as e:
        held = e
    try:
        return _restore_source(path, original, mutated, name), held
    except KeyboardInterrupt:
        msg = (f"{name}: restore interrupted twice; the MUTANT MAY REMAIN "
               f"in {path}; the recovery journal is kept")
        print(f"ERROR {msg}")
        return msg, held


def _pytest_env():
    # COLUMNS=1000 keeps pytest from truncating a short-summary line to the
    # 80 columns a captured run reports.
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", COLUMNS="1000")


def _run_pytest(selection):
    return subprocess.run(
        [PYTHON, "-m", "pytest", *selection, "-rf", "-q", "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=180,
    )


def _clear_pycache():
    for d in SERVICE.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def _translate(pattern: str, data: bytes) -> bytes:
    raw = pattern.encode("utf-8")
    if b"\r\n" in data:
        raw = raw.replace(b"\n", b"\r\n")
    return raw


def _edits(mutation):
    if "edits" in mutation:
        return list(mutation["edits"])
    return [(mutation["old"], mutation["new"])]


def _apply(mutation, data):
    """Return (mutated bytes, None) or (None, error string).

    Every edit needs exactly one match in the ORIGINAL bytes; the edits are
    then applied in order.
    """
    mutated = data
    for index, (old_text, new_text) in enumerate(_edits(mutation)):
        old = _translate(old_text, data)
        new = _translate(new_text, data)
        count = data.count(old)
        if count == 0:
            return None, f"pattern not found (edit {index + 1})"
        if count != 1:
            return None, f"pattern ambiguous ({count} matches, edit {index + 1})"
        mutated = mutated.replace(old, new, 1)
    return mutated, None


def _syntax_error(path, mutated):
    """The parse error of a mutant, or None. Only .py and .toml are parsed."""
    try:
        text = mutated.decode("utf-8")
        if path.suffix == ".py":
            compile(text, str(path), "exec")
        elif path.suffix == ".toml":
            tomllib.loads(text)
    except (SyntaxError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return exc
    return None


def _short_summary_lines(out):
    """Lines of pytest's short test summary section, or None when absent."""
    lines = out.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^=+ short test summary info =+$", line):
            start = i + 1
            break
    if start is None:
        return None
    section = []
    for line in lines[start:]:
        if line.startswith("="):
            break
        section.append(line)
    return section


def _failed_nodes(section):
    """Map failed node id (parameter list cut) -> joined reasons.

    The node id ends at the first " - ": a parametrized id may hold spaces,
    never that separator.
    """
    failed = {}
    for line in section:
        m = re.match(r"^FAILED (.*?)(?: - (.*))?$", line)
        if not m:
            continue
        node = re.sub(r"\[.*\]$", "", m.group(1)).replace("\\", "/")
        failed[node] = (failed.get(node, "") + " | " + (m.group(2) or "")).strip(" |")
    return failed


def _node_matches(node, key):
    """True when the key's ::-separated parts appear in the node, in order.

    The last part must be the node's own name; an earlier part may be a file
    or class component (a file part matches the end of the path).
    """
    parts = node.replace("\\", "/").split("::")
    want = key.split("::")
    if parts[-1] != want[-1]:
        return False
    index = 0
    for part in parts[:-1]:
        if index < len(want) - 1 and (
            part == want[index] or part.endswith("/" + want[index])
        ):
            index += 1
    return index == len(want) - 1


def _in_selection(node, selection):
    n = node.replace("\\", "/")
    return any(n.startswith(s.replace("\\", "/")) for s in selection)


def check_only() -> int:
    """Validate every pattern and mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = mutation["file"]
        data = path.read_bytes()
        mutated, error = _apply(mutation, data)
        if mutated is None:
            if error.startswith("pattern not found"):
                stale += 1
                print(f"STALE {mutation['name']}: {error} in {path}")
            else:
                ambiguous += 1
                print(f"AMBIGUOUS {mutation['name']}: {error} in {path}")
            continue
        if mutated == data:
            broken += 1
            print(f"BROKEN {mutation['name']}: the mutant equals the source")
            continue
        exc = _syntax_error(path, mutated)
        if exc is not None:
            broken += 1
            print(f"BROKEN {mutation['name']}: mutant does not parse: {exc}")
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{ambiguous} ambiguous, {broken} that do not compile"
    )
    print("--check is NOT a sweep: it cannot see a survivor or a masked catcher")
    return 1 if stale or ambiguous or broken else 0


def main() -> int:
    argv = sys.argv[1:]
    if "--check" in argv:
        return check_only()
    only = None
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))

    names = [m["name"] for m in MUTATIONS]
    if len(names) != len(set(names)):
        print("ERROR two mutations share a name")
        return 1
    if only is not None:
        unknown = only - set(names)
        if unknown:
            print(f"ERROR --only names no such mutation: {sorted(unknown)}")
            return 1
    selected = [m for m in MUTATIONS if only is None or m["name"] in only]

    errors = []
    survivors = []
    caught = 0

    recovery_errors = _recover_pending(m["file"] for m in MUTATIONS)
    if recovery_errors:
        for e in recovery_errors:
            print(f"ERROR {e}")
        return 1

    # Every expected test name must exist, inside the mutation's own
    # selection, before the first mutation.
    all_selection = []
    for m in MUTATIONS:
        for s in m["selection"]:
            if s not in all_selection:
                all_selection.append(s)
    collect = subprocess.run(
        [PYTHON, "-m", "pytest", *all_selection, "--collect-only", "-q",
         "-p", "no:randomly"],
        cwd=SERVICE, env=_pytest_env(), capture_output=True, text=True,
        timeout=300,
    )
    collected = [
        re.sub(r"\[.*\]$", "", line.strip()).replace("\\", "/")
        for line in collect.stdout.splitlines() if "::" in line
    ]
    if not collected:
        print("ERROR collection found no tests")
        print((collect.stdout + collect.stderr)[-2000:])
        return 1
    for m in selected:
        in_scope = [n for n in collected if _in_selection(n, m["selection"])]
        for key in m["expect"]:
            if not any(_node_matches(n, key) for n in in_scope):
                errors.append(
                    f"{m['name']}: expected test {key} does not exist inside "
                    f"the selection {m['selection']}"
                )
    if errors:
        for e in errors:
            print(f"ERROR {e}")
        return 1

    # Baseline must be green over every selection any mutation uses.
    base = _run_pytest(all_selection)
    if base.returncode != 0:
        print("ERROR baseline run is not green; refusing to mutate")
        print(base.stdout[-3000:])
        return 1
    print("baseline green")

    for m in selected:
        path = m["file"]
        data = path.read_bytes()
        mutated, error = _apply(m, data)
        if mutated is None:
            errors.append(f"{m['name']}: {error}")
            print(f"ERROR {m['name']}: {error}")
            continue
        exc = _syntax_error(path, mutated)
        if exc is not None:
            errors.append(f"{m['name']}: mutant does not parse: {exc}")
            print(f"ERROR {m['name']}: mutant does not parse: {exc}")
            continue
        _clear_pycache()
        try:
            _write_journal(path, data, mutated)
        except OSError as e:
            errors.append(
                f"{m['name']}: journal write failed; target left unchanged: {e}"
            )
            print(f"ERROR {errors[-1]}")
            continue
        try:
            _write_atomic(path, mutated)
        except OSError as e:
            errors.append(
                f"{m['name']}: mutant write failed; target left unchanged: {e}"
            )
            print(f"ERROR {errors[-1]}")
            _clear_journal(path)
            continue
        timed_out = False
        held = None
        run = None
        try:
            run = _run_pytest(m["selection"])
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            restore_error, held = _restore_holding_interrupt(
                path, data, mutated, m["name"])
            _clear_pycache()
            if held is not None:
                if restore_error:
                    print(f"ERROR {restore_error}")
                raise held
        if restore_error:
            errors.append(restore_error)
            print(f"ERROR {restore_error}")
            print("aborting remaining mutations: source no longer matches "
                  "what this run snapshotted; the recovery journal is kept "
                  "for the next invocation")
            break
        _clear_journal(path)
        if timed_out:
            errors.append(f"{m['name']}: pytest timed out")
            print(f"ERROR {m['name']}: pytest timed out")
            continue
        out = run.stdout + run.stderr
        if "+++ Timeout +++" in out:
            errors.append(f"{m['name']}: suite-timeout-abort")
            print(f"ERROR {m['name']}: suite-timeout-abort")
            continue
        if run.returncode == 0:
            survivors.append(f"{m['name']}: no test failed")
            print(f"SURVIVED {m['name']}: no test failed "
                  f"(expected {sorted(m['expect'])})")
            continue
        if run.returncode != 1:
            errors.append(f"{m['name']}: pytest exit code {run.returncode}")
            print(f"ERROR {m['name']}: pytest exit code {run.returncode}")
            print(out[-2000:])
            continue
        section = _short_summary_lines(out)
        if section is None:
            errors.append(f"{m['name']}: exit 1 but no short test summary")
            print(f"ERROR {m['name']}: exit 1 but no short test summary")
            print(out[-2000:])
            continue
        collection_errors = [ln for ln in section if ln.startswith("ERROR ")]
        if collection_errors:
            errors.append(f"{m['name']}: collection or fixture error")
            print(f"ERROR {m['name']}: the mutant broke collection or a "
                  f"fixture, so no test reached its assertion: "
                  f"{collection_errors[:3]}")
            continue
        failed = _failed_nodes(section)
        found = {}
        missing = []
        for key in m["expect"]:
            nodes = [n for n in failed if _node_matches(n, key)]
            if nodes:
                found[key] = " | ".join(failed[n] for n in nodes)
            else:
                missing.append(key)
        if missing:
            survivors.append(
                f"{m['name']}: expected {missing}, failed={sorted(failed)}")
            print(f"SURVIVED {m['name']}: expected {missing} to fail; "
                  f"failed={sorted(failed)}")
            continue
        wrong = {
            key: found[key] for key, reason in m["expect"].items()
            if reason not in found[key]
        }
        if wrong:
            errors.append(f"{m['name']}: wrong-reason {sorted(wrong)}")
            print(f"ERROR {m['name']}: wrong-reason (an expected catcher "
                  f"failed on something other than its named assertion)")
            for key, reason in wrong.items():
                print(f"    {key}: expected {m['expect'][key]!r} in {reason!r}")
            continue
        caught += 1
        print(f"caught {m['name']}")
        for key in m["expect"]:
            print(f"    {key}: {found[key][:400]}")
        others = sorted(
            n for n in failed
            if not any(_node_matches(n, key) for key in m["expect"])
        )
        if others:
            print(f"    also failed: {others}")

    scope = ("full set for this gate" if only is None
             else f"subset named by --only, {len(MUTATIONS) - len(selected)} skipped")
    print(
        f"scope: ran {len(selected)} of {len(MUTATIONS)} mutations ({scope}): "
        f"{caught} caught, {len(survivors)} survivors, {len(errors)} errors"
    )
    return 1 if (survivors or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
