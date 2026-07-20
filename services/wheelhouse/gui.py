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
import multiprocessing
from multiprocessing import Queue, shared_memory
from multiprocessing.synchronize import Event
import logging

from utils.redact import redact_transcript
import sys
import struct
import json
import math
from pathlib import Path
from queue import Empty, Full
import threading
from functools import partial

# --- Qt and PySide6 Imports ---
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QDialog, QLabel, QVBoxLayout, QFrame
from PySide6.QtCore import Qt, QTimer, QPoint, Signal, QObject
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QAction, QFont, QPixmap

# --- System Tray Imports ---
import pystray
from PIL import Image, ImageDraw

# --- Notification Imports ---
from plyer import notification

logger = logging.getLogger(__name__)


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


def create_icon_image(color_tuple):
    """
    :flow: GUI State Synchronization
    :step: 1
    :description: Creates system tray icon with dynamic color state.
    :data_in: RGB color tuple from StateManager indicating system state.
    :data_out: PIL Image object for QSystemTrayIcon rendering.
    :produces_for: GUI State Synchronization
    :notes: Generates a circular icon indicating WheelHouse operational state:
    Green (normal operation), Red (speech suppressed), Yellow (transitional states).
    Used by GUI process to provide visual feedback of system state via system tray.
    """
    width = 64
    height = 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, width - 8, height - 8), fill=color_tuple)
    return image


# wh-n29v.118 / wh-n29v.120.1: the numbered-overlay "walking" cue self-clear
# fallback timer must outlast the ACTUAL Logic-side success-path latency. On the
# success path the cue is cleared by the GUI when paint_overlay arrives, and
# paint_overlay is enqueued only AFTER both the walk send_request AND the
# PIN_SNAPSHOT ack await complete -- each bounded by
# click_config.response_timeout_ms (default 3000; validated only by
# _is_int_at_least(100) -- NO upper bound). So the bound the cue must survive is
# walk + pin = 2 * response_timeout_ms, not the walk alone. Logic carries that
# combined value in the overlay_walk_cue payload (walk_timeout_ms = 2 *
# response_timeout_ms), so an operator who raises response_timeout_ms does not
# get the cue cleared before the numbers paint. When the field is absent or not
# a usable int, the cue falls back to this default.
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


