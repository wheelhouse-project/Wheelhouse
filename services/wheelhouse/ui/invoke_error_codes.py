"""Allowlist of UI Automation HRESULT codes that are side-effect-free after
a failed InvokePattern.Invoke() (wh-hvvqq).

WheelHouse's voice element-clicking feature (design v5, "Click execution
paths", InvokePattern path branch 2) invokes a cached UIA element via
``InvokePattern.Invoke()``. When that call returns a COM error, WheelHouse
fails CLOSED by default: a COM error is not proof the Invoke had no side
effect. The provider may have activated the control, dismissed a dialog, or
torn down the window before the error propagated back, so blindly retrying
with a coordinate click could double-fire the action.

A SMALL set of HRESULT codes are documented by Microsoft UI Automation in a
way that makes them safe to treat as side-effect-free and to permit a
(separately-gated) coordinate-click fallback. For some of these codes the
spec guarantees the call was rejected up front (UIA_E_NOTSUPPORTED); for
others (UIA_E_ELEMENTNOTAVAILABLE) the safety relies on the consumer's
pre-click re-verification in the ClickExecutor path, not on the code alone --
see the per-code comments below.

Contract:

* Codes are added to this allowlist ONLY with a documented Microsoft UI
  Automation specification citation explaining why the code proves the
  Invoke performed no action. No code goes in on a hunch.
* Production telemetry on the ``execution_failed:invoke_com_error`` signal
  is the evidence used to decide whether the allowlist needs to grow. If a
  real-world COM error recurs and the spec documents it as pre-action, add
  it here with its citation; otherwise the fail-closed default stands.

Source: Microsoft UI Automation "Error Codes (UIAutomationCoreApi.h)"
https://learn.microsoft.com/windows/win32/winauto/uiauto-error-codes

This module ships ONLY the allowlist and its membership helper. The
ClickExecutor that consumes it lives in a separate slice (wh-mzpvx).
"""

from __future__ import annotations

from typing import Final

# UIA_E_ELEMENTNOTAVAILABLE (0x80040201): per the Microsoft UI Automation
# error-code spec, this "indicates that a method was called on a virtualized
# element, or on an element that no longer exists, usually because it has
# been destroyed." Note the spec does NOT guarantee this error is raised
# strictly BEFORE any action: an element can be destroyed *because* the
# Invoke acted on it (a self-dismissing button or a dialog that closes
# itself) and only then surface this code. Treating it as side-effect-free
# is therefore safe ONLY in combination with the consumer's pre-click
# re-verification (the v5 design's re-verification block in the
# ClickExecutor path), which confirms the element is still present and the
# expected target before any coordinate-click fallback fires. The code
# alone is not a pre-action guarantee.
UIA_E_ELEMENTNOTAVAILABLE: Final[int] = 0x80040201

# UIA_E_NOTSUPPORTED (0x80040204): per the Microsoft UI Automation
# error-code spec, this "indicates that the provider explicitly does not
# support the specified property or control pattern. UI Automation will
# return this error code to the caller without attempting to provide a
# default value or falling back to another provider." The InvokePattern
# operation was rejected up front because the provider does not implement
# it, so no action was performed. Safe to treat as side-effect-free.
UIA_E_NOTSUPPORTED: Final[int] = 0x80040204

# Immutable allowlist of HRESULT codes proven side-effect-free by the
# spec citations above. A failed Invoke() whose HRESULT is in this set may
# (subject to separate gating) be followed by a coordinate-click fallback.
NO_SIDE_EFFECT_HRESULTS: Final[frozenset[int]] = frozenset(
    {
        UIA_E_ELEMENTNOTAVAILABLE,
        UIA_E_NOTSUPPORTED,
    }
)


def is_no_side_effect_hresult(hresult: int) -> bool:
    """Return True only when ``hresult`` is on the side-effect-free allowlist.

    Accepts either the SIGNED or the UNSIGNED 32-bit form of the HRESULT.
    A real ``comtypes.COMError`` exposes ``hresult`` as a signed 32-bit int
    (e.g. UIA_E_ELEMENTNOTAVAILABLE arrives as ``-2147220991``), whereas the
    allowlist stores the unsigned hex form (``0x80040201``). The input is
    masked to unsigned 32 bits (``hresult & 0xFFFFFFFF``) before the
    membership check so both forms map to the same allowlisted value.

    Any non-``int`` input (``None``, a string, etc.) returns False, keeping
    the fail-closed default rather than raising. An int outside the valid
    32-bit HRESULT range -- the unsigned form ``0 .. 0xFFFFFFFF`` and the
    signed form ``-0x80000000 .. -1`` -- also returns False, so a wider
    integer whose low 32 bits happen to match an allowlisted code (e.g.
    ``0x180040201``) is not wrongly accepted.

    A True result means a failed ``InvokePattern.Invoke()`` returning this
    HRESULT is safe to treat as side-effect-free, so a coordinate-click
    fallback may proceed (subject to its own separate gating). Every other
    HRESULT returns False.
    """

    if not isinstance(hresult, int) or isinstance(hresult, bool):
        return False
    if hresult < -0x80000000 or hresult > 0xFFFFFFFF:
        return False
    return (hresult & 0xFFFFFFFF) in NO_SIDE_EFFECT_HRESULTS
