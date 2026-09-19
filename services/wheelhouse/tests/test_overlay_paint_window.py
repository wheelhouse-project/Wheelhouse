"""Unit tests for the numbered-overlay paint window
(``overlay_paint_window.py``, slice wh-n29v.53, source leaf wh-h7cvz1).

The module manages one transparent, always-on-top, no-activate,
click-through layered Win32 window PER monitor that currently has badges
and paints numbered speech bubbles (with pointer tails or leader lines,
wh-overlay-bubble-badges) onto on-screen controls via the existing
Qt-to-GDI per-pixel-alpha bridge. These tests mock ctypes (the
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
import math
from ctypes import wintypes
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")

from PySide6.QtGui import QColor, QImage

from ui.element_types import WalkSnapshotSummary, WalkSnapshotSummaryItem
from shared.overlay_dpi_resolver import OverlayPaintRect
from shared.monitor_geometry import _NativeMonitor
from PySide6.QtCore import QRect, QRectF


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


def _fill_badge_box(
    painter, _number, placement, _control, _dpr, _scheme, _metrics, _font,
    _mon_w_phys=None, _mon_h_phys=None,
):
    """Test double for ``_draw_numeral_bubble``: fill the badge BOX fully
    opaque, so an opaque-pixel scan locates exactly WHERE the badge landed
    (the real bubble is inset from the box and rounded, which would blur the
    placement assertions these tests pin)."""
    _bw, _bh, (bl, bt, br, bb) = placement
    painter.fillRect(
        QRectF(bl, bt, br - bl, bb - bt), QColor(255, 255, 255, 255)
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

    def test_paused_resume_repaint_passes_the_gate(self, overlay_mgr):
        # wh-overlay-slow-uia-stale-badges.17: cross-module guard. Replay the
        # REAL overlay state machine's mic-pause clear and mic-resume restore
        # paint through a REAL gate. The machine reports PAINTED after the
        # resume and routes "click N" to badges, so the restore paint MUST
        # present; a pair the gate refuses leaves the user with an empty
        # screen and a machine that believes the badges are up.
        from services.wheelhouse.click_overlay_state import (
            ClickOverlayStateMachine,
            EffectKind,
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
            PaintAckState,
        )

        mgr, mod, _ = overlay_mgr
        gate = mod.GenerationGate()
        m = ClickOverlayStateMachine()

        def _pair(machine):
            return machine.overlay_session_id, machine.paint_generation

        # closed -> walk_in_flight -> paint_in_flight -> painted.
        m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
        sid, gen = _pair(m)
        m.apply(
            OverlayEvent(
                OverlayEventKind.BUILD_RESPONSE,
                overlay_session_id=sid,
                paint_generation=gen,
                snapshot_id="snapP",
            )
        )
        # The first paint presents.
        assert gate.accept_paint(sid, gen) is True
        m.apply(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK,
                overlay_session_id=sid,
                paint_generation=gen,
                paint_state=PaintAckState.PAINTED,
            )
        )
        assert m.state is OverlayState.PAINTED

        # Mic pause: the machine's clear hides the badges.
        pause = m.apply(OverlayEvent(OverlayEventKind.MIC_PAUSE))
        clear = [
            e for e in pause.effects if e.kind is EffectKind.DISPATCH_CLEAR
        ][0]
        assert (
            gate.accept_clear(clear.overlay_session_id, clear.paint_generation)
            is True
        )

        # Mic resume with a still-valid snapshot: the restore paint must
        # actually present, because the machine now says PAINTED.
        resume = m.apply(
            OverlayEvent(OverlayEventKind.MIC_RESUME, snapshot_valid=True)
        )
        assert m.state is OverlayState.PAINTED
        paint = [
            e for e in resume.effects if e.kind is EffectKind.DISPATCH_PAINT
        ][0]
        assert (
            gate.accept_paint(paint.overlay_session_id, paint.paint_generation)
            is True
        )


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
        # _render_badge now renders only the WORKING glyph (numerals draw as
        # bubbles on the surface -- wh-overlay-bubble-badges), so the image
        # contract is pinned through the sentinel.
        mgr, mod, _ = overlay_mgr
        img = mgr._render_badge(mod.WORKING_BADGE_NUMBER, width=80, height=40)
        assert isinstance(img, QImage)
        assert img.format() == QImage.Format.Format_ARGB32_Premultiplied
        # Some non-transparent pixels were painted (the glyph / outline).
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
        # The manager carries font pt and shadow flag from construction. The
        # default is 10 pt, no shadow (user visual review 2026-08-08, down
        # from 12 pt with shadow; matches the documented defaults in
        # config.toml.example).
        assert mgr._badge_font_pt == 10
        assert mgr._badge_shadow is False
        # Constructing with explicit overrides honors them.
        import overlay_paint_window
        m2 = overlay_paint_window.OverlayPaintWindowManager(
            badge_font_pt=24, badge_shadow=True
        )
        assert m2._badge_font_pt == 24
        assert m2._badge_shadow is True

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

    def test_manager_reads_badge_theme_from_construction(self):
        # The bubble badge's color scheme (wh-bubble-theme-config). ClickConfig
        # already validates the value; the manager normalizes defensively so an
        # unexpected string falls back to "auto" instead of an unknown scheme.
        import overlay_paint_window
        m = overlay_paint_window.OverlayPaintWindowManager(badge_theme="dark")
        assert m._badge_theme == "dark"
        m2 = overlay_paint_window.OverlayPaintWindowManager(badge_theme="nonsense")
        assert m2._badge_theme == "auto"

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

        # Pin the numeral size to 24x24 so the bbox math and the box fill agree.
        with patch.object(mgr, "_numeral_badge_size", return_value=(24, 24)):
            bbox = mgr._compute_monitor_bbox(monitor, badges)
            with patch.object(
                mgr, "_draw_numeral_bubble", side_effect=_fill_badge_box
            ):
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

        badges = [(rect, 7)]
        # The bbox is computed on the LOGICAL rect (width 100/height 40), and
        # the opaque PAINT footprint (the box fill below) is the badge box the
        # placement pass produced. The bbox bounds the full logical rect:
        # physical footprint left=200*1.5=300, top=300*1.5=450,
        # right=300*1.5=450, bottom=510; margin=5*1.5=7.5;
        # min_x=floor(300-7.5)=292, min_y=floor(450-7.5)=442;
        # max_x=ceil(450+7.5)=458, max_y=ceil(510+7.5)=518.
        bbox = mgr._compute_monitor_bbox(monitor, badges)
        assert bbox.offset_x == 292
        assert bbox.offset_y == 442
        assert bbox.width == 458 - 292  # 166
        assert bbox.height == 518 - 442  # 76

        # Fill the badge box fully opaque so the opaque-pixel scan pins WHERE
        # the badge landed, independent of the bubble's inset/rounding.
        with patch.object(
            mgr, "_draw_numeral_bubble", side_effect=_fill_badge_box
        ):
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
            mgr, "_draw_numeral_bubble"
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
        """At dpr 2.0 each numeral bubble is drawn at the PHYSICAL badge-box
        geometry (logical * dpr) with the dpr forwarded (which scales the
        font, pens, and shadow), so its edges are sharp rather than an
        enlarged logical-resolution drawing."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = _paint_rect(10, monitor, x=400, y=250, width=60, height=24)
        badges = [(rect, 3)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(
            _painter, _number, placement, _control, dpr, _scheme, _metrics,
            _font, _mon_w_phys=None, _mon_h_phys=None,
        ):
            bw, bh, _footprint = placement
            seen.append((bw, bh, dpr))

        with patch.object(mgr, "_draw_numeral_bubble", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        # Tight physical badge box (dpr 2.0 forwarded), sized to the glyph --
        # NOT the 120x48 control physical size (wh-overlay-badge-alloc-decouple).
        assert seen == [(*mgr._numeral_badge_size(3, 2.0), 2.0)]

    def test_numeral_badge_call_is_tight_physical_at_dpr_1(self, overlay_mgr):
        """At dpr 1.0 the numeral bubble is drawn in a tight numeral-sized
        badge box (from _numeral_badge_size) with dpr 1.0 forwarded --
        decoupled from the control size (wh-overlay-badge-alloc-decouple)."""
        mgr, mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        assert monitor.dpr == 1.0
        rect = _paint_rect(10, monitor, x=100, y=200, width=80, height=30)
        badges = [(rect, 5)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(
            _painter, _number, placement, _control, dpr, _scheme, _metrics,
            _font, _mon_w_phys=None, _mon_h_phys=None,
        ):
            bw, bh, _footprint = placement
            seen.append((bw, bh, dpr))

        with patch.object(mgr, "_draw_numeral_bubble", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert seen == [(*mgr._numeral_badge_size(5, 1.0), 1.0)]

    def test_working_badge_default_logical_size_is_halved_from_64(self, overlay_mgr):
        """The working hourglass default size was halved from 64 to 32
        logical px at the user's request. The outline width was retuned in
        the same change (test_working_glyph_not_swallowed_by_outline) so this
        smaller size does not reproduce the "36 read as a shrunken blob"
        finding from wh-dictation-retraction-indicator.11 -- that finding
        used the same fixed 3px outline this change replaces."""
        _mgr, mod, _ = overlay_mgr
        assert mod.WORKING_BADGE_LOGICAL_PX == 32

    def test_working_badge_width_is_about_30_percent_less_than_height(
        self, overlay_mgr
    ):
        """A live check of the halved 32x32 box found it too wide even though
        the 32px vertical size read fine, so the width was narrowed to ~70%
        of the height (user request: 'make the horizontal size about 30%
        less') instead of matching it."""
        _mgr, mod, _ = overlay_mgr
        ratio = mod.WORKING_BADGE_WIDTH_LOGICAL_PX / mod.WORKING_BADGE_LOGICAL_PX
        assert 0.65 <= ratio <= 0.75, (
            f"width/height ratio {ratio:.2f} is not ~30% narrower than tall"
        )

    def test_working_glyph_not_swallowed_by_outline(self, overlay_mgr):
        """The hourglass must render as a legible white shape at the default
        size, not a near-solid black blob.

        Mirrors test_numeral_not_swallowed_by_outline: the outline is stroked
        centered on the glyph path edge, so a pen too wide for the shape's
        stroke thickness eats the white fill. WORKING_BADGE_LOGICAL_PX was
        halved (64 -> 32); the old fixed 3px outline at 32 is proportionally
        heavier than the 3px outline already found to read as a blob at 36
        (wh-dictation-retraction-indicator.11), so this asserts the retuned
        outline (_HOURGLASS_OUTLINE_PX) keeps the shape legible at the actual
        shipped box (WORKING_BADGE_WIDTH_LOGICAL_PX x WORKING_BADGE_LOGICAL_PX,
        narrower than tall).
        """
        mgr, mod, _ = overlay_mgr
        width = mod.WORKING_BADGE_WIDTH_LOGICAL_PX
        height = mod.WORKING_BADGE_LOGICAL_PX
        img = mgr._render_working_glyph(width, height)

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
        assert opaque > 0, "hourglass drew nothing"
        assert ratio >= 0.10, (
            f"hourglass collapsed to a near-solid blob (white fill {ratio:.2f} "
            "of opaque pixels) -- the outline pen is too heavy for the shape"
        )

    def test_working_glyph_draws_shape_not_box_at_default_size(self, overlay_mgr):
        """At the default (narrower-than-tall) box the glyph is a recognizable
        hourglass: the four corners are transparent (not a filled box) and the
        vertical center line (the waist) has opaque pixels."""
        mgr, mod, _ = overlay_mgr
        width = mod.WORKING_BADGE_WIDTH_LOGICAL_PX
        height = mod.WORKING_BADGE_LOGICAL_PX
        img = mgr._render_working_glyph(width, height)
        assert img.width() == width and img.height() == height
        for cx, cy in [
            (0, 0),
            (width - 1, 0),
            (0, height - 1),
            (width - 1, height - 1),
        ]:
            assert ((img.pixel(cx, cy) >> 24) & 0xFF) == 0, "corner not transparent"
        midx = width // 2
        waist_opaque = any(
            (img.pixel(midx, y) >> 24) & 0xFF for y in range(height)
        )
        assert waist_opaque, "expected opaque pixels along the glyph waist"


class TestNumeralBadgeSizeDecoupled:
    """wh-overlay-badge-alloc-decouple -- a numeral's badge box is sized to
    the NUMERAL (font metrics + bubble padding + decoration margin), NOT to
    the control. A number over a large control never involves a control-sized
    (dpr^2) allocation -- since wh-overlay-bubble-badges there is no per-badge
    allocation at all: the bubble path draws directly on the surface inside
    the badge box. The working glyph (which fills its box) is not affected."""

    def test_numeral_box_is_tight_not_control_sized(self, overlay_mgr):
        """For a large control on a hi-DPI monitor, the bubble is drawn in a
        small numeral-sized badge box, NOT at the control's physical size
        (2000x400 here)."""
        mgr, _mod, _ = overlay_mgr
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=192
        )
        assert monitor.dpr == 2.0
        rect = _paint_rect(10, monitor, x=100, y=100, width=1000, height=200)
        badges = [(rect, 5)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)

        seen = []

        def _spy(
            _painter, _number, placement, _control, dpr, _scheme, _metrics,
            _font, _mon_w_phys=None, _mon_h_phys=None,
        ):
            bw, bh, _footprint = placement
            seen.append((bw, bh, dpr))

        with patch.object(mgr, "_draw_numeral_bubble", side_effect=_spy):
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert len(seen) == 1
        bw, bh, bdpr = seen[0]
        # Still physical-resolution: the dpr is forwarded so the glyph is sharp.
        assert bdpr == 2.0
        # The box is tight (a single bold numeral's bubble), far smaller than
        # the 2000x400 control physical size.
        assert bw < 200 and bh < 200, f"badge box not decoupled: {bw}x{bh}"
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

        # Fill the badge box fully opaque so its top-left is unambiguous: the
        # scan pins WHERE the badge landed, independent of the bubble inset.
        with patch.object(
            mgr, "_draw_numeral_bubble", side_effect=_fill_badge_box
        ):
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

    def test_numeral_bubble_fits_in_tight_badge_box(self, overlay_mgr):
        """The badge box from ``_numeral_badge_size`` actually CONTAINS the
        drawn bubble: composing a surface leaves every opaque pixel strictly
        inside the box footprint, so no bubble, border, shadow, or digit is
        clipped.

        This is the real fit contract. The size-equality tests above compare
        the placement box against ``_numeral_badge_size``'s own output, so a
        bug INSIDE ``_numeral_badge_size`` (too-small margin, capHeight
        under-measuring, wrong dpr factor) would size every box wrong AND
        still match itself, passing CI while bubbles clip on screen. This test
        draws the real bubble and fails if the opaque ink reaches the box edge
        (wh-overlay-4bug-review.1). The corner is pinned INSIDE a large
        control so the state is "overlap". An overlapping bubble draws an
        inward pointer tail toward the control's center (user decision
        2026-08-07), which by design extends past the box on the control's
        side by (_BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX - the 5.25 box margin)
        logical px; here the control center lies down-right of the badge, so
        only the right/bottom bounds get that allowance. The strict top/left
        bounds still catch a clipped bubble, border, shadow, or digit."""
        mgr, mod, _ = overlay_mgr
        tail_overhang_logical = mod._BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX - 5.25
        assert tail_overhang_logical > 0
        mgr._badge_corner = "top_left"
        mgr._badge_trailing_space = False
        for number in (1, 7, 10, 100):
            for dpi, dpr in ((96, 1.0), (192, 2.0)):
                monitor = _NativeMonitor(
                    hmonitor=10, rect_phys=QRect(0, 0, 3840, 2160), dpi=dpi
                )
                assert monitor.dpr == dpr
                rect = _paint_rect(
                    10, monitor, x=100, y=100, width=400, height=200
                )
                badges = [(rect, number)]
                bbox = mgr._compute_monitor_bbox(monitor, badges)
                placements = mgr._numeral_badge_placements_phys(
                    badges, dpr, 3840.0, 2160.0, corner="top_left"
                )
                assert placements[0] is not None
                bw, bh, (fl, ft, fr, fb) = placements[0]
                assert (bw, bh) == mgr._numeral_badge_size(number, dpr)
                surface = mgr._render_monitor_surface(monitor, badges, bbox)
                xs: list[float] = []
                ys: list[float] = []
                for y in range(surface.height()):
                    for x in range(surface.width()):
                        if ((surface.pixel(x, y) >> 24) & 0xFF) >= 128:
                            xs.append(x + bbox.offset_x)
                            ys.append(y + bbox.offset_y)
                assert xs, f"numeral {number} at dpr {dpr} drew nothing"
                # Opaque ink must stay strictly inside the badge box: a
                # clipped bubble would push ink onto the box's first/last row
                # or column.
                assert min(xs) >= fl + 1 and min(ys) >= ft + 1, (
                    f"numeral {number} at dpr {dpr} ink touches the box "
                    f"top/left edge (min x={min(xs)}, y={min(ys)}; box "
                    f"({fl}, {ft})-({fr}, {fb})) -- badge box too small"
                )
                # The inward tail may exceed the box toward the control
                # (down-right here) by the overhang, plus 1 px antialias.
                allow = math.ceil(tail_overhang_logical * dpr) + 1
                assert max(xs) <= fr + allow and max(ys) <= fb + allow, (
                    f"numeral {number} at dpr {dpr} ink exceeds the box "
                    f"bottom/right edge beyond the tail overhang "
                    f"(max x={max(xs)}, y={max(ys)}; box "
                    f"({fl}, {ft})-({fr}, {fb}); allow={allow}) -- badge "
                    f"box too small"
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

    def test_numeral_font_built_once_per_surface(self, overlay_mgr):
        """The numeral QFont is built ONCE per surface render and shared with
        every badge's digit drawing, not rebuilt per badge.

        The metrics hoist (wh-overlay-4bug-review.2) shared the QFontMetricsF
        across badges, but ``_draw_numeral_bubble`` still called
        ``_numeral_font(dpr)`` for each digit's ``addText``, so a dense
        show-numbers paint constructed one identical QFont per badge (the
        font's only variable, dpr, is constant per surface).
        ``_render_monitor_surface`` now builds the font once and passes it
        down alongside the metrics (wh-overlay-bubble-badges.1.1)."""
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
            mgr, "_numeral_font", wraps=mgr._numeral_font
        ) as font_mock:
            mgr._render_monitor_surface(monitor, badges, bbox)

        assert font_mock.call_count == 1, (
            f"_numeral_font called {font_mock.call_count} times for 3 badges; "
            "the numeral font should be built once per surface and shared"
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

        with patch.object(mgr, "_draw_numeral_bubble"):
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
        # 100x80 logical = 200x160 physical: comfortably more than TWICE the
        # bubble badge box (about 70x70 physical at dpr 2) on both axes, so the
        # small-control corner-point rule does not fire and the corner anchor
        # under test is exercised. (The pre-bubble fixture was 60x24 logical;
        # the bubble box reclassified it as small -- wh-bubble-geometry-fns.)
        rect = _paint_rect(10, monitor, x=400, y=250, width=100, height=80)
        badges = [(rect, 3)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)
        # Physical control top-left = (400*2, 250*2) = (800, 500); margin=5*2=10.
        # The numeral "3" bubble is anchored to that corner and is smaller than
        # the 200x160 control on both axes, so the badge footprint stays inside
        # the control box and the bbox is the control box minus the margin:
        # offset = (floor(800-10), floor(500-10)) = (790, 490).
        assert (bbox.offset_x, bbox.offset_y) == (790, 490)

        with patch.object(
            mgr, "_draw_numeral_bubble", side_effect=_fill_badge_box
        ):
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
        # Pin inside placement so the footprint call below (which takes only
        # the corner) mirrors the placement the bbox actually used.
        mgr._badge_trailing_space = False
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        # Control straddling the top-left corner: logical (-30, -20) size 100x50.
        rect = _paint_rect(10, monitor, x=-30, y=-20, width=100, height=50)
        badges = [(rect, 1)]

        bbox = mgr._compute_monitor_bbox(monitor, badges)

        # The clamp under test: control left/top -30/-20 clamps to 0, so the
        # surface starts AT the monitor's top-left.
        assert (bbox.offset_x, bbox.offset_y) == (0, 0)
        # Width/height are the union of the control box (right=70, bottom=30)
        # and the badge's ACTUAL placed footprint (the monitor-edge shift moves
        # the overhanging badge fully on-monitor, and the bubble box may extend
        # past the control's bottom), plus the margin. Derived from the same
        # footprint helper the bbox path uses so the expectation cannot encode
        # a stale badge size (wh-bubble-geometry-fns).
        bw, bh = mgr._numeral_badge_size(1, 1.0)
        _bl, _bt, br, bb = mgr._numeral_badge_footprint_phys(
            rect, bw, bh, 1.0, 1920, 1080, corner="top_left"
        )
        margin = mod._SURFACE_MARGIN_PX
        assert (bbox.width, bbox.height) == (
            int(max(70.0, br) + margin), int(max(30.0, bb) + margin),
        )
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
            with patch.object(
                mgr, "_draw_numeral_bubble", side_effect=_fill_badge_box
            ):
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
    badges already placed, searching rings of eight directions at one, two,
    and three badge-size steps out when the corner collides
    (wh-overlay-bubble-badges.4), and only a pathological pile-up (every
    candidate occupied) accepts the overlap rather than dropping the number.
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

    def test_pileup_deeper_than_every_ring_still_never_drops(self, overlay_mgr):
        # wh-overlay-bubble-badges.4: forty identical stacked controls share
        # one base anchor and 24 ring candidates, so the candidates genuinely
        # run out. Every badge past that point must still get a placement
        # (the base anchor, overlap accepted) rather than raising or being
        # dropped.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 500, 500, 30, 40), n) for n in range(1, 41)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        assert len(placements) == 40
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
        # neighbouring control is still better than stacking on a badge. A
        # full-window canvas control (a Chromium Document fills its whole
        # window) makes every candidate at every distance control-occupied.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        own_box = (100.0, 200.0, 400.0, 240.0)
        neighbour_right = (400.0, 200.0, 500.0, 240.0)
        canvas = (0.0, 0.0, 1920.0, 1080.0)
        placed = [
            (380.0, 200.0, 400.0, 230.0),
            (357.0, 200.0, 377.0, 230.0),
        ]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080,
            [own_box, neighbour_right, canvas],
            corner="top_right", placed_badges=placed,
        )
        # First badge-free candidate in nudge order: below.
        assert (got[0], got[1]) == (380.0, 233.0)
        assert not any(
            mgr._rects_overlap_phys(got, p) for p in placed
        )

    def test_ring_two_spot_found_when_ring_one_is_full(self, overlay_mgr):
        # wh-overlay-bubble-badges.4: badges already occupy the base anchor
        # and every ring-1 candidate (all eight directions). The search must
        # continue to ring 2 and return the ring-2 inward spot: two badge
        # widths plus two 3-px gaps left of the base corner.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        neighbour_right = (400.0, 200.0, 500.0, 240.0)   # blocks trailing strip
        placed = [
            (380.0, 200.0, 400.0, 230.0),   # base
            (357.0, 200.0, 377.0, 230.0),   # inward
            (380.0, 233.0, 400.0, 263.0),   # below
            (380.0, 167.0, 400.0, 197.0),   # above
            (403.0, 200.0, 423.0, 230.0),   # outward
            (357.0, 233.0, 377.0, 263.0),   # inward+below
            (357.0, 167.0, 377.0, 197.0),   # inward+above
            (403.0, 233.0, 423.0, 263.0),   # outward+below
            (403.0, 167.0, 423.0, 197.0),   # outward+above
        ]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [neighbour_right],
            corner="top_right", placed_badges=placed,
        )
        assert (got[0], got[1]) == (334.0, 200.0)
        assert all(
            not mgr._rects_overlap_phys(got, p) for p in placed
        )

    def test_diagonal_spot_found_when_straight_nudges_are_full(
        self, overlay_mgr
    ):
        # wh-overlay-bubble-badges.4: the base anchor and the four straight
        # ring-1 candidates are taken, but the first diagonal (inward+below)
        # is free. The search must return it rather than jumping to ring 2.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        rect = self._rect(mon, 100, 200, 300, 40)
        neighbour_right = (400.0, 200.0, 500.0, 240.0)   # blocks trailing strip
        placed = [
            (380.0, 200.0, 400.0, 230.0),   # base
            (357.0, 200.0, 377.0, 230.0),   # inward
            (380.0, 233.0, 400.0, 263.0),   # below
            (380.0, 167.0, 400.0, 197.0),   # above
            (403.0, 200.0, 423.0, 230.0),   # outward
        ]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [neighbour_right],
            corner="top_right", placed_badges=placed,
        )
        assert (got[0], got[1]) == (357.0, 233.0)
        assert all(
            not mgr._rects_overlap_phys(got, p) for p in placed
        )

    def test_identical_pileup_separates_all_badges(self, overlay_mgr):
        # wh-overlay-bubble-badges.4: five identical stacked controls used to
        # exhaust the three-candidate list, and the fifth digit stacked on the
        # first. With the ring search every badge finds its own spot.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [(self._rect(mon, 500, 500, 30, 40), n) for n in range(1, 6)]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert len(rects) == 5
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)

    def test_corner_pileup_separates_all_badges_on_monitor(self, overlay_mgr):
        # wh-overlay-bubble-badges.4, the live-screenshot shape: a pile-up at
        # the top-right MONITOR corner, where the above and outward candidates
        # fall off the monitor. Every badge must still get its own on-monitor
        # spot.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [(self._rect(mon, 1890, 0, 30, 40), n) for n in range(1, 6)]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert len(rects) == 5
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        for left, top, right, bottom in rects:
            assert left >= 0.0 and top >= 0.0
            assert right <= 1920.0 and bottom <= 1080.0

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


