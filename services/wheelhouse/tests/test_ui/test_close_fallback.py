"""Second try for a spoken close (wh-xray-close-app-fails).

David's 2026-10-01 run of "x-ray close notepad" activated Notepad and sent
Alt+F4, and Notepad stayed open: some windows ignore the chord. The boss
ruling (10:28, option 1): after Alt+F4, wait up to about 500 ms; when the
SAME window is still open, in front, enabled and has no owned popup, post
WM_SYSCOMMAND / SC_CLOSE to it. A window that shows a save prompt, a modal
dialog or the shutdown dialog is left alone.

Layers covered here:

* ui.close_fallback.close_if_still_open, with a fake Win32 layer;
* ui.close_fallback.is_close_chord;
* UIActionHandler.hotkey_action starting the fallback for Alt+F4 only;
* the close-app path (activate step, then Alt+F4) for a target window that
  is already in front and for one behind another window.
"""
import queue
import threading
from unittest.mock import MagicMock, patch

import pytest

from ui import close_fallback
from ui.close_fallback import (
    CLOSE_POLL_S,
    CLOSE_WAIT_S,
    GW_ENABLEDPOPUP,
    SC_CLOSE,
    SHELL_WINDOW_CLASSES,
    WM_SYSCOMMAND,
    close_if_still_open,
    is_close_chord,
)

HWND = 0x1234
OTHER = 0x9999
# The provenance marker the handler stores on the window before Alt+F4.
MARKER = 0x5A5A0001
OTHER_MARKER = 0x5A5A0002


class FakeWin32:
    """A fake Win32 layer with a fake clock; records every call it matters for."""

    def __init__(self, *, class_name="Notepad", foreground=HWND, enabled=True,
                 popup="self", closes_after_sleeps=None, raises_in=None,
                 provenance=MARKER):
        self.class_name = class_name
        self.foreground = foreground
        self.enabled = enabled
        self.popup = HWND if popup == "self" else popup
        self.closes_after_sleeps = closes_after_sleeps
        self.raises_in = raises_in
        # What read_hwnd_provenance answers for HWND: the original window's
        # marker, 0 for an unmarked window object (a recycled handle), or
        # another window's marker.
        self.provenance = provenance
        self.events = []
        self.now = 0.0
        self.sleeps = []
        self.posts = []
        self.get_window_args = []

    def _maybe_raise(self, name):
        if self.raises_in == name:
            raise OSError(f"{name} failed")

    def is_window(self, hwnd):
        self._maybe_raise("IsWindow")
        if self.closes_after_sleeps is None:
            return True
        return len(self.sleeps) < self.closes_after_sleeps

    def ops(self):
        def read_provenance(hwnd):
            self._maybe_raise("ReadProvenance")
            self.events.append(("ReadProvenance", hwnd))
            return self.provenance

        def get_class(hwnd):
            self._maybe_raise("GetClassName")
            return self.class_name

        def foreground():
            self._maybe_raise("GetForegroundRoot")
            return self.foreground

        def enabled(hwnd):
            self._maybe_raise("IsWindowEnabled")
            return self.enabled

        def get_window(hwnd, cmd):
            self._maybe_raise("GetWindow")
            self.get_window_args.append((hwnd, cmd))
            self.events.append(("GetWindow", hwnd))
            return self.popup

        def post(hwnd, msg, wparam, lparam):
            self._maybe_raise("PostMessage")
            self.posts.append((hwnd, msg, wparam, lparam))
            self.events.append(("PostMessage", hwnd))

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds

        return {
            "IsWindow": self.is_window,
            "GetForegroundRoot": foreground,
            "IsWindowEnabled": enabled,
            "GetWindow": get_window,
            "GetClassName": get_class,
            "ReadProvenance": read_provenance,
            "PostMessage": post,
            "sleep": sleep,
            "monotonic": lambda: self.now,
        }


