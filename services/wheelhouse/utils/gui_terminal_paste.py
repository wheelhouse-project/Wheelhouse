"""GUI-process verified-paste helper for terminal editor submission (wh-eolas).

Phase 3 of the wh-u3tj2 molecule moves submit-on-Enter into pure editor
responsibility. The editor (in the GUI process) sends finalized text to
the remembered terminal HWND directly, with no Logic-process strategy
involvement.

The original cross-process submit path enforced four safety
guarantees on the Input-process side; this helper enforces the same
guarantees in the GUI process so the migration does not regress
shell-paste safety:

1. Verify HWND existence AND foreground activation before any text
   delivery. Both ``IsWindow`` and the post-``SetForegroundWindow``
   foreground match must pass.
2. Use a verified-paste sequence that returns an explicit full-success
   result. We copy via pyperclip, poll the clipboard until it matches,
   then send Ctrl+V. Fire-and-forget is not acceptable: a partial
   delivery followed by Enter executes an unintended shell command.
3. Press Enter ONLY after verified full text delivery. Any abort path
   (HWND gone, foreground drift, copy failure, verification timeout,
   post-paste foreground drift) skips Enter and returns a non-success
   outcome.
4. Surface a content-neutral failure indication on every abort path.
   The helper returns the outcome; the caller emits the toast.

The helper does NOT touch the Logic-process strategy mirror or the
input-process click counter. Those are Phase 4 concerns (wh-1g6er).
"""

from __future__ import annotations

import enum
import logging
import time

import pyperclip
import win32con
import win32gui

from utils.win_input_sender import _send_modifier_keyups, verified_press_keys

logger = logging.getLogger(__name__)


class PasteOutcome(enum.Enum):
    """Result of :func:`paste_into_terminal`.

    SUCCESS means the clipboard write was verified, Ctrl+V was sent,
    the post-paste foreground check matched the target HWND, and the
    Enter keystroke was sent.

    Every other value means Enter was NOT sent. The caller MUST treat
    any non-SUCCESS value as an abort and surface a failure toast.

    wh-eolas.1.1 adds ``PRE_PASTE_FOREGROUND_DRIFT``: clipboard
    verification succeeded but the foreground HWND drifted before the
    Ctrl+V keystroke was sent. Distinct from ``FOREGROUND_FAILED``
    (which fires on the initial SetForegroundWindow result) so the
    structured-log surface and downstream consumers can tell the two
    cases apart. Distinct from ``POST_PASTE_FOREGROUND_DRIFT`` because
    NO Ctrl+V was sent in this case -- the dictation text never reached
    the rogue foreground window.

    wh-eolas.1.2 adds ``SENDINPUT_PARTIAL``: SendInput accepted fewer
    events than the chord required, so the user-visible result of the
    Ctrl+V or Enter is undefined. Enter is NOT sent after a partial
    Ctrl+V and a partial Enter is NOT reported as SUCCESS.
    """

    SUCCESS = "success"
    INVALID_HWND = "invalid_hwnd"
    FOREGROUND_FAILED = "foreground_failed"
    CLIPBOARD_COPY_FAILED = "clipboard_copy_failed"
    CLIPBOARD_VERIFY_FAILED = "clipboard_verify_failed"
    PRE_PASTE_FOREGROUND_DRIFT = "pre_paste_foreground_drift"
    POST_PASTE_FOREGROUND_DRIFT = "post_paste_foreground_drift"
    SENDINPUT_PARTIAL = "sendinput_partial"
    EXCEPTION = "exception"


# Verification poll: matches the Input-process timing budget (250 ms,
# 5 ms poll). The GUI process budget can afford the same since the
# editor is already shown and the user is waiting on Enter.
_VERIFY_TIMEOUT_S = 0.25
_VERIFY_POLL_S = 0.005

# Post-SetForegroundWindow settle delay. The legacy path uses 200 ms;
# we match for parity so the user-observable wait is identical.
_FOREGROUND_SETTLE_S = 0.2

# Post-paste settle before Enter. Matches the legacy path's default
# (100 ms). The GUI process has no per-target config to read, so we use
# the same default the user has implicitly accepted via the legacy path.
_POST_PASTE_SETTLE_S = 0.1


