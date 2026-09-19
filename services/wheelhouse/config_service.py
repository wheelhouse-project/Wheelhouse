"""Manages static application configuration using a hybrid strategy.

This module provides the ConfigService, which is responsible for managing
the application's static configuration. It embodies the "pull" part of a
hybrid configuration model:

- **Static "Pull" Configuration (This Service):** For startup-critical
  parameters that are read once and rarely change. This synchronous model
  ensures stability, as failures in loading critical configuration are
  fatal and prevent the application from starting in an invalid state.

- **Dynamic "Push" Configuration (EventBus):** For runtime-changeable
  settings (e.g., `commands.toml`). The EventBus pushes notifications to
  interested services when these settings are modified.

This service uses the TOML format for its configuration files to improve
readability and allow for inline comments, treating configuration as a
form of documentation.
"""
import asyncio
import copy
import logging
import tomllib
from typing import Any, Dict, List, Optional, Set, Tuple
import os

logger = logging.getLogger(__name__)


def _merge_file_over_memory(
    on_disk: Any,
    in_memory: Dict[str, Any],
    loaded: Any,
    removed: Optional[Set[str]] = None,
    prefix: str = "",
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Decide, key by key, whether the file or the program wins.

    wh-config-save-clobbers-hand-edits. The settings are read once at startup,
    so anything the user writes into the file by hand while the program runs
    used to be replaced by the next save. `loaded` is what the program last
    saw in the file; comparing memory against it tells a value the program
    deliberately changed from one it simply has not touched.

    `removed` holds the key paths the program has taken out of the settings
    since it last read the file. A removed key is absent from memory, so
    without that record the file's copy reads as a key the program never held
    and comes straight back. The two rollback paths in main.py that depended
    on the opposite -- remove the key after a failed save, save again, and let
    the file converge on the rolled-back state -- belonged to the STT mode
    change, and wh-in-process-capture-removal deleted both with it. No caller
    removes a key today; the record stays because it is what makes a removal
    stick, and the next caller to need one must not have to rediscover that.

    Returns the settings to write, the key paths whose value came from the
    file, and the key paths dropped because the file no longer carries them.
    A key the program removed is left out of the result and is NOT reported
    as dropped: the program asked for that, so it is not news.
    """
    merged: Dict[str, Any] = {}
    kept: List[str] = []
    dropped: List[str] = []
    disk = on_disk if isinstance(on_disk, dict) else {}
    seen = loaded if isinstance(loaded, dict) else {}
    taken_out = removed or set()

    for key, memory_value in in_memory.items():
        path = f"{prefix}{key}"
        on_disk_value = disk.get(key)
        loaded_value = seen.get(key)

        disk_has_a_table = key in disk and isinstance(on_disk_value, dict)
        if isinstance(memory_value, dict) and (
            disk_has_a_table or key not in disk
        ):
            # A table the user deleted from the file by hand is merged the
            # same way as one still there, against nothing at all. Comparing
            # the whole table instead let one key the program changed drag
            # every untouched key beside it back into the file, which is the
            # opposite of the rule a deleted line follows
            # (wh-config-save-clobbers-hand-edits review finding .1.6).
            sub_merged, sub_kept, sub_dropped = _merge_file_over_memory(
                on_disk_value if disk_has_a_table else {},
                memory_value,
                loaded_value,
                taken_out,
                f"{path}.",
            )
            if sub_merged or disk_has_a_table or not memory_value:
                # Nothing left and nothing there to begin with: the table
                # goes with its keys rather than staying behind as an empty
                # heading. A table the program deliberately emptied is still
                # written, since that is a value it chose.
                merged[key] = sub_merged
            kept.extend(sub_kept)
            dropped.extend(sub_dropped)
            continue

        changed_by_the_program = key not in seen or memory_value != loaded_value
        if changed_by_the_program:
            merged[key] = memory_value
        elif key not in disk:
            # Deleting a line is a hand edit too, and the program did not
            # touch this key, so the deletion stands.
            dropped.append(path)
        elif on_disk_value != memory_value:
            merged[key] = on_disk_value
            kept.append(path)
        else:
            merged[key] = memory_value

    for key, on_disk_value in disk.items():
        if key in in_memory:
            continue
        path = f"{prefix}{key}"
        if path in taken_out:
            # The program took this key out. Its removal is a change like any
            # other change the program made, so the file loses the key too.
            #
            # Only a recorded unset() counts. "In the last write but not in
            # memory now" looks like the same thing and is not: the write-back
            # steps over a table a caller changed while the write ran, so a
            # key the file gained sits in the last write and not in memory,
            # and reading that as a removal deletes a hand edit
            # (test_a_hand_added_key_survives_a_change_made_during_the_write).
            # unset() is the only way anything removes a settings key. A grep
            # for ".unset(" under services/wheelhouse, outside tests/, now
            # gives no call site at all: the three it used to give were the
            # rollback paths of the STT mode change, which
            # wh-in-process-capture-removal deleted. The removal record is
            # kept because the merge above is what makes an unset stick, and
            # a caller that removes a key again must not have to rediscover
            # that.
            continue
        # Written into the file by hand; the program has never held it.
        merged[key] = on_disk_value
        kept.append(path)

    return merged, kept, dropped


def _paths_under(table: Dict[str, Any], prefix: str) -> Set[str]:
    """Every key path inside a table, at every depth.

    The merge asks about a key by its full path, and a table it walks into
    asks about the table's own path, so both are recorded.
    """
    paths: Set[str] = set()
    for key, value in table.items():
        path = f"{prefix}{key}"
        paths.add(path)
        if isinstance(value, dict):
            paths |= _paths_under(value, f"{path}.")
    return paths


def _write_back_in_place(
    target: Dict[str, Any], merged: Dict[str, Any], started_from: Any,
    *, held_tables=None, path=(),
) -> None:
    """Bring the live settings object up to what was just written.

    Updated IN PLACE, never reassigned: service_manager.ServiceManager and
    speech.speech_handler.SpeechHandler each store the object get_config()
    returned and read it for the life of the process, and a caller can hold a
    nested table the same way. Replacing either would leave them reading a
    dictionary nothing updates again.

    A key is brought up to date only while its value still equals the one the
    save started from. save() copies the settings before its first await, so a
    caller can change a key while the write runs in its worker thread; that
    change belongs to the next write, and overwriting it here would undo it.

    Staged saves also supply held table identities. Those distinguish a newer
    sibling edit from replacement of an entire table, allowing a successfully
    staged leaf to publish without overwriting either kind of newer change.
    """
    seen = started_from if isinstance(started_from, dict) else {}

    for key, merged_value in merged.items():
        if key not in target:
            if key in seen:
                # It was there when the save started and it is gone now, so a
                # caller removed it while the write ran. That removal belongs
                # to the next write; adding the key back would undo it.
                continue
            target[key] = copy.deepcopy(merged_value)
            continue
        current = target[key]
        if isinstance(current, dict) and isinstance(merged_value, dict):
            if current != seen.get(key):
                # A caller replaced the whole table while the write ran.
                # Walking into it would put the old table's keys into the new
                # one, which is the same undoing one level down.
                if held_tables is None or held_tables.get(path + (key,)) is not current:
                    continue
            if held_tables is not None:
                _write_back_in_place(current, merged_value, seen.get(key),
                                     held_tables=held_tables, path=path + (key,))
                continue
            _write_back_in_place(current, merged_value, seen.get(key))
            continue
        if key in seen and current == seen[key] and current != merged_value:
            target[key] = copy.deepcopy(merged_value)

    for key in list(target):
        if key in merged:
            continue
        if key in seen and target[key] == seen[key]:
            del target[key]

# Default remote STT provider used when stt.last_provider is absent from config.
# Must match the value config.toml.example ships (guarded by
# tests/test_stt_default_provider.py). Kept local/offline on purpose: a config
# that has lost the key degrades to the no-account Parakeet provider rather than
# a cloud provider that needs a Google account (wh-stt-fallback-default-google).
DEFAULT_STT_PROVIDER = "parakeet_tdt"

# The one speech-engine mode the program can run. wh-in-process-capture-removal
# deleted the in-process engine, so every other value the key can carry is now
# unusable. The key itself stays: it is what the WARNING below reads, and a
# settings file written by the in-process build would otherwise start remote
# without saying why. Nothing branches on the value any more -- one mode
# leaves nothing to branch on -- so the read below reports and returns.
STT_MODE_REMOTE = "remote"
STT_MODE_KEY = "stt.mode"

# The settings file said nothing at all about the mode. Distinguishing that
# from a value that is present and unusable is the whole reason for a sentinel:
# get() cannot tell them apart, and a missing key is not a mistake to report.
_STT_MODE_ABSENT = object()

# Which unusable values this process has already reported, keyed by repr so a
# list or a table -- both writable in TOML, neither hashable -- can be recorded
# like any other. One WARNING per process per value. The record was added when
# state_manager read the mode on every state update, three seconds apart, and a
# per-call warning would have buried the log; that reader is gone
# (wh-in-process-capture-removal) and startup is now the only caller, but the
# record is what makes "once per process" a property of the function rather
# than of who happens to call it.
_WARNED_STT_MODES: Set[str] = set()


def forget_stt_mode_warnings() -> None:
    """Clear the record of which unusable modes have been reported.

    The record is process state, so a test that reads a bad mode would
    otherwise silence the next test that reads the same one.
    """
    _WARNED_STT_MODES.clear()


def warn_if_stt_mode_unsupported(config_service: "ConfigService") -> None:
    """Report a settings file that names a speech-engine mode we do not have.

    wh-stt-mode-normalize. Seven places used to read stt.mode raw, and they
    disagreed about every value that was neither "remote" nor "in_process":
    one built the remote launcher, one started neither speech engine, and
    one reported the in-process provider. A settings file saying
    `mode = "remotee"` therefore left the user with remote machinery, no
    running engine, and an empty provider menu -- no working hands-free
    recovery (wh-remote-stt-robustness.2.4). The fix routed all seven
    through one helper that answered with the one mode the program could
    run.

    wh-in-process-capture-removal deleted the in-process engine, which left
    one mode and so nothing to resolve. What survives is the report, and
    this function is now only that: it takes no decision and hands back no
    answer. A value that is present and unusable -- an unrecognized string,
    or a value that is not a string at all -- is written to the log once
    per process at WARNING. A missing key says nothing, because a file that
    leaves the key out is the ordinary case. Neither is a configuration
    error: refusing to start would take away the voice control the user
    needs in order to correct the file.
    """
    raw = config_service.get(STT_MODE_KEY, _STT_MODE_ABSENT)
    if raw is _STT_MODE_ABSENT:
        return
    if isinstance(raw, str) and raw == STT_MODE_REMOTE:
        return

    quoted = repr(raw)
    if quoted not in _WARNED_STT_MODES:
        _WARNED_STT_MODES.add(quoted)
        logger.warning(
            f"The settings file gives {STT_MODE_KEY} as {quoted}, which is "
            f'not a mode this program has. The only mode it accepts is '
            f'"{STT_MODE_REMOTE}", and that is the mode it is running.'
        )


class ConfigService:
    """
    A service to manage application configuration.

    It reads configuration from a TOML file and provides
    a simple interface to access configuration values.
    """
    _config: Dict[str, Any] = {}

    def __init__(self, config_path: str = None):
        """
        Initializes the ConfigService.

        Args:
            config_path: The path to the configuration file. If None, it defaults
                         to 'config.toml' in the same directory as this file.
        """
        if config_path is None:
            # Default to config.toml in the same directory as this script
            base_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(base_dir, "config.toml")
        
        self.config_path = config_path
        # Lets only one settings write run at a time; see save().
        self._save_lock = asyncio.Lock()
        self.load_config(self.config_path)

    def load_config(self, config_path: str):
        """:flow: Configuration Loading
        :step: 1
        :description: Load and parse TOML configuration file
        :data_in: config_path (absolute path to config.toml)
        :data_out: Parsed configuration dictionary stored in self._config
        :notes: Startup-critical configuration loading. Opens config.toml in binary mode, parses via tomllib.load() into nested dictionary structure. Uses 'pull' model - synchronous, fail-fast loading. If file not found or invalid TOML, raises exception to prevent app startup with invalid config. This ensures stable, validated configuration before any services initialize. For runtime-changeable settings (commands.toml), use EventBus 'push' model instead.
        
        Args:
            config_path: The path to the configuration file.

        Raises:
            FileNotFoundError: If the configuration file cannot be found.
            ValueError: If the configuration file is not valid TOML.
        """
        try:
            with open(config_path, "rb") as f:
                self._config = tomllib.load(f)
            # What the file held when the program last read it. save() compares
            # against this to tell a value the program changed from one only the
            # file changed (wh-config-save-clobbers-hand-edits).
            self._loaded = copy.deepcopy(self._config)
            # Key paths the program has taken out since this read. The merge
            # in save() needs them to tell a removal the program asked for
            # from a key it never held.
            self._removed: Set[str] = set()
        except FileNotFoundError as e:
            logger.error(f"Error: Configuration file not found at {config_path}")
            raise e
        except tomllib.TOMLDecodeError as e:
            logger.error(f"Error: Could not decode TOML from {config_path}")
            raise ValueError(f"Invalid TOML format in {config_path}") from e

    def get_config(self) -> Dict[str, Any]:
        """Returns the entire configuration dictionary.
        
        Returns:
            Dict containing full configuration tree
        """
        return self._config

    def get(self, key: str, default: Any = None) -> Any:
        """
        Retrieves a configuration value for a given key.
        
        Supports dot notation for nested keys (e.g., "plugins.bravia.device_name").

        Args:
            key: The configuration key to retrieve. Use dots for nested keys.
            default: The default value to return if the key is not found.

        Returns:
            The configuration value, or the default if not found.
        """
        # Handle dot notation for nested keys
        if "." in key:
            keys = key.split(".")
            value = self._config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                    if value is None:
                        return default
                else:
                    return default
            return value
        
        # Simple key lookup
        return self._config.get(key, default)

    def get_persisted(self, key: str, default: Any = None) -> Any:
        """Read a detached value from the last completed disk snapshot.

        Live edits may be newer than this snapshot. Acknowledgements and
        reconciliation must never describe those edits as persisted.
        """
        view = copy.copy(self)
        view._config = self._loaded
        return copy.deepcopy(view.get(key, default))

    def set(self, key: str, value: Any):
        """
        Sets a configuration value in memory.
        
        Supports dot notation for nested keys (e.g., "stt.provider").

        Args:
            key: The configuration key to set. Use dots for nested keys.
            value: The value to set.
        """
        # Handle dot notation for nested keys
        if "." in key:
            keys = key.split(".")
            target = self._config
            for k in keys[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]
            # Only a table can be asked what it held. A settings file can
            # carry a plain value where a section belongs, and the answer to
            # that is the TypeError the assignment below raises, which
            # main.py catches and reports; reading through the value first
            # would raise a different error and walk past that handler.
            previous = target.get(keys[-1]) if isinstance(target, dict) else None
            target[keys[-1]] = value
        else:
            previous = self._config.get(key)
            self._config[key] = value

        if isinstance(previous, dict):
            # Putting a whole table in place takes out every key the program
            # was holding in it, and that is a removal like any other: the
            # merge has to know, or the file's copy of each dropped key reads
            # as a key the program never held and comes straight back
            # (wh-config-save-clobbers-hand-edits review finding .1.6).
            #
            # Recording a path that the new table still carries costs
            # nothing, because the merge asks this record only about a key
            # that is ABSENT from memory.
            #
            # A key the file gained by hand is deliberately NOT recorded: the
            # program never held it, so replacing the table says nothing
            # about it, and this bead exists because hand edits were being
            # discarded.
            self._removed.update(_paths_under(previous, f"{key}."))

    def unset(self, key: str):
        """
        Removes a configuration key from memory, if it is there.

        Supports dot notation for nested keys (e.g., "stt.provider").

        This is what a caller needs to undo a set() for a key that was not
        there beforehand. Setting the key back to None reads as absent but
        cannot be written: tomli_w has no representation for it, so the next
        save fails and takes every unrelated setting down with it.

        Args:
            key: The configuration key to remove. Use dots for nested keys.
        """
        keys = key.split(".") if "." in key else [key]
        target = self._config
        for k in keys[:-1]:
            if not isinstance(target, dict) or k not in target:
                return
            target = target[k]
        if isinstance(target, dict) and keys[-1] in target:
            del target[keys[-1]]
            # The next save reads the file again, where the key can still be.
            # Nothing else tells the merge that its absence from memory is a
            # removal the program asked for (wh-config-save-clobbers-hand-edits
            # review finding .1.2).
            self._removed.add(key)

    async def save(self, *, values: Optional[Dict[str, Any]] = None) -> bool:
        """
        Saves the current configuration to the TOML file.
        This is an async method that can be awaited.

        Optional values are staged using set()'s dot-path semantics in a private
        snapshot under the existing write lock. Failure publishes nothing;
        success publishes in place while preserving newer live edits. Caller
        values are copied before waiting. Cancellation of a staged save waits
        for its filesystem worker and publishes its actual outcome before
        propagating cancellation; cancellation cannot undo an atomic replace.

        Returns True when the settings reached the disk and False when they did
        not. Callers act on a save: the floating button reports its new size,
        and a provider switch logs success. Reporting a failure as a success
        makes each of those act on settings the next start will not have.
        wh-in-process-capture-removal deleted the third caller this paragraph
        used to name, the speech-mode change that restarted the program.

        The file is re-read immediately before the write and merged with the
        settings in memory, so an edit made to it by hand while the program
        runs is not thrown away (wh-config-save-clobbers-hand-edits). A key the
        program has changed since it last read the file takes the in-memory
        value; every other key keeps whatever the file says.

        Two callers can reach this at once -- one gesture that changes two
        settings, or two settings changed close together -- and each write
        replaces the whole file. Two of them running at the same time can
        leave the file torn, so a lock lets only one write run at a time, and
        the write itself goes to a temporary file that replaces the real one
        only once it is complete. A crash or a failed write then leaves the
        previous settings intact rather than a half-written file.

        The lock alone is not enough. Callers change the settings in memory
        first and ask for the write afterwards, so while one write runs in its
        worker thread another task can change a value and then wait its turn.
        A write that read the live settings would pick up half of that later
        change -- a new size with an old position, the very mismatch that
        sending them together prevents. So the write takes its own complete
        copy of the settings in one step, with nothing able to run in the
        middle of it, and records that copy.

        That copy is taken with the lock already held, not on the way in. A
        write that queues behind another one and copies first would compare
        the settings against a baseline the write in front has already
        replaced, read an untouched key as a program change, and write a hand
        edit away; the record of what the program removed goes stale the same
        way, and a removal the first write carried out would be applied a
        second time to a key the user had put back
        (wh-config-save-clobbers-hand-edits review finding .1.5). Waiting for
        the lock costs nothing here: the copy is still taken in one step on
        the event loop, so it holds a settled set of values, and a change made
        while this write waited simply travels with it instead of in the next
        write.
        """
        import os
        import tempfile

        import tomli_w

        outcome: Dict[str, Any] = {}
        snapshot: Dict[str, Any] = {}
        loaded: Dict[str, Any] = {}
        removed: Set[str] = set()
        staged = copy.deepcopy(values)
        held_tables = None
        cancelled = False

        def read_the_file_again() -> Any:
            """What the file holds right now, or None if it cannot be read.

            A file that will not parse is not a reason to lose the settings the
            program is holding, so the save goes ahead from memory and says so.
            """
            try:
                with open(self.config_path, "rb") as f:
                    return tomllib.load(f)
            except FileNotFoundError:
                return None
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
                # TOML is UTF-8 by definition, and tomllib decodes the bytes
                # before it parses them, so a file an editor saved as UTF-16
                # raises a decoding error, not a parse error. Both mean the
                # same thing here: the file cannot be read
                # (wh-config-save-clobbers-hand-edits review finding .1.7).
                logger.warning(
                    "Could not read %s before saving, so this save keeps only "
                    "the settings the program is holding and any edit made to "
                    "the file by hand is lost: %s",
                    self.config_path,
                    e,
                )
                return None
            except OSError as e:
                logger.warning(
                    "Could not open %s before saving, so this save keeps only "
                    "the settings the program is holding and any edit made to "
                    "the file by hand is lost: %s",
                    self.config_path,
                    e,
                )
                return None

        def stamp_of_the_file() -> Any:
            """The size and modification time of the file, or None.

            Enough to see that somebody else has written the file since it
            was read, and cheap enough to take twice in every save.
            """
            try:
                information = os.stat(self.config_path)
            except OSError:
                return None
            return information.st_size, information.st_mtime_ns

        def read_and_merge() -> Any:
            """What to write, what the file kept, and what it dropped."""
            on_disk = read_the_file_again()
            if on_disk is not None and not on_disk and snapshot:
                # A file with nothing left in it reads as a hand deletion of
                # every key, which one save would make permanent in the file
                # and in memory. A select-all in an editor, a crash that
                # truncates the file, or a synchronisation tool is a far more
                # likely cause than a user deleting every setting on purpose,
                # so this takes the same path as a file that will not parse
                # (wh-config-save-clobbers-hand-edits review finding .1.3).
                # A file that still holds one section is an edited file and
                # its deletions stand.
                logger.warning(
                    "%s holds no settings at all, so this save keeps the "
                    "settings the program is holding rather than reading the "
                    "empty file as a request to delete every one of them",
                    self.config_path,
                )
                on_disk = None
            if on_disk is None:
                return snapshot, [], []
            return _merge_file_over_memory(on_disk, snapshot, loaded, removed)

        def do_save() -> bool:
            """Synchronous file write operation for TOML config.

            Runs in thread pool via asyncio.to_thread to avoid blocking.
            """
            # The stamp is taken before the read, so a change that lands
            # between the two is seen as a change rather than missed.
            stamp = stamp_of_the_file()
            to_write, kept, dropped = read_and_merge()
            outcome["merged"] = to_write

            directory = os.path.dirname(os.path.abspath(self.config_path)) or "."
            handle = None
            temp_path = None
            try:
                handle, temp_path = tempfile.mkstemp(
                    dir=directory, prefix=".config-", suffix=".tmp"
                )
                with os.fdopen(handle, "wb") as f:
                    handle = None
                    tomli_w.dump(to_write, f)
                    f.flush()
                    os.fsync(f.fileno())

                if stamp_of_the_file() != stamp:
                    # Somebody saved the file while this copy was being
                    # prepared, so the copy carries settings read before
                    # that edit and would write it away
                    # (wh-config-save-clobbers-hand-edits review finding
                    # .1.9). Read the file and merge again, and write the
                    # temporary file again from the result.
                    #
                    # Exactly once, and never in a loop: an editor that
                    # saves every few seconds would otherwise hold this
                    # write open for as long as it kept saving.
                    logger.info(
                        "%s changed while this save was preparing its copy, "
                        "so the file is being read and merged once more",
                        self.config_path,
                    )
                    to_write, kept, dropped = read_and_merge()
                    outcome["merged"] = to_write
                    with open(temp_path, "wb") as f:
                        tomli_w.dump(to_write, f)
                        f.flush()
                        os.fsync(f.fileno())

                # crewcut: an edit saved between the comparison above and
                # the replace below is still lost. The window is now a few
                # file operations wide instead of the whole write, but it
                # cannot be closed this way, because no lock exists that a
                # text editor honours. Closing it needs a different design,
                # such as the program owning the file and the user editing
                # the settings through the program.
                os.replace(temp_path, self.config_path)
                temp_path = None
                logger.info(f"Configuration saved to {self.config_path}")
                if kept or dropped:
                    # Names only. The settings file holds account details and
                    # server addresses, and the log is read by other people.
                    parts = []
                    if kept:
                        parts.append(
                            "kept the value already in the file for: "
                            + ", ".join(sorted(kept))
                        )
                    if dropped:
                        parts.append(
                            "dropped, since the file no longer carries them: "
                            + ", ".join(sorted(dropped))
                        )
                    logger.info("Settings saved; %s", "; ".join(parts))
                return True
            except Exception as e:
                logger.error(f"Failed to save configuration: {e}")
                return False
            finally:
                if handle is not None:
                    try:
                        os.close(handle)
                    except OSError:
                        pass
                if temp_path is not None and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

        async with self._save_lock:
            # Copied with the lock held and before this write's first await,
            # so it is one settled set of values and no write in front can
            # still replace the baseline it is compared against.
            snapshot = copy.deepcopy(self._config)
            loaded = copy.deepcopy(getattr(self, "_loaded", {}))
            removed = set(self._removed)
            baseline = snapshot
            if staged is not None:
                baseline = copy.deepcopy(snapshot)
                held_tables = {}

                def remember_tables(table, path=()):
                    for key, value in table.items():
                        if isinstance(value, dict):
                            held_tables[path + (key,)] = value
                            remember_tables(value, path + (key,))

                remember_tables(self._config)
                # A shallow view reuses set() including table-removal tracking.
                # Only its private dictionaries are mutated; it never saves.
                draft = copy.copy(self)
                draft._config, draft._removed = snapshot, removed
                for key, value in staged.items():
                    draft.set(key, value)
            # Run the synchronous file I/O in a separate thread
            if staged is None:
                saved = await asyncio.to_thread(do_save)
            else:
                # Own completion independently of asyncio Tasks: shutdown's
                # cancel-all can cancel a to_thread Task while its thread is
                # still replacing the file. Shield this executor Future from
                # outer cancellation and keep the lock through publication.
                writer = asyncio.get_running_loop().run_in_executor(None, do_save)
                while True:
                    try:
                        saved = await asyncio.shield(writer)
                        break
                    except asyncio.CancelledError:
                        cancelled = True

            # Publish on the event loop while still owning the save lock.
            # Concurrent setters retain their newer live edits; _loaded is
            # exclusively the snapshot that actually reached disk.
            if saved and "merged" in outcome:
                _write_back_in_place(self._config, outcome["merged"], baseline,
                                     held_tables=held_tables)
                self._loaded = copy.deepcopy(outcome["merged"])
                # A removal made during this write still needs its own save.
                self._removed -= removed
        if cancelled:
            raise asyncio.CancelledError
        return saved

# Example of how to use it (optional, for testing)
if __name__ == "__main__":
    async def main_test():
        # Setup basic logging for the test
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        config_service = ConfigService()
        print(f"Bravia IP: {config_service.get('BRAVIA_IP')}")
        print(f"Original Log Level: {config_service.get('LOG_LEVEL', 'INFO')}")
        
        # Test setting and saving
        print("Setting LOG_LEVEL to DEBUG and saving...")
        config_service.set("LOG_LEVEL", "DEBUG")
        await config_service.save()
        
        # Verify by reloading
        print("Reloading configuration to verify save...")
        new_config_service = ConfigService()
        print(f"New Log Level from file: {new_config_service.get('LOG_LEVEL')}")

        # Revert the change
        print("Reverting LOG_LEVEL to INFO...")
        new_config_service.set("LOG_LEVEL", "INFO")
        await new_config_service.save()
        print("Change reverted.")

    asyncio.run(main_test())
