"""TerminalDictationEditorWindow -- PySide6 terminal dictation editor.

Runs in the GUI Process. Persistent lifecycle: created once, shown/hidden
as needed. Communicates with Logic Process via Qt signals that GuiManager
connects to IPC queues.

Replaces the old Tkinter-based TerminalDictationEditor that ran in the
Input Process.

wh-eolas (Phase 3 of wh-u3tj2): the editor handles Enter submission
directly via SendInput in the GUI process. The verified-paste helper
at ``utils.gui_terminal_paste.paste_into_terminal`` enforces the four
safety guarantees the original cross-process submit path provided
(active window check, foreground match, verified paste, no Enter on
abort). On every abort path the editor emits a ``submit_failed`` ack
carrying a structured reason; the GUI controller surfaces a
content-neutral failure toast. Lifecycle events (``submit_started`` /
``submit_complete`` / ``submit_failed``) ride the existing
``editor_event_acked`` channel so Logic can drive the ``LogicMirror``
through SUBMITTING and SUBMIT_COMPLETE (or ERROR).
"""
import ctypes
import logging
import uuid
import winreg
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QColor, QPalette, QTextCursor

from services.wheelhouse.shared.ledger import CreditLedger, RetractResult
from ui.text_perfector import TextPerfector
from utils.gui_terminal_paste import PasteOutcome, paste_into_terminal

log = logging.getLogger(__name__)


# wh-pkhrp.3.8: stable AccessibleName / UIA Name property value for the
# terminal dictation editor's QPlainTextEdit. The input-process IME
# composition gate (see ui.uia_text_reader._is_composition_active)
# recognises this exact string and skips the fail-closed branch so
# Phase 3 drain words take the TextPattern2 fast path instead of the
# slow clipboard fallback. Shared with the gate as a module-level
# constant so any future rename is a single edit.
_TERMINAL_EDITOR_ACCESSIBLE_NAME = "WheelHouseTerminalEditor"


# wh-redirect-late-cache-and-fg-poll: the post-show focus-confirmed
# check used to be a single ``QTimer.singleShot(50, ...)``. Windows
# often had not promoted the editor HWND to foreground at the 50 ms
# mark, so ``GetForegroundWindow`` returned the prior foreground HWND
# and the check emitted ``focus_lost`` even though the editor was about
# to become foreground. That dropped buffered dictation words. The
# check now polls: an initial 50 ms delay (preserves the original
# settle time), then up to ``_FOCUS_POLL_BUDGET_MS`` of retries at
# ``_FOCUS_POLL_INTERVAL_MS`` intervals. The total budget stays well
# under the FOCUS_PENDING lifecycle deadline of 500 ms
# (services/wheelhouse/shared/editor_lifecycle.py STATE_TIMEOUTS_S).
_FOCUS_POLL_INITIAL_DELAY_MS = 50
_FOCUS_POLL_INTERVAL_MS = 25
_FOCUS_POLL_BUDGET_MS = 250


class _DictationTextEdit(QPlainTextEdit):
    """QPlainTextEdit that submits on plain Enter, newlines on Shift+Enter.

    wh-editor-enter-submit. The terminal dictation editor wants a plain
    Enter press -- whether from the physical key or synthesised by the
    spoken "submit" command -- to act as the Submit button. A bare
    QPlainTextEdit consumes Return/Enter to insert a newline before the
    parent dialog's ``keyPressEvent`` ever sees it, so the interception
    has to happen here, at the focused control.

    Key handling:
      * Plain Return / numpad Enter, and Ctrl+Return / Ctrl+Enter, call
        the injected ``submit_callback`` and consume the event.
      * Shift+Return / Shift+Enter fall through to the base class, which
        inserts a newline so the user can compose a multi-line command.
      * Every other key falls through unchanged.

    The submit callback is injected rather than referenced directly so
    the widget stays decoupled from the window and is trivially testable.
    """

    def __init__(self, submit_callback, parent=None):
        super().__init__(parent)
        self._submit_callback = submit_callback

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            self._submit_callback()
            return
        super().keyPressEvent(event)


