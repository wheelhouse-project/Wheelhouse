"""System tray GUI and floating button interface for WheelHouse.

This module implements the graphical user interface components for WheelHouse,
including a system tray icon with context menu and an optional floating button
overlay. The GUI provides real-time status feedback, configuration access, and
manual control over WheelHouse features through a clean, minimalist interface.

Key Classes:
  - FloatingButton: Draggable overlay button with context menu.
  - WheelHouseTrayApp: System tray application with status management.
  - GuiController: Main GUI coordinator for the GUI process.

Key Features:
  - System tray integration with colored status indicators
  - Floating overlay button with customizable appearance
  - Context menus for feature toggle and configuration
  - Cross-process state synchronization
  - Desktop notifications for status changes

Key Functions:
  - start_gui_process: Entry point for the GUI subprocess.
  - create_icon_image: Utility for generating tray icons.

Typical Usage:
  # Started automatically by launcher
  from gui import start_gui_process
  start_gui_process(state_queue, gui_ready_event)
"""

# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()

import multiprocessing
from multiprocessing import Queue, shared_memory
from multiprocessing.synchronize import Event
import logging

from shared.dialog_owner import STARTUP_OWNER
from utils.redact import redact_transcript
from utils.app_version import get_app_version
from floating_button_geometry import (
    MAX_BUTTON_SIZE,
    MIN_BUTTON_SIZE,
    correct_onto_any_screen,
    correct_onto_screen,
    is_in_resize_ring,
    resize_from_pointer,
)
import sys
import struct
import json
import uuid
import math
from pathlib import Path
from queue import Empty, Full
import threading
import time
import uuid
from functools import partial

# --- Qt and PySide6 Imports ---
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QDialog, QLabel, QVBoxLayout, QFrame, QMessageBox
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QObject
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QAction, QFont, QPixmap, QGuiApplication

# --- System Tray Imports ---
import pystray
from PIL import Image, ImageDraw

# --- Notification Imports ---
# wh-notice-length-guard: every notice goes through send_notice, which
# measures the text against the fixed-size fields plyer writes it into.
# Calling plyer from here again would walk around that measurement, and
# the failure is silent -- see utils/notice_text.py.
from utils.notice_text import send_notice

logger = logging.getLogger(__name__)

# Windows 11 gives every top-level window rounded corners and a thin border of
# its own, drawn by the desktop compositor rather than by the application. The
# floating button is a top-level frameless window, so it inherited a grey ring
# in every colour state even though paintEvent draws the circle with NoPen and
# never strokes an outline. These are the documented DwmSetWindowAttribute
# constants used to switch both off for that one window; they require Windows 11
# build 22000 or later and are rejected on Windows 10, which is harmless (the
# button just keeps the border it already had).
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_DONOTROUND = 1
_DWMWA_BORDER_COLOR = 34
_DWMWA_COLOR_NONE = 0xFFFFFFFE


def _grant_foreground_to_any_process() -> None:
    """Grant any process the right to call SetForegroundWindow once.

    Called from the Try-it-anyway click handler before the IPC chain
    delivers the click to the Input process, which then refocuses the
    originally-rejected target. The Input process is two IPC hops away
    from the user input event and Windows refuses SetForegroundWindow
    from any process that does not currently hold the foreground or
    have recent user-input attribution. The GUI process holds both at
    the moment a toast button is clicked, so it is allowed to call
    AllowSetForegroundWindow on behalf of the Input process. ASFW_ANY
    is the documented constant (DWORD -1) that grants the right to any
    process for one SetForegroundWindow call. The grant is consumed by
    that next call (success or failure) so a stale grant does not
    linger. See wh-override-paste-focus-drift round 2.
    """

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        # ASFW_ANY == DWORD(-1) == 0xFFFFFFFF.
        user32.AllowSetForegroundWindow(0xFFFFFFFF)
    except Exception as exc:
        logger.debug("AllowSetForegroundWindow grant failed: %s", exc)


TRAY_ICON_SIZE = 64


def create_icon_image(color_tuple):
    """Draw a plain filled circle at the tray icon's size.

    This used to be the tray icon itself, in a colour that followed whether
    speech was on. It is now only the stand-in for a build that does not ship
    WheelHouse.ico: a tray with no icon at all is harder to right-click than a
    plain shape.
    """
    width = TRAY_ICON_SIZE
    height = TRAY_ICON_SIZE
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, width - 8, height - 8), fill=color_tuple)
    return image


def load_tray_icon():
    """Return the Wheelhouse icon for the notification area.

    One picture, loaded once when the tray is created, and never replaced.
    The file sits beside this module in a source checkout and inside the
    bundle in a build, which is the same way the plaque image is found.
    """
    try:
        icon_path = Path(__file__).parent / "WheelHouse.ico"
        return (
            Image.open(icon_path)
            .convert("RGBA")
            .resize((TRAY_ICON_SIZE, TRAY_ICON_SIZE), Image.LANCZOS)
        )
    except Exception as exc:
        logger.warning("Could not load the tray icon file: %s", exc)
        return create_icon_image((120, 120, 120))


# wh-n29v.118 / wh-n29v.120.1: the numbered-overlay "walking" cue self-clear
# fallback timer must outlast the ACTUAL Logic-side success-path latency. On the
# success path the cue is cleared by the GUI when paint_overlay arrives, and
# paint_overlay is enqueued only AFTER both the build's send_request AND the
# PIN_SNAPSHOT ack await complete -- the send_request bounded by
# click_config.screen_read_timeout_ms for a walk build (WALK / REFRESH /
# SETTLE) or click_config.response_timeout_ms for AUTO_OPEN, the pin ack
# always bounded by response_timeout_ms (default 3000; validated only by
# _is_int_at_least(100) -- NO upper bound). So the bound the cue must survive
# is the build's own awaited window plus response_timeout_ms for the pin, plus
# the sentence-wait bound for a walk build (wh-overlay-slow-uia-stale-
# badges.3.1.1). Logic carries that combined value in the overlay_walk_cue
# payload as walk_timeout_ms (``_overlay_dispatch_build``'s ``cue_bound_ms``),
# so an operator who raises those keys does not get the cue cleared before the
# numbers paint. When the field is absent or not a usable int, the cue falls
# back to this default.
_WALK_CUE_DEFAULT_WALK_MS = 3000
# The buffer the fallback adds on top of the carried walk_timeout_ms bound. It
# must comfortably exceed the up-to-100ms GUI poll interval on the cue's own
# round trip (the active:True receipt rides the 100ms state_from_logic_queue
# poll) plus Qt timer scheduling slack, so the fallback never fires before a
# legitimately-completing success path's paint_overlay (which also rides the
# 100ms poll) is delivered. The walk + pin latency itself is already in
# walk_timeout_ms (see above); this buffer only covers the poll + scheduling
# slack on top. ~1000ms leaves wide margin.
_WALK_CUE_FALLBACK_BUFFER_MS = 1000
# wh-n29v.119.1: QTimer.start takes a signed 32-bit int (milliseconds), so the
# largest interval Qt accepts is INT32_MAX. response_timeout_ms (carried as
# walk_timeout_ms) is validated only by _is_int_at_least(100) -- it has NO upper
# bound -- so an operator who sets a very large value would otherwise make
# QTimer.start raise OverflowError. The fallback interval is clamped to this
# ceiling so the timer always arms with a value Qt can hold.
_QT_TIMER_MAX_INTERVAL_MS = 2147483647

# wh-overlay-slow-uia-stale-badges.9: the numbered-overlay badge lease. An
# accepted paint arms a single-shot lease timer at this interval; Logic's
# keepalive tick renews it (overlay_lease_renew carries its own lease_ms);
# an accepted clear cancels it. If the lease expires -- Logic crashed, hung,
# or its clear was lost -- the GUI tears the badges down itself and reports
# state="expired". The default must comfortably exceed the renew cadence
# (Logic renews every keepalive tick, default 15s, with lease_ms >= 60s) so
# a healthy Logic never lets it fire. Class-level default, not a config key.
_OVERLAY_LEASE_DEFAULT_MS = 90000

# wh-overlay-slow-uia-stale-badges.18.4: interval between GUI-side retries of
# a teardown that left a surviving badge window (a DestroyWindow failed).
# Single-shot; re-armed after every still-pending retry, so the retries run
# unbounded until the sweep ends clean -- a repeated no-op is cheap.
_OVERLAY_TEARDOWN_RETRY_MS = 2000

# How long after a screen-layout signal the floating button's position is
# checked once more. Probe 4 (2026-09-20, wh-floating-button-offscreen)
# measured why one delayed check is needed: a single resolution change
# produced four signals, and the first three all read the screen at the OLD
# device pixel ratio and found nothing wrong. The signal that saw the new
# ratio arrived 809 ms after the first one going down in resolution, and
# 725 ms coming back up, so this covers both with room to spare.
_FORCED_REAPPLY_DELAY_MS = 1500

# wh-dictation-gate-ux: the uncertain-category rejection notice
# ("Wheelhouse isn't sure it can type here", the only notice with the
# Try-it-anyway button) is disabled until the dictation-gate UX
# redesign lands. David reviewed the flow on 2026-08-04 and found it
# confusing end to end. The suppression lives here in the GUI, NOT in
# the Input-process emission gate
# (shared/rejection_category.py:should_emit_notice), so the event
# emission, word aggregation, text cache, and Try-it-anyway retry
# machinery stay in place and tested; the redesign re-enables the flow
# by flipping this one constant. The elevated (administrator boundary)
# notice is unaffected.
SUPPRESS_UNCERTAIN_REJECTION_NOTICE = True


def _screen_bounds(exclude=None):
    """Return every connected screen as ``(x, y, width, height)``.

    The one place the floating button's stored position meets Qt's screen
    list, so tests replace this rather than a display (the same seam
    ``overlay_paint_window._screens`` uses). The rectangles are ``geometry``,
    the whole screen, and not ``availableGeometry``: the button is an
    always-on-top frameless window, so it stays visible and clickable over
    the taskbar and over every docked appbar strip, and a user may park it
    there on purpose. Measuring it against the area reserved for ordinary
    windows would move a button the user can see. The rectangles are already
    in the logical pixels ``QWidget.move`` takes, so no DPI arithmetic
    belongs here.

    ``exclude`` drops one screen object. ``screenRemoved`` hands over the
    screen Qt is about to drop, and whether ``screens()`` still lists it at
    that moment is Qt's business; excluding the object by identity means the
    correction never depends on the answer.
    """
    bounds = []
    for screen in QGuiApplication.screens():
        if exclude is not None and screen is exclude:
            continue
        rect = screen.geometry()
        bounds.append((rect.x(), rect.y(), rect.width(), rect.height()))
    return bounds


