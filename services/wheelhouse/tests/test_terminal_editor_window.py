"""Tests for TerminalDictationEditorWindow (PySide6 QDialog)."""
import pytest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt

# pytest-qt provides the `qtbot` and `qapp` fixtures automatically.
# Do NOT define a manual qapp fixture -- it conflicts with pytest-qt's built-in.


@pytest.fixture
def editor_window(qtbot):
    from terminal_editor_window import TerminalDictationEditorWindow
    window = TerminalDictationEditorWindow()
    yield window
    window.close()


class TestEditorWindowCreation:
    def test_window_created_hidden(self, editor_window):
        assert not editor_window.isVisible()

    def test_window_title(self, editor_window):
        assert editor_window.windowTitle() == "Terminal Dictation"

    def test_has_text_edit_widget(self, editor_window):
        assert editor_window._text_edit is not None

    def test_has_submit_button(self, editor_window):
        assert editor_window._submit_btn is not None

    def test_has_cancel_button(self, editor_window):
        assert editor_window._cancel_btn is not None


class TestEditorShowHide:
    def test_show_editor_makes_visible(self, editor_window):
        editor_window.show_editor("hello", hwnd=123, rect=(0, 0, 1920, 1080))
        assert editor_window.isVisible()
        # TextPerfector capitalizes first letter
        assert "Hello" in editor_window._text_edit.toPlainText()

    def test_show_editor_stores_hwnd(self, editor_window):
        editor_window.show_editor("test", hwnd=456, rect=(0, 0, 800, 600))
        assert editor_window._terminal_hwnd == 456

    def test_hide_clears_text(self, editor_window):
        editor_window.show_editor("text here", hwnd=1, rect=(0, 0, 800, 600))
        editor_window.hide_editor()
        assert not editor_window.isVisible()
        assert editor_window._text_edit.toPlainText() == ""

    def test_double_show_resets_when_already_visible(self, editor_window):
        """wh-t81d9.2 followup: a second show on an already-visible editor
        hides+resets the window before applying the new text. Resetting
        keeps the editor's content aligned with the strategy mirror that
        also resets at the start of a fresh session.
        """
        editor_window.show_editor("first", hwnd=1, rect=(0, 0, 800, 600))
        editor_window.show_editor("second", hwnd=2, rect=(0, 0, 800, 600))
        # Editor now contains only the second show's perfected text.
        # TextPerfector capitalizes first letter.
        text = editor_window._text_edit.toPlainText()
        assert "First" not in text, (
            "Stale prior content must not survive a fresh show; the strategy "
            "mirror has been reset to '' and any leftover text would diverge."
        )
        assert "Second" in text


class TestEditorSignals:
    def test_cancel_emits_signal(self, editor_window, qtbot):
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))
        with qtbot.waitSignal(editor_window.editor_cancelled, timeout=1000):
            editor_window.do_cancel()

    def test_submit_hides_window(self, editor_window):
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))
        editor_window.do_submit()
        assert not editor_window.isVisible()

    def test_escape_cancels(self, editor_window, qtbot):
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))
        with qtbot.waitSignal(editor_window.editor_cancelled, timeout=1000):
            from PySide6.QtTest import QTest
            QTest.keyClick(editor_window, Qt.Key.Key_Escape)


