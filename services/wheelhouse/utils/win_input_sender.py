"""Low-level Windows input synthesis using SendInput API.

This module provides direct access to Windows' SendInput API for precise
keyboard and mouse input synthesis. It implements proper input event
structures and handles the complex mapping between high-level input
descriptions and low-level Windows input events.

Key Functions:
  - press_keys: Synthesizes keyboard input events including complex hotkeys.
  - type_string: Outputs text through keyboard input simulation.
  - Various helper functions for input event creation and mapping.

Key Features:
  - Direct SendInput API access for maximum precision and reliability
  - Support for complex hotkey combinations (Ctrl+Alt+Shift+Key)
  - Unicode text input via keyboard simulation
  - Proper timing and synchronization for input events
  - Virtual key code mapping for all keyboard keys
  - Error handling and logging for input failures

Technical Implementation:
  - Uses ctypes to interface with Windows SendInput API
  - Implements proper INPUT structure definitions
  - Handles virtual key code translation and mapping
  - Supports both key press and release event synthesis
  - Manages input event sequencing and timing

Input Event Types:
  - Keyboard input with virtual key codes
  - Unicode character input for international text
  - Key combination handling (modifiers + keys)
  - Sequential input event processing

Typical Usage:
  from utils.win_input_sender import press_keys, type_string

  # Send a hotkey. One key name per argument.
  press_keys("ctrl", "c")    # Copy
  press_keys("alt", "tab")   # Switch window

  # Type text directly
  type_string("Hello world!")

  # More modifiers, same shape
  press_keys("ctrl", "shift", "n")  # New window or incognito window

  A COMBINED STRING DOES NOT WORK. press_keys("ctrl+c") passes one argument,
  "ctrl+c", which is not a key name in VK_CODE_MAP. press_keys logs
  "One or more keys ... are not valid. Aborting." and returns without
  sending anything, and the caller is told nothing. Every call site in this
  repository uses one key name per argument.
"""
# utils/win_input_sender.py
import ctypes
from ctypes import wintypes
import logging
import time

logger = logging.getLogger(__name__)

# Ctypes structures for SendInput
PUL = ctypes.POINTER(ctypes.c_ulong)
class KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", PUL)]

class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]

class MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", PUL)]

class Input_I(ctypes.Union):
    _fields_ = [("ki", KeyBdInput),
                ("mi", MouseInput),
                ("hi", HardwareInput)]

class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD),
                ("ii", Input_I)]

# Constants
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Virtual key codes that Windows treats as EXTENDED keys
# (wh-arrow-keys-missing-extended-flag).
#
# The original IBM keyboard had one set of scan codes. The extended keyboard
# added a second navigation cluster, a second control key, a second alt key
# and the numpad divide, and gave each of them the same scan code as its
# older twin with an 0xE0 prefix in front. KEYEVENTF_EXTENDEDKEY is how
# SendInput asks for that prefix. Without it, SendInput derives the plain
# scan code from the virtual key code, which for the navigation cluster is
# the NUMPAD scan code.
#
# An application that reads the virtual key code cannot tell the difference.
# Anything that reads the scan code sees a numpad key. Measured 2026-08-15
# with NVDA 2026.1.1: NVDA named the four arrow keys numpad2, numpad8,
# numpad4 and numpad6, matched them against its own review cursor commands,
# and consumed all four, so the arrow key never reached the application.
# About 150 filed Voice Access parity commands send these keys.
#
# The set holds virtual key codes rather than key names for two reasons.
# First, 'delete' and 'del' both mean 0x2E, and one entry covers both.
# Second, four of these codes have no name in VK_CODE_MAP yet, and
# wh-voice-access-parity.2.17 will add the numpad names. Keying on the code
# means the numpad divide is already right on the day its name arrives, and
# the numpad digits, which are NOT extended keys, stay right as well.
#
# That second reason covers only the codes this set already holds. It is NOT
# a promise about every extended key. The menu key VK_APPS 0x5D and the
# media and browser keys 0xA6 through 0xB1 are extended keys that are absent
# here, so adding a name for any of them without adding its code would send
# it without the prefix. Whoever adds such a name adds the code too.
#
# crewcut: the numpad enter cannot be expressed at all. Windows gives it the
# same virtual key code as the main enter, 0x0D, and separates the two only
# by this flag. A future 'numpadenter' name therefore needs a decision that
# reads the NAME, because the code alone cannot answer it. Adding 0x0D to
# this set would wrongly extend every main enter.
EXTENDED_VK_CODES = frozenset({
    0x21,  # VK_PRIOR, page up
    0x22,  # VK_NEXT, page down
    0x23,  # VK_END
    0x24,  # VK_HOME
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
    0x2C,  # VK_SNAPSHOT, print screen
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0x6F,  # VK_DIVIDE, the numpad slash
    0x90,  # VK_NUMLOCK
    0xA3,  # VK_RCONTROL, the right control key
    0xA5,  # VK_RMENU, the right alt key
})


def _extended_flag(vk_code: int) -> int:
    """Return KEYEVENTF_EXTENDEDKEY for an extended key, otherwise 0.

    Every key-up must carry the same flag as its key-down. A key-up that
    drops the flag does not match the key-down that carried it, and a
    reader of scan codes can then hold the key down for ever.
    """
    return KEYEVENTF_EXTENDEDKEY if vk_code in EXTENDED_VK_CODES else 0

# Mouse SendInput constants (wh-l4h.1 coordinate-click fallback seam).
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
# Mouse wheel (wh-voice-access-parity.2.3). MOUSEEVENTF_WHEEL turns the
# vertical wheel, MOUSEEVENTF_HWHEEL the horizontal one. Neither carries its
# distance in dx/dy: the distance goes in mouseData, and dx/dy are ignored.
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000

# One wheel notch, the unit every wheel-aware application is written against.
# Windows defines WHEEL_DELTA as 120 so a finer wheel can report fractions of
# a notch; Wheelhouse sends whole notches only.
WHEEL_DELTA = 120

# Upper bound on the notches one spoken scroll command may send. The Input
# process command loop is single-threaded, so an unbounded count from a
# malformed message would hold up every other input action while it drains.
# Fifty notches is far past any useful single command and matches the repeat
# cap the hotkey action already applies.
MAX_SCROLL_CLICKS = 50

# GetSystemMetrics indices for the bounding box of the VIRTUAL desktop (the
# union of all monitors). SendInput's ABSOLUTE+VIRTUALDESK coordinates are
# normalized over this box, not over the primary monitor.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Cursor-landing tolerance for the verified coordinate click (physical px,
# per axis). GetCursorPos after the normalized MOVE must land within this of
# the requested point or the click fails closed (no buttons synthesised).
_CLICK_CURSOR_TOLERANCE_PX = 2

# Bounded retry for the post-MOVE cursor verify (wh-9f3t.75.1). SendInput
# posts the MOVE into the system input queue asynchronously, so the first
# GetCursorPos can win the race against the queue drain and read the stale
# pre-move position -- which would spuriously fail closed and silently void a
# legitimate click. Poll up to this many times with a sub-millisecond yield
# and accept the first read within tolerance. The fail-closed semantics are
# unchanged: if no read lands within tolerance, the function still returns
# (False, 0) with no button event synthesised. The worst-case added latency is
# (_CLICK_CURSOR_VERIFY_ATTEMPTS - 1) * _CLICK_CURSOR_VERIFY_DELAY_S, and it is
# paid only on the opt-in coordinate-click fallback path, never on a normal
# Invoke click.
_CLICK_CURSOR_VERIFY_ATTEMPTS = 5
_CLICK_CURSOR_VERIFY_DELAY_S = 0.002

# Outer retry for the whole move+verify+click (wh-click-mouse-contention). When
# the physical mouse is being moved, its motion overrides the injected absolute
# MOVE for the entire verify window, so a single attempt reads the physical
# position and fails closed even though the target and normalization are
# correct. click_at suppresses physical input with BlockInput for the brief
# move+verify+click, and retries the whole block up to this many times as a
# backup: BlockInput is best-effort (silently ignored under a low-level input
# hook or the secure desktop), and the retry rides out a transient miss. The
# fail-closed contract is unchanged; the worst-case added latency (only on the
# opt-in coordinate-click fallback path) is
# (_CLICK_MOVE_ATTEMPTS - 1) * (_CLICK_MOVE_RETRY_DELAY_S + inner verify time).
_CLICK_MOVE_ATTEMPTS = 3
_CLICK_MOVE_RETRY_DELAY_S = 0.01

