"""Help window for the rejection notice's Why-am-I-seeing-this button (wh-b9kpc).

The window explains in plain English why the rejection notice
appears, what Try-it-anyway does, and what the three-click follow-up
prompt is for. The widget is a modeless QDialog so it stays open
while the user reads and does not block the underlying rejection
notice. The notice and the help window can both be visible at the
same time.

The window carries no business logic and no IPC. It is opened by
:class:`rejection_toast.RejectionToast` when the user clicks the
Why-am-I-seeing-this button; the toast owns the lifecycle of a
single help-window instance per toast.

Styling uses the Qt palette (`palette(windowText)`, `palette(window)`)
so the dialog tracks the active Windows light/dark theme. No
hard-coded colors except the WheelHouse plaque border that matches
the rejection notice.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


HELP_TITLE = "Why am I seeing this?"

HELP_BODY = (
    "WheelHouse cannot always tell which controls accept typed text.\n"
    "\n"
    "Some apps (Notepad, the Brave address bar) are easy because "
    "Windows tells WheelHouse that the control accepts text. Other "
    "apps (Zed, Sublime, GPU-rendered editors) draw their text "
    "fields in a way that hides them from Windows.\n"
    "\n"
    "When WheelHouse cannot tell, it refuses to type by default "
    "rather than typing into the wrong spot.\n"
    "\n"
    "What you can do:\n"
    "\n"
    "  - Click Try it anyway to type the words you just spoke "
    "into the spot. The words go in this one time.\n"
    "\n"
    "  - Click Try it anyway three times in a row on the same "
    "kind of spot in the same app. A second small box pops up "
    "and asks if WheelHouse should always type into that kind of "
    "spot in that app.\n"
    "\n"
    "  - Click Yes on that box. From then on, WheelHouse types "
    "into the same kind of spot in the same app silently. No "
    "notice. No button.\n"
    "\n"
    "  - Click No on that box if you do not want WheelHouse to "
    "make it automatic. The notice will still appear next time, "
    "so you can still click Try it anyway as a one-off.\n"
    "\n"
    "WheelHouse decides what counts as the same kind of spot by "
    "looking at how the control identifies itself. A different "
    "field inside the same app (for example, the editor body "
    "versus the command palette) is a separate kind of spot, so "
    "it needs its own Try it anyway."
)


class RejectionHelpWindow(QDialog):
    """Modeless help window opened by the Why-am-I-seeing-this button.

    The dialog uses the standard window decorations so the user can
    close it with the title-bar X. A bottom Close button gives the
    same affordance inside the dialog for users who prefer it.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(HELP_TITLE)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self._build_ui()

    def _build_ui(self) -> None:
        # Imported lazily to avoid a circular import: rejection_toast
        # imports RejectionHelpWindow, and the plaque-border constant
        # lives in rejection_toast (wh-b9kpc local review finding 7).
        from rejection_toast import WHEELHOUSE_PLAQUE_BORDER

        outer = QFrame(self)
        outer.setObjectName("help_outer")
        outer.setStyleSheet(
            "QFrame#help_outer { "
            "background-color: palette(window); "
            f"border: 4px solid {WHEELHOUSE_PLAQUE_BORDER}; "
            "}"
        )

        body_label = QLabel(HELP_BODY)
        body_label.setObjectName("help_body")
        body_label.setFont(QFont("Segoe UI", 10))
        body_label.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        body_label.setWordWrap(True)
        body_label.setMinimumWidth(460)
        body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_label = body_label

        # wh-b9kpc.1.1 (codex review): wrap the body in a scroll area
        # so the Close button below stays reachable even when the
        # window is clamped to a screen smaller than the natural
        # body height (high-DPI / accessibility scaling). The scroll
        # area is transparent so the plaque background still shows
        # through.
        scroll_area = QScrollArea()
        scroll_area.setObjectName("help_scroll")
        scroll_area.setWidget(body_label)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setStyleSheet(
            "QScrollArea#help_scroll { background: transparent; } "
            "QScrollArea#help_scroll > QWidget > QWidget { "
            "background: transparent; }"
        )
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area = scroll_area

        close_button = QPushButton("Close")
        close_button.setStyleSheet(
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 12px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )
        close_button.clicked.connect(self.close)
        self._close_button = close_button

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(close_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addWidget(scroll_area, stretch=1)
        inner.addLayout(button_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

        self.adjustSize()
