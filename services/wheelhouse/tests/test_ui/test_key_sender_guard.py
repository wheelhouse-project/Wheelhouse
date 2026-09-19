"""The guard that keeps tests under tests/test_ui/ off the real keyboard.

wh-test-ui-key-sender-guard, David's QUESTIONS-2026-09-04 item 14. Twenty-
two tests in test_ui_action_handler.py replace only the name the handler
calls, for example ``ui.ui_action_handler.press_keys``. A refactor or a
mutation that restores the direct call runs the real sender, and
``user32.SendInput`` types into whatever window has focus. It happened once
during a full-file run.

These tests pin the guard itself, so they must never send a key event
either. Each one either stops at the guard or points the guard at a stand-in
for Windows. The real ``ctypes.windll.user32.SendInput`` is never called
from this file.

Why the guard sits at ``win_input_sender.user32`` and not at the sender
function names: two import styles reach the same functions, and only one of
them can be patched by name. ``ui/clipboard_operations.py`` line 26 and
``ui/ui_action_handler.py`` line 37 bind the functions at module import
(``from utils.win_input_sender import verified_press_keys``), so replacing
``utils.win_input_sender.verified_press_keys`` afterwards never reaches
them -- that already-bound name still points at the real function. The
module global ``user32`` is read fresh inside every sender on every call, so
replacing it is the one seam no import style can get past. The codebase
already uses this seam by hand: test_ui_action_handler.py line 4139 does
``patch("utils.win_input_sender.user32")`` and its class docstring says the
typing test "replaces user32 outright".
"""

import ctypes

from utils import win_input_sender


class _WindowsStandIn:
    """Stands where the real Windows library stands, and counts.

    A test that wants to show a call REACHING the boundary cannot let the
    real SendInput run, because that is the keystroke the guard exists to
    stop. It puts this object where the real library sits instead, so the
    call arrives somewhere it can be counted and goes no further.
    """

    def __init__(self):
        self.send_input_calls = []

    def __getattr__(self, name):
        raise AssertionError(
            f"the stand-in was asked for user32.{name}, which this test "
            "does not expect to be reached"
        )

    def SendInput(self, count, events, size):
        self.send_input_calls.append(count)
        return count


class TestTheGuardIsInstalled:
    """The conftest fixture runs for every test in this directory."""

    def test_the_real_windows_library_is_unreachable_from_this_directory(self):
        """The one assertion that fails if the conftest is deleted.

        ``ctypes.windll`` holds one library object per library for the whole
        process, so ``ctypes.windll.user32`` is the same object the module
        binds at import. If the module global is still that object, nothing
        stands between a sender and the focused window.
        """
        assert win_input_sender.user32 is not ctypes.windll.user32

    def test_the_guard_reports_what_it_recorded(self, key_sender_guard):
        """The fixture hands the test the guard that is already installed.

        It is the same object the module is using, not a second copy; a
        second copy would record calls nothing made.
        """
        assert key_sender_guard is win_input_sender.user32
        assert key_sender_guard.calls == []


class TestTheGuardStopsWhatItRecords:
    """The guard records the call AND stops it. Both halves are needed.

    A guard that records but still calls Windows would pass a test that only
    checked the record. A guard that stops but records nothing would leave a
    test unable to prove the sender ran at all.
    """

    def test_a_key_press_is_recorded(self, key_sender_guard):
        win_input_sender.press_keys("ctrl", "s")

        assert len(key_sender_guard.calls) == 1
        events = key_sender_guard.calls[0]
        # Two keys down, two keys up, in press-then-release order.
        assert [e["kind"] for e in events] == ["key"] * 4
        assert [e["vk"] for e in events] == [0x11, 0x53, 0x53, 0x11]

    def test_the_same_key_press_never_reaches_windows(
        self, key_sender_guard, monkeypatch
    ):
        """The paired half: the call stops at the guard.

        The stand-in takes the place of the real library INSIDE the guard,
        so this test proves where the call stopped without ever letting a
        key event out. With the guard removed the stand-in is called once;
        with the guard in place it is not called at all. That second half is
        what fails if the conftest is deleted.

        The guard class comes from the installed guard rather than from an
        import of conftest, because a conftest is not importable by name.
        """
        guard_class = type(key_sender_guard)
        windows = _WindowsStandIn()

        # Guard removed: the sender reaches the boundary. This is the call
        # that would have typed into the focused window.
        monkeypatch.setattr(win_input_sender, "user32", windows)
        win_input_sender.press_keys("a")
        assert windows.send_input_calls == [2]

        # Guard back, wrapping the same stand-in: the sender is recorded and
        # the boundary is not reached a second time.
        replacement = guard_class(windows)
        monkeypatch.setattr(win_input_sender, "user32", replacement)
        win_input_sender.press_keys("a")
        assert windows.send_input_calls == [2]
        assert len(replacement.calls) == 1

    def test_typed_text_is_recorded_and_stopped(self, key_sender_guard):
        """Text takes a different route to the same boundary.

        ``type_string`` builds KEYEVENTF_UNICODE events rather than virtual
        key codes, so a guard placed on the key path alone would miss it.
        """
        win_input_sender.type_string("hi")

        sent = [e for call in key_sender_guard.calls for e in call]
        assert sent, "type_string reached no SendInput call at all"
        assert all(e["flags"] & win_input_sender.KEYEVENTF_UNICODE for e in sent)
        assert [e["scan"] for e in sent if not e["flags"] & 0x0002] == [
            ord("h"),
            ord("i"),
        ]


class TestTheGuardCoversNamesBoundAtImport:
    """The case the bead is about, and the reason for the chosen seam."""

    def test_a_caller_that_imported_the_function_is_still_covered(
        self, key_sender_guard
    ):
        """``ui/clipboard_operations.py`` line 26 binds the name at import.

        Replacing ``utils.win_input_sender.verified_press_keys`` cannot
        reach this call: the module already holds the real function. The
        guard reaches it because the real function reads the module global
        ``user32`` on every call.
        """
        from ui import clipboard_operations

        assert (
            clipboard_operations.verified_press_keys
            is win_input_sender.verified_press_keys
        ), "the real function, bound at import -- not a patched name"

        clipboard_operations.verified_press_keys("ctrl", "c")

        assert len(key_sender_guard.calls) == 1
        assert [e["vk"] for e in key_sender_guard.calls[0]] == [
            0x11,
            0x43,
            0x43,
            0x11,
        ]