# Virtual Key Code Map
VK_CODE_MAP = {
    'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D, 'shift': 0x10,
    'ctrl': 0x11, 'alt': 0x12, 'pause': 0x13, 'capslock': 0x14,
    'esc': 0x1B, 'space': 0x20, 'pageup': 0x21, 'pagedown': 0x22,
    'end': 0x23, 'home': 0x24, 'left': 0x25, 'up': 0x26,
    'right': 0x27, 'down': 0x28, 'printscreen': 0x2C, 'insert': 0x2D,
    'delete': 0x2E, 'del': 0x2E,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
    'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45,
    'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A,
    'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F,
    'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54,
    'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A,
    'win': 0x5B, 'lwin': 0x5B,
    '=': 0xBB, '+': 0xBB,  # OEM_PLUS
    '-': 0xBD, '_': 0xBD,  # OEM_MINUS
    ';': 0xBA, ':': 0xBA,  # OEM_1
    '/': 0xBF, '?': 0xBF,  # OEM_2
    '`': 0xC0, '~': 0xC0,  # OEM_3
    '[': 0xDB, '{': 0xDB,  # OEM_4
    '\\': 0xDC, '|': 0xDC,  # OEM_5
    ']': 0xDD, '}': 0xDD,  # OEM_6
    "'": 0xDE, '"': 0xDE,  # OEM_7
    ',': 0xBC, '<': 0xBC,  # OEM_COMMA
    '.': 0xBE, '>': 0xBE,  # OEM_PERIOD
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74,
    'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79,
    'f11': 0x7A, 'f12': 0x7B,
}

# Virtual key codes that a chord HOLDS DOWN while it presses the other keys
# (wh-arrow-keys-missing-extended-flag, the second gap).
#
# _build_press_keys_events used to test four names inline. Any key outside
# those four was tapped instead of held, so press_keys('insert', 't') sent
# insert down, insert up, t down, t up. That is a sequence, not a chord, and
# NVDA reported it as plain t. NVDA accepts Insert or Caps Lock as its own
# modifier, so no NVDA chord could be expressed at all.
#
# The set holds codes rather than names because 'win' and 'lwin' are both
# 0x5B, and one entry covers both.
#
# A key is in this set only when an application reads it as a modifier.
# Holding every key except the last would be simpler and wrong: it would
# turn press_keys('a', 'b') into a chord and stop it typing 'ab'.
# Measured against the 63 unique key lists in speech/config/patterns.toml:
# every one of them is modifiers plus a single final key, and none holds
# insert or capslock, so no shipped command changes shape.
#
# HAZARD FOR WHOEVER WRITES THE NEXT CAPS LOCK OR INSERT COMMAND. Both keys
# are toggles, and the toggle fires on the key-DOWN. An injected Caps Lock
# key-down flips the operating system caps state and the keyboard light. An
# injected Insert key-down flips overwrite mode in an edit control that
# honours it, and overwrite mode is per control, so nothing can read it back.
# Neither key-up undoes its key-down. A chord holding either key therefore
# fires its toggle once per command, and after the command the machine is in
# a different state than it started in.
#
# A screen reader that claims the key as its own modifier consumes the event
# and hides both toggles, which is why the 2026-08-15 NVDA session measured
# no Caps Lock flip. Nothing here checks that such a consumer exists. With no
# screen reader running, a caps lock command leaves caps on, and an insert
# command leaves the focused editor overwriting the text already in it.
#
# This is not new. Before these keys joined the set they were tapped, which
# is the same single key-down, so the toggle fired just as often. What
# changed is that the chord now works, so writing one is now worth doing.
# Whether Wheelhouse should refuse these chords, or restore the caps state
# around them, is an open question for the project owner and is not settled
# here.
HELD_MODIFIER_VK_CODES = frozenset({
    VK_CODE_MAP['ctrl'],
    VK_CODE_MAP['shift'],
    VK_CODE_MAP['alt'],
    VK_CODE_MAP['win'],
    VK_CODE_MAP['insert'],    # the NVDA modifier, desktop layout
    VK_CODE_MAP['capslock'],  # the NVDA modifier, laptop layout
})

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Diagnostic helper -- wh-trailing-corruption-instrument.
# GetAsyncKeyState VK codes used by snapshot_modifier_state. The values
# duplicate entries in VK_CODE_MAP intentionally: the diagnostic snapshot
# must not depend on the dictionary key (`'shift'`, `'ctrl'`, ...) being
# present and named consistently, since the map is consumed by the typing
# path and could be renamed without anyone noticing the snapshot break.
_SNAPSHOT_VK_SHIFT = 0x10
_SNAPSHOT_VK_CONTROL = 0x11
_SNAPSHOT_VK_MENU = 0x12  # Alt
_SNAPSHOT_VK_LWIN = 0x5B
_SNAPSHOT_VK_CAPITAL = 0x14


def snapshot_modifier_state() -> str:
    """Capture the current Windows async-key state of the dictation modifiers.

    Diagnostic helper for the wh-startup-trailing-corruption investigation
    (wh-trailing-corruption-instrument). One hypothesis for the bug is a
    stale modifier (SHIFT, CTRL, ALT, LWIN, or CAPSLOCK) leaking into
    SendInput's keyboard state on the first dictation after WheelHouse
    starts. VerifiedUnicodeStrategy logs this string with every dispatch
    so the next reproduction has the cold-keyboard-state evidence
    inline.

    The high bit of GetAsyncKeyState's return value is set when the key
    is currently down; the low bit is set when the key was pressed since
    the last GetAsyncKeyState call. Both are surfaced: a recently-pressed
    key whose down has already lifted is also useful evidence.

    Returns a compact string like
    ``"shift=- ctrl=- alt=down lwin=- caps=recent"``. ``-`` means up and
    not recently pressed; ``down`` means currently held; ``recent`` means
    pressed since the last call but not currently held. ``?`` means the
    Win32 call raised -- the helper is defensive so a diagnostic log line
    cannot crash the dispatch path.
    """
    def _state(vk: int) -> str:
        try:
            raw = user32.GetAsyncKeyState(vk)
        except Exception:
            return "?"
        v = int(raw) & 0xFFFF
        if v & 0x8000:
            return "down"
        if v & 0x0001:
            return "recent"
        return "-"

    return (
        f"shift={_state(_SNAPSHOT_VK_SHIFT)} "
        f"ctrl={_state(_SNAPSHOT_VK_CONTROL)} "
        f"alt={_state(_SNAPSHOT_VK_MENU)} "
        f"lwin={_state(_SNAPSHOT_VK_LWIN)} "
        f"caps={_state(_SNAPSHOT_VK_CAPITAL)}"
    )


def press_keys(*keys: str):
    """
    :flow: UI Action Execution
    :step: 2a
    :produces_for: Windows Input System
    :description: Sends keyboard hotkey sequences to Windows via low-level SendInput API.
    :data_in: Variable args of key names (e.g., "ctrl", "c", "3").
    :data_out: Win32 INPUT structures sent to Windows input queue via SendInput().
    :notes: Handles modifier key ordering (press modifiers first, release last in reverse).
        Translates key names to virtual key codes via VK_CODE_MAP. Supports repeat counts
        from actions.py hotkey() function. Uses SendInput for reliable low-level input
        that bypasses application-level hooks. Critical for automation reliability.

    Simulates pressing and releasing a sequence of keys using the low-level
    SendInput API. Handles modifier keys (shift, ctrl, alt, win) correctly.

    Returns:
        None. Partial delivery is logged but not surfaced. Callers that need
        to fail closed on a short SendInput count (e.g. the GUI terminal
        paste helper at ``utils.gui_terminal_paste``) MUST use
        :func:`verified_press_keys` instead (wh-eolas.1.2).
    """
    if not keys:
        return

    try:
        events, num_events = _build_press_keys_events(keys)
    except _InvalidKeyError:
        return
    if num_events == 0:
        return

    try:
        input_array = (Input * num_events)(*events)
        events_sent = user32.SendInput(num_events, ctypes.byref(input_array), ctypes.sizeof(Input))
        if events_sent != num_events:
            logger.error(f"SendInput failed. Sent {events_sent}/{num_events} events. Win32 Error: {kernel32.GetLastError()}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in press_keys: {e}", exc_info=True)


class _InvalidKeyError(Exception):
    """Raised by :func:`_build_press_keys_events` when a key is not mapped."""


def _build_press_keys_events(
    keys: tuple[str, ...], *, refusal_level: int = logging.ERROR
) -> tuple[list, int]:
    """Build the INPUT event sequence for a press_keys chord.

    Shared between :func:`press_keys` (fire-and-forget) and
    :func:`verified_press_keys` (returns accepted-event count) so both
    entry points use identical modifier ordering.

    Returns:
        ``(events, num_events)``. May return an empty list when ``keys``
        contains only modifiers that are also non-modifiers (impossible
        in practice but cheap to guard).

    Args:
        refusal_level: the level the unmapped-key record is written at.
            ERROR by default, which is what :func:`press_keys` keeps.
            :func:`verified_press_keys` lowers it to WARNING when its
            caller passes ``caller_notifies`` (wh-keyboard-refusal-notice):
            an ERROR record shows the generic ``[ERROR]`` box beside the
            caller's own notice, and two boxes for one refusal is the
            duplicate this bead removes.

    Raises:
        _InvalidKeyError: when any element of ``keys`` is not in
        :data:`VK_CODE_MAP`. Callers translate this to the fire-and-forget
        no-op or the verified failure tuple as appropriate.
    """
    vk_codes = [VK_CODE_MAP.get(key.lower()) for key in keys]
    if None in vk_codes:
        logger.log(
            refusal_level,
            f"One or more keys in {keys} are not valid. Aborting.",
        )
        raise _InvalidKeyError(keys)

    events: list = []
    modifiers_down: list = []
    for vk_code in vk_codes:
        if vk_code in HELD_MODIFIER_VK_CODES:
            extended = _extended_flag(vk_code)
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=extended))))
            modifiers_down.append(vk_code)
    for vk_code in vk_codes:
        if vk_code not in modifiers_down:
            extended = _extended_flag(vk_code)
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=extended))))
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=extended | KEYEVENTF_KEYUP))))
    for vk_code in reversed(modifiers_down):
        extended = _extended_flag(vk_code)
        events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=extended | KEYEVENTF_KEYUP))))
    return events, len(events)


