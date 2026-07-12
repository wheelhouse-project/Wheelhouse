"""Restricted window-walk fall-back for voice element clicking (wh-86qdm).

ElementFinder.find() walks the focused window first (the v5 "Focus first"
rule). When that focused-window walk produces NO usable match (decide()
returns ``not_found``), the v5 "Restricted fall-back" rule runs: enumerate the
other visible top-level windows, keep only those whose screen rectangle
OVERLAPS the focused window's monitor, order/deprioritise them by the v5
overlay heuristics, and walk them in order until one produces a decided match.
The authoritative spec is the v5 design doc,
docs/plans/2026-05-21-voice-element-clicking-design-v5.md, "Window targeting".

This module owns ONLY the pure-data window shape, the candidate-restriction +
ordering policy, and the real-Win32 default enumerator. It reads no config and
touches no Win32 in a way that breaks headless tests: the enumerator is an
INJECTED callable with a real-Win32 default, exactly like ElementFinder's
``walk_fn`` / ``monitor_resolver`` / ``dpi_resolver`` injection pattern. The
``order_candidates`` / ``restrict_to_monitor`` policy functions are pure and
deterministic so the fall-back ordering can be unit-tested against synthetic
windows with no real display.

The four v5 overlay-deprioritisation signals:

| Window style signal                       | Indicates           |
|-------------------------------------------|---------------------|
| ``WS_EX_TOPMOST`` + ``WS_EX_TOOLWINDOW``  | Floating utility    |
| ``WS_EX_NOACTIVATE``                       | Designed not to take focus |
| Very small relative to screen             | Floating widget     |
| No interactive UI Automation children     | Empty overlay       |

The first three are CHEAP to read from the window's extended style and rect, so
``order_candidates`` ranks them up front: a window carrying any of those
signals sorts AFTER a plain window, so a real application window is always
preferred over a floating overlay on the same monitor. The fourth signal
("no interactive UI Automation children") cannot be known without walking the
window, so it is handled at WALK time rather than in the ordering: a candidate
whose walk yields no decided match (decide() == not_found) is simply skipped
and the next candidate is tried. An empty overlay therefore never wins because
its walk produces nothing, which is exactly the v5 deprioritisation intent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


# Extended-window-style bits (winuser.h). Stable, documented Win32 values.
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

# A candidate window whose area is at or below this fraction of its monitor's
# area is treated as a "floating widget" and deprioritised (v5 signal 3). 5%
# of a monitor is a small floating toolbar / pill / mini-overlay; a real
# application window, dialog, or browser tab is far larger. The threshold lives
# here (not in config) because this module reads no config; the value mirrors
# the v5 "very small relative to screen" heuristic.
_VERY_SMALL_AREA_FRACTION = 0.05


@dataclass(frozen=True)
class FallbackWindow:
    """One visible top-level window the fall-back may walk.

    Pure data -- no COM, no live Win32 handle dereference. ``hwnd`` is the
    window handle the walker resolves to a UI Automation element; ``pid`` /
    ``process_name`` let the coordinator pick the per-window
    ``query_has_role`` (Chromium-family -> False, preserving the load-bearing
    browser wiring); ``ex_style`` carries the WS_EX_* extended-style bits the
    overlay heuristics read; ``rect`` is the window's screen rectangle in
    virtual-desktop physical pixels as ``(x, y, width, height)`` -- the same
    coordinate system UIA bounds and the monitor resolver use.
    """

    hwnd: int
    pid: int
    process_name: str
    ex_style: int
    rect: tuple[int, int, int, int]


def is_topmost_toolwindow(window: FallbackWindow) -> bool:
    """True when BOTH WS_EX_TOPMOST and WS_EX_TOOLWINDOW are set (floating utility)."""
    return bool(window.ex_style & WS_EX_TOPMOST) and bool(
        window.ex_style & WS_EX_TOOLWINDOW
    )


def is_noactivate(window: FallbackWindow) -> bool:
    """True when WS_EX_NOACTIVATE is set (designed not to take focus)."""
    return bool(window.ex_style & WS_EX_NOACTIVATE)


def is_very_small(
    window: FallbackWindow, monitor_rect: tuple[int, int, int, int] | None
) -> bool:
    """True when the window's area is a tiny fraction of its monitor.

    ``monitor_rect`` is the focused window's MONITOR as ``(x, y, width,
    height)`` physical pixels, or ``None`` when the focused monitor could not be
    resolved (off-monitor opt-in path). A window with non-positive area, or one
    whose area is at or below ``_VERY_SMALL_AREA_FRACTION`` of the monitor area,
    is small. A non-positive monitor area, or a ``None`` monitor rect, degrades
    safely to False so an unresolved/degenerate monitor never marks every
    candidate small (finding 46.2/46.3 fail-closed companion).

    NOTE: this reports the raw area signal only. Whether the area signal counts
    as a DEPRIORITISATION is decided in ``_overlay_signal_count`` -- per finding
    46.4 the small-area signal deprioritises a window ONLY when it ALSO carries
    a window-STYLE overlay flag, so a small plain captioned dialog keeps its
    z-order position.
    """
    if monitor_rect is None:
        return False
    _, _, w, h = window.rect
    win_area = w * h
    if win_area <= 0:
        return True
    _, _, mw, mh = monitor_rect
    mon_area = mw * mh
    if mon_area <= 0:
        return False
    return win_area <= _VERY_SMALL_AREA_FRACTION * mon_area


def _overlay_signal_count(
    window: FallbackWindow, monitor_rect: tuple[int, int, int, int] | None
) -> int:
    """Count the CHEAP overlay-deprioritisation signals this window carries.

    Two window-STYLE signals are independent (topmost+toolwindow, noactivate).
    The very-small AREA signal (finding 46.4) is NOT independent: a small window
    counts as an overlay ONLY when it also carries one of the style flags. A
    normal confirmation dialog (~322x322 on 1080p is ~9% but the threshold is
    5%; even a sub-5% plain dialog) has no overlay style, so it keeps its
    enumeration/z-order position rather than being shoved behind a large
    same-label app window. The "no interactive children" signal needs a walk and
    is handled at walk time (see the module docstring). A higher count sorts
    LATER, so a plain window (count 0) is always preferred over an overlay.
    """
    has_style_overlay = is_topmost_toolwindow(window) or is_noactivate(window)
    count = 0
    if is_topmost_toolwindow(window):
        count += 1
    if is_noactivate(window):
        count += 1
    # 46.4: very-small only deprioritises when paired with a style overlay flag.
    if has_style_overlay and is_very_small(window, monitor_rect):
        count += 1
    return count


def _rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when two ``(x, y, w, h)`` rectangles share any positive-area region."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return False
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def restrict_to_monitor(
    windows: list[FallbackWindow],
    *,
    focused_monitor_rect: tuple[int, int, int, int],
    enable_offmonitor_fallback: bool,
) -> list[FallbackWindow]:
    """Keep only the candidates the v5 restricted fall-back may walk.

    When ``enable_offmonitor_fallback`` is False (the v5 default) a candidate is
    kept only when its screen rectangle OVERLAPS the focused window's monitor
    rectangle (``focused_monitor_rect``), per ``_rects_overlap``. This is the v5
    wording verbatim ("top-level windows whose screen rectangle overlaps the
    focused window's monitor"): a window straddling the focused monitor and a
    neighbour is kept even when most of its area sits on the neighbour, because
    the user can still plausibly see the part on the focused monitor (finding
    45.3). The earlier largest-overlap-monitor-id test wrongly dropped such a
    straddler.

    When ``enable_offmonitor_fallback`` is True every supplied candidate is
    kept (the multi-monitor opt-in: walk every monitor).

    ``windows`` is the already-deduplicated candidate set (the focused window
    is excluded by the caller). Input order is preserved among the kept
    windows so the subsequent ordering step is deterministic.
    """
    if enable_offmonitor_fallback:
        return list(windows)
    return [w for w in windows if _rects_overlap(w.rect, focused_monitor_rect)]


def order_candidates(
    windows: list[FallbackWindow],
    *,
    monitor_rect: tuple[int, int, int, int] | None,
) -> list[FallbackWindow]:
    """Order the restricted candidates by v5 overlay deprioritisation.

    A window carrying any overlay signal (topmost+toolwindow, noactivate, or
    very-small-while-also-style-overlay per finding 46.4) sorts AFTER a window
    carrying fewer such signals, so a real application window is walked before a
    floating overlay. The sort is STABLE (Python ``sorted``), so among windows
    with the same signal count the enumeration order is preserved -- the
    deterministic tiebreak the fall-back loop relies on.

    ``monitor_rect`` is the focused window's MONITOR rectangle as
    ``(x, y, w, h)`` physical pixels (resolved via the monitor-rect resolver,
    finding 45.2), or ``None`` when the focused monitor could not be resolved
    (the off-monitor opt-in path, finding 46.2/46.3). When it is ``None`` the
    very-small monitor-relative signal is skipped (``is_very_small`` returns
    False on a ``None`` monitor rect) rather than dividing by a bogus area, so
    ordering still runs off the two pure window-style signals.
    """
    return sorted(windows, key=lambda w: _overlay_signal_count(w, monitor_rect))


def enumerate_top_level_windows() -> list[FallbackWindow]:
    """Real-Win32 default enumerator: visible top-level windows as FallbackWindow.

    Uses pywin32 (``win32gui`` / ``win32process``) the same way
    ``features/window_mover.py`` and ``plugins/window_positioning_plugin.py``
    do: ``EnumWindows`` to walk every top-level window, ``IsWindowVisible`` to
    drop hidden ones, ``GetWindowLong(GWL_EXSTYLE)`` for the extended style,
    ``GetWindowRect`` for the screen rectangle, and
    ``GetWindowThreadProcessId`` + ``psutil`` for the owning process name.

    Imported lazily and guarded so this module stays importable (and the
    pure-policy functions stay testable) on a non-Windows host or a machine
    without pywin32 -- the headless tests inject a fake enumerator and never
    reach this function. Any per-window probe failure skips that window rather
    than aborting the whole enumeration (best-effort, matching window_mover).
    """
    try:
        import win32con
        import win32gui
        import win32process
    except Exception:  # noqa: BLE001 -- non-Windows / no-pywin32 host
        logger.debug("win32 modules unavailable; window fall-back enumeration empty")
        return []

    try:
        import psutil
    except Exception:  # noqa: BLE001
        psutil = None  # type: ignore[assignment]

    windows: list[FallbackWindow] = []

    def _callback(hwnd: int, _lparam: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            rect = (left, top, right - left, bottom - top)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = ""
            if psutil is not None and pid:
                try:
                    process_name = psutil.Process(pid).name()
                except Exception:  # noqa: BLE001 -- NoSuchProcess / AccessDenied
                    process_name = ""
            windows.append(
                FallbackWindow(
                    hwnd=int(hwnd),
                    pid=int(pid),
                    process_name=process_name,
                    ex_style=int(ex_style),
                    rect=rect,
                )
            )
        except Exception:  # noqa: BLE001 -- skip this window, keep enumerating
            logger.debug("window fall-back: skipping HWND %s on probe failure", hwnd)
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:  # noqa: BLE001 -- defensive; return what we collected
        logger.debug("window fall-back: EnumWindows raised", exc_info=True)
    return windows


def resolve_monitor_rect(
    box: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    """Real-Win32 default monitor-RECTANGLE resolver (finding 45.2).

    Given a physical-pixel ``(x, y, w, h)`` box -- the focused window's
    rectangle, or a 1x1 box at the cursor -- return the screen RECTANGLE of the
    monitor that box has the largest overlap with, as ``(x, y, w, h)`` physical
    pixels. The very-small overlay heuristic and the same-monitor overlap test
    both need the focused window's MONITOR rectangle, not the focused window's
    own rectangle; ``monitor_resolver`` returns only an id, so this companion
    resolver supplies the geometry.

    Imports ``shared/monitor_geometry.py`` READ-ONLY for the native monitor
    enumeration + largest-overlap resolution (``_resolve_target_monitor``
    expects an ``(left, top, right, bottom)`` rect, so the ``(x, y, w, h)`` box
    is converted).

    Returns ``None`` (a) when no monitor topology is available (no display /
    non-Windows host), and (b) -- the fail-closed guard for finding 46.3 --
    when the box overlaps NO monitor. ``_resolve_target_monitor`` falls through
    to the FIRST (primary) monitor on zero overlap, so this resolver
    re-validates that the chosen monitor actually has POSITIVE overlap with the
    box (via ``_overlap_area``) and returns ``None`` otherwise. Returning the
    primary monitor for a box that no monitor contains would let the
    same-monitor fall-back walk the wrong monitor; ``None`` signals the caller
    to fail closed instead. Injectable; tests pass a fake.
    """
    try:
        from shared.monitor_geometry import _overlap_area, _resolve_target_monitor
        from PySide6.QtCore import QRect
    except Exception:  # noqa: BLE001 -- non-Windows / import guard
        logger.debug("monitor_geometry unavailable; monitor-rect resolver -> None")
        return None
    x, y, w, h = box
    ltrb = (x, y, x + w, y + h)
    monitor = _resolve_target_monitor(ltrb)
    if monitor is None:
        return None
    rect = monitor.rect_phys  # QRect in virtual-desktop physical pixels
    # Fail-closed overlap guard (46.3): _resolve_target_monitor returns the
    # primary monitor when the box overlaps nothing. Reject that fall-through so
    # the caller treats the focused monitor as unresolved rather than walking
    # the primary monitor for an off-screen box.
    box_qrect = QRect(x, y, max(w, 0), max(h, 0))
    if _overlap_area(box_qrect, rect) <= 0:
        return None
    return (rect.x(), rect.y(), rect.width(), rect.height())


def resolve_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Real-Win32 default focused-window-RECT resolver (finding 46.2).

    Return the screen rectangle of ``hwnd`` as ``(x, y, w, h)`` physical
    pixels, or ``None`` on any failure (the window closed, an invalid handle, a
    non-Windows host). The fall-back resolves the focused window's monitor from
    THIS rectangle -- independent of the best-effort candidate enumeration -- so
    a focused window that does not happen to appear in the enumeration is still
    anchored to its real monitor rather than the cursor's monitor. ``None`` makes
    the caller fail closed (no same-monitor fall-back when off-monitor is
    disabled). Injectable; tests pass a fake.
    """
    try:
        import win32gui
    except Exception:  # noqa: BLE001 -- non-Windows / no-pywin32 host
        logger.debug("win32gui unavailable; focused-window-rect resolver -> None")
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001 -- closed/invalid handle
        logger.debug("GetWindowRect failed for HWND %s; -> None", hwnd)
        return None
    return (left, top, right - left, bottom - top)


__all__ = [
    "FallbackWindow",
    "WS_EX_TOPMOST",
    "WS_EX_TOOLWINDOW",
    "WS_EX_NOACTIVATE",
    "resolve_monitor_rect",
    "resolve_window_rect",
    "is_topmost_toolwindow",
    "is_noactivate",
    "is_very_small",
    "restrict_to_monitor",
    "order_candidates",
    "enumerate_top_level_windows",
]