class TestEditorFocusPoll:
    """wh-redirect-late-cache-and-fg-poll: the post-show focus check
    polls for Qt focus AND foreground HWND match rather than a single
    50 ms shot.

    Windows often has not finished promoting the editor's HWND to
    foreground at the 50 ms mark (observed in wheelhouse.log: Qt focus
    True, foreground_match False -> focus_lost -> dropped words). The
    poll retries every 25 ms up to a 250 ms total budget, which stays
    well under the 500 ms FOCUS_PENDING lifecycle deadline.
    """

    def test_focus_confirmed_when_foreground_matches_after_retries(
        self, qtbot, monkeypatch,
    ):
        from terminal_editor_window import TerminalDictationEditorWindow
        window = TerminalDictationEditorWindow()
        qtbot.addWidget(window)

        state = {"call_count": 0}

        def fake_get_fg():
            state["call_count"] += 1
            # Foreground lags: first two checks return a wrong HWND,
            # the third returns the editor's actual top-level HWND.
            if state["call_count"] >= 3:
                return int(window.winId())
            return 0xDEAD

        monkeypatch.setattr(window, "_get_foreground_hwnd", fake_get_fg)
        # Qt focus may not settle deterministically in offscreen test
        # runs; pin it True so the test isolates the foreground race
        # the bug is actually about.
        monkeypatch.setattr(
            window, "_text_edit_has_focus", lambda: True,
        )

        ops = []

        def record(request_id, op, editor_hwnd):
            ops.append(op)

        window.editor_event_acked.connect(record)
        try:
            window.show_editor(
                "", hwnd=1, rect=(0, 0, 800, 600), request_id="r-show",
            )
            qtbot.waitUntil(
                lambda: "focus_confirmed" in ops or "focus_lost" in ops,
                timeout=2000,
            )
        finally:
            window.editor_event_acked.disconnect(record)

        assert "focus_confirmed" in ops
        assert "focus_lost" not in ops
        # The poll ran more than once before the foreground matched.
        assert state["call_count"] >= 3
        window.close()

    def test_focus_lost_when_foreground_never_matches(
        self, qtbot, monkeypatch,
    ):
        from terminal_editor_window import TerminalDictationEditorWindow
        window = TerminalDictationEditorWindow()
        qtbot.addWidget(window)

        monkeypatch.setattr(
            window, "_get_foreground_hwnd", lambda: 0xDEAD,
        )
        monkeypatch.setattr(
            window, "_text_edit_has_focus", lambda: True,
        )

        ops = []

        def record(request_id, op, editor_hwnd):
            ops.append(op)

        window.editor_event_acked.connect(record)
        try:
            window.show_editor(
                "", hwnd=1, rect=(0, 0, 800, 600), request_id="r-show",
            )
            # Wait long enough for the full poll budget (250 ms) plus
            # the initial 50 ms delay, with margin.
            qtbot.waitUntil(
                lambda: "focus_lost" in ops or "focus_confirmed" in ops,
                timeout=2000,
            )
        finally:
            window.editor_event_acked.disconnect(record)

        # 'show' ack arrives synchronously and 'focus_lost' arrives after
        # the poll exhausts.
        assert "focus_lost" in ops
        assert "focus_confirmed" not in ops
        window.close()


class TestEditorFocusPollRidGuard:
    """wh-redirect-late-cache-and-fg-poll (adversarial-review finding 5):
    when a session is hidden mid-poll and a new show starts before the
    first session's poll callback fires, the stale callback must NOT
    eat the new session's poll budget or ack the new session's rid.
    Each scheduled callback carries the rid it was scheduled for and
    bails out if the editor's pending rid does not match.
    """

    def test_stale_callback_does_not_ack_new_session(
        self, qtbot, monkeypatch,
    ):
        from terminal_editor_window import TerminalDictationEditorWindow
        window = TerminalDictationEditorWindow()
        qtbot.addWidget(window)

        # The poll always sees a successful match -- the test is purely
        # about whether the stale callback can fire an ack against the
        # second session's rid.
        monkeypatch.setattr(window, "_text_edit_has_focus", lambda: True)
        monkeypatch.setattr(
            window, "_get_foreground_hwnd", lambda: int(window.winId()),
        )

        ops: list[tuple[str, str]] = []

        def record(request_id, op, editor_hwnd):
            ops.append((request_id, op))

        window.editor_event_acked.connect(record)
        try:
            # Session A starts; hide immediately before the poll fires.
            window.show_editor(
                "", hwnd=1, rect=(0, 0, 800, 600), request_id="rid-A",
            )
            window.hide_editor()
            # Session B starts before A's scheduled callback can run.
            window.show_editor(
                "", hwnd=1, rect=(0, 0, 800, 600), request_id="rid-B",
            )
            qtbot.waitUntil(
                lambda: any(op == "focus_confirmed" for _, op in ops),
                timeout=2000,
            )
        finally:
            window.editor_event_acked.disconnect(record)

        focus_acks = [
            rid for rid, op in ops if op in ("focus_confirmed", "focus_lost")
        ]
        # Exactly one focus ack, for session B. A's stale callback
        # must not have fired against rid-B (or rid-A after it was
        # cleared on hide).
        assert focus_acks == ["rid-B"]
        window.close()