class FloatingButton(QWidget):
    left_clicked = Signal()
    press_started = Signal()
    press_ended = Signal()
    press_cancelled = Signal()
    drag_started = Signal()
    double_clicked = Signal()
    closed = Signal()
    size_changed = Signal(int)
    moved = Signal(QPoint)
    resize_finished = Signal(int, QPoint)
    # Sent once whenever a gesture stops, by whichever route it stopped --
    # including the routes that save nothing. The manager holds back the
    # stored size and position while a gesture is running, so it needs to know
    # the moment nothing is running any more.
    gesture_ended = Signal()
    context_menu_requested = Signal(QPoint)

    def __init__(self, initial_size=50):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(initial_size, initial_size)

        self._is_enabled = False
        self._is_indeterminate = True
        self._is_ptt_mode = False
        self._ptt_feedback = ""
        self._is_dragging = False
        self._drag_position = QPoint(0, 0)
        self._initial_press_pos = None

        # Edge-drag resizing. A press on the outer ring becomes a resize and
        # nothing else -- notably it never emits press_started, so the
        # push-to-talk hold timer cannot start from grabbing the edge. The
        # centre recorded here stays fixed for the whole drag so the button
        # grows and shrinks evenly around the point it occupied.
        self._is_resizing = False
        # While a gesture runs, this button asks Windows to send it every
        # mouse event. Five separate defects in this feature were all the same
        # accident: the release went to another window -- the right-click
        # menu's own loop, a window that took focus, the button being hidden --
        # so the gesture was never told it had ended, and what it left behind
        # did damage later. Holding the mouse removes the cause instead of
        # patching each route the release can take.
        #
        # The hold is given back on every exit, and the "the button is no
        # longer down" checks in the move handler stay as a backstop for a
        # hold that never took effect. A hold nobody gives back leaves the
        # whole desktop unable to click anything, which for this program would
        # be worse than the defect it prevents.
        self._mouse_held = False
        # True from the moment a gesture starts until nothing is running.
        self._gesture_running = False
        # The button appears on screen before its stored size and position
        # arrive from the Logic process. A gesture begun in that gap measures
        # against the default geometry, and the arriving state then replaces
        # the size and position underneath it. The manager already ignores
        # presses until the state arrives; this is the same rule for the
        # gestures the manager never sees.
        self._accepts_gestures = False
        # Where the previous left press landed. Qt pairs a press with whatever
        # press came before it, so a grab of the edge followed by a press in
        # the middle arrives as a double-click on the middle. Remembering the
        # first one is what lets the double-click handler tell that pair apart
        # from two real presses in the middle.
        self._last_press_was_on_edge = False
        self._resize_centre = QPoint(0, 0)
        self._resize_start_size = initial_size
        self._resize_start_pos = QPoint(0, 0)
        self._resize_screen = None
        self._resize_cursor_active = False

        # Activity state for speech feedback
        self._activity_state = 'idle'  # 'idle', 'hearing', 'confirmed'
        self._pulse_phase = 0.0  # 0.0 to 1.0 for pulse animation

        # wh-n29v.117: numbered-overlay "walking" progress cue. A composable
        # glyph drawn on top of the base ellipse while a walk is in flight, so
        # it coexists with the recording/hearing/confirmed visuals (it means
        # "we heard you, working", not a recording state). Driven by Logic via
        # the "overlay_walk_cue" state-queue action; self-clears on the
        # single-shot timeout below even if no active:False ever arrives.
        self._walk_active = False

        # Pulse animation timer
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        # Flash timer for confirmed state
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)

        # wh-n29v.117 / wh-n29v.118: single-shot fallback that force-clears the
        # walk cue. MANDATORY because a fresh-walk TIMEOUT sends NO clear_overlay
        # to the GUI, so without this timer the cue could stick on screen. The
        # interval is armed in set_walk_cue from the cue bound
        # ``_overlay_dispatch_build`` computes (the build's own awaited window
        # plus response_timeout_ms, plus the sentence-wait bound for a walk
        # build, carried in the payload) plus _WALK_CUE_FALLBACK_BUFFER_MS --
        # NOT a hardcoded value -- so raising those keys does not clear the cue
        # mid-walk (see the module constants above for the full rationale).
        self._walk_timeout_timer = QTimer(self)
        self._walk_timeout_timer.setSingleShot(True)
        self._walk_timeout_timer.timeout.connect(self._on_walk_timeout)

        # Topmost re-assertion timer (Windows can displace topmost windows)
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        self._topmost_timer.start(3000)  # Every 3 seconds

        self.setMouseTracking(True)

    def set_state(self, enabled: bool):
        """Update button state (recording active/inactive) and repaint."""
        self._is_enabled = enabled
        self.update()

    def set_indeterminate(self, indeterminate: bool):
        """Set indeterminate state (grey when STT status unknown) and repaint."""
        self._is_indeterminate = indeterminate
        self.update()

    def set_ptt_mode(self, ptt_mode: bool):
        """Set push-to-talk mode indicator and repaint."""
        self._is_ptt_mode = ptt_mode
        self.update()

    def set_ptt_feedback(self, state: str, description: str):
        """Expose requested/refused listening without claiming recording."""
        self._ptt_feedback = state
        self.setAccessibleName("WheelHouse microphone")
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def set_size(self, diameter: int):
        """Resize button to specified diameter (preserves circular shape)."""
        self.setFixedSize(diameter, diameter)
        self.update()

    def _cancel_press_in_flight(self):
        """Forget a press whose release never arrived, without acting on it.

        This is not the same as the press ending. A real release means the user
        finished what they were doing, so it clicks or stops push-to-talk. An
        abandoned press means the opposite: nothing was decided. Reporting it as
        a release would turn speech ON, because a quick press that never
        reaches the hold threshold is treated as a click. So the button reports
        a cancellation instead, and the manager stops the hold timer and puts
        the microphone back the way it was.
        """
        if self._initial_press_pos is None and not self._is_dragging:
            return
        self._is_dragging = False
        self._initial_press_pos = None
        self.press_cancelled.emit()
        self._settle_after_gesture()

    def _end_gesture_in_flight(self):
        """End whatever gesture is still marked live, whichever one it is.

        A gesture normally ends on the mouse release. Several endings deliver
        no release at all: the button is hidden mid-drag, the context menu's
        modal loop grabs the mouse and takes the release with it, or the user
        simply presses again. Each of those has to end BOTH kinds of gesture,
        because either one can be the one left in flight. A resize is finished
        (so the size the user can see is the size that gets saved) and a press
        is cancelled (so the microphone does not stay open).
        """
        self._finish_resize()
        self._cancel_press_in_flight()

    def set_ready_for_gestures(self, ready: bool):
        """Allow or refuse gestures. Off until the stored geometry arrives."""
        self._accepts_gestures = bool(ready)

    def _hold_mouse(self):
        """Ask Windows to send every mouse event here until further notice."""
        if self._mouse_held:
            return
        self._mouse_held = True
        # A window that is not on screen cannot hold the mouse, and asking
        # prints a warning. The record is kept either way, so the giving-back
        # path runs whatever happened, and the move handler's backstop covers
        # the case where the hold did not take.
        if self.isVisible():
            self.grabMouse()

    def _let_mouse_go(self):
        """Give the mouse back. Safe to call when it was never held."""
        if not self._mouse_held:
            return
        self._mouse_held = False
        self.releaseMouse()

    def _settle_after_gesture(self):
        """Give the mouse back and report the end, once nothing is running.

        Called from every place a gesture can stop. It checks rather than
        assumes, so a call made while another gesture is still live does
        nothing -- which is what makes it safe to call from the shared paths
        that end one gesture while starting another.
        """
        if (
            self._is_resizing
            or self._is_dragging
            or self._initial_press_pos is not None
        ):
            return
        self._let_mouse_go()
        if self._gesture_running:
            self._gesture_running = False
            self.gesture_ended.emit()

    def _begin_press(self, global_point):
        """Start the press that may become a click, a hold, or a move."""
        self._initial_press_pos = global_point
        self._gesture_running = True
        self._hold_mouse()
        self.press_started.emit()

    def _begin_resize(self):
        """Record everything a resize drag measures against, from right now.

        Both ways of starting a resize come through here: an ordinary press on
        the ring, and the second press of a double-click, which Qt delivers to
        a different handler. Sharing one starting point is what stops those two
        entry points from drifting apart.
        """
        self._is_resizing = True
        self._gesture_running = True
        self._hold_mouse()
        self._resize_start_size = self.width()
        self._resize_start_pos = self.pos()
        self._resize_centre = QPoint(
            self.pos().x() + self.width() // 2,
            self.pos().y() + self.height() // 2,
        )
        self._resize_screen = self._current_screen_bounds()

    def hideEvent(self, event):
        """Abandon any gesture in flight when the button leaves the screen."""
        self._end_gesture_in_flight()
        # Unconditionally, not only through the settling path. A hold left
        # behind by a window that is no longer on screen makes the whole
        # desktop unclickable, which is the one failure here bad enough to be
        # worth a second, order-independent guarantee.
        self._let_mouse_go()
        super().hideEvent(event)

    def showEvent(self, event):
        """Re-apply the borderless window style every time the button is shown.

        Qt destroys and re-creates the underlying window whenever window flags
        change, and a re-created window comes back with Windows' default border
        and rounded corners. Applying this once in __init__ would therefore be
        undone later, so it is re-issued on every show.

        This is also the point where any gesture left in flight is abandoned. A
        button that was never shown receives no hide event, so clearing the
        state only on hide would leave that case uncovered.
        """
        super().showEvent(event)
        self._end_gesture_in_flight()
        self._apply_borderless_window_style()

    def _apply_borderless_window_style(self):
        """Ask Windows to stop drawing a border and rounded corners on this window.

        Wheelhouse never paints a border itself (paintEvent uses NoPen), so the
        grey ring users saw around the button came from the Windows compositor.
        Both attributes need Windows 11 build 22000+; older builds reject them
        and the button simply keeps the border it had. Failure is logged at
        debug level and swallowed: this runs during GUI startup, and a cosmetic
        border is not worth taking down the tray and button surface.
        """
        try:
            import ctypes
            from ctypes import wintypes

            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

            window_handle = int(self.winId())

            corner_preference = ctypes.c_int(_DWMWCP_DONOTROUND)
            dwmapi.DwmSetWindowAttribute(
                window_handle,
                _DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(corner_preference),
                ctypes.sizeof(corner_preference),
            )

            border_color = ctypes.c_uint(_DWMWA_COLOR_NONE)
            dwmapi.DwmSetWindowAttribute(
                window_handle,
                _DWMWA_BORDER_COLOR,
                ctypes.byref(border_color),
                ctypes.sizeof(border_color),
            )
        except Exception as exc:
            logger.debug("floating button border removal failed: %s", exc)

    def _reassert_topmost(self):
        """Periodically re-assert topmost position.

        Windows 11 can displace topmost windows when fullscreen apps,
        elevated windows, or DWM compositing changes occur. This timer
        calls raise_() to bring the button back to the top of the Z-order.
        """
        if self.isVisible():
            self.raise_()

    def paintEvent(self, event):
        """Qt paint handler - renders color-coded circular indicator.

        Colors:
            - Dark Grey: Indeterminate (STT status unknown)
            - Blue: PTT mode idle (ready to hold)
            - Red: Enabled (actively recording speech)
            - Light Grey: Disabled (not recording)
            - Pulsing Red: Hearing speech (VAD triggered)
            - Green: Confirmed (utterance complete - flash)
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._ptt_feedback == "pending":
            color = QColor(230, 165, 20, 230)  # Amber: requested, not confirmed
        elif self._ptt_feedback == "refused":
            color = QColor(110, 65, 150, 230)  # Purple: not listening
        elif self._is_indeterminate:
            color = QColor(100, 100, 100, 220)  # Dark Grey
        elif not self._is_enabled and self._is_ptt_mode:
            color = QColor(50, 120, 200, 220)   # Blue (PTT mode idle)
        elif not self._is_enabled:
            color = QColor(160, 160, 160, 180)  # Light Grey (Not Recording)
        elif self._activity_state == 'confirmed':
            color = QColor(0, 200, 0, 220)  # Green flash
        elif self._activity_state == 'hearing':
            # Pulsing red↔orange - more noticeable than alpha variation
            pulse = (math.sin(self._pulse_phase * 2 * math.pi) + 1) / 2  # 0.0 to 1.0
            # Interpolate between red (200, 0, 0) and orange (255, 140, 0)
            r = int(200 + 55 * pulse)   # 200 → 255
            g = int(0 + 140 * pulse)    # 0 → 140
            b = 0
            color = QColor(r, g, b, 220)
        else:  # idle but enabled
            color = QColor(200, 0, 0, 220)  # Solid red (Recording)
            
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())

        if self._ptt_feedback in ("pending", "refused"):
            # Dark on the amber pending fill, white on the dark purple refused
            # fill. White on amber is 2.15:1, under the WCAG 3.0:1 minimum for
            # a graphical object, and this glyph is the only channel that is
            # not colour. (40, 40, 40) is the walk cue outline below.
            painter.setPen(QColor(40, 40, 40) if self._ptt_feedback == "pending" else QColor(255, 255, 255))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "..." if self._ptt_feedback == "pending" else "!")

        # wh-n29v.117: composable "walking" progress cue. Drawn AFTER the base
        # ellipse, on top, as a small white dot with a dark outline in the
        # lower-right quadrant. It is NOT a mutually-exclusive color branch: a
        # walk can overlap the hearing/confirmed/recording visuals and the cue
        # means "we heard you, working", so it must coexist with whatever base
        # color is showing.
        if self._walk_active:
            rect = self.rect()
            diameter = max(6, int(min(rect.width(), rect.height()) * 0.22))
            margin = max(2, int(min(rect.width(), rect.height()) * 0.10))
            x = rect.right() - margin - diameter
            y = rect.bottom() - margin - diameter
            painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
            painter.setPen(QPen(QColor(40, 40, 40, 235), max(1, diameter // 6)))
            painter.drawEllipse(x, y, diameter, diameter)

    def set_activity_state(self, state: str):
        """Set speech activity state: 'idle', 'hearing', 'confirmed'."""
        if state == self._activity_state:
            return
            
        self._activity_state = state
        
        if state == 'hearing':
            self._pulse_timer.start(50)  # 20 FPS pulse
        elif state == 'confirmed':
            self._pulse_timer.stop()
            self._flash_timer.start(400)  # Flash for 400ms
        else:  # idle
            self._pulse_timer.stop()
        
        self.update()
    
    def _pulse_tick(self):
        """Advance pulse animation phase."""
        self._pulse_phase = (self._pulse_phase + 0.1) % 1.0
        self.update()
    
    def _end_flash(self):
        """End confirmation flash, return to idle."""
        self._activity_state = 'idle'
        self.update()

    def set_walk_cue(self, active: bool, walk_timeout_ms=None):
        """Show or hide the numbered-overlay "walking" progress cue (wh-n29v.117).

        Logic drives this via the "overlay_walk_cue" state-queue action:
        active=True at walk-start (overlay states walk_in_flight /
        refresh_in_flight), active=False when the overlay is painted or the
        build fails / times out. The cue is a composable glyph drawn on top of
        the base button color in paintEvent, so it coexists with the
        recording/hearing/confirmed visuals.

        On active=True the single-shot timeout timer is (re)started so the cue
        self-clears even if no active=False message ever arrives -- a
        fresh-walk timeout sends no clear_overlay to the GUI. On active=False
        the timer is stopped.

        wh-n29v.118: ``walk_timeout_ms`` carries the GUI fallback bound
        ``_overlay_dispatch_build`` computes (the build's own awaited window --
        ``screen_read_timeout_ms`` for a walk, ``response_timeout_ms`` for
        AUTO_OPEN -- plus ``response_timeout_ms`` for the pin ack, plus the
        sentence-wait bound for a walk build) in the payload. The fallback
        timer is armed at that value plus ``_WALK_CUE_FALLBACK_BUFFER_MS`` so
        it always outlasts the real walk -- even when an operator raises
        those keys above the GUI default. When the value is absent or not a
        usable int (``bool`` is rejected even though it subclasses ``int``),
        it degrades to ``_WALK_CUE_DEFAULT_WALK_MS``.
        """
        active = bool(active)
        if active:
            if isinstance(walk_timeout_ms, int) and not isinstance(
                walk_timeout_ms, bool
            ) and walk_timeout_ms > 0:
                base_ms = walk_timeout_ms
            else:
                base_ms = _WALK_CUE_DEFAULT_WALK_MS
            # wh-n29v.119.1: clamp the interval to the Qt signed-32-bit timer
            # range, and arm the timer BEFORE marking the cue active so we can
            # refuse by default: if the start still raises for any reason,
            # leave the cue inactive rather than stranding the dot on screen
            # with no fallback timer to clear it.
            interval_ms = min(
                base_ms + _WALK_CUE_FALLBACK_BUFFER_MS, _QT_TIMER_MAX_INTERVAL_MS
            )
            try:
                self._walk_timeout_timer.start(interval_ms)
            except (OverflowError, ValueError, TypeError):
                self._walk_active = False
                self.update()
                return
            self._walk_active = True
        else:
            self._walk_active = False
            self._walk_timeout_timer.stop()
        self.update()

    def _on_walk_timeout(self):
        """Force-clear the walk cue when the fallback timer fires (wh-n29v.117).

        Defends against the cue sticking on screen when no active=False
        arrives (e.g. a fresh-walk timeout sends no clear_overlay to the GUI).
        """
        self._walk_active = False
        self.update()

    def mousePressEvent(self, event):
        """Handle mouse press to begin drag or click detection.
        
        Args:
            event: Qt mouse event
        """
        if event.button() == Qt.MouseButton.LeftButton and self._accepts_gestures:
            # A press is one more way an earlier gesture can end. Finishing that
            # one first -- before deciding what THIS press is -- is what keeps a
            # lost release from throwing the earlier gesture away: the resize it
            # made gets saved, and the press it left open stops holding the
            # microphone. Doing it before the ring check covers both orders, an
            # abandoned resize followed by any press and an abandoned press
            # followed by a grab of the edge.
            self._end_gesture_in_flight()

            if self._point_is_on_edge(event.position()):
                # A resize and nothing else: no press_started (so push-to-talk
                # never arms), no move, no click. Suppressing the signal here
                # rather than cancelling later is what makes an accidental
                # recording structurally impossible instead of a race.
                self._last_press_was_on_edge = True
                self._begin_resize()
                return

            self._last_press_was_on_edge = False
            self._begin_press(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def _point_is_on_edge(self, local_pos) -> bool:
        """Return whether a button-local point falls in the resize ring."""
        return is_in_resize_ring(local_pos.x(), local_pos.y(), self.width())

    def _current_screen_bounds(self):
        """Return the whole screen the button is on right now.

        ``geometry``, not ``availableGeometry``, for the same reason as
        ``_screen_bounds``: the button is an always-on-top frameless window,
        so it stays visible and clickable over the taskbar and over every
        docked appbar strip, and a user may park it there on purpose. These
        bounds and those ones are the two rules that decide where the button
        may sit -- this one corrects an edge-drag resize, that one corrects a
        stored position -- so they must measure the same rectangle. While
        this method asked for the usable area they disagreed: growing a
        button parked on the taskbar pulled it up off the strip, and the next
        apply put it straight back.

        None when Qt reports no screen for the button, which leaves the
        resize uncorrected rather than correcting against a guess.
        """
        screen = self.screen()
        if screen is None:
            return None
        whole = screen.geometry()
        return (
            whole.x(),
            whole.y(),
            whole.width(),
            whole.height(),
        )

    def _apply_resize_to(self, global_pos):
        """Resize so the button's edge follows the pointer, centre unmoved."""
        size, left, top = resize_from_pointer(
            self._resize_centre.x(),
            self._resize_centre.y(),
            global_pos.x(),
            global_pos.y(),
        )

        # The bounds recorded when the drag began, NOT the screen the button is
        # on right now. The button grows around a fixed centre, so its corner
        # travels outward and can reach a neighbouring monitor part-way through
        # a drag. Asking again at that moment returns the neighbour, and
        # correcting against the neighbour's bounds throws the button onto it in
        # a single frame.
        if self._resize_screen is not None:
            left, top = correct_onto_screen(left, top, size, self._resize_screen)

        self.set_size(size)
        self.move(left, top)

    def _finish_resize(self):
        """End a resize in progress, saving the result if anything changed.

        Called from every way a resize can end, not just the release: the left
        button coming up somewhere else, the context menu opening, a wheel
        resize starting, and the button being hidden or shown. Each of those
        leaves the button at its new size on screen, so each has to save that
        size, or what is displayed and what is stored drift apart.

        Size and position go together as one value. The button grows around
        its centre, so its top-left corner moves with its diameter, and the
        corner is what the stored position holds. A drag can also change only
        the position: if the button started partly off screen, the correction
        pulls it back without the diameter ending up any different. Comparing
        the size alone would call that "nothing changed" and leave the old
        position stored, so both are compared.
        """
        if not self._is_resizing:
            return
        self._is_resizing = False
        if (
            self.width() != self._resize_start_size
            or self.pos() != self._resize_start_pos
        ):
            self.resize_finished.emit(self.width(), self.pos())
        self._settle_after_gesture()

    def _update_resize_cursor(self, local_pos):
        """Show a resize cursor over the ring and the plain arrow inside it."""
        on_edge = self._point_is_on_edge(local_pos)
        if on_edge == self._resize_cursor_active:
            return
        self._resize_cursor_active = on_edge
        if on_edge:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.unsetCursor()

    def mouseMoveEvent(self, event):
        """Handle mouse move for button dragging.
        
        Args:
            event: Qt mouse event
        """
        if self._is_resizing:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._apply_resize_to(event.globalPosition().toPoint())
                return
            # The left button is no longer down, so this resize is over even
            # though no release reached us -- the context menu's modal loop
            # takes the release when the user right-clicks mid-drag. Without
            # this check the next ordinary hover carries on resizing, and the
            # button grows while nothing is being pressed.
            self._finish_resize()

        if not (event.buttons() & Qt.MouseButton.LeftButton):
            self._update_resize_cursor(event.position())
            # The same reasoning as the resize check above, for the other
            # gesture. Holding the mouse should make a lost release impossible,
            # but a hold can fail to take -- the window is not on screen yet, or
            # another program is holding the mouse already -- so this stays as
            # the backstop. Without it a lost release leaves the press recorded
            # and the hold timer armed, and push-to-talk starts with the mouse
            # already up.
            if self._is_dragging:
                # The move already happened on screen, so it is saved here
                # exactly as a real release would save it.
                self.moved.emit(self.pos())
                self._is_dragging = False
                self._initial_press_pos = None
                self._settle_after_gesture()
            elif self._initial_press_pos is not None:
                self._cancel_press_in_flight()

        if event.buttons() & Qt.MouseButton.LeftButton and self._initial_press_pos:
            if not self._is_dragging and (event.globalPosition().toPoint() - self._initial_press_pos).manhattanLength() >= QApplication.startDragDistance():
                self._is_dragging = True
                self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self.drag_started.emit()
            
            if self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_position)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Handle mouse release to emit left_clicked or moved signal.
        
        Args:
            event: Qt mouse event
        """
        if event.button() == Qt.MouseButton.LeftButton and self._is_resizing:
            self._finish_resize()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._initial_press_pos:
            if not self._is_dragging:
                self.press_ended.emit()
            else:
                self.moved.emit(self.pos())
            self._is_dragging = False
            self._initial_press_pos = None
            self._settle_after_gesture()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Handle double-click to emit double_clicked signal."""
        if event.button() == Qt.MouseButton.LeftButton and self._accepts_gestures:
            # A double-click is one more way an earlier gesture can end, so it
            # finishes whatever was in flight before classifying this event --
            # the same thing the press handler does, and for the same reason.
            # All three paths below need it: a resize whose release went to
            # another window survives into any of them, and the release that
            # follows would then take the resize branch and return without
            # ending the press this handler just recorded, leaving the hold
            # timer to fire with the mouse already up.
            self._end_gesture_in_flight()

            if self._point_is_on_edge(event.position()):
                # Two quick grabs of the edge are two resizes, not a
                # double-click on the button. Qt sends the second press of a
                # double-click here INSTEAD of to the press handler, so this is
                # the only place that second grab can start its resize. Merely
                # refusing to report a double-click would leave it recording no
                # centre and no starting size, and the drag that follows would
                # do nothing at all.
                # Nothing records the edge here the way the press handler
                # does: Qt always sends a plain press next, even for a third
                # quick click, and that press records where it landed itself.
                self._begin_resize()
                return

            if self._last_press_was_on_edge:
                # Qt counted a grab of the edge as the first half of this
                # double-click, because it pairs a press with whichever press
                # came before it and does not care what we decided that one
                # meant. The user pressed the middle once, so treat it as one
                # press: reporting a double-click here would switch the speech
                # interaction mode off an edge grab and a single click, and at
                # every size the two regions are neighbours.
                #
                # The memory is not cleared here. The press handler is the only
                # place that writes it, and Qt always sends a plain press
                # before it can send another double-click, so that handler has
                # already replaced this value by the time it is read again.
                self._begin_press(event.globalPosition().toPoint())
                return

            self.double_clicked.emit()
        # Don't call super -- prevent Qt from firing another press

    def closeEvent(self, event):
        """Handle window close to emit closed signal.
        
        Args:
            event: Qt close event
        """
        # Same guarantee the hide path makes, for the ending that does not go
        # through a hide at all.
        self._end_gesture_in_flight()
        self._let_mouse_go()
        self.closed.emit()
        event.accept()

    def wheelEvent(self, event):
        """Handle mouse wheel with Ctrl modifier to resize button.
        
        Args:
            event: Qt wheel event
        """
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._accepts_gestures:
            # Two resize gestures must not run at once: the wheel resizes
            # around the top-left while an edge drag holds its own centre, so
            # the next move of that drag would snap the button back.
            self._finish_resize()
            delta = event.angleDelta().y()
            new_size = self.width() + (delta / 12)
            # The same limits the edge-drag gesture uses. Reading them from the
            # shared constants rather than writing the numbers here again is
            # what keeps the two gestures from disagreeing about how small or
            # large the button may get.
            new_size = int(max(MIN_BUTTON_SIZE, min(new_size, MAX_BUTTON_SIZE)))
            self.set_size(new_size)
            self.size_changed.emit(new_size)
        else:
            super().wheelEvent(event)
    
    def contextMenuEvent(self, event):
        """Handle right-click to show context menu.
        
        Args:
            event: Qt context menu event
        """
        # The menu's modal loop grabs the mouse, so the left-button release
        # would go to it and never reach this button. Whichever gesture was in
        # flight has to end here instead of being left marked as still running:
        # a resize gets saved, and a press stops holding the microphone open.
        self._end_gesture_in_flight()
        self.context_menu_requested.emit(event.globalPos())