def paste_into_terminal(text: str, terminal_hwnd: int) -> PasteOutcome:
    """Paste ``text`` into ``terminal_hwnd`` and press Enter on success.

    Safety contract:

    * HWND must exist (``win32gui.IsWindow``) before any work begins.
    * Foreground activation must succeed -- iconic windows are restored,
      then ``SetForegroundWindow`` is called and the foreground HWND is
      compared to the target before continuing.
    * Clipboard copy must succeed AND the verification loop must
      observe our text on the clipboard. A copy that succeeds but
      cannot be verified is treated as failure -- this is more strict
      than the Input-process optimistic-paste fallback, but the
      terminal-submit case is high-risk enough to refuse a paste we
      could not confirm.
    * Post-paste foreground match is re-checked before Enter; a
      foreground drift between Ctrl+V and Enter means the keystroke
      could land in a different window.
    * The original clipboard contents are saved at entry and restored
      at exit (success or abort) so we do not pollute the user's
      clipboard.

    Returns:
        :class:`PasteOutcome` describing what happened. Only
        ``PasteOutcome.SUCCESS`` means Enter was sent.
    """
    if not terminal_hwnd:
        logger.error("paste_into_terminal: terminal_hwnd is 0/None")
        return PasteOutcome.INVALID_HWND

    try:
        if not win32gui.IsWindow(terminal_hwnd):
            logger.error(
                "paste_into_terminal: terminal HWND %d no longer exists",
                terminal_hwnd,
            )
            return PasteOutcome.INVALID_HWND
    except Exception as exc:
        logger.error(
            "paste_into_terminal: IsWindow raised for hwnd=%d: %s",
            terminal_hwnd, exc,
        )
        return PasteOutcome.INVALID_HWND

    saved_clipboard: str | None = None
    try:
        try:
            saved_clipboard = pyperclip.paste()
        except Exception as exc:
            logger.warning(
                "paste_into_terminal: clipboard save failed: %s", exc,
            )
            saved_clipboard = None

        try:
            if win32gui.IsIconic(terminal_hwnd):
                win32gui.ShowWindow(terminal_hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(terminal_hwnd)
        except Exception as exc:
            logger.error(
                "paste_into_terminal: SetForegroundWindow raised "
                "(hwnd=%d): %s",
                terminal_hwnd, exc,
            )
            return PasteOutcome.FOREGROUND_FAILED

        time.sleep(_FOREGROUND_SETTLE_S)

        try:
            actual_fg = win32gui.GetForegroundWindow()
        except Exception as exc:
            logger.error(
                "paste_into_terminal: GetForegroundWindow raised: %s", exc,
            )
            return PasteOutcome.FOREGROUND_FAILED
        if int(actual_fg) != int(terminal_hwnd):
            logger.error(
                "paste_into_terminal: foreground activation failed "
                "expected=%d actual=%d",
                terminal_hwnd, actual_fg,
            )
            return PasteOutcome.FOREGROUND_FAILED

        try:
            pyperclip.copy(text)
        except Exception as exc:
            logger.error(
                "paste_into_terminal: clipboard copy failed: %s", exc,
            )
            return PasteOutcome.CLIPBOARD_COPY_FAILED

        # Verify the clipboard now holds our text. A partial copy or
        # racing writer means we cannot trust Ctrl+V to deliver the
        # text we asked for.
        verified = False
        deadline = time.perf_counter() + _VERIFY_TIMEOUT_S
        while time.perf_counter() < deadline:
            try:
                current = pyperclip.paste()
            except Exception:
                current = None
            if current == text:
                verified = True
                break
            time.sleep(_VERIFY_POLL_S)
        if not verified:
            logger.error(
                "paste_into_terminal: clipboard verification timed out "
                "(text_len=%d, hwnd=%d)",
                len(text), terminal_hwnd,
            )
            return PasteOutcome.CLIPBOARD_VERIFY_FAILED

        # wh-eolas.1.1: re-read foreground IMMEDIATELY before Ctrl+V.
        # Clipboard verification spent up to 250 ms polling; during that
        # window a toast, notification, security prompt, or other
        # foreground steal could have taken over. The original
        # post-paste check only prevented Enter; without this pre-paste
        # check the dictation text leaks into whatever stole foreground.
        try:
            pre_paste_fg = win32gui.GetForegroundWindow()
        except Exception as exc:
            logger.error(
                "paste_into_terminal: pre-paste GetForegroundWindow "
                "raised: %s", exc,
            )
            return PasteOutcome.PRE_PASTE_FOREGROUND_DRIFT
        if int(pre_paste_fg) != int(terminal_hwnd):
            logger.error(
                "paste_into_terminal: pre-paste foreground drift "
                "expected=%d actual=%d -- skipping Ctrl+V and Enter",
                terminal_hwnd, pre_paste_fg,
            )
            return PasteOutcome.PRE_PASTE_FOREGROUND_DRIFT

        # wh-eolas.1.2: verified_press_keys returns (success, accepted,
        # expected) so a partial SendInput delivery fails closed.
        # A partial Ctrl+V followed by Enter is unsafe: the dictation
        # text may not have landed, but Enter would still execute
        # whatever the shell prompt contained.
        ctrl_v_ok, ctrl_v_accepted, ctrl_v_expected = verified_press_keys(
            "ctrl", "v",
        )
        if not ctrl_v_ok:
            logger.error(
                "paste_into_terminal: Ctrl+V SendInput short delivery "
                "sent=%d/%d -- skipping Enter",
                ctrl_v_accepted, ctrl_v_expected,
            )
            # wh-eolas.2.5: a partial Ctrl+V leaves Ctrl physically held
            # in the Windows keyboard state. Release every modifier in
            # the chord (reverse order) so the next keystroke from any
            # source is not interpreted as Ctrl+<key>.
            _send_modifier_keyups(("ctrl",))
            return PasteOutcome.SENDINPUT_PARTIAL
        time.sleep(_POST_PASTE_SETTLE_S)

        # Post-paste foreground check. The legacy path performs this
        # in ``verified_paste`` so a popup that grabbed foreground
        # between SetForegroundWindow and Ctrl+V landing does not get
        # an unintended keystroke. The GUI editor case is symmetric.
        try:
            post_fg = win32gui.GetForegroundWindow()
        except Exception as exc:
            logger.error(
                "paste_into_terminal: post-paste GetForegroundWindow "
                "raised: %s", exc,
            )
            return PasteOutcome.POST_PASTE_FOREGROUND_DRIFT
        if int(post_fg) != int(terminal_hwnd):
            logger.error(
                "paste_into_terminal: post-paste foreground drift "
                "expected=%d actual=%d -- skipping Enter",
                terminal_hwnd, post_fg,
            )
            return PasteOutcome.POST_PASTE_FOREGROUND_DRIFT

        # wh-eolas.1.2: same verified semantics on the Enter keystroke.
        # A short SendInput on Enter would leave the command pasted but
        # never submitted; reporting SUCCESS in that case would mislead
        # the LogicMirror into SUBMIT_COMPLETE and the user into thinking
        # the command ran.
        enter_ok, enter_accepted, enter_expected = verified_press_keys(
            "enter",
        )
        if not enter_ok:
            logger.error(
                "paste_into_terminal: Enter SendInput short delivery "
                "sent=%d/%d",
                enter_accepted, enter_expected,
            )
            # wh-eolas.2.5: a partial Enter could leave Enter physically
            # held. Release it so a stuck Enter-down does not affect
            # subsequent input. Enter is not a modifier but the same
            # recovery shape applies.
            _send_modifier_keyups(("enter",))
            return PasteOutcome.SENDINPUT_PARTIAL

        logger.info(
            "paste_into_terminal: success hwnd=%d text_len=%d",
            terminal_hwnd, len(text),
        )
        return PasteOutcome.SUCCESS
    except Exception as exc:
        logger.error(
            "paste_into_terminal: unexpected exception: %s", exc,
            exc_info=True,
        )
        return PasteOutcome.EXCEPTION
    finally:
        if saved_clipboard is not None:
            try:
                pyperclip.copy(saved_clipboard)
            except Exception as exc:
                logger.warning(
                    "paste_into_terminal: clipboard restore failed: %s",
                    exc,
                )
