"""Mutation gate for wh-config-save-clobbers-hand-edits.

ConfigService.save used to write the whole settings file from its in-memory
copy without re-reading it, so an edit made to the file by hand while the
program ran was replaced by the next save. save() now re-reads the file
immediately before the write and merges: a key the program changed since it
last read the file takes the in-memory value, every other key keeps what the
file says.

Acceptance criterion 5 asks for two mutations by name -- removing the re-read
must fail test (a), and flipping the precedence must fail test (b). Eight more
are here because eight more parts of this change are load-bearing and their
tests were written after the fix, so they owe the same proof:

  no-re-read              Skip the re-read; save from memory as before. This
                          is the defect itself.
  file-wins-over-program  Give the file the key the program changed. The
                          precedence flip criterion 5 names.
  write-back-replaces-a-nested-table
                          Assign a fresh dictionary into the live settings
                          instead of merging into the one already there.
                          ServiceManager and SpeechHandler each hold the
                          object get_config() returned, so a replacement
                          leaves them reading a dictionary nothing updates.
  write-back-ignores-a-later-change
                          Drop the "still equals what the save started from"
                          guard, so a key changed while the write ran in its
                          worker thread is overwritten. save() promises that
                          such a change reaches the file in its own write.
                          The first full sweep after the review fixes
                          reported this as a survivor, because the new
                          table-level guard stops the walk before any key
                          under a touched table is reached; the guard is
                          reachable only at the top level, and the expected
                          test is now a top-level one.
  loaded-snapshot-from-memory
                          Record what the program held rather than what
                          reached the file, so the next save reads a kept
                          value as a deliberate change back and writes the
                          following hand edit away. Two saves cannot see this;
                          it takes two hand EDITS, which is why this gate has
                          a test the acceptance criteria never asked for.
  hand-deletion-ignored   Put back a key the user deleted by hand.
  no-warning-when-unreadable
                          Save from memory without saying the file could not
                          be read.
  no-report-of-what-was-kept
                          Save without the line naming the kept keys.
  the-report-prints-the-values
                          Name the values as well as the keys. The settings
                          file holds account details and server addresses,
                          and other people read the log.
  reports-even-when-nothing-was-kept
                          Print the line on every save, which would train the
                          reader to ignore it.

Fifteen more came from the deepseek review round (findings .1.1 to .1.4 on
epic wh-config-save-clobbers-hand-edits.1):

  merge-reads-a-skipped-key-as-a-removal
                          Treat "in the record of the last write, absent from
                          memory" as a removal. The finding suggested exactly
                          this rule; testing it showed it deletes a hand edit,
                          because the write-back steps over a table a caller
                          changed mid-write.
  write-back-adds-a-key-removed-mid-write
  write-back-walks-into-a-replaced-table
                          The two write-back branches that had no guard: the
                          one that adds a missing key, and the walk into a
                          nested table. The first full sweep reported the
                          second as a survivor: every key its test used was
                          already blocked by the first guard one level down,
                          so the test gained a key the file was given by
                          hand, which that guard cannot block.
  removal-not-recorded
  merge-ignores-the-removals
  removal-record-never-cleared
                          The three parts of the record of what the program
                          removed. Without it a removal is read as a key the
                          program never held and comes back out of the file,
                          which is what stopped main.py's rollback save from
                          converging.
  empty-file-read-as-deletions
  empty-file-guard-says-nothing
  empty-file-guard-too-wide
                          The guard for a settings file with nothing left in
                          it, its warning, and the boundary: a file that still
                          holds one section is an edited file.
  no-removal-from-the-held-object
  no-addition-to-the-held-object
                          The two halves of the write-back read through the
                          object every component holds, not through a reload.
  no-report-of-what-was-dropped
                          Only the kept half of the report line had a
                          mutation.
  no-warning-when-the-file-will-not-open
  a-missing-file-reported-as-a-fault
                          The two arms of the read that are not a parse
                          failure.

Eleven more came from the codex review round (findings .1.5 to .1.9 on the
same epic):

  baseline-copied-before-the-lock
  record-of-the-last-write-copied-before-the-lock
  removals-copied-before-the-lock
                          The three baselines a save compares against, each
                          moved back out of the lock, which is where they
                          were copied before finding .1.5. A save that queues
                          behind another one then compares against values the
                          save in front has already replaced.
  replaced-table-not-recorded
  missing-table-compared-whole
  an-emptied-table-stays-behind
                          The three parts of the table boundary (finding
                          .1.6): the record set() keeps of the keys a
                          replaced table carried, the merge one level down
                          into a table the file no longer holds, and the rule
                          that a table left with nothing in it is not written
                          back as an empty heading.
  a-file-that-will-not-decode-escapes
                          Catch only the parse failure, as before finding
                          .1.7. A file an editor saved as UTF-16 then raises
                          out of the save instead of being reported. The test
                          names a raise as its own failure, so this is caught
                          by an assertion rather than reported as an error.
  the-dropped-half-prints-the-values
                          Name the values as well as the keys in the dropped
                          half of the report. The kept half already had this
                          mutation; finding .1.8 is that the dropped half did
                          not.
  no-second-look-before-the-replace
  the-second-look-does-not-merge
  the-file-is-read-again-every-time
                          The three parts of the comparison finding .1.9
                          asked for: the file is read and merged once more
                          when it changed while the copy was being prepared,
                          that second look merges rather than writing memory,
                          and a file nobody touched is written once.
  a-value-where-a-section-belongs-raises-the-wrong-error
                          Read what a key held without asking whether the
                          thing holding it is a table. A settings file can
                          carry a plain value where a section belongs, and
                          main.py answers that by catching the TypeError the
                          assignment raises. Reading through the value first
                          raises AttributeError, which walks past that
                          handler and closes Wheelhouse. The scoped re-run
                          for acceptance criterion 6 found this, eight
                          failures in tests/test_malformed_stt_settings.py.

Run it from services/wheelhouse:

    python tests/mutation_gate_config_save_merge.py

Never at the same time as another run of this test file: the gate rewrites
config_service.py underneath whatever is importing it.

The runner is the one from tests/mutation_gate_hotword_examples.py, which
already reports pattern-not-found, an ambiguous pattern, a mutant that does
not compile, a per-mutation timeout, a suite-timeout abort, an unexpected
pytest return code, a test that pytest itself reported as an error, and a
test that raised instead of
failing its own assertion, each as an error rather than a verdict. It also
checks every expected test name against the collected names before the first
mutation, so a renamed test cannot read as a survivor, and it translates each
pattern to the target file's own line endings. Added here: the restore is
retried once if an interrupt lands inside it, so a second Ctrl+C cannot leave
the mutant in a tracked file.
"""
# --check validates patterns and Python syntax without collecting tests,
# recovering pending mutations, or writing target files.
import os
import re
import subprocess
import sys
import tempfile
import shutil
import uuid
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

