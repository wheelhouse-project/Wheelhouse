"""Clipboard-based UI operations.

This module provides clipboard interaction methods including:
- Verified paste (with clipboard verification loop)
- Context gathering via arrow-key navigation
- Selection clearing
- Timing-aware operations

All operations handle timing-dependent Windows clipboard behavior.
"""
import contextlib
import logging
import threading
from _ctypes import COMError

from utils.redact import redact_transcript
import time
# The copy retry reads its own clock names, not ``time.*``, so a test that
# replaces this module's ``time`` for the paste timing leaves them real.
from time import monotonic as _copy_monotonic, sleep as _copy_sleep
import unicodedata
import pyperclip
from pyperclip import PyperclipWindowsException
import win32gui
from typing import Dict, Any, Optional
# wh-review-pattern-fixes.36: every non-Flutter state-changing send in
# this module goes through verified_press_keys. press_keys returns None
# and only logs a short SendInput count, so a dropped keystroke was
# indistinguishable from a delivered one -- an undelivered Ctrl+C read a
# live user selection as "no selection", and an undelivered Ctrl+V was
# reported as a successful paste. _send_modifier_keyups releases a
# modifier that a short chord can leave physically held (wh-eolas.2.5).
from utils.win_input_sender import _send_modifier_keyups, verified_press_keys
from ui.clipboard_sequence import get_sequence_number, wait_for_clipboard_write
from ui.paste_landing import (
    PasteCheck,
    focused_control_hwnd,
    max_growth_of_one_paste,
    prepare_paste_check,
    wait_for_length_change,
)
from ui.hwnd_utils import (
    normalize_hwnd_for_foreground_compare,
    process_identity_for_hwnd,
    process_name_for_hwnd,
    read_hwnd_provenance,
    resolve_same_process_browser_names,
    same_process_fallback_matches,
    top_level_hwnd_from_control,
)
from ui.target_identity import TargetIdentity
from ui.invoke_error_codes import UIA_E_ELEMENTNOTAVAILABLE

# wh-g2-refactor.15: grapheme helpers promoted to the shared module so
# the GUI process can call them without importing this file.
from services.wheelhouse.shared.grapheme import (
    count_grapheme_clusters as _shared_count_grapheme_clusters,
    text_contains_grapheme_unsafe_chars as _shared_text_contains_grapheme_unsafe_chars,
)

logger = logging.getLogger(__name__)

# wh-dictation-paste-loss-load.2: how long one word keeps retrying a copy
# while another program holds the clipboard open. ArsClip.exe held it
# 2.4 s under load, and pyperclip's own 0.5 s loop gave up first, so the
# word was lost. pyperclip's 0.5 s counts toward this total, and the last
# pyperclip call can end up to 0.5 s past it.
COPY_RETRY_SECONDS = 3.0
# Pause between two pyperclip calls. pyperclip already polls every 10 ms
# inside a call, so this only keeps the retry from spinning back to back.
_COPY_RETRY_PAUSE = 0.05
# The message pyperclip's clipboard() context manager raises when
# OpenClipboard keeps failing for its whole 0.5 s. Its other
# PyperclipWindowsException messages (SetClipboardData and others) are
# not a held clipboard and are not retried.
_OPEN_CLIPBOARD_FAILURE = "Error calling OpenClipboard"


def _is_open_clipboard_failure(error: Exception) -> bool:
    return (
        isinstance(error, PyperclipWindowsException)
        and _OPEN_CLIPBOARD_FAILURE in str(error)
    )


def captured_hwnd_survives_stale_focus(
    error: Exception, hwnd: Optional[int], identity: Optional[TargetIdentity],
) -> bool:
    """Allow only a stale UIA element to defer to its still-proven HWND.

    SetFocus failure is not generally recoverable. Only the actual COM
    element-unavailable error qualifies, and neither a same-process sibling
    nor a browser helper can replace the exact captured target root.
    This does not retry focus or input; callers retain their final send checks.
    """
    if not isinstance(error, COMError):
        return False
    if error.hresult & 0xFFFFFFFF != UIA_E_ELEMENTNOTAVAILABLE:
        return False
    try:
        return (
            identity is not None
            and hwnd == identity.root
            and identity.is_current()
            and normalize_hwnd_for_foreground_compare(
                win32gui.GetForegroundWindow()
            ) == identity.root
        )
    except Exception:
        return False


