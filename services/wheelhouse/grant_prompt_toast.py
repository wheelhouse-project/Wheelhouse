"""PySide6 follow-up toast for the three-strikes grant prompt (wh-bqv9c).

After the click counter (wh-82lnx) reaches the soft-allow threshold for
an identity tuple, the GUI renders this toast as a separate widget from
the standard rejection toast. It surfaces a Yes/No question:

  Title: "Always type into <App> when you do this?"
  Body:  "You have tried this <N> times in <App>. WheelHouse can stop
         asking and just do it from now on."
  Buttons: [Yes] [No]

The widget is intentionally identity-agnostic: it accepts pre-composed
title and body strings and emits ``yes_clicked`` / ``no_clicked``
signals with no payload. The GUI manager attaches the per-tuple
identity when forwarding the click as IPC. A close without a Yes/No
click fires ``dismissed`` so the manager can keep the per-tuple
dedup map open for the next ``RetryThresholdReached`` event, per the
bead spec.

Both Yes and No disable both action buttons before re-emitting, so a
fast double-click cannot deliver two clicked signals to the same
dialog. ``show_prompt`` re-enables them on the next presentation.
"""

from __future__ import annotations

from typing import Optional

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


class GrantPromptToast(QDialog):
    """Borderless follow-up toast with Yes / No buttons.

    Signals:
      * ``yes_clicked`` -- emitted once per presentation when the user
        clicks Yes. Buttons are disabled before re-emit so a fast
        double-click does not deliver two clicks.
      * ``no_clicked``  -- analogous for No.
      * ``dismissed``   -- emitted when the toast closes without a
        Yes/No click (X button or auto-dismiss). The GUI manager
        treats this as "dismissed without choosing" and keeps the
        per-tuple dedup map open so the next threshold event on the
        same tuple re-fires the toast.
    """

    DEFAULT_LIFETIME_MS = 12000

    yes_clicked = Signal()
    no_clicked = Signal()
    dismissed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # Tracks whether this presentation ended via a Yes/No click.
        # ``closeEvent`` consults the flag to decide whether to emit
        # ``dismissed``. Reset on every ``show_prompt`` so the next
        # presentation starts neutral.
        self._action_taken: bool = False

        self._build_ui()

        self._lifetime_timer = QTimer(self)
        self._lifetime_timer.setSingleShot(True)
        self._lifetime_timer.timeout.connect(self.close)

    def _build_ui(self) -> None:
        outer = QFrame(self)
        outer.setObjectName("grant_toast_outer")
        outer.setStyleSheet(
            "QFrame#grant_toast_outer { "
            "background-color: palette(window); "
            "border: 4px solid rgb(100, 80, 40); "
            "}"
        )

        title_label = QLabel("")
        title_label.setObjectName("grant_toast_title")
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
        body_label.setObjectName("grant_toast_body")
        body_label.setFont(QFont("Segoe UI", 10))
        body_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        body_label.setWordWrap(True)
        body_label.setMinimumWidth(360)
        self._body_label = body_label

        self._yes_button = QPushButton("Yes")
        self._yes_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 12px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._yes_button.clicked.connect(self._on_yes_clicked)

        self._no_button = QPushButton("No")
        self._no_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 12px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        self._no_button.clicked.connect(self._on_no_clicked)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(self._yes_button)
        button_row.addWidget(self._no_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addLayout(title_row)
        inner.addWidget(body_label)
        inner.addLayout(button_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

    def _on_yes_clicked(self) -> None:
        self._action_taken = True
        self._yes_button.setEnabled(False)
        self._no_button.setEnabled(False)
        self.yes_clicked.emit()
        # Close after emit so a subsequent text_target_grant_prompt
        # event for a different tuple is not blocked by the visible
        # toast (wh-vbvgf.7.2 codex finding). closeEvent does NOT
        # emit ``dismissed`` because _action_taken is True.
        self.close()

    def _on_no_clicked(self) -> None:
        self._action_taken = True
        self._yes_button.setEnabled(False)
        self._no_button.setEnabled(False)
        self.no_clicked.emit()
        self.close()

    def show_prompt(
        self,
        title: str,
        body: str,
        *,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
    ) -> None:
        """Render the prompt and show the toast.

        Args:
            title: pre-composed title string.
            body: pre-composed body string.
            lifetime_ms: auto-dismiss timeout. The default is longer
                than the rejection toast (12 seconds) because the user
                must read a question and pick Yes or No, not just
                acknowledge an advisory message.
        """

        self._title_label.setText(title)
        self._body_label.setText(body)

        self._action_taken = False
        self._yes_button.setEnabled(True)
        self._no_button.setEnabled(True)

        self.adjustSize()
        self._anchor_to_screen()

        if not self.isVisible():
            self.show()
        self.raise_()
        self._lifetime_timer.start(max(500, int(lifetime_ms)))

    def closeEvent(self, event):  # type: ignore[override]
        # Emit ``dismissed`` only when the close was not preceded by a
        # Yes/No click. The GUI manager uses this to keep the per-tuple
        # dedup map open for the next threshold event.
        if not self._action_taken:
            self.dismissed.emit()
        super().closeEvent(event)

    def _anchor_to_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        margin = 24
        x = geo.x() + geo.width() - self.width() - margin
        # Stack the grant prompt slightly above the rejection toast
        # position. The standard rejection toast anchors at the very
        # bottom right; the grant prompt sits one toast height above
        # so the user sees both during the short window when they
        # overlap (the rejection toast is auto-dismissing while the
        # grant prompt is up).
        y = geo.y() + geo.height() - self.height() - margin - 100
        if y < geo.y() + margin:
            y = geo.y() + margin
        self.move(x, y)
