"""'Your training word' window -- records the word the Parakeet engine puts
in front of each utterance (wh-first-sound-lost.23).

Runs in the GUI process and holds no session logic. The Logic process owns
the session (``speech/lead_word_controller.py``); the window shows whatever
the latest ``lw_state`` payload says and sends one ``lw_*`` action for each
button press.

IPC contract:
    - Emits ``lead_word_action(dict)`` with one of {"action": "lw_record"},
      "lw_keep", "lw_discard", "lw_restore", "lw_off", "lw_on", "lw_close",
      or {"action": "lw_play_start", "which": "current"|"candidate",
      "duration_ms": int}. The GUI manager puts each on the Logic queue.
    - Receives ``lw_state`` payloads through ``handle_state(dict)`` and the
      facts only this process knows (speech on or off, push-to-talk mode,
      the active speech engine, the current microphone) through
      ``set_environment``.
    - Receives ``lw_play_go`` through ``play_now(which)``. Playback follows
      this order: the window asks (``lw_play_start``), Logic turns
      transcription off, Logic answers ``lw_play_go``, and only then does
      the window start the sound.

Every button is a real QPushButton. The accessible name of each button is
set again each time its text changes, so WheelHouse's own voice clicking
('click keep new recording') finds it by UI Automation.
"""

import logging
import os
import wave

import winsound

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from stt.parakeet_model import PROVIDER_NAME

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Strings
# --------------------------------------------------------------------------

WINDOW_TITLE = "Your training word"
# The provider ID that the state update carries for Parakeet. The stt
# package import is light: parakeet_model uses the standard library only.
PARAKEET_PROVIDER = PROVIDER_NAME

INTRO_TEXT = (
    "The Parakeet engine can miss the first sound of a word. A short "
    "recording of you saying hello, placed in front of what you say, "
    "helps it keep that sound."
)
NON_PARAKEET_TEXT = (
    "Only the Parakeet speech engine uses the training word. You are using "
    "a different engine, which does not need it. If you switch to Parakeet "
    "later, come back here."
)
WAITING_FOR_ENGINE_TEXT = "Waiting for the Parakeet engine to start."
LOADING_TEXT = "Asking the speech engine about your recording."
NO_RECORDING_TEXT = "No recording"
# hello.wav exists but has no recorded date: an earlier file (the one the
# key named before the first Keep) brought back with "Go back".
EARLIER_FILE_TEXT = "Recording from your earlier file"
JOIN_ON_TEXT = "The training word is on."
JOIN_OFF_TEXT = "The training word is off."
SPEECH_OFF_TEXT = "Record works while speech is on. Turn speech on first."
RECORD_PROMPT_TEXT = (
    "Say hello once, at your normal speaking volume. Then stay quiet."
)
PTT_TEXT = (
    "You are in push-to-talk mode. Hold the floating button while you "
    "say hello."
)
CHECKING_TEXT = "Checking the recording."
# Shown under a passed recording whose signal-to-noise ratio is below the
# provider's [engine] lead_word_noise_advice_db (wh-training-word-noisy-reject).
# The noise does not refuse a recording. A passed take is only a candidate
# until Keep, so the text does not say that the recording was saved.
NOISE_ADVICE_TEXT = (
    "The background noise is high. Wheelhouse works better in a quieter "
    "place or with a better microphone."
)
REFUSED_FALLBACK_TEXT = "The recording did not pass the check."
PLAY_FAILED_TEXT = "WheelHouse could not play the recording."

NOTICE_TEXTS = {
    "kept": "Your new recording is saved. The training word is on.",
    "restored": "Went back to the previous recording. The training word is on.",
    "off": "The training word is off. Your recordings are kept.",
    "on": "The training word is on.",
}

RECORD = "Record"
RECORD_AGAIN = "Record again"
PLAY_CURRENT = "Play current recording"
PLAY_NEW = "Play new recording"
KEEP = "Keep new recording"
DISCARD = "Discard"
GO_BACK = "Go back to previous recording"
TURN_OFF = "Turn off"
TURN_ON = "Turn on"
CLOSE = "Close"

_MUTED_STYLE = "color: gray;"
_ERROR_STYLE = "color: #b91c1c;"

_FILE_FOR = {"current": "hello.wav", "candidate": "hello.candidate.wav"}


