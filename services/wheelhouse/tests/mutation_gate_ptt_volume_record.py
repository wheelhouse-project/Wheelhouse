"""Mutation gate for the push-to-talk volume record and the launcher restore (wh-ptt-mute-orphaned-on-process-loss).

A push-to-talk hold lowers the speaker level in the Logic process. Commit
c0c8420b keeps a record of the hold on disk, so that the launcher can put the
level back when the Logic process is lost during the hold. The fix for FIX
ACCEPTANCE F1 to F5 on the bead bounds the launcher's wait for that restore.
Three parts are guarded:

* utils/ptt_volume_record.py: the record is written atomically with the
  endpoint, both levels and the time. restore_orphaned_volume() writes the
  pre-hold level only for a record not older than 24 hours, on an endpoint
  that is present, at a level within LEVEL_MATCH_TOLERANCE_DB of the lowered
  level. It deletes an older record without a write. It deletes the record
  after a write, after a level that the user changed, and after a failed
  read or write of the endpoint level. It keeps the record when the endpoint
  is not present or COM cannot start. A cancel flag that is set immediately
  before the write, or immediately before the delete after an attempt,
  stops that step, keeps the record, logs a warning, and gives
  OUTCOME_CANCELLED. Catchers: tests/test_ptt_volume_record.py, and
  TestAHoldIsKeptOnDiskUntilTheLevelIsBack for the record content.
* plugins/system_volume_plugin.py: the record is written before the mute,
  written again on a wheel turn during the hold, and deleted only after the
  level is back. The release deletes it with _ptt_audio_lock held, so a slow
  delete cannot remove the record of the next hold. Catchers:
  TestAHoldIsKeptOnDiskUntilTheLevelIsBack in
  tests/test_plugins/test_system_volume_plugin.py.
* launcher.py: the restore runs after the stale cleanup and the instance
  record and before the restart loop, and after every child of a cycle has
  exited. While a child still runs, the second restore is skipped with a
  warning that names the child. The restore runs in a daemon thread; the
  launcher waits for it at most SHUTDOWN_GRACE_PERIOD_S, then sets the
  cancel flag, logs a warning that names the limit, and goes on. A restore
  that raises, or that cannot be imported, is logged and the launcher goes
  on. Catchers: TestTheLauncherRestoresTheLevelOfALostHold in
  tests/test_launcher.py.

Each mutation names its catcher tests and the start of the message each must
fail with, as pytest prints it in the short test summary. A leading
"AssertionError: " before "assert " is ignored in that comparison, because
pytest keeps or removes it according to the quote characters in the message.
A mutation is caught only when every catcher failed with its message. A
catcher that failed with another message is an error, because that verdict
would come from a failure that proves nothing. A failure of a test that is
not a catcher is printed and does not change the verdict.

Each target file has its own test selection (SELECTIONS). A mutation with
"edits" applies them in order, for example to move a line; the old text of
each edit must match exactly once.

Usage (run from services/wheelhouse with the wheelhouse interpreter):
    .venv/Scripts/python.exe tests/mutation_gate_ptt_volume_record.py --check
    .venv/Scripts/python.exe tests/mutation_gate_ptt_volume_record.py [--only NAME ...] [--log PATH]

--check verifies that every pattern matches exactly once and that every mutant
compiles, then exits. A sweep also validates the expected catcher names with
--collect-only and requires a green baseline of each selection before the
first mutation.

Exit status: 0 only when every selected mutation is caught; 1 on any
survivor or error (pattern not found, pattern ambiguous, does not compile,
timeout, suite-timeout abort, catcher failed with another message, restore
failure).

Do not run concurrently with a test suite or edits in this worktree: the gate
rewrites utils/ptt_volume_record.py, plugins/system_volume_plugin.py and
launcher.py while each mutation runs.

Mutation left out on purpose:

* _hresult without the conversion to a signed 32-bit value. This mutant is
  equivalent for every exception that ctypes and comtypes raise, because
  both put the HRESULT on the exception as a signed value. Measured
  2026-09-17 with the wheelhouse interpreter: ctypes.oledll.ole32.
  CLSIDFromString with a bad string raised OSError with winerror
  -2147221005, and IMMDeviceEnumerator.GetDevice with an unknown endpoint ID
  raised COMError with hresult -2147023728.
"""
# crewcut: the runner below is copied from
# tests/mutation_gate_audio_pause_notice.py and extended with a test selection
# per target file, edits applied in order, and a verdict read from the
# catchers only. A shared runner module imported by the gates is the way to
# remove the duplication later.
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
RECORD = SERVICE / "utils" / "ptt_volume_record.py"
PLUGIN = SERVICE / "plugins" / "system_volume_plugin.py"
LAUNCHER = SERVICE / "launcher.py"
TARGETS = (RECORD, PLUGIN, LAUNCHER)

RECORD_TESTS = "tests/test_ptt_volume_record.py"
PLUGIN_TESTS = "tests/test_plugins/test_system_volume_plugin.py"
LAUNCHER_TESTS = "tests/test_launcher.py"
SELECTIONS = {
    RECORD: (RECORD_TESTS, f"{PLUGIN_TESTS}::TestAHoldIsKeptOnDiskUntilTheLevelIsBack"),
    PLUGIN: (PLUGIN_TESTS,),
    LAUNCHER: (LAUNCHER_TESTS,),
}
# Measured 2026-09-17: a clean run of each of the three test files took less
# than 3 s of wall time. A mutant that waits for the launcher's restore
# without a limit adds 2 s per blocked-restore test, because each blocked
# restore in those tests has its own 2 s timeout.
RUN_TIMEOUT = 180

# tests/test_ptt_volume_record.py
WRITTEN_HOLDS = "test_a_written_record_holds_the_endpoint_both_levels_and_the_time"
READS_BACK = "test_a_written_record_reads_back"
SYNCED = "test_the_write_is_synced_to_a_file_beside_the_record_and_then_replaces_it"
FAILED_REPLACE = "test_a_failed_replace_keeps_the_previous_record_and_leaves_no_temporary_file"
AT_LEVEL = "test_a_level_still_at_the_lowered_level_is_restored_and_the_record_deleted"
USER_CHANGED = "test_a_level_the_user_changed_is_left_alone_and_the_record_deleted"
ABSENT = "test_an_absent_endpoint_keeps_the_record"
OLDER = "test_a_record_older_than_24_hours_is_deleted_without_a_write"
JUST_UNDER = "test_a_record_just_under_24_hours_old_is_still_restored"
EXACTLY_A_DAY = "test_a_record_exactly_24_hours_old_is_still_restored"
WITHIN_UP = "test_a_level_within_1_db_of_the_lowered_level_counts_as_still_lowered[0.9]"
WITHIN_DOWN = "test_a_level_within_1_db_of_the_lowered_level_counts_as_still_lowered[-0.9]"
AT_TOLERANCE_UP = "test_a_level_within_1_db_of_the_lowered_level_counts_as_still_lowered[1.0]"
AT_TOLERANCE_DOWN = "test_a_level_within_1_db_of_the_lowered_level_counts_as_still_lowered[-1.0]"
BEYOND_UP = "test_a_level_more_than_1_db_from_the_lowered_level_counts_as_changed[1.1]"
BEYOND_DOWN = "test_a_level_more_than_1_db_from_the_lowered_level_counts_as_changed[-1.1]"
NOT_ACTIVE = "test_an_endpoint_that_is_not_active_keeps_the_record"
REMOVED = "test_an_endpoint_removed_during_the_restore_keeps_the_record"
WRITE_FAILS = "test_a_write_that_fails_still_deletes_the_record"
COM_CANNOT_START = "test_com_that_cannot_start_keeps_the_record"
COM_NOT_OURS = "test_com_this_call_did_not_start_is_not_uninitialised"
OWES_S_FALSE = "test_the_result_says_whether_the_call_owes_an_uninitialise[1-True]"
OWES_CHANGED_MODE = "test_the_result_says_whether_the_call_owes_an_uninitialise[-2147417850-False]"

