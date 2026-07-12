"""Concrete insertion strategies.

Retraction accounting on Qt-backed targets (wh-pkhrp design constraint B).
Every strategy that credits retraction (StandardStrategy/
ShadowBufferStrategy/VerifiedUnicodeStrategy/SimplePasteStrategy)
calls ``ClipboardOperations.credit_paste_chars(text,
target_class_name=context.class_name)``. The helper updates two
parallel counters on every successful paste:

  * ``accumulated_paste_chars`` advances by ``len(perfected_text)``
    -- Python UTF-16 code-unit count.
  * ``accumulated_paste_clusters`` advances by the grapheme cluster
    count of the same text.

For ASCII and most BMP text the two counters agree. They disagree on
surrogate-pair emoji and ZWJ family glyphs:

  * Python ``len()`` counts UTF-16 code units (2 per surrogate pair,
    multiple per ZWJ family glyph).
  * The grapheme cluster count counts user-perceived characters
    (1 per surrogate pair, 1 per whole ZWJ family glyph).
  * Qt's backspace deletes one grapheme cluster at a time (the whole
    ZWJ family in one keystroke); other apps and the SendInput
    keystroke stream operate at the code-unit level.

The helper also sets a sticky ``accumulated_paste_was_qt`` flag when
the paste's target class name matches the Qt prefix convention
(``ClipboardOperations.is_qt_class_name``). The flag stays True for
the rest of the utterance once any paste lands in a Qt-backed
target.

The retract path in ``UIActionHandler._handle_retract_last_utterance``
branches on that flag. Qt-backed utterances send
``accumulated_paste_clusters`` backspaces (one per Qt grapheme
deletion); everything else sends ``accumulated_paste_chars``
backspaces (one per SendInput code unit). The earlier
``qt_grapheme_unsafe`` fail-closed gate that blocked retract when
the inserted text contained a surrogate pair or ZWJ joiner is gone
-- the parallel cluster counter is the protection. The
``accumulated_has_grapheme_unsafe`` flag stays in place but is now
informational only (structured logging on retract).

Tests in ``tests/test_phase3_wiring.py`` cover both the BMP-only
happy path (where the two counters agree) and the surrogate-pair /
ZWJ family path (where they disagree and the Qt branch selects the
cluster count).

Concurrent mutation (wh-pkhrp design constraint D).
Phase 1's stress test recorded 80% None reads under simultaneous Qt
appends + UIA reads + SendInput keystrokes. The report concluded
this was probably a probe-design artifact: the probe's SendInput
keystrokes landed on whatever Windows had foreground, not
necessarily the QPlainTextEdit. The production strategy path runs
SendInput against a captured target HWND and the post-send
foreground check refuses to credit the counter on a mismatch.
There is no production scenario where the strategy issues
concurrent SendInput against the editor (the keystroke flow is
single-threaded from the input process), and the editor does not
itself append text concurrently with dictation -- the GUI's append
runs only when an explicit append IPC arrives. Phase 3 therefore
does not introduce additional concurrent-mutation defences. See
docs/design/benchmarks/2026-05-02-155250-qpte-uia-fidelity.md
section Follow-up paragraph 4 for the full analysis.
"""
import gc
import logging
import threading
import time
from typing import Optional
import uiautomation as auto
import win32gui

# wh-trailing-corruption-phase2: psutil is best-effort. If the package
# is not available in the Input process venv we still log the rest of
# the cold-state snapshot; only the working-set memory line is skipped.
try:
    import psutil
    _process = psutil.Process()
except Exception:
    _process = None
from .base import InsertionMode, InsertionOptions, InsertionResult, InsertionStrategy
from ..context import UIContext
from shared.rejection_category import (
    categorize_rejection,
    should_show_try_anyway,
)
from ui.uia_text_reader import read_context_via_text_pattern
from ui.hwnd_utils import (
    _FALLBACK_SAME_PROCESS_BROWSER_NAMES,
    hwnds_match_for_foreground_compare,
    normalize_hwnd_for_foreground_compare,
    process_name_for_hwnd,
)
from utils.win_input_sender import snapshot_modifier_state, type_string_verified
from utils.redact import redact_transcript

logger = logging.getLogger(__name__)


# wh-trailing-corruption-instrument: number of per-strategy-instance
# dispatches that are logged at INFO before falling through to DEBUG.
# Keeps the wh-startup-trailing-corruption hypothesis (corruption near
# process startup) visible in the default log without flooding long
# dictation sessions with INFO records.
DISPATCH_INFO_LOG_LIMIT = 5

# wh-trailing-corruption-phase2: cap the expensive post-send UIA
# readback to the very start of every session. The user reports that
# the corruption usually shows up in the first ~10 dictated words after
# WheelHouse starts; the 2026-05-21 reproduction that hit dispatch 33
# was an unusually long warm-up. Keep the limit small so the slow-
# dictation cost is bounded to the first sentence or so of each
# session, after which the readback turns off and dictation speed
# returns to normal.
POST_SEND_READBACK_DISPATCH_LIMIT = 10

# wh-trailing-corruption-phase2: module import time as a stand-in for
# Input process start. The Input process imports this module once at
# startup, so the elapsed-since-import value tracks process age within
# a few hundred milliseconds. Logged alongside each dispatch so the
# corruption ordinal range can be aligned with wall-clock cold time.
_MODULE_IMPORT_TIME = time.monotonic()


def _snapshot_cold_state() -> str:
    """Return a compact string of cold-path indicators for the dispatch log.

    wh-trailing-corruption-phase2: when the next cold-start reproduction
    of wh-startup-trailing-corruption lands, each VerifiedUnicodeStrategy
    dispatch log line carries this snapshot so the broken ordinal range
    can be correlated with: time since Input process started, current
    garbage-collector counts (which generations have run), the number of
    live threads, and the working-set memory. A correlation between any
    of these and the corrupt-vs-clean boundary narrows the warmup gate
    candidate set.

    The helper is defensive: every probe is wrapped so a diagnostic log
    line cannot crash the dispatch path.
    """
    elapsed_s = time.monotonic() - _MODULE_IMPORT_TIME
    try:
        gc0, gc1, gc2 = gc.get_count()
        gc_str = f"{gc0}/{gc1}/{gc2}"
    except Exception:
        gc_str = "?"
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = -1
    if _process is not None:
        try:
            rss_mb = _process.memory_info().rss / (1024 * 1024)
            rss_str = f"{rss_mb:.1f}MB"
        except Exception:
            rss_str = "?"
    else:
        rss_str = "n/a"
    return (
        f"age={elapsed_s:.1f}s gc={gc_str} threads={thread_count} rss={rss_str}"
    )


def _resolve_options(options: Optional[InsertionOptions]) -> InsertionOptions:
    """Default helper: turn None into the default InsertionOptions.

    Strategies all accept ``options=None`` for backwards compatibility
    with callers that have not been updated; pull the resolved options
    object through this helper so the rest of the strategy body can read
    ``options.mode`` without a None check.
    """
    return options if options is not None else InsertionOptions()


def _hwnd_from_control(focused_control) -> Optional[int]:
    """Extract the top-level HWND from a UIA control, or None on any failure.

    wh-59i32: helper used by the standard/clipboard strategies to capture
    the target HWND at strategy entry so the paste targets the field that
    had focus at capture time, not whatever has focus when the paste
    actually fires.

    wh-oe7u.3: result is root-normalized via
    ``GetAncestor(GA_ROOT)`` so Chromium and Electron applications --
    where UIA's ``GetTopLevelControl().NativeWindowHandle`` can be a
    renderer child of the actual top-level frame -- compare equal to
    ``GetForegroundWindow()`` in the verified_paste post-paste check.
    Without this, Chromium dictation paths classified successful pastes
    as focus drift and skipped retract accounting.
    """
    if not focused_control:
        return None
    try:
        top = focused_control.GetTopLevelControl()
        hwnd = top.NativeWindowHandle if top else None
    except Exception as e:
        logger.debug("Could not resolve target HWND from focused_control: %s", e)
        return None
    if not hwnd:
        return None
    return normalize_hwnd_for_foreground_compare(int(hwnd))

# ============================================================================
# HELPER STRATEGIES (Internal use)
# ============================================================================

class ShadowBufferStrategy(InsertionStrategy):
    """Fast path using cached UIA state from shadow buffer."""

    def __init__(self, buffer_manager, text_perfector, clipboard_ops, window_manager):
        self.buffer_manager = buffer_manager
        self.text_perfector = text_perfector
        self.clipboard = clipboard_ops
        self.window_manager = window_manager

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        """Insert text using shadow buffer for context.

        :flow: Shadow Buffer Text Insertion
        :step: 1
        :consumes_from: Text Insertion Strategy Selection
        :produces_for: Clipboard-Based Text Insertion
        :description: Fast-path insertion using cached text context from shadow buffer.
            Flutter-aware: passes Flutter flag to clipboard operations for correct API
            selection. Bypasses slow context gathering on subsequent words in utterance.
        :data_in: Insertion string, Flutter flag, focused control
        :data_out: Insertion success status

        Always returns clipboard_dirty=True even on the pre-send buffer
        sync failure path because the caller (StandardStrategy) interprets
        a clipboard_dirty=False result as "no clipboard write happened" and
        skips end_utterance restoration. The pre-send branch never wrote
        the clipboard, so strictly speaking it could return False here, but
        keeping it True is the safe default: the only path this matters
        for is utterance-end restore, and an unnecessary restore is a
        no-op while a missed restore clobbers the user's clipboard.

        wh-iti5: in VERBATIM mode, paste the insertion_string exactly. No
        TextPerfector pass, no buffer sync (caller already has the final
        text), no preceding-context lookup. Update the shadow buffer with
        the verbatim string so subsequent dictation in the same utterance
        sees the right preceding context.
        """
        opts = _resolve_options(options)

        if opts.mode is InsertionMode.VERBATIM:
            # Verbatim mode: deliver the provided text exactly. We still
            # need a target HWND for the post-paste foreground check, but
            # we skip the buffer sync gate -- the caller already composed
            # the final text and does not need preceding-context for it.
            target_hwnd = _hwnd_from_control(context.focused_control)
            success = self.clipboard.verified_paste(
                insertion_string,
                self.window_manager,
                context.focused_control if context.is_flutter else None,
                target_control=context.focused_control,
                target_hwnd=target_hwnd,
                target_class_name=context.class_name,
            )
            if success:
                # Update the shadow buffer if it is valid; otherwise leave
                # it invalid so the next dictation re-syncs. Verbatim
                # delivery may have replaced a selection or jumped the
                # cursor in ways the buffer cannot reconstruct from the
                # inserted string alone.
                if self.buffer_manager.is_valid:
                    self.buffer_manager.update_after_insertion(insertion_string)
            return InsertionResult(success=success, clipboard_dirty=True)

        # DICTATION mode (default): existing behavior.
        # Validate/synchronize buffer
        if not self.buffer_manager.is_valid:
            if not self.buffer_manager.synchronize():
                logger.warning("Buffer synchronization failed, falling back")
                return InsertionResult(success=False, clipboard_dirty=False)

        # Get context and perfect the string
        buffer_context = self.buffer_manager.get_context()
        final_string = self.text_perfector.perfected_string(
            insertion_string,
            **buffer_context
        )

        # wh-59i32: capture HWND from the focused control at strategy entry
        # so the paste targets the field that had focus when capture_context
        # ran, not whatever has focus now (focus can drift to a popup or
        # focus-stealing app between capture and paste).
        target_hwnd = _hwnd_from_control(context.focused_control)

        # Paste and update buffer
        # Use context.is_flutter to determine if we need special handling in verified_paste
        success = self.clipboard.verified_paste(
            final_string,
            self.window_manager,
            context.focused_control if context.is_flutter else None,
            target_control=context.focused_control,
            target_hwnd=target_hwnd,
            target_class_name=context.class_name,
        )

        if success:
            self.buffer_manager.update_after_insertion(final_string)

        # verified_paste always writes the clipboard before sending Ctrl+V,
        # so clipboard_dirty=True regardless of success.
        return InsertionResult(success=success, clipboard_dirty=True)


