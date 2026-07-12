"""PySide6 follow-up toast for soft-allow disk-write failures (wh-9dkse).

When LogicController.add_soft_allow fails to persist a soft-allow
tuple to disk, it emits a ``soft_allow_write_failed`` event on the
GUI state queue. The GUI manager renders this widget as the user-
visible acknowledgment:

  Title: "WheelHouse couldn't save your choice"
  Body:  "Try saying the words again later, then click Yes again."
  Buttons: [OK]

The widget is informational, not interactive: there is no retry
path inside the widget. The OK button and the dismiss X both close
the toast; auto-dismiss closes it after the configured lifetime.
The user re-attempts later by saying the dictation words again,
which re-fires the verified-retry counter and re-publishes the
threshold event when it next reaches the soft-allow trigger.

The widget is identity-agnostic: it accepts pre-composed title and
body strings. The GUI manager owns the wording.
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


class SoftAllowWriteFailedToast(QDialog):
    """Borderless one-shot toast with a single OK button.

    Mirrors the WheelHouse plaque styling used by the rejection
    toast and the grant prompt. No payload signal -- the widget is
    informational; the OK button just closes.
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
        outer = QFrame(self)
        outer.setObjectName("write_failed_toast_outer")
        outer.setStyleSheet(
            "QFrame#write_failed_toast_outer { "
            "background-color: palette(window); "
            "border: 4px solid rgb(100, 80, 40); "
            "}"
        )

        title_label = QLabel("")
        title_label.setObjectName("write_failed_toast_title")
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
        self._dismiss_button = dismiss

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title_label, stretch=1)
        title_row.addWidget(
            dismiss, stretch=0,
            alignment=Qt.AlignmentFlag.AlignTop,
        )

        body_label = QLabel("")
        body_label.setObjectName("write_failed_toast_body")
        body_label.setFont(QFont("Segoe UI", 10))
        body_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        body_label.setWordWrap(True)
        body_label.setMinimumWidth(360)
        self._body_label = body_label

        self._ok_button = QPushButton("OK")
        self._ok_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 12px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._ok_button.clicked.connect(self._on_ok_clicked)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(self._ok_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addLayout(title_row)
        inner.addWidget(body_label)
        inner.addLayout(button_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

    def _on_ok_clicked(self) -> None:
        # Disable before close so a fast double-click cannot fire the
        # handler twice on the same dialog. show_message re-enables.
        self._ok_button.setEnabled(False)
        self.close()

    def show_message(
        self,
        title: str,
        body: str,
        *,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
    ) -> None:
        """Render the message and show the toast.

        Args:
            title: pre-composed title string.
            body: pre-composed body string.
            lifetime_ms: auto-dismiss timeout. Clamped to a 500ms
                minimum so a non-positive caller value does not
                produce a toast that never auto-dismisses.
        """

        self._title_label.setText(title)
        self._body_label.setText(body)

        self._ok_button.setEnabled(True)

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
        # Stack one toast height above the rejection toast position
        # so a stale rejection toast still on screen does not overlap
        # this acknowledgment. Mirrors GrantPromptToast.
        y = geo.y() + geo.height() - self.height() - margin - 100
        if y < geo.y() + margin:
            y = geo.y() + margin
        self.move(x, y)
