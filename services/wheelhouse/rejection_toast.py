"""PySide6 advisory toast for text-target rejections (wh-lzsbd).

Renders the branded wording from :mod:`rejection_toast_wording` as a
borderless dialog anchored near the screen's bottom-right corner. The
widget owns no business logic -- it accepts a fully-composed
:class:`ToastWording` plus the raw rejection fields used by the
"Show details" panel.

Phase 2 buttons: "Show details" (one-way expansion -- the button
disappears after first click) and the dismiss X.

Phase 4 buttons (wh-z7qx1): adds "Try it anyway", visible only when
:func:`should_show_try_anyway` allows it for the supplied
:class:`ToastWording.category`. Click emits the
:attr:`try_anyway_clicked` Qt signal; the GUI manager attaches the
correlation token and forwards via IPC under wh-iycks.

Auto-dismiss: the widget honours a configurable lifetime. The default
matches wh-zib65's first-rejection-per-key dwell (8 seconds); the GUI
manager is the right place to switch to the 4-second repeat dwell when
Phase 2's rate-limit map is in place.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rejection_help_window import RejectionHelpWindow
from rejection_toast_wording import ToastWording, should_show_try_anyway

# WheelHouse plaque identity. Imported by rejection_help_window so the
# two widgets stay in lockstep if the plaque colour ever changes
# (wh-b9kpc local review finding 7).
WHEELHOUSE_PLAQUE_BORDER = "rgb(100, 80, 40)"


class RejectionToast(QDialog):
    """Borderless advisory toast with action buttons and a details panel.

    The widget exposes one Qt signal:

      * ``try_anyway_clicked`` -- emitted when the user clicks the
        Try-it-anyway button. The signal carries no payload; the GUI
        manager (the only listener) attaches the correlation token of
        the rejection that produced the toast (wh-iycks). Wiring is
        deliberately split across beads: the widget knows nothing
        about correlation tokens, and the manager does not need to
        peer into the dialog to learn that a click happened.
    """

    DEFAULT_LIFETIME_MS = 8000

    # Phase 4 (wh-z7qx1). wh-iycks attaches a correlation token in
    # the GUI manager; the widget itself is intentionally token-agnostic.
    try_anyway_clicked = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # wh-b9kpc: the Why-am-I-seeing-this help window is created
        # lazily on the first click and reused for subsequent clicks
        # so the user can leave it open across multiple rejections.
        self._help_window: Optional[RejectionHelpWindow] = None

        self._build_ui()

        self._lifetime_timer = QTimer(self)
        self._lifetime_timer.setSingleShot(True)
        self._lifetime_timer.timeout.connect(self.close)

    def _build_ui(self) -> None:
        # Outer frame -- background and text colors come from the Qt
        # palette so the toast tracks the Windows light/dark theme.
        # The brown border is the WheelHouse plaque identity and stays
        # constant across themes.
        outer = QFrame(self)
        outer.setObjectName("toast_outer")
        outer.setStyleSheet(
            "QFrame#toast_outer { "
            "background-color: palette(window); "
            f"border: 4px solid {WHEELHOUSE_PLAQUE_BORDER}; "
            "}"
        )

        title_label = QLabel("")
        title_label.setObjectName("toast_title")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        title_label.setWordWrap(True)
        self._title_label = title_label

        dismiss = QPushButton("X")
        dismiss.setFixedSize(24, 24)
        dismiss.setStyleSheet(
            "QPushButton { border: none; background: transparent; "
            "color: palette(windowText); font-weight: bold; } "
            "QPushButton:hover { color: rgb(160, 40, 40); }"
        )
        dismiss.clicked.connect(self.close)
        # wh-vbvgf.14.3 (codex review): expose the dismiss button so
        # widget tests can exercise the X-click path without
        # findChild gymnastics. Mirrors GrantPromptToast.
        self._dismiss_button = dismiss

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title_label, stretch=1)
        title_row.addWidget(dismiss, stretch=0, alignment=Qt.AlignmentFlag.AlignTop)

        body_label = QLabel("")
        body_label.setObjectName("toast_body")
        body_label.setFont(QFont("Segoe UI", 10))
        body_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        body_label.setWordWrap(True)
        body_label.setMinimumWidth(360)
        self._body_label = body_label

        self._details_label = QLabel("")
        self._details_label.setFont(QFont("Consolas", 9))
        self._details_label.setStyleSheet(
            "color: palette(windowText); background: transparent; "
            "padding-top: 8px;"
        )
        self._details_label.setWordWrap(True)
        self._details_label.setVisible(False)

        # wh-z7qx1: "Try it anyway" is the leftmost button so it sits
        # closest to the body wording. Visibility is governed by the
        # wording category and is set in :meth:`show_rejection`.
        self._try_anyway_button = QPushButton("Try it anyway")
        self._try_anyway_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 8px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._try_anyway_button.clicked.connect(self._on_try_anyway_clicked)
        self._try_anyway_button.setVisible(False)

        # wh-b9kpc: Why-am-I-seeing-this button. Visible only when the
        # Try-it-anyway button is visible -- the help text describes
        # the Try-it-anyway flow, so showing it on the silenced
        # categories would invite the user to look for a button that
        # is not there. After wh-1r2b3 the silenced categories do not
        # surface the notice at all in production; the visibility
        # binding is defensive for any future change.
        self._why_button = QPushButton("Why am I seeing this?")
        self._why_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 8px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._why_button.clicked.connect(self._on_why_clicked)
        self._why_button.setVisible(False)

        self._details_button = QPushButton("Show details")
        self._details_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 8px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._details_button.clicked.connect(self._reveal_details)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(self._try_anyway_button)
        button_row.addWidget(self._why_button)
        button_row.addWidget(self._details_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addLayout(title_row)
        inner.addWidget(body_label)
        inner.addLayout(button_row)
        inner.addWidget(self._details_label)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

    def _reveal_details(self) -> None:
        """One-way reveal of the details panel (wh-z7qx1).

        Spec calls for the Show-details button to disappear after
        click rather than toggle to a Hide-details affordance. A user
        who wants to dismiss the details panel can dismiss the whole
        toast.
        """

        self._details_label.setVisible(True)
        self._details_button.setVisible(False)
        self.adjustSize()

    def _on_try_anyway_clicked(self) -> None:
        """Disable the button before re-emitting (wh-vbvgf.2.2).

        Without this, a fast double-click delivers two clicked signals
        to the dialog, each running the full retry pipeline. The
        button is re-enabled at the start of every show_rejection so
        the next toast presentation has a fresh, ready button.
        """

        self._try_anyway_button.setEnabled(False)
        self.try_anyway_clicked.emit()

    def _on_why_clicked(self) -> None:
        """Open (or raise) the Why-am-I-seeing-this help window (wh-b9kpc).

        The help window is created lazily on first click and reused for
        subsequent clicks. Reusing the instance keeps any window-position
        the user picked, and avoids piling multiple help windows on
        screen if the user clicks Why on several rejections in a row.

        The window is parented to the toast so Qt destroys it cleanly
        when the toast is destroyed. The toast lives the lifetime of
        the GUI process today, but parenting protects against future
        per-toast recreation paths (wh-b9kpc local review finding 1).

        Before each show, the stored geometry is sanity-checked against
        the current screen layout. A user who dragged the window to a
        secondary monitor, closed it, and then unplugged that monitor
        would otherwise see the next click re-show the window at
        coordinates that no longer correspond to a connected screen
        (wh-b9kpc local review findings 2 and 3).

        The window is NOT activated. WheelHouse users include
        voice-only users; stealing keyboard focus would route the next
        utterance to whichever app Windows picks after the user
        closes the help window, instead of back to the editor they
        were dictating into (wh-b9kpc local review finding 9).
        """

        window = self._help_window
        if window is None:
            window = RejectionHelpWindow(self)
            self._help_window = window
        self._ensure_help_window_on_screen(window)
        window.show()
        window.raise_()

    @staticmethod
    def _clamp_frame_to_available(
        frame_x: int,
        frame_y: int,
        frame_width: int,
        frame_height: int,
        avail_x: int,
        avail_y: int,
        avail_width: int,
        avail_height: int,
    ) -> tuple[int, int]:
        """Return the clamped (x, y) for a window frame within available.

        Pure function so the clamp can be tested without involving the
        OS window manager (which on Windows auto-clamps via the
        platform plugin and hides the bug this helper protects against
        on high-DPI displays where Qt does not pre-clamp).

        If the frame is larger than available in either dimension, the
        clamp pins it to the top-left of available; the help window's
        body is wrapped in a scroll area so the Close button below the
        scroll area stays reachable even when the body is taller than
        the screen (wh-b9kpc.1.1, codex review).
        """

        max_x = avail_x + max(0, avail_width - frame_width)
        max_y = avail_y + max(0, avail_height - frame_height)
        new_frame_x = max(avail_x, min(frame_x, max_x))
        new_frame_y = max(avail_y, min(frame_y, max_y))
        return new_frame_x, new_frame_y

    @staticmethod
    def _ensure_help_window_on_screen(window: RejectionHelpWindow) -> None:
        """Clamp the help window's frame inside the available screen.

        wh-b9kpc.1.1 (codex review): the earlier version only checked
        whether the frame's center was on a screen. A frame whose
        center was on-screen but whose bottom edge extended past the
        screen's available geometry (common on high-DPI or
        accessibility-scaled displays where Qt does not pre-clamp)
        passed the check unmoved and the Close button at the bottom
        rendered off-screen.

        The clamping math lives in _clamp_frame_to_available so it
        can be tested without involving the OS window manager. This
        wrapper picks a target screen by the frame center (falling
        back to the primary screen if the center is off all screens)
        and applies the clamp result via window.move().
        """

        frame = window.frameGeometry()
        screen = QApplication.screenAt(frame.center())
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()

        new_frame_x, new_frame_y = RejectionToast._clamp_frame_to_available(
            frame.x(), frame.y(), frame.width(), frame.height(),
            avail.x(), avail.y(), avail.width(), avail.height(),
        )

        if new_frame_x == frame.x() and new_frame_y == frame.y():
            return
        # move() positions the inner geometry, not the frame. Apply
        # the same delta so the frame ends up at the clamped point.
        inner = window.geometry()
        dx = new_frame_x - frame.x()
        dy = new_frame_y - frame.y()
        window.move(inner.x() + dx, inner.y() + dy)

    def show_rejection(
        self,
        wording: ToastWording,
        details: Sequence[str],
        *,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
    ) -> None:
        """Render the rejection and show the toast.

        Args:
            wording: composed title + body + category (the GUI manager
                runs :func:`compose_rejection_wording` and passes the
                result here). The category drives Try-it-anyway button
                visibility per :func:`should_show_try_anyway`.
            details: lines for the "Show details" panel. The panel
                stays hidden until the user clicks the button.
            lifetime_ms: auto-dismiss timeout. The GUI manager picks
                between the first-time and repeat values from wh-zib65;
                the widget itself only honours the supplied number.
        """

        self._title_label.setText(wording.title)
        self._body_label.setText(wording.body)
        self._details_label.setText("\n".join(details))

        # Reset to the collapsed-with-button initial state on every
        # render. The widget is reused across rejection events
        # (the GUI manager keeps a single instance), so a previous
        # show that revealed details or hid the Try-it-anyway button
        # must not leak into the next render.
        self._details_label.setVisible(False)
        self._details_button.setVisible(True)
        try_anyway_visible = should_show_try_anyway(wording.category)
        self._try_anyway_button.setVisible(try_anyway_visible)
        self._try_anyway_button.setEnabled(True)
        # wh-b9kpc: the Why button shares the Try-it-anyway visibility
        # rule. The help text is about the Try-it-anyway flow, so a
        # Why button without a Try-it-anyway button would invite the
        # user to look for a Try-it-anyway button that is not there.
        self._why_button.setVisible(try_anyway_visible)

        self.adjustSize()
        self._anchor_to_screen()

        if not self.isVisible():
            self.show()
        self.raise_()
        self._lifetime_timer.start(max(500, int(lifetime_ms)))

    def _anchor_to_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        margin = 24
        x = geo.x() + geo.width() - self.width() - margin
        y = geo.y() + geo.height() - self.height() - margin
        self.move(x, y)
