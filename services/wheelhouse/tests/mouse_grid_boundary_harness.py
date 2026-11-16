"""Boundary harness for the mouse grid requirement tests (area L5).

Bead: wh-full-refactor.6.1 (stage A of place 3). The tests in
tests/test_mouse_grid_requirements_l5.py drive the mouse grid and the pointer
actions of the Logic process (``MouseGridController`` in
services/wheelhouse/mouse_grid/controller.py, which ``LogicController`` builds)
only through the inputs the shipped Logic process receives, and they read only
what leaves it.

This file adds what tests/overlay_boundary_harness.py does not cover:

  * THE MONITORS. The grid code of ``MouseGridController`` reads the monitor
    list and the DPI context through ``ctypes.windll.user32`` and
    ``ctypes.windll.shcore`` in shared/monitor_geometry.py, and the focused
    window's rectangle through ``win32gui.GetWindowRect``. ``FakeMonitors``
    replaces those operating system calls, so no test reads the real monitors
    of the machine. It replaces the ``ctypes`` name that monitor_geometry.py
    reads with a stand-in whose ``windll`` holds fake ``user32`` and
    ``shcore`` libraries; every other ``ctypes`` member is the real one,
    except ``byref``, which hands the structure itself to the fake call so
    the fake can fill it. No private member of a production class and no
    private function of a production module is named or patched.
  * THE ACTION SETS of the grid: the pointer commands for the Input process
    and the grid messages for the GUI process (the defaults of the overlay
    harness filter to the numbered overlay actions).
  * REPLIES of the Input process to the pointer commands, built through the
    production schema ``MouseActionResponse``, and to ``channel_probe``, built
    the way ``UIActionHandler.channel_probe`` builds it.
  * TYPED WORDS: an answerer for intelligent_insert_text requests, as the
    Input process answers them, so a test can read what was typed.
"""

from __future__ import annotations

import asyncio
import ctypes as _real_ctypes
import threading
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import services.wheelhouse.shared.monitor_geometry as monitor_geometry
from tests.overlay_boundary_harness import overlay_boundary

# The Logic-to-Input commands of the grid and of the pointer click.
POINTER_INPUT_ACTIONS = frozenset({
    "click_point",
    "move_pointer",
    "perform_drag",
    "click_at_pointer",
    "channel_probe",
})

# The Logic-to-GUI messages a grid command can cause. The numbered overlay's
# paint and clear are here too, because opening the grid closes the numbers.
GRID_GUI_ACTIONS = frozenset({
    "paint_grid",
    "paint_grid_pin",
    "clear_grid",
    "show_click_notice",
    "clear_overlay",
    "paint_overlay",
})

TYPED_ACTIONS = frozenset({"intelligent_insert_text"})


@dataclass(frozen=True)
class Monitor:
    """One monitor as Windows reports it, in physical pixels."""

    left: int
    top: int
    width: int
    height: int
    dpi: int = 96


PRIMARY = Monitor(0, 0, 1920, 1080)
SECOND = Monitor(1920, 0, 2560, 1440, dpi=144)


