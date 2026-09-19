"""Probe: what does an application fire when its content changes, and how
long do repeated screen reads take to agree?

wh-overlay-slow-uia-stale-badges.10.

WHY THIS EXISTS
===============
After Wheelhouse clicks a numbered badge it must know when the application
has finished redrawing, so it can read the screen again and paint correct
numbers. Nothing detects that today. ``FocusChangeDebouncer``
(``overlay_focus_hooks.py``) only groups timestamps its callers hand it, and
``main.py`` registers three Windows event hooks: foreground change
(0x0003), menu popup open and close (0x0006..0x0007), and object destroy
(0x8003, only while the overlay is paused). A repaint inside the same
window can therefore fire nothing that Wheelhouse listens to.

Two mechanisms were proposed. This probe measures both before either one
is built:

1. TWO MATCHING READS. Read the window repeatedly and accept the result
   when two reads in a row agree. This probe records every read, its
   duration, and whether it matched the read before it.
2. SUBSCRIBE TO CONTENT-CHANGE EVENTS. This probe subscribes to the whole
   Windows event range and to the UI Automation structure, layout and
   content events, and records everything the target process fires.

WHAT IT MEASURES
================
* Every Windows event (``SetWinEventHook`` over the whole range, filtered
  to the target process) with its offset in milliseconds from the click.
* Every UI Automation StructureChanged, LayoutInvalidated,
  AsyncContentLoaded and LiveRegionChanged event on the target window
  subtree, and optionally every Name and BoundingRectangle property change.
* Every screen read: start offset, duration, control count, and how many
  controls were added or removed since the previous read.

The read comparison is the one David chose on 2026-08-22: two reads match
when the ordered list of (control type id, accessible name, bounding
rectangle) is identical.

HOW TO RUN IT
=============
From ``services/wheelhouse``::

    uv run python scripts/probe_settle_events.py

The probe counts down, takes the foreground window as the target, reads it
once as a baseline, registers its hooks, and then waits for you to click
inside that window. Click something that swaps the content -- in the Claude
desktop application, switching to another conversation is the case that
caused this bug. The probe records for ten seconds and writes a JSON file.

Options::

    --title SUBSTRING   pick the target window by title instead of the
                        countdown
    --seconds N         how long to record after the click (default 10)
    --max-ms N          the settle budget to report against (default 1500)
    --countdown N       seconds to focus the target window (default 5)
    --uia-properties    also subscribe to Name and BoundingRectangle
                        property changes (very noisy on Chromium)
    --no-uia-events     record Windows events only
    --out PATH          where to write the JSON record

WHAT THIS PROBE DOES NOT DO
===========================
It uses a physical mouse click as the trigger, not the UI Automation
Invoke call that Wheelhouse really uses. A physical click produces the same
content change; the trigger instant is the button press, which is a few
milliseconds before the button release that the application acts on.

It walks the window exactly as the overlay does
(``skip_offscreen_or_zero_area=True``) but applies neither the browser DOM
correction pass nor the confidence scorer, because both run after the walk
and neither can change whether the screen has settled.

The cost of the UI Automation subscriptions is measured by comparison, not
directly: run the probe once with ``--no-uia-events`` and once without that
option, then compare the read durations.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# The script lives at services/wheelhouse/scripts/probe_settle_events.py and
# imports the production walker, which imports `from ui.element_types ...`,
# so services/wheelhouse must be on sys.path exactly as it is in the running
# Input process.
SCRIPT_DIR = Path(__file__).resolve().parent
WHEELHOUSE_ROOT = SCRIPT_DIR.parent
if str(WHEELHOUSE_ROOT) not in sys.path:
    sys.path.insert(0, str(WHEELHOUSE_ROOT))

from ui.uia_walker import (  # noqa: E402
    build_cache_request,
    create_automation,
    element_from_hwnd,
    walk_window,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Windows event range. WinUser.h: EVENT_MIN .. EVENT_MAX. Registering the
# whole range and filtering by process id records everything the target
# fires, which is the question this probe answers.
EVENT_MIN = 0x00000001
EVENT_MAX = 0x7FFFFFFF
WINEVENT_OUTOFCONTEXT = 0x0000
PM_REMOVE = 0x0001
VK_LBUTTON = 0x01

# An event offset of -1 ms marks an event that arrived before the click.
BEFORE_CLICK_MS = -1.0

# The events worth naming in the report. Every other id is reported as its
# hexadecimal value, which is enough to look up in WinUser.h.
WINEVENT_NAMES: dict[int, str] = {
    0x0001: "EVENT_SYSTEM_SOUND",
    0x0002: "EVENT_SYSTEM_ALERT",
    0x0003: "EVENT_SYSTEM_FOREGROUND",
    0x0006: "EVENT_SYSTEM_MENUPOPUPSTART",
    0x0007: "EVENT_SYSTEM_MENUPOPUPEND",
    0x000A: "EVENT_SYSTEM_CAPTURESTART",
    0x000B: "EVENT_SYSTEM_CAPTUREEND",
    0x0016: "EVENT_SYSTEM_MINIMIZESTART",
    0x0017: "EVENT_SYSTEM_MINIMIZEEND",
    0x8000: "EVENT_OBJECT_CREATE",
    0x8001: "EVENT_OBJECT_DESTROY",
    0x8002: "EVENT_OBJECT_SHOW",
    0x8003: "EVENT_OBJECT_HIDE",
    0x8004: "EVENT_OBJECT_REORDER",
    0x8005: "EVENT_OBJECT_FOCUS",
    0x8006: "EVENT_OBJECT_SELECTION",
    0x8007: "EVENT_OBJECT_SELECTIONADD",
    0x8008: "EVENT_OBJECT_SELECTIONREMOVE",
    0x8009: "EVENT_OBJECT_SELECTIONWITHIN",
    0x800A: "EVENT_OBJECT_STATECHANGE",
    0x800B: "EVENT_OBJECT_LOCATIONCHANGE",
    0x800C: "EVENT_OBJECT_NAMECHANGE",
    0x800D: "EVENT_OBJECT_DESCRIPTIONCHANGE",
    0x800E: "EVENT_OBJECT_VALUECHANGE",
    0x800F: "EVENT_OBJECT_PARENTCHANGE",
    0x8010: "EVENT_OBJECT_HELPCHANGE",
    0x8011: "EVENT_OBJECT_DEFACTIONCHANGE",
    0x8012: "EVENT_OBJECT_ACCELERATORCHANGE",
    0x8013: "EVENT_OBJECT_INVOKED",
    0x8014: "EVENT_OBJECT_TEXTSELECTIONCHANGED",
    0x8015: "EVENT_OBJECT_CONTENTSCROLLED",
    0x8016: "EVENT_SYSTEM_ARRANGMENTPREVIEW",
    0x8017: "EVENT_OBJECT_CLOAKED",
    0x8018: "EVENT_OBJECT_UNCLOAKED",
    0x8019: "EVENT_OBJECT_LIVEREGIONCHANGED",
    0x8020: "EVENT_OBJECT_DRAGSTART",
    0x8021: "EVENT_OBJECT_DRAGCANCEL",
    0x8022: "EVENT_OBJECT_DRAGCOMPLETE",
    0x8023: "EVENT_OBJECT_DRAGENTER",
    0x8024: "EVENT_OBJECT_DRAGLEAVE",
    0x8025: "EVENT_OBJECT_DRAGDROPPED",
    0x8026: "EVENT_OBJECT_IME_SHOW",
    0x8027: "EVENT_OBJECT_IME_HIDE",
    0x8028: "EVENT_OBJECT_IME_CHANGE",
    0x8030: "EVENT_OBJECT_TEXTEDIT_CONVERSIONTARGETCHANGED",
}

# UI Automation event ids this probe subscribes to, from UIAutomationClient.h.
UIA_EVENT_NAMES: dict[int, str] = {
    20002: "UIA_StructureChangedEventId",
    20006: "UIA_AsyncContentLoadedEventId",
    20008: "UIA_LayoutInvalidatedEventId",
    20024: "UIA_LiveRegionChangedEventId",
}

# The subscription names that can veto an option 3 pair -- the four the
# detector itself subscribes to. PropertyChanged is opt-in probe noise,
# not a veto source (review finding .10.1.11).
CORE_VETO_SOURCES = {
    "StructureChanged",
    "UIA_LayoutInvalidatedEventId",
    "UIA_AsyncContentLoadedEventId",
    "UIA_LiveRegionChangedEventId",
}

# The record keeps at most this many events. A page that fires constantly
# would otherwise fill memory; the report states how many were dropped.
MAX_RECORDED_EVENTS = 20000

WIN_EVENT_PROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)

user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WIN_EVENT_PROC,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_uint,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

GA_ROOT = 2


def top_level_under_cursor() -> int:
    """Return the top-level window the mouse pointer is over, or 0.

    The probe uses this to ignore a click that lands outside the target
    window. Without it, the click that brings the target window to the front
    would be taken as the trigger, and every measurement would be timed from
    the wrong instant.
    """

    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return 0
    hwnd_at_point = user32.WindowFromPoint(point)
    if not hwnd_at_point:
        return 0
    return int(user32.GetAncestor(hwnd_at_point, GA_ROOT) or 0)


def window_title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_process_id(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def find_window_by_title(substring: str) -> Optional[int]:
    """Return the first visible top-level window whose title contains the text."""

    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM,
    )
    found: list[int] = []
    wanted = substring.lower()

    def _callback(hwnd: Any, _lparam: Any) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if wanted in window_title(hwnd).lower():
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(enum_proc_type(_callback), 0)
    return found[0] if found else None


def signature_of(matches: list[Any]) -> list[tuple[int, str, tuple[int, ...]]]:
    """The ordered list David chose: control type id, name, bounding rectangle."""

    return [(m.control_type_id, m.name, tuple(m.bounds)) for m in matches]


class Recorder:
    """Collects timestamped events from the hook thread and the UIA threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self.events: list[dict[str, Any]] = []
        self.dropped = 0
        # The offset of the first dropped event: the record is incomplete
        # from this time on (review finding .10.1.12).
        self.first_dropped_t_ms: Optional[float] = None
        self.t0: Optional[float] = None

    def offset_ms(self) -> float:
        """Milliseconds since the click, or -1 for an event before the click."""

        if self.t0 is None:
            return BEFORE_CLICK_MS
        return (time.monotonic() - self.t0) * 1000.0

    def add(self, source: str, name: str, detail: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            # The stamp shares the lock with the store, so stamp order is
            # storage order: a descheduled callback can no longer hold an
            # earlier stamp than a boundary already counted (review
            # finding .10.1.15).
            entry = {
                "t_ms": round(self.offset_ms(), 2), "source": source, "event": name,
            }
            entry.update(detail)
            if len(self.events) >= MAX_RECORDED_EVENTS:
                # With lock-ordered stamps this minimum cannot lose to an
                # inversion any more; kept as a cheap invariant defense
                # (review finding .10.1.14).
                if (self.first_dropped_t_ms is None
                        or entry["t_ms"] < self.first_dropped_t_ms):
                    self.first_dropped_t_ms = entry["t_ms"]
                self.dropped += 1
                return
            self.events.append(entry)

    def close(self) -> tuple[list[dict[str, Any]], int, Optional[float]]:
        """Stop recording and return one consistent (events, dropped,
        first_dropped_t_ms) snapshot for the summary and the JSON record
        (review finding .10.1.15). A callback that takes the lock after
        the close stamps after it too, so ignoring it equals the
        unavoidable case of an event delivered after recording ended.
        """

        with self._lock:
            self._closed = True
            return list(self.events), self.dropped, self.first_dropped_t_ms


def register_uia_event_handlers(
    recorder: Recorder, hwnd: int, with_properties: bool,
) -> tuple[Any, list[str]]:
    """Subscribe to the UI Automation content events on one window subtree.

    Returns the automation object that holds the subscriptions (the caller
    keeps it alive and later calls RemoveAllEventHandlers) and the list of
    subscriptions that succeeded. Every failure is printed rather than
    raised: the Windows event record alone is still worth having.
    """

    import comtypes
    from comtypes.gen import UIAutomationClient as uia

    registered: list[str] = []
    automation = create_automation()
    element = element_from_hwnd(automation, hwnd)
    scope = uia.TreeScope_Subtree

    class StructureChangedHandler(comtypes.COMObject):
        _com_interfaces_ = [uia.IUIAutomationStructureChangedEventHandler]

        def IUIAutomationStructureChangedEventHandler_HandleStructureChangedEvent(
            self, _sender: Any, change_type: Any, _runtime_id: Any,
        ) -> int:
            recorder.add(
                "uia", "StructureChanged", {"change_type": int(change_type)},
            )
            return 0

    class AutomationEventHandler(comtypes.COMObject):
        _com_interfaces_ = [uia.IUIAutomationEventHandler]

        def IUIAutomationEventHandler_HandleAutomationEvent(
            self, _sender: Any, event_id: Any,
        ) -> int:
            numeric = int(event_id)
            recorder.add(
                "uia",
                UIA_EVENT_NAMES.get(numeric, f"uia_event_{numeric}"),
                {"event_id": numeric},
            )
            return 0

    class PropertyChangedHandler(comtypes.COMObject):
        _com_interfaces_ = [uia.IUIAutomationPropertyChangedEventHandler]

        def IUIAutomationPropertyChangedEventHandler_HandlePropertyChangedEvent(
            self, _sender: Any, property_id: Any, _new_value: Any,
        ) -> int:
            recorder.add(
                "uia", "PropertyChanged", {"property_id": int(property_id)},
            )
            return 0

    # The handler objects must outlive this function. The automation object
    # holds the subscriptions, so it holds the Python objects too.
    handlers: list[Any] = []
    automation._probe_handlers = handlers  # type: ignore[attr-defined]

    structure_handler = StructureChangedHandler()
    handlers.append(structure_handler)
    try:
        automation.AddStructureChangedEventHandler(
            element, scope, None, structure_handler,
        )
        registered.append("StructureChanged")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] StructureChanged subscription failed: {exc}")

    for event_id in (20008, 20006, 20024):
        handler = AutomationEventHandler()
        handlers.append(handler)
        try:
            automation.AddAutomationEventHandler(
                event_id, element, scope, None, handler,
            )
            registered.append(UIA_EVENT_NAMES.get(event_id, str(event_id)))
        except Exception as exc:  # noqa: BLE001
            print(f"[!] event {event_id} subscription failed: {exc}")

    if with_properties:
        property_handler = PropertyChangedHandler()
        handlers.append(property_handler)
        # UIA_NamePropertyId 30005, UIA_BoundingRectanglePropertyId 30001.
        property_ids = (ctypes.c_int * 2)(30005, 30001)
        try:
            automation.AddPropertyChangedEventHandlerNativeArray(
                element, scope, None, property_handler, property_ids, 2,
            )
            registered.append("PropertyChanged(Name,BoundingRectangle)")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] PropertyChanged subscription failed: {exc}")

    return automation, registered