def verified_press_keys(
    *keys: str, caller_notifies: bool = False
) -> tuple[bool, int, int]:
    """Send a key chord and report whether SendInput accepted every event.

    Added for wh-eolas.1.2: the GUI terminal-paste helper cannot rely on
    :func:`press_keys` because that function discards the SendInput count
    and only logs short delivery. A partially delivered Ctrl+V followed
    by Enter is unsafe in a shell -- the helper must observe the count
    and fail closed.

    Returns:
        Tuple ``(success, accepted, expected)``:

        * ``success`` -- True iff every event was accepted by SendInput
          AND no exception was raised.
        * ``accepted`` -- the SendInput return value (number of accepted
          events). 0 on exception or unmapped key (no SendInput attempt
          was made).
        * ``expected`` -- the number of events the chord would have
          produced. 0 when ``keys`` is empty or contained an unmapped
          key (the function returns early before building events). This
          lets the caller distinguish "no work requested" from "tried
          to send N, only M landed".

    Args:
        caller_notifies: pass True when the caller shows the user its own
            notice for a refusal. Every refusal record below then logs at
            WARNING instead of ERROR, so the refusal keeps its full detail
            in the log while ``ErrorNotificationHandler`` stops showing the
            generic ``[ERROR]`` box beside that notice
            (wh-keyboard-refusal-notice, following wh-wheel-refusal-notice.1.7).
            Default False leaves every existing call site exactly as it was.
            :func:`press_keys` is not affected either way.
    """
    refusal_level = logging.WARNING if caller_notifies else logging.ERROR
    if not keys:
        return True, 0, 0
    try:
        events, num_events = _build_press_keys_events(
            keys, refusal_level=refusal_level
        )
    except _InvalidKeyError:
        return False, 0, 0
    if num_events == 0:
        return True, 0, 0

    try:
        input_array = (Input * num_events)(*events)
        events_sent = user32.SendInput(num_events, ctypes.byref(input_array), ctypes.sizeof(Input))
    except Exception as exc:
        logger.log(
            refusal_level,
            "verified_press_keys: SendInput raised for keys=%s: %s",
            keys, exc, exc_info=True,
        )
        return False, 0, num_events

    accepted = int(events_sent or 0)
    if accepted != num_events:
        logger.log(
            refusal_level,
            "verified_press_keys: short SendInput for keys=%s: "
            "sent %d/%d events; Win32 error %s",
            keys, accepted, num_events, kernel32.GetLastError(),
        )
        return False, accepted, num_events
    return True, accepted, num_events


def _send_modifier_keyups(keys: tuple[str, ...]) -> None:
    """Send KEYUP events for every key in ``keys``, in reverse order.

    Recovery helper for wh-eolas.2.5: when a verified_press_keys chord
    short-delivers (Ctrl-down accepted, Ctrl-up dropped), the modifier
    stays physically held in the Windows keyboard state and every
    subsequent keystroke executes with it active. The caller invokes
    this helper before returning the SENDINPUT_PARTIAL outcome so the
    modifier is released even if SendInput only accepted part of the
    chord.

    Reverse-order release matches the press order used by
    :func:`_build_press_keys_events` (modifiers down first, modifiers
    up last in reverse) so the keyboard state observed by Windows is
    symmetric. Unmapped keys are skipped silently -- the helper is a
    best-effort recovery path; raising would mask the original
    SENDINPUT_PARTIAL outcome the caller is about to surface.

    Used by the GUI terminal-paste helper (``utils.gui_terminal_paste``)
    on both the Ctrl+V abort path (releases ctrl) and the Enter abort
    path (releases enter, since a stuck Enter-down is also undesirable
    even though Enter is not a modifier).
    """
    if not keys:
        return
    events: list = []
    for key in reversed(keys):
        vk_code = VK_CODE_MAP.get(key.lower())
        if vk_code is None:
            logger.warning(
                "_send_modifier_keyups: unmapped key %r; skipping", key,
            )
            continue
        events.append(
            Input(
                type=INPUT_KEYBOARD,
                ii=Input_I(
                    ki=KeyBdInput(
                        wVk=vk_code,
                        dwFlags=_extended_flag(vk_code) | KEYEVENTF_KEYUP,
                    ),
                ),
            )
        )
    if not events:
        return
    num_events = len(events)
    try:
        input_array = (Input * num_events)(*events)
        events_sent = user32.SendInput(
            num_events, ctypes.byref(input_array), ctypes.sizeof(Input),
        )
        if events_sent != num_events:
            logger.error(
                "_send_modifier_keyups: short SendInput for keys=%s: "
                "sent %d/%d; Win32 error %s",
                keys, events_sent, num_events, kernel32.GetLastError(),
            )
    except Exception as exc:
        logger.error(
            "_send_modifier_keyups: SendInput raised for keys=%s: %s",
            keys, exc, exc_info=True,
        )


def send_backspaces(count: int) -> bool:
    """Send N backspace key-down/up pairs in a single SendInput batch.

    Sending all events atomically avoids dropped keystrokes that occur
    when calling press_keys("backspace") in a tight loop.

    Returns:
        True if every key event was accepted by SendInput. False if
        SendInput reported partial delivery (or zero) so callers --
        notably retract() in ui_action_handler.py -- can refuse to claim
        success when only some of the requested backspaces actually fired
        (wh-t81d9.1). When count <= 0 this is a no-op and returns True;
        nothing was requested, nothing failed.
    """
    if count <= 0:
        return True
    vk_backspace = VK_CODE_MAP['backspace']
    events = []
    for _ in range(count):
        events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_backspace, dwFlags=0))))
        events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_backspace, dwFlags=KEYEVENTF_KEYUP))))

    num_events = len(events)
    input_array = (Input * num_events)(*events)
    events_sent = user32.SendInput(num_events, ctypes.byref(input_array), ctypes.sizeof(Input))
    if events_sent != num_events:
        logger.error(f"send_backspaces: SendInput sent {events_sent}/{num_events}. Win32 Error: {kernel32.GetLastError()}")
        return False
    return True


def _build_unicode_event_groups(text: str) -> list[list]:
    """Build SendInput INPUT events grouped by Python character.

    Returns one inner list per character. Newline and tab use VK codes
    (one down/up pair, 2 events). BMP characters use KEYEVENTF_UNICODE
    with their UTF-16 code unit (2 events). Non-BMP characters split
    into a UTF-16 surrogate pair (4 events: high surrogate down/up
    plus low surrogate down/up). Grouping by Python character lets
    ``type_string_verified`` count whole characters delivered instead
    of raw events, and refuse to claim a non-BMP character whose low
    surrogate did not land (wh-3pw8.2).
    """
    groups: list[list] = []
    for char in text:
        char_events: list = []
        if char == '\n':
            vk_code = VK_CODE_MAP['enter']
            char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=0))))
            char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=KEYEVENTF_KEYUP))))
        elif char == '\t':
            vk_code = VK_CODE_MAP['tab']
            char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=0))))
            char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=KEYEVENTF_KEYUP))))
        else:
            cp = ord(char)
            if cp <= 0xFFFF:
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=cp, dwFlags=KEYEVENTF_UNICODE))))
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=cp, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))))
            else:
                offset = cp - 0x10000
                high = 0xD800 | (offset >> 10)
                low = 0xDC00 | (offset & 0x3FF)
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=high, dwFlags=KEYEVENTF_UNICODE))))
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=high, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))))
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=low, dwFlags=KEYEVENTF_UNICODE))))
                char_events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=0, wScan=low, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))))
        groups.append(char_events)
    return groups


