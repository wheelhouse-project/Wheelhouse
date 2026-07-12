"""Router for selecting insertion strategies.

Decision tree (in order):

    1. context.is_flutter True?
         -> FlutterStrategy

    2. Text-target predicate (wh-zndq, wh-fc1x, wh-9weum Phase 1,
       wh-soft-allow-verdict-tier):
         The shared TextTargetPredicate decides whether the focused
         control accepts text input. The verdict's reason field
         determines routing across four tiers:

         a. verdict=True, reason='accept_soft_allow_tuple'
              -> ClipboardOnlyStrategy. The user has previously
              approved this (process, class, control_type) tuple via
              the three-strikes grant prompt; the router keeps the
              silent paste behaviour that approval implied. Falls
              through to the default branch when clipboard_only is
              not wired (legacy fixtures only).

         b. verdict=True, any other accept reason
              -> If class_name is in BROKEN_INPUT_PIPELINE_CLASSES
              (the Win11 modern Notepad RichEditD2DPT and any other
              control known to mishandle KEYEVENTF_UNICODE
              SendInput), return StandardStrategy unconditionally.
              StandardStrategy uses clipboard paste under the hood so
              the per-keystroke race in the target's input pipeline
              is bypassed. wh-notepad-clipboard-workaround documents
              the affected controls and the AutoHotkey-community
              evidence behind the list. Otherwise fall through to the
              default length-based branch.

         c. verdict=False, reason='default_reject_paste_capable_class'
              -> RejectedInsertionStrategy with the verdict set via
              set_pending_verdict. The strategy emits a
              text_target_rejected event that surfaces the rejection
              toast with the Try-it-anyway button. This is the entry
              point for the wh-9weum Phase 4 override flow. Earlier
              wiring routed this reason directly to ClipboardOnly,
              which silently pasted and never surfaced the toast in
              production -- the wh-prio bug, fixed by
              wh-soft-allow-verdict-tier.

         d. verdict=False, any other reject reason
              -> RejectedInsertionStrategy. Deliberate no-op: no
              SendInput, no clipboard write, no shadow buffer update.
              Includes default_reject (the wh-zndq trap and the empty-
              ClassName non-browser case), denylist hits, stale_com,
              not_focusable, no_focused_control.

         When the router is constructed without a predicate (legacy
         test fixtures) this step is skipped and routing falls back
         to the older focusable-only check.

    3. Default (normal app, focusable control):
         a. VerifiedUnicodeStrategy is configured AND
            ``insertion_string`` length is <= verified_unicode_max_chars
              -> VerifiedUnicodeStrategy (SendInput, no clipboard write)
         b. Otherwise
              -> StandardStrategy (ShadowBuffer or ClipboardFallback)

The Unicode branch only applies to the default path (wh-606yk). Flutter
routing is unchanged because Flutter apps need SendKeys for the
framework's input quirks.

wh-1g6er: the terminal-editor branch is gone. The focus-redirect path
opens an empty editor via ``UIActionHandler.open_editor_for_redirect``;
once the editor is up the focused control is its QPlainTextEdit, which
exposes UIA TextPattern, so the predicate accepts and the default
length-based branch picks Standard / VerifiedUnicode.

Predicate ordering: flutter runs BEFORE the text-target predicate so
flutter targets keep their existing per-app strategy path and are not
subject to the generic UIA TextPattern check (resolved during wh-ix1z
round 1).
"""
import logging
from typing import Optional

from .context import UIContext
from .strategies.base import InsertionStrategy
from .text_target import TextTargetPredicate, TextTargetVerdict

logger = logging.getLogger(__name__)


# wh-notepad-clipboard-workaround: control class names whose target
# applications are known to mishandle KEYEVENTF_UNICODE SendInput. For
# these controls the router forces StandardStrategy (clipboard paste)
# regardless of insertion length, bypassing the per-keystroke race in
# the target's input pipeline.
#
# RichEditD2DPT is the Windows 11 modern Notepad's Direct2D plain-text
# RichEdit control. The AutoHotkey community has independently
# documented the same bug: the first 1-2 characters of a SendInput
# burst land, the rest get buffered in Notepad's message queue until a
# modifier-key or mouse-move event triggers a flush, and the flush
# often delivers them out of order. WordPad, Word, OneNote, Notepad++,
# and Visual Studio all use different RichEdit variants and do not
# share the bug. See wh-startup-trailing-corruption (the parent
# investigation) for the WheelHouse-side reproduction and the
# wh-notepad-clipboard-workaround bead for the design notes.
BROKEN_INPUT_PIPELINE_CLASSES: frozenset[str] = frozenset({
    "RichEditD2DPT",
})


