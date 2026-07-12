"""Clipboard-based UI operations.

This module provides clipboard interaction methods including:
- Verified paste (with clipboard verification loop)
- Context gathering via arrow-key navigation
- Selection clearing
- Timing-aware operations

All operations handle timing-dependent Windows clipboard behavior.
"""
import logging

from utils.redact import redact_transcript
import time
import unicodedata
import pyperclip
import win32gui
from typing import Dict, Any, Optional
from utils.win_input_sender import press_keys
from ui.clipboard_sequence import get_sequence_number, wait_for_clipboard_write
from ui.hwnd_utils import (
    hwnds_match_for_foreground_compare,
    normalize_hwnd_for_foreground_compare,
    process_name_for_hwnd,
    resolve_same_process_browser_names,
)

# wh-g2-refactor.15: grapheme helpers promoted to the shared module so
# the GUI process can call them without importing this file.
from services.wheelhouse.shared.grapheme import (
    count_grapheme_clusters as _shared_count_grapheme_clusters,
    text_contains_grapheme_unsafe_chars as _shared_text_contains_grapheme_unsafe_chars,
)

logger = logging.getLogger(__name__)


class ClipboardOperations:
    """Handles low-level clipboard operations with verification and timing.

    All methods are aware of clipboard timing issues and include appropriate
    delays and verification loops. Designed to work around flaky clipboard
    behavior in Windows.
    """

    def __init__(self, config: dict):
        """Initialize clipboard operations with timing configuration.

        Args:
            config: Configuration dict containing timing values under
                   ['ui_actions']['timing']
        """
        timing_config = config.get("ui_actions", {}).get("timing", {})

        # Timing configuration (all in seconds)
        self.clipboard_verification_timeout = (
            timing_config.get("clipboard_verification_timeout_ms", 250) / 1000.0
        )
        self.clipboard_operation_delay = (
            timing_config.get("clipboard_operation_delay_ms", 50) / 1000.0
        )
        self.selection_clear_delay = (
            timing_config.get("selection_clear_delay_ms", 20) / 1000.0
        )
        self.context_gather_delay = (
            timing_config.get("context_gather_delay_ms", 10) / 1000.0
        )
        self.post_paste_delay = (
            timing_config.get("post_paste_delay_ms", 30) / 1000.0
        )

        # wh-fc1x.2: resolve the same-process browser list from config.
        # Canonical entries live in
        # services/wheelhouse/config.toml under
        # [ui_actions.foreground_check].same_process_browser_names so
        # users can edit the list without changing code. Fallback to
        # the hardcoded baseline only fires when the config key is
        # missing entirely.
        self._same_process_browser_names: frozenset[str] = (
            resolve_same_process_browser_names(config)
        )

        # Retraction support: count characters pasted during utterance
        self.accumulated_paste_chars: int = 0

        # wh-pkhrp.1.7: track whether any insertion in the current
        # utterance contained surrogate pairs or ZWJ joiners. The
        # retraction counter ``accumulated_paste_chars`` advances by
        # ``len(perfected_text)`` (Python code points), which matches
        # SendInput's per-WCHAR delivery for retract on apps that
        # delete by code unit. On Qt-backed targets the editor deletes
        # by GRAPHEME CLUSTER, so the parallel
        # ``accumulated_paste_clusters`` counter below is the right
        # backspace count there. The retract path picks one counter
        # or the other based on the focused target's class name. This
        # flag stays as an informational signal that an unsafe-char
        # case is in flight; the previous fail-closed gate has been
        # lifted (wh-pkhrp.2).
        self.accumulated_has_grapheme_unsafe: bool = False

        # wh-pkhrp.2: grapheme cluster count for retract on Qt-backed
        # targets. Advances in parallel with ``accumulated_paste_chars``
        # via ``credit_paste_chars``. For BMP-only ASCII the two
        # counters agree; for ZWJ sequences they diverge (clusters is
        # smaller). The retract path uses this counter when the
        # focused target's class name matches the Qt prefix
        # convention.
        self.accumulated_paste_clusters: int = 0

        # wh-pkhrp.2: sticky flag set by ``credit_paste_chars`` when
        # any paste in the current utterance lands in a Qt-backed
        # target (class name matches the Qt prefix convention via
        # ``is_qt_class_name``). The retract path reads this flag to
        # decide whether to use the cluster counter or the code-unit
        # counter as the backspace count. Sticky once set within an
        # utterance because mid-utterance target change is already
        # rejected by the focus-drift gate.
        self.accumulated_paste_was_qt: bool = False

        # Paste provenance flags (wh-d43oi). Both are reset at the start of
        # every verified_paste call and reflect the most recent paste only.
        #
        # last_paste_was_optimistic: True when verified_paste proceeded
        # without a positive clipboard-content match -- the copy succeeded
        # but the verification loop hit pure lock contention until timeout.
        # Consumed by the retract gate to refuse retract with
        # reason='paste_unverified' on optimistic pastes.
        #
        # last_paste_was_sent: True when the ctrl+v keystroke actually fired
        # (either via press_keys or flutter_control.SendKeys). Consumed by
        # the selection-restore path: restore is only safe when a False
        # return from verified_paste happened before the keystroke fired.
        # After the keystroke, the target state is unknown and restore
        # could corrupt user text.
        self.last_paste_was_optimistic: bool = False
        self.last_paste_was_sent: bool = False

        # Selection-restore support (wh-t81d9.5). clear_selection captures
        # any active selection text here before sending Delete so that a
        # later pre-send verified_paste failure can restore it. None means
        # nothing to restore. Updated only by clear_selection (set on
        # detection, cleared when no selection found) and by
        # restore_cleared_selection / the success branch of the slow-path
        # strategy (cleared after consumption).
        self.last_cleared_selection: Optional[str] = None

        # wh-fz7j.2: Win32 clipboard sequence number captured immediately
        # after the most recent successful WheelHouse-side clipboard write.
        # Read by UIActionHandler's mark_clipboard_dirty calls so the
        # UtteranceClipboardManager's ownership baseline reflects the actual
        # post-write seq, not the seq read at some later point in the
        # handler. None means no clipboard write has occurred yet on this
        # ClipboardOperations instance.
        self.last_clipboard_write_seq: Optional[int] = None

        logger.debug("ClipboardOperations initialized with timing configuration")

    # ========================================================================
    # SAFE CLIPBOARD WRAPPERS
    # ========================================================================

    def _safe_copy(self, text: str) -> bool:
        """Safely copy text to clipboard with error handling.

        wh-fz7j.2: captures the post-write Win32 clipboard sequence number
        on success so the deferred-restore ownership check can use the
        actual post-write seq as the WheelHouse baseline. Reading the seq
        at any later point (e.g., when the handler calls mark_clipboard_dirty)
        risks recording a user manual copy that happened in the gap as the
        baseline, which would let the deferred timer overwrite the user's
        clipboard.

        Args:
            text: Text to copy to clipboard

        Returns:
            bool: True if copy succeeded, False if it failed
        """
        try:
            pyperclip.copy(text)
            try:
                self.last_clipboard_write_seq = get_sequence_number()
            except Exception as seq_e:
                logger.warning(f"Could not capture clipboard sequence: {seq_e}")
                self.last_clipboard_write_seq = None
            return True
        except Exception as e:
            logger.error(f"Clipboard copy failed: {e}")
            return False

    def _safe_paste(self) -> Optional[str]:
        """Safely read text from clipboard with error handling.

        Returns:
            The clipboard content, or None if read failed
        """
        try:
            return pyperclip.paste()
        except Exception as e:
            logger.error(f"Clipboard paste/read failed: {e}")
            return None

    def reset_paste_counter(self):
        """Reset the paste character accumulator. Called at utterance start/end."""
        self.accumulated_paste_chars = 0
        # wh-pkhrp.1.7: reset the grapheme-unsafe gate per utterance.
        self.accumulated_has_grapheme_unsafe = False
        # wh-pkhrp.2: reset the parallel cluster counter per utterance.
        self.accumulated_paste_clusters = 0
        # wh-pkhrp.2: reset the Qt-target sticky flag per utterance.
        self.accumulated_paste_was_qt = False

    @staticmethod
    def text_contains_grapheme_unsafe_chars(text: str) -> bool:
        """True when ``text`` contains characters that break the
        ``len()`` == backspace-count equivalence on Qt-backed targets.

        wh-pkhrp.1.7. wh-g2-refactor.15 moved the implementation to
        ``services.wheelhouse.shared.grapheme`` so the GUI process can
        call it without importing this module; the static method here
        delegates so existing callers and tests keep working.
        """
        return _shared_text_contains_grapheme_unsafe_chars(text)

    @staticmethod
    def is_qt_class_name(class_name: str) -> bool:
        """Return True when ``class_name`` looks like a Qt widget class.

        wh-pkhrp.2: Qt widget classes follow the convention ``Q`` +
        capitalised PascalCase (``QPlainTextEdit``, ``QTextEdit``,
        ``QWidget``, ``QLineEdit``). The retract path uses this check
        to pick the grapheme cluster counter over the code-unit
        counter when the focused target deletes by cluster (Qt's
        QTextCursor default).

        Returns False for empty strings, single characters, and any
        class whose second character is not an uppercase letter.
        """
        if not class_name or len(class_name) < 2:
            return False
        return class_name[0] == "Q" and class_name[1].isupper()

    @staticmethod
    def count_grapheme_clusters(text: str) -> int:
        """Count visible grapheme clusters in ``text`` (wh-pkhrp.2).

        wh-g2-refactor.15 moved the segmenter to
        ``services.wheelhouse.shared.grapheme`` so the GUI process's
        credit ledger can call it. The static method here delegates so
        existing Input-process callers and the wh-pkhrp.2 test surface
        keep working.

        Returns 0 for empty input. Cluster rules covered:

        * Zero Width Joiner (U+200D) followed by the joined code point.
        * Zero Width Non-Joiner (U+200C) -- format-category extending
          code point.
        * Variation selectors U+FE00..U+FE0F and U+E0100..U+E01EF.
        * Tag characters U+E0020..U+E007F (subdivision-flag emoji).
        * Fitzpatrick skin-tone modifiers U+1F3FB..U+1F3FF.
        * Combining marks of general category Mn / Mc / Me.
        * Regional indicator pairs (two consecutive U+1F1E6..U+1F1FF
          form one flag cluster).

        The implementation does NOT model the full UAX #29 boundary
        table (no Hangul syllable composition, no Prepend handling).
        Extend the shared helper if a Qt-backed dictation case surfaces
        those cases.
        """
        return _shared_count_grapheme_clusters(text)

    def credit_paste_chars(self, text: str, target_class_name: str = "") -> None:
        """Advance the paste accounting fields for ``text``.

        Canonical hook for paste-counter mutation. Every successful
        paste path MUST call this helper rather than mutating the
        fields inline, so the retract gate has a single provenance
        point (wh-pkhrp.1.7, wh-pkhrp.3.6). The two production paths
        currently routed here are ``verified_paste`` (clipboard
        Ctrl+V) and ``VerifiedUnicodeStrategy.insert`` (Unicode
        SendInput); future paste paths must use this hook.

        Updates four fields:

        * ``accumulated_paste_chars`` -- Python code-point count.
          Backspace target on apps that delete by code unit (the
          historical default).
        * ``accumulated_paste_clusters`` -- grapheme cluster count
          via :meth:`count_grapheme_clusters`. Backspace target on
          Qt-backed targets that delete by cluster (wh-pkhrp.2).
        * ``accumulated_has_grapheme_unsafe`` -- informational flag
          recording whether any insertion in the utterance carried a
          surrogate-pair code point or a ZWJ. Read by structured
          logging; no longer drives a fail-closed retract gate.
        * ``accumulated_paste_was_qt`` -- sticky flag set to True
          when ``target_class_name`` matches the Qt prefix
          convention. The retract path reads this to pick clusters
          over code units (wh-pkhrp.2).

        ``target_class_name`` defaults to empty for backward
        compatibility with paste sites that have not yet been
        threaded. Such sites stay on the code-unit retract counter.
        """
        if not text:
            return
        self.accumulated_paste_chars += len(text)
        self.accumulated_paste_clusters += self.count_grapheme_clusters(text)
        if self.text_contains_grapheme_unsafe_chars(text):
            self.accumulated_has_grapheme_unsafe = True
        if self.is_qt_class_name(target_class_name):
            self.accumulated_paste_was_qt = True

    # ========================================================================
    # VERIFIED PASTE
    # ========================================================================

    def verified_paste(
        self,
        text: str,
        window_manager,
        flutter_control=None,
        target_control=None,
        target_hwnd: Optional[int] = None,
        target_class_name: str = "",
    ) -> bool:
        """Copy text to clipboard, verify it, then paste.

        This method includes:
        1. Copy text to clipboard
        2. Verification loop (poll until clipboard matches)
        3. Focus management (restore window/control focus)
        4. Paste operation (SendInput for normal apps, SendKeys for Flutter)
        5. Post-paste foreground check (wh-59i32, when target_hwnd was provided)
        6. Post-paste delay (protect against clipboard restoration race)

        Resilience: If _safe_copy succeeds but verification times out due to
        clipboard lock contention only (no wrong content observed), proceeds
        with an optimistic paste rather than falling to the heavyweight
        clipboard fallback.  If another process overwrites clipboard content
        during verification, re-copies our text (up to 3 times) before
        retrying the read.

        wh-59i32: ``target_control`` and ``target_hwnd`` let callers pass the
        control and HWND captured at strategy entry so focus drift between
        capture_context and the actual paste cannot send dictation to the
        wrong field. When ``target_hwnd`` is provided, a post-paste foreground
        check confirms the paste actually landed on the intended top-level
        window before the retract accounting counter is advanced. Legacy
        callers that pass neither still get the existing
        ``window_manager.get_target_window(None)`` fallback behavior with no
        post-paste check (no expected value to compare against).

        Args:
            text: Text to paste
            window_manager: WindowFocusManager instance for focus restoration
            flutter_control: Optional Flutter control (uses SendKeys instead of SendInput)
            target_control: Optional UIA control captured at strategy entry.
                When provided, focus restoration uses this control directly
                instead of querying GetFocusedControl at paste time.
            target_hwnd: Optional top-level HWND captured at strategy entry.
                When provided, focus restoration uses this HWND directly and
                a post-paste foreground check confirms the paste landed where
                the caller expected.

        Returns:
            bool: True if paste succeeded and (when target_hwnd was provided)
                the foreground window after Ctrl+V matched. False if
                verification failed or the post-paste foreground check
                detected drift.

        :flow: Clipboard-Based Text Insertion
        :step: 2
        :consumes_from: Flutter Application Detection
        :description: Pastes text using clipboard with Flutter-aware input API selection.
            Uses SendKeys (UIA) for Flutter apps, SendInput for normal apps. Includes
            verification loop to ensure clipboard updates complete before pasting.
        :data_in: Text string, Flutter control reference (optional)
        :data_out: Paste success status
        """
        # Reset provenance flags at the top of every call so a previous
        # paste's state cannot leak into this one (wh-d43oi).
        self.last_paste_was_optimistic = False
        self.last_paste_was_sent = False

        # wh-yki6: when the caller supplied a UIA-derived target_hwnd, also
        # capture GetForegroundWindow at this entry point and log only if the
        # two APIs already disagree. The post-paste check at the bottom of
        # this method compares actual_hwnd against target_hwnd, so a mismatch
        # there is ambiguous: it can mean either "foreground changed during
        # the paste" or "the two APIs always disagreed for this control."
        # This entry-time log lets us tell which one is happening.
        if target_hwnd is not None:
            try:
                entry_foreground_hwnd = win32gui.GetForegroundWindow()
            except Exception as e:
                logger.debug(
                    "verified_paste entry: GetForegroundWindow failed: %s", e,
                )
                entry_foreground_hwnd = None
            if entry_foreground_hwnd is not None:
                target_root = normalize_hwnd_for_foreground_compare(target_hwnd)
                entry_root = normalize_hwnd_for_foreground_compare(
                    entry_foreground_hwnd,
                )
                if target_root != entry_root:
                    logger.info(
                        "verified_paste entry: target_hwnd=%s (root=%s, "
                        "process=%s) disagrees with GetForegroundWindow=%s "
                        "(root=%s, process=%s) before any paste",
                        target_hwnd, target_root,
                        process_name_for_hwnd(target_hwnd),
                        entry_foreground_hwnd, entry_root,
                        process_name_for_hwnd(entry_foreground_hwnd),
                    )

        t_start = time.perf_counter()
        if not self._safe_copy(text):
            logger.error(
                "clipboard copy outcome: failed (text_len=%d), aborting paste",
                len(text),
            )
            return False
        logger.info("clipboard copy outcome: success (text_len=%d)", len(text))
        t_after_copy = time.perf_counter()
        start_time = time.perf_counter()

        # Track verification failure modes to decide on optimistic paste
        saw_wrong_content = False
        lock_failures = 0
        recopy_budget = 3
        recopy_attempts = 0
        recopy_failures = 0
        proceeding_optimistically = False

        # Verification loop: Wait for clipboard to reflect our text
        while time.perf_counter() - start_time < self.clipboard_verification_timeout:
            current = self._safe_paste()
            if current is None:
                # Clipboard read failed (locked by another process)
                lock_failures += 1
                time.sleep(0.01)
                continue
            if current == text:
                # wh-zcx9.2: log recovery context on the verified path so
                # races that triggered a recopy do not look identical to a
                # clean verification in the trace.
                if saw_wrong_content or recopy_attempts > 0 or lock_failures > 0:
                    logger.info(
                        "clipboard verification outcome: verified (recovered: "
                        "saw_wrong_content=%s, recopy_attempts=%d, "
                        "recopy_failures=%d, lock_failures=%d)",
                        saw_wrong_content, recopy_attempts,
                        recopy_failures, lock_failures,
                    )
                else:
                    logger.info("clipboard verification outcome: verified")
                break  # Verified!

            # Content mismatch: another process overwrote clipboard
            saw_wrong_content = True
            if recopy_budget > 0:
                if not self._safe_copy(text):
                    recopy_failures += 1
                recopy_budget -= 1
                recopy_attempts += 1
            time.sleep(0.01)
        else:
            # Timeout expired without verification
            if not saw_wrong_content and lock_failures > 0:
                # Pure lock contention: copy succeeded, only lock errors during
                # reads.  Our text is almost certainly still on the clipboard.
                # Optimistic paste avoids the invasive clipboard fallback
                # (arrow keys + Ctrl+C) that disrupts browser fields.
                logger.warning(
                    "clipboard verification outcome: optimistic "
                    "(lock_failures=%d, recopy_attempts=%d, "
                    "recopy_failures=%d, no wrong content seen)",
                    lock_failures, recopy_attempts, recopy_failures,
                )
                proceeding_optimistically = True
            else:
                logger.error(
                    "clipboard verification outcome: failed "
                    "(saw_wrong_content=%s, lock_failures=%d, "
                    "recopy_attempts=%d, recopy_failures=%d), aborting paste",
                    saw_wrong_content, lock_failures,
                    recopy_attempts, recopy_failures,
                )
                return False

        # --- Proceed with paste (either verified or optimistic) ---
        # The provenance flag for the retract gate must reflect whether the
        # paste bytes were positively confirmed on the clipboard before send.
        self.last_paste_was_optimistic = proceeding_optimistically

        t_after_verify = time.perf_counter()

        # Restore focus before pasting
        # Flutter optimization: Skip focus restoration (already has focus)
        if not flutter_control:
            try:
                # wh-59i32: prefer the explicit target captured by the
                # strategy. Fall back to get_target_window(None) only when
                # the caller did not supply one (legacy callers and the
                # transformer paths under specific.SelectionTransformer).
                if target_hwnd is not None or target_control is not None:
                    hwnd = target_hwnd
                    resolved_control = target_control
                    if hwnd is None and target_control is not None:
                        try:
                            top = target_control.GetTopLevelControl()
                            hwnd = top.NativeWindowHandle if top else None
                        except Exception:
                            hwnd = None
                else:
                    hwnd, resolved_control = window_manager.get_target_window(None)

                if hwnd:
                    window_manager.ensure_focused(hwnd)

                if resolved_control:
                    try:
                        resolved_control.SetFocus()
                    except Exception as e:
                        logger.warning(f"Could not re-focus control before pasting: {e}")
            except Exception as e:
                # wh-zcx9.3: surface the previously-swallowed outer
                # focus-restore failure so a future trace shows
                # 'focus restore failed -> paste requested' instead of a
                # silent verified-then-paste sequence. Keep the fail-safe
                # behavior (continue with the paste attempt).
                logger.warning(
                    "focus restore: outer exception "
                    "(target_hwnd=%s, has_target_control=%s): %s",
                    target_hwnd, target_control is not None, e,
                )

        t_after_focus = time.perf_counter()

        # Record that the paste keystroke is about to fire. Must sit before
        # the Flutter-vs-non-Flutter dispatch so BOTH branches flip the flag
        # true (wh-d43oi). Selection-restore reads this to decide whether a
        # False return is safe to auto-restore from.
        self.last_paste_was_sent = True

        # Execute paste (Flutter needs SendKeys, others use SendInput)
        # wh-zcx9.1: log says 'requested' rather than 'sent' because
        # press_keys() returns None and only logs SendInput failures
        # internally; the trace's positive signal is paired with the
        # error log emitted by win_input_sender.SendInput on partial
        # delivery. The log proves the request was issued, not that the
        # OS accepted every event.
        if flutter_control and flutter_control.Exists(0, 0):
            flutter_control.SendKeys('{Ctrl}v')
            paste_path = "flutter_sendkeys"
        else:
            press_keys('ctrl', 'v')
            paste_path = "sendinput"

        logger.info(
            "paste keystroke requested: '%s' via %s",
            redact_transcript(text), paste_path,
        )

        t_after_sendkeys = time.perf_counter()

        # CRITICAL TIMING: Delay ensures Windows applications fully consume
        # clipboard content before clipboard_context restores original clipboard.
        # Protects against race condition where clipboard restoration happens
        # during application's paste processing (especially with images).
        time.sleep(self.post_paste_delay)
        t_after_sleep = time.perf_counter()

        # wh-59i32: post-paste foreground check. Only when the caller passed
        # an explicit target_hwnd do we have something to compare against;
        # legacy callers (target_hwnd=None) keep the existing behavior. If
        # focus drifted DURING the paste -- e.g. an alert popped up between
        # SetFocus and Ctrl+V landing -- refuse to credit the retract counter
        # and report failure to the caller. The pre-paste resolution above
        # protects against drift before the paste; this check protects
        # against drift during the paste.
        #
        # wh-oe7u.3: both expected and observed HWNDs are root-normalized
        # via win32gui.GetAncestor(GA_ROOT) so Chromium/Electron renderer
        # children compare equal to their top-level frame. Fail closed on
        # ANY normalization failure on either side -- a None on the
        # observed side previously fell open via ``actual_hwnd =
        # target_hwnd``, which silently bypassed this gate when
        # GetForegroundWindow raised.
        if target_hwnd is not None and not flutter_control:
            expected_root = normalize_hwnd_for_foreground_compare(target_hwnd)
            if expected_root is None:
                logger.warning(
                    "Post-paste check: target_hwnd=%s could not be "
                    "root-normalized; refusing to credit counter.",
                    target_hwnd,
                )
                return False
            try:
                actual_hwnd = win32gui.GetForegroundWindow()
            except Exception as e:
                logger.warning(
                    "Post-paste GetForegroundWindow failed: %s; refusing "
                    "to credit counter (fail-closed, wh-oe7u.3).", e,
                )
                return False
            actual_root = normalize_hwnd_for_foreground_compare(actual_hwnd)
            if actual_root is None:
                logger.warning(
                    "Post-paste check: observed foreground hwnd=%s could "
                    "not be root-normalized; refusing to credit counter.",
                    actual_hwnd,
                )
                return False
            if actual_root != expected_root:
                # wh-fc1x.2: Chromium pages routinely produce a
                # same-process foreground drift where UIA's
                # GetTopLevelControl HWND and Win32 GetForegroundWindow
                # return different top-level roots owned by the same
                # browser process. The paste keystrokes still land in
                # the focused renderer of the main HWND (the same
                # wh-3nwy pattern that VerifiedUnicodeStrategy already
                # tolerates at strategies/specific.py:915). Try the
                # same-process fallback via hwnds_match_for_foreground_compare,
                # constrained to the Chromium-derived browser list so
                # non-browser apps keep the strict GA_ROOT contract.
                target_process = process_name_for_hwnd(target_hwnd)
                allow_same_process = (
                    target_process is not None
                    and target_process in self._same_process_browser_names
                )
                same_process_match = (
                    allow_same_process
                    and hwnds_match_for_foreground_compare(
                        target_hwnd, actual_hwnd,
                        allow_same_process=True,
                        expected_process_name=target_process,
                    )
                )
                if not same_process_match:
                    logger.warning(
                        "Post-paste foreground check failed: expected "
                        "root=%s (from hwnd=%s, process=%s), observed "
                        "root=%s (from hwnd=%s, process=%s, "
                        "allow_same_process=%s). Skipping counter increment.",
                        expected_root, target_hwnd, target_process,
                        actual_root, actual_hwnd,
                        process_name_for_hwnd(actual_hwnd),
                        allow_same_process,
                    )
                    return False

        # Track characters pasted for retraction support. Routed
        # through credit_paste_chars so the counter, the grapheme-unsafe
        # flag, and the Qt-target flag stay in lockstep with
        # VerifiedUnicodeStrategy (wh-pkhrp.3.6, wh-pkhrp.2).
        self.credit_paste_chars(text, target_class_name=target_class_name)

        if flutter_control:
            logger.info(f"[FLUTTER PASTE TIMING] copy:{(t_after_copy-t_start)*1000:.1f}ms "
                       f"verify:{(t_after_verify-t_after_copy)*1000:.1f}ms "
                       f"focus:{(t_after_focus-t_after_verify)*1000:.1f}ms "
                       f"sendkeys:{(t_after_sendkeys-t_after_focus)*1000:.1f}ms "
                       f"sleep:{(t_after_sleep-t_after_sendkeys)*1000:.1f}ms "
                       f"total:{(t_after_sleep-t_start)*1000:.1f}ms")

        return True

    # ========================================================================
    # SELECTION MANAGEMENT
    # ========================================================================

    def clear_selection(self, flutter_control=None) -> bool:
        """Clear any existing text selection before gathering context.

        Uses sentinel value to detect if selection exists, then deletes it.
        This follows the philosophy that selected text will be replaced by
        dictation, so we should delete it first to simplify processing.

        wh-t81d9.5: when a selection is detected, the captured text is
        stored on ``self.last_cleared_selection`` so a subsequent pre-send
        ``verified_paste`` failure can restore it. The slot is reset to
        None when no selection is detected so a stale value from a prior
        call does not produce a spurious restore.

        Args:
            flutter_control: Optional Flutter control (uses SendKeys instead of SendInput)

        Returns:
            bool: True if operation succeeded, False on error
        """
        try:
            # Test if there's a selection by trying to copy it.
            # wh-fz7j.4: route through _safe_copy so last_clipboard_write_seq
            # is updated even for sentinel writes -- otherwise the deferred
            # restore ownership check can compare against a stale baseline.
            # If _safe_copy fails, we cannot distinguish the sentinel from
            # an actual selection, so abort with False (preserves the prior
            # behaviour where pyperclip.copy raising bubbled to the outer
            # except).
            sentinel = f"__SENTINEL_SEL_{time.time()}__"
            if not self._safe_copy(sentinel):
                return False

            # Copy any selected text
            if flutter_control and flutter_control.Exists(0, 0):
                flutter_control.SendKeys('{Ctrl}c')
            else:
                press_keys('ctrl', 'c')
            time.sleep(self.selection_clear_delay)

            selected_text = pyperclip.paste()
            if selected_text != sentinel and selected_text:
                # There was a selection. Capture it for possible restore
                # before sending Delete, then issue the Delete (wh-t81d9.5).
                self.last_cleared_selection = selected_text
                if flutter_control and flutter_control.Exists(0, 0):
                    flutter_control.SendKeys('{Delete}')
                else:
                    press_keys('delete')
                time.sleep(self.context_gather_delay)
                logger.debug(f"Cleared existing selection: '{redact_transcript(selected_text)}'")
            else:
                # No selection found. Reset so a value from a prior
                # clear_selection call does not survive into a restore
                # decision for a different paste (wh-t81d9.5).
                self.last_cleared_selection = None

            return True

        except Exception as e:
            logger.error(f"Error clearing selection: {e}")
            return False

    # ========================================================================
    # SELECTION RESTORE (wh-t81d9.5)
    # ========================================================================

    def _raw_paste(
        self,
        text: str,
        window_manager,
        target_control=None,
        target_hwnd: Optional[int] = None,
        flutter_control=None,
    ) -> bool:
        """Best-effort paste used by selection restore.

        This intentionally bypasses the verification loop, the post-paste
        foreground check, and -- most importantly -- every retract
        accounting flag. The restored text is the user's PRIOR content,
        not new dictation, so it must look invisible to the retract
        subsystem. Updating ``accumulated_paste_chars`` would let a later
        retract backspace through the restored text; updating
        ``last_paste_was_optimistic`` would block retract on a phantom
        provenance flag (wh-t81d9.5).

        Args:
            text: Text to paste.
            window_manager: WindowFocusManager instance for focus
                restoration. Threaded through explicitly because
                ``ClipboardOperations`` does not own a focus manager
                (same plumbing pattern as ``verified_paste``).
            target_control: Optional UIA control captured at strategy
                entry. Used for control-level focus restoration.
            target_hwnd: Optional top-level HWND captured at strategy
                entry. Used for window-level focus restoration.
            flutter_control: Optional Flutter control. When provided and
                the control still exists, the paste fires via
                ``SendKeys('{Ctrl}v')`` and focus restoration is skipped
                (matches ``verified_paste`` Flutter optimization).

        Returns:
            True if the copy and the paste keystroke both fired, False
            on copy failure.
        """
        if not self._safe_copy(text):
            logger.error("Selection-restore copy failed; not pasting.")
            return False

        # Brief verification attempt. We do not need a full retry loop
        # here because the restore path is best-effort by design and the
        # caller has already lost the selection if this fails.
        time.sleep(self.clipboard_operation_delay)

        # Restore focus before pasting (skipped on Flutter, same as
        # verified_paste).
        if not (flutter_control and flutter_control.Exists(0, 0)):
            try:
                hwnd = target_hwnd
                resolved_control = target_control
                if hwnd is None and target_control is not None:
                    try:
                        top = target_control.GetTopLevelControl()
                        hwnd = top.NativeWindowHandle if top else None
                    except Exception:
                        hwnd = None
                if hwnd:
                    window_manager.ensure_focused(hwnd)
                if resolved_control:
                    try:
                        resolved_control.SetFocus()
                    except Exception as e:
                        logger.warning(
                            f"Could not re-focus control during selection restore: {e}"
                        )
            except Exception:
                pass

        if flutter_control and flutter_control.Exists(0, 0):
            flutter_control.SendKeys('{Ctrl}v')
        else:
            press_keys('ctrl', 'v')

        time.sleep(self.post_paste_delay)
        return True

    def restore_cleared_selection(
        self,
        window_manager,
        target_control=None,
        target_hwnd: Optional[int] = None,
        flutter_control=None,
    ) -> bool:
        """Restore the selection that ``clear_selection`` deleted.

        Called by ``ClipboardFallbackStrategy.insert`` when
        ``verified_paste`` returned False AND ``last_paste_was_sent`` is
        False (a clean pre-send failure: the Ctrl+V never fired, the
        target field is unchanged from when ``gather_context`` left it,
        and ``gather_context``'s arrow-key sequence is balanced so the
        caret is back at the original position) (wh-t81d9.5).

        Args:
            window_manager: WindowFocusManager instance for focus
                restoration during the raw paste.
            target_control: UIA control captured at strategy entry. Used
                so the restore lands in the field that was supposed to
                receive the original paste even if focus has drifted.
            target_hwnd: Top-level HWND captured at strategy entry.
            flutter_control: Flutter control captured at strategy entry,
                or None for non-Flutter targets. The restore must use
                the same Flutter-aware branch as the original paste or
                it will silently fail on Flutter fields.

        Returns:
            True if a restore was attempted, False if there was nothing
            to restore.
        """
        if self.last_cleared_selection is None:
            return False

        text = self.last_cleared_selection
        # Snapshot the retract-accounting state so we can restore it
        # exactly. _raw_paste does not touch these by contract, but a
        # belt-and-braces approach here makes the invariant explicit
        # against future drift.
        prior_accum = self.accumulated_paste_chars
        prior_optimistic = self.last_paste_was_optimistic
        prior_sent = self.last_paste_was_sent

        try:
            self._raw_paste(
                text,
                window_manager,
                target_control=target_control,
                target_hwnd=target_hwnd,
                flutter_control=flutter_control,
            )
        finally:
            # Always clear after the attempt -- success or failure --
            # so a second consumer cannot fire on stale state.
            self.last_cleared_selection = None
            # Pin the retract-accounting flags back to their prior
            # values. The restore is invisible to the retract subsystem.
            self.accumulated_paste_chars = prior_accum
            self.last_paste_was_optimistic = prior_optimistic
            self.last_paste_was_sent = prior_sent

        logger.info(
            "Restored cleared selection (%d chars) after pre-send paste failure",
            len(text),
        )
        return True

    # ========================================================================
    # CONTEXT GATHERING
    # ========================================================================

    def gather_context(self, flutter_control=None) -> Dict[str, Any]:
        """Gather text context using clipboard and arrow-key navigation.

        [WORKAROUND] For non-UIA-cooperating elements. Uses arrow keys to
        select text before/after cursor, copies it, then returns cursor to
        original position.

        Args:
            flutter_control: Optional Flutter control (uses SendKeys instead of SendInput)

        Returns:
            dict: Context with keys:
                - 'preceding_chars': Text before cursor (up to 2 chars)
                - 'has_selection': Always False (selection is cleared before this)

        Handles edge cases:
        - Beginning of document (no text before cursor)
        - End of document (no text after cursor)
        - Text that doesn't copy (sentinel unchanged)
        
        :flow: Context Gathering for Text Perfection
        :step: 1
        :produces_for: Clipboard-Based Text Insertion
        :description: Retrieves text context around cursor using clipboard round-trip.
            Flutter-aware: uses SendKeys (UIA) for Flutter apps (~560ms per operation),
            SendInput for normal apps (fast). Determines proper capitalization and spacing.
        :data_in: Flutter control reference (optional)
        :data_out: Dictionary with preceding_chars and has_selection flags
        :notes: Flutter performance: ~3-4 seconds due to multiple slow SendKeys calls.
            Shadow buffer bypasses this on subsequent words in same utterance.
        """
        before_text = ""
        after_text = ""

        try:
            # Validate flutter control once at start (used throughout method)
            use_flutter = flutter_control and flutter_control.Exists(0, 0)

            # 1. Get text BEFORE the cursor (up to 2 chars).
            # wh-fz7j.4: route through _safe_copy for seq tracking. Failure
            # to write the sentinel means we cannot distinguish it from a
            # real selection, so propagate as a clipboard exception.
            sentinel_before = f"__SENTINEL_B_{time.time()}__"
            if not self._safe_copy(sentinel_before):
                raise RuntimeError("clipboard copy failed (sentinel_before)")

            # Try to select 2 chars to the left
            if use_flutter:
                # Separate SendKeys calls (combining doesn't work - need time for updates)
                flutter_control.SendKeys('{Shift}{Left}')
                flutter_control.SendKeys('{Shift}{Left}')
            else:
                press_keys('shift', 'left', 'left')

            seq_before = get_sequence_number()
            if use_flutter:
                flutter_control.SendKeys('{Ctrl}c')
                time.sleep(self.clipboard_operation_delay)
            else:
                press_keys('ctrl', 'c')
                seq_changed = wait_for_clipboard_write(
                    seq_before, timeout_s=self.clipboard_operation_delay * 3
                )
                if not seq_changed:
                    logger.debug("Seq polling timed out for 'before' context read")

            read_before = pyperclip.paste()
            if read_before != sentinel_before:
                before_text = read_before
                # Move cursor back to original position
                # Single right arrow deselects and positions cursor at end of selection
                if use_flutter:
                    flutter_control.SendKeys('{Right}')
                else:
                    press_keys('right')
                time.sleep(0.001)  # CRITICAL: Brief yield for cursor positioning

            # 2. Get text AFTER the cursor (1 char).
            # wh-fz7j.4: route through _safe_copy for seq tracking.
            sentinel_after = f"__SENTINEL_A_{time.time()}__"
            if not self._safe_copy(sentinel_after):
                raise RuntimeError("clipboard copy failed (sentinel_after)")

            # Select 1 char to the right
            if use_flutter:
                flutter_control.SendKeys('{Shift}{Right}')
            else:
                press_keys('shift', 'right')

            seq_after = get_sequence_number()
            if use_flutter:
                flutter_control.SendKeys('{Ctrl}c')
                time.sleep(self.clipboard_operation_delay)
            else:
                press_keys('ctrl', 'c')
                seq_changed = wait_for_clipboard_write(
                    seq_after, timeout_s=self.clipboard_operation_delay * 3
                )
                if not seq_changed:
                    logger.debug("Seq polling timed out for 'after' context read")

            read_after = pyperclip.paste()
            if read_after != sentinel_after:
                after_text = read_after
                # Move cursor back to original position
                if use_flutter:
                    flutter_control.SendKeys('{Left}')
                else:
                    press_keys('left')
                time.sleep(0.001)  # CRITICAL: Brief yield for cursor positioning

        except Exception as e:
            logger.error(f"Error during clipboard context gathering: {e}")
            # Return empty context on any failure
            return {'preceding_chars': '', 'has_selection': False}

        # Return context in the same format as ShadowBufferManager
        return {
            'preceding_chars': before_text,
            'has_selection': False  # This method doesn't detect selections
        }
