"""Atomic writer for the declined-tuple file (wh-27gvv).

Owned by the Logic process. The Logic process loads the file at
startup to populate the in-memory declined set consulted by the
grant-prompt forwarder. On a No click against the three-strikes
grant prompt, the Logic process writes the new entry here first;
only on a successful write does it update the in-memory set. A
write failure leaves the in-memory set untouched and surfaces a
"couldn't save your choice" notice on the GUI state queue.

The writer uses the standard atomic-write idiom:

  1. Open a temp file in the same directory as the target.
  2. Write the serialised TOML bytes.
  3. flush the Python buffer and os.fsync the file descriptor so the
     bytes are durable on disk.
  4. Close the temp file.
  5. os.replace(temp, target) -- atomic on Windows since Python 3.3.

If any step before os.replace fails, the function deletes the partial
temp file and returns False. The target is left untouched.

The shape mirrors utils.soft_allow_writer: same atomic-write idiom,
same append-with-dedup contract, same return type.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import tomli_w

from shared.declined_tuples_schema import parse_declined_file

logger = logging.getLogger(__name__)


# Serialise the read-modify-write cycle in append_declined_tuple so
# two concurrent add_declined calls (from asyncio.to_thread workers in
# the Logic process) cannot read the same baseline file, append
# disjoint tuples, and overwrite each other.
_APPEND_LOCK = threading.Lock()


DeclinedTupleWithMeta = tuple[str, str, str, str]
"""(process_name, class_name, control_type, added_at_iso)."""


def write_declined_tuples(
    tuples_with_meta: list[DeclinedTupleWithMeta],
    path: Path,
) -> bool:
    """Atomically rewrite the declined file with the given tuples.

    Returns True on success, False on any failure. Failure does not
    raise; the caller can surface a 'couldn't save' notice or fall
    back to in-memory-only state. The target is left untouched on
    failure.

    The parent directory is created if it does not exist.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "declined writer: could not create parent dir for %s: %s",
            path, exc,
        )
        return False

    payload = _serialise(tuples_with_meta)

    temp_path: str | None = None
    try:
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
            "declined writer: failed to write %s: %s",
            path, exc,
        )
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def append_declined_tuple(
    new_tuple: DeclinedTupleWithMeta,
    path: Path,
) -> bool:
    """Read the existing file, append the new tuple if novel, then rewrite.

    Identity is (process_name, class_name, control_type). When the
    identity matches an existing entry, the new entry is dropped (the
    existing added_at -- the user's original decline timestamp -- is
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
            return write_declined_tuples(list(existing), path)
        return write_declined_tuples(list(existing) + [new_tuple], path)


def _read_existing(path: Path) -> list[DeclinedTupleWithMeta]:
    """Best-effort read of the existing file. Errors return [].

    Delegates schema validation to shared.declined_tuples_schema and
    projects each DeclinedEntry to the (process, class, control_type,
    added_at) shape used by the read-modify-write cycle.

    log_skipped_entries=False: a per-entry warning would re-fire on
    every append against a stale file. The rewrite path drops bad
    entries on the next write, so the loader's manual-edit-surface
    warning is not useful here.
    """
    entries = parse_declined_file(
        path,
        log_skipped_entries=False,
        caller="declined writer",
    )
    return [
        (e.process_name, e.class_name, e.control_type, e.added_at)
        for e in entries
    ]


def _serialise(tuples_with_meta: list[DeclinedTupleWithMeta]) -> bytes:
    """Render the tuple list as TOML bytes.

    An empty list emits an empty document (no [[entries]] section),
    which is the documented initial state.
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