SERVICE = Path(__file__).resolve().parent.parent
SRC = SERVICE / "config_service.py"
sys.path.insert(0, str(SERVICE.parents[1] / "scripts/codex"))
from owned_process import CleanupUnconfirmedError, run_owned
TEST_FILES = ["tests/test_config_service.py"]

# --tb=line is what carries the failure REASON. This project's pytest prints
# its short summary as a bare "FAILED <nodeid>" with no " - reason" suffix, so
# the summary alone cannot tell an assertion failure from a crash.
PYTEST_ARGS = ["-p", "no:randomly", "-q", "-rfE", "--tb=line"]

_TB_LINE = re.compile(r"^.*\.py:\d+: (?P<reason>.+)$")
_EXCEPTION_HEAD = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b")

_OBSERVED = "test_a_hand_edited_key_survives_a_save_of_another_key"
_PROGRAM_WINS = "test_the_program_wins_when_both_sides_changed_one_key"
_NEW_TABLE = "test_a_table_added_by_hand_survives"
_NEW_KEY = "test_a_key_added_by_hand_to_a_loaded_table_survives"
_UNPARSABLE = "test_an_unparsable_file_saves_from_memory_and_warns"
_SECOND_SAVE = "test_a_hand_edit_survives_a_second_save"
_SECOND_EDIT = "test_a_second_hand_edit_also_survives"
_DELETED = "test_a_line_deleted_by_hand_stays_deleted"
_NAMES_KEPT = "test_one_info_line_names_the_kept_keys"
_QUIET = "test_a_save_that_kept_nothing_says_nothing"
_LATER_CHANGE = (
    "test_a_top_level_key_changed_during_the_write_survives"
)
_IN_PLACE = "test_the_kept_value_reaches_memory_in_place"
_LATER_UNSET = "test_an_unset_made_during_the_write_survives"
_REPLACED_TABLE = "test_a_table_replaced_during_the_write_is_not_polluted"
_ADDED_MID_WRITE = (
    "test_a_hand_added_key_survives_a_change_made_during_the_write"
)
_REMOVED = "test_a_key_the_program_removed_stays_removed"
_REMOVED_TWICE = "test_a_removal_survives_the_next_save"
_ROLLBACK = "test_the_rollback_save_removes_a_refused_value"
_SET_AGAIN = "test_a_key_set_again_after_a_removal_is_written"
_ADDED_BACK = "test_a_key_added_back_by_hand_after_a_removal_survives"
_EMPTIED = "test_an_emptied_file_saves_from_memory_and_warns"
_COMMENTS_ONLY = (
    "test_a_file_holding_only_comments_is_treated_the_same_way"
)
_ONE_SECTION = "test_one_section_left_is_still_read_as_hand_deletion"
_DELETED_TWICE = "test_a_hand_deletion_survives_a_second_save"
_ADDED_TABLE_HELD = "test_a_hand_added_table_reaches_the_held_object"
_NAMES_DROPPED = "test_one_info_line_names_the_deleted_keys"
_WILL_NOT_OPEN = (
    "test_a_file_that_cannot_be_opened_saves_from_memory_and_warns"
)
_NO_FILE_YET = "test_a_missing_file_saves_from_memory_without_a_warning"
_KEPT_BASELINE = "test_the_second_save_uses_the_value_the_first_save_kept"
_QUEUED_BASELINE = "test_the_second_save_compares_against_what_the_first_wrote"
_QUEUED_REMOVAL = "test_a_removal_the_first_save_carried_out_is_not_repeated"
_TABLE_REPLACED = "test_a_table_the_program_replaced_reaches_the_file"
_TABLE_DELETED = "test_a_table_deleted_by_hand_keeps_only_the_changed_key"
_TABLE_EMPTIED = "test_a_table_the_user_emptied_by_hand_disappears"
_NOT_UTF8 = "test_a_file_that_is_not_utf8_saves_from_memory_and_warns"
_DROPPED_VALUES = "test_the_dropped_line_names_no_values"
_LATE_EDIT = "test_an_edit_saved_after_the_read_is_not_written_over"
_READ_AGAIN_ONCE = "test_the_file_is_read_again_exactly_once"
_WRITTEN_ONCE = "test_a_file_nobody_touched_is_written_once"
_SCALAR_SECTION = "test_setting_a_key_under_a_value_raises_type_error"

