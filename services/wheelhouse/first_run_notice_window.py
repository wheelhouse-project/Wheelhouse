"""The window shown on the first start after an install (wh-first-run-notice).

A new user cannot tell whether Wheelhouse hears them. This window says
where to look (the round red button, which pulses from red to orange while
the user speaks) and tells the user to say "help" on its own. An Open help
button opens the help page directly; a Close button only closes the window.

The window carries no settings and sends no message between processes. It
reports the Open help choice with the ``help_chosen`` signal, and
:class:`gui.GuiManager` sends the command to the Logic process. The Logic
process decides whether to show the window at all: it reads a marker file
once at start-up (``main.LogicController._run_first_run_notice``).

The widget is a modeless QDialog, so the GUI process keeps handling its
queue while the window is open. Structure and styling are copied from
:mod:`help_explainer_window`, which solves the same problem.

crewcut: this module duplicates the layout, the palette handling, and the
scroll-area structure of :mod:`help_explainer_window` (itself a copy of
:mod:`rejection_help_window`). The duplication is accepted so that this
change does not edit the help explanation window, which the public website
mirrors and which has its own mutation gate. Remove it by extracting a
shared base class for the three windows.

The title, the body, and the two button labels are David's, approved word
for word (bead wh-first-run-notice). Do not reword any of it without his
approval; a test asserts every string.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
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


FIRST_RUN_NOTICE_TITLE = "Welcome to Wheelhouse"

FIRST_RUN_NOTICE_BODY_PARAGRAPHS = (
    "The round red button shows that Wheelhouse hears you. The button pulses"
    " from red to orange while you speak.",
    "Say the word help on its own to open the help page.",
    "Wheelhouse shows this message once.",
)

FIRST_RUN_NOTICE_BODY = "\n\n".join(FIRST_RUN_NOTICE_BODY_PARAGRAPHS)

OPEN_HELP_BUTTON_LABEL = "Open help"

CLOSE_BUTTON_LABEL = "Close"


class FirstRunNoticeWindow(QDialog):
    """Modeless window that tells a new user how to see that Wheelhouse hears them.

    Emits ``help_chosen()`` when the user chooses Open help. Close, the
    Escape key, and the title-bar X emit nothing.
    """

    help_chosen = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(FIRST_RUN_NOTICE_TITLE)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self._build_ui()

    def _build_ui(self) -> None:
        # Imported here rather than at module scope to match
        # help_explainer_window, whose own import of this constant is
        # deferred to avoid a circular import through rejection_toast.
        from rejection_toast import WHEELHOUSE_PLAQUE_BORDER

        outer = QFrame(self)
        outer.setObjectName("first_run_outer")
        outer.setStyleSheet(
            "QFrame#first_run_outer { "
            "background-color: palette(window); "
            f"border: 4px solid {WHEELHOUSE_PLAQUE_BORDER}; "
            "}"
        )

        body_label = QLabel(FIRST_RUN_NOTICE_BODY)
        body_label.setObjectName("first_run_body")
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

        # The body sits in a scroll area so the buttons below stay
        # reachable when the window is clamped to a small or scaled screen.
        scroll_area = QScrollArea()
        scroll_area.setObjectName("first_run_scroll")
        scroll_area.setWidget(body_label)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setStyleSheet(
            "QScrollArea#first_run_scroll { background: transparent; } "
            "QScrollArea#first_run_scroll > QWidget > QWidget { "
            "background: transparent; }"
        )
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area = scroll_area

        close_button = QPushButton(CLOSE_BUTTON_LABEL)
        close_button.setStyleSheet(self._button_style())
        close_button.setAutoDefault(False)
        close_button.setDefault(False)
        close_button.clicked.connect(self.reject)
        self._close_button = close_button

        help_button = QPushButton(OPEN_HELP_BUTTON_LABEL)
        help_button.setStyleSheet(self._button_style())
        # The Enter key activates the default button.
        help_button.setAutoDefault(True)
        help_button.setDefault(True)
        help_button.clicked.connect(self._on_open_help)
        self._help_button = help_button

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        button_row.addWidget(help_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addWidget(scroll_area, stretch=1)
        inner.addLayout(button_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(outer)

        self.adjustSize()

    @staticmethod
    def _button_style() -> str:
        return (
            "QPushButton { color: palette(buttonText); "
            "background: transparent; "
            "border: 1px solid palette(mid); padding: 4px 12px; } "
            "QPushButton:hover { background: palette(midlight); }"
        )

    def _on_open_help(self) -> None:
        self.help_chosen.emit()
        self.accept()