def type_string_verified(
    text: str, chunk_delay: float = 0.001, *, caller_notifies: bool = False
) -> tuple[bool, int, str | None]:
    """Send Unicode text via SendInput with verified delivery semantics.

    Built for VerifiedUnicodeStrategy (wh-jmt5x): the strategy must know
    whether every character actually landed before it updates the shadow
    buffer or increments the retraction counter. The plain ``type_string``
    only logs partial sends, leaving callers blind.

    Non-BMP characters (code points above U+FFFF, e.g. emoji) are sent
    as UTF-16 surrogate pairs (4 events per character). A character only
    counts as delivered when every event in its group has been accepted
    by SendInput (wh-3pw8.2).

    Returns:
        (success, chars_sent, error):
            success -- True only when every event landed; False on any
                partial send, Win32 failure, or SendInput exception.
            chars_sent -- number of complete Python characters delivered.
                A non-BMP character whose low surrogate did not land
                does not count.
            error -- None on full success; a short string identifying
                the failure mode otherwise (``partial: ...``,
                ``win32 error <code>``, or ``sendinput exception ...``).

    Args:
        caller_notifies: same meaning as in :func:`verified_press_keys`.
            True lowers both refusal records below to WARNING because the
            caller shows the user its own notice, and an ERROR record would
            add a second box (wh-keyboard-refusal-notice).
    """
    refusal_level = logging.WARNING if caller_notifies else logging.ERROR
    if not text:
        return True, 0, None

    groups = _build_unicode_event_groups(text)
    if not groups:
        return True, 0, None

    events: list = []
    cumulative_event_count: list[int] = []
    for group in groups:
        events.extend(group)
        cumulative_event_count.append(len(events))

    def _chars_completed(events_done: int) -> int:
        count = 0
        for c in cumulative_event_count:
            if c <= events_done:
                count += 1
            else:
                break
        return count

    CHUNK_SIZE = 8
    total_events_sent = 0
    total_chunks = (len(events) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunk_index = 0

    for i in range(0, len(events), CHUNK_SIZE):
        chunk = events[i:i + CHUNK_SIZE]
        num_events_in_chunk = len(chunk)
        input_array = (Input * num_events_in_chunk)(*chunk)
        chunk_index += 1

        # wh-trailing-corruption-phase2: wall-clock the SendInput call so
        # the next cold-start reproduction shows whether SendInput itself
        # stalls during the broken stretch, or whether it returns in the
        # same time as warm dispatches.
        send_start = time.perf_counter()
        try:
            sent_events = user32.SendInput(
                num_events_in_chunk,
                ctypes.byref(input_array),
                ctypes.sizeof(Input),
            )
        except Exception as exc:
            send_elapsed_us = (time.perf_counter() - send_start) * 1_000_000
            chars_sent = _chars_completed(total_events_sent)
            error = f"sendinput exception {type(exc).__name__}: {exc}"
            logger.log(
                refusal_level,
                "type_string_verified: %s; chars_sent=%d/%d send_us=%.1f",
                error, chars_sent, len(text), send_elapsed_us, exc_info=True,
            )
            return False, chars_sent, error

        send_elapsed_us = (time.perf_counter() - send_start) * 1_000_000
        total_events_sent += sent_events

        if sent_events != num_events_in_chunk:
            chars_sent = _chars_completed(total_events_sent)
            err_code = kernel32.GetLastError()
            if sent_events == 0:
                error = f"win32 error {err_code}"
            else:
                error = (
                    f"partial: expected {num_events_in_chunk} got {sent_events} "
                    f"at chunk offset {i}"
                )
                if err_code != 0:
                    error += f"; win32 error {err_code}"
            logger.log(
                refusal_level,
                "type_string_verified: %s; chars_sent=%d/%d",
                error, chars_sent, len(text),
            )
            return False, chars_sent, error

        # wh-trailing-corruption-instrument: per-chunk happy-path log so
        # the next reproduction of wh-startup-trailing-corruption surfaces
        # any drift between expected and accepted event counts even when
        # the overall return is True.
        # wh-trailing-corruption-phase2: send_us is the wall-clock cost of
        # the user32.SendInput call itself. A sudden change between the
        # last-good and first-bad dispatch would point at SendInput being
        # the warmup gate; consistent timing across the boundary points
        # away from SendInput and toward the input pipeline between
        # SendInput and the target control.
        logger.debug(
            "type_string_verified: chunk %d/%d sent=%d expected=%d total=%d "
            "send_us=%.1f",
            chunk_index, total_chunks, sent_events, num_events_in_chunk,
            total_events_sent, send_elapsed_us,
        )

        time.sleep(chunk_delay)

    return True, len(groups), None


def type_string(text: str, chunk_delay: float = 0.001):
    """
    :flow: UI Action Execution
    :step: 2b
    :produces_for: Windows Input System
    :description: Types Unicode text strings via SendInput with KEYEVENTF_UNICODE flag.
    :data_in: text (string to type), chunk_delay (microsecond delay between chunks).
    :data_out: Unicode INPUT events sent to Windows input queue.
    :notes: Uses KEYEVENTF_UNICODE for direct character insertion, bypassing keyboard layout
        and supporting full Unicode range (emojis, international characters). Chunks events
        with configurable micro-delay to prevent overwhelming target app input queues.
        Handles special chars via Virtual Key codes. Called by both type_text (raw) and
        intelligent_insert_text (with spacing logic) action handlers.
        
    Types a string using the low-level SendInput API with the KEYEVENTF_UNICODE flag.
    Sends events in chunks with a configurable micro-delay to avoid overwhelming
    the target application's input queue.
    """
    if not text:
        return

    events = [ev for group in _build_unicode_event_groups(text) for ev in group]
    if not events:
        return

    CHUNK_SIZE = 8
    for i in range(0, len(events), CHUNK_SIZE):
        chunk = events[i:i + CHUNK_SIZE]
        num_events_in_chunk = len(chunk)
        input_array = (Input * num_events_in_chunk)(*chunk)
        
        sent_events = user32.SendInput(num_events_in_chunk, ctypes.byref(input_array), ctypes.sizeof(Input))
        
        if sent_events != num_events_in_chunk:
            logger.error(f"SendInput failed for a chunk. Sent {sent_events}/{num_events_in_chunk}. Win32 Error: {kernel32.GetLastError()}")
            break

        time.sleep(chunk_delay)


def _normalize_to_virtual_desktop(x: int, y: int) -> tuple[int, int]:
    """Map a physical screen pixel to SendInput ABSOLUTE virtual-desktop units.

    SendInput's ABSOLUTE coordinate space is 0..65535 spanning a target
    rectangle. With ``MOUSEEVENTF_VIRTUALDESK`` that rectangle is the VIRTUAL
    desktop (the union of every monitor), whose origin/size are read from
    ``GetSystemMetrics``. The physical point is normalized into that 0..65535
    space with round-half-up and clamped into ``[0, 65535]``.

    A degenerate virtual-desktop width/height (<= 1, e.g. a metrics read that
    returned 0) cannot be normalized; the corresponding axis is pinned to 0
    rather than dividing by zero. The cursor-verify step in :func:`click_at`
    then catches the resulting wrong landing and fails closed.
    """
    vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    vw = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))

    def _norm(value: int, origin: int, span: int) -> int:
        if span <= 1:
            return 0
        # round-half-up: add 0.5 and floor via int() on a non-negative value.
        scaled = (value - origin) * 65535.0 / (span - 1)
        n = int(scaled + 0.5)
        if n < 0:
            return 0
        if n > 65535:
            return 65535
        return n

    return _norm(x, vx, vw), _norm(y, vy, vh)


# Spoken-gesture button name -> (down flag, up flag). The gesture parameter
# (wh-click-gesture-param) needs the right button as well as the left; a name
# outside this map fails closed in :func:`click_at` with no input sent.
_MOUSE_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