@dataclass(frozen=True, slots=True)
class InsertResult:
    """Result returned by :meth:`TerminalDictationEditorWindow.insert_word`.

    Mirrors Section 5's ``InsertResult`` dataclass; consumed by
    ``EditorIpcResponder`` to build an ``insert_editor_word_response``
    wire payload. Fields:

      * ``chars_inserted`` -- UTF-16 code-unit count the document
        actually consumed (round 2 / codex finding 7.3). For BMP-only
        text this equals ``len(text)``. This is a Qt cursor-position
        delta and is NOT the retract accounting unit.
      * ``failure_reason`` -- ``""`` on success; otherwise one of the
        enumerated values in ``services.wheelhouse.shared.insert_editor_word``.
      * ``clusters_inserted`` -- grapheme-cluster count of the inserted
        run (wh-editor-retract-dup.1.1). This is the unit the retract
        path consumes (``CreditLedger.retract_and_replay`` peels
        clusters and the success invariant is in clusters), so the
        speech-side per-utterance editor total must accumulate THIS, not
        ``chars_inserted``. For BMP text the two coincide; for
        astral-plane input (emoji, etc.) UTF-16 over-counts and a
        chars_inserted-based retract would over-request and underrun.
    """

    chars_inserted: int
    failure_reason: str
    clusters_inserted: int = 0


