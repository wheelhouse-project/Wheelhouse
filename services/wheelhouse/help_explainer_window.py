"""The window shown before the Wheelhouse Assistant opens (wh-assistant-button-explainer).

A new user who chooses Help reaches the Gemini sign-in page with no
explanation. This window comes first: it says the assistant runs inside
Google Gemini, that a sign-in is needed, that a free account works, and
what signing in involves. An Assistant button then opens the assistant;
a Cancel button opens nothing.

The window carries no settings and sends no message between processes.
It reports the user's choice with the ``assistant_chosen`` signal, which
carries the state of the "Do not show this again" check box, and
:class:`gui.GuiManager` decides what to send to the Logic process. The
Logic process owns the settings file, so it is the process that saves
the choice.

The widget is a modeless QDialog, so the GUI process keeps handling its
queue while the window is open. Structure and styling are copied from
:mod:`rejection_help_window`, which solves the same problem (a plain
explanation window that tracks the Windows light/dark theme through the
Qt palette, with the body in a scroll area so the buttons stay reachable
on a small or scaled screen). That class is copied rather than reused
because its text is the rejection notice's text and it carries a single
Close button.

crewcut: this module duplicates the layout, the palette handling, and the
scroll-area structure of :mod:`rejection_help_window`. The duplication is
accepted so that this change does not edit shipped rejection code. Remove
it by extracting a shared base class when a third window of this shape
appears; two copies do not pay for the base class, and the extraction
would have to re-prove the rejection window's own tests.

The body wording is David's, approved word for word on 2026-09-19
(bead wh-assistant-button-explainer, comment of 2026-09-20 00:56). One
later change edited it: on 2026-09-22 the assistant moved from ChatGPT to
a Google Gemini Gem (wh-gem-replaces-gpt-assistant), which rewrote the
assistant's name, the account sentence (it said a ChatGPT account is
needed; Gemini asks for a sign-in instead), and the sign-up sentence (it
said Sign up and named an email address or a Microsoft account; Gemini
says Sign in and takes a Google account, an Apple account, or an email
address). Do not reword any of it further without his approval; a test
asserts every string.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


EXPLAINER_TITLE = "Ask the Wheelhouse Assistant"

EXPLAINER_BODY_PARAGRAPHS = (
    "The Wheelhouse Assistant answers questions about Wheelhouse."
    " It runs inside Google Gemini, in your web browser.",
    "Gemini asks you to sign in first. A free account works.",
    'No account yet? On the Gemini page, select "Sign in". You can use a'
    " Google account, an Apple account, or an email address."
    " Signing in takes about two minutes.",
    "Then type or dictate your question in plain words.",
)

EXPLAINER_BODY = "\n\n".join(EXPLAINER_BODY_PARAGRAPHS)

DO_NOT_SHOW_AGAIN_LABEL = "Do not show this again"

ASSISTANT_BUTTON_LABEL = "Assistant"

CANCEL_BUTTON_LABEL = "Cancel"


class HelpExplainerWindow(QDialog):
    """Modeless window that explains the assistant before it opens.

    Emits ``assistant_chosen(bool)`` when the user chooses Assistant.
    The value is the state of the "Do not show this again" check box.
    Cancel, the Escape key, and the title-bar X emit nothing, so a user
    who backs out never changes a setting.
    """

    assistant_chosen = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(EXPLAINER_TITLE)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self._build_ui()

    def _build_ui(self) -> None:
        # Imported here rather than at module scope to match
        # rejection_help_window, whose own import of this constant is
        # deferred to avoid a circular import through rejection_toast.
        from rejection_toast import WHEELHOUSE_PLAQUE_BORDER

        outer = QFrame(self)
        outer.setObjectName("explainer_outer")
        outer.setStyleSheet(
            "QFrame#explainer_outer { "
            "background-color: palette(window); "
            f"border: 4px solid {WHEELHOUSE_PLAQUE_BORDER}; "
            "}"
        )

        body_label = QLabel(EXPLAINER_BODY)
        body_label.setObjectName("explainer_body")
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
        # reachable when the window is clamped to a small or scaled
        # screen. Same remedy as rejection_help_window (wh-b9kpc.1.1).
        scroll_area = QScrollArea()
        scroll_area.setObjectName("explainer_scroll")
        scroll_area.setWidget(body_label)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setStyleSheet(
            "QScrollArea#explainer_scroll { background: transparent; } "
            "QScrollArea#explainer_scroll > QWidget > QWidget { "
            "background: transparent; }"
        )
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll_area = scroll_area

        do_not_show_again = QCheckBox(DO_NOT_SHOW_AGAIN_LABEL)
        do_not_show_again.setFont(QFont("Segoe UI", 10))
        do_not_show_again.setStyleSheet(
            "color: palette(windowText); background: transparent;"
        )
        self._do_not_show_again = do_not_show_again

        cancel_button = QPushButton(CANCEL_BUTTON_LABEL)
        cancel_button.setStyleSheet(self._button_style())
        cancel_button.setAutoDefault(False)
        cancel_button.setDefault(False)
        cancel_button.clicked.connect(self.reject)
        self._cancel_button = cancel_button

        assistant_button = QPushButton(ASSISTANT_BUTTON_LABEL)
        assistant_button.setStyleSheet(self._button_style())
        # The Enter key activates the default button, which is the
        # whole of the Enter-key requirement. autoDefault keeps it the
        # default while the focus sits on the check box or the body.
        assistant_button.setAutoDefault(True)
        assistant_button.setDefault(True)
        assistant_button.clicked.connect(self._on_assistant)
        self._assistant_button = assistant_button

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 8, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(cancel_button)
        button_row.addWidget(assistant_button)

        inner = QVBoxLayout(outer)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(6)
        inner.addWidget(scroll_area, stretch=1)
        inner.addWidget(do_not_show_again)
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

    def prepare_to_show(self) -> None:
        """Clear the check box before the window is shown again.

        The GUI process keeps one instance and raises it, so a box left
        checked by an earlier reading must not travel with a later
        Assistant press.
        """
        self._do_not_show_again.setChecked(False)

    def _on_assistant(self) -> None:
        self.assistant_chosen.emit(self._do_not_show_again.isChecked())
        self.accept()