class ClipboardFallbackStrategy(InsertionStrategy):
    """Slow path using clipboard and arrow keys for context gathering."""

    def __init__(self, buffer_manager, text_perfector, clipboard_ops, window_manager,
                 *, text_target_predicate=None):
        self.buffer_manager = buffer_manager
        self.text_perfector = text_perfector
        self.clipboard = clipboard_ops
        self.window_manager = window_manager
        # wh-zndq slow-path preflight (wh-ix1z.9): when wired, the
        # predicate re-evaluates against the current focused control
        # before any destructive action (clear_selection, gather_context,
        # verified_paste). Skipped silently when None for back-compat
        # with older test fixtures.
        self.text_target_predicate = text_target_predicate

    def _slow_path_preflight(
        self, context: UIContext,
    ) -> tuple[Optional[InsertionResult], Optional[int]]:
        """Re-evaluate the text-target predicate against current focus.

        Returns a tuple ``(rejection, validated_hwnd)``:

        - ``rejection`` is the InsertionResult to return immediately
          when (a) the captured or current top-level HWND cannot be
          resolved, (b) the two normalized HWNDs differ, or (c) the
          predicate rejects the freshly recaptured focus. None when
          the slow path should proceed.
        - ``validated_hwnd`` is the normalized top-level HWND of the
          captured target when the preflight accepted, None otherwise
          and None when no predicate is wired (legacy fixtures). The
          slow-path caller forwards this HWND to ``verified_paste`` so
          the same value the preflight validated is what the post-paste
          foreground check compares against (wh-ix1z.17). Without this
          plumbing, the late ``_hwnd_from_control(context.focused_control)``
          lookup right before verified_paste could observe a stale
          control and pass ``target_hwnd=None``, which makes
          ClipboardOperations.verified_paste skip the post-paste
          foreground check -- reopening the fail-open shape that
          wh-ix1z.14 closed.

        Three checks run together (wh-ix1z.9, wh-ix1z.11, wh-ix1z.13,
        wh-ix1z.14):

        1. HWND resolution. Both the captured ``context.focused_control``
           and the freshly recaptured current focus are passed through
           ``_hwnd_from_control`` -- the same helper the rest of this
           module uses for foreground / paste checks. That helper
           routes through ``normalize_hwnd_for_foreground_compare``
           (GetAncestor(GA_ROOT)) so Chromium and Electron renderer-child
           HWNDs normalize to the same root as the actual top-level
           frame. ``None`` from either side means the comparison cannot
           be made; the slow path fails closed with a stale-focus
           rejection rather than blundering into a non-text target.

        2. HWND comparison. With both HWNDs resolved and root-normalized,
           a mismatch means the user moved focus across windows during
           the slow path. Reject so the eventual ``verified_paste``
           does not target a different window than the one the predicate
           accepted. Same-window focus changes between two controls are
           still caught by the per-control predicate check (#3) and the
           existing post-paste foreground check.

        3. Predicate evaluation. Re-run the text-target predicate
           against the freshly captured focus. The predicate uses
           ``focused_control.ClassName`` exclusively (no fallback to
           ``context.class_name``) so a freshly recaptured control
           with an empty ClassName cannot inherit the original
           capture's class name and falsely match an allow / deny
           list. The preflight passes ``class_name=""`` to the
           predicate so the verdict's telemetry class field reflects
           the recaptured control only, not the captured-context
           class (wh-ix1z.15).

        All UIA work runs inside ``UIAutomationInitializerInThread`` to
        match the existing pattern in shadow_buffer.synchronize and
        uia_text_reader.read_context_via_text_pattern. Property reads
        and GetPattern calls outside the initializer block can fail
        with COM-uninitialized errors on the slow-path thread
        (wh-ix1z.12).

        Skipped on Flutter targets. FlutterStrategy reaches this code
        path via inheritance from StandardStrategy, but the router-level
        predicate skips Flutter for the same reason -- Flutter widgets
        commonly do not expose UIA TextPattern, so the generic predicate
        would reject Flutter targets that the per-framework
        FlutterStrategy is built to handle. Mirror that short-circuit
        here so Flutter dictation does not silently fail in the slow
        path.

        Skipped when no predicate is wired (legacy test fixtures).
        """
        if self.text_target_predicate is None:
            return None, None
        if getattr(context, "is_flutter", False):
            return None, None

        stale_rejection = InsertionResult(
            success=True,
            clipboard_dirty=False,
            rejected_reason="stale_focus_changed_to_non_text",
        )

        try:
            with auto.UIAutomationInitializerInThread(debug=False):
                current = auto.GetFocusedControl()
                # Resolve normalized top-level HWNDs for both the
                # captured target and the freshly recaptured focus.
                # _hwnd_from_control already routes through
                # normalize_hwnd_for_foreground_compare so Chromium /
                # Electron renderer-child HWNDs collapse to their root
                # frame; comparing the raw NativeWindowHandle values
                # would otherwise misclassify two controls under one
                # browser window as a cross-window change (wh-ix1z.13).
                original_hwnd = _hwnd_from_control(context.focused_control)
                current_hwnd = _hwnd_from_control(current)
                # Either resolution failure means the safety check
                # cannot be made -- fail closed (wh-ix1z.14). The
                # alternative (defer to predicate verdict) re-opened
                # the wh-ix1z.11 class of bug on the failure path.
                if original_hwnd is None or current_hwnd is None:
                    logger.debug(
                        "Slow-path preflight: HWND resolution failed -- "
                        "original=%r current=%r; failing closed.",
                        original_hwnd, current_hwnd,
                    )
                    return stale_rejection, None
                if current_hwnd != original_hwnd:
                    logger.debug(
                        "Slow-path preflight: focus moved across windows "
                        "(original_hwnd=%s, current_hwnd=%s)",
                        original_hwnd, current_hwnd,
                    )
                    return stale_rejection, None
                # Predicate evaluation MUST stay inside the initializer
                # block. The predicate's property reads and GetPattern
                # calls require COM initialized on the calling thread.
                # class_name is intentionally empty: the original
                # context's class belongs to the captured control, not
                # the freshly recaptured one (wh-ix1z.15).
                verdict = self.text_target_predicate.evaluate(
                    current,
                    class_name="",
                    process_name=getattr(context, "process_name", "") or "",
                )
        except Exception as e:
            # Any UIA failure is treated as stale -- fail closed so the
            # slow path does not blunder into a non-text target on a
            # COM error.
            logger.debug("Slow-path preflight: UIA failure: %s", e)
            return stale_rejection, None

        if verdict.verdict:
            return None, original_hwnd
        logger.debug(
            "ClipboardFallbackStrategy: slow-path preflight rejected -- "
            "reason=%s control_type=%s class=%s process=%s",
            verdict.reason, verdict.control_type or "?",
            verdict.class_name or "?", verdict.process_name or "?",
        )
        return stale_rejection, None

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        """Insert text with TextPattern fast path, clipboard fallback.

        :flow: Clipboard-Based Text Insertion
        :step: 1
        :consumes_from: Text Insertion Strategy Selection
        :description: Tries UIA TextPattern for context (~400us), falls back to
            clipboard gather_context (~56-139ms) when unavailable.
        :data_in: Insertion string, Flutter flag, focused control
        :data_out: Insertion success status

        Always reports clipboard_dirty=True. Both the TextPattern and the
        clipboard-gather paths end with verified_paste, which writes the
        clipboard before sending Ctrl+V. The exception branch may not have
        reached the paste call, but defaults to clipboard_dirty=True for
        the same safety reason as ShadowBufferStrategy.

        wh-iti5: in VERBATIM mode, skip both context-gathering paths and
        paste the insertion_string exactly. The caller already has the
        final text and we owe them no perfecting work or context probe.
        """
        opts = _resolve_options(options)

        # wh-ix1z.9 / wh-ix1z.17: slow-path preflight. Verbatim and
        # dictation modes both need the stale-target check before any
        # clipboard write. The validated_hwnd is the normalized
        # top-level HWND of the captured target -- forward it to
        # verified_paste below so the post-paste foreground check
        # compares against the same value the preflight validated. The
        # late _hwnd_from_control(context.focused_control) lookup that
        # was here previously could see a stale captured control and
        # pass target_hwnd=None, which would make verified_paste skip
        # its own foreground check (the wh-ix1z.17 fail-open shape).
        preflight, validated_hwnd = self._slow_path_preflight(context)
        if preflight is not None:
            return preflight

        if opts.mode is InsertionMode.VERBATIM:
            # wh-ksde.1: clear any leftover ``last_cleared_selection`` from a
            # prior dictation slow-path call before doing anything else.
            # The wh-t81d9.5 restore contract is per-call: a strategy may
            # only restore a selection it cleared in this call. The verbatim
            # path skips clear_selection entirely so it has nothing to
            # restore. Resetting at entry ensures a stale value cannot
            # survive into a later restore decision.
            self.clipboard.last_cleared_selection = None
            try:
                # wh-ix1z.17: prefer the HWND the preflight already
                # validated. The captured control may have gone stale
                # by now; validated_hwnd carries the value the
                # post-paste foreground check is supposed to compare
                # against. Falls back to a fresh lookup when no
                # predicate is wired (legacy path, validated_hwnd is
                # None).
                target_hwnd = (
                    validated_hwnd
                    if validated_hwnd is not None
                    else _hwnd_from_control(context.focused_control)
                )
                success = self.clipboard.verified_paste(
                    insertion_string,
                    self.window_manager,
                    context.focused_control if context.is_flutter else None,
                    target_control=context.focused_control,
                    target_hwnd=target_hwnd,
                    target_class_name=context.class_name,
                )
                # No restore on failure: this branch never called
                # clear_selection, so there is nothing call-local to put
                # back. Calling restore_cleared_selection here could raw
                # paste an unrelated stale selection from a prior call
                # into the current target.
                return InsertionResult(success=success, clipboard_dirty=True)
            except Exception as e:
                logger.error("Clipboard fallback strategy (verbatim) failed: %s", e)
                return InsertionResult(success=False, clipboard_dirty=True)

        try:
            t_start = time.perf_counter()

            # Fast path: try UIA TextPattern for context (skip for Flutter)
            uia_context = None
            if not context.is_flutter:
                uia_context = read_context_via_text_pattern()

            if uia_context is not None:
                # TextPattern succeeded -- skip clipboard context gathering
                t_context = time.perf_counter()
                logger.info(
                    "Context via TextPattern (%.1fms): preceding='%s', selection=%s",
                    (t_context - t_start) * 1000,
                    redact_transcript(uia_context['preceding_chars']),
                    uia_context['has_selection'],
                )
                preceding = uia_context.get('preceding_chars', '')
                final_string = self.text_perfector.perfected_string(
                    insertion_string,
                    preceding_chars=preceding,
                    has_selection=uia_context.get('has_selection', False),
                )
            else:
                # Slow path: clipboard-based context gathering
                self.clipboard.clear_selection(
                    context.focused_control if context.is_flutter else None
                )
                clipboard_context = self.clipboard.gather_context(
                    context.focused_control if context.is_flutter else None
                )
                t_context = time.perf_counter()
                preceding = clipboard_context.get('preceding_chars', '')
                logger.info(
                    "Context via clipboard (%.1fms): preceding='%s'",
                    (t_context - t_start) * 1000,
                    redact_transcript(preceding),
                )
                final_string = self.text_perfector.perfected_string(
                    insertion_string,
                    preceding_chars=preceding,
                    has_selection=False,
                )

            # wh-59i32: forward the target HWND captured at strategy
            # entry so the paste targets the field captured by context,
            # not whatever has focus now. wh-ix1z.17: prefer the HWND
            # the preflight already validated -- the captured control
            # may have gone stale during the slow context-gather work
            # above and a late lookup that returns None would make
            # verified_paste skip its post-paste foreground check.
            target_hwnd = (
                validated_hwnd
                if validated_hwnd is not None
                else _hwnd_from_control(context.focused_control)
            )

            # Paste (same for both paths)
            success = self.clipboard.verified_paste(
                final_string,
                self.window_manager,
                context.focused_control if context.is_flutter else None,
                target_control=context.focused_control,
                target_hwnd=target_hwnd,
                target_class_name=context.class_name,
            )

            if success:
                self._update_shadow_buffer_from_context(preceding, final_string)
                # wh-t81d9.5: clear any captured selection on success so
                # the value does not linger across calls and produce a
                # spurious restore on a later unrelated failure.
                self.clipboard.last_cleared_selection = None
            else:
                # wh-t81d9.5: auto-restore the cleared selection only on
                # a clean pre-send failure (last_paste_was_sent is False).
                # If the keystroke fired (post-send failure) we cannot
                # safely raw-paste the saved selection on top of unknown
                # inserted text. Ctrl+Z is the user's recovery for that
                # case.
                if not self.clipboard.last_paste_was_sent:
                    self.clipboard.restore_cleared_selection(
                        self.window_manager,
                        target_control=context.focused_control,
                        target_hwnd=target_hwnd,
                        flutter_control=context.focused_control if context.is_flutter else None,
                    )

            return InsertionResult(success=success, clipboard_dirty=True)

        except Exception as e:
            # wh-t81d9.5: do NOT auto-restore in this branch. If the
            # exception came out of gather_context's arrow-key sequence,
            # the caret is in an indeterminate position and a raw paste
            # would land the saved selection at the wrong location. The
            # selection is lost; Ctrl+Z is the user's manual recovery.
            logger.error("Clipboard fallback strategy failed: %s", e)
            return InsertionResult(success=False, clipboard_dirty=True)

    def _update_shadow_buffer_from_context(self, preceding_chars: str, inserted_text: str) -> None:
        try:
            cursor_pos = len(preceding_chars) + len(inserted_text)
            reconstructed_buffer = preceding_chars + inserted_text
            self.buffer_manager.update_from_clipboard_data(
                reconstructed_buffer,
                cursor_pos,
                0
            )
        except Exception as e:
            logger.error(f"Failed to update shadow buffer: {e}")


