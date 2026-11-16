"""Operating system seam fake for OverlayPaintWindowManager (areas P4, P5, G2).

Bead: wh-full-refactor.7.1 (stage A of place 4). The tests in
tests/test_overlay_gui_requirements_p4.py and
tests/test_overlay_gui_requirements_p5.py drive the real
``OverlayPaintWindowManager`` (overlay_paint_window.py) only through its
public methods, and they read only what leaves it: the return values and the
calls on the operating system seam. The G2 tests use this same fake beneath
the real managers that ``GuiManager`` builds.

What this file replaces, and nothing else:

  * THE WIN32 WINDOW CALLS. overlay_paint_window.py reaches user32, kernel32
    and gdi32 through its module attribute ``ctypes`` (read in the constructor
    of the manager and, for DefWindowProcW, in the window procedure).
    ``PaintSeam.install`` replaces that attribute with a stand-in whose
    ``windll`` holds fake libraries. Every other ``ctypes`` member is the real
    one. The fake user32 records CreateWindowExW with its geometry,
    ShowWindow, DestroyWindow (with a failure switch for each window or for
    every call, and a switch that makes the call raise), GetDC / ReleaseDC and
    RegisterClassExW. No real window is created, so no window appears on the
    screen and no window takes the keyboard focus.
  * THE COMPOSITE. ``overlay_paint_window.composite_layered_window`` (the
    public wrapper of UpdateLayeredWindow) is replaced by a recorder. It keeps
    the window handle, the destination origin, the size of the DIB and a copy
    of the DIB pixels, read through the public ``LayeredDib.read_pixels``.
    The DIB itself is real: ``build_layered_dib`` makes a GDI memory bitmap,
    which shows nothing on the screen.
  * THE MONITORS. overlay_paint_window.py calls ``_enumerate_native_monitors``
    of the module ``shared.monitor_geometry`` (the top-level name; under
    pytest it is a different module object from
    ``services.wheelhouse.shared.monitor_geometry``, which is the one that
    ``FakeMonitors.install`` of tests/mouse_grid_boundary_harness.py
    patches). ``PaintSeam.install`` therefore puts the fake user32 and shcore
    of a ``FakeMonitors`` on the ``ctypes`` name of ``shared.monitor_geometry``
    itself. ``Monitor`` and ``FakeMonitors`` are reused from that harness.

No private member of a production class and no private function of a
production module is named or patched. The fields of the fake classes here
are the fake's own.

Use:

    with paint_seam(FakeMonitors(monitors=[PRIMARY])) as seam:
        manager = seam.manager()
        ...
        assert seam.live_windows() == []

A G2 test calls ``seam.install(stack)`` on its own ExitStack before
``GuiManager()`` runs, so every manager that GuiManager builds uses this fake.
"""

from __future__ import annotations

import ctypes as real_ctypes
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Iterable, Optional
from unittest.mock import patch

import overlay_paint_window
import shared.monitor_geometry as paint_monitor_geometry
from services.wheelhouse.ui.element_types import (
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)
from tests.mouse_grid_boundary_harness import (  # noqa: F401 - re-exported
    PRIMARY,
    SECOND,
    FakeMonitors,
    Monitor,
)

# The first window handle the fake gives out. Each later window gets the next
# number, so a handle is never given out twice in one test.
FIRST_HWND = 0x5000
# What the fake GetDC returns for the screen.
SCREEN_DC = 0xDC0
# ERROR_CLASS_ALREADY_EXISTS: what Windows reports when a second manager
# registers the same window class.
ERROR_CLASS_ALREADY_EXISTS = 1410
# ERROR_ACCESS_DENIED: the error the fake reports for a failed DestroyWindow.
ERROR_ACCESS_DENIED = 5


def handle_value(handle) -> int:
    """The integer value of a handle given as int, None or a wintypes handle."""
    value = getattr(handle, "value", handle)
    return int(value) if value else 0