def click_at(
    x: int,
    y: int,
    *,
    button: str = "left",
    click_count: int = 1,
) -> tuple[bool, int, str | None]:
    """Click at physical screen pixel ``(x, y)`` via SendInput, verified.

    The SendInput-backed coordinate-click seam injected into ``ClickExecutor``
    by the Input process (wh-l4h.1). A click at the wrong coordinate is the
    exact hands-free hazard the executor's coordinate fallback exists to
    prevent, so this primitive is FAIL-CLOSED:

      0. Suppress physical mouse/keyboard input with ``BlockInput`` so a hand
         resting on or moving the mouse cannot override the injected move
         (wh-click-mouse-contention), and retry the whole step 1-3 block up to
         ``_CLICK_MOVE_ATTEMPTS`` times. ``BlockInput`` is best-effort (skipped
         if it returns 0) and is released on every Python control-flow path --
         normal return, a raised exception (via ``finally``), and the
         best-effort no-op. It is NOT released if the calling thread hangs
         inside ``SendInput``/``GetCursorPos`` while blocked, or the process is
         killed outright; Windows itself unblocks input when the thread exits,
         and Ctrl+Alt+Del always forces an unblock, so a stuck block stays
         recoverable.
      1. Normalize the physical pixel to ABSOLUTE virtual-desktop units and
         send a single MOVE event (ABSOLUTE | VIRTUALDESK).
      2. Read ``GetCursorPos`` and confirm the cursor landed within
         ``_CLICK_CURSOR_TOLERANCE_PX`` on each axis. If it did NOT land there,
         release the block, wait ``_CLICK_MOVE_RETRY_DELAY_S`` and retry; after
         the last attempt return ``(False, 0)`` WITHOUT synthesising any button
         event -- a wrong landing must never produce a click. A ``GetCursorPos``
         API failure is treated the same way (abandon the attempt and retry), so
         a single transient read failure does not consume the whole click.
      3. Only after the cursor verified, send a SEPARATE batch of exactly
         ``2 * click_count`` events (down, up per click) at the same ABSOLUTE
         coordinates, while physical input is still blocked. If that batch is
         only partly accepted and the accepted count leaves a down unpaired,
         send a compensating up so a partial click never leaves the button held
         down.

    ``button`` / ``click_count`` (wh-click-gesture-param) carry the spoken
    gesture: the defaults are one left click, exactly what every caller before
    the gesture parameter sent. ``button="right"`` presses the right button;
    ``click_count=2`` sends two down/up pairs in ONE SendInput batch, so
    nothing can slip between them and break the double-click interval. An
    unknown button name or a non-positive count fails CLOSED -- ``(False, 0)``
    with no input sent -- rather than guessing a gesture.

    Returns ``(success, events_sent, reason)``:

    * ``events_sent`` counts ONLY the button batch (the executor expects two
      events per click and maps a shortfall to ``sendinput_short``). The MOVE
      event is deliberately excluded so a short click is not masked. It is 0
      when the cursor did not verify (no click batch was issued).
    * ``success`` is True only when the cursor verified AND the click batch
      accepted every event.
    * ``reason`` is ``"release_failed"`` when a partial click batch left a
      button DOWN and the compensating release was refused too, so the
      button may still be held (wh-mouse-grid.1.5); ``None`` otherwise.
      Every other failure guarantees no button is left pressed.

    Any internal exception fails soft to ``(False, 0, None)`` -- a ctypes /
    Win32 error never propagates out of the seam.
    """
    try:
        button_flags = _MOUSE_BUTTON_FLAGS.get(button)
        if button_flags is None or click_count < 1:
            logger.error(
                "click_at: unsupported gesture (button=%r click_count=%r); "
                "failing closed with no input sent", button, click_count,
            )
            return (False, 0, None)

        nx, ny = _normalize_to_virtual_desktop(x, y)

        move_flags = (
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        )
        move = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=move_flags)),
        )
        move_array = (Input * 1)(move)

        down_flag, up_flag = button_flags
        click_flags_down = down_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        click_flags_up = up_flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        down = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=click_flags_down)),
        )
        up = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=click_flags_up)),
        )
        # One down/up pair per requested click, all in a single batch (a double
        # click is two pairs; the default single click is the one pair every
        # pre-gesture caller sent).
        expected_click_events = 2 * click_count
        click_array = (Input * expected_click_events)(
            *([down, up] * click_count)
        )
        # A standalone button release, used if a partial click batch left the
        # button held down (wh-review-click-overlay-codex.1).
        up_array = (Input * 1)(up)

        # Suppress physical mouse/keyboard for the brief move+verify+click so a
        # hand resting on or moving the mouse cannot override the injected
        # absolute MOVE (wh-click-mouse-contention). Retry the whole block up to
        # _CLICK_MOVE_ATTEMPTS times: BlockInput is best-effort (returns 0 and is
        # skipped if another thread already blocked or the process lacks
        # privilege, and is silently ignored under a low-level input hook or the
        # secure desktop), and a bounded retry rides out a transient miss.
        # Fail-closed is preserved -- the cursor is still verified on the target
        # before any button event, BlockInput is released on every Python
        # control-flow path (normal return, exception via finally, best-effort
        # no-op), and the click batch fires while physical input is still
        # blocked so nothing can move the cursor between the verify and the
        # press. A thread hang inside SendInput/GetCursorPos while blocked, or a
        # process kill, is outside the finally's reach; Windows auto-unblocks
        # when the thread exits and Ctrl+Alt+Del forces an unblock, so a stuck
        # block stays recoverable. UIPI still blocks both the MOVE and
        # BlockInput when the target window is elevated; that is an OS boundary
        # this cannot cross.
        observed = (0, 0)
        for attempt in range(_CLICK_MOVE_ATTEMPTS):
            input_blocked = False
            landed = False
            try:
                input_blocked = bool(user32.BlockInput(1))

                user32.SendInput(
                    1, ctypes.byref(move_array), ctypes.sizeof(Input)
                )

                # The MOVE is delivered asynchronously, so poll GetCursorPos up
                # to _CLICK_CURSOR_VERIFY_ATTEMPTS times and accept the first
                # read within tolerance (wh-9f3t.75.1). A GetCursorPos API
                # failure abandons this attempt and lets the outer loop retry.
                for _ in range(_CLICK_CURSOR_VERIFY_ATTEMPTS):
                    point = wintypes.POINT()
                    if not user32.GetCursorPos(ctypes.byref(point)):
                        # A GetCursorPos failure can be transient (a brief
                        # desktop switch, a UIPI timeout during a foreground
                        # transition, input-queue pressure). Treat it like a
                        # positioning miss: abandon this attempt and let the
                        # outer loop retry the whole move. landed stays False, so
                        # no click is ever sent without a verified on-target
                        # cursor (wh-review-click-overlay-glm52.2).
                        logger.error(
                            "click_at: GetCursorPos failed on attempt %d; will "
                            "retry. requested=(%d,%d) win32 error %s",
                            attempt, x, y, kernel32.GetLastError(),
                        )
                        break
                    observed = (int(point.x), int(point.y))
                    if (
                        abs(observed[0] - x) <= _CLICK_CURSOR_TOLERANCE_PX
                        and abs(observed[1] - y) <= _CLICK_CURSOR_TOLERANCE_PX
                    ):
                        landed = True
                        break
                    time.sleep(_CLICK_CURSOR_VERIFY_DELAY_S)

                if landed:
                    events_sent = int(
                        user32.SendInput(
                            expected_click_events,
                            ctypes.byref(click_array),
                            ctypes.sizeof(Input),
                        ) or 0
                    )
                    if events_sent != expected_click_events:
                        logger.error(
                            "click_at: short SendInput for click batch: sent "
                            "%d/%d; Win32 error %s",
                            events_sent, expected_click_events,
                            kernel32.GetLastError(),
                        )
                        # An ODD accepted count means the last button-down was
                        # accepted without its up, so the logical button is now
                        # held down; send a best-effort release so a partial
                        # batch cannot leave the button stuck (a drag/selection
                        # hazard). This runs while physical input is still
                        # blocked, so nothing can interfere; the finally then
                        # releases BlockInput. An even count (including 0)
                        # leaves every press already paired with its release.
                        if events_sent % 2 == 1:
                            # A raise from the compensating send must be
                            # handled HERE, not by the outer except: that
                            # handler returns (False, 0, None), which both
                            # discards the accepted DOWN count and suppresses
                            # the stuck-button reason (wh-mouse-grid.1.8).
                            try:
                                comp_sent = int(
                                    user32.SendInput(
                                        1, ctypes.byref(up_array),
                                        ctypes.sizeof(Input),
                                    ) or 0
                                )
                            except Exception as comp_exc:  # noqa: BLE001 -- a real SendInput seam can raise
                                logger.error(
                                    "click_at: compensating %s release "
                                    "raised (%s) -- button may be stuck "
                                    "down", button, comp_exc, exc_info=True,
                                )
                                comp_sent = 0
                            # The compensating release is best-effort, but if it
                            # ALSO short-delivers the button is left held
                            # down. Log it AND report it as a distinct reason
                            # so the stuck-button state reaches the user
                            # instead of collapsing onto sendinput_short
                            # (wh-review-click-overlay-glm52.1,
                            # wh-mouse-grid.1.5).
                            if comp_sent != 1:
                                logger.error(
                                    "click_at: compensating %s release also "
                                    "failed: sent %d/1; Win32 error %s -- "
                                    "button may be stuck down",
                                    button, comp_sent, kernel32.GetLastError(),
                                )
                                return (False, events_sent, "release_failed")
                        return (False, events_sent, None)
                    return (True, events_sent, None)
            finally:
                if input_blocked:
                    user32.BlockInput(0)

            # The cursor did not land -- physical input may still be overriding
            # the MOVE. Wait briefly (physical input now unblocked) and try the
            # whole move again.
            if attempt < _CLICK_MOVE_ATTEMPTS - 1:
                time.sleep(_CLICK_MOVE_RETRY_DELAY_S)

        logger.error(
            "click_at: cursor did not land at target after %d move attempts "
            "(%d verify polls each); failing closed. requested=(%d,%d) "
            "observed=(%d,%d)",
            _CLICK_MOVE_ATTEMPTS, _CLICK_CURSOR_VERIFY_ATTEMPTS,
            x, y, observed[0], observed[1],
        )
        return (False, 0, None)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput/Win32 seam can raise
        logger.error("click_at: unexpected error: %s", exc, exc_info=True)
        return (False, 0, None)

# ---------------------------------------------------------------------------
# Mouse-grid pointer primitives (wh-input-mouse-primitives).
#
# The three operations the mouse-grid overlay needs from the Input process --
# click at a point, park the pointer at a point, and perform a drag between
# two points. Design doc:
# docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md, the
# "Input process -- the only place that touches the mouse" section.
#
# These deliberately do NOT run UI Automation verification or the occlusion
# hit-test that ``click_at`` (the by-name coordinate fallback) relies on: the
# user picked the point visually off a painted grid, so the by-name
# protections -- which exist to stop a click on a control the user cannot see
# -- have nothing to protect against here. See the spec's "Verification
# difference" section.
#
# What they DO keep is the physical-cursor landing check: the pointer is moved
# with a normalized absolute MOVE and ``GetCursorPos`` must confirm it landed
# before any button event is synthesised. A hands-free click at the wrong
# coordinate is the hazard, and it is independent of how the point was chosen.
#
# 64-bit note: every INPUT struct here is the shared ``Input`` / ``Input_I`` /
# ``MouseInput`` definition at the top of this module. ``dwExtraInfo`` is a
# ctypes pointer type (8 bytes on x64, matching ULONG_PTR), and dx/dy are
# ``wintypes.LONG``, so ``ctypes.sizeof(Input)`` matches what the 64-bit
# SendInput expects. Do not substitute a hand-rolled struct.
# ---------------------------------------------------------------------------

# Button name -> (down flag, up flag). The grid ships left and right only;
# "middle" has no spoken command and would need its own notice wording.
_MOUSE_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}