class TestCloseIfStillOpen:
    def test_a_window_that_ignores_alt_f4_gets_sc_close(self):
        fake = FakeWin32()
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "posted"
        assert fake.posts == [(HWND, 0x0112, 0xF060, 0)]

    def test_the_constants_are_the_windows_values(self):
        assert WM_SYSCOMMAND == 0x0112
        assert SC_CLOSE == 0xF060
        assert GW_ENABLEDPOPUP == 6

    def test_the_popup_probe_asks_for_the_enabled_popup(self):
        fake = FakeWin32()
        close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert fake.get_window_args == [(HWND, 6)]

    def test_a_save_prompt_stops_the_post(self):
        fake = FakeWin32(popup=OTHER)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "popup"
        assert fake.posts == []

    def test_no_popup_reported_as_zero_or_none_still_posts(self):
        for none_like in (0, None):
            fake = FakeWin32(popup=none_like)
            assert close_if_still_open(HWND, MARKER, ops=fake.ops()) == "posted"
            assert len(fake.posts) == 1

    def test_a_modal_dialog_stops_the_post(self):
        fake = FakeWin32(enabled=False)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "disabled"
        assert fake.posts == []

    def test_a_window_no_longer_in_front_stops_the_post(self):
        fake = FakeWin32(foreground=OTHER)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "not_in_front"
        assert fake.posts == []

    def test_no_foreground_window_stops_the_post(self):
        fake = FakeWin32(foreground=None)
        assert close_if_still_open(HWND, MARKER, ops=fake.ops()) == "not_in_front"
        assert fake.posts == []

    def test_a_shell_window_is_never_closed_and_never_waited_for(self):
        fake = FakeWin32(class_name="Progman")
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "shell"
        assert fake.posts == []
        assert fake.sleeps == []

    @pytest.mark.parametrize(
        "name", ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"]
    )
    def test_every_shell_class_is_refused(self, name):
        assert name in SHELL_WINDOW_CLASSES
        fake = FakeWin32(class_name=name)
        assert close_if_still_open(HWND, MARKER, ops=fake.ops()) == "shell"
        assert fake.posts == []

    def test_the_shell_class_set_cannot_be_changed(self):
        assert isinstance(SHELL_WINDOW_CLASSES, frozenset)

    def test_a_window_that_closes_on_the_third_poll_gets_no_post(self):
        fake = FakeWin32(closes_after_sleeps=3)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "closed"
        assert fake.posts == []
        assert fake.sleeps == [CLOSE_POLL_S] * 3

    def test_a_window_already_gone_gets_no_post(self):
        fake = FakeWin32(closes_after_sleeps=0)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "closed"
        assert fake.posts == []

    def test_the_wait_is_bounded(self):
        fake = FakeWin32()
        close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert fake.now >= CLOSE_WAIT_S
        assert fake.now <= CLOSE_WAIT_S + CLOSE_POLL_S + 1e-9
        assert 9 <= len(fake.sleeps) <= 11

    def test_the_wait_is_about_half_a_second_in_steps_of_about_50_ms(self):
        assert 0.4 <= CLOSE_WAIT_S <= 0.6
        assert 0.03 <= CLOSE_POLL_S <= 0.1

    @pytest.mark.parametrize(
        "name",
        ["IsWindow", "GetClassName", "GetForegroundRoot", "IsWindowEnabled",
         "GetWindow", "ReadProvenance", "PostMessage"],
    )
    def test_a_probe_that_raises_posts_nothing(self, name):
        fake = FakeWin32(raises_in=name)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert outcome == "probe_failed"
        if name != "PostMessage":
            assert fake.posts == []

    def test_a_posted_close_is_logged_at_info(self, caplog):
        import logging

        fake = FakeWin32()
        with caplog.at_level(logging.INFO, logger="ui.close_fallback"):
            close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert any(
            r.levelno == logging.INFO and "posted" in r.getMessage()
            for r in caplog.records
        )


class TestTheWindowObjectIsTheSameOne:
    """wh-xray-close-app-fails.1.2: a numeric handle is not an identity.

    If Alt+F4 closes window A and Windows gives A's handle to a new
    foreground window B inside one polling interval, every numeric probe
    (IsWindow, foreground root, enabled, popup) still answers for B. Only
    the window property set on A before the chord tells them apart: B
    carries none, so reading it gives 0.
    """

    def test_a_new_window_with_the_same_handle_gets_no_post(self):
        # A is gone and B, unmarked, holds the same numeric handle: every
        # numeric probe is true, and only the provenance read answers 0.
        fake = FakeWin32(provenance=0)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert fake.posts == []
        assert outcome == "identity_mismatch"

    def test_a_new_window_carrying_another_marker_gets_no_post(self):
        fake = FakeWin32(provenance=OTHER_MARKER)
        outcome = close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert fake.posts == []
        assert outcome == "identity_mismatch"

    def test_a_zero_marker_gets_no_post(self):
        # Tagging failed (0). The window reads 0 too: an unmarked window
        # would otherwise "match" the failed tag.
        fake = FakeWin32(provenance=0)
        outcome = close_if_still_open(HWND, 0, ops=fake.ops())
        assert fake.posts == []
        assert outcome == "identity_mismatch"

    def test_the_matching_marker_posts(self):
        fake = FakeWin32(provenance=MARKER)
        assert close_if_still_open(HWND, MARKER, ops=fake.ops()) == "posted"
        assert fake.posts == [(HWND, WM_SYSCOMMAND, SC_CLOSE, 0)]

    def test_the_identity_is_read_for_the_window_after_every_other_probe(self):
        fake = FakeWin32()
        close_if_still_open(HWND, MARKER, ops=fake.ops())
        assert fake.events == [
            ("GetWindow", HWND),
            ("ReadProvenance", HWND),
            ("PostMessage", HWND),
        ]

    @pytest.mark.parametrize(
        "kwargs, outcome",
        [({"foreground": OTHER}, "not_in_front"),
         ({"enabled": False}, "disabled"),
         ({"popup": OTHER}, "popup"),
         ({"closes_after_sleeps": 2}, "closed")],
        ids=["not-in-front", "disabled", "popup", "closed"],
    )
    def test_an_earlier_refusal_never_reads_the_identity(self, kwargs, outcome):
        fake = FakeWin32(**kwargs)
        assert close_if_still_open(HWND, MARKER, ops=fake.ops()) == outcome
        assert [e for e in fake.events if e[0] == "ReadProvenance"] == []