@dataclass
class FakeMonitors:
    """The answers Windows gives about monitors and the focused window.

    ``monitors``: what EnumDisplayMonitors reports, in order.
    ``window_rect``: what GetWindowRect returns for the foreground window, as
    (left, top, right, bottom); None makes GetWindowRect fail as it does for
    an invalid window handle.
    ``dpi_aware``: False makes SetThreadDpiAwarenessContext fail (it returns
    NULL) on a thread that is not per-monitor aware.
    ``enumeration_error``: an exception EnumDisplayMonitors raises (other than
    OSError, which shared/monitor_geometry.py catches).
    ``virtualized_scale``: None (the default) returns the physical rectangles
    whatever DPI context the thread has. A number makes the desktop a scaled
    one, as Windows shows it to a DPI-unaware thread: GetMonitorInfoW and
    GetWindowRect return each rectangle divided by the number while the
    calling thread's DPI awareness context is not the per-monitor context
    (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, -4). Each thread starts in
    the unaware context (0x11); SetThreadDpiAwarenessContext stores the
    context it gets for the calling thread and returns the previous one.
    ``park``: None (the default), or a threading.Event that EnumDisplayMonitors
    waits on (at most ``park_timeout_s``) before it reports the monitors. It
    sets ``parked`` when it starts to wait.
    """

    monitors: list = field(default_factory=lambda: [PRIMARY, SECOND])
    window_rect: Optional[tuple] = (100, 100, 900, 700)
    dpi_aware: bool = True
    enumeration_error: Optional[BaseException] = None
    enumerations: int = 0
    virtualized_scale: Optional[int] = None
    park: Optional[threading.Event] = None
    parked: threading.Event = field(default_factory=threading.Event)
    park_timeout_s: float = 5.0
    # The DPI awareness context of each thread, by threading.get_ident().
    thread_contexts: dict = field(default_factory=dict)

    def _virtualized(self) -> bool:
        """True when the calling thread reads virtualized rectangles."""
        if self.virtualized_scale is None:
            return False
        context = self.thread_contexts.get(threading.get_ident(), _UNAWARE)
        return context != _PER_MONITOR_AWARE_V2

    def _scaled(self, value: int) -> int:
        return value // self.virtualized_scale if self._virtualized() else value

    def fake_user32(self):
        fake = self

        def set_thread_dpi_awareness_context(context):
            if not fake.dpi_aware:
                return None
            thread = threading.get_ident()
            previous = fake.thread_contexts.get(thread, _UNAWARE)
            fake.thread_contexts[thread] = _context_value(context)
            return previous

        def get_thread_dpi_awareness_context():
            return fake.thread_contexts.get(threading.get_ident(), _UNAWARE)

        def get_awareness_from_dpi_awareness_context(_context):
            return 0  # DPI_AWARENESS_UNAWARE

        def enum_display_monitors(_hdc, _rect, callback, _lparam):
            fake.enumerations += 1
            if fake.park is not None:
                fake.parked.set()
                fake.park.wait(fake.park_timeout_s)
            if fake.enumeration_error is not None:
                raise fake.enumeration_error
            for index in range(len(fake.monitors)):
                callback(0x10001 + index, None, None, 0)
            return True

        def get_monitor_info_w(hmonitor, info):
            monitor = fake.monitors[int(hmonitor) - 0x10001]
            info.rcMonitor.left = fake._scaled(monitor.left)
            info.rcMonitor.top = fake._scaled(monitor.top)
            info.rcMonitor.right = fake._scaled(monitor.left + monitor.width)
            info.rcMonitor.bottom = fake._scaled(monitor.top + monitor.height)
            return True

        return SimpleNamespace(
            SetThreadDpiAwarenessContext=set_thread_dpi_awareness_context,
            GetThreadDpiAwarenessContext=get_thread_dpi_awareness_context,
            GetAwarenessFromDpiAwarenessContext=(
                get_awareness_from_dpi_awareness_context
            ),
            EnumDisplayMonitors=enum_display_monitors,
            GetMonitorInfoW=get_monitor_info_w,
        )

    def fake_shcore(self):
        fake = self

        def get_dpi_for_monitor(hmonitor, _kind, dpi_x, dpi_y):
            monitor = fake.monitors[int(hmonitor) - 0x10001]
            dpi_x.value = monitor.dpi
            dpi_y.value = monitor.dpi
            return 0

        return SimpleNamespace(GetDpiForMonitor=get_dpi_for_monitor)

    def install(self, stack: ExitStack) -> None:
        import pywintypes
        import win32gui

        windll = SimpleNamespace(user32=self.fake_user32(), shcore=self.fake_shcore())
        stack.enter_context(patch.object(
            monitor_geometry, "ctypes", _CtypesStandIn(windll),
        ))

        def get_window_rect(_hwnd):
            if self.window_rect is None:
                raise pywintypes.error(1400, "GetWindowRect", "Invalid window handle.")
            return tuple(self._scaled(v) for v in self.window_rect)

        stack.enter_context(patch.object(win32gui, "GetWindowRect", get_window_rect))