# The grid's spoken gestures are a single click and a double click; a triple
# click has no command, and an unbounded count would let one malformed IPC
# message flood the input queue.
_MAX_CLICK_COUNT = 2

# Drag shaping. Many applications ignore a drag whose pointer teleports --
# they never see the intermediate movement that starts their drag operation --
# so the movement is interpolated. One step per ~16 ms is roughly one per
# display frame at 60 Hz, which is smooth enough for every drag target tested
# without flooding the input queue. The step count is clamped so a 0 ms drag
# still moves gradually and a long drag does not send hundreds of events.
_DRAG_STEP_TARGET_MS = 16.0
_DRAG_MIN_STEPS = 8
_DRAG_MAX_STEPS = 60

# Accepted range for a requested drag duration, in milliseconds. The spec's
# default is 250. The upper bound is a sanity limit: the Input process command
# loop is single-threaded, so a drag blocks every other input action for its
# whole duration.
_DRAG_MAX_DURATION_MS = 60_000


def _is_plain_int(value) -> bool:
    """True for a real int. ``bool`` is an int subclass and is rejected."""
    return isinstance(value, int) and not isinstance(value, bool)


def _absolute_mouse_event(nx: int, ny: int, flags: int):
    """Build one INPUT struct carrying normalized absolute coordinates."""
    return Input(
        type=INPUT_MOUSE,
        ii=Input_I(
            mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=flags)
        ),
    )


def _send_mouse_events(events: list) -> int:
    """Send a batch of mouse INPUT structs; return the accepted count.

    Raises whatever the ctypes call raises -- each caller decides how to fail.
    """
    count = len(events)
    if count == 0:
        return 0
    array = (Input * count)(*events)
    return int(
        user32.SendInput(count, ctypes.byref(array), ctypes.sizeof(Input)) or 0
    )


def _move_cursor_and_verify(x: int, y: int) -> bool:
    """Send one absolute MOVE to physical ``(x, y)`` and confirm it landed.

    SendInput posts the MOVE into the system input queue asynchronously, so
    the first ``GetCursorPos`` can read the stale pre-move position; poll up to
    ``_CLICK_CURSOR_VERIFY_ATTEMPTS`` times and accept the first read within
    ``_CLICK_CURSOR_TOLERANCE_PX`` on each axis. Returns False when no read
    landed (including a ``GetCursorPos`` API failure), leaving the caller to
    fail closed or retry.
    """
    nx, ny = _normalize_to_virtual_desktop(x, y)
    move_flags = (
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    )
    _send_mouse_events([_absolute_mouse_event(nx, ny, move_flags)])

    for _ in range(_CLICK_CURSOR_VERIFY_ATTEMPTS):
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            logger.error(
                "mouse primitive: GetCursorPos failed while verifying a move "
                "to (%d,%d); Win32 error %s",
                x, y, kernel32.GetLastError(),
            )
            return False
        if (
            abs(int(point.x) - x) <= _CLICK_CURSOR_TOLERANCE_PX
            and abs(int(point.y) - y) <= _CLICK_CURSOR_TOLERANCE_PX
        ):
            return True
        time.sleep(_CLICK_CURSOR_VERIFY_DELAY_S)
    return False


def _move_cursor_with_retry(x: int, y: int) -> bool:
    """Retry :func:`_move_cursor_and_verify` a bounded number of times.

    A physical mouse being moved by hand overrides the injected absolute MOVE
    for the whole verify window (wh-click-mouse-contention), so one miss is
    not proof the coordinate is wrong. Retrying the whole move rides out a
    transient miss without weakening the landing check.
    """
    for attempt in range(_CLICK_MOVE_ATTEMPTS):
        if _move_cursor_and_verify(x, y):
            return True
        if attempt < _CLICK_MOVE_ATTEMPTS - 1:
            time.sleep(_CLICK_MOVE_RETRY_DELAY_S)
    return False


def _release_button_in_place(up_flag: int) -> tuple[bool, str | None]:
    """Release a held mouse button wherever the pointer currently is.

    No ``MOUSEEVENTF_ABSOLUTE`` and no ``MOUSEEVENTF_MOVE``: dx/dy are ignored
    and the release happens at the current cursor position. That is the right
    semantic for the drag's guaranteed release -- when the movement failed
    partway, dragging the pointer to the requested end point just to release
    it there would drop the item somewhere the user never saw it travel to.

    Never raises. Returns ``(released, reason)``; ``reason`` is
    ``"release_failed"`` when the button may still be held.
    """
    try:
        accepted = _send_mouse_events([
            Input(
                type=INPUT_MOUSE,
                ii=Input_I(
                    mi=MouseInput(dx=0, dy=0, mouseData=0, dwFlags=up_flag)
                ),
            )
        ])
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        logger.error(
            "mouse primitive: the button release raised (%s) -- the button "
            "may be stuck down", exc, exc_info=True,
        )
        return (False, "release_failed")
    if accepted != 1:
        logger.error(
            "mouse primitive: the button release was refused (sent %d/1; "
            "Win32 error %s) -- the button may be stuck down",
            accepted, kernel32.GetLastError(),
        )
        return (False, "release_failed")
    return (True, None)


def click_point(
    x: int, y: int, button: str = "left", click_count: int = 1,
) -> tuple[bool, str | None]:
    """Click at physical screen pixel ``(x, y)`` with a button and a count.

    The mouse-grid click primitive. Physical input is suppressed with
    ``BlockInput`` for the brief move-and-click so a hand resting on the mouse
    cannot override the injected absolute MOVE, and the block is released on
    every Python control-flow path via ``finally``.

    Args:
        x, y: physical screen pixels. Negative values are normal -- a monitor
            left of or above the primary has negative coordinates, and the
            virtual-desktop normalization handles the offset.
        button: ``"left"`` or ``"right"``.
        click_count: 1 or 2. A count of 2 sends both down/up pairs in one
            SendInput batch, which Windows reads as a double click.

    Returns ``(succeeded, reason)``. ``reason`` is ``None`` on success and
    otherwise one of ``"invalid_point"``, ``"invalid_button"``,
    ``"invalid_click_count"``, ``"cursor_did_not_land"``,
    ``"sendinput_short"``, ``"sendinput_error"``, ``"release_failed"``.
    Never raises. ``"release_failed"`` means a partial click batch left a
    button DOWN and the compensating release was refused too, so the button
    may still be held; every other failure guarantees it is not.
    """
    if not (_is_plain_int(x) and _is_plain_int(y)):
        logger.error("click_point: non-integer point (%r,%r)", x, y)
        return (False, "invalid_point")
    if not isinstance(button, str) or button not in _MOUSE_BUTTON_FLAGS:
        logger.error("click_point: unsupported button %r", button)
        return (False, "invalid_button")
    if not _is_plain_int(click_count) or not 1 <= click_count <= _MAX_CLICK_COUNT:
        logger.error("click_point: unsupported click count %r", click_count)
        return (False, "invalid_click_count")

    down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
    absolute = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    input_blocked = False
    try:
        input_blocked = bool(user32.BlockInput(1))

        if not _move_cursor_with_retry(x, y):
            logger.error(
                "click_point: the cursor did not land at (%d,%d) after %d "
                "attempts; sending no button event",
                x, y, _CLICK_MOVE_ATTEMPTS,
            )
            return (False, "cursor_did_not_land")

        nx, ny = _normalize_to_virtual_desktop(x, y)
        events = []
        for _ in range(click_count):
            events.append(
                _absolute_mouse_event(nx, ny, down_flag | absolute)
            )
            events.append(_absolute_mouse_event(nx, ny, up_flag | absolute))

        accepted = _send_mouse_events(events)
        if accepted != len(events):
            logger.error(
                "click_point: short SendInput for the %s click batch: sent "
                "%d/%d; Win32 error %s",
                button, accepted, len(events), kernel32.GetLastError(),
            )
            # An odd accepted count means the last accepted event was a DOWN,
            # so the button is held; release it. An even count (including 0)
            # left no button pressed, and a spurious release could register as
            # a real one with nothing down. A refused compensating release
            # outranks the short send (wh-mouse-grid.1.5): release_failed is
            # the one reason that tells the caller the button may still be
            # held, and sendinput_short would hide it.
            if accepted % 2 == 1:
                released, release_reason = _release_button_in_place(up_flag)
                if not released:
                    return (False, release_reason)
            return (False, "sendinput_short")
        return (True, None)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        logger.error("click_point: unexpected error: %s", exc, exc_info=True)
        return (False, "sendinput_error")
    finally:
        if input_blocked:
            user32.BlockInput(0)


