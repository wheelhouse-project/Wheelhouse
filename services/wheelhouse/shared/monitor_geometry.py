"""Win32 multi-monitor geometry resolver for the persistent G2 editor
(wh-g2-refactor.16).

Section 4 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
is the authoritative reference. This module ships the native monitor
topology resolver as a standalone helper under
``services.wheelhouse.shared`` so the GUI process can import it without
dragging Input-process plumbing along (the same constraint slices 14
and 15 followed).

Why a native-Win32 path instead of Qt's ``QScreen.geometry()``
-------------------------------------------------------------

UIA reports control bounding rectangles in **virtual-desktop physical
pixels**. ``EnumDisplayMonitors`` + ``GetMonitorInfo`` return monitor
``rcMonitor`` rectangles in the same coordinate system. Resolving the
target monitor by physical-pixel overlap there is robust on mixed-DPI
"islands-of-screens" desktops where Qt's high-DPI documentation
explicitly disclaims an origin transform between
``QScreen.geometry()`` and the underlying Win32 monitor rectangle.

Per-Monitor v2 DPI awareness is mandatory for this module. The DPI we
read from ``GetDpiForMonitor`` is the **effective** DPI; 96 means
100%, 144 means 150%, 192 means 200%. The DPR (device pixel ratio) is
``dpi / 96``. Logical pixels are ``physical_pixels / dpr``.

The Qt bridge (``_match_qscreen_for_monitor``) consumes the native
monitor's DPR and physical dimensions to find the matching
``QScreen``. The bridge does NOT consume the screen's logical
geometry origin, because Qt's "islands-of-screens" model on Windows
makes that transform unreliable for cross-monitor positioning.

Module exports
--------------

* ``_NativeMonitor`` -- frozen dataclass describing one Win32 monitor
  in virtual-desktop physical coordinates plus its effective DPI.
* ``_enumerate_native_monitors()`` -- enumerate every monitor on the
  host via ``EnumDisplayMonitors`` + ``GetMonitorInfo`` +
  ``GetDpiForMonitor``.
* ``_overlap_area(a, b)`` -- pixel-count overlap helper.
* ``_resolve_target_monitor(rect_phys, monitors=None)`` -- pick the
  ``_NativeMonitor`` with the largest physical-pixel overlap with
  ``rect_phys``.
* ``_match_qscreen_for_monitor(monitor, screens=None)`` -- bridge from
  a resolved ``_NativeMonitor`` to the corresponding ``QScreen``.

The names follow the design doc's leading-underscore convention even
though the module is public; the convention marks them as
implementation details of the editor's geometry layer.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect


if TYPE_CHECKING:  # pragma: no cover - import guard for static checkers.
    from PySide6.QtGui import QScreen


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Win32 constants and ctypes structures.
# ---------------------------------------------------------------------------

# ``MDT_EFFECTIVE_DPI`` (per
# https://learn.microsoft.com/en-us/windows/win32/api/shellscalingapi/ne-shellscalingapi-monitor_dpi_type)
# returns the DPI Windows uses for scaling rendered content on this
# monitor. 96 = 100% scaling, 144 = 150%, 192 = 200%. Other constants
# in the same enum exist (angular DPI, raw DPI) but they are not
# relevant for placing a window on the correct physical monitor.
_MDT_EFFECTIVE_DPI = 0

# ``EnumDisplayMonitors`` callback signature.
_HMONITOR = wintypes.HANDLE
_HDC = wintypes.HDC
_LPRECT = ctypes.POINTER(wintypes.RECT)
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, _HMONITOR, _HDC, _LPRECT, wintypes.LPARAM,
)


class _MONITORINFO(ctypes.Structure):
    """``MONITORINFO`` from ``winuser.h``.

    ``rcMonitor`` is the monitor's full bounding rectangle in
    virtual-desktop physical coordinates (the same coordinate system
    UIA bounding rectangles use). ``rcWork`` is the monitor's work
    area (i.e. ``rcMonitor`` minus the taskbar) -- not consumed here,
    but defined for completeness because the struct layout is
    fixed-size.
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# Data class.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NativeMonitor:
    """A Win32 monitor description in physical coordinates.

    All rectangles are in virtual-desktop physical pixels -- the same
    coordinate system UIA rects use. ``dpi`` is the monitor's
    effective DPI (96 = 100%, 144 = 150%, 192 = 200%); divide by 96
    for a DPR.

    The dataclass is frozen so the resolver can treat monitor lists
    as immutable snapshots of the topology at the time of
    enumeration. A monitor topology change during a session creates a
    fresh list on the next enumeration; the old list is not mutated
    in place.
    """

    hmonitor: int
    rect_phys: QRect
    dpi: int

    @property
    def dpr(self) -> float:
        """Device pixel ratio (logical-to-physical conversion factor).

        ``physical_pixels = logical_pixels * dpr``. For Per-Monitor v2
        DPI awareness, every position arithmetic site that mixes Qt
        logical pixels with UIA / Win32 physical pixels MUST consult
        the DPR of the monitor the operation lives on -- the primary
        monitor's DPR is not a safe global default.
        """
        return self.dpi / 96.0