class TestEditorNoRidFocusAcks:
    """wh-editor-focus-ack-drop: the persistent-editor show path
    (``show_editor_persistent`` in Logic) sends ``te_show`` with an
    empty request_id. The show ack is correctly skipped, but the
    deferred focus poll still emitted ``focus_confirmed`` /
    ``focus_lost`` with the empty rid; Logic's ``_handle_te_event_ack``
    drops those with a WARNING ('te_event_ack with empty request_id;
    dropping' -- 27 per day in the 2026-07-10 log, one per editor
    session). An ack no consumer can correlate must not be emitted at
    all.
    """

    def test_no_focus_confirmed_ack_when_shown_without_request_id(
        self, qtbot, monkeypatch,
    ):
        from terminal_editor_window import TerminalDictationEditorWindow
        window = TerminalDictationEditorWindow()
        qtbot.addWidget(window)

        monkeypatch.setattr(window, "_text_edit_has_focus", lambda: True)
        monkeypatch.setattr(
            window, "_get_foreground_hwnd", lambda: int(window.winId()),
        )

        ops = []

        def record(request_id, op, editor_hwnd):
            ops.append((request_id, op))

        window.editor_event_acked.connect(record)
        try:
            window.show_editor("", hwnd=1, rect=(0, 0, 800, 600))
            # The success path zeroes the poll budget when the check
            # passes; wait on that instead of an ack (none may come).
            qtbot.waitUntil(
                lambda: window._focus_poll_remaining_ms == 0,
                timeout=2000,
            )
        finally:
            window.editor_event_acked.disconnect(record)

        assert ops == [], (
            "a show without a request_id must not emit lifecycle acks; "
            "Logic can only drop an empty-rid ack with a warning"
        )
        window.close()

    def test_no_focus_lost_ack_when_shown_without_request_id(
        self, qtbot, monkeypatch,
    ):
        from terminal_editor_window import TerminalDictationEditorWindow
        window = TerminalDictationEditorWindow()
        qtbot.addWidget(window)

        monkeypatch.setattr(window, "_text_edit_has_focus", lambda: True)
        monkeypatch.setattr(window, "_get_foreground_hwnd", lambda: 0xDEAD)

        ops = []

        def record(request_id, op, editor_hwnd):
            ops.append((request_id, op))

        window.editor_event_acked.connect(record)
        try:
            window.show_editor("", hwnd=1, rect=(0, 0, 800, 600))
            # The failure path decrements the budget until exhaustion.
            qtbot.waitUntil(
                lambda: window._focus_poll_remaining_ms <= 0,
                timeout=2000,
            )
            # Give a (wrong) trailing ack a chance to arrive.
            qtbot.wait(100)
        finally:
            window.editor_event_acked.disconnect(record)

        assert ops == [], (
            "the exhausted focus poll must log its diagnostic warning "
            "but not emit an empty-rid focus_lost ack"
        )
        window.close()