def move_pointer_to(x: int, y: int) -> tuple[bool, str | None]:
    """Park the pointer at physical screen pixel ``(x, y)``, pressing nothing.

    The mouse-grid "move here" primitive, for hover-revealed menus in the
    applications with poor accessibility trees that motivate the grid. No
    ``BlockInput``: a hover is harmless if the user's own hand wins, and
    suppressing physical input for a no-button operation would be a worse
    trade than the miss it prevents. The move itself still retries like the
    button primitives, because a single verify pass can lose the plain
    SendInput/GetCursorPos race even with nobody touching the mouse.

    Returns ``(succeeded, reason)`` with ``reason`` one of ``None``,
    ``"invalid_point"``, ``"cursor_did_not_land"``, ``"sendinput_error"``.
    Never raises.
    """
    if not (_is_plain_int(x) and _is_plain_int(y)):
        logger.error("move_pointer_to: non-integer point (%r,%r)", x, y)
        return (False, "invalid_point")

    try:
        if not _move_cursor_with_retry(x, y):
            logger.error(
                "move_pointer_to: the cursor did not land at (%d,%d) after "
                "%d attempts", x, y, _CLICK_MOVE_ATTEMPTS,
            )
            return (False, "cursor_did_not_land")
        return (True, None)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        logger.error(
            "move_pointer_to: unexpected error: %s", exc, exc_info=True,
        )
        return (False, "sendinput_error")


# Spoken direction -> (wheel flag, sign of one notch). Positive vertical
# scrolls the content up (the wheel turns away from the user); positive
# horizontal scrolls to the right. Both signs are what the Win32
# documentation specifies for mouseData.
_SCROLL_DIRECTIONS: dict[str, tuple[int, int]] = {
    "up": (MOUSEEVENTF_WHEEL, 1),
    "down": (MOUSEEVENTF_WHEEL, -1),
    "right": (MOUSEEVENTF_HWHEEL, 1),
    "left": (MOUSEEVENTF_HWHEEL, -1),
}

# wh-wheel-refusal-notice: the :func:`scroll_wheel` refusal reasons that keep
# a ``logger.error`` record here. ErrorNotificationHandler is attached to the
# root logger and turns every ERROR record into a generic notice box with no
# opt-out, so a reason listed here reports itself and a caller that adds its
# own notice would give the user two boxes.
#
# IT IS EMPTY, AND THAT IS THE POINT. DO NOT DELETE IT AS DEAD CODE.
# Empty asserts the uniform rule this bead arrived at: NO wheel refusal
# logs an ERROR, and every wheel refusal is reported by its caller's own
# written notice. The two validation refusals were the last holdouts, and
# wh-wheel-refusal-notice.1.7 removed them, REVERSING the earlier
# in-bead decision to treat an ERROR record as proof the user was told.
# What felled it: ErrorNotificationHandler.emit keys on (logger name, level,
# message) and returns without submitting anything when the same key
# repeats inside rate_limit_seconds, which utils/logging_setup.py sets to
# 10. A malformed wheel action repeated inside ten seconds produced the
# same key, so the box never appeared -- and the caller stayed quiet
# because this tuple said the reason had reported itself. ZERO notices.
# An ERROR record is not proof of delivery, so nothing may rely on one.
#
# A refusal reason added later must log BELOW ERROR and stay out of
# this tuple. Adding a reason here again re-adopts the rate limiter as
# part of the delivery path; do not do it without reading .1.7 first.
# ui.ui_action_handler reads this tuple rather than repeating it, so the
# choice and the list stay in one file.
# tests/test_win_mouse_scroll.py walks every reason and checks the two halves
# against each other.
WHEEL_REFUSALS_THAT_LOG_ERROR: tuple[str, ...] = ()


def scroll_wheel(direction, clicks: int = 1) -> tuple[bool, str | None]:
    """Turn the mouse wheel ``clicks`` notches in ``direction``.

    The primitive behind the spoken scroll commands. It sends wheel events at
    whatever position the pointer already holds: a wheel event carries its
    distance in ``mouseData`` and Windows ignores dx/dy, so nothing here moves
    the pointer and neither ``MOUSEEVENTF_MOVE`` nor ``MOUSEEVENTF_ABSOLUTE``
    is set.

    No ``BlockInput``, unlike :func:`click_point` and :func:`drag_pointer`.
    Those suppress physical input so a hand on the mouse cannot drag the
    pointer away from the absolute point they just moved it to. A wheel event
    has no coordinate to defend, so suppressing the user's own input would
    cost more than it protects.

    All ``clicks`` notches travel in ONE ``SendInput`` call, so an application
    that coalesces a burst of wheel messages sees them as one gesture.

    Args:
        direction: ``"up"``, ``"down"``, ``"left"`` or ``"right"``.
        clicks: whole wheel notches, 1 to :data:`MAX_SCROLL_CLICKS`. ``bool``
            is rejected even though it is an ``int`` subclass.

    Returns ``(succeeded, reason)``. ``reason`` is ``None`` on success and
    otherwise one of ``"invalid_direction"``, ``"invalid_clicks"``,
    ``"sendinput_short"``, ``"sendinput_error"``. Never raises. A refused
    call sends nothing at all.

    Every refusal here logs BELOW ERROR and the caller writes the
    user's notice: read :data:`WHEEL_REFUSALS_THAT_LOG_ERROR` above before
    you write the record.
    """
    if not isinstance(direction, str) or direction not in _SCROLL_DIRECTIONS:
        # WARNING, not ERROR (wh-wheel-refusal-notice.1.7). A malformed
        # direction is still a code fault worth the full detail in the log,
        # but the ERROR record's generic box cannot be relied on to reach
        # the user: the rate limiter drops a repeat of the same message
        # inside ten seconds. The caller's own notice is the report.
        logger.warning("scroll_wheel: unsupported direction %r", direction)
        return (False, "invalid_direction")
    if not _is_plain_int(clicks) or not 1 <= clicks <= MAX_SCROLL_CLICKS:
        # WARNING for the same reason as the direction check above.
        logger.warning("scroll_wheel: unsupported notch count %r", clicks)
        return (False, "invalid_clicks")

    flag, sign = _SCROLL_DIRECTIONS[direction]
    # mouseData is a DWORD, which is unsigned, and a scroll down or left needs
    # a negative distance. ctypes itself wraps a negative int into the field
    # (measured: -120 reads back as 4294967176), so the mask below changes
    # nothing about what Windows receives. It is written out so the two's
    # complement value is visible at the point it is chosen rather than left
    # to a ctypes conversion a reader has to know about.
    delta = (sign * WHEEL_DELTA) & 0xFFFFFFFF

    try:
        events = [
            Input(
                type=INPUT_MOUSE,
                ii=Input_I(
                    mi=MouseInput(dx=0, dy=0, mouseData=delta, dwFlags=flag)
                ),
            )
            for _ in range(clicks)
        ]
        accepted = _send_mouse_events(events)
        if accepted != len(events):
            # WARNING, not ERROR: both wheel callers write their own notice
            # -- the continuous scroll's WHEEL_FAILED_MESSAGE and the discrete
            # scroll's SCROLL_REFUSED_MESSAGE -- and an ERROR record would add
            # a second, generic box through ErrorNotificationHandler, which is
            # attached to the root logger and has no opt-out
            # (wh-wheel-refusal-notice). The line keeps its accepted count and
            # Win32 error, so the diagnosis survives the level change. This
            # cannot be a per-caller decision: both callers reach this one
            # call, so nothing set here can tell them apart.
            logger.warning(
                "scroll_wheel: short SendInput for %d %s notches: sent %d/%d; "
                "Win32 error %s",
                clicks, direction, accepted, len(events),
                kernel32.GetLastError(),
            )
            # Nothing to undo. A wheel event is complete on its own, so a
            # partly accepted batch has simply scrolled less far than asked,
            # unlike a half-sent click that leaves a button held down.
            return (False, "sendinput_short")
        return (True, None)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        # WARNING for the same reason as the short send above: the caller's
        # own notice is the user's report (wh-wheel-refusal-notice).
        logger.warning(
            "scroll_wheel: unexpected error: %s", exc, exc_info=True,
        )
        return (False, "sendinput_error")


def _drag_step_count(duration_ms: int) -> int:
    """How many interpolation steps a drag of ``duration_ms`` gets.

    Duration 0 is the documented no-interpolation choice
    (``click_config.py``: "a single move then release"), so it gets exactly
    one step -- the final MOVE+UP pair -- rather than the minimum gradual
    count with zero delays (wh-mouse-grid.1.10).
    """
    if duration_ms == 0:
        return 1
    steps = int(round(duration_ms / _DRAG_STEP_TARGET_MS))
    return max(_DRAG_MIN_STEPS, min(_DRAG_MAX_STEPS, steps))


def _interpolate_drag_points(
    start_x: int, start_y: int, end_x: int, end_y: int, steps: int,
) -> list[tuple[int, int]]:
    """The ``steps`` points a drag passes through, ending exactly on the end.

    The start point is NOT included (the pointer is already there and its
    landing was verified). The last entry is the end point verbatim rather
    than a rounded interpolation, so a drag always finishes exactly where it
    was asked to.
    """
    points: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        if i == steps:
            points.append((end_x, end_y))
            continue
        fraction = i / steps
        points.append(
            (
                start_x + int(round((end_x - start_x) * fraction)),
                start_y + int(round((end_y - start_y) * fraction)),
            )
        )
    return points


