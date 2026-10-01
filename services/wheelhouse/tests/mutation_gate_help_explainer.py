"""Mutation gate for the Wheelhouse Assistant explanation window (wh-assistant-button-explainer).

Run it from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_help_explainer.py --check
    .venv/Scripts/python.exe tests/mutation_gate_help_explainer.py [--only NAME ...] [--log PATH]

It proves that the guard tests for criteria W1-W8 really fail when the
behaviour they protect breaks. Seven behaviours are guarded, across four
files:

  1. The ``explained`` check in LogicController.start_help_online. The
     window is shown only when the caller has not already explained AND
     ai.help.explain_before_open is true. Each half is mutated separately,
     the default of that setting is mutated, and the command table's
     ``explained`` argument -- which carries the flag back from the GUI
     process -- is mutated too.
  2. The GUI process saves the setting only when the Assistant button was
     chosen AND the box is ticked. Cancel and the Escape key save nothing
     and open nothing, because HelpExplainerWindow emits assistant_chosen
     only from _on_assistant.
  3. The blank ai.help.assistant_url branch: the notice, and nothing else, with
     no window. The check itself is mutated, and separately the order of
     the two branches is MOVED so the window request comes first.
  4. The single-window guard in GuiManager._open_help_explainer.
  5. The strict ``command.get("explained") is True``. The command crosses a
     process boundary, so bool("false") being True was a real defect.
  6. The single INFO line naming the source, the explained value, and the
     outcome.
  7. The ``source`` argument travelling from the command into the log.

wh-assistant-explainer-once-more (stage S4) added three behaviours and
moved behaviour 2:

  2. (moved) The GUI now sends the box state in its one command, and
     main._record_help_explainer_choice in the Logic process decides and
     writes. The setting is saved only when its value changes, because every
     ConfigService.save drops the user's comments. The three GUI mutations of
     this behaviour were re-targeted to the same decisions in Logic.
  8. The marker file help_explainer_notebook_shown.toml: with the setting
     false and no marker, the window is shown once more; only the Assistant
     button writes the marker, never the showing, never a command without
     the box state; an existing marker is not rewritten.
  9. The start_ticked flag, from Logic through the GUI queue arm and
     GuiManager._open_help_explainer to HelpExplainerWindow.prepare_to_show.
 10. The command table's strict boolean check on do_not_show_again.

The review findings wh-assistant-explainer-once-more.1.1 and .1.2 added two
more and re-targeted the patterns their code moved:

 11. _record_help_explainer_choice saves the setting before it writes the
     marker; a save that returns False or raises (or has no state manager)
     writes no marker; a failed save or marker write returns a notice, and
     start_help_online shows it; a kept choice shows none.
 12. GuiManager._open_help_explainer resets the check box only when the
     window is not visible, so a second Help keeps an unsent choice.

Codex round 2 on .1.1 re-targeted behaviour 11's patterns to the staged
save and added mutations for it: the comparison reads the value on disk
(get_persisted, not get); the save is staged (save(values=...)), so a
failed replace leaves the live value alone and a second press still saves;
the save runs under StateManager's _gui_settings_lock, created the same
lazy way; and a state update follows the save.

wh-assistant-name-cleanup added one more:

 13. The address in ai.help.assistant_url opens as written. Two mutations
     bring back a substitution of the old Gem address and of a ChatGPT
     address by the notebook address.

Each mutation names the tests that must fail and the start of the assertion
text each must fail with, as pytest prints it in the short test summary. A
catch requires every named catcher to fail AT ITS OWN ASSERTION: a failure
whose message is not assertion-shaped is reported as an error, never as a
catch, because an exception upstream of the assertion proves nothing.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run this concurrently with a test suite or with edits in this
worktree: it rewrites main.py, gui.py, help_explainer_window.py, and
speech/actions.py while each mutation runs.

Line endings: all four target files are LF today (measured 2026-09-20,
crlf=0 for each). The runner still detects each file's own ending and
translates every pattern to it, because the mutation-gate skill records a
2026-08-27 run in which speech/actions.py was stored with CRLF and all four
multi-line patterns of another gate matched zero times. Detection costs
nothing and the next file may differ. The files are never normalised.

Two facts from the first run of this gate, kept because both would
otherwise be rediscovered the hard way:

  * pytest does NOT render every failed assertion the same way in the short
    summary. Some reasons carry an "AssertionError: " prefix and some do
    not, in the same file and the same test class. Every marker below was
    therefore read off a real failure rather than predicted. A first sweep
    with predicted markers produced ten "catcher failed at an unexpected
    assertion" errors, all of them the gate's fault and none the tests'.
  * One mutation (the-window-is-not-raised) died with a Windows fatal
    exception, pytest exit 3221227274, and a faulthandler stack dump inside
    _pytest.main.wrap_session. The gate reported it as an error and not as
    a verdict, the finally block restored gui.py, and `git status` was
    clean afterwards. It did not recur on the next sweep, where the same
    mutation was caught. Treat this exit code as the project's known
    intermittent pytest crash: re-run the one mutation with --only rather
    than reading anything into it.

Mutations left out on purpose, so a reader does not mistake the omission
for an oversight:

  * "the spoken command opens a browser of its own", the second half of
    criterion W4. The catcher assertion exists
    (test_it_hands_the_decision_to_the_logic_controller asserts
    mock_open.assert_not_called()), but a mutant that really calls
    webbrowser.open would launch a real browser from
    tests/test_ai/test_silent_actions.py::test_unconfigured_help_is_silent,
    which does not patch webbrowser. The first half of W4 is covered by
    the-spoken-command-does-not-name-itself.
  * The GUI queue's "open_help_explainer" dispatch arm in gui.py. The
    string appears three times in that file (the elif, the method name,
    and the call), so a pattern for the arm alone would need the
    surrounding arms to stay unique as they are edited. The Logic side of
    the same contract is mutated instead, in
    the-explainer-request-uses-another-action-name.
"""
# crewcut: the runner below is copied from
# tests/mutation_gate_audio_pause_notice.py, which copied it from
# tests/mutation_gate_bravia_error_envelope.py, extended here to four target
# files and to parametrized catcher names. A shared runner module imported by
# all three gates is the way to remove the duplication later.
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
MAIN = SERVICE / "main.py"
GUI = SERVICE / "gui.py"
WINDOW = SERVICE / "help_explainer_window.py"
ACTIONS = SERVICE / "speech" / "actions.py"
TARGETS = (MAIN, GUI, WINDOW, ACTIONS)

_VENV_PYTHON = SERVICE / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable))

LOGIC_TESTS = "tests/test_logic_open_help_online.py"
GUI_TESTS = "tests/test_gui_help_explainer.py"
WINDOW_TESTS = "tests/test_help_explainer_window.py"
ACTION_TESTS = "tests/test_ai/test_actions.py"
SILENT_TESTS = "tests/test_ai/test_silent_actions.py"
SELECTION = [LOGIC_TESTS, GUI_TESTS, WINDOW_TESTS, ACTION_TESTS, SILENT_TESTS]
RUN_TIMEOUT = 180  # A clean run of the selection takes about 2.5 seconds.

# --- catcher test names, so a rename breaks one line rather than many ---
SKIPS_WINDOW = "test_the_assistant_button_skips_the_window"
OPENS_ADDRESS = "test_it_opens_the_configured_address"
MISSING_SETTING = "test_a_missing_setting_shows_the_window"
ASKS_FOR_WINDOW = "test_it_asks_for_the_window_and_opens_no_browser"
FIELD_TRAVELS = "test_the_explained_field_travels_with_the_command"
TRUE_SKIPS = "test_the_boolean_true_still_skips_the_window"
CARRIES_SOURCE = "test_the_command_carries_the_source_it_was_given"
BLANK_SAYS_SO = "test_a_blank_address_opens_nothing_and_says_so"
BLANK_WHEN_EXPLAINED = "test_a_blank_address_gives_the_notice_when_explained_is_true"
LOG_WINDOW = "test_asking_for_the_window_is_logged_with_its_source"
LOG_BROWSER = "test_opening_the_browser_is_logged_with_its_source"
LOG_NOTICE = "test_the_unconfigured_notice_is_logged_with_its_source"
OLD_GEM_UNCHANGED = "test_the_old_gem_address_opens_unchanged"
OLD_CHATGPT_UNCHANGED = "test_the_old_chatgpt_address_opens_unchanged"