class TestStealForeground:
    """wh-redirect-steal-foreground: _steal_foreground bypasses the
    Windows foreground lock via AttachThreadInput when the GUI thread
    is not the current foreground owner. Without this, the editor that
    opens in response to a voice event never wins foreground, the
    post-show focus check exhausts its budget on a foreground
    mismatch, and the focus-redirect path drops every buffered word.
    """

    def _make_ops(
        self,
        *,
        fg_hwnd: int,
        fg_thread: int,
        current_thread: int,
        set_fg_result: int = 1,
    ):
        calls: list[tuple[str, tuple]] = []

        def record(name, *args):
            calls.append((name, args))

        ops = {
            "GetForegroundWindow": lambda: (
                record("GetForegroundWindow"), fg_hwnd,
            )[1],
            "GetCurrentThreadId": lambda: (
                record("GetCurrentThreadId"), current_thread,
            )[1],
            "GetWindowThreadProcessId": lambda hwnd: (
                record("GetWindowThreadProcessId", hwnd), fg_thread,
            )[1],
            "AttachThreadInput": lambda c, f, attach: (
                record("AttachThreadInput", c, f, attach), 1,
            )[1],
            "BringWindowToTop": lambda hwnd: (
                record("BringWindowToTop", hwnd), 1,
            )[1],
            "SetForegroundWindow": lambda hwnd: (
                record("SetForegroundWindow", hwnd), set_fg_result,
            )[1],
        }
        return ops, calls

    def test_attaches_and_detaches_when_foreground_is_other_thread(self):
        from terminal_editor_window import _steal_foreground
        ops, calls = self._make_ops(
            fg_hwnd=0xBEEF, fg_thread=42, current_thread=99,
        )

        result = _steal_foreground(0xABCD, win32_ops=ops)

        assert result is True
        names = [c[0] for c in calls]
        assert names == [
            "GetForegroundWindow",
            "GetCurrentThreadId",
            "GetWindowThreadProcessId",
            "AttachThreadInput",  # attach
            "BringWindowToTop",
            "SetForegroundWindow",
            "AttachThreadInput",  # detach
        ]
        attach_calls = [c[1] for c in calls if c[0] == "AttachThreadInput"]
        assert attach_calls[0] == (99, 42, True)
        assert attach_calls[1] == (99, 42, False)
        assert ("SetForegroundWindow", (0xABCD,)) in calls

    def test_skips_attach_when_no_foreground_window(self):
        from terminal_editor_window import _steal_foreground
        ops, calls = self._make_ops(
            fg_hwnd=0, fg_thread=0, current_thread=99,
        )

        result = _steal_foreground(0xABCD, win32_ops=ops)

        assert result is True
        names = [c[0] for c in calls]
        assert "AttachThreadInput" not in names
        assert ("SetForegroundWindow", (0xABCD,)) in calls

    def test_skips_attach_when_foreground_thread_is_current(self):
        from terminal_editor_window import _steal_foreground
        ops, calls = self._make_ops(
            fg_hwnd=0xBEEF, fg_thread=99, current_thread=99,
        )

        result = _steal_foreground(0xABCD, win32_ops=ops)

        assert result is True
        names = [c[0] for c in calls]
        assert "AttachThreadInput" not in names

    def test_detaches_even_when_set_foreground_fails(self):
        from terminal_editor_window import _steal_foreground
        ops, calls = self._make_ops(
            fg_hwnd=0xBEEF, fg_thread=42, current_thread=99,
            set_fg_result=0,
        )

        result = _steal_foreground(0xABCD, win32_ops=ops)

        assert result is False
        attach_calls = [c[1] for c in calls if c[0] == "AttachThreadInput"]
        assert attach_calls[0] == (99, 42, True)
        assert attach_calls[1] == (99, 42, False)

    def test_returns_false_on_exception_without_raising(self):
        from terminal_editor_window import _steal_foreground

        def boom():
            raise RuntimeError("simulated win32 failure")

        ops = {
            "GetForegroundWindow": boom,
            "GetCurrentThreadId": lambda: 99,
            "GetWindowThreadProcessId": lambda _hwnd: 0,
            "AttachThreadInput": lambda *_a: 0,
            "BringWindowToTop": lambda _hwnd: 1,
            "SetForegroundWindow": lambda _hwnd: 1,
        }

        result = _steal_foreground(0xABCD, win32_ops=ops)

        assert result is False


