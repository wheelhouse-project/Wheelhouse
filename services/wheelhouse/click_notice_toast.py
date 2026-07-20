"""PySide6 advisory toast for click_element notices (wh-lstwt).

Renders the wording from :mod:`click_notice_toast_wording` as a
borderless dialog anchored near the screen's bottom-right corner. A NEW
sibling of :mod:`rejection_toast`, NOT a reuse of it: the click notice
is advisory only. It has NO "Try it anyway" button and NO "Why am I
seeing this?" button -- a click outcome has no retry-on-click semantics
(v5 design doc, "Click notice IPC schema").

The widget owns no business logic. It is a thin consumer of the wording
helper: the GUI manager constructs a :class:`ClickNoticeEvent`, calls
:func:`compose_click_notice_wording` to get the single-line notice
string, and passes that string here. All testable wording logic lives
in the helper, so the contract test targets pure data and this widget
needs no QApplication-bound unit test.

Auto-dismiss: the widget honours a configurable lifetime. The default
matches the rejection toast's first-notice dwell (8 seconds); the GUI
manager supplies the value.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
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

# WheelHouse plaque identity -- the brown border shared with the
# rejection toast so the two notices read as one product (matches
# rejection_toast.WHEELHOUSE_PLAQUE_BORDER).
WHEELHOUSE_PLAQUE_BORDER = "rgb(100, 80, 40)"


class ClickNoticeToast(QDialog):
    """Borderless advisory toast for a click_element notice.

    Unlike :class:`rejection_toast.RejectionToast` this widget exposes
    no Qt signals and carries no action buttons beyond the dismiss X --
    the click notice is purely advisory and has no retry-on-click flow.
    """

    DEFAULT_LIFETIME_MS = 8000

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._build_ui()

        self._lifetime_timer = QTimer(self)
        self._lifetime_timer.setSingleShot(True)
        self._lifetime_timer.timeout.connect(self.close)

    def _build_ui(self) -> None:
        # Outer frame -- background and text colors come from the Qt
        # palette so the toast tracks the Windows light/dark theme. The
        # brown border is the WheelHouse plaque identity and stays
        # constant across themes.
        outer = QFrame(self)
        outer.setObjectName("click_notice_outer")
        outer.setStyleSheet(
            "QFrame#click_notice_outer { "
            "background-color: palette(window); "
            f"border: 4px solid {WHEELHOUSE_PLAQUE_BORDER}; "
            "}"
        )

        title_label = QLabel("Wheelhouse")
        title_label.setObjectName("click_notice_title")
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
        # Expose the dismiss button so a widget test could exercise the
        # X-click path without findChild gymnastics (mirrors
        # RejectionToast / GrantPromptToast).
        self._dismiss_button = dismiss

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title_label, stretch=1)
        title_row.addWidget(
            dismiss, stretch=0, alignment=Qt.AlignmentFlag.AlignTop
        )

        body_label = QLabel("")
        body_label.setObjectName("click_notice_body")
        body_label.setFont(QFont("Segoe UI", 10))
        body_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        body_label.setWordWrap(True)
        body_label.setMinimumWidth(360)
        self._body_label = body_label

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addLayout(title_row)
        inner.addWidget(body_label)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

    def show_notice(
        self,
        text: str,
        *,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
    ) -> None:
        """Render the composed notice string and show the toast.

        Args:
            text: the single-line notice string from
                :func:`click_notice_toast_wording.compose_click_notice_wording`.
                The GUI manager runs the helper and passes the result
                here; the widget never composes its own wording.
            lifetime_ms: auto-dismiss timeout in milliseconds. Clamped to
                a 500 ms floor so a misconfigured value cannot make the
                toast vanish instantly.
        """

        self._body_label.setText(text)

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