# DPI_AWARENESS_CONTEXT values (winuser.h): the unaware context handle that
# a new thread of a DPI-unaware process has, and the per-monitor v2
# pseudo-handle.
_UNAWARE = 0x11
_PER_MONITOR_AWARE_V2 = -4


def _context_value(context) -> int:
    """The signed value of a DPI_AWARENESS_CONTEXT given as int or c_void_p."""
    value = getattr(context, "value", context)
    if value is None:
        return 0
    value = int(value)
    if value >= 1 << 63:
        value -= 1 << 64
    return value


class _CtypesStandIn:
    """The ``ctypes`` module as shared/monitor_geometry.py sees it in a test."""

    def __init__(self, windll) -> None:
        self.windll = windll

    @staticmethod
    def byref(obj, _offset=0):
        return obj

    def __getattr__(self, name: str):
        return getattr(_real_ctypes, name)


# ---------------------------------------------------------------------------
# Replies of the Input process
# ---------------------------------------------------------------------------


def mouse_ok(frame) -> dict:
    """A successful pointer action reply, through ``MouseActionResponse``."""

    from shared.mouse_action import MouseActionResponse

    return MouseActionResponse(
        status="ok", outcome="ok", reason=None,
        trace_id=frame.params.get("trace_id", ""),
    ).to_dict()


def mouse_failed(frame, reason: str) -> dict:
    """A failed pointer action reply, through ``MouseActionResponse``."""

    from shared.mouse_action import MouseActionResponse

    return MouseActionResponse(
        status="error", outcome="execution_failed", reason=reason,
        trace_id=frame.params.get("trace_id", ""),
    ).to_dict()


def probe_reply(frame, last_mouse_action: Optional[dict] = None) -> dict:
    """A channel_probe reply, as ``UIActionHandler.channel_probe`` builds it."""

    return {
        "status": "ok",
        "trace_id": frame.params.get("trace_id", ""),
        "last_mouse_action": last_mouse_action,
        "action": "channel_probe",
    }


def last_action_record(action: str, *, trace_id: str, succeeded: bool,
                       reason: Optional[str] = None) -> dict:
    """The record of the last pointer action that channel_probe carries."""

    return {
        "action": action,
        "succeeded": succeeded,
        "reason": None if succeeded else reason,
        "trace_id": trace_id,
    }


# ---------------------------------------------------------------------------
# Typed words
# ---------------------------------------------------------------------------


async def _answer_typing(b) -> None:
    seen = set()
    while True:
        for frame in list(b.frames):
            if frame.action in TYPED_ACTIONS and frame.index not in seen:
                seen.add(frame.index)
                if frame.request_id is not None:
                    b.reply(frame, {"status": "ok"})
        await asyncio.sleep(0.005)


def typed(b, mark) -> list:
    """The words typed since ``mark``."""

    return [
        f.params.get("insertion_string")
        for f in b.frames_since(mark, TYPED_ACTIONS)
    ]


def pointer_frames(b, mark) -> list:
    return b.frames_since(mark, POINTER_INPUT_ACTIONS)


def grid_gui(b, mark) -> list:
    return b.gui_since(mark, GRID_GUI_ACTIONS)


def notices(b, mark) -> list:
    """The reasons of the click notices sent to the GUI since ``mark``."""

    return [
        m.get("reason") for m in b.gui_since(mark, {"show_click_notice"})
    ]


async def quiet(b, mark, window_s: float = 0.3) -> tuple:
    """The grid outputs from ``mark`` to ``window_s`` from now."""

    await b.quiet(mark, window_s)
    return pointer_frames(b, mark), grid_gui(b, mark)


@asynccontextmanager
async def mouse_grid_boundary(
    tmp_path: Path, *, monitors: Optional[FakeMonitors] = None,
    config_path: Optional[Path] = None,
):
    """One real Logic process with fake monitors; yields (boundary, monitors).

    Typed words are answered for the whole test.
    """

    fake = monitors if monitors is not None else FakeMonitors()
    with ExitStack() as stack:
        fake.install(stack)
        async with overlay_boundary(tmp_path, config_path=config_path) as b:
            task = asyncio.ensure_future(_answer_typing(b))
            try:
                yield b, fake
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
