"""First-use discovery hint for the screen-reader-flag opt-in (wh-r3xy1).

A config-only opt-in is too hidden for a hands-free accessibility feature.
The first time a voice click command targets a window in the Chromium-family
process list while ``[click] enable_screen_reader_flag`` is false, WheelHouse
surfaces a one-shot info notice through the existing GUI notice queue,
pointing the user at the config opt-in. (See the v5 design doc,
``docs/plans/2026-05-21-voice-element-clicking-design-v5.md``, the
"First-use discovery hint" subsection and the "Write contract for the hint
file" subsection that follows it.)

This module is owned by the Logic process. It contains:

  * The exact user-visible hint wording (``HINT_TEXT``).
  * The eligibility check (``is_chromium_family``), which reuses
    ``DEFAULT_BROWSER_PROCESS_NAMES`` from ``shared.rejection_category`` --
    the list is imported, never redefined here.
  * A loader / atomic writer / deleter for the record file
    ``data/click_first_use_hint_shown.toml`` following the same atomic-write
    and concurrency pattern as ``utils.soft_allow_writer`` (single Logic-side
    writer, atomic temp + fsync + ``os.replace`` under a module-scoped lock).
  * ``FirstUseHintTracker``, the suppression state machine: the hint shows
    on each eligible click until it is recorded as shown, which happens on
    EITHER a dismiss click OR three subsequent same-process clicks.

Loader tolerance (v5 contract):
  * Missing file -> "not shown yet"; the next eligible click shows the hint.
  * Unparseable TOML -> "treat as shown"; a corrupted file degrades safely so
    the user does not see the hint repeatedly on every restart. The loader
    logs the parse error.

No new IPC channel is added. The hint rides as a ``click_first_use_hint``
action on the existing GUI state queue (the same queue carrying
``show_click_notice`` / ``show_rejection_toast``).
"""

from __future__ import annotations

import logging
import os
import tempfile
import enum
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import tomli_w

from shared.rejection_category import DEFAULT_BROWSER_PROCESS_NAMES

logger = logging.getLogger(__name__)


class HintDecision(enum.Enum):
    """Pure verdict from ``FirstUseHintTracker.evaluate`` (no state mutation).

    The decision and the state mutation are split (wh-9f3t.61.2) so the
    caller can forward the GUI action FIRST and commit the tracker state only
    after a successful enqueue -- a failed delivery then retries on the next
    eligible click instead of silently burning the one-shot display.
    """

    IGNORE = "ignore"
    """Not eligible (flag on, non-browser, or already recorded). Do nothing."""

    SHOW = "show"
    """First eligible click not yet displayed. Forward the notice, then call
    ``commit_displayed`` iff the forward succeeded."""

    COUNT = "count"
    """Already displayed and still eligible. Call ``note_counted`` to advance
    the persistence counter (no notice is shown for a COUNT click)."""


# Exact, verbatim user-visible wording from the v5 design doc. The config key
# renders with backtick-quoting as in the doc; the user-visible words are
# identical to the spec. Keep this string in sync with the v5 doc -- it is the
# single source of truth for the notice text.
HINT_TEXT = (
    "Wheelhouse can speed up clicks in this app by setting the Windows "
    "screen-reader flag. Tradeoff: PSReadLine will warn in every PowerShell "
    "session. See config.toml `[click] enable_screen_reader_flag` to opt in. "
    "Tap to dismiss."
)


# Number of subsequent same-process clicks (after the hint was first shown but
# not dismissed) that mark the hint as shown. From the v5 spec: "clicking the
# dismiss area or any of three subsequent same-process clicks marks the hint
# shown".
SUBSEQUENT_CLICK_THRESHOLD = 3


# Serialise the read-free write-and-delete of the record file. The lock is
# module-scoped because the file path is the same across all callers in
# production (the single Logic process); it is SHARED by the writer
# (``mark_hint_shown``) and the CLI delete (``delete_hint_record``) so a
# ``wheelhouse --reset-first-use-hints`` invocation cannot race a concurrent
# Logic-side mark. Tests that pass distinct paths still serialise; the only
# cost is reduced concurrency, not correctness.
_HINT_LOCK = threading.Lock()


