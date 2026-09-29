"""Start-of-run rotation of the shared wheelhouse.log (wh-log-triple-rotation).

Every WheelHouse process writes to one wheelhouse.log in the project root.
A new run starts a fresh file: the previous run's log moves to
wheelhouse.log.1, and older backups move one place back, up to
LOG_BACKUP_COUNT files.

The launcher rotates once per start cycle, before it writes its first line
of the cycle and before it starts the Logic, Input, and GUI processes. It
then sets LAUNCHER_ROTATED_ENV, which those processes inherit, so their
setup_logging calls do not rotate again. Before this module, each of the
three processes rotated at its own start, so one start could rotate the file
three times and leave two small startup logs as backups.

The module itself imports only the standard library, so the launcher can
import it at module level; rotate_log_for_new_run imports
concurrent_log_handler when it runs, and the launcher reports that import's
failure like any other rotation failure.
"""

import os
import uuid

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# The launcher sets this to "1" before it starts any child process.
LAUNCHER_ROTATED_ENV = "WHEELHOUSE_LOG_ROTATED_BY_LAUNCHER"


def rotate_log_for_new_run(log_file_path: str) -> bool:
    """Move a non-empty log to backup 1 so the new run starts a fresh file.

    Returns True when the file moved, and False when there was nothing to
    move or the log itself could not move (another program holds it open).
    Raises when the lock cannot be taken or a backup cannot be renamed; the
    log and every backup are then back under their own names, so the run
    appends to the log and no backup is lost.

    The move runs inside the library's cross-process lock, the same lock
    its own size rotation and every write take, so a process writing to the
    file at that moment cannot collide with the move. The lock calls are
    the library's private _do_lock and _do_unlock; the dependency pin
    (concurrent-log-handler < 0.10) keeps them stable.
    """
    if not log_has_content(log_file_path):
        return False
    from concurrent_log_handler import ConcurrentRotatingFileHandler

    handler = ConcurrentRotatingFileHandler(
        log_file_path,
        mode="a",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    try:
        handler._do_lock()
        try:
            # Checked again under the lock: another process may have moved
            # the file between the first check and the lock.
            if not log_has_content(log_file_path):
                return False
            return _move_log_to_first_backup(log_file_path)
        finally:
            handler._do_unlock()
    finally:
        handler.close()


def _move_log_to_first_backup(log_file_path: str) -> bool:
    """Shift each backup one place back and move the log to backup 1.

    The library's doRollover is not used here. It moves the log to a
    temporary wheelhouse.log.rotate.* name before it shifts the backups,
    and when a backup cannot move (another program holds it open) it
    raises with the previous run still under that temporary name
    (wh-log-triple-rotation.1.2).

    Nothing is overwritten until every move has succeeded: the oldest
    backup first moves to a temporary name, so each later move goes to a
    free name. When a move fails, every completed move is undone in
    reverse order, so the log and all backups are back under their own
    names before the error is raised. Without the undo, each failed start
    deleted one more old backup (wh-log-triple-rotation.1.3).
    """
    stamp = uuid.uuid4().hex
    temp_path = f"{log_file_path}.rotate.{stamp}"
    try:
        os.rename(log_file_path, temp_path)
    except OSError:
        return False
    oldest_path = f"{log_file_path}.{LOG_BACKUP_COUNT}"
    dropped_path = f"{oldest_path}.rotate.{stamp}"
    done = [(log_file_path, temp_path)]
    try:
        if os.path.exists(oldest_path):
            os.rename(oldest_path, dropped_path)
            done.append((oldest_path, dropped_path))
        for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
            backup_path = f"{log_file_path}.{i}"
            if os.path.exists(backup_path):
                os.rename(backup_path, f"{log_file_path}.{i + 1}")
                done.append((backup_path, f"{log_file_path}.{i + 1}"))
        os.rename(temp_path, f"{log_file_path}.1")
    except BaseException as error:
        _undo_moves(done, error)
        raise
    try:
        os.remove(dropped_path)
    except FileNotFoundError:
        pass
    except OSError:
        # crewcut: a dropped backup that cannot be deleted stays as
        # wheelhouse.log.5.rotate.<hex>. The rename that just succeeded
        # means no program held it without delete sharing, so this needs a
        # program that opened it in the last moment; a later start does
        # not retry the delete. Remove it on the next start to lift this.
        pass
    return True


def _undo_moves(done: list, error: BaseException) -> None:
    """Move each file back to its old name, newest move first.

    The log's own move is undone last. A move that cannot be undone (a
    program has just opened that backup to read it) is left in place and
    named in a note on error, and the other moves are still undone, so the
    log returns to its own name whenever it can
    (wh-log-triple-rotation.1.4). On Windows os.rename refuses a name that
    is taken again, so an undo there never overwrites a file.
    """
    for source, destination in reversed(done):
        try:
            os.rename(destination, source)
        except OSError as undo_error:
            error.add_note(f"could not move {destination} back to {source}: {undo_error}")


def log_has_content(log_file_path: str) -> bool:
    try:
        return os.path.getsize(log_file_path) > 0
    except OSError:
        return False
