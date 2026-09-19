"""Post-click settle detection: how Wheelhouse knows an application has
finished changing before it reads the screen and paints numbered badges.

Decision record: wh-overlay-slow-uia-stale-badges.10, comments "DESIGN
DECISIONS" (2026-08-22) and "DECISION ... OPTION 3" (2026-08-27). The
mechanism:

* Two consecutive reads of the window prove it settled when the ordered
  list of (control type id, accessible name, bounding rectangle) is
  identical for every control.
* A structure event arriving inside the pair's observation window -- from
  the start of the first read to the end of the second -- voids the match
  and restarts it. Probe run 3 on the bead recorded why: a pause in the
  middle of a content swap let two reads agree at 813 ms while the window
  kept changing until about 1.3 s; the StructureChanged stream covered
  exactly the changing interval.
* After ``max_ms`` the detector starts no further read and, when no pair
  has matched, returns the last completed read with ``settled=False``.
  The caller always has something to paint; a window that never settles
  still produces an answer. A read already in flight when the deadline
  passes completes and is used -- a matching pair it completes still
  reports ``settled=True`` -- so the worst-case answer time is ``max_ms``
  plus one read.
* When the event listener cannot register (an accessibility provider that
  refuses subscriptions), the detector degrades to plain two matching
  reads: ``events_between=None`` disables the void rule only.

The pure loop (`wait_for_settled_window`) takes injected callables so it
is testable without COM. `StructureEventListener` is the thin COM seam,
built on the same subscription set the probe validated live
(StructureChanged, LayoutInvalidated, AsyncContentLoaded,
LiveRegionChanged; scripts/probe_settle_events.py, MEASUREMENT 2-3 on the
bead: registration succeeds on Electron, read cost with subscriptions is
about 6 percent over the bare walker).

Integration (wh-overlay-slow-uia-stale-badges.2, not this module): the
Input process runs the detector during the overlay's post_click_settling
state, with ``read`` bound to ``ui.uia_walker.walk_window`` on the focused
window and ``max_ms`` from the click time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

# (OSError, COMError) with comtypes present, (OSError,) without; importing
# uia_walker does not touch COM (its comtypes import is guarded).
from ui.uia_walker import _STALE_WINDOW_ERRORS

logger = logging.getLogger(__name__)

# Maximum wait, milliseconds, measured from the detector's start. David set
# 1500 ms on the bead (DESIGN DECISIONS point 4); do not change it without
# asking him.
DEFAULT_SETTLE_MAX_MS = 1500

# UI Automation event ids, from UIAutomationClient.h. StructureChanged has
# its own registration call; the other three share AddAutomationEventHandler.
_UIA_LAYOUT_INVALIDATED = 20008
_UIA_ASYNC_CONTENT_LOADED = 20006
_UIA_LIVE_REGION_CHANGED = 20024
_AUTOMATION_EVENT_IDS = (
    _UIA_LAYOUT_INVALIDATED,
    _UIA_ASYNC_CONTENT_LOADED,
    _UIA_LIVE_REGION_CHANGED,
)


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def signature_of(matches: list[Any]) -> list[tuple[int, str, tuple[int, ...]]]:
    """The ordered comparison list David chose: control type id, accessible
    name, bounding rectangle -- one entry per walked control."""

    return [(m.control_type_id, m.name, tuple(m.bounds)) for m in matches]


@dataclass(frozen=True)
class SettleResult:
    """What the detector observed. ``matches`` is always the last completed
    read, whether or not the window settled."""

    settled: bool
    matches: list[Any]
    signature: list[tuple[int, str, tuple[int, ...]]]
    read_count: int
    voided_pairs: int
    elapsed_ms: float


def wait_for_settled_window(
    read: Callable[[], list[Any]],
    *,
    events_between: Optional[Callable[[float, float], bool]] = None,
    now_ms: Callable[[], float] = _monotonic_ms,
    max_ms: float = DEFAULT_SETTLE_MAX_MS,
) -> SettleResult:
    """Read the window until two consecutive reads agree, then return.

    ``read`` performs one walk and returns the match list. ``events_between``
    answers "did a structure event arrive in [start_ms, end_ms]?" on the
    ``now_ms`` clock; ``None`` means no listener is available and the void
    rule is skipped.

    Deadline contract: the loop samples the clock once per iteration, and
    that one sample is both the deadline check and the recorded read
    start, so no loop read starts at or past the deadline. The FIRST read
    always runs whatever the clock says -- with no completed read there is
    nothing to paint (decision point 3: never paint nothing). A read
    already in flight when the deadline passes completes, and if it
    completes a matching pair with no event in the observation window the
    result is ``settled=True`` even though the deadline has passed: the
    evidence is the same as for any matching pair, and on a slow machine
    whose single read approaches ``max_ms`` this is the only way
    ``settled=True`` can ever be reported. When the deadline expires
    without a matching pair, the last completed read is returned with
    ``settled=False``.

    Exception contract: a stale-window error from a loop read (the clicked
    window closed or its application crashed mid-detection;
    ``walk_window`` re-raises after its retry bound) returns the last
    completed read with ``settled=False``. The same error from the FIRST
    read propagates -- there is no completed read to return, and the
    caller's normal walk-failure handling applies. Non-stale exceptions
    always propagate; the walker treats them as programming errors.
    """

    t0 = now_ms()
    deadline = t0 + max_ms

    pair_start = now_ms()
    matches = read()
    signature = signature_of(matches)
    read_count = 1
    voided_pairs = 0

    while True:
        # One sample serves as the deadline check AND the recorded read
        # start: no clock hop can slip a read past the deadline between
        # a separate check and the read (review finding .10.1.5).
        next_start = now_ms()
        if next_start >= deadline:
            break
        try:
            next_matches = read()
        except _STALE_WINDOW_ERRORS:
            logger.info(
                "settle detector: the window vanished mid-detection after "
                "%s reads; returning the last read",
                read_count,
                exc_info=True,
            )
            return SettleResult(
                settled=False,
                matches=matches,
                signature=signature,
                read_count=read_count,
                voided_pairs=voided_pairs,
                elapsed_ms=now_ms() - t0,
            )
        next_end = now_ms()
        next_signature = signature_of(next_matches)
        read_count += 1

        if next_signature == signature:
            observed_change = events_between is not None and events_between(
                pair_start, next_end
            )
            if not observed_change:
                return SettleResult(
                    settled=True,
                    matches=next_matches,
                    signature=next_signature,
                    read_count=read_count,
                    voided_pairs=voided_pairs,
                    elapsed_ms=now_ms() - t0,
                )
            voided_pairs += 1

        matches = next_matches
        signature = next_signature
        pair_start = next_start

    logger.info(
        "settle detector hit max_ms=%s after %s reads (%s voided pairs); "
        "returning the last read",
        max_ms,
        read_count,
        voided_pairs,
    )
    return SettleResult(
        settled=False,
        matches=matches,
        signature=signature,
        read_count=read_count,
        voided_pairs=voided_pairs,
        elapsed_ms=now_ms() - t0,
    )


# Upper bound on stored event timestamps (review finding .10.1.6). A
# misbehaving provider can fire without end -- probe run 3 recorded 91
# StructureChanged events in one content swap -- and a hung read means the
# caller may never reach stop(). The store keeps the newest entries and
# remembers the newest timestamp it evicted; a query that overlaps evicted
# evidence answers True, which voids the pair -- the safe direction
# (review finding .10.1.10).
_EVENT_STORE_MAX = 4096


class EventTimes:
    """Timestamps of observed events; written by COM callback threads, read
    by the detector loop, so every access holds the lock. Bounded to the
    newest ``_EVENT_STORE_MAX`` entries; evicted evidence is never taken
    as silence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._times: deque[float] = deque(maxlen=_EVENT_STORE_MAX)
        self._newest_evicted = float("-inf")

    def add(self, t_ms: float) -> None:
        with self._lock:
            if len(self._times) == _EVENT_STORE_MAX:
                self._newest_evicted = max(self._newest_evicted, self._times[0])
            self._times.append(t_ms)

    def any_between(self, start_ms: float, end_ms: float) -> bool:
        with self._lock:
            # An evicted timestamp at or after the interval start may have
            # been the event this query is looking for; lost evidence must
            # not read as a quiet window.
            if self._newest_evicted >= start_ms:
                return True
            return any(start_ms <= t <= end_ms for t in self._times)