class TestStartCloseFallback:
    def test_it_runs_the_decision_on_a_named_daemon_thread(self):
        seen = {}
        done = threading.Event()

        def fake_decide(hwnd, marker):
            seen["hwnd"] = hwnd
            seen["marker"] = marker
            seen["thread"] = threading.current_thread()
            done.set()
            return "posted"

        with patch.object(close_fallback, "close_if_still_open", fake_decide):
            thread = close_fallback.start_close_fallback(HWND, MARKER)
            assert done.wait(timeout=5)
            thread.join(timeout=5)
        assert seen["hwnd"] == HWND
        assert seen["marker"] == MARKER
        assert thread.name == "close-fallback"
        assert thread.daemon is True

    # Without the containment in the thread body, the exception escapes the
    # thread and pytest's threadexception plugin fails this test on a
    # warning before it reaches the assertion below. The unmutated code
    # raises no such warning, so the marker changes nothing about what the
    # test proves (mutation-gate skill, thread-body containment rule).
    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_an_exception_in_the_decision_stays_in_the_thread(self, caplog):
        import logging

        def boom(hwnd, marker):
            raise RuntimeError("boom")

        with caplog.at_level(logging.DEBUG, logger="ui.close_fallback"), \
             patch.object(close_fallback, "close_if_still_open", boom):
            thread = close_fallback.start_close_fallback(HWND, MARKER)
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert any("thread failed" in r.getMessage() for r in caplog.records)


class TestIsCloseChord:
    @pytest.mark.parametrize("keys", [["alt", "f4"], ["F4", "Alt"], ("alt", "f4")])
    def test_alt_and_f4_in_either_order(self, keys):
        assert is_close_chord(keys) is True

    @pytest.mark.parametrize(
        "keys",
        [["alt"], ["ctrl", "f4"], ["alt", "f4", "shift"], "alt+f4", None,
         ["f4"], ["alt", "f5"], [], ["alt", "alt"]],
    )
    def test_anything_else_is_not_the_close_chord(self, keys):
        assert is_close_chord(keys) is False


# ---------------------------------------------------------------------------
# hotkey_action starts the fallback
# ---------------------------------------------------------------------------

