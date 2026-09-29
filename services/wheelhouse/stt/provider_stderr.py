"""Copy a speech provider's error output into wheelhouse.log.

wh-provider-native-crash-trace. The provider's supervisor
(services/stt_providers/shared/shared_stt/launcher.py) starts main.py
with PYTHONFAULTHANDLER=1 and its stderr in
``<app_data_dir>/<provider_name>.stderr.log``, and appends one line per
child exit:

    [launcher] <app_name> process exited with code N (0xHHHHHHHH) after Ss

That supervisor runs in the provider's own virtual environment and
cannot write wheelhouse.log, and it exits 0 whatever its child's code
was. So WheelHouse reads the file: at each launch it records the file's
size, and when a provider dies or reports ready it copies the lines
added since then.

Only an allow-list of lines is copied (ruling R3). faulthandler lines
carry file names, line numbers and function names; a Python exception
message can carry recognized text, so it passes through
redact_transcript like every other transcript in the log. Every other
line is counted, never copied.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from utils.redact import redact_transcript

logger = logging.getLogger(__name__)

#: At most this many of the newest bytes are copied at one time.
MAX_COPY_BYTES = 16 * 1024
#: A copied line is cut to this many characters.
MAX_LINE_CHARS = 500

_EXIT_LINE = re.compile(r"^\[launcher\] \S+ process exited with code (-?\d+) ")

# Lines copied whole. Each is a fixed faulthandler or traceback form.
_KEPT = tuple(re.compile(pattern) for pattern in (
    r"^\[launcher\] ",
    r"^Windows fatal exception: ",
    r"^Fatal Python error: ",
    r"^(Current thread|Thread) 0x[0-9a-fA-F]+ \(most recent call first\):$",
    r"^Stack \(most recent call first\):$",
    r"^Traceback \(most recent call last\):$",
    r"^\s+<no Python frame>$",
    r'^\s+File ".*", line \d+,? in \S+$',
    r"^Extension modules: ",
))

# An exception line: the type is kept, the message is redacted.
_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*): (.*)$")

#: STATUS_CONTROL_C_EXIT: an error-severity code that is not a crash.
_CTRL_C_EXIT = 0xC000013A


def stderr_log_path(app_data_dir, provider_name: str) -> Path:
    """The file the supervisor writes; shared_stt.launcher.get_stderr_log_path
    builds the same name from the provider's app_name, which equals the
    lower-case provider name."""
    return Path(app_data_dir) / f"{provider_name}.stderr.log"


def filter_lines(lines) -> list[str]:
    """Return the lines that may enter wheelhouse.log.

    Blank lines are dropped. Lines outside the allow-list are counted
    into one closing line.
    """
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        if any(pattern.match(line) for pattern in _KEPT):
            kept.append(line[:MAX_LINE_CHARS])
            continue
        exception = _EXCEPTION.match(line)
        if exception:
            kept.append(
                f"{exception.group(1)}: {redact_transcript(exception.group(2))}"
                [:MAX_LINE_CHARS])
            continue
        dropped += 1
    if dropped:
        kept.append(f"<{dropped} other stderr lines not copied>")
    return kept


def is_native_crash(code: Optional[int]) -> bool:
    """True for a Windows error-severity status such as 0xC0000005.

    These codes come from the operating system ending the process after
    a fault in native code; Python code exits with small numbers. Ctrl+C
    also has error severity and is excluded.
    """
    if code is None:
        return False
    code &= 0xFFFFFFFF
    return code >= 0xC0000000 and code != _CTRL_C_EXIT


def native_crash_message(display_name: str, code: int, restarting: bool) -> str:
    """The notice text (David approved the restarting text 2026-09-25
    09:07; the stopped text is ruling R4, overridable)."""
    hex_code = f"0x{code & 0xFFFFFFFF:08X}"
    head = (f"The {display_name} speech engine stopped because of an error "
            f"in its program code (exit code {hex_code})")
    if restarting:
        return (f"{head}. WheelHouse is starting it again. "
                "The file wheelhouse.log has the details.")
    return (f"{head} and did not start again. "
            "The file wheelhouse.log has the details.")


class ProviderStderrTail:
    """Per-provider read position in each provider's stderr file.

    Called from the startup monitor threads, the Logic state loop and
    the WebSocket event loop, so the positions sit under one lock. The
    read itself is at most MAX_COPY_BYTES of a local file.
    """

    def __init__(self, app_data_dir) -> None:
        self._app_data_dir = Path(app_data_dir)
        self._offsets: dict[str, int] = {}
        # The identity of the file each offset belongs to; None when the
        # file system gives none.
        self._file_ids: dict[str, Optional[tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def mark_launch(self, provider_name: str) -> None:
        """Start reading at the file's current end: an earlier run's lines
        are not this launch's.

        A missing file is created here, so the launch has a file identity
        to compare against when the supervisor moves the file aside.
        """
        path = stderr_log_path(self._app_data_dir, provider_name)
        try:
            open(path, "ab").close()
        except OSError:
            pass  # read by size alone, the fallback in _copy_new
        try:
            size, file_id = _examine(path)
        except OSError:
            size, file_id = 0, None
        with self._lock:
            self._offsets[provider_name] = size
            self._file_ids[provider_name] = file_id

    def copy_new(self, provider_name: str) -> Optional[int]:
        """Log the complete lines added since the last read.

        Returns the exit code of the last "[launcher]" exit line among
        them, or None. Never raises.
        """
        try:
            return self._copy_new(provider_name)
        except Exception as exc:  # a diagnostic must never stop a caller
            logger.debug(f"Could not read {provider_name} stderr: {exc}")
            return None

    def _copy_new(self, provider_name: str) -> Optional[int]:
        path = stderr_log_path(self._app_data_dir, provider_name)
        # Each part is (bytes, count of bytes skipped before them), oldest first.
        parts: list[tuple[bytes, int]] = []
        # True when the file the provider writes now could not be read: its
        # lines may hold the exit that ended the run, so no earlier exit
        # code is returned (wh-provider-native-crash-trace.2.4).
        active_unread = False
        with self._lock:
            offset = self._offsets.get(provider_name)
            known_id = self._file_ids.get(provider_name)
            # One examination gives the size and the identity together, so
            # they cannot disagree. A file that exists but cannot be
            # examined now changes no position: it is examined again next
            # time, so nothing is lost or read twice
            # (wh-provider-native-crash-trace.2.2, .2.3).
            try:
                size, current_id = _examine(path)
            except FileNotFoundError:
                size, current_id = None, None
            except OSError:
                return None
            if offset is None:
                # Not launched by this WheelHouse: start at the current end.
                self._offsets[provider_name] = size or 0
                self._file_ids[provider_name] = current_id
                return None
            path_missing = size is None
            if path_missing:
                # Missing: between the supervisor's rename and its new file.
                # That is a move only when the .1 file is the launch's own,
                # checked below.
                if known_id is None:
                    return None
                moved_aside = True
                size = 0
            # The supervisor moves a large file to .1 at each child start.
            # A rename keeps the file's identity, so a different identity
            # at the path means a move; the size cannot show it, because
            # the new file can already be longer than the offset
            # (wh-provider-native-crash-trace.2.1).
            elif known_id is not None:
                moved_aside = current_id != known_id
            else:
                moved_aside = size < offset
            if moved_aside:
                # What this launch wrote before the move is in the .1 file
                # after the offset. At the launch's first child start the
                # .1 file is exactly as long as the offset and holds none
                # of it. A .1 file that cannot be examined or read now
                # returns before any position changes, so the next call
                # reads it again.
                # crewcut: two moves inside one launch lose the older part,
                # because .1 then holds another file. Remove the limit by
                # numbering the moved files.
                moved = Path(f"{path}.1")
                try:
                    moved_size, moved_id = _examine(moved)
                except FileNotFoundError:
                    moved_size, moved_id = 0, None
                    ours = False
                except OSError:
                    return None
                else:
                    ours = known_id is None or moved_id == known_id
                if path_missing and not ours:
                    return None  # no move of this launch; look again later
                if ours and moved_size > offset:
                    data, skipped, _ = _read_new(
                        moved, offset, moved_size,
                        complete_lines_only=False)
                    parts.append((data, skipped))
                offset = 0
                self._offsets[provider_name] = 0
            self._file_ids[provider_name] = current_id
            if size > offset:
                try:
                    data, skipped, next_offset = _read_new(
                        path, offset, size, complete_lines_only=True)
                except OSError as exc:
                    # Keep the position; the .1 part read above is still
                    # logged (wh-provider-native-crash-trace.2.2).
                    logger.debug(f"Could not read {provider_name} stderr: {exc}")
                    active_unread = True
                else:
                    self._offsets[provider_name] = next_offset
                    if data or skipped:
                        parts.append((data, skipped))

        exit_code = None
        prefix = f"[{provider_name} stderr]"
        for data, skipped in parts:
            text = data.decode("utf-8", "backslashreplace")
            lines = [line.rstrip("\r") for line in text.split("\n")]
            for line in lines:
                match = _EXIT_LINE.match(line)
                if match:
                    exit_code = int(match.group(1))
            if skipped:
                logger.warning(
                    f"{prefix} <{skipped} bytes before these lines not copied>")
            for line in filter_lines(lines):
                logger.warning(f"{prefix} {line}")
        if active_unread:
            return None
        return exit_code


def _examine(path: Path) -> tuple[int, Optional[tuple[int, int]]]:
    """The file's size and identity from one os.stat call.

    Raises FileNotFoundError when the file is missing, and another
    OSError when it exists but cannot be examined now.
    """
    stat = os.stat(path)
    return stat.st_size, _file_id(stat)


def _file_id(stat: os.stat_result) -> Optional[tuple[int, int]]:
    """The file's (volume, file index), or None when the file system
    reports no index (st_ino 0)."""
    if not stat.st_ino:
        return None
    return (stat.st_dev, stat.st_ino)


def _read_new(path: Path, offset: int, size: int,
              complete_lines_only: bool) -> tuple[bytes, int, int]:
    """Read bytes [offset, size) of `path`, at most MAX_COPY_BYTES of them.

    Returns (data without its last newline, bytes skipped before it, the
    offset for the next read). The newest bytes are kept; a line cut by
    the cap is dropped whole and counted as skipped. With
    complete_lines_only, a last line with no end yet is left for the
    next read; a moved-aside file is final, so it is read to its end.
    """
    start = max(offset, size - MAX_COPY_BYTES)
    with open(path, "rb") as stream:
        stream.seek(start)
        data = stream.read(size - start)
    if complete_lines_only:
        end = data.rfind(b"\n")
        if end < 0:
            # No complete line yet; read it again next time.
            return b"", 0, offset
        next_offset = start + end + 1
        data = data[:end]
    else:
        next_offset = size
        if data.endswith(b"\n"):
            data = data[:-1]
    skipped = start - offset
    if skipped:
        # The cut can land inside a line; drop that fragment.
        first = data.find(b"\n")
        skipped += first + 1 if first >= 0 else len(data)
        data = data[first + 1:] if first >= 0 else b""
    return data, skipped, next_offset
