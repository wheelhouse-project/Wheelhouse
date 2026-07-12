"""Atomic writer for the click counter persistence file (wh-82lnx).

Owned by the Logic process. The wh-82lnx ClickCounter writes a snapshot
of all per-tuple counts to ``services/wheelhouse/data/soft_allow_pending_counters.toml``
after every verified retry.

The writer mirrors the wh-z0usg soft-allow writer: temp file in the same
directory, write, flush, fsync, atomic os.replace. Failure returns False
and logs a warning; the caller decides what to do (the click counter
treats persistence as best-effort, so a write failure does not block the
user).

The file format is a single ``[[entries]]`` array of dicts with fields
``process_name``, ``class_name``, ``control_type``, ``count``,
``last_updated_at`` (ISO-8601 UTC). The schema deliberately matches the
soft-allow writer's tuple-with-meta shape so a future consolidation can
share format helpers.
"""
from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path

import tomli_w

logger = logging.getLogger(__name__)


CounterEntry = tuple[str, str, str, int, str]
"""(process_name, class_name, control_type, count, last_updated_at_iso)."""


def write_pending_counters(entries: list[CounterEntry], path: Path) -> bool:
    """Atomically rewrite the pending-counters file with the given entries.

    Returns True on success, False on any failure. Failure does not
    raise; the caller logs and continues with in-memory state. The
    target file is left untouched on failure.

    The parent directory is created if it does not exist.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "click_counter writer: could not create parent dir for %s: %s",
            path, exc,
        )
        return False

    temp_path: str | None = None
    try:
        # Serialise inside the try block so a tomli_w.dumps failure
        # (TypeError on a non-serialisable entry shape, etc.) honours
        # the no-raise contract instead of escaping through
        # asyncio.to_thread as an unhandled task exception
        # (wh-82lnx.1.1). The OSError-vs-Exception split below is
        # intentional: serialise errors are programming bugs the
        # caller cannot recover from but must still see as a warning,
        # while OSError on the file path is the routine "disk full /
        # permission denied" case.
        try:
            payload = _serialise(entries)
        except Exception as exc:
            logger.warning(
                "click_counter writer: failed to serialise entries for %s: %s",
                path, exc,
            )
            return False
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
            "click_counter writer: failed to write %s: %s",
            path, exc,
        )
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def read_pending_counters(path: Path) -> list[CounterEntry]:
    """Best-effort read of the existing pending-counters file.

    Returns the list of well-formed entries. Missing file, malformed
    TOML, missing keys, and wrong types each yield an empty list (or
    skip the malformed entry). The caller restarts with a clean
    in-memory state in those cases; the worst symptom is the user sees
    the standard rejection toast a few more times before the threshold
    re-fires.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(
            "click_counter writer: could not read existing file %s: %s -- "
            "treating as empty",
            path, exc,
        )
        return []

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "click_counter writer: malformed existing file %s: %s -- "
            "treating as empty",
            path, exc,
        )
        return []

    entries = data.get("entries")
    if not isinstance(entries, list):
        return []

    out: list[CounterEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        process_name = entry.get("process_name")
        class_name = entry.get("class_name")
        control_type = entry.get("control_type")
        count = entry.get("count")
        last_updated_at = entry.get("last_updated_at", "")
        if not (
            isinstance(process_name, str)
            and isinstance(class_name, str)
            and isinstance(control_type, str)
            and isinstance(count, int)
            and isinstance(last_updated_at, str)
        ):
            continue
        if count < 0:
            continue
        out.append((process_name, class_name, control_type, count, last_updated_at))
    return out


def _serialise(entries: list[CounterEntry]) -> bytes:
    if not entries:
        return b""
    payload = {
        "entries": [
            {
                "process_name": e[0],
                "class_name": e[1],
                "control_type": e[2],
                "count": e[3],
                "last_updated_at": e[4],
            }
            for e in entries
        ],
    }
    return tomli_w.dumps(payload).encode("utf-8")