# ============================================================================
# CONCRETE STRATEGIES (Public)
# ============================================================================

class StandardStrategy(InsertionStrategy):
    """Standard dictation strategy with graduated fallback (ShadowBuffer -> Clipboard)."""

    def __init__(self, buffer_manager, text_perfector, clipboard_ops, window_manager,
                 *, text_target_predicate=None):
        self.clipboard = clipboard_ops
        self.shadow_strategy = ShadowBufferStrategy(buffer_manager, text_perfector, clipboard_ops, window_manager)
        # wh-ix1z.9: forward the predicate so the ClipboardFallback path
        # can re-evaluate against current focus before any clipboard write.
        self.clipboard_strategy = ClipboardFallbackStrategy(
            buffer_manager, text_perfector, clipboard_ops, window_manager,
            text_target_predicate=text_target_predicate,
        )

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        # Reset last_paste_was_sent at entry so the wh-bte fallback gate
        # below only reflects state from THIS call's shadow attempt.
        # Without this reset the flag carries over from a prior call's
        # successful paste; if a later call's shadow_strategy fails before
        # reaching verified_paste (e.g. ShadowBufferManager.synchronize()
        # fails), the gate would wrongly skip the clipboard fallback and
        # the dictated word would never paste. ClipboardOperations.verified_paste
        # itself resets the flag at its own entry, so when shadow_strategy
        # does call verified_paste this assignment is a no-op; when shadow
        # never reaches verified_paste, the assignment clears stale
        # inter-call pollution.
        self.clipboard.last_paste_was_sent = False

        # Try shadow buffer first. Options propagate so VERBATIM mode
        # reaches both inner strategies.
        shadow_result = self.shadow_strategy.insert(insertion_string, context, request_id, options)
        if shadow_result.success:
            return shadow_result

        # wh-bte: ShadowBufferStrategy returned False. Check whether the
        # Ctrl+V keystroke already fired before falling back. The
        # post-paste foreground check in verified_paste returns False
        # AFTER the keystroke when the captured target_hwnd does not
        # match the foreground window -- common in Chromium browsers
        # where the captured HWND is a renderer child while
        # GetForegroundWindow returns the top-level Chrome_WidgetWin_1
        # frame. Falling back to ClipboardFallbackStrategy in that case
        # double-pastes the same word. Read last_paste_was_sent before
        # the fallback's verified_paste call resets it.
        if self.clipboard.last_paste_was_sent:
            logger.warning(
                "Shadow buffer reported failure after Ctrl+V already fired "
                "(post-send failure, e.g. focus drift); skipping clipboard "
                "fallback to avoid double paste"
            )
            # Pass shadow_result through so clipboard_dirty reflects whether
            # the shadow path actually wrote the clipboard.
            return shadow_result

        # Pre-send failure (copy or verification failed before keystroke).
        # The fallback's clipboard path is the legitimate recovery.
        logger.warning("Shadow buffer failed, using clipboard fallback")
        return self.clipboard_strategy.insert(insertion_string, context, request_id, options)


class FlutterStrategy(StandardStrategy):
    """Strategy for Flutter applications (wraps Standard but context implies Flutter).

    Inherits from StandardStrategy because the logic is the same (ShadowBuffer -> Clipboard),
    but the underlying helpers use the 'is_flutter' flag from the context to adjust behavior.
    """
    pass