def _detect_dark_mode() -> bool:
    """Check the Windows registry for dark mode preference."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except OSError:
        return False


def _set_dark_title_bar(hwnd: int, dark: bool) -> None:
    """Tell Windows to use the dark (or light) title bar."""
    try:
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value),
        )
    except Exception as e:
        log.debug("Could not set dark title bar: %s", e)


def _default_win32_ops() -> dict:
    """Return a fresh Win32 callable namespace for _steal_foreground.

    Lazy-binds ctypes attributes and sets strict argtypes once per call
    so a stray non-int cannot silently corrupt a call. Returns a plain
    dict so tests can substitute a fake namespace via the ``win32_ops``
    argument to ``_steal_foreground``.
    """
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return {
        "GetForegroundWindow": user32.GetForegroundWindow,
        "GetWindowThreadProcessId": lambda hwnd: int(
            user32.GetWindowThreadProcessId(hwnd, None)
        ),
        "GetCurrentThreadId": kernel32.GetCurrentThreadId,
        "AttachThreadInput": user32.AttachThreadInput,
        "SetForegroundWindow": user32.SetForegroundWindow,
        "BringWindowToTop": user32.BringWindowToTop,
    }


def _steal_foreground(win_hwnd: int, win32_ops: dict | None = None) -> bool:
    """Bring ``win_hwnd`` to the foreground, bypassing the Windows lock.

    wh-redirect-steal-foreground. The terminal-dictation editor opens
    in response to a voice event that travels STT -> Logic -> Input ->
    GUI. By the time the GUI process calls ``SetForegroundWindow`` for
    the editor, Windows refuses the call: the GUI process has no
    recent user-input attribution and is not the current foreground.
    Without the bypass the editor stays behind the terminal until the
    user clicks it, and the focus-redirect path drops every word it
    tried to drain.

    Standard Windows workaround: attach this thread's input queue to
    the current foreground window's thread, then call
    ``SetForegroundWindow``. The lock check treats the caller as if
    it were the foreground thread and lets the call through. Detach
    immediately afterwards so keyboard / mouse capture do not stay
    shared. Returns True if ``SetForegroundWindow`` reported success.

    The optional ``win32_ops`` parameter is a dependency-injection
    seam for tests; production callers leave it None.
    """
    try:
        ops = win32_ops if win32_ops is not None else _default_win32_ops()
        fg_hwnd = int(ops["GetForegroundWindow"]())
        current_thread = int(ops["GetCurrentThreadId"]())
        if not fg_hwnd:
            return bool(ops["SetForegroundWindow"](win_hwnd))
        fg_thread = int(ops["GetWindowThreadProcessId"](fg_hwnd))
        if not fg_thread or fg_thread == current_thread:
            return bool(ops["SetForegroundWindow"](win_hwnd))
        attached = bool(
            ops["AttachThreadInput"](current_thread, fg_thread, True)
        )
        try:
            ops["BringWindowToTop"](win_hwnd)
            result = bool(ops["SetForegroundWindow"](win_hwnd))
        finally:
            if attached:
                ops["AttachThreadInput"](
                    current_thread, fg_thread, False,
                )
        return result
    except Exception as exc:
        log.debug("_steal_foreground failed: %s", exc)
        return False


class TerminalDictationEditorWindow(QDialog):
    """PySide6 terminal dictation editor window.

    Persistent lifecycle: created once at GUI startup, shown/hidden per
    dictation session. Text perfection (spacing, capitalization) is applied
    locally using TextPerfector since it needs cursor position context.
    """

    # Signals for IPC (connected by GuiManager)
    editor_cancelled = Signal()
    # wh-t81d9.2: ack a previously enqueued show/append te_event so the
    # input-process proxy can advance the retract accounting counter and
    # record the editor HWND. editor_hwnd is 0 for non-show ops.
    #
    # wh-pkhrp.1.2: ``op`` carries one of:
    #   * ``"show"`` -- emitted from ``show_editor`` after the QDialog
    #     is shown. The Phase 3 bridge maps this to ``OPEN_APPLIED`` ->
    #     ``FOCUS_PENDING`` (the contract requires both events, but the
    #     GUI does not yet emit a separate ``open_applied``). Carries
    #     ``editor_hwnd`` so the proxy can record it for terminal-mode
    #     focus verification.
    #   * ``"focus_confirmed"`` -- emitted from
    #     :meth:`_focus_text_edit` after BOTH ``setFocus`` succeeds
    #     AND the Windows foreground HWND matches the editor's
    #     top-level HWND. The Phase 3 bridge maps this to
    #     ``FOCUS_CONFIRMED`` and only then drains the buffer.
    #   * ``"focus_lost"`` -- emitted from :meth:`_focus_text_edit`
    #     when the foreground drifted during the 50 ms focus timer.
    #     The Phase 3 bridge maps this to ``FOCUS_LOST`` and fails
    #     the buffer closed.
    editor_event_acked = Signal(str, str, int)  # (request_id, op, editor_hwnd)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Terminal Dictation")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self._terminal_hwnd: int = 0
        self._submitting: bool = False  # Re-entry guard for do_submit
        self._perfector = TextPerfector()
        # wh-pkhrp.1.2: request_id captured at show_editor time so the
        # deferred focus-confirmed (or focus_lost) ack carries the same
        # id. Cleared when the editor is hidden so a stale request_id
        # from a previous session cannot leak into the next ack.
        self._pending_focus_request_id: str = ""
        # wh-redirect-late-cache-and-fg-poll: remaining budget for the
        # focus-confirmed poll. Seeded on each show; decremented each
        # time ``_focus_text_edit`` re-runs without a foreground match.
        self._focus_poll_remaining_ms: int = 0
        # wh-eolas: paste-helper override for tests. Production leaves
        # this None and the helper imported at module level runs.
        # Tests inject a stub returning a chosen PasteOutcome.
        self._paste_helper = None
        # wh-g2-refactor.18 (Section 6 generation fence): GuiManager seeds
        # the new editor's generation at lazy construction from its own
        # counter. Default ``0`` covers the first-show case and the test
        # path that instantiates the editor directly. The IPC responder's
        # dispatcher reads this attribute on every insert / retract.
        self._editor_generation: int = 0
        self._setup_ui()
        # wh-g2-refactor.18 (Section 5 / Section 3): the credit ledger
        # is constructed once and lives for the editor's lifetime. The
        # editor binds it to the same QPlainTextEdit it owns so the
        # ledger's cursor reads always reflect the document the user
        # sees. Slice 6 wires insert / retract / submit / cancel through
        # the ledger.
        self._ledger = CreditLedger(self._text_edit)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Text editor. wh-editor-enter-submit: the _DictationTextEdit
        # subclass intercepts plain Enter to submit (Shift+Enter inserts
        # a newline) at the focused control, since a plain QPlainTextEdit
        # would swallow Enter as a newline before the dialog sees it.
        self._text_edit = _DictationTextEdit(self.do_submit)
        # wh-g2-refactor.18 (Section 3, deepseek finding H2): the dictation
        # editor's only undo surface is voice retraction; users never press
        # Ctrl+Z in this window. Disabling Qt's undo/redo subsystem prevents
        # the document from snapshotting itself on every insertText, which
        # otherwise grows memory proportional to inserted characters across
        # a session.
        self._text_edit.setUndoRedoEnabled(False)
        self._text_edit.setFont(QFont("Segoe UI", 11))
        # wh-pkhrp.3.8: tag the editor's QPlainTextEdit with a stable
        # AccessibleName so the input-process IME composition gate in
        # uia_text_reader._is_composition_active can recognise this
        # WheelHouse-owned control and skip the slow clipboard
        # fallback. Without the tag, every drain word during a
        # Phase 3 redirect pays the ~50-139 ms clipboard round-trip
        # instead of the ~0.6 ms TextPattern2 fast path -- a 10-word
        # utterance would cost ~500-1400 ms of unnecessary latency.
        # UIA surfaces AccessibleName as the focused control's Name
        # property, which the gate reads cheaply.
        self._text_edit.setAccessibleName(_TERMINAL_EDITOR_ACCESSIBLE_NAME)
        layout.addWidget(self._text_edit)

        # Button row
        btn_layout = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.do_cancel)
        btn_layout.addWidget(self._cancel_btn)

        btn_layout.addStretch()

        self._submit_btn = QPushButton("Submit")
        self._submit_btn.clicked.connect(self.do_submit)
        btn_layout.addWidget(self._submit_btn)

        layout.addLayout(btn_layout)

    # -- Public API (called by GuiManager from queue messages) --

    def show_editor(
        self,
        text: str,
        hwnd: int,
        rect: tuple,
        request_id: str = "",
        utterance_id: str = "",
    ):
        """Show the editor with initial text, positioned near the terminal.

        wh-t81d9.2: when ``request_id`` is non-empty, emit ``editor_event_acked``
        after the show is applied so the input-process proxy can advance its
        retract accounting counter and record this window's HWND.

        Already-visible recovery: if the editor is still visible from a
        previous session that didn't clean up cleanly (the proxy's
        ``_submit_timeout`` cleared ``_is_active`` while the Qt window
        survived), hide and clear the editor first so the next show
        starts against a freshly-reset Qt state.

        The focus-redirect path always opens the editor with empty
        ``text``, so the TextPerfector pass below is a no-op in
        production; the call is preserved so a hypothetical non-empty
        initial text would still be normalised.
        """
        if self.isVisible():
            log.warning(
                "show_editor called while already visible; resetting editor "
                "to a fresh state to keep the strategy mirror and the editor "
                "content in sync."
            )
            self.hide_editor()

        self._terminal_hwnd = hwnd
        self._text_edit.clear()
        # wh-g2-refactor.18 (Section 3 reset table): begin a fresh ledger
        # session. When the caller supplied a utterance_id, seed the
        # ledger so the first insert_editor_word IPC validates cleanly;
        # otherwise leave the id empty so the IPC's implicit-start path
        # binds the first request's id. Both shapes are documented in
        # CreditLedger.insert_word.
        if utterance_id:
            self._ledger.start_utterance(utterance_id)
        else:
            self._ledger.start_utterance("")

        perfected = self._perfector.perfected_string(
            insertion_string=text, preceding_chars="", has_selection=False,
        )
        self._text_edit.setPlainText(perfected)

        # Move cursor to end
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._text_edit.setTextCursor(cursor)

        # Apply theme
        is_dark = _detect_dark_mode()
        self._apply_theme(is_dark)

        # Show first, then position -- Qt on Windows can ignore
        # setGeometry on hidden dialogs.
        self.show()
        self._setup_geometry(rect)
        self.raise_()

        # wh-redirect-steal-foreground: a bare SetForegroundWindow
        # call fails when the GUI process is not the current foreground
        # owner -- which is always the case for the voice-triggered
        # redirect path. _steal_foreground uses AttachThreadInput to
        # bypass the lock so the editor actually wins foreground on
        # show. Without this, the focus-confirmed poll exhausts its
        # 250 ms budget and the redirect drops every drained word.
        win_hwnd = int(self.winId())
        _steal_foreground(win_hwnd)
        # wh-pkhrp.1.2: capture request_id so the deferred
        # focus-confirmed ack (emitted from _focus_text_edit) can
        # reuse it. The Phase 3 bridge demands a separate
        # "focus_confirmed" event so the LogicMirror does not advance
        # to FOCUS_CONFIRMED on the show ack alone -- the contract
        # requires Qt focus AND foreground-HWND match, neither of
        # which has been verified at this point.
        self._pending_focus_request_id = request_id or ""
        self._focus_poll_remaining_ms = _FOCUS_POLL_BUDGET_MS
        # wh-redirect-late-cache-and-fg-poll: claim Qt focus + foreground
        # ONCE at show time. The poll body that follows is observation-
        # only; repeating setFocus/activateWindow on every poll attempt
        # would fight other foreground-claiming windows for 250 ms.
        self._text_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.activateWindow()
        # The scheduled callback carries the rid that started this
        # session so a late callback from a previous session cannot
        # eat this session's poll budget or emit an ack with the
        # wrong rid (adversarial-review finding 5).
        rid = self._pending_focus_request_id
        QTimer.singleShot(
            _FOCUS_POLL_INITIAL_DELAY_MS,
            lambda: self._focus_text_edit(rid),
        )

        if request_id:
            # winId() returns the editor's own native handle. The proxy uses
            # this for terminal-mode focus verification (wh-t81d9.1).
            self.editor_event_acked.emit(request_id, "show", win_hwnd)

    def do_submit(self):
        """Submit the editor contents.

        wh-eolas: the editor runs the verified-paste helper directly in
        the GUI process. On every code path (success, abort, exception)
        the editor emits a lifecycle ack so Logic can drive the
        ``LogicMirror`` through SUBMITTING and then either
        SUBMIT_COMPLETE (on success) or ERROR (on any abort). Enter is
        sent only on full-success verified delivery; every abort skips
        Enter and surfaces a content-neutral failure toast on the GUI
        side.
        """
        if not self.isVisible() or self._submitting:
            return
        self._submitting = True
        try:
            text = self._text_edit.toPlainText()
            hwnd = self._terminal_hwnd
            self._submit_via_gui_paste(text, hwnd)
        finally:
            self._submitting = False

    def _submit_via_gui_paste(self, text: str, hwnd: int) -> None:
        """Run the GUI-process verified-paste and emit lifecycle acks.

        wh-eolas. Steps:

        1. Generate a fresh ``submit_request_id`` for this submit attempt
           so Logic can correlate the started / complete / failed acks
           with each other and with the LogicMirror's transition
           sequence.
        2. Emit ``submit_started`` BEFORE the SendInput call. The
           legacy proxy's safety timeout fires at 5.0 s on the
           Input-process side; the lifecycle's SUBMITTING state has the
           same 5.0 s budget (``STATE_TIMEOUTS_S[SUBMITTING] = 5.0``).
           The mirror is now responsible for that timeout.
        3. Call the verified-paste helper. On SUCCESS, hide the editor
           and emit ``submit_complete``. On any other outcome, hide the
           editor and emit ``submit_failed`` carrying the outcome's
           value as the structured reason -- the GUI controller turns
           the reason into a content-neutral toast (the bead's safety
           constraint 4).
        """
        submit_request_id = uuid.uuid4().hex
        self.editor_event_acked.emit(
            submit_request_id, "submit_started", int(self._terminal_hwnd),
        )
        helper = self._paste_helper or paste_into_terminal
        outcome: PasteOutcome
        try:
            outcome = helper(text, hwnd)
        except Exception as exc:
            log.error(
                "paste_into_terminal raised: %s", exc, exc_info=True,
            )
            outcome = PasteOutcome.EXCEPTION
        # Always close the editor: success or abort, the user pressed
        # Enter and the editor session is over. A failed paste re-opens
        # via the user dictating again; leaving the editor visible
        # would invite a retry that hits the same fail-closed gates
        # without showing the failure toast first.
        #
        # wh-g2-refactor.18 (Section 3 reset table): on the success path
        # the ledger is cleared by ``submit`` rather than the implicit
        # ``cancel`` that ``hide_editor`` runs. Both terminal states
        # leave the ledger empty; the distinction is documented for
        # future telemetry.
        if outcome is PasteOutcome.SUCCESS:
            self._ledger.submit()
        self.hide_editor()
        if outcome is PasteOutcome.SUCCESS:
            self.editor_event_acked.emit(
                submit_request_id, "submit_complete", int(hwnd),
            )
        else:
            self.editor_event_acked.emit(
                submit_request_id,
                f"submit_failed:{outcome.value}",
                int(hwnd),
            )

    def do_cancel(self):
        """Hide window, emit editor_cancelled signal."""
        if not self.isVisible():
            return
        self.hide_editor()
        self.editor_cancelled.emit()

    def hide_editor(self):
        """Hide and reset editor state."""
        self.hide()
        self._text_edit.clear()
        self._terminal_hwnd = 0
        # wh-pkhrp.1.2: drop any unfired focus ack so a stale request_id
        # from this session cannot leak into the next session's
        # focus_text_edit timer.
        self._pending_focus_request_id = ""
        # wh-g2-refactor.18 (Section 3 reset table): clear the credit
        # ledger so a late retract from the previous session cannot
        # match an empty document. ``cancel`` matches the spirit of
        # "hide without submit" -- the text never made it to the
        # terminal.
        self._ledger.cancel()

    # -- G2 IPC entry points (wh-g2-refactor.18) --

    def insert_word(self, text: str, utterance_id: str) -> "InsertResult":
        """Insert ``text`` into the editor for ``utterance_id``.

        Called by ``EditorIpcResponder`` on the Qt main thread when an
        ``insert_editor_word`` IPC arrives from Logic. Returns an
        :class:`InsertResult` whose ``chars_inserted`` is the UTF-16
        code-unit count the document actually consumed and whose
        ``failure_reason`` is one of the values enumerated in
        ``services.wheelhouse.shared.insert_editor_word``.

        Section 5 / round 2 codex finding 7.4: a non-empty session
        mismatch is a hard reject -- the editor MUST NOT silently reset
        the ledger to a stale utterance and insert old text. The
        empty-session case (after ``show_editor`` seeded the ledger
        with an empty id) implicitly starts the new session, matching
        Section 3's reset table.
        """
        if not text:
            return InsertResult(0, "")
        current = self._ledger.utterance_id
        if current == "":
            # Implicit-start path: bind this id and clear the ledger.
            self._ledger.start_utterance(utterance_id)
        elif current != utterance_id:
            log.info(
                "insert_word session mismatch (have=%s got=%s); rejecting",
                current, utterance_id,
            )
            return InsertResult(0, "session_mismatch")
        # wh-editor-retract-dup: apply the spacing pass the legacy dictation
        # path applies, so consecutive editor words are spaced instead of
        # concatenated (``but`` + ``lets`` -> ``butlets``). preceding_chars is
        # the document text already before the append point; TextPerfector
        # reads its tail for the spacing decision.
        #
        # wh-editor-retract-dup.1.2: capitalize=False. The editor's contents
        # are submitted verbatim to a shell, where case is significant
        # (``git status`` must not become ``Git status``). The legacy path
        # only ever perfected words 2..N (mid-sentence, not capitalised), so
        # suppressing sentence-start capitalisation here keeps the editor's
        # casing matching the pre-fix behaviour while still fixing the
        # spacing defect.
        perfected = self._perfector.perfected_string(
            insertion_string=text,
            preceding_chars=self._text_edit.toPlainText(),
            has_selection=False,
            capitalize=False,
        )
        if not perfected:
            # Defensive: perfection never empties a non-empty word today,
            # but guard so the ledger's non-empty contract is not violated.
            return InsertResult(0, "")
        try:
            chars = self._ledger.insert_word(perfected, utterance_id)
        except Exception as exc:
            log.warning("insert_word raised: %s", exc, exc_info=True)
            return InsertResult(0, "editor_unavailable")
        # wh-editor-retract-dup.1.1: report the grapheme-cluster count of the
        # run the ledger just appended (its own canonical-text cluster count),
        # not the UTF-16 delta. The speech side accumulates this so the later
        # retract -- which peels clusters -- requests the matching span.
        runs = self._ledger.runs
        clusters = runs[-1].clusters if runs else int(chars)
        return InsertResult(int(chars), "", clusters_inserted=int(clusters))

    def retract_and_replay(
        self,
        chars_requested: int,
        *,
        utterance_id: str,
        replay_text: str = "",
        whole_utterance: bool = False,
    ) -> RetractResult:
        """Retract ``chars_requested`` clusters and optionally insert ``replay_text``.

        Thin wrapper over ``CreditLedger.retract_and_replay`` (Section 2).
        The ledger validates the utterance, peels runs from the tail of
        the document, and either replays the corrected text or returns
        a ``failure_reason`` matching the IPC schema.

        ``whole_utterance=True`` routes to
        ``CreditLedger.retract_all_and_replay`` instead
        (wh-editor-retract-ledger-authoritative): every ledger run peels
        regardless of ``chars_requested``, so a speech-side mirror that
        drifted below the ledger's true total (an insert response that
        timed out Logic-side while the word landed) cannot cause an
        under-delete.
        """
        if whole_utterance:
            return self._ledger.retract_all_and_replay(
                utterance_id,
                replay_text,
            )
        return self._ledger.retract_and_replay(
            int(chars_requested),
            utterance_id,
            replay_text,
        )

    # -- Overrides --

    def closeEvent(self, event):
        """Hide on close -- don't destroy."""
        event.ignore()
        self.do_cancel()

    def keyPressEvent(self, event):
        """Handle dialog-level shortcuts.

        Escape cancels. wh-editor-enter-submit: plain Enter (Return or
        numpad Enter) submits, so both the physical key and the spoken
        "submit" command (which synthesises an Enter onto the focused
        editor) trigger the Submit button. Shift+Enter inserts a newline
        for composing a multi-line command.

        The focused control during dictation is the QPlainTextEdit, whose
        own ``_DictationTextEdit.keyPressEvent`` already applies this
        policy and consumes plain/Shift Enter before the event reaches
        here. This dialog-level handler is the fallback for when focus is
        elsewhere (for example a button), so the two stay consistent.
        """
        if event.key() == Qt.Key.Key_Escape:
            self.do_cancel()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            self.do_submit()
            return
        super().keyPressEvent(event)

    # -- Internal --

    def _focus_text_edit(self, scheduled_rid: str = ""):
        """Deferred focus after window activation.

        wh-pkhrp.1.2: emits the lifecycle ``focus_confirmed`` ack only
        when BOTH conditions hold at the same instant:

          * ``QPlainTextEdit.setFocus`` succeeded (Qt focus is on the
            text edit), AND
          * ``GetForegroundWindow()`` returns the editor's top-level
            HWND.

        If the foreground drifted during the 50 ms timer (user clicked
        away, another window stole foreground), emit ``focus_lost``
        instead so the LogicMirror moves to ``ERROR`` and the
        focus-change word buffer fails closed -- the buffered words
        were destined for the editor, not for whatever now has
        foreground.

        The show ack already carried ``editor_hwnd`` so the proxy /
        bridge does not need it on this ack. Emitting 0 here would
        violate the editor_lifecycle contract for FOCUS_CONFIRMED, so
        we pass the editor's own winId again.
        """
        # wh-redirect-late-cache-and-fg-poll: ``scheduled_rid`` is
        # captured by the QTimer closure at schedule time. A delayed
        # callback whose session has been cancelled (or replaced by a
        # new show) sees the mismatch and returns without firing.
        # Without this guard a callback from a prior session could
        # ack the new session's rid or burn its poll budget.
        if scheduled_rid != self._pending_focus_request_id:
            return
        rid = scheduled_rid
        win_hwnd = int(self.winId())
        qt_focus_ok = self._text_edit_has_focus()
        foreground_ok = False
        try:
            foreground_ok = (self._get_foreground_hwnd() == win_hwnd)
        except Exception as exc:
            log.debug(
                "GetForegroundWindow failed in _focus_text_edit: %s", exc,
            )
            foreground_ok = False
        if qt_focus_ok and foreground_ok:
            self._pending_focus_request_id = ""
            self._focus_poll_remaining_ms = 0
            # wh-editor-focus-ack-drop: the persistent-editor show path
            # carries no request_id; an ack with an empty rid cannot be
            # correlated by any consumer (Logic drops it with a
            # warning), so emit only when a rid exists.
            if rid:
                self.editor_event_acked.emit(rid, "focus_confirmed", win_hwnd)
            return
        # Foreground often lags Qt focus by tens of milliseconds on
        # Windows. Retry until the foreground catches up or the
        # budget runs out.
        self._focus_poll_remaining_ms -= _FOCUS_POLL_INTERVAL_MS
        if self._focus_poll_remaining_ms > 0:
            QTimer.singleShot(
                _FOCUS_POLL_INTERVAL_MS,
                lambda: self._focus_text_edit(rid),
            )
            return
        log.warning(
            "Editor focus_confirmed check failed after polling: "
            "qt_focus=%s foreground_match=%s (win_hwnd=%s)",
            qt_focus_ok, foreground_ok, win_hwnd,
        )
        self._pending_focus_request_id = ""
        # wh-editor-focus-ack-drop: same empty-rid guard as the
        # focus_confirmed branch above; the diagnostic warning stays.
        if rid:
            self.editor_event_acked.emit(rid, "focus_lost", win_hwnd)

    def _get_foreground_hwnd(self) -> int:
        """Return the current Windows foreground HWND.

        Wrapped in a method so tests can monkeypatch the foreground
        without touching the real Windows API.
        """
        return int(ctypes.windll.user32.GetForegroundWindow())

    def _text_edit_has_focus(self) -> bool:
        """Return True if the QPlainTextEdit currently holds Qt focus.

        Wrapped in a method so tests can isolate the foreground race
        from Qt-level focus quirks in offscreen test runners.
        """
        return bool(self._text_edit.hasFocus())

    def _setup_geometry(self, rect: tuple):
        """Position in the lower-right of the terminal window.

        The rect comes from UIA BoundingRectangle in the Input Process
        (physical pixels, DPI-aware). Qt's setGeometry uses logical pixels.
        We divide by devicePixelRatio to convert.
        """
        if not rect or len(rect) < 4:
            self.resize(500, 160)
            log.warning("No valid rect for editor geometry, using default 500x160")
            return

        # The rect is (left, top, right, bottom) in physical pixels from
        # UIA BoundingRectangle. Qt uses logical pixels. Divide by the
        # devicePixelRatio of the screen containing the terminal.
        screen = self.screen()
        dpr = screen.devicePixelRatio() if screen else 1.0

        term_left = int(rect[0] / dpr)
        term_top = int(rect[1] / dpr)
        term_right = int(rect[2] / dpr)
        term_bottom = int(rect[3] / dpr)
        term_w = term_right - term_left
        term_h = term_bottom - term_top

        win_w = max(int(0.35 * term_w), 300)
        win_h = max(int(0.15 * term_h), 120)

        log.info(
            "Editor geometry: rect=%s dpr=%.2f term=%dx%d win=%dx%d",
            rect, dpr, term_w, term_h, win_w, win_h,
        )

        # Anchor to lower-right of terminal
        x = term_right - win_w
        y = term_bottom - win_h

        # Clamp to screen work area
        if screen:
            avail = screen.availableGeometry()
            if x + win_w > avail.right():
                x = avail.right() - win_w
            if y + win_h > avail.bottom():
                y = avail.bottom() - win_h
            if x < avail.left():
                x = avail.left()
            if y < avail.top():
                y = avail.top()

        self.resize(win_w, win_h)
        self.move(x, y)

        # Nudge the window so the full frame (title bar + borders) stays
        # inside the screen work area.  resize/move set the content rect;
        # the frame extends beyond that.
        if screen:
            avail = screen.availableGeometry()
            frame = self.frameGeometry()
            if frame.bottom() > avail.bottom():
                y -= frame.bottom() - avail.bottom()
            if frame.right() > avail.right():
                x -= frame.right() - avail.right()
            if frame.left() < avail.left():
                x += avail.left() - frame.left()
            if frame.top() < avail.top():
                y += avail.top() - frame.top()
            self.move(x, y)

    def _apply_theme(self, is_dark: bool):
        """Apply dark or light theme colors."""
        if is_dark:
            palette = QPalette()
            bg = QColor("#282C34")
            fg = QColor("#DCDFE4")
            palette.setColor(QPalette.ColorRole.Window, bg)
            palette.setColor(QPalette.ColorRole.WindowText, fg)
            palette.setColor(QPalette.ColorRole.Base, bg)
            palette.setColor(QPalette.ColorRole.Text, fg)
            palette.setColor(QPalette.ColorRole.Button, QColor("#3C4049"))
            palette.setColor(QPalette.ColorRole.ButtonText, fg)
            palette.setColor(QPalette.ColorRole.Highlight, QColor("#61AFEF"))
            palette.setColor(QPalette.ColorRole.HighlightedText, bg)
            self.setPalette(palette)
            self._text_edit.setPalette(palette)
            _set_dark_title_bar(int(self.winId()), True)
        else:
            # Use system default palette
            self.setPalette(QPalette())
            self._text_edit.setPalette(QPalette())
            _set_dark_title_bar(int(self.winId()), False)