_NON_BOOL = "test_a_non_boolean_explained_field_takes_the_full_decision"
# Only these four ids fail under the bool() form. The other four cases
# (number-zero, none, empty-list, empty-dict) pass under BOTH forms, because
# bool() already answers False for them; listing them as catchers would make
# the mutation look caught for the wrong reason.
NON_BOOL_CATCHERS = [
    f"{_NON_BOOL}[string-false]",
    f"{_NON_BOOL}[string-true]",
    f"{_NON_BOOL}[string-True-capital]",
    f"{_NON_BOOL}[number-one]",
]

SECOND_RAISES = "test_a_second_request_raises_the_open_window"
BOX_CLEARED = "test_the_check_box_is_cleared_before_each_showing"
# wh-assistant-explainer-once-more: the GUI sends one command carrying the
# box state, and Logic writes the setting. The three GUI save tests of
# wh-assistant-button-explainer (assistant_asks_logic_to_open_the_page,
# the_checked_box_also_saves_the_setting, a_clear_box_saves_nothing) were
# replaced by this one parametrized test.
_SENDS_ONE = "test_assistant_sends_one_command_with_the_box_state"
SENDS_CLEAR = f"{_SENDS_ONE}[clear]"
SENDS_TICKED = f"{_SENDS_ONE}[ticked]"
FLAG_TICKS = "test_the_flag_ticks_the_box"
NO_FLAG_CLEAR = "test_no_flag_leaves_the_box_clear"
_ONLY_TRUE_TICKS = "test_only_the_boolean_true_ticks_the_box"
ONLY_TRUE_TICKS = [f"{_ONLY_TRUE_TICKS}[{i}]" for i in ("true", "1", "yes")]

# --- wh-assistant-explainer-once-more, Logic side ---
PRE_TICKED = "test_false_and_no_marker_shows_the_window_pre_ticked"
MARKER_OPENS = "test_false_and_the_marker_opens_the_browser"
_TRUE_NO_TICK = "test_true_shows_the_window_without_the_tick"
TRUE_NO_TICK_NO_MARKER = f"{_TRUE_NO_TICK}[no-marker]"
TRUE_NO_TICK_MARKER = f"{_TRUE_NO_TICK}[marker]"
SHOW_WRITES_NOTHING = "test_showing_the_window_writes_nothing"
TICKED_FALSE_NO_SAVE = "test_ticked_with_the_setting_false_saves_no_config"
TICKED_TRUE_SAVES = "test_ticked_with_the_setting_true_saves_false"
UNTICKED_FALSE_SAVES = "test_unticked_with_the_setting_false_saves_true"
UNTICKED_TRUE_NO_SAVE = "test_unticked_with_the_setting_true_saves_no_config"
RECORDED_UNCONFIGURED = "test_the_choice_is_recorded_when_help_is_not_configured"
_NO_BOX_STATE = "test_a_command_without_the_box_state_writes_nothing"
NO_BOX_STATE = [f"{_NO_BOX_STATE}[{i}]" for i in ("setting-false", "setting-true")]
MARKER_NOT_REWRITTEN = "test_an_existing_marker_is_not_rewritten"
BOX_STATE_REACHES = "test_the_box_state_reaches_start_help_online"
_REAL_BOOL = "test_only_a_real_boolean_is_passed_on"
# [None] is left out: None passes through unchanged under both forms.
REAL_BOOL_CATCHERS = [
    f"{_REAL_BOOL}[{i}]" for i in ("false", "true", "1", "0", "value5")
]
MARKER_FOLDER = "test_the_marker_lives_in_the_data_folder"
MISSING_SETTING_CHOICE = "test_a_missing_setting_counts_as_true_for_the_choice"

# --- wh-assistant-explainer-once-more.1.1: a failed write keeps the choice
# open and tells the user ---
SAVED_BEFORE_MARKER = "test_the_setting_is_saved_before_the_marker"
_FAILED_SAVE = "test_a_failed_save_writes_no_marker_and_says_so"
FAILED_SAVE = [
    f"{_FAILED_SAVE}[{case}-{outcome}]"
    for case in ("re-enable", "turn-off")
    for outcome in ("returns-false", "raises")
]
FAILED_SAVE_RAISES = [name for name in FAILED_SAVE if name.endswith("-raises]")]
WINDOW_COMES_BACK = "test_the_window_comes_back_after_a_failed_save"
MARKER_FAILS_REPORTED = "test_a_marker_that_cannot_be_written_is_reported"
MARKER_RAISES_REPORTED = "test_a_marker_writer_that_raises_is_reported"
NO_STATE_MANAGER = "test_no_state_manager_writes_no_marker"
_KEPT_NO_NOTICE = "test_a_kept_choice_shows_no_notice"
KEPT_NO_NOTICE = [
    f"{_KEPT_NO_NOTICE}[{i}]"
    for i in ("no-save-ticked", "save-false", "save-true", "no-save-clear")
]

# --- wh-assistant-explainer-once-more.1.1, round 2: the real ConfigService,
# a failed atomic replace, and a second press in the same process ---
_RETRY = "test_a_second_press_after_a_failed_save_keeps_the_choice"
RETRY = [f"{_RETRY}[{i}]" for i in ("re-enable", "turn-off")]
_WRITABLE = "test_a_press_after_the_file_is_writable_again_keeps_the_choice"
WRITABLE = [f"{_WRITABLE}[{i}]" for i in ("re-enable", "turn-off")]
_LIVE_ONLY = "test_a_live_value_that_never_reached_the_file_is_saved"
LIVE_ONLY_RE_ENABLE = f"{_LIVE_ONLY}[re-enable]"
LIVE_ONLY_TURN_OFF = f"{_LIVE_ONLY}[turn-off]"
SAVE_WAITS = "test_the_save_waits_for_a_gui_settings_write"
LOCK_CREATED = "test_the_lock_is_created_when_the_manager_has_none"
MATCHING_NOT_REWRITTEN = "test_a_matching_file_is_not_rewritten"
# wh-assistant-explainer-once-more.1.3: a second, opposite choice made
# while the first save is pending.
_PENDING = "test_a_later_opposite_choice_waits_for_a_pending_save"
PENDING = [f"{_PENDING}[{i}]" for i in ("tick-then-clear", "clear-then-tick")]
PENDING_LOST = "AssertionError: assert [{'values'"

# --- wh-assistant-explainer-once-more.1.2: a second Help keeps the choice
# in a visible window ---
CLEARED_STAYS = "test_a_cleared_once_more_box_stays_cleared"
TICKED_STAYS = "test_a_ticked_clear_start_box_stays_ticked"
_HIDDEN_RESET = "test_a_hidden_window_is_reset_when_shown_again"
HIDDEN_RESET = [f"{_HIDDEN_RESET}[{i}]" for i in ("clear-start", "once-more")]
VISIBLE_RAISED = "test_a_visible_window_is_still_raised_and_activated"

# --- wh-assistant-explainer-once-more, window side ---
CAN_START_TICKED = "test_the_box_can_start_ticked"
PRE_TICKED_REPORTS = "test_a_pre_ticked_box_reports_true_and_can_be_cleared"
CLEARS_AGAIN = "test_the_box_clears_again_for_a_second_showing"
PRE_TICK_NO_TRAVEL = "test_a_pre_ticked_box_does_not_travel_to_the_next_showing"

CANCEL_SILENT = "test_cancel_reports_nothing_and_closes"
CANCEL_SILENT_TICKED = "test_cancel_reports_nothing_even_when_the_box_is_checked"
ESCAPE_SILENT = "test_escape_reports_nothing_and_closes"
REPORTS_TICKED = "test_assistant_reports_the_checked_box"

HANDS_OVER = "test_it_hands_the_decision_to_the_logic_controller"

# Assertion-text markers, every one MEASURED from a run of this gate rather
# than predicted. pytest prefixes "AssertionError: " to a rewritten assert
# whose explanation runs past one line and omits it otherwise, so which form
# a given assertion produces cannot be reasoned about and has to be read off
# a real failure.
#
# Where pytest's assertion rewriting truncates a long dict or list repr with
# an ellipsis, the marker stops before the truncation: the ellipsis position
# depends on the repr length, which an edit to an unrelated field of the same
# dict would move. The global non-assertion check in _judge is what rules out
# an upstream exception for those catchers, and the marker still proves the
# failing comparison was the intended one.
NO_CALL = "AssertionError: expected call not found"
NO_AWAIT = "AssertionError: expected await not found"
NOT_CALLED = "AssertionError: Expected 'open' to not have been called"
CALLED_ZERO = "AssertionError: Expected 'open' to be called once. Called 0 times."
QUEUED_LIST = "AssertionError: assert [{'action': '"
# The Logic tests' settings stand-in is the AsyncMock config.save (the
# staged save since round 2 of wh-assistant-explainer-once-more.1.1).
# assert_awaited_once_with on a mock never awaited reports the await count,
# not a mismatch, so it has its own marker.
SAVE_NOT_EXPECTED = (
    "AssertionError: Expected 'save' to not have been called. "
    "Called 1 times."
)
SAVE_NEVER_AWAITED = (
    "AssertionError: Expected save to have been awaited once. "
    "Awaited 0 times."
)

