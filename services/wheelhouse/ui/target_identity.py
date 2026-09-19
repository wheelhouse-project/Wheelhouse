"""Window-object identity captured before any insertion strategy can wait.

The existing HWND property marker dies with the window, unlike the numeric
HWND or PID. Preserve both the UIA window and its root: either can be recycled
while the other survives. No probe makes validation plus SendInput atomic;
the final recheck belongs immediately beside the send, without waits between.
"""
from dataclasses import dataclass

import win32gui

from ui.hwnd_utils import (
    _process_id_for_hwnd,
    normalize_hwnd_for_foreground_compare,
    read_hwnd_provenance,
    tag_hwnd_provenance,
    top_level_hwnd_from_control,
)


@dataclass(frozen=True)
class TargetIdentity:
    hwnd: int = 0
    root: int = 0
    process_id: int = 0
    tag: int = 0
    root_tag: int = 0
    foreground_root: int = 0
    foreground_tag: int = 0

    def is_current(self, *, require_foreground: bool = True) -> bool:
        """Fail closed on missing, destroyed, recycled, or redirected targets."""
        if not all((self.hwnd, self.root, self.process_id, self.tag, self.root_tag,
                    self.foreground_root, self.foreground_tag)):
            return False
        try:
            if read_hwnd_provenance(self.hwnd) != self.tag:
                return False
            if normalize_hwnd_for_foreground_compare(self.hwnd) != self.root:
                return False
            if _process_id_for_hwnd(self.hwnd) != self.process_id:
                return False
            if read_hwnd_provenance(self.root) != self.root_tag:
                return False
            if read_hwnd_provenance(self.foreground_root) != self.foreground_tag:
                return False
            # Focus restoration can return from a transient foreground helper
            # to the original target. Both roots retain their capture-time
            # provenance checks; no newly observed helper or sibling is trusted.
            if require_foreground and normalize_hwnd_for_foreground_compare(
                win32gui.GetForegroundWindow()
            ) not in (self.foreground_root, self.root):
                return False
            return (
                read_hwnd_provenance(self.hwnd) == self.tag
                and read_hwnd_provenance(self.root) == self.root_tag
                and read_hwnd_provenance(self.foreground_root) == self.foreground_tag
            )
        except Exception:
            return False


def current_foreground_root() -> int:
    """Normalized root of the foreground window right now, 0 on failure.

    Reads the foreground; captures nothing and tags nothing. wh-insert-focus-
    read-stall uses it to ask whether the foreground still holds the window
    whose identity was captured before a focused-control read, without making
    a second capture -- ``capture_target_identity``'s rule is that a failed
    capture forbids recapture, and a fresh capture after the failure would
    bind whatever window the user moved to.
    """
    try:
        return normalize_hwnd_for_foreground_compare(
            win32gui.GetForegroundWindow()
        ) or 0
    except Exception:
        return 0


def capture_foreground_identity() -> TargetIdentity:
    """Capture the identity of the FOREGROUND window, with no UIA control.

    Same probes and the same completed-identity recheck as
    ``capture_target_identity``, with the foreground window as the target
    instead of a focused control's top-level window. The target IS the
    foreground here, so ``foreground_root`` and ``foreground_tag`` repeat
    ``root`` and ``root_tag``.

    wh-insert-focus-read-stall: capture_context calls this BEFORE it reads the
    focused control, so a read that blocks and then fails still has a window
    identity captured while the user was demonstrably still there. Returns the
    empty identity on any failure, including a higher-integrity foreground
    window, where UIPI blocks the SetProp in ``tag_hwnd_provenance`` and the
    resulting 0 marker fails ``is_current()``.
    """
    try:
        hwnd = int(win32gui.GetForegroundWindow() or 0)
        tag = tag_hwnd_provenance(hwnd)
        root = normalize_hwnd_for_foreground_compare(hwnd) or 0
        root_tag = tag_hwnd_provenance(root)
        process_id = _process_id_for_hwnd(hwnd) or 0
        identity = TargetIdentity(hwnd, root, process_id, tag, root_tag,
                                  root, root_tag)
        if current_foreground_root() != root or not identity.is_current():
            return TargetIdentity()
        return identity
    except Exception:
        return TargetIdentity()


def capture_target_identity(control) -> TargetIdentity:
    """Capture once; an empty identity records failure and forbids recapture.

    Re-derive the UIA handle after tagging, as the existing rejection capture
    does: a recycle before the first tag must not bind the old control to a
    new window. Recheck the completed identity after all capture probes.
    """
    try:
        hwnd = top_level_hwnd_from_control(control) or 0
        tag = tag_hwnd_provenance(hwnd)
        root = normalize_hwnd_for_foreground_compare(hwnd) or 0
        root_tag = tag_hwnd_provenance(root)
        process_id = _process_id_for_hwnd(hwnd) or 0
        foreground = normalize_hwnd_for_foreground_compare(
            win32gui.GetForegroundWindow()
        ) or 0
        foreground_tag = tag_hwnd_provenance(foreground)
        identity = TargetIdentity(hwnd, root, process_id, tag, root_tag,
                                  foreground, foreground_tag)
        if top_level_hwnd_from_control(control) != hwnd or not identity.is_current():
            return TargetIdentity()
        return identity
    except Exception:
        return TargetIdentity()
