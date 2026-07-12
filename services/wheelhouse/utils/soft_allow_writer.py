"""Atomic writer for the soft-allow tuple file (wh-9weum Phase 3 / wh-z0usg).

Owned by the Logic process. The Input process never writes the file --
it only updates its in-memory set via TextTargetPredicate.add_soft_allow.
The Logic process writes disk first via append_soft_allow_tuple; only
on a successful write does it send the IPC command to the Input process
that updates the in-memory set. That ordering means a fresh restart
recovers the same set the running Input process holds, so a write
failure is observable as the toast not disappearing on the next
rejection (Phase 4 wires the user-visible feedback).

The writer uses the standard atomic-write idiom:

  1. Open a temp file in the same directory as the target.
  2. Write the serialised TOML bytes.
  3. flush the Python buffer and os.fsync the file descriptor so the
     bytes are durable on disk.
  4. Close the temp file.
  5. os.replace(temp, target) -- atomic on Windows since Python 3.3.

If any step before os.replace fails, the function deletes the partial
temp file and returns False. The target is left untouched.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import tomli_w

from shared.soft_allow_schema import parse_soft_allow_file

logger = logging.getLogger(__name__)


# wh-9weum.4.5: serialise the read-modify-write cycle in
# append_soft_allow_tuple so two concurrent add_soft_allow calls (from
# asyncio.to_thread workers in the Logic process) cannot read the same
# baseline file, append disjoint tuples, and overwrite each other.
# The lock is module-scoped because the file path is the same across
# all callers in production; tests that pass distinct paths still
# serialise but the only cost is reduced concurrency, not correctness.
_APPEND_LOCK = threading.Lock()


SoftAllowTupleWithMeta = tuple[str, str, str, str]
"""(process_name, class_name, control_type, added_at_iso)."""


def write_soft_allow_tuples(
    tuples_with_meta: list[SoftAllowTupleWithMeta],
    path: Path,
) -> bool:
    """Atomically rewrite the soft-allow file with the given tuples.

    Returns True on success, False on any failure. Failure does not
    raise; the caller can surface a 'couldn't save' toast or fall back
    to in-memory-only state. The target is left untouched on failure.

    The parent directory is created if it does not exist.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "soft_allow writer: could not create parent dir for %s: %s",
            path, exc,
        )
        return False

    payload = _serialise(tuples_with_meta)

    temp_path: str | None = None
    try:
        # delete=False so we can close the handle, fsync the fd, then
        # rename. NamedTemporaryFile with dir=path.parent guarantees the
        # temp file is on the same filesystem as the target, so
        # os.replace is a true atomic rename (cross-volume rename is
        # implemented as copy + unlink which is not atomic).
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
        ) as tmp:
            temp_path = tmp.name
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        logger.warning(
            "soft_allow writer: failed to write %s: %s",
            path, exc,
        )
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def append_soft_allow_tuple(
    new_tuple: SoftAllowTupleWithMeta,
    path: Path,
) -> bool:
    """Read the existing file, append the new tuple if novel, then rewrite.

    Identity is (process_name, class_name, control_type). When the
    identity matches an existing entry, the new entry is dropped (the
    existing added_at -- the user's original approval timestamp -- is
    canonical). Returns True on a successful write, False on failure
    (a malformed existing file is treated as empty and overwritten).
    """
    with _APPEND_LOCK:
        existing = _read_existing(path)
        new_identity = (new_tuple[0], new_tuple[1], new_tuple[2])
        seen_identities = {
            (e[0], e[1], e[2]) for e in existing
        }
        if new_identity in seen_identities:
            # Already present -- rewrite the file with the existing
            # entries so a malformed file gets normalised, but
            # otherwise this is a no-op for the caller.
            return write_soft_allow_tuples(list(existing), path)
        return write_soft_allow_tuples(list(existing) + [new_tuple], path)


def _read_existing(path: Path) -> list[SoftAllowTupleWithMeta]:
    """Best-effort read of the existing file. Errors return [].

    Delegates schema validation to shared.soft_allow_schema.parse_soft_allow_file
    and projects each ParsedEntry to the (process, class, control_type,
    added_at) shape used by the read-modify-write cycle.

    log_skipped_entries=False: a per-entry warning would re-fire on
    every append against a stale file. The rewrite path drops bad
    entries on the next write, so the loader's manual-edit-surface
    warning is not useful here.
    """
    entries = parse_soft_allow_file(
        path,
        log_skipped_entries=False,
        caller="soft_allow writer",
    )
    return [
        (e.process_name, e.class_name, e.control_type, e.added_at)
        for e in entries
    ]


def _serialise(tuples_with_meta: list[SoftAllowTupleWithMeta]) -> bytes:
    """Render the tuple list as TOML bytes.

    An empty list emits an empty document (no [[entries]] section), which
    is the documented initial state.
    """
    if not tuples_with_meta:
        return b""
    payload = {
        "entries": [
            {
                "process_name": t[0],
                "class_name": t[1],
                "control_type": t[2],
                "added_at": t[3],
            }
            for t in tuples_with_meta
        ],
    }
    return tomli_w.dumps(payload).encode("utf-8")