def lead_word_folder():
    """The folder of the recordings, or None when it cannot be built.

    The Parakeet provider builds the same path in
    ``lead_word_store.lead_word_folder`` (services/stt_providers/
    sherpa_offline_parakeet_stt_server/). No message names a path, so each
    side builds it from LOCALAPPDATA. Keep the two the same.
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return os.path.join(base, "WheelHouse", "lead-word")


def _wav_path(which: str):
    folder = lead_word_folder()
    name = _FILE_FOR.get(which)
    if folder is None or name is None:
        return None
    return os.path.join(folder, name)


def wav_duration_ms(path):
    """Length of a WAV file in whole milliseconds, or None if unreadable."""
    try:
        with wave.open(path, "rb") as reader:
            rate = reader.getframerate()
            if rate <= 0:
                return None
            return max(1, round(reader.getnframes() * 1000 / rate))
    except (OSError, EOFError, wave.Error):
        return None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
        os.path.abspath(b)
    )


def keep_note_text(state: dict) -> str:
    """The sentence above the buttons that says what Keep replaces.

    The Keep button itself always reads KEEP: a word file path in the
    button text made the button wider than the window. The sentence is
    empty when Keep replaces nothing (acceptance item 13).
    """
    key_path = state.get("key_path")
    key_path = key_path if isinstance(key_path, str) else ""
    recorded = state.get("recorded")
    recorded = recorded if isinstance(recorded, str) else ""
    if key_path:
        hello = _wav_path("current")
        if hello is None or not _same_path(key_path, hello):
            return f"{KEEP} replaces the word file {key_path}."
    if recorded:
        return f"{KEEP} replaces the recording from {recorded}."
    if state.get("current_exists") is True:
        return f"{KEEP} replaces the recording from your earlier file."
    return ""


def _breakable(text: str) -> str:
    """Add a zero-width space after each path separator in the text.

    Qt breaks a wrapped line only at a space, and a Windows path has none,
    so without these marks a long path is cut where the line ends.
    crewcut: one path part wider than the label still cannot wrap. A
    read-only QTextEdit with WrapAnywhere would remove that limit.
    """
    return text.replace("\\", "\\\u200b").replace("/", "/\u200b")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


class LeadWordDialog(QDialog):
    """The 'Your training word' window.

    Signals:
        lead_word_action(dict): a command for the Logic process.
    """

    lead_word_action = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(520, 380)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        # One lw_close per opening, whichever dismissal path fires first.
        self._close_sent = False
        self._state: dict = {}
        self._env = {
            "speech_enabled": False,
            "push_to_talk": False,
            "provider": None,
            "microphone": "",
        }
        self._local_error = ""

        self._build_ui()
        self._render()

    # ------------------------------------------------------------------ #
    #  UI construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        self._title_label = QLabel(WINDOW_TITLE)
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        layout.addWidget(self._title_label)

        self._intro_label = self._label(INTRO_TEXT)
        layout.addWidget(self._intro_label)
        self._recording_label = self._label("")
        layout.addWidget(self._recording_label)
        self._join_label = self._label("")
        layout.addWidget(self._join_label)
        self._mic_label = self._label("")
        layout.addWidget(self._mic_label)
        self._message_label = self._label("")
        layout.addWidget(self._message_label)
        self._detail_label = self._label("")
        layout.addWidget(self._detail_label)
        self._advice_label = self._label("")
        layout.addWidget(self._advice_label)
        self._error_label = self._label("")
        self._error_label.setStyleSheet(_ERROR_STYLE)
        layout.addWidget(self._error_label)
        self._hint_label = self._label("")
        self._hint_label.setStyleSheet(_MUTED_STYLE)
        layout.addWidget(self._hint_label)
        layout.addStretch()
        # What Keep replaces. It stands above the buttons, with word wrap,
        # so a long word file path shows whole.
        self._keep_note_label = self._label("")
        layout.addWidget(self._keep_note_label)

        self._record_btn = QPushButton(RECORD)
        self._play_current_btn = QPushButton(PLAY_CURRENT)
        self._play_new_btn = QPushButton(PLAY_NEW)
        self._keep_btn = QPushButton(KEEP)
        self._discard_btn = QPushButton(DISCARD)
        self._go_back_btn = QPushButton(GO_BACK)
        self._switch_btn = QPushButton(TURN_OFF)
        self._close_btn = QPushButton(CLOSE)

        self._record_btn.clicked.connect(lambda: self._emit("lw_record"))
        self._play_current_btn.clicked.connect(lambda: self._play("current"))
        self._play_new_btn.clicked.connect(lambda: self._play("candidate"))
        self._keep_btn.clicked.connect(lambda: self._emit("lw_keep"))
        self._discard_btn.clicked.connect(lambda: self._emit("lw_discard"))
        self._go_back_btn.clicked.connect(lambda: self._emit("lw_restore"))
        self._switch_btn.clicked.connect(self._on_switch)
        self._close_btn.clicked.connect(self._dismiss)

        self._buttons = (
            self._record_btn,
            self._play_current_btn,
            self._play_new_btn,
            self._keep_btn,
            self._discard_btn,
            self._go_back_btn,
            self._switch_btn,
            self._close_btn,
        )
        # A stray Enter must not press Keep, Discard or Turn off.
        for btn in self._buttons:
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setAccessibleName(btn.text())

        for row_buttons in (
            (self._record_btn, self._play_current_btn, self._play_new_btn),
            (self._keep_btn, self._discard_btn),
            (self._go_back_btn, self._switch_btn, self._close_btn),
        ):
            row = QHBoxLayout()
            for btn in row_buttons:
                row.addWidget(btn)
            row.addStretch()
            layout.addLayout(row)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    @staticmethod
    def _set_button_text(btn: QPushButton, text: str) -> None:
        """Set the text and the accessible name together, every time."""
        btn.setText(text)
        btn.setAccessibleName(text)

    # ------------------------------------------------------------------ #
    #  Public surface
    # ------------------------------------------------------------------ #

    def begin_session(self):
        """Prepare for a new opening: blank the view and re-arm lw_close."""
        self._close_sent = False
        self._state = {}
        self._local_error = ""
        self._render()

    def handle_state(self, state):
        """Render the latest lw_state payload. Tolerates a malformed one."""
        if not isinstance(state, dict):
            logger.warning("lw_state payload is not a dict; ignored")
            return
        self._state = state
        self._local_error = ""
        self._render()

    def set_environment(
        self, speech_enabled=None, push_to_talk=None, provider=None,
        microphone=None,
    ):
        """Record the facts that only the GUI process knows.

        An argument left at None keeps its earlier value.
        """
        if speech_enabled is not None:
            self._env["speech_enabled"] = bool(speech_enabled)
        if push_to_talk is not None:
            self._env["push_to_talk"] = bool(push_to_talk)
        if provider is not None:
            self._env["provider"] = provider
        if microphone is not None:
            self._env["microphone"] = (
                microphone if isinstance(microphone, str) else ""
            )
        self._render()

    def play_now(self, which):
        """Start the sound. Called when Logic answers lw_play_go.

        Transcription is already off. The sound starts only while the last
        state says a playback is running, so a late or stray lw_play_go
        plays nothing.
        """
        if not self._state.get("playing"):
            logger.info("lw_play_go ignored: no playback is running")
            return False
        if self._close_sent:
            # The window closed after Logic sent lw_play_go, and closing
            # released the hold, so transcription can be on again.
            logger.info("lw_play_go ignored: the window is closed")
            return False
        path = _wav_path(which)
        if path is None or not os.path.isfile(path):
            self._show_local_error(PLAY_FAILED_TEXT)
            return False
        try:
            winsound.PlaySound(
                path, winsound.SND_FILENAME | winsound.SND_ASYNC
            )
        except Exception:
            logger.exception("Lead-word playback failed")
            self._show_local_error(PLAY_FAILED_TEXT)
            return False
        return True

    # ------------------------------------------------------------------ #
    #  Rendering
    # ------------------------------------------------------------------ #

    def _show_local_error(self, text: str) -> None:
        self._local_error = text
        self._render()

    def _is_parakeet(self) -> bool:
        provider = self._env["provider"]
        # An engine not yet known is not called wrong.
        return provider is None or provider == PARAKEET_PROVIDER

    def _render(self):
        state = self._state
        screen = state.get("screen")
        supported = state.get("supported") is True
        busy = state.get("busy") is True
        playing = state.get("playing") is True
        candidate = state.get("candidate") is True
        recorded = state.get("recorded")
        recorded = recorded if isinstance(recorded, str) else ""
        microphone = state.get("microphone")
        microphone = microphone if isinstance(microphone, str) else ""
        # The Logic process fills current_exists for every provider (from
        # the recorded date when an older provider does not send it).
        has_recording = state.get("current_exists") is True
        loaded = state.get("loaded") is True
        recording_now = screen in ("listening", "checking")
        locked = recording_now or playing or busy

        wrong_engine = not self._is_parakeet()

        # Intro, recording line, join state.
        self._intro_label.setVisible(not wrong_engine)
        if wrong_engine:
            self._message_label.setText(NON_PARAKEET_TEXT)
            self._message_label.setVisible(True)
            for label in (
                self._recording_label, self._join_label, self._mic_label,
                self._detail_label, self._error_label, self._hint_label,
                self._advice_label,
            ):
                label.setText("")
                label.setVisible(False)
            self._apply_buttons({})
            return

        waiting = screen in (None, "closed", "loading") or not supported
        if waiting:
            self._message_label.setText(
                LOADING_TEXT if supported else WAITING_FOR_ENGINE_TEXT
            )
            self._message_label.setVisible(True)
            for label in (
                self._recording_label, self._join_label, self._mic_label,
                self._detail_label, self._hint_label, self._advice_label,
            ):
                label.setText("")
                label.setVisible(False)
            self._set_error(self._local_error or state.get("error"))
            self._apply_buttons({})
            return

        if has_recording:
            line = f"Recording from {recorded}" if recorded else EARLIER_FILE_TEXT
            if microphone:
                line += f", microphone {microphone}"
        else:
            line = NO_RECORDING_TEXT
        self._recording_label.setText(line)
        self._recording_label.setVisible(True)
        self._join_label.setText(JOIN_ON_TEXT if loaded else JOIN_OFF_TEXT)
        self._join_label.setVisible(True)

        # The microphone suggestion. The join stays on.
        current_mic = self._env["microphone"]
        suggest = bool(
            has_recording and microphone and current_mic
            and microphone != current_mic
        )
        self._mic_label.setText(
            f"You made this recording with a different microphone "
            f"({microphone}). Your microphone now is {current_mic}. A new "
            f"recording may work better. The training word stays on until "
            f"you turn it off."
            if suggest else ""
        )
        self._mic_label.setVisible(suggest)

        # Message and detail for the screen.
        message, detail = self._screen_texts(state, screen)
        self._message_label.setText(message)
        self._message_label.setVisible(bool(message))
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))
        # The provider decides the advice; an older provider sends no field.
        advice = screen == "passed" and state.get("noise_advice") is True
        self._advice_label.setText(NOISE_ADVICE_TEXT if advice else "")
        self._advice_label.setVisible(advice)
        self._set_error(self._local_error or state.get("error"))

        # Record is available only while speech is on. In push-to-talk mode
        # speech is off between holds, so the mode counts as on.
        speech_ok = self._env["speech_enabled"] or self._env["push_to_talk"]
        record_hint = (
            not speech_ok and screen in ("idle", "refused") and not locked
        )
        self._hint_label.setText(SPEECH_OFF_TEXT if record_hint else "")
        self._hint_label.setVisible(record_hint)

        self._apply_buttons({
            "screen": screen,
            "locked": locked,
            "candidate": candidate,
            "has_recording": has_recording,
            "loaded": loaded,
            "previous_exists": state.get("previous_exists") is True,
            "speech_ok": speech_ok,
            "keep_note": keep_note_text(state),
        })

    def _screen_texts(self, state, screen):
        message = ""
        detail = ""
        if screen == "listening":
            message = RECORD_PROMPT_TEXT
            if self._env["push_to_talk"]:
                detail = PTT_TEXT
        elif screen == "checking":
            message = CHECKING_TEXT
        elif screen == "passed":
            measures = state.get("measures")
            measures = measures if isinstance(measures, dict) else {}
            parts = []
            length = _number(measures.get("voiced_ms"))
            if length is not None:
                parts.append(f"Length: {round(length)} ms.")
            level = _number(measures.get("level_dbfs"))
            if level is not None:
                parts.append(f"Level: {level:.0f} dBFS.")
            parts.append("One word: hello.")
            message = "The recording passed the check."
            detail = " ".join(parts)
        elif screen == "refused":
            reason = state.get("reason")
            message = (
                f"This recording was not used. {reason}"
                if isinstance(reason, str) and reason
                else REFUSED_FALLBACK_TEXT
            )
        else:
            notice = state.get("notice")
            message = NOTICE_TEXTS.get(notice, "") if isinstance(notice, str) else ""
        return message, detail

    def _set_error(self, text):
        text = text if isinstance(text, str) else ""
        self._error_label.setText(text)
        self._error_label.setVisible(bool(text))

    def _apply_buttons(self, view: dict):
        """Set visibility, enabled state and text of every button."""
        buttons = (
            self._record_btn, self._play_current_btn, self._play_new_btn,
            self._keep_btn, self._discard_btn, self._go_back_btn,
            self._switch_btn,
        )
        if not view:
            # Waiting or wrong engine: only Close is there.
            for btn in buttons:
                btn.setVisible(False)
            self._close_btn.setVisible(True)
            self._close_btn.setEnabled(True)
            self._set_button_text(self._close_btn, CLOSE)
            self._set_keep_note("")
            return

        screen = view["screen"]
        locked = view["locked"]
        has_recording = view["has_recording"]
        in_passed = screen == "passed"

        # Record / Record again.
        self._record_btn.setVisible(not in_passed)
        self._set_button_text(
            self._record_btn, RECORD_AGAIN if screen == "refused" else RECORD
        )
        self._record_btn.setEnabled(not locked and view["speech_ok"])

        # Play current recording: only when a recording exists.
        self._play_current_btn.setVisible(True)
        self._set_button_text(self._play_current_btn, PLAY_CURRENT)
        self._play_current_btn.setEnabled(has_recording and not locked)

        # Candidate buttons.
        for btn in (self._play_new_btn, self._keep_btn, self._discard_btn):
            btn.setVisible(in_passed)
        self._set_button_text(self._play_new_btn, PLAY_NEW)
        self._set_button_text(self._keep_btn, KEEP)
        self._set_keep_note(view["keep_note"] if in_passed else "")
        self._set_button_text(self._discard_btn, DISCARD)
        busy_only = self._state.get("busy") is True
        playing = self._state.get("playing") is True
        self._play_new_btn.setEnabled(not busy_only and not playing)
        self._keep_btn.setEnabled(not busy_only and not playing)
        # Logic ignores every command during a playback.
        self._discard_btn.setEnabled(not busy_only and not playing)

        # Go back to previous recording.
        self._go_back_btn.setVisible(view["previous_exists"] and not in_passed)
        self._set_button_text(self._go_back_btn, GO_BACK)
        self._go_back_btn.setEnabled(not locked)

        # Turn off / Turn on: one button, its text and name change together.
        turn_on = has_recording and not view["loaded"]
        self._switch_btn.setVisible(not in_passed)
        self._set_button_text(self._switch_btn, TURN_ON if turn_on else TURN_OFF)
        self._switch_btn.setEnabled(has_recording and not locked)

        # A new recording waits for a choice: Close is not there. The title
        # bar X and Escape still close the window and delete the recording.
        self._close_btn.setVisible(not in_passed)
        self._close_btn.setEnabled(True)
        self._set_button_text(self._close_btn, CLOSE)

    def _set_keep_note(self, sentence: str) -> None:
        """Show what Keep replaces, or hide the line when it is empty."""
        self._keep_note_label.setText(_breakable(sentence))
        # The screen reader gets the sentence without the break marks. The
        # name starts with "Note: " because the sentence starts with the Keep
        # button's name: without the prefix a voice click on "Keep new
        # recording" would also match the note as a starts-with match (see
        # confidence_scorer.is_eligible) and could press nothing or go
        # ambiguous. An empty sentence keeps the empty name of the hidden label.
        self._keep_note_label.setAccessibleName(
            "Note: " + sentence if sentence else sentence
        )
        self._keep_note_label.setVisible(bool(sentence))

    # ------------------------------------------------------------------ #
    #  Actions
    # ------------------------------------------------------------------ #

    def _emit(self, action: str, **extra):
        self.lead_word_action.emit({"action": action, **extra})

    def _on_switch(self):
        # The text decides: the button reads 'Turn on' when the join is off.
        self._emit("lw_on" if self._switch_btn.text() == TURN_ON else "lw_off")

    def _play(self, which: str):
        path = _wav_path(which)
        duration_ms = wav_duration_ms(path) if path else None
        if duration_ms is None:
            self._show_local_error(PLAY_FAILED_TEXT)
            return
        self._emit("lw_play_start", which=which, duration_ms=duration_ms)

    def _stop_sound(self):
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            logger.debug("Lead-word sound stop failed", exc_info=True)

    def _send_close_once(self):
        if not self._close_sent:
            self._close_sent = True
            self.lead_word_action.emit({"action": "lw_close"})

    def _dismiss(self):
        """Every dismissal path (Close, Escape, title-bar close) sends one
        lw_close, which makes Logic delete a candidate and leave a running
        recording (acceptance item 26)."""
        self._stop_sound()
        self._send_close_once()
        self.hide()

    def closeEvent(self, event):
        """Hide instead of closing, so the window can open again."""
        self._dismiss()
        event.ignore()

    def reject(self):
        """Escape key: same as Close."""
        self._dismiss()
