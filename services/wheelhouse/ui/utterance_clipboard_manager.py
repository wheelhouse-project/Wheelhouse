"""Utterance-level clipboard lifecycle management.

This module manages clipboard save/restore for the duration of speech utterances.
The clipboard is saved once at utterance start and restored at utterance end,
with safety timeouts to prevent clipboard corruption if end signal never arrives.

wh-d0lr1: end_utterance no longer restores the clipboard synchronously.
Instead it schedules a ``PendingRestore`` whose timer compares the current
clipboard sequence number against the WheelHouse-write sequence at restore
time. If the sequence advanced between our last paste and the deferred fire
(meaning the user manually copied something), the saved clipboard is NOT
restored -- the user's manual copy survives.

The deferred-restore design also closes the existing race where the
destination application reads the WheelHouse-pasted clipboard ASYNCHRONOUSLY
(SendInput returns before the application's WM_PASTE handler reads). The
synchronous restore was firing before the application's paste handler ran,
so the application sometimes pasted the restored (original) clipboard
contents instead of the dictated text. Deferring the restore by ~300 ms
gives the destination application time to consume the dictated clipboard
before the original returns.
"""
import logging

from utils.redact import redact_transcript
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import pyperclip

from ui import clipboard_sequence

logger = logging.getLogger(__name__)


@dataclass
class PendingRestore:
    """A deferred clipboard restoration scheduled at the end of a dictation utterance.

    Lives on UtteranceClipboardManager._pending_restore. Bound to a specific
    threading.Timer via closure capture so a stale timer callback that fires
    after start_utterance replaced the pending restore detects the identity
    mismatch and refuses to act (wh-9pye.1).
    """

    saved_text: str
    scheduled_restore_time: float
    clipboard_seq_at_paste: int
    timer: Optional[threading.Timer] = None
    cancelled: bool = False
    utterance_id: Optional[int] = field(default=None)