def default_hint_path() -> Path:
    """Return the default record-file path.

    ``services/wheelhouse/data/click_first_use_hint_shown.toml`` -- the same
    ``data/`` directory as the soft-allow lifecycle files, for layout
    consistency.
    """
    return Path(__file__).resolve().parent / "data" / "click_first_use_hint_shown.toml"


def is_chromium_family(
    process_name: str,
    *,
    browser_process_names: Optional[Iterable[str]] = None,
) -> bool:
    """Return True iff ``process_name`` is in the Chromium-family list.

    Comparison is case-insensitive. The list is
    ``DEFAULT_BROWSER_PROCESS_NAMES`` imported from
    ``shared.rejection_category`` -- it is NOT redefined here. A caller may
    pass an explicit ``browser_process_names`` set (e.g. the ClickConfig
    resolved browser list) to match a config-extended view; passing None
    matches against the built-in list only.

    An empty / falsy ``process_name`` is never a member.
    """
    name = (process_name or "").lower()
    if not name:
        return False
    if browser_process_names is None:
        names = DEFAULT_BROWSER_PROCESS_NAMES
    else:
        names = frozenset(n.lower() for n in browser_process_names)
    return name in names


def load_hint_shown(path: Path) -> bool:
    """Return whether the hint has already been recorded as shown.

    Loader tolerance (v5 contract):
      * Missing file -> False ("not shown yet"); the next eligible click
        shows the hint.
      * Unparseable TOML -> True ("treat as shown"); a corrupted file
        degrades safely so the hint is not shown repeatedly. The parse error
        is logged.
      * Parseable file -> the boolean value of the top-level ``shown`` key
        (missing / non-bool ``shown`` is treated as not shown -- a
        well-formed file written by this module always carries ``shown =
        true``).

    Never raises.
    """
    try:
        if not path.exists():
            return False
    except OSError as exc:
        # A stat failure is not proof the file is absent; fail safe by
        # treating it as already shown so a transient FS error does not spam
        # the hint.
        logger.warning(
            "click_first_use_hint: could not stat %s: %s; treating as shown",
            path, exc,
        )
        return True

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "click_first_use_hint: record file %s is unreadable/unparseable "
            "(%s); treating the hint as already shown so it is not surfaced "
            "repeatedly. Delete the file or run "
            "`wheelhouse --reset-first-use-hints` to reset.",
            path, exc,
        )
        return True

    return bool(data.get("shown") is True)


def mark_hint_shown(path: Path) -> bool:
    """Atomically record that the hint has been shown.

    Writes ``shown = true`` (plus an ISO-8601 ``recorded_at`` timestamp for
    operator diagnostics) to ``path`` using the standard atomic-write idiom
    under the module-scoped lock:

      1. Create a temp file in the same directory as the target.
      2. Write the serialised TOML bytes.
      3. flush + ``os.fsync`` so the bytes are durable.
      4. ``os.replace(temp, target)`` -- atomic on Windows since Python 3.3.

    A crash mid-write leaves either the old content or no file at all, never
    a partial file. Returns True on success, False on any failure (the target
    is left untouched on failure). The parent directory is created if absent.
    """
    payload = tomli_w.dumps(
        {
            "shown": True,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    ).encode("utf-8")

    with _HINT_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "click_first_use_hint: could not create parent dir for %s: %s",
                path, exc,
            )
            return False

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
                "click_first_use_hint: failed to write %s: %s", path, exc,
            )
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False


def delete_hint_record(path: Path) -> bool:
    """Delete the record file so the hint can show again (CLI reset).

    Runs under the SAME module-scoped lock the writer uses, but that lock is
    PROCESS-LOCAL: the ``wheelhouse --reset-first-use-hints`` CLI runs in a
    separate launcher process from the Logic writer, each with its own
    ``_HINT_LOCK``, so the lock does NOT serialize a live reset against a
    concurrent Logic threshold-write (wh-9f3t.61.1). This reset is therefore a
    recovery shortcut intended to run while WheelHouse is NOT running; if run
    live, a concurrent first-use-hint write could re-create the record
    (harmless -- just re-run the reset once WheelHouse is stopped). Returns
    True on success -- including when the file is already absent (deleting
    "nothing" leaves the system in the desired state). Returns False only on a
    real deletion error.
    """
    with _HINT_LOCK:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            # Already absent -> the hint can already show; treat as success.
            return True
        except OSError as exc:
            logger.warning(
                "click_first_use_hint: failed to delete record file %s: %s",
                path, exc,
            )
            return False