def _run_drag_movement(
    points: list[tuple[int, int]], step_delay_s: float, up_flag: int,
) -> tuple[bool, str | None]:
    """Move through ``points`` with the button held, then always release it.

    Called with the button already DOWN. The final MOVE and the UP travel in
    ONE SendInput batch: the interpolated movement runs with physical input
    unblocked, and SendInput injects a batch contiguously, so pairing them is
    what stops a physical mouse movement from slipping in between the last
    injected MOVE and the release and relocating the drop
    (wh-mouse-grid.1.4). The UP carries the end coordinates itself.

    The in-place release remains the guarantee for every other exit -- an
    earlier step failing, a raise from the movement, or the final pair only
    half-delivering -- because a drag that leaves the button pressed hands
    the user a desktop where every later pointer movement drags something.

    A raise from the movement is captured, the release runs, and only THEN is
    the raise re-raised to the caller -- and only if the release succeeded. A
    stuck button outranks the movement error (wh-mouse-grid.1.9): when the
    cleanup release also fails, this returns ``(False, "release_failed")``
    instead of re-raising, so the caller cannot collapse the held-button
    state into a generic ``sendinput_error``.
    """
    absolute = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    move_flags = MOUSEEVENTF_MOVE | absolute
    reason: str | None = None
    released = False
    release_reason: str | None = None
    primary_exc: Exception | None = None
    try:
        for px, py in points[:-1]:
            if step_delay_s > 0:
                time.sleep(step_delay_s)
            nx, ny = _normalize_to_virtual_desktop(px, py)
            accepted = _send_mouse_events([
                _absolute_mouse_event(nx, ny, move_flags)
            ])
            if accepted != 1:
                logger.error(
                    "drag_pointer: a movement step to (%d,%d) was refused "
                    "(sent %d/1; Win32 error %s); releasing the button",
                    px, py, accepted, kernel32.GetLastError(),
                )
                reason = "sendinput_short"
                break
        else:
            end_x, end_y = points[-1]
            if step_delay_s > 0:
                time.sleep(step_delay_s)
            nex, ney = _normalize_to_virtual_desktop(end_x, end_y)
            accepted = _send_mouse_events([
                _absolute_mouse_event(nex, ney, move_flags),
                _absolute_mouse_event(nex, ney, up_flag | absolute),
            ])
            if accepted == 2:
                released = True
            else:
                # accepted == 1 means the MOVE landed but the UP was refused
                # (button still held, at the end point); 0 means neither went
                # out (button still held at the previous point). Either way
                # the finally's in-place release is the recovery.
                logger.error(
                    "drag_pointer: the final move-and-release pair at "
                    "(%d,%d) was refused (sent %d/2; Win32 error %s); "
                    "releasing the button in place",
                    end_x, end_y, accepted, kernel32.GetLastError(),
                )
                reason = "sendinput_short"
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        primary_exc = exc
    finally:
        if not released:
            released, release_reason = _release_button_in_place(up_flag)

    if not released:
        # A stuck button outranks whatever else went wrong -- it is the state
        # the user has to live with.
        return (False, release_reason)
    if primary_exc is not None:
        # The button is confirmed released; let the caller map the movement
        # raise to sendinput_error as before.
        raise primary_exc
    if reason is not None:
        return (False, reason)
    return (True, None)


def drag_pointer(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int = 250,
) -> tuple[bool, str | None]:
    """Drag from one physical point to another as ONE operation.

    Left button down at the start point, interpolated movement across
    ``duration_ms``, button up at the end. The whole gesture runs inside this
    call because a drag split across IPC messages could lose its release
    message and strand the button pressed.

    The movement is gradual on purpose: many applications ignore a drag whose
    pointer teleports, because they never see the intermediate movement that
    makes them begin the drag operation.

    Physical input is suppressed with ``BlockInput`` for the initial
    move-and-press only (matching :func:`click_point`), released via
    ``finally`` on every path before the interpolated movement begins.

    Returns ``(succeeded, reason)`` with ``reason`` one of ``None``,
    ``"invalid_point"``, ``"invalid_duration"``, ``"cursor_did_not_land"``,
    ``"sendinput_short"``, ``"sendinput_error"``, ``"release_failed"``.
    Never raises. ``"release_failed"`` is the one outcome that means the
    button may still be held; every other failure guarantees it is not.
    """
    if not all(
        _is_plain_int(v) for v in (start_x, start_y, end_x, end_y)
    ):
        logger.error(
            "drag_pointer: non-integer point in (%r,%r)->(%r,%r)",
            start_x, start_y, end_x, end_y,
        )
        return (False, "invalid_point")
    if (
        not _is_plain_int(duration_ms)
        or not 0 <= duration_ms <= _DRAG_MAX_DURATION_MS
    ):
        logger.error("drag_pointer: unsupported duration %r", duration_ms)
        return (False, "invalid_duration")

    down_flag, up_flag = _MOUSE_BUTTON_FLAGS["left"]
    absolute = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    try:
        # Physical input is suppressed for the move-and-press only, matching
        # click_point: a hand resting on the mouse would otherwise win every
        # verify poll and abort the drag as cursor_did_not_land. The block is
        # released before the interpolated movement -- a drag can take
        # hundreds of milliseconds, and each step re-asserts absolute
        # coordinates, so contention mid-drag cannot change where it ends.
        input_blocked = False
        try:
            input_blocked = bool(user32.BlockInput(1))

            if not _move_cursor_with_retry(start_x, start_y):
                logger.error(
                    "drag_pointer: the cursor did not land on the start point "
                    "(%d,%d); pressing nothing", start_x, start_y,
                )
                return (False, "cursor_did_not_land")

            nsx, nsy = _normalize_to_virtual_desktop(start_x, start_y)
            accepted = _send_mouse_events([
                _absolute_mouse_event(nsx, nsy, down_flag | absolute)
            ])
            if accepted != 1:
                # Nothing was pressed, so there is nothing to release.
                logger.error(
                    "drag_pointer: the button press was refused (sent %d/1; "
                    "Win32 error %s)", accepted, kernel32.GetLastError(),
                )
                return (False, "sendinput_short")
        finally:
            if input_blocked:
                user32.BlockInput(0)

        # From here the button is DOWN and _run_drag_movement owns releasing
        # it on every path, including a raise that lands in the except below.
        steps = _drag_step_count(duration_ms)
        points = _interpolate_drag_points(
            start_x, start_y, end_x, end_y, steps,
        )
        return _run_drag_movement(points, duration_ms / steps / 1000.0, up_flag)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput seam can raise
        logger.error("drag_pointer: unexpected error: %s", exc, exc_info=True)
        return (False, "sendinput_error")


# wh-number-badge-problems: the hit test's 64-bit-safe signatures live on a
# PRIVATE user32 binding, never on ``ctypes.windll.user32``. ``ctypes.windll``
# holds one WinDLL object per library for the whole process and caches one
# function object per name, so a signature assigned there reaches every other
# caller of that function. uiautomation, the UIA library behind every
# focused-control read in the Input process, calls
# ``ctypes.windll.user32.GetAncestor(c_void_p(handle), c_int(flag))`` from
# ``Control.GetTopLevelControl``; on Windows ``c_int`` is ``c_long``, and
# against ``argtypes=[c_void_p, c_uint]`` ctypes raises ``ArgumentError:
# argument 2: TypeError: 'c_long' object cannot be interpreted as an integer``.
# When these signatures sat on the shared object, the first coordinate click
# after "show numbers" broke every later ``GetTopLevelControl`` in the process,
# so no text-insertion strategy could resolve its target window until
# WheelHouse was restarted (trace T-17881312777). ``input_proc.py``'s
# user-click check, which passes a ``wintypes.POINT`` to the shared
# ``WindowFromPoint``, broke the same way. A separate ``ctypes.WinDLL``
# instance has its own function cache, so these signatures are invisible to
# everyone else -- the pattern ``ui/hwnd_utils.py`` uses for SetPropW/GetPropW.
_HIT_TEST_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_HIT_TEST_USER32.WindowFromPoint.argtypes = [wintypes.POINT]
_HIT_TEST_USER32.WindowFromPoint.restype = ctypes.c_void_p  # HWND, 64-bit safe
_HIT_TEST_USER32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_HIT_TEST_USER32.GetAncestor.restype = ctypes.c_void_p
_GA_ROOT = 2


def root_window_at_point(x: int, y: int) -> int:
    """Return the ROOT top-level window handle at physical screen ``(x, y)``.

    The click-point hit-test seam injected into ``ClickExecutor``
    (wh-explorer-navpane-click.1.1): before the coordinate fallback sends a
    real click, it verifies the window that would actually RECEIVE the click
    is the target's own top-level window, not an always-on-top occluder.

    ``WindowFromPoint`` takes the same physical screen coordinates the UIA
    bounding rectangles use (the Input process is per-monitor DPI aware). It
    skips windows with the click-through ``WS_EX_TRANSPARENT`` extended style
    -- WheelHouse's own overlay badge windows -- exactly matching where a
    real click would land. ``GetAncestor(GA_ROOT)`` normalises a child
    control handle to its top-level root so the executor compares roots.

    Both calls go through ``_HIT_TEST_USER32``, the module's private user32
    binding, so their signatures never touch the process-shared
    ``ctypes.windll.user32`` (see the comment above the binding).

    Returns 0 when no window is at the point (the executor treats 0 as a
    mismatch and refuses). Raises are allowed to propagate: the executor maps
    any seam raise to the same fail-closed refusal.
    """
    hwnd = _HIT_TEST_USER32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return 0
    root = _HIT_TEST_USER32.GetAncestor(hwnd, _GA_ROOT)
    return int(root or hwnd or 0)
