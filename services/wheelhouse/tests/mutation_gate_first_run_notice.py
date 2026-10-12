"""Mutation gate for the first-run notice (wh-first-run-notice).

Run it from services/wheelhouse:

    .venv/Scripts/python.exe tests/mutation_gate_first_run_notice.py --check
    .venv/Scripts/python.exe tests/mutation_gate_first_run_notice.py [--only NAME ...] [--log PATH]

It proves that the guard tests in tests/test_first_run_notice.py (and the
startup-registration test in tests/test_update_notice.py) fail when the
behaviour they protect breaks. Five behaviours are guarded, across four
files:

  1. The first-start check in LogicController._run_first_run_notice
     (main.py): the marker is read once, off the event loop; a missing
     marker means the GUI is sent one ``open_first_run_notice`` message
     and the marker is NOT written there; an existing marker means
     nothing is sent and nothing is rewritten; a start that could not
     queue the message writes no marker; no exception escapes the task.
     The write point is the handler for the GUI process's
     ``first_run_notice_shown`` command
     (LogicController._handle_first_run_notice_shown and its entry in the
     command table): the marker is written there, off the event loop, a
     second command writes it again without an error, and no exception
     escapes (wh-first-run-notice.1.1). The marker path
     (default_first_run_notice_marker_path) and the task's registration
     beside the other startup tasks are mutated too.
     The notice also waits for the speech provider (wh-first-run-notice.2):
     after the marker read, _run_first_run_notice waits on
     _speech_provider_ready, and a start that finds a marker never waits;
     _speech_provider_ready_callback sets that event on the loop from any
     thread; main() gives the callback to the launcher.
  2. The GUI process side in GuiManager (gui.py): the queue arm, the
     single window, showing and raising it, the foreground request, the
     one ``first_run_notice_shown`` command sent after the window is shown
     (and again for a second message), and the Open help command
     (``explained`` true, no ``do_not_show_again``). The queue arm holds
     the notice while the loading window is visible; the hide_working arm
     opens a held notice once, and only when the loading window is no
     longer visible after the hide (wh-first-run-notice.2).
  3. FirstRunNoticeWindow (first_run_notice_window.py): the approved title,
     body, and button labels; Open help emits and closes; Close emits
     nothing; the window is modeless and Open help is the default button.
  4. RemoteSTTLauncher.signal_provider_ready (stt/remote_stt_launcher.py):
     the ready callback runs once for a ready from the current launch, not
     for a ready from a replaced launch or after a failed startup, and an
     exception from it does not escape (wh-first-run-notice.2).

Each mutation names the tests that must fail and the start of the
assertion text each must fail with, as pytest prints it in the short test
summary. A catch requires every named catcher to fail AT ITS OWN
ASSERTION: a failure whose message is not assertion-shaped is reported as
an error, never as a catch, because an exception upstream of the assertion
proves nothing.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, non-assertion failure, restore failure).

Do not run this concurrently with a test suite or with edits in this
worktree: it rewrites main.py, gui.py, first_run_notice_window.py, and
stt/remote_stt_launcher.py while each mutation runs.

Mutations left out on purpose:

  * "a foreground-request failure escapes the GUI handler". The queue
    handler that calls _open_first_run_notice catches its own exceptions
    after the window is shown, so no assertion in a test can tell the
    uncontained form from the contained one.
  * "the no-GUI-queue branch returns" and "the failed-put branch returns",
    each replaced by a plain pass. _run_first_run_notice no longer writes
    the marker, so a branch that stops returning has no visible effect:
    with the queue None the AttributeError from put_nowait is caught by
    the inner except, which returns too. The two mutations
    no-queue-writes-the-marker and failed-put-writes-the-marker put a
    marker write into each branch instead, which the no-queue and the
    full-queue tests catch.
  * "the ready callback sets the event directly from the caller thread".
    asyncio.Event.set from another thread usually works in a test, so no
    test can tell it apart from call_soon_threadsafe reliably.
"""
# crewcut: the runner below is copied from
# tests/mutation_gate_audio_pause_notice.py, extended here to four target
# files. A shared runner module imported by all gates is the way to remove
# the duplication later.
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
WINDOW = SERVICE / "first_run_notice_window.py"
LAUNCHER = SERVICE / "stt" / "remote_stt_launcher.py"
TARGETS = (MAIN, GUI, WINDOW, LAUNCHER)
NOTICE_TESTS = "tests/test_first_run_notice.py"
UPDATE_TESTS = "tests/test_update_notice.py"
SELECTION = [
    NOTICE_TESTS,
    f"{UPDATE_TESTS}::TestStartup",
]
RUN_TIMEOUT = 180  # A clean run of the selection takes a few seconds.

