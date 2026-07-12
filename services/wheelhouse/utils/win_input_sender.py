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
  
  # Send hotkey combination
  press_keys("ctrl+c")  # Copy hotkey
  press_keys("alt+tab") # Alt-Tab window switching
  
  # Type text directly
  type_string("Hello world!")
  
  # Complex combinations
  press_keys("ctrl+shift+n")  # New window/incognito
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
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# Mouse SendInput constants (wh-l4h.1 coordinate-click fallback seam).
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

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


def _build_press_keys_events(keys: tuple[str, ...]) -> tuple[list, int]:
    """Build the INPUT event sequence for a press_keys chord.

    Shared between :func:`press_keys` (fire-and-forget) and
    :func:`verified_press_keys` (returns accepted-event count) so both
    entry points use identical modifier ordering.

    Returns:
        ``(events, num_events)``. May return an empty list when ``keys``
        contains only modifiers that are also non-modifiers (impossible
        in practice but cheap to guard).

    Raises:
        _InvalidKeyError: when any element of ``keys`` is not in
        :data:`VK_CODE_MAP`. Callers translate this to the fire-and-forget
        no-op or the verified failure tuple as appropriate.
    """
    vk_codes = [VK_CODE_MAP.get(key.lower()) for key in keys]
    if None in vk_codes:
        logger.error(f"One or more keys in {keys} are not valid. Aborting.")
        raise _InvalidKeyError(keys)

    events: list = []
    modifiers_down: list = []
    for vk_code in vk_codes:
        if vk_code in (VK_CODE_MAP['ctrl'], VK_CODE_MAP['shift'], VK_CODE_MAP['alt'], VK_CODE_MAP['win']):
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=0))))
            modifiers_down.append(vk_code)
    for vk_code in vk_codes:
        if vk_code not in modifiers_down:
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=0))))
            events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=KEYEVENTF_KEYUP))))
    for vk_code in reversed(modifiers_down):
        events.append(Input(type=INPUT_KEYBOARD, ii=Input_I(ki=KeyBdInput(wVk=vk_code, dwFlags=KEYEVENTF_KEYUP))))
    return events, len(events)


def verified_press_keys(*keys: str) -> tuple[bool, int, int]:
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
    """
    if not keys:
        return True, 0, 0
    try:
        events, num_events = _build_press_keys_events(keys)
    except _InvalidKeyError:
        return False, 0, 0
    if num_events == 0:
        return True, 0, 0

    try:
        input_array = (Input * num_events)(*events)
        events_sent = user32.SendInput(num_events, ctypes.byref(input_array), ctypes.sizeof(Input))
    except Exception as exc:
        logger.error(
            "verified_press_keys: SendInput raised for keys=%s: %s",
            keys, exc, exc_info=True,
        )
        return False, 0, num_events

    accepted = int(events_sent or 0)
    if accepted != num_events:
        logger.error(
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
                    ki=KeyBdInput(wVk=vk_code, dwFlags=KEYEVENTF_KEYUP),
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
    text: str, chunk_delay: float = 0.001
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
    """
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
            logger.error(
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
            logger.error(
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


def click_at(x: int, y: int) -> tuple[bool, int]:
    """Left-click at physical screen pixel ``(x, y)`` via SendInput, verified.

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
      3. Only after the cursor verified, send a SEPARATE batch of exactly two
         events (LEFTDOWN, LEFTUP) at the same ABSOLUTE coordinates, while
         physical input is still blocked. If that batch is only partly accepted
         (just the LEFTDOWN went through), send a compensating LEFTUP so a
         partial click never leaves the button held down.

    Returns ``(success, events_sent)``:

    * ``events_sent`` counts ONLY the LEFTDOWN/LEFTUP batch (the executor
      expects 2 and maps ``events_sent < 2`` to ``sendinput_short``). The MOVE
      event is deliberately excluded so a short click is not masked. It is 0
      when the cursor did not verify (no click batch was issued).
    * ``success`` is True only when the cursor verified AND the click batch
      accepted both events.

    Any internal exception fails soft to ``(False, 0)`` -- a ctypes / Win32
    error never propagates out of the seam.
    """
    try:
        nx, ny = _normalize_to_virtual_desktop(x, y)

        move_flags = (
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        )
        move = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=move_flags)),
        )
        move_array = (Input * 1)(move)

        click_flags_down = MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        click_flags_up = MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        down = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=click_flags_down)),
        )
        up = Input(
            type=INPUT_MOUSE,
            ii=Input_I(mi=MouseInput(dx=nx, dy=ny, mouseData=0, dwFlags=click_flags_up)),
        )
        click_array = (Input * 2)(down, up)
        # A standalone LEFTUP, used to release the button if a partial click
        # batch left it held down (wh-review-click-overlay-codex.1).
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
                            2, ctypes.byref(click_array), ctypes.sizeof(Input)
                        ) or 0
                    )
                    if events_sent != 2:
                        logger.error(
                            "click_at: short SendInput for click batch: sent "
                            "%d/2; Win32 error %s",
                            events_sent, kernel32.GetLastError(),
                        )
                        # If only the LEFTDOWN was accepted (events_sent == 1),
                        # the logical left button is now held down; send a
                        # best-effort LEFTUP so a partial batch cannot leave the
                        # button stuck (a drag/selection hazard). This runs while
                        # physical input is still blocked, so nothing can
                        # interfere; the finally then releases BlockInput.
                        # events_sent == 0 means nothing was injected, so the
                        # button was never pressed and no release is needed.
                        if events_sent == 1:
                            comp_sent = int(
                                user32.SendInput(
                                    1, ctypes.byref(up_array),
                                    ctypes.sizeof(Input),
                                ) or 0
                            )
                            # The compensating release is best-effort, but if it
                            # ALSO short-delivers the left button is left held
                            # down. Log it so the stuck-button state is
                            # diagnosable rather than silent
                            # (wh-review-click-overlay-glm52.1).
                            if comp_sent != 1:
                                logger.error(
                                    "click_at: compensating LEFTUP also failed: "
                                    "sent %d/1; Win32 error %s -- left button "
                                    "may be stuck down",
                                    comp_sent, kernel32.GetLastError(),
                                )
                        return (False, events_sent)
                    return (True, events_sent)
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
        return (False, 0)
    except Exception as exc:  # noqa: BLE001 -- a real SendInput/Win32 seam can raise
        logger.error("click_at: unexpected error: %s", exc, exc_info=True)
        return (False, 0)

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

    Returns 0 when no window is at the point (the executor treats 0 as a
    mismatch and refuses). Raises are allowed to propagate: the executor maps
    any seam raise to the same fail-closed refusal.
    """
    GA_ROOT = 2

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    window_from_point = user32.WindowFromPoint
    window_from_point.argtypes = [_POINT]
    window_from_point.restype = ctypes.c_void_p  # HWND, 64-bit safe
    get_ancestor = user32.GetAncestor
    get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    get_ancestor.restype = ctypes.c_void_p

    hwnd = window_from_point(_POINT(x, y))
    if not hwnd:
        return 0
    root = get_ancestor(hwnd, GA_ROOT)
    return int(root or hwnd or 0)
