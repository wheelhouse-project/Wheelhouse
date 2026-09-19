"""Voice-teaching (calibration) window -- guided per-voice tuning session.

Launched from the GUI process tray menu ("Teach WheelHouse your voice...")
or by the Logic process pushing an ``open_calibration`` action on the state
queue (the "learn my voice" voice-command path). The window renders
whichever screen the latest ``cal_state`` payload from the Logic process
names; it holds no session logic of its own (wh-7ou.7.3.1, spec
docs/superpowers/specs/2026-08-07-voice-calibration-design.md Sections
3.1-3.7). Every user-facing string here ships exactly as written in the
spec; changing one is a design change.

IPC contract (mirrors the Pattern Manager dialog):
    - Emits ``calibration_action(dict)`` with one of
      {"action": "cal_start"}, {"action": "cal_skip_noise"},
      {"action": "cal_apply"}, {"action": "cal_cancel"}.
    - Receives Logic's cal_state payloads via ``handle_state(dict)``
      called by the GUI manager. Missing keys are tolerated; unknown
      screens are ignored.

Design rules enforced here: no technical terms, no numbers outside the
"Show details" expander, no clocks, countdowns, or time estimates; the
progress dots re-render only from a received state and the dialog owns no
timers, so they can never advance on their own. Every button is a real
QPushButton whose accessible name is its visible text, so WheelHouse's own
voice clicking finds it through UI Automation.
"""