class UnicodeFirstStrategy(InsertionStrategy):
    """Try VerifiedUnicodeStrategy; fall back to StandardStrategy on
    pre-send failure (wh-r7al.1).

    Production reality: many normal apps (Chromium renderer children,
    Electron shells, custom controls) do not expose UIA TextPattern. In
    those apps, ``ShadowBufferManager.synchronize()`` returns False, and
    a strategy that refuses on sync failure (``VerifiedUnicodeStrategy``)
    leaves the user with a Schema A error instead of a working insert.
    ``StandardStrategy``'s ``ClipboardFallbackStrategy`` handles those
    targets via clipboard ``gather_context``, so wrapping the two
    preserves the wh-606yk routing intent (use SendInput when feasible,
    clipboard otherwise) without introducing a regression.

    Fallback gate: only retry with ``StandardStrategy`` when the Unicode
    attempt did not fire SendInput. The flag the strategy sets right
    before calling ``type_string_verified`` is
    ``ClipboardOperations.last_paste_was_sent``; if it is True after a
    failed Unicode result, partial Unicode keystrokes may have landed in
    the target. Running ``StandardStrategy.insert`` after that would
    paste the same text again and double-insert. ``VerifiedUnicodeStrategy``
    already poisons retract and invalidates the shadow buffer on those
    post-send failure paths, so the handler still receives a meaningful
    Schema A error in that case.

    The wh-bte reset at the top of ``StandardStrategy.insert`` clears
    ``last_paste_was_sent`` again before the fallback's shadow attempt
    starts, so the inner gate inside ``StandardStrategy`` only sees
    state from its own ``ShadowBufferStrategy`` call.
    """

    def __init__(
        self,
        verified_unicode_strategy: "VerifiedUnicodeStrategy",
        standard_strategy: "StandardStrategy",
        clipboard_ops,
    ):
        self.verified_unicode = verified_unicode_strategy
        self.standard = standard_strategy
        self.clipboard = clipboard_ops

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        result = self.verified_unicode.insert(insertion_string, context, request_id, options)
        if result.success:
            return result

        # Unicode failed. If SendInput already fired (last_paste_was_sent
        # is True), do NOT fall back -- StandardStrategy would paste the
        # same text again on top of partially landed Unicode characters
        # and double-insert. The Unicode strategy has already poisoned
        # retract and invalidated the buffer in this case (wh-0juh.1,
        # wh-0juh.2), so returning the Unicode result lets the handler
        # surface a Schema A error to the caller.
        if self.clipboard.last_paste_was_sent:
            return result

        # Pre-send failure (no focused control, unresolvable HWND, shadow
        # buffer sync failure, type_string_verified raised before the
        # first SendInput batch). Hand off to StandardStrategy, whose
        # ClipboardFallbackStrategy can gather context via the clipboard
        # in apps that do not expose UIA TextPattern.
        return self.standard.insert(insertion_string, context, request_id, options)


