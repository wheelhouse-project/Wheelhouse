"""Window focus management and activation.

This module handles window activation, focus restoration, and HWND tracking
for ensuring UI operations target the correct window.
"""
import logging
import win32gui
import win32con
from typing import Iterable, Optional

from .hwnd_utils import (
    hwnd_is_invisible_or_toolwindow,
    normalize_hwnd_for_foreground_compare,
    process_identity_for_hwnd,
    same_process_fallback_matches,
)

logger = logging.getLogger(__name__)


class WindowFocusManager:
    """Manages window focus and activation state.

    Tracks target window handles and ensures proper focus for UI operations.
    Handles minimized windows, maximized state preservation, and focus retries.
    """

    def __init__(
        self,
        same_process_browser_names: Optional[Iterable[str]] = None,
    ):
        """Initialize the window focus manager.

        Args:
            same_process_browser_names: lower-cased exe names eligible for
                the wh-ensure-focused-same-process-fallback relaxation in
                ``ensure_focused``. Production passes the resolved
                ``[ui_actions.foreground_check].same_process_browser_names``
                set (see ``hwnd_utils.resolve_same_process_browser_names``);
                the default keeps the strict GA_ROOT-only contract so bare
                constructions (tests, tools) never relax anything. The set
                is passed in rather than read from config here so this
                module keeps no config dependency.
        """
        self._last_target_hwnd: Optional[int] = None
        self._same_process_browser_names: frozenset[str] = frozenset(
            name.lower() for name in (same_process_browser_names or ())
        )

    def ensure_focused(self, hwnd: int) -> bool:
        """Ensure the specified window is focused and active.

        Handles:
        - Restoring minimized windows (preserves maximized state)
        - Setting foreground focus with retry logic
        - Graceful error handling

        Args:
            hwnd: Window handle to focus

        Returns:
            bool: True if window was successfully focused, False otherwise
        """
        if not hwnd:
            return False

        try:
            # wh-ensure-focused-same-process-fallback.1.10: resolve the
            # target's GA_ROOT once, before any window manipulation.
            # The two _foreground_matches invocations below each
            # re-normalize the handle, and Windows can recycle the
            # numeric HWND between them -- a destroyed helper reborn as
            # a child of the foreground sibling re-normalizes to the
            # sibling's root, and the second invocation's strict
            # equality then credits the sibling without any fallback
            # probe running. Every invocation checks the live root
            # against this snapshot and refuses on drift. An
            # unresolvable snapshot refuses up front: manipulating
            # foreground on behalf of a handle whose identity cannot
            # be pinned is the fail-open the snapshot removes.
            expected_root_snapshot = normalize_hwnd_for_foreground_compare(
                hwnd,
            )
            if expected_root_snapshot is None:
                return False

            # Restore only if minimized; keep maximized windows maximized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            win32gui.SetForegroundWindow(hwnd)

            # Verify without delay; retry once if not active
            if self._foreground_matches(hwnd, expected_root_snapshot):
                return True
            win32gui.SetForegroundWindow(hwnd)

            # wh-override-paste-focus-drift.1.1: report whether foreground
            # actually changed. SetForegroundWindow does not raise on the
            # common Windows restriction where the target thread has not
            # received recent user input; it silently leaves foreground
            # alone. Returning True unconditionally would let the retry
            # handler proceed with capture_context() against the wrong
            # window, which is exactly the production failure this fix
            # is meant to close.
            return self._foreground_matches(hwnd, expected_root_snapshot)
        except Exception as e:
            logger.debug(f"SetForegroundWindow failed: {e}")
            return False

    def _foreground_matches(
        self,
        hwnd: int,
        expected_root_snapshot: Optional[int] = None,
    ) -> bool:
        """Compare ``hwnd`` against the current foreground window.

        wh-ensure-focused-same-process-fallback: the raw
        ``GetForegroundWindow() == hwnd`` equality this replaces had two
        defects. It never normalized through ``GetAncestor(GA_ROOT)``, and
        it had no same-process fallback, so a UIA-captured invisible
        Chromium helper window (Chrome_WidgetWin_0, its own root) never
        compared equal to the visible frame (Chrome_WidgetWin_1) that
        actually held the foreground -- and this strict check blocked the
        tolerant ``_foreground_matches_target`` check that runs after it
        in ``_prove_target_is_foreground`` from ever running.

        Strict GA_ROOT equality first (fail closed on normalization
        failure, wh-oe7u.3). Only a CONFIRMED root mismatch -- both
        normalizations succeeded and produced different roots -- may
        reach the same-process fallback, scoped to the configured
        browser exe names exactly like
        ``ClipboardOperations._foreground_matches_target`` (wh-fc1x.2):
        unlisted processes keep the strict contract.
        wh-ensure-focused-same-process-fallback.1.3: the handles are
        normalized here, once, before the fallback is considered. The
        earlier shape delegated the strict compare to
        ``hwnds_match_for_foreground_compare`` and could not tell a
        root mismatch from a normalization failure, so the fallback
        call re-ran the normalization and a transient failure could
        become a credit.

        wh-ensure-focused-same-process-fallback.1.4: the fallback
        decision itself uses ``same_process_fallback_matches``, which
        never normalizes. Handing the raw handles back to the full
        comparison helper re-normalized them, and a handle reused
        between the strict compare and the fallback (Windows recycles
        numeric HWNDs) could re-normalize to the observed root and
        take that helper's strict-equality return before any PID or
        exe probe ran. GetAncestor runs exactly twice per invocation,
        both times above.

        wh-ensure-focused-same-process-fallback.1.1: the fallback is
        further scoped to targets shaped like the incident's invisible
        helper (invisible or WS_EX_TOOLWINDOW). A visible plain-styled
        target is the two-browser-windows sibling shape -- same PID,
        same exe name, but a genuinely different app window -- and must
        stay strict, or the keystrokes land in the sibling.

        wh-ensure-focused-same-process-fallback.1.10:
        ``expected_root_snapshot`` is the target's GA_ROOT as resolved
        by ``ensure_focused`` before any window manipulation. When the
        live normalization no longer matches it, the handle was
        recycled (or reparented) mid-call and no longer names the
        window the caller resolved -- refuse before the strict
        equality can credit the recycled handle's new root. ``None``
        (the default) skips the check for direct callers that hold no
        snapshot.
        """
        observed = win32gui.GetForegroundWindow()
        expected_root = normalize_hwnd_for_foreground_compare(hwnd)
        if expected_root is None:
            return False
        if (
            expected_root_snapshot is not None
            and expected_root != expected_root_snapshot
        ):
            logger.debug(
                "_foreground_matches: target root drifted from the "
                "ensure_focused snapshot (hwnd=%s snapshot=%s live=%s); "
                "refusing",
                hwnd, expected_root_snapshot, expected_root,
            )
            return False
        observed_root = normalize_hwnd_for_foreground_compare(observed)
        if observed_root is None:
            return False
        if expected_root == observed_root:
            return True
        if not self._same_process_browser_names:
            return False
        if not hwnd_is_invisible_or_toolwindow(hwnd):
            return False
        # wh-ensure-focused-same-process-fallback.1.34: one sample
        # resolves both the pid and the exe name, and the pid rides
        # along into the fallback as its snapshot -- a handle reused
        # by another same-exe process (a second Brave profile) between
        # this resolution and the fallback's own sampling must refuse.
        target_identity = process_identity_for_hwnd(hwnd)
        if target_identity is None:
            return False
        target_pid, target_process = target_identity
        if target_process not in self._same_process_browser_names:
            return False
        if not same_process_fallback_matches(
            hwnd, observed,
            expected_process_name=target_process,
            expected_pid_snapshot=target_pid,
        ):
            return False
        # wh-ensure-focused-same-process-fallback.1.5: the shape gate
        # above ran before the PID and exe probes, and Windows can
        # recycle the numeric handle in between -- a destroyed helper
        # reborn as a VISIBLE plain-styled sibling in the same browser
        # process passes every probe below the gate, which is the
        # exact sibling shape the .1.1 gate exists to refuse. Re-check
        # the shape after the identity probes so the credit stands
        # only while the CURRENT handle still looks like the
        # incident's helper. No Win32 sequence makes this atomic with
        # the caller's send; this narrows the stale-gate interval to a
        # single probe.
        return hwnd_is_invisible_or_toolwindow(hwnd)

    def remember_target(self, control) -> None:
        """Store the top-level HWND of a control for later foreground restoration.

        Args:
            control: UIA control object to extract HWND from
        """
        try:
            if not control:
                return
            top = control.GetTopLevelControl()
            hwnd = top.NativeWindowHandle if top else 0
            if hwnd:
                self._last_target_hwnd = hwnd
        except Exception:
            pass

    def get_target_window(self, focused_control):
        """Identify the target window handle and control for focus operations.

        Handles fallback logic:
        1. Use provided focused_control if keyboard focusable
        2. Otherwise query current focused control
        3. Extract window handle from control's top-level window
        4. Fallback to last remembered target window if no handle found

        Args:
            focused_control: Control object that should have focus, or None

        Returns:
            tuple: (hwnd, target_control) where:
                - hwnd: Window handle (int) or None
                - target_control: UIA Control object or None
        """
        import uiautomation as auto

        target_control = focused_control
        if not target_control or not getattr(target_control, 'IsKeyboardFocusable', False):
            try:
                target_control = auto.GetFocusedControl()
            except Exception:
                target_control = None

        hwnd = None
        try:
            if target_control:
                top = target_control.GetTopLevelControl()
                hwnd = top.NativeWindowHandle if top else None
        except Exception:
            hwnd = None

        if not hwnd:
            hwnd = self._last_target_hwnd

        return hwnd, target_control
