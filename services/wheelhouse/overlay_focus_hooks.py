"""Pure decision logic for the Logic-side overlay focus hooks (wh-n29v.21).

Parent epic: ``wh-n29v`` (voice-element-clicking Phase 1.5), backlog leaf
``wh-htj77g``. The authoritative spec is the v4 design doc
``docs/plans/2026-05-28-voice-element-clicking-phase-1-5-design-v4.md``,
the "### Focused-window change producer", "### Supersession generation",
and "Mic-pause corner cases" (case 3 / r2.8) sections.

This module is the PURE, unit-testable half of the Logic-process focus-hook
slice. It holds:

  * :class:`FocusChangeDebouncer` -- collapses rapid ``EVENT_SYSTEM_FOREGROUND``
    callbacks within ``overlay_focus_debounce_ms`` (default 250) into one fire.
  * :class:`ForegroundIdentity` + :func:`identity_matches` -- the FULL
    foreground-identity comparison (HWND + PID + process name + window
    creation time, NOT ``IsWindow`` alone, per r2.8) used at the mic-resume
    transition.
  * :func:`map_foreground_event` / :func:`map_destroy_event` -- the mapping
    from a raw foreground / destroy event to the :class:`OverlayEvent` the
    state machine should be fed.

The focus-change debounce interval is no longer read here. The validating
``ClickConfig`` reader (``ui/click_config.py``) now carries
``overlay_focus_debounce_ms`` (range ``[0, 5000]``, default 250, landed as
``wh-n29v.29``), and the Logic process builds its
:class:`FocusChangeDebouncer` from that VALIDATED value so the Logic and
Input processes can never disagree on the validated config (``wh-n29v.66``).
The former defensive raw reader (``read_focus_debounce_ms``) was retired with
that fix; it would otherwise be a second, divergent reader of the same key.

PURITY. Nothing here performs I/O of any kind: no asyncio, no Win32, no
``SetWinEventHook``, no IPC, no logging that matters to behaviour. Every
function/class takes its inputs (the current monotonic time in ms, the
machine's generation pair, captured/current identities, raw event fields)
as plain arguments, so the logic is testable with injected values and never
needs a faked OS event loop. The raw ``SetWinEventHook`` registration and the
``PeekMessage`` pump are the THIN seam in ``main.py`` (mirroring
``features/window_mover.py``); the testable decisions live here, mirroring
how ``speech/overlay_click_router.py`` is a pure decision module.

The :class:`OverlayEvent` / :class:`OverlayEventKind` the mapping returns are
imported from :mod:`services.wheelhouse.click_overlay_state`; this module
PRODUCES those events but does NOT modify the state machine (its event kinds
``FOCUS_CHANGE`` and ``FOCUSED_HWND_DESTROYED`` already exist).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.wheelhouse.click_overlay_state import (
    OverlayEvent,
    OverlayEventKind,
)


# The default focus-change debounce window in ms (v4: default 250). The same
# value is the validated ``ClickConfig`` fall-back when the [click] key is
# absent or out of range.
DEFAULT_FOCUS_DEBOUNCE_MS: int = 250

# Win32 WinEvent ids for menu pop-up open/close (WinUser.h
# EVENT_SYSTEM_MENUPOPUPSTART / EVENT_SYSTEM_MENUPOPUPEND). Owned HERE, the
# pure module, so :func:`map_menu_popup_event` and the ``main.py``
# ``SetWinEventHook`` registration seam share one definition -- the seam
# registers exactly this two-id range (0x0006..0x0007; no other WinEvent id
# lies between them). Plain ints, no Win32 import (wh-overlay-menu-close-stale).
EVENT_SYSTEM_MENUPOPUPSTART: int = 0x0006
EVENT_SYSTEM_MENUPOPUPEND: int = 0x0007


@dataclass
class FocusChangeDebouncer:
    """Collapses rapid foreground-change callbacks within a debounce window.

    The ``EVENT_SYSTEM_FOREGROUND`` hook fires on a Windows worker thread for
    every foreground change -- including tooltip-as-foreground artifacts and
    each step of a rapid Alt-Tab. The debouncer filters those: the FIRST event
    fires; any subsequent event whose monotonic timestamp is strictly within
    ``debounce_ms`` of the last FIRED event is coalesced (dropped). A dropped
    event does NOT advance the window -- the window is anchored at the last
    event that actually fired -- so a steady stream of events inside one
    window collapses to a single fire rather than ratcheting the window
    forward indefinitely.

    PURE: the caller supplies ``now_ms`` (a monotonic clock reading in
    milliseconds). The debouncer holds only the timestamp of the last fired
    event; it performs no clock read of its own. ``debounce_ms == 0`` disables
    debouncing (every event fires). Single-thread-owned by the Logic asyncio
    loop (the hook callback marshals onto that loop before calling
    :meth:`should_fire`), so no lock is needed.
    """

    debounce_ms: int = DEFAULT_FOCUS_DEBOUNCE_MS
    _last_fired_ms: Optional[float] = None

    def should_fire(self, *, now_ms: float) -> bool:
        """Return True when a foreground event at ``now_ms`` should fire.

        Fires when this is the first event, when debouncing is disabled
        (``debounce_ms <= 0``), or when ``now_ms`` is at least ``debounce_ms``
        past the last fired event. Otherwise returns False WITHOUT advancing
        the window. On a fire, records ``now_ms`` as the new window anchor.
        """
        if self.debounce_ms <= 0 or self._last_fired_ms is None:
            self._last_fired_ms = now_ms
            return True
        if now_ms - self._last_fired_ms >= self.debounce_ms:
            self._last_fired_ms = now_ms
            return True
        return False

    def remaining_ms(self, *, now_ms: float) -> float:
        """Ms until the debounce window clears (0 = an event now would fire).

        wh-overlay-nested-dupes.1.1: the drop-only debounce loses the FINAL
        event of a burst (double-Escape out of a submenu, a dialog opening
        right after a menu close), leaving dead badges with nothing left to
        clean them. The loop-side seam uses this value to arm a one-shot
        "settle" timer when an event is coalesced: the timer fires at the
        window edge and re-applies one ``FOCUS_CHANGE`` so the burst's final
        state always gets exactly one re-walk. Pure -- the caller supplies
        ``now_ms``; this only reads the anchor ``should_fire`` recorded.
        """
        if self.debounce_ms <= 0 or self._last_fired_ms is None:
            return 0.0
        return max(0.0, self.debounce_ms - (now_ms - self._last_fired_ms))

    def reset(self) -> None:
        """Clear the window so the next event is treated as a first event.

        Used when the overlay session ends (the machine returns to ``closed``)
        so a stale window anchor from a previous session does not suppress the
        first focus change of the next one.
        """
        self._last_fired_ms = None


@dataclass(frozen=True)
class ForegroundIdentity:
    """The full foreground-window identity for the resume validity check.

    Mirrors the four identity fields the Phase 1 ``WalkSnapshot`` /
    ``ForegroundContext`` already carry (``foreground_window``,
    ``foreground_pid``, ``foreground_process_name``,
    ``foreground_window_creation_time``). The resume-time check compares the
    identity captured when the overlay was built against the CURRENT foreground
    identity using ALL four fields -- the same full-identity rule
    ``ElementFinder.get_snapshot`` enforces -- because Windows reuses HWND
    values, so a recycled HWND landing on a different process must invalidate
    the cached overlay (r2.8). ``IsWindow`` alone would miss that case.
    """

    hwnd: int
    pid: int
    process_name: str
    window_creation_time: int


def identity_matches(
    captured: ForegroundIdentity, current: ForegroundIdentity
) -> bool:
    """Return True iff every identity field matches (full-identity rule, r2.8).

    Used at the mic-resume transition: ``captured`` is the identity stored when
    the overlay was built; ``current`` is the foreground identity sampled at
    resume. Any differing field -- HWND, PID, process name, or window creation
    time -- means the focused window is no longer the one the overlay was built
    for, so the cached snapshot is stale (the integration invalidates and
    unpins it and re-walks). This is deliberately NOT an ``IsWindow``-only
    check: it catches the HWND-reuse trap where a recycled HWND lands on a
    different process (same ``hwnd`` but different ``pid`` /
    ``window_creation_time``).
    """
    return (
        captured.hwnd == current.hwnd
        and captured.pid == current.pid
        and captured.process_name == current.process_name
        and captured.window_creation_time == current.window_creation_time
    )


def map_foreground_event(*, hwnd: int) -> OverlayEvent:
    """Map a raw ``EVENT_SYSTEM_FOREGROUND`` event to a ``FOCUS_CHANGE`` event.

    Phase 1.5 ``FOCUS_CHANGE`` carries no generation (the machine reads its own
    current pair). The ``hwnd`` is accepted for symmetry with the destroy
    mapping and so the integration can record the new tracked window alongside
    feeding the event; it is not stamped on the event (the machine does not
    consume it).
    """
    return OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE)


def map_menu_popup_event(*, event_id: int) -> Optional[OverlayEvent]:
    """Map a menu pop-up open/close WinEvent to ``FOCUS_CHANGE``, or ``None``.

    wh-overlay-menu-close-stale: closing a menu does not change the foreground
    window, so the FOREGROUND hook never fires and badges painted for the
    (now gone) menu items stay floating over page controls that carry their
    own badges. The menu pop-up hook closes that gap; this mapper turns its
    raw event into the machine event.

    The reuse of ``FOCUS_CHANGE`` is DELIBERATE, not a shortcut: a menu
    opening or closing means "what is clickable over the focused window
    changed without the focus moving", and the machine's per-state
    focus-change handling is exactly the desired menu behaviour in every
    state -- supersede-refresh while ``painted`` / ``refresh_in_flight``
    (rebuild the badge set against current reality; the owned-popup walk
    numbers a newly OPENED menu's items too), restart while a build is in
    flight, and record-only NO_OP while ``closed`` / ``paused`` / ``error``
    (menus open and close all day with no overlay on screen). No new event
    kind and no state-machine change is needed, so the generation gate,
    trace-id discipline, and timer handling are inherited unchanged.

    Only the pop-up pair maps; any other id (including
    ``EVENT_SYSTEM_MENUSTART`` / ``MENUEND``, the menu-BAR tracking events
    the registered 0x0006..0x0007 range already excludes) returns ``None`` so
    the caller can drop it BEFORE burning the shared debounce window.
    """
    if event_id in (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND):
        return OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE)
    return None


def map_destroy_event(
    *, destroyed_hwnd: int, tracked_hwnd: int
) -> Optional[OverlayEvent]:
    """Map a raw ``EVENT_OBJECT_DESTROY`` to ``FOCUSED_HWND_DESTROYED``, or None.

    The transient destroy hook is registered (only while ``paused``) filtered
    to the tracked window's pid/tid, but a process emits ``EVENT_OBJECT_DESTROY``
    for many child objects, not just its top-level window. Logic narrows to the
    tracked HWND here: only the destruction of the EXACT tracked top-level
    window drives the ``paused -> closed`` edge. Returns ``None`` (no event) for
    any other destroyed HWND, and for a zero ``tracked_hwnd`` (no window is
    being tracked, so nothing can match). The returned ``FOCUSED_HWND_DESTROYED``
    carries no generation (the machine reads its own state).
    """
    if tracked_hwnd == 0 or destroyed_hwnd != tracked_hwnd:
        return None
    return OverlayEvent(kind=OverlayEventKind.FOCUSED_HWND_DESTROYED)


__all__ = [
    "DEFAULT_FOCUS_DEBOUNCE_MS",
    "EVENT_SYSTEM_MENUPOPUPEND",
    "EVENT_SYSTEM_MENUPOPUPSTART",
    "FocusChangeDebouncer",
    "ForegroundIdentity",
    "identity_matches",
    "map_destroy_event",
    "map_foreground_event",
    "map_menu_popup_event",
]