class WorkingDialog(QDialog):
    """Always-on-top dialog shown during long-running operations.

    Displays the WheelHouse plaque image as header,
    with an animated message below showing pulsing dots.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._base_message = ""
        self._dot_count = 0
        # The operation this dialog is currently showing for, or None
        # when whoever raised it named no operation. Every provider and
        # every AI request share this one dialog, so a dismiss has to
        # say which operation it is for (wh-launch-addressed-notices,
        # wh-dialog-ownership-token). The token is a string
        # "<source>:<id>"; see shared/dialog_owner.py.
        self._owner: str | None = None

        # --- Plaque image header ---
        image_label = QLabel()
        image_path = str(Path(__file__).parent / "wheelhouse_plaque.jpg")
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaledToWidth(380, Qt.TransformationMode.SmoothTransformation)
            image_label.setPixmap(pixmap)
        image_label.setStyleSheet("background: transparent; border: none; padding: 0px;")

        # --- Message area (white) ---
        message_frame = QFrame()
        message_frame.setStyleSheet("QFrame { background-color: white; padding: 8px; border: 8px solid rgb(100, 80, 40); }")
        message_layout = QVBoxLayout(message_frame)
        message_layout.setContentsMargins(16, 12, 16, 12)

        self._message_label = QLabel("")
        self._message_label.setFont(QFont("Segoe UI", 11))
        self._message_label.setStyleSheet("color: rgb(40, 40, 40); background: transparent; border: none;")
        message_layout.addWidget(self._message_label)

        # --- Main layout ---
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(image_label)
        main_layout.addWidget(message_frame)

        # Dot animation timer
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._animate_dots)

    def show_working(self, message: str, owner: str | None = None) -> None:
        """Show the dialog with a message and start dot animation.

        If already visible, updates the message text.

        Args:
            message: The status message to display (dots are appended automatically).
            owner: The operation this dialog is being raised for, or
                None when the caller names no operation. It decides
                which later dismiss the dialog will accept
                (wh-launch-addressed-notices).
        """
        self._owner = owner
        self._base_message = message
        self._dot_count = 0
        self._message_label.setText(message)
        self._dot_timer.start(400)
        if not self.isVisible():
            # Center on primary screen
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                self.adjustSize()
                x = geo.x() + (geo.width() - self.width()) // 2
                y = geo.y() + (geo.height() - self.height()) // 2
                self.move(x, y)
            self.show()

    def hide_working(self, owner: str | None = None) -> None:
        """Hide the dialog and stop dot animation.

        A dismiss addressed to a launch that no longer owns the dialog
        is dropped. The launcher cannot make that decision itself: it
        asks whether its launch is still current and then acts, and the
        replacement can land in between (wh-launch-generation.2.9).
        Deciding here needs no lock, and not because the queue puts the
        replacement's show first: the two messages come from racing
        threads -- the hide from the replaced launch's monitor, the show
        from whoever starts the replacement -- so either order reaches
        this method. The owner comparison ends in the same state in
        both. Given show(new) then hide(old), the dismiss is dropped.
        Given hide(old) then show(new), the dismiss applies to the
        dialog the old launch still owns and the show raises it again.
        The dialog displays the new launch either way
        (wh-launch-addressed-notices).

        A dismiss must NAME the operation that owns the dialog. It used
        to be enough for the two sides not to contradict each other, so
        a dismiss naming nothing closed whatever was up -- and an AI
        request finishing closed the "Loading <engine>" dialog of a
        provider switch started after it, while the engine went on
        starting with nothing on screen to say so
        (wh-dialog-ownership-token). Naming nothing is now a claim like
        any other: it matches only a dialog that no operation owns, and
        no shipped sender raises one. The provider launch sends
        ``stt:<generation>``, both AI actions send ``ai:<uuid4>``, and
        GUI startup sends ``STARTUP_OWNER``. An unnamed dismiss is
        therefore dropped in practice, which is the whole point of the
        rule. The None case is kept because the queue reader supplies
        it for a message with no "owner" key, and because dropping such
        a dismiss is the safe answer rather than an error.

        The startup plaque is the one exception, and it is not a
        weakening of the rule. `STARTUP_OWNER` marks a placeholder
        rather than an operation: nothing can outlive startup, and what
        ends startup does not always know a launch token -- the
        WebSocket ready path sends the connection's launch stamp, which
        is None whenever no launch stamped that connection (no launcher
        at all, or the counter has not moved yet). Requiring a match
        there would leave the plaque on screen for the rest of the
        session. So any dismiss ends the placeholder, and the moment a
        real operation claims the dialog the ordinary rule applies
        again.

        Args:
            owner: The operation the dismiss is for, or None when the
                caller names none.
        """
        if owner != self._owner and self._owner != STARTUP_OWNER:
            return
        # Cleared with the dialog rather than left behind. A left-behind
        # owner is worse than it was before this rule: an operation that
        # names none would raise the dialog, inherit the dead owner, and
        # then have its own dismiss dropped against it. `show_working`
        # assigns on every raise, so nothing can read a stale owner
        # today; the clear is what keeps that true if `show_working`
        # ever returns early for a dialog already up.
        self._owner = None
        self._dot_timer.stop()
        self._dot_count = 0
        self.hide()

    def _animate_dots(self) -> None:
        """Cycle the dot animation: '' -> '.' -> '..' -> '...' -> ''."""
        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * self._dot_count
        self._message_label.setText(f"{self._base_message}{dots}")


# Working/busy indicator (wh-dictation-retraction-indicator.3). The badge rides
# a fixed session id on its OWN overlay manager (its generation counter only
# ever increases), so a fixed session is sufficient -- it never has to
# disambiguate concurrent sessions like the numbered overlay does.
_WORKING_BADGE_SESSION = 1
# Self-clearing fallback: a LAST-RESORT net, not the normal clear. The badge is
# normally cleared by the 'confirmed' activity state (the final commit) or by
# 'idle' (the 6s speech-stopped watchdog in websocket_manager re-arms on every
# stable, so it fires ~6s after the user actually stops talking). This timer
# only matters if BOTH of those are missed -- e.g. the Logic process dies with
# 'settling' frozen in the buffer. 'settling' is written once per utterance, so
# this timer is armed once and never re-armed; it must therefore outlast any
# plausible single continuous utterance, or it would clear the badge
# mid-dictation on exactly the long utterances most likely to be retracted
# (wh-dictation-retraction-indicator.8.1). 60s comfortably exceeds a continuous
# utterance (Silero closes the speech segment on real pauses well before then,
# and a pause longer than the watchdog window fires 'idle' first) while still
# bounding a frozen-producer stuck badge.
_WORKING_BADGE_TIMEOUT_MS = 60000


class GuiManager(QObject):
    def __init__(
        self,
        shutdown_event,
        commands_queue,
        state_queue,
        gui_shm_name=None,
        config: dict | None = None,
    ):
        super().__init__()
        self.shutdown_event = shutdown_event
        self.commands_to_logic_queue = commands_queue
        self.state_from_logic_queue = state_queue
        self._gui_shm_name = gui_shm_name
        self._gui_shm = None
        self._last_activity_state = None  # Track last state to avoid re-triggering
        self._last_activity_utterance_id = -1  # Track utterance to detect new speech

        # Authoritative state received from LogicController
        self.speech_enabled = False
        self.button_visible = True
        self.show_speech_pulse = True  # Config option to enable/disable pulse animation
        self.initial_state_received = False
        self.stt_provider = None  # Current STT provider name
        self.stt_providers_available = []  # List of available providers
        self.stt_provider_display_names = {}  # Provider name -> display name mapping
        self.ai_provider = None  # Current AI provider name
        self.ai_providers_available = []  # List of available AI providers
        self.ai_provider_display_names = {}  # AI provider name -> display name mapping
        self.interim_results_enabled = True  # Whether STT sends partial results
        self.debug_mode = False  # Whether log level is DEBUG
        self.speech_interaction_mode = "toggle"  # Updated from state_update
        self._ptt_held = False
        self._ptt_request_id = None
        self._ptt_feedback = ""
        self._ptt_feedback_text = ""

        self._settings_requests = {}
        self._settings_latest = {}
        self._settings_confirmed = {}
        self.settings_status_text = ''
        self._settings_notice = None

        self.button = FloatingButton()
        self.working_dialog = WorkingDialog()
        # wh-lzsbd: rejection toast widget. Lazy-built on first use so
        # Qt does not pay the construction cost when no rejection has
        # fired yet.
        self._rejection_toast = None
        # wh-iycks: correlation_token of the most recent rejection toast.
        # The toast widget itself is token-agnostic by design (wh-z7qx1).
        # The GUI manager records the token here when it renders the
        # toast, then attaches it to the try_anyway_clicked IPC payload
        # when the toast emits its click signal.
        self._last_rejection_token: str | None = None
        # wh-zib65: per-key cooldown + first/repeat dwell. The GUI side
        # owns toast suppression; the input side owns its own log map.
        from rejection_rate_limit import ToastSuppressionMap

        self._rejection_suppression = ToastSuppressionMap()

        # wh-bqv9c: three-strikes follow-up toast. Lazy-built on first
        # use so Qt does not pay the construction cost when no
        # threshold event has fired yet.
        self._grant_prompt_toast = None
        # Per-tuple per-session dedup. A tuple is added when the user
        # clicks Yes or No on the prompt for that tuple; subsequent
        # threshold events for the same tuple are suppressed. A
        # dismiss-without-click does NOT add the tuple, so the next
        # threshold event re-fires the prompt (per bead spec).
        #
        # wh-vbvgf.7.1 (codex review): the dedup boundary here is the
        # GUI-process session, NOT the WheelHouse run as a whole. If the
        # GUI process crashes and the launcher restarts it mid-run,
        # this set is wiped and a previously declined tuple can re-fire.
        # The Yes path survives that restart naturally because wh-8d81z
        # persists the soft-allow tuple to disk and the rejection
        # predicate stops emitting for the granted tuple. The No path
        # is the case that does not survive a GUI restart; wh-vdt1t will
        # introduce the No-click IPC back to Logic and migrate the
        # authoritative suppression to LogicController, at which point
        # this GUI-side set becomes redundant.
        self._grant_prompt_acted_on: set[tuple[str, str, str]] = set()
        # Identity of the most recently shown grant prompt. The Yes /
        # No click handlers attach this tuple to their IPC payloads so
        # downstream beads (wh-8d81z, wh-vdt1t) can resolve the click
        # back to the rejection identity. ``None`` means no prompt has
        # been shown yet this session.
        self._active_grant_tuple: tuple[str, str, str] | None = None
        # wh-9dkse: lazy-built acknowledgment toast for soft-allow
        # disk-write failures. LogicController.add_soft_allow emits a
        # ``soft_allow_write_failed`` event on the GUI state queue when
        # the persistence write fails; this widget surfaces the
        # "couldn't save your choice" message so the user knows the
        # Yes click did not stick. No retry button -- the user
        # re-attempts later by saying the words again.
        self._soft_allow_write_failed_toast = None

        # wh-click-notice-no-gui-handler: lazily-built advisory notice for
        # a click_element non-ok outcome (not_found / ambiguous /
        # execution_failed). Logic pushes a "show_click_notice" action; the
        # widget is created on first use and reused across notices.
        self._click_notice_toast = None

        # wh-n29v.53 (source leaf wh-h7cvz1): numbered-overlay paint window
        # manager. Logic drives it via the "paint_overlay" / "clear_overlay"
        # actions on the state queue; the manager paints one per-monitor
        # click-through layered window and returns an overlay_state_changed
        # dict that the dispatch forwards back to Logic on
        # commands_to_logic_queue. Construction is guarded so a Win32/ctypes
        # failure cannot crash GUI startup and take down the tray, button,
        # and editor; on failure the overlay is simply unavailable.
        #
        # wh-n29v.58: the validated overlay badge settings (overlay_badge_font_pt,
        # overlay_badge_shadow, overlay_badge_corner -- the corner the number
        # sits on -- and overlay_badge_trailing_space -- whether the number is
        # placed just past the control's trailing edge, wh-overlay-badge-occludes-
        # label) are read from the
        # already-loaded GUI-process config here and passed to the manager so
        # a user who sets e.g. overlay_badge_font_pt=32 in config.toml actually
        # sees it. ClickConfig.from_raw NEVER raises and degrades a bad value to
        # the validated default, so this cannot fail; we still derive it before
        # the try so a manager-construction failure (the Win32/ctypes guard
        # below) does not skip the validated read.
        from services.wheelhouse.ui.click_config import ClickConfig

        _click_config = ClickConfig.from_raw((config or {}).get("click", {}))
        self._overlay_manager = None
        try:
            from overlay_paint_window import OverlayPaintWindowManager

            self._overlay_manager = OverlayPaintWindowManager(
                badge_font_pt=_click_config.overlay_badge_font_pt,
                badge_shadow=_click_config.overlay_badge_shadow,
                badge_corner=_click_config.overlay_badge_corner,
                badge_trailing_space=_click_config.overlay_badge_trailing_space,
                badge_theme=_click_config.overlay_badge_theme,
            )
        except Exception:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "Failed to construct OverlayPaintWindowManager; the "
                "numbered overlay will be unavailable this session.",
                exc_info=True,
            )

        # wh-overlay-slow-uia-stale-badges.9: the badge lease. Single-shot;
        # armed by an accepted paint, renewed by overlay_lease_renew,
        # cancelled by an accepted clear or a reset_overlay. On expiry the
        # GUI destroys the badge windows itself (Logic stopped talking) and
        # reports state="expired" back on commands_to_logic_queue.
        self._overlay_lease_timer = QTimer(self)
        self._overlay_lease_timer.setSingleShot(True)
        self._overlay_lease_timer.timeout.connect(
            self._on_overlay_lease_expired
        )
        # The (overlay_session_id, paint_generation) pair the lease covers,
        # or None when no lease is active.
        self._overlay_lease_pair: tuple[int, int] | None = None

        # wh-overlay-slow-uia-stale-badges.18.4: the teardown retry.
        # Single-shot; armed whenever a clear / lease expiry / reset left
        # the manager with teardown_pending True (a DestroyWindow failed,
        # so a badge window may still be on screen), re-armed until a
        # retry sweep ends clean. An incomplete clear emits NO ack, so
        # Logic's clear-ack watchdog stays unresolved and its 5000 ms
        # deadline fires a truthful "badges may still be on the screen"
        # ERROR; the deferred cleared/expired ack then arrives after a
        # later successful retry as bookkeeping. That is the designed
        # behavior, not a bug.
        self._overlay_teardown_retry_timer = QTimer(self)
        self._overlay_teardown_retry_timer.setSingleShot(True)
        self._overlay_teardown_retry_timer.timeout.connect(
            self._on_overlay_teardown_retry
        )

        # wh-grid-paint-mode: the mouse grid's paint window manager. Logic
        # drives it via the "paint_grid" / "paint_grid_pin" / "clear_grid"
        # actions on the state queue; the manager paints three-by-three grid
        # lines, nine cell numbers, and the drag-anchor pin on the same kind
        # of per-monitor click-through layered window the badges use. No
        # reply goes back to Logic -- the grid has no build phase to
        # acknowledge (see shared/clear_grid.py).
        #
        # A THIRD dedicated OverlayPaintWindowManager, for the same reason
        # the working badge owns the second one: a manager's clear destroys
        # every window IT owns, so sharing an instance between two features
        # would let one feature's teardown erase the other's drawing. The
        # process-global window class is registered once and its WNDPROC is
        # the stateless module-scope _PROCESS_WND_PROC, so extra instances
        # cost nothing and cannot leave the class pointing at a freed
        # callback (wh-overlay-shared-wndproc). Badge styling arguments are
        # irrelevant to the grid (its styling is hard-coded beside the
        # badges') except badge_shadow, which the grid labels honor as the
        # same accessibility setting.
        self._grid_overlay = None
        try:
            from overlay_paint_window import OverlayPaintWindowManager

            self._grid_overlay = OverlayPaintWindowManager(
                badge_shadow=_click_config.overlay_badge_shadow,
            )
        except Exception:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "Failed to construct the mouse-grid overlay; the voice mouse "
                "grid will be unavailable this session.",
                exc_info=True,
            )

        # Working/busy indicator (wh-dictation-retraction-indicator.3): a busy
        # glyph painted at the mouse pointer while dictated text is provisional
        # (the live words could still be retracted by the STT final), so a
        # retraction is less surprising. It uses a SEPARATE
        # OverlayPaintWindowManager from the numbered overlay above so their
        # generation gates and window teardown never interfere -- the numbered
        # overlay's clear() destroys all of ITS windows, not the badge's, and
        # vice versa. Crash-safe: the badge window is GUI-process-owned, so a
        # GUI crash makes the OS destroy it (no global state to restore); the
        # fallback timer below bounds a missed 'confirmed'. Gated by config and
        # built only when enabled. badge_shadow follows the same accessibility
        # setting as the numbered overlay (the glyph honors it); badge_font_pt
        # is irrelevant to the glyph but passed for construction symmetry.
        from working_indicator_config import WorkingIndicatorConfig

        self._working_indicator_enabled = WorkingIndicatorConfig.from_raw(
            (config or {}).get("dictation", {})
        ).enabled
        self._working_badge_overlay = None
        self._working_badge_gen = 0
        self._working_badge_shown = False
        # When the GUI thread is DEFINITELY not per-monitor-DPI-aware, the badge
        # would mis-position, so painting is skipped (see the DPI block below and
        # _show_working_badge). Default False (safe to paint) for the disabled
        # path and until computed at construction.
        self._working_badge_dpi_unsafe = False
        if self._working_indicator_enabled:
            try:
                from overlay_paint_window import OverlayPaintWindowManager

                # This is a SECOND OverlayPaintWindowManager (the numbered
                # click overlay owns the first). The two instances isolate
                # their generation gates and window teardown, but they share
                # one process-global Win32 window CLASS: the class is
                # registered once (the second registration hits
                # ERROR_CLASS_ALREADY_EXISTS and is a no-op) and its WNDPROC
                # is the stateless module-scope _PROCESS_WND_PROC in
                # overlay_paint_window.py, retained for the process lifetime
                # (wh-overlay-shared-wndproc). Neither manager owns the
                # callback, so constructing or destroying a manager at
                # runtime can never leave the class pointing at a freed
                # callback.
                # badge_theme is passed for construction symmetry; the working
                # glyph keeps its own fixed colors and ignores it.
                self._working_badge_overlay = OverlayPaintWindowManager(
                    badge_font_pt=_click_config.overlay_badge_font_pt,
                    badge_shadow=_click_config.overlay_badge_shadow,
                    badge_corner=_click_config.overlay_badge_corner,
                    badge_trailing_space=_click_config.overlay_badge_trailing_space,
                    badge_theme=_click_config.overlay_badge_theme,
                )
            except Exception:  # noqa: BLE001 - indicator is non-critical
                logger.warning(
                    "Failed to construct the working-badge overlay; the "
                    "dictation working indicator will be unavailable this "
                    "session.",
                    exc_info=True,
                )
            # DPI-awareness check (wh-dictation-retraction-indicator.8.2, .9.2):
            # the badge is positioned from GetCursorPos, which returns physical
            # pixels only when this thread is per-monitor-DPI-aware. Qt6 sets PMv2
            # by default. If that ever stops holding, the cursor position is
            # virtualized and the badge could land on the wrong monitor on a
            # mixed-DPI desktop. A DEFINITE False is a fail-safe: warn AND skip
            # painting (in _show_working_badge) so a misleading position is never
            # shown. None (undeterminable, e.g. an old-Windows host without the
            # query API) is left as paint -- it does not assert the space is
            # wrong, and disabling on it would needlessly remove the feature.
            if self._working_badge_overlay is not None:
                self._working_badge_dpi_unsafe = (
                    self._dpi_awareness_is_per_monitor() is False
                )
                if self._working_badge_dpi_unsafe:
                    logger.warning(
                        "GUI thread is not per-monitor-DPI-aware; the dictation "
                        "working badge is disabled this session to avoid "
                        "mis-positioning on a mixed-DPI desktop (expected Qt6 "
                        "PMv2 default)."
                    )
        # Self-clearing fallback so a missed 'confirmed' cannot leave the badge
        # stuck (mirrors the FloatingButton walk-cue timeout pattern).
        self._working_badge_timeout_timer = QTimer(self)
        self._working_badge_timeout_timer.setSingleShot(True)
        self._working_badge_timeout_timer.timeout.connect(
            self._hide_working_badge
        )

        # wh-g2-refactor.18 (Section 6 generation fence): the GUI-side
        # editor generation counter mirrors what Logic believes is
        # current. The persistent editor's own counter is seeded from
        # this value at construction; the rebuild orchestrator bumps
        # both together.
        self._editor_generation: int = 0
        # wh-wisp-07m: construct the persistent editor here, while the
        # QApplication exists but before the Qt event loop starts. The
        # G2 design promises the QPlainTextEdit exists at GUI startup
        # so the first insert_editor_word IPC finds a live editor and
        # generation 0 matches Logic's initial observed_generation.
        # The QDialog remains hidden until show_editor is called; the
        # Logic-side show_editor_persistent producer triggers the
        # show via the existing te_show IPC. The rebuilder may later
        # set this back to None during a rebuild, at which point
        # _open_terminal_editor reconstructs lazily.
        from terminal_editor_window import TerminalDictationEditorWindow
        self._te_window = TerminalDictationEditorWindow(parent=None)
        self._te_window._editor_generation = self._editor_generation
        self._te_window.editor_cancelled.connect(self._on_te_cancelled)
        self._te_window.editor_event_acked.connect(self._on_te_event_acked)
        # wh-g2-refactor.18 (Section 5 / Section 2): the IPC responder
        # dispatches per-word insert and retract requests on the Qt
        # main thread. It reads the live editor via the closure below so
        # a None _te_window during rebuild short-circuits to
        # stale_generation cleanly.
        from services.wheelhouse.shared.editor_ipc_responder import (
            EditorIpcResponder,
        )
        from services.wheelhouse.shared.editor_rebuild import (
            PersistentEditorRebuilder,
        )

        self._editor_ipc_responder = EditorIpcResponder(
            get_editor=lambda: self._te_window,
            response_queue=self.commands_to_logic_queue,
        )
        # wh-g2-refactor.18 (Section 6): the rebuilder owns the
        # destroy-and-reconstruct sequence with generation fencing. It
        # is constructed once and reused; ``rebuild(reason)`` runs the
        # bump-and-destroy sequence and clears the editor reference.
        self._editor_rebuilder = PersistentEditorRebuilder(
            get_editor=lambda: self._te_window,
            set_editor=self._set_te_window_for_rebuilder,
            get_generation=lambda: self._editor_generation,
            set_generation=self._set_editor_generation_for_rebuilder,
            post_notification=self._post_editor_rebuilt_notification,
        )

        self.icon = pystray.Icon(
            "wheelhouse", icon=load_tray_icon(), title="Wheelhouse"
        )

        self.button.press_started.connect(self._on_button_press)
        self.button.press_ended.connect(self._on_button_release)
        self.button.double_clicked.connect(self._on_double_click)
        self.button.drag_started.connect(self._on_drag_started)
        self.button.press_cancelled.connect(self._on_press_cancelled)
        self.button.closed.connect(self.hide_button)
        self.button.size_changed.connect(self.send_size_change_command)
        self.button.moved.connect(self.send_pos_change_command)
        self.button.resize_finished.connect(self.send_resize_commit_command)
        self.button.gesture_ended.connect(self._on_gesture_ended)
        self.button.context_menu_requested.connect(self.show_context_menu)

        # A size and position from the Logic process that arrived while the
        # user was mid-gesture, waiting for the gesture to end. None when
        # nothing is waiting.
        self._deferred_geometry = None

        # Whether the screen layout changed while the user was mid-gesture.
        # A flag of its own, and not a second use of _deferred_geometry,
        # because the gesture's own completion clears that one: moved and
        # resize_finished reach send_pos_change_command and
        # send_resize_commit_command BEFORE gesture_ended fires, so a layout
        # correction parked there is discarded a moment before anything can
        # apply it (wh-floating-button-offscreen.1.2).
        self._layout_changed_during_gesture = False

        # The one timer that carries every delayed re-check of the button's
        # position. One resolution change fires several layout signals, and
        # without a single timer each of them would leave its own
        # (wh-floating-button-offscreen, criterion B4). It is built on the
        # first display change and started again on every one after that.
        self._delayed_reapply_timer = None

        # A stored position outlives the screens it was stored on, and nothing
        # in the button noticed when they changed, so the button could come
        # back where no screen is (wh-floating-button-offscreen). Connected
        # after _deferred_geometry exists, because the slot reads it.
        self._watch_the_screen_layout()

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._check_queues_and_events)
        self.icon_thread = threading.Thread(target=self.icon.run, daemon=True)

        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_hold_threshold)
        self._PTT_HOLD_THRESHOLD_MS = 200
        self._ptt_ack_timer = QTimer(self)
        self._ptt_ack_timer.setSingleShot(True)
        self._ptt_ack_timer.timeout.connect(self._ptt_pending_timeout)

        self._double_click_timer = QTimer(self)
        self._double_click_timer.setSingleShot(True)
        self._double_click_timer.timeout.connect(self._on_deferred_single_click)
        self._DOUBLE_CLICK_WAIT_MS = 350

        # Tray icon double-click detection (threading.Timer since pystray runs in its own thread)
        self._tray_click_timer: threading.Timer | None = None
        self._TRAY_DOUBLE_CLICK_WAIT_S = 0.35

        # Fast polling timer for activity state updates (10ms for low latency)
        self._activity_timer = QTimer(self)
        self._activity_timer.timeout.connect(self._check_activity_shm)

    def start(self):
        """Start GUI manager by initializing tray icon, button, and state polling timer."""
        logger.info("GuiManager starting...")
        self.button.set_indeterminate(True)
        self.update_tray_menu()
        self.icon_thread.start()
        self.button.show()

        # Show working dialog immediately so the plaque appears during startup
        self.working_dialog.show_working("Starting", STARTUP_OWNER)

        self.queue_timer.start(100)
        self.send_command({'action': 'request_initial_state'})
        
        # Connect to GUI shared memory for activity state updates
        if self._gui_shm_name:
            try:
                self._gui_shm = shared_memory.SharedMemory(name=self._gui_shm_name)
                self._activity_timer.start(10)  # 10ms polling for low latency
                logger.info(f"GuiManager: Connected to GUI shared memory: {self._gui_shm_name}")
            except Exception as e:
                logger.error(f"GuiManager: Failed to connect to GUI shared memory: {e}")
    
    def _check_activity_shm(self):
        """Poll shared memory for activity state updates."""
        if not self._gui_shm:
            return
        
        try:
            size = struct.unpack('>I', self._gui_shm.buf[:4])[0]
            if size == 0 or size > 200:
                return  # No data or invalid size
            
            data = bytes(self._gui_shm.buf[4:4+size])
            msg = json.loads(data.decode('utf-8'))

            # The read+decode succeeded, so clear the error latch. If reads were
            # failing (torn write, unmapped segment) and have now recovered, the
            # next failure run logs again instead of staying silent forever
            # (wh-dictation-retraction-indicator.10.3).
            self._activity_shm_error_logged = False

            state = msg.get('state', 'idle')
            utterance_id = msg.get('utterance_id', -1)
            
            # Only update button if state actually changed or it's a new utterance
            # This prevents re-triggering timers on every 10ms poll
            is_new_utterance = (utterance_id != self._last_activity_utterance_id)
            is_state_change = (state != self._last_activity_state)
            
            if is_new_utterance or is_state_change:
                self._last_activity_state = state
                self._last_activity_utterance_id = utterance_id
                # If pulse is disabled, only show confirmed state (green flash)
                if self.show_speech_pulse or state == 'confirmed':
                    self.button.set_activity_state(state)
                else:
                    # Pulse disabled - stay idle unless confirmed
                    self.button.set_activity_state('idle' if state == 'hearing' else state)

                # Working/busy badge (wh-dictation-retraction-indicator.3):
                # show it while dictated text is provisional ('settling'),
                # clear it when the final commits ('confirmed') or speech goes
                # idle. Independent of the pulse setting above -- the badge is a
                # separate, opt-out-able affordance.
                if state == 'settling':
                    self._show_working_badge()
                elif state in ('confirmed', 'idle'):
                    self._hide_working_badge()
        except Exception:
            # Never let a read failure disrupt the GUI -- but do not swallow it
            # silently either. This poll fires every 10ms; a torn read or an
            # unmapped segment would otherwise fail invisibly on every tick. Log
            # once per failure run (the latch is cleared on the next successful
            # read above), so the failure is observable without spamming the log
            # at 100 lines/second (wh-dictation-retraction-indicator.10.3).
            if not getattr(self, '_activity_shm_error_logged', False):
                logger.warning(
                    "Activity shared-memory read failed; badge and speech-pulse "
                    "updates may be missed until it recovers.",
                    exc_info=True,
                )
                self._activity_shm_error_logged = True

    def _get_cursor_pos(self):
        """Return the mouse pointer position in virtual-desktop PHYSICAL
        pixels as ``(x, y)``, or ``None`` on failure.

        Uses ``GetCursorPos`` -- universal (works for every app, unlike the
        text caret) and the same coordinate space the overlay resolver and
        native-monitor enumeration use. Never raises (the indicator is
        non-critical).
        """
        try:
            import ctypes
            from ctypes import wintypes

            pt = wintypes.POINT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return None
            return (int(pt.x), int(pt.y))
        except Exception:
            return None

    def _dpi_awareness_is_per_monitor(self):
        """Return True if this thread is per-monitor-DPI-aware, False if it is
        system- or un-aware, or None if it cannot be determined. Never raises.

        ``_get_cursor_pos`` only returns virtual-desktop PHYSICAL pixels -- the
        coordinate space the overlay resolver expects -- when the GUI thread is
        per-monitor aware. Under system-aware or unaware, the cursor position is
        virtualized and the badge would mis-position on a monitor whose DPI
        differs from the primary. Qt6 sets per-monitor-v2 by default, but nothing
        in this process asserts it; this check turns a silent regression (if that
        default ever changes) into a logged warning
        (wh-dictation-retraction-indicator.8.2).
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # DPI_AWARENESS_CONTEXT is a pseudo-pointer handle -- read/pass it as
            # a full 64-bit void* so it is never truncated on 64-bit Windows.
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = [
                ctypes.c_void_p
            ]
            ctx = user32.GetThreadDpiAwarenessContext()
            # DPI_AWARENESS: INVALID=-1, UNAWARE=0, SYSTEM_AWARE=1,
            # PER_MONITOR_AWARE=2 (v1 and v2 both report 2 here).
            awareness = user32.GetAwarenessFromDpiAwarenessContext(ctx)
            if awareness is None or awareness < 0:
                return None
            _PER_MONITOR_AWARE = 2
            return awareness == _PER_MONITOR_AWARE
        except Exception:
            return None

    def _show_working_badge(self):
        """Paint the working/busy badge at the mouse pointer.

        No-op when the indicator is disabled, the overlay is unavailable, the
        GUI thread is not per-monitor-DPI-aware (the badge would mis-position),
        or the cursor position cannot be read. Advances the badge generation
        each call so its dedicated gate never stale-drops the paint, and arms
        the self-clearing fallback timer. Never raises.
        """
        if not self._working_indicator_enabled or self._working_badge_overlay is None:
            return
        # Fail-safe: do not paint at a known-wrong coordinate space
        # (wh-dictation-retraction-indicator.9.2).
        if self._working_badge_dpi_unsafe:
            return
        pos = self._get_cursor_pos()
        if pos is None:
            return
        self._working_badge_gen += 1
        try:
            self._working_badge_overlay.paint_working_badge(
                pos[0],
                pos[1],
                overlay_session_id=_WORKING_BADGE_SESSION,
                paint_generation=self._working_badge_gen,
            )
        except Exception:
            logger.debug("working badge paint failed", exc_info=True)
            return
        self._working_badge_shown = True
        self._working_badge_timeout_timer.start(_WORKING_BADGE_TIMEOUT_MS)

    def _hide_working_badge(self):
        """Clear the working/busy badge and cancel the fallback timer.

        Idempotent (a clear with nothing shown is a no-op) and never raises.
        Advances the generation so the dedicated gate honors the teardown.
        """
        self._working_badge_timeout_timer.stop()
        if self._working_badge_overlay is None or not self._working_badge_shown:
            return
        self._working_badge_gen += 1
        try:
            self._working_badge_overlay.clear(
                overlay_session_id=_WORKING_BADGE_SESSION,
                paint_generation=self._working_badge_gen,
            )
        except Exception:
            logger.debug("working badge clear failed", exc_info=True)
        self._working_badge_shown = False

    def _check_queues_and_events(self):
        """:flow: GUI State Synchronization
        :step: 6
        :description: Timer-driven polling for state updates from logic process
        :data_in: Message dictionary from state_from_logic_queue
        :data_out: Processed state updates or notification commands
        :notes: Runs on QTimer interval in GUI event loop. Polls state_from_logic_queue using get_nowait() to avoid blocking. This is the Logic→GUI direction of bidirectional IPC. Handles two message types: (1) state_update/initial_state: updates GUI internal state variables, (2) show_notification: displays Windows toast notifications. Queue is populated by StateManager.send_state_update() in logic process.
        """
        if self.shutdown_event.is_set():
            self._shutdown_gui()
            return

        # Drain all available messages per tick (prevents help response lag)
        try:
            self._check_settings_timeout()
        except Exception:
            logger.exception('Error checking the settings acknowledgement timeout')
        while True:
            try:
                message = self.state_from_logic_queue.get_nowait()
            except Empty:
                break
            except Exception as e:
                logger.error(f"Error reading GUI state queue: {e}", exc_info=True)
                break

            try:
                action = message.get("action")

                """:flow: GUI State Synchronization
                :step: 7
                :description: Unpacks state update message and updates GuiManager internal state
                :data_in: state_update message with state variables
                :data_out: Updated GuiManager instance variables
                :notes: Handles 'state_update' and 'initial_state' actions by extracting all GUI-relevant state variables (speech_enabled, button_visible, FLOATING_BUTTON_SIZE, FLOATING_BUTTON_POS) from IPC message payload. Sets initial_state_received flag on first message to enable user interactions. Calls update_ui_state() to propagate changes to visual elements.
                """
                if action in ["initial_state", "state_update"]:
                    message = self._settings_filter_state(message)
                    was_initial = not self.initial_state_received
                    if was_initial:
                        self.initial_state_received = True

                    self.speech_enabled = message.get('speech_enabled', False)
                    self.button_visible = message.get('button_visible', True)
                    self.show_speech_pulse = message.get('SHOW_SPEECH_PULSE', True)
                    self.stt_provider = message.get('stt_provider')
                    self.stt_providers_available = message.get('stt_providers_available', [])
                    self.stt_provider_display_names = message.get('stt_provider_display_names', {})
                    self.ai_provider = message.get('ai_provider')
                    self.ai_providers_available = message.get('ai_providers_available', [])
                    self.ai_provider_display_names = message.get('ai_provider_display_names', {})
                    self.interim_results_enabled = message.get('interim_results_enabled', True)
                    self.debug_mode = message.get('debug_mode', False)
                    self.speech_interaction_mode = message.get('speech_interaction_mode', 'toggle')
                    self._handle_ptt_state(message)
                    # Not while the user is dragging. Size and position are
                    # saved when the drag ends, so during one the settings
                    # still hold the geometry from before it started. Putting
                    # that back on screen mid-drag snaps the button to its old
                    # size and place, and a release straight afterwards saves
                    # nothing at all, because the gesture then sees the size it
                    # began with. Any unrelated message would do it: speech
                    # being suppressed, a provider reporting in, a
                    # push-to-talk correction.
                    #
                    # Held back, not dropped. A state update does sometimes
                    # carry a real change -- the settings window, a reset --
                    # and dropping it leaves the button at a size no later
                    # message corrects, because Logic only ever sends the
                    # geometry it already believes the button has. The newest
                    # one waits for the gesture to end.
                    geometry = (
                        message.get('FLOATING_BUTTON_SIZE', 50),
                        message.get('FLOATING_BUTTON_POS', [100, 100]),
                    )
                    # Whether ANY gesture is running, not just the two that
                    # move something. A press in the middle is live from the
                    # moment it lands, and neither of the moving flags is set
                    # between then and the pointer travelling far enough to
                    # count as a drag -- an interval that covers a click that
                    # never moves and a stationary push-to-talk hold, for as
                    # long as the user holds it.
                    if self.button._gesture_running:
                        self._deferred_geometry = geometry
                    else:
                        self._apply_geometry(geometry)

                    if was_initial:
                        self.button.set_indeterminate(False)
                        # Only now. Until the stored size and position arrive,
                        # a gesture would measure against the default geometry
                        # and this message would replace it underneath.
                        self.button.set_ready_for_gestures(True)

                    self.update_ui_state()
                elif action in ('config_write_result', 'config_values_result'):
                    self._handle_settings_result(message)
                elif action == 'toggle_button_visibility':
                    self.toggle_button_visibility()
                elif action == "show_working":
                    # The operation this dialog belongs to travels with
                    # it, so a later dismiss can be matched against it.
                    # Every shipped sender now supplies it: the speech
                    # launcher, both AI actions, and GUI startup
                    # (wh-launch-addressed-notices,
                    # wh-dialog-ownership-token). `.get` stays rather
                    # than `[]` so a message without the key reaches
                    # the dialog as None -- an ordinary claim that
                    # matches only an unowned dialog -- instead of
                    # raising KeyError in the queue reader.
                    self.working_dialog.show_working(
                        message.get("message", "Working"),
                        message.get("owner"),
                    )
                elif action == "hide_working":
                    self.working_dialog.hide_working(message.get("owner"))
                elif action == "show_notification":
                    if not send_notice(
                        message.get("title", "Wheelhouse"),
                        message.get("message", ""),
                        timeout=message.get("timeout", 5),
                    ):
                        logger.warning("Notification service not available for message: %s", message.get("title"))
                elif action == "click_first_use_hint":
                    # wh-r3xy1: one-shot screen-reader-flag discovery hint.
                    # Logic pushes this the first time a voice click targets
                    # a Chromium-family window while the opt-in is off. Render
                    # the exact wording through the existing OS info-notice
                    # path -- the "Tap to dismiss" affordance is the OS toast
                    # itself; suppression is owned Logic-side (dismiss /
                    # three-subsequent-clicks + the durable record file), so no
                    # GUI -> Logic dismiss round trip is needed.
                    self._show_first_use_hint(message)
                elif action == "show_rejection_toast":
                    # wh-lzsbd (wh-9weum Phase 2): advisory toast for
                    # text-target rejections. Wording branches by reason
                    # category; "Show details" exposes the raw fields.
                    self._show_rejection_toast(message)
                elif action == "show_click_notice":
                    # wh-click-notice-no-gui-handler: advisory notice for a
                    # voice-click non-ok outcome (not_found / ambiguous /
                    # execution_failed). Logic builds a ClickNoticeEvent and
                    # forwards it here for every non-ok click; without this
                    # branch the notice was silently dropped and a failed
                    # click produced no on-screen feedback.
                    self._show_click_notice(message)
                elif action == "paint_overlay":
                    # wh-n29v.53: Logic asks the GUI to paint the numbered
                    # overlay. Parse + drive the manager + forward the
                    # resulting overlay_state_changed dict back to Logic.
                    # wh-n29v.117 backstop: the walk that produced this paint
                    # is finished, so clear the walking cue here too (the
                    # primary clear is Logic's active:False message).
                    self.button.set_walk_cue(False)
                    self._handle_paint_overlay(message)
                elif action == "clear_overlay":
                    # wh-n29v.53: Logic asks the GUI to tear down the
                    # numbered overlay.
                    # wh-n29v.117 backstop: a teardown ends any in-flight
                    # walk, so clear the walking cue here too.
                    self.button.set_walk_cue(False)
                    self._handle_clear_overlay(message)
                elif action == "overlay_lease_renew":
                    # wh-overlay-slow-uia-stale-badges.9: Logic's keepalive
                    # tick renews the badge lease while its machine sits in
                    # painted. Side-channel dict, no shared/ schema (the
                    # walk-cue precedent, wh-n29v.117).
                    self._handle_overlay_lease_renew(message)
                elif action == "reset_overlay":
                    # wh-overlay-slow-uia-stale-badges.9: a (re)started
                    # Logic announces itself. Tear down any badges a dead
                    # Logic left behind and reset the generation gate so
                    # the new Logic's from-zero pair numbering can paint.
                    # A reset also ends any in-flight walk cue.
                    self.button.set_walk_cue(False)
                    self._handle_reset_overlay(message)
                elif action == "paint_grid":
                    # wh-grid-paint-mode: Logic asks the GUI to draw the
                    # mouse grid over one rectangle on one monitor.
                    self._handle_paint_grid(message)
                elif action == "paint_grid_pin":
                    # wh-grid-paint-mode: Logic asks the GUI to draw the
                    # drag-anchor pin ("mark"). It coexists with the grid.
                    self._handle_paint_grid_pin(message)
                elif action == "clear_grid":
                    # wh-grid-paint-mode: Logic asks the GUI to tear the
                    # mouse grid down, pin included.
                    self._handle_clear_grid(message)
                elif action == "overlay_walk_cue":
                    # wh-n29v.117: a small "walking" progress cue on the
                    # floating button while a numbered-overlay walk is in
                    # flight (overlay states walk_in_flight /
                    # refresh_in_flight). Logic emits active:True at
                    # walk-start and active:False when painted / on a build
                    # failure or timeout. Defensive (dict.get default,
                    # try/except around the queue loop) so a version-skewed
                    # sender cannot crash the GUI loop (wh-uf54). Routed
                    # through this 100ms state queue, not the 10ms
                    # shared-memory activity fast path.
                    #
                    # wh-n29v.118: thread the GUI fallback bound (walk_timeout_ms
                    # = the build's own awaited window plus response_timeout_ms,
                    # plus the sentence-wait bound for a walk build; see
                    # _overlay_dispatch_build) through to
                    # set_walk_cue so the GUI fallback outlasts the real walk.
                    # Pass it through raw (None when absent); set_walk_cue does
                    # the int/bool validation and falls back to its default.
                    self.button.set_walk_cue(
                        bool(message.get("active", False)),
                        walk_timeout_ms=message.get("walk_timeout_ms"),
                    )
                elif action == "text_target_grant_prompt":
                    # wh-bqv9c: three-strikes follow-up toast. Surfaces
                    # the "Always type into <App>" Yes/No prompt when
                    # the click counter reaches the soft-allow
                    # threshold. Per-tuple per-session deduped on the
                    # GUI side; dismiss-without-click resets dedup.
                    self._show_grant_prompt_toast(message)
                elif action == "soft_allow_write_failed":
                    # wh-9dkse: disk-write-fails follow-up toast.
                    # LogicController.add_soft_allow emits this when the
                    # soft-allow file write fails; the user gets a
                    # "couldn't save your choice" acknowledgment and
                    # retries later by saying the words again.
                    self._show_soft_allow_write_failed_toast(message)
                elif action == "declined_write_failed":
                    # wh-27gvv: declined-file disk-write-fails follow-up
                    # toast. LogicController.add_declined emits this when
                    # the declined-tuple file write fails after a No
                    # click on the three-strikes grant prompt. The user
                    # gets a "couldn't save your choice" acknowledgment
                    # and can click No again the next time the prompt
                    # appears.
                    self._show_declined_write_failed_toast(message)
                elif action == "open_pattern_manager":
                    self._open_pattern_manager()
                elif action == "open_help_explainer":
                    # The Logic process decided the user needs the
                    # explanation before the browser opens. It holds the
                    # settings; this process holds the windows.
                    self._open_help_explainer()
                elif action == "open_calibration":
                    # wh-7ou.7.3.1: voice-command path ("learn my voice").
                    # Logic asks the GUI to open the voice-teaching window.
                    self._open_calibration()
                elif action == "cal_state":
                    # wh-7ou.7.3.1: the window renders whatever screen the
                    # latest cal_state names; it holds no session logic.
                    if hasattr(self, '_cal_dialog') and self._cal_dialog is not None:
                        self._cal_dialog.handle_state(message.get("state") or {})
                elif action and action.startswith("pm_"):
                    if hasattr(self, '_pm_dialog') and self._pm_dialog is not None:
                        self._pm_dialog.handle_response(message)
                elif action == "show_help_chat":
                    self._open_help_chat(question=message.get("question", ""))
                elif action == "help_response":
                    if hasattr(self, "_help_window") and self._help_window:
                        self._help_window.show_response(message.get("text", ""))
                elif action == "help_error":
                    if hasattr(self, "_help_window") and self._help_window:
                        self._help_window.show_error(message.get("message", ""))
                elif action == "te_show":
                    self._open_terminal_editor(
                        text=message.get("text", ""),
                        hwnd=message.get("hwnd", 0),
                        rect=tuple(message.get("rect", ())),
                        request_id=message.get("request_id", ""),
                        utterance_id=message.get("utterance_id", ""),
                    )
                elif action == "te_submit":
                    if self._te_window:
                        self._te_window.do_submit()
                elif action == "te_cancel":
                    if self._te_window:
                        self._te_window.do_cancel()
                elif action == "insert_editor_word":
                    # wh-g2-refactor.18 (Section 5): per-word insert IPC.
                    # The responder handles schema validation, generation
                    # fence, exception capture, and response enqueue.
                    self._editor_ipc_responder.handle(message)
                elif action == "retract_editor_text":
                    # wh-g2-refactor.18 (Section 2): retract+replay IPC.
                    self._editor_ipc_responder.handle(message)
            except Exception as e:
                logger.error(f"Error processing GUI state queue: {e}", exc_info=True)

    def _show_first_use_hint(self, message: dict):
        """Render the screen-reader-flag first-use discovery hint (wh-r3xy1).

        Logic pushes a ``click_first_use_hint`` action carrying the verbatim
        ``HINT_TEXT`` the first time a voice click targets a Chromium-family
        window while the opt-in is off. The notice is surfaced through the
        existing OS info-notice path (the same ``plyer.notification.notify``
        used by ``show_notification``); the "Tap to dismiss" affordance is the
        OS toast itself. Suppression is owned Logic-side, so this method just
        renders. Always returns silently on bad input or a missing
        notification backend so a malformed payload cannot bring the GUI loop
        down.
        """
        try:
            text = message.get("message", "") or ""
            if not text:
                return
            if not send_notice("Wheelhouse", text, timeout=8):
                logger.warning(
                    "click_first_use_hint: notification service unavailable; "
                    "hint not shown",
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "click_first_use_hint render failed: %s", exc, exc_info=True,
            )

    def _show_rejection_toast(self, message: dict):
        """Render a text_target_rejected event as an advisory toast (wh-lzsbd).

        Composes branched wording via :func:`compose_rejection_wording`,
        builds the "Show details" panel content, and shows the toast.
        Always returns silently on bad input so a malformed payload that
        slipped past the schema validation in main.py cannot bring the
        GUI loop down.

        wh-zib65: per-key cooldown gates the show. Within 60 seconds of
        the previous toast for the same (process, class, reason), the
        toast is suppressed. The first toast per key dwells 8 seconds;
        repeats (after the cooldown) dwell 4 seconds.
        """
        try:
            from rejection_toast_wording import (
                compose_rejection_wording,
                detail_lines,
            )
            from rejection_toast import RejectionToast

            process_name = message.get("process_name", "") or ""
            class_name = message.get("class_name", "") or ""
            control_type = message.get("control_type", "") or ""
            reason = message.get("reason", "") or ""

            # wh-dictation-gate-ux: the uncertain-category notice is
            # disabled until the dictation-gate UX redesign lands (see
            # the SUPPRESS_UNCERTAIN_REJECTION_NOTICE comment at module
            # level). Elevated notices fall through and still show.
            if SUPPRESS_UNCERTAIN_REJECTION_NOTICE:
                from shared.rejection_category import (
                    CATEGORY_UNCERTAIN,
                    categorize_rejection,
                )
                category = categorize_rejection(
                    reason=reason,
                    process_name=process_name,
                    class_name=class_name,
                )
                if category == CATEGORY_UNCERTAIN:
                    logger.debug(
                        "rejection toast suppressed "
                        "(wh-dictation-gate-ux interim disable) "
                        "process=%s class=%s control_type=%s reason=%s",
                        process_name, class_name, control_type, reason,
                    )
                    return

            # wh-vbvgf.3.1: update the active correlation_token BEFORE
            # the suppression check returns. A same-key rejection that
            # arrives while the previous toast is still visible would
            # otherwise leave the visible Try-it-anyway button bound to
            # the older token, retrying stale dictation. Updating the
            # token on every rejection (shown or suppressed) keeps the
            # visible button bound to the most recent dictation that
            # was rejected for this target.
            self._last_rejection_token = (
                message.get("correlation_token") or None
            )

            # wh-9weum.4.2: include control_type in the suppression key
            # so frameworks that share a single ClassName across many
            # control types (Chromium's Chrome_RenderWidgetHostHWND
            # hosts every interactive widget) do not collapse different
            # rejections into a single suppression bucket.
            decision = self._rejection_suppression.decide(
                (process_name, class_name, control_type, reason),
            )
            if not decision.show:
                logger.debug(
                    "rejection toast suppressed (cooldown) "
                    "process=%s class=%s control_type=%s reason=%s",
                    process_name, class_name, control_type, reason,
                )
                return

            wording = compose_rejection_wording(
                reason=reason,
                control_type=control_type,
                process_name=process_name,
                class_name=class_name,
                app_friendly_name=message.get("app_friendly_name", "") or "",
            )
            details = detail_lines(
                process_name=process_name,
                class_name=class_name,
                control_type=message.get("control_type", "") or "",
                reason=reason,
                supported_patterns=tuple(
                    message.get("supported_patterns", ()) or ()
                ),
                app_friendly_name=message.get("app_friendly_name", "") or "",
            )

            if self._rejection_toast is None:
                self._rejection_toast = RejectionToast()
                # wh-iycks: connect the click signal exactly once, on
                # construction. The toast widget is reused across
                # rejection events; reconnecting on every render would
                # cause N click handlers to fire for one click.
                self._rejection_toast.try_anyway_clicked.connect(
                    self._on_try_anyway_clicked,
                )

            self._rejection_toast.show_rejection(
                wording, details,
                lifetime_ms=decision.lifetime_ms,
            )
        except Exception as exc:
            logger.warning(
                "show_rejection_toast failed: %s", exc, exc_info=True,
            )

    def _show_click_notice(self, message: dict):
        """Render a click_element non-ok outcome as an advisory notice.

        wh-click-notice-no-gui-handler. Logic forwards a
        ``show_click_notice`` action carrying a ClickNoticeEvent payload
        for every non-ok click outcome (not_found / ambiguous /
        execution_failed). Reconstruct the event from the message (the
        wire dict carries an extra ``action`` key, which ClickNoticeEvent
        .from_dict ignores), compose the v5 wording, and show the
        ClickNoticeToast.

        A malformed payload that slipped past main.py is logged and
        dropped (ClickNoticeSchemaError), never raised, so a
        version-skewed sender cannot crash the GUI loop (wh-uf54). Any
        other rendering failure is caught and logged at WARNING; a
        broken Qt environment must not bring the GUI process down.
        """
        try:
            from click_notice_toast import ClickNoticeToast
            from click_notice_toast_wording import (
                compose_click_notice_wording,
            )
            from shared.click_notice import (
                ClickNoticeEvent,
                ClickNoticeSchemaError,
            )

            try:
                event = ClickNoticeEvent.from_dict(message)
            except ClickNoticeSchemaError as exc:
                logger.warning(
                    "show_click_notice: malformed payload dropped: %s", exc,
                )
                return

            text = compose_click_notice_wording(event)

            if self._click_notice_toast is None:
                self._click_notice_toast = ClickNoticeToast()

            self._click_notice_toast.show_notice(text)
            # wh-n29v.122: the render path used to write zero log lines, so
            # a toast that showed and auto-dismissed (8s) unobserved was
            # indistinguishable from one that never painted. One INFO line
            # after show_notice, sharing Logic's forward-line trace_id.
            logger.info(
                "click notice rendered: %r (trace_id=%s)",
                redact_transcript(text), message.get("trace_id"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "show_click_notice failed: %s", exc, exc_info=True,
            )

    def _handle_paint_overlay(self, message: dict) -> None:
        """Drive the overlay manager for a paint_overlay action (wh-n29v.53).

        Parses the inbound dict via ``PaintOverlayEvent.from_dict`` (a
        malformed payload is logged and dropped, never raised, so a
        version-skewed sender cannot crash the GUI loop -- wh-uf54), calls
        the overlay manager, and forwards the returned
        ``overlay_state_changed`` dict back to Logic on
        ``commands_to_logic_queue``. A stale-gated paint returns ``None``
        and emits nothing.
        """
        if self._overlay_manager is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_overlay import PaintOverlayEvent

            event = safe_parse(
                PaintOverlayEvent.from_dict, message, log_label="paint_overlay",
            )
            if event is None:
                return  # already logged
            result = self._overlay_manager.paint(
                event.summary,
                overlay_session_id=event.overlay_session_id,
                paint_generation=event.paint_generation,
            )
            if result is not None:
                # wh-overlay-slow-uia-stale-badges.9: the paint presented
                # (or failed partway, which can also leave windows), so
                # badges may be on screen -- arm the lease. A stale-gated
                # paint (None) presented nothing; any active lease belongs
                # to the overlay already on screen and must keep running.
                self._arm_overlay_lease(
                    (event.overlay_session_id, event.paint_generation),
                    _OVERLAY_LEASE_DEFAULT_MS,
                )
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "paint_overlay handling failed: %s", exc, exc_info=True,
            )

    def _handle_clear_overlay(self, message: dict) -> None:
        """Drive the overlay manager for a clear_overlay action (wh-n29v.53).

        Parses the inbound dict via ``ClearOverlayEvent.from_dict`` (a
        malformed payload is logged and dropped, never raised -- wh-uf54),
        tears down all overlay windows, and forwards the returned
        ``overlay_state_changed`` dict (``state="cleared"``) back to Logic.
        """
        if self._overlay_manager is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.clear_overlay import ClearOverlayEvent

            event = safe_parse(
                ClearOverlayEvent.from_dict, message, log_label="clear_overlay",
            )
            if event is None:
                return  # already logged
            result = self._overlay_manager.clear(
                overlay_session_id=event.overlay_session_id,
                paint_generation=event.paint_generation,
            )
            if result is not None:
                # wh-overlay-slow-uia-stale-badges.9: the badges are gone,
                # so nothing is left to lease. A STALE clear (None) tore
                # down nothing -- the newer overlay's lease keeps running.
                # An INCOMPLETE clear (also None, teardown_pending True)
                # left a survivor: the lease keeps covering it while the
                # retry timer finishes the teardown (.18.4).
                self._cancel_overlay_lease()
            self._arm_teardown_retry_if_pending()
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "clear_overlay handling failed: %s", exc, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Numbered-overlay badge lease (wh-overlay-slow-uia-stale-badges.9)
    # ------------------------------------------------------------------

    def _arm_overlay_lease(self, pair: tuple[int, int], lease_ms: int) -> None:
        """Start (or restart) the badge lease for ``pair``.

        The interval is clamped to Qt's signed-32-bit ceiling
        (``_QT_TIMER_MAX_INTERVAL_MS``) so ``QTimer.start`` can never raise
        ``OverflowError`` on an oversized renew.
        """
        self._overlay_lease_pair = pair
        self._overlay_lease_timer.start(
            min(int(lease_ms), _QT_TIMER_MAX_INTERVAL_MS)
        )

    def _cancel_overlay_lease(self) -> None:
        """Stop the badge lease; no badges remain to watch."""
        self._overlay_lease_pair = None
        self._overlay_lease_timer.stop()

    def _handle_overlay_lease_renew(self, message: dict) -> None:
        """Re-arm the badge lease for a Logic keepalive renew.

        Side-channel dict (no shared/ schema -- the walk-cue precedent),
        so validation is local and defensive (bool excluded -- it is an
        int subclass). The lease audits that Logic is ALIVE and believes
        this session's badges are visible -- not that Logic's view of the
        generation matches the GUI's. So a renew is accepted when its
        session id equals the armed lease pair's session id AND its
        (sid, gen) pair is <= the armed pair
        (wh-overlay-slow-uia-stale-badges.18.5): a renew from an OLDER
        view of the same session (a late or failed refresh paint armed
        the lease at a newer pair while Logic fell back to its prior
        visible pair) still proves liveness, and the exact-pair rule made
        the 60 s lease kill visible badges in that divergence. A renew
        for a pair NEWER than the armed lease stays rejected -- the GUI
        never presented that pair; Logic's visible-pair renew (.18.2)
        covers that direction. A different session id is rejected in both
        generation directions. The re-arm keeps the ARMED pair, so a
        later expiry tears down under the pair the generation gate
        painted. A malformed ``lease_ms`` falls back to the default
        rather than dropping the renew: a healthy Logic is talking, so
        keep the lease alive.
        """
        try:
            if self._overlay_lease_pair is None:
                return
            sid = message.get("overlay_session_id")
            gen = message.get("paint_generation")
            if isinstance(sid, bool) or not isinstance(sid, int):
                return
            if isinstance(gen, bool) or not isinstance(gen, int):
                return
            armed_sid, armed_gen = self._overlay_lease_pair
            if sid != armed_sid or (sid, gen) > (armed_sid, armed_gen):
                return
            lease_ms = message.get("lease_ms")
            if isinstance(lease_ms, bool) or not isinstance(lease_ms, int) \
                    or lease_ms <= 0:
                lease_ms = _OVERLAY_LEASE_DEFAULT_MS
            self._arm_overlay_lease((armed_sid, armed_gen), lease_ms)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay_lease_renew handling failed: %s", exc, exc_info=True,
            )

    def _on_overlay_lease_expired(self) -> None:
        """The badge lease ran out: Logic stopped renewing.

        Either Logic crashed / hung, or its clear_overlay never arrived
        (a Full queue drops it with only a warning). Tear the badges down
        so they cannot outlive Logic's intent, and report
        ``state="expired"`` so a live Logic can reconcile (its machine
        closes from ``painted``; everywhere else the ack is bookkeeping).
        ``expire_lease`` returns None when the armed pair lost a race with
        a newer paint/clear -- then the newer overlay owns the screen and
        nothing is reported.
        """
        try:
            pair = self._overlay_lease_pair
            self._overlay_lease_pair = None
            if pair is None or self._overlay_manager is None:
                return
            logger.warning(
                "numbered-overlay badge lease expired for pair %s; "
                "tearing the badges down GUI-side", pair,
            )
            result = self._overlay_manager.expire_lease(
                overlay_session_id=pair[0], paint_generation=pair[1],
            )
            self._arm_teardown_retry_if_pending()
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay lease expiry handling failed: %s", exc, exc_info=True,
            )

    def _handle_reset_overlay(self, message: dict) -> None:
        """A (re)started Logic announced itself: start the overlay fresh.

        Destroys any badge windows a previous Logic left behind and resets
        the manager's generation gate (a restarted Logic numbers its pairs
        from zero, which the old high-water mark would gate forever). Also
        cancels the lease -- there is no Logic intent left to watch. No
        event is emitted; the new Logic starts from ``closed``.
        """
        try:
            self._cancel_overlay_lease()
            if self._overlay_manager is None:
                return
            self._overlay_manager.reset()
            # reset() dropped any deferred ack (the old Logic is gone),
            # but a survivor its destroy attempt failed on still needs
            # the retry (.18.4).
            self._arm_teardown_retry_if_pending()
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "reset_overlay handling failed: %s", exc, exc_info=True,
            )

    def _arm_teardown_retry_if_pending(self) -> None:
        """Arm or stop the teardown retry timer to match the manager.

        Called after every clear / expire_lease / reset drive into the
        overlay manager (wh-overlay-slow-uia-stale-badges.18.4): when a
        DestroyWindow failed, a badge window may still be on screen, and
        the retry timer is the mechanism that finishes the teardown.

        The stop branch (wh-overlay-slow-uia-stale-badges.18.8): a clean
        teardown pays the debt an EARLIER incomplete teardown armed the
        timer for. Left armed, that stale single-shot fire would arrive
        after a fresh paint took the screen (``retry_teardown`` itself
        no longer sweeps without debt; this stop is the timer-side half
        of the same fix). The timer stays armed while a deferred ack is
        still parked, so a fire can release it as late bookkeeping --
        the accepted residual.
        """
        if self._overlay_manager is None:
            return
        if self._overlay_manager.teardown_pending:
            self._overlay_teardown_retry_timer.start(
                _OVERLAY_TEARDOWN_RETRY_MS
            )
        elif not self._overlay_manager.has_deferred_teardown_ack:
            self._overlay_teardown_retry_timer.stop()

    def _on_overlay_teardown_retry(self) -> None:
        """Retry destroying badge windows an incomplete teardown left.

        Drives ``retry_teardown`` (wh-overlay-slow-uia-stale-badges.18.4):
        a clean sweep releases the deferred cleared/expired ack, which is
        forwarded to Logic as late bookkeeping (its clear-ack watchdog
        already fired its truthful ERROR at 5000 ms -- designed behavior,
        not a bug); a still-failing sweep re-arms the timer. Unbounded
        2 s retries are fine -- a repeated no-op is cheap.

        A released CLEARED ack whose pair exactly matches the armed lease
        also cancels the lease (.18.6): the clean sweep is the same
        badges-are-gone proof that makes a direct clean clear cancel it,
        and a stranded lease would later fire expire_lease over an empty
        screen and report a spurious expired ack. Exact-match only: an
        old parked cleared ack must not cancel a newer overlay's lease,
        and a deferred EXPIRED ack's own expiry already dropped its
        lease.
        """
        try:
            if self._overlay_manager is None:
                return
            ack = self._overlay_manager.retry_teardown()
            if ack is not None:
                if ack.get("state") == "cleared" and (
                    ack.get("overlay_session_id"),
                    ack.get("paint_generation"),
                ) == self._overlay_lease_pair:
                    self._cancel_overlay_lease()
                self._emit_overlay_state_changed(ack)
            if self._overlay_manager.teardown_pending:
                self._overlay_teardown_retry_timer.start(
                    _OVERLAY_TEARDOWN_RETRY_MS
                )
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "overlay teardown retry failed: %s", exc, exc_info=True,
            )

    def _handle_paint_grid(self, message: dict) -> None:
        """Drive the grid manager for a paint_grid action (wh-grid-paint-mode).

        Parses the inbound dict via ``PaintGridEvent.from_dict`` (a malformed
        payload is logged and dropped, never raised, so a version-skewed
        sender cannot crash the GUI loop -- wh-uf54) and hands the event
        straight to the manager. Nothing is sent back to Logic: the grid has
        no in-flight build phase and no generation pair to acknowledge.
        """
        if self._grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_grid import PaintGridEvent

            event = safe_parse(
                PaintGridEvent.from_dict, message, log_label="paint_grid",
            )
            if event is None:
                return  # already logged
            self._grid_overlay.paint_grid(event)
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "paint_grid handling failed: %s", exc, exc_info=True,
            )

    def _handle_paint_grid_pin(self, message: dict) -> None:
        """Drive the grid manager for a paint_grid_pin action
        (wh-grid-paint-mode).

        Same defensive parse as ``_handle_paint_grid``. The pin does not
        replace the grid -- the manager repaints both -- so no clear is sent
        first.
        """
        if self._grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.paint_grid_pin import PaintGridPinEvent

            event = safe_parse(
                PaintGridPinEvent.from_dict, message,
                log_label="paint_grid_pin",
            )
            if event is None:
                return  # already logged
            self._grid_overlay.paint_grid_pin(event)
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "paint_grid_pin handling failed: %s", exc, exc_info=True,
            )

    def _handle_clear_grid(self, message: dict) -> None:
        """Drive the grid manager for a clear_grid action
        (wh-grid-paint-mode).

        ``ClearGridEvent`` carries no fields, but it is still parsed so a
        payload addressed to a different action can never tear the grid down.
        One clear removes the grid AND the pin.
        """
        if self._grid_overlay is None:
            return
        try:
            from shared.ipc_schema_validation import safe_parse
            from shared.clear_grid import ClearGridEvent

            event = safe_parse(
                ClearGridEvent.from_dict, message, log_label="clear_grid",
            )
            if event is None:
                return  # already logged
            self._grid_overlay.clear_grid()
        except Exception as exc:  # noqa: BLE001 - the grid is non-critical
            logger.warning(
                "clear_grid handling failed: %s", exc, exc_info=True,
            )

    def _emit_overlay_state_changed(self, result) -> None:
        """Forward an overlay_state_changed dict back to Logic (wh-n29v.53).

        ``result`` is the manager's ``overlay_state_changed`` wire dict, or
        ``None`` for a stale-gated paint (nothing to report). A Full queue
        is logged and dropped rather than raised.
        """
        if not result:
            return
        try:
            self.commands_to_logic_queue.put_nowait(result)
        except Full:
            logger.warning(
                "overlay_state_changed: commands_to_logic_queue Full; "
                "dropping the overlay state report",
            )

    def _on_try_anyway_clicked(self) -> None:
        """Forward a Try-it-anyway click as a try_anyway_clicked event (wh-iycks).

        Reads the most recently captured correlation_token (from the
        rejection event that produced the visible toast) and posts a
        canonical action onto commands_to_logic_queue. The Logic
        process resolves the token in its own cache and either fires
        the retry pipeline or surfaces a click_too_late follow-up
        toast.

        Defensive noop when no token is captured: a click that lands
        before the manager records a token would have no rejection to
        retry. We log + drop rather than send a token-less event that
        the Logic-side schema would reject.
        """

        token = self._last_rejection_token
        if not token:
            logger.warning(
                "try_anyway_clicked: no correlation_token captured; "
                "dropping click",
            )
            return
        # wh-override-paste-focus-drift round 2: grant Input the right
        # to SetForegroundWindow before queueing the IPC. The Input
        # process cannot do this itself because Windows blocks
        # SetForegroundWindow from processes that have not received
        # recent user input. The grant must happen BEFORE send_command
        # so it is in place by the time Input's retry handler calls
        # SetForegroundWindow.
        _grant_foreground_to_any_process()
        self.send_command({
            "action": "try_anyway_clicked",
            "correlation_token": token,
        })

    # ------------------------------------------------------------------
    # Three-strikes grant prompt (wh-bqv9c)
    # ------------------------------------------------------------------

    def _show_grant_prompt_toast(self, message: dict) -> None:
        """Render a text_target_grant_prompt event as a follow-up toast.

        The Logic process forwards a ``text_target_grant_prompt``
        action onto the state queue when the click counter reaches the
        soft-allow threshold for an identity tuple. This handler:

          * Validates the payload via
            ``TextTargetGrantPromptEvent.from_dict``. A malformed
            payload is logged and dropped (per wh-uf54).
          * Computes the per-tuple dedup key. If the tuple is already
            in ``_grant_prompt_acted_on``, the toast is suppressed --
            the user has already chosen Yes or No this session.
          * Records the active tuple so a subsequent Yes / No click
            can attach it to the IPC payload sent back to Logic.
          * Builds the GrantPromptToast widget on first use, connecting
            its three signals once. Subsequent renders reuse the
            instance.
          * Composes title / body strings with the friendly app name
            and the current per-tuple count.
          * Calls ``show_prompt`` on the widget.

        Failure handling: any exception inside the rendering path is
        caught and logged at WARNING. A dropped toast is recoverable --
        the next threshold event for the same tuple will re-fire if
        the user dismisses without choosing.
        """
        try:
            from shared.text_target_grant_prompt import (
                TextTargetGrantPromptEvent,
                TextTargetGrantPromptSchemaError,
            )

            # The dispatch action is rebranded back to ``"type"`` so
            # the shared schema validator (which expects ``"type"``)
            # can consume the payload symmetrically with the other
            # IPC schemas in the same package.
            payload = dict(message)
            payload["type"] = payload.pop("action", None)

            try:
                event = TextTargetGrantPromptEvent.from_dict(payload)
            except TextTargetGrantPromptSchemaError as exc:
                logger.warning(
                    "text_target_grant_prompt event dropped, malformed "
                    "payload: %s", exc,
                )
                return

            tuple_key = (
                event.process_name, event.class_name, event.control_type,
            )
            if tuple_key in self._grant_prompt_acted_on:
                logger.debug(
                    "grant_prompt suppressed (already acted on this "
                    "session): tuple=%s", tuple_key,
                )
                return

            # wh-vbvgf.7.2 (codex review of wh-bqv9c): if a grant prompt
            # is currently visible for a different tuple, drop the new
            # event. Replacing the toast mid-presentation can misattribute
            # a click that the user had already decided to make for the
            # original tuple. The dropped event re-fires the next time
            # the click counter publishes RetryThresholdReached for the
            # same tuple, so no permanent loss.
            if (
                self._grant_prompt_toast is not None
                and self._grant_prompt_toast.isVisible()
                and self._active_grant_tuple is not None
                and self._active_grant_tuple != tuple_key
            ):
                logger.debug(
                    "grant_prompt suppressed (different tuple visible): "
                    "visible=%s incoming=%s",
                    self._active_grant_tuple, tuple_key,
                )
                return

            from grant_prompt_toast import GrantPromptToast

            if self._grant_prompt_toast is None:
                self._grant_prompt_toast = GrantPromptToast()
                # Connect the three signals exactly once, on
                # construction. The widget is reused across threshold
                # events; reconnecting on every render would cause N
                # click handlers to fire for one click.
                self._grant_prompt_toast.yes_clicked.connect(
                    self._on_grant_prompt_yes_clicked,
                )
                self._grant_prompt_toast.no_clicked.connect(
                    self._on_grant_prompt_no_clicked,
                )
                self._grant_prompt_toast.dismissed.connect(
                    self._on_grant_prompt_dismissed,
                )

            title = (
                f"Always type into {event.app_friendly_name} when you do this?"
            )
            body = (
                f"You have tried this {event.count} times in "
                f"{event.app_friendly_name}. Wheelhouse can stop "
                "asking and just do it from now on."
            )
            self._grant_prompt_toast.show_prompt(title=title, body=body)
            # wh-vbvgf.8.1 (deepseek review): set _active_grant_tuple
            # AFTER show_prompt succeeds. If the show raises, leave the
            # previous value (None on first show, or the previous tuple
            # on re-show) in place so a stale tuple never gets attached
            # to a click handler that fires for an invisible toast.
            self._active_grant_tuple = tuple_key
        except Exception as exc:
            logger.warning(
                "show_grant_prompt_toast failed: %s", exc, exc_info=True,
            )

    def _on_grant_prompt_yes_clicked(self) -> None:
        """Handle a Yes click on the grant prompt (wh-bqv9c, wh-8d81z).

        Forwards a ``grant_prompt_yes_clicked`` action onto
        commands_to_logic_queue carrying the identity tuple. The Logic
        handler writes the soft-allow file, sends
        ``add_soft_allow_tuple`` IPC to the input process on success,
        and resets the click counter for the tuple.

        Order of operations (wh-vbvgf.9.1 codex review): the active
        tuple is added to ``_grant_prompt_acted_on`` ONLY after the
        ``put_nowait`` on commands_to_logic_queue succeeds. If the
        queue is Full and the command is dropped, the dedup set is
        left unchanged so a later threshold event for the same tuple
        re-fires the toast and gives the user a second chance to
        grant. Without this guard, a single dropped enqueue would
        suppress the entire follow-up path for the rest of the GUI
        process session.

        Defensive noop when no active tuple is recorded: a click that
        somehow lands before ``_show_grant_prompt_toast`` set the
        tuple would have nothing to attach. We log + drop rather than
        send a tupleless event the Logic-side schema would reject.
        """
        if self._active_grant_tuple is None:
            logger.warning(
                "grant_prompt_yes_clicked: no active tuple; dropping click",
            )
            return
        tuple_key = self._active_grant_tuple
        process_name, class_name, control_type = tuple_key
        try:
            self.commands_to_logic_queue.put_nowait({
                "action": "grant_prompt_yes_clicked",
                "process_name": process_name,
                "class_name": class_name,
                "control_type": control_type,
            })
        except Full:
            logger.warning(
                "grant_prompt_yes_clicked: commands_to_logic_queue Full; "
                "dropping click and leaving dedup unchanged so a later "
                "threshold event can re-fire the toast",
            )
            return
        self._grant_prompt_acted_on.add(tuple_key)
        logger.info(
            "grant_prompt_yes_clicked: tuple=%s forwarded to Logic", tuple_key,
        )

    def _on_grant_prompt_no_clicked(self) -> None:
        """Handle a No click on the grant prompt (wh-bqv9c, wh-vdt1t, wh-27gvv).

        Forwards a ``grant_prompt_no_clicked`` action onto
        commands_to_logic_queue carrying the identity tuple. After a
        successful disk write to ``soft_allow_declined_tuples.toml``,
        Logic records the tuple in its ``_grant_prompt_no_suppressed``
        set so subsequent ``RetryThresholdReached`` events for the
        same tuple drop their GUI forward. After wh-27gvv the No
        choice is durable across Logic restarts and GUI restarts:
        Logic reloads the declined file at startup via
        ``_load_declined_tuples``, so the suppression survives a
        full WheelHouse relaunch.

        The counter is intentionally NOT reset (per bead spec wh-vdt1t):
        future verified retries still increment, but the follow-up
        toast does not re-fire because the Logic forwarder consults
        the suppression set first.

        IMPORTANT (wh-vbvgf.13.1, wh-27gvv.2.2): unlike the Yes path,
        this method does NOT add the tuple to
        ``_grant_prompt_acted_on``. Logic owns the authoritative
        suppression for No (per wh-vbvgf.12.1). After wh-27gvv that
        suppression is durable across restarts because Logic reloads
        ``soft_allow_declined_tuples.toml`` at startup; before
        wh-27gvv it was in-memory only. In either era the GUI dedup
        is unnecessary for the No path, and adding one would create a
        second source of truth that can diverge from Logic within the
        same session: on a disk-write failure ``add_declined``
        deliberately does NOT update Logic's in-memory suppression
        (so the user can click No again and try the save), but a
        hypothetical GUI dedup would already have suppressed the
        prompt for the rest of the session and silently blocked the
        retry. The Yes path's GUI dedup exists for a different
        reason -- it guards a within-session race where the Input
        process has not yet acknowledged the ``add_soft_allow_tuple``
        IPC after a successful disk write, so a second prompt could
        fire before the predicate stops rejecting the same control.
        The No path has no equivalent IPC-acknowledgement race
        because No's suppression is entirely Logic-side. A future
        maintainer who notices the asymmetry with the Yes path
        should NOT "fix" it by adding the dedup line -- doing so
        reintroduces wh-vbvgf.12.1.

        Defensive noop when no active tuple is recorded: a click that
        somehow lands before ``_show_grant_prompt_toast`` set the
        tuple would have nothing to attach.
        """
        if self._active_grant_tuple is None:
            logger.warning(
                "grant_prompt_no_clicked: no active tuple; dropping click",
            )
            return
        tuple_key = self._active_grant_tuple
        process_name, class_name, control_type = tuple_key
        try:
            self.commands_to_logic_queue.put_nowait({
                "action": "grant_prompt_no_clicked",
                "process_name": process_name,
                "class_name": class_name,
                "control_type": control_type,
            })
        except Full:
            logger.warning(
                "grant_prompt_no_clicked: commands_to_logic_queue Full; "
                "dropping click. The next threshold event re-fires "
                "the toast and gives the user another chance.",
            )
            return
        # wh-vbvgf.12.1 (codex review) / wh-27gvv / wh-27gvv.2.2
        # (deepseek review): do NOT add to _grant_prompt_acted_on
        # for No. Logic owns the authoritative suppression -- after
        # wh-27gvv it is durable across restarts because Logic
        # reloads soft_allow_declined_tuples.toml at startup. A
        # GUI-side dedup entry would create a within-session
        # divergence: on a disk-write failure add_declined does NOT
        # update Logic's in-memory suppression (so the user can
        # click No again and retry the save), but a GUI dedup
        # would already have suppressed the prompt for the rest of
        # the session and silently blocked the retry. The Yes path
        # has a GUI dedup for an unrelated reason (IPC-acknowledge
        # race within one session). See the docstring above.
        logger.info(
            "grant_prompt_no_clicked: tuple=%s forwarded to Logic "
            "(no GUI dedup add -- Logic owns suppression)",
            tuple_key,
        )

    def _on_grant_prompt_dismissed(self) -> None:
        """Handle a dismiss-without-click on the grant prompt.

        The bead spec says: a dismiss-without-click resets the dedup
        for that tuple so the next threshold event re-fires the
        toast. We accomplish this by NOT adding the tuple to the
        acted-on set, and by clearing the active-tuple slot so a
        late Yes/No click after a re-show attaches to the right
        identity.
        """
        if self._active_grant_tuple is None:
            return
        logger.debug(
            "grant_prompt_dismissed: tuple=%s (dedup not recorded)",
            self._active_grant_tuple,
        )
        self._active_grant_tuple = None

    # ------------------------------------------------------------------
    # Soft-allow disk-write failure (wh-9dkse)
    # ------------------------------------------------------------------

    def _show_soft_allow_write_failed_toast(self, message: dict) -> None:
        """Render a soft_allow_write_failed event as a follow-up toast.

        LogicController.add_soft_allow emits this action onto the GUI
        state queue when the soft-allow file write fails after a Yes
        click on the three-strikes grant prompt. The handler:

          * Removes the identity tuple from
            ``_grant_prompt_acted_on`` so the next threshold event
            for the same tuple can re-fire the grant prompt within
            the same GUI session (wh-vbvgf.18.1, deepseek review).
            Without this, the Yes-click handler's dedup add would
            permanently suppress the prompt for the rest of the GUI
            process even though the persistence write failed.
          * Builds the SoftAllowWriteFailedToast widget on first use;
            the instance is reused across events.
          * Composes the fixed-wording title and body. The toast is
            informational and identity-agnostic, so the event's
            process_name / class_name / control_type fields are not
            surfaced in the user-visible string.
          * Calls ``show_message`` on the widget.

        The toast offers no retry path. The user re-attempts later by
        saying the dictation words again, which re-fires the verified-
        retry counter; when the counter next crosses the soft-allow
        threshold the grant prompt re-fires and the user can click Yes
        again.

        Failure handling: any exception inside the rendering path is
        caught and logged at WARNING. A dropped toast is recoverable;
        the next disk-write failure will re-fire the event.
        """
        try:
            from soft_allow_write_failed_toast import (
                SoftAllowWriteFailedToast,
            )

            # The wording is fixed; the payload is recorded only for
            # diagnostic correlation with the Logic-side write-failure
            # log line.
            logger.debug(
                "soft_allow_write_failed: process=%s class=%s control=%s",
                message.get("process_name"),
                message.get("class_name"),
                message.get("control_type"),
            )

            # wh-vbvgf.18.1 (deepseek review): clear the tuple from
            # _grant_prompt_acted_on so the user can actually do what
            # the toast tells them to ("click Yes again"). The Yes
            # click handler adds the tuple to that set; without this
            # discard, the next text_target_grant_prompt event for
            # the same tuple is suppressed by the GUI dedup at line
            # ~861, even though Logic correctly does NOT reset the
            # counter on DISK_FAILED. Result: the user is told to
            # retry but the prompt never re-fires until the GUI
            # process restarts.
            #
            # The discard is guarded on a fully-formed tuple so a
            # malformed payload cannot accidentally re-arm a
            # different tuple's prompt. all((a, b, c)) is False if
            # any of the three is the empty string.
            tuple_key = (
                message.get("process_name", "") or "",
                message.get("class_name", "") or "",
                message.get("control_type", "") or "",
            )
            if all(tuple_key):
                self._grant_prompt_acted_on.discard(tuple_key)

            if self._soft_allow_write_failed_toast is None:
                self._soft_allow_write_failed_toast = (
                    SoftAllowWriteFailedToast()
                )

            self._soft_allow_write_failed_toast.show_message(
                title="Wheelhouse couldn't save your choice",
                body=(
                    "Try saying the words again later, "
                    "then click Yes again."
                ),
            )
        except Exception as exc:
            logger.warning(
                "soft_allow_write_failed toast failed: %s",
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Declined-tuple disk-write failure (wh-27gvv)
    # ------------------------------------------------------------------

    def _show_declined_write_failed_toast(self, message: dict) -> None:
        """Render a declined_write_failed event as a follow-up toast.

        LogicController.add_declined emits this action onto the GUI
        state queue when the declined-tuple file write fails after a
        No click on the three-strikes grant prompt. The handler:

          * Reuses the SoftAllowWriteFailedToast widget (same plaque
            styling for the "couldn't save your choice" wording) on
            first use; the instance is reused across events.
          * Composes the fixed-wording title and body. The body
            steers the user back to clicking No again, not Yes.

        Note: unlike _show_soft_allow_write_failed_toast, this handler
        does NOT discard the tuple from _grant_prompt_acted_on. The
        Yes handler adds the tuple to that dedup set on click; the No
        handler deliberately does not (see _on_grant_prompt_no_clicked
        docstring and wh-vbvgf.12.1). So there is nothing to clear on
        the No-failure path. The Logic-side forwarder will publish a
        fresh approval prompt for the same control on the next
        verified-retry threshold (add_declined did not update the
        in-memory suppression set on disk failure), and the GUI will
        render it normally.

        Failure handling: any exception inside the rendering path is
        caught and logged at WARNING. A dropped notice is recoverable;
        the next disk-write failure will re-fire the event.
        """
        try:
            from soft_allow_write_failed_toast import (
                SoftAllowWriteFailedToast,
            )

            logger.debug(
                "declined_write_failed: process=%s class=%s control=%s",
                message.get("process_name"),
                message.get("class_name"),
                message.get("control_type"),
            )

            if self._soft_allow_write_failed_toast is None:
                self._soft_allow_write_failed_toast = (
                    SoftAllowWriteFailedToast()
                )

            self._soft_allow_write_failed_toast.show_message(
                title="Wheelhouse couldn't save your choice",
                body=(
                    "Try saying the words again later, "
                    "then click No again."
                ),
            )
        except Exception as exc:
            logger.warning(
                "declined_write_failed toast failed: %s",
                exc,
                exc_info=True,
            )

    def update_ui_state(self):
        """:flow: GUI State Synchronization
        :step: 8
        :description: Propagates internal state changes to visual UI elements
        :data_in: GuiManager instance state variables
        :data_out: Updated floating button and tray menu visuals
        :notes: Called after internal state variables are updated (from step 7). Synchronizes three visual components: (1) button.set_state() - updates button color/appearance based on speech_enabled, (2) button.setVisible() - shows/hides button based on button_visible, (3) update_tray_menu() - rebuilds tray icon menu to reflect current state.
        """
        self.button.set_state(self.speech_enabled and self._ptt_feedback not in ("pending", "refused"))
        self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")
        self.button.setVisible(self.button_visible)
        self.update_tray_menu()

    def send_command(self, command: dict):
        """:flow: GUI State Synchronization
        :step: 2
        :description: Queues command dictionary to logic process via IPC
        :data_in: Command dictionary with 'action' key
        :data_out: Command placed in commands_to_logic_queue
        :notes: IPC transport layer using multiprocessing.Queue for cross-process communication. Uses put_nowait() to avoid blocking GUI thread. If queue is full, command is dropped with warning log. This is the outbound half of bidirectional GUI↔Logic IPC. Queue is consumed by main.py's _listen_for_gui_commands() in logic process.
        """
        # An acknowledged settings write, set_config_value or
        # set_config_values, takes the staged path: a request ID, pending
        # bookkeeping, reconciliation on a silence, and a failure message.
        if command.get('action') in ('set_config_value', 'set_config_values'):
            self._send_settings_command(command)
            return
        try:
            self.commands_to_logic_queue.put_nowait(command)
            return True
        except Full:
            logger.warning("Logic process queue full, command dropped: %s", command.get('action'))
            return False

    @property
    def settings_pending(self):
        return bool(self._settings_requests)

    def _settings_show_status(self, text, *, failure=False):
        self.settings_status_text = text
        rendered = False
        try:
            if self._settings_notice is None:
                from soft_allow_write_failed_toast import SoftAllowWriteFailedToast
                self._settings_notice = SoftAllowWriteFailedToast()
            self._settings_notice.setAccessibleName(text)
            self._settings_notice.show_message(
                title='WheelHouse settings', body=text,
                lifetime_ms=15000 if failure else 2147483647,
            )
            rendered = True
        except Exception:
            logger.exception('Could not render settings notice')
        if failure or not rendered:
            # send_notice does not catch its own delivery failures.
            try:
                send_notice('WheelHouse settings', text, timeout=15)
            except Exception:
                logger.exception('Could not deliver the settings notice')

    def _send_settings_command(self, command):
        values = (dict(command.get('values') or {})
                  if command['action'] == 'set_config_values'
                  else {command['key']: command['value']})
        if not values:
            return
        request_id = uuid.uuid4().hex
        # Register only an enqueued write. A rejected newer command must not
        # supersede an older write whose acknowledgement is still in flight.
        try:
            self.commands_to_logic_queue.put_nowait(dict(command, request_id=request_id))
        except (Full, OSError, ValueError):
            # save_correction=False: this runs because the queue refused the
            # write. Saving a correction would send another command down the
            # same queue, fail again, and roll back again without end. The
            # button still comes back on screen; only the write waits.
            self._apply_geometry((self._settings_confirmed.get('FLOATING_BUTTON_SIZE', 50),
                                  self._settings_confirmed.get('FLOATING_BUTTON_POS', [100, 100])),
                                 save_correction=False)
            self._settings_show_status("Couldn't send settings. Restored the confirmed values.", failure=True)
            return
        self._settings_requests[request_id] = {
            'values': values, 'deadline': time.monotonic() + 5.0, 'attempts': 0,
        }
        for key in values:
            self._settings_latest[key] = request_id
        # Superseded groups may still own other keys; retain those keys only.
        for old_id, pending in list(self._settings_requests.items()):
            pending['values'] = {key: value for key, value in pending['values'].items()
                                 if self._settings_latest.get(key) == old_id}
            if not pending['values']:
                del self._settings_requests[old_id]
        self._settings_show_status('Saving settings. Waiting for confirmation.')

    def _settings_filter_state(self, message):
        message = dict(message)
        for key, wire_key, default in (
            ('FLOATING_BUTTON_SIZE', 'FLOATING_BUTTON_SIZE', 50),
            ('FLOATING_BUTTON_POS', 'FLOATING_BUTTON_POS', [100, 100]),
            ('FLOATING_BUTTON_VISIBLE', 'button_visible', True),
            ('SHOW_SPEECH_PULSE', 'SHOW_SPEECH_PULSE', True),
        ):
            pending = self._settings_requests.get(self._settings_latest.get(key))
            if pending:
                message[wire_key] = pending['values'][key]
            else:
                # Live state still drives the display, but only the disk
                # snapshot can advance the rollback/confirmation baseline.
                persisted = message.get('settings_persisted', {})
                self._settings_confirmed[key] = persisted.get(key, message.get(wire_key, default))
        return message

    def _handle_settings_result(self, message):
        if message['action'] == 'config_write_result' and type(message.get('saved')) is not bool:
            return
        request_id = message.get('request_id')
        pending = self._settings_requests.get(request_id)
        if pending is None:
            return
        values = message.get('values', {})
        keys = [key for key in pending['values']
                if self._settings_latest.get(key) == request_id and key in values]
        if not keys:
            return
        failed = message['action'] == 'config_write_result' and message.get('saved') is False
        defaults = {'FLOATING_BUTTON_SIZE': 50, 'FLOATING_BUTTON_POS': [100, 100],
                    'FLOATING_BUTTON_VISIBLE': True, 'SHOW_SPEECH_PULSE': True}
        for key in keys:
            self._settings_confirmed[key] = (defaults.get(key) if values[key] is None
                                             else values[key])
            del pending['values'][key]
        if not pending['values']:
            del self._settings_requests[request_id]
        geometry = (
            self._settings_confirmed.get('FLOATING_BUTTON_SIZE', 50),
            self._settings_confirmed.get('FLOATING_BUTTON_POS', [100, 100]),
        )
        # A group reply must not move a different key with a newer request.
        geometry = tuple(
            self._settings_requests[self._settings_latest[key]]['values'][key]
            if self._settings_latest.get(key) in self._settings_requests else value
            for key, value in zip(('FLOATING_BUTTON_SIZE', 'FLOATING_BUTTON_POS'), geometry)
        )
        if self.button._gesture_running and not failed:
            self._deferred_geometry = geometry
        else:
            self._apply_geometry(geometry)
        if 'FLOATING_BUTTON_VISIBLE' in keys:
            self.button_visible = self._settings_confirmed['FLOATING_BUTTON_VISIBLE']
        if 'SHOW_SPEECH_PULSE' in keys:
            self.show_speech_pulse = self._settings_confirmed['SHOW_SPEECH_PULSE']
        self.update_ui_state()
        if failed:
            self._settings_show_status("Couldn't save settings. Restored the confirmed values.", failure=True)
        elif not self.settings_pending:
            self.settings_status_text = ''
            if self._settings_notice is not None:
                self._settings_notice.close()

    def _settings_fail_request(self, request_id):
        """Retire a request that never came back, and say so."""
        self._settings_requests.pop(request_id, None)
        self._settings_show_status(
            "Couldn't confirm the settings. They may not have been saved.", failure=True)

    def _check_settings_timeout(self):
        now = time.monotonic()
        for request_id, pending in list(self._settings_requests.items()):
            if now < pending['deadline']:
                continue
            # A stalled logic process must end in a failure the user can
            # see, not a notice that waits for an answer forever.
            if pending['attempts'] >= 3:
                self._settings_fail_request(request_id)
                continue
            pending['attempts'] += 1
            pending['deadline'] = now + 5.0
            self._settings_show_status('Settings outcome unknown. Checking the current settings.')
            try:
                self.commands_to_logic_queue.put_nowait({
                    'action': 'get_config_values', 'request_id': request_id,
                    'keys': list(pending['values']),
                })
            except (Full, OSError, ValueError):
                logger.warning('Settings reconciliation queue unavailable')
                self._settings_fail_request(request_id)

    def send_toggle_speech_command(self):
        """:flow: GUI State Synchronization
        :step: 1
        :description: User clicks floating button to toggle speech state
        :data_in: Mouse click event from user
        :data_out: Command dictionary with toggle action
        :notes: Entry point for GUI-initiated state changes. Constructs IPC command payload {'action': 'toggle_speech_enabled_state'} and queues it for logic process. Ignores clicks before initial_state_received to prevent race conditions during startup. This represents the GUI→Logic direction of bidirectional IPC communication.
        """
        if not self.initial_state_received: return
        self.send_command({'action': 'toggle_speech_enabled_state'})

    def send_size_change_command(self, new_size: int):
        """Send config update to Logic process for button diameter change."""
        self.send_command({'action': 'set_config_value', 'key': 'FLOATING_BUTTON_SIZE', 'value': new_size})
        
    def send_pos_change_command(self, new_pos: QPoint):
        """Send config update to Logic process for button position change."""
        self._deferred_geometry = None
        self.send_command({'action': 'set_config_value', 'key': 'FLOATING_BUTTON_POS', 'value': [new_pos.x(), new_pos.y()]})

    def _apply_geometry(self, geometry, save_correction=True, screens=None):
        """Put a size and a position from the settings onto the button.

        Every route that applies a stored position goes through here -- the
        first message from the Logic process, a settings acknowledgement, the
        apply held back during a gesture, and the rollback when the command
        queue refuses a write. So this is where a position that no longer
        lands on a screen gets corrected, and one correction covers them all
        (wh-floating-button-offscreen).

        ``save_correction`` decides whether a correction is written back to
        the settings as well as shown. It is true everywhere except the
        rollback: that path runs BECAUSE the command queue refused a write,
        and sending another command down the same queue would fail, roll back,
        correct and send again without end. The button still comes back on
        screen there; only the write waits for a queue that works.

        ``screens`` is for the callers that already hold a screen list, and
        for tests. None means ask Qt now.
        """
        size, pos = geometry
        self._deferred_geometry = None
        if screens is None:
            screens = _screen_bounds()
        corrected = correct_onto_any_screen(pos[0], pos[1], size, screens)
        self.button.set_size(size)
        self.button.move(QPoint(*corrected))
        if save_correction and list(corrected) != list(pos):
            # Without this the button comes back, but the settings keep the
            # position that lost its screen, and the correction has to run
            # again on every start.
            self.send_pos_change_command(QPoint(*corrected))

    def _native_button_rect(self):
        """The button's window rectangle in PHYSICAL pixels, or None.

        Qt's own geometry is logical, and it can disagree with what Windows
        holds. Probe 4 measured the disagreement: after a resolution change
        the native rectangle stayed at the old physical position and the old
        physical size, wholly outside the new desktop, while Qt still
        reported the stored logical position (wh-floating-button-offscreen).
        """
        try:
            import win32gui
            return win32gui.GetWindowRect(int(self.button.winId()))
        except Exception as exc:
            # Criterion B7: a reading that fails leaves the button exactly as
            # it is today, and says so at debug level.
            logger.debug('Floating button: could not read the window rectangle: %s', exc)
            return None

    def _screen_pixel_ratio(self):
        """The device pixel ratio of the SCREEN the button is on, or None.

        The WINDOW's own ratio must not be used here. Probe 4 measured it lag
        behind: three display signals in a row read the window at 3.0 while
        the screen already read 2.0, so a comparison against the window's
        ratio reports agreement at the exact moment the fault exists.
        """
        try:
            handle = self.button.windowHandle()
            screen = handle.screen() if handle is not None else None
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            return float(screen.devicePixelRatio()) if screen is not None else None
        except Exception as exc:
            logger.debug('Floating button: could not read the screen scale: %s', exc)
            return None

    def _native_geometry_disagrees(self, native):
        """Does the window rectangle contradict the Qt geometry and the scale?

        crewcut: the wanted rectangle is the logical position multiplied by
        the scale, which is the screen origin only on a desktop whose screens
        share one origin and one scale. On a multiple-monitor desktop where a
        screen's logical origin is not its physical origin divided by that
        screen's ratio, this can report a disagreement that is not one. The
        cost of that mistake is one re-apply of the same logical geometry, so
        the button does not move; removing it needs a physical-origin reading
        per screen, which Qt does not publish.
        """
        ratio = self._screen_pixel_ratio()
        if native is None or ratio is None:
            return False
        left, top, right, bottom = native
        pos = self.button.pos()
        wanted_left = int(round(pos.x() * ratio))
        wanted_top = int(round(pos.y() * ratio))
        wanted_size = int(round(self.button.width() * ratio))
        # One pixel of rounding either way, the tolerance criterion B1 names.
        position_disagrees = (abs(left - wanted_left) > 1
                              or abs(top - wanted_top) > 1)
        size_disagrees = (abs((right - left) - wanted_size) > 1
                          or abs((bottom - top) - wanted_size) > 1)
        return position_disagrees or size_disagrees

    def _force_native_geometry(self):
        """Put the button's WINDOW where its Qt geometry says it is.

        Qt sends nothing to Windows when a move() or a setGeometry() carries
        the geometry it has already cached, so the correction in
        _apply_geometry cannot repair this case by itself: after a resolution
        change correct_onto_any_screen returns the stored position unchanged,
        and the move to that same position never reaches Windows. Probe 4
        measured the remedy on 2026-09-20. A move to a DIFFERENT position
        reaches Windows, and the setGeometry after it differs from THAT, so
        it reaches Windows too and carries the size with it. Both calls are
        needed: the move alone leaves the window one pixel off and the old
        size, and the setGeometry alone is the call Qt drops.
        """
        before = self._native_button_rect()
        forced = False
        if before is not None and self._native_geometry_disagrees(before):
            pos = self.button.pos()
            size = self.button.width()
            try:
                self.button.move(QPoint(pos.x() + 1, pos.y() + 1))
                self.button.setGeometry(pos.x(), pos.y(), size, size)
                forced = True
            except Exception as exc:
                # Criterion B7: a forcing call that fails leaves the button
                # with the behaviour it has today.
                logger.debug('Floating button: could not force the window back: %s', exc)
        after = self._native_button_rect()
        logger.debug(
            'Floating button: window rectangle %s before, %s after, forced=%s',
            before, after, forced)
        return forced

    def _schedule_one_delayed_reapply(self):
        """Check the position once more after the display change settles.

        Probe 4 measured why this is needed. One resolution change produced
        four layout signals, and the first three read the screen at the OLD
        device pixel ratio, found agreement, and rightly did nothing. Only
        the fourth saw the new ratio. When no later signal arrives after the
        ratio updates, this single-shot check is the only thing that catches
        it.

        One timer serves every display change, so one re-check is waiting at
        any moment (criterion B4), and it is single-shot, never a repeating
        timer (criterion B3). Starting it again moves its deadline instead of
        adding a second one. The deadline has to move: a second, independent
        display change that lands near the end of the first delay would
        otherwise inherit whatever was left of it, and its re-check would read
        the screen before that change's own ratio updated
        (wh-floating-button-offscreen.4.2).
        """
        if self._delayed_reapply_timer is None:
            timer = QTimer(self.button)
            timer.setSingleShot(True)
            timer.timeout.connect(self._reapply_after_the_layout_settled)
            self._delayed_reapply_timer = timer
        self._delayed_reapply_timer.start(_FORCED_REAPPLY_DELAY_MS)

    def _reapply_after_the_layout_settled(self):
        """The delayed check itself. It schedules nothing further."""
        self._on_screens_changed(
            signal_name='the delayed re-check', schedule_delayed=False)

    def _on_gesture_ended(self):
        """Apply whatever was held back while the gesture was running.

        Two things can be held back, and only one of them survives the
        gesture's own completion. A state message or a settings
        acknowledgement parks its geometry in _deferred_geometry, and that is
        applied here exactly as it always was. A screen-layout change parks a
        flag as well, because moved and resize_finished reach
        send_pos_change_command and send_resize_commit_command before
        gesture_ended fires, and both of those clear _deferred_geometry
        (wh-floating-button-offscreen.1.2).

        What the flag applies is the button as the user has just left it --
        its current width and current corner -- not the confirmed settings,
        which the gesture has just made a moment out of date. Correcting
        after every gesture instead would take away a position a user parked
        on purpose, over the taskbar or past an edge, so the flag fires only
        for a gesture a layout change really ran into.

        It does not save. The gesture's own write is already in flight, and
        its acknowledgement corrects and saves the result at the apply in
        _handle_settings_result. A second saved write from here would be two
        writes in the queue racing for the last word.

        Whichever apply runs, a gesture that a layout change ran into also
        owes the forcing and the one delayed re-check that an unheld display
        change gets (wh-floating-button-offscreen.4.1). A gesture no layout
        change ran into owes neither.
        """
        layout_changed = self._layout_changed_during_gesture
        self._layout_changed_during_gesture = False
        if self._deferred_geometry is not None:
            self._apply_geometry(self._deferred_geometry)
        elif layout_changed:
            corner = self.button.pos()
            self._apply_geometry(
                (self.button.width(), [corner.x(), corner.y()]),
                save_correction=False,
            )
        if layout_changed:
            # _begin_press sets _gesture_running on any press, so this is the
            # release of a hold that can have lasted as long as the user
            # talked. The apply above re-sends the geometry Qt already holds,
            # and that is exactly the call Qt drops, so without this the
            # window keeps the rectangle the old scale gave it. Forcing here
            # carries whatever geometry the apply settled on, which on the
            # flag's path is the button the user has just dragged, so the
            # move the user just made survives.
            self._force_native_geometry()
            self._schedule_one_delayed_reapply()

    def _on_screens_changed(self, screens=None, signal_name='a screen layout change',
                            schedule_delayed=True):
        """Re-check the stored position when the screen layout changes.

        A monitor unplugged, a resolution changed, or a Remote Desktop session
        reconnecting at another size can all leave a position that was fine
        when it was stored on no screen at all. Nothing in the button reacts
        to any of that by itself, so the floating button used to simply
        disappear (wh-floating-button-offscreen).

        The confirmed settings are re-applied, not some new position, so a
        layout change that needs no correction changes nothing.

        ``signal_name`` names the signal that arrived, for the log.
        ``schedule_delayed`` is false for the delayed re-check itself, so
        that one check never starts another.
        """
        logger.debug(
            'Floating button: %s arrived; re-checking the stored position',
            signal_name)
        geometry = (
            self._settings_confirmed.get('FLOATING_BUTTON_SIZE', 50),
            self._settings_confirmed.get('FLOATING_BUTTON_POS', [100, 100]),
        )
        if self.button._gesture_running:
            # Correcting mid-gesture would fight the pointer the user is
            # holding. The apply waits for the gesture, exactly as an
            # arriving state message does.
            self._deferred_geometry = geometry
            # The flag is what survives the gesture. The geometry above does
            # not: the gesture's completion clears it before gesture_ended.
            self._layout_changed_during_gesture = True
            return
        self._apply_geometry(geometry, screens=screens)
        # Re-applying the stored position is not enough by itself: Qt drops a
        # move whose value it has already cached, so the window Windows holds
        # can stay where the old scale put it (wh-floating-button-offscreen).
        self._force_native_geometry()
        if schedule_delayed:
            self._schedule_one_delayed_reapply()

    def _on_screen_layout_signal(self, signal_name='a screen layout signal', *args):
        """Qt slot for the layout signals whose argument this does not use.

        primaryScreenChanged carries a QScreen, geometryChanged and
        availableGeometryChanged carry a QRect, and logicalDotsPerInchChanged
        carries a float. Connecting any of them straight to
        _on_screens_changed would hand that object to its ``screens``
        parameter, and the correction would read a screen, a rectangle or a
        number as a list of screens.

        Each connection binds the signal's own name as the first argument,
        so the log names the signal that arrived (criterion B5).
        """
        self._on_screens_changed(signal_name=signal_name)

    def _on_screen_added(self, screen):
        """Watch the new screen for size changes, then re-check the position."""
        self._watch_screen(screen)
        self._on_screens_changed(signal_name='QGuiApplication.screenAdded')

    def _on_screen_removed(self, screen):
        """Re-check the position with the screen Qt is dropping left out."""
        self._on_screens_changed(
            screens=_screen_bounds(exclude=screen),
            signal_name='QGuiApplication.screenRemoved')

    def _watch_screen(self, screen):
        """Report a single screen changing size, usable area, or scale.

        Qt has no application-level signal for one screen being resized. The
        case this guards against is a Remote Desktop reconnect that brings
        the same screen back at a different size, with none added and none
        removed. Whether Windows and Qt report a reconnect that way, rather
        than as one screen removed and another added, is not measured, so
        each screen is watched on its own rather than left to the
        application-level signals.

        logicalDotsPerInchChanged is the third of them because a stored
        position is in logical pixels, and a change of scale can redefine
        them: the same monitor is fewer logical pixels across at 150 per
        cent than at 125, so a position near the right edge can end up past
        the new one. Whether Windows also fires one of the two geometry
        signals on a change of scale is not measured, so the third signal is
        connected rather than assumed to be covered by them. A repeated
        signal computes the same correction from the same stored position,
        so the button ends where the first one put it.
        """
        try:
            screen.geometryChanged.connect(
                partial(self._on_screen_layout_signal, 'QScreen.geometryChanged'))
            screen.availableGeometryChanged.connect(
                partial(self._on_screen_layout_signal,
                        'QScreen.availableGeometryChanged'))
            screen.logicalDotsPerInchChanged.connect(
                partial(self._on_screen_layout_signal,
                        'QScreen.logicalDotsPerInchChanged'))
        except (AttributeError, RuntimeError):
            # A screen already being destroyed, or a Qt build without the
            # signals. The layout-level signals still cover adding and
            # removing a monitor, so this is worth a log and no more.
            logger.exception('Could not watch a screen for size changes')

    def _watch_the_screen_layout(self):
        """Connect every signal that can invalidate a stored position."""
        app = QGuiApplication.instance()
        if app is None:
            # No Qt application yet. Nothing can be showing a button either,
            # so there is no position to protect.
            return
        try:
            app.screenAdded.connect(self._on_screen_added)
            app.screenRemoved.connect(self._on_screen_removed)
            app.primaryScreenChanged.connect(
                partial(self._on_screen_layout_signal,
                        'QGuiApplication.primaryScreenChanged'))
            for screen in QGuiApplication.screens():
                self._watch_screen(screen)
        except (AttributeError, RuntimeError):
            logger.exception('Could not watch the screen layout')

    def send_resize_commit_command(self, new_size: int, new_pos: QPoint):
        """Send the button's new size and position to Logic as one change.

        One message, not two. The pair is meaningless apart: a size that
        survived without its position puts a differently sized button at a
        corner belonging to the old size on the next start.
        """
        # The gesture's own result is newer than anything held back while it
        # was running, so the held-back geometry is dropped rather than
        # applied a moment later on top of the size the user dragged to.
        self._deferred_geometry = None
        self.send_command({
            'action': 'set_config_values',
            'values': {
                'FLOATING_BUTTON_SIZE': new_size,
                'FLOATING_BUTTON_POS': [new_pos.x(), new_pos.y()],
            },
        })

    def _request_tray_visibility_toggle(self):
        """Queue tray intent so settings state and notices stay on Qt's thread."""
        try:
            self.state_from_logic_queue.put_nowait({'action': 'toggle_button_visibility'})
        except (Full, OSError, ValueError):
            logger.warning('GUI state queue unavailable; visibility toggle not sent')
            try:
                send_notice('WheelHouse settings', "Couldn't change button visibility. Please try again.", timeout=15)
            except Exception:
                logger.exception('Could not report the refused visibility toggle')

    def toggle_button_visibility(self):
        """Toggle through the staged settings write and its outcome feedback."""
        key = 'FLOATING_BUTTON_VISIBLE'
        pending = self._settings_requests.get(self._settings_latest.get(key))
        visible = pending['values'].get(key, self.button_visible) if pending else self.button_visible
        self.send_command({'action': 'set_config_value', 'key': key, 'value': not visible})

    def toggle_interim_results(self):
        """Send command to toggle interim (partial) STT results."""
        self.send_command({'action': 'toggle_interim_results'})

    def _toggle_ptt_mode(self):
        """Toggle between 'toggle' and 'push_to_talk' interaction modes."""
        new_mode = "push_to_talk" if self.speech_interaction_mode == "toggle" else "toggle"
        self.send_command({"action": "set_speech_interaction_mode", "mode": new_mode})
        # Optimistic UI update -- mode switch disables speech for clean transition
        self.speech_interaction_mode = new_mode
        self.speech_enabled = False
        self.button.set_state(False)
        self.button.set_ptt_mode(new_mode == "push_to_talk")
        self.update_tray_menu()

    def _on_button_press(self):
        """Handle floating button mouse-down for PTT detection."""
        if not self.initial_state_received:
            return
        # Both modes use hold threshold -- quick clicks do nothing in PTT mode,
        # and defer toggle in toggle mode.
        self._ptt_held = False
        self._press_timer.start(self._PTT_HOLD_THRESHOLD_MS)

    def _on_button_release(self):
        """Handle floating button mouse-up."""
        if not self.initial_state_received:
            return
        # There is deliberately nothing here about a second release after a
        # double-click. The button reports a release only for a press it
        # recorded, and the double-click handler records none, so no second
        # release ever arrives. The manager used to hold a flag waiting for
        # one; it stayed set and swallowed the next real release instead,
        # leaving the hold timer running to fire with the mouse already up.
        if self._ptt_held:
            # Was holding for PTT -- stop it
            self._stop_ptt()
        elif self._press_timer.isActive():
            # Quick click -- timer hasn't fired
            self._press_timer.stop()
            if self.speech_interaction_mode == "push_to_talk":
                pass  # Single click does nothing in PTT mode
            else:
                # Defer toggle to allow double-click detection
                self._double_click_timer.start(self._DOUBLE_CLICK_WAIT_MS)

    def _on_hold_threshold(self):
        """Hold timer expired -- activate PTT (unless a gesture is in progress)."""
        if self.button._is_dragging or self.button._is_resizing:
            return
        self._ptt_held = True
        self._start_ptt()

    def _start_ptt(self, source: str = "floating_button"):
        """Send ptt_start command to Logic process."""
        self._ptt_held = True
        self._ptt_request_id = uuid.uuid4().hex
        self._set_ptt_feedback("pending", "Push to talk requested. Waiting for listening confirmation.")
        self.update_ui_state()
        accepted = self.send_command({"action": "ptt_start", "source": source,
                                      "request_id": self._ptt_request_id})
        if accepted is False:
            self._ptt_request_id = None
            self._ptt_ack_timer.stop()
            self._set_ptt_feedback("refused", "Push to talk could not start: the command queue is full.")
            self.update_ui_state()
        else:
            self._ptt_ack_timer.start(5000)

    def _set_ptt_feedback(self, state: str, description: str):
        changed = (state, description) != (self._ptt_feedback, self._ptt_feedback_text)
        self._ptt_feedback, self._ptt_feedback_text = state, description
        self.button.set_ptt_feedback(state, description)
        if state == "refused" and changed:
            try:
                send_notice("Push to talk", description, timeout=8)
            except Exception:
                logger.warning("PTT notice unavailable; button retains the reason", exc_info=True)

    def _handle_ptt_state(self, message: dict):
        if self._ptt_request_id is None:
            if not self._ptt_feedback or not self._ptt_held or message.get("speech_enabled", False):
                self._set_ptt_feedback("", "Listening." if message.get("speech_enabled", False) else "Not listening.")
            return
        if message.get("ptt_request_id") != self._ptt_request_id:
            # A restarted Logic has no request token and no active hold. Once
            # pending ends, its real state must replace the missing-ack notice.
            # Keep rejecting named older holds, including after timeout/release.
            if (self._ptt_feedback != "pending"
                    and message.get("ptt_request_id") is None
                    and not message.get("ptt_active", False)):
                self._ptt_request_id = None
                self._ptt_ack_timer.stop()
                self._set_ptt_feedback("", "Listening." if message.get("speech_enabled", False) else "Not listening.")
            return
        # A start reply can still be queued when the button has been released.
        # Wait for the stopped state instead of confirming a released request.
        if not self._ptt_held and message.get("ptt_active", False):
            return
        self._ptt_ack_timer.stop()
        # ptt_active first: the hold owns the release instruction. A toggle-mode
        # release restores the pre-hold setting, so the stopped reply carries
        # speech_enabled True with nothing left to release, and choosing on
        # speech alone told the user to release a hold that had already ended.
        if message.get("ptt_active", False) and message.get("speech_enabled", False):
            self._set_ptt_feedback("", "Listening. Release to stop push to talk.")
        elif message.get("ptt_active", False):
            self._set_ptt_feedback("refused", message.get("ptt_refusal_reason") or "Push to talk is not listening.")
        elif self._ptt_held:
            # The safety cutoff ends a hold the user is still holding, and it
            # restores the pre-hold setting, so this reply can say listening is
            # on. The ended hold then belongs in the text only: "refused"
            # paints the purple fill documented as not listening and turns the
            # button off through update_ui_state, which would claim an off
            # microphone while the engine transcribes -- the failure
            # wh-ptt-release-disables-speech.1.5 ruled against.
            listening = message.get("speech_enabled", False)
            self._set_ptt_feedback(
                "" if listening else "refused",
                "Listening. Push to talk ended." if listening
                else "Push to talk ended. Release and hold again to listen.")
        elif message.get("speech_enabled", False):
            self._set_ptt_feedback("", "Listening.")
        else:
            self._set_ptt_feedback("", "Push to talk released.")
        if not message.get("ptt_active", False):
            self._ptt_request_id = None

    def _ptt_pending_timeout(self):
        if self._ptt_feedback == "pending" and not self.shutdown_event.is_set():
            self._set_ptt_feedback("refused", "Listening has not been confirmed. Release and try push to talk again.")
            self.update_ui_state()
            # A restart snapshot may have arrived while pending and lacked our
            # token. Read current state once; never repeat the start command.
            self.send_command({"action": "request_initial_state"})

    def _stop_ptt(self):
        """Send ptt_stop command to Logic process.

        No guess about speech is made here. This used to show the microphone
        off at once, which was right while StateManager.ptt_stop forced the
        setting off for an ordinary release. That release now restores the
        pre-hold setting (wh-ptt-release-disables-speech), so the answer can be
        on, and the guess showed an off microphone while the speech engine was
        listening -- with a hands-free user having no other indicator
        (wh-ptt-release-disables-speech.1.5).

        This process cannot work out the answer for itself. A state update
        carries only the computed speech_enabled, not the setting and not the
        three suppression flags, and the hold's own audio override changes the
        computed answer while the hold runs. So ptt_stop's own state update
        decides, on the next queue poll, 100 ms away. _cancel_pending_press
        takes the same position for the two cancellations, since
        wh-ptt-release-disables-speech.1.8; before that it restored the value
        saved at the press.
        """
        self._ptt_held = False
        self.send_command({"action": "ptt_stop"})
        self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")

    def _on_double_click(self):
        """Handle double-click -- toggle between PTT and toggle interaction modes."""
        if not self.initial_state_received:
            return
        self._double_click_timer.stop()
        # If PTT was activated by the first click, cancel it
        if self._ptt_held:
            self._stop_ptt()
        self._toggle_ptt_mode()

    def _on_deferred_single_click(self):
        """Double-click timer expired -- execute the deferred single-click toggle."""
        self.send_toggle_speech_command()

    def _on_drag_started(self):
        """Handle drag start -- cancel any pending PTT activation."""
        self._cancel_pending_press("drag_cancel")

    def _on_press_cancelled(self):
        """Handle a press whose release never arrived.

        The button reports this when something takes the release away -- the
        context menu opening, the button being hidden, or the user pressing
        again. The press decided nothing, so it must not toggle speech; and if
        the hold threshold already opened the microphone, it has to close
        again. That is the same treatment a drag gives an interrupted hold.
        """
        self._cancel_pending_press("gesture_cancel")

    def _cancel_pending_press(self, reason: str):
        """Drop a press in progress and let Logic say what the microphone does.

        This used to put back the value saved at the press. That value is the
        computed display, while StateManager.ptt_stop restores the raw setting,
        and commit 10983243 made an explicit decision taken during the hold
        move the raw value Logic restores. The pre-press value did not move
        with it, so a cancellation put the display back to a decision the user
        had already replaced (wh-ptt-release-disables-speech.1.8).

        A cancellation ends a hold exactly as a release does, so it takes the
        same position as _stop_ptt: send the command, and show what the state
        update reports.
        """
        self._press_timer.stop()
        self._double_click_timer.stop()
        if self._ptt_held:
            # Hold timer activated PTT before the interruption -- cancel PTT.
            self._ptt_held = False
            self.send_command({"action": "ptt_stop", "reason": reason})
            self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")

    def _on_tray_left_click(self):
        """Handle system tray icon left-click with double-click detection."""
        if not self.initial_state_received:
            return
        if self._tray_click_timer and self._tray_click_timer.is_alive():
            # Second click within window -- double-click
            self._tray_click_timer.cancel()
            self._tray_click_timer = None
            self._toggle_ptt_mode()
        else:
            # First click -- defer to allow double-click detection
            self._tray_click_timer = threading.Timer(
                self._TRAY_DOUBLE_CLICK_WAIT_S,
                self._on_deferred_tray_single_click,
            )
            self._tray_click_timer.daemon = True
            self._tray_click_timer.start()

    def _on_deferred_tray_single_click(self):
        """Tray double-click timer expired -- execute deferred toggle."""
        self._tray_click_timer = None
        if self.speech_interaction_mode == "push_to_talk":
            return  # Single click does nothing in PTT mode
        self.send_toggle_speech_command()

    def switch_stt_provider(self, provider: str) -> None:
        """Request STT provider switch from Logic process."""
        self.send_command({'action': 'switch_stt_provider', 'provider': provider})

    def _get_provider_display_name(self, provider: str) -> str:
        """Get user-friendly display name for STT provider.

        Uses display names from state update if available, falls back to
        hardcoded names for legacy providers.
        """
        # First check dynamically discovered display names
        if provider in self.stt_provider_display_names:
            return self.stt_provider_display_names[provider]

        # Fallback to hardcoded names
        fallback_names = {
            "google_remote": "Google Cloud (WebSocket)",
            "google": "Google Cloud",
        }
        return fallback_names.get(provider, provider.replace("_", " ").title())

    def _on_provider_menu_click(self, provider: str, icon=None, item=None) -> None:
        """Callback for STT provider menu item click (pystray compatible).
        
        Args:
            provider: The STT provider name (bound via partial).
            icon: The pystray Icon instance (passed by pystray).
            item: The MenuItem instance (passed by pystray).
        """
        self.switch_stt_provider(provider)

    def _is_provider_checked(self, provider: str, current_provider: str, item=None) -> bool:
        """Check function for STT provider menu item (pystray compatible)."""
        return provider == current_provider

    # -- AI Model helpers --

    def switch_ai_provider(self, provider: str) -> None:
        """Request AI provider switch from Logic process."""
        self.send_command({'action': 'switch_ai_provider', 'provider': provider})

    def _get_ai_provider_display_name(self, provider: str) -> str:
        """Get user-friendly display name for AI provider."""
        if provider in self.ai_provider_display_names:
            return self.ai_provider_display_names[provider]
        return provider.replace("_", " ").title()

    def _on_ai_provider_menu_click(self, provider: str, icon=None, item=None) -> None:
        """Callback for AI provider menu item click (pystray compatible)."""
        self.switch_ai_provider(provider)

    def _is_ai_provider_checked(self, provider: str, current_provider: str, item=None) -> bool:
        """Check function for AI provider menu item (pystray compatible)."""
        return provider == current_provider

    def hide_button(self):
        """Hide floating button if currently visible."""
        if self.button_visible:
            self.toggle_button_visibility()
        
    def show_context_menu(self, position: QPoint):
        """Show Qt context menu at specified position.
        
        Args:
            position: QPoint for menu display
        """
        menu = self._create_menu(is_tray_menu=False)
        if isinstance(menu, QMenu):
            menu.exec(position)
            menu.deleteLater()

    def request_restart(self):
        """Send restart_program command to logic process and show notification."""
        try:
            self.commands_to_logic_queue.put_nowait({'action': 'restart_program'})
            send_notice("Wheelhouse", "Restarting application...")
        except Exception as e:
            logger.error(f"Failed to send restart command: {e}")
            send_notice("Wheelhouse", "Could not restart: Logic process is unresponsive.")

    def _open_help_chat(self, question: str = ""):
        """Open or show the help chat window."""
        from help_chat_window import HelpChatWindow

        if not hasattr(self, "_help_window") or self._help_window is None:
            self._help_window = HelpChatWindow(parent=None)
            self._help_window.question_submitted.connect(self._on_help_question)
            self._help_window.reset_requested.connect(self._on_help_reset)
            self._help_window.cancel_requested.connect(self._on_help_cancel)

        self._help_window.show()
        self._help_window.raise_()
        self._help_window.activateWindow()

        if question:
            self._help_window.submit_question(question)

    def _on_help_question(self, question: str):
        """Forward question to Logic process."""
        self.commands_to_logic_queue.put_nowait({
            "action": "help_ask", "question": question,
        })

    def _on_help_reset(self):
        """Forward reset to Logic process."""
        self.commands_to_logic_queue.put_nowait({"action": "help_reset"})

    def _on_help_cancel(self):
        """Forward cancel to Logic process."""
        self.commands_to_logic_queue.put_nowait({"action": "help_cancel"})

    def _open_terminal_editor(
        self,
        text: str,
        hwnd: int,
        rect: tuple,
        request_id: str = "",
        utterance_id: str = "",
    ):
        """Create or show the terminal dictation editor.

        wh-g2-refactor.18 (Section 6): on lazy construction the new
        editor's ``_editor_generation`` is seeded from
        ``self._editor_generation`` so the dispatcher's per-request
        check (request_gen == editor_gen) holds for the next IPC the
        producer stamps with the same value.
        """
        from terminal_editor_window import TerminalDictationEditorWindow

        if self._te_window is None:
            self._te_window = TerminalDictationEditorWindow(parent=None)
            self._te_window._editor_generation = self._editor_generation
            self._te_window.editor_cancelled.connect(self._on_te_cancelled)
            self._te_window.editor_event_acked.connect(self._on_te_event_acked)

        self._te_window.show_editor(
            text,
            hwnd,
            rect,
            request_id=request_id,
            utterance_id=utterance_id,
        )

    # wh-g2-refactor.18 (Section 6): callable seams the rebuilder owns.
    # The rebuilder is constructed at GuiManager init with closures that
    # ultimately fall through to these methods; keeping them as named
    # attributes makes the wiring testable without invoking PySide6.

    def _set_te_window_for_rebuilder(self, editor):
        """Replace the editor reference (rebuilder callback)."""
        self._te_window = editor

    def _set_editor_generation_for_rebuilder(self, value: int) -> None:
        """Update the GuiManager-side generation counter."""
        self._editor_generation = int(value)

    def _post_editor_rebuilt_notification(self, payload) -> None:
        """Enqueue the rebuilder's notification onto commands_to_logic_queue."""
        try:
            self.commands_to_logic_queue.put_nowait(dict(payload))
        except Exception as exc:
            logger.warning(
                "Failed to enqueue editor_rebuilt notification: %s", exc,
            )

    def _on_te_cancelled(self, request_id: str):
        """Forward cancel to Logic Process.

        wh-overlay-slow-uia-stale-badges.14.17: carries the session's
        show request_id so the input-process proxy can ignore a
        cancellation delivered late from an older session.
        """
        self.commands_to_logic_queue.put_nowait(
            {"action": "te_cancelled", "request_id": request_id},
        )

    def _on_te_event_acked(self, request_id: str, op: str, editor_hwnd: int):
        """Forward te_event ack from editor window to Logic Process (wh-t81d9.2).

        Logic forwards this to Input as a control command so the proxy can
        advance the retract accounting counter and record the editor HWND.

        Wrapped in try/except so a full logic-process queue cannot raise out
        of a Qt slot. A dropped ack is recoverable: the proxy's stale-event
        threshold catches it on the next interaction or retract.

        wh-eolas: ``op`` values ``submit_started``, ``submit_complete``,
        and ``submit_failed:<reason>`` come from the editor-direct
        submit path. They ride the same forward to Logic so the
        focus_redirect_path bridge can drive the LogicMirror through
        SUBMITTING / SUBMIT_COMPLETE / ERROR. On ``submit_failed:*``
        the GUI also fires a content-neutral notification toast
        immediately, since the user pressed Enter but nothing landed
        in the terminal. The toast text is content-neutral by design
        -- shell text can contain credentials and must not appear in
        a notification.
        """
        try:
            self.commands_to_logic_queue.put_nowait({
                "action": "te_event_ack",
                "request_id": request_id,
                "op": op,
                "editor_hwnd": editor_hwnd,
            })
        except Exception as e:
            logger.warning(
                "Failed to enqueue te_event_ack (rid=%s op=%s): %s",
                request_id, op, e,
            )
        if op.startswith("submit_failed"):
            try:
                send_notice(
                    "Terminal paste failed",
                    "Command not submitted.",
                    timeout=5,
                )
            except Exception as exc:
                logger.warning(
                    "submit_failed toast emission raised: %s", exc,
                )

    def _open_pattern_manager(self):
        """Open the Pattern Manager dialog."""
        from pattern_manager_dialog import PatternManagerDialog
        if not hasattr(self, '_pm_dialog') or self._pm_dialog is None:
            self._pm_dialog = PatternManagerDialog(parent=None)
            self._pm_dialog.pattern_action.connect(self._send_pm_command)
        # Request fresh data from Logic process
        self.commands_to_logic_queue.put_nowait({"action": "pm_get_patterns"})
        self._pm_dialog.show()
        self._pm_dialog.raise_()
        self._pm_dialog.activateWindow()

    def _send_pm_command(self, command: dict):
        """Forward Pattern Manager commands to Logic process."""
        self.commands_to_logic_queue.put_nowait(command)

    def _open_help_explainer(self):
        """Show the window that explains the Wheelhouse Assistant.

        One window only: a second Help while it is open raises the one
        already there. The window is modeless, so this method returns at
        once and the queue this call came from keeps being read.
        """
        from help_explainer_window import HelpExplainerWindow
        if getattr(self, '_help_explainer', None) is None:
            self._help_explainer = HelpExplainerWindow(parent=None)
            self._help_explainer.assistant_chosen.connect(
                self._on_help_explainer_choice
            )
        # The same window comes back, so a check box left ticked by an
        # earlier reading must not travel with this one.
        self._help_explainer.prepare_to_show()
        self._help_explainer.show()
        self._help_explainer.raise_()
        self._help_explainer.activateWindow()

    def _on_help_explainer_choice(self, do_not_show_again: bool):
        """Act on the window's Assistant button.

        The Logic process opens the browser, as it does for the menu entry;
        ``explained`` tells it the user has already read the explanation, so
        it does not ask for the window again. The check box writes the
        setting through the acknowledged settings path, in the Logic
        process, which is the only process that writes the settings file.
        Cancel and the Escape key never reach here, so backing out of the
        window changes nothing.
        """
        self.send_command({
            "action": "open_help_online",
            "explained": True,
            # Naming the window as the source keeps the Logic process log
            # honest: this run began at the Assistant button, not at the
            # menu entry or the spoken command that produced the window.
            "source": "window",
        })
        if do_not_show_again:
            self.send_command({
                'action': 'set_config_value',
                'key': 'ai.help.explain_before_open',
                'value': False,
            })

    def _open_calibration(self):
        """Open the voice-teaching (calibration) window (wh-7ou.7.3.1)."""
        from calibration_dialog import CalibrationDialog
        if not hasattr(self, '_cal_dialog') or self._cal_dialog is None:
            self._cal_dialog = CalibrationDialog(parent=None)
            self._cal_dialog.calibration_action.connect(self._send_cal_command)
        # Blank the window and re-arm its one-shot cancel; Logic answers
        # cal_session_open with the first cal_state to render.
        self._cal_dialog.reset_session()
        self.commands_to_logic_queue.put_nowait({"action": "cal_session_open"})
        self._cal_dialog.show()
        self._cal_dialog.raise_()
        self._cal_dialog.activateWindow()

    def _send_cal_command(self, command: dict):
        """Forward voice-teaching commands to Logic process."""
        self.commands_to_logic_queue.put_nowait(command)

    def _create_menu(self, is_tray_menu=True):
        # Resolve the dynamic state to static booleans once.
        is_ready = bool(self.initial_state_received)
        speech_is_checked = bool(self.speech_enabled)
        button_is_visible = bool(self.button_visible)
        interim_results_checked = bool(self.interim_results_enabled)
        debug_is_checked = bool(self.debug_mode)

        if is_tray_menu:
            # Build base menu items
            menu_items = [
                pystray.MenuItem(
                    "Toggle Speech",
                    self._on_tray_left_click,
                    default=True,
                    visible=False,
                ),
                pystray.MenuItem(
                    "Speech Enabled",
                    self.send_toggle_speech_command,
                    checked=lambda item: speech_is_checked,
                    enabled=is_ready
                ),
                pystray.MenuItem(
                    "Show Floating Button",
                    self._request_tray_visibility_toggle,
                    checked=lambda item: button_is_visible,
                    enabled=is_ready
                ),
                pystray.MenuItem(
                    "Interim Results",
                    self.toggle_interim_results,
                    checked=lambda item: interim_results_checked,
                    enabled=is_ready
                ),
                pystray.MenuItem(
                    "Push-to-Talk Mode",
                    self._toggle_ptt_mode,
                    checked=lambda item: self.speech_interaction_mode == "push_to_talk",
                    enabled=is_ready,
                ),
            ]

            # Add STT Provider submenu if providers available
            if self.stt_providers_available:
                current_provider = self.stt_provider
                provider_items = []
                for provider in self.stt_providers_available:
                    display_name = self._get_provider_display_name(provider)
                    # Use partial instead of lambda for picklability
                    callback = partial(self._on_provider_menu_click, provider)
                    checked_fn = partial(self._is_provider_checked, provider, current_provider)
                    provider_items.append(
                        pystray.MenuItem(
                            display_name,
                            callback,
                            checked=checked_fn,
                            enabled=is_ready
                        )
                    )
                # Voice teaching sits with the engine list, because it
                # teaches one engine (wh-voice-teaching-stt-submenu). It
                # marshals to the Qt thread via the state queue, like the
                # Pattern Manager (wh-7ou.7.3.1).
                provider_items.append(pystray.Menu.SEPARATOR)
                provider_items.append(
                    pystray.MenuItem(
                        "Teach WheelHouse your voice...",
                        lambda: self.state_from_logic_queue.put({"action": "open_calibration"}),
                        enabled=is_ready
                    )
                )
                menu_items.append(
                    pystray.MenuItem("STT Provider", pystray.Menu(*provider_items))
                )

            # Add AI Model submenu if models available
            if self.ai_providers_available:
                current_ai = self.ai_provider
                # configured-model-absent: the current selection is not in the
                # available list (e.g. config names a model the server no
                # longer lists). Annotate it so the user can see the mismatch.
                configured_absent = (
                    current_ai is not None
                    and current_ai not in self.ai_providers_available
                )
                ai_items = []
                for provider in self.ai_providers_available:
                    display_name = self._get_ai_provider_display_name(provider)
                    is_unconfigured = provider == "__ai_unconfigured__"
                    is_disabled = provider == "__ai_disabled__"
                    is_placeholder = is_unconfigured or is_disabled
                    if is_unconfigured:
                        label = "AI not configured"
                    elif is_disabled:
                        label = "AI disabled"
                    else:
                        label = display_name
                    callback = partial(self._on_ai_provider_menu_click, provider)
                    checked_fn = partial(self._is_ai_provider_checked, provider, current_ai)
                    ai_items.append(
                        pystray.MenuItem(
                            label,
                            callback,
                            checked=checked_fn,
                            # Sentinel placeholders are non-selectable.
                            enabled=is_ready and not is_placeholder,
                        )
                    )
                if configured_absent:
                    ai_items.append(
                        pystray.MenuItem(
                            f"(configured: {self._get_ai_provider_display_name(current_ai)} -- not available)",
                            lambda: None,
                            enabled=False,
                        )
                    )
                menu_items.append(
                    pystray.MenuItem("AI Model", pystray.Menu(*ai_items))
                )

            # Pattern Manager (marshal to Qt thread via state queue)
            menu_items.append(
                pystray.MenuItem(
                    "Pattern Manager",
                    lambda: self.state_from_logic_queue.put({"action": "open_pattern_manager"}),
                    enabled=is_ready
                )
            )

            # Add remaining items
            menu_items.extend([
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Debug",
                    lambda: self.send_command({'action': 'toggle_log_level'}),
                    checked=lambda item: debug_is_checked,
                    enabled=is_ready
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Help", self.request_help_online, enabled=is_ready),
                pystray.MenuItem("About Wheelhouse", self.show_about_dialog),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Restart Wheelhouse", self.request_restart, enabled=is_ready),
                pystray.MenuItem("Exit", self.exit_app, enabled=is_ready)
            ])
            
            return pystray.Menu(*menu_items)
        else:
            menu = QMenu()
            # A QMenu hides the tooltips of its actions unless this is on.
            menu.setToolTipsVisible(True)
            
            speech_action = QAction("Speech Enabled", self)
            speech_action.setCheckable(True)
            speech_action.setChecked(speech_is_checked)
            speech_action.setEnabled(is_ready)
            speech_action.setToolTip(
                "Switch listening on or off. The checkmark shows the current"
                " state."
            )
            speech_action.triggered.connect(self.send_toggle_speech_command)
            menu.addAction(speech_action)

            button_action = QAction("Show Floating Button", self)
            button_action.setCheckable(True)
            button_action.setChecked(button_is_visible)
            button_action.setEnabled(is_ready)
            button_action.setToolTip(
                "Show or hide the floating button. The checkmark shows whether"
                " it is visible."
            )
            button_action.triggered.connect(self.toggle_button_visibility)
            menu.addAction(button_action)

            interim_action = QAction("Interim Results", self)
            interim_action.setCheckable(True)
            interim_action.setChecked(interim_results_checked)
            interim_action.setEnabled(is_ready)
            interim_action.setToolTip(
                "Select whether Wheelhouse types words as the engine recognizes"
                " them, or holds them until the phrase ends."
            )
            interim_action.triggered.connect(self.toggle_interim_results)
            menu.addAction(interim_action)

            ptt_mode_action = QAction("Push-to-Talk Mode", self)
            ptt_mode_action.setCheckable(True)
            ptt_mode_action.setChecked(self.speech_interaction_mode == "push_to_talk")
            ptt_mode_action.setEnabled(is_ready)
            ptt_mode_action.setToolTip(
                "Switch between the two interaction modes. The checkmark shows"
                " when push-to-talk is active."
            )
            ptt_mode_action.triggered.connect(self._toggle_ptt_mode)
            menu.addAction(ptt_mode_action)

            # STT Provider submenu (only if providers available)
            if self.stt_providers_available:
                stt_submenu = QMenu("STT Provider", menu)
                stt_submenu.setToolTipsVisible(True)
                # The hover comment belongs on the action that OPENS the
                # submenu, because that is the action the parent menu lists.
                stt_submenu.menuAction().setToolTip(
                    "Select the speech engine. This menu lists only the engines"
                    " set up on this computer."
                )
                for provider in self.stt_providers_available:
                    display_name = self._get_provider_display_name(provider)
                    action = QAction(display_name, stt_submenu)
                    action.setCheckable(True)
                    action.setChecked(provider == self.stt_provider)
                    action.setEnabled(is_ready)
                    action.setToolTip(
                        "Switch the speech engine to this one. The change takes"
                        " effect at once."
                    )
                    # Capture provider value in lambda closure
                    action.triggered.connect(
                        lambda checked, p=provider: self.switch_stt_provider(p)
                    )
                    stt_submenu.addAction(action)
                # Voice teaching sits with the engine list, because it
                # teaches one engine (wh-voice-teaching-stt-submenu;
                # wh-7ou.7.3.1 for the handler).
                stt_submenu.addSeparator()
                cal_action = stt_submenu.addAction("Teach WheelHouse your voice...")
                cal_action.setEnabled(is_ready)
                cal_action.setToolTip(
                    "Open the voice-teaching session for the Distil-Whisper engine."
                )
                cal_action.triggered.connect(self._open_calibration)
                menu.addMenu(stt_submenu)

            # AI Model submenu (only if models available)
            if self.ai_providers_available:
                ai_submenu = QMenu("AI Model", menu)
                ai_submenu.setToolTipsVisible(True)
                ai_submenu.menuAction().setToolTip(
                    "Select the AI model. This menu lists the models the"
                    " settings file names and the server offers."
                )
                for provider in self.ai_providers_available:
                    is_unconfigured = provider == "__ai_unconfigured__"
                    is_disabled = provider == "__ai_disabled__"
                    is_placeholder = is_unconfigured or is_disabled
                    if is_unconfigured:
                        display_name = "AI not configured"
                        hover_comment = (
                            "The settings file names no AI server, so Wheelhouse"
                            " can offer no model."
                        )
                    elif is_disabled:
                        display_name = "AI disabled"
                        hover_comment = (
                            "The settings file switches the AI features off."
                        )
                    else:
                        display_name = self._get_ai_provider_display_name(provider)
                        hover_comment = "Use this model for the AI features."
                    action = QAction(display_name, ai_submenu)
                    action.setCheckable(True)
                    action.setChecked(provider == self.ai_provider)
                    # Sentinel placeholders are non-selectable.
                    action.setEnabled(is_ready and not is_placeholder)
                    action.setToolTip(hover_comment)
                    action.triggered.connect(
                        lambda checked, p=provider: self.switch_ai_provider(p)
                    )
                    ai_submenu.addAction(action)
                # configured-model-absent: current selection not in the list.
                if (
                    self.ai_provider is not None
                    and self.ai_provider not in self.ai_providers_available
                ):
                    absent = QAction(
                        f"(configured: {self._get_ai_provider_display_name(self.ai_provider)} -- not available)",
                        ai_submenu,
                    )
                    absent.setEnabled(False)
                    absent.setToolTip(
                        "The settings file names this model. It is not among the"
                        " models on offer."
                    )
                    ai_submenu.addAction(absent)
                menu.addMenu(ai_submenu)

            # Pattern Manager
            pm_action = menu.addAction("Pattern Manager")
            pm_action.setEnabled(is_ready)
            pm_action.setToolTip("Open the editor for personal voice patterns.")
            pm_action.triggered.connect(self._open_pattern_manager)

            menu.addSeparator()

            debug_action = QAction("Debug", self)
            debug_action.setCheckable(True)
            debug_action.setChecked(debug_is_checked)
            debug_action.setEnabled(is_ready)
            debug_action.setToolTip(
                "Switch detailed logging on or off. Leave it off except when"
                " diagnosing or reporting a problem. The checkmark shows the"
                " current state."
            )
            debug_action.triggered.connect(lambda: self.send_command({'action': 'toggle_log_level'}))
            menu.addAction(debug_action)

            menu.addSeparator()

            help_action = QAction("Help", self)
            help_action.setEnabled(is_ready)
            help_action.setToolTip(
                'Open the Wheelhouse Assistant in the browser. The spoken'
                ' command "x-ray help" opens the same page.'
            )
            help_action.triggered.connect(self.request_help_online)
            menu.addAction(help_action)

            # No is_ready check. This one needs nothing from the Logic
            # process, so it works even when the rest of the menu cannot.
            about_action = QAction("About Wheelhouse", self)
            about_action.setToolTip(
                "Show the program name and the running version. Include the"
                " version in any problem report."
            )
            about_action.triggered.connect(self.show_about_dialog)
            menu.addAction(about_action)

            menu.addSeparator()

            restart_action = QAction("Restart Wheelhouse", self)
            restart_action.setEnabled(is_ready)
            restart_action.setToolTip(
                "Restart the whole program. This is the first step when speech"
                " recognition stops responding."
            )
            restart_action.triggered.connect(self.request_restart)
            menu.addAction(restart_action)

            exit_action = QAction("Exit", self)
            exit_action.setEnabled(is_ready)
            exit_action.setToolTip(
                "Close Wheelhouse. Do this before running the installer to"
                " update, and before uninstalling."
            )
            exit_action.triggered.connect(self.exit_app)
            menu.addAction(exit_action)
            
            return menu

    def update_tray_menu(self):
        """:flow: GUI State Synchronization
        :step: 9
        :description: Rebuilds system tray icon and menu to reflect current state
        :data_in: Current speech_enabled, button_visible state values
        :data_out: Updated system tray icon color and menu items
        :notes: Final visual update step. Rebuilds tray menu via _create_menu(is_tray_menu=True) to update checkmarks on state-dependent items. The icon picture is NOT touched here: it is the Wheelhouse icon, set once when the tray is created, and nothing changes it in response to anything.
        """
        self.icon.menu = self._create_menu(is_tray_menu=True)

    def request_help_online(self):
        """Ask the Logic process to open the Wheelhouse help page.

        The address is the ai.help gem_url setting, and settings live in the
        Logic process -- the GUI process has no copy of it. This is the same
        setting, read the same way, as the spoken command "wheelhouse help
        online", so changing it in one place changes both.
        """
        self.send_command({"action": "open_help_online"})

    def show_about_dialog(self):
        """Show the program name, version, and where to get help."""
        version = get_app_version()
        QMessageBox.information(
            None,
            "About Wheelhouse",
            (
                "Wheelhouse\n"
                f"Version {version}\n\n"
                "Voice-controlled desktop automation for Windows.\n\n"
                "Choose Help from this menu, or say \"open voice access help\", "
                "to open the Wheelhouse Assistant in your browser."
            ),
        )

    def exit_app(self):
        """Set shutdown event to trigger graceful application exit."""
        self.shutdown_event.set()

    def _shutdown_gui(self):
        self._ptt_ack_timer.stop()
        self.queue_timer.stop()
        self.icon.stop()
        app = QApplication.instance()
        if app:
            app.quit()
        logger.info("GUI shutdown sequence complete.")


def configure_gui_application(app) -> None:
    """Keep the GUI process running while nothing of its is on screen.

    Wheelhouse lives in the notification area, and every window it opens --
    the About box, the Pattern Manager, a notice -- is temporary. Qt's
    default is to end the program when the last window that counts closes,
    and the floating button does not count: it is a tool window, which Qt
    leaves out of that decision, and the user can turn it off entirely
    anyway. Closing the About box was therefore ending the GUI process, and
    the launcher, seeing a child gone, shut the rest of Wheelhouse down with
    it (wh-gui-about-box-quits-app). This process leaves through its
    shutdown event and nothing else.
    """
    app.setQuitOnLastWindowClosed(False)


def gui_process_target(shutdown_event: Event, commands_to_logic_queue: Queue, state_to_gui_queue: Queue, gui_shm_name: str = None):
    """
    :flow: GUI Process Initialization
    :step: 1
    :produces_for: GUI State Synchronization
    :description: Entry point for the GUI process - sets up logging and launches the Qt application.
    :data_in: shutdown_event (multiprocessing.Event), commands_to_logic_queue (Queue), state_to_gui_queue (Queue), gui_shm_name (str, optional).
    :data_out: Spawns QApplication and GuiManager, runs Qt event loop until shutdown.
    :notes: This is the target function for multiprocessing.Process creation. Runs in a separate process
        with its own Python interpreter. Sets up process-local logging configuration before starting the
        Qt GUI manager. The function blocks on app.exec() until the application exits.
    """
    from services.wheelhouse.config_service import ConfigService
    from utils.logging_setup import setup_logging
    from utils.process_priority import elevate_process_priority

    # Use consistent logging setup across all processes
    config_service = ConfigService()
    config = config_service.get_config()
    setup_logging(config)

    # High process class keeps the overlay painting under a saturated CPU;
    # the Below Normal class a Task Scheduler launch hands down starves it
    # (wh-process-priority-durable). After setup_logging so a refusal's
    # warning reaches the process log instead of bare stderr
    # (wh-process-priority-durable.1.4).
    elevate_process_priority()
    
    logger.info("GUI process started.")
    try:
        app = QApplication(sys.argv)
        configure_gui_application(app)
        manager = GuiManager(shutdown_event, commands_to_logic_queue, state_to_gui_queue, gui_shm_name, config=config)
        manager.start()
        exit_code = app.exec()
        if not shutdown_event.is_set():
            # Nothing asked this process to stop, so the Qt loop ended on its
            # own -- and it ends quietly, with a success code and no traceback.
            # The launcher will take the rest of Wheelhouse down a moment from
            # now, so say here why, while the reason is still known.
            logger.error(
                "Qt event loop ended (code %s) with no shutdown requested. "
                "Wheelhouse will stop.",
                exit_code,
            )
        sys.exit(exit_code)
    except Exception as e:
        logger.critical(f"Unhandled exception in GUI process: {e}", exc_info=True)
    finally:
        logger.info("GUI process finished.")