# ---------------------------------------------------------------------------
# Enumeration: ask Win32 for the topology.
# ---------------------------------------------------------------------------


def _enumerate_native_monitors() -> list[_NativeMonitor]:
    """Return a list of Win32 monitors in physical-pixel coordinates.

    Uses ``EnumDisplayMonitors`` + ``GetMonitorInfoW`` for the
    rectangles, and ``GetDpiForMonitor`` (Windows 8.1+, ``shcore.dll``)
    for the per-monitor DPI. Falls back to 96 DPI on any per-monitor
    DPI lookup failure -- a degraded but safe default.

    On non-Windows hosts (developer machines running tests against
    mocked layouts), ``ctypes.windll`` is unavailable and this
    function returns an empty list. Production code MUST handle that
    case by falling back to the primary screen (see
    ``_resolve_target_monitor``'s ``None`` return contract).
    """
    if sys.platform != "win32":
        logger.debug("non-Windows host; returning empty monitor list")
        return []
    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):  # pragma: no cover - platform guard
        return []
    try:
        shcore = ctypes.windll.shcore
    except (AttributeError, OSError):
        shcore = None

    # 64-bit type safety: HMONITOR and HDC are pointer-sized handles
    # (8 bytes on 64-bit Windows). Without argtypes / restype, ctypes
    # defaults to passing them as c_int (4 bytes), truncating the upper
    # 32 bits. Most HMONITOR values today are small integers that fit
    # in 32 bits, but the contract is pointer-sized; rely on the
    # declared type, not on observed values.
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC, _LPRECT, _MonitorEnumProc, wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [
        _HMONITOR, ctypes.POINTER(_MONITORINFO),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    if shcore is not None:
        shcore.GetDpiForMonitor.argtypes = [
            _HMONITOR,
            ctypes.c_int,                      # MONITOR_DPI_TYPE enum
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        # GetDpiForMonitor returns HRESULT. ctypes.HRESULT is signed
        # 32-bit; declaring it lets a future ctypes change to validate
        # the return path against the documented signature.
        shcore.GetDpiForMonitor.restype = ctypes.HRESULT

    monitors: list[_NativeMonitor] = []

    def _callback(hmon, _hdc, _lprect, _lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return 1  # continue enumeration -- best-effort
        rc = info.rcMonitor
        # rcMonitor is in virtual-desktop PHYSICAL pixels (same
        # coordinate system UIA returns). No DPI conversion happens
        # here; the resolver later compares physical-pixel
        # rectangles directly.
        rect = QRect(rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
        dpi = 96
        if shcore is not None:
            dx = wintypes.UINT(0)
            dy = wintypes.UINT(0)
            hr: int | None
            try:
                hr = shcore.GetDpiForMonitor(
                    hmon,
                    _MDT_EFFECTIVE_DPI,
                    ctypes.byref(dx),
                    ctypes.byref(dy),
                )
            except OSError as exc:
                # The OS raised on a valid HMONITOR. Something is wrong
                # at the OS level; the 96-DPI fallback keeps the editor
                # working but at the wrong size on the affected monitor.
                # Show a WARNING so an operator can correlate half-sized
                # editors with this fallback. ``hr = None`` signals to
                # the branch below that this path already logged.
                logger.warning(
                    "GetDpiForMonitor failed for HMONITOR %s: OSError=%s",
                    hmon,
                    exc,
                )
                hr = None
            if hr == 0:
                # Effective DPI is reported per-axis but Windows
                # always reports both axes equal for the desktop
                # composition path. Prefer the x value; it is the
                # value Qt's devicePixelRatio uses on Windows.
                dpi = int(dx.value)
            elif hr is not None:
                # Non-zero HRESULT without an OSError. May be normal
                # for certain monitor configurations (e.g., MST hubs
                # reporting a "virtual" monitor without DPI), so DEBUG
                # rather than WARNING. The 96-DPI fallback applies.
                logger.debug(
                    "GetDpiForMonitor returned non-zero HRESULT %s "
                    "for HMONITOR %s; using 96-DPI fallback",
                    hr,
                    hmon,
                )
        monitors.append(_NativeMonitor(int(hmon), rect, dpi))
        return 1  # continue enumeration

    callback = _MonitorEnumProc(_callback)
    try:
        user32.EnumDisplayMonitors(0, None, callback, 0)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("EnumDisplayMonitors raised: %s", exc)
    return monitors


# ---------------------------------------------------------------------------
# Geometry helpers.
# ---------------------------------------------------------------------------


def _overlap_area(a: QRect, b: QRect) -> int:
    """Pixel-count overlap between two rectangles, 0 if disjoint.

    Both rectangles must be in the same coordinate system (the
    callers here use virtual-desktop physical pixels exclusively).
    ``QRect.intersected`` returns a zero-size or invalid rect on
    disjoint or touching rectangles; we collapse those to 0.
    """
    inter = a.intersected(b)
    if not inter.isValid():
        return 0
    w = inter.width()
    h = inter.height()
    if w <= 0 or h <= 0:
        return 0
    return w * h


def _resolve_target_monitor(
    rect_phys: tuple[int, int, int, int],
    monitors: list[_NativeMonitor] | None = None,
) -> _NativeMonitor | None:
    """Return the ``_NativeMonitor`` with the largest physical overlap
    with ``rect_phys``.

    ``rect_phys`` is ``(left, top, right, bottom)`` in physical pixels
    from UIA, relative to the virtual-desktop origin (Win32 virtual
    coordinates). The function does NOT do any DPI conversion -- both
    sides of the overlap math are already physical-pixel.

    ``monitors`` is a dependency-injection seam for tests
    (wh-g2-refactor.7.1 / round 2 codex finding). Production calls
    leave it as ``None`` and the function calls
    ``_enumerate_native_monitors()``. Tests pass synthetic layouts.

    Returns ``None`` if no monitors are reported AT ALL (degenerate;
    caller falls back to default geometry, typically the Qt primary
    screen). On non-overlap with all monitors, returns the FIRST
    monitor in the list -- which is the primary on a normal Windows
    enumeration. The first-monitor fallback preserves the
    pre-multi-monitor production behaviour for off-screen UIA rects
    (an editor on the primary screen is always preferable to no
    editor at all).

    On equal positive overlap with two or more monitors, returns the
    monitor that appears earliest in ``monitors`` with that overlap
    count -- the comparison is strict greater-than, so the first
    candidate wins ties. This case shows up when an editor straddles
    the exact boundary between two side-by-side monitors of the same
    physical height; the rare-but-real behaviour is to clamp to the
    earlier monitor.
    """
    if monitors is None:
        monitors = _enumerate_native_monitors()
    if not monitors:
        return None
    # rect_phys is virtual-desktop PHYSICAL pixels by contract.
    # Convert to a QRect for the overlap math.
    rect = QRect(
        rect_phys[0],
        rect_phys[1],
        rect_phys[2] - rect_phys[0],
        rect_phys[3] - rect_phys[1],
    )
    best = monitors[0]
    best_overlap = -1
    for mon in monitors:
        overlap = _overlap_area(rect, mon.rect_phys)
        if overlap > best_overlap:
            best = mon
            best_overlap = overlap
    return best


# ---------------------------------------------------------------------------
# Qt bridge: HMONITOR -> QScreen.
# ---------------------------------------------------------------------------


def _match_qscreen_for_monitor(
    monitor: _NativeMonitor,
    screens: list["QScreen"] | None = None,
) -> "QScreen" | None:
    """Find the ``QScreen`` whose logical geometry corresponds to
    ``monitor``.

    We bridge from the Win32 monitor to a ``QScreen`` so the caller
    can still use Qt's ``availableGeometry`` / ``devicePixelRatio``
    APIs to drive the editor's resize/move. The match strategy:

      1. **Primary path: HMONITOR identity via ``QScreen.handle()``.**
         On the Windows platform plugin, PySide6 exposes the
         ``HMONITOR`` through ``QScreen.handle()`` (or a small
         wrapper). When that path succeeds it is unambiguous
         regardless of how many monitors share resolution and DPI.
         The path is platform- and version-specific, so the code
         wraps it in a ``try`` / ``except`` and falls back to the
         dimensions match below.
      2. **Fallback: DPR + physical dimensions.** Qt's high-DPI docs
         guarantee that ``QScreen.geometry().size() * DPR`` matches
         the monitor's physical pixel dimensions on Windows, but
         explicitly disclaim the origin transform. Matching on size +
         DPR is therefore the largest safe surface beyond the
         identity path.

    Identical-display ambiguity (round 1 / deepseek finding 8.4): if
    two monitors share resolution AND DPI -- a common dual-monitor
    setup with identical displays -- the DPR+dimensions fallback
    produces more than one candidate. The function logs DEBUG when
    this happens and returns the FIRST ``QScreen`` in
    ``QGuiApplication.screens()`` order. The impact is bounded: the
    editor uses that screen's ``availableGeometry`` for the clamp; if
    monitor A and B have different taskbar placements the clamp uses
    the wrong rectangle and the editor's position is shifted, but
    the editor remains visible. The HMONITOR identity path (when it
    succeeds) is the correct disambiguator.

    ``screens`` is a dependency-injection seam for tests. Production
    calls leave it as ``None`` and the function reads
    ``QGuiApplication.screens()``. Tests pass synthetic
    QScreen-shaped stubs.

    Returns ``None`` if no QScreen matches; the caller falls back to
    ``QGuiApplication.primaryScreen()``.
    """
    if screens is None:
        # Lazy import keeps the module importable without a running
        # QGuiApplication (the dependency-injection seam tests use
        # avoids the import path entirely).
        from PySide6.QtGui import QGuiApplication  # noqa: PLC0415
        screens = QGuiApplication.screens()
    if not screens:
        return None

    # Primary path: HMONITOR identity. When this path succeeds it is
    # unambiguous even for identical-display setups (round 1 /
    # deepseek finding 8.4).
    for qs in screens:
        try:
            handle = qs.handle()
        except Exception:  # noqa: BLE001 - platform-specific quirk
            # ``handle()`` may not be available on every PySide6
            # build; one failure means the path is unavailable for
            # ALL screens in this iteration. Fall through to the
            # dimensions match below.
            break
        if handle is None:
            continue
        try:
            handle_int = int(handle)
        except (TypeError, ValueError):
            continue
        if handle_int == monitor.hmonitor:
            return qs

    # Fallback: DPR + physical dimensions. The DPR comparison uses a
    # small epsilon to tolerate IEEE-754 round-trip noise (Qt's
    # ``devicePixelRatio()`` may return 1.4999999999 on a 144 DPI
    # monitor). We compute the physical size from each screen's
    # logical size by multiplying by its DPR -- per Qt's high-DPI
    # docs the size transform IS guaranteed on Windows even though
    # the origin transform is not.
    target_w = monitor.rect_phys.width()
    target_h = monitor.rect_phys.height()
    target_dpr = monitor.dpr
    candidates: list["QScreen"] = []
    for qs in screens:
        try:
            dpr = qs.devicePixelRatio() or 1.0
        except Exception:  # noqa: BLE001 - defensive
            continue
        if abs(dpr - target_dpr) > 0.01:
            continue
        try:
            g = qs.geometry()
        except Exception:  # noqa: BLE001 - defensive
            continue
        # Physical pixels = logical pixels * DPR.
        if (
            int(round(g.width() * dpr)) == target_w
            and int(round(g.height() * dpr)) == target_h
        ):
            candidates.append(qs)

    if len(candidates) > 1:
        # Round 1 / deepseek finding 8.4: identical-display
        # ambiguity. The HMONITOR identity path above did not
        # disambiguate (either it raised, PySide6 did not expose the
        # handle as HMONITOR on this version, or the screens really
        # do share resolution + DPI), and the dimensions fallback
        # alone cannot tell the screens apart. Return the first
        # candidate in QGuiApplication.screens() order; downstream
        # users of availableGeometry get a screen with the right size
        # but potentially the wrong taskbar.
        logger.debug(
            "QScreen ambiguous for monitor hmonitor=%s (%d candidates "
            "with matching DPR+dimensions); returning first",
            monitor.hmonitor,
            len(candidates),
        )
    if candidates:
        return candidates[0]
    return None
