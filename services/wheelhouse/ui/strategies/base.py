"""Base class for insertion strategies."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from ..context import UIContext


class InsertionMode(Enum):
    """How a strategy should compose and deliver the inserted text.

    DICTATION (default): the strategy runs TextPerfector against its own
    preceding-context source (shadow buffer, UIA TextPattern, clipboard
    gather), composes the perfected string, and delivers that. The
    retraction counter and shadow buffer update use the perfected length.
    This is the path streamed dictation has always taken.

    VERBATIM: the strategy delivers the provided text exactly, with no
    TextPerfector pass, no prefix space, no trailing space, no
    capitalization adjustment. Retraction accounting and shadow buffer
    updates use ``len(text)``. Used by callers that already composed
    the final text -- selection wrap (wrap_or_insert), case/style
    transformations (transform_selection), and any future caller that
    knows what should land in the target.
    """

    DICTATION = "dictation"
    VERBATIM = "verbatim"


@dataclass(frozen=True)
class InsertionOptions:
    """Per-call composition options for InsertionStrategy.insert.

    Structured so future composition concerns (e.g., a hypothetical
    no-shadow-buffer-update flag, or per-call timing overrides) can be
    added as new fields without touching every call site or every
    strategy signature.

    wh-iti5: introduced for the option-4 design that came out of the
    wh-9r0i (design review for routing wrap_or_insert / transform_selection)
    review loop.
    """

    mode: InsertionMode = InsertionMode.DICTATION


@dataclass(frozen=True)
class InsertionResult:
    """Outcome of an insertion attempt.

    success: True if the strategy delivered the perfected text and updated
        its accounting (shadow buffer, retraction counter). False if any
        step failed. Callers gate Schema A success/error on this.

    clipboard_dirty: True if the strategy wrote to the Windows clipboard
        as part of delivery (any verified_paste path or any direct
        pyperclip.copy). Used by UtteranceClipboardManager to decide
        whether end_utterance must restore the saved clipboard. Strategies
        that deliver via SendInput or via IPC to the terminal editor must
        set this False so a Unicode-only or terminal-only utterance does
        not clobber legitimate user clipboard changes (wh-4z4g9).
        Stays True even when ``success`` is False, because a failed
        clipboard paste can still leave dictated text on the clipboard
        (e.g. clipboard write succeeded but the post-send foreground
        check refused to credit). The manager must restore in that case.

    rejected_reason: telemetry token set by RejectedInsertionStrategy
        when the focused control is not a text target (wh-zndq, wh-fc1x).
        None for normal delivery results. When set, the result still
        reports success=True so the handler emits a Schema A response
        that resolves the caller's Future cleanly (no traceback noise),
        but the response carries PATH_INSERT_REJECTED instead of
        PATH_INSERT_VERIFIED so downstream paths can distinguish
        "delivered" from "intentional silent no-op". Pre-send refusal
        consumes no clipboard, no SendInput, and no shadow buffer
        update -- accumulated_paste_chars and the wh-0juh fail-closed
        flags stay untouched.

    retry_outcome: outcome token for the soft-fallback ClipboardOnlyStrategy
        path (wh-pc28, wh-9weum Phase 1). One of:

          - "n/a"        : default. Set by every strategy that is not
                           ClipboardOnlyStrategy. Logic process treats
                           this as "field does not apply" and falls
                           through to the existing success/failure
                           branches.
          - "verified"   : ClipboardOnlyStrategy delivered the text and
                           the post-paste foreground check confirmed it
                           landed on the captured target. The Phase 4
                           verified-retry counter increments only on
                           this value (wh-mv5ih).
          - "unverified" : ClipboardOnlyStrategy issued the paste at the
                           IPC level (clipboard write + Ctrl+V) but
                           verification was inconclusive -- typically a
                           verified_paste optimistic path or a post-paste
                           foreground mismatch. Treated as success at the
                           handler level (no Schema A error, no exception)
                           but does NOT count toward the verified-retry
                           threshold.

        UIActionHandler must NOT raise on (success=True, retry_outcome="
        unverified") -- the IPC succeeded and the caller's Future should
        resolve. The IPC response carries the field through to logic so
        the override-flow click counter can branch on it.
    """

    success: bool
    clipboard_dirty: bool
    rejected_reason: Optional[str] = None
    retry_outcome: str = "n/a"

    def __bool__(self) -> bool:
        return self.success

    @property
    def was_rejected(self) -> bool:
        """True if this result is a pre-send rejection (no work done)."""
        return self.rejected_reason is not None


class InsertionStrategy(ABC):
    """Abstract base class for text insertion strategies."""

    @abstractmethod
    def insert(
        self,
        insertion_string: str,
        context: UIContext,
        request_id: Optional[str] = None,
        options: Optional[InsertionOptions] = None,
    ) -> InsertionResult:
        """Insert text using this strategy.

        Args:
            insertion_string: Text to insert. In DICTATION mode this is
                the raw word from STT and the strategy runs TextPerfector
                over it. In VERBATIM mode this is the final text and the
                strategy must deliver it exactly.
            context: The captured UI context.
            request_id: Optional request ID for response tracking.
            options: Per-call composition options. None means use the
                defaults (DICTATION mode), preserving the historical
                contract for callers that have not been updated.

        Returns:
            InsertionResult: success flag plus a clipboard_dirty flag the
            handler forwards to UtteranceClipboardManager.
        """
        pass