import logging

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QWidget,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Shipped strings (spec Sections 3.1-3.6, verbatim; em dashes are the
# spec's own U+2014 characters and are CP1252-safe)
# --------------------------------------------------------------------------

WRONG_PROVIDER_TEXT = (
    "Voice teaching works with the Distil-Whisper speech recognizer. "
    "You're currently using a different one, which doesn't need it. If "
    "you switch to Distil-Whisper later, come back here."
)
INTRO_TITLE = "Teach WheelHouse your voice"
INTRO_BODY = (
    "WheelHouse sometimes misses short words like comma. This short "
    "session teaches WheelHouse how you sound so it stops missing them. "
    "There's no time limit — go at your own pace. Ready?"
)
PTT_LINE = (
    "You're in push-to-talk mode — hold the talk button while you "
    "say each word."
)
SAY_THIS_WORD = "Say this word:"
GOT_IT = "Got it!"
TRY_AGAIN = "Let's try that one again."
NOISE_TEXT = (
    "Now cough or clear your throat. Do this three separate times, "
    "pausing between each one. This teaches WheelHouse the difference "
    "between your words and other sounds."
)
REVIEW_FULL = "Done! WheelHouse learned your voice. Apply the improvement?"
REVIEW_PARTIAL = (
    "Done! WheelHouse learned part of your voice. Your words and your "
    "other sounds were too similar to tell apart this time, so part of "
    "the tune-up was kept as it was. Trying again in a quieter room may "
    "help. Apply the part that worked?"
)
REVIEW_NOTHING = (
    "WheelHouse couldn't learn enough from this session. Nothing was "
    "changed. Trying again in a quieter room, or closer to the "
    "microphone, may help. Speaking in a normal tone of voice helps too "
    "— not too loud and not too soft."
)
APPLYING_TEXT = (
    "WheelHouse is restarting its listening — about ten seconds."
)
APPLIED_TEXT = "All set. Try saying comma into a text box."
SAVE_FAILED_TEXT = (
    "WheelHouse couldn't save your settings file, so nothing was "
    "changed. Your results are still here — choose Apply to try "
    "again. If it keeps failing, the usual causes are a full disk or "
    "another program blocking WheelHouse's files, such as antivirus or "
    "backup software. The exact error is under \"Show details.\""
)
RESTART_SLOW_TEXT = (
    "WheelHouse's speech recognizer is taking longer than expected to "
    "restart. It should recover on its own in a moment."
)
CANCELLED_TEXT = (
    "This teaching session has ended. Anything you say now types "
    "normally again. Say \"learn my voice\" any time to start a new "
    "session."
)

# The CP1252-unsafe glyphs are built with chr() so printing this file's
# source never crashes a CP1252 terminal (Windows Development rule).
_FILLED_DOT = chr(0x25CF)  # black circle: a completed capture
_EMPTY_DOT = chr(0x25CB)  # white circle: a capture still to come
_CHECK_MARK = chr(0x2713)  # green check shown with "Got it!"

_GREEN_FEEDBACK_STYLE = "color: #15803d; font-weight: bold;"
_MUTED_STYLE = "color: gray;"


def _dots(filled, total) -> str:
    """Render the step-progress dots. Values are clamped so a malformed
    state can never render negative or overflowing dots."""
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        total = 0
    if not isinstance(filled, int) or isinstance(filled, bool):
        filled = 0
    filled = max(0, min(filled, total))
    return " ".join([_FILLED_DOT] * filled + [_EMPTY_DOT] * (total - filled))


def _format_details(details, error_detail) -> str:
    """Plain-text body for the Show details expander: the exact provider
    error (save_failed only), then the before-and-after values and the
    measured ranges. This is the one place numbers are allowed."""
    lines = []
    if error_detail:
        lines.append(f"Error: {error_detail}")
    if isinstance(details, dict):
        for key, heading in (
            ("current", "Current"),
            ("proposed", "Proposed"),
            ("measured", "Measured"),
        ):
            block = details.get(key)
            if isinstance(block, dict) and block:
                lines.append(f"{heading}:")
                for name, value in block.items():
                    lines.append(f"    {name} = {value}")
    return "\n".join(lines)


class CalibrationDialog(QDialog):
    """The voice-teaching window.

    Signals:
        calibration_action(dict): Emitted to send a cal_* command to the
            Logic process (forwarded onto commands_to_logic_queue by the
            GUI manager).
    """

    calibration_action = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(INTRO_TITLE)
        self.setMinimumSize(480, 340)
        # Prevent Qt from quitting the app when this dialog closes
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        # One cal_cancel per session, whichever dismissal path fires
        # first (Cancel / Not now / OK / Escape / title-bar close).
        self._cancel_sent = False

        self._build_ui()

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack)

        self._page_blank = QWidget()
        self._page_wrong_provider = self._build_wrong_provider_page()
        self._page_intro = self._build_intro_page()
        self._page_word = self._build_word_page()
        self._page_noise = self._build_noise_page()
        self._page_review = self._build_review_page()
        self._page_applying = self._build_applying_page()
        self._page_applied = self._build_applied_page()
        self._page_restart_slow = self._build_restart_slow_page()
        self._page_cancelled = self._build_cancelled_page()

        for page in (
            self._page_blank,
            self._page_wrong_provider,
            self._page_intro,
            self._page_word,
            self._page_noise,
            self._page_review,
            self._page_applying,
            self._page_applied,
            self._page_restart_slow,
            self._page_cancelled,
        ):
            self._stack.addWidget(page)
        self._stack.setCurrentWidget(self._page_blank)

        # The buttons Enter may activate, one default per visible screen
        # (set in _show_page). Dismissal buttons are never the default so
        # a stray Enter cannot cancel a session.
        self._default_candidates = (
            self._start_btn,
            self._apply_btn,
            self._wrong_provider_ok_btn,
            self._review_ok_btn,
            self._applied_ok_btn,
            self._restart_slow_ok_btn,
            self._cancelled_ok_btn,
        )
        for btn in (
            self._intro_not_now_btn,
            self._word_cancel_btn,
            self._skip_btn,
            self._noise_cancel_btn,
            self._review_not_now_btn,
            self._details_btn,
        ):
            btn.setAutoDefault(False)
            btn.setDefault(False)

        # Voice clicking finds buttons through UI Automation by their
        # accessible name, which must be the visible text.
        for btn in self.findChildren(QPushButton):
            btn.setAccessibleName(btn.text())

    @staticmethod
    def _message_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    @staticmethod
    def _button_row(*buttons) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch()
        for btn in buttons:
            row.addWidget(btn)
        row.addStretch()
        return row

    def _build_wrong_provider_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._wrong_provider_label = self._message_label(WRONG_PROVIDER_TEXT)
        layout.addWidget(self._wrong_provider_label)
        layout.addStretch()
        self._wrong_provider_ok_btn = QPushButton("OK")
        self._wrong_provider_ok_btn.clicked.connect(self._dismiss)
        layout.addLayout(self._button_row(self._wrong_provider_ok_btn))
        return page

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._intro_title = QLabel(INTRO_TITLE)
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._intro_title.setFont(title_font)
        self._intro_title.setWordWrap(True)
        layout.addWidget(self._intro_title)
        self._intro_body = self._message_label(INTRO_BODY)
        layout.addWidget(self._intro_body)
        self._intro_ptt_line = self._message_label(PTT_LINE)
        self._intro_ptt_line.setVisible(False)
        layout.addWidget(self._intro_ptt_line)
        layout.addStretch()
        self._start_btn = QPushButton("Start")
        self._start_btn.clicked.connect(
            lambda: self.calibration_action.emit({"action": "cal_start"})
        )
        self._intro_not_now_btn = QPushButton("Not now")
        self._intro_not_now_btn.clicked.connect(self._dismiss)
        layout.addLayout(
            self._button_row(self._start_btn, self._intro_not_now_btn)
        )
        return page

    def _build_word_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._word_instruction = self._message_label(SAY_THIS_WORD)
        layout.addWidget(self._word_instruction)
        self._word_label = QLabel("")
        word_font = QFont()
        word_font.setPointSize(48)
        word_font.setBold(True)
        self._word_label.setFont(word_font)
        self._word_label.setTextFormat(Qt.TextFormat.PlainText)
        self._word_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._word_label)
        self._word_progress = QLabel("")
        self._word_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._word_progress)
        self._word_dots = QLabel("")
        self._word_dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._word_dots)
        self._word_feedback = QLabel("")
        self._word_feedback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._word_feedback)
        layout.addStretch()
        self._word_cancel_btn = QPushButton("Cancel")
        self._word_cancel_btn.clicked.connect(self._dismiss)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._word_cancel_btn)
        layout.addLayout(row)
        return page

    def _build_noise_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._noise_message = self._message_label(NOISE_TEXT)
        layout.addWidget(self._noise_message)
        self._noise_dots = QLabel("")
        self._noise_dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._noise_dots)
        self._noise_feedback = QLabel("")
        self._noise_feedback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._noise_feedback)
        layout.addStretch()
        self._skip_btn = QPushButton("Skip this part")
        self._skip_btn.clicked.connect(
            lambda: self.calibration_action.emit({"action": "cal_skip_noise"})
        )
        self._noise_cancel_btn = QPushButton("Cancel")
        self._noise_cancel_btn.clicked.connect(self._dismiss)
        layout.addLayout(
            self._button_row(self._skip_btn, self._noise_cancel_btn)
        )
        return page

    def _build_review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._review_message = self._message_label("")
        layout.addWidget(self._review_message)
        # "Show details" expander: everyone else never sees a number.
        self._details_btn = QPushButton("Show details")
        self._details_btn.setCheckable(True)
        self._details_btn.toggled.connect(self._on_details_toggled)
        details_row = QHBoxLayout()
        details_row.addWidget(self._details_btn)
        details_row.addStretch()
        layout.addLayout(details_row)
        self._details_text = QLabel("")
        self._details_text.setWordWrap(True)
        self._details_text.setTextFormat(Qt.TextFormat.PlainText)
        self._details_text.setStyleSheet(_MUTED_STYLE)
        self._details_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._details_text.setVisible(False)
        layout.addWidget(self._details_text)
        layout.addStretch()
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(
            lambda: self.calibration_action.emit({"action": "cal_apply"})
        )
        self._review_not_now_btn = QPushButton("Not now")
        self._review_not_now_btn.clicked.connect(self._dismiss)
        self._review_ok_btn = QPushButton("OK")
        self._review_ok_btn.clicked.connect(self._dismiss)
        self._review_ok_btn.setVisible(False)
        layout.addLayout(
            self._button_row(
                self._apply_btn, self._review_not_now_btn, self._review_ok_btn
            )
        )
        return page

    def _build_applying_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._applying_label = self._message_label(APPLYING_TEXT)
        layout.addWidget(self._applying_label)
        layout.addStretch()
        return page

    def _build_applied_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._applied_label = self._message_label(APPLIED_TEXT)
        layout.addWidget(self._applied_label)
        layout.addStretch()
        self._applied_ok_btn = QPushButton("OK")
        self._applied_ok_btn.clicked.connect(self._dismiss)
        layout.addLayout(self._button_row(self._applied_ok_btn))
        return page

    def _build_restart_slow_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._restart_slow_label = self._message_label(RESTART_SLOW_TEXT)
        layout.addWidget(self._restart_slow_label)
        layout.addStretch()
        self._restart_slow_ok_btn = QPushButton("OK")
        self._restart_slow_ok_btn.clicked.connect(self._dismiss)
        layout.addLayout(self._button_row(self._restart_slow_ok_btn))
        return page

    def _build_cancelled_page(self) -> QWidget:
        # Shown when the Logic process ends the session on its own (the
        # twenty-minute inactivity cancel, or its internal failure
        # recovery): the typing gate has released, so the window must
        # say the session is over rather than stay on a capture screen.
        page = QWidget()
        layout = QVBoxLayout(page)
        self._cancelled_label = self._message_label(CANCELLED_TEXT)
        layout.addWidget(self._cancelled_label)
        layout.addStretch()
        self._cancelled_ok_btn = QPushButton("OK")
        self._cancelled_ok_btn.clicked.connect(self._dismiss)
        layout.addLayout(self._button_row(self._cancelled_ok_btn))
        return page

    # ------------------------------------------------------------------ #
    #  State rendering
    # ------------------------------------------------------------------ #

    def handle_state(self, state):
        """Render the screen the latest cal_state payload names.

        Tolerates missing keys and ignores unknown screens: the Logic
        process owns the session, and a GUI that crashes on a malformed
        state would take the tray and floating button down with it.
        """
        if not isinstance(state, dict):
            logger.warning("cal_state payload is not a dict; ignored")
            return
        screen = state.get("screen")
        renderer = {
            "wrong_provider": self._render_wrong_provider,
            "intro": self._render_intro,
            "word": self._render_word,
            "noise": self._render_noise,
            "review": self._render_review,
            "applying": self._render_applying,
            "applied": self._render_applied,
            "save_failed": self._render_save_failed,
            "restart_slow": self._render_restart_slow,
            "cancelled": self._render_cancelled,
        }.get(screen)
        if renderer is None:
            logger.warning("cal_state names unknown screen %r; ignored", screen)
            return
        renderer(state)

    def _show_page(self, page, default_btn=None):
        for btn in self._default_candidates:
            btn.setDefault(btn is default_btn)
        self._stack.setCurrentWidget(page)

    def _render_wrong_provider(self, state):
        self._show_page(
            self._page_wrong_provider, default_btn=self._wrong_provider_ok_btn
        )

    def _render_intro(self, state):
        self._intro_ptt_line.setVisible(bool(state.get("push_to_talk", False)))
        self._show_page(self._page_intro, default_btn=self._start_btn)

    def _render_word(self, state):
        self._word_label.setText(str(state.get("word", "")))
        self._word_progress.setText(
            f"Word {state.get('word_index', 1)} of {state.get('word_total', 4)}"
        )
        self._word_dots.setText(
            _dots(state.get("sample_count", 0), state.get("sample_total", 5))
        )
        self._set_feedback(self._word_feedback, state.get("feedback"))
        self._show_page(self._page_word)

    def _render_noise(self, state):
        self._noise_dots.setText(
            _dots(state.get("noise_count", 0), state.get("noise_total", 3))
        )
        self._set_feedback(self._noise_feedback, state.get("feedback"))
        self._show_page(self._page_noise)

    def _render_review(self, state):
        kind = state.get("review_kind", "full")
        message = {
            "partial": REVIEW_PARTIAL,
            "nothing": REVIEW_NOTHING,
        }.get(kind, REVIEW_FULL)
        self._review_message.setText(message)
        nothing = kind == "nothing"
        self._apply_btn.setVisible(not nothing)
        self._review_not_now_btn.setVisible(not nothing)
        self._review_ok_btn.setVisible(nothing)
        self._set_details(state.get("details"), None)
        self._show_page(
            self._page_review,
            default_btn=self._review_ok_btn if nothing else self._apply_btn,
        )

    def _render_save_failed(self, state):
        # The save-failed state is the finish screen again, with Apply
        # still available so the save can be retried immediately, and the
        # exact provider error inside Show details (spec Section 3.6).
        self._review_message.setText(SAVE_FAILED_TEXT)
        self._apply_btn.setVisible(True)
        self._review_not_now_btn.setVisible(True)
        self._review_ok_btn.setVisible(False)
        self._set_details(state.get("details"), state.get("error_detail"))
        self._show_page(self._page_review, default_btn=self._apply_btn)

    def _render_applying(self, state):
        self._show_page(self._page_applying)

    def _render_applied(self, state):
        self._show_page(self._page_applied, default_btn=self._applied_ok_btn)

    def _render_restart_slow(self, state):
        self._show_page(
            self._page_restart_slow, default_btn=self._restart_slow_ok_btn
        )

    def _render_cancelled(self, state):
        self._show_page(
            self._page_cancelled, default_btn=self._cancelled_ok_btn
        )

    def _set_feedback(self, label: QLabel, feedback):
        if feedback == "got_it":
            label.setText(f"{_CHECK_MARK} {GOT_IT}")
            label.setStyleSheet(_GREEN_FEEDBACK_STYLE)
        elif feedback == "try_again":
            label.setText(TRY_AGAIN)
            label.setStyleSheet("")
        else:
            label.setText("")
            label.setStyleSheet("")

    def _set_details(self, details, error_detail):
        text = _format_details(details, error_detail)
        self._details_text.setText(text)
        has_details = bool(text)
        self._details_btn.setVisible(has_details)
        if not has_details:
            self._details_btn.setChecked(False)
        self._details_text.setVisible(
            has_details and self._details_btn.isChecked()
        )

    def _on_details_toggled(self, checked: bool):
        self._details_text.setVisible(checked and bool(self._details_text.text()))

    # ------------------------------------------------------------------ #
    #  Session lifecycle
    # ------------------------------------------------------------------ #

    def reset_session(self):
        """Prepare for a fresh session: called by the GUI manager before
        it sends cal_session_open. Blanks the screen (Logic answers with
        the first cal_state) and re-arms the one-shot cancel."""
        self._cancel_sent = False
        self._details_btn.setChecked(False)
        self._details_text.setText("")
        self._word_feedback.setText("")
        self._noise_feedback.setText("")
        self._stack.setCurrentWidget(self._page_blank)

    def _send_cancel_once(self):
        if not self._cancel_sent:
            self._cancel_sent = True
            self.calibration_action.emit({"action": "cal_cancel"})

    def _dismiss(self):
        """Every dismissal path (Cancel / Not now / OK / Escape / close)
        maps to cal_cancel per the message contract; Logic ignores a
        cancel for a session that already ended."""
        self._send_cancel_once()
        self.hide()

    def closeEvent(self, event):
        """Hide instead of closing (like the Pattern Manager), and tell
        Logic the window went away so calibration mode never stays on."""
        self._send_cancel_once()
        self.hide()
        event.ignore()

    def reject(self):
        """Escape key: same as any other dismissal."""
        self._dismiss()