# wh-assistant-explainer-once-more.1.1 and .1.2, measured 2026-09-28.
NOTICE_MISSING = "AssertionError: assert [] == [{'action': '"
MARKER_PRESENT = "AssertionError: assert not True"
NOT_OPENED = "AssertionError: Expected 'open' to not have been called. Called 1 times."
PREPARED_TWICE = (
    "AssertionError: Expected 'prepare_to_show' to be called once. "
    "Called 2 times."
)
PREPARED_NEVER = (
    "AssertionError: Expected 'prepare_to_show' to have been called once. "
    "Called 0 times."
)

# The command table's explained/source arguments, as one unique substring.
TABLE_ARGS = (
    'explained=command.get("explained") is True, '
    'source=str(command.get("source", "menu"))'
)

# The two branches whose ORDER the move mutation swaps.
BLANK_BRANCH = (
    '        assistant_url = config.get("ai.help.assistant_url", "")\n'
    "        if not assistant_url:\n"
    "            # Blanking the setting is how a user turns online help off, so\n"
    "            # this is a plain statement of fact rather than an error. The\n"
    "            # explanation window stays shut: its one button would have\n"
    "            # nothing to open. This check comes first on purpose: the\n"
    "            # window is modeless, so the address can be blanked while it is\n"
    "            # open, and the Assistant button must then say so rather than\n"
    "            # open an empty page.\n"
    "            logger.info(\n"
    '                "Help: source=%s explained=%s, online help is not configured.",\n'
    "                source,\n"
    "                explained,\n"
    "            )\n"
    "            self._send_gui_notification(\n"
    '                "Online help is not configured. Set assistant_url under [ai.help]."\n'
    "            )\n"
    "            return\n"
)
WINDOW_BRANCH = (
    "        if not explained:\n"
    '            explain = bool(config.get("ai.help.explain_before_open", True))\n'
    "            once_more = False\n"
    "            if not explain:\n"
    "                # Setting false: the window is shown once more while the\n"
    "                # marker is missing, with the box ticked to match the\n"
    "                # user's earlier choice. load_hint_shown never raises, and\n"
    "                # treats an unreadable marker as present.\n"
    "                from services.wheelhouse.click_first_use_hint import (\n"
    "                    load_hint_shown,\n"
    "                )\n"
    "                once_more = not await asyncio.to_thread(\n"
    "                    load_hint_shown, default_help_explainer_marker_path()\n"
    "                )\n"
    "            if explain or once_more:\n"
    "                logger.info(\n"
    '                    "Help: source=%s explained=%s, asking for the explanation "\n'
    '                    "window.",\n'
    "                    source,\n"
    "                    explained,\n"
    "                )\n"
    "                self._request_help_explainer(start_ticked=once_more)\n"
    "                return\n"
)

# The Assistant button's record step, whose place before the blank-address
# check the second move mutation tests (wh-assistant-explainer-once-more).
RECORD_BLOCK = (
    "        if explained and do_not_show_again is not None:\n"
    "            # Before the blank-address check: the window is modeless, so the\n"
    "            # address can be blanked while it is open, and the choice made\n"
    "            # in the window is still the user's to keep.\n"
    "            notice = await _record_help_explainer_choice(\n"
    '                config, getattr(self, "state_manager", None), do_not_show_again\n'
    "            )\n"
    "            if notice:\n"
    "                self._send_gui_notification(notice)\n"
)

WINDOW_REQUEST = "                self._request_help_explainer(start_ticked=once_more)\n"

# The save call inside _record_help_explainer_choice. Re-indented by
# wh-assistant-explainer-once-more.1.1, which put it inside the save step,
# and replaced in round 2 by the staged save under the GUI settings lock.
SAVE_CALL = "                        saved = bool(await config.save(values={key: wanted}))\n"
# Round 2: the lock StateManager._save_gui_settings holds, created the same
# lazy way, and the state update after the save.
LOCK_CREATE = (
    '            if not hasattr(state_manager, "_gui_settings_lock"):\n'
    "                state_manager._gui_settings_lock = asyncio.Lock()\n"
)
LOCK_HELD = "            async with state_manager._gui_settings_lock:\n"
STATE_UPDATE = "                    state_manager.send_state_update()\n"
# wh-assistant-explainer-once-more.1.3: the value on disk is read in two
# places. With a state manager it is read under the lock, so a choice made
# while an earlier save is pending waits for that save; without one there
# is no save to wait for.
LOCKED_READ = "                if bool(config.get_persisted(key, True)) == wanted:\n"
NO_MANAGER_READ = "        saved = bool(config.get_persisted(key, True)) == wanted\n"
SAVED_FALSE = "    saved = False\n"
NOT_SAVED_RETURN = (
    "        # the window again and the user can choose again.\n"
    "        return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
)

# wh-assistant-explainer-once-more.1.1: the two steps of
# _record_help_explainer_choice, in their required order. The move mutation
# puts the marker step first, which is the order the finding reported.
# Rebuilt for .1.3, which moved the comparison under the lock.
SAVE_STEP = (
    '    key = "ai.help.explain_before_open"\n'
    "    wanted = not do_not_show_again\n"
    + SAVED_FALSE
    + "    if state_manager is None:\n"
    + NO_MANAGER_READ
    + "        if not saved:\n"
    "            logger.warning(\n"
    '                "Help: no state manager, the window setting was not saved."\n'
    "            )\n"
    "    else:\n"
    "        try:\n"
    "            # The staged save StateManager._save_gui_settings makes for a\n"
    "            # GUI settings write, under the same lock, without its request\n"
    "            # ID: no GUI request waits for an acknowledgement here, so the\n"
    "            # notice returned below takes its place. The comparison is made\n"
    "            # under the lock too: the window hides at once, so a second\n"
    "            # choice can arrive while an earlier save is pending, and until\n"
    "            # that save ends the value on disk is the old one\n"
    "            # (wh-assistant-explainer-once-more.1.3).\n"
    + LOCK_CREATE
    + LOCK_HELD
    + LOCKED_READ
    + "                    saved = True\n"
    "                else:\n"
    "                    try:\n"
    + SAVE_CALL
    + "                        if not saved:\n"
    "                            logger.warning(\n"
    '                                "Help: the window setting was not saved to disk."\n'
    "                            )\n"
    "                    except Exception as exc:\n"
    "                        logger.warning(\n"
    '                            "Help: the window setting could not be saved: %s", exc\n'
    "                        )\n"
    + STATE_UPDATE
    + "        except Exception as exc:\n"
    '            logger.warning("Help: the window setting save did not finish: %s", exc)\n'
    "    if not saved:\n"
    "        # No marker: the choice is not complete, so the next Help shows\n"
    + NOT_SAVED_RETURN
)
MARKER_NOT_WRITTEN = (
    "            if not await asyncio.to_thread(mark_hint_shown, marker):\n"
    '                logger.warning("Help: the explanation window marker was not written.")\n'
    "                return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
)
MARKER_RAISED = (
    "    except Exception as exc:\n"
    '        logger.warning("Help: the explanation window marker failed: %s", exc)\n'
    "        return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
)
MARKER_STEP = (
    "    try:\n"
    "        marker = default_help_explainer_marker_path()\n"
    "        if not await asyncio.to_thread(load_hint_shown, marker):\n"
    + MARKER_NOT_WRITTEN
    + MARKER_RAISED
)
# The caller in start_help_online that shows the returned notice.
NOTICE_SENT = (
    "            if notice:\n"
    "                self._send_gui_notification(notice)\n"
)
# The GUI guard of wh-assistant-explainer-once-more.1.2.
VISIBLE_GUARD = "        if not self._help_explainer.isVisible():\n"

# The table's do_not_show_again argument: only a real boolean is a choice.
TABLE_BOX_ARG = (
    'do_not_show_again=command.get("do_not_show_again") '
    'if isinstance(command.get("do_not_show_again"), bool) else None'
)

GUI_BOX_FIELD = '            "do_not_show_again": bool(do_not_show_again),\n        })\n'

BROWSER_LOG = (
    "        logger.info(\n"
    '            "Help: source=%s explained=%s, opening the browser.",\n'
    "            source,\n"
    "            explained,\n"
    "        )\n"
    "        try:\n"
)