# The three baselines a save compares against, copied with the lock held.
# Each of the three .1.5 mutations moves one of them back out of the lock,
# which is a move and not a copy: leaving both would prove nothing.
_UNDER_THE_LOCK = """        async with self._save_lock:
            # Copied with the lock held and before this write's first await,
            # so it is one settled set of values and no write in front can
            # still replace the baseline it is compared against.
            snapshot = copy.deepcopy(self._config)
            loaded = copy.deepcopy(getattr(self, "_loaded", {}))
            removed = set(self._removed)
"""

MUTATIONS = [
    {
        # Criterion 5, first half: the defect itself.
        "name": "no-re-read",
        "old": "            on_disk = read_the_file_again()\n",
        "new": "            on_disk = None\n",
        # Every merge test fails; the criterion names test (a), and the rest
        # are listed so a partial catch cannot read as a full one.
        "expect": [_OBSERVED, _NEW_TABLE, _NEW_KEY, _SECOND_SAVE, _NAMES_KEPT,
                   _DELETED, _IN_PLACE, _SECOND_EDIT],
    },
    {
        # Criterion 5, second half: the precedence flip.
        "name": "file-wins-over-program",
        "old": """        if changed_by_the_program:
            merged[key] = memory_value
""",
        "new": """        if changed_by_the_program:
            merged[key] = on_disk_value if key in disk else memory_value
""",
        "expect": [_PROGRAM_WINS],
    },
    {
        "name": "write-back-replaces-a-nested-table",
        "old": """            _write_back_in_place(current, merged_value, seen.get(key))
            continue
""",
        "new": """            target[key] = copy.deepcopy(merged_value)
            continue
""",
        "expect": [_IN_PLACE],
    },
    {
        "name": "write-back-ignores-a-later-change",
        "old": (
            "        if key in seen and current == seen[key] "
            "and current != merged_value:\n"
        ),
        "new": "        if key in seen and current != merged_value:\n",
        "expect": [_LATER_CHANGE],
    },
    {
        "name": "loaded-snapshot-from-memory",
        "old": '                self._loaded = copy.deepcopy(outcome["merged"])\n',
        "new": "                self._loaded = copy.deepcopy(snapshot)\n",
        # Not test_a_hand_edit_survives_a_second_save: that one saves twice but
        # edits the file once, and the write-back has already brought memory up
        # to the kept value by then, so both spellings of the record agree. It
        # takes a SECOND hand edit for them to disagree. The gate reported this
        # mutation as a survivor until that test existed.
        "expect": [_SECOND_EDIT],
    },
    {
        "name": "hand-deletion-ignored",
        "old": """        elif key not in disk:
            # Deleting a line is a hand edit too, and the program did not
            # touch this key, so the deletion stands.
            dropped.append(path)
""",
        "new": """        elif key not in disk:
            merged[key] = memory_value
""",
        "expect": [_DELETED],
    },
    {
        "name": "no-warning-when-unreadable",
        # The pattern was refreshed when finding .1.7 added a second
        # exception to this arm and a comment above the warning. It is the
        # warning itself, which is unique: the arm for a file that will not
        # open says "Could not open".
        "old": """                logger.warning(
                    "Could not read %s before saving, so this save keeps only "
                    "the settings the program is holding and any edit made to "
                    "the file by hand is lost: %s",
                    self.config_path,
                    e,
                )
                return None
""",
        "new": """                return None
""",
        "expect": [_UNPARSABLE],
    },
    {
        "name": "no-report-of-what-was-kept",
        "old": "                if kept or dropped:\n",
        "new": "                if False:\n",
        "expect": [_NAMES_KEPT],
    },
    {
        # Input-level: the line is still printed, and it still names the key.
        # Only the half of the test that forbids the value can see this.
        "name": "the-report-prints-the-values",
        "old": '                            + ", ".join(sorted(kept))\n',
        "new": (
            '                            + ", ".join(sorted(kept)) '
            '+ " " + repr(to_write)\n'
        ),
        "expect": [_NAMES_KEPT],
    },
    {
        "name": "reports-even-when-nothing-was-kept",
        "old": """                if kept or dropped:
                    # Names only. The settings file holds account details and
                    # server addresses, and the log is read by other people.
                    parts = []
                    if kept:
""",
        "new": """                if True:
                    parts = []
                    if True:
""",
        "expect": [_QUIET],
    },
    {
        # The rule the deepseek finding suggested and testing rejected.
        # "In the record of the last write but not in memory now" looks like
        # a removal and is not: the write-back steps over a table a caller
        # changed while the write ran, so a key the file gained sits in the
        # record and not in memory, and this reads it as a removal and
        # deletes a hand edit.
        "name": "merge-reads-a-skipped-key-as-a-removal",
        "old": """        if path in taken_out:
""",
        "new": """        if path in taken_out or key in seen:
""",
        "expect": [_ADDED_MID_WRITE],
    },
    {
        # Review finding .1.1, first half. The branch that adds a missing key
        # ran unconditionally, so a removal made while the write ran came
        # straight back into memory.
        "name": "write-back-adds-a-key-removed-mid-write",
        "old": """        if key not in target:
            if key in seen:
""",
        "new": """        if key not in target:
            if False:
""",
        "expect": [_LATER_UNSET],
    },
    {
        # Review finding .1.1, second half. Walking into a table the caller
        # has just replaced puts the old table's keys into the new one.
        "name": "write-back-walks-into-a-replaced-table",
        "old": """            if current != seen.get(key):
""",
        "new": """            if False:
""",
        "expect": [_REPLACED_TABLE],
    },
    {
        # Review finding .1.2. unset() is the only thing that tells the merge
        # a key's absence from memory was asked for.
        "name": "removal-not-recorded",
        "old": """            self._removed.add(key)
""",
        "new": """            pass
""",
        "expect": [_REMOVED, _REMOVED_TWICE, _ROLLBACK],
    },
    {
        "name": "merge-ignores-the-removals",
        "old": "    taken_out = removed or set()\n",
        "new": "    taken_out = set()\n",
        "expect": [_REMOVED, _REMOVED_TWICE, _ROLLBACK],
    },
    {
        # The record must last exactly one write. Kept for ever, it applies a
        # second time to a key the user has written back into the file.
        "name": "removal-record-never-cleared",
        "old": """                self._removed -= removed
""",
        "new": """                pass
""",
        "expect": [_ADDED_BACK],
    },
    {
        # Review finding .1.3. A file with nothing left in it read as a hand
        # deletion of every key, and one save made that permanent.
        "name": "empty-file-read-as-deletions",
        "old": (
            "            if on_disk is not None and not on_disk and snapshot:\n"
        ),
        "new": "            if False:\n",
        "expect": [_EMPTIED, _COMMENTS_ONLY],
    },
    {
        # The guard without its warning: the settings survive and nobody is
        # told the file was found empty.
        "name": "empty-file-guard-says-nothing",
        "old": """                logger.warning(
                    "%s holds no settings at all, so this save keeps the "
                    "settings the program is holding rather than reading the "
                    "empty file as a request to delete every one of them",
                    self.config_path,
                )
                on_disk = None
""",
        "new": """                on_disk = None
""",
        "expect": [_EMPTIED, _COMMENTS_ONLY],
    },
    {
        # Widening the guard to any file smaller than the settings in memory
        # would swallow an ordinary hand deletion.
        "name": "empty-file-guard-too-wide",
        "old": "            if on_disk is not None and not on_disk and snapshot:\n",
        "new": (
            "            if on_disk is not None and len(on_disk) < len(snapshot):\n"
        ),
        "expect": [_ONE_SECTION],
    },
    {
        # Review finding .1.4, first hole. Without the removal loop the
        # deleted key stays in memory and the next ordinary save writes it
        # back into the file.
        "name": "no-removal-from-the-held-object",
        "old": """    for key in list(target):
        if key in merged:
            continue
        if key in seen and target[key] == seen[key]:
            del target[key]
""",
        "new": """    for key in list(target):
        if key in merged:
            continue
""",
        "expect": [_DELETED_TWICE],
    },
    {
        # Review finding .1.4, second hole. The file gains the key and the
        # object every component holds does not.
        "name": "no-addition-to-the-held-object",
        "old": """            target[key] = copy.deepcopy(merged_value)
            continue
        current = target[key]
""",
        "new": """            continue
        current = target[key]
""",
        "expect": [_ADDED_TABLE_HELD],
    },
    {
        # Review finding .1.4, third hole. Only the kept half of the line had
        # a mutation.
        "name": "no-report-of-what-was-dropped",
        "old": """                    if dropped:
""",
        "new": """                    if False:
""",
        "expect": [_NAMES_DROPPED],
    },
    {
        # Review finding .1.4, the minor half: the two arms of the read that
        # are not a parse failure.
        "name": "no-warning-when-the-file-will-not-open",
        "old": """            except OSError as e:
                logger.warning(
                    "Could not open %s before saving, so this save keeps only "
                    "the settings the program is holding and any edit made to "
                    "the file by hand is lost: %s",
                    self.config_path,
                    e,
                )
                return None
""",
        "new": """            except OSError as e:
                return None
""",
        "expect": [_WILL_NOT_OPEN],
    },
    {
        # A file that is simply not there yet is the ordinary first save.
        # Reporting it as a fault would teach the reader to ignore the line.
        "name": "a-missing-file-reported-as-a-fault",
        "old": """            except FileNotFoundError:
                return None
""",
        "new": """            except FileNotFoundError:
                logger.warning("Could not read %s", self.config_path)
                return None
""",
        "expect": [_NO_FILE_YET],
    },
    {
        # Finding .1.5, first of three: the settings themselves. The first
        # save writes a kept hand edit into memory, so a copy taken before
        # the wait holds the value from before that write.
        "name": "baseline-copied-before-the-lock",
        "old": _UNDER_THE_LOCK,
        "new": """        snapshot = copy.deepcopy(self._config)
        async with self._save_lock:
            loaded = copy.deepcopy(getattr(self, "_loaded", {}))
            removed = set(self._removed)
""",
        "expect": [_KEPT_BASELINE],
    },
    {
        # Second of three: the record of what reached the file last time.
        "name": "record-of-the-last-write-copied-before-the-lock",
        "old": _UNDER_THE_LOCK,
        "new": """        loaded = copy.deepcopy(getattr(self, "_loaded", {}))
        async with self._save_lock:
            snapshot = copy.deepcopy(self._config)
            removed = set(self._removed)
""",
        "expect": [_QUEUED_BASELINE],
    },
    {
        # Third of three: the record of what the program removed, which the
        # save in front has already used up.
        "name": "removals-copied-before-the-lock",
        "old": _UNDER_THE_LOCK,
        "new": """        removed = set(self._removed)
        async with self._save_lock:
            snapshot = copy.deepcopy(self._config)
            loaded = copy.deepcopy(getattr(self, "_loaded", {}))
""",
        "expect": [_QUEUED_REMOVAL],
    },
    {
        # Finding .1.6, first of three: the record set() keeps of the keys
        # a replaced table carried.
        "name": "replaced-table-not-recorded",
        "old": '            self._removed.update(_paths_under(previous, f"{key}."))\n',
        "new": "            pass\n",
        "expect": [_TABLE_REPLACED],
    },
    {
        # Second of three: the merge one level down into a table the file
        # no longer holds.
        "name": "missing-table-compared-whole",
        "old": "            disk_has_a_table or key not in disk\n",
        "new": "            disk_has_a_table\n",
        "expect": [_TABLE_DELETED],
    },
    {
        # Third of three: a table left with nothing in it is not written
        # back as an empty heading.
        "name": "an-emptied-table-stays-behind",
        "old": "            if sub_merged or disk_has_a_table or not memory_value:\n",
        "new": "            if True:\n",
        "expect": [_TABLE_EMPTIED],
    },
    {
        # Finding .1.7.
        "name": "a-file-that-will-not-decode-escapes",
        "old": "            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:\n",
        "new": "            except tomllib.TOMLDecodeError as e:\n",
        "expect": [_NOT_UTF8],
    },
    {
        # Finding .1.8. The value comes from the settings the save started
        # from, which is where a reader of the log would find it.
        "name": "the-dropped-half-prints-the-values",
        "old": """                            "dropped, since the file no longer carries them: "
                            + ", ".join(sorted(dropped))
""",
        "new": """                            "dropped, since the file no longer carries them: "
                            + ", ".join(
                                f"{name}={snapshot.get(name)!r}"
                                for name in sorted(dropped)
                            )
""",
        "expect": [_DROPPED_VALUES],
    },
    {
        # Finding .1.9, first of three: no second look at all, which is the
        # defect the finding reported.
        "name": "no-second-look-before-the-replace",
        "old": "                if stamp_of_the_file() != stamp:\n",
        "new": "                if False:\n",
        "expect": [_LATE_EDIT, _READ_AGAIN_ONCE],
    },
    {
        # Second of three: the second look writes memory instead of merging
        # what it just read.
        "name": "the-second-look-does-not-merge",
        "old": """                    to_write, kept, dropped = read_and_merge()
                    outcome["merged"] = to_write
                    with open(temp_path, "wb") as f:
""",
        "new": """                    to_write, kept, dropped = snapshot, [], []
                    outcome["merged"] = to_write
                    with open(temp_path, "wb") as f:
""",
        "expect": [_LATE_EDIT],
    },
    {
        # Third of three, from the other side: a file nobody touched is
        # written once, so the ordinary save gained no second write.
        "name": "the-file-is-read-again-every-time",
        "old": "                if stamp_of_the_file() != stamp:\n",
        "new": "                if True:\n",
        "expect": [_WRITTEN_ONCE],
    },
    {
        # The guard on the record the .1.6 fix added.
        "name": "a-value-where-a-section-belongs-raises-the-wrong-error",
        "old": "            previous = target.get(keys[-1]) if isinstance(target, dict) else None\n",
        "new": "            previous = target.get(keys[-1])\n",
        # Only the first test. The second passes under this mutation on
        # purpose: nothing is written before the raise, whichever error it
        # is, so the settings are left alone either way.
        "expect": [_SCALAR_SECTION],
    },
]