class InsertionRouter:
    """Decides which insertion strategy to use based on context."""

    def __init__(
        self,
        standard_strategy: InsertionStrategy,
        flutter_strategy: InsertionStrategy,
        simple_paste_strategy: InsertionStrategy,
        rejected_strategy: Optional[InsertionStrategy] = None,
        text_target_predicate: Optional[TextTargetPredicate] = None,
        verified_unicode_strategy: Optional[InsertionStrategy] = None,
        verified_unicode_max_chars: int = 50,
        clipboard_only_strategy: Optional[InsertionStrategy] = None,
    ):
        """Wire the available strategies and the Unicode threshold.

        Args:
            rejected_strategy: Pre-send refusal strategy used when the
                shared text-target predicate rejects the focused control
                (wh-zndq). When None, the router skips the predicate
                check entirely and falls back to the legacy
                focusable-only fast path -- this preserves older test
                fixtures that have not been updated and lets the router
                continue to function if construction fails to wire the
                predicate.
            text_target_predicate: Shared text-target predicate. When
                None, the predicate check is skipped (see above). Tests
                inject stub predicates to exercise specific routing
                paths.
            verified_unicode_strategy: Optional Unicode-delivery strategy
                (wh-9jml6). When None, the router never selects it and
                the default path always returns StandardStrategy.
            verified_unicode_max_chars: Inclusive upper bound on the
                ``insertion_string`` length for Unicode routing
                (wh-606yk). Strings longer than this drop to
                StandardStrategy's clipboard path because partial
                SendInput delivery becomes more likely as the event
                count grows.
            clipboard_only_strategy: Silent-paste strategy for the
                soft-allow accept tier. Selected when the predicate
                returns verdict=True with
                reason='accept_soft_allow_tuple' -- the user has
                previously approved this (process, class,
                control_type) triple via the three-strikes grant
                prompt and keeps the silent Ctrl+V paste behaviour.
                When None, the accept_soft_allow_tuple verdict falls
                through to the default length-based branch
                (VerifiedUnicodeStrategy / StandardStrategy) so older
                fixtures that have not added the strategy continue to
                work. Unknown soft rejects (reason
                'default_reject_paste_capable_class') always route to
                rejected_strategy regardless of this argument so the
                Try-it-anyway override flow can run.
        """
        self.standard = standard_strategy
        self.flutter = flutter_strategy
        self.simple_paste = simple_paste_strategy
        self.rejected = rejected_strategy
        self.text_target = text_target_predicate
        self.verified_unicode = verified_unicode_strategy
        self.verified_unicode_max_chars = verified_unicode_max_chars
        self.clipboard_only = clipboard_only_strategy

    def get_strategy(
        self,
        context: UIContext,
        insertion_string: Optional[str] = None,
    ) -> InsertionStrategy:
        """Select the appropriate strategy for the given context.

        Args:
            context: The captured UI context.
            insertion_string: The text about to be inserted. Used only by
                the default branch's Unicode-vs-Standard decision; other
                branches ignore it. May be None when the caller does not
                yet know the text (e.g. a hypothetical pre-routing pass);
                in that case the default branch falls back to
                StandardStrategy because the length check cannot run.

        Returns:
            The selected InsertionStrategy. See the module docstring for
            the full decision tree.
        """
        # 1. Flutter? -> Flutter Strategy. Runs before the text-target
        #    predicate because Flutter's text controls do not always
        #    expose UIA TextPattern, so the per-framework
        #    FlutterStrategy must keep priority over the predicate.
        if context.is_flutter:
            logger.debug("Router: Flutter detected -> FlutterStrategy")
            return self.flutter

        # 2. Text-target predicate (wh-zndq, wh-fc1x, wh-9weum Phase 1,
        #    wh-soft-allow-verdict-tier). When configured, the
        #    predicate is the single source of truth for "is this a
        #    text-input target". The verdict's reason field decides
        #    routing across four tiers:
        #
        #      * verdict=True, accept_soft_allow_tuple
        #          -> ClipboardOnlyStrategy (silent paste for the
        #          user-approved tuple). Falls through to the default
        #          branch when clipboard_only is not wired.
        #      * verdict=True, any other reason
        #          -> fall through to the default branch.
        #      * verdict=False, default_reject_paste_capable_class
        #          -> RejectedInsertionStrategy with set_pending_verdict
        #          (rejection toast + Try-it-anyway button).
        #      * verdict=False, any other reason
        #          -> RejectedInsertionStrategy (hard refuse).
        #
        #    Earlier wiring routed default_reject_paste_capable_class
        #    directly to ClipboardOnly, which silently pasted and
        #    never surfaced the toast in production (the wh-prio bug).
        #    The soft-allow accept tier now owns silent paste and
        #    rejection routing owns the toast, so the override flow is
        #    reachable end-to-end.
        if self.text_target is not None and self.rejected is not None:
            verdict = self.text_target.evaluate(
                context.focused_control,
                class_name=getattr(context, "class_name", "") or "",
                process_name=getattr(context, "process_name", "") or "",
            )
            if not verdict.verdict:
                self._log_rejection(verdict)
                # wh-7318z: hand the verdict to the strategy so it can
                # emit a structured text_target_rejected event during
                # insert. The strategy ignores the call when it was
                # constructed without a response_queue or text_cache
                # (legacy test fixtures).
                set_pending = getattr(
                    self.rejected, "set_pending_verdict", None,
                )
                if callable(set_pending):
                    set_pending(verdict)
                return self.rejected
            # Accept branch. The soft-allow accept tier routes to
            # ClipboardOnly so the approved target keeps the silent
            # paste behaviour the user opted in to. Every other accept
            # reason falls through to the default length-based branch
            # unless the broken-input-pipeline workaround intercepts.
            if (
                verdict.reason == "accept_soft_allow_tuple"
                and self.clipboard_only is not None
            ):
                logger.debug(
                    "Router: soft-allow accept -> ClipboardOnlyStrategy "
                    "(class=%s control_type=%s process=%s)",
                    verdict.class_name or "?",
                    verdict.control_type or "?",
                    verdict.process_name or "?",
                )
                return self.clipboard_only
            # wh-notepad-clipboard-workaround: bypass the per-keystroke
            # SendInput path for control classes whose target apps are
            # known to mishandle KEYEVENTF_UNICODE. Forces clipboard
            # paste regardless of insertion length. The check runs only
            # on the accept branch so the soft-reject and hard-reject
            # paths are unchanged.
            if (
                getattr(context, "class_name", "")
                in BROKEN_INPUT_PIPELINE_CLASSES
            ):
                logger.debug(
                    "Router: broken_input_pipeline_workaround "
                    "(class=%s process=%s) -> StandardStrategy",
                    verdict.class_name or "?",
                    verdict.process_name or "?",
                )
                return self.standard
        else:
            # Legacy path (no predicate wired). Preserve the older
            # focusable-only check so existing tests keep their meaning.
            legacy = self._legacy_focusable_check(context)
            if legacy is not None:
                return legacy

        # 3. Default branch: short text in normal apps prefers the Unicode
        #    SendInput path (wh-606yk). Long text continues through
        #    StandardStrategy's clipboard pipeline because partial-send
        #    risk and verification cost both rise with event count, and
        #    the long-text use case is typically a paste-style insertion
        #    where clipboard semantics are acceptable.
        if (
            self.verified_unicode is not None
            and insertion_string is not None
            and len(insertion_string) <= self.verified_unicode_max_chars
        ):
            logger.debug(
                "Router: short text (len=%d <= %d) -> VerifiedUnicodeStrategy",
                len(insertion_string), self.verified_unicode_max_chars,
            )
            return self.verified_unicode

        logger.debug("Router: Default -> StandardStrategy")
        return self.standard

    def _legacy_focusable_check(
        self, context: UIContext,
    ) -> Optional[InsertionStrategy]:
        """Pre-predicate focusable check used when no predicate is wired.

        Returns SimplePasteStrategy when the focused control is missing,
        unfocusable, or the focusable read raises. Returns None when the
        control passed the legacy check and routing should continue.

        This branch is preserved only for legacy callers (older tests)
        that construct the router without a TextTargetPredicate. The
        production wiring always supplies one and this branch never runs.
        """
        if not context.focused_control:
            logger.debug("Router: No focusable control -> SimplePasteStrategy")
            return self.simple_paste
        try:
            is_focusable = context.focused_control.IsKeyboardFocusable
        except Exception:
            logger.debug("Router: Stale control (COM error) -> SimplePasteStrategy")
            return self.simple_paste
        if not is_focusable:
            logger.debug("Router: Control not focusable -> SimplePasteStrategy")
            return self.simple_paste
        return None

    @staticmethod
    def _log_rejection(verdict: TextTargetVerdict) -> None:
        """DEBUG-level rejection log with telemetry fields.

        Logged once per rejected dictation. Per the round-1 design
        baseline (wh-ix1z.1) and the wh-ix1z.7 round-2 finding, routine
        rejections must NOT log at INFO -- background speech and
        repeated dictation while focus is on a non-text control would
        produce a wall of INFO records with process / class / control
        telemetry. DEBUG keeps the trace available for diagnostics
        without flooding the production log.

        A future rate-limited diagnostic mode (config flag, e.g.
        ui_actions.text_target.rejection_diagnostics_seconds = N) can
        elevate to INFO for an N-second window when the user is
        actively investigating a routing issue. Out of scope here.
        """
        logger.debug(
            "Router: rejected text target -- reason=%s control_type=%s "
            "class=%s process=%s patterns=%s",
            verdict.reason,
            verdict.control_type or "?",
            verdict.class_name or "?",
            verdict.process_name or "?",
            ",".join(verdict.supported_patterns) if verdict.supported_patterns else "-",
        )