class VerifiedUnicodeStrategy(InsertionStrategy):
    """Verified Unicode SendInput insertion strategy (wh-9jml6).

    Uses ``utils.win_input_sender.type_string_verified`` (wh-jmt5x) as the
    delivery transport instead of clipboard paste, but reuses the same
    composition pipeline as ``StandardStrategy``: TextPerfector for
    spacing/capitalization/literal handling, shadow buffer context lookup
    before the send, and shadow buffer + retraction counter updates after
    success. The Unicode path is a delivery alternative, not a replacement
    for composition. A standalone path that skipped TextPerfector would
    produce ``"hello world"`` instead of ``"Hello world"`` on streamed
    dictation; one that skipped the shadow buffer update would leave the
    next streamed word's preceding context stale; one that skipped the
    retraction-counter update would silently break retract.

    Focus and post-send foreground checks mirror
    ``ClipboardOperations.verified_paste`` (wh-59i32 + wh-oe7u.3) so the
    same focus-drift protections apply: the target HWND captured at
    strategy entry is restored before the send, and the foreground window
    after the send is root-normalized and compared against the captured
    target. Any normalization failure or mismatch refuses to credit the
    retraction counter and returns False to the caller.

    Provenance flags on the shared ``ClipboardOperations`` are touched so
    the retraction gate (which reads
    ``last_paste_was_optimistic`` and ``accumulated_paste_chars``) sees
    consistent state regardless of which strategy delivered the last
    insertion. ``last_paste_was_optimistic`` is reset to False at entry
    and only set True on a failure path that ran after SendInput was
    issued -- that generalizes its meaning from "clipboard verification
    timed out" to "paste state uncertain", which is the correct gate for
    both clipboard lock contention and Unicode partial sends or
    post-send focus mismatches (wh-0juh.1).

    On any failure path that runs after ``last_paste_was_sent = True``,
    the shadow buffer is also invalidated (wh-0juh.2). Partial sends or
    post-send-mismatch failures may leave uncredited characters in the
    target field, so the next compose must re-sync via UIA instead of
    reading stale preceding_chars from the cached buffer.
    """

    # wh-3nwy / wh-ix1z.19 / wh-fc1x.2: same-process foreground
    # fallback is opt-in by process exe name. Brave / Chrome / Edge /
    # Vivaldi / Opera / Arc / Brave-Beta and other Chromium-derived
    # browsers spawn transient top-level helper windows (autocomplete
    # popup, autofill suggestions, spellcheck overlay, invisible
    # reCAPTCHA badge) that briefly own foreground after a paste; the
    # keystrokes land correctly in the focused renderer of the main
    # HWND. Other apps (Word, Outlook, Visual Studio, etc.) also have
    # multi top-level windows in a single process, but their
    # dialog/popup patterns are different -- focus shifts there
    # usually mean the paste WAS misdirected. The strict GA_ROOT check
    # is correct for those; the same-process fallback is correct only
    # for known browsers.
    #
    # The canonical list lives in services/wheelhouse/config.toml
    # under [ui_actions.foreground_check].same_process_browser_names
    # so users can edit it without changing code. This class attribute
    # aliases the hardcoded fallback in ui.hwnd_utils (the value used
    # only when the config key is missing entirely). Kept on the class
    # so callers that still pass same_process_browser_names=None at
    # construction (legacy tests, the wh-ix1z.22 handler wiring before
    # wh-fc1x.2 moved to a shared resolver) get the same fallback the
    # config-resolver does.
    DEFAULT_SAME_PROCESS_BROWSER_NAMES: frozenset[str] = (
        _FALLBACK_SAME_PROCESS_BROWSER_NAMES
    )

    def __init__(
        self,
        buffer_manager,
        text_perfector,
        clipboard_ops,
        window_manager,
        *,
        same_process_browser_names: Optional[frozenset[str]] = None,
    ):
        self.buffer_manager = buffer_manager
        self.text_perfector = text_perfector
        self.clipboard = clipboard_ops
        self.window_manager = window_manager
        # Lower-cased, frozen for fast membership checks.
        names = (
            same_process_browser_names
            if same_process_browser_names is not None
            else self.DEFAULT_SAME_PROCESS_BROWSER_NAMES
        )
        self._same_process_browser_names: frozenset[str] = frozenset(
            n.lower() for n in names
        )
        # wh-trailing-corruption-instrument: per-strategy-instance dispatch
        # ordinal. Incremented in insert() right before the SendInput burst
        # so the log carries the same ordinal the SendInput call ran at.
        # The Input process constructs the strategy once at startup, so the
        # counter is effectively process-lifetime-scoped and tests the
        # wh-startup-trailing-corruption hypothesis that corruption clusters
        # near process startup.
        self._dispatch_count: int = 0

    def _resolve_target_process_name(self, target_hwnd: Optional[int]) -> Optional[str]:
        """Look up the captured target's exe name via PID, lowercased.

        Thin wrapper over ui.hwnd_utils.process_name_for_hwnd so tests
        can patch it on the strategy module. Returns None on any
        failure (no HWND, GetWindowThreadProcessId raises or returns 0,
        psutil raises). Used to decide whether the same-process
        foreground fallback applies (only known Chromium-derived
        browsers, per wh-ix1z.19).
        """
        return process_name_for_hwnd(target_hwnd)

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        opts = _resolve_options(options)
        verbatim = opts.mode is InsertionMode.VERBATIM

        # Reset paste provenance flags so a previous paste's state cannot
        # leak into the retraction gate's view of this one. Mirrors the
        # top of ClipboardOperations.verified_paste (wh-d43oi).
        self.clipboard.last_paste_was_optimistic = False
        self.clipboard.last_paste_was_sent = False

        if not context.focused_control:
            logger.warning(
                "VerifiedUnicodeStrategy: no focused_control in context; "
                "cannot determine target HWND, refusing to send."
            )
            return InsertionResult(success=False, clipboard_dirty=False)

        target_control = context.focused_control
        target_hwnd = _hwnd_from_control(target_control)
        if target_hwnd is None:
            logger.warning(
                "VerifiedUnicodeStrategy: could not resolve target HWND "
                "from focused_control; refusing to send."
            )
            return InsertionResult(success=False, clipboard_dirty=False)

        # wh-iti5: VERBATIM mode skips the shadow-buffer sync gate and the
        # TextPerfector pass entirely. The caller already composed the
        # final text. The buffer-sync requirement only existed so the
        # perfecter could see preceding context; with no perfecter to
        # run, the buffer state is irrelevant before the send.
        if verbatim:
            final_string = insertion_string
        else:
            # Validate / synchronize the shadow buffer so perfected_string sees
            # the right preceding context. Same gate as ShadowBufferStrategy.
            if not self.buffer_manager.is_valid:
                if not self.buffer_manager.synchronize():
                    logger.warning(
                        "VerifiedUnicodeStrategy: buffer synchronization failed"
                    )
                    return InsertionResult(success=False, clipboard_dirty=False)

            buffer_context = self.buffer_manager.get_context()
            final_string = self.text_perfector.perfected_string(
                insertion_string,
                **buffer_context,
            )

        # Pre-send focus restoration. Parallels the non-flutter branch of
        # verified_paste: ensure the captured top-level HWND is foreground,
        # then SetFocus on the captured control. ensure_focused failures
        # are non-fatal -- the post-send foreground check below catches
        # the case where focus did not actually land on the target.
        try:
            self.window_manager.ensure_focused(target_hwnd)
            try:
                target_control.SetFocus()
            except Exception as e:
                logger.warning(
                    "VerifiedUnicodeStrategy: SetFocus failed before send: %s", e,
                )
        except Exception as e:
            logger.warning(
                "VerifiedUnicodeStrategy: focus restore outer exception "
                "(target_hwnd=%s): %s",
                target_hwnd, e,
            )

        # wh-trailing-corruption-instrument: capture the dispatch ordinal,
        # modifier-key state, and final-string codepoints right before the
        # SendInput burst so a future reproduction of
        # wh-startup-trailing-corruption (clean SendInput acceptance, wrong
        # on-screen text, clustered near process startup) has the cold-
        # keyboard-state evidence inline. The first DISPATCH_INFO_LOG_LIMIT
        # dispatches log at INFO; subsequent dispatches drop to DEBUG so a
        # long dictation session is not flooded.
        self._dispatch_count += 1
        ordinal = self._dispatch_count
        mods = snapshot_modifier_state()
        first_cp = ord(final_string[0]) if final_string else 0
        last_cp = ord(final_string[-1]) if final_string else 0
        log_method = (
            logger.info if ordinal <= DISPATCH_INFO_LOG_LIMIT else logger.debug
        )
        cold_state = _snapshot_cold_state()
        log_method(
            "VerifiedUnicodeStrategy: dispatch ord=%d class=%s process=%s "
            "text_len=%d first=0x%04x last=0x%04x %s %s",
            ordinal, getattr(context, "class_name", "?"),
            getattr(context, "process_name", "?"),
            len(final_string), first_cp, last_cp, mods, cold_state,
        )

        # Mark the keystroke as fired before SendInput is issued so the
        # provenance flag reflects "something may have landed" even if
        # type_string_verified raises. Mirrors verified_paste's ordering.
        self.clipboard.last_paste_was_sent = True

        try:
            success, chars_sent, error = type_string_verified(final_string)
        except Exception as e:
            logger.error(
                "VerifiedUnicodeStrategy: type_string_verified raised: %s", e,
                exc_info=True,
            )
            self._poison_retract_and_invalidate_buffer()
            return InsertionResult(success=False, clipboard_dirty=False)

        if not success:
            logger.error(
                "VerifiedUnicodeStrategy: send failed (%s); "
                "chars_sent=%d/%d -- skipping shadow update and counter increment",
                error, chars_sent, len(final_string),
            )
            self._poison_retract_and_invalidate_buffer()
            return InsertionResult(success=False, clipboard_dirty=False)

        # Post-send foreground check. wh-oe7u.3 fail-closed semantics:
        # any normalization failure on either side, or a mismatch, refuses
        # to credit the retraction counter and returns False. Partial-send
        # and post-send-mismatch responses also poison retraction
        # (wh-0juh.1) and invalidate the shadow buffer (wh-0juh.2) so the
        # rest of the utterance cannot retract over uncredited text or
        # compose against stale context.
        #
        # wh-3nwy / wh-fc1x: use hwnds_match_for_foreground_compare with
        # allow_same_process=True so transient Chromium / Electron
        # helper windows (autocomplete popup, autofill suggestions,
        # spellcheck correction overlay, invisible reCAPTCHA badge)
        # that briefly own foreground after a paste do not flag the
        # post-send check as a failure. The OS keyboard focus stays
        # on the main browser HWND in those cases and the keystrokes
        # land in the focused renderer correctly. The helper preserves
        # the wh-0juh / wh-oe7u.3 fail-closed semantics: any HWND that
        # cannot be root-normalized makes the helper return False.
        try:
            actual_hwnd = win32gui.GetForegroundWindow()
        except Exception as e:
            logger.warning(
                "VerifiedUnicodeStrategy: GetForegroundWindow failed: %s; "
                "refusing to credit counter (fail-closed).", e,
            )
            self._poison_retract_and_invalidate_buffer()
            return InsertionResult(success=False, clipboard_dirty=False)
        # wh-ix1z.19: same-process fallback is scoped to known
        # Chromium-derived browsers only. Other apps (Word dialogs,
        # Visual Studio popups, etc.) keep the strict GA_ROOT-only
        # behavior because their multi top-level patterns usually
        # mean the paste WAS misdirected when foreground shifts.
        target_process = self._resolve_target_process_name(target_hwnd)
        allow_same_process = (
            target_process is not None
            and target_process in self._same_process_browser_names
        )
        if not hwnds_match_for_foreground_compare(
            target_hwnd, actual_hwnd,
            allow_same_process=allow_same_process,
            expected_process_name=target_process if allow_same_process else None,
        ):
            logger.warning(
                "VerifiedUnicodeStrategy: post-send foreground check failed: "
                "expected hwnd=%s (process=%s), observed hwnd=%s "
                "(allow_same_process=%s). Skipping shadow update and counter "
                "increment.",
                target_hwnd, target_process or "?",
                actual_hwnd, allow_same_process,
            )
            self._poison_retract_and_invalidate_buffer()
            return InsertionResult(success=False, clipboard_dirty=False)

        # wh-trailing-corruption-phase2: post-send UIA TextPattern read.
        # SendInput accepted every event and the foreground HWND still
        # matches the target. If the on-screen text in the cold window
        # is corrupt while this read still shows the EXPECTED tail, the
        # underlying text buffer is correct and the corruption is paint-
        # side. If this read shows the corrupt tail, the keys were
        # mutated in the Windows input pipeline before the control's
        # buffer. If the read fails / times out repeatedly in the cold
        # window, UIA itself is cold and slow reads may be interleaving
        # with the next SendInput.
        #
        # The readback costs hundreds of milliseconds per call on a
        # cold UIA subsystem so we cap it to the first
        # POST_SEND_READBACK_DISPATCH_LIMIT dispatches per session.
        # That bounds the slow-dictation cost to the first sentence or
        # so after WheelHouse starts, after which the readback turns
        # off and dictation speed returns to normal. The user reports
        # the corruption usually shows up in those first few words.
        # If the added latency itself prevents the bug from
        # reproducing inside this window, that result is still useful
        # data: it means inter-word timing is part of the bug.
        if ordinal <= POST_SEND_READBACK_DISPATCH_LIMIT:
            self._post_send_readback_check(target_control, final_string, ordinal)

        # Full success. Update the shadow buffer with the actual delivered
        # string so the next streamed word sees correct preceding context,
        # and credit the retraction counter by the same length so retract's
        # send_backspaces(N) walks back exactly what landed. In VERBATIM
        # mode final_string == insertion_string so accounting reflects the
        # caller-provided text exactly.
        if verbatim and self.buffer_manager.is_valid is False:
            # Verbatim insertion may have replaced a selection or jumped
            # the cursor in ways the buffer cannot reconstruct from the
            # inserted string alone. If the buffer is not currently
            # valid, leave it invalid so the next dictation re-syncs.
            pass
        else:
            self.buffer_manager.update_after_insertion(final_string)
        # wh-pkhrp.1.7 / wh-pkhrp.2: credit the retract accounting fields.
        # The Python-len counter still advances for SendInput parity on
        # apps that delete by code unit; the grapheme-cluster counter
        # advances in parallel so retract on a Qt-backed target uses the
        # right backspace count. Routed through credit_paste_chars
        # (wh-pkhrp.3.6) so the provenance hook is canonical across paste
        # paths. ``target_class_name=context.class_name`` lets the helper
        # set the sticky ``accumulated_paste_was_qt`` flag the retract
        # path reads.
        self.clipboard.credit_paste_chars(
            final_string, target_class_name=context.class_name,
        )
        return InsertionResult(success=True, clipboard_dirty=False)

    def _poison_retract_and_invalidate_buffer(self) -> None:
        """Mark the utterance unsafe for retract and invalidate the shadow buffer.

        Called from every VerifiedUnicodeStrategy failure path that runs
        after ``last_paste_was_sent = True`` (wh-0juh.1, wh-0juh.2). Two
        concerns must be addressed together:

        1. ``accumulated_paste_chars`` may already contain credit from
           earlier successful insertions in the same utterance. Once a
           later send partially lands or finishes in a window we cannot
           verify, the prior credited length no longer points at the
           right span: retraction would walk backspaces over partially
           landed uncredited text and chew the wrong characters. Set
           ``last_paste_was_optimistic = True`` so the retraction gate
           in ``ui_action_handler.retract`` blocks with
           ``reason=paste_unverified``. This generalizes the existing
           clipboard-verification meaning of the flag to "paste state
           uncertain" -- the retract response (refuse) is identical for
           both reasons.

        2. The shadow buffer may be stale: SendInput may have placed
           characters in the target that the strategy did not credit, so
           the next compose would read pre-failure ``preceding_chars``
           and produce wrong capitalization/spacing. Invalidate the
           buffer so the next insertion re-syncs via UIA before
           composing.
        """
        self.clipboard.last_paste_was_optimistic = True
        self.buffer_manager.invalidate()

    def _post_send_readback_check(
        self, target_control, final_string: str, ordinal: int,
    ) -> None:
        """Read recent text from the target control and compare to expected.

        wh-trailing-corruption-phase2: diagnostic only. Runs only inside
        the first POST_SEND_READBACK_DISPATCH_LIMIT dispatches so a long
        warm session does not pay the UIA cost forever. Logs the read
        result at INFO when it disagrees with the expected tail (so the
        next reproduction surfaces in the default log) and at DEBUG when
        it agrees (so a clean warm run stays quiet).

        Three outcomes worth distinguishing in the wheelhouse.log:
          * readback_match -- UIA sees the expected tail. If the screen
            still shows corruption, the underlying buffer is correct;
            the corruption is paint-side.
          * readback_mismatch -- UIA sees the corrupt tail. The keys
            were mutated before the control's buffer received them.
          * readback_failed -- UIA could not read. If this clusters in
            the cold ordinal range, UIA itself is the warmup gate.

        The helper swallows every exception. A diagnostic log line must
        not break the dispatch path on any failure.
        """
        try:
            tail_len = len(final_string)
            if tail_len <= 0:
                return
            # Read a generous window so a single missing char does not
            # zero the comparison. The compare is on the LAST tail_len
            # chars of the read.
            read_chars = max(tail_len * 2, 16)
            t0 = time.perf_counter()
            result = read_context_via_text_pattern(
                target_control, max_chars=read_chars,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if result is None:
                logger.info(
                    "VerifiedUnicodeStrategy: readback_failed ord=%d "
                    "expected=%r elapsed_ms=%.1f",
                    ordinal, redact_transcript(final_string), elapsed_ms,
                )
                return
            preceding = result.get("preceding_chars") or ""
            observed_tail = preceding[-tail_len:]
            if observed_tail == final_string:
                logger.debug(
                    "VerifiedUnicodeStrategy: readback_match ord=%d "
                    "expected=%r elapsed_ms=%.1f",
                    ordinal, redact_transcript(final_string), elapsed_ms,
                )
            else:
                logger.info(
                    "VerifiedUnicodeStrategy: readback_mismatch ord=%d "
                    "expected=%r observed_tail=%r elapsed_ms=%.1f",
                    ordinal, redact_transcript(final_string),
                    redact_transcript(observed_tail), elapsed_ms,
                )
        except Exception as e:
            logger.debug(
                "VerifiedUnicodeStrategy: readback exception ord=%d: %s",
                ordinal, e,
            )


class SimplePasteStrategy(InsertionStrategy):
    """Last-resort fallback for when no focusable control is found."""

    def __init__(self, clipboard_ops, window_manager):
        self.clipboard = clipboard_ops
        self.window_manager = window_manager

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        """Last-resort paste with no UIA control awareness.

        Default DICTATION mode appends a trailing space so streamed words
        do not run together. VERBATIM mode delivers the text exactly --
        callers (selection-wrap, transform_selection paste-back) already
        composed the final string and any extra whitespace would corrupt
        the result.
        """
        opts = _resolve_options(options)
        verbatim = opts.mode is InsertionMode.VERBATIM
        logger.warning(f"Executing simple paste fallback for: '{redact_transcript(insertion_string)}'")
        text_to_paste = insertion_string if verbatim else insertion_string + ' '
        success = self.clipboard.verified_paste(
            text_to_paste,
            self.window_manager,
            None,
            target_class_name=context.class_name,
        )
        # verified_paste writes the clipboard before sending Ctrl+V, so
        # clipboard_dirty=True regardless of success.
        return InsertionResult(success=success, clipboard_dirty=True)


class ClipboardOnlyStrategy(InsertionStrategy):
    """Silent paste for predicate verdicts the user has approved.

    Selected by the router when the text-target predicate returns
    verdict=True with reason='accept_soft_allow_tuple' -- the user has
    previously approved this (process, class, control_type) triple via
    the three-strikes grant prompt that writes soft_allow_tuples.toml,
    so subsequent focuses on the same target take the silent paste
    path the user opted in to (wh-9weum Phase 3,
    wh-soft-allow-verdict-tier). Also reached by the
    retry_dictation_by_token handler in ui_action_handler.py for a
    Try-it-anyway replay on an unknown soft-reject target, where the
    handler bypasses the router and calls this strategy directly. Real
    apps that surface this shape: Zed, Sublime Text, GPU-rendered
    editors that draw their own caret and ship no UIA TextPattern.

    Before wh-soft-allow-verdict-tier the router routed
    default_reject_paste_capable_class straight here, which silently
    pasted on first hit and never surfaced the rejection toast. That
    branch is gone; unknown soft rejects now route to
    RejectedInsertionStrategy (rejection toast + Try-it-anyway button),
    and ClipboardOnly handles only the user-approved-accept tier plus
    the explicit retry override.

    Behaviour intentionally distinct from StandardStrategy and
    SimplePasteStrategy:

      - No shadow buffer sync, lookup, or update. The buffer's
        preceding-context contract requires a target whose accepted
        text we can mirror; the soft-reject target does not meet that
        contract.

      - No retraction-counter advance. accumulated_paste_chars stays
        unchanged so a later retract attempt will not chew over text
        we cannot prove landed.

      - No SendInput keystrokes. Clipboard write + Ctrl+V only. The
        wh-zndq class of bug came from raw keystrokes hitting non-text
        controls; Ctrl+V is the safe shape because non-text controls
        ignore it (or beep) instead of misbehaving.

      - retry_outcome populated on every result:
          * 'verified'   when verified_paste returned True (post-paste
                         foreground check confirmed the target). Phase
                         4's verified-retry counter increments only on
                         this outcome (wh-mv5ih).
          * 'unverified' when verified_paste returned False but the
                         keystroke fired (last_paste_was_sent is True),
                         or when the optimistic-paste branch ran
                         (last_paste_was_optimistic is True). The
                         handler treats this as success at the IPC
                         level (no Schema A error) but the Phase 4
                         counter does NOT count it.
          * 'unverified' when the clipboard write failed before any
                         keystroke (success=False). The success flag
                         carries the failure; retry_outcome stays
                         'unverified' so the contract is uniform.

    InsertionMode handling:

      DICTATION (default for streamed voice): runs TextPerfector with
        a per-utterance preceding-chars mirror so words paste with
        the correct leading space and sentence-start capitalization.
        The mirror is the strategy's own record of what it has
        attempted to paste in this utterance; it is independent from
        the shadow buffer (the soft-reject target is not proven to
        accept text, so we do not synchronize the buffer against it).
        UIActionHandler.start_utterance must call
        ``reset_preceding_mirror`` so the next utterance starts with
        the right capitalization context.

      VERBATIM (selection-wrap, transform_selection paste-back): the
        caller already composed the final text. The strategy delivers
        ``insertion_string`` exactly, no TextPerfector pass, no
        prefix space. The mirror still tracks the verbatim string so
        a later DICTATION word in the same utterance perfects against
        the right preceding context.

    References: wh-9weum Phase 1 (epic), wh-pc28 (retry_outcome
    contract), wh-0ci9n (this strategy), wh-3oy1u (router wiring),
    wh-jldm0 (the wh-zndq trap that stays excluded from this path),
    review epics wh-sm5s and wh-jbo9 (converged design).
    """

    def __init__(self, clipboard_ops, window_manager, text_perfector=None):
        self.clipboard = clipboard_ops
        self.window_manager = window_manager
        # text_perfector is the same instance the rest of the input
        # process uses. Optional for back-compat with older test
        # fixtures that do not pass one; in that case DICTATION mode
        # falls back to a simple trailing-space approach so streamed
        # words still separate, even without sentence-start
        # capitalization.
        self._text_perfector = text_perfector
        # Mirror of what the strategy has attempted to paste in the
        # current utterance. Reset by reset_preceding_mirror() at
        # utterance boundaries. Independent from the shadow buffer.
        self._preceding_mirror: str = ""

    def reset_preceding_mirror(self) -> None:
        """Clear the per-utterance preceding-chars mirror.

        Called by UIActionHandler.start_utterance so the next
        utterance's first word perfects against an empty context
        (sentence-start capitalization). The terminal strategy's
        reset_editor_mirror is the parallel pattern.
        """
        self._preceding_mirror = ""

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        opts = _resolve_options(options)
        verbatim = opts.mode is InsertionMode.VERBATIM

        # Compose the text to paste based on mode.
        #
        #   DICTATION: run TextPerfector against the local
        #   preceding-chars mirror. The mirror captures what we have
        #   attempted to paste in this utterance, so the second word
        #   gets a leading space and sentence-start words get
        #   capitalized. Without this, streamed dictation into Zed
        #   pastes "helloworld" instead of "Hello world".
        #
        #   VERBATIM: deliver insertion_string exactly. The caller
        #   already composed the final text (selection wrap,
        #   transform_selection paste-back), so any prefix or
        #   capitalization change would corrupt the result.
        #
        # The mirror updates after composition in both modes so that
        # a later DICTATION word in the same utterance perfects
        # against the right context.
        if verbatim or self._text_perfector is None:
            text_to_paste = insertion_string
        else:
            text_to_paste = self._text_perfector.perfected_string(
                insertion_string,
                preceding_chars=self._preceding_mirror,
                has_selection=False,
            )

        target_hwnd = _hwnd_from_control(context.focused_control)

        # Snapshot the retract accounting fields before the paste call.
        # ClipboardOperations.verified_paste advances them through
        # credit_paste_chars on every successful paste. The wh-9weum
        # Phase 1 design explicitly keeps ClipboardOnly out of
        # retraction accounting because the target is not proven
        # text-safe -- if the fields advance here and a later Standard
        # insert in the same utterance also advances them, retract
        # would walk back the sum, including the soft-paste's length
        # even though we cannot prove that paste landed. Restore the
        # snapshot after the call (review wh-kox5.2). wh-pkhrp.2
        # widens the snapshot to also cover the cluster counter, the
        # Qt-target sticky flag, and the grapheme-unsafe sticky flag,
        # and runs the restore in a finally block so an exception in
        # verified_paste cannot leak any of the four fields into the
        # retract accounting (wh-pkhrp.2.1.2 codex finding).
        counter_before = self.clipboard.accumulated_paste_chars
        cluster_counter_before = self.clipboard.accumulated_paste_clusters
        was_qt_before = self.clipboard.accumulated_paste_was_qt
        has_grapheme_unsafe_before = self.clipboard.accumulated_has_grapheme_unsafe

        # verified_paste resets last_paste_was_optimistic /
        # last_paste_was_sent at its own entry, so we read the flags
        # AFTER the call to learn what actually happened. We do NOT
        # thread target_class_name here: the soft-paste's class is by
        # design held outside retract accounting, so leaving the Qt
        # flag at its snapshot value is correct.
        try:
            paste_returned_true = self.clipboard.verified_paste(
                text_to_paste,
                self.window_manager,
                None,  # not Flutter; the soft-reject path is non-Flutter only
                target_control=context.focused_control,
                target_hwnd=target_hwnd,
            )
            keystroke_fired = bool(self.clipboard.last_paste_was_sent)
            was_optimistic = bool(self.clipboard.last_paste_was_optimistic)
        finally:
            # Restore the fields so the soft-paste does not leak length,
            # cluster count, Qt-target stickiness, or grapheme-unsafe
            # stickiness into the retract accounting. The finally block
            # ensures restoration even if verified_paste raises.
            self.clipboard.accumulated_paste_chars = counter_before
            self.clipboard.accumulated_paste_clusters = cluster_counter_before
            self.clipboard.accumulated_paste_was_qt = was_qt_before
            self.clipboard.accumulated_has_grapheme_unsafe = has_grapheme_unsafe_before

        # Update the local mirror so the next word in this utterance
        # perfects against the cumulative pasted string. Update on
        # both success and post-send-failure paths because the
        # keystroke may have landed even when verified_paste reported
        # False; not updating would let the next word perfect against
        # a too-short preceding context and produce a spurious leading
        # space mid-sentence. Pre-send failures (no keystroke) leave
        # the mirror untouched.
        if paste_returned_true or keystroke_fired:
            self._preceding_mirror += text_to_paste

        # Decide success and retry_outcome together (review wh-kox5.1).
        # The wh-pc28 contract says the handler must NOT raise on
        # success=True with retry_outcome="unverified" -- the IPC
        # succeeded and the caller's Future should resolve. We honor
        # that contract by treating any path that issued the Ctrl+V
        # keystroke as a successful IPC, regardless of the post-paste
        # verification outcome:
        #
        #   verified_paste True, not optimistic -> verified delivery
        #   verified_paste True, optimistic     -> unverified delivery
        #     (clipboard verification timed out without observing
        #     wrong content, so we cannot prove the right text landed)
        #   verified_paste False, keystroke_fired True -> unverified
        #     delivery (post-send foreground check failed, but Ctrl+V
        #     was issued; treat as IPC success per the contract)
        #   verified_paste False, keystroke_fired False -> failure
        #     (pre-send: _safe_copy or verification refused before any
        #     keystroke; the caller's Future MUST surface this)
        if paste_returned_true:
            success = True
            retry_outcome = "verified" if not was_optimistic else "unverified"
        elif keystroke_fired:
            success = True
            retry_outcome = "unverified"
        else:
            success = False
            retry_outcome = "unverified"

        return InsertionResult(
            success=success,
            clipboard_dirty=True,
            retry_outcome=retry_outcome,
        )


class RejectedInsertionStrategy(InsertionStrategy):
    """Pre-send refusal returned when the focused control is not a text target.

    The router selects this strategy when the shared text-target predicate
    (ui.text_target.TextTargetPredicate.evaluate) returns verdict=False.
    insert is a deliberate no-op: no keystrokes, no clipboard write, no
    shadow buffer update, no counter advance, no fail-closed poisoning of
    the wh-0juh state. The strategy returns
    InsertionResult(success=True, clipboard_dirty=False,
    rejected_reason=...) so the handler emits a Schema A response with
    PATH_INSERT_REJECTED and the caller's Future resolves cleanly without
    the "strategy returned False" traceback that wh-3nwy is removing.

    The router logs the predicate verdict (class, process, control type,
    supported patterns) at DEBUG before invoking this strategy; the
    strategy itself also logs at DEBUG only so background-speech noise
    does not produce a wall of routine INFO records. The first
    rejection per (process, class, reason) key is escalated to INFO
    inside this strategy when first_log_map is wired (wh-zib65) so the
    diagnostic stream remains useful without flooding.

    wh-7318z (wh-9weum Phase 2 / wh-soft-allow-verdict-tier): when
    constructed with a response_queue and a RejectionTextCache, and
    when the router calls set_pending_verdict before invoking insert,
    the strategy may also send a structured text_target_rejected event
    so the GUI can render the advisory notice. The dictation text never
    crosses the process boundary -- the strategy stores it under a
    uuid4 correlation_token in the input-process cache, and only the
    token is forwarded. The router calls set_pending_verdict for every
    reject decision, both the uncertain case
    (default_reject_paste_capable_class) and the three silent
    categories (browser_trap, definitely_not_text, other).

    wh-1r2b3: the silencing check runs in insert() before
    _emit_rejection_event is called. Only the uncertain category
    actually sends the event and populates the cache; the three silent
    categories drop the words and stop here. The DEBUG drop log and
    the FirstRejectionLogMap INFO escalation both fire BEFORE the
    silencing branch, so the operator-facing diagnostic stream is
    unchanged. A silenced rejection still returns the same
    InsertionResult shape (success=True, was_rejected=True), so the
    IPC demuxer behaves identically across silenced and non-silenced
    rejections.

    References: wh-zndq (no-text-input dictation routing), wh-fc1x (text
    input target handling epic), wh-ix1z.2 (codex-review-loop round 1
    finding: explicit silent reject outcome), wh-7318z (Phase 2 emit).
    """

    DEFAULT_REASON = "no_text_target"

    def __init__(
        self,
        response_queue=None,
        text_cache=None,
        app_name_resolver=None,
        first_log_map=None,
        browser_process_names=None,
        text_perfector=None,
    ):
        """Optional emission wiring.

        Args:
            response_queue: input-to-logic response queue. When None,
                the strategy does not emit text_target_rejected events
                (preserves the legacy no-arg construction path used by
                older test fixtures).
            text_cache: RejectionTextCache for token -> dictation-text
                mappings. When None, the strategy does not emit. When
                non-None, the cache is populated on every emit so the
                Phase 4 retry click can recover the original text.
            app_name_resolver: optional FriendlyAppNameResolver (wh-b0sch).
                Resolves a human-readable app name (e.g. ``"Zed"``) for
                the rejection toast. When None, the strategy falls back
                to the captured ``process_name`` so older test fixtures
                without the resolver keep working.
            first_log_map: optional FirstRejectionLogMap (wh-zib65). When
                provided, the strategy escalates the per-call DEBUG
                rejection log to INFO exactly once per
                ``(process, class, reason)`` key so the diagnostic
                stream is not swamped during continuous dictation.
            browser_process_names: optional iterable of browser exe
                names (wh-1r2b3.2.1). Passed through to
                ``categorize_rejection`` so the categorizer's view of
                the browser set matches the text-target check's
                config-extended view. Production wires this from
                ``TextTargetPredicate._browser_processes``. Test
                fixtures that pass None get the built-in default list.
            text_perfector: optional TextPerfector
                (wh-override-multiword-retry.1.1). The speech pipeline
                emits one fragment at a time, and punctuation
                replacements (period, comma, question mark, etc.)
                arrive as their own fragments. When provided, the
                aggregation path composes each new fragment using the
                same spacing and capitalization rules the regular
                paste path uses, so a dictation of "hello period
                world" caches "hello. World" rather than the
                space-joined "hello . world". When None, the
                aggregation path falls back to a single-space join
                (legacy behaviour; preserved so older test fixtures
                without a perfector keep working).
        """

        self._response_queue = response_queue
        self._text_cache = text_cache
        self._app_name_resolver = app_name_resolver
        self._first_log_map = first_log_map
        self._browser_process_names = browser_process_names
        self._text_perfector = text_perfector
        self._pending_verdict = None
        # wh-override-multiword-retry: per-key (process_name,
        # class_name, control_type, reason) -> active correlation_token.
        # When a second rejection arrives for the same key while the
        # cache entry from the previous rejection is still alive, the
        # strategy appends the new insertion_string to that entry and
        # emits the event with the same token, so the GUI's
        # last-rejection-token binding (updated on every event per
        # wh-vbvgf.3.1) keeps the visible Try-it-anyway button pointing
        # at one cache entry that holds the whole utterance. Without
        # this, the per-stable-word dispatch of the speech pipeline
        # produced one cache entry per word and the retry replayed only
        # the last word.
        self._aggregation_buckets: dict[
            tuple[str, str, str, str], str,
        ] = {}

    def set_pending_verdict(self, verdict) -> None:
        """Stash the predicate verdict for the next insert call.

        The router calls this before returning the strategy on every
        reject verdict, soft (default_reject_paste_capable_class) and
        hard alike. The soft-reject call is what wakes up the rejection
        toast + Try-it-anyway button for unknown paste-capable targets
        in production (wh-soft-allow-verdict-tier). The strategy reads
        and clears the stashed verdict inside ``insert`` so a
        subsequent direct ``insert`` call (no router) does not re-emit
        using stale verdict data.
        """

        self._pending_verdict = verdict

    def _prune_dead_buckets(self) -> None:
        """Drop aggregation entries whose cache token is no longer alive.

        Keeps the bucket map's size bounded by the number of live cache
        entries (cap of 100 in production). Without this, a long
        session that touches many distinct (process, class,
        control_type, reason) keys would let the map grow even though
        the individual cache entries time out after 60 seconds.
        """

        if self._text_cache is None or not self._aggregation_buckets:
            return
        from ui.rejection_text_cache import CacheStatus
        dead_keys = [
            key
            for key, token in self._aggregation_buckets.items()
            if self._text_cache.resolve(token).status is not CacheStatus.HIT
        ]
        for key in dead_keys:
            del self._aggregation_buckets[key]

    def forget_token(self, token: str) -> None:
        """Remove any aggregation bucket pointing at ``token``.

        wh-override-multiword-retry.2.2 (deepseek finding): the retry
        handler calls this immediately after invalidating a cache
        entry on a verified retry so the bucket map stays synchronised
        with the cache. Without this call, the stale bucket entry
        would sit in the map until the next call to
        ``_emit_rejection_event`` happened to trigger
        ``_prune_dead_buckets``. In a session where the user clicks
        Try-it-anyway, gets the verified outcome, and never dictates
        against a rejected target again before process exit, the
        bucket entry would leak. Idempotent and safe to call on
        unknown tokens.
        """

        if not self._aggregation_buckets:
            return
        dead_keys = [
            key for key, value in self._aggregation_buckets.items()
            if value == token
        ]
        for key in dead_keys:
            del self._aggregation_buckets[key]

    def _resolve_target_identity(self, context) -> tuple[int, int]:
        """Resolve the rejected target's top-level HWND and owning PID.

        Returns ``(target_hwnd, target_process_id)``. Either or both
        may be 0 if the lookup fails (stale COM, no top-level, no
        focused control). The retry handler treats 0 as 'no refocus
        needed' so callers can store 0 without breaking the win32
        layer.

        wh-override-multiword-retry.2.1 (deepseek finding): the append
        path uses this to upgrade an aggregated entry whose first
        emission resolved HWND=0 (transient stale COM, startup race)
        to a non-zero HWND when a later fragment's lookup succeeds.
        The fresh-token path uses this to populate the cache entry on
        the first emission.
        """

        focused_control = getattr(context, "focused_control", None)
        target_hwnd = 0
        if focused_control is not None:
            try:
                top = focused_control.GetTopLevelControl()
                hwnd_attr = (
                    getattr(top, "NativeWindowHandle", 0) if top else 0
                )
                target_hwnd = int(hwnd_attr) if hwnd_attr else 0
            except Exception as exc:
                logger.debug(
                    "RejectedInsertionStrategy: top-level HWND lookup "
                    "failed: %s",
                    exc,
                )
                target_hwnd = 0
        target_process_id = int(getattr(context, "process_id", 0) or 0)
        return target_hwnd, target_process_id

    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        # The router has already logged the predicate verdict at INFO.
        # This DEBUG line correlates the actual dropped utterance with
        # the routing decision when both lines are present in the log.
        process_name = getattr(context, "process_name", "") or ""
        class_name = getattr(context, "class_name", "") or ""
        verdict = self._pending_verdict
        # NOTE: TextTargetVerdict.__bool__ returns the underlying
        # accept/reject -- it is False for rejection verdicts. We must
        # compare against None to distinguish "verdict provided" from
        # "verdict says reject".
        reason = (
            getattr(verdict, "reason", "") if verdict is not None else ""
        )
        control_type = (
            getattr(verdict, "control_type", "") if verdict is not None else ""
        )
        logger.debug(
            "RejectedInsertionStrategy: dropping insert (len=%d) class=%s process=%s",
            len(insertion_string), class_name, process_name,
        )
        # wh-zib65: input-process first-rejection diagnostic log. Logs
        # INFO exactly once per (process, class, control_type, reason)
        # so a continuous dictation session against the same wrong
        # target surfaces in the diagnostic stream without per-word
        # noise. wh-9weum.4.2: control_type is part of the key so
        # frameworks that share one ClassName across many control
        # types do not collapse distinct rejections into one log line.
        if (
            self._first_log_map is not None
            and reason
            and self._first_log_map.should_log(
                (process_name, class_name, control_type, reason)
            )
        ):
            logger.info(
                "text_target rejection (first per key) "
                "process=%s class=%s control_type=%s reason=%s",
                process_name, class_name, control_type, reason,
            )
        self._pending_verdict = None
        if (
            verdict is not None
            and self._response_queue is not None
            and self._text_cache is not None
        ):
            # wh-1r2b3: only send the rejection event for the uncertain
            # category. For browser_trap, definitely_not_text, and other
            # categories the user has no useful action (no Try-it-anyway
            # button would appear) so showing the notice is pure noise.
            # The DEBUG drop log and the FirstRejectionLogMap INFO log
            # already fired above; the diagnostic stream is preserved.
            # The verdict's process_name and class_name are populated by
            # the predicate's evaluate() path for every reject branch,
            # so we read them directly without falling back to context.
            category = categorize_rejection(
                reason=reason,
                process_name=getattr(verdict, "process_name", "") or "",
                class_name=getattr(verdict, "class_name", "") or "",
                browser_process_names=self._browser_process_names,
            )
            if should_show_try_anyway(category):
                self._emit_rejection_event(insertion_string, context, verdict)
        return InsertionResult(
            success=True,
            clipboard_dirty=False,
            rejected_reason=self.DEFAULT_REASON,
        )

    def _emit_rejection_event(
        self,
        insertion_string: str,
        context: UIContext,
        verdict,
    ) -> None:
        """Build and emit the text_target_rejected payload (wh-7318z).

        Privacy: ``insertion_string`` is stored only in the
        input-process cache, keyed by a fresh uuid4 token. The IPC
        payload carries the token, not the text.

        wh-override-multiword-retry: when the speech pipeline emits
        multiple stable words against the same rejected target within
        one utterance, the strategy aggregates them onto one
        correlation_token. The first emission for a key allocates a
        fresh token and caches the word; subsequent emissions for the
        same key append the new word (joined by a single space) onto
        the existing cache entry and re-emit with the same token. The
        GUI cooldown suppresses the visible toast for the repeat
        events; its ``_last_rejection_token`` binding (wh-vbvgf.3.1)
        keeps pointing at the same token, so the user's single
        Try-it-anyway click replays the whole utterance.
        """

        # Lazy imports keep the existing strategy file's import set
        # unchanged for callers that never trigger emission.
        from services.wheelhouse.shared.text_target_rejection import (
            TextTargetRejectedEvent,
            new_correlation_token,
        )
        from ui.rejection_text_cache import CacheStatus

        process_name = getattr(context, "process_name", "") or ""
        verdict_class_name = getattr(verdict, "class_name", "") or ""
        verdict_control_type = getattr(verdict, "control_type", "") or ""
        verdict_reason = (
            getattr(verdict, "reason", "") or self.DEFAULT_REASON
        )
        aggregation_key = (
            process_name,
            verdict_class_name,
            verdict_control_type,
            verdict_reason,
        )

        # Try to reuse an active token for this key. The cache resolve
        # gives us the source of truth: if the entry is HIT, the token
        # is alive and we can append. If it is MISS or EXPIRED, the
        # token's entry is gone and we must allocate a fresh one.
        existing_token = self._aggregation_buckets.get(aggregation_key)
        existing_result = None
        if existing_token is not None:
            existing_result = self._text_cache.resolve(existing_token)
            if existing_result.status is not CacheStatus.HIT:
                # Drop the stale bucket entry and fall through to fresh
                # token allocation below.
                del self._aggregation_buckets[aggregation_key]
                existing_token = None
                existing_result = None

        if existing_token is not None and existing_result is not None:
            existing_text = existing_result.text or ""
            # wh-override-multiword-retry.1.1: compose the new fragment
            # via TextPerfector against the accumulated text when a
            # perfector is wired. A dictation like "hello period world"
            # arrives as three fragments ("hello", ".", "world"); the
            # unconditional space join used before this finding
            # produced "hello . world", which the retry path then
            # pasted with bad spacing. TextPerfector applies the same
            # spacing and capitalization rules the regular paste path
            # uses, so the punctuation joins cleanly ("hello." then
            # "hello. World"). When no perfector is wired, fall back
            # to the legacy single-space join so older test fixtures
            # without a perfector keep working.
            if self._text_perfector is not None:
                try:
                    fragment = self._text_perfector.perfected_string(
                        insertion_string,
                        preceding_chars=existing_text,
                        has_selection=False,
                    )
                except Exception as exc:
                    logger.warning(
                        "RejectedInsertionStrategy: TextPerfector "
                        "raised during aggregation: %s; falling back "
                        "to space join",
                        exc,
                    )
                    fragment = " " + insertion_string
                combined_text = existing_text + fragment
            else:
                combined_text = existing_text + " " + insertion_string
            # wh-override-multiword-retry.2.1 (deepseek finding): the
            # first fragment's HWND/PID is the source of truth so a
            # transient stale-COM on a later fragment does not poison
            # a valid earlier HWND. The exception is HWND=0: any
            # non-zero HWND from a later fragment is strictly better
            # information, since the retry handler treats HWND=0 as
            # "no refocus needed" and pastes into whatever holds
            # foreground (usually the rejection notice's own button
            # the user just clicked). Try to upgrade.
            cached_hwnd = existing_result.target_hwnd
            cached_pid = existing_result.target_process_id
            if cached_hwnd == 0:
                upgrade_hwnd, upgrade_pid = self._resolve_target_identity(
                    context,
                )
                if upgrade_hwnd != 0:
                    cached_hwnd = upgrade_hwnd
                    cached_pid = upgrade_pid
            try:
                self._text_cache.put(
                    existing_token, combined_text,
                    target_hwnd=cached_hwnd,
                    target_process_id=cached_pid,
                )
            except Exception as exc:
                logger.warning(
                    "RejectedInsertionStrategy: failed to extend cache "
                    "entry for token=%s: %s",
                    existing_token, exc,
                )
                return
            token = existing_token
        else:
            token = new_correlation_token()
            # wh-override-paste-focus-drift: resolve the rejected
            # target's top-level HWND at rejection time so the retry
            # handler can restore foreground to that window before
            # pasting. Without this, the retry's capture_context() sees
            # the toast's QPushButton (the click landed on it) and
            # ClipboardOnlyStrategy pastes into the button, which
            # silently consumes the keystroke. We store the RAW
            # top-level NativeWindowHandle here (not the
            # GetAncestor(GA_ROOT)-normalized form used by
            # paste-verification foreground checks). The retry path
            # passes this value to WindowFocusManager.ensure_focused ->
            # SetForegroundWindow, which wants the raw top-level handle.
            # Stale-COM or missing-top-level produces target_hwnd=0; the
            # retry handler treats 0 as 'no refocus needed' so the win32
            # layer is not touched with a zero handle. wh-override-
            # paste-focus-drift.1.2: cache the rejection-time process_id
            # so the retry handler can detect HWND reuse.
            target_hwnd, target_process_id = self._resolve_target_identity(
                context,
            )
            try:
                self._text_cache.put(
                    token, insertion_string,
                    target_hwnd=target_hwnd,
                    target_process_id=target_process_id,
                )
            except Exception as exc:
                logger.warning(
                    "RejectedInsertionStrategy: failed to store text in "
                    "cache: %s",
                    exc,
                )
                return
            self._aggregation_buckets[aggregation_key] = token

        # Opportunistically prune dead buckets so the map's size stays
        # bounded by the number of alive cache entries.
        self._prune_dead_buckets()

        process_id = int(getattr(context, "process_id", 0) or 0)
        # wh-b0sch: resolve the human-readable application name via Win32
        # GetFileVersionInfo. The resolver caches by process_id with a
        # 5-minute TTL and falls back to the executable basename without
        # ``.exe`` (e.g. ``zed`` for ``zed.exe``) when the lookup fails.
        # Older test fixtures construct the strategy without a resolver,
        # so the None branch falls back to the captured process_name.
        if self._app_name_resolver is not None:
            try:
                app_friendly_name = self._app_name_resolver.resolve(
                    process_id, process_name,
                )
            except Exception as exc:
                logger.warning(
                    "RejectedInsertionStrategy: app name resolver raised: %s",
                    exc,
                )
                app_friendly_name = process_name or "unknown"
        else:
            app_friendly_name = process_name or "unknown"

        event = TextTargetRejectedEvent(
            process_name=process_name,
            class_name=getattr(verdict, "class_name", "") or "",
            control_type=getattr(verdict, "control_type", "") or "",
            reason=getattr(verdict, "reason", "") or self.DEFAULT_REASON,
            supported_patterns=tuple(
                getattr(verdict, "supported_patterns", ()) or ()
            ),
            app_friendly_name=app_friendly_name,
            correlation_token=token,
        )
        try:
            self._response_queue.put(event.to_dict())
        except Exception as exc:
            logger.warning(
                "RejectedInsertionStrategy: failed to emit text_target_rejected: %s",
                exc,
            )