class StructureEventListener:
    """Subscribes to the UI Automation structure events on one window
    subtree and answers "did anything fire in this interval?".

    ``start`` returns True when at least one subscription registered; a
    partially registered listener still vetoes on what it can see. When
    every registration fails the listener stays unavailable and
    ``any_event_between`` always answers False -- the detector then runs
    plain two matching reads. The caller pairs every successful ``start``
    with ``stop``.
    """

    def __init__(
        self,
        hwnd: int,
        *,
        now_ms: Callable[[], float] = _monotonic_ms,
        create_automation_fn: Optional[Callable[[], Any]] = None,
        element_from_hwnd_fn: Optional[Callable[[Any, int], Any]] = None,
    ) -> None:
        self._hwnd = hwnd
        self._now_ms = now_ms
        self._create_automation_fn = create_automation_fn
        self._element_from_hwnd_fn = element_from_hwnd_fn
        self._times = EventTimes()
        self._automation: Any = None
        # The comtypes handler objects must outlive the subscriptions; this
        # list is the keepalive (same pattern as the probe script).
        self._handlers: list[Any] = []
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _record(self) -> None:
        # Disarmed once the listener stops (or never became available):
        # a failed RemoveAllEventHandlers, or a caller that drops the
        # listener without stop(), leaves the COM registrations alive, and
        # nothing reads the store after that -- appending would grow it for
        # the process lifetime.
        if not self._available:
            return
        self._times.add(self._now_ms())

    def any_event_between(self, start_ms: float, end_ms: float) -> bool:
        if not self._available:
            return False
        return self._times.any_between(start_ms, end_ms)

    def start(self) -> bool:
        registered: list[str] = []
        try:
            create_automation_fn = self._create_automation_fn
            element_from_hwnd_fn = self._element_from_hwnd_fn
            if create_automation_fn is None or element_from_hwnd_fn is None:
                from ui.uia_walker import create_automation, element_from_hwnd

                create_automation_fn = create_automation_fn or create_automation
                element_from_hwnd_fn = element_from_hwnd_fn or element_from_hwnd

            import comtypes

            from ui.uia_walker import _uia_module

            # _uia_module generates comtypes' UIAutomationClient module on a
            # fresh venv (wh-uia-gen-fresh-venv); a bare comtypes.gen import
            # would fail there.
            uia = _uia_module()

            automation = create_automation_fn()
            element = element_from_hwnd_fn(automation, self._hwnd)
            scope = uia.TreeScope_Subtree
        except Exception:
            logger.warning(
                "settle listener could not reach UI Automation; "
                "degrading to plain two matching reads",
                exc_info=True,
            )
            return False

        owner = self

        class _StructureChangedHandler(comtypes.COMObject):
            _com_interfaces_ = [uia.IUIAutomationStructureChangedEventHandler]

            def IUIAutomationStructureChangedEventHandler_HandleStructureChangedEvent(
                self, _sender: Any, _change_type: Any, _runtime_id: Any,
            ) -> int:
                owner._record()
                return 0

        class _AutomationEventHandler(comtypes.COMObject):
            _com_interfaces_ = [uia.IUIAutomationEventHandler]

            def IUIAutomationEventHandler_HandleAutomationEvent(
                self, _sender: Any, _event_id: Any,
            ) -> int:
                owner._record()
                return 0

        structure_handler = _StructureChangedHandler()
        self._handlers.append(structure_handler)
        try:
            automation.AddStructureChangedEventHandler(
                element, scope, None, structure_handler,
            )
            registered.append("StructureChanged")
        except Exception:
            logger.warning(
                "settle listener: StructureChanged registration failed",
                exc_info=True,
            )

        for event_id in _AUTOMATION_EVENT_IDS:
            handler = _AutomationEventHandler()
            self._handlers.append(handler)
            try:
                automation.AddAutomationEventHandler(
                    event_id, element, scope, None, handler,
                )
                registered.append(str(event_id))
            except Exception:
                logger.warning(
                    "settle listener: event %s registration failed",
                    event_id,
                    exc_info=True,
                )

        if not registered:
            logger.warning(
                "settle listener: no subscription registered on hwnd=%s; "
                "degrading to plain two matching reads",
                self._hwnd,
            )
            return False

        self._automation = automation
        self._available = True
        return True

    def stop(self) -> None:
        automation = self._automation
        self._automation = None
        self._available = False
        self._handlers.clear()
        if automation is None:
            return
        try:
            automation.RemoveAllEventHandlers()
        except Exception:
            logger.warning(
                "settle listener: RemoveAllEventHandlers failed", exc_info=True,
            )