def _pytest(*extra):
    evidence = SERVICE / '.pytest_cache' / 'config-merge-gate'
    evidence.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='run-', dir=evidence))
    bytecode = directory / 'bytecode'
    confirmed = True
    try:
        result = run_owned(
            [sys.executable, str(SERVICE.parents[1] / "scripts/run_tests.py"),
             *extra, "--junitxml", str(directory / "results.xml")],
            cwd=SERVICE.parents[1], timeout_seconds=300, evidence_dir=directory,
            env={**os.environ, "PYTHONPYCACHEPREFIX": str(bytecode)},
        )
        if not result.cleanup_confirmed:
            raise CleanupUnconfirmedError('Child cleanup was not confirmed')
        return result
    except BaseException as exc:
        confirmed = getattr(exc, 'cleanup_confirmed', True)
        raise
    finally:
        if confirmed and bytecode.is_dir():
            shutil.rmtree(bytecode)


def _restore(original):
    """Put the real source back, even if an interrupt lands inside the write.

    A second Ctrl+C arriving during the restore would otherwise leave the
    mutant in a tracked file, where every later run and every other session
    sharing the checkout reads it as real code.
    """
    held = None
    for _ in range(2):
        try:
            SRC.write_bytes(original)
            break
        except KeyboardInterrupt as interrupt:
            held = interrupt
        except OSError as error:
            print(f"ERROR could not restore {SRC}: {error}")
            break
    else:
        print(f"ERROR could not restore {SRC}; it may still hold a mutant")
    if held is not None:
        raise held
    if SRC.read_bytes() != original:
        raise RuntimeError(f"Source was not restored: {SRC}")


