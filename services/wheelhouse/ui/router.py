"""Router for selecting insertion strategies.

Decision tree (in order):

    1. Elevation check (wh-elevated-target-notice): when the optional
       elevation checker is wired, targets owned by a higher-integrity
       (administrator) process are refused before EVERY other branch,
       including the Flutter early return -- Windows UIPI discards
       input by process integrity, not UI framework, so an elevated
       Flutter app fails exactly like an elevated native app
       (wh-elevated-target-notice.1.1). The router synthesizes a
       verdict with reason 'elevated_process_window' and routes to
       RejectedInsertionStrategy -- ahead of the soft-allow
       silent-paste tier, and without consulting the predicate.
       "not_elevated"/"unknown"/exception all fail open into the
       branches below.

    2. context.is_flutter True?
         -> FlutterStrategy

    3. Text-target predicate (wh-zndq, wh-fc1x, wh-9weum Phase 1,
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

         c. verdict=False, ANY reject reason, and the context's
            captured target identity is NOT the empty record
              -> ClipboardOnlyStrategy (wh-paste-when-unverified.2).
              The words are pasted with Ctrl+V instead of dropped.
              Covers default_reject (the wh-zndq trap and the empty-
              ClassName non-browser case), denylist hits, stale_com,
              not_focusable, no_focused_control, and the soft reject
              default_reject_paste_capable_class. The reject never
              reaches the default length-based branch below: that
              branch sends keystrokes for short text, and keystrokes
              into a browser page body are the one recorded harm here
              (wh-fc1x.1, one page scroll per word in Brave).
              set_pending_verdict is NOT called on this path.

         c2. verdict=False and the captured target identity EQUALS a
             fresh TargetIdentity()
              -> RejectedInsertionStrategy, WITHOUT set_pending_verdict,
              so the drop is silent (DEBUG only, no notice,
              success=True, rejected_reason set). An all-zero identity
              fails is_current() on its first line, so verified_paste
              would refuse the paste before the send every time and log
              that refusal at ERROR -- and an ERROR record IS a Windows
              notification. Only the EMPTY record takes this route; an
              identity captured properly that then went stale keeps the
              paste and its ERROR refusal.

         d. verdict=False and clipboard_only is None (legacy fixtures)
              -> RejectedInsertionStrategy with the verdict set via
              set_pending_verdict. Deliberate no-op: no SendInput, no
              clipboard write, no shadow buffer update. The strategy
              emits the text_target_rejected event that surfaces the
              rejection toast with the Try-it-anyway button (the
              wh-9weum Phase 4 override flow). Production wires
              clipboard_only, so this path does not run there.

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
from typing import Callable, Optional

from .context import UIContext
from .strategies.base import InsertionStrategy
from .target_identity import TargetIdentity
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
        elevation_checker: Optional[Callable[[object], str]] = None,
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
                work. Since wh-paste-when-unverified.2 this strategy
                also serves EVERY reject reason the predicate returns:
                the words are pasted rather than dropped. When None,
                those rejects fall back to rejected_strategy.
            elevation_checker: Optional callable taking the focused
                control and returning one of "elevated",
                "not_elevated", or "unknown"
                (wh-elevated-target-notice; production wiring passes
                ``ui.elevation_check.target_elevation_state``). When it
                returns "elevated" the router refuses BEFORE the
                text-target predicate and before the soft-allow silent
                paste tier, synthesizing a verdict with reason
                'elevated_process_window' -- Windows UIPI would
                silently discard the input and both delivery paths
                would record a false verified success. Any other
                return value, an exception, or None (not wired) leaves
                routing unchanged (fail open).
        """
        self.standard = standard_strategy
        self.flutter = flutter_strategy
        self.simple_paste = simple_paste_strategy
        self.rejected = rejected_strategy
        self.text_target = text_target_predicate
        self.verified_unicode = verified_unicode_strategy
        self.verified_unicode_max_chars = verified_unicode_max_chars
        self.clipboard_only = clipboard_only_strategy
        self.elevation_checker = elevation_checker

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
        # 1. Elevation check (wh-elevated-target-notice): refuse
        #    elevated targets before EVERY other branch, including the
        #    Flutter early return (wh-elevated-target-notice.1.1) and
        #    the soft-allow silent-paste tier. Windows UIPI silently
        #    discards input sent to a higher-integrity window while
        #    SendInput reports success, so an approved control
        #    relaunched as administrator must not take the silent-paste
        #    path, and an elevated Flutter app must not take the
        #    Flutter path -- UIPI filters on process integrity, not UI
        #    framework. The check does not consult the predicate: UIA
        #    visibility into elevated windows is unreliable, and its
        #    answer could not change the routing. Anything other than
        #    "elevated" (including an exception) fails open into the
        #    existing pipeline.
        if self.elevation_checker is not None and self.rejected is not None:
            try:
                elevation_state = self.elevation_checker(
                    context.focused_control,
                )
            except Exception as e:
                logger.debug(
                    "Router: elevation check raised (%s); "
                    "failing open", e,
                )
                elevation_state = "unknown"
            if elevation_state == "elevated":
                verdict = TextTargetVerdict(
                    verdict=False,
                    reason="elevated_process_window",
                    class_name=getattr(context, "class_name", "") or "",
                    process_name=(
                        getattr(context, "process_name", "") or ""
                    ),
                )
                self._log_rejection(verdict)
                set_pending = getattr(
                    self.rejected, "set_pending_verdict", None,
                )
                if callable(set_pending):
                    set_pending(verdict)
                return self.rejected

        # 2. Flutter? -> Flutter Strategy. Runs before the text-target
        #    predicate because Flutter's text controls do not always
        #    expose UIA TextPattern, so the per-framework
        #    FlutterStrategy must keep priority over the predicate.
        if context.is_flutter:
            logger.debug("Router: Flutter detected -> FlutterStrategy")
            return self.flutter

        # 3. Text-target predicate (wh-zndq, wh-fc1x, wh-9weum Phase 1,
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
        #      * verdict=False, any reason, captured identity not the
        #        empty record
        #          -> ClipboardOnlyStrategy: paste the words with
        #          Ctrl+V rather than drop them
        #          (wh-paste-when-unverified.2).
        #      * verdict=False and the captured identity IS the empty
        #        record
        #          -> RejectedInsertionStrategy with no pending
        #          verdict: a silent drop, because the paste would be
        #          refused pre-send and that refusal logs at ERROR,
        #          which is a Windows notification.
        #      * verdict=False and clipboard_only is None
        #          -> RejectedInsertionStrategy with set_pending_verdict
        #          (legacy fixtures; production always wires
        #          clipboard_only).
        if self.text_target is not None and self.rejected is not None:
            verdict = self.text_target.evaluate(
                context.focused_control,
                class_name=getattr(context, "class_name", "") or "",
                process_name=getattr(context, "process_name", "") or "",
            )
            if not verdict.verdict:
                self._log_rejection(verdict)
                # wh-paste-when-unverified.2: a reject no longer drops
                # the words. ClipboardOnlyStrategy pastes them with
                # Ctrl+V, which is the delivery path with no recorded
                # harm against a non-text control -- the one recorded
                # harm here is KEYSTROKES into a browser page body
                # (wh-fc1x.1, one page scroll per word in Brave), and
                # the default length-based branch below is what sends
                # them. Returning here keeps every reject away from it.
                # set_pending_verdict is deliberately NOT called on
                # this path: RejectedInsertionStrategy is not the
                # strategy being returned, and a verdict parked on it
                # would be consumed by a later insert that really does
                # route there (the elevated refusal above).
                #
                # wh-paste-when-unverified.2, boss hard gate finding 1:
                # a reject whose CAPTURED IDENTITY IS THE EMPTY RECORD
                # is dropped silently instead of pasted. ui/context.py
                # captures an identity on every context, and
                # capture_target_identity returns the all-zero
                # TargetIdentity() -- never None -- both when there is
                # no focused control and when the control's reads
                # raise. All seven fields are 0, so
                # TargetIdentity.is_current() returns False on its
                # FIRST line with no Windows call: verified_paste would
                # refuse this paste before the send every time, not
                # sometimes, and log that refusal at ERROR. An ERROR
                # record IS a Windows notification
                # (ErrorNotificationHandler sits on the root logger),
                # and a second one followed on the letter-buffer path
                # because ClipboardOnlyStrategy sets no
                # rejected_reason. Returning self.rejected with NO
                # pending verdict gives the four properties wanted
                # here at once: DEBUG only, no notice, success=True (so
                # raw_insert_text does not raise), and rejected_reason
                # set (so end_utterance takes its WARNING arm).
                #
                # Emptiness is tested by EQUALITY against a fresh
                # TargetIdentity(), never by calling is_current():
                # equality needs no Windows call, so the guard itself
                # cannot fail or flake. None is NOT the empty record --
                # legacy fixtures build a UIContext without an identity
                # and keep the paste above. An identity that was
                # captured properly and THEN went stale also keeps the
                # paste and its existing ERROR refusal, because a
                # genuinely stale target is a real failure worth
                # reporting. Only the empty record becomes a silent
                # drop.
                captured_identity = getattr(context, "target_identity", None)
                if captured_identity == TargetIdentity():
                    logger.debug(
                        "Router: rejected text target with an EMPTY "
                        "captured identity -> RejectedInsertionStrategy "
                        "(silent drop; a paste would be refused "
                        "pre-send and logged at ERROR) reason=%s "
                        "class=%s process=%s",
                        verdict.reason,
                        verdict.class_name or "?",
                        verdict.process_name or "?",
                    )
                    return self.rejected
                if self.clipboard_only is not None:
                    logger.debug(
                        "Router: unverified text target -> "
                        "ClipboardOnlyStrategy (paste, not drop) "
                        "reason=%s class=%s process=%s",
                        verdict.reason,
                        verdict.class_name or "?",
                        verdict.process_name or "?",
                    )
                    return self.clipboard_only
                # No ClipboardOnlyStrategy wired (legacy fixtures
                # only). Keep the pre-wh-paste-when-unverified refusal.
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