class FloatingButton(QWidget):
    left_clicked = Signal()
    press_started = Signal()
    press_ended = Signal()
    drag_started = Signal()
    double_clicked = Signal()
    closed = Signal()
    size_changed = Signal(int)
    moved = Signal(QPoint)
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
        self._is_dragging = False
        self._drag_position = QPoint(0, 0)
        self._initial_press_pos = None
        
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
        # interval is armed in set_walk_cue from the effective Logic walk bound
        # (response_timeout_ms, carried in the payload) plus
        # _WALK_CUE_FALLBACK_BUFFER_MS -- NOT a hardcoded value -- so a raised
        # response_timeout_ms does not clear the cue mid-walk (see the module
        # constants above for the full rationale).
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

    def set_size(self, diameter: int):
        """Resize button to specified diameter (preserves circular shape)."""
        self.setFixedSize(diameter, diameter)
        self.update()

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

        if self._is_indeterminate:
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

        wh-n29v.118: ``walk_timeout_ms`` is the effective Logic-side walk bound
        (``response_timeout_ms``) carried in the payload. The fallback timer is
        armed at that value plus ``_WALK_CUE_FALLBACK_BUFFER_MS`` so it always
        outlasts the real walk -- even when an operator raises
        ``response_timeout_ms`` above the GUI default. When the value is absent
        or not a usable int (``bool`` is rejected even though it subclasses
        ``int``), it degrades to ``_WALK_CUE_DEFAULT_WALK_MS``.
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
            # range (response_timeout_ms has no upper bound), and arm the timer
            # BEFORE marking the cue active so we can fail closed: if the start
            # still raises for any reason, leave the cue inactive rather than
            # stranding the dot on screen with no fallback timer to clear it.
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
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_dragging = False
            self._initial_press_pos = event.globalPosition().toPoint()
            self.press_started.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move for button dragging.
        
        Args:
            event: Qt mouse event
        """
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
        if event.button() == Qt.MouseButton.LeftButton and self._initial_press_pos:
            if not self._is_dragging:
                self.press_ended.emit()
            else:
                self.moved.emit(self.pos())
            self._is_dragging = False
            self._initial_press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Handle double-click to emit double_clicked signal."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        # Don't call super -- prevent Qt from firing another press

    def closeEvent(self, event):
        """Handle window close to emit closed signal.
        
        Args:
            event: Qt close event
        """
        self.closed.emit()
        event.accept()

    def wheelEvent(self, event):
        """Handle mouse wheel with Ctrl modifier to resize button.
        
        Args:
            event: Qt wheel event
        """
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            new_size = self.width() + (delta / 12)
            new_size = int(max(30, min(new_size, 150)))
            self.set_size(new_size)
            self.size_changed.emit(new_size)
        else:
            super().wheelEvent(event)
    
    def contextMenuEvent(self, event):
        """Handle right-click to show context menu.
        
        Args:
            event: Qt context menu event
        """
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

    def show_working(self, message: str) -> None:
        """Show the dialog with a message and start dot animation.

        If already visible, updates the message text.

        Args:
            message: The status message to display (dots are appended automatically).
        """
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

    def hide_working(self) -> None:
        """Hide the dialog and stop dot animation."""
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
        self._speech_before_hold = False  # Saved speech state for drag cancel

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
            )
        except Exception:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "Failed to construct OverlayPaintWindowManager; the "
                "numbered overlay will be unavailable this session.",
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
                self._working_badge_overlay = OverlayPaintWindowManager(
                    badge_font_pt=_click_config.overlay_badge_font_pt,
                    badge_shadow=_click_config.overlay_badge_shadow,
                    badge_corner=_click_config.overlay_badge_corner,
                    badge_trailing_space=_click_config.overlay_badge_trailing_space,
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

        self.icon = pystray.Icon("wheelhouse", title="Wheelhouse")

        self.button.press_started.connect(self._on_button_press)
        self.button.press_ended.connect(self._on_button_release)
        self.button.double_clicked.connect(self._on_double_click)
        self.button.drag_started.connect(self._on_drag_started)
        self.button.closed.connect(self.hide_button)
        self.button.size_changed.connect(self.send_size_change_command)
        self.button.moved.connect(self.send_pos_change_command)
        self.button.context_menu_requested.connect(self.show_context_menu)

        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._check_queues_and_events)
        self.icon_thread = threading.Thread(target=self.icon.run, daemon=True)

        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._on_hold_threshold)
        self._PTT_HOLD_THRESHOLD_MS = 200

        self._double_click_timer = QTimer(self)
        self._double_click_timer.setSingleShot(True)
        self._double_click_timer.timeout.connect(self._on_deferred_single_click)
        self._DOUBLE_CLICK_WAIT_MS = 350
        self._double_click_consumed = False

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
        self.working_dialog.show_working("Starting")

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
                    self.button.set_size(message.get('FLOATING_BUTTON_SIZE', 50))
                    pos = message.get('FLOATING_BUTTON_POS', [100, 100])
                    self.button.move(QPoint(*pos))

                    if was_initial:
                        self.button.set_indeterminate(False)

                    self.update_ui_state()
                elif action == "show_working":
                    self.working_dialog.show_working(message.get("message", "Working"))
                elif action == "hide_working":
                    self.working_dialog.hide_working()
                elif action == "show_notification":
                    if notification.notify:
                        notification.notify(
                            title=message.get("title", "Wheelhouse"),
                            message=message.get("message", ""),
                            timeout=message.get("timeout", 5)
                        )
                    else:
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
                    # wh-n29v.118: thread the effective Logic walk bound
                    # (walk_timeout_ms = response_timeout_ms) through to
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
            if notification.notify:
                notification.notify(
                    title="Wheelhouse",
                    message=text,
                    timeout=8,
                )
            else:
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
            self._emit_overlay_state_changed(result)
        except Exception as exc:  # noqa: BLE001 - overlay is non-critical
            logger.warning(
                "clear_overlay handling failed: %s", exc, exc_info=True,
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
        self.button.set_state(self.speech_enabled)
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
        try:
            self.commands_to_logic_queue.put_nowait(command)
        except Full:
            logger.warning("Logic process queue full, command dropped: %s", command.get('action'))

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
        self.send_command({'action': 'set_config_value', 'key': 'FLOATING_BUTTON_POS', 'value': [new_pos.x(), new_pos.y()]})
        
    def toggle_button_visibility(self):
        """Send command to toggle floating button visibility."""
        self.send_command({'action': 'toggle_button_visibility'})

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
        self._speech_before_hold = self.speech_enabled  # Save for drag cancel
        self._press_timer.start(self._PTT_HOLD_THRESHOLD_MS)

    def _on_button_release(self):
        """Handle floating button mouse-up."""
        if not self.initial_state_received:
            return
        # Ignore the second release after a double-click was consumed
        if self._double_click_consumed:
            self._double_click_consumed = False
            return
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
        """Hold timer expired -- activate PTT (unless drag in progress)."""
        if self.button._is_dragging:
            return
        self._ptt_held = True
        self._start_ptt()

    def _start_ptt(self, source: str = "floating_button"):
        """Send ptt_start command to Logic process."""
        self._ptt_held = True
        self.send_command({"action": "ptt_start", "source": source})
        # Optimistic UI update -- show active state immediately
        # (Logic process will confirm via state_update later)
        self.speech_enabled = True
        self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")
        self.button.set_state(True)
        self.update_tray_menu()

    def _stop_ptt(self):
        """Send ptt_stop command to Logic process."""
        self._ptt_held = False
        self.send_command({"action": "ptt_stop"})
        # Optimistic UI update -- show inactive state immediately
        self.speech_enabled = False
        self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")
        self.button.set_state(False)
        self.update_tray_menu()

    def _on_double_click(self):
        """Handle double-click -- toggle between PTT and toggle interaction modes."""
        if not self.initial_state_received:
            return
        self._double_click_timer.stop()
        self._double_click_consumed = True
        # If PTT was activated by the first click, cancel it
        if self._ptt_held:
            self._stop_ptt()
        self._toggle_ptt_mode()

    def _on_deferred_single_click(self):
        """Double-click timer expired -- execute the deferred single-click toggle."""
        self.send_toggle_speech_command()

    def _on_drag_started(self):
        """Handle drag start -- cancel any pending PTT activation."""
        self._press_timer.stop()
        self._double_click_timer.stop()
        if self._ptt_held:
            # Hold timer activated PTT before drag was detected -- cancel PTT
            # and restore the pre-hold speech state (don't change what the user had)
            self._ptt_held = False
            self.send_command({"action": "ptt_stop", "reason": "drag_cancel"})
            self.speech_enabled = self._speech_before_hold
            self.button.set_state(self._speech_before_hold)
            self.button.set_ptt_mode(self.speech_interaction_mode == "push_to_talk")
            self.update_tray_menu()

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
            "azure": "Azure Speech",
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
            if notification.notify:
                notification.notify(title="Wheelhouse", message="Restarting application...")
        except Exception as e:
            logger.error(f"Failed to send restart command: {e}")
            if notification.notify:
                notification.notify(title="Wheelhouse", message="Could not restart: Logic process is unresponsive.")

    def request_stt_restart(self):
        """
        :flow: STT Restart Request
        :step: 1
        :produces_for: WheelHouse Logic Process
        :description: Sends hard restart command to logic process when user clicks
            "Restart Transcription Service" in the system tray menu.
            Shows working dialog to indicate the operation is in progress.
        :data_in: User menu click event
        :data_out: {action: 'hard_restart_stt_service'} message to commands_queue
        """
        try:
            self.commands_to_logic_queue.put_nowait({'action': 'hard_restart_stt_service'})
            self.working_dialog.show_working("Restarting speech recognition")
        except Exception as e:
            logger.error(f"Failed to send STT restart command: {e}")
            if notification.notify:
                notification.notify(title="Wheelhouse", message="Could not restart STT: Logic process is unresponsive.")

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

    def _on_te_cancelled(self):
        """Forward cancel to Logic Process."""
        self.commands_to_logic_queue.put_nowait({"action": "te_cancelled"})

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
                if notification.notify:
                    notification.notify(
                        title="Terminal paste failed",
                        message="Command not submitted.",
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
                    self.toggle_button_visibility,
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
                pystray.MenuItem("Restart Transcription Service", self.request_stt_restart, enabled=is_ready),
                pystray.MenuItem("Restart Wheelhouse", self.request_restart, enabled=is_ready),
                pystray.MenuItem("Exit", self.exit_app, enabled=is_ready)
            ])
            
            return pystray.Menu(*menu_items)
        else:
            menu = QMenu()
            
            speech_action = QAction("Speech Enabled", self)
            speech_action.setCheckable(True)
            speech_action.setChecked(speech_is_checked)
            speech_action.setEnabled(is_ready)
            speech_action.triggered.connect(self.send_toggle_speech_command)
            menu.addAction(speech_action)

            button_action = QAction("Show Floating Button", self)
            button_action.setCheckable(True)
            button_action.setChecked(button_is_visible)
            button_action.setEnabled(is_ready)
            button_action.triggered.connect(self.toggle_button_visibility)
            menu.addAction(button_action)

            interim_action = QAction("Interim Results", self)
            interim_action.setCheckable(True)
            interim_action.setChecked(interim_results_checked)
            interim_action.setEnabled(is_ready)
            interim_action.triggered.connect(self.toggle_interim_results)
            menu.addAction(interim_action)

            ptt_mode_action = QAction("Push-to-Talk Mode", self)
            ptt_mode_action.setCheckable(True)
            ptt_mode_action.setChecked(self.speech_interaction_mode == "push_to_talk")
            ptt_mode_action.setEnabled(is_ready)
            ptt_mode_action.triggered.connect(self._toggle_ptt_mode)
            menu.addAction(ptt_mode_action)

            # STT Provider submenu (only if providers available)
            if self.stt_providers_available:
                stt_submenu = QMenu("STT Provider", menu)
                for provider in self.stt_providers_available:
                    display_name = self._get_provider_display_name(provider)
                    action = QAction(display_name, stt_submenu)
                    action.setCheckable(True)
                    action.setChecked(provider == self.stt_provider)
                    action.setEnabled(is_ready)
                    # Capture provider value in lambda closure
                    action.triggered.connect(
                        lambda checked, p=provider: self.switch_stt_provider(p)
                    )
                    stt_submenu.addAction(action)
                menu.addMenu(stt_submenu)

            # AI Model submenu (only if models available)
            if self.ai_providers_available:
                ai_submenu = QMenu("AI Model", menu)
                for provider in self.ai_providers_available:
                    is_unconfigured = provider == "__ai_unconfigured__"
                    is_disabled = provider == "__ai_disabled__"
                    is_placeholder = is_unconfigured or is_disabled
                    if is_unconfigured:
                        display_name = "AI not configured"
                    elif is_disabled:
                        display_name = "AI disabled"
                    else:
                        display_name = self._get_ai_provider_display_name(provider)
                    action = QAction(display_name, ai_submenu)
                    action.setCheckable(True)
                    action.setChecked(provider == self.ai_provider)
                    # Sentinel placeholders are non-selectable.
                    action.setEnabled(is_ready and not is_placeholder)
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
                    ai_submenu.addAction(absent)
                menu.addMenu(ai_submenu)

            # Pattern Manager
            pm_action = menu.addAction("Pattern Manager")
            pm_action.setEnabled(is_ready)
            pm_action.triggered.connect(self._open_pattern_manager)

            menu.addSeparator()

            debug_action = QAction("Debug", self)
            debug_action.setCheckable(True)
            debug_action.setChecked(debug_is_checked)
            debug_action.setEnabled(is_ready)
            debug_action.triggered.connect(lambda: self.send_command({'action': 'toggle_log_level'}))
            menu.addAction(debug_action)

            menu.addSeparator()

            restart_stt_action = QAction("Restart Transcription Service", self)
            restart_stt_action.setEnabled(is_ready)
            restart_stt_action.triggered.connect(self.request_stt_restart)
            menu.addAction(restart_stt_action)

            restart_action = QAction("Restart Wheelhouse", self)
            restart_action.setEnabled(is_ready)
            restart_action.triggered.connect(self.request_restart)
            menu.addAction(restart_action)

            exit_action = QAction("Exit", self)
            exit_action.setEnabled(is_ready)
            exit_action.triggered.connect(self.exit_app)
            menu.addAction(exit_action)
            
            return menu

    def update_tray_menu(self):
        """:flow: GUI State Synchronization
        :step: 9
        :description: Rebuilds system tray icon and menu to reflect current state
        :data_in: Current speech_enabled, button_visible state values
        :data_out: Updated system tray icon color and menu items
        :notes: Final visual update step. Rebuilds tray menu via _create_menu(is_tray_menu=True) to update checkmarks on state-dependent items. Updates tray icon color based on three states: indeterminate (gray, 100,100,100) before initial state received, enabled (red, 200,0,0) when speech active, disabled (gray, 160,160,160) when speech inactive. This provides persistent visual feedback even when floating button is hidden.
        """
        self.icon.menu = self._create_menu(is_tray_menu=True)
        color_map = {
            "indeterminate": (100, 100, 100),
            "enabled": (200, 0, 0),
            "ptt_idle": (50, 120, 200),
            "disabled": (160, 160, 160),
        }
        if not self.initial_state_received:
            state = "indeterminate"
        elif self.speech_enabled:
            state = "enabled"
        elif self.speech_interaction_mode == "push_to_talk":
            state = "ptt_idle"
        else:
            state = "disabled"
        self.icon.icon = create_icon_image(color_map[state])

    def exit_app(self):
        """Set shutdown event to trigger graceful application exit."""
        self.shutdown_event.set()

    def _shutdown_gui(self):
        self.queue_timer.stop()
        self.icon.stop()
        app = QApplication.instance()
        if app:
            app.quit()
        logger.info("GUI shutdown sequence complete.")


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
    
    # Use consistent logging setup across all processes
    config_service = ConfigService()
    config = config_service.get_config()
    setup_logging(config)
    
    logger.info("GUI process started.")
    try:
        app = QApplication(sys.argv)
        manager = GuiManager(shutdown_event, commands_to_logic_queue, state_to_gui_queue, gui_shm_name, config=config)
        manager.start()
        sys.exit(app.exec())
    except Exception as e:
        logger.critical(f"Unhandled exception in GUI process: {e}", exc_info=True)
    finally:
        logger.info("GUI process finished.")