class UtteranceClipboardManager:
    """Manages clipboard state for utterance lifecycle.

    :flow: Utterance Clipboard Lifecycle Management
    :description: Saves clipboard at utterance start, schedules deferred restore at
                  utterance end. The deferral lets the destination application
                  consume the dictated clipboard before the original clipboard
                  returns, and the ownership check (Win32 clipboard sequence
                  number) preserves any manual copy the user made between our
                  last paste and the deferred fire.
    :produces_for: UI Action Execution
    :consumes_from: STT Transcription

    Key features:
    - One clipboard save per utterance, regardless of how many words inserted.
    - Deferred restore (default 300 ms) lets the destination app finish pasting
      before the original clipboard returns.
    - Ownership check via Win32 GetClipboardSequenceNumber. If the user copies
      something between our last paste and the deferred fire (or at any point
      after our last write), the restore is skipped and the user's clipboard
      survives.
    - Chained utterances: a new utterance arriving within ``chain_gap_s`` of
      the previous utterance's end keeps the previous utterance's saved
      clipboard as the chained baseline (do NOT re-save fresh, which would
      capture WheelHouse's transient dictated text).
    - Safety timeout (default 1 s since utterance start) flows through the
      same end_utterance path and inherits the deferred-restore behavior.

    Locking discipline (wh-9pye.3):
    Every method that mutates ``_pending_restore``, ``_saved_text``,
    ``_in_utterance``, ``_utterance_id``, ``_clipboard_dirty``, or
    ``_last_wheelhouse_seq`` must hold ``self._lock``. Internal helpers that
    skip the acquire (because the caller already holds it) are suffixed
    ``_locked`` and document the precondition. The lock is non-reentrant
    (``threading.Lock``); helpers that need to call each other while the lock
    is held must use the ``_locked`` variants to avoid deadlock.
    """

    def __init__(
        self,
        timeout_seconds: float = 1.0,
        restore_deferral_s: float = 0.300,
        chain_gap_s: float = 0.500,
    ):
        """Initialize the utterance clipboard manager.

        Args:
            timeout_seconds: Safety timeout for automatic end_utterance if
                ``utterance_end`` never arrives. Measured from start_utterance.
            restore_deferral_s: How long after end_utterance to defer the
                clipboard restoration. Default 300 ms balances destination-app
                paste consumption (typically a few tens of ms) against user
                perception of clipboard latency.
            chain_gap_s: Maximum time after a previous utterance ended within
                which a new utterance is treated as chained (the previous
                saved clipboard is reused as the new utterance's baseline
                instead of capturing the in-flight dictated text). Default
                500 ms covers natural pauses between dictated phrases.
        """
        self._in_utterance = False
        self._utterance_id: Optional[int] = None
        self._saved_text: Optional[str] = None
        self._timeout_task: Optional[threading.Timer] = None
        self.timeout_seconds = timeout_seconds
        self.restore_deferral_s = restore_deferral_s
        self.chain_gap_s = chain_gap_s
        self._skip_restore = False
        self._lock = threading.Lock()
        self._clipboard_dirty = False
        self._last_paste_time: float = 0.0
        self._accumulated_text: str = ""
        # wh-d0lr1: the scheduled deferred restore, if any. Lives across the
        # gap between end_utterance and either timer-fire or the next
        # start_utterance (which may chain by reusing self._pending_restore.saved_text).
        self._pending_restore: Optional[PendingRestore] = None
        # wh-9pye.2: track the clipboard sequence number captured at the
        # moment of every WheelHouse-side clipboard write. Updated by
        # mark_clipboard_dirty (called by the handler after every WheelHouse
        # paste/copy). end_utterance uses this as the ownership baseline:
        # if get_sequence_number() > _last_wheelhouse_seq when end_utterance
        # runs, the user copied something between our last paste and end_utterance,
        # so no PendingRestore is scheduled and the user's clipboard is preserved.
        self._last_wheelhouse_seq: Optional[int] = None
        # wh-d0lr1: time of the previous utterance's end, for chain-gap
        # detection. Updated under lock at the end of every end_utterance call
        # (success or skip path).
        self._last_utterance_end_time: float = 0.0

        logger.debug(
            "UtteranceClipboardManager initialized "
            f"(timeout={timeout_seconds}s, deferral={restore_deferral_s}s, "
            f"chain_gap={chain_gap_s}s)"
        )

    # ========================================================================
    # PUBLIC API - LIFECYCLE
    # ========================================================================

    def start_utterance(self, utterance_id: int) -> None:
        """Save clipboard once at utterance start.

        :flow: Utterance Clipboard Lifecycle Management
        :step: 1
        :description: Called when new utterance begins. Saves the clipboard
                      once for restoration at utterance end. If a previous
                      utterance scheduled a PendingRestore that is still
                      pending and the chain gap has not elapsed, the
                      previous saved clipboard is reused as the new
                      utterance's baseline (chained-utterance policy).

        Args:
            utterance_id: The ID of the utterance starting.
        """
        with self._lock:
            # Active-overlap path: a prior utterance never received its end signal.
            # Force-cleanup through the same locked end path so PendingRestore
            # scheduling, _last_utterance_end_time, and skip/dirty handling all
            # behave identically to a normal end (wh-9pye.3).
            if self._in_utterance:
                logger.warning(
                    f"start_utterance({utterance_id}) while already in utterance "
                    f"{self._utterance_id}; force-ending the previous utterance."
                )
                self._end_utterance_locked(self._utterance_id)

            # Decide what saved_text the new utterance should rely on.
            chained_text: Optional[str] = None
            if self._pending_restore is not None:
                pending = self._pending_restore
                elapsed_since_end = time.monotonic() - self._last_utterance_end_time
                if elapsed_since_end < self.chain_gap_s:
                    # Within chain gap. Before chaining, verify the clipboard
                    # is still WheelHouse-owned (no manual user copy during
                    # the pause). If the user copied between the prior
                    # utterance ending and this start, the chain would
                    # silently lose the user's manual copy when the new
                    # utterance writes the clipboard. Apply the ownership-
                    # aware restore decision (which will skip restoring
                    # because seq advanced) and fall through to fresh save
                    # so the user's manual copy becomes the new baseline.
                    try:
                        current_seq = clipboard_sequence.get_sequence_number()
                    except Exception as e:
                        logger.warning(
                            f"[UTT-{utterance_id}] Could not read clipboard "
                            f"seq for chain ownership check: {e}; falling "
                            f"back to chain (best effort)"
                        )
                        current_seq = pending.clipboard_seq_at_paste

                    if current_seq != pending.clipboard_seq_at_paste:
                        logger.info(
                            f"[UTT-{utterance_id}] External clipboard write "
                            f"during chain gap (seq "
                            f"{pending.clipboard_seq_at_paste} -> {current_seq}); "
                            f"breaking chain, preserving user's clipboard, "
                            f"saving fresh baseline"
                        )
                        self._execute_restore_decision_locked(pending)
                        self._pending_restore = None
                    else:
                        # Clean chain: clipboard is still WheelHouse-owned.
                        # Reuse the prior pending baseline. The dictated
                        # clipboard from the prior utterance may still be on
                        # the system clipboard at this moment, so a fresh
                        # pyperclip.paste() would capture WheelHouse's
                        # transient text instead of the original baseline.
                        pending.cancelled = True
                        if pending.timer is not None:
                            pending.timer.cancel()
                        chained_text = pending.saved_text
                        logger.debug(
                            f"[UTT-{utterance_id}] Chaining from prior "
                            f"PendingRestore (prior UTT-{pending.utterance_id}, "
                            f"elapsed={elapsed_since_end * 1000:.0f}ms, "
                            f"saved_text_len="
                            f"{len(chained_text) if chained_text else 0})"
                        )
                        self._pending_restore = None
                else:
                    # Expired pending: chain gap elapsed, but the timer has not
                    # fired (or was delayed). Apply the ownership-aware restore
                    # decision now, then proceed with a fresh save (wh-9pye.4).
                    self._execute_restore_decision_locked(pending)
                    self._pending_restore = None

            self._utterance_id = utterance_id
            self._in_utterance = True
            self._clipboard_dirty = False
            self._accumulated_text = ""
            self._last_wheelhouse_seq = None

            if chained_text is not None:
                self._saved_text = chained_text
            else:
                try:
                    self._saved_text = pyperclip.paste()
                    logger.debug(
                        f"[UTT-{utterance_id}] Saved clipboard fresh "
                        f"(len={len(self._saved_text) if self._saved_text else 0})"
                    )
                except Exception as e:
                    logger.warning(
                        f"[UTT-{utterance_id}] Could not read clipboard at start: {e}"
                    )
                    self._saved_text = None

            self._start_safety_timeout(utterance_id)

    def end_utterance(self, utterance_id: Optional[int] = None) -> None:
        """Schedule a deferred clipboard restore at utterance end.

        :flow: Utterance Clipboard Lifecycle Management
        :step: 2
        :description: Called when an utterance completes (utterance_end signal
                      from STT) or when the safety timeout fires. Schedules a
                      PendingRestore whose timer compares the clipboard
                      sequence number at fire-time against the WheelHouse-write
                      baseline. If the sequence advanced (user copied
                      something), the restore is skipped and the user's
                      clipboard survives. If skip_restore is set, no restore
                      is scheduled (copy/cut commands).

        Args:
            utterance_id: The ID of the utterance ending. None matches any
                active utterance.
        """
        caller = (
            "timer"
            if threading.current_thread() is not threading.main_thread()
            else "main"
        )
        logger.debug(f"end_utterance({utterance_id}) acquiring lock (caller={caller})")
        acquired = self._lock.acquire(timeout=5.0)
        if not acquired:
            logger.error(
                f"end_utterance({utterance_id}) FAILED to acquire lock after 5s "
                f"(caller={caller}) -- possible deadlock"
            )
            return

        try:
            self._end_utterance_locked(utterance_id)
        finally:
            self._lock.release()
            logger.debug(
                f"end_utterance({utterance_id}) lock released (caller={caller})"
            )

    def _end_utterance_locked(self, utterance_id: Optional[int]) -> None:
        """End the active utterance. Caller MUST hold ``self._lock``.

        Used by both the public end_utterance (which acquires the lock) and
        start_utterance (which holds the lock for the active-overlap path).
        Sharing the body avoids both deadlock and divergence between the
        two callers (wh-9pye.3).
        """
        if not self._in_utterance:
            logger.debug(f"end_utterance({utterance_id}) but not in utterance")
            return

        if utterance_id is not None and self._utterance_id is not None:
            if utterance_id != self._utterance_id:
                logger.warning(
                    f"end_utterance({utterance_id}) ignored - current utterance "
                    f"is {self._utterance_id}"
                )
                return

        self._cancel_timeout()

        if self._skip_restore:
            logger.debug(
                f"[UTT-{self._utterance_id}] Skipping clipboard restore "
                f"(copy/cut command)"
            )
            self._reset_utterance_state_locked()
            self._skip_restore = False
            return

        if not self._clipboard_dirty or self._saved_text is None:
            logger.debug(
                f"[UTT-{self._utterance_id}] Ending utterance, clipboard "
                f"not dirtied or no saved text -- no restore scheduled"
            )
            self._reset_utterance_state_locked()
            return

        # Ownership check at end_utterance time (wh-9pye.2). If the user
        # copied something between our last WheelHouse write and now,
        # _last_wheelhouse_seq will not equal the current sequence number
        # and the user's manual copy must be preserved -- skip scheduling
        # entirely. _last_wheelhouse_seq can be None if mark_clipboard_dirty
        # was called via a path that did not capture seq; in that case fall
        # back to the current sequence (no protection) and proceed.
        try:
            current_seq = clipboard_sequence.get_sequence_number()
        except Exception as e:
            logger.warning(
                f"[UTT-{self._utterance_id}] Could not read clipboard sequence "
                f"at end_utterance: {e}; skipping deferred restore (fail safe)"
            )
            self._reset_utterance_state_locked()
            return

        baseline_seq: int
        if self._last_wheelhouse_seq is None:
            baseline_seq = current_seq
            logger.debug(
                f"[UTT-{self._utterance_id}] No Wheelhouse write seq tracked; "
                f"using current seq {current_seq} as PendingRestore baseline"
            )
        elif current_seq != self._last_wheelhouse_seq:
            logger.info(
                f"[UTT-{self._utterance_id}] External clipboard write between "
                f"last Wheelhouse write and end_utterance "
                f"(wheelhouse_seq={self._last_wheelhouse_seq}, "
                f"current_seq={current_seq}); preserving user's clipboard, "
                f"NOT scheduling restore"
            )
            self._reset_utterance_state_locked()
            return
        else:
            baseline_seq = self._last_wheelhouse_seq

        saved_text = self._saved_text
        ending_utterance_id = self._utterance_id

        scheduled_time = time.monotonic() + self.restore_deferral_s
        pending = PendingRestore(
            saved_text=saved_text,
            scheduled_restore_time=scheduled_time,
            clipboard_seq_at_paste=baseline_seq,
            cancelled=False,
            utterance_id=ending_utterance_id,
        )
        # Bind the timer callback to this specific PendingRestore via closure
        # capture so a stale timer fires against its own pending object,
        # not the manager's current one (wh-9pye.1).
        timer = threading.Timer(
            self.restore_deferral_s, lambda p=pending: self._do_restore(p)
        )
        timer.daemon = True
        pending.timer = timer
        self._pending_restore = pending
        timer.start()

        logger.debug(
            f"[UTT-{ending_utterance_id}] Scheduled deferred clipboard restore "
            f"(deferral={self.restore_deferral_s * 1000:.0f}ms, "
            f"baseline_seq={baseline_seq})"
        )

        self._reset_utterance_state_locked()

    def _reset_utterance_state_locked(self) -> None:
        """Clear per-utterance state. Caller MUST hold ``self._lock``.

        Does NOT clear self._pending_restore -- that lives across the gap
        between end_utterance and the next start_utterance (chained baseline)
        or until the timer fires.
        """
        self._in_utterance = False
        self._saved_text = None
        self._utterance_id = None
        self._clipboard_dirty = False
        self._accumulated_text = ""
        self._last_wheelhouse_seq = None
        self._last_utterance_end_time = time.monotonic()

    # ========================================================================
    # PUBLIC API - QUERIES AND FLAGS
    # ========================================================================

    def is_in_utterance(self) -> bool:
        """Return True if currently between start_utterance and end_utterance."""
        return self._in_utterance

    def get_current_utterance_id(self) -> Optional[int]:
        """Return the currently active utterance ID, if any."""
        return self._utterance_id

    def skip_clipboard_restore(self) -> None:
        """Mark that clipboard should not be restored at utterance end.

        Used for commands that intentionally modify the clipboard (copy, cut).
        The flag is automatically cleared after command execution by the
        command engine's finally block (clear_skip_flag).
        """
        self._skip_restore = True
        logger.debug("Clipboard restoration will be skipped for this utterance")

    def clear_skip_flag(self) -> None:
        """Clear the skip restoration flag.

        Called automatically by command_engine after every command completes
        to prevent state leakage between commands.
        """
        self._skip_restore = False

    def mark_clipboard_dirty(self, write_seq: Optional[int] = None) -> None:
        """Record that the system clipboard was written during this utterance.

        wh-4z4g9: callers (UIActionHandler) invoke this after any operation
        that wrote the system clipboard. end_utterance only schedules the
        deferred restore when this flag is True, so a Unicode-only or
        terminal-only utterance leaves the user's clipboard alone.

        wh-fz7j.1: this method now acquires self._lock to serialize against
        the safety-timeout thread's end_utterance call. Without the lock,
        a timeout firing in the gap between strategy.insert returning and
        the dirty mark would observe _clipboard_dirty=False, schedule no
        restore, and leak the dictated clipboard.

        wh-fz7j.2 / wh-fz7j.3: optional write_seq parameter. When provided,
        the caller is recording the seq captured immediately after a
        WheelHouse-side clipboard write -- update _last_wheelhouse_seq so
        the ownership baseline reflects our latest write. When omitted,
        only set the dirty flag without touching _last_wheelhouse_seq;
        used by safety-net pre-write callers (wrap_or_insert sentinel
        path, transform_selection) that mark dirty before any actual
        write happens. Those callers should call mark_clipboard_dirty
        again with write_seq=... after each subsequent clipboard write.

        Args:
            write_seq: Win32 clipboard sequence number captured immediately
                after a successful WheelHouse-side clipboard write. None
                means the caller has not written and is only setting the
                dirty flag (safety-net usage).
        """
        with self._lock:
            self._clipboard_dirty = True
            if write_seq is not None:
                self._last_wheelhouse_seq = write_seq

    def is_clipboard_dirty(self) -> bool:
        """Return True if any operation in this utterance wrote the clipboard."""
        return self._clipboard_dirty

    def accumulate_text(self, text: str) -> None:
        """Track text being inserted during this utterance.

        Used for post-processing like auto-compression of spelled letters.
        """
        if self._accumulated_text:
            self._accumulated_text += " " + text
        else:
            self._accumulated_text = text
        logger.debug(
            f"[UTT-{self._utterance_id}] Accumulated: "
            f"'{redact_transcript(self._accumulated_text)}'"
        )

    def get_accumulated_text(self) -> str:
        """Return all text accumulated during this utterance, joined with spaces."""
        return self._accumulated_text

    # ========================================================================
    # DEFERRED RESTORE
    # ========================================================================

    def _do_restore(self, expected_pending: PendingRestore) -> None:
        """Timer callback: apply the deferred clipboard restore if still valid.

        The closure-captured ``expected_pending`` is the PendingRestore that
        scheduled this timer. A stale timer that fires after start_utterance
        cancelled or replaced the manager's pending restore must NOT inspect
        the current pending object; that would let an old timer accidentally
        restore for a newer utterance and reintroduce the original race
        (wh-9pye.1).
        """
        with self._lock:
            if self._pending_restore is not expected_pending:
                logger.debug(
                    f"[UTT-{expected_pending.utterance_id}] Stale PendingRestore "
                    f"timer fired; manager's pending differs (or is None). "
                    f"Refusing to restore."
                )
                return
            self._execute_restore_decision_locked(expected_pending)
            if self._pending_restore is expected_pending:
                self._pending_restore = None

    def _execute_restore_decision_locked(self, pending: PendingRestore) -> None:
        """Apply the ownership-aware restore decision. Caller MUST hold ``self._lock``.

        Reads the current Win32 clipboard sequence number. If unchanged from
        ``pending.clipboard_seq_at_paste``, calls ``pyperclip.copy(saved_text)``
        to restore the original clipboard. If the sequence advanced
        (someone else wrote the clipboard between our last paste and now),
        skips the restore so the user's manual copy survives.

        Used by:
        - The timer callback (``_do_restore``) on the normal-deferral path.
        - The expired-pending branch in ``start_utterance`` when
          ``chain_gap_s`` has elapsed but the prior pending is still alive
          (wh-9pye.4).

        Marks ``pending.cancelled = True`` and best-effort cancels its timer
        so any other path that might subsequently see the pending will short
        circuit. Does NOT clear ``self._pending_restore`` -- the caller
        decides whether to clear it (the timer callback clears only if it
        is still the same pending; the expired-pending caller clears
        unconditionally before doing the fresh save).
        """
        if pending.cancelled:
            return
        pending.cancelled = True
        if pending.timer is not None:
            try:
                pending.timer.cancel()
            except Exception:
                pass

        try:
            current_seq = clipboard_sequence.get_sequence_number()
        except Exception as e:
            logger.warning(
                f"[UTT-{pending.utterance_id}] Could not read clipboard sequence "
                f"at restore decision: {e}; refusing to restore (fail safe)"
            )
            return

        if current_seq != pending.clipboard_seq_at_paste:
            logger.info(
                f"[UTT-{pending.utterance_id}] External clipboard write detected "
                f"during deferred restore window "
                f"(seq {pending.clipboard_seq_at_paste} -> {current_seq}); "
                f"preserving user's clipboard, NOT restoring"
            )
            return

        try:
            pyperclip.copy(pending.saved_text)
            logger.debug(
                f"[UTT-{pending.utterance_id}] Restored clipboard "
                f"(seq unchanged at {current_seq}, "
                f"len={len(pending.saved_text) if pending.saved_text else 0})"
            )
        except Exception as e:
            logger.warning(
                f"[UTT-{pending.utterance_id}] Could not restore clipboard: {e}"
            )

    def fire_pending_restore_now(self) -> None:
        """Synchronously fire the pending restore (test hook).

        Tests use this to bypass the threading.Timer deferral and drive the
        restore decision deterministically. Production code never calls this.

        The hook acquires the lock and runs ``_do_restore`` against the
        current pending object, mirroring the timer-fired path exactly.
        """
        with self._lock:
            pending = self._pending_restore
        if pending is None:
            return
        self._do_restore(pending)

    # ========================================================================
    # SAFETY TIMEOUT MANAGEMENT
    # ========================================================================

    def _start_safety_timeout(self, utterance_id: int) -> None:
        """Start safety timeout. Forces end_utterance if the end signal never arrives."""

        def timeout_restore():
            if self._in_utterance and self._utterance_id == utterance_id:
                logger.warning(
                    f"[UTT-{utterance_id}] TIMEOUT after {self.timeout_seconds}s - "
                    f"forcing end_utterance"
                )
                self.end_utterance(utterance_id)

        self._cancel_timeout()
        self._timeout_task = threading.Timer(self.timeout_seconds, timeout_restore)
        self._timeout_task.daemon = True
        self._timeout_task.start()

    def _cancel_timeout(self) -> None:
        """Cancel active safety timeout."""
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None
