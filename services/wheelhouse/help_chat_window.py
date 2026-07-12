"""HelpChatWindow -- PySide6 multi-turn help chat UI.

Runs in the GUI Process. Communicates with the Logic Process via Qt
signals that the GuiController connects to IPC queues. Persistent
lifecycle: created once, shown/hidden as needed.
"""

import ctypes
import logging

import markdown

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
    QLineEdit, QPushButton,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QPalette, QTextCharFormat, QColor, QTextCursor

log = logging.getLogger(__name__)

# Auto-cancel inference after this many seconds with no response
_INFERENCE_TIMEOUT_SEC = 60


class HelpChatWindow(QDialog):
    """Multi-turn help chat window for WheelHouse."""

    # Signals for IPC (connected by GuiController)
    question_submitted = Signal(str)
    reset_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WheelHouse Help")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(500, 400)
        self.resize(550, 500)
        self._is_thinking = False
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Chat log (read-only)
        self._chat_log = QTextEdit()
        self._chat_log.setReadOnly(True)
        self._chat_log.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self._chat_log)

        # Input row
        input_layout = QHBoxLayout()
        self._input_field = QLineEdit()
        self._input_field.setPlaceholderText("Type a question...")
        self._input_field.setFont(QFont("Segoe UI", 10))
        self._input_field.returnPressed.connect(self._on_send)
        input_layout.addWidget(self._input_field)

        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        input_layout.addWidget(self._send_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._cancel_btn.hide()
        input_layout.addWidget(self._cancel_btn)

        layout.addLayout(input_layout)

        # Bottom row
        self._new_chat_btn = QPushButton("New Chat")
        self._new_chat_btn.clicked.connect(self._on_new_chat)
        layout.addWidget(self._new_chat_btn)

    # -- User actions --

    def _on_send(self):
        text = self._input_field.text().strip()
        if not text or self._is_thinking:
            return
        self._append_user_message(text)
        self._input_field.clear()
        self._set_thinking(True)
        self.question_submitted.emit(text)

    def _on_cancel(self):
        self.cancel_requested.emit()

    def _on_new_chat(self):
        self._chat_log.clear()
        self.reset_requested.emit()

    def _on_timeout(self):
        """Auto-cancel after timeout."""
        if self._is_thinking:
            self.cancel_requested.emit()
            self.show_error("Response timed out. Try again or start a new chat.")

    # -- Responses from Logic Process --

    def show_response(self, text: str):
        """Display an assistant response in the chat log."""
        self._set_thinking(False)
        self._remove_thinking_indicator()
        self._append_assistant_message(text)

    def show_error(self, message: str):
        """Display an error message in the chat log."""
        self._set_thinking(False)
        self._remove_thinking_indicator()
        self._append_system_message(f"[Error] {message}")

    def show_unavailable(self, message: str):
        """Show model-unavailable message and disable input."""
        self._chat_log.clear()
        self._append_system_message(message)
        self._input_field.setEnabled(False)
        self._send_btn.setEnabled(False)

    def set_available(self):
        """Re-enable input after model becomes available."""
        self._input_field.setEnabled(True)
        self._send_btn.setEnabled(True)

    # -- Submit question programmatically (from voice command) --

    def submit_question(self, question: str):
        """Submit a question as if the user typed and pressed Send."""
        self._input_field.setText(question)
        self._on_send()

    def showEvent(self, event):
        """Force foreground focus and focus the input field.

        On Windows, activateWindow() alone fails when the calling process
        doesn't own the foreground. We use SetForegroundWindow via ctypes
        to force it, then focus the input field after the event loop settles.
        """
        super().showEvent(event)
        hwnd = int(self.winId())
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        QTimer.singleShot(50, self._focus_input)

    def _focus_input(self):
        """Deferred input focus after window activation settles."""
        self._input_field.setFocus()
        self._input_field.activateWindow()

    # -- Internal --

    def _set_thinking(self, thinking: bool):
        self._is_thinking = thinking
        self._input_field.setEnabled(not thinking)
        self._send_btn.setVisible(not thinking)
        self._cancel_btn.setVisible(thinking)
        if thinking:
            self._append_system_message("Thinking...")
            self._timeout_timer.start(_INFERENCE_TIMEOUT_SEC * 1000)
        else:
            self._timeout_timer.stop()

    def _append_user_message(self, text: str):
        cursor = self._chat_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # Blank line separator
        if self._chat_log.toPlainText():
            cursor.insertText("\n")
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText("You: ", fmt)
        fmt.setFontWeight(QFont.Weight.Normal)
        cursor.insertText(text, fmt)
        self._chat_log.setTextCursor(cursor)
        self._chat_log.ensureCursorVisible()

    def _append_assistant_message(self, text: str):
        cursor = self._chat_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(60, 60, 180))
        fmt.setFontWeight(QFont.Weight.Bold)
        cursor.insertText("WheelHouse: ", fmt)
        # Render markdown as HTML for formatted responses.
        # `tables` enables pipe-table rendering; `fenced_code` handles ``` blocks.
        html = markdown.markdown(text, extensions=["tables", "fenced_code"])
        cursor.insertHtml(html)
        self._chat_log.setTextCursor(cursor)
        self._chat_log.ensureCursorVisible()

    def _append_system_message(self, text: str):
        cursor = self._chat_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(128, 128, 128))
        fmt.setFontItalic(True)
        cursor.insertText(text, fmt)
        self._chat_log.setTextCursor(cursor)
        self._chat_log.ensureCursorVisible()

    def _remove_thinking_indicator(self):
        """Remove the 'Thinking...' line from the end of the chat log."""
        text = self._chat_log.toPlainText()
        if text.endswith("\nThinking..."):
            cursor = self._chat_log.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            # Select from start of "Thinking..." line to end
            cursor.movePosition(
                QTextCursor.MoveOperation.StartOfLine,
                QTextCursor.MoveMode.KeepAnchor,
            )
            # Include the preceding newline
            cursor.movePosition(
                QTextCursor.MoveOperation.Left,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.removeSelectedText()

    def closeEvent(self, event):
        """Hide on close -- don't destroy. Preserves conversation history."""
        event.ignore()
        self.hide()