def read_loop(
    hwnd: int,
    recorder: Recorder,
    start_event: threading.Event,
    stop_event: threading.Event,
    reads: list[dict[str, Any]],
    baseline: dict[str, Any],
    baseline_done: threading.Event,
) -> None:
    """Walk the window once for a baseline, then repeatedly after the click.

    Runs on its own thread with its own single-threaded apartment and its own
    automation object. No COM pointer crosses back to the main thread; only
    numbers, strings and tuples do.
    """

    import comtypes

    comtypes.CoInitialize()
    try:
        automation = create_automation()
        cache_request = build_cache_request(automation)

        def one_read() -> tuple[float, list[Any]]:
            started = time.monotonic()
            result = walk_window(
                hwnd,
                automation=automation,
                cache_request=cache_request,
                skip_offscreen_or_zero_area=True,
            )
            return (time.monotonic() - started) * 1000.0, list(result.matches)

        duration, matches = one_read()
        baseline["duration_ms"] = round(duration, 1)
        baseline["count"] = len(matches)
        baseline["signature"] = signature_of(matches)
        baseline_done.set()

        start_event.wait()
        previous = baseline["signature"]
        index = 0
        while not stop_event.is_set():
            start_offset = recorder.offset_ms()
            duration, matches = one_read()
            current = signature_of(matches)
            previous_counter = Counter(map(repr, previous))
            current_counter = Counter(map(repr, current))
            added = sum((current_counter - previous_counter).values())
            removed = sum((previous_counter - current_counter).values())
            reads.append(
                {
                    "index": index,
                    "start_ms": round(start_offset, 1),
                    "end_ms": round(start_offset + duration, 1),
                    "duration_ms": round(duration, 1),
                    "count": len(matches),
                    "added_since_previous": added,
                    "removed_since_previous": removed,
                    "matches_previous": current == previous,
                }
            )
            previous = current
            index += 1
    except Exception as exc:  # noqa: BLE001
        reads.append({"error": repr(exc)})
        # A baseline failure must wake main immediately with the real
        # error, not a 30-second timeout message (review finding
        # .10.1.13). After the baseline this set() is a no-op.
        baseline_done.set()
    finally:
        comtypes.CoUninitialize()