ON_ASSISTANT = (
    "    def _on_assistant(self) -> None:\n"
    "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())\n"
    "        self.accept()\n"
)

MUTATIONS = [
    # ---------------------------------------------------------------
    # Behaviour 1: the explained check, and the flag that feeds it.
    # ---------------------------------------------------------------
    {
        # Without the explained half the window answers its own Assistant
        # button and the browser never opens.
        # Re-targeted for wh-assistant-explainer-once-more: the explained
        # check and the config read are separate lines now.
        "name": "the-explained-half-is-dropped",
        "file": MAIN,
        "old": "        if not explained:\n",
        "new": "        if True:\n",
        "catchers": {SKIPS_WINDOW: CALLED_ZERO},
    },
    {
        # Without the config read the setting does nothing: the window
        # appears however ai.help.explain_before_open is set.
        "name": "the-config-read-is-dropped",
        "file": MAIN,
        "old": 'explain = bool(config.get("ai.help.explain_before_open", True))',
        "new": "explain = True",
        "catchers": {OPENS_ADDRESS: CALLED_ZERO},
    },
    {
        # W3: an absent key must read as true, so a user who has never
        # touched the setting still gets the explanation. The autouse
        # marker fixture writes the marker, so the once-more rule cannot
        # show the window in place of the default.
        "name": "the-default-becomes-false",
        "file": MAIN,
        "old": '            explain = bool(config.get("ai.help.explain_before_open", True))',
        "new": '            explain = bool(config.get("ai.help.explain_before_open", False))',
        "catchers": {MISSING_SETTING: NOT_CALLED},
    },
    {
        # The command table stops carrying the flag back from the GUI
        # process, so the Assistant button produces the window again.
        "name": "the-explained-field-is-ignored",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": 'explained=False, source=str(command.get("source", "menu"))',
        "catchers": {
            FIELD_TRAVELS: NO_CALL,
            TRUE_SKIPS: "assert False is True",
        },
    },
    {
        # Behaviour 5, the boss ruling's defect. bool("false") is True, so
        # under this form any non-empty string skips the window.
        "name": "the-strict-check-becomes-bool",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": (
            'explained=bool(command.get("explained", False)), '
            'source=str(command.get("source", "menu"))'
        ),
        "catchers": {name: "assert True is False" for name in NON_BOOL_CATCHERS},
    },
    # ---------------------------------------------------------------
    # Behaviour 7: the source argument.
    # ---------------------------------------------------------------
    {
        # Every run then claims the menu, so the log cannot tell the
        # Assistant button from the menu entry or the spoken command.
        "name": "the-source-is-fixed-to-menu",
        "file": MAIN,
        "old": TABLE_ARGS,
        "new": 'explained=command.get("explained") is True, source="menu"',
        "catchers": {CARRIES_SOURCE: "AssertionError: assert 'menu' == 'window'"},
    },
    # ---------------------------------------------------------------
    # Behaviour 3: the blank assistant_url branch.
    # ---------------------------------------------------------------
    {
        # A blank address no longer stops the run, so the window opens on
        # an address its one button could not use.
        "name": "the-blank-check-is-dropped",
        "file": MAIN,
        "old": "        if not assistant_url:",
        "new": "        if False:",
        "catchers": {
            BLANK_SAYS_SO: (
                "AssertionError: assert 'open_help_explainer' "
                "== 'show_notification'"
            ),
            BLANK_WHEN_EXPLAINED: NOT_CALLED,
            LOG_NOTICE: "AssertionError: assert 'not configured' in",
        },
    },
    {
        # A MOVE, not a copy: the window branch is deleted from its place
        # and inserted above the blank-address branch. A copy would leave
        # the original in place and the order assertion would still pass.
        "name": "the-window-request-comes-before-the-blank-check",
        "file": MAIN,
        "old": BLANK_BRANCH + "\n" + WINDOW_BRANCH,
        "new": WINDOW_BRANCH + "\n" + BLANK_BRANCH,
        "catchers": {
            BLANK_SAYS_SO: (
                "AssertionError: assert 'open_help_explainer' "
                "== 'show_notification'"
            ),
            LOG_NOTICE: "AssertionError: assert 'not configured' in",
        },
    },
    {
        # The Logic side of the cross-process contract: the GUI process
        # matches this exact action name and nothing else.
        # Re-targeted: the message is built before the put since
        # wh-assistant-explainer-once-more.
        "name": "the-explainer-request-uses-another-action-name",
        "file": MAIN,
        "old": 'message = {"action": "open_help_explainer"}',
        "new": 'message = {"action": "open_help_explainer_disabled"}',
        "catchers": {
            ASKS_FOR_WINDOW: QUEUED_LIST,
            MISSING_SETTING: QUEUED_LIST,
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 6: one INFO line per run, naming the path taken.
    # ---------------------------------------------------------------
    {
        # The window branch claims the browser opened. One line still, but
        # it names the wrong outcome.
        # Re-targeted: the format string is split over two lines since
        # wh-assistant-explainer-once-more.
        "name": "the-window-branch-logs-the-browser-text",
        "file": MAIN,
        "old": (
            '                    "Help: source=%s explained=%s, asking for the explanation "\n'
            '                    "window.",\n'
        ),
        "new": '                    "Help: source=%s explained=%s, opening the browser.",\n',
        "catchers": {
            LOG_WINDOW: "AssertionError: assert 'explanation window' in",
        },
    },
    {
        # The browser branch logs nothing at all, so the one path a user
        # reaches most often leaves no trace.
        "name": "the-browser-branch-logs-nothing",
        "file": MAIN,
        "old": BROWSER_LOG,
        "new": "        try:\n",
        "catchers": {LOG_BROWSER: "assert 0 == 1"},
    },
    # ---------------------------------------------------------------
    # Behaviour 4: the single-window guard.
    # ---------------------------------------------------------------
    {
        # A second Help builds a second window instead of raising the one
        # already on screen.
        "name": "a-new-window-every-time",
        "file": GUI,
        "old": "        if getattr(self, '_help_explainer', None) is None:",
        "new": "        if True:",
        "catchers": {
            SECOND_RAISES: "AssertionError: Expected 'HelpExplainerWindow' to have been called once",
        },
    },
    {
        # The window is never brought to the front, so a second Help looks
        # like nothing happened.
        "name": "the-window-is-not-raised",
        "file": GUI,
        "old": "        self._help_explainer.raise_()\n",
        "new": "        pass\n",
        "catchers": {SECOND_RAISES: "AssertionError: assert 0 == 2"},
    },
    {
        "name": "the-window-is-not-activated",
        "file": GUI,
        "old": "        self._help_explainer.activateWindow()\n",
        "new": "        pass\n",
        "catchers": {SECOND_RAISES: "AssertionError: assert 0 == 2"},
    },
    {
        # The same instance comes back, so a box ticked in an earlier
        # reading would travel with a later Assistant press.
        # Re-targeted: the call carries start_ticked since
        # wh-assistant-explainer-once-more, and sits under the visible-window
        # guard since .1.2, where a hidden window shown again must still be
        # reset (HIDDEN_RESET).
        "name": "the-check-box-is-not-cleared-before-showing",
        "file": GUI,
        "old": "            self._help_explainer.prepare_to_show(start_ticked=start_ticked)\n",
        "new": "            pass\n",
        "catchers": {
            BOX_CLEARED: "AssertionError: Expected 'prepare_to_show' to have been called once",
            HIDDEN_RESET[0]: "assert True is False",
            HIDDEN_RESET[1]: "assert False is True",
        },
    },
    {
        # wh-assistant-explainer-once-more: the flag stops at the GUI
        # manager, so the once-more showing starts with the box clear.
        "name": "the-gui-drops-the-tick-before-the-window",
        "file": GUI,
        "old": "            self._help_explainer.prepare_to_show(start_ticked=start_ticked)\n",
        "new": "            self._help_explainer.prepare_to_show(start_ticked=False)\n",
        "catchers": {FLAG_TICKS: "AssertionError: expected call not found"},
    },
    {
        # The queue arm drops the flag from the Logic message.
        "name": "the-gui-queue-arm-drops-the-tick",
        "file": GUI,
        "old": 'start_ticked=message.get("start_ticked") is True',
        "new": "start_ticked=False",
        "catchers": {FLAG_TICKS: "AssertionError: expected call not found"},
    },
    {
        # The queue arm inverts the flag.
        "name": "the-gui-queue-arm-inverts-the-tick",
        "file": GUI,
        "old": 'start_ticked=message.get("start_ticked") is True',
        "new": 'start_ticked=message.get("start_ticked") is not True',
        "catchers": {
            FLAG_TICKS: "AssertionError: expected call not found",
            NO_FLAG_CLEAR: "AssertionError: expected call not found",
        },
    },
    {
        # The strict check becomes truthiness: the flag crosses a process
        # boundary, so bool("yes") would tick the box.
        "name": "the-gui-tick-check-becomes-bool",
        "file": GUI,
        "old": 'start_ticked=message.get("start_ticked") is True',
        "new": 'start_ticked=bool(message.get("start_ticked"))',
        "catchers": {
            name: "AssertionError: expected call not found"
            for name in ONLY_TRUE_TICKS
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 2: save only when Assistant was chosen, and only when the
    # value changes. Since wh-assistant-explainer-once-more the GUI sends
    # the box state and Logic's _record_help_explainer_choice decides and
    # writes, so the three GUI mutations of this behaviour are re-targeted
    # to the same decisions in Logic. Every ConfigService.save drops the
    # user's comments, which is why "no save when nothing changes" is
    # itself guarded.
    # ---------------------------------------------------------------
    {
        # Re-targeted: the equality early return is gone, so the setting is
        # written on every Assistant press, even when it already holds the
        # wanted value. Re-targeted again by .1.1: the condition now
        # guards the save step instead of returning early. Re-targeted
        # again by .1.3: the comparison is now made under the lock.
        "name": "the-tick-is-ignored-and-the-setting-always-saves",
        "file": MAIN,
        "old": LOCKED_READ,
        "new": "                if False:\n",
        "catchers": {
            TICKED_FALSE_NO_SAVE: SAVE_NOT_EXPECTED,
            UNTICKED_TRUE_NO_SAVE: SAVE_NOT_EXPECTED,
            MATCHING_NOT_REWRITTEN: "AssertionError: assert b",
        },
    },
    {
        # Re-targeted: the save call is replaced by a claimed success, so
        # the check box never changes the setting.
        "name": "the-tick-never-saves-the-setting",
        "file": MAIN,
        "old": SAVE_CALL,
        "new": "                        saved = True\n",
        "catchers": {
            TICKED_TRUE_SAVES: SAVE_NEVER_AWAITED,
            UNTICKED_FALSE_SAVES: SAVE_NEVER_AWAITED,
            RECORDED_UNCONFIGURED: SAVE_NEVER_AWAITED,
        },
    },
    {
        # Re-targeted: the saved value is always true, so ticking the box
        # turns the explanation ON rather than off.
        "name": "the-saved-value-becomes-true",
        "file": MAIN,
        "old": "values={key: wanted}",
        "new": "values={key: True}",
        "catchers": {
            TICKED_TRUE_SAVES: "AssertionError: expected await not found",
            RECORDED_UNCONFIGURED: "AssertionError: expected await not found",
        },
    },
    {
        # The wanted value is inverted: the box means the opposite.
        "name": "the-wanted-value-is-inverted",
        "file": MAIN,
        "old": "    wanted = not do_not_show_again\n",
        "new": "    wanted = do_not_show_again\n",
        "catchers": {
            TICKED_FALSE_NO_SAVE: SAVE_NOT_EXPECTED,
            TICKED_TRUE_SAVES: SAVE_NEVER_AWAITED,
            UNTICKED_FALSE_SAVES: SAVE_NEVER_AWAITED,
            UNTICKED_TRUE_NO_SAVE: SAVE_NOT_EXPECTED,
        },
    },
    {
        # The tick is ignored and every press means "do not show again": an
        # unticked box with the setting false leaves it false, so the
        # window never returns although the user cleared the box.
        "name": "an-unticked-box-leaves-the-setting-false",
        "file": MAIN,
        "old": "    wanted = not do_not_show_again\n",
        "new": "    wanted = False\n",
        "catchers": {
            UNTICKED_FALSE_SAVES: SAVE_NEVER_AWAITED,
        },
    },
    {
        # The no-save rule broken for the ticked case only: a ticked box
        # with the setting already false rewrites the settings file, and
        # the user's comments are lost for nothing.
        "name": "ticked-with-the-setting-false-saves-anyway",
        "file": MAIN,
        "old": LOCKED_READ,
        "new": LOCKED_READ.replace(":\n", " and not do_not_show_again:\n"),
        "catchers": {
            TICKED_FALSE_NO_SAVE: SAVE_NOT_EXPECTED,
        },
    },
    {
        # The no-save rule broken for the unticked case only.
        "name": "unticked-with-the-setting-true-saves-anyway",
        "file": MAIN,
        "old": LOCKED_READ,
        "new": LOCKED_READ.replace(":\n", " and do_not_show_again:\n"),
        "catchers": {
            UNTICKED_TRUE_NO_SAVE: SAVE_NOT_EXPECTED,
        },
    },
    {
        # The choice reads a missing setting as false, although the window
        # itself reads it as true (W3). An unticked press by a user who
        # never touched the setting then rewrites the settings file.
        "name": "the-choice-reads-a-missing-setting-as-false",
        "file": MAIN,
        "old": LOCKED_READ,
        "new": LOCKED_READ.replace("(key, True)", "(key, False)"),
        "catchers": {
            MISSING_SETTING_CHOICE: SAVE_NOT_EXPECTED,
        },
    },
    {
        # The Assistant button's command claims it came from the menu.
        # Catchers re-named: the GUI save tests became one parametrized
        # test in wh-assistant-explainer-once-more.
        "name": "the-window-does-not-name-itself-as-the-source",
        "file": GUI,
        "old": '            "source": "window",',
        "new": '            "source": "menu",',
        "catchers": {SENDS_CLEAR: QUEUED_LIST, SENDS_TICKED: QUEUED_LIST},
    },
    {
        # The box state is left out of the command, so Logic can never
        # record the choice.
        "name": "the-gui-drops-the-box-state",
        "file": GUI,
        "old": GUI_BOX_FIELD,
        "new": "        })\n",
        "catchers": {SENDS_CLEAR: QUEUED_LIST, SENDS_TICKED: QUEUED_LIST},
    },
    {
        # The box state is sent as always ticked.
        "name": "the-gui-always-sends-ticked",
        "file": GUI,
        "old": GUI_BOX_FIELD,
        "new": '            "do_not_show_again": True,\n        })\n',
        "catchers": {SENDS_CLEAR: QUEUED_LIST},
    },
    {
        # The old design comes back: the GUI writes the setting itself, a
        # second command beside the one Logic now acts on.
        "name": "the-gui-writes-the-setting-itself",
        "file": GUI,
        "old": GUI_BOX_FIELD,
        "new": (
            GUI_BOX_FIELD
            + "        if do_not_show_again:\n"
            + "            self.send_command({\n"
            + "                'action': 'set_config_value',\n"
            + "                'key': 'ai.help.explain_before_open',\n"
            + "                'value': False,\n"
            + "            })\n"
        ),
        "catchers": {SENDS_TICKED: QUEUED_LIST},
    },
    # ---------------------------------------------------------------
    # Behaviour 8 (wh-assistant-explainer-once-more): the marker file and
    # the once-more showing.
    # ---------------------------------------------------------------
    {
        # The marker check is dropped for a false setting: the window is
        # never shown once more.
        "name": "the-once-more-check-is-dropped",
        "file": MAIN,
        "old": "            if not explain:\n",
        "new": "            if False:\n",
        "catchers": {PRE_TICKED: NOT_CALLED},
    },
    {
        # The marker check is inverted: the window is shown to a user who
        # has already chosen since the move, and not to one who has not.
        "name": "the-once-more-check-is-inverted",
        "file": MAIN,
        "old": "once_more = not await asyncio.to_thread(",
        "new": "once_more = await asyncio.to_thread(",
        "catchers": {PRE_TICKED: NOT_CALLED, MARKER_OPENS: CALLED_ZERO},
    },
    {
        # The window is always shown for a false setting, marker or not.
        "name": "the-window-is-always-shown",
        "file": MAIN,
        "old": "            if explain or once_more:\n",
        "new": "            if True:\n",
        "catchers": {MARKER_OPENS: CALLED_ZERO},
    },
    {
        # The marker path names the click hint's file, which many users
        # already hold, so the once-more showing never happens for them.
        "name": "the-marker-reuses-the-click-hint-file",
        "file": MAIN,
        "old": '/ "help_explainer_notebook_shown.toml"',
        "new": '/ "click_first_use_hint_shown.toml"',
        "catchers": {MARKER_FOLDER: "AssertionError: assert WindowsPath("},
    },
    {
        # The marker is never written, so the window comes back at every
        # Help for a user whose setting is false.
        "name": "the-marker-is-never-written",
        "file": MAIN,
        "old": MARKER_NOT_WRITTEN,
        "new": "            pass\n",
        "catchers": {
            TICKED_FALSE_NO_SAVE: "AssertionError: assert False is True",
            TICKED_TRUE_SAVES: "AssertionError: assert False is True",
            UNTICKED_FALSE_SAVES: "AssertionError: assert False is True",
            UNTICKED_TRUE_NO_SAVE: "AssertionError: assert False is True",
            RECORDED_UNCONFIGURED: "AssertionError: assert False is True",
        },
    },
    {
        # An existing marker is written again on every press.
        "name": "an-existing-marker-is-rewritten",
        "file": MAIN,
        "old": "        if not await asyncio.to_thread(load_hint_shown, marker):\n",
        "new": "        if True:\n",
        "catchers": {MARKER_NOT_REWRITTEN: "assert b'shown = tru"},
    },
    {
        # The record step is skipped altogether: neither marker nor setting.
        "name": "the-choice-is-never-recorded",
        "file": MAIN,
        "old": "        if explained and do_not_show_again is not None:\n",
        "new": "        if False:\n",
        "catchers": {
            TICKED_FALSE_NO_SAVE: "AssertionError: assert False is True",
            TICKED_TRUE_SAVES: "AssertionError: assert False is True",
            UNTICKED_FALSE_SAVES: "AssertionError: assert False is True",
            UNTICKED_TRUE_NO_SAVE: "AssertionError: assert False is True",
            RECORDED_UNCONFIGURED: "AssertionError: assert False is True",
        },
    },
    {
        # A command without the box state records a choice anyway: None
        # reads as an unticked box. This is the mutation for the guard
        # test_a_command_without_the_box_state_writes_nothing, which
        # passed before the implementation.
        "name": "a-command-without-the-box-state-records-a-choice",
        "file": MAIN,
        "old": "        if explained and do_not_show_again is not None:\n",
        "new": "        if explained:\n",
        "catchers": {name: "AssertionError: assert not True" for name in NO_BOX_STATE},
    },
    {
        # A MOVE: the record step goes after the blank-address check, so a
        # choice made while the address was blanked is lost.
        "name": "the-choice-is-recorded-after-the-blank-check",
        "file": MAIN,
        "old": RECORD_BLOCK + "\n" + BLANK_BRANCH,
        "new": BLANK_BRANCH + "\n" + RECORD_BLOCK,
        "catchers": {RECORDED_UNCONFIGURED: "AssertionError: assert False is True"},
    },
    {
        # Showing the window uses up the once-more showing: the marker is
        # written when the window is requested, so Cancel ends it too. The
        # mutation for the guard test_showing_the_window_writes_nothing,
        # which passed before the implementation. __import__ binds no
        # name, so the function's own import is untouched.
        "name": "showing-the-window-writes-the-marker",
        "file": MAIN,
        "old": WINDOW_REQUEST,
        "new": (
            '                __import__("services.wheelhouse.click_first_use_hint",'
            ' fromlist=["mark_hint_shown"]).mark_hint_shown('
            "default_help_explainer_marker_path())\n" + WINDOW_REQUEST
        ),
        "catchers": {SHOW_WRITES_NOTHING: "AssertionError: assert not True"},
    },
    {
        # Showing the window saves the setting. Aimed at the second
        # assertion of the same guard: the marker stays unwritten, so the
        # test reaches the settings check.
        "name": "showing-the-window-saves-the-setting",
        "file": MAIN,
        "old": WINDOW_REQUEST,
        "new": (
            "                await config.save(\n"
            '                    values={"ai.help.explain_before_open": True}\n'
            "                )\n" + WINDOW_REQUEST
        ),
        "catchers": {
            SHOW_WRITES_NOTHING: SAVE_NOT_EXPECTED,
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 11 (wh-assistant-explainer-once-more.1.1): the setting is
    # saved before the marker is written, a failed save writes no marker,
    # and every failed write shows the user a notice.
    # ---------------------------------------------------------------
    {
        # A MOVE: the marker step goes before the save step, the order the
        # finding reported. A failed save then leaves the marker written.
        "name": "the-marker-is-written-before-the-save",
        "file": MAIN,
        "old": SAVE_STEP + "\n" + MARKER_STEP,
        "new": MARKER_STEP + "\n" + SAVE_STEP,
        "catchers": {
            SAVED_BEFORE_MARKER: "assert [True] == [False]",
            **{name: MARKER_PRESENT for name in FAILED_SAVE},
            WINDOW_COMES_BACK: NOT_OPENED,
        },
    },
    {
        # A failed save goes on to write the marker (and shows no notice).
        "name": "a-failed-save-still-writes-the-marker",
        "file": MAIN,
        "old": "    if not saved:\n        # No marker",
        "new": "    if False:\n        # No marker",
        "catchers": {
            **{name: MARKER_PRESENT for name in FAILED_SAVE},
            WINDOW_COMES_BACK: NOT_OPENED,
            NO_STATE_MANAGER: MARKER_PRESENT,
        },
    },
    {
        # A missing state manager counts as a saved setting. Re-targeted by
        # .1.3: that path now compares the value on disk itself.
        "name": "no-state-manager-counts-as-saved",
        "file": MAIN,
        "old": NO_MANAGER_READ,
        "new": "        saved = True\n",
        "catchers": {NO_STATE_MANAGER: MARKER_PRESENT},
    },
    {
        # A save that raises counts as saved. Split from the mutation above
        # by .1.3: the start value no longer reaches the no-manager path.
        "name": "a-raised-save-counts-as-saved",
        "file": MAIN,
        "old": SAVED_FALSE,
        "new": "    saved = True\n",
        "catchers": {name: MARKER_PRESENT for name in FAILED_SAVE_RAISES},
    },
    {
        # A failed save writes no marker but tells the user nothing.
        "name": "a-failed-save-shows-no-notice",
        "file": MAIN,
        "old": NOT_SAVED_RETURN,
        "new": NOT_SAVED_RETURN.replace(
            "return _HELP_CHOICE_NOT_SAVED_NOTICE", "return None"
        ),
        "catchers": {
            **{name: NOTICE_MISSING for name in FAILED_SAVE},
            NO_STATE_MANAGER: "AssertionError: assert None == 'Wheelhouse could not",
        },
    },
    {
        # A marker the writer reports as not written tells the user nothing.
        "name": "a-marker-write-failure-shows-no-notice",
        "file": MAIN,
        "old": MARKER_NOT_WRITTEN,
        "new": MARKER_NOT_WRITTEN.replace(
            "return _HELP_CHOICE_NOT_SAVED_NOTICE", "return None"
        ),
        "catchers": {MARKER_FAILS_REPORTED: NOTICE_MISSING},
    },
    {
        # A marker writer that raises tells the user nothing.
        "name": "a-marker-writer-exception-shows-no-notice",
        "file": MAIN,
        "old": MARKER_RAISED,
        "new": MARKER_RAISED.replace(
            "return _HELP_CHOICE_NOT_SAVED_NOTICE", "return None"
        ),
        "catchers": {MARKER_RAISES_REPORTED: NOTICE_MISSING},
    },
    {
        # The caller drops the returned notice, so no failure reaches the
        # user.
        "name": "start-help-online-drops-the-notice",
        "file": MAIN,
        "old": NOTICE_SENT,
        "new": "            pass\n",
        "catchers": {
            **{name: NOTICE_MISSING for name in FAILED_SAVE},
            MARKER_FAILS_REPORTED: NOTICE_MISSING,
            MARKER_RAISES_REPORTED: NOTICE_MISSING,
        },
    },
    {
        # A kept choice also shows the failure notice.
        "name": "a-kept-choice-shows-the-notice",
        "file": MAIN,
        "old": (
            "        return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
            "    return None\n"
        ),
        "new": (
            "        return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
            "    return _HELP_CHOICE_NOT_SAVED_NOTICE\n"
        ),
        "catchers": {name: QUEUED_LIST for name in KEPT_NO_NOTICE},
    },
    # ---------------------------------------------------------------
    # Behaviour 11, round 2 (Codex on wh-assistant-explainer-once-more.1.1):
    # the comparison reads the value on disk, the save is staged so a
    # failure leaves the live value alone, it runs under the GUI settings
    # lock, and a state update follows it. Catchers use the real
    # ConfigService with a failed atomic replace of the settings file.
    # ---------------------------------------------------------------
    {
        # The comparison reads the live value: a live value that matches
        # the choice but never reached the file skips the save.
        "name": "the-choice-reads-the-live-value",
        "file": MAIN,
        "old": LOCKED_READ,
        "new": LOCKED_READ.replace("config.get_persisted(", "config.get("),
        "catchers": {
            LIVE_ONLY_RE_ENABLE: "AssertionError: assert False is not False",
            LIVE_ONLY_TURN_OFF: "AssertionError: assert True is not True",
        },
    },
    {
        # The save changes the live value first and saves the whole live
        # settings, the round-1 call: a failed replace leaves the live value
        # matching the choice, and the second press saves nothing.
        "name": "the-save-changes-the-live-value-first",
        "file": MAIN,
        "old": SAVE_CALL,
        "new": (
            "                        config.set(key, wanted)\n"
            "                        saved = bool(await config.save())\n"
        ),
        "catchers": {name: "AssertionError: 1" for name in RETRY},
    },
    {
        # The save does not take the GUI settings lock, so it can run in
        # the middle of a GUI settings write and its acknowledgement.
        "name": "the-save-takes-no-lock",
        "file": MAIN,
        "old": LOCK_HELD,
        "new": "            if True:\n",
        "catchers": {
            SAVE_WAITS: "AssertionError: the save ran while",
            **{name: PENDING_LOST for name in PENDING},
        },
    },
    {
        # A new lock on every save replaces the one a GUI write holds.
        "name": "the-lock-is-replaced-on-every-save",
        "file": MAIN,
        "old": LOCK_CREATE,
        "new": LOCK_CREATE.replace(
            'if not hasattr(state_manager, "_gui_settings_lock"):', "if True:"
        ),
        "catchers": {SAVE_WAITS: "AssertionError: the save ran while"},
    },
    {
        # The lock is never created: the first save after startup fails on
        # the missing attribute, and the choice is never kept.
        "name": "the-lock-is-never-created",
        "file": MAIN,
        "old": LOCK_CREATE,
        "new": LOCK_CREATE.replace(
            'if not hasattr(state_manager, "_gui_settings_lock"):', "if False:"
        ),
        "catchers": {LOCK_CREATED: "AssertionError: assert False"},
    },
    {
        # A MOVE, the af86a7c8 shape (wh-assistant-explainer-once-more.1.3):
        # the value on disk is compared before the lock is taken. A second,
        # opposite choice made while the first save is pending matches the
        # old value, saves nothing, and the first save then replaces it.
        "name": "the-comparison-is-made-before-the-lock",
        "file": MAIN,
        "old": LOCK_CREATE + LOCK_HELD + LOCKED_READ,
        "new": (
            "            matches = bool(config.get_persisted(key, True)) == wanted\n"
            + LOCK_CREATE
            + LOCK_HELD
            + "                if matches:\n"
        ),
        "catchers": {name: PENDING_LOST for name in PENDING},
    },
    {
        # No state update after the save, so the GUI keeps the old view.
        "name": "no-state-update-after-the-save",
        "file": MAIN,
        "old": STATE_UPDATE,
        "new": "                    pass\n",
        "catchers": {name: "assert False" for name in WRITABLE},
    },
    # ---------------------------------------------------------------
    # Behaviour 12 (wh-assistant-explainer-once-more.1.2): a second Help
    # request raises a visible window without resetting its check box.
    # ---------------------------------------------------------------
    {
        # The guard is dropped: every request resets the box, the defect
        # the finding reported.
        "name": "a-visible-window-is-reset",
        "file": GUI,
        "old": VISIBLE_GUARD,
        "new": "        if True:\n",
        "catchers": {
            CLEARED_STAYS: "assert True is False",
            TICKED_STAYS: "assert False is True",
            VISIBLE_RAISED: PREPARED_TWICE,
        },
    },
    {
        # The guard is inverted: only a visible window is reset, so a new
        # or hidden window keeps whatever its box held.
        "name": "the-visible-guard-is-inverted",
        "file": GUI,
        "old": VISIBLE_GUARD,
        "new": "        if self._help_explainer.isVisible():\n",
        "catchers": {
            HIDDEN_RESET[0]: "assert True is False",
            HIDDEN_RESET[1]: "assert False is True",
            # Fails at its first check: the new window is never set up.
            CLEARED_STAYS: "assert False is True",
            BOX_CLEARED: PREPARED_NEVER,
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 9 (wh-assistant-explainer-once-more): the start_ticked
    # flag, from Logic through the GUI to the window.
    # ---------------------------------------------------------------
    {
        # Logic drops the flag: the once-more window starts clear, and one
        # Assistant press turns the window back on.
        "name": "logic-drops-the-tick",
        "file": MAIN,
        "old": WINDOW_REQUEST,
        "new": "                self._request_help_explainer(start_ticked=False)\n",
        "catchers": {PRE_TICKED: QUEUED_LIST},
    },
    {
        # Logic inverts the flag.
        "name": "logic-inverts-the-tick",
        "file": MAIN,
        "old": WINDOW_REQUEST,
        "new": "                self._request_help_explainer(start_ticked=not once_more)\n",
        "catchers": {
            PRE_TICKED: QUEUED_LIST,
            TRUE_NO_TICK_NO_MARKER: QUEUED_LIST,
            TRUE_NO_TICK_MARKER: QUEUED_LIST,
            ASKS_FOR_WINDOW: QUEUED_LIST,
        },
    },
    {
        # The request always carries the flag, so every showing starts
        # ticked.
        "name": "the-request-always-carries-the-tick",
        "file": MAIN,
        "old": "        if start_ticked:\n",
        "new": "        if True:\n",
        "catchers": {
            TRUE_NO_TICK_NO_MARKER: QUEUED_LIST,
            ASKS_FOR_WINDOW: QUEUED_LIST,
        },
    },
    {
        # The window ignores the flag and always starts clear.
        "name": "the-window-ignores-the-tick",
        "file": WINDOW,
        "old": "        self._do_not_show_again.setChecked(bool(start_ticked))",
        "new": "        self._do_not_show_again.setChecked(False)",
        "catchers": {
            CAN_START_TICKED: "assert False is True",
            PRE_TICKED_REPORTS: "assert [False, False] == [True, False]",
        },
    },
    {
        # The window inverts the flag.
        "name": "the-window-inverts-the-tick",
        "file": WINDOW,
        "old": "        self._do_not_show_again.setChecked(bool(start_ticked))",
        "new": "        self._do_not_show_again.setChecked(not start_ticked)",
        "catchers": {
            CAN_START_TICKED: "assert False is True",
            CLEARS_AGAIN: "assert True is False",
            PRE_TICK_NO_TRAVEL: "assert True is False",
        },
    },
    # ---------------------------------------------------------------
    # Behaviour 10 (wh-assistant-explainer-once-more): the command
    # table's strict boolean check on do_not_show_again.
    # ---------------------------------------------------------------
    {
        # A non-boolean passes through, so the string "false" would read
        # as a ticked box in _record_help_explainer_choice.
        "name": "the-box-state-check-is-dropped",
        "file": MAIN,
        "old": TABLE_BOX_ARG,
        "new": 'do_not_show_again=command.get("do_not_show_again")',
        # Measured: the string cases carry the AssertionError prefix and
        # the others do not.
        "catchers": {
            REAL_BOOL_CATCHERS[0]: "AssertionError: assert '",
            REAL_BOOL_CATCHERS[1]: "AssertionError: assert '",
            REAL_BOOL_CATCHERS[2]: "assert 1 is None",
            REAL_BOOL_CATCHERS[3]: "assert 0 is None",
            REAL_BOOL_CATCHERS[4]: "assert [] is None",
        },
    },
    {
        # The box state never reaches start_help_online.
        "name": "the-box-state-is-not-passed-on",
        "file": MAIN,
        "old": TABLE_BOX_ARG,
        "new": "do_not_show_again=None",
        "catchers": {BOX_STATE_REACHES: NO_CALL},
    },
    {
        # Cancel reaches the Assistant handler, so backing out of the
        # window opens the page and can write the setting.
        "name": "cancel-emits-the-choice",
        "file": WINDOW,
        "old": "        cancel_button.clicked.connect(self.reject)",
        "new": "        cancel_button.clicked.connect(self._on_assistant)",
        "catchers": {
            CANCEL_SILENT: "assert [False] == []",
            CANCEL_SILENT_TICKED: "assert [True] == []",
        },
    },
    {
        # Every way out of the window reports a choice, including the
        # Escape key and the title-bar X.
        "name": "escape-and-cancel-emit-the-choice",
        "file": WINDOW,
        "old": ON_ASSISTANT,
        "new": (
            "    def reject(self) -> None:\n"
            "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())\n"
            "        super().reject()\n"
            "\n" + ON_ASSISTANT
        ),
        "catchers": {
            ESCAPE_SILENT: "assert [False] == []",
            CANCEL_SILENT: "assert [False] == []",
            CANCEL_SILENT_TICKED: "assert [True] == []",
        },
    },
    {
        # The choice is reported without the box's state, so a ticked box
        # never reaches the process that writes the setting.
        "name": "the-check-box-state-is-not-reported",
        "file": WINDOW,
        "old": "        self.assistant_chosen.emit(self._do_not_show_again.isChecked())",
        "new": "        self.assistant_chosen.emit(False)",
        "catchers": {REPORTS_TICKED: "assert [False] == [True]"},
    },
    # ---------------------------------------------------------------
    # The spoken command's half of criterion W4.
    # ---------------------------------------------------------------
    {
        # The spoken command stops naming itself, so the log cannot tell a
        # spoken "help" from the Help menu entry.
        "name": "the-spoken-command-does-not-name-itself",
        "file": ACTIONS,
        "old": '        await lc.start_help_online(source="spoken")',
        "new": "        await lc.start_help_online()",
        "catchers": {HANDS_OVER: NO_AWAIT},
    },
    # ---------------------------------------------------------------
    # Behaviour 13: the address in ai.help.assistant_url opens as written.
    # No substitution of an old address exists (wh-assistant-name-cleanup);
    # each mutation brings one back.
    # ---------------------------------------------------------------
    {
        # The old Gem address is replaced by the notebook address again.
        "name": "the-old-gem-address-is-replaced",
        "file": MAIN,
        "old": "            opened = await asyncio.to_thread(webbrowser.open, assistant_url)\n",
        "new": (
            "            opened = await asyncio.to_thread(\n"
            "                webbrowser.open,\n"
            "                _WHEELHOUSE_ASSISTANT_URL\n"
            '                if assistant_url.strip() == "https://gemini.google.com/gem/'
            '1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A"\n'
            "                else assistant_url,\n"
            "            )\n"
        ),
        "catchers": {
            OLD_GEM_UNCHANGED: "AssertionError: expected call not found",
        },
    },
    {
        # The old ChatGPT address is replaced by the notebook address again.
        "name": "the-old-chatgpt-address-is-replaced",
        "file": MAIN,
        "old": "            opened = await asyncio.to_thread(webbrowser.open, assistant_url)\n",
        "new": (
            "            opened = await asyncio.to_thread(\n"
            "                webbrowser.open,\n"
            "                _WHEELHOUSE_ASSISTANT_URL\n"
            '                if assistant_url.strip().startswith("https://chatgpt.com/")\n'
            "                else assistant_url,\n"
            "            )\n"
        ),
        "catchers": {
            OLD_CHATGPT_UNCHANGED: "AssertionError: expected call not found",
        },
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$")


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(originals, names):
    """Return (prepared mutants, error lines). Never touches the files."""
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        target = mutation["file"]
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
        old = mutation["old"].replace("\n", ending)
        new = mutation["new"].replace("\n", ending)
        count = source.count(old)
        if count == 0:
            errors.append(f"ERROR {mutation['name']}: pattern-not-found")
            continue
        if count > 1:
            errors.append(
                f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)"
            )
            continue
        mutant = source.replace(old, new, 1)
        try:
            compile(mutant, str(target), "exec")
        except SyntaxError as exc:
            errors.append(f"ERROR {mutation['name']}: does-not-compile ({exc})")
            continue
        prepared.append((mutation, mutant.encode("utf-8")))
    return prepared, errors


def _clear_bytecode():
    for directory in SERVICE.rglob("__pycache__"):
        if ".venv" not in directory.parts:
            shutil.rmtree(directory, ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # pytest cuts the " - <reason>" suffix off a short-summary line wider
    # than the terminal, and a captured run reports 80 columns. The node ids
    # in this selection pass 100 characters routinely.
    env["COLUMNS"] = "1000"
    return env


def _pytest(extra, timeout):
    command = [
        PYTHON, "-m", "pytest", *SELECTION,
        "-p", "no:randomly", "-p", "no:cacheprovider",
        *extra,
    ]
    return subprocess.run(
        command, cwd=SERVICE, env=_env(), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _collected_names():
    """Every collected test name, with and without its parameter list."""
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            full = line.split("::")[-1].split(" ")[0].strip()
            names.add(full)
            names.add(full.split("[")[0])
    return result.returncode, names


def _summary(output: str):
    """Map test name -> (kind, message) from pytest's short summary only.

    Reading anywhere else would let a captured log record at ERROR level
    read as a test record.
    """
    records, inside = {}, False
    for line in output.splitlines():
        if SUMMARY_HEADER.match(line):
            inside = True
            continue
        if inside and SECTION_RULE.match(line):
            break
        if inside:
            match = SUMMARY_LINE.match(line)
            if match:
                name = match.group(2).split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    # Key on the exit code first: a green run prints no summary section at
    # all, and that is a survivor rather than an unreadable run.
    if result.returncode == 1 and not records:
        raise RuntimeError(
            f"pytest exit 1 with no short-summary records: {output[-1500:]}"
        )
    return records


def _write_with_retry(path: Path, data: bytes):
    tmp = path.with_name(f"{path.name}.mutation-gate.{os.getpid()}.tmp")
    last = None
    try:
        for _ in range(10):
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _restore(target: Path, original: bytes, stat):
    """Restore bytes and timestamps; hold a Ctrl+C until after the cache clear."""
    held = None
    for _attempt in range(2):
        try:
            _write_with_retry(target, original)
            os.utime(target, ns=stat)
            if target.read_bytes() == original:
                break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as exc:
            print(f"RESTORE-ATTEMPT-FAILED {target}: {exc}", flush=True)
    restored = False
    try:
        restored = target.read_bytes() == original
    except (OSError, KeyboardInterrupt):
        pass
    if not restored:
        print(
            f"RESTORE FAILED: {target} may still hold a mutant; check git diff",
            flush=True,
        )
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


def _judge(mutation, records):
    unexpected = [
        f"{name}: {message}"
        for name, (kind, message) in records.items()
        if kind == "ERROR"
        or not (message.startswith("assert") or message.startswith("AssertionError"))
    ]
    if unexpected:
        return "ERROR", "non-assertion failure: " + "; ".join(unexpected)
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        if name not in records:
            green.append(name)
        elif not records[name][1].startswith(marker):
            wrong.append(f"{name}: {records[name][1]} (expected {marker!r})")
        else:
            fired.append(f"{name}: {records[name][1]}")
    if wrong:
        return "ERROR", "catcher failed at an unexpected assertion: " + "; ".join(wrong)
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append(
            "other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others)
        )
    if not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true",
                        help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME",
                        help="run only these mutations")
    parser.add_argument("--log", type=Path, help="also append output to this file")
    args = parser.parse_args(argv)

    log = args.log.open("a", encoding="utf-8", buffering=1) if args.log else None

    def say(text):
        print(text, flush=True)
        if log:
            log.write(text + "\n")

    known = {m["name"] for m in MUTATIONS}
    unknown = [n for n in args.only if n not in known]
    if unknown:
        say(f"ERROR unknown mutation names: {', '.join(unknown)}")
        return 1

    originals = {target: target.read_bytes() for target in TARGETS}
    stats = {
        target: (target.stat().st_atime_ns, target.stat().st_mtime_ns)
        for target in TARGETS
    }
    for target in TARGETS:
        ending = "CRLF" if _line_ending(originals[target]) == "\r\n" else "LF"
        say(f"Target {target.name}: {ending}")

    prepared, errors = _prepare(originals, set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, "
        f"{broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted(
        {name for mutation, _ in prepared for name in mutation["catchers"]}
        - collected
    )
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): "
            f"{', '.join(missing) or 'none collected'}")
        return 1

    _clear_bytecode()
    try:
        baseline = _run_selection()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        say(f"ERROR baseline: {exc}")
        return 1
    if baseline:
        say(f"ERROR baseline not green: {sorted(baseline)}")
        return 1
    say(f"Baseline green; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    survivors = 0
    error_count = len(errors)
    for index, (mutation, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        target = mutation["file"]
        stat = stats[target]
        try:
            _write_with_retry(target, mutant)
            os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection()
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(target, originals[target], stat)
        if verdict == "SURVIVED":
            survivors += 1
        elif verdict == "ERROR":
            error_count += 1
        say(f"{verdict} {name}: {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{len(prepared) - survivors - (error_count - len(errors))} caught, "
        f"{survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
