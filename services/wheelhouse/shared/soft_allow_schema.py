"""Shared schema validator for soft_allow_tuples.toml (wh-65e46.2).

The soft-allow file is read by two callers:

  - services/wheelhouse/ui/text_target.py:_load_soft_allow_tuples
    (input process loader -- projects entries to a frozenset of
    (process_name, class_name, control_type) identity triples for
    predicate lookup)

  - services/wheelhouse/utils/soft_allow_writer.py:_read_existing
    (logic process reader -- projects entries to a list of 4-tuples
    with added_at for read-modify-write rewrites)

Before this module each caller had its own parser. The two diverged on
whether to warn per skipped entry, on top-level malformed handling, and
on added_at validation. This module defines the schema once. Each caller
consumes ParsedEntry and projects to the shape it needs.

The schema (per services/wheelhouse/data/soft_allow_tuples.toml docstring):
each entry is a TOML table with four required string fields --
process_name, class_name, control_type, added_at. Missing fields,
non-string values, or non-table entries are skipped. The top-level
shape is an "entries" array of tables; a missing "entries" key is the
documented initial state and yields an empty result silently. A
non-list "entries" key is malformed user input and is logged.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedEntry:
    """A validated soft-allow entry with the four schema fields."""

    process_name: str
    class_name: str
    control_type: str
    added_at: str


def parse_soft_allow_file(
    path: Path,
    *,
    log_skipped_entries: bool,
    caller: str,
) -> list[ParsedEntry]:
    """Read the soft-allow file and return the list of valid entries.

    Missing file -> empty list, no warning (the documented initial state).
    OS read failures and TOML decode failures -> empty list with a
    WARNING log. Each entry must contain process_name, class_name,
    control_type, and added_at, all strings; entries that fail the
    schema check are skipped.

    Args:
        path: location of soft_allow_tuples.toml.
        log_skipped_entries: if True, log a WARNING per skipped entry.
            The input-process loader passes True so manual-edit mistakes
            surface in the log; the logic-process writer reader passes
            False because its rewrite path drops bad entries on the
            next write and a per-entry warning would re-fire on every
            append.
        caller: identifier woven into log messages so the source of a
            warning is unambiguous (e.g. "soft_allow loader",
            "soft_allow writer"). Without it the same logger name and
            warning text appear from both call sites.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(
            "%s: could not read %s: %s -- treating as empty",
            caller, path, exc,
        )
        return []

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "%s: malformed file %s: %s -- treating as empty",
            caller, path, exc,
        )
        return []

    entries = data.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        logger.warning(
            "%s: 'entries' in %s must be an array of tables, got %s -- "
            "treating as empty",
            caller, path, type(entries).__name__,
        )
        return []

    out: list[ParsedEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            if log_skipped_entries:
                logger.warning(
                    "%s: skipping non-table entry in %s: %r",
                    caller, path, entry,
                )
            continue
        process_name = entry.get("process_name")
        class_name = entry.get("class_name")
        control_type = entry.get("control_type")
        added_at = entry.get("added_at")
        if not (
            isinstance(process_name, str)
            and isinstance(class_name, str)
            and isinstance(control_type, str)
            and isinstance(added_at, str)
        ):
            if log_skipped_entries:
                logger.warning(
                    "%s: skipping incomplete entry in %s: %r",
                    caller, path, entry,
                )
            continue
        out.append(
            ParsedEntry(
                process_name=process_name,
                class_name=class_name,
                control_type=control_type,
                added_at=added_at,
            )
        )
    return out