# Logic
SENDS_ONE = "test_with_no_marker_the_gui_is_sent_one_show_message"
PUT_ALONE = "test_the_queue_put_alone_writes_no_marker"
NEVER_SHOWN = "test_a_gui_that_never_shows_the_window_gets_the_message_again"
CMD_WRITES = "test_the_command_from_the_gui_writes_the_marker"
CMD_TWICE = "test_a_second_command_writes_the_marker_again_without_an_error"
AFTER_CMD = "test_a_start_after_the_command_shows_nothing"
LATER_SENDS_NOTHING = "test_with_a_marker_the_gui_is_sent_nothing"
NOT_REWRITTEN = "test_a_marker_that_is_not_rewritten"
UNREADABLE = "test_an_unreadable_marker_counts_as_shown"
NO_QUEUE = "test_no_gui_queue_raises_nothing_and_writes_no_marker"
FULL_QUEUE = "test_a_full_queue_raises_nothing_and_writes_no_marker"
WRITE_FAILS = "test_a_marker_write_that_fails_raises_nothing"
READ_FAILS = "test_a_marker_read_that_fails_raises_nothing"
READ_THREAD = "test_the_read_runs_in_a_worker_thread"
WRITE_THREAD = "test_the_write_runs_in_a_worker_thread"
READ_ONCE = "test_the_marker_is_read_once"
PATH_NAME = "test_it_is_the_first_run_file_under_data"
PATH_NOT_HELP = "test_it_is_not_the_help_explainer_marker"
REGISTERED = "test_the_notice_task_is_registered_with_the_other_startup_tasks"
REGISTERED_SET = "test_the_update_check_is_registered_beside_the_other_startup_task"

# GUI
OPENS = "test_the_logic_message_opens_the_window"
SECOND_MESSAGE = "test_a_second_message_raises_the_one_window"
FRONT = "test_the_window_is_brought_to_the_front"
HELP_DIRECT = "test_the_help_choice_opens_help_directly"
NO_SETTING = "test_the_help_choice_carries_no_setting_change"
SHOWN_ONCE = "test_showing_the_window_sends_the_shown_command_once"
SHOWN_AFTER_SHOW = "test_the_shown_command_follows_the_window_being_shown"
SHOW_FAILS_NO_CMD = "test_a_window_that_fails_to_show_sends_no_shown_command"
SECOND_REPORTS = "test_a_second_message_shows_no_second_window_and_reports_again"

# Window
TITLE = "test_the_title_is_the_approved_title"
PARAGRAPHS = "test_the_three_paragraphs_are_the_approved_words"
BODY = "test_the_body_shows_the_three_paragraphs_in_order"
LABELS = "test_the_buttons_carry_the_approved_labels"
OPEN_HELP_REPORTS = "test_open_help_reports_the_choice_and_closes"
CLOSE_REPORTS_NOTHING = "test_close_reports_nothing_and_closes"
NOT_MODAL = "test_the_window_is_not_modal"
DEFAULT_BUTTON = "test_open_help_is_the_default_button"
TITLE_IN_SOURCE = "test_the_title_string_appears_in_the_module_source"

# Logic: the notice waits for the speech provider (wh-first-run-notice.2)
NOT_SENT_EARLY = "test_nothing_is_sent_before_the_provider_is_ready"
SENT_ON_READY = "test_the_message_is_sent_once_the_provider_is_ready"
NEVER_READY = "test_a_start_that_never_becomes_ready_sends_nothing_and_writes_no_marker"
NEXT_READY = "test_the_next_start_that_becomes_ready_shows_the_notice"
MARKER_NO_WAIT = "test_with_a_marker_the_start_does_not_wait_for_ready"
CALLBACK_SETS = "test_the_ready_callback_sets_the_event_from_another_thread"
MAIN_GIVES_CALLBACK = "test_main_gives_the_callback_to_the_launcher"

# Launcher
READY_CALLS_ONCE = "test_a_ready_from_the_current_launch_calls_the_callback_once"
REPLACED_NO_CALL = "test_a_ready_from_a_replaced_launch_does_not"
FAILED_NO_CALL = "test_a_ready_after_a_failed_startup_does_not"
CALLBACK_RAISES = "test_a_callback_that_raises_does_not_stop_the_ready"

# GUI holds the notice for the loading window
HELD_BY_WINDOW = "test_a_message_while_the_loading_window_is_up_opens_nothing"
OPENS_ON_CLOSE = "test_the_held_notice_opens_when_the_loading_window_closes"
STAYS_UP = "test_a_hide_that_leaves_the_loading_window_up_opens_nothing"
OPENS_ONCE = "test_the_held_notice_opens_once"
NO_HELD_NOTICE = "test_a_hide_with_no_held_notice_opens_nothing"

READ_OLD = "if await asyncio.to_thread(load_hint_shown, marker):"
# The first line alone also matches _record_help_explainer_choice, so the
# warning line that follows it in the first-run handler makes it unique.
WRITE_NEXT = '                logger.warning("First-run notice: the marker was not written.")\n'
WRITE_OLD = (
    "            if not await asyncio.to_thread(mark_hint_shown, marker):\n" + WRITE_NEXT
)

PUT_LINE = '                queue.put_nowait({"action": "open_first_run_notice"})\n'
MARK_IMPORT = (
    "                from services.wheelhouse.click_first_use_hint import mark_hint_shown\n"
)
SHOWN_SEND = '        self.send_command({"action": "first_run_notice_shown"})\n'
SHOW_CALL = "        self._first_run_notice.show()\n"