# TestARestoreCancelledAtTheLaunchersTimeLimit in tests/test_ptt_volume_record.py
CANCEL_BEFORE_WRITE = "test_a_flag_set_before_the_write_stops_the_write_and_keeps_the_record"
CANCEL_BEFORE_DELETE_RESTORED = "test_a_flag_set_before_the_delete_keeps_the_record[restored]"
CANCEL_BEFORE_DELETE_CHANGED = "test_a_flag_set_before_the_delete_keeps_the_record[level-changed]"
CANCEL_BEFORE_DELETE_FAILED = "test_a_flag_set_before_the_delete_keeps_the_record[failed]"

# TestAHoldIsKeptOnDiskUntilTheLevelIsBack in tests/test_plugins/test_system_volume_plugin.py
ON_DISK_FIRST = "test_the_record_is_on_disk_before_the_speakers_are_turned_down"
WHEEL_TURN = "test_a_wheel_turn_during_the_hold_updates_the_record"
DELETED_AFTER = "test_the_record_is_deleted_after_the_level_is_back_and_not_before"
RESTORE_RAISES = "test_a_restore_that_raises_keeps_the_record"
NO_DEVICE = "test_a_release_with_no_audio_device_keeps_the_record"
CANNOT_WRITE = "test_a_record_that_cannot_be_written_is_logged_and_the_mute_goes_on"
WRITE_RAISES = "test_a_record_write_that_raises_is_logged_and_the_mute_goes_on"
SLOW_DELETE = "test_a_release_whose_record_delete_is_slow_cannot_delete_the_record_of_the_next_hold"

# TestTheLauncherRestoresTheLevelOfALostHold in tests/test_launcher.py
FIRST_START = (
    "test_the_start_restore_runs_after_the_stale_cleanup_and_the_instance_record_and_before_the_first_child_starts"
)
START_BLOCKS = "test_a_start_restore_that_does_not_return_is_left_behind_at_the_time_limit"
CLEANUP_BLOCKS = (
    "test_a_cleanup_restore_that_does_not_return_is_left_behind_and_the_restart_decision_still_runs"
)
EVERY_EXIT = "test_the_restore_runs_after_every_child_has_exited_and_before_the_restart_decision"
TERMINATE = "test_a_child_that_needed_terminate_is_joined_again_before_the_restore"
STILL_RUNNING = "test_the_restore_is_skipped_while_a_child_is_still_running"
RAISES = "test_a_restore_that_raises_is_logged_and_the_launcher_goes_on"
NOT_IMPORTED = "test_a_restore_that_cannot_be_imported_is_logged_and_the_launcher_goes_on"

NOT_AT_DISK = "AssertionError: the speakers were turned down before the record was on disk"
DELETED_NOT_RESTORED = "AssertionError: the record was deleted although the level was not put back"
START_ORDER = (
    "AssertionError: expected the stale cleanup, the instance record and then the restore "
    "before the first child starts, got "
)
NO_START_RESTORE = START_ORDER + "[('cleanup_stale',), ('instance record written',)]"
RESTORE_BEFORE_RECORD = START_ORDER + "[('cleanup_stale',), ('restore',), ('instance record written',)]"
START_WAITED = "AssertionError: the launcher waited past the time limit for the start restore"
CLEANUP_WAITED = "AssertionError: the launcher waited past the time limit for the cleanup restore"
WROTE_AFTER_CANCEL = "AssertionError: the restore wrote the level after the cancel flag was set"
DELETED_AFTER_CANCEL = "AssertionError: the restore deleted the record after the cancel flag was set"
CANCEL_NOT_LOGGED = """assert "the launcher's time limit" in """
NOT_BOTH_POINTS = "AssertionError: the launcher did not reach both restore points"
RESTORE_ERROR = "RuntimeError: the audio service is not running"
START_SITE = (
    "    # that process runs.\n"
    "    restore_orphaned_ptt_volume()\n"
)
CLEANUP_SITE = (
    "            else:\n"
    "                restore_orphaned_ptt_volume()\n"
)
JOIN_AFTER_TERMINATE = (
    "                # while the Logic process can still hold the speakers down.\n"
    "                p.join(timeout=SHUTDOWN_GRACE_PERIOD_S)\n"
)
DELETE_TUPLE = "(OUTCOME_RESTORED, OUTCOME_LEVEL_CHANGED, OUTCOME_FAILED)"
LEVEL_CHECK = "abs(current_db - record.lowered_db) <= LEVEL_MATCH_TOLERANCE_DB"
LOWERED_FIELD = '                "lowered_db": float(lowered_db),\n'
LEVEL_READ = "        current_db = volume.GetMasterVolumeLevel()\n"
WRITE_CHECK = (
    "        if _cancelled(cancel):\n"
    "            logger.warning(\n"
    """                "Push-to-talk speaker level not restored: the launcher's time limit for "\n"""
)
WRITE_CHECK_BLOCK = WRITE_CHECK + (
    """                "the restore ended before the write. The record is kept."\n"""
    "            )\n"
    "            return OUTCOME_CANCELLED\n"
)
WRITE_AFTER_CHECK = (
    "            return OUTCOME_CANCELLED\n"
    "        volume.SetMasterVolumeLevel(record.pre_hold_db, None)\n"
)
DELETE_CHECK = (
    "            if _cancelled(cancel):\n"
    "                logger.warning(\n"
    '                    "The push-to-talk volume record is kept after the outcome %s: the "\n'
)
DELETE_AFTER_CHECK = (
    "                return OUTCOME_CANCELLED\n"
    "            delete_record(path=target)\n"
)
BOUNDED_WAIT = "    worker.join(SHUTDOWN_GRACE_PERIOD_S)\n"

