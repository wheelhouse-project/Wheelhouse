"""Unit tests for the numbered-overlay paint window
(``overlay_paint_window.py``, slice wh-n29v.53, source leaf wh-h7cvz1).

The module manages one transparent, always-on-top, no-activate,
click-through layered Win32 window PER monitor that currently has badges
and paints centered outlined numerals onto on-screen controls via the
existing Qt-to-GDI per-pixel-alpha bridge. These tests mock ctypes (the
Win32 window lifecycle), the bitmap bridge (``build_layered_dib`` /
``composite_layered_window``), the DPI converter
(``resolve_overlay_paint_rect``), the native-monitor enumeration, and the
QScreen list, so no real on-screen window or rendering is required.

The real on-screen rendering / click-through is validated only by the
separate human-only leaf wh-w7oleq, which this slice blocks.

Test groups mirror the TDD bundle steps:
1. Generation gate (high-water mark).
2. Converter wiring (enumerate once; placement by hmonitor).
3. Per-monitor window lifecycle (ex-style flags, teardown-before-create,
   window proc).
4. Badge render + composite.
5. paint_overlay end-to-end + emit.
6. clear_overlay + emit.
7. GUI routing (in test_gui_overlay_routing, separate file region).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

from PySide6.QtGui import QImage

from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem
from shared.overlay_dpi_resolver import OverlayPaintRect
from shared.monitor_geometry import _NativeMonitor
from PySide6.QtCore import QRect


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_item(
    display_number: int,
    bounds: tuple[int, int, int, int] = (10, 20, 110, 60),
    monitor_id: int = 0,
) -> WalkSnapshotSummaryItem:
    return WalkSnapshotSummaryItem(
        item_id=f"item-{display_number}",
        display_number=display_number,
        name=f"control {display_number}",
        role="Button",
        bounds=bounds,
        monitor_id=monitor_id,
    )


def _make_summary(
    items: list[WalkSnapshotSummaryItem],
    snapshot_id: str = "snap-1",
) -> WalkSnapshotSummary:
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=items,
        created_at_monotonic=123.0,
    )


def _native_monitor(hmonitor: int, left: int = 0, dpi: int = 96) -> _NativeMonitor:
    return _NativeMonitor(
        hmonitor=hmonitor,
        rect_phys=QRect(left, 0, 1920, 1080),
        dpi=dpi,
    )


def _paint_rect(
    hmonitor: int,
    monitor: _NativeMonitor,
    x: int = 5,
    y: int = 5,
    width: int = 100,
    height: int = 40,
) -> OverlayPaintRect:
    return OverlayPaintRect(
        x=x,
        y=y,
        width=width,
        height=height,
        monitor=monitor,
        hmonitor=hmonitor,
        screen=None,
    )


# ``_render_monitor_surface`` returns just the QImage after the bounding-box
# refactor (the bbox offset/size is computed by ``_compute_monitor_bbox`` and
# owned by the caller). Tests that only need the surface to be a non-None
# sentinel patch it with this stub; the bbox geometry still flows through the
# real ``_compute_monitor_bbox`` on the patched-out rects.
def _surface_stub(*_args, **_kwargs):
    return object()


@pytest.fixture
def mock_win_apis():
    """Mock Windows DLLs for the overlay paint window."""
    user32 = MagicMock()
    gdi32 = MagicMock()
    kernel32 = MagicMock()

    user32.DefWindowProcW.return_value = 0
    user32.RegisterClassExW.return_value = 1
    user32.CreateWindowExW.return_value = wintypes.HWND(0xABCD)
    user32.DestroyWindow.return_value = True
    user32.ShowWindow.return_value = True
    user32.GetDC.return_value = wintypes.HDC(0x1111)
    user32.ReleaseDC.return_value = 1
    user32.IsWindow.return_value = True
    kernel32.GetModuleHandleW.return_value = wintypes.HMODULE(1)
    kernel32.GetLastError.return_value = 0
    return user32, gdi32, kernel32


@pytest.fixture
def overlay_mgr(mock_win_apis, qapp):
    """Construct an OverlayPaintWindowManager with mocked ctypes.

    Passes real ctypes through for POINTER / byref / sizeof / WINFUNCTYPE /
    c_ssize_t so the WNDCLASS registration and window-proc plumbing build
    correctly while the DLL calls are intercepted.
    """
    user32, gdi32, kernel32 = mock_win_apis
    with patch("overlay_paint_window.ctypes") as mock_ctypes:
        mock_ctypes.windll.user32 = user32
        mock_ctypes.windll.gdi32 = gdi32
        mock_ctypes.windll.kernel32 = kernel32
        mock_ctypes.POINTER = ctypes.POINTER
        mock_ctypes.byref = ctypes.byref
        mock_ctypes.sizeof = ctypes.sizeof
        mock_ctypes.WINFUNCTYPE = ctypes.WINFUNCTYPE
        mock_ctypes.WinError = ctypes.WinError
        mock_ctypes.c_ssize_t = ctypes.c_ssize_t
        mock_ctypes.c_int = ctypes.c_int
        mock_ctypes.Structure = ctypes.Structure

        import overlay_paint_window

        mgr = overlay_paint_window.OverlayPaintWindowManager()
        yield mgr, overlay_paint_window, (user32, gdi32, kernel32)
        mgr.clear_all()


# ===========================================================================
# Step 1: generation gate (high-water mark)
# ===========================================================================


class TestGenerationGate:
    """High-water mark keyed on (overlay_session_id, paint_generation)."""

    def test_stale_paint_is_noop(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        gate = mod.GenerationGate()
        # Advance the mark to (5, 3).
        assert gate.accept_paint(5, 3) is True
        # A strictly-older paint at the same session is ignored.
        assert gate.accept_paint(5, 2) is False
        # An older session is ignored.
        assert gate.accept_paint(4, 99) is False

    def test_paint_at_or_above_mark_advances(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        gate = mod.GenerationGate()
        assert gate.accept_paint(1, 0) is True
        # Equal pair is accepted (>=) and keeps the mark.
        assert gate.accept_paint(1, 0) is True
        # Higher generation advances.
        assert gate.accept_paint(1, 1) is True
        # Higher session advances even with lower generation.
        assert gate.accept_paint(2, 0) is True
        # Now a generation-0 paint at session 1 is stale.
        assert gate.accept_paint(1, 5) is False

    def test_clear_advances_mark_and_blocks_stale_paint(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        gate = mod.GenerationGate()
        assert gate.accept_paint(3, 1) is True
        # A clear at the same pair advances the mark (>=).
        assert gate.accept_clear(3, 1) is True
        # A stale paint at the prior generation cannot present.
        assert gate.accept_paint(3, 1) is False
        assert gate.accept_paint(3, 0) is False
        # A newer paint still presents.
        assert gate.accept_paint(3, 2) is True

    def test_newer_clear_blocks_prior_gen_paint(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        gate = mod.GenerationGate()
        assert gate.accept_paint(7, 4) is True
        assert gate.accept_clear(7, 5) is True
        # The prior-generation paint is now stale.
        assert gate.accept_paint(7, 4) is False


# ===========================================================================
# Step 2: converter wiring
# ===========================================================================


class TestConverterWiring:
    """Enumerate native monitors + QScreens ONCE per render and pass both to
    every resolve call; placement by rect.hmonitor; None / no-window skip."""

    def test_enumerate_once_and_pass_both_to_every_resolve(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(1001)
        screens_sentinel = [object(), object()]
        monitors_sentinel = [mon]

        with patch.object(
            mod, "_enumerate_native_monitors", return_value=monitors_sentinel
        ) as enum_mock, patch.object(
            mod, "_screens", return_value=screens_sentinel
        ) as screens_mock, patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib"
        ) as build_mock, patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.return_value = _paint_rect(1001, mon)
            build_mock.return_value = MagicMock()

            items = [_make_item(1), _make_item(2), _make_item(3)]
            mgr.paint(_make_summary(items), overlay_session_id=1, paint_generation=0)

            # Enumerated exactly once, screens read exactly once.
            assert enum_mock.call_count == 1
            assert screens_mock.call_count == 1
            # resolve called once per item, always with both seams.
            assert resolve_mock.call_count == 3
            for c in resolve_mock.call_args_list:
                assert c.kwargs.get("monitors") is monitors_sentinel
                assert c.kwargs.get("screens") is screens_sentinel

    def test_item_with_none_resolve_is_skipped(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(2002)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ) as composite_mock:
            # First item resolves; second returns None (skipped).
            resolve_mock.side_effect = [_paint_rect(2002, mon), None]
            items = [_make_item(1), _make_item(2)]
            result = mgr.paint(
                _make_summary(items), overlay_session_id=1, paint_generation=0
            )
            # Only one badge composited (the skipped item produced none).
            assert composite_mock.call_count == 1
            assert result["state"] == "painted"

    def test_badge_with_no_window_for_hmonitor_is_skipped(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mon_a = _native_monitor(3003, left=0)
        mon_b = _native_monitor(4004, left=1920)
        # Only mon_a is enumerated, so a rect resolving to mon_b's hmonitor
        # has no overlay window and must be skipped rather than painted on
        # the wrong monitor.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ) as composite_mock:
            # Item 1 -> mon_a (has a window). Item 2 -> hmonitor 4004 (no window).
            resolve_mock.side_effect = [
                _paint_rect(3003, mon_a),
                _paint_rect(4004, mon_b),
            ]
            items = [_make_item(1), _make_item(2)]
            result = mgr.paint(
                _make_summary(items), overlay_session_id=1, paint_generation=0
            )
            assert composite_mock.call_count == 1
            # Only mon_a's hmonitor painted.
            assert result["monitor_ids"] == [3003]


# ===========================================================================
# Step 3: per-monitor window lifecycle
# ===========================================================================

# Ex-style flags the spec mandates.
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_EXPECTED_EX = (
    _WS_EX_LAYERED
    | _WS_EX_TOPMOST
    | _WS_EX_TRANSPARENT
    | _WS_EX_NOACTIVATE
    | _WS_EX_TOOLWINDOW
)
_WS_POPUP = 0x80000000
_WM_NCHITTEST = 0x0084
_WM_MOUSEACTIVATE = 0x0021
_HTTRANSPARENT = -1
_MA_NOACTIVATE = 3


class TestWindowLifecycle:
    def _paint_two_monitors(self, mgr, mod):
        mon_a = _native_monitor(10, left=0)
        mon_b = _native_monitor(20, left=1920)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a, mon_b]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.side_effect = [
                _paint_rect(10, mon_a),
                _paint_rect(20, mon_b),
            ]
            items = [_make_item(1), _make_item(2)]
            mgr.paint(
                _make_summary(items), overlay_session_id=1, paint_generation=0
            )

    def test_one_create_per_monitor_with_badges(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        self._paint_two_monitors(mgr, mod)
        # Two windows -> two CreateWindowExW calls.
        assert user32.CreateWindowExW.call_count == 2

    def test_create_uses_exact_exstyle_and_bbox_geometry(self, overlay_mgr):
        """The per-monitor window is sized to the badge BOUNDING BOX (plus
        margin) at the bounding box's SCREEN origin -- NOT the full monitor
        physical resolution. Bounding-box surface refactor (wh-n29v.56.1)."""
        mgr, mod, (user32, _, _) = overlay_mgr
        # Pin inside placement: this test checks the window bbox geometry, not
        # the trailing-space placement (covered by
        # TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        # Monitor at screen origin (300, 50), 800x600 physical, dpr 1.0.
        mon = _NativeMonitor(hmonitor=10, rect_phys=QRect(300, 50, 800, 600), dpi=96)
        # One badge at monitor-local logical (5, 5) size 100x40 (dpr 1.0 ->
        # physical identical). With a 5px outline/shadow margin the local
        # physical bbox is (0, 0)..(110, 50) -> 110x50, screen origin
        # (300+0, 50+0).
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        args = user32.CreateWindowExW.call_args[0]
        # CreateWindowExW(dwExStyle, class, window, dwStyle, x, y, w, h, ...)
        assert args[0] == _EXPECTED_EX
        assert args[3] == _WS_POPUP
        assert args[4] == 300  # x = bbox screen origin = rect_phys.left()+0
        assert args[5] == 50   # y = bbox screen origin = rect_phys.top()+0
        assert args[6] == 110  # bbox width (100 + 2*5 margin)
        assert args[7] == 50   # bbox height (40 + 2*5 margin)
        # NOT the full monitor resolution.
        assert args[6] != 800
        assert args[7] != 600

    def test_absent_monitor_windows_destroyed_before_new_created(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        # First render: monitors 10 and 20.
        self._paint_two_monitors(mgr, mod)
        user32.reset_mock()
        # Track call ordering across DestroyWindow and CreateWindowExW.
        order: list[str] = []
        user32.DestroyWindow.side_effect = lambda *a, **k: order.append("destroy") or True
        user32.CreateWindowExW.side_effect = (
            lambda *a, **k: order.append("create") or wintypes.HWND(0x2222)
        )
        # Second render: only monitor 30 (10 and 20 are now absent).
        mon_c = _native_monitor(30, left=3840)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_c]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(30, mon_c)
        ), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=2,
                paint_generation=0,
            )
        # Both old windows destroyed.
        assert user32.DestroyWindow.call_count == 2
        # All destroys happen before the first create.
        first_create = order.index("create")
        assert all(
            evt == "destroy" for evt in order[:first_create]
        )

    def test_window_proc_returns_transparent_and_noactivate(self, overlay_mgr):
        # wh-overlay-shared-wndproc: the proc is MODULE-scope so both manager
        # instances (numbered overlay + working badge) share one
        # process-lifetime callback. Calling it under the fixture's module
        # ctypes patch also proves DefWindowProcW is resolved at CALL time
        # (an import-time capture would bypass the patch).
        mgr, mod, (user32, _, _) = overlay_mgr
        proc = mod._wnd_proc_py
        # WM_NCHITTEST -> HTTRANSPARENT
        assert proc(wintypes.HWND(1), _WM_NCHITTEST, 0, 0) == _HTTRANSPARENT
        # WM_MOUSEACTIVATE -> MA_NOACTIVATE
        assert proc(wintypes.HWND(1), _WM_MOUSEACTIVATE, 0, 0) == _MA_NOACTIVATE
        # Anything else delegates to DefWindowProcW.
        user32.DefWindowProcW.return_value = 4242
        assert proc(wintypes.HWND(1), 0x0001, 0, 0) == 4242
        user32.DefWindowProcW.assert_called()

    def test_wndproc_instance_retained(self, overlay_mgr):
        # wh-overlay-shared-wndproc: the WINFUNCTYPE thunk is retained at
        # MODULE scope for the process lifetime (the registered window class
        # points at it), so no manager GC can ever free it. The old
        # manager-bound instance is gone.
        mgr, mod, _ = overlay_mgr
        assert mod._PROCESS_WND_PROC is not None
        assert not hasattr(mgr, "_wnd_proc_instance")

    def test_class_registers_module_scope_wndproc(self, overlay_mgr):
        # wh-overlay-shared-wndproc: whichever manager registers the class,
        # the class's lpfnWndProc must be the module-scope thunk -- not a
        # manager-bound one whose lifetime is tied to the first manager.
        mgr, mod, (user32, _, _) = overlay_mgr
        mgr._ensure_class_registered()
        registered = user32.RegisterClassExW.call_args[0][0]._obj
        assert ctypes.cast(
            registered.lpfnWndProc, ctypes.c_void_p
        ).value == ctypes.cast(
            mod._PROCESS_WND_PROC, ctypes.c_void_p
        ).value

    def test_wndproc_survives_first_manager_gc(self, overlay_mgr):
        # wh-overlay-shared-wndproc: the hazard being closed -- the class
        # callback must not be anchored to any manager instance. Register the
        # class from a second manager, destroy that manager, and the
        # registered pointer must still be alive at module scope.
        import gc

        mgr, mod, (user32, _, _) = overlay_mgr
        mgr2 = mod.OverlayPaintWindowManager()
        mgr2._ensure_class_registered()
        registered_ptr = ctypes.cast(
            user32.RegisterClassExW.call_args[0][0]._obj.lpfnWndProc,
            ctypes.c_void_p,
        ).value
        del mgr2
        gc.collect()
        assert mod._PROCESS_WND_PROC is not None
        assert ctypes.cast(
            mod._PROCESS_WND_PROC, ctypes.c_void_p
        ).value == registered_ptr

    def test_two_managers_share_process_wndproc(self, overlay_mgr):
        # wh-overlay-shared-wndproc: gui.py constructs TWO managers (numbered
        # click overlay + dictation working badge) registering the same
        # process-global class. Neither owns the callback, and the second
        # registration's ERROR_CLASS_ALREADY_EXISTS path still marks the
        # class usable.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        mgr2 = mod.OverlayPaintWindowManager()
        assert not hasattr(mgr, "_wnd_proc_instance")
        assert not hasattr(mgr2, "_wnd_proc_instance")
        mgr._ensure_class_registered()
        assert mgr._class_registered
        # Second manager: RegisterClassExW fails with
        # ERROR_CLASS_ALREADY_EXISTS (1410) -- benign, class stays usable.
        user32.RegisterClassExW.return_value = 0
        kernel32.GetLastError.return_value = 1410
        mgr2._ensure_class_registered()
        assert mgr2._class_registered


# ===========================================================================
# Step 4: badge render + composite
# ===========================================================================


class TestBadgeRender:
    def test_render_badge_returns_premultiplied_qimage(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        img = mgr._render_badge(7, width=80, height=40)
        assert isinstance(img, QImage)
        assert img.format() == QImage.Format.Format_ARGB32_Premultiplied
        # Some non-transparent pixels were painted (the numeral / outline).
        # Scan for any pixel with non-zero alpha.
        found = False
        for y in range(img.height()):
            for x in range(img.width()):
                if (img.pixel(x, y) >> 24) & 0xFF:
                    found = True
                    break
            if found:
                break
        assert found

    def test_render_badge_uses_font_pt_and_shadow(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # The manager carries font pt and shadow flag from construction.
        assert mgr._badge_font_pt == 16
        assert mgr._badge_shadow is True
        # Constructing with explicit overrides honors them.
        import overlay_paint_window
        m2 = overlay_paint_window.OverlayPaintWindowManager(
            badge_font_pt=24, badge_shadow=False
        )
        assert m2._badge_font_pt == 24
        assert m2._badge_shadow is False

    def test_numeral_badge_anchors_to_control_top_left(self, overlay_mgr):
        # The numeral badge is anchored to the control's TOP-LEFT corner, NOT
        # centered on the control, so the digit covers only a corner and leaves
        # the control's own label/icon visible (wh-overlay-badge-occludes-label).
        # Both the paint path and the surface bounding box call this helper, so
        # they can never disagree on where the badge goes.
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = OverlayPaintRect(
            x=100, y=200, width=300, height=40,
            monitor=monitor, hmonitor=1, screen=None,
        )
        # A 20x30-physical badge over this control: footprint top-left =
        # control top-left * dpr = (200, 400), NOT the control center (500, 440).
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 2.0, corner="top_left"
        )
        assert (left, top) == (200.0, 400.0)
        assert (right, bottom) == (220.0, 430.0)

    def test_numeral_badge_shifts_inward_at_monitor_right_bottom_edge(
        self, overlay_mgr
    ):
        # A control narrower/shorter than the numeral, hard against the monitor's
        # right/bottom edge, would push a top-left-anchored badge past the
        # monitor; the surface clamp would then truncate the digit. When given
        # the monitor's physical bounds, the footprint shifts inward so the whole
        # badge stays on-screen (wh-review-click-overlay-codex.2).
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        assert monitor.dpr == 1.0
        # A 17x10 control 10px from the right edge and 10px from the bottom edge.
        rect = OverlayPaintRect(
            x=1910, y=1070, width=17, height=10,
            monitor=monitor, hmonitor=1, screen=None,
        )
        # A 30x40 badge anchored at (1910, 1070) would reach (1940, 1110), past
        # the 1920x1080 monitor. With bounds it shifts to (1890, 1040).
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, 30, 40, 1.0, 1920, 1080, corner="top_left"
        )
        assert (left, top) == (1890.0, 1040.0)   # 1920-30, 1080-40
        assert (right, bottom) == (1920.0, 1080.0)

    def test_numeral_badge_defaults_to_top_right_corner(self, overlay_mgr):
        # Default placement is the control's TOP-RIGHT corner so the digit clears
        # the icon and the label start on left-aligned list/tree rows (the
        # File Explorer nav pane and Details list; wh-overlay-badge-occludes-label
        # follow-up). A default-constructed manager carries corner "top_right".
        mgr, _mod, _ = overlay_mgr
        assert mgr._badge_corner == "top_right"
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        assert monitor.dpr == 1.0
        rect = OverlayPaintRect(
            x=100, y=200, width=300, height=40,
            monitor=monitor, hmonitor=1, screen=None,
        )
        # top-right: left = (100 + 300) - 20 = 380; top = 200 (dpr 1.0).
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, corner=mgr._badge_corner
        )
        assert (left, top) == (380.0, 200.0)
        assert (right, bottom) == (400.0, 230.0)

    def test_numeral_badge_bottom_corners(self, overlay_mgr):
        # bottom_left / bottom_right anchor to the control's bottom edge.
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        rect = OverlayPaintRect(
            x=100, y=200, width=300, height=40,
            monitor=monitor, hmonitor=1, screen=None,
        )
        # bottom_left: left = 100; top = (200 + 40) - 30 = 210.
        bl = mgr._numeral_badge_footprint_phys(rect, 20, 30, 1.0, corner="bottom_left")
        assert (bl[0], bl[1]) == (100.0, 210.0)
        # bottom_right: left = (100 + 300) - 20 = 380; top = 210.
        br = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, corner="bottom_right"
        )
        assert (br[0], br[1]) == (380.0, 210.0)

    def test_top_right_badge_shifts_inward_at_left_edge(self, overlay_mgr):
        # A top-right anchor on a control wider than the badge normally stays put,
        # but a control whose LEFT edge is at the monitor origin and is narrower
        # than the badge would push a right-anchored badge past the left edge
        # (negative). The inward clamp keeps the whole badge on-screen (>= 0).
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # 10px control flush against the left edge; a 30px right-anchored badge
        # would land at (10 - 30) = -20 without the clamp.
        rect = OverlayPaintRect(
            x=0, y=500, width=10, height=20,
            monitor=monitor, hmonitor=1, screen=None,
        )
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, 30, 20, 1.0, 1920, 1080, corner="top_right"
        )
        assert left == 0.0        # clamped up from -20
        assert right == 30.0

    def test_manager_reads_badge_corner_from_construction(self):
        import overlay_paint_window
        m = overlay_paint_window.OverlayPaintWindowManager(badge_corner="bottom_left")
        assert m._badge_corner == "bottom_left"
        # An unknown corner falls back to the default rather than mis-placing.
        m2 = overlay_paint_window.OverlayPaintWindowManager(badge_corner="nonsense")
        assert m2._badge_corner == "top_right"

    def test_right_edge_badge_shifted_inward_end_to_end(self, overlay_mgr):
        # End-to-end: a small control at the monitor's right edge must have its
        # FULL badge painted inside the surface, not truncated at the surface
        # edge (wh-review-click-overlay-codex.2). The paint site and the bbox
        # site both shift the badge inward using the monitor bounds, so they
        # agree and the clamp never cuts a digit.
        mgr, _mod, _ = overlay_mgr
        # This test pins the TOP-LEFT corner: it verifies the right-edge inward
        # clamp, which is the same for every corner, using top-left arithmetic.
        mgr._badge_corner = "top_left"
        # Pin inside placement: this test checks the corner/edge clamp, not the
        # trailing-space placement (covered by TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        monitor = _NativeMonitor(
            hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # 6px control 8px from the right edge; a 24px badge anchored at its
        # top-left (1912) would reach 1936, past the 1920 edge.
        rect = OverlayPaintRect(
            x=1912, y=500, width=6, height=20,
            monitor=monitor, hmonitor=1, screen=None,
        )
        badges = [(rect, 7)]

        # Pin the numeral size to 24x24 so the bbox math and the opaque spy agree.
        with patch.object(mgr, "_numeral_badge_size", return_value=(24, 24)):
            bbox = mgr._compute_monitor_bbox(monitor, badges)

            def _opaque_badge(_n, _w, _h, _dpr=1.0):
                img = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(0xFFFFFFFF)
                return img

            with patch.object(mgr, "_render_badge", side_effect=_opaque_badge):
                surface = mgr._render_monitor_surface(monitor, badges, bbox)

        xs = [
            x
            for y in range(surface.height())
            for x in range(surface.width())
            if (surface.pixel(x, y) >> 24) & 0xFF
        ]
        assert xs
        # The full 24-px-wide badge is present, not clipped by the surface edge.
        assert max(xs) - min(xs) + 1 == 24
        # Its on-screen right edge sits at the monitor edge (x=1920), fully on.
        assert monitor.rect_phys.left() + bbox.offset_x + max(xs) + 1 == 1920

    def test_render_monitor_surface_is_bbox_sized_at_dpr_1_5(
        self, overlay_mgr
    ):
        """On a dpr != 1.0 monitor the surface is BOUNDING-BOX sized (plus
        margin), NOT the full monitor physical resolution, and the badge
        lands at its bbox-local physical position so on-screen placement is
        preserved. Bounding-box refactor (wh-n29v.56.1); preserves the
        wh-n29v.54.1 DPR scaling contract.
        """
        mgr, mod, _ = overlay_mgr
        # Pins TOP-LEFT: the surface-size/placement contract is corner-agnostic,
        # asserted here with top-left arithmetic.
        mgr._badge_corner = "top_left"
        # Pin inside placement: this test checks surface sizing, not the
        # trailing-space placement (covered by TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        # 1920x1080 physical monitor at 150% scaling -> dpr = 144/96 = 1.5.
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=144
        )
        assert monitor.dpr == 1.5
        rect = OverlayPaintRect(
            x=200,
            y=300,
            width=100,
            height=40,
            monitor=monitor,
            hmonitor=10,
            screen=None,
        )

        # A small fully-opaque badge so the opaque-pixel scan is unambiguous.
        def _opaque_badge(_number, _w, _h, _dpr=1.0):
            badge = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
            badge.fill(0xFFFFFFFF)  # opaque white (ARGB premultiplied)
            return badge

        badges = [(rect, 7)]
        # The bbox is computed on the LOGICAL rect (width 100/height 40), but
        # the opaque PAINT footprint is the 4px-logical badge image scaled by
        # dpr. The bbox bounds the full logical rect: physical footprint
        # left=200*1.5=300, top=300*1.5=450, right=300*1.5=450, bottom=510;
        # margin=5*1.5=7.5; min_x=floor(300-7.5)=292, min_y=floor(450-7.5)=442;
        # max_x=ceil(450+7.5)=458, max_y=ceil(510+7.5)=518.
        bbox = mgr._compute_monitor_bbox(monitor, badges)
        assert bbox.offset_x == 292
        assert bbox.offset_y == 442
        assert bbox.width == 458 - 292  # 166
        assert bbox.height == 518 - 442  # 76

        with patch.object(mgr, "_render_badge", side_effect=_opaque_badge):
            surface = mgr._render_monitor_surface(monitor, badges, bbox)

        assert surface.width() == 166
        assert surface.height() == 76
        # NOT the full monitor resolution.
        assert surface.width() != 1920
        assert surface.height() != 1080

        # Badge lands at the bbox-local physical position of the control's
        # TOP-LEFT corner: 300-292=8, 450-442=8.
        opaque = [
            (x, y)
            for y in range(surface.height())
            for x in range(surface.width())
            if (surface.pixel(x, y) >> 24) & 0xFF
        ]
        assert opaque, "expected the opaque badge to be painted somewhere"
        top_left = min(opaque, key=lambda p: (p[1], p[0]))
        # The numeral is anchored to the control TOP-LEFT (not centered on the
        # control), so the digit covers only a corner and leaves the control's
        # label visible (wh-overlay-badge-occludes-label). Control top-left
        # physical = (200*1.5, 300*1.5) = (300, 450); bbox-local =
        # (300-292, 450-442) = (8, 8).
        assert top_left == (8, 8)
        # On-screen badge TOP-LEFT = composite origin + local = the control's
        # top-left corner.
        assert (monitor.rect_phys.left() + bbox.offset_x + 8) == 300
        assert (monitor.rect_phys.top() + bbox.offset_y + 8) == 450

    def test_composite_called_with_bbox_origin_and_dc_released(self, overlay_mgr):
        """The composite destination origin is the bounding box's SCREEN
        origin (rect_phys top-left + bbox local offset), NOT a fixed monitor
        origin. Here a badge at logical (200, 100) on a dpr-1.0 monitor at
        screen (300, 50) gives a local bbox offset of (195, 95) after the 5px
        margin, so the composite origin is (495, 145). wh-n29v.56.1."""
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _NativeMonitor(hmonitor=10, rect_phys=QRect(300, 50, 800, 600), dpi=96)
        rect = _paint_rect(10, mon, x=200, y=100, width=100, height=40)
        dib = MagicMock()
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=rect
        ), patch.object(
            mgr, "_render_badge", return_value=QImage(2, 2, QImage.Format.Format_ARGB32_Premultiplied)
        ), patch.object(
            mod, "build_layered_dib", return_value=dib
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ) as composite_mock:
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        # composite called with the window hwnd, the screen DC, the dib, and
        # dest origin = bbox SCREEN origin.
        composite_mock.assert_called_once()
        ckwargs = composite_mock.call_args
        passed = list(ckwargs.args) + list(ckwargs.kwargs.values())
        # local bbox offset = (200-5, 100-5) = (195, 95); screen origin =
        # (300+195, 50+95) = (495, 145).
        assert 495 in passed
        assert 145 in passed
        # GetDC(0) acquired and released.
        user32.GetDC.assert_called()
        user32.ReleaseDC.assert_called()


class TestBadgeSharpnessAndSize:
    """On a display scaled above 100%, badges must be rendered at PHYSICAL
    resolution and drawn 1:1, not rendered at logical size and enlarged (which
    smooths every edge). The per-monitor dpr is already known to the painter,
    so no user input is needed. Also: the working hourglass default size must
    be large enough to read (wh-dictation-retraction-indicator.11)."""

    def test_badge_rendered_at_physical_resolution_when_scaled(self, overlay_mgr):
        """At dpr 2.0 each badge image is rendered at the PHYSICAL pixel size
        (logical * dpr) and given the dpr, so its edges are sharp rather than
        an enlarged logical-resolution image."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = _paint_rect(10, monitor, x=400, y=250, width=60, height=24)
        badges = [(rect, 3)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(_number, w, h, dpr=1.0):
            seen.append((w, h, dpr))
            img = QImage(
                max(1, int(w)), max(1, int(h)),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            img.fill(0xFFFFFFFF)
            return img

        with patch.object(mgr, "_render_badge", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        # Tight numeral image at physical resolution (dpr 2.0 forwarded), sized
        # to the glyph -- NOT the 120x48 control physical size -- so a number
        # over a large control no longer allocates a control-sized image
        # (wh-overlay-badge-alloc-decouple).
        assert seen == [(*mgr._numeral_badge_size(3, 2.0), 2.0)]

    def test_numeral_badge_call_is_tight_physical_at_dpr_1(self, overlay_mgr):
        """At dpr 1.0 the numeral badge is rendered into a tight numeral-sized
        image (from _numeral_badge_size) with dpr 1.0 forwarded -- decoupled
        from the control size (wh-overlay-badge-alloc-decouple). The glyph
        itself is the same font at the same point size, so the visible numeral
        is unchanged at 100%."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        assert monitor.dpr == 1.0
        rect = _paint_rect(10, monitor, x=100, y=200, width=80, height=30)
        badges = [(rect, 5)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(_number, w, h, dpr=1.0):
            seen.append((w, h, dpr))
            return QImage(
                max(1, int(w)), max(1, int(h)),
                QImage.Format.Format_ARGB32_Premultiplied,
            )

        with patch.object(mgr, "_render_badge", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert seen == [(*mgr._numeral_badge_size(5, 1.0), 1.0)]

    def test_numeral_not_swallowed_by_outline(self, overlay_mgr):
        """A numeral must render as a legible white glyph, not a near-solid
        black blob.

        The outline is stroked centered on the glyph path edge, so a pen that
        is too wide for the glyph's stroke thickness eats the white fill and
        the numeral reads as a dark blob (perceived as "blurry"). This asserts
        the white fill survives at a representative scaled size
        (wh-dictation-retraction-indicator.11).
        """
        mgr, _mod, _ = overlay_mgr
        # A scaled monitor (dpr 1.5) at a typical control size: 80x40 logical
        # -> 120x60 physical, font 16*1.5 pt. This is where the heavy outline
        # collapsed the glyph to ~0% white fill.
        img = mgr._render_badge(7, 120, 60, dpr=1.5)

        opaque = white = 0
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixel(x, y)
                if ((px >> 24) & 0xFF) < 128:
                    continue
                opaque += 1
                if (
                    ((px >> 16) & 0xFF) > 200
                    and ((px >> 8) & 0xFF) > 200
                    and (px & 0xFF) > 200
                ):
                    white += 1
        ratio = (white / opaque) if opaque else 0.0
        assert opaque > 0, "numeral drew nothing"
        assert ratio >= 0.10, (
            f"numeral collapsed to a near-solid blob (white fill {ratio:.2f} "
            "of opaque pixels) -- the outline pen is too heavy for the glyph"
        )

    def test_working_badge_default_logical_size_is_legible(self, overlay_mgr):
        """The working hourglass default size is large enough to read. The
        first live check at 36 looked like a shrunken blob. The size is now in
        LOGICAL px (scaled by the monitor dpr at paint time)."""
        _mgr, mod, _ = overlay_mgr
        assert mod.WORKING_BADGE_LOGICAL_PX >= 56

    def test_working_glyph_draws_shape_not_box_at_default_size(self, overlay_mgr):
        """At the default size the glyph is a recognizable hourglass: the four
        corners are transparent (not a filled box) and the vertical center line
        (the waist) has opaque pixels."""
        mgr, mod, _ = overlay_mgr
        size = mod.WORKING_BADGE_LOGICAL_PX
        img = mgr._render_working_glyph(size, size)
        assert img.width() == size and img.height() == size
        for cx, cy in [(0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)]:
            assert ((img.pixel(cx, cy) >> 24) & 0xFF) == 0, "corner not transparent"
        midx = size // 2
        waist_opaque = any(
            (img.pixel(midx, y) >> 24) & 0xFF for y in range(size)
        )
        assert waist_opaque, "expected opaque pixels along the glyph waist"


class TestNumeralBadgeSizeDecoupled:
    """wh-overlay-badge-alloc-decouple -- a numeral badge image is sized to the
    NUMERAL (font metrics + decoration margin), NOT to the control. A number
    over a large control no longer allocates a control-sized (dpr^2) transient
    QImage; the numeral is still drawn at the control center, so on-screen
    placement is unchanged. The working glyph (which fills its box) is not
    affected."""

    def test_numeral_image_is_tight_not_control_sized(self, overlay_mgr):
        """For a large control on a hi-DPI monitor, _render_badge is called with
        a small numeral-sized image, NOT the control's physical size (which
        would be 2000x400 = ~3 MB here)."""
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = _paint_rect(10, monitor, x=100, y=100, width=1000, height=200)
        badges = [(rect, 5)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(_number, w, h, dpr=1.0):
            seen.append((w, h, dpr))
            return QImage(
                max(1, int(w)), max(1, int(h)),
                QImage.Format.Format_ARGB32_Premultiplied,
            )

        with patch.object(mgr, "_render_badge", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert len(seen) == 1
        bw, bh, bdpr = seen[0]
        # Still physical-resolution: the dpr is forwarded so the glyph is sharp.
        assert bdpr == 2.0
        # The image is tight (a single bold numeral), far smaller than the
        # 2000x400 control physical size.
        assert bw < 200 and bh < 200, f"badge image not decoupled: {bw}x{bh}"
        # And it matches the dedicated size helper.
        assert (bw, bh) == mgr._numeral_badge_size(5, 2.0)

    def test_numeral_drawn_at_control_top_left(self, overlay_mgr):
        """With the corner set to top_left the numeral lands at the control's
        TOP-LEFT corner (not centered), so the digit covers only a corner and
        leaves the control's own label/icon visible (wh-overlay-badge-occludes-
        label). top_right is the default; this covers the top_left option."""
        mgr, _mod, _ = overlay_mgr
        mgr._badge_corner = "top_left"
        # Pin inside placement: this test checks the top_left corner draw
        # position, not the trailing-space placement (covered by
        # TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        assert monitor.dpr == 1.0
        rect = _paint_rect(10, monitor, x=200, y=100, width=100, height=40)
        badges = [(rect, 7)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        # A 10x10 opaque badge so its top-left is unambiguous (the spy ignores
        # the requested size, which is what lets us check WHERE it is drawn).
        def _opaque_badge(_number, _w, _h, _dpr=1.0):
            img = QImage(10, 10, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0xFFFFFFFF)
            return img

        with patch.object(mgr, "_render_badge", side_effect=_opaque_badge):
            surface = mgr._render_monitor_surface(monitor, badges, bbox)

        opaque = [
            (x, y)
            for y in range(surface.height())
            for x in range(surface.width())
            if (surface.pixel(x, y) >> 24) & 0xFF
        ]
        assert opaque
        xs = [p[0] for p in opaque]
        ys = [p[1] for p in opaque]
        badge_left = min(xs)
        badge_top = min(ys)
        # Control top-left physical (dpr 1.0) = (200, 100); bbox-local subtracts
        # the bbox offset. The badge's top-left pixel sits on the control's
        # top-left corner.
        ctrl_left = rect.x - bbox.offset_x
        ctrl_top = rect.y - bbox.offset_y
        assert badge_left == ctrl_left
        assert badge_top == ctrl_top

    def test_numeral_glyph_fits_in_tight_badge_size(self, overlay_mgr):
        """The tight size from ``_numeral_badge_size`` actually CONTAINS the
        rendered glyph: rendering the numeral into exactly that size leaves the
        image-border pixels transparent, so no digit is clipped.

        This is the real fit contract. The size-equality tests above compare the
        render call against ``_numeral_badge_size``'s own output, so a bug INSIDE
        ``_numeral_badge_size`` (too-small margin, capHeight under-measuring,
        wrong dpr factor) would size every badge wrong AND still match itself,
        passing CI while digits clip on screen. This test renders the real glyph
        into the computed size and fails if the opaque ink reaches the border
        (wh-overlay-4bug-review.1)."""
        mgr, _mod, _ = overlay_mgr
        for number in (1, 7, 10, 100):
            for dpr in (1.0, 2.0):
                w, h = mgr._numeral_badge_size(number, dpr)
                img = mgr._render_badge(number, w, h, dpr)
                assert img.width() == w and img.height() == h
                xs: list[int] = []
                ys: list[int] = []
                for y in range(h):
                    for x in range(w):
                        if ((img.pixel(x, y) >> 24) & 0xFF) >= 128:
                            xs.append(x)
                            ys.append(y)
                assert xs, f"numeral {number} at dpr {dpr} drew nothing"
                # Opaque ink must stay strictly inside the image: a clipped digit
                # would push ink onto the first/last row or column.
                assert min(xs) >= 1 and min(ys) >= 1, (
                    f"numeral {number} at dpr {dpr} ink touches the top/left edge "
                    f"(min x={min(xs)}, y={min(ys)}) -- tight size too small"
                )
                assert max(xs) <= w - 2 and max(ys) <= h - 2, (
                    f"numeral {number} at dpr {dpr} ink touches the bottom/right "
                    f"edge (max x={max(xs)}, y={max(ys)} in {w}x{h}) -- tight size "
                    "too small"
                )

    def test_numeral_font_metrics_built_once_per_surface(self, overlay_mgr):
        """The numeral font metrics are built ONCE per surface render and shared
        across every numeral badge, not rebuilt per badge.

        Fix 4 added a ``QFontMetricsF`` construction in ``_numeral_badge_size``
        on top of the one ``_render_badge`` already does, doubling the
        font-metrics work on the dense "show numbers" paint path that fix 4 was
        meant to lighten. ``_render_monitor_surface`` now builds the metrics once
        and passes them to ``_numeral_badge_size`` for each badge, so the count
        is independent of badge count (wh-overlay-4bug-review.2)."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        rects = [
            _paint_rect(10, monitor, x=100 + 200 * i, y=200, width=60, height=24)
            for i in range(3)
        ]
        badges = [(r, i + 1) for i, r in enumerate(rects)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        with patch.object(
            mod, "QFontMetricsF", wraps=mod.QFontMetricsF
        ) as metrics_mock:
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert metrics_mock.call_count == 1, (
            f"QFontMetricsF built {metrics_mock.call_count} times for 3 badges; "
            "the sizing metrics should be built once and shared"
        )


# ===========================================================================
# Step 4b: bounding-box surface refactor (wh-n29v.56.1)
#
# The per-monitor paint surface (QImage + GDI DIB) and the per-monitor window
# are bounded to the badge bounding box (plus margin), NOT the monitor's full
# physical resolution, so transient paint memory scales with badge count, not
# monitor resolution. On-screen badge placement stays pixel-identical at any
# DPR.
# ===========================================================================


class TestBoundingBoxSurface:
    def test_multi_badge_surface_equals_bbox_plus_margin(self, overlay_mgr):
        """For a multi-badge layout the surface is the union bounding box of
        the badge rects plus the outline/shadow margin -- NOT the monitor's
        full physical resolution."""
        mgr, mod, _ = overlay_mgr
        # Pin inside placement: this test checks the multi-badge union bbox, not
        # the trailing-space placement (covered by
        # TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        # 1920x1080 physical monitor, dpr 1.0 (so logical == physical).
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Three badges spread across the monitor.
        rects = [
            _paint_rect(10, monitor, x=100, y=200, width=80, height=30),
            _paint_rect(10, monitor, x=500, y=210, width=80, height=30),
            _paint_rect(10, monitor, x=300, y=600, width=120, height=40),
        ]
        badges = [(r, i + 1) for i, r in enumerate(rects)]

        # Union bbox of the badge rects:
        #   left   = min(100, 500, 300)                 = 100
        #   top    = min(200, 210, 600)                 = 200
        #   right  = max(180, 580, 420)                 = 580
        #   bottom = max(230, 240, 640)                 = 640
        # Plus the 5px margin on every side (dpr 1.0):
        #   offset = (100-5, 200-5)                     = (95, 195)
        #   size   = (580+5 - (100-5), 640+5 - (200-5)) = (490, 450)
        bbox = mgr._compute_monitor_bbox(monitor, badges)
        assert (bbox.offset_x, bbox.offset_y) == (95, 195)
        assert (bbox.width, bbox.height) == (490, 450)

        with patch.object(
            mgr,
            "_render_badge",
            return_value=QImage(2, 2, QImage.Format.Format_ARGB32_Premultiplied),
        ):
            surface = mgr._render_monitor_surface(monitor, badges, bbox)
        assert surface.width() == 490
        assert surface.height() == 450
        # NOT the full monitor resolution.
        assert surface.width() != 1920
        assert surface.height() != 1080

    def test_bbox_contains_numeral_overhang_on_narrow_control(self, overlay_mgr):
        """A multi-digit numeral on a control narrower than the numeral image
        overhangs the control's edges (such a control is small in both
        dimensions, so it takes the corner-POINT placement of
        wh-overlay-small-control-cover, half outside on both axes). The
        bounding-box surface must grow to contain the full numeral, not clip it
        at the surface edge (wh-overlay-4bug-review-r1.1; retargeted for the
        top-left anchor in wh-overlay-badge-occludes-label). The expected
        footprint is derived from the placement function itself, so the test
        pins the CONTAINMENT contract, not one placement rule."""
        mgr, _mod, _ = overlay_mgr
        # Pins TOP-LEFT: the bbox-grows-to-contain-the-overhang contract holds
        # for any corner; asserted here with the top-left overhang direction.
        mgr._badge_corner = "top_left"
        # Pin inside placement: this test checks the bbox overhang containment,
        # not the trailing-space placement (covered by
        # TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Narrow control (20 logical px wide) with a 3-digit number, placed away
        # from the monitor edges so the monitor clamp does not mask the check.
        rect = _paint_rect(10, monitor, x=400, y=300, width=20, height=30)
        number = 100
        dpr = 1.0
        badges = [(rect, number)]

        bw, bh = mgr._numeral_badge_size(number, dpr)
        # Precondition: the numeral image really is wider than the control, so
        # its footprint overhangs the control's edges. (If this ever fails the
        # test is moot.)
        assert bw > rect.width, "test needs a numeral wider than the control"

        bbox = mgr._compute_monitor_bbox(monitor, badges)
        # The bbox must contain the numeral's ACTUAL footprint on every side.
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, bw, bh, dpr, 1920, 1080, corner="top_left",
        )
        assert bbox.offset_x <= left, (
            f"bbox left {bbox.offset_x} clips numeral left {left}"
        )
        assert bbox.offset_x + bbox.width >= right, (
            f"bbox right {bbox.offset_x + bbox.width} clips numeral right "
            f"{right}"
        )
        assert bbox.offset_y <= top
        assert bbox.offset_y + bbox.height >= bottom

    def test_numeral_top_left_placement_at_dpr_2(self, overlay_mgr):
        """At DPR 2.0, with the corner set to top_left, a numeral's ON-SCREEN
        top-left equals the control's top-left corner (logical*dpr), so the digit
        sits in the control's corner and leaves its label visible
        (wh-overlay-badge-occludes-label). Covers the top_left option's DPI
        scaling; top_right is the default."""
        mgr, mod, _ = overlay_mgr
        mgr._badge_corner = "top_left"
        # Pin inside placement: this test checks the top_left DPI scaling of the
        # corner anchor, not the trailing-space placement (covered by
        # TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = _paint_rect(10, monitor, x=400, y=250, width=60, height=24)
        badges = [(rect, 3)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)
        # Physical control top-left = (400*2, 250*2) = (800, 500); margin=5*2=10.
        # The numeral "3" is anchored to that corner and is smaller than the
        # 120x48 control on both axes, so the badge footprint stays inside the
        # control box and the bbox is the control box minus the margin:
        # offset = (floor(800-10), floor(500-10)) = (790, 490).
        assert (bbox.offset_x, bbox.offset_y) == (790, 490)

        def _opaque_badge(_n, _w, _h, _dpr=1.0):
            img = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(0xFFFFFFFF)
            return img

        with patch.object(mgr, "_render_badge", side_effect=_opaque_badge):
            surface = mgr._render_monitor_surface(monitor, badges, bbox)

        opaque = [
            (x, y)
            for y in range(surface.height())
            for x in range(surface.width())
            if (surface.pixel(x, y) >> 24) & 0xFF
        ]
        assert opaque
        xs = [p[0] for p in opaque]
        ys = [p[1] for p in opaque]
        # The numeral is anchored to the control's TOP-LEFT corner. Its on-screen
        # top-left physical = control top-left = (400*2, 250*2) = (800, 500).
        top_left_x = monitor.rect_phys.left() + bbox.offset_x + min(xs)
        top_left_y = monitor.rect_phys.top() + bbox.offset_y + min(ys)
        assert (top_left_x, top_left_y) == (800, 500)

    def test_bbox_clamped_when_control_overhangs_left_top(self, overlay_mgr):
        """A control partly off the monitor's LEFT/TOP edge (negative local
        x/y per the resolver contract) must clamp the bbox offset to >= 0 so
        the surface/window never starts before the monitor and never paints the
        off-monitor portion onto an adjacent monitor. The on-monitor portion's
        on-screen placement stays exactly where the old full-monitor surface
        clipped it (wh-n29v.64.1)."""
        mgr, mod, _ = overlay_mgr
        # Pins TOP-LEFT so the badge itself overhangs the left/top edge and the
        # test actually exercises the negative-footprint clamp. Under the new
        # top_right default the badge sits at the control's right edge and never
        # goes negative, so the clamp path would go untested (the control box
        # would dominate the union). wh-overlay-badge-occludes-label.
        mgr._badge_corner = "top_left"
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Control straddling the top-left corner: logical (-30, -20) size 100x50.
        rect = _paint_rect(10, monitor, x=-30, y=-20, width=100, height=50)
        badges = [(rect, 1)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)

        # Footprint left=-30/top=-20 clamps to 0; right=70/bottom=30 stay.
        # Margin 5 then clamps min back to 0; max = 70+5 / 30+5.
        assert (bbox.offset_x, bbox.offset_y) == (0, 0)
        assert (bbox.width, bbox.height) == (75, 35)
        # The window therefore starts AT the monitor's top-left, not before it.
        assert monitor.rect_phys.left() + bbox.offset_x == 0
        assert monitor.rect_phys.top() + bbox.offset_y == 0

    def test_bbox_clamped_when_control_overhangs_right_bottom(self, overlay_mgr):
        """A control extending past the monitor's RIGHT/BOTTOM edge must clamp
        the bbox so the surface/window stays within the monitor, preserving the
        old full-monitor clipping and the allocation cap (wh-n29v.64.1)."""
        mgr, mod, _ = overlay_mgr
        # Pins TOP-LEFT so the bbox and the direct footprint call below compare
        # against the same corner; the clamp-to-monitor contract is per-corner.
        mgr._badge_corner = "top_left"
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Control overhanging the bottom-right: logical (1900, 1060) size 100x50,
        # so physical right=2000 (> 1920) and bottom=1110 (> 1080).
        rect = _paint_rect(10, monitor, x=1900, y=1060, width=100, height=50)
        badges = [(rect, 1)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)

        # The control overhangs bottom-right. The badge, anchored at the control
        # top-left, would too, so it shifts inward to stay on the monitor
        # (wh-review-click-overlay-codex.2). The bbox is still clamped so the
        # window's right/bottom edge stays inside the monitor.
        bw, bh = mgr._numeral_badge_size(1, 1.0)
        fl, ft, fr, fb = mgr._numeral_badge_footprint_phys(
            rect, bw, bh, 1.0, 1920, 1080, corner="top_left"
        )
        # The shifted badge is fully on the monitor.
        assert fr <= 1920 and fb <= 1080
        # The window's right/bottom edge stays inside the monitor.
        assert monitor.rect_phys.left() + bbox.offset_x + bbox.width == 1920
        assert monitor.rect_phys.top() + bbox.offset_y + bbox.height == 1080
        # The bbox fully contains the shifted badge footprint (no digit clipped).
        assert bbox.offset_x <= fl and bbox.offset_y <= ft
        assert bbox.offset_x + bbox.width >= fr
        assert bbox.offset_y + bbox.height >= fb

    def test_bbox_contains_bottom_right_badge_at_dpr_2(self, overlay_mgr):
        """End-to-end bbox containment for a NON-top-left corner at DPR 2.0
        (wh-overlay-badge-occludes-label). The default top_right anchor and the
        bottom corners were only unit-tested against the placement helper at
        dpr=1.0; the existing full-surface containment tests were all pinned to
        top_left. This closes that gap: a bottom_right-anchored badge on a
        control flush in the monitor's bottom-right corner at dpr=2.0 must stay
        fully on the monitor and fully inside the computed bbox, so no digit is
        clipped."""
        mgr, _mod, _ = overlay_mgr
        mgr._badge_corner = "bottom_right"
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        # Control flush in the bottom-right corner: logical (1890, 1050) size
        # 30x30, so physical right=3840 and bottom=2160 -- exactly the monitor
        # corner. The bottom_right anchor puts the badge's bottom-right at the
        # control's bottom-right, i.e. the monitor's own corner.
        rect = _paint_rect(10, monitor, x=1890, y=1050, width=30, height=30)
        badges = [(rect, 7)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)

        bw, bh = mgr._numeral_badge_size(7, 2.0)
        fl, ft, fr, fb = mgr._numeral_badge_footprint_phys(
            rect, bw, bh, 2.0, 3840, 2160, corner="bottom_right"
        )
        # The badge is fully on the monitor (bottom-right corner is the anchor).
        assert fr <= 3840 and fb <= 2160
        assert fl >= 0 and ft >= 0
        # The bbox fully contains the badge footprint at dpr=2.0 (no digit
        # clipped) -- the union is direction-agnostic, not top-left-specific.
        assert bbox.offset_x <= fl and bbox.offset_y <= ft
        assert bbox.offset_x + bbox.width >= fr
        assert bbox.offset_y + bbox.height >= fb

    def test_bbox_bounded_for_huge_malformed_bounds(self, overlay_mgr):
        """A malformed UIA bounds rectangle that overlaps the monitor by only a
        sliver but reports a huge off-screen extent must NOT allocate a surface
        proportional to the control's full reported size. The bbox stays bounded
        by the monitor (plus margin), restoring the old monitor-resolution
        allocation cap on the paint path (wh-n29v.64.1)."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Pin inside placement: this test checks the malformed-bounds allocation
        # cap, not the trailing-space placement.
        mgr._badge_trailing_space = False
        # Overlaps the monitor only in x=0..100 but reports a 100100px width.
        rect = _paint_rect(10, monitor, x=-100000, y=0, width=100100, height=100)
        badges = [(rect, 1)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)

        # Without the clamp this would be ~100110 wide; with it the footprint
        # collapses to the on-monitor sliver 0..100 plus margin.
        assert bbox.offset_x == 0
        assert bbox.width == 105
        assert bbox.width <= monitor.rect_phys.width()
        assert bbox.height <= monitor.rect_phys.height()

    def test_empty_monitor_creates_no_window(self, overlay_mgr):
        """A monitor with no badges allocates no surface and shows no window."""
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)
        # The single item resolves to None (off-screen / skipped), so the
        # monitor ends up with zero badges.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=None
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ) as composite_mock:
            result = mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        # No badges -> no window created, nothing composited.
        user32.CreateWindowExW.assert_not_called()
        composite_mock.assert_not_called()
        assert mgr._windows == {}
        assert result["state"] == "painted"
        assert result["monitor_ids"] == []

    def test_monitor_that_loses_all_badges_destroys_its_window(self, overlay_mgr):
        """A monitor painted in render 1 but with zero badges in render 2 has
        its (full-bbox) window torn down -- no stale window is left behind."""
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)
        # Render 1: one badge -> a window exists.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        assert 10 in mgr._windows
        user32.DestroyWindow.reset_mock()

        # Render 2: the item now resolves to None -> the monitor has no badges.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=None
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=1,
            )
        assert 10 not in mgr._windows
        user32.DestroyWindow.assert_called_once()

    def test_reuse_when_bbox_geometry_unchanged(self, overlay_mgr):
        """Two renders that produce the SAME bbox screen geometry reuse the
        same window (one CreateWindowExW), even across distinct monitor
        objects, so there is no churn when the badge layout is unchanged."""
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
            first_window = mgr._windows[10]
            # Same monitor, same badge -> same bbox geometry -> reuse.
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=1,
            )
        assert mgr._windows[10] is first_window
        assert user32.CreateWindowExW.call_count == 1

    def test_layout_change_rebuilds_bbox_window_no_stale_dib(self, overlay_mgr):
        """When the badge layout changes the bbox geometry, the old window is
        torn down and a new one created -- no stale full-monitor window/DIB is
        reused or leaked."""
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod,
            "resolve_overlay_paint_rect",
            return_value=_paint_rect(10, mon, x=100, y=100, width=80, height=30),
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        first_window = mgr._windows[10]
        first_geom = first_window.geom_phys
        user32.DestroyWindow.reset_mock()
        user32.CreateWindowExW.reset_mock()

        # Render 2: the badge moves far -> a different bbox geometry -> rebuild.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod,
            "resolve_overlay_paint_rect",
            return_value=_paint_rect(10, mon, x=900, y=700, width=80, height=30),
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=1,
            )
        new_window = mgr._windows[10]
        assert new_window is not first_window
        assert new_window.geom_phys != first_geom
        # Old window destroyed, new window created.
        user32.DestroyWindow.assert_called_once()
        user32.CreateWindowExW.assert_called_once()


class TestNumeralTrailingSpacePlacement:
    """The numeral is placed in the empty space just past the control's
    trailing edge when that strip is clear of other walked controls and stays
    on the monitor; otherwise it falls back to the configured corner
    (wh-overlay-badge-occludes-label follow-up).

    A File Explorer nav item or a Details-view row keeps its icon and label at
    the LEFT and has blank space to the right -- the nav item's box hugs a short
    folder label, and the file row ends before the scrollbar past the size
    column -- so a corner-anchored badge still landed on the label or on the
    size value. The trailing placement clears both. A grid tile or a packed
    toolbar button has a neighbour immediately to its right, so its strip is
    occupied and the badge stays in the corner, unchanged.
    """

    @staticmethod
    def _mon() -> _NativeMonitor:
        return _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)

    @staticmethod
    def _rect(mon, x, y, width, height) -> OverlayPaintRect:
        return OverlayPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=mon, hmonitor=1, screen=None,
        )

    def test_trailing_space_used_when_strip_clear(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        assert mgr._badge_trailing_space is True  # default on
        mon = self._mon()
        # Control right edge at x=400 (dpr 1.0); nothing else to its right.
        rect = self._rect(mon, 100, 200, 300, 40)
        left, top, right, bottom = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [], corner="top_right",
        )
        # Placed just OUTSIDE the right edge: left == control right edge (400),
        # NOT the inside-corner 380.
        assert (left, top) == (400.0, 200.0)
        assert (right, bottom) == (420.0, 230.0)

    def test_falls_back_to_corner_when_strip_occupied(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        # A neighbouring control occupies the strip just right of the control
        # (a grid tile / packed toolbar button); it overlaps the outside
        # candidate [400,420]x[200,230].
        neighbour = (405.0, 190.0, 505.0, 250.0)
        left, top, _r, _b = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [neighbour], corner="top_right",
        )
        # Inside top-right corner: left = 400 - 20 = 380.
        assert (left, top) == (380.0, 200.0)

    def test_falls_back_to_corner_at_monitor_right_edge(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # Control flush against the monitor's right edge (right = 1920): the
        # outside candidate would run off-screen, so it falls back to the corner
        # (which then shifts inward via the existing clamp).
        rect = self._rect(mon, 1900, 500, 20, 20)
        left, _t, right, _b = mgr._numeral_badge_placement_phys(
            rect, 30, 20, 1.0, 1920, 1080, [], corner="top_right",
        )
        assert right == 1920.0  # stays on the monitor
        assert left == 1890.0   # 1920 - 30, inside the corner

    def test_trailing_space_off_uses_corner(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mgr._badge_trailing_space = False
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        # With trailing placement off, the number stays in the corner even
        # though the strip to the right is clear.
        left, top, _r, _b = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [], corner="top_right",
        )
        assert (left, top) == (380.0, 200.0)

    def test_trailing_side_follows_left_corner(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # A left corner places the badge just past the control's LEFT edge (the
        # left gutter), symmetric with the right case.
        rect = self._rect(mon, 200, 200, 100, 40)
        left, _t, right, _b = mgr._numeral_badge_placement_phys(
            rect, 30, 20, 1.0, 1920, 1080, [], corner="top_left",
        )
        assert right == 200.0  # badge right edge at the control left edge
        assert left == 170.0   # 200 - 30

    def test_self_never_blocks_own_trailing_placement(self, overlay_mgr):
        # The control's own box is allowed in the rect list (the callers pass the
        # full per-monitor list); it shares exactly one edge with the outside
        # candidate and must not count as an overlap.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        own = (100.0, 200.0, 400.0, 240.0)
        left, top, _r, _b = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [own], corner="top_right",
        )
        assert (left, top) == (400.0, 200.0)  # still placed outside

    def test_manager_reads_trailing_space_from_construction(self):
        import overlay_paint_window
        m = overlay_paint_window.OverlayPaintWindowManager(
            badge_trailing_space=False
        )
        assert m._badge_trailing_space is False
        m2 = overlay_paint_window.OverlayPaintWindowManager()
        assert m2._badge_trailing_space is True

    def test_bbox_and_render_agree_on_trailing_badge(self, overlay_mgr):
        # End-to-end: with clear trailing space the badge is drawn just OUTSIDE
        # the control's right edge, and the bounding box contains it -- the paint
        # site and the bbox site agree.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        badges = [(rect, 5)]
        with patch.object(mgr, "_numeral_badge_size", return_value=(24, 24)):
            bbox = mgr._compute_monitor_bbox(mon, badges)

            def _opaque_badge(_n, _w, _h, _dpr=1.0):
                img = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(0xFFFFFFFF)
                return img

            with patch.object(mgr, "_render_badge", side_effect=_opaque_badge):
                surface = mgr._render_monitor_surface(mon, badges, bbox)

        xs = [
            x
            for y in range(surface.height())
            for x in range(surface.width())
            if (surface.pixel(x, y) >> 24) & 0xFF
        ]
        assert xs
        # On-screen left edge of the painted badge == the control's right edge
        # (400): the badge sits just past the control, not on it.
        on_screen_left = mon.rect_phys.left() + bbox.offset_x + min(xs)
        assert on_screen_left == 400


def _rects_disjoint(a, b):
    """True when two (l, t, r, b) rects do not overlap by area."""
    return not (a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1])


class TestBadgeCollisionAvoidance:
    """Two numeral badges must never be drawn on top of each other
    (wh-overlay-badge-collision).

    The live-test trigger: a File Explorer column header and the thin
    column-resize splitter beside it share an edge; both are numbered, both
    trailing strips are occupied, and both corner anchors land in the same
    spot, so the two digits stacked into an unreadable blob (39/40 on the
    Size header). Placement is now a sequential pass: each badge avoids the
    badges already placed, nudging inward / below / above when the corner
    collides, and only a pathological pile-up (every candidate occupied)
    accepts the overlap rather than dropping the number.
    """

    @staticmethod
    def _mon() -> _NativeMonitor:
        return _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)

    @staticmethod
    def _rect(mon, x, y, width, height) -> OverlayPaintRect:
        return OverlayPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=mon, hmonitor=1, screen=None,
        )

    def test_corner_fallback_avoids_already_placed_badge(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        # A neighbour occupies the trailing strip, forcing the corner anchor;
        # an earlier badge already sits exactly in that corner.
        neighbour = (405.0, 190.0, 505.0, 250.0)
        placed = [(380.0, 200.0, 400.0, 230.0)]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [neighbour],
            corner="top_right", placed_badges=placed,
        )
        assert _rects_disjoint(got, placed[0])
        # Nudged INWARD along the corner's horizontal side: one badge width
        # plus the 3-logical-px collision gap left of the base corner.
        assert (got[0], got[1]) == (357.0, 200.0)

    def test_trailing_candidate_avoids_placed_badge(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        # The trailing strip is clear of CONTROLS but an earlier badge already
        # occupies it, so the badge falls back to the inside corner.
        placed = [(400.0, 200.0, 420.0, 230.0)]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [],
            corner="top_right", placed_badges=placed,
        )
        assert _rects_disjoint(got, placed[0])
        assert (got[0], got[1]) == (380.0, 200.0)

    def test_placement_pass_header_splitter_profile(self, overlay_mgr):
        # The live-test profile: wide header + thin splitter + next header,
        # all in one band. Every pairwise placement must be disjoint.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 100, 0, 300, 40), 1),   # header
            (self._rect(mon, 400, 0, 8, 40), 2),     # splitter (thinner than
                                                     # one badge)
            (self._rect(mon, 408, 0, 200, 40), 3),   # next header
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for entry in placements for (_w, _h, fp) in [entry]]
        assert len(rects) == 3
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        # Every badge stays on the monitor.
        for left, top, right, bottom in rects:
            assert left >= 0.0 and top >= 0.0
            assert right <= 1920.0 and bottom <= 1080.0

    def test_placement_pass_is_deterministic(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 100, 0, 300, 40), 1),
            (self._rect(mon, 400, 0, 8, 40), 2),
            (self._rect(mon, 408, 0, 200, 40), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            first = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
            second = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        assert first == second

    def test_placement_pass_never_drops_a_badge(self, overlay_mgr):
        # Pathological pile-up: five identical controls stacked on the same
        # coordinates. Candidates run out, but every badge still gets a
        # placement (overlap as last resort beats a missing number).
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [(self._rect(mon, 500, 500, 30, 40), n) for n in range(1, 6)]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        assert len(placements) == 5
        assert all(entry is not None for entry in placements)

    def test_placement_pass_skips_working_glyph(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        import overlay_paint_window as opw
        badges = [
            (self._rect(mon, 100, 200, 300, 40), 1),
            (self._rect(mon, 100, 300, 60, 60), opw.WORKING_BADGE_NUMBER),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        assert placements[0] is not None
        assert placements[1] is None

    def test_nudge_prefers_candidate_off_other_controls(self, overlay_mgr):
        # wh-overlay-collision-review.1: a nudge spot that lands on ANOTHER
        # numbered control reads as labeling that control. When the inward
        # candidate is taken by a badge and the below candidate sits on a
        # neighbouring control, the resolver must prefer the above candidate
        # (free of both), even though below comes first in the nudge order.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        own_box = (100.0, 200.0, 400.0, 240.0)
        neighbour_right = (400.0, 200.0, 500.0, 240.0)   # blocks trailing strip
        neighbour_below = (300.0, 240.0, 400.0, 280.0)   # under the below nudge
        placed = [
            (380.0, 200.0, 400.0, 230.0),   # on the base corner anchor
            (357.0, 200.0, 377.0, 230.0),   # on the inward nudge spot
        ]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080,
            [own_box, neighbour_right, neighbour_below],
            corner="top_right", placed_badges=placed,
        )
        # Above nudge: base top minus badge height minus the 3px gap.
        assert (got[0], got[1]) == (380.0, 167.0)

    def test_nudge_onto_other_control_still_beats_overlapping_a_badge(
        self, overlay_mgr
    ):
        # Second tier: when EVERY control-free spot is gone, a nudge onto a
        # neighbouring control is still better than stacking on a badge.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        own_box = (100.0, 200.0, 400.0, 240.0)
        neighbour_right = (400.0, 200.0, 500.0, 240.0)
        # Controls above AND below, so no nudge spot is control-free.
        neighbour_below = (300.0, 240.0, 400.0, 280.0)
        neighbour_above = (300.0, 160.0, 400.0, 200.0)
        placed = [
            (380.0, 200.0, 400.0, 230.0),
            (357.0, 200.0, 377.0, 230.0),
        ]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080,
            [own_box, neighbour_right, neighbour_below, neighbour_above],
            corner="top_right", placed_badges=placed,
        )
        # First badge-free candidate in nudge order: below.
        assert (got[0], got[1]) == (380.0, 233.0)
        assert not any(
            mgr._rects_overlap_phys(got, p) for p in placed
        )

    def test_bbox_contains_every_nudged_badge(self, overlay_mgr):
        # The surface bounding box must contain the FINAL (possibly nudged)
        # placements, or a nudged digit would clip at the surface edge.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 100, 0, 300, 40), 1),
            (self._rect(mon, 400, 0, 8, 40), 2),
            (self._rect(mon, 408, 0, 200, 40), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            bbox = mgr._compute_monitor_bbox(mon, badges)
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        for _w, _h, (left, top, right, bottom) in placements:
            assert left >= bbox.offset_x
            assert top >= bbox.offset_y
            assert right <= bbox.offset_x + bbox.width
            assert bottom <= bbox.offset_y + bbox.height


class TestSmallControlCornerPoint:
    """On a control small in BOTH dimensions (under twice the badge size), the
    corner anchor centers the badge on the corner POINT instead of tucking it
    fully inside, so only about a quarter of the badge covers the icon
    (wh-overlay-small-control-cover).

    The live-test trigger: packed toolbar icon buttons (Notepad's B/I/U row,
    Explorer's cut/copy/paste row) have no clear trailing strip, and the
    inside-corner badge covered most of each icon. Wide controls (list rows,
    column headers, tabs) keep the exact inside-corner behaviour; the
    corner-point placement would be ambiguous between two stacked rows.
    """

    @staticmethod
    def _mon() -> _NativeMonitor:
        return _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)

    @staticmethod
    def _rect(mon, x, y, width, height) -> OverlayPaintRect:
        return OverlayPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=mon, hmonitor=1, screen=None,
        )

    def test_small_control_centers_badge_on_corner_point(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        # Control 30x40, badge 20x30: both dimensions under twice the badge.
        rect = self._rect(self._mon(), 500, 500, 30, 40)
        left, top, right, bottom = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, 1920, 1080, corner="top_right",
        )
        # Centered on the top-right corner point (530, 500).
        assert (left, top) == (520.0, 485.0)
        assert (right, bottom) == (540.0, 515.0)

    def test_small_control_corner_point_other_corner(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        rect = self._rect(self._mon(), 500, 500, 30, 40)
        left, top, _r, _b = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, 1920, 1080, corner="bottom_left",
        )
        # Centered on the bottom-left corner point (500, 540).
        assert (left, top) == (490.0, 525.0)

    def test_wide_control_keeps_inside_corner(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        # 300 wide >= twice the badge width: a list row / header. Placement
        # unchanged -- fully inside the top-right corner.
        rect = self._rect(self._mon(), 100, 200, 300, 40)
        left, top, _r, _b = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, 1920, 1080, corner="top_right",
        )
        assert (left, top) == (380.0, 200.0)

    def test_small_control_corner_point_clamped_at_monitor_top(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        # Small control at the very top of the monitor: the half-above overhang
        # would leave the monitor, so the badge clamps back to y=0.
        rect = self._rect(self._mon(), 500, 0, 30, 40)
        _l, top, _r, _b = mgr._numeral_badge_footprint_phys(
            rect, 20, 30, 1.0, 1920, 1080, corner="top_right",
        )
        assert top == 0.0

    def test_small_control_still_prefers_clear_trailing_strip(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # A small control WITH a clear trailing strip keeps the strictly better
        # trailing placement (fully beside the icon, covering nothing).
        rect = self._rect(mon, 500, 500, 30, 40)
        left, top, _r, _b = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [], corner="top_right",
        )
        assert (left, top) == (530.0, 500.0)

    def test_packed_small_controls_get_disjoint_badges(self, overlay_mgr):
        # Buttons narrower than one badge, packed edge to edge: straddled
        # corner-point badges would overlap, so the collision pass separates
        # them. Every pair must be disjoint.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 100, 100, 15, 40), 1),
            (self._rect(mon, 115, 100, 15, 40), 2),
            (self._rect(mon, 130, 100, 15, 40), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)


# ===========================================================================
# Step 5: paint_overlay end-to-end + emit
# ===========================================================================


class TestPaintEndToEnd:
    def test_paint_emits_painted_state(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mon_a = _native_monitor(10, left=0)
        mon_b = _native_monitor(20, left=1920)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a, mon_b]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.side_effect = [
                _paint_rect(10, mon_a),
                _paint_rect(20, mon_b),
            ]
            items = [_make_item(1), _make_item(2)]
            result = mgr.paint(
                _make_summary(items, snapshot_id="snap-XYZ"),
                overlay_session_id=9,
                paint_generation=2,
            )
        assert result["action"] == "overlay_state_changed"
        assert result["state"] == "painted"
        assert result["overlay_session_id"] == 9
        assert result["paint_generation"] == 2
        assert sorted(result["monitor_ids"]) == [10, 20]
        assert result["snapshot_id"] == "snap-XYZ"

    def test_stale_paint_emits_no_window_and_no_painted(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)
        # Advance the mark with a first paint.
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
        ), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=5,
                paint_generation=3,
            )
        user32.CreateWindowExW.reset_mock()
        # A stale paint (older generation) must be a no-op: no window churn.
        result = mgr.paint(
            _make_summary([_make_item(1)]),
            overlay_session_id=5,
            paint_generation=2,
        )
        assert result is None
        user32.CreateWindowExW.assert_not_called()

    def test_internal_failure_emits_failed_state(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(10)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
        ), patch.object(
            mgr, "_render_badge", side_effect=RuntimeError("boom")
        ):
            result = mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        assert result["state"] == "failed"
        assert result["overlay_session_id"] == 1
        assert result["paint_generation"] == 0


# ===========================================================================
# Step 6: clear_overlay + emit
# ===========================================================================


class TestClear:
    def test_clear_tears_down_all_and_emits_cleared(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        mon_a = _native_monitor(10, left=0)
        mon_b = _native_monitor(20, left=1920)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a, mon_b]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.side_effect = [
                _paint_rect(10, mon_a),
                _paint_rect(20, mon_b),
            ]
            mgr.paint(
                _make_summary([_make_item(1), _make_item(2)]),
                overlay_session_id=4,
                paint_generation=1,
            )
        user32.DestroyWindow.reset_mock()
        result = mgr.clear(overlay_session_id=4, paint_generation=2)
        assert result["action"] == "overlay_state_changed"
        assert result["state"] == "cleared"
        assert result["overlay_session_id"] == 4
        assert result["paint_generation"] == 2
        # Both windows destroyed.
        assert user32.DestroyWindow.call_count == 2

    def test_clear_advances_mark_blocks_prior_gen_paint(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        mon = _native_monitor(10)

        def _do_paint(session, gen):
            with patch.object(
                mod, "_enumerate_native_monitors", return_value=[mon]
            ), patch.object(mod, "_screens", return_value=[]), patch.object(
                mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon)
            ), patch.object(
                mgr, "_render_monitor_surface", side_effect=_surface_stub
            ), patch.object(
                mod, "build_layered_dib", return_value=MagicMock()
            ), patch.object(
                mod, "composite_layered_window", return_value=True
            ):
                return mgr.paint(
                    _make_summary([_make_item(1)]),
                    overlay_session_id=session,
                    paint_generation=gen,
                )

        _do_paint(8, 1)
        clear_result = mgr.clear(overlay_session_id=8, paint_generation=2)
        assert clear_result["state"] == "cleared"
        user32.CreateWindowExW.reset_mock()
        # A prior-generation paint is now stale.
        stale = _do_paint(8, 1)
        assert stale is None
        user32.CreateWindowExW.assert_not_called()


# ===========================================================================
# Step 7: GUI routing (paint_overlay / clear_overlay -> manager -> emit)
# ===========================================================================
#
# A full GuiManager.__init__ constructs the system-tray icon, the floating
# button, the persistent terminal editor QDialog, and several lazy toasts --
# heavyweight Qt objects that need a real tray/display environment. The
# overlay wiring is a thin, self-contained two-branch addition, so these
# tests bind the three GuiManager methods (_handle_paint_overlay /
# _handle_clear_overlay / _emit_overlay_state_changed) onto a minimal
# stand-in carrying just a mock overlay manager and a mock back-channel
# queue. This exercises the exact routing the dispatch chain uses (parse ->
# manager call -> put_nowait) without the GuiManager construction cost. The
# manager's own behaviour is covered by the classes above.


class _GuiStub:
    """Minimal carrier for the three GuiManager overlay methods."""

    def __init__(self, manager, queue):
        from gui import GuiManager

        self._overlay_manager = manager
        self.commands_to_logic_queue = queue
        self._handle_paint_overlay = GuiManager._handle_paint_overlay.__get__(self)
        self._handle_clear_overlay = GuiManager._handle_clear_overlay.__get__(self)
        self._emit_overlay_state_changed = (
            GuiManager._emit_overlay_state_changed.__get__(self)
        )


class TestGuiRouting:
    def test_paint_overlay_routes_to_manager_and_emits(self, qapp):
        manager = MagicMock()
        emitted = {"action": "overlay_state_changed", "state": "painted"}
        manager.paint.return_value = emitted
        queue = MagicMock()
        stub = _GuiStub(manager, queue)

        # Build a valid paint_overlay wire dict via the schema.
        from shared.paint_overlay import PaintOverlayEvent

        summary = _make_summary([_make_item(1)], snapshot_id="snap-7")
        payload = PaintOverlayEvent(
            overlay_session_id=3, paint_generation=2, summary=summary
        ).to_dict()

        stub._handle_paint_overlay(payload)

        # The manager was driven with the parsed session/generation.
        manager.paint.assert_called_once()
        ckwargs = manager.paint.call_args
        assert ckwargs.kwargs["overlay_session_id"] == 3
        assert ckwargs.kwargs["paint_generation"] == 2
        # The result was forwarded back to Logic.
        queue.put_nowait.assert_called_once_with(emitted)

    def test_clear_overlay_routes_to_manager_and_emits(self, qapp):
        manager = MagicMock()
        emitted = {"action": "overlay_state_changed", "state": "cleared"}
        manager.clear.return_value = emitted
        queue = MagicMock()
        stub = _GuiStub(manager, queue)

        from shared.clear_overlay import ClearOverlayEvent

        payload = ClearOverlayEvent(
            overlay_session_id=9, paint_generation=4
        ).to_dict()

        stub._handle_clear_overlay(payload)

        manager.clear.assert_called_once_with(
            overlay_session_id=9, paint_generation=4
        )
        queue.put_nowait.assert_called_once_with(emitted)

    def test_stale_paint_emits_nothing(self, qapp):
        manager = MagicMock()
        manager.paint.return_value = None  # stale-gated
        queue = MagicMock()
        stub = _GuiStub(manager, queue)

        from shared.paint_overlay import PaintOverlayEvent

        summary = _make_summary([_make_item(1)])
        payload = PaintOverlayEvent(
            overlay_session_id=1, paint_generation=0, summary=summary
        ).to_dict()

        stub._handle_paint_overlay(payload)
        queue.put_nowait.assert_not_called()

    def test_malformed_paint_payload_dropped(self, qapp):
        manager = MagicMock()
        queue = MagicMock()
        stub = _GuiStub(manager, queue)

        # Missing overlay_session_id -> PaintOverlayEventSchemaError ->
        # safe_parse logs and returns None -> manager not called.
        stub._handle_paint_overlay({"action": "paint_overlay"})
        manager.paint.assert_not_called()
        queue.put_nowait.assert_not_called()

    def test_no_overlay_manager_is_noop(self, qapp):
        queue = MagicMock()
        stub = _GuiStub(None, queue)

        from shared.paint_overlay import PaintOverlayEvent

        summary = _make_summary([_make_item(1)])
        payload = PaintOverlayEvent(
            overlay_session_id=1, paint_generation=0, summary=summary
        ).to_dict()
        # Manager is None (construction failed): handler must be a clean
        # no-op, not crash.
        stub._handle_paint_overlay(payload)
        queue.put_nowait.assert_not_called()


# ===========================================================================
# Step 8: reviewer_1 (codex) findings wh-n29v.55.1 .. .55.4
# ===========================================================================


def _paint_single_monitor(mgr, mod, hmonitor, session, gen, *, composite=True):
    """Drive one paint that creates an overlay window for ``hmonitor``.

    Returns (result_dict_or_None, monitor). Used by the .55.x regression
    tests to get a populated ``mgr._windows`` before exercising the fix.
    """
    mon = _native_monitor(hmonitor)
    with patch.object(
        mod, "_enumerate_native_monitors", return_value=[mon]
    ), patch.object(mod, "_screens", return_value=[]), patch.object(
        mod, "resolve_overlay_paint_rect", return_value=_paint_rect(hmonitor, mon)
    ), patch.object(
        mgr, "_render_monitor_surface", side_effect=_surface_stub
    ), patch.object(
        mod, "build_layered_dib", return_value=MagicMock()
    ), patch.object(
        mod, "composite_layered_window", return_value=composite
    ):
        result = mgr.paint(
            _make_summary([_make_item(1)]),
            overlay_session_id=session,
            paint_generation=gen,
        )
    return result, mon


class TestStaleClearDoesNotTearDownNewerOverlay:
    """wh-n29v.55.1 -- a stale clear must not destroy a newer overlay."""

    def test_stale_clear_returns_none_and_leaves_windows(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        # Paint at a NEWER pair (session 5, gen 4): advances the mark and
        # populates a window.
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        assert mgr._windows, "expected an overlay window after paint"
        user32.DestroyWindow.reset_mock()

        # A clear at a strictly-OLDER pair (session 5, gen 3) is stale: the
        # mark already advanced past it. It must return None, tear down
        # NOTHING, and emit nothing.
        stale = mgr.clear(overlay_session_id=5, paint_generation=3)
        assert stale is None
        assert mgr._windows, "stale clear must NOT destroy the newer overlay"
        user32.DestroyWindow.assert_not_called()

    def test_clear_at_or_above_mark_still_tears_down(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        user32.DestroyWindow.reset_mock()

        # A clear at a pair >= the mark behaves as before: tears down and
        # returns the cleared dict.
        cleared = mgr.clear(overlay_session_id=5, paint_generation=4)
        assert cleared is not None
        assert cleared["state"] == "cleared"
        assert not mgr._windows
        user32.DestroyWindow.assert_called_once()


class TestRegisterClassResetsArgtypes:
    """wh-n29v.55.2 -- RegisterClassExW.argtypes is reset before byref."""

    def test_registration_survives_cross_module_argtypes(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr

        # Simulate software_dimmer having configured the process-global
        # user32.RegisterClassExW.argtypes with a DIFFERENT (incompatible)
        # ctypes.Structure subclass. With a real ctypes function pointer this
        # makes a byref(this-module's WNDCLASSEXW) call raise
        # ctypes.ArgumentError; here we pin the contract that the overlay
        # resets argtypes to None at call time so the byref is accepted.
        class _OtherStruct(ctypes.Structure):
            _fields_ = [("dummy", ctypes.c_int)]

        seeded = [ctypes.POINTER(_OtherStruct)]
        user32.RegisterClassExW.argtypes = seeded

        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)

        # Registration succeeded (no ArgumentError reached paint), so the
        # paint is "painted", not "failed".
        assert result["state"] == "painted"
        # The overlay explicitly cleared the conflicting cross-module
        # argtypes before its byref call.
        assert user32.RegisterClassExW.argtypes is None


class TestRegisterClassRetryOnFailure:
    """wh-overlay-class-register-retry -- a genuine RegisterClassExW failure
    must NOT mark the class registered. Otherwise one transient failure (low
    memory, OS resource exhaustion) makes the overlay permanently dead for the
    manager's lifetime: every later _ensure_class_registered short-circuits,
    every CreateWindowExW fails."""

    def test_genuine_failure_leaves_flag_false_and_retries(self, overlay_mgr):
        mgr, _mod, (user32, _, kernel32) = overlay_mgr
        # A genuine failure: RegisterClassExW returns 0 with a non-1410 error
        # (1410 == ERROR_CLASS_ALREADY_EXISTS is the only benign zero).
        user32.RegisterClassExW.reset_mock()
        user32.RegisterClassExW.return_value = 0
        kernel32.GetLastError.return_value = 8  # ERROR_NOT_ENOUGH_MEMORY

        mgr._ensure_class_registered()
        assert mgr._class_registered is False, (
            "a genuine registration failure must not mark the class registered"
        )
        first = user32.RegisterClassExW.call_count

        # The next attempt RETRIES registration (does not short-circuit).
        mgr._ensure_class_registered()
        assert user32.RegisterClassExW.call_count == first + 1

        # Once registration succeeds, the flag latches True and further calls
        # short-circuit (no redundant RegisterClassExW).
        user32.RegisterClassExW.return_value = 1
        mgr._ensure_class_registered()
        assert mgr._class_registered is True
        after_success = user32.RegisterClassExW.call_count
        mgr._ensure_class_registered()
        assert user32.RegisterClassExW.call_count == after_success

    def test_benign_already_exists_marks_registered_no_retry(self, overlay_mgr):
        """err == 1410 (a sibling manager in the same process already
        registered this class) is benign: the flag latches True, no retry."""
        mgr, _mod, (user32, _, kernel32) = overlay_mgr
        user32.RegisterClassExW.reset_mock()
        user32.RegisterClassExW.return_value = 0
        kernel32.GetLastError.return_value = 1410

        mgr._ensure_class_registered()
        assert mgr._class_registered is True
        calls = user32.RegisterClassExW.call_count
        mgr._ensure_class_registered()
        assert user32.RegisterClassExW.call_count == calls  # no retry


class TestItemBoundsAreXYWHNotLTRB:
    """wh-overlay-bounds-format-mismatch -- WalkSnapshotSummaryItem.bounds is
    (x, y, width, height), the ElementMatch convention used across the
    codebase (uia_walker._rect_to_bounds, clear_winner_rule, click_executor).
    resolve_overlay_paint_rect expects (left, top, right, bottom), the raw UIA
    BoundingRectangle convention. _do_paint must convert between them.

    Without the conversion the resolver reads ``width`` as ``right``: any
    control whose x exceeds its width gets a negative computed width and is
    dropped as degenerate, so nearly all real controls vanish and the few that
    survive (small x, large width) land near the top-left corner. These tests
    exercise the REAL resolver (NOT the _paint_rect stub) with realistic
    (x, y, w, h) controls that are well inside the monitor but degenerate when
    misread as (l, t, r, b).
    """

    def test_realistic_xywh_control_is_painted_at_its_position(
        self, overlay_mgr
    ):
        mgr, mod, _ = overlay_mgr
        # Primary monitor at origin 0, dpr 1.0 (dpi 96).
        mon = _native_monitor(10)

        # x=1000, y=200, size 100x50 -- valid (x,y,w,h), comfortably inside the
        # 1920x1080 monitor. Misread as (left,top,right,bottom) this is
        # left=1000, right=100 -> width=-900 -> dropped as degenerate.
        item = _make_item(7, bounds=(1000, 200, 100, 50), monitor_id=10)

        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            # resolve_overlay_paint_rect is the REAL function here, on purpose.
            result = mgr.paint(
                _make_summary([item]),
                overlay_session_id=1,
                paint_generation=0,
            )

        assert result["state"] == "painted", (
            "a real on-monitor control must paint, not be dropped as degenerate"
        )
        assert 10 in mgr._windows, (
            "the control's monitor must get an overlay window"
        )

        # The window must sit over the control's physical position (x=1000,
        # y=200), not jammed at the top-left corner. The surface is padded by
        # _SURFACE_MARGIN_PX on each side, so allow that much slack.
        geom = mgr._windows[10].geom_phys
        margin = mod._SURFACE_MARGIN_PX
        assert abs(geom.left() - 1000) <= margin + 1, (
            f"badge window left={geom.left()} should sit near control x=1000"
        )
        assert abs(geom.top() - 200) <= margin + 1, (
            f"badge window top={geom.top()} should sit near control y=200"
        )

    def test_right_edge_control_is_not_dropped(self, overlay_mgr):
        # A right-edge scrollbar like the log's (3648,143,48,1879)-style bound,
        # scaled to this monitor: x near the right edge, small width, tall.
        # As (x,y,w,h) it overlaps the monitor; as (l,t,r,b) right<left so it
        # is dropped. This is the exact shape that vanished in the field.
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(10)
        item = _make_item(3, bounds=(1860, 100, 24, 800), monitor_id=10)

        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            result = mgr.paint(
                _make_summary([item]),
                overlay_session_id=1,
                paint_generation=0,
            )

        assert result["state"] == "painted"
        assert 10 in mgr._windows, (
            "a right-edge control must not be dropped as degenerate"
        )

    def test_zero_and_negative_size_items_are_skipped_not_painted(
        self, overlay_mgr
    ):
        # The (x,y,w,h)->(l,t,r,b) conversion in _do_paint must hand the
        # resolver a degenerate rect for a zero- or negative-size control, and
        # the resolver then drops it. Such an item must be silently skipped
        # THROUGH the conversion: no overlay window, no crash, no mispaint
        # (wh-dictation-retraction-indicator.13.2). Resolver-level coverage of
        # zero/inverted bounds exists in test_overlay_dpi_resolver.py; this
        # pins the skip at the call site this commit added.
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(10)

        # (x,y,0,h)  -> left==right -> width 0 -> resolver returns None.
        # (x,y,-w,h) -> right<left -> negative width -> resolver returns None.
        for gen, bad_bounds in enumerate(
            ((1000, 200, 0, 50), (1000, 200, -100, 50))
        ):
            item = _make_item(7, bounds=bad_bounds, monitor_id=10)
            with patch.object(
                mod, "_enumerate_native_monitors", return_value=[mon]
            ), patch.object(mod, "_screens", return_value=[]), patch.object(
                mgr, "_render_monitor_surface", side_effect=_surface_stub
            ), patch.object(
                mod, "build_layered_dib", return_value=MagicMock()
            ), patch.object(
                mod, "composite_layered_window", return_value=True
            ):
                # resolve_overlay_paint_rect is the REAL function here.
                result = mgr.paint(
                    _make_summary([item]),
                    overlay_session_id=1,
                    paint_generation=gen,
                )

            assert result["state"] == "painted", (
                f"degenerate bounds {bad_bounds} must paint nothing, not error"
            )
            assert not result["monitor_ids"], (
                f"degenerate bounds {bad_bounds} must paint no monitor"
            )
            assert not mgr._windows, (
                f"degenerate bounds {bad_bounds} must create no overlay window"
            )


class TestCompositeFailureIsReportedFailed:
    """wh-n29v.55.3 -- a composite failure must not report 'painted' nor
    leave a stale DIB on a reused window."""

    def test_composite_failure_on_reused_window_fails_and_destroys(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, _) = overlay_mgr
        # First paint succeeds and creates the window (mark at session 1).
        ok_result, _mon = _paint_single_monitor(
            mgr, mod, 10, session=1, gen=0, composite=True
        )
        assert ok_result["state"] == "painted"
        assert 10 in mgr._windows
        first_window = mgr._windows[10]
        user32.DestroyWindow.reset_mock()

        # Second paint at the SAME rect_phys reuses the window (no rebuild),
        # but composite now FAILS. paint() must report 'failed' and the
        # failed monitor's window must be destroyed/removed so no stale
        # (prior-generation) DIB lingers on screen.
        fail_result, _mon2 = _paint_single_monitor(
            mgr, mod, 10, session=1, gen=1, composite=False
        )
        assert fail_result["state"] == "failed"
        assert 10 not in mgr._windows, (
            "a monitor whose composite failed must be removed so no stale "
            "DIB lingers"
        )
        # The reused window was the one that got destroyed.
        assert first_window.hwnd is None
        user32.DestroyWindow.assert_called()

    def test_composite_success_keeps_painted(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        result, _mon = _paint_single_monitor(
            mgr, mod, 10, session=1, gen=0, composite=True
        )
        assert result["state"] == "painted"
        assert 10 in mgr._windows


class TestDestroyFailureRetainsHandleAndWindow:
    """wh-n29v.55.4 -- a failed DestroyWindow must not orphan the window."""

    def test_destroy_failure_keeps_hwnd_and_window_then_retries(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        assert window.hwnd is not None

        # DestroyWindow fails (returns 0). The window is still on screen, so
        # the handle MUST be retained and the manager MUST keep the window
        # for a later retry. GetLastError must be consulted for the log.
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.reset_mock()
        kernel32.GetLastError.return_value = 1400  # ERROR_INVALID_WINDOW_HANDLE

        cleared = mgr.clear(overlay_session_id=1, paint_generation=1)
        # The clear pair advances the mark, so it is honored, but the
        # destroy failed: the window must be retained for retry.
        assert cleared is not None
        assert cleared["state"] == "cleared"
        assert window.hwnd is not None, (
            "a window whose DestroyWindow failed must keep its handle"
        )
        assert 10 in mgr._windows, (
            "the manager must retain a window that failed to destroy"
        )
        kernel32.GetLastError.assert_called()

        # A follow-up teardown where DestroyWindow now succeeds clears it.
        user32.DestroyWindow.return_value = 1
        mgr.clear(overlay_session_id=1, paint_generation=2)
        assert window.hwnd is None
        assert 10 not in mgr._windows

    def test_destroy_idempotent_on_none_hwnd(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        # First destroy succeeds -> hwnd None.
        assert window.destroy() is True
        assert window.hwnd is None
        user32.DestroyWindow.reset_mock()
        # Second destroy on an already-None hwnd is a no-op that still
        # reports success and does not call DestroyWindow again.
        assert window.destroy() is True
        assert window.hwnd is None
        user32.DestroyWindow.assert_not_called()

    def test_rebuild_path_failed_destroy_is_retained_not_orphaned(
        self, overlay_mgr
    ):
        """A rebuild (monitor moved/resized) whose old-window DestroyWindow
        fails must NOT orphan the old window -- it is retained for retry while
        the new window claims the hmonitor slot. Regression for wh-n29v.55.4
        rebuild-path gap."""
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        # Pin inside placement: this test checks window-rebuild geometry, not the
        # trailing-space placement (covered by TestNumeralTrailingSpacePlacement).
        mgr._badge_trailing_space = False
        # First paint at a monitor with a known geometry.
        mon_a = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(10, mon_a)
        ), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            first = mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=0,
            )
        assert first["state"] == "painted"
        old_window = mgr._windows[10]
        assert old_window.hwnd is not None

        # Second paint: the SAME hmonitor now has a DIFFERENT rect_phys, so
        # the manager rebuilds. DestroyWindow on the old window fails.
        mon_a_moved = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(100, 100, 1280, 720), dpi=96
        )
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.return_value = 1400
        new_hwnd = wintypes.HWND(0xBEEF)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon_a_moved]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod,
            "resolve_overlay_paint_rect",
            return_value=_paint_rect(10, mon_a_moved),
        ), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            user32.CreateWindowExW.return_value = new_hwnd
            second = mgr.paint(
                _make_summary([_make_item(1)]),
                overlay_session_id=1,
                paint_generation=1,
            )
        assert second["state"] == "painted"
        # (1) The old window is NOT orphaned: retained for retry.
        assert old_window.hwnd is not None
        assert old_window in mgr._pending_destroy
        # (2) The new window at the new geometry is created and tracked.
        # The window geometry is now the badge BOUNDING BOX in screen
        # physical coords (origin = monitor top-left + local bbox offset),
        # not the full monitor rect. The badge is at logical (5, 5) size
        # 100x40 on a dpr-1.0 monitor, so the local bbox is (0, 0, 110, 50)
        # after the 5px margin, and the new screen geom is at (100, 100).
        new_window = mgr._windows[10]
        assert new_window is not old_window
        assert new_window.geom_phys == QRect(100, 100, 110, 50)
        # The geometry changed across the monitor move, which is why a
        # rebuild (not a reuse) happened.
        assert old_window.geom_phys != new_window.geom_phys

        # (3) A later teardown where DestroyWindow now succeeds actually
        # destroys the retained old window and empties _pending_destroy.
        user32.DestroyWindow.return_value = 1
        mgr.clear(overlay_session_id=1, paint_generation=2)
        assert old_window.hwnd is None
        assert mgr._pending_destroy == []
        assert not mgr._windows

    def test_pending_destroy_orphan_swept_on_normal_paint_path(
        self, overlay_mgr
    ):
        """A rebuild-path orphan parked in _pending_destroy (its DestroyWindow
        failed) must be retried and drained on the NEXT normal paint, with no
        intervening clear / clear_all / exception (wh-n29v.63.1).

        Before this fix _pending_destroy was swept only by _destroy_all (the
        clear / clear_all / paint-except paths). The geom_phys rebuild key
        (wh-n29v.62) makes rebuilds routine, so a long run of repaints with no
        clear would leak one live, on-screen, always-on-top, click-through
        window per failed rebuild-destroy. The hot paint path must retry the
        orphan list itself.
        """
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        mon = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )

        def _paint_at(x: int, y: int, gen: int):
            # A different badge position yields a different bbox geom, which is
            # the rebuild key after wh-n29v.62.
            with patch.object(
                mod, "_enumerate_native_monitors", return_value=[mon]
            ), patch.object(mod, "_screens", return_value=[]), patch.object(
                mod,
                "resolve_overlay_paint_rect",
                return_value=_paint_rect(10, mon, x=x, y=y),
            ), patch.object(
                mgr, "_render_monitor_surface", side_effect=_surface_stub
            ), patch.object(
                mod, "build_layered_dib", return_value=MagicMock()
            ), patch.object(
                mod, "composite_layered_window", return_value=True
            ):
                return mgr.paint(
                    _make_summary([_make_item(1)]),
                    overlay_session_id=1,
                    paint_generation=gen,
                )

        # Paint 1: original layout -> window W1 owns the monitor slot.
        assert _paint_at(5, 5, 0)["state"] == "painted"
        first_window = mgr._windows[10]
        assert first_window.hwnd is not None

        # Paint 2: a different badge position -> different bbox -> rebuild. The
        # old window's DestroyWindow fails, so W1 is parked in _pending_destroy
        # while a fresh window claims the slot.
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.return_value = 1400
        user32.CreateWindowExW.return_value = wintypes.HWND(0xBEEF)
        assert _paint_at(200, 200, 1)["state"] == "painted"
        assert first_window in mgr._pending_destroy
        assert first_window.hwnd is not None

        # Paint 3: DestroyWindow now succeeds. A NORMAL paint (no clear) must
        # retry the parked orphan and drain it on the hot path.
        user32.DestroyWindow.return_value = 1
        user32.CreateWindowExW.return_value = wintypes.HWND(0xCAFE)
        assert _paint_at(400, 400, 2)["state"] == "painted"
        assert mgr._pending_destroy == [], (
            "a rebuild-path orphan must be swept on the normal paint path, "
            "not only by clear / clear_all"
        )
        assert first_window.hwnd is None, (
            "the drained orphan's window handle must be released on retry"
        )


# ===========================================================================
# 8. GUI config wiring (wh-n29v.58): the validated overlay badge settings
#    actually reach OverlayPaintWindowManager.
#
#    Today overlay_badge_font_pt and overlay_badge_shadow are validated by
#    ClickConfig but the GUI never reads the config or passes the two values,
#    so the constructor defaults (16 / True) are always used. This proves the
#    config -> ClickConfig.from_raw -> OverlayPaintWindowManager path.
# ===========================================================================


class TestGuiOverlayConfigWiring:
    def _build_manager(self, config):
        """Construct a GuiManager under the standard GUI test patches,
        spying on the OverlayPaintWindowManager constructor.

        Returns the spy mock so the caller can assert on the kwargs the
        overlay manager was constructed with.
        """
        # OverlayPaintWindowManager is imported lazily inside
        # GuiManager.__init__ via ``from overlay_paint_window import
        # OverlayPaintWindowManager``, so patch it at its source module.
        with patch(
            "overlay_paint_window.OverlayPaintWindowManager"
        ) as overlay_ctor, patch("gui.FloatingButton"), patch(
            "gui.WorkingDialog"
        ), patch(
            "gui.pystray"
        ) as mock_pystray, patch(
            "gui.QTimer"
        ):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager

            GuiManager(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                config=config,
            )
            return overlay_ctor

    def test_non_default_overlay_settings_reach_manager(self, qapp):
        config = {
            "click": {
                "overlay_badge_font_pt": 32,
                "overlay_badge_shadow": False,
                "overlay_badge_corner": "bottom_left",
                "overlay_badge_trailing_space": False,
            }
        }
        overlay_ctor = self._build_manager(config)
        # Two managers are now constructed -- the numbered overlay AND the
        # dedicated working-badge overlay (wh-dictation-retraction-indicator.3)
        # -- both with the same validated badge settings, so assert the
        # settings reached a construction (not that there was only one).
        assert overlay_ctor.call_count == 2
        overlay_ctor.assert_any_call(
            badge_font_pt=32,
            badge_shadow=False,
            badge_corner="bottom_left",
            badge_trailing_space=False,
        )

    def test_missing_click_block_uses_validated_defaults(self, qapp):
        # No [click] block: ClickConfig.from_raw({}) yields the validated
        # defaults (font 16, shadow True), and those reach the manager.
        overlay_ctor = self._build_manager({})
        # Two managers (numbered overlay + working-badge overlay), both with
        # the validated defaults.
        assert overlay_ctor.call_count == 2
        overlay_ctor.assert_any_call(
            badge_font_pt=16,
            badge_shadow=True,
            badge_corner="top_right",
            badge_trailing_space=True,
        )

    def test_no_config_argument_uses_validated_defaults(self, qapp):
        # Backward-compat: existing GuiManager constructions pass no config
        # at all. ClickConfig.from_raw({}) still yields validated defaults.
        with patch(
            "overlay_paint_window.OverlayPaintWindowManager"
        ) as overlay_ctor, patch("gui.FloatingButton"), patch(
            "gui.WorkingDialog"
        ), patch(
            "gui.pystray"
        ) as mock_pystray, patch(
            "gui.QTimer"
        ):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager

            GuiManager(MagicMock(), MagicMock(), MagicMock())
            # Two managers (numbered overlay + working-badge overlay), both
            # with the validated defaults.
            assert overlay_ctor.call_count == 2
            overlay_ctor.assert_any_call(
                badge_font_pt=16,
                badge_shadow=True,
                badge_corner="top_right",
                badge_trailing_space=True,
            )


# ===========================================================================
# Working badge: paint one busy/working glyph at an arbitrary screen point
# (wh-dictation-retraction-indicator.2)
# ===========================================================================


class TestWorkingBadge:
    """The overlay painter can show a single working/busy glyph at an
    arbitrary screen point, reusing the per-monitor click-through window and
    per-pixel-alpha compositing of the numbered overlay. v1 is a static
    one-shot badge: paint once at a point, clear on request, no per-frame
    following. The glyph is NOT a numeral.
    """

    def test_render_working_glyph_returns_nonempty_transparent_image(
        self, overlay_mgr
    ):
        """The glyph renders a premultiplied ARGB image with some opaque
        pixels (the glyph) over a transparent background (no filled box)."""
        mgr, mod, _ = overlay_mgr
        img = mgr._render_working_glyph(width=40, height=40)
        assert isinstance(img, QImage)
        assert img.format() == QImage.Format.Format_ARGB32_Premultiplied
        # Some glyph pixels were painted.
        opaque = any(
            (img.pixel(x, y) >> 24) & 0xFF
            for y in range(img.height())
            for x in range(img.width())
        )
        assert opaque, "expected the working glyph to paint some pixels"
        # Transparent background (a corner pixel is fully transparent): the
        # glyph is a shape, not a solid box.
        assert ((img.pixel(0, 0) >> 24) & 0xFF) == 0

    def test_render_badge_routes_working_sentinel_to_glyph(self, overlay_mgr):
        """_render_badge delegates to the working glyph for the sentinel
        number, and still renders a numeral for a real number."""
        mgr, mod, _ = overlay_mgr
        sentinel_img = QImage(3, 3, QImage.Format.Format_ARGB32_Premultiplied)
        with patch.object(
            mgr, "_render_working_glyph", return_value=sentinel_img
        ) as glyph_mock:
            out = mgr._render_badge(mod.WORKING_BADGE_NUMBER, width=40, height=40)
        glyph_mock.assert_called_once_with(40, 40, 1.0)
        assert out is sentinel_img
        # A real number does NOT route to the glyph.
        with patch.object(mgr, "_render_working_glyph") as glyph_mock2:
            mgr._render_badge(7, width=40, height=40)
        glyph_mock2.assert_not_called()

    def test_paint_working_badge_centers_summary_on_point_and_paints(
        self, overlay_mgr
    ):
        """paint_working_badge builds a one-item summary centered on the
        given physical screen point, carrying the working sentinel, and runs
        the normal paint pipeline to a 'painted' result."""
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(5005)
        captured: dict = {}

        def _cap_surface(_monitor, badges, _bbox):
            captured["badges"] = list(badges)
            return object()

        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_cap_surface
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.return_value = _paint_rect(5005, mon)
            result = mgr.paint_working_badge(
                500, 400, overlay_session_id=1, paint_generation=0
            )

        # Exactly one item, its bounds centered on (500, 400).
        assert resolve_mock.call_count == 1
        bounds = resolve_mock.call_args.args[0]
        half = mod.WORKING_BADGE_LOGICAL_PX // 2
        assert bounds == (500 - half, 400 - half, 500 + half, 400 + half)
        # The badge carries the working sentinel (renders the glyph).
        assert [n for _, n in captured["badges"]] == [mod.WORKING_BADGE_NUMBER]
        assert result["state"] == "painted"

    def test_working_badge_off_monitor_near_edge_paints_nothing(
        self, overlay_mgr
    ):
        """A working-badge center point off EVERY monitor must paint nothing,
        even when the center sits within half a badge of a monitor edge.

        The badge box is ``WORKING_BADGE_LOGICAL_PX * dpr`` wide centered on
        the point, so a center just past a monitor edge (e.g. x=-10) builds a
        box that still OVERLAPS that monitor. The real resolver returns a
        valid rect for an overlapping box, so the old code painted a CLIPPED
        working glyph and reported a painted monitor for an off-screen / stale
        point (wh-overlay-4bug-review-r2.1). paint_working_badge must detect
        that the center is on no monitor and emit the normal no-monitor
        'painted' event instead of constructing a badge box.

        Uses the REAL resolver (not a mock) so the overlap is genuine: with a
        64-logical badge at dpr 1.0 the box spans x=-42..22, overlapping the
        monitor's x=0..22.
        """
        mgr, mod, _ = overlay_mgr
        mon = _NativeMonitor(
            hmonitor=8008, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            result = mgr.paint_working_badge(
                -10, 540, overlay_session_id=1, paint_generation=0
            )

        assert result["state"] == "painted"
        assert result["monitor_ids"] == []
        assert mgr._windows == {}

    def test_paint_working_badge_then_clear_tears_down_window(self, overlay_mgr):
        """A painted working badge leaves a live overlay window; clear()
        destroys it."""
        mgr, mod, _ = overlay_mgr
        mon = _native_monitor(6006)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect", return_value=_paint_rect(6006, mon)
        ), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            mgr.paint_working_badge(
                500, 400, overlay_session_id=1, paint_generation=0
            )
            assert mgr._windows, "expected a live overlay window after paint"
            cleared = mgr.clear(overlay_session_id=1, paint_generation=1)
        assert cleared["state"] == "cleared"
        assert not mgr._windows

    def test_working_badge_box_scales_with_monitor_dpr(self, overlay_mgr):
        """The working badge is sized in LOGICAL pixels: paint_working_badge
        scales the physical box by the target monitor's device pixel ratio, so
        the perceived size is constant across mixed-DPI monitors instead of
        shrinking on hi-DPI (wh-dictation-retraction-indicator.11).

        On a 200% monitor (dpr 2.0) a 64-logical-px badge must build a
        128-physical-px box centered on the point, NOT a constant 64 physical.
        """
        mgr, mod, _ = overlay_mgr
        mon = _NativeMonitor(
            hmonitor=7, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert mon.dpr == 2.0
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mod, "resolve_overlay_paint_rect"
        ) as resolve_mock, patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            resolve_mock.return_value = _paint_rect(7, mon)
            mgr.paint_working_badge(
                1000, 800, overlay_session_id=1, paint_generation=0
            )
        # Physical box is logical(64) * dpr(2) = 128 px, centered on the point.
        bounds = resolve_mock.call_args.args[0]
        assert bounds == (1000 - 64, 800 - 64, 1000 + 64, 800 + 64)

    def test_working_glyph_strokes_scale_with_dpr(self, overlay_mgr):
        """The working glyph's outline and shadow scale with the device pixel
        ratio, like the numeral path, so the perceived stroke thickness is
        constant across mixed-DPI monitors (wh-glm52-proving-round.1).

        Rendered into the SAME image size, a higher dpr means a wider outline
        pen and a larger shadow offset, so strictly more opaque pixels.
        """
        mgr, mod, _ = overlay_mgr
        low = mgr._render_badge(mod.WORKING_BADGE_NUMBER, 64, 64, dpr=1.0)
        high = mgr._render_badge(mod.WORKING_BADGE_NUMBER, 64, 64, dpr=2.0)

        def _opaque(img):
            return sum(
                1
                for y in range(img.height())
                for x in range(img.width())
                if (img.pixel(x, y) >> 24) & 0xFF
            )

        assert _opaque(high) > _opaque(low), (
            "higher dpr must thicken the working glyph outline/shadow"
        )
