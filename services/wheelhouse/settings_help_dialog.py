"""Settings window help window (wh-settings-window-help).

The text comes from ``knowledge/settings_window_help.json``, a build output of
scripts/release/build_helpdoc.py. This module only reads that file; it never
writes it. ``load_settings_help`` returns the (label, text) pairs in file
order and raises ``SettingsHelpError`` with a message that names the problem
and the file path. ``build_settings_help_html`` is a pure function, so tests
can pin the page without a QApplication. ``SettingsHelpDialog`` shows the
page with a Close button; Escape closes it through QDialog's own reject.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout

from pattern_manager_dialog import _clamp_dialog_size

SETTINGS_HELP_PATH = (
    Path(__file__).parent / "knowledge" / "settings_window_help.json"
)

_PREFERRED_SIZE = QSize(640, 560)
_MIN_SIZE = QSize(420, 320)


class SettingsHelpError(Exception):
    """The settings help file is missing, unreadable, or has a wrong shape."""


def load_settings_help(path=None) -> list[tuple[str, str]]:
    """Return the (label, text) pairs of the help file, in file order."""
    if path is None:
        path = SETTINGS_HELP_PATH
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise SettingsHelpError(
            f"The settings help file is missing: {path}"
        ) from None
    except OSError as exc:
        raise SettingsHelpError(
            f"The settings help file cannot be read: {path} ({exc})"
        ) from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SettingsHelpError(
            f"The settings help file is not valid UTF-8 JSON: {path} ({exc})"
        ) from None
    entries = data.get("settings") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SettingsHelpError(
            f"The settings help file has no list of settings: {path}"
        )
    pairs = []
    for item in entries:
        label = item.get("label") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if not (isinstance(label, str) and label.strip()
                and isinstance(text, str) and text.strip()):
            raise SettingsHelpError(
                "The settings help file has a settings entry without a "
                f"label and text: {path}"
            )
        pairs.append((label, text))
    return pairs


def build_settings_help_html(pairs) -> str:
    """One entry per pair: the label in bold, then the text. All escaped."""
    parts = ["<h2>WheelHouse Settings Help</h2>"]
    for label, text in pairs:
        parts.append(
            f"<p><b>{html.escape(label)}</b><br/>{html.escape(text)}</p>"
        )
    return "".join(parts)


class SettingsHelpDialog(QDialog):
    """Read-only help window for the Settings window."""

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WheelHouse Settings Help")
        self._labels = [label for label, _ in pairs]
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
        else:
            available = QRect(
                0, 0, _PREFERRED_SIZE.width(), _PREFERRED_SIZE.height()
            )
        min_size, initial_size = _clamp_dialog_size(
            _PREFERRED_SIZE, _MIN_SIZE, available
        )
        self.setMinimumSize(min_size)
        self.resize(initial_size)

        layout = QVBoxLayout(self)
        self._browser = QTextBrowser()
        self._browser.setHtml(build_settings_help_html(pairs))
        layout.addWidget(self._browser)

        close_btn = QPushButton("Close")
        close_btn.setAccessibleName("Close")
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.clicked.connect(lambda checked=False: self.close())
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def entry_labels(self) -> list[str]:
        """The entry labels in display order."""
        return list(self._labels)
