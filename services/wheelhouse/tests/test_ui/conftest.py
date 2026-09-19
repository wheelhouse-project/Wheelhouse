"""No test in this directory reaches the real Windows input queue.

wh-test-ui-key-sender-guard, David's QUESTIONS-2026-09-04 item 14. Twenty-
two tests in test_ui_action_handler.py replace only the name the handler
calls, for example ``ui.ui_action_handler.press_keys``. A refactor or a
mutation that restores the direct call runs the real sender, and
``user32.SendInput`` types into whatever window has focus. It happened once
during a full-file run of that file. Nothing blocked SendInput before this.

WHERE THE GUARD SITS, AND WHY NOT ON THE FUNCTION NAMES.
``utils/win_input_sender.py`` has six functions that call SendInput for the
keyboard (``press_keys`` 405, ``verified_press_keys`` 519,
``_send_modifier_keyups`` 589, ``send_backspaces`` 629,
``type_string_verified`` 748, ``type_string`` 839) and four call sites for
the mouse (``click_at`` 1023, 1058, 1087 and ``_send_mouse_events`` 1220,
which ``click_point``, ``move_pointer_to``, ``scroll_wheel``,
``drag_pointer`` and the cursor helpers all go through). Replacing those
names on the module cannot protect the callers this bead is about: four
production modules bind the functions at import time --
``coordinators/brightness_coordinator.py`` line 180,
``ui/clipboard_operations.py`` line 26, ``ui/strategies/specific.py`` line
86, ``ui/ui_action_handler.py`` line 37 -- so their bound name still points
at the real function no matter what the module attribute is set to
afterwards.

Every one of those senders reads the module global ``user32`` fresh on each
call. Replacing that global is therefore the one seam no import style can
get past, and it covers the mouse callers in the same stroke. The codebase
already uses this seam by hand: test_ui_action_handler.py line 4139 does
``patch("utils.win_input_sender.user32")``, and its class docstring says
the typing test "replaces user32 outright".

WHAT THE GUARD DOES NOT DO. It does not change any sender's behaviour. The
real event-building, key-name refusal, chunking and verification code all
still run, and the recorder returns the requested event count, which is what
Windows returns on full acceptance. A test that wants a short-send failure
still gets it by patching ``user32`` itself, the way the existing test at
line 4139 does; an inner patch replaces the guard for its own duration and
pytest restores the guard afterwards.
"""

import ctypes

import pytest

from utils import win_input_sender


def _decode(count: int, events) -> list[dict]:
    """Read the INPUT array a sender handed to SendInput.

    ``events`` arrives as ``ctypes.byref(array)``, whose ``_obj`` is the
    array itself. A plain array is accepted too, so a caller that stops
    using ``byref`` does not silently record nothing.
    """
    array = getattr(events, "_obj", events)
    decoded = []
    for index in range(count):
        item = array[index]
        if item.type == win_input_sender.INPUT_KEYBOARD:
            decoded.append(
                {
                    "kind": "key",
                    "vk": item.ii.ki.wVk,
                    "scan": item.ii.ki.wScan,
                    "flags": item.ii.ki.dwFlags,
                }
            )
        else:
            decoded.append(
                {
                    "kind": "mouse",
                    "dx": item.ii.mi.dx,
                    "dy": item.ii.mi.dy,
                    "data": item.ii.mi.mouseData,
                    "flags": item.ii.mi.dwFlags,
                }
            )
    return decoded


class RecordingUser32:
    """Stands in for ``ctypes.windll.user32`` and refuses SendInput only.

    Every other name -- ``GetAsyncKeyState``, ``SetCursorPos``,
    ``GetCursorPos``, ``WindowFromPoint`` and the rest -- is delegated to
    the real library, because the senders read cursor and modifier state
    around their SendInput calls and a guard that broke those reads would
    change what the tests are measuring.
    """

    def __init__(self, real):
        self.real = real
        self.calls: list[list[dict]] = []

    def __getattr__(self, name):
        # Reached only for names not in the instance dictionary, so `real`
        # and `calls` never come through here.
        return getattr(self.real, name)

    def SendInput(self, count, events, size):
        self.calls.append(_decode(count, events))
        # The count Windows returns when it accepts every event. Returning
        # anything less makes every sender report a short send and log an
        # error, which would fail tests that assert a clean run.
        return count


@pytest.fixture(autouse=True)
def key_sender_guard(monkeypatch):
    """Install the guard for every test in this directory.

    Autouse, so a test does not have to ask for it. A test that wants to
    read what was recorded names the fixture and gets the same object the
    module is using, not a second copy.
    """
    guard = RecordingUser32(ctypes.windll.user32)
    monkeypatch.setattr(win_input_sender, "user32", guard)
    return guard
