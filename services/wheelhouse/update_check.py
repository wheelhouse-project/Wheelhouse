"""Background check for a newer Wheelhouse release (wh-release-update-check).

Stage A: the logic only, with no GUI. The async runner takes a callback and
calls it when a notice must appear; a later stage wires that callback to the
interface.

Privacy contract: the one request is a plain GET of the public releases
address. It carries no config value, identifier, speech, or text. GitHub
receives the IP address and a User-Agent naming Wheelhouse and its version.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import tomli_w

from utils.app_version import get_app_version

logger = logging.getLogger(__name__)

# Config key that turns the check off. A missing key behaves as on.
UPDATE_CHECK_SETTING = "CHECK_FOR_UPDATES"
UPDATE_CHECK_DEFAULT = True

# crewcut: the repository address is a constant on purpose. No config key
# may redirect it, so an edited config cannot point the check at another
# host. To allow a different repository later, add a validated config key
# here and name it in config_descriptions.toml.
RELEASES_URL = (
    "https://api.github.com/repos/wheelhouse-project/Wheelhouse/releases/latest"
)
USER_AGENT_PREFIX = "Wheelhouse/"

REQUEST_TIMEOUT_SECONDS = 15
# Largest answer read from GitHub; a larger answer counts as unreadable.
MAX_RESPONSE_BYTES = 1024 * 1024

CHECK_INTERVAL_SECONDS = 24 * 60 * 60
FIRST_CHECK_DELAY_SECONDS = 60
NOTICE_DEFER_SECONDS = 7 * 24 * 60 * 60

STATE_FILE_NAME = "update_check_state.toml"

# Version strings carry at most this many numeric parts ("1.2.3.4").
MAX_VERSION_PARTS = 4

_STATE_LOCK = threading.Lock()


def state_file_path() -> Path:
    """Return the record file path, resolved through ``get_user_data_dir``.

    Every call site that reads or writes user state resolves the directory
    through that helper (wh-k8ef): a frozen build keeps it under the app-data
    root, where a path built from ``__file__`` would sit in a directory wiped
    on exit. A function, not a constant, because the answer depends on the
    running build. A source checkout gives
    ``services/wheelhouse/data/update_check_state.toml``.
    """
    from utils.system import get_user_data_dir

    return get_user_data_dir() / STATE_FILE_NAME


class UpdateCheckError(Exception):
    """The release answer could not be fetched or read.

    update_install.py raises the same error for the download itself: an
    empty download, a length that does not match the size in the release
    answer, a digest that does not match, and a file that Wheelhouse could
    not save (update_install.py:250-270).
    """


def parse_version(text: object) -> tuple[int, ...] | None:
    """Return the numeric parts of ``text``, or None when it is malformed.

    One leading "v" is stripped. One to ``MAX_VERSION_PARTS`` parts are read;
    each must be ASCII digits only. Anything else is malformed.
    """
    if not isinstance(text, str):
        return None
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if not 1 <= len(parts) <= MAX_VERSION_PARTS:
        return None
    numbers = []
    for part in parts:
        if not (part.isascii() and part.isdigit()):
            return None
        numbers.append(int(part))
    return tuple(numbers)


def is_newer_version(candidate: object, running: object) -> bool:
    """True only when both versions parse and ``candidate`` is newer."""
    new = parse_version(candidate)
    old = parse_version(running)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    new += (0,) * (width - len(new))
    old += (0,) * (width - len(old))
    return new > old


# --- record file ---------------------------------------------------------


@dataclass(frozen=True)
class UpdateCheckState:
    """What the record file holds.

    ``last_check`` is the time of the last request attempt (epoch seconds).
    ``notified_version`` and ``notified_at`` name the version whose notice was
    last SHOWN and when. "Later", the close button, and no answer all leave
    the same record, so all three start the same wait.
    """

    last_check: float = 0.0
    notified_version: str = ""
    notified_at: float = 0.0


def _as_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def load_state(path: Path | None = None) -> UpdateCheckState:
    """Read the record file; a missing or unreadable file gives empty state."""
    if path is None:
        path = state_file_path()
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return UpdateCheckState()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning("update_check: could not read %s: %s", path, exc)
        return UpdateCheckState()
    version = data.get("notified_version", "")
    return UpdateCheckState(
        last_check=_as_number(data.get("last_check")),
        notified_version=version if isinstance(version, str) else "",
        notified_at=_as_number(data.get("notified_at")),
    )


def save_state(path: Path, state: UpdateCheckState) -> bool:
    """Write the record file atomically. Returns False on any failure."""
    with _STATE_LOCK:
        temp_path: str | None = None
        try:
            payload = tomli_w.dumps(
                {
                    "last_check": state.last_check,
                    "notified_version": state.notified_version,
                    "notified_at": state.notified_at,
                }
            ).encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
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
        except Exception as exc:
            logger.warning("update_check: could not write %s: %s", path, exc)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False


# --- schedule ------------------------------------------------------------


def seconds_until_first_check(last_check: float, now: float) -> float:
    """Wait before the first check after startup.

    Never less than ``FIRST_CHECK_DELAY_SECONDS``. When the last attempt was
    less than 24 hours ago, wait until the 24-hour mark. Never more than
    24 hours, so a clock set back cannot postpone the check for days.
    """
    if last_check <= 0:
        return FIRST_CHECK_DELAY_SECONDS  # never checked
    until_due = last_check + CHECK_INTERVAL_SECONDS - now
    return min(CHECK_INTERVAL_SECONDS, max(FIRST_CHECK_DELAY_SECONDS, until_due))


def notice_is_due(state: UpdateCheckState, latest_version: str, now: float) -> bool:
    """True when the notice for ``latest_version`` may be shown now.

    A version newer than the last-shown one shows at once. The same or an
    older version shows again only after ``NOTICE_DEFER_SECONDS``. The wait
    holds only while ``notified_at`` lies in the past by less than that
    period: a recorded time in the future, or one that is not a finite number
    (a hand-edited record file can hold ``nan`` or ``inf``), ends the wait.
    """
    if is_newer_version(latest_version, state.notified_version):
        return True
    if parse_version(state.notified_version) is None:
        return True
    # Every comparison with nan is False, so a nan time also gives True.
    return not (0 <= now - state.notified_at < NOTICE_DEFER_SECONDS)


# --- the request ---------------------------------------------------------


def http_get(url: str, running_version: str) -> bytes:
    """Return the body of one GET, at most ``MAX_RESPONSE_BYTES`` long.

    Sends one GET carrying only a User-Agent that names Wheelhouse and its
    version. Raises ``UpdateCheckError`` on a network error or on an answer
    over the limit. ``update_install`` imports this function, so the update
    check and the installer download make the same request.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT_PREFIX + running_version},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise UpdateCheckError(f"request failed: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise UpdateCheckError("answer is larger than the limit")
    return body


def fetch_latest_tag(running_version: str) -> str:
    """Return ``tag_name`` of the latest public release.

    Sends one GET carrying only a User-Agent that names Wheelhouse and its
    version. Raises ``UpdateCheckError`` on any network error, on an answer
    that is not readable JSON, and on an answer that holds no ``tag_name``.
    One answer escapes that rule: ``json.loads`` raises ``RecursionError``
    on a deeply nested answer, the arm below does not name it, and it
    reaches ``run_update_check``, which logs it.
    """
    body = http_get(RELEASES_URL, running_version)
    # crewcut: this arm does not name RecursionError, so a deeply nested
    # answer raises RecursionError out of this function in place of
    # UpdateCheckError. update_install.py:221 shows the way to remove the
    # gap: name RecursionError in the arm as well.
    try:
        tag = json.loads(body.decode("utf-8")).get("tag_name")
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise UpdateCheckError(f"answer is not readable: {exc}") from exc
    if not isinstance(tag, str) or not tag:
        raise UpdateCheckError("answer has no tag_name")
    return tag


# --- one check and the background runner ---------------------------------


def check_for_update(
    config_service: Any,
    *,
    state_path: Path | None = None,
    get_version: Callable[[], str] = get_app_version,
    fetch: Callable[[str], str] = fetch_latest_tag,
    clock: Callable[[], float] = time.time,
) -> tuple[str, str] | None:
    """Run one check. Blocking: call it from a worker thread.

    Returns ``(version, tag)`` when a notice must appear, else None. With the
    config key off no request is made. A failed request logs one line and
    returns None; the attempt still counts, so the next try is the next
    24-hour check. When the last_check record cannot be written, no request
    is made, one line is logged, and None is returned.
    """
    if not config_service.get(UPDATE_CHECK_SETTING, UPDATE_CHECK_DEFAULT):
        return None
    if state_path is None:
        state_path = state_file_path()
    running = get_version()
    if parse_version(running) is None:
        logger.info("update_check: skipped, running version is %r", running)
        return None
    now = clock()
    # Fail closed: the last_check record is the only thing that holds the
    # 24-hour limit across restarts. Without it a restart would ask GitHub
    # again after the 60-second startup delay, so make no request and try
    # again at the next scheduled check.
    if not save_state(state_path, replace(load_state(state_path), last_check=now)):
        logger.warning(
            "update_check: could not record the check time, so no request "
            "was made; will retry at the next check"
        )
        return None
    try:
        tag = fetch(running)
    except UpdateCheckError as exc:
        logger.warning("update_check: no result, will retry at the next check: %s", exc)
        return None
    if not is_newer_version(tag, running):
        return None
    if not notice_is_due(load_state(state_path), tag, now):
        return None
    return (tag[1:] if tag.startswith("v") else tag, tag)


def record_notice_shown(path: Path, version: str, now: float) -> bool:
    """Record the version and the time its notice was shown.

    The Logic process calls this when the GUI process reports that the
    notice is on the screen (``update_notice_shown``, handled in main.py
    by LogicController._handle_update_notice_shown). ``run_update_check``
    does not call it: a notice that waits for a place on the screen has not
    been shown, and a record written for it would hide the version for
    ``NOTICE_DEFER_SECONDS`` (wh-blocked-mic-message.1.2).

    "Later", the close button, and no answer all start the same wait.
    """
    return save_state(
        path, replace(load_state(path), notified_version=version, notified_at=now)
    )


async def run_update_check(
    config_service: Any,
    notify: Callable[[str, str], Any],
    *,
    state_path: Path | None = None,
    get_version: Callable[[], str] = get_app_version,
    fetch: Callable[[str], str] = fetch_latest_tag,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Check in the background, at most once per 24 hours, until cancelled.

    ``notify(version, tag)`` is called (sync or async, on the event loop)
    when a notice must appear. Every exception is caught and logged; only
    cancellation ends the loop. The request runs in a worker thread, so it
    never delays the event loop.

    This function writes no shown-notice record. ``notify`` only hands the
    notice to the GUI process, which may keep it waiting for a place on the
    screen. The GUI process reports the moment the notice becomes visible,
    and the Logic process records the version and the time then
    (``record_notice_shown``, wh-blocked-mic-message.1.2). Until that
    report arrives the version has no record, so ``check_for_update``
    returns it again at the next check, at most ``CHECK_INTERVAL_SECONDS``
    (24 hours) after the last check, and ``notify`` is called again. If
    ``notify`` raises, the exception is logged and the notice is tried again
    at the next check.

    wh-blocked-mic-message.1.2 removed the record write that used to run
    here, before ``notify``, and with it everything that existed only to undo
    that write: the read of the previous record, both calls of
    ``_restore_notice_fields``, the one-worker record pool that ordered the
    restore after the write, the ``BaseException`` arm that restored the
    record on a shutdown, and the crewcut comment about the event loop that
    waited on the pool. With no write there is nothing to put back. Kept:
    the ``last_check`` write in ``check_for_update``, which holds the
    24-hour limit across restarts and is not part of the notice record, and
    ``record_notice_shown``, which the Logic process calls on the report.
    """
    if state_path is None:
        state_path = state_file_path()
    try:
        first_state = await asyncio.to_thread(load_state, state_path)
        delay = seconds_until_first_check(first_state.last_check, clock())
    except Exception as exc:
        logger.warning("update_check: could not schedule from the record: %s", exc)
        delay = FIRST_CHECK_DELAY_SECONDS
    while True:
        await sleep(delay)
        delay = CHECK_INTERVAL_SECONDS
        try:
            found = await asyncio.to_thread(
                check_for_update,
                config_service,
                state_path=state_path,
                get_version=get_version,
                fetch=fetch,
                clock=clock,
            )
            if found is not None:
                outcome = notify(*found)
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception as exc:
            logger.warning("update_check: check failed: %s", exc)