class FirstUseHintTracker:
    """Suppression state machine for the first-use discovery hint.

    Owned by the Logic process and driven from a single thread (the asyncio
    event loop that serialises the click awaiter). NOT thread-safe; the
    record-file I/O it performs is serialised by the module-scoped lock, but
    the in-memory counters are single-threaded by construction.

    Lifecycle (delivery-gated, wh-9f3t.61.2):
      * On construction, the tracker loads the on-disk "shown" state once.
      * For each click the orchestrator calls ``evaluate(process, flag)`` --
        a PURE verdict with no mutation -- and acts on the result:
          - ``SHOW``  : forward the GUI notice, then ``commit_displayed`` ONLY
                        if the enqueue succeeded (a failed enqueue leaves the
                        tracker unmutated so the next eligible click retries).
          - ``COUNT`` : ``note_counted`` to advance the persistence counter.
          - ``IGNORE``: do nothing.
      * The notice DISPLAYS exactly once per session (one-shot): SHOW is only
        returned until ``commit_displayed`` runs; thereafter eligible clicks
        evaluate to COUNT.
      * Recording (durable "shown" on disk) happens on EITHER an explicit
        dismiss (``note_dismissed``) OR ``SUBSEQUENT_CLICK_THRESHOLD`` (3)
        COUNT clicks into ANY Chromium-family process after the confirmed
        display. Once recorded, the hint never displays again, across
        restarts. See ``note_counted`` for why the counter spans any browser
        rather than only the first-display process.
      * ``note_click`` is a convenience that combines evaluate + eager commit;
        it is used by direct-tracker tests, NOT the delivery-gated Logic hook.

    Eligibility for a click: the flag is OFF, the target process is in the
    Chromium-family list, and the hint has not been recorded as shown.
    """

    def __init__(
        self,
        path: Path,
        *,
        browser_process_names: Optional[Iterable[str]] = None,
    ) -> None:
        self._path = path
        self._browser_process_names = browser_process_names
        # In-memory mirror of the on-disk record; loaded once at construction.
        self._shown: bool = load_hint_shown(path)
        # Whether the notice has already been DISPLAYED this session. The hint
        # is one-shot per the v5 spec: it shows on exactly the first eligible
        # click and never re-displays, even before the durable "shown" record
        # is written.
        self._displayed: bool = False
        # Count of subsequent eligible clicks AFTER the one display. Counts
        # clicks into ANY Chromium-family process (see note_click for why this
        # deliberately departs from the spec's literal "same-process" wording).
        self._subsequent_clicks: int = 0

    @property
    def recorded_shown(self) -> bool:
        """Whether the hint is recorded as shown (in-memory mirror)."""
        return self._shown

    def evaluate(self, process_name: str, *, flag_enabled: bool) -> HintDecision:
        """Pure verdict for one click; performs NO state mutation (wh-9f3t.61.2).

        Args:
            process_name: exe basename of the target/foreground process.
            flag_enabled: the current ``[click] enable_screen_reader_flag``.

        Returns:
            * ``HintDecision.IGNORE`` -- flag on, non-Chromium-family process,
              or the hint is already recorded as shown. Do nothing.
            * ``HintDecision.SHOW`` -- first eligible click and the notice has
              not been displayed yet. The caller should forward the GUI action
              FIRST and call ``commit_displayed`` only if the enqueue
              succeeded; on a failed enqueue the caller leaves the tracker
              unmutated so the next eligible click retries the display.
            * ``HintDecision.COUNT`` -- already displayed and still eligible.
              The caller should call ``note_counted`` to advance the
              persistence counter; no notice is shown for a COUNT click.

        Splitting the decision from the mutation closes the wh-9f3t.61.2 gap:
        the previous ``note_click`` committed ``_displayed`` (and advanced the
        counter) BEFORE Logic knew the GUI action was actually enqueued, so a
        dropped/failed first enqueue burned the one-shot display and could
        later record "shown" to disk for a hint the user never saw.
        """
        if self._shown:
            return HintDecision.IGNORE
        if flag_enabled:
            return HintDecision.IGNORE
        if not is_chromium_family(
            process_name, browser_process_names=self._browser_process_names
        ):
            return HintDecision.IGNORE
        if not self._displayed:
            return HintDecision.SHOW
        return HintDecision.COUNT

    def commit_displayed(self) -> None:
        """Mark the one-shot notice as displayed (wh-9f3t.61.2).

        Called by the orchestrator ONLY after the GUI action was successfully
        enqueued, so a failed delivery does not burn the one-shot display.
        Idempotent; resets the subsequent-click counter so counting starts
        cleanly after the confirmed display.
        """
        if not self._displayed:
            self._displayed = True
            self._subsequent_clicks = 0

    def note_counted(self) -> bool:
        """Advance the subsequent-eligible-click counter (wh-9f3t.61.2).

        Called for a ``HintDecision.COUNT`` click (already displayed, still
        eligible). When the counter reaches ``SUBSEQUENT_CLICK_THRESHOLD`` (3)
        the hint is recorded as shown (persisted to disk) so it never re-appears
        across restarts. Returns True iff this call recorded "shown".

        Deliberate departure from the spec's literal "three subsequent
        same-process clicks": the counter advances for clicks into ANY
        Chromium-family process, not only the process of the first display. The
        literal same-process rule is unreachable for a user who alternates
        browsers (chrome, edge, brave, ...), which would leave the "shown"
        record never written and -- combined with one-shot display -- silently
        keep the hint un-persisted forever. Counting any eligible click makes
        the persistence threshold reachable for multi-browser users while
        preserving the once-per-session display guarantee.
        """
        self._subsequent_clicks += 1
        if self._subsequent_clicks >= SUBSEQUENT_CLICK_THRESHOLD:
            # Propagate the writer's result so the return contract is honest:
            # True only when the durable record was actually written. On a
            # disk-write failure _record_shown still sets the in-memory
            # _shown flag (session degrades safely, matching the soft-allow
            # write-failure posture) but returns False here (wh-9f3t.62.1).
            return self._record_shown()
        return False

    def note_click(self, process_name: str, *, flag_enabled: bool) -> bool:
        """Convenience: evaluate + commit in one call; return True iff show.

        This is the eager path that commits the display unconditionally on a
        SHOW verdict. It is retained for direct-tracker unit tests and any
        caller that does not gate the commit on delivery. The Logic hook does
        NOT use this -- it uses ``evaluate`` + delivery-gated ``commit_displayed``
        / ``note_counted`` so a failed GUI enqueue retries on the next click
        (wh-9f3t.61.2).
        """
        decision = self.evaluate(process_name, flag_enabled=flag_enabled)
        if decision is HintDecision.SHOW:
            self.commit_displayed()
            return True
        if decision is HintDecision.COUNT:
            self.note_counted()
        return False

    def note_dismissed(self) -> bool:
        """Record the hint as shown because the user explicitly dismissed it.

        NOTE: the current production notice is a ``plyer`` OS toast surfaced
        from ``gui.py``; that toast closes on tap but provides NO clickable
        dismiss CALLBACK, so nothing calls this method in production today.
        Suppression therefore relies on the one-shot display plus the
        subsequent-eligible-click persistence in ``note_click``. This method
        is kept as the correct recording hook for a future explicit-dismiss
        affordance (e.g. an in-app toast with a Dismiss button), which would
        call ``note_dismissed`` to persist "shown" immediately.

        Returns True if the disk write succeeded (or the hint was already
        recorded), False on a disk-write failure. On a write failure the
        in-memory mirror is still set so the hint does not re-surface within
        the running session; a restart re-reads disk and the hint may show
        again (degrade-safe, matches the soft-allow write-failure posture).
        """
        return self._record_shown()

    def _record_shown(self) -> bool:
        """Set the in-memory mirror and persist ``shown = true`` to disk."""
        self._shown = True
        ok = mark_hint_shown(self._path)
        if not ok:
            logger.warning(
                "click_first_use_hint: failed to persist 'shown' to %s; the "
                "hint is suppressed for this session but may re-appear after "
                "a restart.",
                self._path,
            )
        return ok


__all__ = [
    "HINT_TEXT",
    "SUBSEQUENT_CLICK_THRESHOLD",
    "FirstUseHintTracker",
    "HintDecision",
    "default_hint_path",
    "delete_hint_record",
    "is_chromium_family",
    "load_hint_shown",
    "mark_hint_shown",
]