def collect_names():
    """Real test names in the file, so a renamed test cannot read as a survivor."""
    # The wrapper already supplies -q; a second one hides individual node IDs.
    out = _pytest(*TEST_FILES, "--collect-only", "-p", "no:randomly")
    names = set()
    for line in out.stdout.splitlines():
        if "::" in line:
            names.add(line.split("::")[-1].strip().split("[")[0])
    return names


def is_assertion_failure(reason):
    """True when pytest's short reason describes a failed assert statement."""
    if not reason:
        return False
    if reason.startswith("AssertionError"):
        return True
    return not _EXCEPTION_HEAD.match(reason)


def failed_names(output):
    """Every test name on a FAILED summary line, parameters stripped."""
    names = set()
    for line in output.splitlines():
        if line.startswith("FAILED "):
            names.add(line.split("::")[-1].strip().split("[")[0].split(" ")[0])
    return names


def failure_reasons(output):
    """Every failure reason --tb=line printed, one per failing parameter."""
    reasons = []
    for line in output.splitlines():
        match = _TB_LINE.match(line.strip())
        if match:
            reasons.append(match.group("reason").strip())
    return reasons


_ERROR_SUMMARY = re.compile(r"^ERROR (?P<nodeid>\S+\.py(::\S+)?)")


def error_lines(output):
    """Summary lines for tests that ERRORED rather than failed.

    Matching the node id, not the word alone. A captured log record at the
    ERROR level starts with the level name padded to eight characters, so
    it too begins with ERROR and then spaces, and pytest prints the
    captured log of a test that FAILED. A test that deliberately provokes
    such a record -- one that makes a write fail to prove a removal is
    still owed -- then read as a pytest error and buried the verdict for
    two mutations that had in fact been caught.
    """
    return [
        line for line in output.splitlines() if _ERROR_SUMMARY.match(line)
    ]