class TestEditorOpenWithText:
    """The focus-redirect path opens the editor with empty text, but
    show_editor still applies TextPerfector to any non-empty initial
    text it is given.
    """

    def test_show_dictation_runs_perfecter(self, editor_window):
        # Sanity: TextPerfector still capitalizes (regression check).
        editor_window.show_editor(
            "hello", hwnd=1, rect=(0, 0, 800, 600),
        )
        assert "Hello" in editor_window._text_edit.toPlainText()


class TestEditorEnterSubmit:
    """wh-editor-enter-submit: plain Enter submits so the spoken
    "submit" command (which synthesises an Enter keystroke onto the
    focused editor) and the physical Enter key both trigger the Submit
    button. Shift+Enter inserts a newline for composing a multi-line
    command. Ctrl+Enter remains a submit chord.

    Plain Enter must be intercepted at the QPlainTextEdit, not the
    dialog: the text box consumes Return/Enter to insert a newline
    before the dialog's keyPressEvent can see it, so the tests deliver
    the key to ``_text_edit`` (where focus sits during dictation).
    """

    def test_plain_return_submits(self, editor_window):
        from utils.gui_terminal_paste import PasteOutcome
        editor_window._paste_helper = lambda text, hwnd: PasteOutcome.SUCCESS
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))

        from PySide6.QtTest import QTest
        QTest.keyClick(editor_window._text_edit, Qt.Key.Key_Return)
        assert not editor_window.isVisible()

    def test_plain_enter_numpad_submits(self, editor_window):
        from utils.gui_terminal_paste import PasteOutcome
        editor_window._paste_helper = lambda text, hwnd: PasteOutcome.SUCCESS
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))

        from PySide6.QtTest import QTest
        QTest.keyClick(editor_window._text_edit, Qt.Key.Key_Enter)
        assert not editor_window.isVisible()

    def test_shift_return_inserts_newline(self, editor_window):
        editor_window.show_editor("hello", hwnd=1, rect=(0, 0, 800, 600))
        before_text = editor_window._text_edit.toPlainText()

        from PySide6.QtTest import QTest
        QTest.keyClick(
            editor_window._text_edit,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ShiftModifier,
        )

        assert editor_window.isVisible()
        after_text = editor_window._text_edit.toPlainText()
        assert "\n" in after_text
        assert after_text != before_text

    def test_ctrl_return_submits(self, editor_window):
        from utils.gui_terminal_paste import PasteOutcome
        editor_window._paste_helper = lambda text, hwnd: PasteOutcome.SUCCESS
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))

        from PySide6.QtTest import QTest
        QTest.keyClick(
            editor_window._text_edit,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert not editor_window.isVisible()

    def test_ctrl_enter_numpad_submits(self, editor_window):
        from utils.gui_terminal_paste import PasteOutcome
        editor_window._paste_helper = lambda text, hwnd: PasteOutcome.SUCCESS
        editor_window.show_editor("text", hwnd=1, rect=(0, 0, 800, 600))

        from PySide6.QtTest import QTest
        QTest.keyClick(
            editor_window._text_edit,
            Qt.Key.Key_Enter,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert not editor_window.isVisible()

    def test_submitted_text_preserves_real_newlines(self, editor_window):
        from utils.gui_terminal_paste import PasteOutcome
        captured: dict = {}

        def paste_stub(text, hwnd):
            captured["text"] = text
            captured["hwnd"] = hwnd
            return PasteOutcome.SUCCESS

        editor_window._paste_helper = paste_stub
        editor_window.show_editor("", hwnd=42, rect=(0, 0, 800, 600))
        editor_window._text_edit.setPlainText("a\nb")

        editor_window.do_submit()

        assert captured["text"] == "a\nb"
        assert "\\n" not in captured["text"]