def reader_completed(reader: threading.Thread, timeout_s: float) -> bool:
    """Join with a bound; a reader still alive means the walk hung."""

    reader.join(timeout=timeout_s)
    return not reader.is_alive()


def failed_read(reads: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the error record a crashed read loop left, if any."""

    return next((r for r in reads if "error" in r), None)


def option3_settle(
    clean: list[dict[str, Any]],
    events: list[dict[str, Any]],
    dropped_from_ms: Optional[float] = None,
) -> tuple[Optional[dict[str, Any]], int, bool]:
    """Apply the decided mechanism to the recorded reads: a matching pair
    settles unless a subscribed UI Automation event arrived inside its
    observation window (first read start through second read end). The
    detector does not subscribe to Windows events or PropertyChanged, so
    neither can void a pair here. ``dropped_from_ms`` marks where the
    event record became incomplete; a window reaching that far cannot be
    certified quiet. Returns (the settling read, voided pairs, whether a
    candidate was rejected for incomplete evidence)."""

    veto_times = [
        event["t_ms"] for event in events
        if event.get("source") == "uia"
        and event.get("event") != "PropertyChanged"
        and event["t_ms"] >= 0.0
    ]
    by_index = {r["index"]: r for r in clean}
    voided = 0
    truncated = False
    for record in clean:
        if not record["matches_previous"]:
            continue
        # The pre-click baseline is not a read record, so read 0 has no
        # partner here and can never form a pair; the first pair is reads
        # 0 and 1 (review finding .10.1.7).
        first = by_index.get(record["index"] - 1)
        if first is None:
            continue
        window_start = first["start_ms"]
        window_end = record["end_ms"]
        if any(window_start <= t <= window_end for t in veto_times):
            voided += 1
            continue
        if dropped_from_ms is not None and window_end >= dropped_from_ms:
            # Dropped events may hide this window's veto; a quiet look
            # proves nothing (review finding .10.1.12).
            truncated = True
            continue
        return record, voided, truncated
    return None, voided, truncated


def summarize(
    reads: list[dict[str, Any]],
    events: list[dict[str, Any]],
    max_ms: int,
    veto_sources_active: bool,
    dropped_from_ms: Optional[float] = None,
) -> dict[str, Any]:
    """Answer the questions on the bead from the recorded data.

    ``veto_sources_active`` says whether at least one of the four veto
    subscriptions actually registered; without one, the run measured only
    the plain two-read fallback and no option 3 conclusion exists
    (review finding .10.1.11)."""

    clean = [r for r in reads if "error" not in r]
    durations = [r["duration_ms"] for r in clean]
    first_change = next(
        (
            r for r in clean
            if r["added_since_previous"] or r["removed_since_previous"]
        ),
        None,
    )
    # Read 0's matches_previous compares against the PRE-CLICK baseline;
    # a settled pair needs two post-click reads (review finding .10.1.7).
    settled = next(
        (r for r in clean if r["index"] >= 1 and r["matches_previous"]), None,
    )
    option3, option3_voided, option3_truncated = (
        option3_settle(clean, events, dropped_from_ms)
        if veto_sources_active
        else (None, 0, False)
    )
    events_before_settle = [
        event for event in events
        if settled is not None
        and 0.0 <= event["t_ms"] <= settled["end_ms"]
    ]
    counted: Counter = Counter(event["event"] for event in events)
    return {
        "reads": len(clean),
        "read_duration_ms": {
            "min": min(durations) if durations else None,
            "median": round(statistics.median(durations), 1) if durations else None,
            "max": max(durations) if durations else None,
        },
        "first_change_at_ms": first_change["end_ms"] if first_change else None,
        "settled_at_ms": settled["end_ms"] if settled else None,
        "settled_within_max": settled is not None and settled["end_ms"] <= max_ms,
        "option3_available": veto_sources_active,
        "option3_settled_at_ms": option3["end_ms"] if option3 else None,
        "option3_settled_within_max": (
            option3 is not None and option3["end_ms"] <= max_ms
        ),
        "option3_voided_pairs": option3_voided,
        "option3_truncated": option3_truncated,
        "max_ms": max_ms,
        "total_events": len(events),
        "events_before_settle": len(events_before_settle),
        "event_counts": dict(counted.most_common(20)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record what an application fires when its content changes.",
    )
    parser.add_argument("--title", default=None)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--max-ms", type=int, default=1500)
    parser.add_argument("--countdown", type=int, default=5)
    parser.add_argument("--uia-properties", action="store_true")
    parser.add_argument("--no-uia-events", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if args.title:
        found = find_window_by_title(args.title)
        if found is None:
            print(f"[x] no visible window has '{args.title}' in its title")
            return 1
        hwnd = found
    else:
        print(f"[!] focus the target window. Taking it in {args.countdown} seconds.")
        for remaining in range(args.countdown, 0, -1):
            print(f"    {remaining}")
            time.sleep(1.0)
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            print("[x] no foreground window")
            return 1

    target = {
        "hwnd": hwnd,
        "title": window_title(hwnd),
        "class_name": window_class(hwnd),
        "process_id": window_process_id(hwnd),
    }
    print(
        f"[+] target: {target['title']!r} class={target['class_name']} "
        f"pid={target['process_id']}"
    )

    recorder = Recorder()
    reads: list[dict[str, Any]] = []
    baseline: dict[str, Any] = {}
    start_event = threading.Event()
    stop_event = threading.Event()
    baseline_done = threading.Event()

    reader = threading.Thread(
        target=read_loop,
        args=(
            hwnd, recorder, start_event, stop_event, reads, baseline,
            baseline_done,
        ),
        name="probe-reader",
        daemon=True,
    )
    reader.start()
    if not baseline_done.wait(timeout=30.0):
        print("[x] the baseline read did not finish in 30 seconds")
        return 1
    if "count" not in baseline:
        print(f"[x] the baseline read failed: {reads}")
        return 1
    print(
        f"[+] baseline read: {baseline['count']} controls in "
        f"{baseline['duration_ms']} ms"
    )

    import comtypes

    comtypes.CoInitialize()
    uia_automation = None
    registered: list[str] = []
    hook = None
    try:
        if not args.no_uia_events:
            try:
                uia_automation, registered = register_uia_event_handlers(
                    recorder, hwnd, args.uia_properties,
                )
                print(f"[+] UI Automation subscriptions: {', '.join(registered)}")
            except Exception as exc:  # noqa: BLE001
                print(f"[!] no UI Automation subscriptions: {exc}")

        def _win_event(
            _hook: Any, event: Any, event_hwnd: Any, id_object: Any,
            id_child: Any, _thread: Any, _time: Any,
        ) -> None:
            numeric = int(event)
            recorder.add(
                "winevent",
                WINEVENT_NAMES.get(numeric, f"0x{numeric:04X}"),
                {
                    "event_id": numeric,
                    "hwnd": int(event_hwnd) if event_hwnd else 0,
                    "id_object": int(id_object),
                    "id_child": int(id_child),
                },
            )

        callback = WIN_EVENT_PROC(_win_event)
        hook = user32.SetWinEventHook(
            EVENT_MIN, EVENT_MAX, None, callback,
            target["process_id"], 0, WINEVENT_OUTOFCONTEXT,
        )
        if not hook:
            print(f"[x] SetWinEventHook failed (error {ctypes.get_last_error()})")
            return 1
        print("[+] Windows event hook registered over the whole event range")

        print(
            "\n[!] CLICK inside the target window now. Recording starts on "
            "the click.\n"
        )
        user32.GetAsyncKeyState(VK_LBUTTON)  # clear the was-pressed bit

        message = wintypes.MSG()

        def pump() -> None:
            while user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, PM_REMOVE,
            ):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))

        give_up_at = time.monotonic() + 120.0
        while recorder.t0 is None:
            pump()
            if user32.GetAsyncKeyState(VK_LBUTTON) & 0x0001:
                under_cursor = top_level_under_cursor()
                if under_cursor != hwnd:
                    # The click that raises the target window must not be
                    # taken as the trigger; only a click inside it counts.
                    print(
                        "[!] ignored a click outside the target window "
                        f"(window {under_cursor})"
                    )
                else:
                    recorder.t0 = time.monotonic()
                    start_event.set()
                    print(
                        f"[+] click recorded; recording for {args.seconds} seconds"
                    )
                    break
            if time.monotonic() > give_up_at:
                print("[x] no click within two minutes")
                stop_event.set()
                return 1
            time.sleep(0.002)

        end_at = recorder.t0 + args.seconds
        while time.monotonic() < end_at:
            pump()
            time.sleep(0.002)

        stop_event.set()
        if not reader_completed(reader, 30.0):
            # A reader still alive means the final walk hung; its results
            # list is still mutable and incomplete. Writing an exit-0
            # record here would hide the hung provider from a future
            # measurement run (review finding .10.1.9).
            print(
                "[x] the read loop is still running 30 seconds after "
                "recording ended; the walk is hung. No record written."
            )
            return 1
        error = failed_read(reads)
        if error is not None:
            # A crashed read loop left a partial reads list; a record
            # built from it would understate the settle time (review
            # finding .10.1.13).
            print(
                "[x] the read loop failed with "
                f"{error['error']}. No record written."
            )
            return 1
    finally:
        if hook:
            user32.UnhookWinEvent(hook)
        if uia_automation is not None:
            try:
                uia_automation.RemoveAllEventHandlers()
            except Exception as exc:  # noqa: BLE001
                print(f"[!] RemoveAllEventHandlers failed: {exc}")
        comtypes.CoUninitialize()

    # One consistent snapshot for the summary and the JSON record; the
    # live recorder attributes could change between the two reads
    # (review finding .10.1.15).
    events, events_dropped, events_dropped_from_ms = recorder.close()
    veto_sources_active = bool(CORE_VETO_SOURCES & set(registered))
    summary = summarize(
        reads,
        events,
        args.max_ms,
        veto_sources_active,
        events_dropped_from_ms,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(
        args.out or Path(tempfile.gettempdir()) / f"probe-settle-{stamp}.json"
    )
    record = {
        "target": target,
        "settings": {
            "seconds": args.seconds,
            "max_ms": args.max_ms,
            "uia_events": not args.no_uia_events,
            "uia_properties": args.uia_properties,
            "uia_subscriptions": registered,
        },
        "baseline": {
            "count": baseline.get("count"),
            "duration_ms": baseline.get("duration_ms"),
        },
        "reads": reads,
        "events": events,
        "events_dropped": events_dropped,
        "events_dropped_from_ms": events_dropped_from_ms,
        "summary": summary,
    }
    out_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    print("\n--- summary ---")
    print(
        f"reads: {summary['reads']}, duration ms min/median/max: "
        f"{summary['read_duration_ms']['min']}/"
        f"{summary['read_duration_ms']['median']}/"
        f"{summary['read_duration_ms']['max']}"
    )
    print(
        "first read that differed from the one before it: "
        f"{summary['first_change_at_ms']} ms"
    )
    print(
        "first two reads in a row that agreed, measured at the end of the "
        f"second read: {summary['settled_at_ms']} ms"
    )
    print(f"within the {args.max_ms} ms budget: {summary['settled_within_max']}")
    if summary["option3_available"]:
        truncation_note = (
            " (TRUNCATED: dropped events may hide a veto)"
            if summary["option3_truncated"]
            else ""
        )
        print(
            "option 3 (matching pair with no UI Automation event in its "
            f"observation window): {summary['option3_settled_at_ms']} ms, "
            f"voided pairs: {summary['option3_voided_pairs']}, within "
            f"budget: {summary['option3_settled_within_max']}"
            f"{truncation_note}"
        )
    else:
        print(
            "option 3: not measured -- no veto subscription registered "
            "this run"
        )
    print(
        f"events recorded: {summary['total_events']} (dropped "
        f"{events_dropped}), of which {summary['events_before_settle']} "
        "arrived before the reads agreed"
    )
    for name, count in summary["event_counts"].items():
        print(f"    {count:5d}  {name}")
    print(f"\nwritten to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