MUTATIONS = [
    # ------------------------------------------------------------------
    # plugins/system_volume_plugin.py
    # ------------------------------------------------------------------
    {
        # Required: the record write and the device write change places, so a
        # process lost between them leaves the speakers down with no record.
        "name": "record-written-after-the-mute",
        "file": PLUGIN,
        "old": (
            "                await self._write_ptt_volume_record(current_db)\n"
            "                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, self._min_volume_db, None)\n"
        ),
        "new": (
            "                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, self._min_volume_db, None)\n"
            "                await self._write_ptt_volume_record(current_db)\n"
        ),
        "catchers": {ON_DISK_FIRST: NOT_AT_DISK},
    },
    {
        # Required: a wheel turn during the hold no longer updates the record,
        # so a restore after a lost process puts back the level from before
        # the turn.
        "name": "wheel-turn-leaves-the-record-unchanged",
        "file": PLUGIN,
        "old": "                await self._write_ptt_volume_record(new_db)\n",
        "new": "                pass\n",
        "catchers": {
            WHEEL_TURN: "AssertionError: the record still holds the level from before the wheel turn",
        },
    },
    {
        # The wheel turn writes the record with the level from before the turn.
        "name": "wheel-turn-records-the-level-before-the-turn",
        "file": PLUGIN,
        "old": "                await self._write_ptt_volume_record(new_db)\n",
        "new": "                await self._write_ptt_volume_record(held_db)\n",
        "catchers": {
            WHEEL_TURN: "AssertionError: the record still holds the level from before the wheel turn",
        },
    },
    {
        # Required: the delete moves from after the restore write to before
        # it. A process lost between the two leaves the speakers down with no
        # record, and a restore write that raises finds the record gone.
        "name": "record-deleted-before-the-restore-write",
        "file": PLUGIN,
        "edits": [
            (
                "                self._pre_ptt_volume_db = None\n"
                "                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, restore_db, None)\n",
                "                self._pre_ptt_volume_db = None\n"
                "                await self._delete_ptt_volume_record()\n"
                "                await asyncio.to_thread(self._volume_interface.SetMasterVolumeLevel, restore_db, None)\n",
            ),
            (
                "            else:\n"
                "                await self._delete_ptt_volume_record()\n",
                "",
            ),
        ],
        "catchers": {
            DELETED_AFTER: "AssertionError: the record was gone before the level was put back",
            RESTORE_RAISES: DELETED_NOT_RESTORED,
        },
    },
    {
        # Required: the delete runs also when the restore write raised.
        "name": "record-deleted-when-the-restore-write-fails",
        "file": PLUGIN,
        "old": (
            "            else:\n"
            "                await self._delete_ptt_volume_record()\n"
        ),
        "new": (
            "            finally:\n"
            "                await self._delete_ptt_volume_record()\n"
        ),
        "catchers": {RESTORE_RAISES: DELETED_NOT_RESTORED},
    },
    {
        # Required: a release with no audio device deletes the record,
        # although nothing put the level back.
        "name": "record-deleted-with-no-audio-device",
        "file": PLUGIN,
        "old": '                logger.warning("Cannot restore volume after PTT -- audio device not connected")\n',
        "new": (
            '                logger.warning("Cannot restore volume after PTT -- audio device not connected")\n'
            "                await self._delete_ptt_volume_record()\n"
        ),
        "catchers": {NO_DEVICE: DELETED_NOT_RESTORED},
    },
    {
        # Record content, plugin side: the plugin gives the pre-hold level as
        # the lowered level, so the restore finds the level "changed" and
        # never writes.
        "name": "plugin-records-the-pre-hold-level-as-the-lowered-level",
        "file": PLUGIN,
        "old": (
            "                pre_hold_db,\n"
            "                self._min_volume_db,\n"
        ),
        "new": (
            "                pre_hold_db,\n"
            "                pre_hold_db,\n"
        ),
        "catchers": {
            ON_DISK_FIRST: "assert ('synthetic-p... -10.0, -10.0) == ",
            WHEEL_TURN: "assert -4.0 == -65.25",
        },
    },
    {
        # The plugin records an endpoint other than the one it lowers.
        "name": "plugin-records-another-endpoint",
        "file": PLUGIN,
        "old": (
            "                self._audio_device_id,\n"
            "                pre_hold_db,\n"
        ),
        "new": (
            '                "another-endpoint",\n'
            "                pre_hold_db,\n"
        ),
        "catchers": {ON_DISK_FIRST: "assert ('another-end"},
    },
    {
        # A record that was not written is no longer reported in the log.
        "name": "unwritten-record-not-logged",
        "file": PLUGIN,
        "old": "        if not written:\n",
        "new": "        if False:\n",
        "catchers": {CANNOT_WRITE: "assert 'push-to-talk"},
    },
    {
        # An exception from the record write is no longer contained, so it
        # reaches _mute_for_ptt and the mute does not happen.
        "name": "record-write-exception-escapes",
        "file": PLUGIN,
        "old": (
            "        except Exception as e:\n"
            '            logger.warning("[PTT] Writing the push-to-talk volume record raised: %s", e)\n'
        ),
        "new": (
            "        except ZeroDivisionError as e:\n"
            '            logger.warning("[PTT] Writing the push-to-talk volume record raised: %s", e)\n'
        ),
        "catchers": {
            WRITE_RAISES: "AssertionError: Expected 'SetMasterVolumeLevel' to be called once. Called 0 times.",
        },
    },
    {
        # Required (F5): the release deletes the record after it releases
        # _ptt_audio_lock, so a slow delete can remove the record that the
        # next press wrote. A Logic process lost during that hold then leaves
        # the speakers down with no record.
        "name": "release-record-delete-outside-the-lock",
        "file": PLUGIN,
        "edits": [
            (
                "        async with self._ptt_audio_lock:\n"
                "            if self._pre_ptt_volume_db is None:\n"
                "                return  # Nothing to restore\n",
                "        restored = False\n"
                "        async with self._ptt_audio_lock:\n"
                "            if self._pre_ptt_volume_db is None:\n"
                "                return  # Nothing to restore\n",
            ),
            (
                "            else:\n"
                "                await self._delete_ptt_volume_record()\n",
                "            else:\n"
                "                restored = True\n"
                "        if restored:\n"
                "            await self._delete_ptt_volume_record()\n",
            ),
        ],
        "catchers": {SLOW_DELETE: "AssertionError: the release deleted the record of the next hold"},
    },
    # ------------------------------------------------------------------
    # utils/ptt_volume_record.py: the restore
    # ------------------------------------------------------------------
    {
        # Required: the level check is removed, so the restore writes over a
        # level that the user set after the loss.
        "name": "level-check-removed",
        "file": RECORD,
        "old": f"        if not {LEVEL_CHECK}:\n",
        "new": "        if False:\n",
        "catchers": {
            USER_CHANGED: "assert [-10.0] == []",
            BEYOND_UP: "assert [-10.0] == []",
            BEYOND_DOWN: "assert [-10.0] == []",
        },
    },
    {
        # Required: the direction of the tolerance comparison is flipped.
        "name": "level-check-comparison-flipped",
        "file": RECORD,
        "old": LEVEL_CHECK,
        "new": "abs(current_db - record.lowered_db) >= LEVEL_MATCH_TOLERANCE_DB",
        "catchers": {
            AT_LEVEL: "assert [] == [-10.0]",
            USER_CHANGED: "assert [-10.0] == []",
            WITHIN_UP: "assert [] == [-10.0]",
            WITHIN_DOWN: "assert [] == [-10.0]",
            BEYOND_UP: "assert [-10.0] == []",
            BEYOND_DOWN: "assert [-10.0] == []",
        },
    },
    {
        # Required: a tolerance of zero.
        "name": "level-tolerance-zero",
        "file": RECORD,
        "old": "LEVEL_MATCH_TOLERANCE_DB = 1.0\n",
        "new": "LEVEL_MATCH_TOLERANCE_DB = 0.0\n",
        "catchers": {
            WITHIN_UP: "assert [] == [-10.0]",
            WITHIN_DOWN: "assert [] == [-10.0]",
        },
    },
    {
        # A tolerance of 2.0 dB.
        "name": "level-tolerance-two-db",
        "file": RECORD,
        "old": "LEVEL_MATCH_TOLERANCE_DB = 1.0\n",
        "new": "LEVEL_MATCH_TOLERANCE_DB = 2.0\n",
        "catchers": {
            BEYOND_UP: "assert [-10.0] == []",
            BEYOND_DOWN: "assert [-10.0] == []",
        },
    },
    {
        # A level exactly LEVEL_MATCH_TOLERANCE_DB from the lowered level no
        # longer counts as the lowered level.
        "name": "level-tolerance-limit-excluded",
        "file": RECORD,
        "old": LEVEL_CHECK,
        "new": "abs(current_db - record.lowered_db) < LEVEL_MATCH_TOLERANCE_DB",
        "catchers": {
            AT_TOLERANCE_UP: "assert [] == [-10.0]",
            AT_TOLERANCE_DOWN: "assert [] == [-10.0]",
        },
    },
    {
        # The difference is compared without abs(), so a level below the
        # lowered level always counts as the lowered level.
        "name": "level-difference-without-abs",
        "file": RECORD,
        "old": f"        if not {LEVEL_CHECK}:\n",
        "new": "        if not (current_db - record.lowered_db) <= LEVEL_MATCH_TOLERANCE_DB:\n",
        "catchers": {BEYOND_DOWN: "assert [-10.0] == []"},
    },
    {
        # The level is compared with the pre-hold level.
        "name": "level-compared-with-the-pre-hold-level",
        "file": RECORD,
        "old": LEVEL_CHECK,
        "new": "abs(current_db - record.pre_hold_db) <= LEVEL_MATCH_TOLERANCE_DB",
        "catchers": {AT_LEVEL: "assert [] == [-10.0]"},
    },
    {
        # The restore writes the lowered level back.
        "name": "restore-writes-the-lowered-level",
        "file": RECORD,
        "old": "        volume.SetMasterVolumeLevel(record.pre_hold_db, None)\n",
        "new": "        volume.SetMasterVolumeLevel(record.lowered_db, None)\n",
        "catchers": {
            AT_LEVEL: "assert [-65.25] == [-10.0]",
            JUST_UNDER: "assert [-65.25] == [-10.0]",
        },
    },
    {
        # The restore opens an endpoint other than the recorded one.
        "name": "restore-opens-another-endpoint",
        "file": RECORD,
        "old": "        volume = _open_endpoint_volume(enumerator, record.endpoint_id)\n",
        "new": '        volume = _open_endpoint_volume(enumerator, record.endpoint_id + "-other")\n',
        "catchers": {AT_LEVEL: "assert [] == [-10.0]"},
    },
    {
        # Required: the delete after an attempt is removed. The pattern starts
        # at the cancel check's return, which now stands between the outcome
        # test and the delete.
        "name": "record-kept-after-an-attempt",
        "file": RECORD,
        "old": DELETE_AFTER_CHECK,
        "new": (
            "                return OUTCOME_CANCELLED\n"
            "            pass\n"
        ),
        "catchers": {
            AT_LEVEL: "assert not True",
            USER_CHANGED: "assert not True",
            WRITE_FAILS: "assert not True",
            BEYOND_UP: "assert not True",
        },
    },
    {
        # A restored record is kept.
        "name": "restored-record-kept",
        "file": RECORD,
        "old": DELETE_TUPLE,
        "new": "(OUTCOME_LEVEL_CHANGED, OUTCOME_FAILED)",
        "catchers": {AT_LEVEL: "assert not True"},
    },
    {
        # The record of a level the user changed is kept.
        "name": "changed-level-record-kept",
        "file": RECORD,
        "old": DELETE_TUPLE,
        "new": "(OUTCOME_RESTORED, OUTCOME_FAILED)",
        "catchers": {
            USER_CHANGED: "assert not True",
            BEYOND_UP: "assert not True",
            BEYOND_DOWN: "assert not True",
        },
    },
    {
        # The record of a failed attempt is kept.
        "name": "failed-attempt-record-kept",
        "file": RECORD,
        "old": DELETE_TUPLE,
        "new": "(OUTCOME_RESTORED, OUTCOME_LEVEL_CHANGED)",
        "catchers": {WRITE_FAILS: "assert not True"},
    },
    {
        # Required: the absent-endpoint outcome deletes the record, so a
        # restore while the endpoint is away loses the hold.
        "name": "absent-endpoint-record-deleted",
        "file": RECORD,
        "old": DELETE_TUPLE,
        "new": "(OUTCOME_RESTORED, OUTCOME_LEVEL_CHANGED, OUTCOME_FAILED, OUTCOME_ENDPOINT_ABSENT)",
        "catchers": {
            ABSENT: "assert False",
            NOT_ACTIVE: "assert False",
            REMOVED: "assert False",
        },
    },
    {
        # An endpoint that is not found or was removed counts as a failed
        # attempt, which deletes the record.
        "name": "absent-endpoint-error-counted-as-failure",
        "file": RECORD,
        "old": "        if _hresult(exc) in _ENDPOINT_ABSENT_HRESULTS:\n",
        "new": "        if False:\n",
        "catchers": {
            ABSENT: "assert 'failed' == 'endpoint_absent'",
            REMOVED: "assert 'failed' == 'endpoint_absent'",
        },
    },
    {
        # An endpoint that is not active is used as if it were active.
        "name": "inactive-endpoint-counted-as-active",
        "file": RECORD,
        "old": "    if device.GetState() != DEVICE_STATE.ACTIVE.value:\n",
        "new": "    if False:\n",
        "catchers": {NOT_ACTIVE: "assert 'restored' == 'endpoint_absent'"},
    },
    {
        # Required: the direction of the age comparison is flipped.
        "name": "age-limit-comparison-flipped",
        "file": RECORD,
        "old": "        if current_time - record.written_at > MAX_RECORD_AGE_S:\n",
        "new": "        if current_time - record.written_at < MAX_RECORD_AGE_S:\n",
        "catchers": {
            OLDER: "assert [-10.0] == []",
            JUST_UNDER: "assert [] == [-10.0]",
        },
    },
    {
        # A record exactly 24 hours old counts as older than 24 hours.
        "name": "age-limit-comparison-includes-24-hours",
        "file": RECORD,
        "old": "        if current_time - record.written_at > MAX_RECORD_AGE_S:\n",
        "new": "        if current_time - record.written_at >= MAX_RECORD_AGE_S:\n",
        "catchers": {EXACTLY_A_DAY: "assert [] == [-10.0]"},
    },
    {
        # Required: the 24-hour limit is one second longer.
        "name": "age-limit-one-second-longer",
        "file": RECORD,
        "old": "MAX_RECORD_AGE_S = 24 * 60 * 60\n",
        "new": "MAX_RECORD_AGE_S = 24 * 60 * 60 + 1\n",
        "catchers": {OLDER: "assert [-10.0] == []"},
    },
    {
        # Required: the 24-hour limit is one second shorter.
        "name": "age-limit-one-second-shorter",
        "file": RECORD,
        "old": "MAX_RECORD_AGE_S = 24 * 60 * 60\n",
        "new": "MAX_RECORD_AGE_S = 24 * 60 * 60 - 1\n",
        "catchers": {EXACTLY_A_DAY: "assert [] == [-10.0]"},
    },
    {
        # Required: the expired-record branch writes the pre-hold level
        # before it deletes the record.
        "name": "expired-record-written",
        "file": RECORD,
        "old": (
            "            delete_record(path=target)\n"
            "            return OUTCOME_STALE\n"
        ),
        "new": (
            "            _restore_on_endpoint(record)\n"
            "            delete_record(path=target)\n"
            "            return OUTCOME_STALE\n"
        ),
        "catchers": {OLDER: "assert [-10.0] == []"},
    },
    # ------------------------------------------------------------------
    # utils/ptt_volume_record.py: the cancel flag (F2)
    # ------------------------------------------------------------------
    {
        # Required: the check before the write is removed, so a restore that
        # goes on after the launcher's time limit still writes the level.
        "name": "cancel-check-before-the-write-removed",
        "file": RECORD,
        "old": WRITE_CHECK,
        "new": WRITE_CHECK.replace("if _cancelled(cancel):", "if False:"),
        "catchers": {CANCEL_BEFORE_WRITE: WROTE_AFTER_CANCEL},
    },
    {
        # Required: the check before the write moves to before the level read,
        # so a flag set during the read no longer stops the write.
        "name": "cancel-check-before-the-write-moved-before-the-level-read",
        "file": RECORD,
        "edits": [
            (WRITE_CHECK_BLOCK, ""),
            (LEVEL_READ, WRITE_CHECK_BLOCK + LEVEL_READ),
        ],
        "catchers": {CANCEL_BEFORE_WRITE: WROTE_AFTER_CANCEL},
    },
    {
        # A restore stopped before the write gives an outcome that keeps the
        # record but is not OUTCOME_CANCELLED.
        "name": "cancel-before-the-write-returns-another-outcome",
        "file": RECORD,
        "old": WRITE_AFTER_CHECK,
        "new": WRITE_AFTER_CHECK.replace("OUTCOME_CANCELLED", "OUTCOME_ENDPOINT_ABSENT"),
        "catchers": {CANCEL_BEFORE_WRITE: "assert 'endpoint_absent' == 'cancelled'"},
    },
    {
        # A restore stopped before the write logs nothing at the warning level.
        "name": "cancel-before-the-write-not-logged",
        "file": RECORD,
        "old": WRITE_CHECK,
        "new": WRITE_CHECK.replace("logger.warning(", "logger.debug("),
        "catchers": {CANCEL_BEFORE_WRITE: CANCEL_NOT_LOGGED},
    },
    {
        # Required: the check before the delete is removed, so a restore that
        # goes on after the launcher's time limit deletes the record, which by
        # then can belong to a hold of the next Logic process.
        "name": "cancel-check-before-the-delete-removed",
        "file": RECORD,
        "old": DELETE_CHECK,
        "new": DELETE_CHECK.replace("if _cancelled(cancel):", "if False:"),
        "catchers": {
            CANCEL_BEFORE_DELETE_RESTORED: DELETED_AFTER_CANCEL,
            CANCEL_BEFORE_DELETE_CHANGED: DELETED_AFTER_CANCEL,
            CANCEL_BEFORE_DELETE_FAILED: DELETED_AFTER_CANCEL,
        },
    },
    {
        # A restore stopped before the delete gives the outcome of its attempt,
        # which says the record was deleted.
        "name": "cancel-before-the-delete-returns-the-attempt-outcome",
        "file": RECORD,
        "old": DELETE_AFTER_CHECK,
        "new": DELETE_AFTER_CHECK.replace("return OUTCOME_CANCELLED", "return outcome"),
        "catchers": {
            CANCEL_BEFORE_DELETE_RESTORED: "assert 'restored' == 'cancelled'",
            CANCEL_BEFORE_DELETE_CHANGED: "assert 'level_changed' == 'cancelled'",
            CANCEL_BEFORE_DELETE_FAILED: "assert 'failed' == 'cancelled'",
        },
    },
    {
        # A restore stopped before the delete logs nothing at the warning level.
        "name": "cancel-before-the-delete-not-logged",
        "file": RECORD,
        "old": DELETE_CHECK,
        "new": DELETE_CHECK.replace("logger.warning(", "logger.debug("),
        "catchers": {
            CANCEL_BEFORE_DELETE_RESTORED: CANCEL_NOT_LOGGED,
            CANCEL_BEFORE_DELETE_CHANGED: CANCEL_NOT_LOGGED,
            CANCEL_BEFORE_DELETE_FAILED: CANCEL_NOT_LOGGED,
        },
    },
    {
        # Required: restore_orphaned_volume does not pass the flag on, so the
        # check before the write never sees it.
        "name": "cancel-flag-not-passed-to-the-endpoint-restore",
        "file": RECORD,
        "old": "        outcome = _restore_on_endpoint(record, cancel)\n",
        "new": "        outcome = _restore_on_endpoint(record)\n",
        "catchers": {CANCEL_BEFORE_WRITE: WROTE_AFTER_CANCEL},
    },
    {
        # _restore_on_endpoint does not pass the flag on.
        "name": "cancel-flag-not-passed-to-the-com-restore",
        "file": RECORD,
        "old": "        return _restore_with_com(record, cancel)\n",
        "new": "        return _restore_with_com(record)\n",
        "catchers": {CANCEL_BEFORE_WRITE: WROTE_AFTER_CANCEL},
    },
    # ------------------------------------------------------------------
    # utils/ptt_volume_record.py: COM
    # ------------------------------------------------------------------
    {
        # COM is never uninitialised after the restore.
        "name": "com-not-uninitialised",
        "file": RECORD,
        "old": (
            "        if owes_uninitialize:\n"
            "            _uninitialize_com()\n"
        ),
        "new": (
            "        if False:\n"
            "            _uninitialize_com()\n"
        ),
        "catchers": {
            AT_LEVEL: "assert ['initialize'] == ['initialize', 'uninitialize']",
            WRITE_FAILS: "assert ['initialize'] == ['initialize', 'uninitialize']",
        },
    },
    {
        # COM that could not start is used anyway.
        "name": "com-unavailable-ignored",
        "file": RECORD,
        "old": (
            "    if owes_uninitialize is None:\n"
            "        return OUTCOME_COM_UNAVAILABLE\n"
        ),
        "new": (
            "    if False:\n"
            "        return OUTCOME_COM_UNAVAILABLE\n"
        ),
        "catchers": {COM_CANNOT_START: "assert 'restored' == 'com_unavailable'"},
    },
    {
        # COM that this call did not start is uninitialised.
        "name": "com-uninitialised-when-not-owed",
        "file": RECORD,
        "old": "        if owes_uninitialize:\n",
        "new": "        if owes_uninitialize is not None:\n",
        "catchers": {COM_NOT_OURS: "assert ['initialize', 'uninitialize'] == ['initialize']"},
    },
    {
        # S_FALSE (COM already started in this mode) counts as COM not started.
        "name": "s-false-counted-as-not-started",
        "file": RECORD,
        "old": "    if hresult in (_S_OK, _S_FALSE):\n",
        "new": "    if hresult == _S_OK:\n",
        "catchers": {OWES_S_FALSE: "assert None is True"},
    },
    {
        # RPC_E_CHANGED_MODE (COM started in the other mode) counts as COM not started.
        "name": "changed-mode-counted-as-not-started",
        "file": RECORD,
        "old": (
            "    if hresult == _RPC_E_CHANGED_MODE:\n"
            "        return False\n"
        ),
        "new": (
            "    if hresult == _RPC_E_CHANGED_MODE:\n"
            "        return None\n"
        ),
        "catchers": {OWES_CHANGED_MODE: "assert None is False"},
    },
    # ------------------------------------------------------------------
    # utils/ptt_volume_record.py: the record file
    # ------------------------------------------------------------------
    {
        # Required (record content): the lowered level is left out of the
        # record, so the restore cannot use the record.
        "name": "record-without-the-lowered-level",
        "file": RECORD,
        "old": LOWERED_FIELD,
        "new": "",
        "catchers": {
            WRITTEN_HOLDS: "assert {'endpoint_id",
            READS_BACK: "assert None == PttVolumeReco",
            ON_DISK_FIRST: NOT_AT_DISK,
            WHEEL_TURN: "assert None is not None",
        },
    },
    {
        # Required (record content): the pre-hold level is stored in place of
        # the lowered level.
        "name": "record-stores-the-pre-hold-level-as-the-lowered-level",
        "file": RECORD,
        "old": LOWERED_FIELD,
        "new": '                "lowered_db": float(pre_hold_db),\n',
        "catchers": {
            WRITTEN_HOLDS: "assert {'endpoint_id",
            READS_BACK: "assert PttVolumeReco",
            ON_DISK_FIRST: "assert ('synthetic-p... -10.0, -10.0) == ",
            WHEEL_TURN: "assert -4.0 == -65.25",
        },
    },
    {
        # The record is read back with the pre-hold level as the lowered level.
        "name": "load-reads-the-pre-hold-level-as-the-lowered-level",
        "file": RECORD,
        "old": '            lowered_db=fields["lowered_db"],\n',
        "new": '            lowered_db=fields["pre_hold_db"],\n',
        "catchers": {
            READS_BACK: "assert PttVolumeReco",
            AT_LEVEL: "assert [] == [-10.0]",
            ON_DISK_FIRST: "assert ('synthetic-p... -10.0, -10.0) == ",
            WHEEL_TURN: "assert -4.0 == -65.25",
        },
    },
    {
        # Required: the atomic write. The record is written straight to its
        # path, with no temporary file and no os.replace, so a process lost
        # part way through the write leaves a partial record. Included
        # because SYNCED and FAILED_REPLACE claim to guard the atomic write.
        "name": "record-written-straight-to-its-path",
        "file": RECORD,
        "old": (
            "        with tempfile.NamedTemporaryFile(\n"
            '            mode="wb",\n'
            "            delete=False,\n"
            "            dir=directory,\n"
            '            prefix=name + ".",\n'
            '            suffix=".tmp",\n'
            "        ) as handle:\n"
            "            temp_path = handle.name\n"
            "            handle.write(payload)\n"
            "            handle.flush()\n"
            "            os.fsync(handle.fileno())\n"
            "        os.replace(temp_path, target)\n"
        ),
        "new": (
            '        with open(target, "wb") as handle:\n'
            "            handle.write(payload)\n"
            "            handle.flush()\n"
            "            os.fsync(handle.fileno())\n"
        ),
        "catchers": {
            SYNCED: "assert ['fsync'] == ['fsync', ('r",
            FAILED_REPLACE: "assert True is False",
        },
    },
    {
        # The temporary file is made in the system temporary directory, not
        # beside the record, so os.replace can cross volumes.
        "name": "temporary-file-in-another-directory",
        "file": RECORD,
        "old": "            dir=directory,\n",
        "new": "            dir=None,\n",
        "catchers": {SYNCED: "assert ['fsync', ('r... False, True)] == "},
    },
    {
        # The temporary file is not synced before the replace.
        "name": "write-not-synced",
        "file": RECORD,
        "old": "            os.fsync(handle.fileno())\n",
        "new": "            pass\n",
        "catchers": {SYNCED: "assert [('replace', True, True)] == "},
    },
    # ------------------------------------------------------------------
    # launcher.py
    # ------------------------------------------------------------------
    {
        # Required: the restore at launcher start is removed.
        "name": "start-restore-removed",
        "file": LAUNCHER,
        "old": START_SITE,
        "new": (
            "    # that process runs.\n"
            "    pass\n"
        ),
        "catchers": {
            FIRST_START: NO_START_RESTORE,
            RAISES: NOT_BOTH_POINTS,
        },
    },
    {
        # Required: the restore at launcher start moves to after the first
        # child starts.
        "name": "start-restore-after-the-first-child-starts",
        "file": LAUNCHER,
        "edits": [
            (START_SITE, "    # that process runs.\n"),
            (
                "            logic_proc.start()\n",
                "            logic_proc.start()\n"
                "            restore_orphaned_ptt_volume()\n",
            ),
        ],
        "catchers": {
            FIRST_START: NO_START_RESTORE,
            EVERY_EXIT: (
                "AssertionError: got [('start', 'LogicProcess'), "
                "('restore', 'restart flag on disk', ('LogicProcess',)), ('start', 'InputProcess')"
            ),
        },
    },
    {
        # Required: the restore after the children exit is removed.
        "name": "cleanup-restore-removed",
        "file": LAUNCHER,
        "old": CLEANUP_SITE,
        "new": (
            "            else:\n"
            "                pass\n"
        ),
        "catchers": {
            EVERY_EXIT: (
                "AssertionError: got [('start', 'LogicProcess'), ('start', 'InputProcess'), "
                "('start', 'GuiProcess'), ('join', 'LogicProcess'), ('join', 'InputProcess'), "
                "('join', 'GuiProcess'), ('start', 'LogicProcess')"
            ),
            TERMINATE: (
                "AssertionError: got [('join', 'LogicProcess'), ('join', 'InputProcess'), "
                "('join', 'GuiProcess'), ('terminate', 'LogicProcess'), ('join', 'LogicProcess')]"
            ),
            RAISES: NOT_BOTH_POINTS,
        },
    },
    {
        # Required: the restore after the children exit moves to before the
        # child joins.
        "name": "cleanup-restore-before-the-child-joins",
        "file": LAUNCHER,
        "edits": [
            (
                "            procs_to_join = [p for p in [logic_proc, input_proc, gui_proc] if p and p.is_alive()]\n",
                "            restore_orphaned_ptt_volume()\n"
                "            procs_to_join = [p for p in [logic_proc, input_proc, gui_proc] if p and p.is_alive()]\n",
            ),
            (
                CLEANUP_SITE,
                "            else:\n"
                "                pass\n",
            ),
        ],
        "catchers": {
            EVERY_EXIT: (
                "AssertionError: got [('start', 'LogicProcess'), ('start', 'InputProcess'), "
                "('start', 'GuiProcess'), "
                "('restore', 'restart flag on disk', ('LogicProcess', 'InputProcess', 'GuiProcess'))"
            ),
            TERMINATE: (
                "AssertionError: got [('restore', ('LogicProcess', 'InputProcess', 'GuiProcess')), "
                "('join', 'LogicProcess')"
            ),
            STILL_RUNNING: "AssertionError: the restore ran while LogicProcess was still running",
        },
    },
    {
        # Required: the join after terminate is removed.
        "name": "join-after-terminate-removed",
        "file": LAUNCHER,
        "old": JOIN_AFTER_TERMINATE,
        "new": (
            "                # while the Logic process can still hold the speakers down.\n"
            "                pass\n"
        ),
        "catchers": {
            TERMINATE: (
                "AssertionError: got [('join', 'LogicProcess'), ('join', 'InputProcess'), "
                "('join', 'GuiProcess'), ('terminate', 'LogicProcess')]"
            ),
        },
    },
    {
        # The join after terminate does not wait.
        "name": "join-after-terminate-does-not-wait",
        "file": LAUNCHER,
        "old": JOIN_AFTER_TERMINATE,
        "new": (
            "                # while the Logic process can still hold the speakers down.\n"
            "                p.join(timeout=0)\n"
        ),
        "catchers": {TERMINATE: "AssertionError: expected call not found."},
    },
    {
        # The restore runs while a child is still running.
        "name": "restore-runs-while-a-child-still-runs",
        "file": LAUNCHER,
        "old": "            if still_running:\n",
        "new": "            if False:\n",
        "catchers": {
            STILL_RUNNING: "AssertionError: no warning named the child that kept the restore from running",
        },
    },
    {
        # The restore thread loses its exception handling, so a restore that
        # raises is not logged. The pattern moved to the thread body when the
        # restore moved to its own thread; the launcher thread goes on either
        # way, so the catcher is the missing log message.
        "name": "restore-wrapper-exception-propagates",
        "file": LAUNCHER,
        "old": (
            "    try:\n"
            "        restore(cancel=cancel)\n"
            "    except Exception as exc:\n"
            "        logger.error(\n"
            '            "Could not restore the push-to-talk speaker level: %s", exc, exc_info=True,\n'
            "        )\n"
        ),
        "new": "    restore(cancel=cancel)\n",
        "catchers": {RAISES: "assert 'Could not restore the push-to-talk speaker level' in "},
    },
    {
        # Required: the call at launcher start has no exception handling, so
        # a restore that raises stops the launcher before any child starts.
        # The import is aliased so that it binds no name that the function
        # already uses.
        "name": "start-restore-exception-propagates",
        "file": LAUNCHER,
        "old": START_SITE,
        "new": (
            "    # that process runs.\n"
            "    from services.wheelhouse.utils.ptt_volume_record import restore_orphaned_volume as _unguarded_restore\n"
            "    _unguarded_restore()\n"
        ),
        "catchers": {RAISES: RESTORE_ERROR},
    },
    {
        # Required: the call after the children exit has no exception
        # handling, so a restore that raises stops the launcher in its
        # cleanup phase.
        "name": "cleanup-restore-exception-propagates",
        "file": LAUNCHER,
        "old": CLEANUP_SITE,
        "new": (
            "            else:\n"
            "                from services.wheelhouse.utils.ptt_volume_record import restore_orphaned_volume as _unguarded_restore\n"
            "                _unguarded_restore()\n"
        ),
        "catchers": {RAISES: RESTORE_ERROR},
    },
    # ------------------------------------------------------------------
    # launcher.py: the time limit (F1) and the start order (F3)
    # ------------------------------------------------------------------
    {
        # Required (F1): the launcher waits for the restore thread without a
        # limit, so a Core Audio call that does not return holds the launcher
        # at both call sites.
        "name": "restore-waits-without-a-time-limit",
        "file": LAUNCHER,
        "old": BOUNDED_WAIT,
        "new": "    worker.join()\n",
        "catchers": {START_BLOCKS: START_WAITED, CLEANUP_BLOCKS: CLEANUP_WAITED},
    },
    {
        # Required (F1): the call at launcher start runs the restore on the
        # launcher thread with no limit. The exception handling stays.
        "name": "start-restore-without-a-time-limit",
        "file": LAUNCHER,
        "old": START_SITE,
        "new": (
            "    # that process runs.\n"
            "    from services.wheelhouse.utils.ptt_volume_record import restore_orphaned_volume as _unbounded_restore\n"
            "    _run_ptt_volume_restore(_unbounded_restore, threading.Event())\n"
        ),
        "catchers": {START_BLOCKS: START_WAITED},
    },
    {
        # Required (F1): the call after the children exit runs the restore on
        # the launcher thread with no limit. The exception handling stays.
        "name": "cleanup-restore-without-a-time-limit",
        "file": LAUNCHER,
        "old": CLEANUP_SITE,
        "new": (
            "            else:\n"
            "                from services.wheelhouse.utils.ptt_volume_record import restore_orphaned_volume as _unbounded_restore\n"
            "                _run_ptt_volume_restore(_unbounded_restore, threading.Event())\n"
        ),
        "catchers": {CLEANUP_BLOCKS: CLEANUP_WAITED},
    },
    {
        # Required (F1): the restore thread is not a daemon thread, so a Core
        # Audio call that does not return keeps the launcher process alive at
        # exit.
        "name": "restore-thread-not-daemon",
        "file": LAUNCHER,
        "old": '            name="PttVolumeRestore",\n            daemon=True,\n',
        "new": '            name="PttVolumeRestore",\n            daemon=False,\n',
        "catchers": {START_BLOCKS: "AssertionError: the start restore did not run in a daemon thread"},
    },
    {
        # Required (F1): the cancel flag is not set at the time limit, so a
        # restore that goes on still writes the level and deletes the record.
        "name": "cancel-flag-not-set-at-the-time-limit",
        "file": LAUNCHER,
        "old": "        cancel.set()\n",
        "new": "        pass\n",
        "catchers": {
            START_BLOCKS: (
                "AssertionError: the launcher did not set the cancel flag of the start restore "
                "at the time limit"
            ),
        },
    },
    {
        # Required (F1): the thread calls the restore without the cancel flag.
        "name": "restore-thread-gets-no-cancel-flag",
        "file": LAUNCHER,
        "old": "        restore(cancel=cancel)\n",
        "new": "        restore()\n",
        "catchers": {START_BLOCKS: "AssertionError: the launcher gave the start restore no cancel flag"},
    },
    {
        # F1: the warning at the time limit names a fixed 5 s, not the limit
        # that the launcher used.
        "name": "time-limit-warning-names-a-fixed-limit",
        "file": LAUNCHER,
        "old": (
            '            "write or record delete.",\n'
            "            SHUTDOWN_GRACE_PERIOD_S,\n"
        ),
        "new": (
            '            "write or record delete.",\n'
            "            5,\n"
        ),
        "catchers": {START_BLOCKS: "AssertionError: no warning named the time limit"},
    },
    {
        # F1: the message at the time limit is logged below the warning level.
        "name": "time-limit-warning-logged-at-debug",
        "file": LAUNCHER,
        "old": (
            "        logger.warning(\n"
            '            "The push-to-talk speaker restore did not finish within %s s. The "\n'
        ),
        "new": (
            "        logger.debug(\n"
            '            "The push-to-talk speaker restore did not finish within %s s. The "\n'
        ),
        "catchers": {START_BLOCKS: "AssertionError: no warning named the time limit"},
    },
    {
        # Required (F1): an import of the restore module that raises is not
        # contained on the launcher thread, so it stops the launcher before
        # any child starts.
        "name": "restore-import-exception-propagates",
        "file": LAUNCHER,
        "old": (
            "    except Exception as exc:\n"
            "        logger.error(\n"
            '            "Could not restore the push-to-talk speaker level: %s", exc, exc_info=True,\n'
            "        )\n"
            "        return\n"
        ),
        "new": (
            "    except ZeroDivisionError as exc:\n"
            "        logger.error(\n"
            '            "Could not restore the push-to-talk speaker level: %s", exc, exc_info=True,\n'
            "        )\n"
            "        return\n"
        ),
        "catchers": {
            NOT_IMPORTED: "ModuleNotFoundError: import of services.wheelhouse.utils.ptt_volume_record halted",
        },
    },
    {
        # Required (F3): the restore at launcher start moves back to before the
        # instance record is written, so a later start during the restore
        # cannot find and stop this launcher.
        "name": "start-restore-before-the-instance-record",
        "file": LAUNCHER,
        "edits": [
            (START_SITE, "    # that process runs.\n"),
            (
                "    cleanup_stale_resources()\n\n    # Claim the instance record for THIS launcher.",
                "    cleanup_stale_resources()\n    restore_orphaned_ptt_volume()\n\n"
                "    # Claim the instance record for THIS launcher.",
            ),
        ],
        "catchers": {FIRST_START: RESTORE_BEFORE_RECORD},
    },
    {
        # Required (F3): the restore at launcher start moves into the restart
        # loop, before the children of each cycle start, so it also runs again
        # before every later cycle.
        "name": "start-restore-at-the-top-of-the-restart-loop",
        "file": LAUNCHER,
        "edits": [
            (START_SITE, "    # that process runs.\n"),
            (
                "    while should_restart and crash_count < MAX_CRASHES:\n",
                "    while should_restart and crash_count < MAX_CRASHES:\n"
                "        restore_orphaned_ptt_volume()\n",
            ),
        ],
        # The marker stops at the second restore: a second restore follows the
        # first with no start between them. pytest cuts this message inside
        # the second restore, so a longer marker cannot match.
        "catchers": {
            EVERY_EXIT: (
                "AssertionError: got [('start', 'LogicProcess'), ('start', 'InputProcess'), "
                "('start', 'GuiProcess'), ('join', 'LogicProcess'), ('join', 'InputProcess'), "
                "('join', 'GuiProcess'), ('restore', 'restart flag on disk', ()), ('restore', "
            ),
        },
    },
]

SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$")
SECTION_RULE = re.compile(r"^=+ .* =+$")
SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$")
ASSERTION_PREFIX = "AssertionError: assert "


def _edits(mutation):
    if "edits" in mutation:
        return list(mutation["edits"])
    return [(mutation["old"], mutation["new"])]


def _line_ending(data: bytes) -> str:
    return "\r\n" if b"\r\n" in data else "\n"


def _prepare(originals, names):
    """Return (prepared mutants, error lines). Never touches the files."""
    prepared, errors = [], []
    for mutation in MUTATIONS:
        if names and mutation["name"] not in names:
            continue
        if not mutation["catchers"]:
            errors.append(f"ERROR {mutation['name']}: no-catchers")
            continue
        target = mutation["file"]
        original = originals[target]
        source = original.decode("utf-8")
        ending = _line_ending(original)
        edits = _edits(mutation)
        mutant, problem = source, None
        for step, (old, new) in enumerate(edits, 1):
            label = f" (edit {step})" if len(edits) > 1 else ""
            old = old.replace("\n", ending)
            new = new.replace("\n", ending)
            in_source, in_mutant = source.count(old), mutant.count(old)
            if in_source == 0 or in_mutant == 0:
                problem = f"pattern-not-found{label}"
                break
            if in_source > 1 or in_mutant > 1:
                problem = f"pattern-ambiguous{label} ({max(in_source, in_mutant)} matches)"
                break
            mutant = mutant.replace(old, new, 1)
        if problem is None and mutant == source:
            problem = "pattern-unchanged (the mutant equals the source)"
        if problem is not None:
            errors.append(f"ERROR {mutation['name']}: {problem}")
            continue
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
    # A wide terminal keeps more of each short-summary message. pytest still
    # cuts a message that does not fit and ends it with "...", so a marker is
    # only the start of the message.
    env["COLUMNS"] = "400"
    return env


