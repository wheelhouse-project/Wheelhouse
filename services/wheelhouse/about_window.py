"""The About Wheelhouse window, with its manual update check (wh-about-check-updates).

The window shows the program name, the running version, and where to get
help, as the earlier About box did. It adds a "Check for updates" button,
a line that shows the result of the check, and an "Update now" button that
appears only when a newer release exists.

The window sends no message between processes. It reports the two choices
with the ``check_requested`` and ``update_requested`` signals, and
:class:`gui.GuiManager` sends the commands to the Logic process
(``check_for_updates`` and the existing ``update_now``). The Logic process
runs the check off its event loop and sends one ``update_check_result``,
which GuiManager hands to :meth:`AboutWindow.show_result`.

The widget is a modeless QDialog, so the GUI process keeps handling its
queue while the window is open and while a check runs.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


ABOUT_WINDOW_TITLE = "About Wheelhouse"

# The text of the earlier About box, unchanged.
ABOUT_TEXT = (
    "Wheelhouse\n"
    "Version {version}\n\n"
    "Voice-controlled desktop automation for Windows.\n\n"
    "Choose Help from this menu, or say \"open voice access help\", "
    "to open the Wheelhouse Assistant in your browser."
)

# The Wheelhouse website. The About window shows it as a link that opens in
# the default browser (wh-about-check-updates, acceptance item 8).
WEBSITE_URL = "https://wheelhouse-project.org"

CHECK_BUTTON_LABEL = "Check for updates"
UPDATE_BUTTON_LABEL = "Update now"
CLOSE_BUTTON_LABEL = "Close"

# The result texts. The newer-release line is gui.UPDATE_NOTICE_TITLE, the
# title of the update notice, read when a result arrives (update_available_text
# below), so the notice and this window cannot spell it in two ways. The
# literal stays in gui.py, where the help build looks for it.
UPDATE_CHECKING_TEXT = "Checking for updates..."
UPDATE_CURRENT_TEXT = "You have the newest version ({installed})."
UPDATE_FAILED_TEXT = (
    "Wheelhouse could not reach GitHub. Check your internet connection and "
    "try again."
)
UPDATE_UNVERSIONED_TEXT = (
    "The newest release is Wheelhouse {version}. This copy of Wheelhouse has "
    "no version number, so Wheelhouse cannot compare them."
)


def update_available_text(version: str) -> str:
    """The newer-release line: the update notice title with ``version``.

    Imported when called, not at module load: gui.py imports this module
    inside GuiManager.show_about_dialog, so gui is loaded by then.
    """
    from gui import UPDATE_NOTICE_TITLE

    return UPDATE_NOTICE_TITLE.format(version=version)


def _text(message: dict, key: str) -> str:
    value = message.get(key)
    return value if isinstance(value, str) else ""


class AboutWindow(QDialog):
    """Modeless About window with a "Check for updates" button.

    Emits ``check_requested()`` when a check starts and
    ``update_requested(version, tag)`` when the user chooses Update now.
    Close, the Escape key, and the title-bar X emit nothing.
    """

    check_requested = Signal()
    update_requested = Signal(str, str)

    def __init__(self, version: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ABOUT_WINDOW_TITLE)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self._update_version = ""
        self._update_tag = ""
        self._build_ui(version)

    def _build_ui(self, version: str) -> None:
        about_label = QLabel(ABOUT_TEXT.format(version=version))
        about_label.setObjectName("about_text")
        about_label.setFont(QFont("Segoe UI", 10))
        about_label.setWordWrap(True)
        about_label.setMinimumWidth(420)
        about_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._about_label = about_label

        website_label = QLabel(
            f'<a href="{WEBSITE_URL}">{WEBSITE_URL}</a>'
        )
        website_label.setObjectName("about_website")
        website_label.setFont(QFont("Segoe UI", 10))
        website_label.setTextFormat(Qt.TextFormat.RichText)
        # False so the click reaches linkActivated and open_website opens it.
        website_label.setOpenExternalLinks(False)
        website_label.linkActivated.connect(self.open_website)
        self._website_label = website_label

        status_label = QLabel("")
        status_label.setObjectName("about_update_status")
        status_label.setFont(QFont("Segoe UI", 10))
        status_label.setWordWrap(True)
        status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._status_label = status_label

        self.check_button = QPushButton(CHECK_BUTTON_LABEL)
        self.check_button.setAutoDefault(False)
        self.check_button.clicked.connect(self.start_check)

        self.update_button = QPushButton(UPDATE_BUTTON_LABEL)
        self.update_button.setAutoDefault(False)
        self.update_button.clicked.connect(self._on_update_clicked)
        self.update_button.hide()

        close_button = QPushButton(CLOSE_BUTTON_LABEL)
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.reject)
        self._close_button = close_button

        buttons = QHBoxLayout()
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.update_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(about_label)
        layout.addWidget(website_label)
        layout.addWidget(status_label)
        layout.addLayout(buttons)

    # -- read by tests and GuiManager ------------------------------------

    def about_text(self) -> str:
        return self._about_label.text()

    def status_text(self) -> str:
        return self._status_label.text()

    def open_website(self, _link: str = "") -> None:
        """Open the Wheelhouse website in the default browser.

        Always opens WEBSITE_URL, whatever link text the label reports.
        """
        QDesktopServices.openUrl(QUrl(WEBSITE_URL))

    # -- the check --------------------------------------------------------

    def start_check(self) -> None:
        """Show that a check runs, disable the button, and ask for the check."""
        self._status_label.setText(UPDATE_CHECKING_TEXT)
        self.check_button.setEnabled(False)
        self.update_button.hide()
        self.check_requested.emit()

    def show_result(self, message: dict) -> None:
        """Show one ``update_check_result`` from the Logic process.

        A message that does not hold what its status needs shows the
        failure text: the message crosses a process boundary.
        """
        status = message.get("status")
        version = _text(message, "version")
        tag = _text(message, "tag")
        installed = _text(message, "installed")
        newer = status == "newer" and bool(version) and bool(tag)
        if newer:
            text = update_available_text(version)
        elif status == "current" and installed:
            text = UPDATE_CURRENT_TEXT.format(installed=installed)
        elif status == "unversioned" and version:
            text = UPDATE_UNVERSIONED_TEXT.format(version=version)
        else:
            text = UPDATE_FAILED_TEXT
        self._status_label.setText(text)
        self.check_button.setEnabled(True)
        if newer:
            self._update_version = version
            self._update_tag = tag
            self.update_button.setEnabled(True)
            self.update_button.show()
        else:
            self._update_version = ""
            self._update_tag = ""
            self.update_button.hide()

    def allow_update_again(self) -> None:
        """Enable Update now again after an update that did not start.

        GuiManager calls this on ``update_failed`` and when the queue
        refused the command. Without a newer result it does nothing.
        isHidden, not isVisible: the user can close the window while the
        update runs, and the button must work when the window opens again.
        """
        if self._update_tag and not self.update_button.isHidden():
            self.update_button.setEnabled(True)

    def _on_update_clicked(self) -> None:
        # One press per attempt: this window stays open after the press.
        # allow_update_again enables the button again if the update fails.
        if self._update_tag and self.update_button.isEnabled():
            self.update_button.setEnabled(False)
            self.update_requested.emit(self._update_version, self._update_tag)