def _DELIVERY_FAILED_CONTEXT() -> Dict[str, Any]:
    """The context dict ``gather_context`` returns when its probe fails.

    wh-review-pattern-fixes.36. The context values are the same empty
    ones the method has always returned on failure; ``delivery_failed``
    is what tells the caller that the empty values mean "the probe did
    not complete" rather than "the caret sits at the start of an empty
    field". The caller must abort before any paste.

    wh-review-pattern-fixes.39: the exception branch returns this dict
    too, so the name now covers two failure kinds -- a short SendInput
    chord and a raise out of the probe. Both leave the same two things
    unknown (the caret position and whether a selection is live), which
    is what the caller acts on, so they share one signal rather than
    two. The key keeps its name because every consumer of
    ``gather_context`` already fails closed on it.
    """
    return {
        'preceding_chars': '',
        'has_selection': False,
        'delivery_failed': True,
    }


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
        # wh-dictation-paste-loss-load: how long, from the Ctrl+V, a
        # RichEditD2DPT control may take to show the pasted text before
        # verified_paste sends Ctrl+V once more. A Ctrl+V still waiting in
        # the target's input queue looks the same as a lost one, so this
        # must cover the slowest landed paste under load. The wait costs
        # time only for a word that did not arrive.
        # crewcut: 500 ms is a first value; the load session measures the
        # time from Ctrl+V to the length change for landed words and sets
        # this default from the measured maximum plus a margin.
        self.paste_landing_wait = (
            timing_config.get("paste_landing_wait_ms", 500) / 1000.0
        )
        # The utterance the Input process is typing, for log lines.
        # UIActionHandler.start_utterance sets it.
        self.current_utterance_id = None

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
        # (either via verified_press_keys or flutter_control.SendKeys).
        # Consumed by the selection-restore path: restore is only safe when
        # a False return from verified_paste happened before the keystroke
        # fired. After the keystroke, the target state is unknown and
        # restore could corrupt user text.
        # wh-review-pattern-fixes.36: a SendInput chord that accepted zero
        # events counts as "not fired" -- nothing reached the target, so
        # the restore is safe. Any accepted event leaves the flag True.
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

        # wh-dictation-paste-loss-load.2: the copy-retry budget of the
        # word being inserted. ``copy_retry_budget`` opens it; while it is
        # open, every _safe_copy on the opening thread draws on the same
        # COPY_RETRY_SECONDS. Closed (depth 0) is the default, and then
        # each _safe_copy gets the whole window to itself.
        self._copy_budget_depth = 0
        self._copy_budget_owner: Optional[int] = None
        self._copy_budget_spent = 0.0
        # True after a copy in the open budget gave up on a held
        # clipboard. Later copies of the same word then skip pyperclip.
        self._copy_budget_gave_up = False
        # True when the latest _safe_copy returned False without calling
        # pyperclip because an earlier copy of the word gave up.
        self._last_copy_skipped = False
        # wh-clipboard-save-lost-restore: seconds already spent before the
        # next budget opens (the clipboard save at utterance start).
        self._copy_budget_charge = 0.0

        logger.debug("ClipboardOperations initialized with timing configuration")

    # ========================================================================
    # SAFE CLIPBOARD WRAPPERS
    # ========================================================================

    @contextlib.contextmanager
    def copy_retry_budget(self):
        """Make every copy for one word share one COPY_RETRY_SECONDS.

        wh-dictation-paste-loss-load.2. A word can reach _safe_copy
        several times: ShadowBufferStrategy copies it, and when that
        fails the clipboard fallback copies a sentinel and the word
        again. Each call retrying for the full window would hold a
        single word for a multiple of it. Inside this block the time
        every retrying copy spends counts against one window. A copy that
        starts after the window is spent makes one attempt only. Once a
        copy gives up on a held clipboard, later copies return False at
        once without calling pyperclip. Nesting keeps the outermost budget.
        """
        if self._copy_budget_depth == 0:
            self._copy_budget_owner = threading.get_ident()
            self._copy_budget_spent = self._copy_budget_charge
            self._copy_budget_charge = 0.0
            self._copy_budget_gave_up = False
        self._copy_budget_depth += 1
        try:
            yield
        finally:
            self._copy_budget_depth -= 1
            if self._copy_budget_depth == 0:
                self._copy_budget_owner = None

    def charge_next_copy_budget(self, seconds: float) -> None:
        """Start the next copy_retry_budget with ``seconds`` already spent.

        wh-clipboard-save-lost-restore (boss ruling 2026-10-20 20:36,
        option 1). The clipboard save at utterance start retries for up to
        COPY_RETRY_SECONDS too, and Logic's 5.0 s wait for the first word
        includes it. The handler charges the save time here, so the save
        and the first word's copies share one window. The first copy still
        makes one attempt. The charge applies to one budget only, and each
        utterance start replaces it.
        """
        self._copy_budget_charge = max(0.0, seconds)

    def _safe_copy(self, text: str, *, retry: bool = True) -> bool:
        """Safely copy text to clipboard with error handling.

        wh-dictation-paste-loss-load.2: while another program holds the
        clipboard open, pyperclip.copy raises after its own 0.5 s loop.
        This method calls it again until COPY_RETRY_SECONDS have passed
        since the first attempt (pyperclip's 0.5 s counts), pausing
        briefly between calls. Only that failure is retried; any other
        exception returns False at once. It retries only inside
        ``copy_retry_budget`` on the thread that opened it, and there the
        window is shared with the rest of the word. Outside that block, or
        with ``retry=False``, it makes one attempt, as it did before.

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
        self._last_copy_skipped = False
        in_budget = (
            retry
            and self._copy_budget_depth > 0
            and self._copy_budget_owner == threading.get_ident()
        )
        # Boss ruling 01:35: only a dictated word retries. A copy outside
        # the word's budget (the selection sentinels of transform_selection,
        # wrap_or_insert, capture_selected_text, replace_selected_text)
        # makes one attempt, so two of them stay far below the 5.0 s
        # send_request timeout and the 6.0 s dispatch stall report.
        retry = in_budget
        remaining = COPY_RETRY_SECONDS
        if in_budget:
            if self._copy_budget_gave_up:
                self._last_copy_skipped = True
                logger.debug(
                    "Clipboard copy skipped: an earlier copy for this word "
                    "gave up on a held clipboard"
                )
                return False
            # A copy that succeeded late can spend the whole window. The
            # next copy still makes one attempt (verifier finding D1).
            remaining = max(0.0, remaining - self._copy_budget_spent)

        started = _copy_monotonic()
        deadline = started + remaining
        try:
            while True:
                try:
                    pyperclip.copy(text)
                    try:
                        self.last_clipboard_write_seq = get_sequence_number()
                    except Exception as seq_e:
                        logger.warning(
                            f"Could not capture clipboard sequence: {seq_e}"
                        )
                        self.last_clipboard_write_seq = None
                    return True
                except Exception as e:
                    now = _copy_monotonic()
                    if (
                        not retry
                        or not _is_open_clipboard_failure(e)
                        or now >= deadline
                    ):
                        if in_budget:
                            if _is_open_clipboard_failure(e):
                                self._copy_budget_gave_up = True
                            logger.error(
                                "Clipboard copy failed after %.1f s "
                                "(utterance=%s): %s",
                                now - started, self.current_utterance_id, e,
                            )
                        else:
                            logger.error(
                                f"Clipboard copy failed after "
                                f"{now - started:.1f} s: {e}"
                            )
                        return False
                    _copy_sleep(min(_COPY_RETRY_PAUSE, deadline - now))
        finally:
            if in_budget:
                self._copy_budget_spent += _copy_monotonic() - started

    def _safe_paste(self) -> Optional[str]:
        """Safely read text from clipboard with error handling.

        Returns:
            The clipboard content, or None if read failed
        """
        try:
            return pyperclip.paste()
        except Exception as e:
            logger.warning(f"Clipboard paste/read failed: {e}")
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
    # FOREGROUND PROOF (wh-review-pattern-fixes.41)
    # ========================================================================

    def _foreground_matches_target(
        self,
        target_hwnd: Optional[int],
        observed_hwnd: Optional[int],
        *,
        phase: str,
    ) -> bool:
        """Compare a captured target HWND against an observed foreground HWND.

        wh-review-pattern-fixes.41: extracted from the post-paste check so
        the pre-send focus proof and the post-paste drift check ask the
        same question of the same helpers. ``phase`` names the caller in
        the log line ("pre-paste", "post-paste", or "selection-restore").

        Both sides go through ``normalize_hwnd_for_foreground_compare``
        (``GetAncestor(GA_ROOT)``) so a Chromium or Electron renderer
        child compares equal to its top-level frame. A normalization
        failure on either side returns False (wh-oe7u.3 fail-closed).
        When the two roots differ and the captured target belongs to a
        known Chromium-derived browser, the comparison retries through
        ``same_process_fallback_matches`` (wh-fc1x.2; raw-PID and
        exe-name probes per
        wh-ensure-focused-same-process-fallback.1.4, plus the .1.28
        pair-shape guard, which refuses when neither handle is
        invisible or WS_EX_TOOLWINDOW -- two visible plain frames of
        one browser process are a genuine second window, not a
        transient helper); every other process keeps the strict
        GA_ROOT contract.
        """
        expected_root = normalize_hwnd_for_foreground_compare(target_hwnd)
        if expected_root is None:
            logger.warning(
                "%s foreground check: target_hwnd=%s could not be "
                "root-normalized.", phase, target_hwnd,
            )
            return False
        observed_root = normalize_hwnd_for_foreground_compare(observed_hwnd)
        if observed_root is None:
            logger.warning(
                "%s foreground check: observed foreground hwnd=%s could "
                "not be root-normalized.", phase, observed_hwnd,
            )
            return False
        if expected_root == observed_root:
            return True

        # wh-fc1x.2: Chromium pages routinely produce a same-process
        # foreground difference where UIA's GetTopLevelControl HWND and
        # Win32 GetForegroundWindow return different top-level roots owned
        # by the same browser process. The keystrokes still land in the
        # focused renderer of the main HWND (the same wh-3nwy pattern that
        # VerifiedUnicodeStrategy already tolerates). Try the same-process
        # fallback, constrained to the Chromium-derived browser list so
        # non-browser apps keep the strict GA_ROOT contract.
        # wh-ensure-focused-same-process-fallback.1.4: the fallback
        # runs through same_process_fallback_matches, which never
        # normalizes. Handing the raw handles back to
        # hwnds_match_for_foreground_compare re-normalized them, and a
        # handle reused between the strict compare above and this
        # fallback (Windows recycles numeric HWNDs) could re-normalize
        # to the observed root and take that helper's strict-equality
        # return before any PID or exe probe ran.
        # wh-ensure-focused-same-process-fallback.1.34: one sample
        # resolves both the pid and the exe name, and the pid rides
        # along into the fallback as its snapshot -- a handle reused
        # by another same-exe process (a second Brave profile) between
        # this resolution and the fallback's own sampling must refuse.
        target_identity = process_identity_for_hwnd(target_hwnd)
        target_pid, target_process = (
            target_identity if target_identity is not None else (None, None)
        )
        allow_same_process = (
            target_process is not None
            and target_process in self._same_process_browser_names
        )
        same_process_match = (
            allow_same_process
            and same_process_fallback_matches(
                target_hwnd, observed_hwnd,
                expected_process_name=target_process,
                expected_pid_snapshot=target_pid,
            )
        )
        if same_process_match:
            return True
        logger.warning(
            "%s foreground check failed: expected root=%s (from hwnd=%s, "
            "process=%s), observed root=%s (from hwnd=%s, process=%s, "
            "allow_same_process=%s).",
            phase, expected_root, target_hwnd, target_process,
            observed_root, observed_hwnd,
            process_name_for_hwnd(observed_hwnd), allow_same_process,
        )
        return False

    def _prove_target_is_foreground(
        self,
        window_manager,
        hwnd: Optional[int],
        resolved_control,
        *,
        caller: str,
        target_identity: Optional[TargetIdentity] = None,
    ) -> bool:
        """Focus the captured target and prove it holds the foreground.

        wh-review-pattern-fixes.41: the three synthesis paths used to
        treat every step here as advisory. ``ensure_focused`` returns
        False when Windows denies the activation, when the foreground
        still differs after one retry, or when its Win32 work raises, and
        a discarded False meant the following Ctrl+V went to whatever
        window did hold foreground.

        The proof has three parts, and every one of them fails closed:

        1. ``hwnd`` must be present. A missing HWND means the captured
           target could not be resolved, so there is nothing to prove.
        2. ``ensure_focused`` must report True, and ``SetFocus`` on the
           captured control must not raise, except an unavailable UIA element
           whose saved identity and exact foreground are re-proven below.
        3. The foreground window must match the captured HWND through
           ``_foreground_matches_target``. This is the step that catches
           the Windows case ``ensure_focused`` cannot see on its own.

        Returns True only when all three hold. The caller must send no
        keystroke on False.
        """
        if not hwnd:
            logger.error(
                "%s: the captured target could not be resolved to an "
                "HWND; refusing to send.", caller,
            )
            return False

        if not window_manager.ensure_focused(hwnd):
            logger.error(
                "%s: ensure_focused(%s) reported failure; refusing to "
                "send into whatever window holds the foreground.",
                caller, hwnd,
            )
            return False

        if resolved_control is not None:
            try:
                resolved_control.SetFocus()
            except Exception as e:
                if captured_hwnd_survives_stale_focus(e, hwnd, target_identity):
                    logger.debug("%s: stale UIA focus; original HWND remains foreground", caller)
                    return True
                logger.error(
                    "%s: SetFocus on the captured control failed: %s; "
                    "refusing to send.", caller, e,
                )
                return False

        try:
            actual_hwnd = win32gui.GetForegroundWindow()
        except Exception as e:
            logger.error(
                "%s: GetForegroundWindow failed before the send: %s; "
                "refusing to send (fail-closed).", caller, e,
            )
            return False

        return self._foreground_matches_target(
            hwnd, actual_hwnd, phase=f"{caller} pre-send",
        )

    def prove_captured_target_is_foreground(
        self,
        window_manager,
        target_hwnd: Optional[int],
        target_control,
        *,
        caller: str,
    ) -> bool:
        """Public entry to the pre-send focus proof.

        wh-review-pattern-fixes.45: ``UIActionHandler`` must ask the same
        question ``verified_paste`` asks -- does the control the
        selection came from still hold the foreground -- before it routes
        a selection-derived replacement or sends the AI replacement
        Ctrl+V. This wrapper exists so that caller does not reach into a
        private method of another module. It adds no logic: the whole
        comparison stays in ``_prove_target_is_foreground``, which is the
        single implementation both the paste path and the handler use.

        Returns True only when the captured target holds the foreground.
        The caller must send no keystroke on False.
        """
        return self._prove_target_is_foreground(
            window_manager, target_hwnd, target_control, caller=caller,
        )

    # ========================================================================
    # VERIFIED PASTE
    # ========================================================================

    def _captured_identity_is_current(self, identity: Optional[TargetIdentity]) -> bool:
        if identity is None:
            return True
        if not identity.is_current():
            return False
        try:
            matches = self._foreground_matches_target(
                identity.root, win32gui.GetForegroundWindow(),
                phase="captured-identity",
            )
            # The browser-helper comparison can query process names. Finish
            # with native identity/focus probes after that potentially slower
            # work, immediately before the caller sends input.
            return matches and identity.is_current()
        except Exception:
            return False

    def verified_paste(
        self,
        text: str,
        window_manager,
        flutter_control=None,
        target_control=None,
        target_hwnd: Optional[int] = None,
        target_class_name: str = "",
        expected_provenance: Optional[tuple[int, int]] = None,
        target_identity: Optional[TargetIdentity] = None,
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
            expected_provenance: Optional ``(tagged_hwnd, tag)`` pair from
                the retry path (wh-ensure-focused-same-process-fallback.1.18).
                When provided, the provenance marker on ``tagged_hwnd`` is
                re-read after focus restoration, immediately before the
                Ctrl+V, and the paste refuses on any mismatch. Every
                handler-level probe runs before the strategy re-resolves its
                target and before the clipboard verify loop above the send,
                so this is the only check that covers that interval; the
                marker dies with the window object, making it the one probe
                a SAME-RUN numeric handle recycle cannot alias (cross-run,
                markers collide only on equal 43-bit salts, about 2**-43
                per pair of runs -- the accepted residual documented at
                _RUN_SALT in ui/hwnd_utils.py). The recheck also
                binds the live paste target to the tagged window (.1.20):
                it re-resolves the top-level window of ``target_control``
                (or takes ``target_hwnd`` when no control was passed) and
                refuses unless that window IS ``tagged_hwnd``, so a control
                reparented into a different live window cannot receive the
                keystroke. Non-retry callers pass None and keep the
                existing behavior.

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

        if not self._captured_identity_is_current(target_identity):
            logger.error("verified_paste: captured target identity or focus changed; refusing to send")
            return False

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
            if self._last_copy_skipped:
                # An earlier copy for this same word gave up on a held
                # clipboard, and _safe_copy logged that give-up at ERROR
                # with the utterance number.
                logger.debug(
                    "clipboard copy outcome: skipped, the retry window for "
                    "this word is spent (utterance=%s, text_len=%d), "
                    "aborting paste",
                    self.current_utterance_id, len(text),
                )
            else:
                logger.error(
                    "clipboard copy outcome: failed (utterance=%s, "
                    "text_len=%d), aborting paste",
                    self.current_utterance_id, len(text),
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
                # retry=False: this runs inside a 10 ms polling loop
                # with its own timeout, and the first copy above
                # already succeeded.
                if not self._safe_copy(text, retry=False):
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

        # The top-level window the paste goes to, for the paste check
        # (wh-dictation-paste-loss-load). Stays None on the Flutter path.
        check_root_hwnd = None

        # Restore focus before pasting
        # Flutter optimization: Skip focus restoration (already has focus)
        if not flutter_control:
            # wh-review-pattern-fixes.41: an explicit captured target is a
            # pre-send condition. The caller told this method which window
            # the text belongs to, so the focus work must be proved before
            # Ctrl+V, not reported after it.
            has_captured_target = (
                target_hwnd is not None or target_control is not None
            )
            try:
                # wh-59i32: prefer the explicit target captured by the
                # strategy. Fall back to get_target_window(None) only when
                # the caller did not supply one (legacy callers and the
                # transformer paths under specific.SelectionTransformer).
                if has_captured_target:
                    hwnd = target_hwnd
                    resolved_control = target_control
                    if hwnd is None and target_control is not None:
                        # wh-ensure-focused-same-process-fallback.1.2:
                        # route through the helper so every None path
                        # logs which lookup failed (wh-captured-target-
                        # window-lost Step 1); the inline copy this
                        # replaces swallowed the failure silently.
                        hwnd = top_level_hwnd_from_control(target_control)
                else:
                    hwnd, resolved_control = window_manager.get_target_window(None)
                check_root_hwnd = hwnd

                if has_captured_target:
                    # wh-review-pattern-fixes.41: fail closed. The return
                    # happens before ``self.last_paste_was_sent = True``
                    # below, so the flag stays False and the caller's
                    # selection-restore path stays open. That is the same
                    # rule as the wh-review-pattern-fixes.36 short-chord
                    # case: no key reached the target, so the field still
                    # holds what gather_context left there.
                    if not self._prove_target_is_foreground(
                        window_manager, hwnd, resolved_control,
                        caller="verified_paste",
                        target_identity=target_identity,
                    ):
                        return False
                else:
                    # Legacy callers pass no captured target, so this
                    # method resolves one from CURRENT focus. There is no
                    # captured intent to prove the foreground against, and
                    # the behavior below is unchanged on purpose
                    # (wh-review-pattern-fixes.41).
                    if hwnd:
                        window_manager.ensure_focused(hwnd)

                    if resolved_control:
                        try:
                            resolved_control.SetFocus()
                        except Exception as e:
                            logger.warning(
                                f"Could not re-focus control before pasting: {e}"
                            )
            except Exception as e:
                # wh-zcx9.3: surface the previously-swallowed outer
                # focus-restore failure so a future trace shows
                # 'focus restore failed -> paste requested' instead of a
                # silent verified-then-paste sequence.
                #
                # wh-review-pattern-fixes.41: with a captured target the
                # exception now ends the paste. Without one the legacy
                # fail-safe behavior stays: continue with the attempt.
                if has_captured_target:
                    logger.error(
                        "focus restore: outer exception with a captured "
                        "target (target_hwnd=%s, has_target_control=%s): "
                        "%s; refusing to paste.",
                        target_hwnd, target_control is not None, e,
                    )
                    return False
                logger.warning(
                    "focus restore: outer exception "
                    "(target_hwnd=%s, has_target_control=%s): %s",
                    target_hwnd, target_control is not None, e,
                )

        t_after_focus = time.perf_counter()

        # wh-ensure-focused-same-process-fallback.1.18 (codex round
        # 11): the retry path's provenance marker is re-read HERE --
        # after focus restoration, immediately before the Ctrl+V --
        # because every earlier probe ran before the strategy
        # re-resolved its target and before the clipboard verify loop
        # above, the longest interval on this path. The marker dies
        # with the window object, so a vanished or changed marker
        # means the tagged window was destroyed and the numeric
        # handle may now name a different window that every
        # handle-value check aliases.
        #
        # .1.20 (codex round 12): the marker alone proves the tagged
        # window OBJECT is alive; it does not prove the control the
        # keystroke lands in still belongs to that window. A control
        # reparented into another live window keeps the old window's
        # marker intact while the paste target has moved. So the
        # recheck also re-resolves the live paste target's top-level
        # window (from target_control when the caller passed one,
        # else target_hwnd itself) and requires it to BE the tagged
        # window: marker equality pins the object, and same object
        # means the same numeric handle, so a plain != compare on the
        # handle is exact. Refuse before the sent flag flips: no key
        # reached the target, so the caller's selection-restore path
        # stays open.
        if expected_provenance is not None:
            tagged_hwnd, expected_tag = expected_provenance
            live_top = (
                top_level_hwnd_from_control(target_control)
                if target_control is not None
                else target_hwnd
            )
            live_tag = read_hwnd_provenance(tagged_hwnd)
            if live_top != tagged_hwnd or live_tag != expected_tag:
                logger.error(
                    "verified_paste: identity mismatch at the pre-send "
                    "recheck (tagged_hwnd=%s live_top=%s expected=%d "
                    "live=%d); the tagged window was recycled or the "
                    "paste target moved to a different window; "
                    "refusing to paste.",
                    tagged_hwnd, live_top, expected_tag, live_tag,
                )
                return False

        use_flutter = bool(flutter_control and flutter_control.Exists(0, 0))
        if not self._captured_identity_is_current(target_identity):
            logger.error("verified_paste: captured target changed during preparation; refusing to send")
            return False

        # wh-dictation-paste-loss-load: read the target control's text
        # length before the Ctrl+V (RichEditD2DPT only), and the clipboard
        # sequence number, so the check after the Ctrl+V can tell whether
        # the word arrived and whether the clipboard still holds it.
        if use_flutter:
            paste_check, check_reason = None, "flutter"
        else:
            paste_check, check_reason = prepare_paste_check(
                check_root_hwnd, target_class_name,
            )
        if paste_check is None and check_reason != "class_out_of_scope":
            logger.info("paste check: none (%s)", check_reason)
        sequence_before_send = get_sequence_number()

        # Record that the paste keystroke is about to fire. Must sit before
        # the Flutter-vs-non-Flutter dispatch so BOTH branches flip the flag
        # true (wh-d43oi). Selection-restore reads this to decide whether a
        # False return is safe to auto-restore from.
        self.last_paste_was_sent = True

        # Execute paste (Flutter needs SendKeys, others use SendInput).
        #
        # wh-review-pattern-fixes.36: the SendInput branch sends through
        # verified_press_keys and returns False when SendInput did not
        # accept the whole chord. The old press_keys call discarded the
        # count, so a dropped Ctrl+V was credited to the retract counter
        # and reported to the strategy as a successful paste.
        #
        # wh-zcx9.1 (superseded for the SendInput branch): the log line
        # below now follows an accepted chord on that branch. On the
        # Flutter branch it still means 'requested' -- SendKeys is
        # synchronous and raises on failure, which the caller's except
        # branch handles.
        if use_flutter:
            flutter_control.SendKeys('{Ctrl}v')
            paste_path = "flutter_sendkeys"
        else:
            ok, accepted, expected = verified_press_keys('ctrl', 'v')
            if not ok:
                # A short chord can leave Ctrl physically held (down
                # accepted, up dropped) -- release it before reporting
                # (wh-eolas.2.5 shape).
                _send_modifier_keyups(('ctrl',))
                # accepted == 0 means SendInput took no event at all, so
                # nothing reached the target and the caller may safely
                # restore the selection clear_selection deleted. Any
                # accepted event leaves the target state unknown, so the
                # flag stays True and the restore path stays closed.
                self.last_paste_was_sent = accepted > 0
                logger.error(
                    "verified_paste: Ctrl+V SendInput short delivery "
                    "(sent %d/%d); reporting failure without crediting "
                    "the retract counter.",
                    accepted, expected,
                )
                return False
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
        #
        # wh-review-pattern-fixes.41: the comparison now runs through
        # ``_foreground_matches_target``, the same helper the pre-send
        # proof calls, so both phases treat Chromium roots and
        # normalization failures identically.
        if target_hwnd is not None and not flutter_control:
            try:
                actual_hwnd = win32gui.GetForegroundWindow()
            except Exception as e:
                logger.warning(
                    "Post-paste GetForegroundWindow failed: %s; refusing "
                    "to credit counter (fail-closed, wh-oe7u.3).", e,
                )
                return False
            if not self._foreground_matches_target(
                target_hwnd, actual_hwnd, phase="post-paste",
            ):
                logger.warning(
                    "Post-paste foreground check failed; skipping counter "
                    "increment.",
                )
                return False

        # wh-dictation-paste-loss-load: confirm the word arrived, and paste
        # once more when it did not. The answer is how many copies of the
        # word the target holds now.
        copies_typed = 1
        if paste_check is not None:
            copies_typed = self._confirm_paste_landed(
                paste_check, text, t_after_sendkeys, sequence_before_send,
                target_identity,
            )

        # Track characters pasted for retraction support. Routed
        # through credit_paste_chars so the counter, the grapheme-unsafe
        # flag, and the Qt-target flag stay in lockstep with
        # VerifiedUnicodeStrategy (wh-pkhrp.3.6, wh-pkhrp.2).
        # A word that did not arrive gets no credit, so "scratch that"
        # does not delete text before the utterance; a word typed twice
        # gets two.
        for _ in range(copies_typed):
            self.credit_paste_chars(text, target_class_name=target_class_name)

        if flutter_control:
            logger.info(f"[FLUTTER PASTE TIMING] copy:{(t_after_copy-t_start)*1000:.1f}ms "
                       f"verify:{(t_after_verify-t_after_copy)*1000:.1f}ms "
                       f"focus:{(t_after_focus-t_after_verify)*1000:.1f}ms "
                       f"sendkeys:{(t_after_sendkeys-t_after_focus)*1000:.1f}ms "
                       f"sleep:{(t_after_sleep-t_after_sendkeys)*1000:.1f}ms "
                       f"total:{(t_after_sleep-t_start)*1000:.1f}ms")

        return True

    def _check_window_is_foreground(self, check: PasteCheck) -> bool:
        """True when the foreground window's root is the check's window.

        wh-dictation-paste-loss-load: used when verified_paste has no
        captured target identity, so nothing else proves the foreground
        before the second Ctrl+V. A read that fails means False.

        The roots must be equal. This does not call
        ``_foreground_matches_target``: that helper logs its own WARNING,
        and the caller logs the one WARNING line for the word. Its
        same-process browser fallback cannot apply here, because the
        paste check covers RichEditD2DPT only.
        """
        try:
            foreground = win32gui.GetForegroundWindow()
        except Exception as exc:
            logger.debug("paste check: GetForegroundWindow failed: %s", exc)
            return False
        check_root = normalize_hwnd_for_foreground_compare(check.root_hwnd)
        foreground_root = normalize_hwnd_for_foreground_compare(foreground)
        logger.debug(
            "paste check: checked root=%s, foreground root=%s (from hwnd=%s)",
            check_root, foreground_root, foreground,
        )
        return check_root is not None and check_root == foreground_root

    def _confirm_paste_landed(
        self,
        check: PasteCheck,
        text: str,
        t_sent: float,
        sequence_before_send: int,
        target_identity: Optional[TargetIdentity],
    ) -> int:
        """Return how many copies of ``text`` the checked control holds now.

        wh-dictation-paste-loss-load (boss ruling 2026-10-19 21:35): wait
        for the control's text length to change after the Ctrl+V. If it
        does not change before ``paste_landing_wait`` (counted from the
        Ctrl+V), send Ctrl+V once more and wait again. Any answer this
        method cannot trust (a query with no answer, a clipboard that
        changed, a target that changed, a foreground window that is not
        the checked window when there is no captured target identity, a
        focused control that is not the watched one or cannot be read)
        means no second paste.

        Returns 1 when the word arrived or the answer is unknown (the
        behaviour before this check), 0 when it did not arrive, and 2 when
        the first Ctrl+V arrived late, after the second one was sent.
        """
        utterance = self.current_utterance_id
        wait_s = max(0.0, self.paste_landing_wait - (time.perf_counter() - t_sent))
        first = wait_for_length_change(
            check.hwnd, check.length_before, wait_s=wait_s,
        )
        since_send_ms = (time.perf_counter() - t_sent) * 1000
        if first.status == "changed":
            logger.info(
                "paste check: length changed %d -> %d, %.0f ms after Ctrl+V",
                check.length_before, first.length, since_send_ms,
            )
            return 1
        if first.status == "query_failed":
            logger.warning(
                "paste check: the target did not answer the length query "
                "after Ctrl+V (utterance=%s); no second paste.", utterance,
            )
            return 1

        if get_sequence_number() != sequence_before_send:
            logger.warning(
                "paste check: word not typed %.0f ms after Ctrl+V and the "
                "clipboard changed since the copy (utterance=%s, "
                "text_len=%d); no second paste.",
                since_send_ms, utterance, len(text),
            )
            return 0
        if not self._captured_identity_is_current(target_identity):
            logger.warning(
                "paste check: word not typed %.0f ms after Ctrl+V and the "
                "target changed (utterance=%s, text_len=%d); no second paste.",
                since_send_ms, utterance, len(text),
            )
            return 0
        if target_identity is None and not self._check_window_is_foreground(check):
            logger.warning(
                "paste check: word not typed %.0f ms after Ctrl+V and the "
                "foreground window is not the checked window, with no "
                "captured target identity to prove it otherwise "
                "(utterance=%s, text_len=%d); no second paste.",
                since_send_ms, utterance, len(text),
            )
            return 0

        # The second Ctrl+V goes to whichever control holds focus now. It
        # must still be the control whose length was watched.
        focused = focused_control_hwnd(check.root_hwnd)
        if not focused:
            logger.warning(
                "paste check: word not typed %.0f ms after Ctrl+V and the "
                "focused control could not be read (utterance=%s, "
                "text_len=%d); no second paste.",
                since_send_ms, utterance, len(text),
            )
            return 0
        if focused != check.hwnd:
            logger.warning(
                "paste check: word not typed %.0f ms after Ctrl+V and the "
                "focus moved from control %s to control %s (utterance=%s, "
                "text_len=%d); no second paste.",
                since_send_ms, check.hwnd, focused, utterance, len(text),
            )
            return 0

        logger.warning(
            "paste check: no text change %.0f ms after Ctrl+V; pasting "
            "again (utterance=%s, text_len=%d).",
            since_send_ms, utterance, len(text),
        )
        ok, accepted, expected = verified_press_keys('ctrl', 'v')
        if not ok:
            _send_modifier_keyups(('ctrl',))
            logger.warning(
                "paste check: second Ctrl+V SendInput short delivery (sent "
                "%d/%d); word not typed (utterance=%s, text_len=%d).",
                accepted, expected, utterance, len(text),
            )
            return 0
        t_second = time.perf_counter()

        second = wait_for_length_change(
            check.hwnd, check.length_before, wait_s=self.paste_landing_wait,
        )
        second_ms = (time.perf_counter() - t_second) * 1000
        if second.status == "query_failed":
            logger.warning(
                "paste check: the target did not answer the length query "
                "after the second Ctrl+V (utterance=%s).", utterance,
            )
            return 1
        if second.status == "unchanged":
            logger.warning(
                "paste check: word not typed after a second Ctrl+V "
                "(utterance=%s, text_len=%d).", utterance, len(text),
            )
            return 0
        if second.length - check.length_before >= 2 * max_growth_of_one_paste(text):
            logger.warning(
                "paste check: the first Ctrl+V arrived late; the word is "
                "typed twice (utterance=%s, length %d -> %d).",
                utterance, check.length_before, second.length,
            )
            return 2
        logger.info(
            "paste check: second Ctrl+V typed the word, length %d -> %d, "
            "%.0f ms after it.", check.length_before, second.length, second_ms,
        )
        return 1

    # ========================================================================
    # SELECTION MANAGEMENT
    # ========================================================================

    def clear_selection(self, flutter_control=None, *, target_identity=None) -> bool:
        """Clear any existing text selection before gathering context.

        Uses sentinel value to detect if selection exists, then deletes it.
        This follows the philosophy that selected text will be replaced by
        dictation, so we should delete it first to simplify processing.

        wh-t81d9.5: when a selection is detected, the captured text is
        stored on ``self.last_cleared_selection`` so a subsequent pre-send
        ``verified_paste`` failure can restore it. The slot is reset to
        None when no selection is detected so a stale value from a prior
        call does not produce a spurious restore.

        wh-review-pattern-fixes.36: both keystrokes go through
        ``verified_press_keys`` on the non-Flutter path and a short
        SendInput chord returns False. This is the difference between
        "the sentinel is unchanged because the field holds no selection"
        and "the sentinel is unchanged because the Ctrl+C never landed":
        the old press_keys call could not tell them apart, so a live
        user selection was read as no selection and the caller pasted
        over it. A short Delete is likewise no longer credited as a
        cleared selection.

        Args:
            flutter_control: Optional Flutter control (uses SendKeys instead of SendInput)

        Returns:
            bool: True if the operation succeeded. False on error, and
            on any short SendInput chord. On False the field state is
            unknown, so the caller MUST NOT paste;
            ``last_cleared_selection`` is reset to None on every failure
            path because a restore into an unknown caret position would
            put the saved text in the wrong place.
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
            use_flutter = bool(flutter_control and flutter_control.Exists(0, 0))
            if not self._captured_identity_is_current(target_identity):
                self.last_cleared_selection = None
                return False
            if use_flutter:
                flutter_control.SendKeys('{Ctrl}c')
            else:
                ok, accepted, expected = verified_press_keys('ctrl', 'c')
                if not ok:
                    _send_modifier_keyups(('ctrl',))
                    self.last_cleared_selection = None
                    logger.error(
                        "clear_selection: Ctrl+C SendInput short delivery "
                        "(sent %d/%d); the selection state is unknown, "
                        "failing closed.",
                        accepted, expected,
                    )
                    return False
            time.sleep(self.selection_clear_delay)

            selected_text = pyperclip.paste()
            if selected_text != sentinel and selected_text:
                # There was a selection. Capture it for possible restore
                # before sending Delete, then issue the Delete (wh-t81d9.5).
                self.last_cleared_selection = selected_text
                use_flutter = bool(flutter_control and flutter_control.Exists(0, 0))
                if not self._captured_identity_is_current(target_identity):
                    self.last_cleared_selection = None
                    return False
                if use_flutter:
                    flutter_control.SendKeys('{Delete}')
                else:
                    ok, accepted, expected = verified_press_keys('delete')
                    if not ok:
                        # Delete is not a modifier, but a short chord can
                        # still leave the key down and auto-repeating.
                        # Release it on the same recovery path.
                        _send_modifier_keyups(('delete',))
                        # Drop the captured text: the delete may have
                        # landed in part, so a later restore would double
                        # the user's text rather than put it back.
                        self.last_cleared_selection = None
                        logger.error(
                            "clear_selection: Delete SendInput short "
                            "delivery (sent %d/%d); the selection may "
                            "still be live, failing closed.",
                            accepted, expected,
                        )
                        return False
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
            True when the copy succeeded and the paste keystroke was
            fully accepted. False on copy failure, and (on the
            non-Flutter path) when SendInput accepted only part of the
            Ctrl+V chord (wh-review-pattern-fixes.36) -- the restore did
            not happen and the caller must not report one. False also
            when the caller supplied a captured target and this method
            could not prove that target holds the foreground before the
            Ctrl+V (wh-review-pattern-fixes.41); no key is sent in that
            case, so the saved text stays on the clipboard and reaches
            no window.
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
            # wh-review-pattern-fixes.41: an explicit captured target is a
            # pre-send condition here too, and this path carries the most
            # risk of the three. The text is the user's own saved
            # selection, so a paste into the wrong window both loses the
            # text in the original field and puts it into an unrelated
            # application.
            has_captured_target = (
                target_hwnd is not None or target_control is not None
            )
            try:
                hwnd = target_hwnd
                resolved_control = target_control
                if hwnd is None and target_control is not None:
                    # wh-ensure-focused-same-process-fallback.1.2: same
                    # unification as verified_paste above -- the helper
                    # logs every None path.
                    hwnd = top_level_hwnd_from_control(target_control)
                if has_captured_target:
                    if not self._prove_target_is_foreground(
                        window_manager, hwnd, resolved_control,
                        caller="selection restore",
                    ):
                        return False
                else:
                    # No captured target: the caller supplied neither an
                    # HWND nor a control, so there is nothing to prove the
                    # foreground against. Keep the existing behavior
                    # (wh-review-pattern-fixes.41).
                    if hwnd:
                        window_manager.ensure_focused(hwnd)
                    if resolved_control:
                        try:
                            resolved_control.SetFocus()
                        except Exception as e:
                            logger.warning(
                                "Could not re-focus control during "
                                f"selection restore: {e}"
                            )
            except Exception as e:
                # wh-review-pattern-fixes.41: this handler was
                # ``except Exception: pass``. With a captured target the
                # restore now fails closed, which matches the documented
                # return contract below: False means the selection was
                # not restored.
                if has_captured_target:
                    logger.error(
                        "Selection restore: focus restore raised "
                        "(target_hwnd=%s, has_target_control=%s): %s; "
                        "the selection was not restored.",
                        target_hwnd, target_control is not None, e,
                    )
                    return False
                logger.warning(
                    "Selection restore: focus restore raised with no "
                    "captured target: %s", e,
                )

        if flutter_control and flutter_control.Exists(0, 0):
            flutter_control.SendKeys('{Ctrl}v')
        else:
            ok, accepted, expected = verified_press_keys('ctrl', 'v')
            if not ok:
                _send_modifier_keyups(('ctrl',))
                logger.error(
                    "Selection restore: Ctrl+V SendInput short delivery "
                    "(sent %d/%d); the selection was not restored.",
                    accepted, expected,
                )
                return False

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

        pasted = False
        try:
            pasted = self._raw_paste(
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

        if pasted:
            logger.info(
                "Restored cleared selection (%d chars) after pre-send paste failure",
                len(text),
            )
        else:
            # wh-review-pattern-fixes.36: _raw_paste now reports a
            # failed copy or a short Ctrl+V chord. The return value
            # still means "a restore was attempted" (the caller has no
            # second recovery to try), but the log must not claim the
            # user's text is back.
            logger.error(
                "Selection restore attempt failed (%d chars); the "
                "cleared text was not put back.",
                len(text),
            )
        return True

    # ========================================================================
    # CONTEXT GATHERING
    # ========================================================================

    def gather_context(self, flutter_control=None, *, target_identity=None) -> Dict[str, Any]:
        """Gather text context using clipboard and arrow-key navigation.

        [WORKAROUND] For non-UIA-cooperating elements. Uses arrow keys to
        select text before/after cursor, copies it, then returns cursor to
        original position.

        Args:
            flutter_control: Optional Flutter control (uses SendKeys instead of SendInput)

        wh-review-pattern-fixes.36: every non-Flutter key send here is a
        state-changing one -- the two Shift+arrow selections, the two
        copies, and the two caret-restoring arrows. They went through
        press_keys, which discards the SendInput count, so a dropped
        keystroke produced an empty context that looked exactly like a
        genuine start-of-field. Worse, the arrow sequence is only
        balanced when every arrow lands: a dropped Right leaves the
        two-character selection live, and the caller's paste then
        overwrites the user's text. Each send is now verified and the
        method stops at the first short chord and reports
        ``delivery_failed``. The caret is left wherever the failure
        found it -- this method cannot know where that is, which is
        exactly why the caller must abort instead of paste.

        wh-review-pattern-fixes.39: the ``except`` branch reports
        ``delivery_failed`` as well. Each probe reads the clipboard
        AFTER its Shift+arrow selection and its Ctrl+C have landed and
        BEFORE the arrow that collapses that selection, so a raise from
        the clipboard read (or from the sequence-number call, or from a
        Flutter SendKeys) leaves two characters selected. The old
        ``delivery_failed=False`` on that branch told the caller to
        paste over them.

        Returns:
            dict: Context with keys:
                - 'preceding_chars': Text before cursor (up to 2 chars)
                - 'has_selection': Always False (selection is cleared before this)
                - 'delivery_failed': True when a SendInput chord was
                  short, and when any exception escaped the probe. The
                  context values are then meaningless and the caret
                  position is unknown; the caller MUST NOT paste.

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

        def _verified_send(*keys: str) -> bool:
            """Send one chord and report whether SendInput took it all.

            On a short chord, release the keys (a chord that delivered
            the down event but not the up leaves the key physically
            held, wh-eolas.2.5) and log the count. The caller stops at
            the first False.
            """
            if not self._captured_identity_is_current(target_identity):
                return False
            ok, accepted, expected = verified_press_keys(*keys)
            if not ok:
                _send_modifier_keyups(keys)
                logger.error(
                    "gather_context: %s SendInput short delivery "
                    "(sent %d/%d); the caret and selection state are "
                    "unknown, failing closed.",
                    keys, accepted, expected,
                )
            return ok

        def _flutter_send(keys: str) -> None:
            if not self._captured_identity_is_current(target_identity):
                raise RuntimeError("captured target changed before context probe")
            flutter_control.SendKeys(keys)

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
                _flutter_send('{Shift}{Left}')
                _flutter_send('{Shift}{Left}')
            else:
                if not _verified_send('shift', 'left', 'left'):
                    return _DELIVERY_FAILED_CONTEXT()

            seq_before = get_sequence_number()
            if use_flutter:
                _flutter_send('{Ctrl}c')
                time.sleep(self.clipboard_operation_delay)
            else:
                if not _verified_send('ctrl', 'c'):
                    return _DELIVERY_FAILED_CONTEXT()
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
                    _flutter_send('{Right}')
                else:
                    if not _verified_send('right'):
                        return _DELIVERY_FAILED_CONTEXT()
                time.sleep(0.001)  # CRITICAL: Brief yield for cursor positioning

            # 2. Get text AFTER the cursor (1 char).
            # wh-fz7j.4: route through _safe_copy for seq tracking.
            sentinel_after = f"__SENTINEL_A_{time.time()}__"
            if not self._safe_copy(sentinel_after):
                raise RuntimeError("clipboard copy failed (sentinel_after)")

            # Select 1 char to the right
            if use_flutter:
                _flutter_send('{Shift}{Right}')
            else:
                if not _verified_send('shift', 'right'):
                    return _DELIVERY_FAILED_CONTEXT()

            seq_after = get_sequence_number()
            if use_flutter:
                _flutter_send('{Ctrl}c')
                time.sleep(self.clipboard_operation_delay)
            else:
                if not _verified_send('ctrl', 'c'):
                    return _DELIVERY_FAILED_CONTEXT()
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
                    _flutter_send('{Left}')
                else:
                    if not _verified_send('left'):
                        return _DELIVERY_FAILED_CONTEXT()
                time.sleep(0.001)  # CRITICAL: Brief yield for cursor positioning

        except Exception as e:
            # wh-review-pattern-fixes.39: fail closed on every exception,
            # exactly as the short-chord branches above do. The old code
            # returned delivery_failed=False here, which told the caller
            # the empty context was a genuine start-of-field reading.
            #
            # Most of this try block runs with a live selection. The
            # clipboard read at the end of each probe happens AFTER its
            # Shift+arrow selection and its Ctrl+C have both been
            # delivered, and the balancing arrow that collapses the
            # selection comes after that read. So a raise from
            # pyperclip.paste, from get_sequence_number, from
            # wait_for_clipboard_write, or from a Flutter SendKeys call
            # leaves two characters selected and the caller's paste
            # replaced them.
            #
            # The exceptions that can fire before any key is sent (the
            # sentinel copy, the flutter_control.Exists probe) fail
            # closed too. They leave the caret alone, but they also mean
            # the probe never observed the field: the empty
            # preceding_chars is a fabrication, not a reading, and the
            # caller would perfect the dictated text against it.
            logger.error(
                "Error during clipboard context gathering: %s; the caret "
                "and selection state are unknown, failing closed.", e,
            )
            return _DELIVERY_FAILED_CONTEXT()

        # Return context in the same format as ShadowBufferManager
        return {
            'preceding_chars': before_text,
            'has_selection': False,  # This method doesn't detect selections
            'delivery_failed': False,
        }