def _pytest(selection, extra, timeout):
    command = [sys.executable, "-m", "pytest", *selection, "-p", "no:randomly",
               "-p", "no:cacheprovider", "--junitxml=NUL" if os.name == "nt" else "--junitxml=/dev/null",
               *extra]
    return subprocess.run(command, cwd=SERVICE, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _test_name(test_id: str) -> str:
    """The part of a test id after the last '::', with any parameter id kept whole."""
    bracket = test_id.find("[")
    head, parameters = (test_id, "") if bracket == -1 else (test_id[:bracket], test_id[bracket:])
    return head.split("::")[-1] + parameters


def _collected_names(selection):
    result = _pytest(selection, ["--collect-only", "-q"], RUN_TIMEOUT)
    names = set()
    for line in result.stdout.splitlines():
        if "::" in line:
            names.add(_test_name(line.strip()))
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
                name = _test_name(match.group(2))
                if name in records:
                    name = f"{name} ({match.group(1)})"
                records[name] = (match.group(1), match.group(3) or "")
    return records


def _run_selection(selection):
    result = _pytest(selection, ["-rfE", "--tb=short"], RUN_TIMEOUT)
    output = result.stdout + result.stderr
    if "+++ Timeout +++" in output:
        raise RuntimeError("suite-timeout-abort (+++ Timeout +++ in output)")
    records = _summary(output)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pytest exit {result.returncode}: {output[-1500:]}")
    if result.returncode == 1 and not records:
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


def _normal(message: str) -> str:
    """Drop the 'AssertionError: ' that pytest sometimes keeps before a rewritten assert."""
    if message.startswith(ASSERTION_PREFIX):
        return message[len("AssertionError: "):]
    return message


def _judge(mutation, records):
    """Read the verdict from the catchers only; list every other failure."""
    fired, green, wrong = [], [], []
    for name, marker in mutation["catchers"].items():
        record = records.get(name)
        if record is None:
            green.append(name)
        elif record[0] != "FAILED" or not _normal(record[1]).startswith(_normal(marker)):
            wrong.append(f"{name}: {record[0]} {record[1]} (expected {marker!r})")
        else:
            fired.append(f"{name}: {record[1]}")
    others = sorted(set(records) - set(mutation["catchers"]))
    detail = []
    if fired:
        detail.append("fired: " + "; ".join(fired))
    if wrong:
        detail.append("catchers that failed with another message: " + "; ".join(wrong))
    if green:
        detail.append("expected catchers still green: " + ", ".join(green))
    if others:
        detail.append("other failures: " + "; ".join(f"{n}: {records[n][0]} {records[n][1]}" for n in others))
    if wrong:
        return "ERROR", " | ".join(detail)
    if green:
        return "SURVIVED", " | ".join(detail) or "no test failed"
    return "CAUGHT", " | ".join(detail)


def main(argv=None):
    sys.stdout.reconfigure(line_buffering=True, errors="backslashreplace")
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

    all_names = [m["name"] for m in MUTATIONS]
    duplicates = sorted({n for n in all_names if all_names.count(n) > 1})
    if duplicates:
        say(f"ERROR duplicate mutation names: {', '.join(duplicates)}")
        return 1
    unknown = [n for n in args.only if n not in all_names]
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
    say(f"Checked {selected} patterns, {stale} stale or ambiguous, {broken} that do not compile"
        + (f", {len(errors) - stale - broken} other errors" if len(errors) - stale - broken else ""))
    if args.check:
        return 1 if errors else 0

    targets = [t for t in TARGETS if any(m["file"] == t for m, _ in prepared)]
    for target in targets:
        selection = SELECTIONS[target]
        rc, collected = _collected_names(selection)
        expected = {name for m, _ in prepared if m["file"] == target for name in m["catchers"]}
        missing = sorted(expected - collected)
        if rc != 0 or not collected or missing:
            say(f"ERROR expected catcher names not collected from {' '.join(selection)} "
                f"(collect rc={rc}): {', '.join(missing) or 'none collected'}")
            return 1

    _clear_bytecode()
    for target in targets:
        selection = SELECTIONS[target]
        try:
            baseline = _run_selection(selection)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            say(f"ERROR baseline of {' '.join(selection)}: {exc}")
            return 1
        if baseline:
            say(f"ERROR baseline of {' '.join(selection)} not green: {sorted(baseline)}")
            return 1
    say(f"Baseline green for {len(targets)} selections; running {len(prepared)} of {len(MUTATIONS)} mutations"
        + (f" (--only {' '.join(args.only)})" if args.only else ""))

    caught = survivors = 0
    error_count = len(errors)
    for index, (mutation, mutant) in enumerate(prepared, 1):
        name = mutation["name"]
        target = mutation["file"]
        stat = stats[target]
        try:
            _write_with_retry(target, mutant)
            os.utime(target, ns=(stat[0], stat[1] + index * 1_000_000_000))
            _clear_bytecode()
            records = _run_selection(SELECTIONS[target])
            verdict, detail = _judge(mutation, records)
        except subprocess.TimeoutExpired:
            verdict, detail = "ERROR", f"timeout after {RUN_TIMEOUT}s"
        except RuntimeError as exc:
            verdict, detail = "ERROR", str(exc)
        finally:
            _restore(target, originals[target], stat)
        if verdict == "CAUGHT":
            caught += 1
        elif verdict == "SURVIVED":
            survivors += 1
        else:
            error_count += 1
        say(f"{verdict} {name}: {detail}")

    say(f"Ran {len(prepared)} of {len(MUTATIONS)} mutations; "
        f"{caught} caught, {survivors} survived, {error_count} errors")
    if log:
        log.close()
    return 1 if survivors or error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