class TestEdgeClusterColumn:
    """Small controls packed against a vertical monitor edge get their badges
    laid out as ONE single-file column beside the cluster, in the same
    top-to-bottom order as the controls (wh-taskbar-badge-mispoint, option 1).

    The live trigger: the tray corner of a vertical right-edge taskbar. The
    icons are packed tighter than one badge height, so the collision rings
    scattered the badges up and left in ring order -- not target order -- and
    the leader lines crossed into an unreadable tangle. A column beside the
    cluster keeps every leader line short and parallel; crossings are
    impossible because badge order matches target order by construction.

    The column triggers ONLY for a packed cluster: at least three controls,
    each small and lying entirely within a narrow band at the monitor edge,
    stacked with more badges than fit single-file in the cluster's own
    vertical extent. Everything else keeps the existing placement unchanged.
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

    @staticmethod
    def _manual_sequential(mgr, badges, bw, bh, corner="top_right"):
        """The pre-cluster sequential placement, computed control by control:
        the expected output wherever the cluster layout must NOT trigger."""
        ctrl = [
            (r.x, r.y, r.x + r.width, r.y + r.height) for r, _n in badges
        ]
        placed = []
        out = []
        for rect, _n in badges:
            fp = mgr._numeral_badge_placement_phys(
                rect, bw, bh, 1.0, 1920, 1080, ctrl,
                corner=corner, placed_badges=placed,
            )
            placed.append(fp)
            out.append((bw, bh, fp))
        return out

    def test_packed_tray_corner_forms_ordered_column(self, overlay_mgr):
        # The screenshot profile: six 24x24 tray icons packed two-across in
        # the bottom-right corner, inside a 60px-wide vertical taskbar flush
        # against the right monitor edge. Badge 20x30: the 24px row pitch
        # cannot fit 30px badges, so the old rings tangled them.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = []
        n = 1
        for top in (990, 1014, 1038):
            for left in (1864, 1892):
                badges.append((self._rect(mon, left, top, 24, 24), n))
                n += 1
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # One column: every badge shares the same right edge, fully LEFT of
        # the cluster's leftmost control edge (1864).
        assert len({r[2] for r in rects}) == 1
        assert all(r[2] <= 1864.0 for r in rects)
        # Same top-to-bottom order as the targets; rows first. Within one
        # two-across row the FARTHER icon's badge goes higher (see
        # test_same_row_pair_lines_nest_instead_of_crossing), so the target
        # order for a right-edge column is (top, -left). Distinct rows must
        # still come out strictly top-to-bottom -- this is what makes
        # crossing leader lines impossible between rows.
        tops = [r[1] for r in rects]
        assert len(set(tops)) == len(tops)
        row_tops = [min(tops[i], tops[i + 1]) for i in range(0, 6, 2)]
        assert row_tops == sorted(row_tops)
        # Every badge of an earlier row sits above every badge of a later row.
        assert max(tops[0:2]) < min(tops[2:4])
        assert max(tops[2:4]) < min(tops[4:6])
        # Disjoint and fully on the monitor.
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        for left, top, right, bottom in rects:
            assert left >= 0.0 and top >= 0.0
            assert right <= 1920.0 and bottom <= 1080.0

    def test_left_edge_cluster_mirrors_to_right_column(self, overlay_mgr):
        # Three 24x24 icons single-file against the LEFT edge, 24px pitch:
        # the column goes just RIGHT of the cluster. Exact positions: column
        # left edge on the cluster's right edge plus the 3px gap (28 + 3);
        # each badge wants its control's vertical center (tops 297/321/345),
        # and the 24px pitch forces the minimal downward shifts to 330/363.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 4, 300, 24, 24), 1),
            (self._rect(mon, 4, 324, 24, 24), 2),
            (self._rect(mon, 4, 348, 24, 24), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert rects == [
            (31.0, 297.0, 51.0, 327.0),
            (31.0, 330.0, 51.0, 360.0),
            (31.0, 363.0, 51.0, 393.0),
        ]

    def test_bottom_corner_column_shifts_up_onto_monitor(self, overlay_mgr):
        # The tray cluster sits at the very bottom: the column cannot extend
        # below the monitor, so the whole run shifts up while preserving
        # order and separation.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1890, 1008, 24, 24), 1),
            (self._rect(mon, 1890, 1032, 24, 24), 2),
            (self._rect(mon, 1890, 1056, 24, 24), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # Still one column beside the cluster, in target order.
        assert len({r[2] for r in rects}) == 1
        assert all(r[2] <= 1890.0 for r in rects)
        tops = [r[1] for r in rects]
        assert tops == sorted(tops)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        for left, top, right, bottom in rects:
            assert top >= 0.0 and bottom <= 1080.0

    def test_spaced_edge_run_keeps_existing_placement(self, overlay_mgr):
        # Four full-width taskbar rows at 40px pitch: a genuine edge run,
        # but NOT packed (four 33px badge slots fit in the 156px extent), so
        # the shipped placement must be byte-identical.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1860, 200, 60, 36), 1),
            (self._rect(mon, 1860, 240, 60, 36), 2),
            (self._rect(mon, 1860, 280, 60, 36), 3),
            (self._rect(mon, 1860, 320, 60, 36), 4),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
            expected = self._manual_sequential(mgr, badges, 20, 30)
        assert placements == expected

    def test_wide_rows_at_edge_never_cluster(self, overlay_mgr):
        # Nav-pane-style rows: packed 30px pitch and flush to the left edge,
        # but 250px wide -- far wider than the edge band -- so the trailing
        # -space placement they get today must be unchanged.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 0, 100 + 30 * i, 250, 30), i + 1)
            for i in range(4)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
            expected = self._manual_sequential(mgr, badges, 20, 30)
        assert placements == expected

    def test_two_member_stack_never_clusters(self, overlay_mgr):
        # Two packed tray icons: below the three-member minimum, the
        # collision nudge already handles a pair unambiguously.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1890, 990, 24, 24), 1),
            (self._rect(mon, 1890, 1014, 24, 24), 2),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
            expected = self._manual_sequential(mgr, badges, 20, 30)
        assert placements == expected

    def test_same_row_pair_lines_nest_instead_of_crossing(self, overlay_mgr):
        # Two icons side by side in one row of a right-edge cluster (the
        # live tray packs CPU left, keyboard right). The column sits LEFT of
        # the cluster, so if the higher badge points at the NEARER (left)
        # icon, the lower badge's line to the farther (right) icon swaps
        # over it -- the lines cross. The farther icon's badge must go
        # higher so the two lines nest. Third row keeps the run packed.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1864, 990, 24, 24), 1),    # row 1, LEFT icon
            (self._rect(mon, 1892, 990, 24, 24), 2),    # row 1, RIGHT icon
            (self._rect(mon, 1864, 1014, 24, 24), 3),   # row 2, left
            (self._rect(mon, 1892, 1014, 24, 24), 4),   # row 2, right
            (self._rect(mon, 1864, 1038, 24, 24), 5),   # row 3, left
            (self._rect(mon, 1892, 1038, 24, 24), 6),   # row 3, right
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # Within every row: the RIGHT icon's badge is strictly higher than
        # the LEFT icon's badge.
        assert rects[1][1] < rects[0][1]
        assert rects[3][1] < rects[2][1]
        assert rects[5][1] < rects[4][1]

    def test_left_edge_same_row_pair_keeps_left_first(self, overlay_mgr):
        # Mirror case: a LEFT-edge cluster's column sits RIGHT of the
        # cluster, so the farther icon is the LEFT one and plain (top,
        # left) order already nests the lines. Pin it so the right-edge
        # fix cannot flip this side too.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 4, 300, 24, 24), 1),    # row 1, LEFT icon
            (self._rect(mon, 32, 300, 24, 24), 2),   # row 1, RIGHT icon
            (self._rect(mon, 4, 324, 24, 24), 3),
            (self._rect(mon, 32, 324, 24, 24), 4),
            (self._rect(mon, 4, 348, 24, 24), 5),
            (self._rect(mon, 32, 348, 24, 24), 6),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # Within every row: the LEFT icon's badge is strictly higher.
        assert rects[0][1] < rects[1][1]
        assert rects[2][1] < rects[3][1]
        assert rects[4][1] < rects[5][1]

    def test_packed_bottom_tray_row_forms_ordered_row(self, overlay_mgr):
        # The horizontal-taskbar curve ball: six 24x24 tray icons in one
        # packed row against the BOTTOM monitor edge. Badges (30 wide) are
        # wider than the 24px icon pitch, so the old rings tangled them.
        # They must come out as ONE row just above the cluster, left to
        # right in target order.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1700 + 24 * i, 1044, 24, 24), i + 1)
            for i in range(6)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(30, 24)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # One row: every badge shares the same bottom edge, fully ABOVE the
        # cluster's top edge (1044).
        assert len({r[3] for r in rects}) == 1
        assert all(r[3] <= 1044.0 for r in rects)
        # Left-to-right target order; distinct, disjoint, on the monitor.
        lefts = [r[0] for r in rects]
        assert lefts == sorted(lefts)
        assert len(set(lefts)) == len(lefts)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        for left, top, right, bottom in rects:
            assert left >= 0.0 and top >= 0.0
            assert right <= 1920.0 and bottom <= 1080.0

    def test_row_overflow_extends_away_from_the_corner(self, overlay_mgr):
        # A packed run cannot give every badge its control's center, so
        # the overflow must extend somewhere. It must extend AWAY from the
        # nearer monitor corner: the live 2026-08-08 screenshot showed the
        # tray row's overflow drifting rightward into the corner region
        # where the badges of cluster-excluded controls (the wide clock,
        # the bell + Show-desktop pair) also sit, and the two groups'
        # leader lines crossed. Six 24px icons ending 56px short of the
        # right edge need 198px of badges; the extra must go LEFT, over
        # the open space, keeping every badge right edge near the run.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1720 + 24 * i, 1044, 24, 24), i + 1)
            for i in range(6)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(30, 24)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert len({r[3] for r in rects}) == 1
        assert all(r[3] <= 1044.0 for r in rects)
        lefts = [r[0] for r in rects]
        assert lefts == sorted(lefts)
        # No badge may extend more than one badge width past the last
        # icon's right edge (1864): the corner region stays free.
        assert max(r[2] for r in rects) <= 1894.0
        # The overflow went leftward past the first icon's ideal spot.
        assert min(r[0] for r in rects) < 1717.0
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)

    def test_bottom_right_corner_row_shifts_left_onto_monitor(
        self, overlay_mgr
    ):
        # The row cannot extend past the right monitor edge, so the whole
        # run shifts left while preserving order and separation.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1820 + 24 * i, 1044, 24, 24), i + 1)
            for i in range(4)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(30, 24)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert len({r[3] for r in rects}) == 1
        assert all(r[3] <= 1044.0 for r in rects)
        lefts = [r[0] for r in rects]
        assert lefts == sorted(lefts)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert _rects_disjoint(rects[i], rects[j]), (i, j, rects)
        for left, top, right, bottom in rects:
            assert right <= 1920.0

    def test_bottom_stacked_pair_lines_nest_instead_of_crossing(
        self, overlay_mgr
    ):
        # Mirror of the two-across vertical case: icons stacked two-HIGH in
        # a bottom-edge row. The badge row sits ABOVE the cluster, so the
        # farther (lower) icon's badge must come first (left) for the
        # pair's leader lines to nest.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = []
        n = 1
        for left in (1700, 1724, 1748):
            for top in (1032, 1056):
                badges.append((self._rect(mon, left, top, 24, 24), n))
                n += 1
        with patch.object(mgr, "_numeral_badge_size", return_value=(30, 24)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        # Input order per column is (top icon, bottom icon): the BOTTOM
        # icon's badge must sit strictly left of the TOP icon's badge.
        assert rects[1][0] < rects[0][0]
        assert rects[3][0] < rects[2][0]
        assert rects[5][0] < rects[4][0]

    def test_wide_bottom_buttons_never_cluster(self, overlay_mgr):
        # Labeled app buttons on a horizontal taskbar: flush to the bottom
        # edge and side by side, but far wider than the width cap, so the
        # shipped placement must be byte-identical.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 100 + 150 * i, 1040, 146, 40), i + 1)
            for i in range(4)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
            expected = self._manual_sequential(mgr, badges, 20, 30)
        assert placements == expected

    def test_corner_row_never_mistaken_for_vertical_column(self, overlay_mgr):
        # A packed horizontal row whose right end reaches into the
        # right-edge band: the right-edge (vertical) pass sees three
        # same-top members, but a run WIDER than it is tall must not form
        # a vertical column -- the bottom-edge pass owns it. The badges
        # must come out as one row above the icons.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        badges = [
            (self._rect(mon, 1848 + 24 * i, 1044, 24, 24), i + 1)
            for i in range(3)
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(30, 24)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        rects = [fp for _w, _h, fp in placements]
        assert len({r[3] for r in rects}) == 1
        assert all(r[3] <= 1044.0 for r in rects)
        lefts = [r[0] for r in rects]
        assert lefts == sorted(lefts)

    def test_working_glyph_never_joins_a_cluster(self, overlay_mgr):
        # A working glyph inside the edge band neither gets a numeral
        # placement nor counts toward cluster membership.
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        import overlay_paint_window as opw
        badges = [
            (self._rect(mon, 1890, 950, 24, 24), opw.WORKING_BADGE_NUMBER),
            (self._rect(mon, 1890, 990, 24, 24), 1),
            (self._rect(mon, 1890, 1014, 24, 24), 2),
            (self._rect(mon, 1890, 1038, 24, 24), 3),
        ]
        with patch.object(mgr, "_numeral_badge_size", return_value=(20, 30)):
            placements = mgr._numeral_badge_placements_phys(
                badges, 1.0, 1920, 1080, corner="top_right",
            )
        assert placements[0] is None
        rects = [fp for _w, _h, fp in placements[1:]]
        # The three numerals still form the ordered column beside the icons.
        assert len({r[2] for r in rects}) == 1
        assert all(r[2] <= 1890.0 for r in rects)
        tops = [r[1] for r in rects]
        assert tops == sorted(tops)


class TestBasePlacementAvoidsNeighborControls:
    """The BASE placement (not just a collision nudge) must not sit on a
    NEIGHBORING numbered control (wh-taskbar-badge-mispoint).

    The live trigger is a vertical right-edge taskbar: every button reports
    a full-row rectangle, the half-outside corner-point badge of one row
    overhangs the row above (covering its icon), and the monitor clamp
    pushes the bottom "Show desktop" sliver's badge up onto the
    notification bell. When the canonical spot intrudes on another numbered
    control, the badge moves to the LEADING gutter beside the control
    (fully outside, covering nothing), or -- for a small control -- the
    fully-inside corner, before accepting the intrusion. A canonical spot
    clean of other controls is unchanged (the existing
    TestSmallControlCornerPoint expectations pin that)."""

    @staticmethod
    def _mon() -> _NativeMonitor:
        return _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)

    @staticmethod
    def _rect(mon, x, y, width, height) -> OverlayPaintRect:
        return OverlayPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=mon, hmonitor=1, screen=None,
        )

    def test_small_control_moves_to_gutter_when_corner_point_intrudes(
        self, overlay_mgr
    ):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # Vertical-taskbar shape at the monitor's right edge: two stacked
        # full-width rows; the LOWER row's badge is being placed. The
        # trailing strip is off-monitor, and the half-outside corner point
        # (after the monitor clamp) overhangs the row above.
        rect = self._rect(mon, 1776, 110, 144, 108)      # own row
        own_box = (1776.0, 110.0, 1920.0, 218.0)
        row_above = (1776.0, 0.0, 1920.0, 108.0)
        got = mgr._numeral_badge_placement_phys(
            rect, 80, 60, 1.0, 1920, 1080, [own_box, row_above],
            corner="top_right", placed_badges=[],
        )
        # Leading gutter: badge's right edge on the control's left edge,
        # top-aligned with the row -- fully outside, covering nothing.
        assert got == (1696.0, 110.0, 1776.0, 170.0)

    def test_small_control_gutter_when_neighbors_block_trailing(
        self, overlay_mgr
    ):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # Half-width tray-icon shape: a neighbor above (the corner point
        # overhangs it) and a neighbor right (blocks the trailing strip).
        rect = self._rect(mon, 500, 500, 30, 40)
        own_box = (500.0, 500.0, 530.0, 540.0)
        above = (480.0, 460.0, 550.0, 500.0)
        right = (530.0, 495.0, 570.0, 535.0)
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [own_box, above, right],
            corner="top_right", placed_badges=[],
        )
        assert got == (480.0, 500.0, 500.0, 530.0)

    def test_small_control_falls_back_inside_when_gutter_blocked(
        self, overlay_mgr
    ):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # Neighbors above, right, AND left: only the fully-inside corner is
        # clean. Covering the control's own icon beats covering a neighbor's.
        rect = self._rect(mon, 500, 500, 30, 40)
        own_box = (500.0, 500.0, 530.0, 540.0)
        above = (480.0, 460.0, 550.0, 500.0)
        right = (530.0, 495.0, 570.0, 535.0)
        left = (460.0, 490.0, 500.0, 550.0)
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [own_box, above, right, left],
            corner="top_right", placed_badges=[],
        )
        assert got == (510.0, 500.0, 530.0, 530.0)

    def test_clamped_sliver_badge_moves_to_gutter_not_onto_neighbor(
        self, overlay_mgr
    ):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # The "Show desktop" shape: a wide sliver at the monitor's very
        # bottom. The inside-corner anchor is clamped up ONTO the control
        # above (the bell); the badge must move to the gutter beside the
        # sliver (vertically clamped onto the monitor) instead.
        rect = self._rect(mon, 500, 1069, 144, 11)
        own_box = (500.0, 1069.0, 644.0, 1080.0)
        bell = (500.0, 999.0, 644.0, 1069.0)
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [own_box, bell],
            corner="top_right", placed_badges=[],
        )
        assert got == (480.0, 1050.0, 500.0, 1080.0)

    def test_gutter_candidate_avoids_placed_badge(self, overlay_mgr):
        mgr, _mod, _ = overlay_mgr
        mon = self._mon()
        # The gutter spot is clean of controls but an earlier badge already
        # sits there: fall through (here to the small inside corner).
        rect = self._rect(mon, 500, 500, 30, 40)
        own_box = (500.0, 500.0, 530.0, 540.0)
        above = (480.0, 460.0, 550.0, 500.0)
        right = (530.0, 495.0, 570.0, 535.0)
        placed = [(480.0, 500.0, 500.0, 530.0)]
        got = mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, [own_box, above, right],
            corner="top_right", placed_badges=placed,
        )
        assert got == (510.0, 500.0, 530.0, 530.0)


class TestNudgePrefersStayingAttached:
    """A collision nudge prefers a spot that keeps the badge ON (or within
    the attach gap of) its OWN control -- which draws with a pointer tail,
    unambiguous by construction -- over a spot that avoids every control
    but detaches the bubble (wh-taskbar-badge-mispoint).

    On a vertical taskbar every candidate inside the column overlaps some
    full-width row, so the old preference (first candidate clean of all
    other controls) systematically pushed overflow badges onto the desktop,
    a leader line pointing back at blank taskbar edge."""

    def test_nudge_prefers_staying_attached_over_desktop_exile(
        self, overlay_mgr
    ):
        mgr, _mod, _ = overlay_mgr
        own_box = (1776.0, 110.0, 1920.0, 218.0)
        row_above = (1776.0, 0.0, 1920.0, 108.0)
        # A wide badge on a full-width right-edge row: the base straddles
        # the row boundary and collides with an already-placed badge.
        base = (1770.0, 85.0, 1920.0, 115.0)
        placed = [(1770.0, 80.0, 1920.0, 112.0)]
        got = mgr._resolve_badge_collision(
            base, 150, 30, 1.0, 1920, 1080, placed, [row_above],
            corner="top_right", own_box=own_box,
        )
        # Ring-1 below overlaps the own row (attached, clean of the row
        # above); ring-1 inward is fully on the desktop -- clean but
        # DETACHED -- and comes first in ring order. Attached must win.
        assert got == (1770.0, 118.0, 1920.0, 148.0)


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
            mgr, "_draw_numeral_bubble", side_effect=RuntimeError("boom")
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
        # wh-overlay-slow-uia-stale-badges.9: the paint/clear handlers arm
        # and cancel the badge lease, so the stub carries the real helpers
        # over a mock timer.
        self._overlay_lease_timer = MagicMock()
        self._overlay_lease_pair = None
        self._arm_overlay_lease = GuiManager._arm_overlay_lease.__get__(self)
        self._cancel_overlay_lease = (
            GuiManager._cancel_overlay_lease.__get__(self)
        )
        # wh-overlay-slow-uia-stale-badges.18.4: the clear handler checks
        # teardown_pending after driving the manager, so the stub carries
        # the real helper over a mock retry timer.
        self._overlay_teardown_retry_timer = MagicMock()
        self._arm_teardown_retry_if_pending = (
            GuiManager._arm_teardown_retry_if_pending.__get__(self)
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


class TestLeaseExpiry:
    """wh-overlay-slow-uia-stale-badges.9 -- GUI-side badge lease.

    ``expire_lease`` is the lease timer's teardown: destroy every badge
    window, advance the generation gate exactly like a clear, and report
    ``state="expired"`` so Logic learns the badges are gone. A stale
    expiry (the pair lost a race with a newer paint/clear) is a no-op.
    """

    def test_expire_lease_tears_down_and_emits_expired(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        user32.DestroyWindow.reset_mock()

        expired = mgr.expire_lease(overlay_session_id=5, paint_generation=4)
        assert expired is not None
        assert expired["action"] == "overlay_state_changed"
        assert expired["state"] == "expired"
        assert expired["overlay_session_id"] == 5
        assert expired["paint_generation"] == 4
        assert not mgr._windows
        user32.DestroyWindow.assert_called_once()

    def test_expire_lease_blocks_same_pair_late_paint(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        assert mgr.expire_lease(overlay_session_id=5, paint_generation=4)
        user32.CreateWindowExW.reset_mock()

        # A late paint at the expired pair must not re-present dead badges.
        late, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert late is None
        user32.CreateWindowExW.assert_not_called()

    def test_stale_expire_returns_none_and_leaves_windows(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        user32.DestroyWindow.reset_mock()

        # An expiry for a pair the mark already passed is a race loser:
        # a newer overlay is on screen and must stay.
        stale = mgr.expire_lease(overlay_session_id=5, paint_generation=3)
        assert stale is None
        assert mgr._windows, "stale expiry must NOT destroy the newer overlay"
        user32.DestroyWindow.assert_not_called()


class TestReset:
    """wh-overlay-slow-uia-stale-badges.9 -- Logic-restart reset.

    ``reset`` is the ``reset_overlay`` startup action: destroy any badge
    windows a previous Logic left behind and start a FRESH generation
    gate, because a restarted Logic restarts its (session, generation)
    numbering from zero and the old high-water mark would gate every new
    paint forever. No event is emitted -- there is no pair to report.
    """

    def test_reset_destroys_windows_and_accepts_restarted_pairs(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, _) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        user32.DestroyWindow.reset_mock()

        assert mgr.reset() is None
        assert not mgr._windows
        user32.DestroyWindow.assert_called_once()

        # A restarted Logic paints at a pair the OLD gate would refuse.
        fresh, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert fresh is not None
        assert fresh["state"] == "painted"


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
        # destroy failed: the window must be retained for retry, and the
        # ack is DEFERRED, not returned -- an immediate "cleared" would
        # claim success while the badge stayed on screen
        # (wh-overlay-slow-uia-stale-badges.18.4).
        assert cleared is None
        assert mgr.teardown_pending is True
        assert window.hwnd is not None, (
            "a window whose DestroyWindow failed must keep its handle"
        )
        assert window in mgr._pending_destroy, (
            "the manager must move a window that failed to destroy into "
            "the _pending_destroy debt cohort (.18.10)"
        )
        assert not mgr._windows, (
            "_windows must end empty after a teardown sweep (.18.10)"
        )
        kernel32.GetLastError.assert_called()

        # A follow-up teardown where DestroyWindow now succeeds clears it.
        user32.DestroyWindow.return_value = 1
        mgr.clear(overlay_session_id=1, paint_generation=2)
        assert window.hwnd is None
        assert mgr._pending_destroy == []
        assert not mgr._windows

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


class TestIncompleteTeardownDefersAck:
    """wh-overlay-slow-uia-stale-badges.18.4 -- a teardown that leaves a
    surviving window must not claim success.

    ``_destroy_all`` retains any window whose DestroyWindow failed,
    because that window can still be on screen. Emitting ``cleared`` /
    ``expired`` anyway let Logic resolve its clear-ack watchdog and let
    the GUI cancel the badge lease -- the only retry drivers -- while the
    survivor stayed visible. Now an incomplete teardown emits NOTHING:
    the would-be ack is parked in ``_deferred_teardown_ack``,
    ``teardown_pending`` turns True, and the GUI retry timer drives
    ``retry_teardown`` until the sweep ends clean, which releases the
    parked ack exactly once. Logic's 5000 ms clear-ack watchdog fires a
    truthful "badges may still be on the screen" ERROR in the meantime
    -- that is the designed behavior, not a bug.
    """

    @staticmethod
    def _fail_destroys(user32, kernel32):
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.return_value = 1400  # ERROR_INVALID_WINDOW_HANDLE

    def test_incomplete_clear_defers_ack_and_advances_gate(self, overlay_mgr):
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)

        cleared = mgr.clear(overlay_session_id=1, paint_generation=1)

        # No ack while a window survived: a "cleared" here would resolve
        # Logic's clear-ack watchdog with badges still on screen. The
        # survivor moves into the debt cohort (.18.10).
        assert cleared is None
        assert mgr.teardown_pending is True
        assert window in mgr._pending_destroy
        assert not mgr._windows
        # The would-be ack is parked for the retry path.
        deferred = mgr._deferred_teardown_ack
        assert deferred is not None
        assert deferred["state"] == "cleared"
        assert deferred["overlay_session_id"] == 1
        assert deferred["paint_generation"] == 1
        # The gate DID advance: a late paint at the cleared pair stays
        # blocked exactly as after a clean clear.
        late, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=1)
        assert late is None

    def test_incomplete_expire_lease_defers_ack(self, overlay_mgr):
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)

        expired = mgr.expire_lease(overlay_session_id=5, paint_generation=4)

        assert expired is None
        assert mgr.teardown_pending is True
        assert window in mgr._pending_destroy
        assert not mgr._windows
        deferred = mgr._deferred_teardown_ack
        assert deferred is not None
        assert deferred["state"] == "expired"
        assert deferred["overlay_session_id"] == 5
        assert deferred["paint_generation"] == 4

    def test_retry_teardown_releases_deferred_ack_exactly_once(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)
        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None

        # Destroy still fails: the retry releases nothing and stays pending.
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is True
        assert window in mgr._pending_destroy

        # Destroy now succeeds: the deferred ack is released exactly once.
        user32.DestroyWindow.return_value = 1
        ack = mgr.retry_teardown()
        assert ack is not None
        assert ack["state"] == "cleared"
        assert ack["overlay_session_id"] == 1
        assert ack["paint_generation"] == 1
        assert mgr.teardown_pending is False
        assert window.hwnd is None
        assert mgr._pending_destroy == []
        assert not mgr._windows
        # A later retry finds nothing and must not repeat the ack.
        assert mgr.retry_teardown() is None

    def test_reset_drops_deferred_ack_but_keeps_retry_pending(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        self._fail_destroys(user32, kernel32)
        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None
        assert mgr._deferred_teardown_ack is not None

        # A restarted Logic announced itself: the old Logic's parked ack
        # must never surface, but the survivor still needs the retry.
        mgr.reset()
        assert mgr._deferred_teardown_ack is None
        assert mgr.teardown_pending is True

        # A later successful retry destroys the survivor and emits nothing.
        user32.DestroyWindow.return_value = 1
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is False
        assert mgr._pending_destroy == []
        assert not mgr._windows

    def test_clean_teardowns_return_ack_and_stay_unpending(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        assert mgr.teardown_pending is False

        cleared = mgr.clear(overlay_session_id=1, paint_generation=1)
        assert cleared is not None
        assert cleared["state"] == "cleared"
        assert mgr.teardown_pending is False
        assert mgr._deferred_teardown_ack is None

        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=2)
        assert result["state"] == "painted"
        expired = mgr.expire_lease(overlay_session_id=1, paint_generation=2)
        assert expired is not None
        assert expired["state"] == "expired"
        assert mgr.teardown_pending is False
        assert mgr._deferred_teardown_ack is None


class TestStaleRetryDoesNotSweepFreshWindows:
    """wh-overlay-slow-uia-stale-badges.18.8 -- a stale teardown retry
    must not destroy a newer overlay.

    A single-shot retry timer can outlive the teardown it was armed
    for: a later clean clear / expire_lease / reset pays the teardown
    debt (``teardown_pending`` goes False) before the timer fires, and
    a fresh paint then takes the screen. ``retry_teardown`` is
    ownership-aware: without teardown debt it never sweeps -- it only
    releases a still-parked ack (the accepted late-bookkeeping
    residual) or does nothing.
    """

    @staticmethod
    def _fail_destroys(user32, kernel32):
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.return_value = 1400  # ERROR_INVALID_WINDOW_HANDLE

    def test_retry_without_debt_leaves_fresh_windows_alone(self, overlay_mgr):
        mgr, mod, (user32, _, _) = overlay_mgr
        # A fresh paint owns the screen; no teardown debt exists.
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        assert mgr.teardown_pending is False
        user32.DestroyWindow.reset_mock()

        assert mgr.retry_teardown() is None

        # No sweep ran: the fresh paint's window is untouched.
        user32.DestroyWindow.assert_not_called()
        assert 10 in mgr._windows
        assert mgr._windows[10].hwnd is not None

    def test_retry_without_debt_releases_parked_ack_without_sweep(
        self, overlay_mgr
    ):
        mgr, mod, (user32, _, _) = overlay_mgr
        # A fresh paint owns the screen. Simulate an OLD parked ack whose
        # debt a later clean sweep already paid (teardown_pending False).
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=3)
        assert result["state"] == "painted"
        old_ack = {
            "action": "overlay_state_changed", "state": "cleared",
            "overlay_session_id": 1, "paint_generation": 1,
        }
        mgr._deferred_teardown_ack = old_ack
        assert mgr.teardown_pending is False
        user32.DestroyWindow.reset_mock()

        # The parked ack is released exactly once, with NO sweep.
        assert mgr.retry_teardown() is old_ack
        user32.DestroyWindow.assert_not_called()
        assert 10 in mgr._windows
        assert mgr.retry_teardown() is None
        assert 10 in mgr._windows

    def test_retry_with_debt_still_sweeps_and_stays_pending(
        self, overlay_mgr
    ):
        # Pin: the debt path sweeps the COHORT -- a still-failing survivor
        # in _pending_destroy is re-swept and the retry stays pending. It
        # never touches _windows (wh-overlay-slow-uia-stale-badges.18.10).
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)
        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None
        assert mgr.teardown_pending is True
        user32.DestroyWindow.reset_mock()

        assert mgr.retry_teardown() is None

        user32.DestroyWindow.assert_called()  # the cohort sweep DID run
        assert mgr.teardown_pending is True
        assert window in mgr._pending_destroy
        assert not mgr._windows

    def test_stale_retry_after_clean_clear_preserves_fresh_paint(
        self, overlay_mgr
    ):
        # The finding's ordering (a): incomplete clear at P parks P's ack
        # -> a later clean clear pays the debt -> fresh paint R -> the
        # stale timer fires. R's windows survive; the old parked P ack is
        # released as late bookkeeping, exactly once.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        self._fail_destroys(user32, kernel32)
        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None
        assert mgr.teardown_pending is True

        # DestroyWindow works again: a later clean clear pays the debt.
        user32.DestroyWindow.return_value = 1
        later = mgr.clear(overlay_session_id=1, paint_generation=2)
        assert later is not None
        assert later["state"] == "cleared"
        assert mgr.teardown_pending is False

        # A fresh paint R takes the screen.
        fresh, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=3)
        assert fresh["state"] == "painted"
        window = mgr._windows[10]

        ack = mgr.retry_teardown()

        assert ack is not None
        assert ack["state"] == "cleared"
        assert ack["overlay_session_id"] == 1
        assert ack["paint_generation"] == 1
        assert mgr._windows.get(10) is window
        assert window.hwnd is not None
        # Released exactly once; R still untouched.
        assert mgr.retry_teardown() is None
        assert mgr._windows.get(10) is window

    def test_stale_retry_after_reset_preserves_fresh_paint(
        self, overlay_mgr
    ):
        # The finding's ordering (b): the debt-payer is reset() (which
        # drops the parked ack). R's windows survive and NO ack is
        # returned.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        self._fail_destroys(user32, kernel32)
        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None

        # DestroyWindow works again: reset() pays the debt, drops the ack.
        user32.DestroyWindow.return_value = 1
        mgr.reset()
        assert mgr.teardown_pending is False
        assert mgr._deferred_teardown_ack is None

        # Fresh paint from the restarted Logic (pairs restart from zero).
        fresh, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert fresh["state"] == "painted"
        window = mgr._windows[10]

        assert mgr.retry_teardown() is None

        assert mgr._windows.get(10) is window
        assert window.hwnd is not None


class TestDebtBearingRetrySweepsOnlyCohort:
    """wh-overlay-slow-uia-stale-badges.18.10 -- a DEBT-BEARING retry must
    not destroy a newer paint's windows.

    An incomplete teardown of pair P moves its survivor into the
    ``_pending_destroy`` debt cohort and leaves ``_windows`` empty. A
    newer accepted paint Q then owns ``_windows`` alone. The retry
    sweeps ONLY the cohort (``_destroy_pending``), so it can never
    touch a window Q owns. Before this fix the debt branch called
    ``_destroy_all``, which swept the whole live map and could destroy
    (or recycle-then-destroy) Q's windows while Logic stayed PAINTED
    at Q -- a permanent invisible-but-active overlay.
    """

    @staticmethod
    def _fail_destroys(user32, kernel32):
        user32.DestroyWindow.return_value = 0
        kernel32.GetLastError.return_value = 1400  # ERROR_INVALID_WINDOW_HANDLE

    def test_failed_clear_then_new_paint_retry_preserves_new_windows(
        self, overlay_mgr
    ):
        # The finding's primary sequence: failed clear(P) -> accepted new
        # paint Q -> retry. Q's windows survive both the failing retry and
        # the finally-successful one; the successful one releases P's
        # parked cleared ack as late bookkeeping.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        p_window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)

        assert mgr.clear(overlay_session_id=1, paint_generation=1) is None
        assert mgr.teardown_pending is True
        # The survivor is the teardown's debt cohort; the live map is empty.
        assert p_window in mgr._pending_destroy
        assert not mgr._windows

        # A NEW accepted paint Q takes the screen while the destroy still
        # fails. Q gets a FRESH window -- the survivor is never recycled.
        fresh, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=2)
        assert fresh["state"] == "painted"
        q_window = mgr._windows[10]
        assert q_window is not p_window
        # The paint-time orphan sweep retried the survivor and kept it.
        assert p_window in mgr._pending_destroy

        # A retry while the destroy still fails sweeps ONLY the cohort.
        user32.DestroyWindow.reset_mock()
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is True
        user32.DestroyWindow.assert_called_once()  # the survivor was retried
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None

        # The survivor's destroy finally succeeds: the retry releases P's
        # parked cleared ack while Q's windows remain untouched.
        user32.DestroyWindow.return_value = 1
        ack = mgr.retry_teardown()
        assert ack is not None
        assert ack["state"] == "cleared"
        assert ack["overlay_session_id"] == 1
        assert ack["paint_generation"] == 1
        assert mgr.teardown_pending is False
        assert p_window.hwnd is None
        assert mgr._pending_destroy == []
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None
        # Released exactly once; Q still untouched afterwards.
        assert mgr.retry_teardown() is None
        assert mgr._windows.get(10) is q_window

    def test_reset_with_survivor_then_fresh_paint_retry_preserves_it(
        self, overlay_mgr
    ):
        # The fresh-Logic path: reset() keeps the debt but parks no ack.
        # The restarted Logic paints its fresh pair; the retry sweeps only
        # the cohort and returns nothing.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert result["state"] == "painted"
        p_window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)

        mgr.reset()
        assert mgr.teardown_pending is True
        assert mgr._deferred_teardown_ack is None
        assert p_window in mgr._pending_destroy
        assert not mgr._windows

        # The restarted Logic numbers from zero; the reset gate accepts it.
        fresh, _mon = _paint_single_monitor(mgr, mod, 10, session=1, gen=0)
        assert fresh["state"] == "painted"
        q_window = mgr._windows[10]
        assert q_window is not p_window

        user32.DestroyWindow.reset_mock()
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is True
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None

        # The destroy succeeds: the debt is paid, nothing is released
        # (reset dropped the old Logic's ack), and Q survives.
        user32.DestroyWindow.return_value = 1
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is False
        assert p_window.hwnd is None
        assert mgr._pending_destroy == []
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None

    def test_incomplete_expiry_then_repaint_retry_preserves_new_windows(
        self, overlay_mgr
    ):
        # The incomplete-expiry path: expire_lease(P) parks an expired ack;
        # a repaint takes the screen; the retry sweeps only the cohort and
        # releases the expired ack once the survivor destroys.
        mgr, mod, (user32, _, kernel32) = overlay_mgr
        result, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=4)
        assert result["state"] == "painted"
        p_window = mgr._windows[10]
        self._fail_destroys(user32, kernel32)

        assert mgr.expire_lease(overlay_session_id=5, paint_generation=4) is None
        assert mgr.teardown_pending is True
        assert p_window in mgr._pending_destroy
        assert not mgr._windows

        repaint, _mon = _paint_single_monitor(mgr, mod, 10, session=5, gen=5)
        assert repaint["state"] == "painted"
        q_window = mgr._windows[10]
        assert q_window is not p_window

        user32.DestroyWindow.reset_mock()
        assert mgr.retry_teardown() is None
        assert mgr.teardown_pending is True
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None

        user32.DestroyWindow.return_value = 1
        ack = mgr.retry_teardown()
        assert ack is not None
        assert ack["state"] == "expired"
        assert ack["overlay_session_id"] == 5
        assert ack["paint_generation"] == 4
        assert mgr.teardown_pending is False
        assert p_window.hwnd is None
        assert mgr._pending_destroy == []
        assert mgr._windows.get(10) is q_window
        assert q_window.hwnd is not None
        assert mgr.retry_teardown() is None
        assert mgr._windows.get(10) is q_window


# ===========================================================================
# 8. GUI config wiring (wh-n29v.58): the validated overlay badge settings
#    actually reach OverlayPaintWindowManager.
#
#    Today overlay_badge_font_pt and overlay_badge_shadow are validated by
#    ClickConfig but the GUI never reads the config or passes the two values,
#    so the constructor defaults (16 / True) are always used. This proves the
#    config -> ClickConfig.from_raw -> OverlayPaintWindowManager path.
# ===========================================================================


# ===========================================================================
# Bubble badge geometry (wh-bubble-geometry-fns): pure functions for the
# speech-bubble redesign -- theme mapping, three-state drawing decision, and
# the bubble-sized badge box. All unit-tested before any painting changes.
# Spec: docs/plans/2026-08-04-overlay-bubble-badges-design-v1.md.
# ===========================================================================


class TestBubbleSchemeMapping:
    """``_resolve_bubble_scheme`` maps the validated overlay_badge_theme plus
    the system color scheme to the bubble's OWN scheme ("light" or "dark",
    never "auto"). "auto" inverts the system theme for contrast: a mostly-dark
    screen gets a white bubble, a mostly-light screen a near-black bubble."""

    def test_auto_with_system_dark_gives_light_bubble(self):
        import overlay_paint_window as mod
        from PySide6.QtCore import Qt
        assert mod._resolve_bubble_scheme("auto", Qt.ColorScheme.Dark) == "light"

    def test_auto_with_system_light_gives_dark_bubble(self):
        import overlay_paint_window as mod
        from PySide6.QtCore import Qt
        assert mod._resolve_bubble_scheme("auto", Qt.ColorScheme.Light) == "dark"

    def test_auto_with_unknown_scheme_gives_light_bubble(self):
        import overlay_paint_window as mod
        from PySide6.QtCore import Qt
        assert (
            mod._resolve_bubble_scheme("auto", Qt.ColorScheme.Unknown) == "light"
        )

    def test_pinned_values_ignore_the_system_scheme(self):
        import overlay_paint_window as mod
        from PySide6.QtCore import Qt
        schemes = (
            Qt.ColorScheme.Dark, Qt.ColorScheme.Light, Qt.ColorScheme.Unknown
        )
        for scheme in schemes:
            assert mod._resolve_bubble_scheme("light", scheme) == "light"
            assert mod._resolve_bubble_scheme("dark", scheme) == "dark"

    def test_system_scheme_seam_reads_style_hints(self, qapp):
        # _system_color_scheme is the one place that touches
        # QGuiApplication.styleHints(); with a live app it reports the real
        # scheme, read fresh at each call (each paint).
        import overlay_paint_window as mod
        assert mod._system_color_scheme() == qapp.styleHints().colorScheme()

    def test_system_scheme_seam_degrades_to_unknown_on_failure(self):
        # A failing styleHints read degrades to Unknown instead of raising, so
        # a paint can never crash on the theme read.
        import overlay_paint_window as mod
        from PySide6.QtCore import Qt
        with patch.object(
            mod.QGuiApplication, "styleHints", side_effect=RuntimeError("boom")
        ):
            assert mod._system_color_scheme() == Qt.ColorScheme.Unknown


class TestBubbleDrawingState:
    """``_bubble_drawing_state`` picks per badge among the three drawing
    states from the shortest straight-line distance between the bubble's
    placed rectangle and the control's rectangle (both physical px):
    "overlap" (rectangles intersect by area -> bubble + inward pointer
    tail), "adjacent" (gap <= 8 logical px -> bubble + pointer tail across
    the gap), "detached" (further -> bubble + leader line). The threshold
    scales with the device pixel ratio."""

    _CONTROL = (100.0, 100.0, 300.0, 200.0)

    def _state(self, bubble, dpr=1.0):
        import overlay_paint_window as mod
        return mod._bubble_drawing_state(bubble, self._CONTROL, dpr)

    def test_intersecting_rects_are_overlap(self):
        # Small-control corner-point placement: the bubble sits half over the
        # control's corner.
        assert self._state((90.0, 90.0, 130.0, 120.0)) == "overlap"

    def test_flush_touching_rects_are_adjacent(self):
        # Trailing-space placement drops the bubble flush against the
        # control's right edge: touching is NOT area overlap, and gap 0 is
        # within the threshold, so the tail is drawn.
        assert self._state((300.0, 100.0, 340.0, 130.0)) == "adjacent"

    def test_gap_exactly_at_threshold_is_adjacent(self):
        # Control right edge 300, bubble left 308: gap exactly 8 at dpr 1.0.
        assert self._state((308.0, 100.0, 348.0, 130.0)) == "adjacent"

    def test_gap_just_past_threshold_is_detached(self):
        assert self._state((308.5, 100.0, 348.5, 130.0)) == "detached"

    def test_threshold_scales_with_device_pixel_ratio(self):
        # A 12-physical-px gap: past the threshold at dpr 1.0 (8 physical px)
        # but within it at dpr 2.0 (16 physical px).
        bubble = (312.0, 100.0, 352.0, 130.0)
        assert self._state(bubble, dpr=1.0) == "detached"
        assert self._state(bubble, dpr=2.0) == "adjacent"

    def test_diagonal_gap_uses_straight_line_distance(self):
        # Bubble past the control's bottom-right corner. dx 6, dy 6 ->
        # straight-line gap 8.49 > 8: detached, even though each axis gap
        # alone is under the threshold.
        assert self._state((306.0, 206.0, 346.0, 236.0)) == "detached"
        # dx 5, dy 5 -> 7.07 <= 8: adjacent.
        assert self._state((305.0, 205.0, 345.0, 235.0)) == "adjacent"


class TestBubbleTailPath:
    """Geometry contract of ``_bubble_tail_path``'s INWARD branch
    (wh-overlay-bubble-badges.3.1): the whole tail stays inside the union of
    the bubble's rectangle and the control's rectangle. The apex reach is
    capped where the aim ray leaves the control, because a sliver control at
    a monitor edge would otherwise carry the apex past the control AND past
    the monitor clamp in ``_compute_monitor_bbox``, and the surface clamp
    would cut the tail tip flat."""

    def _union_bounds(self, bubble, control):
        return (
            min(bubble.left(), control[0]),
            min(bubble.top(), control[1]),
            max(bubble.right(), control[2]),
            max(bubble.bottom(), control[3]),
        )

    def test_inward_apex_capped_at_a_sliver_control_edge(self, overlay_mgr):
        # Codex round-1 repro: a 1x14 sliver control at the bottom edge of a
        # 1080-high monitor; the badge box clamps against the monitor and the
        # bubble overlaps the control (anchor distance zero -> inward branch).
        # Uncapped, the apex lands at y ~= 1081.7 -- past the control's and
        # the monitor's bottom -- and the surface clamp truncates the tail.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(85.25, 1048.25, 29.5, 26.5)
        control = (100.0, 1066.0, 101.0, 1080.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 1920.0, 1080.0)
        ul, ut, ur, ub = self._union_bounds(bubble, control)
        rect = tail.boundingRect()
        assert rect.bottom() <= ub + 1e-6, (
            f"tail bottom {rect.bottom()} exceeds the control/monitor "
            f"bottom {ub}"
        )
        assert rect.left() >= ul - 1e-6
        assert rect.top() >= ut - 1e-6
        assert rect.right() <= ur + 1e-6

    def test_inward_apex_reaches_full_length_inside_a_large_control(
        self, overlay_mgr
    ):
        # Guard against over-capping: with the control extending far past the
        # bubble in the aim direction, the apex must protrude the FULL
        # inward reach past the bubble edge -- the cap only ever shortens a
        # tail that would leave the control.
        mgr, mod, _ = overlay_mgr
        # Control center (300, 118.5) level with the bubble center (120,
        # 118.5): the aim direction is exactly +x, the ray exits the bubble's
        # right edge at x = 134.75, and the apex ends 7 px further right.
        bubble = QRectF(105.25, 105.25, 29.5, 26.5)
        control = (100.0, 100.0, 500.0, 137.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 2000.0, 1200.0)
        expected_apex_x = (
            bubble.right() + mod._BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX
        )
        assert tail.boundingRect().right() == pytest.approx(expected_apex_x)

    def test_bubble_covering_its_control_draws_no_protruding_tail(
        self, overlay_mgr
    ):
        # The bubble fully covers a tiny control. The anchor distance here is
        # NONZERO (the bubble's nearest-edge point sits outside the control's
        # interior), so the ADJACENT construction fires and its apex ends 3
        # px inside the control -- deep inside the bubble. Either way, the
        # contract this test pins is that no tail ink can protrude from the
        # bubble when the control is entirely underneath it.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(100.0, 100.0, 40.0, 30.0)
        control = (110.0, 110.0, 120.0, 118.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 500.0, 400.0)
        rect = tail.boundingRect()
        assert rect.left() >= bubble.left() - 1e-6
        assert rect.top() >= bubble.top() - 1e-6
        assert rect.right() <= bubble.right() + 1e-6
        assert rect.bottom() <= bubble.bottom() + 1e-6

    def _assert_tail_in_safe_region(
        self, tail, bubble, control, margin, mon_w, mon_h
    ):
        """The whole tail must stay inside the region the surface clamp
        preserves: the badge box (placement clamps it onto the monitor) union
        the control's ON-monitor part. Everything outside is cut flat by the
        monitor clamp in ``_compute_monitor_bbox``
        (wh-overlay-bubble-badges.3.3)."""
        box = bubble.adjusted(-margin, -margin, margin, margin)
        clipped = (
            max(control[0], 0.0),
            max(control[1], 0.0),
            min(control[2], mon_w),
            min(control[3], mon_h),
        )
        rect = tail.boundingRect()
        assert rect.left() >= min(box.left(), clipped[0]) - 1e-6
        assert rect.top() >= min(box.top(), clipped[1]) - 1e-6
        assert rect.right() <= max(box.right(), clipped[2]) + 1e-6
        assert rect.bottom() <= max(box.bottom(), clipped[3]) + 1e-6
        assert rect.left() >= -1e-6
        assert rect.top() >= -1e-6
        assert rect.right() <= mon_w + 1e-6
        assert rect.bottom() <= mon_h + 1e-6

    def test_pre_entry_inward_tail_stays_on_the_monitor(self, overlay_mgr):
        # Codex round-2 repro (wh-overlay-bubble-badges.3.3): a tall narrow
        # control hanging off the bottom of a 240x180 monitor, badge box
        # corner-clamped flush to the monitor bottom. The bubble's left-edge
        # midpoint is inside the control (anchor distance zero -> inward
        # branch), but the center-directed ray exits the bubble's BOTTOM edge
        # at x ~= 14.3 -- outside the control's x span -- so the ray enters
        # the control only at t ~= 11, past the 7 px reach. An
        # exit-distance-only cap leaves the apex at (11.6, 181.2): outside
        # the control, past the monitor bottom, cut flat by the surface
        # clamp.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(5.25, 148.25, 29.5, 26.5)
        control = (0.0, 159.0, 10.0, 234.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 240.0, 180.0)
        self._assert_tail_in_safe_region(
            tail, bubble, control, 5.25, 240.0, 180.0
        )
        # A tail must still exist: some ink beyond the bubble's rectangle.
        assert not bubble.contains(tail.boundingRect())

    def test_inward_tail_stays_inside_a_control_crossing_the_monitor_edge(
        self, overlay_mgr
    ):
        # The sibling case an entry-distance check alone would miss: the ray
        # reaches the control BEFORE the 7 px apex, but the control continues
        # past the monitor bottom and the aim (the raw control center at
        # y=195) points through the off-monitor part. An apex inside the
        # control at y ~= 181.5 is still outside the 180-high monitor, so
        # the surface clamp cuts it flat. The safe region is the control's
        # ON-monitor part.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(5.25, 148.25, 29.5, 26.5)
        control = (0.0, 150.0, 60.0, 240.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 180.0)
        self._assert_tail_in_safe_region(
            tail, bubble, control, 5.25, 400.0, 180.0
        )
        assert not bubble.contains(tail.boundingRect())

    def test_adjacent_tail_apex_stops_at_the_monitor_edge(self, overlay_mgr):
        # Same defect class in the ADJACENT branch: the control hangs off the
        # monitor's LEFT edge with a 2 px on-monitor sliver, and the badge
        # sits across a 6 px gap to its right. The apex ends 3 px past the
        # control's nearest boundary point -- at x = -1, off the monitor --
        # unless the reach is capped at the control's on-monitor part. The
        # cap must additionally leave room for the border pen's half-width
        # (0.625 px at dpr 1.0): an apex exactly ON the edge still strokes
        # ink past it (wh-overlay-bubble-badges.3.4).
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(13.25, 101.25, 29.5, 26.5)
        control = (-50.0, 100.0, 2.0, 130.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 300.0)
        rect = tail.boundingRect()
        assert rect.left() == pytest.approx(0.625), (
            f"tail left {rect.left()} must stop half the border pen inside "
            f"the monitor's left edge"
        )
        # The tail must still span the gap and touch the control's
        # on-monitor sliver.
        assert rect.left() <= 2.0 + 1e-6

    def test_adjacent_sliver_thinner_than_the_ink_inset_falls_back(
        self, overlay_mgr
    ):
        # An on-monitor sliver THINNER than the border pen's half-width: the
        # control's nearest boundary point (x = 0.5) sits inside the
        # monitor-edge ink band, so NO apex position at the control keeps the
        # stroke on the monitor. The apex must fall back to the badge-box ink
        # budget measured from the BUBBLE anchor (3.25 px past the bubble's
        # left edge at dpr 1.0 -> x = 10), not sit at x = 0 where the pen
        # strokes off-monitor ink (wh-overlay-bubble-badges.3.4).
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(13.25, 101.25, 29.5, 26.5)
        control = (-50.0, 100.0, 0.5, 130.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 300.0)
        rect = tail.boundingRect()
        assert rect.left() == pytest.approx(10.0)
        box = bubble.adjusted(-5.25, -5.25, 5.25, 5.25)
        assert rect.left() >= box.left() - 1e-6
        assert rect.top() >= box.top() - 1e-6
        assert rect.right() <= box.right() + 1e-6
        assert rect.bottom() <= box.bottom() + 1e-6
        # A visible tail still exists: ink past the bubble's left edge.
        assert rect.left() < bubble.left() - 1e-6

    def test_adjacent_apex_capped_at_an_interior_sliver_far_edge(
        self, overlay_mgr
    ):
        # The control-boundary cap is still load-bearing AWAY from monitor
        # edges: a 2 px sliver control deep in the monitor's interior, gap
        # 6.5 px. The 3 px apex inset would overshoot the sliver's far edge
        # at x = 139 and point past the control into empty space; the cap
        # stops it exactly at the far edge (wh-overlay-bubble-badges.3.1).
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(100.0, 100.0, 29.5, 26.5)
        control = (136.0, 100.0, 138.0, 130.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 300.0)
        assert tail.boundingRect().right() == pytest.approx(138.0)

    def test_pre_entry_fallback_scales_with_dpr(self, overlay_mgr):
        # The pre-entry repro at dpr 1.5 (every coordinate scaled by 1.5):
        # the cap and the badge-box fallback are physical-pixel math, so the
        # same containment must hold on a fractional-DPR monitor.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(7.875, 222.375, 44.25, 39.75)
        control = (0.0, 238.5, 15.0, 351.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.5, 360.0, 270.0)
        self._assert_tail_in_safe_region(
            tail, bubble, control, 5.25 * 1.5, 360.0, 270.0
        )
        assert not bubble.contains(tail.boundingRect())

    def test_unreachable_control_falls_back_to_the_badge_box_budget(
        self, overlay_mgr
    ):
        # A geometry where even the ray aimed at the ON-monitor control's
        # center enters the control only at t ~= 10.5, past the 7 px reach:
        # the bubble's left-edge midpoint is inside the tall narrow control
        # (anchor distance zero), but the center-directed ray exits through
        # the bubble's TOP edge at x ~= 12.9, right of the control. No point
        # of the requested reach lies inside the control, so the apex must
        # fall back to the badge-box ink budget (box margin 5.25 minus the
        # 2 px shadow offset = 3.25 px past the bubble edge): a visible tail
        # whose ink the always-on-monitor badge box fully contains.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(5.25, 148.25, 29.5, 26.5)
        control = (0.0, 100.0, 8.0, 163.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 300.0)
        rect = tail.boundingRect()
        box = bubble.adjusted(-5.25, -5.25, 5.25, 5.25)
        assert rect.left() >= box.left() - 1e-6
        assert rect.top() >= box.top() - 1e-6
        assert rect.right() <= box.right() + 1e-6
        assert rect.bottom() <= box.bottom() + 1e-6
        assert not bubble.contains(rect)
        # The apex sits exactly the fallback budget past the bubble's top
        # edge along the aim direction (uy = -30/34 for this geometry).
        assert rect.top() == pytest.approx(bubble.top() - 3.25 * (30.0 / 34.0))

    def test_inward_apex_capped_at_the_monitor_edge_of_a_crossing_control(
        self, overlay_mgr
    ):
        # The cap must bind at the CONTROL'S ON-MONITOR PART, not the raw
        # control: a wide control crossing the monitor bottom, aim steeply
        # downward, ray well inside the raw control for 25+ px. Slabbed
        # against the raw control the apex would land at y ~= 180.8, past
        # the 180-high monitor. The cap must ALSO leave room for the shadow
        # fill, which is the whole path translated down-right by the 2 px
        # shadow offset: an apex exactly ON the 180 monitor bottom still
        # paints shadow ink to 182, cut flat by the surface clamp
        # (wh-overlay-bubble-badges.3.4). So the apex stops the shadow
        # offset inside the edge: y = 178.
        mgr, _mod, _ = overlay_mgr
        bubble = QRectF(5.25, 148.25, 29.5, 26.5)
        control = (0.0, 150.0, 36.0, 400.0)
        tail = mgr._bubble_tail_path(bubble, control, 1.0, 400.0, 180.0)
        rect = tail.boundingRect()
        assert rect.bottom() <= 180.0 - 2.0 + 1e-6
        assert rect.bottom() == pytest.approx(178.0)


class TestBubbleTailInk:
    """Rendered-PIXEL containment for the tail fix
    (wh-overlay-bubble-badges.3.4): the path-geometry tests above inspect
    only ``boundingRect()``, which cannot see the border stroke (half the
    pen's width of ink past the path on every side) or the shadow (the whole
    path translated down-right by the shadow offset). These tests render one
    badge through the real ``_draw_numeral_bubble`` into an image PADDED
    past the monitor rect and assert no non-transparent pixel lands outside
    the monitor -- the pixels the real surface clamp would cut flat."""

    def _render_bubble(self, mgr, placement, control, dpr, mon_w, mon_h, pad):
        from PySide6.QtGui import QFontMetricsF, QPainter
        _bw, _bh, (fl, ft, fr, fb) = placement
        img_w = int(round((fr - fl) + 2 * pad))
        img_h = int(round((fb - ft) + 2 * pad))
        img = QImage(img_w, img_h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(-(fl - pad), -(ft - pad))
        font = mgr._numeral_font(dpr)
        metrics = QFontMetricsF(font)
        mgr._draw_numeral_bubble(
            painter, 1, placement, control, dpr, "light", metrics, font,
            mon_w, mon_h,
        )
        painter.end()
        return img, (int(fl - pad), int(ft - pad))

    @staticmethod
    def _pixels_outside_monitor(img, origin, mon_w, mon_h):
        ox, oy = origin
        out = []
        for sy in range(img.height()):
            for sx in range(img.width()):
                if (img.pixel(sx, sy) >> 24) & 0xFF:
                    mx, my = sx + ox, sy + oy
                    if not (0 <= mx < mon_w and 0 <= my < mon_h):
                        out.append((mx, my))
        return out

    @pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
    def test_no_tail_ink_past_the_monitor_right_edge(self, overlay_mgr, dpr):
        # Codex's production shape (wh-overlay-bubble-badges.3.4): the badge
        # box flush in the monitor's bottom-right corner, its control a
        # narrow strip along the right edge crossing the monitor bottom. The
        # inward aim points right; the reach cap lands the apex at the
        # monitor's right edge, and the border stroke plus the down-right
        # shadow then paint PAST the edge unless the cap subtracts the
        # rendered-ink extents.
        mgr, mod, _ = overlay_mgr
        mon_w, mon_h = 400.0 * dpr, 300.0 * dpr
        bw, bh = mgr._numeral_badge_size(1, dpr)
        fl, ft = mon_w - bw, mon_h - bh
        placement = (bw, bh, (fl, ft, mon_w, mon_h))
        control = (
            mon_w - 10.0 * dpr, ft + bh / 4.0,
            mon_w + 30.0 * dpr, mon_h + 60.0 * dpr,
        )
        assert mod._bubble_drawing_state(
            placement[2], control, dpr
        ) == "overlap"
        img, origin = self._render_bubble(
            mgr, placement, control, dpr, mon_w, mon_h, 40.0 * dpr
        )
        out = self._pixels_outside_monitor(img, origin, mon_w, mon_h)
        assert out == [], (
            f"dpr {dpr}: {len(out)} rendered pixels outside the monitor, "
            f"e.g. {out[:5]}"
        )

    @pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
    def test_no_tail_ink_past_the_monitor_left_or_top_edge(
        self, overlay_mgr, dpr
    ):
        # Mirror geometry for the pen-half inset on the LEFT and TOP edges
        # (the shadow only extends down-right; the border pen strokes past
        # the apex on every side): badge box flush in the top-left corner,
        # one control crossing the left edge (aim exactly -x), one crossing
        # the top edge (aim exactly -y).
        mgr, mod, _ = overlay_mgr
        mon_w, mon_h = 400.0 * dpr, 300.0 * dpr
        bw, bh = mgr._numeral_badge_size(1, dpr)
        placement = (bw, bh, (0.0, 0.0, float(bw), float(bh)))
        controls = [
            (-30.0 * dpr, bh / 4.0, 10.0 * dpr, 3.0 * bh / 4.0),
            (bw / 4.0, -30.0 * dpr, 3.0 * bw / 4.0, 10.0 * dpr),
        ]
        for control in controls:
            assert mod._bubble_drawing_state(
                placement[2], control, dpr
            ) == "overlap"
            img, origin = self._render_bubble(
                mgr, placement, control, dpr, mon_w, mon_h, 40.0 * dpr
            )
            out = self._pixels_outside_monitor(img, origin, mon_w, mon_h)
            assert out == [], (
                f"dpr {dpr}, control {control}: {len(out)} rendered pixels "
                f"outside the monitor, e.g. {out[:5]}"
            )


class TestBubbleBadgeSize:
    """``_numeral_badge_size`` returns the BUBBLE's size: glyph advance and
    cap height plus proportional padding (0.4 x cap height per side
    horizontally, 0.3 x cap height vertically, each at least 3 logical px)
    plus the existing outline+shadow+antialias margin. The untouched
    placement pass and collision nudges then operate on the bubble's true
    size with no placement-code changes."""

    def test_bubble_size_is_glyph_plus_padding_and_margins(self, overlay_mgr):
        # Pins the SPEC numbers (0.4 / 0.3 / 3 logical px minimum) as
        # literals, not the module's own constants, so a drifted constant
        # fails here.
        import math
        from PySide6.QtGui import QFontMetricsF
        mgr, _mod, _ = overlay_mgr
        for number in (1, 42):
            for dpr in (1.0, 2.0):
                metrics = QFontMetricsF(mgr._numeral_font(dpr))
                text_w = metrics.horizontalAdvance(str(number))
                cap_h = metrics.capHeight()
                pad_x = max(0.4 * cap_h, 3.0 * dpr)
                pad_y = max(0.3 * cap_h, 3.0 * dpr)
                margin = (1.25 + 2.0 + 2.0) * dpr
                expected_w = max(1, int(math.ceil(text_w + 2 * pad_x + 2 * margin)))
                expected_h = max(1, int(math.ceil(cap_h + 2 * pad_y + 2 * margin)))
                assert mgr._numeral_badge_size(number, dpr) == (
                    expected_w, expected_h,
                ), f"number {number} at dpr {dpr}"

    def test_bubble_adds_padding_over_the_bare_glyph_box(self, overlay_mgr):
        # Behavior check independent of the exact formula: the bubble box is
        # at least the 3-logical-px minimum padding per side larger than the
        # old tight glyph-plus-margin box on both axes.
        import math
        from PySide6.QtGui import QFontMetricsF
        mgr, _mod, _ = overlay_mgr
        for dpr in (1.0, 2.0):
            metrics = QFontMetricsF(mgr._numeral_font(dpr))
            text_w = metrics.horizontalAdvance("7")
            cap_h = metrics.capHeight()
            margin = (1.25 + 2.0 + 2.0) * dpr
            bare_w = math.ceil(text_w + 2 * margin)
            bare_h = math.ceil(cap_h + 2 * margin)
            w, h = mgr._numeral_badge_size(7, dpr)
            assert w >= bare_w + 2 * 3.0 * dpr, f"dpr {dpr}"
            assert h >= bare_h + 2 * 3.0 * dpr, f"dpr {dpr}"

    def test_bubble_size_grows_with_font_point_size(self, qapp):
        import overlay_paint_window
        small = overlay_paint_window.OverlayPaintWindowManager(badge_font_pt=16)
        large = overlay_paint_window.OverlayPaintWindowManager(badge_font_pt=32)
        sw, sh = small._numeral_badge_size(8, 1.0)
        lw, lh = large._numeral_badge_size(8, 1.0)
        assert lw > sw and lh > sh


class TestBubbleRender:
    """Pixel assertions on the composed monitor surface for the speech-bubble
    badges (wh-bubble-render-path). Each numeral badge is one QPainterPath
    drawn directly onto the per-monitor surface: rounded-rect bubble, merged
    pointer tail when adjacent, leader line underneath when detached, colors
    from the theme mapping. Spec:
    docs/plans/2026-08-04-overlay-bubble-badges-design-v1.md."""

    # Scheme colors from the spec's table.
    _LIGHT_FILL = (255, 255, 255)
    _LIGHT_DIGIT = (0, 0, 0)
    _DARK_FILL = (32, 32, 32)
    _DARK_DIGIT = (255, 255, 255)
    _LIGHT_BORDER = (102, 102, 102)
    _DARK_BORDER = (170, 170, 170)

    def _compose(self, mgr, *, x=100, y=100, w=200, h=100, number=1):
        """One control, one numeral badge; returns (surface, bbox, placement,
        control_phys) with placement/control in monitor-local PHYSICAL px
        (dpr 1.0 monitor, so logical == physical)."""
        monitor = _NativeMonitor(
            hmonitor=10, rect_phys=QRect(0, 0, 1920, 1080), dpi=96
        )
        rect = _paint_rect(10, monitor, x=x, y=y, width=w, height=h)
        badges = [(rect, number)]
        bbox = mgr._compute_monitor_bbox(monitor, badges)
        placements = mgr._numeral_badge_placements_phys(
            badges, 1.0, 1920, 1080, corner=mgr._badge_corner
        )
        surface = mgr._render_monitor_surface(monitor, badges, bbox)
        control = (float(x), float(y), float(x + w), float(y + h))
        return surface, bbox, placements[0], control

    @staticmethod
    def _rgba(surface, bbox, px, py):
        """(alpha, r, g, b) at PHYSICAL point (px, py); converts to
        surface-local via the bbox offset."""
        x = int(px) - bbox.offset_x
        y = int(py) - bbox.offset_y
        p = surface.pixel(x, y)
        return ((p >> 24) & 0xFF, (p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF)

    @staticmethod
    def _margin(mod, dpr=1.0):
        """The badge box's per-side margin around the visible bubble (border
        pen + shadow offset + antialias slack), matching _numeral_badge_size."""
        return (
            mod._NUMERAL_OUTLINE_PX * dpr
            + mod._SHADOW_OFFSET_PX * dpr
            + 2.0 * dpr
        )

    def _fill_sample(self, mod, placement):
        """A PHYSICAL point inside the bubble's fill, clear of the border
        (just inside the bubble's left edge) and of the centered digit."""
        _bw, _bh, (bl, bt, _br, bb) = placement
        margin = self._margin(mod)
        return (bl + margin + 3.0, (bt + bb) / 2.0)

    def test_overlapping_badge_ink_stays_in_box_plus_tail_overhang(
        self, overlay_mgr
    ):
        mgr, mod, _ = overlay_mgr
        # Inside-corner placement over a large control: the badge box overlaps
        # the control, so the bubble draws with an INWARD pointer tail toward
        # the control's center (down-right of the badge here) and NO leader
        # line.
        mgr._badge_corner = "top_left"
        mgr._badge_trailing_space = False
        mgr._badge_theme = "light"
        surface, bbox, placement, control = self._compose(mgr)
        bw, bh, (bl, bt, br, bb) = placement
        assert mod._bubble_drawing_state((bl, bt, br, bb), control, 1.0) == (
            "overlap"
        )
        # Bubble fill: opaque white at the fill sample point.
        fx, fy = self._fill_sample(mod, placement)
        assert self._rgba(surface, bbox, fx, fy) == (255, *self._LIGHT_FILL)
        # Just inside the badge box's top-left corner: OUTSIDE the rounded
        # bubble (and away from the bottom-right shadow), so transparent.
        a, _r, _g, _b = self._rgba(surface, bbox, bl + 1, bt + 1)
        assert a == 0
        # Ink containment: the top/left sides stay within the box (1 px slack
        # for rounding); the right/bottom sides additionally allow the inward
        # tail's overhang past the box on the control-center side
        # (_BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX minus the 5.25 box margin,
        # plus 1 px antialias).
        allow = math.ceil(mod._BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX - 5.25) + 1
        for sy in range(surface.height()):
            for sx in range(surface.width()):
                if (surface.pixel(sx, sy) >> 24) & 0xFF:
                    px = sx + bbox.offset_x
                    py = sy + bbox.offset_y
                    assert bl - 1 <= px <= br + allow, (px, py)
                    assert bt - 1 <= py <= bb + allow, (px, py)

    def test_overlapping_badge_draws_inward_tail(self, overlay_mgr):
        """An overlapping badge points at its control too (user decision
        2026-08-07, matching Voice Access): the bubble merges a pointer tail
        aimed from the bubble's edge toward the control's center, reaching
        _BUBBLE_TAIL_INWARD_APEX_LOGICAL_PX logical px past the edge. A pure
        fill-colored pixel strictly outside the bubble's rectangle can only
        be that tail's interior -- the border stroke is border-colored and
        the shadow is translucent black."""
        mgr, mod, _ = overlay_mgr
        mgr._badge_corner = "top_left"
        mgr._badge_trailing_space = False
        mgr._badge_theme = "light"
        surface, bbox, placement, control = self._compose(mgr)
        _bw, _bh, (bl, bt, br, bb) = placement
        assert mod._bubble_drawing_state((bl, bt, br, bb), control, 1.0) == (
            "overlap"
        )
        margin = self._margin(mod)
        # The bubble's rectangle (the rounded rect's bounds), grown 1 px for
        # antialias slack around its border.
        rl, rt = bl + margin - 1.0, bt + margin - 1.0
        rr, rb = br - margin + 1.0, bb - margin + 1.0
        found = False
        for py in range(int(bt), int(bb) + 9):
            for px in range(int(bl), int(br) + 9):
                if rl <= px <= rr and rt <= py <= rb:
                    continue
                if self._rgba(surface, bbox, px, py) == (
                    255, *self._LIGHT_FILL
                ):
                    found = True
                    break
            if found:
                break
        assert found, (
            "no fill-colored tail ink outside the overlapping bubble"
        )

    def test_light_scheme_draws_black_digit_in_white_bubble(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mgr._badge_theme = "light"
        surface, bbox, placement, _control = self._compose(mgr)
        fx, fy = self._fill_sample(mod, placement)
        assert self._rgba(surface, bbox, fx, fy) == (255, *self._LIGHT_FILL)
        # The digit itself: some pure-black pixel inside the bubble.
        _bw, _bh, (bl, bt, br, bb) = placement
        found = False
        for py in range(int(bt), int(bb)):
            for px in range(int(bl), int(br)):
                if self._rgba(surface, bbox, px, py) == (
                    255, *self._LIGHT_DIGIT
                ):
                    found = True
                    break
            if found:
                break
        assert found, "no black digit pixel inside the light bubble"

    def test_dark_scheme_draws_white_digit_in_near_black_bubble(
        self, overlay_mgr
    ):
        mgr, mod, _ = overlay_mgr
        mgr._badge_theme = "dark"
        surface, bbox, placement, _control = self._compose(mgr)
        fx, fy = self._fill_sample(mod, placement)
        assert self._rgba(surface, bbox, fx, fy) == (255, *self._DARK_FILL)
        _bw, _bh, (bl, bt, br, bb) = placement
        found = False
        for py in range(int(bt), int(bb)):
            for px in range(int(bl), int(br)):
                if self._rgba(surface, bbox, px, py) == (
                    255, *self._DARK_DIGIT
                ):
                    found = True
                    break
            if found:
                break
        assert found, "no white digit pixel inside the dark bubble"

    def test_auto_theme_inverts_the_system_scheme_per_paint(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        from PySide6.QtCore import Qt
        assert mgr._badge_theme == "auto"
        # System DARK theme -> LIGHT (white) bubble.
        with patch.object(
            mod, "_system_color_scheme", return_value=Qt.ColorScheme.Dark
        ):
            surface, bbox, placement, _control = self._compose(mgr)
            fx, fy = self._fill_sample(mod, placement)
            assert self._rgba(surface, bbox, fx, fy) == (
                255, *self._LIGHT_FILL
            )
        # System LIGHT theme -> DARK (near-black) bubble, on the next paint.
        with patch.object(
            mod, "_system_color_scheme", return_value=Qt.ColorScheme.Light
        ):
            surface, bbox, placement, _control = self._compose(mgr)
            fx, fy = self._fill_sample(mod, placement)
            assert self._rgba(surface, bbox, fx, fy) == (
                255, *self._DARK_FILL
            )

    def test_adjacent_badge_draws_tail_toward_the_control(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mgr._badge_theme = "light"
        # Default trailing-space placement: the badge box sits flush against
        # the control's right edge (gap 0 -> adjacent -> pointer tail).
        surface, bbox, placement, control = self._compose(mgr)
        bw, bh, (bl, bt, br, bb) = placement
        assert mod._bubble_drawing_state((bl, bt, br, bb), control, 1.0) == (
            "adjacent"
        )
        # The tail spans from the bubble's left edge across the box margin to
        # 3 logical px inside the control. At the control's right edge, on the
        # bubble's vertical center line, the tail interior is fill-colored.
        cy = (bt + bb) / 2.0
        cr = control[2]
        found = False
        for px in range(int(cr) - 1, int(bl + self._margin(mod)) + 1):
            if self._rgba(surface, bbox, px, cy) == (255, *self._LIGHT_FILL):
                found = True
                break
        assert found, "no fill-colored tail pixel between control and bubble"

    def test_detached_badge_draws_a_leader_line(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        mgr._badge_theme = "light"
        # Pin a detached placement (as a collision nudge or monitor-edge shift
        # would produce): the badge box 100 px right of the control.
        detached = (400.0, 100.0, 435.0, 135.0)
        with patch.object(
            mgr, "_numeral_badge_placement_phys", return_value=detached
        ):
            surface, bbox, placement, control = self._compose(mgr)
        bw, bh, footprint = placement
        assert footprint == detached
        assert mod._bubble_drawing_state(footprint, control, 1.0) == "detached"
        # The leader line runs from the center of the bubble edge nearest
        # the target to the CENTER of the control's on-monitor part
        # (wh-taskbar-badge-mispoint) -- not to the nearest point on the
        # control's rectangle, which for a wide control is blank space at
        # its edge. At the segment's midpoint, the topmost stroke is the
        # fill-color core over the border-color outer stroke -- accept
        # either (antialiasing decides which row the sample lands on).
        margin = self._margin(mod)
        start_x = detached[0] + margin
        start_y = (detached[1] + detached[3]) / 2.0
        target_x = (control[0] + control[2]) / 2.0
        target_y = (control[1] + control[3]) / 2.0
        mid_x = (start_x + target_x) / 2.0
        mid_y = (start_y + target_y) / 2.0
        acceptable = {
            (255, *self._LIGHT_FILL), (255, *self._LIGHT_BORDER),
        }
        found = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if self._rgba(
                    surface, bbox, mid_x + dx, mid_y + dy
                ) in acceptable:
                    found = True
        assert found, "no leader-line pixel at the line's midpoint"

    def test_leader_endpoint_is_control_center(self, overlay_mgr):
        # wh-taskbar-badge-mispoint: a taskbar button reports a full-row
        # rectangle with its visible icon centered inside, so the old
        # nearest-point endpoint landed on blank pixels at the row's edge.
        _mgr, mod, _ = overlay_mgr
        bubble = (300.0, 120.0, 350.0, 160.0)
        control = (500.0, 100.0, 1000.0, 200.0)
        (pax, pay), (pbx, pby) = mod._leader_anchor_points(
            bubble, control, 1920.0, 1080.0
        )
        assert (pbx, pby) == (750.0, 150.0)
        # The line leaves the bubble from the edge center nearest the target.
        assert (pax, pay) == (350.0, 140.0)

    def test_leader_endpoint_uses_on_monitor_part(self, overlay_mgr):
        _mgr, mod, _ = overlay_mgr
        bubble = (300.0, 120.0, 350.0, 160.0)
        control = (500.0, 100.0, 1000.0, 200.0)
        # The monitor ends at x=800: the off-monitor part is invisible, so
        # the line aims at the visible part's center.
        _pa, (pbx, pby) = mod._leader_anchor_points(
            bubble, control, 800.0, 1080.0
        )
        assert (pbx, pby) == (650.0, 150.0)

    def test_leader_endpoints_distinct_for_adjacent_cells(self, overlay_mgr):
        # The converging-lines symptom: two stacked full-width rows share a
        # corner, and nearest-point endpoints from far-left bubbles clamped
        # to that SAME shared corner. Center endpoints are distinct.
        _mgr, mod, _ = overlay_mgr
        upper = (500.0, 100.0, 1000.0, 172.0)
        lower = (500.0, 172.0, 1000.0, 244.0)
        bubble_a = (300.0, 60.0, 350.0, 100.0)
        bubble_b = (300.0, 250.0, 350.0, 290.0)
        _a, end_a = mod._leader_anchor_points(bubble_a, upper, 1920.0, 1080.0)
        _b, end_b = mod._leader_anchor_points(bubble_b, lower, 1920.0, 1080.0)
        assert end_a != end_b


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
                "overlay_badge_theme": "dark",
            }
        }
        overlay_ctor = self._build_manager(config)
        # Three managers are now constructed -- the numbered overlay, the
        # dedicated working-badge overlay (wh-dictation-retraction-indicator.3),
        # and the dedicated mouse-grid overlay (wh-grid-paint-mode) -- so
        # assert the settings reached a construction (not that there was only
        # one). The grid's own styling is hard-coded, so it takes only
        # badge_shadow and is not the call matched below.
        assert overlay_ctor.call_count == 3
        overlay_ctor.assert_any_call(
            badge_font_pt=32,
            badge_shadow=False,
            badge_corner="bottom_left",
            badge_trailing_space=False,
            badge_theme="dark",
        )

    def test_missing_click_block_uses_validated_defaults(self, qapp):
        # No [click] block: ClickConfig.from_raw({}) yields the validated
        # defaults (font 10, no shadow), and those reach the manager.
        overlay_ctor = self._build_manager({})
        # Three managers (numbered overlay + working-badge overlay + mouse-grid
        # overlay); the first two carry the validated badge defaults.
        assert overlay_ctor.call_count == 3
        overlay_ctor.assert_any_call(
            badge_font_pt=8,
            badge_shadow=False,
            badge_corner="top_right",
            badge_trailing_space=True,
            badge_theme="auto",
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
            # Three managers (numbered overlay + working-badge overlay +
            # mouse-grid overlay); the first two carry the validated defaults.
            assert overlay_ctor.call_count == 3
            overlay_ctor.assert_any_call(
                badge_font_pt=8,
                badge_shadow=False,
                badge_corner="top_right",
                badge_trailing_space=True,
                badge_theme="auto",
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
        number, and REFUSES a real number -- numerals draw as speech bubbles
        on the monitor surface (wh-overlay-bubble-badges), so nothing can
        silently exercise the deleted numeral-image path."""
        mgr, mod, _ = overlay_mgr
        sentinel_img = QImage(3, 3, QImage.Format.Format_ARGB32_Premultiplied)
        with patch.object(
            mgr, "_render_working_glyph", return_value=sentinel_img
        ) as glyph_mock:
            out = mgr._render_badge(mod.WORKING_BADGE_NUMBER, width=40, height=40)
        glyph_mock.assert_called_once_with(40, 40, 1.0)
        assert out is sentinel_img
        # A real number does NOT route to the glyph: it raises.
        with patch.object(mgr, "_render_working_glyph") as glyph_mock2:
            with pytest.raises(ValueError):
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
        half_w = mod.WORKING_BADGE_WIDTH_LOGICAL_PX // 2
        half_h = mod.WORKING_BADGE_LOGICAL_PX // 2
        assert bounds == (500 - half_w, 400 - half_h, 500 + half_w, 400 + half_h)
        # The badge carries the working sentinel (renders the glyph).
        assert [n for _, n in captured["badges"]] == [mod.WORKING_BADGE_NUMBER]
        assert result["state"] == "painted"

    def test_working_badge_off_monitor_near_edge_paints_nothing(
        self, overlay_mgr
    ):
        """A working-badge center point off EVERY monitor must paint nothing,
        even when the center sits within half a badge of a monitor edge.

        The badge box is ``WORKING_BADGE_WIDTH_LOGICAL_PX * dpr`` wide
        centered on the point, so a center just past a monitor edge (e.g.
        x=-10) builds a box that still OVERLAPS that monitor. The real
        resolver returns a valid rect for an overlapping box, so the old code
        painted a CLIPPED working glyph and reported a painted monitor for an
        off-screen / stale point (wh-overlay-4bug-review-r2.1).
        paint_working_badge must detect that the center is on no monitor and
        emit the normal no-monitor 'painted' event instead of constructing a
        badge box.

        Uses the REAL resolver (not a mock) so the overlap is genuine: with a
        22-logical-px-wide badge at dpr 1.0 the box spans x=-21..1, overlapping
        the monitor's x=0..1.
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

        On a 200% monitor (dpr 2.0) a WORKING_BADGE_LOGICAL_PX-logical-px badge
        must build a box TWICE that size in physical px centered on the
        point, NOT a constant physical size.
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
        # Physical box is logical(WORKING_BADGE_WIDTH_LOGICAL_PX,
        # WORKING_BADGE_LOGICAL_PX) * dpr(2), centered on the point.
        half_w = mod.WORKING_BADGE_WIDTH_LOGICAL_PX
        half_h = mod.WORKING_BADGE_LOGICAL_PX
        bounds = resolve_mock.call_args.args[0]
        assert bounds == (1000 - half_w, 800 - half_h, 1000 + half_w, 800 + half_h)

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


_HEBREW_SAVE = "שמור"  # bidi class R
_ARABIC_SAVE = "حفظ"  # bidi class AL


class TestWideRowLeadingGutter:
    """A row at least ten times wider than tall gets its numeral in the
    LEADING gutter -- beside the edge where its label starts -- instead of
    past the far trailing edge (wh-vscode-menu-badge-misplaced). A VS Code
    menu row is 861 pixels wide, so today's trailing placement puts the
    number about 870 pixels from the word it labels.

    The rule applies only when ALL of these hold: the row is not marked
    ``bounds_outside_menu``; its physical rect lies fully on its monitor; and
    width >= 10 x height. Leading is left for left-to-right text and right
    for right-to-left text, from the first strong bidi character of the
    name. A gutter that is off the monitor or lands on another numbered
    control or a placed badge falls through to today's placement. Every
    "keep today's placement" test compares against the SAME geometry as a
    plain OverlayPaintRect (no name, no mark), which is today's code path.
    """

    @staticmethod
    def _mon() -> _NativeMonitor:
        return _NativeMonitor(hmonitor=1, rect_phys=QRect(0, 0, 1920, 1080), dpi=96)

    @classmethod
    def _plain(cls, x, y, width, height) -> OverlayPaintRect:
        return OverlayPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=cls._mon(), hmonitor=1, screen=None,
        )

    @classmethod
    def _row(cls, mod, x, y, width, height, *, name="Save", suspect=False):
        return mod._TargetPaintRect(
            x=x, y=y, width=width, height=height,
            monitor=cls._mon(), hmonitor=1, screen=None,
            target_name=name, bounds_outside_menu=suspect,
        )

    @staticmethod
    def _place(mgr, rect, *, corner="top_right", ctrl=None, placed=None):
        return mgr._numeral_badge_placement_phys(
            rect, 20, 30, 1.0, 1920, 1080, list(ctrl or []),
            corner=corner, placed_badges=placed,
        )

    def _today(self, mgr, x, y, width, height, **kwargs):
        return self._place(mgr, self._plain(x, y, width, height), **kwargs)

    # -- the rule applies ---------------------------------------------------

    def test_wide_valid_ltr_row_uses_left_gutter(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # 600 x 40 is 15:1. Today: past the right edge at x=900.
        assert self._today(mgr, 300, 200, 600, 40) == (900.0, 200.0, 920.0, 230.0)
        placement = self._place(mgr, self._row(mod, 300, 200, 600, 40))
        # The badge's right edge sits on the row's left edge.
        assert placement == (280.0, 200.0, 300.0, 230.0)

    def test_wide_valid_rtl_hebrew_row_uses_right_gutter(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # A left corner makes today's trailing spot the LEFT gutter, so an RTL
        # row moving to the right proves the side follows the text direction.
        assert self._today(mgr, 300, 200, 600, 40, corner="top_left") == (
            280.0, 200.0, 300.0, 230.0,
        )
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40, name=_HEBREW_SAVE),
            corner="top_left",
        )
        assert placement == (900.0, 200.0, 920.0, 230.0)

    def test_wide_valid_rtl_arabic_row_uses_right_gutter(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40, name=_ARABIC_SAVE),
            corner="top_left",
        )
        assert placement == (900.0, 200.0, 920.0, 230.0)

    def test_ltr_row_uses_left_gutter_under_left_corner_bottom(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # The gutter's vertical alignment follows the corner, like ladder
        # step 1: a bottom corner bottom-aligns it.
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40), corner="bottom_right",
        )
        assert placement == (280.0, 210.0, 300.0, 240.0)

    def test_first_strong_character_decides_direction(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # Digits and spaces are weak or neutral; the first STRONG character
        # is Hebrew, so the row is right-to-left.
        rtl = self._place(
            mgr, self._row(mod, 300, 200, 600, 40, name="12 " + _HEBREW_SAVE),
            corner="top_left",
        )
        assert rtl == (900.0, 200.0, 920.0, 230.0)
        # A Latin letter first: left-to-right, even with Hebrew after it.
        ltr = self._place(
            mgr, self._row(mod, 300, 200, 600, 40, name="Save " + _HEBREW_SAVE),
        )
        assert ltr == (280.0, 200.0, 300.0, 230.0)

    def test_name_without_strong_character_is_ltr(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(mgr, self._row(mod, 300, 200, 600, 40, name="12"))
        assert placement == (280.0, 200.0, 300.0, 230.0)
        empty = self._place(mgr, self._row(mod, 300, 200, 600, 40, name=""))
        assert empty == (280.0, 200.0, 300.0, 230.0)

    def test_exactly_ten_to_one_applies(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(mgr, self._row(mod, 300, 200, 400, 40))
        assert placement == (280.0, 200.0, 300.0, 230.0)

    def test_vscode_like_twelve_to_one_row_applies(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # 861 x 72: about the measured VS Code menu row width.
        placement = self._place(mgr, self._row(mod, 40, 300, 861, 72, name="Exit"))
        assert placement == (20.0, 300.0, 40.0, 330.0)

    def test_threshold_constant_is_ten(self, overlay_mgr):
        _mgr, mod, _ = overlay_mgr
        assert mod._WIDE_ROW_MIN_ASPECT == 10

    # -- the rule does not apply: today's placement, unchanged --------------

    def test_just_under_ten_to_one_keeps_todays_placement(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(mgr, self._row(mod, 300, 200, 399, 40))
        assert placement == self._today(mgr, 300, 200, 399, 40)
        assert placement == (699.0, 200.0, 719.0, 230.0)

    def test_suspect_row_keeps_todays_placement(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40, suspect=True),
        )
        assert placement == self._today(mgr, 300, 200, 600, 40)
        assert placement == (900.0, 200.0, 920.0, 230.0)

    def test_row_past_right_monitor_edge_keeps_todays_placement(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # 1400..2100 hangs past the 1920 edge; its left gutter is on-screen
        # and clean, so only the on-monitor test keeps today's placement.
        placement = self._place(mgr, self._row(mod, 1400, 200, 700, 40))
        assert placement == self._today(mgr, 1400, 200, 700, 40)
        assert placement[0] != 1380.0

    def test_row_past_bottom_monitor_edge_keeps_todays_placement(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(mgr, self._row(mod, 300, 1060, 600, 40))
        assert placement == self._today(mgr, 300, 1060, 600, 40)
        assert placement[0] != 280.0

    def test_row_past_top_monitor_edge_keeps_todays_placement(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        placement = self._place(mgr, self._row(mod, 300, -10, 600, 40))
        assert placement == self._today(mgr, 300, -10, 600, 40)
        assert placement[0] != 280.0

    def test_rtl_row_past_left_monitor_edge_keeps_todays_placement(
        self, overlay_mgr
    ):
        mgr, mod, _ = overlay_mgr
        # -100..500: the RTL gutter at 500..520 is on-screen and clean.
        placement = self._place(
            mgr, self._row(mod, -100, 200, 600, 40, name=_HEBREW_SAVE),
            corner="top_left",
        )
        assert placement == self._today(mgr, -100, 200, 600, 40, corner="top_left")
        assert placement[0] != 500.0

    def test_gutter_off_monitor_falls_through(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # Row at x=10: the left gutter would start at x=-10.
        placement = self._place(mgr, self._row(mod, 10, 200, 600, 40))
        assert placement == self._today(mgr, 10, 200, 600, 40)
        assert placement == (610.0, 200.0, 630.0, 230.0)

    def test_gutter_blocked_by_numbered_control_falls_through(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        neighbour = (270.0, 190.0, 295.0, 250.0)  # overlaps 280..300 x 200..230
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40), ctrl=[neighbour],
        )
        assert placement == self._today(mgr, 300, 200, 600, 40, ctrl=[neighbour])
        assert placement == (900.0, 200.0, 920.0, 230.0)

    def test_gutter_blocked_by_placed_badge_falls_through(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        earlier = [(285.0, 205.0, 300.0, 225.0)]
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40), placed=earlier,
        )
        assert placement == self._today(mgr, 300, 200, 600, 40, placed=earlier)
        assert placement == (900.0, 200.0, 920.0, 230.0)

    def test_own_row_box_does_not_block_the_gutter(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        # The caller passes every badge's box, the row's own included; the
        # gutter shares one edge with it and must not count as blocked.
        placement = self._place(
            mgr, self._row(mod, 300, 200, 600, 40),
            ctrl=[(300.0, 200.0, 900.0, 240.0)],
        )
        assert placement == (280.0, 200.0, 300.0, 230.0)

    # -- direction helper ---------------------------------------------------

    def test_name_is_right_to_left(self, overlay_mgr):
        _mgr, mod, _ = overlay_mgr
        assert mod._name_is_right_to_left(_HEBREW_SAVE) is True
        assert mod._name_is_right_to_left(_ARABIC_SAVE) is True
        assert mod._name_is_right_to_left("  3 " + _ARABIC_SAVE) is True
        assert mod._name_is_right_to_left("Save") is False
        assert mod._name_is_right_to_left("Save " + _HEBREW_SAVE) is False
        assert mod._name_is_right_to_left("123 ...") is False
        assert mod._name_is_right_to_left("") is False

    # -- end to end through paint() -----------------------------------------

    def _paint_one(self, mgr, mod, item):
        mon = _native_monitor(10)
        with patch.object(
            mod, "_enumerate_native_monitors", return_value=[mon]
        ), patch.object(mod, "_screens", return_value=[]), patch.object(
            mgr, "_render_monitor_surface", side_effect=_surface_stub
        ), patch.object(
            mod, "build_layered_dib", return_value=MagicMock()
        ), patch.object(
            mod, "composite_layered_window", return_value=True
        ):
            # The REAL resolver runs, so the name and the mark must survive
            # the resolve step inside _do_paint.
            result = mgr.paint(
                _make_summary([item]), overlay_session_id=1, paint_generation=0,
            )
        assert result["state"] == "painted"
        return mgr._windows[10].geom_phys

    def test_paint_places_wide_row_badge_in_leading_gutter(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        item = WalkSnapshotSummaryItem(
            item_id="uia-1", display_number=1, name="Save", role="MenuItem",
            bounds=(300, 200, 600, 40), monitor_id=10,
        )
        geom = self._paint_one(mgr, mod, item)
        # The surface is the union of the row and its badge plus the margin;
        # a badge left of the row pulls the surface's left edge past it.
        assert geom.left() < 300 - mod._SURFACE_MARGIN_PX

    def test_paint_keeps_todays_placement_for_marked_row(self, overlay_mgr):
        mgr, mod, _ = overlay_mgr
        item = WalkSnapshotSummaryItem(
            item_id="uia-1", display_number=1, name="Exit", role="MenuItem",
            bounds=(300, 200, 600, 40), monitor_id=10, bounds_outside_menu=True,
        )
        geom = self._paint_one(mgr, mod, item)
        # Today's badge sits past the right edge: the surface starts at the
        # row's own left edge minus the margin and ends past the row.
        assert geom.left() == 300 - mod._SURFACE_MARGIN_PX
        assert geom.right() > 900