@dataclass
class FakeWindow:
    """One window that the fake CreateWindowExW made."""

    hwnd: int
    left: int
    top: int
    width: int
    height: int
    ex_style: int
    style: int
    class_name: str
    shown: list = field(default_factory=list)
    destroyed: bool = False

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) in screen physical pixels."""
        return (self.left, self.top, self.width, self.height)


@dataclass
class Composite:
    """One call of composite_layered_window."""

    hwnd: int
    dest_x: int
    dest_y: int
    width: int
    height: int
    pixels: Optional[bytes]
    succeeded: bool

    def contains(self, x: int, y: int) -> bool:
        """True when the screen point (x, y) lies on this surface."""
        return (
            self.dest_x <= x < self.dest_x + self.width
            and self.dest_y <= y < self.dest_y + self.height
        )

    def alpha(self, x: int, y: int) -> int:
        """The alpha (0 to 255) of the surface at the SCREEN point (x, y).

        A point off the surface is transparent (0): nothing is drawn there.
        """
        if self.pixels is None:
            raise AssertionError("the seam did not copy the pixels")
        if not self.contains(x, y):
            return 0
        local_x = x - self.dest_x
        local_y = y - self.dest_y
        return self.pixels[(local_y * self.width + local_x) * 4 + 3]

    def ink(self, left: int, top: int, right: int, bottom: int,
            threshold: int = 128) -> set:
        """The screen points in [left, right) x [top, bottom) whose alpha is
        at least ``threshold``."""
        return {
            (x, y)
            for y in range(top, bottom)
            for x in range(left, right)
            if self.alpha(x, y) >= threshold
        }


class PaintSeam:
    """The fake of the operating system seam of OverlayPaintWindowManager.

    Switches a test sets:
      ``destroy_fails``: window handles whose DestroyWindow returns 0.
      ``destroy_fails_all``: every DestroyWindow returns 0.
      ``destroy_raises``: every DestroyWindow raises OSError.
      ``create_fails``: CreateWindowExW returns 0 (no window).
      ``composite_fails``: every composite returns False.
      ``composite_fails_for``: window handles whose composite returns False.
      ``copy_pixels``: copy the DIB pixels into each Composite record.
    """

    def __init__(self, monitors: Optional[FakeMonitors] = None) -> None:
        self.monitors = (
            monitors if monitors is not None else FakeMonitors(monitors=[PRIMARY])
        )
        self.windows: list[FakeWindow] = []
        self.destroy_calls: list[int] = []
        self.composites: list[Composite] = []
        self.class_registrations = 0
        self.screen_dcs_open = 0
        self.last_error = 0
        self.next_hwnd = FIRST_HWND
        self.destroy_fails: set = set()
        self.destroy_fails_all = False
        self.destroy_raises = False
        self.create_fails = False
        self.composite_fails = False
        self.composite_fails_for: set = set()
        self.copy_pixels = True
        self.user32 = self.fake_user32()
        self.kernel32 = self.fake_kernel32()
        self.gdi32 = SimpleNamespace()

    # -- reading what left the manager -----------------------------------

    def live_windows(self) -> list[FakeWindow]:
        """The windows created and not yet destroyed (still on the screen)."""
        return [w for w in self.windows if not w.destroyed]

    def window(self, hwnd: int) -> FakeWindow:
        for w in self.windows:
            if w.hwnd == hwnd:
                return w
        raise KeyError(hwnd)

    def windows_since(self, mark: int) -> list[FakeWindow]:
        """The windows created after ``mark`` (a length of ``windows``)."""
        return self.windows[mark:]

    def destroys_since(self, mark: int) -> list[int]:
        """The DestroyWindow calls after ``mark`` (a length of
        ``destroy_calls``), as window handles."""
        return self.destroy_calls[mark:]

    def composites_since(self, mark: int) -> list[Composite]:
        return self.composites[mark:]

    def last_composite_for(self, hwnd: int) -> Composite:
        for record in reversed(self.composites):
            if record.hwnd == hwnd:
                return record
        raise KeyError(hwnd)

    # -- the fake libraries ------------------------------------------------

    def fake_user32(self) -> SimpleNamespace:
        seam = self

        def create_window_ex_w(ex_style, class_name, _window_name, style,
                               x, y, width, height, _parent, _menu,
                               _instance, _param):
            if seam.create_fails:
                seam.last_error = ERROR_ACCESS_DENIED
                return 0
            hwnd = seam.next_hwnd
            seam.next_hwnd += 1
            seam.windows.append(FakeWindow(
                hwnd=hwnd, left=int(x), top=int(y), width=int(width),
                height=int(height), ex_style=int(ex_style), style=int(style),
                class_name=str(class_name),
            ))
            return hwnd

        def show_window(hwnd, command):
            seam.window(handle_value(hwnd)).shown.append(int(command))
            return 0

        def destroy_window(hwnd):
            value = handle_value(hwnd)
            seam.destroy_calls.append(value)
            if seam.destroy_raises:
                raise OSError("DestroyWindow failed in the fake")
            if seam.destroy_fails_all or value in seam.destroy_fails:
                seam.last_error = ERROR_ACCESS_DENIED
                return 0
            window = seam.window(value)
            if window.destroyed:
                seam.last_error = 1400  # ERROR_INVALID_WINDOW_HANDLE
                return 0
            window.destroyed = True
            return 1

        def get_dc(_hwnd):
            seam.screen_dcs_open += 1
            return SCREEN_DC

        def release_dc(_hwnd, _dc):
            seam.screen_dcs_open -= 1
            return 1

        def register_class_ex_w(_wnd_class):
            seam.class_registrations += 1
            if seam.class_registrations > 1:
                seam.last_error = ERROR_CLASS_ALREADY_EXISTS
                return 0
            return 0xC001

        def def_window_proc_w(_hwnd, _msg, _wparam, _lparam):
            return 0

        return SimpleNamespace(
            CreateWindowExW=create_window_ex_w,
            ShowWindow=show_window,
            DestroyWindow=destroy_window,
            GetDC=get_dc,
            ReleaseDC=release_dc,
            RegisterClassExW=register_class_ex_w,
            DefWindowProcW=def_window_proc_w,
        )

    def fake_kernel32(self) -> SimpleNamespace:
        seam = self

        def get_module_handle_w(_name):
            return 0x400000

        def get_last_error():
            return seam.last_error

        return SimpleNamespace(
            GetModuleHandleW=get_module_handle_w,
            GetLastError=get_last_error,
        )

    def fake_composite(self, hwnd, screen_dc, dib, dest_x, dest_y) -> bool:
        """Stands in for overlay_paint_window.composite_layered_window."""
        value = handle_value(hwnd)
        if screen_dc != SCREEN_DC:
            raise AssertionError(f"composite got DC {screen_dc!r}, not the screen DC")
        ok = not (self.composite_fails or value in self.composite_fails_for)
        self.composites.append(Composite(
            hwnd=value, dest_x=int(dest_x), dest_y=int(dest_y),
            width=int(dib.width), height=int(dib.height),
            pixels=dib.read_pixels() if self.copy_pixels else None,
            succeeded=ok,
        ))
        return ok

    # -- installing ---------------------------------------------------------

    def install(self, stack: ExitStack) -> None:
        """Install the fake on ``stack``. Call before any manager is built."""
        paint_windll = SimpleNamespace(
            user32=self.user32, kernel32=self.kernel32, gdi32=self.gdi32,
        )
        stack.enter_context(patch.object(
            overlay_paint_window, "ctypes", CtypesStandIn(paint_windll),
        ))
        stack.enter_context(patch.object(
            overlay_paint_window, "composite_layered_window", self.fake_composite,
        ))
        monitor_windll = SimpleNamespace(
            user32=self.monitors.fake_user32(),
            shcore=self.monitors.fake_shcore(),
        )
        stack.enter_context(patch.object(
            paint_monitor_geometry, "ctypes",
            CtypesStandIn(monitor_windll, byref_returns_object=True),
        ))

    def manager(self, **kwargs) -> "overlay_paint_window.OverlayPaintWindowManager":
        """A real OverlayPaintWindowManager built above this fake."""
        return overlay_paint_window.OverlayPaintWindowManager(**kwargs)


class CtypesStandIn:
    """The ``ctypes`` module as the patched module sees it in a test.

    ``windll`` is the fake. With ``byref_returns_object`` the stand-in hands
    the structure itself to the fake call (so the fake monitor calls can fill
    it), as the stand-in of tests/mouse_grid_boundary_harness.py does. Every
    other name is the real ``ctypes`` member.
    """

    def __init__(self, windll, *, byref_returns_object: bool = False) -> None:
        self.windll = windll
        self.byref_returns_object = byref_returns_object

    def byref(self, obj, offset=0):
        if self.byref_returns_object:
            return obj
        return real_ctypes.byref(obj, offset)

    def __getattr__(self, name: str):
        return getattr(real_ctypes, name)


@contextmanager
def paint_seam(monitors: Optional[FakeMonitors] = None):
    """Install a PaintSeam for the duration of the block; yields it."""
    seam = PaintSeam(monitors)
    with ExitStack() as stack:
        seam.install(stack)
        yield seam


# ---------------------------------------------------------------------------
# Inputs: the summary that GuiManager parses out of a paint_overlay message.
# ---------------------------------------------------------------------------


def badge_summary(
    bounds: Iterable[tuple[int, int, int, int]],
    snapshot_id: str = "snap-1",
) -> WalkSnapshotSummary:
    """A WalkSnapshotSummary with one named button per (x, y, width, height)
    box, numbered from 1, as the paint_overlay message carries it."""
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"item-{number}",
            display_number=number,
            name=f"Button {number}",
            role="Button",
            bounds=tuple(box),
            monitor_id=0,
        )
        for number, box in enumerate(bounds, start=1)
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=0.0,
    )