_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """A UIActionHandler with its specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):
        from ui.ui_action_handler import UIActionHandler

        h = UIActionHandler(
            response_queue=MagicMock(),
            config={"ui_actions": {"timing": {
                "utterance_clipboard_timeout_seconds": 1.0}}},
        )
        h.terminal_editor.is_active = False
        yield h


def _context():
    from ui.context import UIContext

    return UIContext(focused_control=None, is_flutter=False,
                     is_terminal=False, process_name="", class_name="")


class _Desktop:
    """A fake desktop: the foreground window and the root of each window."""

    def __init__(self, foreground):
        self.foreground = foreground
        self.roots = {}

    def get_foreground(self):
        return self.foreground

    def root_of(self, hwnd):
        # Like hwnd_utils.normalize_hwnd_for_foreground_compare: a zero
        # or missing handle cannot be normalized and answers None.
        if not hwnd:
            return None
        return self.roots.get(hwnd, hwnd)


def _patch_handler_win32(desktop):
    """Patch the foreground read and the root normalization in ui_action_handler."""
    fake_win32gui = MagicMock()
    fake_win32gui.GetForegroundWindow.side_effect = desktop.get_foreground
    return (
        patch(f"{_MOD}.win32gui", fake_win32gui),
        patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
              side_effect=desktop.root_of),
    )


class TestHotkeyActionStartsTheFallback:
    def _run(self, handler, keys, *, repeat=1, press_result=(True, 2, 2),
             foreground=0x700, root=0x7F0, tag=MARKER):
        desktop = _Desktop(foreground)
        desktop.roots[foreground] = root
        p_gui, p_norm = _patch_handler_win32(desktop)
        with p_gui, p_norm, \
             patch(f"{_MOD}.capture_context", return_value=_context()), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=press_result) as press, \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.send_notice"), \
             patch(f"{_MOD}.tag_hwnd_provenance", return_value=tag) as tagger, \
             patch("ui.close_fallback.start_close_fallback") as start:
            handler.hotkey_action(keys, repeat=repeat)
        self.tagger = tagger
        return start, press

    def test_alt_f4_starts_the_fallback_for_the_root_of_the_foreground_window(
        self, handler
    ):
        start, press = self._run(handler, ["alt", "f4"])
        press.assert_called_once()
        start.assert_called_once_with(0x7F0, MARKER)

    def test_the_root_window_is_tagged_and_the_marker_is_passed_on(self, handler):
        start, _ = self._run(handler, ["alt", "f4"], tag=0x7777)
        self.tagger.assert_called_once_with(0x7F0)
        start.assert_called_once_with(0x7F0, 0x7777)

    def test_a_failed_tag_still_starts_the_fallback_with_zero(self, handler):
        # The fallback refuses a zero marker by itself.
        start, _ = self._run(handler, ["alt", "f4"], tag=0)
        start.assert_called_once_with(0x7F0, 0)

    def test_ctrl_a_does_not_start_it(self, handler):
        start, _ = self._run(handler, ["ctrl", "a"])
        start.assert_not_called()
        self.tagger.assert_not_called()

    def test_a_refused_alt_f4_does_not_start_it(self, handler):
        start, _ = self._run(handler, ["alt", "f4"], press_result=(False, 1, 4))
        start.assert_not_called()

    def test_a_repeated_alt_f4_does_not_start_it(self, handler):
        start, press = self._run(handler, ["alt", "f4"], repeat=2)
        assert press.call_count == 2
        start.assert_not_called()
        self.tagger.assert_not_called()

    def test_no_foreground_window_does_not_start_it(self, handler):
        start, press = self._run(handler, ["alt", "f4"], foreground=0)
        press.assert_called_once()
        start.assert_not_called()
        self.tagger.assert_not_called()

    def test_the_target_is_read_before_the_keys_are_sent(self, handler):
        """The window in front at send time is the target, not the one after."""
        desktop = _Desktop(0x700)
        p_gui, p_norm = _patch_handler_win32(desktop)

        def press(*keys, **kw):
            desktop.foreground = 0x800   # the prompt took the front
            return (True, 2, 2)

        with p_gui, p_norm, \
             patch(f"{_MOD}.capture_context", return_value=_context()), \
             patch(f"{_MOD}.verified_press_keys", side_effect=press), \
             patch(f"{_MOD}.tag_hwnd_provenance", return_value=MARKER), \
             patch("ui.close_fallback.start_close_fallback") as start:
            handler.hotkey_action(["alt", "f4"])
        start.assert_called_once_with(0x700, MARKER)

    def test_the_window_is_tagged_before_the_keys_are_sent(self, handler):
        events = []
        desktop = _Desktop(0x700)
        p_gui, p_norm = _patch_handler_win32(desktop)

        def tag(hwnd):
            events.append(("tag", hwnd))
            return MARKER

        def press(*keys, **kw):
            events.append(("press", keys))
            return (True, 2, 2)

        with p_gui, p_norm, \
             patch(f"{_MOD}.capture_context", return_value=_context()), \
             patch(f"{_MOD}.verified_press_keys", side_effect=press), \
             patch(f"{_MOD}.tag_hwnd_provenance", side_effect=tag), \
             patch("ui.close_fallback.start_close_fallback"):
            handler.hotkey_action(["alt", "f4"])
        assert events == [("tag", 0x700), ("press", ("alt", "f4"))]

    def test_a_flutter_alt_f4_starts_the_fallback_too(self, handler):
        """The Flutter branch sends through UIA SendKeys, not SendInput."""
        from ui.context import UIContext

        control = MagicMock()
        control.Exists.return_value = True
        context = UIContext(focused_control=control, is_flutter=True,
                            is_terminal=False, process_name="", class_name="")
        desktop = _Desktop(0x700)
        desktop.roots[0x700] = 0x7F0
        p_gui, p_norm = _patch_handler_win32(desktop)
        with p_gui, p_norm, \
             patch(f"{_MOD}.capture_context", return_value=context), \
             patch(f"{_MOD}.verified_press_keys") as press, \
             patch(f"{_MOD}.tag_hwnd_provenance", return_value=MARKER), \
             patch("ui.close_fallback.start_close_fallback") as start:
            handler.hotkey_action(["alt", "f4"])
        press.assert_not_called()
        control.SendKeys.assert_called_once()
        start.assert_called_once_with(0x7F0, MARKER)


class TestDefaultCloseOps:
    """Every other test passes its own ops; this pins the production ones."""

    def test_each_name_is_the_matching_win32_call(self):
        import time

        import win32gui

        from ui.hwnd_utils import read_hwnd_provenance

        ops = close_fallback.default_close_ops()
        assert ops == {
            "IsWindow": win32gui.IsWindow,
            "GetForegroundRoot": close_fallback._foreground_root,
            "IsWindowEnabled": win32gui.IsWindowEnabled,
            "GetWindow": win32gui.GetWindow,
            "GetClassName": win32gui.GetClassName,
            "ReadProvenance": read_hwnd_provenance,
            "PostMessage": win32gui.PostMessage,
            "sleep": time.sleep,
            "monotonic": time.monotonic,
        }

    def test_the_foreground_root_normalizes_the_foreground_window(self):
        with patch("ui.close_fallback.win32gui") as gui, \
             patch("ui.close_fallback.normalize_hwnd_for_foreground_compare",
                   return_value=0x7F0) as norm:
            gui.GetForegroundWindow.return_value = 0x700
            assert close_fallback._foreground_root() == 0x7F0
        norm.assert_called_once_with(0x700)


# ---------------------------------------------------------------------------
# Acceptance item 3: close-app, target in front or not
# ---------------------------------------------------------------------------

class TestCloseAppSendsAltF4ToTheAppsWindow:
    """close-app is the activate step, then Alt+F4 to the window in front.

    The activate step is the real input_proc._handle_activate_window with
    its window search and bring-forward replaced by a fake desktop; the
    key step is the real UIActionHandler.hotkey_action. The desktop
    records which window was in front when Alt+F4 was sent.
    """

    APP = 0xA11

    def _close_app(self, handler, desktop):
        import input_proc

        replies = queue.Queue()
        brought = []

        def activate(hwnd, logger):
            brought.append(hwnd)
            desktop.foreground = hwnd
            return True

        with patch.object(input_proc, "_find_window_by_target",
                          lambda t, logger: self.APP), \
             patch.object(input_proc, "_activate_window_impl", activate), \
             patch.object(input_proc, "_target_is_foreground",
                          lambda t, logger: desktop.foreground == self.APP):
            thread = input_proc._handle_activate_window(
                {"target": "notepad"}, "r1", replies, "activate_window",
                threading.Event(), 50, 10, {}, MagicMock(),
            )
            if thread is not None:
                thread.join(timeout=10)
        reply = replies.get(timeout=5)
        assert reply["status"] == "ok", reply

        sent_to = []

        def press(*keys, **kw):
            sent_to.append((list(keys), desktop.foreground))
            return (True, 4, 4)

        p_gui, p_norm = _patch_handler_win32(desktop)
        with p_gui, p_norm, \
             patch(f"{_MOD}.capture_context", return_value=_context()), \
             patch(f"{_MOD}.verified_press_keys", side_effect=press), \
             patch(f"{_MOD}.tag_hwnd_provenance", return_value=MARKER), \
             patch("ui.close_fallback.start_close_fallback") as start:
            handler.hotkey_action(["alt", "f4"])
        return brought, sent_to, start

    def test_the_app_already_in_front_gets_alt_f4_and_the_fallback(self, handler):
        desktop = _Desktop(foreground=self.APP)
        brought, sent_to, start = self._close_app(handler, desktop)
        assert sent_to == [(["alt", "f4"], self.APP)]
        start.assert_called_once_with(self.APP, MARKER)

    def test_the_app_behind_another_window_gets_alt_f4_and_the_fallback(
        self, handler
    ):
        desktop = _Desktop(foreground=0xBEEF)   # another window is in front
        brought, sent_to, start = self._close_app(handler, desktop)
        assert brought == [self.APP]
        assert sent_to == [(["alt", "f4"], self.APP)]
        assert sent_to[0][1] != 0xBEEF
        start.assert_called_once_with(self.APP, MARKER)