MUTATIONS = [
    {
        # Logic reads the marker the wrong way round: a first start shows
        # nothing and a later start shows the notice again.
        "name": "first-start-check-inverted",
        "file": MAIN,
        "old": READ_OLD,
        "new": "if not await asyncio.to_thread(load_hint_shown, marker):",
        "catchers": {
            SENDS_ONE: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
            PUT_ALONE: "assert [] == [False]",
            NEVER_SHOWN: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
            LATER_SENDS_NOTHING: "AssertionError: assert [{'action': '..._run_notice'}] == []",
            AFTER_CMD: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
            UNREADABLE: "AssertionError: assert [{'action': '..._run_notice'}] == []",
            NEVER_READY: "AssertionError: the notice task stopped instead of waiting",
            NEXT_READY: "AssertionError: the notice task stopped instead of waiting",
        },
    },
    {
        # The command from the GUI process writes no marker, so every
        # start is a first start.
        "name": "marker-write-removed",
        "file": MAIN,
        "old": WRITE_OLD,
        "new": "            if False:\n" + WRITE_NEXT,
        "catchers": {
            CMD_WRITES: 'AssertionError: assert False',
            CMD_TWICE: 'AssertionError: assert False',
            AFTER_CMD: "AssertionError: assert [{'action': '..._run_notice'}] == []",
            WRITE_THREAD: 'assert False is True',
        },
    },
    {
        # The message to the GUI is never queued, so the notice is never
        # shown.
        "name": "gui-message-removed",
        "file": MAIN,
        "old": PUT_LINE,
        "new": "                pass\n",
        "catchers": {
            SENDS_ONE: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
            PUT_ALONE: "assert [] == [False]",
            NEVER_SHOWN: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
            AFTER_CMD: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
        },
    },
    {
        # The message names an action the GUI does not know.
        "name": "gui-message-wrong-action",
        "file": MAIN,
        "old": '{"action": "open_first_run_notice"}',
        "new": '{"action": "open_first_run_notices"}',
        "catchers": {
            SENDS_ONE: "AssertionError: assert [{'action': '...run_notices'}] == [{'action': '..._run_notice'}]",
            NEVER_SHOWN: "AssertionError: assert [{'action': '...run_notices'}] == [{'action': '..._run_notice'}]",
        },
    },
    {
        # The marker is written before the message is sent, so a start that
        # dies between the two shows nothing now and nothing later, and a
        # GUI process that never shows the window still costs the marker.
        "name": "marker-written-before-the-message",
        "file": MAIN,
        "old": PUT_LINE,
        "new": (
            MARK_IMPORT
            + "                mark_hint_shown(marker)\n"
            + PUT_LINE
        ),
        "catchers": {
            PUT_ALONE: "assert [True] == [False]",
            NEVER_SHOWN: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
        },
    },
    {
        # The old write point comes back: the marker is written right after
        # the queue put, though no GUI process has shown a window
        # (wh-first-run-notice.1.1).
        "name": "marker-written-after-the-queue-put",
        "file": MAIN,
        "old": PUT_LINE,
        "new": (
            PUT_LINE
            + MARK_IMPORT
            + "                await asyncio.to_thread(mark_hint_shown, marker)\n"
        ),
        "catchers": {
            PUT_ALONE: "AssertionError: assert not True",
            NEVER_SHOWN: "AssertionError: assert [] == [{'action': '..._run_notice'}]",
        },
    },
    {
        # The marker is read on the event loop.
        "name": "marker-read-on-the-event-loop",
        "file": MAIN,
        "old": READ_OLD,
        "new": "if load_hint_shown(marker):",
        "catchers": {
            READ_THREAD: 'assert False is True',
        },
    },
    {
        # The marker is written on the event loop.
        "name": "marker-written-on-the-event-loop",
        "file": MAIN,
        "old": WRITE_OLD,
        "new": "            if not mark_hint_shown(marker):\n" + WRITE_NEXT,
        "catchers": {
            WRITE_THREAD: 'assert False is True',
        },
    },
    {
        # A start with no GUI queue writes the marker, though nothing could
        # be shown.
        "name": "no-queue-writes-the-marker",
        "file": MAIN,
        "old": (
            "            if queue is None:\n"
            '                logger.warning("First-run notice: no GUI queue, not shown.")\n'
            "                return\n"
        ),
        "new": (
            "            if queue is None:\n"
            '                logger.warning("First-run notice: no GUI queue, not shown.")\n'
            + MARK_IMPORT
            + "                mark_hint_shown(marker)\n"
            "                return\n"
        ),
        "catchers": {
            NO_QUEUE: 'AssertionError: assert not True',
        },
    },
    {
        # A start whose queue put fails writes the marker, though nothing
        # could be shown.
        "name": "failed-put-writes-the-marker",
        "file": MAIN,
        "old": (
            '                logger.warning("First-run notice could not be queued: %s", exc)\n'
            "                return\n"
        ),
        "new": (
            '                logger.warning("First-run notice could not be queued: %s", exc)\n'
            + MARK_IMPORT
            + "                mark_hint_shown(marker)\n"
            "                return\n"
        ),
        "catchers": {
            FULL_QUEUE: 'AssertionError: assert not True',
        },
    },
    {
        # An exception from the marker write escapes the handler's task,
        # which create_task_with_error_handling answers by shutting
        # Wheelhouse down.
        "name": "write-exceptions-escape-the-task",
        "file": MAIN,
        "old": (
            "        except Exception as exc:\n"
            '            logger.warning("First-run notice marker failed: %s", exc)\n'
        ),
        "new": (
            "        except KeyboardInterrupt as exc:\n"
            '            logger.warning("First-run notice marker failed: %s", exc)\n'
        ),
        "catchers": {
            WRITE_FAILS: "AssertionError: assert OSError('disk full') is None",
        },
    },
    {
        # An exception from the marker read escapes the notice task.
        "name": "read-exceptions-escape-the-task",
        "file": MAIN,
        "old": (
            "        except Exception as exc:\n"
            '            logger.warning("First-run notice failed: %s", exc)\n'
        ),
        "new": (
            "        except KeyboardInterrupt as exc:\n"
            '            logger.warning("First-run notice failed: %s", exc)\n'
        ),
        "catchers": {
            READ_FAILS: "AssertionError: assert OSError('disk error') is None",
        },
    },
    {
        # The command table has no entry for the command, so the GUI
        # process's answer is dropped and the marker is never written.
        "name": "command-table-entry-renamed",
        "file": MAIN,
        "old": '            "first_run_notice_shown": lambda:',
        "new": '            "first_run_notice_shown_x": lambda:',
        "catchers": {
            CMD_WRITES: "AssertionError: assert 'first_run_notice_shown' in {",
            CMD_TWICE: "AssertionError: assert 'first_run_notice_shown' in {",
            AFTER_CMD: "AssertionError: assert 'first_run_notice_shown' in {",
            WRITE_THREAD: "AssertionError: assert 'first_run_notice_shown' in {",
        },
    },
    {
        # The table entry starts no task.
        "name": "command-table-entry-does-nothing",
        "file": MAIN,
        "old": '"first_run_notice_shown": lambda: self.create_task_with_error_handling(self._handle_first_run_notice_shown(), "FirstRunNoticeShown"),',
        "new": '"first_run_notice_shown": lambda: None,',
        "catchers": {
            CMD_WRITES: "AssertionError: assert [] == ['FirstRunNoticeShown']",
        },
    },
    {
        # The table entry names its task differently.
        "name": "command-table-entry-task-renamed",
        "file": MAIN,
        "old": 'self._handle_first_run_notice_shown(), "FirstRunNoticeShown")',
        "new": 'self._handle_first_run_notice_shown(), "FirstRunNoticeShow")',
        "catchers": {
            CMD_WRITES: "AssertionError: assert ['FirstRunNoticeShow'] == ['FirstRunNoticeShown']",
        },
    },
    {
        # The marker is read twice.
        "name": "marker-read-twice",
        "file": MAIN,
        # The marker line alone also begins the command handler, so the read
        # that follows it in the notice task makes the pattern unique.
        "old": (
            "            marker = default_first_run_notice_marker_path()\n"
            "            " + READ_OLD + "\n"
        ),
        "new": (
            "            marker = default_first_run_notice_marker_path()\n"
            "            await asyncio.to_thread(load_hint_shown, marker)\n"
            "            " + READ_OLD + "\n"
        ),
        "catchers": {
            READ_ONCE: "AssertionError: assert [WindowsPath(..._shown.toml')] == [WindowsPath(..._shown.toml')]",
        },
    },
    {
        # The marker file is the help explanation window's marker.
        "name": "marker-path-is-the-help-marker",
        "file": MAIN,
        "old": '        / "first_run_notice_shown.toml"\n',
        "new": '        / "help_explainer_notebook_shown.toml"\n',
        "catchers": {
            PATH_NAME: "AssertionError: assert 'help_explain...ok_shown.toml' == 'first_run_notice_shown.toml'",
            PATH_NOT_HELP: "AssertionError: assert WindowsPath('",
        },
    },
    {
        # The marker file moves out of the data folder.
        "name": "marker-path-outside-data",
        "file": MAIN,
        "old": '        / "data"\n        / "first_run_notice_shown.toml"\n',
        "new": '        / "first_run_notice_shown.toml"\n',
        "catchers": {
            PATH_NAME: "AssertionError: assert 'wheelhouse' == 'data'",
        },
    },
    {
        # The task is not registered, so it never runs.
        "name": "task-not-registered",
        "file": MAIN,
        "old": '            "FirstRunNotice": self._run_first_run_notice(),\n',
        "new": "",
        "catchers": {
            REGISTERED: "AssertionError: assert 'FirstRunNotice' in {'PeriodicStateUpdate': <coroutine object Async",
            REGISTERED_SET: "AssertionError: assert {'PeriodicSta...'UpdateCheck'} == {'FirstRunNot...'UpdateCheck'}",
        },
    },
    {
        # The GUI queue arm is renamed, so the message is ignored.
        "name": "gui-arm-renamed",
        "file": GUI,
        "old": 'elif action == "open_first_run_notice":',
        "new": 'elif action == "open_first_run_notice_x":',
        "catchers": {
            OPENS: "AssertionError: Expected 'FirstRunNoticeWindow' to have been called once. Called 0 times.",
            SECOND_MESSAGE: "AssertionError: Expected 'FirstRunNoticeWindow' to have been called once. Called 0 times.",
            FRONT: "AssertionError: Expected '_steal_foreground' to have been called once. Called 0 times.",
        },
    },
    {
        # Every message builds a new window.
        "name": "gui-builds-a-window-each-time",
        "file": GUI,
        "old": "if getattr(self, '_first_run_notice', None) is None:",
        "new": "if True:",
        "catchers": {
            SECOND_MESSAGE: "AssertionError: Expected 'FirstRunNoticeWindow' to have been called once. Called 2 times.",
        },
    },
    {
        # The window is built but never shown.
        "name": "gui-window-not-shown",
        "file": GUI,
        "old": "        self._first_run_notice.show()\n",
        "new": "        pass\n",
        "catchers": {
            OPENS: "AssertionError: Expected 'show' to have been called once. Called 0 times.",
            SECOND_MESSAGE: 'AssertionError: assert 0 == 2',
        },
    },
    {
        # The window is not raised.
        "name": "gui-window-not-raised",
        "file": GUI,
        "old": "        self._first_run_notice.raise_()\n",
        "new": "        pass\n",
        "catchers": {
            SECOND_MESSAGE: 'AssertionError: assert 0 == 2',
        },
    },
    {
        # The window is not activated.
        "name": "gui-window-not-activated",
        "file": GUI,
        "old": "        self._first_run_notice.activateWindow()\n",
        "new": "        pass\n",
        "catchers": {
            SECOND_MESSAGE: 'AssertionError: assert 0 == 2',
        },
    },
    {
        # The foreground request is dropped, so Windows only flashes the
        # taskbar button.
        "name": "gui-foreground-not-requested",
        "file": GUI,
        "old": "            _steal_foreground(int(self._first_run_notice.winId()))\n",
        "new": "            pass\n",
        "catchers": {
            FRONT: "AssertionError: Expected '_steal_foreground' to have been called once. Called 0 times.",
        },
    },
    {
        # The window is on screen but the Logic process is never told, so
        # the marker is never written.
        "name": "gui-shown-command-not-sent",
        "file": GUI,
        "old": SHOWN_SEND,
        "new": "",
        "catchers": {
            SHOWN_ONCE: "AssertionError: assert [] == [{'action': '...otice_shown'}]",
            SHOWN_AFTER_SHOW: "AssertionError: assert [] == [{'action': '...otice_shown'}]",
            SECOND_REPORTS: "AssertionError: assert [] == [{'action': '...otice_shown'}]",
        },
    },
    {
        # The command names another action, so the table has no handler.
        "name": "gui-shown-command-wrong-action",
        "file": GUI,
        "old": SHOWN_SEND,
        "new": '        self.send_command({"action": "first_run_notice_show"})\n',
        "catchers": {
            SHOWN_ONCE: "AssertionError: assert [{'action': '...notice_show'}] == [{'action': '...otice_shown'}]",
        },
    },
    {
        # The command is sent before the window is shown, so a window that
        # fails to show still costs the marker.
        "name": "gui-shown-command-before-the-window",
        "file": GUI,
        "old": SHOW_CALL,
        "new": SHOWN_SEND + SHOW_CALL,
        "catchers": {
            SHOWN_ONCE: "AssertionError: assert [{'action': '...otice_shown'}] == [{'action': '...otice_shown'}]",
            SHOWN_AFTER_SHOW: "assert [False] == [True]",
            SHOW_FAILS_NO_CMD: "AssertionError: assert [{'action': '...otice_shown'}] == []",
        },
    },
    {
        # A second message sends no second command.
        "name": "gui-shown-command-sent-once-only",
        "file": GUI,
        "old": SHOWN_SEND,
        "new": (
            '        if not getattr(self, "_first_run_notice_told", False):\n'
            "            self._first_run_notice_told = True\n"
            '            self.send_command({"action": "first_run_notice_shown"})\n'
        ),
        "catchers": {
            SECOND_REPORTS: "AssertionError: assert [{'action': '...otice_shown'}] == [{'action': '...otice_shown'}]",
        },
    },
    {
        # The Open help signal is connected to nothing.
        "name": "help-signal-not-connected",
        "file": GUI,
        "old": (
            "            self._first_run_notice.help_chosen.connect(\n"
            "                self._on_first_run_notice_help\n"
            "            )\n"
        ),
        "new": (
            "            self._first_run_notice.help_chosen.connect(\n"
            "                lambda: None\n"
            "            )\n"
        ),
        "catchers": {
            HELP_DIRECT: 'assert 0 == 1',
            NO_SETTING: 'assert 0 == 1',
        },
    },
    {
        # Open help asks for the explanation window instead of the page.
        "name": "help-command-not-explained",
        "file": GUI,
        "old": '            "explained": True,\n            "source": "first_run_notice",\n',
        "new": '            "explained": False,\n            "source": "first_run_notice",\n',
        "catchers": {
            HELP_DIRECT: 'assert False is True',
        },
    },
    {
        # Open help sends another action.
        "name": "help-command-wrong-action",
        "file": GUI,
        "old": '            "action": "open_help_online",\n            "explained": True,\n            "source": "first_run_notice",\n',
        "new": '            "action": "open_help",\n            "explained": True,\n            "source": "first_run_notice",\n',
        "catchers": {
            HELP_DIRECT: "AssertionError: assert 'open_help' == 'open_help_online'",
        },
    },
    {
        # Open help makes the Logic process save the explanation window's
        # setting and marker.
        "name": "help-command-carries-a-setting",
        "file": GUI,
        "old": '            "source": "first_run_notice",\n',
        "new": '            "source": "first_run_notice",\n            "do_not_show_again": False,\n',
        "catchers": {
            NO_SETTING: "AssertionError: assert 'do_not_show_again' not in {'action': 'open_help_online', 'do_not_s",
        },
    },
    {
        # Open help does not report the choice.
        "name": "window-open-help-emits-nothing",
        "file": WINDOW,
        "old": "        self.help_chosen.emit()\n",
        "new": "        pass\n",
        "catchers": {
            OPEN_HELP_REPORTS: "AssertionError: assert [] == ['help']",
        },
    },
    {
        # Open help leaves the window open.
        "name": "window-open-help-stays-open",
        "file": WINDOW,
        "old": "        self.help_chosen.emit()\n        self.accept()\n",
        "new": "        self.help_chosen.emit()\n",
        "catchers": {
            OPEN_HELP_REPORTS: 'assert True is False',
        },
    },
    {
        # Close reports the Open help choice.
        "name": "window-close-opens-help",
        "file": WINDOW,
        "old": "close_button.clicked.connect(self.reject)",
        "new": "close_button.clicked.connect(self._on_open_help)",
        "catchers": {
            CLOSE_REPORTS_NOTHING: "AssertionError: assert ['help'] == []",
        },
    },
    {
        # The window is modal, so the GUI queue stops while it is open.
        "name": "window-modal",
        "file": WINDOW,
        "old": "        self.setModal(False)\n",
        "new": "        self.setModal(True)\n",
        "catchers": {
            NOT_MODAL: 'assert True is False',
        },
    },
    {
        # Close, not Open help, is the Enter-key button.
        "name": "window-default-button-lost",
        "file": WINDOW,
        "old": "        help_button.setDefault(True)\n",
        "new": "        help_button.setDefault(False)\n",
        "catchers": {
            DEFAULT_BUTTON: 'assert False is True',
        },
    },
    {
        # The title changes by one letter.
        "name": "window-title-reworded",
        "file": WINDOW,
        "old": 'FIRST_RUN_NOTICE_TITLE = "Welcome to Wheelhouse"\n',
        "new": 'FIRST_RUN_NOTICE_TITLE = "Welcome to WheelHouse"\n',
        "catchers": {
            TITLE: "AssertionError: assert 'Welcome to WheelHouse' == 'Welcome to Wheelhouse'",
            TITLE_IN_SOURCE: 'assert \'"Welcome to Wheelhouse"\' in \'"""The window shown on the first start after an insta',
        },
    },
    {
        # The last paragraph is reworded.
        "name": "window-last-paragraph-reworded",
        "file": WINDOW,
        "old": '    "Wheelhouse shows this message once.",\n',
        "new": '    "Wheelhouse shows this message twice.",\n',
        "catchers": {
            PARAGRAPHS: "AssertionError: assert ('The round r...ssage twice.') == ('The round r...essage once.')",
            BODY: "AssertionError: assert 'The round re...essage twice.' == 'The round re...message once.'",
        },
    },
    {
        # The paragraphs are shown in the wrong order.
        "name": "window-paragraphs-reordered",
        "file": WINDOW,
        "old": 'FIRST_RUN_NOTICE_BODY = "\\n\\n".join(FIRST_RUN_NOTICE_BODY_PARAGRAPHS)\n',
        "new": 'FIRST_RUN_NOTICE_BODY = "\\n\\n".join(reversed(FIRST_RUN_NOTICE_BODY_PARAGRAPHS))\n',
        "catchers": {
            BODY: "AssertionError: assert 'Wheelhouse s...le you speak.' == 'The round re...message once.'",
        },
    },
    {
        # The Open help label changes by one letter.
        "name": "window-open-help-label-reworded",
        "file": WINDOW,
        "old": 'OPEN_HELP_BUTTON_LABEL = "Open help"\n',
        "new": 'OPEN_HELP_BUTTON_LABEL = "Open Help"\n',
        "catchers": {
            LABELS: "AssertionError: assert 'Open Help' == 'Open help'",
        },
    },
    {
        # The Close label changes.
        "name": "window-close-label-reworded",
        "file": WINDOW,
        "old": 'CLOSE_BUTTON_LABEL = "Close"\n',
        "new": 'CLOSE_BUTTON_LABEL = "Cancel"\n',
        "catchers": {
            LABELS: "AssertionError: assert 'Cancel' == 'Close'",
        },
    },
    {
        # The notice waits for nothing, so it opens under the loading window.
        "name": "notice-waits-for-nothing",
        "file": MAIN,
        "old": "            await self._speech_provider_ready.wait()\n",
        "new": "            pass\n",
        "catchers": {
            NOT_SENT_EARLY: "AssertionError: assert [{'action': '..._run_notice'}] == []",
            NEVER_READY: "AssertionError: assert [{'action': '..._run_notice'}] == []",
            NEXT_READY: "AssertionError: assert [{'action': '..._run_notice'}] == []",
        },
    },
    {
        # The wait moves above the marker read, so a start that finds a
        # marker (a tray restart after the notice was shown) hangs before it
        # reads it. Every test that leaves the event unset never sees the
        # read either, so each fails at the helper's assertion.
        "name": "wait-before-the-marker-read",
        "file": MAIN,
        "old": (
            "            " + READ_OLD + "\n"
            "                return\n"
            "            await self._speech_provider_ready.wait()\n"
        ),
        "new": (
            "            await self._speech_provider_ready.wait()\n"
            "            " + READ_OLD + "\n"
            "                return\n"
        ),
        "catchers": {
            MARKER_NO_WAIT: "AssertionError: the notice task never read the marker",
            NOT_SENT_EARLY: "AssertionError: the notice task never read the marker",
            SENT_ON_READY: "AssertionError: the notice task never read the marker",
            NEVER_READY: "AssertionError: the notice task never read the marker",
            NEXT_READY: "AssertionError: the notice task never read the marker",
        },
    },
    {
        # The callback the launcher calls never sets the event, so the notice
        # waits for ever.
        "name": "callback-never-sets-the-event",
        "file": MAIN,
        "old": "            loop.call_soon_threadsafe(ready.set)\n",
        "new": "            pass\n",
        "catchers": {
            CALLBACK_SETS: "AssertionError: assert False",
        },
    },
    {
        # main() never gives the callback to the launcher, so a real start
        # never becomes ready for the notice.
        "name": "main-does-not-register-the-callback",
        "file": MAIN,
        "old": (
            "                self.service_manager.remote_stt_launcher.set_provider_ready_callback(\n"
            "                    self._speech_provider_ready_callback()\n"
            "                )\n"
        ),
        "new": "                pass\n",
        "catchers": {
            MAIN_GIVES_CALLBACK: "assert 'set_provider_ready_callback(' in '",
        },
    },
    {
        # The launcher accepts a ready from a replaced launch, so a provider
        # that was switched away calls the callback.
        "name": "launcher-ignores-the-current-launch",
        "file": LAUNCHER,
        "old": "accepted = self.launch_is_current(generation) and not (",
        "new": "accepted = True and not (",
        "catchers": {
            REPLACED_NO_CALL: "AssertionError: Expected 'mock' to not have been called. Called 1 times.",
        },
    },
    {
        # The launcher accepts a ready after a failed startup.
        "name": "launcher-ignores-a-failed-startup",
        "file": LAUNCHER,
        "old": "                signal is not None and signal.failed\n",
        "new": "                False\n",
        "catchers": {
            FAILED_NO_CALL: "AssertionError: Expected 'mock' to not have been called. Called 1 times.",
        },
    },
    {
        # The launcher never calls the callback.
        "name": "launcher-never-calls-the-callback",
        "file": LAUNCHER,
        "old": "if accepted and self._provider_ready_callback is not None:",
        "new": "if False:",
        "catchers": {
            READY_CALLS_ONCE: "AssertionError: Expected 'mock' to be called once. Called 0 times.",
        },
    },
    {
        # An exception from the callback escapes the WebSocket reader.
        "name": "launcher-callback-uncontained",
        "file": LAUNCHER,
        "old": (
            "            try:\n"
            "                self._provider_ready_callback()\n"
            "            except Exception as exc:\n"
            '                logger.warning(f"Provider ready callback failed: {exc}")\n'
        ),
        "new": "            self._provider_ready_callback()\n",
        "catchers": {
            CALLBACK_RAISES: "AssertionError: assert RuntimeError('closed loop') is None",
        },
    },
    {
        # The GUI opens the notice at once, under the loading window.
        "name": "gui-does-not-hold",
        "file": GUI,
        "old": (
            "                    if self.working_dialog.isVisible():\n"
            "                        self._first_run_notice_held = True\n"
        ),
        "new": (
            "                    if False:\n"
            "                        self._first_run_notice_held = True\n"
        ),
        "catchers": {
            HELD_BY_WINDOW: "AssertionError: Expected 'show' to not have been called. Called 1 times.",
            STAYS_UP: "AssertionError: Expected 'show' to not have been called. Called 1 times.",
        },
    },
    {
        # A held notice never opens when the loading window closes.
        "name": "gui-held-notice-never-opens",
        "file": GUI,
        "old": (
            "                        self._first_run_notice_held = False\n"
            "                        self._open_first_run_notice()\n"
        ),
        "new": (
            "                        self._first_run_notice_held = False\n"
            "                        pass\n"
        ),
        "catchers": {
            OPENS_ON_CLOSE: "AssertionError: Expected 'show' to have been called once. Called 0 times.",
            STAYS_UP: "AssertionError: Expected 'show' to have been called once. Called 0 times.",
            OPENS_ONCE: "AssertionError: Expected 'show' to have been called once. Called 0 times.",
        },
    },
    {
        # A hide opens the held notice even when the loading window stays up
        # (the dialog ignored a hide from a replaced launch).
        "name": "gui-opens-on-any-hide",
        "file": GUI,
        "old": "                        and not self.working_dialog.isVisible()\n",
        "new": "                        and True\n",
        "catchers": {
            STAYS_UP: "AssertionError: Expected 'show' to not have been called. Called 1 times.",
        },
    },
    {
        # The held flag stays set, so every later hide opens the notice again.
        "name": "gui-held-flag-not-cleared",
        "file": GUI,
        "old": (
            "                        self._first_run_notice_held = False\n"
            "                        self._open_first_run_notice()\n"
        ),
        "new": (
            "                        pass\n"
            "                        self._open_first_run_notice()\n"
        ),
        "catchers": {
            OPENS_ONCE: "AssertionError: Expected 'show' to have been called once. Called 2 times.",
        },
    },
    {
        # A hide opens the notice though none is held.
        "name": "gui-hide-opens-without-a-held-notice",
        "file": GUI,
        "old": (
            "                        self._first_run_notice_held\n"
            "                        and not"
        ),
        "new": (
            "                        True\n"
            "                        and not"
        ),
        "catchers": {
            NO_HELD_NOTICE: "AssertionError: Expected 'FirstRunNoticeWindow' to not have been called. Called 1 times.",
            OPENS_ONCE: "AssertionError: Expected 'show' to have been called once. Called 2 times.",
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
            errors.append(f"ERROR {mutation['name']}: pattern-ambiguous ({count} matches)")
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
    for directory in {target.parent for target in TARGETS}:
        shutil.rmtree(directory / "__pycache__", ignore_errors=True)


def _env():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "1000"  # keep short-summary messages untruncated
    return env


def _pytest(extra, timeout):
    command = [sys.executable, "-m", "pytest", *SELECTION, "-p", "no:randomly",
               "-p", "no:cacheprovider", "--junitxml=NUL" if os.name == "nt" else "--junitxml=/dev/null",
               *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _collected_names():
    result = _pytest(["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].split("[")[0].strip())
    return result.returncode, names


def _summary(output: str):
    """Map test name -> (kind, message) from pytest's short summary only."""
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
                test_id = match.group(2)
                name = test_id.split("::")[-1]
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection():
    result = _pytest(["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    if result.returncode == 0:
        return {}
    records = _summary(output)
    if result.returncode != 1:
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if not records:
        raise RuntimeError(f"pytest exit 1 with no short-summary records: {output[-1500:]}")
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
        print(f"RESTORE FAILED: {target} may still hold a mutant; check git diff", flush=True)
    _clear_bytecode()
    if held is not None:
        raise held
    if not restored:
        raise RuntimeError(f"restore failed: {target}")


def _judge(mutation, records):
    unexpected = [f"{name}: {message}" for name, (kind, message) in records.items()
                  if kind == "ERROR" or not (message.startswith("assert") or message.startswith("AssertionError"))]
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
        detail.append("other failures: " + "; ".join(f"{n}: {records[n][1]}" for n in others))
    if not green:
        return "CAUGHT", " | ".join(detail)
    return "SURVIVED", " | ".join(detail) or "no test failed"


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify patterns and compilation only")
    parser.add_argument("--only", nargs="+", default=[], metavar="NAME", help="run only these mutations")
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
    stats = {target: (target.stat().st_atime_ns, target.stat().st_mtime_ns) for target in TARGETS}
    prepared, errors = _prepare(originals, set(args.only))
    for line in errors:
        say(line)
    stale = sum("pattern-" in e for e in errors)
    broken = sum("does-not-compile" in e for e in errors)
    selected = len(prepared) + len(errors)
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, {broken} that do not compile")
    if args.check:
        return 1 if errors else 0

    rc, collected = _collected_names()
    missing = sorted({name for mutation, _ in prepared for name in mutation["catchers"]} - collected)
    if rc != 0 or not collected or missing:
        say(f"ERROR expected catcher names not collected (collect rc={rc}): {', '.join(missing) or 'none collected'}")
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