def check_only() -> int:
    """Validate every pattern and Python mutant without running or writing."""
    stale = ambiguous = broken = 0
    for mutation in MUTATIONS:
        path = SRC
        data = path.read_bytes()
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        old = mutation["old"].encode("utf-8").replace(b"\n", newline)
        new = mutation["new"].encode("utf-8").replace(b"\n", newline)
        count = data.count(old)
        if count == 0:
            stale += 1
            print(f"STALE {mutation['name']}: pattern not found in {path}")
            continue
        if count != 1:
            ambiguous += 1
            print(f"AMBIGUOUS {mutation['name']}: {count} matches in {path}")
            continue
        if path.suffix == ".py":
            try:
                compile(data.replace(old, new, 1), str(path), "exec")
            except SyntaxError as exc:
                broken += 1
                print(f"BROKEN {mutation['name']}: mutant does not compile: {exc}")
    print(
        f"checked {len(MUTATIONS)} patterns, {stale} stale, "
        f"{ambiguous} ambiguous, {broken} that do not compile"
    )
    return 1 if stale or ambiguous or broken else 0


def main():
    if "--check" in sys.argv[1:]:
        return check_only()

    original = SRC.read_bytes()
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8")
    for mut in MUTATIONS:
        mut["old"] = mut["old"].replace("\n", newline)
        mut["new"] = mut["new"].replace("\n", newline)

    errors, caught, survived = [], [], []

    real_names = collect_names()
    print(
        f"line endings in {SRC.name}: "
        f"{'CRLF' if newline == chr(13) + chr(10) else 'LF'}"
    )
    print(f"collected {len(real_names)} test names in {', '.join(TEST_FILES)}")
    for mut in MUTATIONS:
        for name in mut["expect"]:
            if name not in real_names:
                errors.append(f"{mut['name']}: expected test {name!r} does not exist")
    if errors:
        for line in errors:
            print("ERROR", line)
        return 1

    baseline = _pytest(*TEST_FILES, *PYTEST_ARGS)
    if baseline.returncode != 0:
        print("ERROR baseline is not green; refusing to start")
        print(baseline.stdout[-2000:])
        return 1
    print("baseline green")
    backups = SERVICE / ".pytest_cache" / "config-merge-gate" / uuid.uuid4().hex
    backups.mkdir(parents=True)

    for mut in MUTATIONS:
        count = text.count(mut["old"])
        if count != 1:
            errors.append(f"{mut['name']}: pattern matched {count} times, expected 1")
            print("ERROR", errors[-1])
            continue
        mutated = text.replace(mut["old"], mut["new"], 1)
        try:
            compile(mutated, str(SRC), "exec")
        except SyntaxError as exc:
            errors.append(f"{mut['name']}: mutated source does not compile: {exc}")
            print("ERROR", errors[-1])
            continue
        (backups / (mut["name"] + ".original")).write_bytes(original)
        confirmed = True
        try:
            SRC.write_bytes(mutated.encode("utf-8"))
            result = _pytest(*TEST_FILES, *PYTEST_ARGS)
        except CleanupUnconfirmedError:
            confirmed = False
            raise
        except subprocess.TimeoutExpired:
            errors.append(f"{mut['name']}: timed out")
            print("ERROR", errors[-1])
            continue
        finally:
            if confirmed:
                _restore(original)
        if "+++ Timeout +++" in result.stdout:
            errors.append(f"{mut['name']}: suite-timeout-abort")
            print("ERROR", errors[-1])
            continue
        if result.returncode not in (0, 1):
            errors.append(
                f"{mut['name']}: pytest returned {result.returncode}; no verdict"
            )
            print("ERROR", errors[-1])
            continue
        stray = error_lines(result.stdout)
        if stray:
            errors.append(f"{mut['name']}: pytest reported errors: {stray[:3]}")
            print("ERROR", errors[-1])
            continue
        got = failed_names(result.stdout)
        missing = [n for n in mut["expect"] if n not in got]
        if result.returncode == 0 or missing:
            survived.append(mut["name"])
            print(f"SURVIVED {mut['name']}; failed tests were {sorted(got)}")
            continue
        crashed = [r for r in failure_reasons(result.stdout)
                   if not is_assertion_failure(r)]
        if crashed:
            errors.append(
                f"{mut['name']}: a test raised instead of failing its "
                f"assertion: {sorted(set(crashed))}"
            )
            print("ERROR", errors[-1])
            continue
        caught.append(mut["name"])
        print(f"caught   {mut['name']} by {sorted(got)}")

    print()
    print(f"scope: {len(MUTATIONS)} mutations, none skipped")
    print(f"caught {len(caught)}, survived {len(survived)}, errors {len(errors)}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    sys.exit(main())
